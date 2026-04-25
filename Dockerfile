# NVIDIA base image
FROM nvidia/cuda:12.9.1-cudnn-runtime-ubuntu24.04

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

# Install system dependencies
RUN apt-get update && apt-get install -y \
    python3 \
    ffmpeg \
    curl \
    unzip \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Install uv
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:$PATH"

# Setting up app
WORKDIR /app
RUN mkdir -p /app/tmp /app/models

# Install dependencies via uv
COPY pyproject.toml .
COPY uv.lock .
RUN uv sync --no-dev

# Install Deno (needed for latest yt-dlp version)
ARG DENO_VERSION="unknown"
RUN echo "Installing Deno ${DENO_VERSION}" && \
    export DENO_INSTALL=/usr/local && \
    curl -fsSL "https://github.com/denoland/deno/releases/download/${DENO_VERSION}/deno-x86_64-unknown-linux-gnu.zip" \
         -o /tmp/deno.zip && \
    unzip -o /tmp/deno.zip -d /usr/local/bin && \
    rm /tmp/deno.zip && \
    chmod a+rx /usr/local/bin/deno

# Copy application files
COPY main.py main.py
COPY live_transcript_worker live_transcript_worker

VOLUME ["/app/tmp", "/app/models"]

ARG APP_VERSION="unknown"
ARG BUILD_DATE="unknown"
ENV APP_VERSION=${APP_VERSION}
ENV BUILD_DATE=${BUILD_DATE}

CMD ["uv", "run", "--no-dev", "main.py"]
