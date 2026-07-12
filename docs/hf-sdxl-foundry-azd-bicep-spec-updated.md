<!--
Source: hf-sdxl-foundry-azd-bicep-spec-updated.docx
Origin: SharePoint (OneDrive) — diberry@microsoft.com / Documents/Copilot/Created
Copied into repo: 2026-07-12
Note: This is a faithful Markdown mirror of the Word document, reproduced
section-by-section. Reference bracketed footnote markers from the source
have been removed for readability; content is otherwise verbatim.
-->

# HF SDXL → Microsoft Foundry azd + Bicep spec (updated)

## 1. Executive summary

This spec defines an azd-ready repo pattern for deploying a Hugging Face model into Microsoft Foundry / Azure AI Foundry as a model endpoint. The recommended path is to select a deployable Hugging Face model from the Foundry model catalog, provision the Foundry resource and project with Bicep, create a model deployment with Bicep, and expose the resulting endpoint to an app through generated azd environment values.

| **Key decision** | Use the managed Foundry model deployment path when the model is available in the catalog and supports the required task, SKU, and region. For SDXL specifically, treat deployability as a validation step: confirm that the selected SDXL-family model appears in the Hugging Face collection in Foundry or is returned by `az cognitiveservices account list-models` before promising one-command deployment. |
|---|---|

---

## 2. Source-grounded facts

- **Foundry model deployments:** Microsoft Learn documents adding a model deployment to a Foundry Models endpoint with Azure CLI or Bicep; the deployment is available for inference by specifying the deployment name in requests.
- **Prerequisites:** The same documentation lists an Azure subscription, a Foundry project, RBAC permissions to create and manage deployments, Marketplace permissions for partner/community models, Azure CLI 2.60 or later, the cognitiveservices extension, and jq for some commands.
- **Model discovery:** The deployment article shows using `az cognitiveservices account list-models` to identify model name, format/provider, version, SKU, and capacity before deployment.
- **Hugging Face availability:** Hugging Face documentation says around 11,000 open models from Hugging Face Hub are available through Microsoft Foundry and Azure Machine Learning, and suggests checking the model card for "Deploy on Microsoft Foundry" or searching the Hugging Face collection in the Foundry or Azure Machine Learning catalog.
- **Classic managed endpoint capability:** Microsoft Learn for Foundry classic says Hugging Face models can be deployed to managed compute endpoints that provide a secure REST API for real-time scoring, with serving, scaling, securing, and monitoring handled by managed online endpoints.
- **Known current nuance:** Internal work items indicate customer confusion around Hugging Face support in new Foundry; some notes state Hugging Face-published models are deployable through Foundry classic and next-gen via managed compute, limited to GPUs in next-gen. Treat this as an internal signal to validate current public docs and catalog behavior for each model.

---

## 3. Goals and non-goals

| **Goals** | **Non-goals** |
|---|---|
| One-command `azd up` experience for infrastructure and app configuration. | Do not guarantee every Hugging Face model, including every SDXL variant, is deployable from catalog. |
| Bicep modules for Foundry resource/project and model deployment. | Do not build a full model training or fine-tuning pipeline. |
| Validation script for model availability, SKU, region, and quota before provisioning. | Do not store model weights in the repo or bypass model/provider license terms. |
| Clear fallback path for BYOC/custom container or ACA GPU when catalog deployment is unavailable. | Do not use keys by default in application code when managed identity is viable. |

---

## 4. Proposed architecture

The sample should separate the application host from model hosting. The app can be Azure Container Apps, App Service, Functions, or local dev. The model endpoint should be the Foundry-hosted endpoint when the selected model is supported.

```text
Developer machine
  azd up
    ├─ infra/main.bicep
    │   ├─ Microsoft.CognitiveServices/accounts (kind: AIServices)
    │   ├─ Microsoft.CognitiveServices/accounts/projects
    │   ├─ model deployment under the Foundry resource
    │   ├─ optional Azure Container Apps front end / API
    │   └─ outputs: endpoint URI, deployment name, project/resource IDs
    └─ app uses Managed Identity or API key during local/dev fallback
Runtime
  Web/API app  ──>  Foundry Models endpoint  ──>  Hugging Face model deployment
```

---

## 5. Model selection and eligibility workflow

- Start with the model ID from Hugging Face, such as `stabilityai/stable-diffusion-xl-base-1.0` or an approved alternative.
- Check the model card for the **Deploy on Microsoft Foundry** option, or search the Hugging Face collection in the Foundry model catalog.
- Run `az cognitiveservices account list-models` against the target Foundry resource and capture the exact model name, provider/format, version, SKU, and capacity.
- Verify Marketplace/provider terms, Hugging Face license terms, and any gating/token requirements.
- Verify regional GPU quota and SKU availability before creating the deployment.
- If the model is not available or deployment fails because of model format, tokenizer, `trust_remote_code`, or unsupported libraries, switch to BYOC/custom container or ACA GPU fallback.

---

## 6. azd repo structure

```text
hf-foundry-endpoint-sample/
├─ azure.yaml
├─ README.md
├─ .env.example
├─ infra/
│  ├─ main.bicep
│  ├─ main.parameters.json
│  ├─ modules/
│  │  ├─ foundry.bicep
│  │  ├─ model-deployment.bicep
│  │  ├─ app-containerapp.bicep        # optional app host
│  │  └─ role-assignments.bicep
│  └─ hooks/
│     ├─ preprovision.sh               # validates model, SKU, region, quota
│     └─ postprovision.sh              # writes endpoint values to azd env
├─ src/
│  └─ api/                             # minimal caller app, optional
└─ scripts/
   ├─ list-models.sh
   ├─ test-endpoint.sh
   └─ smoke-test-image-generation.sh
```

---

## 7. azure.yaml shape

```yaml
name: hf-foundry-endpoint-sample
metadata:
  template: hf-foundry-endpoint-sample@0.1.0
infra:
  provider: bicep
  path: infra
hooks:
  preprovision:
    shell: sh
    run: ./infra/hooks/preprovision.sh
  postprovision:
    shell: sh
    run: ./infra/hooks/postprovision.sh
services:
  api:
    project: ./src/api
    host: containerapp
    language: python
```

---

## 8. Bicep design

Use the official Foundry resource/project Bicep sample as the base. Microsoft Learn says the Foundry resource quickstart deploys `Microsoft.CognitiveServices/accounts` and `Microsoft.CognitiveServices/accounts/projects`. The model deployment module should be parameterized so the same repo can deploy SDXL or another supported model without changing source code.

```bicep
// infra/main.bicep - conceptual skeleton
param location string = resourceGroup().location
param aiFoundryName string
param aiProjectName string
param modelDeploymentName string
param modelName string
param modelVersion string
param modelPublisherFormat string
param modelSkuName string
param modelSkuCapacity int = 1

module foundry './modules/foundry.bicep' = {
  name: 'foundry'
  params: {
    location: location
    aiFoundryName: aiFoundryName
    aiProjectName: aiProjectName
  }
}

module modelDeployment './modules/model-deployment.bicep' = {
  name: 'modelDeployment'
  params: {
    accountName: aiFoundryName
    deploymentName: modelDeploymentName
    modelName: modelName
    modelVersion: modelVersion
    modelPublisherFormat: modelPublisherFormat
    skuName: modelSkuName
    skuCapacity: modelSkuCapacity
  }
  dependsOn: [foundry]
}

output foundryEndpoint string = foundry.outputs.endpoint
output modelDeploymentName string = modelDeploymentName
```

| **Implementation note** |
|---|
| The exact deployment resource schema should be copied from the current Microsoft Learn sample repository for `Azure-Samples/azureai-model-inference-bicep` or the current Foundry Samples repo. Do not hand-author the final resource shape from memory; pin the API version and validate with `az deployment group what-if`. |

---

## 9. Required parameters and environment values

| **Parameter** | **Example** | **Source** | **Notes** |
|---|---|---|---|
| `aiFoundryName` | `my-hf-foundry` | azd env / infra | Unique Foundry AI Services resource name. |
| `aiProjectName` | `hf-sdxl-proj` | azd env / infra | Project name inside the Foundry resource. |
| `modelDeploymentName` | `sdxl-dev` | azd env | Stable name apps use as the model/deployment parameter. |
| `modelName` | catalog-specific | list-models | Exact name returned by list-models or catalog. |
| `modelPublisherFormat` | HuggingFace or provider value | list-models | Use exact format/provider returned by CLI. |
| `modelVersion` | catalog-specific | list-models | Use exact model version. |
| `modelSkuName` | catalog-specific | list-models | Use SKU returned by CLI and available in target region. |
| `modelSkuCapacity` | `1` | list-models / quota | Start with minimum, then tune. |

---

## 10. Pre-provision validation hook

The pre-provision hook should fail fast before azd tries to create infrastructure. The check should be intentionally conservative and print the exact model metadata the user must place in azd env values.

```bash
#!/usr/bin/env bash
set -euo pipefail
: "${AZURE_LOCATION:?Set AZURE_LOCATION}"
: "${AZURE_RESOURCE_GROUP:?Set AZURE_RESOURCE_GROUP}"
: "${AI_FOUNDRY_NAME:?Set AI_FOUNDRY_NAME}"
: "${MODEL_NAME:?Set MODEL_NAME}"
az extension add -n cognitiveservices --only-show-errors || true
az cognitiveservices account list-models \
  -n "$AI_FOUNDRY_NAME" \
  -g "$AZURE_RESOURCE_GROUP" \
  | jq --arg model "$MODEL_NAME" '.[] | select(.name == $model) | {name, format, version, skus}'
# Recommended additions:
# - verify target SKU exists in .skus[]
# - verify target capacity is within allowed/default bounds
# - verify marketplace terms for partner/community models
# - verify resource provider registration and quota before deployment
```

---

## 11. Developer workflow

- `azd auth login`
- `azd init --template <repo-url>`
- `azd env set AI_FOUNDRY_NAME <name>`
- `azd env set MODEL_NAME <exact-catalog-name>`
- `azd env set MODEL_VERSION <exact-version>`
- `azd env set MODEL_PUBLISHER_FORMAT <exact-provider-format>`
- `azd env set MODEL_SKU_NAME <sku>`
- `azd up`
- Run `scripts/test-endpoint.sh` to verify endpoint reachability and inference behavior.

---

## 12. App integration contract

The infrastructure deployment should output a minimal set of values for app code. The app should not hard-code the model endpoint or model deployment name.

```text
FOUNDRY_ENDPOINT=<from bicep/azd output>
FOUNDRY_MODEL_DEPLOYMENT=<deployment name>
AZURE_CLIENT_ID=<managed identity client id, if user-assigned>
# Local-only fallback when Entra auth is not yet wired:
FOUNDRY_API_KEY=<developer secret, never committed>
```

---

## 13. Security, governance, and responsible AI requirements

- Use managed identity for hosted app-to-Foundry calls whenever the chosen SDK/API path supports it; keep API keys as local-development fallback only.
- Disable or avoid local auth where supported by the selected resource path and sample goal.
- Do not commit Hugging Face tokens, inference keys, or generated images that contain sensitive data.
- Document the model license and link to the Hugging Face model card.
- For gated models, document the separate access request and token/connection setup.
- Include quota and cost warnings because GPU-backed model endpoints can accrue charges while deployed.
- Add a content safety / evaluation section if the endpoint is used in an end-user-facing image or text application.

---

## 14. Fallback implementation paths

| **Path** | **Use when** | **Tradeoff** |
|---|---|---|
| Catalog model deployment | Model appears in Foundry/Hugging Face catalog and list-models returns usable metadata. | Lowest maintenance; endpoint hosted by Foundry resource. |
| BYOC / custom container | Model is not deployable from catalog, needs custom inference code, custom libraries, or unsupported runtime behavior. | More control; requires container, registry, model artifact, deployment template, and more operational ownership. |
| Azure Container Apps serverless GPU | Need containerized inference with scale-to-zero behavior or Foundry model on ACA GPU pattern. | Good for bursty workloads; app team owns container/runtime path. |
| Azure ML online endpoint | Need mature ML endpoint controls or existing AML workflow. | More ML-native; less "Foundry-first" for app samples. |

---

## 15. Acceptance criteria

- `azd up` provisions a Foundry resource and project from clean subscription/resource group inputs.
- The preprovision hook prints exact model metadata or fails with actionable guidance.
- Bicep creates the model deployment using parameters only; no hard-coded model name/version/SKU remains in module code.
- azd output exposes endpoint and deployment name to the app or smoke-test script.
- Smoke test verifies the endpoint returns a successful inference response for the selected model.
- README explains SDXL validation and fallback paths clearly.
- `azd down` removes created resources or documents any cleanup exceptions, such as Marketplace agreements or external approvals.

---

## 16. Risks and open questions

- **SDXL model availability:** Which exact SDXL-family model is deployable in the current Foundry catalog and target region?
- **Endpoint API shape for image generation:** Need to verify whether the selected image-generation model exposes the desired API route and response schema.
- **Bicep schema drift:** Foundry resource and model deployment schemas are evolving; pin API versions and validate against current Learn/sample repo.
- **Auth path:** Confirm managed identity support for the exact endpoint route and SDK/API used by the app.
- **Quota/cost:** GPU-backed managed compute can fail for quota or cost reasons; validation and cleanup need to be prominent.

---

## 17. Implementation plan

- **P0:** Create repo skeleton, README, azure.yaml, bicep modules, and validation hook.
- **P1:** Wire Foundry resource/project Bicep from official sample and model deployment module from current Learn sample.
- **P2:** Add model discovery script and sample parameter file for one verified Hugging Face model.
- **P3:** Add optional Azure Container Apps web/API caller and smoke tests.
- **P4:** Add SDXL-specific path once exact current catalog metadata and API behavior are validated.
- **P5:** Add BYOC/ACA GPU fallback branch or separate sample if catalog path is unsupported for SDXL.

---

## 18. Sources reviewed

| **Source** | **Link** | **Use** |
|---|---|---|
| Microsoft Learn — Deploy models using Azure CLI and Bicep | https://learn.microsoft.com/en-us/azure/foundry/foundry-models/how-to/create-model-deployments | Used for model deployment prerequisites, CLI flow, Bicep sample reference, model metadata requirements, endpoint usage, and troubleshooting. |
| Microsoft Learn — Quickstart: Deploy a Foundry resource by using Bicep | https://learn.microsoft.com/en-us/azure/foundry/how-to/create-resource-template | Used for Foundry resource/project Bicep deployment pattern and sample repo starting point. |
| Hugging Face — Supported Models on Microsoft Azure | https://huggingface.co/docs/microsoft-azure/azure-ai/models | Used for Hugging Face model availability and checking model deployability in Foundry/Azure ML catalog. |
| Microsoft Learn — Deploy Hugging Face Hub models in Microsoft Foundry classic | https://learn.microsoft.com/en-us/azure/foundry-classic/how-to/deploy-models-managed-hugging-face | Used for managed endpoint behavior, Hugging Face deployment prerequisites, supported model notes, and troubleshooting. |
| Microsoft Product Help — Using serverless GPUs in Azure Container Apps | internal/product-help result | Used as a fallback consideration for ACA serverless GPU and Foundry model deployment to ACA GPU. Verify against current public docs before publishing externally. |
| Internal ADO / wiki signals | enterprise search results | Used only to identify likely open questions: BYOC import, new Foundry Hugging Face behavior, managed compute GPU limitation, and Bicep operational requirements. Do not cite externally without source-owner review. |

---

## 19. Addendum: Separate model storage, versioning, and deployment selection

This addendum updates the architecture to separate the model artifact lifecycle from the model deployment lifecycle. The goal is to store one or more versioned model artifacts, then choose which stored model version is deployed to the Foundry endpoint without changing application code.

| Recommended answer |
|---|
| Use one azd project when you want a cohesive sample or app deployment, but split it into separate Bicep modules and parameters. Use separate azd commands or separate repos only when model publishing and serving are owned by different teams or released on different schedules. |

### 19.1 Lifecycle separation

| Lifecycle | What changes | Recommended owner | How azd/Bicep should treat it |
|---|---|---|---|
| Model storage / registry | New model artifact or new model version is published. | Model build / ML platform workflow | Provisioned separately from serving; versioned and treated as durable state. |
| Model deployment | A stored model version is selected for serving traffic. | App or service release workflow | Parameterized deployment that points to selected model name and version. |
| Application | Web/API changes, prompt orchestration, UI, auth, telemetry. | App team | Consumes endpoint URI and deployment name from azd outputs; does not own model artifacts. |

### 19.2 Pattern A: Same azd project, separate Bicep modules

Use this for a single sample repo where the developer experience should remain `azd up`, but the infrastructure is cleanly decomposed. The user can change `MODEL_VERSION` and rerun `azd up` to update the endpoint deployment while keeping storage, app, and endpoint naming stable.

```text
infra/
├─ main.bicep
├─ modules/
│  ├─ model-storage.bicep       # creates/links versioned model storage or registry
│  ├─ foundry-project.bicep     # creates/links Foundry resource/project
│  ├─ model-deployment.bicep    # deploys selected model version to endpoint
│  └─ app.bicep                 # optional app host
└─ hooks/
   ├─ preprovision.sh           # validates selected model version exists
   └─ postprovision.sh          # writes endpoint/deployment outputs
```

```bash
# Select which stored model version should be served.
azd env set MODEL_NAME sdxl
azd env set MODEL_VERSION 1.0.3
azd up

# Later: deploy a different registered version without changing app code.
azd env set MODEL_VERSION 1.0.4
azd up
```

### 19.3 Pattern B: Separate azd projects or commands

Use separate azd projects when the model artifact pipeline is materially different from the app deployment pipeline, such as when one repo publishes model versions and another repo controls production serving. This is the cleaner enterprise/MLOps boundary.

| azd project / command | Purpose | Example trigger |
|---|---|---|
| `model-registry azd up` | Creates durable storage/registry and publishes or imports versioned model artifacts. | New SDXL weights, new LoRA merge, safety-approved model refresh. |
| `foundry-deployment azd up` | Creates endpoint/deployment and selects the model version to serve. | Release train, canary, rollback, region-specific deployment. |
| `app azd deploy` | Deploys front end/API only. | UI/API change with no model change. |

| Rule of thumb |
|---|
| If model versions need approval, retention, rollback, or reuse across multiple endpoints, keep publishing and deployment as separate commands or repos. If the sample is primarily educational and one-developer focused, keep one azd project and split the Bicep modules. |

### 19.4 Versioned model artifact options

| Storage option | Best fit | How deployment selects version | Notes |
|---|---|---|---|
| Azure ML registry / model registry | Reusable enterprise model assets with version metadata. | Bicep/CLI parameter points to model name + version. | Best conceptual fit for governed versioned model selection. |
| Azure Storage account + manifest | Simple samples or custom container flow. | Deployment reads `MODEL_VERSION` and resolves a blob path or manifest entry. | Needs clear manifest discipline and integrity checks. |
| Container image tags in ACR | BYOC inference container where model weights are baked into, or mounted by, image version. | Deployment selects image tag/digest. | Useful when inference runtime and weights must be versioned together. |
| Hugging Face / Foundry catalog reference | Model remains externally cataloged; no local artifact copy. | Deployment uses catalog model metadata/version/SKU. | Lowest storage ownership, but less control over artifact retention/version policy. |

### 19.5 Bicep parameter contract

Define the model selection as an explicit contract. That lets the same endpoint deployment module point to a catalog model, model registry version, storage-backed artifact, or containerized fallback without changing application code.

```bicep
param modelSource string // catalog | registry | storage | container
param modelName string
param modelVersion string
param modelRegistryName string = ''
param modelArtifactUri string = ''
param modelImage string = ''
param deploymentName string
param endpointName string

// main.bicep passes these parameters into model-deployment.bicep.
// preprovision.sh validates the selected source/version before deployment.
```

### 19.6 Recommended sample implementation

- Keep one azd project for the sample repo, because it preserves a simple developer workflow.
- Split Bicep into `model-storage`, `foundry-project`, `model-deployment`, and `app` modules.
- Treat model storage as durable and versioned; do not delete it on every app redeploy unless explicitly requested.
- Make `MODEL_SOURCE`, `MODEL_NAME`, and `MODEL_VERSION` azd environment values.
- Add a preprovision validation script that fails if the requested model version does not exist.
- Document a two-repo/two-command enterprise path for teams that need independent model publishing and app deployment lifecycles.
