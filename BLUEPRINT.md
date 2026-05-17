# Claude Code Internals — Architecture Reference

> This document maps Claude Code's architecture through the lens of **routing optimization research**.
> All code references point to `claude-code-source/claude-src-code/src/`.
> Python analogies provided throughout — the codebase is TypeScript/React.
>
> Cross-referenced with: [VILA-Lab/Dive-into-Claude-Code](https://github.com/VILA-Lab/Dive-into-Claude-Code) (arXiv:2604.14228)

## Key Insight

> **"Only 1.6% of Claude Code's codebase is AI decision logic. The other 98.4% is deterministic infrastructure."**
> — Liu et al., "Dive into Claude Code" (2026)

All routing interception points live in that 1.6%. The infrastructure constrains what routing decisions are viable.

## Source Code Location

```
~/<repo-root>/claude-code-source/claude-src-code/src/
```

## Reference Materials

```
~/<repo-root>/reference/dive-into-claude-code/   ← VILA-Lab analysis repo
  docs/architecture.md     ← Their architectural decomposition
  docs/build-your-own-agent.md  ← Design decision guide for agent builders
  docs/related-resources.md     ← Curated community analysis links
  paper/                   ← Full arXiv paper (PDF)
  assets/                  ← Architecture diagrams (PNG)
```

## Architecture: 9-Layer Map

Claude Code is organized into 9 architectural layers. Each layer contains **routing decision points** — places where a model router could intercept to optimize cost, latency, or quality.

```
┌─────────────────────────────────────────────────┐
│  Layer 0: SESSION ARCHITECTURE                  │  ← Container for everything
├─────────────────────────────────────────────────┤
│  Layer 1: MODEL SELECTION & ROUTING             │  ← Which model for this call?
├─────────────────────────────────────────────────┤
│  Layer 2: MEMORY HIERARCHY                      │  ← 4-tier persistent → working memory
├─────────────────────────────────────────────────┤
│  Layer 3: CONTEXT ASSEMBLY & TOKEN BUDGETING    │  ← Building the prompt
├─────────────────────────────────────────────────┤
│  Layer 4: API ECONOMICS                         │  ← Caching, compression, retry, cost
├─────────────────────────────────────────────────┤
│  Layer 5: COMPACTION                            │  ← Context compression (summarization)
├─────────────────────────────────────────────────┤
│  Layer 6: THE AGENT LOOP                        │  ← Core orchestration cycle
├─────────────────────────────────────────────────┤
│  Layer 7: TOOL EXECUTION PIPELINE               │  ← Dispatch, permissions, execution
├─────────────────────────────────────────────────┤
│  Layer 8: MULTI-AGENT & SUBAGENT ORCHESTRATION  │  ← Spawning, coordination, swarm
└─────────────────────────────────────────────────┘

Cross-cutting: Feature gates (GrowthBook/Statsig) control which routing paths are active.
```

---

## Layer 0: Session Architecture

**Role:** The runtime container — every other layer operates inside a session.

**Python analogy:** A session is like a Python process with a UUID. The `.jsonl` transcript is like Python's `logging.FileHandler` — append-only, one JSON per line.

### Key Files
- `bootstrap/state.ts` — Session state initialization, sessionId generation (line 331)
- `history.ts` — Prompt history persistence, global history.jsonl
- Session transcripts stored at `~/.claude/projects/{project-hash}/{sessionId}.jsonl`

### Session Lifecycle
```
CREATE: randomUUID() at startup
  → CACHE: metadata buffered in-memory (no disk write yet)
  → MATERIALIZE: first real message writes .jsonl to disk
  → APPEND: each turn adds messages via insertMessageChain() with parentUuid linking
  → RESUME: /resume → switchSession() atomically swaps sessionId + sessionProjectDir
  → FLUSH: pending entries written on idle or process exit
```

### Key Concepts
- **Session ID:** UUID v4, generated fresh every CLI invocation
- **Transcript:** JSONL file with message chain (each message has `parentUuid` → linked list)
- **Parent sessions:** `parentSessionId` tracks lineage (plan mode → implementation)
- **Subagent sessions:** Each subagent gets own `.jsonl` in subdirectory, tagged with `agentId`
- **Teleport:** NOT machine-to-machine. Means local CLI → remote CCR server handoff. Session UUID carries forward
- **Resume:** `switchSession()` (line 468-479) atomically swaps both sessionId AND sessionProjectDir (always a pair). Cost state restored across resumed segments
- **Cost restoration:** On resume, previous session's token counts + model usage + wall-clock duration accumulate

### Critical Design Choice: Permissions Never Restored on Resume
Trust is re-established every session. When `/resume` restores a session, cost state and conversation carry over, but **all permission grants are reset**. A router that stores session state across resumes must account for this — tool approval patterns will differ in resumed sessions.

### Routing Relevance
- Session is the **cost accounting boundary** — all token spend tracked per session
- Resuming a session restores cost state → router can see "budget remaining"
- Parent-child session lineage enables hierarchical cost tracking across plan → implementation flows
- Permission resets on resume → first few turns in a resumed session may have higher friction (more permission prompts)

---

## Layer 1: Model Selection & Routing

**Role:** Determines which Claude model handles each API call. The most direct routing surface.

**Python analogy:** Like a `model_factory.get_model(config)` with a priority chain — env vars override config override defaults. Similar to how you'd configure `MODEL_NAME` in a ML pipeline's `config.yaml`.

### Key Files
- `utils/model/model.ts` — Core model resolution (getMainLoopModel line 92, getRuntimeMainLoopModel line 145)
- `utils/model/configs.ts` — Model ID mappings per provider (Anthropic, Bedrock, Vertex)
- `utils/model/agent.ts` — Subagent model resolution (getAgentModel line 37)
- `utils/fastMode.ts` — Fast mode state machine (availability, cooldown, overage)
- `utils/modelCost.ts` — Pricing tiers per model (line 27-127)
- `commands/model/` — /model command implementation
- `commands/fast/` — /fast toggle
- `commands/effort/` — Effort level (low/medium/high/max)

### Model Resolution Priority Chain
```
1. Runtime override (/model command)        → getMainLoopModelOverride()
2. Startup flag (--model)                   → from CLI args
3. Environment variable                     → ANTHROPIC_MODEL
4. Saved user settings                      → from config
5. Subscription-tier default:
   - Max/Team Premium → Opus
   - Pro/PAYG/Enterprise → Sonnet
```

### Pricing (per million tokens)

| Model | Input | Output | Cache Read | Cache Write |
|-------|-------|--------|------------|-------------|
| Haiku 4.5 | $1 | $5 | — | — |
| Sonnet (all) | $3 | $15 | $0.30 | $3.75 |
| Opus 4.6 | $5 | $25 | — | — |
| Opus 4.6 Fast | $30 | $150 | — | — |
| Opus 4/4.1 | $15 | $75 | — | — |

### Fast Mode
- Only Opus 4.6 supports fast mode (6x cost multiplier)
- State machine: active → cooldown (rate limit hit) → active (after reset)
- Gated by: feature flag, subscription tier (Max/Team Premium only), first-party provider only
- `getFastModeRuntimeState()` (line 199) returns 'active' or 'cooldown' with resetAt timestamp

### Subagent Model Selection
`getAgentModel()` in `utils/model/agent.ts` (line 37):
```
1. Env override: CLAUDE_CODE_SUBAGENT_MODEL
2. Tool-specified model parameter
3. Agent definition's model field
4. Default: inherit parent's model
```
Family alias matching: if subagent says "sonnet", it inherits parent's exact Sonnet version.

### Routing Interception Points
| Point | Function | File:Line | Data Available |
|-------|----------|-----------|----------------|
| Main model | `getRuntimeMainLoopModel()` | model.ts:145 | permissionMode, tokenCount, mainLoopModel |
| Model parse | `parseUserSpecifiedModel()` | model.ts:445 | user alias string |
| Subagent model | `getAgentModel()` | agent.ts:37 | parent model, agent definition, env |
| Fast mode gate | `isFastModeAvailable()` | fastMode.ts:72 | org status, subscription, provider |
| Model validation | `isModelAllowed()` | model.ts:73 | allowlist from config |

---

## Layer 2: Memory Hierarchy

**Role:** 4-tier memory system — from persistent files to working conversation. Each tier crossing costs tokens.

**Python analogy:** Like a CPU cache hierarchy (L1/L2/L3/RAM) but for LLM context. Tier 1 is disk (slow, persistent), Tier 4 is the context window (fast, expensive per-token).

### Key Files
- `memdir/memdir.ts` — Memory directory management, truncation (MAX_ENTRYPOINT_LINES=200)
- `services/extractMemories/extractMemories.ts` — Background memory extraction after each turn
- `services/SessionMemory/sessionMemory.ts` — Periodic session notes
- `services/teamMemorySync/` — Shared team memory
- `utils/memory/` — Memory utilities
- `context.ts` — CLAUDE.md + git context loading (getUserContext line 155)

### The 4 Tiers

```
Tier 1: PERSISTENT (disk)
├── ~/.claude/projects/<slug>/memory/*.md   ← individual memory files
├── ~/.claude/projects/<slug>/memory/MEMORY.md  ← index (≤200 lines, ≤25KB)
└── Written by: extractMemories service OR model's Write tool
    Survives: across sessions, forever

Tier 2: SESSION (evolving notes)
├── Session-scoped markdown file
├── Updated periodically (every N tool calls or M tokens)
├── Triggered by: hasMetUpdateThreshold()
└── Written by: forked background agent
    Survives: within session only

Tier 3: IN-CONTEXT (semi-persistent)
├── CLAUDE.md files → loaded into system prompt every turn
├── Compaction summaries → replace old messages after compression
├── Team memory → shared across swarm teammates
└── Loaded at: context assembly time
    Survives: until next compaction

Tier 4: WORKING (immediate)
├── Raw conversation messages in context window
├── Most expensive tier (full token cost per message)
└── What compaction operates on
    Survives: until compacted or session ends
```

### Memory Extraction (Tier 4 → Tier 1)
- Runs as a **background forked agent** after each turn completes
- Counts messages since last extraction via `countModelVisibleMessagesSince()`
- Checks if main agent already wrote auto-memory (mutually exclusive per turn)
- Uses `buildExtractAutoOnlyPrompt` with Write/Edit tools limited to memory paths
- **Cost:** Uses main model — a router could use a cheaper model here

### Session Memory (Tier 4 → Tier 2)
- Feature-gated (`tengu_session_memory` via GrowthBook)
- Initialization threshold: waits for N tokens before first write
- Update threshold: triggers every N tool calls or M tokens
- Uses forked agent with FileEditTool

### Routing Relevance
- Every tier crossing is a **token cost** — extracting, loading, and compacting all consume tokens
- Memory extraction and session memory both fork background agents using the main model
- **Router opportunity:** Use cheaper model (Haiku) for memory extraction — it's just note-taking
- MEMORY.md index capped at 200 lines — router doesn't need to worry about unbounded growth

---

## Layer 3: Context Assembly & Token Budgeting

**Role:** Builds the actual API prompt from memory tiers, tool definitions, system instructions — all under a token budget.

**Python analogy:** Like assembling a Jinja2 template with variables, then checking `len(tokenizer.encode(prompt)) < max_context`.

### Key Files
- `context.ts` — System context (git status) + user context (CLAUDE.md) assembly
- `utils/context.ts` — Token budget constants and context window calculation
- `tools.ts` — Tool registry and filtering (getTools, assembleToolPool)
- `Tool.ts` — Tool interface definition (buildTool, line 783)
- `tools/ToolSearchTool/ToolSearchTool.ts` — Deferred tool loading

### Context Window Sizes
| Config | Value |
|--------|-------|
| Default context window | 200K tokens |
| 1M context (Opus/Sonnet 4.6 [1m]) | 1M tokens |
| Default max output | 8K tokens (capped slot reservation) |
| Escalated max output (retry) | 64K tokens |
| Opus 4.6 default output | 64K tokens |
| Sonnet 4.6 default output | 32K tokens |
| Compact max output | 20K tokens (reserved for summaries) |

### 9 Ordered Context Sources (ref: VILA-Lab architecture.md)
```
1. System prompt (base instructions)
2. Environment info (git status, date, platform — truncated to MAX_STATUS_CHARS=2000)
3. CLAUDE.md hierarchy (4 levels — see below)
4. Path-scoped rules (.claude/rules/*.md)
5. Auto-memory (MEMORY.md index + selected memory files)
6. Tool metadata (schemas, deferred markers, MCP tools)
7. Conversation history (Tier 4 messages)
8. Tool results (from previous iterations)
9. Compact summaries (Tier 3, from prior compaction)
```

### CLAUDE.md 4-Level Hierarchy
| Level | Path | Scope |
|-------|------|-------|
| Managed | `/etc/claude-code/CLAUDE.md` | System-wide (enterprise) |
| User | `~/.claude/CLAUDE.md` | Per-user defaults |
| Project | `CLAUDE.md`, `.claude/CLAUDE.md`, `.claude/rules/*.md` | Per-project |
| Local | `CLAUDE.local.md` | Personal, gitignored |

**Critical design:** CLAUDE.md is **user context** (probabilistic compliance), NOT system prompt (deterministic). Permission rules provide the deterministic enforcement layer.

### What Fills the Context (Token Breakdown)
```
System prompt (~2-5K tokens)
├── Git status (truncated to MAX_STATUS_CHARS=2000)
├── CLAUDE.md files (all 4 hierarchy levels)
├── Memory mechanics instructions (if memory path set)
└── Current date + environment info

Tool definitions (variable, significant)
├── Always-loaded tools (~15 core tools)
├── Deferred tools (loaded on demand via ToolSearchTool)
├── MCP tools (from connected servers)
└── Tool ordering: sorted by name for cache stability

Conversation history
├── Previous messages (Tier 4)
├── Compaction summaries (Tier 3, if compacted)
└── Attachment messages (file contents, skill output)
```

### Deferred Tool Loading
- Problem: 60+ tools in system prompt = massive token waste
- Solution: Tools with `shouldDefer: true` sent with `defer_loading: true` flag
- Model uses `ToolSearchTool` to discover and load deferred tools on demand
- Scoring: MCP tools ranked by server name (weight 12), tool name (10), searchHint (4), description (2)

### Context Window Resolution
`getContextWindowForModel()` (utils/context.ts line 51):
```
1. User env override (CLAUDE_CODE_MAX_CONTEXT_TOKENS)
2. [1m] suffix on model name → 1M
3. Model capability registry (max_input_tokens)
4. 1M beta header + model support
5. Default: 200K
```

### Routing Relevance
- Tool definitions are a **significant token cost** — a router that preloads only relevant tools saves tokens
- System prompt is relatively stable → good for prompt caching
- Context window size affects compaction threshold → larger window = fewer (expensive) compaction calls
- **Router opportunity:** "This is a file-editing task → preload only file tools" or "This is a git task → preload only Bash"

---

## Layer 4: API Economics (Caching, Compression, Retry, Cost)

**Role:** Everything between "prompt assembled" and "response received" that affects cost. The most critical layer for routing optimization.

**Python analogy:** Like a `requests.Session` with retry logic, connection pooling, and response caching — but the "cache" is the server's KV cache, and hits are 10x cheaper.

### Key Files
- `services/api/claude.ts` — Full API call pipeline (main file, ~3000+ lines)
- `services/api/client.ts` — Anthropic client creation, auth, provider detection
- `services/api/withRetry.ts` — Retry logic with model fallback
- `services/api/promptCacheBreakDetection.ts` — Cache break monitoring
- `cost-tracker.ts` — Session cost aggregation
- `costHook.ts` — Cost event hooks
- `utils/modelCost.ts` — Pricing calculation

### 4a. Prompt Caching

The single biggest cost optimization. Cached input tokens cost **10x less**.

**How it works:**
- `cache_control` headers attached to system prompt and message blocks
- TTL: 5 minutes (default) or 1 hour (gated by `tengu_prompt_cache_1h_config`)
- 1h TTL requires: feature gate + query source allowlist (supports wildcards like `agent:*`)
- `getCacheControl()` (claude.ts line 358-434) determines TTL per call

**Cache stability strategies:**
- Tool schemas sorted by name → adding a tool doesn't reorder and bust cache
- Advisor tool appended AFTER cached schemas → toggling advisor doesn't invalidate prefix
- `cache_reference` markers on tool_result blocks reference cached content without re-sending

**Cache_edits (incremental patching):**
- Instead of re-sending 100K+ tokens, send ~100 bytes of deletion instructions
- Server patches its cached KV state surgically
- Only works with `cache-editing` beta header on Opus 4.6
- Implemented at claude.ts lines 3050-3202

**Cache break detection (promptCacheBreakDetection.ts):**
- Hashes system prompt + tools + betas + cache scopes
- Detects when cache should've held but didn't
- Logs diffs for root cause analysis

### 4b. 5-Stage Pre-Model Compaction Pipeline (ref: VILA-Lab)

Executed **sequentially before every model call**, cheapest first — a graduated lazy-degradation strategy:

| Stage | Strategy | Trigger | Cost |
|-------|----------|---------|------|
| 1. Budget Reduction | Per-message size caps | Always active | Free |
| 2. Snip | Trim older history | Feature-gated (`HISTORY_SNIP`) | Free |
| 3. Microcompact | Cache-aware tool result stripping | Always (time-based), optional cache-aware | Free |
| 4. Context Collapse | Read-time virtual projection (non-destructive) | Feature-gated (`CONTEXT_COLLAPSE`) | Free |
| 5. Auto-Compact | Full model-generated summary (last resort) | When all else fails | 20K output tokens |

If an earlier stage frees enough tokens, later stages are skipped entirely.

### 4c. Detailed Compression Mechanisms

7 compression levels, cheapest first:

| Mechanism | Type | Cost | File:Line |
|-----------|------|------|-----------|
| Microcompact (time-based) | Replace old tool results with placeholder | Free (in-memory edit) | microCompact.ts:402 |
| Tool result offloading | >128KB results → disk, preview to API | Free (disk write) | toolResultStorage.ts:175 |
| Image compression | PNG→JPEG cascade (quality 80→60) | Free (local) | imageResizer.ts:240 |
| Snip compact | Delete old messages entirely | Free (lossy removal) | query.ts:401 |
| Cache edits (microcompact) | Surgical cache prefix patching | ~100 bytes | microCompact.ts:296 |
| Context collapse | Replace message spans with collapsed blocks | Feature-gated | query.ts:440 |
| Auto-compact (summarization) | Full conversation summary | 20K output tokens | compact.ts (Layer 5) |

### 4c. Retry & Fallback

`withRetry.ts` — not just error handling, but a **routing mechanism**:

- **529 (overloaded):** Retry up to 3 times, then trigger `FallbackTriggeredError` → model switch
- **429 (rate limit):** Same retry logic
- **Query source awareness:** Only foreground queries (repl_main_thread, agent:*) retry; background summaries bail immediately to reduce gateway amplification
- **Persistent retry mode:** `CLAUDE_CODE_UNATTENDED_RETRY` env var enables indefinite retry with heartbeat for unattended sessions
- **Fast mode fallback:** Rate-limited fast Opus auto-degrades to normal Opus

### 4d. Rate Limiting

`services/claudeAiLimits.ts`:
- Per-window utilization from headers: `anthropic-ratelimit-unified-{5h|7d}-utilization`
- Early warning thresholds: 90% at 72% time elapsed (5h window)
- Overage fallback: header `anthropic-ratelimit-unified-fallback: available`
- Rate limit type tracking: 5h session, 7d weekly, model-specific

### 4e. Cost Tracking

`cost-tracker.ts` — `addToTotalSessionCost()` (line 278):
- Receives: cost (USD), Usage object, model string
- Extracts: input_tokens, output_tokens, cache_read, cache_creation, web_search_requests
- Aggregates by canonical model name
- Persists to project config between sessions
- Logs OpenTelemetry metrics

### 4f. Background Token Consumers

These fork agents in the background, consuming tokens invisibly:

| Service | What | Model Used | Shares Cache? |
|---------|------|------------|---------------|
| extractMemories | Durable memory extraction after each turn | Main model | Yes (forked) |
| SessionMemory | Periodic session notes | Main model | Yes (forked) |
| PromptSuggestion | Speculative next-turn generation | Main model | Yes (forked) |
| AgentSummary | Coordinator progress summaries every 30s | Main model | Yes (forked) |

### The Hidden Cost Function for Routing

```
real_cost = (tokens × price_per_token) + cache_miss_penalty

where cache_miss_penalty = cached_tokens × (full_price - cached_price)
```

A model switch that breaks cache can cost MORE than the per-token savings. Router must factor in cache state.

### Routing Interception Points
| Point | Function | File:Line | Impact |
|-------|----------|-----------|--------|
| Cache TTL | `getCacheControl()` | claude.ts:358 | 5min vs 1h cache lifetime |
| Cache eligibility | `getPromptCache1hEligible()` | claude.ts:406 | Gates 1h cache |
| Retry decision | `withRetry()` | withRetry.ts:84 | Query source → retry/bail |
| Fallback trigger | `FallbackTriggeredError` | withRetry.ts:144 | Model degradation |
| Cost recording | `addToTotalSessionCost()` | cost-tracker.ts:278 | Per-call tracking |
| Background fork model | Various forked agents | extractMemories.ts:49 | Could use cheaper model |

---

## Layer 5: Compaction

**Role:** Compresses conversation history (Tier 4 → Tier 3) when context approaches limits. The most expensive compression mechanism.

**Python analogy:** Like `summarizer.summarize(conversation[-N:])` — you pay for a full API call to get a condensed version, then swap the original messages with the summary.

### Key Files
- `services/compact/compact.ts` — Core compaction logic (~1500 lines)
- `services/compact/autoCompact.ts` — Auto-trigger thresholds
- `services/compact/microCompact.ts` — Tool result stripping
- `services/compact/prompt.ts` — Compaction prompt template (9-section analysis)
- `services/compact/sessionMemoryCompact.ts` — Session memory compaction
- `commands/compact/` — /compact command

### Compaction Triggers
```
Auto-compact threshold = contextWindow - maxOutputTokens(20K) - buffer(13K)

Example (200K window): 200K - 20K - 13K = 167K tokens triggers auto-compact
Example (1M window):   1M  - 20K - 13K = ~967K tokens triggers auto-compact
```

Circuit breaker: MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES = 3 (stops retry loop)

### Compaction Flow
```
1. Trigger: token count exceeds threshold OR /compact command OR prompt-too-long error
2. Strip images from messages
3. Create compaction prompt (9-section analysis template)
4. Try forked agent path (shares prompt cache with parent) → cheaper
5. Fallback to streaming path if fork fails
6. Max output: 20K tokens (COMPACT_MAX_OUTPUT_TOKENS)
7. Post-compact restoration:
   ├── Re-inject up to 5 recently-read files (50K token budget)
   ├── Preserve active skill content (25K budget, 5K per skill)
   ├── Restore plan state
   └── Re-announce new tools/agents
```

### Partial Compaction
Two directions:
- **'up_to':** Summarize messages BEFORE pivot, keep messages AFTER (cache-preserving for tail)
- **'from':** Summarize messages AFTER pivot, keep messages BEFORE (cache breaks because summary precedes kept messages)

### What Gets Preserved in Compaction Summary
The prompt (prompt.ts) instructs 9 analysis sections:
1. Primary request & intent
2. Key technical concepts
3. Files and code sections (full snippets preserved)
4. Errors and fixes
5. Problem solving steps
6. All raw user messages (non-tool-result)
7. Pending tasks
8. Current work focus
9. Optional next step (direct quotes)

### Routing Relevance
- Compaction uses the **main loop model** — Opus compaction costs 15x more than Haiku
- Forked agent path shares prompt cache → cheaper than cold start
- **Router opportunity #1:** Use Haiku/Sonnet for compaction (summarization doesn't need Opus reasoning)
- **Router opportunity #2:** Adjust compaction threshold based on budget (compact earlier = less context = cheaper subsequent calls)
- **Router opportunity #3:** Choose microcompact (free, lossy) over full compact (expensive) based on conversation criticality

---

## Layer 6: The Agent Loop

**Role:** Core orchestration — the `while True` loop that drives everything. Receives input, calls API, processes tools, decides whether to continue.

**Python analogy:** Like a `while True: response = model.generate(); if response.has_tool_calls: execute_tools(); else: break` loop.

### Key Files
- `QueryEngine.ts` — Loop owner, submitMessage() entry point (line 209)
- `query.ts` — Main loop implementation (~1700 lines)
- `query/stopHooks.ts` — Stop hook evaluation
- `services/api/claude.ts` — API call (anthropic.messages.create at line 1822)

### 9-Step Turn Pipeline (ref: VILA-Lab)
```
1. Settings resolution → 2. State initialization → 3. Context assembly →
4. Five pre-model shapers → 5. Model call → 6. Tool dispatch →
7. Permission gate → 8. Tool execution → 9. Stop condition check
```

### Turn Lifecycle (Detailed)
```
User types message
  ↓
QueryEngine.submitMessage() [async generator, yields messages incrementally]
  ↓
processUserInput() → decides: shouldQuery? allowedTools? modelFromUserInput?
  ↓
if !shouldQuery → handle locally (slash command), return
  ↓
query() [MAIN LOOP starts]
  ↓
  ┌──→ Check compaction needed (Layer 5)
  │    ↓
  │    Build API request (Layer 3 context + Layer 1 model)
  │    ↓
  │    callModel() → queryModelWithStreaming() → queryModel()
  │    → anthropic.beta.messages.create() [LINE 1822 in claude.ts]
  │    ↓
  │    Stream response: accumulate text + tool_use blocks
  │    ↓
  │    stop_reason == 'tool_use'? ──YES──→ Execute tools (Layer 7)
  │    │                                    ↓
  │    │                                    Append tool results
  │    │                                    ↓
  │    └────────────────────────────────── LOOP BACK
  │
  └── stop_reason == 'end_turn' OR max_turns OR budget exhausted → EXIT
```

### Exit Conditions
1. No tool_use blocks in response → done
2. Max turns reached → done with error
3. Token budget exhausted → done or continue (configurable +500K extension)
4. Stop hooks blocking → retry with hook message or exit
5. prompt-too-long error → recover via compaction, then retry

### Key Decision Points
| Decision | File:Line | Logic |
|----------|-----------|-------|
| Should query API? | QueryEngine.ts:556 | `if (!shouldQuery)` → local-only |
| Which model? | QueryEngine.ts:274-276 | From processUserInput or default |
| Tool filtering | claude.ts:1154-1172 | Deferred, LSP, discovered tools |
| Tool use detected? | query.ts:1062 | `toolUseBlocks.length > 0` |
| Execution mode | query.ts:1366-1378 | Streaming vs batch executor |
| Continue looping? | query.ts:1704-1728 | maxTurns, budget, stop hooks |

### Routing Relevance
- Each loop iteration is a **separate API call** — router can change model between iterations
- After tool results come back, next iteration might only need formatting → cheaper model sufficient
- Per-iteration routing: "Tool results are complex code → keep Opus" vs "Just acknowledging user → switch to Haiku"
- Stop hooks can inject follow-up messages → additional API calls the router should account for

---

## Layer 7: Tool Execution Pipeline

**Role:** When the model decides to use a tool — validate, check permissions, execute, format results.

**Python analogy:** Like middleware in Flask/Django: `permission_middleware(tool) → validate(args) → handler(args) → format_response()`. Tools are like registered route handlers.

### Key Files
- `Tool.ts` — Tool interface (buildTool line 783, interface at line 362-695)
- `tools.ts` — Registry and filtering (getAllBaseTools line 193, getTools line 271)
- `services/tools/toolExecution.ts` — Execution flow
- `utils/permissions/permissions.ts` — Permission decision system
- `tools/` — 40+ individual tool implementations

### Tool Interface Contract
Every tool implements (Python-equivalent):
```python
class Tool:
    name: str                    # unique ID ("Bash", "Read", etc.)
    input_schema: Schema         # like pydantic model for validation
    async def call(input, context, permission_fn) -> ToolResult
    async def description() -> str
    async def check_permissions(input) -> PermissionResult
    def is_enabled() -> bool
    def is_read_only() -> bool
    def should_defer() -> bool   # load on demand via ToolSearchTool?
    search_hint: str             # 3-10 words for ToolSearch matching
```

### Tool Call Flow
```
1. Model returns tool_use block with {tool_name, input}
2. findToolByName() → look up in merged pool (built-in + MCP)
3. validateInput() → schema check → ValidationResult with error if invalid
4. Permission check (4-step cascade):
   ├── Step 1a: Deny rules (blanket blocks, no content match needed)
   ├── Step 1b: Pre-tool hooks (custom classifiers, async)
   ├── Step 2: Tool's own checkPermissions()
   ├── Step 3: Allow rules (always-allow from settings/session)
   └── Step 4: Default → ask user OR auto-deny/allow by mode
5. Execute: tool.call() with context + progress callback
6. Format: mapToolResultToToolResultBlockParam()
7. Budget: applyToolResultBudget (truncate large results)
8. Post-hooks: runPostToolUseHooks()
```

### 7 Permission Modes (ref: VILA-Lab)
| Mode | Behavior | Trust Level |
|------|----------|-------------|
| `plan` | User approves all plans before execution | Lowest |
| `default` | Standard interactive approval | Low |
| `acceptEdits` | File edits + filesystem shell auto-approved | Medium |
| `auto` | ML classifier evaluates tool safety | High |
| `dontAsk` | No prompting, deny rules still enforced | Higher |
| `bypassPermissions` | Skips most prompts, safety-critical checks remain | Highest |
| `bubble` | Internal: subagent escalation to parent | Special |

### Permission Classifiers
- **Bash classifier:** Pattern-matches dangerous commands (rm -rf, etc.)
- **Yolo classifier (`auto` mode):** Separate LLM call evaluating safety independently. Two-stage: fast-filter + chain-of-thought. Races pre-computed classification against a timeout
- **Filesystem validator:** Checks path allowlists
- **Denial tracking:** After N denials, switches from auto-deny to prompting (fallback)

### Concurrency
- Concurrency-safe tools (Read, Glob, Grep): run in parallel
- Blocking tools (Bash, Edit): serialized queue

### Tool Pool Assembly
```
getAllBaseTools() → all possible tools (feature-gated)
  ↓
getTools() → filter by:
  ├── Mode (SIMPLE → only Bash/Read/Edit)
  ├── Deny rules
  ├── isEnabled() check
  └── REPL filtering
  ↓
assembleToolPool() → merge built-in + MCP tools, deduplicate
```

### Routing Relevance
- Pre-tool hooks are an existing **interception point** → add routing logic here
- Tool choice affects follow-up iterations: complex tool results may need Opus to interpret
- Permission denials waste tokens (model tried, got blocked, must retry differently)
- **Router opportunity:** "This Bash command is simple → next iteration can use Sonnet" or "File edit failed → keep Opus for error recovery"

---

## Layer 8: Multi-Agent & Subagent Orchestration

**Role:** Spawning child agents, delegating work, aggregating results. The richest routing surface.

**Python analogy:** Like `multiprocessing.Pool.apply_async()` — parent dispatches tasks to workers, each with their own context. Three execution patterns correspond to different Python concurrency models.

### Key Files
- `tools/AgentTool/AgentTool.tsx` — Agent tool (spawning entry point)
- `tools/AgentTool/forkSubagent.ts` — Fork mechanism (child inherits parent context)
- `tools/AgentTool/runAgent.ts` — Agent lifecycle orchestration
- `tools/AgentTool/agentToolUtils.ts` — Tool filtering for agents (line 70-116)
- `utils/swarm/spawnInProcess.ts` — In-process teammate spawning
- `utils/swarm/inProcessRunner.ts` — Teammate execution wrapper
- `coordinator/coordinatorMode.ts` — Coordinator pattern (line 36-369)
- `tools/SendMessageTool/SendMessageTool.ts` — Inter-agent communication
- `tasks/LocalAgentTask/` — Background agent task
- `tasks/InProcessTeammateTask/` — In-process teammate task
- `tasks/RemoteAgentTask/` — Remote agent task
- `tasks/DreamTask/` — Memory consolidation agent
- `constants/tools.ts` — Tool allowlists per agent type

### Three Orchestration Patterns

#### Pattern 1: Background Agents (AgentTool → LocalAgentTask)
**Python analogy:** `asyncio.create_task()` — fire and forget, get result later.

- Spawned via `Agent` tool with `description`, `prompt`, optional `model`
- Runs async in main process, separate context
- 15 tools allowed (file ops, web, bash — no Agent spawning, no task management)
- Parent continues executing after spawn
- **Fork variant:** Child inherits parent's full conversation + system prompt → prompt cache hits

#### Pattern 2: In-Process Teammates (Swarm)
**Python analogy:** `threading.Thread` with `threading.local()` — same process, isolated context via `AsyncLocalStorage`.

- Spawned via `spawnInProcessTeammate()` → `InProcessTeammateTask`
- Same Node.js process, shared file state, shared MCP servers
- Full tool set + task management + inter-teammate messaging
- Deterministic ID: `"name@teamName"`
- Communication via mailbox system (`teammateMailbox.ts`)

#### Pattern 3: Coordinator Mode
**Python analogy:** Manager pattern — main agent becomes a dispatcher with only 4 tools.

- Gated by env var `CLAUDE_CODE_COORDINATOR_MODE` (mutually exclusive with fork mode)
- Coordinator gets only: Agent, TaskStop, SendMessage, SyntheticOutput
- Workers get: Bash, Read, Edit, MCP tools only
- Workers notify coordinator via `<task-notification>` XML when done
- Coordinator synthesizes results from multiple workers

### Tool Allowlists by Agent Type
| Agent Type | Tools Allowed | Key Exclusions |
|------------|--------------|----------------|
| Background (async) | 15 tools: File, Web, Bash, Glob, Grep, Skill | No Agent (no recursion), no TaskStop, no AskUser |
| In-Process Teammate | All async + TaskCreate/Get/Update + SendMessage + TeamCreate/Delete | Full capability |
| Coordinator | 4 tools only: Agent, TaskStop, SendMessage, SyntheticOutput | No file ops, no bash |
| Coordinator Workers | Bash, Read, Edit, MCP | No Agent, no task mgmt |

### Inter-Agent Communication
- `SendMessageTool` — coordinator→worker, teammate→teammate
- Structured message types: `shutdown_request`, `shutdown_response`, `plan_approval_response`
- Broadcast: send to `"*"` for all teammates
- Mailbox system for async delivery

### SkillTool vs AgentTool: The Cost Divide
| Mechanism | Context Cost | How It Works |
|-----------|-------------|--------------|
| **SkillTool** | Low (~same window) | Injects instructions into current context. No new API call for the injection itself. |
| **AgentTool** | High (~7x tokens) | Spawns new isolated context window. Full system prompt + tools re-sent. Summary-only return. |

**Python analogy:** SkillTool is like `exec(skill_code, current_namespace)` — runs in your process. AgentTool is like `subprocess.Popen()` — new process, new memory, only stdout comes back.

A router should prefer SkillTool when the task fits the current context window, and AgentTool only when isolation is needed (long-running research, file-heavy exploration, parallel work).

### Agent Lifecycle
```
SPAWN: Agent tool called with prompt + optional model
  ↓
REGISTER: LocalAgentTask/InProcessTeammateTask added to AppState.tasks
  ↓
EXECUTE: Agent runs its own query loop (same Layer 6 logic, own context)
  ↓
COMMUNICATE: SendMessage for coordinator pattern, mailbox for teammates
  ↓
COMPLETE: Result serialized back to parent conversation
  ↓
TEARDOWN: Task removed from AppState, TeamDeleteTool for swarms
```

### Routing Relevance
- **Each subagent spawn is an independent model selection decision** — the richest routing surface
- Today: mostly "inherit parent's model" via `getAgentModel()`
- `CLAUDE_CODE_SUBAGENT_MODEL` env var can override ALL subagent models
- **Router opportunities:**
  - Exploration/research tasks → Haiku (cheap, fast, good enough for grep/read)
  - Code editing tasks → Sonnet (5x cheaper than Opus, strong at code)
  - Architecture decisions → Opus (needs deep reasoning)
  - Memory extraction (DreamTask) → Haiku (just note-taking)
  - Coordinator summaries (AgentSummary) → Haiku (formatting only)
- Fork variant shares prompt cache → model switch would break cache (same Layer 4 constraint)

---

## Cross-Cutting: Feature Gates

Feature flags control which routing paths exist. Key gates:

| Gate | Controls | System |
|------|----------|--------|
| `tengu_penguins_off` | Fast mode availability | Statsig |
| `tengu_prompt_cache_1h_config` | 1-hour cache TTL | GrowthBook |
| `tengu_session_memory` | Session memory service | GrowthBook |
| `CLAUDE_CODE_COORDINATOR_MODE` | Coordinator pattern | Env var |
| `HISTORY_SNIP` | Snip compact | Feature flag |
| `CONTEXT_COLLAPSE` | Context collapse | Feature flag |
| `isAdvisorEnabled()` | Advisor model (Claude-in-Claude) | Config |
| `isToolSearchEnabledOptimistic()` | Deferred tool loading | Config |

For a custom routing framework, these gates would be replaced with the router's own decision logic.

---

## Quick Reference: All Routing Interception Points

| Layer | Interception Point | File | What You Can Route |
|-------|--------------------|------|--------------------|
| L1 | `getRuntimeMainLoopModel()` | model.ts:145 | Main model per call |
| L1 | `getAgentModel()` | agent.ts:37 | Subagent model |
| L1 | `isFastModeAvailable()` | fastMode.ts:72 | Fast vs normal speed |
| L3 | `getTools()` / `assembleToolPool()` | tools.ts:271 | Which tools loaded |
| L3 | `isDeferredTool()` | tools.ts | Eager vs lazy tool loading |
| L4 | `getCacheControl()` | claude.ts:358 | Cache TTL |
| L4 | `withRetry()` | withRetry.ts:84 | Retry vs fallback model |
| L4 | Background fork agents | extractMemories.ts:49 | Model for background work |
| L5 | Compaction model | compact.ts:1188 | Model for summarization |
| L5 | `getAutoCompactThreshold()` | autoCompact.ts:72 | When to trigger compaction |
| L6 | Per-iteration model | query.ts (loop) | Model between tool iterations |
| L7 | Pre-tool hooks | toolExecution.ts | Intercept before tool runs |
| L8 | `AgentTool` model param | AgentTool.tsx | Per-subagent model |
| L8 | Coordinator worker spawn | coordinatorMode.ts | Worker model assignment |

---

## Custom Skills

Available skills for navigating this architecture (in `.claude/commands/`):

- `/trace-query` — Trace a query's path through all 9 layers
- `/routing-seams` — List all routing interception points by layer
- `/cost-model` — Show pricing tiers and estimate cost for scenarios
- `/agent-tree` — Visualize agent spawning patterns and model assignments
- `/layer-deep` — Deep dive into any specific layer by number

---

## Related Resources (from VILA-Lab curated list)

### Architecture Analysis Repos
- [VILA-Lab/Dive-into-Claude-Code](https://github.com/VILA-Lab/Dive-into-Claude-Code) — Source-level analysis, values→principles→implementation framework
- [Yuyz0112/claude-code-reverse](https://github.com/Yuyz0112/claude-code-reverse) — Visualize LLM interactions, prompt tracing
- [AgiFlow/claude-code-prompt-analysis](https://github.com/AgiFlow/claude-code-prompt-analysis) — API request/response logs across 5 sessions

### Buildable Research Forks
- [T-Lab-CUHKSZ/claude-code](https://github.com/T-Lab-CUHKSZ/claude-code) — CUHK buildable research fork
- [ultraworkers/claw-code](https://github.com/ultraworkers/claw-code) — Rust reimplementation (~20K lines vs 512K TS)
- [777genius/claude-code-working](https://github.com/777genius/claude-code-working) — Runnable reverse-engineered CLI

### Learning Resources
- [shareAI-lab/learn-claude-code](https://github.com/shareAI-lab/learn-claude-code) — 19-chapter Python agent course
- [Haseeb Qureshi — Cross-agent comparison](https://gist.github.com/Haseeb-Qureshi/2213cc0487ea71d62572a645d7582518) — Claude Code vs Codex vs Cline vs OpenCode

### Key Blog Posts for Routing Research
- [ClaudeCodeCamp — "How Prompt Caching Actually Works"](https://www.claudecodecamp.com/p/how-prompt-caching-actually-works-in-claude-code) — Cache economics deep dive
- [George Sung — "Tracing Claude Code's LLM Traffic"](https://medium.com/@georgesung/tracing-claude-codes-llm-traffic-agentic-loop-sub-agents-tool-use-prompts-7796941806f5) — Discovered dual-model usage (Opus main + Haiku metadata)
- [MindStudio — "Three-Layer Memory Architecture"](https://www.mindstudio.ai/blog/claude-code-source-leak-memory-architecture) — Best resource on memory system
- [Agiflow — "Reverse Engineering Prompt Augmentation"](https://agiflow.io/blog/claude-code-internals-reverse-engineering-prompt-augmentation/) — 5 prompt augmentation mechanisms with network traces
