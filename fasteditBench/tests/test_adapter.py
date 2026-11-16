"""Tests for the EditAdapter Protocol, AdapterResult dataclass, and NullAdapter fake."""

from __future__ import annotations

import asyncio
import dataclasses

from fasteditbench.adapter import AdapterResult, EditAdapter, NullAdapter


def test_adapter_result_defaults() -> None:
    """Constructing AdapterResult() with no args uses the documented defaults."""
    result = AdapterResult()
    assert result.output_text == ""
    assert result.raw_response is None
    assert result.tokens_generated is None
    assert result.error is None


def test_adapter_result_fields() -> None:
    """All four fields round-trip through construction."""
    raw = {"choices": [{"text": "x = 1"}]}
    result = AdapterResult(
        output_text="x = 1\n",
        raw_response=raw,
        tokens_generated=42,
        error=None,
    )
    assert result.output_text == "x = 1\n"
    assert result.raw_response is raw
    assert result.tokens_generated == 42
    assert result.error is None

    err_result = AdapterResult(
        output_text="",
        raw_response=None,
        tokens_generated=None,
        error="boom",
    )
    assert err_result.error == "boom"
    assert err_result.output_text == ""


def test_adapter_result_no_timing_fields() -> None:
    """AdapterResult must have exactly the four documented fields — no timing fields."""
    names = {f.name for f in dataclasses.fields(AdapterResult)}
    assert names == {"output_text", "raw_response", "tokens_generated", "error"}


def test_null_adapter_satisfies_protocol() -> None:
    """NullAdapter satisfies the runtime-checkable EditAdapter protocol."""
    assert isinstance(NullAdapter(), EditAdapter)


def test_null_adapter_identity_apply() -> None:
    """NullAdapter.apply returns original_code unchanged, tokens_generated=0, no error."""

    async def _run() -> AdapterResult:
        return await NullAdapter().apply("x = 1\n", "# snippet", "python", {})

    result = asyncio.run(_run())
    assert result.output_text == "x = 1\n"
    assert result.tokens_generated == 0
    assert result.error is None


def test_bad_adapter_fails_protocol() -> None:
    """A class without an async `apply` method is NOT an EditAdapter."""

    class BadAdapter:
        name = "bad"

        def run(self) -> None:  # noqa: D401 - wrong method, not apply
            """This is not `apply`."""

    assert not isinstance(BadAdapter(), EditAdapter)
