"""Unit tests for Ecospace spatial, MPA, and region tools."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from ewe_mcp.helpers import BATCH_BLOCKED_COMMANDS


# ── Batch blocking tests ──────────────────────────────────────────


def test_create_ecospace_mpa_blocked_from_batch():
    assert "create_ecospace_mpa" in BATCH_BLOCKED_COMMANDS


def test_set_ecospace_mpa_cells_blocked_from_batch():
    assert "set_ecospace_mpa_cells" in BATCH_BLOCKED_COMMANDS


def test_create_regions_from_mpas_blocked_from_batch():
    assert "create_regions_from_mpas" in BATCH_BLOCKED_COMMANDS


# ── Unit tests with mocked _send ─────────────────────────────────


@pytest.mark.asyncio
async def test_get_ecospace_map_summary():
    from ewe_mcp.server import get_ecospace_map_summary

    expected = {
        "rows": 30,
        "cols": 30,
        "land_cells": 100,
        "water_cells": 800,
        "n_mpa": 2,
        "n_habitats": 3,
        "n_regions": 2,
    }
    with patch("ewe_mcp.server._send", new_callable=AsyncMock, return_value=expected):
        result = await get_ecospace_map_summary()
    assert result["rows"] == 30
    assert result["cols"] == 30
    assert result["land_cells"] == 100
    assert result["water_cells"] == 800
    assert result["n_mpa"] == 2
    assert result["n_habitats"] == 3
    assert result["n_regions"] == 2


@pytest.mark.asyncio
async def test_get_ecospace_map_summary_sends_correct_command():
    from ewe_mcp.server import get_ecospace_map_summary

    with patch("ewe_mcp.server._send", new_callable=AsyncMock, return_value={}) as mock_send:
        await get_ecospace_map_summary()
        mock_send.assert_called_once_with("get_ecospace_map_summary")


@pytest.mark.asyncio
async def test_create_ecospace_mpa():
    from ewe_mcp.server import create_ecospace_mpa

    expected = {"created": True, "index": 3, "name": "Test MPA", "dbid": 42}
    with patch("ewe_mcp.server._send", new_callable=AsyncMock, return_value=expected) as mock_send:
        result = await create_ecospace_mpa(name="Test MPA")
    assert result["created"] is True
    assert result["name"] == "Test MPA"
    mock_send.assert_called_once_with(
        "create_ecospace_mpa",
        {"name": "Test MPA", "is_active": True},
        timeout=120,
    )


@pytest.mark.asyncio
async def test_create_ecospace_mpa_inactive():
    from ewe_mcp.server import create_ecospace_mpa

    expected = {"created": True, "index": 3, "name": "Inactive MPA", "dbid": 43}
    with patch("ewe_mcp.server._send", new_callable=AsyncMock, return_value=expected) as mock_send:
        result = await create_ecospace_mpa(name="Inactive MPA", is_active=False)
    assert result["created"] is True
    mock_send.assert_called_once_with(
        "create_ecospace_mpa",
        {"name": "Inactive MPA", "is_active": False},
        timeout=120,
    )


@pytest.mark.asyncio
async def test_set_ecospace_mpa_cells():
    from ewe_mcp.server import set_ecospace_mpa_cells

    cells = [[1, 2], [3, 4], [5, 6]]
    expected = {"set": True, "mpa_index": 1, "cells_set": 3}
    with patch("ewe_mcp.server._send", new_callable=AsyncMock, return_value=expected) as mock_send:
        result = await set_ecospace_mpa_cells(mpa_index=1, cells=cells)
    assert result["set"] is True
    assert result["cells_set"] == 3
    mock_send.assert_called_once_with(
        "set_ecospace_mpa_cells",
        {"mpa_index": 1, "cells": [[1, 2], [3, 4], [5, 6]]},
        timeout=120,
    )


@pytest.mark.asyncio
async def test_set_ecospace_mpa_cells_empty():
    from ewe_mcp.server import set_ecospace_mpa_cells

    expected = {"set": True, "mpa_index": 2, "cells_set": 0}
    with patch("ewe_mcp.server._send", new_callable=AsyncMock, return_value=expected) as mock_send:
        result = await set_ecospace_mpa_cells(mpa_index=2, cells=[])
    assert result["cells_set"] == 0
    mock_send.assert_called_once_with(
        "set_ecospace_mpa_cells",
        {"mpa_index": 2, "cells": []},
        timeout=120,
    )


@pytest.mark.asyncio
async def test_create_regions_from_mpas_default():
    from ewe_mcp.server import create_regions_from_mpas

    expected = {
        "regions_created": True,
        "region_count": 3,
        "cells_assigned": 150,
        "mpas_processed": 3,
    }
    with patch("ewe_mcp.server._send", new_callable=AsyncMock, return_value=expected) as mock_send:
        result = await create_regions_from_mpas()
    assert result["regions_created"] is True
    # When region_count is not provided, no region_count key in params
    mock_send.assert_called_once_with(
        "create_regions_from_mpas",
        {},
        timeout=120,
    )


@pytest.mark.asyncio
async def test_create_regions_from_mpas_with_count():
    from ewe_mcp.server import create_regions_from_mpas

    expected = {
        "regions_created": True,
        "region_count": 5,
        "cells_assigned": 200,
        "mpas_processed": 3,
    }
    with patch("ewe_mcp.server._send", new_callable=AsyncMock, return_value=expected) as mock_send:
        result = await create_regions_from_mpas(region_count=5)
    assert result["region_count"] == 5
    mock_send.assert_called_once_with(
        "create_regions_from_mpas",
        {"region_count": 5},
        timeout=120,
    )


@pytest.mark.asyncio
async def test_create_regions_from_mpas_with_count_zero():
    from ewe_mcp.server import create_regions_from_mpas

    expected = {
        "regions_created": True,
        "region_count": 0,
        "cells_assigned": 0,
        "mpas_processed": 0,
    }
    with patch("ewe_mcp.server._send", new_callable=AsyncMock, return_value=expected) as mock_send:
        result = await create_regions_from_mpas(region_count=0)
    mock_send.assert_called_once_with(
        "create_regions_from_mpas",
        {"region_count": 0},
        timeout=120,
    )
