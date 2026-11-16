"""FastEdit backend health gate (VAL-299).

Loads the MLX ``ModelPool`` from :mod:`fastedit.mcp.server`, acquires an
engine, and runs a trivial ``merge_auto`` probe. The probe verifies that
model weights load from disk and that inference returns a non-empty
result before the benchmark runner commits to a multi-hour sweep.

Design notes:

- The real ModelPool is built lazily — the first ``acquire()`` triggers
  weight loading (5–20 s for the 1.7B 8-bit MLX model).
- FastEdit's MLX engines are *synchronous* (``merge_auto`` is a plain
  method, not a coroutine). This mirrors ``fastedit.mcp.tools_edit``'s
  MLX branch which calls ``merge_auto`` directly under ``async with``.
- We keep the probe tiny (a one-line Python source) so failure modes
  almost always come from weight loading, not merge content.
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = [
    "DEFAULT_MODEL_PATH",
    "check_fastedit_health",
    "fail_fast_if_unhealthy",
]

# Resolve the FastEdit-default model path the same way
# ``fastedit.mcp.server`` does: <FastEdit repo>/models/fastedit-1.7b-mlx-8bit.
# We don't import FastEdit at module level — the import happens inside
# ``check_fastedit_health`` so tests can monkey-patch env vars and so
# environments without MLX still import this module cleanly.
DEFAULT_MODEL_PATH: str = (
    "/Users/cosimo/Desktop/FastEdit/models/fastedit-1.7b-mlx-8bit"
)


def _resolve_model_path() -> str:
    """Return the configured FastEdit model path.

    Precedence: ``FASTEDIT_MODEL_PATH`` env var > :data:`DEFAULT_MODEL_PATH`.
    """
    return os.environ.get("FASTEDIT_MODEL_PATH", DEFAULT_MODEL_PATH)


async def check_fastedit_health() -> dict:
    """Probe the MLX backend end-to-end; return a structured status dict.

    Steps:

    1. Resolve the model path from ``FASTEDIT_MODEL_PATH`` (or default).
    2. Verify the path exists on disk (fast fail with a clear error).
    3. Construct a ``ModelPool`` of size 1.
    4. Acquire an engine under ``async with pool.acquire()``.
    5. Run ``engine.merge_auto`` on a trivial Python source + empty-ish
       snippet. Confirm the returned ``merged_code`` is non-empty and
       does NOT start with ``"Error"``.

    Returns:
        A dict with the keys:

        - ``status`` — ``"ok"`` or ``"fail"``
        - ``model_path`` — the resolved path (string)
        - ``backend`` — always ``"mlx"`` for now
        - ``probe_output_len`` — length of the probe's merged output
          (``0`` on failure)
        - ``error`` — human-readable failure reason, or ``None``

    This function NEVER raises. Callers who want fail-fast semantics
    should use :func:`fail_fast_if_unhealthy`.
    """
    model_path = _resolve_model_path()
    result: dict = {
        "status": "fail",
        "model_path": model_path,
        "backend": "mlx",
        "probe_output_len": 0,
        "error": None,
    }

    if not Path(model_path).exists():
        result["error"] = f"model path does not exist: {model_path}"
        return result

    try:
        from fastedit.mcp.server import ModelPool
    except Exception as exc:  # pragma: no cover - import-time failure is rare
        result["error"] = (
            f"cannot import fastedit.mcp.server.ModelPool: "
            f"{type(exc).__name__}: {exc}"
        )
        return result

    try:
        pool = ModelPool(model_path=model_path, size=1)
        async with pool.acquire() as engine:
            # Tiny probe: merge an empty-edit snippet into a 1-line file.
            # merge_auto is synchronous on MLXEngine (matches the MCP
            # server's pattern at fastedit/mcp/tools_edit.py:58-66).
            probe_original = "x = 1\n"
            probe_snippet = "x = 1\n"
            merge = engine.merge_auto(probe_original, probe_snippet, "python")
            merged_code = getattr(merge, "merged_code", "") or ""
            result["probe_output_len"] = len(merged_code)
            if not merged_code:
                result["error"] = "probe returned empty merged_code"
                return result
            if merged_code.startswith("Error"):
                result["error"] = f"probe returned error string: {merged_code[:120]}"
                return result
    except Exception as exc:  # noqa: BLE001 - we intentionally capture all
        result["error"] = (
            f"ModelPool probe raised: {type(exc).__name__}: {exc}"
        )
        return result

    result["status"] = "ok"
    return result


async def fail_fast_if_unhealthy() -> dict:
    """Raise :class:`RuntimeError` if :func:`check_fastedit_health` fails.

    Returns the health dict on success (so callers can log ``probe_output_len``
    etc). On failure, raises with a message of the form:

        ``FastEdit backend unhealthy: <reason>``

    This is the gate every VAL-301 (full-run) entry point MUST pass before
    committing to a multi-minute sweep.
    """
    health = await check_fastedit_health()
    if health["status"] != "ok":
        raise RuntimeError(
            f"FastEdit backend unhealthy: {health.get('error') or 'unknown reason'}"
        )
    return health
