# SDXL Image Generation

Containerized Stable Diffusion XL image generation service. Pass a batch JSON file, get PNG images out. Deploy locally with Docker Compose or to Azure Container Apps.

**Key Features:**
- **CLI batch runner** — pass a JSON file of prompts, images land in `./outputs/`
- **Browser UI (`GET /ui`)** — point your browser at `/ui` to check health, download the model, submit prompts, and download generated images — no curl required
- Optional thin HTTP server (`app.py`) for the same pipeline over REST
- **Open CORS** — all endpoints return `Access-Control-Allow-Origin: *` so any browser/origin can call them without auth
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

`azd up` provisions Azure infrastructure (ACR, Storage, ACA Environment, Container App), then builds the Docker image using `Dockerfile.cpu`, pushes it to Azure Container Registry, and updates the Container App to the real image — all in one command. On the very first deploy, the Container App provisions with a harmless placeholder image so ARM succeeds before the ACR is populated; azd then swaps in the real image during the deploy phase.

**Persistent model storage via Azure Files:**

The same pattern that works locally with a named volume works on ACA with an Azure Files share. Mount the share at `/root/.cache/huggingface` (= `HF_HOME`) on every replica. The model downloads once (first cold start) and persists across scale-to-zero and across all replicas.

**Recommended production setup:**

1. **Azure Files NFS share** mounted at `/root/.cache/huggingface` on the Container App. See `/azure/container-apps/storage-mounts`.

2. **Optional: init container** to pre-warm the share before the main container starts. Run `scripts/download_model.py` as an init container — on the very first deploy it downloads ~7 GB; on all subsequent starts it detects weights already present and exits immediately. See `/azure/container-apps/init-containers`.

3. **Set `minReplicas: 0`** (default) to reduce cost — the container stops when idle. First request after idle triggers a cold start: the model must reload from the Azure Files share into memory (~6 min typical on CPU). Set `minReplicas: 1` to keep one warm replica loaded and avoid cold-start delay, at the cost of running the D4 node 24/7.

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

### 2. Download the model (optional pre-fetch)

Pre-download the ~7 GB base model into the HF cache so the first generation run is instant.  
This step is **optional** — if you skip it, step 3 downloads the model automatically on first run (you'll just wait during that run).

```bash
python scripts/download_model.py
```

```powershell
python scripts/download_model.py
```

To also pre-fetch the ~6 GB refiner model, set `BAKE_REFINER=true`:

```bash
BAKE_REFINER=true python scripts/download_model.py
```

```powershell
$env:BAKE_REFINER = "true"; python scripts/download_model.py
```

### 3. Run CLI batch locally

```bash
PYTHONPATH=src python -m image_generation.generate --batch-file batch.json
```

```powershell
$env:PYTHONPATH = "src"
python -m image_generation.generate --batch-file batch.json
```

If you skipped step 2, the first run downloads ~7 GB from Hugging Face into the default HF cache (`~/.cache/huggingface`) on demand. If you completed step 2, the model loads from cache immediately. Output PNGs land in the paths specified in `batch.json`.

#### Settings

There are two tiers of settings. **Per-item** settings live inside `batch.json`; **global** settings can live in a `settings` block in `batch.json` or be passed as CLI flags.

**Per-item settings** — keys in each prompt object:

| Key | Meaning | Required | Default |
|---|---|---|---|
| `prompt` | What to generate | ✅ yes | — |
| `negative_prompt` | What to avoid | optional | none |
| `seed` | Fixed seed for reproducibility (same seed + settings → same image) | optional | random |
| `output` | Output PNG path | optional | auto-named |

**Global render settings** — can live in the `settings` block of the object-form `batch.json`, or be overridden at the command line. **Precedence: CLI flag > file `settings` value > built-in default.**

| Setting / Flag | Default | Meaning |
|---|---|---|
| `steps` / `--steps` | 40 | Denoising steps — higher = more detail, slower. 1024×1024 @ 40 steps ≈ 6 min/image on CPU; 768×768 @ 20 steps is a fast smoke test |
| `guidance` / `--guidance` | 7.5 | Classifier-free guidance (CFG) — how strictly the model follows the prompt |
| `width` / `--width` | 1024 | Image width in pixels |
| `height` / `--height` | 1024 | Image height in pixels |
| `refine` / `--refine` | false | Run the SDXL refiner pass (needs the ~6 GB refiner model) |
| `cpu` / `--cpu` | false | Force CPU inference (otherwise auto-detects CUDA/MPS) |

**Batch file formats** — two shapes are supported:

*Object form* (recommended — self-documenting, settings live in the file):

```json
{
  "description": "optional free-text — ignored by the tool",
  "settings": {
    "steps": 40,
    "guidance": 7.5,
    "width": 1024,
    "height": 1024,
    "refine": false,
    "cpu": false
  },
  "prompts": [
    {
      "prompt": "a serene mountain lake at sunrise, photorealistic",
      "negative_prompt": "blur, noise, cartoon",
      "seed": 42,
      "output": "outputs/mountain-lake.png"
    }
  ]
}
```

*Array form* (legacy — still fully supported; globals come from CLI flags or defaults):

```json
[
  {
    "prompt": "a serene mountain lake at sunrise, photorealistic",
    "negative_prompt": "blur, noise, cartoon",
    "seed": 42,
    "output": "outputs/mountain-lake.png"
  }
]
```

CLI flags always win. For example, `--steps 20` overrides `"steps": 40` in the file.

Example with every global setting made explicit via CLI:

```bash
PYTHONPATH=src python -m image_generation.generate --batch-file batch.json \
  --steps 40 --guidance 7.5 --width 1024 --height 1024
```

```powershell
$env:PYTHONPATH = "src"
python -m image_generation.generate --batch-file batch.json --steps 40 --guidance 7.5 --width 1024 --height 1024
```

> ⚠️ The defaults (1024×1024, 40 steps) are heavy on CPU (~6 min/image). For a fast local smoke test, drop to `--width 768 --height 768 --steps 20` or set those values in the `settings` block.

### 4. Optional: Run Flask Server Locally

```console
python app.py
```

Output:
```
WARNING in flask.app: This is a development server. Do not use it in production.
 * Running on http://0.0.0.0:8000
```

### 5. Test the API

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

| Platform | Dockerfile | Works? |
|---|---|---|
| Windows (any — no NVIDIA GPU) | `Dockerfile.cpu` | ✅ Default — use this |
| macOS (Intel or Apple Silicon) | `Dockerfile.cpu` | ✅ Default — use this |
| Linux (no NVIDIA GPU) | `Dockerfile.cpu` | ✅ Default — use this |
| Cloud / Azure Container Apps | `Dockerfile.cpu` | ✅ Used by `azure.yaml` |
| Linux + NVIDIA GPU (native, NVIDIA Container Toolkit installed) | `Dockerfile` | ✅ Faster local generation |
| Windows + NVIDIA GPU via WSL2 GPU passthrough (NVIDIA Container Toolkit inside WSL2) | `Dockerfile` | ✅ Verify with `nvidia-smi` first |
| macOS (any) | `Dockerfile` | ❌ No NVIDIA GPU on Mac; Docker Desktop cannot pass through any GPU |

**CPU image (`Dockerfile.cpu`)** is the default for everyone. It is used by `docker-compose.yml`, `azure.yaml`, and the infra deploy. No GPU, no NVIDIA toolkit — it just works.

**GPU image (`Dockerfile`)** is optional and only useful on machines with a real NVIDIA GPU. It is **not** wired into `docker-compose.yml` or `azure.yaml` (a `docker-compose.gpu.yml` does not exist in this repo). The working path for GPU users is direct Docker commands:

```console
docker build -t sdxl-cli:gpu .
docker run --gpus all --rm -v ${PWD}:/data -v hf-cache:/root/.cache/huggingface sdxl-cli:gpu --batch-file /data/batch.json
```

> **Tip:** Run `nvidia-smi` first to confirm your GPU is visible to Docker. If you see `nvidia-container-cli: initialization error: WSL environment detected but no adapters were found`, GPU passthrough is not configured — use `Dockerfile.cpu` instead.

### How docker-compose.yml works

`docker-compose.yml` defines **two services**, both building from `Dockerfile.cpu`:

- **`img-gen`** — the default CLI batch runner. Used with `docker compose run --rm img-gen ...`.
- **`img-gen-server`** — optional HTTP server (`app.py`), gated behind the `server` Compose profile so it never starts accidentally. Activate with `docker compose --profile server up`.

**Key mounts:**

- **`./:/data:rw`** (bind-mount on `img-gen`) — the repo root is visible inside the container as `/data`. Your `batch.json` and `outputs/` are accessible with no copy step; generated images appear on your host the moment they are written.
- **`hf-cache:/root/.cache/huggingface`** (both services) — a named volume at `HF_HOME`. The ~7 GB model downloads here once and is reused on every run.

**Entrypoint** (from `Dockerfile.cpu`):

```text
ENTRYPOINT ["python", "-m", "image_generation.generate"]
CMD ["--help"]
```

Args you pass to `docker compose run --rm img-gen` flow straight to the generator — e.g. `--batch-file /data/batch.json`.

The volume is declared `external: true` with a pinned `name:` so Compose never silently recreates it and discards your cached weights — see **Step 0** below for the one-time creation command.

### 0. Create the model-cache volume (one-time, fresh clone only)

The `hf-cache` volume is declared `external` in `docker-compose.yml` so Compose never
silently recreates it and loses your cached model weights. You must create it once before
your first `docker compose run`:

```console
docker volume create public-dfberry-image-generation-local-language-model_hf-cache
```

After that, Compose reuses it automatically on every subsequent run. If you already ran a
previous version of this project and the volume exists, skip this step — it is already there.

**To wipe the cache and force a fresh model download:**

```console
docker volume rm public-dfberry-image-generation-local-language-model_hf-cache
docker volume create public-dfberry-image-generation-local-language-model_hf-cache
```

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

> The same `POST /generate` endpoint works against the deployed ACA service — no batch files needed. See [Generating images in the cloud (ACA)](#generating-images-in-the-cloud-aca) below.

## Generating images in the cloud (ACA)

The ACA deployment runs the same Flask HTTP server (`app.py`) you can start locally in [§ 5 above](#5-optional-http-server). The critical difference from local CLI usage: **there are no batch files in the cloud**. You send your prompts and settings as a JSON body in a `POST /generate` call — the server does not read files from disk.

### Endpoints

| Endpoint | Method | Purpose |
|---|---|---|
| `GET /` | GET | API info / version |
| `GET /health` | GET | Health check — returns `status` + `device`; does not load the model |
| `POST /generate` | POST | Generate images from a JSON prompt list |

### Get your deployed URL

After `azd up` completes, retrieve the FQDN:

```bash
azd env get-values | grep CONTAINER_APP_FQDN
```

```powershell
azd env get-values | Select-String CONTAINER_APP_FQDN
```

Or query it directly:

```bash
az containerapp show -n sdxl-generation-api -g rg-diberry-image \
  --query properties.configuration.ingress.fqdn -o tsv
```

```powershell
az containerapp show -n sdxl-generation-api -g rg-diberry-image `
  --query properties.configuration.ingress.fqdn -o tsv
```

Your generate endpoint is then `https://<fqdn>/generate`.

### ⚠️ Cold start — set a long client timeout

With `minReplicas=0` (the default), the container stops when idle. The first request after an idle period triggers a cold start: container boot + loading the ~7 GB model from the Azure Files share ≈ **~6 minutes**. Your HTTP client must tolerate this latency.

**Recommended workflow — poll `/health` before sending prompts:**

```bash
FQDN="<your-fqdn>"
until curl -sf "https://$FQDN/health" | grep -q '"status":"healthy"'; do
  echo "waiting for warm container..."; sleep 30
done
curl -X POST "https://$FQDN/generate" \
  -H "Content-Type: application/json" \
  --data-binary "@cloud-request.json"
```

```powershell
$fqdn = "<your-fqdn>"
do {
  Start-Sleep 30
  $h = try { Invoke-RestMethod "https://$fqdn/health" } catch { $null }
} until ($h.status -eq "healthy")
Invoke-RestMethod -Uri "https://$fqdn/generate" -Method Post -ContentType "application/json" -InFile "cloud-request.json"
```

To eliminate cold starts at the cost of 24/7 D4 compute, set `minReplicas=1` — see [Scaling parameters](#scaling-parameters).

### Request body format

The server accepts **two request shapes** — pick whichever is convenient:

- **Flat form** — settings at the JSON root (original format).
- **Object form** — settings nested under a `"settings"` key, with an optional top-level `"description"` (identical to the CLI `batch.json` layout). The server reads both shapes; `description` is harmless and ignored.

**Precedence when both appear:** root-level key → nested `settings` value → built-in default.

**Flat form example:**

```json
{
  "prompts": [
    {
      "prompt": "a tropical sunset with palm trees, magical realism",
      "seed": 42,
      "output": "sunset.png",
      "negative_prompt": "blur, noise"
    }
  ],
  "steps": 40,
  "guidance": 7.5,
  "width": 1024,
  "height": 1024,
  "refine": false
}
```

**Required:** `prompts` — a non-empty array; each item must contain a `"prompt"` string.  
**Optional per prompt:** `seed`, `output`, `negative_prompt`.  
**Top-level defaults if omitted:** `steps=40`, `guidance=7.5`, `width=1024`, `height=1024`, `refine=false`, `cpu=false`.

**Server-enforced validation (returns HTTP 400 on failure):**

| Field | Constraint |
|---|---|
| `steps` | 1 – 150 |
| `guidance` | 0 – 50 |
| `width` | Must be exactly 512, 768, or 1024 |
| `height` | Must be exactly 512, 768, or 1024 |
| `prompts` | Must be present and non-empty |

### Pre-warming the model (optional but recommended)

Azure Container Apps scales to zero when idle. When the app wakes from zero, the very first `/generate` request must download the ~7 GB SDXL model into the Azure Files cache before work can begin — this adds roughly 6 minutes of latency inline to that first request. Pre-warming lets you trigger that download deliberately in the background, poll until it finishes, and then send your real batch to a warm cache. Pre-warming is optional: `/generate` still works without it and will absorb the cold-start download itself.

**Step 1 — kick off the pull (returns 202 immediately):**

bash / curl.exe:

```bash
FQDN="sdxl-generation-api.victoriousforest-12ab.eastus.azurecontainerapps.io"

curl.exe -X POST "https://$FQDN/model/pull" \
  -H "Content-Type: application/json"
```

PowerShell:

```powershell
$fqdn = "sdxl-generation-api.victoriousforest-12ab.eastus.azurecontainerapps.io"

Invoke-RestMethod -Uri "https://$fqdn/model/pull" -Method Post
```

**Step 2 — poll `GET /model/status` until `state` is `ready` (or `error`):**

bash (requires `jq`):

```bash
while true; do
  state=$(curl -s "https://$FQDN/model/status" | jq -r '.state')
  echo "Model state: $state"
  [[ "$state" == "ready" || "$state" == "error" ]] && break
  sleep 15
done
```

PowerShell:

```powershell
do {
  $status = Invoke-RestMethod -Uri "https://$fqdn/model/status"
  Write-Host "Model state: $($status.state)"
  if ($status.state -eq "ready" -or $status.state -eq "error") { break }
  Start-Sleep -Seconds 15
} while ($true)
```

`/model/status` always returns HTTP 200. Possible `state` values: `not_started`, `in_progress`, `ready`, `error`. Once `ready`, proceed to send your batch.

### Sending your batch.json to the cloud

> **Can I send `batch.json` as-is?**
> **Yes — send it verbatim.** The server accepts the object form directly: your `settings` block **is** honored, `description` is ignored, and all `prompts` (including `seed`, `output`, `negative_prompt`) are used. Flattening is no longer required.
>
> If you include a key both at the root and inside `settings`, the root-level value wins.

#### Send batch.json directly (no conversion needed)

Set your FQDN first (same value used in the **Send the request** section):

**bash / curl.exe:**

```bash
FQDN="sdxl-generation-api.victoriousforest-12ab.eastus.azurecontainerapps.io"

curl.exe -X POST "https://$FQDN/generate" \
  -H "Content-Type: application/json" \
  --data-binary "@batch.json"
```

**PowerShell:**

```powershell
$fqdn = "sdxl-generation-api.victoriousforest-12ab.eastus.azurecontainerapps.io"

Invoke-RestMethod -Uri "https://$fqdn/generate" -Method Post `
  -ContentType "application/json" -InFile "batch.json"
```

> **Timing note:** `batch.json`'s default settings (40 steps, 1024×1024, CPU) take roughly 6 minutes per image on a cloud CPU container. If `minReplicas=0`, poll `/health` first and wait for a `200` before sending (cold start can add 2–3 minutes).

After sending, decode and save the returned images. Each `ok` result includes `image_base64` (base64 PNG bytes) and `filename` (basename). See **[Retrieving generated images](#retrieving-generated-images)** below for the bash/jq and PowerShell save loops that iterate all results and write each file by `filename`.

#### Optional: flatten to a minimal flat body

Flattening is only needed if you want the smallest possible request payload. One-liner with `jq`:

```bash
jq '{prompts: .prompts} + .settings' batch.json > cloud-request.json
```

This lifts the `settings` keys to the root, keeps `prompts`, and drops `description`. Then send with `--data-binary "@cloud-request.json"`.

**Original batch.json (object form):**

```json
{
  "description": "my batch",
  "settings": {
    "steps": 40,
    "guidance": 7.5,
    "width": 1024,
    "height": 1024,
    "refine": false
  },
  "prompts": [
    { "prompt": "a serene mountain lake at sunrise", "seed": 42, "output": "mountain.png" }
  ]
}
```

**Equivalent flat form (optional):**

```json
{
  "prompts": [
    { "prompt": "a serene mountain lake at sunrise", "seed": 42, "output": "mountain.png" }
  ],
  "steps": 40,
  "guidance": 7.5,
  "width": 1024,
  "height": 1024,
  "refine": false
}
```

### One-command client script

The scripts below wrap the full workflow — cold-start polling, POST, and saving every returned image — into a single command. Pass the container-app **root URL** (no `/generate` suffix), a local batch file, and an optional output directory.

**bash** (requires `jq`):

```bash
./scripts/generate-cloud.sh https://<app-root> batch.json ./outputs
```

**PowerShell** (pwsh or Windows PowerShell):

```powershell
./scripts/generate-cloud.ps1 -Url https://<app-root> -BatchFile batch.json -OutputDir ./outputs
```

Both scripts:
1. Strip any trailing `/` from the URL, then build `<root>/health` and `<root>/generate`.
2. Poll `GET /health` up to 20 times (15 s apart) waiting for an HTTP 200 — handles ACA scale-to-zero cold starts (~2–6 min).
3. POST the batch file bytes verbatim (`Content-Type: application/json`) with a 30-minute timeout.
4. For each `ok` result, base64-decode `image_base64` and write it to `<output-dir>/<filename>`.
5. Print a `<N> saved, <M> failed` summary and exit non-zero if any image failed.

**Warm-up flag (optional):** Pass `--warmup` (bash) or `-Warmup` (PowerShell) to pre-pull the model and wait until it reports `ready` before sending the batch. Useful right after an ACA scale-from-zero event to avoid the ~6-minute first-download delay hitting `/generate`. The scripts POST to `/model/pull`, then poll `/model/status` up to 60 times (15 s apart, ~15 min max); they exit with an error if the model enters an error state, or proceed anyway if the poll cap is reached.

```bash
./scripts/generate-cloud.sh --warmup https://<app-root> batch.json ./outputs
```

```powershell
./scripts/generate-cloud.ps1 -Url https://<app-root> -BatchFile batch.json -OutputDir ./outputs -Warmup
```

> **bash note:** `jq` must be on `PATH`. Install with `brew install jq` (macOS) or `sudo apt-get install jq` (Ubuntu/Debian).

### Send the request

> ⚠️ **PowerShell users:** In PowerShell, `curl` is an alias for `Invoke-WebRequest` and does **not** accept `-H` or `-d`. Passing those flags fails with `"The term '-H' is not recognized"`. Use `Invoke-RestMethod` or call `curl.exe` explicitly (the real curl binary).

**bash / curl.exe:**

```bash
FQDN="sdxl-generation-api.victoriousforest-12ab.eastus.azurecontainerapps.io"

# Inline JSON
curl.exe -X POST "https://$FQDN/generate" \
  -H "Content-Type: application/json" \
  -d '{
    "prompts": [{"prompt": "a serene mountain lake at sunrise", "seed": 42}],
    "steps": 40, "guidance": 7.5, "width": 1024, "height": 1024
  }'

# Or send from file
curl.exe -X POST "https://$FQDN/generate" \
  -H "Content-Type: application/json" \
  --data-binary "@cloud-request.json"
```

**PowerShell (Invoke-RestMethod):**

```powershell
$fqdn = "sdxl-generation-api.victoriousforest-12ab.eastus.azurecontainerapps.io"

# Option A — build body as a hashtable and serialize
$body = @{
  prompts  = @(@{ prompt = "a serene mountain lake at sunrise"; seed = 42 })
  steps    = 40
  guidance = 7.5
  width    = 1024
  height   = 1024
  refine   = $false
} | ConvertTo-Json -Depth 5
Invoke-RestMethod -Uri "https://$fqdn/generate" -Method Post -ContentType "application/json" -Body $body

# Option B — send from a saved JSON file
Invoke-RestMethod -Uri "https://$fqdn/generate" -Method Post -ContentType "application/json" -InFile "cloud-request.json"
```

### Response body

Each `ok` result now includes the image bytes base64-encoded in `image_base64`. Use `filename` (the basename) to name the file locally. `output` is the full path on the server's ephemeral disk — clients should ignore it and save using `filename` instead.

```json
{
  "status": "success",
  "device": "cpu",
  "results": [
    {
      "prompt": "a serene mountain lake at sunrise",
      "output": "/app/outputs/mountain.png",
      "status": "ok",
      "error": null,
      "filename": "mountain.png",
      "content_type": "image/png",
      "image_base64": "iVBORw0KGgo...=="
    }
  ],
  "timestamp": "2026-07-05T20:51:00.000000+00:00"
}
```

Non-`ok` results have `image_base64: null` and no `filename` or `content_type`.

### Retrieving generated images

> For a ready-made client that handles cold-start polling and saves all images automatically, see **[One-command client script](#one-command-client-script)** above.

`/generate` returns each PNG **inline as base64** in the result's `image_base64` field. The server reads the file bytes and encodes them in the same request before responding, so images are available to callers even though `/app/outputs` is on the container's ephemeral filesystem and is not mounted to Azure Files.

To save the image, decode `image_base64` and write it to a local file. Use the `filename` field for the local filename.

**bash / jq:**

```bash
FQDN="sdxl-generation-api.victoriousforest-12ab.eastus.azurecontainerapps.io"

curl -s -X POST "https://$FQDN/generate" \
  -H "Content-Type: application/json" \
  -d @request.json \
  | jq -r '.results[0].image_base64' | base64 -d > sunset.png
```

To save each result by its `filename`:

```bash
response=$(curl -s -X POST "https://$FQDN/generate" \
  -H "Content-Type: application/json" \
  -d @request.json)

echo "$response" | jq -c '.results[] | select(.status=="ok")' | while read -r item; do
  fname=$(echo "$item" | jq -r '.filename')
  echo "$item" | jq -r '.image_base64' | base64 -d > "$fname"
  echo "Saved $fname"
done
```

**PowerShell:**

```powershell
$fqdn = "sdxl-generation-api.victoriousforest-12ab.eastus.azurecontainerapps.io"

$resp = Invoke-RestMethod -Uri "https://$fqdn/generate" -Method Post `
  -ContentType "application/json" -InFile "request.json"

foreach ($result in $resp.results) {
  if ($result.status -eq "ok") {
    $bytes = [Convert]::FromBase64String($result.image_base64)
    [IO.File]::WriteAllBytes("$PWD\$($result.filename)", $bytes)
    Write-Host "Saved $($result.filename)"
  }
}
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

## Azure cost & scaling choices

SDXL CPU inference needs 4 vCPU and at least 12 GB RAM. That determines every cost choice below.

### Why not the Consumption profile?

ACA's **Consumption** profile is the cheapest option (pay-per-use, no management fee) but it caps at **4 vCPU / 8 GiB RAM**. SDXL base weights are ~7 GB (fp16) plus text encoders, VAE, and activations — CPU inference realistically needs **12 GB+ RAM** even with attention/VAE slicing and tiling. At 8 GiB the container will very likely OOM. Consumption is not a reliable choice for SDXL.

### Dedicated D4 — smallest SKU that fits SDXL

ACA Dedicated workload profiles start at **D4 (4 vCPU / 16 GiB)**. There is no smaller dedicated D-series option. D4 is the minimum reliable SKU for SDXL; the Bicep is already set to D4.

A dedicated profile environment carries an **environment management fee** (~$70/month, approximate, region-dependent) that applies whenever a dedicated workload profile exists in the environment — even when all replicas are at zero. Verify the current figure in the [Azure Pricing Calculator](https://azure.microsoft.com/pricing/calculator/).

### Scaling parameters

`minReplicas` and `maxReplicas` are set via `azd` environment variables — the Bicep reads them through `infra/parameters.json` (`AZURE_MIN_REPLICAS`, default `0`; `AZURE_MAX_REPLICAS`, default `1`). Note: `azd up` does **not** accept a `--parameters` flag. Set the values, then deploy:

```console
azd env set AZURE_MIN_REPLICAS 1
azd env set AZURE_MAX_REPLICAS 1   # default is 1
azd up
```

> `azd env set` alone does not redeploy — you must follow with `azd up` for the change to take effect.

### Cost choices at a glance

| Choice | Effect | Cost direction |
|---|---|---|
| `minReplicas=0` (default) | Scale-to-zero: ~$0 idle compute; ~6-min cold start on first request after idle | ↓ cheapest |
| `minReplicas=1` | Always-warm: no cold start; model stays in memory; D4 node runs 24/7 | ↑ highest compute cost |
| `maxReplicas=1` (default) | Caps fan-out; never pay for more than one D4 node at a time | ↓ recommended |
| `maxReplicas > 1` | Each additional replica spins up a full D4 node | ↑ multiplies node cost |
| Dedicated D4 profile | 4 vCPU / 16 GiB — reliable for SDXL; carries environment management fee (~$70/mo approx) | ↑ fixed overhead |
| Consumption profile | 4 vCPU / 8 GiB — cheaper but very likely OOM for SDXL | ↓ cheaper but unreliable |

> All dollar figures are approximate and region-dependent. Verify current pricing in the [Azure Pricing Calculator](https://azure.microsoft.com/pricing/calculator/).

### Recommended: cheapest-that-works

**Dedicated D4, `minReplicas=0`, `maxReplicas=1`** — these are already the defaults.

- You pay the ~$70/mo environment management fee regardless of activity.
- Compute cost accrues only while a replica is running (active generation).
- Cold start after idle: container starts, model loads from Azure Files share into memory (~6 min typical).
- No surprise fan-out: `maxReplicas=1` means at most one D4 node is ever running.

### Always-warm alternative

Set `minReplicas=1`. The D4 node runs 24/7 — no cold start, model stays in memory. The additional cost is one D4 node running continuously. Use this if cold-start latency is unacceptable.

> **The readiness probe on `/health` does NOT keep the app warm.** It tells ACA when the model has finished loading after a cold start, so traffic is only routed to a ready replica. Warmth is entirely controlled by `minReplicas`.

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
1. Runs `infra/main.bicep` — creates ACR, Storage Account + File Share, ACA Environment, and a Container App (with a placeholder image on first provision so ARM succeeds)
2. Builds the Docker image using `Dockerfile.cpu` (native azd build — no manual `docker build` needed)
3. Pushes the image to Azure Container Registry
4. Updates the Container App to the real ACR image
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

### Recovering from a failed first deploy

If a previous `azd up` failed with `MANIFEST_UNKNOWN: manifest tagged by "latest" is not found`, the resource group may contain a half-created container app. This fix (placeholder image) prevents the error going forward, but if you hit it on a prior run, recover with one of these options:

**Option A — least destructive (recommended):**
```bash
az containerapp delete --name sdxl-generation-api --resource-group rg-diberry-image --yes
azd up
```
Deletes only the failed container app. Preserves the ACA environment, ACR, Storage, and Log Analytics (~1–2 min, no environment fee interruption).

**Option B — full teardown:**
```bash
azd down --purge --force
azd up
```
Destroys and rebuilds everything (~5–8 min). The dedicated D4 environment (~$70/mo) is not billed during teardown, but the Azure Files model cache is lost — first request after rebuild re-downloads ~7 GB from HuggingFace.

**Recommendation:** Option A is cheaper and faster unless you need a completely clean slate.

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
- **minReplicas: 0** (default) — container stops when idle (~$0 idle compute); ~6-min cold start on first request after idle
- **minReplicas: 1** — always-warm replica; model stays in memory; no cold start; node runs 24/7
- **maxReplicas: 1** (recommended) — caps cost at one D4 node; raise only if you need concurrent replicas (each additional replica adds a full D4 node cost)

> **Note:** The readiness probe on `/health` lets ACA know when the model has finished loading — it does **not** keep the app warm or affect whether the container is running. Warmth is controlled by `minReplicas`.

**Cost example (eastus region, approximate — verify via [Azure Pricing Calculator](https://azure.microsoft.com/pricing/calculator/)):**
- Idle (minReplicas=0): ~$0/hr compute (dedicated env management fee still applies — see cost section below)
- Active (D4 node, per hour): check current D4 dedicated pricing in your region
- 100 batches/month at 5 min each with minReplicas=0: ~compute cost for ~8 hr active + management fee

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

### Local development

Running SDXL locally requires a machine with enough RAM (12 GB+) and optionally a GPU. Cost depends entirely on your hardware.

- **Electricity (CPU-only):** ~100–200W × $0.12/kWh ≈ $0.001–$0.002 per 5-min batch (approximate, varies by hardware)
- **Electricity (GPU-assisted, if available):** similar wattage range; exact draw depends on GPU model
- **Hardware amortization:** if you purchased dedicated hardware (e.g., $5,000 GPU workstation over 60 months ≈ $83/month)
- **Total:** $0–100+/month depending on your hardware situation and usage volume

### Azure Container Apps

For Azure cost details, the single source of truth is [**Azure cost & scaling choices**](#azure-cost--scaling-choices) earlier in this document.

Key facts to keep in mind:

- SDXL CPU inference requires **Dedicated D4** (4 vCPU / 16 GiB); the Consumption plan's 8 GiB cap will very likely OOM.
- The Dedicated environment carries a **management fee (~$70/mo, approximate, region-dependent)** that applies even at zero replicas — idle is not free.
- With `minReplicas=0`: compute is ~$0 while idle (only management fee accrues); cold start ~6 min.
- With `minReplicas=1`: the D4 node runs 24/7 — no cold start, but continuous node cost on top of the management fee.
- Do not rely on per-image or per-hour figures from other SKUs (GPU, Consumption) — they do not apply to this CPU/D4 setup.

Verify current Dedicated D4 pricing in the [Azure Pricing Calculator](https://azure.microsoft.com/pricing/calculator/).

### Local vs Azure

| Factor | Local (CPU or GPU) | Azure (Dedicated D4, CPU) |
|--------|-------------------|--------------------------|
| Upfront cost | Hardware purchase | None |
| Idle cost | ~$0 compute (electricity only) | ~$70/mo management fee (always on) |
| Active cost | Electricity (low) | D4 node-hours (see Pricing Calculator) |
| Cold start | Immediate | ~6 min (if `minReplicas=0`) |
| Availability | Limited to one machine | Accessible anywhere; up to `maxReplicas` nodes |
| Scalability | Fixed to local hardware | Adjustable via `minReplicas`/`maxReplicas` |

**Azure makes sense when:**
- You don't own hardware capable of running SDXL (12 GB+ RAM)
- Usage is bursty and infrequent — scale-to-zero keeps idle cost to just the ~$70/mo management fee
- You need cloud-hosted inference without managing a physical machine

**Local makes sense when:**
- You already own hardware that can run SDXL
- Usage is frequent enough that the Azure base cost (~$70/mo+) exceeds local electricity cost
- You need zero cold-start latency and aren't concerned about availability outside your local network

> All Azure figures are approximate and region-dependent. Verify current pricing in the [Azure Pricing Calculator](https://azure.microsoft.com/pricing/calculator/).

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
