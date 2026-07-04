#!/usr/bin/env python3
"""
Flask wrapper for SDXL image generation.
Exposes /generate POST endpoint accepting JSON batch configs.
"""

import json
import logging
import os
from typing import Any, Optional

import torch
from flask import Flask, request, jsonify
from datetime import datetime, timezone

from src.image_generation.generate import run_batch, OOMError, get_device


# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = Flask(__name__)


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
    
    Request JSON:
    {
        "prompts": [
            {"prompt": "a tropical sunset", "seed": 42, "output": "sunset.png"},
            {"prompt": "underwater scene", "seed": 43}
        ],
        "steps": 40,
        "guidance": 7.5,
        "width": 1024,
        "height": 1024,
        "refine": false
    }
    
    Response JSON:
    {
        "status": "success",
        "device": "cuda",
        "results": [
            {
                "prompt": "a tropical sunset",
                "output": "/path/to/sunset.png",
                "status": "ok",
                "error": null
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
        
        # Extract parameters with defaults
        prompts = config.get("prompts", [])
        steps = int(config.get("steps", 40))
        guidance = float(config.get("guidance", 7.5))
        width = int(config.get("width", 1024))
        height = int(config.get("height", 1024))
        refine = config.get("refine", False)
        use_cpu = config.get("cpu", False)
        
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


@app.route("/", methods=["GET"])
def root():
    """API root endpoint with basic info."""
    return jsonify({
        "name": "SDXL Image Generation API",
        "version": "1.0.0",
        "endpoints": {
            "GET /": "This message",
            "GET /health": "Health check",
            "POST /generate": "Generate images from batch config"
        },
        "documentation": "POST /generate with JSON batch config containing 'prompts' array"
    }), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    logger.info(f"Starting SDXL Generation API on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)

