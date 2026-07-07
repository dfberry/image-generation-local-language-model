# azd postdeploy hook: land the SDXL model on the mounted Azure Files share.
#
# The share is created empty by infra; the model is downloaded lazily by the
# app. This hook triggers the deployed app's async POST /model/pull and blocks
# until GET /model/status reports "ready", so `azd up` finishes with the model
# already cached on the share (first real /generate is then fast).
$ErrorActionPreference = 'Stop'

Write-Host 'SDXL API deployed successfully'

if (-not $env:containerAppUrl) {
  Write-Warning 'pull-model: containerAppUrl output not set; skipping model warm-up.'
  exit 0
}

$base = "https://$($env:containerAppUrl)"
Write-Host "pull-model: Service URL: $base"
Write-Host "pull-model: triggering POST $base/model/pull"
Invoke-RestMethod -Method Post -Uri "$base/model/pull" -ContentType 'application/json' -Body '{}' | Out-Null

Write-Host "pull-model: polling $base/model/status until ready (a cold ~7-13GB HF download can take several minutes)..."
# Timeout after 45 minutes to cover a cold, empty-share download.
$deadline = (Get-Date).AddMinutes(45)
while ($true) {
  try { $s = Invoke-RestMethod -Uri "$base/model/status" } catch { $s = [pscustomobject]@{ state = 'unknown' } }
  Write-Host "pull-model: state=$($s.state)"
  if ($s.state -eq 'ready') {
    Write-Host 'pull-model: model is ready on the share.'
    exit 0
  }
  if ($s.state -eq 'error') {
    Write-Error "pull-model: model pull reported error: $($s.error)"
    exit 1
  }
  if ((Get-Date) -ge $deadline) {
    Write-Error 'pull-model: timed out after 45m waiting for model to become ready.'
    exit 1
  }
  Start-Sleep -Seconds 10
}
