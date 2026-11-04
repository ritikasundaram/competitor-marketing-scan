"""
Manual-run competitor marketing researcher.
- Uses Exa (exa_py) for search + contents, or --dry-run mock data (no key, no cost).
- Synthesis is template-based by default ($0). Set OPENAI_API_KEY to use LLM synthesis.
- Tracing: Phoenix (local, free) and/or Langfuse via OpenTelemetry/OpenInference if installed.
  Falls back to console + traces/*.jsonl always, so dry-run works with zero deps.

Usage:
  pip install -r requirements.txt
  # Rivals auto-discovered; Exa grades campaigns as it searches:
  python runner.py --company Figma --dry-run
  EXA_API_KEY=... python runner.py --company Figma --tracer phoenix
  # Skip discovery or grading when wanted:
  python runner.py --company Figma --competitors "Canva,canva.com" --no-grade --dry-run
"""
from __future__ import annotations
import argparse
import base64
import datetime
import json
import os
import re
import time
import uuid
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # .env loading optional; EXA_API_KEY can come from the environment directly

try:
    import yaml
except ImportError:
    yaml = None

ROOT = Path(__file__).parent
REPORTS = ROOT / "reports"
TRACES = ROOT / "traces"


# ---------- minimal tracer: OTel when available, else JSONL ----------

_OTEL_PROVIDER = None


class Tracer:
    def __init__(self, backend: str):
        self.backend = backend
        self.run_id = uuid.uuid4().hex[:8]
        self._otel = None
        self._provider = None
        self.otel_trace_id = ""  # set by the first span; lets the web app link to Phoenix
        TRACES.mkdir(exist_ok=True)
        if backend in ("phoenix", "langfuse", "both"):
            self._try_setup_otel(backend)

    def _try_setup_otel(self, backend: str):
        global _OTEL_PROVIDER
        try:
            from opentelemetry import trace as otel_trace
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        except Exception as e:
            print(f"[trace] OTel deps missing ({e}). Using local JSONL only.")
            return

        if _OTEL_PROVIDER is None:
            project = os.getenv("PHOENIX_PROJECT_NAME", "marketing-agent")
            # Phoenix files traces by openinference.project.name (else "default").
            provider = TracerProvider(
                resource=Resource.create({"service.name": project,
                                          "openinference.project.name": project})
            )
            added = False
            if backend in ("phoenix", "both"):
                # Phoenix local default: http://localhost:6006/v1/traces
                endpoint = os.getenv("PHOENIX_COLLECTOR_ENDPOINT", "http://localhost:6006/v1/traces")
                provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
                print(f"[trace] Phoenix exporter -> {endpoint}")
                added = True
            if backend in ("langfuse", "both"):
                public_key = os.getenv("LANGFUSE_PUBLIC_KEY")
                secret_key = os.getenv("LANGFUSE_SECRET_KEY")
                if public_key and secret_key:
                    endpoint = (
                        os.getenv("LANGFUSE_OTEL_ENDPOINT")
                        or os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
                        or "https://cloud.langfuse.com/api/public/otel"
                    )
                    if not endpoint.rstrip("/").endswith("/v1/traces"):
                        endpoint = endpoint.rstrip("/") + "/v1/traces"
                    token = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()
                    provider.add_span_processor(
                        BatchSpanProcessor(
                            OTLPSpanExporter(endpoint=endpoint,
                                             headers={"Authorization": f"Basic {token}"})
                        )
                    )
                    print(f"[trace] Langfuse exporter -> {endpoint}")
                    added = True
                else:
                    print("[trace] Langfuse selected but LANGFUSE_PUBLIC_KEY/LANGFUSE_SECRET_KEY are not set.")
            if not added:
                print("[trace] No exporters configured. Using local JSONL only.")
                return
            otel_trace.set_tracer_provider(provider)
            _OTEL_PROVIDER = provider
        else:
            print("[trace] Reusing existing OTel provider (restart to change exporters).")

        self._provider = _OTEL_PROVIDER
        self._otel = otel_trace.get_tracer("marketing-agent")

    def flush(self):
        if self._provider is not None:
            try:
                self._provider.force_flush(timeout_millis=5000)
            except Exception:
                pass

    def span(self, name: str, attrs: dict | None = None):
        return Span(self, name, attrs or {})


class Span:
    def __init__(self, tracer: Tracer, name: str, attrs: dict):
        self.tracer = tracer
        self.name = name
        self.attrs = attrs
        self.start = time.time()
        self._ctx = None
        self._span = None
        if tracer._otel is not None:
            try:
                self._ctx = tracer._otel.start_as_current_span(name)
                self._span = self._ctx.__enter__()
                if not tracer.otel_trace_id:
                    tracer.otel_trace_id = format(self._span.get_span_context().trace_id, "032x")
                for k, v in attrs.items():
                    self._span.set_attribute(k, str(v)[:1000])
            except Exception:
                self._span = None

    def __enter__(self):
        print(f"  ▸ {self.name} {self.attrs}")
        return self

    def set(self, k: str, v):
        self.attrs[k] = v
        if self._span is not None:
            try:
                self._span.set_attribute(k, str(v)[:1000])
            except Exception:
                pass

    def __exit__(self, *exc):
        dur = time.time() - self.start
        self.attrs["duration_s"] = round(dur, 2)
        self.attrs["run_id"] = self.tracer.run_id
        self.attrs["span"] = self.name
        if self._span is not None:
            try:
                self._ctx.__exit__(*exc)
            except Exception:
                pass
        with open(TRACES / f"{self.tracer.run_id}.jsonl", "a") as f:
            f.write(json.dumps(self.attrs, default=str) + "\n")
        print(f"  ✓ {self.name} ({dur:.1f}s)")


# ---------- exa clients ----------

MOCK = {
    "Notion": [
        {"title": "Notion AI launch", "url": "https://notion.so/blog/notion-ai",
         "date": "2026-02-10", "channel": "blog + social", "message": "AI writing assistant launch",
         "snippet": "AI writing assistant launch with creator partnerships.",
         "signal": "120 press hits, #2 SEO for 'ai notes', 8k LinkedIn engagements", "strength": "Strong",
         "confidence": "Medium", "why": "High pickup volume, but engagement counts are estimates from public posts."},
        {"title": "Notion back-to-school", "url": "https://notion.so/blog/students",
         "date": "2025-09-01", "channel": "email + campus ambassadors", "message": "Student discount + templates",
         "snippet": "Student discount + template gallery push.",
         "signal": "Moderate pickup, steady cadence yearly", "strength": "Medium",
         "confidence": "Medium", "why": "Recurring yearly push with moderate coverage."},
    ],
    "Asana": [
        {"title": "Asana AI Studio launch", "url": "https://asana.com/blog/ai-studio",
         "date": "2026-05-20", "channel": "webinar + press", "message": "No-code AI workflows",
         "snippet": "No-code AI workflows launch.",
         "signal": "40 press hits, low social vs baseline", "strength": "Medium",
         "confidence": "Low", "why": "Thin public signal in mock data."},
    ],
}

MOCK_DISCOVERY = [
    {"name": "Canva", "domain": "canva.com", "reason": "Direct design-tool competitor (mock)"},
    {"name": "Miro", "domain": "miro.com", "reason": "Visual collaboration overlap (mock)"},
    {"name": "Notion", "domain": "notion.so", "reason": "Docs/whiteboards overlap (mock)"},
]

# {n} becomes "Name (domain)" when the domain is known, so "Sketch" means sketch.com.
QUERIES = ["recent {n} campaign launch", "latest {n} marketing campaign", "{n} brand partnership"]

CAMPAIGN_SCHEMA = {
    "type": "object",
    "properties": {
        "campaigns": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "url": {"type": "string"},
                    "channel": {"type": "string"},
                    "message": {"type": "string"},
                    "signal": {"type": "string"},
                    "strength": {"type": "string"},
                    "confidence": {"type": "string"},
                    "why": {"type": "string"},
                    # Exa caps output schemas at 10 properties in total, so there is
                    # room for only this one extra field (no run_by here).
                    "about_competitor": {"type": "boolean"},
                },
                "required": ["title", "url", "about_competitor"],
            },
        }
    },
    "required": ["campaigns"],
}

GRADE_SYSTEM_PROMPT = (
    "Extract distinct marketing campaigns from the results. "
    "channel: where it ran (press, social, email, webinar, events). "
    "message: one-line campaign message. "
    "signal: public effectiveness evidence only (press volume, engagement, SEO visibility). "
    "strength: exactly one of Strong, Medium, Weak. confidence: exactly one of High, Medium, Low. "
    "why: one sentence justifying strength from the evidence. "
    "Leave unverifiable fields empty; never invent revenue, spend, or ROI."
)


def who_is(name: str, domain: str = "", market: str = "") -> str:
    """One line that pins down which company we mean, e.g. 'Sketch (sketch.com), in: design tools'."""
    out = f"{name} ({domain})" if domain else name
    return f"{out}, in: {market}" if market else out


def relevance_prompt(target: str) -> str:
    """Tells the grader which company counts, so look-alike names get rejected."""
    return (
        f"The competitor is {target}. "
        "about_competitor: true only if the campaign was run by this exact company/product. "
        "Set it false for different companies with the same or a similar name (restaurants, "
        "agencies, event firms, other software), for agencies describing their own client work "
        "for other brands, and for other products from the same parent company."
    )


class ExaStop(RuntimeError):
    """An Exa error that makes the rest of the scan pointless (bad key, rate limit, no credit)."""


def exa_stop_reason(err: Exception) -> str | None:
    msg = str(err)
    if "401" in msg or "INVALID_API_KEY" in msg or "403" in msg:
        return "Exa rejected the API key. Check EXA_API_KEY in .env (dashboard.exa.ai/api-keys)."
    if "429" in msg or "rate limit" in msg.lower():
        return "Exa rate limit reached. Wait a bit and try again, or check your Exa plan."
    if "402" in msg or "credits" in msg.lower():
        return "Exa account is out of credits. Top up at dashboard.exa.ai."
    return None


def _parse_output_content(result) -> list:
    try:
        content = result.output.content if hasattr(result, "output") and result.output else None
        if isinstance(content, str):
            content = json.loads(content)
        return (content or {}).get("campaigns", []) or []
    except Exception as e:
        print(f"    ! Could not parse graded output ({e}).")
        return []


def exa_search(client, query: str, num: int | None = None, graded: bool = False,
               target: str = ""):
    # Skill-canonical recommended request: query + type auto + nested highlights.
    # num_results omitted unless it's an intentional non-default (server default 10).
    # graded=True adds a compact outputSchema (stays on auto): Exa extracts
    # channel/message/signal/strength/confidence/why per campaign — no extra LLM needed.
    kwargs: dict = {"type": "auto", "contents": {"highlights": True}}
    if num is not None and num != 10:
        kwargs["num_results"] = num
    if graded:
        kwargs["output_schema"] = CAMPAIGN_SCHEMA
        kwargs["system_prompt"] = GRADE_SYSTEM_PROMPT + (" " + relevance_prompt(target) if target else "")
    r = client.search(query, **kwargs)
    out = []
    for x in (r.results or [])[: (num or 10)]:
        out.append({"title": x.title, "url": x.url,
                    "date": getattr(x, "published_date", "") or "",
                    "snippet": " ".join(getattr(x, "highlights", []) or [])[:500]})
    return out, (_parse_output_content(r) if graded else [])


def dedupe_campaigns(campaigns: list) -> list:
    seen, out = set(), []
    for c in campaigns:
        key = (c.get("url") or c.get("title") or "").strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out


# ---------- fact checks: recency + numbers must appear in the source ----------

_URL_DATE = re.compile(r"(?<!\d)(20\d{2})[/_-]?(0[1-9]|1[0-2])(?:[/_-]?(0[1-9]|[12]\d|3[01]))?")
_NUM = re.compile(r"\d+(?:[.,]\d+)*")
_WORDS = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
          "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen",
          "eighteen", "nineteen", "twenty"]


def _norm_url(url: str) -> str:
    return (url or "").split("#")[0].split("?")[0].rstrip("/").lower()


def _parse_date(value) -> datetime.date | None:
    try:
        return datetime.date.fromisoformat(str(value or "")[:10])
    except ValueError:
        return None


def _date_from_url(url: str) -> datetime.date | None:
    """Dates baked into URLs, e.g. /2023/06/29/ or businesswire's /20230629787067/."""
    m = _URL_DATE.search(url or "")
    if not m:
        return None
    try:
        return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3) or 1))
    except ValueError:
        return None


def _numbers_in(text: str) -> list:
    return [n for n in _NUM.findall(text or "") if any(ch.isdigit() for ch in n)]


def _number_in_evidence(num: str, evidence: str, suffix: str = "") -> bool:
    core = num.replace(",", "")
    forms = {core}
    try:
        value = float(core)
        if value.is_integer() and 0 <= value <= 20:
            forms.add(_WORDS[int(value)])
        mult = {"k": 1_000, "m": 1_000_000}.get(suffix)
        if mult:
            full = int(round(value * mult))
            forms |= {str(full), f"{full:,}".replace(",", "")}
    except ValueError:
        pass
    for f in forms:
        pat = rf"\b{re.escape(f)}\b" if f.isalpha() else rf"(?<![\d.]){re.escape(f)}(?![\d])"
        if re.search(pat, evidence):
            return True
    return False


def unverified_numbers(claim: str, evidence: str) -> list:
    """Numbers in a model-written claim that never appear in the fetched source text."""
    ev = re.sub(r"(?<=\d),(?=\d)", "", (evidence or "").lower())
    missing = []
    for m in _NUM.finditer(claim or ""):
        tail = (claim[m.end():m.end() + 2] or "").strip().lower()
        suffix = tail[:1] if tail[:1] in ("k", "m") and not tail[1:2].isalpha() else ""
        shown = m.group(0) + ("%" if tail[:1] == "%" else suffix.upper())
        if not _number_in_evidence(m.group(0), ev, suffix) and shown not in missing:
            missing.append(shown)
    return missing


def _host(url: str) -> str:
    from urllib.parse import urlparse
    try:
        return (urlparse(url or "").hostname or "").lower().removeprefix("www.")
    except ValueError:
        return ""


def relevance(c: dict, name: str, domain: str = "") -> str | None:
    """None if the campaign is about this competitor, else a short reason it isn't.

    Order: a page on the competitor's own site always counts; otherwise trust the
    grader's about_competitor verdict; with no verdict (ungraded runs), the page
    must at least mention the name or domain.
    """
    dom = (domain or "").lower().removeprefix("www.").strip("/")
    host = _host(c.get("url", ""))
    if dom and (host == dom or host.endswith("." + dom)):
        return None
    verdict = c.get("about_competitor")
    if isinstance(verdict, str):
        verdict = {"true": True, "false": False}.get(verdict.strip().lower())
    run_by = str(c.get("run_by") or "").strip()
    if verdict is False:
        return f"not {name}" + (f": run by {run_by}" if run_by and run_by.lower() != name.lower() else "")
    if verdict is True:
        return None
    text = " ".join(str(c.get(k) or "") for k in ("title", "snippet", "_page_text", "proof")).lower()
    if re.search(rf"\b{re.escape(name.lower())}\b", text) or (dom and dom in text):
        return None
    return f"doesn't mention {name}"


def verify_campaigns(campaigns: list, max_age_days: int = 365,
                     today: datetime.date | None = None,
                     competitor: tuple | None = None) -> list:
    """Flag old campaigns, look-alike companies, and numbers the source doesn't back up.

    Adds c["flags"] (short notes), c["stale"] and c["off_topic"] (bools), and
    c["excluded"] (why it's left out of the analysis, or ""). Unverified numbers
    drop confidence to Low; nothing is silently rewritten.
    """
    today = today or datetime.date.today()
    for c in campaigns:
        flags = []
        off = relevance(c, *competitor) if competitor else None
        c["off_topic"] = bool(off)
        # A date written in the URL (e.g. /changelog/2026-09-24) beats an older
        # page date, which is often when the page was first created.
        dates = [x for x in (_parse_date(c.get("date")), _date_from_url(c.get("url", ""))) if x and x <= today]
        d = max(dates) if dates else None
        if d and d <= today:
            c["date"] = d.isoformat()
            age = (today - d).days
            c["stale"] = max_age_days > 0 and age > max_age_days
            if c["stale"]:
                flags.append(f"old: published {d.isoformat()} ({age // 30} months ago)")
        else:
            c["stale"] = False
            flags.append("date unknown")

        evidence = " ".join(str(c.get(k) or "") for k in ("snippet", "_page_text", "proof"))
        claim = " ".join(str(c.get(k) or "") for k in ("signal", "why"))
        missing = unverified_numbers(claim, evidence)
        if missing:
            flags.append("numbers not found in source: " + ", ".join(missing))
            c["confidence"] = "Low"

        for key, allowed in (("strength", ("Strong", "Medium", "Weak")),
                             ("confidence", ("High", "Medium", "Low"))):
            v = str(c.get(key) or "").strip().capitalize()
            if v in allowed:
                c[key] = v
        c["flags"] = flags
        c["excluded"] = off or next((f for f in flags if f.startswith("old")), "")
        c.pop("_page_text", None)
    return campaigns


def fetch_proof(client, campaigns: list, max_urls: int = 5) -> list:
    """Fetch page text for the top campaign URLs (mirrors web_fetch_exa in the agent)."""
    if max_urls <= 0:
        return campaigns
    # Campaigns making numeric claims go first: those are the ones we must check.
    def has_numbers(c):
        return bool(_numbers_in(f"{c.get('signal', '')} {c.get('why', '')}"))
    urls = []
    for c in sorted(campaigns, key=lambda c: not has_numbers(c)):
        if c.get("about_competitor") is False:
            continue  # grader already says it's another company; don't spend a fetch
        url = (c.get("url") or "").strip()
        if url and url not in urls:
            urls.append(url)
        if len(urls) >= max_urls:
            break
    if not urls:
        return campaigns
    try:
        # Longer text for fact-checking; the report still shows a short excerpt.
        res = client.get_contents(urls, text={"max_characters": 6000})
        by_url, dates = {}, {}
        for x in (getattr(res, "results", None) or []):
            text = (getattr(x, "text", "") or "").strip()
            if text:
                by_url[_norm_url(x.url)] = text
            if getattr(x, "published_date", None):
                dates[_norm_url(x.url)] = x.published_date
        for c in campaigns:
            key = _norm_url(c.get("url"))
            text = by_url.get(key)
            if text:
                c["proof"] = text[:1200]
                c["_page_text"] = text
            if not c.get("date") and dates.get(key):
                c["date"] = dates[key]
        print(f"    fetched proof for {len(by_url)}/{len(urls)} URLs")
    except Exception as e:
        print(f"    ! Proof fetch skipped ({e})")
    return campaigns


def discover_competitors(client, company: str, domain: str, description: str,
                         max_n: int = 4, dry: bool = False):
    """Find direct competitors via one skill-canonical structured-output search.

    outputSchema is used because names/domains must be extracted from pages
    (per build-with-exa skill); compact schema stays on type auto.
    """
    if dry or client is None:
        time.sleep(0.1)
        return [dict(c) for c in MOCK_DISCOVERY[:max_n]]
    who = f"{company} ({domain})" if domain else company
    if description:
        who += f" — {description}"
    r = client.search(
        f"top direct competitors of {who}",
        type="auto",
        contents={"highlights": True},
        system_prompt=("List only direct competitors offering a comparable product. "
                        "Skip discontinued or maintenance-only products that are no longer actively marketed. "
                        "Omit results you cannot verify from page content; never guess domains."),
        output_schema={
            "type": "object",
            "properties": {
                "competitors": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "domain": {"type": "string"},
                            "reason": {"type": "string"},
                        },
                        "required": ["name", "domain"],
                    },
                }
            },
            "required": ["competitors"],
        },
    )
    comps = []
    try:
        content = r.output.content if hasattr(r, "output") and r.output else None
        if isinstance(content, str):
            content = json.loads(content)
        for c in (content or {}).get("competitors", [])[:max_n]:
            if c.get("name"):
                comps.append({"name": c["name"], "domain": c.get("domain", ""),
                              "reason": c.get("reason", "")})
    except Exception as e:
        print(f"    ! Could not parse discovery output ({e}); falling back to raw results.")
    if not comps:  # structured output empty — degrade to titles, domains TBD
        for x in (r.results or [])[:max_n]:
            comps.append({"name": (x.title or "").split("|")[0].split("-")[0].strip()[:60],
                          "domain": "", "reason": x.url or ""})
    return [c for c in comps if c.get("name")][:max_n]


def run_competitor(tracer: Tracer, name: str, domain: str, cfg: dict, client, dry: bool):
    campaigns = []
    graded_hits: list = []
    market = (cfg.get("my_company") or {}).get("description", "")
    target = who_is(name, domain, market)
    with tracer.span("competitor", {"competitor": name, "domain": domain}) as comp:
        if dry or client is None:
            with tracer.span("exa.search", {"mode": "mock",
                                            "queries": cfg.get("max_queries_per_competitor", 3)}) as s:
                time.sleep(0.1)
                campaigns = [dict(c) for c in MOCK.get(name, MOCK["Notion"])]
                s.set("results", len(campaigns))
        else:
            want_grades = cfg.get("grade", True)
            for q in QUERIES[: cfg.get("max_queries_per_competitor", 3)]:
                query = q.format(n=f"{name} ({domain})" if domain else name)
                with tracer.span("exa.search", {"query": query, "graded": want_grades}) as s:
                    try:
                        res, graded = exa_search(client, query, cfg.get("max_results_per_query", 10),
                                                graded=want_grades, target=target)
                        s.set("results", len(res))
                        s.set("graded", len(graded))
                        for r in res:
                            campaigns.append({**r, "channel": "see snippet",
                                              "signal": "manual review needed", "strength": "TBD"})
                        graded_hits.extend(graded)
                    except Exception as e:
                        s.set("error", str(e)[:300])
                        stop = exa_stop_reason(e)
                        if stop:
                            raise ExaStop(stop) from e
                        print(f"    ! Exa search failed for this query, skipping it: {e}")
            if graded_hits:
                # Keep each raw result's publish date + highlights so the graded
                # rows can be checked against what the source said.
                raw: dict = {}
                for r in campaigns:
                    entry = raw.setdefault(_norm_url(r.get("url")), {"date": "", "snippet": ""})
                    entry["date"] = entry["date"] or r.get("date", "")
                    if r.get("snippet") and r["snippet"] not in entry["snippet"]:
                        entry["snippet"] = f"{entry['snippet']} {r['snippet']}".strip()
                merged = []
                for g in graded_hits:
                    src = raw.get(_norm_url(g.get("url")), {})
                    merged.append({
                        "title": g.get("title", ""), "url": g.get("url", ""),
                        "date": src.get("date", ""), "snippet": src.get("snippet", ""),
                        "channel": g.get("channel", ""), "message": g.get("message", ""),
                        "signal": g.get("signal", ""), "strength": g.get("strength", "TBD"),
                        "confidence": g.get("confidence", ""), "why": g.get("why", ""),
                        "run_by": g.get("run_by", ""), "about_competitor": g.get("about_competitor"),
                    })
                campaigns = merged
            campaigns = dedupe_campaigns(campaigns)

        if cfg.get("fetch_proof", True):
            with tracer.span("exa.proof", {"campaigns": len(campaigns),
                                           "max_urls": cfg.get("max_proof_urls", 5)}) as s:
                if dry or client is None:
                    if cfg.get("max_proof_urls", 5) > 0:
                        for c in campaigns:
                            c.setdefault("proof", (c.get("snippet") or "")[:600])
                    s.set("mode", "mock")
                else:
                    fetch_proof(client, campaigns, cfg.get("max_proof_urls", 5))
                s.set("with_proof", sum(1 for c in campaigns if c.get("proof")))

        with tracer.span("grade", {"campaigns": len(campaigns)}) as s:
            s.set("mode", "mock" if dry else "exa" if graded_hits else "skipped")

        with tracer.span("verify", {"campaigns": len(campaigns),
                                    "max_age_days": cfg.get("max_age_days", 365)}) as s:
            # Mock rows are shared across rivals, so the "is it them?" check is skipped in dry runs.
            verify_campaigns(campaigns, cfg.get("max_age_days", 365),
                             competitor=None if dry else (name, domain))
            s.set("stale", sum(1 for c in campaigns if c.get("stale")))
            s.set("off_topic", sum(1 for c in campaigns if c.get("off_topic")))
            s.set("undated", sum(1 for c in campaigns if "date unknown" in c.get("flags", [])))
            s.set("unverified_numbers", sum(1 for c in campaigns
                                            if any(f.startswith("numbers") for f in c.get("flags", []))))
    return campaigns


def _md(value) -> str:
    return str(value or "").replace("|", "\\|").replace("\n", " ").strip()


def _top_channel(camps: list) -> str:
    counts: dict = {}
    for c in camps:
        channel = (c.get("channel") or "unknown").split(",")[0].strip()
        if channel:
            counts[channel] = counts.get(channel, 0) + 1
    return max(counts, key=counts.get) if counts else "—"


def _strength_counts(camps: list) -> dict:
    out = {"Strong": 0, "Medium": 0, "Weak": 0, "TBD": 0}
    for c in camps:
        strength = (c.get("strength") or "TBD").split(" ")[0]
        out[strength if strength in out else "TBD"] += 1
    return out


def _campaign_link(c: dict) -> str:
    title = _md(c.get("title") or "untitled")
    url = (c.get("url") or "").strip()
    date = _md(c.get("date"))
    return f"[{title}]({url}){' ' + date if date else ''}" if url else f"{title}{' ' + date if date else ''}"


def _flag_note(c: dict) -> str:
    flags = [f for f in c.get("flags", []) if not f.startswith("old")]
    return f" — ⚠ {_md('; '.join(flags))}" if flags else ""


def _numbers_unverified(c: dict) -> bool:
    return any(f.startswith("numbers") for f in c.get("flags", []))


RECS_SCHEMA = {
    "type": "object",
    "properties": {
        "recommendations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "action": {"type": "string"},
                    "why": {"type": "string"},
                    "evidence": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["title", "action", "why", "evidence"],
            },
        }
    },
    "required": ["recommendations"],
}


def exa_recommendations(client, my_company: dict, results: dict, max_campaigns: int = 30,
                        own: list | None = None, max_own: int = 12) -> list:
    """Ask Exa's answer endpoint for concrete moves for my_company, grounded in the scanned campaigns.

    Each recommendation must cite competitor campaigns from the scan by ID (C1..).
    `own` is the company's own recent campaigns (M1..), shown so Exa doesn't suggest
    what the company already does. Citations that aren't scanned campaigns are
    dropped, and so is any recommendation with no competitor evidence. Returns []
    on any failure so the report falls back to highlights.
    """
    camps = [{"competitor": name, **c} for name, cs in results.items() for c in cs]
    # Strongest, cleanest evidence first; cap the prompt size.
    order = {"Strong": 0, "Medium": 1, "Weak": 2}
    camps.sort(key=lambda c: (_numbers_unverified(c), order.get(c.get("strength"), 3)))
    camps = camps[:max_campaigns]
    if client is None or not camps:
        return []
    # Campaigns get short IDs. Exa's answer endpoint also searches the web on its own;
    # citing by ID means only campaigns from this scan can count as evidence.
    by_id = {f"C{i}": c for i, c in enumerate(camps, 1)}
    me_name = my_company.get("name", "")
    mine = {f"M{i}": {"competitor": me_name, **c} for i, c in enumerate((own or [])[:max_own], 1)}
    own_facts = "\n".join(
        f"- {mid} {c.get('title', '')} | channel: {c.get('channel', '')} | message: {c.get('message', '')}"
        for mid, c in mine.items()) or "- (none found)"
    facts = "\n".join(
        f"- {cid} [{c['competitor']}] {c.get('title', '')} | channel: {c.get('channel', '')} | "
        f"message: {c.get('message', '')} | strength: {c.get('strength', 'TBD')}"
        + (" | (its numbers are unverified)" if _numbers_unverified(c) else "")
        for cid, c in by_id.items())
    me = my_company.get("name", "")
    about = who_is(me, my_company.get("domain", ""), my_company.get("description", ""))
    question = f"What should {me} do in its marketing next, given these competitors' recent campaigns?"
    # The retry names the IDs in the question itself; without that, short lists often come back uncited.
    strict = (f"Which marketing moves for {me} do competitor campaigns {', '.join(by_id)} suggest? "
              "Cite those IDs as evidence.")
    for model, query in ((None, question), ("exa-pro", strict)):
        try:
            r = client.answer(
                query,
                system_prompt=(
                    f"You advise the marketing team of {about}. Use ONLY the campaign list below as evidence "
                    "about competitors. Give 3-5 specific, actionable recommendations for this company: gaps "
                    "it can fill, messages or channels to counter, tactics worth adapting. Each needs: title "
                    "(short), action (one concrete sentence), why (one sentence tied to the evidence), and "
                    "evidence (the campaign IDs from the list that support it, e.g. [\"C3\", \"C7\"]). "
                    "Base every recommendation on listed campaigns, not on other web pages. Do not invent numbers, spend, "
                    "ROI, or campaigns not in the list; effectiveness is public-proxy only. "
                    f"The second list is {me}'s own recent campaigns: do not recommend something {me} is "
                    "already doing; if you build on one of its own campaigns, say how to go further and "
                    "include its M ID in evidence too. Every recommendation needs at least one C ID; evidence must "
                    "never be empty.\n\n"
                    f"Competitor campaigns:\n{facts}\n\n{me}'s own recent campaigns:\n{own_facts}"),
                output_schema=RECS_SCHEMA,
                **({"model": model} if model else {}),
            )
            content = r.answer
            if isinstance(content, str):
                content = json.loads(content)
            recs = (content or {}).get("recommendations", []) or []
        except Exception as e:
            print(f"    ! Exa answer recommendations skipped ({e})")
            return []
        out = _check_recs(recs, by_id, mine, me_name)
        if out:
            return out
        # The default model sometimes ignores the list (empty evidence); retry once with exa-pro.
        print(f"    ! No recommendation from model {model or 'exa'} cited scanned campaigns"
              + ("; retrying with exa-pro" if model is None else ""))
    return []


def _check_recs(recs: list, by_id: dict, mine: dict, me_name: str) -> list:
    """Keep recommendations that cite at least one scanned competitor campaign; flag unbacked numbers."""

    out = []
    for rec in recs[:5]:
        cited = []
        for ref in rec.get("evidence") or []:
            key = str(ref).strip().strip("[],.;'\"").strip().upper()
            c = by_id.get(key) or mine.get(key)
            if c and c.get("url") and c["url"] not in [x["url"] for x in cited]:
                cited.append({"competitor": c["competitor"], "title": c.get("title", ""), "url": c["url"]})
        if not any(x["competitor"] != me_name for x in cited):
            continue  # every recommendation must point at a scanned competitor campaign
        text = f"{rec.get('action', '')} {rec.get('why', '')}"
        by_url = {c.get("url"): c for c in [*by_id.values(), *mine.values()]}
        evidence_text = " ".join(" ".join(str(by_url[x["url"]].get(k) or "")
                                          for k in ("signal", "why", "message", "snippet", "proof", "title"))
                                 for x in cited)
        missing = unverified_numbers(text, evidence_text)
        out.append({"title": rec.get("title", ""), "action": rec.get("action", ""),
                    "why": rec.get("why", ""), "evidence": cited,
                    "flags": ["numbers not found in cited campaigns: " + ", ".join(missing)] if missing else []})
    return out


def report_summary(results: dict, old: dict | None, cfg: dict | None, run_id: str,
                   recs: list | None = None, own: list | None = None) -> dict:
    """Recommendations, fact-check counts and method notes, shared by the markdown and the dashboard."""
    cfg, old = cfg or {}, old or {}
    strong = [{"competitor": name, "title": c.get("title", ""), "url": c.get("url", ""),
               "date": c.get("date", ""), "why": c.get("why", "")}
              for name, camps in results.items() for c in camps
              if (c.get("strength") or "").startswith("Strong") and not _numbers_unverified(c)][:5]
    channels: dict = {}
    for camps in results.values():
        for c in camps:
            channel = (c.get("channel") or "").split(",")[0].strip()
            if channel:
                channels[channel] = channels.get(channel, 0) + 1
    top_channels = [{"channel": ch, "count": channels[ch]}
                    for ch in sorted(channels, key=channels.get, reverse=True)[:3]]

    max_age = cfg.get("max_age_days", 365)
    excluded = [{"competitor": name, **c} for name, camps in old.items() for c in camps]
    all_camps = [c for camps in results.values() for c in camps]
    checks = {
        "max_age_days": max_age,
        "excluded_old": sum(1 for c in excluded if c.get("stale") and not c.get("off_topic")),
        "excluded_off_topic": sum(1 for c in excluded if c.get("off_topic")),
        "undated": sum(1 for c in all_camps if "date unknown" in c.get("flags", [])),
        "unverified_numbers": sum(1 for c in all_camps if _numbers_unverified(c)),
    }
    queries = QUERIES[: cfg.get("max_queries_per_competitor", 3)]
    method = [
        f"Fact checks: excluded {checks['excluded_off_topic']} result(s) about a different company or "
        f"product, and {checks['excluded_old']} older than {max_age} days; {checks['undated']} have no "
        "known date. Numbers in a Signal/Why must appear in the search highlights or fetched page — "
        f"{checks['unverified_numbers']} campaign(s) had numbers that didn't, so their confidence is set "
        "to Low and they are left out of recommendations.",
        "Exa queries per competitor: " + ", ".join(q.format(n="<name> (<domain>)") for q in queries),
        f"Results per query: {cfg.get('max_results_per_query', 10)}; grading: "
        f"{'Exa' if cfg.get('grade', True) else 'off'}.",
        f"Proof fetched for up to {cfg.get('max_proof_urls', 5)} campaign URLs per competitor.",
        "Effectiveness is public-proxy only. No revenue/spend/ROI available via web search.",
        f"Trace: traces/{run_id}.jsonl (open in Phoenix/Langfuse if an exporter is enabled).",
    ]
    if own is not None and cfg.get("scan_self", True):
        method.insert(1, f"Own campaigns: the same searches found {len(own)} recent campaign(s) by "
                         "your company; recommendations are told not to repeat them.")
    if recs:
        method.insert(1, "Recommendations: written by Exa's answer endpoint; each must cite campaigns from this scan "
                         "by ID, and any without such evidence are dropped.")
    return {"recommendations": recs or [], "strong": strong, "top_channels": top_channels,
            "excluded": excluded, "checks": checks, "method": method, "own_campaigns": own or []}


def write_report_data(my_company: dict, results: dict, run_id: str, summary: dict,
                      extra: dict | None = None) -> Path:
    """Structured copy of the report for the web dashboard (same name as the .md, .json)."""
    REPORTS.mkdir(exist_ok=True)
    date = datetime.date.today().isoformat()
    path = REPORTS / f"{date}_competitor-scan_{run_id}.json"
    data = {"run_id": run_id, "date": date, "my_company": my_company,
            "competitors": {name: camps for name, camps in results.items()},
            **summary, **(extra or {})}
    path.write_text(json.dumps(data, indent=1, default=str))
    return path


def write_report(my_company: str, results: dict, run_id: str, cfg: dict | None = None,
                 old: dict | None = None, recs: list | None = None, own: list | None = None) -> Path:
    REPORTS.mkdir(exist_ok=True)
    cfg = cfg or {}
    old = old or {}
    date = datetime.date.today().isoformat()
    path = REPORTS / f"{date}_competitor-scan_{run_id}.md"
    lines = [f"# Competitor Marketing Scan — {date} (run {run_id})", "",
             f"My company: **{_md(my_company)}**", ""]

    lines += ["## Summary",
              "| Competitor | Campaigns found | Top channel | Strong / Medium / Weak |",
              "|---|---|---|---|"]
    for name, camps in results.items():
        sc = _strength_counts(camps)
        lines.append(f"| {_md(name)} | {len(camps)} | {_md(_top_channel(camps))} | "
                     f"{sc['Strong']} / {sc['Medium']} / {sc['Weak']} |")
    lines.append("")

    lines.append("## Per-competitor")
    for name, camps in results.items():
        lines += ["", f"### {_md(name)}", "", "#### Campaigns (facts with citations)"]
        if not camps:
            lines.append("- No campaigns found.")
        for c in camps:
            lines.append(f"- **{_campaign_link(c)}** — Channels: {_md(c.get('channel'))} — "
                         f"Message: {_md(c.get('message'))}")
            if c.get("proof"):
                lines.append(f"  > Proof: {_md(c.get('proof'))[:400]}")
        lines += ["", "#### Effectiveness (proxies only, NOT ROI)"]
        for c in camps:
            lines.append(f"- **{_md(c.get('title'))}** — Signal: {_md(c.get('signal'))} — "
                         f"Strength: {_md(c.get('strength') or 'TBD')} — "
                         f"Confidence: {_md(c.get('confidence') or 'Low')} — Why: {_md(c.get('why'))}"
                         f"{_flag_note(c)}")

    lines += ["", "## Comparison",
              "| Dimension | " + " | ".join(_md(n) for n in results) + " |",
              "|---|" + "---|" * len(results)]
    rows = [
        ("Campaigns found", lambda camps: str(len(camps))),
        ("Top channel", _top_channel),
        ("Strong campaigns", lambda camps: str(_strength_counts(camps)["Strong"])),
        ("Top message", lambda camps: _md(camps[0].get("message")) if camps else "—"),
    ]
    for label, fn in rows:
        lines.append(f"| {label} | " + " | ".join(fn(c) for c in results.values()) + " |")

    summary = report_summary(results, old, cfg, run_id, recs, own)

    lines += ["", f"## Recommendations for {_md(my_company)}"]
    for i, r in enumerate(summary["recommendations"], 1):
        flag = f" ⚠ {_md('; '.join(r['flags']))}" if r.get("flags") else ""
        lines.append(f"{i}. **{_md(r['title'])}** — {_md(r['action'])} {_md(r['why'])}{flag}")
        lines.append("   - Evidence: " + "; ".join(f"{_md(e['competitor'])}: [{_md(e['title'])}]({e['url']})"
                                              for e in r["evidence"]))
    if summary["recommendations"]:
        lines += ["", "### Highlights"]
    if summary["strong"]:
        lines.append("- Strongest competitor campaigns (study their proof points, not just their copy):")
        for c in summary["strong"]:
            lines.append(f"  - {_md(c['competitor'])}: {_campaign_link(c)} — {_md(c.get('why'))}")
    if summary["top_channels"]:
        lines.append("- Compete where rivals are investing: " +
                     ", ".join(f"**{_md(t['channel'])}** ({t['count']})" for t in summary["top_channels"]) + ".")
    lines.append("- Validate every borrowed claim with first-party data before publishing; public proxies are directional only.")
    lines.append("- Re-run this scan monthly to separate recurring cadence from one-off launches.")

    if summary["own_campaigns"]:
        lines += ["", f"## {_md(my_company)}'s own recent campaigns",
                  "Found with the same searches; used so recommendations don't repeat what you already do."]
        for c in summary["own_campaigns"]:
            lines.append(f"- {_campaign_link(c)} — {_md(c.get('message'))}")

    if summary["excluded"]:
        lines += ["", "## Excluded from analysis",
                  "Search returned these, but they are too old or not about the competitor:"]
        for c in summary["excluded"]:
            lines.append(f"- {_md(c['competitor'])}: {_campaign_link(c)} — {_md(c.get('excluded'))}")

    lines += ["", "## Method + Limitations"] + [f"- {_md(m)}" for m in summary["method"]]
    path.write_text("\n".join(lines))
    return path


def llm_synthesize(my_company: str, results: dict, base_md: str) -> str | None:
    """Optional OpenAI rewrite of the template report. Keeps citations and disclaimers."""
    if not os.getenv("OPENAI_API_KEY"):
        print("    ! OpenAI rewrite skipped: OPENAI_API_KEY is not set, keeping the template report.")
        return None
    try:
        from openai import OpenAI
    except ImportError:
        print("    ! OpenAI synthesis skipped (pip install openai).")
        return None

    payload = {
        name: [{
            "title": c.get("title"), "url": c.get("url"), "date": c.get("date"),
            "channel": c.get("channel"), "message": c.get("message"),
            "signal": c.get("signal"), "strength": c.get("strength"),
            "confidence": c.get("confidence"), "why": c.get("why"),
            "flags": c.get("flags", []),
            "proof": (c.get("proof") or "")[:600],
        } for c in camps]
        for name, camps in results.items()
    }
    prompt = (
        f"Rewrite this competitor marketing report for {my_company} as sharp, skimmable markdown. "
        "Keep every URL citation, keep the Summary, Per-competitor, Comparison, Recommendations, "
        "and Method + Limitations sections. Never invent revenue, spend, ROI, or unsourced facts. "
        "Every effectiveness claim must stay labeled as a public proxy, not ROI. "
        "Keep every ⚠ flag next to its campaign, keep the Excluded section, and never present "
        "a flagged number as fact.\n\n"
        "Return markdown only.\n\n"
        f"Facts:\n{json.dumps(payload, indent=2, default=str)[:24000]}\n\n"
        f"Current template report:\n{base_md[:12000]}"
    )
    try:
        client = OpenAI()
        resp = client.chat.completions.create(
            model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
        )
        text = (resp.choices[0].message.content or "").strip()
        return text or None
    except Exception as e:
        print(f"    ! OpenAI synthesis skipped ({e}).")
        return None


def parse_competitor(spec: str) -> dict:
    # Accept "Name,domain" or "Name=domain" or just "Name".
    for sep in (",", "=", ":"):
        if sep in spec:
            name, domain = spec.split(sep, 1)
            return {"name": name.strip(), "domain": domain.strip()}
    return {"name": spec.strip(), "domain": ""}


def prompt_company() -> dict:
    print("=== Competitor Marketing Scan ===")
    name = input("Your company name: ").strip()
    while not name:
        name = input("Company name (required): ").strip()
    domain = input("Your company domain [optional]: ").strip()
    description = input("One-line description [optional]: ").strip()
    return {"name": name, "domain": domain, "description": description}


def confirm_competitors(found: list) -> list:
    print("Discovered competitors:")
    for i, c in enumerate(found, 1):
        print(f"  {i}. {c['name']} ({c.get('domain','?')}) — {c.get('reason','')[:80]}")
    ans = input("Keep all? Enter numbers to DROP (e.g. 2,3) or press Enter to keep: ").strip()
    if not ans:
        return found
    drop = {p.strip() for p in ans.replace(",", " ").split()}
    kept = [c for i, c in enumerate(found, 1) if str(i) not in drop]
    return kept or found


def build_config(args) -> dict:
    # Priority: explicit --competitors > company info (discovery later) > yaml fallback.
    # Interactive (-i or TTY with defaults) asks for company only; the agent discovers rivals.
    import sys
    base = {"max_results_per_query": args.max_results,
            "max_queries_per_competitor": args.max_queries,
            "max_competitors": args.max_competitors, "grade": not args.no_grade,
            "fetch_proof": not args.no_proof, "max_proof_urls": args.max_proof_urls,
            "max_age_days": args.max_age_days, "scan_self": not args.no_self,
            "synthesis": args.synthesis}
    if args.company:
        if args.competitors:
            return {**base,
                    "my_company": {"name": args.company, "domain": args.domain or "",
                                   "description": args.description or ""},
                    "competitors": [parse_competitor(s) for s in args.competitors]}
        return {**base,
                "my_company": {"name": args.company, "domain": args.domain or "",
                               "description": args.description or ""},
                "competitors": []}  # discovered in main()
    if args.interactive or (args.input == "inputs.yaml" and sys.stdin.isatty()):
        me = prompt_company()
        return {**base, "my_company": me, "competitors": []}  # discovered in main()
    if yaml is None:
        raise SystemExit("pip install pyyaml (see requirements.txt)")
    cfg = yaml.safe_load(open(args.input))
    return {**base, **cfg}


def main():
    ap = argparse.ArgumentParser(description="Scan competitor marketing campaigns via Exa. Works for any company — pass it as input.")
    ap.add_argument("--input", default="inputs.yaml", help="yaml config fallback (used when --company is absent)")
    ap.add_argument("--company", help="your company name, e.g. --company Figma")
    ap.add_argument("--domain", default="", help="your company domain, e.g. --domain figma.com")
    ap.add_argument("--description", default="", help="one-line description of your company")
    ap.add_argument("--competitors", nargs="*", default=None,
                    help="competitors as 'Name,domain' pairs, e.g. --competitors 'Canva,canva.com' 'Miro,miro.com'")
    ap.add_argument("--max-results", type=int, default=10, help="results per query (default 10 = Exa default)")
    ap.add_argument("--max-queries", type=int, default=3, help="campaign queries per competitor (default 3)")
    ap.add_argument("--max-competitors", type=int, default=4, help="cap auto-discovered competitors (default 4)")
    ap.add_argument("--no-grade", action="store_true", help="skip grading, leave Signal/Strength TBD")
    ap.add_argument("--no-proof", action="store_true", help="skip Exa get_contents proof fetching")
    ap.add_argument("--max-proof-urls", type=int, default=5, help="campaign URLs to fetch proof for (default 5)")
    ap.add_argument("--no-self", action="store_true",
                    help="skip scanning your own company's campaigns (saves ~3 searches)")
    ap.add_argument("--max-age-days", type=int, default=365,
                    help="exclude campaigns published longer ago than this (default 365; 0 = keep all)")
    ap.add_argument("--synthesis", default="auto", choices=["auto", "template", "openai"],
                    help="report synthesis: auto (OpenAI if key), template ($0), or openai")
    ap.add_argument("-i", "--interactive", action="store_true", help="ask for company in the terminal (rivals auto-discovered)")
    ap.add_argument("--tracer", default="none", choices=["none", "phoenix", "langfuse", "both"])
    ap.add_argument("--dry-run", action="store_true", help="mock Exa data, no key needed")
    args = ap.parse_args()

    cfg = build_config(args)

    client = None
    if not args.dry_run:
        key = os.getenv("EXA_API_KEY")
        if not key:
            raise SystemExit("Set EXA_API_KEY or use --dry-run (free mock, no key). Get key: dashboard.exa.ai/api-keys")
        from exa_py import Exa
        client = Exa(api_key=key)

    import sys as _sys
    confirm_fn = None
    if _sys.stdin.isatty() and not args.dry_run:
        confirm_fn = confirm_competitors
    try:
        out = scan_company(cfg, tracer_backend=args.tracer, client=client,
                           dry=args.dry_run, confirm_fn=confirm_fn)
    except ExaStop as e:
        raise SystemExit(f"Scan stopped: {e}")
    print(f"\nReport: {out['report_path']}\nTrace: traces/{out['run_id']}.jsonl")


def scan_company(cfg: dict, tracer_backend: str = "none", client=None,
                 dry: bool = False, on_step=None, confirm_fn=None) -> dict:
    """Reusable scan pipeline (CLI, Streamlit, FastAPI all call this).

    on_step(event: str, payload: dict) is called after each major step for
    live progress (SSE in the web app). confirm_fn(found) may filter the
    discovered competitor list (confirm-then-scan); defaults to keep-all.
    Returns {"run_id", "results", "flat", "report_path"}.
    """
    tracer = Tracer(tracer_backend)
    flat: list = []
    with tracer.span("scan.run", {"tracer": tracer_backend, "dry_run": dry}):
        if not cfg.get("competitors"):
            me = cfg["my_company"]
            with tracer.span("exa.discover", {"company": me["name"]}) as s:
                try:
                    found = discover_competitors(client, me["name"], me.get("domain", ""),
                                                 me.get("description", ""),
                                                 max_n=cfg.get("max_competitors", 4), dry=dry)
                except Exception as e:
                    stop = exa_stop_reason(e)
                    if stop:
                        raise ExaStop(stop) from e
                    raise
                s.set("discovered", len(found))
                if on_step:
                    on_step("discovered", {"competitors": found})
            if confirm_fn:
                found = confirm_fn(found)
            cfg = {**cfg, "competitors": found}
            if not found:
                raise ValueError("No competitors discovered — pass competitors explicitly.")
        results, old = {}, {}
        total = len(cfg.get("competitors", []))
        for j, c in enumerate(cfg.get("competitors", [])):
            camps = run_competitor(tracer, c["name"], c.get("domain", ""), cfg, client, dry)
            # Old or wrong-company items leave the analysis but stay listed at the end of the report.
            results[c["name"]] = [x for x in camps if not x.get("excluded")]
            old[c["name"]] = [x for x in camps if x.get("excluded")]
            for camp in results[c["name"]]:
                flat.append({"competitor": c["name"], **camp})
            if on_step:
                on_step("competitor_done", {"name": c["name"], "done": j + 1, "total": total})
        recs, own = [], []
        if not dry and client is not None:
            me = cfg["my_company"]
            if cfg.get("scan_self", True):
                # Same searches + checks as a competitor, so recommendations can skip what we already do.
                if on_step:
                    on_step("self_scan", {"name": me["name"]})
                mine = run_competitor(tracer, me["name"], me.get("domain", ""),
                                      {**cfg, "fetch_proof": False}, client, dry)
                own = [x for x in mine if not x.get("excluded")]
            with tracer.span("exa.answer", {"purpose": "recommendations", "own_campaigns": len(own)}) as s:
                recs = exa_recommendations(client, me, results, own=own)
                s.set("recommendations", len(recs))
        path = write_report(cfg["my_company"]["name"], results, tracer.run_id, cfg=cfg, old=old, recs=recs,
                            own=own)
        write_report_data(cfg["my_company"], results, tracer.run_id,
                          report_summary(results, old, cfg, tracer.run_id, recs, own),
                          extra={"tracer": tracer_backend, "otel_trace_id": tracer.otel_trace_id,
                                 "mock": dry})
        synthesis = cfg.get("synthesis", "auto")
        if synthesis == "openai" or (synthesis == "auto" and os.getenv("OPENAI_API_KEY")):
            with tracer.span("synthesis", {"mode": "openai"}) as s:
                md = llm_synthesize(cfg["my_company"]["name"], results, path.read_text())
                if md:
                    path.write_text(md)
                    s.set("chars", len(md))
                else:
                    s.set("mode", "template-fallback")
        if on_step:
            on_step("report_ready", {"path": str(path)})
    tracer.flush()
    old_flat = [{"competitor": name, **c} for name, camps in old.items() for c in camps]
    return {"run_id": tracer.run_id, "tracer": tracer_backend, "otel_trace_id": tracer.otel_trace_id,
            "results": results, "flat": flat, "old": old_flat,
            "report_path": str(path), "report": path.read_text()}


if __name__ == "__main__":
    main()
