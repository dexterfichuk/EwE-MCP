import os

import pytest

from ewe_mcp.server import (
    load_model,
    validate_values,
    validate_indexed_value,
    run_ecopath,
)

live = pytest.mark.skipif(
    os.environ.get("EWE_LIVE_TESTS") != "1",
    reason="Requires live EwE MCP plugin (set EWE_LIVE_TESTS=1 to run)",
)


async def _ensure_model_loaded_and_balanced(sample_model_path):
    await load_model(sample_model_path)
    await run_ecopath()


@live
@pytest.mark.asyncio
async def test_validate_values_rejects_den_dep_catchability_below_minimum(
    sample_model_path,
):
    await _ensure_model_loaded_and_balanced(sample_model_path)
    result = await validate_values(
        domain="ecosim_group_inputs",
        index=25,
        values={"den_dep_catchability": 0},
    )
    den_dep_result = result["results"].get("den_dep_catchability")
    assert den_dep_result is not None
    assert den_dep_result["valid"] is False
    # Inclusive min (>=) for den_dep_catchability
    assert ">= 1" in den_dep_result["reason"]


@live
@pytest.mark.asyncio
async def test_validate_values_rejects_den_dep_catchability_half(sample_model_path):
    await _ensure_model_loaded_and_balanced(sample_model_path)
    result = await validate_values(
        domain="ecosim_group_inputs",
        index=25,
        values={"den_dep_catchability": 0.5},
    )
    assert result["results"]["den_dep_catchability"]["valid"] is False


@live
@pytest.mark.asyncio
async def test_validate_values_accepts_den_dep_catchability_at_minimum(
    sample_model_path,
):
    await _ensure_model_loaded_and_balanced(sample_model_path)
    result = await validate_values(
        domain="ecosim_group_inputs",
        index=25,
        values={"den_dep_catchability": 1.0},
    )
    assert result["results"]["den_dep_catchability"]["valid"] is True


@live
@pytest.mark.asyncio
async def test_validate_indexed_value_rejects_rel_q_below_minimum(sample_model_path):
    await _ensure_model_loaded_and_balanced(sample_model_path)
    result = await validate_indexed_value(
        domain="ecosim_fleet_inputs",
        variable="rel_q",
        indices=[25],
        index=2,
        value=0,
    )
    assert result["valid"] is False
    assert "must be >= 1" in result.get("reason", "")


@live
@pytest.mark.asyncio
async def test_validate_indexed_value_accepts_rel_q_at_minimum(sample_model_path):
    await _ensure_model_loaded_and_balanced(sample_model_path)
    result = await validate_indexed_value(
        domain="ecosim_fleet_inputs",
        variable="rel_q",
        indices=[25],
        index=2,
        value=1.0,
    )
    assert result["valid"] is True
