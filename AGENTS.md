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

Never commit credentials. Put the VM password in `SSHPASS` only.

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
export SSHPASS='...'
export EWE_VM_HOST=192.168.64.2
export EWE_VM_USER=dexter
export EWE_VM_EWE_DIR='C:\Program Files (x86)\Ecopath with Ecosim 40 years 6.7.0.18865_32-bit'
export EWE_BUILD_CONFIGURATION=Release
export EWE_BUILD_PLATFORM=x86
export EWE_MCP_TOKEN=ewe-mcp-local-dev
export EWE_MCP_LOCAL_TUNNEL_PORT=19877
export EWE_MCP_REMOTE_PORT=9877
export EWE_MODEL_PATH='C:\Users\dexter\Eii.Ecopath\Database\Tampa_Bay.EwEmdb'
```

If you are working from a separate worktree whose directory name is not `EwE-MCP`, either use scripts that derive the remote root from the worktree name or set:

```bash
export EWE_VM_REMOTE_ROOT="C:/Users/dexter/$(basename "$PWD")"
```

## Build, Deploy, Launch
Build the plugin on the Windows VM, deploy it into EwE's plugin folder, and launch EwE through the interactive desktop session:

```bash
./scripts/build-plugin-vm.sh
./scripts/deploy-built-plugin-vm.sh
./scripts/launch-ewe-vm.sh
```

Important details:
- `deploy-built-plugin-vm.sh` stops `ewe6.exe` by default so Windows releases the plugin DLL.
- Do not launch EwE directly with SSH `Start-Process`; it can start in the SSH service session and exit before the socket stays available.
- A good launch reports `TcpTestSucceeded True` for `127.0.0.1:9877`.
- Plugin logs are on the VM at `%APPDATA%\Ecopath with Ecosim\EwE_MCP_Plugin.log`.

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
print(client.command_result("load_model", {"path": r"C:\Users\dexter\Eii.Ecopath\Database\Tampa_Bay.EwEmdb"}, timeout=60))
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

The MCP server exposes tools backed by the running EwE plugin. Current main-branch tools include diagnostics, model load/save/close, change-state utilities (`has_changes`, `discard_changes`), Ecopath group/fleet reads, generic entity/value reads, indexed value reads/writes such as `DietComp(prey)` and `Landings(group)`, Ecosim scenario load/create/copy/delete/save, Ecosim run state, Ecosim group/fleet inputs, Ecosim group/fleet result time-series, selected mutations through `set_values` and `set_indexed_value`, UI command invocation, navigation through `list_navigation_nodes`/`navigate_to_node`/`get_active_view`, batching, and a disabled-by-default eval helper.

## Live Tests
Run live tests only after build/deploy/launch/smoke succeeds:

```bash
EWE_MCP_HOST=127.0.0.1 \
EWE_MCP_PORT=19877 \
EWE_MCP_TOKEN=ewe-mcp-local-dev \
EWE_LIVE_TESTS=1 \
EWE_MODEL_PATH='C:\Users\dexter\Eii.Ecopath\Database\Tampa_Bay.EwEmdb' \
uv run pytest
```

Expected local baseline with live tests skipped is `6 passed, 20 skipped`. The focused indexed live suite is:

```bash
EWE_MCP_HOST=127.0.0.1 \
EWE_MCP_PORT=19877 \
EWE_MCP_TOKEN=ewe-mcp-local-dev \
EWE_LIVE_TESTS=1 \
EWE_MODEL_PATH='C:\Users\dexter\Eii.Ecopath\Database\Tampa_Bay.EwEmdb' \
uv run pytest tests/test_indexed_values.py
```

Expected focused indexed live result is `7 passed`.

The focused Ecosim control suite is:

```bash
EWE_MCP_HOST=127.0.0.1 \
EWE_MCP_PORT=19877 \
EWE_MCP_TOKEN=ewe-mcp-local-dev \
EWE_LIVE_TESTS=1 \
EWE_MODEL_PATH='C:\Users\dexter\Eii.Ecopath\Database\Tampa_Bay.EwEmdb' \
uv run pytest tests/test_ecosim_control.py
```

Expected focused Ecosim live result is `5 passed`. The suite loads the sample model, loads an Ecosim scenario, inspects group/fleet inputs, runs Ecosim asynchronously, waits for completion, and reads short result time-series windows.

Prefer testing mutations against copied models. If you mutate the shared sample model, restore the original value in a `finally` block and call `discard_changes` so EwE clears the unsaved-change flag without saving over the sample model.

## Safety Rules
- Keep `execute_ewe_code` disabled unless the user explicitly asks for it and understands the risk.
- Do not batch `execute_ewe_code`, model-closing operations, or destructive Ecosim scenario management.
- Use `validate_values` before `set_values` when possible.
- Value reads/writes navigate by default through `get_values`, `get_indexed_value`, `set_values`, and `set_indexed_value`; pass `navigate=False` for quiet/background access or batch-like workflows.
- Use `discard_changes` only as an explicit cleanup step after test mutations have been restored; it clears EwE's dirty flag but does not physically revert values.
- For model-changing workflows, prefer `save_model_as` to a temporary/copy path instead of overwriting the shared model.
- Record what you changed and what you restored in your final message.

## Troubleshooting
- DLL copy fails: EwE is probably still running; rerun `./scripts/deploy-built-plugin-vm.sh`.
- Port is closed after launch: inspect `%APPDATA%\Ecopath with Ecosim\EwE_MCP_Plugin.log` on the VM.
- Python cannot connect: confirm the tunnel, `EWE_MCP_HOST`, `EWE_MCP_PORT`, and token.
- EwE exits after launch: use `./scripts/launch-ewe-vm.sh`, not direct SSH GUI launch.
- SSH password auth flakes: retry with explicit options: `-o PreferredAuthentications=password -o PubkeyAuthentication=no`.
