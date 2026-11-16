"""Tier-1 (exact-match normalized) and Tier-2 (AST-equivalence) checkers.

Implements VAL-201 from ``continuum/autonomous/fastedit-bench-01/contract.json``.
See ``DESIGN.md`` section 5 for the authoritative algorithm spec.

Public surface:

- :func:`tier1_exact_match_normalized` — FastEdit's ``_n()`` convention.
- :func:`tier2_ast_equivalent` — tree-sitter S-expression comparison with
  comment and whitespace/separator noise stripped.
- :func:`classify` — returns ``(tier1_pass, tier2_pass)`` with Tier-2
  short-circuited to ``True`` whenever Tier-1 passes.

The grammar-loader mirrors ``fixtures/_validate.py`` (same ``_LANG_TO_MODULE``
mapping, same TypeScript/PHP ``language_typescript()`` / ``language_php()``
special-cases, same module-level cache). We deliberately do NOT import from
``_validate.py`` — that's a standalone validation script, not a library.
"""

from __future__ import annotations

import importlib
from typing import Any

# ── Grammar-module mapping (copied from fixtures/_validate.py) ────────

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


# Comment node types to drop before serialising to S-expression. Start with
# the common cross-grammar set; extend per-language only when a failing test
# forces it. Any extensions must be documented here with a short note.
_COMMENT_NODE_TYPES_COMMON: frozenset[str] = frozenset({
    "comment",
    "line_comment",
    "block_comment",
})

_COMMENT_NODE_TYPES_BY_LANG: dict[str, frozenset[str]] = {
    # Per-language overrides/additions go here. Currently none of our 9
    # sample-matrix cases nor the 13 target languages have been observed
    # to need them. Kept as a seam for when a fixture proves otherwise.
}


# Anonymous punctuation tokens to treat as "whitespace-like" and skip when
# serialising. These are statement/expression separators whose presence or
# absence is a formatting choice rather than a semantic change (e.g. TS
# auto-inserted semicolons, trailing commas). Operators, keywords, and
# brackets are NOT in this set — they carry meaning.
_SKIP_ANON_TOKENS: frozenset[str] = frozenset({";", ","})


# ── Caches ────────────────────────────────────────────────────────────

_language_cache: dict[str, Any] = {}
_parser_cache: dict[str, Any] = {}


def _load_language(lang: str) -> Any:
    """Load a tree-sitter ``Language`` binding for ``lang``, with caching.

    Raises:
        ValueError: if ``lang`` is not in :data:`_LANG_TO_MODULE`.
        ImportError: if the grammar package is not installed.
    """
    if lang in _language_cache:
        return _language_cache[lang]

    module_name = _LANG_TO_MODULE.get(lang)
    if module_name is None:
        raise ValueError(f"unsupported language: {lang}")

    import tree_sitter  # local import keeps the module importable in envs
    # without tree-sitter during static analysis.

    mod = importlib.import_module(module_name)

    if lang == "typescript":
        ts_lang = tree_sitter.Language(mod.language_typescript())
    elif lang == "php":
        ts_lang = tree_sitter.Language(mod.language_php())
    else:
        ts_lang = tree_sitter.Language(mod.language())

    _language_cache[lang] = ts_lang
    return ts_lang


def _get_parser(lang: str) -> Any:
    """Return a cached tree-sitter ``Parser`` for ``lang``."""
    if lang in _parser_cache:
        return _parser_cache[lang]
    ts_lang = _load_language(lang)
    import tree_sitter

    parser = tree_sitter.Parser(ts_lang)
    _parser_cache[lang] = parser
    return parser


# ── Tier-1 ────────────────────────────────────────────────────────────


def _normalize(code: str) -> str:
    """Apply FastEdit's ``_n()`` convention.

    1. ``rstrip`` each line (drops trailing whitespace / ``\\r``).
    2. Rejoin with ``"\\n"``.
    3. ``strip`` the whole result (drops leading/trailing blank lines).

    Mirrors ``tests/test_deterministic_benchmark.py:39-40`` in the upstream
    FastEdit repo. See DESIGN.md section 5.
    """
    return "\n".join(line.rstrip() for line in code.splitlines()).strip()


def tier1_exact_match_normalized(adapter_output: str, expected_output: str) -> bool:
    """Tier-1 pass iff the two strings are equal after :func:`_normalize`."""
    return _normalize(adapter_output) == _normalize(expected_output)


# ── Tier-2 ────────────────────────────────────────────────────────────


def _comment_types_for(lang: str) -> frozenset[str]:
    return _COMMENT_NODE_TYPES_COMMON | _COMMENT_NODE_TYPES_BY_LANG.get(
        lang, frozenset()
    )


def _to_sexp(node: Any, comment_types: frozenset[str]) -> str | None:
    """Serialise ``node`` to a custom S-expression string.

    Rules:
    - Comment nodes (per ``comment_types``) are dropped entirely.
    - Whitespace-like anonymous separator tokens (``;``, ``,``) are dropped
      (see :data:`_SKIP_ANON_TOKENS`). tree-sitter never yields pure
      whitespace nodes — whitespace is consumed between tokens.
    - Other anonymous tokens (operators, keywords, brackets) are kept as
      ``("<token>")`` so e.g. ``a > b`` and ``a < b`` differ.
    - Named leaf nodes include their source text (repr'd) so e.g.
      ``(identifier 'x')`` vs ``(identifier 'y')`` differ.
    """
    if node.type in comment_types:
        return None

    if not node.is_named:
        if node.type in _SKIP_ANON_TOKENS:
            return None
        # Anonymous token: its ``type`` IS the literal source text in
        # tree-sitter (e.g. node.type == ">" for a greater-than token).
        return f'("{node.type}")'

    parts: list[str] = [node.type]
    children = list(node.children)
    if not children:
        # Named leaf — include its text so distinct identifiers/literals
        # produce distinct S-expressions.
        try:
            text = node.text.decode("utf-8", errors="replace")
        except Exception:
            text = ""
        parts.append(repr(text))
    else:
        for child in children:
            sub = _to_sexp(child, comment_types)
            if sub is not None:
                parts.append(sub)
    return "(" + " ".join(parts) + ")"


def tier2_ast_equivalent(
    adapter_output: str, expected_output: str, language: str
) -> bool:
    """Tier-2: compare stripped S-expressions for AST equivalence.

    - Parse both sources with tree-sitter's grammar for ``language``.
    - If either parse has errors (``root_node.has_error``) return ``False``.
    - Drop comment nodes and noise separators; serialise the remaining
      tree to a custom S-expression (see :func:`_to_sexp`).
    - Apply Tier-1 normalisation to both S-expr strings and compare.

    Raises:
        ValueError: if ``language`` isn't in our 13-language set.
    """
    parser = _get_parser(language)  # may raise ValueError

    tree_a = parser.parse(adapter_output.encode("utf-8"))
    tree_b = parser.parse(expected_output.encode("utf-8"))

    if tree_a is None or tree_b is None:
        return False
    root_a = tree_a.root_node
    root_b = tree_b.root_node
    if root_a.has_error or root_b.has_error:
        return False

    comment_types = _comment_types_for(language)
    sexp_a = _to_sexp(root_a, comment_types)
    sexp_b = _to_sexp(root_b, comment_types)
    if sexp_a is None or sexp_b is None:
        return False

    return _normalize(sexp_a) == _normalize(sexp_b)


# ── Primary entry point ───────────────────────────────────────────────


def classify(
    adapter_output: str, expected_output: str, language: str
) -> tuple[bool, bool]:
    """Classify a case as ``(tier1_pass, tier2_pass)``.

    Tier-2 is short-circuited to ``True`` whenever Tier-1 passes — this
    matches the DESIGN.md rule "passing Tier-1 implies passing Tier-2"
    and also avoids paying parse cost on trivially-exact cases.

    The attribute-lookup via the module namespace (``tier2_ast_equivalent``
    imported from this module) lets tests monkey-patch the symbol to prove
    short-circuit behaviour.
    """
    t1 = tier1_exact_match_normalized(adapter_output, expected_output)
    if t1:
        return True, True
    # Re-resolve through this module's globals so monkeypatching works.
    t2_fn = globals()["tier2_ast_equivalent"]
    t2 = t2_fn(adapter_output, expected_output, language)
    return False, t2
