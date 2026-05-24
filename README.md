# EwE MCP

MCP bridge for a live Ecopath with Ecosim (EwE) desktop process. Lets AI agents inspect, run, and mutate EwE models through a structured tool interface.

## Architecture

```
Agent (Claude / Codex / Gemini)  ---MCP---►  Python FastMCP server  ---socket---►  EwE .NET plugin (in-process)
```

- `src/EwE_MCP_Plugin` — .NET Framework 4.8 EwE plugin DLL exposing an authenticated length-prefixed JSON socket on `127.0.0.1:9877`.
- `src/ewe_mcp` — Python `FastMCP` server translating that socket protocol into MCP tools, resources, and prompts.

## Quick Start (Local Dev)

```bash
# Install
uv sync

# Configure
cp .env.example .env   # edit token/paths

# Launch EwE on VM, open tunnel, then:
EWE_MCP_HOST=127.0.0.1 EWE_MCP_PORT=19877 EWE_MCP_TOKEN=ewe-mcp-local-dev uv run ewe-mcp
```

For the full VM workflow (build, deploy, launch, tunnel, smoke test), see [AGENTS.md](AGENTS.md).

## Installation

```bash
pip install ewe-mcp
# or from source:
uv build && pip install dist/ewe_mcp-0.1.0-py3-none-any.whl
```

Set required environment variables before running:

```bash
export EWE_MCP_HOST=127.0.0.1
export EWE_MCP_PORT=19877
export EWE_MCP_TOKEN=ewe-mcp-local-dev
```

Then start the server:

```bash
ewe-mcp
```

## Agent Configuration

### Claude Desktop

Add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "ewe": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/EwE-MCP", "ewe-mcp"],
      "env": {
        "EWE_MCP_HOST": "127.0.0.1",
        "EWE_MCP_PORT": "19877",
        "EWE_MCP_TOKEN": "ewe-mcp-local-dev"
      }
    }
  }
}
```

Or if installed via pip:

```json
{
  "mcpServers": {
    "ewe": {
      "command": "ewe-mcp",
      "env": {
        "EWE_MCP_HOST": "127.0.0.1",
        "EWE_MCP_PORT": "19877",
        "EWE_MCP_TOKEN": "ewe-mcp-local-dev"
      }
    }
  }
}
```

### Codex / Gemini CLI

Add a custom MCP server pointing at the `ewe-mcp` entry point with the same environment variables. See your platform's MCP integration docs for the exact config format.

### Environment Variables

| Variable | Default | Description |
|---|---|---|
| `EWE_MCP_HOST` | `127.0.0.1` | Plugin socket host |
| `EWE_MCP_PORT` | `9877` | Plugin socket port |
| `EWE_MCP_TOKEN` | `ewe-mcp-local-dev` | Authentication token |
| `EWE_MCP_ENABLE_EVAL` | — | Gate `execute_ewe_code` and `shutdown_server` (`1` to enable) |
| `EWE_MCP_MAX_RESPONSE_SIZE` | `10485760` (10 MB) | Client-side response size cap |
| `EWE_MCP_FILTER_SENTINELS` | `1` | Filter `-9999` sentinel values from JSON responses (`0` to disable) |

Plugin-side config is read from `%APPDATA%\Ecopath with Ecosim\EwE_MCP_Plugin.cfg` (INI with `[EwE_MCP]` header). Env vars override config file values.

## MCP Tools

### Diagnostics
`ping`, `diagnose`, `get_ewe_info`, `get_runtime_state`, `get_message_log`, `get_core_counters`, `get_installed_plugins`

### Model Lifecycle
`load_model`, `save_model`, `save_model_as`, `close_model`, `get_model_info`, `has_changes`, `discard_changes`

### Ecopath
`run_ecopath`, `get_ecopath_run_state`, `get_ecopath_stats`, `get_ecopath_group_diagnostics`, `get_ecopath_fleet_diagnostics`, `export_ecopath_results`, `list_ecopath_groups`, `get_ecopath_group`, `list_ecopath_fleets`, `get_ecopath_fleet`

### Ecosim
`list_ecosim_scenarios`, `get_ecosim_scenario`, `load_ecosim_scenario`, `create_ecosim_scenario`, `copy_ecosim_scenario`, `delete_ecosim_scenario`, `save_ecosim_scenario`, `get_ecosim_run_state`, `wait_for_ecosim_completion`, `run_ecosim`, `stop_ecosim`, `list_ecosim_group_inputs`, `get_ecosim_group_input`, `list_ecosim_fleet_inputs`, `get_ecosim_fleet_input`, `get_ecosim_group_results`, `get_ecosim_fleet_results`, `export_ecosim_results`

### Ecospace
`list_ecospace_scenarios`, `get_ecospace_scenario`, `load_ecospace_scenario`, `get_ecospace_run_state`, `run_ecospace`, `stop_ecospace`, `wait_for_ecospace_completion`, `list_ecospace_group_inputs`, `get_ecospace_group_input`, `list_ecospace_fleet_inputs`, `get_ecospace_fleet_input`, `get_ecospace_group_results`, `get_ecospace_fleet_results`, `list_ecospace_habitats`, `get_ecospace_habitat`, `list_ecospace_mpas`, `get_ecospace_mpa`, `get_ecospace_basemap`, `get_ecospace_model_parameters`, `create_ecospace_scenario`, `copy_ecospace_scenario`, `delete_ecospace_scenario`, `save_ecospace_scenario`

### Ecotracer
`list_ecotracer_scenarios`, `get_ecotracer_scenario`, `load_ecotracer_scenario`, `get_ecotracer_run_state`, `get_ecotracer_model_parameters`, `list_ecotracer_group_inputs`, `get_ecotracer_group_input`, `get_ecotracer_group_results`, `create_ecotracer_scenario`, `copy_ecotracer_scenario`, `delete_ecotracer_scenario`, `save_ecotracer_scenario`, `close_ecotracer_scenario`

_No standalone `run_ecotracer` — Ecotracer runs within Ecosim/Ecospace when ContaminantTracing is enabled._

### MSE
`get_mse_parameters`, `list_mse_group_inputs`, `get_mse_group_input`, `list_mse_fleet_inputs`, `get_mse_fleet_input`, `get_mse_run_state`, `run_mse`, `stop_mse`, `get_mse_group_results`, `get_mse_fleet_results`

### Monte Carlo
`get_monte_carlo_parameters`, `list_monte_carlo_groups`, `get_monte_carlo_group`, `get_monte_carlo_run_state`, `run_monte_carlo`, `stop_monte_carlo`

### Generic Entity / Value
`list_domains`, `list_entities`, `get_entity`, `list_variables`, `get_variable_metadata`, `get_values`, `validate_values`, `get_indexed_value`, `validate_indexed_value`, `set_values`, `set_indexed_value`, `search_entities`, `get_values_range`, `set_values_range`

### Name-Based Batch Updates
`list_name_batch_capabilities`, `set_values_by_name_batch`

### Navigation / UI
`list_navigation_nodes`, `navigate_to_node`, `get_active_view`, `invoke_ui_command`, `list_ui_commands`, `refresh_ui`

### View Capture
`capture_view`, `save_screenshot`, `export_active_plot`

### Standalone Ecological Tools (No EwE Connection Required)
`query_fishbase`, `query_sealifebase`, `query_globi`

### Ecopath Estimation Helpers (No EwE Connection Required)
`estimate_fishing_mortality`, `estimate_total_mortality`, `estimate_catch`, `estimate_production`, `estimate_consumption`, `estimate_average_longevity`

### Server Control
`shutdown_server` (gated behind `EWE_MCP_ENABLE_EVAL=1`)

### Batching
`batch_commands`

### Eval (Disabled by Default)
`execute_ewe_code` (gated behind `EWE_MCP_ENABLE_EVAL=1`)

## MCP Resources

| URI | Content |
|---|---|
| `ewe://state` | Current EwE runtime state |
| `ewe://commands` | Available EwE UI commands |
| `ewe://entities/ecopath_groups` | Ecopath group listing |
| `ewe://entities/ecopath_fleets` | Ecopath fleet listing |

## MCP Prompt Templates

- `balance_model` — Load model, run Ecopath, report diagnostics.
- `run_ecosim_scenario` — Load model, run Ecosim scenario, fetch results.
- `compare_scenarios` — Run and compare multiple Ecosim scenarios.
- `export_report` — Export Ecopath/Ecosim results for reporting.

## UI Navigation Convention

Value tools navigate EwE's GUI by default so a human can see what the agent is doing:

- `get_values`, `get_indexed_value`, `set_values`, `set_indexed_value`

Pass `navigate=False` for quiet/background access, batch operations, or any workflow where UI flicker would be distracting.

```python
await navigate_to_node(id="ndDietComposition")
await get_indexed_value(domain="ecopath_group", index=20, variable="diet_comp", indices=[25])
await set_values(domain="ecopath_group", index=1, values={"name": "0-12 Snook"}, navigate=False)
```

### Name-Based Batch Updates

`set_values_by_name_batch` updates entities by exact name instead of index, across mixed domains in one request. Use `list_name_batch_capabilities` first to discover supported domains, match fields, and writable fields.

Name resolution is exact-match only. If no exact match is found, the tool returns ranked fuzzy suggestions so the agent can retry with the corrected name — suggestions are never auto-applied.

Recommended agent workflow:
1. Call `list_name_batch_capabilities` to discover available fields and domains.
2. Build operations using the discovered writable fields.
3. Call `set_values_by_name_batch` with the operations.
4. If the response contains `not_found` or `ambiguous` items, retry using the `suggestions` or `candidates` from the result.

Example:
```python
await set_values_by_name_batch(
    operations=[
        {"domain": "ecopath_group", "name": "Adult menhaden", "values": {"pb": 1.42}},
        {"domain": "ecopath_fleet", "name": "Shrimp Trawl", "values": {"landing": 0.95}},
    ],
    options={"dry_run": False, "include_suggestions": True},
)
```

## Safety Rules

- `execute_ewe_code` is disabled by default (requires `EWE_MCP_ENABLE_EVAL=1`). Deny patterns block `System.IO`, `System.Diagnostics.Process`, `System.Net`, file/process operations.
- `shutdown_server` is gated behind the same eval flag. Both are blocked from `batch_commands`.
- Never batch model lifecycle, simulation run/stop, export/capture, eval, shutdown, `set_values`, `set_indexed_value`, or scenario load/mutation commands.
- Use `validate_values` before `set_values`. Use `discard_changes` only for test cleanup — it clears dirty flags but doesn't revert values.
- Prefer `save_model_as` to temp copies for mutations rather than overwriting shared models.

## Tests

```bash
uv run pytest                    # unit/offline tests
EWE_LIVE_TESTS=1 uv run pytest   # live tests (requires running EwE + tunnel)
```

## License

GPL-3.0
