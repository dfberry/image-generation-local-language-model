# GPU CLI image — requires NVIDIA GPU + NVIDIA Container Toolkit.
# Model downloads on first run into a persistent cache volume (download once, reuse every run).
# NOT for Windows devbox / WSL2 without GPU passthrough — use Dockerfile.cpu instead.
# Usage (via docker compose — recommended):
#   docker compose -f docker-compose.gpu.yml run --rm img-gen --batch-file /data/batch.json
# Direct run (verify GPU first with: nvidia-smi):
#   docker build -t sdxl-cli:gpu .
#   docker run --gpus all --rm -v $(pwd):/data -v hf-cache:/root/.cache/huggingface sdxl-cli:gpu --batch-file /data/batch.json
FROM nvidia/cuda:12.1.1-runtime-ubuntu22.04

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app/src \
    HF_HOME=/root/.cache/huggingface

RUN apt-get update && apt-get install -y \
    python3.11 \
    python3-pip \
    python3-venv \
    git \
    libgl1 \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender-dev \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --upgrade pip setuptools wheel && \
    pip install -r requirements.txt

# Copy application code
COPY app.py .
COPY src/ ./src/

WORKDIR /data
VOLUME ["/data"]

ENTRYPOINT ["python3", "-m", "image_generation.generate"]
CMD ["--help"]
