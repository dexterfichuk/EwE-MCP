"""Helpers for name-based mixed-domain batch updates."""

from __future__ import annotations

from difflib import SequenceMatcher
from typing import Any


DOMAIN_CONFIG: dict[str, dict[str, Any]] = {
    "ecopath_group": {"match_fields": ["name"]},
    "ecopath_fleet": {"match_fields": ["name"]},
    "ecosim_group_input": {"match_fields": ["name"]},
    "ecosim_fleet_input": {"match_fields": ["name"]},
}


STATIC_WRITABLE_FIELDS: dict[str, set[str]] = {
    "ecopath_group": {"name", "pb", "qb", "ee", "ba"},
    "ecopath_fleet": {"name", "landing", "discards", "value"},
    "ecosim_group_input": {
        "name",
        "vul_mult",
        "max_rel_pb",
        "max_rel_feeding_time",
        "feeding_time_adjust_rate",
        "den_dep_catchability",
    },
    "ecosim_fleet_input": {
        "name",
        "rel_q",
        "effort_conversion_factor",
        "sail_cost",
    },
}


def default_capabilities(
    dynamic_fields: dict[str, set[str]] | None,
) -> dict[str, Any]:
    """Build capability payload with dynamic-first writable field precedence."""
    domains: dict[str, Any] = {}
    fallback_active = False
    for domain, cfg in DOMAIN_CONFIG.items():
        dyn = (dynamic_fields or {}).get(domain)
        if dyn:
            writable = sorted(dyn)
            field_sources = {k: "dynamic" for k in writable}
        else:
            writable = sorted(STATIC_WRITABLE_FIELDS.get(domain, set()))
            field_sources = {k: "static_fallback" for k in writable}
            fallback_active = True
        domains[domain] = {
            "match_fields": cfg["match_fields"],
            "writable_fields": writable,
            "field_sources": field_sources,
        }
    return {
        "domains": domains,
        "diagnostics": {
            "dynamic_metadata_available": bool(dynamic_fields),
            "fallback_active": fallback_active,
        },
    }


def _entity_field(entity: dict[str, Any], field: str) -> str:
    """Get match_field from entity, checking top-level then values dict."""
    raw = entity.get(field) or entity.get("values", {}).get(field) or ""
    return str(raw).strip()


def resolve_exact_name(
    entities: list[dict[str, Any]],
    target_name: str,
    match_field: str = "name",
) -> dict[str, Any]:
    """Resolve exact match by field; returns exact/not_found/ambiguous metadata."""
    target = target_name.strip().casefold()
    exact = [
        e for e in entities
        if _entity_field(e, match_field).casefold() == target
    ]
    if len(exact) == 1:
        return {
            "resolution_status": "exact",
            "resolved_index": exact[0].get("index"),
            "entity": exact[0],
        }
    if len(exact) > 1:
        return {
            "resolution_status": "ambiguous",
            "candidates": [
                {"index": e.get("index"), "name": _entity_field(e, match_field)} for e in exact
            ],
        }
    return {"resolution_status": "not_found"}


def rank_name_suggestions(
    target_name: str,
    entities: list[dict[str, Any]],
    max_suggestions: int = 5,
    match_field: str = "name",
) -> list[dict[str, Any]]:
    """Rank fuzzy suggestions for operator-guided retry; never auto-applies."""
    ranked: list[dict[str, Any]] = []
    target = target_name.strip().casefold()
    for entity in entities:
        candidate = _entity_field(entity, match_field)
        if not candidate:
            continue
        score = SequenceMatcher(None, target, candidate.casefold()).ratio()
        ranked.append(
            {"name": candidate, "index": entity.get("index"), "score": round(score, 4)}
        )
    ranked.sort(key=lambda x: x["score"], reverse=True)
    return ranked[:max_suggestions]
