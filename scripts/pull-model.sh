#!/bin/sh
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
set -eu

echo "SDXL API deployed successfully"

if [ -z "${containerAppUrl:-}" ]; then
  echo "pull-model: containerAppUrl output not set; skipping model warm-up." >&2
  exit 0
fi

BASE="https://${containerAppUrl}"
echo "pull-model: Service URL: ${BASE}"

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
while :; do
  STATE=$(get_state || true)
  echo "pull-model: state=${STATE:-unknown}"
  case "${STATE:-unknown}" in
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
  if [ "$(date +%s)" -ge "${DEADLINE}" ]; then
    echo "pull-model: timed out after 45m waiting for model to become ready." >&2
    exit 1
  fi
  sleep 10
done
