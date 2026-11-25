"""Check List search with the pinned Unicode case-folding data."""

import unittest

from scripts.review_links import search_fold


class SearchFoldTests(unittest.TestCase):
    def test_search_uses_the_pinned_case_folding(self) -> None:
        for text, expected in (
                ("List", "list"), ("Straße", "strasse"),
                ("\u0100\u0101", "\u0101\u0101"), ("\U00010400", "\U00010428"),
                ("\u1c89\u1c8a", "\u1c89\u1c8a")):
            with self.subTest(text=text):
                self.assertEqual(search_fold.fold(text), expected)
