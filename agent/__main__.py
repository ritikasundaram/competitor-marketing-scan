"""Command line: python3 -m agent --company Figma --domain figma.com [--mock]"""
from __future__ import annotations

import argparse
import json
import sys

from dotenv import load_dotenv

load_dotenv()

from . import tracing  # noqa: E402
from .run import new_run_id, run_segment  # noqa: E402


def parse_rivals(spec: str) -> list:
    out = []
    for part in [p for p in spec.split(";") if p.strip()]:
        name, _, dom = part.partition(",")
        out.append({"name": name.strip(), "domain": dom.strip(), "reason": "given on the command line"})
    return out


def ask_rivals(rivals: list) -> list:
    print("\nCompetitors found:")
    for i, r in enumerate(rivals, 1):
        print(f"  {i}. {r['name']} ({r['domain']}) - {r.get('reason', '')}")
    raw = input("Keep which? (e.g. 1,3; Enter = all): ").strip()
    if not raw:
        return rivals
    keep = {int(x) for x in raw.replace(" ", "").split(",") if x.isdigit()}
    return [r for i, r in enumerate(rivals, 1) if i in keep]


def ask_questions(questions: list) -> list:
    print("\nPlanned questions:")
    for q in questions:
        print(f"  {q['id']} [{q['company']}] {q['question']}")
    raw = input("Remove any? (ids, e.g. Q2,Q5; Enter = keep all): ").strip().upper()
    drop = {x.strip() for x in raw.split(",") if x.strip()}
    return [q for q in questions if q["id"] not in drop]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Agentic competitor marketing scan")
    ap.add_argument("--company", required=True)
    ap.add_argument("--domain", default="")
    ap.add_argument("--description", default="")
    ap.add_argument("--goal", default="", help="what you want to learn, in plain words")
    ap.add_argument("--rivals", default="", help='skip discovery: "Sketch,sketch.com;Canva,canva.com"')
    ap.add_argument("--mock", action="store_true", help="free run with a fake web and fake AI")
    ap.add_argument("--yes", action="store_true", help="don't pause to confirm rivals")
    ap.add_argument("--approve-questions", action="store_true", help="pause to review the question list")
    ap.add_argument("--questions-per-rival", type=int, default=4)
    ap.add_argument("--max-rivals", type=int, default=4)
    ap.add_argument("--own-questions", type=int, default=2)
    ap.add_argument("--max-age-days", type=int, default=365)
    ap.add_argument("--no-trace", action="store_true", help="don't send traces to Phoenix")
    a = ap.parse_args(argv)

    if not a.no_trace:
        tracing.setup_phoenix()
    company = {"name": a.company, "domain": a.domain, "description": a.description, "goal": a.goal}
    if a.rivals:
        company["rivals"] = parse_rivals(a.rivals)
    settings = {"mock": a.mock, "confirm_rivals": not a.yes, "approve_questions": a.approve_questions,
                "questions_per_rival": a.questions_per_rival, "max_rivals": a.max_rivals,
                "own_questions": a.own_questions,
                "max_age_days": a.max_age_days}
    run_id = new_run_id()
    events = run_segment(run_id, start={"company": company, "settings": settings})
    while True:
        last = None
        for e in events:
            last = e
            if e["event"] == "step":
                print(f"• {e['message']}")
            elif e["event"] == "notice":
                print(f"  ({e['message']})")
            elif e["event"] == "question" and e["status"] in ("answered", "partial", "unanswered", "retrying"):
                extra = f" -> {e.get('query')}" if e["status"] == "retrying" else ""
                print(f"  {e['id']}: {e['status']}{extra}")
        if last is None:
            return 1
        if last["event"] == "error":
            print(f"Error: {last['message']}", file=sys.stderr)
            return 1
        if last["event"] == "done":
            rep = last["report"]
            print(f"\nDone. reports/{rep['file']} (and .md)")
            print(json.dumps(rep["stats"], indent=1))
            return 0
        answer = ask_rivals(last["rivals"]) if last["kind"] == "confirm_rivals" else ask_questions(last["questions"])
        events = run_segment(run_id, resume=answer)


if __name__ == "__main__":
    raise SystemExit(main())
