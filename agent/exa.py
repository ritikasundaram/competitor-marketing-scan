"""Exa research tools: discovery, search and page reading. Each call is a traced span.

- discover_rivals: one search with an output schema (Exa fills in name/domain/reason)
- search: type "auto" + highlights -> RETRIEVER span with every result as a document
- read: get_contents for page text -> TOOL span
"""
from __future__ import annotations

import datetime
import json
import os
import re
import time
from types import SimpleNamespace as NS
from typing import Optional

from . import tracing
from .checks import norm_url
from .tracing import KIND


class ExaStop(RuntimeError):
    """An Exa error that makes the rest of the scan pointless (bad key, rate limit, no credit)."""


def exa_stop_reason(err: Exception) -> Optional[str]:
    msg = str(err)
    if "401" in msg or "INVALID_API_KEY" in msg or "403" in msg:
        return "Exa rejected the API key. Check EXA_API_KEY in .env (dashboard.exa.ai/api-keys)."
    if "429" in msg or "rate limit" in msg.lower():
        return "Exa rate limit reached. Wait a bit and try again, or check your Exa plan."
    if "402" in msg or "credits" in msg.lower():
        return "Exa account is out of credits. Top up at dashboard.exa.ai."
    return None


def make_client(mock: bool):
    if mock:
        return MockExa()
    key = os.getenv("EXA_API_KEY")
    if not key:
        raise ExaStop("EXA_API_KEY is not set. Add it to .env, or tick Mock mode.")
    from exa_py import Exa
    return Exa(api_key=key)


def _call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception as e:  # noqa: BLE001
        reason = exa_stop_reason(e)
        if reason:
            raise ExaStop(reason) from e
        raise


RIVALS_SCHEMA = {
    "type": "object",
    "properties": {"rivals": {"type": "array", "items": {
        "type": "object",
        "properties": {"name": {"type": "string"}, "domain": {"type": "string"},
                       "reason": {"type": "string"}},
        "required": ["name", "domain"]}}},
    "required": ["rivals"],
}


def discover_rivals(client, company: dict, market: str, max_n: int) -> list:
    who = f"{company['name']} ({company['domain']})" if company.get("domain") else company["name"]
    query = f"top direct competitors of {who}, {market}"
    with tracing.span("exa.discover", KIND.RETRIEVER, input=query, **{"exa.type": "auto"}) as s:
        r = _call(client.search, query, type="auto", contents={"highlights": True},
                  system_prompt=("List only direct competitors offering a comparable product that are "
                                 "actively marketed. Omit anything you cannot verify from page content; "
                                 "never guess domains."),
                  output_schema=RIVALS_SCHEMA)
        s.documents([_doc(x, f"D{i + 1}") for i, x in enumerate(r.results or [])])
        content = getattr(getattr(r, "output", None), "content", None)
        if isinstance(content, str):
            try:
                content = json.loads(content)
            except ValueError:
                content = None
        own = (company.get("domain") or "").lower()
        rivals = []
        for c in (content or {}).get("rivals", []):
            dom = (c.get("domain") or "").lower().removeprefix("https://").removeprefix("www.").strip("/")
            if c.get("name") and dom != own and all(x["name"] != c["name"] for x in rivals):
                rivals.append({"name": c["name"], "domain": dom, "reason": c.get("reason", "")})
        rivals = rivals[:max_n]
        s.output(rivals)
        return rivals


def _doc(x, doc_id: str) -> dict:
    return {"id": doc_id, "url": x.url, "title": getattr(x, "title", "") or "",
            "date": (getattr(x, "published_date", "") or "")[:10],
            "snippet": " ".join(getattr(x, "highlights", None) or [])[:800],
            "score": getattr(x, "score", None)}


SOCIAL_DOMAINS = ["linkedin.com", "x.com", "twitter.com", "youtube.com", "instagram.com"]


def search(client, query: str, id_prefix: str, num: int = 6, include_domains: Optional[list] = None) -> list:
    """Search by meaning; results get ids like Q3-1 that the AI must cite.

    include_domains limits results to those sites (e.g. SOCIAL_DOMAINS for event engagement).
    """
    extra = {"include_domains": include_domains} if include_domains else {}
    with tracing.span("exa.search", KIND.RETRIEVER, input=query,
                      **{"exa.type": "auto", "exa.num_results": num,
                         "exa.include_domains": ",".join(include_domains or [])}) as s:
        r = _call(client.search, query, type="auto", num_results=num, contents={"highlights": True}, **extra)
        docs = [_doc(x, f"{id_prefix}-{i + 1}") for i, x in enumerate((r.results or [])[:num])]
        s.documents(docs)
        s.output({"results": len(docs)})
        return docs


def read(client, docs: list, max_pages: int = 3, max_chars: int = 5000) -> list:
    """Fetch full page text for the top results so claims can be checked against it."""
    urls = [d["url"] for d in docs if not d.get("text")][:max_pages]
    if not urls:
        return docs
    with tracing.span("exa.read", KIND.TOOL, input={"urls": urls},
                      **{"tool.name": "exa.get_contents",
                         "tool.parameters": json.dumps({"max_characters": max_chars})}) as s:
        try:
            res = _call(client.get_contents, urls, text={"max_characters": max_chars})
        except ExaStop:
            raise
        except Exception as e:  # noqa: BLE001  one failed fetch shouldn't sink the question
            s.set("error", str(e)[:200])
            return docs
        by_url = {norm_url(x.url): x for x in (getattr(res, "results", None) or [])}
        got = 0
        for d in docs:
            x = by_url.get(norm_url(d["url"]))
            if x is not None and (getattr(x, "text", "") or "").strip():
                d["text"] = x.text.strip()
                d["date"] = d.get("date") or (getattr(x, "published_date", "") or "")[:10]
                got += 1
        s.output({"pages_read": got, "asked": len(urls)})
    return docs


# ---------- mock Exa: a small fake web, so mock runs and tests cost nothing ----------

_MOCK_RIVALS = [
    {"name": "Canva", "domain": "canva.com", "reason": "Design tool for non-designers and teams (mock)"},
    {"name": "Miro", "domain": "miro.com", "reason": "Visual collaboration overlap (mock)"},
    {"name": "Penpot", "domain": "penpot.app", "reason": "Open-source design tool (mock)"},
    {"name": "Sketch", "domain": "sketch.com", "reason": "Mac design tool (mock)"},
]


class MockExa:
    """Answers search / get_contents like exa_py, from a tiny invented web."""

    def __init__(self):
        self.calls = []
        # A short pause per call so mock runs look like real ones in the live view (tests set 0).
        self.delay = float(os.getenv("MOCK_DELAY", "0.4"))

    def search(self, query, **kwargs):
        self.calls.append(("search", query, kwargs))
        time.sleep(self.delay)
        if kwargs.get("output_schema"):
            return NS(results=[NS(url="https://example.com/best-design-tools", title="Best design tools",
                                  published_date="", highlights=["Canva, Miro, Penpot and Sketch compete."],
                                  score=0.8)],
                      output=NS(content={"rivals": _MOCK_RIVALS}))
        m = re.search(r"([A-Z][\w.]*)\s*\(([\w.-]+\.\w+)\)", query)
        if kwargs.get("include_domains"):  # social search: posts about an event, with engagement counts
            name = m.group(1) if m else "Acme"
            recent = (datetime.date.today() - datetime.timedelta(days=30)).isoformat()
            return NS(output=None, results=[
                NS(url=f"https://www.linkedin.com/posts/{name.lower()}_summit-recap", title=f"{name} Summit recap",
                   published_date=recent, score=0.9,
                   highlights=[f"{name} Summit recap: 1,240 reactions and 85 comments on our launch keynote."]),
                NS(url=f"https://www.youtube.com/watch?v={name.lower()}keynote", title=f"{name} Summit keynote",
                   published_date=recent, score=0.8, highlights=[f"{name} Summit keynote, 48K views."]),
            ][: kwargs.get("num_results", 6)])
        name, dom = (m.group(1), m.group(2)) if m else ("Acme", "acme.com")
        slug = re.sub(r"[^a-z]+", "-", query.lower())[:40].strip("-")
        recent = (datetime.date.today() - datetime.timedelta(days=40)).isoformat()
        older = (datetime.date.today() - datetime.timedelta(days=120)).isoformat()
        results = [
            NS(url=f"https://www.{dom}/blog/{slug}", title=f"{name}: what's new", published_date=recent,
               highlights=[f"{name} announced a new launch for teams, with 12 partner integrations."], score=0.9),
            NS(url=f"https://techcrunch.com/{older[:4]}/{older[5:7]}/{name.lower()}-{slug[:20]}",
               title=f"{name} pushes into enterprise", published_date=older,
               highlights=[f"{name} drew 3,000 attendees to its user event and signed 2 agency partners."],
               score=0.8),
            NS(url=f"https://www.businesswire.com/news/home/20230629787067/en/{name.lower()}",
               title=f"{name} partnership (2023)", published_date="",
               highlights=[f"{name} announces a partnership."], score=0.6),
            NS(url=f"https://www.linkedin.com/posts/{name.lower()}-events_popup", title="Events agency pop-up",
               published_date=recent, highlights=["A restaurant pop-up by an events agency."], score=0.5),
        ]
        return NS(results=results[: kwargs.get("num_results", 6)], output=None)

    def get_contents(self, urls, **kwargs):
        self.calls.append(("get_contents", urls, kwargs))
        time.sleep(self.delay)
        out = []
        for u in urls:
            if "linkedin.com/posts" in u and "_summit" in u:
                name = u.split("/posts/")[1].split("_")[0].capitalize()
                text = f"{name} Summit recap: 1,240 reactions and 85 comments on our launch keynote."
            elif "linkedin.com" in u:
                text = "A restaurant pop-up built by an events agency for a dessert brand."
            elif "businesswire" in u:
                text = f"{u.rstrip('/').split('/')[-1].capitalize()} announces a partnership with a design agency."
            elif "youtube.com" in u:
                text = "Summit keynote video, 48K views."
            elif "techcrunch" in u:
                name = u.rstrip("/").split("/")[-1].split("-")[0].capitalize()
                text = (f"{name} is pushing into enterprise. Its user event drew 3,000 attendees and it signed "
                        f"2 agency partners. Analysts called it {name}'s biggest marketing push yet.")
            else:
                text = (f"Page text for {u}. The company announced a new launch for teams, with 12 partner "
                        "integrations, a webinar series, and a template gallery.")
            out.append(NS(url=u, text=text, published_date=""))
        return NS(results=out)
