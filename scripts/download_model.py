"""
Optional prewarm helper: pre-populate the HF model cache before the container
starts so the first generation request doesn't have to wait for a ~7 GB download.

This script is NOT required — the app downloads models on demand into the HF
cache directory on first use.  Run it ahead of time (e.g., as a one-shot init
container or a manual cache-warm step) if you want instant startup.

The cache directory is controlled by $HF_HOME (default: /root/.cache/huggingface).
Mount a persistent volume at that path so the download survives container restarts.

Environment variables:
  SDXL_BASE_MODEL     — HF repo id for the base model
  SDXL_REFINER_MODEL  — HF repo id for the refiner model
  SDXL_MODEL_REVISION — optional git revision / branch / tag (default: latest)
  HUGGING_FACE_TOKEN  — optional Hugging Face token (gated models); falls back to HF_TOKEN
  BAKE_REFINER        — set to "true" to also prewarm the refiner (default: false)
"""

import os
import sys
from huggingface_hub import snapshot_download

BASE_MODEL = os.environ.get(
    "SDXL_BASE_MODEL", "stabilityai/stable-diffusion-xl-base-1.0"
)
REFINER_MODEL = os.environ.get(
    "SDXL_REFINER_MODEL", "stabilityai/stable-diffusion-xl-refiner-1.0"
)
MODEL_REVISION = os.environ.get("SDXL_MODEL_REVISION") or None
HF_TOKEN = os.environ.get("HUGGING_FACE_TOKEN") or os.environ.get("HF_TOKEN") or None  # HF_TOKEN is the huggingface_hub standard auto-detected fallback
BAKE_REFINER = os.environ.get("BAKE_REFINER", "false").lower() == "true"

# Resolve cache dir the same way huggingface_hub does: $HF_HOME/hub.
# When the BuildKit cache mount sets HF_HOME=/tmp/hf-cache, downloads go there;
# the Dockerfile then copies that tree into the real image layer at the default path.
CACHE_DIR = os.path.join(os.environ.get("HF_HOME", "/root/.cache/huggingface"), "hub")

kwargs = {}
if MODEL_REVISION:
    kwargs["revision"] = MODEL_REVISION
if HF_TOKEN:
    kwargs["token"] = HF_TOKEN


def download(repo_id: str) -> None:
    print(f"⬇️  Downloading {repo_id}" + (f" @ {MODEL_REVISION}" if MODEL_REVISION else "") + " …")
    snapshot_download(repo_id=repo_id, cache_dir=CACHE_DIR, **kwargs)
    print(f"✅  {repo_id} cached to {CACHE_DIR}")


download(BASE_MODEL)

if BAKE_REFINER:
    download(REFINER_MODEL)
else:
    print(f"ℹ️  BAKE_REFINER=false — skipping refiner ({REFINER_MODEL}). Pass --build-arg BAKE_REFINER=true to include it.")

print("🎉  Model bake complete.")
sys.exit(0)
