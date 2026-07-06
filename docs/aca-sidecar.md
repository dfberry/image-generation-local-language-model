# Running local models on Azure Container Apps: is a sidecar the right pattern?

This guide is a decision framework for hosting a **local model** (LLM or image
model) alongside an application on **Azure Container Apps (ACA)**. It explains
what the sidecar pattern is, when it helps, when it hurts, and which pattern to
reach for instead.

> **TL;DR for this repo:** For the SDXL image-generation service, a sidecar is
> the **wrong** fit — the model is large, GPU-ideal, and generation is bursty.
> Prefer a **separate Container App** (+ an Azure Files model cache, + a
> GPU workload profile). See [Rule of thumb](#rule-of-thumb).

## What a sidecar actually is

A **sidecar** puts the model in a *second container inside the same ACA
replica*. Consequences:

- **Shared node** — the app and the model run on the same compute; a sidecar
  adds **zero** additional compute.
- **Shared localhost network** — the app talks to the model over `localhost`
  (e.g. `http://localhost:11434` for Ollama). No ingress, no auth, no network
  hop.
- **Shared lifecycle** — they deploy, start, stop, and **scale together**. One
  app replica = one model instance.

## When a sidecar is a GOOD idea

A sidecar is a good fit when *most* of these hold:

1. **Small / quantized model** — e.g. Phi-3-mini, TinyLlama, a small GGUF served
   by Ollama or llama.cpp. It fits comfortably in the replica's CPU + RAM
   *alongside* the app.
2. **Tight 1:1 coupling** — nearly every app request calls the model, and you
   want zero network hop, no separate ingress, and no auth between them.
3. **Lockstep scaling is desirable** — "one app replica = one model instance" is
   the behavior you actually want.
4. **Always-warm, latency-sensitive, low-to-moderate concurrency** — the model
   sits hot next to the app with no per-call cold start.
5. **Operational simplicity matters** — one app, one revision, one deploy, with
   nothing else to manage.

## When a sidecar is a BAD idea

Avoid a sidecar when *any* of these hold:

1. **Large model** (SDXL ~7 GB, 13B+ LLMs) — it bloats every replica, slows cold
   starts, and **each app replica duplicates the full model**, wasting memory.
2. **You need a GPU** — a sidecar shares the node, so scaling the app scales GPU
   cost; you cannot cheaply give *only* the model a GPU profile.
3. **App load != model load** — a bursty, light app paired with a heavy model (or
   vice versa). Coupling them wastes resources; a separate app scales
   independently and can **scale to zero**.
4. **Model reused across multiple apps** — a sidecar is private to its app. A
   separate Container App (internal ingress) or a managed endpoint can serve
   many callers.
5. **Different deploy cadence** — you don't want to redeploy a multi-GB model
   every time you tweak a button in the UI.

## Rule of thumb

| Model shape | Best pattern |
|---|---|
| Small, always-needed, latency-critical | **Sidecar** (same replica) |
| Large / GPU / bursty / shared | **Separate ACA app** (+ Azure Files model cache, + GPU workload profile) |
| Enterprise scale / SLA / autoscale | **Managed endpoint** (Azure ML online endpoint, Azure OpenAI) |

## Alternatives to the sidecar

### 1. Separate Container App (recommended for large/GPU/bursty models)

Deploy the model as its **own** Container App and connect the front end to it.

- Expose the model app over **internal ingress** (VNet-only, not public).
- Use a **managed identity** for keyless auth between apps.
- Scale the model app independently — including **scale-to-zero** when idle.
- Give the model app its own workload profile (e.g. a **serverless GPU**
  profile) without inflating the front-end's cost.

### 2. Azure Files model cache (fixes cold-start re-downloads)

Large weights should not live only on the container's ephemeral disk — every
cold start or scale event re-downloads them.

- Mount an **Azure Files** share at the model cache path (e.g. the Hugging Face
  cache dir).
- Weights persist across restarts and are shared by all replicas.
- Alternatively, **bake the weights into the image** for immutable, fast starts
  (at the cost of a larger image).

### 3. Managed endpoint (enterprise scale)

For SLA-backed, autoscaled inference, use a managed service — Azure ML online
endpoints or Azure OpenAI — instead of self-hosting the model in ACA at all.

## How this applies to the SDXL service in this repo

- **Model:** SDXL is a ~7 GB diffusion model — large, and dramatically faster on
  a GPU.
- **Traffic:** image generation is **bursty** (occasional heavy requests), while
  the UI/health endpoints are light and should stay warm.
- **Conclusion:** don't co-locate SDXL as a sidecar. The cloud-performant path
  is **separate app + Azure Files cache + GPU profile**:
  1. **Serverless GPU** workload profile for the inference container — the single
     biggest performance lever (seconds vs. minutes per image on CPU).
  2. **Azure Files** mount for the model cache so the 7 GB weights survive cold
     starts and scale events instead of re-downloading.
  3. Optionally split **UI <-> inference worker** into two apps for independent
     scale-to-zero.

A sidecar *would* make sense here only for a **small helper** — for example,
embedding a quantized Phi-3 next to the SDXL API to rewrite or expand prompts:
lightweight, always needed, and cheap to co-locate.
