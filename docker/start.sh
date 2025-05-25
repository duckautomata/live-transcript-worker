#!/bin/bash

# A script to run the Docker image in the background.
#
# Usage: ./start.sh

# --- Configuration ---
CONTAINER_NAME="live_transcript_worker"
TAG="latest"
RESTART_POLICY="unless-stopped"
CONFIG_FILE_PATH="./config.yaml"

if [ -f "$CONFIG_FILE_PATH" ]; then
    echo "Configuration file '$CONFIG_FILE_PATH' found."
else
    echo "Error: Configuration file '$CONFIG_FILE_PATH' does not exist."
    exit 1
fi

echo "Starting container $CONTAINER_NAME"
docker run \
    --name $CONTAINER_NAME \
    --gpus all \
    -d --restart $RESTART_POLICY \
    -v "$CONFIG_FILE_PATH:/app/config/config.yaml:ro" \
    -v "./docker-tmp:/app/tmp" \
    -v "./docker-models:/app/models" \
    duckautomata/live-transcript-worker:$TAG
