#!/usr/bin/env python3
"""Exa AI Search - Semantic web search with clean text extraction.

Exa uses neural search (embeddings) rather than keyword matching,
returning high-quality results with clean extracted text content.

Default: highlights + summary (token-efficient, ~300-500 chars/result).
Use --text for full page text, --no-contents for metadata only.

Methods:
- search: Find URLs + highlights + summary (default)
- findSimilar: Find pages similar to a given URL
- getContents: Extract clean text from specific URLs

Usage:
  # Search with highlights + summary (default, token-efficient)
  python tools/exa_search.py \
    --search "best practices for LLM context engineering"

  # Search with full text (when you need complete content)
  python tools/exa_search.py \
    --search "rust async patterns" --text

  # Search metadata only (fastest, no content extraction)
  python tools/exa_search.py \
    --search "rust async patterns" --no-contents

  # Find pages similar to a URL
  python tools/exa_search.py \
    --similar "https://docs.anthropic.com/en/docs/build-with-claude/tool-use"

  # Extract clean text from specific URLs
  python tools/exa_search.py \
    --extract "https://exa.ai/docs/sdks/typescript-sdk-specification"

  # Filter by domain, category, or date
  python tools/exa_search.py \
    --search "transformer architecture" --category "research paper" --num 5

  # Deep reasoning search (slower, higher quality)
  python tools/exa_search.py \
    --search "novel approaches to KV cache compression" --type deep-reasoning

Requires: EXA_API_KEY in environment or ~/.claude/.env
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path

# Search results routinely contain emoji and zero-width characters. A Windows
# console defaults to cp1252, so printing them raises UnicodeEncodeError and
# kills the run after the request has already been made. Force UTF-8 and never
# fail on a character we cannot represent.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

EXA_API_URL = "https://api.exa.ai"


def load_api_key() -> str:
    """Load API key from environment or ~/.claude/.env."""
    api_key = os.environ.get("EXA_API_KEY", "")

    if not api_key:
        for env_path in [Path.home() / ".claude" / ".env", Path.cwd() / ".env"]:
            if env_path.exists():
                with open(env_path) as f:
                    for line in f:
                        line = line.strip()
                        if line.startswith("EXA_API_KEY="):
                            api_key = line.split("=", 1)[1].strip().strip('"').strip("'")
                            break
            if api_key:
                break

    return api_key


def get_headers(api_key: str) -> dict:
    return {
        "x-api-key": api_key,
        "Content-Type": "application/json",
    }


def parse_args():
    parser = argparse.ArgumentParser(
        description="Semantic web search via Exa AI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Search types:
  auto             Let Exa choose (default)
  instant          Fastest, simple factual queries
  fast             Quick keyword-like search
  deep             High quality neural search
  deep-reasoning   Highest quality, slower

Categories (optional filter):
  "research paper", "company", "news", "github", "tweet",
  "personal site", "pdf", "linkedin profile"

Examples:
  %(prog)s --search "how to implement RAG with PostgreSQL"
  %(prog)s --search "CUDA kernel optimization" --category "research paper"
  %(prog)s --similar "https://arxiv.org/abs/2301.00001" --num 5
  %(prog)s --extract "https://docs.example.com/api"
        """,
    )

    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--search", metavar="QUERY", help="Search with text content extraction")
    group.add_argument("--similar", metavar="URL", help="Find pages similar to URL")
    group.add_argument(
        "--extract", metavar="URLS", nargs="+", help="Extract clean text from URLs"
    )

    parser.add_argument("--num", type=int, default=5, help="Number of results (default: 5)")
    parser.add_argument(
        "--type",
        choices=["auto", "instant", "fast", "deep", "deep-reasoning"],
        default="auto",
        help="Search type (default: auto)",
    )
    parser.add_argument("--category", help="Filter by category (e.g. 'research paper', 'github')")
    parser.add_argument("--domains", nargs="+", help="Limit to specific domains")
    parser.add_argument("--max-chars", type=int, default=1500, help="Max chars per result text (default: 1500)")
    parser.add_argument("--highlight-chars", type=int, default=2000, help="Max chars for highlights (default: 2000)")
    parser.add_argument("--no-contents", action="store_true", help="Skip content extraction (metadata only)")
    parser.add_argument("--text", action="store_true", help="Include full page text (off by default)")
    parser.add_argument("--no-highlights", action="store_true", help="Disable highlighted snippets")
    parser.add_argument("--no-summary", action="store_true", help="Disable AI-generated summary")
    parser.add_argument("--start-date", help="Filter: published after (YYYY-MM-DD)")
    parser.add_argument("--end-date", help="Filter: published before (YYYY-MM-DD)")

    args_to_parse = [arg for arg in sys.argv[1:] if not arg.endswith(".py")]
    return parser.parse_args(args_to_parse)


async def exa_search(
    query: str,
    num_results: int = 5,
    search_type: str = "auto",
    category: str = None,
    domains: list = None,
    max_chars: int = 1500,
    highlight_chars: int = 2000,
    with_text: bool = False,
    highlights: bool = True,
    summary: bool = True,
    start_date: str = None,
    end_date: str = None,
) -> dict:
    """Search via Exa API, optionally with content extraction."""
    import aiohttp

    api_key = load_api_key()
    if not api_key:
        return {"error": "EXA_API_KEY not found in environment or ~/.claude/.env"}

    payload = {
        "query": query,
        "numResults": num_results,
        "type": search_type,
    }

    has_contents = with_text or highlights or summary
    if has_contents:
        contents = {}
        if with_text:
            contents["text"] = {"maxCharacters": max_chars}
        else:
            contents["text"] = False
        if highlights:
            contents["highlights"] = {"maxCharacters": highlight_chars, "query": query}
        if summary:
            contents["summary"] = {"query": query}
        payload["contents"] = contents

    if category:
        payload["category"] = category
    if domains:
        payload["includeDomains"] = domains
    if start_date:
        payload["startPublishedDate"] = start_date
    if end_date:
        payload["endPublishedDate"] = end_date

    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{EXA_API_URL}/search",
            headers=get_headers(api_key),
            json=payload,
            timeout=aiohttp.ClientTimeout(total=60),
        ) as response:
            if response.status != 200:
                error_text = await response.text()
                return {"error": f"API error {response.status}: {error_text}"}
            return await response.json()


async def exa_find_similar(
    url: str,
    num_results: int = 5,
    highlight_chars: int = 2000,
) -> dict:
    """Find pages similar to a URL."""
    import aiohttp

    api_key = load_api_key()
    if not api_key:
        return {"error": "EXA_API_KEY not found in environment or ~/.claude/.env"}

    payload = {
        "url": url,
        "numResults": num_results,
        "contents": {
            "text": False,
            "highlights": {"maxCharacters": highlight_chars},
            "summary": {},
        },
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{EXA_API_URL}/findSimilar",
            headers=get_headers(api_key),
            json=payload,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as response:
            if response.status != 200:
                error_text = await response.text()
                return {"error": f"API error {response.status}: {error_text}"}
            return await response.json()


async def exa_get_contents(
    urls: list[str],
    max_chars: int = 8000,
) -> dict:
    """Extract clean text from specific URLs."""
    import aiohttp

    api_key = load_api_key()
    if not api_key:
        return {"error": "EXA_API_KEY not found in environment or ~/.claude/.env"}

    payload = {
        "ids": urls,
        "text": {"maxCharacters": max_chars},
        "summary": {},
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{EXA_API_URL}/contents",
            headers=get_headers(api_key),
            json=payload,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as response:
            if response.status != 200:
                error_text = await response.text()
                return {"error": f"API error {response.status}: {error_text}"}
            return await response.json()


def print_results(results: list, show_text: bool = True, max_display: int = 800):
    """Format and print search results."""
    for i, r in enumerate(results, 1):
        title = r.get("title", "No title")
        url = r.get("url", "")
        date = r.get("publishedDate", "")
        score = r.get("score")
        author = r.get("author", "")

        print(f"**{i}. {title}**")
        print(f"   {url}")
        if author:
            print(f"   Author: {author}")
        if date:
            print(f"   Published: {date[:10]}")
        if score is not None:
            print(f"   Score: {score:.3f}")

        # Summary (compact, high signal)
        if r.get("summary"):
            print(f"   Summary: {r['summary'][:300]}")

        # Highlights (key passages)
        if r.get("highlights"):
            for h in r["highlights"][:3]:
                print(f"   > {h[:200]}")

        # Full text
        if show_text and r.get("text"):
            text = r["text"].strip()
            if max_display and len(text) > max_display:
                text = text[:max_display] + "\n   [... truncated]"
            print(f"\n{text}\n")

        print()


async def main():
    args = parse_args()

    if args.search:
        query = args.search
        if args.no_contents:
            with_text = False
            highlights = False
            summary = False
        else:
            with_text = args.text
            highlights = not args.no_highlights
            summary = not args.no_summary

        mode_parts = []
        if highlights:
            mode_parts.append("highlights")
        if summary:
            mode_parts.append("summary")
        if with_text:
            mode_parts.append("text")
        mode = " + ".join(mode_parts) if mode_parts else "metadata only"
        print(f"Exa search: {query}  [{mode}]")
        if args.type != "auto":
            print(f"  Type: {args.type}")
        if args.category:
            print(f"  Category: {args.category}")
        if args.domains:
            print(f"  Domains: {', '.join(args.domains)}")

        result = await exa_search(
            query,
            num_results=args.num,
            search_type=args.type,
            category=args.category,
            domains=args.domains,
            max_chars=args.max_chars,
            highlight_chars=args.highlight_chars,
            with_text=with_text,
            highlights=highlights,
            summary=summary,
            start_date=args.start_date,
            end_date=args.end_date,
        )

        if "error" in result:
            print(f"\nError: {result['error']}")
            sys.exit(1)

        results = result.get("results", [])
        print(f"\nFound {len(results)} results\n")
        print_results(results, show_text=with_text)

    elif args.similar:
        url = args.similar
        print(f"Finding similar to: {url}")

        result = await exa_find_similar(url, num_results=args.num, highlight_chars=args.highlight_chars)

        if "error" in result:
            print(f"\nError: {result['error']}")
            sys.exit(1)

        results = result.get("results", [])
        print(f"\nFound {len(results)} similar pages\n")
        print_results(results)

    elif args.extract:
        urls = args.extract
        print(f"Extracting content from {len(urls)} URL(s)")

        result = await exa_get_contents(urls, max_chars=args.max_chars)

        if "error" in result:
            print(f"\nError: {result['error']}")
            sys.exit(1)

        results = result.get("results", [])
        print(f"\nExtracted {len(results)} page(s)\n")
        print_results(results, max_display=args.max_chars)


if __name__ == "__main__":
    asyncio.run(main())
