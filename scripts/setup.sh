#!/bin/bash

# A script to set up the development environment. This will not verify if nvidia drivers are installed correctly.
#
# Usage: ./scripts/setup.sh

# Exit immediately if a command exits with a non-zero status.
set -e

# --- Path and Environment Setup ---
# This ensures the script always runs from the project's root directory.
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &> /dev/null && pwd)
PROJECT_ROOT=$(dirname "$SCRIPT_DIR")
cd "$PROJECT_ROOT" || exit 1

echo "Running setup from project root: $PWD"

# --- Prerequisite Verification ---
echo -e "\nVerifying required tools..."

# Find a suitable Python command
if command -v python3.12 &>/dev/null; then
    PYTHON_CMD="python3.12"
elif command -v python3 &>/dev/null; then
    PYTHON_CMD="python3"
elif command -v python &>/dev/null; then
    PYTHON_CMD="python"
else
    echo "Error: Python is not installed. Please install Python 3."
    exit 1
fi
echo "Python found ($PYTHON_CMD)"

# Check if the venv module is installed for the found Python interpreter
if ! "$PYTHON_CMD" -m venv --help &>/dev/null; then
    echo "Error: The Python 'venv' module is missing or broken."
    echo "   On Debian/Ubuntu, try: sudo apt install python3-venv"
    echo "   On Fedora/CentOS, try: sudo dnf install python3-virtualenv"
    exit 1
fi
echo "Python venv module found"

# Check for other necessary tools
for tool in ffmpeg curl; do
    if ! command -v "$tool" &>/dev/null; then
        echo "Error: '$tool' is not installed. Please install it to continue."
        exit 1
    fi
    echo "✔️ $tool found"
done

# --- Tool Download ---
echo -e "\nDownloading yt-dlp..."
mkdir -p bin
curl -L https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp -o bin/yt-dlp
chmod a+rx bin/yt-dlp
echo "yt-dlp has been successfully downloaded to bin/yt-dlp."

# --- Python Environment Setup ---
VENV_PATH=".venv"

echo -e "\nSetting up Python virtual environment..."
if [ ! -d "$VENV_PATH" ]; then
    echo "   -> Creating virtual environment at '$VENV_PATH'..."
    "$PYTHON_CMD" -m venv "$VENV_PATH"
else
    echo "   -> Virtual environment already exists."
fi

# Install dependencies using the venv's pip directly
if [ -f "requirements.txt" ]; then
    echo "   -> Installing dependencies from requirements.txt..."
    "$VENV_PATH/bin/pip" install -r requirements.txt
    echo "   -> Dependencies installed successfully."
else
    echo "Error: requirements.txt not found. Cannot install dependencies."
    exit 1
fi

echo -e "\nSetup complete! You can now create a config file and run the program."
