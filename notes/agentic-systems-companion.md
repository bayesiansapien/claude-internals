# Agentic Systems · A First-Principles Companion

> **Companion document to the R&D talk on Claude Code, Hermes Agent, and OpenClaw.**
> Intended as a shareable reference for engineers who want depth beyond what slides can carry.
> Written conversationally — same tone as the talk itself.

---

## How to Read This Document

This document is structured as a layered reference. You can read it linearly or jump to specific sections.

- **Part 1 — First Principles.** What every agentic system is made of, and why.
- **Part 2 — Three Systems Compared.** Claude Code, Hermes Agent, and OpenClaw — what makes them radically different despite sharing the same primitives.
- **Part 3 — Claude Code Internals.** Technical deep dive into the architecture, layer by layer.
- **Part 4 — How Models Are Trained for Agents.** What's different about agentic training vs standard LLM training.
- **Part 5 — Cost & Routing Economics.** Where the money goes, where routing can help.
- **Part 6 — Glossary & References.** Vocabulary, source code pointers, further reading.

Sections are self-contained where possible. Cross-references are marked inline.

---

# Part 1 · First Principles

## 1.1 What Is an Agentic System?

Every production agentic system today decomposes into **eight components**, organized into four tiers:

```
TIER 0 — ReAct Core   ① Reasoning Engine
                      ④ Orchestration Loop
                      ⑤ Action Layer (Tools)

TIER 1 — State        ② Context Manager
                      ③ Memory System

TIER 2 — Governance   ⑥ Permission System
                      ⑦ Session Layer

TIER 3 — Scale        ⑧ Multi-Agent Layer
```

**The thesis:** *Model is the only reasoner. Everything else is harness.*

| # | Component | What it is | Why it must exist |
|---|---|---|---|
| 1 | **Reasoning Engine** | The LLM. Stateless: `context_in → context_out`. | Only thing that understands language. |
| 2 | **Context Manager** | Assembles what the model sees on each call. | Model is blind to anything outside the context window. |
| 3 | **Memory System** | Multi-tier: working → session → long-term. | Model has zero memory between calls. |
| 4 | **Orchestration Loop** | The `while`-loop. Drives perceive → act cycle. | Multi-step goals need iteration. |
| 5 | **Action Layer (Tools)** | Typed interfaces: schema + executor. | Text alone changes nothing in the world. |
| 6 | **Permission System** | Intercepts every action. Deny-first. | Model will attempt unsafe things. |
| 7 | **Session Layer** | Identity, cost tracking, audit trail. | Need boundary for trust + billing. |
| 8 | **Multi-Agent Layer** | Spawn, coordinate, communicate agents. | One context = one thread = one speed. |

### Where Does ReAct Fit?

The cycle **Perceive → Reason → Act → Observe → Repeat** is the **ReAct loop** (Yao et al., 2022). It corresponds to:
- Component **① Reasoning Engine** (the "Reason" step)
- Component **④ Orchestration Loop** (the "Repeat" engine)
- Component **⑤ Action Layer** (the "Act" step)

ReAct says **nothing** about the other five components. They're what production demanded *on top of* ReAct:
- Memory hierarchies didn't exist in the ReAct paper.
- Permission systems didn't exist in the ReAct paper.
- Multi-agent coordination didn't exist in the ReAct paper.

> **All three systems we're comparing (Claude Code, Hermes, OpenClaw) implement the ReAct loop. They differ entirely in what they wrap around it.**

### A Note on the Format

Modern systems use **structured tool-use blocks** via API function-calling — not the textual `Thought:/Action:/Observation:` format from the original ReAct paper. Functionally identical loop, mechanically enforced by the schema.

---

## 1.2 The Five Fundamental Tensions

Every component above exists because of one of these five tensions. **No tension, no component.**

| # | Tension | The Problem | Forces Which Component |
|---|---|---|---|
| **T1** | Statefulness vs Stateless Engine | Model forgets between calls. Tasks need 100s of turns. | Memory System, Session Layer |
| **T2** | Context Scarcity vs Growing Info Need | Window is finite. Tasks generate info faster than fits. | Context Manager |
| **T3** | Capability vs Safety | More tools = more capable AND more dangerous. `rm -rf` only needs to succeed once. | Permission System |
| **T4** | Cost vs Quality | Better model = better reasoning = $$$. Most tasks don't need the best. | Model Selection (routing) |
| **T5** | Parallelism vs Coherence | Parallel agents are faster but isolated. Merging is fragile. | Multi-Agent Layer |

### The Critical Split: Compensate vs Constrain

The harness has **two fundamentally different jobs**:

#### COMPENSATE (Engineering — Depreciates Over Time)

The harness does work **for** the model because the model has a weakness. If the weakness goes away, the work becomes unnecessary.

**Analogy:** Training wheels on a bicycle. The kid can't balance yet → you bolt training wheels on. The day they learn to balance, you take them off.

| Tension | What the model can't do | What the harness does to compensate |
|---|---|---|
| T1 Statefulness | Can't remember anything | Saves memory files to disk, re-injects them every session |
| T2 Context | Can't hold more than 200K tokens | Runs a 5-stage compaction pipeline |
| T4 Cost | Can't be cheap *and* smart | Routes to cheaper models when the task is simple |
| T5 Parallelism | Can only do one thing at a time | Spawns child agents in parallel |

**Compensating work depreciates** — if Claude 7 has a 10M token window, the compaction pipeline becomes dead code. Three years of engineering investment turns into liability.

#### CONSTRAIN (Governance — Permanent)

The harness puts walls **against** the model so it can't take dangerous actions. Even if the model becomes infinitely smarter, you still want those walls — because the issue isn't capability, it's *trust*.

**Analogy:** A guardrail on a mountain road. Even Formula 1 drivers want it. Skill doesn't remove the need — the cost of one failure is too high.

| Tension | What the model *can* do | What the harness does to constrain |
|---|---|---|
| T3 Safety | Run `rm -rf /`, exfiltrate API keys, push to prod | Deny-first permission system; every action checked before execution |

**Constraining work never depreciates** — a more capable model is a *more dangerous* model. The guardrails get more important, not less. A smarter Claude that can write better code can also write better malware. The permission system has to hold either way.

### The Single Most Important Insight

> **T1, T2, T4, T5 are engineering tensions. They depreciate as models improve.**
>
> **T3 is a governance tension. It never depreciates. It grows with regulation, with adoption, with stakes.**

This is the single most important sentence in the talk. Internalize it.

A smarter model can solve memory (T1). A bigger window can solve context (T2). Cheaper models can ease cost (T4). Native parallelism can solve coordination (T5). **Nothing solves safety (T3).** The model is probabilistic — it will eventually attempt something unsafe with non-zero probability. Compliance requires deterministic proof, not probabilistic hope.

---

## 1.3 Why the Harness Encodes Your Assumptions

**Different teams looked at these same five tensions and made completely different bets about which ones matter most.**

| If you believe… | You build… | Real example |
|---|---|---|
| Model is **dangerous + wasteful** | **Deep harness** — compensate hard, constrain hard | Claude Code |
| Model is **locked-in + can't learn** | **Wide harness** — providers + self-improvement | Hermes Agent |
| Model is **self-sufficient** | **Thin harness** — minimal loop, get out of the way | OpenClaw |

These aren't right-or-wrong choices. They're **forecasts about the future of models**. Time will pick the winner.

---

# Part 2 · Three Systems Compared

## 2.1 Core Beliefs

| | Claude Code | Hermes Agent | OpenClaw |
|---|---|---|---|
| **Builder** | Anthropic (commercial) | Nous Research (open-source, MIT) | Original maintainers (Hermes absorbed it) |
| **Language** | TypeScript (~512K LOC) | Python (~780K LOC) + TS TUI | Rust (~20K LOC) |
| **Provider** | Anthropic-only | 15+ providers, plugin-extensible | Multi-provider |
| **API format** | Anthropic Messages API (native) | OpenAI chat completions (lingua franca) | Multi-format adapters |
| **Core belief** | Model is powerful but dangerous + wasteful | Model is capable but locked-in + can't learn | Model is good enough; harness is the bottleneck |
| **Harness depth** | Deep | Wide | Thin |
| **Where is the product?** | The harness (98.4% infra / 1.6% AI logic) | The ecosystem (providers + skills + gateways) | The loop (everything else stripped) |

## 2.2 Comparison Across the Five Tensions

| Tension | Claude Code | Hermes Agent | OpenClaw |
|---|---|---|---|
| **T1 Statefulness** | ███ 4-tier memory. BG extraction. Team sync. | ██ 3-tier + 8 plugin providers. User modeling. | — None. |
| **T2 Context** | ███ 5-layer pipeline. Cache-aware. Deferred tools. | █ Single compressor + preflight. Auxiliary summaries. | — Raw window. |
| **T3 Safety** (★ permanent) | ███ 7 modes. Deny-first. ML classifier. Sandbox. Trust resets per session. | █ Pattern-match + optional LLM. Injection scan. Loop detection. No sandbox. | — Not solved. Operator's problem. |
| **T4 Cost/Quality** | █ Single provider. No model-tier routing. | ███ 15+ providers. Fallback chains. Credential pool. OpenRouter pareto routing. | ██ Multi-provider. Minimal cost tracking. |
| **T5 Parallelism** | ██ 3 patterns (background, swarm, coordinator). Mailbox. | █ 2 patterns (delegate, kanban). Shared budget. | — Sequential only. |

### Reading the Comparison

- **Claude Code overbuilds T1, T2, T5.** Memory hierarchy, compaction pipeline, multi-agent patterns are all best-in-class.
- **Claude Code underbuilds T4.** Single provider, no in-loop model-tier routing. The unsolved tension.
- **Hermes wins T4 decisively.** 15+ providers, fallback chains, credential pool — built for the "any model, anywhere" thesis.
- **Hermes underinvests in T3.** No sandbox, no permission modes. Pattern-matching with optional LLM checks. T3 is Hermes's biggest gap.
- **OpenClaw doesn't compete on most tensions.** That's the point — it's a thin harness. The bet is: the model is good enough; you don't need this much scaffolding.

## 2.3 Unique Advantages

| System | What only this system has |
|---|---|
| **Claude Code** | Cache economics (cache_edits, 1h TTL, sorted schemas). Recovery mechanisms (3-retry escalation, reactive compaction, streaming fallback). |
| **Hermes Agent** | Self-improvement loop (skills from experience, curated by background agent). Gateway adapters (6 messaging platforms: Telegram, Discord, Slack, WhatsApp, Signal, Matrix). |
| **OpenClaw** | Rust performance. 20K lines. Embeddable. Minimal footprint. |

## 2.4 The Time-Dependent Bet

Each system is making a bet about how models evolve:

| System | Wins if… | Loses if… |
|---|---|---|
| **Claude Code** | Models stay limited and risky → deep harness keeps paying off. T3 *never depreciates* regardless. | Models improve dramatically → carries dead weight in T1, T2, T5. (T3 still permanent.) |
| **Hermes Agent** | Models stay locked-in to providers and can't self-improve → wide harness keeps paying off. | Providers converge and skills become a model-level feature → wide is overhead. (T3 remains the gap.) |
| **OpenClaw** | Models improve fast and most current infra becomes obsolete → thin wins. | Models stay limited → missing critical infrastructure to operate at all. (T3 is the gamble it didn't take.) |

> **None of these bets is irrational.** They're three different forecasts of how AI evolves. Three different theories of the model.

---

## 2.5 Hermes "Spreads Sideways" — What This Actually Means

Claude Code goes **vertical**: 512K lines stacked on top of *one* model from *one* provider. Caching, compaction, memory, permissions — all deeper integrations with Anthropic's API.

Hermes goes **horizontal**: same effort, but spent on **integration surface** instead of core depth.

| Where Hermes spends code | What it buys |
|---|---|
| 15+ provider adapters (Anthropic, OpenAI, Bedrock, Codex, OpenRouter, NIM, …) | Run on any model |
| 6 gateway adapters (Telegram, Slack, Discord, WhatsApp, Signal, Matrix) | Reach the agent from anywhere |
| 8 memory plugin providers (mem0, Honcho, supermemory, …) | Plug into any memory backend |
| Self-improvement loop (skills + curator) | Get better over time |

**One sentence:** Claude Code makes the agent *deeper*; Hermes makes the agent *reach more places.*

## 2.6 OpenClaw "Thin Harness" — What This Actually Means

20K Rust lines. They **stripped out**: the permission system, multi-tier memory, compaction pipeline, multi-agent patterns, gateway adapters.

What remains: a basic ReAct loop, file/bash tools, prompt formatting, multi-provider auth. **That's it.**

**One sentence:** If Claude Code is a Tesla, OpenClaw is a go-kart — same wheels and engine, no airbags, no anti-lock brakes, no infotainment.

---

## 2.7 Why Three Different Schools of Thought, If Benchmarks Exist?

Benchmarks measure model capability on **isolated tasks**. Production agents fail on things benchmarks **don't** measure:

| What benchmarks measure | What production agents actually fail on |
|---|---|
| Accuracy on a single task | Long-horizon coherence over 100s of turns |
| Code generation quality | Catastrophic actions (`rm -rf`, key exfiltration) |
| Reasoning depth | Cost at scale across millions of sessions |
| Tool use success | Regulatory and audit requirements |
| Single-shot performance | Recovery from cascading failures |

The gap between benchmark capability and production need is filled by **operator judgment**. Operator judgment is shaped by:

| Team | Customer | Threat model they fear | → Harness shape |
|---|---|---|---|
| Anthropic (CC) | Paying enterprise + SOC2 | One incident → loss of trust + liability | **Deep**: compensate + constrain heavily |
| Nous (Hermes) | OSS research community | Provider lock-in + can't experiment | **Wide**: portability + self-improvement |
| OpenClaw orig. | Researchers / embedders | Over-engineered bloat slows adoption | **Thin**: minimal, embeddable |

> **The disagreement isn't about *what the model can do*** — they all agree there. **It's about *what they're afraid of when it fails.*** Different fears → different harnesses.

---

# Part 3 · Claude Code Technical Architecture

## 3.1 The 9-Layer Map (L0 – L8)

These aren't labels in Claude Code's source — they're a mental model from the VILA-Lab "Dive into Claude Code" analysis. We grouped the codebase into 9 architectural layers:

| Layer | Name | What lives here |
|---|---|---|
| **L0** | Session Architecture | UUID, transcript (.jsonl), parent/child sessions, cost-tracker |
| **L1** | Model Selection & Routing | `getRuntimeMainLoopModel`, fast-mode, subagent model resolution |
| **L2** | Memory Hierarchy | 4-tier memory (disk → working) |
| **L3** | Context Assembly & Token Budgeting | System prompt, CLAUDE.md, tool schemas, summaries |
| **L4** | API Economics | Caching, retry, fallback, cost tracking |
| **L5** | Compaction | 5-stage pipeline (budget → snip → microcompact → collapse → auto) |
| **L6** | The Agent Loop | `QueryEngine.submitMessage()`, `query.ts` main loop |
| **L7** | Tool Execution Pipeline | Permission gate (7 modes), validation, dispatch |
| **L8** | Multi-Agent & Subagent Orchestration | AgentTool, Swarm, Coordinator |

## 3.2 The Architecture at a Glance

```
ENTRY (CLI/IDE/Web)
  ↓
[L0] SESSION  (UUID · transcript · cost-tracker)
  ↓
[L6] QUERY ENGINE  (submitMessage · processInput · shouldQuery?)
  ↓
  ├─ if slash command → return locally → EXIT
  │
  └─ if real query → THE PIPELINE ↓

  STEP 1  CONTEXT ASSEMBLY  [L3]  ← memory feeds in here from L2
  STEP 2  COMPACTION         [L4/L5]
  STEP 3  MODEL SELECT       [L1]
  STEP 4  API CALL           [L4]
  STEP 5  TOOL DISPATCH      [L7]
       │
       ├─ if stop_reason == 'tool_use' → loop back to STEP 1
       └─ else → EXIT (turn complete, wait for next user input)

BACKGROUND (forked, L4-adjacent):
  extractMemories · SessionMemory · PromptSuggestion · AgentSummary

MULTI-AGENT [L8]:
  AgentTool (async) · SwarmTool (in-process) · Coordinator mode

EXTERNAL:
  Anthropic Messages API · MCP Servers · GrowthBook/Statsig (feature gates)
```

## 3.3 Layer Walkthroughs

### L0 — Session Architecture

Every Claude Code invocation creates a session with a UUID. The session is the **cost-accounting boundary** — all token spending is tracked per session.

**Lifecycle:**
```
CREATE     randomUUID() at startup
  ↓
CACHE      metadata buffered in-memory (no disk write yet)
  ↓
MATERIALIZE  first real message writes .jsonl to disk
  ↓
APPEND     each turn adds messages via insertMessageChain()
            with parentUuid linking (the messages form a linked list)
  ↓
RESUME     /resume → switchSession() atomically swaps sessionId
            + sessionProjectDir
  ↓
FLUSH      pending entries written on idle or process exit
```

**Critical design choice — permissions never restored on resume.** When `/resume` restores a session, cost state and conversation carry over, but **all permission grants are reset**. Trust is re-established every session.

### L6 — The Agent Loop (Heart of the System)

The Agent Loop is the `while`-loop that drives everything. It receives input, calls the API, processes tools, decides whether to continue.

**9-step turn pipeline:**

```
1. Settings resolution        — load config, env vars, overrides
2. State initialization        — set up AppState for this turn
3. Context assembly            — system prompt + memory + tools + history
4. Pre-model shapers (5)       — compaction pipeline runs cheapest-first
5. Model call                  — anthropic.beta.messages.create()
6. Tool dispatch               — execute tool_use blocks from response
7. Permission gate             — 7-mode cascade (deny rules → hooks → modes)
8. Tool execution              — actually run the tool
9. Stop condition check        — loop back, exit, or error?
```

**Exit conditions:**
1. No `tool_use` blocks in response → done
2. Max turns reached → done with error
3. Token budget exhausted → done or continue (configurable +500K extension)
4. Stop hooks blocking → retry with hook message or exit
5. `prompt-too-long` error → recover via compaction, then retry

### L7 — Tool Execution Pipeline

When the model returns `tool_use` blocks, this layer takes over.

**The 4-step permission cascade:**
```
1a. Deny rules           — blanket blocks, no content match needed
1b. Pre-tool hooks       — custom classifiers, async
2.  Tool's checkPermissions()  — tool-specific logic
3.  Allow rules          — always-allow from settings/session
4.  Default              — ask user OR auto-decide by mode
```

**The 7 permission modes:**

| Mode | Behavior | Trust Level |
|---|---|---|
| `plan` | User approves all plans before execution | Lowest |
| `default` | Standard interactive approval | Low |
| `acceptEdits` | File edits + filesystem shell auto-approved | Medium |
| `auto` | ML classifier evaluates tool safety | High |
| `dontAsk` | No prompting, deny rules still enforced | Higher |
| `bypassPermissions` | Skips most prompts, safety-critical checks remain | Highest |
| `bubble` | Internal: subagent escalation to parent | Special |

**Yolo classifier (auto mode):** A separate LLM call evaluates safety independently. Two-stage: fast-filter + chain-of-thought. Races pre-computed classification against a timeout.

---

## 3.4 The 4-Tier Memory System (L2)

### Python analogy band (mental model for the audience)

| Tier | Python analog | Lifetime semantics |
|---|---|---|
| **Tier 1** | `shelve` | Persistent dictionary keyed by relevance; survives forever |
| **Tier 2** | `tempfile` | Scratch storage scoped to one process (session) |
| **Tier 3** | Module globals | Accessible every turn within the session; reset on compaction |
| **Tier 4** | Locals | Live in current scope (turn); gone when scope exits |

### Cross-tier promotion (what gets kept, what gets lost)

| Source | Promotion path |
|---|---|
| Tier 4 (live conversation) | → Tier 1 via `extractMemories` agent (durable user/project facts) |
| Tier 4 (live conversation) | → Tier 2 via `SessionMemory` agent (session notes) |
| Tier 4 (live conversation) | → Tier 3 via compaction (summarized history) |
| Tier 3 (in-context summaries) | → No direct promotion path. Lost in next compaction unless re-stated in conversation |

**The critical truth:** anything that exists *only* in Tier 4 and doesn't make it into Tier 3 (summary), Tier 2 (session notes), or Tier 1 (durable memory) is **gone after compaction**. The 4-tier system is fundamentally a decision system about what's worth promoting.



Memory in Claude Code is **not a single store**. It's a hierarchy where each tier serves a different purpose.

```
┌─────────────────────────────────────────────────────────────────────┐
│ TIER 1 — PERSISTENT (Disk)                                          │
│   ~/.claude/projects/<project-hash>/memory/                         │
│   ├── MEMORY.md            ← index (max 200 lines, max 25KB)         │
│   └── *.md                 ← individual memory files                 │
│   Survives: forever, across sessions                                 │
│   Written by: extractMemories (background agent) OR model's Write tool│
├─────────────────────────────────────────────────────────────────────┤
│ TIER 2 — SESSION (Evolving Notes)                                   │
│   Session-scoped markdown file                                       │
│   Updated periodically (threshold-based: N tool calls or M tokens)  │
│   Triggered by: hasMetUpdateThreshold()                              │
│   Written by: SessionMemory (background agent)                        │
│   Survives: within session only                                       │
├─────────────────────────────────────────────────────────────────────┤
│ TIER 3 — IN-CONTEXT (Semi-Persistent)                               │
│   CLAUDE.md files (loaded into system prompt every turn)             │
│   Compaction summaries (replace old messages after compression)       │
│   Team memory (shared across swarm teammates)                        │
│   Loaded at: context assembly time                                    │
│   Survives: until next compaction                                     │
├─────────────────────────────────────────────────────────────────────┤
│ TIER 4 — WORKING (Live Conversation)                                │
│   Raw messages in context window                                      │
│   Most expensive tier (full token cost per message)                   │
│   What compaction operates on                                         │
│   Survives: until compacted or session ends                           │
└─────────────────────────────────────────────────────────────────────┘
```

### Memory vs Transcript — Two Different Things

A common confusion: people think "memory" and "transcript" are the same. They're not.

| | Transcript | Memory |
|---|---|---|
| **Written by** | Session Layer (automatic) | extractMemories (background agent) |
| **What it stores** | Every message verbatim — the raw log | Distilled, durable facts about user/project |
| **Path** | `~/.claude/projects/<hash>/<sessionId>.jsonl` | `~/.claude/projects/<hash>/memory/*.md` |
| **Purpose** | Resume sessions, audit, replay | Remember things across sessions |

**Transcript = raw log.** Every word appended automatically.
**Memory = distilled knowledge.** A background agent reads the conversation, decides what's worth remembering forever, writes it as Markdown.

---

## 3.4.5 Skills vs Memory — Two Parallel Persistence Systems

Claude Code has **two distinct persistence systems**, often confused with each other:

| | Memory (the 4-tier system) | Skills |
|---|---|---|
| **Purpose** | Stores *facts* about user / project / preferences | Stores *capabilities* the model can invoke |
| **Loaded** | Automatically — every session start | On demand — only when the model invokes the Skill tool |
| **Token cost** | Pays input tokens every session | Zero cost if unused; pays only when invoked |
| **Written by** | Background agents (extractMemories, SessionMemory) OR user via Write tool | User OR model via Write tool |
| **Trigger to use** | Just exists in context | Model decides to call the Skill |

The slogan: **memory injects automatically; skills inject on demand.** Both can be persistent on disk, but their access pattern is different.

---

### Memory · Where Things Are Stored

Memory is **per-project**, scoped by the working directory's project hash:

```
~/.claude/projects/<project-hash>/
├── <sessionUUID>.jsonl                ← T4 transcripts (one per session)
├── <sessionUUID>/                     ← session companion folder
│   ├── subagents/...
│   └── tool-results/...
└── memory/                            ← Tier 1 (persistent)
    ├── MEMORY.md                      ← index file (≤200 lines)
    ├── user_*.md                      ← user-related facts
    └── project_*.md                   ← project-related facts
```

Notice:
- **There is no cross-project memory.** Memory is always scoped to a project (the project-hash directory). If you switch projects, you get a different memory store.
- Tier 2 (`session_notes.md`) also lives in the same `<project-hash>/` folder but is session-scoped — abandoned at session end.
- Tier 3 lives in the context window (CLAUDE.md + compaction summaries) — not on disk in a memory-specific way.
- Tier 4 is the live conversation (the JSONL transcript on disk, but that's a *log*, not a memory).

### Memory Files · How They Get Written

Two paths:

**1. Automatic (background agents)**
```
extractMemories agent  →  every turn   →  decides if anything is durable
                                          → if yes, calls Write tool internally
                                          → file appears in ~/.claude/projects/<hash>/memory/
                                          → MEMORY.md index updated
```

**2. Manual (you or the model, via Write tool)**
```
Write tool with path:
  ~/.claude/projects/<project-hash>/memory/<filename>.md
  ~/.claude/projects/<project-hash>/memory/MEMORY.md   (to update the index)
```

You don't need a special API — `Write` to the right path creates a memory file. The system loads it at the next session start.

---

### Skills · Where Things Are Stored

Skills have **two scopes** (user-level vs project-level), determined entirely by where you save the file:

```
USER-LEVEL (cross-project — available on every project)
~/.claude/skills/<skill-name>/
└── SKILL.md                            ← skill definition + instructions

PROJECT-LEVEL (only available when working on this project)
<project-root>/.claude/skills/<skill-name>/
└── SKILL.md
```

Same convention as CLAUDE.md:

| File | User-level | Project-level |
|---|---|---|
| CLAUDE.md | `~/.claude/CLAUDE.md` | `<project>/CLAUDE.md` |
| Skills | `~/.claude/skills/` | `<project>/.claude/skills/` |

### Skills · How They Get Written

**You always use the Write tool. The path determines scope.** That's the entire mechanism — no flag, no setting, no command. Just the file path.

```
Cross-project skill (user-level):
  Write(file_path="/Users/<you>/.claude/skills/git-helpers/SKILL.md", ...)

Project-only skill:
  Write(file_path="<project>/.claude/skills/deploy-helper/SKILL.md", ...)
  (or relative path from working dir: ".claude/skills/deploy-helper/SKILL.md")
```

When the model invokes a skill at runtime, Claude Code:
1. Scans `~/.claude/skills/` (user-level) — adds all of them to the available skill pool
2. Scans `<current-project>/.claude/skills/` (project-level) — adds these too
3. Presents the union as the skill list to the model
4. Model picks one by name when relevant; the SKILL.md content gets injected into the current context

If a skill exists at *both* levels with the same name, the project-level one typically takes precedence (project-specific overrides user defaults).

### Skill File Structure

A skill is a directory (named after the skill) containing at minimum a `SKILL.md`:

```
~/.claude/skills/git-helpers/
├── SKILL.md          ← required: name, description, instructions
├── examples.md       ← optional: examples the model can reference
└── helpers/          ← optional: scripts or supporting files
    └── format.sh
```

The `SKILL.md` typically has frontmatter:

```markdown
---
name: git-helpers
description: Helpers for common git operations (rebase, force-push safety)
---

# Instructions

When the user asks for git operations, use these patterns:
...
```

Description is critical — it's what the model reads to decide whether to invoke the skill. **Bad description = model never picks the skill.**

---

### Side-by-Side Comparison

| Aspect | Memory | Skill |
|---|---|---|
| **Where stored** | `~/.claude/projects/<hash>/memory/` | `~/.claude/skills/` or `<project>/.claude/skills/` |
| **Scope** | Per-project (always) | User-level OR project-level (your choice via path) |
| **Auto-loaded?** | Yes (Tier 1 + Tier 2 at session start) | No — model invokes on demand |
| **Token cost** | Paid every session, whether useful or not | Paid only when invoked |
| **Written by** | extractMemories agent (auto) OR Write tool (manual) | Write tool (manual; user or model) |
| **Lifetime** | Forever (Tier 1) or session-bound (Tier 2) | Until you delete the file |
| **Updated by** | Background agent OR Edit/Write tool | Edit/Write tool |
| **Format** | Markdown with YAML frontmatter (name, description, type) | Markdown directory with `SKILL.md` + optional files |
| **Discovery** | Listed in `MEMORY.md` index | Listed in available skill pool at runtime |
| **Override behavior** | Newer memory file replaces older one with same name | Project-level skill overrides user-level skill with same name |

---

### Direct Answer to "Can I Write a Skill Cross-Project via the Write Tool?"

**Yes — that's the standard mechanism.** Two ways to make a skill cross-project:

```python
# Option 1: Write directly to user-level path
Write(file_path="/Users/<you>/.claude/skills/my-skill/SKILL.md",
      content="...")

# Option 2: Ask Claude Code to create the skill (it'll use Write internally)
# Just tell it: "create a user-level skill for X"
```

The destination path is the only thing that determines whether the skill is global or project-specific. No flag, no command, no config. **Path = scope.**

To make an existing project-level skill global, just move the file:

```bash
mv <project>/.claude/skills/my-skill ~/.claude/skills/my-skill
```

Or copy if you want both:

```bash
cp -r <project>/.claude/skills/my-skill ~/.claude/skills/my-skill
```

---

### Why This Matters for Cost / Routing

| System | Cost Implication |
|---|---|
| **Memory (Tier 1)** | Pays input tokens every session whether you use the memory or not. Large MEMORY.md → growing baseline cost. Cap: 200 lines / 25KB to bound this. |
| **Memory (Tier 2)** | Same — re-loaded every session start. |
| **Skills (any scope)** | **Zero cost if not invoked.** Only pays when summoned. Strictly better than memory for content you don't always need. |

So the design rule:
- Things the model should *always* know → memory or CLAUDE.md
- Things the model should know *when relevant* → skills

If you have content that's only sometimes useful, putting it in skills (not memory) saves tokens every session.

---

## 3.5 The 5-Stage Compaction Pipeline (L4/L5)

When context grows toward the window limit, Claude Code runs a **lazy degradation strategy** — try cheapest stages first, escalate only if needed.

| Stage | Strategy | Trigger | Cost |
|---|---|---|---|
| 1. **Budget Reduction** | Per-message size caps | Always active | Free |
| 2. **Snip** | Trim older history | Feature-gated (`HISTORY_SNIP`) | Free |
| 3. **Microcompact** | Cache-aware tool result stripping | Always (time-based) | Free |
| 4. **Context Collapse** | Read-time virtual projection (non-destructive) | Feature-gated (`CONTEXT_COLLAPSE`) | Free |
| 5. **Auto-Compact** | Full model-generated summary (last resort) | When all else fails | **20K output tokens** |

**Key:** If an earlier stage frees enough tokens, later stages are skipped entirely.

### Auto-Compact Details

The most expensive stage — a full API call that:
- Sends the entire conversation as input
- Generates up to **20K output tokens** (the summary)
- Uses the **main loop model** (Opus if you're on Opus → $2-$4 per event)
- Re-injects up to 5 recently-read files (50K budget) + active skill content (25K budget) + plan state after compaction

**Threshold (default 200K window):** triggers at ~187K tokens (`contextWindow - 13K`). Hard block at ~197K (`contextWindow - 3K`).

**Circuit breaker:** `MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES = 3` stops the retry loop.

---

## 3.6 Background Workers — The Hidden Cost Center

After (or during) every turn, **four forked agents** run silently in the background:

| Worker | Trigger | Cadence | What it does | Output |
|---|---|---|---|---|
| **extractMemories** | Every turn | Per-turn | Distills durable facts → writes Markdown | Disk (Tier 1) |
| **SessionMemory** | Threshold | Every N tool calls or M tokens | Updates session notes | Disk (Tier 2) |
| **PromptSuggestion** | Every turn | Per-turn | Speculatively pre-generates next-turn suggestions | UI chips |
| **AgentSummary** | Coordinator mode active | Every 30 seconds | Status summaries for the coordinator | Coordinator's context |

### Critical: Do Workers Share Parent's Context?

**They FORK** — they have their own context windows, but they CLONE the parent's content.

```
Parent context (Tier 4):  [80K tokens of conversation]
                                 │
                ┌────────────────┼────────────────┬────────────┐
                ▼                ▼                ▼            ▼
        extractMemories   SessionMemory   PromptSuggest   AgentSummary
        own context       own context     own context     own context
        80K cloned        80K cloned      80K cloned      80K cloned
        + own task        + own task      + own task      + own task
        prompt            prompt          prompt          prompt
```

**What this means for cost:**
- Parent's context isn't modified — workers don't *consume* it
- BUT each worker sends ITS OWN API call containing all 80K tokens
- 4 workers × 80K = **320K extra input tokens per turn** going to the API
- They share the parent's **prompt cache** (same prefix → 10× discount), but cache only covers the system prompt + tools, not the full conversation tail

**Net:** workers don't shrink the main agent's context — they multiply your bill. Routing these to Haiku alone saves ~20-25% of session cost.

### Why Is AgentSummary Separate From extractMemories?

Different jobs, different audiences, different cadence:

| | extractMemories | AgentSummary |
|---|---|---|
| **Audience** | Future sessions (you, tomorrow) | Coordinator agent (right now) |
| **Cadence** | Every turn | Every 30 seconds |
| **Output** | Durable Markdown files on disk | Ephemeral status text injected into coordinator's context |
| **Triggers when** | Always | Only when coordinator/swarm subagents are running |
| **Content** | "User prefers Python. Lives in PST. Working on routing research." | "Worker A: still grepping. Worker B: found 3 files. Worker C: blocked on permission." |

> **One sentence:** extractMemories is for **persistence across time**. AgentSummary is for **coordination across agents in the same moment.**

### Why Does PromptSuggestion Exist?

It **speculatively pre-generates next-turn suggestions** for the UI. After the model finishes its response, this agent guesses what the user might ask next and pre-computes 1-3 suggested follow-up prompts.

- Reduces user typing
- Makes the UI feel faster (suggestion is ready when response finishes)
- Helps onboarding users discover features

**The cost:** a full extra API call per turn. If feature-gated off, you save ~$0.05-$0.10 per turn on Opus.

---

## 3.7 Multi-Agent Patterns (L8)

Claude Code supports **three orchestration patterns** for spawning child agents:

### Pattern 1 — Background Agents (AgentTool)

- Spawned via `Agent` tool with description + prompt
- Runs **async** in the main process, separate context
- **15 tools** allowed (file ops, web, bash — no Agent spawning, no task management)
- Parent continues executing after spawn
- Fork variant: child inherits parent's full conversation + system prompt → prompt cache hits

### Pattern 2 — In-Process Teammates (Swarm)

- Spawned via `spawnInProcessTeammate()`
- Same Node.js process, shared file state, shared MCP servers
- **Full tool set** + task management + inter-teammate messaging
- Deterministic ID: `"name@teamName"`
- Communication via mailbox system

### Pattern 3 — Coordinator Mode

- Gated by env var `CLAUDE_CODE_COORDINATOR_MODE` (mutually exclusive with fork mode)
- **Coordinator gets only 4 tools**: Agent, TaskStop, SendMessage, SyntheticOutput
- **Workers get**: Bash, Read, Edit, MCP tools only
- Workers notify coordinator via `<task-notification>` XML when done
- Coordinator synthesizes results from multiple workers

| Agent Type | Tools Allowed | Key Exclusions |
|---|---|---|
| Background (async) | 15 tools | No Agent (no recursion), no TaskStop, no AskUser |
| In-Process Teammate | All async + Task tools + SendMessage + Team tools | None (full capability) |
| Coordinator | 4 tools only | No file ops, no bash |
| Coordinator Workers | Bash, Read, Edit, MCP | No Agent, no task mgmt |

### SkillTool vs AgentTool — The Cost Divide

| Mechanism | Context Cost | How It Works |
|---|---|---|
| **SkillTool** | Low (~same window) | Injects instructions into current context. No new API call for the injection itself. |
| **AgentTool** | High (~7× tokens) | Spawns new isolated context window. Full system prompt + tools re-sent. Summary-only return. |

A router should prefer SkillTool when the task fits the current context, and AgentTool only when isolation is needed (long-running research, file-heavy exploration, parallel work).

---

## 3.8 The "Agent" Is the Loop

The **orange loop-back arrow** on the architecture diagram is the most important piece.

```
1. Model receives context, decides "I need to call tool X with args Y"
2. Returns response with stop_reason: 'tool_use' and tool_use blocks
3. Tool Dispatch executes those tools, gets results
4. ⟲ Loop back to Context Assembly, append tool results, call model again
5. Model now sees tool results, decides next action
6. Repeat until stop_reason: 'end_turn' (no more tool calls)
7. Exit (turn complete)
```

**That's literally what "agentic" means.** The recursion is the agent. One model call with tool use is just a function call. The loop turning function calls into *plans* is the agent.

---

## 3.9 The Slash-Command Bypass

When the user types `/help`, `/clear`, `/resume`, `/model`, etc., the **QueryEngine handles it locally** — bypassing the entire pipeline.

| Path | What happens |
|---|---|
| Real query (`shouldQuery=true`) | Walks Steps 1-5, loops as needed, then EXIT |
| Slash command (`shouldQuery=false`) | QueryEngine handles locally, returns to EXIT |

Both paths converge at EXIT.

**Important:** EXIT means **"this turn is complete," not "session over."**

- The CLI is now idle, waiting for your next input
- The session is still alive
- Type another message, and `submitMessage()` runs again from the top
- Session terminates only when you `Ctrl+C` or call `/exit`

**Why slash commands bypass:** they don't need the model. `/help` is a printout. `/clear` mutates local state. `/model` changes a string in memory. None justify a $0.05 Opus call.

---

## 3.10 External Dependencies

The "External" block on the architecture diagram has three items. They matter for understanding what Claude Code does and doesn't control.

### Anthropic Messages API

The actual HTTP endpoint at `api.anthropic.com/v1/messages`. Every model call is an HTTPS POST to this endpoint. The "External" label reminds us: **the model isn't local.** Claude Code is a client orchestrating remote calls.

### MCP Servers

**Model Context Protocol** servers — Anthropic's open standard for plugging external tools into the model. MCP servers run as separate processes (or HTTP endpoints) and expose tools via a standardized protocol. Examples:
- A GitHub MCP server exposes "create_issue", "list_pull_requests"
- A Postgres MCP server exposes "query_database"
- A Slack MCP server exposes "send_message"

Claude Code connects to MCP servers at startup, fetches their tool schemas, and merges them into the available tool pool.

### GrowthBook / Statsig

**Feature flag services.** Both are SaaS tools (statsig.com, growthbook.io) that let a company toggle features per user, per region, or for A/B tests **without redeploying code**. Anthropic uses them to:

- Enable beta features for some users (`tengu_prompt_cache_1h_config`)
- Roll out features gradually (10% → 50% → 100%)
- A/B test new behaviors

When Claude Code starts up, it pings these services to ask: "for this user, which features are on?" Your install might have different behavior than someone else's.

#### Aside: What Does the `tengu_` Prefix Mean?

You'll see flags like `tengu_prompt_cache_1h_config`, `tengu_session_memory`, `tengu_penguins_off`. **"Tengu" is Anthropic's internal codename for Claude Code** — named after the Japanese yokai (天狗) from folklore.

All Claude Code feature flags are namespaced with `tengu_` to avoid colliding with other Anthropic projects sharing the same flag-service tenant. Same pattern as `chrome_*` at Google or project codenames in any large org. When you see `tengu_` in a flag name, it just means "this is a Claude Code feature flag."

Examples in the source:
| Flag | What it gates |
|---|---|
| `tengu_prompt_cache_1h_config` | 1-hour cache TTL (vs 5-minute default) |
| `tengu_session_memory` | SessionMemory background worker |
| `tengu_penguins_off` | Fast mode availability (Opus 4.6 Fast) |

---

## 3.11 File Structure of a Claude Code Project Folder

Every Claude Code project gets its own folder under `~/.claude/projects/`. The folder name is derived from the working directory by replacing `/` with `-`.

**Example:**
```
Working directory:  ~/path/to/your-project/
Project folder:     ~/.claude/projects/-path-to-your-project/
```

This is the project-hash convention. Every project Claude Code touches gets exactly one folder like this.

### Standard Layout

```
~/.claude/projects/<project-hash>/
│
├── <sessionUUID-1>.jsonl           ← Session 1 transcript (append-only)
├── <sessionUUID-1>/                ← Companion folder for Session 1
│   ├── subagents/
│   │   ├── agent-<agentId-A>.jsonl   ← Each spawned subagent's transcript
│   │   ├── agent-<agentId-B>.jsonl
│   │   └── ...
│   └── tool-results/
│       ├── <hash>.json              ← Offloaded large tool results (>128KB)
│       └── ...
│
├── <sessionUUID-2>.jsonl           ← Session 2 transcript
├── <sessionUUID-2>/                ← Session 2 companion folder
│   └── ...
│
└── memory/                          ← Tier 1 persistent memory (4-tier system)
    ├── MEMORY.md                    ← Index: links to all memory files (≤200 lines, ≤25KB)
    ├── user_profile.md              ← Example memory file (user type)
    ├── project_goals.md             ← Example memory file (project type)
    ├── feedback_*.md                ← Example memory files (feedback type)
    └── reference_*.md               ← Example memory files (reference type)
```

### What Each File or Folder Does

| File / Folder | Purpose | Written By | Lifecycle |
|---|---|---|---|
| `<sessionUUID>.jsonl` | The session's transcript — every message verbatim, one JSON per line. Each message has `uuid`, `parentUuid` (linked-list), `type`, `content`, `timestamp`, `model`, and `usage` fields. This is what `/resume` reads. | **Session Layer (L0)** — automatic, every turn | Permanent |
| `<sessionUUID>/subagents/agent-<id>.jsonl` | Each subagent spawned during the session writes its own transcript here. The subagent's full conversation — its system prompt, tool calls, results, and final output. | **AgentTool / Swarm spawn machinery (L8)** | Permanent |
| `<sessionUUID>/tool-results/` | Large tool results (>128KB by default) are offloaded to disk so they don't bloat the context window. The model receives only a small preview + a reference; the full payload sits here. Critical cost optimization. | **Tool execution pipeline (L7)** — when a result exceeds threshold | Permanent |
| `memory/MEMORY.md` | The index of persistent memory. Capped at ~200 lines / 25KB. Loaded into the system prompt at session start. Lists every memory file with a one-line description. | **Model (Write tool)** or **extractMemories background agent** | Permanent |
| `memory/*.md` | Individual memory files. Each has YAML frontmatter (`name`, `description`, `metadata.type`) plus body. Type is one of: `user`, `feedback`, `project`, `reference`. | **extractMemories background agent (per turn)** or **Model (Write tool)** | Permanent |
| `session-transcript.jsonl` *(non-standard)* | Not part of the standard Claude Code layout. Usually indicates an imported, exported, or manually-renamed transcript. Standard Claude Code creates UUID-named jsonl files. | **Manual / external tooling** | Manual |

### Why the .jsonl and Its Sibling Folder Are Split

The `.jsonl` is the **light, sequential record** — small messages, fast to append, easy to replay for `/resume`.

The matching folder holds **heavy or branched content**:
- Subagent transcripts can't live in the parent `.jsonl` (they're separate conversations with their own context windows)
- Tool results over 128KB would balloon the parent `.jsonl` and re-bloat your context every turn

Splitting them keeps the main transcript small, fast to load, and resumable.

### Anatomy of a Message in the .jsonl

Each line in a session transcript is a JSON object. Typical fields:

```json
{
  "uuid": "a1b2c3...",                 // this message's unique ID
  "parentUuid": "x9y8z7...",            // links to previous message → forms a chain
  "sessionId": "fd9d6977-...",          // owning session
  "type": "assistant",                  // user | assistant | tool_use | tool_result
  "timestamp": "2026-05-12T10:23:45Z",
  "model": "claude-opus-4-7",           // which model handled this (null for user msgs)
  "content": [
    {
      "type": "text",
      "text": "..."
    },
    {
      "type": "tool_use",
      "id": "toolu_...",
      "name": "Bash",
      "input": {"command": "ls"}
    }
  ],
  "usage": {                            // token accounting (assistant msgs only)
    "input_tokens": 1234,
    "output_tokens": 567,
    "cache_read_input_tokens": 8901,
    "cache_creation_input_tokens": 0
  }
}
```

The `parentUuid` chain is critical — it means messages form a **linked list**, not a flat array. This is how branching (e.g., `/resume` from an earlier point) works.

### Lifecycle Events That Generate Files

| Event | What gets created |
|---|---|
| **First message of a new session** | `<sessionUUID>.jsonl` materializes on disk |
| **Every subsequent message** | One new line appended to that `.jsonl` |
| **Subagent spawn** | `<sessionUUID>/subagents/agent-<id>.jsonl` created (if not already) |
| **Tool returns >128KB** | One file written to `<sessionUUID>/tool-results/` |
| **extractMemories writes** | A new or updated file in `memory/` + `MEMORY.md` index update |
| **`/resume`** | No new file — atomic switch of `sessionId` and project dir |

### Practical Implications

- **The `.jsonl` is human-readable.** You can `cat`, `grep`, or `jq` over it to inspect what the agent did. Useful for debugging or producing reports.
- **The `memory/` folder is your knowledge moat.** It's the only part of Claude Code's state that genuinely persists *between* sessions. Worth understanding what gets written there — and what doesn't.
- **The `tool-results/` folder can grow huge.** A long session with big file reads can leave hundreds of MB here. Safe to delete after a session is closed (won't break `/resume` of a closed session).
- **Permissions reset on `/resume`** even though the transcript persists. Trust state is *not* in any of these files — it's session-scoped runtime state only.

---

# Part 4 · How Models Are Trained for Agentic Tasks

## 4.1 Standard vs Agentic Training Pipeline

| Standard LLM training | What gets added for agentic models |
|---|---|
| Pre-training (text prediction) | Same |
| SFT on instructions | **+ SFT on multi-step tool-use trajectories** |
| RLHF on single-turn helpfulness | **+ RL on whole-trajectory outcomes** (did the task succeed end-to-end?) |
| Eval on MMLU, GSM8K, HumanEval | **+ Eval on SWE-bench, WebArena, AgentBench, GAIA, OSWorld, Terminal-Bench** |

### The Three Real Differences

1. **Training data shape changes.** Instead of `(prompt → answer)` pairs, training data becomes `(goal → [thought, tool_call, result, thought, tool_call, result, …] → outcome)` trajectories.

2. **Reward signal changes.** Standard RLHF rewards per-turn helpfulness. Agentic RL rewards **sparse, trajectory-level success** (did the bug get fixed? did the test pass?). Much harder — credit assignment over 50 turns.

3. **Format adherence becomes a hard constraint.** The model must emit syntactically valid tool calls every time. Standard models can be "almost right." Agentic models can't — one malformed JSON breaks the whole loop.

> **One sentence:** Standard models are trained to be helpful in a single turn; agentic models are additionally trained to **succeed across many turns using tools.**

---

## 4.2 Where Does Trajectory Training Data Come From?

Five main sources. Most labs use a combination of all five.

| Source | What it is | Who uses it |
|---|---|---|
| **1. Synthetic rollouts from stronger models** | Have GPT-4/Claude attempt tasks, log trajectory, **filter for success**, train smaller model on those. Pure distillation. | Most labs. Cheapest, biggest volume. |
| **2. Self-play in sandboxed environments** | Let the model itself try tasks in Docker/browser/terminal, record trajectory, keep only the ones that succeed (verified by env state, tests, etc.) | Anthropic (computer use), DeepMind, Nous Research's Atropos |
| **3. Human demonstrations** | Pay annotators to do real tasks (browse the web, edit code, run commands) while every action is recorded. | OpenAI (Operator), Adept, Cognition. Expensive, highest quality. |
| **4. Repurposed benchmarks** | Take SWE-bench / WebArena / GitHub issue-fix commit pairs and turn them into `(task → trajectory → outcome)` training examples. | Almost everyone. Limited volume but verifiable. |
| **5. Replaying real production sessions** | Anonymized user logs from deployed agents. Filter for successful task completions. | Anthropic (Claude Code), OpenAI (ChatGPT agents), Cursor |

### The Bottleneck: Verification

**Text data is everywhere.** Agentic data needs:
- An environment that responds to actions
- A way to check if the task actually succeeded

That's why most pipelines look like:

```
Generate trajectory (model or human)
       ↓
Verify outcome (tests, env state, LLM judge)
       ↓
Keep only successes
       ↓
Train
```

**Why coding agents are ahead:** `pytest` is a free verifier. Web agents and computer-use agents lag because "did the task succeed" is much harder to check programmatically.

---

## 4.3 What Is SWE-bench, Actually?

**Mined from real GitHub history. Not synthesized.**

- Pulled from **real issues + real PRs** across 12 popular Python repos (django, sympy, scikit-learn, matplotlib, etc.)
- Each task = `(issue text, repo state) → (correct patch + the tests that verify it)`
- All three pieces come from **real human work**:
  - The issue was filed by a human
  - The patch was written by a maintainer
  - The tests were added by that maintainer to prove the fix
- ~2,294 instances. **SWE-bench Verified** = 500 subset hand-checked by OpenAI for quality
- **Not synthesized. Mined.** That's why it's trusted — natural distribution of real bugs.

**One sentence:** SWE-bench is a snapshot of real GitHub work, with `pytest` as the verifier.

---

## 4.4 Is Claude Opus 4.6/4.7 Trained Specifically for SWE-bench?

**No — but its post-training emphasizes the exact behaviors SWE-bench measures.**

| Phase | What happens |
|---|---|
| **Pre-training** | General-purpose. Internet-scale text. Same as any LLM. |
| **Post-training (SFT)** | Multi-turn tool-use trajectories. Code-editing patterns. Long-horizon reasoning. |
| **Post-training (RL)** | Coding RL with **verifiable rewards** (tests pass / tests fail). Agentic trajectory rewards. |
| **Specialized** | "Computer use" capability — additional training on screen → action trajectories. |

**One sentence:** General-purpose foundation, agentic post-training. Not benchmark-specific.

It does well on SWE-bench because it was trained on **the kind of problem**, not on the benchmark itself.

---

## 4.5 How Does the Model Know Which Tool to Use?

**Tool selection is learned from training data, then applied at inference using tool descriptions in the prompt.**

### Training (Offline)

The model sees millions of trajectories shaped like:

```
Available tools: [Read, Edit, Bash, Grep, …with descriptions]
Goal: "fix the failing test in auth_test.py"
→ Model emits: Bash(command="pytest auth_test.py")
→ Result: 1 test failed at line 42
→ Model emits: Read(file="auth.py", offset=35, limit=20)
...
```

The model learns the **general pattern**: *given a tool list with descriptions, match the current situation to the best-fitting tool.* It's not memorizing "Bash exists" — it's learning the meta-skill of reading a tool list and choosing.

### Inference (Online)

Every API call carries the tool list as JSON schemas:

```json
{
  "name": "Bash",
  "description": "Execute shell commands in a persistent session...",
  "parameters": { ... }
}
```

The model reads the **descriptions** and decides. **That's its only signal.**

### Why This Matters

- **Tool descriptions are the steering wheel.** A poorly described tool will be misused or ignored.
- The model has never "seen" your custom tool before. It picks it the same way you'd pick from an unfamiliar API: by reading the docstring.
- This is why Claude Code sorts tool schemas alphabetically and writes verbose descriptions — better signal → better routing.

**One sentence:** The model is trained to **read a tool menu and pick**; at inference, the menu is the system prompt.

---

## 4.6 SFT vs RL — The Real Distinction

Common misconception: SFT = single-step, RL = multi-step.

**Reality: both work over full trajectories.** The difference is the **learning signal**:

| Phase | What it teaches | Signal | How |
|---|---|---|---|
| **SFT (supervised)** | Format + which tool fits a situation | "Match this good trajectory token-by-token" | Cross-entropy on assistant tokens vs ground-truth trajectory |
| **RL (reinforcement)** | Strategy + recovery + efficiency | "Succeed at the end, I don't care how" | Sparse reward at trajectory end (PPO/GRPO/etc.) |

**Why both:**
- SFT alone → model imitates but can't recover from its own mistakes (no failure data)
- RL alone → reward too sparse, model never even learns to emit valid JSON
- **SFT bootstraps the format and basic choices. RL optimizes for actually succeeding.**

> **One sentence:** SFT teaches the model to copy good trajectories; RL teaches it to discover new ones that succeed.

---

# Part 5 · Cost & Routing Economics

## 5.1 Where Does the Money Go in a Long Claude Code Session?

**Benchmark scenario:** 200 turns, ~4 hours, Opus model.
**Estimated cost:** $80 – $150 per session (with caching). Without caching, $200 – $400+.

### The 8 Cost Drivers, Ranked

| Rank | Cost Driver | Share | Notes |
|---|---|---|---|
| 1 | Growing history re-sent every turn | 30-40% | Every API call = full context. Cache helps; tail always pays full price |
| 2 | Background agents on main model | 20-25% | Silent · every turn (4 forked workers on Opus) |
| 3 | Auto-compaction events (4-5 per session) | 10-15% | 20K output tokens each |
| 4 | Subagent cold-start overhead | 10-20% | 7× token overhead vs inline |
| 5 | Tool schemas + cache misses | 8-15% | One bust = 10× cost spike |
| 6 | Large tool results accumulating | variable | File reads stay in context until compaction |
| 7 | MCP tool schema accumulation | 5-10% | No cap · unbounded |
| 8 | Image results bypassing result budget | blowout risk | `maxResultSizeChars=Infinity` |

### The Critical Boundary

> **Items 1-5 are addressable by routing. Items 6-8 need design fixes — routing can't help them.**

This is the boundary between what a routing layer can attack and what requires a code-level intervention.

| Tier | Drivers | Strategy |
|---|---|---|
| **Addressable (1-5)** | History · Background agents · Compaction · Subagents · Cache misses | Routing layer — model selection, cache-aware constraints, phase detection |
| **Design fixes (6-8)** | Tool results · MCP accumulation · Image blowout | Code changes — eviction policies, size caps, MCP whitelisting |

Items 1-5 together represent **80-85% of total session cost**. Routing has plenty of headroom.

### Cost Driver Deep-Dives

**#1 — Growing History (30-40%):** The model is stateless. Every API call sends the entire conversation. By turn 200, you're re-sending 200 turns of context every time. Quadratic cost growth. Cache covers the stable prefix (system + tools + early history), but the conversation *tail* always pays full price.

**#2 — Background Agents (20-25%):** The four forked workers (extractMemories, SessionMemory, PromptSuggestion, AgentSummary) all run on the main model. If you're on Opus, all four run on Opus. Silent, invisible, pure overhead. **Biggest routing win: switch them all to Haiku.**

**#3 — Auto-Compaction (10-15%):** Triggers at ~187K tokens. Emits up to 20K output tokens per event. On Opus, $0.50 per event. Long sessions hit 4-5 compactions. **Routing win: Sonnet or Haiku compaction saves 60-80% per event.**

**#4 — Subagent Cold-Start (10-20% if used):** Each subagent spawn re-sends system prompt + tool schemas (~25K tokens) plus a fresh task prompt. Then runs its own loop with its own context growth. **7× token overhead** vs inline SkillTool work. **Routing win: classify subagent tasks, route exploration → Haiku/Sonnet, keep planning on Opus.**

**#5 — Tool Schemas + Cache Misses (8-15%):** Tool schemas are 15-30K tokens in the system prompt. Usually cached. But any change — model switch, new MCP server, 5-minute TTL expiring — busts the cache. One bust = 10× cost spike. **A naive router that switches models often costs *more* than it saves. Cache state must be a hard routing constraint.**

**#6 — Large Tool Results (variable):** A 200K-token file read stays in context until compaction triggers. No automatic eviction. Routing cannot fix this.

**#7 — MCP Tool Accumulation (5-10%):** No cap on tool registration. Tool-heavy MCP servers (e.g., 200+ Slack actions) quietly burn 30-50K of prompt every turn until disconnected.

**#8 — Image Results (blowout):** `maxResultSizeChars=Infinity`. Multi-megabyte image results bypass the budget. Single image can blow the context window.

### 5.1.1 Routing Problems vs Structural Problems

The clearest way to think about what a routing layer can and cannot fix.

**Definition:** *Routing is a decision-making layer — rule-based or learned — that intercepts at an architectural seam.* Three keywords:
- **Decision-making** — something has to be chosen
- **Layer** — it sits between caller and callee
- **Seam** — there's a defined hook to intercept at

If a problem lacks any of those three, it's not a routing problem.

#### Routing Problems (decision seam exists)

| Problem | Seam | Why it's routable |
|---|---|---|
| Which model runs background agents (extract, summarize, etc.) | `getAgentModel()` | Function call you can intercept; trivial classifier |
| Which model runs compaction | `compact.ts:1188` | Single decision point per event |
| Which model spawns each subagent | `AgentTool` spawn | Independent context = independent decision |
| Whether to switch models between agent loop iterations | `getRuntimeMainLoopModel()` per iteration | Decision recomputed every turn |
| When to trigger compaction (token threshold vs task boundary) | `autoCompact.ts:72` | Threshold is a policy, not a fixed law |
| How much to compress (adaptive depth) | Compaction pipeline | Depth is parameterized |
| Whether a model switch is worth the cache miss penalty | Pre-`callModel` | The constraint that gates every other decision |

#### Structural Problems (code fix needed, not routing)

| Problem | Why not routing | Real fix |
|---|---|---|
| History grows every turn | Structural — model is stateless, protocol re-sends. No decision to make. | Compaction (already implemented) |
| Tool results accumulate in context | Microcompaction already handles. No remaining decision. | Already fixed in `microCompact.ts` |
| Images bypass result budget | Pure design bug — `maxResultSizeChars=Infinity`. No seam to intercept. | Set real cap; auto-downsample images |
| MCP schema accumulation | Per-init decision, not per-turn. Routing operates per-turn. | Selective MCP connection, default-deferred loading |
| PromptSuggestion on/off | Binary toggle, not a policy. Trivial. | Feature flag |

#### The Filter Heuristic

> **If no clean architectural seam exists to intercept → not a routing problem.**

- *Image bypass* has no seam — the result format is the format.
- *History growth* has no seam — the protocol re-sends.
- *Which model for background agents* has a seam — `getAgentModel()` is a function call. **That's the difference.**

A common follow-up question: items from the cost analysis 6-8 *can* clearly be fixed (just add a size cap) — why isn't fixing them "routing"?

**Because routing decides *who computes*. Items 6-8 are about *what's in the input*.** Two different layers, two different interventions.

### 5.1.2 How Items 6-8 Actually Get Fixed (Code Changes, Not Routing)

**#6 — Large tool results accumulating**
Claude Code already offloads results >128KB to disk (`toolResultStorage`). Remaining gap: results in the 50-128KB range still accumulate.

| Fix | Where |
|---|---|
| Lower offload threshold (e.g., 128KB → 32KB) | `toolResultStorage.ts:175` |
| LRU eviction: drop unreferenced tool results after N turns | New mechanism |
| Snippet extraction (only relevant lines from file reads) | Tool result formatter |
| Recall-on-demand: model asks for old result via a tool call | New protocol |

**#7 — MCP tool schema accumulation**
Claude Code has `ToolSearchTool` for deferred loading, but MCP tools register eagerly by default.

| Fix | Where |
|---|---|
| Per-server tool cap (e.g., max 20 per MCP server) | MCP registration |
| Default-deferred for MCP tools | MCP registration policy |
| User-side allowlist (`.claude/mcp-allowlist.json` per project) | New config |

**#8 — Image results blowout**
Pure code fix:

| Fix | Where |
|---|---|
| Set real `maxResultSizeChars` for image tool result type | Tool result config |
| Auto-downsample images above a threshold | `imageResizer.ts` (already does this for *inputs*; extend to *outputs*) |
| Quality cascade (PNG → JPEG q80 → q60) until under budget | Same |

**The pattern:** all three are 50-line code changes. Easy. But they belong in a code-change roadmap, not a routing-research roadmap.

### 5.1.3 Deep Dive: #4 Subagent Cold-Start Overhead

Each subagent gets its **own context window** (~200K typical). It does *not* inherit the parent's conversation history. What it starts with is the cold-start boilerplate.

**Anatomy of a subagent's first API call:**

```
PARENT SESSION (already 80K tokens deep)
   │
   │  spawn subagent →
   │
   ▼
SUBAGENT (fresh context window — starts from zero)
   ├─ System prompt           ~15K tokens   ← re-sent
   ├─ Tool schemas             ~15K tokens   ← re-sent
   ├─ Subagent task prompt     ~1K tokens    ← new
   ├─ CLAUDE.md hierarchy      ~3K tokens    ← re-sent
   └─ TOTAL on first call:     ~34K tokens
```

**Where the "7×" overhead figure comes from:**

| Path | Cost |
|---|---|
| SkillTool (inline): skill injected into current context | ~5K extra tokens |
| Subagent: full cold start + own loop (3-5 turns × ~10K) | ~65-85K tokens |

For tasks that could be done inline, spawning a subagent is **roughly 7× more expensive**.

**When is the overhead worth it?**

| Worth it | Not worth it |
|---|---|
| Long-running research (50+ turns of exploration) | Quick file edit, simple grep, single bash |
| Parallel work (3 subagents save wall-clock time) | Sequential dependencies anyway |
| Hard isolation needed (subagent shouldn't see parent secrets) | Anything a SkillTool can do |
| Different model needed (subagent on Haiku, parent on Opus) | Same model anyway |

The routing decision is: *"Should this task be a subagent or a SkillTool?"* A classifier on the task description could make this decision smarter.

### 5.1.4 Deep Dive: #5 Tool Schemas + Cache Misses

**What's in the 15-30K of tool schemas?**

```
Built-in tools (~15 always-loaded):
  Bash, Read, Edit, Write, Grep, Glob, WebFetch, WebSearch,
  TaskCreate, TaskList, NotebookEdit, Agent, SendMessage,
  ToolSearch, etc.
  ≈ 12-18K tokens of JSON schemas

MCP server tools (variable):
  GitHub MCP: ~30 tools     →  +5-8K tokens
  Postgres MCP: ~15 tools   →  +3-4K tokens
  Slack MCP: ~50 tools      →  +10-15K tokens

Total tool block: typically 15-30K tokens, sorted alphabetically
for cache stability.
```

**Cache hit vs cache miss cost (Sonnet):**

| State | Cost on tool block (25K) | Per-turn |
|---|---|---|
| Cached (cache_read rate) | 25K × $0.30/Mtok | $0.0075 |
| Uncached (full input rate) | 25K × $3/Mtok | $0.075 |
| **Penalty per miss** | | **$0.0675** |

Over 50 turns with frequent misses, that's $3+ on tool schemas alone.

**What triggers a cache miss:**

| Trigger | What changes | Cache impact |
|---|---|---|
| Model switch (`/model`) | Cache key includes model name | 100% miss on next call |
| New MCP server connected mid-session | Tool block gets longer | Miss on tool block + everything after |
| Deferred tool loaded via ToolSearch | Tool block gets a new entry | Miss on tool block + everything after |
| CLAUDE.md edited on disk | System prompt body changes | Miss on system prompt + everything after |
| 5-minute TTL expires during a pause | Cache entry deleted server-side | Full miss until rebuilt |
| Feature flag changes tool pool | Tool block changes | Miss on tool block + everything after |

**Why a router must care about this:**

A naive per-turn router thrashes cache:

```
Turn 1: Opus    → builds 30K cache → free
Turn 2: Sonnet  → cache MISS, costs full 30K
Turn 3: Opus    → cache MISS AGAIN
Turn 4: Sonnet  → cache MISS AGAIN

→ Every turn pays full price on the tool block. Router achieved nothing.
```

**Breakeven math for a switch:**

```
breakeven_turns = cache_miss_penalty / per_turn_savings
```

For Opus → Sonnet on a 25K cached prefix:
- Penalty: $0.34
- Savings: ~$0.012/turn
- **Breakeven: ~28 turns on Sonnet to recover the bust**

If you don't expect 28+ more turns after the switch, *don't switch*. This is the core insight behind cache-aware routing.

---

## 5.1.5 How Cost Compounds Over a Session — The Structural View

The fundamental fact: **the model is stateless. Every API call re-sends the ENTIRE conversation.**

The API has no notion of "session." It doesn't remember turn 49 when turn 50 arrives. The client (Claude Code) re-assembles the full history and sends it every time. This is *why* cost grows the way it does.

### The 200K Context Window — What's In It

```
┌─────────────────────────────────────────────────────────────────┐
│ FIXED PREFIX  (system prompt + tool schemas + CLAUDE.md)         │
│ ≈ 15K-30K tokens · same bytes every turn · cache-friendly        │
├─────────────────────────────────────────────────────────────────┤
│ TURN  1   ▓                            ~1K tokens of history    │
│ TURN  5   ▓▓▓                          ~8K                      │
│ TURN 10   ▓▓▓▓▓▓▓                      ~20K                     │
│ TURN 20   ▓▓▓▓▓▓▓▓▓▓▓▓▓▓               ~50K                     │
│ TURN 35   ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓     ~100K                    │
│ TURN 50   ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓ ~150K ← COMPACT │
├─────────────────────────────────────────────────────────────────┤
│ OUTPUT RESERVATION  (~20K tokens · never filled by history)      │
└─────────────────────────────────────────────────────────────────┘
```

### Cost Per Turn (Opus: $15/M input, $75/M output)

| Turn | Input Size | Cost | vs Turn 1 |
|---|---|---|---|
| 1 | ~25K tokens | $0.38 | baseline |
| 20 | ~70K tokens | $1.05 | **2.8× more expensive** |
| 50 | ~170K tokens | $2.55 | **6.7× more expensive** |

That's the cost of a *single turn* at the bottom — not cumulative. At turn 50, every additional turn costs $2.55. 50 turns at that rate = $80-100 on input alone, **before output, before background workers, before subagent spawns**.

### The Caching Softener — And Its Limits

Prefix caching saves **10× on the stable part** (the fixed prefix). But only if cache is **not busted.**

**Cache busters:**
- Model switch
- New MCP tool connected mid-session
- 5-minute TTL expiry (or 1-hour with `tengu_prompt_cache_1h_config`)
- Tool pool change (deferred tool loaded, feature flag flip)
- CLAUDE.md edited on disk

**Why this matters for routing:** A naive router that switches models often can bust a 30K cached prefix and pay back the savings ten times over. **Cache state must be a hard constraint on every routing decision.** This is the central insight behind Phase 2 of the research sequence.

### Why Compaction Fires at 150K, Not 200K

The 20K output reservation has to be preserved. The model needs room to *reply*. So `effective_input_budget = context_window - max_output_tokens - safety_buffer`. For a 200K window with 20K output and ~30K safety buffer, the trigger lands around 150-187K depending on configuration.

---

## 5.2 How Prompt Caching Actually Works

The single biggest cost optimization. **Cached input tokens cost 10× less.**

### The Mechanism

Every API call sends the **entire conversation** to the server. The server **remembers the prefix** of your previous request. If the next request starts with the same bytes, the server skips re-processing those tokens and charges 10× less.

```python
# Turn 1: No cache
request_1 = system_prompt + tools + "User: fix the bug"
# Cost: full price for all 30K tokens

# Turn 2: Cache exists!
request_2 = system_prompt + tools + "User: fix the bug" + "Assistant: ..." + "Tool: ..." + "User: now add tests"
#           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
#           This prefix is IDENTICAL to request_1
#           → CACHE HIT → 10× cheaper for these tokens
#           → Only the NEW tail pays full price
```

### What's Actually in the Cache: KV Tensors

Not text. Not embeddings. **Precomputed Key and Value matrices** from the attention layers.

```python
# Each transformer layer computes:
K = input_embeddings @ W_k   # Key matrix
V = input_embeddings @ W_v   # Value matrix
# These are what the cache stores — at every layer.
```

For 50K cached tokens on a large model, that's **gigabytes of GPU memory** holding KV tensors.

### How Cache Hits Are Checked

**Deterministic prefix match on raw token IDs.** Not semantic similarity, not embeddings.

```python
def check_cache(new_tokens, cached_tokens):
    match_length = 0
    for i in range(min(len(new_tokens), len(cached_tokens))):
        if new_tokens[i] == cached_tokens[i]:
            match_length += 1
        else:
            break  # First mismatch → everything after is uncached
    return match_length
```

**Why order matters:** If you change even one token in the system prompt, the match breaks from that point onward and everything after pays full price.

**Why Claude Code sorts tool schemas alphabetically:** if tool order was random, adding one new tool could shift everything and bust the entire tool block. Sorting ensures stability.

---

## 5.2.3 Does Caching Cover Previous Messages? Yes — Up to 4 Breakpoints

A common misconception: "Prefix caching only caches the system prompt and tools." **Wrong.** The cache extends as far into the conversation as you mark it.

### Two Concepts Running Together

| Concept | What it is |
|---|---|
| **Automatic prefix caching** | The underlying mechanism. Server matches the longest byte-stable prefix of any request. |
| **`cache_control` breakpoints** | Anthropic's per-request markers. You attach them to content blocks. Up to **4 per request**. |

The server doesn't "decide" what to cache — *you* tell it, via `cache_control` markers. Each marker creates a separate cache entry (a "breakpoint"). The server stores up to 4 nested prefixes.

### What Claude Code Actually Caches (4 Breakpoints)

```
─────────────────────────────────────────────────────────────
SYSTEM PROMPT       ~2-3K tokens     ◀── cache_control  ① CACHE BOUNDARY 1
TOOL SCHEMAS        ~8-12K tokens    ◀── cache_control  ② CACHE BOUNDARY 2
CLAUDE.md           ~1-5K tokens     ◀── cache_control  ③ CACHE BOUNDARY 3
MESSAGES 1...40     (older history)  ◀── cache_control  ④ CACHE BOUNDARY 4
─────────────────────────────────────────────────────────────
MESSAGES 41, 42, 43   (recent)         NOT cached · fresh every turn
CURRENT TURN MESSAGE                   NOT cached · new
─────────────────────────────────────────────────────────────
```

Notice: **boundary 4 sits behind older messages.** The cache covers msg 1 through msg 40 even though those are conversation history, because that content is byte-stable from turn to turn.

### Why Recent Messages Aren't Cached

Three reasons:
1. Each `cache_control` slot is precious — only 4 available
2. Recent messages change every turn (new tool results, new responses) — caching them would create entries that immediately become stale
3. The next turn shifts them backward — what's "msg 43" today becomes part of "older messages" tomorrow

### Cascading Breakpoints (Incremental Caching)

As the conversation grows, Claude Code slides boundary 4 forward. Each slide creates a new cache entry that reuses the prior entry as its prefix:

```
Turn 50: boundary 4 covers msg 1...40
Turn 60: boundary 4 covers msg 1...50  (reuses turn-50 entry up through msg 40)
Turn 70: boundary 4 covers msg 1...60  (reuses turn-60 entry up through msg 50)
```

This is what keeps long sessions affordable — each cache "write" is a one-time tax (1.25× input price), but the resulting "read" is 10× cheaper than uncached. Over many subsequent hits, the math wins.

### The Cache Hit / Miss Outcomes

| Outcome | Cost | When it happens |
|---|---|---|
| **Cache hit** | ~10× cheaper than full input price (`cache_read` rate) | Server has seen this exact prefix within TTL |
| **Cache write** | 1.25× full input price | Server is creating a new cache entry (first time it sees this prefix) |
| **Cache miss** | Full input price | Cache entry exists but prefix has diverged |

### TTL

- **5 minutes** — Anthropic default
- **1 hour** — feature-gated (via `tengu_prompt_cache_1h_config`); not available to all users
- Pause longer than the TTL → server evicts → next request pays full price on the whole prefix

### Single-Sentence Summary

> **The cache is whatever you mark with `cache_control`, up to 4 breakpoints. Claude Code uses all four, and the deepest one extends well into the conversation history. Caching isn't just "system prompt and tools" — it's whatever can be made byte-stable across turns.**

## 5.2.3.1 Where Does the 10× Cache Discount Come From?

The "10× cheaper" figure for cache reads is **Anthropic's fixed pricing multiplier**, not a derived calculation.

### Verified Pricing Multipliers (Anthropic's Official Rate Card)

| Operation | Multiplier on base input price |
|---|---|
| **Cache read** (hit) | **0.1×** (the "10× cheaper") |
| **Cache write, 5-min TTL** | 1.25× (small premium to create entry) |
| **Cache write, 1-hour TTL** | 2.0× (bigger premium for longer-lived entry) |
| **Standard input** (uncached) | 1.0× (baseline) |

Applied uniformly across **every Claude model** — Opus, Sonnet, Haiku — no exceptions.

### Current Anthropic Pricing (per million tokens, verified from platform.claude.com)

| Model | Base Input | 5m Cache Write | 1h Cache Write | Cache Read | Output |
|---|---|---|---|---|---|
| Opus 4.5 / 4.6 / 4.7 | $5 | $6.25 | $10 | **$0.50** | $25 |
| Opus 4 / 4.1 | $15 | $18.75 | $30 | **$1.50** | $75 |
| Sonnet 4 / 4.5 / 4.6 | $3 | $3.75 | $6 | **$0.30** | $15 |
| Haiku 4.5 | $1 | $1.25 | $2 | **$0.10** | $5 |
| Haiku 3.5 | $0.80 | $1 | $1.60 | **$0.08** | $4 |

### Why 10×? Two Plausible Readings

1. **Engineering reason** — the GPU cost of reusing precomputed K/V tensors is roughly 10% of the full forward-pass cost. The 0.1× billing reflects the underlying compute economics (with margin).

2. **Business reason** — a 10× discount is the threshold where developers actually restructure their prompts to maximize caching. Smaller discounts don't motivate engineering investment in cache stability; 10× does.

### Industry Comparison (verified Nov 2026)

| Provider | Best cache read multiplier | Activation | Extras |
|---|---|---|---|
| **Anthropic** (all models) | 0.1× (90% off) | Explicit `cache_control` markers | 1.25× write premium (5m TTL), 2× (1h TTL) |
| **Google Gemini 2.5 Pro** | 0.1× (90% off) | Explicit context cache API | $4.50/M-tokens-per-hour storage fee |
| **Google Gemini 2.0 Flash** | 0.25× (75% off) | Same as above | Same as above |
| **OpenAI** (GPT-4o family) | ~0.5× (50% off, per Oct 2024 announcement) | Automatic prefix detection | No write premium, no storage fee |

**Key insight:** Anthropic and Google's flagship now match at 0.1×. OpenAI is the outlier — less discount, but simpler activation (no markers, no storage fee). The trade-off is **how much engineering investment the discount demands**.

### Why 10%? — Reconstruction From First Principles (Not an Anthropic Statement)

This is **inference**, not a sourced claim. A cache hit physically skips:

| Computation | Approx fraction of prefill cost | Skipped on cache hit? |
|---|---|---|
| Q/K/V projections per layer for cached positions | ~30% | ✓ |
| Attention (Q·Kᵀ, softmax, ·V) for cached positions | ~10% | ✓ |
| MLP/FFN per layer for cached positions | **~55%** | ✓ |
| Token embedding | tiny | ✓ |
| Memory bandwidth (loading cached K/V) | small | ✗ still paid |
| Attention with fresh tokens against cached K/V | small | ✗ still paid |

If a cache hit skips ~95% of compute and pricing is 10% of input, that's roughly: real cost (~5%) + margin (~5%). Plausible economics — but Anthropic has never publicly stated their internal cost breakdown, so this remains a reconstruction.

### The Two Interpretations of Why 0.1×

| Read | The argument |
|---|---|
| **Engineering** | 0.1× ≈ real prefill compute cost (~5%) + margin (~5%). Matches the ~95% compute skip from cache hits. Justifies the price as economically meaningful, not loss-making. |
| **Business** | 0.1× is the threshold that motivates developers to engineer for caching. A 2× or 3× discount doesn't move behavior; 10× triggers investment in prompt stability. Aligns developer incentives with Anthropic's GPU savings — developers do free work to improve their own cache hit rate. |

**Most likely:** both are true. The compute economics make 0.1× viable. The behavioral economics make 0.1× the right number to announce.

### Why the MLP Gets Skipped on Cache Hits

A common follow-up: "K/V projections being skipped makes sense — but why does the MLP layer count as skipped too?"

**Because downstream layers only need K and V from cached positions, not the MLP outputs.** The autoregressive attention math reuses past keys and values; it never re-reads past MLP activations. So on a cache hit, the MLP layers at cached positions are wasted compute — the cache short-circuits them entirely.

This is why the skip percentage is roughly 95%, not 35%. The MLP block is the largest single cost (~55% of prefill), and it's the biggest beneficiary of caching.

### Why 0.1× and Not 5× or 20×?

| Hypothetical discount | Plausibility |
|---|---|
| 20× discount (5% of input) | Probably loss-making — would mean Anthropic charges less than the memory-bandwidth + overhead of serving a cache hit |
| 10× discount (10% of input) | **Current rate** — leaves ~5% margin on real cost; large enough to motivate developer behavior |
| 5× discount (20% of input) | Profitable but probably wouldn't move developer behavior enough to maximize cache hit rates |
| 2× discount (50% of input) | OpenAI's level — comfortable for vendor, but doesn't drive engineering investment in cache stability |

The 10× number sits at the intersection of "still profitable" and "behaviorally meaningful." That's not an accident.

### How Cost Scales Across a Long Session — Asymptotic + Amortized

#### Per-turn cost trajectory (Opus 4.6/4.7, 200K window, ~2K out/turn)

| Turn | Cached prefix | $/turn | Dominant component |
|---|---|---|---|
| 1 | 25K (cold write) | $0.21 | Cache write tax |
| 5 | ~35K | $0.08 | Output tokens |
| 10 | ~45K | $0.08 | Output tokens |
| 20 | ~70K | $0.10 | ≈ tied |
| 30 | ~95K | $0.12 | Prefix |
| 50 | ~150K | $0.16 | Prefix |
| 50 (compact fires) | — | +$3-4 spike | One-off |
| 51 (post-compact) | ~30K | $0.08 | Output tokens |
| 80 | ~120K | $0.13 | Prefix |

#### Two regimes

| Regime | Dominant cost | Behavior |
|---|---|---|
| **Early (turns 1-20)** | Output tokens (~$0.05/turn flat) | Cost roughly constant |
| **Mid-Late (turns 20-50)** | Cached prefix × cache-read price | Cost grows linearly with prefix size |
| **Compaction event** | Forced summarization | One-time $3-4 spike, then resets |

#### Asymptotic shape

```
Per-turn cost      = O(prefix_size × cache_read_rate)  ≈ linear in turn count
Cumulative cost    = Σ Cost(turn n) = O(N²)            ≈ quadratic
Compaction events  = periodic resets that break the quadratic chain
```

#### Where the pain starts

| Range | Behavior |
|---|---|
| Below turn ~30 | Cost flat at ~$0.08/turn — no pain |
| Turns 30-50 | Cost climbs linearly to ~$0.16/turn — manageable |
| Past 50 turns | Compaction spikes every ~50 turns ($3-4 each) — periodic pain |
| Past 100 turns | Cumulative spend $15-20+ — measurable |
| Past 200 turns (heavy) | Cumulative spend $80-150 — significant |

#### 1M context window — flat pricing confirmed

Verified from Anthropic's pricing page:
> *"Opus 4.7, Opus 4.6, and Sonnet 4.6 include the full 1M token context window at standard pricing. A 900k-token request is billed at the same per-token rate as a 9k-token request."*

**There is no "2× tier above 200K" for current models.** Earlier analyses that claimed this were based on the older Claude 3.5 Sonnet 1M beta — that rule was dropped.

| Session length | 200K window total | 1M window total |
|---|---|---|
| 50 turns | ~$5-8 | ~$5-8 |
| 100 turns | ~$15-20 (1-2 compacts) | ~$25-40 |
| 200 turns (heavy) | ~$80-150 (4-5 compacts) | ~$200-400+ |

**Trade-off:** 1M defers compaction (~200+ turns vs ~50 turns), so fewer spikes — but each turn costs more because the prefix grows further. For most coding sessions, **200K + frequent compaction is the cheaper path.** 1M is the right call only when uncompacted long-range recall is genuinely needed.

---

### API vs Subscription — Choosing the Right Channel

#### The two channels at a glance

| | API (pay-as-you-go) | Subscription (Pro / Max / Team / Enterprise) |
|---|---|---|
| Billing | Per-token | Flat monthly fee per seat |
| Rate limits | Per-call, tier-based | 5-hour AND 7-day rolling windows |
| Interface | Programmatic (HTTPS) | Personal interactive (web + CLI + IDE) |
| Users | Multi-user via API keys | One seat per user |
| Model variants | All exposed | Subset, opinionated UX |
| Cloud routing | Bedrock / Vertex available | First-party only |
| Batch API discount | 50% off async | N/A |
| Compliance | ZDR / DPA / SOC2 via enterprise contract | Limited (Enterprise tier only) |

**Same model under the hood — different access economics.**

#### When each wins

| API wins | Subscription wins |
|---|---|
| Building a product on Claude | Personal developer use |
| Multi-user / customer-facing apps | Predictable monthly cost |
| Programmatic / batch workloads | Heavy interactive use (Claude is your daily tool) |
| Compliance: SOC2, ZDR, DPA, audit | One person, one machine |
| Bedrock / Vertex cloud integration | Token cost math hidden from you |
| Batch API discount (50%) | Generous limits if you stay disciplined |
| Custom rate-limit negotiations | |
| Per-call attribution of costs | |

The decision isn't about quality. It's about whether you're **consuming Claude** (subscription) or **distributing it** (API).

#### The quality-degradation question

Recurring community suspicion: "Does the subscription tier get a quietly degraded model?"

| Claim | Status |
|---|---|
| Same model weights served on both | No explicit Anthropic confirmation, but no contrary evidence |
| Subscription uses smaller/quantized variant | **Speculated but unproven.** No reproducible benchmark |
| Anthropic throttles subscription load | **Confirmed** — via rate limits, queueing, capacity controls — NOT model swap |
| Inference-path differences cause variance | **Plausible** — server load, priority queues, GPU memory pressure produce variance within the same model |

**Honest verdict:** the suspicion can't be cleanly answered without independent benchmarks. Reports of "Claude felt worse today" likely reflect capacity effects, not model substitution. The verifiable differences (features, scale, compliance) dominate the decision anyway.

#### Practical decision guide

| Your situation | Use |
|---|---|
| Personal R&D / coding tool | Subscription (Pro/Max/Team) |
| Building Claude into a product | API (programmatic, scales with users) |
| Compliance-heavy enterprise | API + enterprise contract (DPA, SOC2, ZDR) |
| Heavy batch / async workloads | API + Batch API (50% discount) |
| Cloud lock-in (AWS / GCP) | Bedrock or Vertex (API via cloud) |
| Just exploring | Free or Pro subscription |

#### Why rate limits matter even on subscription

Even though your subscription is flat-fee, you still benefit from cost-saving practices because:
- **5-hour window cap** — heavy use locks you out for several hours
- **7-day window cap** — sustained heavy use locks you out for days
- **Model-specific limits** — Opus has stricter limits than Sonnet/Haiku

Burning through your quota faster than necessary translates to **time lost waiting**, not money lost. The cache-saving habits (don't switch models often, keep CLAUDE.md lean, disconnect unused MCPs, etc.) reduce your token throughput, which extends your effective working window before hitting limits.

### Pricing-Implication Recalc

The "$0.34 penalty on a 25K Opus prefix" figure in earlier slides reflects **older Opus 4 / 4.1 pricing ($15/M base)**. Under current Opus 4.5+ pricing ($5/M base), the same penalty is:

```
penalty = 25K × ($5 - $0.50) / M = $0.1125 ≈ $0.11
```

Still real, still meaningful — just smaller. The *shape* of the math is unchanged.

## 5.2.4 Prefix Caching vs Prompt Caching — Same Thing

The two terms describe the same mechanism:

- **"Prefix caching"** describes *how* it works (matches on the prefix of tokens)
- **"Prompt caching"** describes *what* it caches (the prompt you send)

**Vendor terminology:**

| Vendor / Project | What they call it |
|---|---|
| Anthropic | "prompt caching" |
| Google, OpenAI | "context caching" |
| vLLM, SGLang | "prefix caching" |
| Academic papers | "prefix caching" |

Some systems add features on top — `cache_control` markers (Anthropic), automatic prefix detection (vLLM) — but the underlying mechanism is identical: deterministic prefix match on token IDs → reuse precomputed K/V tensors.

**One sentence:** Prompt cache = prefix cache. Marketing name vs technical name.

## 5.2.5 What's Actually Cached — KV Tensors, Not Strings

A common misconception: people think the prompt cache stores text, embeddings, or even prior responses. **It stores none of those.** It stores precomputed attention state — specifically, the Key (K) and Value (V) tensors at every transformer layer.

### What the Transformer Computes

Each layer of the model computes three matrices from input tokens:

```python
K = input_embeddings @ W_k    # Key matrix
V = input_embeddings @ W_v    # Value matrix
Q = input_embeddings @ W_q    # Query matrix

attention = softmax(Q @ K.T / sqrt(d_head)) @ V
```

For a ~100-layer model processing 50K tokens, the K and V tensors are the bulk of the work. They're computed **per layer, per token** — that's most of the forward pass.

### Why K and V Are Cached, Not Q

Because of attention math. When generating token N+1, the model attends to all previous tokens — K and V come from past tokens (don't depend on the current generation step). Q depends on the current token, so Q is recomputed every step. **K and V are the reusable parts.**

### What the Cache Conceptually Holds

```python
cache_entry = {
    "layer_0":  {"K": tensor(50000, d_head), "V": tensor(50000, d_head)},
    "layer_1":  {"K": tensor(50000, d_head), "V": tensor(50000, d_head)},
    ...
    "layer_99": {"K": tensor(50000, d_head), "V": tensor(50000, d_head)},
}
```

Gigabytes per entry on a large model. **GPU memory, not disk.**

### How a Hit Is Checked

Not semantic match. Not embedding lookup. **Deterministic prefix match on raw token IDs:**

```python
def check_cache(new_tokens, cached_tokens):
    match_length = 0
    for i in range(min(len(new_tokens), len(cached_tokens))):
        if new_tokens[i] == cached_tokens[i]:
            match_length += 1
        else:
            break  # First mismatch → everything after is uncached
    return match_length
```

"Do the first N tokens match?" If yes, server reuses K/V for those N positions and only computes fresh K/V for the new tokens after the mismatch.

### So the Cache Has Two Parts

| Part | What it does |
|---|---|
| **Lookup key** | Raw token IDs of the prefix — cheap to compare |
| **Stored value** | Precomputed K/V tensors at every layer — gigabytes |

The token IDs are the **fingerprint**. The KV tensors are the **payload**. The 10× discount comes from skipping the K/V computation, not from skipping any text processing.

### Common Confusion: Prompt Cache ≠ Response Cache

The prompt cache **does not** save previous model outputs. The model always generates a fresh response. **The cache only skips the expensive K/V computation on the input prefix — never the generation step.**

## 5.2.6 Cache Busting — The Complete Picture

> **The cache prefix must match BYTE-FOR-BYTE. Any change before a cache_control marker invalidates everything downstream.**

### What Busts the Cache, Grouped by Impact

#### Catastrophic (every cached entry lost)

| Buster | Why |
|---|---|
| **Model switch** (Opus → Sonnet, etc.) | Different W_k, W_v matrices → KV tensors mathematically incompatible. Sonnet cannot read Opus's cache. |
| **5-min TTL idle expiry** | Server evicts KV tensors from GPU memory to make room for other users. Cold rebuild on next call. |

#### Tool block changes (boundary 2 + everything after)

| Buster | Why |
|---|---|
| **New MCP server connected mid-session** | Tool block bytes diverge. |
| **Deferred tool loaded via ToolSearch** | New schema appears in the middle of the tool list. |
| **Feature flag flips tool pool** | Tool block hash changes. |

#### System prompt changes (boundary 1 + everything after)

| Buster | Why |
|---|---|
| **CLAUDE.md edited on disk** | System content bytes diverge. |
| **Date rollover (rare)** | System prompt includes current date — changes once per day. Affects long-running sessions across midnight. |

#### History changes (boundary 4)

| Buster | Why |
|---|---|
| **Microcompaction rewrites old tool results in place** | Bytes change in the middle of history → cache invalidates from that point forward. Internal system event, not user action. |

### How Claude Code Defends Cache Stability

The code is engineered around preserving the cache. Four key design choices:

| Defense | What it prevents |
|---|---|
| **Tool schemas sorted alphabetically** | Adding a new tool at the end doesn't reorder existing ones → no byte change in cached prefix |
| **Advisor tool appended LAST** | Toggling advisor on/off doesn't affect cached content |
| **Deferred tools sent as name stubs** (not full schemas) | Loading them adds at the end, minimizes disruption |
| **MCP tools appended after built-ins** | MCP changes only affect the tail of the tool list, never the core prefix |

**Reading between the lines:** the defenses tell you what the team prioritized. They engineered around tool-block stability obsessively, because that's the one developers can accidentally bust.

### Why Cache Busting Is a Routing Constraint

The naive router's reasoning:

> "Sonnet input is $3/M vs Opus $15/M. Saving $0.012/turn on 2K new tokens. Switch to Sonnet."
>
> Looks like a $0.60 win over 50 turns. Ship it.

What actually happens:

```
Turn N-1   Opus       cache HIT   (paid 25K × $1.50/M = $0.0375)
Turn N     Sonnet     cache MISS  (paid 25K × $3/M    = $0.075)
                      ↓
                      Miss penalty: 25K × ($15 - $1.50)/M = $0.34
```

Wait — but the cache_read price for Sonnet is $0.30/M, not $0.075/M from the table. Let me restate carefully:

**Switching from Opus to Sonnet busts the cache entirely because the entry was Opus-keyed.** Sonnet has to rebuild its own cache from scratch. The first Sonnet turn pays full input price on what was previously cached at Opus's 10×-cheaper rate.

| Scenario | Cost on 25K prefix |
|---|---|
| Opus, cache HIT | 25K × $1.50/M = **$0.0375** |
| Opus, cache MISS (first call or post-bust) | 25K × $15/M = **$0.375** |
| Sonnet, cache MISS (post-switch, first call) | 25K × $3/M = **$0.075** |
| Sonnet, cache HIT (after rebuilding) | 25K × $0.30/M = **$0.0075** |

**The "penalty" of switching:** you go from paying $0.0375/turn (Opus cached) to $0.075/turn (Sonnet cold) — that's the immediate hit. After ~1 turn Sonnet has its own cache, paying $0.0075/turn — much cheaper than Opus cached. But you had to pay the rebuild tax to get there.

### The Break-Even Math — Refined

The simpler formula has three variants depending on which savings you count:

```
break_even_turns = penalty / savings_per_turn
```

**Penalty** = `cached_tokens × (full_input_price − cached_input_price)`

For a 25K Opus prefix (older $15/M pricing): 25K × ($15 − $1.50)/M = **$0.34**.

**Savings** depends on what you count:

| Variant | What's counted | Per-turn savings | Break-even |
|---|---|---|---|
| Best case | Input prefix savings + full output savings | ~$0.15 / turn | ~3 turns |
| Prefix-only | Input prefix savings only | ~$0.03 / turn | ~11 turns |
| Conservative | Smaller savings + larger prefix + cache rebuilds along the way | ~$0.012 / turn | up to ~28 turns |

**Reality is somewhere between.** The exact figure depends on:
- Whether you count output token savings (yes → 3 turns; no → 11+ turns)
- Prefix size (larger prefix = larger penalty AND larger savings, but ratio shifts toward more break-even turns)
- How many cache rebuilds happen during the session (TTL expiry, MCP changes)

**The exact number matters less than what the formula requires.** It requires `remaining_turns_on_new_model` — and that's unknowable at decision time.

**Practical interpretation:** if the session has fewer than ~10 turns left after the switch, the switch likely loses money. If it has 30+, the switch likely pays off. The 11-28 turn range is the dangerous zone where you can't tell without knowing the horizon.

### The Principle

```
real_cost = (tokens × price) + cache_miss_penalty
```

- ✓ Cache state is a **hard constraint** on every routing decision
- ✓ Switch rarely · stay on a model for runs of turns · amortize the cache rebuild
- ✗ Per-turn model swapping = cache thrashing = costs *more* than no router at all

**This is Phase 2 of the research roadmap — the first formal cache-aware routing constraint.**

## 5.2.7 The Horizon Problem and Cost Transparency as a Solution

### Why the Break-Even Math Alone Isn't Enough

The break-even formula `penalty / savings_per_turn` requires `remaining_turns_on_new_model` as an input. **That number is genuinely unknowable at decision time:**

- The user doesn't know how long their coding task will run
- The router can only infer from phase signals, task type, session history — never certainty
- One bad switch can wipe out savings from 100 turns of careful background-agent routing

So "compute the break-even" is necessary but not sufficient. It tells you the *threshold*; it doesn't tell you whether you'll cross it.

### The Reframe: Cost Transparency Instead of Prediction

Instead of asking the router to predict the unknowable, **expose the trade-off to the user** — who has tacit knowledge of their own task horizon.

| Old Problem (router-driven) | New Problem (UX-driven) |
|---|---|
| "How many turns will the user spend on Sonnet?" | "Can we expose the trade-off so the user decides when to switch and when to stay?" |
| Unknowable, hostile signal | Tractable UX problem |
| Math dictates decision | Math powers a user-facing signal |

### The UI Pattern

```
┌────────────────────────────────────────────────────┐
│ Model: Sonnet 4.6   ·   Switched: turn 50          │
│                                                    │
│ Cache rebuild cost:    $0.34                       │
│ Break-even at:         turn 78                     │
│                                                    │
│ Savings recovered:                                 │
│   ████░░░░░░░░░░░░░░░░░░  5 / 28 turns  (18%)      │
│                                                    │
│ Per-turn savings:      $0.012                      │
│ Recovered so far:      $0.06 of $0.34              │
│                                                    │
│ ⚠ Cache TTL: 4m 12s — pause longer = bar resets   │
└────────────────────────────────────────────────────┘
```

### Why It Will Work — The Behavioral Analog

Claude Code already exposes one invisible cost: **the context window indicator.** Users adapt to it without being told to:
- Wrap up tasks before compaction fires
- Trigger `/compact` at natural boundaries
- Split sessions before context overflow

**The break-even bar is the same UX pattern, applied to a different invisible cost.** Same psychology, same kind of adaptive behavior.

### Expected User Adaptations

With break-even visible, users naturally:
- **Batch coding work** into longer Sonnet stints before switching back
- **Avoid impulsive switches** for tiny tasks (single fix isn't worth the rebuild)
- **Plan around TTL** — finish before the idle clock resets the bar
- **Stop second-guessing** — the cost is named, the choice is conscious

### Failure Modes and Mitigations

| Failure mode | Mitigation |
|---|---|
| **Gamification** — user races to fill the bar past natural stopping points | Frame as "savings recovered" not "target to hit"; don't celebrate bar completion |
| **Misleading number** — break-even assumes continued cache hits | Show TTL alongside; warn on cache risks |
| **UI overload** — some users don't care about routing economics | Make collapsible / opt-in; show only after explicit `/model` switches |

### Why This Is a Research Contribution

| Original Phase 2 | Augmented Phase 2 |
|---|---|
| Cache-aware constraint as constrained optimization | + Cost-transparency framework: expose the trade-off to the user |
| Router decides | Router decides for non-user-visible surfaces; user decides for explicit switches with full info |
| ML problem | ML + UX problem |
| One paper | Potentially two: math + behavioral study |

The behavioral study is itself a tractable project: A/B users with the bar vs without, measure switch frequency and total cost. Real empirical contribution.

### Composable with Autonomous Routing

The reframe doesn't kill autonomous routing — it complements it:

| Surface | Decision authority |
|---|---|
| Background workers, compaction, subagents | **Router decides autonomously** (horizon is known or single-call) |
| Explicit user `/model` switches | **Bar shown, user decides** (full info, user knows their own task) |
| Uncertain main-loop transitions | **Router proposes, user confirms** (hybrid mode) |

### The Principle

> **Cost transparency turns an unsolvable prediction problem into a tractable user-choice problem. The router doesn't need to know the future. The user has tacit knowledge of their own task. Expose the trade-off; let them spend it.**

### Minimum Viable Version

The simplest shippable version of this idea:

1. Detect every explicit `/model` switch
2. At switch time, compute and display: prefix size, penalty, per-turn savings, break-even turn, current TTL
3. After each subsequent turn, update: savings recovered, progress percentage, TTL remaining
4. Issue a warning if cache state is at risk (idle approaching TTL, MCP change pending)

**Effort:** 1-2 weeks for a prototype. No changes to routing logic — just instrumentation + UI.

This is a strict win regardless of whether anyone uses it: users who care get information; users who don't are unaffected.

### Success Metrics for the A/B Study

| Metric | Expected direction | Why it matters |
|---|---|---|
| Switches per session | Down (less impulsive switching) | Indicates users are informed |
| Average turns post-switch | Up (longer Sonnet stints) | Indicates batching behavior |
| Total session cost | Down | The ultimate validation |
| Task completion rate | Unchanged or up | Confirms no negative side-effect on quality |
| User satisfaction (qualitative) | Up | Cost-transparency tends to increase trust |

If any of cost/completion goes the wrong way, the bar's framing needs tuning. If all four move correctly, the UX is ready to default-on.

### Composability Recap

| Routing surface | Decision authority | Why |
|---|---|---|
| Background workers, compaction, subagents | **Router decides autonomously** | Each task is single-call or closed-context; horizon is known |
| Explicit user `/model` switches | **Bar shown; user decides** | Horizon unknowable; user has tacit knowledge |
| Uncertain main-loop transitions | **Router proposes; user confirms (or auto-OKs)** | Hybrid — router's prior is shown alongside user's choice |

These three modes coexist. The bar doesn't replace autonomous routing; it fills the gap where autonomous routing has insufficient information.

## 5.3 Model Switching and Cache Destruction

When you switch from Opus to Sonnet (or vice versa), **the entire cache becomes unusable**.

```python
# Opus weights:
K_opus = tokens @ W_k_opus    # Certain KV tensors

# Sonnet weights (different parameters):
K_sonnet = tokens @ W_k_sonnet  # Same tokens, DIFFERENT KV tensors
```

It's not that the cache is "thrown away" by choice — the cached KV tensors are **mathematically incompatible** with a different model.

### The Cache Miss Penalty

Cache miss on a 25K-token prefix (Opus pricing):
```
Uncached:  25K × $15/M  = $0.375
Cached:    25K × $1.5/M = $0.0375
Penalty:   $0.34 per cache bust event
```

If a router naively switches Opus → Sonnet every turn, the savings are dwarfed by the cache miss penalty.

---

## 5.4 The Golden Rule for Router Design

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
        return cache_state.current_model  # Amortize cache first

    # Only switch when savings over next N turns > cache rebuild cost
    savings_per_turn = estimate_savings(new_model, cache_state)
    cache_rebuild_cost = estimate_cache_miss(cache_state)
    if savings_per_turn * expected_remaining_turns > cache_rebuild_cost:
        return new_model
    return cache_state.current_model
```

### When Model Switching DOES Make Sense

| ✓ YES — Switch once, stay | ✗ NO — Per-turn routing |
|---|---|
| Start on Opus for planning → switch to Sonnet for implementation. One cache rebuild, savings accumulate. | "This turn simple → Haiku, next turn complex → Opus." Cache thrashes, costs spike. |
| User explicitly asks for cheaper model | Frequent oscillation between any models |
| Different models for different *agents* — main on Opus, background on Haiku. **No cache conflict** because each agent has its own context. | |

---

## 5.5 The Five Routing Seams in Claude Code

Marked on the technical architecture diagram. These are the places where a router can intercept:

| Seam | File | What you can route |
|---|---|---|
| **Seam 1 — Tool pool filter** | `tools.ts:271` | Which tools loaded for this turn |
| **Seam 2 — Compaction model** | `compact.ts:1188` | Model used for summarization |
| **Seam 3 — Per-call model** | `model.ts:145` | Main loop model |
| **Seam 4 — Cache/retry policy** | `claude.ts:358` | Cache TTL, retry strategy |
| **Seam 5 — Pre-tool hooks** | `toolExecution.ts` | Intercept before tool runs |

## 5.6 Tier Identification — The Research Roadmap

Five routing opportunities, tiered by impact, ordered for execution.

### Tier 1 — Highest Impact, Do First

| ID | Opportunity | Savings | Effort | Contribution Type | What it Does |
|---|---|---|---|---|---|
| **A** | Background Agents → Haiku | 20-25% | Near-zero | Rule-based | Route extractMemories, SessionMemory, AgentSummary, PromptSuggestion to Haiku. Env var `CLAUDE_CODE_SUBAGENT_MODEL` already exists. |
| **B** | Cache-Aware Routing Constraint | High | Medium | **Novel theory** | Don't switch models if cache_miss_penalty > expected per-turn savings × remaining turns. First formalization of cache economics in routing. |
| **C** | Subagent Task-Type Routing | High when subagents used | Medium | **ML contribution** | Classify subagent task from prompt → assign optimal model before spawning. |

### Tier 2 — Strong Candidates

| ID | Opportunity | What it Does |
|---|---|---|
| **D** | Phase-Based Turn Routing | Exploration → Sonnet, Planning → Opus, Implementation → Sonnet, Debugging → Opus. Full system routing by workflow phase. |
| **E** | Adaptive Compaction | Haiku decides compression depth based on phase/content. Variable cap instead of fixed cap. |

### The 5-Phase Research Sequence

```
Phase 1 — BACKGROUND AGENTS → HAIKU
  Rule-based, ships fast, funds the rest
  Deliverable: production routing implementation
  Savings:     20-25% session cost reduction
  Timeline:    2-4 weeks
  Risk:        Near zero

Phase 2 — CACHE-AWARE ROUTING CONSTRAINT
  Novel theory contribution
  Deliverable: formalization + empirical validation paper
  Output:      First routing paper to model prompt cache as hard constraint
  Timeline:    2-3 months
  Risk:        Theoretical contribution; depends on Phase 1 data

Phase 3 — SUBAGENT CLASSIFIER (ML)
  Genetic framework fits here
  Deliverable: trained classifier + training methodology
  Output:      ML contribution; evolutionary search over routing policies
  Timeline:    3-4 months
  Risk:        Data collection bottleneck

Phase 4 — PHASE-BASED ROUTING (FULL SYSTEM)
  Combines Phase 2 + Phase 3 into end-to-end system
  Deliverable: production routing layer with per-iteration decisions
  Output:      End-to-end routing policy with quality + cost benchmarks
  Timeline:    4-6 months
  Risk:        Integration complexity

Phase 5 — COMPACTION QUALITY STUDY
  Empirical validation
  Deliverable: Pareto analysis of compaction model vs downstream quality
  Output:      Recommended compaction policy backed by data
  Timeline:    Ongoing
  Risk:        May surface unexpected quality regressions
```

### Why This Order

Each phase produces independent value AND enables the next:

- **Phase 1 ships even if Phase 2 fails.** Pure baseline win.
- **Phase 2 publishes even if Phase 3 stalls.** Theory contribution stands alone.
- **Phase 3 contributes ML methodology** regardless of Phase 4 integration.
- **Phase 4 is the integration win** — assumes 2 and 3 succeeded.
- **Phase 5 validates the quality side** of all prior phases.

**Cumulative risk-managed, with publishable outputs at every step.**

### Expected Total Impact

| Phase | Marginal Savings | Cumulative |
|---|---|---|
| 1 | 20-25% | 20-25% |
| 2 | (constraint, not direct savings) | 20-25% |
| 3 | 10-15% (subagent routing) | 30-40% |
| 4 | 10-15% (phase routing) | 40-50% |
| 5 | 5-10% (compaction routing) | 45-60% |

**Headline:** 40-60% total session cost reduction with minimal quality loss, when all phases land.

### Where the Genetic Framework Fits

**Phase 3 specifically.** The genome is a routing policy — a mapping from context features to model choices:

```python
context_features = {
    "task_description": str,
    "recent_tool_calls": list[str],
    "turn_number": int,
    "cached_prefix_tokens": int,
    "cache_ttl_remaining": float,
    "session_cost_so_far": float,
    "output_tokens_last_turn": int,
    "tool_result_size_last_turn": int,
}

# Fitness function
def fitness(policy, sessions):
    return alpha * task_success_rate(policy, sessions) \
         - beta  * total_cost(policy, sessions)
```

Evolution searches over policies that dominate the Pareto frontier of `(task_quality, session_cost)`. A policy that routes background agents to Haiku + switches to Sonnet during exploration phases + uses Opus only for planning/debugging could hit 40-60% cost reduction with minimal quality loss — discovered by evolution rather than hand-crafted.

---

## 5.7 Deeper Notes on the Research Sequence

Beyond the slide, these are the second-order considerations that matter when actually executing the roadmap.

### 5.7.1 Phase 2 (Cache-Aware) vs Phase 4 (Phase-Based) Are Complementary, Not Overlapping

A natural question: doesn't phase-based routing (Phase 4) already need cache-aware logic, making Phase 2 redundant?

**No — they're different layers.** Phase 2 is a *constraint*; Phase 4 is a *policy*.

| Phase | Type | Produces | Fires |
|---|---|---|---|
| Phase 2 | Constraint (predicate) | `should_switch(...) → bool` | Every switch consideration |
| Phase 4 | Policy (proposal) | `proposed_model(phase, features) → model` | At phase transitions |

The integrated router composes them:

```python
proposed = phase_router(current_phase, features)         # Phase 4 proposes
if proposed != current:
    if cache_constraint.allows(switch_to=proposed,
                                cache_state=state,
                                horizon=remaining_turns):  # Phase 2 evaluates
        switch_to(proposed)
    else:
        stay_on(current)  # cache penalty exceeds savings
```

**Why they have to be separate research phases:**

| Reason | Why |
|---|---|
| Different intellectual contributions | Phase 2 is a *formalization* (math + paper). Phase 4 is *systems engineering* (phase detector + integration). |
| Independent risk profiles | Phase 2 might fail to find a publishable result. Phase 4 might fail at integration. They fail independently. |
| Phase 2 enables Phase 4 safely | Without the cache constraint, Phase 4 thrashes. Phase 2 is the safety predicate. |
| Phase 4 strengthens the Phase 2 paper | The constraint is much more credible when validated by a working system. |

> **One sentence:** Phase 2 is the *gate*; Phase 4 is the *driver*. The production router is both, composed.

### 5.7.2 Phase 5 (Compaction Quality) — What's Actually Being Studied

**The core hypothesis:**
> Compaction with a cheaper model loses subtle information that becomes load-bearing 20-50 turns later. The savings on the compaction event itself are eaten by downstream re-reads, re-greps, and forgotten constraints.

**Measurable downstream signals:**

| Metric | What it captures | How to measure |
|---|---|---|
| Re-read rate | Model reads files it already read | Count `Read` calls on paths in pre-compaction history |
| Re-grep rate | Model searches for things already searched | Count `Grep` calls with overlapping queries |
| Re-prompt rate | User clarifies same point twice | Detect repeated user clarifications across compaction boundaries |
| Task completion rate | Did the original goal get achieved? | Manual labels or LLM-judged on holdout |
| Turns-to-completion | How many more turns after compaction to finish | Direct count |
| Cost-to-completion | Total session cost goal-stated → goal-achieved | Sum from cost-tracker |

**The hard part — confounders:**
- Task type (debugging vs exploration vs implementation)
- Conversation length pre-compaction
- Number of tool results in pre-compaction span
- User style (terse vs verbose)

The study needs stratified sampling, matched session pairs, ideally a held-out user pool.

**Possible surprising findings:**

| Finding | Implication |
|---|---|
| Haiku compaction is 95% as good for 5% of the cost | Production should default to Haiku |
| Haiku catastrophically fails on debugging specifically | Per-phase compaction routing (needs Phase 4) |
| Sonnet is the sweet spot everywhere | Single fixed choice; no routing needed for compaction |
| Compaction-model choice doesn't matter much | Publishable null result — still valuable |

### 5.7.3 Adaptive Compaction — The Four Axes of "Adaptive"

Current Claude Code compaction:
```
if tokens > 187K: compact_everything_with_main_model()
```

That's a 1-bit decision. Adaptive compaction varies along **four axes** simultaneously:

| Axis | Today | Adaptive |
|---|---|---|
| **When to trigger** | Token threshold (187K) | Task boundary, phase transition, OR threshold |
| **What to compress** | Everything pre-boundary | Selective: keep code verbatim, summarize prose; keep recent, compress old |
| **How much to compress** | Down to ~20K | Variable — 50K for debugging context, 5K for exploration |
| **Which model compresses** | Main loop model | Per-span: structural summary on Haiku, semantic on Sonnet |

**Example adaptive policy:**

```python
def adaptive_compact(conversation, phase, token_pressure):
    # Step 1: Haiku segments and classifies the conversation
    spans = haiku.segment(conversation)  # ~$0.01

    # Step 2: Per-span compression decision
    plan = []
    for span in spans:
        if span.contains_active_code():
            plan.append((span, "preserve_verbatim"))
        elif span.is_exploration_phase() and phase != "debugging":
            plan.append((span, "aggressive_summary"))   # Haiku, ~2K tokens
        elif span.is_recent(within_turns=10):
            plan.append((span, "light_summary"))         # Sonnet, ~5K tokens
        else:
            plan.append((span, "medium_summary"))         # Sonnet, ~3K tokens

    # Step 3: Execute plan with appropriate models per action
    return execute_compression_plan(plan)
```

**Why Tier 2, not Tier 1:** adaptive compaction requires the phase detector from Phase 4 to be reliable. Sequences after Phase 4.

### 5.7.4 Phase 3 — The Real Research Question Is the Training Data

The classifier is the visible artifact. The interesting research is one layer below: **where do the training pairs `(task → optimal_model)` come from?**

| Data source | Method | Pros | Cons |
|---|---|---|---|
| Offline experiments | Run same tasks on Haiku/Sonnet/Opus, measure success | Clean labels | ~3× training cost |
| Synthetic labeling | Opus labels task complexity → infer model | Fast, scalable | Opus may over-recommend Opus (bias) |
| Production trace mining | Filter real sessions by outcome (tests passed) | Free, real distribution | Hard to attribute failure to model |
| Bandit / online RL | Learn from sparse rewards in production | Self-improving | Slow convergence; needs traffic |

**Best path: hybrid.** Bootstrap with synthetic labels → validate on offline-experiment data → refine with production-trace mining.

**Why EA fits here:**
- Discrete decision space (model choice is categorical)
- Sparse rewards (task success is end-of-trajectory)
- Multi-objective (cost + quality + latency)
- No gradient available (API costs aren't differentiable)

**Failure mode to plan for:** classifier overfit to seen task types. Need stratified test sets by *task type*, not by sample.

### 5.7.5 Background Workers — "Cheapest Sufficient" vs "Cheapest"

Three of the four background workers are clearly Haiku-appropriate. **PromptSuggestion is the interesting case:**

| Worker | Difficulty | Haiku-sufficient? |
|---|---|---|
| extractMemories | Low — extraction + formatting | ✅ Yes |
| SessionMemory | Low — append + categorize | ✅ Yes |
| AgentSummary | Low — purely structural | ✅ Yes |
| PromptSuggestion | Medium — needs natural phrasing, user-visible | ⚠️ Borderline |

**So Phase 1 isn't "switch all four to Haiku" — it's a mini-study:**

1. Switch the three structural workers to Haiku immediately (zero risk, full savings).
2. A/B test PromptSuggestion: Haiku vs Sonnet, measure user acceptance rate of suggestions.
3. Pick the **cheapest sufficient** model per worker.

**The defensible research thesis:**
> *"Don't route to the cheapest model. Route to the cheapest **sufficient** model. The boundary of 'sufficient' is what we measure empirically."*

This is a stronger framing than "Haiku for everything."

### 5.7.6 Missing from the Slide — Six Open Questions Beyond the Roadmap

Cross-cutting concerns not on the slide that matter when actually executing:

| # | Concern | Why it matters |
|---|---|---|
| **1** | **Latency ≠ Cost** | Haiku is cheaper *and* faster. But cache misses add latency. Routing affects both axes — the roadmap slide only addresses cost. |
| **2** | **A/B testing infrastructure** | How to validate routing without disrupting users? Shadow mode? Held-out user pools? Consent process? Must exist before Phase 1 ships. |
| **3** | **Rate limit handling** | Haiku/Sonnet/Opus have different rate limits. A router that hits a 429 on Haiku and falls back to Sonnet bills *more* than expected. The router itself must handle 429s gracefully. |
| **4** | **Quality measurement (Phase 0)** | All quality-related phases (2-5) need a measurement substrate. Task success classifiers, re-read detectors, user-acceptance loggers. Build this first. |
| **5** | **Tool selection routing** | Beyond model routing, *which tools* to surface per turn is another routing surface. Tension between cache-stable (sorted) pool vs dynamic per-turn pool. Not in the current roadmap. |
| **6** | **User preference signal** | Some users prefer fast over high-quality. Where does user preference enter the routing decision? Per-user policy vs single global? |

**Key takeaway:** These don't change the roadmap. They shape the execution. Routing isn't just model selection — production reality adds latency, validation, rate limits, measurement, tool surfaces, and user preference.

**Recommendation:** Add an implicit **Phase 0 — Build measurement infrastructure** before Phase 1. Otherwise every phase fails its own validation.

---

# Part 6 · Glossary & References

## 6.1 Glossary

| Term | Definition |
|---|---|
| **Agentic system** | An LLM-driven loop that perceives, reasons, acts, and observes — iterating toward a goal. |
| **Compensate** | Harness work that fills in for model weaknesses (memory, context, cost, parallelism). Depreciates as models improve. |
| **Constrain** | Harness work that prevents the model from taking dangerous actions. Never depreciates. |
| **Context window** | The maximum input tokens a model can attend to in a single call. |
| **Deny-first** | Permission model where every action is denied by default and must be explicitly allowed. |
| **Forked agent** | A background process that inherits parent's conversation state and makes its own API calls. |
| **Harness** | All the engineering scaffolding around the model — context, memory, permissions, loop, tools. |
| **KV cache** | Server-side cache of precomputed Key/Value attention tensors. Enables 10× discount on cached tokens. |
| **MCP** | Model Context Protocol — Anthropic's open standard for pluggable external tools. |
| **PromptCache** | The 10×-cheaper rate for input tokens that match a previously-sent prefix. |
| **ReAct** | Reason + Act prompting pattern. The model alternates between reasoning and tool calls. |
| **Routing seam** | A point in the codebase where a model selection or behavior decision can be intercepted. |
| **Session** | A single Claude Code invocation, identified by UUID. Cost-accounting boundary. |
| **SFT** | Supervised Fine-Tuning. Teaches the model to imitate good trajectories via cross-entropy. |
| **Transcript** | The append-only `.jsonl` log of every message in a session. |
| **Trajectory** | A full sequence of `(thought, tool_call, result, ...)` from goal to outcome. |

## 6.2 Key Source Code Pointers

All references point to `claude-code-source/claude-src-code/src/`:

| Concept | File | Line |
|---|---|---|
| Session ID generation | `bootstrap/state.ts` | 331 |
| Main model resolution | `utils/model/model.ts` | 145 |
| Subagent model | `utils/model/agent.ts` | 37 |
| Cache control headers | `services/api/claude.ts` | 358 |
| Cache break detection | `services/api/promptCacheBreakDetection.ts` | 334 |
| API call entry point | `services/api/claude.ts` | 1822 |
| Cost tracking | `cost-tracker.ts` | 278 |
| Autocompact threshold | `services/compact/autoCompact.ts` | 72 |
| Background memory extraction | `services/extractMemories/extractMemories.ts` | 49 |
| Message accumulation | `query.ts` | 1716 |
| QueryEngine entry | `QueryEngine.ts` | 209 (`submitMessage`) |
| Per-iteration model resolution | `query.ts` | 572-578 |

## 6.3 Reference Materials

### VILA-Lab "Dive into Claude Code"
- arXiv: 2604.14228
- Repo: github.com/VILA-Lab/Dive-into-Claude-Code
- Cited number: **98.4% deterministic infrastructure / 1.6% AI decision logic**
- Architectural decomposition we use throughout

### Hermes Agent
- NousResearch/hermes-agent (MIT license, v0.13.0)
- Python (~780K lines) + TypeScript TUI (~57K lines)
- Notable files: `run_agent.py` (main loop at line 11844), `context_compressor.py` (1,556 LOC), `credential_pool.py` (1,603 LOC)

### Buildable Research Forks
- T-Lab-CUHKSZ/claude-code — CUHK buildable research fork
- ultraworkers/claw-code — Rust reimplementation (~20K lines vs 512K TS)
- 777genius/claude-code-working — Runnable reverse-engineered CLI

### Practitioner / Ecosystem Projects (productivity layers on top of CC)
- **affaan-m/everything-claude-code** — `https://github.com/affaan-m/everything-claude-code` — 60+ agents, 228+ skills, hooks, MCP configs. Distribution-layer productivity tool, not internals analysis. Notable signals: corroborates MCP accumulation as a cost driver ("keep under 10 MCPs"); confirms hooks-based memory persistence as practitioner workaround; potential corpus for Phase 3 (task → specialized agent) training data.
- **safishamsi/graphify** — `https://github.com/safishamsi/graphify` — Knowledge-graph builder + MCP server for codebases (tree-sitter 29 langs), docs, videos, PDFs. Exposes `query_graph` / `get_node` / `get_neighbors` / `shortest_path` to agents. **Structural intervention, not routing** — reduces tool-result tokens by replacing `Read`/`Grep` iteration with structured graph queries. Estimated 5-30% reduction on navigation-heavy workloads (unverified; no published benchmarks). Belongs in the "design fix" tier alongside microcompaction. Composable with routing.

### Key Blog Posts
- ClaudeCodeCamp — "How Prompt Caching Actually Works in Claude Code"
- George Sung — "Tracing Claude Code's LLM Traffic" (discovered dual-model usage)
- MindStudio — "Claude Code Source Leak: Memory Architecture"
- Agiflow — "Reverse Engineering Prompt Augmentation"

---

## 6.4 Pricing Reference

| Model | Input ($/Mtok) | Output ($/Mtok) | Cache Read ($/Mtok) | Cache Write ($/Mtok) |
|---|---|---|---|---|
| Haiku 4.5 | $1 | $5 | — | — |
| Sonnet (all) | $3 | $15 | $0.30 | $3.75 |
| Opus 4.6 | $5 | $25 | — | — |
| Opus 4.6 Fast | $30 | $150 | — | — |
| Opus 4/4.1 | $15 | $75 | — | — |

---

## 6.5 Key Numbers to Remember

```
Codebase size:
  Claude Code:  ~512K LOC TypeScript
  Hermes:       ~780K LOC Python + 57K TS
  OpenClaw:     ~20K LOC Rust

Claude Code's 98.4% / 1.6% split:
  98.4% deterministic infrastructure
   1.6% AI decision logic

Context windows:
  Default:    200K tokens
  1M variant: 1M tokens (Opus/Sonnet 4.6 [1m])

Compaction:
  Autocompact triggers at:  contextWindow - 13K tokens
  Hard block at:             contextWindow - 3K tokens
  Max compaction output:     20K tokens
  Circuit breaker:           3 consecutive failures

Costs (rule of thumb):
  Cache miss penalty (25K prefix, Opus):  ~$0.34 per bust
  Compaction event (Opus):                 ~$2.63 - $3.75
  Background agent cost reduction (→ Haiku): ~80% per call
  Subagent vs SkillTool overhead:          ~7× more tokens
  Phase 1 routing savings:                 ~20-25%
  Potential total reduction:               ~40-60% with minimal quality loss

Hermes:
  Providers supported:        15+
  Memory plugin providers:    8
  Gateway platforms:          6
  Max subagent spawn depth:   3
  Default iteration budget:   90
  Compression trigger:        50% of context window
```

---

*This document is a living reference. Sections will be expanded as new questions come up.*
*Last updated: 2026-05-12*
