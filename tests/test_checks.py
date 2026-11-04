"""The fact checks and the rubric: plain rules, so they're tested directly."""
import datetime
import unittest

import helpers  # noqa: F401  (sets the import path)
from agent import checks

TODAY = datetime.date(2026, 11, 3)
SKETCH = {"name": "Sketch", "domain": "sketch.com"}


def src(url, text="", date="", title=""):
    return {"url": url, "text": text, "date": date, "title": title}


class DateTests(unittest.TestCase):
    def test_dates_in_urls(self):
        self.assertEqual(checks.date_from_url("https://www.businesswire.com/news/home/20230629787067/en/x"),
                         datetime.date(2023, 6, 29))
        self.assertEqual(checks.date_from_url("https://example.com/blog/2025/03/launch"), datetime.date(2025, 3, 1))
        self.assertEqual(checks.date_from_url("https://www.axure.com/changelog/2026-09-24"),
                         datetime.date(2026, 9, 24))

    def test_long_numeric_ids_are_not_dates(self):
        self.assertIsNone(checks.date_from_url("https://www.linkedin.com/posts/x-activity-7511702329889366017-Hz_8"))

    def test_later_url_date_wins_and_future_is_ignored(self):
        self.assertEqual(checks.best_date("2025-06-12", "https://x.com/changelog/2026-09-24", TODAY),
                         datetime.date(2026, 9, 24))
        self.assertIsNone(checks.best_date("2030-01-01", "https://x.com/a", TODAY))


class NumberTests(unittest.TestCase):
    def test_missing_numbers_are_reported_with_units(self):
        self.assertEqual(checks.unverified_numbers("7.7% CTR and 200K reach, 10+ events", "ten events"),
                         ["7.7%", "200K"])

    def test_equivalent_forms_match(self):
        ev = "More than 300 applications in 3 months, 150,000 reach, eight events in seven cities."
        self.assertEqual(checks.unverified_numbers(
            "Over 300 applications in three months, 150K+ reach, 8 events across 7 cities", ev), [])

    def test_years_are_not_claims(self):
        self.assertEqual(checks.unverified_numbers("Launched in 2026 with 12 partners", "12 partners"), [])


class FindingTests(unittest.TestCase):
    def setUp(self):
        self.sources = {
            "Q1-1": src("https://www.sketch.com/blog/ai", "Sketch AI drew 300 signups.", "2026-10-01"),
            "Q1-2": src("https://www.linkedin.com/posts/sketch-events_popup", "A restaurant pop-up.", "2026-10-01"),
            "Q1-3": src("https://www.businesswire.com/news/home/20230629787067/en/sketch", "Sketch partnership."),
            "Q1-4": src("https://techcrunch.com/sketch", "Sketch ran a webinar series."),
        }

    def check(self, *findings, max_age=365):
        return checks.check_findings(list(findings), self.sources, SKETCH, max_age, TODAY)

    def test_good_finding_is_kept_clean(self):
        kept, dropped = self.check({"claim": "Sketch AI drew 300 signups", "source_id": "Q1-1"})
        self.assertEqual((len(kept), dropped), (1, []))
        self.assertEqual(kept[0]["flags"], [])
        self.assertEqual(kept[0]["date"], "2026-10-01")

    def test_made_up_citation_is_dropped(self):
        _, dropped = self.check({"claim": "A billboard", "source_id": "Q99-1"})
        self.assertIn("isn't a source it read", dropped[0]["reason"])

    def test_wrong_company_is_dropped(self):
        _, dropped = self.check({"claim": "A pop-up", "source_id": "Q1-2"})
        self.assertEqual(dropped[0]["reason"], "source doesn't mention Sketch")

    def test_old_is_dropped_unless_age_limit_is_off(self):
        _, dropped = self.check({"claim": "Partnership", "source_id": "Q1-3"})
        self.assertTrue(dropped[0]["reason"].startswith("old: published 2023-06-29"))
        kept, _ = self.check({"claim": "Partnership", "source_id": "Q1-3"}, max_age=0)
        self.assertEqual(len(kept), 1)

    def test_undated_is_flagged_and_unbacked_number_lowers_confidence(self):
        kept, _ = self.check({"claim": "Webinar series hit 7.7% CTR", "source_id": "Q1-4", "confidence": "High"})
        self.assertIn("date unknown", kept[0]["flags"])
        self.assertIn("numbers not found in source: 7.7%", kept[0]["flags"])
        self.assertEqual(kept[0]["confidence"], "Low")


class RubricTests(unittest.TestCase):
    SOURCES = {"A": src("https://www.sketch.com/a"), "B": src("https://techcrunch.com/b"),
               "C": src("https://theverge.com/c")}

    def test_two_outside_sources_allow_strong(self):
        self.assertEqual(checks.rubric_cap("Strong", ["B", "C"], self.SOURCES, "sketch.com", False), ("Strong", ""))

    def test_one_outside_source_caps_at_medium(self):
        self.assertEqual(checks.rubric_cap("Strong", ["A", "B"], self.SOURCES, "sketch.com", False),
                         ("Medium", "lowered from Strong: only one outside source"))

    def test_own_pages_only_cap_at_medium_but_a_verified_number_allows_strong(self):
        self.assertEqual(checks.rubric_cap("Strong", ["A"], self.SOURCES, "sketch.com", False)[0], "Medium")
        self.assertEqual(checks.rubric_cap("Strong", ["A"], self.SOURCES, "sketch.com", True)[0], "Strong")

    def test_never_raised_and_odd_values_become_weak(self):
        self.assertEqual(checks.rubric_cap("Weak", ["B", "C"], self.SOURCES, "sketch.com", True), ("Weak", ""))
        self.assertEqual(checks.rubric_cap("great", [], self.SOURCES, "sketch.com", False)[0], "Weak")

    def test_ids_are_cleaned(self):
        self.assertEqual(checks.clean_ids(["C3,", " q2-1 ", "C3", "[Q4-2]; Q5-1"]), ["C3", "Q2-1", "Q4-2", "Q5-1"])


if __name__ == "__main__":
    unittest.main()
