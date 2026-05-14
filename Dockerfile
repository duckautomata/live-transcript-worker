# NVIDIA base image — digest-pinned so a retag at the registry cannot silently
# change the contents of our build. Bumps for this image are deliberately
# ignored in .github/dependabot.yml; update by hand only.
FROM nvidia/cuda:12.9.1-cudnn-runtime-ubuntu24.04@sha256:d02c4310b6d57ca0b16cd80298bdb33a74187baafe2eccd8a6a16180ddc90802

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
# uv hardening: at runtime, refuse to re-resolve. The lockfile is authoritative
# and `uv run` must not silently sync from the network inside a running container.
ENV UV_FROZEN=1 \
    UV_NO_SYNC=1

# Install system dependencies
RUN apt-get update && apt-get install -y \
    python3 \
    ffmpeg \
    curl \
    unzip \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Install uv from the official Astral image (digest-pinned). Dependabot's
# docker ecosystem will bump both the tag and the digest together on new
# releases of uv.
COPY --from=ghcr.io/astral-sh/uv:0.5.18@sha256:e2101b9e627153b8fe4e8a1249cc4194f1b38ece7f28a5a9b8f958e3b560e69c /uv /uvx /usr/local/bin/

# Setting up app
WORKDIR /app
RUN mkdir -p /app/tmp /app/models /app/bin

# Install dependencies via uv. --frozen: install exactly what uv.lock says, never
# re-resolve, never touch the network beyond fetching the locked wheels.
COPY pyproject.toml .
COPY uv.lock .
RUN uv sync --no-dev --frozen

# Install Deno (needed for latest yt-dlp version), verified by SHA256.
ARG DENO_VERSION="unknown"
RUN echo "Installing Deno ${DENO_VERSION}" && \
    cd /tmp && \
    curl -fsSL "https://github.com/denoland/deno/releases/download/${DENO_VERSION}/deno-x86_64-unknown-linux-gnu.zip" \
         -o deno-x86_64-unknown-linux-gnu.zip && \
    curl -fsSL "https://github.com/denoland/deno/releases/download/${DENO_VERSION}/deno-x86_64-unknown-linux-gnu.zip.sha256sum" \
         -o deno-x86_64-unknown-linux-gnu.zip.sha256sum && \
    sha256sum -c deno-x86_64-unknown-linux-gnu.zip.sha256sum && \
    unzip -o deno-x86_64-unknown-linux-gnu.zip -d /usr/local/bin && \
    rm deno-x86_64-unknown-linux-gnu.zip deno-x86_64-unknown-linux-gnu.zip.sha256sum && \
    chmod a+rx /usr/local/bin/deno

# Install yt-dlp binary, verified against the release's SHA2-256SUMS manifest.
ARG YTDLP_VERSION="unknown"
RUN echo "Installing yt-dlp ${YTDLP_VERSION}" && \
    cd /app/bin && \
    curl -fsSL "https://github.com/yt-dlp/yt-dlp/releases/download/${YTDLP_VERSION}/yt-dlp" -o yt-dlp && \
    curl -fsSL "https://github.com/yt-dlp/yt-dlp/releases/download/${YTDLP_VERSION}/SHA2-256SUMS" -o SHA2-256SUMS && \
    grep '  yt-dlp$' SHA2-256SUMS | sha256sum -c - && \
    rm SHA2-256SUMS && \
    chmod a+rx yt-dlp

# Copy application files
COPY main.py main.py
COPY live_transcript_worker live_transcript_worker

VOLUME ["/app/tmp", "/app/models"]

ARG APP_VERSION="unknown"
ARG BUILD_DATE="unknown"
ENV APP_VERSION=${APP_VERSION}
ENV BUILD_DATE=${BUILD_DATE}

CMD ["uv", "run", "--no-dev", "main.py"]
