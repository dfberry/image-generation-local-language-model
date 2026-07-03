# Quick Start Guide

## One-Minute Setup (Local)

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Run Flask server
python app.py

# 3. Test with curl (in another terminal)
curl -X POST http://localhost:8000/generate \
  -H "Content-Type: application/json" \
  -d '{
    "prompts": [{"prompt": "a tropical sunset"}]
  }'
```

Output appears in `outputs/` directory.

---

## Five-Minute Setup (Docker)

```bash
# 1. Build image
docker build -t sdxl-api:latest .

# 2. Run container
docker run --gpus all -p 8000:8000 \
  --mount type=bind,source=$(pwd)/outputs,target=/app/outputs \
  sdxl-api:latest

# 3. Test (in another terminal)
curl http://localhost:8000/health
```

---

## Deploy to Azure (azd)

```bash
# 1. Authenticate
azd auth login

# 2. Initialize
azd init

# 3. Deploy
azd up

# 4. Get URL
azd env get-values | grep CONTAINER_APP_FQDN

# 5. Test
ENDPOINT="https://your-url.azurecontainerapps.io"
curl -X POST $ENDPOINT/generate \
  -H "Content-Type: application/json" \
  -d '{"prompts": [{"prompt": "test"}]}'
```

First deployment: 5–10 minutes (infrastructure + first model download).

Subsequent deployments: 2–3 minutes.

---

## Key Files

| File | Purpose |
|------|---------|
| `app.py` | Flask API wrapper |
| `Dockerfile` | Container definition |
| `requirements.txt` | Python dependencies |
| `src/image_generation/generate.py` | SDXL generation core |
| `infra/main.bicep` | Azure infrastructure template |
| `infra/azure.yaml` | azd configuration |
| `README.md` | Complete documentation |

---

## Common Tasks

### Generate a single image
```bash
curl -X POST http://localhost:8000/generate \
  -H "Content-Type: application/json" \
  -d '{
    "prompts": [
      {"prompt": "your prompt", "seed": 42}
    ]
  }'
```

### Generate multiple images (batch)
```bash
curl -X POST http://localhost:8000/generate \
  -H "Content-Type: application/json" \
  -d '{
    "prompts": [
      {"prompt": "image 1", "seed": 1},
      {"prompt": "image 2", "seed": 2}
    ]
  }'
```

### Check health
```bash
curl http://localhost:8000/health
```

### View logs (Azure)
```bash
az containerapp logs show \
  --name sdxl-generation-api \
  --resource-group $RESOURCE_GROUP_NAME \
  --follow
```

### Scale up (Azure)
Edit `infra/resources/aca.bicep`:
```bicep
maxReplicas: 5  # was 1
```
Then: `azd deploy`

### Cleanup (Azure)
```bash
azd down
```

---

## Customization

### Adjust inference quality
```json
{
  "prompts": [{"prompt": "..."}],
  "steps": 60,        // more = higher quality, slower
  "guidance": 9.0,    // higher = more prompt-aligned
  "refine": true      // use base + refiner (slower, higher quality)
}
```

### Use specific seed (for reproducibility)
```json
{
  "prompts": [
    {"prompt": "...", "seed": 12345}
  ]
}
```

---

## Troubleshooting

**"Out of memory" error?**
- Reduce `steps` (try 20 instead of 40)
- Reduce resolution (try 768×768 instead of 1024×1024)
- Reduce batch size (fewer prompts at once)

**First request takes too long?**
- Container is cold-starting; this is normal (10–15s)
- Model (~7GB) is being downloaded; subsequent requests are faster

**Container won't start?**
```bash
docker run -p 8000:8000 sdxl-api:latest
# Check the output for errors
```

**Azure deployment fails?**
```bash
az containerapp show --name sdxl-generation-api \
  --resource-group $RESOURCE_GROUP_NAME \
  --query 'properties.provisioningState' -o tsv
```

---

## Next Steps

- Read `README.md` for comprehensive documentation
- Explore `app.py` to customize the API
- Check `infra/main.bicep` to modify Azure resources
- See blog post for the full story

---

**Questions?** Check the README or the blog post linked in the repo.
