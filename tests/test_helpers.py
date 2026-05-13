import pytest

from ewe_mcp.helpers import BATCH_BLOCKED_COMMANDS, DEFAULT_MAX_RESPONSE_SIZE, error_hint, get_max_response_size


def test_error_hints_cover_common_failures():
    assert "EwE is running" in error_hint("Connection refused")
    assert "EWE_MCP_TOKEN" in error_hint("unauthorized token")
    assert "list_ui_commands" in error_hint("command not found")


def test_batch_blocks_dangerous_commands():
    assert "execute_ewe_code" in BATCH_BLOCKED_COMMANDS
    assert "close_model" in BATCH_BLOCKED_COMMANDS


def test_max_response_size_default():
    assert DEFAULT_MAX_RESPONSE_SIZE == 10 * 1024 * 1024


def test_max_response_size_env_override(monkeypatch):
    get_max_response_size.cache_clear()
    monkeypatch.setenv("EWE_MCP_MAX_RESPONSE_SIZE", "5242880")
    try:
        assert get_max_response_size() == 5 * 1024 * 1024
    finally:
        get_max_response_size.cache_clear()


def test_max_response_size_invalid(monkeypatch):
    get_max_response_size.cache_clear()
    monkeypatch.setenv("EWE_MCP_MAX_RESPONSE_SIZE", "not_a_number")
    try:
        with pytest.raises(ValueError, match="must be an integer"):
            get_max_response_size()
    finally:
        get_max_response_size.cache_clear()


def test_max_response_size_negative(monkeypatch):
    get_max_response_size.cache_clear()
    monkeypatch.setenv("EWE_MCP_MAX_RESPONSE_SIZE", "-1")
    try:
        with pytest.raises(ValueError, match="must be positive"):
            get_max_response_size()
    finally:
        get_max_response_size.cache_clear()


def test_error_hint_oversized_response():
    hint = error_hint("Response too large: 50000000 bytes (limit 10485760 bytes)")
    assert "size limit" in hint
    assert "smaller subsets" in hint


def test_error_hint_dialog_command_blocked():
    hint = error_hint("dialog_command_blocked: ImportTimeSeries")
    assert "modal dialog" in hint
    assert "dialog_watcher_enabled" in hint
