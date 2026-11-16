"""Tests for ``scripts/compute_tax_per_operation_v5.py``.

v5 layers three additional minimizations on top of v4:

1. Short unicode marker (``…`` — 1 token) in place of the long
   ``# ... existing code ...`` (5 tokens).
2. Echo-anchor stripping — lines in the snippet that repeat content
   from the original body can be dropped under FastEdit 0.2.4
   marker-position semantics.
3. Inline-edit reclassification — fixtures authored as ``target_kind:
   none`` that are class-method workarounds become ``replace=<method>``
   under FastEdit 0.2.3's class-method partial support.

These tests verify each mechanism and the end-to-end progression
v3 → v4 → v5 is monotonic (within spec).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import tiktoken

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "scripts"))
sys.path.insert(0, str(_REPO_ROOT / "src"))

import compute_tax_per_operation_v5 as v5  # noqa: E402

# ---------------------------------------------------------------------------
# 1. Minimal payload for the walkthrough fixture (retry_batch)
# ---------------------------------------------------------------------------


def test_minimal_snippet_for_add_guard() -> None:
    """``python/01-add_guard`` — the walkthrough fixture — must minimize
    to ``<new_lines>\\n…`` under 0.2.5's top-insert semantics.

    Expected shape:
        "    if not records:\\n"
        "        return []\\n"
        "    …\\n"
    """
    snippet = (
        "def retry_batch(client, records, max_attempts=3):\n"
        "    if not records:\n"
        "        return []\n"
        "    attempt = 0\n"
        "    # ... existing code ...\n"
    )
    original = (
        "def retry_batch(client, records, max_attempts=3):\n"
        "    attempt = 0\n"
        "    while attempt < max_attempts:\n"
        "        try:\n"
        "            return client.send(records)\n"
        "        except TransientError:\n"
        "            attempt += 1\n"
        '    raise RuntimeError("exhausted retries")\n'
    )
    payload, effective, reclassified, form = v5.build_fastedit_payload_v5(
        operation_category="modify_inside",
        file_path="python/01-add_guard.py",
        snippet=snippet,
        original_code=original,
        target_kind="replace",
        target_name="retry_batch",
    )
    assert reclassified is False
    assert form == "modify_inside:top_insert"
    # The signature line is gone (v4 strip).
    assert "def retry_batch" not in effective
    # The anchor line "attempt = 0" is gone (v5 anchor strip).
    assert "attempt = 0" not in effective
    # The new lines are present.
    assert "if not records:" in effective
    assert "return []" in effective
    # The short unicode marker replaced the long one.
    assert v5.SHORT_MARKER in effective
    assert "# ... existing code ..." not in effective
    # Exact shape check.
    assert effective == (
        "    if not records:\n"
        "        return []\n"
        "    …\n"
    )


# ---------------------------------------------------------------------------
# 2. Unicode marker tokenization
# ---------------------------------------------------------------------------


def test_unicode_marker_tokenizes_to_one_token() -> None:
    """The unicode ellipsis ``…`` is a single ``o200k_base`` token, vs
    five for ``# ... existing code ...`` — the foundational saving that
    v5 exploits."""
    enc = tiktoken.get_encoding("o200k_base")
    assert len(enc.encode(v5.SHORT_MARKER)) == 1
    assert len(enc.encode("# ... existing code ...")) == 5
    assert len(enc.encode("// ... existing code ...")) == 5


# ---------------------------------------------------------------------------
# 3. Class-method reauthor detection
# ---------------------------------------------------------------------------


def test_class_method_reauthor_detects_class_shell() -> None:
    """``python/07-class_method`` is authored as a class-shell +
    body-level marker. Under 0.2.3 it can be reauthored as
    ``replace=__init__`` (the inner method being edited)."""
    snippet = (
        "class SessionStore:\n"
        "    def __init__(self, ttl=3600):\n"
        "        self.ttl = ttl\n"
        "        self._data = OrderedDict()\n"
        "    # ... existing code ...\n"
    )
    original = (
        "class SessionStore:\n"
        "    def __init__(self, ttl=3600):\n"
        "        self.ttl = ttl\n"
        "        self._data = {}\n"
        "\n"
        "    def get(self, key):\n"
        "        return self._data.get(key)\n"
    )
    is_cand, name, reason = v5.detect_inline_reauthor_candidate(
        snippet, original
    )
    assert is_cand is True
    assert name == "__init__"
    assert reason == "class_shell_method_edit"


def test_class_method_reauthor_detects_bare_method() -> None:
    """A Ruby-style ``def foo``-first snippet for an inline_edit fixture
    is a bare_method_edit — 0.2.3 scopes replace=foo correctly."""
    snippet = (
        "def total(items)\n"
        "  items.sum\n"
        "end\n"
    )
    original = (
        "class Calculator\n"
        "  def total(items)\n"
        "    acc = 0\n"
        "    items.each do |i|\n"
        "      acc += i\n"
        "    end\n"
        "    acc\n"
        "  end\n"
        "end\n"
    )
    is_cand, name, reason = v5.detect_inline_reauthor_candidate(
        snippet, original
    )
    assert is_cand is True
    assert name == "total"
    assert reason == "bare_method_edit"


def test_class_method_reauthor_rejects_pure_inline() -> None:
    """A genuine pure-inline fixture (no function/class scope — just a
    top-level literal) is NOT a reauthor candidate."""
    snippet = (
        'static const char *const KNOWN_EXTENSIONS[] = {\n'
        '    ".c",\n'
        '    ".h",\n'
        '    ".cpp",\n'
        '    ".rs",\n'
        '    ".go",\n'
        '    NULL,\n'
        '};\n'
    )
    original = (
        'static const char *const KNOWN_EXTENSIONS[] = {\n'
        '    ".c",\n'
        '    ".h",\n'
        '    ".cpp",\n'
        '    NULL,\n'
        '};\n'
    )
    is_cand, _name, reason = v5.detect_inline_reauthor_candidate(
        snippet, original
    )
    assert is_cand is False
    assert reason == "not_a_candidate"


# ---------------------------------------------------------------------------
# 4. v3 / v4 / v5 progression is monotonic
# ---------------------------------------------------------------------------


def test_v3_v4_v5_progression_monotonic() -> None:
    """For EVERY fixture, v3 ≥ v4 ≥ v5 FastEdit token counts.

    Small regressions can happen when trivial-line detection chooses to
    KEEP a `}` that v4 would have dropped — that's a correctness win.
    We tolerate a handful of such fixtures but the aggregate must still
    be monotonic.
    """
    rows = v5.compute_rows_v5(_REPO_ROOT / "fixtures")

    v3_total = sum(r["v3_fastedit_tokens"] for r in rows)
    v4_total = sum(r["v4_fastedit_tokens"] for r in rows)
    v5_total = sum(r["v5_fastedit_tokens"] for r in rows)

    # Aggregate monotonicity — v5 ≤ v4 ≤ v3 across the whole fixture set.
    assert v5_total <= v4_total, (
        f"v5 aggregate ({v5_total}) should not exceed v4 ({v4_total})"
    )
    assert v4_total <= v3_total, (
        f"v4 aggregate ({v4_total}) should not exceed v3 ({v3_total})"
    )

    # Per-fixture check — at least 90% of fixtures satisfy v5 ≤ v4.
    # (Some fixtures regress by 1-3 tokens when trivial-line keeping
    # preserves a `}` that v4 dropped — correctness tradeoff.)
    ok_v5_vs_v4 = sum(
        1 for r in rows if r["v5_fastedit_tokens"] <= r["v4_fastedit_tokens"]
    )
    assert ok_v5_vs_v4 / len(rows) >= 0.90, (
        f"only {ok_v5_vs_v4}/{len(rows)} fixtures respect v5 ≤ v4"
    )


# ---------------------------------------------------------------------------
# 5. Savings envelope by category
# ---------------------------------------------------------------------------


def test_savings_envelope_by_category() -> None:
    """Per-category savings envelopes for v5.

    Expected (with 0.2.5's real floors — the JSON wrapper sets a hard
    per-fixture lower bound on small files):

    * ``delete`` ≥ 75%    — pure symbol-name payload.
    * ``headline`` ≥ 45%  — the composite excl whole_rewrite.

    ``modify_inside`` and ``inline_edit`` are correlated with file size
    (the JSON wrapper is fixed cost). Aggregate is ~48% across the
    bench-02 fixture mix.
    """
    rows = v5.compute_rows_v5(_REPO_ROOT / "fixtures")
    summary = v5.summarize_v5(rows, run_id="test-envelope")

    cats = summary["operation_categories"]
    assert cats["delete"]["savings_pct"] >= 75.0

    headline = summary["headline_excluding_whole_rewrite"]
    assert headline["savings_pct"] >= 45.0


# ---------------------------------------------------------------------------
# 6. End-to-end — summary shape + methodology tag
# ---------------------------------------------------------------------------


def test_summary_has_version_progression() -> None:
    """Summary includes version_progression block with v3/v4/v5 totals
    so the HTML report can render the five-release journey."""
    rows = v5.compute_rows_v5(_REPO_ROOT / "fixtures")
    summary = v5.summarize_v5(rows, run_id="test-prog")
    assert summary["methodology"] == v5.METHODOLOGY_TAG
    prog = summary["version_progression"]
    assert set(prog.keys()) >= {"v3", "v4", "v5", "native_total_headline"}
    for k in ("v3", "v4", "v5"):
        assert "fastedit_total_headline" in prog[k]
        assert "savings_pct_headline" in prog[k]
    # Progression strictly improves (v3 → v4 → v5).
    assert (
        prog["v3"]["fastedit_total_headline"]
        >= prog["v4"]["fastedit_total_headline"]
    )
    assert (
        prog["v4"]["fastedit_total_headline"]
        >= prog["v5"]["fastedit_total_headline"]
    )


def test_row_has_v5_fields() -> None:
    """Per-row v5 fields needed by the HTML report are present."""
    rows = v5.compute_rows_v5(_REPO_ROOT / "fixtures")
    assert rows
    for r in rows:
        assert "v3_fastedit_tokens" in r
        assert "v4_fastedit_tokens" in r
        assert "v5_fastedit_tokens" in r
        assert "v5_reclassified" in r
        assert "v5_minimal_payload_form" in r
        assert "effective_category" in r


# ---------------------------------------------------------------------------
# 7. Sanity check on specific high-signal fixtures
# ---------------------------------------------------------------------------


def test_retry_batch_v5_strictly_cheaper_than_v4() -> None:
    rows = v5.compute_rows_v5(_REPO_ROOT / "fixtures")
    [r] = [r for r in rows if r["case_id"] == "python/01-add_guard"]
    assert r["v5_fastedit_tokens"] < r["v4_fastedit_tokens"]
    assert r["v4_fastedit_tokens"] < r["v3_fastedit_tokens"]


def test_delete_fixtures_unchanged_across_v3_v4_v5() -> None:
    """delete fixtures: FastEdit's fast_delete payload is already
    minimal — v3 == v4 == v5 token count."""
    rows = v5.compute_rows_v5(_REPO_ROOT / "fixtures")
    del_rows = [r for r in rows if r["operation_category"] == "delete"]
    assert del_rows
    for r in del_rows:
        assert r["v3_fastedit_tokens"] == r["v4_fastedit_tokens"]
        assert r["v4_fastedit_tokens"] == r["v5_fastedit_tokens"]


def test_jsonl_writer_round_trips(tmp_path: Path) -> None:
    """run_tax_per_operation_v5 writes valid JSONL + summary JSON."""
    jsonl_path, summary_path, summary = v5.run_tax_per_operation_v5(
        _REPO_ROOT / "fixtures",
        tmp_path,
        run_id="roundtrip-v5",
    )
    assert jsonl_path.exists()
    assert summary_path.exists()
    with jsonl_path.open() as fh:
        lines = [json.loads(line) for line in fh if line.strip()]
    assert len(lines) == summary["n_fixtures"] == 156
    with summary_path.open() as fh:
        stored = json.load(fh)
    assert stored["run_id"] == "roundtrip-v5"
    assert stored["methodology"] == v5.METHODOLOGY_TAG
