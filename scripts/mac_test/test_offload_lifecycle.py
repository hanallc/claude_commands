"""Offload claim, copy and signal lifetimes."""

from __future__ import annotations

import select
import shlex
import shutil
import signal
import io
import json
import os
from pathlib import Path
from contextlib import redirect_stdout
import sys
import subprocess
import tempfile
import threading
import unittest
from unittest import mock
from collections.abc import Sequence
from scripts.copies import copies
from scripts.mac_test import offload, mac_test, claim_state
from scripts.mac_test.offload_test_support import OffloadCommandFixture
from scripts.lint.memory_test_support import UNREACHABLE_WAIT_FIXTURE, config_environment, write_config_fixture
# The fixture loads offload's sibling script paths before the tree-key import.
from scripts.buildlog import treekey
from scripts.lib import test_hang_guard
from scripts.lib import process_start as process_identity


class PendingDispatchTests(unittest.TestCase):
    def test_a_mirror_build_child_inherits_the_start_fence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = Path(offload.__file__).resolve().parents[2]
            (root / ".claude").symlink_to(checkout, target_is_directory=True)
            environment = {**os.environ, **config_environment(root), "HOME": str(root),
                           "XDG_CACHE_HOME": str(root / "cache"), "TMPDIR": str(root),
                           "PYTHONDONTWRITEBYTECODE": "1", "CLAUDE_DISABLE_DATACLASS_CODE_CACHE": "1"}
            _ = write_config_fixture(root / "build_admission.conf", "# whole: 0..\nnested_tokens=0\n")
            identity = claim_state.ClaimIdentity("child-claim", "child-episode", "child-attempt")
            mirror = copies.RemoteMirror("verify", copies.SelectedRepository("hana"), "private-host",
                                        str(root / "mirrors/hana"), "fixture-session")
            fence = root / offload.start_fence_relative(identity, "mac").with_suffix(".lock")
            prepared = subprocess.run(["bash", "-c", offload.prepare_start_fence_command(identity, "mac")],
                cwd=root, env=environment, capture_output=True, text=True, check=False,
                timeout=test_hang_guard.TEST_HANG_GUARD_SECS)
            self.assertEqual(prepared.returncode, 0, prepared.stderr)

            release = root / "release"
            finished = root / "finished"
            os.mkfifo(release)
            os.mkfifo(finished)
            release_fd = os.open(release, os.O_RDWR | os.O_NONBLOCK)
            self.addCleanup(os.close, release_fd)
            finished_fd = os.open(finished, os.O_RDWR | os.O_NONBLOCK)
            self.addCleanup(os.close, finished_fd)
            child = root / "child.py"
            _ = child.write_text('''import os, select, sys
assert os.path.samestat(os.fstat(8), os.stat(sys.argv[1]))
with open(sys.argv[2], 'rb') as release:
    assert select.select([release], [], [], float(sys.argv[4]))[0]
    assert release.read(1) == b'x'
os.close(8)
with open(sys.argv[3], 'wb') as finished:
    finished.write(b'x')
''')
            manifest = root / "Cargo.toml"
            _ = manifest.write_text('''[workspace.metadata.dev.prebuild.fixture-artifact]
target = "fixture-target"
env = "FIXTURE_ARTIFACT"
for = ["fixture-tests"]
''')
            pool = root / "tokens"
            os.mkfifo(pool)
            pool_fd = os.open(pool, os.O_RDWR | os.O_NONBLOCK)
            self.addCleanup(os.close, pool_fd)
            _ = os.write(pool_fd, b"+")
            cargo = root / "cargo"
            _ = cargo.write_text(f"#!{sys.executable}\n" + '''import json, os, subprocess, sys
from pathlib import Path

root = Path(os.environ["HOME"])
fence = os.environ["TEST_FENCE"]
assert os.path.samestat(os.fstat(8), os.stat(fence)), sys.argv
if sys.argv[1] == "build":
    print(json.dumps({"reason": "compiler-artifact", "package_id": "fixture-artifact 0.1.0 (path+file:///fixture)",
                      "target": {"kind": ["bin"]}, "executable": "/fixture/artifact"}))
elif sys.argv[1:3] == ["nextest", "list"]:
    print(json.dumps({"rust-suites": {"fixture": {"testcases": {"test": {"filter-match": {"status": "matches"}}}}}}))
elif "--no-run" not in sys.argv:
    assert os.environ["FIXTURE_ARTIFACT"] == "/fixture/artifact"
    assert os.environ["CARGO_MAKEFLAGS"].startswith("--jobserver-auth=fifo:")
    with (root / "finished.errors").open("wb") as errors:
        subprocess.Popen([sys.executable, str(root / "child.py"), fence, str(root / "release"),
                          str(root / "finished"), os.environ["TEST_FENCE_TIMEOUT"]],
                         pass_fds=(8,), start_new_session=True, cwd=root,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=errors)
''')
            cargo.chmod(0o755)
            environment.update({"PATH": f"{root}:{environment['PATH']}", "TEST_FENCE": str(fence),
                                "TEST_FENCE_TIMEOUT": str(test_hang_guard.TEST_HANG_GUARD_SECS)})
            scripts = root / ".claude/scripts"
            py = str(scripts / "lib/py")
            command = shlex.join([py, "-m", "scripts.mac_test.nextest_log", "run", "4096", "4096",
                py, str(scripts / "lint/prebuild.py"), "--manifest", str(manifest), "--",
                py, str(scripts / "lint/nextest_tokens.py"), "--pool", str(pool), "--",
                "cargo", "nextest", "run", "--test-threads", "1", "-p", "fixture-tests"])
            invocation = "cd " + shlex.quote(mirror.path) + " && " + command
            launch = offload.fenced_start_command(identity, "mac", copies.remote_use_command(mirror, invocation))
            key = offload.pending_start_key(identity)
            try:
                completed = subprocess.run(["bash", "-c", launch], cwd=root, env=environment,
                    capture_output=True, text=True, check=False, timeout=test_hang_guard.TEST_HANG_GUARD_SECS)
                self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
                self.assertEqual(completed.stdout.splitlines()[-2:], ["mac_exit=0", f"dispatch_start_pending={key}"],
                                 completed.stdout + completed.stderr)
                self.assertNotIn("nextest list exited", completed.stdout)
                self.assertTrue(fence.exists())
            finally:
                _ = os.write(release_fd, b"x")

            child_errors = finished.with_suffix(".errors")
            self.assertTrue(child_errors.is_file(), "Cargo did not start the child")
            self.assertTrue(select.select([finished_fd], [], [], test_hang_guard.TEST_HANG_GUARD_SECS)[0],
                            child_errors.read_text())
            self.assertEqual(os.read(finished_fd, 1), b"x")
            settle = offload.start_fence_command(identity, "mac", settle=True)
            settled = subprocess.run(["bash", "-c", settle], cwd=root, env=environment,
                capture_output=True, text=True, check=False, timeout=test_hang_guard.TEST_HANG_GUARD_SECS)
            self.assertEqual(settled.returncode, 0, settled.stderr)
            self.assertEqual(settled.stdout, f"dispatch_start_settled={key}\n")
            self.assertFalse(fence.exists())

    def test_a_pending_claim_or_start_cannot_release_capacity_before_its_child_settles(self) -> None:
        for operation in ("claim", "start-acknowledged", "start-uncertain"):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                paths = mac_test.StatePaths(root / "provider")
                paths.directory.mkdir()
                (root / "mac_test.conf").touch()
                _ = (root / "build_targets.conf").write_text(
                    "targets=mac\nkind.mac=ssh\nplatform.mac=macos\nsettings.mac=mac_test.conf\n" + UNREACHABLE_WAIT_FIXTURE,
                    encoding="utf-8",
                )
                owner = process_identity.process_start(os.getpid())
                if not isinstance(owner, process_identity.Started):
                    self.fail("The test process has no checked start identity")
                record: mac_test.RemoteJobClaim = {"claim_id": "pending-claim", "episode": "pending-episode",
                    "attempt": "pending-attempt", "submitting_session": "", "pid": os.getpid(),
                    "proc_start": owner.start, "what": "pending test", "worktree": str(root),
                    "since": "2026-11-10T10:00:00+00:00", "remote_host": "fixture-mac"}
                mac_test.write_json(paths.run, record)
                controller = offload.ProcessController()
                identity = claim_state.claim_identity(record)
                controller.claim_identity = identity
                ready, finish = root / "ready", root / "finish"
                os.mkfifo(ready)
                os.mkfifo(finish)
                ready_fd = os.open(ready, os.O_RDWR | os.O_NONBLOCK)
                finish_fd = os.open(finish, os.O_RDWR | os.O_NONBLOCK)
                self.addCleanup(os.close, ready_fd)
                self.addCleanup(os.close, finish_fd)
                reply = ("claimed\\nclaim_identity=" + json.dumps({"claim_id": identity.claim_id,
                    "episode": identity.episode, "attempt": identity.attempt}) if operation == "claim" else "mac_exit=0")
                program = ("from pathlib import Path; import sys; "
                           "Path(sys.argv[1]).write_text('ready'); "
                           "Path(sys.argv[2]).open().read(1); print(sys.argv[3],flush=True)")
                arguments = [sys.executable, "-c", program, str(ready), str(finish), reply.replace("\\n", "\n")]
                attempt = offload.LocalClaimAttempt()
                failures: list[BaseException] = []

                def dispatch() -> None:
                    try:
                        if operation == "claim":
                            controller.complete_claim(arguments, attempt)
                        else:
                            _ = controller.stream_mac_run(arguments)
                    except BaseException as error:
                        failures.append(error)

                def release(arguments: Sequence[str],
                            deadline: offload.CommandDeadline = offload.NO_COMMAND_TIME_LIMIT) -> offload.CommandOutput:
                    del deadline
                    parsed = mac_test.parser().parse_args(arguments[2:], namespace=mac_test.CliArguments())
                    with redirect_stdout(io.StringIO()):
                        status = mac_test.command_release(parsed, paths)
                    return offload.CommandOutput(status, "", "")

                worker = threading.Thread(target=dispatch)
                remote: subprocess.Popen[str] | None = None
                if operation == "start-uncertain":
                    remote_program = ("from pathlib import Path; import sys; "
                        "Path(sys.argv[1]).open().read(1); print('delayed-start',flush=True)")
                    remote = subprocess.Popen([sys.executable, "-c", remote_program, str(finish)],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                worker.start()
                try:
                    self.assertTrue(select.select([ready_fd], [], [], test_hang_guard.TEST_HANG_GUARD_SECS)[0])
                    self.assertEqual(os.read(ready_fd, 5), b"ready")
                    before = paths.run.read_bytes()
                    with mock.patch.dict(os.environ, {"CONF_DIR": str(root), "MAC_TEST_STATE_DIR": str(paths.directory)}), \
                         mock.patch.object(controller, "capture", side_effect=release) as provider_release:
                        result = offload.release_claim(controller)
                        self.assertIsInstance(result, offload.ClaimReleaseUnconfirmed)
                        self.assertEqual(paths.run.read_bytes(), before)
                        provider_release.assert_not_called()
                        if operation == "claim":
                            self.assertTrue(controller.defer_claim_interrupt(signal.SIGTERM))
                            controller.stop()
                            self.assertIsInstance(controller.pending, offload.PendingClaimOperation)
                            _ = os.write(finish_fd, b"x")
                        elif operation == "start-uncertain":
                            self.assertIsInstance(controller.pending, offload.PendingStartOperation)
                            controller.stop()
                        else:
                            self.assertIsInstance(controller.pending, offload.PendingStartOperation)
                            _ = os.write(finish_fd, b"x")
                        worker.join(timeout=test_hang_guard.TEST_HANG_GUARD_SECS)
                        self.assertFalse(worker.is_alive())
                        self.assertEqual(failures, [])
                        if remote is not None:
                            self.assertIsInstance(controller.pending, offload.PendingStartOperation)
                            self.assertIsInstance(offload.release_claim(controller), offload.ClaimReleaseUnconfirmed)
                            _ = os.write(finish_fd, b"x")
                            output, errors = remote.communicate(timeout=test_hang_guard.TEST_HANG_GUARD_SECS)
                            self.assertEqual(remote.returncode, 0, errors)
                            self.assertEqual(output, "delayed-start\n")
                            self.assertEqual(paths.run.read_bytes(), before)
                            provider_release.assert_not_called()
                        else:
                            self.assertIsInstance(controller.pending, offload.SettledDispatchOperations)
                            self.assertIsInstance(offload.release_claim(controller), offload.ClaimReleaseConfirmed)
                            self.assertIsInstance(mac_test.read_run(paths.run), mac_test.NoRun)
                            self.assertEqual(provider_release.call_count, 1)
                finally:
                    _ = os.write(finish_fd, b"x")
                    controller.stop()
                    worker.join(timeout=test_hang_guard.TEST_HANG_GUARD_SECS)
                    if remote is not None:
                        _ = remote.communicate(timeout=test_hang_guard.TEST_HANG_GUARD_SECS)

        for target, active_start in (("mac", False), ("mac", True), ("linux", False), ("linux", True)):
            with self.subTest(target=target, active_start=active_start), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                environment = {**os.environ, "HOME": str(root)}
                identity = claim_state.ClaimIdentity("fenced-claim", "fenced-episode", "fenced-attempt")
                acknowledgement = "dispatch_start_settled=" + offload.pending_start_key(identity) + "\n"
                start = offload.start_fence_command(identity, target)
                settle = offload.start_fence_command(identity, target, settle=True)
                prepared = subprocess.run(["bash", "-c", offload.prepare_start_fence_command(identity, target)],
                    cwd=root, env=environment, capture_output=True, text=True, check=False, timeout=5)
                self.assertEqual(prepared.returncode, 0, prepared.stderr)
                fence_directory = root / offload.start_fence_relative(identity, target).parent
                self.assertEqual(len(list(fence_directory.iterdir())), 1)
                if active_start:
                    active = subprocess.Popen(["bash", "-c", start + "printf 'active\\n'; read -r release; printf 'completed\\n'"],
                        cwd=root, env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE, text=True)
                    try:
                        assert active.stdout is not None
                        self.assertTrue(select.select([active.stdout], [], [], 5)[0])
                        self.assertEqual(active.stdout.readline(), "active\n")
                        pending = subprocess.run(["bash", "-c", settle], cwd=root, env=environment,
                            capture_output=True, text=True, check=False, timeout=5)
                        self.assertEqual(pending.returncode, 0, pending.stderr)
                        self.assertNotIn(acknowledgement, pending.stdout)
                        output, errors = active.communicate("release\n", timeout=5)
                        self.assertEqual(active.returncode, 0, errors)
                        self.assertEqual(output, "completed\n")
                    finally:
                        if active.poll() is None:
                            active.kill()
                            _ = active.communicate(timeout=5)
                settled = subprocess.run(["bash", "-c", settle], cwd=root, env=environment,
                    capture_output=True, text=True, check=False, timeout=5)
                self.assertEqual(settled.returncode, 0, settled.stderr)
                self.assertEqual(settled.stdout, acknowledgement)
                self.assertEqual(list(fence_directory.iterdir()), [])
                mirror = fence_directory.parent / "mirror/hana"
                mirror.mkdir(parents=True)
                _ = (mirror / "source.rs").write_text("the copied source\n")
                shutil.rmtree(mirror)
                mirror.mkdir()
                late = subprocess.run(["bash", "-c", start + "printf 'executed\\n'"], cwd=root,
                    env=environment, capture_output=True, text=True, check=False, timeout=5)
                self.assertEqual(late.returncode, 75, late.stdout + late.stderr)
                self.assertEqual(late.stdout, acknowledgement)
                self.assertEqual(list(fence_directory.iterdir()), [])

        for target in ("mac", "linux"):
            for ending in ("completed", "failed", "cancelled", "released"):
                with self.subTest(target=target, ending=ending), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    environment = {**os.environ, **config_environment(root), "HOME": str(root),
                                   "MAC_TEST_STATE_DIR": str(root / ".local/state/mac-test")}
                    _ = (root / "build_targets.conf").write_text(
                        "targets=mac,linux\nkind.mac=ssh\nplatform.mac=macos\nsettings.mac=mac_test.conf\n"
                        + "kind.linux=ssh\nplatform.linux=linux\nsettings.linux=linux_target.conf\n" + UNREACHABLE_WAIT_FIXTURE)
                    settings = root / ("mac_test.conf" if target == "mac" else "linux_target.conf")
                    for name in ("mac_test.conf", "linux_target.conf"):
                        _ = write_config_fixture(root / name, "offload=on\nhost=private-host\n", template="mac_test")
                    identity = claim_state.ClaimIdentity("cleanup-claim", "cleanup-episode", "cleanup-attempt")
                    prepared = subprocess.run(["bash", "-c", offload.prepare_start_fence_command(identity, target)],
                        cwd=root, env=environment, capture_output=True, text=True, check=False, timeout=5)
                    self.assertEqual(prepared.returncode, 0, prepared.stderr)
                    fence_directory = root / offload.start_fence_relative(identity, target).parent
                    self.assertEqual(len(list(fence_directory.iterdir())), 1)
                    if ending in ("completed", "failed"):
                        status = 37 if ending == "failed" else 0
                        command = offload.fenced_start_command(identity, target,
                            f"printf 'payload-completed\\n'; exit {status}")
                        completed = subprocess.run(["bash", "-c", command], cwd=root, env=environment,
                            capture_output=True, text=True, check=False, timeout=5)
                        self.assertEqual(completed.returncode, status, completed.stderr)
                        self.assertIn("payload-completed\n", completed.stdout)
                    elif ending == "cancelled":
                        settled = subprocess.run(["bash", "-c", offload.start_fence_command(identity, target, settle=True)],
                            cwd=root, env=environment, capture_output=True, text=True, check=False, timeout=5)
                        self.assertEqual(settled.returncode, 0, settled.stderr)
                        self.assertEqual(settled.stdout, "dispatch_start_settled=" + offload.pending_start_key(identity) + "\n")
                    else:
                        with mock.patch.dict(os.environ, environment):
                            paths = claim_state.state_paths(target)
                            owner = process_identity.process_start(os.getpid())
                            self.assertIsInstance(owner, process_identity.Started)
                            assert isinstance(owner, process_identity.Started)
                            retained_record: mac_test.RemoteJobClaim = {
                                "claim_id": identity.claim_id, "episode": identity.episode, "attempt": identity.attempt,
                                "submitting_session": "", "pid": os.getpid(), "proc_start": owner.start,
                                "what": "retained cleanup", "worktree": str(root),
                                "since": "2026-11-12T10:00:00+00:00", "remote_host": "private-host"}
                            mac_test.write_json(paths.run, retained_record)
                            pending = offload.pending_start_path(target, identity)
                            pending.parent.mkdir(parents=True, exist_ok=True)
                            _ = pending.write_text("pending")
                            before = paths.run.read_bytes()
                            args = mac_test.CliArguments()
                            args.pid, args.target, args.settings = os.getpid(), target, str(settings)
                            args.claim_id, args.episode, args.attempt = identity.claim_id, identity.episode, identity.attempt
                            real_run = subprocess.run
                            transport_failed = False

                            def release_transport(arguments: Sequence[str], **options: object) -> subprocess.CompletedProcess[str]:
                                nonlocal transport_failed
                                del options
                                self.assertEqual(arguments[0], "ssh")
                                self.assertEqual(arguments[-2], "private-host")
                                if not transport_failed:
                                    transport_failed = True
                                    return subprocess.CompletedProcess(arguments, 255, "", "private SSH failure")
                                return real_run(["bash", "-c", arguments[-1]], cwd=root, env=environment,
                                    capture_output=True, text=True, check=False, timeout=5)

                            with mock.patch.object(subprocess, "run", side_effect=release_transport):
                                self.assertEqual(mac_test.command_release(args, paths), 12)
                                self.assertEqual(paths.run.read_bytes(), before)
                                self.assertTrue(pending.exists())
                                self.assertEqual(len(list(fence_directory.iterdir())), 1)
                                self.assertEqual(mac_test.command_release(args, paths), 0)
                            self.assertIsInstance(mac_test.read_run(paths.run), mac_test.NoRun)
                            self.assertFalse(pending.exists())
                    self.assertEqual(list(fence_directory.iterdir()), [])


        for target in ("mac", "linux"):
            with self.subTest(target=target, stale_scope=True), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                environment = {**os.environ, "HOME": str(root)}
                current = claim_state.ClaimIdentity("current-claim", "current-episode", "current-attempt")
                old = claim_state.ClaimIdentity("old-claim", "old-episode", "old-attempt")
                other = claim_state.ClaimIdentity("other-claim", "other-episode", "other-attempt")
                scope_key = "repository-a.slot-1"
                other_scope_key = "repository-b.slot-1"
                for identity, scope in ((old, scope_key), (other, other_scope_key), (current, scope_key)):
                    prepared = subprocess.run(["bash", "-c", offload.prepare_start_fence_command(
                        identity, target, scope_key=scope)], cwd=root, env=environment,
                        capture_output=True, text=True, check=False, timeout=5)
                    self.assertEqual(prepared.returncode, 0, prepared.stderr)
                old_fence = root / offload.start_fence_relative(old, target, scope_key=scope_key).with_suffix(".lock")
                other_fence = root / offload.start_fence_relative(other, target, scope_key=other_scope_key).with_suffix(".lock")
                current_fence = root / offload.start_fence_relative(current, target, scope_key=scope_key).with_suffix(".lock")
                self.assertTrue(old_fence.exists())
                self.assertTrue(other_fence.exists())
                self.assertTrue(current_fence.exists())
                payload = ("test ! -e " + shlex.quote(str(old_fence))
                    + " && test -e " + shlex.quote(str(current_fence))
                    + " && test -e " + shlex.quote(str(other_fence))
                    + " && printf 'completed\\n'")
                completed = subprocess.run(["bash", "-c", offload.fenced_start_command(
                    current, target, payload, scope_key=scope_key)], cwd=root, env=environment,
                    capture_output=True, text=True, check=False, timeout=5)
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertIn("completed\n", completed.stdout)
                self.assertEqual(list(current_fence.parent.iterdir()), [])
                self.assertTrue(other_fence.exists())
                prepared = subprocess.run(["bash", "-c", offload.prepare_start_fence_command(
                    current, target, scope_key=scope_key)], cwd=root, env=environment,
                    capture_output=True, text=True, check=False, timeout=5)
                self.assertEqual(prepared.returncode, 0, prepared.stderr)
                late = subprocess.run(["bash", "-c", offload.fenced_start_command(
                    old, target, "printf 'executed\\n'", scope_key=scope_key)], cwd=root,
                    env=environment, capture_output=True, text=True, check=False, timeout=5)
                self.assertEqual(late.returncode, 75, late.stdout + late.stderr)
                self.assertNotIn("executed", late.stdout)
                self.assertTrue(current_fence.exists())
                self.assertTrue(other_fence.exists())
                completed = subprocess.run(["bash", "-c", offload.fenced_start_command(
                    current, target, "printf 'completed\\n'", scope_key=scope_key)], cwd=root, env=environment,
                    capture_output=True, text=True, check=False, timeout=5)
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertIn("completed\n", completed.stdout)
                self.assertEqual(list(current_fence.parent.iterdir()), [])
                self.assertTrue(other_fence.exists())


class OffloadCommandTests(OffloadCommandFixture):
    def test_source_changed_after_assignment_never_receives_remote_execution_authority(self) -> None:
        _ = self.commit_sent_tree()
        source = self.repository / "source.txt"
        _ = source.write_text("assigned content\n")
        _ = subprocess.run(["git", "-C", str(self.repository), "add", "source.txt"],
            check=True, capture_output=True, timeout=test_hang_guard.TEST_HANG_GUARD_SECS)
        process = self.start_command(script=self.claim_stub_runner())
        try:
            self.wait_for_path(self.stub_directory / "claim-started")
            _ = source.write_text("changed after assignment\n")
            _ = (self.stub_directory / "finish-claim").write_text("continue\n")
            output, _ = process.communicate(timeout=test_hang_guard.TEST_HANG_GUARD_SECS)
        finally:
            if process.poll() is None:
                process.kill()
                _ = process.communicate(timeout=test_hang_guard.TEST_HANG_GUARD_SECS)
        self.assertEqual(process.returncode, 75, output)
        self.assert_wire("declined", "source_changed")
        self.assertFalse(any("mirror-use" in event["args"][-1] for event in self.events_for("ssh")))

    def test_mac_run_line_waits_for_claim_and_tree_copy(self) -> None:
        self.fixture["hold_copy"] = True
        self.sync_fixture()
        process = self.start_command(script=self.claim_stub_runner())
        try:
            self.wait_for_path(self.stub_directory / "claim-started")
            output = process.stdout
            assert output is not None
            self.assertEqual(select.select([output], [], [], 0)[0], [])
            self.assertEqual(self.events(), [])
            _ = (self.stub_directory / "finish-claim").write_text("continue\n")
            self.wait_for_path(self.stub_directory / "copy-started")
            self.assertEqual(select.select([output], [], [], 0)[0], [])
            self.assertEqual([event["command"] for event in self.events()], ["ssh", "rsync"])
            self.assert_claimed_commands()
            _ = (self.stub_directory / "continue-copy").write_text("continue\n")
            text, _ = process.communicate(timeout=test_hang_guard.TEST_HANG_GUARD_SECS)
        finally:
            if process.poll() is None:
                process.kill()
                _ = process.communicate(timeout=test_hang_guard.TEST_HANG_GUARD_SECS)
        self.assertEqual(process.returncode, 0, text)
        self.assertEqual(text.count("mac_test: running on the Mac (macOS, host Mac); tree copied in "), 1)
        self.assert_wire("passed", "")

    def test_signal_during_fresh_claim_releases_without_ssh_when_check_would_time_out(self) -> None:
        self.assert_fresh_claim_signal_releases_without_ssh("timeout")

    def test_signal_during_fresh_claim_releases_without_ssh_when_check_would_fail(self) -> None:
        self.assert_fresh_claim_signal_releases_without_ssh("ssh_error")

    def test_signal_during_fresh_claim_releases_without_ssh_when_check_would_omit_status(self) -> None:
        self.assert_fresh_claim_signal_releases_without_ssh("missing_status")

    def test_signal_during_takeover_keeps_claim_for_leased_cleanup_without_ssh(self) -> None:
        self.assert_takeover_signal_keeps_claim_for_leased_cleanup("reply-mac")

    def test_signal_during_fresh_claim_from_process_group_preserves_reply_and_releases_without_ssh(self) -> None:
        self.set_leftover_answer("ssh_error")
        output = self.interrupt_held_claim(signal_group=True, signal_number=signal.SIGINT)
        self.assertEqual((self.stub_directory / "state-events").read_text().splitlines(),
                         ["claim", "release"], output)
        self.assertFalse(self.claim_file.exists())
        self.assertEqual(self.events_for("ssh"), [])
        self.assertFalse((self.stub_directory / "cleanup-called").exists())

    def test_signal_during_second_fresh_claim_releases_without_another_leftover_check(self) -> None:
        output = self.interrupt_held_claim(after_takeover=True)
        self.assertEqual((self.stub_directory / "state-events").read_text().splitlines(),
                         ["claim", "release", "claim", "release"], output)
        self.assertFalse(self.claim_file.exists())
        ssh = self.events_for("ssh")
        self.assertEqual(len(ssh), 1)
        self.assertEqual(ssh[0]["args"][-2], "previous-mac")

    def test_signal_during_takeover_with_no_reply_host_keeps_claim_without_ssh(self) -> None:
        self.assert_takeover_signal_keeps_claim_for_leased_cleanup("")

    def test_unchanged_copy_sends_its_known_tree_key_beside_the_call_id(self) -> None:
        key = self.commit_sent_tree()
        result = self.command()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(treekey.tree_key(str(self.repository)), key)
        words = self.sent_command_words()
        call_index = words.index("BUILDLOG_CALL_ID=call-17")
        self.assertEqual(words[call_index + 1], f"BUILDLOG_SENT_TREE_KEY={key}")

    def test_copy_with_a_changed_tree_key_declines_as_source_changed(self) -> None:
        before = self.commit_sent_tree()

        def change() -> None:
            _ = (self.repository / "during-copy.rs").write_text("changed during copy\n")

        self.run_with_copy_change(change, source_changed=True)
        after = treekey.tree_key(str(self.repository))
        self.assertIsInstance(after, str)
        self.assertNotEqual(before, after)
        self.assertFalse(any("BUILDLOG_SENT_TREE_KEY=" in event["args"][-1] for event in self.events_for("ssh")))
        self.assert_stable_copy_sends_key()

    def test_copy_with_unavailable_before_key_and_known_after_key_sends_no_key(self) -> None:
        self.remove_tree_key()
        self.assertIsNone(treekey.tree_key(str(self.repository)))

        def change() -> None:
            _ = self.commit_sent_tree()

        self.run_with_copy_change(change)
        self.assertIsInstance(treekey.tree_key(str(self.repository)), str)
        self.assertFalse(any(word.startswith("BUILDLOG_SENT_TREE_KEY=") for word in self.sent_command_words()))
        self.assert_stable_copy_sends_key()

    def test_copy_that_loses_its_known_tree_key_declines_as_source_changed(self) -> None:
        _ = self.commit_sent_tree()
        head_path = self.repository / ".git" / "HEAD"
        head = head_path.read_text()

        def change() -> None:
            (self.repository / ".git" / "HEAD").unlink()

        self.run_with_copy_change(change, source_changed=True)
        self.assertIsNone(treekey.tree_key(str(self.repository)))
        self.assertFalse(any("BUILDLOG_SENT_TREE_KEY=" in event["args"][-1] for event in self.events_for("ssh")))
        _ = head_path.write_text(head)
        self.assert_stable_copy_sends_key()

    def test_copy_with_both_tree_keys_unavailable_sends_no_key(self) -> None:
        self.remove_tree_key()
        self.assertIsNone(treekey.tree_key(str(self.repository)))
        result = self.command()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIsNone(treekey.tree_key(str(self.repository)))
        self.assertFalse(any(word.startswith("BUILDLOG_SENT_TREE_KEY=") for word in self.sent_command_words()))
        self.assert_stable_copy_sends_key()

    def test_first_remote_line_is_printed_before_the_next_line(self) -> None:
        self.fixture["run_lines"] = [
            {"stream": "stdout", "text": "streamed now"}
        ]
        self.fixture["hold_after_first_line"] = True
        self.sync_fixture()
        process = self.start_command()
        try:
            output_before_gate = self.read_until_output(
                process, "streamed now\n"
            )
            _ = (self.stub_directory / "continue-run").write_text(
                "continue\n", encoding="utf-8"
            )
            remaining_output, _ = process.communicate(timeout=test_hang_guard.TEST_HANG_GUARD_SECS)
        except BaseException:
            process.send_signal(signal.SIGTERM)
            _ = process.communicate(timeout=test_hang_guard.TEST_HANG_GUARD_SECS)
            raise
        output = output_before_gate + remaining_output
        self.assertEqual(process.returncode, 0, output)
        self.assertIn("streamed now\n", output_before_gate)
        self.assert_wire("passed", "")

    def test_signal_keeps_claim_when_leftover_check_does_not_prove_completion(self) -> None:
        self.fixture["hold_run"] = True
        for outcome in ("remaining", "missing_status", "timeout", "ssh_error"):
            with self.subTest(outcome=outcome):
                self.reset_leftover_case()
                self.set_leftover_answer(outcome)
                process = self.start_command("--workspace")
                try:
                    _ = self.read_until_output(process, "fixture acknowledgement\n")
                    process.send_signal(signal.SIGTERM)
                    output, _ = process.communicate(timeout=test_hang_guard.TEST_HANG_GUARD_SECS)
                finally:
                    if process.poll() is None:
                        process.kill()
                        _ = process.communicate(timeout=test_hang_guard.TEST_HANG_GUARD_SECS)
                self.assertEqual(process.returncode, 128 + signal.SIGTERM, output)
                self.assert_leftover_diagnostic(output, outcome)
                self.assert_claim_is_kept()
                self.assertEqual(len(self.leftover_events()), 1)

    def test_signal_before_remote_command_starts_releases_without_leftover_check(self) -> None:
        for stage in ("probe", "copy"):
            with self.subTest(stage=stage):
                self.reset_leftover_case()
                (self.stub_directory / f"{stage}-started").unlink(missing_ok=True)
                self.fixture["hold_probe"] = stage == "probe"
                self.fixture["hold_copy"] = stage == "copy"
                self.sync_fixture()
                process = self.start_command()
                try:
                    self.wait_for_path(self.stub_directory / f"{stage}-started")
                    process.send_signal(signal.SIGTERM)
                    output, _ = process.communicate(timeout=test_hang_guard.TEST_HANG_GUARD_SECS)
                finally:
                    if process.poll() is None:
                        process.kill()
                        _ = process.communicate(timeout=test_hang_guard.TEST_HANG_GUARD_SECS)
                self.assertEqual(process.returncode, 128 + signal.SIGTERM, output)
                self.assertFalse(self.claim_file.exists())
                self.assertEqual(self.leftover_events(), [])

    def test_run_signals_request_remote_cleanup_and_release_claim(self) -> None:
        self.fixture["hold_run"] = True
        self.sync_fixture()
        for signal_number in (signal.SIGTERM, signal.SIGHUP):
            with self.subTest(signal_number=signal_number):
                self.clear_events()
                (self.stub_directory / "run-started").unlink(missing_ok=True)
                (self.stub_directory / "cleanup-called").unlink(missing_ok=True)
                process = self.start_command("--workspace")
                try:
                    _ = self.read_until_output(process, "fixture acknowledgement\n")
                    process.send_signal(signal_number)
                    output, _ = process.communicate(timeout=test_hang_guard.TEST_HANG_GUARD_SECS)
                except BaseException:
                    process.kill()
                    _ = process.communicate(timeout=test_hang_guard.TEST_HANG_GUARD_SECS)
                    raise
                self.assertEqual(process.returncode, 128 + signal_number, output)
                self.assertTrue(
                    (self.stub_directory / "cleanup-called").exists()
                )
                self.assertFalse(self.claim_file.exists())
                probe_event, run_event, cleanup_event, settlement_event = self.events_for("ssh")
                self.assertNotIn("select_leftovers()", probe_event["args"][-1])
                self.assertIn("lint nextest", run_event["args"][-1])
                self.assertIn("dispatch_start_settled=", settlement_event["args"][-1])
                self.assertIn("claim", probe_event)
                assert "claim" in probe_event
                claim = probe_event["claim"]
                identity = claim_state.ClaimIdentity(claim["claim_id"], claim["episode"], claim["attempt"])
                self.assertIn(offload.pending_start_key(identity), settlement_event["args"][-1])
                cleanup_arguments = cleanup_event["args"]
                self.assertEqual(
                    cleanup_arguments[:-1],
                    [
                        "-o",
                        "BatchMode=yes",
                        "-o",
                        "ConnectTimeout=6",
                        "-o",
                        "ServerAliveInterval=15",
                        "-o",
                        "ServerAliveCountMax=3",
                        "mac",
                    ],
                )
                cleanup = cleanup_arguments[-1]
                self.assert_leftover_selectors(cleanup)
                self.assert_claimed_commands()

    def test_second_signal_during_cleanup_keeps_first_status_and_releases(self) -> None:
        self.fixture["hold_run"] = True
        self.fixture["hold_cleanup"] = True
        self.sync_fixture()
        process = self.start_command("--workspace")
        try:
            _ = self.read_until_output(process, "fixture acknowledgement\n")
            process.send_signal(signal.SIGTERM)
            self.wait_for_path(self.stub_directory / "cleanup-started")
            process.send_signal(signal.SIGHUP)
            _ = (self.stub_directory / "continue-cleanup").write_text(
                "continue\n", encoding="utf-8"
            )
            output, _ = process.communicate(timeout=test_hang_guard.TEST_HANG_GUARD_SECS)
        except BaseException:
            process.kill()
            _ = (self.stub_directory / "continue-cleanup").write_text(
                "continue\n", encoding="utf-8"
            )
            _ = process.communicate(timeout=test_hang_guard.TEST_HANG_GUARD_SECS)
            raise

        self.assertEqual(process.returncode, 128 + signal.SIGTERM, output)
        self.assertTrue((self.stub_directory / "cleanup-called").exists())
        self.assertFalse(self.claim_file.exists())

    def test_ignored_interrupt_becomes_default_before_claim(self) -> None:
        startup = self.root / "interrupt-startup"
        startup.mkdir()
        marker = self.stub_directory / "interrupt-is-default"
        gate = self.stub_directory / "continue-after-interrupt-default"
        _ = (startup / "sitecustomize.py").write_text(
            "from __future__ import annotations\n"
            + "import os\n"
            + "from pathlib import Path\n"
            + "import signal\n"
            + "import time\n"
            + "original_signal = signal.signal\n"
            + "def observed_signal(signal_number: int, handler: object) -> object:\n"
            + "    previous = original_signal(signal_number, handler)\n"
            + "    if signal_number == signal.SIGINT and handler == signal.SIG_DFL:\n"
            + "        Path(os.environ['TEST_INTERRUPT_DEFAULT_MARKER']).write_text(\n"
            + "            'ready\\n', encoding='utf-8'\n"
            + "        )\n"
            + "        gate = Path(os.environ['TEST_INTERRUPT_DEFAULT_GATE'])\n"
            + "        while not gate.exists():\n"
            + "            time.sleep(0.01)\n"
            + "    return previous\n"
            + "signal.signal = observed_signal\n",
            encoding="utf-8",
        )
        process = self.start_command(
            extra={
                "PYTHONPATH": str(startup),
                "TEST_INTERRUPT_DEFAULT_MARKER": str(marker),
                "TEST_INTERRUPT_DEFAULT_GATE": str(gate),
            },
            ignore_interrupt=True,
        )
        try:
            self.wait_for_path(marker)
            process.send_signal(signal.SIGINT)
            output, _ = process.communicate(timeout=test_hang_guard.TEST_HANG_GUARD_SECS)
        except BaseException:
            process.kill()
            _ = gate.write_text("continue\n", encoding="utf-8")
            _ = process.communicate(timeout=test_hang_guard.TEST_HANG_GUARD_SECS)
            raise

        self.assertEqual(process.returncode, -signal.SIGINT, output)
        self.assertFalse(self.claim_file.exists())
        self.assertEqual(self.events(), [])
        self.assertFalse(self.result_path.exists())


if __name__ == "__main__":
    _ = unittest.main()
