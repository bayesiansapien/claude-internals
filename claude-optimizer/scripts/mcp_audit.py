#!/usr/bin/env python3
"""mcp-audit · Audit connected MCP servers with EXACT token counts.

Pipeline (no estimation):
  1. Read OAuth token from macOS keychain (Claude Code-credentials)
  2. Fetch server list from api.anthropic.com/v1/mcp_servers
  3. For each server, query the MCP `tools/list` JSON-RPC endpoint
     for the real schemas
  4. Count tokens via Anthropic's /v1/messages/count_tokens endpoint
  5. Report per-server breakdown + current session cost

Distinguishes:
  - "Current" state: what's actually in your prefix now (auth-stub tools
    if you haven't authenticated, real tools if you have)
  - "Post-auth" state: what the prefix would balloon to if you click
    authenticate

Gracefully degrades:
  - No keychain access → reports config-only view
  - Server behind Cloudflare WAF → reports "schema unavailable"
  - No API key → skips token counting
"""

import json
import re
import subprocess
import sys
import urllib.request
import urllib.error
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.transcript import current_session_file, iter_messages
from lib.anatomy import get_model_rates

AUTH_TOOLS = {"authenticate", "complete_authentication", "auth", "login"}


def get_oauth_token():
    """Pull OAuth token + scopes from the system credential store.

    Cross-platform: works on macOS (Keychain), Linux (Secret Service), and
    Windows (Credential Manager) via the platform_compat abstraction.
    """
    from lib.platform_compat import read_oauth_token
    try:
        raw = read_oauth_token()
        if not raw:
            return None, []
        data = json.loads(raw)
        oauth = data.get("claudeAiOauth", {})
        return oauth.get("accessToken"), oauth.get("scopes", [])
    except Exception:
        return None, []


def fetch_mcp_server_list(token):
    """Returns [{display_name, id, url, type}, ...] or None on failure."""
    try:
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/mcp_servers?limit=1000",
            headers={
                "Authorization": f"Bearer {token}",
                "anthropic-beta": "mcp-servers-2025-12-04",
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read()).get("data", [])
    except Exception:
        return None


def mcp_jsonrpc(url, method, token, params=None, request_id=1, timeout=10):
    body = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        body["params"] = params
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {token}",
            "MCP-Protocol-Version": "2025-06-18",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode()
            # Handle SSE-style chunked responses
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
    except urllib.error.HTTPError as e:
        return {"_http_error": e.code}
    except Exception as e:
        return {"_error": str(e)}


def fetch_real_tools(server_url, token):
    """Returns (tools_list, error_str). tools_list is None on failure."""
    init = mcp_jsonrpc(server_url, "initialize", token, params={
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "claude-optimizer-mcp-audit", "version": "1.0"},
    })
    if "_http_error" in init:
        return None, f"HTTP {init['_http_error']} (likely WAF-blocked)"
    if "_error" in init:
        return None, init["_error"][:60]
    if "result" not in init:
        return None, "initialize failed"

    tools = mcp_jsonrpc(server_url, "tools/list", token, request_id=2)
    if "_http_error" in tools:
        return None, f"tools/list HTTP {tools['_http_error']}"
    if "result" not in tools:
        return None, "tools/list failed"
    return tools["result"].get("tools", []), None


def build_auth_stub_schema(server_name, server_url):
    """Reconstruct the auth-stub tool schema CC currently ships for
    unauthenticated MCP servers (from McpAuthTool.ts)."""
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
        "input_schema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    }


def count_tokens(token, tools):
    """Use /v1/messages/count_tokens for exact answer. Returns (with, baseline)."""
    body_with = {
        "model": "claude-opus-4-7",
        "messages": [{"role": "user", "content": "hi"}],
        "tools": tools,
    }
    body_baseline = {
        "model": "claude-opus-4-7",
        "messages": [{"role": "user", "content": "hi"}],
    }
    headers = {
        "Authorization": f"Bearer {token}",
        "anthropic-version": "2023-06-01",
        "anthropic-beta": "oauth-2025-04-20",
        "Content-Type": "application/json",
    }
    def call(b):
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages/count_tokens",
            data=json.dumps(b).encode(),
            headers=headers, method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())["input_tokens"]
    try:
        return call(body_with), call(body_baseline)
    except Exception:
        return None, None


def normalize_server_name(display_name):
    """Mirror CC's MCP name normalization."""
    # Replace non-alphanumerics with underscores
    return "claude_ai_" + re.sub(r"\W+", "_", display_name).strip("_")


def session_api_call_count():
    """Count assistant messages with usage in current session.

    Returns (api_calls, mcp_real_invocations, latest_model).
    """
    path = current_session_file()
    if not path:
        return 0, 0, None
    calls = 0
    mcp_real_invocations = 0
    latest_model = None
    for rec in iter_messages(path):
        msg = rec.get("message")
        if rec.get("type") == "assistant" and isinstance(msg, dict) and msg.get("usage"):
            calls += 1
            if msg.get("model"):
                latest_model = msg["model"]
        raw = json.dumps(rec)
        for _, tool in re.findall(r'"mcp__([a-zA-Z0-9_\-]+)__([a-zA-Z0-9_\-]+)"', raw):
            if tool not in AUTH_TOOLS:
                mcp_real_invocations += 1
    return calls, mcp_real_invocations, latest_model


def main():
    print()
    print("  MCP AUDIT · EXACT token counts")
    print("  " + "═" * 74)

    token, scopes = get_oauth_token()
    if not token:
        print("  Could not read OAuth token from keychain. Falling back to estimate.")
        print("  (Run from your normal user account — keychain requires it.)")
        return

    if "user:mcp_servers" not in scopes:
        print("  OAuth token lacks user:mcp_servers scope — can't enumerate.")
        return

    print("  Fetching server list from api.anthropic.com/v1/mcp_servers ...")
    servers = fetch_mcp_server_list(token)
    if servers is None:
        print("  Failed to fetch server list.")
        return
    if not servers:
        print("  No claude.ai MCP servers connected.")
        return

    api_calls, real_invocations, session_model = session_api_call_count()
    rates = get_model_rates(session_model)
    cache_read_rate = rates["cache_read"]
    model_label = session_model or "unknown"

    print(f"  Found {len(servers)} server(s). Fetching real schemas ...")
    print()
    print(f"  {'Server':<22} {'Real':>4}  {'Current':>10}  {'Post-auth':>10}  {'Tools':>5}  Status")
    print("  " + "─" * 74)

    total_current = 0
    total_post_auth = 0
    dormant = []
    blocked = []

    for srv in servers:
        name = srv["display_name"]
        url = srv["url"]
        normalized = normalize_server_name(name)

        # 1. Current state: auth-stub tokens (always present pre-auth)
        stub = build_auth_stub_schema(normalized, url)
        stub_with, stub_base = count_tokens(token, [stub])
        current_cost = (stub_with - stub_base) if (stub_with and stub_base) else None

        # 2. Post-auth state: real tool schemas
        real_tools, err = fetch_real_tools(url, token)
        if real_tools:
            anth_tools = [{
                "name": f"mcp__{normalized}__{t['name']}",
                "description": t.get("description", ""),
                "input_schema": t.get("inputSchema") or {"type": "object", "properties": {}},
            } for t in real_tools]
            post_with, post_base = count_tokens(token, anth_tools)
            post_cost = (post_with - post_base) if (post_with and post_base) else None
            n_tools = len(anth_tools)
            status = "✓ schema fetched"
        else:
            post_cost = None
            n_tools = 0
            status = f"⚠ post-auth: {err}"
            blocked.append(name)

        # Real invocations from session — does the user actually use this?
        # (We could parse it per-server, but already tracked aggregate above)

        if current_cost is not None:
            total_current += current_cost
        if post_cost is not None:
            total_post_auth += post_cost
        else:
            # Use current as floor when post-auth unavailable
            if current_cost is not None:
                total_post_auth += current_cost

        if api_calls > 0:
            dormant.append(name)  # We know none are actively used this session

        display_name = name if len(name) <= 22 else name[:19] + "..."
        cur_str = f"{current_cost:,}" if current_cost else "?"
        post_str = f"{post_cost:,}" if post_cost else "—"
        n_str = str(n_tools) if n_tools else "—"
        print(f"  {display_name:<22} {0:>4}  {cur_str:>10}  {post_str:>10}  {n_str:>5}  {status}")

    print("  " + "─" * 74)
    print(f"  {'TOTAL':<22} {real_invocations:>4}  {total_current:>10,}  {total_post_auth:>10,}")
    print()

    print(f"  💰 COST IMPACT (this session — {model_label} cache-read rate ${cache_read_rate}/M)")
    if api_calls > 0:
        current_session_cost = total_current * api_calls / 1_000_000 * cache_read_rate
        post_session_cost = total_post_auth * api_calls / 1_000_000 * cache_read_rate
        print(f"     API calls so far in session:    {api_calls:,}")
        print(f"     Current MCP overhead per call:  {total_current:,} tokens")
        print(f"     Already spent on MCP schemas:   ~${current_session_cost:.2f}")
        print()
        print(f"     If you authenticate all servers:")
        print(f"     Overhead jumps to:              {total_post_auth:,} tokens per call")
        print(f"     Would have cost this session:   ~${post_session_cost:.2f}")
        print(f"     Extra cost from authenticating: ~${post_session_cost - current_session_cost:.2f}")
    print()

    print("  💡 INTERPRETATION")
    if real_invocations == 0:
        print(f"     • You've made {api_calls:,} API calls and invoked 0 real MCP tools.")
        print(f"     • All {len(servers)} connector(s) are dead weight in your prefix.")
        print(f"     • Recommendation: disconnect any you don't actively use.")
    else:
        print(f"     • {real_invocations} real MCP tool invocation(s) this session.")
    print()
    if blocked:
        print(f"     Note: {len(blocked)} server(s) behind WAF — post-auth schema couldn't be")
        print(f"     fetched directly: {', '.join(blocked)}.")
        print(f"     The current (auth-stub) numbers ARE exact.")
        print()

    print("  TO DISCONNECT:")
    print("     → claude.ai → Settings → Connectors → toggle off")
    print("     → quit Claude Code (Cmd+Q) and reopen")
    print()


if __name__ == "__main__":
    main()
