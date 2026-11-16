"""Compute the calling-agent *tax* per operation-category (bench-02 v5).

Successor to ``compute_tax_per_operation_v4.py``. The v5 measurement
reflects FastEdit 0.2.5's capability surface — after five stacked
patches shipped 2026-04-22/23:

* 0.2.1 — stale-AST fix on chained edits (benchmark now runs reliably)
* 0.2.2 — signature auto-preserve (v4 stripped the signature)
* 0.2.3 — class-method partial replace works deterministically
* 0.2.4 — marker-position semantics + short marker forms (``…``)
* 0.2.5 — wrap_block + flow-token guards (add-guard patterns are now
  deterministic via the short unicode ``…`` marker)

The calling agent can now emit TRULY minimal payloads. Measurement rules
per category:

modify_inside  (target_kind=replace, has_marker=true)
    * strip signature (v4 behaviour retained — 0.2.2)
    * strip "echo" anchor lines whose content appears verbatim in the
      original body, based on marker position (0.2.4 marker-position
      semantics let us drop them)
    * swap long ``# ... existing code ...`` marker for unicode ``…``
      (0.2.4 short marker form; 1 token vs 5)

whole_rewrite  (target_kind=replace, no marker)
    * strip signature (v4). No marker needed.

insert_after
    * unchanged — snippet is already minimal.

delete
    * unchanged — only the symbol name on the wire.

inline_edit  (target_kind=none)
    * Reclassify if snippet structure matches a class-method workaround
      or a standalone method body; emit ``replace=<name>`` + minimal
      body + ``…``. 0.2.3 makes this reliable for class methods.
    * Otherwise: leave payload as v4 but swap long marker for ``…``.

batch_edit  (target_kind=batch)
    * Apply the above rules per entry in target_batch.

Native-edit side is UNCHANGED from v3/v4 — ±2 line anchor context.

Outputs
-------

* ``results/tax_per_operation/{run_id}.jsonl``
* ``results/tax_per_operation/{run_id}.summary.json``

Per-row additions over v4:

* ``v5_minimal_payload_form: str`` — short classification tag describing
  which minimization path the fixture used (e.g. ``strip_sig+top_insert``,
  ``strip_sig+bottom_insert``, ``strip_sig+middle``, ``whole_rewrite``,
  ``reauthored_class_method``, ``inline_marker_swap``, ``delete``,
  ``insert_after``, ``batch_composed``, ``unchanged_from_v4``).
* ``v5_reclassified: bool`` — true when the fixture's operation_category
  changed vs the original categorization (inline_edit → modify_inside).
* ``v5_fastedit_tokens: int`` — new count.
* ``v5_savings_pct: float``, ``v5_savings_tokens: int``.
* For convenience: ``v3_fastedit_tokens`` and ``v4_fastedit_tokens`` too,
  so the HTML can render the progression inline.

Summary-level ``methodology: "v5_minimal_payloads_fastedit_0_2_5"``.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

import tiktoken

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

import compute_tax_per_operation as v3  # noqa: E402
import compute_tax_per_operation_v4 as v4_mod  # noqa: E402

from fasteditbench.floor_runner import synthesize_file_path  # noqa: E402
from fasteditbench.runner import load_fixtures  # noqa: E402

logger = logging.getLogger(__name__)

__all__ = [
    "METHODOLOGY_TAG",
    "SHORT_MARKER",
    "build_fastedit_payload_v5",
    "compute_rows_v5",
    "detect_inline_reauthor_candidate",
    "minimize_modify_inside_snippet",
    "run_tax_per_operation_v5",
    "summarize_v5",
]

_ENCODING_NAME = "o200k_base"

METHODOLOGY_TAG = "v5_minimal_payloads_fastedit_0_2_5"

# 0.2.4 short marker — unicode ellipsis. Tokenizes to 1 token in o200k_base.
SHORT_MARKER = "…"

# All long marker substrings we know about. Stripping precedes insertion
# of the short marker.
_LONG_MARKERS = (
    "# ... existing code ...",
    "// ... existing code ...",
    "/* ... existing code ... */",
    "-- ... existing code ...",
    "<!-- ... existing code ... -->",
)


# ---------------------------------------------------------------------------
# Snippet line analysis
# ---------------------------------------------------------------------------


def _split_body_lines(snippet: str) -> list[str]:
    """Split a snippet into lines, keepends, with empty trailing line
    removed so ``"a\\n".splitlines(keepends=True)`` gives ``['a\\n']``."""
    return snippet.splitlines(keepends=True)


def _line_marker_kind(line: str) -> str | None:
    """Return a short tag if ``line`` (stripped of leading whitespace + newline)
    is a long FastEdit preservation marker, else None."""
    stripped = line.strip()
    for marker in _LONG_MARKERS:
        if stripped == marker:
            return "long"
    return None


def _normalize_for_compare(line: str) -> str:
    """Collapse whitespace for anchor comparison."""
    return re.sub(r"\s+", " ", line).strip()


# "Trivial" close-punctuation lines that appear in almost every file —
# treating them as anchors leads to misclassification (an if-block's
# closing ``}`` gets matched to the function's outer ``}``). These stay
# with the "new" content they belong to so minimization keeps them.
_TRIVIAL_LINES = frozenset({"}", ")", "]", "};", ");", "end", "end;"})


def _is_anchor_line(line: str, original_body_norm: frozenset[str]) -> bool:
    """Return True if ``line`` (stripped) appears in ``original_body_norm``.

    Empty lines, marker lines, and "trivial" close-punctuation lines are
    never anchors — they carry no distinguishing content.
    """
    stripped = _normalize_for_compare(line)
    if not stripped:
        return False
    if stripped in _TRIVIAL_LINES:
        return False
    if _line_marker_kind(line):
        return False
    return stripped in original_body_norm


# ---------------------------------------------------------------------------
# modify_inside minimization
# ---------------------------------------------------------------------------


def _short_marker_with_indent(line: str) -> str:
    """Return a short unicode marker preserving the indent of ``line``
    (the original long-marker line), with a trailing newline."""
    m = re.match(r"([ \t]*)", line)
    indent = m.group(1) if m else ""
    return f"{indent}{SHORT_MARKER}\n"


def minimize_modify_inside_snippet(
    snippet_no_sig: str,
    original_body: str,
) -> tuple[str, str]:
    """Reduce ``snippet_no_sig`` (signature already stripped) to minimal form.

    Approach:

    1. Find the long-marker line.
    2. Partition non-marker lines into ``before_marker`` and ``after_marker``.
    3. Classify each partition line as "new" (not in original body) or
       "anchor" (appears in original body).
    4. Apply marker-position semantics:

       * If all before-marker lines are new and all after-marker lines are
         anchors → **top_insert**: emit ``new_lines + short_marker``.
       * If all before-marker lines are anchors and all after-marker lines
         are new → **bottom_insert**: emit ``short_marker + new_lines``.
       * Otherwise (middle): keep one anchor on each side of the marker
         when the snippet actually had anchors on both sides; drop
         redundant echoes.

    The function returns ``(minimized_snippet, form_tag)`` where
    ``form_tag`` is one of ``top_insert`` / ``bottom_insert`` /
    ``middle`` / ``no_marker_found``.

    If no marker is present or minimization can't be done safely, returns
    ``(snippet_no_sig, "no_marker_found")`` — caller keeps v4 output.
    """
    lines = _split_body_lines(snippet_no_sig)
    if not lines:
        return snippet_no_sig, "no_marker_found"

    marker_idx = -1
    for i, line in enumerate(lines):
        if _line_marker_kind(line):
            marker_idx = i
            break
    if marker_idx == -1:
        return snippet_no_sig, "no_marker_found"

    if snippet_no_sig.count("# ... existing code ...") + \
            snippet_no_sig.count("// ... existing code ...") + \
            snippet_no_sig.count("/* ... existing code ... */") + \
            snippet_no_sig.count("-- ... existing code ...") + \
            snippet_no_sig.count("<!-- ... existing code ... -->") > 1:
        # two_markers pattern — conservative: keep v4 form (swap marker)
        return snippet_no_sig, "no_marker_found"

    before = lines[:marker_idx]
    marker_line = lines[marker_idx]
    after = lines[marker_idx + 1:]

    # Normalize original body lines for anchor lookup.
    original_lines_norm = frozenset(
        _normalize_for_compare(line)
        for line in original_body.splitlines()
        if line.strip()
    )

    def classify(region: list[str]) -> list[str]:
        """Return one of 'new' / 'anchor' / 'blank' / 'trivial' per line.

        * ``blank`` — empty / whitespace-only
        * ``trivial`` — lone close punctuation (``}`` / ``)`` / ``end``)
          that can't be distinguished from its many siblings in the
          original; treat as neutral (kept in place, not used to pick
          top/bottom/middle semantics)
        * ``anchor`` — line appears verbatim in original body
        * ``new`` — line is genuinely new content
        """
        out: list[str] = []
        for line in region:
            stripped = _normalize_for_compare(line)
            if not stripped:
                out.append("blank")
            elif stripped in _TRIVIAL_LINES:
                out.append("trivial")
            elif _is_anchor_line(line, original_lines_norm):
                out.append("anchor")
            else:
                out.append("new")
        return out

    before_cls = classify(before)
    after_cls = classify(after)

    has_new_before = "new" in before_cls
    has_new_after = "new" in after_cls

    short_marker_line = _short_marker_with_indent(marker_line)

    # Case 1: top_insert — new lines before marker, after contains only
    # anchors/blanks/trivials (the body that follows is implied). Keep
    # new + trivial lines before (trivials like `}` closing the new
    # block must be preserved).
    if has_new_before and not has_new_after:
        kept_before = [
            line for line, cls in zip(before, before_cls, strict=True)
            if cls in ("new", "trivial")
        ]
        minimized = "".join(kept_before) + short_marker_line
        return minimized, "top_insert"

    # Case 2: bottom_insert — new lines after marker, before contains
    # only anchors/blanks/trivials.
    if has_new_after and not has_new_before:
        kept_after = [
            line for line, cls in zip(after, after_cls, strict=True)
            if cls in ("new", "trivial")
        ]
        minimized = short_marker_line + "".join(kept_after)
        return minimized, "bottom_insert"

    # Case 3: middle — new lines on both sides, or no anchors at all.
    # Keep one anchor line on each side of the marker when one exists;
    # keep new + trivial lines verbatim.
    if has_new_before and has_new_after:
        kept_before: list[str] = []
        last_anchor_idx = -1
        for i, cls in enumerate(before_cls):
            if cls == "anchor":
                last_anchor_idx = i
        for i, line in enumerate(before):
            cls = before_cls[i]
            if cls in ("new", "trivial") or cls == "anchor" and i == last_anchor_idx:
                kept_before.append(line)

        kept_after: list[str] = []
        first_anchor_idx = -1
        for i, cls in enumerate(after_cls):
            if cls == "anchor":
                first_anchor_idx = i
                break
        for i, line in enumerate(after):
            cls = after_cls[i]
            if cls in ("new", "trivial") or cls == "anchor" and i == first_anchor_idx:
                kept_after.append(line)

        minimized = (
            "".join(kept_before) + short_marker_line + "".join(kept_after)
        )
        return minimized, "middle"

    # Fallback — couldn't confidently minimize; just swap long marker for
    # short marker, leaving the rest as v4 had it.
    swapped = snippet_no_sig
    for long in _LONG_MARKERS:
        if long in swapped:
            swapped = swapped.replace(long, SHORT_MARKER)
            break
    return swapped, "marker_swap_only"


# ---------------------------------------------------------------------------
# inline_edit reclassification
# ---------------------------------------------------------------------------


# Class-shell starters — first-line patterns that declare a class/struct
# whose body the snippet is partially editing.
_CLASS_SHELL_RE = re.compile(
    r"""
    ^\s*
    (?:
        (?:public|private|internal|protected|final|static|abstract|sealed|open|data|export|declare)\s+
    )*
    (?:class|struct|interface|record|object|enum|final\s+class)\s+
    \w+
    """,
    re.VERBOSE,
)

# Method-signature detection — first non-decorator line starts with a
# language-appropriate signature keyword or modifier+name+paren.
_METHOD_SIGNATURE_RE = re.compile(
    r"""
    ^\s*
    (?:
        (?:public|private|internal|protected|static|final|override|async|export|declare|virtual|abstract)\s+
    )*
    (?:
        (?:def|fn|func|function|fun)\s+\w+
      |
        (?:\w[\w<>,\[\]\s]*?)\s+\w+\s*\(
    )
    """,
    re.VERBOSE,
)


def _extract_first_symbol_name(line: str) -> str | None:
    """Best-effort extract of the method/function name from a signature
    first line."""
    # Python / Ruby / Elixir / Rust / Go / Swift / Kotlin / TS / PHP — def/fn/func/function/fun NAME
    m = re.search(
        r"\b(?:def|fn|func|function|fun)\s+(\w+)", line
    )
    if m:
        return m.group(1)
    # C/C++/Java/C#: modifier...  TYPE NAME(
    m = re.search(r"(\w+)\s*\(", line)
    if m:
        candidate = m.group(1)
        # Avoid catching modifiers/types as "name" — pick the one that
        # immediately precedes the paren.
        return candidate
    return None


def detect_inline_reauthor_candidate(
    snippet: str,
    original: str,
) -> tuple[bool, str | None, str]:
    """Detect if an ``inline_edit`` fixture can be reauthored as
    ``replace=<name>`` + minimal snippet under FastEdit 0.2.3.

    Returns ``(is_candidate, target_name, reason_tag)``.

    Heuristics (conservative — we only reauthor if confidence is high):

    * (A) snippet starts with a class-shell line AND contains a
      body-level marker AND the class has exactly one method whose
      body the snippet redefines → reauthor as ``replace=<method_name>``
      where ``method_name`` is the edited method (extracted from the
      non-echo body lines).
    * (B) snippet starts with a method signature (not class shell, no
      class wrapping), has a marker, and the original contains the same
      method → reauthor as ``replace=<method_name>``.
    * Otherwise → not a candidate.

    Detection is tag-based:

    * ``"class_shell_method_edit"`` — case (A)
    * ``"bare_method_edit"`` — case (B)
    * ``"not_a_candidate"`` — neither

    ``target_name`` is the detected method name (for A and B) or None.
    """
    lines = [line for line in snippet.splitlines() if line.strip()]
    if not lines:
        return False, None, "not_a_candidate"

    first_line = lines[0]

    # Case A: class shell
    if _CLASS_SHELL_RE.match(first_line):
        # Extract the class name — becomes the replace= target.
        class_m = re.search(
            r"\b(?:class|struct|interface|record|object|enum)\s+(\w+)",
            first_line,
        )
        class_name = class_m.group(1) if class_m else None

        # Prefer a nested method def (if present) — 0.2.3 scopes more
        # tightly when the target is the method rather than the class.
        body_lines = lines[1:]
        for line in body_lines:
            if "// ... existing code ..." in line or "# ... existing code ..." in line:
                continue
            m = re.search(
                r"\b(?:def|fn|func|function|fun)\s+(\w+)\s*\(", line
            )
            if m:
                name = m.group(1)
                if re.search(rf"\b(?:def|fn|func|function|fun)\s+{re.escape(name)}\b", original):
                    return True, name, "class_shell_method_edit"
                break
            # C#/Java/C++ style: modifier TYPE NAME(
            m2 = re.search(
                r"(?:public|private|protected|internal|static)\s+\w[\w<>,\s\[\]&*]*\s+(\w+)\s*\(",
                line,
            )
            if m2:
                name = m2.group(1)
                if re.search(rf"\b{re.escape(name)}\s*\(", original):
                    return True, name, "class_shell_method_edit"
                break

        # No inner method signature — class-level addition (e.g. Ruby
        # `attr_reader`, C# field, etc.). Reauthor as replace=<class_name>.
        if class_name and re.search(
            rf"\b(?:class|struct|interface|record|object|enum)\s+{re.escape(class_name)}\b",
            original,
        ):
            return True, class_name, "class_shell_class_level_add"

        return False, None, "not_a_candidate"

    # Skip leading decorator/attribute lines (@Decorator, #[Attribute],
    # [Attribute]) to find the first real signature line.
    probe_idx = 0
    while probe_idx < len(lines):
        probe = lines[probe_idx].strip()
        if (
            probe.startswith("@")
            or probe.startswith("#[")
            or probe.startswith("[")
        ):
            probe_idx += 1
            continue
        break
    if probe_idx >= len(lines):
        return False, None, "not_a_candidate"
    probe_line = lines[probe_idx]

    # Case B: bare method signature — snippet IS the method body (with or
    # without marker). Under 0.2.3, replace=<name> + minimal-body works.
    # When no marker is present the snippet is a whole-body rewrite; when
    # a marker is present it's a partial edit.
    if _METHOD_SIGNATURE_RE.match(probe_line):
        name = _extract_first_symbol_name(probe_line)
        if name and re.search(
            rf"\b{re.escape(name)}\b", original
        ):
            return True, name, "bare_method_edit"

    return False, None, "not_a_candidate"


# ---------------------------------------------------------------------------
# Payload construction
# ---------------------------------------------------------------------------


def _serialize_fast_edit(file_path: str, snippet: str, replace: str | None) -> str:
    """Serialize a fast_edit call."""
    args: dict[str, str] = {
        "file_path": file_path,
        "edit_snippet": snippet,
    }
    if replace:
        args["replace"] = replace
    return json.dumps(
        {"name": "fast_edit", "arguments": args},
        separators=(",", ":"),
    )


def _swap_long_marker(snippet: str) -> str:
    """Replace any long FastEdit marker with the short unicode marker."""
    out = snippet
    for long in _LONG_MARKERS:
        if long in out:
            out = out.replace(long, SHORT_MARKER)
    return out


def build_fastedit_payload_v5(
    operation_category: str,
    file_path: str,
    snippet: str,
    original_code: str,
    target_kind: str | None,
    target_name: str | None,
) -> tuple[str, str, bool, str]:
    """Return ``(payload_json, effective_snippet, reclassified, form_tag)``.

    ``form_tag`` is a short label describing which minimization path was
    applied — fed back into the JSONL row for the HTML report's per-row
    display.
    """
    # --- delete: unchanged
    if operation_category == "delete":
        payload = v3.build_fastedit_payload(
            operation_category, file_path, snippet, target_kind, target_name
        )
        return payload, snippet, False, "delete"

    # --- insert_after: unchanged
    if operation_category == "insert_after":
        payload = v3.build_fastedit_payload(
            operation_category, file_path, snippet, target_kind, target_name
        )
        return payload, snippet, False, "insert_after"

    # --- whole_rewrite: strip signature (v4 behaviour), no marker
    if operation_category == "whole_rewrite":
        stripped, did = v4_mod.maybe_strip_signature(
            operation_category, target_kind, target_name, snippet
        )
        effective = stripped if did else snippet
        payload = _serialize_fast_edit(
            file_path, effective, replace=target_name or None
        )
        return payload, effective, False, "whole_rewrite"

    # --- modify_inside: strip sig + anchor stripping + short marker
    if operation_category == "modify_inside":
        stripped, _did = v4_mod.maybe_strip_signature(
            operation_category, target_kind, target_name, snippet
        )
        # original_code is the full file; original body is the same
        # (snippet targets a function body). For anchor detection we
        # compare against original_code verbatim.
        minimized, form = minimize_modify_inside_snippet(
            stripped, original_code
        )
        payload = _serialize_fast_edit(
            file_path, minimized, replace=target_name or None
        )
        return payload, minimized, False, f"modify_inside:{form}"

    # --- inline_edit: attempt reclassification
    if operation_category == "inline_edit":
        is_cand, name, reason = detect_inline_reauthor_candidate(
            snippet, original_code
        )
        if is_cand and name:
            # Apply modify_inside minimization using the inferred method
            # name: strip signature if first line is a class shell, else
            # strip the method signature line.
            lines = snippet.splitlines(keepends=True)
            if not lines:
                effective = snippet
            elif _CLASS_SHELL_RE.match(lines[0].strip()):
                # Skip the class shell line (0.2.3 scopes via replace=name).
                effective = "".join(lines[1:])
                # If line 2 is the method signature, strip that too.
                l2 = effective.splitlines(keepends=True)
                if l2 and v4_mod.first_line_is_signature(
                    l2[0].rstrip("\n"), name
                ):
                    effective = "".join(l2[1:])
                # Drop the trailing class-close line if present (``}``
                # for C#/Java/TS/PHP, ``end`` for Ruby, ``}`` for
                # Kotlin).
                while effective.rstrip().endswith(("}", "end")):
                    rstripped = effective.rstrip()
                    last = rstripped.splitlines()[-1].strip()
                    if last in ("}", "end"):
                        # remove that last line
                        new_lines = rstripped.splitlines(keepends=True)[:-1]
                        effective = "".join(new_lines)
                        if not effective.endswith("\n") and effective:
                            effective += "\n"
                    else:
                        break
            else:
                # Bare method edit — possibly preceded by decorators.
                # Keep decorators, strip the signature line (0.2.2).
                lines_in = snippet.splitlines(keepends=True)
                decorator_prefix: list[str] = []
                idx = 0
                while idx < len(lines_in):
                    probe = lines_in[idx].strip()
                    if (
                        probe.startswith("@")
                        or probe.startswith("#[")
                        or probe.startswith("[")
                    ):
                        decorator_prefix.append(lines_in[idx])
                        idx += 1
                        continue
                    break
                # idx now points to the method signature line.
                body_lines = lines_in[idx:]
                # Strip the signature line.
                if body_lines and v4_mod.first_line_is_signature(
                    body_lines[0].rstrip("\n"), name
                ):
                    body_lines = body_lines[1:]
                # For Ruby-style `def ... end` or C-brace `{ ... }`, drop
                # the trailing close line so we emit body only.
                while body_lines:
                    last = body_lines[-1].strip()
                    if last in ("end", "}"):
                        body_lines = body_lines[:-1]
                    else:
                        break
                effective = "".join(decorator_prefix) + "".join(body_lines)

            # Now apply anchor stripping + short marker if a marker is present.
            minimized, form = minimize_modify_inside_snippet(
                effective, original_code
            )
            payload = _serialize_fast_edit(
                file_path, minimized, replace=name
            )
            return (
                payload, minimized, True,
                f"reauthored:{reason}:{form}",
            )

        # Not a reauthor candidate — swap long marker for short, otherwise
        # keep v4 form.
        swapped = _swap_long_marker(snippet)
        payload = _serialize_fast_edit(
            file_path, swapped, replace=None
        )
        return payload, swapped, False, "inline_marker_swap"

    # --- batch_edit: compose via target_batch
    if operation_category == "batch_edit":
        # Fall back: use v4's maybe_strip_signature + short marker.
        stripped, did = v4_mod.maybe_strip_signature(
            operation_category, target_kind, target_name, snippet
        )
        base = stripped if did else snippet
        # Attempt the modify_inside-style minimization if a marker is
        # present; otherwise just marker-swap.
        if any(m in base for m in _LONG_MARKERS):
            minimized, form = minimize_modify_inside_snippet(
                base, original_code
            )
            effective = minimized
            form_tag = f"batch:{form}"
        else:
            effective = base
            form_tag = "batch:no_marker"
        # 0.2.x fast_edit accepts a single replace= kwarg; batch fixtures
        # are authored as single-tool-call replacements in v3/v4.
        replace_kw = target_name if target_kind == "replace" else None
        if target_kind == "batch":
            # v3 also passes replace=target_name when present
            replace_kw = target_name or None
        payload = _serialize_fast_edit(
            file_path, effective, replace=replace_kw
        )
        return payload, effective, False, form_tag

    # Defensive fallback
    payload = v3.build_fastedit_payload(
        operation_category, file_path, snippet, target_kind, target_name
    )
    return payload, snippet, False, "unchanged_from_v4"


# ---------------------------------------------------------------------------
# Row computation
# ---------------------------------------------------------------------------


def compute_rows_v5(fixtures_dir: Path) -> list[dict]:
    """Walk every fixture; compute native + FastEdit v5 token costs."""
    enc = tiktoken.get_encoding(_ENCODING_NAME)
    rows: list[dict] = []

    for case_id, fixture in load_fixtures(fixtures_dir, langs=None):
        language = str(fixture.get("language", ""))
        pattern = str(fixture.get("pattern", ""))
        snippet = str(fixture.get("snippet", ""))
        original = str(fixture.get("original_code", ""))
        expected = str(fixture.get("expected_output", ""))
        target_kind = fixture.get("target_kind")
        if not isinstance(target_kind, str):
            target_kind = None
        target_name = fixture.get("target_name")
        if not isinstance(target_name, str):
            target_name = None

        operation_category = v3.categorize_fixture(
            target_kind, pattern, snippet
        )
        file_path = synthesize_file_path(case_id, language)

        # Native side: unchanged from v3.
        native_payloads = v3.build_native_edit_payloads_with_context(
            file_path, original, expected
        )
        native_tokens = sum(len(enc.encode(p)) for p in native_payloads)
        n_hunks = len(native_payloads)

        # v3 payload — for regression row.
        v3_payload = v3.build_fastedit_payload(
            operation_category, file_path, snippet, target_kind, target_name
        )
        v3_fe_tokens = len(enc.encode(v3_payload))

        # v4 payload
        v4_payload, _eff4, _stripped = v4_mod.build_fastedit_payload_v4(
            operation_category, file_path, snippet, target_kind, target_name
        )
        v4_fe_tokens = len(enc.encode(v4_payload))

        # v5 payload
        v5_payload, v5_effective, reclassified, form_tag = (
            build_fastedit_payload_v5(
                operation_category,
                file_path,
                snippet,
                original,
                target_kind,
                target_name,
            )
        )
        v5_fe_tokens = len(enc.encode(v5_payload))

        # Effective operation category (inline_edit reclassified to
        # modify_inside when reauthored). Used for category aggregation.
        effective_category = operation_category
        if reclassified:
            effective_category = "modify_inside"

        savings_tokens = native_tokens - v5_fe_tokens
        savings_pct = (
            (savings_tokens / native_tokens * 100.0)
            if native_tokens
            else 0.0
        )

        rows.append(
            {
                "case_id": case_id,
                "language": language,
                "pattern": pattern,
                "target_kind": target_kind,
                "has_marker": v3.has_existing_code_marker(snippet),
                "operation_category": operation_category,
                "effective_category": effective_category,
                "file_size_bytes": len(original.encode("utf-8")),
                "n_hunks": n_hunks,
                "native_edit_tokens": native_tokens,
                # v5 is the canonical "fastedit_tokens" field so downstream
                # HTML templates using DATA.rows[*].fastedit_tokens pick
                # up v5.
                "fastedit_tokens": v5_fe_tokens,
                "v3_fastedit_tokens": v3_fe_tokens,
                "v4_fastedit_tokens": v4_fe_tokens,
                "v5_fastedit_tokens": v5_fe_tokens,
                "savings_tokens": savings_tokens,
                "savings_pct": round(savings_pct, 3),
                "v5_savings_pct": round(savings_pct, 3),
                "v5_reclassified": reclassified,
                "v5_minimal_payload_form": form_tag,
                "v5_effective_snippet_tokens": len(
                    enc.encode(v5_effective)
                ),
            }
        )

    return rows


# ---------------------------------------------------------------------------
# Summarization
# ---------------------------------------------------------------------------


def _bucket_new() -> dict:
    return {
        "n": 0,
        "native_total": 0,
        "fastedit_total": 0,
        "savings_total": 0,
    }


def _bucket_finalize(b: dict) -> None:
    nat = b["native_total"]
    sav = b["savings_total"]
    b["savings_pct"] = round((sav / nat * 100.0) if nat else 0.0, 3)
    b["ratio_native_over_fastedit"] = round(
        (nat / b["fastedit_total"]) if b["fastedit_total"] else 0.0, 3
    )
    b["native_avg"] = round((nat / b["n"]) if b["n"] else 0.0, 2)
    b["fastedit_avg"] = round(
        (b["fastedit_total"] / b["n"]) if b["n"] else 0.0, 2
    )


def summarize_v5(rows: list[dict], run_id: str) -> dict:
    """v5 summary — per-category + per-language + headline + progression.

    Progression totals (v3/v4/v5 FastEdit totals for the headline set)
    are included so the HTML report can show the five-release journey.
    """
    per_cat: dict[str, dict] = defaultdict(_bucket_new)
    per_lang: dict[str, dict] = defaultdict(_bucket_new)
    per_lang_ex_whole: dict[str, dict] = defaultdict(_bucket_new)

    total_native = 0
    total_fe = 0
    headline_native = 0
    headline_fe = 0
    headline_n = 0

    # Version progression (v3/v4/v5) across the headline set.
    v3_total_headline = 0
    v4_total_headline = 0
    v5_total_headline = 0

    # Also produce per-category version progression (for the HTML's
    # per-operation section).
    per_cat_versions: dict[str, dict[str, int]] = defaultdict(
        lambda: {"v3": 0, "v4": 0, "v5": 0, "native": 0, "n": 0}
    )

    reclassified_count = 0

    for r in rows:
        cat = r["effective_category"]
        lang = r["language"]
        nat = r["native_edit_tokens"]
        fe = r["v5_fastedit_tokens"]
        sav = nat - fe

        for bucket in (per_cat[cat], per_lang[lang]):
            bucket["n"] += 1
            bucket["native_total"] += nat
            bucket["fastedit_total"] += fe
            bucket["savings_total"] += sav

        total_native += nat
        total_fe += fe

        # Per-category progression uses the ORIGINAL category (before
        # reclassification) so the reader can see how the mix changed.
        orig_cat = r["operation_category"]
        pcv = per_cat_versions[orig_cat]
        pcv["v3"] += r["v3_fastedit_tokens"]
        pcv["v4"] += r["v4_fastedit_tokens"]
        pcv["v5"] += r["v5_fastedit_tokens"]
        pcv["native"] += nat
        pcv["n"] += 1

        if cat != "whole_rewrite":
            headline_native += nat
            headline_fe += fe
            headline_n += 1
            v3_total_headline += r["v3_fastedit_tokens"]
            v4_total_headline += r["v4_fastedit_tokens"]
            v5_total_headline += r["v5_fastedit_tokens"]
            b2 = per_lang_ex_whole[lang]
            b2["n"] += 1
            b2["native_total"] += nat
            b2["fastedit_total"] += fe
            b2["savings_total"] += sav

        if r["v5_reclassified"]:
            reclassified_count += 1

    for b in per_cat.values():
        _bucket_finalize(b)
    for b in per_lang.values():
        _bucket_finalize(b)
    for b in per_lang_ex_whole.values():
        _bucket_finalize(b)

    headline_savings = headline_native - headline_fe
    headline_pct = (
        (headline_savings / headline_native * 100.0)
        if headline_native else 0.0
    )
    headline_ratio = (
        (headline_native / headline_fe) if headline_fe else 0.0
    )

    # Top savings per category (for HTML)
    top_by_category: dict[str, list[dict]] = {}
    for cat in {r["effective_category"] for r in rows}:
        cat_rows = [r for r in rows if r["effective_category"] == cat]
        cat_rows.sort(
            key=lambda r: r["native_edit_tokens"] - r["v5_fastedit_tokens"],
            reverse=True,
        )
        top_by_category[cat] = [
            {
                "case_id": r["case_id"],
                "savings_tokens": r["native_edit_tokens"] - r["v5_fastedit_tokens"],
                "savings_pct": round(
                    ((r["native_edit_tokens"] - r["v5_fastedit_tokens"])
                     / r["native_edit_tokens"] * 100.0)
                    if r["native_edit_tokens"] else 0.0,
                    3,
                ),
                "native_edit_tokens": r["native_edit_tokens"],
                "fastedit_tokens": r["v5_fastedit_tokens"],
            }
            for r in cat_rows[:3]
        ]

    # Progression block — headline numbers across v3/v4/v5.
    progression = {
        "v3": {
            "fastedit_total_headline": v3_total_headline,
            "savings_pct_headline": round(
                ((headline_native - v3_total_headline)
                 / headline_native * 100.0)
                if headline_native else 0.0,
                3,
            ),
        },
        "v4": {
            "fastedit_total_headline": v4_total_headline,
            "savings_pct_headline": round(
                ((headline_native - v4_total_headline)
                 / headline_native * 100.0)
                if headline_native else 0.0,
                3,
            ),
        },
        "v5": {
            "fastedit_total_headline": v5_total_headline,
            "savings_pct_headline": round(
                ((headline_native - v5_total_headline)
                 / headline_native * 100.0)
                if headline_native else 0.0,
                3,
            ),
        },
        "native_total_headline": headline_native,
    }

    return {
        "run_id": run_id,
        "methodology": METHODOLOGY_TAG,
        "tokenizer_used": f"tiktoken:{_ENCODING_NAME}",
        "anchor_context_lines": v3.ANCHOR_LINES,
        "n_fixtures": len(rows),
        "reclassified_count": reclassified_count,
        "totals": {
            "native_total": total_native,
            "fastedit_total": total_fe,
            "savings_total": total_native - total_fe,
            "savings_pct": round(
                ((total_native - total_fe) / total_native * 100.0)
                if total_native else 0.0,
                3,
            ),
        },
        "operation_categories": dict(per_cat),
        "headline_excluding_whole_rewrite": {
            "n_fixtures": headline_n,
            "native_total": headline_native,
            "fastedit_total": headline_fe,
            "savings_tokens": headline_savings,
            "savings_pct": round(headline_pct, 3),
            "savings_ratio": round(headline_ratio, 3),
        },
        "per_language": dict(per_lang),
        "per_language_excluding_whole_rewrite": dict(per_lang_ex_whole),
        "version_progression": progression,
        "per_category_versions": {
            k: dict(v) for k, v in per_cat_versions.items()
        },
        "top_by_category": top_by_category,
    }


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def run_tax_per_operation_v5(
    fixtures_dir: Path,
    results_dir: Path,
    run_id: str | None = None,
) -> tuple[Path, Path, dict]:
    """Compute rows, write JSONL + summary, return paths and summary."""
    if run_id is None:
        run_id = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")

    out_dir = results_dir / "tax_per_operation"
    out_dir.mkdir(parents=True, exist_ok=True)

    jsonl_path = out_dir / f"{run_id}.jsonl"
    summary_path = out_dir / f"{run_id}.summary.json"

    rows = compute_rows_v5(fixtures_dir)
    with jsonl_path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")

    summary = summarize_v5(rows, run_id)
    with summary_path.open("w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2, sort_keys=True)

    return jsonl_path, summary_path, summary


def _print_summary(
    summary: dict, jsonl_path: Path, summary_path: Path
) -> None:
    totals = summary["totals"]
    hl = summary["headline_excluding_whole_rewrite"]
    prog = summary["version_progression"]
    print("=" * 78)
    print("bench-02 v5 — calling-agent tax per operation category")
    print("            (FastEdit 0.2.5 minimal-payload form)")
    print("=" * 78)
    print(f"  fixtures:        {summary['n_fixtures']}")
    print(f"  tokenizer:       {summary['tokenizer_used']}")
    print(f"  anchor context:  ±{summary['anchor_context_lines']} lines")
    print(f"  methodology:     {summary['methodology']}")
    print(f"  reclassified:    {summary['reclassified_count']}")
    print()
    print("OVERALL")
    print(f"  native total:    {totals['native_total']:>7}")
    print(f"  fastedit total:  {totals['fastedit_total']:>7}")
    print(
        f"  savings:         {totals['savings_total']:>7} "
        f"({totals['savings_pct']:>6.2f}%)"
    )
    print()
    print("HEADLINE (excluding whole_rewrite)")
    print(f"  n:               {hl['n_fixtures']:>7}")
    print(f"  native total:    {hl['native_total']:>7}")
    print(f"  fastedit total:  {hl['fastedit_total']:>7}")
    print(
        f"  savings:         {hl['savings_tokens']:>7} "
        f"({hl['savings_pct']:>6.2f}%)"
    )
    print(f"  ratio n/fe:      {hl['savings_ratio']:>7.3f}x")
    print()
    print("VERSION PROGRESSION — headline fastedit totals / savings pct")
    print(
        f"  v3:   fe={prog['v3']['fastedit_total_headline']:>5}  "
        f"savings={prog['v3']['savings_pct_headline']:>6.2f}%"
    )
    print(
        f"  v4:   fe={prog['v4']['fastedit_total_headline']:>5}  "
        f"savings={prog['v4']['savings_pct_headline']:>6.2f}%"
    )
    print(
        f"  v5:   fe={prog['v5']['fastedit_total_headline']:>5}  "
        f"savings={prog['v5']['savings_pct_headline']:>6.2f}%"
    )
    print()
    print("PER OPERATION CATEGORY (effective, sorted by savings_pct desc)")
    print(
        f"  {'category':<16} {'n':>3} {'native':>8} {'fastedit':>9} "
        f"{'saved':>8} {'%':>7}"
    )
    sorted_cats = sorted(
        summary["operation_categories"].items(),
        key=lambda kv: kv[1]["savings_pct"],
        reverse=True,
    )
    for cat, b in sorted_cats:
        print(
            f"  {cat:<16} {b['n']:>3} "
            f"{b['native_total']:>8} "
            f"{b['fastedit_total']:>9} "
            f"{b['savings_total']:>8} "
            f"{b['savings_pct']:>6.2f}%"
        )
    print()
    print(f"wrote: {jsonl_path}")
    print(f"wrote: {summary_path}")


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint."""
    parser = argparse.ArgumentParser(
        description=(
            "Compute calling-agent tax per operation category across "
            "every bench-02 fixture — v5 with FastEdit 0.2.5 minimal "
            "payloads (five-release stack)."
        )
    )
    parser.add_argument(
        "--fixtures-dir",
        type=Path,
        default=_REPO_ROOT / "fixtures",
        help="Root of the fixture tree (default: <repo>/fixtures).",
    )
    parser.add_argument(
        "--results-dir",
        type=Path,
        default=_REPO_ROOT / "results",
        help="Root of the results tree (default: <repo>/results).",
    )
    parser.add_argument(
        "--run-id",
        type=str,
        default=None,
        help="Explicit run id; default is the current UTC timestamp.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )

    jsonl_path, summary_path, summary = run_tax_per_operation_v5(
        args.fixtures_dir, args.results_dir, args.run_id
    )
    _print_summary(summary, jsonl_path, summary_path)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
