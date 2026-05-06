#!/usr/bin/env bash
set -euo pipefail

: "${SSHPASS:?Set SSHPASS in your shell before running VM smoke checks.}"

VM_USER="${EWE_VM_USER:-dexter}"
VM_HOST="${EWE_VM_HOST:-192.168.64.2}"
EWE_DIR="${EWE_VM_EWE_DIR:?Set EWE_VM_EWE_DIR to the EwE install folder on Windows.}"

sshpass -e ssh -o StrictHostKeyChecking=no "$VM_USER@$VM_HOST" \
  "powershell -NoProfile -Command \"Get-ChildItem -Path '$EWE_DIR' | Select-Object -First 20 | Format-Table -AutoSize\""
