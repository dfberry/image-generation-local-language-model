# Backend model-size decision

Date: 2026-07-13
Requested by: diberry

## Decision
Download SDXL base weights into a flat persistent model directory (`SDXL_BASE_MODEL_DIR` or `$HF_HOME/models/sdxl-base-1.0`) and measure logical model bytes from that directory first, with fallback to the legacy HF hub cache when the flat directory is empty.

## Rationale
The HF hub cache stores payloads in `blobs/` and references them from `snapshots/`. On Linux snapshots are symlinks, and on Azure Files SMB they can become full copies, causing progress and readiness checks to double-count the model. A flat local directory avoids the duplicate `blobs/` + `snapshots/` layout and keeps `/model/status` aligned with the path `load_base()` uses after prewarm.
