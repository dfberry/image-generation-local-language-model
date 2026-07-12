"""Azure ML scoring entry point for SDXL image generation on a GPU endpoint."""

import base64
import gc
import io
import json
import os
from typing import Any

import torch
from diffusers import DiffusionPipeline

MODEL_NAME = os.environ.get(
    "SDXL_MODEL_NAME", "stabilityai/stable-diffusion-xl-base-1.0"
)
MODEL_REVISION = os.environ.get("SDXL_MODEL_REVISION") or None

pipe: DiffusionPipeline | None = None


def init() -> None:
    """Load SDXL base once per worker in CUDA fp16."""
    global pipe

    if not torch.cuda.is_available():
        raise RuntimeError(
            "CUDA GPU is required for this managed online endpoint. "
            "Use a GPU SKU such as Standard_NC6s_v3 or Standard_NC24ads_A100_v4."
        )

    pipe = DiffusionPipeline.from_pretrained(
        MODEL_NAME,
        torch_dtype=torch.float16,
        use_safetensors=True,
        revision=MODEL_REVISION,
        variant="fp16",
    )
    pipe.to("cuda")
    pipe.enable_attention_slicing()
    try:
        pipe.enable_vae_slicing()
        pipe.enable_vae_tiling()
    except Exception:
        pass


def _payload(data: Any) -> dict[str, Any]:
    if isinstance(data, str):
        data = json.loads(data)
    if isinstance(data, dict) and "input_data" in data:
        data = data["input_data"]
    if not isinstance(data, dict):
        raise ValueError("Request body must be a JSON object.")
    return data


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    value = default if value is None else int(value)
    return max(minimum, min(maximum, value))


def _bounded_float(value: Any, default: float, minimum: float, maximum: float) -> float:
    value = default if value is None else float(value)
    return max(minimum, min(maximum, value))


def run(data: Any) -> dict[str, Any]:
    """Generate one PNG and return it as base64."""
    if pipe is None:
        raise RuntimeError("Model pipeline was not initialized.")

    request = _payload(data)
    prompt = request.get("prompt")
    if not prompt or not isinstance(prompt, str):
        raise ValueError("Request JSON must include a non-empty string field: prompt.")

    steps = _bounded_int(request.get("steps"), 30, 1, 60)
    guidance = _bounded_float(request.get("guidance"), 7.5, 0.0, 20.0)
    width = _bounded_int(request.get("width"), 1024, 512, 1024)
    height = _bounded_int(request.get("height"), 1024, 512, 1024)
    negative_prompt = request.get("negative_prompt")
    seed = request.get("seed")
    generator = None
    if seed is not None:
        generator = torch.Generator(device="cuda").manual_seed(int(seed))

    gc.collect()
    torch.cuda.empty_cache()

    image = pipe(
        prompt=prompt,
        negative_prompt=negative_prompt,
        num_inference_steps=steps,
        guidance_scale=guidance,
        width=width,
        height=height,
        generator=generator,
    ).images[0]

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    image_base64 = base64.b64encode(buffer.getvalue()).decode("ascii")
    return {
        "status": "ok",
        "model": MODEL_NAME,
        "revision": MODEL_REVISION or "default",
        "image_base64": image_base64,
    }
