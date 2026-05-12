"""FishBase client using DuckDB to query Parquet files on Source Cooperative S3."""

from __future__ import annotations

import logging
from typing import Any

import duckdb

logger = logging.getLogger("ewe_mcp")

FISHBASE_S3_BASE = "https://data.source.coop/cboettig/fishbase/fb/{version}/parquet"
DEFAULT_VERSION = "v24.07"
VALID_TABLES = {"popgrowth", "ecology", "diet"}

POPGRWOTH_COLUMNS = ["Loo", "K", "tmax", "M", "MethodM"]
ECOLOGY_COLUMNS = ["DietTroph", "DietSeTroph", "FoodTroph", "FoodSeTroph", "FeedingType"]
DIET_ITEM_COLUMNS = ["FoodI", "FoodII", "FoodIII", "Stage", "DietPercent", "ItemName"]

MAX_ROWS = 500


def _base_url(version: str = DEFAULT_VERSION) -> str:
    return FISHBASE_S3_BASE.format(version=version)


def _parquet_url(table: str, version: str = DEFAULT_VERSION) -> str:
    return f"{_base_url(version)}/{table}.parquet"


def _row_dicts(columns: list[str], rows: list[tuple]) -> list[dict[str, Any]]:
    return [dict(zip(columns, row)) for row in rows]


def _query_popgrowth(base: str, spec_codes: list[int]) -> list[dict[str, Any]]:
    if not spec_codes:
        return []
    codes = ",".join(str(c) for c in spec_codes)
    cols = ", ".join(POPGRWOTH_COLUMNS)
    sql = (
        f"SELECT {cols} FROM read_parquet('{base}/popgrowth.parquet') "
        f"WHERE SpecCode IN ({codes}) AND M IS NOT NULL "
        f"LIMIT {MAX_ROWS}"
    )
    result = duckdb.sql(sql)
    return _row_dicts(POPGRWOTH_COLUMNS, result.fetchall())


def _query_ecology(base: str, spec_codes: list[int]) -> list[dict[str, Any]]:
    if not spec_codes:
        return []
    codes = ",".join(str(c) for c in spec_codes)
    cols = ", ".join(ECOLOGY_COLUMNS)
    sql = (
        f"SELECT {cols} FROM read_parquet('{base}/ecology.parquet') "
        f"WHERE SpecCode IN ({codes}) AND DietTroph IS NOT NULL "
        f"LIMIT {MAX_ROWS}"
    )
    result = duckdb.sql(sql)
    return _row_dicts(ECOLOGY_COLUMNS, result.fetchall())


def _query_diet(base: str, spec_codes: list[int]) -> list[dict[str, Any]]:
    if not spec_codes:
        return []
    codes = ",".join(str(c) for c in spec_codes)
    cols = ", ".join(DIET_ITEM_COLUMNS)
    sql = (
        f"SELECT {cols} FROM read_parquet('{base}/diet.parquet') d "
        f"JOIN read_parquet('{base}/diet_items.parquet') di ON d.DietCode = di.DietCode "
        f"WHERE d.SpecCode IN ({codes}) "
        f"LIMIT {MAX_ROWS}"
    )
    result = duckdb.sql(sql)
    return _row_dicts(DIET_ITEM_COLUMNS, result.fetchall())


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
    if tables is None:
        tables = ["popgrowth", "ecology", "diet"]

    invalid = set(tables) - VALID_TABLES
    if invalid:
        raise ValueError(f"Invalid table(s): {', '.join(sorted(invalid))}. Valid: {sorted(VALID_TABLES)}")

    base = _base_url(version)

    try:
        spec_sql = (
            f"SELECT SpecCode FROM read_parquet('{base}/species.parquet') "
            f"WHERE Genus = '{genus}' AND Species = '{species}'"
        )
        spec_result = duckdb.sql(spec_sql).fetchall()
        spec_codes = [row[0] for row in spec_result]
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
