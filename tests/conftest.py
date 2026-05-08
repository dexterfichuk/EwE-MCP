import os
import pytest
from ewe_mcp.client import EwEMCPClient

@pytest.fixture
def sample_model_path():
    """Provides a consistent fallback path for the test model on the VM."""
    return os.environ.get(
        "EWE_MODEL_PATH", 
        r"C:\Users\dexter\Eii.Ecopath\Database\Tampa_Bay.EwEmdb"
    )

@pytest.fixture
def live_client():
    """Provides a connected synchronous client for live tests, skipping if disabled.
    Starts with a clean slate (model closed)."""
    if not os.environ.get("EWE_LIVE_TESTS"):
        pytest.skip("Skipping live tests")
    
    client = EwEMCPClient()
    assert client.connect()
    
    client.command_result("close_model")
    
    yield client
    
    client.disconnect()
