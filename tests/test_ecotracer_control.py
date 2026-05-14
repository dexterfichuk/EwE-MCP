import os
import pytest
from ewe_mcp.helpers import BATCH_BLOCKED_COMMANDS

live = pytest.mark.skipif(
    os.environ.get("EWE_LIVE_TESTS") != "1",
    reason="Requires live EwE MCP plugin (set EWE_LIVE_TESTS=1 to run)",
)


def test_ecotracer_destructive_commands_blocked_from_batch():
    for cmd in [
        "create_ecotracer_scenario",
        "copy_ecotracer_scenario",
        "delete_ecotracer_scenario",
        "save_ecotracer_scenario",
        "close_ecotracer_scenario",
    ]:
        assert cmd in BATCH_BLOCKED_COMMANDS, f"{cmd} should be batch-blocked"


@live
def test_ecotracer_scenario_list(live_client, sample_model_path):
    live_client.command_result("load_model", {"path": sample_model_path})
    scenarios = live_client.command_result(
        "list_entities", {"domain": "ecotracer_scenarios"}
    )
    if scenarios["total_count"] < 1:
        pytest.skip("No Ecotracer scenarios in sample model")


@live
def test_ecotracer_run_state(live_client, sample_model_path):
    live_client.command_result("load_model", {"path": sample_model_path})
    live_client.command_result("load_ecosim_scenario", {"index": 1})
    scenarios = live_client.command_result(
        "list_entities", {"domain": "ecotracer_scenarios"}
    )
    if scenarios["total_count"] < 1:
        pytest.skip("No Ecotracer scenarios in sample model")
    live_client.command_result("load_ecotracer_scenario", {"index": 1})
    state = live_client.command_result("get_ecotracer_run_state")
    assert "has_ecotracer_loaded" in state


@live
def test_ecotracer_model_parameters(live_client, sample_model_path):
    live_client.command_result("load_model", {"path": sample_model_path})
    live_client.command_result("load_ecosim_scenario", {"index": 1})
    scenarios = live_client.command_result(
        "list_entities", {"domain": "ecotracer_scenarios"}
    )
    if scenarios["total_count"] < 1:
        pytest.skip("No Ecotracer scenarios in sample model")
    live_client.command_result("load_ecotracer_scenario", {"index": 1})
    params = live_client.command_result("get_ecotracer_model_parameters")
    assert "values" in params


@live
def test_ecotracer_group_inputs(live_client, sample_model_path):
    live_client.command_result("load_model", {"path": sample_model_path})
    live_client.command_result("load_ecosim_scenario", {"index": 1})
    scenarios = live_client.command_result(
        "list_entities", {"domain": "ecotracer_scenarios"}
    )
    if scenarios["total_count"] < 1:
        pytest.skip("No Ecotracer scenarios in sample model")
    live_client.command_result("load_ecotracer_scenario", {"index": 1})
    groups = live_client.command_result(
        "list_entities", {"domain": "ecotracer_group_inputs"}
    )
    assert groups["total_count"] > 0
