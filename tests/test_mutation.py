import pytest
import asyncio
import os
from ewe_mcp.server import set_values, get_ecopath_group, list_ecopath_fleets, execute_ewe_code, load_model

@pytest.mark.asyncio
async def test_set_values_group():
    # Ensure model is loaded
    model_path = os.environ.get("EWE_MODEL_PATH", r"C:\Users\dexter\Eii.Ecopath\Database\Tampa_Bay.EwEmdb")
    await load_model(model_path)
    
    idx = 1
    # Test setting name
    res = await set_values(domain="ecopath_group", index=idx, values={"name": "New Name"})
    assert res["results"]["name"] == "updated"
    
    group = await get_ecopath_group(idx)
    assert group["name"] == "New Name"
    
    # Restore original name
    await set_values(domain="ecopath_group", index=idx, values={"name": "0-12 Snook"})

@pytest.mark.asyncio
async def test_set_values_fleet():
    idx = 1
    res = await set_values(domain="ecopath_fleet", index=idx, values={"name": "New Fleet Name"})
    assert res["results"]["name"] == "updated"
    
    fleets = await list_ecopath_fleets()
    fleet = next(f for f in fleets["fleets"] if f["index"] == idx)
    assert fleet["name"] == "New Fleet Name"
    
    # Restore original name
    await set_values(domain="ecopath_fleet", index=idx, values={"name": "Gillnet / Trawl"})

@pytest.mark.asyncio
async def test_execute_ewe_code_disabled():
    with pytest.raises(RuntimeError, match="execute_ewe_code is disabled"):
        await execute_ewe_code(code="Core.nGroups")
