#!/bin/bash

# A script to cleanup all images and containers.
#
# Usage: ./cleanup.sh

CONTAINER_NAME="live_transcript_worker"
IMAGE_NAME="duckautomata/live-transcript-worker"

docker stop $(docker ps -a -q --filter "name=$CONTAINER_NAME")
docker rm -f $(docker ps -a -q --filter "name=$CONTAINER_NAME")
docker rmi -f $(docker images -q "$IMAGE_NAME")
docker image prune -f
