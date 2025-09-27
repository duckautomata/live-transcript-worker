#!/bin/bash

# A script to run the Docker image in the background.
#
# Usage: ./start.sh

# --- Configuration ---
IMAGE_NAME="duckautomata/live-transcript-worker"
TAG="latest"
CONTAINER_NAME="live_transcript_worker"
RESTART_POLICY="always"

# --- Dynamic Paths (using absolute paths for reliability) ---
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

# Check if a container with the same name is already running
# Using '^' and '$' to ensure an exact name match.
if [ $($DOCKER_CMD ps -q -f name="^${CONTAINER_NAME}$") ]; then
    echo "Error: A container named '$CONTAINER_NAME' is already running."
    echo "To stop or update it, please use other scripts like './cleanup.sh' or './update.sh'."
    exit 1
fi

# --- Docker Command ---

echo "Starting container: $CONTAINER_NAME"
$DOCKER_CMD run \
    --name "$CONTAINER_NAME" \
    --gpus all \
    -d --restart "$RESTART_POLICY" \
    -v "$CONFIG_FILE_PATH:/app/config/config.yaml:ro,z" \
    -v "$TMP_DIR:/app/tmp:z" \
    -v "$MODELS_DIR:/app/models:z" \
    "$IMAGE_NAME:$TAG"

# --- Post-run Check ---
# Give the container a moment to start up or fail
sleep 2
if ! $DOCKER_CMD ps -q -f name="^${CONTAINER_NAME}$" > /dev/null; then
    echo "Error: Container failed to start. Check the logs for more details:"
    echo "   $DOCKER_CMD logs $CONTAINER_NAME"
    exit 1
fi

echo "Container '$CONTAINER_NAME' started successfully."
