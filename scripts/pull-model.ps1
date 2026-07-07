# azd postdeploy hook: land the SDXL model on the mounted Azure Files share.
#
# The share is created empty by infra; the model is downloaded lazily by the
# app. This hook triggers the deployed app's async POST /model/pull and blocks
# until GET /model/status reports "ready", so `azd up` finishes with the model
# already cached on the share (first real /generate is then fast).
#
# SELF-HEAL — undo the stranded placeholder command (RF-3):
# On a brand-new environment azd runs provision(apiExists=false) then
# deploy(image-only). The first provision creates the container with a
# placeholder image + command ['python3','-m','http.server','8000']; deploy
# then swaps in the real ACR image but PRESERVES that command, so the real
# image ends up running a static http.server (every route except / returns a
# 404 "File not found") and the model warm-up below can never see a valid
# /model/status. Before warming up, this hook checks the live container command
# and, if it is not the Flask entrypoint, resets it to ['python3','app.py'] via
# `az containerapp update`, then waits for /health. This makes a single `azd up`
# self-heal without the documented second `azd provision`.
#
# HARDENING — self-healing against the revision traffic-shift race:
# During deploy, ACA may still route the ingress URL to a previously-warmed
# revision while traffic shifts to the new one. A plain pull would hit the old
# "ready" revision, no-op, and exit — leaving the NEW revision cold. So instead
# of trusting a single instant "ready", every poll re-triggers a pull whenever
# the revision currently answering is cold (not_started). The loop only exits
# when the serving revision actually reports "ready" (or "error"). Because all
# revisions mount the same file share, whichever revision runs the pull, the
# weights end up on the share. POST /model/pull is idempotent (a pull already
# in progress returns 202 without launching a duplicate), so re-triggering is
# safe.
$ErrorActionPreference = 'Stop'

Write-Host 'SDXL API deployed successfully'

if (-not $env:containerAppUrl) {
  Write-Warning 'pull-model: containerAppUrl output not set; skipping model warm-up.'
  exit 0
}

$base = "https://$($env:containerAppUrl)"
Write-Host "pull-model: Service URL: $base"

function Repair-ContainerCommand {
  # Reset a stranded placeholder http.server command to the real Flask
  # entrypoint so the deployed ACR image actually serves the app.
  if (-not $env:containerAppName -or -not $env:AZURE_RESOURCE_GROUP) {
    Write-Warning 'pull-model: containerAppName/AZURE_RESOURCE_GROUP not set; cannot verify container command.'
    return
  }
  try {
    $current = az containerapp show -n $env:containerAppName -g $env:AZURE_RESOURCE_GROUP `
      --query 'properties.template.containers[0].command' -o json 2>$null | ConvertFrom-Json
  } catch {
    Write-Warning "pull-model: could not read container command: $($_.Exception.Message)"
    return
  }
  $joined = ($current -join ' ')
  if ($joined -eq 'python3 app.py') {
    Write-Host 'pull-model: container command already python3 app.py.'
    return
  }
  Write-Host "pull-model: container command is '$joined'; resetting to 'python3 app.py' (undo placeholder http.server)."
  try {
    az containerapp update -n $env:containerAppName -g $env:AZURE_RESOURCE_GROUP `
      --command 'python3' 'app.py' 2>&1 | Out-Null
  } catch {
    Write-Warning "pull-model: failed to reset container command: $($_.Exception.Message)"
    return
  }
  Write-Host 'pull-model: waiting for the Flask revision to answer /health...'
  $healthDeadline = (Get-Date).AddMinutes(10)
  while ($true) {
    try {
      $r = Invoke-WebRequest -Uri "$base/health" -TimeoutSec 15 -UseBasicParsing
      if ($r.StatusCode -eq 200) { Write-Host 'pull-model: Flask /health is up.'; return }
    } catch { }
    if ((Get-Date) -ge $healthDeadline) {
      Write-Warning 'pull-model: /health not up after 10m; continuing to model warm-up anyway.'
      return
    }
    Start-Sleep -Seconds 10
  }
}

Repair-ContainerCommand

function Get-ModelState {
  try { return (Invoke-RestMethod -Uri "$base/model/status" -TimeoutSec 30).state }
  catch { return 'unknown' }
}

function Invoke-Pull {
  Write-Host "pull-model: triggering POST $base/model/pull"
  try {
    Invoke-RestMethod -Method Post -Uri "$base/model/pull" -ContentType 'application/json' -Body '{}' -TimeoutSec 30 | Out-Null
  } catch {
    Write-Warning "pull-model: /model/pull request failed (will retry on next poll): $($_.Exception.Message)"
  }
}

Invoke-Pull

Write-Host "pull-model: polling $base/model/status until ready (a cold ~7-13GB HF download can take several minutes)..."
$deadline = (Get-Date).AddMinutes(45)
# Fail-fast guard: a healthy Flask app returns a real state within seconds.
# A sustained 'unknown' streak means /model/status is 404/unreachable (e.g. the
# command self-heal did not take), so bail after ~5m instead of hanging 45m.
$unknownStreak = 0
$unknownMax = 30   # 30 polls * 10s = 5 minutes
while ($true) {
  $state = Get-ModelState
  Write-Host "pull-model: state=$state"
  if ($state -eq 'unknown') { $unknownStreak++ } else { $unknownStreak = 0 }
  switch ($state) {
    'ready'       { Write-Host 'pull-model: model is ready on the share.'; exit 0 }
    'error'       { Write-Error 'pull-model: model pull reported error.'; exit 1 }
    'not_started' { Invoke-Pull }   # cold revision now serving (traffic shift) — re-trigger
    # in_progress / unknown: keep waiting.
  }
  if ($unknownStreak -ge $unknownMax) {
    $secs = $unknownMax * 10
    Write-Error "pull-model: /model/status unreachable for ${secs}s (state=unknown). The app is likely not serving Flask (check the container command is 'python3 app.py'). Failing fast."
    exit 1
  }
  if ((Get-Date) -ge $deadline) {
    Write-Error 'pull-model: timed out after 45m waiting for model to become ready.'
    exit 1
  }
  Start-Sleep -Seconds 10
}
