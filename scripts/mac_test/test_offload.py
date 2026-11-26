"""Offload routing and command arguments."""

from __future__ import annotations


from collections.abc import Callable, Sequence
from datetime import datetime, timedelta, timezone
import json
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import ExitStack, closing, redirect_stderr, redirect_stdout
from dataclasses import fields, replace
import io
from pathlib import Path
from typing import TYPE_CHECKING, cast, final, override
from unittest import mock
from scripts.build_queue import execution_identity
from scripts.build_targets import build_targets
from scripts.direct import verify_mac_offload_test_support
from scripts.lint import prebuild
from scripts.lint.memory_test_support import (UNREACHABLE_WAIT_FIXTURE, UNREACHABLE_WAIT_FIXTURE_SECONDS, config_environment,
                                              write_config_fixture,
                                              write_config_fault, commit_config_fixture)

from scripts.mac_test import eligibility, fix_return, floor_claims, mac_queue_run, nextest_log, offload, claim_state
from scripts.build_queue import admission_client, admission_protocol as wire, placement, queue_snapshot
from scripts.build_queue.admission_test_support import AdmissionFixture, caller_admission
from scripts.build_hold.owning_session import NoOwningSession
from scripts.build_hold import override_guard
from scripts.buildlog import repository as buildlog_repository
if TYPE_CHECKING:
    from scripts.lib import copy_record, nix_env, scratch
    from scripts.build_targets import target_config
    from scripts.buildlog import sync as buildlog_sync, health as buildlog_health, record as buildlog_record, store as buildlog_store
else:
    from scripts.lib import copy_record, nix_env, scratch
    from scripts.build_targets import target_config
    from scripts.buildlog import sync as buildlog_sync
    from scripts.buildlog import health as buildlog_health
    from scripts.buildlog import record as buildlog_record
    from scripts.buildlog import store as buildlog_store
from scripts.lib import build_folders, conf, conf_reads, session_folder
from scripts.lib.atomic_file import write_text_atomic
from scripts.production.cli_fixture import invoke_cli
from scripts.copies import copies
from scripts.copies.copies_test_support import copies_environment
from scripts.mac_test.offload_test_commands import install_nextest_log_writer
from scripts.mac_test.offload_test_support import (
    CLAIM_STUB, OffloadCommandFixture, FILTER, FIXTURE_MIRROR_ROOT, HANA, HANA_COMMAND_PATTERN, split_mac_run_command,
)
from scripts.mac_test.test_floor_sweep import install_floor_sweep_stub
from scripts.lib.py_floor_test_support import seed_interpreter_cache
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS

RUN_SUBPROCESS = subprocess.run
START_SUBPROCESS = subprocess.Popen


class GeneratedSourceTests(unittest.TestCase):
    def test_no_envrc_source_requires_generated_source_outside_trusted_roots(self) -> None:
        for live_session in (False, True):
            with self.subTest(live_session=live_session), tempfile.TemporaryDirectory() as session_temporary:
                sessions = Path(session_temporary) / "sessions"
                other_worker = sessions / "other-worker"
                (other_worker / "proof").mkdir(parents=True)
                sentinel = other_worker / "proof/source.txt"
                _ = sentinel.write_text("another worker's source\n")
                index = sessions / ".id"
                index.mkdir()
                (index / "offload-fixture").symlink_to(other_worker.name)
                before = sorted(path.relative_to(sessions) for path in sessions.rglob("*"))
                with mock.patch.dict(os.environ, {
                    "SESSION_TEMP_ROOT": str(sessions if live_session else sessions / "absent"),
                    "CODEX_THREAD_ID": "offload-fixture",
                }):
                    self.assertEqual(session_folder.caller_folder(), other_worker if live_session else None)
                    with tempfile.TemporaryDirectory() as temporary:
                        root = Path(temporary)
                        with mock.patch.dict(os.environ, {
                            "SCRATCH_ROOT": str(root / "scratch"),
                            "SCRATCH_HOLDS": str(root / "holds.json"),
                            "SESSION_TEMP_ROOT": str(root / "absent-session"),
                        }):
                            folder = scratch.new('gate:offload-generated-source', 'proof')
                            self.assertTrue(folder.is_relative_to(root), str(folder))
                            self.assertIsNone(nix_env.nearest_envrc(folder))
                            self.assertFalse(offload.trusted_linux_source(folder))
                            _ = scratch.update_record(folder, lambda record: {
                                **(record.fields if isinstance(record, copy_record.Registered) else {}), 'generated_source': True})
                            self.assertTrue(offload.trusted_linux_source(folder))
                            _ = (folder / '.envrc').write_text('use flake\n')
                            with mock.patch.object(nix_env, 'ensure', return_value=nix_env.Refused('not approved')):
                                self.assertFalse(offload.trusted_linux_source(folder))
                    self.assertFalse(folder.exists())
                self.assertEqual(sorted(path.relative_to(sessions) for path in sessions.rglob("*")), before)
                self.assertEqual(sentinel.read_text(), "another worker's source\n")


class LintVerbTests(unittest.TestCase):
    def test_prebuild_refuses_the_mirror_descriptor_environment_variable(self) -> None:
        name = floor_claims.MIRROR_LEASE_FDS
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "Cargo.toml"
            _ = manifest.write_text(f'''[workspace.metadata.dev.prebuild.fixture-artifact]
target = "wasm32-unknown-unknown"
env = "{name}"
for = ["fixture"]
''')
            stderr = io.StringIO()
            with mock.patch.object(prebuild, "build", return_value=1) as build, \
                 mock.patch.object(os, "execvp") as nextest, redirect_stderr(stderr):
                status = prebuild.main(["--manifest", str(manifest), "--", "cargo", "nextest", "run", "-p", "fixture"])
            self.assertEqual(status, 2, stderr.getvalue())
            self.assertEqual(stderr.getvalue(), f"prebuild: {manifest}: workspace.metadata.dev.prebuild.fixture-artifact"
                             + f".env: the runner sets {name}.\n")
            build.assert_not_called()
            nextest.assert_not_called()

    def test_build_reports_the_cargo_target_directory_after_success(self) -> None:
        lint = Path(__file__).resolve().parents[1] / "lint" / "lint"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            invoke = root / ".claude/scripts/lint/invoke.sh"
            invoke.parent.mkdir(parents=True)
            _ = invoke.write_text('run() { "$@"; }\n')
            cargo = root / "cargo"
            output = str(root / "cargo output")
            _ = cargo.write_text(f'#!{sys.executable}\nimport json, sys\n'
                + 'if sys.argv[1:] == ["build", "--workspace", "--bins"]:\n    raise SystemExit(0)\n'
                + 'assert sys.argv[1:] == ["metadata", "--no-deps", "--format-version", "1"]\n'
                + f'print(json.dumps({{"target_directory": {output!r}}}))\n')
            cargo.chmod(0o755)
            environment = {**os.environ, "HOME": str(root), "PATH": str(root) + os.pathsep + os.environ["PATH"]}
            result = subprocess.run(["bash", str(lint), "build", "--workspace", "--bins"],
                cwd=root, env=environment, capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "build_target_directory=" + output + "\n")

    def test_build_metadata_receives_the_manifest_path_outside_the_workspace(self) -> None:
        for equals in (False, True):
            with self.subTest(equals=equals):
                self.check_build_metadata_option("--manifest-path", equals=equals)

    def test_build_metadata_uses_the_requested_target_directory(self) -> None:
        for equals in (False, True):
            for relative in (False, True):
                with self.subTest(equals=equals, relative=relative):
                    self.check_build_metadata_option("--target-dir", equals=equals, relative=relative)

    def check_build_metadata_option(self, option: str, *, equals: bool, relative: bool = False) -> None:
        lint = Path(__file__).resolve().parents[1] / "lint" / "lint"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            invoke = root / ".claude/scripts/lint/invoke.sh"
            invoke.parent.mkdir(parents=True)
            _ = invoke.write_text('run() { "$@"; }\n')
            workspace = root / "other workspace"
            workspace.mkdir()
            manifest = workspace / "Cargo.toml"
            _ = manifest.write_text('[workspace]\nmembers = []\n')
            output = workspace / "target"
            value = str(manifest)
            if option == "--target-dir":
                value = "relative output" if relative else str(root / "cargo output")
                output = (root / value).resolve()
            arguments = [f"{option}={value}"] if equals else [option, value]
            metadata_arguments = arguments if option == "--manifest-path" else []
            calls = root / "calls"
            cargo = root / "cargo"
            _ = cargo.write_text(
                f'#!{sys.executable}\nimport json, os, sys\nfrom pathlib import Path\n'
                + f'with Path({str(calls)!r}).open("a") as calls:\n'
                + '    calls.write(json.dumps({"args": sys.argv[1:], "target_dir": os.environ.get("CARGO_TARGET_DIR")}) + "\\n")\n'
                + 'if sys.argv[1] == "metadata":\n'
                + f'    output = Path(os.environ.get("CARGO_TARGET_DIR", {str(workspace / "target")!r})).resolve()\n'
                + '    print(json.dumps({"target_directory": str(output)}))\n')
            cargo.chmod(0o755)
            environment = {**os.environ, "HOME": str(root), "PATH": str(root) + os.pathsep + os.environ["PATH"]}
            _ = environment.pop("CARGO_TARGET_DIR", None)
            result = subprocess.run(["bash", str(lint), "build", "--workspace", *arguments],
                cwd=root, env=environment, capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(calls.read_text().splitlines(), [
                json.dumps({"args": ["build", "--workspace", *arguments], "target_dir": None}),
                json.dumps({"args": ["metadata", "--no-deps", "--format-version", "1", *metadata_arguments],
                            "target_dir": value if option == "--target-dir" else None}),
            ])
            self.assertEqual(result.stdout, "build_target_directory=" + str(output) + "\n")

    def test_check_and_mend_check_use_the_public_lint_command_without_rewrites(self) -> None:
        lint = Path(__file__).resolve().parents[1] / "lint" / "lint"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            invoke = root / ".claude/scripts/lint/invoke.sh"
            invoke.parent.mkdir(parents=True)
            _ = invoke.write_text(
                'run() { printf "%s\\n" "$@"; }\n'
                + 'invoke_mend() { printf "%s\\n" mend "$@"; }\n')
            environment = {**os.environ, "HOME": str(root), "BUILDLOG_DIR": str(root / "buildlog")}
            for verb, expected in (("check", ["cargo", "check", "--workspace", "--all-features"]),
                                   ("mend-check", ["mend", "--fail-on-warn", "--workspace", "--all-features"])):
                with self.subTest(verb=verb):
                    answer = subprocess.run(["bash", str(lint), verb, "--workspace", "--all-features"],
                                            env=environment, cwd=root, capture_output=True, text=True, check=False)
                    self.assertEqual(answer.returncode, 0, answer.stderr)
                    self.assertEqual(answer.stdout.splitlines(), expected)

    def check_scope_start(self, *, symlink: bool) -> None:
        scripts = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lint_dir = root / ".claude/scripts/lint"
            lint_dir.mkdir(parents=True)
            for name in ("lint", "scope.py"):
                _ = shutil.copy2(scripts / "lint" / name, lint_dir / name)
            library = lint_dir.parent / "lib"
            library.mkdir()
            _ = shutil.copy2(scripts / "lib/py", library / "py")
            _ = (lint_dir / "invoke.sh").write_text(
                'run() { printf "%s\\n" "$@"; }\n'
                + 'invoke_clippy() { printf "%s\\n" cargo clippy "$@"; }\n'
                + 'invoke_mend() { printf "%s\\n" cargo mend "$@"; }\n')
            workspace = root / "workspace"
            workspace.mkdir()
            _ = (workspace / "Cargo.toml").write_text('[workspace]\nmembers=["one", "two"]\n')
            for name in ("one", "two"):
                member = workspace / name
                (member / "src").mkdir(parents=True)
                _ = (member / "Cargo.toml").write_text(
                    f'[package]\nname="{name}"\nversion="0.1.0"\n')
                _ = (member / "src/lib.rs").write_text("")
            front = root / "bin"
            front.mkdir()
            metadata = {
                "packages": [{"name": name, "id": name,
                              "manifest_path": str(workspace / name / "Cargo.toml")}
                             for name in ("one", "two")],
                "workspace_members": ["one", "two"],
            }
            cargo = front / "cargo"
            _ = cargo.write_text(
                '#!/bin/sh\n[ "$1" = metadata ] || exit 99\n'
                + "printf '%s\\n' " + shlex.quote(json.dumps(metadata)) + "\n")
            cargo.chmod(0o755)
            seed_interpreter_cache(root / "cache")
            environment = {**os.environ, "HOME": str(root), "XDG_CACHE_HOME": str(root / "cache"),
                           "PATH": str(front) + os.pathsep + os.environ["PATH"]}
            _ = environment.pop("CLAUDE_PYTHON", None)
            if symlink:
                command = front / "lint"
                command.symlink_to(lint_dir / "lint")
                cwd = workspace
            else:
                command = Path("lint")
                cwd = lint_dir
            for verb, expected in (
                ("check", ["cargo", "check", "--workspace"]),
                ("clippy", ["cargo", "clippy", "--workspace", "--all-targets"]),
                ("mend", ["cargo", "mend", "--workspace"]),
            ):
                with self.subTest(verb=verb):
                    manifest = ["--manifest-path", str(workspace / "Cargo.toml")]
                    answer = subprocess.run(
                        ["bash", str(command), verb, *manifest], env=environment, cwd=cwd,
                        capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS,
                    )
                    self.assertEqual(answer.returncode, 0, answer.stderr)
                    self.assertEqual(answer.stdout.splitlines(), [*expected, *manifest])

    def test_lint_symlink_in_another_folder_keeps_workspace_scope(self) -> None:
        self.check_scope_start(symlink=True)

    def test_lint_filename_in_its_folder_keeps_workspace_scope(self) -> None:
        self.check_scope_start(symlink=False)


class RunnerCompletionTests(unittest.TestCase):
    def test_missing_stdout_preserves_the_published_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "result"
            request = offload.OffloadRequest(root, "hana", "call", offload.ResultFile(path), (),
                                             owner="verify", holder_session="fixture",
                                             fetch=offload.FetchFolder("output"))
            result = offload.PassedOnMac(1, offload.Fetched(root / "fetched", 12))
            controller = offload.ProcessController()
            with mock.patch.object(sys, "stdout", None):
                status = offload.publish_offload_result(controller, request, result)
                offload.report_offload_completion(controller, request, result, 0)
                offload.flush_cli_output()
            self.assertEqual(status, 0)
            self.assertIn("fetch=fetched", path.read_text().splitlines())

    def test_missing_stderr_preserves_the_published_result_and_continues_recording(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "result"
            request = offload.OffloadRequest(root, "hana", "call", offload.ResultFile(path), (),
                                             owner="verify", holder_session="fixture")
            result = offload.PassedOnMac(1)
            controller = offload.ProcessController()
            controller.pending = offload.PendingHanaOperation()
            with (mock.patch.object(sys, "stderr", None),
                  redirect_stdout(io.StringIO()) as output,
                  mock.patch.object(offload, "report_placement_result", side_effect=OSError),
                  mock.patch.object(offload, "persist_attempt") as attempt):
                status = offload.publish_offload_result(controller, request, result)
                offload.report_offload_completion(controller, request, result, 0)
                offload.flush_cli_output()
            self.assertEqual(status, 0)
            self.assertIn("mac=passed", path.read_text().splitlines())
            self.assertEqual(output.getvalue(), "")
            attempt.assert_called_once()

    def test_broken_output_pipe_does_not_change_the_process_exit_status(self) -> None:
        program = (
            "from scripts.mac_test.offload import flush_cli_output\n"
            "print('Mac summary')\n"
            "flush_cli_output()\n"
            "raise SystemExit(7)\n"
        )
        reader, writer = os.pipe()
        os.close(reader)
        environment = {key: value for key, value in os.environ.items() if key != "PYTHONUNBUFFERED"}
        with os.fdopen(writer, "wb") as pipe:
            result = subprocess.run([sys.executable, "-c", program], stdout=pipe, stderr=subprocess.PIPE,
                                    cwd=Path(__file__).resolve().parents[2], env=environment, text=True,
                                    check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertEqual(result.stderr, "")

    def test_missing_completion_reports_the_runner_exit_without_claiming_link_loss(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            answer = offload.ProcessController().stream_mac_run([
                "/bin/sh", "-c", "echo admission-wait-ended; exit 143",
            ])
        self.assertIsInstance(answer, offload.MissingMacStatus)
        if not isinstance(answer, offload.MissingMacStatus):
            self.fail("The runner did not send a completion marker")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "result"
            target = target_config.SshTarget("linux", "linux", Path(directory) / "linux.conf")
            with redirect_stdout(output), mock.patch.object(target_config, "read_targets", return_value=(target,)):
                status = offload.write_result(offload.LostMacRun(1, answer.detail), offload.ResultFile(path), target="linux")
            self.assertEqual(status, 75)
            self.assertIn("reason=runner exited with status 143 without a final status\n", path.read_text())
        self.assertIn("the linux runner exited with status 143 without a final status", output.getvalue())
        self.assertNotIn("link", output.getvalue())
        self.assertNotIn("running on natedev instead", output.getvalue())

        wire = "admission-wait-ended café\r\n"
        for stream in (io.StringIO(wire), io.BytesIO(wire.encode("utf-8"))):
            with self.subTest(stream=type(stream).__name__):
                controller = offload.ProcessController()
                process = mock.Mock(stdout=stream, wait=mock.Mock(return_value=143))
                with mock.patch.object(subprocess, "Popen", return_value=process), \
                     redirect_stdout(io.StringIO()) as streamed:
                    answer = controller.stream_mac_run(["ssh", "fixture", "run"])
                self.assertEqual(answer, offload.MissingMacStatus(
                    "runner exited with status 143 without a final status"))
                self.assertEqual(streamed.getvalue(), wire.replace("\r\n", "\n"))
                self.assertEqual(controller.attempt_evidence.traffic.transfers, [{
                    "operation": "ssh_run", "sent_bytes": 3,
                    "received_bytes": len(wire.encode("utf-8")), "complete": True,
                }])


    def test_capture_timeout_closes_the_pipes_before_reaping(self) -> None:
        arguments = ["ssh", "fixture", "true"]
        controller = offload.ProcessController()
        with io.BytesIO() as stdout, io.BytesIO() as stderr:
            def reap() -> int:
                self.assertTrue(stdout.closed)
                self.assertTrue(stderr.closed)
                return -9

            communicate = mock.Mock(side_effect=[subprocess.TimeoutExpired(arguments, 1, b"partial"), (b"partial", b"")])
            wait = mock.Mock(side_effect=reap)
            process = mock.Mock(stdout=stdout, stderr=stderr, communicate=communicate, wait=wait)
            with mock.patch.object(subprocess, "Popen", return_value=process):
                result = controller.capture(arguments, offload.CommandTimeLimit(1))
            self.assertIsInstance(result, offload.CommandTimedOut)
            communicate.assert_called_once_with(timeout=1)
            wait.assert_called_once_with()
            self.assertFalse(controller.attempt_evidence.traffic.transfers[0]["complete"])

    def test_release_deadline_stops_the_helpers_ssh_child(self) -> None:
        ssh = """
import socket
import sys

with socket.socket(fileno=int(sys.argv[1])) as peer:
    peer.sendall(b"+")
    peer.recv(1)
"""
        helper = """
import subprocess
import sys

fd = int(sys.argv[1])
subprocess.run([sys.executable, "-I", "-S", "-c", sys.argv[2], str(fd)], pass_fds=(fd,), check=True)
"""
        controller = offload.ProcessController()
        identity = claim_state.ClaimIdentity("fixture-claim", "fixture-episode", "fixture-attempt")
        controller.claim_identity = identity
        reader, sender = socket.socketpair()
        with reader, sender:
            reader.settimeout(TEST_HANG_GUARD_SECS)

            def start(arguments: Sequence[str], *, stdout: int, stderr: int,
                      start_new_session: bool = False) -> subprocess.Popen[bytes]:
                self.assertEqual(arguments[2], "release")
                descriptor = sender.fileno()
                process = START_SUBPROCESS([sys.executable, "-I", "-S", "-c", helper, str(descriptor), ssh],
                                            stdout=stdout, stderr=stderr, pass_fds=(descriptor,),
                                            start_new_session=start_new_session)
                sender.close()
                try:
                    self.assertEqual(reader.recv(1), b"+")
                except BaseException:
                    process.kill()
                    _ = process.wait()
                    raise
                return process

            with mock.patch.object(subprocess, "Popen", side_effect=start):
                result = offload.release_claim(controller, deadline=offload.CommandTimeLimit(1))

            self.assertIsInstance(result, offload.ClaimReleaseUnconfirmed)
            self.assertEqual(controller.claim_identity, identity)
            reader.settimeout(1)
            try:
                self.assertEqual(reader.recv(1), b"")
            except TimeoutError:
                self.fail("The SSH child survived the claim release deadline")

    def test_split_utf8_tail_preserves_status_and_counts_raw_bytes(self) -> None:
        wire = b"mac_test: tail fragment: \xa9\nmac_exit=42\nmac_cleanup=kept\n"
        process = mock.Mock(stdout=io.BytesIO(wire), wait=mock.Mock(return_value=0))
        controller = offload.ProcessController()
        with mock.patch.object(subprocess, "Popen", return_value=process), redirect_stdout(io.StringIO()) as output:
            result = controller.stream_mac_run(["ssh", "fixture", "run"])
        self.assertEqual(result, offload.ReportedMacStatus(42, offload.CleanupKept()))
        self.assertEqual(output.getvalue(), "mac_test: tail fragment: \ufffd\n")
        self.assertEqual(controller.attempt_evidence.traffic.transfers, [{
            "operation": "ssh_run", "sent_bytes": 3,
            "received_bytes": len(wire), "complete": True,
        }])


class CompletedRunSyncTests(unittest.TestCase):
    def test_finished_runs_exchange_after_release_and_preserve_results(self) -> None:
        for target in ("linux", "mac"):
            for status in (0, 42):
                for fails in (False, True):
                    with self.subTest(target=target, status=status, fails=fails), tempfile.TemporaryDirectory() as directory:
                        self.check_finished_run(Path(directory), target, status, fails)

    def check_finished_run(self, root: Path, target: str, status: int, fails: bool) -> None:
        configured = target_config.SshTarget(target, "linux" if target == "linux" else "macos", root / "target.conf")
        config = eligibility.OffloadConfig(True, eligibility.SshConnectionSettings("linux-build", 1, 1, UNREACHABLE_WAIT_FIXTURE_SECONDS), 1, 1, 1, "1", {})
        request = offload.OffloadRequest(root, "hana", "call", offload.NO_RESULT_FILE, (), owner="verify", target=target, holder_session="fixture-session")
        controller = offload.ProcessController()
        # A claimed run holds the exact claim that it releases.
        controller.claim_identity = claim_state.ClaimIdentity("claim", "episode", "attempt")
        output = io.StringIO()

        def release(_controller: offload.ProcessController, _target: str) -> None:
            self.assertEqual(exchange.call_count, 0)

        def exchange_records(*_args: object) -> int:
            released.assert_called_once_with(controller, target)
            if fails:
                raise OSError("fixture exchange failure")
            return 0

        with (
            mock.patch.dict(os.environ, copies_environment(root)),
            mock.patch.object(eligibility, "configured_ssh_target", return_value=configured),
            mock.patch.object(offload, "expected_linux_parity", return_value={}),
            mock.patch.object(offload, "copy_tracked_scripts", return_value=True),
            mock.patch.object(offload, "copy_tree_with_key", return_value=offload.CopiedTree(offload.KnownTreeKey("tree"))),
            mock.patch.object(offload, "probe_mac", return_value=offload.ProbeSnapshot(100, "linux-build")),
            mock.patch.object(offload, "remote_test_command", return_value="fixture step"),
            mock.patch.object(controller, "stream_mac_run", return_value=offload.ReportedMacStatus(status, offload.CleanupKept())),
            mock.patch.object(offload, "release_claim", side_effect=release) as released,
            mock.patch.object(time, "monotonic", return_value=5),
            mock.patch.object(buildlog_store, "root", return_value=root),
            mock.patch.object(buildlog_store, "host_name", return_value="natedev"),
            mock.patch.object(buildlog_sync, "configured_peers", return_value=(buildlog_sync.SyncPeer("linux", "linux-build"),)),
            mock.patch.object(buildlog_record, "target_hosts", return_value=("linux-build",)),
            mock.patch.object(buildlog_record, "sync_config", return_value={
                "sync.connect_timeout_s": "3", "sync.reach_timeout_s": "7", "sync.rsync_timeout_s": "11"}),
            mock.patch.object(buildlog_health, "write_snapshot"),
            mock.patch.object(buildlog_sync, "sync_peer", side_effect=exchange_records) as exchange,
            mock.patch.object(subprocess, "run", side_effect=AssertionError("No external command expected")),
            mock.patch.object(subprocess, "Popen", side_effect=AssertionError("No child process expected")),
            redirect_stdout(output),
        ):
            result = offload.claimed_run(controller, config, request, eligibility.KnownRepository("hana"), 1)
        expected = offload.PassedOnMac(4) if status == 0 else offload.FailedOnMac(status, 4)
        self.assertEqual(result, expected)
        released.assert_called_once_with(controller, target)
        if target == "linux":
            self.assertEqual(exchange.call_args.args[0], buildlog_sync.SyncPeer("linux", "linux-build"))
            self.assertEqual(exchange.call_args.args[-2:], (7, 11))
            self.assertEqual(output.getvalue().count("exchange failed"), int(fails))


class LocalVersionPreflightTests(unittest.TestCase):
    def test_recorded_versions_decline_before_claim_copy_or_ssh(self) -> None:
        self.check_record('{"versions":{"rustc":"rustc 1.99.1"}}', declined=True)

    def test_matching_versions_continue_to_claim(self) -> None:
        self.check_record('{"versions":{"rustc":" rustc 1.99.0 \\n"}}')

    def test_later_match_removes_the_decline_record(self) -> None:
        self.check_record('{"versions":{"rustc":"rustc 1.99.1"}}', declined=True, later_match=True)

    def test_missing_and_unreadable_records_continue_to_claim(self) -> None:
        for content in (None, 'invalid json', '{"versions":[]}'):
            with self.subTest(content=content):
                self.check_record(content)

    def test_only_recorded_keys_are_compared(self) -> None:
        self.check_record('{"versions":{"nextest":"nextest fixture","mend":"mend fixture"}}')

    def check_record(self, content: str | None, *, declined: bool = False, later_match: bool = False) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            if content is not None:
                _ = (root / "box-tools.json").write_text(content)
            target = target_config.SshTarget("linux", "linux", root / "linux.conf")
            config = eligibility.OffloadConfig(True, eligibility.SshConnectionSettings("dummy", 1, 1, UNREACHABLE_WAIT_FIXTURE_SECONDS),
                                          1, 1, 1, "1", {"version_pin_file": "box-pins.nix"})
            request = offload.OffloadRequest(root, "hana", "call", offload.NO_RESULT_FILE, (), owner="verify", target="linux", holder_session="fixture-session")
            controller = offload.ProcessController()
            versions = {"rustc": "rustc 1.99.0", "nextest": "nextest fixture", "mend": "mend fixture", "git": "commit"}

            def capture(arguments: Sequence[str], deadline: offload.CommandDeadline) -> offload.CommandOutput:
                self.assertIsInstance(deadline, offload.CommandTimeLimit)
                name = arguments[1] if arguments[0] == "cargo" else arguments[0]
                return offload.CommandOutput(0, versions[name], "")

            with (
                mock.patch.object(eligibility, "mac_step_eligibility", return_value=HANA),
                mock.patch.object(eligibility, "configured_ssh_target", return_value=target),
                mock.patch.object(offload, "trusted_linux_source", return_value=True),
                mock.patch.object(target_config, "ssh_target_state_directory", return_value=root),
                mock.patch.object(controller, "capture", side_effect=capture),
                mock.patch.object(offload, "claim_for_run", return_value=offload.MacClaimNotConfirmed("blocked", "", "")) as claim,
                mock.patch.object(offload, "copy_tracked_scripts") as scripts_copy,
                mock.patch.object(offload, "copy_tree_with_key") as tree_copy,
                mock.patch.object(subprocess, "Popen", side_effect=AssertionError("No process expected")),
                mock.patch.object(subprocess, "run", side_effect=AssertionError("No command expected")),
                mock.patch.object(time, "monotonic", return_value=5),
                mock.patch.object(time, "time", return_value=100),
            ):
                answer = offload.run_offload(controller, config, request, 1)
                if declined:
                    self.assertEqual(answer, offload.DeclinedMacRun("state", 4,
                        "linux version mismatch: rustc box=rustc 1.99.1 natedev=rustc 1.99.0; move the box pins in box-pins.nix"))
                    claim.assert_not_called()
                    self.assertEqual(eligibility.linux_version_decline_path("linux", HANA.name), root / "version-decline-hana")
                    self.assertTrue(offload.linux_version_declined_recently("linux", HANA.name, 60, 159))
                    self.assertFalse(offload.linux_version_declined_recently("linux", HANA.name, 60, 160))
                    if later_match:
                        _ = (root / "box-tools.json").write_text('{"versions":{"rustc":"rustc 1.99.0"}}')
                        _ = offload.run_offload(controller, config, request, 1)
                        claim.assert_called_once_with(controller, config, request, HANA,
                                                      mac_repository=claim_state.NO_SAVED_MAC_REPOSITORY,
                                                      retire_wait=False)
                        self.assertFalse(eligibility.linux_version_decline_path("linux", HANA.name).exists())
                else:
                    claim.assert_called_once_with(controller, config, request, HANA,
                                                  mac_repository=claim_state.NO_SAVED_MAC_REPOSITORY,
                                                  retire_wait=False)
                    self.assertFalse(offload.linux_version_declined_recently("linux", HANA.name, 60, 100))
            scripts_copy.assert_not_called()
            tree_copy.assert_not_called()


class OffloadSettingsTests(unittest.TestCase):
    root: Path = Path()
    settings: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.settings = self.root / "mac_test.conf"
        _ = write_config_fixture(self.settings,
            "offload=on\nhost=mac\nmirror_root=~/.local/state/mac-test/test-mirror\n"
            + "linux_only.cargo-liner=\nmac_budget_gib=24\nconnect_timeout_s=6\n"
            + "probe_timeout_s=15\nleftover_timeout_s=10\nunreachable_backoff_s=300\nfree_floor_gib=100\n")
        _ = write_config_fixture(self.root / "build_targets.conf", "platform.mac=macos\nunreachable_wait_seconds=45\n")
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {
            **config_environment(self.root),
            **copies_environment(self.root),
            "MAC_TEST_CONFIG": str(self.settings), "CONF_DIR": str(Path(self.settings).parent),
            "MAC_TEST_STATE_DIR": str(self.root / "state"),
        })))
        _ = self.enterContext(mock.patch.object(Path, "home", return_value=self.root))
        _ = self.enterContext(mock.patch("subprocess.run", side_effect=AssertionError("No external command expected")))
        _ = self.enterContext(mock.patch("subprocess.Popen", side_effect=AssertionError("No external command expected")))

    def test_fresh_claim_refusal_after_cleanup_does_not_report_failed_adoption(self) -> None:
        controller = offload.ProcessController()
        config = eligibility.read_config(self.settings)
        admission = offload.MacClaimFromWait(41, claim_state.ClaimIdentity("claim-a", "episode-a", "attempt-a"),
                                             NoOwningSession())
        request = offload.OffloadRequest(self.root, "hana", "call", offload.NO_RESULT_FILE, (),
                                         owner="verify", holder_session="fixture-session",
                                         placement=offload.DirectOffloadRequest(), admission=admission)
        fresh_error = "state unreadable: fixture block\n"
        refusal = offload.read_claim_answer(offload.CommandOutput(12, "", fresh_error))
        result_path = self.root / "result"
        with (ExitStack() as lease,
              mock.patch.object(offload, "claim_for_run", side_effect=(offload.TakenOverMacClaim("fixture-mac"), refusal)) as claim,
              mock.patch.object(offload, "check_taken_over_claim", return_value=offload.LeftoversEnded()),
              mock.patch.object(offload, "release_claim", return_value=offload.ClaimReleaseConfirmed()) as release,
              redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors):
            result = offload.leased_offload(controller, config, request, HANA, time.monotonic(), True, lease,
                                            admission_slot=1)
            assert isinstance(result, offload.DeclinedMacRun)
            self.assertEqual(offload.write_result(result, offload.ResultFile(result_path)), 75)

        self.assertEqual(claim.call_args_list, [
            mock.call(controller, config, request, HANA, mac_repository=claim_state.NO_SAVED_MAC_REPOSITORY,
                      retire_wait=False),
            mock.call(controller, config, replace(request, admission=offload.UnclaimedMacRun()), HANA,
                      mac_repository=claim_state.NO_SAVED_MAC_REPOSITORY),
        ])
        release.assert_called_once_with(controller, request.target)
        self.assertEqual(output.getvalue(), "mac_test: not running on the Mac (state)\n")
        self.assertEqual(errors.getvalue(), fresh_error)
        self.assertEqual(result_path.read_text().splitlines()[:2], ["mac=declined", "reason=state"])

    def test_mac_summary_write_failure_preserves_the_published_result(self) -> None:
        def place(request: offload.OffloadRequest, *_arguments: object) -> offload.OffloadRequest:
            return request

        for options, outcome, prefix, field in (
            (("--fetch", "output"), offload.PassedOnMac(1, offload.Fetched(self.root / "fetched", 12)),
             "mac_test: fetched", "fetch=fetched"),
            (("--verb", "mend-fix"), offload.PassedOnMac(1, fix=fix_return.FixNone()),
             "mac_test: the Mac fix pass passed", "fix=none"),
        ):
            with self.subTest(options=options):
                path = self.root / (field.partition("=")[0] + ".result")
                argv = ["offload", "run", "--repo-root", str(self.root), "--call-id", "call",
                        "--result", str(path), *options]
                file_at_summary: list[str] = []

                def write_output(message: str) -> int:
                    if message.startswith(prefix):
                        file_at_summary.append(path.read_text())
                        raise BrokenPipeError("The fixture cannot print the Mac summary")
                    return len(message)

                with (mock.patch.object(sys, "argv", argv),
                      redirect_stdout(io.StringIO()),
                      mock.patch.object(sys.stdout, "write", side_effect=write_output),
                      mock.patch.object(override_guard, "refuse_overrides"),
                      mock.patch.object(copies, "source_owner", return_value="fixture"),
                      mock.patch.object(offload, "holder_session", return_value="fixture"),
                      mock.patch.object(offload, "restore_ignored_interrupt_default"),
                      mock.patch.object(offload, "submit_direct_placement", side_effect=place),
                      mock.patch.object(offload, "run_offload", return_value=outcome),
                      mock.patch.object(offload, "report_placement_result") as placement_report,
                      mock.patch.object(offload, "persist_attempt") as attempt_record):
                    status = offload.main()
                self.assertEqual(status, 0)
                result = path.read_text()
                self.assertEqual(file_at_summary, [result])
                self.assertEqual(result.splitlines()[:2], ["mac=passed", "reason="])
                self.assertIn(field, result.splitlines())
                placement_report.assert_called_once()
                attempt_record.assert_called_once()

    def test_mac_command_sends_repository_identity_only_when_nonempty(self) -> None:
        config = eligibility.read_config(self.settings)
        request = offload.OffloadRequest(self.root, "hana", "fixture-call", offload.NO_RESULT_FILE, (), owner="verify", holder_session="fixture-session")
        for key in ("https://example.com/team/hana", "git-root:/source with spaces/hana", ""):
            with self.subTest(key=key):
                words = shlex.split(offload.remote_test_command(
                    config, request, eligibility.KnownRepository("hana"), history_key=key,
                ))
                assignments = [word for word in words if word.startswith("BUILDLOG_REPO_IDENTITY=")]
                self.assertEqual(assignments, [f"BUILDLOG_REPO_IDENTITY={key}"] if key else [])

    def test_ssh_target_state_path_needs_no_registry(self) -> None:
        with mock.patch.object(target_config, "read_targets", side_effect=AssertionError("No registry read expected")):
            self.assertEqual(eligibility.ssh_target_state_directory("linux"), self.root / ".local/state/build-targets/linux")
            self.assertEqual(eligibility.ssh_target_state_directory("mac"), self.root / "state")
            with mock.patch.dict(os.environ, {"BUILD_TARGETS_STATE_DIR": str(self.root / "build-targets")}):
                self.assertEqual(eligibility.ssh_target_state_directory("linux"), self.root / "build-targets/linux")
                self.assertEqual(eligibility.ssh_target_state_directory("mac"), self.root / "state")
            with mock.patch.dict(os.environ, {"BUILD_TARGETS_STATE_DIR": "~/private-targets", "HOME": str(self.root)}):
                self.assertEqual(eligibility.ssh_target_state_directory("linux"), self.root / "private-targets/linux")

    def test_ssh_target_state_path_refuses_invalid_names(self) -> None:
        for target in (".", "..", "../linux", "/tmp/linux", "linux/other"):
            with self.subTest(target=target), self.assertRaises(target_config.ConfigError):
                _ = eligibility.ssh_target_state_directory(target)

    def test_explicit_settings_path_controls_every_offload_field(self) -> None:
        _ = write_config_fixture(self.settings, "mirror_busy_backoff_s=37\n", template="mac_test")
        for filename, budget_key in (("other-mac.conf", "mac_budget_gib"),
                                     ("linux.conf", "target_budget_gib")):
            with self.subTest(settings=filename):
                settings = self.root / filename
                _ = write_config_fixture(settings, "offload=on\nhost=other-mac\nconnect_timeout_s=2\nprobe_timeout_s=3\n"
                    + f"unreachable_backoff_s=4\nfree_floor_gib=5\n{budget_key}=6\n"
                    + "mac_budget_gib.hana=7\nlinux_only.hana=local-package\nmirror_busy_backoff_s=91\n", template="mac_test")
                config = eligibility.read_config(settings)
                self.assertTrue(config.enabled)
                self.assertEqual(config.host, "other-mac")
                self.assertEqual(config.connect_timeout_s, 2.0)
                self.assertEqual(config.probe_timeout_s, 3.0)
                self.assertEqual(config.unreachable_backoff_s, 4.0)
                self.assertEqual(config.free_floor_gib, 5.0)
                self.assertEqual(config.default_target_budget_gib, "6")
                self.assertEqual(config.values["mac_budget_gib.hana"], "7")
                self.assertEqual(config.values["linux_only.hana"], "local-package")
                self.assertEqual(config.mirror_busy_backoff_s, 37)

    def test_explicit_mac_settings_cannot_bypass_the_nextest_summary_limit(self) -> None:
        request = offload.OffloadRequest(self.root, "hana", "log-limit", offload.NO_RESULT_FILE, (),
                                        owner="verify", holder_session="fixture-session")
        ceiling = conf.read(nextest_log.DEFAULT_SETTINGS).whole(nextest_log.SUMMARY_BYTES)
        larger = ceiling + 1
        source = self.settings.read_text()
        summary = (rf"^(?:{re.escape(conf_reads.SHIFT_MARK)}[^\n]*\n)?"
                   + rf"# whole: [^\n]*\n{nextest_log.SUMMARY_BYTES}=[^\n]*$")
        for limit in ("", f"# whole: 1..{larger}\n"):
            with self.subTest(limit=limit):
                settings = self.root / "summary-limit.conf"
                contents, replacements = re.subn(summary, f"{limit}{nextest_log.SUMMARY_BYTES}={larger}",
                                                 source, flags=re.MULTILINE)
                self.assertEqual(replacements, 1)
                _ = settings.write_text(contents)
                config = eligibility.read_config(settings)
                error = f"Summary limit must be at most {ceiling}" if limit else nextest_log.SUMMARY_BYTES
                with (mock.patch.object(subprocess, "run", RUN_SUBPROCESS),
                      mock.patch.object(subprocess, "Popen", START_SUBPROCESS),
                      self.assertRaisesRegex(conf.Unusable, error)):
                    _ = offload.remote_test_command(config, request, HANA)

    def test_explicit_mac_settings_use_host_only_nextest_limits(self) -> None:
        host = conf.local_host_key_name()
        source = self.settings.read_text()
        for key, value in ((nextest_log.TAIL_BYTES, 36), (nextest_log.SUMMARY_BYTES, 52)):
            source = re.sub(rf"^{key}=\d+$", f"{key}.{host}={value}", source, flags=re.MULTILINE)
        _ = self.settings.write_text(source)
        config = eligibility.read_config(self.settings)
        request = offload.OffloadRequest(self.root, "hana", "log-limit", offload.NO_RESULT_FILE, (),
                                        owner="verify", holder_session="fixture-session")
        with mock.patch.object(subprocess, "run", RUN_SUBPROCESS), mock.patch.object(subprocess, "Popen", START_SUBPROCESS):
            self.assertIn(" run 36 52 ", offload.remote_test_command(config, request, HANA))

    def test_sender_normalizes_multiline_tool_versions_before_parity(self) -> None:
        versions = {"rustc": " rustc fixture \n", "nextest":
            " cargo-nextest 0.9.143  \n release: 0.9.143\n commit: abc\n date: today\n host: linux \n",
            "mend": " mend fixture \n", "git": "fixture-commit\n"}
        controller = offload.ProcessController()

        def capture(arguments: Sequence[str],
                    deadline: offload.CommandDeadline = offload.NO_COMMAND_TIME_LIMIT) -> offload.CapturedCommandResult:
            del deadline
            name = "nextest" if arguments[:2] == ["cargo", "nextest"] else "mend" if arguments[0] == "cargo" else arguments[0]
            return offload.CommandOutput(0, versions[name], "")

        with mock.patch.object(controller, "capture", side_effect=capture):
            expected = offload.expected_linux_parity(controller, eligibility.read_config(self.settings))

        self.assertNotIsInstance(expected, offload.UnavailableToolParity)
        if isinstance(expected, offload.UnavailableToolParity):
            self.fail("The sender must read all fixture versions")
        self.assertEqual(expected["nextest"],
            "cargo-nextest 0.9.143 release: 0.9.143 commit: abc date: today host: linux")
        self.assertEqual((expected["rustc"], expected["mend"], expected["scripts_commit"]),
                         ("rustc fixture", "mend fixture", "fixture-commit"))

    def test_no_argument_config_reader_uses_the_file_values(self) -> None:
        config = eligibility.read_config()
        self.assertEqual(config, eligibility.read_config(self.settings))
        self.assertTrue(config.enabled)
        self.assertEqual(config.free_floor_gib, 100)
        self.assertEqual(config.host, "mac")

    def test_no_argument_config_reader_uses_conf_dir(self) -> None:
        _ = write_config_fixture(self.settings, "host=environment-mac\nprobe_timeout_s=7\n", template="mac_test")
        config = eligibility.read_config()
        self.assertEqual(config.host, "environment-mac")
        self.assertEqual(config.probe_timeout_s, 7)
        self.assertEqual(config.connect_timeout_s, 6)

    @mock.patch("subprocess.run", new=RUN_SUBPROCESS)
    @mock.patch("subprocess.Popen", new=START_SUBPROCESS)
    def test_path_reader_recovers_invalid_numbers_from_its_history(self) -> None:
        commit_config_fixture(self.settings)
        _ = write_config_fault(self.settings,
            "connect_timeout_s=bad\nprobe_timeout_s=0\nunreachable_backoff_s=-1\nfree_floor_gib=nan\n")
        warning = io.StringIO()
        with redirect_stderr(warning):
            config = eligibility.read_config(self.settings)
        self.assertEqual(
            (config.connect_timeout_s, config.probe_timeout_s, config.unreachable_backoff_s, config.free_floor_gib),
            (6.0, 15.0, 300.0, 100.0),
        )
        self.assertIn("connect_timeout_s", warning.getvalue())
        self.assertIn("free_floor_gib", warning.getvalue())
        # An explicit new file with no good revision cannot recover.
        unknown = self.root / "uncommitted.conf"
        _ = unknown.write_text("offload=on\n")
        with redirect_stderr(io.StringIO()), self.assertRaises(conf.Unusable):
            _ = eligibility.read_config(unknown)

    def test_ssh_keepalive_count_follows_the_unreachable_wait(self) -> None:
        for wait, count in ((45, 3), (50, 4), (600, 40)):
            with self.subTest(wait=wait):
                _ = write_config_fixture(self.root / "build_targets.conf",
                                         f"platform.mac=macos\nunreachable_wait_seconds={wait}\n")
                options = offload.ssh_options(eligibility.read_config(self.settings).connection)
                self.assertEqual(options[-4:], ["-o", "ServerAliveInterval=15", "-o", f"ServerAliveCountMax={count}"])

    def test_reachability_probe_runs_only_a_read_only_remote_command(self) -> None:
        _ = write_config_fixture(self.settings, "host=other-mac\nconnect_timeout_s=2\nprobe_timeout_s=3\n", template="mac_test")
        config = eligibility.read_config(self.settings)
        controller = offload.ProcessController()
        calls: list[tuple[list[str], offload.CommandDeadline]] = []

        def capture(
            arguments: list[str],
            deadline: offload.CommandDeadline = offload.NO_COMMAND_TIME_LIMIT,
        ) -> offload.CapturedCommandResult:
            calls.append((arguments, deadline))
            return offload.CommandOutput(0, "", "")

        with mock.patch.object(controller, "capture", side_effect=capture):
            self.assertEqual(offload.probe_reachability(controller, config), offload.ReachableHost())
        self.assertEqual(len(calls), 1)
        arguments, deadline = calls[0]
        self.assertEqual(arguments[:-1], [
            "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=2",
            "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3", "other-mac",
        ])
        self.assertEqual(arguments[-1], "true")
        self.assertNotRegex(arguments[-1], r"\b(?:mkdir|touch|tee|rm|mv|cp)\b|[>]")
        self.assertEqual(deadline, offload.CommandTimeLimit(3.0))
        self.assertFalse((self.root / "state").exists())

    def test_failed_reachability_command_reports_unreachable(self) -> None:
        config = eligibility.read_config(self.settings)
        failures: tuple[offload.CapturedCommandResult, ...] = (
            offload.CommandOutput(255, "", "connection refused"),
            offload.CommandOutput(1, "", "remote command failed"),
            offload.COMMAND_TIMED_OUT,
            offload.COMMAND_COULD_NOT_START,
        )
        for failure in failures:
            with self.subTest(result=type(failure).__name__):
                controller = offload.ProcessController()
                with mock.patch.object(controller, "capture", return_value=failure):
                    result = offload.probe_reachability(controller, config)
                self.assertIsInstance(result, offload.UnreachableHost)
                if isinstance(result, offload.UnreachableHost):
                    self.assertTrue(result.reason)

    def test_resource_probe_needs_no_process_count_and_ignores_legacy_counts(self) -> None:
        wire = "free_gib=120\nhost=Mac\nrc=0\n"
        expected = offload.ProbeSnapshot(120.0, "Mac")
        self.assertEqual(offload.parse_probe(wire), expected)
        self.assertEqual(offload.parse_probe("procs=12\n" + wire), expected)
        self.assertEqual(offload.parse_probe("procs=unreadable\n" + wire), expected)
        self.assertNotIn("processes", {field.name for field in fields(expected)})
        script = offload.probe_script(eligibility.KnownRepository("hana"))
        self.assertNotIn("pgrep", script)
        self.assertNotIn("procs", script)

    def test_leftover_check_requires_count_and_printed_success_on_the_named_host(self) -> None:
        config = eligibility.read_config(self.settings)
        for answer, expected in (("leftovers=0\nrc=0\n", offload.LeftoversEnded()),
                                 ("leftovers=4\nrc=0\n", offload.LeftoversRemain(4))):
            with self.subTest(answer=answer):
                response = subprocess.CompletedProcess(["ssh"], 0, answer, "")
                with mock.patch.object(subprocess, "run", return_value=response) as run:
                    self.assertEqual(offload.check_leftovers(config.connection, "kept-host", HANA), expected)
                arguments = cast(list[str], run.call_args.args[0])
                self.assertEqual(arguments[:-1], ["ssh", *offload.ssh_options(config.connection), "kept-host"])
                self.assertEqual(run.call_args.kwargs["timeout"], 10)
                self.assertIn('pgrep -u "$(id -u)" -f "$mirror_command_pattern"', arguments[-1])
                self.assertIn(f"mirror_command_pattern={shlex.quote(HANA_COMMAND_PATTERN)}\n", arguments[-1])
        for answer in ("leftovers=0\n", "rc=0\n", "leftovers=-1\nrc=0\n", "leftovers=bad\nrc=0\n"):
            with self.subTest(answer=answer):
                response = subprocess.CompletedProcess(["ssh"], 0, answer, "")
                with mock.patch.object(subprocess, "run", return_value=response):
                    result = offload.check_leftovers(config.connection, "kept-host", HANA)
                self.assertIsInstance(result, offload.LeftoverCheckFailed)
                if isinstance(result, offload.LeftoverCheckFailed):
                    self.assertIn("answer could not be read", result.reason)

    def test_leftover_check_reports_timeout_start_failure_and_ssh_error(self) -> None:
        config = eligibility.read_config(self.settings)
        for error, expected in ((subprocess.TimeoutExpired("ssh", 10), "timed out after 10"),
                                (FileNotFoundError("ssh missing"), "could not start")):
            with self.subTest(error=type(error).__name__):
                with mock.patch.object(subprocess, "run", side_effect=error):
                    result = offload.check_leftovers(config.connection, "kept-host", HANA)
                self.assertIsInstance(result, offload.LeftoverCheckFailed)
                if isinstance(result, offload.LeftoverCheckFailed):
                    self.assertIn(expected, result.reason)
                    self.assertIn("10", result.reason)
        response = subprocess.CompletedProcess(["ssh"], 255, "", "connection\n refused\tby host")
        with mock.patch.object(subprocess, "run", return_value=response):
            result = offload.check_leftovers(config.connection, "kept-host", HANA)
        self.assertIsInstance(result, offload.LeftoverCheckFailed)
        if isinstance(result, offload.LeftoverCheckFailed):
            self.assertEqual(result.reason, "connection refused by host")

    @mock.patch("subprocess.run", new=RUN_SUBPROCESS)
    @mock.patch("subprocess.Popen", new=START_SUBPROCESS)
    def test_leftover_check_uses_configured_timeout_and_reports_it(self) -> None:
        _ = write_config_fixture(self.settings, "leftover_timeout_s=0.125\n", template="mac_test")
        config = eligibility.read_config(self.settings)
        with mock.patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired("ssh", 0.125)) as run:
            result = offload.check_leftovers(config.connection, "kept-host", HANA)
        self.assertEqual(run.call_args.kwargs["timeout"], 0.125)
        self.assertEqual(result, offload.LeftoverCheckFailed("SSH leftover check timed out after 0.125 s"))
        self.settings.unlink()
        _ = write_config_fixture(self.settings, "leftover_timeout_s=10\n")
        commit_config_fixture(self.settings)
        for value in ("bad", "0", "-1", "nan", "inf"):
            with self.subTest(value=value):
                _ = write_config_fixture(self.settings, f"leftover_timeout_s={value}\n", template="mac_test")
                warning = io.StringIO()
                with redirect_stderr(warning):
                    self.assertEqual(eligibility.read_config(self.settings).leftover_timeout_s, 10)
                self.assertIn("leftover_timeout_s", warning.getvalue())

    def test_probe_mirror_lease_stays_under_the_temporary_root(self) -> None:
        request = offload.OffloadRequest(self.root, "hana", "probe-start", offload.NO_RESULT_FILE, (),
                                         owner="verify", holder_session="fixture-session")
        mirror = offload.request_mirror(eligibility.read_config(self.settings), request, eligibility.KnownRepository("hana"))
        self.assertTrue(copies.remote_lease_path(mirror).is_relative_to(self.root))

    def test_probe_that_cannot_start_reports_its_limit_through_run(self) -> None:
        from scripts.build_queue import admission_protocol as wire, placement
        from scripts.build_queue.admission_test_support import AdmissionFixture
        from scripts.build_queue import admission_records as records, admission_service
        from scripts.build_hold.owning_session import NoOwningSession

        _ = write_config_fixture(self.settings, "offload=on\nlinux_only.hana=\nprobe_timeout_s=2.5\n", template="mac_test")
        with mock.patch.object(admission_service, "loaded_git_revision", return_value="fixture"):
            fixture = self.enterContext(AdmissionFixture(root=self.root))
        start = records.process_start_time(os.getpid())
        assert start is not None
        local = fixture.request(owner=wire.Owner(os.getpid(), start))
        target = placement.PlacementDestination("mac", "mac", "mac", claim_state.RepositoryScope("hana"))
        constraints = placement.PlacementConstraints(execution_identity.SourceKeyUnavailable("the fixture has no tree key"),
            ("cargo", "nextest", "run"), str(self.root), "hana", NoOwningSession(),
            (placement.PlacementCandidate(target),), placement.PinnedHost("mac"))
        request = wire.PlacementRequest(local.episode,
            placement.SubmittingOwner(fixture.server.host, local.owner.pid, local.owner.start_time),
            local.since, local.profile, constraints, identity=local.identity, code_version=local.code_version)
        fixture.server.publish_host_evidence(placement.FreshHostEvidence(
            fixture.server.host_evidence_key(request, target), fixture.clock.now(), "", placement.AllowedHost()))
        assigned, = fixture.server.handle_request(request)
        self.assertIsInstance(assigned, wire.Assignment)
        assignment_file = self.root / "probe.assignment"
        _ = assignment_file.write_text(wire.encode_reply(assigned))
        _ = self.enterContext(fixture.serving())
        destination = self.root / "result"
        responses = [offload.COMMAND_COULD_NOT_START, offload.CommandOutput(0, "", "")]
        mirror = claim_state.ResolvedMacMirror("fixture-hana", "hana", "hana.json", "hana", str(self.root))
        history_key = "https://fixture.invalid/hana"
        saved = mirror.claim_repository(history_key=claim_state.KnownMacHistoryKey(history_key))
        checkout = self.root / "mac"
        checkout.mkdir()
        holder = """
import json
import sys

print("build_folder=" + json.dumps(sys.argv[1]), flush=True)
if sys.stdin.read(1) != "+":
    sys.exit(1)
print("build_folder_confirmed", flush=True)
sys.exit(0 if sys.stdin.read(1) == "\\n" else 1)
"""

        def start_holder(arguments: Sequence[str], *, stdin: int, stdout: int, text: bool) -> subprocess.Popen[bytes]:
            self.assertEqual(arguments[0], "ssh")
            self.assertIn("mac_queue_run.py hold-offload-folder mac ", arguments[-1])
            self.assertFalse(text)
            return START_SUBPROCESS([sys.executable, "-I", "-S", "-c", holder, str(checkout)],
                                    cwd=self.root, stdin=stdin, stdout=stdout)

        def complete_claim(controller: offload.ProcessController, arguments: Sequence[str],
                           attempt: offload.LocalClaimAttempt) -> None:
            claim = claim_state.ClaimIdentity(
                arguments[arguments.index("--claim-id") + 1], arguments[arguments.index("--episode") + 1],
                arguments[arguments.index("--attempt") + 1])
            controller.claim_identity = claim
            controller.mac_repository = saved
            attempt.answer = offload.FreshMacClaim()

        with mock.patch.object(sys, "argv", ["offload.py", "run", "--repo-root", str(self.root),
                                             "--package", "hana", "--call-id", "probe-start", "--result", str(destination),
                                             "--assignment-file", str(assignment_file)]), \
             mock.patch.object(claim_state, "mac_repository_identity", return_value=mirror), \
             mock.patch.object(buildlog_repository, "repository_key", return_value=history_key), \
             mock.patch.object(offload.ProcessController, "complete_claim", autospec=True, side_effect=complete_claim), \
             mock.patch.object(subprocess, "Popen", side_effect=start_holder) as start_process, \
             mock.patch.object(offload.ProcessController, "capture", side_effect=responses) as capture, \
             redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(offload.main(), 75)
        self.assertEqual(output.getvalue(), "mac_test: not running on the Mac (unreachable)\n")
        self.assertRegex(errors.getvalue(), r"^mac_test: could not reach the Mac: .*could not start.*2\.5.*\n$")
        start_process.assert_called_once()
        self.assertEqual(capture.call_count, 2)
        arguments, deadline = cast(tuple[list[str], offload.CommandDeadline], capture.call_args_list[0].args)
        self.assertIn(f"test -d {shlex.quote(str(checkout))}", arguments[-1])
        self.assertEqual(deadline, offload.CommandTimeLimit(2.5))
        self.assertEqual(destination.read_text().splitlines()[:2], ["mac=declined", "reason=unreachable"])
        self.assertTrue((self.root / "state" / "unreachable_until").exists())

    def test_fixture_settings_send_every_cargo_liner_member_to_the_mac(self) -> None:
        config = eligibility.read_config(self.settings)
        self.assertEqual(config.values["linux_only.cargo-liner"], "")
        self.assertEqual(config.default_target_budget_gib, "24")
        self.assertNotIn("mac_budget_gib.cargo-liner", config.values)
        offload.write_backoff(time.time() + 60)
        expected = {"cargo-port": "backoff", "cargo-tile": "backoff", "tui_pane": "backoff", "cargo-berth": "backoff"}
        for package, reason in expected.items():
            with self.subTest(package=package):
                request = offload.OffloadRequest(self.root, package, "cargo-liner", offload.NO_RESULT_FILE, (), owner="verify", holder_session="fixture-session")
                mirror = claim_state.ResolvedMacMirror(
                    "fixture-cargo-liner", "cargo-liner", "cargo-liner.json", "cargo-liner", str(self.root))
                with mock.patch.object(claim_state, "mac_repository_identity", return_value=mirror), \
                        mock.patch.object(buildlog_repository, "repository_key",
                                          return_value="https://fixture.invalid/cargo-liner"):
                    result = offload.run_offload(offload.ProcessController(), config, request, time.monotonic())
                self.assertEqual(result.reason if isinstance(result, offload.DeclinedMacRun) else result, reason)


@final
class MacPlacementWaitTests(unittest.TestCase):
    def test_a_mac_copy_waits_for_the_mac_while_its_local_parent_holds_memory(self) -> None:
        with AdmissionFixture() as fixture:
            owner = fixture.fixture_owner(4094003, "35916506")
            run = wire.RunIdentity(owner.pid, owner.start_time)
            parent = fixture.request(need=14912008192, owner=owner,
                                     since=fixture.clock.now() - 3600, run=run)
            grant, = fixture.server.handle_request(parent)
            assert isinstance(grant, wire.Admitted)

            child = fixture.fixture_owner(3682121, "36172697")
            mac = placement.PlacementDestination("mac", "mac", "fixture-mac", claim_state.RepositoryScope("hana"))
            constraints = placement.PlacementConstraints(execution_identity.SourceKeyKnown("fixture-tree"),
                ("cargo", "nextest", "run"), str(fixture.root), "hana", NoOwningSession(),
                (placement.PlacementCandidate(mac),), placement.PinnedHost("mac"))
            request = wire.PlacementRequest("mac-copy", placement.SubmittingOwner(
                fixture.server.host, child.pid, child.start_time), fixture.clock.now() - 3100,
                parent.profile, constraints, identity=parent.identity, run=run,
                purpose=placement.SpeculativeCopy(parent.episode, parent.since))
            key = fixture.server.host_evidence_key(request, mac)
            busy = placement.FreshHostEvidence(key, fixture.clock.now(), "", placement.TemporaryExclusion("busy"))
            fixture.server.publish_host_evidence(busy)

            reply, = fixture.server.handle_request(request)
            assert isinstance(reply, wire.PlacementWait)
            self.assertEqual(reply.wait, wire.PlacementResourceWait("mac", "busy"))
            snapshot = queue_snapshot.read_queue_snapshot(fixture.state_dir)
            assert isinstance(snapshot, queue_snapshot.QueueSnapshot)
            self.assertEqual([row["pid"] for _, row in snapshot.inputs.ordered], [])
            self.assertEqual([row["pid"] for row in snapshot.inputs.reservations], [owner.pid])
            view, = fixture.connect(wire.ViewRequest()).replies()
            assert isinstance(view, wire.ViewReply)
            self.assertEqual(view.reading.view.waiting, ())

            fixture.allow_next_start()
            younger = fixture.request(owner=fixture.fixture_owner(1855215, "36200000"))
            self.assertIsInstance(fixture.server.handle_request(younger)[0], wire.Admitted)
            self.assertNotIn(request.episode, fixture.server.reservations)

            fixture.server.publish_host_evidence(replace(busy, observed_at=fixture.clock.now(),
                                                         eligibility=placement.AllowedHost()))
            assigned, = fixture.server.handle_request(request)
            assert isinstance(assigned, wire.Assignment)
            self.assertEqual(assigned.assignment.destination, mac)
            self.assertEqual(fixture.server.reservations[parent.episode].reservation_id, grant.reservation)
            self.assertNotIn(request.episode, fixture.server.reservations)
            snapshot = queue_snapshot.read_queue_snapshot(fixture.state_dir)
            assert isinstance(snapshot, queue_snapshot.QueueSnapshot)
            self.assertEqual(snapshot.inputs.ordered, [])


@final
class BusyHolderTests(unittest.TestCase):
    def test_claim_reply_preserves_other_output_and_decodes_only_known_checks(self) -> None:
        output = "claim child output\n"
        unknown = "cannot adopt the Mac claim: unknown check\n"
        cases = (
            (12, "state", "", offload.NoWaitClaimCheck),
            (11, "busy", "cannot adopt the Mac claim: scope\n", claim_state.WaitClaimCheck.SCOPE),
        )
        for status, reason, diagnostic, check in cases:
            with self.subTest(status=status):
                result = offload.read_claim_answer(offload.CommandOutput(status, output, diagnostic + unknown))
                self.assertIsInstance(result, offload.MacClaimChildRefusal)
                assert isinstance(result, offload.MacClaimChildRefusal)
                self.assertEqual((result.status, result.reason), (status, reason))
                self.assertEqual((result.stdout, result.stderr), (output, unknown))
                if isinstance(check, claim_state.WaitClaimCheck):
                    self.assertIs(result.check, check)
                else:
                    self.assertIsInstance(result.check, check)

    def test_busy_claim_line_keeps_the_job_and_kept_claim_description(self) -> None:
        for what in ("cargo nextest -p hana", "leftovers of cargo test on prior-mac", "job since last week"):
            with self.subTest(what=what):
                self.assertEqual(offload.claim_holder(f"busy: {what} since 13:05\n"), offload.KnownMacJob(what))

    def test_busy_claim_without_the_expected_line_has_unreadable_holder(self) -> None:
        for line in ("", "busy", "busy: task", "busy: task since yesterday", "busy: task since 13:05\nextra"):
            with self.subTest(line=line):
                self.assertIsInstance(offload.claim_holder(line), offload.UnreadableMacHolder)

    def test_only_busy_results_write_the_holder_line(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / "result"
            for holder, expected in ((offload.KnownMacJob("render checks"), "render checks"),
                                     (offload.LostRunLeftovers(), "leftovers"),
                                     (offload.UnreadableMacHolder(), "unreadable")):
                with self.subTest(holder=expected), redirect_stdout(io.StringIO()):
                    self.assertEqual(offload.write_result(offload.BusyMacDecline(holder, 0.25), offload.ResultFile(path)), 75)
                    self.assertIn(f"holder={expected}\n", path.read_text())
            with redirect_stdout(io.StringIO()):
                _ = offload.write_result(offload.PassedOnMac(0.5), offload.ResultFile(path))
            self.assertNotIn("holder=", path.read_text())


@final
class VerifyMirrorWaitTests(verify_mac_offload_test_support.VerifyMacOffloadFixture):
    def test_verify_refuses_the_mirror_descriptor_environment_variable(self) -> None:
        name = "COPIES_MIRROR_LEASE_FDS"
        result = self.verify("test", "sample", "--mac", "fixture", "--env", name, extra={name: ""})
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(result.stderr, f"verify.sh: --env {name}: the runner sets it, "
                         + "or it changes what Cargo builds; it is not forwarded.\n")
        self.assertEqual(self.runner_events(), [])
        self.assertEqual(self.local_runs(), [])

    def test_unreadable_history_ends_verify_without_a_retry(self) -> None:
        git = self.bin_directory / "git"
        source = git.read_text().replace("#!/bin/bash\n",
            '#!/bin/bash\nif [[ " $* " == *" rev-list "* ]]; then exit 1; fi\n', 1)
        self.write_executable(git, source)
        result = self.verify("test", "sample", "--filter", "alpha", "--no-cache",
                             extra={"VERIFY_MAC_RUNNER": str(offload.__file__)})
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 15, output)
        self.assertEqual(output.count("history_key_unavailable"), 1, output)
        self.assertEqual(self.local_runs(), [])

    def test_verify_waits_and_names_the_busy_mirror_session(self) -> None:
        _ = self.routing_fixture()
        git = self.bin_directory / "git"
        origin = """if [[ "$*" == *' config --includes --null --show-scope --get-regexp '* ]]; then
    printf 'local\\0%s\\n%s\\0' remote.origin.url 'https://fixture.invalid/case.git'
    exit 0
fi
"""
        script = git.read_text().replace("#!/bin/bash\n", "#!/bin/bash\n" + origin, 1)
        self.write_executable(git, script)
        holder = "last recorded mirror session reader (mirror process check)"
        self.environment.update({"TEST_FREE_NOW_FREE": "1", "TEST_RUNNER_HOLDER": holder})
        process = self.start_routing()

        _ = (self.runner_state / "mac-free").write_text("free\n")
        _ = (self.runner_state / "continue-sleep").write_text("continue\n")
        result = self.finish_verify(process)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stderr.count(f"verify.sh: Mac holder: {holder}.\n"), 1)
        self.assertEqual(len(self.runner_events()), 2)
        self.assertEqual(self.local_runs(), [])


class FixedFolderRouteTests(unittest.TestCase):
    root: Path = Path()
    checkout: Path = Path()
    environment: dict[str, str] = {}

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        home = self.root / "home"
        home.mkdir()
        self.environment = {**os.environ, **copies_environment(self.root), "HOME": str(home), "CONF_DIR": str(self.root),
                            "TMPDIR": str(self.root), "GIT_CONFIG_GLOBAL": os.devnull,
                            "GIT_CONFIG_NOSYSTEM": "1", "PYTHONDONTWRITEBYTECODE": "1"}
        _ = write_config_fixture(self.root / "build_folders.conf", "roots.build=~/build\nroots.build.natedev=~/build\n")
        _ = write_config_fixture(self.root / "copies.conf")
        # The fixture runs the Mac entry point on Linux.
        _ = (self.root / "machines.conf").write_text("aliases=mac\nmac.platform=linux\nmac.hosts=fixture\n")
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, self.environment, clear=True)))
        self.checkout = home / "build/hana/mac"

    def test_lost_offload_recovers_the_recorded_folder_without_allocating(self) -> None:
        state = Path("state/claim.folder")
        with mock.patch.object(sys, "stdin", io.StringIO("+")), redirect_stdout(io.StringIO()):
            self.assertEqual(mac_queue_run.hold_offload_folder("mac", "hana", "demo", state), 1)
        record = Path.home() / state
        self.assertEqual(offload.read_fixed_folder_record(offload.SavedOffloadFolder(record)).path, self.checkout)
        self.assertEqual(build_folders.build_folder_state(self.checkout), "kept")
        with self.assertRaises(BlockingIOError):
            with build_folders.fixed_repository_build_folder("hana", machine="mac", unit="other"):
                self.fail("The retained folder accepted a second run.")
        _ = write_config_fixture(self.root / "build_folders.conf", "roots.build=~/other\nroots.build.natedev=~/other\n")
        offload.clear_fixed_folder_record(offload.SavedOffloadFolder(record))
        self.assertFalse(record.exists())
        self.assertEqual(build_folders.build_folder_state(self.checkout), "free")
        self.assertFalse((Path.home() / "other").exists())

    def test_offload_recovery_waits_for_delayed_folder_registration(self) -> None:
        identity = claim_state.ClaimIdentity("claim", "episode", "attempt")
        state = offload.fixed_folder_record(identity, "mac", "run").path
        record = Path.home() / state
        marker = build_folders.copy_keep_file(self.checkout)
        settle = offload.start_fence_command(identity, "mac", settle=True)
        observations: list[str] = []

        def observe_recovery() -> str:
            return RUN_SUBPROCESS(["sh", "-c", settle], cwd=self.root, check=True,
                                  capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS).stdout

        def delayed_write(path: Path, value: str, *, new_file_mode: int | None = None) -> None:
            if path == record:
                self.assertFalse(marker.exists())
                observations.append(observe_recovery())
            write_text_atomic(path, value, new_file_mode=new_file_mode)

        with (mock.patch.object(mac_queue_run, "write_text_atomic", side_effect=delayed_write),
              mock.patch.object(sys, "stdin", io.StringIO("+")), redirect_stdout(io.StringIO())):
            self.assertEqual(mac_queue_run.hold_offload_folder("mac", "hana", "demo", state), 1)

        key = offload.pending_start_key(identity)
        self.assertEqual(observations, [f"dispatch_start_pending={key}\n"])
        self.assertEqual(observe_recovery(), f"dispatch_start_settled={key}\n")
        offload.clear_fixed_folder_record(offload.SavedOffloadFolder(record))
        self.assertFalse(record.exists())
        self.assertFalse(marker.exists())

    def test_smoke_recovery_removes_an_interrupted_bundle_retirement(self) -> None:
        episode = "d" * 32
        record = mac_queue_run.smoke_folder_record(episode)
        upload = self.root / f"smoke.bundle.{episode}" / "smoke.bundle"
        upload.parent.mkdir()
        _ = upload.write_text("fixture bundle")
        write_text_atomic(record, json.dumps({"checkout": str(self.checkout), "launch": "", "bundle": str(upload)}))

        with mock.patch.object(shutil, "rmtree", side_effect=OSError("interrupted removal")):
            with self.assertRaisesRegex(OSError, "interrupted removal"):
                mac_queue_run.clear_smoke_bundle(upload, episode)

        self.assertFalse(upload.parent.exists())
        retired, = self.root.glob(f"*{episode}*")
        self.assertEqual((retired / upload.name).read_text(), "fixture bundle")
        self.assertEqual(mac_queue_run.recover_smoke_folder(episode), 0)
        self.assertFalse(record.exists())
        self.assertFalse(retired.exists())

    def test_delayed_offload_recovery_cannot_scan_the_next_build(self) -> None:
        state = Path("state/claim.folder")
        with mock.patch.object(sys, "stdin", io.StringIO("+")), redirect_stdout(io.StringIO()):
            self.assertEqual(mac_queue_run.hold_offload_folder("mac", "hana", "demo", state), 1)
        record = offload.SavedOffloadFolder(Path.home() / state)
        read_record = offload.read_fixed_folder_record

        with ExitStack() as next_run:
            def delayed_read(saved: offload.SavedOffloadFolder) -> offload.RecordedOffloadCheckout:
                folder = read_record(saved)
                with mock.patch.object(offload, "read_fixed_folder_record", read_record):
                    offload.clear_fixed_folder_record(saved)
                checkout = next_run.enter_context(
                    build_folders.fixed_repository_build_folder("hana", machine="mac", unit="next-run"))
                self.assertEqual(checkout, folder.path)
                return folder

            with (mock.patch.object(offload, "read_fixed_folder_record", side_effect=delayed_read),
                  mock.patch.object(offload, "mirror_selector_script", wraps=offload.mirror_selector_script) as select):
                with self.assertRaises(BlockingIOError):
                    _ = offload.recover_fixed_offload_folder(record, eligibility.KnownRepository("hana"),
                                                             "mac", 1, "", observe=False)
            select.assert_not_called()
            self.assertFalse(record.path.exists())

    def test_offload_recovery_holds_the_folder_through_the_scan(self) -> None:
        state = Path("state/claim.folder")
        with mock.patch.object(sys, "stdin", io.StringIO("+")), redirect_stdout(io.StringIO()):
            self.assertEqual(mac_queue_run.hold_offload_folder("mac", "hana", "demo", state), 1)
        record = offload.SavedOffloadFolder(Path.home() / state)
        existing = set(build_folders.held_build_folder_descriptors())

        def selector(*_args: object, **_kwargs: object) -> str:
            descriptor, = set(build_folders.held_build_folder_descriptors()) - existing
            with self.assertRaises(BlockingIOError):
                offload.clear_fixed_folder_record(record)
            self.assertTrue(record.path.exists())
            return f"test -e /dev/fd/{descriptor} || exit 1"

        checks = "printf 'leftovers=0\\nrc=0\\n'\n"
        with mock.patch.object(offload, "mirror_selector_script", side_effect=selector), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(offload.recover_fixed_offload_folder(record, eligibility.KnownRepository("hana"),
                                                                 "mac", 1, checks, observe=False), 0)
        self.assertEqual(output.getvalue(), "leftovers=0\nrc=0\n")
        self.assertFalse(record.path.exists())
        self.assertEqual(build_folders.build_folder_state(self.checkout), "free")

    def test_smoke_recovery_cannot_leave_a_late_keep_file(self) -> None:
        source = self.root / "source"
        _ = RUN_SUBPROCESS(["git", "init", "-q", str(source)], check=True)
        _ = RUN_SUBPROCESS(["git", "-C", str(source), "remote", "add", "origin",
                            "https://example.invalid/team/hana.git"], check=True)
        episode = "c" * 32
        record = mac_queue_run.smoke_folder_record(episode)
        marker = build_folders.copy_keep_file(self.checkout)
        runner = Path(mac_queue_run.__file__).resolve()
        self.assertEqual(mac_queue_run.recover_smoke_folder(episode), 0)

        with START_SUBPROCESS([sys.executable, str(runner), "smoke-folder", str(source), "demo",
                               "unused", "", "", str(self.root / "launch"), "--", "true"],
                              env={**self.environment, "MAC_PLACEMENT": episode + "/attempt"},
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT) as child:
            assert child.stdin is not None and child.stdout is not None
            deadline = time.monotonic() + TEST_HANG_GUARD_SECS
            try:
                with closing(offload.bounded_output_lines(child.stdout, lambda: deadline - time.monotonic())) as lines:
                    self.assertEqual(next(lines, b""), f"mac_smoke_register={episode}\n".encode())
                self.assertTrue(record.exists())
                self.assertFalse(marker.exists())
                with self.assertRaises(BlockingIOError):
                    _ = mac_queue_run.recover_smoke_folder(episode)
                child.stdin.close()
                self.assertEqual(child.wait(timeout=TEST_HANG_GUARD_SECS), 4)
            finally:
                child.stdin.close()
                if child.poll() is None:
                    child.kill()

        self.assertFalse(record.exists())
        self.assertFalse(marker.exists())
        self.assertEqual(build_folders.build_folder_state(self.checkout), "free")

    def test_smoke_holds_the_fixed_checkout_through_shutdown(self) -> None:
        source = self.root / "source"
        _ = RUN_SUBPROCESS(["git", "init", "-q", str(source)], check=True)
        _ = RUN_SUBPROCESS(["git", "-C", str(source), "remote", "add", "origin",
                            "https://example.invalid/team/hana.git"], check=True)
        incoming = self.root / "incoming"
        _ = RUN_SUBPROCESS(["git", "init", "-q", str(incoming)], check=True)
        _ = (incoming / "source-file").write_text("fixed source\n")
        _ = RUN_SUBPROCESS(["git", "-C", str(incoming), "add", "source-file"], check=True)
        _ = RUN_SUBPROCESS(["git", "-C", str(incoming), "-c", "user.name=Fixture",
                            "-c", "user.email=fixture@example.invalid", "commit", "-qm", "Fixture",
                            "--no-gpg-sign"], check=True)
        commit = subprocess.check_output(["git", "-C", str(incoming), "rev-parse", "HEAD"], text=True).strip()
        ref = subprocess.check_output(["git", "-C", str(incoming), "symbolic-ref", "HEAD"], text=True).strip()
        source_bundle = self.root / "source bundle"
        episode = "a" * 32
        bundle = self.root / f"remote bundle.{episode}" / "remote bundle"
        _ = RUN_SUBPROCESS(["git", "-C", str(incoming), "bundle", "create", str(source_bundle), ref], check=True)
        target = self.checkout / "target"
        target.mkdir(parents=True)
        warm = target / "warm"
        _ = warm.write_text("warm output\n")
        warm_inode = warm.stat().st_ino
        path = Path(mac_queue_run.__file__).resolve()
        work = 'git rev-parse HEAD && test -r "$CARGO_TARGET_DIR/warm" && cat source-file'
        cleanup = 'rm -- "$MAC_SMOKE_LAUNCH_TOKEN" "$COPIES_KEEP_FILE"'
        _ = write_config_fixture(self.root / "production.conf")
        environment = {**self.environment, "MAC_PLACEMENT": episode + "/attempt"}
        with START_SUBPROCESS([sys.executable, str(path), "smoke-folder", str(source), "demo", commit,
                               str(bundle), ref, str(self.root / "launch"), "--",
                               sys.executable, str(path), "smoke-work", work, cleanup],
                              env=environment, stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=False) as child:
            assert child.stdout is not None and child.stdin is not None
            deadline = time.monotonic() + TEST_HANG_GUARD_SECS
            try:
                output: list[bytes] = []
                with closing(offload.bounded_output_lines(child.stdout, lambda: deadline - time.monotonic())) as lines:
                    for line in lines:
                        output.append(line)
                        if line == f"mac_smoke_register={episode}\n".encode():
                            _ = child.stdin.write(b"+")
                            child.stdin.flush()
                        elif line == f"mac_smoke_upload={episode}\n".encode():
                            self.assertEqual(build_folders.build_folder_state(self.checkout), "held")
                            self.assertFalse(bundle.exists())
                            _ = shutil.copyfile(source_bundle, bundle)
                            _ = child.stdin.write(b"\n")
                            child.stdin.flush()
                        elif line == f"mac_smoke_ready={episode}\n".encode():
                            break
                self.assertIn(f"mac_smoke_upload={episode}\n".encode(), output, b"".join(output))
                self.assertIn(f"mac_smoke_ready={episode}\n".encode(), output, b"".join(output))
                self.assertIn(commit.encode() + b"\n", output)
                self.assertIn(b"fixed source\n", output)
                self.assertEqual(build_folders.build_folder_state(self.checkout), "held")
                self.assertEqual(build_folders.folder_lease_fields(self.checkout)["unit"], "demo")
                _ = child.stdin.write(b"\n")
                child.stdin.flush()
                self.assertEqual(child.wait(timeout=TEST_HANG_GUARD_SECS), 0)
            finally:
                child.stdin.close()
                if child.poll() is None:
                    child.kill()
        self.assertEqual(build_folders.build_folder_state(self.checkout), "free")
        self.assertEqual(warm.read_text(), "warm output\n")
        self.assertEqual(warm.stat().st_ino, warm_inode)
        self.assertFalse(bundle.exists())
        self.assertFalse(bundle.parent.exists())
        self.assertFalse(mac_queue_run.smoke_folder_record(episode).exists())


    def test_smoke_recovers_the_exact_folder_after_checkout_clearance_fails(self) -> None:
        source = self.root / "source"
        _ = RUN_SUBPROCESS(["git", "init", "-q", str(source)], check=True)
        _ = RUN_SUBPROCESS(["git", "-C", str(source), "remote", "add", "origin",
                            "https://example.invalid/team/hana.git"], check=True)
        _ = RUN_SUBPROCESS(["git", "-C", str(source), "-c", "user.name=Fixture",
                            "-c", "user.email=fixture@example.invalid", "commit", "-qm", "Fixture",
                            "--allow-empty", "--no-gpg-sign"], check=True)
        commit = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
        _ = write_config_fixture(self.root / "production.conf")
        _ = write_config_fixture(self.root / "scratch.conf")
        episode = "b" * 32
        launch = self.root / "launch"
        launch.touch()
        runner = Path(mac_queue_run.__file__).resolve()
        command = [sys.executable, str(runner), "smoke-copy", sys.executable, str(runner), "smoke-folder",
                   str(source), "demo", commit, "", "", str(launch), "--", sys.executable, str(runner),
                   "smoke-work", "true", "exit 4"]
        with START_SUBPROCESS(command, env={**self.environment, "MAC_PLACEMENT": episode + "/attempt"},
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT) as child:
            assert child.stdout is not None and child.stdin is not None
            deadline = time.monotonic() + TEST_HANG_GUARD_SECS
            try:
                with closing(offload.bounded_output_lines(child.stdout, lambda: deadline - time.monotonic())) as lines:
                    self.assertEqual(next(lines, b""), f"mac_smoke_register={episode}\n".encode())
                    _ = child.stdin.write(b"+")
                    child.stdin.flush()
                    self.assertIn(f"mac_smoke_ready={episode}\n".encode(), lines)
                _ = child.stdin.write(b"\n")
                child.stdin.flush()
                status = child.wait(timeout=TEST_HANG_GUARD_SECS)
                self.assertEqual(status, 0, child.stdout.read())
            finally:
                child.stdin.close()
                if child.poll() is None:
                    child.kill()
        self.assertEqual(mac_queue_run.smoke_status_path(episode).read_text(), "4\nretained\n")
        self.assertEqual(build_folders.build_folder_state(self.checkout), "kept")
        self.assertTrue(mac_queue_run.smoke_launch_token(self.checkout, episode).exists())

        installed = self.root / "installed/scripts"
        (installed / "production").mkdir(parents=True)
        _ = shutil.copy2(runner.parents[1] / "production/mac_run.sh", installed / "production/mac_run.sh")
        (installed / "lib").mkdir()
        proof = installed / "lib/py"
        _ = proof.write_text('#!/bin/sh\n[ "$2" = stop-checkout ] && [ "$3" = "$FIXTURE_CHECKOUT" ] || exit 4\n'
                             + 'printf "%s\\n" "hana-instances: checkout clear"\n')
        proof.chmod(0o755)
        _ = write_config_fixture(self.root / "build_folders.conf", "roots.build=~/other\nroots.build.natedev=~/other\n")
        with (mock.patch.object(mac_queue_run, "__file__", str(installed / "mac_test/mac_queue_run.py")),
              mock.patch.dict(os.environ, {"FIXTURE_CHECKOUT": str(self.checkout)})):
            self.assertEqual(mac_queue_run.recover_smoke_folder(episode), 0)
        self.assertFalse(mac_queue_run.smoke_folder_record(episode).exists())
        self.assertFalse(mac_queue_run.smoke_launch_token(self.checkout, episode).exists())
        self.assertFalse(launch.exists())
        self.assertFalse((Path.home() / "other").exists())
        self.assertEqual(build_folders.build_folder_state(self.checkout), "free")


class OffloadCommandTests(OffloadCommandFixture):
    @override
    def setUp(self) -> None:
        super().setUp()
        _ = write_config_fixture(self.root / "build_folders.conf", "forms.build.mac=per-unit\n",
                                 template="build_folders")

    def install_real_fixed_folder_transport(self, *, hold_receiver: bool = False, delay_cleanup: bool = False) -> None:
        _ = write_config_fixture(self.root / "build_folders.conf",
                                 "forms.build.mac=per-repository\nroots.build=~/build\nroots.build.natedev=~/build\n")
        _ = (self.root / "machines.conf").write_text("aliases=mac\nmac.platform=linux\nmac.hosts=fixture\n")
        home = Path(self.environment["HOME"])
        installed = home / ".claude"
        installed.mkdir(parents=True, exist_ok=True)
        (installed / "scripts").symlink_to(Path(offload.__file__).resolve().parents[1])
        source = self.repository / "source.txt"
        _ = source.write_text("fixture source\n")
        fixture = self.stub_directory
        workload = fixture / "workload.py"
        self.write_executable(workload, rf"""
from pathlib import Path
checkout = Path(Path({str(fixture / "checkout-path")!r}).read_text())
assert Path({str(self.claim_file)!r}).exists()
assert (checkout / 'source.txt').read_text() == 'fixture source\n'
(checkout / 'report').mkdir()
(checkout / 'report/result.txt').write_text('fixture result\n')
(checkout / 'target').mkdir()
(checkout / 'target/old-build').touch()
print('mac_exit=0', flush=True)
print('mac_cleanup=0 32715571 59', flush=True)
""")
        cleanup_bin = fixture / "cleanup-bin"
        cleanup_bin.mkdir()
        self.write_executable(cleanup_bin / "df", 'print("Filesystem 1G-blocks Used Available\\nfixture 10 9 1")\n')
        replacements = self.root / "legacy-bin"
        replacements.mkdir()
        (replacements / "ssh").symlink_to(self.bin_directory / "command-stub")
        ssh = self.bin_directory / "ssh"
        ssh.unlink()
        self.write_executable(ssh, f"""
import os, shlex, sys
from pathlib import Path
command = sys.argv[-1]
shell = "/bin/bash"
if "hold-offload-folder" in command or "fetch_kib=" in command:
    os.execv(shell, [shell, "-c", command])
if "sweep.sweep_workspace" in command and "~/.claude/scripts/lint/lint " not in command:
    Path({str(fixture / "post-fetch-command")!r}).write_text(command)
    if {delay_cleanup!r}:
        sys.exit(255)
    os.environ["PATH"] = {str(cleanup_bin)!r} + os.pathsep + os.environ["PATH"]
    os.execv(shell, [shell, "-c", command])
if "copies.py mirror-use " in command and "\\ndispatch_status=$?\\n" in command:
    prefix, separator, body = command.partition("~/.claude/scripts/lib/py ~/.claude/scripts/copies/copies.py mirror-use ")
    mirror, end, suffix = body.rpartition("\\n)\\ndispatch_status=$?\\n")
    words = shlex.split(mirror)
    assert words[-3:-1] == ["sh", "-c"], words
    words[-1] = shlex.join([sys.executable, {str(workload)!r}])
    command = prefix + separator + shlex.join(words) + end + suffix
    os.execv(shell, [shell, "-c", command])
fallback = {str(replacements / "ssh")!r}
os.execv(fallback, [fallback, *sys.argv[1:]])
""")
        rsync = self.bin_directory / "rsync"
        rsync.unlink()
        self.write_executable(rsync, f"""
import fcntl, os, shutil, subprocess, sys
from pathlib import Path
fixture = Path({str(fixture)!r})
claim = Path({str(self.claim_file)!r})
dispatch = Path.home() / ".local/state/mac-test/dispatch-fences"
arguments = sys.argv[1:]
if arguments == ["--server", "--sender"]:
    record, = dispatch.rglob("*.folder")
    assert os.path.samestat(os.fstat(8), record.with_suffix(".lock").stat())
elif arguments == ["--server"]:
    record, = dispatch.rglob("*.folder")
    checkout = Path(record.read_text().strip())
    assert checkout.is_relative_to(Path.home()), checkout
    (fixture / "checkout-path").write_text(str(checkout))
    assert claim.exists()
    if {hold_receiver!r}:
        (fixture / "receiver-started").touch()
        with (fixture / "continue-copy").open() as gate:
            assert gate.readline() == "continue\\n"
        (fixture / "receiver-had-claim").write_text(str(claim.exists()))
    else:
        fences = tuple(dispatch.rglob("*.lock"))
        assert len(fences) == 1, fences
        with fences[0].open() as fence:
            try:
                fcntl.flock(fence, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                pass
            else:
                sys.exit("source receiver did not hold the dispatch fence")
    shutil.copyfile({str(source)!r}, checkout / "source.txt")
elif any(argument.startswith("--rsync-path=") and "mirror-receive" in argument for argument in arguments):
    remote = next(argument.removeprefix("--rsync-path=") for argument in arguments
                  if argument.startswith("--rsync-path=") and "mirror-receive" in argument)
    with subprocess.Popen(["/bin/bash", "-c", remote + " --server"], start_new_session=True) as receiver:
        sys.exit(receiver.wait())
else:
    for argument in arguments:
        if argument.startswith("--rsync-path="):
            remote = argument.removeprefix("--rsync-path=")
            subprocess.run(["/bin/bash", "-c", remote + " --server --sender"], check=True)
    checkout = Path((fixture / "checkout-path").read_text())
    assert claim.exists()
    assert checkout.with_name(checkout.name + ".keep").exists()
    shutil.copyfile(checkout / "report/result.txt", Path(arguments[-1]) / "result.txt")
""")

    def test_real_fixed_holder_releases_after_copy_test_and_fetch(self) -> None:
        self.install_real_fixed_folder_transport()

        result = self.command(runner_options=("--fetch", "report"), call_id="fixed-holder")

        self.assertEqual(result.returncode, 0, result.stdout)
        checkout = Path((self.stub_directory / "checkout-path").read_text())
        self.assertEqual((self.state_directory / "fetched/fixed-holder/report/result.txt").read_text(),
                         "fixture result\n")
        self.assertFalse((checkout / "target/old-build").exists(), result.stdout)
        self.assertFalse(self.claim_file.exists())
        self.assertFalse(build_folders.copy_keep_file(checkout).exists())
        dispatch = Path(self.environment["HOME"]) / ".local/state/mac-test/dispatch-fences"
        self.assertEqual(list(dispatch.rglob("*.folder")), [])

    def test_delayed_post_fetch_cleanup_cannot_remove_the_next_build_output(self) -> None:
        self.install_real_fixed_folder_transport(delay_cleanup=True)
        self.fixture["cleanup_lines"] = ["leftovers=1", "rc=0"]
        result = self.command(runner_options=("--fetch", "report"))
        self.assertIn("the fixed build folder clearance is unconfirmed", result.stdout)
        self.assertEqual((self.state_directory / "fetched/call-17/report/result.txt").read_text(), "fixture result\n")
        delayed = (self.stub_directory / "post-fetch-command").read_text()
        dispatch = Path(self.environment["HOME"]) / ".local/state/mac-test/dispatch-fences"
        record, = dispatch.rglob("*.folder")
        with mock.patch.dict(os.environ, self.environment, clear=True):
            saved = offload.SavedOffloadFolder(record)
            old_checkout = offload.read_fixed_folder_record(saved).path
            offload.clear_fixed_folder_record(saved)
            with build_folders.fixed_repository_build_folder(old_checkout.parent.name, machine="mac",
                                                             unit="next-run") as checkout:
                self.assertEqual(checkout, old_checkout)
                next_output = checkout / "target/next-build"
                next_output.parent.mkdir(exist_ok=True)
                _ = next_output.write_text("new build")
                environment = {**self.environment,
                               "PATH": str(self.stub_directory / "cleanup-bin") + os.pathsep + self.environment["PATH"]}
                replay = RUN_SUBPROCESS(["/bin/bash", "-c", delayed], cwd=self.root, env=environment,
                                        capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS)
                self.assertEqual(replay.returncode, os.EX_TEMPFAIL, replay.stdout + replay.stderr)
                self.assertEqual(next_output.read_text(), "new build")

    def test_term_during_source_transfer_keeps_the_claim_for_a_live_receiver(self) -> None:
        self.install_real_fixed_folder_transport(hold_receiver=True)
        gate = os.open(self.stub_directory / "continue-copy", os.O_WRONLY | os.O_NONBLOCK)
        self.addCleanup(os.close, gate)
        process = self.start_command()
        try:
            self.wait_for_path(self.stub_directory / "receiver-started")
            checkout = Path((self.stub_directory / "checkout-path").read_text())
            process.send_signal(signal.SIGTERM)
            output, _ = process.communicate(timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(process.returncode, 128 + signal.SIGTERM, output)
            self.assertTrue(self.claim_file.exists())
            self.assertTrue(build_folders.copy_keep_file(checkout).exists())
            claim = claim_state.read_run(self.claim_file)
            assert not isinstance(claim, claim_state.NoRun)
            paths = claim_state.StatePaths(self.state_directory, "mac")
            self.assertTrue(claim_state.claim_pending_start(paths, self.claim_file, claim).exists())
            home = Path(self.environment["HOME"])
            state = offload.fixed_folder_record(claim_state.claim_identity(claim), "mac",
                                               claim_state.claim_fence_scope_key(self.claim_file, claim))
            with self.assertRaises(BlockingIOError):
                offload.clear_fixed_folder_record(offload.SavedOffloadFolder(home / state.path))
        finally:
            _ = os.write(gate, b"continue\n")
            if process.poll() is None:
                process.kill()
            _ = process.communicate(timeout=TEST_HANG_GUARD_SECS)
        self.wait_for_path(checkout / "source.txt")
        self.assertEqual((self.stub_directory / "receiver-had-claim").read_text(), "True")

    def install_fixed_folder_ssh(self, *, drop_clearance: bool) -> tuple[Path, Path]:
        _ = write_config_fixture(self.root / "build_folders.conf", "forms.build.mac=per-repository\n",
                                 template="build_folders")
        checkout = self.root / "remote build/hana/mac"
        holder = self.stub_directory / "holder"
        real_ssh = self.root / "legacy-bin/ssh"
        real_ssh.parent.mkdir()
        real_ssh.symlink_to(self.bin_directory / "command-stub")
        ssh = self.bin_directory / "ssh"
        ssh.unlink()
        self.write_executable(ssh, f"""
import json, os, sys
from pathlib import Path
if "hold-offload-folder" not in sys.argv[-1]:
    os.execv({str(real_ssh)!r}, [{str(real_ssh)!r}, *sys.argv[1:]])
print("build_folder=" + json.dumps({str(checkout)!r}), flush=True)
if sys.stdin.read(1) != "+":
    sys.exit(1)
Path({str(holder)!r}).write_text("held")
print("build_folder_confirmed", flush=True)
if sys.stdin.read(1) != "\\n":
    sys.exit(1)
if not Path({str(self.claim_file)!r}).exists():
    sys.exit("the provider claim disappeared before clearance")
Path({str(holder)!r}).write_text("clearance lost" if {drop_clearance!r} else "released")
sys.exit(255 if {drop_clearance!r} else 0)
""")
        return checkout, holder

    def test_lost_fixed_folder_clearance_keeps_the_offload_claim(self) -> None:
        _, holder = self.install_fixed_folder_ssh(drop_clearance=True)

        result = self.command()

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assert_wire("passed", "")
        self.assertIn("the fixed build folder clearance is unconfirmed", result.stdout)
        self.assertEqual(holder.read_text(), "clearance lost")
        self.assertTrue(self.claim_file.exists())
        claim = cast(claim_state.RunRecord, json.loads(self.claim_file.read_text()))
        self.assertTrue(claim_state.claim_pending_start(
            claim_state.StatePaths(self.state_directory, "mac"), self.claim_file, claim).exists())

    def test_fixed_mac_offload_uses_one_folder_for_copy_run_and_fetch(self) -> None:
        checkout, holder = self.install_fixed_folder_ssh(drop_clearance=False)
        result = self.command(runner_options=("--fetch", "report"))
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(holder.read_text(), "released")
        copied, fetched = self.events_for("rsync")
        self.assertIn(f":{shlex.quote(str(checkout))}/", copied["args"][-1])
        self.assertIn(shlex.quote(str(checkout / "report")), fetched["args"][-2])
        ran = next(event["args"][-1] for event in self.events_for("ssh") if "lint nextest" in event["args"][-1])
        self.assertIn(f"cd {shlex.quote(str(checkout))}", ran)
        self.assertIn(f"CARGO_BUILD_BUILD_DIR={shlex.quote(str(checkout / 'target'))}", ran)

    def test_source_copy_interrupt_releases_claim_after_fixed_folder_clearance(self) -> None:
        _, holder = self.install_fixed_folder_ssh(drop_clearance=False)
        capture = offload.ProcessController.capture_copy

        def interrupt_copy(controller: offload.ProcessController, arguments: Sequence[str],
                           deadline: offload.CommandDeadline = offload.NO_COMMAND_TIME_LIMIT
                           ) -> offload.CapturedCommandResult:
            if arguments[0] == "rsync":
                self.assertEqual(holder.read_text(), "held")
                self.assertTrue(self.claim_file.exists())
                raise offload.OffloadInterrupted(signal.SIGTERM)
            return capture(controller, arguments, deadline)

        with mock.patch.object(offload.ProcessController, "capture_copy", interrupt_copy):
            result = self.command()

        self.assertEqual(result.returncode, 128 + signal.SIGTERM, result.stdout)
        self.assertEqual(holder.read_text(), "released")
        self.assertFalse(self.claim_file.exists())

    def test_waiter_hands_relative_path_claim_to_runner_for_cleanup_only(self) -> None:
        assignment = self.assignment_options(self.repository, "nextest", self.config_path, ())
        retained = self.write_kept_claim(legacy=True)
        claim_file = self.claim_file.rename(self.claim_file.with_name("saved-legacy.json"))
        _ = claim_file.write_text(json.dumps({**retained, "worktree": ".", "repo_name": "hana"}))
        reservation = self.root / "reservation"
        identity = self.root / "reservation.identity"
        waiter = str(os.getpid())
        result = invoke_cli(build_targets, build_targets.main, [
            "reserve", *assignment, "--repo-root", str(self.repository), "--package", "hana",
            "--kind", "test", "--pid", waiter, "--reservation-file", str(reservation),
            "--claim-identity-file", str(identity),
        ], self.environment, self.root,
            environment_lock=caller_admission(self, self.root, self.environment).loop_access)
        self.assertEqual(result.returncode, 13, result.stdout + result.stderr)
        target, settings = reservation.read_text().rstrip("\n").split("\t")
        claim_id, episode, attempt, session = identity.read_text().rstrip("\n").split("\t")

        missing = self.repository
        _ = missing.rename(self.root / "former-checkout")
        selectors = self.stub_directory / "selectors"
        selectors.mkdir()
        self.write_executable(selectors / "pgrep", "raise SystemExit(1)\n")
        self.write_executable(selectors / "lsof", f"print('p41\\nn/home/fixture/{FIXTURE_MIRROR_ROOT}/old-mirror')\n")
        self.write_executable(selectors / "sleep", "raise SystemExit(0)\n")
        self.fixture["selector_shell"] = "sh"
        self.sync_fixture()
        result = self.command(repository=missing, subprocess_cli=True, assigned_adapter=True, settings=Path(settings), runner_options=(
            *assignment, "--target", target, "--claim-owner-pid", waiter, "--claim-id", claim_id,
            "--episode", episode, "--attempt", attempt, "--claim-session", session,
        ))
        self.assertEqual(result.returncode, 75, result.stdout)
        self.assertTrue(result.stdout.endswith(f"mac_test: not running on the {target} (busy)\n"), result.stdout)
        self.assert_wire("declined", "busy")
        self.assertTrue(claim_file.exists())
        cleanup, = self.leftover_events()
        self.assertEqual(self.events(), [cleanup])
        adopted = cleanup.get("claim")
        self.assertIsNotNone(adopted)
        assert adopted is not None
        self.assertNotEqual(adopted["pid"], int(waiter))
        self.assertEqual(adopted["claim_id"], retained["claim_id"])
        self.assertEqual(adopted["worktree"], ".")
        self.assertEqual(adopted["remote_host"], retained["remote_host"])
        script = cleanup["args"][-1]
        self.assertNotIn("kill ", script)
        self.assertEqual((self.stub_directory / "selector-answer").read_text().splitlines(), ["leftovers=1", "rc=0"])

    def test_claim_stub_preserves_the_host_and_worktree_during_cleanup_handoff(self) -> None:
        kept = self.write_kept_claim(legacy=True)
        _ = self.claim_file.write_text(json.dumps({**kept, "worktree": "older-path", "cleanup_pending": True}))
        (self.stub_directory / "finish-claim").touch()
        result = subprocess.run([sys.executable, "-c", CLAIM_STUB, "claim", "--from-pid", str(kept["pid"]),
            "--pid", str(os.getpid()), "--what", "new check", "--worktree", str(self.repository),
            "--remote", "new-mac", "--claim-id", kept["claim_id"], "--episode", kept["episode"],
            "--attempt", kept["attempt"], "--session", kept["submitting_session"]],
            cwd=Path(offload.__file__).parents[2], env=self.environment, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 13, result.stderr)
        self.assertIn(f"leftovers: {kept['remote_host']}\n", result.stdout)
        saved = self.remote_claim()
        self.assertEqual((saved["remote_host"], saved["worktree"], saved["pid"]),
                         (kept["remote_host"], "older-path", os.getpid()))

    def test_wait_claim_decline_prints_the_child_status_and_failed_check(self) -> None:
        result = self.command(runner_options=(
            "--claim-owner-pid", str(os.getpid()), "--claim-id", "missing-claim",
            "--episode", "wait-episode", "--attempt", "wait-attempt",
        ))
        self.assertEqual(result.returncode, 75, result.stdout + result.stderr)
        self.assertIn("mac_test: not running on the Mac (state: claim child exited 12: "
                      + "cannot adopt the Mac claim: no reservation)", result.stdout)
        self.assert_wire("declined", "state")
        self.assertEqual(self.events(), [])

    def test_remote_build_target_directory_reaches_the_result_file(self) -> None:
        self.write_portable_workspace()
        stub = self.bin_directory / "command-stub"
        _ = stub.write_text(stub.read_text().replace("$remote == *'lint doc'*",
            "$remote == *'lint doc'* || $remote == *'lint build'*"))
        self.fixture["run_status_line"] = "build_target_directory=/remote/cargo output\nmac_exit=0"
        self.sync_fixture()
        result = self.command("--workspace", "--bins", verb="build")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("build_target_directory=/remote/cargo output\n", self.result_path.read_text())

    def test_configuration_declines_before_claiming(self) -> None:
        self.config["offload"] = "off"
        self.sync_config()
        self.assert_declined(self.command(), "off")
        self.assertEqual(self.events(), [])

        self.config["offload"] = "on"
        _ = self.config.pop("linux_only.hana")
        self.sync_config()
        self.assert_declined(self.command(), "repo")
        self.assertEqual(self.events(), [])

        self.config["linux_only.hana"] = "hana,platform_only"
        self.sync_config()
        self.assert_declined(self.command(), "linux_only")
        self.assertEqual(self.events(), [])

    def test_directory_outside_git_declines_as_unknown_repository(self) -> None:
        result = self.command(repository=self.unknown_directory)
        self.assert_declined(result, "repo")
        self.assertEqual(self.events(), [])

    def test_unreachable_status_sets_backoff_without_a_second_probe(self) -> None:
        self.fixture["probe_status"] = 255
        self.sync_fixture()
        self.assert_declined(self.command(), "unreachable")
        probe, settlement = self.events_for("ssh")
        self.assertNotIn("dispatch_start_settled=", probe["args"][-1])
        self.assertIn("dispatch_start_settled=", settlement["args"][-1])
        self.assertIn("claim", probe)
        assert "claim" in probe
        claim = probe["claim"]
        identity = claim_state.ClaimIdentity(claim["claim_id"], claim["episode"], claim["attempt"])
        self.assertIn(offload.pending_start_key(identity), settlement["args"][-1])

        self.clear_events()
        self.assert_declined(self.command(), "backoff")
        self.assertEqual(self.events(), [])

    def test_invalid_backoff_file_does_not_skip_probe(self) -> None:
        self.state_directory.mkdir()
        _ = (self.state_directory / "unreachable_until").write_text(
            "not a number\n", encoding="utf-8"
        )
        result = self.command()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertTrue((self.stub_directory / "probe-started").exists())
        self.assertEqual(len(self.events_for("ssh")), 2)

    def test_probe_timeout_declines_as_unreachable(self) -> None:
        self.config["probe_timeout_s"] = "0.1"
        self.fixture["hold_probe"] = True
        self.sync_config()
        self.sync_fixture()
        original_launch = subprocess.Popen
        original_communicate = cast(Callable[[subprocess.Popen[str], str | None, float | None], tuple[str, str]],
                                    original_launch.communicate)

        def communicate(process: subprocess.Popen[str], input: str | None = None,
                        timeout: float | None = None) -> tuple[str, str]:
            if timeout == 0.1:
                self.wait_for_path(self.stub_directory / "probe-started")
                raise subprocess.TimeoutExpired(process.args, 0.1)
            return original_communicate(process, input, timeout)

        with mock.patch.object(original_launch, "communicate", communicate):
            result = self.command(separate_stderr=True)
        self.assert_declined(result, "unreachable")
        self.assertRegex(result.stderr, r"^mac_test: could not reach the Mac: .*0\.1.*\n$")
        self.assertRegex(result.stderr, r"(?i)tim(?:e|ed).*out|limit|finish")
        self.assertTrue((self.stub_directory / "probe-started").exists())
        probe, settlement = self.events_for("ssh")
        self.assertNotIn("dispatch_start_settled=", probe["args"][-1])
        self.assertIn("dispatch_start_settled=", settlement["args"][-1])
        self.assertIn("claim", probe)
        assert "claim" in probe
        claim = probe["claim"]
        identity = claim_state.ClaimIdentity(claim["claim_id"], claim["episode"], claim["attempt"])
        self.assertIn(offload.pending_start_key(identity), settlement["args"][-1])

    def test_probe_without_final_status_declines_as_unreachable(self) -> None:
        self.fixture["probe_lines"] = self.fixture["probe_lines"][:-1]
        self.sync_fixture()
        result = self.command(separate_stderr=True)
        self.assert_declined(result, "unreachable")
        self.assertEqual(result.stderr, "mac_test: could not reach the Mac: the probe answer could not be read\n")
        probe, settlement = self.events_for("ssh")
        self.assertNotIn("dispatch_start_settled=", probe["args"][-1])
        self.assertIn("dispatch_start_settled=", settlement["args"][-1])
        self.assertIn("claim", probe)
        assert "claim" in probe
        claim = probe["claim"]
        identity = claim_state.ClaimIdentity(claim["claim_id"], claim["episode"], claim["attempt"])
        self.assertIn(offload.pending_start_key(identity), settlement["args"][-1])

    def test_failed_probe_prints_collapsed_ssh_error_before_unreachable_decline(self) -> None:
        self.fixture["probe_status"] = 255
        self.fixture["probe_error"] = "ssh:\tconnection refused\n  host mac is unavailable  "
        self.sync_fixture()
        result = self.command(separate_stderr=True)
        self.assert_declined(result, "unreachable")
        self.assertEqual(result.stderr, "mac_test: could not reach the Mac: host mac is unavailable\n")
        self.assertEqual(self.events_for("rsync"), [])
        self.assertTrue((self.state_directory / "unreachable_until").exists())

    def test_other_build_processes_do_not_decline_mac_tests(self) -> None:
        self.fixture["probe_lines"] = [
            "procs=1",
            "free_gib=120",
            "host=Mac",
            "rc=0",
        ]
        self.sync_fixture()
        result = self.command()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assert_wire("passed", "")
        self.assertEqual(len(self.events_for("ssh")), 2)
        self.assertEqual(len(self.events_for("rsync")), 1)

    def test_run_settings_selects_alternate_host_and_budget_over_environment_default(self) -> None:
        alternate = self.root / "alternate settings.conf"
        values = {**self.config, "host": "alternate-mac", "mac_budget_gib.hana": "8"}
        _ = write_config_fixture(alternate, "".join(f"{key}={value}\n" for key, value in values.items()),
                                 template="mac_test")
        self.config["offload"] = "off"
        self.sync_config()
        linux_settings = self.root / "linux.conf"
        _ = write_config_fixture(linux_settings, "offload=on\nhost=fixture-linux\nkinds=test\n", template="mac_test")
        registry = self.root / "build_targets.conf"
        _ = registry.write_text("targets=mac,builder\nkind.mac=ssh\nplatform.mac=macos\n"
            + f"settings.mac={alternate}\nkind.builder=ssh\nplatform.builder=linux\nsettings.builder={linux_settings}\n"
            + UNREACHABLE_WAIT_FIXTURE)
        self.environment["BUILD_TARGETS_CONFIG"] = str(registry)
        request_placement = admission_client.request_placement

        def submit(request: wire.PlacementRequest
                   ) -> wire.Assignment | wire.LocalPlacementGranted | wire.PlacementWait | wire.DispatchStatus:
            self.assertEqual([candidate.settings for candidate in request.constraints.candidates
                              if candidate.destination.target == "mac"],
                             [placement.ExplicitProviderSettings(alternate.resolve())])
            return request_placement(request)

        for index, (target, settings) in enumerate((
            ("", alternate), ("", alternate.relative_to(self.root)),
            ("mac", alternate), ("mac", Path(alternate.name)), ("builder", alternate),
        )):
            with self.subTest(target=target, settings=settings), mock.patch.object(
                    admission_client, "request_placement", side_effect=submit):
                self.clear_events()
                options = ("--target", target) if target else ()
                if target == "builder":
                    with (
                        mock.patch.object(eligibility, "mac_step_eligibility", return_value=HANA),
                        mock.patch.object(offload, "trusted_linux_source", return_value=True),
                        mock.patch.object(offload, "recorded_linux_version_mismatch", return_value=""),
                        mock.patch.object(offload, "claim_for_run", return_value=offload.FreshMacClaim()),
                        mock.patch.object(offload, "expected_linux_parity", return_value={}),
                        mock.patch.object(offload, "copy_tracked_scripts", return_value=False) as scripts_copy,
                    ):
                        result = self.command(settings=settings, runner_options=options, separate_stderr=True)
                    self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                    self.assertIn(str(alternate.resolve()), result.stderr)
                    self.assertIn(str(linux_settings.resolve()), result.stderr)
                    self.assertEqual(self.events_for("rsync"), [])
                    scripts_copy.assert_not_called()
                    continue
                if index == 0:
                    result = self.command(settings=settings, runner_options=options, direct_placement=True)
                    self.assertEqual(result.returncode, 0, result.stdout)
                    self.assert_wire("passed", "")
                    ssh = self.events_for("ssh")
                    self.assertEqual(ssh[0]["args"][-2], "alternate-mac")
                    self.assertEqual(ssh[1]["args"][-2], "alternate-mac")
                    self.assertIn("LINT_SWEEP_BUDGET_GIB=8", ssh[1]["args"][-1])
                    remote_words, _ = split_mac_run_command(ssh[1]["args"][-1])
                    lint_index = remote_words.index("~/.claude/scripts/lint/lint")
                    self.assertIn("MACOSX_DEPLOYMENT_TARGET=14.0", remote_words)
                    self.assertLess(remote_words.index("MACOSX_DEPLOYMENT_TARGET=14.0"), lint_index)
                    self.assertEqual(remote_words[lint_index + 1 :], ["nextest"])
                    self.assertIn("alternate-mac:.local/state/mac-test/test-mirror/hana/", self.events_for("rsync")[0]["args"])
                    continue
                observed: list[tuple[eligibility.OffloadConfig, offload.OffloadRequest]] = []

                def observe(_controller: offload.ProcessController, config: eligibility.OffloadConfig,
                            request: offload.OffloadRequest, _started: float) -> offload.MacOffloadResult:
                    observed.append((config, request))
                    return offload.PassedOnMac(0)

                with mock.patch.object(offload, "run_offload", side_effect=observe):
                    result = invoke_cli(offload, offload.main, ["run", "--repo-root", str(self.repository),
                        "--package", "hana", "--call-id", "settings", "--result", str(self.result_path),
                        "--settings", str(settings), *options], self.environment, self.root,
                        environment_lock=caller_admission(self, self.root, self.environment).loop_access)
                self.assertEqual(result.returncode, 0, result.stdout)
                self.assert_wire("passed", "")
                config, request = observed[0]
                with mock.patch.dict(os.environ, self.environment, clear=True):
                    prefix = offload.ssh_prefix(config)
                    remote = offload.remote_test_command(config, request, HANA)
                    mirror = offload.request_mirror(config, request, HANA)
                    copied = copies.remote_rsync(self.repository, mirror, self.root / "excludes",
                                                  offload.ssh_options(config.connection))
                self.assertEqual(prefix[-1], "alternate-mac")
                self.assertIn("LINT_SWEEP_BUDGET_GIB=8", remote)
                remote_words, _ = split_mac_run_command(remote)
                lint_index = remote_words.index("~/.claude/scripts/lint/lint")
                self.assertIn("MACOSX_DEPLOYMENT_TARGET=14.0", remote_words)
                self.assertLess(remote_words.index("MACOSX_DEPLOYMENT_TARGET=14.0"), lint_index)
                self.assertEqual(remote_words[lint_index + 1 :], ["nextest"])
                self.assertIn("alternate-mac:.local/state/mac-test/test-mirror/hana/", copied)

    def test_low_disk_declines_after_probe(self) -> None:
        install_floor_sweep_stub(self)
        self.fixture["probe_lines"] = ["free_gib=59", "host=Mac", "rc=0"]
        self.sync_fixture()
        self.assert_declined(self.command(), "disk")
        self.assertEqual(self.events_for("rsync"), [])
        [arguments] = [event["args"] for event in self.events_for("ssh")
                       if "sweep.py --mac-floor-deadline " in event["args"][-1]]
        self.assertIn("-tt", arguments)
        self.assertEqual(arguments[arguments.index("-S") + 1], "none")

    def test_battery_power_does_not_decline_mac_tests(self) -> None:
        self.assertNotIn("pmset", offload.probe_script(eligibility.KnownRepository("hana")))
        self.fixture["probe_lines"] = ["power=battery", "free_gib=120", "host=Mac", "rc=0"]
        self.sync_fixture()
        result = self.command()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assert_wire("passed", "")

    def test_claim_exit_reasons_never_reach_external_commands(self) -> None:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        self.state_directory.mkdir()
        block = {
            "version": 2,
            "holder": "alice",
            "for": "demo",
            "since": now.isoformat(),
            "expires": (now + timedelta(hours=1)).isoformat(),
            "session": None,
            "showrunner": None,
            "state": "active",
            "ci": "not_configured",
            "free_message": {"kind": "not_needed"},
        }
        _ = (self.state_directory / "block.json").write_text(
            json.dumps(block), encoding="utf-8"
        )
        self.assert_declined(self.command(assigned_adapter=True), "blocked")
        self.assertEqual(self.events(), [])

        (self.state_directory / "block.json").unlink()
        claimed = self.state_command(
            "claim",
            "--pid",
            str(os.getpid()),
            "--what",
            "other tests",
            "--worktree",
            str(self.repository),
        )
        self.assertEqual(claimed.returncode, 0, claimed.stdout + claimed.stderr)
        busy = self.command(assigned_adapter=True)
        self.assertEqual(busy.returncode, 75, busy.stdout)
        self.assertTrue(busy.stdout.endswith("mac_test: not running on the Mac (busy)\n"), busy.stdout)
        self.assertIn("busy", busy.stdout)
        self.assert_wire("declined", "busy")
        self.assertEqual(self.wire_result().get("holder"), "other tests")
        self.assertTrue(self.claim_file.exists())
        self.assertEqual(self.events(), [])
        claim = self.remote_claim()
        released = self.state_command("release", "--pid", str(os.getpid()), "--claim-id", claim["claim_id"],
                                      "--episode", claim["episode"], "--attempt", claim["attempt"])
        self.assertEqual(released.returncode, 0, released.stdout + released.stderr)

        _ = (self.state_directory / "block.json").write_text(
            "damaged\n", encoding="utf-8"
        )
        unreadable = self.command(assigned_adapter=True)
        self.assert_declined(unreadable, "state")
        self.assertIn("state unreadable", unreadable.stdout)
        self.assertIn("block.json", unreadable.stdout)
        self.assertEqual(self.events(), [])

    def test_retained_expired_block_still_declines_and_prints_holder_diagnostic(self) -> None:
        self.state_directory.mkdir()
        now = datetime.now(timezone.utc)
        block = {"version": 2, "holder": "alice", "for": "restore CI first",
                 "since": (now - timedelta(hours=2)).isoformat(),
                 "expires": (now - timedelta(seconds=1)).isoformat(),
                 "session": None, "showrunner": None, "state": "active",
                 "ci": "off_by_this_block", "free_message": {"kind": "not_needed"}}
        path = self.state_directory / "block.json"
        _ = path.write_text(json.dumps(block))
        before = path.read_bytes()
        result = self.command(assigned_adapter=True)
        self.assert_declined(result, "blocked")
        self.assertIn("blocked by alice: restore CI first", result.stdout)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.events(), [])

    def test_repeated_busy_decline_is_silent_and_preserves_wire_result(self) -> None:
        claimed = self.state_command("claim", "--pid", str(os.getpid()), "--what", "other tests",
                                     "--worktree", str(self.repository))
        self.assertEqual(claimed.returncode, 0, claimed.stdout + claimed.stderr)
        first = self.command(separate_stderr=True, assigned_adapter=True)
        first_wire = self.wire_result()
        self.assertEqual(first.stdout, "mac_test: not running on the Mac (busy)\n")
        self.assertTrue(first.stderr)
        repeated = self.command(last_decline="busy", separate_stderr=True, assigned_adapter=True)
        self.assertEqual((repeated.returncode, repeated.stdout, repeated.stderr), (75, "", ""))
        self.assertEqual(repeated.returncode, first.returncode)
        self.assertEqual(self.wire_result()["mac"], first_wire["mac"])
        self.assertEqual(self.wire_result()["reason"], first_wire["reason"])
        self.assertEqual(self.wire_result().get("holder"), "other tests")
        self.assertEqual(self.events(), [])
        self.assertTrue(self.claim_file.exists())

    def test_changed_decline_prints_normally_and_repeated_blocked_decline_is_silent(self) -> None:
        self.state_directory.mkdir()
        now = datetime.now(timezone.utc)
        block = {"version": 2, "holder": "alice", "for": "restore CI first",
                 "since": now.isoformat(), "expires": (now + timedelta(hours=1)).isoformat(),
                 "session": None, "showrunner": None, "state": "active",
                 "ci": "not_configured", "free_message": {"kind": "not_needed"}}
        _ = (self.state_directory / "block.json").write_text(json.dumps(block))
        first = self.command(separate_stderr=True, assigned_adapter=True)
        changed = self.command(last_decline="busy", separate_stderr=True, assigned_adapter=True)
        self.assertEqual((changed.returncode, changed.stdout, changed.stderr),
                         (first.returncode, first.stdout, first.stderr))
        self.assertEqual(changed.stdout, "mac_test: not running on the Mac (blocked)\n")
        self.assertIn("blocked by alice: restore CI first", changed.stderr)
        self.assert_wire("declined", "blocked")
        repeated = self.command(last_decline="blocked", separate_stderr=True, assigned_adapter=True)
        self.assertEqual((repeated.returncode, repeated.stdout, repeated.stderr), (75, "", ""))
        self.assert_wire("declined", "blocked")
        self.assertEqual(self.events(), [])

    def test_copy_failure_releases_claim(self) -> None:
        self.config["mirror_busy_backoff_s"] = "120"
        self.sync_config()
        self.fixture["rsync_status"] = 23
        self.sync_fixture()
        self.assert_declined(self.command(), "copy")
        probe, settlement = self.events_for("ssh")
        self.assertNotIn("dispatch_start_settled=", probe["args"][-1])
        self.assertIn("dispatch_start_settled=", settlement["args"][-1])
        self.assertIn("claim", probe)
        assert "claim" in probe
        claim = probe["claim"]
        identity = claim_state.ClaimIdentity(claim["claim_id"], claim["episode"], claim["attempt"])
        self.assertIn(offload.pending_start_key(identity), settlement["args"][-1])
        self.assertEqual(len(self.events_for("rsync")), 1)
        self.assert_claimed_commands()
        path = self.state_directory / "mirror-busy-hana.until"
        self.assertFalse(path.exists())
        self.fixture["rsync_status"] = 0
        self.sync_fixture()
        config = eligibility.read_config(self.config_path)
        request = offload.OffloadRequest(self.repository, "hana", "busy", offload.NO_RESULT_FILE, (),
                                        owner="verify", target="mac", holder_session="fixture")
        mirror = offload.request_mirror(config, request, HANA)
        with mock.patch.dict(os.environ, self.environment), copies.remote_lease(mirror):
            result = self.command(runner_options=("--target", "mac"))
            self.assertEqual(result.returncode, 75, result.stdout)
            self.assertEqual(result.stdout, "mac_test: not running on the mac (busy)\n")
            self.assert_wire("declined", "busy")
        self.assertGreater(float(path.read_text()), time.time())
        self.assertLessEqual(float(path.read_text()), time.time() + 120)
        result = self.command(runner_options=("--target", "mac"))
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertFalse(path.exists())
        self.fixture["run_status_reported"] = False
        self.fixture["run_lines"] = [{"stream": "stderr", "text": "copies: /mirror/hana: busy; held by session grep"}]
        self.sync_fixture()
        result = self.command(runner_options=("--target", "mac"), direct_placement=True)
        self.assertEqual(result.returncode, 75, result.stdout)
        self.assert_wire("declined", "busy")
        self.assertEqual(self.wire_result().get("holder"), "last recorded mirror session grep")
        self.assertNotIn("without a final status", result.stdout)
        self.assertTrue(path.exists())
        self.assertFalse(self.claim_file.exists())

        admission = caller_admission(self, self.root, self.environment)
        with admission.loop_turn():
            assigned = [saved for saved in admission.server.assignments.values()
                        if isinstance(saved.permission, placement.PermissionConsumed)]
            self.assertTrue(assigned)
            for saved in assigned:
                submitted = admission.server.placement_requests[saved.assignment.episode]
                key = admission.server.host_evidence_key(submitted, saved.assignment.destination)
                admission.server.publish_host_evidence(admission.server.host_probe(key))
        _ = admission.run_pass()
        with admission.loop_turn():
            self.assertTrue(all(isinstance(admission.server.assignments[execution_identity.attempt_key(
                saved.assignment.episode, saved.assignment.attempt)].state, placement.ClosedAssignment)
                for saved in assigned), assigned)

    def test_copy_refusal_keeps_rsync_stderr_in_output_and_the_result(self) -> None:
        self.fixture["rsync_status"] = 23
        self.sync_fixture()
        stub = self.bin_directory / "command-stub"
        source = stub.read_text().replace('    exit "$rsync_status"',
            "    printf '%s\\n' 'rsync: destination denied' 'rsync error: code 23' >&2\n"
            + '    exit "$rsync_status"')
        _ = stub.write_text(source)

        result = self.command()

        self.assert_declined(result, "copy: rsync: rsync: destination denied; rsync error: code 23")
        self.assert_claimed_commands()
        self.assertEqual(len(self.events_for("rsync")), 1)
        probe, settlement = self.events_for("ssh")
        self.assertNotIn("dispatch_start_settled=", probe["args"][-1])
        self.assertIn("dispatch_start_settled=", settlement["args"][-1])
        self.assertIn("claim", probe)
        assert "claim" in probe
        claim = probe["claim"]
        identity = claim_state.ClaimIdentity(claim["claim_id"], claim["episode"], claim["attempt"])
        self.assertIn(offload.pending_start_key(identity), settlement["args"][-1])

        self.assertNotIn("holder", self.wire_result())
        self.assertFalse((self.state_directory / "mirror-busy-hana.until").exists())

        self.fixture["rsync_status"] = 12
        self.sync_fixture()
        backoff = self.state_directory / "mirror-busy-hana.until"
        for check in ("mirror lease", "mirror process check"):
            with self.subTest(check=check):
                _ = stub.write_text(source.replace(
                    "rsync: destination denied", f"copies: /mirror/hana: busy; held by session reader ({check})",
                ).replace("code 23", "code 12"))
                before = len(self.events_for("rsync"))
                result = self.command(runner_options=("--target", "mac"))
                self.assertEqual(result.returncode, 75, result.stdout + result.stderr)
                self.assert_wire("declined", "busy")
                self.assertEqual(self.wire_result().get("holder"), f"last recorded mirror session reader ({check})")
                self.assertEqual(len(self.events_for("rsync")), before + 1)
                self.assertGreater(float(backoff.read_text()), time.time())
                self.assertFalse(self.claim_file.exists())
                backoff.unlink()

    def test_tracked_script_transfer_keeps_the_failed_command_error(self) -> None:
        controller = offload.ProcessController()
        config = eligibility.read_config(self.config_path)
        commands: list[list[str]] = []

        def capture(arguments: Sequence[str],
                    deadline: offload.CommandDeadline = offload.NO_COMMAND_TIME_LIMIT) -> offload.CapturedCommandResult:
            del deadline
            argv = list(arguments)
            commands.append(argv)
            if argv[0] == "rsync":
                return offload.CommandOutput(23, "", "rsync: config denied\n")
            if "ls-files" in argv:
                return offload.CommandOutput(0, "scripts/mac_test/linux_runner.py\0", "")
            if "rev-parse" in argv:
                return offload.CommandOutput(0, "fixture-commit\n", "")
            return offload.CommandOutput(0, "", "")

        parity: offload.PublicToolParity = {"rustc": "fixture", "nextest": "fixture",
                                           "mend": "fixture", "scripts_commit": "fixture-commit"}
        with mock.patch.object(controller, "capture", side_effect=capture), \
                mock.patch.object(offload, "expected_linux_parity", return_value=parity):
            copied = offload.copy_tracked_scripts(controller, config)

        self.assertFalse(copied)
        self.assertEqual(controller.copy_failure_detail, "copy: rsync: rsync: config denied")
        self.assertEqual(commands[-1][0], "rsync")
        self.assertIn("--compress", commands[-1])
        self.assertIn("-rlpc", commands[-1])

    def test_each_mac_verb_keeps_arguments_and_names_its_claim(self) -> None:
        self.write_portable_workspace()
        self.config["mac_skip.hana"] = "test(skip-this)"
        self.sync_config()
        for verb in ("nextest", "check", "clippy", "mend-check", "doc"):
            with self.subTest(verb=verb):
                words = ("--workspace", "--all-features")
                if verb == "nextest":
                    result = self.command(*words, verb=verb)
                    self.assertEqual(result.returncode, 0, result.stdout)
                    remote = self.events_for("ssh")[-1]["args"][-1]
                    # The local claim is read by rsync before remote execution.
                    held = self.events_for("rsync")[-1]
                    self.assertTrue(held["claimed"])
                    self.assertEqual(held.get("what"), f"{verb} hana")
                else:
                    with mock.patch.dict(os.environ, self.environment, clear=True):
                        config = eligibility.read_config(self.config_path)
                        request = offload.OffloadRequest(self.repository, "hana", "call-17",
                            offload.NO_RESULT_FILE, words, owner="verify", holder_session="fixture", verb=verb)
                        remote = offload.remote_test_command(config, request, HANA)
                        claim = offload.claim_arguments(request, config)
                    self.assertEqual(claim[claim.index("--what") + 1], f"{verb} hana")
                sent, _ = split_mac_run_command(remote)
                position = sent.index("~/.claude/scripts/lint/lint")
                self.assertEqual(sent[position + 1:], [verb, *words])
                if verb == "doc":
                    self.assertIn("LINT_CONFIG_FORCE=1", sent[:position])

    def test_no_package_names_the_mac_claim_for_the_workspace(self) -> None:
        self.write_portable_workspace()
        result = self.command("--workspace", verb="check", package="")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.events_for("rsync")[-1].get("what"), "check workspace")

    def test_exit_four_is_no_tests_only_for_nextest(self) -> None:
        self.write_portable_workspace()
        self.fixture["run_status_line"] = "mac_exit=4"
        self.sync_fixture()
        for verb in ("check", "clippy", "mend-check", "doc", "nextest"):
            with self.subTest(verb=verb):
                if verb == "check":
                    result = self.command("--workspace", verb=verb)
                    status, output = result.returncode, result.stdout
                else:
                    controller = offload.ProcessController()
                    request = offload.OffloadRequest(self.repository, "hana", "call-17",
                        offload.ResultFile(self.result_path), ("--workspace",),
                        owner="verify", holder_session="fixture", verb=verb)
                    with (
                        mock.patch.dict(os.environ, self.environment, clear=True),
                        mock.patch.object(offload, "probe_mac", return_value=offload.ProbeSnapshot(120, "Mac")),
                        mock.patch.object(offload, "copy_tree_with_key", return_value=offload.CopiedTree(offload.UNAVAILABLE_TREE_KEY)),
                        mock.patch.object(copies, "register_remote"),
                        mock.patch.object(offload, "release_claim"),
                        mock.patch.object(controller, "stream_mac_run", return_value=offload.ReportedMacStatus(4, offload.CleanupKept())),
                        redirect_stdout(io.StringIO()) as captured,
                    ):
                        config = eligibility.read_config(self.config_path)
                        outcome = offload.claimed_run(controller, config, request, HANA, time.monotonic())
                        status = offload.write_result(outcome, request.result, verb=verb)
                    output = captured.getvalue()
                if verb == "nextest":
                    self.assertEqual(status, 75, output)
                    self.assertIn("mac_test: no test matched on the Mac; running on natedev instead", output)
                    self.assert_wire("declined", "no_tests")
                else:
                    self.assertEqual(status, 4, output)
                    self.assertIn("fix the finding in the local worktree", output)
                    self.assertNotIn("only a test", output)
                    self.assert_wire("failed", "")

    def test_non_test_verbs_never_apply_nextest_skip_words_or_start_a_copy(self) -> None:
        self.write_portable_workspace()
        self.config["mac_skip.hana"] = "test(skip-this)"
        self.sync_config()
        for verb in ("check", "clippy", "mend-check", "doc"):
            with self.subTest(verb=verb):
                if verb == "check":
                    result = self.command("-E", "leave-this-argument", verb=verb)
                    self.assertEqual(result.returncode, 0, result.stdout)
                    remote = self.events_for("ssh")[-1]["args"][-1]
                    self.assertFalse(self.events_for("rsync")[-1].get("copy_claim"))
                else:
                    with mock.patch.dict(os.environ, self.environment, clear=True):
                        config = eligibility.read_config(self.config_path)
                        request = offload.OffloadRequest(self.repository, "hana", "call-17",
                            offload.NO_RESULT_FILE, ("-E", "leave-this-argument"),
                            owner="verify", holder_session="fixture", verb=verb)
                        remote = offload.remote_test_command(config, request, HANA)
                        claim = offload.claim_arguments(request, config)
                    self.assertNotIn("--copy", claim)
                self.assertIn("-E leave-this-argument", remote)
                self.assertNotIn("skip-this", remote)
                for option in ("copy", "filter"):
                    with self.subTest(option=option):
                        result = invoke_cli(offload, offload.main, ["run", "--repo-root", str(self.repository),
                            "--call-id", "call-17", "--verb", verb,
                            "--copy" if option == "copy" else "--filter-run"], self.environment, self.root,
                            environment_lock=caller_admission(self, self.root, self.environment).loop_access)
                        self.assertEqual(result.returncode, 2, result.stdout)

    def test_linux_only_workspace_member_declines_a_workspace_build_before_claim(self) -> None:
        self.write_portable_workspace()
        self.config["linux_only.hana"] = "portable"
        self.sync_config()
        for verb in ("build",):
            with self.subTest(verb=verb):
                self.assert_declined(self.command("--workspace", verb=verb, package=""), "linux_only")
        self.assertEqual(self.events_for("ssh"), [])
        self.assertEqual(self.events_for("rsync"), [])

    def test_named_package_is_checked_alone_and_compile_verbs_ignore_linux_only(self) -> None:
        # linux_only keeps the named package's tests and builds off the Mac; a package that depends on it still builds.
        # check, lint and doc only compile, and only the Mac compiles macOS code, so linux_only never applies to them.
        self.write_portable_workspace()
        _ = (self.repository / "Cargo.toml").write_text('[workspace]\nmembers=["portable", "linux-part"]\n')
        member = self.repository / "linux-part"
        member.mkdir(exist_ok=True)
        _ = (member / "Cargo.toml").write_text('[package]\nname="linux-part"\nversion="0.1.0"\n')
        with (self.repository / "portable/Cargo.toml").open("a") as output:
            _ = output.write('[dependencies]\nlinux-part={path="../linux-part"}\n')
        self.config["linux_only.hana"] = "linux-part"
        self.sync_config()
        config = eligibility.read_config(self.config_path)
        compile_verbs: tuple[eligibility.MacVerb, ...] = ("check", "clippy", "mend-check", "doc")
        for verb in (*compile_verbs, "nextest", "build"):
            with self.subTest(verb=verb):
                self.assertIsInstance(eligibility.mac_step_eligibility(config, self.repository, "portable", verb),
                                      eligibility.KnownRepository)
                for package in ("linux-part", ""):
                    policy = eligibility.mac_step_eligibility(config, self.repository, package, verb)
                    if verb in compile_verbs:
                        self.assertIsInstance(policy, eligibility.KnownRepository)
                    else:
                        self.assertEqual(policy, eligibility.IneligibleMacStep("linux_only"))
                if verb not in compile_verbs:
                    self.assert_declined(self.command(verb=verb, package="linux-part"), "linux_only")
                    self.assert_declined(self.command("--workspace", verb=verb, package=""), "linux_only")
        self.assertEqual(self.events_for("ssh"), [])
        self.assertEqual(self.events_for("rsync"), [])
        self.assertEqual(eligibility.mac_step_eligibility(config, self.repository, "linux-part", "clippy", kind="example"),
                         eligibility.IneligibleMacStep("linux_only"))
        result = self.command(verb="clippy", package="linux-part")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(len(self.events_for("rsync")), 1)
        _ = self.config.pop("linux_only.hana")
        self.sync_config()
        self.assertEqual(eligibility.mac_step_eligibility(eligibility.read_config(self.config_path), self.repository,
                                                          "linux-part", "clippy"),
                         eligibility.IneligibleMacStep("repo"))

    def test_workspace_test_with_a_linux_only_member_declines_before_claim(self) -> None:
        self.write_portable_workspace()
        self.config["linux_only.hana"] = "portable"
        self.sync_config()
        self.assert_declined(self.command("--workspace", package=""), "linux_only")
        self.assertEqual(self.events_for("ssh"), [])
        self.assertEqual(self.events_for("rsync"), [])

    def test_implicit_linux_member_blocks_workspace_verbs_through_target_and_shared_dependencies(self) -> None:
        for section in ("dependencies", "dev-dependencies", "build-dependencies", 'target.\'cfg(unix)\'.dependencies'):
            with self.subTest(section=section):
                self.write_portable_workspace()
                _ = (self.repository / "Cargo.toml").write_text(
                    '[workspace]\nmembers=["portable"]\n[workspace.dependencies]\nimplicit={path="implicit"}\n')
                member = self.repository / "implicit"
                member.mkdir(exist_ok=True)
                _ = (member / "Cargo.toml").write_text('[package]\nname="linux-part"\nversion="0.1.0"\n')
                with (self.repository / "portable/Cargo.toml").open("a") as output:
                    _ = output.write(f'[{section}]\nimplicit={{workspace=true}}\n')
                self.config["linux_only.hana"] = "linux-part"
                self.sync_config()
                for verb in ("nextest", "build"):
                    self.assert_declined(self.command("--workspace", verb=verb, package=""), "linux_only")
        self.assertEqual(self.events_for("ssh"), [])
        self.assertEqual(self.events_for("rsync"), [])

    def test_unreadable_implicit_membership_refuses_workspace_before_claim(self) -> None:
        self.write_portable_workspace()
        with (self.repository / "portable/Cargo.toml").open("a") as output:
            _ = output.write('[dependencies]\nmissing={path="../missing"}\n')
        for verb in ("nextest", "build"):
            self.assert_declined(self.command("--workspace", verb=verb, package=""), "repo")
        self.assertEqual(self.events_for("ssh"), [])
        self.assertEqual(self.events_for("rsync"), [])

    def test_doc_copies_the_supplied_snapshot_without_working_tree_ignores(self) -> None:
        self.write_portable_workspace()
        snapshot = self.root / "staged-doc"
        snapshot.mkdir()
        _ = (snapshot / "Cargo.toml").write_text('[package]\nname="staged"\nversion="0.1.0"\n')
        _ = (self.repository / ".gitignore").write_text("staged-file.rs\n")
        _ = (snapshot / "staged-file.rs").write_text("staged source")
        git_environment = {name: value for name, value in self.environment.items() if not name.startswith("GIT_")}
        git_environment.update({"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})
        git = ["git", "-C", str(snapshot), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
               "-c", "core.hooksPath=/dev/null"]
        _ = subprocess.run([*git, "init", "-q"], check=True, env=git_environment)
        _ = subprocess.run([*git, "add", "--", "Cargo.toml", "staged-file.rs"], check=True, env=git_environment)
        _ = subprocess.run([*git, "commit", "-qm", "Staged doc source", "--no-gpg-sign"],
                           check=True, env=git_environment)
        snapshot_source = offload.StagedDocSnapshot(snapshot)
        result = self.command("--workspace", verb="doc", source=snapshot_source)
        self.assertEqual(result.returncode, 0, result.stdout)
        copied, = self.events_for("rsync")
        self.assertIn(str(snapshot) + "/", copied["args"])
        self.assertNotIn(str(self.repository) + "/", copied["args"])
        self.assertNotIn("/staged-file.rs", copied.get("exclude", ""))
        self.config["linux_only.hana"] = "staged"
        self.sync_config()
        result = self.command("--workspace", verb="doc", package="", source=snapshot_source)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(len(self.events_for("rsync")), 2)

    def test_remote_test_command_sets_development_deployment_target_before_nextest(self) -> None:
        words = ("--no-fail-fast", "unit_case")
        result = self.command(*words, runner_options=("--run-id", "123:456"))
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assert_wire("passed", "")
        remote = self.events_for("ssh")[-1]["args"][-1]
        remote_words, _ = split_mac_run_command(remote)
        lint_index = remote_words.index("~/.claude/scripts/lint/lint")
        self.assertIn("MACOSX_DEPLOYMENT_TARGET=14.0", remote_words)
        self.assertIn("BUILDLOG_RUN=123:456", remote_words)
        self.assertLess(remote_words.index("MACOSX_DEPLOYMENT_TARGET=14.0"), lint_index)
        self.assertEqual(remote_words[lint_index + 1 :], ["nextest", *words])

    def test_offload_refuses_the_mirror_descriptor_environment_variable(self) -> None:
        name = "COPIES_MIRROR_LEASE_FDS"
        self.environment[name] = ""
        result = self.command(runner_options=("--env", name), separate_stderr=True)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(result.stderr, f"offload: --env {name}: the runner sets it; it is not forwarded.\n")
        self.assertEqual(self.events_for("ssh"), [])

    def test_nextest_and_copy_runs_deliver_forwarded_environment_to_the_remote_command(self) -> None:
        self.environment.update({"NO_COLOR": "1", "TERM": "xterm-256color", "TERM_PROGRAM": "verify"})
        for copy in (False, True):
            with self.subTest(copy=copy):
                result = self.command(copy=copy, runner_options=("--env", "NO_COLOR", "--env", "TERM", "--env", "TERM_PROGRAM"))
                self.assertEqual(result.returncode, 0, result.stdout)
                self.assert_wire("passed", "")
                remote = shlex.split(self.events_for("ssh")[-1]["args"][-1])
                for name in ("NO_COLOR", "TERM", "TERM_PROGRAM"):
                    self.assertIn(name + "=" + self.environment[name], remote)

    def test_mac_nextest_clears_target_values_before_applying_call_values(self) -> None:
        request = offload.OffloadRequest(
            self.repository, "hana", "call", offload.NO_RESULT_FILE, (), owner="verify",
            holder_session="fixture-session", fetch=offload.FetchFolder("output"),
            environment=(("CLICOLOR", "call-value"),))
        with mock.patch.dict(os.environ, self.environment, clear=True):
            config = eligibility.read_config(self.config_path)
            remote = offload.remote_test_command(config, request, eligibility.KnownRepository("hana"))
            mirror = self.root / offload.remote_mirror(eligibility.KnownRepository("hana"))
        mirror.mkdir(parents=True, exist_ok=True)
        _ = install_nextest_log_writer(self.root)
        lint = self.root / ".claude/scripts/lint/lint"
        lint.parent.mkdir(parents=True, exist_ok=True)
        capture = self.root / "received-environment.json"
        self.write_executable(lint, "import json, os\nfrom pathlib import Path\n"
                              + f"Path({str(capture)!r}).write_text(json.dumps(dict(os.environ)))\n")
        for shell in ("bash", "zsh"):
            with self.subTest(shell=shell):
                result = subprocess.run([shell, "-c", remote], cwd=self.root, env={
                    **self.environment, "HOME": str(self.root), "PATH": os.environ["PATH"],
                    "NO_COLOR": "target-value", "HANA_TARGET_ONLY": "target-value",
                    "CLICOLOR": "target-value", "LC_TERMINAL": "target-terminal",
                }, capture_output=True, text=True, check=False, timeout=30)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                observed = cast(dict[str, str], json.loads(capture.read_text()))
                self.assertNotIn("NO_COLOR", observed)
                self.assertNotIn("HANA_TARGET_ONLY", observed)
                self.assertEqual(observed["CLICOLOR"], "call-value")
                self.assertEqual(observed["LC_TERMINAL"], "target-terminal")

    def test_mac_cleanup_writes_only_markers_after_the_verdict(self) -> None:
        request = offload.OffloadRequest(
            self.repository, "hana", "call", offload.NO_RESULT_FILE, (), owner="verify", holder_session="fixture-session")
        repository = eligibility.KnownRepository("hana")
        with mock.patch.dict(os.environ, self.environment, clear=True):
            config = eligibility.read_config(self.config_path)
            cleanup = offload.remote_cleanup_command(config, request, repository)
            mirror = self.root / offload.remote_mirror(repository, request.target, request.slot)
        # Linux df has no -g option and a missing mirror fails cd; the cleanup hides both error lines.
        for present in (False, True):
            with self.subTest(mirror_present=present):
                if present:
                    mirror.mkdir(parents=True)
                result = subprocess.run(["bash", "-c", "echo mac_exit=42; " + cleanup], cwd=self.root,
                                        env={**self.environment, "HOME": str(self.root), "PATH": os.environ["PATH"]},
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, check=False,
                                        timeout=30)
                verdict, *after = result.stdout.splitlines()
                self.assertEqual(verdict, "mac_exit=42")
                if present:
                    self.assertTrue(after and after[0].startswith("mac_trim="), result.stdout)
                    self.assertTrue(all(line.startswith(("mac_trim=", "mac_cleanup=")) for line in after), result.stdout)
                else:
                    self.assertEqual(after, ["mac_cleanup=1"])

    def test_probe_copy_and_run_arguments_preserve_nextest_words(self) -> None:
        _ = (self.repository / ".gitignore").write_text(
            "generated/\n", encoding="utf-8"
        )
        generated = self.repository / "generated"
        generated.mkdir()
        _ = (generated / "artifact").write_text("ignored\n", encoding="utf-8")
        words = ("-E", FILTER, "--no-fail-fast", "unit_case")
        result = self.command(*words, filter_run=True)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assert_wire("passed", "")
        self.assertFalse(self.claim_file.exists())
        self.assert_claimed_commands()

        events = self.events()
        self.assertEqual([event["command"] for event in events], ["ssh", "rsync", "ssh"])
        ssh_arguments = [
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=6",
            "-o",
            "ServerAliveInterval=15",
            "-o",
            "ServerAliveCountMax=3",
            "mac",
        ]
        probe = events[0]["args"]
        self.assertEqual(probe[: len(ssh_arguments)], ssh_arguments)
        probe_script = probe[len(ssh_arguments)]
        for fragment in (
            ".local/state/mac-test/test-mirror/hana",
            'df -g "$HOME"',
            "hostname -s",
        ):
            self.assertIn(fragment, probe_script)
        self.assertNotIn("loadavg", probe_script)
        self.assertNotIn("pgrep", probe_script)
        self.assertNotIn("procs", probe_script)
        self.assertNotIn("pmset", probe_script)

        copy = events[1]
        exclude_argument = next(
            argument
            for argument in copy["args"]
            if argument.startswith("--exclude-from=")
        )
        session = (self.environment.get("CLAUDE_CODE_SESSION_ID")
                   or self.environment.get("CODEX_THREAD_ID") or f"pid-{os.getpid()}")
        receiver_argument = (
            "--rsync-path=~/.claude/scripts/lib/py ~/.claude/scripts/copies/copies.py mirror-receive "
            + shlex.join(["--path", ".local/state/mac-test/test-mirror/hana", "--owner", "verify",
                          "--repository", "hana", "--session", session, "--", "rsync"])
        )
        self.assertEqual(
            copy["args"],
            [
                "-rlpc",
                "--stats",
                "--compress",
                "--delete",
                receiver_argument,
                "--exclude=/.mac-test-mirror",
                "--exclude=/.copy-record.json",
                "--exclude=/.copy-session",
                exclude_argument,
                "-e",
                "ssh -o BatchMode=yes -o ConnectTimeout=6 "
                + "-o ServerAliveInterval=15 -o ServerAliveCountMax=3",
                f"{self.repository}/",
                "mac:.local/state/mac-test/test-mirror/hana/",
            ],
        )
        self.assertEqual(copy.get("exclude"), "/.git\n/target\n/generated/\n")

        run = events[2]["args"]
        self.assertEqual(run[: len(ssh_arguments)], ssh_arguments)
        remote = run[-1]
        remote_words, cleanup = split_mac_run_command(remote)
        self.assertIn("mac_cleanup=", cleanup)
        lint_index = remote_words.index("~/.claude/scripts/lint/lint")
        self.assertEqual(
            remote_words[lint_index :],
            ["~/.claude/scripts/lint/lint", "nextest", *words],
        )
        self.assertIn("MACOSX_DEPLOYMENT_TARGET=14.0", remote_words)
        self.assertLess(remote_words.index("MACOSX_DEPLOYMENT_TARGET=14.0"), lint_index)
        self.assertIn("PATH=$HOME/.claude/scripts/mac_test/cargo_gate/bin:$HOME/.cargo/bin:$PATH", remote_words)
        self.assertIn("BUILDLOG_CALLER=verify-mac", remote_words)
        self.assertIn("BUILDLOG_CALL_ID=call-17", remote_words)
        self.assertIn("BUILDLOG_SYNC=1", remote_words)
        self.assertIn("LINT_SWEEP_BUDGET_GIB=64", remote_words)
        self.assertIn(shlex.quote(FILTER), remote)

        for stats, sent, received, complete in (
            ("Total bytes sent: 1,234\nTotal bytes received: 56\n", 1234, 56, True),
            ("sent 1,234 bytes  received 56 bytes  1.0 bytes/sec\n", 1234, 56, True),
            ("", 0, 0, False),
        ):
            with self.subTest(stats=stats):
                ssh = self.events_for("ssh")[-1]
                self.assertIn("claim", ssh)
                assert "claim" in ssh
                claim = ssh["claim"]
                identity = claim_state.ClaimIdentity(claim["claim_id"], claim["episode"], claim["attempt"])
                marker = "dispatch_start_settled=" + offload.pending_start_key(identity) + "\n"
                controller = offload.ProcessController()
                controller.claim_identity = identity
                wire_output = "résultat\nanomaly\nmac_exit=0\nmac_cleanup=0 32715571 59\n" + marker
                copy_process = mock.Mock(communicate=mock.Mock(return_value=(stats.encode(), b"")), returncode=0)
                run_process = mock.Mock(stdout=io.BytesIO(wire_output.encode("utf-8")),
                                        wait=mock.Mock(return_value=0), poll=mock.Mock(return_value=0))
                request = offload.OffloadRequest(self.repository, "hana", "byte-record",
                    offload.NO_RESULT_FILE, words, owner="verify", holder_session="fixture", target="mac")
                with (
                    mock.patch.dict(os.environ, self.environment, clear=True),
                    mock.patch.object(subprocess, "Popen", side_effect=[copy_process, run_process]),
                    redirect_stdout(io.StringIO()) as captured,
                ):
                    controller.observe_command(["ssh", *probe], b"free_gib=120\nhost=Mac\nrc=0\n", b"")
                    _ = controller.capture_copy(["rsync", *copy["args"]])
                    outcome = controller.stream_mac_run(["ssh", *ssh["args"]])
                    self.assertIsInstance(outcome, offload.ReportedMacStatus)
                    if not isinstance(outcome, offload.ReportedMacStatus):
                        self.fail("The runner did not send a completion status")
                    self.assertEqual(outcome.status, 0, captured.getvalue())
                with mock.patch.dict(os.environ, self.environment, clear=True):
                    offload.persist_attempt(request, offload.PassedOnMac(0), controller.attempt_evidence, time.monotonic())
                rows = [cast(dict[str, object], json.loads(line))
                        for path in (self.root / "buildlog").glob("*/*.jsonl")
                        for line in path.read_text().splitlines()]
                attempt = [row for row in rows if row.get("kind") == "ssh_attempt"
                           and row.get("call_id") == "byte-record"][-1]
                transfers = cast(list[buildlog_sync.TransferBytes], attempt["transfers"])
                self.assertEqual([entry["operation"] for entry in transfers],
                                 ["ssh_control", "rsync_source", "ssh_run"])
                self.assertEqual(transfers[1], {"operation": "rsync_source", "sent_bytes": sent,
                                               "received_bytes": received, "complete": complete})
                self.assertEqual(transfers[2]["received_bytes"],
                    len(("résultat\nanomaly\nmac_exit=0\nmac_cleanup=0 32715571 59\n" + marker).encode("utf-8")))
                self.assertEqual(attempt["sent_bytes"], sum(entry["sent_bytes"] for entry in transfers))
                self.assertEqual(attempt["received_bytes"], sum(entry["received_bytes"] for entry in transfers))
                self.assertIn("résultat\nanomaly\n", captured.getvalue())
                self.assertNotIn("Total bytes sent", captured.getvalue())

    def test_non_ascii_ignored_directory_is_in_the_exclude_file(self) -> None:
        _ = (self.repository / ".gitignore").write_text(
            "café/\n", encoding="utf-8"
        )
        ignored_directory = self.repository / "café"
        ignored_directory.mkdir()
        _ = (ignored_directory / "artifact").write_text(
            "ignored\n", encoding="utf-8"
        )
        result = self.command()
        self.assertEqual(result.returncode, 0, result.stdout)
        copy = self.events_for("rsync")[0]
        self.assertIn("/café/\n", copy.get("exclude", ""))

    def test_repository_skip_is_added_to_filter(self) -> None:
        self.config["mac_skip.hana"] = "test(slow) | test(gpu)"
        self.sync_config()
        result = self.command("-E", FILTER, filter_run=True)
        self.assertEqual(result.returncode, 0, result.stdout)
        remote = self.events_for("ssh")[-1]["args"][-1]
        remote_words, cleanup = split_mac_run_command(remote)
        self.assertIn("mac_cleanup=", cleanup)
        self.assertIn(f"({FILTER}) & not (test(slow) | test(gpu))", remote_words)
        self.assertNotIn(FILTER, remote_words)

    def test_unwritable_result_destination_returns_internal_error(self) -> None:
        self.config["offload"] = "off"
        self.sync_config()
        self.result_path: Path = self.root / "missing" / "result"
        result = self.command()
        self.assertEqual(result.returncode, 75, result.stdout)
        self.assertIn("mac_test: not running on the Mac (off)\n", result.stdout)
        self.assertIn("FileNotFoundError", result.stdout)
        self.assertFalse(self.result_path.exists())



class MirrorExcludeTests(unittest.TestCase):
    def test_a_symlinked_target_leaves_the_mirror_target_folder_alone(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, cache, mirror = root / "source", root / "cache", root / "mirror"
            for folder in (source, cache, mirror / "target"):
                folder.mkdir(parents=True)
            _ = (source / "lib.rs").write_text("fn main() {}\n", encoding="utf-8")
            _ = (cache / "gate.o").write_text("gate\n", encoding="utf-8")
            (source / "target").symlink_to(cache)
            _ = (mirror / "target" / "mac.o").write_text("mac\n", encoding="utf-8")
            excludes = root / "excludes"
            _ = excludes.write_text(offload.MIRROR_EXCLUDES, encoding="utf-8")
            result = subprocess.run(["rsync", "-rlpc", "--delete", f"--exclude-from={excludes}",
                                     f"{source}/", f"{mirror}/"], capture_output=True, text=True,
                                    check=False, timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((mirror / "target").is_symlink())
            self.assertEqual((mirror / "target" / "mac.o").read_text(encoding="utf-8"), "mac\n")
            self.assertEqual((mirror / "lib.rs").read_text(encoding="utf-8"), "fn main() {}\n")


class BackoffReadTests(unittest.TestCase):
    def test_strict_read_names_an_unreadable_backoff_and_the_default_read_ignores_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "unreachable_until"
            with mock.patch.object(eligibility, "backoff_path", return_value=path):
                self.assertIs(eligibility.read_backoff(100, strict=True), eligibility.NO_BACKOFF)
                for text, reason in (("soon", "could not convert string to float: 'soon'"), ("inf", "backoff time is not finite")):
                    with self.subTest(text=text):
                        _ = path.write_text(text, encoding="utf-8")
                        self.assertEqual(eligibility.read_backoff(100, strict=True), eligibility.UnreadableBackoff(reason))
                        self.assertIs(eligibility.read_backoff(100), eligibility.NO_BACKOFF)
                _ = path.write_text("200", encoding="utf-8")
                self.assertIs(eligibility.read_backoff(100, strict=True), eligibility.ACTIVE_BACKOFF)

    def test_a_passed_backoff_decides_eligibility_without_a_read(self) -> None:
        config = eligibility.OffloadConfig(True, eligibility.SshConnectionSettings("dummy", 1, 1, UNREACHABLE_WAIT_FIXTURE_SECONDS), 1, 1, 1, "1",
                                       {"linux_only.hana": ""})
        root = Path("/fixture/hana")
        key = "https://fixture.invalid/hana"
        mirror = claim_state.ResolvedMacMirror(key, "hana", "hana.json", "hana", str(root))
        cases: tuple[tuple[eligibility.BackoffState, eligibility.KnownRepository | eligibility.IneligibleMacStep], ...] = (
            (eligibility.UnreadableBackoff("bad"), eligibility.IneligibleMacStep("state")),
            (eligibility.ACTIVE_BACKOFF, eligibility.IneligibleMacStep("backoff")),
            (eligibility.NO_BACKOFF, eligibility.KnownRepository("hana", key)),
        )
        with mock.patch.object(eligibility, "read_backoff", side_effect=AssertionError("backoff was passed")):
            for backoff, expected in cases:
                with self.subTest(backoff=type(backoff).__name__):
                    self.assertEqual(eligibility.mac_step_eligibility(config, root, "hana", "nextest",
                                                                  backoff=backoff, mac_repository=mirror), expected)

if __name__ == "__main__":
    _ = unittest.main()
