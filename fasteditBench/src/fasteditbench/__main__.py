"""CLI entry point for the fasteditbench harness (VAL-203).

Usage::

    fasteditbench run --adapter null --lang all
    fasteditbench run --adapter null --lang python
    python -m fasteditbench run --adapter null --lang all

Exit codes:

    0 — success
    1 — runtime error (message on stderr)
    2 — bad CLI arguments (argparse's default)
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from fasteditbench.runner import get_adapter, run_all

# Mirror the 13-language allowlist used elsewhere in the project. Kept
# local so `__main__` doesn't reach into `runner`'s private constant.
_LANGUAGES: tuple[str, ...] = (
    "python", "rust", "go", "typescript", "java", "kotlin", "csharp",
    "c", "cpp", "ruby", "swift", "php", "elixir",
)


def _build_parser() -> argparse.ArgumentParser:
    """Construct the argparse parser for the ``fasteditbench`` CLI."""
    parser = argparse.ArgumentParser(
        prog="fasteditbench",
        description="FastEdit benchmark harness.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser(
        "run",
        help="Run the benchmark against one adapter over one or all languages.",
    )
    run_parser.add_argument(
        "--adapter",
        required=True,
        help="Adapter name (e.g. 'null').",
    )
    run_parser.add_argument(
        "--lang",
        default="all",
        choices=("all", *_LANGUAGES),
        help="Language slug to run, or 'all' (default: all).",
    )
    run_parser.add_argument(
        "--fixtures-dir",
        type=Path,
        default=Path("fixtures"),
        help="Fixtures root (default: fixtures/).",
    )
    run_parser.add_argument(
        "--results-dir",
        type=Path,
        default=Path("results"),
        help="Results root (default: results/).",
    )
    run_parser.set_defaults(func=_cmd_run)
    return parser


def _cmd_run(args: argparse.Namespace) -> int:
    """Handle the ``run`` subcommand."""
    try:
        adapter = get_adapter(args.adapter)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    langs: list[str] | None = None if args.lang == "all" else [args.lang]

    try:
        out_path = asyncio.run(
            run_all(
                adapter=adapter,
                adapter_name=args.adapter,
                fixtures_dir=args.fixtures_dir,
                langs=langs,
                results_dir=args.results_dir,
            )
        )
    except Exception as exc:  # noqa: BLE001 - top-level guard so we can set exit 1
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print(str(out_path))
    # write_summary (called inside run_all) produces a sibling file.
    # Emit it on stderr so the canonical stdout stays a single path
    # (preserves the test_cli_run_null_all stdout contract).
    summary_path = out_path.with_suffix(".summary.json")
    if summary_path.exists():
        print(f"summary: {summary_path}", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    """Entry point used by the ``fasteditbench`` console script and ``-m``."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
