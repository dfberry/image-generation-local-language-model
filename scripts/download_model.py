"""
Optional prewarm helper: pre-populate the HF model cache before a run so the
first generation request doesn't have to wait for a ~7 GB download.

This script is NOT required — the app downloads models on demand into the HF
cache directory on first use.  Run it ahead of time (e.g., locally before the
first CLI run, or as a one-shot init container) if you want instant startup.

Cache directory behaviour:
  - When HF_HOME is set (e.g. in containers): downloads to $HF_HOME/hub — same
    path the app reads, so the prewarm is used immediately.
  - When HF_HOME is unset (local host): no cache_dir override is passed;
    snapshot_download uses the huggingface_hub library default
    (~/.cache/huggingface/hub), which is exactly where DiffusionPipeline reads.

Mount a persistent volume at $HF_HOME so downloads survive container restarts.

Environment variables:
  SDXL_BASE_MODEL     — HF repo id for the base model
  SDXL_REFINER_MODEL  — HF repo id for the refiner model
  SDXL_MODEL_REVISION — optional git revision / branch / tag (default: latest)
  HUGGING_FACE_TOKEN  — optional Hugging Face token (gated models); falls back to HF_TOKEN
  BAKE_REFINER        — set to "true" to also prewarm the refiner (default: false)
"""

import os
import sys

# Azure Files (SMB) lacks POSIX flock(); huggingface_hub's default FileLock
# then raises "PermissionError: [Errno 13] Permission denied" when the HF cache
# is on an SMB share. SoftFileLock (create-a-file lock) works on SMB. Patch
# before importing huggingface_hub. No-op behaviour change on local disks.
import filelock as _filelock
if hasattr(_filelock, "SoftFileLock"):
    _filelock.FileLock = _filelock.SoftFileLock

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

# When HF_HOME is set (containers), resolve $HF_HOME/hub so prewarm and app read
# the same path.  When unset (local host), pass no cache_dir — snapshot_download
# then uses the huggingface_hub library default (~/.cache/huggingface/hub), which
# is exactly where DiffusionPipeline.from_pretrained reads on the same host.
_hf_home = os.environ.get("HF_HOME")
CACHE_DIR = os.path.join(_hf_home, "hub") if _hf_home else None

kwargs = {}
if MODEL_REVISION:
    kwargs["revision"] = MODEL_REVISION
if HF_TOKEN:
    kwargs["token"] = HF_TOKEN


def download(repo_id: str) -> None:
    print(f"⬇️  Downloading {repo_id}" + (f" @ {MODEL_REVISION}" if MODEL_REVISION else "") + " …")
    kw = dict(kwargs)
    if CACHE_DIR is not None:
        kw["cache_dir"] = CACHE_DIR
    snapshot_download(repo_id=repo_id, **kw)
    cache_display = CACHE_DIR or "default HF cache (~/.cache/huggingface/hub)"
    print(f"✅  {repo_id} cached to {cache_display}")


download(BASE_MODEL)

if BAKE_REFINER:
    download(REFINER_MODEL)
else:
    print(f"ℹ️  BAKE_REFINER=false — skipping refiner ({REFINER_MODEL}). Pass --build-arg BAKE_REFINER=true to include it.")

print("🎉  Model bake complete.")
sys.exit(0)
