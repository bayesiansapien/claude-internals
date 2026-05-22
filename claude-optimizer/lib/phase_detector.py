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


def generate_session_name(current_phase, predicted_next, project_name=None, recent_user_msgs=None):
    """Generate a short kebab-case name for a new session.

    Format: <project>/<phase>-<short-topic>
    The short-topic is extracted from recent user messages if available.
    """
    project = project_name or "session"
    phase_label = predicted_next or current_phase or "next"

    topic = "continued"
    if recent_user_msgs:
        # Use first non-empty word that looks like a noun/verb from the most recent msg
        text = " ".join(recent_user_msgs[-1:]).lower()
        # Strip punctuation, pull first 2-3 meaningful words
        import re
        words = re.findall(r"[a-z]{3,15}", text)
        SKIP = {"the", "and", "for", "with", "into", "from", "this", "that", "you", "are",
                "let", "going", "yeah", "okay", "give", "tell", "have", "would", "should",
                "want", "need", "make", "sure", "can", "now", "also", "like", "what", "see"}
        meaningful = [w for w in words if w not in SKIP][:3]
        if meaningful:
            topic = "-".join(meaningful)

    return f"{project}/{phase_label}-{topic}"[:60]


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
    name = generate_session_name(phase, next_phase, project_name="mine-cc")
    print(f"Suggested next-session name: {name}")
