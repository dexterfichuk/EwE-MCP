"""Socket client for the in-process EwE MCP plugin."""

from __future__ import annotations

import json
import socket
import uuid
from typing import Any

from ewe_mcp.helpers import (
    HEADER_STRUCT,
    MAX_RESPONSE_SIZE,
    RECV_CHUNK_SIZE,
    TIMEOUT_DEFAULT,
    get_host,
    get_port,
    get_token,
)


class EwEMCPError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class EwEMCPClient:
    def __init__(self, host: str | None = None, port: int | None = None, token: str | None = None):
        self.host = host or get_host()
        self.port = port or get_port()
        self.token = token or get_token()
        self.socket: socket.socket | None = None
        self._current_timeout: int | float | None = None

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
            return False

    def disconnect(self) -> None:
        if self.socket is not None:
            self.socket.close()
            self.socket = None
            self._current_timeout = None

    def _set_timeout(self, timeout: int | float | None) -> None:
        if self.socket is None:
            raise ConnectionError("Not connected to EwE MCP plugin")
        if self._current_timeout != timeout:
            self.socket.settimeout(timeout)
            self._current_timeout = timeout

    def _recv_exact(self, n: int) -> bytes:
        if self.socket is None:
            raise ConnectionError("Not connected to EwE MCP plugin")
        if n > MAX_RESPONSE_SIZE:
            raise ValueError(f"Response too large: {n} bytes")
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
            raise ConnectionError("Not connected to EwE MCP plugin")

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

            self._set_timeout(timeout)
            header = self._recv_exact(4)
            length = HEADER_STRUCT.unpack(header)[0]
            data = self._recv_exact(length)
            self._set_timeout(None)
            return json.loads(data.decode("utf-8"))
        except Exception:
            self.disconnect()
            raise

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
