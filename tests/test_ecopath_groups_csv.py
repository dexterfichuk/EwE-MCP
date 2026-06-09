"""Tests for export_ecopath_groups_csv and import_ecopath_groups_csv."""
from unittest.mock import AsyncMock, patch

import pytest


def _make_list_groups_response():
    """Simulate the list_ecopath_groups response from the C# plugin."""
    return {
        "groups": [
            {
                "name": "Phytoplankton",
                "index": 1,
                "type": "producer",
                "input": {"biomass_area": 10.5, "pb": 50.0, "qb": 0.0, "ee": 0.95},
            },
            {
                "name": "Zooplankton",
                "index": 2,
                "type": "consumer",
                "input": {"biomass_area": 2.3, "pb": 20.0, "qb": 80.0, "ee": 0.90},
            },
            {
                "name": "Snook",
                "index": 3,
                "type": "consumer",
                "input": {"biomass_area": 0.5, "pb": 0.8, "qb": 5.0, "ee": None},
            },
            {
                "name": "Detritus",
                "index": 4,
                "type": "detritus",
                "input": {"biomass_area": 1.0, "pb": None, "qb": 0.0, "ee": 0.0},
            },
        ],
        "total_count": 4,
    }


def _make_list_groups_with_sentinel():
    """Response with group containing sentinel values."""
    return {
        "groups": [
            {
                "name": "Phytoplankton",
                "index": 1,
                "type": "producer",
                "input": {"biomass_area": -9999.0, "pb": -9999.0, "qb": 0.0, "ee": -9999.0},
            },
            {
                "name": "Zooplankton",
                "index": 2,
                "type": "consumer",
                "input": {"biomass_area": 2.3, "pb": 20.0, "qb": 80.0, "ee": 0.90},
            },
            {
                "name": "Snook",
                "index": 3,
                "type": "consumer",
                "input": {"biomass_area": 0.5, "pb": 0.8, "qb": 5.0, "ee": None},
            },
            {
                "name": "Detritus",
                "index": 4,
                "type": "detritus",
                "input": {"biomass_area": -9999.0, "pb": None, "qb": 0.0, "ee": 0.0},
            },
        ],
        "total_count": 4,
    }


@pytest.mark.asyncio
async def test_export_ecopath_groups_csv_basic():
    """Export returns CSV string with correct headers and all groups."""
    mock_response = _make_list_groups_response()
    list_called = False

    async def mock_send(cmd, params=None, timeout=30):
        nonlocal list_called
        if cmd == "list_ecopath_groups":
            list_called = True
            return mock_response
        return {}

    with patch("ewe_mcp.server._send", side_effect=mock_send):
        from ewe_mcp.server import export_ecopath_groups_csv
        result = await export_ecopath_groups_csv()

    assert result["status"] == "success"
    assert result["group_count"] == 4
    assert list_called

    # Check CSV content
    raw = result["csv"].strip()
    csv_lines = [line.rstrip("\r") for line in raw.split("\n")]
    assert len(csv_lines) == 5  # header + 4 groups
    assert csv_lines[0] == "Group,Biomass,PB,QB,EE,GroupType"
    # Groups appear in list_ecopath_groups response order: Phytoplankton, Zooplankton, Snook, Detritus
    assert csv_lines[1].startswith("Phytoplankton")
    assert csv_lines[2].startswith("Zooplankton")
    assert csv_lines[3].startswith("Snook")
    assert csv_lines[4].startswith("Detritus")


@pytest.mark.asyncio
async def test_export_ecopath_groups_csv_with_path():
    """Export writes to host file when path is provided."""
    mock_response = _make_list_groups_response()
    sent_path = None
    sent_content = None

    async def mock_send(cmd, params=None, timeout=30):
        nonlocal sent_path, sent_content
        if cmd == "list_ecopath_groups":
            return mock_response
        if cmd == "write_host_file":
            sent_path = params.get("path")
            sent_content = params.get("content")
            return {"status": "success"}
        return {}

    with patch("ewe_mcp.server._send", side_effect=mock_send):
        from ewe_mcp.server import export_ecopath_groups_csv
        result = await export_ecopath_groups_csv(path="C:\\tmp\\groups.csv")

    assert result["written_to_host"] is True
    assert sent_path == "C:\\tmp\\groups.csv"
    assert sent_content is not None
    assert "Group,Biomass" in sent_content


@pytest.mark.asyncio
async def test_import_ecopath_groups_csv_basic():
    """Import parses CSV and delegates to set_values_by_name_batch with correct operations."""
    csv_data = "Group,Biomass,PB,QB,EE\nPhytoplankton,12.0,55.0,,0.98\nZooplankton,2.5,22.0,85.0,0.92\n"
    captured_ops = None
    captured_opts = None

    async def mock_batch(operations, options=None):
        nonlocal captured_ops, captured_opts
        captured_ops = operations
        captured_opts = options
        return {
            "status": "success",
            "summary": {"total": 2, "applied": 2, "failed": 0, "not_found": 0},
            "results": [
                {"domain": "ecopath_group", "name": "Phytoplankton", "status": "applied"},
                {"domain": "ecopath_group", "name": "Zooplankton", "status": "applied"},
            ],
        }

    with patch("ewe_mcp.server.set_values_by_name_batch", new=mock_batch):
        from ewe_mcp.server import import_ecopath_groups_csv
        result = await import_ecopath_groups_csv(data=csv_data)

    assert result["summary"]["total"] == 2
    assert result["summary"]["applied"] == 2
    assert captured_ops is not None
    assert len(captured_ops) == 2

    # Check first operation
    op1 = captured_ops[0]
    assert op1["domain"] == "ecopath_group"
    assert op1["name"] == "Phytoplankton"
    assert op1["values"]["biomass_area_input"] == 12.0
    assert op1["values"]["pbinput"] == 55.0
    assert "qbinput" not in op1["values"]  # was empty string, so skipped
    assert op1["values"]["eeinput"] == 0.98

    # Check second operation
    op2 = captured_ops[1]
    assert op2["domain"] == "ecopath_group"
    assert op2["name"] == "Zooplankton"
    assert op2["values"]["biomass_area_input"] == 2.5
    assert op2["values"]["pbinput"] == 22.0
    assert op2["values"]["qbinput"] == 85.0
    assert op2["values"]["eeinput"] == 0.92

    assert captured_opts is not None
    assert captured_opts.get("include_suggestions") is True


@pytest.mark.asyncio
async def test_import_ecopath_groups_csv_ignores_grouptype():
    """GroupType column is informational and ignored on import."""
    csv_data = "Group,Biomass,PB,QB,EE,GroupType\nPhytoplankton,12.0,55.0,,0.98,producer\n"
    captured_ops = None

    async def mock_batch(operations, options=None):
        nonlocal captured_ops
        captured_ops = operations
        return {
            "status": "success",
            "summary": {"total": 1, "applied": 1, "failed": 0, "not_found": 0},
            "results": [{"domain": "ecopath_group", "name": "Phytoplankton", "status": "applied"}],
        }

    with patch("ewe_mcp.server.set_values_by_name_batch", new=mock_batch):
        from ewe_mcp.server import import_ecopath_groups_csv
        result = await import_ecopath_groups_csv(data=csv_data)

    assert result["summary"]["total"] == 1
    op = captured_ops[0]
    assert op["values"]["biomass_area_input"] == 12.0
    assert op["values"]["pbinput"] == 55.0
    # GroupType should not appear in values
    assert not any(k.lower() == "grouptype" for k in op["values"])


@pytest.mark.asyncio
async def test_import_ecopath_groups_csv_skips_empty_rows():
    """Rows where all values are empty are skipped."""
    csv_data = "Group,Biomass,PB,QB,EE\nPhytoplankton,12.0,55.0,,0.98\n,,,,\nZooplankton,2.5,,,0.92\n"
    captured_ops = None

    async def mock_batch(operations, options=None):
        nonlocal captured_ops
        captured_ops = operations
        return {
            "status": "success",
            "summary": {"total": 2, "applied": 2, "failed": 0},
            "results": [
                {"domain": "ecopath_group", "name": "Phytoplankton", "status": "applied"},
                {"domain": "ecopath_group", "name": "Zooplankton", "status": "applied"},
            ],
        }

    with patch("ewe_mcp.server.set_values_by_name_batch", new=mock_batch):
        from ewe_mcp.server import import_ecopath_groups_csv
        result = await import_ecopath_groups_csv(data=csv_data)

    assert captured_ops is not None
    assert len(captured_ops) == 2  # empty row skipped


@pytest.mark.asyncio
async def test_import_ecopath_groups_csv_invalid_header():
    """Rejects CSV without 'Group' as first column."""
    csv_data = "Name,Biomass,PB\nPhytoplankton,12.0,55.0\n"

    with patch("ewe_mcp.server.set_values_by_name_batch", new=AsyncMock()):
        from ewe_mcp.server import import_ecopath_groups_csv
        with pytest.raises(ValueError, match="first column must be 'Group'"):
            await import_ecopath_groups_csv(data=csv_data)


@pytest.mark.asyncio
async def test_import_ecopath_groups_csv_no_data_no_path():
    """Rejects when neither data nor path is provided."""
    with patch("ewe_mcp.server.set_values_by_name_batch", new=AsyncMock()):
        from ewe_mcp.server import import_ecopath_groups_csv
        with pytest.raises(ValueError, match="provide.*CSV data"):
            await import_ecopath_groups_csv()


@pytest.mark.asyncio
async def test_import_ecopath_groups_csv_reads_from_path():
    """When path is provided, reads file from EwE host."""
    csv_data = "Group,Biomass,PB\nPhytoplankton,12.0,55.0\n"
    captured_ops = None
    read_file_called = False

    async def mock_send(cmd, params=None, timeout=30):
        nonlocal read_file_called
        if cmd == "read_host_file":
            read_file_called = True
            return {"content": csv_data, "status": "success"}
        if cmd == "list_ecopath_groups":
            return _make_list_groups_response()
        # Let other _send calls through for batch function internals
        return {}

    async def mock_batch(operations, options=None):
        nonlocal captured_ops
        captured_ops = operations
        return {
            "status": "success",
            "summary": {"total": 1, "applied": 1, "failed": 0},
            "results": [{"domain": "ecopath_group", "name": "Phytoplankton", "status": "applied"}],
        }

    with patch("ewe_mcp.server._send", side_effect=mock_send), \
         patch("ewe_mcp.server.set_values_by_name_batch", new=mock_batch):
        from ewe_mcp.server import import_ecopath_groups_csv
        result = await import_ecopath_groups_csv(path="C:\\tmp\\groups.csv")

    assert result["summary"]["total"] == 1
    assert read_file_called
    assert captured_ops is not None
    assert captured_ops[0]["name"] == "Phytoplankton"


@pytest.mark.asyncio
async def test_import_ecopath_groups_csv_skip_invalid_numeric():
    """Rows with invalid numeric values skip those fields gracefully."""
    csv_data = "Group,Biomass,PB,QB,EE\nPhytoplankton,12.0,notanumber,,0.98\n"
    captured_ops = None

    async def mock_batch(operations, options=None):
        nonlocal captured_ops
        captured_ops = operations
        return {
            "status": "success",
            "summary": {"total": len(operations), "applied": len(operations), "failed": 0},
            "results": [],
        }

    with patch("ewe_mcp.server.set_values_by_name_batch", new=mock_batch):
        from ewe_mcp.server import import_ecopath_groups_csv
        result = await import_ecopath_groups_csv(data=csv_data)

    # The row should still be counted, just the invalid field skipped
    assert result["summary"]["total"] == 1
    assert captured_ops is not None
    op = captured_ops[0]
    assert "pbinput" not in op["values"]  # notanumber skipped
    assert op["values"]["biomass_area_input"] == 12.0
    assert op["values"]["eeinput"] == 0.98
