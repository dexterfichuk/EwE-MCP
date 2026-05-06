param(
    [string]$Configuration = "Release",
    [string]$Platform = "x86",
    [string]$EwEAssemblyDir = $env:EWE_ASSEMBLY_DIR
)

if ([string]::IsNullOrWhiteSpace($EwEAssemblyDir)) {
    throw "Set EWE_ASSEMBLY_DIR to the EwE Sources directory containing EwECore, EwEUtils, and ScientificInterfaceShared."
}

dotnet build "$PSScriptRoot\..\src\EwE_MCP_Plugin\EwE_MCP_Plugin.csproj" `
    -c $Configuration `
    -p:PlatformTarget=$Platform `
    -p:EwEAssemblyPlatform=$Platform `
    -p:EwEAssemblyConfiguration=$Configuration `
    -p:EwEAssemblyDir="$EwEAssemblyDir"
