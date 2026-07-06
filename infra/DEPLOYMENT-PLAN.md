# Deployment Plan — SDXL Image Generation API on Azure Container Apps

**Date:** 2026-07-05  
**Author:** Gonzo (Infrastructure/DevOps)  
**Branch:** squad/aca-acr-auth-complete

---

## Root Causes (Complete List)

### RC-1 — No ACR pull authentication (UNAUTHORIZED)

**Error:** `ContainerAppOperationError: UNAUTHORIZED: authentication required`

The container app had no managed identity and no valid `registries` entry. The `containerRegistryPassword` param defaulted to `''`, making the registries array empty. The container runtime could not authenticate to the private ACR to pull the image.

**Evidence:** `aca.bicep` — `registries: !empty(containerRegistryPassword) ? [...] : []` with `containerRegistryPassword: ''` always passed from `main.bicep`.

**Fix:** Create a User-Assigned Managed Identity (UAMI). Assign it the built-in **AcrPull** role (`7f951dda-4ed3-4680-a7ca-43fe172d538d`) scoped to the ACR resource. Add the UAMI to the container app's `identity` block and add a UAMI-based `registries` entry (no password needed).

---

### RC-2 — Chicken-and-egg: image doesn't exist at provision time

**Error:** `ContainerAppOperationError: Field 'template.containers.sdxl-api.image' is invalid: UNAUTHORIZED`

azd's deploy sequence is **package → provision → deploy**. The Docker image is built and pushed to ACR during **deploy**, but `azd provision` runs **before** that. At provision time, `sdxlregistryXXX.azurecr.io/sdxl-api:latest` does not exist in ACR, so the container app creation fails.

**Evidence:** `main.bicep` passed `imageName: fullImageName` (which references the ACR) directly to `aca.bicep`, which used it unconditionally.

**Fix:** Use the canonical azd `exists` pattern:
- Add `param apiExists bool = false` to `main.bicep`. azd automatically sets this to `true` after first provision.
- In `aca.bicep`, declare a conditional `existing` resource reference.
- `containerImage = exists ? <current running image read from existing app> : <public placeholder>`
- First provision uses `mcr.microsoft.com/azuredocs/containerapps-helloworld:latest` (no ACR auth needed, no missing tag). 
- After `azd deploy` pushes the real image and calls `az containerapp update`, subsequent provisions (exists=true) read the currently-deployed image from the live container app.
- Health probes are also gated on `exists` (placeholder doesn't serve `/health` on port 8000).

---

### RC-3 — azd can't find the container app to update after deploy

**Error:** (silent failure) azd has no way to identify which Container App corresponds to the `api` service.

**Evidence:** `aca.bicep` had no `tags` block. azd locates the correct container app by the tag `azd-service-name: <service-name>`.

**Fix:** Add `tags: { 'azd-service-name': 'api' }` to the container app resource.

---

### RC-4 — `AZURE_CONTAINER_REGISTRY_ENDPOINT` output missing

**Error:** (silent failure on `azd deploy`) azd reads `AZURE_CONTAINER_REGISTRY_ENDPOINT` from bicep outputs to know where to push the built image. The old output was named `containerRegistryUrl`, which azd didn't recognize.

**Evidence:** `main.bicep` — `output containerRegistryUrl string = acr.outputs.loginServer` (wrong name).

**Fix:** Rename to `output AZURE_CONTAINER_REGISTRY_ENDPOINT string = acr.outputs.loginServer`.

---

### RC-5 — Redundant `predeploy` hook in azure.yaml

**Error:** (would fail on Windows) The hook ran `docker build -t ${AZURE_CONTAINER_REGISTRY_ENDPOINT}/sdxl-api:latest .` using `shell: sh`. On Windows, `sh` may not be available. More importantly, azd already builds and pushes the image via the `services.api.docker` block — the hook was a duplicate that tagged with `:latest` instead of the azd deploy tag.

**Evidence:** `azure.yaml` hooks section.

**Fix:** Remove the `predeploy` hook. The `postdeploy` echo is harmless; kept.

---

## Complete Fix (Per-File Changes)

### NEW: `infra/resources/uami.bicep`
Creates a User-Assigned Managed Identity. Outputs `identityId`, `principalId`, `clientId`.

### MODIFIED: `infra/resources/acr.bicep`
- Added `param uamiPrincipalId string`
- Changed `adminUserEnabled: true` → `false` (admin creds no longer needed)
- Added `resource acrPullAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01'` scoped to the ACR, granting AcrPull to the UAMI

### MODIFIED: `infra/main.bicep`
- Removed `param imageName` and `var fullImageName` (aca.bicep no longer needs the image name at provision time)
- Added `param apiExists bool = false`
- Added `module identity 'resources/uami.bicep'` (runs first)
- Wired `uamiPrincipalId: identity.outputs.principalId` → `acr` module
- Wired `uamiId: identity.outputs.identityId` and `exists: apiExists` → `containerApp` module
- Renamed output `containerRegistryUrl` → `AZURE_CONTAINER_REGISTRY_ENDPOINT`

### MODIFIED: `infra/resources/aca.bicep`
- Removed `param imageName`, `param containerRegistryPassword`, `param fileShareName` (unused)
- Added `param uamiId string`, `@secure() param storageAccountKey`
- Added conditional `existing` resource: `resource existingApp if (exists) { ... }`
- Image logic: `exists ? existingApp.properties.template.containers[0].image ?? placeholder : placeholder`
- Added `tags: { 'azd-service-name': 'api' }`
- Added `identity: { type: 'UserAssigned', userAssignedIdentities: { '${uamiId}': {} } }`
- Replaced password-based `registries` with UAMI-based: `{ server: containerRegistryUrl, identity: uamiId }`
- Simplified `secrets` (removed containerRegistryPassword; only storageAccountKey if set)
- Probes gated on `exists` (placeholder doesn't serve /health on port 8000)

### MODIFIED: `azure.yaml`
- Removed `predeploy` hook (azd handles image build/push via `docker:` block)

### REGENERATED: `infra/main.json`
Recompiled from main.bicep to stay in sync.

---

## Deploy Sequence

**Prerequisites:**
- azd >= 1.23.9 installed
- `az login` done (active subscription BAMI_DIBERRY_2)
- Docker Desktop running

**First-time deploy (fresh environment):**

```bash
cd <repo-root>
azd env new <your-env-name>       # e.g. diberry-image4
azd env set AZURE_LOCATION eastus2
azd up
azd provision
```

What happens:
1. `azd package` — builds Docker image from `./Dockerfile` (CPU, python:3.11-slim)
2. `azd provision` — deploys:
   - UAMI
   - ACR (with AcrPull role for UAMI)
   - Log Analytics + ACA Environment (Dedicated D4 workload profile)
   - Container App (`apiExists=false` → placeholder image, no probes)
3. `azd deploy` — pushes real image to ACR, calls `az containerapp update --image <acr>/<tag>`
4. `azd provision` — re-runs bicep with `apiExists=true`, reads the real running image, and explicitly sets `command: ['python3', 'app.py']` so Flask serves the app.

Why the second `azd provision` is required on a brand-new environment: azd's first pass provisions before the ACR image exists, so bicep must create a placeholder container that listens on port 8000. The subsequent azd deploy updates only the image and preserves the placeholder command. A second provision is the bicep-only step that applies the real-container command. `azd deploy` alone after the first `azd up` is not sufficient because it preserves the existing command.

**Re-deploy (code changes after the first successful environment setup):**

```bash
azd deploy     # or azd up (re-runs provision then deploy)
```

On re-provision: `apiExists=true` → bicep reads current image from running app and keeps `command: ['python3', 'app.py']`, so the placeholder command cannot return.

**Teardown:**

```bash
azd down --purge
```

Note: the Dedicated D4 profile always keeps **1 node running** — see Cost note below.

---

## Cost / Teardown Note

The Dedicated D4 workload profile (`minimumCount: 1`) means **one D4 node is always running**, even when no requests come in. D4 = 4 vCPU / 16 GB RAM. In East US 2 this is approximately **$0.50–0.70/hour** (billed per-second). There is no scale-to-zero option on Dedicated profiles.

**Run `azd down --purge` when not using the environment** to avoid ongoing charges. `--purge` ensures the ACR and Log Analytics workspace are fully deleted rather than soft-deleted.

ACR name is `sdxlregistry${uniqueString(resourceGroup().id)}` — deterministic per resource group, so it will not conflict across environments or re-creates of the same RG.

---

## Runtime Fixes — Post-First-Deploy (2026-07-05)

These two issues were only visible at actual deploy runtime — bicep validation and preflight checks cannot catch them.

### RF-1 — Workload-profile node-count deadlock

**Symptom:** Repeated system events: `"The workload profile has reached its maximum node count. Please increase maximum node count."` + `AssigningReplicaFailed` / `Waiting for infrastructure to be ready`. The container app would never transition from the placeholder revision to the real revision.

**Root cause:** `dedicated-d4` profile was `minimumCount: 1, maximumCount: 1` — exactly one D4 node ever. Each D4 node holds exactly one container (4 vCPU / 16 Gi per container = one full node). During a rolling revision handoff, ACA starts the NEW revision's replica before retiring the old one:

```
Node 1: placeholder revision (still Running) ← occupying the only node
Node ?:  real (azd-…) revision              ← no node available → never starts
```

In Single revision mode ACA won't retire the placeholder until the new revision is healthy. But the new revision can't start without a free node. **Permanent deadlock.**

**Live hotfix applied (2026-07-05):** `az containerapp env workload-profile update --name sdxl-env --resource-group rg-diberry-image3 --profile-name dedicated-d4 --max-nodes 2`

**Durable fix (`aca-env.bicep`):** `maximumCount: 2` — two D4 nodes. The transient overlap of old + new revision during every rolling update now has room.

---

### RF-2 — Placeholder container crash-loop from ACA default targetPort probe

**Symptom:** System log for placeholder revision: `Container sdxl-api failed startup probe, will be restarted` → `startup probe failed: connection refused (Count 300)`. Placeholder crash-looped, keeping node 1 occupied and preventing the real revision from advancing.

**Root cause (investigation findings):**

The probe gating in the bicep (`probes: exists ? [...] : []`) compiled CORRECTLY to the ARM template — this was **not** the bug. The ARM expression correctly evaluates `probes: []` when `exists=false`.

The crash-loop was caused by **ACA's platform-level default probe**: when ingress `targetPort` is set (8000), ACA applies a built-in startup check on that port regardless of the user-configured `probes` array. The prior placeholder (`mcr.microsoft.com/azuredocs/containerapps-helloworld:latest`) listens on port 80, not 8000 → ACA default check on 8000 → `connection refused` → crash-loop.

**Fix (`aca.bicep`):**
- Changed placeholder to `python:3.11-slim` with `command: ['python3', '-m', 'http.server', '8000']`. This is the same image used as the Dockerfile base (publicly available, no ACR auth needed). The command starts a minimal HTTP server on port 8000, satisfying ACA's built-in health check with no user-configured probes required.
- Added **Startup probe** to the `exists=true` (real image) probe array. `failureThreshold: 30 × periodSeconds: 10 = 300s` startup window for SDXL's slow CPU-based model load.
- Kept `probes: []` for `exists=false` (placeholder) as designed.
- Restructured bicep into two explicit container vars (`placeholderContainer`, `realContainer`) selected by `containers: [exists ? realContainer : placeholderContainer]`. This makes the compiled ARM expression unambiguous and avoids any probe leaking between cases.

**Verification (main.json compiled ARM expressions):**

`exists=false` → `variables('placeholderContainer')`:
- `image: 'python:3.11-slim'`
- `command: ['python3', '-m', 'http.server', '8000']`
- `probes: []` ✅

`exists=true` → `createObject(... probes: createArray(Startup, Liveness, Readiness))`:
- Startup: `/health:8000`, initialDelay 10s, period 10s, failureThreshold 30
- Liveness: `/health:8000`, initialDelay 60s, period 30s
- Readiness: `/health:8000`, initialDelay 30s, period 10s ✅

Validation: both `apiExists=false` and `apiExists=true` → `provisioningState: Succeeded, error: null`.

---

### RF-3 — Placeholder command override leaked into real container

**Symptom:** The real ACR image was deployed, but `/health` and `/ui` returned `http.server` 404 responses. The container was running `python3 -m http.server 8000` instead of the app entrypoint.

**Recurrence observed (2026-07-06):** Live app `sdxl-generation-api` in `rg-diberry-image4` served a Python `http.server` directory listing at `/`. The serving revision used the real ACR image (`sdxlregistry4itktska4kzhq.azurecr.io/...:azd-deploy-1783351839`) but still had `command: ['python3', '-m', 'http.server', '8000']`.

**Definitive root cause:** On a fresh environment, azd runs `package -> provision -> deploy`. The first provision has `apiExists=false`, so bicep must create the app with the placeholder image and placeholder command. The deploy step then updates only the container image to the real ACR image and preserves the command. The `apiExists=true` bicep branch is not applied during that first `azd up`, so the placeholder command is stranded on the real image.

The secondary theory that `command: []` may not clear a previous command is not needed to explain this recurrence. However, the durable fix avoids that ambiguity entirely by never relying on empty-array clearing.

**Live hotfix applied (2026-07-06):**

```bash
az containerapp update \
  --name sdxl-generation-api \
  --resource-group rg-diberry-image4 \
  --command python3 app.py
```

Verification after the hotfix: `/health` returned HTTP 200 with `{"status":"healthy",...}`, and `/` returned the Flask API JSON instead of a directory listing.

**Durable fix (`aca.bicep`):** Keep the placeholder command only for `apiExists=false`. For `apiExists=true`, explicitly set `command: ['python3', 'app.py']`. This is more robust than `command: []` because it does not depend on ARM/Container Apps clearing semantics and it makes every real-container revision carry the Flask command explicitly.

**Options evaluated:**
- Placeholder image with no command override: rejected. Common small public images that serve by default listen on port 80 or 8080, while this app's ingress and Flask process use port 8000. Changing ingress for the placeholder would break the real app after azd deploy.
- Explicit real command in bicep: chosen. It reliably fixes all `apiExists=true` provisions and minimizes manual steps.
- Postdeploy command reset: avoided. A hook could make a single fresh `azd up` self-heal, but hooks were previously removed as flaky on Windows. The bicep-only reliable sequence is `azd up` followed by `azd provision` for fresh environments.
