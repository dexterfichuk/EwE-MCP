import pytest

from ewe_mcp.server import (
    balance_model,
    compare_scenarios,
    export_report,
    run_ecosim_scenario,
)


# ── Offline tests ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_balance_model_prompt_structure():
    """balance_model prompt should return a list of messages with the path included."""
    result = await balance_model(path=r"C:\test\model.EwEmdb")
    assert isinstance(result, list)
    assert len(result) >= 1
    msg = result[0]
    assert "role" in msg
    assert msg["role"] == "user"
    assert r"C:\test\model.EwEmdb" in msg["content"] or repr(r"C:\test\model.EwEmdb") in msg["content"]
    assert "run_ecopath" in msg["content"]


@pytest.mark.asyncio
async def test_run_ecosim_scenario_prompt_structure():
    """run_ecosim_scenario prompt should reference the scenario index."""
    result = await run_ecosim_scenario(path=r"C:\test\model.EwEmdb", scenario_index=2)
    assert isinstance(result, list)
    msg = result[0]
    assert "2" in msg["content"]
    assert "run_ecosim" in msg["content"]


@pytest.mark.asyncio
async def test_compare_scenarios_prompt_structure():
    """compare_scenarios prompt should include the indices string."""
    result = await compare_scenarios(path=r"C:\test\model.EwEmdb", indices="1,3")
    assert isinstance(result, list)
    msg = result[0]
    assert "1,3" in msg["content"]
    assert "scenario" in msg["content"].lower()


@pytest.mark.asyncio
async def test_export_report_prompt_structure():
    """export_report prompt should reference export and model inspection tools."""
    result = await export_report(path=r"C:\test\model.EwEmdb")
    assert isinstance(result, list)
    msg = result[0]
    assert r"C:\test\model.EwEmdb" in msg["content"] or repr(r"C:\test\model.EwEmdb") in msg["content"]
    assert "export_ecopath_results" in msg["content"]