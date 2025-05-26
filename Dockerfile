# NVIDIA base image
ARG CUDA_VERSION=12.3.2
ARG CUDNN_VERSION=9
ARG OS_VERSION=ubuntu22.04
FROM nvidia/cuda:${CUDA_VERSION}-cudnn${CUDNN_VERSION}-runtime-${OS_VERSION}

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

# Install Python and other system dependencies and creating symlink for python and pip
RUN apt-get update && apt-get install -y \
    python3-pip \
    ffmpeg \
    curl \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/bin/python3 /usr/bin/python \
    && ln -sf /usr/bin/pip3 /usr/bin/pip

# Setting up app
WORKDIR /app
RUN mkdir -p /app/tmp /app/model
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install yt-dlp binary
RUN mkdir -p /app/bin/ && \
    curl -L https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp -o /app/bin/yt-dlp && \
    chmod a+rx /app/bin/yt-dlp

# Copy application files
COPY main.py main.py
COPY src src

RUN chown -R 1000:1000 /app
VOLUME ["/app/tmp", "/app/models"]
USER 1000

CMD ["python3", "main.py"]
