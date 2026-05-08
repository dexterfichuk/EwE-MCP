import os
import asyncio

import pytest

from ewe_mcp.client import EwEMCPError
from ewe_mcp.helpers import BATCH_BLOCKED_COMMANDS
from ewe_mcp.server import (
    batch_commands,
    get_ecosim_fleet_input,
    get_ecosim_fleet_results,
    get_ecosim_group_input,
    get_ecosim_group_results,
    get_ecosim_run_state,
    get_ecosim_scenario,
    list_ecosim_fleet_inputs,
    list_ecosim_group_inputs,
    list_ecosim_scenarios,
    load_ecosim_scenario,
    load_model,
    run_ecosim,
)

live = pytest.mark.skipif(
    os.environ.get("EWE_LIVE_TESTS") != "1",
    reason="Requires live EwE MCP plugin (set EWE_LIVE_TESTS=1 to run)",
)




async def _ensure_ecosim_loaded(sample_model_path):
    model = await load_model(sample_model_path)
    assert model["loaded"] is True
    scenarios = await list_ecosim_scenarios()
    assert scenarios["total_count"] >= 1
    active = scenarios.get("active_index") or 1
    loaded = await load_ecosim_scenario(active)
    assert loaded["loaded"] is True
    return active


async def _wait_for_ecosim_completed(timeout_seconds=60):
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    last_state = {}
    while asyncio.get_running_loop().time() < deadline:
        last_state = await get_ecosim_run_state()
        if last_state.get("has_ecosim_ran"):
            return last_state
        await asyncio.sleep(1)
    pytest.fail(f"Ecosim did not complete within {timeout_seconds}s; last state: {last_state}")


@pytest.mark.asyncio
async def test_ecosim_destructive_scenario_commands_blocked_from_batch(sample_model_path):
    assert "create_ecosim_scenario" in BATCH_BLOCKED_COMMANDS
    assert "copy_ecosim_scenario" in BATCH_BLOCKED_COMMANDS
    assert "delete_ecosim_scenario" in BATCH_BLOCKED_COMMANDS
    with pytest.raises(ValueError, match="is not allowed in batch"):
        await batch_commands([{"type": "delete_ecosim_scenario", "params": {"index": 2}}])


@live
@pytest.mark.asyncio
async def test_ecosim_scenario_list_and_load(sample_model_path):
    active = await _ensure_ecosim_loaded(sample_model_path)

    scenario = await get_ecosim_scenario(active)
    assert scenario["index"] == active
    assert scenario["is_loaded"] is True

    state = await get_ecosim_run_state()
    assert state["has_ecosim_loaded"] is True
    assert state["active_scenario_index"] == active


@live
@pytest.mark.asyncio
async def test_ecosim_inputs_available_after_load(sample_model_path):
    await _ensure_ecosim_loaded(sample_model_path)

    groups = await list_ecosim_group_inputs()
    assert groups["total_count"] > 0
    group = await get_ecosim_group_input(1)
    assert group["index"] == 1
    assert "values" in group

    fleets = await list_ecosim_fleet_inputs()
    assert fleets["total_count"] > 0
    fleet = await get_ecosim_fleet_input(1)
    assert fleet["index"] == 1
    assert "values" in fleet


@live
@pytest.mark.asyncio
async def test_ecosim_run_and_fetch_results(sample_model_path):
    await _ensure_ecosim_loaded(sample_model_path)

    run = await run_ecosim()
    assert run["started"] is True

    state = await _wait_for_ecosim_completed()
    assert state["has_ecosim_ran"] is True

    group_results = await get_ecosim_group_results(
        index=1,
        variables=["biomass", "catch"],
        start_time=1,
        end_time=3,
    )
    assert len(group_results["series"]["biomass"]) == 3
    assert len(group_results["series"]["catch"]) == 3

    fleet_results = await get_ecosim_fleet_results(
        index=1,
        variables=["catch_biomass"],
        start_time=1,
        end_time=3,
    )
    assert len(fleet_results["series"]["catch_biomass"]) == 3


@live
@pytest.mark.asyncio
async def test_ecosim_result_bounds_error(sample_model_path):
    await _ensure_ecosim_loaded(sample_model_path)
    await run_ecosim()
    await _wait_for_ecosim_completed()

    with pytest.raises(EwEMCPError) as excinfo:
        await get_ecosim_group_results(index=1, start_time=0, end_time=1)
    assert excinfo.value.code == "time_range_out_of_range"
