# EwE-MCP: Windows Setup Guide

Install the plugin and MCP server on your EwE machine, then connect from any MCP client (opencode, ChatGPT, Claude, etc).

## Step 1: Run the Installer

1. Run `EwE-MCP-Setup.exe` as Administrator
2. The installer prompts you for:
   - **EwE installation directory** — where `ewe6.exe` lives (auto-detected)
   - **EwE architecture** — 32-bit or 64-bit
   - **Agent instructions directory** — where `AGENT_SETUP.md` will be saved
3. It installs:
   - `EwE_MCP_Plugin.dll` into `{EwE dir}\Plugins\`
   - `ewe-mcp.exe` and supporting files into `{install dir}\`
   - `AGENT_SETUP.md` with your specific paths
4. Launch EwE normally — the plugin starts automatically and listens on `127.0.0.1:9877`
5. Verify in PowerShell:

```powershell
Test-NetConnection 127.0.0.1 -Port 9877 | Select-Object TcpTestSucceeded
```

Should print `True`. Plugin log: `%APPDATA%\Ecopath with Ecosim\EwE_MCP_Plugin.log`

## Step 2: Start the MCP Server

The installer placed `ewe-mcp.exe` at `{install dir}\ewe-mcp.exe`. Start it:

```cmd
set EWE_MCP_HOST=127.0.0.1
set EWE_MCP_PORT=9877
set EWE_MCP_TOKEN=ewe-mcp-local-dev
"{install dir}\ewe-mcp.exe"
```

Or in PowerShell:

```powershell
$env:EWE_MCP_HOST="127.0.0.1"
$env:EWE_MCP_PORT="9877"
$env:EWE_MCP_TOKEN="ewe-mcp-local-dev"
& "{install dir}\ewe-mcp.exe"
```

Keep this terminal window open — the server must stay running while your agent works.

## Step 3: Configure Your MCP Client

### opencode

Create `opencode.jsonc` in your project and point it at the installed `ewe-mcp.exe`:

```jsonc
{
  "mcpServers": {
    "ewe": {
      "command": "C:\\Path\\To\\EwE-MCP\\ewe-mcp.exe",
      "env": {
        "EWE_MCP_HOST": "127.0.0.1",
        "EWE_MCP_PORT": "9877",
        "EWE_MCP_TOKEN": "ewe-mcp-local-dev"
      }
    }
  }
}
```

Now ask opencode: *"Load the sample model at C:\Users\<you>\EwE-MCP\tests\fixtures\Sample_Model.EwEmdb and run Ecopath."*

### ChatGPT (paid plan with MCP)

Add a custom MCP server in ChatGPT's settings:
- Command: `C:\Path\To\EwE-MCP\ewe-mcp.exe`
- Environment: `EWE_MCP_HOST=127.0.0.1`, `EWE_MCP_PORT=9877`, `EWE_MCP_TOKEN=ewe-mcp-local-dev`

### Claude Desktop

Add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "ewe": {
      "command": "C:\\Path\\To\\EwE-MCP\\ewe-mcp.exe",
      "env": {
        "EWE_MCP_HOST": "127.0.0.1",
        "EWE_MCP_PORT": "9877",
        "EWE_MCP_TOKEN": "ewe-mcp-local-dev"
      }
    }
  }
}
```

## First Commands to Try

Once your agent is connected, ask it to:

1. `ping` — should return `{"pong": true}`
2. `diagnose` — shows plugin version, config, health checks
3. `load_model` with a path to an `.EwEmdb` file on your machine
4. `list_ecopath_groups` — shows functional groups
5. `run_ecopath` — runs mass balance

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `Connection refused` | EwE not running, or plugin didn't load. Restart EwE, check plugin log. |
| `Unauthorized` | Token mismatch. Set same token in plugin config and client env. |
| `No model loaded` | Call `load_model(path=...)` with a full path to a `.EwEmdb` file. |
| `Modal dialog blocking` | Set `EWE_MCP_DIALOG_WATCHER=1` env var and restart EwE. |

## Token Configuration

Default dev token: `ewe-mcp-local-dev`. To change it, edit `%APPDATA%\Ecopath with Ecosim\EwE_MCP_Plugin.cfg`:

```ini
[EwE_MCP]
token=your-secret-token
```

Then set the same `EWE_MCP_TOKEN` in your MCP client config and server environment.
