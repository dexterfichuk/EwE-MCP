param(
    [string]$EweDir = "C:\Program Files (x86)\Ecopath with Ecosim 40 years 6.7.0.18865_32-bit",
    [string]$Configuration = "Release",
    [string]$Platform = "x86"
)

$remoteRoot = "C:/Users/dexter/EwE-MCP"
Remove-Item -Recurse -Force $remoteRoot -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force "$remoteRoot/src/EwE_MCP_Plugin/obj" -ErrorAction SilentlyContinue
tar -xzf C:\Users\dexter\ewe-mcp-src.tgz -C C:\Users\dexter
Set-Location $remoteRoot

# Touch source to force recompilation (tar preserves timestamps)
$srcFile = "$remoteRoot/src/EwE_MCP_Plugin/EwEMcpPlugin.cs"
[System.IO.File]::SetLastWriteTimeUtc($srcFile, [DateTime]::UtcNow)

Write-Host "Building with EweDir=$EweDir"
Remove-Item -Recurse -Force "$remoteRoot/src/EwE_MCP_Plugin/obj" -ErrorAction SilentlyContinue
dotnet build .\src\EwE_MCP_Plugin\EwE_MCP_Plugin.csproj -c $Configuration -p:PlatformTarget=$Platform "-p:EwEAssemblyDir=$EweDir"
if ($LASTEXITCODE -eq 0) {
    Write-Host "Build succeeded"
} else {
    Write-Host "Build FAILED with exit code $LASTEXITCODE"
}
