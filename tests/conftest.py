import os
from pathlib import Path

import pytest
from ewe_mcp.client import EwEMCPClient

FIXTURE_MODEL = os.path.join(os.path.dirname(__file__), "fixtures", "Tampa_Bay.EwEmdb")

SPATIAL_MODEL = os.path.join(
    os.path.dirname(__file__), "fixtures", "Anchovy_Bay_Spatial_Mosaic.eweaccdb"
)


def _remote_root():
    """Resolve the VM remote root path, or skip if not configured."""
    root = os.environ.get("EWE_VM_REMOTE_ROOT")
    if root:
        return root
    vm_user = os.environ.get("EWE_VM_USER", "dexter")
    repo_name = Path(__file__).resolve().parents[1].name
    return f"C:/Users/{vm_user}/{repo_name}"


@pytest.fixture
def sample_model_path():
    """Path to the sample EwE model included in the test fixtures."""
    if os.environ.get("EWE_LIVE_TESTS") == "1":
        if os.environ.get("EWE_MODEL_PATH"):
            return os.environ["EWE_MODEL_PATH"]
        return f"{_remote_root().rstrip('/\\\\')}\\tests\\fixtures\\Tampa_Bay.EwEmdb"
    return FIXTURE_MODEL


@pytest.fixture
def spatial_model_path():
    """Path to an EwE model with Ecospace scenarios (.eweaccdb format)."""
    if os.environ.get("EWE_LIVE_TESTS") == "1":
        return f"{_remote_root().rstrip('/\\\\')}\\tests\\fixtures\\Anchovy_Bay_Spatial_Mosaic.eweaccdb"
    return SPATIAL_MODEL

@pytest.fixture
def live_client():
    """Provides a connected synchronous client for live tests, skipping if disabled.
    Starts with a clean slate (model closed)."""
    if os.environ.get("EWE_LIVE_TESTS") != "1":
        pytest.skip("Skipping live tests")
    
    client = EwEMCPClient()
    assert client.connect()
    
    client.command_result("close_model")
    
    yield client
    
    client.disconnect()
