#!/usr/bin/env python3
"""
Flask wrapper for SDXL image generation.

Endpoints:
  GET  /             - Browser UI (HTML, self-contained)
  GET  /ui           - Browser UI (alias of /)
  GET  /api          - API info (JSON)
  GET  /health       - Health check (no model load)
  POST /generate     - Generate images from a batch config (synchronous; loads model on first call)
  POST /generate/async - Start background generation; returns 202 immediately
  GET  /generate/status - Poll generation state: idle | in_progress | ready | error | cancelled
  POST /generate/cancel - Request cancellation of async generation
  POST /model/pull   - Kick off async model download/warm-up; returns 202 immediately
  GET  /model/status - Poll warm-up state: not_started | in_progress | ready | error | stalled

CORS: permissive (Access-Control-Allow-Origin: *) on all routes so the API
can be called from any browser origin or from the / (UI) page itself.
"""

import base64
import gc
import json
import logging
import os
import re
import shutil
import tempfile
import threading
import uuid
from typing import Any, Optional

import torch
from flask import Flask, request, jsonify, make_response, send_file, abort
from datetime import datetime, timezone

from src.image_generation.generate import (
    run_batch, OOMError, GenerationCancelled, get_device, load_base
)


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
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    return response

@app.route("/", defaults={"path": ""}, methods=["OPTIONS"])
@app.route("/<path:path>", methods=["OPTIONS"])
def _options_preflight(path):
    resp = make_response("", 204)
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    return resp

# ---------------------------------------------------------------------------
# Model warm-up state
# ---------------------------------------------------------------------------
BASE_MODEL_CACHE_DIR = "models--stabilityai--stable-diffusion-xl-base-1.0"
PROGRESS_FILE_NAME = ".pull-progress.json"
PULL_STALE_AFTER_SECONDS = 120
PULL_HEARTBEAT_SECONDS = 5
BASE_MODEL_EXPECTED_BYTES = int(os.environ.get(
    "SDXL_BASE_MODEL_EXPECTED_BYTES", str(7 * 1024 * 1024 * 1024)
))
BASE_MODEL_READY_MIN_BYTES = int(os.environ.get(
    "SDXL_BASE_MODEL_READY_MIN_BYTES", str(5 * 1024 * 1024 * 1024)
))

_model_state_lock = threading.Lock()
_model_state: dict = {
    "state": "not_started",   # not_started | in_progress | ready | error
    "message": "Model has not been pulled yet.",
    "bytes_downloaded": 0,
    "bytes_expected": BASE_MODEL_EXPECTED_BYTES,
    "percent": 0.0,
    "weights_present": False,
    "started_at": None,
    "last_updated": None,
    "finished_at": None,
    "elapsed_seconds": None,
    "error": None,
    "cache_path": None,
    "revision": os.environ.get("SDXL_MODEL_REVISION") or None,
}
_model_pull_thread: Optional[threading.Thread] = None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


def _parse_iso_datetime(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except ValueError:
        return None


def get_hf_cache_root() -> str:
    """Return the HF cache root used by DiffusionPipeline.from_pretrained."""
    return os.environ.get("HF_HOME") or os.path.expanduser("~/.cache/huggingface")


def get_hf_hub_cache_dir(cache_root: Optional[str] = None) -> str:
    """Return the Hugging Face hub cache directory beneath the HF cache root."""
    return os.environ.get("HF_HUB_CACHE") or os.path.join(
        cache_root or get_hf_cache_root(), "hub"
    )


def get_base_model_cache_path(cache_root: Optional[str] = None) -> str:
    return os.path.join(
        get_hf_hub_cache_dir(cache_root), BASE_MODEL_CACHE_DIR
    )


def get_pull_progress_path(cache_root: Optional[str] = None) -> str:
    return os.path.join(cache_root or get_hf_cache_root(), PROGRESS_FILE_NAME)


def _safe_dir_size(path: str) -> int:
    total = 0
    if not os.path.isdir(path):
        return 0
    for root, _, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                continue
    return total


def _calculate_percent(bytes_downloaded: Optional[int], bytes_expected: Optional[int]) -> Optional[float]:
    if not bytes_downloaded or not bytes_expected or bytes_expected <= 0:
        return None
    return round(min(100.0, (bytes_downloaded / bytes_expected) * 100.0), 2)


def _progress_payload(
    state: str,
    message: str,
    *,
    started_at: Optional[datetime] = None,
    finished_at: Optional[datetime] = None,
    error: Optional[str] = None,
    cache_root: Optional[str] = None,
    now: Optional[datetime] = None,
) -> dict:
    now = now or _utc_now()
    cache_root = cache_root or get_hf_cache_root()
    cache_path = get_base_model_cache_path(cache_root)
    bytes_downloaded = _safe_dir_size(cache_path)
    bytes_expected = BASE_MODEL_EXPECTED_BYTES
    started_iso = _iso(started_at)
    finished_iso = _iso(finished_at)
    elapsed_seconds = None
    if started_at is not None:
        end = finished_at or now
        elapsed_seconds = (end - started_at).total_seconds()
    return {
        "state": state,
        "message": message,
        "bytes_downloaded": bytes_downloaded,
        "bytes_expected": bytes_expected,
        "percent": _calculate_percent(bytes_downloaded, bytes_expected),
        "started_at": started_iso,
        "last_updated": _iso(now),
        "finished_at": finished_iso,
        "elapsed_seconds": elapsed_seconds,
        "error": error,
        "cache_path": cache_path,
        "revision": os.environ.get("SDXL_MODEL_REVISION") or None,
    }


def _write_progress_file(payload: dict, cache_root: Optional[str] = None) -> None:
    cache_root = cache_root or get_hf_cache_root()
    os.makedirs(cache_root, exist_ok=True)
    progress_path = get_pull_progress_path(cache_root)
    fd, temp_path = tempfile.mkstemp(
        prefix=f"{PROGRESS_FILE_NAME}.", suffix=".tmp", dir=cache_root
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, progress_path)
    except Exception:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise


def _safe_write_progress(payload: dict) -> None:
    try:
        _write_progress_file(payload)
    except Exception as exc:
        logger.warning("model/pull: could not write durable progress: %s", exc)


def _read_progress_file(cache_root: Optional[str] = None) -> Optional[dict]:
    progress_path = get_pull_progress_path(cache_root)
    try:
        with open(progress_path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def is_base_model_cache_present(
    cache_root: Optional[str] = None,
    *,
    min_ready_bytes: int = BASE_MODEL_READY_MIN_BYTES,
) -> bool:
    model_path = get_base_model_cache_path(cache_root)
    snapshots_path = os.path.join(model_path, "snapshots")
    if not os.path.isdir(snapshots_path):
        return False
    has_snapshot = False
    try:
        for entry in os.scandir(snapshots_path):
            if entry.is_dir():
                has_snapshot = True
                break
    except OSError:
        return False
    if not has_snapshot:
        return False
    return _safe_dir_size(model_path) >= min_ready_bytes


def _is_model_pull_worker_alive() -> bool:
    with _model_state_lock:
        return _model_pull_thread is not None and _model_pull_thread.is_alive()


def reconcile_model_status(
    *,
    cache_root: Optional[str] = None,
    active_worker: bool = False,
    now: Optional[datetime] = None,
    stale_after_seconds: int = PULL_STALE_AFTER_SECONDS,
    min_ready_bytes: int = BASE_MODEL_READY_MIN_BYTES,
) -> dict:
    """Reconcile durable progress with the actual HF cache on disk."""
    now = now or _utc_now()
    cache_root = cache_root or get_hf_cache_root()
    cache_path = get_base_model_cache_path(cache_root)

    with _model_state_lock:
        base = dict(_model_state)
    progress = _read_progress_file(cache_root)
    if progress:
        base.update(progress)

    bytes_downloaded = _safe_dir_size(cache_path)
    bytes_expected = base.get("bytes_expected") or BASE_MODEL_EXPECTED_BYTES
    base["bytes_downloaded"] = bytes_downloaded
    base["bytes_expected"] = bytes_expected
    base["percent"] = _calculate_percent(bytes_downloaded, bytes_expected)
    base.setdefault("last_updated", None)
    base.setdefault("finished_at", None)
    base.setdefault("elapsed_seconds", None)
    base.setdefault("error", None)
    base.setdefault("revision", os.environ.get("SDXL_MODEL_REVISION") or None)
    base["cache_path"] = cache_path

    model_present = is_base_model_cache_present(
        cache_root, min_ready_bytes=min_ready_bytes
    )
    base["weights_present"] = model_present
    if model_present and not active_worker:
        base.update({
            "state": "ready",
            "message": "Model weights are present on the HF cache share.",
            "bytes_downloaded": _safe_dir_size(cache_path),
            "bytes_expected": BASE_MODEL_EXPECTED_BYTES,
            "percent": 100.0,
            "weights_present": True,
            "finished_at": base.get("finished_at") or base.get("last_updated"),
            "error": None,
        })
    elif base.get("state") == "ready" and not model_present:
        base.update({
            "state": "not_started",
            "message": "Model cache is not present on the HF cache share.",
            "finished_at": None,
            "elapsed_seconds": None,
            "error": None,
        })
    elif base.get("state") == "in_progress" and not active_worker:
        last_updated = _parse_iso_datetime(base.get("last_updated"))
        age = (
            (now - last_updated).total_seconds()
            if last_updated
            else stale_after_seconds + 1
        )
        if age > stale_after_seconds:
            base.update({
                "state": "stalled",
                "message": (
                    "Model pull progress is stale. Trigger POST /model/pull "
                    "again or check container logs."
                ),
                "error": None,
            })

    started = _parse_iso_datetime(base.get("started_at"))
    finished = _parse_iso_datetime(base.get("finished_at"))
    if started and base.get("elapsed_seconds") is None:
        base["elapsed_seconds"] = ((finished or now) - started).total_seconds()

    base["timestamp"] = _iso(now)
    return base


def _heartbeat_pull_progress(stop_event: threading.Event, started: datetime, device: str) -> None:
    while not stop_event.wait(PULL_HEARTBEAT_SECONDS):
        payload = _progress_payload(
            "in_progress",
            f"Downloading/loading model on device '{device}'…",
            started_at=started,
        )
        _safe_write_progress(payload)


def _stop_pull_heartbeat(stop_event: threading.Event, heartbeat: threading.Thread) -> None:
    stop_event.set()
    heartbeat.join(timeout=1)


def _pull_worker() -> None:
    """Background thread: loads the base model to warm the on-disk HF cache."""
    device = get_device()
    started = _utc_now()

    with _model_state_lock:
        _model_state.update({
            "state": "in_progress",
            "message": f"Downloading/loading model on device '{device}'…",
            "started_at": _iso(started),
            "last_updated": _iso(started),
            "finished_at": None,
            "elapsed_seconds": None,
            "error": None,
        })

    _safe_write_progress(_progress_payload(
        "in_progress",
        f"Downloading/loading model on device '{device}'…",
        started_at=started,
        now=started,
    ))
    logger.info("model/pull: starting load_base() to warm HF disk cache")
    heartbeat_stop = threading.Event()
    heartbeat = threading.Thread(
        target=_heartbeat_pull_progress,
        args=(heartbeat_stop, started, device),
        daemon=True,
        name="model-pull-heartbeat",
    )
    heartbeat.start()

    try:
        pipe = load_base(device)

        # Free the in-memory pipeline immediately — the durable win is the
        # on-disk HF cache. run_batch() will reload from that warm cache when
        # /generate is called, avoiding the expensive ~7 GB network download.
        del pipe
        gc.collect()

        finished = _utc_now()
        elapsed = (finished - started).total_seconds()
        logger.info(f"model/pull: done in {elapsed:.1f}s; in-memory pipeline released")
        _stop_pull_heartbeat(heartbeat_stop, heartbeat)

        with _model_state_lock:
            _model_state.update({
                "state": "ready",
                "message": "Model weights cached on disk. /generate will load from cache.",
                "last_updated": _iso(finished),
                "finished_at": _iso(finished),
                "elapsed_seconds": elapsed,
                "error": None,
            })
        _safe_write_progress(_progress_payload(
            "ready",
            "Model weights cached on disk. /generate will load from cache.",
            started_at=started,
            finished_at=finished,
            now=finished,
        ))

    except (OOMError, Exception) as exc:
        finished = _utc_now()
        elapsed = (finished - started).total_seconds()
        msg = str(exc)
        logger.error(f"model/pull: failed after {elapsed:.1f}s — {msg}", exc_info=True)
        _stop_pull_heartbeat(heartbeat_stop, heartbeat)

        with _model_state_lock:
            _model_state.update({
                "state": "error",
                "message": "Model pull failed. See 'error' field.",
                "last_updated": _iso(finished),
                "finished_at": _iso(finished),
                "elapsed_seconds": elapsed,
                "error": msg,
            })
        _safe_write_progress(_progress_payload(
            "error",
            "Model pull failed. See 'error' field.",
            started_at=started,
            finished_at=finished,
            error=msg,
            now=finished,
        ))
    finally:
        _stop_pull_heartbeat(heartbeat_stop, heartbeat)


# ---------------------------------------------------------------------------
# Async generation state (browser flow)
#
# Synchronous /generate blocks until every image is rendered. On CPU that can
# take many minutes, which exceeds the Azure Container Apps HTTP ingress
# request timeout (~240s) — the connection is killed before the response
# starts, so the browser never receives the image. The endpoints below run
# generation in a background thread (like /model/pull) so the browser can poll
# for the result instead of holding one long request open.
# ---------------------------------------------------------------------------
_gen_state_lock = threading.Lock()
_gen_cancel = threading.Event()
_gen_state: dict = {
    "state": "idle",          # idle | in_progress | ready | error | cancelled
    "message": "No generation has been requested yet.",
    "results": [],
    "device": None,
    "started_at": None,
    "finished_at": None,
    "elapsed_seconds": None,
    "error": None,
    "percent": 0.0,
    "step": 0,
    "total_steps": 0,
    "image_index": 0,
    "image_count": 0,
    "phase": None,
}


def _run_generation(config: dict) -> None:
    """Background thread: run a batch and stash base64 results in _gen_state."""
    started = datetime.now(timezone.utc)
    try:
        settings = config["settings"] if isinstance(config.get("settings"), dict) else {}

        def _resolve(key, default):
            if key in config:
                return config[key]
            if key in settings:
                return settings[key]
            return default

        prompts = config.get("prompts", [])
        image_count = len(prompts)
        steps = int(_resolve("steps", 40))
        guidance = float(_resolve("guidance", 7.5))
        width = int(_resolve("width", 1024))
        height = int(_resolve("height", 1024))
        refine = _resolve("refine", False)
        use_cpu = _resolve("cpu", False)
        resolved_settings = {
            "steps": steps,
            "guidance": guidance,
            "width": width,
            "height": height,
            "refine": refine,
            "cpu": use_cpu,
        }
        device = "cpu" if use_cpu else get_device(use_cpu)
        logger.info(f"generate(async): {image_count} image(s) on device: {device}")

        current_image_index = 0
        with _gen_state_lock:
            _gen_state.update({
                "state": "in_progress",
                "message": "Generation started.",
                "results": [],
                "device": device,
                "started_at": started.isoformat(),
                "finished_at": None,
                "elapsed_seconds": None,
                "error": None,
                "percent": 0.0,
                "step": 0,
                "total_steps": steps,
                "image_index": 0,
                "image_count": image_count,
                "phase": None,
            })

        def item_callback(index: int) -> None:
            nonlocal current_image_index
            current_image_index = index
            denominator = max(1, image_count * max(1, steps))
            with _gen_state_lock:
                _gen_state.update({
                    "image_index": index,
                    "image_count": image_count,
                    "step": 0,
                    "total_steps": steps,
                    "phase": None,
                    "percent": max(0.0, min(100.0, (index * steps) / denominator * 100.0)),
                })

        def progress_callback(completed: int, total: int, phase: str) -> None:
            denominator = max(1, image_count * max(1, total))
            overall = ((current_image_index * total) + completed) / denominator * 100.0
            with _gen_state_lock:
                _gen_state.update({
                    "percent": max(0.0, min(100.0, overall)),
                    "step": completed,
                    "total_steps": total,
                    "image_index": current_image_index,
                    "image_count": image_count,
                    "phase": phase,
                })

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
            progress_callback=progress_callback,
            cancel_check=_gen_cancel.is_set,
            item_callback=item_callback,
        )

        # Enrich ok results with base64 image bytes (ephemeral cloud filesystem).
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
                logger.info(f"✅ Generated: {r.get('output')}")
            else:
                r["image_base64"] = None
                if r["status"] != "ok":
                    logger.error(f"❌ {r['status']}: {r.get('error')}")

        finished = datetime.now(timezone.utc)
        ok_count = sum(1 for r in results if r["status"] == "ok")
        try:
            _save_history(resolved_settings, prompts, results, device)
        except Exception as history_err:
            logger.warning(f"history: async save skipped: {history_err}", exc_info=True)
        with _gen_state_lock:
            _gen_state.update({
                "state": "ready",
                "message": f"Generated {ok_count}/{len(results)} image(s).",
                "results": results,
                "device": device,
                "finished_at": finished.isoformat(),
                "elapsed_seconds": (finished - started).total_seconds(),
                "error": None,
                "percent": 100.0,
                "step": steps,
                "total_steps": steps,
                "image_index": max(0, image_count - 1),
                "image_count": image_count,
                "phase": "complete",
            })

    except GenerationCancelled:
        finished = datetime.now(timezone.utc)
        logger.info("generate(async): cancelled by user")
        with _gen_state_lock:
            _gen_state.update({
                "state": "cancelled",
                "message": "Generation cancelled by user.",
                "results": [],
                "finished_at": finished.isoformat(),
                "elapsed_seconds": (finished - started).total_seconds(),
                "error": None,
            })
    except (OOMError, Exception) as exc:
        finished = datetime.now(timezone.utc)
        msg = str(exc)
        logger.error(f"generate(async): failed — {msg}", exc_info=True)
        with _gen_state_lock:
            _gen_state.update({
                "state": "error",
                "message": "Generation failed. See 'error' field.",
                "results": [],
                "finished_at": finished.isoformat(),
                "elapsed_seconds": (finished - started).total_seconds(),
                "error": msg,
            })


# SDXL's CLIP text encoders have a fixed context window of 77 tokens
# (75 content tokens + BOS/EOS). Anything past that is silently truncated by
# diffusers, so we cap prompt input at this limit and warn the user instead.
MAX_PROMPT_TOKENS = 77

HISTORY_DIR = os.environ.get("HISTORY_DIR", os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "outputs", "history"
))
HISTORY_MAX = int(os.environ.get("HISTORY_MAX", "50"))
_history_lock = threading.Lock()


def _new_history_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]


def _history_base() -> str:
    return os.path.abspath(HISTORY_DIR)


def _history_folder(hid: str) -> Optional[str]:
    if not isinstance(hid, str) or not hid or os.path.basename(hid) != hid or ".." in hid:
        return None
    base = _history_base()
    path = os.path.abspath(os.path.join(base, hid))
    try:
        if os.path.commonpath([base, path]) != base:
            return None
    except ValueError:
        return None
    return path


def _save_history(settings: dict, prompts_meta: list, results: list, device) -> str | None:
    try:
        with _history_lock:
            hid = _new_history_id()
            folder = os.path.join(HISTORY_DIR, hid)
            os.makedirs(folder, exist_ok=True)
            created_at = datetime.now(timezone.utc).isoformat()
            prompt_rows = []
            ok_count = 0

            for i, r in enumerate(results):
                prompt_meta = prompts_meta[i] if i < len(prompts_meta) and isinstance(prompts_meta[i], dict) else {}
                status = r.get("status")
                filename = None
                if status == "ok":
                    image_path = os.path.join(folder, f"{i}.png")
                    if r.get("image_base64"):
                        with open(image_path, "wb") as handle:
                            handle.write(base64.b64decode(r["image_base64"]))
                        filename = f"{i}.png"
                    elif r.get("output") and os.path.isfile(r["output"]):
                        shutil.copyfile(r["output"], image_path)
                        filename = f"{i}.png"
                    if filename:
                        ok_count += 1
                prompt_rows.append({
                    "prompt": prompt_meta.get("prompt", r.get("prompt")),
                    "negative_prompt": prompt_meta.get("negative_prompt", r.get("negative_prompt")),
                    "seed": prompt_meta.get("seed", r.get("seed")),
                    "status": status,
                    "error": r.get("error"),
                    "filename": filename,
                })

            meta = {
                "id": hid,
                "created_at": created_at,
                "device": device,
                "settings": {
                    "steps": settings.get("steps"),
                    "guidance": settings.get("guidance"),
                    "width": settings.get("width"),
                    "height": settings.get("height"),
                    "refine": settings.get("refine"),
                    "cpu": settings.get("cpu"),
                },
                "image_count": len(results),
                "ok_count": ok_count,
                "prompts": prompt_rows,
            }
            with open(os.path.join(folder, "meta.json"), "w", encoding="utf-8") as handle:
                json.dump(meta, handle, indent=2)
                handle.write("\n")
            _prune_history()
            return hid
    except Exception as exc:
        logger.warning(f"history: failed to save generation history: {exc}", exc_info=True)
        return None


def _prune_history():
    try:
        if not os.path.isdir(HISTORY_DIR):
            return
        folders = []
        for name in os.listdir(HISTORY_DIR):
            path = os.path.join(HISTORY_DIR, name)
            if os.path.isdir(path) and os.path.isfile(os.path.join(path, "meta.json")):
                folders.append((name, path))
        folders.sort(key=lambda item: item[0])
        while len(folders) > HISTORY_MAX:
            _, path = folders.pop(0)
            shutil.rmtree(path)
    except Exception as exc:
        logger.warning(f"history: failed to prune generation history: {exc}", exc_info=True)


def _list_history(limit: int) -> list:
    items = []
    try:
        if not os.path.isdir(HISTORY_DIR):
            return items
        names = sorted(os.listdir(HISTORY_DIR), reverse=True)
        for name in names:
            if len(items) >= limit:
                break
            folder = _history_folder(name)
            if not folder:
                continue
            meta_path = os.path.join(folder, "meta.json")
            if not os.path.isfile(meta_path):
                continue
            try:
                with open(meta_path, "r", encoding="utf-8") as handle:
                    meta = json.load(handle)
                items.append({
                    "id": meta.get("id"),
                    "created_at": meta.get("created_at"),
                    "device": meta.get("device"),
                    "image_count": meta.get("image_count"),
                    "ok_count": meta.get("ok_count"),
                    "prompts": [
                        {
                            "prompt": p.get("prompt"),
                            "negative_prompt": p.get("negative_prompt"),
                            "status": p.get("status"),
                            "filename": p.get("filename"),
                        }
                        for p in meta.get("prompts", [])
                        if isinstance(p, dict)
                    ],
                })
            except Exception as exc:
                logger.warning(f"history: failed to read history item {name}: {exc}")
                continue
    except Exception as exc:
        logger.warning(f"history: failed to list generation history: {exc}", exc_info=True)
    return items


def _read_meta(hid) -> dict | None:
    try:
        folder = _history_folder(hid)
        if not folder:
            return None
        meta_path = os.path.join(folder, "meta.json")
        if not os.path.isfile(meta_path):
            return None
        with open(meta_path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception as exc:
        logger.warning(f"history: failed to read history meta {hid}: {exc}")
        return None

_TOKEN_WORD_RE = re.compile(r"[A-Za-z0-9]+")
_TOKEN_PUNCT_RE = re.compile(r"[^\sA-Za-z0-9]")


def estimate_clip_tokens(text: str) -> int:
    """Approximate the CLIP token count for a prompt.

    This is a lightweight estimate (words + punctuation marks + BOS/EOS) that
    mirrors the browser-side counter, so the model never silently drops text.
    It is intentionally conservative rather than a full BPE tokenizer.
    """
    if not text:
        return 0
    words = len(_TOKEN_WORD_RE.findall(text))
    punct = len(_TOKEN_PUNCT_RE.findall(text))
    return words + punct + 2  # +2 for the BOS/EOS special tokens


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

        est = estimate_clip_tokens(prompt_obj["prompt"])
        if est > MAX_PROMPT_TOKENS:
            return (
                f"Prompt at index {i} is too long (~{est} tokens). "
                f"SDXL's CLIP text encoder only reads the first {MAX_PROMPT_TOKENS} "
                f"tokens; shorten the prompt so nothing is silently dropped."
            )

        neg = prompt_obj.get("negative_prompt")
        if isinstance(neg, str) and neg:
            neg_est = estimate_clip_tokens(neg)
            if neg_est > MAX_PROMPT_TOKENS:
                return (
                    f"Negative prompt at index {i} is too long (~{neg_est} tokens). "
                    f"Keep it under the {MAX_PROMPT_TOKENS}-token CLIP limit."
                )
    
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
        resolved_settings = {
            "steps": steps,
            "guidance": guidance,
            "width": width,
            "height": height,
            "refine": refine,
            "cpu": use_cpu,
        }
        
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

        try:
            _save_history(resolved_settings, prompts, results, device)
        except Exception as history_err:
            logger.warning(f"history: sync save skipped: {history_err}", exc_info=True)

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
        { "state": "in_progress"|"ready"|"error"|"not_started"|"stalled",
          "message": "...", "timestamp": "..." }
    """
    global _model_pull_thread

    body = request.get_json(silent=True) or {}
    force = bool(body.get("force", False))
    active_worker = _is_model_pull_worker_alive()
    reconciled = reconcile_model_status(active_worker=active_worker)

    with _model_state_lock:
        current_state = reconciled["state"]

        if active_worker or (current_state == "in_progress" and not force):
            reconciled["message"] = "Pull already in progress."
            return jsonify(reconciled), 202

        if current_state == "ready" and not force:
            reconciled["message"] = (
                "Model is already cached. Pass {\"force\": true} to re-pull."
            )
            return jsonify(reconciled), 200

        # Transition to in_progress synchronously (inside the lock) so a
        # concurrent caller sees the right state before the thread starts.
        started = _utc_now()
        bytes_downloaded = _safe_dir_size(get_base_model_cache_path())
        _model_state.update({
            "state": "in_progress",
            "message": "Pull started.",
            "bytes_downloaded": bytes_downloaded,
            "bytes_expected": BASE_MODEL_EXPECTED_BYTES,
            "percent": _calculate_percent(
                bytes_downloaded, BASE_MODEL_EXPECTED_BYTES
            ),
            "started_at": _iso(started),
            "last_updated": _iso(started),
            "finished_at": None,
            "elapsed_seconds": None,
            "error": None,
            "cache_path": get_base_model_cache_path(),
            "revision": os.environ.get("SDXL_MODEL_REVISION") or None,
        })

    _safe_write_progress(_progress_payload(
        "in_progress",
        "Pull started.",
        started_at=started,
        now=started,
    ))
    thread = threading.Thread(target=_pull_worker, daemon=True, name="model-pull")
    with _model_state_lock:
        _model_pull_thread = thread
    thread.start()
    logger.info("model/pull: background thread launched")

    snapshot = reconcile_model_status(active_worker=True)
    snapshot["message"] = "Model pull started in background. Poll GET /model/status."
    return jsonify(snapshot), 202


@app.route("/model/status", methods=["GET"])
def model_status():
    """
    GET /model/status

    Returns the current model warm-up state.

    Response (always HTTP 200):
        { "state": "not_started"|"in_progress"|"ready"|"error"|"stalled",
          "message": "...",
          "bytes_downloaded": <number>,
          "bytes_expected": <number|null>,
          "percent": <number|null>,
          "weights_present": <boolean>,
          "started_at": "<ISO8601 or null>",
          "last_updated": "<ISO8601 or null>",
          "finished_at": "<ISO8601 or null>",
          "elapsed_seconds": <number or null>,
          "error": "<string or null>",
          "cache_path": "<string>",
          "revision": "<string or null>",
          "device": "cuda|mps|cpu",
          "timestamp": "<ISO8601>" }
    """
    snapshot = reconcile_model_status(active_worker=_is_model_pull_worker_alive())
    snapshot["device"] = get_device()

    return jsonify(snapshot), 200


@app.route("/generate/async", methods=["POST"])
def generate_async():
    """
    POST /generate/async

    Same request body as POST /generate, but runs generation in a background
    thread and returns 202 immediately. Poll GET /generate/status for the
    result. Use this from browsers / over Azure Container Apps ingress, where
    a long synchronous /generate would exceed the request timeout.

    Response (202): { "state": "in_progress", "message": "...", "timestamp": "..." }
    """
    config = request.get_json(silent=True)
    if config is None:
        return jsonify({
            "state": "error",
            "error": "Request body must be valid JSON",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }), 400

    validation_error = validate_batch_config(config)
    if validation_error:
        return jsonify({
            "state": "error",
            "error": validation_error,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }), 400

    with _gen_state_lock:
        if _gen_state["state"] == "in_progress":
            return jsonify({
                "state": "in_progress",
                "message": "A generation is already in progress. Poll GET /generate/status.",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }), 202

        _gen_cancel.clear()
        _gen_state.update({
            "state": "in_progress",
            "message": "Generation started.",
            "results": [],
            "device": None,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "finished_at": None,
            "elapsed_seconds": None,
            "error": None,
            "percent": 0.0,
            "step": 0,
            "total_steps": 0,
            "image_index": 0,
            "image_count": len(config.get("prompts", [])),
            "phase": None,
        })

    threading.Thread(
        target=_run_generation, args=(config,), daemon=True, name="generate"
    ).start()
    logger.info("generate/async: background thread launched")

    return jsonify({
        "state": "in_progress",
        "message": "Generation started in background. Poll GET /generate/status.",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }), 202


@app.route("/generate/cancel", methods=["POST"])
def generate_cancel():
    """Request cooperative cancellation of the current async generation."""
    _gen_cancel.set()
    with _gen_state_lock:
        running = _gen_state.get("state") == "in_progress"
    return jsonify({
        "state": "cancelling" if running else "idle",
        "message": "Generation cancellation requested." if running else "No generation is running.",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }), 202


@app.route("/generate/status", methods=["GET"])
def generate_status():
    """
    GET /generate/status

    Returns the current async-generation state. When state is "ready", the
    "results" array carries the same per-image objects as POST /generate,
    including "image_base64" for download.

    Response (always HTTP 200):
        { "state": "idle"|"in_progress"|"ready"|"error"|"cancelled",
          "message": "...", "results": [...], "device": "...",
          "percent": <number>, "step": <number>, "total_steps": <number>,
          "image_index": <number>, "image_count": <number>, "phase": "...",
          "started_at": "...", "finished_at": "...",
          "elapsed_seconds": <number|null>, "error": "<string|null>",
          "timestamp": "..." }
    """
    with _gen_state_lock:
        snapshot = dict(_gen_state)

    snapshot["timestamp"] = datetime.now(timezone.utc).isoformat()
    if snapshot["state"] != "error":
        snapshot.pop("error", None)

    return jsonify(snapshot), 200


@app.route("/history", methods=["GET"])
def history_list():
    limit_raw = request.args.get("limit", str(HISTORY_MAX))
    try:
        limit = max(0, min(200, int(limit_raw)))
    except ValueError:
        limit = max(0, min(200, HISTORY_MAX))
    items = _list_history(limit)
    return jsonify({
        "status": "success",
        "items": items,
        "count": len(items),
    }), 200


@app.route("/history/<hid>", methods=["GET"])
def history_detail(hid):
    meta = _read_meta(hid)
    if meta is None:
        abort(404)
    return jsonify(meta), 200


@app.route("/history/<hid>/image/<int:n>", methods=["GET"])
def history_image(hid, n):
    try:
        folder = _history_folder(hid)
        if not folder:
            abort(404)
        base = _history_base()
        path = os.path.abspath(os.path.join(folder, f"{n}.png"))
        if os.path.commonpath([base, path]) != base:
            abort(404)
        if not os.path.isfile(path):
            abort(404)
        return send_file(path, mimetype="image/png")
    except ValueError:
        abort(404)
    except OSError as exc:
        logger.warning(f"history: failed to serve image {hid}/{n}: {exc}")
        abort(404)


@app.route("/history/<hid>", methods=["DELETE"])
def history_delete(hid):
    folder = _history_folder(hid)
    if not folder or not os.path.isdir(folder):
        abort(404)
    with _history_lock:
        if not os.path.isdir(folder):
            abort(404)
        try:
            shutil.rmtree(folder)
        except Exception as exc:
            logger.warning(f"history: failed to delete history item {hid}: {exc}", exc_info=True)
            abort(404)
    return jsonify({
        "status": "success",
        "deleted": hid,
    }), 200


@app.route("/history", methods=["DELETE"])
def history_clear():
    cleared = 0
    try:
        with _history_lock:
            if os.path.isdir(HISTORY_DIR):
                for name in os.listdir(HISTORY_DIR):
                    folder = _history_folder(name)
                    if folder and os.path.isdir(folder):
                        shutil.rmtree(folder)
                        cleared += 1
    except Exception as exc:
        logger.warning(f"history: failed to clear history: {exc}", exc_info=True)
    return jsonify({
        "status": "success",
        "cleared": cleared,
    }), 200


@app.route("/", methods=["GET"])
@app.route("/ui", methods=["GET"])
def browser_ui():
    """Serve a self-contained browser console — one section per endpoint.

    Available at both `/` (root) and `/ui`. The JSON API index lives at `/api`.
    """
    html = (
        '<!DOCTYPE html>\n'
        '<html lang="en">\n'
        '<head>\n'
        '<meta charset="UTF-8">\n'
        '<meta name="viewport" content="width=device-width,initial-scale=1">\n'
        '<title>SDXL API Console</title>\n'
        '<style>\n'
        '*{box-sizing:border-box;margin:0;padding:0}\n'
        'body{font-family:system-ui,sans-serif;background:#0f0f13;color:#e2e2e6;padding:1.5rem 1.5rem 3rem}\n'
        'h1{font-size:1.5rem;font-weight:700;color:#a78bfa;margin-bottom:.4rem}\n'
        '.subtitle{font-size:.8rem;color:#52525b;margin-bottom:1.5rem}\n'
        '.card{background:#1a1a24;border:1px solid #2d2d40;border-radius:10px;padding:1.25rem;margin-bottom:1.25rem}\n'
        '.card-header{display:flex;align-items:center;gap:.75rem;flex-wrap:wrap;margin-bottom:.9rem}\n'
        '.method{font-size:.7rem;font-weight:700;padding:.2rem .5rem;border-radius:4px;text-transform:uppercase;letter-spacing:.05em}\n'
        '.get{background:#14532d;color:#4ade80}.post{background:#1e3a5f;color:#60a5fa}\n'
        '.path{font-family:monospace;font-size:.9rem;color:#e2e2e6;font-weight:600}\n'
        '.desc{font-size:.78rem;color:#71717a;flex:1}\n'
        '.controls{display:flex;gap:.5rem;align-items:center;flex-wrap:wrap;margin-bottom:.75rem}\n'
        'button{background:#7c3aed;color:#fff;border:none;border-radius:6px;padding:.4rem .85rem;font-size:.82rem;cursor:pointer;font-weight:600;transition:background .15s}\n'
        'button:hover{background:#6d28d9}\n'
        'button:disabled{background:#3f3f46;color:#71717a;cursor:not-allowed}\n'
        'button.sec{background:#27272a;color:#e2e2e6}\n'
        'button.sec:hover{background:#3f3f46}\n'
        '.toggle-label{font-size:.78rem;color:#a1a1aa;display:flex;align-items:center;gap:.35rem;cursor:pointer;user-select:none}\n'
        '.toggle-label input{width:auto;cursor:pointer}\n'
        '.field{margin-bottom:.65rem}\n'
        'label{display:block;font-size:.78rem;font-weight:500;color:#a1a1aa;margin-bottom:.2rem}\n'
        'input[type=text],input[type=number],select,textarea{width:100%;background:#0f0f13;border:1px solid #3f3f46;border-radius:6px;color:#e2e2e6;padding:.38rem .6rem;font-size:.83rem;font-family:inherit}\n'
        'input:focus,select:focus,textarea:focus{outline:none;border-color:#7c3aed}\n'
        'textarea{resize:vertical;min-height:110px}\n'
        '.grid2{display:grid;grid-template-columns:1fr 1fr;gap:.65rem}\n'
        '.grid3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:.65rem}\n'
        '.cbrow{display:flex;align-items:center;gap:.4rem;font-size:.82rem;color:#a1a1aa}\n'
        '.cbrow input{width:auto}\n'
        '.spinner{display:inline-block;width:14px;height:14px;border:2px solid #7c3aed;border-top-color:transparent;border-radius:50%;animation:spin .7s linear infinite;vertical-align:middle;margin-right:.3rem}\n'
        '@keyframes spin{to{transform:rotate(360deg)}}\n'
        '.hidden{display:none !important}\n'
        '.badge{display:inline-block;padding:.15rem .55rem;border-radius:20px;font-size:.72rem;font-weight:700;text-transform:uppercase}\n'
        '.ok{background:#14532d;color:#4ade80}.info{background:#1e3a5f;color:#60a5fa}\n'
        '.err{background:#450a0a;color:#f87171}.grey{background:#27272a;color:#a1a1aa}\n'
        '.warn{background:#713f12;color:#fbbf24}\n'
        '.response-wrap{margin-top:.75rem}\n'
        'details{border:1px solid #2d2d40;border-radius:6px;overflow:hidden}\n'
        'summary{padding:.4rem .7rem;font-size:.78rem;font-weight:600;color:#94a3b8;cursor:pointer;background:#111118;list-style:none;display:flex;align-items:center;gap:.5rem}\n'
        'summary::-webkit-details-marker{display:none}\n'
        'summary::before{content:"\\25B6";font-size:.6rem;transition:transform .15s}\n'
        'details[open] summary::before{transform:rotate(90deg)}\n'
        'pre{background:#0a0a0f;color:#a5f3fc;font-size:.75rem;padding:.75rem;overflow-x:auto;max-height:320px;white-space:pre-wrap;word-break:break-all}\n'
        '.status-row{display:flex;align-items:center;gap:.5rem;flex-wrap:wrap;font-size:.83rem;margin-bottom:.3rem}\n'
        '.elapsed{font-size:.72rem;color:#71717a}\n'
        '.err-box{background:#450a0a;border:1px solid #7f1d1d;border-radius:6px;padding:.6rem;font-size:.82rem;color:#fca5a5;margin-top:.4rem}\n'
        '.progress{width:100%;height:3px;background:#27272a;border-radius:2px;margin-top:.5rem}\n'
        '.progress-fill{height:100%;background:#7c3aed;border-radius:2px;width:0;transition:width .2s}\n'
        '.progress-fill.pulse{animation:pulse-width 2s ease-in-out infinite}\n'
        '@keyframes pulse-width{0%{width:15%}50%{width:75%}100%{width:15%}}\n'
        '.tabs{display:flex;border-bottom:1px solid #2d2d40;margin-bottom:.75rem}\n'
        '.tab{padding:.4rem .9rem;cursor:pointer;font-size:.82rem;font-weight:500;color:#71717a;border-bottom:2px solid transparent;margin-bottom:-1px}\n'
        '.tab.on{color:#a78bfa;border-bottom-color:#a78bfa}\n'
        '.tp{display:none}.tp.on{display:block}\n'
        '.img-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:.9rem;margin-top:.9rem}\n'
        '.img-card{background:#111118;border:1px solid #2d2d40;border-radius:8px;overflow:hidden}\n'
        '.img-card img{width:100%;display:block}\n'
        '.img-meta{padding:.55rem;display:flex;flex-direction:column;gap:.3rem}\n'
        '.img-meta p{font-size:.72rem;color:#71717a;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}\n'
        'a.dl{display:inline-block;padding:.25rem .55rem;background:#27272a;color:#a78bfa;border-radius:4px;font-size:.72rem;text-decoration:none;font-weight:600}\n'
        'a.dl:hover{background:#3f3f46}\n'
        '.hint{font-size:.72rem;color:#52525b;margin-top:.2rem}\n'
        '.tok-count{display:block;font-size:.7rem;color:#52525b;margin-top:.2rem;text-align:right}\n'
        '.tok-count.over{color:#f87171;font-weight:600}\n'
        '#hist-list{max-height:420px;overflow-y:auto}\n'
        '.hist-item{background:#111118;border:1px solid #2d2d40;border-radius:8px;padding:.75rem;margin-bottom:.65rem}\n'
        '.hist-meta{display:flex;align-items:center;gap:.5rem;flex-wrap:wrap;font-size:.76rem;color:#71717a;margin-bottom:.35rem}\n'
        '.hist-prompt{font-size:.82rem;color:#d4d4d8;margin-bottom:.5rem;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}\n'
        '.hist-thumbs{display:flex;gap:.45rem;flex-wrap:wrap}\n'
        '.hist-thumb{width:96px;height:96px;object-fit:cover;border-radius:6px;border:1px solid #2d2d40;cursor:pointer}\n'
        'hr{border:none;border-top:1px solid #1f1f2e;margin:.3rem 0 .7rem}\n'
        '</style>\n'
        '</head>\n'
        '<body>\n'
        '<h1>SDXL API Console</h1>\n'
        '<p class="subtitle">One section per endpoint. Every call shows the raw JSON response.</p>\n'
        '\n'
        '<!-- 1. GET /api -->\n'
        '<div class="card">\n'
        '  <div class="card-header">\n'
        '    <span class="method get">GET</span><span class="path">/api</span>\n'
        '    <span class="desc">API info &amp; endpoint listing</span>\n'
        '  </div>\n'
        '  <div class="controls">\n'
        '    <button onclick="callRoot()">Get API info</button>\n'
        '    <span id="root-spin" class="hidden"><span class="spinner"></span></span>\n'
        '  </div>\n'
        '  <div id="root-resp" class="response-wrap hidden">\n'
        '    <details open><summary>JSON response</summary><pre id="root-pre"></pre></details>\n'
        '  </div>\n'
        '</div>\n'
        '\n'
        '<!-- 2. GET /health -->\n'
        '<div class="card">\n'
        '  <div class="card-header">\n'
        '    <span class="method get">GET</span><span class="path">/health</span>\n'
        '    <span class="desc">Server health &amp; device</span>\n'
        '  </div>\n'
        '  <div class="controls">\n'
        '    <button onclick="callHealth()">Check health</button>\n'
        '    <span id="health-spin" class="hidden"><span class="spinner"></span></span>\n'
        '    <label class="toggle-label"><input type="checkbox" id="health-auto" onchange="toggleHealthPoll()"> auto-poll every 10 s</label>\n'
        '  </div>\n'
        '  <div id="health-result" class="hidden">\n'
        '    <div class="status-row">\n'
        '      <span class="badge grey" id="health-badge">—</span>\n'
        '      <span id="health-device" style="font-size:.82rem;color:#94a3b8"></span>\n'
        '      <span class="elapsed" id="health-ts"></span>\n'
        '    </div>\n'
        '    <details><summary>JSON response</summary><pre id="health-pre"></pre></details>\n'
        '  </div>\n'
        '</div>\n'
        '\n'
        '<!-- 3. GET /model/status -->\n'
        '<div class="card">\n'
        '  <div class="card-header">\n'
        '    <span class="method get">GET</span><span class="path">/model/status</span>\n'
        '    <span class="desc">Current model warm-up state</span>\n'
        '  </div>\n'
        '  <div class="controls">\n'
        '    <button onclick="callModelStatus()">Check model status</button>\n'
        '    <span id="mstatus-spin" class="hidden"><span class="spinner"></span></span>\n'
        '    <label class="toggle-label"><input type="checkbox" id="mstatus-auto" onchange="toggleMStatusPoll()"> auto-poll every 5 s</label>\n'
        '  </div>\n'
        '  <div id="mstatus-result" class="hidden">\n'
        '    <div class="status-row">\n'
        '      <span class="badge grey" id="mstatus-badge">—</span>\n'
        '      <span id="mstatus-msg" style="font-size:.82rem;color:#94a3b8"></span>\n'
        '      <span class="elapsed" id="mstatus-elapsed"></span>\n'
        '    </div>\n'
        '    <div id="mstatus-progress" class="progress hidden"><div id="mstatus-progress-fill" class="progress-fill"></div></div>\n'
        '    <div id="mstatus-progress-text" class="elapsed hidden" style="margin-top:.35rem"></div>\n'
        '    <div id="mstatus-err" class="err-box hidden"></div>\n'
        '    <details><summary>JSON response</summary><pre id="mstatus-pre"></pre></details>\n'
        '  </div>\n'
        '</div>\n'
        '\n'
        '<!-- 4. POST /model/pull -->\n'
        '<div class="card">\n'
        '  <div class="card-header">\n'
        '    <span class="method post">POST</span><span class="path">/model/pull</span>\n'
        '    <span class="desc">Start async model download &amp; warm-up (returns 202)</span>\n'
        '  </div>\n'
        '  <div class="field">\n'
        '    <label class="cbrow"><input type="checkbox" id="pull-force"> <span>force re-pull even if already ready</span></label>\n'
        '  </div>\n'
        '  <div class="controls">\n'
        '    <button onclick="callModelPull()" id="btn-pull">Pull / warm up model</button>\n'
        '    <span id="pull-spin" class="hidden"><span class="spinner"></span>Working…</span>\n'
        '  </div>\n'
        '  <div id="pull-result" class="hidden">\n'
        '    <div class="status-row">\n'
        '      <span class="badge grey" id="pull-badge">—</span>\n'
        '      <span id="pull-msg" style="font-size:.82rem;color:#94a3b8"></span>\n'
        '      <span class="elapsed" id="pull-elapsed"></span>\n'
        '    </div>\n'
        '    <div id="pull-progress" class="progress hidden"><div id="pull-progress-fill" class="progress-fill pulse"></div></div>\n'
        '    <div id="pull-progress-text" class="elapsed hidden" style="margin-top:.35rem"></div>\n'
        '    <div id="pull-poll-status" class="status-row hidden" style="margin-top:.5rem">\n'
        '      <span class="spinner"></span>\n'
        '      <span style="font-size:.78rem;color:#71717a">Polling /model/status…</span>\n'
        '      <span class="elapsed" id="pull-poll-elapsed"></span>\n'
        '      <button class="sec" onclick="stopPullPoll()">Stop polling</button>\n'
        '    </div>\n'
        '    <div id="pull-err" class="err-box hidden"></div>\n'
        '    <details><summary>JSON response (pull)</summary><pre id="pull-pre"></pre></details>\n'
        '    <details id="pull-status-details" class="hidden" style="margin-top:.4rem"><summary>JSON response (latest status poll)</summary><pre id="pull-status-pre"></pre></details>\n'
        '  </div>\n'
        '</div>\n'
        '\n'
        '<!-- 5. POST /generate -->\n'
        '<div class="card">\n'
        '  <div class="card-header">\n'
        '    <span class="method post">POST</span><span class="path">/generate</span>\n'
        '    <span class="desc">Generate images from prompts</span>\n'
        '  </div>\n'
        '  <p class="hint" style="margin-bottom:.75rem">Tip: Steps=20, 512x512 for a fast CPU smoke-test. Generation can take several minutes on CPU.</p>\n'
        '\n'
        '  <div class="tabs">\n'
        '    <div class="tab on" id="tab-prompt" onclick="switchTab(\'prompt\')">Free-text prompt</div>\n'
        '    <div class="tab" id="tab-batch" onclick="switchTab(\'batch\')">Batch JSON</div>\n'
        '  </div>\n'
        '\n'
        '  <div class="tp on" id="tp-prompt">\n'
        '    <div class="field"><label>Prompt *</label><input type="text" id="p-prompt" placeholder="a serene mountain lake at sunrise, photorealistic" oninput="updateTokCount()"><span class="tok-count" id="p-prompt-tok">0 / 77 tokens</span></div>\n'
        '    <div class="field"><label>Negative prompt</label><input type="text" id="p-neg" placeholder="blur, noise, cartoon" oninput="updateTokCount()"><span class="tok-count" id="p-neg-tok">0 / 77 tokens</span></div>\n'
        '    <div class="grid3" style="margin-bottom:.65rem">\n'
        '      <div class="field"><label>Steps (1-150)</label><input type="number" id="p-steps" value="20" min="1" max="150"></div>\n'
        '      <div class="field"><label>Guidance (0-50)</label><input type="number" id="p-guidance" value="7.5" min="0" max="50" step="0.5"></div>\n'
        '      <div class="field"><label>Seed (blank=random)</label><input type="number" id="p-seed" placeholder="42"></div>\n'
        '    </div>\n'
        '    <div class="grid2" style="margin-bottom:.65rem">\n'
        '      <div class="field"><label>Width</label><select id="p-width"><option value="512" selected>512</option><option value="768">768</option><option value="1024">1024</option></select></div>\n'
        '      <div class="field"><label>Height</label><select id="p-height"><option value="512" selected>512</option><option value="768">768</option><option value="1024">1024</option></select></div>\n'
        '    </div>\n'
        '    <div style="display:flex;gap:1.2rem;flex-wrap:wrap;margin-bottom:.5rem">\n'
        '      <label class="cbrow"><input type="checkbox" id="p-refine"> Use refiner</label>\n'
        '      <label class="cbrow"><input type="checkbox" id="p-cpu" checked> Force CPU</label>\n'
        '    </div>\n'
        '  </div>\n'
        '\n'
        '  <div class="tp" id="tp-batch">\n'
        '    <div class="field">\n'
        '      <label>Upload batch JSON file</label>\n'
        '      <input type="file" id="b-file" accept=".json,application/json" style="background:transparent;border:none;padding:0;color:#94a3b8">\n'
        '    </div>\n'
        '    <div class="field">\n'
        '      <label>Or paste / edit batch JSON</label>\n'
        '      <textarea id="b-json" placeholder=\'{"prompts":[{"prompt":"a tropical sunset","seed":42}],"steps":20,"width":512,"height":512}\'></textarea>\n'
        '      <p class="hint">Shape: {"prompts":[{"prompt":"...","seed":42}],"steps":20,"width":512,"height":512}</p>\n'
        '    </div>\n'
        '  </div>\n'
        '\n'
        '  <div class="controls" style="margin-top:.5rem">\n'
        '    <button onclick="callGenerate()" id="btn-gen">Generate</button>\n'
        '    <button class="sec" onclick="callGenerateCancel()" id="btn-gen-stop" disabled>Stop</button>\n'
        '    <span id="gen-spin" class="hidden"><span class="spinner"></span>Generating… (may take minutes on CPU)</span>\n'
        '    <button class="sec hidden" id="btn-dl-all" onclick="downloadAll()">Download all images</button>\n'
        '  </div>\n'
        '  <div id="gen-err" class="err-box hidden"></div>\n'
        '  <div id="gen-status" class="status-row hidden">\n'
        '    <span class="badge grey" id="gen-badge">—</span>\n'
        '    <span id="gen-msg" style="font-size:.82rem;color:#94a3b8"></span>\n'
        '  </div>\n'
        '  <div id="gen-progress" class="progress hidden"><div id="gen-progress-fill" class="progress-fill pulse"></div></div>\n'
        '  <div id="gen-progress-text" class="elapsed hidden" style="margin-top:.35rem"></div>\n'
        '\n'
        '  <div id="gen-resp" class="response-wrap hidden">\n'
        '    <details><summary id="gen-resp-label">JSON response</summary><pre id="gen-pre"></pre></details>\n'
        '  </div>\n'
        '  <div id="gen-imgs" class="img-grid"></div>\n'

        '</div>\n'
        '\n'
        '<!-- 6. GET /history -->\n'
        '<div class="card">\n'
        '  <div class="card-header">\n'
        '    <span class="method get">GET</span><span class="path">/history</span>\n'
        '    <span class="desc">Recent server-side generation history</span>\n'
        '  </div>\n'
        '  <div class="controls">\n'
        '    <button onclick="loadHistory()">Refresh</button>\n'
        '    <button class="sec" onclick="clearHistory()">Clear all</button>\n'
        '  </div>\n'
        '  <div id="hist-list"></div>\n'
        '</div>\n'
        '\n'
        '<script>\n'
        'const B="";\n'
        '\n'
        '// helpers\n'
        'function bc(s){if(s==="healthy"||s==="ready"||s==="ok")return"ok";if(s==="in_progress")return"info";if(s==="error")return"err";if(s==="not_started")return"grey";if(s==="stalled")return"warn";if(s==="cancelled")return"grey";return"warn";}\n'
        'function badge(el,txt,cls){el.textContent=txt;el.className="badge "+cls;}\n'
        'function show(id){document.getElementById(id).classList.remove("hidden");}\n'
        'function hide(id){document.getElementById(id).classList.add("hidden");}\n'
        'function estClipTokens(t){if(!t)return 0;t=t.trim();if(!t)return 0;const w=(t.match(/[A-Za-z0-9]+/g)||[]).length;const p=(t.match(/[^\\sA-Za-z0-9]/g)||[]).length;return w+p+2;}\n'
        'function updateTokCount(){[["p-prompt","p-prompt-tok"],["p-neg","p-neg-tok"]].forEach(function(pair){const inp=document.getElementById(pair[0]);const out=document.getElementById(pair[1]);if(!inp||!out)return;const n=estClipTokens(inp.value);out.textContent=n+" / 77 tokens";out.classList.toggle("over",n>77);});}\n'
        'function setText(id,t){document.getElementById(id).textContent=t;}\n'
        'function setJson(id,obj){\n'
        '  // strip image_base64 from display to keep pre readable\n'
        '  const clone=JSON.parse(JSON.stringify(obj));\n'
        '  if(clone.results)clone.results=clone.results.map(r=>{const c={...r};if(c.image_base64)c.image_base64="<base64 omitted>";return c;});\n'
        '  document.getElementById(id).textContent=JSON.stringify(clone,null,2);\n'
        '}\n'
        'function fmtBytes(n){\n'
        '  if(n==null)return"?";\n'
        '  const u=["B","KB","MB","GB","TB"];let v=Number(n);let i=0;\n'
        '  while(v>=1024&&i<u.length-1){v/=1024;i++;}\n'
        '  return (i===0?v.toFixed(0):v.toFixed(1))+" "+u[i];\n'
        '}\n'
        'function modelMeta(d){\n'
        '  const parts=[];\n'
        '  if(d.elapsed_seconds!=null)parts.push("elapsed: "+Number(d.elapsed_seconds).toFixed(1)+"s");\n'
        '  if(d.last_updated)parts.push("updated: "+new Date(d.last_updated).toLocaleTimeString());\n'
        '  if(d.cache_path)parts.push("cache: "+d.cache_path);\n'
        '  return parts.join(" · ");\n'
        '}\n'
        'function renderProgress(prefix,d){\n'
        '  const bar=document.getElementById(prefix+"-progress");\n'
        '  const fill=document.getElementById(prefix+"-progress-fill");\n'
        '  const txt=document.getElementById(prefix+"-progress-text");\n'
        '  if(!bar||!fill||!txt)return;\n'
        '  const state=d.state||"unknown";\n'
        '  // The progress bar is shown ONLY during an active download. Every other\n'
        '  // state (ready, not_started, error, stalled, unknown) hides the bar and\n'
        '  // clears the pulse, so an idle/ready card never shows a leftover bar.\n'
        '  if(state!=="in_progress"){\n'
        '    bar.classList.add("hidden");txt.classList.add("hidden");\n'
        '    fill.classList.remove("pulse");fill.style.width="0%";\n'
        '    return;\n'
        '  }\n'
        '  bar.classList.remove("hidden");txt.classList.remove("hidden");\n'
        '  const pct=d.percent!=null?Number(d.percent):null;\n'
        '  if(pct!=null){fill.classList.remove("pulse");fill.style.width=Math.max(0,Math.min(100,pct))+"%";}\n'
        '  else{fill.classList.add("pulse");fill.style.width="35%";}\n'
        '  const label=(pct!=null?pct.toFixed(1)+"% · ":"")+"≈"+fmtBytes(d.bytes_downloaded)+" downloaded"+(d.bytes_expected?" / "+fmtBytes(d.bytes_expected):"");\n'
        '  txt.textContent=label+(d.last_updated?" · updated "+new Date(d.last_updated).toLocaleTimeString():"");\n'
        '}\n'
        'function renderModelState(d){\n'
        '  const s=d.state||"unknown";\n'
        '  ["mstatus","pull"].forEach(prefix=>{\n'
        '    const result=document.getElementById(prefix+"-result");if(result)result.classList.remove("hidden");\n'
        '    badge(document.getElementById(prefix+"-badge"),s,bc(s));\n'
        '    setText(prefix+"-msg",d.message||"");\n'
        '    const elapsed=document.getElementById(prefix+"-elapsed");if(elapsed)elapsed.textContent=modelMeta(d);\n'
        '    renderProgress(prefix,d);\n'
        '    const err=document.getElementById(prefix+"-err");\n'
        '    if(err){if(s==="error"&&d.error){err.textContent=d.error;err.classList.remove("hidden");}else{err.classList.add("hidden");}}\n'
        '  });\n'
        '  // Self-heal: the polling spinner may only be visible while a download is\n'
        '  // actually in progress. Any render that sees a terminal/idle state stops\n'
        '  // the poll timer and hides the spinner so it can never get stuck.\n'
        '  if(s!=="in_progress"){stopPullPoll(true);}\n'
        '}\n'
        '\n'
        '// ── 1. GET /api ──────────────────────────────────────────────────\n'
        'async function callRoot(){\n'
        '  show("root-spin");hide("root-resp");\n'
        '  try{\n'
        '    const r=await fetch(B+"/api");const d=await r.json();\n'
        '    setJson("root-pre",d);show("root-resp");\n'
        '  }catch(e){alert("Error: "+e);}\n'
        '  finally{hide("root-spin");}\n'
        '}\n'
        '\n'
        '// ── 2. GET /health ──────────────────────────────────────────────────\n'
        'let _hTimer=null;\n'
        'async function callHealth(){\n'
        '  show("health-spin");\n'
        '  try{\n'
        '    const r=await fetch(B+"/health");const d=await r.json();\n'
        '    badge(document.getElementById("health-badge"),d.status||"?",bc(d.status));\n'
        '    setText("health-device",d.device?"device: "+d.device:"");\n'
        '    setText("health-ts",d.timestamp?new Date(d.timestamp).toLocaleTimeString():"");\n'
        '    setJson("health-pre",d);show("health-result");\n'
        '  }catch(e){\n'
        '    badge(document.getElementById("health-badge"),"unreachable","err");\n'
        '    setText("health-device","");setText("health-ts","");\n'
        '  }finally{hide("health-spin");}\n'
        '}\n'
        'function toggleHealthPoll(){\n'
        '  if(document.getElementById("health-auto").checked){\n'
        '    callHealth();_hTimer=setInterval(callHealth,10000);\n'
        '  }else{clearInterval(_hTimer);_hTimer=null;}\n'
        '}\n'
        '\n'
        '// ── 3. GET /model/status ────────────────────────────────────────────\n'
        'let _msTimer=null;\n'
        'async function callModelStatus(){\n'
        '  show("mstatus-spin");\n'
        '  try{\n'
        '    const r=await fetch(B+"/model/status");const d=await r.json();\n'
        '    renderModelState(d);\n'
        '    setJson("mstatus-pre",d);show("mstatus-result");\n'
        '  }catch(e){setText("mstatus-msg","fetch error: "+e);}\n'
        '  finally{hide("mstatus-spin");}\n'
        '}\n'
        'function toggleMStatusPoll(){\n'
        '  if(document.getElementById("mstatus-auto").checked){\n'
        '    callModelStatus();_msTimer=setInterval(callModelStatus,5000);\n'
        '  }else{clearInterval(_msTimer);_msTimer=null;}\n'
        '}\n'
        '\n'
        '// ── 4. POST /model/pull ─────────────────────────────────────────────\n'
        'let _pullPollTimer=null;\n'
        'let _pullStart=null;\n'
        'async function callModelPull(){\n'
        '  const force=document.getElementById("pull-force").checked;\n'
        '  document.getElementById("btn-pull").disabled=true;\n'
        '  show("pull-spin");hide("pull-err");hide("pull-status-details");\n'
        '  show("pull-result");\n'
        '  try{\n'
        '    const r=await fetch(B+"/model/pull",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({force:force})});\n'
        '    const d=await r.json();\n'
        '    setJson("pull-pre",d);\n'
        '    renderModelState(d);\n'
        '    if(d.state==="in_progress"){startPullPoll();}\n'
        '  }catch(e){setText("pull-err","Request failed: "+e);show("pull-err");}\n'
        '  finally{hide("pull-spin");document.getElementById("btn-pull").disabled=false;}\n'
        '}\n'
        'function startPullPoll(){\n'
        '  _pullStart=Date.now();show("pull-poll-status");\n'
        '  if(!_pullPollTimer)_pullPollTimer=setInterval(doPullPoll,5000);\n'
        '  doPullPoll();\n'
        '}\n'
        'function stopPullPoll(silent){\n'
        '  if(_pullPollTimer){clearInterval(_pullPollTimer);_pullPollTimer=null;}\n'
        '  hide("pull-poll-status");\n'
        '  if(!silent){\n'
        '    const pf=document.getElementById("pull-progress-fill");if(pf){pf.classList.remove("pulse");}\n'
        '    const pt=document.getElementById("pull-progress-text");if(pt){pt.classList.remove("hidden");pt.textContent="Polling paused — the model may still be downloading in the background. Click Pull / warm up model to resume.";}\n'
        '  }\n'
        '}\n'
        'async function doPullPoll(){\n'
        '  const elapsed=_pullStart?((Date.now()-_pullStart)/1000).toFixed(0)+"s":"?";\n'
        '  setText("pull-poll-elapsed",elapsed);\n'
        '  try{\n'
        '    const r=await fetch(B+"/model/status");const d=await r.json();\n'
        '    const s=d.state||"unknown";\n'
        '    renderModelState(d);\n'
        '    setJson("pull-status-pre",d);show("pull-status-details");\n'
        '    if(s==="ready"||s==="error"||s==="not_started"||s==="stalled"){stopPullPoll(true);}\n'
        '  }catch(e){/* keep polling */}\n'
        '}\n'
        '\n'
        '// ── 5. POST /generate ───────────────────────────────────────────────\n'
        'function switchTab(n){\n'
        '  ["prompt","batch"].forEach(t=>{\n'
        '    document.getElementById("tab-"+t).classList.toggle("on",t===n);\n'
        '    document.getElementById("tp-"+t).classList.toggle("on",t===n);\n'
        '  });\n'
        '}\n'
        'document.getElementById("b-file").addEventListener("change",function(){\n'
        '  const f=this.files[0];if(!f)return;\n'
        '  const rd=new FileReader();\n'
        '  rd.onload=e=>{document.getElementById("b-json").value=e.target.result;};\n'
        '  rd.readAsText(f);\n'
        '});\n'
        'let _lastImgs=[];\n'
        'let _genPollTimer=null;\n'
        'function stopGeneratePoll(){if(_genPollTimer){clearTimeout(_genPollTimer);_genPollTimer=null;}}\n'
        'function setGenerateActive(active){document.getElementById("btn-gen").disabled=active;document.getElementById("btn-gen-stop").disabled=!active;if(active)show("gen-spin");else hide("gen-spin");}\n'
        'function renderGenerateStatus(d){\n'
        '  show("gen-status");\n'
        '  const s=d.state||"unknown";\n'
        '  badge(document.getElementById("gen-badge"),s,bc(s));\n'
        '  setText("gen-msg",d.message||"");\n'
        '  const bar=document.getElementById("gen-progress");\n'
        '  const fill=document.getElementById("gen-progress-fill");\n'
        '  const txt=document.getElementById("gen-progress-text");\n'
        '  if(s==="in_progress"){\n'
        '    bar.classList.remove("hidden");txt.classList.remove("hidden");\n'
        '    if(d.total_steps&&d.total_steps>0){\n'
        '      const pct=Math.max(0,Math.min(100,Number(d.percent||0)));\n'
        '      fill.classList.remove("pulse");fill.style.width=pct+"%";\n'
        '      const img=(d.image_count?Number(d.image_index||0)+1:0)+"/"+(d.image_count||0);\n'
        '      txt.textContent=Math.round(pct)+"% · step "+(d.step||0)+"/"+d.total_steps+" · image "+img+(d.phase?" · "+d.phase:"");\n'
        '    }else{fill.classList.add("pulse");fill.style.width="35%";txt.textContent="Generating…";}\n'
        '  }else if(s==="ready"){\n'
        '    bar.classList.remove("hidden");txt.classList.remove("hidden");fill.classList.remove("pulse");fill.style.width="100%";txt.textContent="100% · complete";\n'
        '  }else if(s==="cancelled"){\n'
        '    bar.classList.remove("hidden");txt.classList.remove("hidden");fill.classList.remove("pulse");fill.style.width=Math.max(0,Math.min(100,Number(d.percent||0)))+"%";txt.textContent="Generation cancelled";\n'
        '  }\n'
        '}\n'
        'async function callGenerate(){\n'
        '  const btn=document.getElementById("btn-gen");\n'
        '  stopGeneratePoll();setGenerateActive(true);hide("gen-err");hide("gen-resp");hide("btn-dl-all");hide("gen-status");hide("gen-progress");hide("gen-progress-text");\n'
        '  document.getElementById("gen-imgs").innerHTML="";\n'
        '  _lastImgs=[];\n'
        '  let body;\n'
        '  const isPrompt=document.getElementById("tp-prompt").classList.contains("on");\n'
        '  if(isPrompt){\n'
        '    const p=document.getElementById("p-prompt").value.trim();\n'
        '    if(!p){const e=document.getElementById("gen-err");e.textContent="Prompt is required.";e.classList.remove("hidden");setGenerateActive(false);return;}\n'
        '    const po={prompt:p};\n'
        '    const neg=document.getElementById("p-neg").value.trim();if(neg)po.negative_prompt=neg;\n'
        '    if(estClipTokens(p)>77||(neg&&estClipTokens(neg)>77)){const e=document.getElementById("gen-err");e.textContent="Prompt or negative prompt is over the 77-token CLIP limit. Shorten it — text past 77 tokens is silently dropped by the model.";e.classList.remove("hidden");setGenerateActive(false);return;}\n'
        '    const seed=document.getElementById("p-seed").value.trim();if(seed)po.seed=parseInt(seed,10);\n'
        '    body={prompts:[po],steps:parseInt(document.getElementById("p-steps").value,10)||20,guidance:parseFloat(document.getElementById("p-guidance").value)||7.5,width:parseInt(document.getElementById("p-width").value,10)||512,height:parseInt(document.getElementById("p-height").value,10)||512,refine:document.getElementById("p-refine").checked,cpu:document.getElementById("p-cpu").checked};\n'
        '  }else{\n'
        '    const raw=document.getElementById("b-json").value.trim();\n'
        '    if(!raw){const e=document.getElementById("gen-err");e.textContent="Paste or upload a batch JSON file.";e.classList.remove("hidden");setGenerateActive(false);return;}\n'
        '    try{body=JSON.parse(raw);}catch(ex){const e=document.getElementById("gen-err");e.textContent="Invalid JSON: "+ex.message;e.classList.remove("hidden");setGenerateActive(false);return;}\n'
        '  }\n'
        '  try{\n'
        '    const r=await fetch(B+"/generate/async",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});\n'
        '    const d=await r.json();\n'
        '    if(r.status>=400||d.state==="error"){\n'
        '      const e=document.getElementById("gen-err");e.textContent="Server error: "+(d.error||JSON.stringify(d));e.classList.remove("hidden");\n'
        '      setGenerateActive(false);return;\n'
        '    }\n'
        '    renderGenerateStatus(d);\n'
        '    pollGenerate();\n'
        '  }catch(e){const el=document.getElementById("gen-err");el.textContent="Request failed: "+e;el.classList.remove("hidden");setGenerateActive(false);}\n'
        '}\n'
        'async function callGenerateCancel(){\n'
        '  stopGeneratePoll();setGenerateActive(false);\n'
        '  try{const r=await fetch(B+"/generate/cancel",{method:"POST"});const d=await r.json();renderGenerateStatus({state:"cancelled",message:d.message||"Generation cancellation requested.",percent:0});}\n'
        '  catch(e){const el=document.getElementById("gen-err");el.textContent="Cancel failed: "+e;el.classList.remove("hidden");}\n'
        '}\n'
        'async function pollGenerate(){\n'
        '  stopGeneratePoll();\n'
        '  try{\n'
        '    const r=await fetch(B+"/generate/status");\n'
        '    const d=await r.json();\n'
        '    renderGenerateStatus(d);\n'
        '    if(d.state==="in_progress"){_genPollTimer=setTimeout(pollGenerate,3000);return;}\n'
        '    stopGeneratePoll();setGenerateActive(false);\n'
        '    const disp=Object.assign({},d);\n'
        '    if(Array.isArray(d.results)){disp.results=d.results.map(x=>{const c=Object.assign({},x);if(c.image_base64)c.image_base64="["+c.image_base64.length+" b64 chars]";return c;});}\n'
        '    setJson("gen-pre",disp);\n'
        '    const lbl=document.getElementById("gen-resp-label");\n'
        '    lbl.textContent="JSON response ("+d.state+(d.device?" · "+d.device:"")+(d.elapsed_seconds?" · "+Math.round(d.elapsed_seconds)+"s":"")+")";\n'
        '    show("gen-resp");\n'
        '    if(d.state==="error"){\n'
        '      const e=document.getElementById("gen-err");e.textContent="Server error: "+(d.error||JSON.stringify(d));e.classList.remove("hidden");return;\n'
        '    }\n'
        '    if(d.state==="cancelled"){return;}\n'
        '    _lastImgs=d.results||[];\n'
        '    renderImages(_lastImgs);\n'
        '    if(_lastImgs.some(x=>x.image_base64))show("btn-dl-all");\n'
        '    if(d.state==="ready")loadHistory();\n'
        '  }catch(e){stopGeneratePoll();setGenerateActive(false);const el=document.getElementById("gen-err");el.textContent="Status poll failed: "+e;el.classList.remove("hidden");}\n'
        '}\n'
        'function renderImages(results){\n'
        '  const c=document.getElementById("gen-imgs");c.innerHTML="";\n'
        '  results.forEach((r,i)=>{\n'
        '    const div=document.createElement("div");div.className="img-card";\n'
        '    if(r.status==="ok"&&r.image_base64){\n'
        '      const img=document.createElement("img");\n'
        '      img.src="data:image/png;base64,"+r.image_base64;\n'
        '      img.alt=r.prompt||"image "+i;\n'
        '      div.appendChild(img);\n'
        '      const meta=document.createElement("div");meta.className="img-meta";\n'
        '      const p=document.createElement("p");p.title=r.prompt||"";p.textContent=(r.prompt||"").substring(0,72);meta.appendChild(p);\n'
        '      const a=document.createElement("a");a.className="dl";\n'
        '      a.href="data:image/png;base64,"+r.image_base64;\n'
        '      a.download=r.filename||("image-"+i+".png");\n'
        '      a.textContent="Download "+( r.filename||"image-"+i+".png");meta.appendChild(a);\n'
        '      div.appendChild(meta);\n'
        '    }else{\n'
        '      const meta=document.createElement("div");meta.className="img-meta";\n'
        '      const eb=document.createElement("div");eb.className="err-box";eb.style.margin="0";\n'
        '      eb.textContent="Error: "+(r.error||r.status||"unknown");meta.appendChild(eb);\n'
        '      const p=document.createElement("p");p.textContent=(r.prompt||"").substring(0,72);meta.appendChild(p);\n'
        '      div.appendChild(meta);\n'
        '    }\n'
        '    c.appendChild(div);\n'
        '  });\n'
        '}\n'
        'function downloadAll(){\n'
        '  _lastImgs.forEach((r,i)=>{\n'
        '    if(r.status==="ok"&&r.image_base64){\n'
        '      const a=document.createElement("a");\n'
        '      a.href="data:image/png;base64,"+r.image_base64;\n'
        '      a.download=r.filename||("image-"+i+".png");\n'
        '      document.body.appendChild(a);a.click();document.body.removeChild(a);\n'
        '    }\n'
        '  });\n'
        '}\n'
        '\n'
        '// ── 6. GET /history ───────────────────────────────────────────────\n'
        'async function loadHistory(){\n'
        '  const c=document.getElementById("hist-list");\n'
        '  c.textContent="Loading history…";\n'
        '  try{\n'
        '    const r=await fetch(B+"/history");const d=await r.json();\n'
        '    c.innerHTML="";\n'
        '    const items=d.items||[];\n'
        '    if(!items.length){c.innerHTML="<p class=\\"hint\\">No generation history yet.</p>";return;}\n'
        '    items.forEach(item=>{\n'
        '      const div=document.createElement("div");div.className="hist-item";\n'
        '      const meta=document.createElement("div");meta.className="hist-meta";\n'
        '      const when=item.created_at?new Date(item.created_at).toLocaleString():"unknown time";\n'
        '      meta.appendChild(document.createTextNode(when+" · "+(item.device||"?")+" · "+(item.ok_count||0)+"/"+(item.image_count||0)+" ok"));\n'
        '      const del=document.createElement("button");del.className="sec";del.textContent="Delete";del.onclick=function(){deleteHistory(item.id);};meta.appendChild(del);\n'
        '      div.appendChild(meta);\n'
        '      const first=(item.prompts&&item.prompts[0]&&item.prompts[0].prompt)||"";\n'
        '      const p=document.createElement("div");p.className="hist-prompt";p.title=first;p.textContent=first.substring(0,140);div.appendChild(p);\n'
        '      const thumbs=document.createElement("div");thumbs.className="hist-thumbs";\n'
        '      (item.prompts||[]).forEach((pm,i)=>{\n'
        '        if(pm.status==="ok"&&pm.filename){\n'
        '          const src=B+"/history/"+encodeURIComponent(item.id)+"/image/"+i;\n'
        '          const img=document.createElement("img");img.className="hist-thumb";img.src=src;img.alt=pm.prompt||"history image "+i;img.onclick=function(){window.open(src,"_blank");};thumbs.appendChild(img);\n'
        '        }\n'
        '      });\n'
        '      div.appendChild(thumbs);c.appendChild(div);\n'
        '    });\n'
        '  }catch(e){c.innerHTML="<div class=\\"err-box\\">History failed: "+e+"</div>";}\n'
        '}\n'
        'async function deleteHistory(id){\n'
        '  try{await fetch(B+"/history/"+encodeURIComponent(id),{method:"DELETE"});loadHistory();}catch(e){alert("Delete failed: "+e);}\n'
        '}\n'
        'async function clearHistory(){\n'
        '  if(!confirm("Clear all generation history?"))return;\n'
        '  try{await fetch(B+"/history",{method:"DELETE"});loadHistory();}catch(e){alert("Clear failed: "+e);}\n'
        '}\n'
        '\n'
        '// auto-fire health on load\n'
        'callHealth();\n'
        'callModelStatus();\n'
        'loadHistory();\n'
        '</script>\n'
        '</body>\n'
        '</html>\n'
    )
    resp = make_response(html, 200)
    resp.headers["Content-Type"] = "text/html; charset=utf-8"
    return resp


@app.route("/api", methods=["GET"])
def root():
    """JSON API index with basic info. The browser UI is served at `/` and `/ui`."""
    return jsonify({
        "name": "SDXL Image Generation API",
        "version": "1.0.0",
        "endpoints": {
            "GET /": "Browser UI — open in your browser to manage and generate images",
            "GET /ui": "Browser UI (alias of /)",
            "GET /api": "This message (JSON API index)",
            "GET /health": "Health check",
            "POST /generate": "Generate images synchronously (returns images inline; may exceed cloud ingress timeouts on CPU)",
            "POST /generate/async": "Start background generation (returns 202); poll GET /generate/status",
            "POST /generate/cancel": "Request cancellation of async generation",
            "GET /generate/status": "Poll async generation state: idle | in_progress | ready | error | cancelled",
            "POST /model/pull": "Start async model download/warm-up (returns 202)",
            "GET /model/status": "Poll model warm-up state and cache presence",
            "GET /history": "List recent generation history (no inline image data)",
            "GET /history/<hid>": "Read one generation history metadata record",
            "GET /history/<hid>/image/<n>": "Download one persisted history image",
            "DELETE /history/<hid>": "Delete one generation history item",
            "DELETE /history": "Clear all generation history"
        },
        "documentation": "POST /generate with JSON batch config containing 'prompts' array"
    }), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    logger.info(f"Starting SDXL Generation API on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
