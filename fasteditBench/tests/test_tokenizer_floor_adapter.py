"""Tests for the token-floor adapter (VAL-200 + VAL-201).

VAL-200 unit coverage:

* Payload extraction: single-hunk, two-hunk, identical-input-raises.
* Tokenizer backends: GPT offline path; Opus missing-key fallback to
  tiktoken cl100k_base proxy.
* Wrapper overhead is measured once per enabled model at init.
* Unknown model ids raise ``ValueError``.

VAL-201 integration coverage:

* Feed ``fixtures/python/01-add_guard.yaml`` through the adapter for
  both models; assert ``edit_payload_tokens > 0`` and an order-of-
  magnitude sanity check against the classic ``len(old + new) / 4``
  bytes-per-token heuristic (0.5×–3× envelope).

No real Anthropic API calls are made: the Opus branch is always exercised
via the missing-key fallback so the test suite stays hermetic.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from fasteditbench.backends.tokenizer_floor_adapter import (
    FloorResult,
    TokenizerFloorAdapter,
    _serialize_tool_call,
    extract_edit_payloads,
)

_GPT = "openai/gpt-5.4"
_OPUS = "anthropic/claude-opus-4.7"

_FIXTURE_01 = (
    Path(__file__).parent.parent / "fixtures" / "python" / "01-add_guard.yaml"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _adapter_hermetic(monkeypatch) -> TokenizerFloorAdapter:
    """Build an adapter with the Opus fallback path forced on.

    We unconditionally clear ``ANTHROPIC_API_KEY`` so Opus uses
    ``tiktoken:cl100k_base`` — no network calls, fully deterministic.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    return TokenizerFloorAdapter(enable_opus=True, enable_gpt=True)


# ---------------------------------------------------------------------------
# Payload extraction
# ---------------------------------------------------------------------------


def test_payload_extraction_single_hunk():
    """A single inserted line produces exactly one anchored hunk."""
    original = "def f():\n    return 1\n"
    expected = "def f():\n    x = 0\n    return 1\n"

    hunks = extract_edit_payloads(original, expected)
    assert len(hunks) == 1
    old, new = hunks[0]
    # Anchored on the trailing `return 1` line.
    assert old == "    return 1\n"
    assert new == "    x = 0\n    return 1\n"


def test_payload_extraction_two_hunks():
    """Two non-adjacent inserts yield two separate hunks."""
    original = "line1\nline2\nline3\nline4\nline5\n"
    expected = "line1\nNEW_A\nline2\nline3\nline4\nNEW_B\nline5\n"

    hunks = extract_edit_payloads(original, expected)
    assert len(hunks) == 2, f"expected 2 hunks, got {hunks}"

    # First hunk: insert 'NEW_A' before line2 -> anchored on line2.
    old0, new0 = hunks[0]
    assert "NEW_A" in new0
    assert "line2" in old0 and "line2" in new0
    assert "NEW_A" not in old0

    # Second hunk: insert 'NEW_B' before line5 -> anchored on line5.
    old1, new1 = hunks[1]
    assert "NEW_B" in new1
    assert "line5" in old1 and "line5" in new1
    assert "NEW_B" not in old1


def test_payload_extraction_identical_raises():
    """Identical original/expected is a fixture bug — surface loudly."""
    text = "def f(): return 1\n"
    with pytest.raises(ValueError, match="identical fixture"):
        extract_edit_payloads(text, text)


def test_payload_extraction_replace_hunk():
    """A pure replace hunk needs no anchor — old_block is already non-empty."""
    original = "a\nOLD\nb\n"
    expected = "a\nNEW\nb\n"
    hunks = extract_edit_payloads(original, expected)
    assert len(hunks) == 1
    old, new = hunks[0]
    assert old == "OLD\n"
    assert new == "NEW\n"


def test_payload_extraction_trailing_insert_uses_prev_anchor():
    """Insert at end of file anchors on the preceding line."""
    original = "a\nb\n"
    expected = "a\nb\nc\n"
    hunks = extract_edit_payloads(original, expected)
    assert len(hunks) == 1
    old, new = hunks[0]
    # The trailing-insert branch grafts the preceding equal line onto both
    # old and new, so `b` appears in both and `c` only in new.
    assert "b" in old
    assert "c" in new
    assert old != new


# ---------------------------------------------------------------------------
# Tokenizer backends
# ---------------------------------------------------------------------------


def test_gpt_offline_tokenization(monkeypatch):
    """GPT path is fully offline via tiktoken o200k_base."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    adapter = TokenizerFloorAdapter(enable_opus=False, enable_gpt=True)

    result = adapter.count_floor(
        original_code="def f(): return 1\n",
        expected_output="def f():\n    x = 0\n    return 1\n",
        file_path="a.py",
        model_id=_GPT,
    )

    assert isinstance(result, FloorResult)
    assert result.tokenizer_used == "tiktoken:o200k_base"
    assert result.tokenizer_proxy is False
    assert result.edit_payload_tokens > 0
    assert result.payload_bytes > 0
    assert result.n_hunks >= 1
    # Wrapper overhead of the empty-args JSON is a small positive integer.
    assert result.wrapper_overhead > 0
    assert result.wrapper_overhead < 100  # sanity: nowhere near full-file


def test_opus_fallback_when_key_missing(monkeypatch):
    """Missing ANTHROPIC_API_KEY → cl100k_base proxy with the flag set."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    adapter = TokenizerFloorAdapter(enable_opus=True, enable_gpt=False)

    result = adapter.count_floor(
        original_code="def f(): return 1\n",
        expected_output="def f():\n    x = 0\n    return 1\n",
        file_path="a.py",
        model_id=_OPUS,
    )

    assert result.tokenizer_used == "tiktoken:cl100k_base"
    assert result.tokenizer_proxy is True
    assert result.edit_payload_tokens > 0


def test_wrapper_overhead_measured_once(monkeypatch):
    """Both enabled models get their wrapper overhead cached at init."""
    adapter = _adapter_hermetic(monkeypatch)
    assert _GPT in adapter._wrapper_overhead_by_model
    assert _OPUS in adapter._wrapper_overhead_by_model

    # Deterministic: repeated lookups return the same value.
    before = dict(adapter._wrapper_overhead_by_model)
    _ = adapter.count_floor(
        original_code="a\n",
        expected_output="a\nb\n",
        file_path="x.py",
        model_id=_GPT,
    )
    after = dict(adapter._wrapper_overhead_by_model)
    assert before == after


def test_unknown_model_raises(monkeypatch):
    """An id outside the supported set raises ValueError."""
    adapter = _adapter_hermetic(monkeypatch)
    with pytest.raises(ValueError, match="unknown model_id"):
        adapter.count_floor(
            original_code="a\n",
            expected_output="a\nb\n",
            file_path="x.py",
            model_id="foo/bar",
        )


def test_disabled_model_raises(monkeypatch):
    """A supported-but-disabled id raises ValueError too."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    adapter = TokenizerFloorAdapter(enable_opus=False, enable_gpt=True)
    with pytest.raises(ValueError, match="not enabled"):
        adapter.count_floor(
            original_code="a\n",
            expected_output="a\nb\n",
            file_path="x.py",
            model_id=_OPUS,
        )


def test_both_disabled_raises():
    """An adapter with no backends is pointless — fail fast."""
    with pytest.raises(ValueError, match="at least one"):
        TokenizerFloorAdapter(enable_opus=False, enable_gpt=False)


def test_serialize_is_whitespace_free():
    """Wire-shape sanity: no whitespace between JSON tokens."""
    s = _serialize_tool_call("a.py", "old", "new")
    assert " " not in s
    assert "\n" not in s
    assert s.startswith('{"name":"edit","arguments":')


# ---------------------------------------------------------------------------
# VAL-201: integration test on fixture python/01-add_guard.yaml
# ---------------------------------------------------------------------------


def test_integration_fixture_01_add_guard(monkeypatch, capsys):
    """Full integration: load fixture 01, run through both backends.

    Hermetic: we clear ``ANTHROPIC_API_KEY`` so the Opus path uses its
    cl100k_base proxy — we are NOT exercising the live API here.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    with _FIXTURE_01.open("r", encoding="utf-8") as fh:
        fixture = yaml.safe_load(fh)

    original = fixture["original_code"]
    expected = fixture["expected_output"]

    # Derive the payload once for audit — the integration test records
    # what was extracted so a human can eyeball it before M3 runs on 156.
    hunks = extract_edit_payloads(original, expected)
    audit_old, audit_new = hunks[0]
    print(f"\n[VAL-201 audit] n_hunks={len(hunks)}")
    print(f"[VAL-201 audit] old_string[0]={audit_old!r}")
    print(f"[VAL-201 audit] new_string[0]={audit_new!r}")

    adapter = TokenizerFloorAdapter(enable_opus=True, enable_gpt=True)

    for model_id, expected_label in (
        (_GPT, "tiktoken:o200k_base"),
        (_OPUS, "tiktoken:cl100k_base"),  # proxy, since key is unset
    ):
        result = adapter.count_floor(
            original_code=original,
            expected_output=expected,
            file_path="python/01-add_guard.py",
            model_id=model_id,
        )

        assert result.edit_payload_tokens > 0, (
            f"{model_id}: floor must be positive"
        )
        assert result.tokenizer_used == expected_label
        assert result.n_hunks == len(hunks)
        assert result.payload_bytes > 0

        # Order-of-magnitude sanity: tokens ≈ bytes / 4. Envelope 0.5×–3×.
        byte_heuristic_tokens = result.payload_bytes / 4
        lower = 0.5 * byte_heuristic_tokens
        upper = 3.0 * byte_heuristic_tokens
        assert lower <= result.edit_payload_tokens <= upper, (
            f"{model_id}: tokens {result.edit_payload_tokens} outside "
            f"[{lower:.1f}, {upper:.1f}] for {result.payload_bytes} bytes"
        )

    # Surface the audit print when -s is passed; otherwise pytest captures.
    _ = capsys.readouterr()
