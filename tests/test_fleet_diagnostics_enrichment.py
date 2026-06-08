"""Tests for get_ecopath_fleet_diagnostics enriched passthrough."""
from unittest.mock import patch

import pytest


def _make_fleet_diag_enriched(fleet_groups, enrichment_succeeded=True):
    """Simulate the enriched response from the C# plugin."""
    return {
        "fleets": [
            {"name": f"Fleet {i+1}", "catch_by_group": fleet_groups[i]}
            for i in range(len(fleet_groups))
        ],
        "total_count": len(fleet_groups),
        "_enrichment": {
            "succeeded": enrichment_succeeded,
            "sources": ["get_ecopath_group_diagnostics"]
        }
    }


@pytest.mark.asyncio
async def test_fleet_diagnostics_passthrough_to_enriched():
    """Python function calls get_ecopath_fleet_diagnostics_enriched command."""
    fleet_diag = _make_fleet_diag_enriched([
        [
            {"group_index": 1, "catch_total": 100.0, "landings": 90.0, "fishing_mortality": 0.2},
            {"group_index": 2, "catch_total": 50.0, "landings": 45.0, "fishing_mortality": 0.2},
        ]
    ])

    async def mock_send(cmd, params=None):
        if cmd == "get_ecopath_fleet_diagnostics_enriched":
            return fleet_diag
        return {}

    with patch("ewe_mcp.server._send", side_effect=mock_send):
        from ewe_mcp.server import get_ecopath_fleet_diagnostics
        result = await get_ecopath_fleet_diagnostics()

    assert "fleets" in result
    assert "status_code_map" in result
    assert "_enrichment" in result
    assert result["_enrichment"]["succeeded"] is True


@pytest.mark.asyncio
async def test_fleet_diagnostics_enrichment_failure_preserved():
    """When C# enrichment fails, _enrichment is preserved from plugin."""
    fleet_diag = _make_fleet_diag_enriched([
        [{"group_index": 1, "catch_total": 100.0}]
    ], enrichment_succeeded=False)

    async def mock_send(cmd, params=None):
        if cmd == "get_ecopath_fleet_diagnostics_enriched":
            return fleet_diag
        return {}

    with patch("ewe_mcp.server._send", side_effect=mock_send):
        from ewe_mcp.server import get_ecopath_fleet_diagnostics
        result = await get_ecopath_fleet_diagnostics()

    assert "fleets" in result
    assert result["_enrichment"]["succeeded"] is False


@pytest.mark.asyncio
async def test_fleet_diagnostics_empty_fleets():
    """Works with empty fleet list."""
    fleet_diag = _make_fleet_diag_enriched([])

    async def mock_send(cmd, params=None):
        if cmd == "get_ecopath_fleet_diagnostics_enriched":
            return fleet_diag
        return {}

    with patch("ewe_mcp.server._send", side_effect=mock_send):
        from ewe_mcp.server import get_ecopath_fleet_diagnostics
        result = await get_ecopath_fleet_diagnostics()

    assert result["fleets"] == []
    assert result["total_count"] == 0
    assert "status_code_map" in result
