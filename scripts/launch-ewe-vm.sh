#!/usr/bin/env bash
set -euo pipefail

: "${SSHPASS:?Set SSHPASS in your shell before launching EwE on the VM.}"

VM_USER="${EWE_VM_USER:-dexter}"
VM_HOST="${EWE_VM_HOST:-192.168.64.2}"
EWE_DIR="${EWE_VM_EWE_DIR:-C:\\Program Files (x86)\\Ecopath with Ecosim 40 years 6.7.0.18865_32-bit}"
TASK_NAME="${EWE_VM_LAUNCH_TASK:-EwE-MCP-Launch}"
REMOTE_SCRIPT="${EWE_VM_REMOTE_LAUNCH_SCRIPT:-C:/Users/dexter/ewe-mcp-launch.ps1}"
SSH_OPTS=(-o StrictHostKeyChecking=no -o PreferredAuthentications=password -o PubkeyAuthentication=no)

TMP_SCRIPT="$(mktemp)"
trap 'rm -f "$TMP_SCRIPT"' EXIT

cat > "$TMP_SCRIPT" <<PS
\$ErrorActionPreference = 'Stop'
\$exe = Join-Path '$EWE_DIR' 'ewe6.exe'
\$dir = Split-Path \$exe
\$log = Join-Path \$env:APPDATA 'Ecopath with Ecosim\EwE_MCP_Plugin.log'

Stop-Process -Name ewe6 -Force -ErrorAction SilentlyContinue
Remove-Item \$log -Force -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName '$TASK_NAME' -Confirm:\$false -ErrorAction SilentlyContinue

\$action = New-ScheduledTaskAction -Execute \$exe -WorkingDirectory \$dir
\$trigger = New-ScheduledTaskTrigger -Once -At ((Get-Date).AddMinutes(5))
\$principal = New-ScheduledTaskPrincipal -UserId '$VM_USER' -LogonType Interactive
Register-ScheduledTask -TaskName '$TASK_NAME' -Action \$action -Trigger \$trigger -Principal \$principal -Force | Out-Null
Start-ScheduledTask -TaskName '$TASK_NAME'

Start-Sleep -Seconds 20
Get-ScheduledTaskInfo -TaskName '$TASK_NAME' | Format-List LastRunTime,LastTaskResult
Get-Process ewe6 -ErrorAction SilentlyContinue | Format-List Id,ProcessName,StartTime,SessionId
Test-NetConnection 127.0.0.1 -Port 9877 | Select-Object TcpTestSucceeded,RemoteAddress,RemotePort
if (Test-Path \$log) {
  Get-Content \$log -Tail 80
} else {
  Write-Output 'NO_MCP_LOG'
}
PS

sshpass -e scp "${SSH_OPTS[@]}" "$TMP_SCRIPT" "${VM_USER}@${VM_HOST}:$REMOTE_SCRIPT"
sshpass -e ssh "${SSH_OPTS[@]}" "${VM_USER}@${VM_HOST}" \
  "powershell -NoProfile -ExecutionPolicy Bypass -File $REMOTE_SCRIPT"
