#!/usr/bin/env python3
"""VAL-102.5 template A/B pilot.

10 fixtures x 2 templates (Anthropic XML vs OpenAI Markdown) x 2 models
(opus-4.7, gpt-5.4) = 40 API calls. Outputs tier-1 variance per model and
writes decision (unified_template_A | per_model_registry | incomplete) to
validation/template-pilot.json.

Budget guard: if cumulative spend since start of Task C exceeds $4,
abort with `decision: incomplete` and write partial rows.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import yaml

from fasteditbench.backends.openrouter_client import BudgetState, OpenRouterClient
from fasteditbench.tiers import tier1_exact_match_normalized

FIXTURES = [
    "python/01-add_guard",
    "python/12-change_signature",
    "ruby/05-add-at-end-process",
    "rust/02-wrap_block",
    "cpp/12-add_decorator_nodiscard",
    "java/09-class_method-init_change",
    "swift/06-class-method-store-init",
    "typescript/09-class-method-store",
    "go/12-class_method",
    "elixir/09-add-branch-grade",
]

MODELS = [
    ("opus", "anthropic/claude-opus-4.7"),
    ("gpt", "openai/gpt-5.4"),
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

# Template A: Anthropic XML-style
TEMPLATE_A_SYSTEM = (
    "You are a code editor. Apply the requested edit to the file using the `edit` tool.\n"
    "Emit one or more `edit` tool calls. Do not emit code fences, explanations, or chain-of-thought — just the tool calls.\n"
    "The `old_string` must match the file content EXACTLY (including whitespace and indentation)."
)


def template_a_user(file_path: str, snippet: str, original_code: str) -> str:
    return (
        f"Apply this edit to `{file_path}`:\n\n"
        f"<snippet>\n{snippet}\n</snippet>\n\n"
        f"Here is the current file content:\n\n"
        f"<file>\n{original_code}\n</file>"
    )


# Template B: OpenAI Markdown-style
TEMPLATE_B_SYSTEM = (
    "You are a code editor. Use the `edit` tool to apply edits to the file below. "
    "Make one or more `edit` calls. Each call replaces `old_string` with `new_string`. "
    "Do not include explanations or preambles in your response — only tool calls. "
    "The `old_string` must match the file exactly, including whitespace."
)


def template_b_user(file_path: str, snippet: str, original_code: str) -> str:
    return (
        f"## File to edit\n"
        f"Path: {file_path}\n\n"
        f"```\n{original_code}\n```\n\n"
        f"## Edit instruction\n"
        f"Apply this snippet (the `# ... existing code ...` markers indicate preserved regions):\n\n"
        f"```\n{snippet}\n```"
    )


FIXTURES_DIR = Path("fixtures")


def load_fixture(case_id: str) -> dict:
    return yaml.safe_load((FIXTURES_DIR / f"{case_id}.yaml").read_text())


def apply_edits(original: str, tool_calls: list[dict]) -> tuple[str, str | None]:
    """Apply each edit tool call sequentially. Return (final_code, error)."""
    current = original
    for tc in tool_calls:
        if tc.get("name") != "edit":
            continue
        args = tc.get("arguments")
        if not isinstance(args, dict):
            return current, "arguments_not_dict"
        if args.get("_parse_error"):
            return current, "parse_error"
        old = args.get("old_string")
        new = args.get("new_string")
        if old is None or new is None:
            return current, "missing_fields"
        if old not in current:
            return current, "old_string_not_found"
        current = current.replace(old, new, 1)
    return current, None


async def run_one(
    client: OpenRouterClient,
    case_id: str,
    fixture: dict,
    template_label: str,
    model_id: str,
) -> dict:
    # Derive a file_path from the case's language
    lang = fixture["language"]
    file_path = f"{case_id.split('/')[-1]}.{_ext(lang)}"
    original = fixture["original_code"]
    snippet = fixture["snippet"]

    if template_label == "A":
        system = TEMPLATE_A_SYSTEM
        user = template_a_user(file_path, snippet, original)
    else:
        system = TEMPLATE_B_SYSTEM
        user = template_b_user(file_path, snippet, original)

    msgs = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]

    try:
        r = await asyncio.wait_for(
            client.invoke(model_id, msgs, tools=[EDIT_TOOL]), timeout=60
        )
    except Exception as e:
        return {
            "fixture": case_id,
            "model": model_id,
            "template": template_label,
            "tier1_pass": False,
            "tokens_input": None,
            "tokens_output": None,
            "cost": 0.0,
            "error": f"exception: {type(e).__name__}: {e}",
        }

    final_code, edit_err = apply_edits(original, r.tool_calls)
    tier1 = False if edit_err else tier1_exact_match_normalized(
        final_code, fixture["expected_output"]
    )

    return {
        "fixture": case_id,
        "model": model_id,
        "template": template_label,
        "tier1_pass": tier1,
        "tokens_input": r.input_tokens_openrouter,
        "tokens_output": r.output_tokens_openrouter,
        "cost": r.cost_usd,
        "latency_ms": round(r.latency_ms, 1),
        "tool_call_count": len(r.tool_calls),
        "error": edit_err,
    }


def _ext(lang: str) -> str:
    return {
        "python": "py",
        "ruby": "rb",
        "rust": "rs",
        "cpp": "cpp",
        "c": "c",
        "java": "java",
        "swift": "swift",
        "typescript": "ts",
        "go": "go",
        "elixir": "ex",
        "kotlin": "kt",
        "csharp": "cs",
        "php": "php",
    }.get(lang, "txt")


async def main():
    # Starting cumulative spend from smoke test run — track separately since
    # we open a new client here. Budget guard: abort if TOTAL m1 spend > $4.
    prior_smoke_spend = 0.00981465  # from smoke-results.json cumulative_usd
    abort_at_usd = 4.0 - prior_smoke_spend  # budget remaining for pilot

    client = OpenRouterClient(
        budget=BudgetState(soft_warn_usd=5.0, hard_stop_usd=10.0)
    )

    pilot_runs: list[dict] = []
    aborted = False
    try:
        # Load all fixtures up front
        fixtures = {cid: load_fixture(cid) for cid in FIXTURES}

        call_count = 0
        for model_label, model_id in MODELS:
            for template in ("A", "B"):
                for case_id in FIXTURES:
                    if client.budget.cumulative_usd > abort_at_usd:
                        aborted = True
                        print(
                            f"BUDGET ABORT: pilot spend "
                            f"${client.budget.cumulative_usd:.4f} "
                            f"+ smoke ${prior_smoke_spend:.4f} > $4.00",
                            flush=True,
                        )
                        break

                    print(
                        f"[{call_count + 1}/40] {model_label} template={template} {case_id}",
                        flush=True,
                    )
                    row = await run_one(
                        client, case_id, fixtures[case_id], template, model_id
                    )
                    pilot_runs.append(row)
                    call_count += 1

                    # Periodic budget re-check every 10 calls
                    if call_count % 10 == 0:
                        total = prior_smoke_spend + client.budget.cumulative_usd
                        print(
                            f"   running total: ${total:.4f}",
                            flush=True,
                        )
                if aborted:
                    break
            if aborted:
                break

        # Summary
        summary = {}
        for model_label, model_id in MODELS:
            rows_a = [
                r
                for r in pilot_runs
                if r["model"] == model_id and r["template"] == "A"
            ]
            rows_b = [
                r
                for r in pilot_runs
                if r["model"] == model_id and r["template"] == "B"
            ]
            passes_a = sum(1 for r in rows_a if r["tier1_pass"])
            passes_b = sum(1 for r in rows_b if r["tier1_pass"])
            total_a = len(rows_a)
            total_b = len(rows_b)
            # Variance in percentage points — use counts (not fractions) if
            # partial; normalise to 10 per spec if complete.
            if total_a and total_b:
                variance_pp = abs(
                    passes_a / total_a - passes_b / total_b
                ) * 100
            else:
                variance_pp = None
            summary[model_label] = {
                "A": passes_a,
                "B": passes_b,
                "A_total": total_a,
                "B_total": total_b,
                "variance_pp": None if variance_pp is None else round(variance_pp, 2),
            }

        # Decision
        if aborted:
            decision = "incomplete"
            reason = "budget aborted partway through pilot"
        else:
            all_variances = [
                s["variance_pp"]
                for s in summary.values()
                if s["variance_pp"] is not None
            ]
            if not all_variances:
                decision = "incomplete"
                reason = "no complete variance data"
            elif all(v <= 5.0 for v in all_variances):
                decision = "unified_template_A"
                reason = (
                    f"all variances <= 5pp (max {max(all_variances):.1f}pp); "
                    f"Anthropic XML template used universally"
                )
            else:
                decision = "per_model_registry"
                reason = (
                    f"variance >5pp on at least one model "
                    f"(max {max(all_variances):.1f}pp); per-model registry built"
                )

        out_path = Path(
            "../continuum/autonomous/fastedit-bench-02/validation/template-pilot.json"
        )
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(
                {
                    "pilot_runs": pilot_runs,
                    "summary": summary,
                    "decision": decision,
                    "decision_reason": reason,
                    "total_cost_usd": client.budget.cumulative_usd,
                    "cumulative_m1_usd": prior_smoke_spend
                    + client.budget.cumulative_usd,
                    "aborted": aborted,
                },
                indent=2,
            )
        )
        print(f"\nWrote {out_path}")
        print(f"Decision: {decision} — {reason}")
        print(
            f"Pilot cost: ${client.budget.cumulative_usd:.4f} | "
            f"M1 total: ${prior_smoke_spend + client.budget.cumulative_usd:.4f}"
        )
        return decision
    finally:
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
