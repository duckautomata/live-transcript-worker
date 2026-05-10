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
# Two yt-dlp binaries are used:
#   bin/yt-dlp       — upstream yt-dlp; used for stream-stats checks and every
#                      worker except SABRWorker.
#   bin/yt-dlp-sabr  — fork from duckautomata/yt-dlp-sabr that adds the SABR
#                      live-from-start downloader; used only by SABRWorker.
# The SABR fork tag is sourced from the Dockerfile (single source of truth) so
# bumping the version there propagates everywhere.
SABR_FORK_TAG=$(grep -E '^ARG SABR_YTDLP_VERSION=' Dockerfile | sed -E 's/.*"([^"]+)".*/\1/')
if [ -z "$SABR_FORK_TAG" ]; then
    echo "Error: could not parse SABR_YTDLP_VERSION from Dockerfile."
    exit 1
fi

echo -e "\nDownloading yt-dlp (upstream)..."
mkdir -p bin
curl -L https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp_linux -o bin/yt-dlp
chmod a+rx bin/yt-dlp
echo "yt-dlp has been successfully downloaded to bin/yt-dlp."

echo -e "\nDownloading yt-dlp-sabr fork ($SABR_FORK_TAG)..."
curl -L "https://github.com/duckautomata/yt-dlp-sabr/releases/download/$SABR_FORK_TAG/yt-dlp" -o bin/yt-dlp-sabr
chmod a+rx bin/yt-dlp-sabr
echo "yt-dlp-sabr has been successfully downloaded to bin/yt-dlp-sabr."

# --- Python Environment Setup ---
echo -e "\nInstalling dependencies..."
uv sync
echo "Dependencies installed successfully."

echo -e "\nSetup complete! You can now create a config file and run the program."
