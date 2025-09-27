#!/bin/bash

# A script to update the Docker container with the latest image.
#
# Usage: ./update.sh

# --- Configuration ---
IMAGE_NAME="duckautomata/live-transcript-worker"
TAG="latest"
CONTAINER_NAME="live_transcript_worker"
RESTART_POLICY="always"

# --- Dynamic Paths (using absolute paths for reliability) ---
# This makes the script runnable from any directory.
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &> /dev/null && pwd)
CONFIG_FILE_PATH="$SCRIPT_DIR/config.yaml"
TMP_DIR="$SCRIPT_DIR/tmp"
MODELS_DIR="$SCRIPT_DIR/models"


# --- Pre-flight Checks ---

# Determine the correct Docker command (docker or sudo docker)
if command -v docker &> /dev/null && docker info > /dev/null 2>&1; then
    DOCKER_CMD="docker"
elif command -v sudo &> /dev/null && sudo docker info > /dev/null 2>&1; then
    DOCKER_CMD="sudo docker"
else
    echo "Error: Docker is not running or you lack permission to use it."
    echo "Please ensure the Docker daemon is active and that your user is in the 'docker' group or has suda access."
    exit 1
fi

# Check for the required config file
if [ ! -f "$CONFIG_FILE_PATH" ]; then
    echo "Error: Config file '$CONFIG_FILE_PATH' does not exist."
    exit 1
fi

# Create necessary host directories
mkdir -p "$TMP_DIR" "$MODELS_DIR"

# --- Update Process ---

# Stop and remove the existing container if it exists
if [ $($DOCKER_CMD ps -a -q -f name="^${CONTAINER_NAME}$") ]; then
    echo "Stopping and removing existing container: $CONTAINER_NAME"
    $DOCKER_CMD stop "$CONTAINER_NAME" > /dev/null
    $DOCKER_CMD rm -f "$CONTAINER_NAME" > /dev/null
else
    echo "No existing container named '$CONTAINER_NAME' found. Proceeding to create a new one."
fi

# Pull the latest image
echo "Pulling the latest image: $IMAGE_NAME:$TAG"
if ! $DOCKER_CMD pull "$IMAGE_NAME:$TAG"; then
    echo "Error: Failed to pull the Docker image. Please check your network connection and the image name."
    exit 1
fi

# Start the new container
echo "Starting new container: $CONTAINER_NAME"
$DOCKER_CMD run \
    --name "$CONTAINER_NAME" \
    --gpus all \
    -d --restart "$RESTART_POLICY" \
    -v "$CONFIG_FILE_PATH:/app/config/config.yaml:ro,z" \
    -v "$TMP_DIR:/app/tmp:z" \
    -v "$MODELS_DIR:/app/models:z" \
    "$IMAGE_NAME:$TAG"

# --- Post-run Check ---
sleep 2 # Give the container a moment to start
if ! $DOCKER_CMD ps -q -f name="^${CONTAINER_NAME}$" > /dev/null; then
    echo "Error: Container failed to start after update. Check the logs:"
    echo "   $DOCKER_CMD logs $CONTAINER_NAME"
    exit 1
fi

echo "Update complete. Container '$CONTAINER_NAME' is running with the latest image."
