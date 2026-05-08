import os
import pytest
from ewe_mcp.client import EwEMCPClient

def test_stanzas_inspection(live_client, sample_model_path):
    live_client.command_result("load_model", {"path": sample_model_path})
    # Verify we can read stanza groups from core domain
    res = live_client.command_result("get_values", {
        "domain": "core",
        "index": 1,
        "variables": ["stanza_groups"]
    })
    # This might fail if stanza_groups isn't formatted right, but let's check
    assert "values" in res

def test_detritus_fate_and_diet_matrix(live_client, sample_model_path):
    live_client.command_result("load_model", {"path": sample_model_path})
    # Verify we can read detritus fate and diet comp from ecopath groups
    res = live_client.command_result("get_values", {
        "domain": "ecopath_groups",
        "index": 1,
        "variables": ["detritus_fate", "diet_comp", "remark", "validation_status"]
    })
    vals = res["values"]
    assert "detritus_fate" in vals
    assert isinstance(vals["detritus_fate"], list)
    assert "diet_comp" in vals
    assert isinstance(vals["diet_comp"], list)
    assert "remark" in vals

def test_ecopath_balancing_diagnostics(live_client, sample_model_path):
    live_client.command_result("load_model", {"path": sample_model_path})
    # Run ecopath first
    live_client.command_result("run_ecopath")
    
    # Read diagnostics from outputs
    res = live_client.command_result("get_values", {
        "domain": "ecopath_group_outputs",
        "index": 1,
        "variables": ["eeoutput", "eeoutput_status", "pboutput", "pboutput_status"]
    })
    vals = res["values"]
    assert "eeoutput" in vals
    assert "eeoutput_status" in vals
    assert "pboutput" in vals
