from __future__ import annotations

import json
import socket
import threading

import pytest

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
                if not header or len(header) < 4:
                    return
                length = HEADER_STRUCT.unpack(header)[0]
                request = json.loads(conn.recv(length).decode("utf-8"))
                response = handler(request)
                payload = json.dumps(response).encode("utf-8")
                try:
                    conn.sendall(HEADER_STRUCT.pack(len(payload)))
                    conn.sendall(payload)
                except BrokenPipeError:
                    pass
        finally:
            server.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return port, thread


def test_client_sends_token_and_unwraps_success():
    def handler(request):
        assert request["type"] == "ping"
        assert request["token"] == "secret"
        assert "timeout" in request
        return {"id": request["id"], "status": "success", "result": {"pong": True}}

    port, thread = _serve_once(handler)
    client = EwEMCPClient(host="127.0.0.1", port=port, token="secret")
    assert client.connect()
    assert client.command_result("ping") == {"pong": True}
    client.disconnect()
    thread.join(timeout=2)


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

def test_client_timeout():
    import time
    def handler(request):
        time.sleep(0.5)
        return {"id": request["id"], "status": "success", "result": {}}

    port, thread = _serve_once(handler)
    client = EwEMCPClient(host="127.0.0.1", port=port, token="secret")
    assert client.connect()

    with pytest.raises(socket.timeout):
        client.command_result("ping", timeout=0.1)

    client.disconnect()
    thread.join(timeout=2)

def test_client_cleanup_on_close():
    def handler(request):
        return {"id": request["id"], "status": "success", "result": {}}

    port, thread = _serve_once(handler)
    client = EwEMCPClient(host="127.0.0.1", port=port, token="secret")
    assert client.connect()
    assert client.socket is not None
    client.disconnect()
    assert client.socket is None

    # We should handle calling disconnect multiple times safely
    client.disconnect()
    thread.join(timeout=2)

def test_client_disconnects_on_oversized_response():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]

    def run():
        try:
            conn, _ = server.accept()
            with conn:
                header = conn.recv(4)
                if not header or len(header) < 4:
                    return
                length = HEADER_STRUCT.unpack(header)[0]
                _ = conn.recv(length)
                fake_length = 50 * 1024 * 1024
                conn.sendall(HEADER_STRUCT.pack(fake_length))
                conn.sendall(b'{"id":"test","status":"success","result":{}')
        finally:
            server.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()

    client = EwEMCPClient(host="127.0.0.1", port=port, token="secret")
    assert client.connect()
    assert client.socket is not None

    with pytest.raises(ValueError, match="Response too large"):
        client.command_result("ping")

    assert client.socket is None, "Socket should be disconnected after oversized response rejection"
    thread.join(timeout=2)


def test_client_recovers_after_timeout():
    import time
    def handler(request):
        if request["type"] == "slow":
            time.sleep(0.5)
            return {"id": request["id"], "status": "success", "result": {"slow": True}}
        return {"id": request["id"], "status": "success", "result": {"fast": True}}

    # we need the server to be able to accept multiple connections for this test
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]

    def run():
        try:
            while True:
                conn, _ = server.accept()
                with conn:
                    while True:
                        header = conn.recv(4)
                        if not header or len(header) < 4:
                            break
                        length = HEADER_STRUCT.unpack(header)[0]
                        request = json.loads(conn.recv(length).decode("utf-8"))
                        response = handler(request)
                        payload = json.dumps(response).encode("utf-8")
                        try:
                            conn.sendall(HEADER_STRUCT.pack(len(payload)))
                            conn.sendall(payload)
                        except BrokenPipeError:
                            break
        except Exception:
            pass
        finally:
            server.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()

    client = EwEMCPClient(host="127.0.0.1", port=port, token="secret")
    assert client.connect()

    with pytest.raises(socket.timeout):
        client.command_result("slow", timeout=0.1)

    # Wait for the slow response to finish and arrive in the old socket buffer
    time.sleep(0.6)

    # Send a new request on the same client, it should reconnect properly
    assert client.command_result("fast") == {"fast": True}

    client.disconnect()
