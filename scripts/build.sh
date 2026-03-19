#!/bin/bash

# A script to build and tag Docker images with major and minor versions.
#
# Usage: ./scripts/build.sh <version>
# Example: ./scripts/build.sh 1.2

# --- Configuration ---
IMAGE_NAME="duckautomata/live-transcript-worker"
# ---------------------

# --- Path and Environment Setup ---
# Change to the project root directory
cd "$(dirname "$0")/.."
echo "Running from project root: $PWD"

set -e

# --- Input Validation ---
if [ -z "$1" ]; then
    echo "Error: No version specified."
    echo "   Usage: $0 <version>"
    exit 1
fi

VERSION=$1

if ! [[ $VERSION =~ ^[0-9]+\.[0-9]+$ ]]; then
    echo "Error: Invalid version format: '${VERSION}'"
    echo "   Please use the format 'major.minor' (e.g., '1.2' or '10.4')."
    exit 1
fi

# --- Code Quality Checks ---
echo -e "\nRunning code quality checks before build..."

echo "   Running Ruff formatter..."
uv run ruff format .

echo "   Running Ruff linter..."
uv run ruff check .

echo "   Running Pyrefly type checker..."
uv run pyrefly check

echo "   All checks passed."
echo "-----------------------------------"

# --- Tag Generation ---
BUILD_DATE=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
MAJOR_VERSION=$(echo "$VERSION" | cut -d. -f1)

SPECIFIC_TAG="${IMAGE_NAME}:${VERSION}"
MAJOR_TAG="${IMAGE_NAME}:${MAJOR_VERSION}"
LATEST_TAG="${IMAGE_NAME}:latest"

echo -e "\nWill build image with the following tags:"
echo "   - Specific: ${SPECIFIC_TAG}"
echo "   - Major:    ${MAJOR_TAG}"
echo "   - Latest:   ${LATEST_TAG}"
echo "-----------------------------------"

# --- Docker Command ---
if command -v docker &> /dev/null && docker info > /dev/null 2>&1; then
    DOCKER_CMD="docker"
elif command -v sudo &> /dev/null && sudo docker info > /dev/null 2>&1; then
    DOCKER_CMD="sudo docker"
else
    echo "Error: Docker is not running or you lack permission to use it."
    exit 1
fi

echo "Building Docker image..."
if ! $DOCKER_CMD build \
    --build-arg APP_VERSION="${VERSION}" \
    --build-arg BUILD_DATE="${BUILD_DATE}" \
    --build-arg CACHEBUST="${BUILD_DATE}" \
    -t "${SPECIFIC_TAG}" \
    -t "${MAJOR_TAG}" \
    -t "${LATEST_TAG}" \
    .; then
    echo "Docker build failed. Aborting."
    exit 1
fi

echo -e "\nBuild successful. Created images:"
$DOCKER_CMD images --filter=reference="${IMAGE_NAME}"

# --- Optional Push to Registry ---
read -p "Push these tags to the registry? (y/n) " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    echo " Pushing ${SPECIFIC_TAG}..."
    $DOCKER_CMD push "${SPECIFIC_TAG}"

    echo " Pushing ${MAJOR_TAG}..."
    $DOCKER_CMD push "${MAJOR_TAG}"

    echo " Pushing ${LATEST_TAG}..."
    $DOCKER_CMD push "${LATEST_TAG}"
    echo "Push complete."
fi
