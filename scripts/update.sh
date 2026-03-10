#!/bin/bash

# A script to update the package version. Better this way so that we don't run into conflicting versions.
#
# Usage: ./scripts/update.sh

# --- Configuration ---
VENV_PATH=".venv"
PIP_EXE="$VENV_PATH/bin/pip"
PACKAGES=(
    "faster-whisper"
    "pyrefly"
    "pytest-cov"
    "pytest-mock"
    "requests"
    "ruff"
    "types-PyYAML"
)

# --- Path and Environment Setup ---
# Change to the project root directory
cd "$(dirname "$0")/.."
echo "Running from project root: $PWD"

# Exit immediately if a command exits with a non-zero status
set -e

# --- Verify Virtual Environment Pip ---
echo -e "\nVerifying virtual environment pip..."
if [ ! -x "$PIP_EXE" ]; then
    echo "Error: Pip executable not found or not executable at '$PIP_EXE'."
    echo "Make sure your virtual environment is created in the '.venv' directory."
    exit 1
fi

echo "Cleaning up current pip packages..."
"$PIP_EXE" freeze > requirements.txt

# Only run uninstall if requirements.txt has content (prevents pip errors)
if [ -s requirements.txt ]; then
    "$PIP_EXE" uninstall -r requirements.txt -y
else
    echo "No existing packages found to uninstall."
fi

echo "Installing requested packages..."
# The [@] expands the array so pip installs them all at once
"$PIP_EXE" install --no-cache-dir "${PACKAGES[@]}"

echo "Setting requirements with new versions..."
"$PIP_EXE" freeze > requirements.txt

echo "Update complete!"
