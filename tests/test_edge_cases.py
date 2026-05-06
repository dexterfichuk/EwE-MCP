import os

import pytest
from ewe_mcp.client import EwEMCPError
from ewe_mcp.server import (
    set_values,
    get_ecopath_group,
    batch_commands,
    load_model,
    get_model_info,
    _send,
)

# Tests that require a running EwE plugin on the Windows VM.
live = pytest.mark.skipif(
    os.environ.get("EWE_LIVE_TESTS") != "1",
    reason="Requires live EwE MCP plugin (set EWE_LIVE_TESTS=1 to run)",
)


@live
@pytest.mark.asyncio
async def test_set_values_invalid_domain():
    with pytest.raises(EwEMCPError) as excinfo:
        await set_values(domain="invalid_domain", index=1, values={"name": "test"})
    assert excinfo.value.code == "unsupported_domain"


@live
@pytest.mark.asyncio
async def test_set_values_index_out_of_range():
    with pytest.raises(EwEMCPError) as excinfo:
        await set_values(domain="ecopath_group", index=9999, values={"name": "test"})
    assert excinfo.value.code == "index_out_of_range"


@live
@pytest.mark.asyncio
async def test_set_values_unknown_property():
    res = await set_values(domain="ecopath_group", index=1, values={"non_existent_prop": 1.0})
    assert res["results"]["non_existent_prop"] == "unknown_property"


@live
@pytest.mark.asyncio
async def test_batch_commands_success():
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
async def test_unknown_command():
    with pytest.raises(EwEMCPError) as excinfo:
        await _send("non_existent_command")
    assert excinfo.value.code == "unknown_command"


@live
@pytest.mark.asyncio
async def test_load_model_invalid_path():
    res = await load_model(r"C:\non_existent_file.EwEmdb")
    assert res["loaded"] is False
