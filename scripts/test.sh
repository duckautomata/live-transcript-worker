#!/bin/bash

# A script to run tests using uv - mainly used for development.
#
# Usage: ./scripts/test.sh [options]
# Options:
#   --include-whisper: Include whisper tests (excluded by default)
#   --print: Print test output

# --- Path and Environment Setup ---
# Change to the project root directory
cd "$(dirname "$0")/.."
echo "Running from project root: $PWD"

# Exit immediately if a command exits with a non-zero status.
set -e

# --- Argument Parsing ---
IGNORE_WHISPER="--ignore-glob=tests/test_whisper_*.py"
EXTRA_ARGS=""
for arg in "$@"; do
    if [ "$arg" == "--include-whisper" ]; then
        IGNORE_WHISPER=""
    elif [ "$arg" == "--print" ]; then
        EXTRA_ARGS="$EXTRA_ARGS -s"
    fi
done

# --- Run Tests ---
echo -e "\nRunning tests..."
if [ -n "$IGNORE_WHISPER" ]; then
    echo "   (Skipping whisper tests, use --include-whisper to run them)"
fi

uv run pytest tests $IGNORE_WHISPER $EXTRA_ARGS

echo -e "\nTests completed successfully."
