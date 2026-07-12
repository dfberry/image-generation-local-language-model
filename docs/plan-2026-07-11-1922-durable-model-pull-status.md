# Durable model-pull status plan (2026-07-11)

## Root cause

The Flask API kept `/model/pull` progress only in a process-local `_model_state`
dictionary. That state reset to `not_started` whenever Azure Container Apps
restarted or shifted revisions, even when the Azure Files Hugging Face cache was
already populated at `/root/.cache/huggingface`. The `azd` postdeploy hook also
blocked while polling model warm-up, which made deploys unreliable on macOS.

## Ranked proposal

1. **Durable progress file on the Azure Files share — implemented now.** Write
   `<HF_HOME or ~/.cache/huggingface>/.pull-progress.json` with atomic
   same-directory temp-file replacement. A heartbeat updates progress while
   `load_base()` downloads/loads the SDXL base model.
2. **ACA Job that warms the share — deferred.** A separate Container Apps Job
   could populate the model cache without tying warm-up to the serving revision.
3. **Reconcile `/model/status` against durable state and cache reality —
   implemented now.** Every status call reads the progress file, checks the
   expected Hugging Face snapshot directory, self-heals ready caches after
   restart, and marks stale in-progress files as `stalled`.
4. **Portable Python poller — deferred.** A small cross-platform Python command
   could replace shell-specific warm-up polling for operators who want blocking
   behavior outside `azd`.
5. **Short, non-blocking `azd` postdeploy hook — implemented now.** The hook
   prints the service URL and manual `POST /model/pull` / `GET /model/status`
   instructions only.

## Progress file JSON schema

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

`bytes_expected` is best-effort and defaults to approximately 7 GiB for the
SDXL base model. `percent` is nullable when expected bytes are unknown.

## `/model/status` contract changes

`GET /model/status` still returns HTTP 200. Its `state` can now be
`not_started`, `in_progress`, `ready`, `error`, or `stalled`. The response also
includes durable fields for OS-independent clients: `bytes_downloaded`,
`bytes_expected`, `percent`, `last_updated`, `cache_path`, and `revision`.

Status reconciliation rules:

1. Read `.pull-progress.json` if present; ignore malformed JSON.
2. Check the SDXL base model cache directory independently. If the snapshot is
   present and no local pull worker is active, report `ready`.
3. If the durable file says `in_progress`, `last_updated` is older than the
   stale window, and no local worker is active, report `stalled`.
4. Return durable byte and timestamp fields so browser, curl, PowerShell, and
   Python clients observe the same progress.

## `azure.yaml` hook change

The `postdeploy` hook remains `continueOnError: true`, but the referenced
PowerShell and POSIX scripts no longer call `/model/pull` or poll. They only
print the service URL plus one-line commands to trigger the pull and check
status/logs, avoiding long deploy-time waits.

## Future work

- Implement option #2 with an ACA Job when warm-up should be an explicit cloud
  operation independent of the serving API revision.
- Implement option #4 with a portable Python poller for users who want a
  blocking wait command after `azd up` without embedding that wait in `azd`.
