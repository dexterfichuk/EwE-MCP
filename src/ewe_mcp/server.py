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
            return result
        except Exception as exc:
            hint = _error_hint_for_exception(exc)
            logger.warning("ERR: %s — %s", command_type, exc)
            if hint:
                raise RuntimeError(f"{exc}\n\nHINT: {hint}") from exc
            raise


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


@mcp.tool()
async def save_model(path: str | None = None) -> dict[str, Any]:
    """Save the current model, optionally to a new path."""
    if path is not None:
        _reject_path_traversal(path)
    params = {"path": path} if path else {}
    return await _send("save_model", params, timeout=TIMEOUT_LONG)


@mcp.tool()
async def save_model_as(path: str) -> dict[str, Any]:
    """Save the current model to a new path."""
    _reject_path_traversal(path)
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
async def create_ecopath_fleet(name: str) -> dict[str, Any]:
    """Create a new Ecopath fishing fleet.

    Note: The new fleet is written to the model database but in-memory core
    counts are not immediately refreshed. The fleet may not appear in
    list_ecopath_fleets until the model is saved and reloaded. Use
    save_model followed by load_model to see the new fleet.
    """
    return await _send("create_ecopath_fleet", {"name": name})


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
    """Create and load a new Ecosim scenario."""
    return await _send(
        "create_ecosim_scenario",
        {
            "name": name,
            "description": description,
            "author": author,
            "contact": contact,
        },
        timeout=TIMEOUT_LONG,
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


@mcp.tool()
async def run_ecopath() -> dict[str, Any]:
    """Run Ecopath mass balance. Returns whether the model balanced and the Ecopath run state."""
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
    """Set values for an EwE entity and refresh the UI. Pass navigate=False to avoid changing the visible EwE screen."""
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
    """Set one indexed EwE value and refresh the UI. Pass navigate=False to avoid changing the visible EwE screen."""
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

    normalized_commands = []
    for command in commands:
        command_type = command.get("command_type") if "command_type" in command else command.get("type")
        if command_type is None:
            raise ValueError("Each batch command must have a 'type' field")
        if command_type in blocked:
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
    "- **Diagnostics**: ping, diagnose, get_ewe_info, get_runtime_state\n\n"
    "## Safety Rules\n"
    "- execute_ewe_code and shutdown_server are disabled by default\n"
    "- Do not batch model lifecycle, simulation run/stop, export/capture, "
    "or scenario mutation commands\n"
    "- Use validate_values before set_values\n"
    "- Prefer save_model_as to a copy path when mutating models\n\n"
    "## First Steps\n"
    "1. Call ping to verify connectivity\n"
    "2. Call diagnose to check server configuration\n"
    "3. Call load_model with the path to an EwE model file\n"
    "4. Call list_ecopath_groups to explore the model structure\n"
    "5. Call run_ecopath to balance the model\n"
    "6. Proceed to Ecosim or Ecospace scenarios for dynamic simulations"
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
