# EwE MCP Project Plan

Last updated: 2026-05-13

## Purpose

EwE MCP is a plugin-first bridge that gives agents controlled, UI-equivalent access to a live Ecopath with Ecosim (EwE) desktop process.

The project follows a two-piece architecture:

- A C# .NET Framework 4.8 EwE plugin runs in-process and exposes the live EwE runtime through a local authenticated socket.
- A Python FastMCP server speaks to that socket and exposes stable MCP tools, diagnostics, prompts, and safety guards.

The Windows VM is the authoritative live integration environment. macOS local tests cover protocol, safety, and Python behavior; VM tests prove the plugin against real EwE assemblies, GUI state, and sample models.

## Current State

The core bridge is implemented and live-tested.

Implemented runtime foundations:

- EwE plugin lifecycle via `IPlugin`, `IUIContextPlugin`, and `IDisposedPlugin`.
- In-process access to `cCore`, `cUIContext`, `cCommandHandler`, and the EwE UI synchronization context.
- Local TCP socket protocol on `127.0.0.1:9877`.
- 4-byte big-endian length-prefixed UTF-8 JSON framing.
- Token authentication with development fallback token `ewe-mcp-local-dev`.
- Structured request/response envelopes with command id, status, result, and structured error fields.
- In-memory audit log and plugin file log at `%APPDATA%\Ecopath with Ecosim\EwE_MCP_Plugin.log`.
- Python socket client and FastMCP server in `src/ewe_mcp`.

Implemented safety foundations:

- Socket server binds to localhost by default.
- Python and plugin response limits are aligned at 10 MB.
- C# response serialization aborts through a size-limited stream before oversized payloads are sent.
- Python client disconnects when an oversized frame is announced to prevent protocol desync.
- `EWE_MCP_MAX_RESPONSE_SIZE` configures the Python client limit for local testing and client-side protection.
- `execute_ewe_code` is disabled by default and gated behind `EWE_MCP_ENABLE_EVAL=1`.
- `shutdown_server` is gated behind the same eval flag.
- Eval and shutdown are blocked from batch commands and audit-logged.
- Model lifecycle, simulation run/stop, export/capture, scenario mutation/load, eval, shutdown, and UI invocation commands are blocked from batch commands.
- `invoke_ui_command` is blocked from batch commands.
- Dialog-opening UI commands such as `ImportTimeSeries` are denied with `dialog_command_blocked`.
- In-plugin dialog watcher is disabled by default and only enabled by `dialog_watcher_enabled=true` or `EWE_MCP_DIALOG_WATCHER=1`.
- Dialog watcher is scoped to the current EwE process, uses `SendMessageTimeout` with a `PostMessage` fallback, and logs errors.
- Value reads/writes navigate EwE by default for human visibility; callers can pass `navigate=False` for quiet/background workflows.
- Live mutation tests restore original values and call `discard_changes`.

Implemented configuration:

- Environment variables override config file values.
- Plugin config file: `%APPDATA%\Ecopath with Ecosim\EwE_MCP_Plugin.cfg`.
- Config file requires `[EwE_MCP]`; key=value lines outside that section are ignored.
- Supported plugin keys:
  - `host`
  - `port`
  - `token`
  - `enabled`
  - `enable_eval`
  - `dialog_watcher_enabled`
  - `dialog_blocked_commands`
- `diagnose` reports config file path/existence and core plugin health.

## Implemented Tool Surface

Diagnostics:

- `ping`
- `diagnose`
- `get_ewe_info`
- `get_runtime_state`
- `get_message_log`
- `get_core_counters`
- `get_installed_plugins`
- `export_active_plot`

Model lifecycle:

- `load_model`
- `save_model`
- `save_model_as`
- `close_model`
- `get_model_info`
- `has_changes`
- `discard_changes`

Ecopath:

- `run_ecopath`
- `get_ecopath_run_state`
- `get_ecopath_stats`
- `get_ecopath_group_diagnostics`
- `get_ecopath_fleet_diagnostics`
- `export_ecopath_results`
- `list_ecopath_groups`
- `get_ecopath_group`
- `list_ecopath_fleets`
- `get_ecopath_fleet`

Ecopath run-state reporting now uses `StateMonitor.HasEcopathRan` plus `IsModelBalanced` for the stable public state and exposes raw internal `ecopath_detail` from Ecopath `RunState` for diagnostics.

Ecosim:

- `list_ecosim_scenarios`
- `get_ecosim_scenario`
- `load_ecosim_scenario`
- `create_ecosim_scenario`
- `copy_ecosim_scenario`
- `delete_ecosim_scenario`
- `save_ecosim_scenario`
- `get_ecosim_run_state`
- `wait_for_ecosim_completion`
- `run_ecosim`
- `stop_ecosim`
- `list_ecosim_group_inputs`
- `get_ecosim_group_input`
- `list_ecosim_fleet_inputs`
- `get_ecosim_fleet_input`
- `get_ecosim_group_results`
- `get_ecosim_fleet_results`
- `export_ecosim_results`

Ecospace:

- `list_ecospace_scenarios`
- `get_ecospace_scenario`
- `load_ecospace_scenario`
- `create_ecospace_scenario`
- `copy_ecospace_scenario`
- `delete_ecospace_scenario`
- `save_ecospace_scenario`
- `get_ecospace_run_state`
- `wait_for_ecospace_completion`
- `run_ecospace`
- `stop_ecospace`
- `list_ecospace_group_inputs`
- `get_ecospace_group_input`
- `list_ecospace_fleet_inputs`
- `get_ecospace_fleet_input`
- `get_ecospace_group_results`
- `get_ecospace_fleet_results`
- `list_ecospace_habitats`
- `get_ecospace_habitat`
- `list_ecospace_mpas`
- `get_ecospace_mpa`
- `get_ecospace_basemap`
- `get_ecospace_model_parameters`

Ecotracer:

- `list_ecotracer_scenarios`
- `get_ecotracer_scenario`
- `load_ecotracer_scenario`
- `create_ecotracer_scenario`
- `copy_ecotracer_scenario`
- `delete_ecotracer_scenario`
- `save_ecotracer_scenario`
- `close_ecotracer_scenario`
- `get_ecotracer_run_state`
- `get_ecotracer_model_parameters`
- `list_ecotracer_group_inputs`
- `get_ecotracer_group_input`
- `get_ecotracer_group_results`

There is no standalone `run_ecotracer`; Ecotracer runs as part of Ecosim/Ecospace when contaminant tracing is enabled.

MSE:

- `get_mse_parameters`
- `list_mse_group_inputs`
- `get_mse_group_input`
- `list_mse_fleet_inputs`
- `get_mse_fleet_input`
- `get_mse_run_state`
- `run_mse`
- `stop_mse`
- `get_mse_group_results`
- `get_mse_fleet_results`

Monte Carlo:

- `get_monte_carlo_parameters`
- `list_monte_carlo_groups`
- `get_monte_carlo_group`
- `get_monte_carlo_run_state`
- `run_monte_carlo`
- `stop_monte_carlo`

Generic entity/value layer:

- `list_domains`
- `list_entities`
- `get_entity`
- `list_variables`
- `get_variable_metadata`
- `get_values`
- `validate_values`
- `set_values`
- `get_indexed_value`
- `validate_indexed_value`
- `set_indexed_value`

Navigation/UI:

- `list_navigation_nodes`
- `navigate_to_node`
- `get_active_view`
- `refresh_ui`
- `invoke_ui_command`
- `list_ui_commands`
- `capture_view`
- `save_screenshot`

Batching, server control, prompts:

- `batch`
- `batch_commands`
- `execute_ewe_code` (disabled by default)
- `shutdown_server` (disabled by default)
- Prompt templates: `balance_model`, `run_ecosim_scenario`, `compare_scenarios`, `export_report`

## Verification Baseline

Latest verified on PR #83 after merging current `main` and deploying to the Windows VM:

```text
uv run pytest
69 passed, 83 skipped
```

```text
EWE_LIVE_TESTS=1 uv run pytest
144 passed, 8 skipped
```

Windows VM plugin build:

```text
0 warnings, 0 errors
```

Focused VM probes verified:

- EwE plugin socket starts on `127.0.0.1:9877`.
- `diagnose` reports healthy core, UI context, command handler, token, and log path.
- Dialog watcher is disabled by default.
- `invoke_ui_command` rejects `ImportTimeSeries` with `dialog_command_blocked`.
- `run_ecopath` on `Sample_Model.EwEmdb` returns `ecopath_run_state: balanced`.
- `ecopath_detail` returns `Balanced`.
- Mutation cleanup path uses `discard_changes` and `close_model`.

## VM Runbook

Use the VM password only through the local shell environment:

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

If working from a worktree whose directory name is not `EwE-MCP`, set:

```bash
export EWE_VM_REMOTE_ROOT="C:/Users/dexter/$(basename "$PWD")"
```

Build, deploy, launch:

```bash
./scripts/build-plugin-vm.sh
./scripts/deploy-built-plugin-vm.sh
./scripts/launch-ewe-vm.sh
```

Smoke test:

```bash
./scripts/vm-live-smoke.sh
```

Manual tunnel:

```bash
sshpass -e ssh -f -N \
  -L 19877:127.0.0.1:9877 \
  -o StrictHostKeyChecking=no \
  dexter@192.168.64.2
```

Run live tests:

```bash
EWE_MCP_HOST=127.0.0.1 \
EWE_MCP_PORT=19877 \
EWE_MCP_TOKEN=ewe-mcp-local-dev \
EWE_LIVE_TESTS=1 \
uv run pytest
```

Close tunnel:

```bash
pkill -f 'ssh.*19877:127.0.0.1:9877' || true
```

## Operational Rules

- Treat the live EwE process and sample models as shared state.
- Inspect before mutating.
- Prefer copied models for mutation tests.
- If a shared sample model is mutated, restore the original value in `finally`, then call `discard_changes`.
- `discard_changes` clears dirty flags but does not physically revert values.
- Do not save over the shared sample model.
- Do not enable `execute_ewe_code`, `shutdown_server`, or the in-plugin dialog watcher unless the workflow explicitly requires it.
- Prefer data-reading tools for tables and numerical data.
- Use `capture_view`/`save_screenshot` for visual charts, maps, and spatial displays.
- Use `export_active_plot` for structured chart series data when a ZedGraph plot is active.

## Recent Milestones

PR #82: response-size protection

- Lowered Python default response size limit to 10 MB.
- Added `ResponseTooLargeError`.
- Disconnects the Python socket on oversized frame headers.
- Added C# outbound response size guard.
- Reworked C# serialization to stop through a size-limited stream.
- Reduced known response bloat sources, including safer model metadata and capture defaults.

PR #83: dialog safety, Ecopath state, metadata correctness

- Fixed indexed property writability reporting in metadata.
- Replaced fragile Ecopath run-state reporting with `StateMonitor` plus raw `ecopath_detail`.
- Added disabled-by-default dialog watcher.
- Blocked dialog-opening UI commands in MCP mode.
- Added configurable extra dialog-blocked commands.
- Added tests and VM probes for dialog blocking and Ecopath detail.

## Remaining Work

Near-term documentation cleanup:

- Update `README.md` to match the current config keys, response-size behavior, dialog watcher behavior, and test baselines.
- Update `installer/EwE_MCP_Plugin.cfg` with commented examples for `enabled`, `enable_eval`, `dialog_watcher_enabled`, and `dialog_blocked_commands`.
- Update `RELEASE_CHECKLIST.md` baselines and add PR #82/#83 top-hat checks.
- Decide whether `docs/superpowers/plans/*` should be archived, removed, or left as process history.

Release/distribution:

- Build and validate Python wheel with `uv build`.
- Verify wheel install into a fresh virtual environment.
- Build and verify the Inno Setup installer.
- Verify installer/uninstaller behavior on the Windows VM.
- Confirm installer writes a valid `[EwE_MCP]` config section.

Live coverage to consider:

- More targeted tests for response-size behavior through the deployed plugin.
- A supervised dialog-watcher-enabled test that opens a known harmless OK-only dialog and verifies dismissal/logging.
- More live tests for Ecospace/Ecotracer/MSE/Monte Carlo run/stop edge cases.
- More regression tests around UI command availability/status.

Product/API polish:

- Decide whether to expose a dedicated time-series import tool instead of relying on blocked EwE UI commands.
- Consider pagination or slicing for large result domains.
- Consider a richer `get_runtime_state` summary that highlights loaded model, dirty flag, active scenario, and last run states.
- Review prompt templates against the current tool surface and update examples.

## File Roles

- `README.md`: user-facing overview, quickstart, current tool/config summary.
- `AGENTS.md`: operational agent runbook for VM, live tests, safety rules, and shared-state handling.
- `EwE-MCP-Plan.md`: this living project status and roadmap.
- `RELEASE_CHECKLIST.md`: release verification checklist.
- `docs/superpowers/plans/*`: historical/process plans only; not the canonical project roadmap.

## Rollback

The integration is optional and external. To disable without uninstalling:

```ini
[EwE_MCP]
enabled=0
```

Or set:

```bash
export EWE_MCP_ENABLED=0
```

To remove the plugin, close EwE and delete from the installed EwE `Plugins` folder:

- `EwE_MCP_Plugin.dll`
- `Newtonsoft.Json.dll`, if it is only present for EwE MCP
