#!/usr/bin/env bash
set -euo pipefail

resolve_version() {
    curl -sI "https://github.com/$1/releases/latest" \
        | grep -i '^location:' \
        | sed 's|.*/tag/||' \
        | tr -d '[:space:]'
}

DENO_VERSION=$(resolve_version "denoland/deno")

echo "Resolved Deno version: $DENO_VERSION"

IMAGE_NAME="duckautomata/live-transcript-worker"
BUILD_DATE=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

# If NEW_VERSION is not set, we default to dev
if [ -z "${NEW_VERSION:-}" ]; then
    echo "No version provided, building with dev tag..."
    docker build \
        --build-arg APP_VERSION="${NEW_VERSION}" \
        --build-arg BUILD_DATE="${BUILD_DATE}" \
        --build-arg DENO_VERSION="${DENO_VERSION}" \
        -t "${IMAGE_NAME}:dev" \
        .
    echo "Pushing ${IMAGE_NAME}:dev..."
    docker push "${IMAGE_NAME}:dev"
    echo "Done. Published ${IMAGE_NAME}:dev"
    exit 0
fi

echo "Building version ${NEW_VERSION}..."
docker build \
    --build-arg APP_VERSION="${NEW_VERSION}" \
    --build-arg BUILD_DATE="${BUILD_DATE}" \
    --build-arg DENO_VERSION="${DENO_VERSION}" \
    -t "${IMAGE_NAME}:${NEW_VERSION}" \
    -t "${IMAGE_NAME}:latest" \
    .

echo "Pushing ${IMAGE_NAME}:${NEW_VERSION}..."
docker push "${IMAGE_NAME}:${NEW_VERSION}"

echo "Pushing ${IMAGE_NAME}:latest..."
docker push "${IMAGE_NAME}:latest"

echo "Done. Published ${IMAGE_NAME}:${NEW_VERSION} and ${IMAGE_NAME}:latest"
