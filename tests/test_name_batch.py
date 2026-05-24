from unittest.mock import patch, AsyncMock

import pytest

from ewe_mcp.name_batch import (
    _entity_field,
    default_capabilities,
    resolve_exact_name,
    rank_name_suggestions,
)


@pytest.fixture(autouse=True)
def _reset_writable_fields_cache():
    import ewe_mcp.server as srv
    original = srv._writable_fields_cache
    srv._writable_fields_cache = None
    yield
    srv._writable_fields_cache = original


def test_default_capabilities_has_domains_and_provenance():
    caps = default_capabilities(dynamic_fields=None)
    assert "domains" in caps
    assert "ecopath_group" in caps["domains"]
    group = caps["domains"]["ecopath_group"]
    assert "match_fields" in group
    assert "name" in group["match_fields"]
    assert "writable_fields" in group
    assert "name" in group["writable_fields"]
    assert group["field_sources"]["name"] == "static_fallback"


def test_default_capabilities_prefers_dynamic_field_source():
    dynamic = {"ecopath_group": {"name", "pb", "qb"}}
    caps = default_capabilities(dynamic_fields=dynamic)
    group = caps["domains"]["ecopath_group"]
    assert "pb" in group["writable_fields"]
    assert group["field_sources"]["pb"] == "dynamic"


def test_resolve_exact_name_success():
    entities = [
        {"index": 1, "name": "Herring"},
        {"index": 2, "name": "Anchovy"},
    ]
    result = resolve_exact_name(entities, "Anchovy")
    assert result["resolution_status"] == "exact"
    assert result["resolved_index"] == 2


def test_resolve_exact_name_ambiguous():
    entities = [
        {"index": 1, "name": "Anchovy"},
        {"index": 2, "name": "Anchovy"},
    ]
    result = resolve_exact_name(entities, "Anchovy")
    assert result["resolution_status"] == "ambiguous"
    assert len(result["candidates"]) == 2


def test_resolve_exact_name_not_found():
    entities = [{"index": 1, "name": "Herring"}]
    result = resolve_exact_name(entities, "Anchovy")
    assert result["resolution_status"] == "not_found"


def test_rank_name_suggestions_returns_scored_candidates():
    entities = [
        {"index": 1, "name": "Shrimp Trawl"},
        {"index": 2, "name": "Pelagic Trawl"},
    ]
    suggestions = rank_name_suggestions("Shrimp trawl", entities, max_suggestions=2)
    assert suggestions[0]["name"] == "Shrimp Trawl"
    assert suggestions[0]["score"] > 0.9


# ── Capability discovery tool tests ─────────────────────────────────


@pytest.mark.asyncio
async def test_list_name_batch_capabilities_has_expected_shape():
    from ewe_mcp.server import list_name_batch_capabilities

    result = await list_name_batch_capabilities()
    assert "domains" in result
    assert "diagnostics" in result
    assert "ecopath_group" in result["domains"]
    group = result["domains"]["ecopath_group"]
    assert "match_fields" in group
    assert "writable_fields" in group
    assert "field_sources" in group


@pytest.mark.asyncio
async def test_list_name_batch_capabilities_contains_fallback_diagnostics():
    from ewe_mcp.server import list_name_batch_capabilities

    result = await list_name_batch_capabilities()
    assert "fallback_active" in result["diagnostics"]
    assert "dynamic_metadata_available" in result["diagnostics"]


# ── set_values_by_name_batch tool tests ──────────────────────────────


@pytest.mark.asyncio
async def test_set_values_by_name_batch_applies_exact_match():
    from ewe_mcp.server import set_values_by_name_batch

    operations = [
        {"domain": "ecopath_group", "name": "Anchovy", "values": {"pb": 1.5}}
    ]

    async def fake_send(command_type, params=None, timeout=30):
        if command_type == "get_values_range":
            return {"entities": [{"index": 2, "name": "Anchovy"}]}
        if command_type == "validate_values":
            return {"valid": True, "results": {"pb": {"valid": True}}}
        if command_type == "set_values":
            return {"results": {"pb": "stored"}}
        if command_type == "refresh_ui":
            return {}
        raise AssertionError(f"Unexpected command: {command_type}")

    with patch("ewe_mcp.server._send", side_effect=fake_send):
        result = await set_values_by_name_batch(operations)

    assert result["status"] == "success"
    assert result["summary"]["applied"] == 1
    assert result["results"][0]["status"] == "applied"
    assert result["results"][0]["resolved_index"] == 2


@pytest.mark.asyncio
async def test_set_values_by_name_batch_not_found_has_suggestions():
    from ewe_mcp.server import set_values_by_name_batch

    operations = [
        {"domain": "ecopath_fleet", "name": "Nonexistent Fleet", "values": {"landing": 1.2}}
    ]

    async def fake_send(command_type, params=None, timeout=30):
        if command_type == "get_values_range":
            return {"entities": [{"index": 3, "name": "Shrimp Trawl"}]}
        raise AssertionError(f"Unexpected command: {command_type}")

    with patch("ewe_mcp.server._send", side_effect=fake_send):
        result = await set_values_by_name_batch(operations)

    assert result["status"] == "failed"
    assert result["results"][0]["status"] == "not_found"
    assert result["results"][0]["suggestions"][0]["name"] == "Shrimp Trawl"


@pytest.mark.asyncio
async def test_set_values_by_name_batch_dry_run_does_not_mutate():
    from ewe_mcp.server import set_values_by_name_batch

    operations = [
        {"domain": "ecopath_group", "name": "Anchovy", "values": {"pb": 1.5}}
    ]

    async def fake_send(command_type, params=None, timeout=30):
        if command_type == "get_values_range":
            return {"entities": [{"index": 2, "name": "Anchovy"}]}
        if command_type == "validate_values":
            return {"valid": True, "results": {"pb": {"valid": True}}}
        if command_type == "set_values":
            raise AssertionError("set_values must not be called in dry_run")
        raise AssertionError(f"Unexpected command: {command_type}")

    with patch("ewe_mcp.server._send", side_effect=fake_send):
        result = await set_values_by_name_batch(operations, options={"dry_run": True})

    assert result["status"] == "success"
    assert result["results"][0]["status"] == "applied"
    assert result["results"][0]["dry_run"] is True


@pytest.mark.asyncio
async def test_set_values_by_name_batch_unknown_domain():
    from ewe_mcp.server import set_values_by_name_batch

    operations = [
        {"domain": "invalid_domain", "name": "test", "values": {"name": "test"}}
    ]

    with patch("ewe_mcp.server._send", return_value={"entities": []}):
        result = await set_values_by_name_batch(operations)

    assert result["results"][0]["status"] == "set_failed"
    assert result["results"][0]["error_code"] == "unknown_domain"


@pytest.mark.asyncio
async def test_set_values_by_name_batch_unknown_field():
    import ewe_mcp.server as srv

    operations = [
        {"domain": "ecopath_group", "name": "Anchovy", "values": {"nonexistent_field": 1.0}}
    ]

    async def fake_send(command_type, params=None, timeout=30):
        if command_type == "get_values_range":
            return {"entities": [{"index": 2, "name": "Anchovy"}]}
        if command_type == "list_variables":
            return {
                "variables": [
                    {"name": "pb", "writable": True},
                    {"name": "name", "writable": True},
                ]
            }
        raise AssertionError(f"Unexpected command: {command_type}")

    srv._writable_fields_cache = {"ecopath_group": {"pb", "name"}}
    with patch("ewe_mcp.server._send", side_effect=fake_send):
        result = await srv.set_values_by_name_batch(operations)
    assert result["results"][0]["status"] == "validation_failed"
    assert result["results"][0]["error_code"] == "unknown_field"


# ── _entity_field helper tests ──────────────────────────────────────


def test_entity_field_reads_top_level():
    assert _entity_field({"name": "Herring"}, "name") == "Herring"


def test_entity_field_falls_back_to_values_dict():
    entity = {"index": 1, "values": {"name": "Herring"}}
    assert _entity_field(entity, "name") == "Herring"


def test_entity_field_top_level_takes_precedence():
    entity = {"name": "Top", "values": {"name": "Nested"}}
    assert _entity_field(entity, "name") == "Top"


def test_entity_field_missing_returns_empty():
    assert _entity_field({"index": 1}, "name") == ""


# ── resolve_exact_name with values dict ─────────────────────────────


def test_resolve_exact_name_reads_from_values_dict():
    entities = [{"index": 1, "values": {"name": "Herring"}}]
    result = resolve_exact_name(entities, "Herring")
    assert result["resolution_status"] == "exact"
    assert result["resolved_index"] == 1


# ── rank_name_suggestions with values dict ──────────────────────────


def test_rank_name_suggestions_reads_from_values_dict():
    entities = [{"index": 1, "values": {"name": "Shrimp Trawl"}}]
    suggestions = rank_name_suggestions("Shrimp trawl", entities)
    assert suggestions[0]["name"] == "Shrimp Trawl"


# ── match_field validation test ─────────────────────────────────────


@pytest.mark.asyncio
async def test_set_values_by_name_batch_invalid_match_field():
    from ewe_mcp.server import set_values_by_name_batch

    operations = [
        {"domain": "ecopath_group", "name": "Anchovy", "values": {"pb": 1.5}, "match_field": "invalid_field"}
    ]

    with patch("ewe_mcp.server._send", return_value={}):
        result = await set_values_by_name_batch(operations)

    assert result["results"][0]["status"] == "set_failed"
    assert result["results"][0]["error_code"] == "invalid_match_field"


# ── Paging through truncated results ────────────────────────────────


@pytest.mark.asyncio
async def test_set_values_by_name_batch_pages_through_truncated():
    from ewe_mcp.server import set_values_by_name_batch

    page1 = {
        "entities": [{"index": i, "name": f"Group{i}"} for i in range(1, 501)],
        "truncated": True,
    }
    page2 = {
        "entities": [{"index": 501, "name": "Anchovy"}],
        "truncated": False,
    }
    call_count = 0

    async def fake_send(command_type, params=None, timeout=30):
        nonlocal call_count
        if command_type == "get_values_range":
            call_count += 1
            if params and params.get("start_index", 1) == 1:
                return page1
            return page2
        if command_type == "validate_values":
            return {"valid": True, "results": {"pb": {"valid": True}}}
        if command_type == "set_values":
            return {"results": {"pb": "stored"}}
        if command_type == "refresh_ui":
            return {}
        raise AssertionError(f"Unexpected command: {command_type}")

    with patch("ewe_mcp.server._send", side_effect=fake_send):
        result = await set_values_by_name_batch(
            [{"domain": "ecopath_group", "name": "Anchovy", "values": {"pb": 1.5}}]
        )

    assert result["results"][0]["status"] == "applied"
    assert result["results"][0]["resolved_index"] == 501
    assert call_count == 2


# ── Entity cache within batch ───────────────────────────────────────


@pytest.mark.asyncio
async def test_set_values_by_name_batch_caches_entities_per_domain():
    from ewe_mcp.server import set_values_by_name_batch

    get_range_calls = 0

    async def fake_send(command_type, params=None, timeout=30):
        nonlocal get_range_calls
        if command_type == "get_values_range":
            get_range_calls += 1
            return {"entities": [{"index": 2, "name": "Anchovy"}]}
        if command_type == "validate_values":
            return {"valid": True, "results": {"pb": {"valid": True}}}
        if command_type == "set_values":
            return {"results": {"pb": "stored"}}
        if command_type == "refresh_ui":
            return {}
        raise AssertionError(f"Unexpected command: {command_type}")

    operations = [
        {"domain": "ecopath_group", "name": "Anchovy", "values": {"pb": 1.5}},
        {"domain": "ecopath_group", "name": "Anchovy", "values": {"qb": 2.0}},
    ]

    with patch("ewe_mcp.server._send", side_effect=fake_send):
        result = await set_values_by_name_batch(operations)

    assert result["summary"]["applied"] == 2
    assert get_range_calls == 1


# ── Dynamic discovery path test ─────────────────────────────────────


@pytest.mark.asyncio
async def test_discover_dynamic_writable_fields_uses_writable_key():
    import ewe_mcp.server as srv

    async def fake_list_variables(domain):
        return {
            "variables": [
                {"name": "pb", "writable": True},
                {"name": "ee", "writable": False},
                {"name": "name", "writable": True},
            ]
        }

    with patch.object(srv, "list_variables", side_effect=fake_list_variables):
        dynamic = await srv._discover_dynamic_writable_fields()

    assert "ecopath_group" in dynamic
    assert "pb" in dynamic["ecopath_group"]
    assert "name" in dynamic["ecopath_group"]
    assert "ee" not in dynamic["ecopath_group"]
