# Security Layer · Claude Code Internals

> **Source snapshot:** `/Users/amitsinghbhatti/Desktop/AI-LAB/claude-src-code/src/` (April 2026 cut).
> **Lens:** routing-optimization integration points. Where decisions are made, what state is checked, where to plug in a custom router.
> **Cross-cutting layer.** Sits across L1 (model selection), L3 (context assembly), L6 (agent loop), L7 (tool execution) from the 9-layer map. Not a separate layer — a *cascade* that fires at every tool invocation.

---

## 1 · One-screen mental model

```
Tool invocation
  │
  ▼
hasPermissionsToUseTool()                       ← src/utils/permissions/permissions.ts:473
  │   (wrapper: denial-streak bookkeeping,
  │    dontAsk-mode coercion, auto-mode classifier)
  ▼
hasPermissionsToUseToolInner()                   ← permissions.ts:1158
  │
  │   1a. Tool-wide DENY rule?         → deny
  │   1b. Tool-wide ASK rule?          → ask  (sandbox-bypass possible)
  │   1c. tool.checkPermissions(input) → passthrough | allow | ask | deny
  │   1d. tool said deny               → deny
  │   1e. tool requires UI + ask       → ask  (bypass-immune)
  │   1f. content-specific ask rule    → ask  (bypass-immune)
  │   1g. safetyCheck reason           → ask  (bypass-immune)
  │   2a. mode == bypassPermissions    → allow
  │   2b. Tool-wide ALLOW rule         → allow
  │   3.  passthrough → ask
  ▼
(returned to wrapper)
  │
  │  if allow → reset consecutive-denial counter, done
  │  if ask + mode==dontAsk → coerce to deny
  │  if ask + mode==auto (feature TRANSCRIPT_CLASSIFIER):
  │       ├─ acceptEdits fast-path probe       ← cheap simulation
  │       ├─ safe-tool allowlist               ← classifierDecision.ts:56
  │       └─ YOLO classifier (2-stage)         ← yoloClassifier.ts
  │  if still ask → headless-agent hooks → interactive prompt → fallback deny
  ▼
PermissionDecision { behavior, decisionReason, updatedInput?, message? }
```

Python analogue: think `if/elif` ladder with side-effects (state mutations on hit) and an async classifier call replacing the "user dialog" branch in headless mode.

---

## 2 · Core data shapes

`src/types/permissions.ts` (the pure-types module that breaks import cycles — anything implementation-y re-exports from here).

```python
PermissionBehavior = Literal['allow', 'deny', 'ask']

class PermissionResult:                          # 4-way: allow|deny|ask|passthrough
    behavior: Literal['allow','deny','ask','passthrough']
    decisionReason: PermissionDecisionReason | None
    message: str | None
    updatedInput: dict | None        # allow path
    suggestions: list[PermissionUpdate] | None   # ask path
    pendingClassifierCheck: PendingClassifierCheck | None
    isBashSecurityCheckForMisparsing: bool | None  # bash-parse-safety flag

class PermissionRule:
    source: PermissionRuleSource     # 8-way (see §3)
    ruleBehavior: PermissionBehavior
    ruleValue: { toolName: str, ruleContent: str | None }

class ToolPermissionContext:        # the "session ACL state"
    mode: PermissionMode
    additionalWorkingDirectories: ReadonlyMap[str, AdditionalWorkingDirectory]
    alwaysAllowRules: dict[PermissionRuleSource, list[str]]
    alwaysDenyRules:  dict[PermissionRuleSource, list[str]]
    alwaysAskRules:   dict[PermissionRuleSource, list[str]]
    isBypassPermissionsModeAvailable: bool
    strippedDangerousRules: dict[PermissionRuleSource, list[str]] | None
    shouldAvoidPermissionPrompts: bool | None   # headless/sub-agent
    awaitAutomatedChecksBeforeDialog: bool | None
    prePlanMode: PermissionMode | None          # for plan→exit restore
```

`PermissionDecisionReason` is a 12-variant tagged union — keep this list handy, every UI surface and analytics event keys off the `type` field:

| `.type` | When fired | Where |
|---|---|---|
| `rule` | matched a deny/allow/ask rule | steps 1a/1b/2b |
| `mode` | mode override (bypass, dontAsk, plan, auto) | step 2a, wrapper |
| `subcommandResults` | Bash `&&`-chain — per-subcommand map | bash classifier |
| `permissionPromptTool` | SDK host's external tool decided | `PermissionPromptToolResultSchema.ts:89` |
| `hook` | a `PermissionRequest` hook intercepted | `permissions.ts:400`, `runPermissionRequestHooksForHeadlessAgent` |
| `asyncAgent` | sub-agent can't show UI, auto-deny | wrapper, steps for headless |
| `sandboxOverride` | `excludedCommand` or `dangerouslyDisableSandbox` flag | bash + sandbox adapter |
| `classifier` | YOLO classifier said so | yoloClassifier flow |
| `workingDir` | path outside CWD/additional dirs | pathValidation |
| `safetyCheck` | `.git/`, `.claude/`, dotfiles, etc. — has `classifierApprovable: bool` | filesystem.ts |
| `other` | catch-all (rare) | — |

---

## 3 · The 7 permission modes (verified)

`src/types/permissions.ts:16-36`.

```python
EXTERNAL_PERMISSION_MODES = ('acceptEdits', 'bypassPermissions',
                             'default', 'dontAsk', 'plan')
INTERNAL_ONLY = ('auto', 'bubble')          # auto = TRANSCRIPT_CLASSIFIER gated, ant-only
                                            # bubble = legacy, maps to default externally
```

| Mode | Behavior | Bypass-immune carve-outs |
|---|---|---|
| `default` | Rules first, prompt for unknowns | n/a |
| `acceptEdits` | File-edit tools auto-allow in CWD; rest like default | n/a |
| `plan` | Plan-safe tools only; everything else asks | n/a |
| `bypassPermissions` | Allow-all — but **1d, 1e, 1f, 1g still fire** (deny rules, UI-required tools, content-ask rules, safetyChecks) | yes — bypass is not blanket |
| `dontAsk` | Allow only what rules permit; anything that would `ask` → coerced to `deny` (`permissions.ts:508`) | wrapper-level coercion |
| `auto` *(ant-only, feature TRANSCRIPT_CLASSIFIER)* | YOLO classifier replaces user prompt | safety-check non-`classifierApprovable` paths stay ask; PowerShell stays ask unless `POWERSHELL_AUTO_MODE` |
| `bubble` *(legacy)* | Maps to `default` for external users | — |

**Mode transition (Shift+Tab):** `getNextPermissionMode.ts`. Cycle order depends on `USER_TYPE` (ant vs external) and the two availability flags (`isBypassPermissionsModeAvailable`, `feature('TRANSCRIPT_CLASSIFIER')` + `verifyAutoModeGateAccess` result).

**Bypass is conditional:**
- `bypassPermissionsCheckRan` (run-once flag, `bypassPermissionsKillswitch.ts:17`) calls Statsig gate `tengu_bypass_permissions_disabled` once per session.
- If gate trips, `createDisabledBypassPermissionsContext()` strips bypass availability for the rest of the session.
- Reset only on `/login` (`resetBypassPermissionsCheck`).

**Auto mode is double-gated:**
- Static: `feature('TRANSCRIPT_CLASSIFIER')` bundle flag (Bun DCE for external builds).
- Dynamic: `verifyAutoModeGateAccess()` re-checks GrowthBook gate on every model/fast-mode change (`bypassPermissionsKillswitch.ts:141-154`). A `/model` switch can flip auto off mid-session.

---

## 4 · The 8 rule sources

```python
PermissionRuleSource = Literal[
    'userSettings',     # ~/.claude/settings.json  (per-user)
    'projectSettings',  # <repo>/.claude/settings.json
    'localSettings',    # <repo>/.claude/settings.local.json (gitignored)
    'flagSettings',     # feature-flag overrides
    'policySettings',   # org-managed (enterprise)
    'cliArg',           # --allow / --deny CLI flag
    'command',          # in-session slash command (/permissions add ...)
    'session',          # in-session "always allow" button
]
```

Loader: `permissionsLoader.ts:120` (`loadAllPermissionRulesFromDisk`). If `policySettings.allowManagedPermissionRulesOnly == true`, **only** policySettings is loaded — user/project/local are silently ignored, and "always allow" UI buttons disabled. This is the enterprise enforcement seam.

**Rule string grammar** (`permissionRuleParser.ts`):

| Form | Means |
|---|---|
| `Bash` | tool-wide |
| `Bash(npm:*)` | legacy `:*` prefix |
| `Bash(npm*)` or `Bash(npm *)` | wildcard (trailing `*`, space-optional) |
| `Bash(npm install)` | exact command |
| `Bash(python -c "print\(1\)")` | escaped parens inside content |

**Conflict resolution** (`shadowedRuleDetection.ts`):
- Deny shadows allow (tool-wide deny blocks specific allow).
- Ask shadows allow (tool-wide ask blocks specific allow).
- **Exception:** when sandbox auto-allow fires for Bash, ask-rule shadowing is suppressed (`shadowedRuleDetection.ts:35-37`) so sandboxed commands still run.
- Detection is *advisory* — unreachable rules log warnings, they don't block execution.

---

## 5 · The full cascade — verified line numbers

`src/utils/permissions/permissions.ts:1158` (`hasPermissionsToUseToolInner`).

| Step | Line | Check | On hit |
|---|---|---|---|
| **1a** | 1171 | `getDenyRuleForTool(ctx, tool)` | return `deny` (`type: 'rule'`) |
| **1b** | 1184 | `getAskRuleForTool(ctx, tool)` | return `ask` — **unless** Bash + sandbox enabled + `shouldUseSandbox(input)`, in which case fall through |
| **1c** | 1216 | `tool.checkPermissions(parsedInput, ctx)` | get tool-local result (default = `passthrough`) |
| **1d** | 1226 | tool result = `deny` | return as-is |
| **1e** | 1231 | `tool.requiresUserInteraction?()` and tool said `ask` | return `ask` (bypass-immune) |
| **1f** | 1244 | tool said `ask` with `decisionReason.type == 'rule'` and `rule.ruleBehavior == 'ask'` | return `ask` (bypass-immune content rule) |
| **1g** | 1255 | tool said `ask` with `decisionReason.type == 'safetyCheck'` | return `ask` (bypass-immune `.git`/`.claude`/dotfile path) |
| **2a** | 1268 | `mode == 'bypassPermissions'` OR (`mode == 'plan'` AND `isBypassPermissionsModeAvailable`) | return `allow` (`type: 'mode'`) |
| **2b** | 1284 | `toolAlwaysAllowedRule(ctx, tool)` | return `allow` (`type: 'rule'`) |
| **3** | 1299 | `toolPermissionResult.behavior == 'passthrough'` | coerce to `ask` |

**Critical ordering invariants:**
- Steps 1d–1g run *before* step 2a, so bypass mode is **not** an unconditional allow. Deny rules, UI-required tools, content-specific ask rules, and safety-check paths all override bypass.
- This is the security teeth of the system. Treat bypass as "auto-yes to the prompt" not "skip all checks".

**Wrapper** (`permissions.ts:473`, `hasPermissionsToUseTool`):

| Post-inner action | Line | Trigger |
|---|---|---|
| Reset consecutive-denial counter | 486 | `result == allow` AND `mode == 'auto'` |
| Coerce `ask` → `deny` | 508 | `mode == 'dontAsk'` |
| Block non-`classifierApprovable` safety checks from auto-approve | 532 | `mode == 'auto'`, safetyCheck reason, `classifierApprovable: false` |
| Skip auto for PowerShell | 572 | `tool == PowerShell` AND not `feature('POWERSHELL_AUTO_MODE')` |
| **acceptEdits fast-path probe** | 600 | re-invoke `tool.checkPermissions` with mode swapped to `acceptEdits`; if that would allow → skip classifier, allow now |
| Safe-tool allowlist short-circuit | 660 | `isAutoModeAllowlistedTool(tool.name)` |
| YOLO classifier call | (later) | everything else in auto mode |

The acceptEdits probe (line 600) is **the cheapest cost-saving step in the cascade** — it avoids a classifier API call by running a synthetic permission check at the local mode-overridden state. Note the explicit carve-out for `AGENT_TOOL_NAME` and `REPL_TOOL_NAME` (line 602): REPL especially has VM-escape risk between inner tool calls, so the classifier *must* see the glue JS.

---

## 6 · Classifiers (YOLO + two-stage)

### YOLO classifier — `src/utils/permissions/yoloClassifier.ts` (1495 lines)

Purpose: in `auto` mode, classify a pending tool use as block-or-allow via a side-query to Claude. Output validated against `YoloClassifierResult`.

```python
class YoloClassifierResult:
    shouldBlock: bool
    reason: str
    thinking: str | None
    unavailable: bool | None
    transcriptTooLong: bool | None      # ctx-window exhaustion → fallback, not retry
    model: str
    # 2-stage telemetry — fast → thinking
    stage: Literal['fast', 'thinking'] | None
    stage1Usage: ClassifierUsage | None
    stage1DurationMs: int | None
    stage1RequestId: str | None         # join to api_usage logs
    stage1MsgId: str | None             # join to tengu_auto_mode_decision analytics
    stage2Usage: ClassifierUsage | None
    stage2DurationMs: int | None
    stage2RequestId: str | None
    stage2MsgId: str | None
    promptLengths: { systemPrompt, toolCalls, userPrompts }
    errorDumpPath: str | None
```

**Two-stage architecture** (visible in the result shape): a cheap "fast" model runs first; if low confidence or a borderline call, "thinking" model takes a second pass. Both stages emit independent request IDs for post-hoc routing attribution. This is *the* place to drop in a cheap router — both stages are already plumbed for model swaps.

**Failure handling:**
- API error → `unavailable: true`, falls back to prompting (not to deny — fail-open is intentional in the prompted case).
- Prompt exceeds context → `transcriptTooLong: true`, deterministic, **don't retry**, fall back to prompt.
- 3 consecutive denials OR 20 total denials (`denialTracking.ts:12-15`) → `shouldFallbackToPrompting()` returns true, classifier output gets a prompt instead of auto-deny.

### Safe-tool allowlist — `classifierDecision.ts:56` (`SAFE_YOLO_ALLOWLISTED_TOOLS`)

Tools that skip the classifier entirely in auto mode (read-only, metadata-only, plan/swarm coordination). Verified contents:

```
FileRead, Grep, Glob, LSP, ToolSearch, ListMcpResources, ReadMcpResource,
TodoWrite, TaskCreate, TaskGet, TaskUpdate, TaskList, TaskStop, TaskOutput,
AskUserQuestion, EnterPlanMode, ExitPlanMode,
TeamCreate, TeamDelete, SendMessage,        # swarm — internal mailbox only
Sleep, YoloClassifier(self),
+ ant-only: TerminalCapture, OverflowTest, VerifyPlanExecution, Workflow
```

**Write/edit tools are deliberately not here** — those use the acceptEdits fast-path instead (allowed in CWD, classified outside CWD).

### Dangerous-pattern strip — `dangerousPatterns.ts` + `permissionSetup.ts:85-147`

On auto-mode entry, `isDangerousBashPermission` / `isDangerousPowerShellPermission` predicates scan existing allow rules and **strip** any that grant code execution to:

```
Cross-platform: python, python3, python2, node, deno, tsx, ruby, perl, php, lua,
                npx, bunx, npm run, yarn run, pnpm run, bun run,
                bash, sh, ssh
Bash-only:      zsh, fish, eval, exec, env, xargs, sudo
Ant-only adds:  fa run, coo, gh, gh api, curl, wget, git,
                kubectl, aws, gcloud, gsutil
```

Stripped rules are preserved in `ctx.strippedDangerousRules` (visible in the type shape) — they reappear on mode exit. The match handles all four rule shapes (exact, `:*`, trailing `*`, ` *`, ` -…*`).

---

## 7 · Filesystem & sandbox

### Path validation — `pathValidation.ts` (485 lines), `filesystem.ts`

Order of checks for any path-touching tool input:

1. Resolve symlinks (`realpath()`); tilde-expand.
2. Detect path traversal (`../../...`).
3. Check `alwaysDenyRules` for path-bound rules.
4. Check `alwaysAllowRules`.
5. Check sandbox write allowlist (`isPathInSandboxWriteAllowlist`).
6. Internal safety: against `DANGEROUS_FILES` and `DANGEROUS_DIRECTORIES`:
   - Files: `.gitconfig`, `.gitmodules`, `.bashrc`, `.bash_profile`, `.zshrc`, `.zprofile`, `.profile`, `.ripgreprc`, `.mcp.json`, `.claude.json`
   - Dirs: `.git`, `.vscode`, `.idea`, `.claude`
7. `checkPathSafetyForAutoEdit` (yields `safetyCheck` reason with `classifierApprovable` flag).
8. Default deny.

The `classifierApprovable` flag is subtle: for sensitive dotfile paths the auto-mode classifier *can* see context and decide; for Windows-path bypass attempts (e.g. `C:\..\..\windows\system32`) and cross-machine bridge messages it cannot — those are force-prompt.

### Sandbox — `src/utils/sandbox/sandbox-adapter.ts`, `src/components/sandbox/`

Thin adapter over `@anthropic-ai/sandbox-runtime` (external dep, macOS Seatbelt / Linux landlock under the hood).

```python
class SandboxManager:
    isSandboxingEnabled() -> bool
    isAutoAllowBashIfSandboxedEnabled() -> bool
    getFsWriteConfig() -> { allowOnly: [paths], denyWithinAllow: [paths] }
```

Two interactions with the cascade:

1. **Step 1b sandbox bypass** (`permissions.ts:1189`): if Bash + sandbox enabled + `shouldUseSandbox(input)`, the tool-wide ask rule is *skipped* so the Bash tool's own `checkPermissions` can grant via sandbox path validation. `decisionReason.type == 'sandboxOverride'` with `reason: 'excludedCommand' | 'dangerouslyDisableSandbox'` for non-sandboxed escapes.

2. **Per-path deny-within-allow**: `.claude/settings.json` blocked even if its parent dir is allowed — the deny list is checked *inside* the allow list.

---

## 8 · Hooks layer (3 handlers, one event)

`src/hooks/toolPermission/` — React hooks that surface permission state to the UI **and** provide the headless-agent interception point.

```
PermissionContext.ts         ← provider (subscribes to AppState.toolPermissionContext)
permissionLogging.ts         ← every decision → Statsig event tengu_auto_mode_decision et al.
handlers/
  interactiveHandler.ts      ← terminal-attached user, shows dialog
  coordinatorHandler.ts      ← team-lead orchestrator (decides for teammates)
  swarmWorkerHandler.ts      ← sub-agent (no UI; auto-deny unless hook intercepts)
```

### `runPermissionRequestHooksForHeadlessAgent` — `permissions.ts:400`

The headless-agent interception point. When `shouldAvoidPermissionPrompts == true` (sub-agents, print-mode, Workflow children):

1. Iterate `executePermissionRequestHooks(...)` (async generator).
2. First hook to yield a `permissionRequestResult` wins.
3. Result `allow` → persist suggested updates, return allow.
4. Result `deny` → optionally abort entire turn (`signal.abort()`), return deny.
5. No hook responded → fall through to auto-deny.
6. Hook *threw* → swallow, fall through to auto-deny (don't crash on user hook errors).

`decisionReason.type == 'hook'` with `hookName: 'PermissionRequest'`.

This is the **cleanest router seam** in the entire layer. A custom router can register a hook that runs a cheaper model (or a deterministic policy) before the classifier ever fires.

---

## 9 · Per-tool permission UI

`src/components/permissions/*PermissionRequest.tsx` — 40+ files, one per tool family. Common shape:

```python
class ToolUseConfirm:
    behavior: Literal['allow', 'deny']
    updatedInput: dict | None
    updatedPermissions: list[PermissionUpdate] | None   # "always allow" → persist a rule
    feedback: str | None                                 # denial reason → fed back to model
```

Signal richness per tool — all the prompts the user can see, which is also the surface area a router has to mimic if it bypasses the dialog:

| Tool | Surface |
|---|---|
| Bash | command preview, exact/prefix/wildcard radio, sandbox-override toggle, `subcommandResults` breakdown for `&&`-chains |
| FileEdit | diff preview, skill-scope "narrow allow" |
| FileWrite | path + content preview |
| WebFetch | URL, cookie/auth signal, host-allowlist rule suggestion |
| AskUserQuestion | the LLM's own question to user — answer is fed back as model input |
| ExitPlanMode | summary of plan + "approve & switch to default mode" |
| PowerShell | cmdlet highlighting (separate file) |
| Sandbox | "run outside sandbox" override |
| FilePermissionDialog | shared base for Edit/Write/Read |

---

## 10 · OAuth, MCP auth, token storage

`src/services/oauth/`:

```
client.ts              ← buildAuthUrl, exchangeCodeForTokens, refresh
crypto.ts              ← PKCE (code_verifier, code_challenge)
auth-code-listener.ts  ← localhost callback server (random port)
getOauthProfile.ts     ← /v1/oauth/profile fetch
index.ts               ← surface
```

Flow (PKCE OAuth):
1. Generate `code_verifier` + `code_challenge` (`crypto.ts`).
2. Spin up loopback HTTP server on random port (`auth-code-listener.ts`).
3. Build `auth_url` with `client_id`, `redirect_uri=http://localhost:PORT`, `code_challenge`, `state`, optional `orgUUID`, `loginHint`.
4. Open in browser → user logs in on `console.anthropic.com` or `claude.ai`.
5. Callback → exchange code for `{access_token, refresh_token, expires_in, scope}`.
6. Encrypt and persist to `~/.claude/config.json` per provider.
7. Refresh 5 min before expiry (`checkAndRefreshOAuthTokenIfNeeded` in `src/utils/auth.ts`).

**Scope set:** `ALL_OAUTH_SCOPES` includes `profile`, `claude_ai_inference`, etc. Inference-only mode uses a long-lived `CLAUDE_AI_INFERENCE_SCOPE` for API-key issuance.

**MCP auth — `src/tools/McpAuthTool/`, `src/services/mcp/auth.ts`:**
- Per-MCP-server OAuth token store.
- Refresh on demand from `McpAuthTool` invocation.
- Reuses the same loopback-callback machinery.
- Critical for routing: each MCP server's tokens are isolated, so MCP-aware routing can pin per-server keys without leakage.

`/oauth-refresh` slash command at `src/commands/oauth-refresh/` forces a refresh outside the auto-window.

---

## 11 · Managed / enterprise security

`src/components/ManagedSettingsSecurityDialog/`:
- Shown at startup if **policySettings contains rules the user would normally have to approve** — e.g. broad `Bash` allow, file write/edit allows, PowerShell rules, custom hooks.
- User must explicitly accept or exit; can't be skipped.
- Combined with `allowManagedPermissionRulesOnly` (in policySettings itself), the org admin can:
  - Force-load only policySettings rules.
  - Block all "always allow" buttons (no rule additions from the UI).
  - Pre-approve specific dangerous patterns via the dialog acceptance.

This is the seam for an org-level router: drop your routing/cost-control policy into policySettings, lock everything else out.

---

## 12 · PermissionPromptTool (SDK host intercept)

`src/utils/permissions/PermissionPromptToolResultSchema.ts` (127 lines) — the SDK's "call out to an external tool to decide" pattern. When the SDK host (managed agent harness) wants to own the decision:

1. Host registers `PermissionPromptTool` in the tool list.
2. When a permission decision is reached, claude-code invokes this tool with `{tool_name, input, tool_use_id}`.
3. Tool returns one of:

```python
class PermissionAllowResult:
    behavior: 'allow'
    updatedInput: dict                              # may modify input
    updatedPermissions: list[PermissionUpdate] | None  # may grant rules
    toolUseID: str | None
    decisionClassification: Literal['user_temporary', 'user_permanent', 'user_reject'] | None

class PermissionDenyResult:
    behavior: 'deny'
    message: str
    interrupt: bool | None   # if True → abort entire turn (signal.abort())
    toolUseID: str | None
    decisionClassification: ...   # same enum
```

4. `permissionPromptToolResultToPermissionDecision()` validates and applies. If `behavior=='allow'` with `updatedPermissions`, those are *both* applied to live `AppState` AND persisted to disk (`applyPermissionUpdates + persistPermissionUpdates`, lines 100-105).
5. Mobile-client carve-out (line 110): push-notification approvals send `{}` for `updatedInput` since the device doesn't have the original — empty dict means "use original input verbatim".
6. Malformed `updatedPermissions` array → swallow with a warning, don't reject the whole decision (defensive against SDK-host bugs; line 53-58).

**This is the maximum-leverage seam** for a custom router: register a `PermissionPromptTool`, intercept every decision, route to any policy engine, decide on behalf of the user — but persist any rules you generate so they don't replay through the cascade next turn.

---

## 13 · Denial-tracking + explainer

`denialTracking.ts` (45 lines, full file):

```python
DENIAL_LIMITS = { maxConsecutive: 3, maxTotal: 20 }

class DenialTrackingState:
    consecutiveDenials: int   # reset on any allow
    totalDenials: int         # never resets in-session

def shouldFallbackToPrompting(state) -> bool:
    return (state.consecutiveDenials >= 3) or (state.totalDenials >= 20)
```

Two storage paths: `appState.denialTracking` for normal sessions, `context.localDenialTracking` for async sub-agents whose `setAppState` is a no-op (line 555-558 of `permissions.ts`). Both are checked.

`permissionExplainer.ts`: side-query to Claude with an `EXPLAIN_COMMAND_TOOL` schema. Returns `PermissionExplanation { riskLevel: 'LOW'|'MEDIUM'|'HIGH', explanation, reasoning, risk }`. Surfaced in the permission dialog as risk advisory. This is an *additional* model call per ambiguous prompt — a router that can replace it with a cheaper model gets a direct cost win on every interactive ask.

---

## 14 · Threat → defense → location

| Threat | Defense | Where |
|---|---|---|
| Allow-rule escalation (broad `Bash(*)` granted) | Shadow detection logs; dangerous-pattern strip on auto entry | `shadowedRuleDetection.ts`, `permissionSetup.ts:85-147` |
| Interpreter bypass (`Bash(python:*)` runs any code) | `DANGEROUS_BASH_PATTERNS` list strips these on auto entry | `dangerousPatterns.ts` |
| Classifier outage → silent fail-open | `unavailable: true` → fall through to prompt, not allow | `yoloClassifier.ts` result handler |
| Classifier transcript overflow | `transcriptTooLong: true` (deterministic) → prompt, don't retry | same |
| Repeated false negatives in classifier | `shouldFallbackToPrompting` after 3 consecutive or 20 total denials | `denialTracking.ts` |
| Bypass mode used to dodge `.git` writes | safetyCheck reason `classifierApprovable: false` → bypass-immune at step 1g | `permissions.ts:1255` |
| Sandbox escape via path traversal | symlink resolve + `containsPathTraversal`; deny-within-allow | `pathValidation.ts` |
| Dotfile writes (.bashrc, .mcp.json) | `DANGEROUS_FILES` list → safetyCheck → bypass-immune ask | `filesystem.ts:57-68` |
| Managed-rule subversion | `allowManagedPermissionRulesOnly` ignores user/project/local rules | `permissionsLoader.ts:31-44` |
| Sub-agent runaway (no UI to ask) | Headless-agent hook chain; fall through to auto-deny | `permissions.ts:400` |
| Mid-session bypass flip via stale gate | Statsig gate re-checked on /login + once per session | `bypassPermissionsKillswitch.ts:34` |
| Auto-mode persistence after model switch | `verifyAutoModeGateAccess` re-runs on every `mainLoopModel` change | `bypassPermissionsKillswitch.ts:141` |
| PowerShell IEX-download-and-exec | PS skips auto classifier unless `POWERSHELL_AUTO_MODE`; classifier prompt appends `POWERSHELL_DENY_GUIDANCE` | `permissions.ts:572` |
| SDK host returns malformed permission updates | Zod `.catch()` swallows, logs warning | `PermissionPromptToolResultSchema.ts:53` |
| Bash command parser misparse (line continuations) | `isBashSecurityCheckForMisparsing` flag → block before `splitCommand_DEPRECATED` transforms it | `permissions.ts` (set), `tools/BashTool` (consumed) |
| OAuth token theft from disk | Encrypted at rest in `~/.claude/config.json`; PKCE for code exchange | `services/oauth/crypto.ts`, `utils/auth.ts` |
| Prompt injection into classifier transcript | Classifier input is structured `tool_use` blocks, not string-concat user content | `yoloClassifier.ts` prompt assembly |
| Cross-server MCP token leak | Per-server OAuth token store | `services/mcp/auth.ts` |

---

## 15 · Routing seams (the integration map)

Ordered roughly by cost of integration — top is easiest, bottom is most invasive.

| # | Seam | File:line | Why it's useful for routing |
|---|---|---|---|
| 1 | **`PermissionPromptTool`** (SDK) | `PermissionPromptToolResultSchema.ts:84` | Register one tool, own every decision. Cleanest possible interception. Can persist rules to avoid re-asking. |
| 2 | **PermissionRequest hook** | `permissions.ts:400`, `hooks/toolPermission/handlers/` | Headless-mode interception. Drop a cheaper model in front of YOLO classifier. |
| 3 | **YOLO classifier model swap** | `yoloClassifier.ts` (2-stage telemetry shape) | Stage1 + stage2 already plumbed for separate models. Swap stage1 to a cheap Haiku, keep Opus for stage2 only when needed. |
| 4 | **acceptEdits fast-path probe** | `permissions.ts:600` | Already saves classifier calls. Extend predicate to cover more "obviously safe" cases for your router. |
| 5 | **Safe-tool allowlist** | `classifierDecision.ts:56` | Add tool names here to bypass classifier entirely. Pure config win for new safe tools. |
| 6 | **Permission explainer model** | `permissionExplainer.ts` | Per-prompt side-query. Replace with a Haiku call — every ambiguous ask saves a Sonnet/Opus invocation. |
| 7 | **Custom rule source** | `permissionsLoader.ts:120` | Load rules from LDAP / vault / git / API. Slot into the 8-source list. |
| 8 | **Custom classifier (replace YOLO)** | `permissions.ts:520-700` | Replace `classifyYoloAction()` entirely with a deterministic / cheap-model policy. Keep the same result shape so the cascade is unchanged. |
| 9 | **Pre-cascade interception** | `permissions.ts:473` (wrap `hasPermissionsToUseTool`) | Decorate `CanUseToolFn`. Most invasive, most powerful — every tool call passes through. |
| 10 | **Mode transition hook** | `getNextPermissionMode.ts` | Inject custom modes or restrict transitions. |
| 11 | **Managed settings policy** | `ManagedSettingsSecurityDialog.tsx`, `policySettings` | Org-wide enforcement at boot — pre-bake your routing policy as enterprise rules. |
| 12 | **OAuth token store** | `services/oauth/client.ts:107` | Swap to vault/HSM if cross-tenant routing requires hardware-isolated keys. |
| 13 | **Sandbox config** | `sandbox-adapter.ts` | Custom write-allowlist driven by router (per-task scoped write zones). |

---

## 16 · Decision-routing analytics surface

Every classifier decision and permission outcome emits a structured analytics event. For a router, these are the *labelled training data* and the post-hoc cost-attribution channel:

| Event | When | Key fields |
|---|---|---|
| `tengu_auto_mode_decision` | every auto-mode decision (fast-path + classifier) | `decision`, `toolName` (sanitized), `fastPath` ('acceptEdits' \| 'safeAllowlist' \| null), `confidence`, `agentMsgId`, `stage1RequestId`, `stage1MsgId`, `stage2RequestId`, `stage2MsgId` |
| `tengu_auto_mode_config` | mode toggles | gate state |
| Bypass-killswitch trip | once per session | gate result |

`agentMsgId` joins the classifier decision back to the **main agent's API response** that produced the tool use — the action at the bottom of the classifier transcript. With both `stage1MsgId` and `agentMsgId`, you can reconstruct the full router decision chain in post.

---

## 17 · Open questions worth chasing (for the xr router)

- [ ] How does `verifyAutoModeGateAccess` interact with `fastMode`? (line 154 watches it, but the gate logic is in `permissionSetup.ts:525` — what does fastMode actually toggle?)
- [ ] What's in `tengu_auto_mode_config.disableFastMode`? Is fastMode itself one of the circuit breakers?
- [ ] `awaitAutomatedChecksBeforeDialog` field on `ToolPermissionContext` — under what mode does the dialog wait for classifier? (Race-condition handling per `tools.md` mention.)
- [ ] Does the headless hook chain (`executePermissionRequestHooks`) run in parallel or serial? First-yielded-wins — but if hook A is slow and hook B is fast, does B's decision short-circuit A?
- [ ] `subcommandResults` decision reason for Bash `&&`-chains: per-subcommand classifier calls? Cached?
- [ ] Where does `EXTERNAL_PERMISSION_MODES` get re-checked on conversation recovery? (Mentioned in comment at `types/permissions.ts:32`.)

---

## 18 · TL;DR for the routing agenda

1. **The cleanest single hook** is `PermissionPromptTool` (SDK-host level). One registration intercepts everything.
2. **The cheapest single win** is replacing the YOLO classifier's stage 1 with a cheaper model — already plumbed for it, separate telemetry, no semantic risk.
3. **The fastest pure-config win** is extending `SAFE_YOLO_ALLOWLISTED_TOOLS` for your safe tools and tuning `DANGEROUS_BASH_PATTERNS` for your environment.
4. **The least visible danger** is conflating bypass mode with "no checks" — steps 1d/1e/1f/1g are bypass-immune by design. Any router that auto-bypasses needs to honor the same carve-outs or it loses safetyCheck protection on dotfiles, `.git`, `.claude`, and content-specific ask rules.
5. **The most underused signal** is `decisionReason.type` (12 variants) — it's already on every decision, already in analytics, and tells you exactly which branch fired. Build the router's policy on top of this taxonomy rather than re-deriving it.

---

*Snapshot date: claude-src-code as of 2026-04-01 mtime. Re-verify line numbers against the live `npm view @anthropic-ai/claude-code` source if pulling for a paper or production integration — the cascade ordering is stable, but exact line offsets shift between releases.*
