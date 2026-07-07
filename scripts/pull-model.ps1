# azd postdeploy hook: land the SDXL model on the mounted Azure Files share.
#
# The share is created empty by infra; the model is downloaded lazily by the
# app. This hook triggers the deployed app's async POST /model/pull and blocks
# until GET /model/status reports "ready", so `azd up` finishes with the model
# already cached on the share (first real /generate is then fast).
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
while ($true) {
  $state = Get-ModelState
  Write-Host "pull-model: state=$state"
  switch ($state) {
    'ready'       { Write-Host 'pull-model: model is ready on the share.'; exit 0 }
    'error'       { Write-Error 'pull-model: model pull reported error.'; exit 1 }
    'not_started' { Invoke-Pull }   # cold revision now serving (traffic shift) — re-trigger
    # in_progress / unknown: keep waiting.
  }
  if ((Get-Date) -ge $deadline) {
    Write-Error 'pull-model: timed out after 45m waiting for model to become ready.'
    exit 1
  }
  Start-Sleep -Seconds 10
}
