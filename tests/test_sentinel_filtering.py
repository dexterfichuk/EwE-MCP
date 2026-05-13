import os

import pytest

live = pytest.mark.skipif(
    os.environ.get("EWE_LIVE_TESTS") != "1",
    reason="Requires live EwE MCP plugin (set EWE_LIVE_TESTS=1 to run)",
)


@live
@pytest.mark.asyncio
async def test_sentinel_filtering_ecopath_group_i_stanza(sample_model_path):
    from ewe_mcp.server import get_ecopath_group, load_model

    await load_model(sample_model_path)
    group = await get_ecopath_group(index=1)
    assert "input" in group
    assert group["input"]["i_stanza"] is None, (
        f"Expected i_stanza to be None (filtered sentinel), got {group['input']['i_stanza']}"
    )


@live
@pytest.mark.asyncio
async def test_sentinel_filtering_diagnose_check(live_client):
    from ewe_mcp.helpers import enrich_diagnose

    raw = live_client.command_result("diagnose")
    result = enrich_diagnose(raw)
    checks = result.get("checks", [])
    sentinel_check = next(
        (c for c in checks if c["name"] == "sentinel_filtering"), None
    )
    assert sentinel_check is not None, "No sentinel_filtering check in diagnose"
    assert sentinel_check["status"] == "ok"
    assert sentinel_check["detail"]["enabled"] is True


@live
@pytest.mark.asyncio
async def test_sentinel_filtering_ecosim_group_i_stanza(sample_model_path):
    from ewe_mcp.server import (
        get_ecosim_group_input,
        load_ecosim_scenario,
        load_model,
    )

    await load_model(sample_model_path)
    await load_ecosim_scenario(index=1)
    group = await get_ecosim_group_input(index=1)
    assert "i_stanza" in group
    assert group["i_stanza"] is None, (
        f"Expected i_stanza to be None (filtered sentinel), got {group['i_stanza']}"
    )
