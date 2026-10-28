# EwE MCP: Agent Install Guide

Instructions for an AI coding agent (Claude Code, Codex, OpenCode, etc.) to install EwE MCP on a Windows machine without the `EwE-MCP-Setup.exe` installer. The agent builds the plugin against the local EwE install, copies it into EwE's `Plugins` folder, installs the Python MCP server, writes the config, and connects an MCP client.

> **For humans:** open your agent on the Windows machine where EwE is installed and say:
> *"Follow https://github.com/dexterfichuk/EwE-MCP/blob/main/AGENT_INSTALL.md to install EwE MCP."*

All commands are PowerShell. Run each step, check its result, and stop and report to the user if a check fails. Don't skip ahead.

## 0. Preconditions

- Windows 10 or 11 with **Ecopath with Ecosim 6.7** installed.
- Ask the user to **close EwE** before step 3. A running EwE locks the `Plugins` folder.
- Copying into `C:\Program Files*` needs administrator rights. If a copy fails with "access denied", ask the user to rerun the agent from an elevated (Administrator) terminal, or to approve the UAC prompt.

## 1. Find EwE and its architecture

```powershell
$EwEExe = Get-ChildItem "C:\Program Files (x86)", "C:\Program Files" -Filter ewe6.exe -Recurse -Depth 2 -ErrorAction SilentlyContinue | Select-Object -First 1
$EwEDir = $EwEExe.DirectoryName
$EwEDir
```

- If nothing is found, ask the user where EwE is installed (the folder containing `ewe6.exe`) and set `$EwEDir` to it.
- If more than one install exists, ask the user which one to use.

Set the architecture. A folder name ending in `_32-bit` or living under `Program Files (x86)` means **x86**, otherwise **x64**:

```powershell
$Platform = if ($EwEDir -match '32-bit' -or $EwEDir -like 'C:\Program Files (x86)*') { 'x86' } else { 'x64' }
$Platform
```

Check that the assemblies the plugin compiles against are there:

```powershell
'EwECore.dll','EwEPlugin.dll','EwEUtils.dll','ScientificInterfaceShared.dll' | ForEach-Object { "$_ : $(Test-Path (Join-Path $EwEDir $_))" }
```

All four must be `True`.

## 2. Install the tools

Install whatever is missing: git, the .NET SDK (builds the plugin) and uv (installs the Python server).

```powershell
if (-not (Get-Command git -ErrorAction SilentlyContinue))    { winget install --id Git.Git -e --accept-package-agreements --accept-source-agreements }
if (-not (Get-Command dotnet -ErrorAction SilentlyContinue)) { winget install --id Microsoft.DotNet.SDK.8 -e --accept-package-agreements --accept-source-agreements }
if (-not (Get-Command uv -ErrorAction SilentlyContinue))     { powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex" }
$env:Path = [Environment]::GetEnvironmentVariable('Path','Machine') + ';' + [Environment]::GetEnvironmentVariable('Path','User')
git --version; dotnet --version; uv --version
```

## 3. Build and install the EwE plugin

```powershell
$Src = "$env:USERPROFILE\EwE-MCP"
if (Test-Path $Src) { git -C $Src pull } else { git clone https://github.com/dexterfichuk/EwE-MCP.git $Src }

$Out = "$Src\src\EwE_MCP_Plugin\bin\Release\net48\$Platform"
dotnet build "$Src\src\EwE_MCP_Plugin\EwE_MCP_Plugin.csproj" -c Release `
  -p:PlatformTarget=$Platform -p:EwEAssemblyPlatform=$Platform `
  -p:EwEAssemblyDir="$EwEDir" -o $Out
```

The build must end with `Build succeeded` and `0 Error(s)`. Then copy the plugin into EwE (close EwE first):

```powershell
New-Item -ItemType Directory -Force "$EwEDir\Plugins" | Out-Null
Copy-Item "$Out\EwE_MCP_Plugin.dll" "$EwEDir\Plugins\" -Force
if (-not (Test-Path "$EwEDir\Plugins\Newtonsoft.Json.dll") -and -not (Test-Path "$EwEDir\Newtonsoft.Json.dll")) {
  Copy-Item "$Out\Newtonsoft.Json.dll" "$EwEDir\Plugins\"
}
Get-ChildItem "$EwEDir\Plugins\EwE_MCP_Plugin.dll"
```

Don't overwrite an existing `Newtonsoft.Json.dll`, because other EwE plugins may depend on that version.

## 4. Write the plugin config

Generate a token, which the plugin and the MCP client must share:

```powershell
$Token = [guid]::NewGuid().ToString('N')
$CfgDir = "$env:APPDATA\Ecopath with Ecosim"
New-Item -ItemType Directory -Force $CfgDir | Out-Null
@"
[EwE_MCP]
host=127.0.0.1
port=9877
token=$Token
"@ | Set-Content -Encoding ASCII "$CfgDir\EwE_MCP_Plugin.cfg"
```

If `EwE_MCP_Plugin.cfg` already exists, read its `token=` value into `$Token` and keep the file instead of overwriting it.

## 5. Install the MCP server

```powershell
uv tool install --force "git+https://github.com/dexterfichuk/EwE-MCP.git"
$ServerExe = Join-Path (uv tool dir --bin) 'ewe-mcp.exe'
Test-Path $ServerExe
$ServerExe
```

Use the full `$ServerExe` path in client configs. Desktop apps often don't see the updated `PATH`.

## 6. Connect an MCP client

Use the client the user has. Merge into an existing config file, don't replace it. Keep the user's other servers.

**Claude Desktop:** `%APPDATA%\Claude\claude_desktop_config.json`

```powershell
$CfgPath = "$env:APPDATA\Claude\claude_desktop_config.json"
$cfg = if (Test-Path $CfgPath) { Get-Content $CfgPath -Raw | ConvertFrom-Json } else { [pscustomobject]@{} }
if (-not $cfg.mcpServers) { $cfg | Add-Member -NotePropertyName mcpServers -NotePropertyValue ([pscustomobject]@{}) }
$cfg.mcpServers | Add-Member -Force -NotePropertyName ewe -NotePropertyValue ([pscustomobject]@{
  command = $ServerExe
  env = [pscustomobject]@{ EWE_MCP_HOST = '127.0.0.1'; EWE_MCP_PORT = '9877'; EWE_MCP_TOKEN = $Token }
})
New-Item -ItemType Directory -Force (Split-Path $CfgPath) | Out-Null
$cfg | ConvertTo-Json -Depth 10 | Set-Content -Encoding UTF8 $CfgPath
```

Then tell the user to fully quit and reopen Claude Desktop.

**Claude Code:**

```powershell
claude mcp add ewe --scope user -e EWE_MCP_HOST=127.0.0.1 -e EWE_MCP_PORT=9877 -e EWE_MCP_TOKEN=$Token -- $ServerExe
```

**OpenCode:** add to `opencode.json` (project) or `%USERPROFILE%\.config\opencode\opencode.json` (global):

```json
{
  "mcp": {
    "ewe": {
      "type": "local",
      "command": ["<ServerExe path>"],
      "environment": { "EWE_MCP_HOST": "127.0.0.1", "EWE_MCP_PORT": "9877", "EWE_MCP_TOKEN": "<Token>" }
    }
  }
}
```

**Codex:** add to `%USERPROFILE%\.codex\config.toml`:

```toml
[mcp_servers.ewe]
command = '<ServerExe path>'
env = { EWE_MCP_HOST = "127.0.0.1", EWE_MCP_PORT = "9877", EWE_MCP_TOKEN = "<Token>" }
```

## 7. Verify

1. Ask the user to start EwE (`ewe6.exe`) and wait for its main window.
2. Check that the plugin is listening and logging:

   ```powershell
   (Test-NetConnection 127.0.0.1 -Port 9877).TcpTestSucceeded
   Get-Content "$env:APPDATA\Ecopath with Ecosim\EwE_MCP_Plugin.log" -Tail 20
   ```

   The first command should print `True`.
3. After the user restarts their MCP client, call the `ping` tool. It should return `{"status": "ok"}`.

If port 9877 isn't listening, check in order: EwE is running, the DLL is in `$EwEDir\Plugins`, `$Platform` matches the EwE build (an x86 DLL won't load in 64-bit EwE, and vice versa), and the plugin log has no errors.

## 8. Report to the user

Tell the user:
- The EwE folder and architecture used.
- Where the plugin and the server were installed.
- Which MCP client was configured, and that it must be restarted.
- That EwE must be open before they ask the assistant to work with a model.
- To ask the assistant to **save as a copy** before it changes a model.
- That a sample model is at `%USERPROFILE%\EwE-MCP\tests\fixtures\Sample_Model.EwEmdb`.

## Updating

Rerun steps 3 and 5. Close EwE first. The config and token from step 4 stay as they are.

## Uninstalling

```powershell
Remove-Item "$EwEDir\Plugins\EwE_MCP_Plugin.dll"
uv tool uninstall ewe-mcp
Remove-Item "$env:APPDATA\Ecopath with Ecosim\EwE_MCP_Plugin.cfg"
```

Then remove the `ewe` entry from the MCP client config. Leave `Newtonsoft.Json.dll` in place if other plugins are installed.
