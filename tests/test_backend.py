"""Backend API tests (mock mode only; no Exa calls)."""
import json
import unittest

from fastapi.testclient import TestClient

from helpers import TempOutput  # also puts the project on sys.path
import backend.main as backend


def sse_events(text):
    return [json.loads(line[5:]) for line in text.splitlines() if line.startswith("data:")]


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.out = TempOutput(backend).__enter__()
        self.api = TestClient(backend.app)

    def tearDown(self):
        self.out.__exit__(None, None, None)

    def test_health(self):
        self.assertTrue(self.api.get("/api/health").json()["ok"])

    def test_discover_mock(self):
        r = self.api.post("/api/discover", json={"name": "Acme", "mock": True})
        self.assertEqual([c["name"] for c in r.json()["competitors"]], ["Canva", "Miro", "Notion"])

    def test_scan_then_list_and_open(self):
        r = self.api.post("/api/scan", json={"company": {"name": "Acme"}, "mock": True, "synthesis": "template"})
        events = sse_events(r.text)
        self.assertEqual([e["event"] for e in events][-1], "done")
        run_id = events[-1]["run_id"]

        runs = self.api.get("/api/runs").json()["runs"]
        self.assertEqual(runs[0]["run_id"], run_id)
        self.assertEqual(runs[0]["company"], "Acme")
        self.assertTrue(runs[0]["has_data"])

        rep = self.api.get(f"/api/reports/{run_id}").json()
        self.assertIn("# Competitor Marketing Scan", rep["markdown"])
        self.assertEqual(rep["data"]["run_id"], run_id)
        self.assertTrue(self.api.get(f"/api/traces/{run_id}").json()["spans"])

    def test_text_only_report_has_no_data(self):
        (self.out.path / "reports" / "2026-01-01_competitor-scan_abc123.md").write_text(
            "# Competitor Marketing Scan\n\nMy company: **Old Co**\n")
        rep = self.api.get("/api/reports/abc123").json()
        self.assertIsNone(rep["data"])
        self.assertEqual(self.api.get("/api/runs").json()["runs"][0]["company"], "Old Co")

    def test_bad_ids_are_rejected(self):
        self.assertEqual(self.api.get("/api/reports/nope").status_code, 404)
        self.assertEqual(self.api.get("/api/reports/..%2f..%2fREADME").status_code, 404)
        self.assertEqual(self.api.get("/api/traces/xyz").status_code, 404)

    def test_real_scan_without_key_is_a_clear_error(self):
        saved = backend.os.environ.pop("EXA_API_KEY", None)
        try:
            r = self.api.post("/api/discover", json={"name": "Acme", "mock": False})
            self.assertEqual(r.status_code, 400)
            self.assertIn("EXA_API_KEY", r.json()["detail"])
        finally:
            if saved is not None:
                backend.os.environ["EXA_API_KEY"] = saved

    def test_discovery_with_bad_key_is_a_clear_error(self):
        class BadExa:
            def search(self, *a, **k):
                raise RuntimeError("Request failed with status code 401: INVALID_API_KEY")
        saved = backend._exa_client
        backend._exa_client = lambda: BadExa()
        try:
            r = self.api.post("/api/discover", json={"name": "Acme", "mock": False})
        finally:
            backend._exa_client = saved
        self.assertEqual(r.status_code, 502)
        self.assertIn("rejected the API key", r.json()["detail"])

    def test_scan_with_bad_key_sends_error_event(self):
        class BadExa:
            def search(self, *a, **k):
                raise RuntimeError("401 INVALID_API_KEY")
        saved = backend._exa_client
        backend._exa_client = lambda: BadExa()
        try:
            r = self.api.post("/api/scan", json={"company": {"name": "Acme"}, "mock": False,
                                                 "competitors": [{"name": "Sketch", "domain": "sketch.com"}],
                                                 "synthesis": "template"})
        finally:
            backend._exa_client = saved
        last = sse_events(r.text)[-1]
        self.assertEqual(last["event"], "error")
        self.assertIn("rejected the API key", last["message"])

    def test_old_judge_field_is_ignored(self):
        r = self.api.post("/api/scan", json={"company": {"name": "Acme"}, "mock": True,
                                             "synthesis": "template", "judge": "ollama"})
        self.assertEqual(sse_events(r.text)[-1]["event"], "done")


if __name__ == "__main__":
    unittest.main()
