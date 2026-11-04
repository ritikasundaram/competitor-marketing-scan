"""Save a finished scan as reports/<date>_scan_<run>.json (dashboard) and .md (readable)."""
from __future__ import annotations

import datetime
import json
from collections import Counter
from pathlib import Path

from opentelemetry import trace as otel_trace

ROOT = Path(__file__).resolve().parent.parent
REPORTS = ROOT / "reports"
VERSION = 2


def _reason_kind(reason: str) -> str:
    if reason.startswith("old"):
        return "too old"
    if "doesn't mention" in reason:
        return "wrong company"
    if "isn't a source" in reason:
        return "made-up citation"
    return "other"


def build_data(state: dict) -> dict:
    answers = state.get("answers") or []
    findings = [f for a in answers for f in a["findings"]]
    dropped = state.get("dropped") or []
    profiles = state.get("profiles") or []
    plan = state.get("plan") or {}
    status = Counter(a["status"] for a in answers)
    span_ctx = otel_trace.get_current_span().get_span_context()
    sources = {}
    for sid, s in (state.get("sources") or {}).items():
        sources[sid] = {"url": s["url"], "title": s.get("title", ""), "date": s.get("date", ""),
                        "company": s.get("company", ""), "question_id": s.get("question_id", ""),
                        "excerpt": (s.get("text") or s.get("snippet") or "")[:400]}
    settings = state.get("settings") or {}
    return {
        "version": VERSION,
        "run_id": state["run_id"],
        "created": datetime.datetime.now().isoformat(timespec="seconds"),
        "company": state["company"],
        "profile": state.get("profile") or {},
        "rivals": state.get("rivals") or [],
        "plan": plan,
        "profiles": profiles,
        "questions": answers,
        "sources": sources,
        "dropped": dropped,
        "reviews": state.get("reviews") or [],
        "settings": {k: settings.get(k) for k in ("mock", "max_rivals", "questions_per_rival", "own_questions",
                                                   "max_attempts", "max_rounds", "max_age_days",
                                                   "approve_questions", "models")},
        "stats": {
            "questions": len(answers),
            "answered": status.get("answered", 0),
            "partial": status.get("partial", 0),
            "unanswered": status.get("unanswered", 0),
            "retried": sum(1 for a in answers if a["attempts"] > 1),
            "follow_ups": sum(1 for a in answers if a["question"]["round"] > 1),
            "findings": len(findings),
            "sources": len(sources),
            "flagged_numbers": sum(1 for f in findings if any("numbers not found" in x for x in f["flags"])),
            "undated": sum(1 for f in findings if "date unknown" in f["flags"]),
            "dropped": dict(Counter(_reason_kind(d.get("reason", "")) for d in dropped)),
            "lowered_by_rubric": sum(1 for p in profiles for c in p["campaigns"] if c.get("rubric_note")),
            "actions": len(plan.get("actions") or []),
            "actions_dropped": len(plan.get("dropped") or []),
            "actions_flagged": sum(1 for a in plan.get("actions") or [] if a.get("flags")),
        },
        "trace": {"session_id": state["run_id"],
                  "trace_id": format(span_ctx.trace_id, "032x") if span_ctx.is_valid else ""},
    }


def _link(sources: dict, ids: list) -> str:
    return ", ".join(f"[{i}]({sources[i]['url']})" for i in ids if i in sources)


def to_markdown(d: dict) -> str:
    c, st, src = d["company"], d["stats"], d["sources"]
    out = [f"# Competitor scan: {c['name']}", "",
           f"Run `{d['run_id']}` · {d['created'][:10]} · rivals: {', '.join(r['name'] for r in d['rivals'])}"
           + (" · **mock data**" if d["settings"].get("mock") else ""), ""]
    plan = d["plan"]
    out += ["## Action plan", "", f"**{plan.get('headline', '')}**", ""]
    for i, a in enumerate(plan.get("actions") or [], 1):
        out += [f"{i}. **{a['title']}** ({a['impact']} impact, {a['effort']} effort, {a['horizon'].lower()}, "
                f"{a['channel']})", f"   - What: {a['what']}", f"   - Why: {a['why']}",
                f"   - First step: {a['first_step']}", f"   - Evidence: {_link(src, a['source_ids'])}"]
        out += [f"   - ⚠ {fl}" for fl in a.get("flags") or []]
    out += [""]
    out += ["## Rivals", ""]
    for p in d["profiles"]:
        r = p["rival"]
        out += [f"### {r['name']} ({r.get('domain', '')}) · threat {p['threat']}", "", p["summary"], "",
                f"_Positioning:_ {p['positioning']}  ", f"_Threat:_ {p['threat_why']}", ""]
        for camp in p["campaigns"]:
            note = f" ({camp['rubric_note']})" if camp.get("rubric_note") else ""
            out.append(f"- **{camp['name']}** · {camp['strength']}{note} · {camp['channel']}: {camp['what']} "
                       f"{_link(src, camp['source_ids'])}")
        out.append("")
    out += ["## Questions researched", ""]
    for a in d["questions"]:
        q = a["question"]
        out += [f"- **{q['id']}** ({q['company']}, {q['dimension']}, {a['status']}"
                + (f", {a['attempts']} searches" if a["attempts"] > 1 else "") + f"): {q['question']}"]
        if a["answer"]:
            out.append(f"  - {a['answer']}")
        for f in a["findings"]:
            flags = f" ⚠ {'; '.join(f['flags'])}" if f["flags"] else ""
            out.append(f"  - {f['claim']} {_link(src, [f['source_id']])}{flags}")
    out += ["", "## Checks", "",
            f"- {st['questions']} questions: {st['answered']} answered, {st['partial']} partly, "
            f"{st['unanswered']} unanswered; {st['retried']} needed a second search; "
            f"{st['follow_ups']} were follow-ups from the gap review.",
            f"- {st['findings']} findings kept from {st['sources']} sources. Removed: "
            + (", ".join(f"{n} {k}" for k, n in st["dropped"].items()) or "none") + ".",
            f"- {st['flagged_numbers']} findings had numbers the source doesn't show (confidence set to Low); "
            f"{st['undated']} have no date.",
            f"- {st['lowered_by_rubric']} campaign ratings were lowered to fit the rubric; "
            f"{st['actions_dropped']} actions were dropped for citing no checked rival source.", ""]
    if d["dropped"]:
        out += ["### Removed findings", ""]
        out += [f"- {x.get('question_id', '')}: {x.get('claim', '')} ({x.get('reason', '')})" for x in d["dropped"]]
        out.append("")
    return "\n".join(out)


def save_report(state: dict, folder: Path | None = None) -> dict:
    folder = folder or REPORTS
    folder.mkdir(exist_ok=True)
    data = build_data(state)
    stem = f"{data['created'][:10]}_scan_{data['run_id']}"
    (folder / f"{stem}.json").write_text(json.dumps(data, indent=1, default=str))
    (folder / f"{stem}.md").write_text(to_markdown(data))
    return {"run_id": data["run_id"], "file": f"{stem}.json", "stats": data["stats"],
            "trace": data["trace"]}
