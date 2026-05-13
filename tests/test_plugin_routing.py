from __future__ import annotations

import json
import socket
import struct
import threading

import pytest

from ewe_mcp.client import EwEMCPClient, EwEMCPError
from ewe_mcp.helpers import HEADER_STRUCT, MAX_RESPONSE_SIZE


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


def _serve_once_raw(handler):
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
                conn.recv(length)
                handler(conn)
        finally:
            server.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return port, thread


def test_malformed_json_sends_valid_request_and_gets_error():
    def handler(request):
        assert request["type"] == "ping"
        return {
            "id": request["id"],
            "status": "error",
            "error": {"code": "malformed_json", "message": "Could not parse JSON body"},
        }

    port, thread = _serve_once(handler)
    client = EwEMCPClient(host="127.0.0.1", port=port, token="secret")
    assert client.connect()
    with pytest.raises(EwEMCPError) as excinfo:
        client.command_result("ping")
    assert excinfo.value.code == "malformed_json"
    assert "parse" in excinfo.value.message.lower()
    client.disconnect()
    thread.join(timeout=2)


def test_bad_token_returns_unauthorized():
    def handler(request):
        return {
            "id": request["id"],
            "status": "error",
            "error": {"code": "unauthorized", "message": "Invalid or missing EWE_MCP_TOKEN"},
        }

    port, thread = _serve_once(handler)
    client = EwEMCPClient(host="127.0.0.1", port=port, token="wrong-token")
    assert client.connect()
    with pytest.raises(EwEMCPError) as excinfo:
        client.command_result("ping")
    assert excinfo.value.code == "unauthorized"
    assert "EWE_MCP_TOKEN" in excinfo.value.message
    client.disconnect()
    thread.join(timeout=2)


def test_unknown_command_returns_error():
    def handler(request):
        return {
            "id": request["id"],
            "status": "error",
            "error": {"code": "unknown_command", "message": f"Unknown command type: {request['type']}"},
        }

    port, thread = _serve_once(handler)
    client = EwEMCPClient(host="127.0.0.1", port=port, token="secret")
    assert client.connect()
    with pytest.raises(EwEMCPError) as excinfo:
        client.command_result("do_magic")
    assert excinfo.value.code == "unknown_command"
    client.disconnect()
    thread.join(timeout=2)


def test_oversized_message_raises_value_error():
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
                conn.recv(length)
                oversize = MAX_RESPONSE_SIZE + 1
                conn.sendall(struct.pack(">I", oversize))
        finally:
            server.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()

    client = EwEMCPClient(host="127.0.0.1", port=port, token="secret")
    assert client.connect()
    with pytest.raises(ValueError, match="too large"):
        client.command_result("ping")
    client.disconnect()
    thread.join(timeout=2)


def test_structured_exception_envelope():
    def handler(request):
        return {
            "id": request["id"],
            "status": "error",
            "error": {"code": "core_unavailable", "message": "Core is not initialized"},
        }

    port, thread = _serve_once(handler)
    client = EwEMCPClient(host="127.0.0.1", port=port, token="secret")
    assert client.connect()
    with pytest.raises(EwEMCPError) as excinfo:
        client.command_result("get_ecopath_stats")
    assert excinfo.value.code == "core_unavailable"
    assert excinfo.value.message == "Core is not initialized"
    client.disconnect()
    thread.join(timeout=2)


def test_missing_command_type_returns_error():
    def handler(request):
        return {
            "id": request["id"],
            "status": "error",
            "error": {"code": "missing_type", "message": "Missing command type"},
        }

    port, thread = _serve_once(handler)
    client = EwEMCPClient(host="127.0.0.1", port=port, token="secret")
    assert client.connect()
    with pytest.raises(EwEMCPError) as excinfo:
        client.command_result("")
    assert excinfo.value.code == "missing_type"
    client.disconnect()
    thread.join(timeout=2)


def test_success_response_unwraps_result():
    def handler(request):
        return {
            "id": request["id"],
            "status": "success",
            "result": {"version": "6.7", "build": "18865"},
        }

    port, thread = _serve_once(handler)
    client = EwEMCPClient(host="127.0.0.1", port=port, token="secret")
    assert client.connect()
    result = client.command_result("get_ewe_info")
    assert result == {"version": "6.7", "build": "18865"}
    assert "status" not in result
    assert "id" not in result
    client.disconnect()
    thread.join(timeout=2)


def test_server_returns_non_json():
    def send_garbage(conn):
        payload = b"<<not valid json!!>>"
        conn.sendall(HEADER_STRUCT.pack(len(payload)))
        conn.sendall(payload)

    port, thread = _serve_once_raw(send_garbage)
    client = EwEMCPClient(host="127.0.0.1", port=port, token="secret")
    assert client.connect()
    with pytest.raises((json.JSONDecodeError, ValueError)):
        client.command_result("ping")
    client.disconnect()
    thread.join(timeout=2)
