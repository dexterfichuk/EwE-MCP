# EwE MCP Project Instructions

This project is an MCP bridge for Ecopath with Ecosim (EwE). It consists of a C# plugin for EwE and a Python FastMCP server.

## Core Mandates & Safety Rules

- **Shared State:** Treat the live EwE process and models as shared state. Inspect before mutating.
- **Mutations:** Prefer testing mutations against copied models (`save_model_as`).
- **Cleanup:** If you mutate a shared model (like `Tampa_Bay.EwEmdb`), always restore original values in a `finally` block and call `discard_changes` to clear dirty flags.
- **Eval Gating:** `execute_ewe_code` and `shutdown_server` are disabled by default. Do not enable unless explicitly requested and risks are understood. Requires `EWE_MCP_ENABLE_EVAL=1`.
- **Batching:** Never batch model lifecycle, simulation run/stop, export/capture, or scenario load/mutation commands. `execute_ewe_code` and `shutdown_server` are also blocked from batching.
- **Range Validation:** Use `validate_values` before `set_values`. Respect EwE's range constraints (e.g., `den_dep_catchability` >= 1.0).
- **Navigation:** Value tools (`get_values`, `set_values`, etc.) navigate the EwE GUI by default. Use `navigate=False` for quiet/background operations.
- **Data vs. Visuals:** Prefer data-reading tools for tabular data. Use `capture_view`/`save_screenshot` only for graphs, charts, and spatial visualizations. Use `export_active_plot` for structured chart data.

## Environment & Configuration

- **VM Host:** `dexter@192.168.64.2`
- **Default Port:** `9877` (plugin) / `19877` (macOS local tunnel)
- **Token:** `ewe-mcp-local-dev` (default)
- **Plugin Config:** `%APPDATA%\Ecopath with Ecosim\EwE_MCP_Plugin.cfg` (requires `[EwE_MCP]` header)

## Workflow

1. **Verify Environment:** Run `./scripts/vm-live-smoke.sh` to check connectivity.
2. **Build & Deploy:** Use `./scripts/build-plugin-vm.sh` and `./scripts/deploy-built-plugin-vm.sh`.
3. **Launch:** Use `./scripts/launch-ewe-vm.sh`.
4. **Test:**
   - Offline: `uv run pytest`
   - Live: `EWE_LIVE_TESTS=1 uv run pytest`

## Style & Conventions

- Follow Python type hinting and async/await patterns.
- Keep `helpers.py` for shared logic and constants.
- Maintain consistency between Python constants (`EVAL_DENY_PATTERNS`, `BATCH_BLOCKED_COMMANDS`) and C# implementations.
