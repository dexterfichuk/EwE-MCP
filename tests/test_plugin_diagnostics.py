import os
import pytest

live = pytest.mark.skipif(
    os.environ.get("EWE_LIVE_TESTS") != "1",
    reason="Requires live EwE MCP plugin (set EWE_LIVE_TESTS=1 to run)",
)


@live
def test_get_installed_plugins(live_client, sample_model_path):
    live_client.command_result("load_model", {"path": sample_model_path})
    result = live_client.command_result("get_installed_plugins")
    assert "plugins" in result
    assert "total_count" in result