from ewe_mcp.helpers import BATCH_BLOCKED_COMMANDS, error_hint


def test_error_hints_cover_common_failures():
    assert "EwE is running" in error_hint("Connection refused")
    assert "EWE_MCP_TOKEN" in error_hint("unauthorized token")
    assert "list_ui_commands" in error_hint("command not found")


def test_batch_blocks_dangerous_commands():
    assert "execute_ewe_code" in BATCH_BLOCKED_COMMANDS
    assert "close_model" in BATCH_BLOCKED_COMMANDS
