"""The web API: start, pause, resume, saved reports, and input checks."""
import json
import os
import unittest

os.environ["PHOENIX_TRACING"] = "0"
from helpers import TempReports  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from backend import main  # noqa: E402

client = TestClient(main.app)
START = {"company": {"name": "Figma", "domain": "figma.com"},
         "settings": {"mock": True, "max_rivals": 2, "questions_per_rival": 2}}


def events(resp):
    return [json.loads(line[6:]) for line in resp.text.splitlines() if line.startswith("data: ")]


class ApiTests(unittest.TestCase):
    def test_health_hides_keys(self):
        body = client.get("/api/health").json()
        self.assertIn("has_exa_key", body)
        self.assertNotIn("key", json.dumps(body["models"]))

    def test_start_pause_resume_and_open_the_report(self):
        with TempReports():
            first = events(client.post("/api/scans", json=START))
            pause = first[-1]
            self.assertEqual((pause["event"], pause["kind"]), ("interrupt", "confirm_rivals"))
            second = events(client.post(f"/api/scans/{pause['run_id']}/resume", json={"value": pause["rivals"][:1]}))
            done = second[-1]
            self.assertEqual(done["event"], "done")
            self.assertTrue(any(e["event"] == "question" for e in second))

            listed = client.get("/api/reports").json()["reports"]
            self.assertEqual([r["run_id"] for r in listed], [done["run_id"]])
            self.assertEqual(len(listed[0]["rivals"]), 1)

            got = client.get(f"/api/reports/{done['run_id']}").json()
            self.assertEqual(got["data"]["run_id"], done["run_id"])
            self.assertIn("## Action plan", got["markdown"])

    def test_field_focus_is_accepted_and_listed(self):
        body = {**START, "settings": {**START["settings"], "focus": "field"}}
        with TempReports():
            pause = events(client.post("/api/scans", json=body))[-1]
            done = events(client.post(f"/api/scans/{pause['run_id']}/resume", json={"value": pause["rivals"]}))[-1]
            self.assertEqual(done["event"], "done")
            listed = client.get("/api/reports").json()["reports"]
            self.assertEqual(listed[0]["focus"], "field")
        bad = {**START, "settings": {**START["settings"], "focus": "events"}}
        self.assertEqual(client.post("/api/scans", json=bad).status_code, 422)

    def test_given_rivals_are_used(self):
        body = {**START, "company": {**START["company"], "rivals": [{"name": "Sketch", "domain": "sketch.com"}]}}
        with TempReports():
            pause = events(client.post("/api/scans", json=body))[-1]
        self.assertEqual([r["name"] for r in pause["rivals"]], ["Sketch"])

    def test_bad_ids_and_inputs_are_rejected(self):
        self.assertEqual(client.get("/api/reports/../../etc").status_code, 404)
        self.assertEqual(client.get("/api/reports/abc").status_code, 404)
        self.assertEqual(client.post("/api/scans/zzzzzzzz/resume", json={"value": []}).status_code, 404)
        self.assertEqual(client.post("/api/scans/0123abcd/resume", json={"value": "x"}).status_code, 422)
        bad = {**START, "settings": {"mock": True, "max_rivals": 50}}
        self.assertEqual(client.post("/api/scans", json=bad).status_code, 422)

    def test_old_v1_reports_are_not_listed(self):
        with TempReports() as t:
            (t.path / "2026-09-30_scan_0123abcd.json").write_text(json.dumps({"run_id": "0123abcd"}))
            self.assertEqual(client.get("/api/reports").json()["reports"], [])


if __name__ == "__main__":
    unittest.main()
