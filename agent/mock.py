"""Canned AI answers for mock mode and tests. Reads the real prompts, returns schema-valid dicts.

On purpose, it makes a few mistakes the checks should catch:
- cites a source id that doesn't exist (Q1 cites Q99-1) -> dropped
- adds a number the source never says (40% faster) -> flagged, confidence Low
- finds nothing for pricing questions on the first try -> the researcher retries
- rates everything Strong -> the rubric lowers what the sources can't back
- proposes one action citing nothing real -> dropped
"""
from __future__ import annotations

import re

DIMENSIONS = ["launches", "partnerships", "events", "pricing", "channels", "positioning", "customers"]


def _text(messages) -> tuple[str, str]:
    system = str(messages[0].content) if messages else ""
    user = str(messages[-1].content) if messages else ""
    return system, user


def respond(name: str, messages) -> dict:
    system, user = _text(messages)
    fn = {"CompanyProfile": _profile, "QuestionPlan": _plan, "ResearchAnswer": _answer,
          "Judgment": _judge, "FollowUps": _follow_ups, "RivalProfile": _rival,
          "ActionPlan": _actions}.get(name)
    return fn(system, user) if fn else {}


def _profile(system, user):
    m = re.search(r"Company: (.+?)(?: \((.+?)\))?\n", user)
    name = m.group(1) if m else "Acme"
    return {"summary": f"{name} makes a collaborative design tool for product teams (mock).",
            "market": "collaborative design tools",
            "audiences": ["product designers", "developers", "marketing teams"],
            "products": ["design editor", "prototyping", "dev handoff", "whiteboard"],
            "focus": "find rival marketing moves worth answering this quarter"}


def _plan(system, user):
    rivals = re.findall(r"^- (.+?) \((.+?)\):", user, re.M)
    per = int((re.search(r"exactly (\d+) questions per rival", user) or [0, 3])[1])
    own_n = int((re.search(r"and (\d+) about", user) or [0, 2])[1])
    me = re.search(r"User's company: (.+?) \((.+?)\)", user)
    qs = []
    for name, dom in rivals:
        for dim in DIMENSIONS[:per]:
            qs.append({"company": name, "dimension": dim,
                       "question": f"What {dim} has {name} announced in the last 12 months?",
                       "search_query": f"{name} ({dom}) {dim} announcement 2026"})
    if me:
        for dim in ["campaigns", "events"][:own_n]:
            qs.append({"company": me.group(1), "dimension": dim,
                       "question": f"What {dim} has {me.group(1)} run in the last 12 months?",
                       "search_query": f"{me.group(1)} ({me.group(2)}) {dim} 2026"})
    return {"questions": qs}


_SOURCE = re.compile(r"^\[(\S+?)\] .*?\n(.+?)(?=\n\n\[|\Z)", re.S | re.M)


def _answer(system, user):
    qid = (re.search(r"Question (Q\d+)", user) or [None, ""])[1]
    if "(pricing)" in user and "Attempt: 1" in user:
        return {"answer": "The sources don't cover pricing.", "findings": []}
    findings = []
    for sid, body in _SOURCE.findall(user):
        first = re.split(r"(?<=[.!?])\s", body.strip())[0]
        if "Page text for" in first:
            first = "The company announced a new launch for teams, with 12 partner integrations and 40% faster onboarding."
        findings.append({"claim": first, "source_id": sid, "date": "", "confidence": "High"})
    if qid == "Q1":
        findings.append({"claim": "It also ran a billboard campaign.", "source_id": "Q99-1", "confidence": "Medium"})
    return {"answer": f"Mock answer for {qid} from {len(findings)} findings.", "findings": findings}


def _judge(system, user):
    q = (re.search(r"Question about .+?: (.+)", user) or [None, "the question"])[1]
    if "Nothing usable" in system:
        return {"answered": False, "missing": "no relevant sources", "next_query": q + " official announcement"}
    claims = re.findall(r"^- (.+)$", user, re.M)
    return {"answered": True, "missing": "", "next_query": "", "answer": " ".join(claims[:2])}


def _follow_ups(system, user):
    rivals = re.findall(r"([A-Z][\w.]*) \(([\w.-]+\.\w+)\)", (re.search(r"Rivals: (.+)", user) or [None, ""])[1])
    if not rivals:
        return {"gaps": "No gaps worth another round.", "questions": []}
    name, dom = rivals[0]
    return {"gaps": f"Little is known about who {name}'s customers are.",
            "questions": [{"company": name, "dimension": "customers",
                           "question": f"Which customer stories has {name} published in the last 6 months?",
                           "search_query": f"{name} ({dom}) customer story case study 2026"}]}


def _rival(system, user):
    comp = (re.search(r"Competitor: (.+?) \(", user) or [None, "Rival"])[1]
    camps = []
    for block in re.split(r"^Q ", user, flags=re.M)[1:]:
        dim = (re.search(r"^\S+ \(.+?, (\w+)\)", block) or [None, "campaign"])[1]
        found = re.findall(r"^  - \[(\S+?)\] (.+?) \(date:", block, re.M)
        if found and len(camps) < 4:
            camps.append({"name": f"{comp} {dim} push", "what": found[0][1], "channel": "press",
                          "strength": "Strong", "why": "Mock rating: the mock calls everything strong.",
                          "source_ids": [sid for sid, _ in found[:3]]})
    return {"summary": f"{comp} is pushing launches and partnerships at teams (mock).",
            "positioning": f"{comp} pitches itself as the simpler option (mock).",
            "campaigns": camps, "threat": "Medium", "threat_why": "Overlapping audience, moderate pickup (mock)."}


def _actions(system, user):
    moves = re.findall(r"^- (.+?) \[(.+?)\] (\w+): (.+)$", user, re.M)
    acts = []
    for i, (name, ids, strength, what) in enumerate(moves[:4]):
        acts.append({"title": f"Answer the {name}", "what": f"Respond to this move: {what[:140]}",
                     "why": f"Rated {strength}; leaving it unanswered cedes the story.",
                     "first_step": "Draft a one-page response brief and share it with the team.",
                     "channel": ["press", "social", "email", "events"][i % 4],
                     "effort": ["Low", "Medium", "High", "Medium"][i % 4],
                     "impact": ["High", "High", "Medium", "Low"][i % 4],
                     "horizon": ["This month", "This quarter", "This quarter", "This half"][i % 4],
                     "source_ids": [x.strip() for x in ids.split(",")]})
    acts.append({"title": "Launch a podcast", "what": "Start a weekly podcast.", "why": "Everyone has one.",
                 "first_step": "Buy a mic.", "channel": "audio", "effort": "High", "impact": "Low",
                 "horizon": "This half", "source_ids": ["M9"]})
    return {"headline": "Rivals are winning attention with partner launches; answer the strongest two first (mock).",
            "actions": acts}
