#!/usr/bin/env bash
# generate-cloud.sh — POST a local batch.json to the ACA /generate endpoint
# and save every returned image to disk.
#
# Usage:
#   ./scripts/generate-cloud.sh [--warmup] <container-app-root-url> <batch-file> [output-dir]
#
# Example:
#   ./scripts/generate-cloud.sh https://sdxl-generation-api.xxx.eastus.azurecontainerapps.io batch.json ./outputs
#   ./scripts/generate-cloud.sh --warmup https://sdxl-generation-api.xxx.eastus.azurecontainerapps.io batch.json ./outputs

set -euo pipefail

# ── dependency check ────────────────────────────────────────────────────────
if ! command -v jq &>/dev/null; then
  echo "ERROR: jq is required but not found on PATH." >&2
  echo "  Install: https://jqlang.github.io/jq/download/" >&2
  echo "  macOS:   brew install jq" >&2
  echo "  Ubuntu:  sudo apt-get install jq" >&2
  exit 1
fi

# ── usage ───────────────────────────────────────────────────────────────────
usage() {
  echo "Usage: $0 [--warmup] <container-app-root-url> <batch-file> [output-dir]" >&2
  echo "  --warmup                Pre-pull the model and wait until ready before generating" >&2
  echo "  container-app-root-url  Root URL of the ACA app (no /generate suffix)" >&2
  echo "  batch-file              Path to a local JSON batch file" >&2
  echo "  output-dir              Directory to save images (default: .)" >&2
  echo "" >&2
  echo "Example:" >&2
  echo "  $0 https://sdxl-generation-api.xxx.eastus.azurecontainerapps.io batch.json ./outputs" >&2
  echo "  $0 --warmup https://sdxl-generation-api.xxx.eastus.azurecontainerapps.io batch.json ./outputs" >&2
  exit 1
}

# ── parse flags ──────────────────────────────────────────────────────────────
WARMUP=false
POSITIONAL=()
for arg in "$@"; do
  if [[ "$arg" == "--warmup" ]]; then
    WARMUP=true
  else
    POSITIONAL+=("$arg")
  fi
done
set -- "${POSITIONAL[@]+"${POSITIONAL[@]}"}"

[[ $# -lt 2 ]] && usage

# ── args ────────────────────────────────────────────────────────────────────
RAW_URL="${1%/}"          # strip trailing slash
BATCH_FILE="$2"
OUTPUT_DIR="${3:-.}"

GENERATE_URL="${RAW_URL}/generate"
HEALTH_URL="${RAW_URL}/health"
PULL_URL="${RAW_URL}/model/pull"
STATUS_URL="${RAW_URL}/model/status"

# ── validate batch file ──────────────────────────────────────────────────────
if [[ ! -f "$BATCH_FILE" ]]; then
  echo "ERROR: Batch file not found: $BATCH_FILE" >&2
  exit 1
fi

# ── create output dir ────────────────────────────────────────────────────────
mkdir -p "$OUTPUT_DIR"

# ── cold-start health poll ───────────────────────────────────────────────────
MAX_HEALTH_ATTEMPTS=20
HEALTH_SLEEP=15
echo "Polling health at $HEALTH_URL (up to ${MAX_HEALTH_ATTEMPTS} attempts, ${HEALTH_SLEEP}s apart)..."

healthy=false
for (( i=1; i<=MAX_HEALTH_ATTEMPTS; i++ )); do
  http_code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 "$HEALTH_URL" 2>/dev/null || echo "000")
  if [[ "$http_code" == "200" ]]; then
    echo "Container is healthy (attempt $i)."
    healthy=true
    break
  fi
  echo "  Attempt $i/$MAX_HEALTH_ATTEMPTS — HTTP $http_code; waiting ${HEALTH_SLEEP}s for cold start..."
  sleep "$HEALTH_SLEEP"
done

if [[ "$healthy" == "false" ]]; then
  echo "WARNING: Container did not report healthy after $MAX_HEALTH_ATTEMPTS attempts. Proceeding anyway..." >&2
fi

# ── warm-up (opt-in) ─────────────────────────────────────────────────────────
if [[ "$WARMUP" == "true" ]]; then
  echo "Warm-up requested — pulling model..."
  curl -s -X POST "$PULL_URL" -H "Content-Type: application/json" --max-time 30 >/dev/null || true

  MAX_WARMUP_ATTEMPTS=60
  WARMUP_SLEEP=15
  warmed_up=false
  for (( w=1; w<=MAX_WARMUP_ATTEMPTS; w++ )); do
    state=$(curl -s --max-time 10 "$STATUS_URL" | jq -r '.state // "unknown"')
    echo "  Model state: $state (attempt $w/$MAX_WARMUP_ATTEMPTS)"
    if [[ "$state" == "ready" ]]; then
      warmed_up=true
      break
    fi
    if [[ "$state" == "error" ]]; then
      err_detail=$(curl -s --max-time 10 "$STATUS_URL" | jq -r '.error // "unknown error"')
      echo "ERROR: Model warm-up failed: $err_detail" >&2
      exit 1
    fi
    sleep "$WARMUP_SLEEP"
  done
  if [[ "$warmed_up" == "false" ]]; then
    echo "WARNING: Model did not reach 'ready' after $MAX_WARMUP_ATTEMPTS attempts. Proceeding anyway..." >&2
  fi
fi

# ── POST to /generate ────────────────────────────────────────────────────────
echo "Sending batch to $GENERATE_URL ..."

RESPONSE=$(curl \
  --max-time 1800 \
  --fail-with-body \
  -s \
  -X POST "$GENERATE_URL" \
  -H "Content-Type: application/json" \
  --data-binary "@${BATCH_FILE}" \
  2>&1) || {
  # fall back: --fail-with-body may not exist on older curl; try without --fail
  RESPONSE=$(curl \
    --max-time 1800 \
    -s \
    -X POST "$GENERATE_URL" \
    -H "Content-Type: application/json" \
    --data-binary "@${BATCH_FILE}")
}

# ── check top-level status ───────────────────────────────────────────────────
TOP_STATUS=$(echo "$RESPONSE" | jq -r '.status // "unknown"')
if [[ "$TOP_STATUS" == "error" ]]; then
  ERR_MSG=$(echo "$RESPONSE" | jq -r '.error // "no error message"')
  echo "ERROR: Server returned status=error: $ERR_MSG" >&2
  exit 1
fi

# ── iterate results ──────────────────────────────────────────────────────────
saved=0
failed=0

while IFS= read -r item; do
  result_status=$(echo "$item" | jq -r '.status // "unknown"')
  prompt=$(echo "$item" | jq -r '.prompt // ""')
  filename=$(echo "$item" | jq -r '.filename // ""')
  b64=$(echo "$item" | jq -r '.image_base64 // ""')
  error=$(echo "$item" | jq -r '.error // ""')

  if [[ "$result_status" == "ok" && -n "$b64" && -n "$filename" ]]; then
    dest="${OUTPUT_DIR}/${filename}"
    echo "$b64" | base64 -d > "$dest"
    echo "Saved $dest"
    (( saved++ )) || true
  else
    echo "FAILED: prompt=\"$prompt\" error=\"$error\"" >&2
    (( failed++ )) || true
  fi
done < <(echo "$RESPONSE" | jq -c '.results[]')

# ── summary ──────────────────────────────────────────────────────────────────
echo ""
echo "Summary: $saved saved, $failed failed."

if [[ $failed -gt 0 || "$TOP_STATUS" == "error" ]]; then
  exit 1
fi
