"""Check Mac outage recovery with private transport and admission fixtures."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
import os
import shlex
import shutil
import unittest
from unittest import mock

from scripts.build_queue import admission_protocol as wire, execution_identity, placement
from scripts.build_queue.admission_test_support import caller_admission
from scripts.build_queue.test_placement import local_candidate, placement_request
from scripts.mac_test import claim_state, eligibility, mac_test, offload
from scripts.mac_test.offload_test_commands import SELECTOR_STUB, write_shell
from scripts.mac_test.offload_test_support import OffloadCommandFixture, RemoteClaimRecord


class MacReturnTests(OffloadCommandFixture):
    def lose_mac_run(self) -> RemoteClaimRecord:
        _ = (self.stub_directory / "boot_id").write_text("first-boot\n")
        self.fixture["run_lines"] = [{"stream": "stdout", "text": "running tests before the outage"}]
        self.fixture["run_status_reported"] = False
        self.fixture["run_ssh_status"] = 255
        self.set_leftover_answer("ssh_error")

        interrupted = self.process_command(separate_stderr=True)

        self.assertEqual(interrupted.returncode, 75, interrupted.stdout + interrupted.stderr)
        self.assertIn("running tests before the outage", interrupted.stdout)
        self.assert_wire("lost", "")
        self.assert_claim_is_kept()
        self.assert_claimed_commands()
        cleanup, = self.leftover_events()
        self.assertEqual(cleanup["args"][-2], "mac")
        return self.remote_claim()

    def reboot_fixture_mac(self) -> None:
        # The Mac selector reads processes. The fixture exposes an empty table after reboot.
        boot = self.stub_directory / "boot_id"
        selectors = self.stub_directory / "selectors"
        selectors.mkdir()
        write_shell(selectors / "pgrep",
                    f'[[ $(cat {shlex.quote(str(boot))}) == second-boot ]] || exit 2\n' + SELECTOR_STUB)
        for tool in ("lsof", "sleep"):
            write_shell(selectors / tool, SELECTOR_STUB)
        _ = boot.write_text("second-boot\n")
        self.fixture.update(selector_shell="sh", run_lines=[], run_status_reported=True, run_ssh_status=0)
        self.set_leftover_answer("clean")
        self.clear_events()

    def test_next_run_checks_and_releases_the_kept_claim_after_reboot(self) -> None:
        kept = self.lose_mac_run()
        self.reboot_fixture_mac()

        self.assertEqual(self.remote_claim(), kept)
        takeover = self.command("--no-fail-fast")

        self.assertEqual(takeover.returncode, 75, takeover.stdout + takeover.stderr)
        self.assert_wire("declined", "busy")
        cleanup, = self.leftover_events()
        self.assertEqual(cleanup["args"][-2], kept["remote_host"])
        self.assertTrue(cleanup["claimed"])
        assert "claim" in cleanup
        self.assertEqual(cleanup["claim"]["claim_id"], kept["claim_id"])
        self.assertNotEqual(cleanup["claim"]["pid"], kept["pid"])
        self.assertEqual(self.events_for("rsync"), [])
        self.assertIn("leftovers=0", (self.stub_directory / "selector-answer").read_text())
        with mock.patch.dict(os.environ, self.environment, clear=True):
            paths = claim_state.state_paths("mac")
            self.assertEqual(claim_state.read_claims(paths), ())
            self.assertEqual(mac_test.free_now(paths, self.config_path, self.repository), "free")

        result = self.command("--no-fail-fast")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_wire("passed", "")

    # The service retains the claim when its probe finds no process.
    @unittest.expectedFailure
    def test_service_observation_after_reboot_clears_the_kept_claim_without_a_new_run(self) -> None:
        kept = self.lose_mac_run()
        self.reboot_fixture_mac()
        admission = caller_admission(self, self.root, self.environment)

        with admission.loop_turn(), mock.patch.dict(os.environ, self.environment, clear=True):
            request = admission.server.placement_requests[kept["episode"]]
            destination = request.constraints.candidates[0].destination
            key = admission.server.host_evidence_key(request, destination)
            admission.server.host_requests[key] = request
            evidence = admission.server.read_provider_evidence(key)
            admission.server.publish_host_evidence(evidence)
            _ = admission.run_pass()
            paths = claim_state.state_paths("mac")
            remaining = claim_state.read_claims(paths)
            free = mac_test.free_now(paths, self.config_path, self.repository)

        self.assertIsInstance(evidence, placement.FreshHostEvidence)
        assert isinstance(evidence, placement.FreshHostEvidence)
        self.assertEqual(evidence.eligibility, placement.AllowedHost())
        (reference, observation), = evidence.executions
        self.assertEqual(reference.claim_id, kept["claim_id"])
        self.assertIsInstance(observation, execution_identity.ExecutionEvidenceUnavailable)
        self.assertEqual((self.stub_directory / "selector-answer").read_text(), "")
        self.assertEqual(self.events_for("rsync"), [])
        self.assertEqual(free, "free")
        self.assertEqual(remaining, ())

    def test_placement_uses_natedev_when_the_mac_probe_fails(self) -> None:
        self.fixture.update(probe_status=255, probe_lines=[], probe_error="fixture Mac is off")
        self.sync_fixture()
        admission = caller_admission(self, self.root, self.environment)

        with admission.loop_turn(), mock.patch.dict(os.environ, self.environment, clear=True):
            request = placement_request(admission)
            mac, = request.constraints.candidates
            local = local_candidate(admission)
            request = replace(request, constraints=replace(request.constraints,
                repository=str(self.repository), candidates=(mac, local)))
            for candidate in request.constraints.candidates:
                key = admission.server.host_evidence_key(request, candidate.destination)
                admission.server.host_requests[key] = request
                evidence = admission.server.read_provider_evidence(key)
                if candidate == mac:
                    self.assertIsInstance(evidence, placement.FreshHostEvidence)
                    assert isinstance(evidence, placement.FreshHostEvidence)
                    self.assertEqual(evidence.eligibility, placement.TemporaryExclusion("unreachable"))
                admission.server.publish_host_evidence(evidence)
            reply, = admission.server.handle_request(request)

        self.assertIsInstance(reply, wire.LocalPlacementGranted)
        assert isinstance(reply, wire.LocalPlacementGranted)
        self.assertEqual(reply.assignment.destination, local.destination)
        self.assertEqual((reply.assignment.episode, reply.assignment.since), (request.episode, request.since))
        self.assertFalse(self.claim_file.exists())
        self.assertEqual(self.events_for("rsync"), [])

    def test_retry_repairs_an_interrupted_mirror_copy(self) -> None:
        rsync = shutil.which("rsync")
        if rsync is None:
            self.fail("The mirror fixture requires local rsync")
        mirror = self.root / "mirror"
        mirror.mkdir()
        _ = (self.repository / "copied").write_bytes(b"copied before cutoff")
        _ = (self.repository / "remaining").write_bytes(b"copied on retry")
        damaged = self.repository / "same-time"
        _ = damaged.write_bytes(b"good")
        _ = (mirror / "removed").write_bytes(b"absent from source")
        controller = offload.ProcessController()
        request = offload.OffloadRequest(self.repository, "hana", "mirror-retry",
                                         offload.NO_RESULT_FILE, (), owner="verify", holder_session="fixture")
        capture = controller.capture
        destinations: list[str] = []

        def fixture_copy(arguments: Sequence[str],
                         deadline: offload.CommandDeadline = offload.NO_COMMAND_TIME_LIMIT
                         ) -> offload.CapturedCommandResult:
            if arguments[0] != "rsync":
                return capture(arguments, deadline)
            destinations.append(arguments[-1])
            if len(destinations) == 1:
                _ = shutil.copyfile(self.repository / "copied", mirror / "copied")
                _ = (mirror / damaged.name).write_bytes(b"lost")
                stamp = damaged.stat()
                os.utime(mirror / damaged.name, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
                return offload.CommandOutput(20, "", "fixture interrupted transfer")
            # Use the production options with a local destination.
            options = list(arguments[1:-2])
            relay = options.index("-e")
            del options[relay:relay + 2]
            options = [option for option in options if not option.startswith("--rsync-path=")]
            return capture([rsync, *options, arguments[-2], f"{mirror}/"], deadline)

        with (mock.patch.dict(os.environ, self.environment, clear=True),
              mock.patch.object(controller, "capture", side_effect=fixture_copy)):
            config = eligibility.read_config(self.config_path)
            repository = eligibility.KnownRepository("hana")
            self.assertFalse(offload.copy_tree(controller, config, request, repository))
            self.assertIn("fixture interrupted transfer", controller.copy_failure_detail)
            self.assertEqual({path.name for path in mirror.iterdir()}, {"copied", "same-time", "removed"})
            self.assertEqual((mirror / damaged.name).read_bytes(), b"lost")
            self.assertTrue(offload.copy_tree(controller, config, request, repository))

        first, retry = destinations
        self.assertEqual(first, retry)
        self.assertEqual({path.name: path.read_bytes() for path in mirror.iterdir()},
                         {path.name: path.read_bytes() for path in self.repository.iterdir() if path.is_file()})


if __name__ == "__main__":
    _ = unittest.main()
