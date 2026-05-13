import pytest

from ewe_mcp.helpers import (
    BATCH_BLOCKED_COMMANDS,
    DEFAULT_MAX_RESPONSE_SIZE,
    EWE_NULL_VALUE,
    STATUS_CODE_MAP,
    error_hint,
    get_max_response_size,
    normalize_domain_name,
    validate_ewe_ranges,
)


def test_error_hints_cover_common_failures():
    assert "EwE is running" in error_hint("Connection refused")
    assert "EWE_MCP_TOKEN" in error_hint("unauthorized token")
    assert "list_ui_commands" in error_hint("command not found")


def test_batch_blocks_dangerous_commands():
    assert "execute_ewe_code" in BATCH_BLOCKED_COMMANDS
    assert "close_model" in BATCH_BLOCKED_COMMANDS


def test_max_response_size_default():
    assert DEFAULT_MAX_RESPONSE_SIZE == 10 * 1024 * 1024


def test_max_response_size_env_override(monkeypatch):
    get_max_response_size.cache_clear()
    monkeypatch.setenv("EWE_MCP_MAX_RESPONSE_SIZE", "5242880")
    try:
        assert get_max_response_size() == 5 * 1024 * 1024
    finally:
        get_max_response_size.cache_clear()


def test_max_response_size_invalid(monkeypatch):
    get_max_response_size.cache_clear()
    monkeypatch.setenv("EWE_MCP_MAX_RESPONSE_SIZE", "not_a_number")
    try:
        with pytest.raises(ValueError, match="must be an integer"):
            get_max_response_size()
    finally:
        get_max_response_size.cache_clear()


def test_max_response_size_negative(monkeypatch):
    get_max_response_size.cache_clear()
    monkeypatch.setenv("EWE_MCP_MAX_RESPONSE_SIZE", "-1")
    try:
        with pytest.raises(ValueError, match="must be positive"):
            get_max_response_size()
    finally:
        get_max_response_size.cache_clear()


def test_error_hint_oversized_response():
    hint = error_hint("Response too large: 50000000 bytes (limit 10485760 bytes)")
    assert "size limit" in hint
    assert "smaller subsets" in hint


def test_error_hint_dialog_command_blocked():
    hint = error_hint("dialog_command_blocked: ImportTimeSeries")
    assert "modal dialog" in hint
    assert "dialog_watcher_enabled" in hint


# ── normalize_domain_name ───────────────────────────────────────────────


def test_normalize_domain_name_singular_to_plural():
    assert normalize_domain_name("ecopath_group") == "ecopath_group"
    assert normalize_domain_name("ecopath_groups") == "ecopath_group"
    assert normalize_domain_name("ecopath_fleet") == "ecopath_fleet"
    assert normalize_domain_name("ecopath_fleets") == "ecopath_fleet"
    assert normalize_domain_name("ecospace_habitats") == "ecospace_habitat"
    # Dead aliases removed — these should pass through unchanged
    assert normalize_domain_name("ecosim_groups") == "ecosim_groups"
    assert normalize_domain_name("ecospace_fleets") == "ecospace_fleets"


def test_normalize_domain_name_unknown_passes_through():
    assert normalize_domain_name("custom_domain") == "custom_domain"


# ── validate_ewe_ranges ─────────────────────────────────────────────────


def test_validate_ewe_ranges_den_dep_catchability_rejects_zero():
    result = validate_ewe_ranges("ecosim_group_inputs", {"den_dep_catchability": 0})
    assert result["den_dep_catchability"]["valid"] is False
    assert ">= 1" in result["den_dep_catchability"]["reason"]


def test_validate_ewe_ranges_den_dep_catchability_rejects_half():
    result = validate_ewe_ranges("ecosim_group_inputs", {"den_dep_catchability": 0.5})
    assert result["den_dep_catchability"]["valid"] is False


def test_validate_ewe_ranges_den_dep_catchability_accepts_one():
    result = validate_ewe_ranges("ecosim_group_inputs", {"den_dep_catchability": 1.0})
    assert result["den_dep_catchability"]["valid"] is True


def test_validate_ewe_ranges_vul_mult_rejects_below_one():
    result = validate_ewe_ranges("ecosim_group_inputs", {"vul_mult": 0.5})
    assert result["vul_mult"]["valid"] is False


def test_validate_ewe_ranges_rel_q_rejects_below_one():
    result = validate_ewe_ranges("ecosim_fleet_inputs", {"rel_q": 0.01})
    assert result["rel_q"]["valid"] is False


def test_validate_ewe_ranges_max_rel_feeding_time_rejects_zero():
    """max_rel_feeding_time is gt 0 (exclusive min), so 0 is invalid."""
    result = validate_ewe_ranges("ecosim_group_inputs", {"max_rel_feeding_time": 0})
    assert result["max_rel_feeding_time"]["valid"] is False
    assert ">> 0" in result["max_rel_feeding_time"]["reason"]


def test_validate_ewe_ranges_max_rel_feeding_time_accepts_positive():
    result = validate_ewe_ranges("ecosim_group_inputs", {"max_rel_feeding_time": 1.0})
    assert result["max_rel_feeding_time"]["valid"] is True


def test_validate_ewe_ranges_max_rel_feeding_time_rejects_above_100():
    """max_rel_feeding_time has an inclusive max of 100."""
    result = validate_ewe_ranges("ecosim_group_inputs", {"max_rel_feeding_time": 101})
    assert result["max_rel_feeding_time"]["valid"] is False


def test_validate_ewe_ranges_max_rel_feeding_time_accepts_100():
    result = validate_ewe_ranges("ecosim_group_inputs", {"max_rel_feeding_time": 100})
    assert result["max_rel_feeding_time"]["valid"] is True


def test_validate_ewe_ranges_effort_conversion_factor_rejects_zero():
    """effort_conversion_factor is gt 0 (exclusive min), so 0 is invalid."""
    result = validate_ewe_ranges("ecosim_fleet_inputs", {"effort_conversion_factor": 0})
    assert result["effort_conversion_factor"]["valid"] is False
    assert ">> 0" in result["effort_conversion_factor"]["reason"]


def test_validate_ewe_ranges_effort_conversion_factor_accepts_positive():
    result = validate_ewe_ranges(
        "ecosim_fleet_inputs", {"effort_conversion_factor": 0.001}
    )
    assert result["effort_conversion_factor"]["valid"] is True


def test_validate_ewe_ranges_feeding_time_adjust_rate_rejects_one():
    """feeding_time_adjust_rate is lt 1 (exclusive max), so 1.0 is invalid."""
    result = validate_ewe_ranges(
        "ecosim_group_inputs", {"feeding_time_adjust_rate": 1.0}
    )
    assert result["feeding_time_adjust_rate"]["valid"] is False
    assert "< 1" in result["feeding_time_adjust_rate"]["reason"]


def test_validate_ewe_ranges_feeding_time_adjust_rate_accepts_below_one():
    result = validate_ewe_ranges(
        "ecosim_group_inputs", {"feeding_time_adjust_rate": 0.9}
    )
    assert result["feeding_time_adjust_rate"]["valid"] is True


def test_validate_ewe_ranges_feeding_time_adjust_rate_accepts_zero():
    """feeding_time_adjust_rate has inclusive min of 0."""
    result = validate_ewe_ranges("ecosim_group_inputs", {"feeding_time_adjust_rate": 0})
    assert result["feeding_time_adjust_rate"]["valid"] is True


def test_validate_ewe_ranges_max_rel_pb_rejects_zero():
    """max_rel_pb is gt 0 (exclusive min), so 0 is invalid."""
    result = validate_ewe_ranges("ecosim_group_inputs", {"max_rel_pb": 0})
    assert result["max_rel_pb"]["valid"] is False
    assert ">> 0" in result["max_rel_pb"]["reason"]


def test_validate_ewe_ranges_max_rel_pb_accepts_positive():
    result = validate_ewe_ranges("ecosim_group_inputs", {"max_rel_pb": 0.1})
    assert result["max_rel_pb"]["valid"] is True


def test_validate_ewe_ranges_unknown_domain_passes():
    result = validate_ewe_ranges("unknown_domain", {"any_field": 0})
    assert result == {}


def test_validate_ewe_ranges_unknown_variable_passes():
    result = validate_ewe_ranges("ecosim_group_inputs", {"unknown_var": 0})
    assert result == {}


def test_ewe_null_value_constant():
    assert EWE_NULL_VALUE == -9999.0


# ── STATUS_CODE_MAP ─────────────────────────────────────────────────────


def test_status_code_map_contains_known_codes():
    assert 513 in STATUS_CODE_MAP
    assert 528 in STATUS_CODE_MAP
    assert 532 in STATUS_CODE_MAP
    assert 784 in STATUS_CODE_MAP
    assert "Stored" in STATUS_CODE_MAP[513]
    assert "NotEditable" in STATUS_CODE_MAP[528]


# ── error_hint: range violations and modal dialogs ─────────────────────


def test_error_hint_ewe_range_violation():
    hint = error_hint("EcosimRelQ cannot be 0. Value is out of range.")
    assert "out of range" in hint
    assert "validate_values" in hint


def test_error_hint_modal_dialog_data_keyerror():
    hint = error_hint("KeyError: 'data'")
    assert "modal dialog" in hint.lower()


def test_error_hint_den_dep_catchability_zero():
    hint = error_hint("DenDepCatchability cannot be 0")
    assert "out of range" in hint
    assert "validate_values" in hint
