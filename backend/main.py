"""FastAPI backend for the Competitor Marketing Scan web app.

Serves the React frontend's API. EXA_API_KEY stays server-side (loaded from
project-root .env) and is never sent to the browser.

Run:  uvicorn backend.main:app --port 8000
"""
from __future__ import annotations

import json
import os
import queue
import re
import sys
import threading
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from runner import discover_competitors, exa_stop_reason, scan_company  # noqa: E402

app = FastAPI(title="Competitor Marketing Scan")


def _exa_client():
    key = os.getenv("EXA_API_KEY")
    if not key:
        raise HTTPException(400, "EXA_API_KEY not set on server (add it to .env) or use mock mode.")
    from exa_py import Exa
    return Exa(api_key=key)


class Company(BaseModel):
    name: str
    domain: str = ""
    description: str = ""


class DiscoverBody(Company):
    mock: bool = False
    max_competitors: int = Field(4, ge=1, le=6)


class ScanBody(BaseModel):
    company: Company
    competitors: list[dict] = []
    mock: bool = False
    grade: bool = True
    tracer: str = "none"
    max_competitors: int = Field(4, ge=1, le=6)
    max_results_per_query: int = Field(10, ge=1, le=25)
    max_queries_per_competitor: int = Field(3, ge=1, le=6)
    fetch_proof: bool = True
    max_proof_urls: int = Field(5, ge=0, le=10)
    max_age_days: int = Field(365, ge=0, le=3650)
    scan_self: bool = True
    synthesis: str = "auto"


@app.get("/api/health")
def health():
    return {"ok": True, "has_key": bool(os.getenv("EXA_API_KEY"))}


@app.post("/api/discover")
def discover(body: DiscoverBody):
    client = None if body.mock else _exa_client()
    try:
        found = discover_competitors(client, body.name, body.domain, body.description,
                                     max_n=body.max_competitors, dry=body.mock)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, exa_stop_reason(e) or f"Competitor discovery failed: {str(e)[:200]}")
    return {"competitors": found}


@app.post("/api/scan")
def scan(body: ScanBody):
    client = None if body.mock else _exa_client()
    cfg = {"my_company": body.company.model_dump(), "competitors": body.competitors,
           "max_results_per_query": body.max_results_per_query,
           "max_queries_per_competitor": body.max_queries_per_competitor,
           "max_competitors": body.max_competitors, "grade": body.grade,
           "fetch_proof": body.fetch_proof, "max_proof_urls": body.max_proof_urls,
           "max_age_days": body.max_age_days, "scan_self": body.scan_self,
           "synthesis": body.synthesis}

    def events():
        events_q: queue.Queue = queue.Queue()
        out_holder: dict = {}

        def work():
            try:
                out_holder["out"] = scan_company(
                    cfg, tracer_backend=body.tracer, client=client, dry=body.mock,
                    on_step=lambda e, p: events_q.put({"event": e, **p}),
                    confirm_fn=None,
                )
            except Exception as exc:  # noqa: BLE001
                events_q.put({"event": "error", "message": str(exc)[:300]})
            finally:
                events_q.put(None)

        threading.Thread(target=work, daemon=True).start()
        while True:
            item = events_q.get()
            if item is None:
                break
            yield f"data: {json.dumps(item, default=str)}\n\n"

        out = out_holder.get("out")
        if out:
            yield f"data: {json.dumps({'event': 'done', 'run_id': out['run_id'], 'flat': out['flat'], 'old': out['old'], 'report': out['report'], 'tracer': out['tracer'], 'otel_trace_id': out['otel_trace_id']}, default=str)}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/runs")
def runs():
    items = []
    for path in sorted((ROOT / "reports").glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True):
        match = re.match(r"(\d{4}-\d{2}-\d{2})_competitor-scan_([0-9a-f]+)\.md$", path.name)
        if not match:
            continue
        head = path.read_text()[:400]
        company = re.search(r"My company: \*\*(.+?)\*\*", head)
        items.append({"run_id": match.group(2), "date": match.group(1), "filename": path.name,
                      "company": company.group(1) if company else "",
                      "has_data": path.with_suffix(".json").exists()})
    return {"runs": items}


@app.get("/api/reports/{run_id}")
def report(run_id: str):
    if not re.fullmatch(r"[0-9a-f]+", run_id):
        raise HTTPException(404, "report not found")
    matches = sorted(ROOT.glob(f"reports/*_{run_id}.md"))
    if not matches:
        raise HTTPException(404, "report not found")
    data_path = matches[0].with_suffix(".json")
    data = json.loads(data_path.read_text()) if data_path.exists() else None
    return {"run_id": run_id, "filename": matches[0].name,
            "markdown": matches[0].read_text(), "data": data}


@app.get("/api/traces/{run_id}")
def trace(run_id: str):
    if not re.fullmatch(r"[0-9a-f]+", run_id):
        raise HTTPException(404, "trace not found")
    path = ROOT / "traces" / f"{run_id}.jsonl"
    if not path.exists():
        raise HTTPException(404, "trace not found")
    return {"run_id": run_id,
            "spans": [json.loads(line) for line in path.read_text().splitlines() if line.strip()]}
