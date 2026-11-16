"""Tests for the FastEdit health gate (VAL-299).

Fast tests use monkey-patched env vars and mocks; the slow test is
marked ``@pytest.mark.slow`` and loads the real 1.7B MLX model.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from fasteditbench.backends import fastedit_health

# ── fast: missing path ────────────────────────────────────────────────


def test_health_check_missing_model_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nonexistent FASTEDIT_MODEL_PATH -> status=fail, error mentions 'path'."""
    monkeypatch.setenv(
        "FASTEDIT_MODEL_PATH", "/tmp/definitely-not-a-real-fastedit-model-path"
    )
    import asyncio

    health = asyncio.run(fastedit_health.check_fastedit_health())
    assert health["status"] == "fail"
    assert "path" in (health["error"] or "").lower()
    assert health["backend"] == "mlx"
    assert health["probe_output_len"] == 0


# ── fast: fail_fast raises when health returns fail ──────────────────


def test_fail_fast_raises_on_unhealthy() -> None:
    """fail_fast_if_unhealthy raises RuntimeError when the check returns fail."""
    import asyncio

    async def _fake_check() -> dict:
        return {
            "status": "fail",
            "error": "probe returned empty merged_code",
            "model_path": "/fake",
            "backend": "mlx",
            "probe_output_len": 0,
        }

    with patch.object(
        fastedit_health,
        "check_fastedit_health",
        AsyncMock(side_effect=_fake_check),
    ), pytest.raises(RuntimeError, match="FastEdit backend unhealthy"):
        asyncio.run(fastedit_health.fail_fast_if_unhealthy())


def test_fail_fast_returns_health_on_ok() -> None:
    """fail_fast_if_unhealthy returns the health dict on status=ok."""
    import asyncio

    async def _fake_check() -> dict:
        return {
            "status": "ok",
            "error": None,
            "model_path": "/fake",
            "backend": "mlx",
            "probe_output_len": 42,
        }

    with patch.object(
        fastedit_health,
        "check_fastedit_health",
        AsyncMock(side_effect=_fake_check),
    ):
        health = asyncio.run(fastedit_health.fail_fast_if_unhealthy())
        assert health["status"] == "ok"
        assert health["probe_output_len"] == 42


# ── slow: real model probe ────────────────────────────────────────────


@pytest.mark.slow
def test_health_check_happy_path() -> None:
    """Loads the real MLX 1.7B model and runs a trivial merge probe.

    Expected: status=ok, probe_output_len > 0. This is the authoritative
    VAL-299 gate — gated behind ``@pytest.mark.slow`` so fast CI skips it.
    """
    import asyncio

    health = asyncio.run(fastedit_health.check_fastedit_health())
    assert health["status"] == "ok", f"health check failed: {health}"
    assert health["probe_output_len"] > 0
    assert health["backend"] == "mlx"
    assert health["error"] is None
