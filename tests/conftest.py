import os
import pytest
from ewe_mcp.client import EwEMCPClient

@pytest.fixture
def sample_model_path():
    """Path to the sample EwE model on the VM. Requires EWE_MODEL_PATH to be set."""
    path = os.environ.get("EWE_MODEL_PATH")
    if not path:
        pytest.skip("EWE_MODEL_PATH must be set for live tests")
    return path

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
