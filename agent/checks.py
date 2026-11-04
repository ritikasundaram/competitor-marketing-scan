"""Fact checks. Plain rules, no AI: dates, wrong-company matches, numbers, citations, rubric.

These run as GUARDRAIL spans in the graph. Nothing is silently rewritten: findings
that fail are dropped with a reason, and doubtful ones are flagged.
"""
from __future__ import annotations

import datetime
import re
from urllib.parse import urlparse

_URL_DATE = re.compile(r"(?<!\d)(20\d{2})[/_-]?(0[1-9]|1[0-2])(?:[/_-]?(0[1-9]|[12]\d|3[01]))?")
_NUM = re.compile(r"\d+(?:[.,]\d+)*")
_WORDS = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
          "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen",
          "eighteen", "nineteen", "twenty"]
_YEAR = re.compile(r"^(19|20)\d{2}$")


def norm_url(url: str) -> str:
    return (url or "").split("#")[0].split("?")[0].rstrip("/").lower()


def host(url: str) -> str:
    try:
        h = (urlparse(url or "").hostname or "").lower()
    except ValueError:
        return ""
    return h[4:] if h.startswith("www.") else h


def parse_date(value) -> datetime.date | None:
    try:
        return datetime.date.fromisoformat(str(value or "")[:10])
    except ValueError:
        return None


def date_from_url(url: str) -> datetime.date | None:
    """Dates baked into URLs, e.g. /2023/06/29/ or businesswire's /20230629787067/."""
    for m in _URL_DATE.finditer(url or ""):
        try:
            return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3) or 1))
        except ValueError:
            continue
    return None


def best_date(page_date, url: str, today: datetime.date) -> datetime.date | None:
    """The later of the page's date and a date in its URL, ignoring future dates."""
    dates = [d for d in (parse_date(page_date), date_from_url(url)) if d and d <= today]
    return max(dates) if dates else None


def _number_in_evidence(num: str, evidence: str, suffix: str = "") -> bool:
    core = num.replace(",", "")
    forms = {core}
    try:
        value = float(core)
        if value.is_integer() and 0 <= value <= 20:
            forms.add(_WORDS[int(value)])
        mult = {"k": 1_000, "m": 1_000_000}.get(suffix)
        if mult:
            forms.add(str(int(round(value * mult))))
    except ValueError:
        pass
    for f in forms:
        pat = rf"\b{re.escape(f)}\b" if f.isalpha() else rf"(?<![\d.]){re.escape(f)}(?![\d])"
        if re.search(pat, evidence):
            return True
    return False


def unverified_numbers(claim: str, evidence: str) -> list:
    """Numbers in a model-written claim that never appear in the source text.

    Years (2026) are skipped: they describe when, not how much.
    """
    ev = re.sub(r"(?<=\d),(?=\d)", "", (evidence or "").lower())
    missing = []
    for m in _NUM.finditer(claim or ""):
        tail = (claim[m.end():m.end() + 2] or "").strip().lower()
        suffix = tail[:1] if tail[:1] in ("k", "m") and not tail[1:2].isalpha() else ""
        if _YEAR.match(m.group(0)) and not suffix and tail[:1] != "%":
            continue
        shown = m.group(0) + ("%" if tail[:1] == "%" else suffix.upper())
        if not _number_in_evidence(m.group(0), ev, suffix) and shown not in missing:
            missing.append(shown)
    return missing


def on_own_site(url: str, domain: str) -> bool:
    dom = (domain or "").lower().removeprefix("www.").strip("/")
    h = host(url)
    return bool(dom) and (h == dom or h.endswith("." + dom))


def mentions(text: str, name: str, domain: str = "") -> bool:
    t = (text or "").lower()
    dom = (domain or "").lower().removeprefix("www.")
    return bool(re.search(rf"\b{re.escape(name.lower())}\b", t)) or bool(dom and dom in t)


def check_findings(findings: list, sources: dict, company: dict, max_age_days: int,
                   today: datetime.date | None = None) -> tuple[list, list]:
    """Split a researcher's findings into (kept, dropped).

    Each finding cites one source id. Rules, in order:
    1. the source id must exist (no made-up citations)
    2. the source must be about this company: on its own site, or naming it
    3. older than max_age_days -> dropped; no date at all -> kept, flagged
    4. numbers in the claim must appear in the source text -> otherwise flagged, confidence Low
    """
    today = today or datetime.date.today()
    kept, dropped = [], []
    name, domain = company.get("name", ""), company.get("domain", "")
    for f in findings:
        f = dict(f)
        f["flags"] = []
        src = sources.get(f.get("source_id", ""))
        if not src:
            dropped.append({**f, "reason": f"cites {f.get('source_id') or 'nothing'}, which isn't a source it read"})
            continue
        text = f"{src.get('title', '')} {src.get('text', '')}"
        if not on_own_site(src["url"], domain) and not mentions(text, name, domain):
            dropped.append({**f, "reason": f"source doesn't mention {name}"})
            continue
        d = best_date(f.get("date") or src.get("date"), src["url"], today)
        if d:
            f["date"] = d.isoformat()
            age = (today - d).days
            if max_age_days > 0 and age > max_age_days:
                dropped.append({**f, "reason": f"old: published {d.isoformat()} ({age // 30} months ago)"})
                continue
        else:
            f["date"] = ""
            f["flags"].append("date unknown")
        missing = unverified_numbers(f.get("claim", ""), text)
        if missing:
            f["flags"].append("numbers not found in source: " + ", ".join(missing))
            f["confidence"] = "Low"
        kept.append(f)
    return kept, dropped


# ---------- the strength rubric ----------

RUBRIC = (
    "Strength rubric (use exactly one):\n"
    "- Strong: covered by at least two independent outside sources (press, analysts, partners), "
    "or a concrete result stated in a source (signups, attendance, ranking, reach).\n"
    "- Medium: one outside source, or clear engagement on the company's own channels.\n"
    "- Weak: only the company's own announcement, with no sign of pickup."
)


def rubric_cap(strength: str, source_ids: list, sources: dict, company_domain: str,
               verified_number: bool) -> tuple[str, str]:
    """Hold the model's rating to the rubric, using the cited sources.

    Returns (strength, note). The model can rate lower than the evidence allows,
    never higher.
    """
    s = (strength or "").strip().capitalize()
    if s not in ("Strong", "Medium", "Weak"):
        s = "Weak"
    cited = [sources[i] for i in source_ids if i in sources]
    outside = {host(c["url"]) for c in cited if not on_own_site(c["url"], company_domain)}
    if len(outside) >= 2 or verified_number:
        ceiling = "Strong"
    elif len(outside) == 1:
        ceiling = "Medium"
    else:
        ceiling = "Medium" if cited else "Weak"
    order = {"Weak": 0, "Medium": 1, "Strong": 2}
    if order[s] > order[ceiling]:
        why = ("only one outside source" if ceiling == "Medium" and outside
               else "only the company's own pages" if ceiling == "Medium" else "no valid sources")
        return ceiling, f"lowered from {s}: {why}"
    return s, ""


def clean_ids(ids) -> list:
    """'C3,' / ' q2-1 ' -> 'C3' / 'Q2-1'. Models sometimes add punctuation."""
    out = []
    for i in ids or []:
        for part in re.split(r"[,\s;]+", str(i)):
            part = part.strip(".()[]").upper()
            if part and part not in out:
                out.append(part)
    return out
