#!/usr/bin/env bash
set -euo pipefail

: "${SSHPASS:?Set SSHPASS in your shell before deploying from the VM.}"

VM_USER="${EWE_VM_USER:-dexter}"
VM_HOST="${EWE_VM_HOST:-192.168.64.2}"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ROOT_NAME="$(basename "$ROOT_DIR")"
REMOTE_ROOT="${EWE_VM_REMOTE_ROOT:-C:/Users/dexter/$ROOT_NAME}"
EWE_DIR="${EWE_VM_EWE_DIR:-C:\\Program Files (x86)\\Ecopath with Ecosim 40 years 6.7.0.18865_32-bit}"
CONFIGURATION="${EWE_BUILD_CONFIGURATION:-Release}"
STOP_EWE="${EWE_VM_STOP_EWE_BEFORE_DEPLOY:-1}"

sshpass -e ssh -o StrictHostKeyChecking=no "${VM_USER}@${VM_HOST}" \
  "powershell -NoProfile -Command \"if ('$STOP_EWE' -eq '1') { Stop-Process -Name ewe6 -Force -ErrorAction SilentlyContinue; Start-Sleep -Seconds 2 }; \$src='$REMOTE_ROOT\\src\\EwE_MCP_Plugin\\bin\\$CONFIGURATION\\net48'; \$dst='$EWE_DIR\\Plugins'; Copy-Item (Join-Path \$src 'EwE_MCP_Plugin.dll') \$dst -Force; Copy-Item (Join-Path \$src 'Newtonsoft.Json.dll') \$dst -Force; Get-ChildItem \$dst -Filter 'EwE_MCP_Plugin.dll' | Select-Object FullName,Length,LastWriteTime\""
