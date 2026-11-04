"""The AI models: Claude Haiku 5.5 (Anthropic API), Gemini, or Claude via OpenCode's gateway.

Two roles:
- planner: judgment-heavy steps (understand, break down, review gaps, profile, actions)
- researcher: runs many times per scan (answer a question, check its own answer)

Provider: AI_PROVIDER=anthropic|gemini|opencode, else whichever key is set
(ANTHROPIC_API_KEY first, then GEMINI_API_KEY, then OPENCODE_API_KEY).
Gemini calls share one rate limiter (GEMINI_RPM, default 10 a minute) so parallel
researchers stay under free-tier limits; busy/quota errors are retried in graph.ask.

Mock mode swaps in MockChatModel: a real LangChain chat model that returns canned,
schema-valid answers, so the graph, tracing (LLM spans with token counts) and the
dashboard all behave as in a live run, for free.
"""
from __future__ import annotations

import json
import os
import threading
from typing import Any, Callable, List, Optional

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.utils.function_calling import convert_to_openai_tool

OPENCODE_BASE_URL = "https://opencode.ai/inference/anthropic"
# Each role tries its models in order. Gemini's free tier caps requests per model per day
# (as low as 20 for some Flash models), so when one runs out the next takes over.
DEFAULT_MODELS = {
    "gemini": {"planner": ["gemini-3.6-flash", "gemini-3.5-flash", "gemini-3.8-flash", "gemini-3.7-flash",
                           "gemini-3.5-flash-lite", "gemini-flash-lite-latest"],
               "researcher": ["gemini-3.5-flash-lite", "gemini-flash-lite-latest", "gemini-3.1-flash-lite"]},
    "anthropic": {"planner": ["claude-haiku-5-5"], "researcher": ["claude-haiku-5-5"]},
    "opencode": {"planner": ["claude-sonnet-5-5"], "researcher": ["claude-haiku-5-5"]},
    "mock": {"planner": ["planner"], "researcher": ["researcher"]},
}
KEYS = {"anthropic": "ANTHROPIC_API_KEY", "gemini": "GEMINI_API_KEY", "opencode": "OPENCODE_API_KEY"}
NAMES = {"anthropic": "Claude", "gemini": "Gemini", "opencode": "OpenCode"}


class LLMStop(RuntimeError):
    """A model error that makes the rest of the scan pointless (bad key, no credit, quota)."""


def provider() -> str:
    chosen = os.getenv("AI_PROVIDER", "").strip().lower()
    if chosen in KEYS:
        return chosen
    if os.getenv("ANTHROPIC_API_KEY"):
        return "anthropic"
    if os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY"):
        return "gemini"
    if os.getenv("OPENCODE_API_KEY"):
        return "opencode"
    return "gemini"


def gemini_key() -> str:
    return os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or ""


def has_key() -> bool:
    prov = provider()
    return bool(gemini_key()) if prov == "gemini" else bool(os.getenv(KEYS[prov]))


def is_daily_quota(err: Exception) -> bool:
    """A per-day limit for this model: waiting won't help today, another model might."""
    return "perday" in str(err).lower().replace("_", "").replace(" ", "")


def is_stuck(err: Exception) -> bool:
    """Timed out: the model is overloaded, so retrying the same one wastes minutes."""
    low = str(err).lower()
    return "504" in low or "deadline" in low or "timed out" in low or "timeout" in low


def is_transient(err: Exception) -> bool:
    """Worth waiting and retrying: busy servers, per-minute rate limits, timeouts."""
    low = str(err).lower()
    return any(s in low for s in ("503", "529", "unavailable", "high demand", "overloaded", "429",
                                  "resource_exhausted", "rate limit", "quota", "deadline", "timeout",
                                  "500 internal", "502"))


def llm_stop_reason(err: Exception, transient_exhausted: bool = False) -> Optional[str]:
    msg = str(err)
    low = msg.lower()
    if "api key not valid" in low or "api_key_invalid" in low or "permission_denied" in low:
        return "Gemini rejected the API key. Check GEMINI_API_KEY in .env (aistudio.google.com/apikey)."
    if "401" in msg or "invalid credential" in low or "authentication" in low or "invalid x-api-key" in low:
        prov = provider()
        return f"{NAMES.get(prov, 'The AI provider')} rejected the API key. Check {KEYS.get(prov, 'the key')} in .env."
    if "prepayment" in low or ("402" in msg and "ai studio" in low):
        return ("Gemini's prepaid credits are used up for this API key's project. Top up in Google AI "
                "Studio (Billing), or use a key from a project with credit.")
    if "credit balance is too low" in low or (provider() == "anthropic" and "credit" in low):
        return "Your Anthropic credit balance is too low. Add credits at console.anthropic.com (Billing)."
    if "402" in msg or "insufficient" in low or "credit" in low or "balance" in low:
        return "OpenCode account is out of credits. Top up in the OpenCode console."
    if ("not found" in low or "not_found" in low) and "model" in low:
        return f"That AI model isn't available to this key: {msg[:160]}"
    if transient_exhausted:
        if "quota" in low or "429" in msg or "resource_exhausted" in low:
            return ("The AI's quota or rate limit was reached. Wait a few minutes, or use fewer questions"
                    + (" (Gemini's free tier has per-minute and per-day limits; lower GEMINI_RPM)."
                       if provider() == "gemini" else "."))
        return "The AI service is overloaded right now. Try again in a few minutes."
    return None


def model_chain(role: str, settings: dict) -> List[str]:
    """Models to try for a role, in order. A setting or env var (comma-separated) replaces the default."""
    prov = "mock" if settings.get("mock") else provider()
    chosen = (settings.get("models") or {}).get(role) or (
        os.getenv(f"{role.upper()}_MODEL") if prov != "mock" else None)
    if chosen:
        return [m.strip() for m in str(chosen).split(",") if m.strip()]
    return list(DEFAULT_MODELS[prov][role])


def model_name(role: str, settings: dict) -> str:
    """The first model for a role that hasn't hit its daily limit (or the first, if all have)."""
    chain = model_chain(role, settings)
    return next((m for m in chain if not exhausted(m)), chain[0])


_exhausted: dict = {}  # model -> time its daily limit should reset
_exhausted_lock = threading.Lock()


def mark_exhausted(model: str, err: Exception) -> None:
    import re
    import time
    m = re.search(r"retry in (?:(\d+)h)?(?:(\d+)m)?([\d.]+)s", str(err))
    wait = (int(m.group(1) or 0) * 3600 + int(m.group(2) or 0) * 60 + float(m.group(3))) if m else 3600
    with _exhausted_lock:
        _exhausted[model] = time.time() + wait


def exhausted(model: str) -> bool:
    import time
    with _exhausted_lock:
        until = _exhausted.get(model)
        if until and until < time.time():
            _exhausted.pop(model, None)
            return False
        return bool(until)


_limiter = None
_limiter_lock = threading.Lock()


def _gemini_limiter():
    """One limiter shared by every Gemini call in this process."""
    global _limiter
    with _limiter_lock:
        if _limiter is None:
            from langchain_core.rate_limiters import InMemoryRateLimiter
            rpm = max(1.0, float(os.getenv("GEMINI_RPM", "10")))
            _limiter = InMemoryRateLimiter(requests_per_second=rpm / 60, check_every_n_seconds=0.2,
                                           max_bucket_size=2)
        return _limiter


def get_model(role: str, settings: dict, mock_responder: Optional[Callable] = None,
              model: Optional[str] = None) -> BaseChatModel:
    model = model or model_name(role, settings)
    if settings.get("mock"):
        return MockChatModel(responder=mock_responder or (lambda name, msgs: {}), model=f"mock-{model}")
    prov = provider()
    if prov == "gemini":
        if not gemini_key():
            raise LLMStop("GEMINI_API_KEY is not set. Add it to .env, or tick Mock mode.")
        from langchain_google_genai import ChatGoogleGenerativeAI
        return ChatGoogleGenerativeAI(
            model=model,
            google_api_key=gemini_key(),
            temperature=0.2,
            max_output_tokens=8192 if role == "planner" else 4096,
            timeout=60,  # a model that slow is overloaded; graph.ask moves to the next one
            max_retries=1,  # graph.ask does the patient retrying
            rate_limiter=_gemini_limiter(),
        )
    key = os.getenv(KEYS[prov])
    if not key:
        raise LLMStop(f"{KEYS[prov]} is not set. Add it to .env, or tick Mock mode.")
    from langchain_anthropic import ChatAnthropic
    extra = {} if prov == "anthropic" else {"base_url": os.getenv("OPENCODE_BASE_URL", OPENCODE_BASE_URL)}
    return ChatAnthropic(
        model=model,
        api_key=key,
        **extra,
        max_tokens=8192 if role == "planner" else 4096,
        temperature=0.2,
        timeout=120,
        max_retries=2,
    )


class MockChatModel(BaseChatModel):
    """Answers structured-output calls with responder(schema_name, messages) -> dict."""

    responder: Any
    model: str = "mock"

    @property
    def _llm_type(self) -> str:
        return "mock-chat"

    @property
    def _identifying_params(self) -> dict:
        return {"model_name": self.model}

    def bind_tools(self, tools, tool_choice=None, **kwargs):
        return self.bind(tools=[convert_to_openai_tool(t) for t in tools], **kwargs)

    def _generate(self, messages: List[BaseMessage], stop=None,
                  run_manager: Optional[CallbackManagerForLLMRun] = None, **kwargs) -> ChatResult:
        tools = kwargs.get("tools") or []
        name = tools[0]["function"]["name"] if tools else ""
        args = self.responder(name, messages) if name else {}
        prompt_chars = sum(len(str(m.content)) for m in messages)
        out_chars = len(json.dumps(args))
        usage = {"input_tokens": prompt_chars // 4, "output_tokens": out_chars // 4,
                 "total_tokens": prompt_chars // 4 + out_chars // 4}
        msg = AIMessage(content="" if name else "ok",
                        tool_calls=[{"name": name, "args": args, "id": "mock-call"}] if name else [],
                        usage_metadata=usage)
        return ChatResult(generations=[ChatGeneration(message=msg)],
                          llm_output={"model_name": self.model, "token_usage": usage})
