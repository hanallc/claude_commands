"""Check Mac outage recovery with private transport and admission fixtures."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import ExitStack
from dataclasses import replace
import fcntl
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import time
from typing import cast
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
        self.fixture["run_ssh_status"] = 143
        self.set_leftover_answer("ssh_error")

        interrupted = self.process_command(separate_stderr=True)

        self.assertEqual(interrupted.returncode, 75, interrupted.stdout + interrupted.stderr)
        self.assertIn("running tests before the outage", interrupted.stdout)
        self.assert_wire("lost", "cleanup_unconfirmed")
        self.assert_claim_is_kept()
        self.assert_claimed_commands()
        cleanup, = self.leftover_events()
        self.assertEqual(cleanup["args"][-2], "mac")
        return self.remote_claim()

    def set_transport_outage(self) -> None:
        self.fixture["probe_lines"].append("dispatch_mac_id=11111111-2222-3333-4444-555555555555")
        self.fixture["run_lines"] = [{"stream": "stderr", "text": "Timeout, server mac not responding."}]
        self.fixture["run_status_reported"] = False
        self.fixture["run_ssh_status"] = 255
        self.sync_fixture()

    def test_native_lost_run_saves_its_fence_until_the_next_claim_writer(self) -> None:
        self.set_transport_outage()
        result = self.process_command(separate_stderr=True)
        self.assertEqual(result.returncode, 75, result.stdout + result.stderr)
        wire_result = dict(line.split("=", 1) for line in self.result_path.read_text().splitlines())
        self.assertEqual((wire_result["mac"], wire_result["reason"], wire_result["outage"]),
                         ("lost", "cleanup_unconfirmed", "MAC_STATUS_MISSING"))
        self.assertGreater(float(wire_result["outage_since"]), 0.0)
        self.assertEqual(self.leftover_events(), [])

        with mock.patch.dict(os.environ, self.environment, clear=True):
            paths = claim_state.state_paths("mac")
            claim, = claim_state.read_claims(paths)
            fence, = claim_state.read_lost_mac_fences(paths)
            self.assertEqual(fence.record, claim.record)
            self.assertTrue(claim_state.claim_owner_ended(claim.record))
            pending = claim_state.claim_pending_start(paths, claim.path, claim.record)
            self.assertTrue(pending.exists())
            args = mac_test.CliArguments()
            args.pid, args.worktree = os.getpid(), str(self.repository)
            with (mock.patch.object(mac_test, "start_claim_watcher"), claim_state.locked(paths.lock)):
                next_claim = mac_test.claim_once(args, paths, claim.scope,
                    session=claim_state.submitting_session(claim_state.CALLING_SESSION))
            self.assertEqual(next_claim, mac_test.ClaimBusy(fence.record))
            self.assertEqual(claim_state.read_claims(paths), ())
            self.assertEqual(claim_state.read_lost_mac_fences(paths), (fence,))
            self.assertTrue(pending.exists())
        self.assertEqual(fence.path.name, self.claim_file.name)
        self.assertIsInstance(fence.scope, claim_state.RepositoryScope)
        assert isinstance(fence.scope, claim_state.RepositoryScope)
        self.assertEqual(fence.scope.name, "hana")

    def test_native_lost_run_keeps_a_claim_after_takeover(self) -> None:
        self.set_transport_outage()
        stream = offload.ProcessController.stream_mac_run
        saved_replacement: list[RemoteClaimRecord] = []

        def disconnect(controller: offload.ProcessController, arguments: Sequence[str],
                       cleanup_limit: offload.CleanupAfterVerdict | offload.CleanupOnly
                       | offload.NoCommandTimeLimit = offload.NO_COMMAND_TIME_LIMIT) -> offload.RemoteRunStatus:
            result = stream(controller, arguments, cleanup_limit)
            paths = claim_state.state_paths("mac")
            with claim_state.locked(paths.lock):
                record = self.remote_claim()
                record["proc_start"] = "replacement-owner-start"
                mac_test.write_json(self.claim_file, dict(record))
                saved_replacement.append(record)
            return result

        with mock.patch.object(offload.ProcessController, "stream_mac_run", autospec=True, side_effect=disconnect):
            result = self.command(separate_stderr=True)
        self.assertEqual(result.returncode, 75, result.stdout + result.stderr)
        self.assert_wire("lost", "cleanup_unconfirmed")
        self.assertNotIn("outage=", self.result_path.read_text())
        replacement, = saved_replacement
        self.assertEqual(self.remote_claim(), replacement)
        self.assertEqual(self.leftover_events(), [])
        with mock.patch.dict(os.environ, self.environment, clear=True):
            self.assertEqual(claim_state.read_lost_mac_fences(claim_state.state_paths("mac")), ())

    def test_native_failed_fence_save_prevents_outage_publication(self) -> None:
        self.set_transport_outage()
        with mock.patch.object(claim_state, "save_lost_mac_fence", side_effect=OSError("fixture disk error")) as save:
            result = self.command(separate_stderr=True)
        self.assertEqual(result.returncode, 75, result.stdout + result.stderr)
        self.assert_wire("lost", "cleanup_unconfirmed")
        self.assertNotIn("outage=", self.result_path.read_text())
        self.assertIn("could not save the lost Mac fence", result.stdout)
        save.assert_called_once()
        with mock.patch.dict(os.environ, self.environment, clear=True):
            paths = claim_state.state_paths("mac")
            claim, = claim_state.read_claims(paths)
            self.assertEqual(claim.record["pid"], os.getpid())
            self.assertTrue(claim_state.claim_pending_start(paths, claim.path, claim.record).exists())
            self.assertEqual(claim_state.read_lost_mac_fences(paths), ())

    def test_native_reconnect_checks_the_original_attempt_before_the_deadline(self) -> None:
        self.set_transport_outage()
        self.fixture["run_lines"] = [{"stream": "stderr", "text": "client_loop: send disconnect: Broken pipe"}]
        self.sync_fixture()
        self.set_leftover_answer("clean")
        machine_id = "11111111-2222-3333-4444-555555555555"
        write_shell(self.bin_directory / "ioreg", f"""printf '%s\n' '"IOPlatformUUID" = "{machine_id}"'""")
        run = cast(Callable[..., subprocess.CompletedProcess[bytes]], subprocess.run)

        for finished in (True, False):
            with self.subTest(finished=finished):
                checks: list[str] = []
                offset = 0.0

                def advance(seconds: float) -> None:
                    nonlocal offset
                    offset += seconds

                def reconnect(command: Sequence[str], **options: object) -> subprocess.CompletedProcess[bytes]:
                    if command[0] != "ssh" or "dispatch_reconnect=" not in command[-1]:
                        return run(command, **options)
                    record = claim_state.read_run(self.claim_file)
                    assert not isinstance(record, claim_state.NoRun)
                    identity = claim_state.claim_identity(record)
                    scope = claim_state.claim_fence_scope_key(self.claim_file, record)
                    relative = offload.start_fence_relative(identity, "mac", scope_key=scope)
                    self.assertEqual(shlex.split(command[-1])[-3], str(relative))
                    timeout = options["timeout"]
                    assert isinstance(timeout, float)
                    self.assertGreater(timeout, 0)
                    self.assertLessEqual(timeout, eligibility.unreachable_wait_seconds())
                    checks.append(identity.attempt)
                    with ExitStack() as locks:
                        if finished:
                            prepare = offload.prepare_start_fence_command(identity, "mac", scope_key=scope)
                            key = offload.pending_start_key(identity)
                            report = shlex.join([sys.executable, "-B", "-c", offload.MAC_VERDICT_REPORTER,
                                                 str(relative), key, "42"])
                            finish = offload.fenced_start_command(identity, "mac", report, scope_key=scope)
                            complete = run(["/bin/sh", "-c", prepare + "\n" + finish],
                                           capture_output=True, check=True, timeout=5)
                            self.assertIn(b"mac_exit=42\n", complete.stdout)
                        else:
                            lock_path = (Path(self.environment["HOME"]) / relative).with_suffix(".lock")
                            lock_path.parent.mkdir(parents=True, exist_ok=True)
                            lock = locks.enter_context(lock_path.open("ab"))
                            fcntl.flock(lock, fcntl.LOCK_EX)
                        return run(["/bin/sh", "-c", command[-1]], **options)

                clock = mock.Mock(wraps=time, monotonic=lambda: time.monotonic() + offset, sleep=advance)
                with (mock.patch.object(offload, "time", clock),
                      mock.patch.object(subprocess, "run", side_effect=reconnect)):
                    result = self.command(separate_stderr=True)
                self.assertEqual(len(checks), 1, result.stdout + result.stderr)
                wire_result = self.result_path.read_text().splitlines()
                self.assertFalse(any(line.startswith("outage=") for line in wire_result))
                if finished:
                    self.assertIn("mac=failed", wire_result)
                    self.assertEqual(result.returncode, 42)
                    self.assertFalse(self.claim_file.exists())
                else:
                    self.assertIn("mac=lost", wire_result)
                    self.assertTrue(self.claim_file.exists())
                    kept = self.remote_claim()
                    self.assertEqual(checks, [kept["attempt"]])

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

    # The probe finds no process and keeps the claim for the next run (F1a).
    # A kept claim holds no work, so the Mac stays free and allowed.
    def test_service_observation_after_reboot_keeps_the_claim_and_leaves_the_mac_free(self) -> None:
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

        assert isinstance(evidence, placement.FreshHostEvidence)
        (reference, observation), = evidence.executions
        self.assertEqual({
            "eligibility": evidence.eligibility,
            "observed claim": reference.claim_id,
            "observation": type(observation),
            "selector answer": (self.stub_directory / "selector-answer").read_text(),
            "rsync calls": self.events_for("rsync"),
            "free": free,
            "kept claims": [claim.record["claim_id"] for claim in remaining],
            "claims holding work": [claim.record["claim_id"] for claim in remaining
                                    if mac_test.run_holds_work(claim.record)],
        }, {
            "eligibility": placement.AllowedHost(),
            "observed claim": kept["claim_id"],
            "observation": execution_identity.ExecutionEvidenceUnavailable,
            "selector answer": "",
            "rsync calls": [],
            "free": "free",
            "kept claims": [kept["claim_id"]],
            "claims holding work": [],
        })

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
