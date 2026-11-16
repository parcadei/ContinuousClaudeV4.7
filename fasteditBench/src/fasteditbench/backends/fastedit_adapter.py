"""FastEdit benchmark adapter (VAL-300).

Invokes the *full* FastEdit harness (``chunked_merge``) rather than the
raw MLX merge model. Per user architectural direction (2026-04-22):

    "The model only works well within the full harness because ~70% of
    the edits go through the AST structure. The rest drop down to the
    model when needed."

Direct ``chunked_merge`` IS the harness — the AST-vs-model routing
happens inside it.

Adapter contract (see :mod:`fasteditbench.adapter`):

- ``apply`` NEVER raises. Failures surface via
  :attr:`AdapterResult.error`.
- ``AdapterResult`` has NO timing fields. FastEdit's
  ``ChunkedMergeResult.latency_ms`` is intentionally dropped.

Language handling:

- All 13 benchmark languages — including ``elixir`` — now route through
  ``chunked_merge``. FastEdit gained tree-sitter-elixir support in
  FASTEDIT-M5, so ``detect_language(".ex")`` returns ``"elixir"`` and
  the deterministic text-match / model fallback paths both work.
- Our slug ``csharp`` maps to FastEdit's internal ``c_sharp``; we use
  the file extension (``.cs``) and let ``chunked_merge`` see the
  canonical language via ``detect_language``. We still pass our benchmark
  slug as the ``language`` kwarg so FastEdit's own parse-validation uses
  the correct tree-sitter grammar.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path
from typing import Any

from fasteditbench.adapter import AdapterResult

__all__ = ["FastEditAdapter", "LANG_TO_EXT"]


# Mapping from benchmark language slug -> file extension FastEdit's
# ``detect_language`` understands. Cross-referenced against
# fastedit.data_gen.ast_analyzer.EXTENSION_TO_LANGUAGE (verified).
LANG_TO_EXT: dict[str, str] = {
    "python": ".py",
    "rust": ".rs",
    "go": ".go",
    "typescript": ".ts",
    "java": ".java",
    "kotlin": ".kt",
    "csharp": ".cs",
    "c": ".c",
    "cpp": ".cpp",
    "ruby": ".rb",
    "swift": ".swift",
    "php": ".php",
    "elixir": ".ex",
}

# FastEdit's canonical language slug for parse validation differs from
# ours for one language only: csharp -> c_sharp. We translate on the
# way INTO chunked_merge so FastEdit's ``validate_parse`` picks the
# right grammar.
_BENCH_TO_FASTEDIT_LANG: dict[str, str] = {"csharp": "c_sharp"}


class FastEditAdapter:
    """Bench adapter that routes every case through ``chunked_merge``.

    Construction is cheap — the ``ModelPool`` is created lazily on first
    ``apply()`` so unit tests that patch ``_get_pool`` can run without
    installing MLX weights.
    """

    name = "fastedit"

    def __init__(
        self,
        model_path: str | None = None,
        pool_size: int = 1,
    ) -> None:
        """Store configuration; defer model loading until first ``apply``.

        Args:
            model_path: Path to the MLX model dir. Defaults to the value
                of ``FASTEDIT_MODEL_PATH`` or FastEdit's bundled
                ``fastedit-1.7b-mlx-8bit`` checkpoint.
            pool_size: Number of MLX engines to load. Benchmarks run
                serially, so ``1`` is always correct for this harness.
        """
        if model_path is None:
            from fasteditbench.backends.fastedit_health import (
                DEFAULT_MODEL_PATH,
            )
            model_path = os.environ.get(
                "FASTEDIT_MODEL_PATH", DEFAULT_MODEL_PATH
            )
        self._model_path: str = model_path
        self._pool_size: int = pool_size
        self._pool: Any = None  # lazy ModelPool

    def _get_pool(self) -> Any:
        """Return the lazy ``ModelPool``, constructing it on first call."""
        if self._pool is None:
            from fastedit.mcp.server import ModelPool
            self._pool = ModelPool(
                model_path=self._model_path, size=self._pool_size
            )
        return self._pool

    async def apply(
        self,
        original_code: str,
        snippet: str,
        language: str,
        context: dict[str, Any],
    ) -> AdapterResult:
        """Run one benchmark case through FastEdit's chunked_merge.

        The call shape mirrors ``fastedit.mcp.tools_edit.fast_edit``'s
        MLX branch (tools_edit.py:54–77): write ``original_code`` to a
        NamedTemporaryFile with a language-appropriate suffix, acquire
        a pool engine, and call ``chunked_merge`` with
        ``engine.merge_auto`` as the merge function.

        Failure handling follows the adapter contract:

        - Unknown language slug (no extension mapping) → ``error`` set,
          no model load.
        - Any exception during merge → ``error`` captures
          ``type(exc).__name__``, ``output_text`` empty.
        - Parse invalid merged output → ``error`` =
          ``"parse_errors_in_merged_output"`` but ``output_text`` still
          populated (same convention fast_edit uses when it writes the
          file and warns the user).
        - All chunks rejected → ``error`` =
          ``"rejected_hallucinated_chunks"`` and ``output_text`` kept
          equal to ``original_code`` (FastEdit's own convention: the
          rejected merge is not written).

        MCP-mode target metadata (VAL-304): the caller supplies
        ``context['target_kind']`` and ``context['target_name']``. They
        map onto chunked_merge's ``after=`` / ``replace=`` kwargs:

        - ``target_kind='replace'`` → ``replace=target_name``
        - ``target_kind='after'``   → ``after=target_name``
        - ``target_kind='none'``    → both kwargs stay ``None`` (the
          legacy markers-only behaviour, kept as a safety valve for
          fixtures where no symbol target could be derived).
        - ``target_kind='batch'``   → route through
          ``fastedit.inference.chunked_merge.batch_chunked_merge`` with
          a list of :class:`BatchEdit` constructed from
          ``context['target_batch']``. Each element is a dict with
          ``kind`` (``'replace'`` | ``'after'``), ``name`` (target
          symbol), and ``snippet`` (the per-edit snippet). The top-level
          ``snippet`` argument to this method is IGNORED in batch mode
          because each edit carries its own snippet. This mirrors the
          real MCP invocation shape produced by ``fast_batch_edit``.
        """
        ext = LANG_TO_EXT.get(language)
        if ext is None:
            return AdapterResult(
                output_text="",
                raw_response=None,
                tokens_generated=None,
                error=f"no extension mapping for language '{language}'",
            )

        # Import at call time: keeps the module importable on hosts
        # without MLX (unit tests for the elixir short-circuit).
        try:
            from fastedit.inference.chunked_merge import (
                BatchEdit,
                batch_chunked_merge,
                chunked_merge,
            )
        except Exception as exc:  # noqa: BLE001 - surface, never raise
            return AdapterResult(
                output_text="",
                raw_response=None,
                tokens_generated=None,
                error=(
                    f"cannot import fastedit.inference.chunked_merge: "
                    f"{type(exc).__name__}: {exc}"
                ),
            )

        fastedit_lang = _BENCH_TO_FASTEDIT_LANG.get(language, language)

        # MCP-mode target routing (VAL-304 / M7). Unknown kinds fall
        # back to markers-only so a malformed fixture degrades to the
        # prior behaviour instead of raising.
        target_kind = context.get("target_kind")
        target_name = context.get("target_name")
        target_batch = context.get("target_batch")
        after_kwarg: str | None = None
        replace_kwarg: str | None = None
        batch_edits: list[Any] | None = None
        if target_kind == "replace" and target_name:
            replace_kwarg = target_name
        elif target_kind == "after" and target_name:
            after_kwarg = target_name
        elif target_kind == "batch":
            # Build BatchEdit list from the fixture's target_batch spec.
            # When target_kind='batch', the top-level `snippet` arg is
            # NOT used — each element carries its own per-edit snippet,
            # matching the real fast_batch_edit MCP invocation shape.
            if not isinstance(target_batch, list) or not target_batch:
                return AdapterResult(
                    output_text="",
                    raw_response=None,
                    tokens_generated=None,
                    error="target_kind='batch' requires a non-empty target_batch list",
                )
            try:
                batch_edits = [
                    BatchEdit(
                        snippet=e["snippet"],
                        after=(
                            e.get("name") if e.get("kind") == "after" else None
                        ),
                        replace=(
                            e.get("name") if e.get("kind") == "replace" else None
                        ),
                        # M12: per-entry preserve_siblings threads through to
                        # FastEdit's surgical-replace fast path (commit 333c651).
                        # Back-compat default False matches BatchEdit's dataclass
                        # default and MCP's fast_batch_edit schema (commit b621370).
                        preserve_siblings=bool(e.get("preserve_siblings", False)),
                    )
                    for e in target_batch
                ]
            except (KeyError, TypeError) as exc:
                return AdapterResult(
                    output_text="",
                    raw_response=None,
                    tokens_generated=None,
                    error=f"malformed target_batch entry: {type(exc).__name__}: {exc}",
                )

        # Write original_code to a tempfile so chunked_merge's AST path
        # (``get_ast_map(file_path, ...)``) sees a real file of the
        # right language. Mirror the MLX branch of
        # ``fastedit.mcp.tools_edit.fast_edit`` (tools_edit.py:54–77).
        pool = self._get_pool()

        try:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=ext, delete=False, encoding="utf-8"
            ) as tmp:
                tmp.write(original_code)
                tmp_path = tmp.name
        except Exception as exc:  # noqa: BLE001
            return AdapterResult(
                output_text="",
                raw_response=None,
                tokens_generated=None,
                error=f"tempfile write failed: {type(exc).__name__}: {exc}",
            )

        try:
            async with pool.acquire() as engine:
                # chunked_merge is synchronous; engine.merge_auto is
                # synchronous on MLX (see tools_edit.py:58-66). Running
                # it inline on the event loop blocks for the duration of
                # the merge — acceptable here because the benchmark
                # harness runs cases serially by design.
                if batch_edits is not None:
                    result = batch_chunked_merge(
                        original_code=original_code,
                        edits=batch_edits,
                        file_path=tmp_path,
                        merge_fn=engine.merge_auto,
                        language=fastedit_lang,
                        padding=30,
                    )
                else:
                    result = chunked_merge(
                        original_code=original_code,
                        snippet=snippet,
                        file_path=tmp_path,
                        merge_fn=engine.merge_auto,
                        language=fastedit_lang,
                        after=after_kwarg,
                        replace=replace_kwarg,
                    )
        except Exception as exc:  # noqa: BLE001 - contract forbids raising
            return AdapterResult(
                output_text="",
                raw_response=None,
                tokens_generated=None,
                error=f"{type(exc).__name__}: {exc}",
            )
        finally:
            with contextlib.suppress(OSError):
                Path(tmp_path).unlink(missing_ok=True)

        merged_code: str = getattr(result, "merged_code", "") or ""
        model_tokens = int(getattr(result, "model_tokens", 0) or 0)
        chunks_used = int(getattr(result, "chunks_used", 0) or 0)
        chunks_rejected = int(getattr(result, "chunks_rejected", 0) or 0)
        parse_valid = bool(getattr(result, "parse_valid", True))

        # Structured raw_response — JSON-serializable, no latency.
        raw_response = {
            "chunks_used": chunks_used,
            "chunks_rejected": chunks_rejected,
            "parse_valid": parse_valid,
            "model_tokens": model_tokens,
        }

        # All chunks rejected (or no chunks attempted): FastEdit's own
        # convention is to leave the file unchanged. Mirror that here
        # so downstream tier/diff metrics compare against the original.
        if chunks_used > 0 and chunks_rejected >= chunks_used:
            return AdapterResult(
                output_text=original_code,
                raw_response=raw_response,
                tokens_generated=model_tokens,
                error="rejected_hallucinated_chunks",
            )

        if not parse_valid:
            return AdapterResult(
                output_text=merged_code,
                raw_response=raw_response,
                tokens_generated=model_tokens,
                error="parse_errors_in_merged_output",
            )

        return AdapterResult(
            output_text=merged_code,
            raw_response=raw_response,
            tokens_generated=model_tokens,
            error=None,
        )

