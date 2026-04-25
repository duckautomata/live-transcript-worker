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
RUN mkdir -p /app/tmp /app/models /app/bin

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

# Install yt-dlp binary
ARG YTDLP_VERSION="unknown"
RUN echo "Installing yt-dlp ${YTDLP_VERSION}" && \
    curl -L "https://github.com/yt-dlp/yt-dlp/releases/download/${YTDLP_VERSION}/yt-dlp" \
         -o /app/bin/yt-dlp && \
    chmod a+rx /app/bin/yt-dlp

# Install bgutil-ytdlp-pot-provider plugin (HTTP mode; pairs with the
# bgutil-provider sidecar in docker-compose.yml). The plugin is loaded via
# yt-dlp's --plugin-dirs flag, set in helper.ytdlp_auth_args.
ARG BGUTIL_VERSION="unknown"
RUN echo "Installing bgutil-ytdlp-pot-provider ${BGUTIL_VERSION}" && \
    mkdir -p /app/yt-dlp-plugins && \
    curl -L "https://github.com/Brainicism/bgutil-ytdlp-pot-provider/releases/download/${BGUTIL_VERSION}/bgutil-ytdlp-pot-provider.zip" \
         -o /tmp/bgutil.zip && \
    unzip -o /tmp/bgutil.zip -d /app/yt-dlp-plugins && \
    rm /tmp/bgutil.zip

# Copy application files
COPY main.py main.py
COPY live_transcript_worker live_transcript_worker

VOLUME ["/app/tmp", "/app/models"]

ARG APP_VERSION="unknown"
ARG BUILD_DATE="unknown"
ENV APP_VERSION=${APP_VERSION}
ENV BUILD_DATE=${BUILD_DATE}

CMD ["uv", "run", "--no-dev", "main.py"]
