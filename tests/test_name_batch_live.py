import os

import pytest

from ewe_mcp.server import (
    discard_changes,
    get_values,
    load_model,
    set_values,
    set_values_by_name_batch,
)

live = pytest.mark.skipif(
    os.environ.get("EWE_LIVE_TESTS") != "1",
    reason="Requires live EwE MCP plugin (set EWE_LIVE_TESTS=1 to run)",
)


@live
@pytest.mark.asyncio
async def test_set_values_by_name_batch_mixed_domain_roundtrip(sample_model_path):
    await load_model(sample_model_path)

    original_group = await get_values(
        domain="ecopath_group", index=1, variables=["name"], navigate=False
    )
    original_fleet = await get_values(
        domain="ecopath_fleet", index=1, variables=["name"], navigate=False
    )

    group_name = original_group["values"]["name"]
    fleet_name = original_fleet["values"]["name"]

    updated_group_name = f"{group_name} (tmp)"
    updated_fleet_name = f"{fleet_name} (tmp)"

    try:
        result = await set_values_by_name_batch(
            operations=[
                {
                    "domain": "ecopath_group",
                    "name": group_name,
                    "values": {"name": updated_group_name},
                    "navigate": False,
                },
                {
                    "domain": "ecopath_fleet",
                    "name": fleet_name,
                    "values": {"name": updated_fleet_name},
                    "navigate": False,
                },
            ]
        )

        assert result["status"] in {"success", "partial_success"}
        assert result["summary"]["applied"] == 2
    finally:
        await set_values(
            domain="ecopath_group",
            index=1,
            values={"name": group_name},
            navigate=False,
        )
        await set_values(
            domain="ecopath_fleet",
            index=1,
            values={"name": fleet_name},
            navigate=False,
        )
        await discard_changes()


@live
@pytest.mark.asyncio
async def test_set_values_by_name_batch_not_found_returns_suggestions(sample_model_path):
    await load_model(sample_model_path)
    result = await set_values_by_name_batch(
        operations=[
            {
                "domain": "ecopath_group",
                "name": "definitely-not-a-real-group-name",
                "values": {"pb": 1.1},
            }
        ]
    )
    assert result["status"] == "failed"
    assert result["results"][0]["status"] == "not_found"
    assert "suggestions" in result["results"][0]
