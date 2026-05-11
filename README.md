# EwE MCP

External plugin-first MCP bridge for Ecopath with Ecosim.

Agents working in this repo should start with [AGENTS.md](/Users/dexterfichuk/Projects/EwE-MCP/AGENTS.md) for VM connection, launch, tunnel, MCP usage, and safety instructions.

The project follows the same two-piece shape as `qgis-mcp`:

- `src/EwE_MCP_Plugin`: .NET Framework 4.8 EwE plugin DLL that runs inside EwE and exposes a local authenticated socket.
- `src/ewe_mcp`: Python FastMCP server that turns that socket protocol into MCP tools/resources.

## Current Vertical Slice

Implemented now:

- Authenticated length-prefixed JSON socket protocol on `127.0.0.1:9877`.
- EwE plugin lifecycle: `IPlugin`, `IUIContextPlugin`, `IDisposedPlugin`.
- Core tools: `ping`, `diagnose`, `get_ewe_info`, `get_runtime_state`, `get_message_log`.
- Model/run tools: `load_model`, `save_model`, `save_model_as`, `close_model`, `get_model_info` (which extracts robust model database metadata via reflection), `has_changes`, `discard_changes`, `get_core_counters`, `run_ecopath`, `run_ecosim`, `stop_ecosim`.
- Ecopath control tools: `run_ecopath` (returns `is_model_balanced` and `ecopath_run_state`), `get_ecopath_run_state` (read-only balancing state check), `get_ecopath_stats` (system-level statistics like throughput, connectance, diversity), `get_ecopath_group_diagnostics` (EE/PB/GE/biomass/trophic level for all groups), `get_ecopath_fleet_diagnostics` (catch/landings/discards by group for all fleets), `export_ecopath_results` (CSV export to caller-specified path).
- Read-only Ecopath tools: `list_domains`, `list_entities`, `list_ecopath_groups`, `get_ecopath_group`, `list_ecopath_fleets`, `get_ecopath_fleet`. The generic data layer seamlessly exposes deep Ecopath objects including stanzas, detritus fate arrays, the full diet matrix, remarks, and balancing diagnostics (`eeoutput`, `pboutput`, `respiration`, etc.).
- Ecosim control tools: scenario list/get/load/create/copy/delete/save, `get_ecosim_run_state`, `wait_for_ecosim_completion` (polls until completion with timeout), group/fleet input inspection, group/fleet result time-series, and reflected `export_ecosim_results` for installed-build compatibility. Ecospace and Ecotracer scenario loading tools are also available.
- Generic entity/value tools: `get_entity`, `list_variables`, `get_variable_metadata`, `get_values`, `validate_values`.
- Indexed value tools: `get_indexed_value`, `validate_indexed_value`, `set_indexed_value` for targeted cell-level control such as `DietComp(prey)` or `Landings(group)`.
- Value tools: `get_values`, `get_indexed_value`, `set_values`, and `set_indexed_value` navigate EwE to the relevant screen by default; pass `navigate=False` for quiet/background access, especially in batches to avoid UI flicker. `set_values` is generic and works across all domains (Ecopath, Ecosim, Ecospace, Ecotracer, taxa, core); `set_indexed_value` supports targeted indexed cells.
- Expanded generic domains now include Ecopath, Ecosim, Ecospace, Ecotracer, taxa, `core`, and UI commands. Some subsystem entity collections remain unavailable until EwE initializes or loads the relevant scenario.
- UI command/navigation tools: `list_ui_commands`, `invoke_ui_command`, `list_navigation_nodes`, `navigate_to_node`, `get_active_view`.
- View capture tools: `capture_view` and `save_screenshot` (primarily for graphs and spatial visualizations; prefer data-reading tools for tables).
- Python MCP server, socket client, batch command guard, and protocol tests.

The broad Ecospace/Ecotracer/MSE/Monte Carlo object model and Ecosim progress event subscription are intentionally not fully filled in yet; the bridge spine and first Ecosim scenario/run/results loop are ready for expansion.

## Local Configuration

Copy `.env.example` into your shell environment, or export values manually:

```bash
export EWE_MCP_TOKEN='replace-me'
export EWE_MCP_HOST=127.0.0.1
export EWE_MCP_PORT=9877
```

The plugin defaults to `ewe-mcp-local-dev` if no token is configured. That is for local smoke tests only.

The plugin can be disabled without uninstalling DLLs:

```bash
export EWE_MCP_ENABLED=0
```

When disabled, the plugin logs the disabled state and does not bind the socket. Leave `EWE_MCP_ENABLED` unset, or set it to any value other than `0`, for normal operation.

Client calls include a timeout value in the socket request. If a command times out or an in-flight socket operation fails, the Python client closes that socket before re-raising so a late response cannot be read by the next command.

## Build Plugin

Build against either an installed EwE directory or compiled EwE assemblies:

```powershell
$env:EWE_ASSEMBLY_DIR = "C:\Program Files (x86)\Ecopath with Ecosim 40 years 6.7.0.18865_32-bit"
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
./scripts/build-plugin-vm.sh
./scripts/deploy-built-plugin-vm.sh
```

Launch EwE in the VM's interactive desktop session, then smoke-test the live plugin through an SSH tunnel:

```bash
./scripts/launch-ewe-vm.sh
./scripts/vm-live-smoke.sh
```

Launching through an interactive scheduled task is intentional: starting the GUI directly from the SSH service session can exit before the socket stays available. The launch helper also starts a short-lived EwE dialog sweeper so modal EwE error dialogs with an `OK` button do not block live tests. If a dialog is already blocking the VM, run:

```bash
./scripts/dismiss-ewe-dialogs-vm.sh
```

Set `EWE_VM_DIALOG_WATCH_SECONDS` to change how long the sweeper watches. Dismissals are logged at `%APPDATA%\Ecopath with Ecosim\EwE_MCP_DialogSweeper.log` on the VM.

The live Ecopath read-only smoke path has been verified with:

```text
C:\Users\dexter\Eii.Ecopath\Database\Tampa_Bay.EwEmdb
```

## Run Python MCP Server

```bash
uv run ewe-mcp
```

Or directly:

```bash
uv run python -m ewe_mcp.server
```

## Navigation And Value Tools

The MCP is designed to make EwE's GUI show what an agent is doing. These tools navigate EwE by default:

- `get_values`
- `get_indexed_value`
- `set_values`
- `set_indexed_value`

Pass `navigate=False` for quiet/background reads, loops, or batches where UI flicker would be distracting.

Useful navigation calls:

```python
await list_navigation_nodes()
await navigate_to_node(id="ndDietComposition")
await get_active_view()
```

Example value reads/writes:

```python
await get_indexed_value(
    domain="ecopath_group",
    index=20,
    variable="diet_comp",
    indices=[25],
)

await set_values(
    domain="ecopath_group",
    index=1,
    values={"name": "0-12 Snook"},
    navigate=False,
)
```

`discard_changes` is available for test cleanup after restored mutations. It clears EwE's unsaved-change flag; it does not physically revert values.

## Tests

```bash
uv run pytest
```

Live VM tests require a built/deployed/launched EwE process and tunnel:

```bash
EWE_MCP_HOST=127.0.0.1 \
EWE_MCP_PORT=19877 \
EWE_MCP_TOKEN=ewe-mcp-local-dev \
EWE_LIVE_TESTS=1 \
uv run pytest
```

The sample model is bundled at `tests/fixtures/Tampa_Bay.EwEmdb` (GPL-3.0, same license as EwE) and does not require a separate `EWE_MODEL_PATH` environment variable.
