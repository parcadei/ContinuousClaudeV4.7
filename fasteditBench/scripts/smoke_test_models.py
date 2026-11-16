#!/usr/bin/env python3
"""VAL-102 smoke test: verify each model emits a valid edit tool_call via OpenRouter.

Sends a trivial edit request to each of the four bench-02 models and records:
- tool_call shape (pass/drop)
- OpenRouter-reported prompt/completion tokens
- tiktoken o200k_base native counts (cross-model proxy)
- discrepancy % between reported vs native
- per-call cost + latency

Output: continuum/autonomous/fastedit-bench-02/validation/smoke-results.json
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import tiktoken

from fasteditbench.backends.openrouter_client import BudgetState, OpenRouterClient

MODELS = [
    "moonshotai/kimi-k2.6",
    "anthropic/claude-opus-4.7",
    "z-ai/glm-5.1",
    "openai/gpt-5.4",
]

EDIT_TOOL = {
    "name": "edit",
    "description": "Edit a file by replacing old_string with new_string.",
    "parameters": {
        "type": "object",
        "properties": {
            "file_path": {"type": "string"},
            "old_string": {"type": "string"},
            "new_string": {"type": "string"},
        },
        "required": ["file_path", "old_string", "new_string"],
    },
}

ORIGINAL = 'def greet(name):\n    print(f"Hello, {name}!")\n'
SYSTEM = (
    "You are a code editor. Use the `edit` tool to make the requested change. "
    "Return only the tool call."
)
USER = f"""Add a type annotation `str` to the `name` parameter in this file.

file_path: greet.py

<file>
{ORIGINAL}
</file>"""


async def smoke_one(client: OpenRouterClient, model: str) -> dict:
    msgs = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": USER},
    ]
    try:
        r = await asyncio.wait_for(
            client.invoke(model, msgs, tools=[EDIT_TOOL]), timeout=60
        )
    except Exception as e:
        return {
            "model": model,
            "status": "dropped",
            "reason": f"exception: {type(e).__name__}: {e}",
        }
    # Require at least one tool_call with name=edit and valid args
    edits = [
        tc
        for tc in r.tool_calls
        if tc.get("name") == "edit"
        and isinstance(tc.get("arguments"), dict)
        and "old_string" in tc["arguments"]
        and "new_string" in tc["arguments"]
    ]
    if not edits:
        return {
            "model": model,
            "status": "dropped",
            "reason": "no valid edit tool_call",
            "content_excerpt": r.content[:200],
            "raw_tool_calls": r.tool_calls,
        }
    # Tokenizer diff — use o200k_base as consistent cross-model proxy
    try:
        enc = tiktoken.get_encoding("o200k_base")
        prompt_text = SYSTEM + USER + json.dumps(EDIT_TOOL)
        native_input = len(enc.encode(prompt_text))
        native_output = len(enc.encode(json.dumps(list(r.tool_calls))))
    except Exception:
        native_input = None
        native_output = None
    return {
        "model": model,
        "status": "passed",
        "tool_call_sample": edits[0]["arguments"],
        "openrouter_input_tokens": r.input_tokens_openrouter,
        "openrouter_output_tokens": r.output_tokens_openrouter,
        "native_input_tokens": native_input,
        "native_output_tokens": native_output,
        "input_discrepancy_pct": (
            None
            if not native_input
            else round(
                100
                * abs(r.input_tokens_openrouter - native_input)
                / max(native_input, 1),
                1,
            )
        ),
        "output_discrepancy_pct": (
            None
            if not native_output
            else round(
                100
                * abs(r.output_tokens_openrouter - native_output)
                / max(native_output, 1),
                1,
            )
        ),
        "cost_usd": r.cost_usd,
        "latency_ms": round(r.latency_ms, 1),
    }


async def main() -> list[dict]:
    client = OpenRouterClient(
        budget=BudgetState(soft_warn_usd=5.0, hard_stop_usd=10.0)
    )
    try:
        results = []
        for m in MODELS:
            print(f"Smoking {m}...", flush=True)
            results.append(await smoke_one(client, m))
        out_path = Path(
            "../continuum/autonomous/fastedit-bench-02/validation/smoke-results.json"
        )
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(
                {
                    "models": results,
                    "cumulative_usd": client.budget.cumulative_usd,
                },
                indent=2,
            )
        )
        print(f"\nWrote {out_path}")
        print(f"Cumulative spend: ${client.budget.cumulative_usd:.4f}")
        dropped = [r for r in results if r["status"] == "dropped"]
        if dropped:
            print(
                f"\nDROPPED models: {[d['model'] for d in dropped]}",
                file=sys.stderr,
            )
        return results
    finally:
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
