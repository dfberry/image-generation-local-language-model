#!/usr/bin/env python3
"""
Stable Diffusion XL image generation script.
Uses SDXL Base 1.0 with optional refiner for high-quality output.

Default model: stabilityai/stable-diffusion-xl-base-1.0
Configurable via environment variables:
  SDXL_BASE_MODEL     — HF repo id or local path for the base model
  SDXL_REFINER_MODEL  — HF repo id or local path for the refiner
  SDXL_MODEL_REVISION — optional git revision / branch / tag
License: CreativeML Open RAIL++-M
"""

import argparse
import gc
import json
import os
import sys
from datetime import datetime
from types import SimpleNamespace

# ---------------------------------------------------------------------------
# Azure Files (SMB) does not support POSIX flock(), which huggingface_hub's
# default FileLock uses to guard concurrent model downloads. When the HF cache
# lives on an SMB-mounted share (e.g. /root/.cache/huggingface backed by Azure
# Files in Container Apps), acquiring that lock raises
# "PermissionError: [Errno 13] Permission denied" and the download aborts.
# SoftFileLock uses a plain create-a-file lock (no flock syscall), which works
# on SMB. Patch filelock BEFORE importing diffusers/huggingface_hub so their
# `from filelock import FileLock` binds to the soft implementation. This is a
# no-op behavioural change on local filesystems.
# ---------------------------------------------------------------------------
import filelock as _filelock
if hasattr(_filelock, "SoftFileLock"):
    _filelock.FileLock = _filelock.SoftFileLock

import torch
from diffusers import DiffusionPipeline

DEFAULT_BASE_MODEL = "stabilityai/stable-diffusion-xl-base-1.0"
BASE_MODEL = os.environ.get("SDXL_BASE_MODEL", DEFAULT_BASE_MODEL)
REFINER_MODEL = os.environ.get(
    "SDXL_REFINER_MODEL", "stabilityai/stable-diffusion-xl-refiner-1.0"
)
MODEL_REVISION = os.environ.get("SDXL_MODEL_REVISION") or None
BASE_MODEL_LOCAL_DIR_NAME = "sdxl-base-1.0"


class OOMError(RuntimeError):
    """Raised when GPU/MPS runs out of memory during generation."""
    pass


class GenerationCancelled(Exception):
    """Raised when an in-flight generation is cancelled cooperatively."""
    pass


def parse_args():
    parser = argparse.ArgumentParser(
        description="Generate images with Stable Diffusion XL",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--prompt", help="Text prompt for image generation")
    group.add_argument("--batch-file", dest="batch_file", metavar="PATH",
                       help="JSON file with list of prompt dicts for batch generation")
    parser.add_argument("--output", default=None, help="Output file path")
    parser.add_argument("--steps", type=int, default=40, help="Number of inference steps")
    parser.add_argument("--guidance", type=float, default=7.5, help="Guidance scale (CFG)")
    parser.add_argument("--width", type=int, default=1024, help="Image width in pixels")
    parser.add_argument("--height", type=int, default=1024, help="Image height in pixels")
    parser.add_argument("--seed", type=int, default=None, help="Random seed for reproducibility")
    parser.add_argument("--negative-prompt", dest="negative_prompt", default=None,
                        help="Negative prompt (things to avoid in the image)")
    parser.add_argument("--refine", action="store_true", help="Use base + refiner pipeline (higher quality)")
    parser.add_argument("--cpu", action="store_true", help="Force CPU mode (slow, no GPU required)")
    return parser.parse_args()


def get_device(force_cpu: bool = False, verbose: bool = True) -> str:
    """Detect best available device."""
    if force_cpu:
        return "cpu"
    if torch.cuda.is_available():
        if verbose:
            print("✅ CUDA GPU detected")
        return "cuda"
    if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
        if verbose:
            print("✅ Apple Silicon (MPS) detected")
        return "mps"
    if verbose:
        print("⚠️  No GPU detected — falling back to CPU (slow)")
    return "cpu"


def get_dtype(device: str):
    """Float16 on GPU, float32 on CPU."""
    return torch.float16 if device in ("cuda", "mps") else torch.float32


def get_base_model_local_dir() -> str:
    override = os.environ.get("SDXL_BASE_MODEL_DIR")
    if override:
        return os.path.abspath(os.path.expandvars(os.path.expanduser(override)))
    hf_home = os.environ.get("HF_HOME") or os.path.expanduser("~/.cache/huggingface")
    return os.path.join(hf_home, "models", BASE_MODEL_LOCAL_DIR_NAME)


def _is_populated_dir(path: str) -> bool:
    if not os.path.isdir(path):
        return False
    for root, dirs, files in os.walk(path):
        dirs[:] = [name for name in dirs if name != ".cache"]
        if any(name for name in files if not os.path.islink(os.path.join(root, name))):
            return True
    return False


def get_base_model_source() -> str:
    if os.environ.get("SDXL_BASE_MODEL"):
        return BASE_MODEL
    local_dir = get_base_model_local_dir()
    return local_dir if _is_populated_dir(local_dir) else BASE_MODEL


def load_base(device: str) -> DiffusionPipeline:
    """Load SDXL base model."""
    model_source = get_base_model_source()
    print(f"📥 Loading SDXL base model ({model_source}) (first run downloads model weights)...")
    dtype = get_dtype(device)
    # The flat prewarm dir intentionally contains fp32 safetensors only; asking
    # for an fp16 variant there would make diffusers look for files we skipped.
    variant = "fp16" if device in ("cuda", "mps") and model_source == BASE_MODEL else None
    pipe = DiffusionPipeline.from_pretrained(
        model_source,
        torch_dtype=dtype,
        use_safetensors=True,
        revision=MODEL_REVISION,
        variant=variant,
    )
    if device == "mps":
        # MPS benefits from model CPU offload
        pipe.enable_model_cpu_offload()
    elif device == "cpu":
        # Pure CPU: offload requires an accelerator, so keep the pipeline
        # resident. VAE tiling causes SDXL tile-seam/rainbow-band artifacts
        # at <=1024px and is only needed for 2K+; attention slicing alone
        # keeps peak RAM in check.
        pipe.to("cpu")
        pipe.enable_attention_slicing()
    else:
        pipe.to(device)

    # torch.compile gives ~20-30% speedup on CUDA with torch >= 2.0
    if device == "cuda" and hasattr(torch, "compile"):
        print("⚡ Compiling UNet with torch.compile (one-time, ~30s)...")
        pipe.unet = torch.compile(pipe.unet, mode="reduce-overhead", fullgraph=True)

    return pipe


def load_refiner(text_encoder_2, vae, device: str) -> DiffusionPipeline:
    """Load SDXL refiner, sharing text encoder and VAE from base."""
    print(f"📥 Loading SDXL refiner model ({REFINER_MODEL})...")
    dtype = get_dtype(device)
    refiner = DiffusionPipeline.from_pretrained(
        REFINER_MODEL,
        # Share components with base to save VRAM
        text_encoder_2=text_encoder_2,
        vae=vae,
        torch_dtype=dtype,
        use_safetensors=True,
        revision=MODEL_REVISION,
        variant="fp16" if device in ("cuda", "mps") else None,
    )
    if device == "mps":
        refiner.enable_model_cpu_offload()
    elif device == "cpu":
        # VAE tiling causes SDXL tile-seam/rainbow-band artifacts at <=1024px
        # and is only needed for 2K+; attention slicing alone keeps peak RAM
        # in check.
        refiner.to("cpu")
        refiner.enable_attention_slicing()
    else:
        refiner.to(device)
    return refiner


def generate(args, progress_callback=None, cancel_check=None) -> str:
    """Run image generation and save to output path."""
    device = get_device(args.cpu)
    negative_prompt = getattr(args, "negative_prompt", None)

    # Fix 3: Pre-flight flush — reclaim any GPU memory from a prior generate()
    # call before loading new pipelines. Reduces OOM risk in back-to-back runs.
    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
    if torch.backends.mps.is_available():
        torch.mps.empty_cache()

    # Set up generator for reproducible output
    generator = None
    if args.seed is not None:
        # Fix C: cpu_offload routes layers to CPU for "cpu"/"mps" devices,
        # so bind the generator to CPU to avoid a device mismatch.
        generator_device = "cpu" if device in ("cpu", "mps") else device
        generator = torch.Generator(device=generator_device).manual_seed(args.seed)
        print(f"🌱 Seed: {args.seed}")

    # Resolve output path
    output_path = args.output
    if output_path is None:
        os.makedirs("outputs", exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = f"outputs/image_{timestamp}.png"

    # Base+refiner split: 80% of steps on base, 20% on refiner
    high_noise_frac = 0.8
    completed_steps = 0

    def _make_step_callback(phase: str):
        def _cb(pipe, step_index, timestep, callback_kwargs):
            nonlocal completed_steps
            completed_steps += 1
            reported = min(completed_steps, args.steps)
            if progress_callback is not None:
                progress_callback(reported, args.steps, phase)
            if cancel_check is not None and cancel_check():
                raise GenerationCancelled("cancelled by user")
            return callback_kwargs
        return _cb

    base = refiner = latents = text_encoder_2 = vae = image = None
    try:
        if args.refine:
            print(f"🎨 Running base + refiner pipeline ({args.steps} steps total)...")
            base = load_base(device)

            # Stage 1: base model produces latents
            latents = base(
                prompt=args.prompt,
                negative_prompt=negative_prompt,
                num_inference_steps=args.steps,
                guidance_scale=args.guidance,
                width=args.width,
                height=args.height,
                denoising_end=high_noise_frac,
                output_type="latent",
                generator=generator,
                callback_on_step_end=_make_step_callback("base"),
                callback_on_step_end_tensor_inputs=["latents"],
            ).images

            # Extract shared components before freeing base from GPU
            text_encoder_2 = base.text_encoder_2
            vae = base.vae

            # Fix 1: Move latents to CPU before the cache flush window so that
            # the GPU-resident tensor doesn't pin VRAM while empty_cache() runs.
            if device in ("cuda", "mps"):
                latents = latents.cpu()

            del base
            base = None
            if device == "mps":
                torch.mps.empty_cache()
            if device == "cuda":
                torch.cuda.empty_cache()
            gc.collect()

            refiner = load_refiner(text_encoder_2, vae, device)
            image = refiner(
                prompt=args.prompt,
                negative_prompt=negative_prompt,
                num_inference_steps=args.steps,
                guidance_scale=args.guidance,
                denoising_start=high_noise_frac,
                # Move latents back to device for refiner inference.
                image=latents.to(device) if device in ("cuda", "mps") else latents,
                generator=generator,
                callback_on_step_end=_make_step_callback("refine"),
                callback_on_step_end_tensor_inputs=["latents"],
            ).images[0]
        else:
            print(f"🎨 Running base model ({args.steps} steps)...")
            base = load_base(device)
            image = base(
                prompt=args.prompt,
                negative_prompt=negative_prompt,
                num_inference_steps=args.steps,
                guidance_scale=args.guidance,
                width=args.width,
                height=args.height,
                generator=generator,
                callback_on_step_end=_make_step_callback("base"),
                callback_on_step_end_tensor_inputs=["latents"],
            ).images[0]

        if image is not None:
            image.save(output_path)
            print(f"✅ Saved: {output_path}")
    except Exception as exc:
        _is_cuda_oom = (
            hasattr(torch.cuda, "OutOfMemoryError")
            and isinstance(exc, torch.cuda.OutOfMemoryError)
        )
        _is_mps_oom = isinstance(exc, RuntimeError) and "out of memory" in str(exc).lower()
        if _is_cuda_oom or _is_mps_oom:
            raise OOMError(
                "Out of GPU memory. Reduce steps with --steps or switch to CPU with --cpu."
            ) from exc
        raise
    finally:
        # Unconditional cleanup — runs on success, OOM, interrupt, or any exception.
        # base may already be None (freed mid-refine path) but del is safe on None.
        del base, refiner, latents, text_encoder_2, vae
        image = None
        gc.collect()
        torch.cuda.empty_cache()
        if torch.backends.mps.is_available():
            torch.mps.empty_cache()
        # Fix 2: torch.compile (used on CUDA in load_base) populates a process-global
        # dynamo cache that survives del base. Reset it to prevent accumulation across
        # repeated generate() calls. If torch.compile is added for other devices later,
        # broaden this guard accordingly.
        if device == "cuda" and hasattr(torch, "_dynamo"):
            torch._dynamo.reset()

    return output_path


def run_batch(
    prompts: list[dict],
    steps: int = 40,
    guidance: float = 7.5,
    width: int = 1024,
    height: int = 1024,
    refine: bool = False,
    cpu: bool = False,
    negative_prompt: str | None = None,
    progress_callback=None,
    cancel_check=None,
    item_callback=None,
) -> list[dict]:
    """Generate a batch using the Flask API parameter shape."""
    results = []
    for i, item in enumerate(prompts):
        if item_callback is not None:
            item_callback(i)
        args = SimpleNamespace(
            prompt=item["prompt"],
            negative_prompt=item.get("negative_prompt", negative_prompt),
            output=item.get("output"),
            seed=item.get("seed"),
            steps=steps,
            guidance=guidance,
            width=width,
            height=height,
            refine=refine,
            cpu=cpu,
        )
        try:
            output_path = generate_with_retry(
                args,
                progress_callback=progress_callback,
                cancel_check=cancel_check,
            )
            results.append({
                "prompt": item["prompt"],
                "output": output_path,
                "status": "ok",
                "error": None,
            })
        except GenerationCancelled:
            raise
        except Exception as exc:
            results.append({
                "prompt": item["prompt"],
                "output": item.get("output"),
                "status": "error",
                "error": str(exc),
            })

    return results


def batch_generate(
    prompts: list[dict],
    device: str = "mps",
    negative_prompt: str | None = None,
) -> list[dict]:
    """
    Generate images for a list of prompt dicts, flushing GPU memory between items.

    Each input dict: {"prompt": str, "output": str, "seed": int (optional), "negative_prompt": str (optional)}
    Returns list of {"prompt": str, "output": str, "status": "ok"|"error", "error": str|None}
    """
    results = []
    for i, item in enumerate(prompts):
        args = SimpleNamespace(
            prompt=item["prompt"],
            negative_prompt=item.get("negative_prompt", negative_prompt),
            output=item["output"],
            seed=item.get("seed"),
            steps=40,
            guidance=7.5,
            width=1024,
            height=1024,
            refine=False,
            cpu=(device == "cpu"),
        )
        try:
            output_path = generate(args)
            results.append({
                "prompt": item["prompt"],
                "output": output_path,
                "status": "ok",
                "error": None,
            })
        except Exception as exc:
            results.append({
                "prompt": item["prompt"],
                "output": item["output"],
                "status": "error",
                "error": str(exc),
            })

        # Flush GPU memory between items (not needed after the last item)
        if i < len(prompts) - 1:
            gc.collect()
            torch.cuda.empty_cache()
            if torch.backends.mps.is_available():
                torch.mps.empty_cache()

    return results


def generate_with_retry(args, max_retries: int = 2, progress_callback=None, cancel_check=None) -> str:
    """
    Wraps generate(args) with OOM retry logic.
    - On OOMError: halves args.steps (floor at 1), prints warning, retries
    - Retries up to max_retries times (so up to max_retries+1 total calls)
    - If all retries exhausted: raises OOMError with message mentioning final steps count
    - Non-OOM exceptions: re-raised immediately, no retry
    """
    for attempt in range(max_retries + 1):
        try:
            return generate(
                args,
                progress_callback=progress_callback,
                cancel_check=cancel_check,
            )
        except GenerationCancelled:
            raise
        except OOMError:
            if attempt == max_retries:
                raise OOMError(
                    f"Out of GPU memory after {max_retries} retries. Last attempt used {args.steps} steps."
                )
            args.steps = max(1, args.steps // 2)
            print(f"OOM: retrying with {args.steps} steps")


def main():
    args = parse_args()
    if hasattr(args, 'batch_file') and args.batch_file:
        try:
            with open(args.batch_file) as f:
                prompts = json.load(f)
        except FileNotFoundError:
            print(f"Error: batch file not found: {args.batch_file}", file=sys.stderr)
            sys.exit(1)
        except json.JSONDecodeError as e:
            print(f"Error: invalid JSON in batch file: {e}", file=sys.stderr)
            sys.exit(1)
        device = "cpu" if args.cpu else get_device(False)
        results = batch_generate(prompts, device=device, negative_prompt=args.negative_prompt)
        for r in results:
            status = r['status']
            print(f"[{status}] {r['prompt'][:50]} → {r.get('output', r.get('error', ''))}")
    else:
        generate_with_retry(args)


if __name__ == "__main__":
    main()
