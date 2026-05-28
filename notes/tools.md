# Tools in Claude Code — Complete Reference

> Deep dive into every aspect of how tools work in Claude Code.
> All code references point to `claude-code-source/claude-src-code/src/`.
> Python analogies throughout.

---

## Table of Contents

1. [What Is a Tool? Ground Zero](#1-what-is-a-tool-ground-zero)
2. [The Tool Interface Contract](#2-the-tool-interface-contract)
3. [Complete Tool Inventory](#3-complete-tool-inventory)
4. [Deferred vs Always-Loaded: The Token Budget Decision](#4-deferred-vs-always-loaded-the-token-budget-decision)
5. [How Deferred Tools Get Loaded: ToolSearch](#5-how-deferred-tools-get-loaded-toolsearch)
6. [MCP Tools: Always Deferred, Always Dynamic](#6-mcp-tools-always-deferred-always-dynamic)
7. [Token Memory Breakdown](#7-token-memory-breakdown)
8. [The Tool Execution Pipeline: Step by Step](#8-the-tool-execution-pipeline-step-by-step)
9. [Concurrency: Parallel vs Serial Execution](#9-concurrency-parallel-vs-serial-execution)
10. [Tool Result Budgeting and Disk Offloading](#10-tool-result-budgeting-and-disk-offloading)
11. [Per-Tool Result Size Limits](#11-per-tool-result-size-limits)
12. [Tool Pool Assembly: How the Final List Is Built](#12-tool-pool-assembly-how-the-final-list-is-built)
13. [How Tools Are Called: The Full LLM Round-Trip](#13-how-tools-are-called-the-full-llm-round-trip)
14. [Error Handling in Tool Calls](#14-error-handling-in-tool-calls)
15. [Tool Aliases and Deprecated Names](#15-tool-aliases-and-deprecated-names)
16. [Key Source Code References](#16-key-source-code-references)
17. [Memory Bloat and Design Gaps](#17-memory-bloat-and-design-gaps)
18. [Routing Opportunities in the Tool System](#18-routing-opportunities-in-the-tool-system)

---

## 1. What Is a Tool? Ground Zero

The LLM cannot execute code, read files, or hit APIs. It only ever outputs text. But it can output text in a special structured format that says: **"I want to call this function with these arguments."**

The harness (Claude Code's infrastructure) reads that structured output, actually runs the operation, and feeds the result back as text. The LLM reads that text and continues.

```python
# Ground zero: the entire tool-calling mechanism
def agent_loop(user_message):
    messages = [{"role": "user", "content": user_message}]

    while True:
        # LLM outputs text — that's all it ever does
        response = llm.generate(
            system="You have tools: [Read, Bash, Edit...]",
            messages=messages
        )

        if response.stop_reason == "end_turn":
            return response.text           # Done — plain text answer

        if response.stop_reason == "tool_use":
            # LLM requested a tool — it outputted structured JSON
            # LLM is WAITING — it has produced no final answer yet
            for tool_call in response.tool_use_blocks:
                # Execution happens HERE, in the harness — NOT in the LLM
                result = execute_tool(tool_call.name, tool_call.input)

            # Feed result back — LLM sees it as more text in the conversation
            messages.append({"role": "assistant", "content": response})
            messages.append({"role": "user",      "content": tool_results})
            # Loop back — LLM generates again with tool results in context
```

### How the LLM "knows" about tools

Tool definitions are injected into the **system prompt on every single API call** — just text. The LLM was fine-tuned on millions of examples of: "given tool definitions in context + user request → output the correct JSON tool call." It's learned pattern completion, not a built-in capability.

```
System Prompt (every API call):
┌──────────────────────────────────────────────────────────┐
│ Here are the tools available:                            │
│                                                          │
│ {"name": "Read",                                         │
│  "description": "Read a file from disk",                 │
│  "parameters": {                                         │
│    "file_path": {"type": "string",                       │
│                  "description": "Absolute path"}         │
│  }}                                                      │
│                                                          │
│ {"name": "Bash",                                         │
│  "description": "Run a shell command",                   │
│  "parameters": {                                         │
│    "command": {"type": "string"},                        │
│    "timeout": {"type": "number", "optional": true}       │
│  }}                                                      │
│ ... more tools ...                                       │
└──────────────────────────────────────────────────────────┘
```

**Important:** Only the schema (name, description, parameters) is injected — NOT the tool's implementation code. The LLM never sees how a tool works, only what it accepts and what it does.

---

## 2. The Tool Interface Contract

Every tool in Claude Code must implement this interface (Python equivalent):

```python
class Tool:
    # --- Identity ---
    name: str                    # Unique ID — "Bash", "Read", "Edit"
                                 # Used for tool_use block matching
    aliases: list[str]           # Deprecated names (e.g., "KillShell" -> TaskStop)

    # --- What the LLM Sees ---
    description: str             # Goes into the system prompt
    input_schema: JSONSchema     # Parameter definitions (zod schema in TS)
    search_hint: str             # 3-10 words for ToolSearch keyword ranking
                                 # e.g., "read files, images, PDFs, notebooks"

    # --- Behavior Flags ---
    should_defer: bool           # True = start as name-only stub, load on demand
    always_load: bool            # True = force full schema upfront (MCP opt-out)
    is_mcp: bool                 # True = this is an MCP-provided tool

    # --- Execution ---
    max_result_size_chars: int   # Per-tool result truncation limit
                                 # Infinity = use tool's own self-bounding logic

    def is_enabled() -> bool:
        """Feature-gated: can return False to hide tool entirely."""

    def is_read_only(input) -> bool:
        """
        True = tool doesn't modify anything (Read, Glob, Grep).
        Affects permission requirements — read-only tools auto-approved in
        most permission modes.
        """

    def is_concurrency_safe(input) -> bool:
        """
        True = tool can run in parallel with others (Read, Glob, Grep).
        False = must serialize (Bash, Edit, Write).
        """

    def validate_input(input, context) -> ValidationResult:
        """
        Two-stage validation:
        1. Schema validation (zod/JSON Schema) — catches wrong types
        2. Semantic validation — tool-specific logic (e.g., path exists?)
        """

    def check_permissions(input) -> PermissionResult:
        """Tool's own permission logic, runs in the permission cascade."""

    async def call(input, context, progress_callback) -> ToolResult:
        """The actual execution — reads files, runs commands, etc."""

    def map_tool_result_to_api_format(result, tool_use_id) -> ToolResultBlock:
        """Converts result to the format the API expects back."""
```

---

## 3. Complete Tool Inventory

### Group A: Always-Loaded Core Tools (~15)

These have their **full JSON schema** injected into the system prompt on every API call. The model can call them immediately without any ToolSearch step.

| Tool Name | Read-Only | Concurrent | What It Does |
|---|---|---|---|
| `Read` | Yes | Yes | Read files, images, PDFs, Jupyter notebooks |
| `Edit` | No | No | Modify file contents in place (old_string → new_string) |
| `Write` | No | No | Create or overwrite a file completely |
| `Glob` | Yes | Yes | Find files by pattern (`**/*.py`, `src/**/*.ts`) |
| `Grep` | Yes | Yes | Search file contents by regex across a directory |
| `Bash` | No | No | Run shell commands with timeout |
| `PowerShell` | No | No | Windows PowerShell commands (Windows only) |
| `ToolSearch` | Yes | Yes | Load deferred tool schemas on demand |
| `Skill` | Yes | Yes | Inject skill (SKILL.md) instructions into context |
| `EnterPlanMode` | — | — | Switch agent to plan mode |
| `Agent` | No | No | Spawn a subagent |
| `MCP` (dispatcher) | — | — | Meta-tool that routes to MCP-provided tools |
| `Brief` | — | — | KAIROS feature — compact communication channel |
| `SendUserFile` | — | — | KAIROS feature — file delivery channel |
| `SyntheticOutput` | Yes | Yes | Structured JSON output (SDK/headless only) |

> Note: Exact set varies by feature flags (KAIROS, FORK_SUBAGENT), platform (Windows), and session mode (interactive vs SDK).

### Group B: Deferred Tools (`shouldDefer: true`)

These exist as **name stubs only** in the system prompt. The LLM must call ToolSearch to load their full schemas before it can use them.

| Tool Name | `shouldDefer` Source | Purpose |
|---|---|---|
| `WebFetch` | `WebFetchTool.ts` | Fetch a URL, return content |
| `WebSearch` | `WebSearchTool.ts` | Search the web |
| `NotebookEdit` | `NotebookEditTool.ts` | Edit Jupyter notebook cells |
| `AskUserQuestion` | `AskUserQuestionTool.tsx` | Ask user a question mid-task |
| `TaskCreate` | `TaskCreateTool.ts` | Create a background agent task |
| `TaskGet` | `TaskGetTool.ts` | Get status of a task |
| `TaskUpdate` | `TaskUpdateTool.ts` | Update a task |
| `TaskStop` | `TaskStopTool.ts` | Abort/kill a running task |
| `TaskList` | `TaskListTool.ts` | List all tasks |
| `TaskOutput` | `TaskOutputTool.tsx` | Get output delta from a task |
| `SendMessage` | `SendMessageTool.ts` | Send message to a teammate |
| `TeamCreate` | `TeamCreateTool.ts` | Create an agent team |
| `TeamDelete` | `TeamDeleteTool.ts` | Disband an agent team |
| `EnterWorktree` | `EnterWorktreeTool.ts` | Enter a git worktree |
| `ExitWorktree` | `ExitWorktreeTool.ts` | Exit a git worktree |
| `ExitPlanMode` | `ExitPlanModeV2Tool.ts` | Exit plan mode |
| `Config` | `ConfigTool.ts` | Read/write Claude Code settings |
| `CronCreate` | `CronCreateTool.ts` | Create a scheduled cron job |
| `CronDelete` | `CronDeleteTool.ts` | Delete a cron job |
| `CronList` | `CronListTool.ts` | List cron jobs |
| `LSP` | `LSPTool.ts` | Language server protocol (code intelligence) |
| `ListMcpResources` | `ListMcpResourcesTool.ts` | List MCP server resources |
| `ReadMcpResource` | `ReadMcpResourceTool.ts` | Read a specific MCP resource |
| `RemoteTrigger` | `RemoteTriggerTool.ts` | Trigger remote execution |
| `TodoWrite` | `TodoWriteTool.ts` | Write todo items |
| `Sleep` | `SleepTool.ts` | Pause/wait for a duration |

### Group C: MCP Tools (Always Deferred, Dynamic)

- Naming convention: `mcp__{server_name}__{tool_name}`
- Examples: `mcp__github__create_issue`, `mcp__slack__send_message`, `mcp__filesystem__read_file`
- **Always deferred** — hardcoded in `isDeferredTool()` at `tools/ToolSearchTool/prompt.ts:68`
- Only exception: MCP tool sets `alwaysLoad: true` in `_meta['anthropic/alwaysLoad']`
- Not known at startup — registered dynamically as MCP servers connect
- Announced as names in `<system-reminder>` messages

---

## 4. Deferred vs Always-Loaded: The Token Budget Decision

The split is entirely a **token cost optimization**. Full JSON schemas are expensive. The always-loaded set contains tools the model will almost certainly use in any session. The deferred set contains tools that are situational.

```
WITHOUT deferred loading (hypothetical):
┌─────────────────────────────────────────────────────────────┐
│ System prompt: 54 full tool schemas                         │
│ Approximate token cost: ~50,000–80,000 tokens               │
│ → Paid on EVERY SINGLE API CALL                             │
│ → Even when 40 of those tools are never used                │
└─────────────────────────────────────────────────────────────┘

WITH deferred loading (actual):
┌─────────────────────────────────────────────────────────────┐
│ System prompt: ~15 full schemas + ~25 names only            │
│ Approximate token cost: ~20,000–25,000 tokens               │
│ → Extra cost when needed: 1 ToolSearch call (~200 tokens)   │
│ → Net saving: 30,000–55,000 tokens per call on average      │
└─────────────────────────────────────────────────────────────┘
```

### The `isDeferredTool()` Decision Logic

From `tools/ToolSearchTool/prompt.ts:62`:

```python
def is_deferred_tool(tool) -> bool:
    # 1. Explicit opt-out (MCP tool with alwaysLoad: true)
    if tool.always_load == True:
        return False

    # 2. All MCP tools are always deferred
    if tool.is_mcp == True:
        return True

    # 3. ToolSearch itself is never deferred — model needs it to load everything else
    if tool.name == "ToolSearch":
        return False

    # 4. Agent tool: never deferred when FORK_SUBAGENT feature is active
    #    (fork mode needs Agent available on turn 1)
    if feature('FORK_SUBAGENT') and tool.name == "Agent":
        return False

    # 5. Brief and SendUserFile: communication channels, must be available immediately
    if tool.name in [BRIEF_TOOL_NAME, SEND_USER_FILE_TOOL_NAME]:
        return False

    # 6. Regular tools: deferred if they opted in
    return tool.should_defer == True
```

### How Deferred Tools Are Announced

Two mechanisms (based on feature flag `tengu_glacier_2xr`):

**Old behavior (pre-flag):**
```xml
<available-deferred-tools>
WebFetch
WebSearch
TaskCreate
TaskGet
...
</available-deferred-tools>
```

**New behavior (delta-enabled):**
```xml
<system-reminder>
The following deferred tools are now available via ToolSearch. Their schemas
are NOT loaded — calling them directly will fail with InputValidationError.
Use ToolSearch with query "select:<name>[,<name>...]" to load tool schemas
before calling them:
WebFetch
WebSearch
TaskCreate
...
</system-reminder>
```

---

## 5. How Deferred Tools Get Loaded: ToolSearch

ToolSearch is the **meta-tool** that loads other tools' schemas on demand. It is always loaded (never deferred) because the model needs it to discover everything else.

### Two Query Modes

**Mode 1: Direct select (exact names)**
```
Model calls: ToolSearch(query="select:TaskCreate,TaskUpdate")
Result:
<functions>
{"name": "TaskCreate", "description": "...", "parameters": {...}}
{"name": "TaskUpdate", "description": "...", "parameters": {...}}
</functions>
```

**Mode 2: Keyword search**
```
Model calls: ToolSearch(query="notebook jupyter")
Result: matches ranked by score
<functions>
{"name": "NotebookEdit", "description": "...", "parameters": {...}}
</functions>
```

### Keyword Search Scoring

```python
# Score per term, per tool:
if term in tool_name_parts:
    score += 12 if is_mcp else 10   # MCP server names ranked higher
elif term in tool_name_parts (partial):
    score += 6 if is_mcp else 5

if term in tool.search_hint:
    score += 4                       # Curated hint, high signal

if term in tool.description:
    score += 2                       # Description, lower signal
```

### What the Model Gets Back

```python
# ToolSearch returns tool_reference blocks, NOT text:
{
    "type": "tool_result",
    "tool_use_id": "...",
    "content": [
        {"type": "tool_reference", "tool_name": "TaskCreate"},
        {"type": "tool_reference", "tool_name": "TaskUpdate"}
    ]
}
# The API resolves tool_reference → full schema on the server side
# Model now sees the full schemas and can call those tools
```

### The Two-Step Flow for a Deferred Tool

```
Turn N:
  Model: "I need to create a task"
  Model sees "TaskCreate" in deferred tool list
    │
    ▼
  Model calls: ToolSearch(query="select:TaskCreate")
    │
    ▼
  Harness returns: full TaskCreate schema as tool_reference
    │
    ▼
Turn N+1:
  Model now has full schema in context
  Model calls: TaskCreate(title="Research auth patterns", ...)
    │
    ▼
  Harness executes: creates the task
  Returns: task ID and status
```

### MCP Pending State

If an MCP server is still connecting when ToolSearch is called:
```
No matching deferred tools found. Some MCP servers are still connecting:
github-server, slack-server. Their tools will become available shortly
— try searching again.
```

---

## 6. MCP Tools: Always Deferred, Always Dynamic

MCP (Model Context Protocol) tools come from external servers connected to Claude Code. They are:

- Named: `mcp__{server_name}__{tool_name}` (e.g., `mcp__github__create_issue`)
- Always deferred — never in the initial system prompt with full schema
- Discovered via ToolSearch like any other deferred tool
- The search ranking gives MCP tools **higher scores** for server-name matches (weight 12 vs 10)

### MCP Tool Lifecycle

```
1. MCP server connects (stdio, SSE, HTTP, WebSocket)
   → AppState.mcp.clients adds the connection

2. Tools discovered from server
   → AppState.mcp.tools populated with mcp__server__tool entries

3. Tool names announced in <system-reminder> as deferred tools

4. Model calls ToolSearch("slack") to find slack tools
   → Returns schemas for mcp__slack__send_message, mcp__slack__list_channels, etc.

5. Model calls mcp__slack__send_message({channel: "#general", text: "..."})
   → Harness routes to the MCP server via the connection
   → Server executes, returns result
   → Result delivered back to model as tool_result
```

### MCP Tool Filtering

From `tools.ts`:
```python
def assemble_tool_pool(permission_context, mcp_tools):
    built_in = get_tools(permission_context)

    # Filter MCP tools by deny rules first
    allowed_mcp = filter_by_deny_rules(mcp_tools, permission_context)

    # MCP tools appended AFTER built-ins — never interleaved
    # (interleaving would invalidate prompt cache keys)
    return deduplicate(built_in + allowed_mcp)
```

**Why MCP tools are appended after, never interleaved:** Insertion into the sorted built-in list would change cache keys for all downstream tools, breaking the prompt cache. Appending at the end is cache-safe.

---

## 7. Token Memory Breakdown

### Per-Call Token Budget (200K window model)

```
┌─────────────────────────────────────────────────────────────────┐
│                    SYSTEM PROMPT (stable prefix)                │
│                                                                 │
│  Base instructions + safety rules         ~2,000–3,000 tokens  │
│  Git status (truncated to 2,000 chars)      ~500–1,000 tokens  │
│  CLAUDE.md hierarchy (all 4 levels)       ~1,000–5,000 tokens  │
│  Memory mechanics instructions                   ~500 tokens   │
│  Environment info (date, platform)               ~200 tokens   │
│                                         ─────────────────────  │
│  Subtotal: base system                  ~4,000–10,000 tokens   │
│                                                                 │
│  ~15 always-loaded tool schemas        ~10,000–15,000 tokens   │
│  Deferred tool name stubs                      ~500 tokens     │
│                                         ─────────────────────  │
│  TOTAL SYSTEM + TOOLS (cached prefix)  ~15,000–26,000 tokens   │
│                                                                 │
├─────────────────────────────────────────────────────────────────┤
│                    CONVERSATION (grows each turn)               │
│                                                                 │
│  Prior messages + tool results         Grows ~5,000–15,000/turn │
│  Current user message                     ~100–2,000 tokens    │
│                                                                 │
├─────────────────────────────────────────────────────────────────┤
│  AUTO-COMPACT THRESHOLD (200K window): ~187,000 tokens          │
│  (contextWindow - 20K output reserve - 13K buffer)             │
└─────────────────────────────────────────────────────────────────┘
```

### What Adding a Deferred Tool Costs

```
Loading 1 deferred tool via ToolSearch:
  - ToolSearch call input:  ~50 tokens (query)
  - ToolSearch result:      ~500–2,000 tokens (schema returned)
  - Net cost:               ~550–2,050 tokens

Vs. having it always-loaded:
  - 550–2,050 tokens on EVERY call, even when never used
  - With deferred loading: only paid when tool is actually needed
```

### Token Math Constants (from `constants/toolLimits.ts`)

```python
BYTES_PER_TOKEN = 4                     # Conservative estimate
MAX_TOOL_RESULT_TOKENS = 100_000        # Per-tool result cap
MAX_TOOL_RESULT_BYTES = 400_000         # = 100K tokens × 4 bytes
DEFAULT_MAX_RESULT_SIZE_CHARS = 50_000  # Disk offload threshold
MAX_TOOL_RESULTS_PER_MESSAGE = 200_000  # Per-turn aggregate cap (chars)
TOOL_SUMMARY_MAX_LENGTH = 50            # Display summary truncation
```

### Caching Impact

The system prompt + always-loaded tools (~15K–25K tokens) is the **stable prefix** — identical every call, highly cacheable. With prompt caching:
- First call: full price (~25K × $5/Mtok = $0.125 for Opus)
- Every subsequent call: cache hit → 10x cheaper ($0.0125)
- This is why tool schemas are **sorted by name** — adding a new tool to an alphabetical list is cache-safe; adding to an arbitrary order would shift all downstream tools and bust the cache

---

## 8. The Tool Execution Pipeline: Step by Step

From `services/tools/toolExecution.ts`:

```
LLM outputs tool_use block:
  {"type": "tool_use", "id": "tu_abc123", "name": "Bash", "input": {"command": "ls -la"}}
          │
          ▼
Step 1: TOOL LOOKUP
  findToolByName(tools, "Bash")
  → If not found: check aliases (deprecated names like KillShell → TaskStop)
  → If still not found: return error tool_result to LLM
          │
          ▼
Step 2: ABORT CHECK
  if abortController.signal.aborted:
      → return CANCEL_MESSAGE tool_result, stop processing
          │
          ▼
Step 3: SCHEMA VALIDATION (zod)
  tool.inputSchema.safeParse(input)
  → If invalid: return InputValidationError tool_result to LLM
  → Includes hint if schema wasn't sent (deferred tool called without ToolSearch)
          │
          ▼
Step 4: SEMANTIC VALIDATION
  tool.validateInput(parsedInput, context)
  → Tool-specific logic: path exists? file readable? timeout valid?
  → If invalid: return custom error tool_result to LLM
          │
          ▼
Step 5: PRE-TOOL HOOKS (runPreToolUseHooks)
  → External hooks (shell, LLM, webhook) run first
  → Hook can:
      "continue": True   → proceed
      "continue": False  → block with hook message
      updatedInput       → modify tool input before execution
      permissionDecision → short-circuit to permission result
          │
          ▼
Step 6: PERMISSION CHECK (canUseTool)
  Simplified summary — see notes/security-layer.md for the full 11-step inner
  cascade + 6-step wrapper, all 7 modes, classifier flow, and router seams.

  Inner cascade (hasPermissionsToUseToolInner, permissions.ts:1158):
  1a. Tool-wide DENY rule → deny
  1b. Tool-wide ASK rule  → ask (sandbox bypass possible for Bash)
  1c. tool.checkPermissions(input)
  1d. Tool said deny → deny
  1e. Tool requires UI + ask → ask (bypass-immune)
  1f. Content-specific ask rule from tool → ask (bypass-immune)
  1g. safetyCheck reason (.git/, .claude/, dotfiles) → ask (bypass-immune)
  2a. mode == bypassPermissions → allow
  2b. Tool-wide ALLOW rule → allow
  3.  passthrough → ask

  Wrapper (hasPermissionsToUseTool, permissions.ts:473) then handles:
  - dontAsk mode: ask → deny coercion (NOT auto-allow as previously noted)
  - auto mode: acceptEdits fast-path probe → safe-tool allowlist → 2-stage YOLO
  - Headless agent: PermissionRequest hook chain before fallback deny

  For Bash: classifier is async side-query; first response wins (race for speed)
          │
          ▼
Step 7: TOOL EXECUTION
  result = await tool.call(validatedInput, context, progressCallback)
  → Real I/O happens here: disk reads, shell commands, web requests
  → Progress events streamed back during long-running tools
          │
          ▼
Step 8: RESULT MAPPING
  mapped = tool.mapToolResultToToolResultBlockParam(result, toolUseId)
  → Converts to API format: {type: "tool_result", tool_use_id: ..., content: ...}
          │
          ▼
Step 9: RESULT BUDGETING (applyToolResultBudget)
  → Per-tool limit: check result size vs tool.maxResultSizeChars
  → If > threshold: offload to disk, replace with preview + file path
  → Per-message aggregate: check if this turn's total exceeds 200K chars
  → If > 200K: largest results offloaded until under budget
          │
          ▼
Step 10: POST-TOOL HOOKS (runPostToolUseHooks)
  → Runs after successful execution
  → Can transform output, log results, trigger side effects
  → Failure path: runPostToolUseFailureHooks (on tool error)
          │
          ▼
Step 11: RESULT RETURNED TO LLM
  Tool result injected as user-role message:
  {"role": "user", "content": [{"type": "tool_result", ...}]}
  LLM generates again with result in context
```

### Python Analogy for the Full Pipeline

```python
def execute_tool(tool_name, input, context):
    # Step 1: Lookup
    tool = tool_registry.get(tool_name)
    if not tool:
        return error_result("No such tool: " + tool_name)

    # Step 3: Schema validation
    validated = tool.schema.parse(input)
    if not validated.ok:
        return error_result("InputValidationError: " + validated.error)

    # Step 4: Semantic validation
    semantic = tool.validate_input(validated.data, context)
    if not semantic.ok:
        return error_result(semantic.message)

    # Step 5: Pre-hooks
    for hook in pre_tool_hooks:
        decision = hook.run(tool_name, validated.data)
        if decision.blocks:
            return error_result(decision.message)
        if decision.modifies_input:
            validated.data = decision.updated_input

    # Step 6: Permission
    permission = check_permission(tool, validated.data, context)
    if permission.behavior == "deny":
        return error_result("Permission denied")
    if permission.behavior == "ask":
        approved = ask_user(tool_name, validated.data)
        if not approved:
            return error_result("User denied")

    # Step 7: Execute
    result = tool.call(validated.data, context)

    # Step 8-9: Map + budget
    mapped = tool.map_to_api_format(result)
    mapped = apply_budget(mapped, tool.max_result_size_chars)

    # Step 10: Post-hooks
    for hook in post_tool_hooks:
        hook.run(tool_name, result)

    return mapped
```

---

## 9. Concurrency: Parallel vs Serial Execution

When the LLM outputs **multiple tool_use blocks in a single response**, Claude Code decides which to run in parallel and which to serialize based on `isConcurrencySafe()`.

### Example: LLM Outputs Three Tools at Once

```
LLM response:
  tool_use: Read(file_path="/src/auth.py")
  tool_use: Grep(pattern="import", directory="/src")
  tool_use: Bash(command="git status")
```

```python
tool_calls = [Read(...), Grep(...), Bash(...)]

safe_tools   = [t for t in tool_calls if t.is_concurrency_safe()]
# → [Read, Grep]   (read-only, concurrent-safe)

unsafe_tools = [t for t in tool_calls if not t.is_concurrency_safe()]
# → [Bash]         (writes to shell state, not concurrent-safe)

# Run safe ones in parallel (asyncio.gather equivalent)
results_safe = await asyncio.gather(
    execute(Read),
    execute(Grep)
)

# Run unsafe ones one at a time
for tool in unsafe_tools:
    result = await execute(tool)
```

### Concurrency Classification by Tool

| Concurrent (safe) | Serial (not safe) |
|---|---|
| `Read` | `Bash` |
| `Glob` | `Edit` |
| `Grep` | `Write` |
| `ToolSearch` | `NotebookEdit` |
| `WebFetch` | `Agent` |
| `WebSearch` | `SendMessage` |
| `SyntheticOutput` | `TaskCreate/Stop/Update` |
| `ListMcpResources` | `TeamCreate/Delete` |
| `ReadMcpResource` | `Config` |
| MCP tools (varies) | `EnterPlanMode/Worktree` |

**Why serializing writes matters:** Two concurrent `Edit` calls on the same file would produce a race condition — last write wins, first write lost. Serialization prevents this.

---

## 10. Tool Result Budgeting and Disk Offloading

Tool results go back into the conversation as messages. Large results bloat the context window, consuming tokens on every subsequent call. Claude Code has a multi-layer budget system to prevent this.

### Layer 1: Per-Tool Result Cap

Each tool declares `maxResultSizeChars`. If the result exceeds this:

```
Result is OFFLOADED to disk:
  ~/.claude/projects/{project-hash}/{sessionId}/tool-results/{id}.txt

The model receives:
  <persisted-output>
  Tool result was too large (152,000 chars). Saved to:
  /home/user/.claude/projects/.../tool-results/abc123.txt
  Preview (first 500 chars):
  [content preview here...]
  </persisted-output>
```

**Python analogy:**
```python
def apply_per_tool_budget(result, tool_max_size_chars):
    if len(result) <= min(tool_max_size_chars, DEFAULT_MAX_RESULT_SIZE_CHARS):
        return result  # fits in context, return directly

    # Too large — save to disk
    file_path = save_to_disk(result)
    return f"<persisted-output>Saved to {file_path}. Preview: {result[:500]}</persisted-output>"
```

**Exception:** `Read` tool has `maxResultSizeChars = Infinity`. Why? It self-bounds via its own token budget logic (`maxTokens`) — the tool itself handles large files, so the generic offload mechanism is disabled for it.

### Layer 2: Per-Message Aggregate Cap (200K chars)

When the LLM calls multiple tools in one turn (parallel tools), their results all arrive in ONE user message. Even if each result is under its per-tool limit, their combined size could be enormous.

```python
PER_MESSAGE_BUDGET = 200_000  # chars (~50K tokens)

def apply_per_message_budget(all_tool_results_this_turn):
    total = sum(len(r) for r in all_tool_results_this_turn)

    while total > PER_MESSAGE_BUDGET:
        # Find the largest result and offload it
        largest = max(all_tool_results_this_turn, key=len)
        offloaded = offload_to_disk(largest)
        all_tool_results_this_turn[largest.index] = offloaded
        total = sum(len(r) for r in all_tool_results_this_turn)

    return all_tool_results_this_turn
```

**Example:** 5 parallel tools each returning 50K chars = 250K total → exceeds 200K → largest result offloaded until under budget.

### Layer 3: GrowthBook Override

Per-tool thresholds can be overridden at runtime via the `tengu_satin_quoll` GrowthBook flag:
- Map of `{tool_name: threshold_chars}`
- Bypasses the `Math.min(declared, 50K)` clamp
- Allows Anthropic to tune limits without code deploys

### What Happens to Old Tool Results (Microcompact)

Old tool results from earlier turns (not the current turn) are handled by the **microcompact** stage in the 5-stage compaction pipeline:
- Replaces old tool results with `[Old tool result content cleared]` placeholder
- The actual content stays on disk — model can re-read via `Read` tool if needed
- Free (no API call) — just in-memory text replacement before sending to API

---

## 11. Per-Tool Result Size Limits

From source code inspection:

| Tool | `maxResultSizeChars` | Effective Limit | Notes |
|---|---|---|---|
| `Read` | `Infinity` | Self-bounded | Tool handles its own truncation via maxTokens |
| `Edit` | `100_000` | 50,000 (clamped) | Edit confirmation messages |
| `Write` | `100_000` | 50,000 (clamped) | Write confirmation messages |
| `Glob` | `100_000` | 50,000 (clamped) | File list results |
| `Grep` | `20_000` | 20,000 | Tightest cap — grep results very context-dense |
| `WebFetch` | `100_000` | 50,000 (clamped) | Fetched page content |
| `WebSearch` | `100_000` | 50,000 (clamped) | Search results |
| `ToolSearch` | `100_000` | 50,000 (clamped) | Tool schemas returned |
| MCP tools | Varies | 50,000 (default cap) | Can be overridden via GB flag |

**The clamp:** `Math.min(tool.maxResultSizeChars, DEFAULT_MAX_RESULT_SIZE_CHARS)` — so declaring 100K doesn't mean 100K; it's still clamped to 50K unless overridden.

**Token conversion:** 50,000 chars ÷ 4 bytes/token ≈ **12,500 tokens** per tool result at the default cap.

**Per-message max:** 200,000 chars ÷ 4 ≈ **50,000 tokens** total for all parallel tool results in one turn.

---

## 12. Tool Pool Assembly: How the Final List Is Built

```
getAllBaseTools()
  → Returns ALL possible built-in tools (feature-gated)
  → ~54 tools before filtering
          │
          ▼
getTools(permissionContext)
  → Filters by:
     - Permission mode (SIMPLE mode → only Bash/Read/Edit)
     - Deny rules (blanket blocks from settings)
     - isEnabled() check (feature flags, platform, session type)
     - REPL filtering (some tools hidden in REPL mode)
  → Returns: filtered built-in tool list
          │
          ▼
assembleToolPool(permissionContext, mcpTools)
  → Gets filtered built-in tools via getTools()
  → Filters MCP tools by deny rules
  → Appends MCP tools AFTER built-ins (never interleaved — cache safety)
  → Deduplicates by name
  → Returns: final tool pool (what the model sees)
```

### Mode-Based Filtering

| Permission Mode | Tool Access |
|---|---|
| `plan` | All tools, but execution blocked until plan approved |
| `default` | Full tool pool |
| `acceptEdits` | Full tool pool, file edits auto-approved |
| `auto` | Full tool pool, ML classifier gates execution |
| `dontAsk` | Full tool pool, no prompts |
| `bypassPermissions` | Full tool pool, minimal checks |
| `SIMPLE` mode | Only: Bash, Read, Edit (minimal footprint) |

### Why Built-ins Are Sorted by Name

Tools in the system prompt are sorted alphabetically by name before being sent to the API. This ensures that adding, removing, or enabling a new tool doesn't shift the position of existing tools in the prompt. Position stability = cache key stability = cache hits preserved.

```python
# What would break cache:
tools_unsorted = [WebFetch, Bash, Read, Edit, ...]  # arbitrary order
# Adding "Agent" anywhere in the middle shifts all tools after it → cache miss

# What Claude Code does:
tools_sorted = sorted(tools, key=lambda t: t.name)
# Adding "Agent" inserts at the correct alphabetical position
# Only tools after "Agent" in the alphabet need new cache entries
```

---

## 13. How Tools Are Called: The Full LLM Round-Trip

End-to-end example: user asks "find all TODO comments in the src directory."

```
Turn 1:
──────
User message:        "find all TODO comments in the src directory"

System prompt sent:  [Base instructions] + [Tool schemas: Read, Bash, Grep, ...] +
                     [Deferred names: WebFetch, TaskCreate, ...] +
                     [Conversation: just the user message]

LLM generates:
  {"type": "tool_use",
   "id": "tu_001",
   "name": "Grep",
   "input": {"pattern": "TODO", "directory": "/src", "recursive": true}}

Harness:
  → Finds Grep tool in pool (already loaded, not deferred)
  → Validates input schema ✓
  → Checks permission: Grep is read-only → auto-allowed
  → Executes: grep -r "TODO" /src
  → Result: "src/auth.py:42: # TODO: invalidate token on logout\n
             src/api.py:87:  # TODO: add rate limiting\n"
  → Size: 200 chars → under limit → no offload
  → Formatted as tool_result

Turn 2:
──────
Messages sent to API:
  [System prompt + tools]
  [User: "find all TODO comments"]
  [Assistant: tool_use Grep]
  [User: tool_result "src/auth.py:42: # TODO..."]   ← result injected here

LLM generates:
  "I found 2 TODO comments:
   - `src/auth.py:42`: Token invalidation on logout
   - `src/api.py:87`: Rate limiting
   
   stop_reason: end_turn"

Done — no more tool calls.
```

### When Deferred Tool Is Needed

```
Turn 1:
──────
User: "search the web for latest OAuth 2.0 best practices"

LLM sees "WebSearch" in the deferred names list.
LLM generates:
  {"type": "tool_use", "name": "ToolSearch", "input": {"query": "select:WebSearch"}}

Harness: executes ToolSearch, returns:
  <functions>
  {"name": "WebSearch", "description": "...", "parameters": {"query": ...}}
  </functions>

Turn 2:
──────
LLM now has WebSearch schema in context.
LLM generates:
  {"type": "tool_use", "name": "WebSearch",
   "input": {"query": "OAuth 2.0 best practices 2026"}}

Harness: executes WebSearch, returns results.

Turn 3:
──────
LLM synthesizes results and responds to user.
```

---

## 14. Error Handling in Tool Calls

### Schema Validation Failure

```python
# Model called Edit without required old_string parameter
# LLM gets back:
{
    "type": "tool_result",
    "is_error": True,
    "content": "<tool_use_error>InputValidationError: old_string is required</tool_use_error>"
}
# LLM sees this and typically retries with correct parameters
```

### Deferred Tool Called Without Schema

```python
# Model tried to call TaskCreate directly (without ToolSearch first)
# InputValidationError + special hint:
"InputValidationError: missing required fields.
 Note: TaskCreate is a deferred tool. Its schema was not sent in this context.
 Use ToolSearch(query='select:TaskCreate') to load the schema first."
```

### Tool Not Found

```python
# Model hallucinated a tool name "FileSearch" that doesn't exist
{
    "type": "tool_result",
    "is_error": True,
    "content": "<tool_use_error>Error: No such tool available: FileSearch</tool_use_error>"
}
# Checked against deprecated aliases first (KillShell → TaskStop)
```

### Permission Denied

```python
# User denied Bash command permission
{
    "type": "tool_result",
    "content": "Tool execution cancelled by user"
}
# Model sees this and stops or tries a different approach
```

### Abort (ESC Pressed)

```python
# User pressed ESC mid-execution
{
    "type": "tool_result",
    "content": "[Cancelled]"  # CANCEL_MESSAGE constant
}
```

---

## 15. Tool Aliases and Deprecated Names

Tools can have aliases for backward compatibility. The `findToolByName` fallback checks aliases when the primary name isn't found.

| Current Name | Deprecated Alias | Source |
|---|---|---|
| `TaskStop` | `KillShell` | `TaskStopTool.ts:44` |
| `Brief` | `LEGACY_BRIEF_TOOL_NAME` | `BriefTool.ts:138` |

### How Aliases Work in Execution

```python
# Tool lookup sequence:
tool = find_by_name(available_tools, tool_name)

if not tool:
    # Check if it's a deprecated alias in ALL tools (not just available)
    fallback = find_by_name(all_base_tools, tool_name)
    # Only use fallback if the PRIMARY name is different (it was found via alias)
    if fallback and tool_name in fallback.aliases:
        tool = fallback
    # This handles old transcripts/sessions using deprecated tool names
```

---

## 16. Key Source Code References

| Component | File | Key Lines | What's There |
|---|---|---|---|
| Tool interface | `Tool.ts` | 362-695 | Full `Tool` type definition |
| Tool builder | `Tool.ts` | 783 | `buildTool()` helper |
| Concurrency flags | `Tool.ts` | 402-404 | `isConcurrencySafe`, `isReadOnly` |
| Defer flag | `Tool.ts` | 442-449 | `shouldDefer`, `alwaysLoad` |
| Tool registry | `tools.ts` | 193 | `getAllBaseTools()` |
| Tool filtering | `tools.ts` | 271 | `getTools()` |
| Tool pool assembly | `tools.ts` | 337-388 | `assembleToolPool()` |
| Deferred logic | `tools/ToolSearchTool/prompt.ts` | 62-108 | `isDeferredTool()` |
| ToolSearch impl | `tools/ToolSearchTool/ToolSearchTool.ts` | 304-471 | Full ToolSearch tool |
| Keyword scoring | `tools/ToolSearchTool/ToolSearchTool.ts` | 259-302 | Score weights (12/10/6/5/4/2) |
| Execution pipeline | `services/tools/toolExecution.ts` | 599-800 | `checkPermissionsAndCallTool()` |
| Result budgeting | `utils/toolResultStorage.ts` | 203-330 | `mapToolResultToToolResultBlockParam()` |
| Per-message budget | `utils/toolResultStorage.ts` | 740-810 | Aggregate budget enforcement |
| Budget constants | `constants/toolLimits.ts` | all | All size limits |
| MCP tool filtering | `tools.ts` | 347-370 | `assembleToolPool()` MCP section |
| Pre-tool hooks | `services/tools/toolExecution.ts` | 800 | `runPreToolUseHooks()` |
| Post-tool hooks | `services/tools/toolExecution.ts` | 1483 | `runPostToolUseHooks()` |
| TaskStop alias | `tools/TaskStopTool/TaskStopTool.ts` | 44 | `aliases: ['KillShell']` |
| Grep result cap | `tools/GrepTool/GrepTool.ts` | 164 | `maxResultSizeChars: 20_000` |
| Read: Infinity | `tools/FileReadTool/FileReadTool.ts` | 342 | `maxResultSizeChars: Infinity` |
| MCP always deferred | `tools/ToolSearchTool/prompt.ts` | 68 | `if (tool.isMcp) return true` |

---

## Quick Reference: All Size Limits

```
Per-tool result cap (default):   50,000 chars  ≈ 12,500 tokens
Per-tool result cap (declared):  100,000 chars (clamped to 50K by default)
Grep (tightest):                  20,000 chars  ≈  5,000 tokens
Read (no cap):                   Infinity       (self-bounds via maxTokens)
Per-message aggregate:           200,000 chars  ≈ 50,000 tokens
Max tool result (absolute):      400,000 bytes  = 100,000 tokens
Bytes per token estimate:              4 bytes
Autocompact trigger (200K window): 187,000 tokens
```

---

## 17. Memory Bloat and Design Gaps

> Analysis of where the tool system accumulates cost silently, especially in long sessions
> or heavy MCP usage. All findings backed by source code evidence.

### Structural Pattern Behind All Gaps

Every gap below shares one root cause: **the tool system is designed around single-server, single-session, interactive use**. The mitigations that exist (per-message budget, microcompact, compaction) are reactive — they fire after bloat has already happened. None of them are preventive quotas. As you connect more MCP servers, run longer sessions, or use heavy parallel tool patterns, multiple unbounded accumulators compound simultaneously.

---

### Gap 1: MCP Tool Schema Accumulation — No Cap

**Source:** `services/mcp/client.ts:2171`, `state/AppStateStore.ts:173`

`appState.mcp.tools` is a plain `Tool[]` with **zero size limit**. Every tool from every connected MCP server lands here unconditionally.

```python
# What happens at scale:
mcp_servers = 5
tools_per_server = 20
avg_schema_size_tokens = 200

total_mcp_tokens = 5 * 20 * 200  # = 20,000 tokens for schemas in memory

# MCP tools are always deferred — names only in prompt (safe)
# The real damage: if model runs ToolSearch("send") and 20 MCP tools match,
# ToolSearch returns 20 full schemas as a single tool_result:
# 20 × 500 tokens = 10,000 tokens in one tool_result, sitting in history forever

# Worst case: OpenAPI-generated MCP server (Stripe, GitHub full API)
# = 200+ endpoints per server × 5 servers = 1,000 MCP tool names registered
```

**Concrete accumulation:**
- Each ToolSearch result that returns MCP schemas stays in conversation history
- In a 30-turn session using many MCP tools, ToolSearch call results alone can add 30K–50K tokens to history
- Those tokens re-sent on EVERY subsequent API call

**What's missing:** No per-server tool quota, no total MCP tool cap, no warning when the MCP tool set is large, no trimming of stale server registrations.

---

### Gap 2: Tool Result Disk Files — No Cleanup

**Source:** `utils/toolResultStorage.ts:103-184`, `services/compact/postCompactCleanup.ts`

Every tool result that exceeds the offload threshold is written to disk:
```
~/.claude/projects/{hash}/{sessionId}/tool-results/{toolUseId}.txt
```

```python
# Long session scenario:
turns = 100
offloaded_per_turn = 2          # conservative
avg_offload_size_chars = 80_000

total_disk_chars = 100 * 2 * 80_000  # = 16,000,000 chars = ~16MB per session

# These files are NEVER deleted during the session.
# postCompactCleanup.ts exists but does not document tool-result file deletion.
# Files persist until the user manually prunes ~/.claude/

# For a power user running daily:
# 5 sessions/week × 52 weeks × 16MB = 4GB of orphaned tool result files/year
```

**What's missing:** No session-end cleanup hook. No TTL or `--prune` mechanism. No documentation of when these files are safe to delete.

---

### Gap 3: File State Cache — 25MB Per Agent Spawn

**Source:** `utils/fileStateCache.ts:22`

```python
DEFAULT_MAX_CACHE_SIZE_BYTES = 25 * 1024 * 1024  # 25MB per agent instance

# Each agent spawn CLONES (copies) this cache, not shares it:
# cloneFileStateCache(toolUseContext.readFileState)

# Coordinator pattern with 10 workers:
total_ram = (1 + 10) * 25  # = 275MB in Node.js heap

# Caches are never GC'd when agents complete — held until session end
# In a session with many compactions + re-spawns, memory pressure compounds
```

**Root cause:** Cache cloning (copy-on-write intent but always copies) instead of a shared LRU with per-agent views. Should use a shared cache with read-only references per agent — agent-specific writes go to a small per-agent overlay, not a full 25MB clone.

---

### Gap 4: Image Results — Bypass the Budget System

**Source:** `utils/toolResultStorage.ts:518-529`, `tools/FileReadTool/FileReadTool.ts:342`

When `Read` opens an image file, it converts to base64 and sends **inline** to the API. The `contentSize()` function used for budgeting **excludes image blocks** — they are not counted toward the 200K per-message aggregate cap.

```python
# A 1MB PNG image:
# → Compressed via Sharp: ~300KB JPEG
# → Base64 encoded: ~400KB string
# → Token count: ~100,000 tokens

# This 100K-token image:
# 1. NOT counted in the 200K/message budget   ← escape hatch
# 2. NOT subject to the per-tool offload threshold (maxResultSizeChars = Infinity)
# 3. Sits in conversation FOREVER (images stripped at compaction, but until then...)
# 4. Re-sent on EVERY subsequent API call at full cost

# If model reads 3 screenshots in one turn:
tokens_per_image = 100_000
total = 3 * tokens_per_image    # = 300,000 tokens
cost_per_call_at_opus = 300_000 * 5 / 1_000_000  # = $1.50 per call
# Paid again on every single subsequent turn until compaction
```

**Images are a silent budget escape hatch.** A session reading multiple large images can blow far past the 200K budget cap because images bypass that check entirely.

**What's missing:** Image blocks should count toward the per-message budget. They should be offloadable to disk just like text results. `maxResultSizeChars = Infinity` for Read with no per-image cap is the design gap.

---

### Gap 5: ToolSearch History Accumulation

**Source:** `tools/ToolSearchTool/ToolSearchTool.ts:304-434`

Every ToolSearch call produces a `tool_use + tool_result` pair in the conversation history. These pairs are:
- Never deduplicated
- Never removed by microcompact (microcompact only strips non-Read tool results)
- Only cleared by full compaction

```python
# Long agentic session with many tool types:
tool_search_calls = 20
avg_toolsearch_result_tokens = 500  # returned schemas

tokens_in_history = 20 * 500       # = 10,000 tokens of ToolSearch overhead

# At turn 30, these 10,000 tokens are re-sent on every call:
extra_cost_tokens = 10_000 * 20    # = 200,000 tokens over 20 remaining turns
# At Opus: 200K × $5/Mtok = $1.00 in pure ToolSearch history overhead

# Post-compaction: model forgets tool schemas, must ToolSearch again
# → New ToolSearch calls added to fresh context
# → Doubles the accumulation across compaction boundaries in very long sessions
```

**What's missing:** ToolSearch results are idempotent — same query returns same schema. A deduplication layer that recognizes repeated loads of the same tool and collapses them into a single context entry would eliminate this.

---

### Gap 6: Deferred Schema Memoize — Never Cleared at Compaction

**Source:** `tools/ToolSearchTool/ToolSearchTool.ts:49-100`

```python
# Module-level memoize (process lifetime):
get_tool_description_memoized = memoize(
    fn=lambda tool_name, tools: tool.prompt(...),
    key=lambda tool_name, _: tool_name
)

# Cache cleared ONLY when:
# 1. Deferred tool set changes (MCP server reconnects with different tools)
# 2. clearToolSearchDescriptionCache() called explicitly

# NOT cleared at:
# - Compaction (the big context reset)
# - Agent spawn or completion
# - Session resume

# Result: RAM holds all tool descriptions loaded during the session
# even after compaction resets the context.
# Across a 6-hour session: 50 tools × 2KB each = 100KB of RAM leak
# Minor per session, but never released until process exits.
```

**What's missing:** `clearToolSearchDescriptionCache()` should be called at compaction boundaries. The function exists — it's just not wired to the compaction pipeline. A one-line fix.

---

### Gap 7: MCP Tool Name Collisions — Silent Drop

**Source:** `tools.ts:345-367`

```python
def assemble_tool_pool(permission_context, mcp_tools):
    built_ins = get_tools(permission_context)
    allowed_mcp = filter_by_deny_rules(mcp_tools, permission_context)
    combined = built_ins + allowed_mcp
    return uniq_by(combined, key=lambda t: t.name)  # first wins, rest silently dropped
```

If two MCP servers expose tools with the same name, the second is dropped without warning.

```python
# Scenario: two MCP servers both expose "search" or "read_file"
# mcp__filesystem__read_file and mcp__gdrive__read_file
# But a custom MCP server calls its tool just "read_file" (no server prefix)

# assembleToolPool():
# combined = [Read, Bash, ..., mcp__filesystem__read_file, custom_mcp__read_file]
# uniq_by(name) → custom_mcp__read_file silently dropped if same name as first

# Model calls read_file → only hits filesystem, never Google Drive
# No error, no warning, no log entry
```

**MCP tools using the `mcp__{server}__{tool}` convention are safe** — names are globally unique. But MCP servers that don't follow the convention, or whose tool names collide with built-in tool names like `Bash` or `Read`, are silently overridden.

**What's missing:** Collision detection with a warning log. Optionally: force-prefix all MCP tools that don't follow the convention.

---

### Gap 8: Bash Hang in Unattended/Headless Mode

**Source:** `tools/BashTool/BashTool.tsx:860-982`

```python
# The issue: Bash can block indefinitely
# e.g., command reads from stdin, or infinite loop with no output

# KAIROS mode mitigation (line 976-982):
# After 15s, auto-backgrounds command as "SleepingShell"
# BUT: this is feature-gated — default interactive mode has no auto-background

# Default behavior:
# - Hang until user presses ESC (interactive)
# - Hang until timeout expires (headless, up to 600 seconds)
# - In CLAUDE_CODE_UNATTENDED_RETRY mode: no user to press ESC
#   → session frozen until OS-level process dies or timeout hits

max_timeout = 600_000  # ms = 10 minutes, user-configurable
# A model that sets timeout=600000 can freeze a headless session for 10 minutes
# with no recovery path except external SIGKILL
```

**What's missing:** A hard system-enforced timeout independent of the user-specified `timeout` parameter. The shell process should be force-killed at a max cap regardless of what the model requests.

---

### Gap 9: Parallel Tool Result Previews Accumulate in History

**Source:** `utils/toolResultStorage.ts:769-900`

When the per-message 200K budget is exceeded, large results are offloaded to disk and replaced with previews. But the previews themselves stay in conversation history permanently:

```python
# Turn 5: 10 parallel tools, 7 offloaded
# Each offload creates a preview in the tool_result:
# "<persisted-output>Saved to .../tool-results/abc.txt. Preview: ..."

# This preview (~200 chars) stays in history forever
# At turn 30, 7 preview stubs × 25 turns of accumulation = 175 preview stubs
# Each re-sent on every subsequent API call

# 175 × 200 chars = 35,000 chars ≈ 8,750 tokens of dead preview stubs
# At Opus: 8,750 × $5/Mtok = $0.044 per call in dead preview overhead
# Over 30 remaining turns: $1.30 wasted on previews nobody reads
```

**Microcompact does clear old tool results** — but it replaces them with `[Old tool result content cleared]`, not removes them entirely. The preview stubs from offloaded results are also cleared, but only after they've accumulated across many turns.

---

### Summary Table: All Gaps

| Gap | Type | Severity | Mitigation Exists? | Cleared When |
|---|---|---|---|---|
| MCP tool schema accumulation | Context + RAM | **HIGH** | None | Server disconnect |
| Tool-result disk files | Disk | **HIGH** | None documented | Manual prune |
| File state cache per agent | RAM | **MEDIUM** | Session end only | Process exit |
| Image results bypass budget | Context | **MEDIUM** | Compaction strips | Next compact |
| ToolSearch history | Context | **MEDIUM** | Compaction | Next compact |
| Deferred schema memoize | RAM | **LOW** | Function exists, unwired | Process exit |
| MCP tool name collisions | Correctness | **MEDIUM** | None | Never |
| Bash hang (headless) | Liveness | **MEDIUM** | KAIROS only | Timeout / SIGKILL |
| Parallel result previews | Context | **LOW** | Microcompact (slow) | Next microcompact |

---

## 18. Routing Opportunities in the Tool System

> Where a model router can intercept the tool system to save cost, improve latency,
> or improve quality. All opportunities are grounded in the actual interception points
> from the source code.

---

### Opportunity 1: Route ToolSearch Calls to a Cheaper Model

**Why:** ToolSearch is a lookup operation. The model calls it to find and load tool schemas. This requires zero reasoning — it's keyword matching. Using Opus for ToolSearch is like using a senior engineer to look things up in a dictionary.

```python
# Router intercept point: per-iteration model in query.ts (Layer 6)
def route_model_for_turn(context):
    if context.last_tool_use == "ToolSearch":
        # Previous turn was a schema load — model is about to call the loaded tool
        # OR this turn IS a ToolSearch call
        return "haiku"   # $1/Mtok vs $5/Mtok for Opus
    return "opus"

# Savings: ToolSearch calls are ~100-500 tokens input, ~500 tokens output
# Haiku: (600 tokens × $1/Mtok) = $0.0006 vs Opus ($0.003) per ToolSearch call
# Across 20 ToolSearch calls in a session: saves $0.048 — small but free
```

**Interception point:** `getRuntimeMainLoopModel()` in `utils/model/model.ts:145` — change model per iteration based on what the previous turn produced.

---

### Opportunity 2: Preload Only Relevant Tool Schemas Based on Task Type

**Why:** The model currently uses ToolSearch reactively — it only discovers what it needs when it needs it. A router that understands the task upfront could preload the right schemas, eliminating ToolSearch round-trips entirely.

```python
# Router pre-classifies the task before the agent loop starts
def preload_tools_for_task(user_message):
    task_type = classify_task(user_message)

    if task_type == "web_research":
        preload = ["WebFetch", "WebSearch"]

    elif task_type == "notebook_analysis":
        preload = ["NotebookEdit", "WebFetch"]

    elif task_type == "agent_coordination":
        preload = ["TaskCreate", "TaskGet", "TaskUpdate", "TaskStop", "SendMessage"]

    elif task_type == "git_workflow":
        preload = ["Bash"]  # git commands via Bash, no special tools needed

    # Inject schemas directly into system prompt instead of waiting for ToolSearch
    inject_tool_schemas(preload)
    # Saves 1 round-trip per tool that would have been discovered via ToolSearch
```

**Interception point:** `getTools()` / `assembleToolPool()` in `tools.ts:271` and `tools.ts:337` — modify the tool pool before it's sent to the API.

**Cost saving:** Each avoided ToolSearch call saves 1 full turn (input + output tokens). In a 20-tool session, eliminating 15 ToolSearch calls = 15 fewer turns = significant savings on quadratic context growth.

---

### Opportunity 3: Cap MCP Tool Set Per Task

**Why:** Large MCP server sets (50+ tools) bloat ToolSearch results and make it harder for the model to find the right tool. A router can filter the MCP tool set down to only servers relevant to the current task.

```python
# Instead of registering ALL connected MCP servers:
# mcp_tools = [all 200 tools from 10 servers]

# Router restricts to relevant servers:
def get_relevant_mcp_tools(task_type, all_mcp_tools):
    relevant_servers = {
        "github_task":    ["mcp__github__*"],
        "slack_notify":   ["mcp__slack__*"],
        "db_query":       ["mcp__postgres__*", "mcp__bigquery__*"],
        "file_ops":       ["mcp__filesystem__*"],
    }.get(task_type, [])

    return [t for t in all_mcp_tools
            if any(t.name.startswith(s.rstrip('*')) for s in relevant_servers)]

# Reduces ToolSearch search space from 200 to 10-20 tools
# Improves ToolSearch precision (fewer false positives in keyword search)
# Reduces context bloat from loaded schemas
```

**Interception point:** `assembleToolPool()` in `tools.ts:337` — filter `mcpTools` parameter before it's merged with built-ins.

---

### Opportunity 4: Assign Cheaper Models to Read-Only Tool Turns

**Why:** When the model is doing pure exploration — reading files, running grep, listing directories — it's in an information-gathering phase that doesn't require deep reasoning. The next turn (after seeing the results) may need Opus, but the turn that just issues `Read` and `Grep` calls doesn't.

```python
# Classify turns by tool type:
def route_model_for_turn(context):
    tools_in_last_turn = context.last_response.tool_calls

    all_readonly = all(is_read_only(t) for t in tools_in_last_turn)
    has_write_tools = any(not is_read_only(t) for t in tools_in_last_turn)

    if all_readonly and not context.has_complex_reasoning_ahead:
        return "sonnet"  # $3/Mtok — good enough for read + grep
    elif has_write_tools:
        return "opus"    # $5/Mtok — edits need reliability
    elif context.is_synthesis_turn:
        return "opus"    # Synthesizing findings needs reasoning
    return "sonnet"      # Default

# Key: read-only turns (Read + Grep + Glob) are often 3-5 consecutive turns
# Staying on Sonnet for that run = 40% cost saving with cache preserved
```

**Cache consideration:** Once you switch from Opus to Sonnet, the KV cache is cold. Stay on the cheaper model for a run of turns to amortize the cache rebuild cost. Don't switch back and forth per turn.

**Interception point:** `getRuntimeMainLoopModel()` in `utils/model/model.ts:145`.

---

### Opportunity 5: Route Compaction to a Cheaper Model

**Why:** Compaction summarizes the conversation. This is a summarization task — it doesn't need Opus-level reasoning. The compaction prompt has 9 defined sections with explicit instructions. Sonnet handles this well at 40% lower cost.

```python
# Current: compaction uses the main loop model (Opus if on Opus)
# Compaction cost on Opus: 20K output tokens × $25/Mtok = $0.50 per compaction event

# With Sonnet routing:
# Compaction cost on Sonnet: 20K output tokens × $15/Mtok = $0.30 per compaction event
# Saving: $0.20 per compaction event (40%)

# In a long session with 3 compaction events: saves $0.60
```

**Interception point:** The compaction model selection in `services/compact/compact.ts:1188`. Today it inherits the main model — override it explicitly.

**Cache note:** Compaction uses the forked agent path, which shares the parent's prompt cache. Switching the compaction model to Sonnet breaks that cache sharing. Net benefit depends on session length — for long sessions, the per-token saving outweighs the one-time cache miss.

---

### Opportunity 6: Route Background Tool Workers to Haiku

**Why:** The four background services (extractMemories, SessionMemory, PromptSuggestion, AgentSummary) all use the main loop model. They are note-taking and formatting tasks — not reasoning tasks.

```python
# Current cost (main model = Opus):
background_services = ["extractMemories", "SessionMemory", "PromptSuggestion", "AgentSummary"]
turns_per_session = 50
background_forks_per_turn = 2   # on average
avg_fork_tokens = 5_000          # inherits parent context

cost_per_turn = background_forks_per_turn * avg_fork_tokens * 5 / 1_000_000  # Opus
# = 2 × 5K × $5/Mtok = $0.05/turn
# Over 50 turns: $2.50 in background service cost

# With Haiku routing:
cost_per_turn_haiku = background_forks_per_turn * avg_fork_tokens * 1 / 1_000_000
# = 2 × 5K × $1/Mtok = $0.01/turn
# Over 50 turns: $0.50 — saves $2.00 (80% reduction on background services)
```

**Interception point:** Model selection in the forked agent calls for each background service:
- `services/extractMemories/extractMemories.ts:49`
- `services/SessionMemory/sessionMemory.ts:43`
- `services/AgentSummary/agentSummary.ts` — summary timer

**Cache note:** Background forks share the parent's prompt cache prefix. Switching to Haiku breaks cache sharing — Haiku KV tensors are incompatible with Opus KV tensors. The fork would start cold. For short sessions this negates the saving; for long sessions (many turns of background fork overhead) it's still worth it.

---

### Opportunity 7: Selective Subagent Tool Allowlists by Task

**Why:** Subagents get a fixed 15-tool allowlist. Many of those tools will never be needed for a specific task. Trimming the allowlist reduces schema injection tokens for that subagent's system prompt.

```python
# Generic subagent system prompt: 15 tool schemas ≈ 10,000 tokens
# For a "grep and summarize" research subagent: only needs Read, Grep, Glob
# Trimmed system prompt: 3 tool schemas ≈ 2,000 tokens
# Saving: 8,000 tokens × every API call the subagent makes

# Example for a dedicated file reader subagent:
researcher_agent_tools = ["Read", "Glob", "Grep"]  # not Bash, not Edit, not Web*

# If subagent makes 10 API calls: saves 80,000 tokens
# At Sonnet: 80K × $3/Mtok = $0.24 per subagent invocation
```

**Interception point:** Custom agent definition via `.claude/agents/*.md` frontmatter — `tools:` field limits the tool set. Or directly via `agentToolUtils.ts:70-116` for programmatic control.

---

### Opportunity 8: Preemptive MCP Tool Set Reduction

**Why:** Before a task starts, a router that understands the MCP landscape can disable MCP servers that aren't relevant, preventing their tool names from even appearing in the deferred tool announcements. This reduces ToolSearch noise and prevents accidental schema bloat.

```python
# Before the agent loop: inspect user intent
task_intent = classify_intent(user_message)

if task_intent in ["code_editing", "refactoring", "debugging"]:
    # Disable all MCP servers except LSP
    active_mcp_servers = ["lsp-server"]
elif task_intent in ["research", "web_browsing"]:
    # Disable all MCP servers except web-related
    active_mcp_servers = ["brave-search", "fetch-server"]
elif task_intent in ["project_management"]:
    active_mcp_servers = ["linear", "github"]

# Effect: deferred tool announcement only lists ~5-10 tools instead of 100+
# ToolSearch results are precise, model doesn't hallucinate wrong tool calls
```

**Interception point:** MCP client connection management in `services/mcp/client.ts`. Selectively connect/disconnect servers before the session's first API call.

---

### Routing Decision Matrix for Tools

```
┌───────────────────────────────────────────────────────────────────────────────┐
│ TURN TYPE                    │ RECOMMENDED MODEL │ RATIONALE                  │
├───────────────────────────────────────────────────────────────────────────────┤
│ ToolSearch call              │ Haiku             │ Lookup, no reasoning        │
│ Read / Grep / Glob only      │ Sonnet            │ Exploration, not creation   │
│ Edit / Write / Bash          │ Opus or Sonnet    │ Depends on complexity       │
│ Architecture / planning turn │ Opus              │ Needs deep reasoning        │
│ Compaction                   │ Sonnet            │ Summarization task          │
│ extractMemories background   │ Haiku             │ Note-taking only            │
│ SessionMemory background     │ Haiku             │ Note-taking only            │
│ PromptSuggestion background  │ Haiku             │ Speculative, low stakes     │
│ AgentSummary background      │ Haiku             │ 3-5 word formatting         │
│ Verification agent           │ Sonnet            │ Running tests, not creating │
│ Research subagent (read-only)│ Sonnet            │ Exploration phase           │
│ Code-writing subagent        │ Sonnet            │ Strong at code, 5x cheaper  │
│ Coordinator                  │ Haiku             │ Dispatch only, no reasoning │
└───────────────────────────────────────────────────────────────────────────────┘
```

### The Golden Rule for Tool-Aware Routing

```python
# BAD: switch model every turn based on tool type
for turn in session:
    if turn.has_read_tools: model = "haiku"
    if turn.has_write_tools: model = "opus"
    # Every switch = cache miss = one-time penalty
    # Oscillating haiku→opus→haiku = permanent cache miss = costs MORE

# GOOD: switch model at phase transitions, stay for runs of turns
phases = [
    Phase("exploration",  model="sonnet", turns=5),   # Read/Grep/Glob burst
    Phase("planning",     model="opus",   turns=2),   # Architecture decision
    Phase("implementation",model="sonnet",turns=10),  # Edit/Write burst
    Phase("verification", model="sonnet", turns=3),   # Test + check
]
# Each phase switch = one cache miss, amortized over multiple turns
# Cache rebuilds within the phase, savings accumulate

# For background services: ALWAYS Haiku (independent contexts, no cache sharing concern)
# For ToolSearch specifically: ALWAYS Haiku (pure lookup, never reasoning)
```

### Interception Points Summary for Tool-Specific Routing

| Interception Point | File | Line | What to Route |
|---|---|---|---|
| Main model per iteration | `utils/model/model.ts` | 145 | Turn type → model |
| Subagent model | `utils/model/agent.ts` | 37 | Task type → model |
| Compaction model | `services/compact/compact.ts` | 1188 | Always → Sonnet |
| Background fork model | `services/extractMemories/extractMemories.ts` | 49 | Always → Haiku |
| AgentSummary model | `services/AgentSummary/agentSummary.ts` | — | Always → Haiku |
| Tool pool (preload) | `tools.ts` | 271, 337 | Task type → tool subset |
| MCP tool filtering | `tools.ts` | 347-370 | Task type → server subset |
| Subagent tool list | `constants/tools.ts` | — | Task type → trimmed allowlist |
| Pre-tool hooks | `services/tools/toolExecution.ts` | 800 | Intercept before execution |
