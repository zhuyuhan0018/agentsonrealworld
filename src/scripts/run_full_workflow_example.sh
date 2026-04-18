#!/usr/bin/env bash
# Run one end-to-end GrantBox workflow (needs Docker + .env + servers_source).
# Default: Wikipedia MCP only (minimal third-party secrets).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

if ! docker info >/dev/null 2>&1; then
  echo "ERROR: Docker is not usable (try: groups | grep docker, or sudo usermod -aG docker \$USER)." >&2
  exit 1
fi

if [ ! -d "$ROOT/servers_source/wikipedia-mcp-server" ]; then
  echo "ERROR: Missing MCP sources under servers_source/ (unpack servers_source.zip)." >&2
  exit 1
fi

# shellcheck source=/dev/null
source "$ROOT/.venv/bin/activate"

WF_ID="${1:-wikipedia_search_mcp}"
exec python main.py \
  --config configs/react.yaml \
  --workflows configs/workflows.json \
  --workflow-id "$WF_ID" \
  --log-level INFO
