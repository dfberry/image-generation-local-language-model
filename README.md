# SDXL Image Generation

Containerized Stable Diffusion XL image generation service. Pass a batch JSON file, get PNG images out. Deploy locally with Docker Compose or to Azure Container Apps.

**Key Features:**
- **CLI batch runner** — pass a JSON file of prompts, images land in `./outputs/`
- Optional thin HTTP server (`app.py`) for the same pipeline over REST
- Model downloads on demand into a **persistent cache volume** — ~7 GB download once, reused every run; image itself is small (~hundreds of MB + torch)
- Automatic OOM handling with step reduction retry
- GPU-accelerated (NVIDIA CUDA) with CPU fallback; Windows devbox → CPU variant
- Zero-cost idle scaling on Azure Container Apps (scales to zero when not generating)
- HTTPS ingress on Azure Container Apps
- Production logging and health checks

## Why Container + Cloud?

Building ML inference APIs locally works great until you need:
- **Elastic scaling** — generate 5 images one day, 500 the next, without paying for idle compute
- **GPU access without hardware** — no $5k GPU investment, $0.30/hour on-demand
- **Reproducibility** — same code runs locally and in production
- **Isolation** — no dependency conflicts with your system Python
- **Cost control** — turn it off when not in use

This scaffolding gives you that without vendor lock-in (Bicep + container = portable to any cloud).

## Architecture

The service is built in three layers: application code, a Docker container, and a cloud host. The SDXL model (~7 GB base, ~6 GB optional refiner) lives in a **persistent cache volume** — it downloads on the first run and is reused on every subsequent run. Understanding *when* it downloads and *when* it loads into memory is the key to predicting first-run vs warm-run behaviour.

```
┌─────────────────────────────────────────────┐
│  Cloud layer (Azure Container Apps)         │
│  ┌───────────────────────────────────────┐  │
│  │  Container layer (Docker image)       │  │
│  │  ┌─────────────────────────────────┐  │  │
│  │  │  Application layer (Flask)      │  │  │
│  │  │  app.py  ·  generate.py         │  │  │
│  │  └─────────────────────────────────┘  │  │
│  └───────────────────────────────────────┘  │
└─────────────────────────────────────────────┘
```

### Model timing at a glance

| Layer | When model downloads | When model loads into app | Persistence |
|---|---|---|---|
| **Application** | First `run` (CLI) or first `POST /generate` (server) — lazy, on demand | Same invocation that triggered the download; stays resident for the process lifetime | Persists in the mounted cache volume across runs |
| **Container** | **Not at build time.** First `docker compose run` downloads ~7 GB into the `hf-cache` named volume | After download, loads from volume into CPU/GPU RAM — first run is slow; all later runs are fast | Volume survives container removal; only lost if the named volume is deleted |
| **Cloud (ACA)** | First request to a fresh replica if the Azure Files share is empty; skipped on subsequent starts if the share is populated | Loads from the Azure Files share into GPU RAM on each container start | Azure Files share persists across scale-to-zero; shared across all replicas |

### Layer 1 — Application

**Files:** `app.py` (Flask server), `src/image_generation/generate.py` (pipeline logic)

**Request flow:**

- `GET /health` — always returns immediately; reports device (CUDA / CPU) and timestamp. Does **not** trigger any model load.
- `POST /generate` — accepts a JSON array of prompts and generation parameters, runs the SDXL pipeline, and writes PNG files to `/app/outputs/`.

**Model loading — lazy on first run:**

The application calls `from_pretrained(...)` on its first invocation (CLI run or first `POST /generate` to the server), not at startup. Weights download from Hugging Face Hub into the `HF_HOME` cache on that first call. You will see:

```
📥 Loading SDXL base model (first run downloads ~7GB)...
```

Once loaded, the pipeline stays resident in memory for the lifetime of the process.

### Layer 2 — Container

**Files:** `Dockerfile.cpu` (CPU — `python:3.11-slim`, **primary**), `Dockerfile` (GPU — `nvidia/cuda:12.1.1-runtime-ubuntu22.04`, requires NVIDIA Container Toolkit)

Both Dockerfiles install Python dependencies and copy application code only — **no model download at build time**. Build completes in ~2–3 minutes; the resulting image is small (hundreds of MB + torch, not 7 GB).

The model lives in a **persistent named volume** (`hf-cache`) mounted at `HF_HOME` (`/root/.cache/huggingface`). It downloads once on the first run and is reused on every subsequent run regardless of container restarts or image rebuilds.

**What happens on first `docker compose run`:**

1. Container starts, `/data` is bind-mounted from the host.
2. `generate.py` loads, calls `from_pretrained(...)` → downloads ~7 GB from Hugging Face into the `hf-cache` volume.
3. Images are generated and written to `/data/outputs/` (visible on the host immediately).

**What happens on every subsequent run:**

1. Container starts.
2. `generate.py` finds weights already in the `hf-cache` volume — loads into memory in seconds, no download.
3. Generation starts almost immediately.

**Deleting the volume forces a fresh download:**

```console
docker volume rm $(docker compose config --volumes | head -1)
```

Or by name: `docker volume ls` then `docker volume rm <project>_hf-cache`.

### Layer 3 — Cloud (Azure Container Apps)

`azd up` builds the Docker image, pushes it to Azure Container Registry, and deploys a Container App in the configured region (default: `eastus`).

**Persistent model storage via Azure Files:**

The same pattern that works locally with a named volume works on ACA with an Azure Files share. Mount the share at `/root/.cache/huggingface` (= `HF_HOME`) on every replica. The model downloads once (first cold start) and persists across scale-to-zero and across all replicas.

**Recommended production setup:**

1. **Azure Files NFS share** mounted at `/root/.cache/huggingface` on the Container App. See `/azure/container-apps/storage-mounts`.

2. **Optional: init container** to pre-warm the share before the main container starts. Run `scripts/download_model.py` as an init container — on the very first deploy it downloads ~7 GB; on all subsequent starts it detects weights already present and exits immediately. See `/azure/container-apps/init-containers`.

3. **Set `minReplicas: 1`** to keep one warm replica loaded and avoid cold-start load time.

4. **Add a readiness probe** on `/health` so ACA only routes traffic once the model is in memory:

   ```yaml
   probes:
     - type: Readiness
       httpGet:
         path: /health
         port: 8000
       initialDelaySeconds: 30
       periodSeconds: 10
       failureThreshold: 18
   ```

See `/azure/container-apps/health-probes` for ACA probe configuration reference.

**Local volume ↔ ACA Azure Files — same mechanism, two backings:**

| | Local (docker compose) | ACA |
|---|---|---|
| Storage backing | Docker named volume (`hf-cache`) | Azure Files share (NFS preferred) |
| Mount path | `/root/.cache/huggingface` | `/root/.cache/huggingface` (same) |
| Populate | First `docker compose run` | Init container or lazy first request |
| Survives container restart | ✅ | ✅ (scale-to-zero safe) |
| Shared across replicas | N/A | ✅ |

## Prerequisites

### Local Development
- Docker (for containerization)
- Python 3.10+ (for local runs)
- NVIDIA CUDA toolkit (optional, for local GPU; Docker handles it)
- GPU with ≥8GB VRAM (or CPU, but slow)

### Azure Deployment
- Azure CLI (`az`)
- Azure Developer CLI (`azd`)
- Azure subscription with:
  - Container Registry access
  - Container Apps access
  - Sufficient GPU quota (default quota is 0; request increase)
- `docker` CLI (for building images)

**Install `azd`:**
```bash
# macOS
brew install azure-developer-cli

# Linux / WSL
curl -fsSL https://aka.ms/install-azd.sh | bash

# Windows
choco install azd
# or
winget install Microsoft.AzureDeveloperCLI
```

## Local Development

### 1. Install Dependencies

```console
pip install -r requirements.txt
```

**Note:** Sets up local Python dependencies for running the CLI or server directly on the host, outside Docker. The model downloads on demand the first time you run the CLI — not during `pip install`.

### 2. Run CLI batch locally

```bash
PYTHONPATH=src python -m image_generation.generate --batch-file batch.json
```

```powershell
$env:PYTHONPATH = "src"
python -m image_generation.generate --batch-file batch.json
```

First run downloads ~7 GB from Hugging Face into the default HF cache (`~/.cache/huggingface`). Subsequent runs load from cache. Output PNGs land in the paths specified in `batch.json`.

### 3. Optional: Run Flask Server Locally

```console
python app.py
```

Output:
```
WARNING in flask.app: This is a development server. Do not use it in production.
 * Running on http://0.0.0.0:8000
```

### 4. Test the API

**Health check:**
```bash
curl http://localhost:8000/health
```

**PowerShell:**
```powershell
Invoke-RestMethod http://localhost:8000/health
```

Response:
```json
{
  "status": "healthy",
  "device": "cuda (NVIDIA)",
  "timestamp": "2026-07-03T09:23:14.123456Z"
}
```

**Generate a single image:**
```bash
curl -X POST http://localhost:8000/generate \
  -H "Content-Type: application/json" \
  -d '{
    "prompts": [
      {
        "prompt": "a tropical sunset with palm trees, magical realism, oil painting style",
        "seed": 42,
        "output": "outputs/sunset.png"
      }
    ],
    "steps": 40,
    "guidance": 7.5,
    "width": 1024,
    "height": 1024,
    "refine": false
  }'
```

> **PowerShell users:** In PowerShell, `curl` is an alias for `Invoke-WebRequest` and does not accept bash-style `\` line continuations or `-H`/`-d` flags. Use `Invoke-RestMethod` instead:
>
> ```powershell
> $body = @{
>   prompts = @(
>     @{
>       prompt = "a tropical sunset with palm trees, magical realism, oil painting style"
>       seed   = 42
>       output = "outputs/sunset.png"
>     }
>   )
>   steps    = 40
>   guidance = 7.5
>   width    = 1024
>   height   = 1024
>   refine   = $false
> } | ConvertTo-Json -Depth 5
>
> Invoke-RestMethod -Uri http://localhost:8000/generate -Method Post -ContentType 'application/json' -Body $body
> ```
>
> Or call the real curl binary explicitly as `curl.exe` (single line, no `\`).

**Batch generation (multiple images):**
```bash
curl -X POST http://localhost:8000/generate \
  -H "Content-Type: application/json" \
  -d '{
    "prompts": [
      {
        "prompt": "underwater coral reef with bioluminescent creatures, fantasy art",
        "seed": 43,
        "output": "outputs/underwater.png"
      },
      {
        "prompt": "mystical forest with glowing flowers and ethereal fog, impressionist",
        "seed": 44,
        "output": "outputs/forest.png"
      }
    ],
    "steps": 40,
    "guidance": 7.5,
    "width": 1024,
    "height": 1024,
    "refine": false
  }'
```

**PowerShell:**
```powershell
$body = '{
  "prompts": [
    { "prompt": "underwater coral reef with bioluminescent creatures, fantasy art", "seed": 43, "output": "outputs/underwater.png" },
    { "prompt": "mystical forest with glowing flowers and ethereal fog, impressionist", "seed": 44, "output": "outputs/forest.png" }
  ],
  "steps": 40, "guidance": 7.5, "width": 1024, "height": 1024, "refine": false
}'
Invoke-RestMethod -Uri http://localhost:8000/generate -Method Post -ContentType 'application/json' -Body $body
```

Response (success):
```json
{
  "status": "success",
  "device": "cuda",
  "results": [
    {
      "prompt": "underwater coral reef...",
      "output": "outputs/underwater.png",
      "status": "ok",
      "error": null
    },
    {
      "prompt": "mystical forest...",
      "output": "outputs/forest.png",
      "status": "ok",
      "error": null
    }
  ],
  "timestamp": "2026-07-03T09:23:45.987654Z"
}
```

Response (on OOM with automatic retry):
```json
{
  "status": "success",
  "device": "cuda",
  "results": [
    {
      "prompt": "...",
      "output": "outputs/image.png",
      "status": "ok",
      "error": null
    }
  ],
  "timestamp": "..."
}
```

**Handle OOM gracefully:**
If inference hits out-of-memory, the app retries automatically by halving steps:
- Initial attempt: 40 steps
- 1st retry: 20 steps
- 2nd retry: 10 steps
- If all fail: return `"status": "oom_error"` with helpful message

## Build and Run

Everything runs via Docker Compose — no Python environment needed on the host.

### Prerequisites

- **Docker Desktop ≥ 23** (with Docker Compose V2)
- **Windows devbox / WSL2:** use the CPU image by default — see the note below
- No Hugging Face token needed for the default public SDXL model

### Which image?

> **Windows devbox / WSL2 (no NVIDIA GPU passthrough):** use `Dockerfile.cpu` (the default in `docker-compose.yml`) — do NOT use `--gpus all`. Most Windows developer machines fall into this category. If you see `nvidia-container-cli: initialization error: WSL environment detected but no adapters were found`, you need the CPU image.
>
> **GPU image** (`Dockerfile` + `--gpus all`) requires a real NVIDIA GPU **and** the [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html) for WSL2. Verify with `nvidia-smi` first.

### 1. Build

```console
docker compose build
```

**What it does:** installs Python dependencies and copies app code. No model download at build time — the image is small (~hundreds of MB + torch). Build completes in ~2–3 minutes.

### 2. Write a batch file

Create a JSON file (e.g. `batch.json`) in the repo root — a top-level array, one object per image:

```json
[
  {
    "output": "outputs/sunset.png",
    "prompt": "a tropical sunset with palm trees, magical realism, oil painting style",
    "negative_prompt": "blur, low quality",
    "seed": 42
  },
  {
    "output": "outputs/forest.png",
    "prompt": "mystical forest with glowing flowers and ethereal fog, impressionist",
    "negative_prompt": "blur, low quality",
    "seed": 43
  }
]
```

### 3. Run

```console
docker compose run --rm img-gen --batch-file /data/batch.json
```

**First run** (~7–15 min): the model downloads ~7 GB from Hugging Face into the `hf-cache` named volume. You'll see:
```
📥 Loading SDXL base model (first run downloads ~7GB)...
```
Images land in `./outputs/` on your host as they complete.

**Every subsequent run** (seconds to start): the model is already in the `hf-cache` volume — loads into memory and generation starts almost immediately.

### 4. View outputs

Generated PNGs are written to `./outputs/` (bind-mounted from the host via `./:/data:rw`). No need to copy files out of the container.

### 5. Optional: HTTP server

The HTTP server (`app.py`) calls the same pipeline as the CLI. Start it with the `server` profile:

```console
docker compose --profile server up
```

**Health check:**

```bash
curl http://localhost:8000/health
```

```powershell
Invoke-RestMethod http://localhost:8000/health
```

**Generate via HTTP** (bash):

```bash
curl -X POST http://localhost:8000/generate \
  -H "Content-Type: application/json" \
  -d '[{"output": "outputs/sunset.png", "prompt": "a tropical sunset with palm trees", "seed": 42}]'
```

**Generate via HTTP** (PowerShell — `curl` is an alias for `Invoke-WebRequest` in PS; use `Invoke-RestMethod`):

```powershell
$body = '[{"output": "outputs/sunset.png", "prompt": "a tropical sunset with palm trees", "seed": 42}]'
Invoke-RestMethod -Uri http://localhost:8000/generate -Method Post -ContentType 'application/json' -Body $body
```

Stop the server:

```console
docker compose --profile server down
```

### 6. Cloud: ACA + Azure Files

The same pattern that works locally with the `hf-cache` named volume maps directly to Azure Container Apps with an Azure Files share. Mount the share at `/root/.cache/huggingface` — model downloads once into the share, persists across scale-to-zero, shared across all replicas.

- See `/azure/container-apps/storage-mounts` to attach an Azure Files share.
- See `/azure/container-apps/init-containers` to pre-warm the share with `scripts/download_model.py` before the main container starts.
- See `/azure/container-apps/health-probes` for readiness probe config.

| | Local (docker compose) | ACA |
|---|---|---|
| Model storage | `hf-cache` named volume | Azure Files NFS share |
| Mount path | `/root/.cache/huggingface` | `/root/.cache/huggingface` |
| Model download | First `docker compose run` | First ACA start (init container or lazy) |
| Survives restart | ✅ | ✅ (scale-to-zero safe) |
| Shared across replicas | N/A | ✅ |

## Deploy to Azure Container Apps

### 1. Authenticate & Initialize

```console
# Login to Azure
azd auth login

# Initialize project (select location, resource group strategy)
azd init
```

When prompted:
- **Environment name:** `sdxl-prod` (or similar)
- **Azure location:** `eastus` (or nearest region with GPU quota)
- **Resource group naming:** accept default

### 2. Build & Deploy

```console
azd up
```

**What this does:**
1. Builds Docker image
2. Pushes to Azure Container Registry (created automatically)
3. Deploys Bicep template (Container Apps, environment, ACR)
4. Configures ingress, health checks, GPU allocation
5. Outputs HTTPS URL

**Output example:**
```
✓ Deploying service api
✓ Service deployed to https://sdxl-generation-api.victoriousforest-12ab.eastus.azurecontainerapps.io

Use 'azd deploy' for subsequent updates
```

**First run takes ~10-20 min:**
- Infrastructure provisioning (~3-5 min)
- Container image build & push (~1-2 min — small image, no model)
- First request: model downloads ~7 GB into the Azure Files share (~5-10 min); subsequent requests are fast

### 3. Get Your API URL

```bash
azd env get-values | grep CONTAINER_APP_FQDN
```

**PowerShell:**
```powershell
azd env get-values | Select-String CONTAINER_APP_FQDN
```

Or:
```bash
az containerapp show --name sdxl-generation-api --resource-group $RESOURCE_GROUP_NAME \
  --query 'properties.configuration.ingress.fqdn' -o tsv
```

**PowerShell:**
```powershell
az containerapp show --name sdxl-generation-api --resource-group $env:RESOURCE_GROUP_NAME `
  --query 'properties.configuration.ingress.fqdn' -o tsv
```

### 4. Test Cloud Deployment

```bash
CONTAINER_APP_URL="https://sdxl-generation-api.victoriousforest-12ab.eastus.azurecontainerapps.io"

curl $CONTAINER_APP_URL/health

curl -X POST $CONTAINER_APP_URL/generate \
  -H "Content-Type: application/json" \
  -d '{
    "prompts": [
      {
        "prompt": "a tropical sunset with palm trees, magical realism, oil painting style",
        "seed": 42
      }
    ],
    "steps": 40,
    "guidance": 7.5
  }'
```

**PowerShell:**
```powershell
$CONTAINER_APP_URL = "https://sdxl-generation-api.victoriousforest-12ab.eastus.azurecontainerapps.io"

Invoke-RestMethod "$CONTAINER_APP_URL/health"

$body = '{
  "prompts": [
    { "prompt": "a tropical sunset with palm trees, magical realism, oil painting style", "seed": 42 }
  ],
  "steps": 40, "guidance": 7.5
}'
Invoke-RestMethod -Uri "$CONTAINER_APP_URL/generate" -Method Post -ContentType 'application/json' -Body $body
```

## Use from Cloud

### Basic Request

```bash
curl -X POST https://<your-container-app-url>/generate \
  -H "Content-Type: application/json" \
  -d '{
    "prompts": [
      {"prompt": "your prompt here", "seed": 123}
    ]
  }'
```

**PowerShell:**
```powershell
$body = '{
  "prompts": [
    {"prompt": "your prompt here", "seed": 123}
  ]
}'
Invoke-RestMethod -Uri https://<your-container-app-url>/generate -Method Post -ContentType 'application/json' -Body $body
```

### Batch Generation

```bash
curl -X POST https://<your-container-app-url>/generate \
  -H "Content-Type: application/json" \
  -d '{
    "prompts": [
      {"prompt": "underwater kingdom", "seed": 1},
      {"prompt": "floating islands", "seed": 2},
      {"prompt": "crystal caves", "seed": 3}
    ],
    "steps": 50,
    "guidance": 8.0,
    "width": 1024,
    "height": 1024,
    "refine": false
  }'
```

**PowerShell:**
```powershell
$body = '{
  "prompts": [
    {"prompt": "underwater kingdom", "seed": 1},
    {"prompt": "floating islands", "seed": 2},
    {"prompt": "crystal caves", "seed": 3}
  ],
  "steps": 50, "guidance": 8.0, "width": 1024, "height": 1024, "refine": false
}'
Invoke-RestMethod -Uri https://<your-container-app-url>/generate -Method Post -ContentType 'application/json' -Body $body
```

### Parameters

| Parameter | Type | Default | Notes |
|-----------|------|---------|-------|
| `prompts` | array | required | List of prompt objects |
| `prompts[].prompt` | string | required | Text prompt for SDXL |
| `prompts[].seed` | int | random | Set for reproducibility |
| `prompts[].output` | string | auto | Filename; auto-generated if omitted |
| `steps` | int | 40 | Inference steps (1–150); more = slower, higher quality |
| `guidance` | float | 7.5 | CFG scale (0–50); higher = more prompt adherence |
| `width` | int | 1024 | Image width (512, 768, 1024) |
| `height` | int | 1024 | Image height (512, 768, 1024) |
| `refine` | bool | false | Use base+refiner pipeline (slower, higher quality) |
| `cpu` | bool | false | Force CPU (slow, no GPU needed) |

### Scaling

Scale to handle more concurrent requests:

```console
# Edit infra/resources/aca.bicep
# Change: maxReplicas: 1 → maxReplicas: 5

azd deploy
```

**Scaling rules:**
- **minReplicas: 0** — container stops when idle (no charges)
- **maxReplicas: 1** — default; can handle 1 request at a time
- **maxReplicas: 5** — can queue/handle up to 5 concurrent requests

**Cost example (eastus region with 1 GPU vCPU):**
- Idle: $0/hr
- Generating (1 replica, 1 min): $0.005
- Generating (5 min): $0.025
- 100 batches/month, 5 min each: ~$12/month

### Monitor Logs

**View live logs:**
```bash
az containerapp logs show --name sdxl-generation-api --resource-group $RESOURCE_GROUP_NAME \
  --container-name sdxl-api --follow
```

**PowerShell:**
```powershell
az containerapp logs show --name sdxl-generation-api --resource-group $env:RESOURCE_GROUP_NAME `
  --container-name sdxl-api --follow
```

**Get recent logs:**
```bash
az containerapp logs show --name sdxl-generation-api --resource-group $RESOURCE_GROUP_NAME \
  --container-name sdxl-api --tail 50
```

**PowerShell:**
```powershell
az containerapp logs show --name sdxl-generation-api --resource-group $env:RESOURCE_GROUP_NAME `
  --container-name sdxl-api --tail 50
```

**Common log messages:**
```
📥 Loading SDXL base model (first run downloads ~7GB)...
⚡ Compiling UNet with torch.compile (one-time, ~30s)...
🎨 Running base model (40 steps)...
✅ Saved: /app/outputs/image_20260703_092314.png
```

## Customize & Extend

### Adjust Model Quality

**Increase quality (slower):**
```json
{
  "prompts": [...],
  "steps": 60,
  "guidance": 9.0,
  "refine": true
}
```

**Speed up (lower quality):**
```json
{
  "prompts": [...],
  "steps": 20,
  "guidance": 5.0,
  "refine": false
}
```

### Add Custom Prompts

Modify `prompts` array to include your own prompt library. Example with full customization:

```json
{
  "prompts": [
    {
      "prompt": "a serene tropical lagoon at sunset, bioluminescent water, oil painting, fantasy",
      "seed": 42,
      "output": "lagoon.png"
    }
  ],
  "steps": 45,
  "guidance": 8.5,
  "width": 1024,
  "height": 1024,
  "refine": false
}
```

### Modify GPU Allocation

Edit `infra/resources/aca.bicep`:
```bicep
resources: {
  cpu: json('4')          # 4 vCPU
  memory: '16Gi'          # 16 GB RAM
  ephemeralStorage: '1Gi' # Temp storage
}
```

**Note:** GPU allocation is fixed at Container Apps level (currently 1 GPU). For multi-GPU, scale horizontally (maxReplicas).

### Add Application Insights Monitoring

Add monitoring module to `infra/main.bicep`:
```bicep
module monitoring 'resources/monitoring.bicep' = {
  name: 'monitoring-deployment'
  params: {
    location: location
    environmentName: environmentName
  }
}
```

## Cleanup

### Delete Azure Resources

```console
azd down
```

**This deletes:**
- Container App and environment
- Container Registry
- Resource group (if you created it)
- All associated costs stop immediately

### Remove Local Images and Volumes

```bash
docker compose down --rmi all
rm -rf outputs/  # optional
```

```powershell
docker compose down --rmi all
Remove-Item -Recurse -Force outputs/  # optional
```

To also delete the cached model weights (forces a fresh ~7 GB download on next run):

```console
docker volume rm $(docker volume ls -q --filter name=hf-cache)
```

## Troubleshooting

### Exit Code 139 on CPU / WSL2 — Docker Desktop Memory Limit

**Symptom:** The `img-gen` container exits immediately with code **139** (segfault / OOM kill). No Python traceback appears.

**Root cause:** SDXL on CPU (fp32) needs **12–16 GB of RAM** for a single 1024×1024 image. Docker Desktop on Windows runs inside WSL2, which defaults to capping the VM at **~8 GB**. When the container exceeds that limit the Linux OOM killer sends SIGKILL, producing exit code 139 with no Python-level error.

**Fix — raise the WSL2 memory cap:**

1. Open (or create) `%UserProfile%\.wslconfig` in a text editor.
2. Add or update the `[wsl2]` section:
   ```ini
   [wsl2]
   memory=16GB
   swap=8GB
   ```
3. Shut down the WSL2 VM and restart Docker Desktop:
   ```console
   wsl --shutdown
   ```
   Then reopen Docker Desktop (it restarts the WSL2 backend automatically).

**Faster smoke-test alternative:** Run at a smaller resolution while the model loads correctly. The `--width` and `--height` CLI flags apply globally to every item in the batch:

```console
docker compose run --rm img-gen --batch-file /data/batch.example.json --width 768 --height 768 --steps 20
```

768×768 at 20 steps typically needs ~8 GB and completes in a few minutes on CPU.

### OOM (Out of Memory) Errors

**Symptom:** Generation fails with "out of memory" error.

**Cause:** GPU/memory insufficient for inference parameters.

**Fix:**
```bash
# App auto-retries with half steps, but you can also reduce upfront:
curl -X POST http://localhost:8000/generate \
  -H "Content-Type: application/json" \
  -d '{
    "prompts": [{"prompt": "..."}],
    "steps": 20,  # reduced from 40
    "guidance": 6.0  # slightly reduced
  }'

# Or switch to smaller resolution:
curl -X POST http://localhost:8000/generate \
  -H "Content-Type: application/json" \
  -d '{
    "prompts": [{"prompt": "..."}],
    "width": 768,
    "height": 768  # instead of 1024x1024
  }'
```

**PowerShell:**
```powershell
# App auto-retries with half steps, but you can also reduce upfront:
$body = '{ "prompts": [{"prompt": "..."}], "steps": 20, "guidance": 6.0 }'
Invoke-RestMethod -Uri http://localhost:8000/generate -Method Post -ContentType 'application/json' -Body $body

# Or switch to smaller resolution:
$body = '{ "prompts": [{"prompt": "..."}], "width": 768, "height": 768 }'
Invoke-RestMethod -Uri http://localhost:8000/generate -Method Post -ContentType 'application/json' -Body $body
```

### Model Download Fails

**Symptom:** First run hangs or times out.

**Cause:** 
- Container can't reach Hugging Face (network blocked)
- Insufficient disk space for the ~7 GB model cache

**Fix:**

The model downloads automatically into the `hf-cache` named volume on first `docker compose run`. If the download is blocked or interrupted:

1. Check network access to `huggingface.co` from inside the container:
   ```console
   docker compose run --rm img-gen python3 -c "import urllib.request; urllib.request.urlopen('https://huggingface.co').read(100)"
   ```
2. **Optional pre-warm** — populate the cache volume before the first generation run using the helper script:
   ```console
   docker compose run --rm img-gen python3 /app/scripts/download_model.py
   ```
3. For ACA, increase `ephemeralStorage` in `aca.bicep` if the Azure Files share isn't mounted and the container is storing to ephemeral disk:
   ```bicep
   ephemeralStorage: '15Gi'  # up from 1Gi
   ```

### Slow Inference

**Symptom:** Generation takes >2 minutes.

**Causes:**
- GPU might be preempted/throttled
- Model still compiling (first run only)
- CPU fallback active (no GPU detected)

**Fix:**
```bash
# Check device:
curl http://localhost:8000/health
```

```powershell
# Check device:
Invoke-RestMethod http://localhost:8000/health
```

If `device` reports `cpu` but you have a GPU, confirm you're using the GPU Dockerfile (`Dockerfile`) and passing `--gpus all`. On Windows devbox / WSL2 without NVIDIA GPU passthrough the CPU is expected — see the [Which image?](#which-image) note.

For the server via compose (CPU image):
```console
docker compose --profile server up
```

On Azure, GPU preemption is rare; try scaling via `infra/resources/aca.bicep`.

### Container Won't Start

**Symptom:** Container App shows "Failed" status.

**Causes:**
- Port 8000 not exposed
- Invalid environment variable
- Missing Python dependencies

**Fix:**
```console
# Check logs
az containerapp logs show --name sdxl-generation-api --resource-group $RESOURCE_GROUP_NAME
```

```bash
# Rebuild locally and test
docker compose build
docker compose --profile server up

# If local works, issue is in Azure config; redeploy:
azd deploy
```

```powershell
# Rebuild locally and test
docker compose build
docker compose --profile server up

# If local works, issue is in Azure config; redeploy:
azd deploy
```

## Cost Analysis

### Local Development
- **Electricity:** ~200W GPU × $0.12/kWh = ~$0.002 per 5-min batch
- **Hardware amortization:** $5000 GPU / 60 months = $83/month (if using)
- **Total:** $0–100/month depending on local compute

### Azure Container Apps (eastus)
- **GPU vCPU:** $0.30/hour active
- **Compute + memory:** ~$0.04/hour
- **Storage (model cache):** ~$1/month (10GB at $0.1/GB)
- **Idle:** $0/month (scales to zero)

**Example: 100 batches/month, 5 min each:**
- Total GPU time: 500 min = 8.33 hours
- Cost: 8.33 × $0.34 = ~$2.83/month
- Plus model storage: ~$1/month
- **Total: ~$4/month**

### Cost Comparison
| Scenario | Local | Azure |
|----------|-------|-------|
| 5 batches/week | $20/month | <$1/month |
| Daily batches | $50/month | $5/month |
| Always-on (100 req/day) | $150/month | $50/month |

**Azure wins** when:
- Utilization is bursty (scale to zero)
- You don't own a GPU
- You need high concurrency (easy scaling)

## Production Checklist

- [ ] Configure resource group & subscription in `azd`
- [ ] Adjust `maxReplicas` in `infra/resources/aca.bicep` for expected load
- [ ] Set up Application Insights monitoring (optional)
- [ ] Test `/health` endpoint for alerting
- [ ] Document prompt library & expected inference time
- [ ] Plan for model updates (update Dockerfile, redeploy)
- [ ] Set up Azure Storage for permanent image storage (optional)
- [ ] Enable private endpoints if using corporate network
- [ ] Implement API key authentication (add to app.py if needed)

## Advanced: Multi-Model Setup

To serve multiple models (SDXL, Stable Diffusion 2, etc.):

1. Extend `app.py` to accept `model_name` parameter
2. Modify `generate_with_retry()` to switch pipelines
3. Increase container memory to 32Gi
4. Scale maxReplicas to 3-5 for concurrency

## Advanced: Persistent Image Storage

Store generated images in Azure Blob Storage:

1. Add Azure Storage Blob SDK to `requirements.txt`
2. Modify `app.py` to upload images after generation
3. Return signed URLs in response
4. Add storage account to Bicep template

Example flow:
```python
# After image.save(output_path):
blob_client.upload_blob(output_path)
url = blob_client.url
return {"url": url, ...}
```

## License

This scaffolding is provided as-is. SDXL model is under CreativeML Open RAIL++-M license.

## Support

- **Model docs:** https://huggingface.co/stabilityai/stable-diffusion-xl-base-1.0
- **Diffusers docs:** https://huggingface.co/docs/diffusers
- **Azure Container Apps:** https://aka.ms/aca-docs
- **azd CLI reference:** https://aka.ms/azd-cli

---

**Built for production. Deploy with confidence.**
