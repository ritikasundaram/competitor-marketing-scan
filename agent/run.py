"""Run the graph in segments: start -> (pause for approval) -> resume -> ... -> done.

Each segment is one root AGENT span in Phoenix; all segments share the run's
session id, so Phoenix's Sessions view shows the whole scan.
"""
from __future__ import annotations

import uuid
from typing import Iterator, Optional

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from . import tracing
from .exa import ExaStop
from .graph import build_graph, initial_state
from .llm import LLMStop
from .tracing import KIND

CHECKPOINTER = InMemorySaver()  # paused scans live in memory; a server restart forgets them
GRAPH = build_graph(CHECKPOINTER)


def new_run_id() -> str:
    return uuid.uuid4().hex[:8]


def _config(run_id: str) -> dict:
    return {"configurable": {"thread_id": run_id}, "max_concurrency": 4, "recursion_limit": 200}


def run_segment(run_id: str, start: Optional[dict] = None, resume=None) -> Iterator[dict]:
    """Yield progress events, then one 'interrupt', 'done' or 'error' event.

    start: {"company": {...}, "settings": {...}} for a new run; resume: the user's answer.
    """
    config = _config(run_id)
    if start is not None:
        inp = initial_state(run_id, start["company"], start.get("settings"))
        company, mock = start["company"]["name"], bool((start.get("settings") or {}).get("mock"))
        segment = "start"
    else:
        snap = GRAPH.get_state(config)
        if not snap.values:
            yield {"event": "error", "message": "This scan is no longer in memory (the server restarted). "
                                                "Start a new one."}
            return
        inp = Command(resume=resume)
        company, mock = snap.values["company"]["name"], snap.values["settings"]["mock"]
        segment = "resume"
    try:
        with tracing.session(run_id, company, mock), tracing.span(
                f"competitor_scan ({segment})", KIND.AGENT, input=start or {"resume": resume},
                **{"scan.run_id": run_id, "scan.segment": segment}) as root:
            for mode, chunk in GRAPH.stream(inp, config, stream_mode=["custom", "updates"]):
                if mode == "custom":
                    yield chunk
                elif mode == "updates":
                    for node in chunk:
                        if node != "__interrupt__":
                            yield {"event": "node", "node": node}
            snap = GRAPH.get_state(config)
            pending = [i.value for t in snap.tasks for i in (t.interrupts or [])]
            root.output({"paused_for": pending[0]["kind"]} if pending else {"done": True})
            trace_id = root.trace_id()
    except (ExaStop, LLMStop) as e:
        yield {"event": "error", "message": str(e)}
        return
    except ValueError as e:
        yield {"event": "error", "message": str(e)}
        return
    finally:
        tracing.flush()
    if pending:
        yield {"event": "interrupt", "run_id": run_id, "trace_id": trace_id, **pending[0]}
    else:
        yield {"event": "done", "run_id": run_id, "trace_id": trace_id, "report": snap.values.get("report")}
