"""Test the shared records for admission."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.build_queue import admission_records as records
from scripts.build_queue import admission_wire as wire, placement
from scripts.mac_test.claim_state import RepositoryScope, WHOLE_MAC


class RunRecordCodecTests(unittest.TestCase):
    def test_a_slot_without_a_destination_uses_natedev(self) -> None:
        old = ('{"kind":"VerifyRunSlot","repository":"fixture",'
               '"owner":{"kind":"Owner","pid":123,"start_time":"4200"}}')
        slot = wire.parse_record(old, wire.VerifyRunSlot)

        self.assertEqual(slot.destination, placement.PlacementDestination("local", "natedev", "natedev", WHOLE_MAC))
        self.assertEqual(slot.key, "123-4200:fixture")
        self.assertEqual(wire.parse_record(wire.encode_record(slot), wire.VerifyRunSlot), slot)

        scope = RepositoryScope("fixture", "fixture-key", 2)
        remote = replace(slot, destination=placement.PlacementDestination("ssh", "linux-builder", "builder-host", scope, 2))
        self.assertEqual(wire.parse_record(wire.encode_record(remote), wire.VerifyRunSlot), remote)

    def test_the_run_request_preserves_the_set_of_named_machines(self) -> None:
        owner = placement.SubmittingOwner("natedev", 123, "4200")
        request = records.RunRequest("run-a", owner, "fixture", 100.0,
                                     records.NamedMachines(frozenset({"natedev", "mac"})))

        self.assertEqual(wire.parse_record(wire.encode_record(request), records.RunRequest), request)


class ProcessStartTimeTests(unittest.TestCase):
    def test_process_stat_with_invalid_name_keeps_start_time(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            proc = Path(temporary)
            directory = proc / "123"
            directory.mkdir()
            _ = (directory / "stat").write_bytes(b"123 (verify\xff) S " + b"0 " * 18 + b"4200\n")

            with patch.object(records, "Path", return_value=proc):
                self.assertEqual(records.process_start_time(123), "4200")


class QueueCallerTests(unittest.TestCase):
    def test_an_unknown_worktree_does_not_get_a_terminal_prefix(self) -> None:
        caller = records.queue_caller("", "", "", "", "/private/unregistered session")
        self.assertEqual(caller.name, "unregistered session")
