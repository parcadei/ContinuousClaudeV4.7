"""Tests for ``scripts/compute_tax_per_operation_v4.py``.

v4 adds one behaviour over v3: for fixtures routed through FastEdit's
``replace=<name>`` keyword, the authored snippet's first line is
stripped when it looks like the function signature for ``<name>``. The
justification is FastEdit 0.2.2's AST-level auto-preserve of that
signature.

These tests cover:

* Signature detection across Python, Rust, Go, Java, TypeScript, Ruby,
  Elixir, Swift, Kotlin, PHP, C, C++, C# — the 13 languages in the
  fixture set.
* Non-signature first lines (decorators, comments, imports) do NOT
  trigger stripping.
* ``insert_after`` / ``delete`` / ``inline_edit`` fixtures are never
  stripped regardless of first-line content.
* The end-to-end compute loop produces ``signature_stripped=True`` on
  the walkthrough fixture (``python/01-add_guard``) and the savings
  percentage jumps above the v3 floor on that fixture.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# The scripts are under ``scripts/`` (not a package) — load via path.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "scripts"))
sys.path.insert(0, str(_REPO_ROOT / "src"))

import compute_tax_per_operation as ctpo  # noqa: E402
import compute_tax_per_operation_v4 as v4  # noqa: E402


# ---------------------------------------------------------------------------
# Signature detection
# ---------------------------------------------------------------------------


def test_signature_stripping_python() -> None:
    """``def retry_batch(...)`` on line 1 is detected + stripped."""
    snippet = (
        "def retry_batch(client, records, max_attempts=3):\n"
        "    if not records:\n"
        "        return []\n"
        "    attempt = 0\n"
        "    # ... existing code ...\n"
    )
    stripped, did = v4.maybe_strip_signature(
        operation_category="modify_inside",
        target_kind="replace",
        target_name="retry_batch",
        snippet=snippet,
    )
    assert did is True
    # The signature line is gone.
    assert "def retry_batch" not in stripped
    # The body is preserved verbatim, starting from line 2.
    assert stripped.startswith("    if not records:\n")
    assert "# ... existing code ..." in stripped


def test_signature_stripping_rust() -> None:
    """``fn divide(...)`` on line 1 of a Rust snippet is stripped."""
    snippet = (
        "fn divide(a: f64, b: f64) -> f64 {\n"
        "    if b == 0.0 {\n"
        "        return f64::NAN;\n"
        "    }\n"
        "    a / b\n"
        "}\n"
    )
    stripped, did = v4.maybe_strip_signature(
        operation_category="whole_rewrite",
        target_kind="replace",
        target_name="divide",
        snippet=snippet,
    )
    assert did is True
    assert "fn divide" not in stripped
    assert stripped.startswith("    if b == 0.0 {\n")


def test_signature_stripping_go() -> None:
    snippet = (
        "func Average(nums []float64) float64 {\n"
        "    if len(nums) == 0 {\n"
        "        return 0.0\n"
        "    }\n"
        "    total := 0.0\n"
        "    // ... existing code ...\n"
        "}\n"
    )
    stripped, did = v4.maybe_strip_signature(
        operation_category="modify_inside",
        target_kind="replace",
        target_name="Average",
        snippet=snippet,
    )
    assert did is True
    assert "func Average" not in stripped


def test_signature_stripping_typescript() -> None:
    snippet = (
        "export function getDisplayName(user: User): string {\n"
        "  if (user == null) {\n"
        '    return "Anonymous";\n'
        "  }\n"
        '  return user.firstName + " " + user.lastName;\n'
        "}\n"
    )
    stripped, did = v4.maybe_strip_signature(
        operation_category="whole_rewrite",
        target_kind="replace",
        target_name="getDisplayName",
        snippet=snippet,
    )
    assert did is True
    assert "getDisplayName" not in stripped


def test_signature_stripping_java() -> None:
    snippet = (
        "public double average(java.util.List<Integer> nums) {\n"
        "    if (nums == null || nums.isEmpty()) {\n"
        "        return 0.0;\n"
        "    }\n"
        "    int total = 0;\n"
        "    // ... existing code ...\n"
        "}\n"
    )
    stripped, did = v4.maybe_strip_signature(
        operation_category="modify_inside",
        target_kind="replace",
        target_name="average",
        snippet=snippet,
    )
    assert did is True
    assert "public double average" not in stripped


def test_signature_stripping_ruby_without_parens() -> None:
    """Ruby's ``def foo`` with no parens still has to be recognised."""
    snippet = (
        "def average\n"
        "  return 0.0 if nums.empty?\n"
        "  total = nums.sum\n"
        "  # ... existing code ...\n"
        "end\n"
    )
    stripped, did = v4.maybe_strip_signature(
        operation_category="modify_inside",
        target_kind="replace",
        target_name="average",
        snippet=snippet,
    )
    assert did is True
    assert "def average" not in stripped


def test_signature_stripping_elixir() -> None:
    snippet = (
        "def average(nums) do\n"
        "  if Enum.empty?(nums), do: 0.0\n"
        "  total = Enum.sum(nums)\n"
        "  # ... existing code ...\n"
        "end\n"
    )
    stripped, did = v4.maybe_strip_signature(
        operation_category="modify_inside",
        target_kind="replace",
        target_name="average",
        snippet=snippet,
    )
    assert did is True


def test_signature_stripping_swift() -> None:
    snippet = (
        "func average(_ nums: [Double]) -> Double {\n"
        "    guard !nums.isEmpty else {\n"
        "        return 0.0\n"
        "    }\n"
        "    let total = nums.reduce(0, +)\n"
        "    // ... existing code ...\n"
        "}\n"
    )
    stripped, did = v4.maybe_strip_signature(
        operation_category="modify_inside",
        target_kind="replace",
        target_name="average",
        snippet=snippet,
    )
    assert did is True


def test_signature_stripping_kotlin() -> None:
    snippet = (
        "fun average(nums: List<Int>): Double {\n"
        "    if (nums.isEmpty()) {\n"
        "        return 0.0\n"
        "    }\n"
        "    val total = nums.sum()\n"
        "    // ... existing code ...\n"
        "}\n"
    )
    stripped, did = v4.maybe_strip_signature(
        operation_category="modify_inside",
        target_kind="replace",
        target_name="average",
        snippet=snippet,
    )
    assert did is True


def test_signature_stripping_php() -> None:
    snippet = (
        "public function average(array $nums): float\n"
        "{\n"
        "    if (empty($nums)) {\n"
        "        return 0.0;\n"
        "    }\n"
        "    $total = array_sum($nums);\n"
        "    // ... existing code ...\n"
        "}\n"
    )
    stripped, did = v4.maybe_strip_signature(
        operation_category="modify_inside",
        target_kind="replace",
        target_name="average",
        snippet=snippet,
    )
    assert did is True


def test_signature_stripping_c() -> None:
    snippet = (
        "size_t safe_strlen(const char *s) {\n"
        "    if (s == NULL) {\n"
        "        return 0;\n"
        "    }\n"
        "    // ... existing code ...\n"
        "}\n"
    )
    stripped, did = v4.maybe_strip_signature(
        operation_category="modify_inside",
        target_kind="replace",
        target_name="safe_strlen",
        snippet=snippet,
    )
    assert did is True


def test_signature_stripping_cpp() -> None:
    snippet = (
        "double average(const std::vector<double>& nums) {\n"
        "    if (nums.empty()) {\n"
        "        return 0.0;\n"
        "    }\n"
        "    // ... existing code ...\n"
        "}\n"
    )
    stripped, did = v4.maybe_strip_signature(
        operation_category="modify_inside",
        target_kind="replace",
        target_name="average",
        snippet=snippet,
    )
    assert did is True


def test_signature_stripping_csharp() -> None:
    snippet = (
        "public string GetName(int userId)\n"
        "{\n"
        "    if (!_users.ContainsKey(userId))\n"
        "    {\n"
        '        return string.Empty;\n'
        "    }\n"
        "    // ... existing code ...\n"
        "}\n"
    )
    stripped, did = v4.maybe_strip_signature(
        operation_category="modify_inside",
        target_kind="replace",
        target_name="GetName",
        snippet=snippet,
    )
    assert did is True


# ---------------------------------------------------------------------------
# Categories that must NEVER strip
# ---------------------------------------------------------------------------


def test_no_strip_for_after_category() -> None:
    """``insert_after`` fixtures use ``after=``, not ``replace=`` — the
    0.2.2 auto-preserve behaviour doesn't apply, so the snippet must
    stay intact."""
    snippet = "def new_helper():\n    return 1\n"
    stripped, did = v4.maybe_strip_signature(
        operation_category="insert_after",
        target_kind="after",
        target_name="existing_fn",
        snippet=snippet,
    )
    assert did is False
    assert stripped == snippet


def test_no_strip_for_delete_category() -> None:
    """``delete`` fixtures send only the symbol name — snippet is
    irrelevant and must not be touched."""
    snippet = "def goner():\n    pass\n"
    stripped, did = v4.maybe_strip_signature(
        operation_category="delete",
        target_kind="replace",
        target_name="goner",
        snippet=snippet,
    )
    assert did is False
    assert stripped == snippet


def test_no_strip_for_inline_edit_category() -> None:
    """``inline_edit`` (target_kind=none) doesn't route through
    ``replace=`` either."""
    snippet = "class Foo:\n    value = 1\n"
    stripped, did = v4.maybe_strip_signature(
        operation_category="inline_edit",
        target_kind="none",
        target_name=None,
        snippet=snippet,
    )
    assert did is False
    assert stripped == snippet


# ---------------------------------------------------------------------------
# First-line shape rejections
# ---------------------------------------------------------------------------


def test_no_strip_when_first_line_is_decorator() -> None:
    """Swift/Python decorator-style first line: signature is on line 2,
    so the heuristic must leave the snippet alone."""
    snippet = (
        "@MainActor\n"
        "func refreshUI(_ text: String) {\n"
        "    // ... existing code ...\n"
        "}\n"
    )
    stripped, did = v4.maybe_strip_signature(
        operation_category="modify_inside",
        target_kind="replace",
        target_name="refreshUI",
        snippet=snippet,
    )
    assert did is False
    assert stripped == snippet


def test_no_strip_when_first_line_is_comment() -> None:
    snippet = (
        "# top-level note\n"
        "def average(nums):\n"
        "    return sum(nums) / len(nums)\n"
    )
    stripped, did = v4.maybe_strip_signature(
        operation_category="modify_inside",
        target_kind="replace",
        target_name="average",
        snippet=snippet,
    )
    assert did is False
    assert stripped == snippet


def test_no_strip_when_first_line_is_import() -> None:
    """An ``import`` first line can't possibly be a signature — even if
    it happens to contain the target name token."""
    snippet = (
        "from mylib import average\n"
        "def average(nums):\n"
        "    return sum(nums) / len(nums)\n"
    )
    stripped, did = v4.maybe_strip_signature(
        operation_category="modify_inside",
        target_kind="replace",
        target_name="average",
        snippet=snippet,
    )
    # First line contains ``average`` and no parens, and doesn't start
    # with a signature keyword ``(def|fn|func|function|fun)`` — rejected.
    assert did is False
    assert stripped == snippet


def test_no_strip_when_first_line_doesnt_mention_target() -> None:
    """First line doesn't contain the target name at all."""
    snippet = (
        "// note\n"
        "function foo() {\n"
        "    return 1;\n"
        "}\n"
    )
    stripped, did = v4.maybe_strip_signature(
        operation_category="modify_inside",
        target_kind="replace",
        target_name="foo",
        snippet=snippet,
    )
    assert did is False


def test_no_strip_when_target_name_empty() -> None:
    snippet = "def anything():\n    pass\n"
    stripped, did = v4.maybe_strip_signature(
        operation_category="modify_inside",
        target_kind="replace",
        target_name=None,
        snippet=snippet,
    )
    assert did is False


# ---------------------------------------------------------------------------
# Payload construction
# ---------------------------------------------------------------------------


def test_v4_payload_contains_stripped_snippet() -> None:
    """The JSON payload produced for a modify_inside fixture does not
    include the signature line — only the body + marker."""
    snippet = (
        "def retry_batch(client, records, max_attempts=3):\n"
        "    if not records:\n"
        "        return []\n"
        "    attempt = 0\n"
        "    # ... existing code ...\n"
    )
    payload, eff, stripped = v4.build_fastedit_payload_v4(
        operation_category="modify_inside",
        file_path="python/01-add_guard.py",
        snippet=snippet,
        target_kind="replace",
        target_name="retry_batch",
    )
    assert stripped is True
    obj = json.loads(payload)
    assert obj["name"] == "fast_edit"
    assert obj["arguments"]["replace"] == "retry_batch"
    assert "def retry_batch" not in obj["arguments"]["edit_snippet"]
    assert "# ... existing code ..." in obj["arguments"]["edit_snippet"]


def test_v4_insert_after_payload_unchanged_from_v3() -> None:
    """``insert_after`` serializes identically to v3 — no strip."""
    snippet = "def new_fn():\n    return 1\n"
    v3_payload = ctpo.build_fastedit_payload(
        operation_category="insert_after",
        file_path="foo.py",
        snippet=snippet,
        target_kind="after",
        target_name="existing_fn",
    )
    v4_payload, _, stripped = v4.build_fastedit_payload_v4(
        operation_category="insert_after",
        file_path="foo.py",
        snippet=snippet,
        target_kind="after",
        target_name="existing_fn",
    )
    assert stripped is False
    assert v3_payload == v4_payload


def test_v4_delete_payload_unchanged_from_v3() -> None:
    v3_payload = ctpo.build_fastedit_payload(
        operation_category="delete",
        file_path="foo.go",
        snippet="func Run() error {}",
        target_kind="replace",
        target_name="Run",
    )
    v4_payload, _, stripped = v4.build_fastedit_payload_v4(
        operation_category="delete",
        file_path="foo.go",
        snippet="func Run() error {}",
        target_kind="replace",
        target_name="Run",
    )
    assert stripped is False
    assert v3_payload == v4_payload


# ---------------------------------------------------------------------------
# End-to-end smoke
# ---------------------------------------------------------------------------


def test_end_to_end_retry_batch_signature_stripped() -> None:
    """``python/01-add_guard`` — the walkthrough fixture — must have
    ``signature_stripped=True`` and strictly lower FastEdit tokens than
    the v3 measurement for the same fixture."""
    v4_rows = v4.compute_rows_v4(_REPO_ROOT / "fixtures")
    v3_rows = ctpo.compute_rows(_REPO_ROOT / "fixtures")

    [r4] = [r for r in v4_rows if r["case_id"] == "python/01-add_guard"]
    [r3] = [r for r in v3_rows if r["case_id"] == "python/01-add_guard"]

    assert r4["signature_stripped"] is True
    assert r4["operation_category"] == "modify_inside"
    # Native side unchanged across versions.
    assert r4["native_edit_tokens"] == r3["native_edit_tokens"]
    # FastEdit side strictly smaller in v4.
    assert r4["fastedit_tokens"] < r3["fastedit_tokens"]
    # And snippet-before > snippet-after on a stripped fixture.
    assert (
        r4["snippet_before_strip_tokens"]
        > r4["snippet_after_strip_tokens"]
    )


def test_end_to_end_swift_decorator_not_stripped() -> None:
    """``swift/07-add-decorator-mainactor`` has a decorator on line 1 —
    stripping would corrupt the edit. Must stay intact."""
    rows = v4.compute_rows_v4(_REPO_ROOT / "fixtures")
    hits = [
        r for r in rows if r["case_id"] == "swift/07-add-decorator-mainactor"
    ]
    assert hits, "expected fixture missing"
    [row] = hits
    assert row["signature_stripped"] is False


def test_end_to_end_delete_fixtures_not_stripped() -> None:
    """None of the three ``delete`` fixtures should be reported as
    stripped."""
    rows = v4.compute_rows_v4(_REPO_ROOT / "fixtures")
    del_rows = [r for r in rows if r["operation_category"] == "delete"]
    assert del_rows, "expected at least one delete fixture"
    for r in del_rows:
        assert r["signature_stripped"] is False


def test_end_to_end_summary_has_strip_stats() -> None:
    rows = v4.compute_rows_v4(_REPO_ROOT / "fixtures")
    summary = v4.summarize_v4(rows, run_id="test-run")
    assert summary["methodology"] == v4.METHODOLOGY_TAG
    strip_stats = summary["signature_strip_stats"]
    assert strip_stats["total_fixtures"] == len(rows)
    # At least a few of the 156 fixtures must have been stripped.
    assert strip_stats["total_stripped"] >= 10
    # Every language appears in the per-language map.
    assert set(strip_stats["per_language"].keys()) == {
        r["language"] for r in rows
    }
