"""Tests for the OpenRouter client (VAL-100).

These tests exercise :class:`OpenRouterClient` via an injected
``httpx.MockTransport`` so no network access is required. Each test sets
``OPENROUTER_API_KEY`` in the environment (unless the test specifically
verifies the missing-key branch) to avoid depending on ``.env``. We use
``asyncio.run`` directly rather than pytest-asyncio to keep the test
suite's dependency footprint minimal.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from fasteditbench.backends.openrouter_client import (
    BudgetState,
    OpenRouterClient,
)


def _make_response(
    *,
    tool_call_args: str | None = None,
    content: str = "",
    cost: float = 0.001,
    prompt_tokens: int = 10,
    completion_tokens: int = 5,
) -> httpx.Response:
    """Build a fake OpenRouter completion response."""
    message: dict = {"role": "assistant", "content": content}
    if tool_call_args is not None:
        message["tool_calls"] = [
            {
                "id": "call_1",
                "type": "function",
                "function": {
                    "name": "edit",
                    "arguments": tool_call_args,
                },
            }
        ]
    body = {
        "id": "chatcmpl-1",
        "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "cost": cost,
        },
    }
    return httpx.Response(200, json=body)


def _transport_returning(response: httpx.Response) -> httpx.MockTransport:
    def handler(_request: httpx.Request) -> httpx.Response:
        return response

    return httpx.MockTransport(handler)


def test_successful_tool_call_parses(monkeypatch):
    """A well-formed tool-call with valid JSON arguments parses cleanly."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    args = json.dumps(
        {"file_path": "a.py", "old_string": "foo", "new_string": "bar"}
    )
    transport = _transport_returning(_make_response(tool_call_args=args))

    async def run():
        client = OpenRouterClient(transport=transport)
        try:
            return await client.invoke(
                "anthropic/claude-opus-4.7",
                messages=[{"role": "user", "content": "edit this"}],
                tools=[
                    {
                        "name": "edit",
                        "description": "Edit a file.",
                        "input_schema": {"type": "object", "properties": {}},
                    }
                ],
            )
        finally:
            await client.aclose()

    resp = asyncio.run(run())

    assert len(resp.tool_calls) == 1
    tc = resp.tool_calls[0]
    assert tc["name"] == "edit"
    assert tc["arguments"]["file_path"] == "a.py"
    assert tc["arguments"]["old_string"] == "foo"
    assert tc["arguments"]["new_string"] == "bar"
    assert "_parse_error" not in tc["arguments"]
    assert resp.input_tokens_openrouter == 10
    assert resp.output_tokens_openrouter == 5
    assert resp.cost_usd == pytest.approx(0.001)
    assert resp.latency_ms >= 0.0


def test_malformed_tool_call_args_flag_parse_error(monkeypatch):
    """A non-JSON arguments string is surfaced via ``_parse_error: True``."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    transport = _transport_returning(
        _make_response(tool_call_args="not-json-at-all")
    )

    async def run():
        client = OpenRouterClient(transport=transport)
        try:
            return await client.invoke(
                "anthropic/claude-opus-4.7",
                messages=[{"role": "user", "content": "x"}],
                tools=[{"name": "edit", "input_schema": {}}],
            )
        finally:
            await client.aclose()

    resp = asyncio.run(run())

    assert len(resp.tool_calls) == 1
    args = resp.tool_calls[0]["arguments"]
    assert args["_parse_error"] is True
    assert args["_raw"] == "not-json-at-all"


def test_budget_soft_warn_fires_once(monkeypatch, capsys):
    """Soft-warn prints exactly once when crossing the threshold."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    budget = BudgetState(soft_warn_usd=0.0005, hard_stop_usd=100.0)
    transport = _transport_returning(_make_response(cost=0.001))

    async def run():
        client = OpenRouterClient(budget=budget, transport=transport)
        try:
            await client.invoke(
                "anthropic/claude-opus-4.7",
                messages=[{"role": "user", "content": "x"}],
            )
            # Second call should NOT re-warn (already crossed).
            await client.invoke(
                "anthropic/claude-opus-4.7",
                messages=[{"role": "user", "content": "x"}],
            )
        finally:
            await client.aclose()

    asyncio.run(run())

    captured = capsys.readouterr()
    warnings = [
        line for line in captured.out.splitlines() if "Budget warn" in line
    ]
    assert len(warnings) == 1
    assert budget.warned is True
    assert budget.cumulative_usd == pytest.approx(0.002)


def test_budget_hard_stop_raises(monkeypatch):
    """Once cumulative spend crosses hard-stop, invoke raises RuntimeError."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    budget = BudgetState(
        cumulative_usd=60.0, soft_warn_usd=20.0, hard_stop_usd=50.0
    )
    transport = _transport_returning(_make_response())

    async def run():
        client = OpenRouterClient(budget=budget, transport=transport)
        try:
            await client.invoke(
                "anthropic/claude-opus-4.7",
                messages=[{"role": "user", "content": "x"}],
            )
        finally:
            await client.aclose()

    with pytest.raises(RuntimeError, match="hard-stop"):
        asyncio.run(run())


def test_missing_api_key_raises(monkeypatch):
    """Construction fails loudly when no key is available."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    # Ensure load_dotenv doesn't silently repopulate from a .env that
    # happens to exist at the repo root during CI. The client calls
    # load_dotenv() so we stub it to a no-op for deterministic behaviour.
    monkeypatch.setattr(
        "fasteditbench.backends.openrouter_client.load_dotenv",
        lambda *a, **kw: False,
    )
    with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
        OpenRouterClient()
