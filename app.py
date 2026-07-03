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
from datetime import datetime

from src.image_generation.generate import generate_with_retry, OOMError, get_device, batch_generate
from types import SimpleNamespace


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
        "timestamp": datetime.utcnow().isoformat()
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
                "timestamp": datetime.utcnow().isoformat()
            }), 400
        
        # Validate structure
        validation_error = validate_batch_config(config)
        if validation_error:
            return jsonify({
                "status": "error",
                "error": validation_error,
                "timestamp": datetime.utcnow().isoformat()
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
                "timestamp": datetime.utcnow().isoformat()
            }), 400
        
        if guidance < 0 or guidance > 50:
            return jsonify({
                "status": "error",
                "error": "'guidance' must be between 0 and 50",
                "timestamp": datetime.utcnow().isoformat()
            }), 400
        
        if width not in (512, 768, 1024):
            return jsonify({
                "status": "error",
                "error": "'width' must be 512, 768, or 1024",
                "timestamp": datetime.utcnow().isoformat()
            }), 400
        
        if height not in (512, 768, 1024):
            return jsonify({
                "status": "error",
                "error": "'height' must be 512, 768, or 1024",
                "timestamp": datetime.utcnow().isoformat()
            }), 400
        
        device = "cpu" if use_cpu else get_device(use_cpu)
        logger.info(f"Generating {len(prompts)} images on device: {device}")
        
        # Convert prompts to expected format
        generation_inputs = []
        for p_obj in prompts:
            prompt_text = p_obj["prompt"]
            output_path = p_obj.get("output")
            if output_path is None:
                os.makedirs("outputs", exist_ok=True)
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                output_path = f"outputs/image_{timestamp}.png"
            
            generation_inputs.append({
                "prompt": prompt_text,
                "output": output_path,
                "seed": p_obj.get("seed"),
                "steps": steps,
                "guidance": guidance,
                "width": width,
                "height": height,
                "refine": refine,
                "cpu": use_cpu
            })
        
        # Generate images
        results = []
        for gen_input in generation_inputs:
            args = SimpleNamespace(
                prompt=gen_input["prompt"],
                output=gen_input["output"],
                seed=gen_input.get("seed"),
                steps=gen_input.get("steps", steps),
                guidance=gen_input.get("guidance", guidance),
                width=gen_input.get("width", width),
                height=gen_input.get("height", height),
                refine=gen_input.get("refine", refine),
                cpu=gen_input.get("cpu", use_cpu)
            )
            
            try:
                output_path = generate_with_retry(args, max_retries=2)
                results.append({
                    "prompt": gen_input["prompt"],
                    "output": output_path,
                    "status": "ok",
                    "error": None
                })
                logger.info(f"✅ Generated: {output_path}")
            except OOMError as e:
                error_msg = str(e)
                results.append({
                    "prompt": gen_input["prompt"],
                    "output": None,
                    "status": "oom_error",
                    "error": error_msg
                })
                logger.error(f"❌ OOM Error: {error_msg}")
            except Exception as e:
                error_msg = str(e)
                results.append({
                    "prompt": gen_input["prompt"],
                    "output": None,
                    "status": "error",
                    "error": error_msg
                })
                logger.error(f"❌ Generation failed: {error_msg}")
        
        return jsonify({
            "status": "success",
            "device": device,
            "results": results,
            "timestamp": datetime.utcnow().isoformat()
        }), 200
    
    except ValueError as e:
        logger.error(f"Invalid parameter value: {e}")
        return jsonify({
            "status": "error",
            "error": f"Invalid parameter: {str(e)}",
            "timestamp": datetime.utcnow().isoformat()
        }), 400
    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)
        return jsonify({
            "status": "error",
            "error": f"Internal server error: {str(e)}",
            "timestamp": datetime.utcnow().isoformat()
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
