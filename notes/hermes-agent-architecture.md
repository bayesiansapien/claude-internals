# Hermes Agent — Architecture Reference

> NousResearch/hermes-agent (MIT license, v0.13.0)
> Source cloned to: `reference/hermes-agent/`
> Python (~780K lines) + TypeScript TUI (~57K lines)

---

## What Is It?

A **self-improving, provider-agnostic AI agent** built by Nous Research. The core thesis: agents should run on any model, any provider, any platform — and should get better at tasks through a closed learning loop (skills created from experience, curated over time, improved during use).

Hermes Agent absorbed OpenClaw via `hermes claw migrate` — it is OpenClaw's **successor**, not a competitor.

### Positioning vs. Claude Code

| Dimension | Claude Code | Hermes Agent |
|-----------|-------------|--------------|
| Builder | Anthropic (commercial) | Nous Research (open-source, MIT) |
| Language | TypeScript (~512K LOC) | Python (~780K LOC) + TS TUI (~57K) |
| Provider | Anthropic-only | 15+ providers, plugin-extensible |
| API format | Anthropic Messages API (native) | OpenAI chat completions (universal lingua franca) |
| Core bet | Harness quality → agent quality | Provider freedom + self-improvement → agent quality |

---

## Core Design Philosophy

### 5 Invariants

1. **Provider agnosticism is the primary constraint.** Everything uses OpenAI chat completions format as lingua franca. Provider-specific adapters (Anthropic, Bedrock, Codex) translate at the edges. The exact opposite of Claude Code's Anthropic-locked design.

2. **Self-improvement loop.** The agent creates skills from experience, improves them during use, and a background curator manages skill lifecycle. Neither Claude Code nor OpenClaw has this.

3. **Prompt cache preservation is sacred.** System prompt frozen at session start. Memory writes update disk but NOT the running system prompt. Slash commands that mutate state defer invalidation.

4. **Runs anywhere, not just your laptop.** 7 terminal backends. Gateway for messaging platforms (Telegram, Discord, Slack, WhatsApp, Signal). Serverless persistence (Modal, Daytona, Vercel Sandbox).

5. **Research-ready.** Trajectory saving, batch processing, Atropos RL environments, trajectory compression for training next-gen tool-calling models.

---

## Architecture — Component Map

### Agent Loop

**Synchronous while-loop** in `run_agent.py`, `AIAgent.run_conversation()` (line 11419).

Main loop at **line 11844**:
```python
while (api_call_count < self.max_iterations and
       self.iteration_budget.remaining > 0) or self._budget_grace_call:
```

**9-step turn pipeline** (structurally mirrors Claude Code):
1. Interrupt check (`self._interrupt_requested`, line 11849)
2. Budget consumption (`self.iteration_budget.consume()`, line 11865)
3. Pre-API steer drain (inject `/steer` messages, line 11917)
4. Message sanitization (surrogates, tool call args, role alternation repair)
5. API message assembly (inject memory, plugins, ephemeral prompts)
6. Prompt caching application (Anthropic cache_control breakpoints, line 12061)
7. API call with retry loop (`while retry_count < max_retries`, line 12176)
8. Tool dispatch (handle tool_calls from response)
9. Loop exit: no tool_calls, budget exhausted, interrupt, max_turns

**Budget system:** `IterationBudget` class (line ~1172). Shared between parent and child agents. Default 90 iterations. "Grace call" mechanism: when budget exhausts, model gets one final turn to wrap up.

---

### Model Selection & Provider System

**Plugin-based provider architecture:**
- `hermes_cli/providers.py` — `ProviderProfile` dataclass, `register_provider()`
- Discovery is lazy (first call scans all plugins)

**Supported providers (from code):**
Nous Portal, OpenRouter (200+ models), NVIDIA NIM, Xiaomi MiMo, z.ai/GLM, Kimi/Moonshot, MiniMax, Hugging Face, OpenAI, Anthropic (native), AWS Bedrock, GitHub Copilot, LMStudio, Ollama, Google Gemini, xAI Codex, plus any custom OpenAI-compatible endpoint.

**API mode auto-detection** at `run_agent.py` lines 1204-1235:
- `anthropic_messages` — native Anthropic SDK
- `codex_responses` — OpenAI Codex/Responses API
- `bedrock_converse` — AWS Bedrock
- `chat_completions` — default (OpenAI-compatible, the lingua franca)

**Fallback chain:** `fallback_model` parameter supports single-dict and list of dicts. On API failure, `_try_activate_fallback()` walks the chain. Error classifier (`agent/error_classifier.py`) provides structured `FailoverReason` taxonomy (auth, billing, rate_limit, overloaded, context_overflow, model_not_found) determining retry vs rotate vs compress vs fallback.

**Credential pool:** `agent/credential_pool.py` (1,603 lines). Multi-credential same-provider failover. Strategies: `fill_first`, `round_robin`, `random`, `least_used`. Exhaustion TTLs (401 = 5min, 429 = 1hr).

**OpenRouter pareto routing:** Config key `model.min_coding_score` for `openrouter/pareto-code`.

---

### Context Management

**Single-stage compressor** — `agent/context_compressor.py` (1,556 lines).

| Aspect | Detail |
|--------|--------|
| Trigger | Token count exceeds `compression.threshold` (default 50% of context window) |
| Strategy | Protect head N + tail N messages, summarize middle |
| Summary model | Auxiliary model (cheap/fast) via `agent/auxiliary_client.py` |
| Summary budget | 20% of compressed content, capped at 12K tokens, minimum 2K |
| Pre-pass | Prunes old tool outputs before LLM summarization (free) |
| Iterative | Summary prefix marks content as "REFERENCE ONLY" (no re-execution) |

**Preflight compression** (lines 11697-11756): Before entering main loop, checks if loaded history already exceeds threshold. Up to 3 compression passes for very large sessions.

**Token estimation:** `agent/model_metadata.py` — `estimate_tokens_rough()`. Rough char-based (4 chars per token). No tiktoken.

**Context window discovery:** `get_model_context_length()` in `agent/model_metadata.py` (1,607 lines). Fetches from provider APIs, caches, has probe-based escalation, parses context limits from error messages.

**Comparison to Claude Code:**
| Feature | Claude Code | Hermes Agent |
|---------|-------------|--------------|
| Compaction stages | 5 (budget→snip→microcompact→collapse→full) | 1 (single compressor) |
| Cache-aware compression | Yes (cache_edits, microcompact) | No |
| Deferred tool loading | Yes (ToolSearchTool) | No |
| Per-message budget caps | Yes (always active) | No |

---

### Memory System — 3 Tiers + Plugin Providers

**Tier 1: Persistent (disk)**
- `~/.hermes/memories/MEMORY.md` — agent's personal notes
- `~/.hermes/memories/USER.md` — user model (what agent knows about user)
- Section delimiter: `\n§\n` (section sign)
- Written via `memory` tool with actions: add, replace, remove, read
- **Injection threat scanning:** regex patterns for prompt injection, exfiltration, SSH backdoors

**Tier 2: Session (SQLite)**
- `hermes_state.py` — `SessionDB` class (schema version 11)
- Tables: `sessions` (metadata, costs, tokens) + `messages` (content, tool metadata)
- `messages_fts` (FTS5) + `messages_fts_trigram` (CJK substring search)
- WAL mode with NFS/SMB fallback

**Tier 3: Working (in-context)**
- Conversation history in OpenAI message format
- Memory injected into system prompt at session start only (frozen — never mid-session)

**Plugin memory providers:** `agent/memory_manager.py` (555 lines). Supports: Honcho (dialectic user modeling), mem0, supermemory, byterover, hindsight, holographic, openviking, retaindb. Only ONE external plugin allowed at a time.

**Memory nudge system:** Periodic prompting for agent to review/update memory. Turn-based interval (`memory.nudge_interval`). Counter tracks `_turns_since_memory`.

---

### Tool System

**~80 registered tool calls** across `tools/*.py` files.

**Registry:** `tools/registry.py` — `ToolEntry` dataclass with: name, toolset, schema (OpenAI function-calling format), handler, check_fn, requires_env, is_async, emoji, max_result_size_chars.

**Auto-discovery:** `discover_builtin_tools()` AST-scans each `tools/*.py` for `registry.register()` calls, imports matching modules.

**Toolset system:** `toolsets.py` — `TOOLSETS` dict grouping tools by category. `_HERMES_CORE_TOOLS` is the default bundle (~50 tools). Categories: browser, code_execution, delegation, file, kanban, memory, messaging, search, skills, terminal, todo, vision, web, and more.

**MCP support:** `tools/mcp_tool.py` (3,408 lines). Full MCP client via stdio, HTTP/StreamableHTTP, or SSE. Config in `~/.hermes/config.yaml`. Sampling support (server-initiated LLM requests).

---

### Safety / Permissions

**Not deny-first.** Pattern-based approval system.

| Mechanism | File | What It Does |
|-----------|------|--------------|
| Dangerous command detection | `tools/approval.py` (1,367 lines) | Regex-matches risky shell commands |
| Smart approval | `tools/approval.py` | Optional auxiliary LLM evaluates command risk |
| File safety | `agent/file_safety.py` | Hardcoded deny lists for sensitive paths |
| Tool guardrails | `agent/tool_guardrails.py` (455 lines) | Loop detection, failure dedup, no-progress detection |
| Memory injection scan | `tools/memory_tool.py` | Regex for prompt injection in memory writes |

**No permission modes** like Claude Code's plan/default/acceptEdits/auto/dontAsk/bypassPermissions. Binary: pattern match → ask user or auto-approve.

---

### Multi-Agent / Subagents

**Two coordination patterns:**

**1. Delegate tool** — `tools/delegate_tool.py` (2,767 lines)
- Single: `goal` + `context` + `toolsets` → spawns child `AIAgent`
- Batch (parallel): `tasks: [...]`, each gets own subagent
- Concurrency cap: `delegation.max_concurrent_children` (default 3)
- Max depth: `delegation.max_spawn_depth` (default 2, cap 3)
- Roles: `leaf` (no recursion) vs `orchestrator` (nested spawning)
- Blocked tools for children: delegate_task, clarify, memory, send_message, execute_code

**2. Kanban multi-agent** — `hermes_cli/kanban.py` + `tools/kanban_tools.py`
- Durable SQLite-backed work queue
- Dispatcher spawns workers per profile
- Board-level isolation
- Auto-blocks tasks after 5 consecutive spawn failures

**Budget sharing:** Parent creates `IterationBudget`, children inherit same reference. Prevents unbounded subagent cost.

**No equivalent to Claude Code's coordinator mode** (parent becomes dispatcher with only 4 tools) or teammate swarm pattern (shared-memory, in-process).

---

### Self-Improvement Loop (Unique to Hermes)

**Skills system:** Agent-created reusable capabilities.
- `~/.hermes/skills/` — directory of skill YAML/MD files
- Agent can create, update, archive skills during conversation
- Skills have provenance tracking (`created_by: "agent"`)
- Curator (`agent/curator.py`, 1,781 lines) — background lifecycle management
  - Tracks usage per skill
  - Auto-archives stale agent-created skills
  - Pinned skills exempt from archival
  - Archives to `.archive/`, never deletes

**User modeling:** `USER.md` captures preferences, patterns, communication style. Updated by agent across sessions. Memory nudge system triggers periodic review.

This is genuinely novel — neither Claude Code nor OpenClaw has a closed learning loop.

---

### Session Persistence

**SQLite-based** (vs. Claude Code's JSONL files).

| Feature | Claude Code | Hermes Agent |
|---------|-------------|--------------|
| Format | Append-only JSONL | SQLite (WAL mode) |
| Search | Sequential scan | FTS5 + trigram CJK |
| Resume | `/resume` + UUID chain | `/resume` + parent_session_id |
| Cost tracking | In-memory + project config | Per-session columns |
| Auditability | Human-readable, version-controllable | Queryable but opaque |

---

### Cost Tracking

**Usage pricing:** `agent/usage_pricing.py` (866 lines).
- `CanonicalUsage`: input_tokens, output_tokens, cache_read/write, reasoning_tokens
- `PricingEntry` with per-million costs
- `CostResult` with USD amount and status (actual/estimated/included/unknown)

**Per-session:** Stored in SQLite: input_tokens, output_tokens, cache_read/write, reasoning_tokens, estimated_cost_usd, actual_cost_usd, cost_status, cost_source.

**No real-time rate limit awareness** (no equivalent to Claude Code's `anthropic-ratelimit-unified` header tracking). Post-hoc only.

---

### Gateway — Multi-Platform Agent Access

Unique to Hermes: agent accessible from messaging platforms.

| Platform | Adapter |
|----------|---------|
| Telegram | `gateways/telegram/` |
| Discord | `gateways/discord/` |
| Slack | `gateways/slack/` |
| WhatsApp | `gateways/whatsapp/` |
| Signal | `gateways/signal/` |
| Matrix | `gateways/matrix/` |

Each adapter maintains per-user, per-chat session keys. Session splitting on compression.

---

## Routing Interception Points

| Point | File | What You Can Route |
|-------|------|--------------------|
| Model selection | `run_agent.py:1168` | Main model (`self.model`) |
| Fallback chain | `run_agent.py:1744-1758` | `self._fallback_chain` walk |
| Credential rotation | `agent/credential_pool.py` | `PooledCredential` selection strategy |
| Subagent model | `tools/delegate_tool.py` | Child inherits parent model/provider |
| Auxiliary model | `agent/auxiliary_client.py` | Per-task overrides in `auxiliary:` config |
| Compression model | `agent/context_compressor.py` | Uses auxiliary client |
| Error classification | `agent/error_classifier.py` | `FailoverReason` → retry/rotate/compress/fallback |
| OpenRouter pareto | Config `model.min_coding_score` | Provider-level routing via OpenRouter |

---

## Key Numbers

```
Codebase:            ~780K lines Python + ~57K lines TypeScript
Tools:               ~80 registered
Providers:           15+ supported
Memory providers:    8 plugin options
Max spawn depth:     3 (subagent nesting)
Max concurrent:      3 (parallel children)
Default budget:      90 iterations (shared parent↔children)
Compression trigger: 50% of context window (default)
Summary cap:         12K tokens
MCP transports:      stdio, HTTP, StreamableHTTP, SSE
Gateway platforms:   6 (Telegram, Discord, Slack, WhatsApp, Signal, Matrix)
```

---

## Source References

| Concept | File | Detail |
|---------|------|--------|
| Main agent class | `run_agent.py` | `AIAgent`, ~15.5K LOC |
| Agent loop | `run_agent.py:11844` | Main while-loop |
| CLI orchestrator | `cli.py` | `HermesCLI`, ~13.4K LOC |
| Model tools | `model_tools.py` | Tool orchestration, ~870 LOC |
| Session DB | `hermes_state.py` | SQLite, schema v11, ~3K LOC |
| Context compressor | `agent/context_compressor.py` | 1,556 LOC |
| Credential pool | `agent/credential_pool.py` | 1,603 LOC |
| Error classifier | `agent/error_classifier.py` | `FailoverReason` taxonomy |
| Delegate tool | `tools/delegate_tool.py` | 2,767 LOC |
| MCP client | `tools/mcp_tool.py` | 3,408 LOC |
| Curator | `agent/curator.py` | 1,781 LOC |
| Usage pricing | `agent/usage_pricing.py` | 866 LOC |
| Tool registry | `tools/registry.py` | `ToolEntry` + auto-discovery |
| Approval | `tools/approval.py` | 1,367 LOC |
| Memory manager | `agent/memory_manager.py` | 555 LOC + plugin ABC |
