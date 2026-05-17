#!/usr/bin/env python3
"""cost-snapshot · Per-source cost breakdown for the current Claude Code session.

Usage:
    python3 cost_snapshot.py              # current session, current project
    python3 cost_snapshot.py --all        # aggregate across all sessions in this project
    python3 cost_snapshot.py --session <uuid>   # specific session
"""

import argparse
import sys
from collections import defaultdict
from pathlib import Path

# Make `lib` importable when run as a script
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.pricing import get_pricing, call_cost
from lib.transcript import (
    current_session_file,
    find_session_files,
    iter_messages,
    extract_usage,
    categorize_record,
)


def analyze_file(path: Path) -> dict:
    """Return {category: {msgs, cost, input, output, cache_r, cache_w, models}} for one file."""
    buckets = defaultdict(lambda: {
        "msgs": 0, "input": 0, "output": 0,
        "cache_r": 0, "cache_w": 0, "cost": 0.0,
        "models": defaultdict(int),
    })
    for record in iter_messages(path):
        usage, model = extract_usage(record)
        if not usage:
            continue
        cat = categorize_record(record)
        b = buckets[cat]
        b["msgs"] += 1
        b["input"]   += usage.get("input_tokens", 0)
        b["output"]  += usage.get("output_tokens", 0)
        b["cache_r"] += usage.get("cache_read_input_tokens", 0)
        b["cache_w"] += usage.get("cache_creation_input_tokens", 0)
        b["models"][model or "unknown"] += 1
        b["cost"] += call_cost(usage, model or "")
    return buckets


def merge_buckets(target, source):
    """Aggregate two bucket dicts in place."""
    for cat, src in source.items():
        tgt = target[cat]
        tgt["msgs"]    += src["msgs"]
        tgt["input"]   += src["input"]
        tgt["output"]  += src["output"]
        tgt["cache_r"] += src["cache_r"]
        tgt["cache_w"] += src["cache_w"]
        tgt["cost"]    += src["cost"]
        for m, n in src["models"].items():
            tgt["models"][m] += n
    return target


def cost_breakdown(buckets: dict) -> dict:
    """Compute total spend per pricing component across all categories."""
    breakdown = {"input": 0.0, "output": 0.0, "cache_r": 0.0, "cache_w": 0.0}
    for b in buckets.values():
        # Recompute per-component cost by re-applying pricing per model used
        for m, _ in b["models"].items():
            p = get_pricing(m)
            if not p:
                continue
            # We don't have per-model token splits — approximate proportionally
        # Simpler approach: scan transcript again at file level would be precise.
    return breakdown


def fmt_int(n: int) -> str:
    return f"{n:,}"


def fmt_cost(c: float) -> str:
    return f"${c:,.2f}"


def render(buckets: dict, label: str):
    if not buckets:
        print(f"No usage data found for: {label}")
        return

    total_cost = sum(b["cost"] for b in buckets.values())
    total_msgs = sum(b["msgs"] for b in buckets.values())

    print()
    print(f"  COST SNAPSHOT · {label}")
    print(f"  {'─' * 74}")
    print(f"  {'Category':<28} {'Msgs':>5} {'Out':>8} {'Cache_R':>10} {'Cost':>9}  Share")
    print(f"  {'─' * 74}")

    for cat in sorted(buckets.keys(), key=lambda k: -buckets[k]["cost"]):
        b = buckets[cat]
        share = (b["cost"] / total_cost * 100) if total_cost else 0
        print(f"  {cat:<28} {fmt_int(b['msgs']):>5} {fmt_int(b['output']):>8} "
              f"{fmt_int(b['cache_r']):>10} {fmt_cost(b['cost']):>9}  {share:>4.1f}%")

    print(f"  {'─' * 74}")
    print(f"  {'TOTAL':<28} {fmt_int(total_msgs):>5} {'':>8} {'':>10} {fmt_cost(total_cost):>9}  100.0%")
    print()

    # Cost-component breakdown (recompute precisely)
    in_cost = out_cost = cr_cost = cw_cost = 0.0
    for b in buckets.values():
        # Use weighted-by-model-count pricing as approximation
        if not b["models"]:
            continue
        for m, n in b["models"].items():
            p = get_pricing(m)
            if not p:
                continue
            # Distribute tokens by message count weight
            weight = n / b["msgs"] if b["msgs"] else 0
            in_cost  += b["input"]   * weight / 1e6 * p["in"]
            out_cost += b["output"]  * weight / 1e6 * p["out"]
            cr_cost  += b["cache_r"] * weight / 1e6 * p["cache_r"]
            cw_cost  += b["cache_w"] * weight / 1e6 * p["cache_w_5m"]

    total_components = in_cost + out_cost + cr_cost + cw_cost
    if total_components > 0:
        print(f"  COST BY COMPONENT")
        print(f"  {'─' * 50}")
        for label, val in [
            ("Cache reads (re-sending prefix)", cr_cost),
            ("Cache writes (cache rebuilds)", cw_cost),
            ("Output tokens (model responses)", out_cost),
            ("New input tokens", in_cost),
        ]:
            share = (val / total_components * 100) if total_components else 0
            print(f"  {label:<35} {fmt_cost(val):>9}  {share:>4.1f}%")
        print()

    # Quick insights
    insights = []
    main_share = next((b["cost"] / total_cost for c, b in buckets.items()
                       if c == "main loop"), 0) * 100
    if total_cost > 0 and cr_cost / total_components > 0.40:
        insights.append("Cache reads dominate — your prefix is growing. Consider /compact at natural boundaries.")
    if total_cost > 0 and cw_cost / total_components > 0.30:
        insights.append("Cache writes are high — check for cache busts (model switches, MCP changes, CLAUDE.md edits).")
    bg_share = sum(b["cost"] for c, b in buckets.items() if "background" in c or "auto memory" in c) / total_cost * 100 if total_cost else 0
    if bg_share > 15:
        insights.append(f"Background activity is {bg_share:.0f}% of cost — consider disabling auto-memory if not used.")

    if insights:
        print(f"  💡 INSIGHTS")
        for ins in insights:
            print(f"     • {ins}")
        print()


def main():
    parser = argparse.ArgumentParser(description="Per-source cost breakdown for Claude Code sessions")
    parser.add_argument("--all", action="store_true",
                        help="Aggregate across all sessions in this project")
    parser.add_argument("--session", type=str, default=None,
                        help="Analyze a specific session UUID (or path)")
    args = parser.parse_args()

    if args.session:
        # User specified session — try path first, then look in projects/
        path = Path(args.session)
        if not path.exists():
            files = [f for f in find_session_files() if args.session in f.name]
            if not files:
                print(f"No session matching: {args.session}", file=sys.stderr)
                sys.exit(1)
            path = files[0]
        buckets = analyze_file(path)
        render(buckets, f"session {path.stem}")
        return

    if args.all:
        all_files = find_session_files()
        if not all_files:
            print("No sessions found for this project.", file=sys.stderr)
            sys.exit(1)
        combined = defaultdict(lambda: {
            "msgs": 0, "input": 0, "output": 0,
            "cache_r": 0, "cache_w": 0, "cost": 0.0,
            "models": defaultdict(int),
        })
        for path in all_files:
            merge_buckets(combined, analyze_file(path))
        render(combined, f"all sessions ({len(all_files)} files)")
        return

    # Default: current session
    path = current_session_file()
    if not path:
        print("No session jsonl found for this project.", file=sys.stderr)
        sys.exit(1)
    buckets = analyze_file(path)
    render(buckets, f"current session ({path.stem[:8]}…)")


if __name__ == "__main__":
    main()
