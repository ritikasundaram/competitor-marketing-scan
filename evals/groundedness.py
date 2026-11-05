"""Groundedness evals, written back to Phoenix as span annotations.

Two steps are graded, each against exactly what its AI saw:

- `groundedness` on every `answer` span: a researcher's answer to one small question, judged against
  the pages Exa returned. Graded before the rule-based checks remove anything, so it measures how often
  the researcher itself goes beyond its sources.
- `profile_groundedness` on every `profile_rival` span: one rival's profile as it lands in the report.
  Each event or campaign (and the summary) is judged on its own against the findings it cites, the
  strength rubric and the scan's rules (right company, not too old, only attendance earns Strong).
  The profile passes only if every item does; its score is the share that passed, and the
  explanation names the items that failed.

Claude Haiku 5.5 is the judge (Phoenix's faithfulness evaluator). Mock-mode spans are skipped, and a
span that already has the annotation is never graded twice.

    python3 -m evals.groundedness                  # backfill the last 2 days, then stop
    python3 -m evals.groundedness --watch          # backfill, then keep grading new spans
    python3 -m evals.groundedness --only profile --days 7 --limit 20

Key: ANTHROPIC_API_KEY in .env (the same one the agent uses). Set EVAL_ANTHROPIC_API_KEY to bill
evals to a different key, and EVAL_MODEL to use another model.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
from dotenv import load_dotenv

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(ROOT, ".env"))

PHOENIX = os.getenv("PHOENIX_URL") or os.getenv("PHOENIX_COLLECTOR_ENDPOINT", "http://localhost:6006").split("/v1/")[0]
PROJECT = os.getenv("PHOENIX_PROJECT_NAME", "default")
MODEL = os.getenv("EVAL_MODEL", "claude-haiku-5-5")
SOURCE_CHARS = 1500  # same cut-off the researcher sees (agent.graph.fmt_sources)
LABELS = {"faithful": "grounded", "unfaithful": "ungrounded"}


def log(msg: str) -> None:
    print(f"[{dt.datetime.now():%H:%M:%S}] {msg}", flush=True)


# ---------- what each eval sees ----------

def answer_input(span: dict) -> dict:
    """The question, the pages Exa returned (what the researcher saw), and what it wrote."""
    a = span["attributes"]
    inp, out = json.loads(a["input.value"]), json.loads(a["output.value"])
    q = inp["question"]
    pages = [f"[{d['id']}] {d.get('title', '')} | {d['url']} | date: {d.get('date') or 'unknown'}\n"
             + (d.get("text") or d.get("snippet") or "")[:SOURCE_CHARS].replace("\n", " ")
             for d in inp.get("docs") or []]
    claims = "\n".join(f"- {f['claim']} [{f.get('source_id', '')}]" for f in out.get("kept") or [])
    return {"input": f"{q['question']} (about {q['company']}, {q.get('domain', '')})",
            "output": (out.get("answer") or "") + (f"\n\nClaims:\n{claims}" if claims else ""),
            "context": f"Today's date: {span['start_time'][:10]}\n\n" + ("\n\n".join(pages) or "(no sources)")}


def _host(url: str) -> str:
    from urllib.parse import urlparse
    return urlparse(url or "").netloc.removeprefix("www.")


def _findings(answers: list, ids: set = None, sources: dict = None) -> str:
    """Research findings as the profile step saw them; with `ids`, only the findings citing those sources.

    Each line also names the source's website, so the judge can tell the rival's own pages from outside
    ones when checking a strength rating."""
    sources = sources or {}
    lines = []
    for a in answers:
        fs = [f for f in a["findings"] if ids is None or f["source_id"] in ids]
        if not fs:
            continue
        q = a["question"]
        lines.append(f"Q {q['id']} ({q['company']}, {q['dimension']}): {q['question']}")
        for f in fs:
            flag = f" [flags: {'; '.join(f['flags'])}]" if f.get("flags") else ""
            site = _host((sources.get(f["source_id"]) or {}).get("url", ""))
            lines.append(f"  - [{f['source_id']}{' · ' + site if site else ''}] {f['claim']} "
                         f"(date: {f.get('date') or 'unknown'}){flag}")
    return "\n".join(lines) or "(no findings cite these sources)"


def _pages(ids: list, sources: dict) -> str:
    """The text of each cited page, cut like the researcher's view, so figures on a page are visible."""
    out = []
    for i in ids:
        x = sources.get(i)
        if x:
            body = (x.get("text") or x.get("snippet") or "")[:SOURCE_CHARS].replace("\n", " ")
            out.append(f"[{i} · {_host(x.get('url', ''))}] {x.get('title', '')} | date: {x.get('date') or 'unknown'}\n{body}")
    return "\n\n".join(out) or "(pages not available)"


def profile_items(span: dict) -> list:
    """One eval input per event or campaign in a rival's profile, plus one for its summary and threat.

    Each item is judged only against the findings it cites, so a slip in one event can't hide in a long
    profile, and the explanation says which item went wrong."""
    from agent.checks import RUBRIC
    a = span["attributes"]
    inp, out = json.loads(a["input.value"]), json.loads(a["output.value"])
    st, rival = inp["state"], inp["rival"]
    answers, sources = st.get("answers") or [], st.get("sources") or {}
    field = (st.get("settings") or {}).get("focus") == "field"
    days = (st.get("settings") or {}).get("max_age_days") or 365
    p = out["profiles"][0]
    today = f"Today's date: {span['start_time'][:10]}"
    who = f"{rival['name']} ({rival.get('domain', '')})"
    rules = (f"Only events that {who} itself hosted, co-hosted or sponsored count, not events by another "
             f"company with a similar name. Events held more than {days} days before today don't count; "
             f"upcoming, announced events do. A stated attendance figure counts as a concrete result for a "
             f"Strong rating; engagement counts (reactions, comments) alone don't."
             if field else
             f"Only marketing by {who} itself counts. Marketing from more than {days} days before today "
             f"doesn't count.")
    rules += (" Labels such as kind, format, channel and the campaign or event name are the writer's own "
              "categories: judge them for being reasonable, not for appearing word for word. A source on "
              f"{rival.get('domain', 'the rival')}'s own site is the rival's own announcement; other sites "
              "are outside sources. A rating lower than the evidence would allow is cautious, not "
              "unfaithful: fail a rating only if it is higher than the evidence supports.")
    items = []
    for e in (p.get("events") or []) if field else (p.get("campaigns") or []):
        if field:
            facts = "\n".join(f"{k.capitalize()}: {e[k]}" for k in (
                "organizer", "kind", "date", "location", "format", "attendees", "engagement", "coverage",
                "partners") if e.get(k) and e[k] != "unknown")
            text = f"Event: {e['name']}\n{facts}\nStrength: {e['strength']}\nWhy: {e['why']}"
        else:
            text = (f"Campaign: {e['name']}\nChannel: {e['channel']}\nWhat: {e['what']}\n"
                    f"Strength: {e['strength']}\nWhy: {e['why']}")
        items.append((e["name"], {
            "input": f"One entry in {who}'s {'events' if field else 'marketing'} profile, written from research "
                     f"findings and rated with the rubric. {rules}",
            "output": text,
            "context": f"{today}\n\n{RUBRIC}\n\nFindings it cites:\n"
                       f"{_findings(answers, set(e['source_ids']), sources)}\n\nThe cited pages:\n"
                       f"{_pages(e['source_ids'], sources)}"}))
    me, prof = st.get("company") or {}, st.get("profile") or {}
    items.append(("summary and threat", {
        "input": f"The summary of {who}'s {'events' if field else 'marketing'}, written from research findings, "
                 f"and how much of a threat it is to {me.get('name', 'the user')}'s company.",
        "output": f"Summary: {p['summary']}\nPositioning: {p['positioning']}\nThreat: {p['threat']} ({p['threat_why']})",
        "context": f"{today}\n\nThe user's company: {me.get('name', '')} ({me.get('domain', '')}). "
                   f"{prof.get('summary', '')} Products: {', '.join(prof.get('products') or [])}.\n\n"
                   f"Research findings about {rival['name']}:\n"
                   + _findings([x for x in answers if x["question"]["company"] == rival["name"]], None, sources)}))
    return items


def is_answer(s: dict) -> bool:
    a = s["attributes"]
    return s["name"] == "answer" and a.get("metadata.langgraph_node") == "answer"


def is_profile(s: dict) -> bool:
    return s["name"] == "profile_rival"


TARGETS = {
    "answer": {"name": "groundedness", "match": is_answer, "build": answer_input},
    "profile": {"name": "profile_groundedness", "match": is_profile, "build": None},
}


# ---------- reading from Phoenix ----------

def find_spans(http: httpx.Client, since: dt.datetime, targets: list) -> dict:
    """Non-mock, finished spans for each target that started after `since`."""
    out, cursor = {t: [] for t in targets}, None
    while True:
        params = {"start_time": since.isoformat(), "limit": 1000}
        if cursor:
            params["cursor"] = cursor
        r = http.get(f"/v1/projects/{PROJECT}/spans", params=params)
        r.raise_for_status()
        body = r.json()
        for s in body["data"]:
            a = s["attributes"]
            if a.get("metadata.mock") or s.get("status_code") != "OK" or not a.get("output.value"):
                continue
            for t in targets:
                if TARGETS[t]["match"](s):
                    out[t].append(s)
        cursor = body.get("next_cursor")
        if not cursor:
            return out


def answer_spans(http: httpx.Client, since: dt.datetime) -> list:
    return find_spans(http, since, ["answer"])["answer"]


def already_graded(client, span_ids: list, name: str) -> set:
    done = set()
    for i in range(0, len(span_ids), 100):
        rows = client.spans.get_span_annotations(span_ids=span_ids[i:i + 100], project_identifier=PROJECT,
                                                 include_annotation_names=[name], limit=1000, timeout=30)
        done.update(r["span_id"] for r in rows)
    return done


# ---------- grading ----------

def make_evaluator():
    from phoenix.evals import LLM
    from phoenix.evals.metrics import FaithfulnessEvaluator
    key = os.getenv("EVAL_ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_API_KEY")
    if not key:
        sys.exit("No Anthropic key. Add ANTHROPIC_API_KEY (or EVAL_ANTHROPIC_API_KEY) to .env.")
    return FaithfulnessEvaluator(llm=LLM(provider="anthropic", model=MODEL, sync_client_kwargs={"api_key": key}))


def judge(evaluator, x: dict):
    """One judge call, retried: the model now and then answers without a label."""
    for attempt in range(3):
        try:
            return evaluator.evaluate(x)[0]
        except (KeyError, ValueError):
            if attempt == 2:
                raise
            time.sleep(1)


def grade(evaluator, span: dict, target: str) -> dict:
    t, a = TARGETS[target], span["attributes"]
    meta = {"model": MODEL, "run_id": a.get("metadata.run_id", "")}
    if target == "answer":
        sc = judge(evaluator, answer_input(span))
        meta["question_id"] = a.get("metadata.question_id", "")
        result = {"label": LABELS.get(sc.label, sc.label), "score": sc.score, "explanation": sc.explanation}
    else:
        # Grade every item; the profile passes only if all do, and its score is the share that passed.
        graded = [(name, judge(evaluator, x)) for name, x in profile_items(span)]
        bad = [(name, sc) for name, sc in graded if sc.label != "faithful"]
        meta.update({"rival": json.loads(a["input.value"])["rival"]["name"], "items": len(graded),
                     "items_ungrounded": [name for name, _ in bad]})
        result = {"label": "ungrounded" if bad else "grounded",
                  "score": round(1 - len(bad) / len(graded), 3),
                  "explanation": (f"{len(bad)} of {len(graded)} items not supported by the findings they cite:\n"
                                  + "\n".join(f"- {name}: {sc.explanation}" for name, sc in bad))
                                 if bad else f"All {len(graded)} items are supported by the findings they cite."}
    return {"span_id": span["context"]["span_id"], "name": t["name"], "annotator_kind": "LLM",
            "result": result, "metadata": meta}


def run_once(client, http, evaluator, since: dt.datetime, targets: list, limit: int = 0, workers: int = 4) -> int:
    found = find_spans(http, since, targets)
    total = 0
    for target in targets:
        spans, name = found[target], TARGETS[target]["name"]
        if not spans:
            continue
        done = already_graded(client, [s["context"]["span_id"] for s in spans], name)
        todo = [s for s in spans if s["context"]["span_id"] not in done][:limit or None]
        if not todo:
            continue
        log(f"{name}: grading {len(todo)} span(s) with {MODEL} ({len(done)} already graded)…")
        results, failed = [], 0

        def one(s):
            try:
                return grade(evaluator, s, target)
            except Exception as e:  # keep going; a failed span is retried on the next pass
                log(f"  could not grade {s['context']['span_id']}: {str(e)[:160]}")
                return None

        with ThreadPoolExecutor(max_workers=workers) as pool:
            for r in pool.map(one, todo):
                if r is None:
                    failed += 1
                    continue
                results.append(r)
                if len(results) % 25 == 0:  # write in batches so progress shows up in Phoenix
                    client.spans.log_span_annotations(span_annotations=results[-25:], sync=True)
        rest = len(results) % 25
        if rest:
            client.spans.log_span_annotations(span_annotations=results[-rest:], sync=True)
        grounded = sum(1 for r in results if r["result"]["label"] == "grounded")
        log(f"{name}: {grounded} grounded, {len(results) - grounded} ungrounded"
            + (f", {failed} failed (will retry)" if failed else "") + ".")
        total += len(results)
    return total


def main(argv=None) -> None:
    p = argparse.ArgumentParser(description="Grade research answers and rival profiles for groundedness.")
    p.add_argument("--days", type=float, default=2, help="how far back to backfill (default 2)")
    p.add_argument("--watch", action="store_true", help="keep running and grade new spans as they arrive")
    p.add_argument("--every", type=int, default=60, help="seconds between checks in --watch mode")
    p.add_argument("--only", choices=sorted(TARGETS), help="grade just one step")
    p.add_argument("--limit", type=int, default=0, help="grade at most this many per step per pass (0 = all)")
    p.add_argument("--workers", type=int, default=4)
    args = p.parse_args(argv)
    targets = [args.only] if args.only else list(TARGETS)

    from phoenix.client import Client
    client = Client(base_url=PHOENIX)
    evaluator = make_evaluator()
    log(f"Groundedness evals ({', '.join(TARGETS[t]['name'] for t in targets)}) on Phoenix project "
        f"'{PROJECT}' at {PHOENIX}")
    with httpx.Client(base_url=PHOENIX, timeout=60) as http:
        since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=args.days)
        run_once(client, http, evaluator, since, targets, args.limit, args.workers)
        if not args.watch:
            return
        log(f"Watching for new spans every {args.every}s. Stop with Ctrl-C.")
        while True:
            time.sleep(args.every)
            # Look back an hour each time: spans arrive after they finish, and graded ones are skipped.
            recent = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)
            try:
                run_once(client, http, evaluator, recent, targets, args.limit, args.workers)
            except httpx.HTTPError as e:
                log(f"Phoenix not reachable ({e}); trying again in {args.every}s.")


if __name__ == "__main__":
    main()
