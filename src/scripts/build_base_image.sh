#!/bin/bash
# Build base Docker image for MCP Sandbox
# This script builds the mcp-sandbox-base:latest image from Dockerfile.base

set -e

# Configuration
IMAGE_NAME="mcp-sandbox-base"
IMAGE_TAG="latest"
DOCKERFILE_PATH="src/container/Dockerfile.base"

# Get the project root directory (repo root)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# This script lives in src/scripts/, so repo root is two levels up
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

echo "============================================================"
echo "Building MCP Sandbox Base Image"
echo "============================================================"
echo ""
echo "Image: ${IMAGE_NAME}:${IMAGE_TAG}"
echo "Dockerfile: ${DOCKERFILE_PATH}"
echo "Build context: ${PROJECT_ROOT}"
echo ""

# Change to project root directory
cd "$PROJECT_ROOT"

# Check if Dockerfile exists
if [ ! -f "$DOCKERFILE_PATH" ]; then
    echo "Error: Dockerfile not found at ${DOCKERFILE_PATH}"
    exit 1
fi

# Build the image
echo "Building Docker image..."
docker build -f "$DOCKERFILE_PATH" -t "${IMAGE_NAME}:${IMAGE_TAG}" .

echo ""
echo "============================================================"
echo "Build Complete!"
echo "============================================================"
echo ""
echo "Image: ${IMAGE_NAME}:${IMAGE_TAG}"
echo ""
echo "To verify the image:"
echo "  docker images | grep ${IMAGE_NAME}"
echo ""
echo "To test the image:"
echo "  docker run -it --rm ${IMAGE_NAME}:${IMAGE_TAG} /bin/bash"
echo ""
echo "To use this image, ensure configs/container.yaml has:"
echo "  image: ${IMAGE_NAME}:${IMAGE_TAG}"
echo ""

