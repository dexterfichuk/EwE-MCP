#!/usr/bin/env bash
set -euo pipefail

: "${SSHPASS:?Set SSHPASS in your shell before dismissing VM dialogs.}"

VM_USER="${EWE_VM_USER:-dexter}"
VM_HOST="${EWE_VM_HOST:-192.168.64.2}"
TASK_NAME="${EWE_VM_DIALOG_TASK:-EwE-MCP-Dismiss-Dialogs}"
REMOTE_SCRIPT="${EWE_VM_REMOTE_DIALOG_SCRIPT:-C:/Users/dexter/ewe-mcp-dismiss-dialogs.ps1}"
SECONDS_TO_RUN="${EWE_VM_DIALOG_WATCH_SECONDS:-30}"
SSH_OPTS=(-o StrictHostKeyChecking=no -o PreferredAuthentications=password -o PubkeyAuthentication=no)

TMP_SCRIPT="$(mktemp)"
trap 'rm -f "$TMP_SCRIPT"' EXIT

cat > "$TMP_SCRIPT" <<'PS'
param(
  [int]$Seconds = 30
)

$ErrorActionPreference = 'Stop'

Add-Type @"
using System;
using System.Runtime.InteropServices;
using System.Text;

public static class EwEDialogWin32 {
    public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);

    [DllImport("user32.dll")]
    public static extern bool EnumWindows(EnumWindowsProc enumFunc, IntPtr lParam);

    [DllImport("user32.dll")]
    public static extern bool IsWindowVisible(IntPtr hWnd);

    [DllImport("user32.dll")]
    public static extern int GetWindowText(IntPtr hWnd, StringBuilder text, int count);

    [DllImport("user32.dll")]
    public static extern int GetClassName(IntPtr hWnd, StringBuilder text, int count);

    [DllImport("user32.dll")]
    public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint processId);

    [DllImport("user32.dll", SetLastError = true)]
    public static extern IntPtr FindWindowEx(IntPtr parent, IntPtr childAfter, string className, string windowTitle);

    [DllImport("user32.dll")]
    public static extern IntPtr SendMessage(IntPtr hWnd, int message, IntPtr wParam, IntPtr lParam);
}
"@

$BM_CLICK = 0x00F5
$deadline = (Get-Date).AddSeconds($Seconds)
$log = Join-Path $env:APPDATA 'Ecopath with Ecosim\EwE_MCP_DialogSweeper.log'

function Get-WindowTextValue([IntPtr]$Handle) {
  $buffer = New-Object System.Text.StringBuilder 512
  [void][EwEDialogWin32]::GetWindowText($Handle, $buffer, $buffer.Capacity)
  $buffer.ToString()
}

function Get-WindowClassValue([IntPtr]$Handle) {
  $buffer = New-Object System.Text.StringBuilder 256
  [void][EwEDialogWin32]::GetClassName($Handle, $buffer, $buffer.Capacity)
  $buffer.ToString()
}

while ((Get-Date) -lt $deadline) {
  $script:ewePids = @(Get-Process ewe6 -ErrorAction SilentlyContinue | ForEach-Object { [uint32]$_.Id })
  if ($script:ewePids.Count -gt 0) {
    [EwEDialogWin32]::EnumWindows({
      param([IntPtr]$hWnd, [IntPtr]$lParam)

      try {
        if (-not [EwEDialogWin32]::IsWindowVisible($hWnd)) { return $true }

        [uint32]$windowPid = 0
        [void][EwEDialogWin32]::GetWindowThreadProcessId($hWnd, [ref]$windowPid)
        if ($script:ewePids -notcontains $windowPid) { return $true }

        $className = Get-WindowClassValue $hWnd
        if ($className -ne '#32770') { return $true }

        $okButton = [EwEDialogWin32]::FindWindowEx($hWnd, [IntPtr]::Zero, 'Button', 'OK')
        if ($okButton -eq [IntPtr]::Zero) { return $true }

        $title = Get-WindowTextValue $hWnd
        $line = '{0:o} Dismissed EwE dialog pid={1} title="{2}"' -f (Get-Date), $windowPid, $title
        Add-Content -Path $log -Value $line
        [void][EwEDialogWin32]::SendMessage($okButton, $BM_CLICK, [IntPtr]::Zero, [IntPtr]::Zero)
      } catch {
        Add-Content -Path $log -Value ('{0:o} Dialog sweeper callback error: {1}' -f (Get-Date), $_.Exception.Message)
      }
      return $true
    }, [IntPtr]::Zero) | Out-Null
  }

  Start-Sleep -Milliseconds 500
}
PS

sshpass -e scp "${SSH_OPTS[@]}" "$TMP_SCRIPT" "${VM_USER}@${VM_HOST}:$REMOTE_SCRIPT"
sshpass -e ssh "${SSH_OPTS[@]}" "${VM_USER}@${VM_HOST}" \
  "powershell -NoProfile -ExecutionPolicy Bypass -Command \"Unregister-ScheduledTask -TaskName '$TASK_NAME' -Confirm:\$false -ErrorAction SilentlyContinue; \$args = '-NoProfile -ExecutionPolicy Bypass -File \\\"$REMOTE_SCRIPT\\\" -Seconds $SECONDS_TO_RUN'; \$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument \$args; \$trigger = New-ScheduledTaskTrigger -Once -At ((Get-Date).AddMinutes(5)); \$principal = New-ScheduledTaskPrincipal -UserId '$VM_USER' -LogonType Interactive; Register-ScheduledTask -TaskName '$TASK_NAME' -Action \$action -Trigger \$trigger -Principal \$principal -Force | Out-Null; Start-ScheduledTask -TaskName '$TASK_NAME'; Get-ScheduledTaskInfo -TaskName '$TASK_NAME' | Select-Object LastRunTime,LastTaskResult\""
