import os

import pytest

from ewe_mcp.client import EwEMCPError
from ewe_mcp.server import (
    discard_changes,
    get_ecopath_group,
    get_ecopath_group_diagnostics,
    get_ecopath_stats,
    get_ecospace_basemap,
    get_ecospace_model_parameters,
    get_ecospace_run_state,
    get_ecosim_group_input,
    get_ecosim_run_state,
    get_ecotracer_model_parameters,
    get_ecotracer_run_state,
    get_monte_carlo_parameters,
    get_monte_carlo_run_state,
    get_mse_parameters,
    get_mse_run_state,
    list_ecopath_groups,
    list_ecospace_group_inputs,
    list_ecospace_scenarios,
    list_ecosim_group_inputs,
    list_ecosim_scenarios,
    list_ecotracer_group_inputs,
    list_ecotracer_scenarios,
    list_monte_carlo_groups,
    list_mse_group_inputs,
    load_ecosim_scenario,
    load_ecospace_scenario,
    load_ecotracer_scenario,
    load_model,
    run_ecopath,
    set_values,
)

live = pytest.mark.skipif(
    os.environ.get("EWE_LIVE_TESTS") != "1",
    reason="Requires live EwE MCP plugin (set EWE_LIVE_TESTS=1 to run)",
)


@live
@pytest.mark.asyncio
async def test_ecopath_read_only_workflow(sample_model_path):
    await load_model(sample_model_path)

    groups = await list_ecopath_groups()
    assert "groups" in groups
    assert groups["total_count"] > 0

    group = await get_ecopath_group(1)
    assert "index" in group
    assert "name" in group

    await run_ecopath()

    stats = await get_ecopath_stats()
    assert "available" in stats
    assert "values" in stats

    diag = await get_ecopath_group_diagnostics()
    assert "groups" in diag
    assert diag["total_count"] > 0


@live
@pytest.mark.asyncio
async def test_ecopath_safe_mutation_restore(sample_model_path):
    await load_model(sample_model_path)

    idx = 1
    original = await get_ecopath_group(idx)
    original_name = original["name"]

    try:
        await set_values(
            domain="ecopath_group",
            index=idx,
            values={"name": "TEST_SMOKE"},
            navigate=False,
        )

        changed = await get_ecopath_group(idx)
        assert changed["name"] == "TEST_SMOKE"
    finally:
        await set_values(
            domain="ecopath_group",
            index=idx,
            values={"name": original_name},
            navigate=False,
        )
        await discard_changes()


@live
@pytest.mark.asyncio
async def test_ecosim_read_only_workflow(sample_model_path):
    await load_model(sample_model_path)

    scenarios = await list_ecosim_scenarios()
    assert "total_count" in scenarios
    assert scenarios["total_count"] >= 1

    active = scenarios.get("active_index") or 1
    await load_ecosim_scenario(active)

    state = await get_ecosim_run_state()
    assert "has_ecosim_loaded" in state

    groups = await list_ecosim_group_inputs()
    assert groups["total_count"] > 0

    group = await get_ecosim_group_input(1)
    assert group["index"] == 1
    assert "values" in group


@live
@pytest.mark.asyncio
async def test_ecosim_safe_mutation_restore(sample_model_path):
    await load_model(sample_model_path)

    scenarios = await list_ecosim_scenarios()
    active = scenarios.get("active_index") or 1
    await load_ecosim_scenario(active)

    idx = 1
    original = await get_ecosim_group_input(idx)
    original_value = original["values"].get("max_rel_feeding_time")

    try:
        await set_values(
            domain="ecosim_group_input",
            index=idx,
            values={"max_rel_feeding_time": 0.5},
            navigate=False,
        )

        changed = await get_ecosim_group_input(idx)
        assert changed["values"]["max_rel_feeding_time"] == 0.5
    finally:
        await set_values(
            domain="ecosim_group_input",
            index=idx,
            values={"max_rel_feeding_time": original_value},
            navigate=False,
        )
        await discard_changes()


@live
@pytest.mark.asyncio
async def test_ecospace_read_only_workflow(sample_model_path):
    await load_model(sample_model_path)

    scenarios = await list_ecospace_scenarios()
    if scenarios.get("total_count", 0) < 1:
        pytest.skip("No Ecospace scenarios in sample model")

    load_result = await load_ecospace_scenario(1)
    if not load_result.get("loaded"):
        pytest.skip("Ecospace scenario 1 did not load (known EwE limitation)")

    state = await get_ecospace_run_state()
    assert "has_ecospace_loaded" in state

    groups = await list_ecospace_group_inputs()
    assert groups["total_count"] > 0

    basemap = await get_ecospace_basemap()
    assert "rows" in basemap
    assert "cols" in basemap

    params = await get_ecospace_model_parameters()
    assert isinstance(params, dict) and len(params) > 0


@live
@pytest.mark.asyncio
async def test_ecotracer_read_only_workflow(sample_model_path):
    await load_model(sample_model_path)

    scenarios = await list_ecotracer_scenarios()
    if scenarios.get("total_count", 0) < 1:
        pytest.skip("No Ecotracer scenarios in sample model")

    await load_ecotracer_scenario(1)

    state = await get_ecotracer_run_state()
    assert "has_ecotracer_loaded" in state

    params = await get_ecotracer_model_parameters()
    assert "values" in params

    groups = await list_ecotracer_group_inputs()
    assert groups["total_count"] > 0


@live
@pytest.mark.asyncio
async def test_mse_read_only_workflow(sample_model_path):
    await load_model(sample_model_path)
    await load_ecosim_scenario(1)

    try:
        params = await get_mse_parameters()
        assert isinstance(params, dict) and len(params) > 0
    except EwEMCPError as e:
        if e.code == "core_unavailable":
            pytest.skip("MSE not available in this model configuration")
        raise

    groups = await list_mse_group_inputs()
    assert groups["total_count"] > 0

    state = await get_mse_run_state()
    assert "mse_available" in state


@live
@pytest.mark.asyncio
async def test_monte_carlo_read_only_workflow(sample_model_path):
    await load_model(sample_model_path)
    await load_ecosim_scenario(1)

    try:
        params = await get_monte_carlo_parameters()
        assert isinstance(params, dict) and len(params) > 0
    except EwEMCPError as e:
        if e.code == "core_unavailable":
            pytest.skip("Monte Carlo not available in this model configuration")
        raise

    groups = await list_monte_carlo_groups()
    assert groups["total_count"] > 0

    state = await get_monte_carlo_run_state()
    assert "has_monte_carlo_loaded" in state or "monte_carlo_available" in state
