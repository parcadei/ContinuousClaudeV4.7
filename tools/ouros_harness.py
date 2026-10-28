"""Ouros Harness — programmatic code execution with external function bridge.

Runs agent-written Python in an ouros sandbox. External functions (exa_search,
nia_docs, etc.) pause execution, call the real API outside the sandbox, and
resume with results. The model never sees raw API responses — only the computed
output from the agent's Python code.

Usage:
    # Execute code with access to research tools
    python ouros_harness.py --file /tmp/agent-code.py

    # With session persistence
    python ouros_harness.py --file code.py --session dive-auth --storage thoughts/shared/dives

    # Load a previous session and continue
    python ouros_harness.py --file code.py --session dive-auth --load

    # List variables in a session
    python ouros_harness.py --session dive-auth --list-vars

    # Get a variable as JSON
    python ouros_harness.py --session dive-auth --get-var research

    # Fork a session
    python ouros_harness.py --session dive-auth --fork approach-a
"""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# External function registry
# ---------------------------------------------------------------------------

def _load_env():
    """Load API keys from ~/.claude/.env if present."""
    env_path = Path.home() / ".claude" / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip().strip("'\""))


async def _call_exa_search(query, num_results=5, category=None, domains=None,
                           with_text=False, start_date=None):
    """Bridge to the real Exa API."""
    script_dir = Path(__file__).parent
    sys.path.insert(0, str(script_dir))
    from exa_search import exa_search, load_api_key

    api_key = load_api_key()
    if not api_key:
        return {"error": "EXA_API_KEY not found"}

    kwargs = {"query": query, "num_results": num_results, "with_text": with_text}
    if category:
        kwargs["category"] = category
    if domains and isinstance(domains, list):
        kwargs["domains"] = domains
    if start_date:
        kwargs["start_date"] = start_date

    return await exa_search(**kwargs)


def _call_exa_search_sync(*args, **kwargs):
    """Sync wrapper — ouros external functions must be sync."""
    return asyncio.run(_call_exa_search(*args, **kwargs))

async def _call_nia_search(query, repositories=None, data_sources=None,
                          search_mode="unified", include_sources=True):
    """Bridge to the real Nia documentation search API.

    Args:
        query: Search query string
        repositories: Optional list of repository identifiers
        data_sources: Optional list of documentation source IDs
        search_mode: 'unified' (default, searches everything), 'repositories', or 'data_sources'
        include_sources: Whether to include source metadata in results
    """
    script_dir = Path(__file__).parent
    sys.path.insert(0, str(script_dir))
    from nia_docs import search_query, load_api_key

    api_key = load_api_key()
    if not api_key:
        return {"error": "NIA_API_KEY not found"}

    messages = [{"role": "user", "content": query}]
    return await search_query(
        messages=messages,
        repositories=repositories,
        data_sources=data_sources,
        search_mode=search_mode,
        include_sources=include_sources,
    )


def _call_nia_search_sync(*args, **kwargs):
    """Sync wrapper — ouros external functions must be sync."""
    return asyncio.run(_call_nia_search(*args, **kwargs))

async def _call_nia_universal(query, limit=10):
    """Bridge to Nia universal search — searches all 10k+ public indexed sources."""
    script_dir = Path(__file__).parent
    sys.path.insert(0, str(script_dir))
    from nia_docs import search_universal, load_api_key

    api_key = load_api_key()
    if not api_key:
        return {"error": "NIA_API_KEY not found"}

    return await search_universal(query=query, limit=limit)


def _call_nia_universal_sync(*args, **kwargs):
    """Sync wrapper for nia_universal."""
    return asyncio.run(_call_nia_universal(*args, **kwargs))


async def _call_nia_web(query, category=None, time_range=None):
    """Bridge to Nia web search."""
    script_dir = Path(__file__).parent
    sys.path.insert(0, str(script_dir))
    from nia_docs import search_web, load_api_key

    api_key = load_api_key()
    if not api_key:
        return {"error": "NIA_API_KEY not found"}

    return await search_web(query=query, category=category, time_range=time_range)


def _call_nia_web_sync(*args, **kwargs):
    """Sync wrapper for nia_web."""
    return asyncio.run(_call_nia_web(*args, **kwargs))


async def _call_nia_package(package, query, registry="npm", limit=10):
    """Bridge to Nia package-specific semantic search.

    Args:
        package: Package name (e.g. 'express', 'fastapi')
        query: What to search for within the package
        registry: 'npm', 'py_pi', 'crates_io', 'go_modules'
        limit: Max results (default 10)
    """
    script_dir = Path(__file__).parent
    sys.path.insert(0, str(script_dir))
    from nia_docs import search_package_hybrid, load_api_key

    api_key = load_api_key()
    if not api_key:
        return {"error": "NIA_API_KEY not found"}

    return await search_package_hybrid(
        package=package, query=query, registry=registry, limit=limit
    )


def _call_nia_package_sync(*args, **kwargs):
    """Sync wrapper for nia_package."""
    return asyncio.run(_call_nia_package(*args, **kwargs))


async def _call_nia_package_grep(package, pattern, registry="npm", limit=10):
    """Bridge to Nia package regex search.

    Args:
        package: Package name (e.g. 'express', 'fastapi')
        pattern: Regex pattern to search for in package source
        registry: 'npm', 'py_pi', 'crates_io', 'go_modules'
        limit: Max results (default 10)
    """
    script_dir = Path(__file__).parent
    sys.path.insert(0, str(script_dir))
    from nia_docs import search_package_grep, load_api_key

    api_key = load_api_key()
    if not api_key:
        return {"error": "NIA_API_KEY not found"}

    return await search_package_grep(
        package=package, pattern=pattern, registry=registry, limit=limit
    )


def _call_nia_package_grep_sync(*args, **kwargs):
    """Sync wrapper for nia_package_grep."""
    return asyncio.run(_call_nia_package_grep(*args, **kwargs))


def _call_nia_help():
    """Return help text describing all available nia functions."""
    return {
        "research_package": {
            "description": "ALL-IN-ONE: Research a package for building a context block. Runs nia_search + nia_package + nia_package_grep + exa_search, filters noise, returns compact structured data. USE THIS FIRST.",
            "args": {
                "package": "Package name, e.g. 'express' (required)",
                "version": "Version string, e.g. '5.1.0' (optional, improves search)",
                "registry": "'npm', 'py_pi', 'crates_io', 'go_modules' (default 'npm')",
                "max_results": "Max results per search call (default 3)",
                "max_chars": "Max chars per result content (default 600)",
            },
            "returns": "dict with sections: nia_answer, official_docs, source_patterns, deprecations, guides",
        },
        "nia_search": {
            "description": "Search indexed docs and repositories. Returns answer + ranked results.",
            "args": {
                "query": "Search query (required)",
                "repositories": "List of repo identifiers (optional)",
                "data_sources": "List of doc source IDs (optional)",
                "search_mode": "'unified' (default, all sources), 'repositories', 'data_sources'",
                "include_sources": "Include source metadata (default True)",
            },
            "returns": "dict with 'answer' (synthesized markdown), 'results' (ranked docs with content, score, source)",
        },
        "nia_universal": {
            "description": "Search all 10k+ public indexed sources. Broader than nia_search.",
            "args": {
                "query": "Search query (required)",
                "limit": "Max results (default 10)",
            },
        },
        "nia_web": {
            "description": "Web search via Nia.",
            "args": {
                "query": "Search query (required)",
                "category": "Filter category (optional)",
                "time_range": "Time filter (optional)",
            },
        },
        "nia_package": {
            "description": "Semantic search WITHIN a specific package's source code.",
            "args": {
                "package": "Package name, e.g. 'express' (required)",
                "query": "What to search for (required)",
                "registry": "'npm', 'py_pi', 'crates_io', 'go_modules' (default 'npm')",
                "limit": "Max results (default 10)",
            },
        },
        "nia_package_grep": {
            "description": "Regex search WITHIN a specific package's source code.",
            "args": {
                "package": "Package name (required)",
                "pattern": "Regex pattern (required)",
                "registry": "'npm', 'py_pi', 'crates_io', 'go_modules' (default 'npm')",
                "limit": "Max results (default 10)",
            },
        },
        "exa_search": {
            "description": "Semantic web search via Exa AI. Good for blogs, guides, broader coverage.",
            "args": {
                "query": "Search query (required)",
                "num_results": "Number of results (default 5)",
                "category": "Filter: 'research paper', 'github', 'news', etc. (optional)",
                "domains": "List of domains to restrict to (optional)",
                "with_text": "Include full page text (default False)",
                "start_date": "Filter: published after YYYY-MM-DD (optional)",
            },
        },
        "llm_call": {
            "description": "Call an LM as a sub-query (RLM recursive call). You choose the backend explicitly. Returns text.",
            "args": {
                "prompt": "Text prompt to send (required)",
                "model": "Model ID — depends on backend. Anthropic: claude-haiku-4-5-20251001, claude-sonnet-4-6. OpenAI: gpt-4o-mini. LM Studio: whatever is loaded. OpenRouter: anthropic/claude-sonnet-4-6, etc.",
                "max_tokens": "Max response tokens (default 1000)",
                "system": "Optional system prompt",
                "temperature": "Sampling temperature (default 0.0)",
                "backend": "'anthropic' (default), 'openai', 'local' (LM Studio), 'openrouter'",
            },
            "returns": "str — the model's text response",
        },
        "agent_call": {
            "description": "Spawn a headless agent with full tool access (RLM recursive call). The agent can read/write files, run commands, iterate. Returns final output.",
            "args": {
                "prompt": "Task description for the agent (required)",
                "agent": "'claude-code' (default) or 'codex'",
                "model": "Model override: 'sonnet', 'opus', 'haiku' (optional)",
                "max_turns": "Max agent turns (default 10)",
                "timeout": "Max seconds to wait (default 300 = 5 min)",
                "cwd": "Working directory for the agent (optional)",
            },
            "returns": "str — the agent's final text output",
        },
        "read_file": {
            "description": "Read a file from the host filesystem (subject to security policy).",
            "args": {"path": "File path (required)"},
        },
        "write_file": {
            "description": "Write content to a file (subject to security policy).",
            "args": {"path": "File path (required)", "content": "Content to write (required)"},
        },
        "glob_files": {
            "description": "Find files matching a glob pattern.",
            "args": {"pattern": "Glob pattern (required)", "path": "Base directory (default '.')"},
        },
        "run_command": {
            "description": "Run a shell command (subject to security policy allowlist).",
            "args": {"cmd": "Command to run (required)", "timeout": "Timeout in seconds (default 30)"},
        },
        "nia_help": {
            "description": "Show this help text.",
        },
    }

def _call_research_package(package, version=None, registry="npm", max_results=3, max_chars=600):
    """All-in-one research function for building context blocks.

    Runs nia_search + nia_package + nia_package_grep + exa_search,
    filters noise, truncates, and returns a compact structured result.
    The agent uses this output to write the actual context block.

    Args:
        package: Package name (e.g. 'express', 'fastapi')
        version: Version string (e.g. '5.1.0') — used in search queries
        registry: 'npm', 'py_pi', 'crates_io', 'go_modules'
        max_results: Max results per search call (default 3)
        max_chars: Max chars per result content (default 600)

    Returns:
        dict with sections: nia_answer, official_docs, source_patterns,
        deprecations, guides, plus metadata (package, version_indexed, sources_used)
    """
    version_str = f" v{version}" if version else ""
    major = version.split(".")[0] if version else ""

    output = {
        "package": package,
        "version_requested": version,
        "version_indexed": None,
        "registry": registry,
        "sources_used": 0,
        "sections": {},
    }

    # 1. Structured docs via nia_search
    docs = _call_nia_search_sync(
        f"{package}{version_str} API breaking changes migration guide"
    )
    answer = docs.get("answer", "")
    if answer:
        output["sections"]["nia_answer"] = answer
        output["sources_used"] += 1

    # Filter to trusted sources only
    official_docs = []
    for r in docs.get("results", [])[:max_results]:
        src = r.get("source", {})
        display = src.get("display_name", "").lower()
        # Skip obvious noise (other packages, unrelated sites)
        if package.lower() not in display and display not in ["github.com", "npmjs.com", "pypi.org"]:
            continue
        official_docs.append({
            "source": src.get("display_name", ""),
            "doc": src.get("document_name", ""),
            "content": r.get("content", "")[:max_chars],
        })
    if official_docs:
        output["sections"]["official_docs"] = official_docs
        output["sources_used"] += len(official_docs)

    # 2. Source code patterns via nia_package
    pkg = _call_nia_package_sync(
        package, f"API usage patterns middleware routing", registry=registry
    )
    output["version_indexed"] = pkg.get("version_used", "unknown")
    source_patterns = []
    for r in pkg.get("results", [])[:max_results]:
        code = r.get("document", "")
        if code:
            source_patterns.append(code[:max_chars])
    if source_patterns:
        output["sections"]["source_patterns"] = source_patterns
        output["sources_used"] += len(source_patterns)

    # 3. Deprecations via nia_package_grep
    grep = _call_nia_package_grep_sync(package, "deprecat", registry=registry)
    deprecations = []
    for hit in grep.get("results", [])[:max_results * 2]:  # more hits for deprecations
        res = hit.get("result", hit)
        content = res.get("content", "")
        file_path = res.get("file_path", "")
        line = res.get("start_line", "")
        if content:
            deprecations.append({
                "file": file_path,
                "line": line,
                "content": content[:200],
            })
    if deprecations:
        output["sections"]["deprecations"] = deprecations
        output["sources_used"] += len(deprecations)

    # 4. Broader coverage via exa_search
    exa = _call_exa_search_sync(
        f"{package}{version_str} migration guide best practices breaking changes",
        num_results=max_results,
    )
    guides = []
    for r in exa.get("results", []):
        guides.append({
            "title": r.get("title", ""),
            "summary": r.get("summary", "")[:300],
        })
    if guides:
        output["sections"]["guides"] = guides
        output["sources_used"] += len(guides)

    return output

# ---------------------------------------------------------------------------
# Security policy — controls what bridge functions can access
# ---------------------------------------------------------------------------

SECURITY_POLICY = {
    # Directories the sandbox can read from (resolved to absolute paths at runtime)
    "read_allow": [
        ".",              # current project
        "/tmp/ouros",     # ouros source
    ],
    # Directories the sandbox can write to
    "write_allow": [
        "/tmp/ouros-sandbox-output",
    ],
    # Shell commands the sandbox can run (prefix match)
    "command_allow": [
        "tldr ",
        "grep ",
        "rg ",
        "git log",
        "git diff",
        "git show",
        "git blame",
        "wc ",
        "echo ",
        "claude ",
        "codex ",
        "cargo build",
        "cargo test",
        "cargo clippy",
        "npm test",
        "npm run ",
        "python -m pytest",
        "uv run python",
    ],
    # Patterns that are always blocked
    "command_deny": [
        "rm ", "rm\t", "rmdir",
        "chmod", "chown",
        "curl ", "wget ",
        "ssh ", "scp ",
        "sudo ",
        "kill ", "pkill",
        "> /dev/", ">> /dev/",
        "| sh", "| bash", "| zsh",
        "eval ", "exec ",
    ],
}


def _check_path_allowed(path, allowlist):
    """Check if a path falls under an allowed directory."""
    resolved = Path(path).resolve()
    for allowed in allowlist:
        allowed_resolved = Path(allowed).resolve()
        try:
            resolved.relative_to(allowed_resolved)
            return True
        except ValueError:
            continue
    return False


def _check_command_allowed(cmd):
    """Check if a command is allowed by policy."""
    cmd_lower = cmd.strip().lower()
    for deny in SECURITY_POLICY["command_deny"]:
        if deny in cmd_lower:
            return False, f"blocked by deny rule: '{deny}'"
    for allow in SECURITY_POLICY["command_allow"]:
        if cmd_lower.startswith(allow):
            return True, ""
    return False, f"not in command allowlist"

def _call_read_file(path):
    """Read a file from the host filesystem."""
    if not _check_path_allowed(path, SECURITY_POLICY["read_allow"]):
        return {"error": f"read_file denied: '{path}' is outside allowed directories"}
    try:
        return Path(path).read_text()
    except Exception as e:
        return {"error": f"read_file failed: {e}"}


def _call_write_file(path, content):
    """Write content to a file on the host filesystem."""
    if not _check_path_allowed(path, SECURITY_POLICY["write_allow"]):
        return {"error": f"write_file denied: '{path}' is outside allowed directories"}
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        return {"ok": True, "path": str(p)}
    except Exception as e:
        return {"error": f"write_file failed: {e}"}


def _call_glob_files(pattern, path="."):
    """Find files matching a glob pattern."""
    import glob as g
    matches = sorted(g.glob(os.path.join(path, pattern), recursive=True))
    return matches[:500]


def _call_run_command(cmd, timeout=30):
    """Run a shell command and return stdout/stderr."""
    import subprocess
    allowed, reason = _check_command_allowed(cmd)
    if not allowed:
        return {"error": f"run_command denied: {reason}"}
    try:
        r = subprocess.run(
            cmd, shell=True, capture_output=True, text=True,
            timeout=timeout,
        )
        result = {"stdout": r.stdout, "stderr": r.stderr, "returncode": r.returncode}
        return result
    except subprocess.TimeoutExpired:
        return {"error": f"Command timed out after {timeout}s"}
    except Exception as e:
        return {"error": f"run_command failed: {e}"}

def _call_llm(prompt, model="claude-haiku-4-5-20251001", max_tokens=1000,
              system=None, temperature=0.0, backend="anthropic"):
    """Call an LM as a sub-query. Returns the text response.

    Args:
        prompt: The text prompt to send
        model: Model identifier. Anthropic: claude-haiku-4-5-20251001,
               claude-sonnet-4-6. LM Studio: whatever is loaded.
               OpenAI: gpt-4o-mini, gpt-5-nano, etc.
        max_tokens: Maximum response tokens (default 1000)
        system: Optional system prompt
        temperature: Sampling temperature (default 0.0 for deterministic)
        backend: Which provider to use:
                 'local' — LM Studio at localhost:1234
                 'anthropic' — Anthropic API (default)
                 'openai' — OpenAI API
                 'openrouter' — OpenRouter API

    Returns:
        str: The model's text response
    """
    import urllib.request

    messages = [{"role": "user", "content": prompt}]

    if backend == "local":
        body = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        req = urllib.request.Request(
            "http://localhost:1234/v1/chat/completions",
            data=json.dumps(body).encode(),
            headers={"content-type": "application/json"},
        )
        resp = json.loads(urllib.request.urlopen(req, timeout=60).read())
        return resp["choices"][0]["message"]["content"]

    elif backend == "anthropic":
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            return {"error": "ANTHROPIC_API_KEY not set"}
        body = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
        }
        if system:
            body["system"] = system
        if temperature > 0:
            body["temperature"] = temperature
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=json.dumps(body).encode(),
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
        )
        resp = json.loads(urllib.request.urlopen(req, timeout=120).read())
        return resp["content"][0]["text"]

    elif backend == "openai":
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            return {"error": "OPENAI_API_KEY not set"}
        body = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        req = urllib.request.Request(
            "https://api.openai.com/v1/chat/completions",
            data=json.dumps(body).encode(),
            headers={
                "Authorization": f"Bearer {api_key}",
                "content-type": "application/json",
            },
        )
        resp = json.loads(urllib.request.urlopen(req, timeout=120).read())
        return resp["choices"][0]["message"]["content"]

    elif backend == "openrouter":
        api_key = os.environ.get("OPENROUTER_API_KEY")
        if not api_key:
            return {"error": "OPENROUTER_API_KEY not set"}
        body = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        req = urllib.request.Request(
            "https://openrouter.ai/api/v1/chat/completions",
            data=json.dumps(body).encode(),
            headers={
                "Authorization": f"Bearer {api_key}",
                "content-type": "application/json",
            },
        )
        resp = json.loads(urllib.request.urlopen(req, timeout=120).read())
        return resp["choices"][0]["message"]["content"]

    else:
        return {"error": f"Unknown backend: {backend}. Use 'local', 'anthropic', 'openai', or 'openrouter'."}

    return {"error": "No LLM backend available. Start LM Studio or set ANTHROPIC_API_KEY / OPENAI_API_KEY."}


def _call_agent(prompt, agent="claude-code", model=None, max_turns=None,
               timeout=600, cwd=None, isolated=False, permission_mode="default"):
    """Spawn a headless agent and return its output.

    This is the RLM recursive call — a full agent with tool access runs
    autonomously and returns its final output. The agent can read files,
    edit code, run commands, and iterate.

    Supported agents:
    - claude-code: Claude Code CLI in headless mode (claude -p)
    - codex: OpenAI Codex CLI in exec mode (codex exec)

    Args:
        prompt: Task description for the agent
        agent: Which agent to spawn (default: claude-code)
        model: Model override. Claude Code: 'sonnet', 'opus', 'haiku'.
               Codex: 'o3', 'o4-mini', 'gpt-4.1', etc.
        max_turns: Max agent turns (default: None = unlimited, runs until done).
                   Only set this if you want to cap a potentially runaway task.
        timeout: Max seconds to wait (default 600 = 10 min). This is the
                 real safety valve — if the agent hasn't finished, it's stuck.
        cwd: Working directory for the agent (default: current directory)
        isolated: If True, run in a git worktree (Claude Code only).
                  Safe for destructive tasks — if it fucks up, throw away
                  the worktree. No risk to your working tree.
        permission_mode: Permission mode for Claude Code (default: 'default').
                         'default' = agent can't do destructive ops without permission
                         'plan' = agent proposes changes, doesn't execute
                         'auto' = agent decides what needs permission
                         'bypassPermissions' = full autonomy (use with isolated=True)

    Returns:
        str: The agent's final text output
    """
    import subprocess

    if agent == "claude-code":
        cmd = ["claude", "-p", prompt, "--output-format", "text"]
        if model:
            cmd.extend(["--model", model])
        if max_turns is not None:
            cmd.extend(["--max-turns", str(max_turns)])
        if isolated:
            cmd.append("--worktree")
        if permission_mode != "default":
            cmd.extend(["--permission-mode", permission_mode])
    elif agent == "codex":
        cmd = ["codex", "exec", prompt, "--json"]
        if model:
            cmd.extend(["-m", model])
    else:
        return {"error": f"Unknown agent: {agent}. Supported: claude-code, codex"}

    try:
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
        )
        output = r.stdout.strip()

        # Parse Codex JSON output — extract agent_message text
        if agent == "codex" and output:
            messages = []
            for line in output.splitlines():
                try:
                    event = json.loads(line)
                    item = event.get("item", {})
                    if item.get("type") == "agent_message":
                        messages.append(item.get("text", ""))
                except (json.JSONDecodeError, AttributeError):
                    continue
            if messages:
                output = "\n".join(messages)

        if r.returncode != 0 and r.stderr:
            output += f"\n[stderr]: {r.stderr.strip()}"
        return output
    except subprocess.TimeoutExpired:
        return {"error": f"Agent timed out after {timeout}s. Task may be too complex or agent is stuck."}
    except FileNotFoundError:
        return {"error": f"Agent '{agent}' not found on PATH. Is it installed?"}



# function_name -> sync handler
EXTERNAL_FUNCTIONS = {
    "exa_search": _call_exa_search_sync,
    "nia_search": _call_nia_search_sync,
    "nia_universal": _call_nia_universal_sync,
    "nia_web": _call_nia_web_sync,
    "nia_package": _call_nia_package_sync,
    "nia_package_grep": _call_nia_package_grep_sync,
    "nia_help": _call_nia_help,
    "research_package": _call_research_package,
    "read_file": _call_read_file,
    "write_file": _call_write_file,
    "glob_files": _call_glob_files,
    "run_command": _call_run_command,
    "llm_call": _call_llm,
    "agent_call": _call_agent,
}


# ---------------------------------------------------------------------------
# Harness core
# ---------------------------------------------------------------------------

def _create_manager(storage_dir=None):
    """Create a SessionManager with optional persistence."""
    import ouros
    sm = ouros.SessionManager()
    if storage_dir:
        storage = Path(storage_dir)
        storage.mkdir(parents=True, exist_ok=True)
        sm.set_storage_dir(str(storage))
    return sm


def execute_in_sandbox(code, session_id=None, storage_dir=None, load_session=False):
    """Execute Python code in an ouros session with external function bridge.

    Uses Session API for state persistence. External function calls pause
    execution; the harness calls the real API and resumes. Variables persist
    across executions and can be saved/loaded from disk.
    """
    import ouros

    sm = _create_manager(storage_dir)
    ext_funcs = list(EXTERNAL_FUNCTIONS.keys())
    sid = session_id or "default"

    # Load saved session or create fresh one
    if load_session and session_id:
        try:
            sm.load_session(name=session_id, session_id=sid)
            sm.register_external_functions(ext_funcs, session_id=sid)
        except Exception as e:
            print(f"Warning: could not load session '{session_id}': {e}", file=sys.stderr)
            sm.create_session(sid, external_functions=ext_funcs)
    else:
        existing = [s["id"] for s in sm.list_sessions()]
        if sid not in existing:
            sm.create_session(sid, external_functions=ext_funcs)
        else:
            sm.reset(session_id=sid, external_functions=ext_funcs)

    session = ouros.Session(manager=sm, session_id=sid)

    # Each execute()/resume() returns only the stdout produced since the previous
    # pause. Collect every segment; keeping only the last one silently drops
    # anything printed before a later external-function call.
    chunks = []

    def _collect(r):
        out = r.get("stdout")
        if out:
            chunks.append(out)
        return r

    # Execute — may pause at external function calls
    result = _collect(session.execute(code))

    # Pause/resume loop
    while not result.get("is_complete", True):
        progress = result.get("progress", {})
        if progress.get("status") != "function_call":
            break

        func_name = progress["function_name"]
        call_id = progress["call_id"]
        args = progress.get("args", [])
        kwargs = progress.get("kwargs", {})

        # Call real API
        handler = EXTERNAL_FUNCTIONS.get(func_name)
        if not handler:
            result = _collect(session.resume(call_id, {
                "error": f"Unknown function: {func_name}"
            }))
            continue

        try:
            api_result = handler(*args, **kwargs)
        except Exception as e:
            api_result = {"error": f"{func_name} failed: {e}"}

        result = _collect(session.resume(call_id, api_result))

    # Save session
    if session_id and storage_dir:
        try:
            sm.save_session(session_id=sid, name=session_id)
        except Exception:
            pass

    result = dict(result)
    result["stdout"] = "".join(chunks)
    return result


def list_variables(session_id, storage_dir):
    """List variables in a saved session."""
    import ouros
    sm = _create_manager(storage_dir)
    try:
        sm.load_session(name=session_id, session_id=session_id)
    except Exception as e:
        print(f"Error loading session '{session_id}': {e}", file=sys.stderr)
        return
    variables = sm.list_variables(session_id=session_id)
    for v in variables:
        print(f"  {v['name']}: {v['type_name']}")


def get_variable(session_id, var_name, storage_dir):
    """Get a variable from a saved session as JSON."""
    sm = _create_manager(storage_dir)
    try:
        sm.load_session(name=session_id, session_id=session_id)
    except Exception as e:
        print(f"Error loading session '{session_id}': {e}", file=sys.stderr)
        return
    val = sm.get_variable(var_name, session_id=session_id)
    print(json.dumps(val.get("json_value", val), indent=2))


def fork_session(source_id, new_id, storage_dir):
    """Fork a saved session into a new independent copy."""
    sm = _create_manager(storage_dir)
    try:
        sm.load_session(name=source_id, session_id=source_id)
    except Exception as e:
        print(f"Error loading session '{source_id}': {e}", file=sys.stderr)
        return
    sm.fork_session(source_id, new_id)
    if storage_dir:
        sm.save_session(session_id=new_id, name=new_id)
    print(f"Forked '{source_id}' -> '{new_id}'")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser(
        description="Ouros Harness — sandboxed code execution with external function bridge"
    )
    p.add_argument("--code", "-c", help="Python code to execute")
    p.add_argument("--file", "-f", help="Python file to execute")
    p.add_argument("--session", "-s", help="Session ID for persistence")
    p.add_argument("--storage", default=None, help="Storage directory (default: thoughts/shared/dives)")
    p.add_argument("--load", action="store_true", help="Load a saved session before executing")
    p.add_argument("--list-vars", action="store_true", help="List variables in a session")
    p.add_argument("--get-var", help="Get a variable as JSON")
    p.add_argument("--fork", help="Fork session into a new ID")
    return p.parse_args()


def main():
    _load_env()
    args = parse_args()
    storage = args.storage or "thoughts/shared/dives"

    if args.list_vars:
        if not args.session:
            print("Error: --list-vars requires --session", file=sys.stderr)
            sys.exit(1)
        list_variables(args.session, storage)
        return

    if args.get_var:
        if not args.session:
            print("Error: --get-var requires --session", file=sys.stderr)
            sys.exit(1)
        get_variable(args.session, args.get_var, storage)
        return

    if args.fork:
        if not args.session:
            print("Error: --fork requires --session", file=sys.stderr)
            sys.exit(1)
        fork_session(args.session, args.fork, storage)
        return

    # Code execution
    code = None
    if args.code:
        code = args.code
    elif args.file:
        code = Path(args.file).read_text()
    elif not sys.stdin.isatty():
        code = sys.stdin.read()

    if not code:
        print("Error: provide code via --code, --file, or stdin", file=sys.stderr)
        sys.exit(1)

    result = execute_in_sandbox(
        code=code,
        session_id=args.session,
        storage_dir=storage,
        load_session=args.load,
    )

    if result.get("stdout"):
        print(result["stdout"], end="")


if __name__ == "__main__":
    main()
