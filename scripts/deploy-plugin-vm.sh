#!/usr/bin/env bash
set -euo pipefail

: "${SSHPASS:?Set SSHPASS in your shell before deploying to the VM.}"

VM_USER="${EWE_VM_USER:-dexter}"
VM_HOST="${EWE_VM_HOST:-192.168.64.2}"
PLUGIN_DIR="${EWE_VM_PLUGIN_DIR:?Set EWE_VM_PLUGIN_DIR to the EwE plugins folder on Windows.}"
CONFIGURATION="${EWE_BUILD_CONFIGURATION:-Release}"
PLATFORM="${EWE_BUILD_PLATFORM:-x86}"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DLL_DIR="$ROOT_DIR/src/EwE_MCP_Plugin/bin/$CONFIGURATION/net48"

if [[ ! -d "$DLL_DIR" ]]; then
  echo "Plugin output folder not found: $DLL_DIR" >&2
  echo "Build the plugin first, then rerun deploy." >&2
  exit 1
fi

sshpass -e ssh -o StrictHostKeyChecking=no "$VM_USER@$VM_HOST" "powershell -NoProfile -Command \"New-Item -ItemType Directory -Force -Path '$PLUGIN_DIR' | Out-Null\""
sshpass -e scp -o StrictHostKeyChecking=no "$DLL_DIR/EwE_MCP_Plugin.dll" "${VM_USER}@${VM_HOST}:${PLUGIN_DIR}/"

if [[ -f "$DLL_DIR/Newtonsoft.Json.dll" ]]; then
  sshpass -e scp -o StrictHostKeyChecking=no "$DLL_DIR/Newtonsoft.Json.dll" "${VM_USER}@${VM_HOST}:${PLUGIN_DIR}/"
fi

echo "Deployed EwE_MCP_Plugin to $VM_USER@$VM_HOST:$PLUGIN_DIR ($PLATFORM/$CONFIGURATION)"
