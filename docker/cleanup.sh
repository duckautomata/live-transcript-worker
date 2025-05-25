#!/bin/bash

# A script to cleanup all images and containers.
#
# Usage: ./cleanup.sh

docker stop $(docker ps -a -q)
docker rm $(docker ps -a -q)
docker rmi -f $(docker images -a -q)
