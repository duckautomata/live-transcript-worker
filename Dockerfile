# NVIDIA base image
FROM nvidia/cuda:13.2.0-cudnn-runtime-ubuntu24.04
ARG CACHEBUST=1
ARG APP_VERSION="unknown"
ARG BUILD_DATE="unknown"

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

# Install Deno (needed for latest yt-dlp version)
RUN echo "Cache bust: ${CACHEBUST}" && export DENO_INSTALL=/usr/local && \
    curl -fsSL https://deno.land/install.sh | sh

# Setting up app
WORKDIR /app
RUN mkdir -p /app/tmp /app/models /app/bin

# Install dependencies via uv
COPY pyproject.toml .
COPY uv.lock .
RUN echo "Cache bust: ${CACHEBUST}" && uv sync --no-dev

# Install yt-dlp binary
RUN echo "Cache bust: ${CACHEBUST}" && \
    curl -L https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp -o /app/bin/yt-dlp && \
    chmod a+rx /app/bin/yt-dlp && \
    /app/bin/yt-dlp -U

# Copy application files
COPY main.py main.py
COPY live_transcript_worker live_transcript_worker

VOLUME ["/app/tmp", "/app/models"]

ENV APP_VERSION=${APP_VERSION}
ENV BUILD_DATE=${BUILD_DATE}

CMD ["uv", "run", "--no-dev", "main.py"]
