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
# ---------------------------------------------------------------------------
# Built-in defaults for all six global render settings.
# Used in parse_args() resolution AND as run_batch() kwarg defaults so there
# is exactly one source of truth — no duplicated magic numbers.
# ---------------------------------------------------------------------------
DEFAULTS = {
    "steps": 40,
    "guidance": 7.5,
    "width": 1024,
    "height": 1024,
    "refine": False,
    "cpu": False,
}

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
    # Defaults are None so main() can detect whether the flag was explicitly passed.
    # None is resolved to the built-in default (DEFAULTS dict) before use.
    parser.add_argument("--steps", type=int, default=None, help="Number of inference steps")
    parser.add_argument("--guidance", type=float, default=None, help="Guidance scale (CFG)")
    parser.add_argument("--width", type=int, default=None, help="Image width in pixels")
    parser.add_argument("--height", type=int, default=None, help="Image height in pixels")
    parser.add_argument("--seed", type=int, default=None, help="Random seed for reproducibility")
    parser.add_argument("--negative-prompt", dest="negative_prompt", default=None,
                        help="Negative prompt (things to avoid in the image)")
    # store_true with default=None: absent → None, present → True.
    # None means "not explicitly passed"; used for CLI-vs-file precedence.
    parser.add_argument("--refine", action="store_true", default=None,
                        help="Use base + refiner pipeline (higher quality)")
    parser.add_argument("--cpu", action="store_true", default=None,
                        help="Force CPU mode (slow, no GPU required)")
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


def generate(args, *, pipeline=None) -> str:
    """Run image generation and save to output path.

    pipeline: optional pre-loaded DiffusionPipeline (non-refine path only).
      When supplied, it is reused as-is and NOT freed in finally — the caller
      owns its lifecycle and is responsible for cleanup.  When None, the function
      loads and frees its own pipeline (original standalone behaviour).
    """
    device = get_device(args.cpu)
    negative_prompt = getattr(args, 'negative_prompt', None)

    # Fix 3: Pre-flight flush — reclaim any GPU memory from a prior generate()
    # call before loading new pipelines. Reduces OOM risk in back-to-back runs.
    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
    if torch.backends.mps.is_available():
        torch.mps.empty_cache()

    # Set up generator for reproducible output.
    # Re-created every call so per-prompt seeds are always honoured, even when
    # the pipeline is reused across prompts in a batch.
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

    # True when the caller supplied a pre-loaded pipeline for the non-refine path.
    # In that case the finally block must NOT del it — the caller owns its lifecycle.
    # The refine path always loads its own base (it must be freed before loading the
    # refiner to avoid keeping both resident simultaneously), so caller_owns_pipeline
    # is always False on the refine path regardless of the pipeline= argument.
    caller_owns_pipeline = pipeline is not None and not args.refine

    base = refiner = latents = text_encoder_2 = vae = image = None
    try:
        if args.refine:
            # Refine path: per-call base load/free is preserved intentionally.
            # The refiner shares text_encoder_2 and vae with the base pipeline.
            # The base must be freed before the refiner is loaded to avoid keeping
            # both resident simultaneously — doing so risks OOM on low-VRAM GPUs.
            # Making this path truly load-once would require holding both pipelines
            # in VRAM at the same time, which is unsafe on constrained devices.
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
            # Use the caller's pre-loaded pipeline if supplied; otherwise load one.
            base = pipeline if caller_owns_pipeline else load_base(device)
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
        # `del base` is always safe: when base holds the caller's pipeline, del only
        # removes our local reference; the caller's own variable keeps the object alive.
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
        # Skip reset when using a caller-supplied pipeline; run_batch performs a
        # single reset in its own finally after the full batch completes.
        if not caller_owns_pipeline and device == "cuda" and hasattr(torch, "_dynamo"):
            torch._dynamo.reset()

    return output_path


def run_batch(
    prompts: list[dict],
    *,
    steps: int = DEFAULTS["steps"],
    guidance: float = DEFAULTS["guidance"],
    width: int = DEFAULTS["width"],
    height: int = DEFAULTS["height"],
    refine: bool = DEFAULTS["refine"],
    cpu: bool = DEFAULTS["cpu"],
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
    device = get_device(cpu)

    # Load the base pipeline once for the entire batch (non-refine path).
    # CPU memory optimisations (attention_slicing, vae_slicing, vae_tiling) are
    # applied inside load_base and therefore happen exactly once here.
    # The refine path keeps per-item loading: the base must be freed before the
    # refiner is loaded to avoid keeping both resident simultaneously (OOM risk on
    # low-VRAM GPUs).  See the comment inside generate() for full rationale.
    resident_pipeline = load_base(device) if not refine else None

    try:
        for item in prompts:
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
                output_path = generate_with_retry(args, pipeline=resident_pipeline)
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
    finally:
        # Free the resident pipeline after all prompts complete (or on failure
        # mid-batch).  When refine=True, resident_pipeline is None; del None is safe.
        del resident_pipeline
        gc.collect()
        torch.cuda.empty_cache()
        if torch.backends.mps.is_available():
            torch.mps.empty_cache()
        # Fix 2: Reset torch.compile's dynamo cache once for the full batch.
        if device == "cuda" and hasattr(torch, "_dynamo"):
            torch._dynamo.reset()

    return results


def generate_with_retry(args, max_retries: int = 2, *, pipeline=None) -> str:
    """
    Wraps generate(args) with OOM retry logic.
    - On OOMError: halves args.steps (floor at 1), prints warning, retries
    - Retries up to max_retries times (so up to max_retries+1 total calls)
    - If all retries exhausted: raises OOMError with message mentioning final steps count
    - Non-OOM exceptions: re-raised immediately, no retry
    - pipeline: forwarded to generate(); when supplied, retries reuse the same
      loaded pipeline without reloading (caller owns lifecycle).
    """
    for attempt in range(max_retries + 1):
        try:
            return generate(args, pipeline=pipeline)
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
                data = json.load(f)
        except FileNotFoundError:
            print(f"Error: batch file not found: {args.batch_file}", file=sys.stderr)
            sys.exit(1)
        except json.JSONDecodeError as e:
            print(f"Error: invalid JSON in batch file: {e}", file=sys.stderr)
            sys.exit(1)

        if isinstance(data, list):
            # Legacy array form — globals come only from CLI/defaults.
            prompts = data
            file_settings = {}
        elif isinstance(data, dict):
            # New object form — may contain "settings" and must contain "prompts".
            file_settings = data.get("settings") or {}
            prompts = data.get("prompts")
            if not isinstance(prompts, list) or len(prompts) == 0:
                print(
                    "Error: batch file object form requires a non-empty 'prompts' list.",
                    file=sys.stderr,
                )
                sys.exit(1)
        else:
            print(
                "Error: batch file must be a JSON array (legacy) or a JSON object with a 'prompts' key.",
                file=sys.stderr,
            )
            sys.exit(1)

        def _resolve(cli_val, file_val, default):
            """Return first non-None value: CLI flag > file setting > built-in default."""
            if cli_val is not None:
                return cli_val
            if file_val is not None:
                return file_val
            return default

        results = run_batch(
            prompts,
            steps=_resolve(args.steps, file_settings.get("steps"), DEFAULTS["steps"]),
            guidance=_resolve(args.guidance, file_settings.get("guidance"), DEFAULTS["guidance"]),
            width=_resolve(args.width, file_settings.get("width"), DEFAULTS["width"]),
            height=_resolve(args.height, file_settings.get("height"), DEFAULTS["height"]),
            refine=_resolve(args.refine, file_settings.get("refine"), DEFAULTS["refine"]),
            cpu=_resolve(args.cpu, file_settings.get("cpu"), DEFAULTS["cpu"]),
        )
        for r in results:
            status = r['status']
            print(f"[{status}] {r['prompt'][:50]} → {r.get('output') or r.get('error', '')}")
    else:
        # Single-prompt path: resolve any None CLI flags to built-in defaults.
        if args.steps is None:
            args.steps = DEFAULTS["steps"]
        if args.guidance is None:
            args.guidance = DEFAULTS["guidance"]
        if args.width is None:
            args.width = DEFAULTS["width"]
        if args.height is None:
            args.height = DEFAULTS["height"]
        if args.refine is None:
            args.refine = DEFAULTS["refine"]
        if args.cpu is None:
            args.cpu = DEFAULTS["cpu"]
        generate_with_retry(args)


if __name__ == "__main__":
    main()
