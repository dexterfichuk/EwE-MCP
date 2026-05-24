import os
import uuid
from pathlib import PureWindowsPath

import pytest

from ewe_mcp.server import (
    create_ecopath_group,
    discard_changes,
    get_entity,
    get_indexed_value,
    load_model,
    save_model,
    save_model_as,
    set_indexed_value,
)


live = pytest.mark.skipif(
    os.environ.get("EWE_LIVE_TESTS") != "1",
    reason="Requires live EwE MCP plugin (set EWE_LIVE_TESTS=1 to run)",
)

GROUP = "DietPTest"


@live
@pytest.mark.asyncio
async def test_diet_comp_write_live(sample_model_path):
    """set_indexed_value writes diet_comp in-memory immediately."""
    await load_model(sample_model_path)
    cp = str(PureWindowsPath(sample_model_path).with_name(f"live_{uuid.uuid4().hex[:8]}.EwEmdb"))
    try:
        await save_model_as(cp)
        c = await create_ecopath_group(name=GROUP, group_type=0)
        p = {"domain": "ecopath_group", "index": c["group_index"],
             "variable": "diet_comp", "indices": [1]}
        r = await set_indexed_value(**p, value=0.5)
        assert r["result"] == "updated"
        assert (await get_indexed_value(**p))["value"] == pytest.approx(0.5)
    finally:
        await load_model(sample_model_path)
        await discard_changes()


@live
@pytest.mark.asyncio
async def test_diet_comp_reload_workflow(sample_model_path):
    """
    Workflow for new groups:
    set → save → load → re-set → save → load → verify
    The load after save syncs DC arrays from DB (which has SQL-written value).
    The re-set writes to correctly-sized arrays.
    """
    await load_model(sample_model_path)
    cp = str(PureWindowsPath(sample_model_path).with_name(f"rl_{uuid.uuid4().hex[:8]}.EwEmdb"))
    try:
        await save_model_as(cp)
        c = await create_ecopath_group(name=GROUP, group_type=0)
        p = {"domain": "ecopath_group", "index": c["group_index"],
             "variable": "diet_comp", "indices": [1]}

        await set_indexed_value(**p, value=0.5)
        await save_model()
        await load_model(cp)

        g = await get_entity(domain="ecopath_group", index=c["group_index"])
        assert g["name"] == GROUP, f"Group at index {c['group_index']} is '{g['name']}'"

        r = await set_indexed_value(**p, value=0.5)
        assert r["result"] == "updated"

        await save_model()
        await load_model(cp)

        assert (await get_indexed_value(**p))["value"] == pytest.approx(0.5)
    finally:
        await load_model(sample_model_path)
        await discard_changes()
