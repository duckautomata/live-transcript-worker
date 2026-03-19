#!/bin/bash

# A script to run the python program using uv - mainly used for development.
#
# Usage: ./scripts/run.sh [config_name]
# Example: ./scripts/run.sh dev.yaml

# --- Path and Environment Setup ---
# Change to the project root directory
cd "$(dirname "$0")/.."
echo "Running from project root: $PWD"

# Exit immediately if a command exits with a non-zero status.
set -e

# --- Configuration ---
YT_DLP_PATH="./bin/yt-dlp"
PYTHON_SCRIPT="main.py"
CONFIG_DIR="config"
DEFAULT_CONFIG_FILE="config"

# --- Determine Configuration File ---
CONFIG_FILE_NAME=""
if [ "$#" -eq 0 ]; then
    echo "No configuration specified, defaulting to '$DEFAULT_CONFIG_FILE'."
    CONFIG_FILE_NAME="$DEFAULT_CONFIG_FILE"
elif [ "$#" -eq 1 ]; then
    CONFIG_FILE_NAME="$1"
    echo "Using specified configuration file: '$CONFIG_FILE_NAME'."
else
    echo "Error: Please provide 0 or 1 arguments for the config file."
    echo "   Usage: $0 [config_filename]"
    exit 1
fi

CONFIG_FILE_PATH="$CONFIG_DIR/$CONFIG_FILE_NAME.yaml"

if [ ! -f "$CONFIG_FILE_PATH" ]; then
    echo "Error: Configuration file not found at '$CONFIG_FILE_PATH'"
    exit 1
fi

echo "   Using configuration: $CONFIG_FILE_PATH"

# --- Update yt-dlp ---
echo -e "\nAttempting to update yt-dlp..."
if [ -f "$YT_DLP_PATH" ]; then
    chmod +x "$YT_DLP_PATH"
    "$YT_DLP_PATH" -U
    echo "   yt-dlp update attempt finished."
else
    echo "Error: yt-dlp not found at '$YT_DLP_PATH'"
    exit 1
fi

uv run main.py "$CONFIG_FILE_NAME.yaml"

echo -e "\nScript completed successfully."
