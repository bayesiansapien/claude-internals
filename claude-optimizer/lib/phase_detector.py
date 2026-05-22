"""Rule-based phase detection for the session-boundary-advisor.

Walks the last N turns of a session JSONL and classifies the session's current
working phase based on tool-call distribution and content signals. Pure
deterministic logic, no LLM calls.

Phases:
  - research    : reading, searching, exploring (Read, Grep, Glob, WebFetch heavy)
  - planning    : TaskCreate / discussion-only / TodoWrite heavy
  - implementation : Edit / Write / Bash heavy
  - verification : Bash with test patterns (pytest, npm test, etc.) heavy
  - wrap_up     : git commits, push, PR creation
  - mixed       : multiple phases active, no clear dominant
  - unknown     : insufficient signal

Each phase is scored 0-N based on tool calls in the last LOOKBACK turns.
The phase with the highest score wins, subject to a min-confidence threshold.
"""

import json
from collections import Counter
from pathlib import Path

LOOKBACK_TURNS = 20
MIN_CONFIDENCE = 3  # require at least 3 phase-indicating tool calls

# Tool name → phase contribution
RESEARCH_TOOLS = {"Read", "Grep", "Glob", "WebFetch", "WebSearch", "ToolSearch"}
PLANNING_TOOLS = {"TaskCreate", "TaskList", "TaskUpdate", "TaskGet", "ExitPlanMode", "EnterPlanMode"}
IMPLEMENTATION_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
# Bash needs content sniffing — split based on command pattern
BASH_TEST_PATTERNS = {"pytest", "npm test", "jest", "go test", "cargo test", "bun test", "vitest"}
BASH_WRAPUP_PATTERNS = {"git commit", "git push", "git tag", "gh pr create", "gh release"}

NEXT_PHASE_HINTS = {
    "research":       "planning",
    "planning":       "implementation",
    "implementation": "verification",
    "verification":   "wrap_up",
    "wrap_up":        "research",  # next task often starts a new research phase
    "mixed":          None,
    "unknown":        None,
}


def _extract_tool_calls_from_message(msg):
    """Return list of (tool_name, tool_input) for tool_use blocks in an assistant message."""
    out = []
    if not isinstance(msg, dict):
        return out
    content = msg.get("content")
    if not isinstance(content, list):
        return out
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            out.append((block.get("name", ""), block.get("input", {})))
    return out


def _classify_bash_call(command):
    """Classify a Bash command into a phase contribution."""
    if not isinstance(command, str):
        return None
    cmd_lower = command.lower()
    for pat in BASH_WRAPUP_PATTERNS:
        if pat in cmd_lower:
            return "wrap_up"
    for pat in BASH_TEST_PATTERNS:
        if pat in cmd_lower:
            return "verification"
    # Generic bash (file ops, builds, dev servers, etc.) counts as implementation
    return "implementation"


def detect_phase(jsonl_path, lookback_turns=LOOKBACK_TURNS):
    """Return (phase, confidence, breakdown) for the most recent activity.

    confidence is the count of phase-indicating tool calls observed.
    breakdown is a dict {phase: count} for inspection.
    """
    if not Path(jsonl_path).exists():
        return "unknown", 0, {}

    # Read all assistant messages and their tool calls
    assistant_msgs = []
    try:
        with open(jsonl_path) as f:
            for line in f:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if d.get("type") == "assistant":
                    msg = d.get("message", {})
                    tools = _extract_tool_calls_from_message(msg)
                    if tools:
                        assistant_msgs.append(tools)
    except Exception:
        return "unknown", 0, {}

    # Take the last N turns
    recent = assistant_msgs[-lookback_turns:]
    if not recent:
        return "unknown", 0, {}

    # Score each phase
    counts = Counter()
    for tool_list in recent:
        for name, inp in tool_list:
            if name in RESEARCH_TOOLS:
                counts["research"] += 1
            elif name in PLANNING_TOOLS:
                counts["planning"] += 1
            elif name in IMPLEMENTATION_TOOLS:
                counts["implementation"] += 1
            elif name == "Bash":
                cmd = inp.get("command", "") if isinstance(inp, dict) else ""
                phase = _classify_bash_call(cmd)
                if phase:
                    counts[phase] += 1

    if not counts:
        return "unknown", 0, dict(counts)

    total = sum(counts.values())
    top_phase, top_count = counts.most_common(1)[0]

    if top_count < MIN_CONFIDENCE:
        return "unknown", top_count, dict(counts)

    # Mixed-phase check: if top is less than 50% of total and 2+ phases each have
    # significant count, classify as mixed
    if top_count / total < 0.5 and len([c for c in counts.values() if c >= MIN_CONFIDENCE]) >= 2:
        return "mixed", total, dict(counts)

    return top_phase, top_count, dict(counts)


def predict_next_phase(current_phase, breakdown, recent_phases=None):
    """Predict the next phase based on current + history.

    Standard progression: research → planning → implementation → verification → wrap_up.
    If history shows we're cycling, predict accordingly.
    """
    if current_phase in ("unknown", "mixed"):
        return None
    return NEXT_PHASE_HINTS.get(current_phase)


def generate_session_name(current_phase, predicted_next, project_name=None,
                          recent_user_msgs=None, current_session_name=None,
                          enumerated_default=None):
    """Return naming options for the next session.

    Returns a dict:
      {
        "default":     "cc-internals-2",       # enumeration-based, always present
        "suggestions": ["cc-internals-verification",
                        "cc-internals-cross-platform-cleanup",
                        "cc-internals-next-phase"],
      }

    The caller (boundary advisor) shows the default plus 2-3 phase-based
    suggestions in the banner. If user accepts default → enumerated name wins.
    If user picks a suggestion → that name wins.

    Inputs:
      current_phase / predicted_next : from detect_phase + predict_next_phase
      project_name                   : last segment of cwd (fallback base)
      recent_user_msgs               : recent user turns (used to extract topic)
      current_session_name           : the running session's name (e.g. "cc-internals")
                                       used to derive the base for enumeration + suggestions
      enumerated_default             : pre-computed via session_budget_state.next_enumerated_name;
                                       if not provided, we synthesize from current_session_name
    """
    # 1. Resolve the BASE name (the part before any "-N" suffix)
    base = current_session_name or project_name or "session"
    # Strip any trailing -<digits> to get the canonical base
    import re as _re
    m = _re.match(r"^(.+?)(?:-(\d+))?$", base)
    if m:
        base = m.group(1)

    # 2. Resolve the enumerated default
    if enumerated_default:
        default_name = enumerated_default
    elif current_session_name:
        # If the running session ends in -N, increment; else start at -2
        m2 = _re.match(r"^(.+?)-(\d+)$", current_session_name)
        if m2:
            default_name = f"{m2.group(1)}-{int(m2.group(2)) + 1}"
        else:
            default_name = f"{current_session_name}-2"
    else:
        default_name = f"{base}-2"

    # 3. Build phase-based suggestions
    suggestions = []
    phase_label = predicted_next or current_phase
    if phase_label and phase_label not in ("unknown", "mixed"):
        suggestions.append(f"{base}-{phase_label.replace('_', '-')}")

    # Topic-based suggestion from recent user messages
    topic = _extract_topic_words(recent_user_msgs)
    if topic:
        suggestions.append(f"{base}-{topic}")

    # Generic "next" suggestion as a 3rd option
    if predicted_next and predicted_next not in ("unknown", "mixed"):
        suggestions.append(f"{base}-next-{predicted_next.replace('_', '-')}")
    elif current_phase and current_phase not in ("unknown", "mixed"):
        suggestions.append(f"{base}-continued")

    # Dedupe and trim length per suggestion
    seen = set()
    deduped = []
    for s in suggestions:
        s = s[:60]
        if s and s != default_name and s not in seen:
            seen.add(s)
            deduped.append(s)

    return {"default": default_name[:60], "suggestions": deduped[:3]}


def _extract_topic_words(recent_user_msgs):
    """Pull a 1-3 word topical phrase from recent user messages."""
    if not recent_user_msgs:
        return None
    import re as _re
    text = " ".join(recent_user_msgs[-2:]).lower()
    words = _re.findall(r"[a-z]{3,15}", text)
    SKIP = {"the", "and", "for", "with", "into", "from", "this", "that", "you", "are",
            "let", "going", "yeah", "okay", "give", "tell", "have", "would", "should",
            "want", "need", "make", "sure", "can", "now", "also", "like", "what", "see",
            "session", "name", "fine", "good", "ok", "yes", "but", "ill", "well", "just"}
    meaningful = [w for w in words if w not in SKIP][:3]
    if not meaningful:
        return None
    return "-".join(meaningful)


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from lib.session_budget_state import find_session_jsonl_for_cwd
    sid, jsonl = find_session_jsonl_for_cwd()
    if not jsonl:
        print("No session found.")
        sys.exit(0)
    phase, conf, breakdown = detect_phase(jsonl)
    next_phase = predict_next_phase(phase, breakdown)
    print(f"Session: {sid}")
    print(f"Current phase:   {phase} (confidence {conf})")
    print(f"Predicted next:  {next_phase}")
    print(f"Tool-call breakdown (last {LOOKBACK_TURNS} turns):")
    for k, v in sorted(breakdown.items(), key=lambda kv: -kv[1]):
        print(f"  {k:20s} {v}")
    names = generate_session_name(
        phase, next_phase, project_name="mine-cc",
        current_session_name="cc-internals",
    )
    print(f"Enumerated default: {names['default']}")
    print(f"Suggestions:")
    for s in names["suggestions"]:
        print(f"  - {s}")
