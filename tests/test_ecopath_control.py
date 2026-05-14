import os
import pytest

live = pytest.mark.skipif(
    os.environ.get("EWE_LIVE_TESTS") != "1",
    reason="Requires live EwE MCP plugin (set EWE_LIVE_TESTS=1 to run)",
)


def test_ecopath_commands_not_in_batch_blocked():
    from ewe_mcp.helpers import BATCH_BLOCKED_COMMANDS

    for cmd in [
        "get_ecopath_run_state",
        "get_ecopath_stats",
        "get_ecopath_group_diagnostics",
        "get_ecopath_fleet_diagnostics",
        "get_mti_matrix",
    ]:
        assert cmd not in BATCH_BLOCKED_COMMANDS, (
            f"{cmd} should not be in BATCH_BLOCKED_COMMANDS"
        )
    assert "export_ecopath_results" in BATCH_BLOCKED_COMMANDS


def test_run_ecopath_returns_ecopath_detail_field():
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
    assert "ecopath_detail" in text, (
        "RunEcopath and GetEcopathRunState must return ecopath_detail field"
    )


@live
def test_ecopath_run_state_after_load(live_client, sample_model_path):
    live_client.command_result("load_model", {"path": sample_model_path})
    state = live_client.command_result("get_ecopath_run_state")
    assert "has_ecopath_loaded" in state
    assert "has_ecopath_ran" in state
    assert "is_model_balanced" in state
    assert "ecopath_run_state" in state
    assert "queried_at" in state


@live
def test_ecopath_run_state_after_run(live_client, sample_model_path):
    live_client.command_result("load_model", {"path": sample_model_path})
    run = live_client.command_result("run_ecopath")
    assert run["started"] is True
    assert "is_model_balanced" in run
    assert "ecopath_run_state" in run
    assert run["ecopath_run_state"] in ("balanced", "unbalanced"), (
        f"ecopath_run_state should be 'balanced' or 'unbalanced' after run, got {run['ecopath_run_state']!r}"
    )
    assert "ecopath_detail" in run

    state = live_client.command_result("get_ecopath_run_state")
    assert state["has_ecopath_ran"] is True
    assert "is_model_balanced" in state
    assert "ecopath_run_state" in state
    assert state["ecopath_run_state"] in ("balanced", "unbalanced"), (
        f"ecopath_run_state should be 'balanced' or 'unbalanced' after run, got {state['ecopath_run_state']!r}"
    )
    assert "ecopath_detail" in state


@live
def test_ecopath_stats_after_run(live_client, sample_model_path):
    live_client.command_result("load_model", {"path": sample_model_path})
    live_client.command_result("run_ecopath")
    stats = live_client.command_result("get_ecopath_stats")
    assert stats["available"] is True
    assert "values" in stats
    vals = stats["values"]
    assert len(vals) > 0


@live
def test_ecopath_group_diagnostics_after_run(live_client, sample_model_path):
    live_client.command_result("load_model", {"path": sample_model_path})
    live_client.command_result("run_ecopath")
    diag = live_client.command_result("get_ecopath_group_diagnostics")
    assert "groups" in diag
    assert diag["total_count"] > 0
    first = diag["groups"][0]
    assert "index" in first
    assert "name" in first
    assert "values" in first


@live
def test_ecopath_fleet_diagnostics_after_run(live_client, sample_model_path):
    live_client.command_result("load_model", {"path": sample_model_path})
    live_client.command_result("run_ecopath")
    diag = live_client.command_result("get_ecopath_fleet_diagnostics")
    assert "fleets" in diag
    assert diag["total_count"] > 0
    first_fleet = diag["fleets"][0]
    assert "index" in first_fleet
    assert "name" in first_fleet
    assert "catch_by_group" in first_fleet


@live
def test_export_ecopath_results(live_client, sample_model_path):
    live_client.command_result("load_model", {"path": sample_model_path})
    live_client.command_result("run_ecopath")
    export_path = "C:/Users/dexter/ewe_mcp_test_export.csv"
    result = live_client.command_result("export_ecopath_results", {"path": export_path})
    assert result["exported"] is True
    assert "path" in result
    assert "method" in result
