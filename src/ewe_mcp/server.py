"""FastMCP server exposing a running EwE instance."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any

import socket

import base64
import pathlib

from mcp.server.fastmcp import FastMCP
from mcp.types import ImageContent, TextContent, ToolAnnotations

from ewe_mcp.client import EwEMCPClient, EwEMCPError, ResponseTooLargeError
from ewe_mcp.ecopath import (
    estimate_average_longevity as ecopath_estimate_average_longevity,
    estimate_catch as ecopath_estimate_catch,
    estimate_consumption as ecopath_estimate_consumption,
    estimate_fishing_mortality as ecopath_estimate_fishing_mortality,
    estimate_production as ecopath_estimate_production,
    estimate_total_mortality as ecopath_estimate_total_mortality,
)
from ewe_mcp.data_sources.fishbase import query_fishbase as _query_fishbase
from ewe_mcp.data_sources.sealifebase import query_sealifebase as _query_sealifebase
from ewe_mcp.data_sources.globi import query_globi as _query_globi
from ewe_mcp.helpers import (
    STATUS_CODE_MAP,
    TIMEOUT_CREATE_SCENARIO,
    TIMEOUT_DEFAULT,
    TIMEOUT_LONG,
    _ERROR_CODE_HINTS,
    cache_batch_blocked_commands,
    enrich_diagnose,
    error_hint,
    filter_sentinels,
    get_batch_blocked_commands,
    get_host,
    get_port,
    get_token,
    normalize_domain_name,
    should_filter_sentinels,
    validate_ewe_ranges,
)
from ewe_mcp.name_batch import (
    DOMAIN_CONFIG,
    STATIC_WRITABLE_FIELDS,
    default_capabilities,
    rank_name_suggestions,
    resolve_exact_name,
)

logger = logging.getLogger("ewe_mcp")
mcp = FastMCP("EwE MCP")

_connection: EwEMCPClient | None = None
_validated_at = 0.0
_connection_ttl = 5.0
_send_lock = asyncio.Lock()
_writable_fields_cache: dict[str, set[str]] | None = None
_VALID_CAPTURE_TARGETS = {"auto", "main", "active", "content"}
_VALID_IMPORT_TYPES = {"biomass", "catch", "effort", "forcing"}


def _reject_path_traversal(path: str) -> None:
    """Reject paths containing '..' components. Works for both POSIX and Windows paths on any host."""
    # Check Windows path interpretation (catches C:..\foo type bypasses that
    # PurePosixPath would not parse as containing a '..' component).
    if ".." in pathlib.PureWindowsPath(path).parts:
        raise ValueError("Path cannot contain '..' components.")
    # Also check POSIX interpretation for cross-platform safety.
    if ".." in pathlib.PurePosixPath(path).parts:
        raise ValueError("Path cannot contain '..' components.")


def _invalidate_connection() -> None:
    global _connection, _validated_at
    if _connection is not None:
        with contextlib.suppress(Exception):
            _connection.disconnect()
    _connection = None
    _validated_at = 0.0


def _get_connection() -> EwEMCPClient:
    global _connection, _validated_at
    if _connection is not None:
        now = time.monotonic()
        if now - _validated_at < _connection_ttl:
            return _connection
        try:
            if _connection.socket is None:
                raise OSError("socket is None")
            _connection.socket.getpeername()
            _validated_at = now
            return _connection
        except OSError:
            _invalidate_connection()

    client = EwEMCPClient(host=get_host(), port=get_port(), token=get_token())
    if not client.connect():
        raise ConnectionError(
            f"Could not connect to EwE MCP plugin at {client.host}:{client.port}"
        )
    _connection = client
    _validated_at = time.monotonic()
    return client


def _send_sync(
    command_type: str,
    params: dict[str, Any] | None = None,
    timeout: int = TIMEOUT_DEFAULT,
):
    try:
        return _get_connection().command_result(
            command_type, params, timeout=timeout
        )
    except ResponseTooLargeError:
        raise
    except (OSError, ConnectionError) as exc:
        _invalidate_connection()
        raise


def _error_hint_for_exception(exc: Exception) -> str | None:
    """Return an actionable hint for any exception type.

    For EwEMCPError exceptions from the C# plugin, uses error-code-based matching
    first, then falls back to string-based matching on the message.
    For all other exceptions, uses the existing string-based error_hint() matching.
    """
    if isinstance(exc, EwEMCPError):
        code_hint = _ERROR_CODE_HINTS.get(exc.code)
        if code_hint:
            return code_hint
        # Fall back to string matching — EwEMCPError messages now include [code] prefix
        hint = error_hint(str(exc))
        if hint:
            return hint
        # Generic fallback for unknown EwEMCPError codes
        return (
            f"EwE plugin returned error code '{exc.code}'. "
            "Check the plugin log at %APPDATA%\\Ecopath with Ecosim\\EwE_MCP_Plugin.log "
            "on the Windows VM for details."
        )
    return error_hint(str(exc))


async def _send(
    command_type: str,
    params: dict[str, Any] | None = None,
    timeout: int = TIMEOUT_DEFAULT,
):
    async with _send_lock:
        try:
            logger.info("CMD: %s %s", command_type, params if params else "")
            result = await asyncio.to_thread(_send_sync, command_type, params, timeout)
            if should_filter_sentinels():
                result = filter_sentinels(result)
            logger.info("OK:  %s", command_type)
        except (TimeoutError, socket.timeout) as exc:
            # Socket timeout: invalidate connection so next call creates a fresh one.
            # _send_sync already calls _invalidate_connection() for OSError (parent class),
            # but TimeoutError may not reach there in all asyncio.to_thread flows,
            # so we invalidate explicitly here for defense-in-depth.
            _invalidate_connection()
            hint = _error_hint_for_exception(exc)
            logger.warning("TIMEOUT: %s — %s", command_type, exc)
            if hint:
                raise RuntimeError(f"{exc}\n\nHINT: {hint}") from exc
            raise
        except Exception as exc:
            hint = _error_hint_for_exception(exc)
            try:
                events = _get_connection().notifications
                if events:
                    logger.warning("Dialog events detected after failed %s: %s", command_type, events)
                    dialog_hint = (
                        f"\n\nDIALOG BLOCKING: {len(events)} dialog(s) are blocking the EwE GUI. "
                        f"Events: {events}. "
                        f"Use get_dialog_events() to inspect, or enable dialog_watcher_enabled."
                    )
                    hint = (hint or "") + dialog_hint
            except Exception:
                pass
            logger.warning("ERR: %s — %s", command_type, exc)
            if hint:
                raise RuntimeError(f"{exc}\n\nHINT: {hint}") from exc
            raise

    # Piggyback dialog events on success responses so agents see them immediately.
    # Deduplicate by (title, text) and cap to most recent 10 unique events to prevent
    # unbounded accumulation (e.g. persistent dialog loops causing 2GB "response too large" errors).
    MAX_DIALOG_EVENTS = 10
    try:
        events = _get_connection().notifications
        if events:
            seen: dict[tuple[str, str], int] = {}
            deduped: list[dict[str, Any]] = []
            for event in events:
                key = (event.get("title", ""), event.get("text", ""))
                if key not in seen:
                    seen[key] = 1
                    entry = dict(event)
                    entry["count"] = 1
                    deduped.append(entry)
                else:
                    seen[key] += 1
                    # Update the last entry's count in place
                    if deduped and deduped[-1].get("title") == event.get("title") and deduped[-1].get("text") == event.get("text"):
                        deduped[-1]["count"] = seen[key]
                    else:
                        # The duplicate is not at the end; find and update
                        for entry in deduped:
                            if entry.get("title") == event.get("title") and entry.get("text") == event.get("text"):
                                entry["count"] = seen[key]
                                break
            # Cap to most recent MAX_DIALOG_EVENTS
            result["_dialog_events"] = deduped[-MAX_DIALOG_EVENTS:]
    except Exception:
        pass
    return result


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def estimate_fishing_mortality(catch: float, biomass: float) -> float:
    """
    Estimate fishing mortality (F) as Catch (C) / Biomass (B).
    Formula: F = C / B
    """
    return ecopath_estimate_fishing_mortality(catch, biomass)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def estimate_total_mortality(
    fishing_mortality: float, natural_mortality: float
) -> float:
    """
    Estimate total mortality (Z) as the sum of fishing mortality (F) and natural mortality (M).
    Formula: Z = F + M
    """
    return ecopath_estimate_total_mortality(fishing_mortality, natural_mortality)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def estimate_catch(biomass: float, fishing_mortality: float) -> float:
    """
    Estimate catch (C) as Biomass (B) * Fishing Mortality (F).
    Formula: C = B * F
    """
    return ecopath_estimate_catch(biomass, fishing_mortality)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def estimate_production(biomass: float, pb_ratio: float) -> float:
    """
    Estimate total production (P) as Biomass (B) * Production/Biomass ratio (P/B).
    Formula: P = B * (P/B)
    """
    return ecopath_estimate_production(biomass, pb_ratio)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def estimate_consumption(biomass: float, qb_ratio: float) -> float:
    """
    Estimate total consumption (Q) as Biomass (B) * Consumption/Biomass ratio (Q/B).
    Formula: Q = B * (Q/B)
    """
    return ecopath_estimate_consumption(biomass, qb_ratio)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def estimate_average_longevity(pb_ratio: float) -> float:
    """
    Estimate average longevity as the inverse of the P/B ratio (expressed in years).
    Formula: Longevity = 1 / (P/B)
    """
    return ecopath_estimate_average_longevity(pb_ratio)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def query_fishbase(
    genus: str, species: str, tables: list[str] | None = None
) -> dict[str, Any]:
    """Query FishBase for fish species trait data (growth, mortality, trophic level, diet).

    Queries FishBase via DuckDB against Source Cooperative S3 Parquet files.
    Does NOT require an EwE connection — works standalone.

    Args:
        genus: Genus name (e.g., "Gadus").
        species: Species epithet (e.g., "morhua").
        tables: Optional list of tables to query: "popgrowth", "ecology", "diet".
                Defaults to all three.

    Returns:
        Dict keyed by table name with list of row dicts containing trait data,
        or {"error": "..."} on failure.
    """
    return await asyncio.to_thread(_query_fishbase, genus, species, tables)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def query_sealifebase(
    genus: str, species: str, tables: list[str] | None = None
) -> dict[str, Any]:
    """Query SeaLifeBase for non-fish marine species trait data (growth, mortality, trophic level, diet).

    Queries SeaLifeBase via DuckDB against Source Cooperative S3 Parquet files.
    Same structure as FishBase but covers invertebrates, marine mammals, etc.
    Does NOT require an EwE connection — works standalone.

    Args:
        genus: Genus name (e.g., "Octopus").
        species: Species epithet (e.g., "vulgaris").
        tables: Optional list of tables to query: "popgrowth", "ecology", "diet".
                Defaults to all three.

    Returns:
        Dict keyed by table name with list of row dicts containing trait data,
        or {"error": "..."} on failure.
    """
    return await asyncio.to_thread(_query_sealifebase, genus, species, tables)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def query_globi(
    taxon_name: str, interaction_type: str = "preysOn"
) -> dict[str, Any]:
    """Query GLOBI for species interaction data (predator-prey relationships).

    Searches the Global Biotic Interactions database for trophic and other
    biotic interactions. Returns up to 100 interactions.
    Does NOT require an EwE connection — works standalone.

    Args:
        taxon_name: Full scientific name (e.g., "Gadus morhua").
        interaction_type: Type of interaction. Common: "preysOn", "eatenBy",
                         "parasiteOf", "hostOf". Defaults to "preysOn".

    Returns:
        Dict with source, interaction_type, interactions list, count, and
        optional note if capped. Returns {"error": "..."} on failure.
    """
    return await asyncio.to_thread(_query_globi, taxon_name, interaction_type)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def ping() -> dict[str, Any]:
    """Check connectivity to the EwE plugin."""
    return await _send("ping")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_dialog_events() -> list[dict[str, Any]]:
    """Fetch recent dialog events captured by the EwE dialog watcher.

    Returns a list of dialog events, each containing:
    - title: dialog window title
    - text: dialog body text (from STATIC controls)
    - is_risky: whether the dialog contains risky keywords
    - safe_to_dismiss: whether the watcher considered it safe
    - has_ok_button: whether an OK/Yes button was found
    - timestamp_utc: when the dialog was detected

    Sends a ping to trigger piggyback delivery from the plugin.
    Returns an empty list if no dialogs were detected.
    """
    try:
        await _send("ping")
    except (ConnectionError, OSError, socket.timeout, RuntimeError):
        logger.warning("get_dialog_events ping failed — transport may be down")
    try:
        async with _send_lock:
            return _get_connection().notifications
    except (ConnectionError, OSError, RuntimeError):
        logger.warning("get_dialog_events drain failed — transport may be down")
        return []


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def diagnose() -> dict[str, Any]:
    """Run diagnostic checks on the EwE MCP stack."""
    return enrich_diagnose(await _send("diagnose"))


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_ewe_info() -> dict[str, Any]:
    """Get EwE core/plugin version information."""
    return await _send("get_ewe_info")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_runtime_state() -> dict[str, Any]:
    """Get current EwE runtime state, model status, and command summary."""
    return await _send("get_runtime_state")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_message_log() -> dict[str, Any]:
    """Get recent EwE MCP audit log entries."""
    return await _send("get_message_log")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def list_ui_commands() -> dict[str, Any]:
    """List EwE UI commands known to the command handler."""
    return await _send("list_ui_commands")


@mcp.tool()
async def invoke_ui_command(
    name: str | None = None,
    command: str | None = None,
    tag: str | None = None,
) -> dict[str, Any]:
    """Invoke a named EwE UI command.

    Args:
        name: The EwE UI command name (preferred).
        command: Alias for 'name' (backward compatibility).
        tag: Optional tag passed as cCommand.Tag.
    """
    cmd_name = name or command
    if not cmd_name:
        raise ValueError("Either 'name' or 'command' parameter is required")
    params: dict[str, Any] = {"name": cmd_name}
    if tag is not None:
        params["tag"] = tag
    return await _send("invoke_ui_command", params)

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def list_navigation_nodes() -> dict[str, Any]:
    """List EwE navigation tree nodes that can be selected with navigate_to_node."""
    return await _send("list_navigation_nodes")


@mcp.tool()
async def navigate_to_node(
    id: str | None = None,
    text: str | None = None,
    path: str | None = None,
    query: str | None = None,
) -> dict[str, Any]:
    """Navigate EwE to a UI screen by navigation node id/name, visible text, path, or search query."""
    params = {
        key: value
        for key, value in {"id": id, "text": text, "path": path, "query": query}.items()
        if value
    }
    if not params:
        raise ValueError("Provide one of id, text, path, or query.")
    return await _send("navigate_to_node", params)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_active_view() -> dict[str, Any]:
    """Return the currently selected EwE navigation node and active form details where available."""
    return await _send("get_active_view")


@mcp.tool()
async def load_model(path: str) -> dict[str, Any]:
    """Load an EwE model file inside the running EwE instance.

    If the load times out, EwE may be showing a modal dialog
    (e.g., a database conversion prompt). Try restarting EwE
    with dialog watcher enabled (EWE_MCP_DIALOG_WATCHER=1) or
    convert the model manually before loading.

    After batch-creating groups or fleets (e.g., via create_ecopath_groups or create_ecopath_fleets),
    save then reload the model to make new entities visible in list queries.
    """
    _reject_path_traversal(path)
    return await _send("load_model", {"path": path}, timeout=TIMEOUT_LONG)


@mcp.tool(annotations=ToolAnnotations(destructiveHint=True))
async def create_model(
    path: str,
    model_name: str | None = None,
    author: str | None = None,
) -> dict[str, Any]:
    """Create a new empty EwE model at the specified path.

    Uses the access database engine to create a blank EwE model with
    the required schema (one default Detritus group).  After creation
    the model is automatically loaded in the running EwE instance.

    Args:
        path: Full path where the .EwEmdb file will be created.
        model_name: Optional display name (defaults to filename).
        author: Optional author string (defaults to EwE's current default).
    """
    if not path.lower().endswith(".ewemdb"):
        raise ValueError("path must have a .EwEmdb extension")
    _reject_path_traversal(path)
    params: dict[str, Any] = {"path": path}
    if model_name is not None:
        params["model_name"] = model_name
    if author is not None:
        params["author"] = author
    return await _send("create_model", params, timeout=TIMEOUT_LONG)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_model_info() -> dict[str, Any]:
    """Get the currently loaded model summary."""
    return await _send("get_model_info")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def has_changes() -> dict[str, Any]:
    """Return whether EwE currently has unsaved model changes."""
    return await _send("has_changes")


@mcp.tool(annotations=ToolAnnotations(destructiveHint=True))
async def discard_changes() -> dict[str, Any]:
    """Clear EwE unsaved-change flags without saving. Intended for live test cleanup."""
    return await _send("discard_changes")


async def _validate_before_save() -> str | None:
    """Check model groups for sentinel values that would cause a save failure.

    Returns None if validation passes, or an error string describing the issue.
    """
    try:
        groups_result = await _send("list_ecopath_groups")
    except Exception as exc:
        # If we cannot list groups, let the save proceed and fail naturally
        logger.warning("Pre-save validation skipped (cannot list groups): %s", exc)
        return None

    groups = groups_result.get("groups", [])
    if not groups:
        return None  # No groups to validate

    sentinel_fields = {"biomass_area", "pb", "qb", "ee"}
    problematic: list[str] = []

    for group in groups:
        group_sentinels: list[str] = []
        inp = group.get("input", {})
        for field in sentinel_fields:
            val = inp.get(field)
            if val is not None and val == -9999:
                group_sentinels.append(field)
        if group_sentinels:
            problematic.append(
                f"{group['name']} (idx {group['index']}): {', '.join(group_sentinels)}"
            )

    if problematic:
        return (
            "Cannot save: the following groups have sentinel (-9999) values in critical fields, "
            "which would corrupt the model database on save:\n"
            + "\n".join(f"  - {p}" for p in problematic)
            + "\n\nUse set_values(domain='ecopath_group', index=<idx>, "
            "values={'biomass': <value>, 'pb': <value>, 'qb': <value>, 'ee': <value>}, "
            "navigate=False) to set valid values before saving."
        )
    return None


@mcp.tool()
async def save_model(path: str | None = None) -> dict[str, Any]:
    """Save the current model, optionally to a new path.

    After batch-creating groups or fleets (e.g., via create_ecopath_groups or create_ecopath_fleets),
    you must save + reload for the new entities to appear in list queries.

    Before saving, validates that no groups have sentinel (-9999) values in
    critical fields (biomass, P/B, Q/B, EE) which would corrupt the database.
    """
    if path is not None:
        _reject_path_traversal(path)
    params = {"path": path} if path else {}
    error = await _validate_before_save()
    if error:
        raise ValueError(error)
    return await _send("save_model", params, timeout=TIMEOUT_LONG)


@mcp.tool()
async def save_model_as(path: str) -> dict[str, Any]:
    """Save the current model to a new path.

    Before saving, validates that no groups have sentinel (-9999) values in
    critical fields (biomass, P/B, Q/B, EE) which would corrupt the database.
    """
    _reject_path_traversal(path)
    error = await _validate_before_save()
    if error:
        raise ValueError(error)
    return await _send("save_model_as", {"path": path}, timeout=TIMEOUT_LONG)


@mcp.tool(annotations=ToolAnnotations(destructiveHint=True))
async def close_model() -> dict[str, Any]:
    """Close the current model."""
    return await _send("close_model")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_core_counters() -> dict[str, Any]:
    """Get EwE core counters such as groups/fleets where available."""
    return await _send("get_core_counters")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def list_domains() -> dict[str, Any]:
    """List implemented EwE data domains."""
    return await _send("list_domains")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def list_entities(domain: str) -> dict[str, Any]:
    """List entities for a supported EwE domain."""
    return await _send("list_entities", {"domain": domain})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_entity(domain: str, index: int) -> dict[str, Any]:
    """Get one entity from a supported EwE domain. Domain and index are both required."""
    params: dict[str, Any] = {"domain": domain, "index": index}
    return await _send("get_entity", params)

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def list_variables(domain: str, index: int | None = None) -> dict[str, Any]:
    """List readable/writable variables exposed by an EwE entity domain."""
    params: dict[str, Any] = {"domain": domain}
    if index is not None:
        params["index"] = index
    return await _send("list_variables", params)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_variable_metadata(
    domain: str, variable: str, index: int | None = None
) -> dict[str, Any]:
    """Get metadata for one variable on an EwE entity domain."""
    params: dict[str, Any] = {"domain": domain, "variable": variable}
    if index is not None:
        params["index"] = index
    return await _send("get_variable_metadata", params)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_values(
    domain: str,
    index: int | None = None,
    variables: list[str] | None = None,
    navigate: bool = True,
) -> dict[str, Any]:
    """Read values from an EwE entity. Pass navigate=False to avoid changing the visible EwE screen.

    When index is provided, variables is required to avoid returning all fields.
    Use list_variables(domain, index) to see available field names.

    For reading multiple entities at once, use get_values_range or batch_commands.
    """
    if index is not None and not variables:
        raise ValueError(
            "variables is required when index is provided. "
            "Use list_variables(domain, index) to see available fields."
        )
    params: dict[str, Any] = {"domain": domain, "navigate": navigate}
    if index is not None:
        params["index"] = index
    if variables is not None:
        params["variables"] = variables
    return await _send("get_values", params)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def validate_values(
    domain: str, values: dict[str, Any], index: int | None = None
) -> dict[str, Any]:
    """Validate proposed values against writable EwE entity properties without mutating."""
    params: dict[str, Any] = {"domain": domain, "values": values}
    if index is not None:
        params["index"] = index
    result = await _send("validate_values", params)
    range_results = validate_ewe_ranges(domain, values)
    for var_name, range_result in range_results.items():
        if not range_result["valid"]:
            if "results" not in result:
                result["results"] = {}
            result["results"][var_name] = range_result
    result["domain"] = normalize_domain_name(result.get("domain", domain))
    return result


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_indexed_value(
    domain: str,
    variable: str,
    indices: list[int],
    index: int | None = None,
    navigate: bool = True,
) -> dict[str, Any]:
    """Read one indexed EwE value. Pass navigate=False to avoid changing the visible EwE screen."""
    params: dict[str, Any] = {
        "domain": domain,
        "variable": variable,
        "indices": indices,
        "navigate": navigate,
    }
    if index is not None:
        params["index"] = index
    return await _send("get_indexed_value", params)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def validate_indexed_value(
    domain: str,
    variable: str,
    indices: list[int],
    value: Any,
    index: int | None = None,
) -> dict[str, Any]:
    """Validate one indexed EwE value without mutating."""
    params: dict[str, Any] = {
        "domain": domain,
        "variable": variable,
        "indices": indices,
        "value": value,
    }
    if index is not None:
        params["index"] = index
    result = await _send("validate_indexed_value", params)
    range_results = validate_ewe_ranges(domain, {variable: value})
    for var_name, range_result in range_results.items():
        if not range_result["valid"]:
            result["valid"] = False
            result["reason"] = range_result["reason"]
    result["domain"] = normalize_domain_name(result.get("domain", domain))
    return result


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def search_entities(
    domain: str,
    name_contains: str | None = None,
    search: str | None = None,
    property_filters: dict[str, str] | None = None,
    max_results: int = 100,
) -> dict[str, Any]:
    """Search entities in an EwE domain by name substring and/or property value filters."""
    if max_results < 1:
        raise ValueError("max_results must be a positive integer")
    params: dict[str, Any] = {"domain": domain, "max_results": max_results}
    effective_name = name_contains or search
    if effective_name is not None:
        params["name_contains"] = effective_name
    if property_filters is not None:
        params["property_filters"] = property_filters
    result = await _send("search_entities", params)
    result["domain"] = normalize_domain_name(result.get("domain", domain))
    return result


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_values_range(
    domain: str,
    variables: list[str] | None = None,
    start_index: int = 1,
    end_index: int | None = None,
    max_entities: int = 50,
    navigate: bool = False,
) -> dict[str, Any]:
    """Read values from a range of entities in an EwE domain in one round-trip.

    More efficient than calling get_values N times for contiguous indices.
    Returns entity index, name, and requested variable values for each entity
    in the range [start_index, end_index].

    Args:
        domain: EwE entity domain (e.g. "ecopath_groups", "ecosim_group_inputs").
        variables: Optional list of variable names to read. If omitted, reads all
            scalar properties (same as get_values with no variables).
        start_index: First 1-based entity index (default 1).
        end_index: Last 1-based entity index. If omitted, reads to end of domain.
        max_entities: Safety cap on number of entities returned (default 50).
        navigate: Whether to navigate the EwE UI (default False for bulk reads).
    """
    if max_entities < 1:
        raise ValueError("max_entities must be a positive integer")
    params: dict[str, Any] = {
        "domain": domain,
        "start_index": start_index,
        "max_entities": max_entities,
        "navigate": navigate,
    }
    if end_index is not None:
        params["end_index"] = end_index
    if variables is not None:
        params["variables"] = variables
    result = await _send("get_values_range", params)
    result["domain"] = normalize_domain_name(result.get("domain", domain))
    return result


@mcp.tool(annotations=ToolAnnotations(destructiveHint=True))
async def set_values_range(
    domain: str,
    values: dict[str, Any],
    start_index: int = 1,
    end_index: int | None = None,
    max_entities: int = 50,
    navigate: bool = False,
) -> dict[str, Any]:
    """Set the same values on a range of entities in an EwE domain in one round-trip.

    Applies the same value dict to every entity in the range [start_index, end_index].
    More efficient than calling set_values N times for bulk updates.

    Args:
        domain: EwE entity domain (e.g. "ecopath_groups", "ecosim_group_inputs").
        values: Dict of {variable_name: value} to set on each entity.
        start_index: First 1-based entity index (default 1).
        end_index: Last 1-based entity index. If omitted, sets on all entities.
        max_entities: Safety cap on number of entities modified (default 50).
        navigate: Whether to navigate the EwE UI (default False for bulk writes).
    """
    if max_entities < 1:
        raise ValueError("max_entities must be a positive integer")
    params: dict[str, Any] = {
        "domain": domain,
        "values": values,
        "start_index": start_index,
        "max_entities": max_entities,
        "navigate": navigate,
    }
    if end_index is not None:
        params["end_index"] = end_index
    result = await _send("set_values_range", params, timeout=TIMEOUT_LONG)
    result["domain"] = normalize_domain_name(result.get("domain", domain))
    await refresh_ui()
    return result


async def _discover_dynamic_writable_fields() -> dict[str, set[str]]:
    """Best-effort metadata discovery; caches and returns result."""
    global _writable_fields_cache
    if _writable_fields_cache is not None:
        return _writable_fields_cache
    dynamic: dict[str, set[str]] = {}
    for domain in DOMAIN_CONFIG:
        try:
            variables = await list_variables(domain=domain)
        except Exception:
            continue
        writable = {
            v.get("name")
            for v in variables.get("variables", [])
            if isinstance(v, dict) and v.get("name") and bool(v.get("writable", False))
        }
        if writable:
            dynamic[domain] = writable
    _writable_fields_cache = dynamic
    return dynamic


def _get_writable_fields(domain: str) -> frozenset:
    """Get writable fields for a domain: dynamic-cached first, static fallback.
    Merges both to catch aliases (e.g. 'pb' vs 'PBInput')."""
    static = frozenset(STATIC_WRITABLE_FIELDS.get(domain, set()))
    if _writable_fields_cache is not None and domain in _writable_fields_cache:
        return frozenset(_writable_fields_cache[domain]) | static
    return static


def _csv_float(val: Any) -> str:
    """Format a float for CSV output with consistent .6f precision.

    Returns empty string for None or sentinel (-9999) values.
    """
    if val is None or val == -9999:
        return ""
    return f"{float(val):.6f}"


def _empty_batch_summary() -> dict[str, int]:
    return {
        "total": 0,
        "applied": 0,
        "failed": 0,
        "not_found": 0,
        "ambiguous": 0,
        "validation_failed": 0,
        "skipped": 0,
    }


def _final_batch_status(summary: dict[str, int]) -> str:
    if summary["failed"] == 0:
        return "success"
    if summary["applied"] == 0:
        return "failed"
    return "partial_success"


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def list_name_batch_capabilities() -> dict[str, Any]:
    """List supported domains, match fields, and writable fields for name-based batch updates.

    Use this first to discover which domains and fields are available before
    calling set_values_by_name_batch.
    """
    dynamic = await _discover_dynamic_writable_fields()
    return default_capabilities(dynamic_fields=dynamic)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def list_ecopath_groups() -> dict[str, Any]:
    """List Ecopath functional groups with key read-only input/output values."""
    return await _send("list_ecopath_groups")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_ecopath_group(index: int) -> dict[str, Any]:
    """Get one Ecopath functional group with auto-enriched output.

    Includes diet composition, output values (if Ecopath has run), and
    enrichment metadata (EE, trophic level, P/B, GE). Enrichment status
    is reported in the `_enrichment` field.
    """
    return await _send("get_ecopath_group_enriched", {"index": index})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def list_ecopath_fleets() -> dict[str, Any]:
    """List Ecopath fishing fleets."""
    return await _send("list_ecopath_fleets")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_ecopath_fleet(index: int) -> dict[str, Any]:
    """Get one Ecopath fleet, including non-zero landings/discards by group."""
    return await _send("get_ecopath_fleet", {"index": index})


@mcp.tool(annotations=ToolAnnotations(destructiveHint=True))
async def create_ecopath_group(
    name: str,
    biomass: float | None = None,
    pb_ratio: float | None = None,
    qb_ratio: float | None = None,
    ee: float | None = None,
    group_type: int | None = None,
) -> dict[str, Any]:
    """Create a new Ecopath functional group.

    Args:
        name: Name for the new group.
        biomass: Initial biomass value.
        pb_ratio: Production/biomass ratio.
        qb_ratio: Consumption/biomass ratio.
        ee: Ecotrophic efficiency.
        group_type: Group type (0=consumer, 1=producer, 2=detritus). Required.

    Note: The new group is written to the model database but in-memory core
    counts are not immediately refreshed. The group may not appear in
    list_ecopath_groups until the model is saved and reloaded. Use
    save_model followed by load_model to see the new group.

    For creating multiple groups at once, use create_ecopath_groups.
    """
    if group_type is None:
        raise ValueError("group_type is required (0=consumer, 1=producer, 2=detritus)")
    if group_type not in (0, 1, 2):
        raise ValueError(f"group_type must be 0 (consumer), 1 (producer), or 2 (detritus), got {group_type}")

    params: dict[str, Any] = {"name": name}
    if biomass is not None:
        params["biomass"] = biomass
    if pb_ratio is not None:
        params["pb_ratio"] = pb_ratio
    if qb_ratio is not None:
        params["qb_ratio"] = qb_ratio
    if ee is not None:
        params["ee"] = ee
    params["group_type"] = group_type
    return await _send("create_ecopath_group", params)


@mcp.tool(annotations=ToolAnnotations(destructiveHint=True))
async def create_ecopath_groups(groups: list[dict[str, Any]], auto_reload: bool = True) -> dict[str, Any]:
    """Create multiple Ecopath functional groups in one call.

    Each group dict requires:
        name (str): Name for the new group.
        group_type (int): 0=consumer, 1=producer, 2=detritus.

    Optional fields per group:
        biomass (float): Initial biomass value.
        pb_ratio (float): Production/biomass ratio.
        qb_ratio (float): Consumption/biomass ratio.
        ee (float): Ecotrophic efficiency.

    Creates groups one at a time (each is written directly to the database by
    the C# plugin), then optionally saves + reloads to refresh in-memory core
    counts.  Set `auto_reload=False` to skip the final save+load for speed
    (useful when creating many groups at once — save+load separately after).

    Returns a summary of created/failed groups.

    For creating a single group, use create_ecopath_group instead.
    """
    if not groups:
        raise ValueError("At least one group definition is required")

    created: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    model_path: str | None = None

    for i, g in enumerate(groups):
        try:
            if not isinstance(g, dict):
                raise ValueError(f"Group definition must be a dict, got {type(g).__name__}")
            name = g.get("name", "")
            if not name:
                raise ValueError("Group definition missing required 'name' field")
            group_type = g.get("group_type")
            if group_type not in (0, 1, 2):
                raise ValueError(
                    f"group_type must be 0 (consumer), 1 (producer), or 2 (detritus), "
                    f"got {group_type!r}"
                )
            params: dict[str, Any] = {"name": name, "group_type": group_type}
            for opt in ("biomass", "pb_ratio", "qb_ratio", "ee"):
                val = g.get(opt)
                if val is not None:
                    params[opt] = val
            result = await _send("create_ecopath_group", params)
            created.append({"name": name, "index": result.get("group_index")})
        except Exception as exc:
            errors.append({
                "index": i,
                "name": g.get("name", f"<group {i}>"),
                "error": str(exc),
            })
            continue

    # Save + reload at the end if auto_reload is enabled (default).
    # Skip for speed when creating many groups — caller saves+reloads separately.
    if auto_reload and created:
        try:
            info = await _send("get_model_info")
            model_path = info.get("path") or info.get("file_name")
            if model_path:
                await _send("save_model", timeout=TIMEOUT_LONG)
                await _send("load_model", {"path": model_path}, timeout=TIMEOUT_LONG)
        except Exception as exc:
            logger.warning("Final save/reload after group creation failed: %s — "
                          "groups are in the database, reload model to see them", exc)

    return {
        "status": "success" if len(errors) == 0 else "partial_success" if created else "failed",
        "total": len(groups),
        "created": len(created),
        "failed": len(errors),
        "created_groups": created,
        "errors": errors if errors else None,
    }


@mcp.tool(annotations=ToolAnnotations(destructiveHint=True))
async def create_ecopath_fleet(name: str) -> dict[str, Any]:
    """Create a new Ecopath fishing fleet.

    Note: The new fleet is written to the model database but in-memory core
    counts are not immediately refreshed. The fleet may not appear in
    list_ecopath_fleets until the model is saved and reloaded. Use
    save_model followed by load_model to see the new fleet.

    For creating multiple fleets at once, use create_ecopath_fleets.
    """
    return await _send("create_ecopath_fleet", {"name": name})


@mcp.tool(annotations=ToolAnnotations(destructiveHint=True))
async def create_ecopath_fleets(names: list[str], auto_reload: bool = True) -> dict[str, Any]:
    """Create multiple Ecopath fishing fleets in one call.

    Creates fleets one at a time (each is written directly to the database by
    the C# plugin), then optionally saves + reloads to refresh in-memory core
    counts.  Set `auto_reload=False` to skip the final save+load for speed.
    Returns a summary of created/failed fleets.

    For creating a single fleet, use create_ecopath_fleet instead.
    """
    if not names:
        raise ValueError("At least one fleet name is required")

    created: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    model_path: str | None = None

    for i, name in enumerate(names):
        try:
            if not name or not name.strip():
                raise ValueError("Fleet name must be a non-empty string")
            result = await _send("create_ecopath_fleet", {"name": name.strip()})
            created.append({"name": name.strip(), "index": result.get("fleet_index")})
        except Exception as exc:
            errors.append({
                "index": i,
                "name": name,
                "error": str(exc),
            })
            continue

    # Save + reload at the end if auto_reload is enabled (default).
    # Skip for speed — caller saves+reloads separately with auto_reload=False.
    if auto_reload and created:
        try:
            info = await _send("get_model_info")
            model_path = info.get("path") or info.get("file_name")
            if model_path:
                await _send("save_model", timeout=TIMEOUT_LONG)
                await _send("load_model", {"path": model_path}, timeout=TIMEOUT_LONG)
        except Exception as exc:
            logger.warning("Final save/reload after fleet creation failed: %s — "
                          "fleets are in the database, reload model to see them", exc)

    return {
        "status": "success" if not errors else "partial_failure",
        "total": len(names),
        "created": len(created),
        "failed": len(errors),
        "created_fleets": created,
        "errors": errors or None,
    }


@mcp.tool(annotations=ToolAnnotations(destructiveHint=True))
async def import_time_series(
    data: str | None = None,
    path: str | None = None,
    import_type: str = "biomass",
    dataset_name: str = "MCP Import",
    delimiter: str = ",",
    decimal_separator: str = ".",
    interval: str = "annual",
    enable_on_load: bool = True,
    pool_indices: list[int] | None = None,
) -> dict[str, Any]:
    """Import time series data into EwE. Accepts CSV string or path on EwE host."""
    import csv
    import io as _io
    import_type = import_type.lower()
    if import_type not in _VALID_IMPORT_TYPES:
        raise ValueError(f"import_type must be one of {sorted(_VALID_IMPORT_TYPES)}, got '{import_type}'")
    if delimiter == decimal_separator:
        raise ValueError(f"delimiter and decimal_separator cannot be the same")
    if not data and path:
        _reject_path_traversal(path)
        import_result = await _send("read_host_file", {"path": path})
        data = import_result.get("content", "")
        if not data:
            raise ValueError(f"File at {path} is empty or unreadable")
    if not data or not data.strip():
        raise ValueError("data must not be empty")

    # Auto-detect format: if the first cell of the header starts with "year"
    # (case-insensitive), treat as simple row-per-year and transform it.
    reader = csv.reader(_io.StringIO(data), delimiter=delimiter)
    rows = list(reader)
    if len(rows) >= 2 and rows[0] and rows[0][0].strip().lower().startswith("year"):
        # Simple row-per-year format — transform to EwE column-oriented CSV
        header = rows[0]
        series_names = [h.strip() for h in header[1:]]
        n_series = len(series_names)
        if n_series == 0:
            raise ValueError(
                "Simple CSV must have at least one data column after the year column"
            )

        # Pool indices: use provided list or default to sequential
        if pool_indices is None:
            pool_indices = list(range(1, n_series + 1))
        elif len(pool_indices) != n_series:
            raise ValueError(
                f"pool_indices length {len(pool_indices)} must match number of series {n_series}"
            )

# Map import_type to EwE type string or integer code.
        # Names must match EwE's eTimeSeriesAliases enum (case-insensitive):
        # BiomassAbs/BAbs=1, Catch/C=6, Effort=3, Forcing=2, etc.
        # Avoid ambiguous short names like "f" (matches FishingMort=5, not Forcing=2).
        _TYPE_MAP: dict[str, str] = {
            "biomass": "BiomassAbs",
            "catch": "Catch",
            "effort": "Effort",
            "forcing": "Forcing",
        }
        type_code = _TYPE_MAP.get(import_type, import_type)
        # Effort time series reference fleets; all others reference groups
        pool_label = "fleet" if import_type == "effort" else "group"

        # Build the EwE CSV
        ewe_lines: list[str] = []
        ewe_lines.append(delimiter + delimiter.join(series_names))
        ewe_lines.append("weight" + delimiter + delimiter.join(["1"] * n_series))
        ewe_lines.append(
            pool_label + delimiter + delimiter.join(str(p) for p in pool_indices)
        )
        ewe_lines.append("type" + delimiter + delimiter.join([type_code] * n_series))
        for row in rows[1:]:
            if not any(cell.strip() for cell in row):
                continue
            year = row[0].strip()
            try:
                int(year)
            except (ValueError, TypeError):
                raise ValueError(f"Year value {year!r} is not a valid integer") from None
            values = [cell.strip() for cell in row[1:]]
            if len(values) != n_series:
                raise ValueError(f"Row for year {year!r} has {len(values)} data columns, expected {n_series}")
            ewe_lines.append(year + delimiter + delimiter.join(values))
        data = "\n".join(ewe_lines)

    return await _send(
        "import_time_series",
        {
            "data": data,
            "import_type": import_type,
            "dataset_name": dataset_name,
            "delimiter": delimiter,
            "decimal_separator": decimal_separator,
            "interval": interval,
            "enable_on_load": enable_on_load,
        },
        timeout=TIMEOUT_LONG,
    )


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def list_ecosim_scenarios() -> dict[str, Any]:
    """List Ecosim scenarios in the loaded model."""
    return await _send("list_ecosim_scenarios")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_ecosim_scenario(index: int) -> dict[str, Any]:
    """Get Ecosim scenario metadata by one-based index."""
    return await _send("get_ecosim_scenario", {"index": index})


@mcp.tool(annotations=ToolAnnotations(destructiveHint=True))
async def load_ecosim_scenario(index: int) -> dict[str, Any]:
    """Load an Ecosim scenario by one-based index."""
    return await _send("load_ecosim_scenario", {"index": index}, timeout=TIMEOUT_LONG)


@mcp.tool(annotations=ToolAnnotations(destructiveHint=True))
async def load_ecospace_scenario(index: int) -> dict[str, Any]:
    """Load an Ecospace scenario by one-based index."""
    return await _send("load_ecospace_scenario", {"index": index}, timeout=TIMEOUT_LONG)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def list_ecospace_scenarios() -> dict[str, Any]:
    """List Ecospace scenarios in the loaded model."""
    return await _send("list_ecospace_scenarios")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_ecospace_scenario(index: int) -> dict[str, Any]:
    """Get Ecospace scenario metadata by one-based index."""
    return await _send("get_ecospace_scenario", {"index": index})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_ecospace_run_state() -> dict[str, Any]:
    """Get Ecospace run state: whether Ecospace is running, loaded, and has outputs."""
    return await _send("get_ecospace_run_state")


@mcp.tool(annotations=ToolAnnotations(destructiveHint=True))
async def run_ecospace() -> dict[str, Any]:
    """Run the active Ecospace scenario."""
    return await _send("run_ecospace", timeout=TIMEOUT_LONG)


@mcp.tool(annotations=ToolAnnotations(destructiveHint=True))
async def stop_ecospace() -> dict[str, Any]:
    """Stop the active Ecospace run."""
    return await _send("stop_ecospace")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def list_ecospace_group_inputs() -> dict[str, Any]:
    """List Ecospace group input parameters for the active scenario."""
    return await _send("list_ecospace_group_inputs")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_ecospace_group_input(index: int) -> dict[str, Any]:
    """Get one Ecospace group input parameter record."""
    return await _send("get_ecospace_group_input", {"index": index})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def list_ecospace_fleet_inputs() -> dict[str, Any]:
    """List Ecospace fleet input parameters for the active scenario."""
    return await _send("list_ecospace_fleet_inputs")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_ecospace_fleet_input(index: int) -> dict[str, Any]:
    """Get one Ecospace fleet input parameter record."""
    return await _send("get_ecospace_fleet_input", {"index": index})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_ecospace_group_results(
    index: int | None = None,
    variables: list[str] | None = None,
    start_time: int | None = None,
    end_time: int | None = None,
    list: bool = False,
) -> dict[str, Any]:
    """Read Ecospace group result time-series after a run.

    Set `list=True` to return results for all groups instead of a single
    group by index. When `list=True`, the `index` parameter is ignored.

    Known variable names: biomass, relative_biomass.
    If variables is None, all available variables are returned.
    Unknown variable names will be rejected by the EwE plugin with
    a descriptive error.
    """
    if not list and index is None:
        raise ValueError("Either 'index' or 'list=True' is required")
    params: dict[str, Any] = {}
    if index is not None:
        params["index"] = index
    if variables is not None:
        params["variables"] = variables
    if start_time is not None:
        params["start_time"] = start_time
    if end_time is not None:
        params["end_time"] = end_time
    if list:
        params["list"] = True
    return await _send("get_ecospace_group_results", params, timeout=TIMEOUT_LONG)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_ecospace_fleet_results(
    index: int | None = None,
    variables: list[str] | None = None,
    start_time: int | None = None,
    end_time: int | None = None,
    list: bool = False,
) -> dict[str, Any]:
    """Read Ecospace fleet result time-series after a run.

    Set `list=True` to return results for all fleets instead of a single
    fleet by index. When `list=True`, the `index` parameter is ignored.

    Known variable names: catch_biomass, value.
    If variables is None, all available variables are returned.
    Unknown variable names will be rejected by the EwE plugin with
    a descriptive error.
    """
    if not list and index is None:
        raise ValueError("Either 'index' or 'list=True' is required")
    params: dict[str, Any] = {}
    if index is not None:
        params["index"] = index
    if variables is not None:
        params["variables"] = variables
    if start_time is not None:
        params["start_time"] = start_time
    if end_time is not None:
        params["end_time"] = end_time
    if list:
        params["list"] = True
    return await _send("get_ecospace_fleet_results", params, timeout=TIMEOUT_LONG)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def list_ecospace_habitats() -> dict[str, Any]:
    """List Ecospace habitats in the active scenario.

    Note: Habitats may not be fully initialized for newly created Ecospace
    scenarios. Entities that cannot be resolved are filtered out with a
    warning.
    """
    result = await _send("list_ecospace_habitats")
    # Normalize the plugin payload to the tool's habitat naming.
    if isinstance(result, dict) and "habitats" not in result and "entities" in result:
        result["habitats"] = result.pop("entities")
    # Filter out entities that cannot be resolved
    if isinstance(result, dict) and isinstance(result.get("habitats"), list):
        valid = []
        for h in result["habitats"]:
            is_unavailable = isinstance(h, dict) and (
                h.get("status") == "entity_unavailable"
                or h.get("error", {}).get("code") == "entity_unavailable"
            )
            if not is_unavailable:
                valid.append(h)
            else:
                logger.warning(
                    "Filtered unresolved Ecospace habitat: %s (index %s)",
                    h.get("name", "unknown"),
                    h.get("index", "unknown"),
                )
        if len(valid) != len(result["habitats"]):
            result["filtered_count"] = len(result["habitats"]) - len(valid)
            result["note"] = (
                f"{result['filtered_count']} habitat(s) could not be resolved because the "
                "Ecospace scenario was recently created and habitats are not fully initialized. "
                "Save the scenario and configure the basemap to initialize habitat data."
            )
        result["habitats"] = valid
    return result


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_ecospace_habitat(index: int) -> dict[str, Any]:
    """Get one Ecospace habitat by one-based index.

    Note: Habitats may not be initialized for newly created Ecospace
    scenarios. If the habitat data cannot be resolved, a clear error
    message is returned.
    """
    try:
        return await _send("get_ecospace_habitat", {"index": index})
    except RuntimeError as exc:
        # _send wraps EwEMCPError in RuntimeError with HINT — check if the
        # underlying error was entity_unavailable and add Ecospace-specific context.
        cause = exc.__cause__ or exc
        if isinstance(cause, EwEMCPError) and cause.code == "entity_unavailable":
            raise RuntimeError(
                f"Ecospace habitat at index {index} could not be resolved. "
                "This typically happens when the Ecospace scenario was recently "
                "created and habitats have not been fully initialized. "
                "Ensure the scenario has been saved and the basemap is configured."
            ) from cause
        raise


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def list_ecospace_mpas() -> dict[str, Any]:
    """List Ecospace MPAs in the active scenario."""
    return await _send("list_ecospace_mpas")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_ecospace_mpa(index: int) -> dict[str, Any]:
    """Get one Ecospace MPA by one-based index."""
    return await _send("get_ecospace_mpa", {"index": index})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_ecospace_basemap() -> dict[str, Any]:
    """Get the Ecospace basemap for the active scenario."""
    return await _send("get_ecospace_basemap")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_ecospace_model_parameters() -> dict[str, Any]:
    """Get Ecospace model parameters for the active scenario."""
    return await _send("get_ecospace_model_parameters")


@mcp.tool(annotations=ToolAnnotations(destructiveHint=True))
async def create_ecospace_scenario(
    name: str,
    description: str = "",
    author: str = "",
    contact: str = "",
    rows: int = 30,
    cols: int = 30,
    latitude: float = 0.0,
    longitude: float = 0.0,
    cell_length: float = 0.0833,
) -> dict[str, Any]:
    """Create and load a new Ecospace scenario."""
    if rows < 1 or cols < 1:
        raise ValueError(f"rows and cols must be at least 1 (got rows={rows}, cols={cols})")
    return await _send(
        "create_ecospace_scenario",
        {
            "name": name,
            "description": description,
            "author": author,
            "contact": contact,
            "rows": rows,
            "cols": cols,
            "latitude": latitude,
            "longitude": longitude,
            "cell_length": cell_length,
        },
        timeout=TIMEOUT_LONG,
    )


@mcp.tool(annotations=ToolAnnotations(destructiveHint=True))
async def copy_ecospace_scenario(name: str, description: str = "") -> dict[str, Any]:
    """Copy the active Ecospace scenario under a new name and load the copy."""
    return await _send(
        "copy_ecospace_scenario",
        {"name": name, "description": description},
        timeout=TIMEOUT_LONG,
    )


@mcp.tool(annotations=ToolAnnotations(destructiveHint=True))
async def delete_ecospace_scenario(index: int) -> dict[str, Any]:
    """Delete an unloaded Ecospace scenario by one-based index."""
    return await _send(
        "delete_ecospace_scenario", {"index": index}, timeout=TIMEOUT_LONG
    )


@mcp.tool(annotations=ToolAnnotations(destructiveHint=True))
async def save_ecospace_scenario() -> dict[str, Any]:
    """Save the active Ecospace scenario."""
    return await _send("save_ecospace_scenario", timeout=TIMEOUT_LONG)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def wait_for_ecospace_completion(
    timeout_seconds: float = 300,
    poll_interval_seconds: float = 2.0,
) -> dict[str, Any]:
    """Wait for Ecospace to finish running, polling until completion or timeout.

    Returns the final Ecospace run state. Polls at the specified interval.
    Raises a timeout error if Ecospace doesn't complete within the timeout.
    """
    deadline = time.monotonic() + timeout_seconds
    last_state: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last_state = await _send("get_ecospace_run_state")
        if not last_state.get("is_ecospace_running") and last_state.get(
            "has_ecospace_ran"
        ):
            return last_state
        await asyncio.sleep(poll_interval_seconds)
    raise RuntimeError(
        f"Ecospace did not complete within {timeout_seconds}s. Last state: {last_state}"
    )


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_ecospace_map_summary() -> dict[str, Any]:
    """Get Ecospace basemap dimensional metadata without the full grid.

    Returns rows, cols, land_cells, water_cells, n_mpa, n_habitats, and n_regions.
    """
    return await _send("get_ecospace_map_summary")


@mcp.tool(annotations=ToolAnnotations(destructiveHint=True))
async def create_ecospace_mpa(name: str, is_active: bool = True) -> dict[str, Any]:
    """Create a new MPA in the active Ecospace scenario.

    Args:
        name: Name for the new MPA.
        is_active: Whether the MPA is enforced (closed to fishing). Defaults to True.
    """
    return await _send(
        "create_ecospace_mpa",
        {"name": name, "is_active": is_active},
        timeout=TIMEOUT_LONG,
    )


@mcp.tool(annotations=ToolAnnotations(destructiveHint=True))
async def set_ecospace_mpa_cells(
    mpa_index: int, cells: list[list[int]]
) -> dict[str, Any]:
    """Set MPA cell assignments on the Ecospace basemap.

    Each cell is a [row, col] pair (one-based indices). Cells are marked as
    part of the specified MPA.

    Args:
        mpa_index: One-based MPA index.
        cells: List of [row, col] pairs to assign to the MPA.
    """
    return await _send(
        "set_ecospace_mpa_cells",
        {"mpa_index": mpa_index, "cells": cells},
        timeout=TIMEOUT_LONG,
    )


@mcp.tool(annotations=ToolAnnotations(destructiveHint=True))
async def create_regions_from_mpas(region_count: int | None = None) -> dict[str, Any]:
    """Create Ecospace regions from MPA boundaries.

    Copies MPA cell assignments to the region layer so that each MPA
    becomes a region. If region_count is not specified, defaults to the
    number of MPAs.

    Args:
        region_count: Optional override for the number of regions.
    """
    params: dict[str, Any] = {}
    if region_count is not None:
        params["region_count"] = region_count
    return await _send("create_regions_from_mpas", params, timeout=TIMEOUT_LONG)


@mcp.tool(annotations=ToolAnnotations(destructiveHint=True))
async def load_ecotracer_scenario(index: int) -> dict[str, Any]:
    """Load an Ecotracer scenario by one-based index."""
    return await _send(
        "load_ecotracer_scenario", {"index": index}, timeout=TIMEOUT_LONG
    )


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def list_ecotracer_scenarios() -> dict[str, Any]:
    """List Ecotracer scenarios in the loaded model."""
    return await _send("list_ecotracer_scenarios")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_ecotracer_scenario(index: int) -> dict[str, Any]:
    """Get Ecotracer scenario metadata by one-based index."""
    return await _send("get_ecotracer_scenario", {"index": index})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_ecotracer_run_state() -> dict[str, Any]:
    """Get Ecotracer run state: whether Ecotracer is running and has outputs."""
    return await _send("get_ecotracer_run_state")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_ecotracer_model_parameters() -> dict[str, Any]:
    """Get Ecotracer model parameters for the active scenario."""
    return await _send("get_ecotracer_model_parameters")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def list_ecotracer_group_inputs() -> dict[str, Any]:
    """List Ecotracer group input parameters for the active scenario."""
    return await _send("list_ecotracer_group_inputs")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_ecotracer_group_input(index: int) -> dict[str, Any]:
    """Get one Ecotracer group input parameter record."""
    return await _send("get_ecotracer_group_input", {"index": index})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_ecotracer_group_results(
    index: int | None = None,
    variables: list[str] | None = None,
    start_time: int | None = None,
    end_time: int | None = None,
    list: bool = False,
) -> dict[str, Any]:
    """Read Ecotracer group result time-series after a run.

    Set `list=True` to return results for all groups instead of a single
    group by index. When `list=True`, the `index` parameter is ignored.

    Known variable names: concentration, conbio.
    Defaults to at most 120 timesteps (start_time..start_time+119) when
    end_time is omitted, matching Ecosim/Ecospace result conventions.
    If variables is None, all available variables are returned.
    Unknown variable names will be rejected by the EwE plugin with
    a descriptive error.
    """
    if not list and index is None:
        raise ValueError("Either 'index' or 'list=True' is required")
    params: dict[str, Any] = {}
    if index is not None:
        params["index"] = index
    if variables is not None:
        params["variables"] = variables
    if start_time is not None:
        params["start_time"] = start_time
    if end_time is not None:
        params["end_time"] = end_time
    if list:
        params["list"] = True
    return await _send("get_ecotracer_group_results", params, timeout=TIMEOUT_LONG)


@mcp.tool(annotations=ToolAnnotations(destructiveHint=True))
async def create_ecotracer_scenario(
    name: str,
    description: str = "",
    author: str = "",
    contact: str = "",
) -> dict[str, Any]:
    """Create and load a new Ecotracer scenario."""
    return await _send(
        "create_ecotracer_scenario",
        {
            "name": name,
            "description": description,
            "author": author,
            "contact": contact,
        },
        timeout=TIMEOUT_LONG,
    )


@mcp.tool(annotations=ToolAnnotations(destructiveHint=True))
async def copy_ecotracer_scenario(name: str, description: str = "") -> dict[str, Any]:
    """Copy the active Ecotracer scenario under a new name and load the copy."""
    return await _send(
        "copy_ecotracer_scenario",
        {"name": name, "description": description},
        timeout=TIMEOUT_LONG,
    )


@mcp.tool(annotations=ToolAnnotations(destructiveHint=True))
async def delete_ecotracer_scenario(index: int) -> dict[str, Any]:
    """Delete an unloaded Ecotracer scenario by one-based index."""
    return await _send(
        "delete_ecotracer_scenario", {"index": index}, timeout=TIMEOUT_LONG
    )


@mcp.tool(annotations=ToolAnnotations(destructiveHint=True))
async def save_ecotracer_scenario() -> dict[str, Any]:
    """Save the active Ecotracer scenario."""
    return await _send("save_ecotracer_scenario", timeout=TIMEOUT_LONG)


@mcp.tool(annotations=ToolAnnotations(destructiveHint=True))
async def close_ecotracer_scenario() -> dict[str, Any]:
    """Close the active Ecotracer scenario."""
    return await _send("close_ecotracer_scenario")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_mse_parameters() -> dict[str, Any]:
    """Get MSE model parameters."""
    return await _send("get_mse_parameters")


@mcp.tool(annotations=ToolAnnotations(destructiveHint=True))
async def set_mse_parameters(values: dict[str, Any]) -> dict[str, Any]:
    """Set MSE model parameters (e.g., ntrials). Use get_mse_parameters() first."""
    return await _send("set_mse_parameters", {"values": values}, timeout=TIMEOUT_LONG)


async def list_mse_group_inputs() -> dict[str, Any]:
    """List MSE group input parameters."""
    return await _send("list_mse_group_inputs")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_mse_group_input(index: int) -> dict[str, Any]:
    """Get one MSE group input parameter record."""
    return await _send("get_mse_group_input", {"index": index})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def list_mse_fleet_inputs() -> dict[str, Any]:
    """List MSE fleet input parameters."""
    return await _send("list_mse_fleet_inputs")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_mse_fleet_input(index: int) -> dict[str, Any]:
    """Get one MSE fleet input parameter record."""
    return await _send("get_mse_fleet_input", {"index": index})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_mse_run_state() -> dict[str, Any]:
    """Get MSE run state."""
    return await _send("get_mse_run_state")


@mcp.tool(annotations=ToolAnnotations(destructiveHint=True))
async def run_mse() -> dict[str, Any]:
    """Run the active MSE scenario."""
    return await _send("run_mse", timeout=TIMEOUT_LONG)


@mcp.tool(annotations=ToolAnnotations(destructiveHint=True))
async def stop_mse() -> dict[str, Any]:
    """Stop the active MSE run."""
    return await _send("stop_mse")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_mse_group_results(index: int) -> dict[str, Any]:
    """Get MSE group results by one-based index."""
    return await _send("get_mse_group_results", {"index": index})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_mse_fleet_results(index: int) -> dict[str, Any]:
    """Get MSE fleet results by one-based index."""
    return await _send("get_mse_fleet_results", {"index": index})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_monte_carlo_parameters() -> dict[str, Any]:
    """Get Monte Carlo model parameters."""
    return await _send("get_monte_carlo_parameters")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def list_monte_carlo_groups() -> dict[str, Any]:
    """List Monte Carlo group parameters."""
    return await _send("list_monte_carlo_groups")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_monte_carlo_group(index: int) -> dict[str, Any]:
    """Get one Monte Carlo group parameter record."""
    return await _send("get_monte_carlo_group", {"index": index})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_monte_carlo_run_state() -> dict[str, Any]:
    """Get Monte Carlo run state."""
    return await _send("get_monte_carlo_run_state")


@mcp.tool(annotations=ToolAnnotations(destructiveHint=True))
async def run_monte_carlo() -> dict[str, Any]:
    """Run Monte Carlo analysis."""
    return await _send("run_monte_carlo", timeout=TIMEOUT_LONG)


@mcp.tool(annotations=ToolAnnotations(destructiveHint=True))
async def stop_monte_carlo() -> dict[str, Any]:
    """Stop the active Monte Carlo run."""
    return await _send("stop_monte_carlo")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_installed_plugins() -> dict[str, Any]:
    """List EwE plugins installed in the running instance."""
    return await _send("get_installed_plugins")


@mcp.tool(annotations=ToolAnnotations(destructiveHint=True))
async def create_ecosim_scenario(
    name: str,
    description: str = "",
    author: str = "",
    contact: str = "",
) -> dict[str, Any]:
    """Create and load a new Ecosim scenario.

    Sends a ping first to flush any pending dialog events before the
    potentially slow scenario-creation call.  The creation timeout is
    180s (TIMEOUT_CREATE_SCENARIO) because the operation runs on the
    EwE UI thread and may involve database I/O or dialog prompts.

    If the command times out, the scenario may still have been created
    on the EwE side.  Call ``list_ecosim_scenarios`` to check.
    """
    # Flush pending dialog events before the potentially slow create call
    try:
        await _send("ping")
        # Read and discard any pending notifications so a blocking dialog
        # (if present) is delivered to the plugin before the create call.
        _get_connection().notifications
    except (ConnectionError, OSError, TimeoutError, RuntimeError):
        logger.debug("create_ecosim_scenario: pre-ping not required")

    return await _send(
        "create_ecosim_scenario",
        {
            "name": name,
            "description": description,
            "author": author,
            "contact": contact,
        },
        timeout=TIMEOUT_CREATE_SCENARIO,
    )


@mcp.tool(annotations=ToolAnnotations(destructiveHint=True))
async def copy_ecosim_scenario(
    name: str,
    description: str = "",
    source_index: int | None = None,
    index: int | None = None,
) -> dict[str, Any]:
    """Copy an Ecosim scenario under a new name and load the copy.

    If source_index or index is provided, that scenario is loaded before copying.
    If omitted, the currently active scenario is copied.
    """
    effective_index = source_index or index
    params: dict[str, Any] = {"name": name, "description": description}
    if effective_index is not None:
        params["source_index"] = effective_index
    return await _send(
        "copy_ecosim_scenario",
        params,
        timeout=TIMEOUT_LONG,
    )


@mcp.tool(annotations=ToolAnnotations(destructiveHint=True))
async def delete_ecosim_scenario(index: int) -> dict[str, Any]:
    """Delete an unloaded Ecosim scenario by one-based index."""
    return await _send("delete_ecosim_scenario", {"index": index}, timeout=TIMEOUT_LONG)


@mcp.tool(annotations=ToolAnnotations(destructiveHint=True))
async def save_ecosim_scenario() -> dict[str, Any]:
    """Save the active Ecosim scenario."""
    return await _send("save_ecosim_scenario", timeout=TIMEOUT_LONG)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_ecosim_run_state() -> dict[str, Any]:
    """Get Ecosim scenario/run state, active scenario, run flags, and counters."""
    return await _send("get_ecosim_run_state")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def wait_for_ecosim_completion(
    timeout_seconds: int = 120,
    poll_interval_seconds: float = 1.0,
) -> dict[str, Any]:
    """Wait for Ecosim to finish running, polling until completion or timeout.

    Returns the final Ecosim run state. Polls at the specified interval.
    Raises a timeout error if Ecosim doesn't complete within the timeout.
    """
    deadline = time.monotonic() + timeout_seconds
    last_state: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last_state = await _send("get_ecosim_run_state")
        if not last_state.get("is_ecosim_running") and last_state.get("has_ecosim_ran"):
            return last_state
        await asyncio.sleep(poll_interval_seconds)
    raise RuntimeError(
        f"Ecosim did not complete within {timeout_seconds}s. Last state: {last_state}"
    )


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def list_ecosim_group_inputs() -> dict[str, Any]:
    """List Ecosim group input parameters for the active scenario.

    Key parameters include:
    - den_dep_catchability: Controls density-dependent catchability
      (minimum 1.0). Higher values = more catchable at high density.
    - vul_mult: Vulnerability multiplier (minimum 1.0). Higher values =
      more vulnerable to predation.
    - max_rel_feeding_time: Maximum relative feeding time (0 to 100,
      exclusive lower bound). Higher values = more feeding time available.
    - feeding_time_adjust_rate: Rate at which feeding time adjusts
      (0 to < 1.0). 0 = no adjustment.
    - max_rel_pb: Maximum relative production/biomass ratio
      (greater than 0).
    """
    return await _send("list_ecosim_group_inputs")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_ecosim_group_input(index: int) -> dict[str, Any]:
    """Get one Ecosim group input parameter record."""
    return await _send("get_ecosim_group_input", {"index": index})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_predators_of(
    index: int | None = None, name: str | None = None
) -> dict[str, Any]:
    """List all predators that eat a given prey group.

    Returns each predator's group index, name, diet fraction (from
    Ecopath diet composition), and vulnerability multiplier (vul_mult,
    from Ecosim if an Ecosim scenario is loaded).

    Use either `index` (numeric group index) or `name` (exact group name,
    case-insensitive) to identify the prey group.  Prefer `name` to avoid
    index confusion.
    """
    params: dict[str, Any] = {}
    if name is not None:
        params["name"] = name
    elif index is not None:
        params["index"] = index
    else:
        raise ValueError("Either 'name' or 'index' is required")
    return await _send("get_predators_of", params)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False))
async def set_vul_mult_by_name(
    prey: str, predator: str, value: float
) -> dict[str, Any]:
    """Set the vulnerability multiplier for a prey-predator pair by name.

    Resolves group names to indices internally, then calls set_indexed_value
    on ecosim_group_input.vul_mult.  Safer than setting indices manually.

    Args:
        prey: Exact prey group name (case-insensitive), e.g. "Herring A"
        predator: Exact predator group name, e.g. "Steller sea lions"
        value: New vul_mult value (minimum 1.0, higher = more vulnerable)
    """
    # Resolve prey and predator names to indices
    prey_result = await _send("search_entities", {
        "domain": "ecopath_groups",
        "name_contains": prey,
    })
    pred_result = await _send("search_entities", {
        "domain": "ecopath_groups",
        "name_contains": predator,
    })

    def _find_exact(results, target_name):
        for r in results:
            if r.get("name", "").strip().lower() == target_name.strip().lower():
                return r.get("index")
        return None

    prey_idx = _find_exact(prey_result.get("results", []), prey)
    pred_idx = _find_exact(pred_result.get("results", []), predator)

    if prey_idx is None:
        raise ValueError(f"Prey group '{prey}' not found")
    if pred_idx is None:
        raise ValueError(f"Predator group '{predator}' not found")

    result = await _send("set_indexed_value", {
        "domain": "ecosim_group_input",
        "index": prey_idx,
        "variable": "vul_mult",
        "indices": [pred_idx],
        "value": value,
    })
    result["_resolved"] = {
        "prey": {"name": prey, "index": prey_idx},
        "predator": {"name": predator, "index": pred_idx},
    }
    return result


@mcp.tool(annotations=ToolAnnotations(destructiveHint=True))
async def set_diet_matrix(diets: dict[str, dict[str, float]]) -> dict[str, Any]:
    """Set diet compositions for multiple predator groups in one call.

    Accepts a structured dict mapping predator group names to their prey
    fractions. Each predator's diet fractions should sum to approximately 1.0.

    Example:
        {
            "Snook": {"Mullet": 0.4, "Shrimp": 0.3, "Crabs": 0.3},
            "Red Drum": {"Mullet": 0.5, "Crabs": 0.5},
        }

    Resolves all names to indices from a single list_ecopath_groups call,
    builds flat diet_comp arrays, and sends all set_values via a single
    batch command with navigate=False — just 2 round-trips total regardless
    of how many predators or prey are specified.

    For setting a single diet entry, use set_indexed_value with
    variable="diet_comp" instead.
    """
    if not diets:
        raise ValueError("At least one predator diet definition is required")

    # Step 1: Get group list (builds name → index mapping in one call)
    try:
        groups_result = await _send("list_ecopath_groups")
        groups = groups_result.get("groups", [])
        n_groups = len(groups)
    except Exception as exc:
        return {"status": "failed", "error": f"Cannot determine group count: {exc}"}

    if n_groups == 0:
        return {"status": "failed", "error": "No groups found in the current model"}

    # Build local name → index lookup from the group list (casefold for case-insensitive matching)
    name_to_index: dict[str, int] = {}
    for g in groups:
        gname = g.get("name", "")
        gidx = g.get("index")
        if gname and gidx is not None:
            name_to_index[gname.casefold()] = int(gidx)

    # Step 2: Resolve names locally and build batch set_values commands
    resolution_errors: list[dict[str, Any]] = []
    diet_errors: list[dict[str, Any]] = []
    batch_cmds: list[dict[str, Any]] = []

    for pred_name, prey_fractions in diets.items():
        pred_idx = name_to_index.get(pred_name.casefold())
        if pred_idx is None:
            resolution_errors.append({"predator": pred_name, "error": "Name not found in model"})
            continue

        diet_array = [0.0] * n_groups
        has_valid = False
        for prey_name, fraction in prey_fractions.items():
            prey_idx = name_to_index.get(prey_name.casefold())
            if prey_idx is not None:
                if 1 <= prey_idx <= n_groups:
                    diet_array[prey_idx - 1] = fraction
                    has_valid = True
                else:
                    diet_errors.append({
                        "predator": pred_name,
                        "prey": prey_name,
                        "error": f"Prey index {prey_idx} out of range (1..{n_groups})",
                    })
            else:
                diet_errors.append({
                    "predator": pred_name,
                    "prey": prey_name,
                    "error": "Name not found in model",
                })

        if not has_valid:
            diet_errors.append({
                "predator": pred_name,
                "prey": list(prey_fractions.keys()),
                "error": "No prey names could be resolved for this predator",
            })
            continue

        batch_cmds.append({
            "type": "set_values",
            "params": {
                "domain": "ecopath_group",
                "index": pred_idx,
                "values": {"diet_comp": diet_array},
                "navigate": False,
            },
        })

    if not batch_cmds:
        return {
            "status": "failed",
            "error": "No valid diet entries could be constructed",
            "resolution_errors": resolution_errors,
            "diet_errors": diet_errors,
        }

    # Step 3: Send ONE batch command with ALL set_values calls
    send_errors: list[dict[str, Any]] = []
    batch_failed_indices: set[int] = set()
    n_applied = 0

    try:
        batch_result = await _send("batch", {"commands": batch_cmds}, timeout=TIMEOUT_LONG)
        results_list = batch_result.get("results", []) if isinstance(batch_result, dict) else []
        for i, sub in enumerate(results_list):
            if isinstance(sub, dict) and sub.get("status") == "error":
                pred_idx = (batch_cmds[i]["params"]["index"]
                            if i < len(batch_cmds) else None)
                send_errors.append({
                    "predator_index": pred_idx,
                    "error": sub.get("message", str(sub)),
                })
                if pred_idx is not None:
                    batch_failed_indices.add(pred_idx)
            else:
                n_applied += 1
    except Exception as exc:
        send_errors.append({"predator_index": None, "error": str(exc)})

    # Step 4: Verify diet_comp was actually set (silent batch failures can leave
    # the last predator with all-zero diet).  Read back diet_comp for all predators
    # in the batch that had non-zero expected entries, and retry any that show
    # all-zero when they shouldn't.
    round_trips = 2  # list_ecopath_groups + batch set_values
    retried_predators: list[dict[str, Any]] = []
    verified_ok: list[dict[str, Any]] = []

    # Gather predator indices and their expected diet arrays for verification
    verify_targets: list[tuple[int, list[float]]] = []
    for cmd in batch_cmds:
        pidx = cmd["params"]["index"]
        expected = cmd["params"]["values"]["diet_comp"]
        # Only verify predators that had non-zero expected entries
        if any(abs(v) > 1e-9 for v in expected):
            verify_targets.append((pidx, expected))

    if verify_targets:
        verify_cmds: list[dict[str, Any]] = [
            {
                "type": "get_values",
                "params": {
                    "domain": "ecopath_group",
                    "index": pidx,
                    "variables": ["diet_comp"],
                    "navigate": False,
                },
            }
            for pidx, _ in verify_targets
        ]
        try:
            verify_result = await _send("batch", {"commands": verify_cmds}, timeout=TIMEOUT_LONG)
            round_trips += 1  # verification batch
            verify_results = verify_result.get("results", []) if isinstance(verify_result, dict) else []
            for i, sub in enumerate(verify_results):
                if i >= len(verify_targets):
                    break
                pidx, expected_diet = verify_targets[i]
                needs_retry = pidx in batch_failed_indices  # batch already failed

                if not needs_retry and isinstance(sub, dict) and sub.get("status") == "success":
                    actual_diet = sub.get("values", {}).get("diet_comp")
                    if actual_diet is not None:
                        # Check if actual diet is all-zero (or near-zero)
                        actual_non_zero = any(abs(v) > 1e-9 for v in actual_diet)
                        if not actual_non_zero:
                            needs_retry = True
                            logger.warning(
                                "set_diet_matrix: predator index %d has all-zero diet after "
                                "batch (expected non-zero), retrying individually",
                                pidx,
                            )

                if needs_retry:
                    try:
                        retry_result = await _send(
                            "set_values",
                            {
                                "domain": "ecopath_group",
                                "index": pidx,
                                "values": {"diet_comp": expected_diet},
                                "navigate": False,
                            },
                            timeout=TIMEOUT_LONG,
                        )
                        round_trips += 1  # retry set_values
                        retried_predators.append({
                            "predator_index": pidx,
                            "result": retry_result.get("result", str(retry_result)),
                        })
                        # Count as applied if it wasn't already counted
                        if pidx in batch_failed_indices:
                            n_applied += 1
                    except Exception as retry_exc:
                        round_trips += 1  # failed retry attempt
                        retried_predators.append({
                            "predator_index": pidx,
                            "error": str(retry_exc),
                        })
                else:
                    verified_ok.append({
                        "predator_index": pidx,
                        "verified": True,
                    })
        except Exception as exc:
            logger.warning("set_diet_matrix: verification batch failed: %s", exc)

    diet_verification: dict[str, Any] = {
        "verified_count": len(verified_ok),
        "retried_count": len(retried_predators),
    }
    if verified_ok:
        diet_verification["verified"] = verified_ok
    if retried_predators:
        diet_verification["retried"] = retried_predators

    status = "success"
    n_expected = len(diets) - len(resolution_errors)
    if diet_errors or resolution_errors:
        status = "partial_success" if n_applied else "failed"
    elif send_errors or retried_predators:
        # send_errors and retried_predators may have been resolved by retry;
        # consider it success if all expected predators were eventually applied
        if n_applied >= n_expected:
            status = "success"
        else:
            status = "partial_success" if n_applied else "failed"

    return {
        "status": status,
        "total_predators": len(diets),
        "n_applied": n_applied,
        "n_groups": n_groups,
        "batch_size": len(batch_cmds),
        "round_trips": round_trips,
        "resolution_errors": resolution_errors or None,
        "diet_errors": diet_errors or None,
        "send_errors": send_errors or None,
        "diet_verification": diet_verification,
    }


# ── Diet CSV import/export ────────────────────────────────────────


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def export_diet_csv(path: str | None = None) -> dict[str, Any]:
    """Export the current diet composition matrix as a CSV.

    Builds a CSV of predator × prey diet fractions from the loaded model
    and returns the CSV content. If a path is provided, also attempts to
    write the CSV to a file on the EwE host machine.

    The CSV format uses group names as column headers:

        Predator,Group1,Group2,...,GroupN
        PredatorName,0.10,0.00,0.25,...,0.00

    Args:
        path: Optional path on the EwE host to write the CSV file.
              The CSV content is always returned regardless.

    Returns:
        Dict with status, n_groups, csv (content), and path info.
    """
    import csv
    import io as _io

    # Step 1: Get all groups (name + index)
    groups_result = await _send("list_ecopath_groups")
    groups = groups_result.get("groups", [])
    if not groups:
        raise ValueError("No groups found in the current model")

    group_names: list[str] = []
    group_indices: list[int] = []
    for g in groups:
        gname = g.get("name", "")
        gidx = g.get("index")
        if gname and gidx is not None:
            group_names.append(gname)
            group_indices.append(int(gidx))

    n_groups = len(group_names)
    if n_groups == 0:
        raise ValueError("No valid groups found in the current model")

    # Step 2: Batch get diet_comp for ALL groups (navigate=False avoids UI flicker)
    batch_cmds: list[dict[str, Any]] = []
    for idx in group_indices:
        batch_cmds.append({
            "type": "get_values",
            "params": {
                "domain": "ecopath_group",
                "index": idx,
                "variables": ["diet_comp"],
                "navigate": False,
            },
        })

    batch_result = await _send("batch", {"commands": batch_cmds}, timeout=TIMEOUT_LONG)
    results_list = batch_result.get("results", [])

    # Step 3: Build diet_matrix: group_index → diet_comp array
    diet_matrix: dict[int, list[float]] = {}
    for i, sub in enumerate(results_list):
        if isinstance(sub, dict) and sub.get("status") != "error":
            sub_idx = sub.get("index")
            if sub_idx is None and i < len(group_indices):
                sub_idx = group_indices[i]
            if sub_idx is not None:
                values = sub.get("values", {}) if isinstance(sub, dict) else {}
                diet_comp = values.get("diet_comp", [])
                if isinstance(diet_comp, list):
                    diet_matrix[int(sub_idx)] = [
                        float(v) if v is not None else 0.0 for v in diet_comp
                    ]

    # Step 4: Build CSV
    output = _io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Predator"] + group_names)

    for idx in group_indices:
        row_name = group_names[idx - 1]  # groups are 1-based
        diet = diet_matrix.get(idx, [])
        row_values: list[str] = [row_name]
        for prey_idx in range(n_groups):
            val = 0.0
            if prey_idx < len(diet):
                val = diet[prey_idx] if diet[prey_idx] is not None else 0.0
            row_values.append(_csv_float(val))
        writer.writerow(row_values)

    csv_content = output.getvalue()

    # Step 5: Optionally write to VM host file (write_host_file is not
    # available in all plugin versions; CSV content is always returned)
    written_to_host = False
    if path:
        _reject_path_traversal(path)
        if not path.lower().endswith('.csv'):
            raise ValueError("Path must have a .csv extension")
        try:
            await _send("write_host_file", {"path": path, "content": csv_content})
            written_to_host = True
        except (RuntimeError, ConnectionError, OSError, Exception):
            logger.warning(
                "export_diet_csv: write_host_file not available on this plugin version. "
                "CSV content is returned in the 'csv' field."
            )

    return {
        "status": "success",
        "n_groups": n_groups,
        "n_predators": len(group_indices),
        "csv": csv_content,
        "written_to_host": written_to_host,
        "path": path,
    }


@mcp.tool(annotations=ToolAnnotations(destructiveHint=True))
async def import_diet_csv(
    data: str | None = None,
    path: str | None = None,
) -> dict[str, Any]:
    """Import diet composition from CSV.

    Accepts either a CSV string or a path to a CSV file on the EwE host.
    The CSV must have a header row starting with 'Predator' followed by
    prey group names, and one data row per predator with fraction values.

    Format:

        Predator,Group1,Group2,...,GroupN
        PredatorName,0.10,0.00,0.25,...,0.00

    Args:
        data: CSV string content to import.
        path: Path to a CSV file on the EwE host machine to read and import.

    Returns:
        Dict with import status, count of imported predators, and
        per-predator results from set_diet_matrix.
    """
    import csv
    import io as _io

    # Read from path if data not provided directly
    if not data and path:
        _reject_path_traversal(path)
        if not path.lower().endswith('.csv'):
            raise ValueError("Path must have a .csv extension")
        read_result = await _send("read_host_file", {"path": path})
        data = read_result.get("content", "")
        if not data:
            raise ValueError(f"File at {path} is empty or unreadable")

    if not data or not data.strip():
        raise ValueError(
            "Either 'data' or 'path' must be provided with non-empty content"
        )

    # Parse CSV
    reader = csv.reader(_io.StringIO(data))
    rows = list(reader)
    if len(rows) < 2:
        raise ValueError("CSV must have at least a header row and one data row")

    header = rows[0]
    if not header or header[0].strip().lower() != "predator":
        raise ValueError(
            "CSV header must start with 'Predator' as the first column. "
            f"Got: {header[0] if header else '(empty)'}"
        )

    # Prey group names from CSV header (columns after 'Predator')
    csv_prey_names = [h.strip() for h in header[1:]]

    # Verify model connectivity and group list exists
    try:
        groups_result = await _send("list_ecopath_groups")
        model_groups = groups_result.get("groups", [])
    except Exception as exc:
        return {"status": "failed", "error": f"Cannot list model groups: {exc}"}

    if not model_groups:
        return {"status": "failed", "error": "No groups found in the current model"}

    # Build diets dict for set_diet_matrix
    diets: dict[str, dict[str, float]] = {}
    skipped_predators: list[dict[str, Any]] = []
    row_count = 0

    for row in rows[1:]:
        if not any(cell.strip() for cell in row):
            continue
        row_count += 1
        pred_name = row[0].strip()
        if not pred_name:
            skipped_predators.append({
                "row": row_count,
                "reason": "Empty predator name",
            })
            continue

        fractions: dict[str, float] = {}
        has_nonzero = False
        for i, cell in enumerate(row[1:]):
            if i >= len(csv_prey_names):
                break
            prey_name = csv_prey_names[i]
            try:
                val = float(cell.strip())
            except (ValueError, TypeError):
                skipped_predators.append({
                    "predator": pred_name,
                    "prey": prey_name,
                    "row": row_count,
                    "reason": f"Invalid numeric value: {cell!r}",
                })
                continue
            if val != 0.0:
                fractions[prey_name] = val
                has_nonzero = True

        if not has_nonzero:
            skipped_predators.append({
                "predator": pred_name,
                "row": row_count,
                "reason": "All diet fractions are zero; skipping",
            })
            continue

        diets[pred_name] = fractions

    if not diets:
        return {
            "status": "failed",
            "error": "No valid predator diet rows found in CSV",
            "total_csv_rows": row_count,
            "skipped_predators": skipped_predators or None,
        }

    # Delegate to set_diet_matrix (name-resolution + batch mutation)
    result = await set_diet_matrix(diets=diets)

    # Add import-specific metadata
    result["import_type"] = "csv"
    result["total_csv_rows"] = row_count
    result["parsed_predators"] = len(diets)
    result["skipped_predators"] = skipped_predators or None
    result["csv_prey_count"] = len(csv_prey_names)

    return result


# ── Ecopath Groups CSV import/export ──────────────────────────────


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def export_ecopath_groups_csv(path: str | None = None) -> dict[str, Any]:
    """Export Ecopath group parameters to CSV.

    Returns a CSV string with columns: Group,Biomass,PB,QB,EE,GroupType.
    Sentinel (-9999) and None values are replaced with empty strings.
    If a path is provided on the EwE host, attempts to write the CSV there.
    The CSV is always returned in the response as well.

    Args:
        path: Optional path on the EwE host to write the CSV file.

    Returns:
        Dict with status, group_count, csv (content), and path info.
    """
    import csv
    import io as _io

    # Step 1: Get all groups
    groups_result = await _send("list_ecopath_groups")
    groups = groups_result.get("groups", [])
    if not groups:
        raise ValueError("No groups found in the current model")

    # Step 2: Build CSV
    output = _io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Group", "Biomass", "PB", "QB", "EE", "GroupType"])

    for group in groups:
        name = group.get("name", "")
        gtype = group.get("type", "")
        inp = group.get("input", {})

        def _val(field: str) -> str:
            v = inp.get(field)
            if v is None or v == -9999:
                return ""
            return str(v)

        # NOTE: Export reads display-field names (biomass_area, pb, qb, ee) from
        # list_ecopath_groups, while import_ecopath_groups_csv writes input-field
        # names (biomass_area_input, pbinput, qbinput, eeinput) via set_values_by_name_batch.
        # These are different database columns but their values are equivalent for
        # a balanced model — this is NOT a data corruption issue in practice.
        writer.writerow([
            name,
            _val("biomass_area"),
            _val("pb"),
            _val("qb"),
            _val("ee"),
            gtype,
        ])

    csv_content = output.getvalue()

    # Step 3: Optionally write to VM host file
    written_to_host = False
    if path:
        _reject_path_traversal(path)
        if not path.lower().endswith('.csv'):
            raise ValueError("Path must have a .csv extension")
        try:
            await _send("write_host_file", {"path": path, "content": csv_content})
            written_to_host = True
        except (RuntimeError, ConnectionError, OSError, Exception):
            logger.warning(
                "export_ecopath_groups_csv: write_host_file not available on this plugin version. "
                "CSV content is returned in the 'csv' field."
            )

    return {
        "status": "success",
        "group_count": len(groups),
        "csv": csv_content,
        "written_to_host": written_to_host,
        "path": path,
    }


@mcp.tool(annotations=ToolAnnotations(destructiveHint=True))
async def import_ecopath_groups_csv(
    data: str | None = None,
    path: str | None = None,
) -> dict[str, Any]:
    """Import Ecopath group parameters from CSV.

    CSV format: Group,Biomass,PB,QB,EE
    The GroupType column is informational and ignored on import.
    Accepts either a CSV string (data) or a path on the EwE host (path).
    Uses set_values_by_name_batch for efficient bulk update.

    Column mapping:
        Group    → entity name (used for resolution)
        Biomass  → ba
        PB       → pb
        QB       → qb
        EE       → ee
        GroupType → informational, skipped

    Args:
        data: CSV string content to import.
        path: Path to a CSV file on the EwE host machine to read and import.

    Returns:
        Dict with import status, total/applied/skipped counts, and results.
    """
    import csv
    import io as _io

    # Read from path if data not provided directly
    if not data and path:
        _reject_path_traversal(path)
        if not path.lower().endswith('.csv'):
            raise ValueError("Path must have a .csv extension")
        read_result = await _send("read_host_file", {"path": path})
        data = read_result.get("content", "")
        if not data:
            raise ValueError(f"File at {path} is empty or unreadable")

    if not data or not data.strip():
        raise ValueError(
            "Must provide CSV data via 'data' parameter or 'path' to a file on the EwE host"
        )

    # Parse CSV
    reader = csv.reader(_io.StringIO(data))
    rows = list(reader)
    if len(rows) < 2:
        raise ValueError("CSV must have at least a header row and one data row")

    header = rows[0]
    if not header or header[0].strip().lower() != "group":
        raise ValueError(
            "CSV header first column must be 'Group'. "
            f"Got: {header[0] if header else '(empty)'}"
        )

    # Map CSV columns to ecopath_group writable field names
    # Known columns: Group (ignored, used as name), Biomass→biomass_area_input, PB→pbinput, QB→qbinput, EE→eeinput, GroupType→skipped
    col_map: dict[str, str | None] = {
        "group": None,        # used as entity name, not a field
        "biomass": "biomass_area_input",
        "pb": "pbinput",
        "qb": "qbinput",
        "ee": "eeinput",
        "grouptype": None,    # informational, skipped
    }

    col_headers = [h.strip() for h in header]

    # Build list of (csv_col_index, ewe_field) for columns we recognise
    field_cols: list[tuple[int, str]] = []
    for i, hdr in enumerate(col_headers[1:], start=1):
        hdr_lower = hdr.lower()
        mapped = col_map.get(hdr_lower)
        if mapped:
            field_cols.append((i, mapped))
        # None means recognised but skipped; absent means unrecognised (also skipped)

    # Build operations for set_values_by_name_batch
    operations: list[dict[str, Any]] = []
    row_count = 0

    for row in rows[1:]:
        row_count += 1
        group_name = row[0].strip() if row else ""
        if not group_name:
            continue

        # Check if all value columns are empty — skip such rows
        values: dict[str, float] = {}
        all_empty = True
        for col_idx, ewe_field in field_cols:
            cell = row[col_idx].strip() if col_idx < len(row) else ""
            if cell == "":
                continue
            all_empty = False
            try:
                values[ewe_field] = float(cell)
            except (ValueError, TypeError):
                # Skip invalid numeric values gracefully
                continue

        if all_empty:
            continue

        operations.append({
            "domain": "ecopath_group",
            "name": group_name,
            "values": values,
        })

    if not operations:
        return {
            "status": "failed",
            "error": "No valid group data rows found in CSV",
            "total_csv_rows": row_count,
        }

    # Delegate to set_values_by_name_batch
    result = await set_values_by_name_batch(
        operations,
        options={"include_suggestions": True},
    )

    # Add import-specific metadata
    result["import_type"] = "csv"
    result["total_csv_rows"] = row_count

    return result


# ── Ecosim Vulnerability Multiplier CSV ───────────────────────────


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def export_ecosim_vul_csv(path: str | None = None) -> dict[str, Any]:
    """Export the current Ecosim vulnerability multiplier matrix as a CSV.

    Builds a CSV of prey × predator vulnerability multipliers from the active
    Ecosim scenario and returns the CSV content. If a path is provided on the
    EwE host, also attempts to write the file there.

    The CSV uses group names as column headers:

        Prey,Predator1,Predator2,...,PredatorN
        PreyName,1.0,2.5,1.0,...,1.0

    Args:
        path: Optional path on the EwE host to write the CSV file.
              The CSV content is always returned regardless.

    Returns:
        Dict with status, n_groups, csv (content), and path info.
    """
    import csv
    import io as _io

    # Step 1: Get all groups (name + index)
    groups_result = await _send("list_ecopath_groups")
    groups = groups_result.get("groups", [])
    if not groups:
        raise ValueError("No groups found in the current model")

    group_names: list[str] = []
    group_indices: list[int] = []
    for g in groups:
        gname = g.get("name", "")
        gidx = g.get("index")
        if gname and gidx is not None:
            group_names.append(gname)
            group_indices.append(int(gidx))

    n_groups = len(group_names)
    if n_groups == 0:
        raise ValueError("No valid groups found in the current model")

    # Step 2: vul_mult is an indexed property (C# indexer), not a regular array.
    # Read it via get_indexed_value for each (prey, predator) pair.
    # Build ONE batch with all N×M sub-commands (instead of N separate batches).
    all_batch_cmds: list[dict[str, Any]] = []
    for prey_idx in group_indices:
        for pred_idx in group_indices:
            all_batch_cmds.append({
                "type": "get_indexed_value",
                "params": {
                    "domain": "ecosim_group_input",
                    "variable": "vul_mult",
                    "index": prey_idx,
                    "indices": [pred_idx],
                    "navigate": False,
                },
            })

    batch_result = await _send("batch", {"commands": all_batch_cmds}, timeout=TIMEOUT_LONG)
    all_results = batch_result.get("results", []) if isinstance(batch_result, dict) else []

    # Reshape flat results list back into per-prey vul_matrix
    vul_matrix: dict[int, list[float]] = {}
    for pi, prey_idx in enumerate(group_indices):
        start = pi * n_groups
        end = start + n_groups
        pred_results = all_results[start:end] if end <= len(all_results) else []
        vul_array: list[float] = []
        for sub in pred_results:
            if isinstance(sub, dict) and sub.get("status") != "error":
                val = sub.get("value")
                if val is not None:
                    vul_array.append(float(val))
                else:
                    vul_array.append(1.0)
            else:
                vul_array.append(1.0)
        vul_matrix[prey_idx] = vul_array

    # Step 3: Build CSV
    output = _io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Prey"] + group_names)

    for idx in group_indices:
        row_name = group_names[idx - 1]  # groups are 1-based
        vul = vul_matrix.get(idx, [])
        row_values: list[str] = [row_name]
        for pred_idx in range(n_groups):
            val = 1.0
            if pred_idx < len(vul):
                val = vul[pred_idx] if vul[pred_idx] is not None else 1.0
            row_values.append(_csv_float(val))
        writer.writerow(row_values)

    csv_content = output.getvalue()

    # Step 5: Optionally write to VM host file
    written_to_host = False
    if path:
        _reject_path_traversal(path)
        if not path.lower().endswith('.csv'):
            raise ValueError("Path must have a .csv extension")
        try:
            await _send("write_host_file", {"path": path, "content": csv_content})
            written_to_host = True
        except (RuntimeError, ConnectionError, OSError, Exception):
            logger.warning(
                "export_ecosim_vul_csv: write_host_file not available on this plugin version. "
                "CSV content is returned in the 'csv' field."
            )

    return {
        "status": "success",
        "n_groups": n_groups,
        "n_prey": len(group_indices),
        "csv": csv_content,
        "written_to_host": written_to_host,
        "path": path,
    }


@mcp.tool(annotations=ToolAnnotations(destructiveHint=True))
async def import_ecosim_vul_csv(
    data: str | None = None,
    path: str | None = None,
) -> dict[str, Any]:
    """Import Ecosim vulnerability multipliers from CSV.

    Accepts either a CSV string or a path to a CSV file on the EwE host.
    The CSV must have a header row starting with 'Prey' followed by
    predator group names, and one data row per prey group with multiplier
    values. Values of 1.0 are skipped (they represent the default).

    Format:

        Prey,Predator1,Predator2,...,PredatorN
        PreyName,1.0,2.5,1.0,...,1.0

    Args:
        data: CSV string content to import.
        path: Path to a CSV file on the EwE host machine to read and import.

    Returns:
        Dict with import status, counts, and per-entry results.
    """
    import csv
    import io as _io

    # Read from path if data not provided directly
    if not data and path:
        _reject_path_traversal(path)
        if not path.lower().endswith('.csv'):
            raise ValueError("Path must have a .csv extension")
        read_result = await _send("read_host_file", {"path": path})
        data = read_result.get("content", "")
        if not data:
            raise ValueError(f"File at {path} is empty or unreadable")

    if not data or not data.strip():
        raise ValueError(
            "Either 'data' or 'path' must be provided with non-empty content"
        )

    # Parse CSV
    reader = csv.reader(_io.StringIO(data))
    rows = list(reader)
    if len(rows) < 2:
        raise ValueError("CSV must have at least a header row and one data row")

    header = rows[0]
    if not header or header[0].strip().lower() != "prey":
        raise ValueError(
            "CSV header must start with 'Prey' as the first column. "
            f"Got: {header[0] if header else '(empty)'}"
        )

    # Predator group names from CSV header (columns after 'Prey')
    csv_pred_names = [h.strip() for h in header[1:]]

    # Build name → index mapping from model
    groups_result = await _send("list_ecopath_groups")
    model_groups = groups_result.get("groups", [])
    if not model_groups:
        return {"status": "failed", "error": "No groups found in the current model"}

    name_to_index: dict[str, int] = {}
    for g in model_groups:
        gname = g.get("name", "")
        gidx = g.get("index")
        if gname and gidx is not None:
            name_to_index[gname] = int(gidx)

    # Resolve CSV predator column names to indices
    pred_indices: list[int] = []
    pred_resolution_errors: list[dict[str, Any]] = []
    for pname in csv_pred_names:
        pidx = name_to_index.get(pname)
        if pidx is not None:
            pred_indices.append(pidx)
        else:
            pred_resolution_errors.append({"name": pname, "error": "Not found in model"})

    n_predators = len(pred_indices)
    if pred_resolution_errors:
        return {
            "status": "failed",
            "error": "One or more predator column names could not be resolved",
            "resolution_errors": pred_resolution_errors,
        }

    # Build batch_commands for non-1.0 entries
    batch_cmds: list[dict[str, Any]] = []
    set_count = 0
    row_count = 0
    skipped_rows: list[dict[str, Any]] = []

    for row in rows[1:]:
        if not any(cell.strip() for cell in row):
            continue
        row_count += 1
        prey_name = row[0].strip()
        if not prey_name:
            skipped_rows.append({"row": row_count, "reason": "Empty prey name"})
            continue

        prey_idx = name_to_index.get(prey_name)
        if prey_idx is None:
            skipped_rows.append({
                "row": row_count,
                "prey": prey_name,
                "reason": "Not found in model",
            })
            continue

        for i, cell in enumerate(row[1:]):
            if i >= n_predators:
                break
            try:
                val = float(cell.strip())
            except (ValueError, TypeError):
                skipped_rows.append({
                    "row": row_count,
                    "prey": prey_name,
                    "predator": csv_pred_names[i] if i < len(csv_pred_names) else str(i),
                    "reason": f"Invalid numeric value: {cell!r}",
                })
                continue
            if val == 1.0:
                continue  # skip defaults

            batch_cmds.append({
                "type": "set_indexed_value",
                "params": {
                    "domain": "ecosim_group_input",
                    "index": prey_idx,
                    "variable": "vul_mult",
                    "indices": [pred_indices[i]],
                    "value": val,
                    "navigate": False,
                },
            })
            set_count += 1

    if not batch_cmds:
        return {
            "status": "success",
            "message": "No non-default vul_mult values to set",
            "total_csv_rows": row_count,
            "skipped_rows": skipped_rows or None,
        }

    # Execute batch
    send_errors: list[dict[str, Any]] = []
    n_applied = 0
    try:
        batch_result = await _send("batch", {"commands": batch_cmds}, timeout=TIMEOUT_LONG)
        results_list = batch_result.get("results", []) if isinstance(batch_result, dict) else []
        for i, sub in enumerate(results_list):
            if isinstance(sub, dict) and sub.get("status") == "error":
                send_errors.append({"index": i, "error": sub.get("message", str(sub))})
            else:
                n_applied += 1
    except Exception as exc:
        send_errors.append({"error": str(exc)})

    status = "success"
    if send_errors or skipped_rows:
        status = "partial_success" if n_applied else "failed"

    return {
        "status": status,
        "total_csv_rows": row_count,
        "n_sets_requested": set_count,
        "n_applied": n_applied,
        "n_errors": len(send_errors),
        "errors": send_errors or None,
        "skipped_rows": skipped_rows or None,
        "csv_predator_count": len(csv_pred_names),
    }


# ── Fleet Landings/Discards CSV ───────────────────────────────────


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def export_fleet_landings_csv(path: str | None = None) -> dict[str, Any]:
    """Export fleet landings and discards to CSV.

    Builds a long-format CSV with one row per fleet-group pair that has
    non-zero landings or discards. Returns the CSV content. If a path is
    provided on the EwE host, also attempts to write the file there.

    CSV format:

        Fleet,TargetGroup,Landings,Discards
        Gillnet,Snook,0.500000,0.000000

    Args:
        path: Optional path on the EwE host to write the CSV file.
              The CSV content is always returned regardless.

    Returns:
        Dict with status, fleet_count, group_count, csv (content), and path info.
    """
    import csv
    import io as _io

    # Step 1: Get all fleets (name + index)
    fleets_result = await _send("list_ecopath_fleets")
    fleets = fleets_result.get("fleets", [])
    if not fleets:
        raise ValueError("No fleets found in the current model")

    fleet_names: list[str] = []
    fleet_indices: list[int] = []
    for f in fleets:
        fname = f.get("name", "")
        fci = f.get("index")
        if fname and fci is not None:
            fleet_names.append(fname)
            fleet_indices.append(int(fci))

    # Step 2: Get all groups (name + index)
    groups_result = await _send("list_ecopath_groups")
    groups = groups_result.get("groups", [])
    group_names: list[str] = []
    group_indices: list[int] = []
    for g in groups:
        gname = g.get("name", "")
        gidx = g.get("index")
        if gname and gidx is not None:
            group_names.append(gname)
            group_indices.append(int(gidx))

    n_groups = len(group_names)
    if n_groups == 0:
        raise ValueError("No valid groups found in the current model")

    # Step 3: Batch get fleet details for ALL fleets
    batch_cmds: list[dict[str, Any]] = []
    for fi in fleet_indices:
        batch_cmds.append({
            "type": "get_ecopath_fleet",
            "params": {"index": fi},
        })

    batch_result = await _send("batch", {"commands": batch_cmds}, timeout=TIMEOUT_LONG)
    results_list = batch_result.get("results", [])

    # Step 4: Build CSV in long format (one row per fleet-group pair with non-zero values)
    output = _io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Fleet", "TargetGroup", "Landings", "Discards"])

    non_zero_rows = 0
    for i, sub in enumerate(results_list):
        if not isinstance(sub, dict) or sub.get("status") == "error":
            continue
        fleet_name = sub.get("name", fleet_names[i] if i < len(fleet_names) else "")

        landings = sub.get("landings", []) or []
        discards = sub.get("discards", []) or []

        for g_idx, g_name in zip(group_indices, group_names):
            l_val = 0.0
            if g_idx - 1 < len(landings) and landings[g_idx - 1] is not None:
                l_val = float(landings[g_idx - 1])
            d_val = 0.0
            if g_idx - 1 < len(discards) and discards[g_idx - 1] is not None:
                d_val = float(discards[g_idx - 1])

            if l_val != 0.0 or d_val != 0.0:
                writer.writerow([
                    fleet_name,
                    g_name,
                    _csv_float(l_val),
                    _csv_float(d_val),
                ])
                non_zero_rows += 1

    csv_content = output.getvalue()

    # Step 5: Optionally write to VM host file
    written_to_host = False
    if path:
        _reject_path_traversal(path)
        if not path.lower().endswith('.csv'):
            raise ValueError("Path must have a .csv extension")
        try:
            await _send("write_host_file", {"path": path, "content": csv_content})
            written_to_host = True
        except (RuntimeError, ConnectionError, OSError, Exception):
            logger.warning(
                "export_fleet_landings_csv: write_host_file not available on this plugin version. "
                "CSV content is returned in the 'csv' field."
            )

    return {
        "status": "success",
        "fleet_count": len(fleet_indices),
        "group_count": n_groups,
        "non_zero_rows": non_zero_rows,
        "csv": csv_content,
        "written_to_host": written_to_host,
        "path": path,
    }


@mcp.tool(annotations=ToolAnnotations(destructiveHint=True))
async def import_fleet_landings_csv(
    data: str | None = None,
    path: str | None = None,
) -> dict[str, Any]:
    """Import fleet landings and discards from CSV.

    Accepts either a CSV string or a path to a CSV file on the EwE host.
    CSV must have columns: Fleet,TargetGroup,Landings,Discards (long format,
    one row per fleet-group pair).

    Only non-zero values are set. Uses set_indexed_value with navigate=False
    via batch_commands.

    Args:
        data: CSV string content to import.
        path: Path to a CSV file on the EwE host machine to read and import.

    Returns:
        Dict with import status, counts, and results.
    """
    import csv
    import io as _io

    # Read from path if data not provided directly
    if not data and path:
        _reject_path_traversal(path)
        if not path.lower().endswith('.csv'):
            raise ValueError("Path must have a .csv extension")
        read_result = await _send("read_host_file", {"path": path})
        data = read_result.get("content", "")
        if not data:
            raise ValueError(f"File at {path} is empty or unreadable")

    if not data or not data.strip():
        raise ValueError(
            "Either 'data' or 'path' must be provided with non-empty content"
        )

    # Parse CSV
    reader = csv.reader(_io.StringIO(data))
    rows = list(reader)
    if len(rows) < 2:
        raise ValueError("CSV must have at least a header row and one data row")

    header = rows[0]
    expected_header = ["fleet", "targetgroup", "landings", "discards"]
    header_lower = [h.strip().lower() for h in header]
    if header_lower != expected_header:
        raise ValueError(
            f"CSV columns must be: {expected_header}, got: {header_lower}"
        )

    # Build name → index mappings
    fleets_result = await _send("list_ecopath_fleets")
    fleets = fleets_result.get("fleets", [])
    fleet_name_to_idx: dict[str, int] = {}
    for f in fleets:
        fname = f.get("name", "")
        fci = f.get("index")
        if fname and fci is not None:
            fleet_name_to_idx[fname] = int(fci)

    groups_result = await _send("list_ecopath_groups")
    groups = groups_result.get("groups", [])
    group_name_to_idx: dict[str, int] = {}
    for g in groups:
        gname = g.get("name", "")
        gidx = g.get("index")
        if gname and gidx is not None:
            group_name_to_idx[gname] = int(gidx)

    # Build batch commands for each non-zero value
    batch_cmds: list[dict[str, Any]] = []
    set_count = 0
    row_count = 0
    skipped: list[dict[str, Any]] = []

    for row in rows[1:]:
        if not any(cell.strip() for cell in row):
            continue
        row_count += 1
        fleet_name = row[0].strip()
        group_name = row[1].strip() if len(row) > 1 else ""

        if not fleet_name or not group_name:
            skipped.append({"row": row_count, "reason": "Empty fleet or group name"})
            continue

        fleet_idx = fleet_name_to_idx.get(fleet_name)
        if fleet_idx is None:
            skipped.append({"row": row_count, "reason": f"Fleet '{fleet_name}' not found in model"})
            continue

        group_idx = group_name_to_idx.get(group_name)
        if group_idx is None:
            skipped.append({"row": row_count, "reason": f"Group '{group_name}' not found in model"})
            continue

        landings_str = row[2].strip() if len(row) > 2 else "0"
        discards_str = row[3].strip() if len(row) > 3 else "0"

        try:
            landings_val = float(landings_str)
        except (ValueError, TypeError):
            skipped.append({"row": row_count, "reason": f"Invalid landings value: {landings_str!r}"})
            continue

        try:
            discards_val = float(discards_str)
        except (ValueError, TypeError):
            skipped.append({"row": row_count, "reason": f"Invalid discards value: {discards_str!r}"})
            continue

        if landings_val != 0.0:
            batch_cmds.append({
                "type": "set_indexed_value",
                "params": {
                    "domain": "ecopath_fleet",
                    "index": fleet_idx,
                    "variable": "landings",
                    "indices": [group_idx],
                    "value": landings_val,
                    "navigate": False,
                },
            })
            set_count += 1

        if discards_val != 0.0:
            batch_cmds.append({
                "type": "set_indexed_value",
                "params": {
                    "domain": "ecopath_fleet",
                    "index": fleet_idx,
                    "variable": "discards",
                    "indices": [group_idx],
                    "value": discards_val,
                    "navigate": False,
                },
            })
            set_count += 1

    if not batch_cmds:
        return {
            "status": "success",
            "message": "No non-zero values to import from CSV",
            "total_csv_rows": row_count,
            "skipped": skipped or None,
        }

    # Execute batch
    send_errors: list[dict[str, Any]] = []
    n_applied = 0
    try:
        batch_result = await _send("batch", {"commands": batch_cmds}, timeout=TIMEOUT_LONG)
        results_list = batch_result.get("results", []) if isinstance(batch_result, dict) else []
        for i, sub in enumerate(results_list):
            if isinstance(sub, dict) and sub.get("status") == "error":
                send_errors.append({"index": i, "error": sub.get("message", str(sub))})
            else:
                n_applied += 1
    except Exception as exc:
        send_errors.append({"error": str(exc)})

    status = "success"
    if send_errors or skipped:
        status = "partial_success" if n_applied else "failed"

    return {
        "status": status,
        "total_csv_rows": row_count,
        "n_sets_requested": set_count,
        "n_applied": n_applied,
        "n_errors": len(send_errors),
        "errors": send_errors or None,
        "skipped": skipped or None,
    }


# ── Ecosim Group Inputs CSV ───────────────────────────────────────


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def export_ecosim_inputs_csv(path: str | None = None) -> dict[str, Any]:
    """Export Ecosim group input parameters to CSV.

    Returns a CSV string with columns:
    Group,MaxRelPB,MaxRelFeedingTime,FeedingTimeAdjustRate,DenDepCatchability.
    Sentinel (-9999) and None values are replaced with empty strings.
    Vulnerability multipliers are handled separately by export_ecosim_vul_csv.

    If a path is provided on the EwE host, attempts to write the CSV there.
    The CSV is always returned in the response as well.

    Args:
        path: Optional path on the EwE host to write the CSV file.

    Returns:
        Dict with status, input_count, csv (content), and path info.
    """
    import csv
    import io as _io

    # Step 1: Get all Ecosim group inputs
    inputs_result = await _send("list_ecosim_group_inputs")
    inputs = inputs_result.get("groups", []) if isinstance(inputs_result, dict) else []
    if not inputs:
        raise ValueError(
            "No Ecosim group inputs found — is an Ecosim scenario loaded?"
        )

    # Step 2: Build CSV
    output = _io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Group",
        "MaxRelPB",
        "MaxRelFeedingTime",
        "FeedingTimeAdjustRate",
        "DenDepCatchability",
    ])

    for inp in inputs:
        name = inp.get("name", "")
        values = inp.get("values", {}) if isinstance(inp, dict) else {}

        def _val(field: str) -> str:
            v = values.get(field)
            if v is None or v == -9999:
                return ""
            return str(v)

        writer.writerow([
            name,
            _val("max_rel_pb"),
            _val("max_rel_feeding_time"),
            _val("feeding_time_adjust_rate"),
            _val("den_dep_catchability"),
        ])

    csv_content = output.getvalue()

    # Step 3: Optionally write to VM host file
    written_to_host = False
    if path:
        _reject_path_traversal(path)
        if not path.lower().endswith('.csv'):
            raise ValueError("Path must have a .csv extension")
        try:
            await _send("write_host_file", {"path": path, "content": csv_content})
            written_to_host = True
        except (RuntimeError, ConnectionError, OSError, Exception):
            logger.warning(
                "export_ecosim_inputs_csv: write_host_file not available on this plugin version. "
                "CSV content is returned in the 'csv' field."
            )

    return {
        "status": "success",
        "input_count": len(inputs),
        "csv": csv_content,
        "written_to_host": written_to_host,
        "path": path,
    }


@mcp.tool(annotations=ToolAnnotations(destructiveHint=True))
async def import_ecosim_inputs_csv(
    data: str | None = None,
    path: str | None = None,
) -> dict[str, Any]:
    """Import Ecosim group input parameters from CSV.

    CSV format: Group,MaxRelPB,MaxRelFeedingTime,FeedingTimeAdjustRate,DenDepCatchability
    Accepts either a CSV string (data) or a path on the EwE host (path).
    Uses set_values_by_name_batch for efficient bulk update.

    Column mapping:
        Group               → entity name (used for resolution)
        MaxRelPB            → max_rel_pb
        MaxRelFeedingTime   → max_rel_feeding_time
        FeedingTimeAdjustRate → feeding_time_adjust_rate
        DenDepCatchability  → den_dep_catchability

    Args:
        data: CSV string content to import.
        path: Path to a CSV file on the EwE host machine to read and import.

    Returns:
        Dict with import status, total/applied/skipped counts, and results.
    """
    import csv
    import io as _io

    # Read from path if data not provided directly
    if not data and path:
        _reject_path_traversal(path)
        if not path.lower().endswith('.csv'):
            raise ValueError("Path must have a .csv extension")
        read_result = await _send("read_host_file", {"path": path})
        data = read_result.get("content", "")
        if not data:
            raise ValueError(f"File at {path} is empty or unreadable")

    if not data or not data.strip():
        raise ValueError(
            "Must provide CSV data via 'data' parameter or 'path' to a file on the EwE host"
        )

    # Parse CSV
    reader = csv.reader(_io.StringIO(data))
    rows = list(reader)
    if len(rows) < 2:
        raise ValueError("CSV must have at least a header row and one data row")

    header = rows[0]
    if not header or header[0].strip().lower() != "group":
        raise ValueError(
            "CSV header first column must be 'Group'. "
            f"Got: {header[0] if header else '(empty)'}"
        )

    # Map CSV column names to ecosim_group_input field names
    col_map: dict[str, str | None] = {
        "group": None,                    # used as entity name, not a field
        "maxrelpb": "max_rel_pb",
        "maxrelfeedingtime": "max_rel_feeding_time",
        "feedingtimeadjustrate": "feeding_time_adjust_rate",
        "dendepcatchability": "den_dep_catchability",
    }

    col_headers = [h.strip() for h in header]

    # Build list of (csv_col_index, ewe_field) for columns we recognise
    field_cols: list[tuple[int, str]] = []
    for i, hdr in enumerate(col_headers[1:], start=1):
        hdr_lower = hdr.lower()
        mapped = col_map.get(hdr_lower)
        if mapped:
            field_cols.append((i, mapped))
        # None means recognised but skipped; absent means unrecognised (also skipped)

    # Build operations for set_values_by_name_batch
    operations: list[dict[str, Any]] = []
    row_count = 0

    for row in rows[1:]:
        row_count += 1
        group_name = row[0].strip() if row else ""
        if not group_name:
            continue

        # Check if all value columns are empty — skip such rows
        values: dict[str, float] = {}
        all_empty = True
        for col_idx, ewe_field in field_cols:
            cell = row[col_idx].strip() if col_idx < len(row) else ""
            if cell == "":
                continue
            all_empty = False
            try:
                values[ewe_field] = float(cell)
            except (ValueError, TypeError):
                # Skip invalid numeric values gracefully
                continue

        if all_empty:
            continue

        operations.append({
            "domain": "ecosim_group_input",
            "name": group_name,
            "values": values,
        })

    if not operations:
        return {
            "status": "failed",
            "error": "No valid Ecosim group input data rows found in CSV",
            "total_csv_rows": row_count,
        }

    # Delegate to set_values_by_name_batch
    result = await set_values_by_name_batch(
        operations,
        options={"include_suggestions": True},
    )

    # Add import-specific metadata
    result["import_type"] = "csv"
    result["total_csv_rows"] = row_count

    return result


# ── Ecotracer Group Inputs CSV ─────────────────────────────────────


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def export_ecotracer_inputs_csv(path: str | None = None) -> dict[str, Any]:
    """Export Ecotracer group input parameters to CSV.

    Requires an Ecotracer scenario to be loaded first.
    Returns a CSV string with Group and Ecotracer-specific parameter columns.
    If a path is provided on the EwE host, attempts to write the CSV there.
    The CSV is always returned in the response as well.

    Args:
        path: Optional path on the EwE host to write the CSV file.

    Returns:
        Dict with status, group_count, csv (content), and path info.
    """
    import csv
    import io as _io

    # Check if an Ecotracer scenario is loaded
    try:
        tracer_state = await _send("get_ecotracer_run_state")
        if not tracer_state.get("has_ecotracer_loaded", False):
            return {"status": "error", "message": "No Ecotracer scenario loaded"}
    except (RuntimeError, ConnectionError, OSError) as exc:
        return {"status": "error", "message": f"Cannot check Ecotracer state: {exc}"}

    # List Ecotracer group inputs
    list_result = await _send("list_ecotracer_group_inputs")
    items = list_result.get("items", []) if isinstance(list_result, dict) else []
    if not items:
        raise ValueError("No Ecotracer group inputs found")

    # Batch-get values for all groups
    batch_cmds: list[dict[str, Any]] = []
    indices: list[int] = []
    names: list[str] = []
    for item in items:
        idx = item.get("index")
        name = item.get("name", "")
        if idx is not None and name:
            indices.append(int(idx))
            names.append(name)
            batch_cmds.append({
                "type": "get_ecotracer_group_input",
                "params": {"index": int(idx)},
            })

    if not batch_cmds:
        raise ValueError("No valid Ecotracer group inputs found")

    batch_result = await _send("batch", {"commands": batch_cmds}, timeout=TIMEOUT_LONG)
    results_list = batch_result.get("results", []) if isinstance(batch_result, dict) else []

    # Collect all unique field names from the values dicts
    all_field_names: set[str] = set()
    group_values: dict[int, dict[str, Any]] = {}
    for i, sub in enumerate(results_list):
        if isinstance(sub, dict) and sub.get("status") != "error":
            sub_idx = sub.get("index")
            if sub_idx is not None:
                values = sub.get("values", {}) if isinstance(sub, dict) else {}
                if isinstance(values, dict):
                    group_values[int(sub_idx)] = values
                    all_field_names.update(k for k in values
                                           if k != "name" and values[k] is not None)

    sorted_fields = sorted(all_field_names)

    # Build CSV
    output = _io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Group"] + sorted_fields)

    for idx, name in zip(indices, names):
        vals = group_values.get(idx, {})
        row = [name]
        for field in sorted_fields:
            v = vals.get(field)
            if v is None or v == -9999:
                row.append("")
            else:
                row.append(str(v))
        writer.writerow(row)

    csv_content = output.getvalue()

    # Optionally write to VM host file
    written_to_host = False
    if path:
        _reject_path_traversal(path)
        if not path.lower().endswith('.csv'):
            raise ValueError("Path must have a .csv extension")
        try:
            await _send("write_host_file", {"path": path, "content": csv_content})
            written_to_host = True
        except (RuntimeError, ConnectionError, OSError, Exception):
            logger.warning(
                "export_ecotracer_inputs_csv: write_host_file not available. "
                "CSV content is returned in the 'csv' field."
            )

    return {
        "status": "success",
        "group_count": len(indices),
        "csv": csv_content,
        "written_to_host": written_to_host,
        "path": path,
    }


@mcp.tool(annotations=ToolAnnotations(destructiveHint=True))
async def import_ecotracer_inputs_csv(
    data: str | None = None,
    path: str | None = None,
) -> dict[str, Any]:
    """Import Ecotracer group input parameters from CSV.

    CSV must have a header row starting with 'Group' followed by
    Ecotracer parameter columns matching the field names returned by
    get_ecotracer_group_input. Uses set_values_by_name_batch for import.

    Args:
        data: CSV string content to import.
        path: Path to a CSV file on the EwE host machine to read and import.

    Returns:
        Dict with import status, total/applied/skipped counts, and results.
    """
    import csv
    import io as _io

    # Read from path if data not provided directly
    if not data and path:
        _reject_path_traversal(path)
        if not path.lower().endswith('.csv'):
            raise ValueError("Path must have a .csv extension")
        read_result = await _send("read_host_file", {"path": path})
        data = read_result.get("content", "")
        if not data:
            raise ValueError(f"File at {path} is empty or unreadable")

    if not data or not data.strip():
        raise ValueError(
            "Must provide CSV data via 'data' parameter or 'path' to a file on the EwE host"
        )

    # Parse CSV
    reader = csv.reader(_io.StringIO(data))
    rows = list(reader)
    if len(rows) < 2:
        raise ValueError("CSV must have at least a header row and one data row")

    header = rows[0]
    if not header or header[0].strip().lower() != "group":
        raise ValueError(
            "CSV header first column must be 'Group'. "
            f"Got: {header[0] if header else '(empty)'}"
        )

    # Build operations for set_values_by_name_batch
    col_headers = [h.strip() for h in header]
    operations: list[dict[str, Any]] = []
    row_count = 0

    for row in rows[1:]:
        row_count += 1
        group_name = row[0].strip() if row else ""
        if not group_name:
            continue

        values: dict[str, float] = {}
        all_empty = True
        for i in range(1, min(len(row), len(col_headers))):
            cell = row[i].strip()
            if cell == "":
                continue
            all_empty = False
            try:
                values[col_headers[i]] = float(cell)
            except (ValueError, TypeError):
                continue

        if all_empty:
            continue

        operations.append({
            "domain": "ecotracer_group_input",
            "name": group_name,
            "values": values,
        })

    if not operations:
        return {
            "status": "failed",
            "error": "No valid Ecotracer group input data rows found in CSV",
            "total_csv_rows": row_count,
        }

    result = await set_values_by_name_batch(
        operations,
        options={"include_suggestions": True},
    )

    result["import_type"] = "csv"
    result["total_csv_rows"] = row_count

    return result


# ── Monte Carlo Group Params CSV ───────────────────────────────────


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def export_monte_carlo_csv(path: str | None = None) -> dict[str, Any]:
    """Export Monte Carlo group parameters (CV values) to CSV.

    Monte Carlo must be available in the current scenario.
    Returns a CSV string with Group and CV parameter columns.
    If a path is provided on the EwE host, attempts to write the CSV there.
    The CSV is always returned in the response as well.

    Args:
        path: Optional path on the EwE host to write the CSV file.

    Returns:
        Dict with status, group_count, csv (content), and path info.
    """
    import csv
    import io as _io

    # Check Monte Carlo availability
    try:
        mc_params = await _send("get_monte_carlo_parameters")
        if not isinstance(mc_params, dict) or not mc_params:
            return {"status": "error", "message": "Monte Carlo not available"}
    except (RuntimeError, ConnectionError, OSError) as exc:
        error_str = str(exc)
        if "core_unavailable" in error_str or "not available" in error_str.lower():
            return {"status": "error", "message": "Monte Carlo not available"}
        return {"status": "error", "message": f"Cannot check Monte Carlo state: {exc}"}

    # List Monte Carlo groups
    list_result = await _send("list_monte_carlo_groups")
    items = list_result.get("items", []) if isinstance(list_result, dict) else []
    if not items:
        raise ValueError("No Monte Carlo group parameters found")

    # Batch-get values for all groups
    batch_cmds: list[dict[str, Any]] = []
    indices: list[int] = []
    names: list[str] = []
    for item in items:
        idx = item.get("index")
        name = item.get("name", "")
        if idx is not None and name:
            indices.append(int(idx))
            names.append(name)
            batch_cmds.append({
                "type": "get_monte_carlo_group",
                "params": {"index": int(idx)},
            })

    if not batch_cmds:
        raise ValueError("No valid Monte Carlo group parameters found")

    batch_result = await _send("batch", {"commands": batch_cmds}, timeout=TIMEOUT_LONG)
    results_list = batch_result.get("results", []) if isinstance(batch_result, dict) else []

    # Collect all unique field names from the values dicts
    all_field_names: set[str] = set()
    group_values: dict[int, dict[str, Any]] = {}
    for i, sub in enumerate(results_list):
        if isinstance(sub, dict) and sub.get("status") != "error":
            sub_idx = sub.get("index")
            if sub_idx is not None:
                values = sub.get("values", {}) if isinstance(sub, dict) else {}
                if isinstance(values, dict):
                    group_values[int(sub_idx)] = values
                    all_field_names.update(k for k in values
                                           if k != "name" and values[k] is not None)

    sorted_fields = sorted(all_field_names)

    # Build CSV
    output = _io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Group"] + sorted_fields)

    for idx, name in zip(indices, names):
        vals = group_values.get(idx, {})
        row = [name]
        for field in sorted_fields:
            v = vals.get(field)
            if v is None or v == -9999:
                row.append("")
            else:
                row.append(str(v))
        writer.writerow(row)

    csv_content = output.getvalue()

    # Optionally write to VM host file
    written_to_host = False
    if path:
        _reject_path_traversal(path)
        if not path.lower().endswith('.csv'):
            raise ValueError("Path must have a .csv extension")
        try:
            await _send("write_host_file", {"path": path, "content": csv_content})
            written_to_host = True
        except (RuntimeError, ConnectionError, OSError, Exception):
            logger.warning(
                "export_monte_carlo_csv: write_host_file not available. "
                "CSV content is returned in the 'csv' field."
            )

    return {
        "status": "success",
        "group_count": len(indices),
        "csv": csv_content,
        "written_to_host": written_to_host,
        "path": path,
    }


@mcp.tool(annotations=ToolAnnotations(destructiveHint=True))
async def import_monte_carlo_csv(
    data: str | None = None,
    path: str | None = None,
) -> dict[str, Any]:
    """Import Monte Carlo group parameters (CV values) from CSV.

    CSV must have a header row starting with 'Group' followed by
    CV parameter columns matching the field names returned by
    get_monte_carlo_group. Uses set_values_by_name_batch for import.

    Args:
        data: CSV string content to import.
        path: Path to a CSV file on the EwE host machine to read and import.

    Returns:
        Dict with import status, total/applied/skipped counts, and results.
    """
    import csv
    import io as _io

    # Read from path if data not provided directly
    if not data and path:
        _reject_path_traversal(path)
        if not path.lower().endswith('.csv'):
            raise ValueError("Path must have a .csv extension")
        read_result = await _send("read_host_file", {"path": path})
        data = read_result.get("content", "")
        if not data:
            raise ValueError(f"File at {path} is empty or unreadable")

    if not data or not data.strip():
        raise ValueError(
            "Must provide CSV data via 'data' parameter or 'path' to a file on the EwE host"
        )

    # Parse CSV
    reader = csv.reader(_io.StringIO(data))
    rows = list(reader)
    if len(rows) < 2:
        raise ValueError("CSV must have at least a header row and one data row")

    header = rows[0]
    if not header or header[0].strip().lower() != "group":
        raise ValueError(
            "CSV header first column must be 'Group'. "
            f"Got: {header[0] if header else '(empty)'}"
        )

    col_headers = [h.strip() for h in header]
    operations: list[dict[str, Any]] = []
    row_count = 0

    for row in rows[1:]:
        row_count += 1
        group_name = row[0].strip() if row else ""
        if not group_name:
            continue

        values: dict[str, float] = {}
        all_empty = True
        for i in range(1, min(len(row), len(col_headers))):
            cell = row[i].strip()
            if cell == "":
                continue
            all_empty = False
            try:
                values[col_headers[i]] = float(cell)
            except (ValueError, TypeError):
                continue

        if all_empty:
            continue

        operations.append({
            "domain": "monte_carlo_group",
            "name": group_name,
            "values": values,
        })

    if not operations:
        return {
            "status": "failed",
            "error": "No valid Monte Carlo group data rows found in CSV",
            "total_csv_rows": row_count,
        }

    result = await set_values_by_name_batch(
        operations,
        options={"include_suggestions": True},
    )

    result["import_type"] = "csv"
    result["total_csv_rows"] = row_count

    return result


# ── Ecospace Group Inputs CSV ──────────────────────────────────────


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def export_ecospace_inputs_csv(path: str | None = None) -> dict[str, Any]:
    """Export Ecospace group input parameters to CSV.

    Requires an Ecospace scenario to be loaded first.
    Returns a CSV string with Group and Ecospace-specific parameter columns.
    If a path is provided on the EwE host, attempts to write the CSV there.
    The CSV is always returned in the response as well.

    Args:
        path: Optional path on the EwE host to write the CSV file.

    Returns:
        Dict with status, group_count, csv (content), and path info.
    """
    import csv
    import io as _io

    # Check if an Ecospace scenario is loaded
    try:
        space_state = await _send("get_ecospace_run_state")
        if not space_state.get("is_ecospace_loaded", False):
            return {"status": "error", "message": "No Ecospace scenario loaded"}
    except (RuntimeError, ConnectionError, OSError) as exc:
        return {"status": "error", "message": f"Cannot check Ecospace state: {exc}"}

    # List Ecospace group inputs
    list_result = await _send("list_ecospace_group_inputs")
    items = list_result.get("group_inputs", []) if isinstance(list_result, dict) else []
    if not items:
        raise ValueError("No Ecospace group inputs found")

    # Batch-get values for all groups
    batch_cmds: list[dict[str, Any]] = []
    indices: list[int] = []
    names: list[str] = []
    for item in items:
        idx = item.get("index")
        name = item.get("name", "")
        if idx is not None and name:
            indices.append(int(idx))
            names.append(name)
            batch_cmds.append({
                "type": "get_ecospace_group_input",
                "params": {"index": int(idx)},
            })

    if not batch_cmds:
        raise ValueError("No valid Ecospace group inputs found")

    batch_result = await _send("batch", {"commands": batch_cmds}, timeout=TIMEOUT_LONG)
    results_list = batch_result.get("results", []) if isinstance(batch_result, dict) else []

    # Collect all unique field names from the values dicts
    all_field_names: set[str] = set()
    group_values: dict[int, dict[str, Any]] = {}
    for i, sub in enumerate(results_list):
        if isinstance(sub, dict) and sub.get("status") != "error":
            sub_idx = sub.get("index")
            if sub_idx is not None:
                values = sub.get("values", {}) if isinstance(sub, dict) else {}
                if isinstance(values, dict):
                    group_values[int(sub_idx)] = values
                    all_field_names.update(k for k in values
                                           if k != "name" and values[k] is not None)

    sorted_fields = sorted(all_field_names)

    # Build CSV
    output = _io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Group"] + sorted_fields)

    for idx, name in zip(indices, names):
        vals = group_values.get(idx, {})
        row = [name]
        for field in sorted_fields:
            v = vals.get(field)
            if v is None or v == -9999:
                row.append("")
            else:
                row.append(str(v))
        writer.writerow(row)

    csv_content = output.getvalue()

    # Optionally write to VM host file
    written_to_host = False
    if path:
        _reject_path_traversal(path)
        if not path.lower().endswith('.csv'):
            raise ValueError("Path must have a .csv extension")
        try:
            await _send("write_host_file", {"path": path, "content": csv_content})
            written_to_host = True
        except (RuntimeError, ConnectionError, OSError, Exception):
            logger.warning(
                "export_ecospace_inputs_csv: write_host_file not available. "
                "CSV content is returned in the 'csv' field."
            )

    return {
        "status": "success",
        "group_count": len(indices),
        "csv": csv_content,
        "written_to_host": written_to_host,
        "path": path,
    }


@mcp.tool(annotations=ToolAnnotations(destructiveHint=True))
async def import_ecospace_inputs_csv(
    data: str | None = None,
    path: str | None = None,
) -> dict[str, Any]:
    """Import Ecospace group input parameters from CSV.

    CSV must have a header row starting with 'Group' followed by
    Ecospace parameter columns matching the field names returned by
    get_ecospace_group_input. Uses set_values_by_name_batch for import.

    Args:
        data: CSV string content to import.
        path: Path to a CSV file on the EwE host machine to read and import.

    Returns:
        Dict with import status, total/applied/skipped counts, and results.
    """
    import csv
    import io as _io

    # Read from path if data not provided directly
    if not data and path:
        _reject_path_traversal(path)
        if not path.lower().endswith('.csv'):
            raise ValueError("Path must have a .csv extension")
        read_result = await _send("read_host_file", {"path": path})
        data = read_result.get("content", "")
        if not data:
            raise ValueError(f"File at {path} is empty or unreadable")

    if not data or not data.strip():
        raise ValueError(
            "Must provide CSV data via 'data' parameter or 'path' to a file on the EwE host"
        )

    # Parse CSV
    reader = csv.reader(_io.StringIO(data))
    rows = list(reader)
    if len(rows) < 2:
        raise ValueError("CSV must have at least a header row and one data row")

    header = rows[0]
    if not header or header[0].strip().lower() != "group":
        raise ValueError(
            "CSV header first column must be 'Group'. "
            f"Got: {header[0] if header else '(empty)'}"
        )

    col_headers = [h.strip() for h in header]
    operations: list[dict[str, Any]] = []
    row_count = 0

    for row in rows[1:]:
        row_count += 1
        group_name = row[0].strip() if row else ""
        if not group_name:
            continue

        values: dict[str, float] = {}
        all_empty = True
        for i in range(1, min(len(row), len(col_headers))):
            cell = row[i].strip()
            if cell == "":
                continue
            all_empty = False
            try:
                values[col_headers[i]] = float(cell)
            except (ValueError, TypeError):
                continue

        if all_empty:
            continue

        operations.append({
            "domain": "ecospace_group_input",
            "name": group_name,
            "values": values,
        })

    if not operations:
        return {
            "status": "failed",
            "error": "No valid Ecospace group input data rows found in CSV",
            "total_csv_rows": row_count,
        }

    result = await set_values_by_name_batch(
        operations,
        options={"include_suggestions": True},
    )

    result["import_type"] = "csv"
    result["total_csv_rows"] = row_count

    return result


# ── Ecospace Fleet Inputs CSV ────────────────────────────────────────


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def export_ecospace_fleet_inputs_csv(path: str | None = None) -> dict[str, Any]:
    """Export Ecospace fleet input parameters to CSV.

    Requires an Ecospace scenario to be loaded first.
    Returns a CSV string with Fleet and Ecospace-specific parameter columns.
    If a path is provided on the EwE host, attempts to write the CSV there.
    The CSV is always returned in the response as well.

    Args:
        path: Optional path on the EwE host to write the CSV file.

    Returns:
        Dict with status, fleet_count, csv (content), and path info.
    """
    import csv
    import io as _io

    # Check if an Ecospace scenario is loaded
    try:
        space_state = await _send("get_ecospace_run_state")
        if not space_state.get("is_ecospace_loaded", False):
            return {"status": "error", "message": "No Ecospace scenario loaded"}
    except (RuntimeError, ConnectionError, OSError) as exc:
        return {"status": "error", "message": f"Cannot check Ecospace state: {exc}"}

    # List Ecospace fleet inputs
    list_result = await _send("list_ecospace_fleet_inputs")
    items: list[dict[str, Any]] = []
    if isinstance(list_result, dict):
        # Three-key fallback: earlier plugin versions used "fleet_inputs", later
        # builds migrated to "items", and older EwE cores emitted "fleets".  All
        # three are checked because the C# plugin response key has not been
        # formally standardised across every plugin + core combination.
        items = (
            list_result.get("fleet_inputs", [])
            or list_result.get("items", [])
            or list_result.get("fleets", [])
        )
    if not items:
        raise ValueError("No Ecospace fleet inputs found")

    # Batch-get values for all fleets
    batch_cmds: list[dict[str, Any]] = []
    indices: list[int] = []
    names: list[str] = []
    for item in items:
        idx = item.get("index")
        name = item.get("name", "")
        if idx is not None and name:
            indices.append(int(idx))
            names.append(name)
            batch_cmds.append({
                "type": "get_ecospace_fleet_input",
                "params": {"index": int(idx)},
            })

    if not batch_cmds:
        raise ValueError("No valid Ecospace fleet inputs found")

    batch_result = await _send("batch", {"commands": batch_cmds}, timeout=TIMEOUT_LONG)
    results_list = batch_result.get("results", []) if isinstance(batch_result, dict) else []

    # Collect all unique field names from the values dicts
    all_field_names: set[str] = set()
    fleet_values: dict[int, dict[str, Any]] = {}
    for i, sub in enumerate(results_list):
        if isinstance(sub, dict) and sub.get("status") != "error":
            sub_idx = sub.get("index")
            if sub_idx is not None:
                values = sub.get("values", {}) if isinstance(sub, dict) else {}
                if isinstance(values, dict):
                    fleet_values[int(sub_idx)] = values
                    all_field_names.update(k for k in values
                                           if k != "name" and values[k] is not None)

    sorted_fields = sorted(all_field_names)

    # Build CSV
    output = _io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Fleet"] + sorted_fields)

    for idx, name in zip(indices, names):
        vals = fleet_values.get(idx, {})
        row = [name]
        for field in sorted_fields:
            v = vals.get(field)
            if v is None or v == -9999:
                row.append("")
            else:
                row.append(str(v))
        writer.writerow(row)

    csv_content = output.getvalue()

    # Optionally write to VM host file
    written_to_host = False
    if path:
        _reject_path_traversal(path)
        if not path.lower().endswith('.csv'):
            raise ValueError("Path must have a .csv extension")
        try:
            await _send("write_host_file", {"path": path, "content": csv_content})
            written_to_host = True
        except (RuntimeError, ConnectionError, OSError, Exception):
            logger.warning(
                "export_ecospace_fleet_inputs_csv: write_host_file not available. "
                "CSV content is returned in the 'csv' field."
            )

    return {
        "status": "success",
        "fleet_count": len(indices),
        "csv": csv_content,
        "written_to_host": written_to_host,
        "path": path,
    }


@mcp.tool(annotations=ToolAnnotations(destructiveHint=True))
async def import_ecospace_fleet_inputs_csv(
    data: str | None = None,
    path: str | None = None,
) -> dict[str, Any]:
    """Import Ecospace fleet input parameters from CSV.

    CSV must have a header row starting with 'Fleet' followed by
    Ecospace parameter columns matching the field names returned by
    get_ecospace_fleet_input. Uses set_values_by_name_batch for import.

    Args:
        data: CSV string content to import.
        path: Path to a CSV file on the EwE host machine to read and import.

    Returns:
        Dict with import status, total/applied/skipped counts, and results.
    """
    import csv
    import io as _io

    # Read from path if data not provided directly
    if not data and path:
        _reject_path_traversal(path)
        if not path.lower().endswith('.csv'):
            raise ValueError("Path must have a .csv extension")
        read_result = await _send("read_host_file", {"path": path})
        data = read_result.get("content", "")
        if not data:
            raise ValueError(f"File at {path} is empty or unreadable")

    if not data or not data.strip():
        raise ValueError(
            "Must provide CSV data via 'data' parameter or 'path' to a file on the EwE host"
        )

    # Parse CSV
    reader = csv.reader(_io.StringIO(data))
    rows = list(reader)
    if len(rows) < 2:
        raise ValueError("CSV must have at least a header row and one data row")

    header = rows[0]
    if not header or header[0].strip().lower() != "fleet":
        raise ValueError(
            "CSV header first column must be 'Fleet'. "
            f"Got: {header[0] if header else '(empty)'}"
        )

    col_headers = [h.strip() for h in header]
    operations: list[dict[str, Any]] = []
    row_count = 0

    for row in rows[1:]:
        row_count += 1
        fleet_name = row[0].strip() if row else ""
        if not fleet_name:
            continue

        values: dict[str, float] = {}
        all_empty = True
        for i in range(1, min(len(row), len(col_headers))):
            cell = row[i].strip()
            if cell == "":
                continue
            all_empty = False
            try:
                values[col_headers[i]] = float(cell)
            except (ValueError, TypeError):
                continue

        if all_empty:
            continue

        operations.append({
            "domain": "ecospace_fleet_input",
            "name": fleet_name,
            "values": values,
        })

    if not operations:
        return {
            "status": "failed",
            "error": "No valid Ecospace fleet input data rows found in CSV",
            "total_csv_rows": row_count,
        }

    result = await set_values_by_name_batch(
        operations,
        options={"include_suggestions": True},
    )

    result["import_type"] = "csv"
    result["total_csv_rows"] = row_count

    return result


# ── Fleet Indexed Value Matrix CSV ────────────────────────────────────

_VALID_FLEET_INDEXED_VARIABLES = frozenset({
    "off_vessel_price",
    "off_vessel_value",
    "discard_fate",
    "discard_mortality",
})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def export_fleet_indexed_csv(
    variable: str,
    path: str | None = None,
) -> dict[str, Any]:
    """Export a fleet×group indexed value matrix as CSV.

    Supported variables: off_vessel_price, off_vessel_value, discard_fate,
    discard_mortality.

    Builds a CSV with Fleet as the first column and group names as subsequent
    column headers. One row per fleet with the indexed values for each group.

    Args:
        variable: The indexed variable to export.
        path: Optional path on the EwE host to write the CSV file.
              The CSV content is always returned regardless.

    Returns:
        Dict with status, fleet_count, group_count, csv (content), and path info.
    """
    import csv
    import io as _io

    var_lower = variable.lower()
    if var_lower not in _VALID_FLEET_INDEXED_VARIABLES:
        raise ValueError(
            f"Unsupported variable '{variable}'. "
            f"Supported: {sorted(_VALID_FLEET_INDEXED_VARIABLES)}"
        )

    # Step 1: Get all fleets
    fleets_result = await _send("list_ecopath_fleets")
    fleets = fleets_result.get("fleets", [])
    if not fleets:
        raise ValueError("No fleets found in the current model")

    fleet_names: list[str] = []
    fleet_indices: list[int] = []
    for f in fleets:
        fname = f.get("name", "")
        fci = f.get("index")
        if fname and fci is not None:
            fleet_names.append(fname)
            fleet_indices.append(int(fci))

    # Step 2: Get all groups
    groups_result = await _send("list_ecopath_groups")
    groups = groups_result.get("groups", [])
    group_names: list[str] = []
    group_indices: list[int] = []
    for g in groups:
        gname = g.get("name", "")
        gidx = g.get("index")
        if gname and gidx is not None:
            group_names.append(gname)
            group_indices.append(int(gidx))

    n_groups = len(group_names)
    if n_groups == 0:
        raise ValueError("No valid groups found in the current model")

    # Step 3: Batch get the indexed variable for ALL fleets
    batch_cmds: list[dict[str, Any]] = []
    for fi in fleet_indices:
        batch_cmds.append({
            "type": "get_values",
            "params": {
                "domain": "ecopath_fleet",
                "index": fi,
                "variables": [var_lower],
                "navigate": False,
            },
        })

    batch_result = await _send("batch", {"commands": batch_cmds}, timeout=TIMEOUT_LONG)
    results_list = batch_result.get("results", [])

    # Step 4: Build matrix: fleet_index → array of values
    fleet_array: dict[int, list[float]] = {}
    for i, sub in enumerate(results_list):
        if isinstance(sub, dict) and sub.get("status") != "error":
            sub_idx = sub.get("index")
            if sub_idx is None and i < len(fleet_indices):
                sub_idx = fleet_indices[i]
            if sub_idx is not None:
                values = sub.get("values", {}) if isinstance(sub, dict) else {}
                arr = values.get(var_lower, [])
                if isinstance(arr, list):
                    fleet_array[int(sub_idx)] = [
                        float(v) if v is not None else 0.0 for v in arr
                    ]

    # Step 5: Build CSV
    output = _io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Fleet"] + group_names)

    for idx in fleet_indices:
        row_name = fleet_names[idx - 1]  # fleets are 1-based
        arr = fleet_array.get(idx, [])
        row_values: list[str] = [row_name]
        for g_idx in range(n_groups):
            val = 0.0
            if g_idx < len(arr):
                val = arr[g_idx] if arr[g_idx] is not None else 0.0
            if val == 0.0:
                row_values.append("0.0")
            else:
                row_values.append(f"{val:.6f}")
        writer.writerow(row_values)

    csv_content = output.getvalue()

    # Step 6: Optionally write to VM host file
    written_to_host = False
    if path:
        _reject_path_traversal(path)
        if not path.lower().endswith('.csv'):
            raise ValueError("Path must have a .csv extension")
        try:
            await _send("write_host_file", {"path": path, "content": csv_content})
            written_to_host = True
        except (RuntimeError, ConnectionError, OSError, Exception):
            logger.warning(
                "export_fleet_indexed_csv: write_host_file not available on this plugin version. "
                "CSV content is returned in the 'csv' field."
            )

    return {
        "status": "success",
        "variable": var_lower,
        "fleet_count": len(fleet_indices),
        "group_count": n_groups,
        "csv": csv_content,
        "written_to_host": written_to_host,
        "path": path,
    }


@mcp.tool(annotations=ToolAnnotations(destructiveHint=True))
async def import_fleet_indexed_csv(
    variable: str,
    data: str | None = None,
    path: str | None = None,
) -> dict[str, Any]:
    """Import a fleet×group indexed value matrix from CSV.

    CSV must have 'Fleet' as the first column header followed by group
    names as subsequent column headers. One row per fleet.

    For each non-zero value, uses set_indexed_value with navigate=False
    via batch_commands.

    Args:
        variable: The indexed variable to import into.
        data: CSV string content to import.
        path: Path to a CSV file on the EwE host machine to read and import.

    Returns:
        Dict with import status, counts, and results.
    """
    import csv
    import io as _io

    var_lower = variable.lower()
    if var_lower not in _VALID_FLEET_INDEXED_VARIABLES:
        raise ValueError(
            f"Unsupported variable '{variable}'. "
            f"Supported: {sorted(_VALID_FLEET_INDEXED_VARIABLES)}"
        )

    # Read from path if data not provided directly
    if not data and path:
        _reject_path_traversal(path)
        if not path.lower().endswith('.csv'):
            raise ValueError("Path must have a .csv extension")
        read_result = await _send("read_host_file", {"path": path})
        data = read_result.get("content", "")
        if not data:
            raise ValueError(f"File at {path} is empty or unreadable")

    if not data or not data.strip():
        raise ValueError(
            "Either 'data' or 'path' must be provided with non-empty content"
        )

    # Parse CSV
    reader = csv.reader(_io.StringIO(data))
    rows = list(reader)
    if len(rows) < 2:
        raise ValueError("CSV must have at least a header row and one data row")

    header = rows[0]
    if not header or header[0].strip().lower() != "fleet":
        raise ValueError(
            "CSV header must start with 'Fleet' as the first column. "
            f"Got: {header[0] if header else '(empty)'}"
        )

    # Build name → index mappings
    fleets_result = await _send("list_ecopath_fleets")
    fleets = fleets_result.get("fleets", [])
    fleet_name_to_idx: dict[str, int] = {}
    for f in fleets:
        fname = f.get("name", "")
        fci = f.get("index")
        if fname and fci is not None:
            fleet_name_to_idx[fname] = int(fci)

    groups_result = await _send("list_ecopath_groups")
    groups = groups_result.get("groups", [])
    group_name_to_idx: dict[str, int] = {}
    for g in groups:
        gname = g.get("name", "")
        gidx = g.get("index")
        if gname and gidx is not None:
            group_name_to_idx[gname] = int(gidx)

    # CSV group column names (after 'Fleet')
    csv_group_names = [h.strip() for h in header[1:]]

    # Build batch commands for each non-zero value
    batch_cmds: list[dict[str, Any]] = []
    set_count = 0
    row_count = 0
    skipped: list[dict[str, Any]] = []

    for row in rows[1:]:
        if not any(cell.strip() for cell in row):
            continue
        row_count += 1
        fleet_name = row[0].strip()
        if not fleet_name:
            skipped.append({"row": row_count, "reason": "Empty fleet name"})
            continue

        fleet_idx = fleet_name_to_idx.get(fleet_name)
        if fleet_idx is None:
            skipped.append({"row": row_count, "reason": f"Fleet '{fleet_name}' not found in model"})
            continue

        for ci, csv_group_name in enumerate(csv_group_names, start=1):
            cell = row[ci].strip() if ci < len(row) else "0"
            if cell == "" or cell == "0" or cell == "0.0":
                continue

            try:
                val = float(cell)
            except (ValueError, TypeError):
                skipped.append({"row": row_count, "reason": f"Invalid value '{cell}' for group '{csv_group_name}'"})
                continue

            if val == 0.0:
                continue

            group_idx = group_name_to_idx.get(csv_group_name)
            if group_idx is None:
                # Try case-insensitive match
                matched = False
                for gname, gidx in group_name_to_idx.items():
                    if gname.lower() == csv_group_name.lower():
                        group_idx = gidx
                        matched = True
                        break
                if not matched:
                    skipped.append({"row": row_count, "reason": f"Group '{csv_group_name}' not found in model"})
                    continue

            batch_cmds.append({
                "type": "set_indexed_value",
                "params": {
                    "domain": "ecopath_fleet",
                    "index": fleet_idx,
                    "variable": var_lower,
                    "indices": [group_idx],
                    "value": val,
                    "navigate": False,
                },
            })
            set_count += 1

    if not batch_cmds:
        return {
            "status": "success",
            "message": "No non-zero values to import from CSV",
            "variable": var_lower,
            "total_csv_rows": row_count,
            "skipped": skipped or None,
        }

    # Execute batch
    send_errors: list[dict[str, Any]] = []
    n_applied = 0
    try:
        batch_result = await _send("batch", {"commands": batch_cmds}, timeout=TIMEOUT_LONG)
        results_list = batch_result.get("results", []) if isinstance(batch_result, dict) else []
        for i, sub in enumerate(results_list):
            if isinstance(sub, dict) and sub.get("status") == "error":
                send_errors.append({"index": i, "error": sub.get("message", str(sub))})
            else:
                n_applied += 1
    except Exception as exc:
        send_errors.append({"error": str(exc)})

    status = "success"
    if send_errors or skipped:
        status = "partial_success" if n_applied else "failed"

    return {
        "status": status,
        "variable": var_lower,
        "total_csv_rows": row_count,
        "n_sets_requested": set_count,
        "n_applied": n_applied,
        "n_errors": len(send_errors),
        "errors": send_errors or None,
        "skipped": skipped or None,
    }


# ── Detritus Fate CSV ────────────────────────────────────────────────


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def export_detritus_fate_csv(path: str | None = None) -> dict[str, Any]:
    """Export detritus fate fractions for all groups as CSV.

    Builds a CSV with Group as the first column and detritus group names
    as subsequent column headers. One row per non-detritus group with
    the detritus fate fractions.

    Args:
        path: Optional path on the EwE host to write the CSV file.
              The CSV content is always returned regardless.

    Returns:
        Dict with status, group_count, detritus_count, csv (content), and path info.
    """
    import csv
    import io as _io

    # Step 1: Get all groups
    groups_result = await _send("list_ecopath_groups")
    groups = groups_result.get("groups", [])
    if not groups:
        raise ValueError("No groups found in the current model")

    # Identify detritus groups and non-detritus groups
    detritus_group_indices: list[int] = []
    detritus_group_names: list[str] = []
    non_detritus_indices: list[int] = []
    non_detritus_names: list[str] = []

    for g in groups:
        gname = g.get("name", "")
        gidx = g.get("index")
        gtype = g.get("type", "").lower()
        if gname and gidx is not None:
            if gtype == "detritus" or gtype == 2 or gtype == "2":
                detritus_group_indices.append(int(gidx))
                detritus_group_names.append(gname)
            else:
                non_detritus_indices.append(int(gidx))
                non_detritus_names.append(gname)

    n_detritus = len(detritus_group_indices)
    fallback_mode = False
    if n_detritus == 0:
        fallback_mode = True
        # Fallback: use index-based detritus detection — the last group is
        # often detritus, but we'll check if detritus_fate has values.
        # For safety, treat group 1 as the likely detritus group (common in
        # many EwE models) and report all groups.
        non_detritus_indices = []
        non_detritus_names = []
        for g in groups:
            gname = g.get("name", "")
            gidx = g.get("index")
            if gname and gidx is not None:
                non_detritus_indices.append(int(gidx))
                non_detritus_names.append(gname)

    # Step 2: Batch get detritus_fate for all non-detritus groups
    batch_cmds: list[dict[str, Any]] = []
    for idx in non_detritus_indices:
        batch_cmds.append({
            "type": "get_values",
            "params": {
                "domain": "ecopath_group",
                "index": idx,
                "variables": ["detritus_fate"],
                "navigate": False,
            },
        })

    batch_result = await _send("batch", {"commands": batch_cmds}, timeout=TIMEOUT_LONG)
    results_list = batch_result.get("results", [])

    # Step 3: Build detritus fate matrix: group_index → array
    fate_matrix: dict[int, list[float]] = {}
    for i, sub in enumerate(results_list):
        if isinstance(sub, dict) and sub.get("status") != "error":
            sub_idx = sub.get("index")
            if sub_idx is None and i < len(non_detritus_indices):
                sub_idx = non_detritus_indices[i]
            if sub_idx is not None:
                values = sub.get("values", {}) if isinstance(sub, dict) else {}
                fate = values.get("detritus_fate", [])
                if isinstance(fate, list):
                    fate_matrix[int(sub_idx)] = [
                        float(v) if v is not None else 0.0 for v in fate
                    ]

    # Step 4: Determine detritus group count from data if we didn't find them by type
    if n_detritus == 0:
        # Use the max array length from any group's detritus_fate
        n_from_data = max((len(arr) for arr in fate_matrix.values()), default=0)
        if n_from_data == 0 and non_detritus_names:
            # At least 1 detritus column
            n_from_data = 1
        detritus_names_for_csv = [f"Detritus{i+1}" for i in range(n_from_data)]
    else:
        detritus_names_for_csv = detritus_group_names

    # Step 5: Build CSV
    output = _io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Group"] + detritus_names_for_csv)

    row_count = 0
    for idx, name in zip(non_detritus_indices, non_detritus_names):
        fate = fate_matrix.get(idx, [])
        row_values: list[str] = [name]
        has_non_zero = False
        for d_idx in range(len(detritus_names_for_csv)):
            val = 0.0
            if d_idx < len(fate):
                val = fate[d_idx] if fate[d_idx] is not None else 0.0
            if val != 0.0:
                has_non_zero = True
                row_values.append(f"{val:.6f}")
            else:
                row_values.append("0.0")
        if has_non_zero or any(v != "0.0" for v in row_values[1:]):
            writer.writerow(row_values)
            row_count += 1
        else:
            # Still write the row even if all zeros — gives complete picture
            writer.writerow(row_values)
            row_count += 1

    csv_content = output.getvalue()

    # Step 6: Optionally write to VM host file
    written_to_host = False
    if path:
        _reject_path_traversal(path)
        if not path.lower().endswith('.csv'):
            raise ValueError("Path must have a .csv extension")
        try:
            await _send("write_host_file", {"path": path, "content": csv_content})
            written_to_host = True
        except (RuntimeError, ConnectionError, OSError, Exception):
            logger.warning(
                "export_detritus_fate_csv: write_host_file not available on this plugin version. "
                "CSV content is returned in the 'csv' field."
            )

    return {
        "status": "success",
        "group_count": len(non_detritus_indices),
        "detritus_count": len(detritus_names_for_csv),
        "fallback_mode": fallback_mode,
        "csv": csv_content,
        "written_to_host": written_to_host,
        "path": path,
    }


@mcp.tool(annotations=ToolAnnotations(destructiveHint=True))
async def import_detritus_fate_csv(
    data: str | None = None,
    path: str | None = None,
) -> dict[str, Any]:
    """Import detritus fate fractions from CSV.

    CSV must have 'Group' as the first column header followed by detritus
    group names (or 'DetritusFraction' for single-detritus models). Uses
    set_indexed_value with navigate=False via batch_commands.

    Args:
        data: CSV string content to import.
        path: Path to a CSV file on the EwE host machine to read and import.

    Returns:
        Dict with import status, counts, and results.
    """
    import csv
    import io as _io

    # Read from path if data not provided directly
    if not data and path:
        _reject_path_traversal(path)
        if not path.lower().endswith('.csv'):
            raise ValueError("Path must have a .csv extension")
        read_result = await _send("read_host_file", {"path": path})
        data = read_result.get("content", "")
        if not data:
            raise ValueError(f"File at {path} is empty or unreadable")

    if not data or not data.strip():
        raise ValueError(
            "Either 'data' or 'path' must be provided with non-empty content"
        )

    # Parse CSV
    reader = csv.reader(_io.StringIO(data))
    rows = list(reader)
    if len(rows) < 2:
        raise ValueError("CSV must have at least a header row and one data row")

    header = rows[0]
    if not header or header[0].strip().lower() != "group":
        raise ValueError(
            "CSV header must start with 'Group' as the first column. "
            f"Got: {header[0] if header else '(empty)'}"
        )

    # CSV detritus column names (after 'Group')
    csv_detritus_names = [h.strip() for h in header[1:]]
    if not csv_detritus_names:
        raise ValueError("CSV must have at least one detritus column after 'Group'")

    # Build name → index mapping for groups
    groups_result = await _send("list_ecopath_groups")
    groups = groups_result.get("groups", [])
    group_name_to_idx: dict[str, int] = {}
    for g in groups:
        gname = g.get("name", "")
        gidx = g.get("index")
        if gname and gidx is not None:
            group_name_to_idx[gname] = int(gidx)

    # Determine detritus group indices
    # First try to find groups with type 'detritus'
    detritus_name_to_idx: dict[str, int] = {}
    for g in groups:
        gname = g.get("name", "")
        gidx = g.get("index")
        gtype = g.get("type", "").lower()
        if gname and gidx is not None and (gtype == "detritus" or gtype == 2 or gtype == "2"):
            detritus_name_to_idx[gname] = int(gidx)

    # Map CSV detritus column names to detritus group indices
    detritus_col_map: list[tuple[int, int]] = []  # (csv_col_index, detritus_group_index)
    for ci, csv_name in enumerate(csv_detritus_names, start=1):
        # Try exact match first
        det_idx = detritus_name_to_idx.get(csv_name)
        if det_idx is None:
            # Try case-insensitive match
            d_lower = csv_name.lower()
            for gname, gidx in detritus_name_to_idx.items():
                if gname.lower() == d_lower:
                    det_idx = gidx
                    break
        if det_idx is None:
            # If there's only one detritus group, use it regardless of name
            if len(detritus_name_to_idx) == 1:
                det_idx = next(iter(detritus_name_to_idx.values()))
            else:
                # If multiple detritus groups but CSV has a name we can't match
                # try numeric: "Detritus1" → first, "Detritus2" → second
                if csv_name.lower().startswith("detritus"):
                    try:
                        det_num = int(csv_name[len("detritus"):])
                        sorted_names = sorted(detritus_name_to_idx.keys(),
                                              key=lambda n: detritus_name_to_idx[n])
                        if 1 <= det_num <= len(sorted_names):
                            det_idx = detritus_name_to_idx[sorted_names[det_num - 1]]
                    except (ValueError, IndexError):
                        pass
                if det_idx is None:
                    # If all else fails and we have any detritus groups, use the first
                    if detritus_name_to_idx:
                        sorted_names = sorted(detritus_name_to_idx.keys(),
                                              key=lambda n: detritus_name_to_idx[n])
                        if ci - 1 < len(sorted_names):
                            det_idx = detritus_name_to_idx[sorted_names[ci - 1]]
        if det_idx is not None:
            detritus_col_map.append((ci, det_idx))

    if not detritus_col_map:
        # Fallback: if no detritus groups found, use group 1 as default detritus
        raise ValueError(
            "No detritus groups found in the model. "
            "Cannot import detritus fate fractions."
        )

    # Build batch commands
    batch_cmds: list[dict[str, Any]] = []
    set_count = 0
    row_count = 0
    skipped: list[dict[str, Any]] = []

    for row in rows[1:]:
        if not any(cell.strip() for cell in row):
            continue
        row_count += 1
        group_name = row[0].strip()
        if not group_name:
            skipped.append({"row": row_count, "reason": "Empty group name"})
            continue

        group_idx = group_name_to_idx.get(group_name)
        if group_idx is None:
            # Try case-insensitive match
            matched = False
            for gname, gidx in group_name_to_idx.items():
                if gname.lower() == group_name.lower():
                    group_idx = gidx
                    matched = True
                    break
            if not matched:
                skipped.append({"row": row_count, "reason": f"Group '{group_name}' not found in model"})
                continue

        for ci, det_idx in detritus_col_map:
            cell = row[ci].strip() if ci < len(row) else "0"
            if cell == "" or cell == "0" or cell == "0.0":
                continue

            try:
                val = float(cell)
            except (ValueError, TypeError):
                skipped.append({"row": row_count, "reason": f"Invalid value '{cell}' for detritus index {det_idx}"})
                continue

            if val == 0.0:
                continue

            batch_cmds.append({
                "type": "set_indexed_value",
                "params": {
                    "domain": "ecopath_group",
                    "index": group_idx,
                    "variable": "detritus_fate",
                    "indices": [det_idx],
                    "value": val,
                    "navigate": False,
                },
            })
            set_count += 1

    if not batch_cmds:
        return {
            "status": "success",
            "message": "No non-zero values to import from CSV",
            "total_csv_rows": row_count,
            "skipped": skipped or None,
        }

    # Execute batch
    send_errors: list[dict[str, Any]] = []
    n_applied = 0
    try:
        batch_result = await _send("batch", {"commands": batch_cmds}, timeout=TIMEOUT_LONG)
        results_list = batch_result.get("results", []) if isinstance(batch_result, dict) else []
        for i, sub in enumerate(results_list):
            if isinstance(sub, dict) and sub.get("status") == "error":
                send_errors.append({"index": i, "error": sub.get("message", str(sub))})
            else:
                n_applied += 1
    except Exception as exc:
        send_errors.append({"error": str(exc)})

    status = "success"
    if send_errors or skipped:
        status = "partial_success" if n_applied else "failed"

    return {
        "status": status,
        "total_csv_rows": row_count,
        "n_sets_requested": set_count,
        "n_applied": n_applied,
        "n_errors": len(send_errors),
        "errors": send_errors or None,
        "skipped": skipped or None,
    }


# ── Name-based entity access ──────────────────────────────────────


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def resolve_name(domain: str, name: str) -> dict[str, Any]:
    """Resolve an entity name to its numeric index in a domain.

    Args:
        domain: Domain name (e.g., 'ecopath_group', 'ecosim_group_input',
                'ecospace_habitat'). Use list_domains() to see all options.
        name: Exact entity name (case-insensitive).

    Returns:
        Dict with domain, index, name, and type fields.
    Raises:
        EwEMCPError with code 'name_not_found' if no exact match exists.
    """
    if not name.strip():
        raise ValueError("name must not be empty")
    return await _send("resolve_entity_name", {"domain": domain, "name": name.strip()})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_values_by_name(
    domain: str,
    name: str,
    variables: list[str] | None = None,
) -> dict[str, Any]:
    """Get values from an entity by name instead of numeric index.

    Resolves the entity name to its index internally, then reads the
    requested variables. Safer than managing numeric indices manually.

    Args:
        domain: Domain name (e.g., 'ecopath_group', 'ecosim_group_input').
        name: Exact entity name (case-insensitive).
        variables: Optional list of variable names to read. If None, all
                   readable scalar variables are returned.
    """
    if not name.strip():
        raise ValueError("name must not be empty")
    resolved = await _send("resolve_entity_name", {"domain": domain, "name": name})
    params: dict[str, Any] = {
        "domain": domain,
        "index": resolved["index"],
        "navigate": False,
    }
    if variables is not None:
        params["variables"] = variables
    result = await _send("get_values", params)
    result["_resolved"] = dict(resolved)
    return result


@mcp.tool(annotations=ToolAnnotations(destructiveHint=True))
async def set_values_by_name(
    domain: str,
    name: str,
    values: dict[str, Any],
    navigate: bool = False,
) -> dict[str, Any]:
    """Set values on an entity by name instead of numeric index.

    Resolves the entity name to its index internally, then writes the
    given values. Safer than managing numeric indices manually.

    For multi-domain edits, prefer set_values_by_name_batch over ad-hoc
    index lookup loops.

    Args:
        domain: Domain name (e.g., 'ecopath_group', 'ecosim_group_input').
        name: Exact entity name (case-insensitive).
        values: Dict of variable-name to value to set.
        navigate: Whether to navigate the EwE UI to show the change.
                  Defaults to False for quiet background access.
    """
    if not name.strip():
        raise ValueError("name must not be empty")
    resolved = await _send("resolve_entity_name", {"domain": domain, "name": name})
    result = await _send("set_values", {
        "domain": domain,
        "index": resolved["index"],
        "values": values,
        "navigate": navigate,
    })
    result["_resolved"] = dict(resolved)
    return result


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def list_ecosim_fleet_inputs() -> dict[str, Any]:
    """List Ecosim fleet input parameters for the active scenario.

    Key parameters include:
    - rel_q: Relative catchability (minimum 1.0). Scales fishing mortality
      rate for this fleet.
    - effort_conversion_factor: Scales capital-to-effort conversion in fleet
      dynamics. Does NOT directly multiply fishing mortality (F). Instead it
      influences how capital investment translates to effective fishing effort
      in the fleet size dynamics model.
    """
    return await _send("list_ecosim_fleet_inputs")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_ecosim_fleet_input(index: int) -> dict[str, Any]:
    """Get one Ecosim fleet input parameter record."""
    return await _send("get_ecosim_fleet_input", {"index": index})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def list_ecosim_forcing_shapes() -> dict[str, Any]:
    """List Ecosim forcing shape functions for the active scenario."""
    return await _send("list_ecosim_forcing_shapes")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_ecosim_forcing_shape(index: int) -> dict[str, Any]:
    """Get data points for one Ecosim forcing shape."""
    return await _send("get_ecosim_forcing_shape", {"index": index})


@mcp.tool(annotations=ToolAnnotations(destructiveHint=True))
async def set_ecosim_forcing_shape(
    index: int, shape_data: list[float]
) -> dict[str, Any]:
    """Modify Ecosim forcing shape data points."""
    return await _send(
        "set_ecosim_forcing_shape",
        {"index": index, "shape_data": shape_data},
    )


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def list_ecosim_pp_forcing() -> dict[str, Any]:
    """List primary production forcing assignments for Ecosim groups."""
    return await _send("list_ecosim_pp_forcing")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_ecosim_pp_forcing(index: int) -> dict[str, Any]:
    """Get primary production forcing data for an Ecosim group."""
    return await _send("get_ecosim_pp_forcing", {"index": index})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_ecosim_group_results(
    index: int | None = None,
    variables: list[str] | None = None,
    start_time: int | None = None,
    end_time: int | None = None,
    fleet_index: int | None = None,
    list: bool = False,
) -> dict[str, Any]:
    """Read Ecosim group result time-series after a run.

    Set `list=True` to return results for all groups instead of a single
    group by index. When `list=True`, the `index` parameter is ignored.

    Known variable names: biomass, biomass_rel, catch, catch_rel,
    feeding_time, fish_mort, pred_mort, total_mort, tl,
    consump_biomass, avg_weight, prod_consump, catch_by_fleet,
    fishing_mort_by_fleet, value, value_rel, value_by_fleet,
    landings_by_fleet, discard_by_fleet.

    Note: catch can also be requested as "yield".
    Variables ending in _by_fleet require a fleet_index parameter.
    Additional variables may be available depending on the EwE build.
    If variables is None, all available variables are returned.
    Unknown variable names will be rejected by the EwE plugin with
    a descriptive error.
    """
    params: dict[str, Any] = {}
    if index is not None:
        params["index"] = index
    if variables is not None:
        params["variables"] = variables
    if start_time is not None:
        params["start_time"] = start_time
    if end_time is not None:
        params["end_time"] = end_time
    if fleet_index is not None:
        params["fleet_index"] = fleet_index
    if list:
        params["list"] = True
    return await _send("get_ecosim_group_results", params, timeout=TIMEOUT_LONG)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_ecosim_fleet_results(
    index: int,
    variables: list[str] | None = None,
    start_time: int | None = None,
    end_time: int | None = None,
) -> dict[str, Any]:
    """Read Ecosim fleet result time-series after a run.

    Known variable names: catch_biomass, value.
    Note: catch_biomass can also be requested as "catch".
    Additional variables may be available depending on the EwE build.
    If variables is None, all available variables are returned.
    Unknown variable names will be rejected by the EwE plugin with
    a descriptive error.
    """
    params: dict[str, Any] = {"index": index}
    if variables is not None:
        params["variables"] = variables
    if start_time is not None:
        params["start_time"] = start_time
    if end_time is not None:
        params["end_time"] = end_time
    return await _send("get_ecosim_fleet_results", params, timeout=TIMEOUT_LONG)


@mcp.tool()
async def export_ecosim_results(path: str) -> dict[str, Any]:
    """Export Ecosim model results to a file path on the EwE host.

    This tool may not be available in all EwE builds. If the C# plugin
    does not expose dumpEcosimModelResults, an error will be returned
    indicating the feature is unavailable. As an alternative, use
    get_ecosim_group_results or get_ecosim_fleet_results to extract
    individual result time-series.
    """
    _reject_path_traversal(path)
    if not path.lower().endswith(('.csv', '.txt')):
        raise ValueError("Path must have a .csv or .txt extension")
    return await _send("export_ecosim_results", {"path": path}, timeout=TIMEOUT_LONG)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def validate_model_readiness() -> dict[str, Any]:
    """Check model state before running Ecopath and return a readiness report.

    Inspects all groups for:
    - Whether all consumer groups have diet composition entries (list any missing)
    - Whether any groups have sentinel (-9999) values in critical fields
      (biomass, P/B, Q/B, EE)
    - Whether the model is loaded
    - Any other issues that would prevent Ecopath from running

    Returns a dict with:
    - ready: bool (True if all checks pass)
    - model_loaded: bool
    - issues: list of strings describing each problem found
    - consumer_groups_without_diet: list of group names/indices
    - groups_with_sentinel_values: list of group info
    - total_groups: int
    - consumer_count: int
    - _errors_if_any: optional error message
    """
    issues: list[str] = []
    consumer_groups_without_diet: list[str] = []
    groups_with_sentinel_values: list[dict[str, Any]] = []

    try:
        groups_result = await _send("list_ecopath_groups")
    except Exception as exc:
        return {
            "ready": False,
            "model_loaded": False,
            "issues": [f"Cannot read groups: {exc}"],
            "consumer_groups_without_diet": [],
            "groups_with_sentinel_values": [],
            "total_groups": 0,
            "consumer_count": 0,
        }

    groups = groups_result.get("groups", [])
    total_groups = groups_result.get("total_count", len(groups))

    if total_groups == 0:
        issues.append("No groups found — model may not be loaded.")

    consumer_count = 0
    for group in groups:
        if group.get("type") == "consumer":
            consumer_count += 1
            idx = group["index"]
            try:
                enriched = await _send("get_ecopath_group_enriched", {"index": idx})
            except Exception:
                issues.append(f"Cannot inspect group {group['name']} (idx {idx})")
                continue
            diet = enriched.get("diet", [])
            if not diet:
                label = f"{group['name']} (idx {idx})"
                consumer_groups_without_diet.append(label)
                issues.append(f"Consumer group {label} has no diet composition entries.")

        # Check for sentinel values in critical input fields
        sentinel_fields = {"biomass_area", "pb", "qb", "ee"}
        group_sentinels: list[str] = []
        inp = group.get("input", {})
        for field in sentinel_fields:
            val = inp.get(field)
            if val is not None and val == -9999:
                group_sentinels.append(field)
        if group_sentinels:
            groups_with_sentinel_values.append({
                "index": group["index"],
                "name": group["name"],
                "type": group.get("type", "unknown"),
                "sentinel_fields": group_sentinels,
            })
            issues.append(
                f"Group {group['name']} (idx {group['index']}) has sentinel "
                f"values (-9999) in: {', '.join(group_sentinels)}."
            )

    ready = len(issues) == 0
    return {
        "ready": ready,
        "model_loaded": total_groups > 0,
        "issues": issues,
        "consumer_groups_without_diet": consumer_groups_without_diet,
        "groups_with_sentinel_values": groups_with_sentinel_values,
        "total_groups": total_groups,
        "consumer_count": consumer_count,
    }


@mcp.tool()
async def run_ecopath() -> dict[str, Any]:
    """Run Ecopath mass balance. Returns whether the model balanced and the Ecopath run state.

    Validates that all consumer groups have diet composition entries before running.
    If any consumer groups have empty or incomplete diets, a clear ValueError is
    raised listing which groups need attention and how to fix them.
    """
    # Pre-run validation: check all consumer groups have diet composition
    groups_result = await _send("list_ecopath_groups")
    groups = groups_result.get("groups", [])
    missing_diet_groups: list[str] = []
    for group in groups:
        if group.get("type") == "consumer":
            idx = group["index"]
            enriched = await _send("get_ecopath_group_enriched", {"index": idx})
            diet = enriched.get("diet", [])
            if not diet:
                missing_diet_groups.append(f"{group['name']} (idx {idx})")
    if missing_diet_groups:
        raise ValueError(
            "Cannot run Ecopath: diet composition is missing or incomplete for these consumer groups: "
            + ", ".join(missing_diet_groups)
            + ". Each consumer group must have diet entries summing to 1.0. "
            "Use set_indexed_value(domain='ecopath_group', variable='diet_comp', "
            "index=<predator_idx>, indices=[<prey_idx>], value=<fraction>, navigate=False) "
            "to set each diet entry. For bulk setting, use batch_commands with set_values + diet_comp flat array."
        )
    return await _send("run_ecopath", timeout=TIMEOUT_LONG)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_ecopath_run_state() -> dict[str, Any]:
    """Get Ecopath run state: whether Ecopath has run, whether the model is balanced, and the run state."""
    return await _send("get_ecopath_run_state")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_ecopath_stats() -> dict[str, Any]:
    """Get Ecopath system-level statistics after an Ecopath run (throughput, connectance, etc.)."""
    result = await _send("get_ecopath_stats")
    result["status_code_map"] = STATUS_CODE_MAP
    return result


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_ecopath_group_diagnostics() -> dict[str, Any]:
    """Fetch balancing diagnostics (EE, PB, GE outputs and status flags, biomass, trophic level) for all Ecopath groups after a run."""
    result = await _send("get_ecopath_group_diagnostics")
    result["status_code_map"] = STATUS_CODE_MAP
    return result


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_ecopath_fleet_diagnostics() -> dict[str, Any]:
    """Fetch fleet-level Ecopath catch, landings, and discard breakdowns after an Ecopath run.

    Auto-enriched with fishing mortality (F = catch / biomass) for each fleet/group
    pair. Enrichment status reported in `_enrichment` field. Includes
    `status_code_map` for interpreting status flag values.
    """
    fleet_diag = await _send("get_ecopath_fleet_diagnostics_enriched")
    fleet_diag["status_code_map"] = STATUS_CODE_MAP
    return fleet_diag


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_mti_matrix() -> dict[str, Any]:
    """Get the Mixed Trophic Impact (MTI) matrix from Ecopath.

    Returns group names and the MTI matrix as structured JSON data, showing
    how each group impacts every other group trophically. Requires Ecopath to
    have been run first.

    The matrix[i][j] value represents the impact of group j on group i.
    A positive value indicates a beneficial impact, negative indicates a
    detrimental impact. Values near zero indicate negligible trophic
    interaction. If the Network Analysis plugin is not available, a zero
    matrix is returned with status "zero_fallback".
    """
    return await _send("get_mti_matrix")


@mcp.tool()
async def export_ecopath_results(path: str) -> dict[str, Any]:
    """Export Ecopath results (basic estimates CSV) to a file path on the EwE host.

    Requires Ecopath to have been run first. The path should be a writable path
    on the Windows machine running EwE.
    """
    _reject_path_traversal(path)
    if not path.lower().endswith(('.csv', '.txt')):
        raise ValueError("Path must have a .csv or .txt extension")
    return await _send("export_ecopath_results", {"path": path}, timeout=TIMEOUT_LONG)


@mcp.tool()
async def run_ecosim() -> dict[str, Any]:
    """Run the active Ecosim scenario."""
    return await _send("run_ecosim", timeout=TIMEOUT_LONG)


@mcp.tool()
async def stop_ecosim() -> dict[str, Any]:
    """Stop the active Ecosim run."""
    return await _send("stop_ecosim", timeout=TIMEOUT_LONG)


@mcp.tool()
async def refresh_ui() -> dict[str, Any]:
    """Try to trigger a refresh of the EwE GUI to show updated values."""
    return await _send("refresh_ui")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def capture_view(
    target: str = "auto",
    max_width: int | None = None,
    max_height: int | None = None,
) -> list[TextContent | ImageContent]:
    """Capture a screenshot of the current EwE view.

    NOTE: Prefer using data-reading tools (e.g., get_values, get_indexed_value) to extract tabular data.
    Only use this tool for capturing graphs, charts, and spatial visualizations.

    Args:
        target: What to capture — "auto" (content panel preferred), "main" (full window),
                "active" (active form), or "content" (content panel only).
        max_width: Optional max pixel width to downscale to (preserves aspect ratio).
        max_height: Optional max pixel height to downscale to (preserves aspect ratio).

    Returns:
        A 2-element list containing TextContent with metadata and ImageContent with the PNG.
    """
    target = target.lower()
    if target not in _VALID_CAPTURE_TARGETS:
        raise ValueError(f"target must be one of {sorted(_VALID_CAPTURE_TARGETS)}, got {target!r}")
    params: dict[str, Any] = {"target": target}
    if max_width is not None:
        params["max_width"] = max_width
    if max_height is not None:
        params["max_height"] = max_height

    result = await _send("capture_view", params, timeout=TIMEOUT_LONG)

    # Build metadata by excluding the base64 data
    meta = {k: v for k, v in result.items() if k != "data"}
    image_data = result.get("data")
    if not image_data:
        raise RuntimeError(f"Capture returned no image data. Response: {meta}")
    return [
        TextContent(type="text", text=str(meta)),
        ImageContent(
            type="image",
            data=image_data,
            mimeType="image/png",
        ),
    ]


@mcp.tool()
async def save_screenshot(
    path: str,
    target: str = "auto",
    max_width: int | None = None,
    max_height: int | None = None,
) -> dict[str, Any]:
    """Capture a screenshot and save it to a local file on the machine running the MCP server.

    NOTE: Prefer using data-reading tools (e.g., get_values, get_indexed_value) to extract tabular data.
    Only use this tool for capturing graphs, charts, and spatial visualizations.

    Args:
        path: File path where the PNG will be saved (local to the Python MCP process).
        target: What to capture — "auto", "main", "active", or "content".
        max_width: Optional max pixel width to downscale to.
        max_height: Optional max pixel height to downscale to.
    """
    _reject_path_traversal(path)
    if not path.lower().endswith('.png'):
        raise ValueError("path must end with .png extension")
    target = target.lower()
    if target not in _VALID_CAPTURE_TARGETS:
        raise ValueError(f"target must be one of {sorted(_VALID_CAPTURE_TARGETS)}, got {target!r}")

    params: dict[str, Any] = {"target": target}
    if max_width is not None:
        params["max_width"] = max_width
    if max_height is not None:
        params["max_height"] = max_height

    result = await _send("capture_view", params, timeout=TIMEOUT_LONG)

    # Decode base64 data and save it locally on the machine running the MCP server.
    # This allows the MCP client to easily access the file, regardless of where the EwE plugin is hosted.
    image_bytes = base64.b64decode(result["data"])
    await asyncio.to_thread(_save_screenshot_to_host, path, image_bytes)

    return {
        "saved": True,
        "path": path,
        "target": result.get("target", target),
        "width": result.get("width"),
        "height": result.get("height"),
        "bytes": len(image_bytes),
        "active_view": result.get("active_view"),
    }


def _save_screenshot_to_host(path: str, data: bytes) -> None:
    """Write screenshot bytes to a local file. Runs in a thread."""
    out = pathlib.Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def export_active_plot(target: str = "auto") -> dict[str, Any]:
    """Extract chart data from the active EwE plot as structured JSON.

    Finds the first ZedGraph chart control in the active view and extracts
    series names, X/Y data points, and axis labels. Returns structured data
    suitable for analysis. If no chart is found, suggests using capture_view
    for maps and other visuals.

    NOTE: Prefer this tool for extracting numerical data from charts and graphs.
    Use capture_view for visual screenshots when data extraction is not needed.

    Args:
        target: What to search for the chart — "auto" (content panel preferred),
                "main" (full window), "active" (active form), or "content" (content panel only).
    """
    params: dict[str, Any] = {"target": target}
    result = await _send("export_active_plot", params)
    return result


@mcp.tool(annotations=ToolAnnotations(destructiveHint=True))
async def set_values(
    domain: str, values: dict[str, Any], index: int | None = None, navigate: bool = True
) -> dict[str, Any]:
    """Set values for an EwE entity and refresh the UI. Pass navigate=False to avoid changing the visible EwE screen.

    For bulk updates, prefer set_values_by_name_batch or batch_commands with navigate=False.
    For setting diet compositions for multiple predators at once, use set_diet_matrix.
    For setting group parameters in bulk, also consider import_ecopath_groups_csv,
    import_ecosim_inputs_csv, and related CSV import tools.
    """
    params: dict[str, Any] = {"domain": domain, "values": values, "navigate": navigate}
    if index is not None:
        params["index"] = index
    result = await _send("set_values", params, timeout=TIMEOUT_LONG)
    # Automatically refresh UI after setting values
    await refresh_ui()
    return result


@mcp.tool(annotations=ToolAnnotations(destructiveHint=True))
async def set_indexed_value(
    domain: str,
    variable: str,
    indices: list[int],
    value: Any,
    index: int | None = None,
    navigate: bool = True,
) -> dict[str, Any]:
    """Set one indexed EwE value and refresh the UI. Pass navigate=False to avoid changing the visible EwE screen.

    For setting diet compositions for multiple predators at once, use set_diet_matrix instead.
    For bulk indexed updates, consider building a batch_commands payload with navigate=False.
    """
    params: dict[str, Any] = {
        "domain": domain,
        "variable": variable,
        "indices": indices,
        "value": value,
        "navigate": navigate,
    }
    if index is not None:
        params["index"] = index
    result = await _send("set_indexed_value", params, timeout=TIMEOUT_LONG)
    await refresh_ui()
    return result


@mcp.tool(annotations=ToolAnnotations(destructiveHint=True))
async def set_values_by_name_batch(
    operations: list[dict[str, Any]],
    options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve exact names to indices and apply mixed-domain set_values in one request.

    Each operation specifies a domain, name, and values. Names are resolved by
    exact match only. If no exact match is found, ranked fuzzy suggestions are
    returned so the agent can retry with the correct spelling.

    Use list_name_batch_capabilities first to discover supported domains,
    match fields, and writable fields.

    Pass navigate=True per operation only when you want to visually navigate
    the EwE GUI to the edited screen. Defaults to False for bulk workflows.

    Args:
        operations: List of operation dicts, each with domain, name, values,
            and optional match_field/navigate.
        options: Optional dict with dry_run, stop_on_error,
            include_suggestions, max_suggestions.
    """
    opts = options or {}
    dry_run = bool(opts.get("dry_run", False))
    stop_on_error = bool(opts.get("stop_on_error", False))
    include_suggestions = bool(opts.get("include_suggestions", True))
    max_suggestions = int(opts.get("max_suggestions", 5))

    summary = _empty_batch_summary()
    summary["total"] = len(operations)
    results: list[dict[str, Any]] = []
    entity_cache: dict[tuple[str, str], list[dict[str, Any]]] = {}

    for idx, op in enumerate(operations):
        domain = normalize_domain_name(str(op.get("domain", "")))
        name = str(op.get("name", ""))
        values = op.get("values")
        match_field = str(op.get("match_field", "name"))
        navigate = bool(op.get("navigate", False))

        item: dict[str, Any] = {
            "op_index": idx,
            "domain": domain,
            "name": name,
            "match_field": match_field,
        }

        # Validate domain
        if domain not in DOMAIN_CONFIG:
            item.update({
                "status": "set_failed",
                "error_code": "unknown_domain",
                "message": f"Unsupported domain for name batch: {domain}",
            })
            summary["failed"] += 1
            results.append(item)
            if stop_on_error:
                break
            continue

        # Validate match_field
        allowed_match_fields = DOMAIN_CONFIG[domain].get("match_fields", ["name"])
        if match_field not in allowed_match_fields:
            item.update({
                "status": "set_failed",
                "error_code": "invalid_match_field",
                "message": f"match_field '{match_field}' not supported for domain '{domain}'; allowed: {allowed_match_fields}",
            })
            summary["failed"] += 1
            results.append(item)
            if stop_on_error:
                break
            continue

        # Validate values
        if not isinstance(values, dict) or not values:
            item.update({
                "status": "set_failed",
                "error_code": "invalid_values",
                "message": "values must be a non-empty object",
            })
            summary["failed"] += 1
            results.append(item)
            if stop_on_error:
                break
            continue

        # Ensure writable fields cache is populated (dynamic discovery first)
        if _writable_fields_cache is None:
            await _discover_dynamic_writable_fields()

        # Validate fields against dynamically discovered metadata with
        # STATIC_WRITABLE_FIELDS as fallback when dynamic is unavailable.
        allowed = _get_writable_fields(domain)
        using_dynamic = _writable_fields_cache is not None and domain in (_writable_fields_cache or {})

        unknown = sorted([k for k in values if k not in allowed])
        if unknown:
            if using_dynamic:
                # Dynamic discovery confirmed these fields are not writable
                item.update({
                    "status": "validation_failed",
                    "error_code": "unknown_field",
                    "message": "One or more fields are not writable in this domain",
                    "details": {"unknown_fields": unknown},
                })
                summary["failed"] += 1
                summary["validation_failed"] += 1
                results.append(item)
                if stop_on_error:
                    break
                continue
            else:
                # Static fallback may be incomplete; warn but do not reject
                item.setdefault("_warnings", []).append(
                    f"Fields not listed in static writable set; "
                    f"will attempt set_values with validate_values: {unknown}"
                )

        # Fetch all entities for name resolution (cached per domain+match_field, paged)
        cache_key = (domain, match_field)
        if cache_key not in entity_cache:
            entities: list[dict[str, Any]] = []
            start_index = 1
            while True:
                range_result = await get_values_range(
                    domain=domain,
                    variables=[match_field],
                    start_index=start_index,
                    max_entities=500,
                    navigate=False,
                )
                page = range_result.get("entities", [])
                entities.extend(page)
                if not range_result.get("truncated"):
                    break
                start_index = (page[-1]["index"] + 1) if page else (start_index + 500)
            entity_cache[cache_key] = entities
        entities = entity_cache[cache_key]
        resolved = resolve_exact_name(entities, name, match_field=match_field)

        # Handle not_found
        if resolved["resolution_status"] == "not_found":
            item.update({
                "resolution_status": "not_found",
                "status": "not_found",
                "error_code": "not_found",
                "message": "No exact name match found",
            })
            if include_suggestions:
                item["suggestions"] = rank_name_suggestions(
                    name,
                    entities,
                    max_suggestions=max_suggestions,
                    match_field=match_field,
                )
            summary["failed"] += 1
            summary["not_found"] += 1
            results.append(item)
            if stop_on_error:
                break
            continue

        # Handle ambiguous
        if resolved["resolution_status"] == "ambiguous":
            item.update({
                "resolution_status": "ambiguous",
                "status": "ambiguous",
                "error_code": "ambiguous_name",
                "message": "Multiple exact matches found",
                "candidates": resolved["candidates"],
            })
            summary["failed"] += 1
            summary["ambiguous"] += 1
            results.append(item)
            if stop_on_error:
                break
            continue

        # Exact match — proceed with validation
        resolved_index = int(resolved["resolved_index"])
        item["resolved_index"] = resolved_index
        item["resolution_status"] = "exact"

        validation = await validate_values(domain=domain, index=resolved_index, values=values)
        invalid = [
            k
            for k, v in (validation.get("results") or {}).items()
            if isinstance(v, dict) and v.get("valid") is False
        ]
        if invalid:
            item.update({
                "status": "validation_failed",
                "error_code": "validation_failed",
                "message": "Validation failed for one or more fields",
                "validation": validation,
            })
            summary["failed"] += 1
            summary["validation_failed"] += 1
            results.append(item)
            if stop_on_error:
                break
            continue

        # Dry run — report without mutating
        if dry_run:
            item.update({"status": "applied", "dry_run": True, "validation": validation})
            summary["applied"] += 1
            results.append(item)
            continue

        # Apply mutation
        set_result = await set_values(
            domain=domain,
            index=resolved_index,
            values=values,
            navigate=navigate,
        )
        item.update({"status": "applied", "dry_run": False, "set_result": set_result})
        summary["applied"] += 1
        results.append(item)

    return {"status": _final_batch_status(summary), "summary": summary, "results": results}


@mcp.tool(
    description="Execute multiple EwE MCP commands in one round-trip. "
    "Blocked commands include eval, model load/save/close, simulation runs/stops, view capture, and scenario mutations. "
    "set_values/set_indexed_value/set_values_range allowed only with navigate=False.",
)
async def batch_commands(commands: list[dict[str, Any]]) -> dict[str, Any]:
    """Execute multiple non-blocked commands through the EwE plugin.

    Blocked commands include eval, model lifecycle, simulation runs/stops,
    view capture, scenario mutations, and other destructive operations.
    set_values / set_indexed_value / set_values_range are allowed in batch
    ONLY when navigate=False is explicitly set for every command.
    """
    # Use cached value or hardcoded fallback (non-blocking).
    blocked = get_batch_blocked_commands()

    _NAVIGATE_GATED_COMMANDS = frozenset({"set_values", "set_indexed_value", "set_values_range"})

    normalized_commands = []
    for command in commands:
        command_type = command.get("command_type") if "command_type" in command else command.get("type")
        if command_type is None:
            raise ValueError("Each batch command must have a 'type' field")
        if command_type in blocked:
            # Allow set_values/set_indexed_value/set_values_range when navigate=False
            if command_type in _NAVIGATE_GATED_COMMANDS:
                params = command.get("params", {})
                if not params.get("navigate", True):
                    # Explicit navigate=False — allow it
                    normalized_commands.append({"type": command_type, "params": params})
                    continue
            raise ValueError(f"Command {command_type!r} is not allowed in batch")
        normalized_commands.append({"type": command_type, "params": command.get("params", {})})
    result = await _send("batch", {"commands": normalized_commands}, timeout=TIMEOUT_LONG)
    # Normalize domain names in batch results
    if isinstance(result, dict):
        for entry in result.get("results", []):
            if isinstance(entry, dict) and "domain" in entry:
                entry["domain"] = normalize_domain_name(entry["domain"])
    return result


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def call_command(
    command_type: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute any EwE plugin command directly by name.

    Use this for simple read-only operations that don't need Python-side
    enrichment or safety checks. For operations that combine multiple
    plugin calls (e.g., get_ecopath_group, which enriches with diagnostics),
    use the dedicated tool instead.

    Args:
        command_type: The C# plugin command name (e.g., "list_ecopath_groups",
                      "get_ecosim_group_input", "get_ecospace_run_state").
        params: Optional dict of parameters for the command.

    Blocked: model lifecycle, simulation runs/stops, destructive mutations,
             eval, view capture, exports. Use dedicated tools for those.
    """
    if not command_type:
        raise ValueError("command_type is required")
    blocked = get_batch_blocked_commands()
    # set_values / set_indexed_value / set_values_range are allowed in
    # batch_commands with navigate=False, but never in call_command which
    # bypasses the navigate gate. Block them unconditionally here.
    blocked_direct = blocked | frozenset({"set_values", "set_indexed_value", "set_values_range"})
    if command_type in blocked_direct:
        raise ValueError(
            f"Command {command_type!r} is blocked from direct execution. "
            "Use the dedicated MCP tool for this operation."
        )
    return await _send(command_type, params)


@mcp.tool(annotations=ToolAnnotations(destructiveHint=True))
async def execute_ewe_code(code: str) -> dict[str, Any]:
    """Gated escape hatch for in-process EwE Core reflection.

    Disabled by default. Requires EWE_MCP_ENABLE_EVAL=1 on the EwE plugin side.
    Blocked from batch_commands. Expressions matching dangerous patterns
    (System.IO, Process, File operations, network access) are denied even when enabled.
    Only simple Core.Property reads, Core.Property = Value sets, and Core.Method() calls are supported.
    """
    return await _send("execute_ewe_code", {"code": code}, timeout=TIMEOUT_LONG)


@mcp.tool(annotations=ToolAnnotations(destructiveHint=True))
async def shutdown_server() -> dict[str, Any]:
    """Request the MCP plugin server to shut down.

    Only available when EWE_MCP_ENABLE_EVAL=1 is set on the EwE side.
    Blocked from batch_commands.
    """
    return await _send("shutdown_server", timeout=5)


@mcp.resource("ewe://state")
async def state_resource() -> str:
    return str(await _send("get_runtime_state"))


@mcp.resource("ewe://commands")
async def commands_resource() -> str:
    return str(await _send("list_ui_commands"))


@mcp.resource("ewe://entities/ecopath_groups")
async def ecopath_groups_resource() -> str:
    return str(await _send("list_ecopath_groups"))


@mcp.resource("ewe://entities/ecopath_fleets")
async def ecopath_fleets_resource() -> str:
    return str(await _send("list_ecopath_fleets"))


_AGENT_INSTRUCTIONS = (
    "You are connected to an EwE MCP server. This server bridges an AI agent "
    "to a live Ecopath with Ecosim (EwE) desktop process for ecosystem modeling.\n\n"
    "## Connection Model\n"
    "The MCP protocol requires a persistent server session. The ewe-mcp.exe server "
    "process must remain running for the duration of the workflow. Do not restart "
    "it between commands.\n\n"
    "## Key Tools\n"
    "- **Model lifecycle**: load_model, save_model, save_model_as, close_model, "
    "get_model_info, has_changes, discard_changes\n"
    "- **Ecopath**: run_ecopath, get_ecopath_run_state, get_ecopath_stats, "
    "get_ecopath_group_diagnostics, get_ecopath_fleet_diagnostics, "
    "list_ecopath_groups, get_ecopath_group, list_ecopath_fleets, get_ecopath_fleet\n"
    "- **Ecosim**: list_ecosim_scenarios, load_ecosim_scenario, run_ecosim, "
    "wait_for_ecosim_completion, get_ecosim_group_results, get_ecosim_fleet_results\n"
    "- **Ecospace**: list_ecospace_scenarios, load_ecospace_scenario, run_ecospace, "
    "get_ecospace_group_results, get_ecospace_fleet_results\n"
    "- **Value access**: get_values, set_values, get_indexed_value, "
    "set_indexed_value, validate_values (pass navigate=False for background access)\n"
    "- **Model readiness**: validate_model_readiness (pre-flight check before run_ecopath)\n"
    "- **Bulk Operations**:\n"
    "  - batch_commands: execute multiple read-only or value-set operations (with \n"
    "  navigate=False) in one round-trip. Blocked commands include model lifecycle, \n"
    "  simulation runs/stops, exports, view captures, and scenario mutations.\n"
    "  - set_values_by_name_batch: resolve exact entity names to indices and apply \n"
    "  mixed-domain set_values in one request. Use list_name_batch_capabilities first \n"
    "  to discover supported domains and writable fields.\n"
    "  - create_ecopath_groups: batch-create Ecopath groups. Use ~14 groups per call\n"
    "  with auto_reload=False for speed, then one save_model + load_model at the end.\n"
    "  - create_ecopath_fleets: batch-create Ecopath fleets. Same pattern as groups.\n"
    "  - set_diet_matrix: set diet compositions for multiple predator groups in one \n"
    "  call using structured predator->{prey: fraction} format. Resolves names to \n"
    "  indices and sends a single batch internally.\n"
    "  - set_values with diet_comp flat array: set diet compositions inside \n"
    "  batch_commands by passing diet_comp as a flat array of prey fractions.\n"
    "  - navigate=False must be set on every batched mutation to avoid UI flicker.\n"
    "- **CSV Import/Export**:\n"
    "  - export/import_ecopath_groups_csv: bulk group params (Biomass, PB, QB, EE)\n"
    "  - export/import_diet_csv: bulk diet composition matrix (predator x prey)\n"
    "  - export/import_ecosim_vul_csv: Ecosim vulnerability multiplier matrix\n"
    "  - export/import_ecosim_inputs_csv: Ecosim scalar group parameters\n"
    "  - export/import_fleet_landings_csv: fleet landings and discards\n"
    "  - export/import_fleet_indexed_csv: fleet x group indexed values\n"
    "  - export/import_ecotracer_inputs_csv: Ecotracer group inputs\n"
    "  - export/import_monte_carlo_csv: Monte Carlo CV parameters\n"
    "  - export/import_ecospace_inputs_csv: Ecospace group inputs\n"
    "  - export/import_ecospace_fleet_inputs_csv: Ecospace fleet parameters\n"
    "  - export/import_detritus_fate_csv: per-group detritus allocation\n"
    "  Prefer CSV import for loading bulk data: export current state, edit in\n"
    "  spreadsheet, import back. One round-trip per domain instead of N calls.\n"
    "  When building new models, use import_ecopath_groups_csv with CSV data\n"
    "  instead of many individual set_values calls. CSV is faster and atomic.\n"
    "- **Diagnostics**: ping, diagnose, get_ewe_info, get_runtime_state\n\n"
    "## 🚨 Dialog Events (CRITICAL)\n"
    "**Every command response may contain a `_dialog_events` key with pending EwE "
    "modal dialog information. ALWAYS check this key after any command that times out "
    "or returns an unexpected result. Dialogs that block the GUI will prevent ALL "
    "subsequent commands from working.**\n"
    "Use get_dialog_events() to inspect and clear pending dialogs. If you see "
    "unexpected timeouts or failures, check for `_dialog_events` first.\n\n"
    "## Safety Rules\n"
    "- execute_ewe_code and shutdown_server are disabled by default\n"
    "- Do not batch model lifecycle, simulation run/stop, export/capture, "
    "or scenario mutation commands\n"
    "- Use validate_values before set_values\n"
    "- Prefer save_model_as to a copy path when mutating models\n\n"
    "## Efficient Model Creation Pattern\n"
    "When building a new model from scratch, use this batching pattern to avoid timeouts:\n"
    "1. create_model to create the blank model\n"
    "2. create_ecopath_groups(groups=[14], auto_reload=False) — first batch\n"
    "3. create_ecopath_groups(groups=[13], auto_reload=False) — second batch\n"
    "4. set_values to fix Detritus params (biomass_area_input=50, eeinput=0.4)\n"
    "5. save_model + load_model — one refresh after all groups created\n"
    "6. set_diet_matrix(diets={...}) — all 25+ predator diets in one call\n"
    "7. create_ecopath_fleets(names=[...], auto_reload=False) — all fleets at once\n"
    "8. save_model + load_model — refresh fleet counts\n"
    "9. validate_model_readiness() — confirm all diets entered, no sentinels\n"
    "10. run_ecopath() — balance the model\n\n"
    "For parameter loading after groups exist, use CSV import tools:\n"
    "- import_ecopath_groups_csv for bulk group params (Biomass, PB, QB, EE)\n"
    "- import_diet_csv for bulk diet composition matrix\n"
    "- import_ecosim_inputs_csv for Ecosim group parameters\n"
    "These use set_values_by_name_batch internally — one round-trip per domain.\n\n"
    "## First Steps\n"
    "1. Call ping to verify connectivity\n"
    "2. Call diagnose to check server configuration\n"
    "3. Call load_model with the path to an EwE model file\n"
    "4. Call list_ecopath_groups to explore the model structure\n"
    "5. Call validate_model_readiness before run_ecopath to check for missing diets or sentinel values\n"
    "6. Call run_ecopath to balance the model\n"
    "7. Proceed to Ecosim or Ecospace scenarios for dynamic simulations"
)


@mcp.resource("ewe://agent_instructions")
async def agent_instructions_resource() -> str:
    """How to use this EwE MCP server: persistent session, key tools, safety rules, and first steps."""
    return _AGENT_INSTRUCTIONS


# ── Prompt templates ──────────────────────────────────────────────


@mcp.prompt()
async def ewe_setup_guide() -> list[dict[str, str]]:
    """Onboarding guide: how to connect to and use this EwE MCP server."""
    return [{"role": "user", "content": _AGENT_INSTRUCTIONS}]


@mcp.prompt()
async def balance_model(path: str) -> list[dict[str, str]]:
    """Guide through loading an EwE model, running Ecopath, and checking balancing diagnostics."""
    return [
        {
            "role": "user",
            "content": (
                f"Load the EwE model at {path!r}, run Ecopath, and report the balancing "
                "diagnostics. Steps:\n"
                "1. Call load_model with this path\n"
                "2. Call run_ecopath to balance the model\n"
                "3. Call get_ecopath_run_state to check the result\n"
                "4. Call get_ecopath_stats for overall statistics\n"
                "5. Call get_ecopath_group_diagnostics to see which groups have issues\n"
                "Be thorough: report which groups are unbalanced and what their EE values are."
            ),
        }
    ]


@mcp.prompt()
async def run_ecosim_scenario(
    path: str, scenario_index: int = 1
) -> list[dict[str, str]]:
    """Guide through loading a model and running an Ecosim scenario."""
    return [
        {
            "role": "user",
            "content": (
                f"Run Ecosim scenario {scenario_index} from the model at {path!r}. Steps:\n"
                "1. Call load_model with this path\n"
                "2. Call list_ecosim_scenarios to see available scenarios\n"
                f"3. Call load_ecosim_scenario with index {scenario_index}\n"
                "4. Call run_ecosim to start the simulation\n"
                "5. Call wait_for_ecosim_completion to wait for it to finish\n"
                "6. Call get_ecosim_group_results to fetch biomass time series\n"
                "Report the time-series results."
            ),
        }
    ]


@mcp.prompt()
async def compare_scenarios(path: str, indices: str = "1,2") -> list[dict[str, str]]:
    """Guide through running and comparing multiple Ecosim scenarios."""
    return [
        {
            "role": "user",
            "content": (
                f"Compare Ecosim scenarios [{indices}] from the model at {path!r}. Steps:\n"
                "1. Call load_model with this path\n"
                "2. Call list_ecosim_scenarios to see available scenarios\n"
                f"3. For each scenario index in [{indices}]:\n"
                "   a. Call load_ecosim_scenario with that index\n"
                "   b. Call run_ecosim\n"
                "   c. Call wait_for_ecosim_completion\n"
                "   d. Call get_ecosim_group_results to collect biomass over time\n"
                "4. Compare the biomass trajectories across scenarios, highlighting differences.\n"
                "Use navigate=False for all value reads to avoid UI flicker during batch operations."
            ),
        }
    ]


@mcp.prompt()
async def export_report(path: str) -> list[dict[str, str]]:
    """Guide through exporting Ecopath and Ecosim results for reporting."""
    return [
        {
            "role": "user",
            "content": (
                f"Export EwE model results from {path!r}. Steps:\n"
                "1. Call load_model with this path\n"
                "2. Call get_model_info to get the model name and metadata\n"
                "3. Call get_ecopath_stats for summary statistics\n"
                "4. Call get_ecopath_group_diagnostics for group-level detail\n"
                "5. Call get_ecopath_fleet_diagnostics for fleet-level detail\n"
                "6. Call list_ecosim_scenarios to show available scenarios\n"
                "7. Call export_ecopath_results to export CSV data\n"
                "Compile the results into a readable summary."
            ),
        }
    ]


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    logger.info("EwE MCP server starting")
    # Pre-cache batch blocked commands from the C# plugin
    try:
        client = _get_connection()
        if cache_batch_blocked_commands(client):
            logger.info("Batch blocked commands cached from plugin (%d commands)", len(get_batch_blocked_commands()))
    except Exception:
        logger.info("Using hardcoded fallback for batch blocked commands (plugin not available)")
    mcp.run()


if __name__ == "__main__":
    main()
