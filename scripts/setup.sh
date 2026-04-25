#!/bin/bash

# A script to set up the development environment. This will not verify if nvidia drivers are installed correctly.
#
# Usage: ./scripts/setup.sh

# Exit immediately if a command exits with a non-zero status.
set -e

# --- Path and Environment Setup ---
# Change to the project root directory
cd "$(dirname "$0")/.."
echo "Running from project root: $PWD"

# --- Prerequisite Verification ---
echo -e "\nVerifying required tools..."
for tool in ffmpeg curl deno uv; do
    if ! command -v "$tool" &>/dev/null; then
        echo "Error: '$tool' is not installed. Please install it to continue."
        exit 1
    fi
    echo "$tool found"
done

# --- Python Environment Setup ---
echo -e "\nInstalling dependencies..."
uv sync
echo "Dependencies installed successfully."

echo -e "\nSetup complete! You can now create a config file and run the program."
