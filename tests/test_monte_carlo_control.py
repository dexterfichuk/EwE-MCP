import os
import pytest
from ewe_mcp.client import EwEMCPError
from ewe_mcp.helpers import BATCH_BLOCKED_COMMANDS

live = pytest.mark.skipif(
    os.environ.get("EWE_LIVE_TESTS") != "1",
    reason="Requires live EwE MCP plugin (set EWE_LIVE_TESTS=1 to run)",
)


def test_monte_carlo_destructive_commands_blocked_from_batch():
    assert "run_monte_carlo" in BATCH_BLOCKED_COMMANDS
    assert "stop_monte_carlo" in BATCH_BLOCKED_COMMANDS


@live
def test_monte_carlo_parameters(live_client, sample_model_path):
    live_client.command_result("load_model", {"path": sample_model_path})
    live_client.command_result("load_ecosim_scenario", {"index": 1})
    try:
        result = live_client.command_result("get_monte_carlo_parameters")
        assert isinstance(result, dict) and len(result) > 0
    except EwEMCPError as e:
        if e.code == "core_unavailable":
            pytest.skip("Monte Carlo not available in this model configuration")
        raise


@live
def test_monte_carlo_run_state(live_client, sample_model_path):
    live_client.command_result("load_model", {"path": sample_model_path})
    live_client.command_result("load_ecosim_scenario", {"index": 1})
    try:
        result = live_client.command_result("get_monte_carlo_run_state")
        assert "has_monte_carlo_loaded" in result or "monte_carlo_available" in result
    except EwEMCPError as e:
        if e.code == "core_unavailable":
            pytest.skip("Monte Carlo not available in this model configuration")
        raise
