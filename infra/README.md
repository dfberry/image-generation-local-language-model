# SDXL Image Generation — Azure Infrastructure

## Architecture

| Component | Where | How |
|-----------|-------|-----|
| HTTP API door | Azure Container Apps (Dedicated D4 workload profile) | `Dockerfile.cpu` — CPU-only Flask server |
| CLI batch generation | Local machine | `Dockerfile` or direct Python — GPU optional |
| Model cache | Azure Files share (`huggingface-models`) | Mounted at `/root/.cache/huggingface` in the container |

## How the model cache works

The SDXL base model (~7 GB from HuggingFace) is **not baked into the container image**.

1. On the **first cold start**, `diffusers` downloads the model from HuggingFace into `HF_HOME=/root/.cache/huggingface`, which is backed by the Azure Files share (`huggingface-models`).
2. On every **subsequent cold start** (including after scale-to-zero), the model is already on the share — no re-download, no 7 GB transfer.
3. Multiple replicas (max 1 today) would share the same files via the `ReadWrite` mount, safely because inference is read-only.

## Modules

| File | Purpose |
|------|---------|
| `main.bicep` | Orchestrator — wires ACR, Storage, ACA Env, Container App |
| `resources/acr.bicep` | Azure Container Registry (Basic, admin enabled) |
| `resources/storage.bicep` | Storage account + `huggingface-models` file share (100 GiB, Standard_LRS) |
| `resources/aca-env.bicep` | Managed environment + Log Analytics + `models-storage` link to Azure Files + `sdxl-profile` (D4) workload profile |
| `resources/aca.bicep` | Container App — ingress :8000, `/health` probes, scale min 0 / max 1, AzureFile volume mount |

## Sizing decision — Dedicated D4 workload profile: 4 vCPU / 16 Gi

SDXL on CPU requires approximately:
- **~6.5 GB** to load fp16 model weights into RAM
- **~3–5 GB** activation memory during a single inference pass
- **~1 GB** Python / Flask / diffusers overhead
- **Total minimum: ~11–12 GB**

ACA Consumption plan caps at 4 vCPU / 8 Gi (max ratio 1:2). That is not enough for SDXL and will OOM. The fix is a **Dedicated D4 workload profile** (4 vCPU / 16 Gi), which is the smallest Azure-managed profile that fits the workload.

**Cost implication:** Dedicated profile nodes are billed per node-hour, but with `minimumCount: 0` the profile scales to zero alongside the container app — you only pay when the container is actually running. Cold start after scale-to-zero includes node-provisioning time (~60–120 s) on top of the container start and model-load time (~3–5 min on first request, ~30–60 s on warm cache).

A dedicated D8 profile (8 vCPU / 32 Gi) would allow parallel requests and larger SDXL variants but roughly doubles cost. D4 is the right starting point.

## Scale-to-zero

`minReplicas: 0` means the container app idles at zero cost when unused. The Azure Files share retains the model cache across these zero-replica periods. The `/health` probe has a 60-second initial delay to accommodate model loading on first request after a cold start.

---

## Deploy runbook — `azd up`

### Prerequisites

- [Azure Developer CLI (`azd`)](https://learn.microsoft.com/azure/developer-cli/install-azd) installed
- [Docker Desktop](https://www.docker.com/products/docker-desktop/) running locally (needed for `docker build` in the `predeploy` hook)
- An Azure subscription with `Contributor` rights
- Git working directory on branch `diberry/win-provements`

### Step-by-step

```bash
# 1. Authenticate
azd auth login
# A browser window opens — sign in with your Azure account.

# 2. From the repo root, run the full deploy
azd up
# azd will prompt for:
#   Environment name  → e.g. sdxl-dev  (choose any short name, all lowercase)
#   Azure subscription → select from the list
#   Azure location     → e.g. eastus (must support ACA dedicated workload profiles)
#
# azd then:
#   a. Runs infra/main.bicep  → creates ACR, Storage Account + File Share, ACA Environment, Container App
#   b. Runs the predeploy hook → docker build -f Dockerfile.cpu -t <acr>.azurecr.io/sdxl-api:latest .
#      then docker push to ACR
#   c. Updates the Container App to the new image revision
#   d. Prints the service URL
```

### What you should see

```
SUCCESS: Your up workflow to provision and deploy to Azure completed in X minutes Y seconds.
- Endpoint: https://sdxl-generation-api.<unique>.eastus.azurecontainerapps.io
```

### Calling /generate after deploy

```bash
# Replace <FQDN> with the URL printed by azd up
curl -X POST https://<FQDN>/generate \
  -H "Content-Type: application/json" \
  -d '{"prompt": "a golden retriever on the moon, photorealistic"}'
```

> **First-request cold start:** The container must download the ~7 GB SDXL model from HuggingFace on the very first call. Expect 5–10 minutes. Subsequent requests on the warm share are much faster (~3–5 min for CPU inference).

### Teardown

```bash
azd down
# Prompts for confirmation, then deletes all provisioned Azure resources.
```

### Re-deploy after code changes

```bash
# Rebuild and push only the container image, then update ACA (skip infra re-provision)
azd deploy
```

---

## Local CLI batch (no cloud needed)

```bash
# Uses docker-compose.yml with the hf-cache named volume for local persistence
docker compose run --rm app python src/batch.py --prompt "..."
```
