"""FastMCP server exposing a running EwE instance."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any

import base64
import pathlib

from mcp.server.fastmcp import FastMCP
from mcp.types import ImageContent, TextContent, ToolAnnotations

from ewe_mcp.client import EwEMCPClient, ResponseTooLargeError
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
    BATCH_BLOCKED_COMMANDS,
    STATUS_CODE_MAP,
    TIMEOUT_DEFAULT,
    TIMEOUT_LONG,
    enrich_diagnose,
    error_hint,
    filter_sentinels,
    get_host,
    get_port,
    get_token,
    normalize_domain_name,
    should_filter_sentinels,
    validate_ewe_ranges,
)

logger = logging.getLogger("ewe_mcp")
mcp = FastMCP("EwE MCP")

_connection: EwEMCPClient | None = None
_validated_at = 0.0
_connection_ttl = 5.0


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
            assert _connection.socket is not None
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
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            return _get_connection().command_result(
                command_type, params, timeout=timeout
            )
        except (OSError, ConnectionError, ResponseTooLargeError) as exc:
            last_error = exc
            _invalidate_connection()
            if attempt < 2:
                time.sleep(0.5 * (attempt + 1))
    if last_error is not None:
        raise last_error
    raise RuntimeError("EwE MCP command failed")


async def _send(
    command_type: str,
    params: dict[str, Any] | None = None,
    timeout: int = TIMEOUT_DEFAULT,
):
    try:
        result = await asyncio.to_thread(_send_sync, command_type, params, timeout)
        if should_filter_sentinels():
            result = filter_sentinels(result)
        return result
    except Exception as exc:
        hint = error_hint(str(exc))
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
async def query_fishbase(genus: str, species: str, tables: list[str] | None = None) -> dict[str, Any]:
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
async def query_sealifebase(genus: str, species: str, tables: list[str] | None = None) -> dict[str, Any]:
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
async def query_globi(taxon_name: str, interaction_type: str = "preysOn") -> dict[str, Any]:
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
async def invoke_ui_command(name: str, tag: str | None = None) -> dict[str, Any]:
    """Invoke a named EwE UI command. Optional tag is passed as cCommand.Tag."""
    params: dict[str, Any] = {"name": name}
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
    """Load an EwE model file inside the running EwE instance."""
    return await _send("load_model", {"path": path}, timeout=TIMEOUT_LONG)


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
    params = {"path": path} if path else {}
    return await _send("save_model", params, timeout=TIMEOUT_LONG)


@mcp.tool()
async def save_model_as(path: str) -> dict[str, Any]:
    """Save the current model to a new path."""
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
async def get_entity(domain: str, index: int | None = None) -> dict[str, Any]:
    """Get one entity from a supported EwE domain."""
    params: dict[str, Any] = {"domain": domain}
    if index is not None:
        params["index"] = index
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
    """Read values from an EwE entity. Pass navigate=False to avoid changing the visible EwE screen."""
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
async def list_ecopath_groups() -> dict[str, Any]:
    """List Ecopath functional groups with key read-only input/output values."""
    return await _send("list_ecopath_groups")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_ecopath_group(index: int) -> dict[str, Any]:
    """Get one Ecopath functional group, including non-zero diet entries."""
    group = await _send("get_ecopath_group", {"index": index})
    run_state = await _send("get_ecopath_run_state")
    if run_state.get("has_ecopath_ran") and "output" in group:
        try:
            diag = await _send("get_ecopath_group_diagnostics")
            diag_group = next(
                (g for g in diag.get("groups", []) if g["index"] == index), None
            )
            if diag_group:
                values = diag_group.get("values", {})
                enriched_output = dict(group["output"])
                if "EEOutput" in values:
                    enriched_output["ee"] = values["EEOutput"]
                if "TTLX" in values:
                    enriched_output["trophic_level"] = values["TTLX"]
                if "PBOutput" in values:
                    enriched_output["pb_computed"] = values["PBOutput"]
                    if "GEOutput" in values and values["GEOutput"] is not None:
                        enriched_output["ge"] = values["GEOutput"]
                group["output"] = enriched_output
        except Exception:
            logger.warning(
                "Failed to enrich ecopath group %d with diagnostics",
                index,
                exc_info=True,
            )
    return group


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def list_ecopath_fleets() -> dict[str, Any]:
    """List Ecopath fishing fleets."""
    return await _send("list_ecopath_fleets")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_ecopath_fleet(index: int) -> dict[str, Any]:
    """Get one Ecopath fleet, including non-zero landings/discards by group."""
    return await _send("get_ecopath_fleet", {"index": index})


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
    index: int,
    variables: list[str] | None = None,
    start_time: int | None = None,
    end_time: int | None = None,
) -> dict[str, Any]:
    """Read Ecospace group result time-series after a run."""
    params: dict[str, Any] = {"index": index}
    if variables is not None:
        params["variables"] = variables
    if start_time is not None:
        params["start_time"] = start_time
    if end_time is not None:
        params["end_time"] = end_time
    return await _send("get_ecospace_group_results", params, timeout=TIMEOUT_LONG)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_ecospace_fleet_results(
    index: int,
    variables: list[str] | None = None,
    start_time: int | None = None,
    end_time: int | None = None,
) -> dict[str, Any]:
    """Read Ecospace fleet result time-series after a run."""
    params: dict[str, Any] = {"index": index}
    if variables is not None:
        params["variables"] = variables
    if start_time is not None:
        params["start_time"] = start_time
    if end_time is not None:
        params["end_time"] = end_time
    return await _send("get_ecospace_fleet_results", params, timeout=TIMEOUT_LONG)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def list_ecospace_habitats() -> dict[str, Any]:
    """List Ecospace habitats in the active scenario."""
    return await _send("list_ecospace_habitats")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_ecospace_habitat(index: int) -> dict[str, Any]:
    """Get one Ecospace habitat by one-based index."""
    return await _send("get_ecospace_habitat", {"index": index})


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
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    last_state: dict[str, Any] = {}
    while asyncio.get_running_loop().time() < deadline:
        last_state = await _send("get_ecospace_run_state")
        if not last_state.get("is_ecospace_running") and last_state.get(
            "has_ecospace_loaded"
        ):
            return last_state
        await asyncio.sleep(poll_interval_seconds)
    raise RuntimeError(
        f"Ecospace did not complete within {timeout_seconds}s. Last state: {last_state}"
    )


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
    index: int,
    variables: list[str] | None = None,
    start_time: int | None = None,
    end_time: int | None = None,
) -> dict[str, Any]:
    """Read Ecotracer group result time-series after a run.

    Defaults to at most 120 timesteps (start_time..start_time+119) when
    end_time is omitted, matching Ecosim/Ecospace result conventions.
    """
    params: dict[str, Any] = {"index": index}
    if variables is not None:
        params["variables"] = variables
    if start_time is not None:
        params["start_time"] = start_time
    if end_time is not None:
        params["end_time"] = end_time
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


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
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
async def copy_ecosim_scenario(name: str, description: str = "") -> dict[str, Any]:
    """Copy the active Ecosim scenario under a new name and load the copy."""
    return await _send(
        "copy_ecosim_scenario",
        {"name": name, "description": description},
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
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    last_state: dict[str, Any] = {}
    while asyncio.get_running_loop().time() < deadline:
        last_state = await _send("get_ecosim_run_state")
        if last_state.get("has_ecosim_ran"):
            return last_state
        await asyncio.sleep(poll_interval_seconds)
    raise RuntimeError(
        f"Ecosim did not complete within {timeout_seconds}s. Last state: {last_state}"
    )


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def list_ecosim_group_inputs() -> dict[str, Any]:
    """List Ecosim group input parameters for the active scenario."""
    return await _send("list_ecosim_group_inputs")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_ecosim_group_input(index: int) -> dict[str, Any]:
    """Get one Ecosim group input parameter record."""
    return await _send("get_ecosim_group_input", {"index": index})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def list_ecosim_fleet_inputs() -> dict[str, Any]:
    """List Ecosim fleet input parameters for the active scenario."""
    return await _send("list_ecosim_fleet_inputs")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_ecosim_fleet_input(index: int) -> dict[str, Any]:
    """Get one Ecosim fleet input parameter record."""
    return await _send("get_ecosim_fleet_input", {"index": index})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_ecosim_group_results(
    index: int,
    variables: list[str] | None = None,
    start_time: int | None = None,
    end_time: int | None = None,
    fleet_index: int | None = None,
) -> dict[str, Any]:
    """Read Ecosim group result time-series after a run."""
    params: dict[str, Any] = {"index": index}
    if variables is not None:
        params["variables"] = variables
    if start_time is not None:
        params["start_time"] = start_time
    if end_time is not None:
        params["end_time"] = end_time
    if fleet_index is not None:
        params["fleet_index"] = fleet_index
    return await _send("get_ecosim_group_results", params, timeout=TIMEOUT_LONG)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_ecosim_fleet_results(
    index: int,
    variables: list[str] | None = None,
    start_time: int | None = None,
    end_time: int | None = None,
) -> dict[str, Any]:
    """Read Ecosim fleet result time-series after a run."""
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
    """Export Ecosim model results to a file path on the EwE host."""
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
    """Fetch fleet-level Ecopath catch, landings, and discard breakdowns after an Ecopath run."""
    return await _send("get_ecopath_fleet_diagnostics")


@mcp.tool()
async def export_ecopath_results(path: str) -> dict[str, Any]:
    """Export Ecopath results (basic estimates CSV) to a file path on the EwE host.

    Requires Ecopath to have been run first. The path should be a writable path
    on the Windows machine running EwE.
    """
    return await _send("export_ecopath_results", {"path": path}, timeout=TIMEOUT_LONG)


@mcp.tool()
async def run_ecosim() -> dict[str, Any]:
    """Run the active Ecosim scenario."""
    return await _send("run_ecosim", timeout=TIMEOUT_LONG)


@mcp.tool()
async def stop_ecosim() -> dict[str, Any]:
    """Stop the active Ecosim run."""
    return await _send("stop_ecosim")


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
    params: dict[str, Any] = {"target": target}
    if max_width is not None:
        params["max_width"] = max_width
    if max_height is not None:
        params["max_height"] = max_height

    result = await _send("capture_view", params, timeout=TIMEOUT_LONG)

    image_data = base64.b64decode(result["data"])

    # Build metadata summary (everything except the raw data).
    meta = {key: value for key, value in result.items() if key != "data"}

    return [
        TextContent(type="text", text=str(meta)),
        ImageContent(
            type="image",
            data=result["data"],
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
    if ".." in pathlib.Path(path).parts:
        raise ValueError("Path cannot contain '..' components for security.")

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


@mcp.tool(
    description="Execute multiple EwE MCP commands in one round-trip. "
    "Blocked commands include eval, model load/save/close, simulation runs/stops, view capture, and scenario mutations/loads.",
)
async def batch_commands(commands: list[dict[str, Any]]) -> dict[str, Any]:
    """Execute multiple non-blocked commands through the EwE plugin."""
    for command in commands:
        command_type = command.get("type")
        if command_type in BATCH_BLOCKED_COMMANDS:
            raise ValueError(f"Command {command_type!r} is not allowed in batch")
    return await _send("batch", {"commands": commands}, timeout=TIMEOUT_LONG)


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


# ── Prompt templates ──────────────────────────────────────────────


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
    mcp.run()


if __name__ == "__main__":
    main()
