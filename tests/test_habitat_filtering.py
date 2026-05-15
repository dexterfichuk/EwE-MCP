"""Tests for Ecospace habitat entity resolution filtering (#95)."""

import asyncio
from unittest.mock import patch

import pytest


def test_list_ecospace_habitats_filters_unavailable():
    """entity_unavailable habitats should be filtered out with filtered_count."""

    async def _run():
        mock_result = {
            "habitats": [
                {"index": 1, "name": "Seagrass", "status": "available"},
                {"index": 2, "name": "Unresolved", "status": "entity_unavailable"},
            ]
        }
        with patch("ewe_mcp.server._send", return_value=mock_result):
            from ewe_mcp.server import list_ecospace_habitats

            result = await list_ecospace_habitats()

        assert len(result["habitats"]) == 1
        assert result["habitats"][0]["name"] == "Seagrass"
        assert result.get("filtered_count") == 1

    asyncio.get_event_loop().run_until_complete(_run())


def test_list_ecospace_habitats_no_filter_when_all_available():
    """When all habitats are available, no filtering should occur."""

    async def _run():
        mock_result = {
            "habitats": [
                {"index": 1, "name": "Seagrass", "status": "available"},
                {"index": 2, "name": "Mangrove", "status": "available"},
            ]
        }
        with patch("ewe_mcp.server._send", return_value=mock_result):
            from ewe_mcp.server import list_ecospace_habitats

            result = await list_ecospace_habitats()

        assert len(result["habitats"]) == 2
        assert "filtered_count" not in result

    asyncio.get_event_loop().run_until_complete(_run())


def test_get_ecospace_habitat_raises_friendly_error_on_unavailable():
    """get_ecospace_habitat should raise a clear error for entity_unavailable."""

    async def _run():
        async def mock_send(cmd, params=None):
            raise RuntimeError("entity_unavailable: Could not resolve ecospace_habitats[1]")

        with patch("ewe_mcp.server._send", side_effect=mock_send):
            from ewe_mcp.server import get_ecospace_habitat

            with pytest.raises(RuntimeError, match="could not be resolved"):
                await get_ecospace_habitat(1)

    asyncio.get_event_loop().run_until_complete(_run())


def test_get_ecospace_habitat_passes_through_other_errors():
    """Non-entity_unavailable errors should pass through unchanged."""

    async def _run():
        async def mock_send(cmd, params=None):
            raise RuntimeError("Some other error")

        with patch("ewe_mcp.server._send", side_effect=mock_send):
            from ewe_mcp.server import get_ecospace_habitat

            with pytest.raises(RuntimeError, match="Some other error"):
                await get_ecospace_habitat(1)

    asyncio.get_event_loop().run_until_complete(_run())
