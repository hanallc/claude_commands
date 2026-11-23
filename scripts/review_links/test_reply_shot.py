"""Tests for the reply shot crop and transcript."""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import cast

from scripts.review_links import reply_shot


class ReplyShotTests(unittest.TestCase):
    def test_lit_rows_read_one_mean_per_row(self) -> None:
        means = ("# ImageMagick pixel enumeration: 1,4,0,65535,gray\n"
                 + "0,0: (391)  #018701870187  gray(0.597271%)\n"
                 + "0,1: (0)  #000000000000  gray(0)\n"
                 + "0,2: (0)  #000000000000  gray(0)\n"
                 + "0,3: (217)  #00D900D900D9  gray(0.331765%)\n")
        self.assertEqual(reply_shot.lit_rows(means), [True, False, False, True])

    def test_the_shot_ends_at_the_first_tall_blank_band(self) -> None:
        reply = [True] * 5 + [False] * 3 + [True] * 4
        for lit, gap, expected in ((reply + [False] * 10 + [True], 4, 12), (reply + [False] * 10, 3, 5),
                                   (reply + [False] * 2, 4, None), ([False] * 6, 4, 0)):
            with self.subTest(gap=gap, expected=expected):
                self.assertEqual(reply_shot.content_height(lit, gap), expected)

    def test_transcript_holds_the_reply_as_the_assistant_text(self) -> None:
        lines = reply_shot.transcript("| a |", Path("/tmp/cwd"), "session", "2.1.319").splitlines()
        reply = cast(dict[str, dict[str, list[dict[str, str]]]], json.loads(lines[1]))
        self.assertEqual(len(lines), 2)
        self.assertEqual(reply["message"]["content"][0]["text"], "| a |")


if __name__ == "__main__":
    _ = unittest.main()
