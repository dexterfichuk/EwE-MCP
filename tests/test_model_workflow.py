import os
import pytest
from ewe_mcp.client import EwEMCPClient

@pytest.fixture
def live_client():
    if not os.environ.get("EWE_LIVE_TESTS"):
        pytest.skip("Skipping live model workflow tests because EWE_LIVE_TESTS is not set")
    
    client = EwEMCPClient()
    assert client.connect()
    
    # Ensure starting from a clean slate
    client.command_result("close_model")
    
    yield client
    
    client.disconnect()


def test_model_metadata_and_save_workflow(live_client):
    original_path = os.environ.get("EWE_MODEL_PATH")
    if not original_path:
        pytest.skip("EWE_MODEL_PATH must be set for live model workflow tests")
        
    scratch_path = original_path.replace(".EwEmdb", "_Scratch.EwEmdb")
    
    # 1. Load original
    live_client.command_result("load_model", {"path": original_path})
    
    # 2. Get metadata
    info = live_client.command_result("get_model_info")
    assert info["loaded"] is True
    assert "metadata" in info
    assert "Author" in info["metadata"]
    assert "Description" in info["metadata"]
    
    # 3. Edit something harmless (e.g., name of group 1)
    original_group = live_client.command_result("get_ecopath_group", {"index": 1})
    new_name = original_group["name"] + " (Test)"
    
    live_client.command_result("set_values", {
        "domain": "ecopath_group",
        "index": 1,
        "values": {"name": new_name},
        "navigate": False
    })
    
    # Ensure there are changes
    assert live_client.command_result("has_changes")["has_changes"] is True
    
    # 4. Save as new scratch model
    live_client.command_result("save_model_as", {"path": scratch_path})
    
    # 5. Close it
    live_client.command_result("close_model")
    info_closed = live_client.command_result("get_model_info")
    assert info_closed["loaded"] is False
    
    # 6. Load the scratch model and verify changes
    live_client.command_result("load_model", {"path": scratch_path})
    scratch_group = live_client.command_result("get_ecopath_group", {"index": 1})
    assert scratch_group["name"] == new_name
    
    # Cleanup
    live_client.command_result("close_model")
    
    # Note: we can't easily delete the file from the VM here since tests run locally,
    # but in a real environment the script would clean up. For testing against the VM,
    # we just overwrite the scratch file each time.
