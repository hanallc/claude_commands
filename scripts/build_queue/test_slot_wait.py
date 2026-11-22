"""Check queue names and refusal of retired capacity requests."""
from __future__ import annotations

import unittest

from scripts.build_queue import admission_protocol as wire, slot_wait
from scripts.build_queue.admission_test_support import AdmissionFixture


class QueueRowNameTests(unittest.TestCase):
    def test_an_unknown_repository_uses_the_folder_name(self) -> None:
        self.assertEqual(slot_wait.QueueRowName.repository("unregistered session", "/private/widget"), "widget build")


class RepositorySlotWaitTests(unittest.TestCase):
    def test_retired_capacity_request_never_waits_or_reserves_memory(self) -> None:
        fixture = self.enterContext(AdmissionFixture())
        original = fixture.request()
        request = wire.RepositorySlotRequest(original.episode, original.owner, original.since,
            "widget", "fixture", str(fixture.root / "copies"), 1, original.since + 30, original.identity)
        self.assertEqual(fixture.connect(request).replies(),
                         [wire.Refused("Repository copy capacity no longer requires admission")])
        self.assertFalse(fixture.server.reservations)
        self.assertFalse(fixture.server.waiters)
