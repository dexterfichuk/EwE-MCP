# EwE MCP

External plugin-first MCP bridge for Ecopath with Ecosim.

The project follows the same two-piece shape as `qgis-mcp`:

- `src/EwE_MCP_Plugin`: .NET Framework 4.8 EwE plugin DLL that runs inside EwE and exposes a local authenticated socket.
- `src/ewe_mcp`: Python FastMCP server that turns that socket protocol into MCP tools/resources.

## Current Vertical Slice

Implemented now:

- Authenticated length-prefixed JSON socket protocol on `127.0.0.1:9877`.
- EwE plugin lifecycle: `IPlugin`, `IUIContextPlugin`, `IDisposedPlugin`.
- Core tools: `ping`, `diagnose`, `get_ewe_info`, `get_runtime_state`, `get_message_log`.
- Model/run tools: `load_model`, `save_model`, `save_model_as`, `close_model`, `get_model_info`, `get_core_counters`, `run_ecopath`, `run_ecosim`, `stop_ecosim`.
- UI command tools: `list_ui_commands`, `invoke_ui_command`.
- Python MCP server, socket client, batch command guard, and protocol tests.

The broad Ecopath/Ecosim/Ecospace/Ecotracer/MSE/Monte Carlo object model is intentionally not fully filled in yet; the bridge spine is ready for those tools.

## Local Configuration

Copy `.env.example` into your shell environment, or export values manually:

```bash
export EWE_MCP_TOKEN='replace-me'
export EWE_MCP_HOST=127.0.0.1
export EWE_MCP_PORT=9877
```

The plugin defaults to `ewe-mcp-local-dev` if no token is configured. That is for local smoke tests only.

## Build Plugin

Build against compiled EwE assemblies:

```powershell
$env:EWE_ASSEMBLY_DIR = "C:\Path\To\Eii.Ecopath-master\Sources"
.\scripts\build-plugin.ps1 -Configuration Release -Platform x86
```

On macOS, the checked-in project is still useful for editing, but `net48` plugin builds should be done on Windows or the Windows VM.

## VM Workflow

Never write the VM password into tracked files. Use `SSHPASS`:

```bash
export SSHPASS='...'
export EWE_VM_HOST=192.168.64.2
export EWE_VM_USER=dexter
export EWE_VM_PLUGIN_DIR='C:\Path\To\EwE\plugins'
export EWE_VM_EWE_DIR='C:\Path\To\EwE'
```

Deploy after building:

```bash
./scripts/deploy-plugin-vm.sh
```

Run a basic VM path smoke check:

```bash
./scripts/vm-smoke.sh
```

## Run Python MCP Server

```bash
uv run ewe-mcp
```

Or directly:

```bash
uv run python -m ewe_mcp.server
```

## Tests

```bash
uv run pytest
```
