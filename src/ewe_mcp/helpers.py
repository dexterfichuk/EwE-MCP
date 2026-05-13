"""Shared constants and helpers for EwE MCP."""

from __future__ import annotations

import os
import struct
from typing import Any

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9877
DEFAULT_TOKEN = "ewe-mcp-local-dev"
TIMEOUT_DEFAULT = 30
TIMEOUT_LONG = 120
RECV_CHUNK_SIZE = 65_536
MAX_RESPONSE_SIZE = 100 * 1024 * 1024
HEADER_STRUCT = struct.Struct(">I")

EVAL_DENY_PATTERNS = (
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

BATCH_BLOCKED_COMMANDS = frozenset(
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
        "export_ecopath_results",
        "shutdown_server",
        "invoke_ui_command",
    }
)


def get_host() -> str:
    return os.environ.get("EWE_MCP_HOST", DEFAULT_HOST)


def get_port() -> int:
    raw = os.environ.get("EWE_MCP_PORT", str(DEFAULT_PORT))
    try:
        port = int(raw)
    except ValueError as exc:
        raise ValueError(f"EWE_MCP_PORT must be an integer, got {raw!r}") from exc
    if not 1 <= port <= 65535:
        raise ValueError(f"EWE_MCP_PORT out of range: {port}")
    return port


def get_token() -> str:
    return os.environ.get("EWE_MCP_TOKEN", DEFAULT_TOKEN)


def error_hint(message: str) -> str | None:
    lower = message.lower()
    if "connection" in lower or "refused" in lower:
        return "Ensure EwE is running and the EwE MCP plugin is loaded and listening."
    if "unauthorized" in lower or "token" in lower:
        return "Set the same EWE_MCP_TOKEN for the EwE plugin and Python MCP server."
    if "command" in lower and "not found" in lower:
        return "Call list_ui_commands or get_command_catalog to inspect available EwE UI commands."
    if "core" in lower and "unavailable" in lower:
        return "Wait for EwE startup to finish or load a model before using model/run tools."
    if "dialog_command_blocked" in lower:
        return "This UI command opens a modal dialog that blocks the MCP plugin. Use the dedicated MCP tool instead (e.g., set_values for parameter edits, run_ecopath for balancing)."
    return None


def enrich_diagnose(result: dict[str, Any]) -> dict[str, Any]:
    checks = result.setdefault("checks", [])
    checks.append(
        {
            "name": "python_server",
            "status": "ok",
            "detail": {"package": "ewe-mcp"},
        }
    )
    return result
