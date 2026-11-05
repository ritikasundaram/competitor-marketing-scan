"""The agent: a LangGraph graph that plans, researches with Exa, checks and recommends.

    understand -> find_rivals -> confirm_rivals (pause) -> plan_questions
      -> approve_questions (optional pause) -> research xN (parallel researchers)
      -> review_gaps -> (one more research round if gaps) -> profile_rival xN (parallel)
      -> plan_actions -> write_report

Field focus: research -> event_followups (attendance + social engagement questions) -> research
      -> profile_rival xN -> summarize_events (key findings, no action plan) -> write_report

Each researcher is its own small graph that loops:
    search (Exa) -> read (Exa) -> answer (AI) -> check (rules) -> judge (AI) -> retry or stop
"""
from __future__ import annotations

import datetime
import operator
import re
import time
from typing import Annotated, Optional, TypedDict

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send, interrupt

from . import exa, mock, tracing
from .checks import (RUBRIC, check_findings, clean_ids, host, norm_url, on_own_site, rubric_cap, scrub_people,
                     similar, unverified_numbers)
from .llm import (LLMStop, exhausted, get_model, is_daily_quota, is_stuck, is_transient, llm_stop_reason,
                  mark_exhausted, model_chain)
from .schemas import (ActionPlan, CompanyProfile, EventLeads, FieldSummary, FollowUps, Judgment, QuestionPlan,
                      ResearchAnswer, RivalEventProfile, RivalProfile)
from .tracing import KIND

DEFAULTS = {
    "mock": False,
    "max_rivals": 4,
    "questions_per_rival": 4,
    "own_questions": 2,
    "max_attempts": 2,          # searches per question, counting the first
    "results_per_search": 6,
    "pages_per_search": 3,
    "max_age_days": 365,
    "max_rounds": 2,            # research rounds, counting the first
    "max_follow_ups": 4,
    "confirm_rivals": True,
    "approve_questions": False,
    "models": {},
    "focus": "general",          # or "field": rivals' events, attendance and engagement
    "max_events_per_rival": 2,   # field focus: events that get attendance + engagement questions
}


def merge_dicts(a: dict, b: dict) -> dict:
    return {**(a or {}), **(b or {})}


class ScanState(TypedDict, total=False):
    run_id: str
    company: dict           # name, domain, description, goal
    settings: dict
    profile: dict           # what the company is, from its own site
    rivals: list            # confirmed rivals
    questions: list         # every question asked, all rounds
    round: int
    answers: Annotated[list, operator.add]
    sources: Annotated[dict, merge_dicts]     # id -> {url, title, date, snippet, text, company}
    dropped: Annotated[list, operator.add]    # findings the checks removed, with reasons
    reviews: Annotated[list, operator.add]
    profiles: Annotated[list, operator.add]
    plan: dict
    summary: dict           # field focus: key findings about rivals' events
    report: dict


def today() -> datetime.date:
    return datetime.date.today()


def emit(event: str, **data) -> None:
    """Live progress for the web app (LangGraph 'custom' stream)."""
    try:
        get_stream_writer()({"event": event, **data})
    except Exception:  # noqa: BLE001  not streaming (e.g. tests calling invoke)
        pass


def ask(role: str, schema, system: str, user: str, settings: dict):
    """One structured AI call. Auto-traced as an LLM span by OpenInference.

    Tries the role's models in order: a model that hits its daily free-tier limit is skipped
    for the rest of the day; busy errors are retried with waits, then the next model is tried.
    """
    messages = [SystemMessage(content=system), HumanMessage(content=user)]
    chain = [m for m in model_chain(role, settings) if not exhausted(m)]
    if not chain:
        raise LLMStop("Every Gemini model for this step has hit its daily free-tier limit. "
                      "Try again later (limits reset daily) or add billing in Google AI Studio.")
    last_error: Exception | None = None
    for i, name in enumerate(chain):
        # Forced tool call, parsed here (not by a LangChain parser) so Phoenix shows one clean LLM span.
        model = get_model(role, settings, mock_responder=mock.respond, model=name).bind_tools(
            [schema], tool_choice=schema.__name__)
        try:
            return _structured(model, schema, messages)
        except _TryNextModel as e:
            last_error = e.cause
            if is_daily_quota(e.cause):
                mark_exhausted(name, e.cause)
            nxt = chain[i + 1] if i + 1 < len(chain) else None
            if nxt:
                why = ("hit its daily free limit" if is_daily_quota(e.cause)
                       else "timed out" if is_stuck(e.cause) else "is busy")
                emit("notice", message=f"{name} {why}; switching to {nxt}…")
    raise LLMStop(llm_stop_reason(last_error, transient_exhausted=True) if last_error else "The AI failed.")


class _TryNextModel(Exception):
    def __init__(self, cause: Exception):
        super().__init__(str(cause))
        self.cause = cause


def _structured(model, schema, messages):
    last_error = None
    for _ in range(2):
        msg = _invoke_patiently(model, messages)
        calls = [c for c in (getattr(msg, "tool_calls", None) or []) if c["name"] == schema.__name__]
        try:
            return schema.model_validate(calls[0]["args"])
        except (IndexError, ValueError) as e:
            last_error = e
            problem = str(e).split("For further information")[0].strip()[:400]
            messages = messages + [HumanMessage(content=f"Answer by calling {schema.__name__} with valid fields. "
                                                        f"Your last answer had this problem: {problem}")]
    raise RuntimeError(f"The model didn't return a valid {schema.__name__}: {last_error}")


RETRY_WAITS = (5, 15, 30)  # seconds; busy servers and per-minute quotas usually clear


def _invoke_patiently(model, messages):
    for attempt in range(len(RETRY_WAITS) + 1):
        try:
            return model.invoke(messages)
        except Exception as e:  # noqa: BLE001
            reason = llm_stop_reason(e)
            if reason:
                raise LLMStop(reason) from e
            if is_daily_quota(e) or is_stuck(e):
                raise _TryNextModel(e) from e
            if not is_transient(e):
                raise
            if attempt == len(RETRY_WAITS):
                raise _TryNextModel(e) from e
            emit("notice", message=f"The AI is busy; retrying in {RETRY_WAITS[attempt]}s…")
            time.sleep(RETRY_WAITS[attempt])


def who(c: dict) -> str:
    """'Sketch (sketch.com)'. Works for companies ({name}) and questions ({company})."""
    name = c.get("name") or c.get("company", "")
    return f"{name} ({c['domain']})" if c.get("domain") else name


def fmt_sources(docs: list, chars: int = 1500) -> str:
    out = []
    for d in docs:
        body = (d.get("text") or d.get("snippet") or "")[:chars].replace("\n", " ")
        out.append(f"[{d['id']}] {d.get('title', '')} | {d['url']} | date: {d.get('date') or 'unknown'}\n{body}")
    return "\n\n".join(out) or "(no sources)"


def fmt_findings(answers: list) -> str:
    lines = []
    for a in answers:
        q = a["question"]
        lines.append(f"Q {q['id']} ({q['company']}, {q['dimension']}): {q['question']}")
        lines.append(f"  status: {a['status']}")
        for f in a["findings"]:
            flag = f" [flags: {'; '.join(f['flags'])}]" if f.get("flags") else ""
            lines.append(f"  - [{f['source_id']}] {f['claim']} (date: {f.get('date') or 'unknown'}){flag}")
    return "\n".join(lines) or "(no findings)"


def fmt_research(answers: list, sources: dict, own_domain: str = "") -> str:
    """For the profile step: each question with the researcher's own answer (which carries its caveats,
    like "may be a different company"), then its checked findings with the website each source is on."""
    lines = []
    for a in answers:
        q = a["question"]
        lines.append(f"Q {q['id']} ({q['company']}, {q['dimension']}): {q['question']}")
        lines.append(f"  status: {a['status']}")
        if a.get("answer"):
            lines.append(f"  researcher's answer: {a['answer']}")
        for f in a["findings"]:
            site = host((sources.get(f["source_id"]) or {}).get("url", ""))
            mark = " (own site)" if own_domain and site.endswith(own_domain.lower()) else ""
            flag = f" [flags: {'; '.join(f['flags'])}]" if f.get("flags") else ""
            lines.append(f"  - [{f['source_id']}{' · ' + site + mark if site else ''}] {f['claim']} "
                         f"(date: {f.get('date') or 'unknown'}){flag}")
    return "\n".join(lines) or "(no findings)"


PROFILE_RULES = (
    "Read each researcher's answer, not just the findings: if it says a source may be about a different "
    "company, or a figure isn't clearly tied to the event, respect that. Cite the source ids of the "
    "findings that state each fact and figure, and nothing else. A source marked (own site) is the "
    "competitor's own announcement; other sites are outside sources."
)


# ---------- 1. understand the user's company ----------

def understand(state: ScanState) -> dict:
    c, s = state["company"], state["settings"]
    emit("step", step="understand", message=f"Reading {c['name']}'s own site…")
    client = exa.make_client(s["mock"])
    docs = exa.search(client, f"{who(c)} products, features and who it is for", "ME", num=4)
    docs = [d for d in docs if on_own_site(d["url"], c.get("domain", ""))] or docs[:2]
    docs = exa.read(client, docs, max_pages=2)
    prof = ask("planner", CompanyProfile,
               "You profile a company from its own web pages. Use only the sources. Be concrete.",
               f"Company: {who(c)}\nDescription from the user: {c.get('description') or '(none)'}\n"
               f"The user's goal for this scan: {c.get('goal') or 'see what competitors are doing in marketing'}\n\n"
               f"Sources:\n{fmt_sources(docs)}", s)
    return {"profile": prof.model_dump(),
            "sources": {d["id"]: {**d, "company": c["name"]} for d in docs}}


# ---------- 2. find and confirm rivals ----------

def find_rivals(state: ScanState) -> dict:
    s, c = state["settings"], state["company"]
    if c.get("rivals"):
        return {"rivals": c["rivals"][: s["max_rivals"]]}
    emit("step", step="find_rivals", message="Finding direct competitors…")
    found = exa.discover_rivals(exa.make_client(s["mock"]), c, state["profile"]["market"], s["max_rivals"])
    return {"rivals": found}


def confirm_rivals(state: ScanState) -> dict:
    if not state["settings"]["confirm_rivals"]:
        return {}
    picked = interrupt({"kind": "confirm_rivals", "rivals": state["rivals"]})
    rivals = [{"name": r["name"].strip(), "domain": (r.get("domain") or "").strip().lower(),
               "reason": r.get("reason", "")} for r in (picked or []) if (r.get("name") or "").strip()]
    if not rivals:
        raise ValueError("No competitors selected.")
    return {"rivals": rivals}


# ---------- 3. break the job into atomic questions ----------

PLAN_SYSTEM = (
    "You plan competitor marketing research. Break the job into small, atomic questions: each one about "
    "one company, one topic, one time window, answerable from a few web pages. No compound questions. "
    "Cover different dimensions per company (launches, campaigns, channels, partnerships, positioning, "
    "pricing, events, customers), choosing the ones that matter most for the user's goal. "
    "Each question needs a natural-language search query that names the company with its domain in brackets."
)


FIELD_PLAN_SYSTEM = (
    "You plan field marketing research on competitors' events. Break the job into small, atomic questions: "
    "each about one company and one kind of event, answerable from a few web pages. Cover what they hosted "
    "(user conferences, summits, meetups, roadshows, workshops, hackathons, webinars), what they sponsored or "
    "exhibited at (trade shows, partner events), and what is announced as upcoming. Ask for names, dates, "
    "cities and audiences. Each question needs a natural-language search query that names the company with "
    "its domain in brackets, plus words like 'event', 'conference', 'meetup' or 'recap'."
)


def plan_questions(state: ScanState) -> dict:
    c, s, prof = state["company"], state["settings"], state["profile"]
    field = s.get("focus") == "field"
    emit("step", step="plan_questions", message="Breaking the research into small questions…")
    rivals = "\n".join(f"- {who(r)}: {r.get('reason', '')}" for r in state["rivals"])
    about_self = ("its own recent events, so advice doesn't repeat what it already runs" if field else
                  "its own recent marketing, so advice doesn't repeat what it already does")
    plan = ask("planner", QuestionPlan, FIELD_PLAN_SYSTEM if field else PLAN_SYSTEM,
               f"Today is {today().isoformat()}. Look at the last {s['max_age_days']} days.\n"
               f"User's company: {who(c)}. {prof['summary']} Market: {prof['market']}.\n"
               f"User's goal: {prof['focus']}\n\n"
               f"Rivals:\n{rivals}\n\n"
               f"Write exactly {s['questions_per_rival']} questions per rival, and {s['own_questions']} about "
               f"{c['name']} itself ({about_self}).",
               s)
    qs = _number(plan.questions, state, 1)
    emit("questions", questions=qs)
    return {"questions": qs, "round": 1}


def _number(questions, state: ScanState, rnd: int, cap: bool = True) -> list:
    """Give questions ids, attach domains, drop ones about unknown companies, enforce caps."""
    s, me = state["settings"], state["company"]
    known = {r["name"].lower(): r for r in state["rivals"]}
    known[me["name"].lower()] = {"name": me["name"], "domain": me.get("domain", "")}
    start = len(state.get("questions") or []) + 1
    counts: dict = {}
    out = []
    for q in questions:
        q = q.model_dump() if hasattr(q, "model_dump") else dict(q)
        co = known.get((q.get("company") or "").strip().lower())
        if not co or not (q.get("question") or "").strip():
            continue
        limit = s["own_questions"] if co["name"] == me["name"] else s["questions_per_rival"]
        if cap and rnd == 1 and counts.get(co["name"], 0) >= limit:
            continue
        counts[co["name"]] = counts.get(co["name"], 0) + 1
        out.append({"id": f"Q{start + len(out)}", "company": co["name"], "domain": co.get("domain", ""),
                    "is_self": co["name"] == me["name"], "dimension": q.get("dimension", "campaigns"),
                    "question": q["question"].strip(),
                    "search_query": (q.get("search_query") or q["question"]).strip(), "round": rnd,
                    "include_domains": q.get("include_domains") or []})
    return out


def approve_questions(state: ScanState) -> dict:
    if not state["settings"]["approve_questions"]:
        return {}
    edited = interrupt({"kind": "approve_questions", "questions": state["questions"]})
    kept = [q for q in (edited or []) if (q.get("question") or "").strip()]
    if not kept:
        raise ValueError("No questions left to research.")
    # The user may edit, remove or add questions; renumber so ids stay Q1, Q2, ... with no caps.
    renumbered = _number(kept, {**state, "questions": []}, 1, cap=False)
    emit("questions", questions=renumbered)
    return {"questions": renumbered}


def dispatch_research(state: ScanState) -> list:
    rnd = state.get("round", 1)
    return [Send("research", {"question": q, "settings": state["settings"]})
            for q in state["questions"] if q["round"] == rnd]


# ---------- 4. the researcher: a small agent per question ----------

class ResearchState(TypedDict, total=False):
    question: dict
    settings: dict
    attempt: int
    query: str
    queries: list
    docs: list
    answer: str
    kept: list
    dropped: list
    judgment: dict


ANSWER_SYSTEM = (
    "You answer one research question using only the numbered sources given. Each finding is one fact "
    "with the id of the single source it came from. Copy numbers exactly as the source writes them. "
    "Ignore sources about a different company with a similar name. If the sources don't answer the "
    "question, return no findings and say so."
)


def r_search(st: ResearchState) -> dict:
    q, s = st["question"], st["settings"]
    attempt = st.get("attempt", 0) + 1
    query = q["search_query"] if attempt == 1 else (st.get("judgment") or {}).get("next_query") or q["search_query"]
    if q.get("domain") and q["domain"].lower() not in query.lower():
        query = f"{who(q)}: {query}"  # always name the website, so look-alike companies stay out
    if attempt > 1:
        emit("question", id=q["id"], status="retrying", query=query)
    # Social-only searches (event engagement) widen to the whole web if the first try comes up short.
    domains = q.get("include_domains") if attempt == 1 else None
    found = exa.search(exa.make_client(s["mock"]), query, f"{q['id']}x", num=s["results_per_search"],
                       include_domains=domains)
    seen = {d["url"] for d in st.get("docs") or []}
    docs = list(st.get("docs") or [])
    for d in found:
        if d["url"] not in seen:
            d["id"] = f"{q['id']}-{len(docs) + 1}"
            docs.append(d)
            seen.add(d["url"])
    return {"attempt": attempt, "query": query, "queries": (st.get("queries") or []) + [query], "docs": docs}


def r_read(st: ResearchState) -> dict:
    s = st["settings"]
    unread = [d for d in st["docs"] if not d.get("text")]
    exa.read(exa.make_client(s["mock"]), unread, max_pages=s["pages_per_search"])
    return {"docs": st["docs"]}


def r_answer(st: ResearchState) -> dict:
    q, s = st["question"], st["settings"]
    res = ask("researcher", ResearchAnswer, ANSWER_SYSTEM,
              f"Today is {today().isoformat()}.\nQuestion {q['id']} about {who(q)} ({q['dimension']}): "
              f"{q['question']}\nAttempt: {st['attempt']}\n\nSources:\n{fmt_sources(st['docs'])}", s)
    findings = [{**f.model_dump(), "source_id": (clean_ids([f.source_id]) or [""])[0]} for f in res.findings]
    return {"answer": res.answer, "kept": findings}


def r_check(st: ResearchState) -> dict:
    q, s = st["question"], st["settings"]
    sources = {d["id"]: d for d in st["docs"]}
    with tracing.span("check.findings", KIND.GUARDRAIL, input={"question": q["id"], "findings": st["kept"]},
                      **{"guardrail.rules": "citation exists; about this company; not older than max_age_days; "
                                            "numbers appear in source"}) as sp:
        kept, dropped = check_findings(st["kept"], sources, {"name": q["company"], "domain": q["domain"]},
                                       s["max_age_days"], today())
        sp.set("findings.kept", len(kept))
        sp.set("findings.dropped", len(dropped))
        sp.output({"kept": kept, "dropped": dropped})
    return {"kept": kept, "dropped": (st.get("dropped") or []) + [{**d, "question_id": q["id"]} for d in dropped]}


def r_judge(st: ResearchState) -> dict:
    q, s = st["question"], st["settings"]
    with tracing.span("judge.answer", KIND.EVALUATOR, input={"question": q["question"], "kept": st["kept"]},
                      **{"eval.attempt": st["attempt"]}) as sp:
        if not st["kept"]:
            j = {"answered": False, "missing": "no findings survived the checks", "next_query": ""}
            if st["attempt"] < s["max_attempts"]:
                j = ask("researcher", Judgment,
                        "Nothing usable was found. Suggest a different web search that would find it.",
                        f"Question about {who(q)}: {q['question']}\nSearches tried: {st['queries']}", s).model_dump()
                j["answered"] = False
        else:
            lines = "\n".join(f"- {f['claim']}" for f in st["kept"])
            j = ask("researcher", Judgment,
                    "Judge strictly whether these checked findings answer the question. If not, say what's "
                    "missing and write a different search to find it. Also write the answer again using "
                    "only these findings (some earlier findings failed fact checks and must not appear).",
                    f"Question about {who(q)}: {q['question']}\nAttempt: {st['attempt']}\n"
                    f"Findings:\n{lines}\nSearches tried: {st['queries']}", s).model_dump()
        sp.set("eval.answered", bool(j["answered"]))
        sp.set("eval.label", "answered" if j["answered"] else "not_answered")
        sp.output(j)
    return {"judgment": j}


def r_route(st: ResearchState) -> str:
    j, s = st.get("judgment") or {}, st["settings"]
    if not j.get("answered") and st["attempt"] < s["max_attempts"] and j.get("next_query"):
        return "search"
    return END


def build_researcher():
    g = StateGraph(ResearchState)
    g.add_node("search", r_search)
    g.add_node("read", r_read)
    g.add_node("answer", r_answer)
    g.add_node("check", r_check)
    g.add_node("judge", r_judge)
    g.add_edge(START, "search")
    g.add_edge("search", "read")
    g.add_edge("read", "answer")
    g.add_edge("answer", "check")
    g.add_edge("check", "judge")
    g.add_conditional_edges("judge", r_route, ["search", END])
    return g.compile(name="researcher_agent")


RESEARCHER = build_researcher()


def research(payload: dict) -> dict:
    q = payload["question"]
    emit("question", id=q["id"], status="researching")
    out = {}
    # The run name contains "agent", so OpenInference records this researcher as an AGENT span,
    # with its search/read/answer/check/judge steps inside. Streaming lets its progress
    # (e.g. "retrying") reach the live view.
    config = {"run_name": f"researcher_agent {q['id']}",
              "metadata": {"question_id": q["id"], "question_company": q["company"],
                           "question_dimension": q["dimension"]}}
    for mode, chunk in RESEARCHER.stream({"question": q, "settings": payload["settings"]}, config,
                                         stream_mode=["custom", "values"]):
        if mode == "custom":
            emit(chunk.pop("event"), **chunk)
        else:
            out = chunk
    j = out.get("judgment") or {}
    status = "answered" if j.get("answered") else ("partial" if out.get("kept") else "unanswered")
    # The first answer was written before the checks; when anything was removed, use the
    # judge's rewrite, which only saw the findings that passed.
    if out.get("dropped") and out.get("kept"):
        out["answer"] = j.get("answer") or " ".join(f["claim"] for f in out["kept"])
    elif not out.get("kept"):
        out["answer"] = out.get("answer") if not out.get("dropped") else "Nothing that passed the checks."
    cited = {f["source_id"] for f in out.get("kept") or []}
    sources = {d["id"]: {**d, "company": q["company"], "question_id": q["id"]}
               for d in out["docs"] if d["id"] in cited}
    emit("question", id=q["id"], status=status, attempts=out["attempt"], findings=len(out.get("kept") or []))
    return {"answers": [{"question": q, "status": status, "answer": out.get("answer", ""),
                         "findings": out.get("kept") or [], "attempts": out["attempt"],
                         "queries": out.get("queries") or [], "missing": j.get("missing", "")}],
            "sources": sources, "dropped": out.get("dropped") or []}


# ---------- 5. look for gaps, maybe research again ----------

def after_research(state: ScanState) -> str:
    """Field focus: after the first round, dig into the events found. Otherwise review gaps."""
    if state["settings"].get("focus") == "field" and state.get("round", 1) == 1:
        return "event_followups"
    return "review_gaps"


def event_followups(state: ScanState) -> dict:
    """Field focus: pick each rival's main events and ask about attendance and social engagement."""
    s, rnd = state["settings"], state.get("round", 1)
    emit("step", step="event_followups", message="Finding rivals' events; asking about attendance and engagement…")
    rivals = {r["name"].lower(): r for r in state["rivals"]}
    found = [a for a in state["answers"] if not a["question"]["is_self"]]
    leads = ask("planner", EventLeads,
                "From checked research findings, list the distinct events each competitor hosted, sponsored or "
                "announced. Use the event's own name. Skip vague mentions with no identifiable event. Rate how "
                "much each matters for the user's field marketing.",
                f"Today is {today().isoformat()}. User's company: {who(state['company'])}.\n"
                f"Rivals: {', '.join(who(r) for r in state['rivals'])}\n\nFindings:\n{fmt_findings(found)}", s)
    picked: dict = {}
    for e in sorted(leads.events, key=lambda e: -e.importance):
        r = rivals.get(e.company.strip().lower())
        if not r or len(picked.setdefault(r["name"], [])) >= s["max_events_per_rival"]:
            continue
        if all(not similar(e.name, x.name, 0.8) for x in picked[r["name"]]):
            picked[r["name"]].append(e)
    questions = []
    for name, events in picked.items():
        dom = rivals[name.lower()].get("domain", "")
        tag = f"{name} ({dom})" if dom else name
        for e in events:
            when = f" ({e.date})" if e.date else ""
            questions.append({"company": name, "dimension": "attendance",
                              "question": f"How many people attended or registered for {e.name}{when}?",
                              "search_query": f"{tag} {e.name} attendees registrations recap"})
            questions.append({"company": name, "dimension": "engagement",
                              "question": f"How much social media engagement did {e.name}{when} get "
                                          "(reactions, comments, views, hashtag use)?",
                              "search_query": f"{tag} {e.name} recap highlights",
                              "include_domains": exa.SOCIAL_DOMAINS})
    new = _number(questions, state, rnd + 1, cap=False)
    gaps = "; ".join(f"{n}: {', '.join(e.name for e in evs)}" for n, evs in picked.items()) or "no named events found"
    if new:
        emit("questions", questions=new, follow_up=True, gaps=f"Digging into events: {gaps}")
    return {"questions": state["questions"] + new, "round": rnd + 1 if new else rnd,
            "reviews": [{"round": rnd, "gaps": f"Events picked for attendance and engagement questions: {gaps}",
                         "added": len(new)}]}


def review_gaps(state: ScanState) -> dict:
    s, rnd = state["settings"], state.get("round", 1)
    if rnd >= s["max_rounds"]:
        return {"reviews": [{"round": rnd, "gaps": "", "added": 0}]}
    emit("step", step="review_gaps", message="Checking for gaps in what was found…")
    fu = ask("planner", FollowUps,
             "You review competitor research for gaps that matter to the user's goal. Only ask follow-ups "
             "that would change the advice, usually a missing angle on a rival. Don't repeat a question that "
             "already failed after two searches unless you take a clearly different angle (e.g. press "
             "coverage instead of the company's own blog). Fewer, better questions beat using the full "
             "allowance. Name companies exactly as given and include their domain in the search query.",
             f"User's company: {who(state['company'])}. Goal: {state['profile']['focus']}\n"
             f"Rivals: {', '.join(who(r) for r in state['rivals'])}\n"
             f"Allowed follow-up questions: up to {s['max_follow_ups']}.\n\n"
             f"Research so far:\n{fmt_findings(state['answers'])}", s)
    # Don't spend a round re-asking what already failed: skip follow-ups that closely match an
    # unanswered question about the same company (the prompt asks for this; this rule makes sure).
    failed = [a["question"] for a in state["answers"] if a["status"] == "unanswered"]
    fresh = [q for q in fu.questions
             if not any(q.company.lower() == f["company"].lower() and similar(q.question, f["question"])
                        for f in failed)]
    new = _number(fresh[: s["max_follow_ups"]], state, rnd + 1)
    if new:
        emit("questions", questions=new, follow_up=True, gaps=fu.gaps)
    return {"questions": state["questions"] + new, "round": rnd + 1 if new else rnd,
            "reviews": [{"round": rnd, "gaps": fu.gaps, "added": len(new)}]}


def after_review(state: ScanState):
    rnd = state.get("round", 1)
    if any(q["round"] == rnd for q in state["questions"]) and not any(
            a["question"]["round"] == rnd for a in state["answers"]):
        return dispatch_research(state)
    return [Send("profile_rival", {"rival": r, "state": _slim(state, r["name"])}) for r in state["rivals"]]


def _slim(state: ScanState, company: str) -> dict:
    answers = [a for a in state["answers"] if a["question"]["company"] == company]
    ids = {f["source_id"] for a in answers for f in a["findings"]}
    return {"company": state["company"], "profile": state["profile"], "settings": state["settings"],
            "answers": answers, "sources": {i: state["sources"][i] for i in ids if i in state["sources"]}}


# ---------- 6. one profile per rival, rated with the rubric ----------

def _unique_ids(raw_ids, sources: dict) -> list:
    ids, urls = [], set()
    for i in clean_ids(raw_ids):  # one id per page: two ids can point at the same URL
        if i in sources and norm_url(sources[i]["url"]) not in urls:
            ids.append(i)
            urls.add(norm_url(sources[i]["url"]))
    return ids


EVENT_METRICS = ("attendees", "engagement", "coverage")


def event_date(value: str):
    """'2026-05-07', '2026-05' or '2026' -> a date (first day of the period); None if unknown."""
    m = re.match(r"^(\d{4})(?:-(\d{2}))?(?:-(\d{2}))?", (value or "").strip())
    if not m:
        return None
    try:
        return datetime.date(int(m.group(1)), int(m.group(2) or 12), int(m.group(3) or 28))
    except ValueError:
        return None


def profile_rival_events(r: dict, st: dict) -> dict:
    """Field focus: one card per rival listing its events, each metric checked against the sources."""
    s, sources = st["settings"], st["sources"]
    prof = ask("planner", RivalEventProfile,
               "You profile one competitor's events from checked research findings. One entry per distinct "
               "event. Only events the competitor itself hosted, co-hosted or sponsored count; mark anything run "
               "by a different company with a similar name as run_by_rival false. Copy attendance and engagement "
               "numbers exactly as the findings state them, and leave a field empty when no finding gives it: "
               "never estimate. Keep metric fields short and never name private individuals. Cite only the "
               "source ids shown. Rate each event with the rubric; a stated attendance figure counts as a "
               "concrete result, engagement counts alone don't. Give a date only when a finding states it, "
               "and include an undated event only if the research suggests it is recent or upcoming. "
               + PROFILE_RULES + "\n\n" + RUBRIC,
               f"User's company: {who(st['company'])}. {st['profile']['summary']}\n"
               f"Competitor: {who(r)}\n\nResearch:\n{fmt_research(st['answers'], sources, r.get('domain', ''))}", s)
    events, camps, dropped = [], [], []
    with tracing.span("check.events", KIND.GUARDRAIL, input={"rival": r["name"],
                                                             "events": [e.model_dump() for e in prof.events]},
                      **{"guardrail.rules": "run by this rival; not older than max_age_days; citations exist; "
                                            "attendance/engagement numbers appear in the cited sources (a page "
                                            "showing a figure the event doesn't cite gets added); rating held "
                                            "to the rubric (only attendance counts as a concrete result)"}) as sp:
        page = lambda x: f"{x.get('title', '')} {x.get('snippet', '')} {x.get('text', '')}"
        all_text = " ".join(page(x) for x in sources.values())
        repaired = []
        for e in prof.events:
            ids = _unique_ids(e.source_ids, sources)
            if not ids:
                dropped.append({"name": e.name, "reason": "cites no checked source"})
                continue
            if not e.run_by_rival:
                dropped.append({"name": e.name, "reason": f"run by {e.organizer or 'another company'}, not {r['name']}"})
                continue
            when = event_date(e.date)
            if when and s["max_age_days"] > 0 and (today() - when).days > s["max_age_days"]:
                dropped.append({"name": e.name, "reason": f"old: held {e.date}"})
                continue
            text = " ".join(page(sources[i]) for i in ids)
            ev, flags, verified = e.model_dump(), [], False
            for field in EVENT_METRICS:
                value = ev[field]
                if not value or not any(ch.isdigit() for ch in value):
                    continue
                if unverified_numbers(value, text):
                    # The figure isn't on the pages this event cites. If another page shows it, cite that
                    # page too, so every figure links to where it came from.
                    extra = [i for i, x in sources.items() if i not in ids and not unverified_numbers(value, page(x))]
                    if extra:
                        ids += extra[:2]
                        text += " " + " ".join(page(sources[i]) for i in extra[:2])
                        repaired.append({"event": e.name, "field": field, "added": extra[:2]})
                if unverified_numbers(value, text) and unverified_numbers(value, all_text):
                    flags.append(f"{field} not found in sources: {value}")
                    ev[field] = ""  # an unbacked number is worse than none; the flag keeps the claim visible
                elif field == "attendees":
                    verified = True  # a sourced attendance figure is a concrete result; engagement alone isn't
            strength, note = rubric_cap(e.strength, ids, sources, r.get("domain", ""), verified)
            keep = {r["name"], ev["name"], ev["organizer"]}
            for field in ("attendees", "engagement", "coverage", "partners", "organizer"):
                ev[field] = scrub_people(ev[field], keep)  # no private individuals in the report
            ev.update({"source_ids": ids, "strength": strength, "rubric_note": note, "flags": flags})
            events.append(ev)
            facts = ", ".join(x for x in (ev["date"], ev["location"] or ev["format"],
                                          ev["attendees"], ev["engagement"]) if x and x != "unknown")
            camps.append({"name": ev["name"], "what": f"{ev['kind']}" + (f": {facts}" if facts else ""),
                          "channel": ev["kind"], "strength": strength, "why": ev["why"], "source_ids": ids,
                          "rubric_note": note})
        sp.set("events.kept", len(events))
        sp.set("events.dropped", len(dropped))
        sp.set("events.metric_flags", sum(len(e["flags"]) for e in events))
        sp.set("events.citations_added", len(repaired))
        sp.output(events)
    emit("rival", name=r["name"], status="done")
    return {"profiles": [{"rival": r, "summary": prof.summary, "positioning": prof.positioning,
                          "threat": prof.threat, "threat_why": prof.threat_why,
                          "campaigns": camps, "events": events, "dropped_events": dropped}]}


def profile_rival(payload: dict) -> dict:
    r, st = payload["rival"], payload["state"]
    s, sources = st["settings"], st["sources"]
    emit("rival", name=r["name"], status="profiling")
    if s.get("focus") == "field":
        return profile_rival_events(r, st)
    prof = ask("planner", RivalProfile,
               "You profile one competitor's recent marketing from checked research findings. Group findings "
               "into campaigns or moves. Cite only the source ids shown. Rate each campaign with the rubric; "
               "rate lower when unsure. " + PROFILE_RULES + "\n\n" + RUBRIC,
               f"User's company: {who(st['company'])}. {st['profile']['summary']}\n"
               f"Competitor: {who(r)}\n\nResearch:\n{fmt_research(st['answers'], sources, r.get('domain', ''))}", s)
    camps = []
    with tracing.span("check.rubric", KIND.GUARDRAIL, input={"rival": r["name"],
                                                             "campaigns": [c.model_dump() for c in prof.campaigns]},
                      **{"guardrail.rules": "citations exist; rating held to the rubric by source count"}) as sp:
        for c in prof.campaigns:
            ids = _unique_ids(c.source_ids, sources)
            if not ids:
                continue
            claims = " ".join(f["claim"] for a in st["answers"] for f in a["findings"]
                              if f["source_id"] in ids and not any("numbers not found" in x for x in f["flags"]))
            text = " ".join(sources[i].get("text") or sources[i].get("snippet") or "" for i in ids)
            has_verified_number = bool(claims) and any(ch.isdigit() for ch in claims) and not unverified_numbers(
                claims, text)
            strength, note = rubric_cap(c.strength, ids, sources, r.get("domain", ""), has_verified_number)
            camps.append({**c.model_dump(), "source_ids": ids, "strength": strength, "rubric_note": note})
        sp.set("campaigns.kept", len(camps))
        sp.set("campaigns.lowered", sum(1 for c in camps if c["rubric_note"]))
        sp.output(camps)
    emit("rival", name=r["name"], status="done")
    return {"profiles": [{"rival": r, **prof.model_dump(), "campaigns": camps}]}


# ---------- 7. the action plan ----------

ACTIONS_SYSTEM = (
    "You are a marketing strategist. Turn checked competitor research into a short, ranked action plan for "
    "the user's company. Every action must respond to specific rival moves and cite their source ids. "
    "Don't recommend what the company already does or already offers (see its products and its own recent "
    "marketing). Be specific: a real channel, a real first step this week. Rank by impact over effort."
)


FIELD_ACTIONS_SYSTEM = (
    "You are a field marketing strategist. Turn checked research on competitors' events into a short, ranked "
    "plan for the user's company: which rival events to counter-program, attend, sponsor or speak at; which "
    "cities, formats and dates to target; how to win attention around rival events (side events, socials, "
    "partner co-marketing); and what to borrow from rival events that drew strong attendance or engagement. "
    "Every action must respond to specific rival events and cite their source ids. Use real dates and places "
    "from the research. Don't recommend events the company already runs (see its own recent events). Rank by "
    "impact over effort."
)


def plan_actions(state: ScanState) -> dict:
    s, c = state["settings"], state["company"]
    field = s.get("focus") == "field"
    emit("step", step="plan_actions", message="Writing the action plan…")
    rival_ids = {i for p in state["profiles"] for camp in p["campaigns"] for i in camp["source_ids"]}
    own = [a for a in state["answers"] if a["question"]["is_self"]]
    rivals_txt = "\n\n".join(
        f"{who(p['rival'])} (threat {p['threat']}: {p['threat_why']})\n{p['summary']}\n" + "\n".join(
            f"- {x['name']} [{', '.join(x['source_ids'])}] {x['strength']}: {x['what']} ({x['channel']})"
            for x in p["campaigns"]) for p in state["profiles"])
    prompt = (f"User's company: {who(c)}. {state['profile']['summary']}\n"
              f"Already offers: {', '.join(state['profile']['products'])}\n"
              f"Goal: {state['profile']['focus']}\n\n"
              f"Its own recent {'events' if field else 'marketing'}:\n{fmt_findings(own)}\n\n"
              f"Rival {'events' if field else 'moves'} (cite these ids):\n{rivals_txt}\n\nWrite 3 to 6 actions.")
    evidence = " ".join(f"{x.get('title', '')} {x.get('snippet', '')} {x.get('text', '')}"
                        for x in state["sources"].values())
    result, dropped = None, []
    for attempt in (1, 2):
        plan = ask("planner", ActionPlan, FIELD_ACTIONS_SYSTEM if field else ACTIONS_SYSTEM, prompt, s)
        with tracing.span("check.actions", KIND.GUARDRAIL, input={"attempt": attempt,
                                                                  "actions": [a.model_dump() for a in plan.actions]},
                          **{"guardrail.rules": "each action cites at least one checked rival source; "
                                                "numbers in its claims appear in the sources"}) as sp:
            kept, dropped = [], []
            for a in plan.actions:
                ids = [i for i in clean_ids(a.source_ids) if i in rival_ids]
                if ids:
                    # Numbers in the claims (what/why) must appear somewhere in the scan's sources.
                    claim = re.sub(r"\b(?:Q\d+|ME)-\d+\b", " ", f"{a.what} {a.why}")
                    missing = unverified_numbers(claim, evidence)
                    flags = [f"numbers not found in sources: {', '.join(missing)}"] if missing else []
                    kept.append({**a.model_dump(), "source_ids": ids, "flags": flags})
                else:
                    dropped.append({"title": a.title, "reason": "cites no checked rival source"})
            sp.set("actions.kept", len(kept))
            sp.set("actions.dropped", len(dropped))
            sp.output({"kept": kept, "dropped": dropped})
        result = {"headline": plan.headline, "actions": kept, "dropped": dropped}
        if kept or not rival_ids:
            break
        prompt += ("\n\nYour last answer cited no valid ids, so every action was dropped. Cite only ids "
                   "shown in square brackets above.")
    return {"plan": result}


# ---------- 7b. field focus: key findings instead of an action plan ----------

SUMMARY_SYSTEM = (
    "You are a field marketing research analyst. Summarize checked research on competitors' events as "
    "findings, not advice: who runs the most events, which formats, cities and months they favor, which "
    "events drew the most attendance or social engagement, and what is coming up. Every finding must cite "
    "the source ids of the events behind it. Use only numbers the events state. Never recommend actions."
)


def after_profiles(state: ScanState) -> str:
    return "summarize_events" if state["settings"].get("focus") == "field" else "plan_actions"


def summarize_events(state: ScanState) -> dict:
    s = state["settings"]
    emit("step", step="summarize_events", message="Summarizing what the research found…")
    lines = []
    for p in state["profiles"]:
        for e in p.get("events") or []:
            facts = " · ".join(x for x in (p["rival"]["name"], e["kind"], e["date"], e["location"] or e["format"],
                                           e["attendees"], e["engagement"]) if x and x != "unknown")
            lines.append(f"- {e['name']} [{', '.join(e['source_ids'])}] {facts}")
    prompt = (f"User's company: {who(state['company'])}. {state['profile']['summary']}\n"
              f"Rivals: {', '.join(who(r) for r in state['rivals'])}\n\n"
              f"Rival events (cite these ids):\n" + ("\n".join(lines) or "(none found)") +
              "\n\nWrite a headline and 3 to 5 findings.")
    event_ids = {i for p in state["profiles"] for e in p.get("events") or [] for i in e["source_ids"]}
    evidence = " ".join(f"{x.get('title', '')} {x.get('snippet', '')} {x.get('text', '')}"
                        for x in state["sources"].values())
    out = ask("planner", FieldSummary, SUMMARY_SYSTEM, prompt, s)
    with tracing.span("check.summary", KIND.GUARDRAIL, input={"findings": [f.model_dump() for f in out.findings]},
                      **{"guardrail.rules": "each finding cites at least one checked rival event; "
                                            "numbers in it appear in the sources"}) as sp:
        kept, dropped = [], []
        for f in out.findings:
            ids = [i for i in clean_ids(f.source_ids) if i in event_ids]
            if not ids:
                dropped.append({"text": f.text, "reason": "cites no checked rival event"})
                continue
            missing = unverified_numbers(f.text, evidence)
            flags = [f"numbers not found in sources: {', '.join(missing)}"] if missing else []
            kept.append({"text": f.text, "source_ids": ids, "flags": flags})
        sp.set("findings.kept", len(kept))
        sp.set("findings.dropped", len(dropped))
        sp.output({"kept": kept, "dropped": dropped})
    summary = {"headline": out.headline, "findings": kept, "dropped": dropped}
    return {"summary": summary, "plan": {"headline": out.headline, "actions": [], "dropped": []}}


# ---------- 8. the report ----------

def write_report(state: ScanState) -> dict:
    from .report import save_report
    emit("step", step="write_report", message="Saving the report…")
    return {"report": save_report(state)}


def build_graph(checkpointer=None):
    g = StateGraph(ScanState)
    g.add_node("understand", understand)
    g.add_node("find_rivals", find_rivals)
    g.add_node("confirm_rivals", confirm_rivals)
    g.add_node("plan_questions", plan_questions)
    g.add_node("approve_questions", approve_questions)
    g.add_node("research", research)
    g.add_node("review_gaps", review_gaps)
    g.add_node("event_followups", event_followups)
    g.add_node("profile_rival", profile_rival)
    g.add_node("plan_actions", plan_actions)
    g.add_node("summarize_events", summarize_events)
    g.add_node("write_report", write_report)
    g.add_edge(START, "understand")
    g.add_edge("understand", "find_rivals")
    g.add_edge("find_rivals", "confirm_rivals")
    g.add_edge("confirm_rivals", "plan_questions")
    g.add_edge("plan_questions", "approve_questions")
    g.add_conditional_edges("approve_questions", dispatch_research, ["research"])
    g.add_conditional_edges("research", after_research, ["event_followups", "review_gaps"])
    g.add_conditional_edges("event_followups", after_review, ["research", "profile_rival"])
    g.add_conditional_edges("review_gaps", after_review, ["research", "profile_rival"])
    g.add_conditional_edges("profile_rival", after_profiles, ["plan_actions", "summarize_events"])
    g.add_edge("summarize_events", "write_report")
    g.add_edge("plan_actions", "write_report")
    g.add_edge("write_report", END)
    return g.compile(checkpointer=checkpointer, name="competitor_scan")


def initial_state(run_id: str, company: dict, settings: Optional[dict] = None) -> ScanState:
    return {"run_id": run_id, "company": company, "settings": {**DEFAULTS, **(settings or {})},
            "answers": [], "sources": {}, "dropped": [], "reviews": [], "profiles": []}
