"""FishBase client using DuckDB to query Parquet files on Source Cooperative S3."""

from __future__ import annotations

import logging

from typing import Any

import duckdb

from ewe_mcp.data_sources._shared import _TAXON_PATTERN, sanitize_taxon_name

logger = logging.getLogger("ewe_mcp")

FISHBASE_S3_BASE = "https://data.source.coop/cboettig/fishbase/fb/{version}/parquet"
DEFAULT_VERSION = "v24.07"
VALID_TABLES = {"popgrowth", "ecology", "diet"}

POPGROWTH_COLUMNS = ["Loo", "K", "tmax", "M", "MethodM"]
ECOLOGY_COLUMNS = ["DietTroph", "DietSeTroph", "FoodTroph", "FoodSeTroph", "FeedingType"]
DIET_ITEM_COLUMNS = ["FoodI", "FoodII", "FoodIII", "Stage", "DietPercent", "ItemName"]

MAX_ROWS = 500


def _validate_taxon_name(name: str, label: str) -> None:
    sanitized = sanitize_taxon_name(name)
    if not sanitized:
        raise ValueError(f"{label} must be a non-empty string")
    if not _TAXON_PATTERN.match(sanitized):
        raise ValueError(f"{label} contains invalid characters: {name!r}")


def _base_url(version: str = DEFAULT_VERSION) -> str:
    return FISHBASE_S3_BASE.format(version=version)


def _row_dicts(columns: list[str], rows: list[tuple]) -> list[dict[str, Any]]:
    return [dict(zip(columns, row)) for row in rows]


def _query_popgrowth(base: str, spec_codes: list[int]) -> list[dict[str, Any]]:
    if not spec_codes:
        return []
    placeholders = ", ".join(f"${i}" for i in range(1, len(spec_codes) + 1))
    cols = ", ".join(POPGROWTH_COLUMNS)
    sql = (
        f"SELECT {cols} FROM read_parquet('{base}/popgrowth.parquet') "
        f"WHERE SpecCode IN ({placeholders}) AND M IS NOT NULL "
        f"LIMIT {MAX_ROWS}"
    )
    result = duckdb.sql(sql, params=spec_codes)
    return _row_dicts(POPGROWTH_COLUMNS, result.fetchall())


def _query_ecology(base: str, spec_codes: list[int]) -> list[dict[str, Any]]:
    if not spec_codes:
        return []
    placeholders = ", ".join(f"${i}" for i in range(1, len(spec_codes) + 1))
    cols = ", ".join(ECOLOGY_COLUMNS)
    sql = (
        f"SELECT {cols} FROM read_parquet('{base}/ecology.parquet') "
        f"WHERE SpecCode IN ({placeholders}) AND DietTroph IS NOT NULL "
        f"LIMIT {MAX_ROWS}"
    )
    result = duckdb.sql(sql, params=spec_codes)
    return _row_dicts(ECOLOGY_COLUMNS, result.fetchall())


def _query_diet(base: str, spec_codes: list[int]) -> list[dict[str, Any]]:
    if not spec_codes:
        return []
    placeholders = ", ".join(f"${i}" for i in range(1, len(spec_codes) + 1))
    cols = ", ".join(DIET_ITEM_COLUMNS)
    sql = (
        f"SELECT {cols} FROM read_parquet('{base}/diet.parquet') d "
        f"JOIN read_parquet('{base}/diet_items.parquet') di ON d.DietCode = di.DietCode "
        f"WHERE d.SpecCode IN ({placeholders}) "
        f"LIMIT {MAX_ROWS}"
    )
    result = duckdb.sql(sql, params=spec_codes)
    return _row_dicts(DIET_ITEM_COLUMNS, result.fetchall())


def _resolve_spec_codes(base: str, genus: str, species: str) -> list[int]:
    """Look up SpecCode(s) for a binomial name in the species table.

    Returns a list of int SpecCodes.  Shared by FishBase and SeaLifeBase.
    """
    sql = (
        f"SELECT SpecCode FROM read_parquet('{base}/species.parquet') "
        f"WHERE Genus = $1 AND Species = $2"
    )
    spec_result = duckdb.sql(sql, params=[genus.strip(), species.strip()]).fetchall()
    spec_codes = [int(row[0]) for row in spec_result]
    return spec_codes


def query_fishbase(
    genus: str,
    species: str,
    tables: list[str] | None = None,
    version: str = DEFAULT_VERSION,
) -> dict[str, Any]:
    """Query FishBase for species trait data via DuckDB against Source Cooperative S3 Parquet files.

    Returns growth, mortality, trophic level, and diet composition data relevant
    for EwE model parameterization (P/B, Q/B, diet).

    Args:
        genus: Genus name (e.g., "Gadus").
        species: Species epithet (e.g., "morhua").
        tables: Optional list of tables to query: "popgrowth", "ecology", "diet".
            Defaults to all three.
        version: FishBase release version (default "v24.07").

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
        logger.error("FishBase species lookup failed: %s", exc)
        return {"error": f"FishBase query failed: {exc}"}

    result: dict[str, Any] = {}
    try:
        if "popgrowth" in tables:
            result["popgrowth"] = _query_popgrowth(base, spec_codes)
        if "ecology" in tables:
            result["ecology"] = _query_ecology(base, spec_codes)
        if "diet" in tables:
            result["diet"] = _query_diet(base, spec_codes)
    except Exception as exc:
        logger.error("FishBase data query failed: %s", exc)
        return {"error": f"FishBase query failed: {exc}"}

    return result
