#!/bin/sh
# azd postdeploy hook: land the SDXL model on the mounted Azure Files share.
#
# The share is created empty by infra; the model is downloaded lazily by the
# app. This hook triggers the deployed app's async POST /model/pull and blocks
# until GET /model/status reports "ready", so `azd up` finishes with the model
# already cached on the share (first real /generate is then fast).
set -eu

echo "SDXL API deployed successfully"

if [ -z "${containerAppUrl:-}" ]; then
  echo "pull-model: containerAppUrl output not set; skipping model warm-up." >&2
  exit 0
fi

BASE="https://${containerAppUrl}"
echo "pull-model: Service URL: ${BASE}"
echo "pull-model: triggering POST ${BASE}/model/pull"
curl -fsS -X POST "${BASE}/model/pull" \
  -H "Content-Type: application/json" -d '{}' >/dev/null || {
  echo "pull-model: /model/pull request failed" >&2
  exit 1
}

echo "pull-model: polling ${BASE}/model/status until ready (a cold ~7-13GB HF download can take several minutes)…"
# Timeout after 45 minutes to cover a cold, empty-share download.
DEADLINE=$(( $(date +%s) + 2700 ))
while :; do
  STATUS=$(curl -fsS "${BASE}/model/status" || echo '{"state":"unknown"}')
  STATE=$(printf '%s' "$STATUS" | sed -n 's/.*"state"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')
  echo "pull-model: state=${STATE:-unknown}"
  case "$STATE" in
    ready)
      echo "pull-model: model is ready on the share."
      exit 0
      ;;
    error)
      echo "pull-model: model pull reported error: ${STATUS}" >&2
      exit 1
      ;;
  esac
  if [ "$(date +%s)" -ge "${DEADLINE}" ]; then
    echo "pull-model: timed out after 45m waiting for model to become ready." >&2
    exit 1
  fi
  sleep 10
done
