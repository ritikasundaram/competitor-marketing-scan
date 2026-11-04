"""The AI models, through OpenCode's inference gateway (Anthropic-style endpoint).

Two roles:
- planner: judgment-heavy steps (understand, break down, review gaps, profile, actions)
- researcher: runs many times per scan (answer a question, check its own answer)

Mock mode swaps in MockChatModel: a real LangChain chat model that returns canned,
schema-valid answers, so the graph, tracing (LLM spans with token counts) and the
dashboard all behave as in a live run, for free.
"""
from __future__ import annotations

import json
import os
from typing import Any, Callable, List, Optional

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.utils.function_calling import convert_to_openai_tool

DEFAULT_BASE_URL = "https://opencode.ai/inference/anthropic"
DEFAULT_MODELS = {"planner": "claude-sonnet-5-5", "researcher": "claude-haiku-5-5"}


class LLMStop(RuntimeError):
    """A model error that makes the rest of the scan pointless (bad key, no credit)."""


def llm_stop_reason(err: Exception) -> Optional[str]:
    msg = str(err)
    low = msg.lower()
    if "401" in msg or "invalid credential" in low or "authentication" in low:
        return "OpenCode rejected the API key. Check OPENCODE_API_KEY in .env."
    if "402" in msg or "insufficient" in low or "credit" in low or "balance" in low:
        return "OpenCode account is out of credits. Top up in the OpenCode console."
    if "429" in msg or "rate limit" in low:
        return "OpenCode rate limit reached. Wait a bit and try again."
    return None


def model_name(role: str, settings: dict) -> str:
    return (settings.get("models") or {}).get(role) or os.getenv(
        f"{role.upper()}_MODEL", DEFAULT_MODELS[role])


def get_model(role: str, settings: dict, mock_responder: Optional[Callable] = None) -> BaseChatModel:
    if settings.get("mock"):
        return MockChatModel(responder=mock_responder or (lambda name, msgs: {}),
                             model=f"mock-{model_name(role, settings)}")
    key = os.getenv("OPENCODE_API_KEY")
    if not key:
        raise LLMStop("OPENCODE_API_KEY is not set. Add it to .env, or tick Mock mode.")
    from langchain_anthropic import ChatAnthropic
    return ChatAnthropic(
        model=model_name(role, settings),
        api_key=key,
        base_url=os.getenv("OPENCODE_BASE_URL", DEFAULT_BASE_URL),
        max_tokens=4096 if role == "planner" else 2048,
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
