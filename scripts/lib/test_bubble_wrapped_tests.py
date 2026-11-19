"""Check Bubblewrap from inside a gate job."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import ExitStack, suppress
import errno
import fcntl
import json
import os
from pathlib import Path
import runpy
import select
import signal
import socket
import subprocess
import sys
import tempfile
import time
from typing import cast, override
import unittest
from unittest import mock
from uuid import uuid4

from scripts.lib import conf, bubble_wrapped_tests
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.production import merge_gate

CHECKOUT = Path(__file__).resolve().parents[2]
CONFIG = CHECKOUT / "config/bubble_wrapped_tests.conf"
MODULE_RUNNER = CHECKOUT / "scripts/test_times/module_run.py"
HOOK = CHECKOUT / "scripts/hooks/pre-tool-use-build-hold-guard.py"
SHIM = CHECKOUT / "scripts/lib/py"
PUBLIC_ADDRESS = ("1.1.1.1", 443)
METADATA_ADDRESS = ("169.254.169.254", 80)
CALLER_VARIABLES = ("BASH_ENV", "GIT_DIR", "GIT_INDEX_FILE", "COPIES_RUN_PID")
AGENT_SOCKET_VARIABLES = ("SSH_AUTH_SOCK", "CLAUDE_CODE_MESSAGING_SOCKET", "DBUS_SESSION_BUS_ADDRESS")
SOCKET_TIMEOUT = 3.0
AUDITED_TESTS = (
    "scripts.aws_target.test_gpu_size_trial.GpuOnlineTests"
    + ".test_cancel_stops_the_assigned_unit_and_terminates_the_box",
    "scripts.build_queue.test_hana_placement.HanaGpuQueueTests"
    + ".test_pinned_route_reports_settled_refusal_without_local_fallback",
    "scripts.lint.test_memory_gate_wait.MemoryGateWaitTests"
    + ".test_nested_step_refuses_before_memory_or_cargo_token_wait",
    "scripts.mac_test.test_fix_return.RollForwardTests.test_stop_while_staging_leaves_the_worktree_unchanged",
    "scripts.direct.test_progress_history.ProgressHistoryTests"
    + ".test_capacity_waits_keep_session_and_unit_holders_in_observation_order",
    "scripts.direct.test_runner_result.RunnerResultTests"
    + ".test_shell_reports_helper_faults_but_keeps_unusable_results_quiet",
    "scripts.buildlog.test_record.RecordTests.test_step_record_fields",
)


def parent_pid(pid: int) -> int:
    data = Path(f"/proc/{pid}/stat").read_text()
    return int(data[data.rindex(")") + 2:].split()[1])


class TestBubblewrapProof(unittest.TestCase):
    job: Path = Path()

    @override
    def setUp(self) -> None:
        self.assertTrue(bubble_wrapped_tests.guarded(), "The test is outside Bubblewrap.")
        self.job = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="guard-proof-")))

    def denied_create(self, path: Path, allowed_errors: tuple[int, ...] = (errno.EROFS, errno.ENOENT)) -> None:
        try:
            with path.open("x"):
                pass
        except OSError as error:
            self.assertIn(error.errno, allowed_errors, str(error))
        else:
            path.unlink()
            self.fail(f"The test could create {path}.")

    def test_live_roots_and_git_state_refuse_writes(self) -> None:
        roots = conf.read(CONFIG).words("live_roots")
        self.assertTrue(roots)
        for value in roots:
            # The guard replaces HOME. Expand the configured natedev home here.
            root = Path(value.replace("~/", "/home/natepiano/", 1) if value.startswith("~/") else value)
            with self.subTest(root=root):
                self.assertTrue(root.is_absolute())
                name = f"guard-proof-{uuid4().hex}"
                self.denied_create(root / name)
                link = self.job / "live-root"
                link.symlink_to(root, target_is_directory=True)
                try:
                    self.denied_create(link / name)
                finally:
                    link.unlink()

        self.assertTrue(os.statvfs(CHECKOUT).f_flag & os.ST_RDONLY)
        lock = CHECKOUT / ".git/guard.lock"
        self.denied_create(lock, (errno.EROFS, errno.ENOENT, errno.ENOTDIR))
        git_pointer = CHECKOUT / ".git"
        if git_pointer.is_file():
            git_dir = CHECKOUT / git_pointer.read_text().strip().removeprefix("gitdir: ")
            self.denied_create(git_dir / lock.name)

    def test_job_folder_accepts_writes(self) -> None:
        marker = self.job / "write"
        _ = marker.write_text("Bubblewrap test\n")
        self.assertEqual(marker.read_text(), "Bubblewrap test\n")

    def test_caller_environment_is_absent(self) -> None:
        for name in (*CALLER_VARIABLES, *AGENT_SOCKET_VARIABLES):
            with self.subTest(name=name):
                self.assertNotIn(name, os.environ)

    def run_command(self, args: list[str], *, cwd: Path = CHECKOUT,
                    environment: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(args, cwd=cwd, env=environment, capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, timeout=TEST_HANG_GUARD_SECS, check=False)

    def assert_passed(self, result: subprocess.CompletedProcess[str]) -> None:
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def guarded_command(self, argv: list[str], environment: dict[str, str]) -> list[str]:
        # Build a second launch to test the boundary from this guarded module.
        with mock.patch.object(bubble_wrapped_tests, "guarded", return_value=False), \
                mock.patch.dict(os.environ, environment, clear=True):
            return bubble_wrapped_tests.command(argv, self.job)

    def test_shell_git_and_type_jobs_discard_caller_traps(self) -> None:
        repo = self.job / "repo"
        repo.mkdir()
        tracked = repo / "tracked"
        _ = tracked.write_text("tracked\n")
        self.assert_passed(self.run_command(["git", "init", "-q"], cwd=repo))
        self.assert_passed(self.run_command(["git", "add", "--", tracked.name], cwd=repo))
        startup = self.job / "bash-startup"
        _ = startup.write_text("exit 97\n")
        index = self.job / "caller-index"
        _ = index.write_text("invalid caller index\n")
        environment = {
            **os.environ,
            "BASH_ENV": str(startup),
            "GIT_DIR": str(self.job / "caller-git"),
            "GIT_INDEX_FILE": str(index),
            "COPIES_RUN_PID": str(os.getpid()),
        }
        cases = (
            ("Verify shell harnesses", ["bash", "-c", "printf '%s' reached"], "reached"),
            ("Git setup in buildlog/test_record.py",
             ["git", "-C", str(repo), "ls-files", "--error-unmatch", tracked.name], "tracked\n"),
            ("copies caller PID", [sys.executable, "-I", "-c",
                                  "import os; assert 'COPIES_RUN_PID' not in os.environ"], ""),
            ("type job shim", [str(SHIM), "-c", "print('reached')"], "reached\n"),
        )
        for name, args, output in cases:
            with self.subTest(name=name):
                command = self.guarded_command(args, environment)
                result = self.run_command(command, environment=environment)
                self.assert_passed(result)
                self.assertEqual(result.stdout, output)
        proof = self.guarded_command([str(SHIM), "-m", "unittest", "-v", *AUDITED_TESTS], environment)
        audited = self.run_command(proof, environment=environment)
        self.assert_passed(audited)
        self.assertIn(f"Ran {len(AUDITED_TESTS)} tests", audited.stderr)
        self.assertEqual(index.read_text(), "invalid caller index\n")

    def test_all_processes_descend_from_bwrap(self) -> None:
        self.assertEqual(Path("/proc/1/comm").read_text().strip(), "bwrap")
        for entry in Path("/proc").iterdir():
            if not entry.name.isdecimal():
                continue
            pid = int(entry.name)
            seen: set[int] = set()
            while pid != 1:
                self.assertGreater(pid, 1, str(entry))
                self.assertNotIn(pid, seen, str(entry))
                seen.add(pid)
                try:
                    pid = parent_pid(pid)
                except FileNotFoundError:
                    break

    def test_outside_addresses_and_sockets_are_unavailable(self) -> None:
        for address in (PUBLIC_ADDRESS, METADATA_ADDRESS):
            with self.subTest(address=address):
                with socket.socket() as client:
                    client.settimeout(SOCKET_TIMEOUT)
                    self.assertIn(client.connect_ex(address), (errno.ENETUNREACH, errno.EHOSTUNREACH))

        for path in (Path(f"/run/user/{os.getuid()}/bus"), Path("/run/dbus/system_bus_socket"),
                     Path("/run/user/1000/bus"), Path("/dev/steve")):
            with self.subTest(path=path):
                self.assertFalse(os.path.lexists(path), str(path))
        for pattern in ("/tmp/claude*/*.sock", "/tmp/claude*/*/*.sock",
                        "/home/natepiano/.config/herdr/*/herdr.sock",
                        "/home/natepiano/.config/herdr/herdr.sock",
                        "/home/natepiano/.codex/*.sock"):
            with self.subTest(pattern=pattern):
                self.assertFalse(list(Path("/").glob(pattern.removeprefix("/"))))

    def test_private_loopback_accepts_connections(self) -> None:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            listener.settimeout(SOCKET_TIMEOUT)
            address = cast(tuple[str, int], listener.getsockname())
            with socket.create_connection(address, timeout=SOCKET_TIMEOUT) as client:
                with listener.accept()[0] as server:
                    server.settimeout(SOCKET_TIMEOUT)
                    client.sendall(b"1")
                    self.assertEqual(server.recv(1), b"1")

    def test_external_commands_fail(self) -> None:
        environment = {**os.environ, "LC_ALL": "C", "GIT_CONFIG_NOSYSTEM": "1",
                       "GIT_CONFIG_GLOBAL": os.devnull}
        self.assert_passed(self.run_command(["git", "init", "-q"], cwd=self.job, environment=environment))
        self.assert_passed(self.run_command(
            ["git", "-c", "user.name=Guard Proof", "-c", "user.email=guard@example.invalid",
             "commit", "--allow-empty", "--no-gpg-sign", "-qm", "Fixture"],
            cwd=self.job, environment=environment))
        destination = f"git://{PUBLIC_ADDRESS[0]}:{PUBLIC_ADDRESS[1]}/guard-proof.git"
        push = self.run_command(["git", "push", "--dry-run", "--no-verify", destination,
                                 "HEAD:refs/heads/guard-proof"], cwd=self.job, environment=environment)
        self.assertNotEqual(push.returncode, 0, push.stdout + push.stderr)
        self.assertIn("unreachable", push.stderr)

        manager = self.run_command(["systemctl", "--user", "list-units", "--no-pager"],
                                   environment=environment)
        self.assertNotEqual(manager.returncode, 0, manager.stdout + manager.stderr)
        self.assertIn("Failed to connect to", manager.stderr)

        address = f"uds:/session/test-guard-proof-{uuid4().hex}.sock"
        sender = self.run_command([sys.executable, str(CHECKOUT / "scripts/message/send.py"),
                                   "--here", "--to", address, "--from", "guard proof", "--text", "proof"],
                                  environment=environment)
        self.assertNotEqual(sender.returncode, 0, sender.stdout + sender.stderr)

    def test_missing_bubblewrap_refuses_the_runner_before_the_module(self) -> None:
        executable = Path(conf.read(CONFIG).text("bubblewrap", recover_invalid=False))
        self.assertEqual(executable, bubble_wrapped_tests.SYSTEM / "bin/bwrap")
        is_file = Path.is_file

        def installed(path: Path) -> bool:
            return path != executable and is_file(path)

        marker = self.job / "started"
        module = self.job / "test_bubblewrap_refusal_fixture.py"
        _ = module.write_text(f"from pathlib import Path\nPath({str(marker)!r}).touch()\n")
        argv = [str(MODULE_RUNNER), str(self.job), module.name]
        with mock.patch.object(Path, "is_file", autospec=True, side_effect=installed) as existence, \
                mock.patch.object(sys, "path", sys.path.copy()), \
                mock.patch.object(bubble_wrapped_tests, "guarded", return_value=False), \
                mock.patch.dict(os.environ, {"MERGE_GATE_PARENT_PID": "1"}, clear=True), \
                mock.patch.object(sys, "argv", argv), \
                mock.patch.object(sys, "orig_argv", [sys.executable, *argv]):
            with self.assertRaisesRegex(bubble_wrapped_tests.BubblewrapUnavailable, "Bubblewrap is unavailable"):
                bubble_wrapped_tests.enter([sys.executable, *argv])
            existence.assert_any_call(executable)
            existence.reset_mock()
            with self.assertRaisesRegex(SystemExit, "Bubblewrap is unavailable"):
                _ = runpy.run_path(str(MODULE_RUNNER), run_name="__main__")
            existence.assert_any_call(executable)
        self.assertFalse(marker.exists())

    def test_launcher_refuses_without_bubblewrap(self) -> None:
        with mock.patch.object(bubble_wrapped_tests, "guarded", return_value=False), \
                mock.patch.object(os, "execvpe", side_effect=AssertionError("The command must not start.")):
            with self.assertRaisesRegex(SystemExit, "the test started outside Bubblewrap"):
                bubble_wrapped_tests.main(["--inside", sys.executable, "-I", "-c", "raise SystemExit(0)"])

    def test_gate_stop_ends_the_namespace_during_startup_and_after_session_setup(self) -> None:
        for during_startup in (True, False):
            with self.subTest(during_startup=during_startup):
                self.check_gate_stop(during_startup)

    def check_gate_stop(self, during_startup: bool) -> None:
        startup = self.job / "sitecustomize.py"
        _ = startup.write_text("raise SystemExit('untrusted startup ran')\n")
        environment = {**os.environ, "PYTHONPATH": str(self.job)}

        def end_leftover_namespace(descriptor: int) -> None:
            if select.select([descriptor], [], [], 0)[0]:
                return
            with suppress(ProcessLookupError):
                signal.pidfd_send_signal(descriptor, signal.SIGKILL)
            self.assertTrue(select.select([descriptor], [], [], TEST_HANG_GUARD_SECS)[0])

        with ExitStack() as cleanup:
            folder = Path(cleanup.enter_context(tempfile.TemporaryDirectory(prefix="launch-", dir=self.job)))
            marker = folder / "command-started"
            lock_path = folder.with_name(folder.name + ".lock")
            _ = cleanup.callback(lock_path.unlink, missing_ok=True)
            lock = cleanup.enter_context(lock_path.open("w"))
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            lease = lock.fileno()
            block_read, block_write = os.pipe()
            for fd in (block_read, block_write):
                _ = cleanup.callback(os.close, fd)
            info_read, info_write = os.pipe()
            status = cleanup.enter_context(os.fdopen(info_read, "rb"))
            ready_read, ready_write = os.pipe()
            _ = cleanup.callback(os.close, ready_read)
            child = """import os, select, sys
open(sys.argv[1], "x").close()
os.write(int(sys.argv[2]), b"1")
select.select([], [], [])
"""
            with os.fdopen(info_write, "wb"), os.fdopen(ready_write, "wb"):
                with (mock.patch.object(bubble_wrapped_tests, "guarded", return_value=False),
                      bubble_wrapped_tests.gate_inputs(CHECKOUT, environment, read_only=())):
                    argv = bubble_wrapped_tests.command(
                        [sys.executable, "-I", "-c", child, str(marker), str(ready_write)], folder, lease_fd=lease,
                    )
                argv[1:1] = ["--info-fd", str(info_write), "--block-fd", str(block_read)]
                job = merge_gate.start_gate_job(
                    "lib", Path(__file__), argv, CHECKOUT, environment, folder / "gate.log",
                    pass_fds=(lease, info_write, block_read, ready_write),
                )
            _ = cleanup.callback(job.output.close)
            descriptor = None
            try:
                ready, _, _ = select.select([info_read], [], [], TEST_HANG_GUARD_SECS)
                self.assertTrue(ready, job.log.read_text())
                info = cast(dict[str, int], json.load(status))
                pid = info["child-pid"]
                descriptor = os.pidfd_open(pid)
                _ = cleanup.callback(os.close, descriptor)
                _ = cleanup.callback(end_leftover_namespace, descriptor)
                self.assertFalse(marker.exists())
                if not during_startup:
                    _ = os.write(block_write, b"1")
                    ready, _, _ = select.select([ready_read, descriptor], [], [], TEST_HANG_GUARD_SECS)
                    self.assertIn(ready_read, ready, job.log.read_text())
                    self.assertEqual(os.read(ready_read, 1), b"1")
                    self.assertTrue(marker.exists())
                self.assertEqual(os.getpgid(pid), job.process.pid)
                self.assertNotIn("untrusted startup ran", job.log.read_text())
            finally:
                with mock.patch.object(merge_gate, "module_stop_deadline",
                                       return_value=time.monotonic() + TEST_HANG_GUARD_SECS):
                    self.assertTrue(merge_gate.kill_job_group(job), job.stop_error)
            assert descriptor is not None
            self.assertTrue(select.select([descriptor], [], [], 0)[0], "The namespace PID 1 stayed alive.")
            self.assertEqual(marker.exists(), not during_startup)


class TestBubblewrapHookProof(unittest.TestCase):
    def invoke(self, command: str | list[str], root: Path) -> str:
        with mock.patch.object(sys, "path", sys.path.copy()):
            decide = cast(Callable[[str], str], runpy.run_path(str(HOOK))["decide"])
        return decide(json.dumps({"cwd": str(root), "tool_name": "Bash", "tool_input": {"command": command}}))

    def test_raw_tests_are_denied_and_guarded_forms_are_allowed(self) -> None:
        for command in ("python3 -m unittest x", "pytest", "bash test_x.sh", "node memory_page_proof.mjs",
                        ["bash", "-lc", "env FLAG=1 python3 -munittest x"],
                        "python3 scripts/lib/test_bubble_wrapped_tests.py", "bash -O extglob -c 'bash test_x.sh'",
                        "echo ok; python3 -m unittest x"):
            with self.subTest(command=command):
                self.assertIn("Start tests through", self.invoke(command, CHECKOUT))
        for command in ("scripts/lib/py -m unittest x", "bash scripts/lib/py -m unittest x",
                        "scripts/lib/py scripts/lib/test_bubble_wrapped_tests.py",
                        "scripts/lib/py scripts/lib/bubble_wrapped_tests.py -- bash test_x.sh",
                        "printf '%s' 'python3 -m unittest x'", "rg 'pytest' scripts"):
            with self.subTest(command=command):
                self.assertEqual(self.invoke(command, CHECKOUT), "")

    def test_old_checkout_refuses_shim_and_gate_starts(self) -> None:
        with tempfile.TemporaryDirectory(prefix="guard-old-tree-") as directory:
            old = Path(directory)
            for relative in (SHIM.relative_to(CHECKOUT), MODULE_RUNNER.relative_to(CHECKOUT)):
                path = old / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()
            for command, root in (
                ("scripts/lib/py -m unittest x", old),
                (f"cd {old} && scripts/lib/py -m unittest x", CHECKOUT),
                (["python3", str(MODULE_RUNNER), str(old / "scripts/lib"), "test_x.py"], CHECKOUT),
                (["python3", "scripts/production/merge_gate.py", "--repo", str(old)], CHECKOUT),
            ):
                with self.subTest(command=command):
                    self.assertIn(f"Test starts require scripts/lib/bubble_wrapped_tests.py in {old}",
                                  self.invoke(command, root))


if __name__ == "__main__":
    _ = unittest.main()
