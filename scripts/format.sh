#!/bin/bash
set -e

# Get the directory where the script is located
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

echo "Formatting code with Ruff..."
ruff format "$PROJECT_ROOT"

echo "Fixing lint issues..."
ruff check --fix "$PROJECT_ROOT"
