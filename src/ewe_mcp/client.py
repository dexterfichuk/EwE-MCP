"""Socket client for the in-process EwE MCP plugin."""

from __future__ import annotations

import json
import logging
import socket
import uuid
from typing import Any

from ewe_mcp.helpers import (
    HEADER_STRUCT,
    RECV_CHUNK_SIZE,
    TIMEOUT_DEFAULT,
    get_host,
    get_max_response_size,
    get_port,
    get_token,
)

logger = logging.getLogger("ewe_mcp.client")
LARGE_RESPONSE_WARN_RATIO = 0.1


class EwEMCPError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message


class ResponseTooLargeError(ConnectionError):
    def __init__(self, size: int, limit: int):
        self.size = size
        self.limit = limit
        super().__init__(f"Response too large: {size} bytes (limit {limit} bytes)")


class EwEMCPClient:
    def __init__(
        self, host: str | None = None, port: int | None = None, token: str | None = None
    ):
        self.host = host or get_host()
        self.port = port or get_port()
        self.token = token or get_token()
        self.socket: socket.socket | None = None
        self._current_timeout: int | float | None = None
        self._notifications: list[dict[str, Any]] = []

    def connect(self) -> bool:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.connect((self.host, self.port))
            self.socket = sock
            self._current_timeout = None
            return True
        except OSError:
            self.socket = None
            self._current_timeout = None
            return False

    def disconnect(self) -> None:
        if self.socket is not None:
            self.socket.close()
            self.socket = None
            self._current_timeout = None

    @property
    def notifications(self) -> list[dict[str, Any]]:
        """Return pending dialog/event notifications and clear the queue."""
        result = list(self._notifications)
        self._notifications.clear()
        return result

    def clear_notifications(self) -> None:
        self._notifications.clear()

    def _set_timeout(self, timeout: int | float | None) -> None:
        if self.socket is None:
            raise ConnectionError("Not connected to EwE MCP plugin")
        if self._current_timeout != timeout:
            self.socket.settimeout(timeout)
            self._current_timeout = timeout

    def _recv_exact(self, n: int, max_size: int | None = None) -> bytes:
        """Read exactly n bytes, disconnecting first if the frame would desync the protocol."""
        if self.socket is None:
            raise ConnectionError("Not connected to EwE MCP plugin")
        if max_size is None:
            max_size = get_max_response_size()
        if n > max_size:
            self.disconnect()
            raise ResponseTooLargeError(n, max_size)
        buf = bytearray(n)
        view = memoryview(buf)
        pos = 0
        while pos < n:
            read = self.socket.recv_into(view[pos:], min(n - pos, RECV_CHUNK_SIZE))
            if read == 0:
                raise ConnectionError("Connection closed by EwE MCP plugin")
            pos += read
        return bytes(buf)

    def send_command(
        self,
        command_type: str,
        params: dict[str, Any] | None = None,
        timeout: int = TIMEOUT_DEFAULT,
    ) -> dict[str, Any]:
        if self.socket is None:
            if not self.connect():
                raise ConnectionError("Failed to connect to EwE MCP plugin")

        request = {
            "id": uuid.uuid4().hex,
            "type": command_type,
            "params": params or {},
            "token": self.token,
            "timeout": timeout,
        }
        payload = json.dumps(request, separators=(",", ":")).encode("utf-8")
        try:
            self.socket.sendall(HEADER_STRUCT.pack(len(payload)))
            self.socket.sendall(payload)
        except OSError:
            self.disconnect()
            raise ConnectionError("Connection lost during send") from None

        self._set_timeout(timeout)
        try:
            max_response_size = get_max_response_size()
            header = self._recv_exact(4, max_response_size)
            length = HEADER_STRUCT.unpack(header)[0]
            data = self._recv_exact(length, max_response_size)
            if length > int(max_response_size * LARGE_RESPONSE_WARN_RATIO):
                logger.warning(
                    "Large response from EwE plugin: %d bytes for command %s",
                    length,
                    command_type,
                )
            self._set_timeout(None)
            if not data.strip():
                raise ConnectionError("Received empty response from EwE MCP plugin")
            response = json.loads(data.decode("utf-8"))
            if "_events" in response:
                self._notifications.extend(response.pop("_events"))
            return response
        except socket.timeout:
            # If the socket times out, it is poisoned with an unread response.
            # We must disconnect to force a fresh connection on the next call.
            self.disconnect()
            raise socket.timeout(
                f"EwE MCP command '{command_type}' timed out after {timeout}s. "
                "This may indicate EwE is showing a modal dialog. "
                "Try enabling dialog_watcher_enabled or increasing the timeout."
            ) from None

    def command_result(
        self,
        command_type: str,
        params: dict[str, Any] | None = None,
        timeout: int = TIMEOUT_DEFAULT,
    ) -> dict[str, Any]:
        response = self.send_command(command_type, params, timeout=timeout)
        if response.get("status") == "error":
            error = response.get("error") or {}
            raise EwEMCPError(
                error.get("code") or "unknown_error",
                error.get("message") or "EwE MCP command failed",
            )
        return response.get("result") or {}

    def get_dialog_events(self) -> list[dict[str, Any]]:
        """Fetch pending dialog events. Sends a no-op ping to trigger piggyback delivery."""
        try:
            self.send_command("ping", timeout=5)
        except (ConnectionError, OSError, socket.timeout) as exc:
            logger.debug("get_dialog_events ping failed: %s", exc)
        return self.notifications
