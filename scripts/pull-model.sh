#!/bin/sh
# azd postdeploy hook: report the deployed API URL and manual model pull commands.
# Keep this hook short and non-blocking so azd up cannot time out while the
# SDXL cache warms on Azure Files.
set -eu

echo "pull-model: SDXL API deployed successfully"

if [ -z "${containerAppUrl:-}" ]; then
  echo "pull-model: containerAppUrl output not set; skipping model pull instructions." >&2
  exit 0
fi

BASE="https://${containerAppUrl}"
echo "pull-model: Service URL: ${BASE}"
echo "pull-model: Trigger cache warm-up: curl -X POST "${BASE}/model/pull" -H "Content-Type: application/json" -d '{}'"
echo "pull-model: Check progress: curl "${BASE}/model/status" or run az containerapp logs show --type console --follow"
