import os

import pytest

from ewe_mcp.server import (
    discard_changes,
    get_active_view,
    get_indexed_value,
    get_values,
    list_navigation_nodes,
    load_model,
    navigate_to_node,
    set_values,
)

live = pytest.mark.skipif(
    os.environ.get("EWE_LIVE_TESTS") != "1",
    reason="Requires live EwE MCP plugin (set EWE_LIVE_TESTS=1 to run)",
)


@pytest.mark.asyncio
async def test_navigate_to_node_requires_target(sample_model_path):
    with pytest.raises(ValueError, match="Provide one of"):
        await navigate_to_node()


@live
@pytest.mark.asyncio
async def test_list_and_navigate_to_basic_input(sample_model_path):
    await load_model(sample_model_path)

    nodes = await list_navigation_nodes()
    node_ids = {node["id"] for node in nodes["nodes"]}
    assert "ndBasicInput" in node_ids

    result = await navigate_to_node(id="ndBasicInput")
    assert result["navigated"] is True
    assert result["selected"]["id"] == "ndBasicInput"

    active = await get_active_view()
    assert active["selected"]["id"] == "ndBasicInput"


@live
@pytest.mark.asyncio
async def test_set_values_can_navigate_to_edit_screen(sample_model_path):
    await load_model(sample_model_path)

    result = await set_values(
        domain="ecopath_group", index=1, values={"name": "0-12 Snook"}
    )
    assert result["results"]["name"] == "updated"
    assert result["navigation"]["suggested_node_id"] == "ndBasicInput"
    await discard_changes()


@live
@pytest.mark.asyncio
async def test_value_reads_navigate_by_default_and_can_skip(sample_model_path):
    await load_model(sample_model_path)

    values = await get_values(domain="ecopath_group", index=1, variables=["name"])
    assert values["navigation"]["suggested_node_id"] == "ndBasicInput"

    indexed = await get_indexed_value(
        domain="ecopath_group", index=20, variable="diet_comp", indices=[25]
    )
    assert indexed["navigation"]["suggested_node_id"] == "ndDietComposition"

    quiet = await get_values(
        domain="ecopath_group", index=1, variables=["name"], navigate=False
    )
    assert "navigation" not in quiet
