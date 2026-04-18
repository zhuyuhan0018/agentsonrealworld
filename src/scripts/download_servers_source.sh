#!/usr/bin/env bash
# Download pre-built MCP servers from GrantBox GitHub Releases and extract into servers_source/.
# Supports resume (re-run if interrupted). Requires: curl or wget, unzip.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

ZIP_NAME="servers_source.zip"
ZIP_PATH="$ROOT/$ZIP_NAME"
# Official release asset (see README / GitHub Releases "Pre‑Deployed MCP Servers")
URL_PRIMARY="https://github.com/ZQ-Struggle/Agent-GrantBox/releases/download/MCP_Servers/servers_source.zip"

URL="${GRANTBOX_SERVERS_URL:-$URL_PRIMARY}"

echo "Target: $ZIP_PATH"
echo "URL:    $URL"
echo ""

mkdir -p "$ROOT/servers_source"

download_curl() {
  # -C - : resume partial download
  curl -fL --connect-timeout 30 --retry 5 --retry-delay 5 --max-time 0 -C - -o "$ZIP_PATH" "$URL"
}

download_wget() {
  wget --continue --tries=0 --timeout=60 --read-timeout=300 -O "$ZIP_PATH" "$URL"
}

if command -v curl >/dev/null 2>&1; then
  download_curl
elif command -v wget >/dev/null 2>&1; then
  download_wget
else
  echo "Need curl or wget." >&2
  exit 1
fi

# Expected size from release metadata: ~124389172 bytes; allow some slack
SIZE=$(stat -c%s "$ZIP_PATH" 2>/dev/null || stat -f%z "$ZIP_PATH" 2>/dev/null)
if [ "${SIZE:-0}" -lt 1000000 ]; then
  echo "Downloaded file is too small ($SIZE bytes); likely an error page. Remove $ZIP_PATH and retry." >&2
  exit 1
fi

echo "Verifying zip..."
unzip -tq "$ZIP_PATH"

echo "Extracting to $ROOT/servers_source/ ..."
unzip -q -o "$ZIP_PATH" -d "$ROOT/servers_source"

# Release zip nests paths as servers_source/<mcp-dirs>; flatten so configs match servers_source/notion-mcp, etc.
if [ -d "$ROOT/servers_source/servers_source" ]; then
  echo "Flattening nested servers_source/ directory from archive..."
  shopt -s dotglob nullglob
  mv "$ROOT/servers_source/servers_source/"* "$ROOT/servers_source/"
  rmdir "$ROOT/servers_source/servers_source"
  shopt -u dotglob nullglob
fi

echo "Done. Top-level entries:"
ls -la "$ROOT/servers_source" | head -25
