import os

import pytest

from ewe_mcp.server import list_ui_commands, load_model

live = pytest.mark.skipif(
    os.environ.get("EWE_LIVE_TESTS") != "1",
    reason="Requires live EwE MCP plugin (set EWE_LIVE_TESTS=1 to run)",
)


# ── Live tests ─────────────────────────────────────────────────────


@live
@pytest.mark.asyncio
async def test_list_ui_commands_structure(sample_model_path):
    """list_ui_commands should return commands with all expected fields."""
    await load_model(sample_model_path)

    result = await list_ui_commands()

    assert "commands" in result
    assert "total_count" in result
    assert isinstance(result["commands"], list)
    assert result["total_count"] > 0

    first_cmd = result["commands"][0]
    for field in (
        "name",
        "description",
        "enabled",
        "checked",
        "is_available",
        "status",
        "is_invoking",
    ):
        assert field in first_cmd, f"Missing field {field!r} in command: {first_cmd}"


@live
@pytest.mark.asyncio
async def test_list_ui_commands_known_command(sample_model_path):
    """list_ui_commands should include commands with non-empty names."""
    await load_model(sample_model_path)

    result = await list_ui_commands()

    names = [cmd["name"] for cmd in result["commands"]]
    assert len(names) > 0
    assert any(name.strip() for name in names)
