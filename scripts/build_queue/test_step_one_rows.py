"""Check provider evidence and disk recovery with private admission state."""
from __future__ import annotations

from collections.abc import Callable
from contextlib import redirect_stderr
from dataclasses import replace
from io import StringIO
import json
from pathlib import Path
import subprocess
import threading
import time
from typing import cast
import unittest
from unittest import mock

from scripts.build_queue import admission_protocol as wire, admission_service as service, execution_identity, placement
from scripts.build_queue import test_admission_service as service_fixtures, test_placement as fixtures
from scripts.build_targets import build_targets, mac_linux
from scripts.lib import conf
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.lint.memory_test_support import UNREACHABLE_WAIT_FIXTURE
from scripts.mac_test import claim_state, eligibility, offload


class StepOneRows(unittest.TestCase):
    def read_provider(self, fixture: fixtures.PlacementFixture,
                      request: wire.PlacementRequest) -> placement.HostEvidence:
        destination = request.constraints.candidates[0].destination
        key = fixture.server.host_evidence_key(request, destination)
        fixture.server.host_requests[key] = request
        return fixture.read_host_evidence(key)

    def guest_request(self, fixture: fixtures.PlacementFixture) -> wire.PlacementRequest:
        _ = fixtures.mac_linux_fixture(fixture)
        request = fixtures.placement_request(fixture)
        return replace(request, constraints=replace(request.constraints, candidates=(fixtures.mac_linux_candidate(),)))

    def test_provider_evidence_includes_pending_disk_recovery(self) -> None:
        """Admission provider dispatch row 16."""
        owner = service_fixtures.LocalHoldPlacementTests()
        with owner.mac_disk_sweep_fixture() as fixture:
            server = fixture.admission.server
            reading = server.read_provider_evidence(fixture.key)
            self.assertIsInstance(reading, service.MacDiskRecoveryPendingEvidence)
            assert isinstance(reading, service.MacDiskRecoveryPendingEvidence)
            self.assertEqual(reading.key, fixture.key)
            self.assertEqual(reading.disk, offload.MacDiskShort(59, 60))
            self.assertEqual(reading.eligibility, placement.TemporaryExclusion("disk"))
            self.assertTrue(fixture.started.wait(TEST_HANG_GUARD_SECS))

    def request_recovery(self, server: service.AdmissionServer, sweep: Callable[[], None]
                         ) -> build_targets.MacDiskRecoveryState:
        request = cast(build_targets.RequestMacDiskRecovery, getattr(server, "_request_mac_disk_recovery"))
        return request("fixture-mac", build_targets.MacDiskSweep(offload.MacDiskShort(59, 60), sweep))

    def recovery_threads(self) -> tuple[list[threading.Thread], Callable[[threading.Thread], None]]:
        threads: list[threading.Thread] = []
        start = threading.Thread.start

        def record(thread: threading.Thread) -> None:
            if thread.name == "mac-disk-recovery":
                threads.append(thread)
            start(thread)

        return threads, record

    def test_recovery_reports_io_error_from_outside_the_map_lock(self) -> None:
        """Admission Mac disk recovery callback row 3."""
        with fixtures.PlacementFixture() as fixture:
            server = fixture.server
            threads, record = self.recovery_threads()

            def sweep() -> None:
                if server.mac_recovery_lock.acquire(timeout=TEST_HANG_GUARD_SECS):
                    server.mac_recovery_lock.release()
                    raise OSError("fixture sweep failed")
                raise ValueError("sweep held the map lock")

            with mock.patch.object(threading.Thread, "start", record), redirect_stderr(StringIO()) as errors:
                self.assertEqual(self.request_recovery(server, sweep),
                                 build_targets.MacDiskRecoveryActive(offload.MacDiskShort(59, 60)))
                thread, = threads
                thread.join(TEST_HANG_GUARD_SECS)
                self.assertFalse(thread.is_alive())
            self.assertIn("Mac disk recovery failed: fixture sweep failed", errors.getvalue())

    def test_recovery_completion_clears_active_and_expedites_mac(self) -> None:
        """Admission Mac disk recovery callback row 4."""
        with fixtures.PlacementFixture() as fixture:
            server = fixture.server
            threads: list[threading.Thread] = []
            completion_lock: list[bool] = []
            recovery_cleared: list[bool] = []
            mac, ssh = (mock.Mock(spec=service.HostEvidenceWorker) for _index in range(2))
            keys = (("mac", "fixture-mac"), ("ssh", "fixture-linux"))
            server.host_targets[keys[0]] = mac
            server.host_targets[keys[1]] = ssh

            def interval() -> float:
                completion_lock.append(server.mac_recovery_lock.locked())
                recovery_cleared.append("fixture-mac" not in server.mac_disk_recoveries)
                return 2

            try:
                with mock.patch.object(server, "_waiting_pass_interval", side_effect=interval) as retry:
                    # Start the callback after the submitter releases the lock.
                    with mock.patch.object(threading.Thread, "start", autospec=True, side_effect=threads.append):
                        _ = self.request_recovery(server, lambda: None)
                    thread, = threads
                    thread.start()
                    thread.join(TEST_HANG_GUARD_SECS)
                    self.assertFalse(thread.is_alive())
                retry.assert_called_once()
                self.assertEqual(completion_lock, [True], "The completion callback must hold mac_recovery_lock")
                self.assertEqual(recovery_cleared, [True])
                self.assertEqual(server.mac_disk_retry_at["fixture-mac"], fixture.clock.monotonic() + 2)
                cast(mock.Mock, mac.expedite_all).assert_called_once_with()
                self.assertEqual(ssh.mock_calls, [])
            finally:
                for key in keys:
                    del server.host_targets[key]

    def test_failed_claim_ssh_writes_retry_and_retains_the_claim(self) -> None:
        """Admission per-claim SSH observation row 7."""
        with fixtures.PlacementFixture() as fixture:
            request = fixtures.placement_request(fixture)
            destination = request.constraints.candidates[0].destination
            config = fixture.root / "mac_test.conf"
            _ = config.write_text("host=fixture-mac\nmirror_root=~/fixture/mirror\n# range: 1..\nconnect_timeout_s=2\n"
                                  + "# range: 1..\nprobe_timeout_s=3\n# range: 1..\nunreachable_backoff_s=20\n")
            _ = (fixture.root / "build_targets.conf").write_text(
                "targets=mac\nkind.mac=ssh\nplatform.mac=macos\nsettings.mac=mac_test.conf\n" + UNREACHABLE_WAIT_FIXTURE)
            repo_key = "https://github.com/fixture/hana"
            stem = claim_state.mac_claim_stem(repo_key)
            mirror = claim_state.CleanupOnlyMacRepository(repo_key, "hana", f"{stem}.json", stem)
            record_path = fixture.root / mirror.claim_file
            _ = record_path.write_text(json.dumps({"mac_repository": mirror.record(),
                "repo_key": repo_key, "repo_name": "hana", "claim_id": "claim", "episode": request.episode, "attempt": "attempt",
                "submitting_session": "", "pid": request.owner.pid, "proc_start": request.owner.start_time,
                "remote_host": "fixture-mac", "what": "fixture", "worktree": str(fixture.root),
                "since": "2026-11-01T12:00:00+00:00"}))
            record = claim_state.read_run(record_path)
            assert not isinstance(record, claim_state.NoRun) and "remote_host" in record
            observe = cast(Callable[[wire.PlacementRequest, placement.PlacementDestination, Path,
                            claim_state.RemoteJobClaim, str, conf.ConfigRead], execution_identity.ExecutionEvidence],
                           getattr(fixture.server, "_observe_provider_execution"))
            before = record_path.read_bytes()
            with (mock.patch.object(time, "time", return_value=100),
                  mock.patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired("ssh", 3)) as ssh,
                  mock.patch.object(claim_state, "locked", wraps=claim_state.locked) as lock):
                seen = observe(request, destination, record_path, record, "macos", conf.read(config))
            self.assertEqual(seen, execution_identity.ExecutionEvidenceUnavailable(
                "remote execution remains unresolved", fixture.clock.now()))
            self.assertEqual(ssh.call_count, 1)
            backoff = eligibility.backoff_path("mac")
            lock.assert_called_once_with(backoff.with_name(offload.PROBE_RESULT_LOCK_NAME), wait=True)
            self.assertEqual(backoff.read_text(), "120.000000\n")
            self.assertEqual(record_path.read_bytes(), before)

    def test_guest_provider_reads_the_registry_and_validates_guest_settings(self) -> None:
        """Admission mac-linux guest row 6."""
        with fixtures.PlacementFixture() as fixture:
            request = self.guest_request(fixture)
            with (mock.patch.object(mac_linux, "read_guest_main", return_value=mac_linux.EmptyGuestMain()) as probe,
                  mock.patch.object(conf, "read", wraps=conf.read) as read):
                seen = self.read_provider(fixture, request)
                self.assertIsInstance(seen, placement.FreshHostEvidence)
                probe.assert_called_once_with(mac_linux.GuestSettings(
                    ("fixture-mac", "fixture-guest"), "fixture", 8, 7200, 600))
                settings = fixture.root / "mac_linux.conf"
                self.assertEqual(sum(call.args[0] == settings for call in read.call_args_list), 2)
                _ = settings.write_text(settings.read_text().replace("test.jobs=8", "test.jobs=0"))
                probe.reset_mock()
                refused = self.read_provider(fixture, request)
            assert isinstance(refused, placement.FreshHostEvidence)
            self.assertEqual(refused.eligibility, placement.TemporaryExclusion(placement.MAC_LINUX_NO_ANSWER))
            probe.assert_not_called()

    def test_guest_provider_runs_bounded_probe_for_main(self) -> None:
        """Admission mac-linux guest row 7."""
        with fixtures.PlacementFixture() as fixture:
            request = self.guest_request(fixture)
            with mock.patch.object(mac_linux, "run_guest_ssh",
                    return_value=subprocess.CompletedProcess([], 0, "a" * 40 + "\n", "")) as ssh:
                seen = self.read_provider(fixture, request)
            assert isinstance(seen, placement.FreshHostEvidence)
            self.assertEqual(seen.eligibility, placement.AllowedHost())
            ssh.assert_called_once()
            arguments, timeout = cast(tuple[tuple[str, ...], float], ssh.call_args.args)
            self.assertIn("fixture-mac", arguments)
            self.assertIn("fixture-guest", arguments[-1])
            self.assertIn("command -v bwrap", arguments[-1])
            self.assertIn('$checkout/.git/HEAD', arguments[-1])
            self.assertIn("refs/heads/main", arguments[-1])
            self.assertGreater(timeout, 0)
            self.assertLessEqual(timeout, mac_linux.GUEST_REF_TIMEOUT_SECONDS)
