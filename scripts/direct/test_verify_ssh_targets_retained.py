"""Check retained SSH slots and claims with private verification processes."""

from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import cast
from unittest import mock

from scripts.build_queue import execution_identity
from scripts.build_targets import test_ssh_eligibility as eligibility_support
from scripts.build_queue import admission_protocol as wire, admission_service as service, placement
from scripts.build_queue.admission_test_support import caller_provider_controls
from scripts.direct import verify_ssh_targets_test_support as support
from scripts.direct.verify_ssh_targets_test_support import VerifySshTargetsFixture
from scripts.lint.memory_test_support import write_config_fixture
from scripts.lib.process_start_test_support import process_reader
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.mac_test import claim_state, offload

from scripts.lib import process_start as process_identity


mac_test = eligibility_support.mac_test


class VerifySshRetainedTests(VerifySshTargetsFixture):
    def test_lost_authorization_releases_the_exact_settled_start(self) -> None:
        self.check_lost_authorization(confirmed=True)

    def test_lost_authorization_keeps_the_exact_unsettled_start(self) -> None:
        self.check_lost_authorization(confirmed=False)

    def check_lost_authorization(self, *, confirmed: bool) -> None:
        self.configure("natedev,linux")
        _ = self.hold_memory()
        server = self.admission.server
        send = server._send  # pyright: ignore[reportPrivateUsage]
        authorizations: list[wire.FirstAuthorization] = []
        consumed: list[bool] = []

        def drop_first_authorization(connection: service.Connection, reply: wire.Reply) -> None:
            if isinstance(reply, wire.FirstAuthorization):
                authorizations.append(reply)
                if len(authorizations) == 1:
                    assignment = reply.assignment
                    saved = server.assignments[execution_identity.attempt_key(assignment.episode, assignment.attempt)]
                    consumed.append(isinstance(saved.permission, placement.PermissionConsumed))
                    server._disconnect(connection)  # pyright: ignore[reportPrivateUsage]
                    return
            send(connection, reply)

        arguments = ["test", "sample", "--no-cache"]
        if not confirmed:
            arguments.extend(("--target", "linux"))
        mode = "start-fence-confirmed" if confirmed else "start-fence-unconfirmed"
        with mock.patch.object(server, "_send", side_effect=drop_first_authorization):
            process = self.start_verify(*arguments, extra={"TEST_SSH_MODES": json.dumps({"linux": mode})})
            self.addCleanup(self.stop_verify, process)
            events_path = self.runner_state / "ssh-events.jsonl"
            self.wait_until(lambda: events_path.exists() and events_path.read_text().endswith("\n"),
                            str(events_path), (process,), directory=self.runner_state)
            first = self.ssh_events()[0]
            expected_actions = ["cleanup", "release"] if confirmed else ["cleanup"]
            self.assertEqual(first["actions"][-len(expected_actions):], expected_actions, first)
            self.assertNotIn("run", first["actions"])
            result = self.finish_verify(process)

        lost = authorizations[0]
        self.assertEqual(consumed, [True])
        claim = lost.claim
        self.assertEqual(first["adopted_claim_ids"], [claim.claim_id])
        identity = claim_state.ClaimIdentity(claim.claim_id, claim.episode, claim.attempt)
        state = self.target_state("linux")
        journal = state / "pending-starts" / offload.pending_start_key(identity)
        events = (self.runner_state / "queue-events.jsonl").read_text().splitlines()
        requests = [wire.parse_request(json.dumps(cast(dict[str, object], json.loads(line))["request"]))
                    for line in events]
        report, = [request for request in requests if isinstance(request, wire.ExecutionReport)
                   and request.assignment == lost.assignment]
        self.assertEqual(report.claim, claim)
        self.assertEqual(report.evidence, execution_identity.ExecutionNotStarted())
        self.assertEqual(report.outcome, placement.PrelaunchRefused("state"))
        self.assertEqual(self.local_runs(), [])
        if confirmed:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIsInstance(report.path, placement.DispatchPathSettled)
            self.assertNotIn(claim.claim_id, first["remaining_claim_ids"])
            self.assertFalse(journal.exists())
            self.assertFalse((self.runner_state / "provider-outcome-linux.json").exists())
            _, successful = authorizations
            self.assertEqual(successful.assignment.episode, lost.assignment.episode)
            self.assertNotEqual(successful.claim, claim)
            _, run = self.ssh_events()
            self.assertIn("run", run["actions"])
            self.assertEqual(run["result_type"], "PassedOnTarget")
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIsInstance(report.path, placement.DispatchPathPending)
            self.assertEqual(authorizations, [lost])
            self.assertEqual(self.ssh_events(), [first])
            held, = caller_provider_controls(self, self.admission).claims("linux")
            self.assertEqual(claim_state.claim_identity(held.record), identity)
            self.assertTrue(journal.exists())
            fence = offload.start_fence_relative(identity, "linux", scope_key=held.path.stem)
            self.assertTrue((self.home / fence.with_suffix(".lock")).exists())

    def test_verify_exit_before_first_memory_hook_fails(self) -> None:
        self.release_memory_after_hook()
        diagnostic_root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        with mock.patch.object(self, "verify_diagnostic_root", return_value=diagnostic_root), \
             subprocess.Popen(["printf", "partial\n"], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process, \
             mock.patch.object(self, "start_verify", return_value=process), \
             self.assertRaisesRegex(self.failureException, "verify exited"):
            _ = self.verify("test", "sample", "--no-cache")
        evidence, = diagnostic_root.iterdir()
        self.assertFalse(evidence.is_relative_to(self.root))
        self.assertIn(f"PID {process.pid}", (evidence / "processes.txt").read_text())
        self.assertEqual((evidence / "stdout").read_text(), "partial\n")

    def verify_with_memory_hook(
        self, event: support.CompletedMemoryHook, *, output_size: int = 0,
    ) -> subprocess.CompletedProcess[str]:
        """Publish a complete hook event before the fixture can read it."""
        self.release_memory_after_hook()
        command = '''
from pathlib import Path
import sys

from scripts.lib.atomic_file import write_text_atomic

write_text_atomic(Path("memory-hook-1"), sys.argv[1])
output_size = int(sys.argv[2])
if output_size:
    with Path("memory-hook-ack").open() as acknowledgment:
        assert acknowledgment.read(len("continue")) == "continue"
    sys.stdout.write("x" * output_size)
'''
        with subprocess.Popen([sys.executable, "-c", command, json.dumps(event), str(output_size)],
                              cwd=self.runner_state, text=True,
                              env={**self.environment, "PYTHONPATH": str(support.REPOSITORY)},
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process, \
             mock.patch.object(self, "start_verify", return_value=process):
            return self.verify("test", "sample", "--no-cache")

    def test_verify_drains_output_after_memory_hook_settles(self) -> None:
        output_size = 128 * 1024
        result = self.verify_with_memory_hook({"left": False, "clock": "", "attempts": 0}, output_size=output_size)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "x" * output_size)

    def test_verify_returns_when_settled_hook_skips_acknowledgment(self) -> None:
        result = self.verify_with_memory_hook({"left": True, "clock": "", "attempts": 0})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def retain_slots(self, old_capacity: int = 2) -> list[Path]:
        _ = write_config_fixture(self.root / "linux_target.conf",
                                 (self.root / "linux_target.conf").read_text()
                                 + f"slots={old_capacity}\nslots.hana={old_capacity}\n",
                                 template="mac_test")
        paths: list[Path] = []
        with mock.patch.dict(os.environ, self.environment), \
             mock.patch.object(Path, "home", return_value=self.home), \
             mock.patch.object(process_identity, "process_start", side_effect=process_reader(eligibility_support.SshEligibilityTests().process_start)), \
             mock.patch.object(mac_test, "target_claim_scope", return_value=mac_test.RepositoryScope(
                 "hana", "https://example.com/team/hana")):
            state = mac_test.state_paths("linux")
            for pid in range(2147483000, 2147483000 + old_capacity):
                args = mac_test.CliArguments()
                args.target, args.worktree, args.pid = "linux", str(self.root), pid
                args.what, args.remote = "test hana", "fake-linux"
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(mac_test.command_claim(args, state, watch_claim=False), 0)
            paths = [claim.path for claim in mac_test.read_claims(state)]
        self.assertEqual(len(paths), old_capacity)
        # The current step has one slot. The other old claim remains outside current capacity.
        settings = self.root / "linux_target.conf"
        _ = write_config_fixture(settings, settings.read_text() + "slots=1\nslots.hana=1\n", template="mac_test")
        return paths

    def test_queue_retry_cleans_retained_slots_after_their_owners_exit(self) -> None:
        self.configure("natedev,linux")
        claims = self.retain_slots()
        controls = caller_provider_controls(self, self.admission)
        old_claims = {held.record["claim_id"]: (held.record["episode"], held.record["attempt"])
                      for held in controls.claims("linux")}
        _ = self.hold_memory()
        result = self.verify("test", "sample", "--no-cache")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        cleanup, run = self.ssh_events()
        self.assertEqual(cleanup["actions"], ["claim:13", "cleanup", "release"])
        self.assertEqual(cleanup["result_type"], "BusyMacDecline")
        selected, = cleanup["adopted_claim_ids"]
        self.assertIn(selected, old_claims)
        self.assertNotIn(selected, cleanup["remaining_claim_ids"])
        self.assertEqual(run["actions"][0], "claim:0")
        self.assertIn("run", run["actions"])
        self.assertEqual(run["result_type"], "PassedOnTarget")
        replacement, = run["adopted_claim_ids"]
        self.assertNotIn(replacement, old_claims)
        self.assertIn(replacement, run["entry_claim_ids"])
        self.assertEqual(cleanup["queued_at"], run["queued_at"])
        self.assertEqual(self.local_runs(), [])
        self.assertLess(sum(path.exists() for path in claims), len(claims))
        remaining, = controls.claims("linux")
        self.assertNotEqual(remaining.record["claim_id"], selected)
        self.assertEqual((remaining.record["episode"], remaining.record["attempt"]),
                         old_claims[remaining.record["claim_id"]])

    def test_retained_slots_stay_owned_when_retry_cannot_confirm_cleanup(self) -> None:
        self.configure("natedev,linux,spare")
        claims = self.retain_slots()
        _ = self.hold_memory()
        pressure = Path(self.environment["BUILDLOG_MEMORY_PRESSURE"])
        _ = pressure.write_text("some avg10=100 avg60=0 avg300=0 total=0\n"
                                + "full avg10=100 avg60=0 avg300=0 total=0\n")
        before_attempt = r'''
steps_path = root / "memory-steps.json"
steps = json.loads(steps_path.read_text()) if steps_path.exists() else []
if request.target == "linux":
    assert steps == [], steps
    steps.append("linux_attempt")
else:
    assert request.target == "spare", request.target
    assert steps == ["linux_attempt", "linux_cleanup_failed"], steps
    prior = json.loads((root / "ssh-events.jsonl").read_text().splitlines()[-1])
    assert prior["target"] == "linux" and "cleanup" in prior["actions"], prior
    assert prior["holder"] == "LostRunLeftovers", prior
    steps.append("spare_retry")
steps_path.write_text(json.dumps(steps))
'''
        after_attempt = r'''
if request.target == "linux":
    assert isinstance(result, offload.BusyMacDecline), result
    assert isinstance(result.holder, offload.LostRunLeftovers), result
    assert "cleanup" in actions and "run" not in actions, actions
    steps.append("linux_cleanup_failed")
else:
    assert isinstance(result, offload.PassedOnTarget), result
    steps.append("spare_completed")
    Path(os.environ["BUILDLOG_MEMINFO"]).write_text("MemAvailable: 268435456 kB\n")
    Path(os.environ["BUILDLOG_MEMORY_PRESSURE"]).write_text(
        "some avg10=0 avg60=0 avg300=0 total=0\nfull avg10=0 avg60=0 avg300=0 total=0\n")
steps_path.write_text(json.dumps(steps))
'''
        self.write_executable(self.runner, support.SSH_RUNNER.replace(
            "started = time.monotonic()", before_attempt + "\nstarted = time.monotonic()").replace(
            "\nconfigured_mode = ", after_attempt + "\nconfigured_mode = "))
        with self.spare_after_linux_refusal():
            result = self.verify("test", "sample", "--no-cache", extra={
                "TEST_SSH_MODES": '{"linux":"retained-leftovers"}', "BUILDLOG_MEM_WAIT_SINCE": "100.25"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        first, second = self.ssh_events()
        self.assertEqual((first["target"], second["target"]), ("linux", "spare"))
        self.assertEqual(first["result_type"], "BusyMacDecline")
        self.assertEqual(first["holder"], "LostRunLeftovers")
        self.assertIn("cleanup", first["actions"])
        self.assertNotIn("run", first["actions"])
        self.assertNotIn("release", first["actions"])
        self.assertTrue(all(path.exists() for path in claims))
        self.assertEqual(first["queued_at"], second["queued_at"])
        self.assertFalse((self.target_state("linux") / "unreachable_until").exists())
        self.assertEqual(json.loads((self.runner_state / "memory-steps.json").read_text()),
                         ["linux_attempt", "linux_cleanup_failed", "spare_retry", "spare_completed"])
        requests = [wire.parse_request(json.dumps(cast(dict[str, object], json.loads(line))["request"]))
                    for line in (self.runner_state / "queue-events.jsonl").read_text().splitlines()]
        refusal, = [request for request in requests if isinstance(request, wire.ClaimFailure)
                    and request.assignment.destination.target == "linux"]
        self.assertIsInstance(refusal.claim, placement.ProviderClaimReference)
        assert isinstance(refusal.claim, placement.ProviderClaimReference)
        self.assertEqual(refusal.claim.claim_id, first["adopted_claim_ids"][0])
        self.assertNotEqual(refusal.claim.episode, refusal.assignment.episode)
        saved = self.admission.server.assignments[execution_identity.attempt_key(
            refusal.assignment.episode, refusal.assignment.attempt)]
        self.assertEqual(saved.provider, refusal.claim)
        self.assertIsInstance(saved.state, placement.ClosedAssignment)

    def test_mac_memory_retry_cleans_a_retained_claim_after_its_owner_exits(self) -> None:
        self.configure("natedev,mac")
        self.enable_mac()
        # Keep os.environ stable while the admission reader uses the private memory paths.
        claim = subprocess.run([str(Path(__file__).resolve().parents[1] / "lib/py"), "-c", """
import os
from unittest import mock
from scripts.lib import process_start as process_identity
from scripts.mac_test import mac_test
args = mac_test.CliArguments()
args.target, args.worktree, args.pid = "mac", os.getcwd(), 2147483000
args.what, args.remote = "test sample", "test-mac"
with mock.patch.object(process_identity, "process_start", return_value=process_identity.Started("old-owner")):
    raise SystemExit(mac_test.command_claim(args, mac_test.state_paths("mac"), watch_claim=False))
"""], cwd=self.root, env=self.environment, capture_output=True, text=True,
                               timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(claim.returncode, 0, claim.stdout + claim.stderr)
        held, = caller_provider_controls(self, self.admission).claims("mac")
        old_claim_id = held.record["claim_id"]
        _ = self.hold_memory()
        result = self.verify("test", "sample", "--no-cache", extra={
            "VERIFY_MAC_TEST_SCRIPT": str(support.MAC_TEST_SCRIPT)})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        cleanup, run = self.ssh_events()
        self.assertEqual(cleanup["actions"], ["claim:13", "cleanup", "release"])
        self.assertEqual(cleanup["result_type"], "BusyMacDecline")
        self.assertEqual(cleanup["adopted_claim_ids"], [old_claim_id])
        self.assertNotIn(old_claim_id, cleanup["remaining_claim_ids"])
        self.assertEqual(run["actions"], ["claim:0", "probe", "source-copy", "run", "release"])
        self.assertEqual(run["result_type"], "PassedOnMac")
        self.assertEqual(len(run["entry_claim_ids"]), 1)
        self.assertNotIn(old_claim_id, run["entry_claim_ids"])
        self.assertEqual(run["adopted_claim_ids"], run["entry_claim_ids"])
        self.assertEqual(cleanup["queued_at"], run["queued_at"])
        self.assertEqual(self.local_runs(), [])

    def test_mac_memory_retry_recovers_its_claim_without_a_reservation_file(self) -> None:
        self.configure("natedev,mac")
        self.enable_mac()
        meminfo = self.hold_memory()
        interrupted = self.runner_state / "reservation-interrupted"
        recovered = self.runner_state / "reservation-recovered"
        continue_reserve = self.runner_state / "continue-reserve"
        continue_recovery = self.runner_state / "continue-recovery"
        self.addCleanup(continue_reserve.touch)
        self.addCleanup(continue_recovery.touch)
        wrapper = self.root / "interrupted-reservation.py"
        self.write_executable(wrapper, r'''
import os
from pathlib import Path
import sys
source = Path(os.environ["TEST_SOURCE_REPO"])
arguments = sys.argv[1:]
''' + support.BUILD_TARGETS_STATUS + r'''
status = build_targets_status()
from scripts.direct.wait_for_test_state import wait_for_file
root = Path(os.environ["TEST_RUNNER_STATE"])
marker = root / "reservation-interrupted"
if arguments[0] == "reserve" and status == 0 and not marker.exists():
    destination = Path(arguments[arguments.index("--reservation-file") + 1])
    destination.unlink()
    marker.touch()
    wait_for_file(root / "continue-reserve")
    raise SystemExit(1)
if arguments[0] == "recover-reservation" and status == 0:
    (root / "reservation-recovered").touch()
    wait_for_file(root / "continue-recovery")
raise SystemExit(status)
''')
        process = self.start_verify("test", "sample", "--no-cache", extra={
            "BUILD_TARGETS_SCRIPT": str(wrapper),
            "VERIFY_MAC_TEST_SCRIPT": str(support.MAC_TEST_SCRIPT)})
        self.addCleanup(self.stop_verify, process)
        self.wait_for_path(interrupted, (process,))
        controls = caller_provider_controls(self, self.admission)
        claims = controls.claims("mac")
        self.assertEqual(len(claims), 1)
        original_claim = [(claim.path, claim.record) for claim in claims]
        episode = claims[0].record["episode"]
        with self.admission.loop_turn():
            submitted = self.admission.server.placement_requests[episode]
            assignment = self.admission.server.assignments[
                self.admission.server.current_assignments[episode]].assignment
            original_since = submitted.since

        # Reconnect once after each unresolved response in this recovery scenario.
        placement_retry_boundaries = (("reserve response lost", interrupted, continue_reserve),
                                      ("recovery response pending", recovered, continue_recovery))
        for boundary, observed, release in placement_retry_boundaries:
            with self.subTest(boundary=boundary):
                self.wait_for_path(observed, (process,))
                claims = controls.claims("mac")
                self.assertEqual([(claim.path, claim.record) for claim in claims], original_claim)
                self.assertEqual(self.local_runs(), [])
                self.assertEqual(meminfo.read_text(), "MemAvailable: 0 kB\n")
                with self.admission.loop_turn():
                    saved = self.admission.server.assignments[execution_identity.attempt_key(episode, assignment.attempt)]
                    self.assertIsInstance(saved.permission, placement.PermissionAvailable)
                    self.assertEqual(self.admission.server.placement_requests[episode].since, original_since)
                    peer = self.admission.connect(submitted)
                    reply, = peer.replies()
                    peer.close()
                    self.assertIsInstance(reply, wire.Assignment)
                    assert isinstance(reply, wire.Assignment)
                    self.assertEqual(reply.assignment, assignment)
                    self.assertEqual(self.admission.caller_reservations(), {})
                release.touch()

        result = self.finish_verify(process)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(interrupted.exists())
        event, = self.ssh_events()
        self.assertTrue(event["reserved"])
        self.assertTrue(event["same_path"])
        self.assertIn("claim:0", event["actions"])
        self.assertEqual(float(event["queued_at"]), original_since)
        self.assertEqual(self.local_runs(), [])
        with self.admission.loop_turn():
            self.assertEqual(sum(isinstance(saved.permission, placement.PermissionConsumed)
                                 for saved in self.admission.server.assignments.values()), 1)
            self.assertEqual(self.admission.caller_reservations(), {})
