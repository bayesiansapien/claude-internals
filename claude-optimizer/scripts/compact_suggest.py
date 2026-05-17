#!/usr/bin/env python3
"""compact-suggest · Full anatomy + projection + verdict.

The flagship optimizer view. Shows:
  - Total prefix size + % of window
  - Anatomy: where every token lives (system vs conversation, ranked)
  - Projection: growth rate, headroom, economic savings if /compact now
  - Boundary analysis: T1 rules + optional T2 Haiku + optional T3 Sonnet
  - Verdict: COMPACT_NOW | SOON | WAIT | NO_ACTION + reason

Uses the unified compact_decision scorer — same logic that powers the
compact_advisor hook (just rendered with more detail here).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.compact_decision import decide_compact


VERDICT_GLYPH = {
    "COMPACT_NOW": "🚨",
    "SOON":        "⚠ ",
    "WAIT":        "⏸ ",
    "NO_ACTION":   "✓ ",
}


def fmt_int(n):
    return "—" if n is None else f"{int(n):,}"


def fmt_pct(p):
    return "—" if p is None else f"{p:.1f}%"


def fmt_money_pos(usd):
    if usd is None:
        return "—"
    if abs(usd) >= 1:
        return f"${usd:.2f}"
    return f"${usd:.3f}"


def render_anatomy(anatomy, total):
    print(f"  ANATOMY  (where the {total:,} prefix tokens live)")
    print("  " + "─" * 72)

    conv = anatomy["conversation"]
    sys_ = anatomy["system"]

    sys_rows = [
        ("CC core + built-in tools + skills", sys_["cc_core_estimate"], "(locked)"),
        (f"MCP schemas ({sys_['mcp_server_count']} server{'s' if sys_['mcp_server_count'] != 1 else ''})",
         sys_["mcp_tokens"], "/mcp-audit"),
        (f"CLAUDE.md hierarchy ({sys_['claude_md_files']} files)",
         sys_["claude_md_tokens"], "edit files"),
        (f"Auto-memory ({sys_['memory_files']} files)",
         sys_["memory_tokens"], "/memory-hygiene"),
    ]
    sys_rows = [(n, t, a) for n, t, a in sys_rows if t > 0]
    sys_rows.sort(key=lambda r: r[1], reverse=True)

    cb = conv["buckets_tokens"]
    conv_rows = [
        ("Assistant responses", cb.get("assistant_text", 0)),
        ("Tool results", cb.get("tool_results", 0)),
        ("Tool calls (assistant)", cb.get("tool_calls", 0)),
        ("Attachments", cb.get("attachments", 0)),
        ("User messages", cb.get("user_text", 0)),
    ]
    conv_rows = [(n, t) for n, t in conv_rows if t > 0]
    conv_rows.sort(key=lambda r: r[1], reverse=True)

    print(f"    {'Conversation: ' + fmt_int(conv['total']):<46}{fmt_pct(conv['total']/total*100 if total else 0):>10}")
    for name, tok in conv_rows:
        print(f"      {name:<44}{fmt_int(tok):>10}  {fmt_pct(tok/total*100 if total else 0):>7}")
    print(f"    {'System: ' + fmt_int(sys_['total']):<46}{fmt_pct(sys_['total']/total*100 if total else 0):>10}")
    for name, tok, action in sys_rows:
        print(f"      {name:<44}{fmt_int(tok):>10}  {fmt_pct(tok/total*100 if total else 0):>7}  {action}")
    print()


def render_projection(projection):
    print("  PROJECTION  (based on trailing 15 turns)")
    print("  " + "─" * 72)
    if projection.get("insufficient_data"):
        print(f"    Not enough turn history for projection yet.")
        print(f"    Compact threshold: {fmt_int(projection.get('compact_threshold'))} tokens")
        print(f"    Headroom: {fmt_int(projection.get('headroom_tokens'))} tokens")
        print()
        return

    ttc = projection["turns_to_compact"]
    ttc_str = "(no growth)" if ttc == float("inf") else f"({ttc:,} turns away)"
    print(f"    Growth rate:                 +{fmt_int(projection['growth_per_turn'])} tokens / turn")
    print(f"    Headroom to auto-compact:    {fmt_int(projection['headroom_tokens'])} tokens {ttc_str}")
    print(f"    Current cost / turn:         {fmt_money_pos(projection['current_cost_per_turn'])}")
    if projection["cost_until_compact"] is not None:
        print(f"    Cost from NOW to auto-compact: {fmt_money_pos(projection['cost_until_compact'])}")
    print()
    print(f"    If /compact NOW:")
    print(f"      One-time cost:             {fmt_money_pos(projection['compact_now_cost'])}")
    print(f"      Per-turn cost after:       {fmt_money_pos(projection['post_compact_per_call_cost'])}")
    if projection["projected_savings"] is not None:
        sv = projection["projected_savings"]
        if sv > 0:
            print(f"      Projected savings:         {fmt_money_pos(sv)} ✓")
        else:
            print(f"      Compact would cost MORE:   {fmt_money_pos(-sv)} ✗")
    print()


def render_boundary(boundary, signals, tiers_run, t2_verdict, t3_verdict, t3_reason):
    print("  BOUNDARY ANALYSIS  (is this a good moment to compact?)")
    print("  " + "─" * 72)
    score = boundary["score"]
    print(f"    T1 score:                    {score}/10")
    bd_parts = []
    for k, v in (boundary["breakdown"] or {}).items():
        if k == "pressure":
            continue
        sign = "+" if v > 0 else ""
        bd_parts.append(f"{k}={sign}{v}")
    if bd_parts:
        print(f"    Signals:                     {', '.join(bd_parts)}")
    macro = signals.get("macro_keywords") or []
    if macro:
        keys = " ".join(macro[:8])
        print(f"    Macro task keywords:         {keys}")
    flags = boundary.get("info_loss_flags") or []
    if flags:
        print(f"    Info-loss flags:")
        for f in flags:
            print(f"      ⚠ {f}")
    else:
        print(f"    Info-loss flags:             none")

    if t2_verdict is True:
        t2_str = "different topic (boundary)"
    elif t2_verdict is False:
        t2_str = "same topic (not a boundary)"
    elif any("T2" in t for t in tiers_run):
        t2_str = "called but no verdict"
    else:
        t2_str = "skipped (score unambiguous)"
    print(f"    T2 Haiku tiebreaker:         {t2_str}")

    if t3_verdict:
        print(f"    T3 Sonnet judge:             {t3_verdict} — {t3_reason or ''}")
    else:
        print(f"    T3 Sonnet judge:             skipped (pressure under 85% or unambiguous)")
    print()


def render_verdict(verdict, reason, tiers_run, projection):
    glyph = VERDICT_GLYPH.get(verdict, "")
    print(f"  VERDICT: {glyph}{verdict}")
    print("  " + "═" * 72)
    print(f"    Reason:  {reason}")
    print(f"    Tiers:   {', '.join(tiers_run) if tiers_run else '(gate)'}")
    if (projection and not projection.get("insufficient_data")
            and projection.get("projected_savings") is not None):
        sv = projection["projected_savings"]
        if sv > 0:
            print(f"    Economic outlook: /compact now would save {fmt_money_pos(sv)} long-term.")
        else:
            print(f"    Economic outlook: /compact now would cost {fmt_money_pos(-sv)} extra.")
    print()
    if verdict in ("COMPACT_NOW", "SOON"):
        print(f"  → Run /compact when ready.")
    elif verdict == "WAIT":
        print(f"  → Don't compact yet (Sonnet judge said wait).")
    else:
        print(f"  → No action needed.")
    print()


def main():
    d = decide_compact()
    anatomy = d["anatomy"]
    total = anatomy["total_prefix"]
    pressure = anatomy["pressure_pct"]
    window = anatomy["window"]
    model = anatomy.get("model") or "unknown"

    print()
    print("  COMPACT DECISION · current session")
    print("  " + "═" * 72)
    print(f"  Prefix:  {fmt_int(total)} / {fmt_int(window)} tokens  ({fmt_pct(pressure)})")
    print(f"  Model:   {model}")
    if d["projection"] and not d["projection"].get("insufficient_data"):
        cpc = d["projection"]["current_cost_per_turn"]
        gpt = d["projection"]["growth_per_turn"]
        print(f"  Burn:    {fmt_money_pos(cpc)}/turn  ·  growth +{fmt_int(gpt)} tokens/turn")
    print()

    if d.get("below_pressure_gate"):
        print(f"  ⏸ {d['verdict']}")
        print("  " + "─" * 72)
        print(f"  Reason: {d['verdict_reason']}")
        print(f"  (Pressure must reach 50% before boundary analysis runs.)")
        print()
        render_anatomy(anatomy, total)
        return

    render_anatomy(anatomy, total)
    render_projection(d["projection"])
    render_boundary(d["boundary"], d["signals"], d["tiers_run"],
                     d["t2_verdict"], d["t3_verdict"], d["t3_reason"])
    render_verdict(d["verdict"], d["verdict_reason"], d["tiers_run"], d["projection"])


if __name__ == "__main__":
    main()
