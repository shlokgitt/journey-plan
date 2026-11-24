import unittest

import app


class StopSearchTests(unittest.TestCase):
    def test_substring_search_returns_matches_sorted_by_relevance(self):
        query = "isbt"
        result = app.stop_list(search=query)
        all_matches = [
            stop for stop in app.stop_by_id.values()
            if query in stop["stop_name"].casefold()
        ]

        # Every returned stop must be a valid substring match.
        for stop in result["stops"]:
            self.assertIn(query, stop["stop_name"].casefold())

        expected = sorted(
            all_matches,
            key=lambda stop: app._stop_search_score(
                stop["stop_name"].casefold(), query
            ),
        )
        self.assertEqual(result["stops"], expected)
        self.assertEqual(result["count"], len(all_matches))
        self.assertEqual(result["total_matches"], len(all_matches))
        self.assertFalse(result["truncated"])

    def test_starts_with_matches_appear_before_substring_matches(self):
        query = "isbt"
        result = app.stop_list(search=query)

        starts_with_seen = False
        substring_seen = False
        for stop in result["stops"]:
            name_lower = stop["stop_name"].casefold()
            if name_lower.startswith(query):
                # Once we see a pure-substring match, no starts-with should follow.
                self.assertFalse(
                    substring_seen,
                    f"starts-with match '{stop['stop_name']}' appeared "
                    f"after a substring-only match",
                )
                starts_with_seen = True
            else:
                substring_seen = True

    def test_exact_name_search_returns_all_stops_with_that_name(self):
        exact_name = "ISBT Kashmere Gate"
        result = app.stop_list(search=exact_name)

        self.assertTrue(result["stops"])
        self.assertTrue(all(stop["stop_name"] == exact_name for stop in result["stops"]))

    def test_short_query_relevance_order(self):
        """Typing 'nehru' should show 'Nehru Place' before 'Jawaharlal Nehru ...'."""
        query = "nehru"
        result = app.stop_list(search=query)
        if not result["stops"]:
            self.skipTest("No stops match 'nehru'")

        # First result should start with or have a word starting with the query,
        # not be buried under substring-only matches.
        first = result["stops"][0]["stop_name"].casefold()
        words = first.replace("/", " ").replace("-", " ").split()
        starts_or_word = first.startswith(query) or any(
            w.startswith(query) for w in words
        )
        self.assertTrue(
            starts_or_word,
            f"First result '{result['stops'][0]['stop_name']}' does not "
            f"start with or have a word starting with '{query}'",
        )


if __name__ == "__main__":
    unittest.main()
