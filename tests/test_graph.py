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
        env = {k: v for k, v in os.environ.items() if k != "OPENCODE_API_KEY"}
        with umock.patch.dict(os.environ, env, clear=True), \
                umock.patch.object(exa, "make_client", lambda mock: exa.MockExa()), TempReports():
            events = list(run_segment(new_run_id(), start={"company": dict(FIGMA), "settings": {"mock": False}}))
        self.assertEqual(events[-1]["event"], "error")
        self.assertIn("OPENCODE_API_KEY", events[-1]["message"])

    def test_ai_errors_become_plain_messages(self):
        from agent.llm import llm_stop_reason
        self.assertIn("rejected the API key", llm_stop_reason(Exception("Upstream request failed: Invalid credential")))
        self.assertIn("out of credits", llm_stop_reason(Exception("402 insufficient balance")))
        self.assertIsNone(llm_stop_reason(Exception("timeout")))


class DefaultsTests(unittest.TestCase):
    def test_defaults_match_the_plan(self):
        d = graph.DEFAULTS
        self.assertTrue(d["confirm_rivals"])
        self.assertFalse(d["approve_questions"])
        self.assertEqual((d["max_attempts"], d["max_rounds"]), (2, 2))


if __name__ == "__main__":
    unittest.main()
