import pytest


def test_stanzas_inspection(live_client, sample_model_path):
    live_client.command_result("load_model", {"path": sample_model_path})
    # Verify we can read stanza groups from core domain
    res = live_client.command_result(
        "get_values", {"domain": "core", "index": 1, "variables": ["stanza_groups"]}
    )
    # This might fail if stanza_groups isn't formatted right, but let's check
    assert "values" in res


def test_detritus_fate_and_diet_matrix(live_client, sample_model_path):
    live_client.command_result("load_model", {"path": sample_model_path})
    # Verify we can read detritus fate and diet comp from ecopath groups
    res = live_client.command_result(
        "get_values",
        {
            "domain": "ecopath_groups",
            "index": 1,
            "variables": ["detritus_fate", "diet_comp", "remark", "validation_status"],
        },
    )
    vals = res["values"]
    assert "detritus_fate" in vals
    assert isinstance(vals["detritus_fate"], list)
    assert "diet_comp" in vals
    assert isinstance(vals["diet_comp"], list)
    assert "remark" in vals


def test_ecopath_balancing_diagnostics(live_client, sample_model_path):
    live_client.command_result("load_model", {"path": sample_model_path})
    run_res = live_client.command_result("run_ecopath")
    assert run_res["started"] is True
    assert "is_model_balanced" in run_res
    assert "ecopath_run_state" in run_res

    res = live_client.command_result(
        "get_values",
        {
            "domain": "ecopath_group_outputs",
            "index": 1,
            "variables": ["eeoutput", "eeoutput_status", "pboutput", "pboutput_status"],
        },
    )
    vals = res["values"]
    assert "eeoutput" in vals
    assert "eeoutput_status" in vals
    assert "pboutput" in vals


def test_ecopath_output_domains_are_read_only(live_client, sample_model_path):
    live_client.command_result("load_model", {"path": sample_model_path})
    live_client.command_result("run_ecopath")

    variables = live_client.command_result(
        "list_variables", {"domain": "ecopath_group_outputs", "index": 1}
    )
    biomass = next(
        v for v in variables["variables"] if v["name"] == "biomass_area"
    )
    assert biomass["writable"] is False

    res = live_client.command_result(
        "set_values",
        {
            "domain": "ecopath_group_outputs",
            "index": 1,
            "values": {"biomass_area": 0.0005},
            "navigate": False,
        },
    )
    assert res["results"]["biomass_area"] == "unknown_or_readonly_property"


def test_ecopath_stats_generic_get_values(live_client, sample_model_path):
    live_client.command_result("load_model", {"path": sample_model_path})
    live_client.command_result("run_ecopath")

    res = live_client.command_result("get_values", {"domain": "ecopath_stats"})
    assert res["domain"] == "ecopath_stats"
    assert res["index"] is None
    assert len(res["values"]) > 0


def test_ecospace_scenario_list_and_load(live_client, sample_model_path):
    live_client.command_result("load_model", {"path": sample_model_path})
    scenarios = live_client.command_result(
        "list_entities", {"domain": "ecospace_scenarios"}
    )
    assert scenarios["total_count"] >= 1
    dedicated = live_client.command_result("list_ecospace_scenarios")
    assert dedicated["total_count"] >= 1
    live_client.command_result("load_ecosim_scenario", {"index": 1})
    loaded = live_client.command_result("load_ecospace_scenario", {"index": 1})
    assert loaded["loaded"] is True


def test_ecotracer_scenario_list_and_load(live_client, sample_model_path):
    live_client.command_result("load_model", {"path": sample_model_path})
    scenarios = live_client.command_result(
        "list_entities", {"domain": "ecotracer_scenarios"}
    )
    if scenarios["total_count"] < 1:
        pytest.skip("No Ecotracer scenarios in sample model")
    dedicated = live_client.command_result("list_ecotracer_scenarios")
    assert dedicated["total_count"] >= 1
    live_client.command_result("load_ecosim_scenario", {"index": 1})
    loaded = live_client.command_result("load_ecotracer_scenario", {"index": 1})
    assert loaded["loaded"] is True
