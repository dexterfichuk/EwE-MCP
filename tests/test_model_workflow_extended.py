import contextlib
import os
import uuid

import pytest

from ewe_mcp.server import (
    close_model,
    discard_changes,
    get_ecopath_group,
    get_model_info,
    has_changes,
    list_ecopath_fleets,
    list_ecopath_groups,
    load_model,
    save_model_as,
    set_values,
)

live = pytest.mark.skipif(
    os.environ.get("EWE_LIVE_TESTS") != "1",
    reason="Requires live EwE MCP plugin (set EWE_LIVE_TESTS=1 to run)",
)


@live
@pytest.mark.asyncio
async def test_load_inspect_groups_and_fleets(sample_model_path):
    await load_model(sample_model_path)

    groups = await list_ecopath_groups()
    assert groups["total_count"] > 0
    assert len(groups["groups"]) > 0

    fleets = await list_ecopath_fleets()
    assert fleets["total_count"] > 0
    assert len(fleets["fleets"]) > 0


@live
@pytest.mark.asyncio
async def test_save_as_copy_close_reload(sample_model_path):
    await load_model(sample_model_path)
    info = await get_model_info()
    assert info["loaded"] is True

    vm_temp = os.environ.get(
        "EWE_VM_TEMP",
        rf"C:\Users\{os.environ.get('EWE_VM_USER', 'dexter')}\AppData\Local\Temp",
    )
    vm_temp = vm_temp.rstrip("\\/")
    temp_path = rf"{vm_temp}\ewe_mcp_test_copy_{uuid.uuid4().hex}.EwEmdb"
    try:
        await save_model_as(temp_path)
        await close_model()
        info_closed = await get_model_info()
        assert info_closed["loaded"] is False

        await load_model(temp_path)
        info_reloaded = await get_model_info()
        assert info_reloaded["loaded"] is True
    finally:
        with contextlib.suppress(Exception):
            await close_model()


@live
@pytest.mark.asyncio
async def test_has_changes_after_mutation_and_discard(sample_model_path):
    await load_model(sample_model_path)

    original_group = await get_ecopath_group(1)
    original_name = original_group["name"]

    original = await has_changes()
    assert original["has_changes"] is False

    try:
        await set_values(
            domain="ecopath_group",
            index=1,
            values={"name": "Test Mutation"},
            navigate=False,
        )
        dirty = await has_changes()
        assert dirty["has_changes"] is True

    finally:
        try:
            await set_values(
                domain="ecopath_group",
                index=1,
                values={"name": original_name},
                navigate=False,
            )
        finally:
            await discard_changes()

    clean = await has_changes()
    assert clean["has_changes"] is False


@live
@pytest.mark.asyncio
async def test_close_model_and_verify_no_model(sample_model_path):
    await load_model(sample_model_path)
    info = await get_model_info()
    assert info["loaded"] is True

    await close_model()
    info_closed = await get_model_info()
    assert info_closed["loaded"] is False


@live
@pytest.mark.asyncio
async def test_load_model_idempotent(sample_model_path):
    await load_model(sample_model_path)
    info1 = await get_model_info()
    assert info1["loaded"] is True

    await load_model(sample_model_path)
    info2 = await get_model_info()
    assert info2["loaded"] is True

    assert info1["metadata"]["Name"] == info2["metadata"]["Name"]
