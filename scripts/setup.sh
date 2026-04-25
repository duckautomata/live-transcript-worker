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

# --- Tool Download ---
echo -e "\nDownloading yt-dlp..."
mkdir -p bin
curl -L https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp_linux -o bin/yt-dlp
chmod a+rx bin/yt-dlp
echo "yt-dlp has been successfully downloaded to bin/yt-dlp."

# --- bgutil PO Token plugin (paired with the bgutil-provider sidecar) ---
# Only used when `server.pot_provider.enabled` is true in the config.
# yt-dlp expects <plugin-dirs>/<pkg>/yt_dlp_plugins/... so we extract into a
# named subdirectory.
echo -e "\nDownloading bgutil-ytdlp-pot-provider plugin..."
mkdir -p yt-dlp-plugins/bgutil-ytdlp-pot-provider
curl -L https://github.com/Brainicism/bgutil-ytdlp-pot-provider/releases/latest/download/bgutil-ytdlp-pot-provider.zip -o /tmp/bgutil.zip
unzip -o /tmp/bgutil.zip -d yt-dlp-plugins/bgutil-ytdlp-pot-provider
rm /tmp/bgutil.zip
echo "bgutil plugin extracted to yt-dlp-plugins/bgutil-ytdlp-pot-provider/."

# --- Python Environment Setup ---
echo -e "\nInstalling dependencies..."
uv sync
echo "Dependencies installed successfully."

echo -e "\nSetup complete! You can now create a config file and run the program."
