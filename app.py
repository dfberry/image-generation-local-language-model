#!/usr/bin/env python3
"""
Flask wrapper for SDXL image generation.

Endpoints:
  GET  /             - API info (JSON)
  GET  /health       - Health check (no model load)
  GET  /ui           - Browser UI (HTML, self-contained)
  POST /generate     - Generate images from a batch config (loads model on first call)
  POST /model/pull   - Kick off async model download/warm-up; returns 202 immediately
  GET  /model/status - Poll warm-up state: not_started | in_progress | ready | error

CORS: permissive (Access-Control-Allow-Origin: *) on all routes so the API
can be called from any browser origin or from the /ui page itself.
"""

import base64
import gc
import json
import logging
import os
import threading
from typing import Any, Optional

import torch
from flask import Flask, request, jsonify, make_response
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
# CORS — permissive, no auth required, works from any browser/origin
# ---------------------------------------------------------------------------
@app.after_request
def _add_cors_headers(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    return response

@app.route("/", defaults={"path": ""}, methods=["OPTIONS"])
@app.route("/<path:path>", methods=["OPTIONS"])
def _options_preflight(path):
    resp = make_response("", 204)
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    return resp

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


@app.route("/ui", methods=["GET"])
def browser_ui():
    """Serve a self-contained browser UI for health, model management, and image generation."""
    html = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>SDXL Image Generator</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:system-ui,sans-serif;background:#0f0f13;color:#e2e2e6;min-height:100vh;padding:1.5rem}
h1{font-size:1.6rem;font-weight:700;margin-bottom:1.5rem;color:#a78bfa}
h2{font-size:1.1rem;font-weight:600;margin-bottom:.75rem;color:#c4b5fd}
.card{background:#1a1a24;border:1px solid #2d2d40;border-radius:10px;padding:1.25rem;margin-bottom:1.25rem}
.row{display:flex;gap:.75rem;align-items:center;flex-wrap:wrap;margin-bottom:.5rem}
.badge{display:inline-block;padding:.2rem .6rem;border-radius:20px;font-size:.75rem;font-weight:600;text-transform:uppercase}
.badge-ok{background:#14532d;color:#4ade80}
.badge-warn{background:#713f12;color:#fbbf24}
.badge-error{background:#450a0a;color:#f87171}
.badge-info{background:#1e3a5f;color:#60a5fa}
.badge-grey{background:#27272a;color:#a1a1aa}
button{background:#7c3aed;color:#fff;border:none;border-radius:6px;padding:.45rem .9rem;font-size:.85rem;cursor:pointer;font-weight:600;transition:background .15s}
button:hover{background:#6d28d9}
button:disabled{background:#3f3f46;color:#71717a;cursor:not-allowed}
button.secondary{background:#27272a;color:#e2e2e6}
button.secondary:hover{background:#3f3f46}
button.danger{background:#b91c1c}
button.danger:hover{background:#991b1b}
.mono{font-family:monospace;font-size:.8rem;color:#94a3b8}
label{display:block;font-size:.8rem;font-weight:500;color:#a1a1aa;margin-bottom:.25rem}
input[type=text],input[type=number],select,textarea{width:100%;background:#0f0f13;border:1px solid #3f3f46;border-radius:6px;color:#e2e2e6;padding:.4rem .6rem;font-size:.85rem;font-family:inherit}
input[type=text]:focus,input[type=number]:focus,select:focus,textarea:focus{outline:none;border-color:#7c3aed}
textarea{resize:vertical;min-height:120px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:.75rem}
.grid3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:.75rem}
.spinner{display:inline-block;width:16px;height:16px;border:2px solid #7c3aed;border-top-color:transparent;border-radius:50%;animation:spin .7s linear infinite;vertical-align:middle;margin-right:.4rem}
@keyframes spin{to{transform:rotate(360deg)}}
.hidden{display:none}
.img-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:1rem;margin-top:1rem}
.img-card{background:#0f0f13;border:1px solid #2d2d40;border-radius:8px;overflow:hidden}
.img-card img{width:100%;display:block}
.img-card .img-meta{padding:.6rem;display:flex;flex-direction:column;gap:.4rem}
.img-card .img-meta p{font-size:.75rem;color:#94a3b8;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.error-box{background:#450a0a;border:1px solid #b91c1c;border-radius:6px;padding:.75rem;margin-top:.5rem;font-size:.85rem;color:#fca5a5}
.progress-bar{width:100%;height:4px;background:#27272a;border-radius:2px;overflow:hidden;margin-top:.5rem}
.progress-bar-fill{height:100%;background:#7c3aed;border-radius:2px;transition:width .3s}
.tabs{display:flex;gap:0;margin-bottom:1rem;border-bottom:1px solid #2d2d40}
.tab{padding:.5rem 1rem;cursor:pointer;font-size:.85rem;font-weight:500;color:#71717a;border-bottom:2px solid transparent;margin-bottom:-1px}
.tab.active{color:#a78bfa;border-bottom-color:#a78bfa}
.tab-panel{display:none}
.tab-panel.active{display:block}
.hint{font-size:.75rem;color:#71717a;margin-top:.25rem}
.checkbox-row{display:flex;align-items:center;gap:.5rem}
.checkbox-row input{width:auto}
.status-line{display:flex;align-items:center;gap:.5rem;flex-wrap:wrap}
.elapsed{font-size:.75rem;color:#71717a}
a.dl-link{display:inline-block;margin-top:.25rem;padding:.3rem .6rem;background:#27272a;color:#a78bfa;border-radius:4px;font-size:.75rem;text-decoration:none;font-weight:600}
a.dl-link:hover{background:#3f3f46}
</style>
</head>
<body>
<h1>🎨 SDXL Image Generator</h1>

<!-- Health Card -->
<div class="card" id="health-card">
  <div class="row">
    <h2>Server Health</h2>
    <button class="secondary" onclick="refreshHealth()">↻ Refresh</button>
    <span id="health-auto-label" class="mono" style="font-size:.7rem;color:#52525b"></span>
  </div>
  <div class="status-line" id="health-status-line">
    <span class="badge badge-grey" id="health-badge">—</span>
    <span class="mono" id="health-device"></span>
    <span class="elapsed" id="health-ts"></span>
  </div>
</div>

<!-- Model Card -->
<div class="card" id="model-card">
  <div class="row">
    <h2>Model Status</h2>
    <button onclick="pullModel()" id="btn-pull">⬇ Download / Warm Up Model</button>
    <button class="secondary" onclick="refreshModel()">↻ Refresh</button>
  </div>
  <div class="status-line" id="model-status-line">
    <span class="badge badge-grey" id="model-badge">—</span>
    <span class="mono" id="model-msg"></span>
  </div>
  <div id="model-progress" class="hidden">
    <div class="progress-bar"><div class="progress-bar-fill" id="model-progress-fill" style="width:30%"></div></div>
  </div>
  <div class="elapsed" id="model-elapsed" style="margin-top:.4rem"></div>
  <div class="error-box hidden" id="model-error"></div>
</div>

<!-- Generate Card -->
<div class="card">
  <h2>Generate Images</h2>
  <p class="hint" style="margin-bottom:.75rem">💡 For a quick smoke test on CPU, use Steps=20 and 512×512.</p>

  <div class="tabs">
    <div class="tab active" onclick="switchTab('prompt')" id="tab-prompt">✏️ Free-text prompt</div>
    <div class="tab" onclick="switchTab('batch')" id="tab-batch">📄 Batch JSON</div>
  </div>

  <!-- Prompt tab -->
  <div class="tab-panel active" id="panel-prompt">
    <div style="margin-bottom:.75rem">
      <label>Prompt *</label>
      <input type="text" id="prompt-text" placeholder="a serene mountain lake at sunrise, golden light, photorealistic">
    </div>
    <div style="margin-bottom:.75rem">
      <label>Negative prompt (optional)</label>
      <input type="text" id="prompt-neg" placeholder="blur, noise, cartoon">
    </div>
    <div class="grid3" style="margin-bottom:.75rem">
      <div>
        <label>Steps (1–150)</label>
        <input type="number" id="p-steps" value="20" min="1" max="150">
      </div>
      <div>
        <label>Guidance (0–50)</label>
        <input type="number" id="p-guidance" value="7.5" min="0" max="50" step="0.5">
      </div>
      <div>
        <label>Seed (blank = random)</label>
        <input type="number" id="p-seed" placeholder="42">
      </div>
    </div>
    <div class="grid2" style="margin-bottom:.75rem">
      <div>
        <label>Width</label>
        <select id="p-width">
          <option value="512" selected>512</option>
          <option value="768">768</option>
          <option value="1024">1024</option>
        </select>
      </div>
      <div>
        <label>Height</label>
        <select id="p-height">
          <option value="512" selected>512</option>
          <option value="768">768</option>
          <option value="1024">1024</option>
        </select>
      </div>
    </div>
    <div class="row" style="margin-bottom:.75rem">
      <div class="checkbox-row">
        <input type="checkbox" id="p-refine">
        <label style="margin:0" for="p-refine">Use refiner (slower, higher quality)</label>
      </div>
      <div class="checkbox-row">
        <input type="checkbox" id="p-cpu" checked>
        <label style="margin:0" for="p-cpu">Force CPU</label>
      </div>
    </div>
  </div>

  <!-- Batch tab -->
  <div class="tab-panel" id="panel-batch">
    <div style="margin-bottom:.5rem">
      <label>Upload batch JSON file</label>
      <input type="file" id="batch-file" accept=".json,application/json" style="background:transparent;border:none;padding:0;color:#94a3b8">
    </div>
    <div>
      <label>Or paste / edit batch JSON</label>
      <textarea id="batch-json" placeholder='{"prompts":[{"prompt":"a tropical sunset","seed":42}],"steps":20,"width":512,"height":512}'></textarea>
    </div>
    <p class="hint">Shape: <code>{"prompts":[{"prompt":"...","seed":42}],"steps":20,"width":512,"height":512}</code></p>
  </div>

  <div style="margin-top:1rem;display:flex;gap:.75rem;align-items:center;flex-wrap:wrap">
    <button onclick="generate()" id="btn-generate">🚀 Generate</button>
    <span id="gen-spinner" class="hidden"><span class="spinner"></span>Generating… (may take minutes on CPU)</span>
    <button class="secondary hidden" id="btn-dl-all" onclick="downloadAll()">⬇ Download all</button>
  </div>
  <div class="error-box hidden" id="gen-error"></div>
  <div id="gen-results" class="img-grid"></div>
</div>

<script>
const BASE = '';  // same origin

// ── helpers ─────────────────────────────────────────────────────────────────
function badgeClass(state){
  if(state==='healthy'||state==='ready'||state==='ok') return 'badge-ok';
  if(state==='in_progress') return 'badge-info';
  if(state==='error') return 'badge-error';
  if(state==='not_started') return 'badge-grey';
  return 'badge-warn';
}
function setBadge(el,text,cls){
  el.textContent=text;
  el.className='badge '+cls;
}
function showError(el,msg){el.textContent=msg;el.classList.remove('hidden')}
function hideError(el){el.classList.add('hidden')}

// ── health ───────────────────────────────────────────────────────────────────
let _healthTimer=null;
async function refreshHealth(){
  const badge=document.getElementById('health-badge');
  const dev=document.getElementById('health-device');
  const ts=document.getElementById('health-ts');
  try{
    const r=await fetch(BASE+'/health');
    const d=await r.json();
    setBadge(badge,d.status||'?',badgeClass(d.status));
    dev.textContent=d.device?'device: '+d.device:'';
    ts.textContent=d.timestamp?new Date(d.timestamp).toLocaleTimeString():'';
  }catch(e){
    setBadge(badge,'unreachable','badge-error');
    dev.textContent='';
    ts.textContent='';
  }
}
function startHealthPoll(){
  refreshHealth();
  document.getElementById('health-auto-label').textContent='auto-refreshes every 15s';
  _healthTimer=setInterval(refreshHealth,15000);
}

// ── model ────────────────────────────────────────────────────────────────────
let _modelPollTimer=null;
async function refreshModel(){
  const badge=document.getElementById('model-badge');
  const msg=document.getElementById('model-msg');
  const elapsed=document.getElementById('model-elapsed');
  const prog=document.getElementById('model-progress');
  const err=document.getElementById('model-error');
  const btn=document.getElementById('btn-pull');
  try{
    const r=await fetch(BASE+'/model/status');
    const d=await r.json();
    const state=d.state||'unknown';
    setBadge(badge,state,badgeClass(state));
    msg.textContent=d.message||'';
    elapsed.textContent=d.elapsed_seconds!=null?('⏱ '+d.elapsed_seconds.toFixed(1)+'s'):'';
    if(state==='in_progress'){
      prog.classList.remove('hidden');
      btn.disabled=true;
      ensureModelPoll();
    } else {
      prog.classList.add('hidden');
      btn.disabled=false;
      stopModelPoll();
    }
    if(state==='error'&&d.error){
      showError(err,d.error);
    } else {
      hideError(err);
    }
    // warn generate button if not ready
    document.getElementById('btn-generate').title=
      state==='ready'?'':'Model not ready — generation may fail or be slow. Pull model first.';
  }catch(e){
    setBadge(badge,'fetch error','badge-error');
    msg.textContent=String(e);
  }
}
function ensureModelPoll(){
  if(!_modelPollTimer) _modelPollTimer=setInterval(refreshModel,5000);
}
function stopModelPoll(){
  if(_modelPollTimer){clearInterval(_modelPollTimer);_modelPollTimer=null;}
}
async function pullModel(){
  const btn=document.getElementById('btn-pull');
  btn.disabled=true;
  hideError(document.getElementById('model-error'));
  try{
    const r=await fetch(BASE+'/model/pull',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
    const d=await r.json();
    document.getElementById('model-badge').textContent=d.state||'?';
    document.getElementById('model-msg').textContent=d.message||'';
    ensureModelPoll();
  }catch(e){
    btn.disabled=false;
    showError(document.getElementById('model-error'),'Failed to start pull: '+String(e));
  }
}

// ── tabs ─────────────────────────────────────────────────────────────────────
function switchTab(name){
  ['prompt','batch'].forEach(t=>{
    document.getElementById('tab-'+t).classList.toggle('active',t===name);
    document.getElementById('panel-'+t).classList.toggle('active',t===name);
  });
}
// file picker fills textarea
document.getElementById('batch-file').addEventListener('change',function(){
  const f=this.files[0];if(!f)return;
  const reader=new FileReader();
  reader.onload=e=>{document.getElementById('batch-json').value=e.target.result;};
  reader.readAsText(f);
});

// ── generate ─────────────────────────────────────────────────────────────────
let _lastResults=[];
async function generate(){
  const btn=document.getElementById('btn-generate');
  const spinner=document.getElementById('gen-spinner');
  const errEl=document.getElementById('gen-error');
  const resultsEl=document.getElementById('gen-results');
  const dlAll=document.getElementById('btn-dl-all');

  hideError(errEl);
  resultsEl.innerHTML='';
  dlAll.classList.add('hidden');
  _lastResults=[];
  btn.disabled=true;
  spinner.classList.remove('hidden');

  let body;
  // Determine active tab
  const isPromptTab=document.getElementById('panel-prompt').classList.contains('active');
  if(isPromptTab){
    const p=document.getElementById('prompt-text').value.trim();
    if(!p){
      showError(errEl,'Prompt is required.');
      btn.disabled=false;spinner.classList.add('hidden');return;
    }
    const promptObj={prompt:p};
    const neg=document.getElementById('prompt-neg').value.trim();
    if(neg) promptObj.negative_prompt=neg;
    const seed=document.getElementById('p-seed').value.trim();
    if(seed) promptObj.seed=parseInt(seed,10);
    body={
      prompts:[promptObj],
      steps:parseInt(document.getElementById('p-steps').value,10)||20,
      guidance:parseFloat(document.getElementById('p-guidance').value)||7.5,
      width:parseInt(document.getElementById('p-width').value,10)||512,
      height:parseInt(document.getElementById('p-height').value,10)||512,
      refine:document.getElementById('p-refine').checked,
      cpu:document.getElementById('p-cpu').checked,
    };
  } else {
    const raw=document.getElementById('batch-json').value.trim();
    if(!raw){
      showError(errEl,'Paste or upload a batch JSON file.');
      btn.disabled=false;spinner.classList.add('hidden');return;
    }
    try{body=JSON.parse(raw);}
    catch(e){
      showError(errEl,'Invalid JSON: '+e.message);
      btn.disabled=false;spinner.classList.add('hidden');return;
    }
  }

  try{
    const r=await fetch(BASE+'/generate',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify(body),
    });
    const data=await r.json();
    if(data.status!=='success'){
      showError(errEl,'Server error: '+(data.error||JSON.stringify(data)));
      return;
    }
    _lastResults=data.results||[];
    renderResults(_lastResults,resultsEl);
    if(_lastResults.some(r=>r.image_base64)) dlAll.classList.remove('hidden');
  }catch(e){
    showError(errEl,'Request failed: '+String(e));
  }finally{
    btn.disabled=false;
    spinner.classList.add('hidden');
  }
}

function renderResults(results,container){
  container.innerHTML='';
  results.forEach((r,i)=>{
    const div=document.createElement('div');
    div.className='img-card';
    if(r.status==='ok'&&r.image_base64){
      const img=document.createElement('img');
      img.src='data:image/png;base64,'+r.image_base64;
      img.alt=r.prompt||'Generated image '+i;
      div.appendChild(img);
      const meta=document.createElement('div');
      meta.className='img-meta';
      const pp=document.createElement('p');
      pp.title=r.prompt||'';
      pp.textContent=(r.prompt||'').substring(0,80)||(r.filename||'image');
      meta.appendChild(pp);
      const a=document.createElement('a');
      a.className='dl-link';
      a.href='data:image/png;base64,'+r.image_base64;
      a.download=r.filename||('image-'+i+'.png');
      a.textContent='⬇ Download';
      meta.appendChild(a);
      div.appendChild(meta);
    } else {
      const meta=document.createElement('div');
      meta.className='img-meta';
      const errDiv=document.createElement('div');
      errDiv.className='error-box';
      errDiv.style.margin='0';
      errDiv.textContent='Error: '+(r.error||r.status||'unknown error');
      meta.appendChild(errDiv);
      const pp=document.createElement('p');
      pp.textContent=(r.prompt||'').substring(0,80);
      meta.appendChild(pp);
      div.appendChild(meta);
    }
    container.appendChild(div);
  });
}

function downloadAll(){
  _lastResults.forEach((r,i)=>{
    if(r.status==='ok'&&r.image_base64){
      const a=document.createElement('a');
      a.href='data:image/png;base64,'+r.image_base64;
      a.download=r.filename||('image-'+i+'.png');
      document.body.appendChild(a);a.click();document.body.removeChild(a);
    }
  });
}

// ── init ─────────────────────────────────────────────────────────────────────
startHealthPoll();
refreshModel();
</script>
</body>
</html>"""
    resp = make_response(html, 200)
    resp.headers["Content-Type"] = "text/html; charset=utf-8"
    return resp


@app.route("/", methods=["GET"])
def root():
    """API root endpoint with basic info."""
    return jsonify({
        "name": "SDXL Image Generation API",
        "version": "1.0.0",
        "endpoints": {
            "GET /": "This message",
            "GET /health": "Health check",
            "GET /ui": "Browser UI — open in your browser to manage and generate images",
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

