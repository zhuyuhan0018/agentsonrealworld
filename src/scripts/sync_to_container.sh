#!/bin/bash
# Sync infrastructure files to a running container
# This is a convenience wrapper around the Python sync script

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Get script directory and project root
# Script is at src/scripts/, so project root is 2 levels up
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# Default container name
CONTAINER_NAME="${1:-mcp-sandbox}"

echo -e "${BLUE}============================================================${NC}"
echo -e "${BLUE}Syncing Infrastructure to Container${NC}"
echo -e "${BLUE}============================================================${NC}"
echo ""
echo "Container: ${CONTAINER_NAME}"
echo "Project root: ${PROJECT_ROOT}"
echo ""

# Run Python script
cd "$PROJECT_ROOT"
python src/scripts/sync_infrastructure.py --container "$CONTAINER_NAME"

