Write-Host "Inno Setup Compiler test"

Write-Host "Stopping any running EwE instances..."
Stop-Process -Name "ewe6" -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2

Write-Host "Running Installer silently..."
$installer = "C:\Users\dexter\EwE-MCP\dist\EwE-MCP-Setup.exe"
$process = Start-Process -Wait -PassThru -FilePath $installer -ArgumentList '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART'

if ($process.ExitCode -ne 0) {
    throw "Installer failed with exit code $($process.ExitCode)"
}

Write-Host "Verifying installation..."
$expectedExe = "$env:LOCALAPPDATA\Programs\EwE-MCP\ewe-mcp.exe"
if (-Not (Test-Path $expectedExe)) {
    throw "Installed executable not found at $expectedExe"
}

Write-Host "Installer test passed successfully!"
