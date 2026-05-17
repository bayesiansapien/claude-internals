# Cost Drivers & Routing Priorities in Claude Code

> Based on deep analysis of Claude Code internals (source at `claude-code-source/claude-src-code/src/`).
> Framing: which bottlenecks are highest-impact AND fit within a routing decision layer (rule-based or learned)?

---

## Table of Contents

1. [All Cost Drivers Ranked](#1-all-cost-drivers-ranked)
2. [What Is and Isn't a Routing Problem](#2-what-is-and-isnt-a-routing-problem)
3. [Tier 1: Highest Priority](#3-tier-1-highest-priority)
4. [Tier 2: Strong Candidates](#4-tier-2-strong-candidates)
5. [What's Not Worth Routing Research Time](#5-whats-not-worth-routing-research-time)
6. [Recommended Research Sequence](#6-recommended-research-sequence)
7. [Where the Genetic Framework Fits](#7-where-the-genetic-framework-fits)

---

## 1. All Cost Drivers Ranked

Over a medium session (2–4 hours, 30–50 turns, Opus model):

| Rank | Cost Driver | % of Total Cost | Notes |
|------|-------------|-----------------|-------|
| 1 | Growing conversation history re-sent every turn | ~30–40% | Cache helps; cache misses hurt badly |
| 2 | Background agents running on main model | ~20–25% | extractMemories, SessionMemory, AgentSummary, PromptSuggestion |
| 3 | Auto-compaction calls | ~10–15% | 20K output tokens, main model, 1–5 events per long session |
| 4 | Subagent cold-start overhead | ~10–20% | If subagents used; 7x token overhead vs SkillTool |
| 5 | Tool schemas in every call | ~10–15% | ~15K–30K tokens; cache covers when stable |
| 6 | Large tool results accumulating in context | ~varies | File reads, Bash output; stays until compaction |
| 7 | Cache misses after tool pool changes | ~varies | Each miss = 10x cost on cached prefix |
| 8 | MCP tool schema accumulation | ~5–10% | No cap; unbounded per session |
| 9 | Image results bypassing result budget | ~blowout risk | maxResultSizeChars=Infinity; excluded from 200K aggregate |
| 10 | PromptSuggestion (speculative pre-generation) | ~invisible | 1 extra API call per turn if feature-gated on |

### Where the Money Goes (Approximate)

```
Growing history re-sent          ~30-40%   ← structural, compaction handles it
Background agents on main model  ~20-25%   ← ROUTING TARGET
Compaction events (1-5x)         ~10-15%   ← ROUTING TARGET
Tool schemas + cache misses      ~10-15%   ← ROUTING TARGET (cache constraint)
Subagent overhead                ~10-20%   ← ROUTING TARGET
MCP + image blowouts             ~5-10%    ← design fix needed
```

---

## 2. What Is and Isn't a Routing Problem

**Routing** = a decision-making layer (rule-based or learned) that intercepts at an architectural seam to optimize cost/latency/quality.

### NOT routing problems (structural — can't route around them):
- History accumulation → compaction handles it; routing can only affect compaction timing/model
- Tool results accumulating → microcompaction handles it
- Images bypassing budget → design bug, needs a code fix
- MCP accumulation → design fix (selective connection), partially routing

### ARE routing problems (decision seam exists):
- Which model handles each background agent task
- Which model handles compaction
- Which model handles each subagent
- Whether to switch models between agent loop iterations
- Whether a model switch is worth it given current cache state

---

## 3. Tier 1: Highest Priority

### A. Background Agents → Haiku

**Cost impact:** 20–25% of total session cost  
**Implementation effort:** Near zero  
**Research value:** Low (rule-based sufficient)  
**Source seam:** `utils/model/agent.ts:37` (`getAgentModel()`), `CLAUDE_CODE_SUBAGENT_MODEL` env var already exists

The four background agents that silently consume tokens:

| Agent | Trigger | Task Type |
|-------|---------|-----------|
| `extractMemories` | After each turn | Note-taking → Haiku |
| `SessionMemory` | Periodic threshold | Note-taking → Haiku |
| `AgentSummary` | Every 30 seconds | Formatting → Haiku |
| `PromptSuggestion` | After each turn | Speculative → Haiku or disable |

**Routing rule (trivial classifier):**
```python
def route_background_agent(task_description: str) -> str:
    HAIKU_KEYWORDS = ["extract memory", "session notes", "summarize progress",
                      "update memory", "write notes"]
    if any(kw in task_description.lower() for kw in HAIKU_KEYWORDS):
        return "claude-haiku-4-5"
    return parent_model  # inherit default
```

**Why it's Tier 1:** Ship this as a baseline immediately. Zero quality risk (memory extraction ≠ Opus-level reasoning). Use the cost savings to fund harder research.

---

### B. Cache-Aware Routing as a Constraint

**Cost impact:** High and underappreciated  
**Implementation effort:** Moderate  
**Research value:** HIGH — novel contribution, no existing routing paper addresses prompt cache economics  
**Source seam:** `services/api/claude.ts:358` (`getCacheControl()`), `services/api/promptCacheBreakDetection.ts`

**The core insight:** Model switching breaks the prompt cache. A naive router that switches Opus → Sonnet to save per-token costs can cost MORE if it busts a 25K-token cached prefix.

```
Cache miss penalty on 25K prefix (Opus pricing):
  Uncached:  25K × $15/M  = $0.375
  Cached:    25K × $1.5/M = $0.0375
  Penalty:   $0.34 per cache bust event

If 50 turns × $0.34 penalty = $17 in cache miss overhead
vs. Opus→Sonnet savings: 50 turns × ~2K output × ($75-$15)/M = $6
→ The switch COSTS MORE than it saves
```

**Routing as constrained optimization:**
```python
def should_switch_model(
    current_model: str,
    candidate_model: str,
    cached_prefix_tokens: int,
    turns_remaining_estimate: int,
    cache_ttl_remaining_seconds: int
) -> bool:
    cache_miss_penalty = cached_prefix_tokens * (FULL_PRICE - CACHED_PRICE)
    per_turn_savings = estimate_per_turn_savings(current_model, candidate_model)
    total_savings = per_turn_savings * turns_remaining_estimate
    return total_savings > cache_miss_penalty
```

**Research contribution:** Formalize routing as:
```
minimize: Σ(model_cost × tokens)
subject to: cache_penalty_if_switch(model_A, model_B, prefix_size) < projected_savings
```

This is the first cache-constrained LLM routing formulation. Genuinely novel.

---

### C. Subagent Task-Type Routing

**Cost impact:** High when subagents used (model choice × 7x spawn overhead)  
**Implementation effort:** Moderate  
**Research value:** HIGH — predicting required model capability from task description before execution  
**Source seam:** `tools/AgentTool/AgentTool.tsx` (before spawn), `model` param in `.claude/agents/*.md`

Before a subagent runs, you have its `prompt` and `description`. That's sufficient signal to classify required capability level:

```python
# Task type → model mapping
TASK_ROUTING = {
    "exploration":    "claude-sonnet-4-6",   # grep, read, search
    "summarization":  "claude-haiku-4-5",    # summarize, format, extract
    "implementation": "claude-sonnet-4-6",   # write code, edit files
    "architecture":   "claude-opus-4-7",     # design, plan, reason
    "verification":   "claude-sonnet-4-6",   # test, review, validate
    "debugging":      "claude-opus-4-7",     # root cause, error recovery
}

def classify_subagent_task(prompt: str, description: str) -> str:
    # Rule-based baseline OR trained classifier
    ...
```

**The ML research angle:** Train a lightweight classifier (or use Haiku itself) to predict capability level from the subagent prompt. The classifier runs on Haiku (~$0.001) and pays for itself in 1 correct routing decision (Opus→Sonnet saves ~$0.05 per 1K output tokens).

**Training signal:** Run tasks on multiple models, use task success rate as label.

---

## 4. Tier 2: Strong Candidates

### D. Phase-Based Turn-Level Routing

**Cost impact:** Medium — per-turn model choice across the agent loop  
**Research value:** MEDIUM-HIGH — predicting workflow phase from tool call history  
**Source seam:** `query.ts` inner loop, between iterations

Identifiable phases over a long coding task:

```
Phase 1: Exploration     (Read, Glob, Grep tools called)       → Sonnet sufficient
Phase 2: Planning        (no tools, just text reasoning)        → Opus needed
Phase 3: Implementation  (Edit, Write, Bash tools called)       → Sonnet sufficient
Phase 4: Verification    (Bash with test commands, review)      → Sonnet sufficient
Phase 5: Error Recovery  (debugging, failed tests, re-planning) → Opus needed
```

**Phase detector (rule-based):**
```python
def detect_phase(recent_tool_calls: list[str]) -> str:
    READ_TOOLS = {"Read", "Glob", "Grep", "WebSearch", "WebFetch"}
    WRITE_TOOLS = {"Edit", "Write", "NotebookEdit"}
    EXEC_TOOLS = {"Bash"}

    if all(t in READ_TOOLS for t in recent_tool_calls):
        return "exploration"
    if any(t in WRITE_TOOLS for t in recent_tool_calls):
        return "implementation"
    if not recent_tool_calls:
        return "planning"  # pure reasoning turn
    return "mixed"
```

**Constraint:** Phase transitions must check cache state (from B) before switching models.

---

### E. Compaction Model Routing

**Cost impact:** $2–$4 per compaction event, 1–5 events per long session  
**Research value:** MEDIUM — raises empirical quality question  
**Source seam:** `services/compact/compact.ts:1188`

Compaction is summarization. Sonnet does summarization well. Routing compaction to Sonnet saves 40% per event.

**The research question worth studying:** Does compaction quality (what's preserved vs. lost in the summary) affect downstream task quality? If you compact with Haiku and lose a critical code snippet, the next 20 turns spend tokens recovering it — did you actually save anything?

**This is an offline evaluation problem:**
```
Run N sessions with {Opus, Sonnet, Haiku} compaction
Measure: downstream task success rate, re-read rate (proxy for lost context)
Output: Pareto frontier of (compaction_cost, downstream_quality_loss)
```

---

## 5. What's Not Worth Routing Research Time

| Area | Reason |
|------|--------|
| Tool schema preloading (dynamic pool) | Cache stability constraint: changing the tool pool busts the cache prefix → likely costs more than it saves |
| PromptSuggestion on/off | Binary feature flag; routing decision is trivial (just disable it) |
| Image budget bypass | Pure design bug; needs a code fix, not a routing layer |
| MCP tool accumulation | Design fix (selective connection at session start); routing can help but it's an initialization decision, not per-turn |
| History accumulation | Structural; compaction timing/model choice covers it |

---

## 6. Recommended Research Sequence

```
Phase 1 — Baseline (ship fast, funds everything else)
  └─ A: Background agents → Haiku
         Rule-based, env var already exists, zero quality risk
         Expected savings: 20-25% of session cost

Phase 2 — Theory (novel contribution)
  └─ B: Cache-aware routing constraint
         Formalize as constrained optimization
         Novel: no existing paper covers prompt cache economics in routing
         Expected output: routing constraint formulation + empirical validation

Phase 3 — ML Contribution
  └─ C: Subagent task-type classifier
         Train on (task_description → model) pairs
         Fitness signal: (task_success_rate, cost)
         Expected output: lightweight classifier, training methodology

Phase 4 — Full System
  └─ D: Phase-based turn routing (combines B + C)
         Per-iteration routing in the agent loop
         Cache-constrained model switching
         Expected output: end-to-end routing policy

Phase 5 — Empirical Study
  └─ E: Compaction quality evaluation
         Pareto analysis: compaction model vs. downstream quality
         Expected output: empirical paper + recommended compaction policy
```

---

## 7. Where the Genetic Framework Fits

The genetic/evolutionary framework fits best at **Phase 3 and 4**:

**Genome:** A routing policy — a mapping from (context_features) → (model_choice)

```python
# Context features available at each routing decision point:
context_features = {
    "task_description": str,         # subagent prompt
    "recent_tool_calls": list[str],  # phase signal
    "turn_number": int,              # session progress
    "cached_prefix_tokens": int,     # cache state
    "cache_ttl_remaining": float,    # cache freshness
    "session_cost_so_far": float,    # budget remaining
    "output_tokens_last_turn": int,  # complexity signal
    "tool_result_size_last_turn": int, # context growth rate
}
```

**Fitness function:**
```python
def fitness(policy, session_trajectories):
    return alpha * task_success_rate(policy, sessions) \
         - beta * total_cost(policy, sessions)
    # tune alpha/beta to explore the Pareto frontier
```

**Evolution target:** Discover routing policies that dominate the Pareto frontier of (task_quality, session_cost). A policy that routes background agents to Haiku + switches to Sonnet during exploration phases + uses Opus only for planning/debugging could hit 40–60% cost reduction with minimal quality loss.

**Key architectural seams for policy interception:**

| Seam | File | Decision |
|------|------|----------|
| `getAgentModel()` | `utils/model/agent.ts:37` | Model per background/subagent task |
| `getRuntimeMainLoopModel()` | `utils/model/model.ts:145` | Main loop model per turn |
| `AgentTool` spawn | `tools/AgentTool/AgentTool.tsx` | Model per subagent |
| `getCacheControl()` | `services/api/claude.ts:358` | Cache TTL (affects switching cost) |
| `compact.ts:1188` | `services/compact/compact.ts` | Model for compaction |

---

## Key Numbers to Remember

```
Cache miss penalty on 25K prefix (Opus): ~$0.34 per bust
Compaction cost (Opus, 150K→20K summary): ~$2.63–$3.75 per event
Background agent cost saved by Haiku:     ~80% reduction per background call
Subagent spawn overhead vs SkillTool:     ~7x more tokens
Phase 1 routing (background→Haiku):       ~20–25% session cost reduction
Potential total reduction (all tiers):    ~40–60% with minimal quality loss
```

---

## Source References

| Concept | File | Line |
|---------|------|------|
| `getAgentModel()` — subagent model selection | `utils/model/agent.ts` | 37 |
| `getRuntimeMainLoopModel()` — main model per call | `utils/model/model.ts` | 145 |
| `getCacheControl()` — cache TTL logic | `services/api/claude.ts` | 358 |
| Cache break detection | `services/api/promptCacheBreakDetection.ts` | — |
| Compaction model selection | `services/compact/compact.ts` | 1188 |
| Background memory extraction | `services/extractMemories/extractMemories.ts` | 49 |
| AgentSummary timer | `services/AgentSummary/agentSummary.ts` | — |
| AgentTool spawn entry point | `tools/AgentTool/AgentTool.tsx` | — |
| `CLAUDE_CODE_SUBAGENT_MODEL` env var | `utils/model/agent.ts` | — |
