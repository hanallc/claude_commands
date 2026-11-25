"""The Builds page shows the saved priority list."""

import unittest

from scripts.build_queue import admission_protocol as wire, admission_records as records, head_facts


class PriorityTextTests(unittest.TestCase):
    def test_priority_text_uses_the_saved_order_and_reasons(self) -> None:
        cases: tuple[tuple[tuple[records.PriorityEntry, ...], str], ...] = (
            ((), ""),
            (({"unit": "first", "why": "urgent", "set_at": 0, "set_by": "user"},),
             "queue priority: first (urgent)"),
            (({"unit": "first", "why": "urgent", "set_at": 0, "set_by": "user"},
              {"unit": "second", "why": "demo", "set_at": 0, "set_by": "user"}),
             "queue priority: first (urgent), second (demo)"),
            (({"kind": "UnitPriorityEntry", "unit": "hana", "why": "natemerge can bump at will", "set_at": 0,
               "set_by": "merge-session", "asker": records.StaffAsker("natemerge")},
              {"kind": "RunPriorityEntry", "run": wire.RunIdentity(41, "900"), "unit": "cables", "step": "nextest",
               "why": "/build:bump --run 2", "set_at": 0, "set_by": "user", "asker": records.PlainShellAsker()},
              {"kind": "UnitPriorityEntry", "unit": "demo", "why": "/build:bump 3", "set_at": 0,
               "set_by": "lone-session", "asker": records.UnstaffedSession("lone-session")}),
             'queue priority: hana (natemerge: "natemerge can bump at will"), '
             + 'cables · nextest (one run) (nate (plain shell): "/build:bump --run 2"), '
             + 'demo (lone-session: "/build:bump 3")'),
        )
        for entries, expected in cases:
            with self.subTest(entries=entries):
                priority = records.QueuePriority(entries) if entries else records.NoQueuePriority()
                self.assertEqual(head_facts.priority_text(priority), expected)
