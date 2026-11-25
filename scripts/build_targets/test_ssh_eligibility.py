"""Check SSH routing before reservation with private target state."""

from __future__ import annotations

from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from decimal import Decimal
from datetime import datetime, timedelta, timezone
from functools import partial
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from typing import cast, override

from scripts.build_hold.owning_session import NoOwningSession
from scripts.lib import process_start as process_identity
from scripts.lib.process_start_test_support import process_reader
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS

import unittest
from unittest import mock

from scripts.build_targets import build_targets as build_targets, target_config, targets
from scripts.build_targets.test_targets import fixture_mac_mirror
from scripts.mac_test import eligibility, claim_state, mac_test as mac_test, offload as offload
from scripts.build_hold import owning_session as session_owner
from scripts.aws_target import ledger
from scripts.aws_target.ledger_test_support import REAL_POPEN, instant
from scripts.lint.memory_test_support import UNREACHABLE_WAIT_FIXTURE, config_environment, write_config_fixture


class SshEligibilityTests(unittest.TestCase):
    root: Path = Path()
    commands: list[list[str]] = []
    reachable: bool = True
    now: int = instant("2026-01-15T00:00:00+00:00")
    notices: list[ledger.LimitEvent] = []

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.commands = []
        self.reachable = True
        self.notices = []
        self.now = instant("2026-01-15T00:00:00+00:00")
        environment = config_environment(self.root)
        _ = (self.root / "build_targets.conf").write_text(
            "targets=mac,natedev,linux\nkind.mac=ssh\nplatform.mac=macos\nsettings.mac=mac_test.conf\n"
            + "kind.natedev=local\nplatform.natedev=linux\n"
            + "kind.linux=ssh\nplatform.linux=linux\nsettings.linux=linux.conf\n" + UNREACHABLE_WAIT_FIXTURE)
        _ = write_config_fixture(self.root / "mac_test.conf", "offload=on\nlinux_only.hana=linux-part\n")
        _ = write_config_fixture(self.root / "linux.conf",
                              "offload=on\nhost=fake-linux\nkinds=test,check,doc\n", template="mac_test")
        _ = (self.root / "Cargo.toml").write_text('[package]\nname="linux-part"\nversion="0.1.0"\n')
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {
            **environment, "HOME": str(self.root), "MAC_TEST_CONFIG": str(self.root / "mac_test.conf"),
            "MAC_TEST_STATE_DIR": str(self.root / "mac-state"),
            "BUILDLOG_DIR": str(self.root / "buildlog"), "BUILD_HOLD_DIR": str(self.root / "holds"),
            "BUILD_HOLD_RELEASE_DIR": str(self.root / "releases"),
            "NOTIFIER_SESSIONS_DIR": str(self.root / "sessions"),
        })))
        _ = self.enterContext(mock.patch.object(Path, "home", return_value=self.root))
        key = "https://github.com/team/hana"
        _ = self.enterContext(mock.patch.object(eligibility, "repository_identity",
                                               return_value=eligibility.KnownRepository("hana", key)))
        _ = self.enterContext(mock.patch.object(claim_state, "mac_repository_identity",
                                               return_value=fixture_mac_mirror(self.root, "hana", key)))
        _ = self.enterContext(mock.patch.object(mac_test, "target_claim_scope",
                                               return_value=mac_test.RepositoryScope("hana", key)))
        _ = self.enterContext(mock.patch.object(process_identity, "process_start", side_effect=process_reader(self.process_start)))
        _ = self.enterContext(mock.patch.object(mac_test, "start_claim_watcher"))
        _ = self.enterContext(mock.patch.object(session_owner, "owning_session", return_value=NoOwningSession()))
        book = ledger.CostLedger(mac_test.state_paths("linux").directory)
        book.record_inventory(ledger.ResourceInventory(self.now, (
            ledger.PricedResource("retained-storage", Decimal("0")),), "priced fixture inventory"))
        book.observe(ledger.InstanceObservation(self.now, "stopped", "fake DescribeInstances"))
        _ = self.enterContext(mock.patch.object(
            ledger, "spending_guard",
            new=partial(ledger.spending_guard, clock=lambda: self.now, notify=self.notice)))
        _ = self.enterContext(mock.patch.object(subprocess, "run", side_effect=AssertionError("Unexpected external command")))
        _ = self.enterContext(mock.patch.object(subprocess, "Popen", side_effect=AssertionError("Unexpected child process")))
        _ = self.enterContext(mock.patch.object(offload.ProcessController, "capture", side_effect=self.capture))
        _ = self.enterContext(mock.patch.object(offload, "probe_mac", side_effect=AssertionError("No execution probe before reservation")))

    def process_start(self, pid: int) -> str:
        return f"start-{pid}"

    def notice(self, event: ledger.LimitEvent) -> bool:
        self.notices.append(event)
        return True

    def capture(self, arguments: Sequence[str],
                _deadline: offload.CommandDeadline = offload.NO_COMMAND_TIME_LIMIT) -> offload.CommandOutput:
        argv = list(arguments)
        self.commands.append(argv)
        self.assertEqual(argv[0], "ssh")
        self.assertEqual(argv[-1], "true", "Routing must not evaluate a shell")
        return offload.CommandOutput(0 if self.reachable else 255, "", "")

    def answer(self, kind: str = "test", local: bool = False) -> list[list[str]]:
        output = io.StringIO()
        arguments = ["build_targets.py", "eligible", "--kind", kind, "--repo-root", str(self.root),
                     "--package", "linux-part"]
        if local:
            arguments += ["--local", "requires local files"]
        with mock.patch.object(sys, "argv", arguments), redirect_stdout(output):
            self.assertEqual(build_targets.main(), 0)
        return [line.split("\t") for line in output.getvalue().splitlines()[1:]]

    def mac_offload_off(self) -> None:
        # check, lint and doc ignore linux_only, so turn the Mac off to keep these Linux tests from probing it.
        _ = write_config_fixture(self.root / "mac_test.conf", "offload=off\nlinux_only.hana=linux-part\n")

    def linux(self, kind: str = "test", local: bool = False) -> list[str]:
        return next(row for row in self.answer(kind, local) if row[0] == "linux")

    def occupy(self, pid: int) -> None:
        args = mac_test.CliArguments()
        args.target, args.worktree, args.pid = "linux", str(self.root), pid
        args.what, args.remote = "check hana", "fake-linux"
        with redirect_stdout(io.StringIO()):
            self.assertEqual(mac_test.command_claim(args, mac_test.state_paths("linux")), 0)

    def decision(self) -> str:
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(build_targets.command_kind_eligible(
                self.root, "linux-part", "test", targets.ANY_ELIGIBLE_TARGET), 0)
        return output.getvalue().splitlines()[0]

    def reserve(self, name: str = "linux", *, recover_only: bool = False) -> int:
        args = build_targets.CliArguments()
        args.kind, args.repo_root, args.package = "test", str(self.root), "linux-part"
        args.pid, args.target = 501, name
        args.reservation_file = str(self.root / "reservation.tsv")
        args.claim_identity_file = str(self.root / "reservation.identity")
        args.recover_only = recover_only
        return build_targets.command_reserve(args)

    def add_mac_linux_guest(self) -> target_config.SshTarget:
        registry = self.root / "build_targets.conf"
        _ = registry.write_text(registry.read_text().replace("targets=mac,natedev,linux\n", "targets=mac,natedev,linux,mac-linux\n")
            + "kind.mac-linux=ssh\nplatform.mac-linux=linux\nsettings.mac-linux=mac_linux.conf\n")
        _ = (self.root / "mac_linux.conf").write_text("ssh.hops=mac linux-guest\nguest.user=gate-runner\n")
        return target_config.configured_ssh_target(eligibility.MAC_LINUX_TARGET)

    def test_guest_execution_settings_refuse_before_transport_read(self) -> None:
        """Linux target eligibility row 2."""
        guest = self.add_mac_linux_guest()
        with (mock.patch.object(target_config, "read_execution_transport",
                               wraps=target_config.read_execution_transport) as transport,
              mock.patch.object(build_targets, "start_probe_recovery") as recovery):
            result = build_targets.ssh_target_config(guest)
        self.assertEqual(result, targets.ExcludedTarget(guest, "The Mac Linux guest requires an exact queue assignment"))
        transport.assert_not_called()
        recovery.assert_not_called()
        self.assertEqual(self.commands, [])

    def test_linux_transport_is_read_again_before_execution_settings(self) -> None:
        """Linux target eligibility row 3."""
        from scripts.aws_target import hana_placement

        linux = target_config.configured_ssh_target("linux")
        calls = mock.Mock()
        with (mock.patch.object(target_config, "read_execution_transport",
                               wraps=target_config.read_execution_transport) as first_transport,
              mock.patch.object(hana_placement, "read_execution_transport",
                               wraps=hana_placement.read_execution_transport) as second_transport,
              mock.patch.object(eligibility, "read_config", wraps=eligibility.read_config) as config,
              mock.patch.object(build_targets, "start_probe_recovery") as recovery):
            calls.attach_mock(first_transport, "dispatch_transport")
            calls.attach_mock(second_transport, "execution_transport")
            calls.attach_mock(config, "execution_settings")
            result = build_targets.ssh_target_eligibility(
                linux, self.root, "linux-part", "check", targets.ANY_ELIGIBLE_TARGET)
        self.assertEqual(result, targets.EligibleTarget(linux))
        self.assertEqual([call[0] for call in calls.mock_calls],
                         ["dispatch_transport", "execution_transport", "dispatch_transport", "execution_settings"])
        recovery.assert_not_called()

    def test_inactive_backoff_returns_before_target_lookup(self) -> None:
        """command_probe_backoff row 1."""
        with (mock.patch.object(eligibility, "read_backoff", wraps=eligibility.read_backoff) as backoff,
              mock.patch.object(build_targets, "ssh_target_config") as settings,
              mock.patch.object(offload, "record_reachable") as reachable):
            self.assertEqual(build_targets.command_probe_backoff("linux", probe=True), 0)
        backoff.assert_called_once()
        settings.assert_not_called()
        reachable.assert_not_called()
        self.assertEqual(self.commands, [])

    def test_active_backoff_without_probe_skips_settings_and_publication(self) -> None:
        """command_probe_backoff row 2."""
        path = eligibility.backoff_path("linux")
        deadline = f"{self.now + 3600}\n"
        _ = path.write_text(deadline)
        with (mock.patch.object(time, "time", return_value=float(self.now)),
              mock.patch.object(build_targets, "ssh_target_config") as settings,
              mock.patch.object(offload, "record_reachable") as reachable):
            self.assertEqual(build_targets.command_probe_backoff("linux", probe=False), 1)
        settings.assert_not_called()
        reachable.assert_not_called()
        self.assertEqual(self.commands, [])
        self.assertEqual(path.read_text(), deadline)

    def test_failed_backoff_probe_preserves_the_mark(self) -> None:
        """command_probe_backoff row 4."""
        self.reachable = False
        path = eligibility.backoff_path("linux")
        deadline = f"{self.now + 3600}\n"
        _ = path.write_text(deadline)
        calls = mock.Mock()
        with (mock.patch.object(time, "time", return_value=float(self.now)),
              mock.patch.object(time, "time_ns", return_value=self.now * 1_000_000_000) as clock,
              mock.patch.object(offload, "probe_reachability", wraps=offload.probe_reachability) as probe,
              mock.patch.object(offload, "record_reachable") as reachable):
            calls.attach_mock(clock, "clock")
            calls.attach_mock(probe, "probe")
            self.assertEqual(build_targets.command_probe_backoff("linux", probe=True), 1)
        self.assertEqual([call[0] for call in calls.mock_calls], ["clock", "probe"])
        reachable.assert_not_called()
        self.assertEqual(len(self.commands), 1)
        self.assertEqual(path.read_text(), deadline)

    def test_recovery_returns_before_claim_when_registration_excludes_transport(self) -> None:
        """Probe recovery start and worker row 6."""
        guest = self.add_mac_linux_guest()
        args = build_targets.CliArguments()
        args.target = guest.name
        args.kind = "check"
        args.repo_root = str(self.root)
        args.recovery_fd = os.open(os.devnull, os.O_RDONLY)
        with (mock.patch.object(build_targets, "ssh_target_config", wraps=build_targets.ssh_target_config) as settings,
              mock.patch.object(claim_state, "locked", wraps=claim_state.locked) as lock,
              mock.patch.object(offload, "claim_for_run") as claim):
            self.assertEqual(build_targets.command_recover_probe(args), 0)
        settings.assert_called_once_with(guest)
        lock.assert_not_called()
        claim.assert_not_called()
        self.assertEqual(self.commands, [])

    def test_recovery_missing_mirror_returns_before_claim_lock(self) -> None:
        """Probe recovery start and worker row 7."""
        args = build_targets.CliArguments()
        args.target = "mac"
        args.kind = "check"
        args.repo_root = str(self.root)
        args.recovery_fd = os.open(os.devnull, os.O_RDONLY)
        with (mock.patch.object(claim_state, "mac_repository_identity", return_value=claim_state.WHOLE_MAC) as mirror,
              mock.patch.object(claim_state, "locked", wraps=claim_state.locked) as lock,
              mock.patch.object(offload, "claim_for_run") as claim):
            self.assertEqual(build_targets.command_recover_probe(args), 0)
        mirror.assert_called_once_with(str(self.root))
        lock.assert_not_called()
        claim.assert_not_called()
        self.assertEqual(self.commands, [])

    def test_fixed_candidates_apply_explicit_settings_only_to_the_pin(self) -> None:
        """Fixed placement candidates row 3."""
        from scripts.build_queue import placement
        from scripts.direct.step_selection import Pinned

        alternate = self.root / "alternate.conf"
        _ = write_config_fixture(alternate, "host=alternate-fixture\noffload=on\nkinds=check\n", template="mac_test")
        explicit = placement.ExplicitProviderSettings(alternate)
        with mock.patch.object(build_targets, "fixed_remote_candidates",
                               wraps=build_targets.fixed_remote_candidates) as remote:
            result = build_targets.fixed_placement_constraints(
                self.root, "linux-part", "check", ("cargo", "check"), NoOwningSession(), "source",
                selection=Pinned("linux"), provider_settings=explicit)
        candidate, = result.candidates
        self.assertEqual(candidate.destination.host, "alternate-fixture")
        self.assertEqual(candidate.settings, explicit)
        remote.assert_called_once()

    def test_fixed_ssh_candidate_resolves_state_after_reading_transport(self) -> None:
        """Fixed placement candidates row 11."""
        from scripts.direct.step_selection import Pinned

        trace = mock.Mock()
        with (mock.patch.object(target_config, "read_execution_transport",
                               wraps=target_config.read_execution_transport) as transport,
              mock.patch.object(claim_state, "state_paths", wraps=claim_state.state_paths) as paths):
            trace.attach_mock(transport, "transport")
            trace.attach_mock(paths, "paths")
            result = build_targets.fixed_placement_constraints(
                self.root, "linux-part", "check", ("cargo", "check"), NoOwningSession(), "source",
                selection=Pinned("linux"))
        self.assertEqual([call[0] for call in trace.mock_calls], ["transport", "paths"])
        paths.assert_called_once_with("linux")
        candidate, = result.candidates
        self.assertEqual(candidate.destination.target, "linux")

    def test_fixed_ssh_candidate_reads_host_before_local_exclusion(self) -> None:
        """Fixed placement candidates row 13."""
        from scripts.build_queue import placement
        from scripts.direct.step_selection import Pinned

        linux = target_config.configured_ssh_target("linux")
        candidates = build_targets.fixed_placement_constraints(
            self.root, "linux-part", "check", ("cargo", "check"), NoOwningSession(), "source",
            routing=targets.LocalRequired("fixture requires local execution"), selection=Pinned(linux.name))
        candidate = next(candidate for candidate in candidates.candidates if candidate.destination.target == linux.name)
        self.assertEqual(candidate.destination.host, "fake-linux")
        self.assertEqual(candidate.eligibility, placement.FixedExclusion("fixture requires local execution"))

    def test_mac_linux_guest_config_is_excluded_without_offload_settings(self) -> None:
        guest = self.add_mac_linux_guest()
        config = build_targets.ssh_target_config(guest)
        self.assertEqual(config, targets.ExcludedTarget(guest, "The Mac Linux guest requires an exact queue assignment"))

    def test_probe_of_the_mac_linux_guest_reads_its_state_without_a_probe(self) -> None:
        guest = self.add_mac_linux_guest()
        self.assertIsInstance(build_targets.probe_target(guest), targets.Free)
        self.assertEqual(self.commands, [])

    def test_eligible_kind_with_the_mac_linux_guest_answers_for_every_target(self) -> None:
        _ = self.add_mac_linux_guest()
        self.mac_offload_off()
        output = io.StringIO()
        arguments = ["build_targets.py", "eligible", "--kind", "test", "--repo-root", str(self.root), "--package", "linux-part"]
        with mock.patch.object(sys, "argv", arguments), redirect_stdout(output):
            self.assertEqual(build_targets.main(), 0)
        self.assertNotIn("natedev_only state unreadable", output.getvalue())
        rows = {row[0]: row for row in (line.split("\t") for line in output.getvalue().splitlines()[1:])}
        self.assertEqual(rows["mac-linux"][1], "excluded")
        self.assertIn("linux", rows)

    def test_probe_backoff_keeps_the_mac_linux_guest_backoff_without_a_probe(self) -> None:
        _ = self.add_mac_linux_guest()
        backoff = eligibility.backoff_path(eligibility.MAC_LINUX_TARGET)
        backoff.parent.mkdir(parents=True, exist_ok=True)
        _ = backoff.write_text(f"{self.now + 3600}\n")
        with mock.patch.object(time, "time", return_value=float(self.now)):
            self.assertEqual(build_targets.command_probe_backoff(eligibility.MAC_LINUX_TARGET, probe=True), 1)
        self.assertEqual(self.commands, [])

    def test_a_mac_block_stops_a_memory_wait_from_reusing_its_reservation(self) -> None:
        _ = write_config_fixture(self.root / "mac_test.conf", "offload=on\nlinux_only.hana=\n")
        self.assertEqual(self.reserve("mac"), 0)
        paths = mac_test.state_paths("mac")
        claim, = mac_test.read_claims(paths)
        for published in (True, False):
            with self.subTest(existing_wait_claim=True, reservation_published=published):
                if not published:
                    (self.root / "reservation.tsv").unlink()
                self.assertEqual(self.reserve("mac"), 0)
                self.assertEqual(mac_test.read_claims(paths)[0].record, claim.record)
        self.commands.clear()
        block = mac_test.ActiveMacBlock(
            "assembly", "assembly checks", datetime.now(timezone.utc),
            datetime.now(timezone.utc) + timedelta(hours=1),
            mac_test.RecordedSession("assembly-session", "assembly"), mac_test.NO_SHOWRUNNER,
            "off_by_this_block", mac_test.FREE_MESSAGE_NOT_NEEDED)
        mac_test.write_block(paths.block, block)
        for published in (True, False):
            with self.subTest(reservation_published=published):
                destination = self.root / "reservation.tsv"
                if not published:
                    destination.unlink(missing_ok=True)
                self.assertEqual(self.reserve("mac"), 1)
                self.assertFalse(destination.exists())
                retained, = mac_test.read_claims(paths)
                self.assertEqual(retained.record, claim.record)

        for refusal, remote_cleanup, refusal_status in (("blocked", False, 1), ("backoff", False, 1),
                                                        ("blocked", True, 0)):
            record = dict(claim.record)
            if remote_cleanup:
                record.update(remote_host="fake-mac", cleanup_pending=True)
            _ = claim.path.write_text(json.dumps(record))
            if refusal == "blocked":
                eligibility.backoff_path("mac").unlink(missing_ok=True)
                mac_test.write_block(paths.block, block)
            else:
                paths.block.unlink(missing_ok=True)
                offload.write_backoff(10**12, "mac")
            for published in (False, True):
                expected = 0 if refusal == "backoff" and published else refusal_status
                with self.subTest(refusal=refusal, cleanup_pending=remote_cleanup,
                                  state_lock_held=True, reservation_published=published):
                    destination = self.root / "reservation.tsv"
                    if published:
                        _ = destination.write_text(f"mac\t{self.root / 'mac_test.conf'}\n")
                    else:
                        destination.unlink(missing_ok=True)
                    command = [sys.executable, "-c",
                               "import fcntl,sys; from pathlib import Path; "
                               + "lock=Path(sys.argv[1]).open('a+'); fcntl.flock(lock,fcntl.LOCK_EX); "
                               + "print('locked',flush=True); sys.stdin.read()", str(paths.lock)]
                    with REAL_POPEN(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True) as holder:
                        assert holder.stdout is not None and holder.stdin is not None
                        self.assertEqual(holder.stdout.readline(), "locked\n")
                        with ThreadPoolExecutor(max_workers=1) as executor:
                            pending = executor.submit(self.reserve, "mac")
                            try:
                                self.assertEqual(pending.result(timeout=TEST_HANG_GUARD_SECS), expected)
                                self.assertEqual(destination.exists(), expected == 0)
                                self.assertEqual(mac_test.read_claims(paths)[0].record, record)
                            finally:
                                holder.stdin.close()
                                _ = holder.wait(timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(self.commands, [])


    def add_spare_target(self) -> None:
        _ = write_config_fixture(self.root / "spare.conf",
                                 "offload=on\nhost=fake-spare\nkinds=test,check,doc\n", template="mac_test")
        table = self.root / "build_targets.conf"
        _ = table.write_text(table.read_text().replace("targets=mac,natedev,linux", "targets=mac,natedev,linux,spare")
                             + "kind.spare=ssh\nplatform.spare=linux\nsettings.spare=spare.conf\n")

    def test_wait_release_matches_the_acquired_attempt(self) -> None:
        self.assertEqual(self.reserve(), 0)
        paths = mac_test.state_paths("linux")
        fields = (self.root / "reservation.identity").read_text().split("\t")
        previous = claim_state.ClaimIdentity(fields[0], fields[1], fields[2])
        self.assertEqual(build_targets.command_release_wait(501, identity=previous), 0)
        (self.root / "reservation.tsv").unlink()
        self.assertEqual(self.reserve(), 0)
        current, = mac_test.read_claims(paths)
        fields = (self.root / "reservation.identity").read_text().split("\t")
        acquired = claim_state.ClaimIdentity(fields[0], fields[1], fields[2])
        for delayed in (previous,
                        claim_state.ClaimIdentity(previous.claim_id, acquired.episode, acquired.attempt),
                        claim_state.ClaimIdentity(acquired.claim_id, previous.episode, acquired.attempt),
                        claim_state.ClaimIdentity(acquired.claim_id, acquired.episode, previous.attempt)):
            with self.subTest(identity=delayed):
                self.assertEqual(build_targets.command_release_wait(501, identity=delayed), 0)
                retained, = mac_test.read_claims(paths)
                self.assertEqual(retained.record, current.record)
        self.assertEqual(build_targets.command_release_wait(501, identity=acquired), 0)
        self.assertEqual(mac_test.read_claims(paths), ())


    def test_recovery_only_does_not_create_a_new_claim(self) -> None:
        self.assertEqual(self.reserve(recover_only=True), 1)
        self.assertFalse((self.root / "reservation.tsv").exists())
        self.assertEqual(mac_test.read_claims(mac_test.state_paths("linux")), ())

    def test_busy_mac_recovery_keeps_the_existing_reserve_status(self) -> None:
        _ = write_config_fixture(self.root / "mac_test.conf", "offload=on\nlinux_only.hana=\n")
        self.assertEqual(self.reserve("mac", recover_only=True), 11)
        self.assertFalse((self.root / "reservation.tsv").exists())


    def test_linux_backoff_keeps_the_memory_leave_route(self) -> None:
        offload.write_backoff(10**12, "linux")
        self.assertEqual(self.decision(), "any_target")
        self.assertEqual(self.commands, [])

    def test_unreachable_linux_target_keeps_the_memory_leave_route(self) -> None:
        self.reachable = False
        self.assertEqual(self.decision(), "any_target")
        self.assertTrue(self.commands)

    def test_disk_refusal_keeps_the_memory_leave_route(self) -> None:
        with mock.patch.object(mac_test, "free_now", return_value="disk"):
            self.assertEqual(self.decision(), "any_target")
        self.assertEqual(self.commands, [])

    def test_capacity_exclusions_keep_the_memory_leave_route_available(self) -> None:
        self.occupy(71)
        self.assertEqual(self.decision(), "any_target")
        with mock.patch.object(mac_test, "free_now", return_value="blocked"):
            self.assertEqual(self.decision(), "any_target")
        self.assertEqual(self.commands, [])

    def test_linux_only_work_keeps_linux_capacity_without_a_mac(self) -> None:
        rows = self.answer()
        self.assertEqual([row[0] for row in rows], ["mac", "natedev", "linux"])
        self.assertEqual(rows[0][1:3], ["excluded", "linux_only"])
        self.assertEqual(rows[2][1:3], ["eligible", "-"])
        self.assertEqual([row[4] for row in rows], ["macos", "-", "linux"])
        self.assertTrue(self.commands)

    def test_linux_test_check_and_doc_need_no_mac_repository_setting(self) -> None:
        self.mac_offload_off()
        for kind in ("test", "check", "doc"):
            with self.subTest(kind=kind):
                self.assertEqual(self.linux(kind)[1], "eligible")

    def test_local_only_request_excludes_every_ssh_target(self) -> None:
        rows = self.answer(local=True)
        self.assertEqual([(row[0], row[1]) for row in rows],
                         [("mac", "excluded"), ("natedev", "eligible"), ("linux", "excluded")])
        self.assertEqual(self.commands, [])

    def test_artifacts_final_gate_and_lint_never_use_linux_ssh(self) -> None:
        self.mac_offload_off()
        for kind in ("build", "example", "final", "lint"):
            with self.subTest(kind=kind):
                self.assertEqual(self.linux(kind)[1], "excluded")
        self.assertEqual(self.commands, [])

    def test_configured_kinds_are_read_on_each_eligibility_check(self) -> None:
        self.mac_offload_off()
        self.assertEqual(self.linux("check")[1], "eligible")
        _ = write_config_fixture(self.root / "linux.conf", "offload=on\nkinds=test,doc\n")
        self.assertEqual(self.linux("check")[1], "excluded")

    def test_mac_backoff_does_not_exclude_linux_but_linux_backoff_does(self) -> None:
        offload.write_backoff(10**12, "mac")
        self.assertEqual(self.linux()[1], "eligible")
        self.commands.clear()
        offload.write_backoff(10**12, "linux")
        self.assertEqual(self.linux()[1:3], ["excluded", "backoff"])
        self.assertEqual(self.commands, [])

    def test_occupied_repository_excludes_target_before_ssh(self) -> None:
        self.occupy(71)
        self.assertEqual(self.linux()[1:3], ["excluded", "busy"])
        self.assertEqual(self.commands, [])

    def test_unreachable_host_is_excluded_without_shell_evaluation(self) -> None:
        self.reachable = False
        self.assertEqual(self.linux()[1:3], ["excluded", "unreachable"])
        self.assertTrue(self.commands)

    def test_queue_retries_never_evaluate_or_reserve_a_shell(self) -> None:
        with mock.patch.object(mac_test, "command_claim", side_effect=AssertionError("Routing cannot reserve")):
            for _ in range(3):
                self.assertEqual(self.linux()[1], "eligible")
        self.assertTrue(self.commands)
        self.assertEqual(mac_test.read_claims(mac_test.state_paths("linux")), ())


if __name__ == "__main__":
    _ = unittest.main()
