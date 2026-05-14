"""GLOBI client for predator-prey interaction data via REST API."""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx
from httpx import HTTPStatusError, ConnectError, TimeoutException

from ewe_mcp.data_sources._shared import _TAXON_PATTERN, _VALID_GLOBI_INTERACTION_TYPES

logger = logging.getLogger("ewe_mcp")


def _validate_taxon_name(name: str, param_name: str) -> None:
    if not name or not name.strip():
        raise ValueError(f"{param_name} must be a non-empty string")
    if not _TAXON_PATTERN.match(name.strip()):
        raise ValueError(f"{param_name} contains invalid characters: {name!r}")

GLOBI_BASE_URL = "https://api.globalbioticinteractions.org"
MAX_INTERACTIONS = 100
HTTP_TIMEOUT = 15.0

EXTRACT_COLUMNS = [
    "source_taxon_name",
    "interaction_type",
    "target_taxon_name",
    "latitude",
    "longitude",
    "study_title",
]


def _parse_response(data: dict[str, Any]) -> list[dict[str, Any]]:
    columns = data.get("columns", [])
    rows = data.get("data", [])
    indices = {col: i for i, col in enumerate(columns) if col in EXTRACT_COLUMNS}
    if not indices:
        logger.warning("GLOBI response missing expected columns: %s", EXTRACT_COLUMNS)
        return []
    interactions = []
    for row in rows:
        entry = {}
        for col, idx in indices.items():
            if idx < len(row):
                entry[col] = row[idx]
        target = entry.get("target_taxon_name")
        if target:
            interactions.append({
                "target": target,
                "study": entry.get("study_title"),
                "latitude": entry.get("latitude"),
                "longitude": entry.get("longitude"),
            })
    return interactions


def query_globi(
    taxon_name: str,
    interaction_type: str = "preysOn",
) -> dict[str, Any]:
    """Query GLOBI for species interaction data (predator-prey relationships).

    Searches the Global Biotic Interactions database for trophic and other
    biotic interactions involving the given taxon.

    Args:
        taxon_name: Full scientific name (e.g., "Gadus morhua").
        interaction_type: Type of interaction. Common values: "preysOn",
            "eatenBy", "parasiteOf", "hostOf". Defaults to "preysOn".

    Returns:
        Dict with source, interaction_type, interactions list, count, and
        optional note if results were capped. Returns {"error": "..."} on failure.
    """
    _validate_taxon_name(taxon_name, "taxon_name")
    if not interaction_type or not re.match(r"^[A-Za-z]+$", interaction_type):
        raise ValueError(f"interaction_type contains invalid characters: {interaction_type!r}")
    if interaction_type not in _VALID_GLOBI_INTERACTION_TYPES:
        raise ValueError(
            f"Unknown interaction_type {interaction_type!r}. "
            f"Valid types: {', '.join(sorted(_VALID_GLOBI_INTERACTION_TYPES))}"
        )

    try:
        response = httpx.get(
            f"{GLOBI_BASE_URL}/interaction",
            params={
                "sourceTaxon": taxon_name,
                "interactionType": interaction_type,
            },
            timeout=HTTP_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()
    except HTTPStatusError as exc:
        logger.error("GLOBI HTTP error: %s", exc)
        return {"error": f"GLOBI HTTP error: {exc.response.status_code}"}
    except ConnectError as exc:
        logger.error("GLOBI connection error: %s", exc)
        return {"error": f"GLOBI connection failed: {exc}"}
    except TimeoutException as exc:
        logger.error("GLOBI timeout: %s", exc)
        return {"error": f"GLOBI request timed out: {exc}"}
    except Exception as exc:
        logger.exception("GLOBI unexpected error")
        return {"error": f"GLOBI query failed: {exc}"}

    all_interactions = _parse_response(data)
    total = len(all_interactions)
    capped = all_interactions[:MAX_INTERACTIONS]

    result: dict[str, Any] = {
        "source": taxon_name,
        "interaction_type": interaction_type,
        "interactions": capped,
        "count": total,
    }
    if total > MAX_INTERACTIONS:
        result["note"] = f"Showing {MAX_INTERACTIONS} of {total} interactions"

    return result
