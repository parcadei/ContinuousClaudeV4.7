"""Token-floor adapter for the bench-02 comparator (VAL-200).

Computes the theoretical minimum output tokens each frontier model would
emit to produce an ``edit`` tool-call for the same fixture that FastEdit
handles. No real API calls are made to evaluate edits — this is a pure
local tokenization pass.

Two tokenizer backends:

* ``openai/gpt-5.4``  → ``tiktoken.get_encoding("o200k_base")``  (offline).
* ``anthropic/claude-opus-4.7`` → ``anthropic.Anthropic().messages.count_tokens``
  (free API call). If ``ANTHROPIC_API_KEY`` is missing we fall back to
  ``tiktoken.get_encoding("cl100k_base")`` as a Claude-proxy and set
  ``FloorResult.tokenizer_proxy = True``.

Payload shape — exactly what a real tool-call on the wire looks like::

    {"name":"edit","arguments":{"file_path":"...","old_string":"...","new_string":"..."}}

Serialized via ``json.dumps(..., separators=(",", ":"))`` — no whitespace.

For a fixture, ``(old_string, new_string)`` pairs are derived from the
``original_code`` and ``expected_output`` texts via minimal-diff extraction
(``difflib.SequenceMatcher``). Pure-insert / pure-delete hunks are
anchored by one adjacent equal line so the resulting ``old_string`` is
non-empty (required by the Edit tool schema). Multi-hunk fixtures emit
one tool-call payload per hunk and sum the per-hunk token counts.

The ``wrapper_overhead`` field on :class:`FloorResult` is informational:
it is the token cost of the empty-args serialized payload, recorded once
per model at adapter init. The per-fixture count is always the direct
tokenization of the full serialized payload (the wrapper is already
embedded in that JSON) — we do NOT subtract or add it to the reported
``edit_payload_tokens``. Surfacing it separately lets report consumers
see how much of each payload is fixed JSON structure versus real content.
"""

from __future__ import annotations

import difflib
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Protocol

import tiktoken

__all__ = [
    "FloorResult",
    "TokenizerFloorAdapter",
    "extract_edit_payloads",
]

logger = logging.getLogger(__name__)

# Supported OpenRouter model IDs, mapped to the native API model IDs
# (used for the Anthropic count_tokens call).
_GPT_MODEL_ID = "openai/gpt-5.4"
_OPUS_MODEL_ID = "anthropic/claude-opus-4.7"
# Native Anthropic API expects the bare model identifier (no provider
# prefix). We use the closest currently-published Opus-family id the
# count_tokens endpoint recognises; the actual tokenization is identical
# across Opus point releases.
_OPUS_NATIVE_API_MODEL = "claude-opus-4-5"

_SUPPORTED_MODELS = frozenset({_GPT_MODEL_ID, _OPUS_MODEL_ID})


# ---------------------------------------------------------------------------
# Result shape
# ---------------------------------------------------------------------------


@dataclass
class FloorResult:
    """The floor-mode measurement for one (fixture, model) pair.

    Attributes:
        edit_payload_tokens: The floor — total tokens across all hunks of
            the full serialized tool-call payload(s). This is the headline
            number the bench-02 report shows side-by-side with FastEdit's
            1,977-token total.
        tokenizer_used: Human-readable tokenizer identifier, e.g.
            ``"tiktoken:o200k_base"``, ``"anthropic.count_tokens"``, or
            ``"tiktoken:cl100k_base"`` (the fallback proxy).
        wrapper_overhead: Token cost of the empty-args serialized payload
            (``{"name":"edit","arguments":{"file_path":"","old_string":"",
            "new_string":""}}``), measured once per model at adapter init.
            Informational only — NOT added to ``edit_payload_tokens``.
        tokenizer_proxy: ``True`` only when we fell back to
            ``tiktoken:cl100k_base`` for Opus because ``ANTHROPIC_API_KEY``
            was unset. Callers / reports MUST disclose this.
        payload_bytes: ``len(payload_json_string)`` summed across hunks —
            sanity check against tokens (the classic bytes/4 heuristic).
        n_hunks: Number of tool-call payloads summed to produce
            ``edit_payload_tokens``. 1 for most fixtures.
    """

    edit_payload_tokens: int
    tokenizer_used: str
    wrapper_overhead: int
    tokenizer_proxy: bool = False
    payload_bytes: int = 0
    n_hunks: int = 1


# ---------------------------------------------------------------------------
# Payload extraction
# ---------------------------------------------------------------------------


def extract_edit_payloads(
    original: str, expected: str
) -> list[tuple[str, str]]:
    """Derive minimal ``(old_string, new_string)`` pairs that transform
    ``original`` into ``expected`` via an Edit-tool-shaped replacement.

    Uses :class:`difflib.SequenceMatcher` on the two texts split by line
    (with newlines preserved). For each non-``equal`` opcode we emit one
    ``(old_block, new_block)`` tuple. Pure inserts and pure deletes have
    at least one adjacent equal line grafted onto both sides so the
    resulting ``old_string`` is non-empty (required by the Edit tool
    schema — ``old_string == ""`` can't uniquely match anything).

    The anchor is drawn from the FOLLOWING equal block when available
    (first line of the next ``equal`` opcode); if the insert/delete is at
    the end of the file we fall back to the PRECEDING equal block (last
    line of the prior ``equal`` opcode).

    Edge cases:

    * ``original == expected`` → raises ``ValueError``. A fixture with no
      diff means a zero-token payload, which would silently skew the
      floor. The contract (VAL-301) declares this an error — surface it
      early rather than emitting a bogus empty payload downstream.
    * Completely different texts → SequenceMatcher still produces opcodes
      (usually one ``replace`` spanning the whole buffer). We return that
      as-is; the caller pays the upper-bound token cost.
    * Trailing newline handling: we use ``splitlines(keepends=True)`` so
      line boundaries are faithful and the serialized payload re-glues
      exactly the original bytes.

    Args:
        original: Pre-edit full source text.
        expected: Post-edit full source text.

    Returns:
        List of ``(old_string, new_string)`` tuples, in source order. Never
        empty (we raise on identical inputs).

    Raises:
        ValueError: if ``original == expected``.
    """
    if original == expected:
        raise ValueError("identical fixture — payload is empty")

    orig_lines = original.splitlines(keepends=True)
    exp_lines = expected.splitlines(keepends=True)

    sm = difflib.SequenceMatcher(a=orig_lines, b=exp_lines, autojunk=False)
    opcodes = sm.get_opcodes()
    hunks: list[tuple[str, str]] = []

    for idx, (tag, i1, i2, j1, j2) in enumerate(opcodes):
        if tag == "equal":
            continue

        old_start, old_end = i1, i2
        new_start, new_end = j1, j2

        # Pure inserts and pure deletes need an anchor so the Edit-tool's
        # old_string has something unique to match. For a 'replace' the
        # old block is already non-empty so no anchor is needed.
        if tag in ("insert", "delete"):
            next_equal = (
                opcodes[idx + 1]
                if idx + 1 < len(opcodes) and opcodes[idx + 1][0] == "equal"
                else None
            )
            prev_equal = (
                opcodes[idx - 1]
                if idx > 0 and opcodes[idx - 1][0] == "equal"
                else None
            )
            if next_equal is not None:
                # Graft the first line of the next equal block onto both
                # ends. Both old_end and new_end advance by one line.
                old_end = i2 + 1
                new_end = j2 + 1
            elif prev_equal is not None:
                # Trailing insert/delete: anchor on the preceding line.
                old_start = i1 - 1
                new_start = j1 - 1
            # else: no equal blocks at all (shouldn't happen since then
            # original == expected or the texts are totally disjoint, in
            # which case we'd see a single 'replace'). Leave as-is.

        old_block = "".join(orig_lines[old_start:old_end])
        new_block = "".join(exp_lines[new_start:new_end])
        hunks.append((old_block, new_block))

    # If the entire file is one non-equal opcode (fully replaced), we'll
    # have exactly one hunk with the full original + expected — that's the
    # correct upper-bound behaviour.
    return hunks


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def _serialize_tool_call(file_path: str, old: str, new: str) -> str:
    """Serialize one ``edit`` tool-call payload exactly as it'd appear on
    the wire.

    Uses ``separators=(",", ":")`` — no whitespace between tokens. Key
    order is fixed by insertion order (Python 3.7+ preserves dict order)
    to ensure the empty-args overhead measurement is byte-for-byte
    identical across invocations.
    """
    return json.dumps(
        {
            "name": "edit",
            "arguments": {
                "file_path": file_path,
                "old_string": old,
                "new_string": new,
            },
        },
        separators=(",", ":"),
    )


# ---------------------------------------------------------------------------
# Tokenizer backends
# ---------------------------------------------------------------------------


class _Tokenizer(Protocol):
    """Minimal tokenizer surface. Both backends return a token count."""

    def count(self, text: str) -> int: ...


@dataclass
class _TiktokenBackend:
    """Offline tiktoken backend."""

    encoding_name: str
    _enc: object = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._enc = tiktoken.get_encoding(self.encoding_name)

    def count(self, text: str) -> int:
        return len(self._enc.encode(text))  # type: ignore[attr-defined]


@dataclass
class _AnthropicCountTokensBackend:
    """Network-backed Anthropic ``count_tokens`` backend.

    Each ``count`` call hits ``messages.count_tokens`` on the Anthropic
    SDK. The endpoint is free of charge per current Anthropic pricing,
    but rate-limited — callers running over many fixtures should throttle
    upstream.
    """

    api_model: str
    _client: object = field(init=False, repr=False)

    def __post_init__(self) -> None:
        # Import locally so the module loads cleanly when the SDK isn't
        # installed; the adapter's Opus init explicitly guards that case.
        import anthropic

        self._client = anthropic.Anthropic()

    def count(self, text: str) -> int:
        result = self._client.messages.count_tokens(  # type: ignore[attr-defined]
            model=self.api_model,
            messages=[{"role": "user", "content": text}],
        )
        return int(result.input_tokens)


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class TokenizerFloorAdapter:
    """Computes per-fixture token floors for Opus 4.7 and GPT-5.4.

    Each enabled model gets its own tokenizer backend initialized in
    ``__init__``; the wrapper overhead is measured once at init and
    cached per-model on ``self._wrapper_overhead_by_model``. Subsequent
    :meth:`count_floor` calls do exactly one tokenization per hunk.

    Args:
        enable_opus: If ``True`` (default), initialize the Opus backend.
            When ``ANTHROPIC_API_KEY`` is unset we fall back silently to
            ``tiktoken:cl100k_base`` and set ``tokenizer_proxy=True`` on
            every Opus ``FloorResult`` returned.
        enable_gpt: If ``True`` (default), initialize the GPT backend
            (offline tiktoken o200k_base).

    Raises:
        ValueError: if both flags are ``False`` (nothing to do) or if
            neither backend could be initialized.
    """

    def __init__(
        self,
        enable_opus: bool = True,
        enable_gpt: bool = True,
    ) -> None:
        if not (enable_opus or enable_gpt):
            raise ValueError(
                "at least one of enable_opus / enable_gpt must be True"
            )

        self._backends: dict[str, _Tokenizer] = {}
        self._tokenizer_label: dict[str, str] = {}
        self._is_proxy: dict[str, bool] = {}
        self._wrapper_overhead_by_model: dict[str, int] = {}

        if enable_gpt:
            self._backends[_GPT_MODEL_ID] = _TiktokenBackend("o200k_base")
            self._tokenizer_label[_GPT_MODEL_ID] = "tiktoken:o200k_base"
            self._is_proxy[_GPT_MODEL_ID] = False

        if enable_opus:
            key_present = bool(os.environ.get("ANTHROPIC_API_KEY"))
            if key_present:
                try:
                    backend: _Tokenizer = _AnthropicCountTokensBackend(
                        _OPUS_NATIVE_API_MODEL
                    )
                    self._backends[_OPUS_MODEL_ID] = backend
                    self._tokenizer_label[_OPUS_MODEL_ID] = (
                        "anthropic.count_tokens"
                    )
                    self._is_proxy[_OPUS_MODEL_ID] = False
                except Exception as exc:
                    logger.warning(
                        "anthropic.count_tokens backend failed to init (%s); "
                        "falling back to tiktoken:cl100k_base proxy",
                        exc,
                    )
                    self._install_opus_proxy()
            else:
                logger.warning(
                    "ANTHROPIC_API_KEY not set; using tiktoken:cl100k_base "
                    "as Opus tokenizer proxy (tokenizer_proxy=True on results)"
                )
                self._install_opus_proxy()

        if not self._backends:
            raise ValueError("no tokenizer backends could be initialized")

        # Wrapper overhead: tokens of the empty-args serialized payload,
        # measured exactly once per model.
        empty_payload = _serialize_tool_call("", "", "")
        for model_id, backend in self._backends.items():
            self._wrapper_overhead_by_model[model_id] = backend.count(
                empty_payload
            )

    def _install_opus_proxy(self) -> None:
        """Install the cl100k_base fallback for Opus and flag it."""
        self._backends[_OPUS_MODEL_ID] = _TiktokenBackend("cl100k_base")
        self._tokenizer_label[_OPUS_MODEL_ID] = "tiktoken:cl100k_base"
        self._is_proxy[_OPUS_MODEL_ID] = True

    def count_floor(
        self,
        original_code: str,
        expected_output: str,
        file_path: str,
        model_id: str,
    ) -> FloorResult:
        """Compute the token floor for one (fixture, model) pair.

        Extracts the minimal ``(old_string, new_string)`` hunks from the
        diff, serializes one ``edit`` tool-call payload per hunk, and
        sums the tokenized costs under the model's native tokenizer.

        Args:
            original_code: Fixture ``original_code`` — pre-edit full file.
            expected_output: Fixture ``expected_output`` — post-edit full
                file.
            file_path: Path to embed into the serialized tool-call (real
                models would see this as the ``file_path`` argument).
            model_id: OpenRouter-style id. Must be one of
                :data:`_SUPPORTED_MODELS` and must have been enabled at
                adapter init.

        Returns:
            :class:`FloorResult` — see that dataclass' docstring for field
            semantics.

        Raises:
            ValueError: if ``model_id`` is unknown or was disabled at init,
                or if ``original_code == expected_output``.
        """
        if model_id not in _SUPPORTED_MODELS:
            raise ValueError(
                f"unknown model_id {model_id!r}; "
                f"supported: {sorted(_SUPPORTED_MODELS)}"
            )
        if model_id not in self._backends:
            raise ValueError(
                f"model_id {model_id!r} was not enabled at adapter init"
            )

        hunks = extract_edit_payloads(original_code, expected_output)
        backend = self._backends[model_id]

        total_tokens = 0
        total_bytes = 0
        for old, new in hunks:
            payload = _serialize_tool_call(file_path, old, new)
            total_tokens += backend.count(payload)
            total_bytes += len(payload)

        return FloorResult(
            edit_payload_tokens=total_tokens,
            tokenizer_used=self._tokenizer_label[model_id],
            wrapper_overhead=self._wrapper_overhead_by_model[model_id],
            tokenizer_proxy=self._is_proxy[model_id],
            payload_bytes=total_bytes,
            n_hunks=len(hunks),
        )


# Re-export a callable-style helper for consumers that prefer a single
# function entrypoint over the class-based API. Construction cost is
# dominated by the tokenizer init so callers should still cache a single
# adapter instance across many fixtures.
def make_default_adapter() -> TokenizerFloorAdapter:
    """Shortcut for ``TokenizerFloorAdapter()`` with both backends on."""
    return TokenizerFloorAdapter(enable_opus=True, enable_gpt=True)
