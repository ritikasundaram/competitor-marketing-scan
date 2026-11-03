"""Shared test helpers: a temp output folder and a fake Exa client (no network, no cost)."""
from __future__ import annotations

import datetime
import json
import re
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace as NS

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import runner  # noqa: E402

TODAY = datetime.date.today()
RECENT = (TODAY - datetime.timedelta(days=30)).isoformat()


class TempOutput:
    """Point runner (and optionally the backend) at a throwaway reports/traces folder."""

    def __init__(self, backend_module=None):
        self.backend = backend_module

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name)
        (self.path / "reports").mkdir()
        (self.path / "traces").mkdir()
        self._old = (runner.REPORTS, runner.TRACES)
        runner.REPORTS, runner.TRACES = self.path / "reports", self.path / "traces"
        if self.backend is not None:
            self._old_root = self.backend.ROOT
            self.backend.ROOT = self.path
        return self

    def __exit__(self, *exc):
        runner.REPORTS, runner.TRACES = self._old
        if self.backend is not None:
            self.backend.ROOT = self._old_root
        self._tmp.cleanup()


# One competitor (Sketch) with four kinds of result, plus the user's own company (Figma).
SKETCH = [
    # 1. Real, recent, number backed by the highlight -> kept, no flags
    {"title": "Sketch AI launch", "url": "https://www.sketch.com/blog/ai-launch", "date": RECENT,
     "highlights": ["Sketch AI launch drew 300 signups in a week."],
     "graded": {"channel": "blog", "message": "AI features", "signal": "300 signups in a week",
                "strength": "strong", "confidence": "high", "why": "Clear uptake", "about_competitor": True}},
    # 2. Same-name different company -> excluded as off topic
    {"title": "Little Moons pop-up", "url": "https://www.linkedin.com/posts/sketch-events_pop-up", "date": RECENT,
     "highlights": ["Sketch Events built a pop-up for Little Moons."],
     "graded": {"channel": "events", "message": "Pop-up", "signal": "Busy launch", "strength": "Strong",
                "confidence": "High", "why": "Footfall", "about_competitor": False}},
    # 3. Old (date in the URL) -> excluded as stale
    {"title": "Sketch 2023 partnership", "url": "https://www.businesswire.com/news/home/20230629787067/en/sketch",
     "date": "", "highlights": ["Sketch announces partnership."],
     "graded": {"channel": "press", "message": "Partnership", "signal": "Press pickup", "strength": "Medium",
                "confidence": "Medium", "why": "Coverage", "about_competitor": True}},
    # 4. Number the source never mentions -> kept but flagged, confidence Low
    {"title": "Sketch webinar series", "url": "https://www.sketch.com/webinars", "date": RECENT,
     "highlights": ["Join our webinar series on design systems."],
     "graded": {"channel": "webinar", "message": "Design systems", "signal": "7.7% CTR on invites",
                "strength": "Medium", "confidence": "High", "why": "Good CTR", "about_competitor": True}},
]
FIGMA_OWN = [
    {"title": "Figma Config keynote", "url": "https://www.figma.com/blog/config", "date": RECENT,
     "highlights": ["Figma announced new features at Config."],
     "graded": {"channel": "events", "message": "Config launches", "signal": "Wide coverage",
                "strength": "Strong", "confidence": "High", "why": "Flagship event", "about_competitor": True}},
]


class FakeExa:
    """Answers search / get_contents / answer like exa_py does, from the fixtures above."""

    def __init__(self, pages=None, fail_search="", fail_once=False, uncited_unless_pro=False):
        self.pages = pages or {"Sketch": SKETCH, "Figma": FIGMA_OWN}
        self.fail_search = fail_search  # error text to raise from search(), e.g. "429 rate limit"
        self.fail_once = fail_once
        self.uncited_unless_pro = uncited_unless_pro  # mimic the default model ignoring the list
        self.calls: list = []

    def _for(self, query):
        for name in self.pages:
            if query.startswith(f"recent {name} ") or query.startswith(f"latest {name} ") or query.startswith(f"{name} "):
                return self.pages[name]
        return []

    def search(self, query, **kw):
        self.calls.append(("search", query, kw))
        if self.fail_search and not query.startswith("top direct competitors"):
            msg = self.fail_search
            if self.fail_once:
                self.fail_search = ""
            raise RuntimeError(msg)
        if query.startswith("top direct competitors"):
            out = {"competitors": [{"name": "Sketch", "domain": "sketch.com", "reason": "design tool"}]}
            return NS(results=[], output=NS(content=json.dumps(out)))
        rows = self._for(query)
        results = [NS(title=r["title"], url=r["url"], published_date=r["date"], highlights=r["highlights"])
                   for r in rows]
        content = {"campaigns": [{"title": r["title"], "url": r["url"], **r["graded"]} for r in rows]}
        return NS(results=results, output=NS(content=content) if "output_schema" in kw else None)

    def get_contents(self, urls, **kw):
        self.calls.append(("get_contents", urls, kw))
        all_rows = [r for rows in self.pages.values() for r in rows]
        return NS(results=[NS(url=r["url"], text=" ".join(r["highlights"]), published_date=r["date"] or None)
                           for r in all_rows if r["url"] in urls])

    def answer(self, query, **kw):
        self.calls.append(("answer", query, kw))
        ids = re.findall(r"- (C\d+) ", kw.get("system_prompt", ""))
        own = re.findall(r"- (M\d+) ", kw.get("system_prompt", ""))
        if self.uncited_unless_pro and kw.get("model") != "exa-pro":
            return NS(answer={"recommendations": [{"title": "Generic", "action": "x", "why": "y", "evidence": []}]},
                      citations=[])
        recs = [
            {"title": "Kept", "action": "Do the thing.", "why": "Rival did it.", "evidence": ids[:1] + own[:1]},
            {"title": "Web-only", "action": "x", "why": "y", "evidence": ["https://example.com/article"]},
            {"title": "Own-only", "action": "x", "why": "y", "evidence": own[:1]},
            {"title": "Made-up number", "action": "Aim for 45% growth.", "why": "Rival did.", "evidence": ids[:1]},
        ]
        return NS(answer={"recommendations": recs}, citations=[])
