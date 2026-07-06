# CPU-only base: Azure Container Apps has no GPU; python:3.11-slim provides
# Python 3.11 + pip without pulling a multi-GB CUDA runtime layer.
FROM python:3.11-slim

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Install system dependencies for image processing and OpenMP (torch)
RUN apt-get update && apt-get install -y \
    git \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender-dev \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Create app directory
WORKDIR /app

# Install CPU-only torch first so pip resolves torch==2.1.2 from the CPU index
# and skips the multi-GB CUDA wheel from PyPI.
COPY requirements.txt .
RUN pip install --upgrade pip setuptools wheel && \
    pip install torch==2.1.2 --index-url https://download.pytorch.org/whl/cpu && \
    pip install -r requirements.txt

# Copy application code
COPY app.py .
COPY src/ ./src/

# Create output directory
RUN mkdir -p /app/outputs

# Expose port
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health').read()" || exit 1

# Run application
CMD ["python3", "app.py"]
