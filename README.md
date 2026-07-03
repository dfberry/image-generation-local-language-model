# SDXL Image Generation API

Production-ready containerized Stable Diffusion XL image generation service. Deploy locally with Docker or elastically to Azure Container Apps using `azd`.

**Key Features:**
- Flask REST API exposing `/generate` POST endpoint
- Batch image generation from JSON configs
- Automatic OOM handling with step reduction retry
- GPU-accelerated (NVIDIA CUDA, Apple Silicon MPS) with CPU fallback
- Zero-cost idle scaling (scales to zero when not generating)
- ~5-10 minute first-run model download, then ~30-60 seconds per 1024×1024 image
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

```bash
cd /path/to/image-generation-ai
pip install -r requirements.txt
```

**Note:** First `pip install` downloads Hugging Face models (~7GB). On subsequent runs, models cache locally.

### 2. Run Flask Server Locally

```bash
python app.py
```

Output:
```
WARNING in flask.app: This is a development server. Do not use it in production.
 * Running on http://0.0.0.0:8000
```

### 3. Test the API

**Health check:**
```bash
curl http://localhost:8000/health
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

## Containerize Locally

### 1. Build Docker Image

```bash
docker build -t sdxl-api:latest .
```

**Note:** Build takes ~2 min on modern hardware (base image, deps, no model yet).

### 2. Run Container Locally

```bash
docker run --gpus all -p 8000:8000 \
  --mount type=bind,source=$(pwd)/outputs,target=/app/outputs \
  sdxl-api:latest
```

**Flags:**
- `--gpus all` — pass all GPUs to container (requires NVIDIA Docker runtime)
- `-p 8000:8000` — expose port 8000
- `--mount` — persist generated images to host `./outputs/`

### 3. Test Container

```bash
curl http://localhost:8000/health
curl -X POST http://localhost:8000/generate \
  -H "Content-Type: application/json" \
  -d '{"prompts": [{"prompt": "test image", "seed": 42}]}'
```

**On ARM64 (Apple Silicon, AWS Graviton):**
```bash
# Build multi-platform (may be slower)
docker buildx build --platform linux/amd64,linux/arm64 -t sdxl-api:latest .

# Or build native ARM64
docker build -t sdxl-api:latest .
```

## Deploy to Azure Container Apps

### 1. Authenticate & Initialize

```bash
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

```bash
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

**First run takes ~5-10 min:**
- Infrastructure provisioning (~3-5 min)
- Container image build & push (~1-2 min)
- Model download on first inference (~2-5 min)

### 3. Get Your API URL

```bash
azd env get-values | grep CONTAINER_APP_FQDN
```

Or:
```bash
az containerapp show --name sdxl-generation-api --resource-group $RESOURCE_GROUP_NAME \
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

```bash
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

**Get recent logs:**
```bash
az containerapp logs show --name sdxl-generation-api --resource-group $RESOURCE_GROUP_NAME \
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

```bash
azd down
```

**This deletes:**
- Container App and environment
- Container Registry
- Resource group (if you created it)
- All associated costs stop immediately

### Remove Local Images

```bash
docker rmi sdxl-api:latest
rm -rf outputs/  # optional
```

## Troubleshooting

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

### Model Download Fails

**Symptom:** First request hangs or times out.

**Cause:** 
- Container can't reach Hugging Face (network blocked)
- Insufficient storage for 7GB model cache

**Fix:**
```bash
# Pre-download model before deploying
docker build -t sdxl-api:latest .
docker run --rm sdxl-api:latest python -c \
  "from diffusers import DiffusionPipeline; \
   DiffusionPipeline.from_pretrained('stabilityai/stable-diffusion-xl-base-1.0')"

# Or increase container ephemeralStorage in aca.bicep:
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

# If device is "cpu", ensure --gpus passed to docker:
docker run --gpus all -p 8000:8000 sdxl-api:latest

# On Azure, GPU preemption is rare, but try scaling:
# Edit infra/resources/aca.bicep, increase maxReplicas
```

### Container Won't Start

**Symptom:** Container App shows "Failed" status.

**Causes:**
- Port 8000 not exposed
- Invalid environment variable
- Missing Python dependencies

**Fix:**
```bash
# Check logs
az containerapp logs show --name sdxl-generation-api --resource-group $RESOURCE_GROUP_NAME

# Rebuild locally and test
docker build -t sdxl-api:latest .
docker run -p 8000:8000 sdxl-api:latest

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
