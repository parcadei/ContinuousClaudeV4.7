#!/usr/bin/env python3
"""Live FastEdit demo — sends real tool-call requests through OpenRouter and
prints the token-savings delta in an asymmetric terminal layout.

Usage:
    uv run --no-sync python demo/fastedit_live.py

Requires OPENROUTER_API_KEY in ../.env (or env). Picks ten language fixtures
with visible savings and runs two real completions per fixture (native vs
fastedit payload shape), reads usage.prompt_tokens off each response, and
renders the diff in place (same screen redraws between fixtures).
"""
from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import yaml
from openai import OpenAI
from rich.console import Console, Group
from rich.columns import Columns
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from scripts.compute_tax_per_operation import extract_hunks_with_context  # noqa: E402
from scripts.compute_tax_per_operation_v4 import maybe_strip_signature  # noqa: E402
from scripts.compute_tax_per_operation_v5 import minimize_modify_inside_snippet  # noqa: E402

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

MODEL = "openai/gpt-4o-mini"
ACCENT = "bright_cyan"
RED = "#fca5a5"
GREEN = "#86efac"
DIM = "grey39"
GREY = "grey58"

# Target 10 languages — first-hit picker falls through to backups if a lang
# has no modify_inside fixture matching the target-matches-signature filter.
LANG_PLAN = [
    ("python",     []),
    ("typescript", []),
    ("rust",       []),
    ("go",         []),
    ("swift",      []),
    ("java",       []),
    ("kotlin",     []),
    ("elixir",     []),
    ("ruby",       []),
    ("csharp",     ["c", "cpp"]),  # csharp's add-guard targets a class — falls back
]


# ---------------------------------------------------------------------------
# Fixture picking
# ---------------------------------------------------------------------------

@dataclass
class Fixture:
    lang: str
    case_id: str
    pattern: str
    target_name: str
    original: str
    expected: str
    snippet: str


def _target_matches_snippet_signature(target: str, snippet: str) -> bool:
    if not target:
        return False
    first = next((ln for ln in snippet.splitlines() if ln.strip()), "")
    return target in first


def load_best_fixture(lang: str) -> Fixture | None:
    """Pick the modify_inside fixture with the best native/fastedit delta."""
    best: tuple[int, Fixture] | None = None
    lang_dir = ROOT / "fixtures" / lang
    if not lang_dir.is_dir():
        return None
    for path in sorted(lang_dir.glob("*.yaml")):
        try:
            d = yaml.safe_load(path.read_text())
        except Exception:
            continue
        if not d or d.get("target_kind") != "replace":
            continue
        snippet = d.get("snippet", "")
        if "... existing code ..." not in snippet:
            continue
        target = d.get("target_name") or ""
        if not _target_matches_snippet_signature(target, snippet):
            continue
        original = d.get("original_code", "")
        expected = d.get("expected_output", "")
        if not (original and expected):
            continue
        native_size = sum(len(o) + len(n)
                          for o, n in extract_hunks_with_context(original, expected))
        score = native_size - len(snippet)
        if best is None or score > best[0]:
            best = (
                score,
                Fixture(
                    lang=lang,
                    case_id=f"{lang}/{path.stem}",
                    pattern=d.get("pattern", ""),
                    target_name=target,
                    original=original,
                    expected=expected,
                    snippet=snippet,
                ),
            )
    return best[1] if best else None


def load_fixtures() -> list[Fixture]:
    picked: list[Fixture] = []
    used_langs: set[str] = set()
    for primary, backups in LANG_PLAN:
        for lang in (primary, *backups):
            if lang in used_langs:
                continue
            fx = load_best_fixture(lang)
            if fx:
                picked.append(fx)
                used_langs.add(lang)
                break
    return picked


# ---------------------------------------------------------------------------
# Payload builders
# ---------------------------------------------------------------------------

def build_native_payload(fx: Fixture) -> str:
    hunks = extract_hunks_with_context(fx.original, fx.expected)
    if len(hunks) == 1:
        obj = {"old_string": hunks[0][0], "new_string": hunks[0][1]}
    else:
        obj = {"edits": [{"old_string": o, "new_string": n} for o, n in hunks]}
    return json.dumps(obj, indent=2)


def build_fastedit_payload(fx: Fixture) -> str:
    stripped, _ = maybe_strip_signature(
        "modify_inside", "replace", fx.target_name, fx.snippet,
    )
    minimized, _form = minimize_modify_inside_snippet(stripped, fx.original)
    lines = minimized.splitlines(keepends=True)
    body = "".join(f"    {json.dumps(line)}\n" for line in lines)
    return (
        f"fast_edit(\n"
        f"  replace='{fx.target_name}',\n"
        f"  snippet=(\n{body}  ),\n"
        f")"
    )


# ---------------------------------------------------------------------------
# OpenRouter
# ---------------------------------------------------------------------------

def openrouter_client() -> OpenAI:
    env_path = ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise SystemExit("OPENROUTER_API_KEY not set (looked in env and .env)")
    return OpenAI(api_key=key, base_url="https://openrouter.ai/api/v1")


def count_payload_tokens(client: OpenAI, payload: str) -> int:
    resp = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": payload}],
        max_tokens=1,
        temperature=0.0,
    )
    return resp.usage.prompt_tokens


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _truncate(text: str, max_lines: int = 14) -> str:
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return text
    return "\n".join(lines[:max_lines] + [f"  … +{len(lines) - max_lines} more"])


def banner() -> Panel:
    t = Text()
    t.append("◢ ", style=ACCENT)
    t.append("FASTEDIT ", style=f"bold {ACCENT}")
    t.append("LIVE", style="bold white")
    t.append("   ────   ", style=DIM)
    t.append("native edit  →  fast_edit  ·  real token delta via openrouter",
             style=DIM)
    return Panel(t, border_style=DIM, padding=(0, 2))


_BLOCK_DIGITS = {
    "0": ["█████", "█   █", "█   █", "█   █", "█████"],
    "1": ["   █ ", "   █ ", "   █ ", "   █ ", "   █ "],
    "2": ["█████", "    █", "█████", "█    ", "█████"],
    "3": ["█████", "    █", " ████", "    █", "█████"],
    "4": ["█   █", "█   █", "█████", "    █", "    █"],
    "5": ["█████", "█    ", "█████", "    █", "█████"],
    "6": ["█████", "█    ", "█████", "█   █", "█████"],
    "7": ["█████", "    █", "   █ ", "  █  ", "  █  "],
    "8": ["█████", "█   █", "█████", "█   █", "█████"],
    "9": ["█████", "█   █", "█████", "    █", "█████"],
    ".": ["     ", "     ", "     ", "     ", "  █  "],
    "%": ["█   █", "   █ ", "  █  ", " █   ", "█   █"],
    "·": ["     ", "     ", "  █  ", "     ", "     "],
    ":": ["     ", "  █  ", "     ", "  █  ", "     "],
    " ": ["     ", "     ", "     ", "     ", "     "],
    "C": ["█████", "█    ", "█    ", "█    ", "█████"],
    "O": ["█████", "█   █", "█   █", "█   █", "█████"],
    "S": ["█████", "█    ", "█████", "    █", "█████"],
    "T": ["█████", "  █  ", "  █  ", "  █  ", "  █  "],
    "K": ["█   █", "█  █ ", "███  ", "█  █ ", "█   █"],
    "E": ["█████", "█    ", "████ ", "█    ", "█████"],
    "N": ["█   █", "██  █", "█ █ █", "█  ██", "█   █"],
}


def _block_digits(s: str, color: str, scale_space: str = "  ") -> Text:
    rows = ["", "", "", "", ""]
    for ch in s:
        art = _BLOCK_DIGITS.get(ch, ["     "] * 5)
        for i in range(5):
            rows[i] += art[i] + scale_space
    t = Text()
    for i, row in enumerate(rows):
        t.append(row, style=f"bold {color}")
        if i < 4:
            t.append("\n")
    return t


def tokens_counter(count: int | None, color: str, sending: bool) -> Group:
    """COST: <num> TOKENS in big block letters + digits.

    Renders three stacked rows (5 lines each):
        COST:
        <number>
        TOKENS
    Sending state shows '···' for the number.
    """
    cost_label = _block_digits("COST:", color)
    tokens_label = _block_digits("TOKENS", color)
    if sending:
        number = _block_digits("···", DIM)
    elif count is None:
        number = _block_digits("···", DIM)
    else:
        number = _block_digits(f"{count}", color)
    return Group(
        cost_label,
        Text(""),
        number,
        Text(""),
        tokens_label,
    )


# Lock both payload panels to the same visual height regardless of the
# body's wrapped line count.
_BODY_LINES = 10
_PANEL_HEIGHT = 38  # rows incl. title, rule, body, cost/num/tokens blocks


def _pad_body(text: str, target: int, panel_cols: int) -> str:
    """Hard-truncate wrapped lines too long for the panel, then pad with
    blank lines so the visual block is ``target`` rows tall."""
    visual: list[str] = []
    for raw in text.splitlines():
        if len(raw) <= panel_cols:
            visual.append(raw)
        else:
            visual.append(raw[: panel_cols - 1] + "…")
    if len(visual) >= target:
        kept = visual[: target - 1] + [f"  … +{len(visual) - target + 1} more"]
    else:
        kept = visual + [""] * (target - len(visual))
    return "\n".join(kept)


def payload_panel(
    title: str, body: str, tokens: int | None, color: str, sending: bool,
    inner_cols: int = 80,
) -> Panel:
    head_text = Text(f"  {title.upper()}  ", style=f"bold {color} reverse")
    rule = Text("─" * min(inner_cols, 80), style=color)
    padded = _pad_body(body, _BODY_LINES, inner_cols - 4)
    body_text = Text(padded, style="white", overflow="crop", no_wrap=True)
    counter = tokens_counter(tokens, color, sending)
    return Panel(
        Group(head_text, rule, Text(""), body_text, Text(""), counter),
        border_style=color,
        padding=(1, 2),
        height=_PANEL_HEIGHT,
    )


def fixture_header(fx: Fixture, idx: int, total: int) -> Text:
    t = Text()
    t.append(f"  {idx:02d} ──── ", style=DIM)
    t.append(fx.lang.upper(), style=f"bold {ACCENT}")
    t.append(f"    {fx.pattern}", style=DIM)
    t.append(f"    [{fx.case_id}]", style=GREY)
    return t


def savings_panel(native: int | None, fe: int | None) -> Panel | Text:
    if native is None or fe is None:
        return Text("")
    saved = native - fe
    pct = (saved / native * 100) if native else 0
    big_pct = _block_digits(f"{pct:.1f}%", ACCENT)
    label = Text("saved", style=DIM, justify="center")
    saved_tok = Text()
    saved_tok.append(f"{saved}", style=f"bold {ACCENT}")
    saved_tok.append(" tok", style=DIM)
    saved_tok.justify = "center"
    return Panel(
        Group(label, Text(""), big_pct, Text(""), saved_tok),
        border_style=ACCENT,
        padding=(1, 4),
    )


def _side_by_side(left, right, console_width: int):
    """Force two renderables side-by-side in equal halves using Table.grid,
    which honors fractional column widths reliably."""
    grid = Table.grid(expand=True, padding=(0, 1))
    half = max((console_width - 4) // 2, 30)
    grid.add_column(width=half, overflow="crop")
    grid.add_column(width=half, overflow="crop")
    grid.add_row(left, right)
    return grid


def render_stage(
    fx: Fixture,
    idx: int,
    total: int,
    native_body: str,
    fastedit_body: str,
    native_tok: int | None,
    fe_tok: int | None,
    native_sending: bool,
    fastedit_visible: bool,
    fastedit_sending: bool,
    console_width: int,
) -> Group:
    """Single-screen renderable for one fixture at a given stage."""

    half_cols = max((console_width - 6) // 2, 40)
    native = payload_panel(
        "◢ native edit payload",
        native_body,
        native_tok,
        RED,
        sending=native_sending,
        inner_cols=half_cols,
    )

    fastedit = payload_panel(
        "◤ fastedit payload",
        fastedit_body if fastedit_visible else "",
        fe_tok,
        GREEN if fastedit_visible else DIM,
        sending=fastedit_sending,
        inner_cols=half_cols,
    )

    body = _side_by_side(native, fastedit, console_width)
    savings = savings_panel(native_tok, fe_tok)
    return Group(body, Text(""), savings)


# ---------------------------------------------------------------------------
# Final headline — big block-digit art
# ---------------------------------------------------------------------------

def big_number(s: str, color: str) -> Text:
    return _block_digits(s, color)


def final_headline(rows: list[tuple[str, int, int]]) -> Panel:
    total_native = sum(n for _, n, _ in rows)
    total_fe = sum(f for _, _, f in rows)
    saved = total_native - total_fe
    pct = (saved / total_native * 100) if total_native else 0

    # Big block-digit percentage, intentionally flush-left for asymmetry
    big = big_number(f"{pct:.1f}%", ACCENT)

    # Metadata strip, flush-right
    meta = Table.grid(padding=(0, 2))
    meta.add_column(style=DIM, justify="right")
    meta.add_column(justify="left")
    meta.add_row("languages", Text(str(len(rows)), style="bold white"))
    meta.add_row("native total", Text(f"{total_native:,} tok", style=RED))
    meta.add_row("fastedit total", Text(f"{total_fe:,} tok", style=GREEN))
    meta.add_row("saved", Text(f"{saved:,} tok", style=f"bold {ACCENT}"))
    meta.add_row("ratio", Text(f"{total_native/max(total_fe,1):.2f}×",
                               style=f"bold {ACCENT}"))

    # Per-language mini-grid
    per = Table.grid(padding=(0, 2), expand=False)
    per.add_column(style=DIM, justify="right")  # lang
    per.add_column(justify="right")             # native
    per.add_column(justify="center", style=DIM)  # arrow
    per.add_column(justify="right")             # fe
    per.add_column(justify="right", style=f"bold {ACCENT}")  # pct
    for lang, nat, fe in rows:
        p = (nat - fe) / nat * 100 if nat else 0
        per.add_row(
            lang,
            Text(f"{nat}", style=RED),
            "→",
            Text(f"{fe}", style=GREEN),
            f"{p:.1f}%",
        )

    # Asymmetric composition: big number in its own "row", then the
    # meta on the right and the per-language grid on the left.
    kicker = Text("◤ output token savings", style=DIM)
    top = Group(
        kicker,
        Text(""),
        big,
    )
    bottom = Columns([per, meta], expand=True, padding=(0, 6))

    return Panel(
        Group(top, Text(""), bottom),
        border_style=ACCENT,
        padding=(2, 4),
        title=Text(" FASTEDIT LIVE ", style=f"bold {ACCENT}"),
        title_align="left",
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    # Force-detect terminal width even when stdout is piped during capture;
    # when running interactively, Console auto-picks the real width.
    import shutil
    width = shutil.get_terminal_size((180, 40)).columns
    console = Console(highlight=False, width=max(width, 140))
    console.clear()
    console.print(banner())
    console.print()

    fixtures = load_fixtures()
    if not fixtures:
        console.print("[red]no fixtures loaded[/]")
        return 1

    client = openrouter_client()
    results: list[tuple[str, int, int]] = []

    width = console.width

    def stage(native_tok, fe_tok, native_sending, fe_visible, fe_sending,
              native_body, fastedit_body, fx, idx):
        return render_stage(
            fx, idx, len(fixtures), native_body, fastedit_body,
            native_tok, fe_tok,
            native_sending=native_sending,
            fastedit_visible=fe_visible,
            fastedit_sending=fe_sending,
            console_width=width,
        )

    with Live(console=console, refresh_per_second=8, transient=False,
              auto_refresh=False) as live:
        for idx, fx in enumerate(fixtures, 1):
            native_body = build_native_payload(fx)
            fastedit_body = build_fastedit_payload(fx)

            # Stage 1: both panels visible, native sending
            live.update(stage(None, None, True, True, False,
                              native_body, fastedit_body, fx, idx),
                        refresh=True)
            native_tok = count_payload_tokens(client, native_body)

            live.update(stage(native_tok, None, False, True, False,
                              native_body, fastedit_body, fx, idx),
                        refresh=True)
            time.sleep(0.35)

            # Stage 2: fastedit sending
            live.update(stage(native_tok, None, False, True, True,
                              native_body, fastedit_body, fx, idx),
                        refresh=True)
            fe_tok = count_payload_tokens(client, fastedit_body)

            # Stage 3: both tokens + savings
            live.update(stage(native_tok, fe_tok, False, True, False,
                              native_body, fastedit_body, fx, idx),
                        refresh=True)
            results.append((fx.lang, native_tok, fe_tok))
            time.sleep(1.5)

    console.print()
    console.print(final_headline(results))
    console.print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
