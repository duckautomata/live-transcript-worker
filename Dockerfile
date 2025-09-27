# NVIDIA base image
FROM nvidia/cuda:12.9.1-cudnn-runtime-ubuntu24.04

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

# Install Python and other system dependencies and creating symlink for python and pip
RUN apt-get update && apt-get install -y \
    python3 \
    python3-pip \
    python3.12-venv \
    ffmpeg \
    curl \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Setting up app
WORKDIR /app
RUN mkdir -p /app/tmp /app/model
RUN python3 -m venv /app/venv
ENV PATH="/app/venv/bin:$PATH"
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install yt-dlp binary
RUN mkdir -p /app/bin/ && \
    curl -L https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp -o /app/bin/yt-dlp && \
    chmod a+rx /app/bin/yt-dlp && \
    /app/bin/yt-dlp -U

# Copy application files
COPY main.py main.py
COPY src src

RUN chown -R 1000:1000 /app
VOLUME ["/app/tmp", "/app/models"]
USER 1000

CMD ["python3", "main.py"]
