"""The whole agent, end to end, with the mock web and mock AI (free, offline)."""
import json
import os
import unittest
from unittest import mock as umock

from helpers import FIGMA, TempReports, run_all
from agent import exa, graph
from agent.run import new_run_id, run_segment


class FullRunTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = TempReports().__enter__()
        cls.events, cls.run_id = run_all()
        cls.done = cls.events[-1]
        cls.data = json.loads((cls.tmp.path / cls.done["report"]["file"]).read_text())

    @classmethod
    def tearDownClass(cls):
        cls.tmp.__exit__(None, None, None)

    def test_pauses_once_to_confirm_rivals_then_finishes(self):
        kinds = [e.get("kind") for e in self.events if e["event"] == "interrupt"]
        self.assertEqual(kinds, ["confirm_rivals"])
        self.assertEqual(self.done["event"], "done")

    def test_questions_are_atomic_and_capped(self):
        qs = [a["question"] for a in self.data["questions"]]
        first = [q for q in qs if q["round"] == 1]
        self.assertEqual(len(first), 2 * 4 + 2)  # 2 rivals x 4 + 2 about Figma
        self.assertEqual(sum(q["is_self"] for q in first), 2)
        self.assertTrue(all(q["domain"] for q in qs))
        self.assertEqual(len({q["id"] for q in qs}), len(qs))

    def test_researchers_retry_when_nothing_is_found(self):
        pricing = [a for a in self.data["questions"] if a["question"]["dimension"] == "pricing"]
        self.assertTrue(pricing and all(a["attempts"] == 2 for a in pricing))
        self.assertTrue(all(len(a["queries"]) == 2 for a in pricing))
        self.assertTrue(any(e["event"] == "question" and e["status"] == "retrying" for e in self.events))

    def test_retry_searches_still_name_the_website(self):
        for a in self.data["questions"]:
            for q in a["queries"]:
                self.assertIn(a["question"]["domain"], q)

    def test_gap_review_adds_a_follow_up_round(self):
        self.assertEqual(self.data["stats"]["follow_ups"], 1)
        self.assertEqual(self.data["reviews"][0]["added"], 1)

    def test_checks_catch_the_planted_mistakes(self):
        dropped = self.data["stats"]["dropped"]
        self.assertGreaterEqual(dropped.get("made-up citation", 0), 1)
        self.assertGreaterEqual(dropped.get("wrong company", 0), 1)
        self.assertGreaterEqual(dropped.get("too old", 0), 1)
        self.assertGreaterEqual(self.data["stats"]["flagged_numbers"], 1)

    def test_answers_are_rewritten_when_checks_removed_something(self):
        q1 = next(a for a in self.data["questions"] if a["question"]["id"] == "Q1")
        removed = [d["claim"] for d in self.data["dropped"] if d["question_id"] == "Q1"]
        self.assertTrue(removed)
        self.assertNotIn("billboard", q1["answer"])
        self.assertTrue(q1["answer"])

    def test_every_kept_finding_cites_a_saved_source(self):
        for a in self.data["questions"]:
            for f in a["findings"]:
                self.assertIn(f["source_id"], self.data["sources"])

    def test_rubric_lowers_ratings_the_sources_cant_back(self):
        self.assertGreaterEqual(self.data["stats"]["lowered_by_rubric"], 1)
        for p in self.data["profiles"]:
            for c in p["campaigns"]:
                self.assertIn(c["strength"], ("Strong", "Medium", "Weak"))

    def test_actions_cite_rival_sources_and_uncited_ones_are_dropped(self):
        plan = self.data["plan"]
        self.assertTrue(plan["actions"])
        rival_ids = {i for p in self.data["profiles"] for c in p["campaigns"] for i in c["source_ids"]}
        for a in plan["actions"]:
            self.assertTrue(set(a["source_ids"]) <= rival_ids and a["source_ids"])
        self.assertEqual([d["title"] for d in plan["dropped"]], ["Launch a podcast"])

    def test_report_files_are_written(self):
        stem = self.done["report"]["file"][:-5]
        self.assertTrue((self.tmp.path / f"{stem}.md").exists())
        self.assertEqual(self.data["version"], 2)
        self.assertEqual(self.data["trace"]["session_id"], self.run_id)


class FollowUpTests(unittest.TestCase):
    def test_follow_ups_repeating_a_failed_question_are_skipped(self):
        from agent.schemas import AtomicQuestion, FollowUps
        failed = {"id": "Q1", "company": "Axure", "domain": "axure.com", "is_self": False, "dimension": "pricing",
                  "question": "What pricing or enterprise tier changes did Axure introduce this year?",
                  "search_query": "x", "round": 1}
        state = {"settings": {**graph.DEFAULTS, "mock": True}, "round": 1, "questions": [failed],
                 "company": {"name": "Figma", "domain": "figma.com"}, "profile": {"focus": "x"},
                 "rivals": [{"name": "Axure", "domain": "axure.com"}],
                 "answers": [{"question": failed, "status": "unanswered", "findings": []}]}
        reply = FollowUps(gaps="pricing", questions=[
            AtomicQuestion(company="Axure", dimension="pricing", search_query="a",
                           question="What new enterprise pricing tiers did Axure introduce this year?"),
            AtomicQuestion(company="Axure", dimension="customers", search_query="b",
                           question="Which customer stories has Axure published?")])
        with umock.patch.object(graph, "ask", lambda *a, **k: reply):
            out = graph.review_gaps(state)
        self.assertEqual([q["question"] for q in out["questions"][1:]], ["Which customer stories has Axure published?"])


class PauseTests(unittest.TestCase):
    def test_unticked_rivals_are_not_researched(self):
        with TempReports():
            events, _ = run_all(answers={"confirm_rivals": lambda e: e["rivals"][:1]})
        self.assertEqual(events[-1]["event"], "done")
        profiled = {e["name"] for e in events if e["event"] == "rival"}
        self.assertEqual(len(profiled), 1)
        self.assertEqual(events[-1]["report"]["stats"]["questions"], 4 + 2 + 1)  # 1 rival + Figma + follow-up

    def test_question_approval_pauses_and_respects_removals(self):
        seen = {}

        def edit(e):
            qs = [dict(q) for q in e["questions"]]
            seen["removed"] = [q["question"] for q in qs[:2]]
            qs[2]["question"] = "What did Canva announce at its last event?"  # an edit
            added = {"company": "Canva", "dimension": "customers", "question": "Who are Canva's newest customers?",
                     "search_query": "Canva (canva.com) new customers"}  # an addition, no id yet
            return qs[2:] + [added]

        with TempReports() as t:
            events, _ = run_all(settings={"approve_questions": True}, answers={"approve_questions": edit})
            data = json.loads((t.path / events[-1]["report"]["file"]).read_text())
        kinds = [e.get("kind") for e in events if e["event"] == "interrupt"]
        self.assertEqual(kinds, ["confirm_rivals", "approve_questions"])
        asked = [a["question"]["question"] for a in data["questions"]]
        for removed in seen["removed"]:
            self.assertNotIn(removed, asked)
        self.assertIn("What did Canva announce at its last event?", asked)
        self.assertIn("Who are Canva's newest customers?", asked)
        ids = [a["question"]["id"] for a in data["questions"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(sorted(ids, key=lambda i: int(i[1:]))[0], "Q1")

    def test_no_pauses_when_turned_off(self):
        with TempReports():
            events, _ = run_all(settings={"confirm_rivals": False})
        self.assertFalse([e for e in events if e["event"] == "interrupt"])
        self.assertEqual(events[-1]["event"], "done")

    def test_given_rivals_skip_discovery(self):
        company = {**FIGMA, "rivals": [{"name": "Sketch", "domain": "sketch.com", "reason": "given"}]}
        with TempReports():
            events, _ = run_all(company=company)
        first = next(e for e in events if e["event"] == "interrupt")
        self.assertEqual([r["name"] for r in first["rivals"]], ["Sketch"])

    def test_resume_unknown_run_says_so(self):
        events = list(run_segment(new_run_id(), resume=[]))
        self.assertEqual(events[-1]["event"], "error")
        self.assertIn("no longer in memory", events[-1]["message"])


class StopTests(unittest.TestCase):
    def test_bad_exa_key_stops_cleanly(self):
        class Broken:
            def search(self, *a, **k):
                raise RuntimeError("401 INVALID_API_KEY")

        with umock.patch.object(exa, "make_client", lambda mock: Broken()), TempReports():
            events = list(run_segment(new_run_id(), start={"company": dict(FIGMA), "settings": {"mock": True}}))
        self.assertEqual(events[-1]["event"], "error")
        self.assertIn("Exa rejected the API key", events[-1]["message"])

    def test_missing_ai_key_stops_cleanly(self):
        drop = ("OPENCODE_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "ANTHROPIC_API_KEY", "AI_PROVIDER")
        env = {k: v for k, v in os.environ.items() if k not in drop}
        with umock.patch.dict(os.environ, env, clear=True), \
                umock.patch.object(exa, "make_client", lambda mock: exa.MockExa()), TempReports():
            events = list(run_segment(new_run_id(), start={"company": dict(FIGMA), "settings": {"mock": False}}))
        self.assertEqual(events[-1]["event"], "error")
        self.assertIn("GEMINI_API_KEY", events[-1]["message"])

    def test_ai_errors_become_plain_messages(self):
        from agent.llm import llm_stop_reason
        self.assertIn("Gemini rejected", llm_stop_reason(Exception("400 API key not valid. Please pass a valid key")))
        with umock.patch.dict(os.environ, {"AI_PROVIDER": "opencode"}):
            self.assertIn("OpenCode rejected", llm_stop_reason(Exception("Upstream request failed: Invalid credential")))
        with umock.patch.dict(os.environ, {"AI_PROVIDER": "opencode"}):
            self.assertIn("out of credits", llm_stop_reason(Exception("402 insufficient balance")))
        self.assertIn("Gemini's prepaid credits", llm_stop_reason(Exception(
            "402 Your prepayment credits are depleted. Please go to AI Studio at https://ai.studio/projects")))
        self.assertIn("isn't available", llm_stop_reason(Exception("404 NOT_FOUND models/gemini-x is not found")))
        self.assertIsNone(llm_stop_reason(Exception("503 high demand")))
        self.assertIn("quota", llm_stop_reason(Exception("429 RESOURCE_EXHAUSTED quota"), transient_exhausted=True))
        self.assertIn("overloaded", llm_stop_reason(Exception("503 UNAVAILABLE"), transient_exhausted=True))


class ProviderTests(unittest.TestCase):
    def env(self, **kw):
        base = {k: v for k, v in os.environ.items()
                if k not in ("OPENCODE_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "ANTHROPIC_API_KEY",
                             "AI_PROVIDER", "PLANNER_MODEL", "RESEARCHER_MODEL")}
        return umock.patch.dict(os.environ, {**base, **kw}, clear=True)

    def test_gemini_is_picked_when_its_key_is_set(self):
        from agent import llm
        with self.env(GEMINI_API_KEY="x", OPENCODE_API_KEY="y"):
            self.assertEqual(llm.provider(), "gemini")
            self.assertEqual(llm.model_chain("planner", {})[0], "gemini-3.6-flash")
            self.assertEqual(llm.model_chain("researcher", {})[0], "gemini-3.5-flash-lite")
            self.assertGreater(len(llm.model_chain("researcher", {})), 1)
        with self.env(OPENCODE_API_KEY="y"):
            self.assertEqual(llm.provider(), "opencode")
        with self.env(GEMINI_API_KEY="x", AI_PROVIDER="opencode"):
            self.assertEqual(llm.provider(), "opencode")

    def test_anthropic_key_wins_and_uses_haiku_for_both_roles(self):
        from agent import llm
        with self.env(ANTHROPIC_API_KEY="a", GEMINI_API_KEY="g"):
            self.assertEqual(llm.provider(), "anthropic")
            self.assertTrue(llm.has_key())
            self.assertEqual(llm.model_chain("planner", {}), ["claude-haiku-5-5"])
            self.assertEqual(llm.model_chain("researcher", {}), ["claude-haiku-5-5"])
            m = llm.get_model("planner", {"mock": False})
            self.assertEqual(type(m).__name__, "ChatAnthropic")
            self.assertEqual(m.model, "claude-haiku-5-5")
            self.assertNotIn("opencode", str(m.anthropic_api_url))
            self.assertIn("Claude rejected", llm.llm_stop_reason(Exception("401 authentication_error invalid x-api-key")))
            self.assertIn("console.anthropic.com", llm.llm_stop_reason(
                Exception("400 Your credit balance is too low to access the Anthropic API")))
        with self.env(GEMINI_API_KEY="g", AI_PROVIDER="anthropic"):
            self.assertFalse(llm.has_key())

    def test_model_overrides_can_be_lists(self):
        from agent import llm
        with self.env(GEMINI_API_KEY="x", PLANNER_MODEL="gemini-3.8-flash, gemini-3.6-flash"):
            self.assertEqual(llm.model_chain("planner", {}), ["gemini-3.8-flash", "gemini-3.6-flash"])
            self.assertEqual(llm.model_chain("planner", {"models": {"planner": "gemini-3.7-flash"}}),
                             ["gemini-3.7-flash"])

    def test_daily_limits_are_told_apart_and_expire(self):
        from agent import llm
        daily = Exception('429 quota_id: "GenerateRequestsPerDayPerProjectPerModel-FreeTier" Please retry in 0.0s')
        self.assertTrue(llm.is_daily_quota(daily))
        self.assertFalse(llm.is_daily_quota(Exception("429 GenerateRequestsPerMinutePerProjectPerModel")))
        llm.mark_exhausted("model-z", daily)
        self.assertFalse(llm.exhausted("model-z"))  # retry delay already passed
        llm.mark_exhausted("model-y", Exception("PerDay Please retry in 2h40m58.4s"))
        self.assertTrue(llm.exhausted("model-y"))
        llm._exhausted.clear()


class Flaky:
    """A fake model: raises the queued errors, then answers with a valid Judgment tool call."""

    def __init__(self, errors):
        self.errors, self.calls = list(errors), 0

    def bind_tools(self, *a, **k):
        return self

    def invoke(self, messages):
        from langchain_core.messages import AIMessage
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return AIMessage(content="", tool_calls=[{"name": "Judgment", "id": "1",
                                                  "args": {"answered": True, "missing": "", "next_query": ""}}])


class RetryTests(unittest.TestCase):
    def setUp(self):
        from agent import llm
        llm._exhausted.clear()
        self.waits = umock.patch.object(graph, "RETRY_WAITS", (0, 0))
        self.waits.start()

    def tearDown(self):
        from agent import llm
        self.waits.stop()
        llm._exhausted.clear()

    def run_ask(self, models):
        from agent.schemas import Judgment
        settings = {"mock": False, "models": {"researcher": ",".join(models)}}
        fakes = {m: f for m, f in models.items()}
        with umock.patch.object(graph, "get_model", lambda role, s, mock_responder=None, model=None: fakes[model]):
            return graph.ask("researcher", Judgment, "sys", "user", settings)

    def test_busy_errors_are_retried_on_the_same_model(self):
        a = Flaky([Exception("503 UNAVAILABLE high demand"), Exception("429 per minute")])
        self.assertTrue(self.run_ask({"a": a}).answered)
        self.assertEqual(a.calls, 3)

    def test_daily_limit_switches_model_and_remembers(self):
        from agent import llm
        a = Flaky([Exception("429 PerDayPerProjectPerModel Please retry in 2h0m1s")])
        b = Flaky([])
        self.assertTrue(self.run_ask({"a": a, "b": b}).answered)
        self.assertEqual((a.calls, b.calls), (1, 1))
        self.assertTrue(llm.exhausted("a"))
        self.run_ask({"a": a, "b": b})
        self.assertEqual((a.calls, b.calls), (1, 2))  # a is skipped now

    def test_still_busy_after_retries_moves_on_then_gives_up_plainly(self):
        from agent.llm import LLMStop
        a = Flaky([Exception("503 UNAVAILABLE")] * 5)
        b = Flaky([Exception("429 RESOURCE_EXHAUSTED quota")] * 5)
        with self.assertRaises(LLMStop) as cm:
            self.run_ask({"a": a, "b": b})
        self.assertEqual((a.calls, b.calls), (3, 3))
        self.assertIn("quota", str(cm.exception))

    def test_all_models_used_up_says_so(self):
        from agent import llm
        from agent.llm import LLMStop
        llm.mark_exhausted("a", Exception("retry in 1h0m0s"))
        with self.assertRaises(LLMStop) as cm:
            self.run_ask({"a": Flaky([])})
        self.assertIn("daily free-tier limit", str(cm.exception))

    def test_timeouts_move_on_without_retrying(self):
        a = Flaky([Exception("504 Deadline expired before operation could complete.")])
        b = Flaky([])
        self.assertTrue(self.run_ask({"a": a, "b": b}).answered)
        self.assertEqual((a.calls, b.calls), (1, 1))

    def test_real_errors_are_not_retried(self):
        a = Flaky([ValueError("bad schema")])
        with self.assertRaises(ValueError):
            self.run_ask({"a": a})
        self.assertEqual(a.calls, 1)


class DefaultsTests(unittest.TestCase):
    def test_defaults_match_the_plan(self):
        d = graph.DEFAULTS
        self.assertTrue(d["confirm_rivals"])
        self.assertFalse(d["approve_questions"])
        self.assertEqual((d["max_attempts"], d["max_rounds"]), (2, 2))


if __name__ == "__main__":
    unittest.main()
