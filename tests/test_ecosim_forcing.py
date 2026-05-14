"""Unit tests for Ecosim forcing shape and PP forcing tools.

These tests mock the _send transport to verify that each tool
sends the correct command name and parameters to the plugin.
"""

import pytest

from ewe_mcp.helpers import BATCH_BLOCKED_COMMANDS


# ---------------------------------------------------------------------------
# Helper: patch _send within the server module
# ---------------------------------------------------------------------------

@pytest.fixture()
def mock_send(monkeypatch):
    """Return a list that records every (command, params) call to _send."""
    calls = []

    async def _fake_send(command, params=None, *, timeout=None):
        calls.append({"command": command, "params": params})
        return {"_mock": True, "command": command}

    import ewe_mcp.server as server_mod
    monkeypatch.setattr(server_mod, "_send", _fake_send)
    return calls


# ---------------------------------------------------------------------------
# list_ecosim_forcing_shapes
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_ecosim_forcing_shapes(mock_send):
    from ewe_mcp.server import list_ecosim_forcing_shapes

    result = await list_ecosim_forcing_shapes()
    assert result["_mock"] is True
    assert len(mock_send) == 1
    assert mock_send[0]["command"] == "list_ecosim_forcing_shapes"
    assert mock_send[0]["params"] is None


# ---------------------------------------------------------------------------
# get_ecosim_forcing_shape
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_ecosim_forcing_shape(mock_send):
    from ewe_mcp.server import get_ecosim_forcing_shape

    result = await get_ecosim_forcing_shape(index=3)
    assert result["_mock"] is True
    assert len(mock_send) == 1
    assert mock_send[0]["command"] == "get_ecosim_forcing_shape"
    assert mock_send[0]["params"] == {"index": 3}


# ---------------------------------------------------------------------------
# set_ecosim_forcing_shape
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_set_ecosim_forcing_shape(mock_send):
    from ewe_mcp.server import set_ecosim_forcing_shape

    data = [1.0, 0.5, 0.8, 1.2]
    result = await set_ecosim_forcing_shape(index=2, shape_data=data)
    assert result["_mock"] is True
    assert len(mock_send) == 1
    assert mock_send[0]["command"] == "set_ecosim_forcing_shape"
    assert mock_send[0]["params"] == {"index": 2, "shape_data": data}


def test_set_ecosim_forcing_shape_blocked_from_batch():
    """set_ecosim_forcing_shape mutates model state and must be blocked in batch."""
    assert "set_ecosim_forcing_shape" in BATCH_BLOCKED_COMMANDS


# ---------------------------------------------------------------------------
# list_ecosim_pp_forcing
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_ecosim_pp_forcing(mock_send):
    from ewe_mcp.server import list_ecosim_pp_forcing

    result = await list_ecosim_pp_forcing()
    assert result["_mock"] is True
    assert len(mock_send) == 1
    assert mock_send[0]["command"] == "list_ecosim_pp_forcing"
    assert mock_send[0]["params"] is None


# ---------------------------------------------------------------------------
# get_ecosim_pp_forcing
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_ecosim_pp_forcing(mock_send):
    from ewe_mcp.server import get_ecosim_pp_forcing

    result = await get_ecosim_pp_forcing(index=5)
    assert result["_mock"] is True
    assert len(mock_send) == 1
    assert mock_send[0]["command"] == "get_ecosim_pp_forcing"
    assert mock_send[0]["params"] == {"index": 5}


# ---------------------------------------------------------------------------
# Verify batch_commands blocks set_ecosim_forcing_shape
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_set_ecosim_forcing_shape_rejected_in_batch(mock_send):
    from ewe_mcp.server import batch_commands

    with pytest.raises(ValueError, match="is not allowed in batch"):
        await batch_commands(
            [
                {
                    "type": "set_ecosim_forcing_shape",
                    "params": {"index": 1, "shape_data": [1.0, 1.0]},
                }
            ]
        )
    # _send should never have been called
    assert len(mock_send) == 0
