#!/usr/bin/env bash
# deploy-sdxl-endpoint.sh — end-to-end Azure CLI deployment of SDXL as an
# Azure Machine Learning Managed Online Endpoint on managed GPU compute.
#
# Usage:
#   RESOURCE_GROUP=rg-sdxl LOCATION=eastus WORKSPACE=mlw-sdxl \
#     ENDPOINT_NAME=sdxl-image-endpoint ./scripts/deploy-sdxl-endpoint.sh
#
# Optional flags mirror the environment variables:
#   --resource-group --location --workspace --endpoint-name --deployment-name
#   --gpu-sku --instance-count --model-name --model-version

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ASSET_DIR="${SCRIPT_DIR}/sdxl-endpoint"
MODEL_DIR="${ASSET_DIR}/model"

RESOURCE_GROUP="${RESOURCE_GROUP:-rg-sdxl-aml}"
LOCATION="${LOCATION:-eastus}"
WORKSPACE="${WORKSPACE:-mlw-sdxl-image-generation}"
ENDPOINT_NAME="${ENDPOINT_NAME:-sdxl-image-endpoint}"
DEPLOYMENT_NAME="${DEPLOYMENT_NAME:-blue}"
GPU_SKU="${GPU_SKU:-Standard_NC6s_v3}"
INSTANCE_COUNT="${INSTANCE_COUNT:-1}"
MODEL_NAME="${MODEL_NAME:-stabilityai/stable-diffusion-xl-base-1.0}"
MODEL_VERSION="${MODEL_VERSION:-${MODEL_REVISION:-main}}"
MODEL_ASSET_NAME="${MODEL_ASSET_NAME:-sdxl-hf-model}"
MODEL_ASSET_VERSION="${MODEL_ASSET_VERSION:-1}"
SAMPLE_PROMPT="${SAMPLE_PROMPT:-A watercolor painting of a friendly robot painting a sunset over the Pacific Northwest}"
OUTPUT_IMAGE="${OUTPUT_IMAGE:-${ASSET_DIR}/sample-response.png}"

usage() {
  cat >&2 <<'USAGE'
Usage: deploy-sdxl-endpoint.sh [options]

Options:
  --resource-group NAME   Azure resource group (env: RESOURCE_GROUP)
  --location LOCATION     Azure region, default eastus (env: LOCATION)
  --workspace NAME        Azure ML workspace (env: WORKSPACE)
  --endpoint-name NAME    Managed online endpoint name (env: ENDPOINT_NAME)
  --deployment-name NAME  Deployment name, default blue (env: DEPLOYMENT_NAME)
  --gpu-sku SKU           Standard_NC6s_v3 or Standard_NC24ads_A100_v4
  --instance-count N      GPU instances, default 1
  --model-name HF_ID      Hugging Face model id
  --model-version REV     Hugging Face revision/branch/tag, default main
  -h, --help              Show this help
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --resource-group) RESOURCE_GROUP="$2"; shift 2 ;;
    --location) LOCATION="$2"; shift 2 ;;
    --workspace) WORKSPACE="$2"; shift 2 ;;
    --endpoint-name) ENDPOINT_NAME="$2"; shift 2 ;;
    --deployment-name) DEPLOYMENT_NAME="$2"; shift 2 ;;
    --gpu-sku) GPU_SKU="$2"; shift 2 ;;
    --instance-count) INSTANCE_COUNT="$2"; shift 2 ;;
    --model-name) MODEL_NAME="$2"; shift 2 ;;
    --model-version|--model-revision) MODEL_VERSION="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "ERROR: Unknown argument: $1" >&2; usage; exit 1 ;;
  esac
done

log() { echo "sdxl-endpoint: $*"; }
fail() { echo "ERROR: $*" >&2; exit 1; }

check_az_version() {
  local minimum_version="2.38.0"
  local installed_version
  installed_version="$(az version -o json | sed -n 's/.*"azure-cli"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')" \
    || fail "Could not determine Azure CLI version. Upgrade with 'az upgrade': https://learn.microsoft.com/cli/azure/install-azure-cli"
  [[ -n "$installed_version" ]] || fail "Could not determine Azure CLI version. Upgrade with 'az upgrade': https://learn.microsoft.com/cli/azure/install-azure-cli"
  if [[ "$(printf '%s\n' "$minimum_version" "$installed_version" | sort -V | head -n1)" != "$minimum_version" ]]; then
    fail "Azure CLI ${minimum_version} or later is required for the ml extension. Installed version: ${installed_version}. Upgrade with 'az upgrade': https://learn.microsoft.com/cli/azure/install-azure-cli"
  fi
}

require_gpu_sku() {
  # Standard_NC6 is retired; do not let callers accidentally target NC v1.
  case "$GPU_SKU" in
    Standard_NC6s_v3)
      GPU_USAGE_NAME="standardNCSv3Family"
      GPU_CORES_PER_INSTANCE=6
      GPU_FAMILY_LABEL="NCv3 (V100)"
      ;;
    Standard_NC24ads_A100_v4)
      GPU_USAGE_NAME="standardNCADSA100v4Family"
      GPU_CORES_PER_INSTANCE=24
      GPU_FAMILY_LABEL="NC A100 v4"
      ;;
    Standard_NC6)
      fail "Standard_NC6 is retired. Use Standard_NC6s_v3 or Standard_NC24ads_A100_v4."
      ;;
    *)
      fail "Unsupported GPU_SKU '$GPU_SKU'. This script supports Standard_NC6s_v3 and Standard_NC24ads_A100_v4."
      ;;
  esac
}

quota_remediation() {
  local required="$1"
  local scope="/subscriptions/${SUBSCRIPTION_ID}/providers/Microsoft.Compute/locations/${LOCATION}"
  cat >&2 <<EOF

GPU quota is insufficient for ${GPU_SKU} (${GPU_FAMILY_LABEL}) in ${LOCATION}.
Required dedicated vCPUs: ${required}; available dedicated vCPUs: ${AVAILABLE_CORES}.

Request quota in the same region as the Azure ML workspace before retrying: ${LOCATION}
AML quota portal:
  https://ml.azure.com/quota

Azure CLI option:
  az extension add --name quota
  az quota create --resource-name "${GPU_USAGE_NAME}" --scope "${scope}" --limit-object '{"limit":{"value":'"${required}"',"limitObjectType":"LimitValue"},"name":{"value":"'"${GPU_USAGE_NAME}"'"},"resourceType":"dedicated"}'

Portal path:
  https://portal.azure.com/#view/Microsoft_Azure_Capacity/QuotaMenuBlade/~/myQuotas
  Filter for region '${LOCATION}' and VM family '${GPU_FAMILY_LABEL}', then request at least ${required} dedicated vCPUs.

EOF
}

check_quota() {
  require_gpu_sku
  local required=$((GPU_CORES_PER_INSTANCE * INSTANCE_COUNT))
  log "Checking Compute quota for ${GPU_SKU} before deployment. Default GPU quota is often 0."

  local usage
  usage="$(az vm list-usage --location "$LOCATION" \
    --query "[?name.value=='${GPU_USAGE_NAME}'] | [0].[currentValue,limit]" -o tsv 2>/dev/null || true)"
  [[ -n "$usage" ]] || fail "Could not read quota usage '${GPU_USAGE_NAME}' in ${LOCATION}. Verify the region supports ${GPU_SKU}."

  read -r CURRENT_CORES LIMIT_CORES <<<"$usage"
  CURRENT_CORES="${CURRENT_CORES:-0}"
  LIMIT_CORES="${LIMIT_CORES:-0}"
  AVAILABLE_CORES=$((LIMIT_CORES - CURRENT_CORES))

  if (( LIMIT_CORES == 0 || AVAILABLE_CORES < required )); then
    quota_remediation "$required"
    exit 1
  fi
  log "Quota OK: ${AVAILABLE_CORES}/${LIMIT_CORES} dedicated vCPUs available for ${GPU_FAMILY_LABEL}; ${required} required."
}

write_assets() {
  mkdir -p "$MODEL_DIR"

  # Keep endpoint/deployment assets as real files so they can be inspected and
  # committed. The script rewrites the parameterized values on each run.
  cat > "${ASSET_DIR}/endpoint.yml" <<EOF
\$schema: https://azuremlschemas.azureedge.net/latest/managedOnlineEndpoint.schema.json
name: ${ENDPOINT_NAME}
auth_mode: key
EOF

  cat > "${ASSET_DIR}/deployment.yml" <<EOF
\$schema: https://azuremlschemas.azureedge.net/latest/managedOnlineDeployment.schema.json
name: ${DEPLOYMENT_NAME}
endpoint_name: ${ENDPOINT_NAME}
model:
  name: ${MODEL_ASSET_NAME}
  version: "${MODEL_ASSET_VERSION}"
  path: ./model
  type: custom_model
environment:
  name: sdxl-diffusers-cuda
  image: mcr.microsoft.com/azureml/curated/acpt-pytorch-2.2-cuda12.1:latest
  conda_file: conda.yaml
code_configuration:
  code: .
  scoring_script: score.py
instance_type: ${GPU_SKU}
instance_count: ${INSTANCE_COUNT}
environment_variables:
  SDXL_MODEL_NAME: ${MODEL_NAME}
  SDXL_MODEL_REVISION: ${MODEL_VERSION}
request_settings:
  request_timeout_ms: 180000
  max_concurrent_requests_per_instance: 1
liveness_probe:
  initial_delay: 600
  period: 30
  timeout: 20
  failure_threshold: 10
readiness_probe:
  initial_delay: 600
  period: 30
  timeout: 20
  failure_threshold: 10
EOF

  cat > "${ASSET_DIR}/sample-request.json" <<EOF
{
  "prompt": "${SAMPLE_PROMPT}",
  "negative_prompt": "blurry, low quality, distorted",
  "steps": 30,
  "guidance": 7.5,
  "width": 1024,
  "height": 1024,
  "seed": 12345
}
EOF

  cat > "${MODEL_DIR}/README.md" <<EOF
# SDXL Hugging Face model marker

This lightweight Azure ML model asset points the deployment at:

- model: \`${MODEL_NAME}\`
- revision: \`${MODEL_VERSION}\`
- precision: \`fp16\`

The weights are resolved by Diffusers at endpoint startup and are not stored in this repository.
EOF
}

wait_for_deployment() {
  local max_attempts=80
  local sleep_seconds=30
  for ((i=1; i<=max_attempts; i++)); do
    state="$(az ml online-deployment show \
      --name "$DEPLOYMENT_NAME" --endpoint-name "$ENDPOINT_NAME" \
      --resource-group "$RESOURCE_GROUP" --workspace-name "$WORKSPACE" \
      --query provisioning_state -o tsv 2>/dev/null || echo Unknown)"
    log "Deployment state: ${state} (attempt ${i}/${max_attempts})"
    if [[ "$state" == "Succeeded" ]]; then
      return 0
    fi
    if [[ "$state" == "Failed" || "$state" == "Canceled" ]]; then
      az ml online-deployment get-logs \
        --name "$DEPLOYMENT_NAME" --endpoint-name "$ENDPOINT_NAME" \
        --resource-group "$RESOURCE_GROUP" --workspace-name "$WORKSPACE" \
        --lines 200 || true
      fail "Deployment provisioning ended in state: ${state}"
    fi
    sleep "$sleep_seconds"
  done
  az ml online-deployment get-logs \
    --name "$DEPLOYMENT_NAME" --endpoint-name "$ENDPOINT_NAME" \
    --resource-group "$RESOURCE_GROUP" --workspace-name "$WORKSPACE" \
    --lines 200 || true
  fail "Timed out waiting for deployment to reach Succeeded."
}

decode_response() {
  python - "$ASSET_DIR/sample-response.json" "$OUTPUT_IMAGE" <<'PY'
import base64
import json
import sys

response_path, output_path = sys.argv[1], sys.argv[2]
with open(response_path, "r", encoding="utf-8") as f:
    data = json.load(f)
if isinstance(data, str):
    data = json.loads(data)
image_b64 = data.get("image_base64") or data.get("image")
if not image_b64:
    raise SystemExit(f"No image_base64 field found in {response_path}: {data}")
with open(output_path, "wb") as f:
    f.write(base64.b64decode(image_b64))
print(output_path)
PY
}

# ── 1. Preflight ────────────────────────────────────────────────────────────
command -v az >/dev/null 2>&1 || fail "Azure CLI 'az' is required: https://learn.microsoft.com/cli/azure/install-azure-cli"
check_az_version
az account show >/dev/null 2>&1 || fail "Run 'az login' and select the target subscription before running this script."
if ! az extension show --name ml >/dev/null 2>&1; then
  log "Azure ML extension missing; installing it."
  az extension add --name ml --only-show-errors
fi

SUBSCRIPTION_ID="$(az account show --query id -o tsv)"
SUBSCRIPTION_NAME="$(az account show --query name -o tsv)"
log "Target subscription: ${SUBSCRIPTION_NAME} (${SUBSCRIPTION_ID})"

# ── 2-4. Resource group and workspace ───────────────────────────────────────
if az group show --name "$RESOURCE_GROUP" >/dev/null 2>&1; then
  log "Resource group exists: ${RESOURCE_GROUP}"
else
  log "Creating resource group: ${RESOURCE_GROUP} (${LOCATION})"
  az group create --name "$RESOURCE_GROUP" --location "$LOCATION" --only-show-errors >/dev/null
fi

if az ml workspace show --name "$WORKSPACE" --resource-group "$RESOURCE_GROUP" >/dev/null 2>&1; then
  log "Azure ML workspace exists: ${WORKSPACE}"
else
  log "Creating Azure ML workspace: ${WORKSPACE}"
  az ml workspace create --name "$WORKSPACE" --resource-group "$RESOURCE_GROUP" --location "$LOCATION" --only-show-errors >/dev/null
fi

# ── 5. GPU quota fail-fast ──────────────────────────────────────────────────
check_quota

# ── 6. Generate inspectable endpoint assets ─────────────────────────────────
write_assets
log "Endpoint assets written to ${ASSET_DIR}"

# ── 7. Create endpoint and create/update GPU deployment ─────────────────────
pushd "$ASSET_DIR" >/dev/null
if az ml online-endpoint show --name "$ENDPOINT_NAME" --resource-group "$RESOURCE_GROUP" --workspace-name "$WORKSPACE" >/dev/null 2>&1; then
  log "Managed online endpoint exists: ${ENDPOINT_NAME}"
else
  log "Creating managed online endpoint: ${ENDPOINT_NAME}"
  az ml online-endpoint create --file endpoint.yml --resource-group "$RESOURCE_GROUP" --workspace-name "$WORKSPACE"
fi

if az ml online-deployment show --name "$DEPLOYMENT_NAME" --endpoint-name "$ENDPOINT_NAME" --resource-group "$RESOURCE_GROUP" --workspace-name "$WORKSPACE" >/dev/null 2>&1; then
  log "Updating deployment: ${DEPLOYMENT_NAME}"
  az ml online-deployment update --file deployment.yml --resource-group "$RESOURCE_GROUP" --workspace-name "$WORKSPACE"
  az ml online-endpoint update --name "$ENDPOINT_NAME" --traffic "${DEPLOYMENT_NAME}=100" --resource-group "$RESOURCE_GROUP" --workspace-name "$WORKSPACE"
else
  log "Creating deployment on ${GPU_SKU}; this can take 20-45 minutes while the image builds and SDXL downloads."
  az ml online-deployment create --file deployment.yml --all-traffic --resource-group "$RESOURCE_GROUP" --workspace-name "$WORKSPACE"
fi
popd >/dev/null

# ── 8. Poll and dump logs on failure ────────────────────────────────────────
wait_for_deployment

# ── 9. Invoke endpoint and decode returned PNG ──────────────────────────────
SCORING_URI="$(az ml online-endpoint show --name "$ENDPOINT_NAME" --resource-group "$RESOURCE_GROUP" --workspace-name "$WORKSPACE" --query scoring_uri -o tsv)"
PRIMARY_KEY="$(az ml online-endpoint get-credentials --name "$ENDPOINT_NAME" --resource-group "$RESOURCE_GROUP" --workspace-name "$WORKSPACE" --query primaryKey -o tsv)"
log "Scoring URI: ${SCORING_URI}"
log "Endpoint key retrieved for key-auth invoke (not printed)."

az ml online-endpoint invoke \
  --name "$ENDPOINT_NAME" \
  --deployment-name "$DEPLOYMENT_NAME" \
  --request-file "${ASSET_DIR}/sample-request.json" \
  --resource-group "$RESOURCE_GROUP" \
  --workspace-name "$WORKSPACE" \
  > "${ASSET_DIR}/sample-response.json"

SAVED_PATH="$(decode_response)"
log "Saved generated image: ${SAVED_PATH}"

# ── 10. Cost teardown hint ──────────────────────────────────────────────────
cat <<EOF

IMPORTANT: Managed GPU endpoints are expensive while running.
Delete the endpoint when finished:
  az ml online-endpoint delete --name "${ENDPOINT_NAME}" --resource-group "${RESOURCE_GROUP}" --workspace-name "${WORKSPACE}" --yes

EOF
