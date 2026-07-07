#!/bin/sh
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
set -eu

echo "SDXL API deployed successfully"

if [ -z "${containerAppUrl:-}" ]; then
  echo "pull-model: containerAppUrl output not set; skipping model warm-up." >&2
  exit 0
fi

BASE="https://${containerAppUrl}"
echo "pull-model: Service URL: ${BASE}"

repair_container_command() {
  # Reset a stranded placeholder http.server command to the real Flask
  # entrypoint so the deployed ACR image actually serves the app.
  if [ -z "${containerAppName:-}" ] || [ -z "${AZURE_RESOURCE_GROUP:-}" ]; then
    echo "pull-model: containerAppName/AZURE_RESOURCE_GROUP not set; cannot verify container command." >&2
    return 0
  fi
  current=$(az containerapp show -n "${containerAppName}" -g "${AZURE_RESOURCE_GROUP}" \
    --query 'properties.template.containers[0].command' -o tsv 2>/dev/null | tr '\t' ' ' | tr -s ' ' | sed 's/ *$//')
  if [ "${current}" = "python3 app.py" ]; then
    echo "pull-model: container command already python3 app.py."
    return 0
  fi
  echo "pull-model: container command is '${current}'; resetting to 'python3 app.py' (undo placeholder http.server)."
  if ! az containerapp update -n "${containerAppName}" -g "${AZURE_RESOURCE_GROUP}" \
      --command 'python3' 'app.py' >/dev/null 2>&1; then
    echo "pull-model: failed to reset container command; continuing anyway." >&2
    return 0
  fi
  echo "pull-model: waiting for the Flask revision to answer /health..."
  health_deadline=$(( $(date +%s) + 600 ))   # 10 minutes
  while :; do
    if curl -fsS "${BASE}/health" >/dev/null 2>&1; then
      echo "pull-model: Flask /health is up."
      return 0
    fi
    if [ "$(date +%s)" -ge "${health_deadline}" ]; then
      echo "pull-model: /health not up after 10m; continuing to model warm-up anyway." >&2
      return 0
    fi
    sleep 10
  done
}

repair_container_command

get_state() {
  curl -fsS "${BASE}/model/status" 2>/dev/null \
    | sed -n 's/.*"state"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p'
}

trigger_pull() {
  echo "pull-model: triggering POST ${BASE}/model/pull"
  curl -fsS -X POST "${BASE}/model/pull" \
    -H "Content-Type: application/json" -d '{}' >/dev/null 2>&1 \
    || echo "pull-model: /model/pull request failed (will retry on next poll)" >&2
}

trigger_pull

echo "pull-model: polling ${BASE}/model/status until ready (a cold ~7-13GB HF download can take several minutes)…"
DEADLINE=$(( $(date +%s) + 2700 ))   # 45 minutes
# Fail-fast guard: a healthy Flask app returns a real state within seconds.
# A sustained 'unknown' streak means /model/status is 404/unreachable (e.g. the
# command self-heal did not take), so bail after ~5m instead of hanging 45m.
UNKNOWN_STREAK=0
UNKNOWN_MAX=30   # 30 polls * 10s = 5 minutes
while :; do
  STATE=$(get_state || true)
  STATE="${STATE:-unknown}"
  echo "pull-model: state=${STATE}"
  if [ "${STATE}" = "unknown" ]; then
    UNKNOWN_STREAK=$(( UNKNOWN_STREAK + 1 ))
  else
    UNKNOWN_STREAK=0
  fi
  case "${STATE}" in
    ready)
      echo "pull-model: model is ready on the share."
      exit 0
      ;;
    error)
      echo "pull-model: model pull reported error." >&2
      exit 1
      ;;
    not_started)
      # A cold revision is now serving (traffic shift) — re-trigger the pull.
      trigger_pull
      ;;
    *)
      # in_progress / unknown / transient curl failure: keep waiting.
      : ;;
  esac
  if [ "${UNKNOWN_STREAK}" -ge "${UNKNOWN_MAX}" ]; then
    echo "pull-model: /model/status unreachable for $(( UNKNOWN_MAX * 10 ))s (state=unknown). The app is likely not serving Flask (check the container command is 'python3 app.py'). Failing fast." >&2
    exit 1
  fi
  if [ "$(date +%s)" -ge "${DEADLINE}" ]; then
    echo "pull-model: timed out after 45m waiting for model to become ready." >&2
    exit 1
  fi
  sleep 10
done
