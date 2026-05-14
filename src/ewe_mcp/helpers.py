"""Shared constants and helpers for EwE MCP."""

from __future__ import annotations

from functools import lru_cache
import os
import struct
from typing import Any

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9877
DEFAULT_TOKEN = "ewe-mcp-local-dev"
TIMEOUT_DEFAULT = 30
TIMEOUT_LONG = 120
RECV_CHUNK_SIZE = 65_536
DEFAULT_MAX_RESPONSE_SIZE = 10 * 1024 * 1024


@lru_cache(maxsize=1)
def get_max_response_size() -> int:
    raw = os.environ.get("EWE_MCP_MAX_RESPONSE_SIZE", str(DEFAULT_MAX_RESPONSE_SIZE))
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"EWE_MCP_MAX_RESPONSE_SIZE must be an integer, got {raw!r}")
    if value <= 0:
        raise ValueError(f"EWE_MCP_MAX_RESPONSE_SIZE must be positive, got {value}")
    return value


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
        return "This UI command opens a modal dialog and is blocked in MCP mode. Use a dedicated MCP tool when available, operate the EwE GUI manually, or enable dialog_watcher_enabled only for supervised workflows."
    if "response too large" in lower:
        return "The EwE plugin response exceeded the size limit. Try requesting smaller subsets (e.g., fewer variables, shorter time ranges, or pagination)."
    if (
        "cannot be 0" in lower
        or "cannot be zero" in lower
        or "value is out of range" in lower
    ):
        return "EwE rejected this value as out of range. Use validate_values first and check EwE's range constraints for the specific variable."
    if "'data'" in lower and "keyerror" in lower:
        return "A modal dialog may be blocking the EwE GUI. Try enabling dialog_watcher_enabled in the plugin config, or dismiss the dialog manually on the Windows VM."
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
    filtering = should_filter_sentinels()
    checks.append(
        {
            "name": "sentinel_filtering",
            "status": "ok" if filtering else "disabled",
            "detail": {
                "enabled": filtering,
                "env_var": "EWE_MCP_FILTER_SENTINELS",
            },
        }
    )
    return result


# ── EwE range constraints (BUG-8) ───────────────────────────────────────

EWE_NULL_VALUE = -9999.0

# EwE uses -9999 as its null/not-set sentinel (cNullValue in EwECore).
# Both int and float forms appear depending on JSON serialization.
_SENTINEL_VALUES = frozenset({-9999, -9999.0})


@lru_cache(maxsize=1)
def should_filter_sentinels() -> bool:
    return os.environ.get("EWE_MCP_FILTER_SENTINELS", "1") != "0"


def filter_sentinels(value: Any) -> Any:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value in _SENTINEL_VALUES:
        return None
    if isinstance(value, dict):
        return {k: filter_sentinels(v) for k, v in value.items()}
    if isinstance(value, list):
        return [filter_sentinels(item) for item in value]
    if isinstance(value, tuple):
        return tuple(filter_sentinels(item) for item in value)
    return value


# 4-tuple: (min_val, max_val, min_exclusive, max_exclusive)
# min_exclusive=True means value must be > min_val (not >=)
# max_exclusive=True means value must be < max_val (not <=)
EWE_RANGE_CONSTRAINTS: dict[str, dict[str, tuple[float, float, bool, bool]]] = {
    "ecosim_group_inputs": {
        # (min, max, min_exclusive, max_exclusive)
        "den_dep_catchability": (1.0, float("inf"), False, False),  # ge 1.0
        "vul_mult": (1.0, float("inf"), False, False),  # ge 1.0
        "max_rel_feeding_time": (0.0, 100.0, True, False),  # gt 0, le 100
        "feeding_time_adjust_rate": (0.0, 1.0, False, True),  # ge 0, lt 1
        "max_rel_pb": (0.0, float("inf"), True, False),  # gt 0
    },
    "ecosim_fleet_inputs": {
        "rel_q": (1.0, float("inf"), False, False),  # ge 1.0
        "effort_conversion_factor": (0.0, float("inf"), True, False),  # gt 0
    },
}

_DOMAIN_ALIASES: dict[str, str] = {
    "ecopath_groups": "ecopath_group",
    "ecopath_fleets": "ecopath_fleet",
    "ecospace_habitats": "ecospace_habitat",
}

# Non-exhaustive map of common eStatusFlags composite codes.
# Bits: 1=OK, 2=FailedValidation, 4=ValueComputed, 8=InvalidModelResult,
# 16=NotEditable, 32=ErrorEncountered, 64=MissingParameter,
# 128=CoreHighlight, 256=Null, 512=Stored
STATUS_CODE_MAP: dict[int, str] = {
    1: "OK",
    2: "FailedValidation",
    4: "ValueComputed",
    8: "InvalidModelResult",
    16: "NotEditable",
    32: "ErrorEncountered",
    64: "MissingParameter",
    128: "CoreHighlight",
    256: "Null",
    512: "Stored",
    513: "Stored+OK",
    528: "Stored+NotEditable",
    532: "Stored+NotEditable+ValueComputed",
    768: "Stored+Null",
    784: "Stored+Null+NotEditable",
    785: "Stored+Null+NotEditable+OK",
}


def normalize_domain_name(domain: str) -> str:
    """Normalize domain name aliases (e.g., ecopath_groups -> ecopath_group)."""
    return _DOMAIN_ALIASES.get(domain, domain)


def validate_ewe_ranges(
    domain: str, values: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    """Check EwE-specific range constraints that the plugin's validate_values misses.

    Returns a dict keyed by variable name with validation results for any
    variables that violate range constraints. Unknown domains/variables are
    skipped (return empty dict).
    """
    constraints = EWE_RANGE_CONSTRAINTS.get(domain, {})
    if not constraints:
        return {}
    results: dict[str, dict[str, Any]] = {}
    for var_name, raw_value in values.items():
        if var_name not in constraints:
            continue
        if isinstance(raw_value, (int, float)) and raw_value in _SENTINEL_VALUES:
            continue
        min_val, max_val, min_exclusive, max_exclusive = constraints[var_name]
        try:
            value = float(raw_value)
        except (TypeError, ValueError):
            continue
        violates_min = value <= min_val if min_exclusive else value < min_val
        violates_max = value >= max_val if max_exclusive else value > max_val
        if violates_min or violates_max:
            min_desc = f">{'>' if min_exclusive else '='} {min_val}"
            max_desc = f"{'<' if max_exclusive else '<='} {max_val}"
            results[var_name] = {
                "valid": False,
                "reason": f"{var_name} must be {min_desc} and {max_desc} (got {value})",
            }
        else:
            results[var_name] = {"valid": True}
    return results
