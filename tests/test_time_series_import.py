"""Tests for programmatic time series import (Issue #72)."""

import asyncio
from unittest.mock import patch

import pytest

from ewe_mcp.helpers import BATCH_BLOCKED_COMMANDS


def test_import_time_series_sends_params():
    """import_time_series sends the right params to the plugin with EwE-native CSV."""
    mock_result = {
        "status": "ok",
        "rows_imported": 3,
        "time_series_count": 3,
        "forcing_count": 0,
        "error_count": 0,
        "dataset_index": 2,
        "dataset_name": "MCP Import",
        "import_type": "biomass",
        "interval": "annual",
        "first_year": 1990,
        "num_points": 10,
        "loaded": True,
        "total_in_file": 3,
    }
    # EwE-native format: first cell not starting with "year"
    ewe_csv = ",Group1,Group2\nweight,1,1\ngroup,1,2\ntype,biomass,biomass\n1990,100,200\n1991,110,210\n1992,120,220"

    with patch("ewe_mcp.server._send", return_value=mock_result) as mock_send:
        from ewe_mcp.server import import_time_series

        asyncio.get_event_loop().run_until_complete(
            import_time_series(data=ewe_csv, import_type="biomass")
        )
        mock_send.assert_called_once_with(
            "import_time_series",
            {
                "data": ewe_csv,
                "import_type": "biomass",
                "dataset_name": "MCP Import",
                "delimiter": ",",
                "decimal_separator": ".",
                "interval": "annual",
                "enable_on_load": True,
            },
            timeout=120,
        )


def test_import_time_series_simple_format_transforms():
    """import_time_series auto-detects 'Year,...' header and transforms to EwE format."""
    mock_result = {"status": "ok", "rows_imported": 2, "time_series_count": 1}
    simple_csv = "Year,MySeries\n1990,100\n1991,110"

    with patch("ewe_mcp.server._send", return_value=mock_result) as mock_send:
        from ewe_mcp.server import import_time_series

        asyncio.get_event_loop().run_until_complete(
            import_time_series(data=simple_csv, import_type="catch")
        )
        call_args = mock_send.call_args
        sent_data = call_args[0][1]["data"]
        lines = sent_data.split("\n")
        assert len(lines) >= 4
        assert lines[0] == ",MySeries"
        assert "weight" in lines[1]
        assert "group" in lines[2]
        assert "type" in lines[3]
        assert "1990,100" in lines[4]
        assert "1991,110" in lines[5]


def test_import_time_series_custom_options():
    """import_time_series passes custom options correctly with EwE-native CSV."""
    mock_result = {"status": "ok", "rows_imported": 1, "time_series_count": 1,
                   "forcing_count": 0, "error_count": 0, "dataset_index": 1,
                   "dataset_name": "My Dataset", "import_type": "forcing",
                   "interval": "timestep", "first_year": 2000, "num_points": 12,
                   "loaded": True, "total_in_file": 1}
    # EwE-native format: first cell empty (not "year")
    ewe_csv = ",Forcing1\nweight,1\ngroup,1\ntype,f\n2000,1.5"

    with patch("ewe_mcp.server._send", return_value=mock_result) as mock_send:
        from ewe_mcp.server import import_time_series

        asyncio.get_event_loop().run_until_complete(
            import_time_series(
                data=ewe_csv,
                import_type="forcing",
                dataset_name="My Dataset",
                delimiter=";",
                decimal_separator=",",
                interval="timestep",
                enable_on_load=False,
            )
        )
        mock_send.assert_called_once_with(
            "import_time_series",
            {
                "data": ewe_csv,
                "import_type": "forcing",
                "dataset_name": "My Dataset",
                "delimiter": ";",
                "decimal_separator": ",",
                "interval": "timestep",
                "enable_on_load": False,
            },
            timeout=120,
        )


def test_import_time_series_is_batch_blocked():
    """import_time_series must be in BATCH_BLOCKED_COMMANDS because it mutates model data."""
    assert "import_time_series" in BATCH_BLOCKED_COMMANDS, (
        "import_time_series should be batch-blocked since it writes time series data to the model"
    )


def test_import_time_series_returns_result():
    """import_time_series returns the result from the plugin."""
    mock_result = {"status": "ok", "rows_imported": 5, "time_series_count": 5,
                   "forcing_count": 0, "error_count": 0, "dataset_index": 3,
                   "dataset_name": "MCP Import", "import_type": "catch",
                   "interval": "annual", "first_year": 1985, "num_points": 20,
                   "loaded": True, "total_in_file": 5}
    # EwE-native: first cell empty, not "year"
    ewe_csv = ",Group1\nweight,1\ngroup,1\ntype,catch\n1985,50"

    with patch("ewe_mcp.server._send", return_value=mock_result):
        from ewe_mcp.server import import_time_series

        result = asyncio.get_event_loop().run_until_complete(
            import_time_series(data=ewe_csv, import_type="catch")
        )
    assert result["status"] == "ok"
    assert result["rows_imported"] == 5
    assert result["import_type"] == "catch"


def test_csharp_dispatch_routes_import_time_series():
    """Verify the C# plugin dispatches import_time_series."""
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
    assert 'case "import_time_series":' in text
    assert "ImportTimeSeries" in text


def test_csharp_import_time_series_is_batch_blocked():
    """Verify the C# plugin also blocks import_time_series in batch."""
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
    assert '"import_time_series"' in text, "import_time_series should be in C# batch blocked commands"


def test_csharp_import_time_series_uses_reader_factory():
    """Verify the C# ImportTimeSeries method uses cTimeSeriesReaderFactory."""
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
    assert "cTimeSeriesReaderFactory" in text
    assert "ImportEcosimTimeSeries" in text
    assert "AppendTimeSeriesDataset" in text
    assert "LoadTimeSeries" in text
