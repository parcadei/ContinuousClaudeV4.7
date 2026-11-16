#!/usr/bin/env python3
"""Annotate every fixture YAML with MCP-mode target metadata (VAL-303).

Background
----------

``fastedit.inference.chunked_merge`` accepts two optional kwargs describing
how an MCP agent would invoke ``fast_edit``:

* ``after=<symbol>`` — pure AST splice. The snippet IS the new code,
  spliced after the named top-level symbol. Zero model tokens.
* ``replace=<symbol>`` — AST-scoped region. The snippet replaces the
  body/signature of the named top-level symbol. The merge model sees
  only a ~35-line window, not the whole file.

Real agents *always* pass one of these kwargs (see the MCP tool
description). The prior benchmark run left both ``None``, routing every
case through the whole-file markers-only path — a path no agent actually
uses. This script adds ``target_kind`` and ``target_name`` fields to
every fixture YAML so the benchmark can replay the real MCP invocation
shape.

Derivation rule (applied structurally via ``tldr structure``)
-------------------------------------------------------------

For each fixture we compare the top-level symbol maps of
``original_code`` and ``expected_output``:

1. **Existing symbol modified** (present in both, body text differs):
   ``target_kind='replace'``, ``target_name=<symbol>``. The chunked_merge
   replace path scopes the model to that symbol's ~35-line region.
2. **New top-level symbol in expected_output not in original_code**:
   ``target_kind='after'``, ``target_name=<preceding top-level symbol
   in expected_output>``. This is a pure AST splice; we ALSO rewrite
   the snippet to the raw new code (strip ``# ... existing code ...``
   markers) because the after-path expects the new code verbatim.
3. **class_method pattern** (adds/modifies a method inside an existing
   class): ``target_kind='replace'``, ``target_name=<class_name>``.
   Method insertion is a sibling-insertion inside a class body — the
   after-fast-path handles top-level siblings only, so we route these
   through the replace path with the class as the target.
4. **Whole-file replacements with no preserved top-level structure**:
   ``target_kind='none'``, ``target_name=None``. Reserved for genuine
   full-file rewrites (expected to be 0 cases).
5. **Elixir**: FastEdit supports .ex files via tree-sitter-elixir
   (FASTEDIT-M5). Annotations follow the same derivation rules as
   every other language.

Ambiguous cases (multiple existing symbols changed with no clear
single target) are flagged and left as ``target_kind='none'`` for
manual review — we NEVER invent a target.

Usage
-----

    python fixtures/_annotate_targets.py fixtures/
    python fixtures/_annotate_targets.py fixtures/python/01-add_guard.yaml  # one file
    python fixtures/_annotate_targets.py --dry-run fixtures/               # print, don't write

The script is idempotent: re-running leaves already-annotated YAMLs
untouched unless the derived annotation differs.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - exercised only in broken envs
    print("FATAL: PyYAML is required for this script.", file=sys.stderr)
    sys.exit(2)


# ─── Language → file extension (matches fastedit_adapter.LANG_TO_EXT) ────

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


# ─── Data types ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SymbolDef:
    """One top-level symbol as reported by ``tldr structure``."""

    name: str
    kind: str
    line_start: int
    line_end: int

    @property
    def is_class_like(self) -> bool:
        return self.kind in {"class", "struct", "interface", "module", "enum"}


@dataclass
class Annotation:
    """Outcome of annotating a single fixture."""

    path: Path
    target_kind: str  # 'replace' | 'after' | 'none'
    target_name: str | None
    snippet_rewritten: bool
    ambiguous_reason: str | None = None


# ─── tldr structure extraction ──────────────────────────────────────────


def _tldr_structure(code: str, ext: str) -> list[SymbolDef]:
    """Return top-level symbols from ``code`` via ``tldr structure``.

    Writes ``code`` to a tempfile with ``ext`` suffix (so tldr picks the
    right language), runs ``tldr structure --format compact``, parses
    the JSON, and filters to TOP-LEVEL symbols only (no methods).
    """
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=ext, delete=False, encoding="utf-8"
    ) as tmp:
        tmp.write(code)
        tmp_path = tmp.name

    try:
        proc = subprocess.run(
            ["tldr", "structure", tmp_path, "--format", "compact"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if proc.returncode != 0:
            return []
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return []
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    files = data.get("files") or []
    if not files:
        return []
    defs = files[0].get("definitions") or []

    # Top-level = not a method AND not nested inside another symbol's
    # line range. Some language extractors (notably Swift) report
    # `let`/`var` bindings inside function bodies as "constant" — we
    # strip those by checking containment under any class-like OR
    # function-like node.
    container_ranges: list[tuple[int, int]] = []
    for d in defs:
        if d.get("kind") in {
            "class", "struct", "interface", "module", "enum",
            "function", "method",
        }:
            container_ranges.append((d.get("line_start", 0), d.get("line_end", 0)))

    symbols: list[SymbolDef] = []
    for d in defs:
        name = d.get("name")
        kind = d.get("kind", "")
        line_start = d.get("line_start", 0)
        line_end = d.get("line_end", 0)
        if not name or not line_start:
            continue
        if kind == "method":
            continue
        # Nested inside some outer class/function? Treat as non-top-level.
        nested = False
        for cs, ce in container_ranges:
            if cs < line_start and line_end <= ce:
                nested = True
                break
        if nested:
            continue
        symbols.append(SymbolDef(name=name, kind=kind, line_start=line_start, line_end=line_end))

    # Deduplicate: some extractors (notably C with struct-pointer
    # parameters) report the same name multiple times. Keep the
    # earliest line_start per name.
    by_name: dict[str, SymbolDef] = {}
    for s in symbols:
        existing = by_name.get(s.name)
        if existing is None or s.line_start < existing.line_start:
            by_name[s.name] = s
    dedup = list(by_name.values())
    dedup.sort(key=lambda s: s.line_start)
    return dedup


def _symbol_body_text(code: str, sym: SymbolDef) -> str:
    """Extract ``sym``'s text slice from ``code`` (1-indexed line range)."""
    lines = code.splitlines(keepends=True)
    start = max(0, sym.line_start - 1)
    end = min(len(lines), sym.line_end)
    return "".join(lines[start:end])


# ─── Derivation logic ──────────────────────────────────────────────────


def _tldr_snippet_names(snippet: str, ext: str) -> list[str]:
    """Return the list of symbol names that chunked_merge's guard sees.

    Mirrors ``fastedit.inference.snippet_analysis._extract_snippet_names``
    by parsing the snippet with ``tldr structure`` and returning ALL
    definition names (including nested methods/constants). chunked_merge
    does NOT filter nesting, so neither do we.
    """
    return [n for n, _ in _tldr_snippet_named_defs(snippet, ext)]


def _tldr_snippet_named_defs(snippet: str, ext: str) -> list[tuple[str, str]]:
    """Return ``[(name, kind), ...]`` for each definition in the snippet.

    Used for smarter filtering: M7 workaround for tldr C's struct-pointer
    misdetection — params of type ``struct X *`` are reported as
    top-level struct defs. We can filter these out by preferring
    function/class/method/module kinds when deriving targets.
    """
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=ext, delete=False, encoding="utf-8"
    ) as tmp:
        tmp.write(snippet)
        tmp_path = tmp.name
    try:
        proc = subprocess.run(
            ["tldr", "structure", tmp_path, "--format", "compact"],
            capture_output=True, text=True, timeout=15,
        )
        if proc.returncode != 0:
            return []
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return []
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    files = data.get("files") or []
    if not files:
        return []
    out: list[tuple[str, str]] = []
    for d in files[0].get("definitions") or []:
        n = d.get("name")
        k = d.get("kind", "")
        if n:
            out.append((n, k))
    return out


def _strip_existing_code_markers(snippet: str) -> str:
    """Remove ``# ... existing code ...`` / ``// ... existing code ...`` lines.

    Used when rewriting an 'after'-mode snippet to raw new code. We
    strip a full line when its stripped text matches any of the common
    marker flavors (#, //, --, %, <!-- ... -->, ; etc.).
    """
    marker_tokens = (
        "# ... existing code ...",
        "// ... existing code ...",
        "-- ... existing code ...",
        "% ... existing code ...",
        "/* ... existing code ... */",
        "<!-- ... existing code ... -->",
        "; ... existing code ...",
    )
    out: list[str] = []
    for line in snippet.splitlines(keepends=True):
        if line.strip() in marker_tokens:
            continue
        out.append(line)
    return "".join(out)


def _all_tldr_structure_names(code: str, ext: str) -> set[str]:
    """Return every definition name reported by ``tldr structure``.

    Unlike :func:`_tldr_structure`, this function does NOT filter nested
    symbols: chunked_merge's :func:`_resolve_symbol` iterates the full
    ``get_ast_map`` output, so nested constants / methods / etc. are
    ALSO valid targets at runtime.
    """
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=ext, delete=False, encoding="utf-8"
    ) as tmp:
        tmp.write(code)
        tmp_path = tmp.name
    try:
        proc = subprocess.run(
            ["tldr", "structure", tmp_path, "--format", "compact"],
            capture_output=True, text=True, timeout=15,
        )
        if proc.returncode != 0:
            return set()
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return set()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return set()
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    names: set[str] = set()
    files = data.get("files") or []
    if not files:
        return names
    for d in files[0].get("definitions") or []:
        if d.get("name"):
            names.add(d["name"])
    return names


def _original_symbol_names(
    orig_syms: list[SymbolDef], original: str, ext: str
) -> set[str]:
    """Return every name chunked_merge's ``_resolve_symbol`` could match.

    Combines the top-level symbols we already have with the full
    ``tldr structure`` definition list (methods, nested constants) and
    method names from ``tldr extract``. Any of these is a valid
    ``replace=`` target at runtime.
    """
    names: set[str] = {s.name for s in orig_syms}
    names |= _all_tldr_structure_names(original, ext)
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=ext, delete=False, encoding="utf-8"
    ) as tmp:
        tmp.write(original)
        tmp_path = tmp.name
    try:
        proc = subprocess.run(
            ["tldr", "extract", tmp_path, "--format", "json"],
            capture_output=True, text=True, timeout=10,
        )
        if proc.returncode == 0:
            try:
                data = json.loads(proc.stdout)
            except json.JSONDecodeError:
                data = {}
            for cls in data.get("classes", []) or []:
                for method in cls.get("methods", []) or []:
                    mname = method.get("name")
                    if mname:
                        names.add(mname)
            for fn in data.get("functions", []) or []:
                if fn.get("name"):
                    names.add(fn["name"])
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    finally:
        Path(tmp_path).unlink(missing_ok=True)
    return names


def derive_annotation(
    fixture: dict,
    path: Path,
) -> Annotation:
    """Compute (target_kind, target_name) for one fixture.

    Rule: pick a target_name that BOTH (a) passes chunked_merge's
    guard (``snippet_names ⊆ {target_name}``) AND (b) resolves to a
    real symbol in the original file. If no such target exists, fall
    back to ``target_kind='none'`` (markers-only) — that path still
    works, it's just not the MCP fast path.
    """
    language = fixture.get("language", "")
    original = fixture.get("original_code", "") or ""
    expected = fixture.get("expected_output", "") or ""
    snippet = fixture.get("snippet", "") or ""

    ext = LANG_TO_EXT.get(language)

    # Respect authorial intent (partial): when the fixture's
    # ``authoring_notes`` explicitly document ``target_kind=none`` as
    # the correct annotation, it usually means some FastEdit path
    # (typically ``deterministic_edit`` / text_match) produces wrong
    # output on this input shape. We preserve ``none`` UNLESS the
    # FASTEDIT-M7-001 direct-swap fast path now covers the case:
    # snippet has no markers AND parses to exactly one top-level def
    # whose name equals the inferred replace target. In that case the
    # model-path bug the note documented is sidestepped, so we promote
    # to ``replace``.
    authoring_notes = fixture.get("authoring_notes", "") or ""
    # Respect authorial intent: when the fixture's ``authoring_notes``
    # explicitly document ``target_kind=none`` as the correct
    # annotation, keep 'none'. These notes are added by fixture authors
    # after observing misbehaviour under one of FastEdit's non-model
    # paths (text_match anchor-drift on nested end/}, direct-swap
    # indent mismatch on class-scoped methods, etc.). Overriding the
    # note statically is risky; the same snippet shape that direct-swap
    # handles cleanly for one language trips deterministic_edit in
    # another. We prefer the conservative path and leave these at
    # 'none' — the markers-only / model path still produces correct
    # output, just without the MCP fast-path speedup.
    #
    # The concrete tradeoff, measured on the M7 rebench:
    #   - 12 author-noted fixtures pass on 'none' (markers-only model)
    #   - 4 pass on 'replace' (direct-swap applies, model bypass works)
    #   - 5 regress to tier-1 FAIL on 'replace' (deterministic_edit
    #     finds false anchors)
    #   Net: preferring 'none' for author-noted fixtures is safer.
    # Match the canonical "target_kind=none:" prose format used by
    # fixture authors to declare an intentional 'none'. We deliberately
    # require the trailing colon so that retrospective mentions like
    # "Earlier draft used target_kind=none ... failed" do NOT suppress
    # the derivation — those notes document REJECTED old choices, not
    # the intent for the current fixture.
    if "target_kind=none:" in authoring_notes:
        return Annotation(
            path=path, target_kind="none", target_name=None,
            snippet_rewritten=False,
            ambiguous_reason=(
                "authoring_notes document target_kind=none as intentional"
            ),
        )
    if ext is None:
        return Annotation(
            path=path, target_kind="none", target_name=None,
            snippet_rewritten=False,
            ambiguous_reason=f"no extension mapping for language '{language}'",
        )

    orig_syms = _tldr_structure(original, ext)
    exp_syms = _tldr_structure(expected, ext)
    snippet_defs = _tldr_snippet_named_defs(snippet, ext)
    orig_resolvable = _original_symbol_names(orig_syms, original, ext)

    # M7 workaround for tldr's struct-pointer misdetection (C / Swift /
    # Go): param types like ``struct sockaddr *addr`` get reported as
    # top-level struct defs with a one-line range. These are NOT real
    # definitions in the snippet. Filter them out by preferring the
    # *primary* definition kinds (function/class/method/module/interface
    # /enum) when multiple names are reported — but only when the
    # "secondary" name is a struct-like kind whose range is trivially
    # short (≤1 line) or whose name does not appear anywhere in the
    # ORIGINAL as a resolvable top-level symbol.
    primary_kinds = {
        "function", "class", "method", "module", "interface", "enum",
    }
    primary_names = [n for n, k in snippet_defs if k in primary_kinds]
    if primary_names:
        filtered_snippet_names = [
            n for n, k in snippet_defs
            if k in primary_kinds
            or n in orig_resolvable  # struct that IS a real symbol
        ]
        # If filtering actually reduced the count, use the filtered
        # list. Otherwise keep the original for backward compatibility.
        if len(filtered_snippet_names) < len(snippet_defs):
            snippet_names = filtered_snippet_names
        else:
            snippet_names = [n for n, _ in snippet_defs]
    else:
        snippet_names = [n for n, _ in snippet_defs]

    orig_by_name = {s.name: s for s in orig_syms}
    exp_by_name = {s.name: s for s in exp_syms}

    added = [s for s in exp_syms if s.name not in orig_by_name]
    removed = [s for s in orig_syms if s.name not in exp_by_name]
    changed: list[SymbolDef] = []
    for name, o_sym in orig_by_name.items():
        e_sym = exp_by_name.get(name)
        if e_sym is None:
            continue
        if _symbol_body_text(original, o_sym) != _symbol_body_text(expected, e_sym):
            changed.append(e_sym)

    # ── Case A: `after` splice (exactly one new top-level symbol) ──
    if len(added) == 1 and not changed and not removed:
        new_sym = added[0]
        preceding: SymbolDef | None = None
        for s in exp_syms:
            if s.line_start >= new_sym.line_start:
                break
            preceding = s
        if preceding is not None and preceding.name in orig_resolvable:
            raw_snippet = _strip_existing_code_markers(snippet)
            if raw_snippet and not raw_snippet.endswith("\n"):
                raw_snippet += "\n"
            snippet_rewritten = raw_snippet != snippet
            fixture["snippet"] = raw_snippet
            return Annotation(
                path=path, target_kind="after",
                target_name=preceding.name,
                snippet_rewritten=snippet_rewritten,
            )

    # ── Case B: `replace` — target must match the snippet's symbol(s)
    #
    # The guard requires: extras = [n for n in snippet_names if n != replace] == [].
    # Viable options:
    #   • snippet_names empty        → replace can be any resolvable name
    #   • snippet_names == [X]       → replace = X (if X resolves)
    #   • snippet_names == [X, X, …] → replace = X (duplicates are fine)
    #   • snippet_names has >1 distinct name → single fast_edit CAN'T do it
    distinct_snippet = list(dict.fromkeys(snippet_names))  # preserve order, dedup

    if len(distinct_snippet) == 1 and distinct_snippet[0] in orig_resolvable:
        return Annotation(
            path=path, target_kind="replace",
            target_name=distinct_snippet[0],
            snippet_rewritten=False,
        )

    if not distinct_snippet:
        # snippet has no top-level def — typical line-level marker edit.
        # Pick the single modified top-level symbol if unambiguous.
        if len(changed) == 1 and not added and not removed:
            return Annotation(
                path=path, target_kind="replace",
                target_name=changed[0].name,
                snippet_rewritten=False,
            )
        # Decorator-prepend style: no symbol diff'd, but file differs
        # and exactly one top-level symbol exists.
        if (
            not added and not changed and not removed and original != expected
            and len(orig_syms) == 1
        ):
            return Annotation(
                path=path, target_kind="replace",
                target_name=orig_syms[0].name,
                snippet_rewritten=False,
            )

    # ── Case C: full_replace whole-file swap (rare) ──
    if (
        fixture.get("pattern") == "full_replace"
        and len(orig_syms) <= 1 and len(exp_syms) <= 1
        and orig_syms and exp_syms
        and orig_syms[0].name != exp_syms[0].name
    ):
        return Annotation(
            path=path, target_kind="none", target_name=None,
            snippet_rewritten=False,
        )

    # ── Case D: class_method / batch path (M7 Worker B) ──
    # Snippet carries a class shell + exactly one nested method whose
    # body differs from the original. A real MCP agent would issue
    # ``fast_edit(replace='<method>', snippet=<new method body>)`` or
    # use ``fast_batch_edit``. We emit a single-element batch with the
    # method as the replace target; the snippet is extracted from the
    # class-scoped text in the fixture's ``snippet`` by stripping the
    # class header, trailing closing delimiter, and the
    # ``... existing code ...`` marker, then de-indented.
    #
    # Pass the original code + ext so the helper can diff the snippet's
    # methods against the original to pick the one method whose body
    # actually changed (relevant for snippets that include a verbatim
    # class shell + ctor).
    batch_ann = _try_class_method_batch(
        fixture, path, snippet_defs, orig_resolvable, orig_syms, ext,
        original, expected,
    )
    if batch_ann is not None:
        return batch_ann

    # ── Fallback: no single-target fast_edit invocation fits. The
    # markers-only path still succeeds on these cases, we just can't
    # flag them as MCP fast-path. An agent would use fast_batch_edit.
    details = (
        f"snippet={distinct_snippet}, "
        f"added={[s.name for s in added]}, "
        f"changed={[s.name for s in changed]}, "
        f"removed={[s.name for s in removed]}"
    )
    return Annotation(
        path=path, target_kind="none", target_name=None,
        snippet_rewritten=False,
        ambiguous_reason=(
            "no single-call fast_edit satisfies both the snippet guard "
            f"and the resolver: {details}"
        ),
    )


# ─── M7: class_method → single-element batch edit ──────────────────────


def _try_class_method_batch(
    fixture: dict,
    path: Path,
    snippet_defs: list[tuple[str, str]],
    orig_resolvable: set[str],
    orig_syms: list[SymbolDef],
    ext: str,
    original: str,
    expected: str,
) -> Annotation | None:
    """Return a batch Annotation for class_method-style fixtures, or None.

    The pattern we detect: the snippet contains exactly one class-like
    definition AND exactly one method (or nested function) whose name
    resolves to an existing symbol in the original. The resulting
    ``target_batch`` is a one-element list:

        [{"kind": "replace", "name": <method>, "snippet": <method-body-only>}]

    We extract the method body from the snippet by:
      1. Parsing snippet with tldr to get the method's (line_start, line_end).
      2. Slicing those lines out verbatim.
      3. De-indenting by the common leading whitespace.

    On any extraction failure we return None so the caller falls through
    to the ambiguous/'none' branch.
    """
    # Only fire for class_method-pattern fixtures. Other patterns that
    # happen to trip the same snippet_defs shape (e.g. C functions whose
    # ``struct`` parameter types get misdetected as top-level defs)
    # should stay on the 'none' path rather than be mis-annotated as batches.
    if fixture.get("pattern") != "class_method":
        return None

    class_kinds = {"class", "struct", "interface", "module", "enum"}
    method_kinds = {"method", "function"}

    class_defs = [(n, k) for n, k in snippet_defs if k in class_kinds]
    # Dedup method names (tldr may report the same name twice with
    # different kinds, e.g. C++ constructors).
    seen_methods: set[str] = set()
    method_defs: list[tuple[str, str]] = []
    for n, k in snippet_defs:
        if k in method_kinds and n not in seen_methods:
            method_defs.append((n, k))
            seen_methods.add(n)
    if len(class_defs) != 1 or not method_defs:
        return None

    # Pick the method whose body differs between the snippet and the
    # original. If only one method is in the snippet, use it directly.
    # If multiple candidates, fall back to the one whose body text
    # actually changed when we look up its line range in both.
    def _body_for_name(code: str, name: str, language_ext: str) -> str | None:
        """Return the full source slice for a method named ``name``."""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=language_ext, delete=False, encoding="utf-8"
        ) as t:
            t.write(code)
            p = t.name
        try:
            r = subprocess.run(
                ["tldr", "structure", p, "--format", "compact"],
                capture_output=True, text=True, timeout=10,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return None
        finally:
            Path(p).unlink(missing_ok=True)
        if r.returncode != 0:
            return None
        try:
            doc = json.loads(r.stdout)
        except json.JSONDecodeError:
            return None
        for d in (doc.get("files") or [{}])[0].get("definitions") or []:
            if d.get("name") == name and d.get("kind") in method_kinds:
                ls = d.get("line_start", 0)
                le = d.get("line_end", 0)
                if not ls or not le:
                    return None
                src_lines = code.splitlines(keepends=True)
                return "".join(
                    src_lines[max(0, ls - 1):min(len(src_lines), le)]
                )
        return None

    snippet = fixture.get("snippet", "") or ""

    if len(method_defs) == 1:
        method_name = method_defs[0][0]
    else:
        # Multiple methods in the snippet: pick the one whose body
        # differs from the original (the "changed" method).
        candidates: list[str] = []
        for n, _k in method_defs:
            if n not in orig_resolvable:
                continue
            snip_body = _body_for_name(snippet, n, ext)
            orig_body = _body_for_name(original, n, ext)
            if snip_body is None or orig_body is None:
                continue
            # Whitespace-normalized compare — class-indented vs flat
            # would otherwise always diff.
            def _norm(s: str) -> str:
                return "\n".join(ln.strip() for ln in s.splitlines() if ln.strip())
            if _norm(snip_body) != _norm(orig_body):
                candidates.append(n)
        if len(candidates) != 1:
            return None
        method_name = candidates[0]

    if method_name not in orig_resolvable:
        return None

    # Re-parse snippet to locate the method's exact line range.
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=ext, delete=False, encoding="utf-8"
    ) as tmp:
        tmp.write(snippet)
        tmp_path = tmp.name
    try:
        proc = subprocess.run(
            ["tldr", "structure", tmp_path, "--format", "compact"],
            capture_output=True, text=True, timeout=15,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    finally:
        Path(tmp_path).unlink(missing_ok=True)
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None

    files = data.get("files") or []
    if not files:
        return None
    defs = files[0].get("definitions") or []
    method_range: tuple[int, int] | None = None
    for d in defs:
        if d.get("name") == method_name and d.get("kind") in method_kinds:
            ls = d.get("line_start", 0)
            le = d.get("line_end", 0)
            if ls and le:
                method_range = (ls, le)
                break
    if method_range is None:
        return None

    lines = snippet.splitlines(keepends=True)
    ls, le = method_range
    method_lines = lines[max(0, ls - 1):min(len(lines), le)]
    method_body = "".join(method_lines)
    if not method_body.strip():
        return None

    # De-indent by the method's leading whitespace (same prefix for all
    # its lines, which is how class-member snippets are formatted).
    de_indented_lines: list[str] = []
    # Determine common leading-whitespace prefix across non-blank lines.
    prefix: str | None = None
    for line in method_lines:
        if not line.strip():
            continue
        lead = line[: len(line) - len(line.lstrip())]
        if prefix is None:
            prefix = lead
        else:
            # Longest common prefix.
            common = 0
            for a, b in zip(prefix, lead, strict=False):
                if a == b:
                    common += 1
                else:
                    break
            prefix = prefix[:common]
    prefix = prefix or ""
    for line in method_lines:
        if line.startswith(prefix):
            de_indented_lines.append(line[len(prefix):])
        else:
            de_indented_lines.append(line)
    method_snippet = "".join(de_indented_lines)
    if not method_snippet.endswith("\n"):
        method_snippet += "\n"

    # If the class/struct body ALSO differs between snippet and
    # original (e.g. Go/Rust fixtures that swap a struct field type
    # alongside a method body), emit a second batch element targeting
    # the class itself. The class snippet is the top-level def slice
    # from the fixture.snippet, verbatim (no de-indent needed —
    # top-level defs are already at column 0).
    class_name = class_defs[0][0]
    class_kind = class_defs[0][1]
    batch_entries: list[dict] = []
    # Only add a class-level entry when the class def exists in the
    # original AND the snippet version differs. For "simple"
    # class_method cases (method-only change), we skip this to keep
    # the batch minimal.
    if class_name in orig_resolvable:
        # Re-parse snippet defs for line range of class
        snippet_class_range: tuple[int, int] | None = None
        original_class_range: tuple[int, int] | None = None
        for tmp_code, target in [(snippet, "snippet"), (original, "original")]:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=ext, delete=False, encoding="utf-8"
            ) as t:
                t.write(tmp_code)
                p = t.name
            try:
                r = subprocess.run(
                    ["tldr", "structure", p, "--format", "compact"],
                    capture_output=True, text=True, timeout=10,
                )
                if r.returncode == 0:
                    try:
                        dd = json.loads(r.stdout)
                        for dd_def in (dd.get("files") or [{}])[0].get("definitions") or []:
                            if (
                                dd_def.get("name") == class_name
                                and dd_def.get("kind") == class_kind
                            ):
                                ls_ = dd_def.get("line_start", 0)
                                le_ = dd_def.get("line_end", 0)
                                if ls_ and le_:
                                    rng = (ls_, le_)
                                    if target == "snippet":
                                        snippet_class_range = rng
                                    else:
                                        original_class_range = rng
                                break
                    except json.JSONDecodeError:
                        pass
            except (subprocess.TimeoutExpired, FileNotFoundError):
                pass
            finally:
                Path(p).unlink(missing_ok=True)
        # For structs/enums (Go/Rust) the class_kind body is the small
        # top-level shell. Slice it from the snippet and diff against
        # the original slice. Only include if they actually differ.
        if (
            snippet_class_range is not None
            and original_class_range is not None
        ):
            s_ls, s_le = snippet_class_range
            snip_slice = "".join(
                snippet.splitlines(keepends=True)[
                    max(0, s_ls - 1):s_le
                ]
            )
            o_ls, o_le = original_class_range
            orig_slice = "".join(
                original.splitlines(keepends=True)[
                    max(0, o_ls - 1):o_le
                ]
            )
            # For data-only defs (struct/enum, and Go's `type X struct`
            # which tldr reports with kind='class') the snippet slice
            # covers the full def and a diff is meaningful. For truly
            # class-with-methods kinds (Python/Java/etc), the line
            # range spans the whole class including methods that the
            # snippet replaces with a marker; a diff would
            # false-positive. We gate on: the original class line
            # range is "small" (≤ a few lines) OR the snippet class
            # line range matches the original (both data-only).
            orig_span = o_le - o_ls + 1
            snip_span = s_le - s_ls + 1
            data_like = (
                class_kind in {"struct", "enum"}
                or (orig_span <= 4 and snip_span <= 4)
            )
            if data_like and snip_slice and snip_slice != orig_slice:
                batch_entries.append(
                    {
                        "kind": "replace",
                        "name": class_name,
                        "snippet": snip_slice,
                    }
                )

    batch_entries.append(
        {
            "kind": "replace",
            "name": method_name,
            "snippet": method_snippet,
        }
    )
    # Inject target_batch into the fixture dict so it is persisted on
    # write. The top-level `snippet` field is NOT used in batch mode,
    # so we leave it intact for auditability.
    fixture["target_batch"] = batch_entries
    return Annotation(
        path=path, target_kind="batch", target_name=None,
        snippet_rewritten=False,
    )


# ─── YAML writer preserving key order ──────────────────────────────────


_YAML_KEY_ORDER = (
    "name",
    "pattern",
    "language",
    "original_code",
    "snippet",
    "expected_output",
    "target_kind",
    "target_name",
    "target_batch",
    "authoring_notes",
)


class _LiteralStr(str):
    """str subclass so yaml emits ``|`` block scalar for multi-line strings."""


def _literal_representer(dumper, data):
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")


yaml.add_representer(_LiteralStr, _literal_representer)


def _to_literal_if_multiline(value):
    """Promote multi-line strings to _LiteralStr so yaml dumps them as |."""
    if isinstance(value, str) and "\n" in value:
        # Preserve trailing newline if present.
        return _LiteralStr(value)
    return value


def _reorder_for_dump(fixture: dict) -> dict:
    """Return a new dict with keys in canonical order."""
    out: dict = {}
    for k in _YAML_KEY_ORDER:
        if k in fixture:
            out[k] = _to_literal_if_multiline(fixture[k])
    # Append any unexpected keys at the end (defensive; shouldn't happen).
    for k, v in fixture.items():
        if k not in out:
            out[k] = _to_literal_if_multiline(v)
    return out


def _write_yaml(path: Path, fixture: dict) -> None:
    """Write ``fixture`` back to ``path`` with canonical key order."""
    ordered = _reorder_for_dump(fixture)
    with path.open("w", encoding="utf-8") as fh:
        yaml.dump(
            ordered,
            fh,
            sort_keys=False,
            allow_unicode=True,
            default_flow_style=False,
            width=1_000_000,  # don't wrap long signature lines
        )


# ─── Driver ────────────────────────────────────────────────────────────


def annotate_file(path: Path, dry_run: bool = False) -> Annotation:
    """Annotate one YAML fixture; returns the Annotation produced."""
    with path.open("r", encoding="utf-8") as fh:
        fixture = yaml.safe_load(fh)

    # Strip any pre-existing target_batch so a re-annotation that no
    # longer picks the batch path doesn't leave stale metadata behind.
    # derive_annotation's batch path re-injects target_batch when
    # applicable.
    fixture.pop("target_batch", None)

    ann = derive_annotation(fixture, path)
    fixture["target_kind"] = ann.target_kind
    fixture["target_name"] = ann.target_name

    if not dry_run:
        _write_yaml(path, fixture)
    return ann


def _discover(root: Path) -> list[Path]:
    if root.is_file() and root.suffix == ".yaml":
        return [root]
    return sorted(
        p for p in root.rglob("*.yaml") if not p.name.startswith("_")
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Annotate fixtures with target_kind/target_name for MCP-mode benchmark."
    )
    parser.add_argument("root", type=Path, help="Fixtures directory or single YAML file.")
    parser.add_argument("--dry-run", action="store_true", help="Print actions, do not write.")
    args = parser.parse_args(argv)

    files = _discover(args.root)
    if not files:
        print(f"ERROR: no YAML fixtures found under {args.root}", file=sys.stderr)
        return 1

    per_pattern: dict[str, dict[str, int]] = {}
    ambiguous: list[Annotation] = []
    rewrote_snippets = 0

    zero_counts = {"replace": 0, "after": 0, "none": 0, "batch": 0}
    for path in files:
        ann = annotate_file(path, dry_run=args.dry_run)
        # Reload for pattern (annotate_file wrote it, but we need the field).
        with path.open("r", encoding="utf-8") as fh:
            fixture = yaml.safe_load(fh)
        pattern = fixture.get("pattern", "?")
        per_pattern.setdefault(pattern, dict(zero_counts))
        per_pattern[pattern][ann.target_kind] = (
            per_pattern[pattern].get(ann.target_kind, 0) + 1
        )
        if ann.snippet_rewritten:
            rewrote_snippets += 1
        if ann.ambiguous_reason:
            ambiguous.append(ann)

    # ── Summary ──────────────────────────────────────────────────────
    totals: dict[str, int] = {"replace": 0, "after": 0, "none": 0, "batch": 0}
    for counts in per_pattern.values():
        for k, v in counts.items():
            totals[k] = totals.get(k, 0) + v

    print()
    print(f"Annotated {len(files)} fixture(s).")
    print()
    print("Per-pattern breakdown:")
    for pattern in sorted(per_pattern):
        c = per_pattern[pattern]
        print(
            f"  {pattern:<20s}  replace={c.get('replace', 0):3d}  "
            f"after={c.get('after', 0):3d}  "
            f"batch={c.get('batch', 0):3d}  "
            f"none={c.get('none', 0):3d}"
        )
    print()
    print(
        f"Totals: {totals['replace']} → replace, "
        f"{totals['after']} → after, "
        f"{totals['batch']} → batch, "
        f"{totals['none']} → none"
    )
    print(f"Snippet rewrites (after-mode): {rewrote_snippets}")
    if ambiguous:
        print()
        print(f"AMBIGUOUS ({len(ambiguous)}) — left as target_kind='none':")
        for a in ambiguous:
            print(f"  - {a.path.relative_to(args.root) if args.root.is_dir() else a.path}: {a.ambiguous_reason}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
