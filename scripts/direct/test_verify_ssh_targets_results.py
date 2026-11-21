"""Check SSH results with private verification processes."""

from __future__ import annotations

import json
import subprocess
from threading import Event
from unittest import mock

from scripts.build_queue import admission_protocol as wire, admission_service as service, placement
from scripts.build_queue.admission_test_support import caller_provider_controls
from scripts.direct.verify_ssh_targets_test_support import SshRunnerEvent, VerifySshTargetsFixture
from scripts.lib.test_hang_guard import LauncherHangGuard


class VerifySshResultsTests(VerifySshTargetsFixture):
    def verify_after_ssh_result(self, target: str, *, extra: dict[str, str]) -> subprocess.CompletedProcess[str]:
        """Keep the first choice local only after the expected remote result."""
        controls = caller_provider_controls(self, self.admission)
        controls.set_host("natedev", placement.TemporaryExclusion("blocked"))
        handle_request = self.admission.server.handle_request

        def allow_local_after_result(
            request: wire.Request,
            connection: service.Connection | service.NoConnection = service.NO_CONNECTION,
        ) -> list[wire.Reply]:
            if isinstance(request, wire.ExecutionReport) and request.assignment.destination.target == target:
                controls.set_host("natedev", placement.AllowedHost())
            return handle_request(request, connection)

        with mock.patch.object(self.admission.server, "handle_request", side_effect=allow_local_after_result):
            return self.verify("test", "sample", "--no-cache", extra=extra)

    def only_attempt(self) -> dict[str, object]:
        attempts = self.attempts()
        self.assertEqual(len(attempts), 1,
                         json.dumps({"attempts": attempts, "events": self.ssh_events()})
                         + repr(self.admission.server.assignments))
        return attempts[0]

    def test_failed_probe_with_live_leftovers_keeps_the_claim_without_backoff(self) -> None:
        self.configure("linux,natedev")
        result = self.verify("test", "sample", "--no-cache", "--target", "linux", extra={
            "TEST_SSH_MODES": '{"linux":"probe-leftovers"}', "BUILDLOG_MEM_WAIT_SINCE": "100.25"})
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        first, = self.ssh_events()
        self.assertEqual(first["target"], "linux")
        self.check_live_leftovers(first)

    def test_failed_probe_with_live_leftovers_continues_without_backoff(self) -> None:
        self.configure("natedev,linux,spare")
        with self.spare_after_linux_refusal():
            result = self.verify_after_ssh_result("spare", extra={
                "TEST_SSH_MODES": '{"linux":"probe-fence-with-leftovers"}', "BUILDLOG_MEM_WAIT_SINCE": "100.25"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        events = self.ssh_events()
        self.assertEqual(len(events), 2, json.dumps(events) + repr(self.admission.server.assignments))
        first, second = events
        self.assertEqual((first["target"], second["target"]), ("linux", "spare"))
        self.assertEqual(first["queued_at"], second["queued_at"])
        self.assertFalse(list((self.target_state("linux") / "pending-starts").iterdir()))
        self.check_live_leftovers(first)

    def check_live_leftovers(self, first: SshRunnerEvent) -> None:
        self.assertEqual(first["result_type"], "BusyMacDecline")
        self.assertEqual(first["holder"], "LostRunLeftovers")
        self.assertNotIn("run", first["actions"])
        self.assertNotIn("release", first["actions"])
        self.assertEqual(first["queued_at"], "100.25")
        state = self.target_state("linux")
        self.assertTrue(list((state / "claims").glob("**/*.json")))
        self.assertFalse((state / "unreachable_until").exists())
        attempt = self.attempts()[0]
        self.assertEqual((attempt["cleanup"], attempt["unresolved_count"]), ("unconfirmed", 2))

    def test_no_matching_tests_have_a_distinct_durable_outcome(self) -> None:
        self.configure("natedev,linux")
        result = self.verify_after_ssh_result("linux", extra={"TEST_SSH_MODES": '{"linux":"no-tests"}'})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        attempt = self.only_attempt()
        self.assertEqual((attempt["outcome"], attempt["completion"], attempt["cleanup"]),
                         ("no_tests", "confirmed", "confirmed"))
        self.assertEqual(len(self.local_runs()), 1)

    def test_linux_loss_keeps_the_attempt_and_queue_age_after_local_success(self) -> None:
        self.check_linux_loss()

    def test_delayed_linux_probe_still_records_the_loss_before_local_success(self) -> None:
        controls = caller_provider_controls(self, self.admission)
        read_provider = controls.reader
        reply_filter = self.admission.server.reply_filter
        release_probe = Event()
        guard = LauncherHangGuard("the pending Linux probe reply")
        received_pending_probe = False

        def read_after_wait(key: placement.HostEvidenceKey) -> placement.HostEvidence:
            if key.destination.target == "linux":
                guard.event(release_probe)
            return read_provider(key)

        def observe(reply: wire.Reply) -> wire.Reply:
            nonlocal received_pending_probe
            if isinstance(reply, wire.PlacementWait):
                eligibility = {candidate.destination.target: candidate.eligibility for candidate in reply.eligibility}
                if (eligibility.get("natedev") == placement.TemporaryExclusion("blocked")
                        and isinstance(eligibility.get("linux"), placement.EvidenceNeeded)):
                    received_pending_probe = True
                    release_probe.set()
            elif isinstance(reply, wire.LocalPlacementGranted):
                release_probe.set()
            return reply_filter(reply)

        with mock.patch.object(controls, "reader", read_after_wait), mock.patch.object(
            self.admission.server, "reply_filter", side_effect=observe,
        ):
            try:
                self.check_linux_loss()
            finally:
                release_probe.set()
        self.assertTrue(received_pending_probe)

    def check_linux_loss(self) -> None:
        self.configure("natedev,linux")
        first, wait_environment = self.first_wait()
        result = self.verify_after_ssh_result("linux", extra={
            "TEST_SSH_MODES": '{"linux":"lost"}', **wait_environment})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(self.local_runs()), 1)
        events = self.ssh_events()
        self.assertEqual(len(events), 1, json.dumps(events) + result.stdout + result.stderr)
        attempt = self.only_attempt()
        self.assertEqual((attempt["target"], attempt["slot"], attempt["outcome"], attempt["queued_at"]),
                         ("linux", 1, "lost", 100.25))
        self.assertEqual(attempt["repo_identity"], "https://example.com/team/hana")
        self.assertEqual(self.routing_record()["target"], "natedev")
        self.assertEqual(first.read_text().strip(), "100.25")

    def test_confirmed_linux_failure_never_repeats_the_test_locally(self) -> None:
        self.configure("natedev,linux")
        result = self.verify_after_ssh_result("linux", extra={"TEST_SSH_MODES": '{"linux":"failed"}'})
        self.assertEqual(result.returncode, 42, result.stdout + result.stderr)
        self.assertEqual(self.local_runs(), [])
        attempt = self.only_attempt()
        self.assertEqual(self.ssh_events()[0]["result_type"], "FailedOnTarget")
        self.assertEqual((attempt["outcome"], attempt["completion"], attempt["cleanup"]),
                         ("failed", "confirmed", "confirmed"))
