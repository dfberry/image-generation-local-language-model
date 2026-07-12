# PRD: Durable `/model/pull` Progress and Non-Blocking Deployment for SDXL ACA API

| Field | Value |
|---|---|
| Author | Dina Berry |
| Date | 2026-07-11 |
| Status | Draft — engineering plan converted to PRD; core items already implemented in code |
| Related plan | `docs/plan-2026-07-11-1922-durable-model-pull-status.md` |
| Repo | `repos/public-dfberry-image-generation-local-language-model` |
| Primary surfaces | `app.py`, `src/image_generation/generate.py`, `azure.yaml`, `infra/resources/storage.bicep`, `infra/resources/aca.bicep`, `scripts/pull-model.ps1`, `scripts/pull-model.sh`, `README.md` |
| Standing constraints | `C:\project-dina-dfberry\.github\skills\standing-constraints.md` |

## 1. Overview / Summary

The SDXL Flask API must expose durable model warm-up progress so browser users, curl users, PowerShell users, Python clients, and deployment operators see reliable `/model/pull` and `/model/status` behavior across Azure Container Apps (ACA) restarts and revisions.

The current code already implements the highest-priority parts of the source plan:

| Plan item | Status | Evidence |
|---|---|---|
| #1 Durable progress file on Azure Files share | Already implemented | `app.py` defines `PROGRESS_FILE_NAME = ".pull-progress.json"`, writes via `_write_progress_file()` with same-directory `tempfile.mkstemp(..., dir=cache_root)` and `os.replace()`, and heartbeats through `_heartbeat_pull_progress()`. |
| #2 ACA warm-up Job | Deferred / future | Not implemented in current infra; future work only. |
| #3 Reconcile `/model/status` with durable state and cache reality | Already implemented | `app.py` implements `reconcile_model_status()`, `_read_progress_file()`, `is_base_model_cache_present()`, and stale `in_progress` -> `stalled` handling. |
| #4 Portable Python poller | Deferred / future | Existing `scripts/generate-cloud.ps1` and `scripts/generate-cloud.sh` have optional warm-up polling, but no dedicated cross-platform Python poller exists. |
| #5 Short, non-blocking `azd` postdeploy hook | Already implemented | `azure.yaml` keeps `hooks.postdeploy.continueOnError: true`; `scripts/pull-model.ps1` and `scripts/pull-model.sh` print manual commands and do not call or poll `/model/pull`. |

## 2. Problem Statement

Before the durable progress work, `/model/pull` progress was held only in the process-local `app.py` `_model_state` dictionary. ACA restarts, replacement revisions, or process exits reset that dictionary to `not_started` even when the Azure Files-backed Hugging Face cache at `/root/.cache/huggingface` already contained the SDXL base model.

The deployment flow also had an operator-experience problem: a blocking `azd` postdeploy hook that initiated or polled model warm-up could make `azd up` unreliable, especially on macOS where shell behavior and long-running waits were brittle. Operators need deployment to finish quickly, with clear manual commands for warm-up instead of hidden blocking work.

## 3. Root Cause

1. **Process-local state was not durable.** `_model_state` in `app.py` is an in-memory dictionary. It is useful while a Flask worker is alive but cannot survive ACA process restarts or revision swaps.
2. **Cache presence and progress were not the same signal.** A populated Hugging Face cache is durable on Azure Files, but process state did not previously reconcile against the actual snapshot directory under `HF_HOME` / `HF_HUB_CACHE`.
3. **Deployment and warm-up were coupled.** Model warm-up can take minutes and depends on network, Azure Files SMB, and Hugging Face behavior. Putting that wait in `azd` postdeploy made infrastructure deployment appear hung or failed even when the API was deployed.

## 4. Goals and Non-Goals

### Goals

- Persist `/model/pull` progress to `<HF_HOME or ~/.cache/huggingface>/.pull-progress.json` on the same durable file share as the Hugging Face cache.
- Write the progress file atomically using a same-directory temporary file and replacement, so readers never observe partial JSON.
- Heartbeat progress while `load_base()` downloads or loads the SDXL base model.
- Make `GET /model/status` always return HTTP 200 with a stable state machine: `not_started`, `in_progress`, `ready`, `error`, `stalled`.
- Reconcile status against both the durable progress file and the real Hugging Face snapshot cache, including self-healing to `ready` after restart when the cache is populated.
- Mark stale in-progress durable files as `stalled` when no local pull worker is alive.
- Keep `azd` postdeploy non-blocking while still surfacing exact manual warm-up and status commands.

### Non-Goals

- Do not add an ACA warm-up Job in the current scope.
- Do not add a portable Python poller in the current scope.
- Do not change SDXL generation request schemas, image output handling, or `/generate` behavior.
- Do not change the Azure Files mount path `/root/.cache/huggingface` or the storage share architecture.
- Do not add new external dependencies.

## 5. Users / Personas

- **Browser UI users:** Use `/` or `/ui` to start model warm-up and observe progress without a terminal.
- **curl users:** Trigger `POST /model/pull` and poll `GET /model/status` from POSIX shells or `curl.exe`.
- **PowerShell users:** Trigger and poll with `Invoke-RestMethod` without relying on the `curl` alias.
- **Python clients:** Poll JSON fields consistently across local and ACA deployments.
- **Operators running `azd`:** Need `azd up` / `azd deploy` to finish without waiting for the 7 GiB model warm-up.

## 6. Requirements

### Functional requirements

| ID | Requirement | Status | Verification |
|---|---|---|---|
| FR-1 | The service MUST store model pull progress at `<HF_HOME or ~/.cache/huggingface>/.pull-progress.json`. | Already implemented | Inspect `app.py` `get_hf_cache_root()`, `get_pull_progress_path()`, `PROGRESS_FILE_NAME`. |
| FR-2 | Progress file writes MUST use a temp file in the same directory as the final file and atomically replace the final file. | Already implemented | Inspect `app.py` `_write_progress_file()` for `tempfile.mkstemp(..., dir=cache_root)` and `os.replace(temp_path, progress_path)`. |
| FR-3 | The service MUST heartbeat durable progress during `load_base()` execution. | Already implemented | Inspect `app.py` `_pull_worker()` and `_heartbeat_pull_progress()`; heartbeat interval is `PULL_HEARTBEAT_SECONDS = 5`. |
| FR-4 | The progress file MUST use the JSON schema in Section 7. | Already implemented | Inspect `app.py` `_progress_payload()`. |
| FR-5 | `GET /model/status` MUST always return HTTP 200 for valid service requests, including `not_started`, `in_progress`, `ready`, `error`, and `stalled`. | Already implemented | Inspect `app.py` `model_status()`. |
| FR-6 | `/model/status` MUST read `.pull-progress.json` when present and ignore malformed JSON. | Already implemented | Inspect `app.py` `_read_progress_file()` and `reconcile_model_status()`. |
| FR-7 | `/model/status` MUST independently check the SDXL base model cache snapshot directory and self-heal to `ready` when cache exists and no local pull worker is active. | Already implemented | Inspect `app.py` `is_base_model_cache_present()` and `reconcile_model_status()`. |
| FR-8 | `/model/status` MUST mark stale durable `in_progress` state as `stalled` when no local pull worker is active and `last_updated` exceeds the stale window. | Already implemented | Inspect `app.py` `PULL_STALE_AFTER_SECONDS = 120` and `reconcile_model_status()`. |
| FR-9 | `POST /model/pull` MUST return immediately with 202 when starting or observing an active pull, and MUST not block on the model download/load. | Already implemented | Inspect `app.py` `model_pull()` thread launch. |
| FR-10 | `POST /model/pull` MUST return 200 when the cache is already `ready` and `force` is not requested. | Already implemented | Inspect `app.py` `model_pull()`. |
| FR-11 | The `azd` postdeploy hook MUST be non-blocking and MUST print service URL plus manual pull/status instructions only. | Already implemented | Inspect `azure.yaml`, `scripts/pull-model.ps1`, and `scripts/pull-model.sh`. |
| FR-12 | The ACA app MUST mount the Azure Files share at `/root/.cache/huggingface` when storage parameters are available. | Already implemented | Inspect `infra/resources/aca.bicep` `volumeMounts` and `volumes`; inspect `infra/resources/storage.bicep` Azure Files share registration. |
| FR-13 | Hugging Face downloads on Azure Files SMB MUST use `SoftFileLock` instead of POSIX `flock()`. | Already implemented | Inspect `src/image_generation/generate.py` filelock patch before importing diffusers. |

### Non-functional requirements

| ID | Requirement | Status | Measurement |
|---|---|---|---|
| NFR-1 | Status polling MUST be OS-independent and expose JSON fields usable by browser, curl, PowerShell, and Python clients. | Already implemented | `/model/status` includes durable fields and README documents curl/PowerShell usage. |
| NFR-2 | Status reconciliation MUST be safe during restarts and revisions; no status response may require process-local state to determine cache readiness. | Already implemented | `reconcile_model_status()` reads durable state and checks cache snapshots. |
| NFR-3 | Progress-file writes MUST minimize corruption risk on SMB-backed Azure Files. | Already implemented | Same-directory temp file, `fsync`, and `os.replace()`. |
| NFR-4 | Deployment MUST not wait for model warm-up. | Already implemented | `pull-model` hook scripts only print commands; `continueOnError: true` remains set. |
| NFR-5 | The solution MUST preserve existing API compatibility for clients that only read `state` and `message`. | Already implemented | Existing fields remain; new durable fields are additive. |

## 7. Data Model / Progress File JSON Schema

Progress is persisted as JSON at `<HF_HOME or ~/.cache/huggingface>/.pull-progress.json`.

```json
{
  "state": "not_started|in_progress|ready|error|stalled",
  "message": "string",
  "bytes_downloaded": 0,
  "bytes_expected": 7516192768,
  "percent": 0.0,
  "started_at": "2026-07-11T00:00:00+00:00",
  "last_updated": "2026-07-11T00:00:05+00:00",
  "finished_at": null,
  "elapsed_seconds": 5.0,
  "error": null,
  "cache_path": "/root/.cache/huggingface/hub/models--stabilityai--stable-diffusion-xl-base-1.0",
  "revision": null
}
```

| Field | Type | Nullable | Meaning |
|---|---|---|---|
| `state` | string enum | No | One of `not_started`, `in_progress`, `ready`, `error`, `stalled`. |
| `message` | string | No | Human-readable status summary. |
| `bytes_downloaded` | integer | No | Current size of the SDXL base model cache directory, best effort. |
| `bytes_expected` | integer | Yes | Expected SDXL base model bytes. Defaults to approximately 7 GiB through `SDXL_BASE_MODEL_EXPECTED_BYTES`; nullable if expected bytes become unknown in a future implementation. |
| `percent` | number | Yes | Percent complete from `bytes_downloaded / bytes_expected`, rounded; `null` when expected bytes are unknown or invalid. |
| `started_at` | ISO 8601 string | Yes | UTC time the current pull attempt started. |
| `last_updated` | ISO 8601 string | Yes | UTC time the durable progress file was last updated. |
| `finished_at` | ISO 8601 string | Yes | UTC time the pull finished or failed. |
| `elapsed_seconds` | number | Yes | Seconds between `started_at` and `finished_at`, or current time for active work. |
| `error` | string | Yes | Error details when `state=error`; otherwise `null`. |
| `cache_path` | string | No | Absolute path to the SDXL base model cache directory under HF hub cache. |
| `revision` | string | Yes | `SDXL_MODEL_REVISION` value when configured; otherwise `null`. |

## 8. API Contract Changes

### `GET /model/status`

- **HTTP status:** Always `200` for a live service request.
- **State enum:** `not_started`, `in_progress`, `ready`, `error`, `stalled`.
- **Additive durable fields:** `bytes_downloaded`, `bytes_expected`, `percent`, `started_at`, `last_updated`, `finished_at`, `elapsed_seconds`, `error`, `cache_path`, `revision`, `timestamp`.
- **Runtime field:** `device` is added by `model_status()` using `get_device()`.
- **Compatibility:** Existing clients reading only `state` and `message` continue to work.

Reconciliation rules:

1. Read `.pull-progress.json` if present; ignore missing or malformed JSON.
2. Compute current `bytes_downloaded` from the SDXL base model cache directory.
3. Check `<HF hub cache>/models--stabilityai--stable-diffusion-xl-base-1.0/snapshots` and minimum cache size independently.
4. If the cache is present and no local pull worker is active, return `state=ready`, even after an ACA restart or revision replacement.
5. If durable state says `ready` but the cache is absent, return `state=not_started`.
6. If durable state says `in_progress`, no local worker is active, and `last_updated` is older than the stale window, return `state=stalled`.

### `POST /model/pull`

- Starts a background thread that calls `load_base(device)` and returns without waiting for the model download/load.
- Returns `202` with reconciled progress when a pull starts or is already in progress.
- Returns `200` when the model is already cached and `force` is not requested.
- Accepts optional JSON body `{ "force": true }` to start a new pull even if current status is ready.
- Writes durable progress before and during the background work.
- Releases the in-memory pipeline after warming the on-disk Hugging Face cache; `/generate` reloads from the warm cache when needed.

## 9. Acceptance Criteria

| ID | Criterion | Verification |
|---|---|---|
| AC-1 | After `POST /model/pull`, `.pull-progress.json` exists under the HF cache root and includes `state`, byte counts, timestamps, cache path, and revision. | Run the API with `HF_HOME` set to a test directory, call `/model/pull`, and inspect `<HF_HOME>/.pull-progress.json`. |
| AC-2 | During a long `load_base()` operation, `last_updated` advances at least once every heartbeat interval plus reasonable scheduling tolerance. | Observe `.pull-progress.json` updates while the model is downloading/loading. |
| AC-3 | Progress-file writes are atomic from the reader perspective; no client receives partial JSON. | Inspect `_write_progress_file()` and run repeated `GET /model/status` calls during writes without JSON decode failures surfacing to clients. |
| AC-4 | After an ACA revision restart with a populated Azure Files cache, `GET /model/status` returns `state=ready` without a new pull. | Populate cache, restart/revise ACA, call `GET /model/status`; response is HTTP 200 and `state=ready`. |
| AC-5 | If `.pull-progress.json` says `in_progress`, no worker is alive, and `last_updated` is stale, `GET /model/status` returns `state=stalled`. | Seed stale progress JSON in HF cache and call `/model/status` with no pull thread. |
| AC-6 | If the progress file is malformed, `/model/status` ignores it and still returns HTTP 200 based on cache and in-memory state. | Write invalid JSON to the progress path and call `/model/status`. |
| AC-7 | `POST /model/pull` returns 202 promptly when starting a pull and does not block for the full model download/load. | Time the HTTP call; response returns immediately while background thread continues. |
| AC-8 | `POST /model/pull` returns 200 when the cache is already ready and `force` is false. | Warm cache, call `/model/pull` without `force`, observe HTTP 200 and ready response. |
| AC-9 | `azd` postdeploy hook does not call `/model/pull`, does not poll `/model/status`, and prints manual commands instead. | Inspect `scripts/pull-model.ps1` and `scripts/pull-model.sh`; run hook scripts with `containerAppUrl` set and confirm output only. |
| AC-10 | Azure Files remains mounted at `/root/.cache/huggingface` for the real ACA app container. | Inspect `infra/resources/aca.bicep` `volumeMounts.mountPath`. |

## 10. Rollout / Deployment Considerations

- **`azure.yaml` hook:** `hooks.postdeploy.continueOnError: true` remains set. The hook runs `scripts/pull-model.sh` on POSIX and `scripts/pull-model.ps1` on Windows, but those scripts only print the service URL and manual commands.
- **Azure Files SMB:** `infra/resources/storage.bicep` provisions a `Standard_LRS` Azure Files share named `models`, registers it as ACA environment storage `models-storage`, and enables SMB. `infra/resources/aca.bicep` mounts that storage to `/root/.cache/huggingface` for the real app container.
- **Hugging Face cache path:** `app.py` resolves cache root from `HF_HOME` or `~/.cache/huggingface`, and hub cache from `HF_HUB_CACHE` or `<cache_root>/hub`.
- **SoftFileLock:** `src/image_generation/generate.py` patches `filelock.FileLock` to `SoftFileLock` before importing diffusers, avoiding POSIX `flock()` failures on Azure Files SMB.
- **Stale threshold:** Current code uses `PULL_STALE_AFTER_SECONDS = 120`. This is short enough to surface dead workers, but long enough for normal 5-second heartbeats to tolerate transient scheduling delays.
- **No schema-breaking client changes:** New `/model/status` fields are additive. Existing README guidance that lists states should be updated to include `stalled` wherever the old state list omits it.

## 11. Future Work

### FW-1: ACA warm-up Job (plan option #2)

Add a Container Apps Job that warms the Azure Files Hugging Face cache independently from the serving Flask revision. The job should mount the same `models-storage` share, run the same model download/load path or a dedicated download helper, and report completion through Azure job status or a durable progress file. This is useful when warm-up should be an explicit cloud operation rather than triggered by the first API request.

Out of current scope:

- New Bicep resources for Container Apps Jobs.
- New job invocation commands in `azd` hooks.
- New operational runbook for job retries.

### FW-2: Portable Python poller (plan option #4)

Add a cross-platform Python command for operators who want a blocking wait after `azd up` without embedding that wait in `azd` itself. The poller should accept a base URL, optionally call `POST /model/pull`, poll `GET /model/status`, and exit non-zero on `error` or timeout.

Out of current scope:

- Replacing existing `scripts/generate-cloud.ps1` / `scripts/generate-cloud.sh` warm-up options.
- Making `azd` postdeploy blocking again.

## 12. Open Questions

No open technical questions block the current PRD. The core plan decisions are resolved: durable progress file, status reconciliation, and non-blocking postdeploy are in scope and already implemented; ACA warm-up Job and portable Python poller are explicitly deferred.

## 13. Risks and Mitigations

| Risk | Mitigation |
|---|---|
| Azure Files SMB latency makes byte-size scans slow on very large caches. | `app.py` scopes size checks to the SDXL base model cache directory; future optimization can cache size or scan less frequently if needed. |
| Stale threshold could mark a slow but alive pull as `stalled` after a restart. | The `stalled` state is only reported when no local worker is active; active workers continue heartbeating every 5 seconds. |
| `bytes_expected` is approximate, so `percent` may be imperfect. | Treat byte progress as best-effort; readiness is determined by snapshot presence and minimum cache size, not percent alone. |
| Malformed progress JSON could confuse clients. | `_read_progress_file()` ignores malformed JSON and `reconcile_model_status()` continues from process/cache state. |
| Multiple replicas could contend on the same Hugging Face cache share. | Current infra pins `minReplicas: 1` and `maxReplicas: 1`; `SoftFileLock` reduces SMB lock failures for Hugging Face operations. |
| Operators may assume `azd up` warms the model. | Postdeploy scripts print explicit `POST /model/pull` and `GET /model/status` commands; README documents pre-warming separately. |

## 14. Scope Fence

### In Scope

- Document durable `/model/pull` progress behavior.
- Document `/model/status` reconciliation and API contract.
- Document non-blocking `azd` postdeploy behavior.
- Distinguish implemented plan items from deferred future work.

### Out of Scope

- Implementing new application code.
- Implementing a Container Apps Job.
- Implementing a portable Python poller.
- Changing generation, image storage, auth, cost model, or scaling settings.

### Must Not Change

```yaml
must-not-change:
  - "app.py"
  - "src/image_generation/generate.py"
  - "azure.yaml"
  - "infra/resources/storage.bicep"
  - "infra/resources/aca.bicep"
  - "scripts/pull-model.ps1"
  - "scripts/pull-model.sh"
  - "README.md"
```

## 15. Verification Commands

Run from `repos/public-dfberry-image-generation-local-language-model`.

| Check | Command / Observable | Expected Result |
|---|---|---|
| Positive: PRD file exists | `Test-Path .\docs\prd-2026-07-11-2013-durable-model-pull-status.md` | `True` |
| Positive: durable progress implementation is present | `Select-String -Path .\app.py -SimpleMatch -Pattern 'PROGRESS_FILE_NAME','os.replace','_heartbeat_pull_progress','reconcile_model_status','stalled'` | Matches all listed implementation concepts. |
| Positive: postdeploy hook is non-blocking | `Select-String -Path .\scripts\pull-model.ps1,.\scripts\pull-model.sh -SimpleMatch -Pattern 'Trigger cache warm-up','Check progress'` | Both scripts print manual warm-up/status instructions. |
| Negative: postdeploy hook does not invoke polling loop | `(Select-String -Path .\scripts\pull-model.ps1,.\scripts\pull-model.sh -Pattern 'Start-Sleep|sleep [0-9]|while |do \{').Where({ $_.Line -notmatch '^\s*#' })` | No non-comment polling loop or sleep statements. |
| Negative: no ACA Job in current scope | `git grep 'Microsoft.App/jobs' -- infra` | No matches. |

## 16. Dispatch Instructions

- **Trigger:** Manual PRD conversion requested by Dina Berry.
- **Entry point:** Start with `docs/plan-2026-07-11-1922-durable-model-pull-status.md`, then inspect the code paths listed in the metadata table.
- **Autonomous execution:** Yes. Proceed without confirmation unless git push or PR creation fails due to authentication/permission issues.
- **Output target:** `docs/prd-2026-07-11-2013-durable-model-pull-status.md`.
- **Primary repo:** `repos/public-dfberry-image-generation-local-language-model`.
- **Branch:** `diberry/prd-durable-model-pull-status` from `origin/main`.
- **PR target:** `main`.
- **Standing constraints:** `C:\project-dina-dfberry\.github\skills\standing-constraints.md`.

