#!/bin/bash

# A script to set up the development environment. This will not verify if nvidia drivers are installed correctly.
#
# Usage: ./scripts/setup.sh

if [[ "$PWD" == */scripts ]]; then
    echo "Error: This script must be run from the project's root directory, not from within the 'scripts' subdirectory."
    echo "You are currently in: $PWD"
    echo "Please change to the parent directory (e.g., 'cd ..') and run the script like this: ./scripts/setup.sh"
    exit 1
fi

# --- Verification Section ---

echo ""
echo "----------------------------------------"

echo "### Verifying FFmpeg installation... ###"
if command -v ffmpeg &> /dev/null; then
    echo "FFmpeg is installed."
else
    echo "Error: FFmpeg is not installed. Please install it to continue."
    exit 1
fi

# --- Tool Download Section ---

echo "### Downloading yt-dlp... ###"
# Create the bin directory if it doesn't exist
mkdir -p bin
if [ $? -ne 0 ]; then
    echo "Error: Failed to create the 'bin' directory."
    exit 1
fi

echo "Downloading the latest version of yt-dlp to bin/yt-dlp..."
# Use curl to download, -L follows redirects, -o specifies the output file, overwriting if it exists
curl -L https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp -o bin/yt-dlp
if [ $? -ne 0 ]; then
    echo "Error: Failed to download yt-dlp."
    exit 1
fi

echo "Making yt-dlp executable..."
chmod a+rx bin/yt-dlp
if [ $? -ne 0 ]; then
    echo "Error: Failed to make yt-dlp executable."
    exit 1
fi

echo "yt-dlp has been successfully downloaded and placed in bin/yt-dlp."

echo ""
echo "----------------------------------------"


# --- Environment Setup Section ---

echo "### Creating Python virtual environment and installing dependencies... ###"
if [ ! -d ".venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv .venv
    if [ $? -ne 0 ]; then
        echo "Error: Failed to create the virtual environment."
        exit 1
    fi
else
    echo "Virtual environment '.venv' already exists."
fi

echo "Activating the virtual environment..."
source .venv/bin/activate

if [ -f "requirements.txt" ]; then
    echo "Installing dependencies from requirements.txt..."
    pip install -r requirements.txt
    if [ $? -ne 0 ]; then
        echo "Error: Failed to install dependencies from requirements.txt."
        exit 1
    fi
    echo "Dependencies installed successfully."
else
    echo "Error: requirements.txt not found. Cannot install Python dependencies."
    exit 1
fi

echo ""
echo "Setup complete! You can now create your config file and run the program"
