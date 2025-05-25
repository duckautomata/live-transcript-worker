#!/bin/bash

# A script to run the python program using the virtual environment .venv - mainly used for development.
#
# Usage: ./scripts/run.sh [config_name]
# Example: ./scripts/run.sh dev.yaml

if [[ "$PWD" == */scripts ]]; then
    echo "Error: This script must be run from the project's root directory, not from within the 'scripts' subdirectory."
    echo "You are currently in: $PWD"
    echo "Please change to the parent directory (e.g., 'cd ..') and run the script like this: ./scripts/run.sh"
    exit 1
fi

set -e

# --- Configuration ---
YT_DLP_PATH="./bin/yt-dlp"
VENV_PATH=".venv"
PYTHON_SCRIPT="main.py"
CONFIG_DIR="config"
DEFAULT_CONFIG_FILE="config.yaml"

# --- Determine Configuration File ---
CONFIG_FILE_NAME=""

# If no argument is given, use the default. If one is given, use that.
# If more than one argument is given, exit with an error.
if [ "$#" -eq 0 ]; then
    echo "No configuration file specified, defaulting to '$DEFAULT_CONFIG_FILE'."
    CONFIG_FILE_NAME="$DEFAULT_CONFIG_FILE"
elif [ "$#" -eq 1 ]; then
    CONFIG_FILE_NAME="$1"
    echo "Using specified configuration file: '$CONFIG_FILE_NAME'."
else
    echo "Error: Please provide 0 or 1 arguments."
    echo "Usage: $0 [config_filename]"
    exit 1
fi

CONFIG_FILE_PATH="$CONFIG_DIR/$CONFIG_FILE_NAME"

# Check if the determined config file exists in the config directory.
if [ ! -f "$CONFIG_FILE_PATH" ]; then
    echo "Error: Configuration file not found at '$CONFIG_FILE_PATH'"
    exit 1
fi

echo "Using configuration file: $CONFIG_FILE_NAME"

# --- Update yt-dlp ---
echo "Attempting to update yt-dlp..."

# Check if the yt-dlp binary exists at the specified path
if [ -f "$YT_DLP_PATH" ]; then
    # Ensure the binary is executable
    if [ ! -x "$YT_DLP_PATH" ]; then
        echo "Making $YT_DLP_PATH executable..."
        chmod +x "$YT_DLP_PATH"
    fi
    echo "Running self-update for $YT_DLP_PATH..."
    "$YT_DLP_PATH" -U
    echo "yt-dlp update attempt finished."
else
    echo "Error: $YT_DLP_PATH not found."
    echo "Please ensure yt-dlp is located at '$YT_DLP_PATH'"
    exit 1
fi

# --- Activate virtual environment ---
if [ -d "$VENV_PATH" ] && [ -f "$VENV_PATH/bin/activate" ]; then
    echo "Activating virtual environment..."
    source "$VENV_PATH/bin/activate"
    echo "Virtual environment activated."
else
    echo "Error: Virtual environment not found at '$VENV_PATH' or activate script is missing."
    exit 1
fi

# --- Run Python script ---
if [ -f "$PYTHON_SCRIPT" ]; then
    echo "Running $PYTHON_SCRIPT with config $CONFIG_FILE_NAME..."
    python "$PYTHON_SCRIPT" "$CONFIG_FILE_NAME"
else
    echo "Error: $PYTHON_SCRIPT not found."
    exit 1
fi

echo "Deactivating virtual environment..."
deactivate

echo "Script completed."
