#!/usr/bin/env python3
"""Validate fastedit-bench-01 fixture YAML cases.

Checks performed per case:

    1. Schema: required fields present with correct types.
    2. Pattern allowlist: `pattern` is one of the 22 slugs from PATTERNS.md.
    3. Language allowlist: `language` is one of the 13 target slugs AND
       matches the parent directory name.
    4. tree-sitter parse check: `expected_output` parses cleanly under its
       language's tree-sitter grammar (VAL-101).

Usage:

    python _validate.py <fixtures_dir>          # validate all *.yaml recursively
    python _validate.py --self-test             # internal self-tests
    python _validate.py <dir> --strict          # treat 'deferred' as failure

Exit codes:

    0 — all cases pass (deferred cases allowed unless --strict)
    1 — one or more cases failed validation
    2 — self-test failed (implementation bug in this validator)

The allowlists are hard-coded frozensets at the top of this module — we
intentionally do NOT parse `PATTERNS.md` at runtime, so the validator has
zero dependency on markdown rendering quirks (see PATTERNS.md § "Adding a
new pattern" for the extension protocol).
"""

from __future__ import annotations

import argparse
import importlib
import sys
import tempfile
import textwrap
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# ── Allowlists ────────────────────────────────────────────────────────

PATTERNS_ALLOWLIST: frozenset[str] = frozenset({
    "add_guard",
    "add_line",
    "change_line",
    "wrap_block",
    "add_at_end",
    "replace_block",
    "change_signature",
    "multi_insert",
    "class_method",
    "two_markers",
    "delete_lines",
    "full_replace",
    "add_branch",
    "extend_literal",
    "add_decorator",
    "rename_variable",
    "reorder_statements",
    "extract_variable",
    "inline_variable",
    "change_expression",
    "add_parameter",
    "remove_parameter",
})

LANGUAGES_ALLOWLIST: frozenset[str] = frozenset({
    "python", "rust", "go", "typescript", "java", "kotlin", "csharp",
    "c", "cpp", "ruby", "swift", "php", "elixir",
})

REQUIRED_FIELDS: tuple[str, ...] = (
    "name", "pattern", "language",
    "original_code", "snippet", "expected_output",
    # MCP-mode target metadata (VAL-303). Every fixture records how an
    # agent would invoke fast_edit for this case.
    "target_kind", "target_name",
)
OPTIONAL_FIELDS: tuple[str, ...] = ("authoring_notes", "target_batch")

# Allowed values for target_kind. 'batch' added in M7 (Worker B) for
# fast_batch_edit-style multi-target edits.
TARGET_KINDS_ALLOWLIST: frozenset[str] = frozenset(
    {"replace", "after", "none", "batch"}
)

# Allowed `kind` values inside a target_batch entry.
BATCH_ENTRY_KINDS: frozenset[str] = frozenset({"replace", "after"})

# Map fixture-directory / yaml `language` slug -> tree_sitter_* module name.
# `csharp` uses the upstream package `tree_sitter_c_sharp`.
_LANG_TO_MODULE: dict[str, str] = {
    "python": "tree_sitter_python",
    "rust": "tree_sitter_rust",
    "go": "tree_sitter_go",
    "typescript": "tree_sitter_typescript",
    "java": "tree_sitter_java",
    "kotlin": "tree_sitter_kotlin",
    "csharp": "tree_sitter_c_sharp",
    "c": "tree_sitter_c",
    "cpp": "tree_sitter_cpp",
    "ruby": "tree_sitter_ruby",
    "swift": "tree_sitter_swift",
    "php": "tree_sitter_php",
    "elixir": "tree_sitter_elixir",
}


# ── Result types ──────────────────────────────────────────────────────

@dataclass
class CaseResult:
    """Outcome of validating one YAML file."""
    path: Path
    status: str       # "pass" | "fail" | "defer"
    messages: list[str]

    def is_failure(self) -> bool:
        return self.status == "fail"


# ── tree-sitter grammar loading ───────────────────────────────────────

_language_cache: dict[str, Any] = {}
_parser_cache: dict[str, Any] = {}
_unavailable_langs: set[str] = set()


def _load_language(lang: str) -> Any | None:
    """Load a tree-sitter language binding, caching results.

    Returns None if the grammar is not installed (caller treats as
    'deferred', not 'failed'). Prints a one-time warning per language.
    """
    if lang in _language_cache:
        return _language_cache[lang]
    if lang in _unavailable_langs:
        return None

    module_name = _LANG_TO_MODULE.get(lang)
    if module_name is None:
        # No mapping at all (e.g. elixir) — defer.
        _unavailable_langs.add(lang)
        print(f"  WARN: no tree-sitter grammar configured for '{lang}' — deferring parse check.")
        return None

    try:
        import tree_sitter  # local import so --self-test scaffolding works without deps
    except ImportError:
        _unavailable_langs.add(lang)
        print(f"  WARN: tree-sitter not installed — deferring parse check for '{lang}'.")
        return None

    try:
        mod = importlib.import_module(module_name)
    except ImportError:
        _unavailable_langs.add(lang)
        print(f"  WARN: module '{module_name}' not installed — deferring parse check for '{lang}'.")
        return None

    try:
        if lang == "typescript":
            ts_lang = tree_sitter.Language(mod.language_typescript())
        elif lang == "php":
            ts_lang = tree_sitter.Language(mod.language_php())
        else:
            ts_lang = tree_sitter.Language(mod.language())
    except Exception as exc:
        _unavailable_langs.add(lang)
        print(f"  WARN: failed to load grammar '{module_name}' for '{lang}' ({exc!r}) — deferring parse check.")
        return None

    _language_cache[lang] = ts_lang
    return ts_lang


def _get_parser(lang: str) -> Any | None:
    """Return a cached tree-sitter parser for `lang`, or None if unavailable."""
    if lang in _parser_cache:
        return _parser_cache[lang]
    ts_lang = _load_language(lang)
    if ts_lang is None:
        return None
    import tree_sitter
    parser = tree_sitter.Parser(ts_lang)
    _parser_cache[lang] = parser
    return parser


def _first_error_node(node: Any) -> Any | None:
    """Depth-first search for the first ERROR node in a tree-sitter tree."""
    if node.type == "ERROR" or node.is_missing:
        return node
    for child in node.children:
        found = _first_error_node(child)
        if found is not None:
            return found
    return None


def _parse_check(source: str, lang: str) -> tuple[bool, str]:
    """Parse `source` as `lang`; return (ok, message).

    ok=True means either the grammar is available AND the source parsed
    cleanly, OR the grammar is unavailable (caller treats missing grammar
    as 'deferred' via a separate code path — this function returns
    (True, 'deferred: ...') when the parser is None).
    """
    parser = _get_parser(lang)
    if parser is None:
        return True, f"deferred: grammar for '{lang}' unavailable"
    tree = parser.parse(source.encode("utf-8"))
    root = tree.root_node
    if not root.has_error:
        return True, "parse_ok"
    err = _first_error_node(root)
    if err is None:
        return False, "parse failed (no ERROR node located — has_error=True)"
    start = err.start_point
    end = err.end_point
    try:
        snippet = source.encode("utf-8")[err.start_byte:err.end_byte].decode("utf-8", errors="replace")
    except Exception:
        snippet = "<binary>"
    snippet = snippet.replace("\n", "\\n")
    if len(snippet) > 80:
        snippet = snippet[:77] + "..."
    return False, (
        f"parse error at {start[0] + 1}:{start[1]} – {end[0] + 1}:{end[1]} "
        f"(node type: {err.type!r}, text: {snippet!r})"
    )


# ── Schema & directory checks ─────────────────────────────────────────

def _validate_schema(data: Any, path: Path) -> list[str]:
    """Return a list of schema error messages (empty = ok)."""
    errors: list[str] = []
    if not isinstance(data, dict):
        return [f"top-level YAML must be a mapping, got {type(data).__name__}"]

    # Fields that must be strings (excludes target_name which can be None).
    _str_required = tuple(f for f in REQUIRED_FIELDS if f != "target_name")

    for field in REQUIRED_FIELDS:
        if field not in data:
            errors.append(f"missing required field: '{field}'")
            continue
        if field in _str_required and not isinstance(data[field], str):
            errors.append(
                f"field '{field}' must be a string, got {type(data[field]).__name__}"
            )

    extras = set(data.keys()) - set(REQUIRED_FIELDS) - set(OPTIONAL_FIELDS)
    if extras:
        errors.append(f"unknown fields: {sorted(extras)}")

    # authoring_notes if present must be a string
    if "authoring_notes" in data and not isinstance(data["authoring_notes"], str):
        errors.append(
            f"field 'authoring_notes' must be a string if present, got "
            f"{type(data['authoring_notes']).__name__}"
        )

    # target_kind taxonomy (VAL-303)
    tk = data.get("target_kind")
    if isinstance(tk, str) and tk not in TARGET_KINDS_ALLOWLIST:
        errors.append(
            f"target_kind '{tk}' not in allowlist "
            f"{sorted(TARGET_KINDS_ALLOWLIST)}"
        )

    # target_name rules: None XOR non-empty string (VAL-303)
    if "target_name" in data:
        tn = data["target_name"]
        if tn is not None and not isinstance(tn, str):
            errors.append(
                f"field 'target_name' must be a string or null, got "
                f"{type(tn).__name__}"
            )
        elif isinstance(tn, str) and not tn.strip():
            errors.append("target_name must be a non-empty string if non-null")

    # Cross-field: target_kind='none' → target_name MUST be None; other
    # kinds → target_name MUST be a non-empty string.
    if isinstance(tk, str) and tk in TARGET_KINDS_ALLOWLIST:
        tn = data.get("target_name")
        if tk == "none" and tn is not None:
            errors.append(
                f"target_kind='none' requires target_name=null, got {tn!r}"
            )
        if tk in {"replace", "after"} and (tn is None or not str(tn).strip()):
            errors.append(
                f"target_kind='{tk}' requires a non-empty target_name, got {tn!r}"
            )
        if tk == "batch":
            # target_batch MUST be a non-empty list of dicts with
            # {kind in {replace, after}, name: str, snippet: str}.
            # target_name MUST be None (the list carries the names).
            if tn is not None:
                errors.append(
                    f"target_kind='batch' requires target_name=null, got {tn!r}"
                )
            tb = data.get("target_batch")
            if not isinstance(tb, list) or not tb:
                errors.append(
                    "target_kind='batch' requires a non-empty target_batch list"
                )
            else:
                for i, entry in enumerate(tb):
                    if not isinstance(entry, dict):
                        errors.append(
                            f"target_batch[{i}] must be a dict, got "
                            f"{type(entry).__name__}"
                        )
                        continue
                    k = entry.get("kind")
                    if k not in BATCH_ENTRY_KINDS:
                        errors.append(
                            f"target_batch[{i}].kind must be one of "
                            f"{sorted(BATCH_ENTRY_KINDS)}, got {k!r}"
                        )
                    nm = entry.get("name")
                    if not isinstance(nm, str) or not nm.strip():
                        errors.append(
                            f"target_batch[{i}].name must be a non-empty string"
                        )
                    sn = entry.get("snippet")
                    if not isinstance(sn, str) or not sn.strip():
                        errors.append(
                            f"target_batch[{i}].snippet must be a non-empty string"
                        )
                    # M12: optional preserve_siblings flag on batch entries.
                    # When true, FastEdit's chunked_merge (>= 333c651) takes
                    # a surgical-replace fast path that keeps any sibling
                    # members of the target class that the snippet omits.
                    if "preserve_siblings" in entry:
                        ps = entry["preserve_siblings"]
                        if not isinstance(ps, bool):
                            errors.append(
                                f"target_batch[{i}].preserve_siblings must be "
                                f"a bool if present, got {type(ps).__name__}"
                            )
                        elif ps and entry.get("kind") != "replace":
                            errors.append(
                                f"target_batch[{i}].preserve_siblings=true "
                                f"requires kind='replace', got "
                                f"kind={entry.get('kind')!r}"
                            )
                    # Flag unknown keys so typos surface in validation.
                    allowed_keys = {"kind", "name", "snippet", "preserve_siblings"}
                    extras_k = set(entry.keys()) - allowed_keys
                    if extras_k:
                        errors.append(
                            f"target_batch[{i}] has unknown keys: "
                            f"{sorted(extras_k)}"
                        )

    # target_batch is allowed ONLY when target_kind='batch' (prevents
    # stale list lingering after re-annotation to a non-batch kind).
    if "target_batch" in data and (
        not isinstance(tk, str) or tk != "batch"
    ):
        errors.append(
            "target_batch is only valid when target_kind='batch'"
        )

    return errors


def _validate_taxonomy(data: dict[str, Any], path: Path) -> list[str]:
    """Check pattern & language slugs and directory/language match."""
    errors: list[str] = []
    pattern = data.get("pattern")
    language = data.get("language")

    if isinstance(pattern, str) and pattern not in PATTERNS_ALLOWLIST:
        errors.append(
            f"pattern '{pattern}' is not in the 22-slug taxonomy "
            f"(see fixtures/PATTERNS.md). Allowed: "
            f"{', '.join(sorted(PATTERNS_ALLOWLIST))}"
        )

    if isinstance(language, str) and language not in LANGUAGES_ALLOWLIST:
        errors.append(
            f"language '{language}' is not in the 13-language allowlist. "
            f"Allowed: {', '.join(sorted(LANGUAGES_ALLOWLIST))}"
        )

    # Directory vs language consistency. Parent dir name must equal the
    # `language` field (both are the slug, e.g. "csharp", not "c-sharp").
    parent_name = path.parent.name
    if (
        parent_name in LANGUAGES_ALLOWLIST
        and isinstance(language, str)
        and parent_name != language
    ):
        errors.append(
            f"directory '{parent_name}' does not match language field "
            f"'{language}'. Move the file or fix the `language:` key."
        )

    return errors


# ── Per-case driver ───────────────────────────────────────────────────

def validate_case(path: Path) -> CaseResult:
    """Run all checks on a single YAML fixture file."""
    messages: list[str] = []

    try:
        import yaml
    except ImportError:
        return CaseResult(path, "fail", ["PyYAML not installed — cannot parse fixtures"])

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        return CaseResult(path, "fail", [f"could not read file: {exc}"])

    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        return CaseResult(path, "fail", [f"YAML parse error: {exc}"])

    schema_errs = _validate_schema(data, path)
    if schema_errs:
        return CaseResult(path, "fail", schema_errs)

    assert isinstance(data, dict)  # guaranteed by schema check
    tax_errs = _validate_taxonomy(data, path)
    messages.extend(tax_errs)

    language = data["language"]
    expected = data["expected_output"]

    # If taxonomy failed on `language`, skip parse check (garbage-in).
    if any("language" in m for m in tax_errs):
        return CaseResult(path, "fail", messages)

    ok, parse_msg = _parse_check(expected, language)
    if parse_msg.startswith("deferred:"):
        if messages:
            # Schema/taxonomy issues still present — fail over defer.
            return CaseResult(path, "fail", messages)
        return CaseResult(path, "defer", [parse_msg])
    if not ok:
        messages.append(f"expected_output parse check failed: {parse_msg}")
        return CaseResult(path, "fail", messages)

    if messages:
        return CaseResult(path, "fail", messages)
    return CaseResult(path, "pass", ["ok"])


def _discover_yaml(root: Path) -> list[Path]:
    """Return all *.yaml files under `root`, excluding those starting with '_'."""
    results: list[Path] = []
    for p in sorted(root.rglob("*.yaml")):
        if p.name.startswith("_"):
            continue
        results.append(p)
    return results


def run_validation(fixtures_dir: Path, strict: bool = False) -> int:
    """Validate every YAML under `fixtures_dir`; return exit code."""
    if not fixtures_dir.exists():
        print(f"ERROR: fixtures directory not found: {fixtures_dir}", file=sys.stderr)
        return 1
    if not fixtures_dir.is_dir():
        print(f"ERROR: not a directory: {fixtures_dir}", file=sys.stderr)
        return 1

    files = _discover_yaml(fixtures_dir)
    passed = 0
    failed: list[CaseResult] = []
    deferred: list[CaseResult] = []

    for path in files:
        result = validate_case(path)
        rel = path.relative_to(fixtures_dir) if path.is_relative_to(fixtures_dir) else path
        if result.status == "pass":
            passed += 1
            print(f"  PASS {rel}")
        elif result.status == "defer":
            deferred.append(result)
            print(f"  DEFER {rel} — {'; '.join(result.messages)}")
        else:
            failed.append(result)
            print(f"  FAIL {rel}")
            for msg in result.messages:
                print(f"      - {msg}")

    total = len(files)
    print()
    print(f"Summary: {total} file(s) checked — "
          f"{passed} passed, {len(failed)} failed, {len(deferred)} deferred.")

    if failed:
        return 1
    if strict and deferred:
        print("ERROR: --strict is set and deferred cases exist.", file=sys.stderr)
        return 1
    return 0


# ── Self-test ─────────────────────────────────────────────────────────

_GOOD_PY = textwrap.dedent("""\
    name: st_good_add_guard
    pattern: add_guard
    language: python
    original_code: |
      def f(xs):
          return sum(xs) / len(xs)
    snippet: |
      def f(xs):
          if not xs:
              return 0.0
          return sum(xs) / len(xs)
    expected_output: |
      def f(xs):
          if not xs:
              return 0.0
          return sum(xs) / len(xs)
    target_kind: replace
    target_name: f
    authoring_notes: |
      self-test good case.
    """)

_BAD_PATTERN = textwrap.dedent("""\
    name: st_bad_pattern
    pattern: totally_made_up_pattern
    language: python
    original_code: |
      x = 1
    snippet: |
      x = 2
    expected_output: |
      x = 2
    target_kind: none
    target_name: null
    """)

_BAD_SYNTAX = textwrap.dedent("""\
    name: st_bad_syntax
    pattern: change_line
    language: python
    original_code: |
      def f():
          return 1
    snippet: |
      def f():
          return 2
    expected_output: |
      def f(:
          return 2
    target_kind: replace
    target_name: f
    """)

_MISSING_FIELD = textwrap.dedent("""\
    name: st_missing_field
    pattern: add_guard
    language: python
    original_code: |
      x = 1
    snippet: |
      x = 2
    target_kind: none
    target_name: null
    """)


def _self_test() -> int:
    """Run the validator against hand-crafted fixtures in a tmpdir."""
    try:
        import yaml  # noqa: F401 — we need it present for the real validator
    except ImportError:
        print("SELF-TEST FAIL: PyYAML is not installed.", file=sys.stderr)
        return 2

    # Confirm at least the Python grammar is available. If not, the
    # parse-check portion can't run — we still exercise schema/taxonomy.
    py_parser_available = _get_parser("python") is not None
    if not py_parser_available:
        print("SELF-TEST WARN: tree-sitter-python unavailable; "
              "parse-check assertion will be skipped.")

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "python").mkdir()
        (root / "python" / "good.yaml").write_text(_GOOD_PY)
        (root / "python" / "bad_pattern.yaml").write_text(_BAD_PATTERN)
        (root / "python" / "missing_field.yaml").write_text(_MISSING_FIELD)
        if py_parser_available:
            (root / "python" / "bad_syntax.yaml").write_text(_BAD_SYNTAX)

        # Validate each case individually and assert expected outcomes.
        outcomes: dict[str, str] = {}
        for yaml_file in sorted(root.rglob("*.yaml")):
            r = validate_case(yaml_file)
            outcomes[yaml_file.name] = r.status

        failures: list[str] = []

        if outcomes.get("good.yaml") != "pass":
            failures.append(
                f"expected good.yaml=pass, got {outcomes.get('good.yaml')!r}"
            )
        if outcomes.get("bad_pattern.yaml") != "fail":
            failures.append(
                f"expected bad_pattern.yaml=fail, got "
                f"{outcomes.get('bad_pattern.yaml')!r}"
            )
        if outcomes.get("missing_field.yaml") != "fail":
            failures.append(
                f"expected missing_field.yaml=fail, got "
                f"{outcomes.get('missing_field.yaml')!r}"
            )
        if py_parser_available and outcomes.get("bad_syntax.yaml") != "fail":
            failures.append(
                f"expected bad_syntax.yaml=fail, got "
                f"{outcomes.get('bad_syntax.yaml')!r}"
            )

        # Also run the full run_validation() on the tmp tree — the good
        # case alone should exit 0 (no failures) if we isolate it.
        good_only = Path(tmp) / "python_good_only"
        good_only.mkdir()
        (good_only / "good.yaml").write_text(_GOOD_PY)
        rc_good = run_validation(good_only.parent / "python_good_only")
        if rc_good != 0:
            failures.append(f"run_validation on good-only returned {rc_good}, expected 0")

        if failures:
            print("SELF-TEST FAILED:", file=sys.stderr)
            for f in failures:
                print(f"  - {f}", file=sys.stderr)
            return 2

    print("SELF-TEST PASSED.")
    return 0


# ── CLI entrypoint ────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate fastedit-bench-01 fixture YAML cases.",
    )
    parser.add_argument(
        "fixtures_dir",
        nargs="?",
        help="Directory containing fixture YAML files (recursive).",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Run internal self-tests against in-memory good/bad fixtures.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Treat 'deferred' (missing grammar) cases as failures.",
    )
    args = parser.parse_args(argv)

    if args.self_test:
        try:
            return _self_test()
        except Exception:
            print("SELF-TEST CRASHED:", file=sys.stderr)
            traceback.print_exc()
            return 2

    if args.fixtures_dir is None:
        parser.error("fixtures_dir is required unless --self-test is passed")

    return run_validation(Path(args.fixtures_dir).resolve(), strict=args.strict)


if __name__ == "__main__":
    sys.exit(main())
