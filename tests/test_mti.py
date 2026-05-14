"""Tests for the get_mti_matrix MCP tool."""

import asyncio
import os
import pathlib
from unittest.mock import patch

import pytest

live = pytest.mark.skipif(
    os.environ.get("EWE_LIVE_TESTS") != "1",
    reason="Requires live EwE MCP plugin (set EWE_LIVE_TESTS=1 to run)",
)


def test_mti_matrix_not_in_batch_blocked():
    """get_mti_matrix is read-only and should NOT be in BATCH_BLOCKED_COMMANDS."""
    from ewe_mcp.helpers import BATCH_BLOCKED_COMMANDS

    assert "get_mti_matrix" not in BATCH_BLOCKED_COMMANDS, (
        "get_mti_matrix should not be in BATCH_BLOCKED_COMMANDS"
    )


def test_mti_matrix_command_in_csharp_dispatch():
    """Verify the C# plugin dispatches get_mti_matrix."""
    cs_path = (
        pathlib.Path(__file__).resolve().parents[1]
        / "src"
        / "EwE_MCP_Plugin"
        / "EwEMcpPlugin.cs"
    )
    if not cs_path.exists():
        pytest.skip("C# source not available")
    text = cs_path.read_text(encoding="utf-8")
    assert 'case "get_mti_matrix"' in text, (
        "C# Dispatch must include get_mti_matrix case"
    )
    assert "GetMtiMatrix" in text, "C# must implement GetMtiMatrix method"


def test_mti_matrix_sends_correct_command():
    """Verify the Python tool sends the right command name."""

    async def _run():
        mock_result = {
            "status": "available",
            "group_names": ["Group1", "Group2"],
            "n_groups": 2,
            "matrix": [[0.0, 0.5], [-0.3, 0.0]],
        }
        with patch("ewe_mcp.server._send", return_value=mock_result) as mock_send:
            from ewe_mcp.server import get_mti_matrix

            result = await get_mti_matrix()
            mock_send.assert_called_once_with("get_mti_matrix")
            assert result["status"] == "available"
            assert result["n_groups"] == 2
            assert len(result["group_names"]) == 2
            assert len(result["matrix"]) == 2
            assert len(result["matrix"][0]) == 2

    asyncio.get_event_loop().run_until_complete(_run())


def test_mti_matrix_zero_fallback_result():
    """Verify the tool handles zero_fallback status gracefully."""

    async def _run():
        mock_result = {
            "status": "zero_fallback",
            "group_names": ["G1", "G2", "G3"],
            "n_groups": 3,
            "matrix": [[0.0, 0.0, 0.0], [0.0, 0.0, 0.0], [0.0, 0.0, 0.0]],
        }
        with patch("ewe_mcp.server._send", return_value=mock_result):
            from ewe_mcp.server import get_mti_matrix

            result = await get_mti_matrix()
            assert result["status"] == "zero_fallback"
            assert result["n_groups"] == 3
            for row in result["matrix"]:
                assert all(v == 0.0 for v in row)

    asyncio.get_event_loop().run_until_complete(_run())


def test_mti_matrix_result_structure():
    """Verify the result has all expected keys."""

    async def _run():
        mock_result = {
            "status": "available",
            "group_names": ["A"],
            "n_groups": 1,
            "matrix": [[0.0]],
        }
        with patch("ewe_mcp.server._send", return_value=mock_result):
            from ewe_mcp.server import get_mti_matrix

            result = await get_mti_matrix()
            assert "status" in result
            assert "group_names" in result
            assert "n_groups" in result
            assert "matrix" in result
            assert isinstance(result["group_names"], list)
            assert isinstance(result["matrix"], list)

    asyncio.get_event_loop().run_until_complete(_run())


@live
def test_mti_matrix_after_ecopath_run(live_client, sample_model_path):
    """Live test: get_mti_matrix returns structured data after Ecopath runs."""
    live_client.command_result("load_model", {"path": sample_model_path})
    live_client.command_result("run_ecopath")
    result = live_client.command_result("get_mti_matrix")
    assert "status" in result
    assert "group_names" in result
    assert "n_groups" in result
    assert "matrix" in result
    assert result["n_groups"] > 0
    assert len(result["group_names"]) == result["n_groups"]
    assert len(result["matrix"]) == result["n_groups"]
