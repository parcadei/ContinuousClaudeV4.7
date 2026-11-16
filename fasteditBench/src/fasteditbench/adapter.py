"""EditAdapter protocol — the only surface the benchmark harness calls into."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@dataclass
class AdapterResult:
    """The result of applying one edit.

    No timing fields — speed is explicitly out of scope for this benchmark
    (see DESIGN.md section 1 and section 4).

    Attributes:
        output_text: The adapter's final edited source code. A complete file /
            symbol body, NOT a diff. Empty string on failure.
        raw_response: Provider-specific raw payload (dict, SDK object, etc.).
            Stored for debugging and re-analysis without re-running the
            adapter. Must be JSON-serializable OR explicitly ``None``.
        tokens_generated: Number of output tokens the adapter's underlying
            model generated. ``None`` when the adapter is deterministic /
            doesn't invoke a model.
        error: Error message if the adapter failed. ``None`` on success.
            If ``error`` is set, ``output_text`` SHOULD be ``""``.
    """

    output_text: str = ""
    raw_response: Any = None
    tokens_generated: int | None = None
    error: str | None = None


@runtime_checkable
class EditAdapter(Protocol):
    """Every benchmark adapter implements this Protocol.

    ``apply`` is async so the harness can parallelize in the future without
    adapter-level changes. Synchronous adapters (e.g. FastEditAdapter wrapping
    a blocking Python API) wrap their sync call in ``asyncio.to_thread``.
    """

    async def apply(
        self,
        original_code: str,
        snippet: str,
        language: str,
        context: dict[str, Any],
    ) -> AdapterResult:
        """Apply the edit snippet to ``original_code``.

        Args:
            original_code: The full source BEFORE the edit.
            snippet: The edit snippet (context + new lines + optional markers).
            language: One of the 13 language slugs from DESIGN.md section 1.
            context: Extra per-case hints (e.g. ``{"case_id": "...",
                "pattern": "add_guard", "file_path": "/tmp/..."}``). Adapters
                may ignore or use as needed. MCP-mode target routing is
                signaled via ``target_kind`` ∈ {``'replace'``, ``'after'``,
                ``'none'``, ``'batch'``}. For ``'batch'``, ``target_batch``
                carries a list of per-edit dicts — see
                ``fasteditbench.backends.fastedit_adapter.FastEditAdapter.apply``
                for the schema.

        Returns:
            An ``AdapterResult``. Implementations MUST NOT raise — all
            failures surface via ``AdapterResult.error`` so the harness can
            continue the run and write one row per case.
        """
        ...


class NullAdapter:
    """Identity adapter used for unit tests and dry-run smoke checks.

    Returns ``original_code`` unchanged. Useful for exercising the harness
    plumbing (fixture loading, tier-1 / tier-2 checkers, diff metrics,
    JSONL writer) without invoking a real edit model.
    """

    name = "null"

    async def apply(
        self,
        original_code: str,
        snippet: str,
        language: str,
        context: dict[str, Any],
    ) -> AdapterResult:
        """Return ``original_code`` unchanged — a no-op / identity edit."""
        return AdapterResult(
            output_text=original_code,
            raw_response=None,
            tokens_generated=0,
            error=None,
        )
