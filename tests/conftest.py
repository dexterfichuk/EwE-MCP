import os
import pytest
from ewe_mcp.client import EwEMCPClient

FIXTURE_MODEL = os.path.join(os.path.dirname(__file__), "fixtures", "Tampa_Bay.EwEmdb")

@pytest.fixture
def sample_model_path():
    """Path to the sample EwE model included in the test fixtures."""
    return FIXTURE_MODEL

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