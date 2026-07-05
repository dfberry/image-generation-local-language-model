#!/usr/bin/env python3
"""
Flask wrapper for SDXL image generation.

Endpoints:
  GET  /             - API info
  GET  /health       - Health check (no model load)
  POST /generate     - Generate images from a batch config (loads model on first call)
  POST /model/pull   - Kick off async model download/warm-up; returns 202 immediately
  GET  /model/status - Poll warm-up state: not_started | in_progress | ready | error
"""

import base64
import gc
import json
import logging
import os
import threading
from typing import Any, Optional

import torch
from flask import Flask, request, jsonify
from datetime import datetime, timezone

from src.image_generation.generate import run_batch, OOMError, get_device, load_base


# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Model warm-up state
# ---------------------------------------------------------------------------
_model_state_lock = threading.Lock()
_model_state: dict = {
    "state": "not_started",   # not_started | in_progress | ready | error
    "message": "Model has not been pulled yet.",
    "started_at": None,
    "finished_at": None,
    "elapsed_seconds": None,
    "error": None,
}


def _pull_worker() -> None:
    """Background thread: loads the base model to warm the on-disk HF cache."""
    device = get_device()
    started = datetime.now(timezone.utc)

    with _model_state_lock:
        _model_state.update({
            "state": "in_progress",
            "message": f"Downloading/loading model on device '{device}'…",
            "started_at": started.isoformat(),
            "finished_at": None,
            "elapsed_seconds": None,
            "error": None,
        })

    logger.info("model/pull: starting load_base() to warm HF disk cache")

    try:
        pipe = load_base(device)

        # Free the in-memory pipeline immediately — the durable win is the
        # on-disk HF cache. run_batch() will reload from that warm cache when
        # /generate is called, avoiding the expensive ~7 GB network download.
        del pipe
        gc.collect()

        finished = datetime.now(timezone.utc)
        elapsed = (finished - started).total_seconds()
        logger.info(f"model/pull: done in {elapsed:.1f}s; in-memory pipeline released")

        with _model_state_lock:
            _model_state.update({
                "state": "ready",
                "message": "Model weights cached on disk. /generate will load from cache.",
                "finished_at": finished.isoformat(),
                "elapsed_seconds": elapsed,
                "error": None,
            })

    except (OOMError, Exception) as exc:
        finished = datetime.now(timezone.utc)
        elapsed = (finished - started).total_seconds()
        msg = str(exc)
        logger.error(f"model/pull: failed after {elapsed:.1f}s — {msg}", exc_info=True)

        with _model_state_lock:
            _model_state.update({
                "state": "error",
                "message": "Model pull failed. See 'error' field.",
                "finished_at": finished.isoformat(),
                "elapsed_seconds": elapsed,
                "error": msg,
            })


def validate_batch_config(config: dict) -> Optional[str]:
    """Validate batch configuration structure. Returns error message if invalid."""
    if not isinstance(config, dict):
        return "Config must be a JSON object"
    
    if "prompts" not in config:
        return "Config must contain 'prompts' array"
    
    if not isinstance(config["prompts"], list):
        return "'prompts' must be an array"
    
    if len(config["prompts"]) == 0:
        return "'prompts' array cannot be empty"
    
    for i, prompt_obj in enumerate(config["prompts"]):
        if not isinstance(prompt_obj, dict):
            return f"Prompt at index {i} must be an object"
        
        if "prompt" not in prompt_obj or not isinstance(prompt_obj["prompt"], str):
            return f"Prompt at index {i} missing 'prompt' string"
    
    return None


@app.route("/health", methods=["GET"])
def health_check():
    """Health check endpoint."""
    device = "unknown"
    try:
        if torch.cuda.is_available():
            device = f"cuda (NVIDIA)"
        elif hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
            device = "mps (Apple Silicon)"
        else:
            device = "cpu"
    except Exception:
        pass
    
    return jsonify({
        "status": "healthy",
        "device": device,
        "timestamp": datetime.now(timezone.utc).isoformat()
    }), 200


@app.route("/generate", methods=["POST"])
def generate_endpoint():
    """
    POST /generate

    Accepts two request shapes:

    Flat form — settings at root level:
    {
        "prompts": [...],
        "steps": 40,
        "guidance": 7.5,
        "width": 1024,
        "height": 1024,
        "refine": false,
        "cpu": false
    }

    Object form — globals nested under a "settings" key (CLI batch.json verbatim).
    An optional top-level "description" field is ignored. Root-level keys always
    override matching keys inside "settings":
    {
        "description": "optional, ignored",
        "settings": {
            "steps": 30,
            "guidance": 9.0,
            "width": 768,
            "height": 768,
            "refine": true,
            "cpu": false
        },
        "prompts": [...]
    }

    Resolution precedence: explicit root value > nested settings value > built-in default.

    Response JSON:
    {
        "status": "success",
        "device": "cuda",
        "results": [
            {
                "prompt": "a tropical sunset",
                "output": "/app/outputs/sunset.png",
                "status": "ok",
                "error": null,
                "filename": "sunset.png",
                "content_type": "image/png",
                "image_base64": "iVBORw0KGgo...=="
            }
        ],
        "timestamp": "2026-07-03T09:23:14Z"
    }
    """
    try:
        config = request.get_json()
        
        if config is None:
            return jsonify({
                "status": "error",
                "error": "Request body must be valid JSON",
                "timestamp": datetime.now(timezone.utc).isoformat()
            }), 400
        
        # Validate structure
        validation_error = validate_batch_config(config)
        if validation_error:
            return jsonify({
                "status": "error",
                "error": validation_error,
                "timestamp": datetime.now(timezone.utc).isoformat()
            }), 400
        
        # Support object-form: globals nested under "settings" (CLI batch.json).
        # Root-level keys always win over nested settings values.
        settings = config["settings"] if isinstance(config.get("settings"), dict) else {}

        def _resolve(key, default):
            if key in config:
                return config[key]
            if key in settings:
                return settings[key]
            return default

        # Extract parameters with defaults (root > settings > default)
        prompts = config.get("prompts", [])
        steps = int(_resolve("steps", 40))
        guidance = float(_resolve("guidance", 7.5))
        width = int(_resolve("width", 1024))
        height = int(_resolve("height", 1024))
        refine = _resolve("refine", False)
        use_cpu = _resolve("cpu", False)
        
        # Validate numeric ranges
        if steps < 1 or steps > 150:
            return jsonify({
                "status": "error",
                "error": "'steps' must be between 1 and 150",
                "timestamp": datetime.now(timezone.utc).isoformat()
            }), 400
        
        if guidance < 0 or guidance > 50:
            return jsonify({
                "status": "error",
                "error": "'guidance' must be between 0 and 50",
                "timestamp": datetime.now(timezone.utc).isoformat()
            }), 400
        
        if width not in (512, 768, 1024):
            return jsonify({
                "status": "error",
                "error": "'width' must be 512, 768, or 1024",
                "timestamp": datetime.now(timezone.utc).isoformat()
            }), 400
        
        if height not in (512, 768, 1024):
            return jsonify({
                "status": "error",
                "error": "'height' must be 512, 768, or 1024",
                "timestamp": datetime.now(timezone.utc).isoformat()
            }), 400
        
        device = "cpu" if use_cpu else get_device(use_cpu)
        logger.info(f"Generating {len(prompts)} images on device: {device}")

        # Build per-item list in the reference batch format
        batch_items = []
        for p_obj in prompts:
            batch_items.append({
                "prompt": p_obj["prompt"],
                "output": p_obj.get("output"),
                "seed": p_obj.get("seed"),
                "negative_prompt": p_obj.get("negative_prompt"),
            })

        results = run_batch(
            batch_items,
            steps=steps,
            guidance=guidance,
            width=width,
            height=height,
            refine=refine,
            cpu=use_cpu,
        )
        for r in results:
            if r["status"] == "ok":
                logger.info(f"✅ Generated: {r['output']}")
            else:
                logger.error(f"❌ {r['status']}: {r['error']}")

        # Enrich ok results with base64 image bytes for ephemeral cloud filesystems
        for r in results:
            if r["status"] == "ok" and r.get("output") and os.path.isfile(r["output"]):
                try:
                    with open(r["output"], "rb") as f:
                        r["image_base64"] = base64.b64encode(f.read()).decode("utf-8")
                    r["filename"] = os.path.basename(r["output"])
                    r["content_type"] = "image/png"
                except Exception as read_err:
                    r["image_base64"] = None
                    existing_error = r.get("error") or ""
                    r["error"] = f"{existing_error}; read error: {read_err}".lstrip("; ")
            else:
                r["image_base64"] = None

        return jsonify({
            "status": "success",
            "device": device,
            "results": results,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }), 200
    
    except ValueError as e:
        logger.error(f"Invalid parameter value: {e}")
        return jsonify({
            "status": "error",
            "error": f"Invalid parameter: {str(e)}",
            "timestamp": datetime.now(timezone.utc).isoformat()
        }), 400
    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)
        return jsonify({
            "status": "error",
            "error": f"Internal server error: {str(e)}",
            "timestamp": datetime.now(timezone.utc).isoformat()
        }), 500


@app.route("/model/pull", methods=["POST"])
def model_pull():
    """
    POST /model/pull

    Starts a one-time background download of the SDXL base model into the
    HF disk cache. Returns 202 immediately; poll GET /model/status for progress.

    Optional JSON body: {"force": true} — re-pull even if state is already "ready".

    Response:
        { "state": "in_progress"|"ready"|"error"|"not_started",
          "message": "...", "timestamp": "..." }
    """
    body = request.get_json(silent=True) or {}
    force = bool(body.get("force", False))

    with _model_state_lock:
        current_state = _model_state["state"]

        if current_state == "in_progress":
            return jsonify({
                "state": "in_progress",
                "message": "Pull already in progress.",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }), 202

        if current_state == "ready" and not force:
            return jsonify({
                "state": "ready",
                "message": "Model is already cached. Pass {\"force\": true} to re-pull.",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }), 200

        # Transition to in_progress synchronously (inside the lock) so a
        # concurrent caller sees the right state before the thread starts.
        _model_state.update({
            "state": "in_progress",
            "message": "Pull started.",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "finished_at": None,
            "elapsed_seconds": None,
            "error": None,
        })

    thread = threading.Thread(target=_pull_worker, daemon=True, name="model-pull")
    thread.start()
    logger.info("model/pull: background thread launched")

    return jsonify({
        "state": "in_progress",
        "message": "Model pull started in background. Poll GET /model/status.",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }), 202


@app.route("/model/status", methods=["GET"])
def model_status():
    """
    GET /model/status

    Returns the current model warm-up state.

    Response (always HTTP 200):
        { "state": "not_started"|"in_progress"|"ready"|"error",
          "message": "...",
          "started_at": "<ISO8601 or null>",
          "finished_at": "<ISO8601 or null>",
          "elapsed_seconds": <number or null>,
          "error": "<string or null>",
          "device": "cuda|mps|cpu",
          "timestamp": "<ISO8601>" }
    """
    with _model_state_lock:
        snapshot = dict(_model_state)

    snapshot["device"] = get_device()
    snapshot["timestamp"] = datetime.now(timezone.utc).isoformat()

    # Only include "error" key when state is error
    if snapshot["state"] != "error":
        snapshot.pop("error", None)

    return jsonify(snapshot), 200


@app.route("/", methods=["GET"])
def root():
    """API root endpoint with basic info."""
    return jsonify({
        "name": "SDXL Image Generation API",
        "version": "1.0.0",
        "endpoints": {
            "GET /": "This message",
            "GET /health": "Health check",
            "POST /generate": "Generate images from batch config",
            "POST /model/pull": "Start async model download/warm-up (returns 202)",
            "GET /model/status": "Poll model warm-up state"
        },
        "documentation": "POST /generate with JSON batch config containing 'prompts' array"
    }), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    logger.info(f"Starting SDXL Generation API on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)

