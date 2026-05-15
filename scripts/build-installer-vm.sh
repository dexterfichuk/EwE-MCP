#!/usr/bin/env bash
set -euo pipefail

: "${SSHPASS:?Set SSHPASS in your shell before building on the VM.}"

VM_USER="${EWE_VM_USER:-dexter}"
VM_HOST="${EWE_VM_HOST:-192.168.64.2}"
EWE_DIR="${EWE_VM_EWE_DIR:-C:\\Program Files (x86)\\Ecopath with Ecosim 40 years 6.7.0.18865_32-bit}"
EWE_DIR_X64="${EWE_VM_EWE_DIR_X64:-C:\\Program Files (x86)\\Ecopath with Ecosim 40 years 6.7.0.18865_64-bit}"
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
  --exclude='tests' \
  --exclude='._*' \
  -czf "$ARCHIVE" \
  -C "$(dirname "$ROOT_DIR")" \
  "$(basename "$ROOT_DIR")"

sshpass -e scp "${SSH_OPTS[@]}" "$ARCHIVE" "${VM_USER}@${VM_HOST}:C:/Users/dexter/ewe-mcp-src.tgz"
sshpass -e ssh "${SSH_OPTS[@]}" "${VM_USER}@${VM_HOST}" \
  "powershell -NoProfile -ExecutionPolicy Bypass -Command \"if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { irm https://astral.sh/uv/install.ps1 | iex }; \$env:Path = \\\"\$env:USERPROFILE\.local\bin;\$env:Path\\\"; if ((-not (Test-Path 'C:\Program Files (x86)\Inno Setup 6\ISCC.exe')) -and (-not (Test-Path \\\"\$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe\\\"))) { Write-Host 'Installing Inno Setup...'; winget install --id JRSoftware.InnoSetup -e --accept-package-agreements --accept-source-agreements }; Remove-Item -Recurse -Force '$REMOTE_ROOT' -ErrorAction SilentlyContinue; tar -xzf C:\\Users\\dexter\\ewe-mcp-src.tgz -C C:\\Users\\dexter; Set-Location '$REMOTE_ROOT'; .\scripts\build-installer.ps1 -Configuration $CONFIGURATION -Platform $PLATFORM -EwEAssemblyDir '$EWE_DIR' -EwEAssemblyDirX64 '$EWE_DIR_X64'\""
