"""Tests for Ecotracer, Monte Carlo, and Ecospace CSV export/import tools."""
from unittest.mock import AsyncMock, patch

import pytest


# ── Fixtures ───────────────────────────────────────────────────────


def _make_ecotracer_list_response():
    """Simulate list_ecotracer_group_inputs response."""
    return {
        "items": [
            {"index": 1, "name": "Phytoplankton"},
            {"index": 2, "name": "Zooplankton"},
            {"index": 3, "name": "Snook"},
        ],
        "total_count": 3,
    }


def _make_ecotracer_get_responses():
    """Simulate get_ecotracer_group_input responses."""
    return [
        {
            "index": 1,
            "name": "Phytoplankton",
            "values": {"concentration": 0.5, "decay_rate": 0.01, "uptake_rate": 0.2},
        },
        {
            "index": 2,
            "name": "Zooplankton",
            "values": {"concentration": 1.2, "decay_rate": 0.02, "uptake_rate": 0.15},
        },
        {
            "index": 3,
            "name": "Snook",
            "values": {"concentration": 3.5, "decay_rate": 0.005, "uptake_rate": 0.0},
        },
    ]


def _make_monte_carlo_list_response():
    """Simulate list_monte_carlo_groups response."""
    return {
        "items": [
            {"index": 1, "name": "Phytoplankton"},
            {"index": 2, "name": "Zooplankton"},
        ],
        "total_count": 2,
    }


def _make_monte_carlo_get_responses():
    """Simulate get_monte_carlo_group responses."""
    return [
        {
            "index": 1,
            "name": "Phytoplankton",
            "values": {
                "cv_biomass": 0.2,
                "cv_pb": 0.3,
                "cv_qb": 0.0,
                "cv_ee": 0.1,
                "cv_catch": 0.25,
            },
        },
        {
            "index": 2,
            "name": "Zooplankton",
            "values": {
                "cv_biomass": 0.15,
                "cv_pb": 0.25,
                "cv_qb": 0.2,
                "cv_ee": 0.12,
                "cv_catch": None,
            },
        },
    ]


def _make_ecospace_list_response():
    """Simulate list_ecospace_group_inputs response."""
    return {
        "group_inputs": [
            {"index": 1, "name": "Phytoplankton"},
            {"index": 2, "name": "Zooplankton"},
        ],
        "total_count": 2,
    }


def _make_ecospace_get_responses():
    """Simulate get_ecospace_group_input responses."""
    return [
        {
            "index": 1,
            "name": "Phytoplankton",
            "values": {
                "dispersal_rate": 0.5,
                "dispersal_rate_juvenile": 0.3,
                "adult_movement": 1.0,
            },
        },
        {
            "index": 2,
            "name": "Zooplankton",
            "values": {
                "dispersal_rate": 0.8,
                "dispersal_rate_juvenile": 0.6,
                "adult_movement": 1.0,
            },
        },
    ]


def _make_mc_params_response():
    """Simulate get_monte_carlo_parameters response."""
    return {"ntrials": 100, "search_mode": "best_fit"}


# ── Ecotracer CSV Export ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_export_ecotracer_inputs_csv_basic():
    """Export returns CSV with correct headers and all groups."""
    tracer_state = {"has_ecotracer_loaded": True}
    list_resp = _make_ecotracer_list_response()
    get_responses = _make_ecotracer_get_responses()

    call_log: list[str] = []

    async def mock_send(cmd, params=None, timeout=30):
        call_log.append(cmd)
        if cmd == "get_ecotracer_run_state":
            return tracer_state
        if cmd == "list_ecotracer_group_inputs":
            return list_resp
        if cmd == "batch":
            idx = (params or {}).get("commands", [{}])[0].get("params", {}).get("index", 1)
            return {"results": get_responses}
        return {}

    with patch("ewe_mcp.server._send", side_effect=mock_send):
        from ewe_mcp.server import export_ecotracer_inputs_csv
        result = await export_ecotracer_inputs_csv()

    assert result["status"] == "success"
    assert result["group_count"] == 3

    raw = result["csv"].strip()
    csv_lines = [line.rstrip("\r") for line in raw.split("\n")]
    assert len(csv_lines) == 4  # header + 3 groups

    # Check header
    headers = csv_lines[0].split(",")
    assert headers[0] == "Group"
    expected_fields = {"concentration", "decay_rate", "uptake_rate"}
    assert expected_fields.issubset(set(headers[1:]))

    # Check data rows
    assert csv_lines[1].startswith("Phytoplankton,")
    assert csv_lines[2].startswith("Zooplankton,")
    assert csv_lines[3].startswith("Snook,")


@pytest.mark.asyncio
async def test_export_ecotracer_inputs_csv_no_scenario():
    """Export returns error when no Ecotracer scenario is loaded."""
    tracer_state = {"has_ecotracer_loaded": False}

    async def mock_send(cmd, params=None, timeout=30):
        if cmd == "get_ecotracer_run_state":
            return tracer_state
        return {}

    with patch("ewe_mcp.server._send", side_effect=mock_send):
        from ewe_mcp.server import export_ecotracer_inputs_csv
        result = await export_ecotracer_inputs_csv()

    assert result["status"] == "error"
    assert "no ecotracer scenario loaded" in result["message"].lower()


@pytest.mark.asyncio
async def test_export_ecotracer_inputs_csv_with_path():
    """Export writes to host file when path is provided."""
    tracer_state = {"has_ecotracer_loaded": True}
    list_resp = _make_ecotracer_list_response()
    get_responses = _make_ecotracer_get_responses()
    sent_path = None
    sent_content = None

    async def mock_send(cmd, params=None, timeout=30):
        nonlocal sent_path, sent_content
        if cmd == "get_ecotracer_run_state":
            return tracer_state
        if cmd == "list_ecotracer_group_inputs":
            return list_resp
        if cmd == "batch":
            return {"results": get_responses}
        if cmd == "write_host_file":
            sent_path = params.get("path")
            sent_content = params.get("content")
            return {"status": "success"}
        return {}

    with patch("ewe_mcp.server._send", side_effect=mock_send):
        from ewe_mcp.server import export_ecotracer_inputs_csv
        result = await export_ecotracer_inputs_csv(path="C:\\tmp\\ecotracer_inputs.csv")

    assert result["written_to_host"] is True
    assert sent_path == "C:\\tmp\\ecotracer_inputs.csv"
    assert sent_content is not None
    assert "Group" in sent_content


# ── Ecotracer CSV Import ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_import_ecotracer_inputs_csv_basic():
    """Import parses CSV and delegates to set_values_by_name_batch."""
    csv_data = "Group,concentration,decay_rate\nPhytoplankton,0.6,0.015\nZooplankton,1.5,0.025\n"
    captured_ops = None

    async def mock_batch(operations, options=None):
        nonlocal captured_ops
        captured_ops = operations
        return {
            "status": "success",
            "summary": {"total": 2, "applied": 2, "failed": 0, "not_found": 0},
            "results": [
                {"domain": "ecotracer_group_input", "name": "Phytoplankton", "status": "applied"},
                {"domain": "ecotracer_group_input", "name": "Zooplankton", "status": "applied"},
            ],
        }

    with patch("ewe_mcp.server.set_values_by_name_batch", new=mock_batch):
        from ewe_mcp.server import import_ecotracer_inputs_csv
        result = await import_ecotracer_inputs_csv(data=csv_data)

    assert result["summary"]["total"] == 2
    assert result["summary"]["applied"] == 2
    assert captured_ops is not None
    assert len(captured_ops) == 2

    # Check operations
    op1 = captured_ops[0]
    assert op1["domain"] == "ecotracer_group_input"
    assert op1["name"] == "Phytoplankton"
    assert op1["values"]["concentration"] == 0.6
    assert op1["values"]["decay_rate"] == 0.015

    op2 = captured_ops[1]
    assert op2["domain"] == "ecotracer_group_input"
    assert op2["name"] == "Zooplankton"
    assert op2["values"]["concentration"] == 1.5
    assert op2["values"]["decay_rate"] == 0.025


@pytest.mark.asyncio
async def test_import_ecotracer_inputs_csv_invalid_header():
    """Rejects CSV without 'Group' as first column."""
    csv_data = "Name,concentration\nPhytoplankton,0.5\n"

    with patch("ewe_mcp.server.set_values_by_name_batch", new=AsyncMock()):
        from ewe_mcp.server import import_ecotracer_inputs_csv
        with pytest.raises(ValueError, match="first column must be 'Group'"):
            await import_ecotracer_inputs_csv(data=csv_data)


@pytest.mark.asyncio
async def test_import_ecotracer_inputs_csv_skips_empty_values():
    """Rows with all empty values are skipped."""
    csv_data = "Group,concentration,decay_rate\nPhytoplankton,0.6,0.015\n,,,\n"
    captured_ops = None

    async def mock_batch(operations, options=None):
        nonlocal captured_ops
        captured_ops = operations
        return {
            "status": "success",
            "summary": {"total": 1, "applied": 1, "failed": 0},
            "results": [{"domain": "ecotracer_group_input", "name": "Phytoplankton", "status": "applied"}],
        }

    with patch("ewe_mcp.server.set_values_by_name_batch", new=mock_batch):
        from ewe_mcp.server import import_ecotracer_inputs_csv
        result = await import_ecotracer_inputs_csv(data=csv_data)

    assert result["summary"]["total"] == 1
    assert captured_ops is not None
    assert len(captured_ops) == 1


@pytest.mark.asyncio
async def test_import_ecotracer_inputs_csv_no_data_no_path():
    """Rejects when neither data nor path is provided."""
    with patch("ewe_mcp.server.set_values_by_name_batch", new=AsyncMock()):
        from ewe_mcp.server import import_ecotracer_inputs_csv
        with pytest.raises(ValueError, match="provide.*CSV data"):
            await import_ecotracer_inputs_csv()


@pytest.mark.asyncio
async def test_import_ecotracer_inputs_csv_reads_from_path():
    """When path is provided, reads file from EwE host."""
    csv_data = "Group,concentration\nPhytoplankton,0.6\n"
    captured_ops = None
    read_file_called = False

    async def mock_send(cmd, params=None, timeout=30):
        nonlocal read_file_called
        if cmd == "read_host_file":
            read_file_called = True
            return {"content": csv_data, "status": "success"}
        return {}

    async def mock_batch(operations, options=None):
        nonlocal captured_ops
        captured_ops = operations
        return {
            "status": "success",
            "summary": {"total": 1, "applied": 1, "failed": 0},
            "results": [{"domain": "ecotracer_group_input", "name": "Phytoplankton", "status": "applied"}],
        }

    with patch("ewe_mcp.server._send", side_effect=mock_send), \
         patch("ewe_mcp.server.set_values_by_name_batch", new=mock_batch):
        from ewe_mcp.server import import_ecotracer_inputs_csv
        result = await import_ecotracer_inputs_csv(path="C:\\tmp\\input.csv")

    assert result["summary"]["total"] == 1
    assert read_file_called
    assert captured_ops[0]["name"] == "Phytoplankton"


# ── Monte Carlo CSV Export ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_export_monte_carlo_csv_basic():
    """Export returns CSV with correct headers and all groups."""
    mc_params = _make_mc_params_response()
    list_resp = _make_monte_carlo_list_response()
    get_responses = _make_monte_carlo_get_responses()

    async def mock_send(cmd, params=None, timeout=30):
        if cmd == "get_monte_carlo_parameters":
            return mc_params
        if cmd == "list_monte_carlo_groups":
            return list_resp
        if cmd == "batch":
            return {"results": get_responses}
        return {}

    with patch("ewe_mcp.server._send", side_effect=mock_send):
        from ewe_mcp.server import export_monte_carlo_csv
        result = await export_monte_carlo_csv()

    assert result["status"] == "success"
    assert result["group_count"] == 2

    raw = result["csv"].strip()
    csv_lines = [line.rstrip("\r") for line in raw.split("\n")]
    assert len(csv_lines) == 3  # header + 2 groups

    headers = csv_lines[0].split(",")
    assert headers[0] == "Group"
    assert "cv_biomass" in headers
    assert "cv_pb" in headers


@pytest.mark.asyncio
async def test_export_monte_carlo_csv_not_available():
    """Export returns error when Monte Carlo is not available."""
    async def mock_send(cmd, params=None, timeout=30):
        if cmd == "get_monte_carlo_parameters":
            raise RuntimeError("core_unavailable: Monte Carlo not available")
        return {}

    with patch("ewe_mcp.server._send", side_effect=mock_send):
        from ewe_mcp.server import export_monte_carlo_csv
        result = await export_monte_carlo_csv()

    assert result["status"] == "error"
    assert "not available" in result["message"].lower()


@pytest.mark.asyncio
async def test_export_monte_carlo_csv_with_path():
    """Export writes to host file when path is provided."""
    mc_params = _make_mc_params_response()
    list_resp = _make_monte_carlo_list_response()
    get_responses = _make_monte_carlo_get_responses()
    sent_path = None
    sent_content = None

    async def mock_send(cmd, params=None, timeout=30):
        nonlocal sent_path, sent_content
        if cmd == "get_monte_carlo_parameters":
            return mc_params
        if cmd == "list_monte_carlo_groups":
            return list_resp
        if cmd == "batch":
            return {"results": get_responses}
        if cmd == "write_host_file":
            sent_path = params.get("path")
            sent_content = params.get("content")
            return {"status": "success"}
        return {}

    with patch("ewe_mcp.server._send", side_effect=mock_send):
        from ewe_mcp.server import export_monte_carlo_csv
        result = await export_monte_carlo_csv(path="C:\\tmp\\mc.csv")

    assert result["written_to_host"] is True
    assert sent_path == "C:\\tmp\\mc.csv"
    assert "Group" in sent_content


# ── Monte Carlo CSV Import ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_import_monte_carlo_csv_basic():
    """Import parses CSV and delegates to set_values_by_name_batch."""
    csv_data = "Group,cv_biomass,cv_pb\nPhytoplankton,0.25,0.35\nZooplankton,0.18,0.28\n"
    captured_ops = None

    async def mock_batch(operations, options=None):
        nonlocal captured_ops
        captured_ops = operations
        return {
            "status": "success",
            "summary": {"total": 2, "applied": 2, "failed": 0},
            "results": [
                {"domain": "monte_carlo_group", "name": "Phytoplankton", "status": "applied"},
                {"domain": "monte_carlo_group", "name": "Zooplankton", "status": "applied"},
            ],
        }

    with patch("ewe_mcp.server.set_values_by_name_batch", new=mock_batch):
        from ewe_mcp.server import import_monte_carlo_csv
        result = await import_monte_carlo_csv(data=csv_data)

    assert result["summary"]["total"] == 2
    assert result["summary"]["applied"] == 2
    assert captured_ops is not None
    assert len(captured_ops) == 2

    op1 = captured_ops[0]
    assert op1["domain"] == "monte_carlo_group"
    assert op1["name"] == "Phytoplankton"
    assert op1["values"]["cv_biomass"] == 0.25
    assert op1["values"]["cv_pb"] == 0.35

    op2 = captured_ops[1]
    assert op2["domain"] == "monte_carlo_group"
    assert op2["name"] == "Zooplankton"
    assert op2["values"]["cv_biomass"] == 0.18


@pytest.mark.asyncio
async def test_import_monte_carlo_csv_invalid_header():
    """Rejects CSV without 'Group' as first column."""
    csv_data = "CV,cv_biomass\nPhytoplankton,0.2\n"

    with patch("ewe_mcp.server.set_values_by_name_batch", new=AsyncMock()):
        from ewe_mcp.server import import_monte_carlo_csv
        with pytest.raises(ValueError, match="first column must be 'Group'"):
            await import_monte_carlo_csv(data=csv_data)


@pytest.mark.asyncio
async def test_import_monte_carlo_csv_skip_invalid_numeric():
    """Rows with invalid numeric values skip those fields gracefully."""
    csv_data = "Group,cv_biomass,cv_pb\nPhytoplankton,0.25,notanumber\n"
    captured_ops = None

    async def mock_batch(operations, options=None):
        nonlocal captured_ops
        captured_ops = operations
        return {
            "status": "success",
            "summary": {"total": 1, "applied": 1, "failed": 0},
            "results": [],
        }

    with patch("ewe_mcp.server.set_values_by_name_batch", new=mock_batch):
        from ewe_mcp.server import import_monte_carlo_csv
        result = await import_monte_carlo_csv(data=csv_data)

    assert result["summary"]["total"] == 1
    assert captured_ops is not None
    op = captured_ops[0]
    assert op["values"]["cv_biomass"] == 0.25
    assert "cv_pb" not in op["values"]


# ── Ecospace CSV Export ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_export_ecospace_inputs_csv_basic():
    """Export returns CSV with correct headers and all groups."""
    space_state = {"is_ecospace_loaded": True}
    list_resp = _make_ecospace_list_response()
    get_responses = _make_ecospace_get_responses()

    async def mock_send(cmd, params=None, timeout=30):
        if cmd == "get_ecospace_run_state":
            return space_state
        if cmd == "list_ecospace_group_inputs":
            return list_resp
        if cmd == "batch":
            return {"results": get_responses}
        return {}

    with patch("ewe_mcp.server._send", side_effect=mock_send):
        from ewe_mcp.server import export_ecospace_inputs_csv
        result = await export_ecospace_inputs_csv()

    assert result["status"] == "success"
    assert result["group_count"] == 2

    raw = result["csv"].strip()
    csv_lines = [line.rstrip("\r") for line in raw.split("\n")]
    assert len(csv_lines) == 3  # header + 2 groups

    headers = csv_lines[0].split(",")
    assert headers[0] == "Group"
    assert "dispersal_rate" in headers
    assert "adult_movement" in headers


@pytest.mark.asyncio
async def test_export_ecospace_inputs_csv_no_scenario():
    """Export returns error when no Ecospace scenario is loaded."""
    space_state = {"is_ecospace_loaded": False}

    async def mock_send(cmd, params=None, timeout=30):
        if cmd == "get_ecospace_run_state":
            return space_state
        return {}

    with patch("ewe_mcp.server._send", side_effect=mock_send):
        from ewe_mcp.server import export_ecospace_inputs_csv
        result = await export_ecospace_inputs_csv()

    assert result["status"] == "error"
    assert "no ecospace scenario loaded" in result["message"].lower()


@pytest.mark.asyncio
async def test_export_ecospace_inputs_csv_with_path():
    """Export writes to host file when path is provided."""
    space_state = {"is_ecospace_loaded": True}
    list_resp = _make_ecospace_list_response()
    get_responses = _make_ecospace_get_responses()
    sent_path = None
    sent_content = None

    async def mock_send(cmd, params=None, timeout=30):
        nonlocal sent_path, sent_content
        if cmd == "get_ecospace_run_state":
            return space_state
        if cmd == "list_ecospace_group_inputs":
            return list_resp
        if cmd == "batch":
            return {"results": get_responses}
        if cmd == "write_host_file":
            sent_path = params.get("path")
            sent_content = params.get("content")
            return {"status": "success"}
        return {}

    with patch("ewe_mcp.server._send", side_effect=mock_send):
        from ewe_mcp.server import export_ecospace_inputs_csv
        result = await export_ecospace_inputs_csv(path="C:\\tmp\\ecospace_inputs.csv")

    assert result["written_to_host"] is True
    assert sent_path == "C:\\tmp\\ecospace_inputs.csv"
    assert "Group" in sent_content


# ── Ecospace CSV Import ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_import_ecospace_inputs_csv_basic():
    """Import parses CSV and delegates to set_values_by_name_batch."""
    csv_data = "Group,dispersal_rate,adult_movement\nPhytoplankton,0.6,1.5\nZooplankton,0.9,1.2\n"
    captured_ops = None

    async def mock_batch(operations, options=None):
        nonlocal captured_ops
        captured_ops = operations
        return {
            "status": "success",
            "summary": {"total": 2, "applied": 2, "failed": 0},
            "results": [
                {"domain": "ecospace_group_input", "name": "Phytoplankton", "status": "applied"},
                {"domain": "ecospace_group_input", "name": "Zooplankton", "status": "applied"},
            ],
        }

    with patch("ewe_mcp.server.set_values_by_name_batch", new=mock_batch):
        from ewe_mcp.server import import_ecospace_inputs_csv
        result = await import_ecospace_inputs_csv(data=csv_data)

    assert result["summary"]["total"] == 2
    assert result["summary"]["applied"] == 2
    assert captured_ops is not None
    assert len(captured_ops) == 2

    op1 = captured_ops[0]
    assert op1["domain"] == "ecospace_group_input"
    assert op1["name"] == "Phytoplankton"
    assert op1["values"]["dispersal_rate"] == 0.6
    assert op1["values"]["adult_movement"] == 1.5


@pytest.mark.asyncio
async def test_import_ecospace_inputs_csv_invalid_header():
    """Rejects CSV without 'Group' as first column."""
    csv_data = "Name,dispersal_rate\nPhytoplankton,0.5\n"

    with patch("ewe_mcp.server.set_values_by_name_batch", new=AsyncMock()):
        from ewe_mcp.server import import_ecospace_inputs_csv
        with pytest.raises(ValueError, match="first column must be 'Group'"):
            await import_ecospace_inputs_csv(data=csv_data)


@pytest.mark.asyncio
async def test_import_ecospace_inputs_csv_skips_empty_rows():
    """Rows where all values are empty are skipped."""
    csv_data = "Group,dispersal_rate,adult_movement\nPhytoplankton,0.6,1.5\n,,,\nZooplankton,0.9,1.2\n"
    captured_ops = None

    async def mock_batch(operations, options=None):
        nonlocal captured_ops
        captured_ops = operations
        return {
            "status": "success",
            "summary": {"total": 2, "applied": 2, "failed": 0},
            "results": [
                {"domain": "ecospace_group_input", "name": "Phytoplankton", "status": "applied"},
                {"domain": "ecospace_group_input", "name": "Zooplankton", "status": "applied"},
            ],
        }

    with patch("ewe_mcp.server.set_values_by_name_batch", new=mock_batch):
        from ewe_mcp.server import import_ecospace_inputs_csv
        result = await import_ecospace_inputs_csv(data=csv_data)

    assert captured_ops is not None
    assert len(captured_ops) == 2  # empty row skipped


# ── Ecospace Fleet Inputs CSV Fixtures ───────────────────────────────


def _make_ecospace_fleet_list_response():
    """Simulate list_ecospace_fleet_inputs response."""
    return {
        "fleet_inputs": [
            {"index": 1, "name": "Fleet A"},
            {"index": 2, "name": "Fleet B"},
        ],
        "total_count": 2,
    }


def _make_ecospace_fleet_get_responses():
    """Simulate get_ecospace_fleet_input responses."""
    return [
        {
            "index": 1,
            "name": "Fleet A",
            "values": {"sail_cost": 100.0, "relative_effort": 0.5, "effort_capacity": 1.0},
        },
        {
            "index": 2,
            "name": "Fleet B",
            "values": {"sail_cost": 200.0, "relative_effort": 0.8, "effort_capacity": 1.5},
        },
    ]


def _make_fleets_list_response():
    """Simulate list_ecopath_fleets response."""
    return {
        "fleets": [
            {"index": 1, "name": "Gillnet"},
            {"index": 2, "name": "Trawl"},
        ],
        "total_count": 2,
    }


def _make_groups_list_response():
    """Simulate list_ecopath_groups response with consumer/detritus types."""
    return {
        "groups": [
            {"index": 1, "name": "Phytoplankton", "type": "producer"},
            {"index": 2, "name": "Zooplankton", "type": "consumer"},
            {"index": 3, "name": "Snook", "type": "consumer"},
            {"index": 4, "name": "Detritus", "type": "detritus"},
        ],
        "total_count": 4,
    }


def _make_fleet_get_values_response(variable, fill=1.0):
    """Simulate get_values for a fleet indexed variable."""
    return {
        "results": [
            {
                "index": 1,
                "name": "Gillnet",
                "values": {variable: [fill, fill * 0.5, fill * 0.3, 0.0]},
            },
            {
                "index": 2,
                "name": "Trawl",
                "values": {variable: [0.0, fill * 0.8, fill * 0.2, 0.0]},
            },
        ]
    }


def _make_fate_get_values_responses():
    """Simulate get_values for detritus_fate on groups."""
    return {
        "results": [
            {
                "index": 1,
                "name": "Phytoplankton",
                "values": {"detritus_fate": [0.8]},
            },
            {
                "index": 2,
                "name": "Zooplankton",
                "values": {"detritus_fate": [0.5]},
            },
            {
                "index": 3,
                "name": "Snook",
                "values": {"detritus_fate": [0.3]},
            },
        ]
    }


# ── Ecospace Fleet Inputs CSV Export Tests ──────────────────────────


@pytest.mark.asyncio
async def test_export_ecospace_fleet_inputs_csv_basic():
    """Export returns CSV with correct headers and all fleets."""
    space_state = {"is_ecospace_loaded": True}
    list_resp = _make_ecospace_fleet_list_response()
    get_responses = _make_ecospace_fleet_get_responses()

    call_log: list[str] = []

    async def mock_send(cmd, params=None, timeout=30):
        call_log.append(cmd)
        if cmd == "get_ecospace_run_state":
            return space_state
        if cmd == "list_ecospace_fleet_inputs":
            return list_resp
        if cmd == "batch":
            return {"results": get_responses}
        return {}

    with patch("ewe_mcp.server._send", side_effect=mock_send):
        from ewe_mcp.server import export_ecospace_fleet_inputs_csv
        result = await export_ecospace_fleet_inputs_csv()

    assert result["status"] == "success"
    assert result["fleet_count"] == 2

    raw = result["csv"].strip()
    csv_lines = [line.rstrip("\r") for line in raw.split("\n")]
    assert len(csv_lines) == 3  # header + 2 fleets

    # Check header
    headers = csv_lines[0].split(",")
    assert headers[0] == "Fleet"
    expected_fields = {"sail_cost", "relative_effort", "effort_capacity"}
    assert expected_fields.issubset(set(headers[1:]))

    # Check data rows
    assert csv_lines[1].startswith("Fleet A,")
    assert csv_lines[2].startswith("Fleet B,")


@pytest.mark.asyncio
async def test_export_ecospace_fleet_inputs_csv_no_scenario():
    """Export returns error when no Ecospace scenario is loaded."""
    space_state = {"is_ecospace_loaded": False}

    async def mock_send(cmd, params=None, timeout=30):
        if cmd == "get_ecospace_run_state":
            return space_state
        return {}

    with patch("ewe_mcp.server._send", side_effect=mock_send):
        from ewe_mcp.server import export_ecospace_fleet_inputs_csv
        result = await export_ecospace_fleet_inputs_csv()

    assert result["status"] == "error"
    assert "no ecospace scenario loaded" in result["message"].lower()


@pytest.mark.asyncio
async def test_export_ecospace_fleet_inputs_csv_with_path():
    """Export writes to host file when path is provided."""
    space_state = {"is_ecospace_loaded": True}
    list_resp = _make_ecospace_fleet_list_response()
    get_responses = _make_ecospace_fleet_get_responses()
    sent_path = None
    sent_content = None

    async def mock_send(cmd, params=None, timeout=30):
        nonlocal sent_path, sent_content
        if cmd == "get_ecospace_run_state":
            return space_state
        if cmd == "list_ecospace_fleet_inputs":
            return list_resp
        if cmd == "batch":
            return {"results": get_responses}
        if cmd == "write_host_file":
            sent_path = params.get("path")
            sent_content = params.get("content")
            return {"status": "success"}
        return {}

    with patch("ewe_mcp.server._send", side_effect=mock_send):
        from ewe_mcp.server import export_ecospace_fleet_inputs_csv
        result = await export_ecospace_fleet_inputs_csv(path="C:\\tmp\\ecospace_fleet.csv")

    assert result["written_to_host"] is True
    assert sent_path == "C:\\tmp\\ecospace_fleet.csv"
    assert sent_content is not None
    assert "Fleet" in sent_content


# ── Ecospace Fleet Inputs CSV Import Tests ──────────────────────────


@pytest.mark.asyncio
async def test_import_ecospace_fleet_inputs_csv_basic():
    """Import parses CSV and delegates to set_values_by_name_batch."""
    csv_data = "Fleet,sail_cost,relative_effort\nFleet A,150.0,0.6\nFleet B,250.0,0.9\n"
    captured_ops = None

    async def mock_batch(operations, options=None):
        nonlocal captured_ops
        captured_ops = operations
        return {
            "status": "success",
            "summary": {"total": 2, "applied": 2, "failed": 0, "not_found": 0},
            "results": [
                {"domain": "ecospace_fleet_input", "name": "Fleet A", "status": "applied"},
                {"domain": "ecospace_fleet_input", "name": "Fleet B", "status": "applied"},
            ],
        }

    with patch("ewe_mcp.server.set_values_by_name_batch", new=mock_batch):
        from ewe_mcp.server import import_ecospace_fleet_inputs_csv
        result = await import_ecospace_fleet_inputs_csv(data=csv_data)

    assert result["summary"]["total"] == 2
    assert result["summary"]["applied"] == 2
    assert captured_ops is not None
    assert len(captured_ops) == 2

    op1 = captured_ops[0]
    assert op1["domain"] == "ecospace_fleet_input"
    assert op1["name"] == "Fleet A"
    assert op1["values"]["sail_cost"] == 150.0
    assert op1["values"]["relative_effort"] == 0.6

    op2 = captured_ops[1]
    assert op2["domain"] == "ecospace_fleet_input"
    assert op2["name"] == "Fleet B"
    assert op2["values"]["sail_cost"] == 250.0


@pytest.mark.asyncio
async def test_import_ecospace_fleet_inputs_csv_invalid_header():
    """Rejects CSV without 'Fleet' as first column."""
    csv_data = "Name,sail_cost\nFleet A,100.0\n"

    with patch("ewe_mcp.server.set_values_by_name_batch", new=AsyncMock()):
        from ewe_mcp.server import import_ecospace_fleet_inputs_csv
        with pytest.raises(ValueError, match="first column must be 'Fleet'"):
            await import_ecospace_fleet_inputs_csv(data=csv_data)


@pytest.mark.asyncio
async def test_import_ecospace_fleet_inputs_csv_skips_empty_rows():
    """Rows with all empty values are skipped."""
    csv_data = "Fleet,sail_cost,relative_effort\nFleet A,150.0,0.6\n,,,\n"
    captured_ops = None

    async def mock_batch(operations, options=None):
        nonlocal captured_ops
        captured_ops = operations
        return {
            "status": "success",
            "summary": {"total": 1, "applied": 1, "failed": 0},
            "results": [{"domain": "ecospace_fleet_input", "name": "Fleet A", "status": "applied"}],
        }

    with patch("ewe_mcp.server.set_values_by_name_batch", new=mock_batch):
        from ewe_mcp.server import import_ecospace_fleet_inputs_csv
        result = await import_ecospace_fleet_inputs_csv(data=csv_data)

    assert result["summary"]["total"] == 1
    assert captured_ops is not None
    assert len(captured_ops) == 1


# ── Fleet Indexed Value Matrix Export Tests ─────────────────────────


@pytest.mark.asyncio
async def test_export_fleet_indexed_csv_basic():
    """Export returns matrix CSV with correct headers."""
    variable = "off_vessel_price"
    fleets_resp = _make_fleets_list_response()
    groups_resp = _make_groups_list_response()
    batch_resp = _make_fleet_get_values_response(variable, fill=5.0)

    async def mock_send(cmd, params=None, timeout=30):
        if cmd == "list_ecopath_fleets":
            return fleets_resp
        if cmd == "list_ecopath_groups":
            return groups_resp
        if cmd == "batch":
            return batch_resp
        return {}

    with patch("ewe_mcp.server._send", side_effect=mock_send):
        from ewe_mcp.server import export_fleet_indexed_csv
        result = await export_fleet_indexed_csv(variable=variable)

    assert result["status"] == "success"
    assert result["variable"] == variable
    assert result["fleet_count"] == 2
    assert result["group_count"] == 4

    raw = result["csv"].strip()
    csv_lines = [line.rstrip("\r") for line in raw.split("\n")]
    assert len(csv_lines) == 3  # header + 2 fleets

    # Check header
    headers = csv_lines[0].split(",")
    assert headers[0] == "Fleet"
    # All group names should be column headers
    assert "Snook" in headers
    assert "Detritus" in headers

    # Check data rows
    assert csv_lines[1].startswith("Gillnet,")
    assert csv_lines[2].startswith("Trawl,")


@pytest.mark.asyncio
async def test_export_fleet_indexed_csv_invalid_variable():
    """Rejects unsupported variable name."""
    from ewe_mcp.server import export_fleet_indexed_csv
    with pytest.raises(ValueError, match="Unsupported variable"):
        await export_fleet_indexed_csv(variable="not_a_valid_var")


@pytest.mark.asyncio
async def test_export_fleet_indexed_csv_with_path():
    """Export writes to host file when path is provided."""
    variable = "discard_mortality"
    fleets_resp = _make_fleets_list_response()
    groups_resp = _make_groups_list_response()
    batch_resp = _make_fleet_get_values_response(variable, fill=0.1)
    sent_path = None
    sent_content = None

    async def mock_send(cmd, params=None, timeout=30):
        nonlocal sent_path, sent_content
        if cmd == "list_ecopath_fleets":
            return fleets_resp
        if cmd == "list_ecopath_groups":
            return groups_resp
        if cmd == "batch":
            return batch_resp
        if cmd == "write_host_file":
            sent_path = params.get("path")
            sent_content = params.get("content")
            return {"status": "success"}
        return {}

    with patch("ewe_mcp.server._send", side_effect=mock_send):
        from ewe_mcp.server import export_fleet_indexed_csv
        result = await export_fleet_indexed_csv(variable=variable, path="C:\\tmp\\fleet_price.csv")

    assert result["written_to_host"] is True
    assert sent_path == "C:\\tmp\\fleet_price.csv"
    assert "Fleet" in sent_content


# ── Fleet Indexed Value Matrix Import Tests ─────────────────────────


@pytest.mark.asyncio
async def test_import_fleet_indexed_csv_basic():
    """Import parses matrix CSV and sets indexed values for non-zero cells."""
    variable = "off_vessel_price"
    csv_data = "Fleet,Phytoplankton,Zooplankton,Snook,Detritus\nGillnet,5.0,2.5,0.0,0.0\nTrawl,0.0,4.0,1.0,0.0\n"
    captured_cmds = None

    async def mock_send(cmd, params=None, timeout=30):
        nonlocal captured_cmds
        if cmd == "list_ecopath_fleets":
            return _make_fleets_list_response()
        if cmd == "list_ecopath_groups":
            return _make_groups_list_response()
        if cmd == "batch":
            captured_cmds = (params or {}).get("commands", [])
            return {"results": [{"status": "success"} for _ in captured_cmds]}
        return {}

    with patch("ewe_mcp.server._send", side_effect=mock_send):
        from ewe_mcp.server import import_fleet_indexed_csv
        result = await import_fleet_indexed_csv(variable=variable, data=csv_data)

    assert result["status"] == "success"
    assert result["variable"] == variable
    assert result["n_sets_requested"] == 4  # 5.0, 2.5, 4.0, 1.0 — only non-zero
    assert result["n_applied"] == 4

    # Verify batch commands structure
    assert captured_cmds is not None
    assert len(captured_cmds) == 4

    cmd0 = captured_cmds[0]
    assert cmd0["type"] == "set_indexed_value"
    assert cmd0["params"]["domain"] == "ecopath_fleet"
    assert cmd0["params"]["index"] == 1  # Gillnet
    assert cmd0["params"]["variable"] == variable
    assert cmd0["params"]["value"] == 5.0

    cmd1 = captured_cmds[1]
    assert cmd1["params"]["indices"] == [2]  # Zooplankton
    assert cmd1["params"]["value"] == 2.5


@pytest.mark.asyncio
async def test_import_fleet_indexed_csv_invalid_variable():
    """Rejects unsupported variable name."""
    from ewe_mcp.server import import_fleet_indexed_csv
    with pytest.raises(ValueError, match="Unsupported variable"):
        await import_fleet_indexed_csv(variable="bad_var", data="Fleet,Group\nF1,1.0\n")


# ── Detritus Fate CSV Export Tests ────────────────────────────────────


@pytest.mark.asyncio
async def test_export_detritus_fate_csv_basic():
    """Export returns CSV with detritus fate fractions."""
    groups_resp = _make_groups_list_response()
    batch_resp = _make_fate_get_values_responses()

    async def mock_send(cmd, params=None, timeout=30):
        if cmd == "list_ecopath_groups":
            return groups_resp
        if cmd == "batch":
            return batch_resp
        return {}

    with patch("ewe_mcp.server._send", side_effect=mock_send):
        from ewe_mcp.server import export_detritus_fate_csv
        result = await export_detritus_fate_csv()

    assert result["status"] == "success"
    assert result["group_count"] == 3  # 3 non-detritus groups
    assert result["detritus_count"] == 1  # 1 detritus group

    raw = result["csv"].strip()
    csv_lines = [line.rstrip("\r") for line in raw.split("\n")]
    assert len(csv_lines) == 4  # header + 3 groups

    headers = csv_lines[0].split(",")
    assert headers[0] == "Group"
    assert "Detritus" in headers[1]

    assert csv_lines[1].startswith("Phytoplankton,")
    assert csv_lines[2].startswith("Zooplankton,")
    assert csv_lines[3].startswith("Snook,")


@pytest.mark.asyncio
async def test_export_detritus_fate_csv_with_path():
    """Export writes to host file when path is provided."""
    groups_resp = _make_groups_list_response()
    batch_resp = _make_fate_get_values_responses()
    sent_path = None
    sent_content = None

    async def mock_send(cmd, params=None, timeout=30):
        nonlocal sent_path, sent_content
        if cmd == "list_ecopath_groups":
            return groups_resp
        if cmd == "batch":
            return batch_resp
        if cmd == "write_host_file":
            sent_path = params.get("path")
            sent_content = params.get("content")
            return {"status": "success"}
        return {}

    with patch("ewe_mcp.server._send", side_effect=mock_send):
        from ewe_mcp.server import export_detritus_fate_csv
        result = await export_detritus_fate_csv(path="C:\\tmp\\detritus_fate.csv")

    assert result["written_to_host"] is True
    assert sent_path == "C:\\tmp\\detritus_fate.csv"
    assert "Group" in sent_content


# ── Detritus Fate CSV Import Tests ────────────────────────────────────


@pytest.mark.asyncio
async def test_import_detritus_fate_csv_basic():
    """Import parses CSV and sets indexed values for non-zero fractions."""
    csv_data = "Group,Detritus\nPhytoplankton,0.85\nZooplankton,0.55\nSnook,0.35\n"
    captured_cmds = None

    async def mock_send(cmd, params=None, timeout=30):
        nonlocal captured_cmds
        if cmd == "list_ecopath_groups":
            return _make_groups_list_response()
        if cmd == "batch":
            captured_cmds = (params or {}).get("commands", [])
            return {"results": [{"status": "success"} for _ in captured_cmds]}
        return {}

    with patch("ewe_mcp.server._send", side_effect=mock_send):
        from ewe_mcp.server import import_detritus_fate_csv
        result = await import_detritus_fate_csv(data=csv_data)

    assert result["status"] == "success"
    assert result["n_sets_requested"] == 3
    assert result["n_applied"] == 3

    assert captured_cmds is not None
    assert len(captured_cmds) == 3

    cmd0 = captured_cmds[0]
    assert cmd0["type"] == "set_indexed_value"
    assert cmd0["params"]["domain"] == "ecopath_group"
    assert cmd0["params"]["variable"] == "detritus_fate"
    assert cmd0["params"]["value"] == 0.85


@pytest.mark.asyncio
async def test_import_detritus_fate_csv_invalid_header():
    """Rejects CSV without 'Group' as first column."""
    csv_data = "Name,Detritus\nPhytoplankton,0.5\n"

    with patch("ewe_mcp.server._send", new=AsyncMock()):
        from ewe_mcp.server import import_detritus_fate_csv
        with pytest.raises(ValueError, match="CSV header must start with 'Group'"):
            await import_detritus_fate_csv(data=csv_data)
