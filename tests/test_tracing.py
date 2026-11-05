"""Phoenix instrumentation: span kinds, nesting and sessions, captured in memory."""
import collections
import unittest

from helpers import TempReports, run_all
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from agent import tracing

KIND = "openinference.span.kind"


class TracingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(cls.exporter))
        tracing.use_provider(provider)
        with TempReports():
            _, cls.run_id = run_all(settings={"max_rivals": 1, "questions_per_rival": 2, "own_questions": 1})
        cls.spans = cls.exporter.get_finished_spans()
        cls.by_id = {s.context.span_id: s for s in cls.spans}

    def parent(self, s):
        return self.by_id.get(s.parent.span_id) if s.parent else None

    def named(self, prefix):
        return [s for s in self.spans if s.name.startswith(prefix)]

    def test_every_span_has_a_known_kind(self):
        kinds = collections.Counter(s.attributes.get(KIND) for s in self.spans)
        self.assertNotIn(None, kinds)
        self.assertNotIn("UNKNOWN", kinds)
        for k in ("AGENT", "CHAIN", "LLM", "RETRIEVER", "TOOL", "GUARDRAIL", "EVALUATOR"):
            self.assertIn(k, kinds)

    def test_every_span_is_in_the_session(self):
        self.assertTrue(all(s.attributes.get("session.id") == self.run_id for s in self.spans))

    def test_one_root_agent_span_per_segment(self):
        roots = [s for s in self.spans if s.parent is None]
        self.assertEqual(sorted(s.name for s in roots), ["competitor_scan (resume)", "competitor_scan (start)"])
        self.assertTrue(all(s.attributes[KIND] == "AGENT" for s in roots))

    def test_researchers_are_agents_holding_their_steps(self):
        researchers = self.named("researcher_agent Q")
        self.assertTrue(researchers)
        self.assertTrue(all(s.attributes[KIND] == "AGENT" for s in researchers))
        for s in self.named("check.findings"):
            self.assertEqual(s.attributes[KIND], "GUARDRAIL")
            self.assertEqual(self.parent(self.parent(s)).attributes[KIND], "AGENT")

    def test_exa_spans_nest_under_the_step_that_made_them(self):
        for s in self.named("exa.search"):
            self.assertEqual(s.attributes[KIND], "RETRIEVER")
            self.assertIn(self.parent(s).name, ("understand", "search"))
            self.assertIn("retrieval.documents.0.document.id", s.attributes)
        for s in self.named("exa.read"):
            self.assertEqual(s.attributes[KIND], "TOOL")
            self.assertEqual(s.attributes["tool.name"], "exa.get_contents")
        self.assertEqual(self.parent(self.named("exa.discover")[0]).name, "find_rivals")

    def test_llm_spans_have_model_and_tokens(self):
        llms = [s for s in self.spans if s.attributes.get(KIND) == "LLM"]
        self.assertTrue(llms)
        for s in llms:
            self.assertIn("llm.model_name", s.attributes)
            self.assertGreater(s.attributes.get("llm.token_count.total", 0), 0)

    def test_judge_spans_are_evaluators_with_a_label(self):
        judges = self.named("judge.answer")
        self.assertTrue(judges)
        self.assertTrue(all(s.attributes[KIND] == "EVALUATOR" and "eval.label" in s.attributes for s in judges))


if __name__ == "__main__":
    unittest.main()
