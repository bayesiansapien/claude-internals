# Claude Code Optimizer

A toolkit of **slash commands** and **hooks** that gives you visibility into
where your Claude Code session's tokens and dollars go, plus proactive
guidance on when to compact, when to switch models, and when to disconnect
unused MCP servers.

Everything reads local session state from `~/.claude/projects/...`. Most
features cost $0 to run; two optional tiers in the compact advisor use
Haiku/Sonnet at predictable, capped cost.

---

## Why

Claude Code is great, but it gives you almost no visibility into:

- Where your token spend is going each session (system prompt? conversation? MCP?)
- Whether your current cache prefix is bloated and worth compacting
- What MCP servers are silently inflating every API call
- When edits to `CLAUDE.md`, memory, or `/model` switches blow the cache
- Whether a cache-bust has been "recovered" through cache savings yet

This toolkit closes that gap. Every component answers a specific decision the
user has to make, with exact numbers from Anthropic's own tokenizer where
possible.

---

## Quick install

```bash
# Bundle everything portable
cd /path/to/your/project
tar -czf claude-optimizer.tar.gz claude-optimizer/ .claude/

# In a new project:
tar -xzf claude-optimizer.tar.gz
# Restart Claude Code → hooks pick up automatically
```

**Dependencies:**

| Required | What for |
|---|---|
| Python 3.9+ | Every script is Python; stdlib only — no pip install |
| OS credential store | `/mcp-audit` and `/cache-bust-advisor` read your Claude OAuth token from it |

**Cross-platform support:**

The toolkit runs on macOS, Linux, and Windows. The two OS-specific concerns are handled by `lib/platform_compat.py`, which tries each platform's native mechanism and falls back gracefully:

| Concern | macOS | Linux | Windows |
|---|---|---|---|
| OAuth token read | `security` (Keychain) | `secret-tool` (libsecret) or `keyring` lib | PowerShell `Get-StoredCredential` or `keyring` lib |
| Terminal auto-launch (session-launcher) | iTerm2 or Apple Terminal via `osascript` | gnome-terminal, kitty, konsole, or xterm | Windows Terminal (`wt.exe`) or `cmd` |

If any of the above isn't available, the affected feature degrades to "print the command for the user to paste." The core hooks (`prefix_monitor`, `compact_advisor`, `session_boundary_advisor`) and the read-only slash commands (`/cost-snapshot`, `/compact-suggest`, `/session-status`, `/memory-hygiene`) don't need any of this — they work on any OS out of the box.

**Optional:**

| Optional | Unlocks |
|---|---|
| `pip install keyring` | Most reliable OAuth-token retrieval across all 3 OSes (otherwise toolkit falls back to native commands) |
| `ANTHROPIC_API_KEY` env var | T3 Sonnet judge in compact advisor |
| `claude.ai` OAuth (auto-present if logged in) | Exact MCP token counts, exact anatomy via Anthropic's `count_tokens` |

Everything degrades gracefully if any optional is missing.

---

## File structure

```
claude-optimizer/
├── lib/                              Shared Python library
│   ├── anatomy.py                    Prefix anatomy + projection helpers
│   ├── compact_decision.py           Unified compact scorer (used by skill + hook)
│   ├── task_hierarchy.py             Boundary signal extraction
│   ├── cache_bust_state.py           Bust event state (per-session)
│   ├── transcript.py                 Session jsonl parser
│   ├── pricing.py                    Anthropic pricing constants
│   └── judges/
│       ├── haiku_judge.py            T2 — Haiku tiebreaker
│       └── main_model_judge.py       T3 — Sonnet structured verdict
├── scripts/                          Standalone analysis scripts
│   ├── compact_suggest.py            Flagship: anatomy + projection + verdict
│   ├── cost_snapshot.py              Per-source cost breakdown
│   ├── mcp_audit.py                  Exact MCP token counts
│   ├── memory_hygiene.py             Auto-memory dir audit
│   └── cache_bust_advisor.py         Proactive cache-bust recommendation
├── hooks/                            Auto-firing on CC lifecycle events
│   ├── cache_bust_warner.py          PreToolUse on Bash|Edit|Write
│   ├── prefix_monitor.py             Stop event — prefix bar + cache-bust recovery
│   └── compact_advisor.py            Stop event — auto compact banner
└── README.md                         This file

.claude/
├── commands/                         Slash commands registered with CC
│   ├── compact-suggest.md
│   ├── cost-snapshot.md
│   ├── mcp-audit.md
│   ├── memory-hygiene.md
│   └── cache-bust-advisor.md
└── settings.json                     Hook registration + permission rules
```

---

## Slash commands

Every command is a thin `.md` file in `.claude/commands/` that runs the
corresponding `scripts/*.py`. Invoke with `/<command-name>`.

### `/compact-suggest` — The flagship view

**Purpose:** Tell me everything about my current context state and whether I
should `/compact` now.

**Returns:**
- Prefix size + % of context window + current $/turn burn rate
- **ANATOMY** — where every prefix token lives (system breakdown + conversation breakdown)
- **PROJECTION** — growth rate, headroom in turns, projected $ savings if I `/compact` now
- **BOUNDARY ANALYSIS** — T1 deterministic + T2 Haiku tiebreaker + T3 Sonnet judge (graduated cost)
- **VERDICT** — `COMPACT_NOW` | `SOON` | `WAIT` | `NO_ACTION` + reason

**Sample output:**

```
COMPACT DECISION · current session
═══════════════════════════════════════════════════════════════
Prefix:  486,371 / 1,000,000 tokens  (48.6%)
Model:   claude-opus-4-7
Burn:    $0.243/turn  ·  growth +2,540 tokens/turn

ANATOMY  (where the 486K prefix tokens live)
  Conversation: 449,188                              92.4%
    Assistant responses              158K  32.6%
    Tool calls (assistant)            93K  19.2%
    Attachments                       83K  17.1%
    Tool results                      74K  15.2%
    User messages                     24K   5.0%
  System: 37,183                                      7.6%
    CLAUDE.md hierarchy (1 files)     17K   3.5%   edit files
    Auto-memory (8 files)              5K   1.0%   /memory-hygiene
    CC core + built-in tools + skills  4K   0.7%   (locked)
    MCP schemas (0 servers)            0K   0.0%   /mcp-audit

PROJECTION  (based on trailing 15 turns)
  Growth rate:                 +2,540 tokens / turn
  Headroom to auto-compact:    480,629 tokens (189 turns away)
  Current cost / turn:         $0.243
  Cost from NOW to auto-compact: $87.34

  If /compact NOW:
    One-time cost:             $0.74
    Per-turn cost after:       $0.024
    Projected savings:         $82.10 ✓

BOUNDARY ANALYSIS  (is this a good moment to compact?)
  T1 score:                    7/10
  Signals:                     discussion_turn=+2, topic_shift=+3, idle_pause=+2
  T2 Haiku tiebreaker:         skipped (score unambiguous)
  T3 Sonnet judge:             skipped (pressure under 85%)

VERDICT: ⚠ SOON
  Reason:  Boundary + economic gain (~$82)
  Economic outlook: /compact now would save $82.10 long-term.

→ Run /compact when ready.
```

**Cost:** T1 free; T2 ~$0.001/call (only fires for ambiguous scores 4–6);
T3 ~$0.05/call (only fires at pressure ≥85% AND ambiguous).

---

### `/cost-snapshot` — Per-source cost breakdown

**Purpose:** Where did my session $ go? Broken out by message source.

**Returns:** Table with main loop, auto-memory, subagents (if any), tool call
volume, cost share. Also shows the cost-by-component lens (cache reads vs
writes vs output).

**Sample output:**

```
COST SNAPSHOT · current session (fd9d6977…)
──────────────────────────────────────────────────────
Category                Msgs   Out   Cache_R    Cost  Share
──────────────────────────────────────────────────────
main loop               810  1.5M   342M       $302  98.5%
auto memory             15   29K    7M           $5   1.5%
──────────────────────────────────────────────────────
TOTAL                   825                    $307 100%

COST BY COMPONENT
Cache reads (re-sending prefix)    $173  56.9%
Cache writes (cache rebuilds)       $93  30.5%
Output tokens (model responses)     $38  12.6%
New input tokens                     $0   0.0%

INSIGHTS
  • Cache reads dominate — prefix is large
  • Cache writes are high — check for cache busts
```

**Args:** `--all` to aggregate across all sessions, `--session <uuid>` for a
specific session.

---

### `/mcp-audit` — Exact MCP token costs

**Purpose:** Which MCP servers are eating tokens in my prefix, and how much?

**How it works** (the only skill with a multi-step pipeline):
1. Pulls your `claude.ai` OAuth token from macOS Keychain
2. Calls `api.anthropic.com/v1/mcp_servers` for connected server URLs
3. For each server: queries the MCP `tools/list` JSON-RPC endpoint for real schemas
4. Sends schemas to `/v1/messages/count_tokens` for exact counts
5. Compares "current state" (auth-stub tokens) vs "post-auth state" (full schemas)

**Sample output:**

```
MCP SERVERS · 5 connected
══════════════════════════════════════════════════════════════
Server                   Real  Current  Post-auth  Tools  Status
Notion                    0     845         —        —    ⚠ WAF-blocked
Atlassian Rovo            0     864         —        —    ⚠ WAF-blocked
Google Drive              0     858     4,283       8    ✓ fetched
Google Calendar           0     861     8,573       8    ✓ fetched
Gmail                     0     855     7,166      12    ✓ fetched
──────────────────────────────────────────────────────────────
TOTAL                     0    4,283   21,731

💰 COST IMPACT (this session)
   Tokens added to every API call: ~4,283
   API calls so far in session:    889
   Already spent on MCP schemas:   ~$1.90

   If you authenticate all servers:
   Overhead jumps to:              21,731 tokens per call
   Would have cost this session:   ~$9.66

💡 RECOMMENDATION
   5 server(s) dormant this session — disconnect any you don't use.
   → claude.ai → Settings → Connectors → toggle off
   → quit Claude Code (Cmd+Q) and reopen
```

**Why this matters:** Most users don't know that connected-but-unused MCP
servers add ~500–4,000 tokens to **every** API call. Across hundreds of calls
per session, that's $1–$10 wasted per session.

---

### `/memory-hygiene` — Auto-memory dir audit

**Purpose:** Is my `~/.claude/projects/<hash>/memory/` directory bloated?

**Returns:** File-by-file size, line count, age, flags (large, stale,
redundant). Plus total token estimate of what gets loaded into every session.

**Sample output:**

```
MEMORY HYGIENE
File                              Size  Lines  Age  Flags
MEMORY.md                         829B    7    3d
user_profile.md                  1.0K   14    4d
feedback_answer_style.md          990B   20    3d
reference_ecosystem.md           5.5K  100    3d   ⚠ large (>5KB)
[...]

Total: 8 files · 12,908 bytes · ~3,227 tokens
Per-session impact: ~3,227 tokens loaded every session
```

**Note:** Memory dir is loaded into every CC session's system prompt. Each
byte costs you across all future sessions until the file is edited or removed.

---

### `/cache-bust-advisor <description>` — Proactive bust check

**Purpose:** "I'm thinking about doing X. Is now a good moment?"

**Inputs:** Free-text description of planned change. Examples:
```
/cache-bust-advisor switch to sonnet
/cache-bust-advisor edit CLAUDE.md to tighten style
/cache-bust-advisor add coding preference to memory
```

**Auto-classifies** into: `model_switch` | `claude_md_edit` | `memory_edit` |
`mcp_change` | `generic`.

**Returns:**
- Current prefix size + exact rebuild cost (cache-write rate)
- **Every signal explained** — what fired (GO/WAIT), what didn't, with the
  actual values/thresholds and why
- Action-type context note
- Verdict: `GO` | `WAIT` | `MIXED` | `URGENT`
- Recent bust history (if any)

**Sample output:**

```
CACHE-BUST ADVISOR
═══════════════════════════════════════════════════════
Planned change:  switch to sonnet
Classified as:   model switch

Current cached prefix:  486,371 tokens (48.6% of window)
Model:                  claude-opus-4-7
Rebuild penalty (cache_write rate): $3.04
Worst-case (no cache write): $2.43

BOUNDARY ANALYSIS  (every signal explained)
✓ FIRED — Reasons it's OK:
    • Velocity slowing
      input_tokens growth slowing across last 4 turns

⚠ FIRED — Reasons to WAIT:
    • New file just read
      files read in last 3 turns not seen earlier

◌ NOT fired (here's why):
    • Rapid tool activity: tools in last 3 turns = 6; threshold = 15.
    • Unresolved tool errors: no tool errors detected.
    • Topic shift: overlap with last 5 user prompts: 44.1%
      (threshold < 20%) — topic stable.
    • Idle pause: 2 min since last user message; threshold > 5 min.

Action context: Model swap = totally fresh KV cache. Best done
at a clear handoff (planning → implementation, etc.).

VERDICT: ◐ MIXED — 1 GO vs 1 WAIT — your call based on signals above
```

**Cost:** Free (deterministic + optional OAuth for prefix size).

---

## Hooks (auto-firing)

Hooks are registered in `.claude/settings.json`. They run automatically on
CC lifecycle events; no user invocation needed.

### `cache_bust_warner.py` — PreToolUse on Bash|Edit|Write

**Fires when** Claude is about to execute an action that invalidates the
prompt cache:
- Bash with `/model` (model switch)
- Edit/Write to `CLAUDE.md`
- Edit/Write to a memory file

**What it does:**
1. Estimates the $ penalty of re-caching the prefix (current prefix × model's cache-write rate)
2. Reads session signals (same as `cache-bust-advisor`)
3. Generates context-aware warning text with timing recommendation (GO/WAIT/MIXED)
4. **Records the bust event** to `~/.claude/cache-bust-events.json` for recovery tracking
5. Emits the warning via `systemMessage` (visible in terminal) and stderr (visible in tool output)

**Limitations:**
- In `bypassPermissions` mode, the warning shows AFTER the edit, not before
- In `default`/`acceptEdits` modes, the warning shows alongside the diff prompt
- For maximum protection, the `.claude/settings.json` `ask` rules also force
  prompts on these actions even in bypass mode (see Settings below)

---

### `prefix_monitor.py` — Stop event

**Fires** at the end of every Claude turn.

**Renders** a one-line status banner:

```
📊 prefix: 486K / 1,000K (48.6%) █████████░░░░░░░░░░░ · 480K to compact
```

**Plus** — if a cache bust happened recently — appends a recovery line in one of three states:

```
In progress:   ♻ model switch: [███░░░░░░░] $0.23/$0.73 (31%) · ~2 more user turns to recover
Recovered:     ♻ model switch: [██████████] ✓ RECOVERED ($0.73 rebuilt · earned back in 3 user turns)
After:         (silent — no line shown until the next bust)
```

**The economic framing.** A cache bust spends real money (rebuild cost) up
front. That money is "recovered" through cache-read discount on every
subsequent turn — each `cache_read` token costs `cache_read_rate` instead of
full `input_rate`. The savings per turn is `cache_reads × (input_rate − cache_read_rate)`
on the new model. Once cumulative savings exceed the rebuild cost, the
investment has paid for itself.

This is direction-agnostic: switching cheap → expensive still recovers
(cache discount on the new model pays back the rebuild) — the question is
*how long it takes*, not *whether* it eventually does.

**Why "user turns" instead of "API calls".** One user message can trigger
many API iterations (tool loops). Counting iterations would inflate the
"turns to recover" number and confuse the user. Real user turns = real
interactions, which is how humans think about pacing their switches.

**Rolling projection.** While in progress, the banner shows `~N more user
turns to recover` based on the current recovery rate. This is a rolling
estimate that narrows as more turns accumulate — **not a prediction of
the future**. Each turn updates the rate, so the estimate gets tighter
over time.

**Auto-detection of `/model` switches.** The `/model` slash command is a CC
built-in (not a tool call), so `cache_bust_warner` can't intercept it. Instead,
`prefix_monitor` walks the session jsonl looking for cache rebuild spikes
(large `cache_creation_input_tokens` after a warm-cache run) and auto-records
the bust event. Model switches and any other untracked cache rebuilds appear
in the recovery line automatically.

**Filtering false positives.** `latest_confirmed_bust()` only returns events
with `actual_rebuild_cost ≥ $0.02` (~5K tokens at Sonnet rate). Per-turn
incidental cache writes and test/fake events fall below this threshold and
are ignored.

**State-file hygiene.** Every load/save runs a GC pass that:
- Drops `/tmp/*` test-session keys (smoke tests can't pollute production state)
- Drops phantoms (`actual < $0.02`) older than 1h
- Drops orphans (recovered=$0, not resolved, not the latest event) older than 1h —
  these are busts that got superseded by a newer bust before recovery could
  accumulate
- Sorts events by `ts` (chronological), so `reversed(events)` walks
  newest-by-time first (not newest-by-insertion-order)
- Clamps over-accumulated `recovered` values to `actual_rebuild_cost`

**Auto-detector guards** (in `prefix_monitor._auto_detect_busts`):
- Skips spikes older than 24h (no resurrection of ancient busts on resume)
- Skips spikes at `ts ≤ latest_resolved_event.ts` (no orphan-insertion into the
  session's "resolved past")
- ±600s dedup window against existing events
- Skips if computed `actual_rebuild_cost < $0.02`

**Why it matters:** A cache bust is an investment. The recovery tracker tells
you whether enough conversation has happened on the new model for the cache
discount to pay back the rebuild cost. **Switch again before recovery and
you forfeit the unrecovered portion.**

---

### `compact_advisor.py` — Stop event

**Fires** at the end of every Claude turn.

**Runs** the same `compact_decision()` function that powers `/compact-suggest`,
but renders the verdict as a one-line banner only when actionable.

**Behavior:**
- Below 50% pressure → silent (no analysis worth doing)
- Score below fire threshold → silent
- Anti-spam: max 1 banner per 5 turns (per session)
- Sonnet T3 veto → suppresses banner even if T1 score was high

**Sample banner:**

```
⚠ Compact advisor [T1+T2]: Boundary + economic gain (~$82) (49% used) · ~$82 savings
```

State file: `~/.claude/compact-advisor-state.json` tracks anti-spam state
and history per session.

---

## Settings (`.claude/settings.json`)

The settings file wires up hooks and adds permission guardrails that work
**even in bypass mode**.

```json
{
  "permissions": {
    "deny": [
      "Bash(rm -rf /:*)", "Bash(rm -rf /*)",
      "Bash(rm -fr /:*)", "Bash(rm -fr /*)",
      "Bash(rm -rf ~:*)", "Bash(rm -rf $HOME:*)",
      "Bash(sudo rm:*)", "Bash(sudo rmdir:*)",
      "Bash(dd if=*of=/dev/*)",
      "Bash(mkfs:*)", "Bash(mkfs.*:*)",
      "Bash(chmod -R 000 /:*)"
    ],
    "ask": [
      "Bash(rm:*)", "Bash(rm -r:*)", "Bash(rm -rf:*)",
      "Bash(rm -f:*)", "Bash(rm -fr:*)",
      "Bash(rmdir:*)", "Bash(unlink:*)", "Bash(trash:*)",
      "Bash(find * -delete:*)", "Bash(find * -exec rm:*)",
      "Bash(claude /model:*)", "Bash(/model:*)"
    ]
  },
  "hooks": {
    "PreToolUse": [
      { "matcher": "Bash|Edit|Write", "hooks": [{ "type": "command",
        "command": "python3 $CLAUDE_PROJECT_DIR/claude-optimizer/hooks/cache_bust_warner.py" }] }
    ],
    "Stop": [
      { "hooks": [
        { "type": "command", "command": "python3 $CLAUDE_PROJECT_DIR/claude-optimizer/hooks/prefix_monitor.py" },
        { "type": "command", "command": "python3 $CLAUDE_PROJECT_DIR/claude-optimizer/hooks/compact_advisor.py" }
      ] }
    ]
  }
}
```

**Key behaviors:**
- `deny` rules **hard-block** catastrophic commands even in bypass mode
- `ask` rules **force a Y/N prompt** for any delete or model-switch command,
  even in bypass mode
- Hooks fire on every matching event

---

## Lib internals (for contributors)

### `lib/anatomy.py`

Pure helpers for prefix anatomy and projection. No decision logic.

| Function | Returns |
|---|---|
| `get_oauth_token()` | claude.ai OAuth token from Keychain |
| `count_tokens_text(token, text)` | Exact token count via `/v1/messages/count_tokens` |
| `count_tokens_tools(token, tools)` | Exact token count for a tool list |
| `fetch_mcp_tools_via_anthropic(token)` | `{server_name: [tool_schemas]}` |
| `find_claude_md_files()` | List of CLAUDE.md hierarchy files |
| `find_memory_files()` | List of auto-memory files |
| `walk_conversation(session_path)` | `(buckets, first_usage, last_usage, model)` |
| `compute_anatomy(token=None)` | Full system + conversation breakdown |
| `compute_projection(window, current_total, model=None)` | Growth + economic projection (model-aware) |
| `get_context_window(model)` | Model's max context size — API-backed with cache |
| `get_model_rates(model)` | Per-million-token USD rates: `input`, `output`, `cache_read`, `cache_write` |

**`get_context_window` resolution order:**

1. Local cache (`~/.claude/model-window-cache.json`, 24h TTL)
2. Live `GET /v1/models/{model_id}` from Anthropic → reads `max_input_tokens`
3. Substring pattern fallback (`opus-4` / `sonnet-4` → 1M, `haiku-4` → 200K)
4. Default: 200K

New models work automatically — the API call returns the correct window size
and caches it. No code changes needed for `claude-haiku-4-6`, `sonnet-4-7`,
or future releases.

### `lib/compact_decision.py`

The single source of truth for "should I compact?". Used by both
`/compact-suggest` and `compact_advisor` hook.

```python
def decide_compact(run_t2=True, run_t3=True, min_warmup_turns=5):
    """Returns a dict with: anatomy, projection, signals, boundary score,
    tiers_run, T2/T3 verdicts, final verdict + reason."""
```

Tiered evaluation:
- **T1** (free, always): pressure gate (skip if < 50%) → deterministic boundary score
- **T2** (~$0.001/call, only if score 4–6 ambiguous): Haiku topic-continuity tiebreaker
- **T3** (~$0.05/call, only if pressure ≥ 85% AND ambiguous): Sonnet structured verdict
- **Verdict synthesis**: combines pressure + score + projected savings + tier verdicts

### `lib/task_hierarchy.py`

Extracts boundary signals from session jsonl (no API calls):
- Total turns, tools per turn, tools last 3 turns
- Idle minutes since last user message
- Topic shift (current vs union of last 5 user prompts, with overlap %)
- Velocity (input_tokens delta trend)
- Macro-task keywords (from first 3 prompts)
- Info-loss flags: new file read, new skill loaded, recent tool errors

### `lib/cache_bust_state.py`

Per-session bust event log: `~/.claude/cache-bust-events.json`.
Used by `cache_bust_warner` (writes events) and `prefix_monitor` (reads for
recovery tracking, also auto-records busts from `/model` switches it detects
via cache_creation spikes).

Every `_load()` / `_save()` runs `_gc()` — drops `/tmp/*` keys, phantoms,
orphans; sorts by `ts`; clamps over-accumulated recovered values. See the
"State-file hygiene" callout above for details.

Key functions:
| Function | Returns |
|---|---|
| `record_bust(..., ts=None)` | Append new bust event; `ts` defaults to `time.time()` but auto-detector passes the historical spike timestamp. Rejects `/tmp/*` session keys silently. |
| `latest_confirmed_bust(key)` | Most recent real bust (≥$0.02) by `ts`, or `None` if already recovered → drives the silent-after-recovery behavior |
| `update_latest_bust(key, updates, target_ts=None)` | Patch an event. `target_ts` (used by auto-detector) finds by timestamp; default mode patches the latest confirmed bust to stay aligned with `latest_confirmed_bust`'s selection. Returns `None` if no target found (safe no-op). |
| `all_events_for_session(key)` | Full history (used by `/cache-bust-advisor` for retrospective) |

### `lib/judges/haiku_judge.py`

Tier 2 judge. Single Haiku call for topic-continuity tiebreaker:
> Topic A: "..." Topic B: "..." Are these the same task? YES/NO

### `lib/judges/main_model_judge.py`

Tier 3 judge. Single Sonnet call with full session context for structured
COMPACT_NOW / SOON / WAIT verdict + one-sentence reason.

---

## Cost summary

| Component | Per call | Per long session worst case |
|---|---|---|
| All `/cost-snapshot`, `/memory-hygiene` runs | $0 | $0 |
| `/mcp-audit` (uses Anthropic count_tokens API) | ~$0 (tiny request) | ~$0.01 |
| `/cache-bust-advisor` | $0 | $0 |
| `/compact-suggest` T1 | $0 | $0 |
| `/compact-suggest` T2 (Haiku tiebreaker) | ~$0.001 | ~$0.005 |
| `/compact-suggest` T3 (Sonnet judge) | ~$0.05 | ~$0.15 |
| All hooks | $0 (local Python) | $0 |
| **Total cost of running the toolkit** | | **< $0.20 / long session** |

vs. typical savings from acting on its recommendations:
- Disconnecting one dormant MCP server: ~$1–2 / session
- One well-timed `/compact`: ~$50–200 over remaining session
- Avoiding a mistimed cache bust: ~$2–5

---

## Design principles

1. **No model calls from diagnostic scripts.** `/cost-snapshot`, `/memory-hygiene`,
   `/cache-bust-advisor` are pure local Python.
2. **Exact over estimated.** Where Anthropic's tokenizer is reachable
   (via OAuth + `count_tokens` API), we use it. No 4-chars-per-token heuristics
   when we can have the truth.
3. **Graceful degradation.** Every component falls back to lesser modes if
   dependencies are missing (no API key → skip T3, no OAuth → skip MCP audit,
   etc.). Never crashes the user's session.
4. **Single source of truth.** The compact decision logic lives in one place
   (`compact_decision.py`) used by both the slash command and the hook.
   They cannot disagree.
5. **Cost-aware tiering.** Free deterministic checks first; pay for
   intelligence only when needed (Haiku for ambiguity, Sonnet for high stakes).
6. **Honest about limits.** When a hook can't render (e.g., bypass mode
   suppression), we document it instead of pretending it works.

---

## Privacy notes

- All scripts read only **local** session jsonl files in `~/.claude/projects/`
- No telemetry, no external sinks
- Exact-count operations use **your own** OAuth token / API key — sent only
  to `api.anthropic.com`
- Bust event state is local-only (`~/.claude/cache-bust-events.json`)
- Compact advisor state is local-only (`~/.claude/compact-advisor-state.json`)

---

## Sharing with the team

```bash
# Bundle everything portable
cd /path/to/your/project
tar -czf claude-optimizer.tar.gz claude-optimizer/ .claude/

# Send claude-optimizer.tar.gz to teammate
```

**Teammate setup (per project):**
1. Extract into project root: `tar -xzf claude-optimizer.tar.gz`
2. Optionally set `ANTHROPIC_API_KEY` env var (enables T3 Sonnet judge)
3. Restart Claude Code → hooks auto-load
4. Verify: `/compact-suggest` should appear in skills list

**No npm install, no pip install, no shell config.** Python stdlib only.

---

## Quick reference card

| Want to know | Run |
|---|---|
| Should I compact now? Full picture? | `/compact-suggest` |
| Where did my $ go this session? | `/cost-snapshot` |
| Are MCP servers wasting tokens? | `/mcp-audit` |
| Is my memory dir bloated? | `/memory-hygiene` |
| Is now a good time to switch model / edit CLAUDE.md? | `/cache-bust-advisor <description>` |
| (Automatic) Cache-bust warning before action | `cache_bust_warner` hook |
| (Automatic) Prefix size after every turn | `prefix_monitor` hook |
| (Automatic) Compact recommendation banner | `compact_advisor` hook |

---

## Disabling

| Component | How |
|---|---|
| All hooks | Comment them out in `.claude/settings.json` |
| Specific hook | Remove its entry from `settings.json` |
| All slash commands | Delete files in `.claude/commands/` |
| Permission rules | Edit `.claude/settings.json` `permissions.deny` / `permissions.ask` |
| Entire toolkit | `rm -rf claude-optimizer/ .claude/` (or just remove from bundle) |

Fully removable; nothing modifies system files outside the project directory.
