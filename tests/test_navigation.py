import os

import pytest

from ewe_mcp.server import get_active_view, list_navigation_nodes, load_model, navigate_to_node, set_values

live = pytest.mark.skipif(
    os.environ.get("EWE_LIVE_TESTS") != "1",
    reason="Requires live EwE MCP plugin (set EWE_LIVE_TESTS=1 to run)",
)

SAMPLE_MODEL = os.environ.get(
    "EWE_MODEL_PATH",
    r"C:\Users\dexter\Eii.Ecopath\Database\Tampa_Bay.EwEmdb",
)


@pytest.mark.asyncio
async def test_navigate_to_node_requires_target():
    with pytest.raises(ValueError, match="Provide one of"):
        await navigate_to_node()


@live
@pytest.mark.asyncio
async def test_list_and_navigate_to_basic_input():
    await load_model(SAMPLE_MODEL)

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
async def test_set_values_can_navigate_to_edit_screen():
    await load_model(SAMPLE_MODEL)

    result = await set_values(domain="ecopath_group", index=1, values={"name": "0-12 Snook"}, navigate=True)
    assert result["results"]["name"] == "updated"
    assert result["navigation"]["suggested_node_id"] == "ndBasicInput"
