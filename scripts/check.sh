#!/bin/bash

# A script to run all code quality checks - mainly used for development.
#
# Usage: ./scripts/check.sh [options]
# Options:
#   --fix: Auto-fix ruff lint violations where possible

# --- Path and Environment Setup ---
cd "$(dirname "$0")/.."
echo "Running from project root: $PWD"

# Exit immediately if a command exits with a non-zero status.
set -e

# --- Argument Parsing ---
LINT_FIX=""
for arg in "$@"; do
    if [ "$arg" == "--fix" ]; then
        LINT_FIX="--fix"
    fi
done

# --- Ruff Format ---
echo -e "\nRunning Ruff formatter..."
uv run ruff format .
echo "   Ruff format complete."

# --- Ruff Lint ---
echo -e "\nRunning Ruff linter..."
uv run ruff check . $LINT_FIX
echo "   Ruff lint complete."

# --- Pyrefly ---
echo -e "\nRunning Pyrefly type checker..."
uv run pyrefly check
echo "   Pyrefly complete."

echo -e "\nAll checks passed!"
