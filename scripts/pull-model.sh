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

# --- telemetry helpers -------------------------------------------------------
# Every line is prefixed with elapsed time since the hook started (t+MM:SS) so
# a long cold download is observable. Byte-level HF download progress is not
# tracked by the app; it appears in the container console logs
# (az containerapp logs show --type console). This hook reports state + the
# app's own message/elapsed each poll.
HOOK_START=$(date +%s)
elapsed() {
  _now=$(date +%s); _d=$(( _now - HOOK_START ))
  printf '%02d:%02d' $(( _d / 60 )) $(( _d % 60 ))
}
log() { echo "pull-model: [t+$(elapsed)] $1"; }

log "SDXL API deployed successfully"

if [ -z "${containerAppUrl:-}" ]; then
  echo "pull-model: containerAppUrl output not set; skipping model warm-up." >&2
  exit 0
fi

BASE="https://${containerAppUrl}"
log "Service URL: ${BASE}"

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
    log "container command already python3 app.py."
    return 0
  fi
  log "container command is '${current}'; resetting to 'python3 app.py' (undo placeholder http.server)."
  if ! az containerapp update -n "${containerAppName}" -g "${AZURE_RESOURCE_GROUP}" \
      --command 'python3' 'app.py' >/dev/null 2>&1; then
    echo "pull-model: failed to reset container command; continuing anyway." >&2
    return 0
  fi
  log "waiting for the Flask revision to answer /health..."
  health_deadline=$(( $(date +%s) + 600 ))   # 10 minutes
  while :; do
    if curl -fsS "${BASE}/health" >/dev/null 2>&1; then
      log "Flask /health is up."
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

get_status_body() { curl -fsS "${BASE}/model/status" 2>/dev/null; }

json_str() {  # $1=body $2=key -> string value
  printf '%s' "$1" | sed -n "s/.*\"$2\"[[:space:]]*:[[:space:]]*\"\([^\"]*\)\".*/\1/p"
}
json_num() {  # $1=body $2=key -> numeric value
  printf '%s' "$1" | sed -n "s/.*\"$2\"[[:space:]]*:[[:space:]]*\([0-9.][0-9.]*\).*/\1/p"
}

trigger_pull() {
  log "triggering POST ${BASE}/model/pull"
  curl -fsS -X POST "${BASE}/model/pull" \
    -H "Content-Type: application/json" -d '{}' >/dev/null 2>&1 \
    || echo "pull-model: /model/pull request failed (will retry on next poll)" >&2
}

trigger_pull

log "polling ${BASE}/model/status until ready (a cold ~7-13GB HF download can take several minutes)…"
log "tip: live download progress is in the container console logs — az containerapp logs show --type console --follow"

# Overall deadline is configurable via PULL_MODEL_TIMEOUT_MIN (default 45m).
TIMEOUT_MIN="${PULL_MODEL_TIMEOUT_MIN:-45}"
DEADLINE=$(( $(date +%s) + TIMEOUT_MIN * 60 ))
log "overall deadline: ${TIMEOUT_MIN}m (set PULL_MODEL_TIMEOUT_MIN to change)."

# Fail-fast guard: a healthy Flask app returns a real state within seconds.
# A sustained 'unknown' streak means /model/status is 404/unreachable (e.g. the
# command self-heal did not take), so bail after ~5m instead of hanging.
UNKNOWN_STREAK=0
UNKNOWN_MAX=30   # 30 polls * 10s = 5 minutes
POLL=0
LAST_STATE=""
while :; do
  POLL=$(( POLL + 1 ))
  BODY=$(get_status_body || true)
  if [ -z "${BODY}" ]; then
    STATE="unknown"
    log "poll #${POLL} state=unknown (/model/status unreachable)"
  else
    STATE=$(json_str "${BODY}" state); STATE="${STATE:-unknown}"
    MSG=$(json_str "${BODY}" message)
    ELAP=$(json_num "${BODY}" elapsed_seconds)
    DETAIL=""
    [ -n "${MSG}" ] && DETAIL=" — ${MSG}"
    [ -n "${ELAP}" ] && DETAIL="${DETAIL} [app elapsed ${ELAP}s]"
    log "poll #${POLL} state=${STATE}${DETAIL}"
  fi
  if [ -n "${LAST_STATE}" ] && [ "${STATE}" != "${LAST_STATE}" ]; then
    log "state transition: ${LAST_STATE} -> ${STATE}"
  fi
  LAST_STATE="${STATE}"
  if [ "${STATE}" = "unknown" ]; then
    UNKNOWN_STREAK=$(( UNKNOWN_STREAK + 1 ))
  else
    UNKNOWN_STREAK=0
  fi
  case "${STATE}" in
    ready)
      log "model is ready on the share. total hook time t+$(elapsed)."
      exit 0
      ;;
    error)
      ERR=$(json_str "${BODY:-}" error); ERR="${ERR:-see container logs}"
      echo "pull-model: model pull reported error: ${ERR}" >&2
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
    echo "pull-model: timed out after ${TIMEOUT_MIN}m waiting for model to become ready." >&2
    exit 1
  fi
  sleep 10
done
