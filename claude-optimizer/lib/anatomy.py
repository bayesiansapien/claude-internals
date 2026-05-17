"""Anatomy + projection helpers: where every prefix token lives + growth math.

Pure helpers — no decision logic, no LLM judges. Designed to be called by
both compact_decision and any standalone reporting tool.
"""

import json
import os
import re
import subprocess
import time
import urllib.request
import urllib.error
from collections import defaultdict
from pathlib import Path

from .transcript import current_session_file, iter_messages


COMPACT_MAX_OUTPUT_TOKENS = 20_000
COMPACT_BUFFER_TOKENS = 13_000

GROWTH_WINDOW_TURNS = 15

_DEFAULT_WINDOW = 200_000


def get_model_rates(model: str) -> dict:
    """Return per-million-token USD rates for a model.

    Single source of truth = lib/pricing.py (more complete table covering
    Opus 4.1, Sonnet 4.x, Haiku 3.5, etc.). This function adapts the field
    names for cache-bust / projection callers.
    """
    from .pricing import get_pricing
    p = get_pricing(model)
    if p:
        return {
            "input": p["in"],
            "output": p["out"],
            "cache_read": p["cache_r"],
            "cache_write": p["cache_w_5m"],
        }
    # Unknown model → conservative Opus 4.6/4.7 default
    return {"input": 5.0, "output": 25.0, "cache_read": 0.50, "cache_write": 6.25}
_WINDOW_CACHE_PATH = Path.home() / ".claude" / "model-window-cache.json"
_WINDOW_CACHE_TTL = 86_400  # 24 hours

# Last-resort pattern fallback — only used if API is unreachable.
# Keyed by substring → window size, checked in order.
_FALLBACK_PATTERNS = [
    ("opus-4",     1_000_000),
    ("sonnet-4",   1_000_000),
    ("haiku-4",      200_000),
]


def _load_window_cache() -> dict:
    if not _WINDOW_CACHE_PATH.exists():
        return {}
    try:
        raw = json.loads(_WINDOW_CACHE_PATH.read_text())
        now = time.time()
        return {k: v for k, v in raw.items()
                if now - v.get("ts", 0) < _WINDOW_CACHE_TTL}
    except Exception:
        return {}


def _save_window_cache(cache: dict):
    try:
        _WINDOW_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _WINDOW_CACHE_PATH.write_text(json.dumps(cache, indent=2))
    except Exception:
        pass


def _fetch_window_from_api(model: str):
    """Query GET /v1/models/{model} for context_window. Returns None on failure."""
    # Prefer explicit API key; fall back to OAuth token
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if api_key:
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        }
    else:
        token = get_oauth_token()
        if not token:
            return None
        headers = {
            "Authorization": f"Bearer {token}",
            "anthropic-version": "2023-06-01",
            "anthropic-beta": "oauth-2025-04-20",
        }
    try:
        url = f"https://api.anthropic.com/v1/models/{model}"
        req = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read())
        # API field is max_input_tokens (not context_window)
        window = data.get("max_input_tokens") or data.get("context_window")
        if isinstance(window, int) and window > 0:
            return window
    except Exception:
        pass
    return None


def get_context_window(model: str) -> int:
    """Return context window size for a model ID.

    Resolution order (most → least authoritative):
      1. Local cache (~/.claude/model-window-cache.json, 24h TTL)
      2. Anthropic GET /v1/models/{model} API — live, model-specific
      3. Substring pattern fallback — covers known families
      4. Hard default: 200K

    Adding a new model never requires code changes — the API call handles it
    automatically and caches the result.
    """
    if not model:
        return _DEFAULT_WINDOW

    # 1. Cache hit
    cache = _load_window_cache()
    if model in cache:
        return cache[model]["window"]

    # 2. Live API
    window = _fetch_window_from_api(model)

    # 3. Pattern fallback if API failed
    if window is None:
        m = model.lower()
        for pattern, w in _FALLBACK_PATTERNS:
            if pattern in m:
                window = w
                break
        else:
            window = _DEFAULT_WINDOW

    # Persist to cache
    cache[model] = {"window": window, "ts": time.time()}
    _save_window_cache(cache)
    return window


# ---- OAuth ----

def get_oauth_token():
    """Pull claude.ai OAuth token from macOS keychain. Returns None on any failure."""
    try:
        blob = subprocess.run(
            ["security", "find-generic-password", "-s",
             "Claude Code-credentials", "-w"],
            capture_output=True, text=True, timeout=5,
        )
        if blob.returncode != 0:
            return None
        return json.loads(blob.stdout).get("claudeAiOauth", {}).get("accessToken")
    except Exception:
        return None


# ---- Anthropic count_tokens helpers ----

def _post_json(url, body, headers, timeout=10):
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers=headers, method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def count_tokens_text(token, text):
    if not text or not token:
        return 0
    headers = {
        "Authorization": f"Bearer {token}",
        "anthropic-version": "2023-06-01",
        "anthropic-beta": "oauth-2025-04-20",
        "Content-Type": "application/json",
    }
    try:
        d = _post_json(
            "https://api.anthropic.com/v1/messages/count_tokens",
            {"model": "claude-opus-4-7",
             "messages": [{"role": "user", "content": text}]},
            headers,
        )
        return max(0, d["input_tokens"] - 3)
    except Exception:
        return 0


def count_tokens_tools(token, tools):
    if not token or not tools:
        return 0
    headers = {
        "Authorization": f"Bearer {token}",
        "anthropic-version": "2023-06-01",
        "anthropic-beta": "oauth-2025-04-20",
        "Content-Type": "application/json",
    }
    try:
        with_tools = _post_json(
            "https://api.anthropic.com/v1/messages/count_tokens",
            {"model": "claude-opus-4-7",
             "messages": [{"role": "user", "content": "hi"}],
             "tools": tools},
            headers,
        )["input_tokens"]
        baseline = _post_json(
            "https://api.anthropic.com/v1/messages/count_tokens",
            {"model": "claude-opus-4-7",
             "messages": [{"role": "user", "content": "hi"}]},
            headers,
        )["input_tokens"]
        return with_tools - baseline
    except Exception:
        return 0


# ---- MCP discovery ----

def _mcp_jsonrpc(url, method, token, params=None, request_id=1):
    body = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        body["params"] = params
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {token}",
            "MCP-Protocol-Version": "2025-06-18",
        }, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = r.read().decode()
            if "data:" in raw[:100] or raw.startswith("event:"):
                for line in raw.splitlines():
                    if line.startswith("data:"):
                        try:
                            d = json.loads(line[5:].strip())
                            if "result" in d or "error" in d:
                                return d
                        except Exception:
                            continue
            return json.loads(raw)
    except Exception:
        return {}


def build_auth_stub_schema(server_name, server_url):
    """Reconstruct the auth-stub tool schema CC ships for unauthenticated
    MCP servers (mirrors McpAuthTool.ts in CC source)."""
    location = f"http at {server_url}" if server_url else "stdio"
    description = (
        f"The `{server_name}` MCP server ({location}) is installed but "
        f"requires authentication. Call this tool to start the OAuth flow — "
        f"you'll receive an authorization URL to share with the user. Once "
        f"the user completes authorization in their browser, the server's "
        f"real tools will become available automatically."
    )
    return {
        "name": f"mcp__{server_name}__authenticate",
        "description": description,
        "input_schema": {"type": "object", "properties": {},
                          "additionalProperties": False},
    }


def normalize_mcp_name(display_name):
    return "claude_ai_" + re.sub(r"\W+", "_", display_name).strip("_")


def fetch_mcp_tools_via_anthropic(token):
    """Returns {normalized_server_name: [tool_dicts]} of auth-stub schemas."""
    out = {}
    if not token:
        return out
    try:
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/mcp_servers?limit=1000",
            headers={
                "Authorization": f"Bearer {token}",
                "anthropic-beta": "mcp-servers-2025-12-04",
                "anthropic-version": "2023-06-01",
            }, method="GET",
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            servers = json.loads(r.read()).get("data", [])
    except Exception:
        return out
    for srv in servers:
        norm = normalize_mcp_name(srv["display_name"])
        out[norm] = [build_auth_stub_schema(norm, srv["url"])]
    return out


# ---- CLAUDE.md hierarchy + auto-memory ----

def find_claude_md_files():
    files = []
    p = Path.cwd() / "CLAUDE.md"
    if p.exists(): files.append(p)
    p = Path.cwd() / ".claude" / "CLAUDE.md"
    if p.exists(): files.append(p)
    p = Path.cwd() / "CLAUDE.local.md"
    if p.exists(): files.append(p)
    rules = Path.cwd() / ".claude" / "rules"
    if rules.is_dir():
        files.extend(sorted(rules.glob("*.md")))
    p = Path.home() / ".claude" / "CLAUDE.md"
    if p.exists(): files.append(p)
    return files


def find_memory_files():
    cwd = Path.cwd()
    proj_hash = str(cwd.resolve()).replace("/", "-")
    mem = Path.home() / ".claude" / "projects" / proj_hash / "memory"
    if not mem.is_dir():
        return []
    return sorted(mem.glob("*.md"))


# ---- Conversation + usage walk ----

def walk_conversation(session_path):
    """Bucket conversation content by type (returns char counts), plus
    first/last assistant usage + last model."""
    buckets = defaultdict(int)
    last_usage = None
    last_model = None
    first_usage = None

    if not session_path:
        return buckets, None, None, None

    for rec in iter_messages(session_path):
        if rec.get("isSidechain"):
            continue
        msg = rec.get("message")
        if rec.get("type") == "assistant" and isinstance(msg, dict):
            usage = msg.get("usage")
            if usage:
                last_usage = usage
                last_model = msg.get("model") or last_model
                if first_usage is None:
                    first_usage = usage
            content = msg.get("content", [])
            if isinstance(content, list):
                for c in content:
                    if not isinstance(c, dict):
                        continue
                    if c.get("type") == "text":
                        buckets["assistant_text"] += len(c.get("text", ""))
                    elif c.get("type") == "tool_use":
                        buckets["tool_calls"] += len(json.dumps(c.get("input", {})))
                        buckets["tool_calls"] += len(c.get("name", ""))
        elif rec.get("type") == "user" and isinstance(msg, dict):
            content = msg.get("content")
            if isinstance(content, str):
                buckets["user_text"] += len(content)
            elif isinstance(content, list):
                for c in content:
                    if not isinstance(c, dict):
                        continue
                    if c.get("type") == "text":
                        buckets["user_text"] += len(c.get("text", ""))
                    elif c.get("type") == "tool_result":
                        inner = c.get("content")
                        if isinstance(inner, str):
                            buckets["tool_results"] += len(inner)
                        elif isinstance(inner, list):
                            for ic in inner:
                                if isinstance(ic, dict) and ic.get("type") == "text":
                                    buckets["tool_results"] += len(ic.get("text", ""))
        if rec.get("type") == "attachment":
            buckets["attachments"] += len(json.dumps(rec))

    return buckets, first_usage, last_usage, last_model


def prefix_total(usage):
    if not usage:
        return 0
    return (usage.get("cache_read_input_tokens", 0)
            + usage.get("cache_creation_input_tokens", 0)
            + usage.get("input_tokens", 0))


def per_call_cost(usage, model=None):
    """Cost of one API call. Model-aware via get_model_rates()."""
    if not usage:
        return 0.0
    rates = get_model_rates(model)
    cr = usage.get("cache_read_input_tokens", 0)
    cw = usage.get("cache_creation_input_tokens", 0)
    inp = usage.get("input_tokens", 0)
    out = usage.get("output_tokens", 0)
    return (
        cr / 1e6 * rates["cache_read"]
        + cw / 1e6 * rates["cache_write"]
        + inp / 1e6 * rates["input"]
        + out / 1e6 * rates["output"]
    )


def collect_assistant_usages(session_path):
    out = []
    if not session_path:
        return out
    for rec in iter_messages(session_path):
        if rec.get("isSidechain"):
            continue
        if rec.get("type") != "assistant":
            continue
        msg = rec.get("message")
        if not isinstance(msg, dict):
            continue
        usage = msg.get("usage")
        if not usage:
            continue
        model = msg.get("model")
        out.append({
            "prefix": prefix_total(usage),
            "cost": per_call_cost(usage, model),
            "output_tokens": usage.get("output_tokens", 0),
            "model": model,
        })
    return out


# ---- Anatomy + projection main entrypoints ----

def compute_anatomy(token=None):
    """Returns full prefix anatomy for current session.

    {
      "total_prefix": int,
      "model": str | None,
      "window": int,
      "pressure_pct": float,
      "system": {
        "mcp_tokens": int, "mcp_server_count": int,
        "claude_md_tokens": int, "claude_md_files": int,
        "memory_tokens": int, "memory_files": int,
        "cc_core_estimate": int,
        "total": int,
      },
      "conversation": {
        "buckets_tokens": {bucket_name: int, ...},
        "total": int,
      },
    }
    """
    session_path = current_session_file()
    buckets, first_usage, last_usage, model = walk_conversation(session_path)
    total = prefix_total(last_usage)
    initial_system = (first_usage or {}).get("cache_creation_input_tokens", 0)

    window = get_context_window(model)

    mcp_by_server = fetch_mcp_tools_via_anthropic(token) if token else {}
    all_mcp_tools = [t for tools in mcp_by_server.values() for t in tools]
    mcp_tokens = count_tokens_tools(token, all_mcp_tools) if all_mcp_tools else 0

    claude_md_files = find_claude_md_files()
    claude_md_combined = "\n\n".join(p.read_text() for p in claude_md_files) if claude_md_files else ""
    claude_md_tokens = count_tokens_text(token, claude_md_combined) if claude_md_combined else 0

    memory_files = find_memory_files()
    memory_combined = "\n\n".join(p.read_text() for p in memory_files) if memory_files else ""
    memory_tokens = count_tokens_text(token, memory_combined) if memory_combined else 0

    known_system = mcp_tokens + claude_md_tokens + memory_tokens
    cc_core_estimate = max(0, initial_system - known_system)
    current_system = known_system + cc_core_estimate

    conv_total = max(0, total - current_system)
    total_conv_chars = sum(buckets.values())
    conv_buckets_tokens = {}
    if total_conv_chars > 0 and conv_total > 0:
        for k, v in buckets.items():
            conv_buckets_tokens[k] = int(v / total_conv_chars * conv_total)

    return {
        "total_prefix": total,
        "model": model,
        "window": window,
        "pressure_pct": (total / window * 100) if window else 0,
        "system": {
            "mcp_tokens": mcp_tokens,
            "mcp_server_count": len(mcp_by_server),
            "claude_md_tokens": claude_md_tokens,
            "claude_md_files": len(claude_md_files),
            "memory_tokens": memory_tokens,
            "memory_files": len(memory_files),
            "cc_core_estimate": cc_core_estimate,
            "total": current_system,
        },
        "conversation": {
            "buckets_tokens": conv_buckets_tokens,
            "total": conv_total,
        },
    }


def compute_projection(window, current_total, model=None):
    """Returns growth + projection from session usage history.

    Model-aware: cache_read and output rates come from get_model_rates(model).
    Falls back to the most recent assistant message's model if `model` is None.
    """
    session_path = current_session_file()
    usages = collect_assistant_usages(session_path)
    threshold = max(0, window - COMPACT_MAX_OUTPUT_TOKENS - COMPACT_BUFFER_TOKENS)

    if len(usages) < 3:
        return {
            "insufficient_data": True,
            "compact_threshold": threshold,
            "headroom_tokens": max(0, threshold - current_total),
        }

    # Resolve model from latest usage if not provided
    if model is None:
        for u in reversed(usages):
            if u.get("model"):
                model = u["model"]
                break
    rates = get_model_rates(model)

    recent = usages[-GROWTH_WINDOW_TURNS:]
    deltas = [recent[i]["prefix"] - recent[i - 1]["prefix"]
              for i in range(1, len(recent))]
    deltas = [d for d in deltas if d > 0]
    if not deltas:
        return {
            "insufficient_data": True,
            "compact_threshold": threshold,
            "headroom_tokens": max(0, threshold - current_total),
        }

    growth = sum(deltas) / len(deltas)
    recent_costs = [u["cost"] for u in recent if u["cost"] > 0]
    current_cost = sum(recent_costs) / len(recent_costs) if recent_costs else 0
    headroom = max(0, threshold - current_total)
    ttc = int(headroom / growth) if growth > 0 else float("inf")

    if ttc == float("inf") or ttc > 1000:
        cost_to_compact = None
    else:
        linear = current_cost * ttc
        triangle = 0.5 * (growth / 1e6 * rates["cache_read"]) * (ttc ** 2)
        cost_to_compact = linear + triangle

    compact_now_cost = (
        current_total / 1e6 * rates["cache_read"]
        + COMPACT_MAX_OUTPUT_TOKENS / 1e6 * rates["output"]
    )
    post_compact_prefix = 30_000 + 17_000
    post_call_cost = post_compact_prefix / 1e6 * rates["cache_read"]

    if ttc != float("inf") and cost_to_compact is not None:
        post_total = compact_now_cost + post_call_cost * ttc
        savings = cost_to_compact - post_total
    else:
        savings = None

    return {
        "insufficient_data": False,
        "growth_per_turn": int(growth),
        "compact_threshold": threshold,
        "headroom_tokens": headroom,
        "turns_to_compact": ttc,
        "current_cost_per_turn": current_cost,
        "cost_until_compact": cost_to_compact,
        "compact_now_cost": compact_now_cost,
        "post_compact_per_call_cost": post_call_cost,
        "projected_savings": savings,
    }
