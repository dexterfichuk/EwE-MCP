import os
import time
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


@live
def test_ecospace_run_and_stop(live_client, spatial_model_path):
    live_client.command_result("load_model", {"path": spatial_model_path})
    live_client.command_result("run_ecopath")
    live_client.command_result("load_ecosim_scenario", {"index": 1})
    live_client.command_result("load_ecospace_scenario", {"index": 1})
    try:
        result = live_client.command_result("run_ecospace")
        assert result.get("started") is True
        state = live_client.command_result("get_ecospace_run_state")
        assert "has_ecospace_loaded" in state
    finally:
        live_client.command_result("stop_ecospace")


@live
def test_ecospace_run_state_after_load(live_client, spatial_model_path):
    live_client.command_result("load_model", {"path": spatial_model_path})
    live_client.command_result("run_ecopath")
    live_client.command_result("load_ecosim_scenario", {"index": 1})
    live_client.command_result("load_ecospace_scenario", {"index": 1})
    state = live_client.command_result("get_ecospace_run_state")
    assert state.get("has_ecospace_loaded") is True
    assert state.get("has_ecospace_initialized") is True


@live
def test_ecospace_run_to_completion_with_results(live_client, spatial_model_path):
    live_client.command_result("load_model", {"path": spatial_model_path})
    live_client.command_result("run_ecopath")
    live_client.command_result("load_ecosim_scenario", {"index": 1})
    live_client.command_result("load_ecospace_scenario", {"index": 1})
    try:
        result = live_client.command_result("run_ecospace")
        assert result.get("started") is True

        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            state = live_client.command_result("get_ecospace_run_state")
            if not state.get("is_ecospace_running") and state.get("has_ecospace_ran"):
                break
            time.sleep(2)
        else:
            raise RuntimeError(
                f"Ecospace did not complete within 300s. Last state: {state}"
            )

        assert state["has_ecospace_ran"] is True

        groups = live_client.command_result(
            "list_entities", {"domain": "ecospace_group_inputs"}
        )
        first_group_index = 1
        results = live_client.command_result(
            "get_ecospace_group_results",
            {"index": first_group_index, "variables": ["biomass"]},
        )
        assert "series" in results
        assert "biomass" in results["series"]
        assert len(results["series"]["biomass"]) > 0
    finally:
        live_client.command_result("stop_ecospace")
