"""Tests for _dialog_events deduplication and capping in _send.

Bug: _dialog_events accumulates indefinitely, causing "Response too large: 2GB"
errors when there's a persistent dialog loop.
Fix: Cap to most recent N unique dialog events and deduplicate by (title, text).
"""

import pytest

from unittest.mock import patch, MagicMock


@pytest.mark.asyncio
async def test_dialog_events_deduplicated():
    """Duplicate dialog events should be collapsed with a count field."""
    from ewe_mcp import server as server_mod

    dup_events = [
        {"title": "Warning", "text": "Unsaved changes", "is_risky": True},
        {"title": "Warning", "text": "Unsaved changes", "is_risky": True},
        {"title": "Info", "text": "Loading complete", "is_risky": False},
    ]

    # Mock the send_sync path
    original_send_sync = server_mod._send_sync

    call_count = 0

    def mock_send_sync(command, params=None, timeout=30):
        nonlocal call_count
        call_count += 1
        if command == "ping":
            return {"pong": True}
        return {"result": "ok"}

    # Mock the connection to return our events from notifications
    mock_conn = MagicMock()
    mock_conn.notifications = dup_events
    mock_conn.socket = MagicMock()
    mock_conn.socket.getpeername.return_value = ("127.0.0.1", 12345)

    try:
        server_mod._send_sync = mock_send_sync

        with patch.object(server_mod, "_get_connection", return_value=mock_conn):
            with patch.object(server_mod, "_send_lock", new=MagicMock()):
                # Create a simple async context manager for the lock
                class FakeLock:
                    async def __aenter__(self):
                        return None
                    async def __aexit__(self, *args):
                        return None

                with patch.object(server_mod, "_send_lock", FakeLock()):
                    result = await server_mod._send("ping")

    finally:
        server_mod._send_sync = original_send_sync

    # The result should have _dialog_events
    assert "_dialog_events" in result, (
        f"Expected _dialog_events in result. Keys: {list(result.keys())}"
    )

    events = result["_dialog_events"]
    # Should have 2 unique events (Warning collapsed, Info)
    assert len(events) == 2, (
        f"Expected 2 unique events (Warning deduped, Info). Got {len(events)}: {events}"
    )

    # Find the warning event — it should have a count
    warning_events = [e for e in events if e.get("title") == "Warning"]
    assert len(warning_events) == 1, (
        f"Expected exactly 1 Warning event after dedup. Got {len(warning_events)}"
    )

    warning = warning_events[0]
    assert "count" in warning, (
        f"Warning event should have a 'count' field. Got keys: {list(warning.keys())}"
    )
    assert warning["count"] >= 2, (
        f"Warning event count should be >= 2. Got: {warning['count']}"
    )


@pytest.mark.asyncio
async def test_dialog_events_capped():
    """When more than 10 unique dialog events exist, only the 10 most recent should be returned."""
    from ewe_mcp import server as server_mod

    # Create 15 unique dialog events
    many_events = [
        {"title": f"Dialog {i}", "text": f"Event number {i}", "is_risky": False}
        for i in range(15)
    ]

    original_send_sync = server_mod._send_sync

    def mock_send_sync(command, params=None, timeout=30):
        if command == "ping":
            return {"pong": True}
        return {"result": "ok"}

    mock_conn = MagicMock()
    mock_conn.notifications = many_events
    mock_conn.socket = MagicMock()
    mock_conn.socket.getpeername.return_value = ("127.0.0.1", 12345)

    try:
        server_mod._send_sync = mock_send_sync

        class FakeLock:
            async def __aenter__(self):
                return None
            async def __aexit__(self, *args):
                return None

        with patch.object(server_mod, "_get_connection", return_value=mock_conn):
            with patch.object(server_mod, "_send_lock", FakeLock()):
                result = await server_mod._send("ping")
    finally:
        server_mod._send_sync = original_send_sync

    events = result.get("_dialog_events", [])
    # Should be capped at 10
    assert len(events) <= 10, (
        f"Expected at most 10 dialog events (capped). Got {len(events)}"
    )

    # The last 10 events (indices 5-14) should be the ones returned
    # since they're the most recent
    if len(events) == 10:
        last_titles = [e.get("title", "") for e in events]
        expected_last = [f"Dialog {i}" for i in range(5, 15)]
        assert last_titles == expected_last, (
            f"Expected last 10 events to be Dialogs 5-14. Got: {last_titles}"
        )


@pytest.mark.asyncio
async def test_dialog_events_empty_when_no_notifications():
    """When no dialog events exist, _dialog_events should not be in the result."""
    from ewe_mcp import server as server_mod

    original_send_sync = server_mod._send_sync

    def mock_send_sync(command, params=None, timeout=30):
        return {"pong": True}

    mock_conn = MagicMock()
    mock_conn.notifications = []
    mock_conn.socket = MagicMock()
    mock_conn.socket.getpeername.return_value = ("127.0.0.1", 12345)

    try:
        server_mod._send_sync = mock_send_sync

        class FakeLock:
            async def __aenter__(self):
                return None
            async def __aexit__(self, *args):
                return None

        with patch.object(server_mod, "_get_connection", return_value=mock_conn):
            with patch.object(server_mod, "_send_lock", FakeLock()):
                result = await server_mod._send("ping")
    finally:
        server_mod._send_sync = original_send_sync

    # _dialog_events should not be present when there are no events
    assert "_dialog_events" not in result, (
        f"Expected no _dialog_events key when no notifications. "
        f"Got keys: {list(result.keys())}"
    )
