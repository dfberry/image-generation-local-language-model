<!--
Source: authored in-repo (not a SharePoint mirror)
Origin: conversation with @diberry via Squad Coordinator
Date: 2026-07-12
Purpose: Capture the Azure-native options for downloading and storing the SDXL
         model (with weights) into Azure Storage WITHOUT the application load
         file (src/image_generation/generate.py) managing the download/upload.
         Feeds Stage 1 of the 3-stage Foundry/Bicep PRD.
-->

# Azure-Native SDXL Model Pull & Store

## Goal

Get the SDXL model (weights included) into an Azure Storage **blob container**
whose name encodes the model **version** and **weight/precision**, using a
mechanism that runs **completely in Azure**. The web application's load file
(`src/image_generation/generate.py`) must **not** download from Hugging Face at
runtime — it only *reads* the already-populated blob container.

## Key constraint: Bicep cannot download files

Bicep is **declarative** — it provisions resources, it does not fetch bytes.
Pulling ~13 GB of SDXL weights from Hugging Face into blob is an **imperative**
action. So the pattern is always:

```
Bicep declares:  storage account + blob container (+ optional compute/identity)
Something imperative populates: HF weights ---> blob container
App reads:       blob container ---> generate.py (no HF at runtime)
```

The question is only **which Azure-native mechanism** performs the populate step.

---

## Blob container naming rules (must sanitize)

Azure blob container names must be:

- lowercase letters, digits, and hyphens only
- 3–63 characters
- start and end with a letter or digit
- **no** `.` characters
- **no** consecutive hyphens (`--`)

So a model id + version + weight like
`stabilityai/stable-diffusion-xl-base-1.0` + `fp16` must be sanitized:

| Raw | Sanitized container name |
|---|---|
| `stable-diffusion-xl-base-1.0` / `fp16` | `sdxl-base-1-0-fp16` |
| `stable-diffusion-xl-refiner-1.0` / `fp16` | `sdxl-refiner-1-0-fp16` |

**Sanitization rule:** `lowercase`, then `.` → `-`, `/` → `-`, collapse any `--` → `-`.

---

## Option A — Server-side blob copy (zero compute, most "pure Azure")

The Storage service itself pulls the bytes from Hugging Face's public download
URLs. Nothing runs on your machine or in your app — it is an async
server-to-server copy.

Hugging Face exposes direct file URLs:
`https://huggingface.co/{repo}/resolve/{revision}/{file}`

```bash
az storage blob copy start \
  --account-name "$ACCOUNT" --auth-mode login \
  --destination-container sdxl-base-1-0-fp16 \
  --destination-blob "sd_xl_base_1.0.safetensors" \
  --source-uri "https://huggingface.co/stabilityai/stable-diffusion-xl-base-1.0/resolve/main/sd_xl_base_1.0.safetensors"
```

Repeat per file (SDXL is ~5–7 files: the `.safetensors` weights plus config/
tokenizer JSON). Poll completion with `az storage blob show ... --query properties.copy`.

- **Pros:** truly serverless; no disk/timeout trap; no local egress; no compute to manage.
- **Cons:** per-file (enumerate the file list); works only for **public / ungated**
  repos — server-side copy cannot send an HF `Authorization` header. SDXL base is public.

---

## Option B — Container Apps Job (best all-around for this repo)

A one-shot **Azure Container Apps Job** runs
`huggingface-cli download → az storage blob upload-batch`, triggered once at
deploy time. This repo already runs on ACA, so it reuses the same environment.

- **Pros:** repeatable; handles **gated/private** repos (HF token as a secret);
  large ephemeral disk; managed-identity auth (`Storage Blob Data Contributor`);
  fully decoupled from the web app.
- **Cons:** it is compute (spins up, runs, exits) — but it is **not** the app's
  load file. This is the intended separation.

---

## Option C — Bicep `deploymentScripts` (provision-time, IaC-native)

The same download+upload logic embedded in the Bicep deploy as an ACI-backed
Azure CLI script, so it runs automatically during `azd up`.

```bicep
resource pull 'Microsoft.Resources/deploymentScripts@2023-08-01' = {
  name: 'pull-${containerName}'
  location: location
  kind: 'AzureCLI'
  identity: { type: 'UserAssigned', userAssignedIdentities: { '${uami.id}': {} } }
  properties: {
    azCliVersion: '2.62.0'
    timeout: 'PT2H'            // large model -> long timeout
    retentionInterval: 'PT1H'
    storageAccountSettings: { /* mount a file share -> avoids the 13 GB disk trap */ }
    environmentVariables: [
      { name: 'HF_REPO', value: hfRepo }
      { name: 'HF_REV', value: hfRevision }
      { name: 'ACCOUNT', value: sa.name }
      { name: 'CONTAINER', value: containerName }
    ]
    scriptContent: '''
      pip install -q "huggingface_hub[cli]"
      huggingface-cli download "$HF_REPO" --revision "$HF_REV" --local-dir /tmp/m
      az storage blob upload-batch --account-name "$ACCOUNT" \
        --destination "$CONTAINER" --source /tmp/m --auth-mode login --overwrite
    '''
  }
  dependsOn: [ container ]
}
```

- **Pros:** fully hands-off inside the infra deploy; no separate trigger.
- **Cons:** ACI ephemeral disk default (~8 GB) is **smaller than 13 GB SDXL** →
  must configure a mounted Azure Files share (`storageAccountSettings`). This is
  the #1 failure mode for this option.

---

## Storage-account provisioning sketch (shared by all options)

```bicep
param modelName    string = 'sdxl-base'
param modelVersion string = '1.0'
param modelWeight  string = 'fp16'   // precision / variant
param hfRepo       string = 'stabilityai/stable-diffusion-xl-base-1.0'
param hfRevision   string = 'main'

// version + weight in the container name, sanitized to blob naming rules
var containerName = toLower(replace(replace('${modelName}-${modelVersion}-${modelWeight}', '.', '-'), '/', '-'))

resource sa 'Microsoft.Storage/storageAccounts@2023-05-01' = { /* Standard_LRS */ }
resource blob 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: sa
  name: 'default'
}
resource container 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blob
  name: containerName
}
```

---

## Recommendation

| Need | Pick |
|---|---|
| Simplest, public SDXL only, no compute | **A — server-side copy** (wrap file list in an `azd postprovision` hook or small script) |
| Gated/private models, re-runnable, robust | **B — Container Apps Job** |
| Want it inside the Bicep deploy itself | **C — deploymentScripts**, only with a mounted file share for the 13 GB |

**Primary design for the PRD:** Option **A** for public SDXL, with Option **B**
documented as the fallback for gated/private models.

## App-reads-from-blob contract (the whole point)

`src/image_generation/generate.py` changes so it **no longer talks to Hugging
Face**. Instead it reads the pre-populated blob container via one of:

- **Mount Azure Files** into the container at the model cache path, or
- **Download-on-start from blob** using managed identity (`Storage Blob Data Reader`).

Either way, the model bytes are already in Azure before the app starts, and the
version/weight are visible in the container name.

## Gotchas to carry into the PRD acceptance criteria

| Issue | Detail |
|---|---|
| 13 GB in a deploymentScript | ACI default disk (~8 GB) < SDXL; requires a mounted file share (Option C only). |
| Timeout | Large model → set `PT1H`–`PT2H`. |
| Gated/private HF repos | Need `HF_TOKEN` as a secure env var (Options B/C); Option A cannot auth. |
| Idempotency | Guard re-runs on "container already populated" so you don't re-upload. |
| Auth | Use a user-assigned managed identity with `Storage Blob Data Contributor` — not account keys. |

---

## Mapping to the 3-stage PRD

- **Stage 1 — Provision + populate model store:** everything in this document.
- **Stage 2 — Deploy model to Foundry:** separate concern (Foundry model
  deployment; see `hf-sdxl-foundry-azd-bicep-spec-updated.md`).
- **Stage 3 — Deploy web app:** app reads from the endpoint/blob via config from
  `azd` outputs.
