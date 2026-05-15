"""Tests for fishing mortality enrichment in get_ecopath_fleet_diagnostics."""

import asyncio
from unittest.mock import patch

import pytest


def _make_fleet_diag(fleet_groups):
    return {
        "fleets": [
            {"name": f"Fleet {i+1}", "catch_by_group": fleet_groups[i]}
            for i in range(len(fleet_groups))
        ]
    }


def _make_group_diag(groups):
    return {"groups": groups}


def test_fishing_mortality_enrichment_basic():
    """F = catch_total / biomass for each fleet/group pair."""

    async def _run():
        fleet_diag = _make_fleet_diag(
            [
                [
                    {"group_index": 1, "catch_total": 100.0, "landings": 90.0},
                    {"group_index": 2, "catch_total": 50.0, "landings": 45.0},
                ]
            ]
        )
        group_diag = _make_group_diag(
            [
                {"index": 1, "values": {"Biomass": 500.0}},
                {"index": 2, "values": {"Biomass": 250.0}},
            ]
        )

        async def mock_send(cmd, params=None):
            if cmd == "get_ecopath_fleet_diagnostics":
                return fleet_diag
            if cmd == "get_ecopath_group_diagnostics":
                return group_diag
            return {}

        with patch("ewe_mcp.server._send", side_effect=mock_send):
            from ewe_mcp.server import get_ecopath_fleet_diagnostics

            result = await get_ecopath_fleet_diagnostics()

        groups = result["fleets"][0]["catch_by_group"]
        assert groups[0].get("fishing_mortality") == pytest.approx(0.2, abs=0.001)
        assert groups[1].get("fishing_mortality") == pytest.approx(0.2, abs=0.001)

    asyncio.get_event_loop().run_until_complete(_run())


def test_fishing_mortality_zero_biomass_skipped():
    """Groups with zero biomass should not get fishing_mortality."""

    async def _run():
        fleet_diag = _make_fleet_diag(
            [
                [
                    {"group_index": 1, "catch_total": 100.0},
                    {"group_index": 2, "catch_total": 50.0},
                ]
            ]
        )
        group_diag = _make_group_diag(
            [
                {"index": 1, "values": {"Biomass": 0.0}},
                {"index": 2, "values": {"Biomass": 250.0}},
            ]
        )

        async def mock_send(cmd, params=None):
            if cmd == "get_ecopath_fleet_diagnostics":
                return fleet_diag
            if cmd == "get_ecopath_group_diagnostics":
                return group_diag
            return {}

        with patch("ewe_mcp.server._send", side_effect=mock_send):
            from ewe_mcp.server import get_ecopath_fleet_diagnostics

            result = await get_ecopath_fleet_diagnostics()

        groups = result["fleets"][0]["catch_by_group"]
        assert "fishing_mortality" not in groups[0]
        assert groups[1].get("fishing_mortality") == pytest.approx(0.2, abs=0.001)

    asyncio.get_event_loop().run_until_complete(_run())


def test_fishing_mortality_missing_biomass_skipped():
    """Groups not found in group diagnostics should not get fishing_mortality."""

    async def _run():
        fleet_diag = _make_fleet_diag(
            [[{"group_index": 99, "catch_total": 100.0}]]
        )
        group_diag = _make_group_diag(
            [{"index": 1, "values": {"Biomass": 500.0}}]
        )

        async def mock_send(cmd, params=None):
            if cmd == "get_ecopath_fleet_diagnostics":
                return fleet_diag
            if cmd == "get_ecopath_group_diagnostics":
                return group_diag
            return {}

        with patch("ewe_mcp.server._send", side_effect=mock_send):
            from ewe_mcp.server import get_ecopath_fleet_diagnostics

            result = await get_ecopath_fleet_diagnostics()

        assert "fishing_mortality" not in result["fleets"][0]["catch_by_group"][0]

    asyncio.get_event_loop().run_until_complete(_run())


def test_fishing_mortality_enrichment_failure_graceful():
    """If group diagnostics fails, fleet diagnostics should still be returned."""

    async def _run():
        fleet_diag = _make_fleet_diag(
            [[{"group_index": 1, "catch_total": 100.0}]]
        )

        call_count = 0

        async def mock_send(cmd, params=None):
            nonlocal call_count
            call_count += 1
            if cmd == "get_ecopath_fleet_diagnostics":
                return fleet_diag
            if cmd == "get_ecopath_group_diagnostics":
                raise RuntimeError("Ecopath has not run yet")
            return {}

        with patch("ewe_mcp.server._send", side_effect=mock_send):
            from ewe_mcp.server import get_ecopath_fleet_diagnostics

            result = await get_ecopath_fleet_diagnostics()

        assert "fleets" in result
        assert "fishing_mortality" not in result["fleets"][0]["catch_by_group"][0]

    asyncio.get_event_loop().run_until_complete(_run())


def test_fishing_mortality_no_catch_skipped():
    """Groups with no catch_total should not get fishing_mortality."""

    async def _run():
        fleet_diag = _make_fleet_diag(
            [[{"group_index": 1}]]
        )
        group_diag = _make_group_diag(
            [{"index": 1, "values": {"Biomass": 500.0}}]
        )

        async def mock_send(cmd, params=None):
            if cmd == "get_ecopath_fleet_diagnostics":
                return fleet_diag
            if cmd == "get_ecopath_group_diagnostics":
                return group_diag
            return {}

        with patch("ewe_mcp.server._send", side_effect=mock_send):
            from ewe_mcp.server import get_ecopath_fleet_diagnostics

            result = await get_ecopath_fleet_diagnostics()

        assert "fishing_mortality" not in result["fleets"][0]["catch_by_group"][0]

    asyncio.get_event_loop().run_until_complete(_run())
