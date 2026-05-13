import os

import pytest

from ewe_mcp.server import get_ecopath_group, load_model, run_ecopath

live = pytest.mark.skipif(
    os.environ.get("EWE_LIVE_TESTS") != "1",
    reason="Requires live EwE MCP plugin (set EWE_LIVE_TESTS=1 to run)",
)


@live
@pytest.mark.asyncio
async def test_get_ecopath_group_includes_computed_fields_after_balance(
    sample_model_path,
):
    await load_model(sample_model_path)
    await run_ecopath()
    group = await get_ecopath_group(index=1)
    assert "output" in group
    output = group["output"]
    assert "biomass" in output
    assert "ee" in output, (
        f"Expected computed EE in output, got keys: {list(output.keys())}"
    )
    assert "trophic_level" in output, (
        f"Expected computed trophic level in output, got keys: {list(output.keys())}"
    )


@live
@pytest.mark.asyncio
async def test_get_ecopath_group_computed_fields_match_diagnostics(sample_model_path):
    await load_model(sample_model_path)
    await run_ecopath()
    from ewe_mcp.server import get_ecopath_group_diagnostics

    group = await get_ecopath_group(index=3)
    diag = await get_ecopath_group_diagnostics()
    diag_group3 = next(g for g in diag["groups"] if g["index"] == 3)
    assert abs(group["output"]["ee"] - diag_group3["values"]["EEOutput"]) < 0.001
    assert abs(group["output"]["trophic_level"] - diag_group3["values"]["TTLX"]) < 0.001
