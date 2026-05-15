import os

import pytest

from ewe_mcp.helpers import BATCH_BLOCKED_COMMANDS

live = pytest.mark.skipif(
    os.environ.get("EWE_LIVE_TESTS") != "1",
    reason="Requires live EwE MCP plugin (set EWE_LIVE_TESTS=1 to run)",
)


def test_set_values_range_is_batch_blocked():
    assert "set_values_range" in BATCH_BLOCKED_COMMANDS


def test_search_entities_not_batch_blocked():
    assert "search_entities" not in BATCH_BLOCKED_COMMANDS


def test_get_values_range_not_batch_blocked():
    assert "get_values_range" not in BATCH_BLOCKED_COMMANDS


@live
@pytest.mark.asyncio
async def test_search_entities_by_name(sample_model_path):
    from ewe_mcp.server import load_model, search_entities

    await load_model(sample_model_path)
    result = await search_entities(domain="ecopath_groups", name_contains="Snook")
    assert result["match_count"] >= 1
    assert all(
        "Snook" in e.get("name", "").lower() or "snook" in e.get("name", "")
        for e in result["results"]
    )


@live
@pytest.mark.asyncio
async def test_search_entities_no_filter(sample_model_path):
    from ewe_mcp.server import load_model, search_entities

    await load_model(sample_model_path)
    result = await search_entities(domain="ecopath_groups")
    assert result["match_count"] == result["total_count"]
    assert result["total_count"] > 0


@live
@pytest.mark.asyncio
async def test_search_entities_property_filter(sample_model_path):
    from ewe_mcp.server import load_model, search_entities

    await load_model(sample_model_path)
    result = await search_entities(
        domain="ecopath_groups",
        name_contains="Snook",
        property_filters={"is_living": "True"},
    )
    assert result["match_count"] >= 1


@live
@pytest.mark.asyncio
async def test_search_entities_max_results(sample_model_path):
    from ewe_mcp.server import load_model, search_entities

    await load_model(sample_model_path)
    result = await search_entities(domain="ecopath_groups", max_results=2)
    assert result["match_count"] <= 2


@live
@pytest.mark.asyncio
async def test_search_entities_unknown_domain():
    from ewe_mcp.server import search_entities

    with pytest.raises(Exception, match="unknown_domain"):
        await search_entities(domain="nonexistent_domain")


@live
@pytest.mark.asyncio
async def test_get_values_range_basic(sample_model_path):
    from ewe_mcp.server import get_values_range, load_model

    await load_model(sample_model_path)
    result = await get_values_range(
        domain="ecopath_groups",
        variables=["name", "biomass_area"],
        start_index=1,
        end_index=3,
    )
    assert result["entity_count"] == 3
    assert result["start_index"] == 1
    assert result["truncated"] is False
    entities = result["entities"]
    assert len(entities) == 3
    for e in entities:
        assert "index" in e
        assert "values" in e


@live
@pytest.mark.asyncio
async def test_get_values_range_max_entities(sample_model_path):
    from ewe_mcp.server import get_values_range, load_model

    await load_model(sample_model_path)
    result = await get_values_range(
        domain="ecopath_groups",
        variables=["name"],
        max_entities=2,
    )
    assert result["entity_count"] == 2
    assert result["truncated"] is True


@live
@pytest.mark.asyncio
async def test_get_values_range_default_no_navigate(sample_model_path):
    from ewe_mcp.server import get_values_range, load_model

    await load_model(sample_model_path)
    result = await get_values_range(
        domain="ecopath_groups",
        variables=["name"],
        start_index=1,
        end_index=1,
    )
    assert result["entity_count"] == 1
    assert "navigation" not in result["entities"][0]


@live
@pytest.mark.asyncio
async def test_set_values_range_restore(sample_model_path):
    from ewe_mcp.server import (
        discard_changes,
        get_values_range,
        load_model,
        set_values_range,
    )

    await load_model(sample_model_path)

    original = await get_values_range(
        domain="ecopath_groups",
        variables=["remark"],
        start_index=1,
        end_index=2,
    )
    original_remarks = [e["values"].get("remark", "") for e in original["entities"]]

    try:
        result = await set_values_range(
            domain="ecopath_groups",
            values={"remark": "TEST_RANGE"},
            start_index=1,
            end_index=2,
        )
        assert result["entity_count"] == 2
        for e in result["entities"]:
            assert e["results"]["remark"] == "updated"

        after = await get_values_range(
            domain="ecopath_groups",
            variables=["remark"],
            start_index=1,
            end_index=2,
        )
        for e in after["entities"]:
            assert e["values"]["remark"] == "TEST_RANGE"
    finally:
        for i, remark in enumerate(original_remarks, start=1):
            from ewe_mcp.server import set_values

            await set_values(
                domain="ecopath_group",
                index=i,
                values={"remark": remark if remark else ""},
                navigate=False,
            )
        await discard_changes()
