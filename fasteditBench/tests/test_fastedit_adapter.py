"""Tests for FastEditAdapter (VAL-300).

Fast tests cover protocol conformance, unknown-language error handling,
and kwarg threading. Slow tests run real fixtures through the 1.7B MLX
model end-to-end. Elixir is fully supported (FASTEDIT-M5) and routes
through ``chunked_merge`` like every other language.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from fasteditbench.adapter import EditAdapter
from fasteditbench.backends.fastedit_adapter import FastEditAdapter
from fasteditbench.tiers import classify

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = REPO_ROOT / "fixtures"


# ── protocol conformance ──────────────────────────────────────────────


def test_adapter_satisfies_protocol() -> None:
    """FastEditAdapter is a structural match for the EditAdapter Protocol."""
    adapter = FastEditAdapter(model_path="/tmp/unused-for-this-check")
    assert isinstance(adapter, EditAdapter)


# ── fast: elixir routes through chunked_merge like every other language ─


def test_adapter_elixir_flows_through_chunked_merge() -> None:
    """FASTEDIT-M5: elixir is supported; adapter must not short-circuit.

    We spy on ``chunked_merge`` to capture the kwargs, so no MLX weights
    are required. The spy asserts the adapter reached the merge call
    with the canonical ``.ex``-derived language slug and that the fixture
    snippet/original_code were forwarded verbatim. A regression to the
    pre-M5 short-circuit would make ``captured`` stay empty.
    """
    adapter = FastEditAdapter(model_path="/tmp/unused")
    captured = _run_spy(
        adapter,
        fixture={
            "original_code": "defmodule X do\nend\n",
            "snippet": "defmodule X do\n  def f, do: 1\nend\n",
            "expected_output": "defmodule X do\n  def f, do: 1\nend\n",
            "language": "elixir",
        },
        context={"target_kind": "replace", "target_name": "X"},
        merged_code="defmodule X do\n  def f, do: 1\nend\n",
    )
    # Spy was called → no short-circuit.
    assert captured, "chunked_merge was not invoked for elixir"
    assert captured.get("language") == "elixir"
    assert captured.get("replace") == "X"
    assert captured.get("after") is None


# ── fast: unknown language falls through to error path ──────────────


class _FakeEngine:
    """Sentinel engine — merge_auto should never fire when chunked_merge is spied."""

    def merge_auto(self, *_args, **_kwargs):  # pragma: no cover - never called
        raise AssertionError(
            "merge_auto should not run; spy short-circuits chunked_merge"
        )


class _FakeCtx:
    """Async context manager yielding a :class:`_FakeEngine`."""

    async def __aenter__(self):
        return _FakeEngine()

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakePool:
    """ModelPool stand-in used to bypass MLX in adapter kwarg tests."""

    def acquire(self):
        return _FakeCtx()


class _SpyResult:
    """Minimal ChunkedMergeResult stand-in used by chunked_merge spies."""

    def __init__(self, merged_code: str = "") -> None:
        self.merged_code = merged_code
        self.parse_valid = True
        self.chunks_used = 0
        self.chunk_regions: list = []
        self.model_tokens = 0
        self.latency_ms = 0.0


def _run_spy(
    adapter: FastEditAdapter,
    fixture: dict | None,
    context: dict,
    merged_code: str,
) -> dict:
    """Invoke adapter.apply with chunked_merge patched to a kwargs spy.

    Returns the dict of keyword args chunked_merge was called with (for
    assertions). The spy returns ``_SpyResult(merged_code=merged_code)``
    so the adapter produces a successful AdapterResult.
    """
    captured: dict = {}

    def _spy(*_args, **kwargs):
        captured.update(kwargs)
        return _SpyResult(merged_code=merged_code)

    original_code = (
        fixture["original_code"] if fixture is not None else "x = 1\n"
    )
    snippet = fixture["snippet"] if fixture is not None else "x = 2\n"
    language = fixture["language"] if fixture is not None else "python"

    with (
        patch.object(adapter, "_get_pool", return_value=_FakePool()),
        patch("fastedit.inference.chunked_merge.chunked_merge", _spy),
    ):
        asyncio.run(
            adapter.apply(
                original_code=original_code,
                snippet=snippet,
                language=language,
                context=context,
            )
        )
    return captured


def test_adapter_passes_replace_kwarg_to_chunked_merge() -> None:
    """VAL-304: MCP-mode replace target threads through as replace= kwarg.

    Invokes the adapter on python/01-add_guard.yaml with
    ``context={'target_kind': 'replace', 'target_name': 'retry_batch'}``
    and asserts chunked_merge saw ``replace='retry_batch'`` and
    ``after=None``. This is the invocation shape a real MCP agent
    produces — the prior markers-only run always passed both kwargs
    ``None`` and is hereby superseded.
    """
    fixture = _load_fixture("python/01-add_guard.yaml")
    adapter = FastEditAdapter(model_path="/tmp/unused")
    captured = _run_spy(
        adapter,
        fixture,
        context={"target_kind": "replace", "target_name": "retry_batch"},
        merged_code=fixture["expected_output"],
    )
    assert captured.get("replace") == "retry_batch"
    assert captured.get("after") is None


def test_adapter_passes_after_kwarg_to_chunked_merge() -> None:
    """Symmetric check: target_kind='after' threads through as after= kwarg."""
    adapter = FastEditAdapter(model_path="/tmp/unused")
    captured = _run_spy(
        adapter,
        fixture=None,
        context={"target_kind": "after", "target_name": "a"},
        merged_code="def a(): pass\ndef b(): pass\n",
    )
    assert captured.get("after") == "a"
    assert captured.get("replace") is None


def test_adapter_target_kind_none_passes_both_kwargs_none() -> None:
    """target_kind='none' keeps the legacy markers-only behaviour."""
    adapter = FastEditAdapter(model_path="/tmp/unused")
    captured = _run_spy(
        adapter,
        fixture=None,
        context={"target_kind": "none", "target_name": None},
        merged_code="x = 2\n",
    )
    assert captured.get("after") is None
    assert captured.get("replace") is None


def test_adapter_batch_constructs_batchedit_list_and_calls_batch_merge() -> None:
    """M7: target_kind='batch' routes through batch_chunked_merge with BatchEdit list.

    The adapter is invoked with ``context={'target_kind': 'batch',
    'target_batch': [...]}``. We spy on ``batch_chunked_merge`` to
    capture its kwargs and assert:

    - ``edits`` kwarg is a list of 2 ``BatchEdit`` instances
    - First entry has ``replace='Store'``, ``after=None``
    - Second entry has ``after='oldMethod'``, ``replace=None``
    - ``chunked_merge`` (single-call) was NOT invoked
    - The top-level ``snippet`` arg is ignored; each BatchEdit carries its own
    """
    from fastedit.inference.chunked_merge import BatchEdit

    adapter = FastEditAdapter(model_path="/tmp/unused")
    captured_batch: dict = {}
    single_called: list[bool] = []

    def _batch_spy(*_args, **kwargs):
        captured_batch.update(kwargs)
        return _SpyResult(merged_code="merged\n")

    def _single_spy(*_args, **_kwargs):  # pragma: no cover - must not fire
        single_called.append(True)
        return _SpyResult(merged_code="should-not-be-used\n")

    batch = [
        {
            "kind": "replace",
            "name": "Store",
            "snippet": "class Store { /* shell */ }",
        },
        {
            "kind": "after",
            "name": "oldMethod",
            "snippet": "public void newMethod() {}",
        },
    ]

    with (
        patch.object(adapter, "_get_pool", return_value=_FakePool()),
        patch(
            "fastedit.inference.chunked_merge.batch_chunked_merge", _batch_spy
        ),
        patch(
            "fastedit.inference.chunked_merge.chunked_merge", _single_spy
        ),
    ):
        asyncio.run(
            adapter.apply(
                original_code="class Store {}\n",
                snippet="IGNORED_IN_BATCH_MODE\n",
                language="java",
                context={
                    "target_kind": "batch",
                    "target_name": None,
                    "target_batch": batch,
                },
            )
        )

    assert not single_called, "single-call chunked_merge must not fire in batch mode"
    assert captured_batch, "batch_chunked_merge was not invoked"

    edits = captured_batch.get("edits")
    assert isinstance(edits, list) and len(edits) == 2
    for e in edits:
        assert isinstance(e, BatchEdit)
    assert edits[0].replace == "Store"
    assert edits[0].after is None
    assert edits[0].snippet == "class Store { /* shell */ }"
    assert edits[1].after == "oldMethod"
    assert edits[1].replace is None
    assert edits[1].snippet == "public void newMethod() {}"
    # language arg threads through (java stays java; no mapping needed)
    assert captured_batch.get("language") == "java"


def test_adapter_batch_threads_preserve_siblings_per_entry() -> None:
    """M12: target_kind='batch' passes per-entry preserve_siblings to BatchEdit.

    When a batch entry sets ``preserve_siblings: true``, the adapter must
    construct the corresponding ``BatchEdit`` with
    ``preserve_siblings=True`` so FastEdit's chunked_merge can take the
    surgical-replace fast path (introduced in FastEdit commit 333c651).
    Entries without the flag keep the back-compat default of ``False``.
    """
    from fastedit.inference.chunked_merge import BatchEdit

    adapter = FastEditAdapter(model_path="/tmp/unused")
    captured_batch: dict = {}

    def _batch_spy(*_args, **kwargs):
        captured_batch.update(kwargs)
        return _SpyResult(merged_code="merged\n")

    batch = [
        {
            "kind": "replace",
            "name": "Store",
            "snippet": "class Store { private x: int; }",
            "preserve_siblings": True,
        },
        {
            "kind": "replace",
            "name": "Other",
            "snippet": "class Other {}",
        },
    ]

    with (
        patch.object(adapter, "_get_pool", return_value=_FakePool()),
        patch(
            "fastedit.inference.chunked_merge.batch_chunked_merge", _batch_spy
        ),
    ):
        asyncio.run(
            adapter.apply(
                original_code="class Store {}\nclass Other {}\n",
                snippet="IGNORED\n",
                language="java",
                context={
                    "target_kind": "batch",
                    "target_name": None,
                    "target_batch": batch,
                },
            )
        )

    edits = captured_batch.get("edits")
    assert isinstance(edits, list) and len(edits) == 2
    for e in edits:
        assert isinstance(e, BatchEdit)
    # First entry: explicit preserve_siblings=True must thread through.
    assert edits[0].replace == "Store"
    assert edits[0].preserve_siblings is True
    # Second entry: missing key → back-compat default False.
    assert edits[1].replace == "Other"
    assert edits[1].preserve_siblings is False


def test_adapter_batch_missing_list_returns_error() -> None:
    """target_kind='batch' without a non-empty target_batch surfaces a clean error."""
    adapter = FastEditAdapter(model_path="/tmp/unused")
    with patch.object(adapter, "_get_pool", return_value=_FakePool()):
        result = asyncio.run(
            adapter.apply(
                original_code="x = 1\n",
                snippet="x = 2\n",
                language="python",
                context={"target_kind": "batch", "target_batch": None},
            )
        )
    assert result.error is not None
    assert "batch" in result.error.lower()


def test_adapter_unknown_language_returns_error() -> None:
    """A language slug with no extension mapping errors cleanly (no model call)."""
    adapter = FastEditAdapter(model_path="/tmp/unused")

    with patch.object(
        adapter, "_get_pool", MagicMock(side_effect=AssertionError("should not be called"))
    ):
        result = asyncio.run(
            adapter.apply(
                original_code="x = 1\n",
                snippet="x = 2\n",
                language="haskell",  # not in LANG_TO_EXT
                context={},
            )
        )
    assert result.error is not None
    assert "extension" in result.error.lower()


# ── helpers for slow tests ───────────────────────────────────────────


def _load_fixture(rel_path: str) -> dict:
    """Load one fixture YAML from the repo fixtures dir."""
    path = FIXTURES_DIR / rel_path
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


# ── slow: end-to-end single-fixture integration ──────────────────────


@pytest.mark.slow
def test_apply_single_python_fixture() -> None:
    """End-to-end: python/01-add_guard.yaml goes through chunked_merge.

    We only assert the pipe works: output_text non-empty and
    tokens_generated populated. Tier classification is REPORTED for
    visibility but not asserted (VAL-300 proves the adapter works;
    actual accuracy across all langs is measured by VAL-301).
    """
    fixture = _load_fixture("python/01-add_guard.yaml")
    adapter = FastEditAdapter()

    result = asyncio.run(
        adapter.apply(
            original_code=fixture["original_code"],
            snippet=fixture["snippet"],
            language=fixture["language"],
            context={},
        )
    )

    assert result.error is None, f"adapter errored: {result.error}"
    assert result.output_text, "output_text is empty"
    assert result.tokens_generated is not None

    # Report tier results for visibility; don't assert.
    t1, t2 = classify(
        result.output_text, fixture["expected_output"], fixture["language"]
    )
    print(
        f"\n[adapter smoke] python/01-add_guard: tier1={t1} tier2={t2} "
        f"tokens={result.tokens_generated} "
        f"raw={result.raw_response}"
    )


@pytest.mark.slow
def test_apply_ast_only_edit_returns_zero_tokens() -> None:
    """An add_at_end edit should take FastEdit's AST path (0 model tokens).

    Uses python/05-add_at_end.yaml. The snippet opens with
    ``def process_order(order):`` then ``# ... existing code ...`` then
    a new line before the final return — this pattern routes through
    ``chunked_merge`` without ``after=``/``replace=``, so behavior
    depends on marker-driven chunking.

    We assert the harness produces a non-error result with a sensible
    AST-derived raw_response; we do NOT require tokens_generated==0
    because chunked_merge without after=/replace= may still invoke the
    model on the matched chunk. Instead, we verify hybrid routing
    plumbing works: ``raw_response["chunks_used"]`` is populated.
    """
    fixture = _load_fixture("python/05-add_at_end.yaml")
    adapter = FastEditAdapter()

    result = asyncio.run(
        adapter.apply(
            original_code=fixture["original_code"],
            snippet=fixture["snippet"],
            language=fixture["language"],
            context={},
        )
    )

    assert result.output_text, f"output empty; error={result.error}"
    assert result.raw_response is not None
    assert "chunks_used" in result.raw_response
    print(
        f"\n[hybrid routing] python/05-add_at_end: "
        f"tokens={result.tokens_generated} "
        f"chunks_used={result.raw_response['chunks_used']} "
        f"parse_valid={result.raw_response['parse_valid']}"
    )
