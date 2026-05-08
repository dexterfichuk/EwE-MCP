import base64
import os
import tempfile

import pytest

from ewe_mcp.server import (
    capture_view,
    get_active_view,
    load_model,
    navigate_to_node,
    save_screenshot,
)

# Tests that require a running EwE plugin on the Windows VM.
live = pytest.mark.skipif(
    os.environ.get("EWE_LIVE_TESTS") != "1",
    reason="Requires live EwE MCP plugin (set EWE_LIVE_TESTS=1 to run)",
)




# ── Offline tests ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_capture_view_batch_blocked(sample_model_path):
    """capture_view should be blocked from batch commands."""
    from ewe_mcp.helpers import BATCH_BLOCKED_COMMANDS

    assert "capture_view" in BATCH_BLOCKED_COMMANDS


# ── Live tests ─────────────────────────────────────────────────────


@live
@pytest.mark.asyncio
async def test_capture_view_returns_image(sample_model_path):
    """Basic capture_view should return a PNG image with metadata."""
    await load_model(sample_model_path)

    result = await capture_view()

    # result is a list of [TextContent, ImageContent]
    assert len(result) == 2

    # First item is text metadata
    text_content = result[0]
    assert text_content.type == "text"
    assert "captured" in text_content.text

    # Second item is the PNG image
    image_content = result[1]
    assert image_content.type == "image"
    assert image_content.mimeType == "image/png"
    # Verify the data is valid base64 that decodes to a PNG
    image_bytes = base64.b64decode(image_content.data)
    assert image_bytes[:4] == b"\x89PNG"


@live
@pytest.mark.asyncio
async def test_capture_view_with_max_dimensions(sample_model_path):
    """capture_view with max_width/max_height should still return valid PNG."""
    await load_model(sample_model_path)

    result = await capture_view(max_width=640, max_height=480)

    assert len(result) == 2
    image_content = result[1]
    image_bytes = base64.b64decode(image_content.data)
    assert image_bytes[:4] == b"\x89PNG"


@live
@pytest.mark.asyncio
async def test_capture_view_targets(sample_model_path):
    """Different capture targets should all succeed."""
    await load_model(sample_model_path)

    for target in ("auto", "main", "active", "content"):
        result = await capture_view(target=target)
        assert len(result) == 2
        image_content = result[1]
        image_bytes = base64.b64decode(image_content.data)
        assert image_bytes[:4] == b"\x89PNG", f"Target {target!r} did not return valid PNG"


@live
@pytest.mark.asyncio
async def test_capture_view_after_navigation(sample_model_path):
    """Capture should reflect the currently navigated view."""
    await load_model(sample_model_path)

    # Navigate to a known screen
    await navigate_to_node(id="ndBasicInput")

    result = await capture_view()
    assert len(result) == 2

    # Metadata should include active_view with the expected node
    text_content = result[0]
    assert "ndBasicInput" in text_content.text


@live
@pytest.mark.asyncio
async def test_save_screenshot(sample_model_path):
    """save_screenshot should write a valid PNG to the local filesystem."""
    await load_model(sample_model_path)

    # Use a temporary file
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "test_screenshot.png")

        result = await save_screenshot(path=path)

        assert result["saved"] is True
        assert result["path"] == path
        assert result["bytes"] > 0
        assert os.path.exists(path)

        # Verify saved file is valid PNG
        with open(path, "rb") as f:
            header = f.read(4)
        assert header == b"\x89PNG"
