"""Test the shared records for admission."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.build_queue import admission_records as records
from scripts.build_queue import admission_wire as wire, execution_identity, placement
from scripts.build_hold.owning_session import NoOwningSession
from scripts.build_targets.execution_transport import SshCommandTransport
from scripts.mac_test.claim_state import RepositoryScope, WHOLE_MAC


class RunRecordCodecTests(unittest.TestCase):
    def test_a_legacy_slot_uses_natedev_and_counts_as_used(self) -> None:
        old = ('{"kind":"VerifyRunSlot","repository":"fixture",'
               '"owner":{"kind":"Owner","pid":123,"start_time":"4200"}}')
        slot = wire.parse_record(old, wire.VerifyRunSlot)

        self.assertEqual(slot.destination, placement.PlacementDestination("local", "natedev", "natedev", WHOLE_MAC))
        self.assertEqual(slot.use, wire.UsedVerifySlot())
        self.assertEqual(slot.provider, wire.LegacyRunProviderUnrecorded())
        self.assertEqual(slot.settlement, wire.RunOpen())
        self.assertEqual(slot.launch, wire.RunLaunchUnrecorded())
        self.assertEqual(slot.generation, wire.RunGenerationUnrecorded())
        self.assertEqual(slot.key, "123-4200:fixture")
        self.assertEqual(wire.parse_record(wire.encode_record(slot), wire.VerifyRunSlot), slot)

    def test_a_pending_slot_retains_its_destination_and_reservations(self) -> None:
        scope = RepositoryScope("fixture", "fixture-key", 2)
        destination = placement.PlacementDestination("ssh", "linux-builder", "builder-host", scope, 2)
        slot = wire.VerifyRunSlot("fixture", wire.Owner(123, "4200"),
            wire.PendingVerifyGrants(frozenset({"first-grant", "second-grant"})), destination=destination)
        encoded = wire.encode_record(slot)

        self.assertEqual(wire.parse_record(encoded, wire.VerifyRunSlot), slot)

        old = wire.parse_record(encoded, dict[str, object])
        del old["destination"]
        local = wire.parse_record(wire.encode_record(old), wire.VerifyRunSlot)
        self.assertEqual(local, replace(slot, destination=wire.VerifyRunSlot("fixture", slot.owner).destination))

    def test_run_launch_states_survive_the_slot_codec(self) -> None:
        command = wire.RunCommandAttempt("first-step", "first-attempt")
        generation = wire.VerifyRunGeneration("local-generation", wire.Owner(124, "4201"))
        for launch in (wire.RunNeverLaunched(), wire.RunLaunchUnresolved(command), wire.RunLaunched()):
            with self.subTest(launch=launch):
                slot = wire.VerifyRunSlot("fixture", wire.Owner(123, "4200"),
                    provider=wire.LocalRunProvider(), launch=launch, generation=generation)
                self.assertEqual(wire.parse_record(wire.encode_record(slot), wire.VerifyRunSlot), slot)

    def test_remote_settlement_retains_the_provider_and_the_exact_operation(self) -> None:
        scope = RepositoryScope("fixture", "fixture-key", 2)
        destination = placement.PlacementDestination("mac", "mac", "mac-host", scope, 2)
        claim = placement.RunProviderClaimReference("mac", "mac", scope, "run-claim", "run-a", "placement-a")
        command = wire.RunCommandAttempt("nextest-step", "nextest-attempt")
        boot = wire.MacBootIdentity("mac-hardware-id", "mac-boot-id", 100_000_000)
        fence = wire.MacRunWatcherFence(command, placement.RunCommandBinding(claim, "nextest-fence"), boot)
        generation = wire.VerifyRunGeneration("placement-a", wire.Owner(124, "4201"))
        provider = wire.RemoteRunProvider("original-mac",
            SshCommandTransport(Path("/fixture/mac.conf")), claim,
            placement.ClaimHolderIdentity("natedev", 124, "4201"), watcher_fence=fence)
        for operation, launch in (
            (wire.RunClaimAcquisitionPending(claim), wire.RunNeverLaunched()),
            (wire.RunCommandSettlementPending(command), wire.RunLaunched()),
            (wire.RunClaimReleasePending(claim), wire.RunLaunched()),
            (wire.MacWatcherSettlementPending(fence), wire.RunLaunched()),
        ):
            with self.subTest(operation=operation):
                slot = wire.VerifyRunSlot("fixture", wire.Owner(123, "4200"), destination=destination,
                    provider=provider, settlement=wire.SettlementPending("run ended", operation),
                    launch=launch, generation=generation)
                self.assertEqual(wire.parse_record(wire.encode_record(slot), wire.VerifyRunSlot), slot)

        receipt = wire.MacWatcherReleaseReceipt(fence, 103.0)
        self.assertEqual(wire.parse_record(wire.encode_record(receipt), wire.MacWatcherReleaseReceipt), receipt)

        for unconfirmed in (
            wire.MacRunWatcherFenceUnrecorded(),
            wire.MacRunWatcherBootUnrecorded(command, fence.binding),
        ):
            with self.subTest(unconfirmed=unconfirmed):
                raw = {"kind": "MacWatcherSettlementPending", "fence": unconfirmed}
                with self.assertRaises(ValueError):
                    _ = wire.parse_record(wire.encode_record(raw), wire.MacWatcherSettlementPending)

    def test_mac_watcher_handoff_rejects_invalid_boot_identity(self) -> None:
        scope = RepositoryScope("fixture", "fixture-key", 2)
        claim = placement.RunProviderClaimReference("mac", "mac", scope, "run-claim", "run-a", "placement-a")
        command = wire.RunCommandAttempt("nextest-step", "nextest-attempt")
        binding = placement.RunCommandBinding(claim, "nextest-fence")
        for hardware_id, boot_id, boot_at_us in (
            ("", "mac-boot-id", 100_000_000),
            (" ", "mac-boot-id", 100_000_000),
            ("mac-hardware-id", "", 100_000_000),
            ("mac-hardware-id", " ", 100_000_000),
            ("mac-hardware-id", "mac-boot-id", 0),
            ("mac-hardware-id", "mac-boot-id", -1),
            ("mac-hardware-id", "mac-boot-id", True),
        ):
            with self.subTest(hardware_id=hardware_id, boot_id=boot_id, boot_at_us=boot_at_us):
                with self.subTest(source="constructor"), self.assertRaises(ValueError):
                    _ = wire.MacBootIdentity(hardware_id, boot_id, boot_at_us)

                saved = {
                    "kind": "MacWatcherSettlementPending",
                    "fence": {
                        "kind": "MacRunWatcherFence",
                        "command": command,
                        "binding": binding,
                        "boot": {"kind": "MacBootIdentity", "hardware_id": hardware_id,
                                 "boot_id": boot_id, "boot_at_us": boot_at_us},
                    },
                }
                with self.subTest(source="snapshot"), self.assertRaises(ValueError):
                    _ = wire.parse_record(wire.encode_record(saved), wire.MacWatcherSettlementPending)

    def test_legacy_mac_watcher_handoff_keeps_the_claim_without_release_authority(self) -> None:
        scope = RepositoryScope("fixture", "fixture-key", 2)
        claim = placement.RunProviderClaimReference("mac", "mac", scope, "run-claim", "run-a", "placement-a")
        command = wire.RunCommandAttempt("nextest-step", "nextest-attempt")
        binding = placement.RunCommandBinding(claim, "nextest-fence")
        transport = SshCommandTransport(Path("/fixture/mac.conf"))
        holder = placement.ClaimHolderIdentity("natedev", 124, "4201")
        old = {
            "kind": "VerifyRunSlot",
            "repository": "fixture",
            "owner": wire.Owner(123, "4200"),
            "destination": placement.PlacementDestination("mac", "mac", "mac-host", scope, 2),
            "provider": {"kind": "RemoteRunProvider", "endpoint": "original-mac", "transport": transport,
                         "claim": claim, "holder": holder,
                         "watcher_fence": {"kind": "MacRunWatcherFence", "command": command, "binding": binding}},
            "settlement": {"kind": "SettlementPending", "reason": "lost Mac",
                           "operation": {"kind": "MacWatcherSettlementPending", "claim": claim}},
        }

        slot = wire.parse_record(wire.encode_record(old), wire.VerifyRunSlot)

        self.assertEqual(slot.provider, wire.RemoteRunProvider("original-mac", transport, claim, holder,
            watcher_fence=wire.MacRunWatcherBootUnrecorded(command, binding)))
        self.assertEqual(slot.settlement, wire.SettlementPending("lost Mac", wire.MacWatcherHandoffUnconfirmed(claim)))
        self.assertEqual(wire.parse_record(wire.encode_record(slot), wire.VerifyRunSlot), slot)

    def test_legacy_remote_slot_preserves_the_attempt_claim_and_fence(self) -> None:
        scope = RepositoryScope("fixture", "fixture-key", 2)
        claim = placement.ProviderClaimReference("mac", "mac", scope, "old-claim", "run-a", "old-attempt")
        transport = SshCommandTransport(Path("/fixture/mac.conf"))
        holder = placement.ClaimHolderIdentity("natedev", 123, "4200")
        settlement = wire.SettlementPending("cancelled", wire.RunClaimReleasePending(claim))
        old = {
            "kind": "VerifyRunSlot",
            "repository": "fixture",
            "owner": wire.Owner(123, "4200"),
            "destination": placement.PlacementDestination("mac", "mac", "mac-host", scope, 2),
            "provider": {"kind": "RemoteRunProvider", "endpoint": "original-mac", "transport": transport,
                         "holder": holder, "claim": claim, "start_fence_id": "old-fence"},
            "settlement": settlement,
        }

        slot = wire.parse_record(wire.encode_record(old), wire.VerifyRunSlot)

        self.assertEqual(slot.provider, wire.RemoteRunProvider("original-mac", transport,
            wire.LegacyAttemptBoundRunClaim(claim, "old-fence"), holder))
        self.assertEqual(slot.settlement, settlement)
        self.assertEqual(wire.parse_record(wire.encode_record(slot), wire.VerifyRunSlot), slot)

    def test_two_commands_preserve_one_run_claim_and_distinct_permissions_and_fences(self) -> None:
        scope = RepositoryScope("fixture", "fixture-key", 2)
        destination = placement.PlacementDestination("mac", "mac", "mac-host", scope, 2)
        claim = placement.RunProviderClaimReference("mac", "mac", scope, "run-claim", "run-a", "placement-a")
        assignment = placement.PlacementAssignment("check-step", "check-attempt", destination,
            execution_identity.SourceKeyKnown("source"), placement.SubmittingOwner("natedev", 123, "4200"),
            NoOwningSession(), 100.0)
        check = records.SavedPlacementAssignment(assignment, permission=placement.PermissionConsumed(101.0),
            run_command=placement.RunCommandBinding(claim, "check-fence"))
        nextest = records.SavedPlacementAssignment(replace(assignment, episode="nextest-step", attempt="nextest-attempt"),
            run_command=placement.RunCommandBinding(claim, "nextest-fence"))

        saved = {"check": check, "nextest": nextest}
        self.assertEqual(wire.parse_record(wire.encode_record(saved), dict[str, records.SavedPlacementAssignment]), saved)

        old = wire.parse_record(wire.encode_record(check), dict[str, object])
        del old["run_command"]
        self.assertEqual(wire.parse_record(wire.encode_record(old), records.SavedPlacementAssignment),
            replace(check, run_command=placement.NoRunCommandBinding()))

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
