# Caching and Routing in Claude Code — Deep Analysis

> Notes from architecture deep-dive conversations exploring prompt caching mechanics,
> model switching costs, and routing optimization opportunities in Claude Code.
> All code references point to `claude-code-source/claude-src-code/src/`.

---

## Table of Contents

1. [The #1 Cost Driver: Quadratic Context Growth](#1-the-1-cost-driver-quadratic-context-growth-in-the-agent-loop)
2. [The #2 Cost Driver: Background Forks](#2-the-2-cost-driver-background-forks-that-clone-full-context)
3. [The #3 Cost Driver: Compaction Itself](#3-the-3-cost-driver-compaction-itself)
4. [The Multiplier Effect](#4-the-multiplier-effect)
5. [Where a Router Should Focus](#5-where-a-router-should-focus)
6. [How Prompt Caching Actually Works](#6-how-prompt-caching-actually-works)
7. [What Happens When You Switch Models Mid-Conversation](#7-what-happens-when-you-switch-models-mid-conversation)
8. [When Switching Doesn't Pay Off: Cache Thrashing](#8-when-switching-doesnt-pay-off-cache-thrashing)
9. [What's Actually In the Cache: KV Tensors](#9-whats-actually-in-the-cache-kv-tensors-not-text)
10. [How a Cache Hit Is Checked: Prefix Token Matching](#10-how-a-cache-hit-is-checked-prefix-token-matching)
11. [Why a Model Switch Completely Destroys the Cache](#11-why-a-model-switch-completely-destroys-the-cache)
12. [What Claude Code Controls Client-Side](#12-what-claude-code-controls-client-side)
13. [The Golden Rule for Router Design](#13-the-golden-rule-for-router-design)
14. [Key Source Code References](#14-key-source-code-references)

---

## 1. The #1 Cost Driver: Quadratic Context Growth in the Agent Loop

The single biggest cost factor in Claude Code is structural — every iteration of the agent loop re-sends ALL previous messages to the API.

Think of it like this in Python:

```python
messages = []

for turn in range(N):
    messages.append(user_message)
    
    # THIS IS THE EXPENSIVE PART:
    # Every iteration sends ALL previous messages to the API
    response = api.call(
        system_prompt=system,      # ~15K tokens (cached, cheap)
        tools=tool_definitions,     # ~10K tokens (cached, cheap)  
        messages=messages           # GROWS every iteration!
    )
    
    messages.append(response)
    if response.has_tool_calls:
        tool_results = execute_tools(response)
        messages.append(tool_results)  # 5-50K per tool result
```

### The Math

In a 10-turn tool-using conversation:

- Turn 1: Send 25K tokens (system + tools + message)
- Turn 2: Send 25K + 8K (previous response + tool result) = 33K
- Turn 3: Send 33K + 8K = 41K
- ...
- Turn 10: ~97K tokens

**Total input tokens across all 10 turns: ~610K** — not 97K. You pay for every previous message in every subsequent call.

Without prompt caching, this is **O(N²)** cost growth. With caching (system prompt + tools stay cached at 10x discount), the stable prefix is cheap — but the growing conversation tail pays full price every time.

### Cost at Different Model Tiers

On Opus 4.6 at $5/Mtok input: that 610K input alone = ~$3.
On Opus Fast at $30/Mtok: ~$18.
Same conversation on Sonnet at $3/Mtok: ~$1.80.

### Source Code Evidence

In `query.ts`, line 1716 shows the recursive state accumulation:

```typescript
messages: [...messagesForQuery, ...assistantMessages, ...toolResults]
```

Every API call response gets appended. The `toolResults` array (built from line 1395) accumulates ALL tool outputs — file reads, grep results, web searches. **Nothing is removed between iterations**; messages only compact when thresholds trigger.

In `services/api/claude.ts`, line 1822 calls `anthropic.beta.messages.create()` where `params.messages` is the FULL uncompacted array. No pre-API trimming happens — all messages go to the API intact.

---

## 2. The #2 Cost Driver: Background Forks That Clone Full Context

After each turn, Claude Code forks **up to 4 background agents** — all inheriting the full conversation:

| Service | What It Does | Context Inherited | Frequency |
|---------|-------------|-------------------|-----------|
| extractMemories | Save durable memories to disk | Full conversation | Every turn |
| SessionMemory | Update session notes | Full conversation | Every N tool calls |
| PromptSuggestion | Speculate next user message | Full conversation | Every turn |
| AgentSummary | Progress summary for coordinator | Full conversation | Every 30s |

Each fork re-sends the parent's full message history. They do share the prompt cache prefix (so system + tools are cheap), but the conversation portion pays full price.

### The Multiplier

If your main conversation is 80K tokens and 3 background forks run, that's 3 × 80K = 240K extra input tokens per turn — **invisible** because `cost-tracker.ts` doesn't break down by source. It all blends into one total.

### Why It's Invisible

`cost-tracker.ts` (lines 278-323) `addToTotalSessionCost()` aggregates ALL costs into a single `modelUsage` map keyed by model name. There's NO tracking of:

- Main loop vs. background forks
- Compact overhead
- Session memory extraction
- Query source (compact vs session_memory vs main loop all blend together)

A session burning 500K tokens on compact + session memory operations is invisible — only total cost is tracked.

---

## 3. The #3 Cost Driver: Compaction Itself

When context hits the threshold (~187K for a 200K window), auto-compaction triggers. This is a full API call that:

- Sends the entire conversation as input
- Generates up to **20K output tokens** (the summary)
- Uses the **main model** (if you're on Opus, you pay Opus prices for summarization)

### Cost Per Compaction Event

On Opus 4.6: 20K output tokens × $25/Mtok = **$0.50 per compaction event**.
On Sonnet: 20K × $15/Mtok = $0.30.

### The Post-Compaction Inflation

After compaction, the system re-injects:
- Up to 5 recently-read files (50K token budget)
- Active skill content (25K budget, 5K per skill)
- Plan state

So post-compaction context isn't small — it can immediately be 75K+, meaning the next few turns are still expensive.

### The Autocompact Gap

From `autoCompact.ts`:

- Autocompact threshold (line 72): `effectiveContextWindow - 13,000` tokens
- Blocking limit (line 124): `effectiveContextWindow - 3,000` tokens
- Gap: 10,000-token window between "autocompact fires" and "hard block"

For Opus (200K window): autocompact triggers at ~187K tokens. During that 10K-token gap, every new message re-sends the full 180K+ context. If a tool result is 5K tokens, 2 iterations waste 30K+ in the gap.

Circuit breaker: `MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES = 3` stops the retry loop.

---

## 4. The Multiplier Effect

The three cost drivers multiply each other:

```
Total cost ≈ Σ(per_turn_context × price) + (N_forks × context × price) + (compaction_cost)

Where per_turn_context grows linearly → cumulative cost grows quadratically
And N_forks multiplies each turn's cost by 2-4x
And compaction adds a ~$0.50 spike periodically
```

### Concrete Scenario: 30-Turn Coding Session on Opus 4.6

**Naive (single model, no optimization):**
- Main loop: ~2M cumulative input tokens × $5/Mtok = **$10**
- Background forks: ~1.5M extra tokens × $5/Mtok = **$7.50**
- 2 compaction events: ~$1
- Output tokens: ~300K × $25/Mtok = **$7.50**
- **Total: ~$26**

**Optimally routed (Sonnet for main, Haiku for forks, Haiku for compaction):**
- Main loop: 2M × $3/Mtok = $6
- Background forks: 1.5M × $1/Mtok = $1.50
- Compaction: ~$0.10
- Output: 300K × $15/Mtok = $4.50
- **Total: ~$12** (54% savings)

---

## 5. Where a Router Should Focus

In priority order of cost impact:

1. **Per-iteration model selection in the agent loop** — the quadratic growth means each turn's model choice matters more as the conversation grows. Later turns are the most expensive.

2. **Background fork model selection** — these are invisible overhead. Using Haiku for memory extraction and session notes instead of Opus would save 60-80% of fork costs.

3. **Compaction model selection** — easy win, summarization doesn't need Opus-level reasoning.

4. **Cache-aware routing** — a model switch that breaks the prompt cache costs more than keeping a slightly more expensive model that maintains cache hits.

---

## 6. How Prompt Caching Actually Works

Every time you send a message, the system sends the **entire conversation** to the API — not just your latest message, but everything from the beginning. Think of it like an email thread where you keep forwarding the entire chain every time you reply.

### What Gets Sent on Every Single API Call

```
┌─────────────────────────────────────────────┐
│  PART A: System Prompt + Tool Definitions   │  ← ~25K tokens
│  (Same every turn. Never changes.)          │     
├─────────────────────────────────────────────┤
│  PART B: Conversation History               │  ← Grows each turn
│  (All previous messages + tool results)     │     
├─────────────────────────────────────────────┤
│  PART C: Your Latest Message                │  ← New each turn
└─────────────────────────────────────────────┘
```

Without caching, you pay full price for A + B + C every turn. That's wasteful because **Part A never changes** and **Part B only grows** (old messages don't change, new ones are appended).

### What the Cache Does

Anthropic's servers remember the **prefix** of your previous request. If the next request starts with the same bytes, they skip re-processing those tokens and charge **10x less** for them.

```python
# Turn 1: No cache exists yet
request_1 = system_prompt + tools + "User: fix the bug in auth.py"
# Server processes ALL 30K tokens at full price ($5/Mtok for Opus)
# Server saves: cache["opus"] = processed_state_for(request_1)

# Turn 2: Cache exists!
request_2 = system_prompt + tools + "User: fix the bug in auth.py" + "Assistant: I'll look..." + "Tool: [file content]" + "User: now add tests"
#           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
#           This prefix is IDENTICAL to request_1
#           Server recognizes it → charges only $0.50/Mtok (10x cheaper)
#           Only the NEW part (assistant response + tool + new message) pays full price
```

### Concrete 5-Turn Example on Opus

Part A (system + tools) = 25K tokens. Each turn adds ~8K tokens (assistant response + tool results + your message).

```
Turn 1:
  Send: [25K system+tools] + [2K your message]         = 27K tokens
  Cache: nothing cached yet
  Cost:  27K × $5/Mtok = $0.135  (all full price)
  After: server caches the 27K prefix

Turn 2:
  Send: [25K system+tools] + [2K msg1] + [8K turn1 results] + [2K msg2] = 37K tokens
  Cache: first 27K tokens match cache → CACHE HIT
  Cost:  27K × $0.50/Mtok + 10K × $5/Mtok = $0.0135 + $0.05 = $0.064
  After: server updates cache to 37K prefix

Turn 3:
  Send: [25K] + [2K] + [8K] + [2K] + [8K] + [2K]      = 47K tokens
  Cache: first 37K match → CACHE HIT
  Cost:  37K × $0.50 + 10K × $5 = $0.0185 + $0.05 = $0.069

Turn 4:
  Send: 57K tokens total
  Cache: first 47K match → HIT
  Cost:  47K × $0.50 + 10K × $5 = $0.0235 + $0.05 = $0.074

Turn 5:
  Send: 67K tokens total  
  Cache: first 57K match → HIT
  Cost:  57K × $0.50 + 10K × $5 = $0.0285 + $0.05 = $0.079
```

**Total cost for 5 turns: $0.42**

The pattern: only the **new tokens** (last ~10K) pay full price. Everything before is cached and cheap.

---

## 7. What Happens When You Switch Models Mid-Conversation

### The Switch Itself: Trivially Cheap

When you type `/model sonnet`, the code does exactly one thing: changes a string variable in memory.

```python
# Python equivalent of what happens (commands/model/model.tsx:53-56)
STATE.mainLoopModelOverride = "claude-sonnet-4-6-20260401"
# That's it. No reconstruction. No re-initialization.
```

Conversation messages, system prompt, tool definitions — **all stay identical**. The next time the agent loop iterates, it reads the new model from state (`query.ts:572-578`) and passes it to the API call.

### The Model is Re-Resolved Every Iteration

The model is NOT cached at session start — it's read from AppState on every query call:

```typescript
// query.ts:572-578
let currentModel = getRuntimeMainLoopModel({
  permissionMode,
  mainLoopModel: toolUseContext.options.mainLoopModel,
  exceeds200kTokens: ...
})
```

### Cache Impact: The Expensive Part

Anthropic's server-side prompt cache is keyed by the tuple `(model, system_prompt, tools)`.

```python
cache_key = (model_name, hash(system_prompt), hash(tool_schemas))

# Before switch:
cache["opus-4-6", "abc123", "def456"] = cached_kv_state  # 80K tokens cached

# After switch to Sonnet:
# Cache lookup: ("sonnet-4-6", "abc123", "def456") → MISS
# The Opus cache is useless. Sonnet starts cold.
```

### Cost Comparison: Switch vs Stay

Say you're 15 turns into a conversation. Context is ~80K tokens. ~25K are system prompt + tools (the cached prefix), ~55K are conversation history.

| Scenario | Cached Prefix Cost | Conversation Cost | Total Input Cost |
|----------|-------------------|-------------------|-----------------|
| **No switch** (cache hit, Opus) | 25K × $0.50/Mtok = $0.0125 | 55K × $5/Mtok = $0.275 | **$0.29** |
| **After switch** (cache miss, Sonnet) | 25K × $3/Mtok = $0.075 | 55K × $3/Mtok = $0.165 | **$0.24** |
| **After switch** (cache miss, staying Opus) | 25K × $5/Mtok = $0.125 | 55K × $5/Mtok = $0.275 | **$0.40** |

### Cache Warming After a Switch

| Turn After Switch | Sonnet (warming cache) | Opus (cache still warm) |
|-------------------|----------------------|------------------------|
| Turn 1 (cold) | $0.24 (no cache) | $0.29 (cached) |
| Turn 2 (partial cache) | $0.18 (prefix cached) | $0.29 (cached) |
| Turn 3+ (warm) | $0.17 (fully cached) | $0.29 (cached) |

For Opus → Sonnet, the switch **pays for itself within 1-2 turns** because Sonnet is 40% cheaper per token. The cache miss is a one-time penalty.

### What Else Changes Beyond Cache

**Context window shifts immediately.** If you switch from Opus 4.6 [1m] (1M window) to Sonnet (200K window), and your context is already at 300K tokens — you'll immediately hit compaction, which costs another 20K output tokens to summarize.

**Model-specific betas change.** Cache-editing (`cache_edits`) only works on Opus 4.6. If you switch to Sonnet, that optimization pathway disappears. The code detects this and stops sending the `cache-editing` beta header.

**Quality changes.** This is harder to quantify, but:
- Complex code reasoning, architecture decisions → Opus is noticeably better
- Straightforward file edits, formatting, grep → Sonnet is equally good
- Simple acknowledgments, summaries → Haiku is sufficient

**No quality degradation from cache.** Quality is unaffected by cache state. The cache is purely an optimization of the computation — the model produces identical outputs whether the input was cached or not. It's like a CPU cache: cache miss means slower (more expensive), but the computation result is the same.

---

## 8. When Switching Doesn't Pay Off: Cache Thrashing

The dangerous case is **frequent switching** — the router oscillates:

```
Turn 1: Opus  (cold start, build cache)     → $0.40
Turn 2: Sonnet (cache miss, rebuild)        → $0.24  
Turn 3: Opus  (cache miss AGAIN, rebuild)   → $0.40
Turn 4: Sonnet (cache miss AGAIN)           → $0.24
```

Every switch throws away the previous model's cache. You're paying full price on every single turn. Compare to just staying on Opus:

```
Turn 1: Opus  (cold)   → $0.40
Turn 2: Opus  (cached) → $0.29
Turn 3: Opus  (cached) → $0.29
Turn 4: Opus  (cached) → $0.29
```

**4 turns with oscillation: $1.28. 4 turns steady on Opus: $1.27.** And on Opus you got better quality. The router achieved nothing.

---

## 9. What's Actually In the Cache: KV Tensors, Not Text

Each transformer layer computes **Key** and **Value** matrices from the input tokens:

```python
# Simplified — what happens at each transformer layer
K = input_embeddings @ W_k   # Key matrix:   shape (seq_len, d_head)
V = input_embeddings @ W_v   # Value matrix:  shape (seq_len, d_head)
Q = input_embeddings @ W_q   # Query matrix:  shape (seq_len, d_head)

attention = softmax(Q @ K.T / sqrt(d_head)) @ V
```

For a model like Claude with ~100+ layers, processing 50K tokens means computing K and V at every layer. That's the expensive part — it's the bulk of the forward pass.

**The cache stores these precomputed K and V tensors.** Not text. Not embeddings. The actual intermediate attention state.

```python
# What the cache conceptually holds:
cache = {
    "layer_0": {"K": tensor(50000, d_head), "V": tensor(50000, d_head)},
    "layer_1": {"K": tensor(50000, d_head), "V": tensor(50000, d_head)},
    ...
    "layer_99": {"K": tensor(50000, d_head), "V": tensor(50000, d_head)},
}
```

This is substantial memory — for a large model with 50K cached tokens, we're talking gigabytes of GPU memory holding these tensors.

---

## 10. How a Cache Hit Is Checked: Prefix Token Matching

The hit check is **not** a semantic similarity search or an embedding lookup. It's a **deterministic prefix match on the raw token IDs**.

```python
def check_cache(new_request_tokens: list[int], cached_tokens: list[int]):
    """
    Walk through token by token from the start.
    As long as tokens match, those positions are 'cache hits'.
    The moment they diverge, everything after is a 'cache miss'.
    """
    match_length = 0
    for i in range(min(len(new_request_tokens), len(cached_tokens))):
        if new_request_tokens[i] == cached_tokens[i]:
            match_length += 1
        else:
            break  # First mismatch → everything after is uncached
    
    return match_length  # This many tokens can reuse cached KV tensors
```

It's literally: "Do the first N tokens of this request match the first N tokens of the cached request?" If yes, reuse the KV tensors for those N tokens. Only compute new KV tensors for the remaining tokens.

### Why Order Matters

If you change even one token in the system prompt, the match breaks from that point onward and everything after pays full price — even if the rest is identical.

```python
# Example:
cached  = tokenize("You are Claude. Tools: [Bash, Read, Edit]. User: fix bug")
#         [1, 42, 99, 7, 8, 15, 3, 22, 67, 88, 44, 11, 55]

# CACHE HIT — same prefix, new suffix:
request = tokenize("You are Claude. Tools: [Bash, Read, Edit]. User: fix bug. Assistant: ...")
#         [1, 42, 99, 7, 8, 15, 3, 22, 67, 88, 44, 11, 55, 203, 77, ...]
#          ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^ MATCH (13 tokens cached)
#                                                               ^^^^^^ NEW (compute these)

# CACHE MISS — one token changed in the middle:
request = tokenize("You are Claude. Tools: [Bash, Read, Write]. User: fix bug")
#         [1, 42, 99, 7, 8, 15, 3, 22, 67, 88, 44, 11, 91]
#          ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^ MATCH (up to position 11)
#                                                 ^^ MISMATCH at "Write" vs "Edit"
#                                                    Everything after → full price
```

### Why Claude Code Sorts Tool Schemas by Name

This is a direct consequence of prefix matching. If tool schemas were in random order, adding one new tool could shift all subsequent tools, breaking the prefix match for the entire tool block. By sorting alphabetically, adding a new tool only affects the insertion point — everything before it still matches the cache.

From `tools.ts`: tool ordering is sorted by name for cache stability.

---

## 11. Why a Model Switch Completely Destroys the Cache

The KV tensors are computed using the **model's weight matrices** (W_k, W_v). Different models have different weights:

```python
# Opus weights:
K_opus = tokens @ W_k_opus    # Produces certain KV tensors

# Sonnet weights (completely different parameters):
K_sonnet = tokens @ W_k_sonnet  # Same tokens, DIFFERENT KV tensors

# Opus's cached KV tensors are meaningless to Sonnet.
# It's like caching the result of f(x) and trying to use it for g(x).
# Same input, different function, different output.
```

It's not that the cache is "thrown away" by choice — it's that the cached KV tensors are **mathematically incompatible** with a different model. Sonnet can't use Opus's attention states because they were computed with different weight matrices.

---

## 12. What Claude Code Controls Client-Side

The cache lives on **Anthropic's servers**, not in the Claude Code client. The Claude Code source code only manages:

### Cache Hints (Telling the Server What to Cache)

```python
# What Claude Code sends to the API (simplified):
request = {
    "model": "claude-opus-4-6",
    "system": [
        {
            "type": "text",
            "text": "You are Claude...",
            "cache_control": {"type": "ephemeral"}  # ← "Please cache up to here"
        }
    ],
    "tools": [...tool_definitions...],  # Sorted by name for cache stability
    "messages": [...]
}
```

The `cache_control` marker says: "Everything from the start up to this point should be cached." The server decides how long to keep it (5 minutes or 1 hour based on TTL).

### Cache TTL Management

From `services/api/claude.ts`, `getCacheControl()` (lines 358-434):

- Default: 5-minute TTL
- Extended: 1-hour TTL (gated by feature flag `tengu_prompt_cache_1h_config`)
- 1h TTL requires feature gate + query source allowlist (supports wildcards like `agent:*`)

### Cache Break Detection

From `services/api/promptCacheBreakDetection.ts`:

- Hashes system prompt + tools + betas + cache scopes
- Detects when cache should've held but didn't
- Explicitly detects model changes as cache breaks (line 334: `modelChanged = model !== prev.model`)
- Logs diffs for root cause analysis

### Cache Edits (Surgical KV Patching)

From `services/api/claude.ts` (lines 3050-3202):

- Instead of re-sending 100K+ tokens, send ~100 bytes of deletion instructions
- Server patches its cached KV state surgically
- Only works with `cache-editing` beta header on Opus 4.6

### Cache Economics in API Response

```python
# What comes back in the API response:
usage = {
    "input_tokens": 10000,              # New tokens (full price)
    "cache_read_input_tokens": 40000,   # Reused from cache (10x cheaper)
    "cache_creation_input_tokens": 0,   # New cache entries created
    "output_tokens": 2000               # Model's response
}
```

### Cache Stability Strategies in the Codebase

- Tool schemas sorted by name → adding a tool doesn't reorder and bust cache
- Advisor tool appended AFTER cached schemas → toggling advisor doesn't invalidate prefix
- `cache_reference` markers on tool_result blocks reference cached content without re-sending

---

## 13. The Golden Rule for Router Design

### BAD: Switch Models Frequently

```python
def naive_router(query):
    if is_simple(query): return "haiku"
    if is_medium(query): return "sonnet"
    return "opus"  # Every switch = cache miss
```

### GOOD: Switch Rarely, Stay on a Model for Runs of Turns

```python
def cache_aware_router(query, cache_state, turns_since_switch):
    if turns_since_switch < 3:
        return cache_state.current_model  # Don't switch yet, amortize cache
    
    # Only switch when savings over next N turns > cache rebuild cost
    savings_per_turn = estimate_savings(new_model, cache_state)
    cache_rebuild_cost = estimate_cache_miss(cache_state)
    
    if savings_per_turn * expected_remaining_turns > cache_rebuild_cost:
        return new_model
    return cache_state.current_model
```

### The Key Insight

A model switch has a **fixed cost** (cache rebuild) and a **per-turn benefit** (cheaper tokens). The router should only switch when the expected remaining turns justify the one-time cache penalty.

### The Hidden Cost Function

```
real_cost = (tokens × price_per_token) + cache_miss_penalty

where cache_miss_penalty = cached_tokens × (full_price - cached_price)
```

A model switch that breaks cache can cost MORE than the per-token savings.

### When Model Switching DOES Make Sense

**Switch once, stay (YES):**
- Start on Opus for complex planning → switch to Sonnet for implementation. One switch, cache rebuilds once, Sonnet savings accumulate over many turns. Net positive.
- User explicitly asks for a cheaper model for a low-stakes task.

**Different models for different agents (YES):**
- Main conversation on Opus, subagents on Sonnet/Haiku. No cache conflict because each agent has its own context. This is the biggest routing opportunity in Claude Code.

**Per-turn routing (NO):**
- "This turn is simple → Haiku, next turn is complex → Opus, next is simple → Haiku." Cache thrashes, costs spike, and the router overhead adds latency.

### The Real Routing Opportunity

The biggest opportunity isn't switching the main loop model per-turn. It's routing the **background services and subagents** — those are independent contexts with independent caches:

- extractMemories → Haiku (just note-taking, doesn't need Opus reasoning)
- SessionMemory → Haiku (periodic session notes)
- PromptSuggestion → Haiku (speculative, can be low quality)
- AgentSummary → Haiku (formatting only)
- Compaction → Sonnet (summarization, good enough quality)
- Exploration subagents → Sonnet/Haiku (grep/read tasks)
- Code editing subagents → Sonnet (strong at code, 5x cheaper than Opus)
- Architecture/planning → Opus (needs deep reasoning)

Each of these has its own context window and its own cache — no interference with the main loop's cache.

---

## 14. Key Source Code References

| Component | File | Line | What It Does |
|-----------|------|------|-------------|
| Model switch | commands/model/model.tsx | 53-56 | Sets `mainLoopModel` in AppState (string swap) |
| Model override state | bootstrap/state.ts | 838-850 | `getMainLoopModelOverride()` / `setMainLoopModelOverride()` |
| Per-iteration model resolution | query.ts | 572-578 | `getRuntimeMainLoopModel()` called every loop |
| API call | services/api/claude.ts | 1822 | `anthropic.beta.messages.create()` |
| Cache control headers | services/api/claude.ts | 358-434 | `getCacheControl()` — TTL determination |
| Cache break detection | services/api/promptCacheBreakDetection.ts | 334, 351 | `modelChanged` flag, hash comparison |
| Cache edits | services/api/claude.ts | 3050-3202 | Surgical KV cache patching |
| Context window resolution | utils/context.ts | 51 | `getContextWindowForModel()` |
| Cost tracking | cost-tracker.ts | 278-323 | `addToTotalSessionCost()` |
| Autocompact threshold | services/compact/autoCompact.ts | 72 | `getAutoCompactThreshold()` |
| Background fork — memories | services/extractMemories/extractMemories.ts | 49 | Forked agent for memory extraction |
| Background fork — session | services/SessionMemory/sessionMemory.ts | 43 | Forked agent for session notes |
| Message accumulation | query.ts | 1716 | `messages: [...messagesForQuery, ...assistantMessages, ...toolResults]` |
| Tool sort for cache stability | tools.ts | — | Tool schemas sorted by name |

---

## Pricing Reference

| Model | Input ($/Mtok) | Output ($/Mtok) | Cache Read ($/Mtok) | Cache Write ($/Mtok) |
|-------|---------------|-----------------|--------------------|--------------------|
| Haiku 4.5 | $1 | $5 | — | — |
| Sonnet (all) | $3 | $15 | $0.30 | $3.75 |
| Opus 4.6 | $5 | $25 | — | — |
| Opus 4.6 Fast | $30 | $150 | — | — |
| Opus 4/4.1 | $15 | $75 | — | — |
