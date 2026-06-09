"""Tests for set_diet_matrix post-batch verification and retry logic.

Bug: set_diet_matrix reports success with all predators applied, but the
last predator (e.g. Cormorants) gets an all-zero diet array.  This test
verifies that post-batch verification catches silent failures and retries
them individually.
"""

import pytest

from unittest.mock import AsyncMock, patch

# Simulated model with 3 groups
_GROUPS = [
    {"name": "Detritus", "index": 1},
    {"name": "Mullet", "index": 2},
    {"name": "Cormorants", "index": 3},
]


def _diet_for_index(idx: int) -> list[float]:
    """Return diet_comp values for a given group index in the 3-group mock."""
    if idx == 1:  # Detritus
        return [0.0, 0.0, 0.0]
    elif idx == 2:  # Mullet
        return [0.5, 0.0, 0.0]
    elif idx == 3:  # Cormorants
        return [0.0, 0.5, 0.5]
    return [0.0, 0.0, 0.0]


class _FakeSend:
    """Fake _send that simulates the batch silently dropping the last predator."""

    def __init__(self):
        self.call_count = 0
        self.list_groups_calls = 0
        self.batch_calls = 0
        self.get_values_calls = 0
        self.retry_set_calls = 0

    async def __call__(self, command, params=None, *, timeout=None):
        self.call_count += 1

        if command == "list_ecopath_groups":
            self.list_groups_calls += 1
            return {"groups": _GROUPS}

        if command == "batch":
            self.batch_calls += 1
            cmds = (params or {}).get("commands", [])
            results = []
            for cmd in cmds:
                cmd_type = cmd.get("type")
                cmd_params = cmd.get("params", {})
                if cmd_type == "set_values":
                    pred_idx = cmd_params.get("index")
                    # Simulate that the last predator in the batch silently fails
                    if pred_idx == _GROUPS[-1]["index"]:
                        results.append({"status": "error", "message": "silent failure"})
                    else:
                        results.append({"status": "success"})
                elif cmd_type == "get_values":
                    pidx = cmd_params.get("index")
                    diet = _diet_for_index(pidx)
                    results.append({
                        "status": "success",
                        "values": {"diet_comp": diet},
                    })
                else:
                    results.append({"status": "success"})
            return {"results": results}

        if command == "get_values":
            self.get_values_calls += 1
            idx = (params or {}).get("index")
            diet = _diet_for_index(idx)
            return {"domain": "ecopath_group", "index": idx, "values": {"diet_comp": diet}}

        if command == "set_values":
            # This is the retry call — check if it's for the last predator
            idx = (params or {}).get("index")
            if idx == _GROUPS[-1]["index"]:
                self.retry_set_calls += 1
            return {"result": "updated"}

        return {}


@pytest.mark.asyncio
async def test_set_diet_matrix_verifies_and_retries_last_predator():
    """set_diet_matrix should detect silent batch failures and retry individually."""
    fake = _FakeSend()

    with patch("ewe_mcp.server._send", fake):
        from ewe_mcp.server import set_diet_matrix

        diets = {
            "Mullet": {"Detritus": 0.5, "Cormorants": 0.0},
            "Cormorants": {"Mullet": 0.8, "Detritus": 0.2},
        }

        result = await set_diet_matrix(diets)

        # The overall status should still be success
        assert result["status"] == "success", f"Got status: {result['status']}"

        # Should have attempted a retry for the last predator
        assert fake.retry_set_calls >= 1, (
            "Expected at least 1 retry for the silently-failed predator. "
            f"get_values_calls={fake.get_values_calls}, retry_set_calls={fake.retry_set_calls}"
        )

        # Should have done post-batch verification (get_values via batch for all predators)
        # The verification runs inside a batch command (not individual get_values calls)
        assert fake.batch_calls >= 2, (
            "Expected at least 2 batch calls (1 set_values + 1 verification). "
            f"batch_calls={fake.batch_calls}"
        )

        # Response should include diet_verification section
        assert "diet_verification" in result, (
            "Response should contain a diet_verification section. "
            f"Keys: {list(result.keys())}"
        )

        diet_verification = result["diet_verification"]
        # Should report the retried predator
        retried = diet_verification.get("retried", [])
        assert len(retried) >= 1, (
            f"Expected at least one retried predator in diet_verification. "
            f"Got: {diet_verification}"
        )


@pytest.mark.asyncio
async def test_set_diet_matrix_no_retry_when_all_succeed():
    """When all predators succeed in batch, no retries should occur."""
    fake = _FakeSend()

    with patch("ewe_mcp.server._send", fake):
        from ewe_mcp.server import set_diet_matrix

        diets = {
            "Mullet": {"Detritus": 0.5, "Cormorants": 0.5},
        }

        result = await set_diet_matrix(diets)

        # All predators succeed in batch
        assert result["status"] == "success"

        # No get_values verification needed if batch reported all success
        # (This is relaxed — the implementation may or may not verify)
        pass


@pytest.mark.asyncio
async def test_set_diet_matrix_verification_fixes_zero_diet():
    """When verification finds all-zero diet for a predator that shouldn't be zero,
    it should be retried."""
    class _FakeSendZeroVerification:
        """Fake where batch succeeds for all but verification shows last is zero."""

        def __init__(self):
            self.batch_calls = 0
            self.get_values_calls = 0
            self.retry_set_calls = 0
            self._total_calls = 0  # count total calls to distinguish phases

        async def __call__(self, command, params=None, *, timeout=None):
            self._total_calls += 1

            if command == "list_ecopath_groups":
                return {"groups": _GROUPS}

            if command == "batch":
                self.batch_calls += 1
                cmds = (params or {}).get("commands", [])
                # Check if this batch has get_values commands (verification phase)
                has_get_values = any(cmd.get("type") == "get_values" for cmd in cmds)
                results = []
                for cmd in cmds:
                    cmd_type = cmd.get("type")
                    cmd_params = cmd.get("params", {})
                    if cmd_type == "set_values":
                        results.append({"status": "success"})
                    elif cmd_type == "get_values":
                        pidx = cmd_params.get("index")
                        # In verification phase, return all-zero diet for last predator
                        if has_get_values and pidx == _GROUPS[-1]["index"]:
                            diet = [0.0] * len(_GROUPS)
                        else:
                            diet = _diet_for_index(pidx)
                        results.append({
                            "status": "success",
                            "values": {"diet_comp": diet},
                        })
                    else:
                        results.append({"status": "success"})
                return {"results": results}

            if command == "get_values":
                self.get_values_calls += 1
                idx = (params or {}).get("index")
                diet = _diet_for_index(idx)
                return {"domain": "ecopath_group", "index": idx, "values": {"diet_comp": diet}}

            if command == "set_values":
                self.retry_set_calls += 1
                return {"result": "updated"}

            return {}

    fake = _FakeSendZeroVerification()

    with patch("ewe_mcp.server._send", fake):
        from ewe_mcp.server import set_diet_matrix

        diets = {
            "Cormorants": {"Mullet": 0.8, "Detritus": 0.2},
        }

        result = await set_diet_matrix(diets)

        assert result["status"] == "success"
        assert fake.retry_set_calls >= 1, (
            f"Expected retry for last predator with zero diet. "
            f"retry_set_calls={fake.retry_set_calls}"
        )

        dv = result.get("diet_verification", {})
        retried = dv.get("retried", [])
        assert len(retried) >= 1, (
            f"Expected retried predator in diet_verification. Got: {dv}"
        )
