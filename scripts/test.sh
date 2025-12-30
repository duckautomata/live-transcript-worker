#!/bin/bash

# A script to run tests using the virtual environment .venv - mainly used for development.
#
# Usage: ./scripts/test.sh [options]
# Options:
#   --no-whisper: Skip whisper tests
#   --print: Print test output

# --- Path and Environment Setup ---
# This ensures the script always runs from the project's root directory.
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &> /dev/null && pwd)
PROJECT_ROOT=$(dirname "$SCRIPT_DIR")
cd "$PROJECT_ROOT" || exit 1

echo "Running from project root: $PWD"

# Exit immediately if a command exits with a non-zero status.
set -e

# --- Configuration ---
VENV_PATH=".venv"
PYTHON_EXE="$VENV_PATH/bin/python"

# --- Argument Parsing ---
IGNORE_WHISPER=""
EXTRA_ARGS=""
for arg in "$@"; do
    if [ "$arg" == "--no-whisper" ]; then
        IGNORE_WHISPER="--ignore-glob=tests/test_whisper_*.py"
    elif [ "$arg" == "--print" ]; then
        EXTRA_ARGS="$EXTRA_ARGS -s"
    fi
done

# --- Activate Virtual Environment ---
echo -e "\nActivating virtual environment..."
if [ -f "$PYTHON_EXE" ]; then
    source "$VENV_PATH/bin/activate"
    echo "   Virtual environment activated."
else
    echo "Error: Python virtual environment not found or is invalid."
    echo "   Expected Python executable at: '$PYTHON_EXE'"
    exit 1
fi

# --- Run Tests ---
echo -e "\nRunning tests..."
if [ -n "$IGNORE_WHISPER" ]; then
    echo "   (Skipping whisper tests)"
fi

python -m pytest tests $IGNORE_WHISPER $EXTRA_ARGS

# --- Deactivate and Finish ---
deactivate
echo -e "\nTests completed successfully."
