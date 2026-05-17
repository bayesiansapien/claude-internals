# Sub-Agents and Teams in Claude Code — Deep Analysis

> Notes from architecture deep-dive conversations exploring multi-agent orchestration,
> subagent isolation, teammate coordination, mailbox messaging, and the routing
> implications of each pattern.
> All code references point to `claude-code-source/claude-src-code/src/`.
> Python analogies throughout — the codebase is TypeScript/React.

---

## Table of Contents

1. [The Three Orchestration Patterns](#1-the-three-orchestration-patterns)
2. [Subagents: Isolated Workers](#2-subagents-isolated-workers)
3. [Teammates: Coordinated Workers (Swarm)](#3-teammates-coordinated-workers-swarm)
4. [Coordinator: The Manager Pattern](#4-coordinator-the-manager-pattern)
5. [Fork vs Regular Spawn: Two Ways to Create a Subagent](#5-fork-vs-regular-spawn-two-ways-to-create-a-subagent)
6. [SkillTool vs AgentTool: The Cost Divide](#6-skilltool-vs-agenttool-the-cost-divide)
7. [The Mailbox Messaging System](#7-the-mailbox-messaging-system)
8. [Can You Blindfold Teammates?](#8-can-you-blindfold-teammates)
9. [Context Isolation: How Each Agent Stays Separate](#9-context-isolation-how-each-agent-stays-separate)
10. [Cost Comparison: Subagents vs Teammates](#10-cost-comparison-subagents-vs-teammates)
11. [When to Use Subagents vs Teams](#11-when-to-use-subagents-vs-teams)
12. [How Agent Results Return to the Parent](#12-how-agent-results-return-to-the-parent)
13. [Recursive Agent Spawning](#13-recursive-agent-spawning)
14. [No Concurrency Limit](#14-no-concurrency-limit)
15. [Permission Bubbling](#15-permission-bubbling-the-bubble-mode)
16. [Agent Abort and Cancellation](#16-agent-abort-and-cancellation)
17. [Custom Agent Definitions](#17-custom-agent-definitions-claudeagentsmd)
18. [Git Worktree Isolation](#18-git-worktree-isolation)
19. [Inter-Agent File Conflicts](#19-inter-agent-file-conflicts-no-resolution)
20. [Agent Token Budget: Shared, Not Separate](#20-agent-token-budget-shared-not-separate)
21. [Subagent Compaction: Doesn't Happen](#21-subagent-compaction-doesnt-happen)
22. [MCP Server Sharing](#22-mcp-server-sharing)
23. [The Verification Agent](#23-the-verification-agent)
24. [SyntheticOutput Tool](#24-syntheticoutput-tool)
25. [AgentSummary Service](#25-agentsummary-service-30-second-progress-updates)
26. [Plan Mode and Agents](#26-plan-mode-and-agents)
27. [Agent Hooks](#27-agent-hooks)
28. [Team Lifecycle: TeamCreate and TeamDelete](#28-team-lifecycle-teamcreate-and-teamdelete)
29. [Agent Naming: Deterministic IDs](#29-agent-naming-deterministic-ids)
30. [DreamTask: Not Found](#30-dreamtask-not-found)
31. [Tool Allowlists by Agent Type](#31-tool-allowlists-by-agent-type)
32. [Key Source Code References](#32-key-source-code-references)

---

## 1. The Three Orchestration Patterns

Claude Code has three distinct ways to run multiple agents. They differ in isolation, communication, and control flow:

```
┌──────────────────────────────────────────────────────────────────────────┐
│                        PARENT / LEADER AGENT                           │
│                                                                        │
│   ┌─────────────┐    ┌──────────────────┐    ┌────────────────────┐   │
│   │  Pattern 1   │    │   Pattern 2       │    │   Pattern 3         │   │
│   │  SUBAGENTS   │    │   TEAMMATES       │    │   COORDINATOR       │   │
│   │              │    │   (SWARM)         │    │                    │   │
│   │  Fire &      │    │   Long-lived      │    │   Manager +        │   │
│   │  forget      │    │   coordinated     │    │   specialized      │   │
│   │              │    │   workers         │    │   workers          │   │
│   │  No inter-   │    │   Mailbox         │    │   One-way          │   │
│   │  agent       │    │   messaging       │    │   dispatch         │   │
│   │  comms       │    │   (any-to-any)    │    │                    │   │
│   └─────────────┘    └──────────────────┘    └────────────────────┘   │
│                                                                        │
│   Python analogy:     Python analogy:         Python analogy:          │
│   ProcessPoolExecutor multiprocessing +       Manager pattern          │
│                       Queue                   (dispatcher + workers)   │
└──────────────────────────────────────────────────────────────────────────┘
```

---

## 2. Subagents: Isolated Workers

**Python analogy:** `concurrent.futures.ProcessPoolExecutor` — each worker gets a task, runs independently, returns a result. No shared state, no inter-worker communication.

```python
# Subagent pattern — embarrassingly parallel
with ProcessPoolExecutor() as pool:
    futures = [
        pool.submit(research, "find all auth middleware patterns"),
        pool.submit(research, "find all rate limiting implementations"),
        pool.submit(research, "find all caching strategies"),
    ]
    results = [f.result() for f in futures]  # summaries only
    synthesize(results)  # parent does the thinking
```

### Key Properties

- Each subagent gets its own **context window** (fork: snapshot of parent; regular: empty)
- Only the **summary** returns to parent — full history stays in sidechain `.jsonl`
- No `SendMessageTool` available (excluded from the 15-tool allowlist in `constants/tools.ts`)
- Parent can spawn multiple in parallel, continue its own work, collect results later
- Cannot spawn child agents (Agent tool excluded from allowlist)

### When to Use

- Tasks are **embarrassingly parallel** — no agent needs another agent's output
- You want **diverse perspectives** — each agent explores independently, parent synthesizes
- Results are **summaries** — you don't need the full chain of reasoning, just findings
- You want **context isolation** — one agent's 150K-token exploration doesn't pollute another's window

### Source Files

- `tools/AgentTool/AgentTool.tsx` — Agent tool (spawning entry point)
- `tools/AgentTool/runAgent.ts` — Agent lifecycle orchestration
- `tools/AgentTool/forkSubagent.ts` — Fork mechanism
- `constants/tools.ts` — Tool allowlists (15 tools for subagents)

---

## 3. Teammates: Coordinated Workers (Swarm)

**Python analogy:** `multiprocessing` with `Queue` and shared `Manager` — workers coordinate through message passing, share task state, and can react to each other's progress.

```python
# Team pattern — coordinated workers
manager = Manager()
shared_tasks = manager.dict()
message_queue = Queue()

def data_loader_worker(tasks, queue):
    schema = design_schema()
    queue.put({"type": "schema_ready", "schema": schema})  # notify others
    msg = queue.get()  # wait for model engine's interface contract
    implement_loader(schema, msg["interface"])

def model_engine_worker(tasks, queue):
    msg = queue.get()  # wait for schema
    interface = design_interface(msg["schema"])
    queue.put({"type": "interface_ready", "interface": interface})
    implement_engine(interface)
```

### Key Properties

- `SendMessageTool` available — unicast and broadcast messaging
- `TaskCreate/TaskGet/TaskUpdate` available — shared task tracking
- `teammateMailbox.ts` — async message delivery between teammates
- Same Node.js process via `AsyncLocalStorage` — shared filesystem, shared MCP servers
- Deterministic IDs (`"name@teamName"`) — teammates can address each other by name
- CAN spawn subagents (Agent tool included in teammate allowlist)
- Long-lived — stay running, can receive multiple prompts via mailbox

### When to Use

- Tasks have **data dependencies** — agent B needs agent A's output to proceed
- You need **live coordination** — agents must stay in sync as they work
- The work involves **shared artifacts** — same filesystem, same codebase, changes must be compatible
- You want **broadcast communication** — "everyone, the API schema changed"

### Source Files

- `utils/swarm/spawnInProcess.ts` — In-process teammate spawning
- `utils/swarm/inProcessRunner.ts` — Teammate execution wrapper
- `utils/teammateMailbox.ts` — Mailbox read/write with file locking
- `tools/SendMessageTool/SendMessageTool.ts` — Inter-agent communication

---

## 4. Coordinator: The Manager Pattern

**Python analogy:** Manager pattern — one dispatcher with limited powers, workers with execution powers.

```python
# Coordinator pattern — dispatcher + workers
def coordinator(workers):
    # coordinator ONLY has: assign_task, stop_task, send_message, output
    # NO file ops, NO bash — it can only delegate
    task1 = assign_task(workers[0], "implement the data loader")
    task2 = assign_task(workers[1], "implement the model engine")
    wait_for_notifications()  # workers notify when done via <task-notification>
    synthesize_and_report()
```

### Key Properties

- Gated by env var `CLAUDE_CODE_COORDINATOR_MODE` (mutually exclusive with fork mode)
- Coordinator gets only 4 tools: `Agent`, `TaskStop`, `SendMessage`, `SyntheticOutput`
- Workers get: `Bash`, `Read`, `Edit`, MCP tools only — no Agent (no recursion)
- Workers notify coordinator via `<task-notification>` XML when done
- Coordinator synthesizes results from multiple workers

### When to Use

- You want a **single point of control** — one agent decides what happens
- Workers are **specialized** — each gets execution tools but not delegation tools
- The coordination logic is **complex** — dependencies, sequencing, error recovery

### Source Files

- `coordinator/coordinatorMode.ts` — Coordinator pattern (lines 36-369)
- `constants/tools.ts` — `COORDINATOR_MODE_ALLOWED_TOOLS`

---

## 5. Fork vs Regular Spawn: Two Ways to Create a Subagent

There are two distinct mechanisms for creating a subagent, with very different cost profiles:

```
┌─────────────────────────────────────────────────────────────────────┐
│                     PARENT CONTEXT WINDOW                         │
│  [System Prompt] [Tools] [Msg1] [Msg2] ... [MsgN] [Current Turn] │
│                                                                   │
│        ┌──────────────┐              ┌──────────────┐            │
│        │  FORK CHILD   │              │ REGULAR SPAWN │            │
│        │               │              │               │            │
│        │  Gets FULL    │              │  Starts       │            │
│        │  snapshot of  │              │  EMPTY        │            │
│        │  parent:      │              │               │            │
│        │  - System     │              │  Only gets:   │            │
│        │  - Tools      │              │  - System     │            │
│        │  - All msgs   │              │  - Tools      │            │
│        │  - Context    │              │  - Task       │            │
│        │               │              │    prompt     │            │
│        │  SHARES cache │              │  NEW cache    │            │
│        │  (same prefix)│              │  (cold start) │            │
│        └──────────────┘              └──────────────┘            │
└─────────────────────────────────────────────────────────────────────┘
```

### Fork Spawn (forkSubagent.ts)

```python
# Python analogy — like os.fork(), child gets copy of parent's memory
child_pid = os.fork()
if child_pid == 0:
    # Child: has EVERYTHING parent had at moment of fork
    # But it's a SNAPSHOT — changes don't flow back to parent
    do_research()
    return summary  # only this returns to parent
```

**What gets cloned from parent:**
1. Full conversation history (all prior messages)
2. Full parent assistant message (all tool_use blocks, thinking, text — byte-for-byte)
3. System prompt (cached-rendered bytes, not re-called)
4. Exact tool pool (`tools: ['*']` + `useExactTools: true` for identical API prefix)
5. Model (`'inherit'` for cache parity)

**What does NOT get cloned:**
- Parent's file state cache (agent gets fresh cache)
- Parent's permissions (fork gets `permissionMode: 'bubble'`)
- Agent-specific hooks (fork uses built-in defaults)

**Why forks share cache:** The fork constructs a byte-identical API prefix to the parent. Same model + same system prompt + same tools (in same order) = same cache key. Only the last text block (the child's directive) differs. This means the fork reuses the parent's cached KV tensors — saving up to 10x on the cached portion.

**Recursion guard:** Child message is wrapped in `<fork_boilerplate_tag>`. If a fork child tries to fork again, `isInForkChild()` detects the tag and kills the attempt with "Fork is not available inside a forked worker."

### Regular Spawn (runAgent.ts)

```python
# Python analogy — like subprocess.Popen(), child starts fresh
child = subprocess.Popen(["python", "worker.py", "--task", task_description])
# Child starts with NOTHING from parent except the task description
result = child.communicate()
```

- Starts with empty context — only the task prompt and system prompt
- New cache entry (cold start — no cache sharing with parent)
- Independent model (can be different from parent)
- Gets the standard 15-tool allowlist from `constants/tools.ts`

### Source Files

- `tools/AgentTool/forkSubagent.ts` — Fork mechanism, snapshot cloning (lines 107-168)
- `tools/AgentTool/runAgent.ts` — Non-fork path starts empty (lines 370-373)

---

## 6. SkillTool vs AgentTool: The Cost Divide

Two fundamentally different ways to extend the agent's capabilities:

```
┌────────────────────────────────────────────────────────────────────┐
│                    CURRENT CONTEXT WINDOW                        │
│                                                                  │
│  SkillTool: Injects instructions HERE ─────────┐                │
│  (like exec() in same namespace)                │                │
│  Cost: LOW (~same window, no new API call       │                │
│         for the injection itself)                ▼                │
│  ┌──────────────────────────────────────────────────┐            │
│  │  [System] [Tools] [History] [SKILL INSTRUCTIONS] │            │
│  └──────────────────────────────────────────────────┘            │
│                                                                  │
│  AgentTool: Spawns NEW window ──────────────────────────────┐    │
│  (like subprocess.Popen())                                  │    │
│  Cost: HIGH (~7x tokens — full system prompt + tools        │    │
│         re-sent in new context)                             ▼    │
│  ┌──────────────────────────────────────────────────────────┐    │
│  │  [System] [Tools] [Task Prompt] ← ENTIRELY SEPARATE     │    │
│  │  Only SUMMARY returns to parent                         │    │
│  └──────────────────────────────────────────────────────────┘    │
└────────────────────────────────────────────────────────────────────┘
```

```python
# Python analogy:
# SkillTool = exec(skill_code, current_namespace) — runs in your process
# AgentTool = subprocess.Popen() — new process, new memory, only stdout comes back
```

### Decision Rule

Use SkillTool when the task fits the current context window and doesn't need isolation. Use AgentTool only when isolation is needed (long-running research, file-heavy exploration, parallel work).

---

## 7. The Mailbox Messaging System

The mailbox system is how teammates communicate. It's **file-based, pull-driven, and private per agent** — like email, not like a Slack channel.

### Storage Model

```python
# Each teammate has their own inbox file on disk:
# ~/.claude/teams/{team_name}/inboxes/{agent_name}.json

# Message structure:
message = {
    "from": "data-loader",        # sender name
    "text": "Schema is ready, here's the interface...",
    "timestamp": "2026-04-19T10:30:00Z",
    "read": False,                # flipped after delivery
    "color": "#ff6b6b",           # sender's UI color
    "summary": "Schema ready",    # 5-10 word preview
}
```

### Communication Topology

```
UNICAST (one-to-one):
  A ──msg──→ B's inbox file     (only B sees it)

BROADCAST (one-to-all):
  A ──"*"──→ B's inbox file     (each gets their OWN copy)
             C's inbox file     (they CANNOT see each other's copies)
             D's inbox file     (no shared bulletin board)
```

**Broadcast is NOT a shared message board.** It reads `team.json` to get the member list, then writes a separate copy to each teammate's inbox file (excluding the sender):

```python
def broadcast(sender, message, team):
    members = read_team_file(team)
    for member in members:
        if member.name != sender:
            write_to_mailbox(member.name, message)  # individual copy per recipient
```

### Send Mechanism (Push)

```python
def write_to_mailbox(recipient_name, message, team_name):
    inbox_path = f"~/.claude/teams/{team_name}/inboxes/{recipient_name}.json"
    
    # 1. Acquire file lock (proper-lockfile, retries up to 10x, 5-100ms backoff)
    lock = acquire_lock(inbox_path)
    
    # 2. Re-read inbox under lock (get latest state, avoid lost writes)
    current_messages = json.loads(read(inbox_path))
    
    # 3. Append new message with read=False
    current_messages.append({**message, "read": False})
    
    # 4. Write back to disk
    write(inbox_path, json.dumps(current_messages, indent=2))
    
    # 5. Release lock
    release_lock(lock)
```

### Receive Mechanism (Pull — Polling)

```python
# Two polling mechanisms depending on agent type:

# For tmux-based teammates: useInboxPoller hook, polls every 1000ms
# For in-process teammates: waitForNextPromptOrShutdown(), polls every 500ms

def poll_mailbox(agent_name, team_name):
    messages = read_unread_messages(agent_name, team_name)
    
    # Filter out structured protocol messages (permissions, shutdown, etc.)
    user_messages = [m for m in messages if not is_structured_protocol(m)]
    
    if agent_is_idle:
        submit_as_new_turn(user_messages)    # triggers new agent turn
    else:
        queue_in_app_state(user_messages)    # deliver when idle
    
    mark_as_read(user_messages)
```

### Message Delivery to the LLM

Messages are injected as XML attachments in the teammate's next turn:

```xml
<teammate_message from="data-loader" color="#ff6b6b" summary="Schema ready">
Schema is ready, here's the interface: DataPoint(x: float, y: float)...
</teammate_message>
```

### In-Process Message Priority (waitForNextPromptOrShutdown)

```
1. Shutdown requests      ← highest (prevents starvation)
2. Team-lead messages     ← coordinator intent, not peer chatter
3. First unread message   ← FIFO for peer messages
```

### Structured Protocol Messages (Routed Separately)

These are JSON messages with specific `type` fields that get routed to special handlers, NOT delivered as LLM context:

| Message Type | Purpose | Handler |
|---|---|---|
| `shutdown_request` | Graceful shutdown coordination | Teammate's shutdown dialog |
| `shutdown_approved` | Confirm shutdown | Leader's pane killer |
| `permission_request` | Worker needs permission for a tool | Leader's permission queue |
| `permission_response` | Leader's permission decision | Worker's permission callback |
| `plan_approval_request` | Teammate needs plan approved | Leader's auto-approval |
| `plan_approval_response` | Leader's plan decision | Worker's mode transition |
| `idle_notification` | Worker is idle | UI (collapsed, only latest shown) |

Detection: `isStructuredProtocolMessage(text)` checks if message text is JSON with one of these types. These are filtered out by `getTeammateMailboxAttachments()` before being shown to the LLM.

### Concurrency Safety

- File locking via `proper-lockfile` with retries (min 5ms, max 100ms timeout)
- Re-read under lock before appending (prevents lost writes from concurrent senders)
- Deduplication in attachments: same message in both file mailbox + AppState.inbox is deduplicated on `from|timestamp|text[:100]`
- Only marked as read AFTER successful delivery — if session crashes mid-delivery, messages survive next poll

### Source Files

- `utils/teammateMailbox.ts` — Teammate mailbox (read/write with file locking)
- `tools/SendMessageTool/SendMessageTool.ts` — Message sending (650+ lines)
- `hooks/useInboxPoller.ts` — Message polling (React hook, tmux teammates)
- `utils/swarm/inProcessRunner.ts` — In-process polling (500ms interval)
- `utils/attachments.ts` — Message attachment assembly and deduplication

---

## 8. Can You Blindfold Teammates?

**No.** There is no mechanism in the source code to isolate a teammate from messages:

- No blocklist or filter for senders/message types
- No "silent mode" per teammate
- No ACL system — all team members can message all other team members
- No opt-out — if you're in a team, your inbox file exists and is polled
- No unsubscribe — messages are unconditionally written to disk

**However — teammates already have isolated context windows.** Each teammate has its own conversation history via `AsyncLocalStorage`. Teammates cannot see each other's context windows — they can only see messages explicitly sent to them via the mailbox.

**Practical implication:** If you simply never send messages between teammates, they behave almost identically to subagents — isolated context, independent execution. The difference becomes pure overhead (mailbox polling, team file management) with no benefit.

---

## 9. Context Isolation: How Each Agent Stays Separate

### AsyncLocalStorage for In-Process Teammates

```python
# Python analogy — each teammate runs in its own threading.local()
import threading

teammate_context = threading.local()

def run_teammate(name, team, task):
    teammate_context.agent_id = f"{name}@{team}"
    teammate_context.agent_name = name
    teammate_context.abort_controller = AbortController()
    teammate_context.color = "blue"
    teammate_context.plan_mode_required = False
    teammate_context.parent_session_id = leader_session_id
    
    # This teammate's conversation is completely separate
    run_agent(task)  # own context window, own API calls
```

### What Gets Isolated Per Agent

```
┌─────────────────────────────────────────────────────────────────┐
│                    PER-AGENT ISOLATION                         │
│                                                               │
│  Context Window:     Own conversation history (own API calls) │
│  File State Cache:   Cloned from parent, independent copy     │
│  AbortController:    Independent (async) or shared (sync)     │
│  Content Replacement: Own ContentReplacementState              │
│  Sidechain:          Own .jsonl transcript file                │
│  Permissions:        Own permission mode (bubble/inherited)   │
│  MCP Servers:        Shared by reference, or agent-specific   │
│                                                               │
│  NOT isolated:                                                │
│  - Filesystem (same working directory, unless worktree)       │
│  - Session cost (all agents share parent session's budget)    │
│  - AppState (mutations serialized through setAppState)        │
└─────────────────────────────────────────────────────────────────┘
```

### Identity Resolution Priority

```
1. AsyncLocalStorage (in-process teammates) ← highest priority
2. dynamicTeamContext (tmux via CLI args)
3. Environment variables (process-based)
4. AppState.teamContext (leaders)
```

### Source Files

- `utils/swarm/spawnInProcess.ts` — Context creation, AsyncLocalStorage wrapping
- `utils/teammate.ts` — Identity resolution priority chain
- `utils/teammateContext.ts` — AsyncLocalStorage context definition

---

## 10. Cost Comparison: Subagents vs Teammates

### Per-Agent Cost Factors

| Cost Factor | Subagent | Teammate | Difference |
|---|---|---|---|
| API calls per turn | 1 per turn | 1 per turn | **Same** |
| Context window | Own (fork: snapshot; regular: empty) | Own (starts with task prompt) | **Same** |
| System prompt tokens | Re-sent per call | Re-sent per call | **Same** |
| Tool schemas | 15 tools allowed | Full toolset + task mgmt + messaging | **Teammate slightly more** |
| Mailbox polling | None | Every 500-1000ms (file I/O only) | **Negligible** |
| Team file management | None | team.json reads/writes | **Negligible** |
| Token cost per call | ~7x parent (VILA-Lab finding) | ~7x parent | **Same** |
| Prompt cache sharing | Fork variant shares parent cache | No fork variant, starts fresh | **Subagent cheaper if forked** |

### Structural Cost Difference

The key cost difference is not per-agent — it's structural:

```python
# Subagent: fire-and-forget
# Cost = 1 spawn + N turns until done + 1 summary return
subagent_cost = system_prompt_tokens + task_tokens + N * turn_cost

# Teammate: long-lived, can receive multiple prompts
# Cost = 1 spawn + N turns + M message deliveries (each = new turn)
teammate_cost = system_prompt_tokens + task_tokens + N * turn_cost + M * message_turn_cost

# If M = 0 (no messages), teammate_cost ~= subagent_cost
# If M > 0, each message injection triggers a new agent turn = new API call
```

### The Hidden Cost of Messaging

Every message a teammate receives triggers a new turn in their agent loop. That's a new API call with the full context re-sent. If Agent A sends 5 messages to Agent B during a task, that's 5 additional API calls for Agent B beyond what it would have needed as a subagent.

---

## 11. When to Use Subagents vs Teams

### The Decision Function

```python
def choose_orchestration(task):
    needs_realtime_sync = task.has_data_dependencies_between_workers()
    needs_shared_task_tracking = task.workers_must_update_shared_state()
    workers_need_to_react = task.output_of_A_changes_B()

    if not needs_realtime_sync and not workers_need_to_react:
        # Even for coding tasks!
        # "Write 3 independent utility functions" -> subagents
        # "Research 5 different approaches"       -> subagents
        return "subagent"

    if needs_realtime_sync or workers_need_to_react:
        # "Write data loader + model engine with shared schema" -> team
        # "One agent writes tests while another writes code"    -> team
        return "team"

    if needs_shared_task_tracking and complex_dependencies:
        # "Coordinate 4 workers building a microservice" -> coordinator
        return "coordinator"
```

### The Decision Matrix

| Criterion | Subagent | Team/Teammate | Coordinator |
|---|---|---|---|
| Inter-agent communication | None | Mailbox messaging | Manager -> worker only |
| Shared filesystem awareness | No (snapshot or empty) | Yes (same process) | Yes (workers share) |
| Task tracking | No | Yes (TaskCreate/Get) | Yes (via coordinator) |
| Context cost | ~7x per agent | ~7x per agent | ~7x per agent |
| Agent can spawn children | No (Agent tool excluded) | Yes | No (workers can't) |
| Best for | Research, exploration | Coordinated coding | Complex orchestration |

### The Blurry Boundary

The boundary isn't about capability — it's about **communication necessity:**

**Does agent B need to react to agent A's work in real-time?**
- No -> subagent (cheaper, simpler, easier to route-optimize)
- Yes -> teammate/team (more overhead, but necessary for coordination)
- Yes, and it's complex -> coordinator (highest overhead, cleanest control flow)

**Most parallel coding tasks can be done with subagents.** Teams only justify their overhead when agents genuinely need to react to each other's intermediate outputs — not just share a final result. If you can define the interface upfront and each agent can work independently, subagents are cheaper and simpler.

**Example:** "Data loader + model engine" — if you define the `DataPoint` schema upfront in the parent's prompt to both subagents, they can work independently. You only need a team if the schema itself is being discovered during execution.

### Routing Optimization Perspective

**Subagents are the easiest to route-optimize:**
- Each is independent -> assign different models based on task complexity
- "Grep for patterns" -> Haiku. "Analyze architecture" -> Opus. "Summarize findings" -> Sonnet.
- No coordination overhead means model switching between agents has zero cache interaction

**Teams are harder to route-optimize:**
- Shared filesystem means one agent's edits affect another's context
- Message dependencies create ordering constraints — can't use a fast-but-sloppy model for a teammate that others depend on
- But you CAN route by role: boilerplate teammate gets Sonnet, design decision teammate gets Opus

**Coordinator is the most route-friendly pattern:**
- Coordinator itself only dispatches -> Haiku is fine (just sends messages and reads notifications)
- Workers are isolated by task -> route each worker independently
- Clear separation of "thinking" (coordinator) vs "doing" (workers)

---

## 12. How Agent Results Return to the Parent

### Foreground (Sync) Agents

Parent blocks and receives messages live, like iterating a Python generator:

```python
# Python analogy
for message in agent.run():  # async generator, yields as agent works
    parent_conversation.append(message)
# Parent's turn doesn't end until agent finishes
```

- Messages from the agent's internal `query()` loop are filtered (incomplete tool calls removed)
- Only recordable messages are yielded: assistant, user, progress, system/compact_boundary
- Each message is persisted to disk via `recordSidechainTranscript(agentId)`

### Background (Async) Agents

Parent continues immediately. When the agent finishes, a `<task-notification>` XML message gets queued and delivered as a user-role message in a later turn:

```xml
<task-notification>
  <task-id>abc123</task-id>
  <status>completed</status>          <!-- or: failed, killed -->
  <summary>Agent "researcher" completed</summary>
  <result>Found 3 auth middleware patterns in src/...</result>
  <usage>
    <total_tokens>45000</total_tokens>
    <tool_uses>12</tool_uses>
    <duration>34s</duration>
  </usage>
</task-notification>
```

The parent **never sees** the agent's full conversation — only this notification. The full history lives in a sidechain `.jsonl` file on disk.

### The `run_in_background` Parameter

- Default: foreground (blocking) — parent waits
- `run_in_background: true`: fire-and-forget — parent gets notified automatically via task-notification
- No polling needed — notification delivery is asynchronous via `enqueuePendingNotification()`
- Notifications delivered at turn boundaries, not mid-turn

### Backgrounding Mid-Execution

A sync agent can be **backgrounded mid-execution** via `backgroundSignal` race — the parent can decide to stop waiting and continue, converting a sync agent into an async one on the fly.

### Source Files

- `tools/AgentTool/runAgent.ts` — Agent lifecycle, yield mechanism
- `tools/AgentTool/AgentTool.tsx` — Task notification format (lines 867-1205)
- `utils/task/framework.ts` — Task registration, notification enqueueing

---

## 13. Recursive Agent Spawning

### Can a Subagent Spawn Another Subagent?

```python
# Who can spawn whom:
subagent.can_spawn_agent       = False   # blocked by ALL_AGENT_DISALLOWED_TOOLS
fork_child.can_spawn_agent     = False   # blocked by <fork_boilerplate_tag> marker
teammate.can_spawn_agent       = True    # full toolset includes Agent
coordinator_worker.can_spawn_agent = False  # restricted to Bash/Read/Edit/MCP
```

### Three Recursion Guards

1. **Tool allowlist (ALL_AGENT_DISALLOWED_TOOLS):** `AGENT_TOOL_NAME` is in the disallowed list for standard subagents. They simply don't have the Agent tool.

2. **Fork boilerplate marker:** Fork children have `<fork_boilerplate_tag>` in their conversation history. `isInForkChild()` scans for this tag and kills the attempt with "Fork is not available inside a forked worker." Two detection mechanisms: (1) `querySource === 'agent:builtin:fork'` (survives autocompact), (2) message-scan fallback.

3. **Custom agent `disallowedTools`:** Agents defined in `.claude/agents/*.md` can explicitly block `Agent` tool in their frontmatter (e.g., the verification agent does this).

### Source Files

- `constants/tools.ts` — `ALL_AGENT_DISALLOWED_TOOLS` list
- `tools/AgentTool/forkSubagent.ts` — `isInForkChild()` guard

---

## 14. No Concurrency Limit

There is **no hard limit** on how many agents can run simultaneously. No `MAX_AGENTS`, no `maxConcurrent`, no concurrency cap in the source code.

### What Practically Limits Concurrency

- **API rate limits** — each agent makes its own API calls, all count against the same account
- **Memory** — each agent maintains its own context in the Node.js process
- **File I/O** — MCP connections, mailbox polling, sidechain writes
- **Sync agents serialize naturally** — parent blocks until each completes
- **Async agents and teammates run truly concurrently** — as many as you spawn

### Resource Isolation Per Agent

- Each agent clones `readFileState` cache (limited by `READ_FILE_STATE_CACHE_SIZE`)
- Each gets isolated `ContentReplacementState` for tool result replacements
- Async agents get independent `AbortController` (not linked to parent)

---

## 15. Permission Bubbling (the "bubble" Mode)

When a subagent needs permission for a tool, instead of prompting the user directly, the request **escalates to the parent**.

### The Flow

```python
# Python analogy — like a child process asking parent for sudo
class BubblePermission:
    def check(self, tool_call, worker):
        # Worker tries to use a tool
        decision = worker.local_permission_check(tool_call)

        if decision == "ask":
            # Instead of prompting the user directly,
            # worker sends a permission request to the PARENT via mailbox
            request_id = worker.send_permission_request(
                tool_name=tool_call.name,
                tool_input=tool_call.input
            )
            # Worker BLOCKS here, waiting for parent's response
            response = worker.await_permission_response(request_id)
            return response  # allow or deny
```

```
┌──────────────┐     permission_request      ┌──────────────┐
│    WORKER     │ ─────────────────────────→  │    PARENT     │
│  (bubble mode)│                              │  (leader)     │
│               │     permission_response      │               │
│  BLOCKS HERE  │ ←─────────────────────────  │  Auto-approve │
│               │                              │  via classifier│
│  Resumes with │                              │  or prompt    │
│  allow/deny   │                              │  user         │
└──────────────┘                              └──────────────┘
```

### When Bubble Mode Activates

- Fork children default to `permissionMode: 'bubble'`
- Can be set via agent definition frontmatter: `permissionMode: 'bubble'`
- Gets **overridden** if the parent is in `bypassPermissions`, `acceptEdits`, or `auto` — those higher-trust modes take precedence (safety constraint)

### For Swarm Workers

- Worker's tool attempt -> `hasPermissionsToUseTool()` -> `'ask'` result
- Permission request forwarded to leader via `sendPermissionRequestViaMailbox()`
- Worker blocks in `Promise` waiting for leader's response
- Leader can auto-approve via classifier (bash only), hook, or manual UI prompt
- Response flows back through `registerPermissionCallback()` -> `onAllow/onReject`

### Routing Implication

Bubble mode means **extra API round-trips** between worker and parent for permissions. A router should account for this overhead — permission-heavy tasks in bubble mode are more expensive than the same tasks in `auto` mode.

### Source Files

- `hooks/toolPermission/handlers/swarmWorkerHandler.ts` — Bubble handler
- `tools/AgentTool/runAgent.ts` — Permission mode inheritance (lines 440-451)

---

## 16. Agent Abort and Cancellation

### AbortController Linkage by Agent Type

```python
# Python analogy
class SyncAgent:
    # Shares parent's AbortController — same reference
    # Parent presses ESC -> both parent AND agent get cancelled
    abort_controller = parent.abort_controller

class AsyncAgent:
    # Gets its own INDEPENDENT AbortController
    # Parent's ESC does NOT cancel background agents
    abort_controller = AbortController()  # new, independent
    # Only way to stop: TaskStop tool explicitly calls abort_controller.abort()

class InProcessTeammate:
    # Gets its own INDEPENDENT AbortController
    # Created via createAbortController() in inProcessRunner.ts
    # Can be aborted independently from leader and other teammates
    abort_controller = AbortController()  # new, independent
```

### What Happens on Crash

```
Agent encounters error
  │
  ├─ API error → Caught, inserted as createAssistantAPIErrorMessage()
  │              Agent may continue or exit depending on error type
  │
  ├─ Thrown exception → Cleanup runs in finally block:
  │   ├── MCP server cleanup (try/catch with timeout)
  │   ├── Session hook cleanup
  │   ├── Prompt cache tracking cleanup
  │   ├── File state cache release
  │   └── Perfetto agent unregistration
  │
  ├─ Async agent failure →
  │   ├── If AbortError: killAsyncAgent() → status "killed"
  │   └── Otherwise: failAsyncAgent() → status "failed" with error text
  │   └── Worktree cleanup attempted before notification
  │   └── Error notification enqueued
  │
  └─ Sync agent failure →
      └── Exception propagates up to parent (parent sees it)
      └── Worktree cleanup in finally block
```

### TaskStop Tool

- Calls `task.abortController.abort()` on target task
- Async agent lifecycle catches abort -> transitions to "killed"
- Only way to stop a background agent from the parent

### Source Files

- `tools/AgentTool/runAgent.ts` — Abort controller linkage (lines 524-528)
- `tools/AgentTool/AgentTool.tsx` — Error handling (lines 867-1205)
- `tools/TaskStopTool/` — Manual abort mechanism

---

## 17. Custom Agent Definitions (`.claude/agents/*.md`)

You can define custom agent types as markdown files with YAML frontmatter:

```yaml
# .claude/agents/researcher.md
---
name: researcher
description: Deep research into codebases and documentation
model: sonnet                       # 'inherit', 'opus', 'sonnet', 'haiku', or full model ID
tools: ['Read', 'Glob', 'Grep', 'WebFetch', 'WebSearch']  # or ['*'] for all
disallowedTools: ['Agent']          # block specific tools
background: true                    # always run async
isolation: worktree                 # git worktree isolation
permissionMode: bubble              # escalate permissions to parent
maxTurns: 50                        # cap iterations
effort: medium                      # reasoning effort (low/medium/high/max)
color: blue                         # UI color
skills: ['deep-research']           # preload skills
memory: project                     # 'user', 'project', or 'local' persistent memory scope
initialPrompt: "Focus on patterns"  # prepend to first user turn
hooks:                              # agent-scoped hooks
  PreToolUse:
    - command: "echo checking"
mcpServers: ['my-server']           # agent-specific MCP servers
---

You are a research specialist. Focus on finding patterns and
summarizing findings concisely. Never modify files.
```

### Full YAML Frontmatter Fields

| Field | Type | Description |
|---|---|---|
| `name` | string (required) | Agent type identifier |
| `description` | string (required) | "When to use" blurb |
| `model` | string | `'inherit'`, `'opus'`, `'sonnet'`, `'haiku'`, or full model ID |
| `tools` | list | Allowed tools; empty = all tools |
| `disallowedTools` | list | Blocked tools (applied after allowlist) |
| `background` | bool | Always run as background task |
| `isolation` | string | `'worktree'` (or `'remote'` on internal builds) |
| `permissionMode` | string | `'default'`, `'ask'`, `'bubble'` |
| `maxTurns` | int | Max agentic turns before stopping |
| `effort` | string/int | `'low'`/`'medium'`/`'high'`/`'max'` or 1-4 |
| `color` | string | Agent UI color |
| `skills` | list | Preload skills (slash commands) |
| `memory` | string | `'user'`, `'project'`, or `'local'` |
| `initialPrompt` | string | Prepend to first user turn |
| `hooks` | object | Session-scoped hooks (PreToolUse, PostToolUse, etc.) |
| `mcpServers` | list | Agent-specific MCP servers (strings or inline configs) |

### Resolution Hierarchy (Later Overrides Earlier)

```
built-in → plugin → user settings → project settings → managed (enterprise) → CLI
```

### Agent Definition Types

- `BuiltInAgentDefinition`: Dynamic prompts via `getSystemPrompt()` callback
- `CustomAgentDefinition`: Static content + closure-captured prompt
- `PluginAgentDefinition`: Plugin-sourced agents with metadata

### Routing Relevance

This is a **major routing surface** — define agents with different models and tools for different task types, and the parent just calls `Agent(subagent_type="researcher")`. The model field in the frontmatter is a static routing decision.

### Source Files

- `tools/AgentTool/loadAgentsDir.ts` — Agent definition loading (756 lines)
- `tools/AgentTool/AgentTool.tsx` — Agent spawning from definitions

---

## 18. Git Worktree Isolation

When you set `isolation: worktree` on an agent, it gets its own **complete copy of the repo**:

```python
# Python analogy — like creating a separate git checkout
import subprocess

def create_agent_worktree(agent_id):
    slug = f"agent-{agent_id[:8]}"
    worktree_path = f"/tmp/worktrees/{slug}"
    branch = f"agent/{slug}"

    subprocess.run(["git", "worktree", "add", worktree_path, "-b", branch])
    return worktree_path, branch

# Agent runs with CWD overridden to worktree_path
# All file ops happen in the isolated copy
```

### Lifecycle

```
SPAWN with isolation: worktree
  │
  ├─ createAgentWorktree(slug)
  │   → Returns: {worktreePath, worktreeBranch, headCommit, gitRoot}
  │
  ├─ Agent execution wrapped in runWithCwdOverride(worktreePath, fn)
  │   → All filesystem + shell operations use worktree as root
  │
  ├─ Fork + worktree: injects buildWorktreeNotice() message:
  │   "You are operating in isolated git worktree at {path}.
  │    Paths in inherited context refer to parent's directory;
  │    translate them to your worktree root. Re-read files before editing."
  │
  └─ After completion:
      ├─ hasWorktreeChanges(worktreePath, headCommit)?
      │   ├─ No changes → removeAgentWorktree(), clear metadata
      │   └─ Has changes → KEEP on disk, return {worktreePath, worktreeBranch}
      │                     User/parent can inspect and merge
      └─ Metadata persisted: writeAgentMetadata(agentId, {...})
          → On resume: reconnects to same worktree
```

### Why This Matters

Two agents can edit the **same file** in different worktrees without conflicts. This is the only safe way to have multiple agents do parallel coding on overlapping files.

### Source Files

- `tools/AgentTool/AgentTool.tsx` — Worktree creation and cleanup (lines 582-685)

---

## 19. Inter-Agent File Conflicts: No Resolution

If two teammates (NOT in worktrees) edit the same file simultaneously — **last write wins.**

- No file locking between agents
- No conflict detection
- No merge resolution
- No warning system

### The Design Assumption

The coordinator or parent is responsible for directing agents to **disjoint files**:

```
SAFE:                               UNSAFE:
Agent A → src/loader.py             Agent A → src/main.py  ← conflict!
Agent B → src/engine.py             Agent B → src/main.py  ← conflict!
Agent C → src/tests.py              (last write wins, silent data loss)
```

### Mitigation Strategies

1. **Worktree isolation** — each agent gets a separate repo copy (no filesystem conflict)
2. **File assignment** — coordinator explicitly assigns non-overlapping files
3. **Sequential execution** — don't run agents that touch the same files concurrently

---

## 20. Agent Token Budget: Shared, Not Separate

Subagents do **not** have their own token budgets. All agents share the parent session's budget:

```python
# There is NO per-agent budget:
agent.max_tokens = 50000  # ← doesn't exist

# Instead, everything rolls up to the session:
session.total_cost += agent_1.cost + agent_2.cost + agent_3.cost
```

### Usage Tracking (Per-Agent, But No Cap)

Each agent's usage is tracked individually for reporting:
- `latestInputTokens` (cumulative per turn)
- `cumulativeOutputTokens` (summed per turn)
- `toolUseCount` (incremented per tool_use block)

But there's no per-agent spending cap. The only limit is the session-level budget.

### Routing Implication

A router that wants to cap per-agent spend would need to **add this itself** — it doesn't exist today. You'd need to monitor usage and call `TaskStop` when a subagent exceeds its allocation.

### Source Files

- `cost-tracker.ts` — Session-level cost aggregation
- `utils/forkedAgent.ts` — Per-fork usage accumulation

---

## 21. Subagent Compaction: Doesn't Happen

Subagents are designed to be **short-lived** — they do their task and exit. They don't run their own compaction pipeline.

- No auto-compact triggers for subagents
- If a subagent's context fills up, it errors or stops
- Only the parent session runs the 5-stage compaction pipeline
- Subagent transcripts are archived to sidechain files after completion

### Design Implication

Subagents work best for **focused, bounded tasks** — not open-ended sessions. If a task might need 200K+ tokens of context, either:
1. Use the main agent (which has compaction)
2. Break the task into smaller subtasks for multiple subagents
3. Accept the risk of context overflow

---

## 22. MCP Server Sharing

MCP connections have a nuanced sharing model between parent and agents:

```python
# Python analogy
mcp_cache = {}  # memoized, process-wide (lodash memoize)

def connect_to_server(name):
    if name not in mcp_cache:
        mcp_cache[name] = create_connection(name)
    return mcp_cache[name]  # shared reference

# Parent connects to "github-server" → cached
# Subagent references "github-server" → gets SAME connection (cache hit)
# Subagent defines inline server config → NEW connection, cleaned up on exit
```

### Two Ways Agents Get MCP Servers

1. **By name reference** (string in frontmatter `mcpServers: ['github-server']`):
   - Uses memoized `connectToServer()` → **shared** with parent (same memoize cache)
   - Connection persists after agent exits

2. **Inline config** (object in frontmatter `mcpServers: [{serverName: {...config}}]`):
   - Creates new connection → **agent-specific**
   - Cleaned up when agent exits (`newlyCreatedClients` array tracked for cleanup)

### Connection Types

- SSE: Long-lived EventSource + fetch-based auth refresh
- HTTP/WebSocket: Persistent connection
- Stdio: Child process
- IDE: Long-lived (not cleaned up per agent)

### Source Files

- `services/mcp/client.ts` — MCP client with memoized `connectToServer()` (2400+ lines)
- `tools/AgentTool/runAgent.ts` — `initializeAgentMcpServers()` for agent-specific servers

---

## 23. The Verification Agent

A built-in agent type designed to **break your implementation**, not confirm it works:

```python
# Built-in definition (simplified):
VERIFICATION_AGENT = {
    "agentType": "verification",
    "background": True,       # always runs async
    "disallowedTools": ["Agent", "ExitPlanMode", "Edit", "Write", "NotebookEdit"],
    "model": "inherit",
    "color": "red",
}
```

### What It Does

- Runs builds, test suites, linters first
- Applies adversarial probes: concurrency, boundary values, idempotency, orphan operations
- Can write to `/tmp` for ephemeral test scripts but **cannot modify source files**
- Must include at least one command run block per check
- Returns a verdict: `PASS`, `FAIL`, or `PARTIAL` with evidence

### Read-Only Design

The verification agent has `Edit`, `Write`, and `NotebookEdit` in its `disallowedTools`. It can read code and run tests but cannot change anything. Also cannot spawn child agents (Agent tool blocked).

### Routing Relevance

Always a background agent — a router could assign it a cheaper model since it's doing verification (running tests, checking outputs), not creation. Sonnet or even Haiku would likely suffice.

### Source Files

- `tools/AgentTool/built-in/verificationAgent.ts` — Agent definition and system prompt

---

## 24. SyntheticOutput Tool

Only available in **non-interactive sessions** (SDK, headless CLI). Lets the agent return structured JSON that conforms to a user-provided schema:

```python
# Python analogy
from jsonschema import validate

def synthetic_output(data, schema):
    """Only available when running programmatically, not interactively."""
    validate(data, schema)  # Ajv validation (cached via WeakMap)
    return {"data": "Structured output provided", "structured_output": data}

# Example: agent returns {"bugs": ["bug1", "bug2"]} matching a provided schema
```

### Key Properties

- **Only enabled when** `isNonInteractiveSession === true`
- **Dynamic schema**: Takes arbitrary JSON schema object, validates via Ajv
- **Caching**: Identity-keyed WeakMap caches compiled validators (1.4ms -> <0.1ms per call)
- **Always allowed**: No permission check, `isReadOnly() === true`, `isConcurrencySafe() === true`

### Why This Matters for SDK Users

When building applications on top of Claude Code via the SDK, you define the output schema, and the agent uses this tool to return structured data instead of free-form text. This is how you get reliable, parseable output from an agent.

### Source Files

- `tools/SyntheticOutputTool/SyntheticOutputTool.ts` — Full implementation

---

## 25. AgentSummary Service (30-Second Progress Updates)

For coordinator and fork patterns, the parent gets periodic progress summaries from each running agent:

```
┌──────────────────┐      every 30s       ┌──────────────────┐
│ RUNNING SUBAGENT  │ ────────────────────→ │  FORKED SUMMARY   │
│                   │                       │  AGENT            │
│  (own context,    │   reads transcript    │                   │
│   own API calls)  │   from sidechain      │  Produces 3-5     │
│                   │   .jsonl file         │  word summary:    │
│                   │                       │  "Reading auth    │
│                   │                       │   middleware"     │
│                   │                       │                   │
└──────────────────┘                       └────────┬─────────┘
                                                    │
                                                    ▼
                                      AppState.agentProgress[taskId]
                                            .summary
                                      (displayed in parent's UI)
```

### How It Works

1. Timer fires every 30 seconds (on completion, not interval — no stacking)
2. Forks a lightweight agent that reads the subagent's transcript (rebuilt from disk each tick via `getAgentTranscript()`)
3. Prompt: "Describe your most recent action in 3-5 words using present tense (-ing)"
4. Tools are denied via callback (not filtered from schema) to **preserve prompt cache sharing**
5. Summary stored in `AppState.agentProgress[taskId].summary` for UI display
6. Cleanup: `stop()` function called when agent completes, aborts pending summary

### When It's Enabled

- `isCoordinator` mode, OR
- `isForkSubagentEnabled()`, OR
- `getSdkAgentProgressSummariesEnabled()`

### Cost

Each summary tick is a forked agent API call. For long-running tasks: 30 seconds x N agents = significant background token consumption. A router could use Haiku for these summaries — they're just generating 3-5 words.

### Source Files

- `services/AgentSummary/agentSummary.ts` — Summary timer, fork, and state update

---

## 26. Plan Mode and Agents

Plan mode has specific interactions with agent spawning:

### Permission Mode Inheritance

```
Parent's permissionMode → passed to child by default
  │
  ├─ Agent definition can OVERRIDE with its own permissionMode field
  │   (e.g., fork agents default to 'bubble')
  │
  └─ BUT: if parent is in bypassPermissions, acceptEdits, or auto,
      those ALWAYS take precedence — agent can't downgrade the trust level
```

### Plan Mode Requirements

- Some agents enforce `planModeRequired: true` — blocks tool execution until plan approval
- Teammates can have `plan_mode_required: true` set at spawn time
- The `ExitPlanMode` tool is only available when `permissionMode === 'plan'`

### Coordinator Workers

- Coordinator mode locks workers to `COORDINATOR_MODE_ALLOWED_TOOLS`
- Workers cannot change their own permission mode
- Coordinator's prompt explains worker tool limits

---

## 27. Agent Hooks

Hooks can intercept agent lifecycle events at multiple points:

### Hook Types That Affect Agents

| Hook | When It Fires | What It Can Do |
|---|---|---|
| `SubagentStart` | When subagent begins execution | Inject `additionalContext` into agent's system prompt |
| `PreToolUse` (on Agent tool) | Before Agent tool runs | Block agent spawning, modify prompt before spawn |
| `PostToolUse` | After agent result delivered | Transform output, log results |
| Agent frontmatter hooks | During agent's own execution | Scoped to that agent only |

### SubagentStart Hook

```python
# Can add context to the subagent's system prompt
hook_response = {
    "continue": True,
    "hookSpecificOutput": {
        "hookEventName": "SubagentStart",
        "additionalContext": "Always check tests before reporting done"
    }
}
```

### Agent Frontmatter Hooks

Defined in `.claude/agents/*.md` frontmatter under `hooks:`:
- Scoped to that agent's execution only
- Registered on agent start via `registerFrontmatterHooks()`
- Unregistered via `clearSessionHooks()` on agent exit
- Do NOT propagate to child agents or workers

### No Hook Chain Between Coordinator and Workers

- Coordinator's hooks don't apply to workers automatically
- Workers have their own hook context (fresh session)
- Worker frontmatter hooks apply to that worker only

### Source Files

- `types/hooks.ts` — Hook type definitions
- `utils/hooks/registerFrontmatterHooks.js` — Frontmatter hook registration
- `tools/AgentTool/runAgent.ts` — Hook registration/cleanup (lines 557-558)

---

## 28. Team Lifecycle: TeamCreate and TeamDelete

### TeamCreate Flow

```
TeamCreate(team_name="my-project")
  │
  ├─ Check: is leader already in a team? (one team per leader)
  │
  ├─ Generate unique name if needed (via generateWordSlug())
  │
  ├─ Write team.json:
  │   {
  │     "name": "my-project",
  │     "leadAgentId": "team-lead@my-project",   ← deterministic
  │     "leadSessionId": "...",
  │     "members": [
  │       {"agentId": "team-lead@my-project", "name": "team-lead", ...}
  │     ]
  │   }
  │
  ├─ Create/reset task list directory for team
  │
  ├─ Register team for session-end cleanup
  │   (prevents orphan team files if session crashes)
  │
  ├─ Update AppState.teamContext with team name, lead ID, teammates roster
  │
  └─ Emit tengu_team_created analytics event
```

### Team File Structure

```python
team_file = {
    "name": "my-project",
    "description": "Building the data pipeline",
    "createdAt": 1702500000,
    "leadAgentId": "team-lead@my-project",
    "leadSessionId": "abc-123-def",
    "members": [
        {
            "agentId": "team-lead@my-project",
            "name": "team-lead",
            "agentType": "leader",
            "model": "claude-opus-4-6",
            "joinedAt": 1702500000,
            "tmuxPaneId": "%0",
            "cwd": "/home/user/project",
            "subscriptions": []
        },
        {
            "agentId": "researcher@my-project",
            "name": "researcher",
            "agentType": "custom",
            "model": "claude-sonnet-4-6",
            "joinedAt": 1702500030,
            "tmuxPaneId": "%1",
            "cwd": "/home/user/project",
            "subscriptions": []
        }
    ]
}
```

### TeamDelete

- Removes team file
- Clears `AppState.teamContext`
- Kills all teammates
- Allows leader to leave/disband team

### Source Files

- `tools/TeamCreateTool/TeamCreateTool.ts` — Team creation
- `tools/TeamDeleteTool/` — Team deletion

---

## 29. Agent Naming: Deterministic IDs

### ID Format

```python
# Teammate/team agent IDs are deterministic:
agent_id = f"{agent_name}@{team_name}"
# Example: "researcher@my-project"

# Request IDs (for structured messages):
request_id = f"{request_type}-{timestamp}@{agent_id}"
# Example: "shutdown-1702500000000@researcher@my-project"

# Main session task IDs: prefix 's' + 8 random bytes base-36
# Example: "s7k3m9p2x1"

# Subagent IDs (non-team): UUID-based via createAgentId()
```

### Why Deterministic

- Teammates can compute each other's IDs without lookup (for SendMessage routing)
- IDs survive crash/restart — reconnection is possible
- Human-readable for debugging (not random GUIDs)

### Source Files

- `utils/agentId.ts` — ID generation and formatting

---

## 30. DreamTask: Not Found

The VILA-Lab paper and community resources mention a DreamTask for memory consolidation (AutoDream). However, it **does not exist** in this version of the source code:

- No `DreamTask` directory found under `tasks/`
- No "dream" references in agent definitions
- No consolidation-related agent in built-in agents

The memory consolidation that does exist is the `extractMemories` background service (which runs after each turn — see the caching-and-routing notes). DreamTask may be unreleased, removed, or named differently in the current codebase.

---

## 31. Tool Allowlists by Agent Type

### Complete Comparison

| Agent Type | Tools Available | Key Exclusions |
|---|---|---|
| **Background subagent** | 15 tools: File (Read/Edit/Write/Glob/Grep), Web (Fetch/Search), Bash, Notebook, Skill | No Agent (no recursion), no TaskStop, no AskUser, no SendMessage |
| **In-Process Teammate** | All above + TaskCreate/Get/Update + SendMessage + TeamCreate/Delete + Agent | Full capability, can spawn subagents |
| **Coordinator** | 4 tools only: Agent, TaskStop, SendMessage, SyntheticOutput | No file ops, no bash, no reading |
| **Coordinator Worker** | Bash, Read, Edit, MCP tools | No Agent, no task management, no messaging |
| **Verification Agent** | All read tools + Bash | No Edit, no Write, no NotebookEdit, no Agent |
| **Fork Child** | Same as parent (`tools: ['*']` + `useExactTools`) | No forking (boilerplate guard), inherits parent's full toolset |

### Source Files

- `constants/tools.ts` — All allowlists defined here:
  - `ALL_AGENT_DISALLOWED_TOOLS` — blocked for all subagents
  - `COORDINATOR_MODE_ALLOWED_TOOLS` — coordinator's 4 tools
  - `COORDINATOR_MODE_WORKER_ALLOWED_TOOLS` — worker's tools

---

## 32. Key Source Code References

| Component | File | Key Lines | What It Does |
|---|---|---|---|
| Agent tool entry | `tools/AgentTool/AgentTool.tsx` | — | Spawning entry point, task notifications |
| Agent lifecycle | `tools/AgentTool/runAgent.ts` | 370-373, 440-451, 524-528 | Empty start, permissions, abort |
| Fork mechanism | `tools/AgentTool/forkSubagent.ts` | 107-168 | Snapshot cloning, cache sharing |
| Agent tool filtering | `tools/AgentTool/agentToolUtils.ts` | 70-116 | Tool allowlists for agents |
| Agent definitions | `tools/AgentTool/loadAgentsDir.ts` | — | Custom agent loading (756 lines) |
| Verification agent | `tools/AgentTool/built-in/verificationAgent.ts` | — | Read-only QA agent definition |
| Teammate spawning | `utils/swarm/spawnInProcess.ts` | — | AsyncLocalStorage context creation |
| Teammate execution | `utils/swarm/inProcessRunner.ts` | — | Polling, message delivery |
| Teammate mailbox | `utils/teammateMailbox.ts` | — | File-based read/write with locking |
| SendMessage tool | `tools/SendMessageTool/SendMessageTool.ts` | 191-266 | Unicast + broadcast handling |
| Inbox polling | `hooks/useInboxPoller.ts` | — | 1000ms polling for tmux teammates |
| Coordinator mode | `coordinator/coordinatorMode.ts` | 36-369 | Coordinator pattern |
| Tool allowlists | `constants/tools.ts` | — | Per-agent-type tool lists |
| Agent model | `utils/model/agent.ts` | 37 | getAgentModel() resolution chain |
| Agent ID | `utils/agentId.ts` | — | Deterministic ID generation |
| Agent summary | `services/AgentSummary/agentSummary.ts` | — | 30s progress updates |
| TeamCreate | `tools/TeamCreateTool/TeamCreateTool.ts` | — | Team lifecycle |
| TeamDelete | `tools/TeamDeleteTool/` | — | Team disbanding |
| SyntheticOutput | `tools/SyntheticOutputTool/SyntheticOutputTool.ts` | — | Structured JSON output (SDK) |
| Task framework | `utils/task/framework.ts` | — | Task registration, notifications |
| Teammate context | `utils/teammateContext.ts` | — | AsyncLocalStorage context type |
| Identity resolution | `utils/teammate.ts` | — | Priority chain for agent identity |
| Attachments | `utils/attachments.ts` | — | Message attachment assembly |
| Cost tracker | `cost-tracker.ts` | 278-323 | Session-level cost (no per-agent cap) |

---

## Pricing Reference (for Cost Calculations)

| Model | Input ($/Mtok) | Output ($/Mtok) | Cache Read ($/Mtok) | Cache Write ($/Mtok) |
|---|---|---|---|---|
| Haiku 4.5 | $1 | $5 | — | — |
| Sonnet (all) | $3 | $15 | $0.30 | $3.75 |
| Opus 4.6 | $5 | $25 | — | — |
| Opus 4.6 Fast | $30 | $150 | — | — |
| Opus 4/4.1 | $15 | $75 | — | — |
