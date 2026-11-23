"""The Builds page shows the saved priority list."""

import unittest

from scripts.build_queue import admission_records as records, head_facts


class PriorityTextTests(unittest.TestCase):
    def test_priority_text_uses_the_saved_order_and_reasons(self) -> None:
        cases: tuple[tuple[tuple[records.PriorityEntry, ...], str], ...] = (
            ((), ""),
            (({"unit": "first", "why": "urgent", "set_at": 0, "set_by": "user"},),
             "queue priority: first (urgent)"),
            (({"unit": "first", "why": "urgent", "set_at": 0, "set_by": "user"},
              {"unit": "second", "why": "demo", "set_at": 0, "set_by": "user"}),
             "queue priority: first (urgent), second (demo)"),
        )
        for entries, expected in cases:
            with self.subTest(entries=entries):
                priority = records.QueuePriority(entries) if entries else records.NoQueuePriority()
                self.assertEqual(head_facts.priority_text(priority), expected)
