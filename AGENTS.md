# Agent Instructions

This repo builds an MCP bridge for a live Ecopath with Ecosim (EwE) desktop process. Treat the EwE process and sample models as shared state: inspect first, mutate carefully, and restore anything you change.

## Quick Context
- Main repo: `/Users/dexterfichuk/Projects/EwE-MCP`
- EwE source/reference tree: `/Users/dexterfichuk/Downloads/Eii.Ecopath-master`
- Live Windows VM: `dexter@192.168.64.2`
- Installed EwE path on VM: `C:\Program Files (x86)\Ecopath with Ecosim 40 years 6.7.0.18865_32-bit`
- Default plugin socket inside VM: `127.0.0.1:9877`
- Recommended local tunnel: `127.0.0.1:19877 -> VM 127.0.0.1:9877`
- Development token: `ewe-mcp-local-dev`
- Sample model: `C:\Users\dexter\Eii.Ecopath\Database\Tampa_Bay.EwEmdb`

## MCP Client Config

Add to your `opencode.jsonc` or MCP client config, pointing at the installed `ewe-mcp.exe`:

```jsonc
{
  "mcpServers": {
    "ewe": {
      "command": "C:\\Program Files\\EwE-MCP\\ewe-mcp.exe",
      "env": {
        "EWE_MCP_HOST": "127.0.0.1",
        "EWE_MCP_PORT": "9877",
        "EWE_MCP_TOKEN": "ewe-mcp-local-dev"
      }
    }
  }
}
```

Adjust the `command` path to wherever the installer placed `ewe-mcp.exe`. For development from source: `"command": "uv", "args": ["run", "--directory", "/path/to/EwE-MCP", "ewe-mcp"]`.

Never commit credentials. Put the VM password in the `SSHPASS` environment variable before using `sshpass` commands.

## Start Safely
Before editing or deploying, check the local state and leave user scratch files alone:

```bash
cd /Users/dexterfichuk/Projects/EwE-MCP
git status --short --branch
git fetch origin
uv sync
uv run pytest
```

Live tests are skipped unless `EWE_LIVE_TESTS=1` is set.

## VM Environment
Use these exports for build/deploy/live work:

```bash
export SSHPASS="${SSHPASS:?Set the VM password}"
export EWE_VM_HOST=192.168.64.2
export EWE_VM_USER=dexter
export EWE_VM_EWE_DIR='C:\Program Files (x86)\Ecopath with Ecosim 40 years 6.7.0.18865_32-bit'
export EWE_BUILD_CONFIGURATION=Release
export EWE_BUILD_PLATFORM=x86
export EWE_MCP_TOKEN=ewe-mcp-local-dev
export EWE_MCP_LOCAL_TUNNEL_PORT=19877
export EWE_MCP_REMOTE_PORT=9877
```

Optional: set `EWE_MCP_ENABLED=0` before launching EwE to keep the plugin installed but prevent it from binding the socket. Leave it unset for normal build/deploy/live work.

Response-size guard: plugin and Python defaults are aligned at 10 MB. The C# plugin emits a structured `response_too_large` error instead of sending oversized frames, and the Python client disconnects if an oversized frame is announced. For client-side testing only, adjust `EWE_MCP_MAX_RESPONSE_SIZE`; the plugin limit is compiled at 10 MB.

Dialog watcher: set `EWE_MCP_DIALOG_WATCHER=1` or add `dialog_watcher_enabled=true` to the config file to enable the in-plugin dialog watcher that auto-dismisses blocking modal dialogs. Disabled by default; prefer leaving it off unless a supervised workflow explicitly needs it. Extend the dialog-opening command denylist with `EWE_MCP_DIALOG_BLOCKED_COMMANDS=cmd1,cmd2` or `dialog_blocked_commands=cmd1,cmd2` in the config file. `ImportTimeSeries` and `m_cmdImportTimeSeries` are blocked by default through `dialog_command_blocked`.

**Dialog Events:** When the in-plugin dialog watcher is enabled (`dialog_watcher_enabled=true` or `EWE_MCP_DIALOG_WATCHER=1`), the plugin captures modal dialog title + body text, classifies it as safe or risky, and delivers it as `_events` on the next command response. Safe informational dialogs (OK-only, no destructive keywords) are auto-dismissed; risky dialogs (Yes/No, destructive actions) are reported but not dismissed. Use `get_dialog_events` to fetch pending events, or check `_events` in any command response.

If you are working from a separate worktree whose directory name is not `EwE-MCP`, either use scripts that derive the remote root from the worktree name or set:

```bash
export EWE_VM_REMOTE_ROOT="C:/Users/dexter/$(basename "$PWD")"
```

## Build, Deploy, Launch

The plugin reads configuration from `EwE_MCP_Plugin.cfg` at `%APPDATA%\Ecopath with Ecosim\` (INI-style with a required `[EwE_MCP]` section header; key=value lines outside this section are ignored). Configuration precedence: environment variables > config file > compiled defaults. The `diagnose` command reports `config_file_path` and `config_file_exists` so you can verify which file is loaded.

Supported config keys are `host`, `port`, `token`, `enabled`, `enable_eval`, `dialog_watcher_enabled`, and `dialog_blocked_commands`.

Build the plugin on the Windows VM, deploy it into EwE's plugin folder, and launch EwE through the interactive desktop session:

```bash
./scripts/build-plugin-vm.sh
./scripts/deploy-built-plugin-vm.sh
./scripts/launch-ewe-vm.sh
```

Important details:
- `deploy-built-plugin-vm.sh` stops `ewe6.exe` by default so Windows releases the plugin DLL.
- Do not launch EwE directly with SSH `Start-Process`; it can start in the SSH service session and exit before the socket stays available.
- `launch-ewe-vm.sh` also starts an interactive dialog sweeper for a short window (`EWE_VM_DIALOG_WATCH_SECONDS`, default `300`) so EwE modal error dialogs with an `OK` button do not block live tests.
- A good launch reports `TcpTestSucceeded True` for `127.0.0.1:9877`.
- Plugin logs are on the VM at `%APPDATA%\Ecopath with Ecosim\EwE_MCP_Plugin.log`.
- Dialog sweeper dismissals are logged at `%APPDATA%\Ecopath with Ecosim\EwE_MCP_DialogSweeper.log`.

## Connect Through A Tunnel
The plugin binds to VM localhost, so connect from macOS with an SSH tunnel:

```bash
sshpass -e ssh -f -N \
  -L 19877:127.0.0.1:9877 \
  -o StrictHostKeyChecking=no \
  dexter@192.168.64.2
```

Then point the Python MCP/client at the local tunnel:

```bash
export EWE_MCP_HOST=127.0.0.1
export EWE_MCP_PORT=19877
export EWE_MCP_TOKEN=ewe-mcp-local-dev
```

Close the tunnel when done:

```bash
pkill -f 'ssh.*19877:127.0.0.1:9877' || true
```

## Smoke Test
Use the helper first:

```bash
./scripts/vm-live-smoke.sh
```

Or probe manually:

```bash
EWE_MCP_HOST=127.0.0.1 EWE_MCP_PORT=19877 EWE_MCP_TOKEN=ewe-mcp-local-dev uv run python - <<'PY'
from ewe_mcp.client import EwEMCPClient

client = EwEMCPClient()
assert client.connect()
print(client.command_result("ping"))
print(client.command_result("diagnose"))
print(client.command_result("get_ewe_info"))
print(client.command_result("load_model", {"path": r"C:\Users\dexter\EwE-MCP\tests\fixtures\Tampa_Bay.EwEmdb"}, timeout=60))
print(client.command_result("get_core_counters"))
client.disconnect()
PY
```

## Run The MCP Server
After the tunnel is open:

```bash
EWE_MCP_HOST=127.0.0.1 \
EWE_MCP_PORT=19877 \
EWE_MCP_TOKEN=ewe-mcp-local-dev \
uv run ewe-mcp
```

The MCP server exposes tools backed by the running EwE plugin. Current tools include:

- **Diagnostics:** `ping`, `diagnose`, `get_ewe_info`, `get_runtime_state`, `get_message_log`, `get_core_counters`, `get_installed_plugins`, `export_active_plot`, `get_dialog_events`
- **Model lifecycle:** `load_model`, `save_model`, `save_model_as`, `close_model`, `get_model_info`, `has_changes`, `discard_changes`
- **Ecopath:** `run_ecopath`, `get_ecopath_run_state`, `get_ecopath_stats`, `get_ecopath_group_diagnostics`, `get_ecopath_fleet_diagnostics`, `export_ecopath_results`, `list_ecopath_groups`, `get_ecopath_group`, `list_ecopath_fleets`, `get_ecopath_fleet`. Ecopath run state uses `StateMonitor.HasEcopathRan` + `IsModelBalanced`; `ecopath_detail` exposes the raw internal `RunState` for diagnostics.
- **Ecosim:** `list_ecosim_scenarios`, `get_ecosim_scenario`, `load_ecosim_scenario`, `create_ecosim_scenario`, `copy_ecosim_scenario`, `delete_ecosim_scenario`, `save_ecosim_scenario`, `get_ecosim_run_state`, `wait_for_ecosim_completion`, `run_ecosim`, `stop_ecosim`, `list_ecosim_group_inputs`, `get_ecosim_group_input`, `list_ecosim_fleet_inputs`, `get_ecosim_fleet_input`, `get_ecosim_group_results`, `get_ecosim_fleet_results`, `export_ecosim_results`
- **Ecospace:** `list_ecospace_scenarios`, `get_ecospace_scenario`, `load_ecospace_scenario`, `get_ecospace_run_state`, `run_ecospace`, `stop_ecospace`, `list_ecospace_group_inputs`, `get_ecospace_group_input`, `list_ecospace_fleet_inputs`, `get_ecospace_fleet_input`, `get_ecospace_group_results`, `get_ecospace_fleet_results`, `list_ecospace_habitats`, `get_ecospace_habitat`, `list_ecospace_mpas`, `get_ecospace_mpa`, `get_ecospace_basemap`, `get_ecospace_model_parameters`, `create_ecospace_scenario`, `copy_ecospace_scenario`, `delete_ecospace_scenario`, `save_ecospace_scenario`, `wait_for_ecospace_completion`
- **Ecotracer:** `list_ecotracer_scenarios`, `get_ecotracer_scenario`, `load_ecotracer_scenario`, `get_ecotracer_run_state`, `get_ecotracer_model_parameters`, `list_ecotracer_group_inputs`, `get_ecotracer_group_input`, `get_ecotracer_group_results`, `create_ecotracer_scenario`, `copy_ecotracer_scenario`, `delete_ecotracer_scenario`, `save_ecotracer_scenario`, `close_ecotracer_scenario`
- **MSE:** `get_mse_parameters`, `list_mse_group_inputs`, `get_mse_group_input`, `list_mse_fleet_inputs`, `get_mse_fleet_input`, `get_mse_run_state`, `run_mse`, `stop_mse`, `get_mse_group_results`, `get_mse_fleet_results`
- **Monte Carlo:** `get_monte_carlo_parameters`, `list_monte_carlo_groups`, `get_monte_carlo_group`, `get_monte_carlo_run_state`, `run_monte_carlo`, `stop_monte_carlo`
- **Generic entity/value:** `list_domains`, `list_entities`, `get_entity`, `list_variables`, `get_variable_metadata`, `get_values`, `validate_values`, `get_indexed_value`, `validate_indexed_value`, `set_values`, `set_indexed_value`, `search_entities`, `get_values_range`, `set_values_range`
- **Name-based batch updates:** `list_name_batch_capabilities`, `set_values_by_name_batch`
- **Navigation/UI:** `list_navigation_nodes`, `navigate_to_node`, `get_active_view`, `invoke_ui_command`, `list_ui_commands`, `refresh_ui`, `capture_view`, `save_screenshot`
- **Server control:** `shutdown_server`
- **Batching:** `batch_commands`
- **Eval (disabled by default):** `execute_ewe_code`
- **Prompt templates:** `balance_model`, `run_ecosim_scenario`, `compare_scenarios`, `export_report`

For view capture tools (`capture_view` and `save_screenshot`), prefer using data-reading tools to extract tabular data, and reserve view capture for graphs, charts, and spatial visualizations. For extracting numerical data from charts, use `export_active_plot` which returns series names, X/Y data points, and axis labels as structured JSON.

Value tools navigate EwE's GUI by default so humans can see what the agent is inspecting or editing:

- `get_values`
- `get_indexed_value`
- `set_values`
- `set_indexed_value`

Pass `navigate=False` for quiet/background reads, loops, batches, or any workflow where UI movement would be distracting.

Common UI navigation examples:

```python
await navigate_to_node(id="ndDietComposition")
await get_indexed_value(domain="ecopath_group", index=20, variable="diet_comp", indices=[25])
await set_values(domain="ecopath_group", index=1, values={"name": "0-12 Snook"}, navigate=False)
```

## Live Tests
Run live tests only after build/deploy/launch/smoke succeeds:

```bash
EWE_MCP_HOST=127.0.0.1 \
EWE_MCP_PORT=19877 \
EWE_MCP_TOKEN=ewe-mcp-local-dev \
EWE_LIVE_TESTS=1 \
uv run pytest
```

Expected local baseline with live tests skipped is currently `199 passed, 105 skipped`.

The sample model (`tests/fixtures/Tampa_Bay.EwEmdb`) is bundled under the same GPL-3.0 license as EwE and does not require a separate `EWE_MODEL_PATH` environment variable.

Expected full live result is `297 passed, 7 skipped`.

Prefer testing mutations against copied models. If you mutate the shared sample model, restore the original value in a `finally` block and call `discard_changes` so EwE clears the unsaved-change flag without saving over the sample model. `discard_changes` only clears dirty flags; it does not physically revert values.

## Safety Rules
- Keep `execute_ewe_code` disabled unless the user explicitly asks for it and understands the risk.
  - Disabled by default; gated behind `EWE_MCP_ENABLE_EVAL=1`.
  - Deny patterns block expressions containing: `System.IO`, `System.Diagnostics.Process`, `System.Reflection.Assembly.Load`, `File.Delete`, `File.Write`, `Directory.Delete`, `System.Net`, `WebClient`, `HttpClient`, `Process.Start`.
  - Blocked from `batch_commands`.
  - Every eval request is audit-logged with outcome (`denied`/`executed`).
- `shutdown_server` is gated behind `EWE_MCP_ENABLE_EVAL=1` (same flag as `execute_ewe_code`), blocked from `batch_commands`, and audit-logged. It is destructive — it stops the plugin socket server.
- Do not batch `execute_ewe_code`, `shutdown_server`, `invoke_ui_command`, `set_values`, `set_indexed_value`, model lifecycle operations, simulation run/stop commands, export/capture commands, or scenario load/mutation operations. The plugin enforces this guard for direct socket clients as well as the Python MCP server.
- Do not use `invoke_ui_command` for commands that open modal dialogs. Known dialog-opening commands such as `ImportTimeSeries` are blocked by default with `dialog_command_blocked`; use a dedicated MCP tool when available or operate the EwE GUI manually.
- Use `validate_values` before `set_values` when possible.
- Value reads/writes navigate by default through `get_values`, `get_indexed_value`, `set_values`, and `set_indexed_value`; pass `navigate=False` for quiet/background access, batches, or batch-like workflows to avoid UI flicker.
- Use `discard_changes` only as an explicit cleanup step after test mutations have been restored; it clears EwE's dirty flag but does not physically revert values.
- For model-changing workflows, prefer `save_model_as` to a temporary/copy path instead of overwriting the shared model.
- `export_ecopath_results` and `export_ecosim_results` validate that the path has a `.csv` or `.txt` extension and reject directory traversal (`..`).
- `save_model_as` and `save_model(path=...)` validate path traversal.
- `capture_view` and `save_screenshot` target parameter is case-insensitive (normalized to lowercase).
- `batch_commands` accepts both `type` and `command_type` keys in inner commands (both are normalized to `type` before sending).
- `import_time_series` no longer silently zero-pads short rows — rows with mismatched column counts will raise an error.
- `stop_ecosim` uses `TIMEOUT_LONG` (120s) to accommodate long-running simulations.
- Ecotracer runs as part of Ecosim/Ecospace when `ContaminantTracing` is enabled — there is no standalone `run_ecotracer` command.
- MSE and Monte Carlo are configured within the Ecosim scenario context; there are no scenario management commands for them.
- `set_values_by_name_batch` performs exact-name resolution before mutation. Fuzzy matching is suggestion-only and never auto-applies values. For multi-domain edits, prefer `set_values_by_name_batch` over ad-hoc index lookup loops.
- Record what you changed and what you restored in your final message.

## Troubleshooting
- DLL copy fails: EwE is probably still running; rerun `./scripts/deploy-built-plugin-vm.sh`.
- Port is closed after launch: inspect `%APPDATA%\Ecopath with Ecosim\EwE_MCP_Plugin.log` on the VM.
- EwE modal dialog blocks live tests: run `./scripts/dismiss-ewe-dialogs-vm.sh` or relaunch with `./scripts/launch-ewe-vm.sh`; the launch helper starts the same sweeper automatically.
- Python cannot connect: confirm the tunnel, `EWE_MCP_HOST`, `EWE_MCP_PORT`, and token.
- EwE exits after launch: use `./scripts/launch-ewe-vm.sh`, not direct SSH GUI launch.
- SSH password auth flakes: retry with explicit options: `-o PreferredAuthentications=password -o PubkeyAuthentication=no`.
