import os

import pytest

from ewe_mcp.client import EwEMCPError
from ewe_mcp.server import (
    get_indexed_value,
    load_model,
    set_indexed_value,
    validate_indexed_value,
)

live = pytest.mark.skipif(
    os.environ.get("EWE_LIVE_TESTS") != "1",
    reason="Requires live EwE MCP plugin (set EWE_LIVE_TESTS=1 to run)",
)

SAMPLE_MODEL = os.environ.get(
    "EWE_MODEL_PATH",
    r"C:\Users\dexter\Eii.Ecopath\Database\Tampa_Bay.EwEmdb",
)


async def _ensure_model_loaded():
    res = await load_model(SAMPLE_MODEL)
    assert res["loaded"] is True, f"Failed to load sample model: {SAMPLE_MODEL}"


@live
@pytest.mark.asyncio
async def test_indexed_diet_comp_round_trip():
    await _ensure_model_loaded()
    params = {"domain": "ecopath_group", "index": 20, "variable": "diet_comp", "indices": [25]}
    original = (await get_indexed_value(**params))["value"]

    validation = await validate_indexed_value(**params, value=original)
    assert validation["valid"] is True
    assert validation["property"] == "DietComp"

    try:
        updated = original + 0.001
        result = await set_indexed_value(**params, value=updated)
        assert result["result"] == "updated"
        assert (await get_indexed_value(**params))["value"] == pytest.approx(updated)
    finally:
        await set_indexed_value(**params, value=original)


@live
@pytest.mark.asyncio
async def test_indexed_fleet_landings_round_trip():
    await _ensure_model_loaded()
    params = {"domain": "ecopath_fleet", "index": 1, "variable": "landings", "indices": [18]}
    original = (await get_indexed_value(**params))["value"]

    validation = await validate_indexed_value(**params, value=original)
    assert validation["valid"] is True
    assert validation["property"] == "Landings"

    try:
        updated = original + 0.01
        result = await set_indexed_value(**params, value=updated)
        assert result["result"] == "updated"
        assert (await get_indexed_value(**params))["value"] == pytest.approx(updated)
    finally:
        await set_indexed_value(**params, value=original)


@live
@pytest.mark.asyncio
async def test_indexed_unknown_variable_errors():
    await _ensure_model_loaded()
    params = {"domain": "ecopath_group", "index": 20, "variable": "not_a_real_value", "indices": [25]}

    with pytest.raises(EwEMCPError) as excinfo:
        await get_indexed_value(**params)
    assert excinfo.value.code == "unknown_indexed_variable"

    validation = await validate_indexed_value(**params, value=1.0)
    assert validation["valid"] is False
    assert validation["reason"] == "unknown_or_readonly_indexed_variable"


@live
@pytest.mark.asyncio
async def test_indexed_empty_indices_error():
    await _ensure_model_loaded()
    with pytest.raises(EwEMCPError) as excinfo:
        await get_indexed_value(domain="ecopath_group", index=20, variable="diet_comp", indices=[])
    assert excinfo.value.code == "invalid_parameter"


@live
@pytest.mark.asyncio
async def test_indexed_bad_conversion_error():
    await _ensure_model_loaded()
    params = {"domain": "ecopath_group", "index": 20, "variable": "diet_comp", "indices": [25]}

    validation = await validate_indexed_value(**params, value="definitely-not-a-number")
    assert validation["valid"] is False
    assert validation["reason"] == "conversion_failed"

    with pytest.raises(EwEMCPError) as excinfo:
        await set_indexed_value(**params, value="definitely-not-a-number")
    assert excinfo.value.code == "conversion_failed"


@live
@pytest.mark.asyncio
async def test_indexed_target_index_out_of_range_error():
    await _ensure_model_loaded()
    with pytest.raises(EwEMCPError) as excinfo:
        await get_indexed_value(domain="ecopath_group", index=9999, variable="diet_comp", indices=[25])
    assert excinfo.value.code == "index_out_of_range"


@live
@pytest.mark.asyncio
async def test_indexed_cell_index_out_of_range_error():
    await _ensure_model_loaded()
    with pytest.raises(EwEMCPError) as excinfo:
        await get_indexed_value(domain="ecopath_group", index=20, variable="diet_comp", indices=[9999])
    assert excinfo.value.code == "indexed_index_out_of_range"

    validation = await validate_indexed_value(
        domain="ecopath_group",
        index=20,
        variable="diet_comp",
        indices=[9999],
        value=0.1,
    )
    assert validation["valid"] is False
    assert validation["reason"].startswith("Index 9999 is outside valid group range")
