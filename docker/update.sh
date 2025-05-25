#!/bin/bash

# A script to update the Docker image.
#
# Usage: ./update.sh

# --- Configuration ---
CONTAINER_NAME="live_transcript_worker"
TAG="latest"
RESTART_POLICY="unless-stopped"
CONFIG_FILE_PATH="./config.yaml"

if [ -f "$CONFIG_FILE_PATH" ]; then
    echo "Config file '$CONFIG_FILE_PATH' found."
else
    echo "Error: Config file '$CONFIG_FILE_PATH' does not exist."
    exit 1
fi

docker stop $NAME
docker rm $NAME
docker rmi duckautomata/live-transcript-worker:$TAG
docker run \
    --name $CONTAINER_NAME \
    --gpus all \
    -d --restart $RESTART_POLICY \
    -v "$CONFIG_FILE_PATH:/app/config/config.yaml:ro" \
    -v "./tmp:/app/tmp" \
    -v "./models:/app/models" \
    duckautomata/live-transcript-worker:$TAG
