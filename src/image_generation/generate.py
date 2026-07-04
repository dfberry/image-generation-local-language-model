#!/usr/bin/env python3
"""
Stable Diffusion XL image generation script.
Uses SDXL Base 1.0 with optional refiner for high-quality output.

Default model: stabilityai/stable-diffusion-xl-base-1.0 (SDXL architecture).
Configurable via environment variables:
  SDXL_BASE_MODEL     — HF repo id or local path for the base model (default: SDXL 1.0)
  SDXL_REFINER_MODEL  — HF repo id or local path for the refiner (default: SDXL refiner 1.0)
  SDXL_MODEL_REVISION — optional git revision / branch passed to from_pretrained (default: latest)

NOTE: The refiner shares VAE + text_encoder_2 with the base pipeline.
Swapping models requires a compatible SDXL-architecture refiner.
Non-SDXL architectures (SD 1.5, SD3, Flux …) need code changes.
License: CreativeML Open RAIL++-M
"""

import argparse
import gc
import json
import os
import sys
from datetime import datetime
from types import SimpleNamespace

import torch
from diffusers import DiffusionPipeline

# ---------------------------------------------------------------------------
# Model configuration — override via environment variables before process start.
# The model is NOT baked into the image; it downloads on first run into the HF
# cache (default /root/.cache/huggingface, or $HF_HOME/hub).  Mount a persistent
# volume at that path so the ~7 GB download only happens once.
#
# SDXL architecture assumption: the refiner shares VAE + text_encoder_2 with
# the base pipeline.  Safe to swap in any SDXL-compatible checkpoint.
# A non-SDXL architecture (SD 1.5, SD3, Flux …) breaks the refiner path and
# may conflict with the 1024 px defaults and the 80/20 step-split; those
# paths would need code changes before a cross-architecture swap is safe.
# ---------------------------------------------------------------------------
BASE_MODEL_ID = os.environ.get(
    "SDXL_BASE_MODEL", "stabilityai/stable-diffusion-xl-base-1.0"
)
REFINER_MODEL_ID = os.environ.get(
    "SDXL_REFINER_MODEL", "stabilityai/stable-diffusion-xl-refiner-1.0"
)
# None → from_pretrained uses the default branch (main / latest).
MODEL_REVISION = os.environ.get("SDXL_MODEL_REVISION") or None


class OOMError(RuntimeError):
    """Raised when GPU/MPS runs out of memory during generation."""
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


def get_device(force_cpu: bool) -> str:
    """Detect best available device."""
    if force_cpu:
        return "cpu"
    if torch.cuda.is_available():
        print("✅ CUDA GPU detected")
        return "cuda"
    if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
        print("✅ Apple Silicon (MPS) detected")
        return "mps"
    print("⚠️  No GPU detected — falling back to CPU (slow)")
    return "cpu"


def get_dtype(device: str):
    """Float16 on GPU, float32 on CPU."""
    return torch.float16 if device in ("cuda", "mps") else torch.float32


def load_base(device: str) -> DiffusionPipeline:
    """Load SDXL base model."""
    print(f"📥 Loading base model ({BASE_MODEL_ID}) — first run downloads ~7 GB into the HF cache; subsequent runs reuse the cached weights...")
    dtype = get_dtype(device)
    pipe = DiffusionPipeline.from_pretrained(
        BASE_MODEL_ID,
        torch_dtype=dtype,
        use_safetensors=True,
        revision=MODEL_REVISION,
        # fp16 variant available for CUDA and MPS
        variant="fp16" if device in ("cuda", "mps") else None,
    )
    if device == "mps":
        # MPS is a real accelerator; offload keeps peak VRAM low
        pipe.enable_model_cpu_offload()
    else:
        # cpu: to("cpu") — enable_model_cpu_offload() requires CUDA/MPS and raises on pure CPU
        # cuda: to(device) — standard GPU placement
        pipe.to(device)

    # CPU memory optimisations: slice attention and VAE in chunks to avoid
    # the large intermediate tensors that cause exit-139 OOM on CPU-only
    # systems (including Docker Desktop / WSL2 with the default 8 GB cap).
    # These have no effect on CUDA/MPS paths.
    if device == "cpu":
        pipe.enable_attention_slicing("max")
        pipe.enable_vae_slicing()
        pipe.enable_vae_tiling()

    # torch.compile gives ~20-30% speedup on CUDA with torch >= 2.0
    if device == "cuda" and hasattr(torch, "compile"):
        print("⚡ Compiling UNet with torch.compile (one-time, ~30s)...")
        pipe.unet = torch.compile(pipe.unet, mode="reduce-overhead", fullgraph=True)

    return pipe


def load_refiner(text_encoder_2, vae, device: str) -> DiffusionPipeline:
    """Load SDXL refiner, sharing text encoder and VAE from base."""
    print(f"📥 Loading refiner model ({REFINER_MODEL_ID})...")
    dtype = get_dtype(device)
    refiner = DiffusionPipeline.from_pretrained(
        REFINER_MODEL_ID,
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
    else:
        # cpu: to("cpu") — same guard as load_base (offload requires CUDA/MPS)
        # cuda: to(device)
        refiner.to(device)

    # Same CPU slicing applied to the refiner pipeline (see load_base comment).
    if device == "cpu":
        refiner.enable_attention_slicing("max")
        refiner.enable_vae_slicing()
        refiner.enable_vae_tiling()

    return refiner


def generate(args) -> str:
    """Run image generation and save to output path."""
    device = get_device(args.cpu)
    negative_prompt = getattr(args, 'negative_prompt', None)

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
    *,
    steps: int = 40,
    guidance: float = 7.5,
    width: int = 1024,
    height: int = 1024,
    refine: bool = False,
    cpu: bool = False,
) -> list[dict]:
    """
    Canonical batch engine — used by both the CLI and the Flask server.

    Each input dict (reference format):
        {"prompt": str, "output": str (optional), "negative_prompt": str (optional), "seed": int (optional)}

    Global params (steps, guidance, width, height, refine, cpu) apply to every item.
    Per-item "output", "seed", and "negative_prompt" override per-call.

    Returns list of:
        {"prompt": str, "output": str|None, "status": "ok"|"oom_error"|"error", "error": str|None}
    """
    results = []
    for i, item in enumerate(prompts):
        args = SimpleNamespace(
            prompt=item["prompt"],
            negative_prompt=item.get("negative_prompt"),
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
            output_path = generate_with_retry(args)
            results.append({
                "prompt": item["prompt"],
                "output": output_path,
                "status": "ok",
                "error": None,
            })
        except OOMError as exc:
            results.append({
                "prompt": item["prompt"],
                "output": item.get("output"),
                "status": "oom_error",
                "error": str(exc),
            })
        except Exception as exc:
            results.append({
                "prompt": item["prompt"],
                "output": item.get("output"),
                "status": "error",
                "error": str(exc),
            })

        # Flush GPU memory between items (skip after the last item)
        if i < len(prompts) - 1:
            gc.collect()
            torch.cuda.empty_cache()
            if torch.backends.mps.is_available():
                torch.mps.empty_cache()

    return results


def generate_with_retry(args, max_retries: int = 2) -> str:
    """
    Wraps generate(args) with OOM retry logic.
    - On OOMError: halves args.steps (floor at 1), prints warning, retries
    - Retries up to max_retries times (so up to max_retries+1 total calls)
    - If all retries exhausted: raises OOMError with message mentioning final steps count
    - Non-OOM exceptions: re-raised immediately, no retry
    """
    for attempt in range(max_retries + 1):
        try:
            return generate(args)
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
            with open(args.batch_file, encoding="utf-8") as f:
                prompts = json.load(f)
        except FileNotFoundError:
            print(f"Error: batch file not found: {args.batch_file}", file=sys.stderr)
            sys.exit(1)
        except json.JSONDecodeError as e:
            print(f"Error: invalid JSON in batch file: {e}", file=sys.stderr)
            sys.exit(1)
        results = run_batch(
            prompts,
            steps=args.steps,
            guidance=args.guidance,
            width=args.width,
            height=args.height,
            refine=args.refine,
            cpu=args.cpu,
        )
        for r in results:
            status = r['status']
            print(f"[{status}] {r['prompt'][:50]} → {r.get('output') or r.get('error', '')}")
    else:
        generate_with_retry(args)


if __name__ == "__main__":
    main()
