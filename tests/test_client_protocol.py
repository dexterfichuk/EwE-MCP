from __future__ import annotations

import json
import socket
import threading

from ewe_mcp.client import EwEMCPClient, EwEMCPError
from ewe_mcp.helpers import HEADER_STRUCT


def _serve_once(handler):
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]

    def run():
        try:
            conn, _ = server.accept()
            with conn:
                header = conn.recv(4)
                length = HEADER_STRUCT.unpack(header)[0]
                request = json.loads(conn.recv(length).decode("utf-8"))
                response = handler(request)
                payload = json.dumps(response).encode("utf-8")
                conn.sendall(HEADER_STRUCT.pack(len(payload)))
                conn.sendall(payload)
        finally:
            server.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return port, thread


def test_client_sends_token_and_unwraps_success():
    def handler(request):
        assert request["type"] == "ping"
        assert request["token"] == "secret"
        return {"id": request["id"], "status": "success", "result": {"pong": True}}

    port, thread = _serve_once(handler)
    client = EwEMCPClient(host="127.0.0.1", port=port, token="secret")
    assert client.connect()
    assert client.command_result("ping") == {"pong": True}
    client.disconnect()
    thread.join(timeout=2)


import pytest

def test_client_raises_on_error_response():
    def handler(request):
        return {
            "id": request["id"],
            "status": "error",
            "error": {"code": "unauthorized", "message": "Invalid token"},
        }

    port, thread = _serve_once(handler)
    client = EwEMCPClient(host="127.0.0.1", port=port, token="bad")
    assert client.connect()
    with pytest.raises(EwEMCPError) as excinfo:
        client.command_result("ping")
    assert excinfo.value.code == "unauthorized"
    assert excinfo.value.message == "Invalid token"
    client.disconnect()
    thread.join(timeout=2)
