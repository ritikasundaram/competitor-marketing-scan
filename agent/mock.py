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
          "ActionPlan": _actions, "EventLeads": _event_leads, "RivalEventProfile": _rival_events,
          "FieldSummary": _field_summary}.get(name)
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
    dims = ["events"] * per if "field marketing" in system else DIMENSIONS[:per]
    for name, dom in rivals:
        for i, dim in enumerate(dims):
            if dim == "events":
                kind = ["conferences or summits", "meetups or roadshows", "sponsored trade shows", "webinars"][i % 4]
                qs.append({"company": name, "dimension": "events",
                           "question": f"Which {kind} has {name} hosted or announced in the last 12 months?",
                           "search_query": f"{name} ({dom}) {kind} event 2026"})
                continue
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
    want = "attendees" if "(attendance)" in user else "reactions" if "(engagement)" in user else None
    for sid, body in _SOURCE.findall(user):
        sentences = re.split(r"(?<=[.!?])\s", body.strip())
        first = next((x for x in sentences if want and want in x), sentences[0])
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
        found = re.findall(r"^  - \[(\S+?)(?: · [^\]]*)?\] (.+?) \(date:", block, re.M)
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
                     "why": f"Rated {strength}; leaving it unanswered cedes the story"
                            + (" to a rival growing 300% a year." if i == 0 else "."),
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


def _field_summary(system, user):
    events = re.findall(r"^- (.+?) \[(.+?)\]", user, re.M)
    found = [{"text": f"{name} is the most visible event in this group (mock).",
              "source_ids": [x.strip() for x in ids.split(",")]} for name, ids in events[:3]]
    if events:  # planted: a number no source shows, which the check should flag
        found.append({"text": "Rival events grew 250% this year (mock).",
                      "source_ids": [x.strip() for x in events[0][1].split(",")]})
    found.append({"text": "Rivals favor online webinars (mock).", "source_ids": ["M9"]})  # planted: fake id
    return {"headline": "Rivals lean on one flagship summit each, backed by smaller meetups (mock).",
            "findings": found}


def _event_leads(system, user):
    rivals = re.findall(r"([A-Z][\w.]*) \(([\w.-]+\.\w+)\)", (re.search(r"Rivals: (.+)", user) or [None, ""])[1])
    out = []
    for name, _ in rivals:
        out.append({"company": name, "name": f"{name} Summit 2026", "kind": "summit", "date": "2026-09", "importance": 5})
        out.append({"company": name, "name": f"{name} Meetup Tour", "kind": "meetup", "date": "", "importance": 3})
        out.append({"company": name, "name": f"{name} Summit", "kind": "summit", "date": "", "importance": 2})
    return {"events": out}


def _rival_events(system, user):
    comp = (re.search(r"Competitor: (.+?) \(", user) or [None, "Rival"])[1]
    blocks = {}
    for block in re.split(r"^Q ", user, flags=re.M)[1:]:
        dim = (re.search(r"^\S+ \(.+?, (\w+)\)", block) or [None, ""])[1]
        blocks.setdefault(dim, []).extend(re.findall(r"^  - \[(\S+?)(?: · [^\]]*)?\] (.+?) \(date:", block, re.M))
    att = next((c for _, c in blocks.get("attendance", []) if "attendees" in c), "")
    att = (re.search(r"[\d,]+ attendees", att) or [""])[0]
    eng = next((c for _, c in blocks.get("engagement", []) if "reactions" in c), "")
    eng = (re.search(r"[\d,]+ reactions and [\d,]+ comments", eng) or [""])[0]
    summit_ids = [i for i, _ in blocks.get("attendance", [])[:2] + blocks.get("engagement", [])[:1]]
    tour_ids = [i for i, _ in blocks.get("events", [])[:1]]
    events = []
    if summit_ids:
        events.append({"name": f"{comp} Summit 2026", "kind": "summit", "organizer": comp, "run_by_rival": True,
                       "date": "2026-09", "location": "San Francisco",
                       "format": "in-person", "audience": "product teams", "attendees": att, "engagement": eng,
                       "coverage": "TechCrunch recap", "partners": "2 agency partners", "strength": "Strong",
                       "why": "Mock: stated attendance and engagement.", "source_ids": summit_ids})
    if tour_ids:
        events.append({"name": f"{comp} Meetup Tour", "kind": "meetup", "organizer": comp, "run_by_rival": True,
                       "date": "", "location": "",
                       "format": "in-person", "audience": "designers", "attendees": "10,000 attendees",
                       "engagement": "", "coverage": "", "partners": "", "strength": "Strong",
                       "why": "Mock: invents an attendance number the checks should catch.",
                       "source_ids": tour_ids})
    if tour_ids:  # two more planted mistakes: a look-alike company's event, and an old one
        events.append({"name": "City TechWeek", "kind": "sponsorship", "organizer": f"{comp} Development Services",
                       "run_by_rival": False, "date": "2026-04", "location": "St. Louis", "format": "in-person",
                       "strength": "Weak", "why": "Mock: a different company with a similar name.",
                       "source_ids": tour_ids})
        events.append({"name": f"{comp} Enterprise 2020", "kind": "conference", "organizer": comp,
                       "run_by_rival": True, "date": "2020", "location": "", "format": "unknown",
                       "strength": "Weak", "why": "Mock: an old event from an undated page.", "source_ids": tour_ids})
    return {"summary": f"{comp} runs a flagship summit plus a meetup tour (mock).",
            "positioning": f"{comp} uses events to look like the community leader (mock).",
            "events": events, "threat": "Medium", "threat_why": "Big flagship event, overlapping audience (mock)."}
