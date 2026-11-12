"""Terminal rendering for eval results: one compact row per model x effort, failures listed once."""
from __future__ import annotations

from typing import Any


def _pct(x: float) -> str:
    return f"{x * 100:.0f}%"


def _cost(x: float) -> str:
    return f"${x:.4f}" if x < 0.1 else f"${x:.2f}"


def _model_label(m: str, e: str) -> tuple[str, str]:
    return (m or "(active)", e or "default")


def table(summary: dict[str, Any]) -> str:
    rows = summary["combos"]
    head = ("model", "effort", "pass", "rate", "steps", "cost", "time")
    body = []
    for r in rows:
        m, e = _model_label(r["model"], r["effort"])
        body.append((m, e, f"{r['passed']}/{r['runs']}", _pct(r["pass_rate"]), f"{r['avg_steps']:.1f}", _cost(r["avg_cost"]), f"{r['avg_time']:.1f}s"))
    widths = [max(len(str(x[i])) for x in [head, *body]) for i in range(len(head))]
    fmt = lambda row: "  ".join(str(c).ljust(widths[i]) if i < 2 else str(c).rjust(widths[i]) for i, c in enumerate(row)).rstrip()  # noqa: E731
    lines = [fmt(head), *[fmt(b) for b in body]]
    # failing tasks, each id once, with the combos it failed in and the first failing check
    fails: dict[str, dict[str, Any]] = {}
    for r in rows:
        for tid, check in r["failed"].items():
            f = fails.setdefault(tid, {"check": check, "combos": []})
            m, e = _model_label(r["model"], r["effort"])
            f["combos"].append(f"{m}/{e}")
    if fails:
        lines.append("")
        lines.append("failed:")
        multi = len(rows) > 1
        for tid, f in fails.items():
            where = f"  ({', '.join(f['combos'])})" if multi else ""
            lines.append(f"  {tid}{where}  {f['check']}")
    return "\n".join(lines)


def compare_text(c: dict[str, Any], a_id: str, b_id: str) -> str:
    lines = [f"{a_id} -> {b_id}: pass rate {_pct(c['pass_rate_a'])} -> {_pct(c['pass_rate_b'])}, "
             f"cost {_cost(c['cost_a'])} -> {_cost(c['cost_b'])}, {c['unchanged']} unchanged"]
    for title, key in (("regressions", "regressions"), ("improvements", "improvements")):
        if c[key]:
            lines.append(f"{title}:")
            for i in c[key]:
                m, e = _model_label(i["model"], i["effort"])
                lines.append(f"  {i['task']}  {m}/{e}  {i['a']} -> {i['b']}")
    if c["only_in_one"]:
        lines.append("not in both runs: " + ", ".join(f"{i['task']} ({i['model'] or 'active'}/{i['effort'] or 'default'}, only {i['in']})" for i in c["only_in_one"]))
    if not c["regressions"] and not c["improvements"]:
        lines.append("no per-task differences")
    return "\n".join(lines)


def runs_text(runs: list[dict[str, Any]]) -> str:
    if not runs:
        return "No eval runs yet. `axon eval` runs the built-in golden suite."
    out = []
    for s in runs:
        out.append(f"{s['id']}  {s.get('created', '')[:16]}  {s['passed']}/{s['runs']} passed ({_pct(s['pass_rate'])})  {_cost(s['total_cost'])}  "
                   f"{len({(c['model'], c['effort']) for c in s['combos']})} combo(s)")
    return "\n".join(out)
