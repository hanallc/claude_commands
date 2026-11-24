"""Offload cleanup leftovers contracts."""

from __future__ import annotations

from collections.abc import Callable, Generator, Sequence
from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
from typing import BinaryIO, TextIO, cast
import unittest
from unittest import mock
from scripts.build_queue.admission_test_support import caller_admission
from scripts.copies import copies
from scripts.mac_test import claim_state, eligibility, offload
from scripts.mac_test.mirror_root import MacMirrorRoot
from scripts.lint.memory_test_support import UNREACHABLE_WAIT_FIXTURE_SECONDS, write_config_fixture
from scripts.mac_test.offload_test_commands import SELECTOR_STUB, write_shell
from scripts.mac_test.offload_test_support import FIXTURE_MIRROR_ROOT, HANA_COMMAND_PATTERN, SelectorEvent, write_mac_target_registry
from scripts.mac_test.offload_cleanup_test_support import SeededCleanupFixture


class OffloadCommandTests(SeededCleanupFixture):
    def doc_with_cleanup_timeout(self, *, live_work: bool, mirror_root: str = FIXTURE_MIRROR_ROOT) -> subprocess.CompletedProcess[str]:
        self.write_portable_workspace()
        (Path(self.environment["HOME"]) / mirror_root / "hana").mkdir(parents=True)
        self.fixture.update(selector_shell="sh", run_cleanup_line="", run_fence_reported=False)
        self.sync_fixture()
        selectors = self.stub_directory / "selectors"
        selectors.mkdir()
        write_shell(selectors / "pgrep", "printf '42\\n'" if live_work else "exit 1")
        write_shell(selectors / "lsof", "printf 'p41\\nn/home/fixture\\n'")
        write_shell(selectors / "sleep", "exit 99")
        write_shell(selectors / "sysctl", "printf 'fixture-boot\\n'")
        read_lines = offload.bounded_output_lines

        def timeout_after_verdict(output: BinaryIO | TextIO,
                                  remaining: Callable[[], float | None]) -> Generator[bytes]:
            for line in read_lines(output, remaining):
                yield line
                if line == b"mac_exit=0\n":
                    raise TimeoutError

        with mock.patch.object(offload, "bounded_output_lines", side_effect=timeout_after_verdict):
            result = self.command("--workspace", verb="doc", package="", runner_options=("--target", "mac"),
                                  separate_stderr=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_wire("passed", "")
        self.assertIn("cleanup timed out", result.stdout)
        return result

    def test_passed_doc_releases_claim_after_cleanup_stream_timeout(self) -> None:
        _ = self.doc_with_cleanup_timeout(live_work=False)
        self.assertFalse(self.claim_file.exists())
        self.assertEqual(list((self.state_directory / "pending-starts").iterdir()), [])
        proof, = self.leftover_events()
        self.assertTrue(proof["claimed"])
        self.assertNotIn("kill ", proof["args"][-1])

    def test_passed_doc_keeps_claim_while_remote_work_remains(self) -> None:
        result = self.doc_with_cleanup_timeout(live_work=True)
        claim = claim_state.read_run(self.claim_file)
        assert not isinstance(claim, claim_state.NoRun)
        saved = claim_state.saved_mac_repository(claim)
        assert isinstance(saved, claim_state.MacMirrorIdentity)
        self.assertEqual(saved.dispatch, {"boot_id": "fixture-boot",
                                         "mirror_path": f"{offload.mac_mirror_root().relative}/{saved.mirror_name}",
                                         "home": self.environment["HOME"]})
        self.assertEqual(len(list((self.state_directory / "pending-starts").iterdir())), 1)
        proof, = self.leftover_events()
        self.assertTrue(proof["claimed"])
        self.assertNotIn("kill ", proof["args"][-1])
        self.assertIn("stays busy", result.stderr)

    def test_lost_link_before_dispatch_save_keeps_claim(self) -> None:
        self.fixture.update(run_dispatch_reported=False, run_status_reported=False, run_ssh_status=255)
        self.sync_fixture()

        def empty_probe(*_args: object, **_options: object) -> offload.LeftoverObservation:
            claim = claim_state.read_run(self.claim_file)
            assert not isinstance(claim, claim_state.NoRun)
            key = offload.pending_start_key(claim_state.claim_identity(claim))
            return offload.LeftoverObservation(offload.LeftoversEnded(), f"dispatch_start_settled={key}\n")

        with mock.patch.object(offload, "read_leftovers", side_effect=empty_probe) as probe:
            result = self.command(separate_stderr=True)
        self.assertTrue(self.claim_file.exists(), result.stdout + result.stderr)
        self.assertEqual(len(list((self.state_directory / "pending-starts").iterdir())), 1)
        probe.assert_not_called()

    def test_root_change_after_lease_keeps_copy_command_and_probe_on_leased_path(self) -> None:
        first = MacMirrorRoot(".local/state/mac-test/first-root")
        second = MacMirrorRoot(".local/state/mac-test/second-root")
        configured = first
        lease = copies.remote_lease

        @contextmanager
        def change_root(mirror: copies.RemoteMirror) -> Generator[None]:
            nonlocal configured
            with lease(mirror):
                configured = second
                yield

        with mock.patch.object(copies, "remote_lease", change_root), \
             mock.patch.object(offload, "mac_mirror_root", side_effect=lambda: configured):
            _ = self.doc_with_cleanup_timeout(live_work=True, mirror_root=first.relative)
        claim = claim_state.read_run(self.claim_file)
        assert not isinstance(claim, claim_state.NoRun)
        saved = claim_state.saved_mac_repository(claim)
        assert isinstance(saved, claim_state.MacMirrorIdentity) and saved.dispatch is not None
        path = f"{first.relative}/{saved.mirror_name}"
        self.assertEqual(saved.dispatch["mirror_path"], path)
        copy, = self.events_for("rsync")
        self.assertTrue(copy["args"][-1].endswith(f":{path}/"), copy["args"])
        command, = [event["args"][-1] for event in self.events_for("ssh") if "lint doc" in event["args"][-1]]
        self.assertIn(f"cd {path} &&", command)
        proof, = self.leftover_events()
        self.assertIn(first.relative.removeprefix(".") + "/hana", proof["args"][-1])
        self.assertNotIn(second.relative, proof["args"][-1])

    def command_with_changed_cleanup_timeout(self, value: str) -> subprocess.CompletedProcess[str]:
        self.config["leftover_timeout_s"] = "10"
        self.sync_config()
        _ = write_config_fixture(self.root / "transport-mac.conf", self.config_path.read_text(), template="mac_test")
        write_mac_target_registry(self.root, "transport-mac.conf")
        settings = self.root / "cleanup-settings.conf"
        configuration = self.config_path.read_text()
        _ = settings.write_text(configuration)
        self.fixture["run_cleanup_line"] = ""
        self.sync_fixture()
        stream = offload.ProcessController.stream_mac_run

        def change_settings(controller: offload.ProcessController, arguments: Sequence[str],
                            cleanup_limit: offload.CleanupAfterVerdict | offload.CleanupOnly
                            | offload.NoCommandTimeLimit = offload.NO_COMMAND_TIME_LIMIT) -> offload.RemoteRunStatus:
            answer = stream(controller, arguments, cleanup_limit)
            # Keep the admission fixture configuration valid during later passes.
            _ = settings.write_text(configuration.replace(
                "leftover_timeout_s=10\n", f"leftover_timeout_s={value}\n"))
            return answer

        with mock.patch.object(offload.ProcessController, "stream_mac_run", change_settings):
            return self.command(settings=settings, separate_stderr=True)

    def test_completed_run_reads_the_cleanup_timeout_after_execution(self) -> None:
        with mock.patch.object(offload, "check_leftovers", wraps=offload.check_leftovers) as cleanup:
            result = self.command_with_changed_cleanup_timeout("23")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_wire("passed", "")
        self.assertEqual(cleanup.call_count, 1)
        connection = cast(eligibility.SshConnectionSettings, cleanup.call_args.args[0])
        self.assertEqual(connection.leftover_timeout_s, 23)
        self.assertEqual(len(self.leftover_events()), 1)
        self.assertFalse(self.claim_file.exists())
        self.assertNotIn("Traceback", result.stdout + result.stderr)

    def test_completed_run_keeps_the_claim_when_the_cleanup_timeout_becomes_unusable(self) -> None:
        for value in ("broken", "0", "nan"):
            with self.subTest(leftover_timeout_s=value):
                self.reset_leftover_case()
                result = self.command_with_changed_cleanup_timeout(value)
                admission = caller_admission(self, self.root, self.environment)
                # Force the settings read that raced the command's completion.
                with admission.loop_turn():
                    self.assertTrue(admission.server.placement_requests)
                    for request in admission.server.placement_requests.values():
                        for candidate in request.constraints.candidates:
                            _ = admission.server.host_evidence_key(request, candidate.destination)
                    _ = admission.run_pass()
                self.assertEqual(eligibility.read_connection_settings(self.config_path).leftover_timeout_s, 10)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assert_wire("passed", "")
                self.assertTrue(self.claim_file.exists())
                self.assertEqual(self.remote_claim()["remote_host"], "mac")
                self.assertEqual(self.leftover_events(), [])
                self.assertIn("leftover_timeout_s", result.stderr)
                self.assertIn("stays busy", result.stderr)
                self.assertNotIn("Traceback", result.stdout + result.stderr)

    def test_leftover_check_uses_connection_settings_and_the_recorded_host(self) -> None:
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, self.environment)))
        connection = eligibility.SshConnectionSettings("current-host", 7, 19, UNREACHABLE_WAIT_FIXTURE_SECONDS)
        repository = eligibility.KnownRepository("hana")
        response = subprocess.CompletedProcess(["ssh"], 0, "leftovers=0\nrc=0\n", "")
        with mock.patch.object(subprocess, "run", return_value=response) as run:
            result = offload.check_leftovers(connection, "retained-host", repository)
        self.assertEqual(result, offload.LeftoversEnded())
        arguments = cast(list[str], run.call_args.args[0])
        self.assertEqual(arguments[-2], "retained-host")
        self.assertNotIn("current-host", arguments)
        self.assertIn("ConnectTimeout=7", arguments)
        self.assertEqual(arguments[-1], offload.leftover_check_script(repository))
        self.assertEqual(run.call_args.kwargs["timeout"], 19)

    def test_connection_reader_ignores_execution_values_and_reads_changes_at_use(self) -> None:
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, self.environment)))
        self.config["free_floor_gib"] = "broken"
        self.config["host"] = "first-host"
        self.config["connect_timeout_s"] = "7"
        self.config["leftover_timeout_s"] = "19"
        self.sync_config()
        self.assertEqual(eligibility.read_connection_settings(self.config_path),
                         eligibility.SshConnectionSettings("first-host", 7, 19, UNREACHABLE_WAIT_FIXTURE_SECONDS))
        self.config["host"] = "second-host"
        self.config["connect_timeout_s"] = "8"
        self.config["leftover_timeout_s"] = "21"
        self.sync_config()
        self.assertEqual(eligibility.read_connection_settings(self.config_path),
                         eligibility.SshConnectionSettings("second-host", 8, 21, UNREACHABLE_WAIT_FIXTURE_SECONDS))

    def test_unconfirmed_cleanup_checks_leftovers_before_release_and_keeps_test_result(self) -> None:
        self.fixture["run_cleanup_line"] = ""
        self.fixture["run_ssh_status"] = 255
        for status in (0, 42):
            for outcome in ("clean", "remaining", "missing_status", "ssh_error"):
                with self.subTest(status=status, outcome=outcome):
                    self.reset_leftover_case()
                    self.fixture["run_status_line"] = f"mac_exit={status}"
                    self.set_leftover_answer(outcome)
                    result = self.process_command(separate_stderr=True)
                    self.assertEqual(result.returncode, status, result.stdout + result.stderr)
                    self.assert_wire("passed" if status == 0 else "failed", "")
                    self.assertEqual(result.stdout.count("Mac build output cleanup unconfirmed\n"), 1)
                    checks = self.leftover_events()
                    self.assertEqual(len(checks), 1)
                    self.assertTrue(checks[0]["claimed"], checks[0])
                    self.assert_leftover_selectors(checks[0]["args"][-1])
                    if outcome == "clean":
                        self.assertFalse(self.claim_file.exists())
                    else:
                        self.assert_claim_is_kept()
                        self.assert_leftover_diagnostic(result.stderr, outcome)

    def test_run_time_includes_remote_cleanup(self) -> None:
        self.fixture["run_lines"] = [
            {"stream": "stdout", "text": "partial output"}
        ]
        self.fixture["run_status_reported"] = True
        self.fixture["run_cleanup_line"] = ""
        self.sync_fixture()
        result = self.command_with_cleanup_clock()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("partial output\n", result.stdout)
        self.assertIn("Mac build output cleanup unconfirmed\n", result.stdout)
        wire = self.wire_result()
        self.assertEqual(wire["mac"], "passed")
        self.assertEqual(wire["reason"], "")
        recorded_seconds = float(wire["seconds"])
        self.assertEqual(recorded_seconds, 8.0)
        self.assertFalse(self.claim_file.exists())
        cleanup_events = [
            event
            for event in self.events_for("ssh")
            if "select_leftovers()" in event["args"][-1]
        ]
        self.assertEqual(len(cleanup_events), 1, self.events())
        cleanup = cleanup_events[0]
        self.assertTrue(cleanup["claimed"], cleanup)
        self.assert_leftover_selectors(cleanup["args"][-1])

    def test_run_keeps_claim_when_leftover_check_does_not_prove_completion(self) -> None:
        self.fixture["run_status_reported"] = True
        self.fixture["run_cleanup_line"] = ""
        for outcome in ("remaining", "missing_status", "timeout", "ssh_error"):
            with self.subTest(outcome=outcome):
                self.reset_leftover_case()
                self.set_leftover_answer(outcome)
                result = self.command_with_cleanup_clock(separate_stderr=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assert_wire("passed", "")
                self.assertIn("Mac build output cleanup unconfirmed\n", result.stdout)
                self.assert_leftover_diagnostic(result.stderr, outcome)
                self.assert_claim_is_kept()
                self.assertEqual(len(self.leftover_events()), 1)
                self.assert_leftover_selectors(self.leftover_events()[0]["args"][-1])
                self.assertEqual(float(self.wire_result()["seconds"]), 13.0 if outcome == "timeout" else 8.0)

    def test_leftover_selectors_complete_under_sh_and_zsh_before_releasing_claim(self) -> None:
        (Path(self.environment["HOME"]) / FIXTURE_MIRROR_ROOT / "hana").mkdir(parents=True)
        selectors = self.stub_directory / "selectors"
        selectors.mkdir()
        for name in ("pgrep", "lsof", "sleep"):
            write_shell(selectors / name, SELECTOR_STUB)
        write_shell(selectors / "sysctl", "printf 'fixture-boot\\n'")
        self.fixture["run_status_reported"] = True
        self.fixture["run_cleanup_line"] = ""
        self.config["leftover_timeout_s"] = "300"
        self.sync_config()
        for shell in ("sh", "zsh"):
            for pgrep_status, lsof_status in ((1, 1), (1, 0), (2, 0)):
                with self.subTest(shell=shell, pgrep_status=pgrep_status, lsof_status=lsof_status):
                    self.reset_leftover_case()
                    self.fixture["selector_shell"] = shell
                    self.fixture["selector_pgrep_status"] = pgrep_status
                    self.fixture["selector_lsof_status"] = lsof_status
                    self.sync_fixture()
                    result = self.process_command(separate_stderr=True)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assert_wire("passed", "")
                    cleanup = self.leftover_events()
                    self.assertEqual(len(cleanup), 1)
                    self.assert_leftover_selectors(cleanup[0]["args"][-1])
                    path = self.stub_directory / "selector-events.jsonl"
                    calls = [cast(SelectorEvent, json.loads(line)) for line in path.read_text().splitlines()]
                    names = [call["command"] for call in calls]
                    self.assertIn("pgrep", names)
                    if pgrep_status == 1:
                        self.assertIn("lsof", names)
                    for call in calls:
                        if call["command"] == "pgrep":
                            self.assertEqual(call["args"][:3], ["-u", str(os.getuid()), "-f"])
                            self.assertTrue(call["args"][3].startswith(HANA_COMMAND_PATTERN + "|[/]"))
                        if call["command"] == "lsof":
                            self.assertEqual(call["args"], ["-a", "-u", str(os.getuid()), "-d", "cwd", "-Fpn"])
                    answer = (self.stub_directory / "selector-answer").read_text()
                    if pgrep_status == 1 and lsof_status == 0:
                        self.assertIn("sleep", names)
                        self.assertEqual(answer.splitlines(), ["leftovers=0", "rc=0"])
                        self.assertFalse(self.claim_file.exists())
                    else:
                        self.assertNotIn("rc=0", answer.splitlines())
                        self.assert_claim_is_kept()
                        self.assert_leftover_diagnostic(result.stderr, "missing_status")



if __name__ == "__main__":
    _ = unittest.main()
