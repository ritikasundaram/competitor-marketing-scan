"""Unit tests for the fact checks: dates, numbers, wrong-company matches."""
import datetime
import unittest

from helpers import runner

TODAY = datetime.date(2026, 11, 3)


class DateTests(unittest.TestCase):
    def test_dates_in_urls(self):
        self.assertEqual(runner._date_from_url("https://www.businesswire.com/news/home/20230629787067/en/x"),
                         datetime.date(2023, 6, 29))
        self.assertEqual(runner._date_from_url("https://example.com/blog/2025/03/launch"), datetime.date(2025, 3, 1))
        self.assertEqual(runner._date_from_url("https://www.axure.com/changelog/2026-09-24"), datetime.date(2026, 9, 24))

    def test_long_numeric_ids_are_not_dates(self):
        self.assertIsNone(runner._date_from_url(
            "https://www.linkedin.com/posts/x-activity-7511702329889366017-Hz_8"))

    def test_old_campaign_is_excluded(self):
        c = {"url": "https://www.businesswire.com/news/home/20230629787067/en/x", "date": ""}
        runner.verify_campaigns([c], 365, TODAY)
        self.assertTrue(c["stale"])
        self.assertTrue(c["excluded"].startswith("old: published 2023-06-29"))

    def test_later_url_date_wins_over_older_page_date(self):
        c = {"url": "https://www.axure.com/changelog/2026-09-24", "date": "2025-06-12"}
        runner.verify_campaigns([c], 365, TODAY)
        self.assertEqual(c["date"], "2026-09-24")
        self.assertFalse(c["stale"])

    def test_max_age_zero_keeps_everything(self):
        c = {"url": "https://x.com/2019/01/05/a", "date": ""}
        runner.verify_campaigns([c], 0, TODAY)
        self.assertFalse(c["stale"])

    def test_undated_is_flagged_not_excluded(self):
        c = {"url": "https://x.com/a", "date": ""}
        runner.verify_campaigns([c], 365, TODAY)
        self.assertIn("date unknown", c["flags"])
        self.assertEqual(c["excluded"], "")

    def test_future_dates_are_ignored(self):
        c = {"url": "https://x.com/a", "date": "2030-01-01"}
        runner.verify_campaigns([c], 365, TODAY)
        self.assertIn("date unknown", c["flags"])


class NumberTests(unittest.TestCase):
    def test_missing_numbers_are_reported_with_units(self):
        self.assertEqual(runner.unverified_numbers("7.7% CTR and 200K reach, 10+ events", "ten events"),
                         ["7.7%", "200K"])

    def test_equivalent_forms_match(self):
        ev = "More than 300 applications in 3 months, 150,000 reach, eight events in seven cities."
        self.assertEqual(runner.unverified_numbers(
            "Over 300 applications in three months, 150K+ reach, 8 events across 7 cities", ev), [])

    def test_unverified_number_lowers_confidence(self):
        c = {"url": "https://x.com/a", "date": "2026-10-01", "signal": "7.7% CTR",
             "snippet": "great launch", "confidence": "high"}
        runner.verify_campaigns([c], 365, TODAY)
        self.assertEqual(c["confidence"], "Low")
        self.assertTrue(any(f.startswith("numbers not found") for f in c["flags"]))

    def test_casing_is_normalized(self):
        c = {"url": "https://x.com/a", "date": "2026-10-01", "strength": "strong", "confidence": "medium"}
        runner.verify_campaigns([c], 365, TODAY)
        self.assertEqual((c["strength"], c["confidence"]), ("Strong", "Medium"))


class RelevanceTests(unittest.TestCase):
    def test_own_site_always_counts(self):
        self.assertIsNone(runner.relevance({"url": "https://updates.uxpin.com/x", "about_competitor": False},
                                           "UXPin", "uxpin.com"))

    def test_grader_verdict_false_excludes(self):
        reason = runner.relevance({"url": "https://linkedin.com/x", "about_competitor": "false",
                                   "run_by": "Sketch Events"}, "Sketch", "sketch.com")
        self.assertEqual(reason, "not Sketch: run by Sketch Events")

    def test_no_verdict_needs_a_mention(self):
        self.assertIsNone(runner.relevance({"url": "https://tc.com/x", "snippet": "Notion announced"},
                                           "Notion", "notion.so"))
        self.assertEqual(runner.relevance({"url": "https://tc.com/x", "snippet": "Coach and Spotify"},
                                          "Notion", "notion.so"), "doesn't mention Notion")

    def test_off_topic_wins_over_old_as_the_reason(self):
        c = {"url": "https://x.com/2023/01/05/a", "about_competitor": False, "run_by": "Sketch Restaurant"}
        runner.verify_campaigns([c], 365, TODAY, competitor=("Sketch", "sketch.com"))
        self.assertEqual(c["excluded"], "not Sketch: run by Sketch Restaurant")


if __name__ == "__main__":
    unittest.main()
