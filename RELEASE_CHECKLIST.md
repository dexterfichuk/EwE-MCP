# Release Checklist

## Local Checks

- [ ] `uv sync` succeeds with no dependency errors
- [ ] `uv run pytest` passes (54 passed, 80 skipped offline baseline)
- [ ] All Python files compile: `python -m py_compile src/ewe_mcp/server.py` (etc.)
- [ ] No secrets or credentials in `git diff`
- [ ] C# plugin builds on Windows VM: `./scripts/build-plugin-vm.sh`
- [ ] Plugin deploys: `./scripts/deploy-built-plugin-vm.sh`
- [ ] EwE launches: `./scripts/launch-ewe-vm.sh`
- [ ] Smoke test passes: `./scripts/vm-live-smoke.sh`

## VM Build/Deploy/Live Checks

- [ ] `EWE_LIVE_TESTS=1 uv run pytest` passes (expected: 83 passed, 5 skipped)
- [ ] Plugin log (`%APPDATA%\Ecopath with Ecosim\EwE_MCP_Plugin.log`) has no unexpected errors
- [ ] Dialog sweeper log is clean

## Sample Model Mutation Safety

- [ ] All mutation tests restore original values in `finally` blocks
- [ ] `discard_changes` called after test mutations
- [ ] No test writes to the shared `Tampa_Bay.EwEmdb` fixture

## Packaging Checks

- [ ] `uv build` produces a valid wheel
- [ ] Wheel installs into fresh venv and `ewe-mcp` CLI is on PATH
- [ ] Inno Setup installer builds without errors
- [ ] Installer deploys plugin DLLs correctly on VM
- [ ] Uninstaller removes only EwE-MCP files

## Documentation Checks

- [ ] AGENTS.md test baseline matches actual `uv run pytest` output
- [ ] README.md tool list matches `server.py` tool definitions
- [ ] EwE-MCP-Plan.md phase completion status is current
- [ ] All closed GitHub issues have closing comments referencing PRs/commits

