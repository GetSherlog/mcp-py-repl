#!/bin/bash
# Build and push the Docker image
docker buildx build --builder=mcp-builder --platform linux/amd64,linux/arm64 -t ghcr.io/evalstate/sherlog-repl --push .

# Usage instructions for running with Docker access
echo "
To run this container with Docker access, use one of these methods:

# Method 1: Docker-in-Docker (DinD)
docker run --privileged \
  -e DOCKER_TLS_CERTDIR=/certs \
  -v mcp-docker-certs:/certs \
  -v mcp-docker-volume:/var/lib/docker \
  ghcr.io/evalstate/sherlog-repl

# Method 2: Mount host Docker socket
docker run \
  -v /var/run/docker.sock:/var/run/docker.sock \
  ghcr.io/evalstate/sherlog-repl
"
