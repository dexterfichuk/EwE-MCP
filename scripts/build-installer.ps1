param(
    [string]$Configuration = "Release",
    [string]$Platform = "x86",
    [string]$EwEAssemblyDir = $env:EWE_ASSEMBLY_DIR,
    [string]$EwEAssemblyDirX64 = $env:EWE_ASSEMBLY_DIR_X64,
    [string]$InnoSetupDir = ""
)

if ([string]::IsNullOrWhiteSpace($InnoSetupDir)) {
    if (Test-Path "C:\Program Files (x86)\Inno Setup 6\ISCC.exe") {
        $InnoSetupDir = "C:\Program Files (x86)\Inno Setup 6"
    } elseif (Test-Path "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe") {
        $InnoSetupDir = "$env:LOCALAPPDATA\Programs\Inno Setup 6"
    } else {
        $InnoSetupDir = "C:\Program Files (x86)\Inno Setup 6"
    }
}

if ([string]::IsNullOrWhiteSpace($EwEAssemblyDir)) {
    throw "Set EWE_ASSEMBLY_DIR to either the EwE install directory or a built EwE Sources directory."
}

$ErrorActionPreference = 'Stop'

Write-Host "Building C# Plugin (x86)..."
& "$PSScriptRoot\build-plugin.ps1" -Configuration $Configuration -Platform x86 -EwEAssemblyDir $EwEAssemblyDir
if ($LASTEXITCODE -ne 0) { throw "Plugin build (x86) failed" }

Write-Host "Building C# Plugin (x64)..."
if ([string]::IsNullOrWhiteSpace($EwEAssemblyDirX64)) {
    throw "Set EwEAssemblyDirX64 to the 64-bit EwE install directory for the x64 plugin build."
}
& "$PSScriptRoot\build-plugin.ps1" -Configuration $Configuration -Platform x64 -EwEAssemblyDir $EwEAssemblyDirX64
if ($LASTEXITCODE -ne 0) { throw "Plugin build (x64) failed" }

Write-Host "Building Python Server Standalone Executable..."
Set-Location "$PSScriptRoot\.."
# Run pyinstaller using uv. It will create 'dist\ewe-mcp\ewe-mcp.exe'
& uv run pyinstaller --noconfirm --onedir --name ewe-mcp --clean "src\ewe_mcp\server.py"
if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed" }

Write-Host "Building Inno Setup Installer..."
$ISCC = Join-Path $InnoSetupDir "ISCC.exe"
if (-Not (Test-Path $ISCC)) {
    throw "Could not find Inno Setup compiler at $ISCC. Please ensure Inno Setup is installed."
}

& $ISCC "$PSScriptRoot\..\installer\EwE-MCP.iss"
if ($LASTEXITCODE -ne 0) { throw "Inno Setup compilation failed" }

Write-Host "Build Complete! Installer should be in the dist/ directory."
