"""
Optional prewarm helper: pre-populate the local model directory before a run so the
first generation request doesn't have to wait for a ~7 GB download.

This script is NOT required — the app downloads models on demand into the HF
model directory on first use.  Run it ahead of time (e.g., locally before the
first CLI run, or as a one-shot init container) if you want instant startup.

Cache directory behaviour:
  - Base model downloads to SDXL_BASE_MODEL_DIR when set.
  - Otherwise it downloads to $HF_HOME/models/sdxl-base-1.0, or
    ~/.cache/huggingface/models/sdxl-base-1.0 when HF_HOME is unset.

Mount a persistent volume at $HF_HOME so downloads survive container restarts.

Environment variables:
  SDXL_BASE_MODEL     — HF repo id for the base model
  SDXL_BASE_MODEL_DIR — local directory for the flat base model download
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
BASE_MODEL_LOCAL_DIR_NAME = "sdxl-base-1.0"
REFINER_MODEL_LOCAL_DIR_NAME = "sdxl-refiner-1.0"
MODEL_DOWNLOAD_IGNORE_PATTERNS = [
    "*.bin",
    "*.pt",
    "*.pth",
    "*.ckpt",
    "*.onnx",
    "*.onnx_data",
    "*.msgpack",
    "*.fp16.safetensors",
    "*.fp16.bin",
]

kwargs = {}
if MODEL_REVISION:
    kwargs["revision"] = MODEL_REVISION
if HF_TOKEN:
    kwargs["token"] = HF_TOKEN


def get_hf_cache_root() -> str:
    return os.environ.get("HF_HOME") or os.path.expanduser("~/.cache/huggingface")


def get_base_model_local_dir() -> str:
    override = os.environ.get("SDXL_BASE_MODEL_DIR")
    if override:
        return os.path.abspath(os.path.expandvars(os.path.expanduser(override)))
    return os.path.join(get_hf_cache_root(), "models", BASE_MODEL_LOCAL_DIR_NAME)


def get_refiner_model_local_dir() -> str:
    return os.path.join(get_hf_cache_root(), "models", REFINER_MODEL_LOCAL_DIR_NAME)


def download(repo_id: str, local_dir: str, *, ignore_patterns: list[str] | None = None) -> None:
    print(f"⬇️  Downloading {repo_id}" + (f" @ {MODEL_REVISION}" if MODEL_REVISION else "") + " …")
    kw = dict(kwargs)
    kw["local_dir"] = local_dir
    if ignore_patterns:
        # Store a single flat fp32 safetensors copy; the HF hub cache duplicates
        # blobs under snapshots on SMB-backed Azure Files.
        kw["ignore_patterns"] = ignore_patterns
    snapshot_download(repo_id=repo_id, **kw)
    print(f"✅  {repo_id} cached to {local_dir}")


download(BASE_MODEL, get_base_model_local_dir(), ignore_patterns=MODEL_DOWNLOAD_IGNORE_PATTERNS)

if BAKE_REFINER:
    download(REFINER_MODEL, get_refiner_model_local_dir())
else:
    print(f"ℹ️  BAKE_REFINER=false — skipping refiner ({REFINER_MODEL}). Pass --build-arg BAKE_REFINER=true to include it.")

print("🎉  Model bake complete.")
sys.exit(0)
