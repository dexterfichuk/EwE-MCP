import os

import pytest
from ewe_mcp.client import EwEMCPError
from ewe_mcp.server import (
    set_values,
    batch_commands,
    load_model,
    _send,
)
from ewe_mcp.helpers import BATCH_BLOCKED_COMMANDS

# Tests that require a running EwE plugin on the Windows VM.
live = pytest.mark.skipif(
    os.environ.get("EWE_LIVE_TESTS") != "1",
    reason="Requires live EwE MCP plugin (set EWE_LIVE_TESTS=1 to run)",
)


async def _ensure_model_loaded(sample_model_path):
    """Load the sample model if not already loaded (idempotent)."""
    res = await load_model(sample_model_path)
    assert res["loaded"] is True, f"Failed to load sample model: {sample_model_path}"


@live
@pytest.mark.asyncio
async def test_set_values_invalid_domain(sample_model_path):
    with pytest.raises(EwEMCPError) as excinfo:
        await set_values(domain="invalid_domain", index=1, values={"name": "test"})
    assert excinfo.value.code == "unknown_domain"


@live
@pytest.mark.asyncio
async def test_set_values_index_out_of_range(sample_model_path):
    await _ensure_model_loaded(sample_model_path)
    with pytest.raises(EwEMCPError) as excinfo:
        await set_values(domain="ecopath_group", index=9999, values={"name": "test"})
    assert excinfo.value.code == "index_out_of_range"


@live
@pytest.mark.asyncio
async def test_set_values_unknown_property(sample_model_path):
    await _ensure_model_loaded(sample_model_path)
    res = await set_values(
        domain="ecopath_group", index=1, values={"non_existent_prop": 1.0}
    )
    assert res["results"]["non_existent_prop"] == "unknown_or_readonly_property"


@live
@pytest.mark.asyncio
async def test_batch_commands_success(sample_model_path):
    await _ensure_model_loaded(sample_model_path)
    cmds = [
        {"type": "get_model_info", "params": {}},
        {"type": "ping", "params": {}},
    ]
    res = await batch_commands(cmds)
    assert "results" in res
    assert len(res["results"]) == 2
    assert res["results"][0]["loaded"] is True
    assert res["results"][1]["pong"] is True


@pytest.mark.asyncio
async def test_batch_commands_blocked():
    """This test is purely local — it raises before any socket connection."""
    cmds = [{"type": "close_model", "params": {}}]
    with pytest.raises(ValueError, match="is not allowed in batch"):
        await batch_commands(cmds)


@live
@pytest.mark.asyncio
async def test_unknown_command(sample_model_path):
    with pytest.raises(EwEMCPError) as excinfo:
        await _send("non_existent_command")
    assert excinfo.value.code == "unknown_command"


@live
@pytest.mark.asyncio
async def test_load_model_invalid_path(sample_model_path):
    res = await load_model(r"C:\non_existent_file.EwEmdb")
    assert res["loaded"] is False


@pytest.mark.asyncio
async def test_new_batch_blocked_commands():
    assert "load_model" in BATCH_BLOCKED_COMMANDS
    assert "save_model" in BATCH_BLOCKED_COMMANDS
    assert "save_model_as" in BATCH_BLOCKED_COMMANDS
    assert "run_ecopath" in BATCH_BLOCKED_COMMANDS
    assert "run_ecosim" in BATCH_BLOCKED_COMMANDS
    assert "stop_ecosim" in BATCH_BLOCKED_COMMANDS
    assert "load_ecosim_scenario" in BATCH_BLOCKED_COMMANDS
    assert "load_ecospace_scenario" in BATCH_BLOCKED_COMMANDS
    assert "load_ecotracer_scenario" in BATCH_BLOCKED_COMMANDS
    assert "create_ecospace_scenario" in BATCH_BLOCKED_COMMANDS
    assert "copy_ecospace_scenario" in BATCH_BLOCKED_COMMANDS
    assert "delete_ecospace_scenario" in BATCH_BLOCKED_COMMANDS
    assert "save_ecospace_scenario" in BATCH_BLOCKED_COMMANDS
    assert "run_ecospace" in BATCH_BLOCKED_COMMANDS
    assert "stop_ecospace" in BATCH_BLOCKED_COMMANDS
    assert "create_ecotracer_scenario" in BATCH_BLOCKED_COMMANDS
    assert "copy_ecotracer_scenario" in BATCH_BLOCKED_COMMANDS
    assert "delete_ecotracer_scenario" in BATCH_BLOCKED_COMMANDS
    assert "save_ecotracer_scenario" in BATCH_BLOCKED_COMMANDS
    assert "close_ecotracer_scenario" in BATCH_BLOCKED_COMMANDS
    assert "run_mse" in BATCH_BLOCKED_COMMANDS
    assert "stop_mse" in BATCH_BLOCKED_COMMANDS
    assert "run_monte_carlo" in BATCH_BLOCKED_COMMANDS
    assert "stop_monte_carlo" in BATCH_BLOCKED_COMMANDS


@pytest.mark.asyncio
async def test_batch_commands_command_type_alias():
    """Verify that 'command_type' is accepted as an alias for 'type' and normalized before sending."""
    from unittest.mock import patch
    from ewe_mcp.server import batch_commands

    cmds = [{"command_type": "ping", "params": {}}]
    with patch("ewe_mcp.server._send", return_value={"results": [{"pong": True}]}) as mock_send:
        result = await batch_commands(cmds)
        # Verify _send was called with the normalized command
        mock_send.assert_called_once()
        args, kwargs = mock_send.call_args
        assert args[0] == "batch"
        assert args[1]["commands"][0]["type"] == "ping"
        assert args[1]["commands"][0].get("command_type") == "ping"
    # Also verify that command_type in blocked list raises correctly
    blocked_cmds = [{"command_type": "close_model", "params": {}}]
    with pytest.raises(ValueError, match="is not allowed in batch"):
        await batch_commands(blocked_cmds)
    print("batch command_type alias test PASSED")
