"""Check retained SSH slots and claims with private verification processes."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from typing import cast
from unittest import mock

from scripts.lib.helper_tree_test_support import verify_test_environment
from scripts.build_queue import execution_identity
from scripts.build_targets import test_ssh_eligibility as eligibility_support
from scripts.build_queue import admission_protocol as wire, admission_records, admission_service as service, placement
from scripts.build_queue.admission_test_support import caller_provider_controls
from scripts.direct import verify_ssh_targets_test_support as support
from scripts.direct.verify_ssh_targets_test_support import VerifySshTargetsFixture
from scripts.lint.memory_test_support import write_config_fixture
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.mac_test import claim_state


mac_test = eligibility_support.mac_test


class VerifySshRetainedTests(VerifySshTargetsFixture):
    def test_lost_mac_releases_the_verify_place_and_keeps_the_claim(self) -> None:
        self.check_lost_mac("lost-cleanup-timeout")

    def test_mac_runner_exit_without_result_releases_the_verify_place(self) -> None:
        self.check_lost_mac("missing-result-after-authorization")

    def test_lost_mac_authorization_reply_releases_the_verify_place(self) -> None:
        self.check_lost_mac("lost-authorization-reply")

    def test_mac_cleanup_stream_timeout_preserves_the_failed_verdict(self) -> None:
        self.check_lost_mac("cleanup-stream-timeout", status=42)

    def test_mac_post_fetch_cleanup_timeout_preserves_the_failed_verdict(self) -> None:
        self.check_lost_mac("post-fetch-cleanup-timeout", status=42)

    def test_mac_check_cleanup_timeout_preserves_the_failed_verdict(self) -> None:
        self.check_lost_mac("cleanup-stream-timeout", status=42, command="check")

    def test_mac_setup_failure_with_cleanup_timeout_ends_without_a_retry(self) -> None:
        self.check_lost_mac("setup-failure-cleanup-timeout")

    def test_lost_mac_check_releases_the_verify_place_and_keeps_the_claim(self) -> None:
        self.check_lost_mac("lost-cleanup-timeout", command="check")

    def check_lost_mac(self, mode: str, *, status: int = 2, command: str = "test") -> None:
        self.configure("natedev,mac")
        self.enable_mac()
        settings = self.root / "mac_test.conf"
        _ = write_config_fixture(settings, "leftover_timeout_s=1\n")
        _ = self.hold_memory()
        slots_during_authorization: list[int] = []
        previous = self.admission.server.handle_request

        def observe(request: wire.Request,
                    connection: service.Connection | service.NoConnection = service.NO_CONNECTION) -> list[wire.Reply]:
            replies = previous(request, connection)
            if any(isinstance(reply, wire.FirstAuthorization) for reply in replies):
                slots_during_authorization.extend(
                    slot.owner.pid for slot in self.admission.server.verify_run_slots.values())
            return replies

        with mock.patch.object(self.admission.server, "handle_request", side_effect=observe):
            process = self.start_verify(command, "sample", "--no-cache", extra={
                "TEST_SSH_MODES": json.dumps({"mac": mode})})
            try:
                self.wait_for_path(self.runner_state / "mac-loss-ready", (process,))
                output, errors = process.communicate(timeout=8)
            except subprocess.TimeoutExpired:
                self.fail("Verify did not exit after the Mac lost its result or cleanup.")
            finally:
                if process.poll() is None:
                    self.stop_verify(process)

        self.assertEqual(process.returncode, status, output + errors)
        if status == 2:
            self.assertIn("the Mac does not answer; stop and submit again", errors)
        else:
            self.assertIn("cleanup timed out after 1 s", output)
            self.assertNotIn("the Mac does not answer; stop and submit again", errors)
        # A Mac run takes no natedev run slot (036f97c797: the slot comes with the first local memory grant).
        self.assertEqual(slots_during_authorization, [])
        self.assertEqual(self.local_runs(), [])
        if command == "check":
            self.assertFalse(any(event["args"][:1] == ["check"] for event in self.cargo_events()))
        if mode == "missing-result-after-authorization":
            self.assertEqual(self.attempts(), [])
        else:
            attempt, = self.attempts()
            # A setup failure is an observed end; only its cleanup is lost.
            lost_end = "confirmed" if mode == "setup-failure-cleanup-timeout" else "missing"
            expected = ("lost", lost_end) if status == 2 else ("failed", "confirmed")
            self.assertEqual((attempt["outcome"], attempt["completion"]), expected)
            self.assertEqual(attempt["cleanup"], "unconfirmed")
        if mode == "post-fetch-cleanup-timeout":
            event, = self.ssh_events()
            self.assertEqual(event["actions"][-3:], ["run", "fetch", "run"])
        controls = caller_provider_controls(self, self.admission)
        claim, = controls.claims("mac")
        identity = claim_state.claim_identity(claim.record)
        journal = claim_state.claim_pending_start(controls.claim_paths("mac"), claim.path, claim.record)
        self.assertEqual(journal.is_file(), mode != "post-fetch-cleanup-timeout")

        _ = self.admission.run_pass()
        with self.admission.loop_turn():
            server = self.admission.server
            self.assertEqual(server.verify_run_slots, {})
            key = execution_identity.attempt_key(identity.episode, identity.attempt)
            saved = server.assignments[key]
            self.assertIsInstance(saved.provider, placement.ProviderClaimReference)
            assert isinstance(saved.provider, placement.ProviderClaimReference)
            self.assertEqual(saved.provider.claim_id, identity.claim_id)
            self.assertIsInstance(saved.remote_start_hold, admission_records.RemoteStartHoldEnded)
            self.assertFalse(any(place.episode == identity.episode for place in server.places.values()))

    def test_verify_exit_before_first_memory_hook_fails(self) -> None:
        self.release_memory_after_hook()
        with subprocess.Popen(["true"],
                             env=verify_test_environment(), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process, \
             mock.patch.object(self, "start_verify", return_value=process), \
             self.assertRaisesRegex(self.failureException, "verify exited"):
            _ = self.verify("test", "sample", "--no-cache")

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

    def retain_slots(self) -> list[Path]:
        with mock.patch.dict(os.environ, self.environment), mock.patch.object(Path, "home", return_value=self.home):
            state = mac_test.state_paths("linux")
            scope = mac_test.RepositoryScope("hana", "https://example.com/team/hana")
            claim_path = mac_test.claim_path(state, scope)
            claim_path.parent.mkdir(parents=True, exist_ok=True)
            paths = [claim_path.with_name(f"{claim_path.stem}.slot-{number}.json") for number in (1, 2)]
            for number, path in enumerate(paths, start=1):
                pid = 2147482999 + number
                mac_test.write_json(path, {
                    "claim_id": f"saved-claim-{number}", "episode": f"saved-episode-{number}",
                    "attempt": f"saved-attempt-{number}", "submitting_session": "", "pid": pid,
                    "proc_start": f"start-{pid}", "worktree": str(self.root), "what": "test hana",
                    "since": f"2026-11-01T00:00:0{number}+00:00", "remote_host": "fake-linux",
                    "repo_name": scope.name, "repo_key": scope.key, "slot": number,
                })
        return paths

    def test_queue_retry_cleans_retained_slots_after_their_owners_exit(self) -> None:
        self.configure("natedev,linux")
        claims = self.retain_slots()
        controls = caller_provider_controls(self, self.admission)
        old_claims = {held.record["claim_id"] for held in controls.claims("linux")}
        _ = self.hold_memory()
        result = self.verify("test", "sample", "--no-cache")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        first_cleanup, second_cleanup, run = self.ssh_events()
        released: set[str] = set()
        for cleanup in (first_cleanup, second_cleanup):
            self.assertEqual(cleanup["actions"], ["claim:13", "cleanup", "release"])
            self.assertEqual(cleanup["result_type"], "BusyMacDecline")
            selected, = cleanup["adopted_claim_ids"]
            released.add(selected)
            self.assertNotIn(selected, cleanup["remaining_claim_ids"])
            self.assertEqual(cleanup["queued_at"], run["queued_at"])
        self.assertEqual(released, old_claims)
        self.assertEqual(run["actions"][0], "claim:0")
        self.assertIn("run", run["actions"])
        self.assertEqual(run["result_type"], "PassedOnTarget")
        replacement, = run["adopted_claim_ids"]
        self.assertNotIn(replacement, old_claims)
        self.assertIn(replacement, run["entry_claim_ids"])
        self.assertEqual(self.local_runs(), [])
        self.assertFalse(any(path.exists() for path in claims))
        self.assertEqual(controls.claims("linux"), ())

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
from unittest import mockfrom scripts.mac_test import mac_test
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
