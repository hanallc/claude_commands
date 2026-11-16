"""The Builds page shows the memory amounts from the saved admission pass."""

import unittest

from scripts.build_queue import admission_records as records, queue_evidence
from scripts.lint import memory_admit as gate
from scripts.lint.memory_test_support import admission_policy_fixture

GIB = records.GIB


class RoomTextTests(unittest.TestCase):
    def test_ci_amount_shows_mib_only_under_a_tenth_of_a_gib(self) -> None:
        for value, text in ((47 * GIB // 1024, "47 MiB"), (GIB // 10 - 1, "102 MiB"),
                            (GIB // 10, "0.1 GiB"), (22 * GIB, "22.0 GiB")):
            with self.subTest(value=value):
                self.assertEqual(queue_evidence.ci_amount(value), text)

    def test_cap_text_shows_idle_ci_use_in_mib(self) -> None:
        room = gate.memory_room(records.HostMemory(20 * GIB, 64 * GIB),
            (records.RunMemory("run", 8 * GIB, records.AnonymousRunUse(3 * GIB)),),
            admission_policy_fixture(), ci_use=63 * GIB // 1024)
        assert isinstance(room, records.MemoryRoom)
        self.assertIn("anonymous use 3.0 GiB + CI use 63 MiB + free 20.0 GiB", queue_evidence.room_text(room))


    def test_cap_text_names_ci_use_and_its_printed_terms_sum_to_the_cap(self) -> None:
        for ci_use, swap_free, text in (
            (0, 32 * GIB, "Cap 20.0 GiB; anonymous use 3.0 GiB + free 20.0 GiB − safety 3.0 GiB; kept aside 5.0 GiB; room 12.0 GiB"),
            (4 * GIB, 32 * GIB, "Cap 24.0 GiB; anonymous use 3.0 GiB + CI use 4.0 GiB + free 20.0 GiB − safety 3.0 GiB; kept aside 5.0 GiB; room 12.0 GiB"),
            (0, 22 * GIB, "Cap 10.0 GiB; anonymous use 3.0 GiB + free 20.0 GiB − safety 3.0 GiB − 10.0 GiB swapped outside builds; kept aside 5.0 GiB; room 2.0 GiB"),
        ):
            with self.subTest(ci_use=ci_use, swap_free=swap_free):
                room = gate.memory_room(records.HostMemory(20 * GIB, 64 * GIB, 32 * GIB, swap_free),
                    (records.RunMemory("run", 8 * GIB, records.AnonymousRunUse(3 * GIB)),),
                    admission_policy_fixture(), ci_use=ci_use)
                assert isinstance(room, records.MemoryRoom)
                self.assertEqual(queue_evidence.room_text(room), text)
