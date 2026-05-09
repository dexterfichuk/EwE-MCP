import os
import pytest
from ewe_mcp.client import EwEMCPError
from ewe_mcp.helpers import BATCH_BLOCKED_COMMANDS

live = pytest.mark.skipif(
    os.environ.get("EWE_LIVE_TESTS") != "1",
    reason="Requires live EwE MCP plugin (set EWE_LIVE_TESTS=1 to run)",
)


def test_ecospace_destructive_commands_blocked_from_batch():
    for cmd in [
        "create_ecospace_scenario",
        "copy_ecospace_scenario",
        "delete_ecospace_scenario",
        "save_ecospace_scenario",
        "run_ecospace",
        "stop_ecospace",
    ]:
        assert cmd in BATCH_BLOCKED_COMMANDS, f"{cmd} should be batch-blocked"


@live
def test_ecospace_scenario_list(live_client, sample_model_path):
    live_client.command_result("load_model", {"path": sample_model_path})
    scenarios = live_client.command_result(
        "list_entities", {"domain": "ecospace_scenarios"}
    )
    assert scenarios["total_count"] >= 1


@live
def test_ecospace_scenario_load(live_client, sample_model_path):
    live_client.command_result("load_model", {"path": sample_model_path})
    live_client.command_result("load_ecosim_scenario", {"index": 1})
    loaded = live_client.command_result("load_ecospace_scenario", {"index": 1})
    assert loaded["loaded"] is True


@live
def test_ecospace_run_state(live_client, sample_model_path):
    live_client.command_result("load_model", {"path": sample_model_path})
    live_client.command_result("load_ecosim_scenario", {"index": 1})
    live_client.command_result("load_ecospace_scenario", {"index": 1})
    state = live_client.command_result("get_ecospace_run_state")
    assert "has_ecospace_loaded" in state


@live
def test_ecospace_group_inputs(live_client, sample_model_path):
    live_client.command_result("load_model", {"path": sample_model_path})
    live_client.command_result("load_ecosim_scenario", {"index": 1})
    live_client.command_result("load_ecospace_scenario", {"index": 1})
    groups = live_client.command_result(
        "list_entities", {"domain": "ecospace_group_inputs"}
    )
    assert groups["total_count"] > 0


@live
def test_ecospace_basemap(live_client, sample_model_path):
    live_client.command_result("load_model", {"path": sample_model_path})
    live_client.command_result("load_ecosim_scenario", {"index": 1})
    live_client.command_result("load_ecospace_scenario", {"index": 1})
    basemap = live_client.command_result("get_ecospace_basemap")
    assert "rows" in basemap
    assert "cols" in basemap


@live
def test_ecospace_model_parameters(live_client, sample_model_path):
    live_client.command_result("load_model", {"path": sample_model_path})
    live_client.command_result("load_ecosim_scenario", {"index": 1})
    live_client.command_result("load_ecospace_scenario", {"index": 1})
    params = live_client.command_result("get_ecospace_model_parameters")
    assert isinstance(params, dict) and len(params) > 0