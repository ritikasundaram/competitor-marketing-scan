"""Shared test helpers: a temp reports folder, quiet warnings, and a full mock run."""
from __future__ import annotations

import os
import sys
import tempfile
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
os.environ["MOCK_DELAY"] = "0"
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent import report  # noqa: E402
from agent.run import new_run_id, run_segment  # noqa: E402

FIGMA = {"name": "Figma", "domain": "figma.com", "description": "Collaborative design tool"}
SMALL = {"mock": True, "max_rivals": 2, "questions_per_rival": 4, "own_questions": 2}


class TempReports:
    """Point the report writer at a throwaway folder."""

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name)
        self._old = report.REPORTS
        report.REPORTS = self.path
        return self

    def __exit__(self, *exc):
        report.REPORTS = self._old
        self._tmp.cleanup()


def run_all(company=None, settings=None, answers=None):
    """Run a scan to the end, answering each pause. Returns (events, run_id).

    answers: optional {kind: fn(payload) -> resume value}; default keeps everything.
    """
    answers = answers or {}
    run_id = new_run_id()
    events = list(run_segment(run_id, start={"company": company or dict(FIGMA),
                                             "settings": {**SMALL, **(settings or {})}}))
    for _ in range(5):
        last = events[-1]
        if last["event"] != "interrupt":
            break
        kind = last["kind"]
        default = last["rivals"] if kind == "confirm_rivals" else last["questions"]
        value = answers[kind](last) if kind in answers else default
        events += list(run_segment(run_id, resume=value))
    return events, run_id
