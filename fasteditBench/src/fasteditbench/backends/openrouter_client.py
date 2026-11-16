"""OpenRouter HTTP client with budget guard (VAL-100).

Thin async wrapper around ``/api/v1/chat/completions`` on
``https://openrouter.ai``. Focused on the edit-benchmark use case:

- Sends one request per call (no streaming).
- Returns a structured :class:`OpenRouterResponse` with usage, tool_calls,
  latency, and the raw envelope for audit.
- Tracks cumulative spend in a :class:`BudgetState`; warns once at
  ``soft_warn_usd`` and raises at ``hard_stop_usd``.
- API key is read from ``OPENROUTER_API_KEY`` via ``python-dotenv`` so the
  ``.env`` at ``fasteditBench/`` root Just Works.

Tool-call parsing follows the OpenAI-compatible schema that OpenRouter
normalises all providers to: ``choices[0].message.tool_calls`` is a list
of ``{"id", "type", "function": {"name", "arguments"}}`` where
``arguments`` is a JSON-encoded string. If the arguments string fails to
parse we keep the raw text and flag ``_parse_error: True`` rather than
raise, so the adapter's retry logic can handle it as a model error.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any

import httpx
from dotenv import load_dotenv

__all__ = [
    "BudgetState",
    "OpenRouterClient",
    "OpenRouterResponse",
]


@dataclass
class OpenRouterResponse:
    """Structured response from one OpenRouter chat completion.

    Attributes:
        content: Assistant message text. Often empty when ``tool_calls``
            is non-empty — modern function-calling providers tend to emit
            tool calls alone.
        tool_calls: List of ``{"id": str, "name": str, "arguments": dict}``.
            ``arguments`` is ``{"_raw": str, "_parse_error": True}`` when
            the provider emitted a non-JSON string.
        input_tokens_openrouter: ``usage.prompt_tokens`` from the provider.
        output_tokens_openrouter: ``usage.completion_tokens``.
        cost_usd: ``usage.cost`` from OpenRouter. 0.0 when unreported.
        latency_ms: Wall-clock from request send to response receipt.
        raw: Full response JSON for audit / debugging.
    """

    content: str
    tool_calls: list[dict[str, Any]]
    input_tokens_openrouter: int
    output_tokens_openrouter: int
    cost_usd: float
    latency_ms: float
    raw: dict[str, Any]


@dataclass
class BudgetState:
    """Cumulative budget tracker shared across :class:`OpenRouterClient` calls.

    Soft-warn fires exactly once (via the ``warned`` flag) to avoid log
    spam; hard-stop raises on the next ``invoke`` after the threshold
    is crossed.
    """

    cumulative_usd: float = 0.0
    warned: bool = False
    soft_warn_usd: float = 20.0
    hard_stop_usd: float = 50.0


class OpenRouterClient:
    """Async OpenRouter HTTP client with budget enforcement.

    The client owns an ``httpx.AsyncClient``; close it via :meth:`aclose`
    when done (or reuse across a matrix run).
    """

    _ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"

    def __init__(
        self,
        api_key: str | None = None,
        budget: BudgetState | None = None,
        timeout: float = 120.0,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        """Construct a client.

        Args:
            api_key: Overrides ``OPENROUTER_API_KEY``. Raises if neither is set.
            budget: Shared budget state. Defaults to a fresh instance.
            timeout: Per-request timeout in seconds.
            transport: Optional httpx transport. Used by tests to inject
                ``httpx.MockTransport``; production callers leave it None.
        """
        load_dotenv()
        self.api_key = api_key or os.getenv("OPENROUTER_API_KEY")
        if not self.api_key:
            raise RuntimeError(
                "OPENROUTER_API_KEY not found in .env or environment"
            )
        self.budget = budget or BudgetState()
        self.timeout = timeout
        if transport is not None:
            self._client = httpx.AsyncClient(
                timeout=timeout, transport=transport
            )
        else:
            self._client = httpx.AsyncClient(timeout=timeout)

    async def invoke(
        self,
        model_id: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str = "auto",
    ) -> OpenRouterResponse:
        """Send one chat completion request and return a parsed response.

        Args:
            model_id: OpenRouter model id, e.g. ``"anthropic/claude-opus-4.7"``.
            messages: OpenAI-style list of ``{"role", "content"}`` dicts.
            tools: Optional list of function schemas in the shape
                ``{"name", "description", "input_schema"}``. We wrap them
                into the OpenRouter-required
                ``{"type": "function", "function": <schema>}`` envelope
                here so callers can keep the contract's simpler shape.
            tool_choice: ``"auto"``, ``"none"``, or a forced tool name.

        Raises:
            RuntimeError: when cumulative spend has already crossed
                :attr:`BudgetState.hard_stop_usd`.
            httpx.HTTPStatusError: on non-2xx from OpenRouter.
        """
        # Budget check BEFORE spending. Once we cross the hard stop we
        # refuse further calls even if the caller keeps trying.
        if self.budget.cumulative_usd >= self.budget.hard_stop_usd:
            raise RuntimeError(
                "Budget hard-stop: "
                f"${self.budget.cumulative_usd:.2f} >= "
                f"${self.budget.hard_stop_usd:.2f}"
            )

        body: dict[str, Any] = {"model": model_id, "messages": messages}
        if tools:
            body["tools"] = [
                {"type": "function", "function": _normalize_tool(t)}
                for t in tools
            ]
            body["tool_choice"] = tool_choice

        t0 = time.monotonic()
        resp = await self._client.post(
            self._ENDPOINT,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json=body,
        )
        latency_ms = (time.monotonic() - t0) * 1000.0
        resp.raise_for_status()
        data = resp.json()

        choice_msg = data["choices"][0]["message"]
        content = choice_msg.get("content") or ""

        tool_calls: list[dict[str, Any]] = []
        for tc in choice_msg.get("tool_calls") or []:
            fn = tc.get("function", {}) or {}
            raw_args = fn.get("arguments")
            parsed_args: dict[str, Any]
            if isinstance(raw_args, str):
                try:
                    parsed_args = json.loads(raw_args)
                    if not isinstance(parsed_args, dict):
                        # JSON-valid but wrong shape (e.g. a list): treat
                        # as parse error so the adapter can retry.
                        parsed_args = {
                            "_raw": raw_args,
                            "_parse_error": True,
                        }
                except json.JSONDecodeError:
                    parsed_args = {
                        "_raw": raw_args,
                        "_parse_error": True,
                    }
            elif isinstance(raw_args, dict):
                parsed_args = raw_args
            else:
                parsed_args = {
                    "_raw": "" if raw_args is None else str(raw_args),
                    "_parse_error": True,
                }
            tool_calls.append(
                {
                    "id": tc.get("id"),
                    "name": fn.get("name"),
                    "arguments": parsed_args,
                }
            )

        usage = data.get("usage") or {}
        cost = float(usage.get("cost") or 0.0)
        self.budget.cumulative_usd += cost
        if (
            self.budget.cumulative_usd >= self.budget.soft_warn_usd
            and not self.budget.warned
        ):
            print(
                f"Budget warn: ${self.budget.cumulative_usd:.2f} crossed "
                f"${self.budget.soft_warn_usd:.2f}",
                flush=True,
            )
            self.budget.warned = True

        return OpenRouterResponse(
            content=content,
            tool_calls=tool_calls,
            input_tokens_openrouter=int(usage.get("prompt_tokens") or 0),
            output_tokens_openrouter=int(usage.get("completion_tokens") or 0),
            cost_usd=cost,
            latency_ms=latency_ms,
            raw=data,
        )

    async def aclose(self) -> None:
        """Close the underlying httpx client."""
        await self._client.aclose()


def _normalize_tool(tool: dict[str, Any]) -> dict[str, Any]:
    """Normalize our contract tool schema to OpenAI's ``function`` shape.

    The benchmark contract uses ``{"name", "description", "input_schema"}``
    (matching Claude's Anthropic SDK shape). OpenRouter expects OpenAI's
    ``{"name", "description", "parameters"}``. Translate here so callers
    keep the simpler contract shape.
    """
    if "parameters" in tool:
        return tool
    normalized = dict(tool)
    if "input_schema" in normalized:
        normalized["parameters"] = normalized.pop("input_schema")
    return normalized
