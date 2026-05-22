#!/usr/bin/env bash
set -euo pipefail

: "${SSHPASS:?Set SSHPASS in your shell before building on the VM.}"

VM_USER="${EWE_VM_USER:-dexter}"
VM_HOST="${EWE_VM_HOST:-192.168.64.2}"
EWE_DIR="${EWE_VM_EWE_DIR:-C:\\Program Files (x86)\\Ecopath with Ecosim 40 years 6.7.0.18865_32-bit}"
CONFIGURATION="${EWE_BUILD_CONFIGURATION:-Release}"
PLATFORM="${EWE_BUILD_PLATFORM:-x86}"
SSH_OPTS=(-o StrictHostKeyChecking=no -o PreferredAuthentications=password -o PubkeyAuthentication=no)

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ROOT_NAME="$(basename "$ROOT_DIR")"
REMOTE_ROOT="${EWE_VM_REMOTE_ROOT:-C:/Users/dexter/$ROOT_NAME}"
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

sshpass -e scp "${SSH_OPTS[@]}" "$ARCHIVE" "${VM_USER}@${VM_HOST}:C:/Users/dexter/ewe-mcp-src.tgz"

# scp build script to avoid quoting issues with spaces in path
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
sshpass -e scp "${SSH_OPTS[@]}" "${SCRIPT_DIR}/build_sync.ps1" "${VM_USER}@${VM_HOST}:C:/Users/dexter/build.ps1"
sshpass -e ssh "${SSH_OPTS[@]}" "${VM_USER}@${VM_HOST}" \
  "C:\\Windows\\SysWOW64\\WindowsPowerShell\\v1.0\\powershell.exe -ExecutionPolicy Bypass -File C:\\Users\\dexter\\build.ps1"
