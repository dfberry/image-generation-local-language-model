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
```

What happens:
1. `azd package` — builds Docker image from `./Dockerfile` (CPU, python:3.11-slim)
2. `azd provision` — deploys:
   - UAMI
   - ACR (with AcrPull role for UAMI)
   - Log Analytics + ACA Environment (Dedicated D4 workload profile)
   - Container App (`apiExists=false` → placeholder image, no probes)
3. `azd deploy` — pushes real image to ACR, calls `az containerapp update --image <acr>/<tag>`, probes go live

**Re-deploy (code changes):**

```bash
azd deploy     # or azd up (re-runs provision then deploy)
```

On re-provision: `apiExists=true` → bicep reads current image from running app, no disruption.

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
