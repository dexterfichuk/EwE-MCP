"""SeaLifeBase client — reuses FishBase module with SeaLifeBase S3 prefix."""

from __future__ import annotations

import logging
from typing import Any

from ewe_mcp.data_sources.fishbase import (
    DEFAULT_VERSION,
    _query_diet,
    _query_ecology,
    _query_popgrowth,
    _resolve_spec_codes,
    _validate_taxon_name,
    VALID_TABLES,
)

logger = logging.getLogger("ewe_mcp")

SEALIFEBASE_S3_BASE = "https://data.source.coop/cboettig/fishbase/slb/{version}/parquet"


def _base_url(version: str = DEFAULT_VERSION) -> str:
    return SEALIFEBASE_S3_BASE.format(version=version)


def query_sealifebase(
    genus: str,
    species: str,
    tables: list[str] | None = None,
    version: str = DEFAULT_VERSION,
) -> dict[str, Any]:
    """Query SeaLifeBase for non-fish marine species trait data.

    Same interface and table structure as FishBase, but queries the SeaLifeBase
    Parquet files which cover invertebrates, marine mammals, and other non-fish taxa.

    Args:
        genus: Genus name (e.g., "Octopus").
        species: Species epithet (e.g., "vulgaris").
        tables: Optional list of tables to query: "popgrowth", "ecology", "diet".
            Defaults to all three.
        version: SeaLifeBase release version (default "v24.07").

    Returns:
        Dict keyed by table name with list of row dicts, or {"error": "..."} on failure.
    """
    _validate_taxon_name(genus, "genus")
    _validate_taxon_name(species, "species")

    if tables is None:
        tables = ["popgrowth", "ecology", "diet"]

    invalid = set(tables) - VALID_TABLES
    if invalid:
        raise ValueError(f"Invalid table(s): {', '.join(sorted(invalid))}. Valid: {sorted(VALID_TABLES)}")

    base = _base_url(version)

    try:
        spec_codes = _resolve_spec_codes(base, genus, species)
        if not spec_codes:
            return {t: [] for t in tables}
    except Exception as exc:
        logger.error("SeaLifeBase species lookup failed: %s", exc)
        return {"error": f"SeaLifeBase query failed: {exc}"}

    result: dict[str, Any] = {}
    try:
        if "popgrowth" in tables:
            result["popgrowth"] = _query_popgrowth(base, spec_codes)
        if "ecology" in tables:
            result["ecology"] = _query_ecology(base, spec_codes)
        if "diet" in tables:
            result["diet"] = _query_diet(base, spec_codes)
    except Exception as exc:
        logger.error("SeaLifeBase data query failed: %s", exc)
        return {"error": f"SeaLifeBase query failed: {exc}"}

    return result
