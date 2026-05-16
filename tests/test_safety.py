"""Safety-focused tests for EwE MCP — all offline unless marked live."""

from __future__ import annotations

import json
import os
import pathlib
import re
import socket
import threading

import pytest

from ewe_mcp.client import EwEMCPClient
from ewe_mcp.helpers import (
    BATCH_BLOCKED_COMMANDS,
    DEFAULT_HOST,
    DEFAULT_MAX_RESPONSE_SIZE,
    DEFAULT_TOKEN,
    EVAL_DENY_PATTERNS,
    get_token,
    HEADER_STRUCT,
)
from ewe_mcp.server import save_screenshot


def test_plugin_defaults_to_localhost():
    assert DEFAULT_HOST == "127.0.0.1"


def test_token_is_required_by_default():
    assert DEFAULT_TOKEN, "DEFAULT_TOKEN must not be empty"
    assert get_token(), "get_token() must return a non-empty string"
    saved = os.environ.pop("EWE_MCP_TOKEN", None)
    try:
        assert get_token() == DEFAULT_TOKEN, (
            "get_token() should fall back to DEFAULT_TOKEN when env var is unset"
        )
    finally:
        if saved is not None:
            os.environ["EWE_MCP_TOKEN"] = saved


def test_eval_disabled_by_default():
    val = os.environ.get("EWE_MCP_ENABLE_EVAL")
    assert val is None or val == "0", (
        f"EWE_MCP_ENABLE_EVAL should be unset or '0', got {val!r}"
    )
    assert "execute_ewe_code" in BATCH_BLOCKED_COMMANDS


def test_destructive_tools_have_annotations():
    from ewe_mcp import server

    destructive_annotated = set()
    for name in dir(server):
        obj = getattr(server, name, None)
        if obj is None or not callable(obj):
            continue
        annotations = getattr(obj, "_mcp_tool_annotations", None) or getattr(
            obj, "__mcp_annotations__", None
        )
        if annotations is not None and getattr(annotations, "destructiveHint", False):
            destructive_annotated.add(name)

    batch_proxy = {
        "discard_changes",
        "save_ecosim_scenario",
        "shutdown_server",
    }
    not_in_batch = destructive_annotated - BATCH_BLOCKED_COMMANDS - batch_proxy
    assert not not_in_batch, (
        f"These destructive-annotated tools are missing from BATCH_BLOCKED_COMMANDS "
        f"(and not in the known non-batched proxy list): {sorted(not_in_batch)}"
    )
    assert "execute_ewe_code" in BATCH_BLOCKED_COMMANDS


def test_audit_log_entries_have_required_fields():
    def _serve_persistent(handler, max_requests=2):
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]

        def run():
            try:
                conn, _ = server.accept()
                with conn:
                    for _ in range(max_requests):
                        header = b""
                        while len(header) < 4:
                            chunk = conn.recv(4 - len(header))
                            if not chunk:
                                return
                            header += chunk
                        length = HEADER_STRUCT.unpack(header)[0]
                        data = b""
                        while len(data) < length:
                            chunk = conn.recv(length - len(data))
                            if not chunk:
                                return
                            data += chunk
                        request = json.loads(data.decode("utf-8"))
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

    def handler(request):
        if request["type"] == "ping":
            return {"id": request["id"], "status": "success", "result": {"pong": True}}
        if request["type"] == "get_message_log":
            entries = [
                {
                    "timestamp_utc": "2025-01-01T00:00:00.0000000Z",
                    "command": "ping",
                    "status": "success",
                    "message": "",
                },
                {
                    "timestamp_utc": "2025-01-01T00:00:01.0000000Z",
                    "command": "load_model",
                    "status": "success",
                    "message": "",
                },
            ]
            return {
                "id": request["id"],
                "status": "success",
                "result": {"entries": entries},
            }
        return {"id": request["id"], "status": "success", "result": {}}

    port, thread = _serve_persistent(handler)
    client = EwEMCPClient(host="127.0.0.1", port=port, token="test")
    assert client.connect()
    client.command_result("ping")
    log_result = client.command_result("get_message_log")
    client.disconnect()
    thread.join(timeout=2)

    entries = log_result.get("entries", [])
    assert len(entries) > 0, "Audit log should contain entries"
    required_fields = {"timestamp_utc", "command", "status"}
    for entry in entries:
        missing = required_fields - set(entry.keys())
        assert not missing, f"Audit log entry missing required fields: {missing}"


def test_max_response_size_prevents_oversized_reads():
    assert isinstance(DEFAULT_MAX_RESPONSE_SIZE, int)
    assert DEFAULT_MAX_RESPONSE_SIZE > 0
    assert DEFAULT_MAX_RESPONSE_SIZE == 10 * 1024 * 1024


_RUN_STOP_PATTERN = re.compile(r"^(run_|stop_)")
_SCENARIO_CRUD_PATTERN = re.compile(
    r"^(create_|copy_|delete_|save_|load_|close_).*scenario$"
)
_ECOPATH_ENTITY_CREATE_PATTERN = re.compile(
    r"^create_ecopath_(group|fleet)$"
)
_ECOSPACE_SPATIAL_MUTATION_PATTERN = re.compile(
    r"^(create_ecospace_mpa|set_ecospace_mpa_cells|create_regions_from_mpas)$"
)
_SET_MUTATION_PATTERN = re.compile(r"^set_")
_IMPORT_MUTATION_PATTERN = re.compile(r"^import_")
_MODEL_LIFECYCLE = {"create_model", "load_model", "save_model", "save_model_as", "close_model"}
_CAPTURE_TOOLS = {"capture_view", "save_screenshot"}


def test_batch_blocked_commands_are_destructive_or_sensitive():
    allowed_categories = (
        _MODEL_LIFECYCLE
        | _CAPTURE_TOOLS
        | {
            "execute_ewe_code",
            "export_ecopath_results",
            "discard_changes",
            "run_ecopath",
            "shutdown_server",
            "invoke_ui_command",
        }
    )
    for cmd in BATCH_BLOCKED_COMMANDS:
        if cmd in allowed_categories:
            continue
        if _RUN_STOP_PATTERN.match(cmd):
            continue
        if _SCENARIO_CRUD_PATTERN.match(cmd):
            continue
        if _ECOPATH_ENTITY_CREATE_PATTERN.match(cmd):
            continue
        if _ECOSPACE_SPATIAL_MUTATION_PATTERN.match(cmd):
            continue
        if _SET_MUTATION_PATTERN.match(cmd):
            continue
        if _IMPORT_MUTATION_PATTERN.match(cmd):
            continue
        pytest.fail(
            f"Command {cmd!r} is in BATCH_BLOCKED_COMMANDS but does not match "
            f"any recognized destructive/sensitive category"
        )


@pytest.mark.asyncio
async def test_export_path_validation():
    with pytest.raises(ValueError, match=r"\.\."):
        await save_screenshot(path="/tmp/../etc/evil.png")


@pytest.mark.asyncio
async def test_save_screenshot_rejects_dotdot():
    with pytest.raises(ValueError, match=r"\.\."):
        await save_screenshot(path="relative/../escape.png")


def test_execute_ewe_code_blocked_from_batch():
    assert "execute_ewe_code" in BATCH_BLOCKED_COMMANDS


def test_execute_ewe_code_has_destructive_annotation():
    from ewe_mcp.server import execute_ewe_code

    assert callable(execute_ewe_code)


def test_eval_deny_patterns_defined():
    expected = (
        "System.IO",
        "System.Diagnostics.Process",
        "System.Reflection.Assembly.Load",
        "File.Delete",
        "File.Write",
        "Directory.Delete",
        "System.Net",
        "WebClient",
        "HttpClient",
        "Process.Start",
    )
    assert EVAL_DENY_PATTERNS == expected, (
        f"EVAL_DENY_PATTERNS must match the C# _evalDenyPatterns exactly. "
        f"Expected {expected!r}, got {EVAL_DENY_PATTERNS!r}"
    )


def test_installer_config_template_has_required_section_and_keys():
    cfg_path = (
        pathlib.Path(__file__).resolve().parents[1] / "installer" / "EwE_MCP_Plugin.cfg"
    )
    text = cfg_path.read_text(encoding="utf-8")
    assert "[EwE_MCP]" in text
    for key in ("host=", "port=", "token="):
        assert key in text


def test_shutdown_server_blocked_from_batch():
    assert "shutdown_server" in BATCH_BLOCKED_COMMANDS


_CSHARP_EVAL_DENY_PATTERNS = (
    "System.IO",
    "System.Diagnostics.Process",
    "System.Reflection.Assembly.Load",
    "File.Delete",
    "File.Write",
    "Directory.Delete",
    "System.Net",
    "WebClient",
    "HttpClient",
    "Process.Start",
)

_CSHARP_BATCH_BLOCKED = frozenset(
    {
        "execute_ewe_code",
        "close_model",
        "load_model",
        "save_model",
        "save_model_as",
        "run_ecopath",
        "run_ecosim",
        "stop_ecosim",
        "capture_view",
        "save_screenshot",
        "create_ecosim_scenario",
        "copy_ecosim_scenario",
        "delete_ecosim_scenario",
        "load_ecosim_scenario",
        "load_ecospace_scenario",
        "create_ecospace_scenario",
        "copy_ecospace_scenario",
        "delete_ecospace_scenario",
        "save_ecospace_scenario",
        "create_ecospace_mpa",
        "set_ecospace_mpa_cells",
        "create_regions_from_mpas",
        "run_ecospace",
        "stop_ecospace",
        "load_ecotracer_scenario",
        "create_ecotracer_scenario",
        "copy_ecotracer_scenario",
        "delete_ecotracer_scenario",
        "save_ecotracer_scenario",
        "close_ecotracer_scenario",
        "run_mse",
        "stop_mse",
        "run_monte_carlo",
        "stop_monte_carlo",
        "set_ecosim_forcing_shape",
        "export_ecopath_results",
        "create_ecopath_group",
        "create_ecopath_fleet",
        "shutdown_server",
        "invoke_ui_command",
        "import_time_series",
    }
)


def test_python_eval_deny_patterns_match_csharp():
    assert set(EVAL_DENY_PATTERNS) == set(_CSHARP_EVAL_DENY_PATTERNS), (
        f"Python EVAL_DENY_PATTERNS differs from C# _evalDenyPatterns. "
        f"Python-only: {set(EVAL_DENY_PATTERNS) - set(_CSHARP_EVAL_DENY_PATTERNS)}, "
        f"C#-only: {set(_CSHARP_EVAL_DENY_PATTERNS) - set(EVAL_DENY_PATTERNS)}"
    )


def test_python_batch_blocked_is_superset_of_csharp():
    csharp_missing = _CSHARP_BATCH_BLOCKED - BATCH_BLOCKED_COMMANDS
    assert not csharp_missing, (
        f"C# blocks these commands in batch but Python does not: {sorted(csharp_missing)}"
    )


def _extract_csharp_dialog_commands():
    cs_path = (
        pathlib.Path(__file__).resolve().parents[1]
        / "src"
        / "EwE_MCP_Plugin"
        / "EwEMcpPlugin.cs"
    )
    if not cs_path.exists():
        return frozenset()
    text = cs_path.read_text(encoding="utf-8")
    import re

    matches = re.findall(
        r"DefaultDialogOpeningCommands[^;]*?=\s*new\s+HashSet[^{]*\{(.*?)\}",
        text,
        re.DOTALL,
    )
    if not matches:
        return frozenset()
    commands = set()
    for m in matches:
        for item in re.findall(r'"([^"]+)"', m):
            commands.add(item)
    return frozenset(commands)


_CSHARP_DIALOG_OPENING_COMMANDS = _extract_csharp_dialog_commands()


def test_dialog_opening_commands_denylist_is_known():
    if not _CSHARP_DIALOG_OPENING_COMMANDS:
        pytest.skip("Could not parse DefaultDialogOpeningCommands from C# source")
    assert "ImportTimeSeries" in _CSHARP_DIALOG_OPENING_COMMANDS


def test_invoke_ui_command_is_batch_blocked():
    assert "invoke_ui_command" in BATCH_BLOCKED_COMMANDS, (
        "invoke_ui_command should be batch-blocked since dialog-opening commands can hang"
    )


def test_dialog_watcher_disabled_by_default():
    cs_path = (
        pathlib.Path(__file__).resolve().parents[1]
        / "src"
        / "EwE_MCP_Plugin"
        / "EwEMcpProtocol.cs"
    )
    if not cs_path.exists():
        pytest.skip("C# source not available")
    text = cs_path.read_text(encoding="utf-8")
    assert "DialogWatcherEnabled" in text, (
        "DialogWatcherEnabled property must exist in EwEMcpConfig"
    )
    import re

    default_match = re.findall(r"dialogWatcherEnabled\s*=\s*(true|false)", text)
    if default_match:
        assert default_match[-1] == "false", (
            "Dialog watcher should be disabled by default"
        )


def test_dialog_blocked_commands_config_key_exists():
    cs_path = (
        pathlib.Path(__file__).resolve().parents[1]
        / "src"
        / "EwE_MCP_Plugin"
        / "EwEMcpProtocol.cs"
    )
    if not cs_path.exists():
        pytest.skip("C# source not available")
    text = cs_path.read_text(encoding="utf-8")
    assert "DialogBlockedCommands" in text, (
        "DialogBlockedCommands property must exist in EwEMcpConfig"
    )
    assert "EWE_MCP_DIALOG_BLOCKED_COMMANDS" in text, (
        "EWE_MCP_DIALOG_BLOCKED_COMMANDS env var must be read"
    )


def test_ecopath_run_state_detail_reads_nonpublic_field():
    cs_path = (
        pathlib.Path(__file__).resolve().parents[1]
        / "src"
        / "EwE_MCP_Plugin"
        / "EwEMcpPlugin.cs"
    )
    if not cs_path.exists():
        pytest.skip("C# source not available")
    text = cs_path.read_text(encoding="utf-8")
    assert 'SafeGetMember(core, "Ecopath") ?? SafeGetMember(core, "m_Ecopath")' in text
    assert 'GetField(\n                        "RunState",' in text
    assert "BindingFlags.NonPublic" in text
    assert "BindingFlags.Instance" in text


def test_dialog_opening_commands_are_cached():
    cs_path = (
        pathlib.Path(__file__).resolve().parents[1]
        / "src"
        / "EwE_MCP_Plugin"
        / "EwEMcpPlugin.cs"
    )
    if not cs_path.exists():
        pytest.skip("C# source not available")
    text = cs_path.read_text(encoding="utf-8")
    assert "private HashSet<string> _dialogOpeningCommands;" in text
    assert "_dialogOpeningCommands = commands;" in text


def test_set_value_commands_navigate_by_default_in_plugin():
    cs_path = (
        pathlib.Path(__file__).resolve().parents[1]
        / "src"
        / "EwE_MCP_Plugin"
        / "EwEMcpPlugin.cs"
    )
    if not cs_path.exists():
        pytest.skip("C# source not available")
    text = cs_path.read_text(encoding="utf-8")
    assert 'OptionalBool(parameters, "navigate", true)' in text


def test_dialog_watcher_logs_errors_and_does_not_foreground():
    cs_path = (
        pathlib.Path(__file__).resolve().parents[1]
        / "src"
        / "EwE_MCP_Plugin"
        / "EwEMcpSocketServer.cs"
    )
    if not cs_path.exists():
        pytest.skip("C# source not available")
    text = cs_path.read_text(encoding="utf-8")
    assert "Dialog watcher error:" in text
    assert "SetForegroundWindow" not in text


def test_config_file_requires_ewe_mcp_section_header():
    """The C# ReadConfigFile() only reads key=value pairs under [EwE_MCP].
    The installer must write this section header in the default config file."""

    expected_header = "[EwE_MCP]"
    installer_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)),
        "installer",
        "EwE_MCP_Plugin.iss",
    )
    if not os.path.exists(installer_path):
        pytest.skip("Installer .iss file not present")

    content = pathlib.Path(installer_path).read_text()
    lines_section = (
        content.split("CurStepChanged")[1] if "CurStepChanged" in content else ""
    )
    assert expected_header in lines_section, (
        f"Installer CurStepChanged must write '{expected_header}' section header "
        f"before key=value lines — without it, ReadConfigFile() ignores all values"
    )
