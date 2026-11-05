"""Phoenix tracing with OpenInference.

- LangChainInstrumentor turns every LangGraph step into a CHAIN span and every
  model call into an LLM span (prompt, output, tokens).
- Exa calls and checks are traced by hand with the right span kind:
  RETRIEVER (search), TOOL (page fetch), GUARDRAIL (fact checks),
  EVALUATOR (self-checks), AGENT (the whole run and each researcher).
- All spans of one scan share a session id (the run id), so a scan that
  pauses for your approval and resumes still groups together in Phoenix.
"""
from __future__ import annotations

import json
import os
from contextlib import contextmanager

from openinference.instrumentation import get_attributes_from_context, using_attributes
from openinference.semconv.trace import (DocumentAttributes, OpenInferenceSpanKindValues,
                                         SpanAttributes)
from opentelemetry import trace as otel_trace
from opentelemetry.trace import Status, StatusCode

KIND = OpenInferenceSpanKindValues
_tracer = otel_trace.get_tracer("competitor-agent")
_setup_done = False


def setup_phoenix(endpoint: str | None = None, project: str | None = None) -> bool:
    """Send traces to Phoenix. Safe to call more than once. Returns False if it couldn't."""
    global _setup_done, _tracer
    if _setup_done:
        return True
    try:
        from openinference.instrumentation.langchain import LangChainInstrumentor
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError as e:
        print(f"[trace] Phoenix packages missing ({e}); tracing off.")
        return False
    project = project or os.getenv("PHOENIX_PROJECT_NAME", "competitor-agent")
    endpoint = endpoint or os.getenv("PHOENIX_COLLECTOR_ENDPOINT", "http://localhost:6006/v1/traces")
    provider = TracerProvider(resource=Resource.create(
        {"service.name": project, "openinference.project.name": project}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    otel_trace.set_tracer_provider(provider)
    LangChainInstrumentor().instrument(tracer_provider=provider)
    _tracer = otel_trace.get_tracer("competitor-agent")
    _setup_done = True
    print(f"[trace] Phoenix -> {endpoint} (project {project})")
    return True


def use_provider(provider) -> None:
    """Tests: send spans to an in-memory provider instead of Phoenix."""
    global _tracer, _setup_done
    from openinference.instrumentation.langchain import LangChainInstrumentor
    inst = LangChainInstrumentor()
    if inst.is_instrumented_by_opentelemetry:
        inst.uninstrument()
    inst.instrument(tracer_provider=provider)
    _tracer = provider.get_tracer("competitor-agent")
    _setup_done = True


def flush() -> None:
    provider = otel_trace.get_tracer_provider()
    if hasattr(provider, "force_flush"):
        try:
            provider.force_flush(timeout_millis=5000)
        except Exception:  # noqa: BLE001
            pass


def _text(value) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, default=str)
    except TypeError:
        return str(value)


class SpanHandle:
    def __init__(self, span):
        self.span = span

    def set(self, key: str, value) -> None:
        if isinstance(value, (str, bool, int, float)):
            self.span.set_attribute(key, value)
        else:
            self.span.set_attribute(key, _text(value)[:4000])

    def output(self, value) -> None:
        is_json = not isinstance(value, str)
        self.span.set_attribute(SpanAttributes.OUTPUT_VALUE, _text(value)[:8000])
        self.span.set_attribute(SpanAttributes.OUTPUT_MIME_TYPE, "application/json" if is_json else "text/plain")

    def documents(self, docs: list) -> None:
        """RETRIEVER spans: each search result as a retrieved document Phoenix can show."""
        for i, d in enumerate(docs):
            p = f"{SpanAttributes.RETRIEVAL_DOCUMENTS}.{i}."
            self.span.set_attribute(p + DocumentAttributes.DOCUMENT_ID, d.get("id", d.get("url", "")))
            self.span.set_attribute(p + DocumentAttributes.DOCUMENT_CONTENT,
                                    (d.get("text") or d.get("snippet") or "")[:2000])
            if d.get("score") is not None:
                self.span.set_attribute(p + DocumentAttributes.DOCUMENT_SCORE, float(d["score"]))
            self.span.set_attribute(p + DocumentAttributes.DOCUMENT_METADATA, _text(
                {"url": d.get("url"), "title": d.get("title"), "date": d.get("date")}))

    def trace_id(self) -> str:
        return format(self.span.get_span_context().trace_id, "032x")


def _parent_context():
    """Nest under the LangGraph step that's running (LangChain doesn't set the OTel context itself)."""
    if not _setup_done:
        return None
    try:
        from openinference.instrumentation.langchain import get_current_span
        parent = get_current_span()
    except Exception:  # noqa: BLE001
        parent = None
    return otel_trace.set_span_in_context(parent) if parent is not None else None


@contextmanager
def span(name: str, kind: OpenInferenceSpanKindValues, input=None, **attrs):
    """A hand-made OpenInference span. Nests under whatever span is current."""
    with _tracer.start_as_current_span(name, context=_parent_context()) as s:
        s.set_attribute(SpanAttributes.OPENINFERENCE_SPAN_KIND, kind.value)
        for k, v in get_attributes_from_context():  # session.id, metadata, tags
            s.set_attribute(k, v)
        if input is not None:
            s.set_attribute(SpanAttributes.INPUT_VALUE, _text(input)[:8000])
            s.set_attribute(SpanAttributes.INPUT_MIME_TYPE,
                            "text/plain" if isinstance(input, str) else "application/json")
        h = SpanHandle(s)
        for k, v in attrs.items():
            h.set(k, v)
        try:
            yield h
        except Exception as e:
            s.record_exception(e)
            s.set_status(Status(StatusCode.ERROR, str(e)[:200]))
            raise
        else:
            s.set_status(Status(StatusCode.OK))


def session(run_id: str, company: str, mock: bool):
    """Tag every span inside (auto and hand-made) with the run's session id and metadata."""
    return using_attributes(session_id=run_id, metadata={"run_id": run_id, "company": company, "mock": mock},
                            tags=["mock" if mock else "live"])
