"""FastAPI backend for the agentic Competitor Scan.

Keys (EXA_API_KEY, OPENCODE_API_KEY) stay server-side in .env; the browser never sees them.

  POST /api/scans                 start a scan; streams events until it pauses or finishes
  POST /api/scans/{run_id}/resume answer a pause (rivals or questions); streams again
  GET  /api/reports               saved reports
  GET  /api/reports/{run_id}      one report (dashboard data + markdown)

Run:  uvicorn backend.main:app --port 8000
"""
from __future__ import annotations

import json
import os
import queue
import re
import threading
import sys
import urllib.request
from pathlib import Path
from typing import Any, List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from agent import report as report_mod  # noqa: E402
from agent import tracing  # noqa: E402
from agent.llm import has_key, model_name, provider  # noqa: E402
from agent.run import new_run_id, run_segment  # noqa: E402

PHOENIX_URL = os.getenv("PHOENIX_URL", "http://localhost:6006")
if os.getenv("PHOENIX_TRACING", "1") != "0":
    tracing.setup_phoenix()

app = FastAPI(title="Competitor Scan (agentic)")
_RUN_ID = re.compile(r"[0-9a-f]{8}")


class Rival(BaseModel):
    name: str
    domain: str = ""
    reason: str = ""


class Company(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    domain: str = ""
    description: str = ""
    goal: str = ""
    rivals: List[Rival] = []


class Settings(BaseModel):
    mock: bool = False
    max_rivals: int = Field(4, ge=1, le=6)
    questions_per_rival: int = Field(4, ge=1, le=8)
    own_questions: int = Field(2, ge=0, le=4)
    max_attempts: int = Field(2, ge=1, le=3)
    max_rounds: int = Field(2, ge=1, le=3)
    max_age_days: int = Field(365, ge=0, le=3650)
    approve_questions: bool = False
    planner_model: str = ""
    researcher_model: str = ""


class StartBody(BaseModel):
    company: Company
    settings: Settings = Settings()


class ResumeBody(BaseModel):
    value: Any


def _sse(events):
    """Stream events as SSE. The scan runs start to finish on its own thread: tracing keeps
    'current span' state per thread, and the server would otherwise hop threads between events."""
    q: queue.Queue = queue.Queue()

    def work():
        try:
            for e in events:
                q.put(e)
        except Exception as exc:  # noqa: BLE001
            q.put({"event": "error", "message": f"Scan failed: {str(exc)[:300]}"})
        finally:
            q.put(None)

    threading.Thread(target=work, daemon=True).start()

    def gen():
        while (e := q.get()) is not None:
            yield f"data: {json.dumps(e, default=str)}\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _phoenix_up() -> bool:
    try:
        urllib.request.urlopen(PHOENIX_URL, timeout=0.5)
        return True
    except Exception:  # noqa: BLE001
        return False


@app.get("/api/health")
def health():
    return {"ok": True, "has_exa_key": bool(os.getenv("EXA_API_KEY")),
            "has_ai_key": has_key(), "ai_provider": provider(),
            "models": {"planner": model_name("planner", {}), "researcher": model_name("researcher", {})},
            "phoenix": {"url": PHOENIX_URL, "up": _phoenix_up()}}


@app.post("/api/scans")
def start_scan(body: StartBody):
    s = body.settings.model_dump()
    models = {k: v for k, v in (("planner", s.pop("planner_model")), ("researcher", s.pop("researcher_model"))) if v}
    company = body.company.model_dump()
    if not company["rivals"]:
        company.pop("rivals")
    run_id = new_run_id()
    return _sse(run_segment(run_id, start={"company": company, "settings": {**s, "models": models}}))


@app.post("/api/scans/{run_id}/resume")
def resume_scan(run_id: str, body: ResumeBody):
    if not _RUN_ID.fullmatch(run_id):
        raise HTTPException(404, "scan not found")
    if not isinstance(body.value, list):
        raise HTTPException(422, "value must be a list of rivals or questions")
    return _sse(run_segment(run_id, resume=body.value))


def _reports_dir() -> Path:
    return report_mod.REPORTS


@app.get("/api/reports")
def list_reports():
    items = []
    for path in sorted(_reports_dir().glob("*_scan_*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            d = json.loads(path.read_text())
        except ValueError:
            continue
        if d.get("version") != report_mod.VERSION:
            continue
        items.append({"run_id": d["run_id"], "created": d["created"], "company": d["company"]["name"],
                      "rivals": [r["name"] for r in d["rivals"]], "mock": d["settings"].get("mock"),
                      "actions": d["stats"]["actions"]})
    return {"reports": items}


@app.get("/api/reports/{run_id}")
def get_report(run_id: str):
    if not _RUN_ID.fullmatch(run_id):
        raise HTTPException(404, "report not found")
    matches = sorted(_reports_dir().glob(f"*_scan_{run_id}.json"))
    if not matches:
        raise HTTPException(404, "report not found")
    md = matches[0].with_suffix(".md")
    return {"data": json.loads(matches[0].read_text()), "markdown": md.read_text() if md.exists() else "",
            "filename": md.name, "phoenix_url": PHOENIX_URL}
