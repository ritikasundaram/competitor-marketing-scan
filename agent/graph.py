"""The agent: a LangGraph graph that plans, researches with Exa, checks and recommends.

    understand -> find_rivals -> confirm_rivals (pause) -> plan_questions
      -> approve_questions (optional pause) -> research xN (parallel researchers)
      -> review_gaps -> (one more research round if gaps) -> profile_rival xN (parallel)
      -> plan_actions -> write_report

Each researcher is its own small graph that loops:
    search (Exa) -> read (Exa) -> answer (AI) -> check (rules) -> judge (AI) -> retry or stop
"""
from __future__ import annotations

import datetime
import operator
from typing import Annotated, Optional, TypedDict

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send, interrupt

from . import exa, mock, tracing
from .checks import RUBRIC, check_findings, clean_ids, on_own_site, rubric_cap, unverified_numbers
from .llm import LLMStop, get_model, llm_stop_reason
from .schemas import (ActionPlan, CompanyProfile, FollowUps, Judgment, QuestionPlan, ResearchAnswer,
                      RivalProfile)
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
    """One structured AI call. Auto-traced as an LLM span by OpenInference."""
    # Forced tool call, parsed here (not by a LangChain parser) so Phoenix shows one clean LLM span.
    model = get_model(role, settings, mock_responder=mock.respond).bind_tools([schema], tool_choice=schema.__name__)
    messages = [SystemMessage(content=system), HumanMessage(content=user)]
    last_error = None
    for _ in range(2):
        try:
            msg = model.invoke(messages)
        except Exception as e:  # noqa: BLE001
            reason = llm_stop_reason(e)
            if reason:
                raise LLMStop(reason) from e
            raise
        calls = [c for c in (getattr(msg, "tool_calls", None) or []) if c["name"] == schema.__name__]
        try:
            return schema.model_validate(calls[0]["args"])
        except (IndexError, ValueError) as e:
            last_error = e
            messages = messages + [HumanMessage(content=f"Answer by calling {schema.__name__} with valid fields.")]
    raise RuntimeError(f"The model didn't return a valid {schema.__name__}: {last_error}")


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


def plan_questions(state: ScanState) -> dict:
    c, s, prof = state["company"], state["settings"], state["profile"]
    emit("step", step="plan_questions", message="Breaking the research into small questions…")
    rivals = "\n".join(f"- {who(r)}: {r.get('reason', '')}" for r in state["rivals"])
    plan = ask("planner", QuestionPlan, PLAN_SYSTEM,
               f"Today is {today().isoformat()}. Look at the last {s['max_age_days']} days.\n"
               f"User's company: {who(c)}. {prof['summary']} Market: {prof['market']}.\n"
               f"User's goal: {prof['focus']}\n\n"
               f"Rivals:\n{rivals}\n\n"
               f"Write exactly {s['questions_per_rival']} questions per rival, and {s['own_questions']} about "
               f"{c['name']} itself (its own recent marketing, so advice doesn't repeat what it already does).",
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
                    "search_query": (q.get("search_query") or q["question"]).strip(), "round": rnd})
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
    found = exa.search(exa.make_client(s["mock"]), query, f"{q['id']}x", num=s["results_per_search"])
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
                    "missing and write a different search to find it.",
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
    cited = {f["source_id"] for f in out.get("kept") or []}
    sources = {d["id"]: {**d, "company": q["company"], "question_id": q["id"]}
               for d in out["docs"] if d["id"] in cited}
    emit("question", id=q["id"], status=status, attempts=out["attempt"], findings=len(out.get("kept") or []))
    return {"answers": [{"question": q, "status": status, "answer": out.get("answer", ""),
                         "findings": out.get("kept") or [], "attempts": out["attempt"],
                         "queries": out.get("queries") or [], "missing": j.get("missing", "")}],
            "sources": sources, "dropped": out.get("dropped") or []}


# ---------- 5. look for gaps, maybe research again ----------

def review_gaps(state: ScanState) -> dict:
    s, rnd = state["settings"], state.get("round", 1)
    if rnd >= s["max_rounds"]:
        return {"reviews": [{"round": rnd, "gaps": "", "added": 0}]}
    emit("step", step="review_gaps", message="Checking for gaps in what was found…")
    fu = ask("planner", FollowUps,
             "You review competitor research for gaps that matter to the user's goal. Only ask follow-ups "
             "that would change the advice: unanswered questions worth rephrasing, or a missing angle on a "
             "rival. Name companies exactly as given and include their domain in the search query.",
             f"User's company: {who(state['company'])}. Goal: {state['profile']['focus']}\n"
             f"Rivals: {', '.join(who(r) for r in state['rivals'])}\n"
             f"Allowed follow-up questions: up to {s['max_follow_ups']}.\n\n"
             f"Research so far:\n{fmt_findings(state['answers'])}", s)
    new = _number(fu.questions[: s["max_follow_ups"]], state, rnd + 1)
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

def profile_rival(payload: dict) -> dict:
    r, st = payload["rival"], payload["state"]
    s, sources = st["settings"], st["sources"]
    emit("rival", name=r["name"], status="profiling")
    prof = ask("planner", RivalProfile,
               "You profile one competitor's recent marketing from checked research findings. Group findings "
               "into campaigns or moves. Cite only the source ids shown. Rate each campaign with the rubric; "
               "rate lower when unsure.\n\n" + RUBRIC,
               f"User's company: {who(st['company'])}. {st['profile']['summary']}\n"
               f"Competitor: {who(r)}\n\nFindings:\n{fmt_findings(st['answers'])}", s)
    camps = []
    with tracing.span("check.rubric", KIND.GUARDRAIL, input={"rival": r["name"],
                                                             "campaigns": [c.model_dump() for c in prof.campaigns]},
                      **{"guardrail.rules": "citations exist; rating held to the rubric by source count"}) as sp:
        for c in prof.campaigns:
            ids = [i for i in clean_ids(c.source_ids) if i in sources]
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


def plan_actions(state: ScanState) -> dict:
    s, c = state["settings"], state["company"]
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
              f"Its own recent marketing:\n{fmt_findings(own)}\n\n"
              f"Rival moves (cite these ids):\n{rivals_txt}\n\nWrite 3 to 6 actions.")
    result, dropped = None, []
    for attempt in (1, 2):
        plan = ask("planner", ActionPlan, ACTIONS_SYSTEM, prompt, s)
        with tracing.span("check.actions", KIND.GUARDRAIL, input={"attempt": attempt,
                                                                  "actions": [a.model_dump() for a in plan.actions]},
                          **{"guardrail.rules": "each action cites at least one checked rival source"}) as sp:
            kept, dropped = [], []
            for a in plan.actions:
                ids = [i for i in clean_ids(a.source_ids) if i in rival_ids]
                if ids:
                    kept.append({**a.model_dump(), "source_ids": ids})
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
    g.add_node("profile_rival", profile_rival)
    g.add_node("plan_actions", plan_actions)
    g.add_node("write_report", write_report)
    g.add_edge(START, "understand")
    g.add_edge("understand", "find_rivals")
    g.add_edge("find_rivals", "confirm_rivals")
    g.add_edge("confirm_rivals", "plan_questions")
    g.add_edge("plan_questions", "approve_questions")
    g.add_conditional_edges("approve_questions", dispatch_research, ["research"])
    g.add_edge("research", "review_gaps")
    g.add_conditional_edges("review_gaps", after_review, ["research", "profile_rival"])
    g.add_edge("profile_rival", "plan_actions")
    g.add_edge("plan_actions", "write_report")
    g.add_edge("write_report", END)
    return g.compile(checkpointer=checkpointer, name="competitor_scan")


def initial_state(run_id: str, company: dict, settings: Optional[dict] = None) -> ScanState:
    return {"run_id": run_id, "company": company, "settings": {**DEFAULTS, **(settings or {})},
            "answers": [], "sources": {}, "dropped": [], "reviews": [], "profiles": []}
