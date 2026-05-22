"""Tests for Ecopath group and fleet creation tools."""

from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture
def mock_send():
    with patch("ewe_mcp.server._send", new_callable=AsyncMock) as m:
        m.return_value = {"created": True}
        yield m


@pytest.mark.asyncio
async def test_create_ecopath_group_requires_group_type(mock_send):
    from ewe_mcp.server import create_ecopath_group

    with pytest.raises(ValueError, match="group_type is required"):
        await create_ecopath_group(name="TestGroup")
    mock_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_ecopath_group_with_all_optional_params(mock_send):
    from ewe_mcp.server import create_ecopath_group

    result = await create_ecopath_group(
        name="TestGroup",
        biomass=1.5,
        pb_ratio=0.8,
        qb_ratio=10.0,
        ee=0.5,
        group_type=1,
    )
    mock_send.assert_awaited_once_with(
        "create_ecopath_group",
        {
            "name": "TestGroup",
            "biomass": 1.5,
            "pb_ratio": 0.8,
            "qb_ratio": 10.0,
            "ee": 0.5,
            "group_type": 1,
        },
    )
    assert result == {"created": True}


@pytest.mark.asyncio
@pytest.mark.parametrize("group_type", [0, 1, 2])
async def test_create_ecopath_group_accepts_all_group_types(mock_send, group_type):
    from ewe_mcp.server import create_ecopath_group

    result = await create_ecopath_group(name="Group", group_type=group_type)
    mock_send.assert_awaited_once_with(
        "create_ecopath_group",
        {
            "name": "Group",
            "group_type": group_type,
        },
    )
    assert result == {"created": True}


@pytest.mark.asyncio
@pytest.mark.parametrize("group_type", [-1, 3, 99, None])
async def test_create_ecopath_group_rejects_invalid_types(mock_send, group_type):
    from ewe_mcp.server import create_ecopath_group

    if group_type is None:
        with pytest.raises(ValueError, match="group_type is required"):
            await create_ecopath_group(name="BadGroup", group_type=group_type)
    else:
        with pytest.raises(ValueError, match="group_type must be"):
            await create_ecopath_group(name="BadGroup", group_type=group_type)
    mock_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_ecopath_fleet_name_only(mock_send):
    from ewe_mcp.server import create_ecopath_fleet

    result = await create_ecopath_fleet(name="TestFleet")
    mock_send.assert_awaited_once_with(
        "create_ecopath_fleet",
        {"name": "TestFleet"},
    )
    assert result == {"created": True}


def test_create_ecopath_group_is_batch_blocked():
    from ewe_mcp.helpers import BATCH_BLOCKED_COMMANDS

    assert "create_ecopath_group" in BATCH_BLOCKED_COMMANDS


def test_create_ecopath_fleet_is_batch_blocked():
    from ewe_mcp.helpers import BATCH_BLOCKED_COMMANDS

    assert "create_ecopath_fleet" in BATCH_BLOCKED_COMMANDS


def test_create_ecopath_commands_in_csharp_batch_blocked():
    """Verify the C# plugin also blocks these commands in batch."""
    import pathlib

    cs_path = (
        pathlib.Path(__file__).resolve().parents[1]
        / "src"
        / "EwE_MCP_Plugin"
        / "EwEMcpPlugin.cs"
    )
    if not cs_path.exists():
        pytest.skip("C# source not available")
    text = cs_path.read_text(encoding="utf-8")
    assert '"create_ecopath_group"' in text, "create_ecopath_group should be in C# batch blocked commands"
    assert '"create_ecopath_fleet"' in text, "create_ecopath_fleet should be in C# batch blocked commands"


def test_csharp_dispatch_routes_create_ecopath_group():
    """Verify the C# plugin dispatches create_ecopath_group."""
    import pathlib

    cs_path = (
        pathlib.Path(__file__).resolve().parents[1]
        / "src"
        / "EwE_MCP_Plugin"
        / "EwEMcpPlugin.cs"
    )
    if not cs_path.exists():
        pytest.skip("C# source not available")
    text = cs_path.read_text(encoding="utf-8")
    assert 'case "create_ecopath_group":' in text
    assert "CreateEcopathGroup" in text


def test_csharp_dispatch_routes_create_ecopath_fleet():
    """Verify the C# plugin dispatches create_ecopath_fleet."""
    import pathlib

    cs_path = (
        pathlib.Path(__file__).resolve().parents[1]
        / "src"
        / "EwE_MCP_Plugin"
        / "EwEMcpPlugin.cs"
    )
    if not cs_path.exists():
        pytest.skip("C# source not available")
    text = cs_path.read_text(encoding="utf-8")
    assert 'case "create_ecopath_fleet":' in text
    assert "CreateEcopathFleet" in text


def test_csharp_create_ecopath_group_calls_add_group():
    """Verify the C# CreateEcopathGroup method uses AddGroup via reflection."""
    import pathlib

    cs_path = (
        pathlib.Path(__file__).resolve().parents[1]
        / "src"
        / "EwE_MCP_Plugin"
        / "EwEMcpPlugin.cs"
    )
    if not cs_path.exists():
        pytest.skip("C# source not available")
    text = cs_path.read_text(encoding="utf-8")
    assert '"AddGroup"' in text or "'AddGroup'" in text


def test_csharp_create_ecopath_fleet_calls_add_fleet():
    """Verify the C# CreateEcopathFleet method uses AddFleet via reflection."""
    import pathlib

    cs_path = (
        pathlib.Path(__file__).resolve().parents[1]
        / "src"
        / "EwE_MCP_Plugin"
        / "EwEMcpPlugin.cs"
    )
    if not cs_path.exists():
        pytest.skip("C# source not available")
    text = cs_path.read_text(encoding="utf-8")
    assert '"AddFleet"' in text or "'AddFleet'" in text
