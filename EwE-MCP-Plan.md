# EwE MCP Implementation Plan

## Objective
Build EwE MCP as a separate plugin-first project in `/Users/dexterfichuk/Projects/EwE-MCP`. The goal is to give an agent UI-equivalent control of a running Ecopath with Ecosim (EwE) instance through MCP, following the QGIS MCP pattern:

- A C# `.NET Framework 4.8` EwE plugin runs in-process and exposes the live EwE runtime over a local authenticated socket.
- A Python FastMCP server exposes stable tools, resources, diagnostics, batching, and eventually plot/screenshot/result resources.
- Windows VM testing is the primary live integration loop.

## Current Status

### Completed
- Created the standalone project, separate from the EwE source tree.
- Created and pushed a private GitHub repository: `https://github.com/dexterfichuk/EwE-MCP`.
- Reviewed merged PR work through `main` at commit `dff53a5`.
- Implemented the first C# plugin vertical slice in `src/EwE_MCP_Plugin`.
- Implemented `IPlugin`, `IUIContextPlugin`, and `IDisposedPlugin` against the installed EwE plugin API.
- Captured live EwE runtime references: `cCore`, `cUIContext`, `cCommandHandler`, and `SynchronizationContext`.
- Implemented a local TCP socket server on `127.0.0.1:9877`.
- Implemented 4-byte big-endian length-prefixed UTF-8 JSON framing.
- Implemented token auth with local development fallback token `ewe-mcp-local-dev`.
- Implemented request/response envelopes with command ID, status, result, and structured error fields.
- Marshaled mutating/runtime commands onto EwE's synchronization context.
- Added in-memory command audit log.
- Added plugin startup file logging at `%APPDATA%\Ecopath with Ecosim\EwE_MCP_Plugin.log`.
- Implemented core plugin commands:
  - `ping`
  - `diagnose`
  - `get_ewe_info`
  - `get_runtime_state`
  - `get_message_log`
  - `get_command_catalog`
  - `list_ui_commands`
  - `invoke_ui_command`
  - `load_model`
  - `save_model`
  - `save_model_as`
  - `close_model`
  - `has_changes`
  - `discard_changes`
  - `get_model_info`
  - `get_core_counters`
  - `list_domains`
  - `list_entities`
  - `get_entity`
  - `list_variables`
  - `get_variable_metadata`
  - `get_values`
  - `validate_values`
  - `get_indexed_value`
  - `validate_indexed_value`
  - `list_ecopath_groups`
  - `get_ecopath_group`
  - `list_ecopath_fleets`
  - `get_ecopath_fleet`
  - `list_ecosim_scenarios`
  - `get_ecosim_scenario`
  - `load_ecosim_scenario`
  - `create_ecosim_scenario`
  - `copy_ecosim_scenario`
  - `delete_ecosim_scenario`
  - `save_ecosim_scenario`
  - `get_ecosim_run_state`
  - `list_ecosim_group_inputs`
  - `get_ecosim_group_input`
  - `list_ecosim_fleet_inputs`
  - `get_ecosim_fleet_input`
  - `get_ecosim_group_results`
  - `get_ecosim_fleet_results`
  - `export_ecosim_results`
  - `run_ecopath`
  - `run_ecosim`
  - `stop_ecosim`
  - `refresh_ui`
  - `list_navigation_nodes`
  - `navigate_to_node`
  - `get_active_view`
  - `capture_view`
  - `set_values`
  - `set_indexed_value`
  - `batch`
  - `batch_commands`
  - gated limited `execute_ewe_code`
- Implemented `refresh_ui` using `FormMain.Refresh` and a best-effort `~navigate` invoke.
- Implemented first-class UI navigation over EwE's navigation tree, including node listing, direct navigation by node id/text/path/query, and active view reporting.
- Implemented `set_values` for `ecopath_group`, `ecopath_fleet`, `ecosim_group_input`, and `ecosim_fleet_input`.
- Implemented Python-side automatic UI refresh after `set_values`, plus default value-tool navigation on `get_values`, `get_indexed_value`, `set_values`, and `set_indexed_value`; callers can pass `navigate=False` for quiet/background access.
- Implemented explicit test-cleanup utilities `has_changes` and `discard_changes`; live mutation tests restore values and call `discard_changes` to avoid EwE save prompts without saving over the shared sample model.
- Implemented the first explicit Ecosim control loop:
  - list/get scenarios
  - load a scenario
  - create/copy/delete/save scenarios
  - inspect run state
  - inspect group/fleet inputs
  - run/stop Ecosim using existing run tools
  - fetch group/fleet result time-series after a run
  - export Ecosim results to a host-side path
- Blocked destructive Ecosim scenario management from `batch_commands`.
- Implemented generic reflection-backed entity/value tools:
  - `get_entity`
  - `list_variables`
  - `get_variable_metadata`
  - `get_values`
  - `validate_values`
- Implemented indexed value tools for targeted cell-level control:
  - `get_indexed_value`
  - `validate_indexed_value`
  - `set_indexed_value`
- Hardened indexed value safety:
  - mutation tools are annotated with MCP `destructiveHint`
  - indexed getter failures return structured error envelopes
  - known group-backed indexed values reject out-of-range group/prey indices before EwE accessors can silently default or create sparse entries
- Verified indexed reads/writes/restores against `Tampa_Bay.EwEmdb`:
  - Ecopath group `DietComp(prey)` via group 20, prey 25
  - Ecopath fleet `Landings(group)` via fleet 1, group 18
- Expanded `list_domains`/`list_entities` beyond Ecopath to cover generic domains:
  - `ecopath_groups`
  - `ecopath_group_outputs`
  - `ecopath_fleets`
  - `ecopath_fleet_outputs`
  - `ecosim_scenarios`
  - `ecosim_group_inputs`
  - `ecosim_group_outputs`
  - `ecosim_fleet_inputs`
  - `ecosim_fleet_outputs`
  - `ecospace_scenarios`
  - `ecospace_group_inputs`
  - `ecospace_fleet_inputs`
  - `ecospace_group_outputs`
  - `ecospace_fleet_outputs`
  - `ecospace_habitats`
  - `ecospace_mpas`
  - `ecospace_region_outputs`
  - `ecotracer_scenarios`
  - `ecotracer_group_inputs`
  - `taxa`
  - `core`
  - `ui_commands`
- Generic `list_entities` now degrades per entity with structured errors when EwE reports a count but the collection object is unavailable until a subsystem/scenario is initialized.
- Implemented a limited gated `execute_ewe_code` reflection helper:
  - disabled unless `EWE_MCP_ENABLE_EVAL=1`
  - supports `Core.Property`
  - supports `Core.Property = Value`
  - supports `Core.Method()`
  - supports simple nested property/method paths
- Implemented Python package in `src/ewe_mcp`.
- Implemented Python socket client and FastMCP server skeleton.
- Added structured Python client error type `EwEMCPError` with plugin error code/message preservation.
- Added Phase 1 bridge hardening in PR #14:
  - configurable `EWE_MCP_ENABLED=0` disabled mode that skips socket binding
  - startup diagnostics with plugin assembly version, plugin path, EwE install path, and explicit bind failure logging
  - plugin/client command timeout plumbing
  - client-side socket invalidation on timeout or failed in-flight command, preventing late responses from poisoning the next command
  - socket timeout and reconnect regression coverage
- Exposed initial Python tools and resources, including `ewe://state` and `ewe://commands`.
- Added Python protocol/helper tests.
- Added edge-case tests for structured errors, batch blocking, unknown commands, invalid paths, and mutation validation.
- Added mutation tests for group/fleet name updates and eval-disabled behavior.
- Live tests are skipped by default and enabled with `EWE_LIVE_TESTS=1`.
- Added VM build workflow using `sshpass -e`, without committing credentials.
- Confirmed the Windows VM has EwE installed at:
  `C:\Program Files (x86)\Ecopath with Ecosim 40 years 6.7.0.18865_32-bit`
- Confirmed the plugin builds on the Windows VM against installed EwE binaries.
- Confirmed deployment to the installed EwE `Plugins` folder.
- Confirmed EwE loads the plugin and starts the socket when launched in the interactive Windows desktop session.
- Confirmed live smoke test through an SSH tunnel:
  - `ping`
  - `diagnose`
  - `get_ewe_info`
- Confirmed focused live Ecosim control test suite through an SSH tunnel:
  - `tests/test_ecosim_control.py` passed with 5 live tests
  - loads `Tampa_Bay.EwEmdb`
  - loads an Ecosim scenario
  - inspects group/fleet inputs
  - runs Ecosim asynchronously and waits for completion
  - reads short group/fleet result time-series windows
- Confirmed live Ecopath read-only inspection against `C:\Users\dexter\Eii.Ecopath\Database\Tampa_Bay.EwEmdb`:
  - counters: 52 groups, 51 living groups, 7 fleets, 1 detritus group
  - group summaries with biomass/PB/QB/EE input values
  - group detail with non-zero diet entries
  - fleet detail with non-zero landings/discards by group
- Confirmed live generic value inspection against `Tampa_Bay.EwEmdb`:
  - `list_domains` returns 22 domains
  - `list_variables` returns 77 variables for Ecopath group inputs
  - `get_values` reads scalar values and simple arrays, including `diet_comp`, fleet `landings`, and fleet `discards`
  - `validate_values` validates writable values and rejects unknown/read-only values without mutation
  - Ecosim and Ecospace scenario domains return live scenario entities
  - unavailable subsystem collections return per-entity `entity_unavailable` errors instead of failing full-domain discovery
- Added helper scripts:
  - `scripts/build-plugin-vm.sh`
  - `scripts/deploy-built-plugin-vm.sh`
  - `scripts/launch-ewe-vm.sh`
  - `scripts/vm-live-smoke.sh`
- Hardened VM helper scripts for repeated agent use from worktrees:
  - default remote source directory is derived from the local worktree name
  - SSH/SCP use explicit password authentication options with `sshpass -e`
  - credentials remain outside tracked files

### Verified Commands
```bash
uv run pytest
SSHPASS=... ./scripts/build-plugin-vm.sh
SSHPASS=... ./scripts/deploy-built-plugin-vm.sh
SSHPASS=... ./scripts/launch-ewe-vm.sh
SSHPASS=... ./scripts/vm-live-smoke.sh
```

Current local test expectation:

```text
11 passed, 32 skipped
```

The skipped tests are live EwE integration tests. Run them only when the VM plugin is deployed, EwE is launched, and a tunnel or direct connection is configured.

Current focused live indexed-value expectation:

```text
7 passed
```

Run it from the repo or active worktree after build/deploy/launch with:

```bash
EWE_MCP_HOST=127.0.0.1 \
EWE_MCP_PORT=19877 \
EWE_MCP_TOKEN=ewe-mcp-local-dev \
EWE_LIVE_TESTS=1 \
EWE_MODEL_PATH='C:\Users\dexter\Eii.Ecopath\Database\Tampa_Bay.EwEmdb' \
uv run pytest tests/test_indexed_values.py
```

Current focused live Ecosim-control expectation:

```text
5 passed
```

Run it from the repo or active worktree after build/deploy/launch with:

```bash
EWE_MCP_HOST=127.0.0.1 \
EWE_MCP_PORT=19877 \
EWE_MCP_TOKEN=ewe-mcp-local-dev \
EWE_LIVE_TESTS=1 \
EWE_MODEL_PATH='C:\Users\dexter\Eii.Ecopath\Database\Tampa_Bay.EwEmdb' \
uv run pytest tests/test_ecosim_control.py
```

### Important Discovery
Do not launch the EwE GUI directly from the SSH service session for live testing. EwE may start and then exit before the socket remains available. Use `scripts/launch-ewe-vm.sh`, which launches EwE through an interactive scheduled task in the logged-in Windows desktop session.

## Agent VM Setup Runbook

Use this section when a new agent needs a working EwE MCP environment with the Windows VM.

### 1. Confirm Clean Local State
Do not overwrite untracked or user-created files. Start with:

```bash
cd /Users/dexterfichuk/Projects/EwE-MCP
git status --short --branch
git fetch origin
git log --oneline --decorate --max-count=12
```

If local tracked files are modified, inspect before editing. If only untracked scratch files are present, leave them alone unless the user asks otherwise.

### 2. Local Python Environment
Run the default test suite first. These tests do not require live EwE by default.

```bash
uv sync
uv run pytest
```

Expected baseline at the time of this update:

```text
11 passed, 32 skipped
```

### 3. VM Environment Variables
Use the password only through the shell environment. Never write it into tracked files.

```bash
export SSHPASS='...'
export EWE_VM_HOST=192.168.64.2
export EWE_VM_USER=dexter
export EWE_VM_EWE_DIR='C:\Program Files (x86)\Ecopath with Ecosim 40 years 6.7.0.18865_32-bit'
export EWE_MCP_TOKEN=ewe-mcp-local-dev
```

Optional defaults:

```bash
export EWE_BUILD_CONFIGURATION=Release
export EWE_BUILD_PLATFORM=x86
export EWE_MCP_LOCAL_TUNNEL_PORT=19877
export EWE_MCP_REMOTE_PORT=9877
export EWE_MODEL_PATH='C:\Users\dexter\Eii.Ecopath\Database\Tampa_Bay.EwEmdb'
```

### 4. Build, Deploy, Launch
Build on the Windows VM against the installed EwE binaries, then deploy into the installed EwE `Plugins` folder.

```bash
./scripts/build-plugin-vm.sh
./scripts/deploy-built-plugin-vm.sh
./scripts/launch-ewe-vm.sh
```

Notes:

- `deploy-built-plugin-vm.sh` stops `ewe6.exe` by default to avoid Windows DLL locks.
- `launch-ewe-vm.sh` starts EwE via an interactive scheduled task. This is required for a stable GUI/plugin process.
- A healthy launch shows `TcpTestSucceeded True` for `127.0.0.1:9877`.

### 5. Smoke Test Through SSH Tunnel
Run the script:

```bash
./scripts/vm-live-smoke.sh
```

This opens a temporary local tunnel from `127.0.0.1:19877` to the VM plugin at `127.0.0.1:9877` and checks:

- `ping`
- `diagnose`
- `get_ewe_info`
- `list_domains`

### 6. Run Live Tests
Only run live tests after build/deploy/launch/smoke succeeds.

```bash
SSHPASS="$SSHPASS" sshpass -e ssh -f -N \
  -L 19877:127.0.0.1:9877 \
  -o StrictHostKeyChecking=no \
  dexter@192.168.64.2

EWE_MCP_HOST=127.0.0.1 \
EWE_MCP_PORT=19877 \
EWE_MCP_TOKEN=ewe-mcp-local-dev \
EWE_LIVE_TESTS=1 \
EWE_MODEL_PATH='C:\Users\dexter\Eii.Ecopath\Database\Tampa_Bay.EwEmdb' \
uv run pytest
```

After live tests, close the tunnel:

```bash
pkill -f 'ssh.*19877:127.0.0.1:9877' || true
```

### 7. Manual Live Probe
For a quick manual probe:

```bash
EWE_MCP_HOST=127.0.0.1 EWE_MCP_PORT=19877 uv run python - <<'PY'
from ewe_mcp.client import EwEMCPClient

client = EwEMCPClient()
assert client.connect()
print(client.command_result("ping"))
print(client.command_result("load_model", {"path": r"C:\Users\dexter\Eii.Ecopath\Database\Tampa_Bay.EwEmdb"}, timeout=60))
print(client.command_result("get_core_counters"))
print(client.command_result("get_ecopath_group", {"index": 20}))
client.disconnect()
PY
```

### 8. Troubleshooting Checklist
- If the plugin DLL cannot be copied, EwE is probably running. Run `./scripts/deploy-built-plugin-vm.sh`, which stops EwE by default.
- If the port is closed after launch, check `%APPDATA%\Ecopath with Ecosim\EwE_MCP_Plugin.log` on the VM.
- If EwE starts then exits, make sure it was launched with `./scripts/launch-ewe-vm.sh`, not direct SSH `Start-Process`.
- If Python cannot connect, confirm the SSH tunnel and `EWE_MCP_HOST/EWE_MCP_PORT`.
- If live mutation tests trigger EwE save prompts, restore the original values and call `discard_changes`; this clears dirty flags but does not physically revert values.

## Architecture

### C# EwE Plugin
- Location: `src/EwE_MCP_Plugin`
- Target: `.NET Framework 4.8`
- Default platform: `x86`
- Build target: installed EwE binaries first, with source-tree binary paths as fallback.
- Runtime role: in-process bridge with direct access to EwE core, UI context, command handler, and run state.
- Socket protocol:
  - Local TCP
  - 4-byte big-endian length prefix
  - UTF-8 JSON
  - max message size
  - request token
  - structured result/error envelope

### Python FastMCP Server
- Location: `src/ewe_mcp`
- Runtime role: MCP-facing server that converts tools/resources into plugin socket commands.
- Current resource pattern:
  - `ewe://state`
  - `ewe://commands`
- Intended resource pattern:
  - `ewe://model`
  - `ewe://entities/{domain}`
  - `ewe://variables/{domain}`
  - `ewe://results/{run_id}/{kind}`
  - `ewe://plots/{plot_id}`
  - `ewe://cache/{cache_id}`

### Security Baseline
- Bind to `127.0.0.1` by default.
- Require token authentication.
- Never commit VM credentials.
- Use `SSHPASS` from the local environment for VM workflows.
- Keep `execute_ewe_code` disabled unless explicitly enabled with `EWE_MCP_ENABLE_EVAL=1`.
- Keep destructive and eval tools gated and auditable.

## Remaining Work

### Phase 1: Harden The Bridge
- ~~Add a configurable plugin enabled/disabled flag.~~ Completed via `EWE_MCP_ENABLED=0` in PR #14. ([#15](https://github.com/dexterfichuk/EwE-MCP/issues/15))
- Move token/host/port configuration beyond environment variables if EwE’s runtime environment makes env vars awkward. ([#16](https://github.com/dexterfichuk/EwE-MCP/issues/16))
- ~~Add richer startup diagnostics.~~ Completed for plugin assembly version, EwE install path, plugin path, and port bind failures in PR #14. ([#17](https://github.com/dexterfichuk/EwE-MCP/issues/17))
- ~~Add socket idle/client cleanup tests.~~ Completed for disconnect/idempotent cleanup and timeout cleanup behavior in PR #14. ([#18](https://github.com/dexterfichuk/EwE-MCP/issues/18))
- ~~Add command timeout handling in the plugin and Python client.~~ Completed in PR #14, including client reconnect safety after timed-out requests. ([#19](https://github.com/dexterfichuk/EwE-MCP/issues/19))
- Add a `shutdown_server` or `restart_server` development-only command if useful. ([#20](https://github.com/dexterfichuk/EwE-MCP/issues/20))

### Phase 2: Model Inspection And Safe Model Workflow (COMPLETED)
- ~~Locate sample model files on the VM or in EwE install/source directories. Initial sample located and verified:~~
  ~~`C:\Users\dexter\Eii.Ecopath\Database\Tampa_Bay.EwEmdb`~~
- ~~Implement `list_open_model_metadata`.~~
- ~~Implement robust `load_model`, `save_model_as`, `close_model`, and reload smoke tests against a copy of a sample model.~~
- ~~Expand read-only inspection tools beyond the completed group/fleet/diet/landings slice:~~
  ~~stanzas, vulnerabilities, detritus fate, full diet matrix, remarks/statuses, and Ecopath balancing diagnostics.~~
- ~~Harden the safe mutation workflow:~~
  - ~~copy sample model~~
  - ~~edit harmless value through `set_values`~~
  - ~~validate~~
  - ~~save as new file~~
  - ~~close/reload~~

### Phase 3: Generic EwE Object And Variable Layer
- Initial generic reflection-backed entity/value reads are done for available domains.
- Initial domain-specific mutation is done for Ecopath groups/fleets and Ecosim group/fleet inputs via `set_values`; the remaining deeper generic data layer should build around EwE’s variable APIs. ([#21](https://github.com/dexterfichuk/EwE-MCP/issues/21)) Likely includes:
  - `cCoreInputOutputBase`
  - `eVarNameFlags`
  - `cVariableMetaData`
  - `GetVariable`
  - `SetVariable`
  - status and remark fields
  - entity IDs
- Remaining MCP/value work:
  - expose EwE metadata/status/remarks from `cCoreInputOutputBase` ([#22](https://github.com/dexterfichuk/EwE-MCP/issues/22))
  - initialize/load subsystem scenario data before reading unavailable Ecospace/Ecotracer collections ([#23](https://github.com/dexterfichuk/EwE-MCP/issues/23))
  - broaden edit-to-screen navigation mappings beyond the current Ecopath group/fleet and Ecosim group/fleet input mappings ([#24](https://github.com/dexterfichuk/EwE-MCP/issues/24))
  - add generic `set_values` support beyond Ecopath groups/fleets and Ecosim group/fleet inputs ([#25](https://github.com/dexterfichuk/EwE-MCP/issues/25))
  - extend indexed writes into multi-index variables and additional domains after the initial single-index Ecopath group/fleet slice ([#26](https://github.com/dexterfichuk/EwE-MCP/issues/26))
- Add broader batch safety rules for mixed read/write command groups; destructive scenario management is already blocked. ([#27](https://github.com/dexterfichuk/EwE-MCP/issues/27))

### Phase 4: Ecopath And Ecosim Control
- Implement explicit Ecopath tools. ([#28](https://github.com/dexterfichuk/EwE-MCP/issues/28))
  - run Ecopath
  - fetch balancing diagnostics
  - fetch group/fleet values
  - export key Ecopath tables
- Implement explicit Ecosim tools:
  - list scenarios: done
  - create/copy/delete scenario: done
  - load scenario: done
  - set scenario parameters: initial group/fleet input support done
  - run/stop Ecosim: done
  - subscribe to progress/completion events where available ([#29](https://github.com/dexterfichuk/EwE-MCP/issues/29))
  - fetch time-series outputs: initial group/fleet result series done
  - export results: initial reflected `dumpEcosimModelResults` wrapper done for installed-build compatibility

### Phase 5: Full EwE Suite Coverage
- Add Ecospace tools. ([#30](https://github.com/dexterfichuk/EwE-MCP/issues/30))
  - scenario management
  - spatial layer inspection
  - run/stop
  - progress
  - results and map exports
- Add Ecotracer tools. ([#31](https://github.com/dexterfichuk/EwE-MCP/issues/31))
  - tracer setup inspection
  - scenario/run control
  - result extraction
- Add MSE tools. ([#32](https://github.com/dexterfichuk/EwE-MCP/issues/32))
  - management procedure/scenario inspection
  - run control
  - result extraction
- Add Monte Carlo tools. ([#33](https://github.com/dexterfichuk/EwE-MCP/issues/33))
  - configuration inspection
  - run control
  - result summaries
- Add plugin manager/control diagnostics where safe. ([#34](https://github.com/dexterfichuk/EwE-MCP/issues/34))

### Phase 6: UI-Equivalent Ergonomics
- Navigation tools are implemented:
  - `list_navigation_nodes`
  - `navigate_to_node`
  - `get_active_view`
- Value tools navigate by default unless callers pass `navigate=False`:
  - `get_values`
  - `get_indexed_value`
  - `set_values`
  - `set_indexed_value`
- Test cleanup utilities are implemented:
  - `has_changes`
  - `discard_changes`
- Implemented view capture:
  - `capture_view` (returns inline image)
  - `save_screenshot` (saves to host)
  - `export_active_plot` (pending) ([#35](https://github.com/dexterfichuk/EwE-MCP/issues/35))
- Improve command catalog with command grouping, enabled state, checked state, and descriptions. ([#36](https://github.com/dexterfichuk/EwE-MCP/issues/36))
- Add prompt templates for common workflows. ([#37](https://github.com/dexterfichuk/EwE-MCP/issues/37))
  - balance a model
  - run Ecosim scenario
  - compare scenarios
  - export report-ready outputs

### Phase 7: Gated Power-User Escape Hatch
- A limited `execute_ewe_code` exists now. Remaining work is hardening, not initial implementation. ([#38](https://github.com/dexterfichuk/EwE-MCP/issues/38))
- Keep it disabled by default and require token auth plus `EWE_MCP_ENABLE_EVAL=1`.
- Keep it blocked from batches.
- Add stricter allow/deny rules before broadening beyond simple `Core.*` reflection.
- Audit every eval request and document risk clearly.

## Test Plan

### Current Passing Tests
- Python framing/protocol tests.
- Timeout protocol tests, including socket invalidation after timed-out in-flight commands.
- Python helper/config tests.
- Structured client error tests via `EwEMCPError`.
- Edge-case tests for batch blocking, unknown commands, invalid model paths, invalid domains, out-of-range indices, and unknown mutation properties.
- Mutation tests for Ecopath group/fleet names and disabled eval behavior.
- Indexed value tests for live read/write/restore plus unknown variable, empty indices, bad conversion, target index out of range, and indexed cell bounds.
- Live VM smoke test for plugin startup and basic commands.
- Live VM sample-model smoke test for Ecopath groups, diets, fleets, and counters.
- Full live VM suite at PR #14 merge point: `39 passed`.

### Next Tests To Add
- Plugin routing tests with fake handlers where possible. ([#39](https://github.com/dexterfichuk/EwE-MCP/issues/39))
  - malformed JSON
  - bad token
  - unknown command
  - oversized message
  - exception envelope
- VM lifecycle tests. ([#40](https://github.com/dexterfichuk/EwE-MCP/issues/40))
  - build
  - deploy
  - launch EwE
  - wait for socket
  - smoke commands
  - collect logs
- Model workflow tests. ([#41](https://github.com/dexterfichuk/EwE-MCP/issues/41))
  - load sample model
  - inspect groups/fleets
  - save a copy
  - close/reload
- Domain smoke tests. ([#42](https://github.com/dexterfichuk/EwE-MCP/issues/42))
  - Ecopath read-only and mutation workflow
  - Ecosim read-only and mutation workflow
  - Ecospace read-only workflow
  - Ecotracer read-only workflow
  - MSE read-only workflow
  - Monte Carlo read-only workflow
- Safety tests. ([#43](https://github.com/dexterfichuk/EwE-MCP/issues/43))
  - localhost binding
  - token required
  - eval disabled by default
  - destructive tools annotated
  - audit logs generated
  - mutation restore/rollback leaves sample model clean

## Operational Notes
- Main EwE source/reference tree: `/Users/dexterfichuk/Downloads/Eii.Ecopath-master`
- Live Windows VM: `dexter@192.168.64.2`
- Use `SSHPASS` for VM commands; do not write passwords to tracked files.
- Default local tunnel for agents: `127.0.0.1:19877 -> VM 127.0.0.1:9877`
- Installed EwE target on VM:
  `C:\Program Files (x86)\Ecopath with Ecosim 40 years 6.7.0.18865_32-bit`
- Installed plugin target on VM:
  `C:\Program Files (x86)\Ecopath with Ecosim 40 years 6.7.0.18865_32-bit\Plugins`
- Plugin startup log:
  `%APPDATA%\Ecopath with Ecosim\EwE_MCP_Plugin.log`
- EwE log:
  `%APPDATA%\Ecopath with Ecosim\EwE6_log.xml`
- Verified sample model:
  `C:\Users\dexter\Eii.Ecopath\Database\Tampa_Bay.EwEmdb`

## Packaging and Distribution

### C# Plugin (.dll)
- **Versioning Strategy:** The EwE core libraries update frequently. To maximize compatibility across minor EwE versions, the plugin references EwE DLLs with `<SpecificVersion>False</SpecificVersion>` and `<Private>False</Private>`. This ensures the plugin uses the user's installed EwE binaries at runtime. Breaking ABI changes will still require distributing version-specific builds (e.g., separate installers for EwE 6.6 vs 6.7).
- **Distribution:** Wrap the plugin in a standard Windows installer (e.g., **Inno Setup**) rather than requiring users to manually copy files into `Program Files (x86)`. The installer should request Administrator privileges, auto-detect or ask for the EwE `Plugins` directory, copy `EwE_MCP_Plugin.dll` and `Newtonsoft.Json.dll`, and provide a clean uninstaller. ([#44](https://github.com/dexterfichuk/EwE-MCP/issues/44))

### Python Server (ewe-mcp)
- The Python server is packaged via standard `pyproject.toml` tools (`hatchling`).
- Build the distribution wheel using `uv build`. ([#45](https://github.com/dexterfichuk/EwE-MCP/issues/45))
- Users install the wheel via `pip install ewe_mcp-*.whl` and run it via the `ewe-mcp` CLI command, ensuring they configure their connection environment variables (`EWE_MCP_TOKEN`, `EWE_MCP_HOST`, `EWE_MCP_PORT`) first. ([#45](https://github.com/dexterfichuk/EwE-MCP/issues/45))

## Rollback
The integration is optional and external. To disable it, close EwE and remove these files from the installed EwE `Plugins` folder:

- `EwE_MCP_Plugin.dll`
- `Newtonsoft.Json.dll`, if it is only present for EwE MCP

Removing the plugin does not modify the EwE installation or model databases.
