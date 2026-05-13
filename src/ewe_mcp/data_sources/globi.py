"""GLOBI client for predator-prey interaction data via REST API."""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger("ewe_mcp")

GLOBI_BASE_URL = "https://api.globalbioticinteractions.org"
MAX_INTERACTIONS = 100
HTTP_TIMEOUT = 15.0
COMMON_INTERACTION_TYPES = {
    "preysOn", "eatenBy", "parasiteOf", "hostOf",
    "pollinates", "pollinatedBy", "symbiontOf", "kills",
}

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
    except Exception as exc:
        logger.error("GLOBI query failed: %s", exc)
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
    if interaction_type not in COMMON_INTERACTION_TYPES:
        result["note"] = (
            f"Interaction type '{interaction_type}' is uncommon. "
            f"Common types: {', '.join(sorted(COMMON_INTERACTION_TYPES))}"
        )
    if total > MAX_INTERACTIONS:
        cap_note = f"Showing {MAX_INTERACTIONS} of {total} interactions"
        result["note"] = f"{result.get('note', '')} {cap_note}".strip()

    return result
