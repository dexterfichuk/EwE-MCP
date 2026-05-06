#!/usr/bin/env bash
set -euo pipefail

: "${SSHPASS:?Set SSHPASS in your shell before running live VM smoke tests.}"

VM_USER="${EWE_VM_USER:-dexter}"
VM_HOST="${EWE_VM_HOST:-192.168.64.2}"
LOCAL_PORT="${EWE_MCP_LOCAL_TUNNEL_PORT:-19877}"
REMOTE_PORT="${EWE_MCP_REMOTE_PORT:-9877}"
TOKEN="${EWE_MCP_TOKEN:-ewe-mcp-local-dev}"

sshpass -e ssh -f -N \
  -L "${LOCAL_PORT}:127.0.0.1:${REMOTE_PORT}" \
  -o StrictHostKeyChecking=no \
  "${VM_USER}@${VM_HOST}"

cleanup() {
  pkill -f "ssh.*${LOCAL_PORT}:127.0.0.1:${REMOTE_PORT}" >/dev/null 2>&1 || true
}
trap cleanup EXIT

sleep 1

EWE_MCP_HOST=127.0.0.1 \
EWE_MCP_PORT="$LOCAL_PORT" \
EWE_MCP_TOKEN="$TOKEN" \
uv run python - <<'PY'
from ewe_mcp.client import EwEMCPClient

client = EwEMCPClient()
if not client.connect():
    raise SystemExit("Could not connect to EwE MCP plugin through SSH tunnel")

for command in ("ping", "diagnose", "get_ewe_info", "list_domains"):
    print(f"{command}: {client.command_result(command)}")

client.disconnect()
PY
