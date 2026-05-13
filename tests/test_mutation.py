import os

import pytest

from ewe_mcp.server import (
    discard_changes,
    execute_ewe_code,
    get_ecopath_group,
    list_ecopath_fleets,
    load_model,
    set_values,
)

# Tests that require a running EwE plugin on the Windows VM.
live = pytest.mark.skipif(
    os.environ.get("EWE_LIVE_TESTS") != "1",
    reason="Requires live EwE MCP plugin (set EWE_LIVE_TESTS=1 to run)",
)


@live
@pytest.mark.asyncio
async def test_set_values_group(sample_model_path):
    # Ensure model is loaded
    await load_model(sample_model_path)

    idx = 1
    try:
        res = await set_values(
            domain="ecopath_group", index=idx, values={"name": "New Name"}
        )
        assert res["results"]["name"] == "updated"

        group = await get_ecopath_group(idx)
        assert group["name"] == "New Name"
    finally:
        await set_values(
            domain="ecopath_group", index=idx, values={"name": "0-12 Snook"}
        )
        await discard_changes()


@live
@pytest.mark.asyncio
async def test_set_values_fleet(sample_model_path):
    await load_model(sample_model_path)

    idx = 1
    try:
        res = await set_values(
            domain="ecopath_fleet", index=idx, values={"name": "New Fleet Name"}
        )
        assert res["results"]["name"] == "updated"

        fleets = await list_ecopath_fleets()
        fleet = next(f for f in fleets["fleets"] if f["index"] == idx)
        assert fleet["name"] == "New Fleet Name"
    finally:
        await set_values(
            domain="ecopath_fleet", index=idx, values={"name": "Gillnet / Trawl"}
        )
        await discard_changes()


@live
@pytest.mark.asyncio
async def test_execute_ewe_code_disabled():
    with pytest.raises(RuntimeError, match="execute_ewe_code is disabled"):
        await execute_ewe_code(code="Core.nGroups")
