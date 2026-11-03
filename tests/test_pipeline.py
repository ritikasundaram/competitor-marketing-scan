"""End-to-end scan tests: mock mode, and the real-scan path driven by a fake Exa client."""
import json
import unittest

from helpers import FakeExa, TempOutput, runner

CFG = {"my_company": {"name": "Figma", "domain": "figma.com", "description": "Collaborative design tool"},
       "competitors": [{"name": "Sketch", "domain": "sketch.com"}], "synthesis": "template",
       "max_age_days": 365}


class MockScanTests(unittest.TestCase):
    def test_dry_run_writes_report_and_data(self):
        with TempOutput() as out:
            res = runner.scan_company({"my_company": {"name": "Acme"}, "synthesis": "template"}, dry=True)
            md = (out.path / "reports" / f"{res['report_path'].split('/')[-1]}").read_text()
            data = json.loads(next((out.path / "reports").glob("*.json")).read_text())
            self.assertTrue((out.path / "traces" / f"{res['run_id']}.jsonl").exists())
        self.assertIn("## Recommendations for Acme", md)
        self.assertEqual(set(data["competitors"]), {"Canva", "Miro", "Notion"})
        self.assertTrue(data["mock"])
        self.assertEqual(data["recommendations"], [])  # mock skips Exa answer
        self.assertEqual(data["own_campaigns"], [])


class FakeExaScanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = FakeExa()
        with TempOutput() as out:
            cls.res = runner.scan_company(dict(CFG), client=cls.client)
            cls.md = next((out.path / "reports").glob("*.md")).read_text()
            cls.data = json.loads(next((out.path / "reports").glob("*.json")).read_text())
            cls.trace = (out.path / "traces" / f"{cls.res['run_id']}.jsonl").read_text()

    def test_kept_campaigns(self):
        titles = [c["title"] for c in self.data["competitors"]["Sketch"]]
        self.assertEqual(titles, ["Sketch AI launch", "Sketch webinar series"])

    def test_dates_survive_grading(self):
        self.assertTrue(all(c["date"] for c in self.data["competitors"]["Sketch"]))

    def test_excluded_with_reasons(self):
        reasons = {c["title"]: c["excluded"] for c in self.data["excluded"]}
        self.assertTrue(reasons["Little Moons pop-up"].startswith("not Sketch"))
        self.assertTrue(reasons["Sketch 2023 partnership"].startswith("old:"))

    def test_unverified_number_flagged(self):
        webinar = self.data["competitors"]["Sketch"][1]
        self.assertEqual(webinar["confidence"], "Low")
        self.assertIn("numbers not found in source: 7.7%", webinar["flags"])
        self.assertEqual(self.data["competitors"]["Sketch"][0]["flags"], [])

    def test_search_names_the_company(self):
        queries = [q for kind, q, _ in self.client.calls if kind == "search"]
        self.assertTrue(all("(sketch.com)" in q or "(figma.com)" in q for q in queries))
        self.assertTrue(any("(figma.com)" in q for q in queries), "own company should be scanned")

    def test_own_campaigns_scanned_without_page_fetches(self):
        self.assertEqual([c["title"] for c in self.data["own_campaigns"]], ["Figma Config keynote"])
        fetched = [u for kind, urls, _ in self.client.calls if kind == "get_contents" for u in urls]
        self.assertNotIn("https://www.figma.com/blog/config", fetched)

    def test_recommendations_are_checked(self):
        recs = {r["title"]: r for r in self.data["recommendations"]}
        self.assertIn("Kept", recs)
        self.assertNotIn("Web-only", recs)   # cited a page that isn't in the scan
        self.assertNotIn("Own-only", recs)   # cited only the company's own campaign
        self.assertEqual({e["competitor"] for e in recs["Kept"]["evidence"]}, {"Sketch", "Figma"})
        self.assertTrue(recs["Made-up number"]["flags"][0].startswith("numbers not found"))

    def test_markdown_matches_data(self):
        self.assertIn("## Excluded from analysis", self.md)
        self.assertIn("⚠ numbers not found in source: 7.7%", self.md)
        self.assertIn("## Figma's own recent campaigns", self.md)
        self.assertIn("1. **Kept**", self.md)

    def test_trace_records_checks(self):
        spans = [json.loads(line) for line in self.trace.splitlines()]
        verify = [s for s in spans if s["span"] == "verify"]
        self.assertEqual(verify[0]["stale"], 1)
        self.assertEqual(verify[0]["off_topic"], 1)
        self.assertTrue(any(s["span"] == "exa.answer" for s in spans))


class RecommendationRetryTests(unittest.TestCase):
    def test_retries_with_exa_pro_when_nothing_is_cited(self):
        client = FakeExa(uncited_unless_pro=True)
        with TempOutput() as out:
            runner.scan_company(dict(CFG), client=client)
            data = json.loads(next((out.path / "reports").glob("*.json")).read_text())
        models = [kw.get("model") for k, _, kw in client.calls if k == "answer"]
        self.assertEqual(models, [None, "exa-pro"])
        self.assertIn("Kept", [r["title"] for r in data["recommendations"]])

    def test_no_retry_when_first_answer_is_cited(self):
        client = FakeExa()
        with TempOutput():
            runner.scan_company(dict(CFG), client=client)
        self.assertEqual(len([1 for k, _, _ in client.calls if k == "answer"]), 1)


class OptionTests(unittest.TestCase):
    def run_scan(self, **cfg):
        client = FakeExa()
        with TempOutput() as out:
            runner.scan_company({**CFG, **cfg}, client=client)
            data = json.loads(next((out.path / "reports").glob("*.json")).read_text())
        return client, data

    def test_no_self(self):
        client, data = self.run_scan(scan_self=False)
        self.assertEqual(data["own_campaigns"], [])
        self.assertFalse(any("figma.com" in q for k, q, _ in client.calls if k == "search"))

    def test_no_grade_leaves_tbd(self):
        client, data = self.run_scan(grade=False)
        self.assertFalse(any("output_schema" in kw for k, _, kw in client.calls if k == "search"))
        self.assertTrue(all(c["strength"] == "TBD" for c in data["competitors"]["Sketch"]))

    def test_no_proof(self):
        client, _ = self.run_scan(fetch_proof=False)
        self.assertFalse(any(k == "get_contents" for k, _, _ in client.calls))

    def test_max_age_zero(self):
        _, data = self.run_scan(max_age_days=0)
        self.assertFalse(any(c.get("stale") for c in data["excluded"]))

    def test_rate_limit_stops_the_scan_without_a_report(self):
        with TempOutput() as out:
            with self.assertRaises(runner.ExaStop) as ctx:
                runner.scan_company(dict(CFG), client=FakeExa(fail_search="429 rate limit"))
            self.assertEqual(list((out.path / "reports").glob("*")), [])
        self.assertIn("rate limit", str(ctx.exception))

    def test_bad_key_stops_the_scan(self):
        with TempOutput():
            with self.assertRaises(runner.ExaStop) as ctx:
                runner.scan_company(dict(CFG), client=FakeExa(fail_search="401 INVALID_API_KEY"))
        self.assertIn("EXA_API_KEY", str(ctx.exception))

    def test_one_failed_query_is_skipped(self):
        with TempOutput() as out:
            runner.scan_company(dict(CFG), client=FakeExa(fail_search="500 server error", fail_once=True))
            data = json.loads(next((out.path / "reports").glob("*.json")).read_text())
        self.assertEqual(len(data["competitors"]["Sketch"]), 2)  # other queries still found them

    def test_competitor_without_domain(self):
        pages = {"Sketch": [dict(r, graded={k: v for k, v in r["graded"].items() if k != "about_competitor"})
                            for r in __import__("helpers").SKETCH]}
        with TempOutput() as out:
            runner.scan_company({**CFG, "competitors": [{"name": "Sketch", "domain": ""}], "scan_self": False},
                                client=FakeExa(pages))
            data = json.loads(next((out.path / "reports").glob("*.json")).read_text())
        # With no domain or grader verdict, the page must mention the name; the Sketch Events post does.
        self.assertGreaterEqual(len(data["competitors"]["Sketch"]), 1)


if __name__ == "__main__":
    unittest.main()
