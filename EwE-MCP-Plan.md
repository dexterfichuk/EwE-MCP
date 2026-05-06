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
  - `get_model_info`
  - `get_core_counters`
  - `run_ecopath`
  - `run_ecosim`
  - `stop_ecosim`
  - `batch`
  - `batch_commands`
  - gated placeholder `execute_ewe_code`
- Implemented Python package in `src/ewe_mcp`.
- Implemented Python socket client and FastMCP server skeleton.
- Exposed initial Python tools and resources, including `ewe://state` and `ewe://commands`.
- Added Python protocol/helper tests.
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
- Added helper scripts:
  - `scripts/build-plugin-vm.sh`
  - `scripts/deploy-built-plugin-vm.sh`
  - `scripts/launch-ewe-vm.sh`
  - `scripts/vm-live-smoke.sh`

### Verified Commands
```bash
uv run pytest
SSHPASS=... ./scripts/build-plugin-vm.sh
SSHPASS=... ./scripts/deploy-built-plugin-vm.sh
SSHPASS=... ./scripts/launch-ewe-vm.sh
SSHPASS=... ./scripts/vm-live-smoke.sh
```

### Important Discovery
Do not launch the EwE GUI directly from the SSH service session for live testing. EwE may start and then exit before the socket remains available. Use `scripts/launch-ewe-vm.sh`, which launches EwE through an interactive scheduled task in the logged-in Windows desktop session.

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
- Add a configurable plugin enabled/disabled flag.
- Move token/host/port configuration beyond environment variables if EwE’s runtime environment makes env vars awkward.
- Add richer startup diagnostics:
  - plugin assembly version
  - EwE install path
  - plugin path
  - port bind failures
  - dependency versions
- Add socket idle/client cleanup tests.
- Add command timeout handling in the plugin and Python client.
- Add a `shutdown_server` or `restart_server` development-only command if useful.

### Phase 2: Model Inspection And Safe Model Workflow
- Locate sample model files on the VM or in EwE install/source directories.
- Implement `list_open_model_metadata`.
- Implement robust `load_model`, `save_model_as`, `close_model`, and reload smoke tests against a copy of a sample model.
- Add read-only inspection tools for groups, fleets, stanzas, vulnerabilities, diets, landings, and basic Ecopath inputs.
- Add safe mutation workflow:
  - copy sample model
  - edit harmless value
  - validate
  - save as new file
  - close/reload

### Phase 3: Generic EwE Object And Variable Layer
- Build a generic data layer around EwE’s variable APIs, likely including:
  - `cCoreInputOutputBase`
  - `eVarNameFlags`
  - `cVariableMetaData`
  - `GetVariable`
  - `SetVariable`
  - status and remark fields
  - entity IDs
- Implement MCP tools:
  - `list_domains`
  - `list_entities`
  - `get_entity`
  - `list_variables`
  - `get_variable_metadata`
  - `get_values`
  - `validate_values`
  - `set_values`
- Add batch safety rules for mixed read/write command groups.

### Phase 4: Ecopath And Ecosim Control
- Implement explicit Ecopath tools:
  - run Ecopath
  - fetch balancing diagnostics
  - fetch group/fleet values
  - export key Ecopath tables
- Implement explicit Ecosim tools:
  - list scenarios
  - create/copy/delete scenario
  - load scenario
  - set scenario parameters
  - run/stop Ecosim
  - subscribe to progress/completion events where available
  - fetch time-series outputs
  - export results

### Phase 5: Full EwE Suite Coverage
- Add Ecospace tools:
  - scenario management
  - spatial layer inspection
  - run/stop
  - progress
  - results and map exports
- Add Ecotracer tools:
  - tracer setup inspection
  - scenario/run control
  - result extraction
- Add MSE tools:
  - management procedure/scenario inspection
  - run control
  - result extraction
- Add Monte Carlo tools:
  - configuration inspection
  - run control
  - result summaries
- Add plugin manager/control diagnostics where safe.

### Phase 6: UI-Equivalent Ergonomics
- Implement navigation tools:
  - `list_navigation_nodes`
  - `navigate_to_node`
  - `get_active_view`
- Implement view capture:
  - `capture_view`
  - `export_active_plot`
  - plot/image MCP content
- Improve command catalog with command grouping, enabled state, checked state, and descriptions.
- Add prompt templates for common workflows:
  - balance a model
  - run Ecosim scenario
  - compare scenarios
  - export report-ready outputs

### Phase 7: Gated Power-User Escape Hatch
- Implement `execute_ewe_code` only after named tools and the generic value layer are stable.
- Keep it disabled by default.
- Require token auth and `EWE_MCP_ENABLE_EVAL=1`.
- Audit every eval request.
- Prevent eval from being allowed in batches.
- Document risk clearly.

## Test Plan

### Current Passing Tests
- Python framing/protocol tests.
- Python helper/config tests.
- Live VM smoke test for plugin startup and basic commands.

### Next Tests To Add
- Plugin routing tests with fake handlers where possible:
  - malformed JSON
  - bad token
  - unknown command
  - oversized message
  - exception envelope
- VM lifecycle tests:
  - build
  - deploy
  - launch EwE
  - wait for socket
  - smoke commands
  - collect logs
- Model workflow tests:
  - load sample model
  - inspect groups/fleets
  - save a copy
  - close/reload
- Domain smoke tests:
  - Ecopath read-only and mutation workflow
  - Ecosim read-only and mutation workflow
  - Ecospace read-only workflow
  - Ecotracer read-only workflow
  - MSE read-only workflow
  - Monte Carlo read-only workflow
- Safety tests:
  - localhost binding
  - token required
  - eval disabled by default
  - destructive tools annotated
  - audit logs generated

## Operational Notes
- Main EwE source/reference tree: `/Users/dexterfichuk/Downloads/Eii.Ecopath-master`
- Live Windows VM: `dexter@192.168.64.2`
- Use `SSHPASS` for VM commands; do not write passwords to tracked files.
- Installed EwE target on VM:
  `C:\Program Files (x86)\Ecopath with Ecosim 40 years 6.7.0.18865_32-bit`
- Installed plugin target on VM:
  `C:\Program Files (x86)\Ecopath with Ecosim 40 years 6.7.0.18865_32-bit\Plugins`
- Plugin startup log:
  `%APPDATA%\Ecopath with Ecosim\EwE_MCP_Plugin.log`
- EwE log:
  `%APPDATA%\Ecopath with Ecosim\EwE6_log.xml`

## Rollback
The integration is optional and external. To disable it, close EwE and remove these files from the installed EwE `Plugins` folder:

- `EwE_MCP_Plugin.dll`
- `Newtonsoft.Json.dll`, if it is only present for EwE MCP

Removing the plugin does not modify the EwE installation or model databases.
