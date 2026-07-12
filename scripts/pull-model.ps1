# azd postdeploy hook: report the deployed API URL and manual model pull commands.
# Keep this hook short and non-blocking so azd up cannot time out while the
# SDXL cache warms on Azure Files.
$ErrorActionPreference = 'Stop'

Write-Host 'pull-model: SDXL API deployed successfully'

if (-not $env:containerAppUrl) {
  Write-Warning 'pull-model: containerAppUrl output not set; skipping model pull instructions.'
  exit 0
}

$base = "https://$($env:containerAppUrl)"
Write-Host "pull-model: Service URL: $base"
Write-Host "pull-model: Trigger cache warm-up: Invoke-RestMethod -Method Post -Uri '$base/model/pull' -ContentType 'application/json' -Body '{}'"
Write-Host "pull-model: Check progress: Invoke-RestMethod -Uri '$base/model/status' or run az containerapp logs show --type console --follow"
