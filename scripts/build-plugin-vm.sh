#!/usr/bin/env bash
set -euo pipefail

: "${SSHPASS:?Set SSHPASS in your shell before building on the VM.}"

VM_USER="${EWE_VM_USER:-dexter}"
VM_HOST="${EWE_VM_HOST:-192.168.64.2}"
REMOTE_ROOT="${EWE_VM_REMOTE_ROOT:-C:/Users/dexter/EwE-MCP}"
EWE_DIR="${EWE_VM_EWE_DIR:-C:\\Program Files (x86)\\Ecopath with Ecosim 40 years 6.7.0.18865_32-bit}"
CONFIGURATION="${EWE_BUILD_CONFIGURATION:-Release}"
PLATFORM="${EWE_BUILD_PLATFORM:-x86}"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ARCHIVE="/tmp/ewe-mcp-src.tgz"

COPYFILE_DISABLE=1 tar \
  --exclude='.git' \
  --exclude='.venv' \
  --exclude='.pytest_cache' \
  --exclude='src/EwE_MCP_Plugin/bin' \
  --exclude='src/EwE_MCP_Plugin/obj' \
  --exclude='__pycache__' \
  --exclude='._*' \
  -czf "$ARCHIVE" \
  -C "$(dirname "$ROOT_DIR")" \
  "$(basename "$ROOT_DIR")"

sshpass -e scp -o StrictHostKeyChecking=no "$ARCHIVE" "${VM_USER}@${VM_HOST}:C:/Users/dexter/ewe-mcp-src.tgz"
sshpass -e ssh -o StrictHostKeyChecking=no "${VM_USER}@${VM_HOST}" \
  "powershell -NoProfile -Command \"Remove-Item -Recurse -Force '$REMOTE_ROOT' -ErrorAction SilentlyContinue; tar -xzf C:\\Users\\dexter\\ewe-mcp-src.tgz -C C:\\Users\\dexter; Set-Location '$REMOTE_ROOT'; dotnet build .\\src\\EwE_MCP_Plugin\\EwE_MCP_Plugin.csproj -c $CONFIGURATION -p:PlatformTarget=$PLATFORM -p:EwEAssemblyDir='$EWE_DIR'\""
