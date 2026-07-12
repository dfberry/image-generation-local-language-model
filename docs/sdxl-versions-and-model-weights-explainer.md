<!--
Source: sdxl-versions-and-model-weights-explainer.docx
Origin: SharePoint (OneDrive) — diberry@microsoft.com / Documents/Copilot/Created
Copied into repo: 2026-07-12
Note: This is a faithful Markdown mirror of the Word document, reproduced
section-by-section. Reference footnote markers from the source have been
removed for readability; content is otherwise verbatim.
-->

# SDXL Versions, Model Weights, and Deployment Selection

> **Bottom line**
> Yes, "SDXL" has multiple practical versions or variants. In deployment design, treat SDXL as a model family and make the exact deployable asset explicit: model source + model name + model version. If you are only calling an endpoint, you may never touch the model weights directly; if you are running or packaging SDXL yourself, the weights are the large learned files that make that variant behave the way it does.

---

## 1. What "model weights" means

A model is not just code. It combines architecture, configuration, tokenizer/text encoders or pipeline components, and the trained parameters. Those trained parameters are commonly called model weights.

```text
Model architecture + configuration + model weights = runnable model
```

For SDXL-style models, the weight files are often large files such as:

- `.safetensors` checkpoint files
- multi-file Diffusers folders
- related VAE / text encoder / UNet components, depending on packaging

- **If you call a hosted endpoint:** you are using a model, but the hosting platform manages the weights behind the endpoint.
- **If you run ComfyUI, Automatic1111, InvokeAI, or Diffusers locally:** you usually download and load the model weights yourself.
- **If you build a custom Azure container:** you need to decide whether weights are in the image, mounted from storage, pulled from Hugging Face, or resolved from a model registry.

---

## 2. Why SDXL has "many versions" in practice

People often use "SDXL" loosely. They might mean the original Stable Diffusion XL base model, a companion refiner, a faster distilled version, or a community/custom fine-tune derived from SDXL. These are not interchangeable from an infrastructure perspective because each deployable artifact can have different files, licensing, inference behavior, hardware requirements, and API assumptions.

| Term people use | What it likely means | Are these separate weights? | Deployment implication |
| --- | --- | --- | --- |
| SDXL Base 1.0 | The base Stable Diffusion XL model/checkpoint. | Yes | Treat as one deployable model asset/version. |
| SDXL Refiner 1.0 | A companion model used to refine/improve output in some workflows. | Yes | May require a two-stage workflow, not just one endpoint. |
| SDXL Turbo | A fast/distilled SDXL-family model optimized for few-step generation. | Yes | Different inference settings and possibly different output profile. |
| Juggernaut XL / RealVisXL / DreamShaper XL | Community fine-tuned SDXL-derived models. | Yes | Treat as separate model names, not just versions of the same artifact. |
| Custom SDXL + LoRA | A base SDXL model with adaptation weights applied. | Usually yes, plus LoRA files | Need clear promotion/versioning of both base model and adaptation. |

### 2.1 SDXL Base 1.0

The base Stable Diffusion XL model/checkpoint.

- Yes
- Treat as one deployable model asset/version.

### 2.2 SDXL Refiner 1.0

A companion model used to refine/improve output in some workflows.

- Yes
- May require a two-stage workflow, not just one endpoint.

### 2.3 SDXL Turbo

A fast/distilled SDXL-family model optimized for few-step generation.

- Yes
- Different inference settings and possibly different output profile.

### 2.4 Juggernaut XL / RealVisXL / DreamShaper XL

Community fine-tuned SDXL-derived models.

- Yes
- Treat as separate model names, not just versions of the same artifact.

### 2.5 Custom SDXL + LoRA

A base SDXL model with adaptation weights applied.

- Usually yes, plus LoRA files
- Need clear promotion/versioning of both base model and adaptation.

---

## 3. How this maps to Azure / Foundry design

```text
Versioned model storage / registry
  SDXL-base-1.0
  SDXL-refiner-1.0
  SDXL-turbo
  org-custom-sdxl-v1
  org-custom-sdxl-v2

        ↓ selected by parameters

Foundry or custom endpoint deployment

        ↓ stable endpoint contract

Application
```

The most important design move is to separate the artifact from the serving endpoint. The artifact can be versioned and governed. The deployment simply points to one selected artifact/version.

### 3.1 Versioned model storage / registry (Foundry catalog deployment)

- For a Foundry catalog deployment, the "model version" is the catalog/provider metadata you select; Foundry or the provider handles the weights.

### 3.2 Foundry or custom endpoint deployment (Azure ML registry or custom storage)

- For Azure ML registry or custom storage, the "model version" is your own governed artifact version.

### 3.3 Application (custom container hosting)

- For custom container hosting, the "version" might be an Azure ML model version, a blob path/manifest entry, or a container image digest/tag.
- For SDXL variants, do not assume "version" only means 1.0, 1.1, etc. A community checkpoint is better modeled as a different model name, with its own versions.

---

## 4. Recommended azd/Bicep parameter contract

Make the selected model explicit in environment variables and Bicep parameters. This keeps the app endpoint stable while letting you switch model versions or roll back.

```bash
azd env set MODEL_SOURCE registry       # catalog | registry | storage | container
azd env set MODEL_NAME sdxl-base
azd env set MODEL_VERSION 1.0.0
azd up

# Promote or roll back by changing the deployed model version.
azd env set MODEL_VERSION 1.0.1
azd up
```

```bicep
param modelSource string
param modelName string
param modelVersion string
param endpointName string
param deploymentName string

// model-deployment.bicep resolves modelSource + modelName + modelVersion
// and deploys the selected artifact to the endpoint.
```

---

## 5. Recommendation for your spec

### 5.1 Spec wording to use

| Spec wording to use |
| --- |
| SDXL should be described as a model family. The sample should require explicit model selection through MODEL_SOURCE, MODEL_NAME, and MODEL_VERSION. For catalog-hosted Hugging Face models, the sample selects provider/catalog metadata. For custom SDXL checkpoints or LoRAs, the sample should use a versioned model registry or storage manifest, then deploy the selected artifact to Foundry or a custom GPU endpoint. |

- Use one azd project for a simple developer sample, but split Bicep modules into model storage, Foundry project, model deployment, and app.
- Use separate azd projects/commands if model publishing and app deployment are owned by different teams or require separate approval gates.
- Do not make "SDXL" a single hard-coded deployment. Make the exact model/variant/version selectable.
- Document rollback as changing MODEL_VERSION and redeploying the endpoint module, not rebuilding the app.

---

## 6. Sources checked

| Source | What it supports | URL |
| --- | --- | --- |
| Hugging Face model card: stabilityai/sdxl-turbo | SDXL-Turbo is a generative text-to-image model, distilled from SDXL 1.0, using Safetensors/Diffusers packaging. | https://huggingface.co/stabilityai/sdxl-turbo |
| Hugging Face Diffusers SDXL Turbo docs | SDXL Turbo uses the same architecture/API as SDXL and is designed for 1–4 step generation; docs also mention model weights may be stored in Hub/local subfolders. | https://huggingface.co/docs/diffusers/main/en/api/pipelines/stable_diffusion/sdxl_turbo |
| Hugging Face search results | Examples of SDXL-family or SDXL-related model pages such as base/refiner, Turbo, quantized, and fine-tuned variants. | https://huggingface.co/models |

---

_Draft note for planning. Verify exact model availability, licenses, and endpoint support before committing implementation details._
