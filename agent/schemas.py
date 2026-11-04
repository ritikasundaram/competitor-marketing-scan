"""The forms each AI step fills in. LangChain turns these into tool calls, so answers
always come back in the same shape."""
from __future__ import annotations

from typing import List, Literal

from pydantic import BaseModel, Field

Dimension = Literal["launches", "campaigns", "channels", "partnerships", "positioning",
                    "pricing", "events", "customers"]


class CompanyProfile(BaseModel):
    """What the user's company is, from its own site."""
    summary: str = Field(description="One or two sentences on what the company sells and to whom.")
    market: str = Field(description="The market category in a few words, e.g. 'collaborative design tools'.")
    audiences: List[str] = Field(description="Main buyer or user groups.")
    products: List[str] = Field(description="Products and notable features the company already offers.")
    focus: str = Field(description="What the user should care about most in this scan, from their goal.")


class Rival(BaseModel):
    name: str
    domain: str = Field(description="The rival's main website domain, e.g. sketch.com. Never guess.")
    reason: str = Field(description="Why it competes directly, in one short sentence.")


class RivalList(BaseModel):
    rivals: List[Rival]


class AtomicQuestion(BaseModel):
    """One small question a researcher can answer from a few web pages."""
    company: str = Field(description="Exact company name this question is about.")
    dimension: Dimension
    question: str = Field(description="One specific, answerable question with a time window.")
    search_query: str = Field(description="A natural-language web search that would find the answer. "
                                          "Include the company's domain in brackets, e.g. 'Sketch (sketch.com)'.")


class QuestionPlan(BaseModel):
    questions: List[AtomicQuestion]


class Finding(BaseModel):
    claim: str = Field(description="One fact from a source, in your own words. Keep any numbers exactly as written.")
    source_id: str = Field(description="The id of the one source this fact comes from, e.g. Q3-2.")
    date: str = Field("", description="When it happened or was published (YYYY-MM-DD), if the source says.")
    confidence: Literal["High", "Medium", "Low"] = "Medium"


class ResearchAnswer(BaseModel):
    answer: str = Field(description="Two or three sentences answering the question, only from the findings.")
    findings: List[Finding] = Field(description="Each fact used, each with one source id. Empty if nothing relevant.")


class Judgment(BaseModel):
    """The researcher's check on its own answer."""
    answered: bool = Field(description="True if the findings actually answer the question.")
    missing: str = Field("", description="What is still missing, if anything.")
    next_query: str = Field("", description="A better web search to try next, if not answered.")
    answer: str = Field("", description="Two or three sentences answering the question using ONLY the "
                                        "findings listed, or empty if they say nothing useful.")


class FollowUps(BaseModel):
    """Gaps the reviewer wants filled with one more round of research."""
    gaps: str = Field(description="What important thing is still unknown, in a sentence or two.")
    questions: List[AtomicQuestion] = Field(description="Up to the allowed number of new questions. Empty if no gaps.")


class Campaign(BaseModel):
    name: str = Field(description="Short name for the campaign or move.")
    what: str = Field(description="What they did, one sentence.")
    channel: str = Field(description="Where it ran: press, social, email, webinar, events, partners, product, pricing.")
    strength: Literal["Strong", "Medium", "Weak"] = Field(description="Rated with the rubric.")
    why: str = Field(description="One sentence explaining the rating from the evidence.")
    source_ids: List[str] = Field(description="Source ids backing this campaign.")


class RivalProfile(BaseModel):
    summary: str = Field(description="Two sentences: what this rival is pushing right now and to whom.")
    positioning: str = Field(description="How they describe themselves vs. the user's company, one sentence.")
    campaigns: List[Campaign]
    threat: Literal["High", "Medium", "Low"] = Field(description="How much this rival's recent moves threaten the user's company.")
    threat_why: str = Field(description="One sentence on why.")


class Action(BaseModel):
    title: str = Field(description="Imperative, under 10 words.")
    what: str = Field(description="What to do, specifically, in one or two sentences.")
    why: str = Field(description="Why now, tied to the cited rival moves.")
    first_step: str = Field(description="The first concrete step someone could take this week.")
    channel: str
    effort: Literal["Low", "Medium", "High"]
    impact: Literal["Low", "Medium", "High"]
    horizon: Literal["This month", "This quarter", "This half"]
    source_ids: List[str] = Field(description="Source ids of the rival moves this responds to (at least one).")


class ActionPlan(BaseModel):
    headline: str = Field(description="The single most important takeaway, one sentence.")
    actions: List[Action]
