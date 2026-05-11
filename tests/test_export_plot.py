import os

import pytest

from ewe_mcp.server import export_active_plot, load_model, navigate_to_node

live = pytest.mark.skipif(
    os.environ.get("EWE_LIVE_TESTS") != "1",
    reason="Requires live EwE MCP plugin (set EWE_LIVE_TESTS=1 to run)",
)


# ── Offline tests ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_export_active_plot_not_batch_blocked():
    """export_active_plot is read-only and should NOT be in batch-blocked commands."""
    from ewe_mcp.helpers import BATCH_BLOCKED_COMMANDS

    assert "export_active_plot" not in BATCH_BLOCKED_COMMANDS


# ── Live tests ─────────────────────────────────────────────────────


@live
@pytest.mark.asyncio
async def test_export_active_plot_no_chart(sample_model_path):
    """When no chart is visible, export_active_plot should return found=false with suggestion."""
    await load_model(sample_model_path)

    await navigate_to_node(id="ndBasicInput")

    result = await export_active_plot()

    assert "found" in result
    if not result.get("found"):
        assert "suggestion" in result


@live
@pytest.mark.asyncio
async def test_export_active_plot_after_ecosim_run(sample_model_path):
    """After running Ecosim, navigate to output plots and extract chart data."""
    from ewe_mcp.server import (
        list_ecosim_scenarios,
        load_ecosim_scenario,
        run_ecosim,
        wait_for_ecosim_completion,
    )

    await load_model(sample_model_path)

    scenarios = await list_ecosim_scenarios()
    if not scenarios.get("scenarios"):
        pytest.skip("No Ecosim scenarios available")

    first_scenario = scenarios["scenarios"][0]
    await load_ecosim_scenario(index=first_scenario["index"])

    await run_ecosim()
    await wait_for_ecosim_completion(timeout_seconds=120)

    await navigate_to_node(id="ndEcosimPlots")

    result = await export_active_plot(target="main")

    assert result["found"] is True
    assert result["series_count"] > 0
    assert result["pane_count"] > 0
    assert isinstance(result["series"], list)
    assert any(series["point_count"] > 0 for series in result["series"])
