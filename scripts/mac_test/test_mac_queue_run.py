"""Exercise placement return and SSH routing with executable stand-ins."""
from __future__ import annotations

from contextlib import ExitStack, redirect_stderr, redirect_stdout, suppress
from datetime import UTC, datetime, timedelta
import io
import json
import os
from pathlib import Path
import re
import select
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from typing import BinaryIO, TextIO, cast
import unittest
from unittest import mock

from scripts.direct.wait_for_test_state import wait_until
from scripts.lib import build_folders, conf, removal_guard, scratch
from scripts.lib.checkout_test_support import write_stdlib_tool
from scripts.lib.process_group import send_process_group
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.build_hold import override_guard
from scripts.build_hold.test_override_guard import installed_script
from scripts.lint import tmp_sweep
from scripts.lint import sweep
from scripts.mac_test import floor_claims, mac_queue_run, mac_test, mac_transport
from scripts.lint.memory_test_support import config_environment, write_config_fixture
from scripts.production import staff
from scripts.production.staff_test_support import fixture_runtime_id


QUEUE = r'''
import json, os, sys, time
from pathlib import Path
args = sys.argv[1:]
root = Path(os.environ["FAKE_QUEUE_STATE"])
def option(name):
    return args[args.index(name)+1]
with (root / "events").open("a") as stream:
    stream.write(json.dumps(args)+"\n")
if args[0] == "place-step":
    if os.environ.get("FAKE_QUEUE_BUSY") == "1":
        print("waiting for the Mac")
        raise SystemExit(78)
    if os.environ.get("FAKE_QUEUE_WAIT") == "1":
        (root / "waiting").write_text("waiting")
        time.sleep(60)
    if os.environ.get("FAKE_QUEUE_REFUSE") == "1":
        print("refused\tclosed")
        raise SystemExit(2)
    pending = root / "block-on-placement.json"
    if pending.exists():
        pending.rename(Path(os.environ["MAC_TEST_STATE_DIR"]) / "block.json")
    receipt = Path(option("--assignment-file"))
    if os.environ.get("FAKE_QUEUE_SIDECARS") == "1":
        for suffix in (".wait", ".queued"):
            receipt.with_name(receipt.name + suffix).touch()
    receipt.write_text(json.dumps({"episode":option("--episode"),"attempt":"attempt"}))
    print("mac\tmac\tfake-mac\t0\t"+option("--episode")+"\tattempt")
else:
    if os.environ.get("FAKE_RETURN_FAIL") == "1":
        raise SystemExit(3)
    print('{"state":"closed"}')
'''

SSH = r'''
import json, os, shlex, sys
from pathlib import Path
root = Path(os.environ["FAKE_QUEUE_STATE"])
args = shlex.split(sys.argv[-1])
if "mac_stopped=" in args[-1]:
    (root / "stop").write_text(args[-1])
    if os.environ.get("FAKE_STOP_FAIL") == "1":
        raise SystemExit(255)
    print("mac_stopped=4567", flush=True)
    raise SystemExit(0)
(root / "remote").write_text(json.dumps(args))
print("mac_group=4567", flush=True)
if os.environ.get("FAKE_SSH_HOLD") == "1":
    print("Mac command started", flush=True)
    # The real ssh sends the end of its input to the Mac wrapper, which stops the work.
    _ = sys.stdin.read()
    (root / "input_closed").write_text("closed")
    raise SystemExit(255)
status = int(os.environ.get("FAKE_MAC_STATUS","0"))
print("Mac output", flush=True)
if status == 125:
    print("mac cargo gate refused: placement closed; run it through mac_queue_run.py", file=sys.stderr)
print(f"mac_exit={status}", flush=True)
'''


def plain_ssh_builds(root: Path) -> list[str]:
    forbidden = re.compile(r"\b(?:ssh|on_mac)\b[^\n]*(?:\bcargo(?:\s|$)|\brustc(?:\s|$)|\bnextest(?:\s|$)|scripts/lint/lint\b|/target/(?:debug|release)/)")
    findings: list[str] = []
    for path in sorted(root.rglob("*")):
        if path.suffix not in (".py", ".sh") or path.name.startswith("test_") or "test_support" in path.name:
            continue
        # Item 11 changes offload.py after the queue merge.
        if path.relative_to(root).as_posix() == "mac_test/offload.py":
            continue
        for number, line in enumerate(path.read_text().splitlines(), 1):
            if not line.lstrip().startswith("#") and forbidden.search(line):
                findings.append(f"{path.relative_to(root)}:{number}: {line.strip()}")
    return findings


# The queue releases on process end, so these files set no signal handler (user rule, 2026-11-14).
NO_HANDLER_FILES = ("lint/nextest_tokens.py", "mac_test/mac_queue_run.py", "mac_test/mac_transport.py",
                    "mac_test/cargo_gate/gate.zsh")
SIGNAL_HANDLER = re.compile(r"\bsignal\.(?:signal|SIG_IGN|SIG_DFL|pthread_sigmask|setitimer)\b|\bfrom signal import\b"
                            + r"|^\s*trap\b|\bTRAP[A-Z]+\b")


def signal_handlers(root: Path, names: tuple[str, ...]) -> list[str]:
    findings: list[str] = []
    for name in names:
        for number, line in enumerate((root / name).read_text().splitlines(), 1):
            if not line.lstrip().startswith("#") and SIGNAL_HANDLER.search(line):
                findings.append(f"{name}:{number}: {line.strip()}")
    return findings


def queue_fixture(root: Path, ssh_source: str = SSH) -> dict[str, str]:
    queue = root / "queue.py"
    _ = queue.write_text(QUEUE)
    ssh = root / "ssh"
    _ = ssh.write_text(f"#!{sys.executable}\n" + ssh_source)
    ssh.chmod(0o755)
    environment = {**os.environ, **config_environment(root / "config"), "FAKE_QUEUE_STATE": str(root),
                   "PATH": str(root) + os.pathsep + os.environ["PATH"], "HOME": str(root),
                   "MAC_TEST_STATE_DIR": str(root / "mac-test"), "STAFF_FILE": str(root / "staff.json")}
    _ = write_config_fixture(root / "config/mac_gate.conf", "admission_client_command="
                             + shlex.join([sys.executable, str(queue)]) + "\n")
    return environment


def queue_with_ready_event(root: Path) -> Path:
    """Run the real queue and wait for the watcher pipe in lifecycle tests."""
    source = Path(__file__).resolve().parents[2]
    script = root / "queue-entry.py"
    _ = script.write_text(f"import sys\nsys.path.insert(0, {str(source)!r})\n" + r"""
import os
import select

from scripts.mac_test import mac_queue_run

def wait_for_ready(output, _timeout):
    expected = b"ready\n"
    received = b""
    while received != expected:
        readable, _, _ = select.select([caller, output], [], [])
        if caller in readable:
            return False
        chunk = os.read(output, len(expected) - len(received))
        received += chunk
        if not chunk or not expected.startswith(received):
            return False
    return True

with os.fdopen(os.dup(int(sys.argv[sys.argv.index("--caller-fd") + 1])), "rb") as pipe:
    caller = pipe.fileno()
    mac_queue_run.watcher_ready = wait_for_ready
    raise SystemExit(mac_queue_run.main())
""")
    return script


def end_queue_fixture(process: subprocess.Popen[str] | subprocess.Popen[bytes], caller: BinaryIO,
                      *, queue_group: bool = False) -> None:
    """Close the caller pipe so the queue can stop and reap its watcher."""
    caller.close()
    try:
        _ = process.wait(timeout=TEST_HANG_GUARD_SECS)
    except subprocess.TimeoutExpired:
        if queue_group:
            with suppress(ProcessLookupError):
                send_process_group(process.pid, signal.SIGKILL)
        else:
            process.kill()
        _ = process.wait(timeout=TEST_HANG_GUARD_SECS)
        raise


class QueueRunTests(unittest.TestCase):
    def command(self, root: Path, kind: str = "shot") -> list[str]:
        return [sys.executable, "-m", "scripts.mac_test.mac_queue_run", "--what", "camera shot", "--kind", kind,
                "--package", "hana", "--worktree", str(root), "--cwd", "/Mac scratch/checkout", "--",
                "/bin/zsh", "-c", "cargo build -p hana; printf '%s' 'quoted value'"]

    def events(self, root: Path) -> list[list[str]]:
        return [cast(list[str], json.loads(line)) for line in (root / "events").read_text().splitlines()]

    def write_block(self, path: Path, *, expired: bool = False, ci: mac_test.CiSwitch = "not_configured",
                    session: str = "holder-session") -> None:
        now = datetime.now(UTC)
        since = now - timedelta(hours=2) if expired else now
        mac_test.write_block(path, mac_test.ActiveMacBlock(
            "fixture-holder", "fixture work\nneeds the Mac", since, since + timedelta(hours=1),
            mac_test.RecordedSession(session, "fixture-holder"),
            mac_test.NO_SHOWRUNNER, ci, mac_test.FREE_MESSAGE_NOT_NEEDED))

    def test_run_waits_once_for_another_sessions_block(self) -> None:
        for block_after_request in (False, True):
            with self.subTest(block_after_request=block_after_request), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                environment = {**queue_fixture(root), "CODEX_THREAD_ID": "other-session"}
                block = Path(environment["MAC_TEST_STATE_DIR"]) / "block.json"
                block.parent.mkdir()
                self.write_block(root / "block-on-placement.json" if block_after_request else block)
                previous = ["place-step", "cancel-placement"] if block_after_request else []
                waits = 0

                def release_after_two_waits() -> None:
                    nonlocal waits
                    waits += 1
                    self.assertTrue(block.is_file())
                    self.assertFalse((root / "remote").exists())
                    events = self.events(root) if (root / "events").exists() else []
                    self.assertEqual([event[0] for event in events], previous)
                    if waits == 2:
                        block.unlink()

                with (mock.patch.dict(os.environ, environment, clear=True),
                      mock.patch.object(mac_queue_run, "wait_to_retry", side_effect=release_after_two_waits),
                      redirect_stdout(io.StringIO()) as output):
                    self.assertEqual(mac_queue_run.main(self.command(root, "run")[3:]), 0)
                self.assertEqual(waits, 2)
                self.assertEqual(output.getvalue().splitlines(), [
                    "Waiting for the Mac block held by fixture-holder: fixture work needs the Mac",
                    "Mac output", "mac_exit=0",
                ])
                self.assertTrue((root / "remote").is_file())
                self.assertEqual([event[0] for event in self.events(root)],
                                 [*previous, "place-step", "cancel-placement"])

    def test_run_starts_for_the_block_holder(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = {**queue_fixture(root), "CLAUDE_CODE_SESSION_ID": "holder-session"}
            block = Path(environment["MAC_TEST_STATE_DIR"]) / "block.json"
            block.parent.mkdir()
            self.write_block(block)
            result = subprocess.run(self.command(root, "run"), env=environment, capture_output=True, text=True,
                                    timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(result.stdout.splitlines(), ["Mac output", "mac_exit=0"])
            self.assertTrue(block.is_file())
            self.assertTrue((root / "remote").is_file())

    def test_run_uses_the_block_holding_units_session_for_placement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            named_run = root / "unit-run"
            holder = fixture_runtime_id("queue-run-holder")
            environment = {**queue_fixture(root), "UNIT_RUN_DIR": str(named_run),
                           "CLAUDE_CODE_SESSION_ID": "worker-session", "CODEX_THREAD_ID": "worker-thread"}
            director = staff.StaffId(fixture_runtime_id("queue-run-director"))
            parent = staff.StaffRow(director, "showrunner", "fixture-director", staff.StaffRoot(),
                                    current_session=director)
            row = staff.StaffRow(staff.StaffId(fixture_runtime_id("queue-run-unit")), "unit", "fixture-holder",
                                 staff.ReportsTo(director), current_session=staff.StaffId(holder),
                                 run=staff.OpenRun(named_run), unit_type=staff.UnitDetails("direct"))
            _ = Path(environment["STAFF_FILE"]).write_text(staff.StaffSnapshot((parent, row)).encode())
            block = Path(environment["MAC_TEST_STATE_DIR"]) / "block.json"
            block.parent.mkdir()
            self.write_block(block, session=holder)
            with mock.patch.dict(os.environ, environment, clear=True), redirect_stdout(io.StringIO()) as output:
                self.assertIn(row, staff.local_snapshot().rows)
                self.assertEqual(mac_queue_run.main(["--no-wait", *self.command(root, "run")[3:]]), 0)
            placed = self.events(root)[0]
            self.assertEqual(placed[placed.index("--session-id") + 1], holder)
            self.assertEqual(output.getvalue().splitlines(), ["Mac output", "mac_exit=0"])
            self.assertTrue(block.is_file())

    def test_shot_of_the_block_holding_units_worker_starts_with_its_own_placement_session(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            named_run = root / "unit-run"
            holder = fixture_runtime_id("queue-shot-holder")
            environment = {**queue_fixture(root), "UNIT_RUN_DIR": str(named_run),
                           "CLAUDE_CODE_SESSION_ID": "worker-session", "CODEX_THREAD_ID": "worker-thread"}
            director = staff.StaffId(fixture_runtime_id("queue-shot-director"))
            parent = staff.StaffRow(director, "showrunner", "fixture-director", staff.StaffRoot(),
                                    current_session=director)
            row = staff.StaffRow(staff.StaffId(fixture_runtime_id("queue-shot-unit")), "unit", "fixture-holder",
                                 staff.ReportsTo(director), current_session=staff.StaffId(holder),
                                 run=staff.OpenRun(named_run), unit_type=staff.UnitDetails("direct"))
            _ = Path(environment["STAFF_FILE"]).write_text(staff.StaffSnapshot((parent, row)).encode())
            block = Path(environment["MAC_TEST_STATE_DIR"]) / "block.json"
            block.parent.mkdir()
            self.write_block(block, session=holder)
            with mock.patch.dict(os.environ, environment, clear=True), redirect_stdout(io.StringIO()) as output:
                self.assertEqual(mac_queue_run.main(["--no-wait", *self.command(root, "shot")[3:]]), 0)
            placed = self.events(root)[0]
            self.assertEqual(placed[placed.index("--session-id") + 1], "worker-thread")
            self.assertEqual(output.getvalue().splitlines(), ["Mac output", "mac_exit=0"])
            self.assertTrue(block.is_file())

    def test_block_during_recovery_prevents_prepare_and_removes_retry_receipts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = {**queue_fixture(root), "FAKE_QUEUE_SIDECARS": "1"}
            block = Path(environment["MAC_TEST_STATE_DIR"]) / "block.json"
            block.parent.mkdir()
            callback = root / "callback"
            write_stdlib_tool(callback, """
import json, sys
from pathlib import Path
if sys.argv[1] == "prepare":
    Path(__file__).with_name("prepared").touch()
    Path(sys.argv[2]).write_text(json.dumps(sys.argv[3:]))
""")
            receipts = root / mac_queue_run.RECEIPTS

            def block_after_placement(_environment: dict[str, str]) -> None:
                if len(self.events(root)) == 1:
                    self.assertEqual(sorted(path.suffix for path in receipts.iterdir()), [".json", ".queued", ".wait"])
                    self.write_block(block)
                else:
                    for suffix in (".wait", ".queued"):
                        for path in receipts.glob("*" + suffix):
                            path.unlink()

            def release_block() -> None:
                self.assertFalse((root / "prepared").exists())
                self.assertFalse((root / "remote").exists())
                self.assertEqual([event[0] for event in self.events(root)], ["place-step", "cancel-placement"])
                self.assertEqual(list(receipts.iterdir()), [])
                block.unlink()

            with (mock.patch.dict(os.environ, environment, clear=True),
                  mock.patch.object(mac_queue_run, "clean_ended_runs", side_effect=block_after_placement),
                  mock.patch.object(mac_queue_run, "wait_to_retry", side_effect=release_block) as wait,
                  redirect_stdout(io.StringIO()) as output):
                status = mac_queue_run.main(["--lifecycle-script", str(callback), *self.command(root, "run")[3:]])
            self.assertEqual(status, 0)
            wait.assert_called_once()
            self.assertTrue((root / "prepared").is_file())
            self.assertTrue((root / "remote").is_file())
            self.assertEqual(output.getvalue().splitlines(), [
                "Waiting for the Mac block held by fixture-holder: fixture work needs the Mac",
                "Mac output", "mac_exit=0",
            ])

    def test_mac_run_refuses_blocked_or_unreadable_state_after_placement(self) -> None:
        for unreadable in (False, True):
            with self.subTest(unreadable=unreadable), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                environment = {**queue_fixture(root), "CODEX_THREAD_ID": "other-session",
                               "FAKE_QUEUE_SIDECARS": "1"}
                block = Path(environment["MAC_TEST_STATE_DIR"]) / "block.json"
                block.parent.mkdir()
                pending = root / "block-on-placement.json"
                if unreadable:
                    _ = pending.write_text("damaged\n")
                    expected = f"state unreadable: {block}\n"
                else:
                    self.write_block(pending)
                    expected = "blocked by fixture-holder: fixture work\nneeds the Mac\n"

                callback = root / mac_queue_run.MAC_RUN_SCRIPT.name
                with (mock.patch.dict(os.environ, environment, clear=True),
                      mock.patch.object(mac_queue_run, "run_callback") as lifecycle,
                      mock.patch.object(mac_queue_run, "wait_to_retry") as wait,
                      redirect_stdout(io.StringIO()) as output):
                    status = mac_queue_run.main(["--lifecycle-script", str(callback), *self.command(root, "run")[3:]])

                self.assertEqual(status, 9)
                self.assertEqual(output.getvalue(), expected)
                lifecycle.assert_not_called()
                wait.assert_not_called()
                self.assertTrue(block.is_file())
                self.assertFalse((root / "remote").exists())
                self.assertEqual([event[0] for event in self.events(root)], ["place-step", "cancel-placement"])
                self.assertEqual(list((root / mac_queue_run.RECEIPTS).iterdir()), [])

    def check_kind_waits_for_a_block_after_its_placement(self, kind: str) -> None:
        """A block or an unreadable block record that appears at placement keeps the kind waiting."""
        for unreadable in (False, True):
            with self.subTest(unreadable=unreadable), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                environment = {**queue_fixture(root), "CODEX_THREAD_ID": "other-session"}
                block = Path(environment["MAC_TEST_STATE_DIR"]) / "block.json"
                block.parent.mkdir()
                pending = root / "block-on-placement.json"
                if unreadable:
                    _ = pending.write_text("damaged\n")
                    expected = f"Waiting for a readable Mac block record: {block}"
                else:
                    self.write_block(pending)
                    expected = "Waiting for the Mac block held by fixture-holder: fixture work needs the Mac"
                waits = 0

                def release_block() -> None:
                    nonlocal waits
                    waits += 1
                    self.assertTrue(block.is_file())
                    self.assertFalse((root / "remote").exists())
                    self.assertEqual([event[0] for event in self.events(root)], ["place-step", "cancel-placement"])
                    block.unlink()

                with (mock.patch.dict(os.environ, environment, clear=True),
                      mock.patch.object(mac_queue_run, "wait_to_retry", side_effect=release_block),
                      redirect_stdout(io.StringIO()) as output):
                    self.assertEqual(mac_queue_run.main(self.command(root, kind)[3:]), 0)
                self.assertEqual(waits, 1)
                self.assertEqual(output.getvalue().splitlines(), [expected, "Mac output", "mac_exit=0"])
                self.assertTrue((root / "remote").is_file())
                self.assertEqual([event[0] for event in self.events(root)],
                                 ["place-step", "cancel-placement", "place-step", "cancel-placement"])

    def test_shot_waits_for_a_block_after_its_placement(self) -> None:
        self.check_kind_waits_for_a_block_after_its_placement("shot")

    def test_install_waits_for_a_block_after_its_placement(self) -> None:
        self.check_kind_waits_for_a_block_after_its_placement("install")

    def test_probe_waits_for_a_block_after_its_placement(self) -> None:
        self.check_kind_waits_for_a_block_after_its_placement("probe")

    def test_expired_block_lifts_without_the_watcher_and_preserves_a_failed_ci_restore(self) -> None:
        for restore_fails in (False, True):
            with self.subTest(restore_fails=restore_fails), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                environment = queue_fixture(root)
                block = Path(environment["MAC_TEST_STATE_DIR"]) / "block.json"
                block.parent.mkdir()
                self.write_block(block, expired=True, ci="off_by_this_block" if restore_fails else "not_configured")
                _ = write_config_fixture(root / "config/mac_test.conf", "ci_repo=fixture/project\n")
                with (mock.patch.dict(os.environ, environment, clear=True),
                      mock.patch.object(mac_test, "switch_ci_back", return_value=mac_test.GhFailure("fixture CI error")) as restore,
                      mock.patch.object(mac_test, "send_to_block_recipients"),
                      mock.patch.object(mac_test, "record_hold_event") as hold,
                      mock.patch.object(mac_test, "start_watcher") as watcher,
                      mock.patch.object(mac_queue_run, "wait_to_retry") as wait,
                      redirect_stdout(io.StringIO()) as output):
                    status = mac_queue_run.main(["--no-wait", *self.command(root, "run")[3:]])
                watcher.assert_not_called()
                wait.assert_not_called()
                self.assertEqual(block.exists(), restore_fails)
                self.assertEqual((root / "remote").exists(), not restore_fails)
                if restore_fails:
                    self.assertEqual(status, mac_queue_run.MAC_BUSY_STATUS)
                    self.assertIn("Mac blocked held by fixture-holder", output.getvalue())
                    restore.assert_called_once()
                    hold.assert_not_called()
                else:
                    self.assertEqual(status, 0)
                    self.assertEqual(output.getvalue().splitlines(), ["Mac output", "mac_exit=0"])
                    restore.assert_not_called()
                    hold.assert_called_once()

    def test_two_waiters_keep_waiting_after_one_failed_ci_restore(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = {**queue_fixture(root), "CLAUDE_CODE_SESSION_ID": "first-waiter"}
            block = Path(environment["MAC_TEST_STATE_DIR"]) / "block.json"
            block.parent.mkdir()
            self.write_block(block, expired=True, ci="off_by_this_block")
            _ = write_config_fixture(root / "config/mac_test.conf", "ci_repo=fixture/project\n")
            args = self.command(root, "run")[3:]
            first_waits = second_waits = 0
            second_output = io.StringIO()
            restores = [mac_test.GhFailure("fixture CI error"), mac_test.GhAnswer("")]

            def check_wait() -> None:
                self.assertTrue(block.is_file())
                self.assertFalse((root / "events").exists())
                restore.assert_called_once()

            def wait_second() -> None:
                nonlocal second_waits
                second_waits += 1
                check_wait()
                if second_waits == 2:
                    unblock = mac_test.CliArguments()
                    unblock.holder = "fixture-holder"
                    with redirect_stdout(io.StringIO()):
                        self.assertEqual(mac_test.command_unblock(
                            unblock, mac_test.state_paths(), mac_test.read_config()), 0)

            def wait_first() -> None:
                nonlocal first_waits
                first_waits += 1
                check_wait()
                if first_waits == 2:
                    with (mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "second-waiter"}),
                          mock.patch.object(mac_queue_run, "wait_to_retry", side_effect=wait_second),
                          redirect_stdout(second_output)):
                        self.assertEqual(mac_queue_run.main(args), 0)

            with (mock.patch.dict(os.environ, environment, clear=True),
                  mock.patch.object(mac_test, "switch_ci_back", side_effect=restores) as restore,
                  mock.patch.object(mac_test, "send_to_block_recipients") as send,
                  mock.patch.object(mac_test, "record_hold_event"),
                  mock.patch.object(mac_test, "skipped_ci_runs", return_value=""),
                  mock.patch.object(mac_queue_run, "wait_to_retry", side_effect=wait_first),
                  redirect_stdout(io.StringIO()) as first_output):
                self.assertEqual(mac_queue_run.main(args), 0)
            self.assertEqual((first_waits, second_waits), (2, 2))
            self.assertEqual(restore.call_count, 2)
            send.assert_called_once()
            self.assertFalse(block.exists())
            self.assertFalse((block.parent / "block-lift-failure.json").exists())
            for output in (first_output, second_output):
                self.assertEqual(output.getvalue().splitlines(), [
                    "Waiting for the Mac block held by fixture-holder: fixture work needs the Mac",
                    "Mac output", "mac_exit=0",
                ])

    def test_installed_run_refuses_a_mac_state_override_before_placement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            installed = installed_script(root, "mac_test/mac_queue_run.py")
            override = str(root / "private-mac-state")
            with (mock.patch.object(mac_queue_run, "__file__", str(installed.script)),
                  mock.patch.object(override_guard, "user_home", return_value=installed.home),
                  mock.patch.dict(os.environ, {"MAC_TEST_STATE_DIR": override}, clear=True),
                  mock.patch.object(mac_queue_run, "run") as run, redirect_stderr(io.StringIO()) as error,
                  self.assertRaises(SystemExit) as stopped):
                _ = mac_queue_run.main(self.command(root, "run")[3:])
            self.assertEqual(stopped.exception.code, override_guard.REFUSED_STATUS)
            self.assertIn(f"MAC_TEST_STATE_DIR is set to '{override}'", error.getvalue())
            run.assert_not_called()

    def test_run_starts_without_a_mac_block(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = subprocess.run(self.command(root, "run"), env=queue_fixture(root),
                                    capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(result.stdout.splitlines(), ["Mac output", "mac_exit=0"])
            self.assertTrue((root / "remote").is_file())

    def test_queued_mirror_command_waits_for_the_floor_and_holds_the_lease(self) -> None:
        self.check_queued_mirror_lease("hana", "hana", git=True)

    def test_queue_leases_the_resolved_cwd_even_for_a_different_or_non_git_worktree(self) -> None:
        for git in (True, False):
            with self.subTest(git=git):
                self.check_queued_mirror_lease("source", "other", git=git)

    def check_queued_mirror_lease(self, worktree: str, mirror_name: str, *, git: bool) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        checkout = root / worktree
        checkout.mkdir()
        if git:
            _ = subprocess.run(["git", "init", "-q", str(checkout)], check=True, capture_output=True)
            _ = subprocess.run(["git", "-C", str(checkout), "remote", "add", "origin",
                                f"https://example.test/{worktree}"], check=True, capture_output=True)
        environment = queue_fixture(root, ssh_source="import os, sys\nos.execl('/bin/zsh', 'zsh', '-c', sys.argv[-1])\n")
        _ = write_config_fixture(root / "config/mac_test.conf", "mirror_root=~/mirrors\nfloor_sweep_timeout_s=5\n")
        mirror = root / "mirrors" / mirror_name
        workdir = mirror / "subdir"
        workdir.mkdir(parents=True)
        alias = root / "alias"
        alias.symlink_to(workdir, target_is_directory=True)
        shim = root / ".claude/scripts/lib/py"
        shim.parent.mkdir(parents=True)
        _ = shim.write_text("#!/bin/sh\nexec " + shlex.quote(sys.executable) + ' "$@"\n')
        shim.chmod(0o755)
        started = root / "started"
        lease = floor_claims.mirror_lock_path(mirror)
        group = lease.with_name(lease.name + build_folders.MacFloorLeaseFile.GROUP.value)
        work = """import fcntl, sys
from pathlib import Path

assert Path.cwd() == Path(sys.argv[3]).resolve()
with Path(sys.argv[2]).open() as lock:
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        Path(sys.argv[1]).touch()
    else:
        raise SystemExit("queued command has no shared lease")
"""
        command = self.command(checkout, "run")[:-3]
        command[command.index("--cwd") + 1] = str(alias)
        command.extend((sys.executable, "-c", work, str(started), str(group), str(workdir)))
        with ExitStack() as floor:
            self.assertTrue(floor.enter_context(sweep.mac_floor_mirror_leases({mirror})))
            with subprocess.Popen(command, env=environment, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  bufsize=0, start_new_session=True) as process:
                try:
                    assert process.stdout is not None
                    stdout = cast(BinaryIO, process.stdout)
                    self.assertTrue(select.select([stdout], [], [], 8)[0])
                    self.assertEqual(stdout.readline(), b"Mac queue: camera shot\n")
                    self.assertTrue(select.select([stdout], [], [], 8)[0])
                    self.assertIn(b"waiting for the Mac floor sweep", stdout.readline())
                    self.assertFalse(started.exists())
                    floor.close()
                    output, _ = process.communicate(timeout=TEST_HANG_GUARD_SECS)
                    self.assertEqual(process.returncode, 0, output.decode())
                finally:
                    if process.poll() is None:
                        send_process_group(process.pid, signal.SIGKILL)
        self.assertTrue(started.is_file())

    def test_a_cwd_outside_the_mirror_does_not_wait_for_the_worktrees_lease(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        checkout = root / "source"
        _ = subprocess.run(["git", "init", "-q", str(checkout)], check=True, capture_output=True)
        _ = subprocess.run(["git", "-C", str(checkout), "remote", "add", "origin",
                            "https://example.test/source"], check=True, capture_output=True)
        environment = queue_fixture(root, ssh_source="import os, sys\nos.execl('/bin/zsh', 'zsh', '-c', sys.argv[-1])\n")
        _ = write_config_fixture(root / "config/mac_test.conf", "mirror_root=~/mirrors\nfloor_sweep_timeout_s=0.2\n")
        shim = root / ".claude/scripts/lib/py"
        shim.parent.mkdir(parents=True)
        _ = shim.write_text("#!/bin/sh\nexec " + shlex.quote(sys.executable) + ' "$@"\n')
        shim.chmod(0o755)
        command = self.command(checkout, "run")[:-3]
        command[command.index("--cwd") + 1] = str(checkout)
        command.append("pwd")
        with sweep.mac_floor_mirror_leases({root / "mirrors/source"}) as acquired:
            self.assertTrue(acquired)
            result = subprocess.run(command, env=environment, capture_output=True, text=True,
                                    timeout=TEST_HANG_GUARD_SECS, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(str(checkout.resolve()), result.stdout.splitlines())
        self.assertNotIn("the Mac floor sweep", result.stderr)

    def test_every_run_returns_the_placement_and_keeps_the_mac_status(self) -> None:
        cases = ((0, False, False), (23, False, False), (125, False, False), (2, True, False), (0, False, True), (2, True, True))
        for kind in ("shot", "install", "probe", "run"):
            for status, queue_refuses, return_fails in cases:
                with (self.subTest(kind=kind, status=status, queue_refuses=queue_refuses, return_fails=return_fails),
                      tempfile.TemporaryDirectory() as temporary):
                    self.check_returned_placement(Path(temporary), kind, status, queue_refuses, return_fails)

    def check_returned_placement(self, root: Path, kind: str, status: int, queue_refuses: bool, return_fails: bool) -> None:
        environment = {**queue_fixture(root), "FAKE_MAC_STATUS": str(status),
                       "FAKE_QUEUE_REFUSE": "1" if queue_refuses else "0", "FAKE_RETURN_FAIL": "1" if return_fails else "0"}
        result = subprocess.run(self.command(root, kind), capture_output=True, text=True, env=environment,
                                timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, status or (1 if return_fails else 0), result.stdout + result.stderr)
        events = self.events(root)
        # A Mac placement has no local grant, so each run returns it with a cancel.
        self.assertEqual([event[0] for event in events], ["place-step", "cancel-placement"])
        placed, released = events
        self.assertIn("--once", placed)
        self.assertIn("--wait-events", placed)
        self.assertEqual(placed[placed.index("--target") + 1], "mac")
        self.assertEqual(placed[placed.index("--kind") + 1], kind)
        self.assertEqual(released[released.index("--episode") + 1], placed[placed.index("--episode") + 1])
        self.assertEqual(released[released.index("--pid") + 1], placed[placed.index("--pid") + 1])
        # No receipt outlives its run, a refused return included.
        self.assertEqual(list((root / mac_queue_run.RECEIPTS).iterdir()), [])
        if return_fails:
            self.assertIn("the placement was not returned", result.stderr)
        if queue_refuses:
            self.assertEqual(released[released.index("--reason") + 1], "the Mac run did not start")
            self.assertFalse((root / "remote").exists())
        else:
            self.assertEqual(released[released.index("--reason") + 1], f"the Mac run ended with status {status}")
            remote = cast(list[str], json.loads((root / "remote").read_text()))
            wrapper = shlex.split(remote[2])
            work = shlex.split(wrapper[0].removeprefix("work="))
            self.assertEqual(work[0], "MAC_PLACEMENT=" + placed[placed.index("--episode") + 1] + "/attempt")
            self.assertEqual(work[1:3], ["/bin/zsh", "-c"])
            self.assertEqual(shlex.split(work[3])[-4:], ["/Mac scratch/checkout", *self.command(root)[-3:]])
            self.assertEqual(result.stdout.splitlines()[-1], f"mac_exit={status}")

    def test_no_wait_returns_the_busy_status_after_one_queue_call(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = {**queue_fixture(root), "FAKE_QUEUE_BUSY": "1"}
            command = self.command(root, "run")
            command.insert(command.index("--"), "--no-wait")
            result = subprocess.run(command, capture_output=True, text=True, env=environment,
                                    timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(result.returncode, mac_queue_run.MAC_BUSY_STATUS, result.stdout + result.stderr)
            self.assertEqual([event[0] for event in self.events(root)], ["place-step", "cancel-placement"])
            self.assertFalse((root / "remote").exists())

    def test_run_retries_the_busy_status_when_it_can_wait(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = {**queue_fixture(root), "FAKE_QUEUE_BUSY": "1"}

            def allow_placement() -> None:
                self.assertEqual([event[0] for event in self.events(root)], ["place-step"])
                self.assertFalse((root / "remote").exists())
                del os.environ["FAKE_QUEUE_BUSY"]

            with (mock.patch.dict(os.environ, environment, clear=True),
                  mock.patch.object(mac_queue_run, "wait_to_retry", side_effect=allow_placement) as wait,
                  redirect_stdout(io.StringIO()) as output):
                self.assertEqual(mac_queue_run.main(self.command(root, "run")[3:]), 0)

            wait.assert_called_once()
            self.assertEqual(output.getvalue().splitlines(), ["waiting for the Mac", "Mac output", "mac_exit=0"])
            self.assertEqual([event[0] for event in self.events(root)],
                             ["place-step", "place-step", "cancel-placement"])
            self.assertTrue((root / "remote").is_file())

    def test_caller_watch_must_be_ready_before_placement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = queue_fixture(root)
            reader, writer = os.pipe()
            os.close(writer)
            try:
                command = self.command(root)
                command[3:3] = ["--caller-fd", str(reader)]
                result = subprocess.run(command, env=environment, start_new_session=True, pass_fds=(reader,),
                                        capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS)
            finally:
                os.close(reader)
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertIn("caller watch did not start", result.stderr)
            self.assertFalse((root / "events").exists())

    def test_caller_watch_ends_a_watcher_without_a_complete_ready_line(self) -> None:
        for output in (b"", b"rea"):
            with self.subTest(output=output), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                environment = queue_fixture(root)
                _ = write_config_fixture(root / "config/mac_gate.conf", "placement_check_timeout_seconds=1\n")
                watcher = root / "watcher.py"
                _ = watcher.write_text(f"import os, sys\nos.write(1, {output!r})\nos.read(int(sys.argv[2]), 1)\n")
                queue_program = """\
import sys
from scripts.mac_test import mac_queue_run
mac_queue_run.__file__ = sys.argv[1]
raise SystemExit(mac_queue_run.main(sys.argv[2:]))
"""
                reader, writer = os.pipe()
                try:
                    command = [sys.executable, "-c", queue_program, str(watcher), "--caller-fd", str(reader),
                               *self.command(root)[3:]]
                    # The watcher holds stderr open until it ends. Keep its caller pipe open.
                    result = subprocess.run(command, env=environment, start_new_session=True, pass_fds=(reader,),
                                            capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS)
                finally:
                    os.close(reader)
                    os.close(writer)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertEqual(result.stderr, "mac queue: the Mac queue caller watch did not start\n")
                self.assertFalse((root / "events").exists())

    def test_a_caller_watch_failure_after_ready_stops_the_queue_group(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = {**queue_fixture(root), "FAKE_SSH_HOLD": "1"}
            reader, writer = os.pipe()
            try:
                with os.fdopen(writer, "wb") as caller:
                    command = [sys.executable, str(queue_with_ready_event(root)), "--caller-fd", str(reader),
                               *self.command(root)[3:]]
                    queue = subprocess.Popen(command, env=environment, start_new_session=True, pass_fds=(reader,),
                                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                    assert queue.stdout is not None and queue.stderr is not None
                    queue_output = cast(TextIO, queue.stdout)
                    try:
                        with queue_output, queue.stderr:
                            self.assertTrue(select.select([queue_output], [], [], TEST_HANG_GUARD_SECS)[0])
                            self.assertEqual(queue_output.readline().strip(), "Mac command started")
                            processes = subprocess.check_output(["ps", "-axo", "pid=,ppid=,args="], text=True,
                                                                timeout=TEST_HANG_GUARD_SECS)
                            watchers: list[int] = []
                            for line in processes.splitlines():
                                fields = line.split(maxsplit=2)
                                if len(fields) == 3 and fields[1] == str(queue.pid) and " watch-caller " in fields[2]:
                                    watchers.append(int(fields[0]))
                            self.assertEqual(len(watchers), 1)
                            os.kill(watchers[0], signal.SIGKILL)
                            output, errors = queue.communicate(timeout=10)
                    finally:
                        end_queue_fixture(queue, caller, queue_group=True)
                self.assertEqual(queue.returncode, -signal.SIGTERM, output + errors)
                self.assertIn("caller watch ended unexpectedly", errors)
                self.assertEqual([event[0] for event in self.events(root)], ["place-step", "cancel-placement"])
                self.assertIn("signal_group TERM", (root / "stop").read_text())
            finally:
                os.close(reader)

    def test_caller_watch_accepts_ready_after_the_termination_grace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = queue_fixture(root)
            grace = conf.read(root / "config/mac_gate.conf").number("terminate_grace_seconds")
            watcher = root / "watcher.py"
            _ = watcher.write_text(
                """\
import os, sys
print('watcher started', file=sys.stderr, flush=True)
assert os.read(int(sys.argv[2]), 1) == b'x'
print('ready', flush=True)
assert os.read(int(sys.argv[3]), 1) == b''
""")
            queue_program = """\
import sys
from scripts.mac_test import mac_queue_run
mac_queue_run.__file__ = sys.argv[1]
with mac_queue_run.caller_watch(int(sys.argv[2])):
    print('queue ready', flush=True)
"""
            reader, writer = os.pipe()
            try:
                with subprocess.Popen([sys.executable, "-c", queue_program, str(watcher), str(reader)],
                                      env=environment, start_new_session=True, pass_fds=(reader,),
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as queue:
                    try:
                        assert queue.stderr is not None
                        self.assertTrue(select.select([queue.stderr], [], [], TEST_HANG_GUARD_SECS)[0])
                        self.assertEqual(queue.stderr.readline(), "watcher started\n")
                        with self.assertRaises(subprocess.TimeoutExpired):
                            _ = queue.wait(timeout=2 * grace)
                        self.assertEqual(os.write(writer, b"x"), 1)
                        output, errors = queue.communicate(timeout=TEST_HANG_GUARD_SECS)
                        self.assertEqual(queue.returncode, 0, errors)
                        self.assertEqual(output, "queue ready\n")
                    finally:
                        if queue.poll() is None:
                            queue.kill()
            finally:
                os.close(reader)
                os.close(writer)

    def test_ended_gate_uses_the_installed_script_with_an_old_or_missing_callback(self) -> None:
        for old_exists in (False, True):
            with self.subTest(old_exists=old_exists), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                environment = queue_fixture(root)
                stale = root / mac_queue_run.RUNS / "stale"
                stale.mkdir(parents=True)
                (stale / "lock").touch()
                receipt = root / mac_queue_run.RECEIPTS / "stale.json"
                receipt.parent.mkdir(parents=True)
                _ = receipt.write_text("{}")
                old = root / "old worktree/mac_gate.py"
                if old_exists:
                    old.parent.mkdir()
                    write_stdlib_tool(old, "raise SystemExit('The queue used the old callback')\n")
                installed = root / ".claude/scripts/mac_test/mac_gate.py"
                installed.parent.mkdir(parents=True)
                write_stdlib_tool(installed, "import os, sys\nprint(os.environ['MAC_QUEUE_HOST'], *sys.argv[1:])\n")
                attempt = root / "mac-gate-fixture"
                _ = (stale / "run.json").write_text(json.dumps({"lifecycle_script": str(old),
                    "arguments": [str(attempt), "1"], "host": "saved-mac"}))
                result = subprocess.run(self.command(root, "run"), env=environment, capture_output=True,
                                        text=True, timeout=TEST_HANG_GUARD_SECS)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn(f"saved-mac cleanup-ended {stale / 'argv.json'} {attempt} 1", result.stdout)
                self.assertNotIn("cleanup of an ended run failed", result.stderr)
                self.assertFalse(stale.exists())
                self.assertFalse(receipt.exists())
                self.assertEqual([event[0] for event in self.events(root)], ["place-step", "cancel-placement"])

    def test_failed_cleanup_leaves_the_record_for_the_next_run(self) -> None:
        lifecycle = """
import json, os, sys
from pathlib import Path
phase, argv_file, *command = sys.argv[1:]
if phase == 'prepare':
    Path(argv_file).write_text(json.dumps(command))
else:
    with (Path(os.environ['FAKE_QUEUE_STATE']) / 'cleanup_calls').open('a') as out:
        print(sys.argv[0], phase, argv_file, file=out)
    raise SystemExit(int(os.environ.get('FAKE_CLEANUP_STATUS', '0')))
"""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = queue_fixture(root)
            callback = root / "mac_gate.py"
            installed = root / ".claude/scripts/mac_test/mac_gate.py"
            installed.parent.mkdir(parents=True)
            for script in (callback, installed):
                write_stdlib_tool(script, lifecycle)
            command = self.command(root, "run")
            command[command.index("--"):command.index("--")] = ["--lifecycle-script", str(callback)]
            result = subprocess.run(command, env={**environment, "FAKE_CLEANUP_STATUS": "19"},
                                    capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(result.returncode, 19, result.stdout + result.stderr)
            (saved,) = (root / mac_queue_run.RUNS).glob("*/run.json")
            recovered = subprocess.run(self.command(root, "run"), env=environment,
                                       capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(recovered.returncode, 0, recovered.stdout + recovered.stderr)
            called = str(saved.with_name("argv.json"))
            self.assertEqual((root / "cleanup_calls").read_text().splitlines(),
                             [f"{callback} cleanup {called}", f"{installed} cleanup-ended {called}"])
            self.assertFalse(saved.exists())

    def smoke_fixture(self, root: Path, work: str) -> tuple[dict[str, str], list[str]]:
        environment = queue_fixture(root, """
import json, os, shlex, signal, subprocess, sys
from pathlib import Path
root = Path(os.environ["FAKE_QUEUE_STATE"])
command = shlex.split(sys.argv[-1])
if " smoke-status " in command[-1]:
    assert (root / "input_closed").exists(), "Status read before the first SSH call ended"
    (root / "status-read").touch()
    if os.environ.get("FAKE_STATUS_STALL") == "1":
        signal.alarm(3)
        with (root / "status-gate").open("rb"):
            raise AssertionError("The status gate opened")
    _, _, _, action, episode = shlex.split(command[-1])
    subprocess.run([sys.executable, os.environ["SMOKE_WORKER"], action, episode], check=False)
    raise SystemExit(0)
(root / "smoke-ssh.json").write_text(json.dumps(sys.argv[1:]))
drop = os.environ.get("FAKE_SMOKE_DROP")
if drop:
    with subprocess.Popen(command, cwd=root / "checkout", stdin=subprocess.PIPE, stdout=subprocess.PIPE) as worker:
        if drop == "ready":
            for line in worker.stdout:
                sys.stdout.buffer.write(line)
                sys.stdout.buffer.flush()
                if line.startswith(b"mac_smoke_ready="):
                    break
            else:
                raise AssertionError("The worker did not report readiness")
        else:
            assert worker.stdout.readline() == b"work-ready" + bytes([10])
        worker.stdin.close()
        worker.stdin = None
        worker.communicate()
    if drop == "ready":
        (root / "input_closed").touch()
        raise SystemExit(255)
else:
    subprocess.run(command, cwd=root / "checkout", check=False)
    for status in Path(os.environ["SCRATCH_ROOT"]).glob("mac-smoke-*/status"):
        if os.environ.get("FAKE_SMOKE_FILE") == "missing":
            status.unlink()
        elif os.environ.get("FAKE_SMOKE_FILE") == "malformed":
            status.write_text("256" + chr(10))
(root / "input_closed").touch()
raise SystemExit(0)
""")
        (root / "checkout").mkdir()
        write_stdlib_tool(root / "copies.py", """
import os, subprocess, sys
from pathlib import Path
root = Path(os.environ["FAKE_QUEUE_STATE"])
status = subprocess.run(sys.argv[1:], check=False).returncode
(root / "worker-exit").write_text(str(status))
episode = os.environ["MAC_PLACEMENT"].split("/")[0]
path = Path(os.environ["SCRATCH_ROOT"]) / f"mac-smoke-{episode}" / "status"
if path.exists():
    (root / "early-status").write_text(path.read_text())
raise SystemExit(int(os.environ.get("FAKE_SMOKE_LEASE_STATUS", status)))
""")
        environment.update(COPIES_KEEP_FILE=str(root / "copy.keep"), SCRATCH_ROOT=str(root / "scratch"),
                           SMOKE_WORKER=str(Path(mac_queue_run.__file__).resolve()), FAKE_SMOKE_WORK=work)
        callback = root / "mac_run.sh"
        write_stdlib_tool(callback, """
import json, os, sys
from pathlib import Path
root = Path(os.environ["FAKE_QUEUE_STATE"])
phase, argv_file = sys.argv[1:3]
if phase == "prepare":
    cleanup = 'touch "$FAKE_QUEUE_STATE/remote_cleanup"; exit "${FAKE_MAC_STATUS:-0}"'
    runner = [sys.executable, os.environ["SMOKE_WORKER"]]
    Path(argv_file).write_text(json.dumps([
        *runner, "smoke-copy", str(root / "copies.py"),
        *runner, "smoke-work", os.environ["FAKE_SMOKE_WORK"], cleanup,
    ]))
else:
    assert (root / "remote_cleanup").exists(), "Local cleanup started before remote cleanup"
    if not os.environ.get("FAKE_SMOKE_DROP"):
        assert not (root / "input_closed").exists(), "SSH closed before local cleanup"
    (root / "cleanup").touch()
""")
        command = self.command(root, "run")
        command[command.index("--"):command.index("--")] = ["--lifecycle-script", str(callback)]
        return environment, command

    def test_smoke_cleanup_runs_before_ssh_input_closes(self) -> None:
        for status in (0, 4):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                environment, command = self.smoke_fixture(root, "true")
                result = subprocess.run(command, env={**environment, "FAKE_MAC_STATUS": str(status)},
                                        capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS)
                self.assertEqual(result.returncode, status, result.stdout + result.stderr)
                self.assertTrue((root / "cleanup").exists())
                self.assertTrue((root / "input_closed").exists())
                self.assertEqual(list((root / "scratch").iterdir()), [])

    def test_smoke_uses_the_remote_status_when_tailscale_exits_zero(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment, command = self.smoke_fixture(root, "print mac_exit=0; exit 7")
            result = subprocess.run(command, env=environment, capture_output=True, text=True,
                                    timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(result.returncode, 7, result.stdout + result.stderr)
            self.assertIn("mac_exit=0", result.stdout.splitlines())
            self.assertTrue((root / "status-read").exists())
            ssh_arguments = cast(list[str], json.loads((root / "smoke-ssh.json").read_text()))
            self.assertEqual(ssh_arguments[:-1], ["-o", "BatchMode=yes", "fake-mac"])
            remote_command = shlex.split(ssh_arguments[-1])
            self.assertEqual(remote_command[:-1], ["/bin/zsh", "-f", "-c"])
            self.assertIn("smoke-copy", shlex.split(remote_command[-1]))
            self.assertIn("smoke-work", shlex.split(remote_command[-1]))
            self.assertTrue(all("MAC_SMOKE_COOKIE" not in argument for argument in ssh_arguments))
            self.assertEqual([event[0] for event in self.events(root)], ["place-step", "cancel-placement"])

    def test_smoke_rejects_missing_or_malformed_status(self) -> None:
        for status in ("missing", "malformed"):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                environment, command = self.smoke_fixture(root, "true")
                result = subprocess.run(command, env={**environment, "FAKE_SMOKE_FILE": status},
                                        capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS)
                self.assertEqual(result.returncode, 255, result.stdout + result.stderr)
                self.assertIn("no valid final status", result.stderr)
                self.assertTrue((root / "status-read").exists())
                self.assertEqual([event[0] for event in self.events(root)], ["place-step"])
                self.assertEqual(len(list((root / mac_queue_run.RUNS).glob("*/run.json"))), 1)

    def test_smoke_rejects_a_disconnect_before_status(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            gate = root / "gate"
            os.mkfifo(gate)
            environment, command = self.smoke_fixture(root, "print work-ready; cat " + shlex.quote(str(gate)))
            result = subprocess.run(command, env={**environment, "FAKE_SMOKE_DROP": "1"},
                                    capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(result.returncode, 255, result.stdout + result.stderr)
            self.assertIn("the ready line is missing", result.stderr)
            self.assertTrue((root / "remote_cleanup").exists())
            self.assertFalse((root / "cleanup").exists())
            self.assertFalse((root / "status-read").exists())
            placed, = self.events(root)
            self.assertEqual(placed[0], "place-step")
            self.assertEqual(len(list((root / mac_queue_run.RUNS).glob("*/run.json"))), 1)
            episode = placed[placed.index("--episode") + 1]
            scratch_root = Path(environment["SCRATCH_ROOT"])
            folder = scratch_root / f"mac-smoke-{episode}"
            status_file = folder / "status"
            self.assertTrue(status_file.is_file(), "The status is missing from the scratch folder")
            self.assertEqual(status_file.read_text(), "1\n")
            proc = root / "proc"
            proc.mkdir()
            sessions = root / "sessions"
            sessions.mkdir()
            environment["NOTIFIER_SESSIONS_DIR"] = str(sessions)
            _ = Path(environment["STAFF_FILE"]).write_text(staff.StaffSnapshot(()).encode())
            plan = removal_guard.RemovalList(removal_guard.Tool.TEMPORARY)
            with mock.patch.dict(os.environ, environment, clear=True):
                now = time.time()
                self.assertIsInstance(scratch.record_lifetime(folder, now), scratch.Live)
                self.assertTrue(scratch.is_throwaway(folder))
                expiry = now + conf.read("scratch").whole("default_life_seconds")
                roots = tmp_sweep.Roots((), (), (), scratch=(str(scratch_root),))
                sweep = tmp_sweep.sweep(False, roots, expiry, str(proc), plan=plan)
            self.assertIn(folder, plan.rows, sweep)
            self.assertEqual(plan.rows[folder].fingerprint, "")
            self.assertEqual(status_file.read_text(), "1\n")

    def test_smoke_disconnect_after_ready_does_not_publish_success(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment, command = self.smoke_fixture(root, "true")
            result = subprocess.run(command, env={**environment, "FAKE_SMOKE_DROP": "ready"},
                                    capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(result.returncode, 255, result.stdout + result.stderr)
            self.assertTrue((root / "remote_cleanup").exists())
            self.assertFalse((root / "status-read").exists())
            self.assertEqual((root / "worker-exit").read_text(), "1")
            placed, = self.events(root)
            self.assertEqual(placed[0], "place-step")
            episode = placed[placed.index("--episode") + 1]
            status_file = root / "scratch" / f"mac-smoke-{episode}" / "status"
            self.assertTrue(not status_file.exists() or status_file.read_text() != "0\n")

    def test_smoke_reports_a_failed_copy_lease_close(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment, command = self.smoke_fixture(root, "true")
            result = subprocess.run(command, env={**environment, "FAKE_SMOKE_LEASE_STATUS": "19"},
                                    capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(result.returncode, 19, result.stdout + result.stderr)
            self.assertEqual((root / "worker-exit").read_text(), "0")
            self.assertFalse((root / "early-status").exists())
            self.assertTrue((root / "status-read").exists())
            self.assertEqual([event[0] for event in self.events(root)], ["place-step", "cancel-placement"])

    def test_smoke_status_read_uses_the_probe_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment, command = self.smoke_fixture(root, "true")
            _ = write_config_fixture(root / "config/mac_test.conf", "probe_timeout_s=0.5\n")
            os.mkfifo(root / "status-gate")
            result = subprocess.run(command, env={**environment, "FAKE_STATUS_STALL": "1"},
                                    capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(result.returncode, 255, result.stdout + result.stderr)
            self.assertIn("status read timed out after 0.5 seconds", result.stderr)
            self.assertTrue((root / "status-read").exists())
            self.assertEqual([event[0] for event in self.events(root)], ["place-step"])

    def test_smoke_release_timeout_closes_the_lease_with_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment, _ = self.smoke_fixture(root, "true")
            _ = write_config_fixture(root / "config/production.conf", "mac_run_status_grace_seconds=1\n")
            episode = "73b79143c2c846a18d3bdb66b5dd15a0"
            environment["MAC_PLACEMENT"] = episode + "/attempt"
            runner = [sys.executable, environment["SMOKE_WORKER"]]
            with subprocess.Popen(
                [*runner, "smoke-copy", str(root / "copies.py"), *runner, "smoke-work", "true", "true"],
                cwd=root / "checkout", env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            ) as process:
                assert process.stdin is not None and process.stdout is not None
                output = cast(BinaryIO, process.stdout)
                try:
                    self.assertTrue(select.select([output], [], [], TEST_HANG_GUARD_SECS)[0])
                    self.assertEqual(output.readline(), b"\n")
                    self.assertEqual(output.readline(), f"mac_smoke_ready={episode}\n".encode())
                    try:
                        self.assertEqual(process.wait(timeout=5), 0)
                    except subprocess.TimeoutExpired:
                        self.fail("The copy lease stayed open after the release deadline")
                    self.assertIn(b"copy release timed out after 1s", output.read())
                    self.assertEqual((root / "worker-exit").read_text(), "1")
                    self.assertEqual((root / "scratch" / f"mac-smoke-{episode}" / "status").read_text(), "1\n")
                finally:
                    process.stdin.close()

    def test_ended_smoke_retires_the_legacy_scratch_record_after_cleanup(self) -> None:
        self.check_legacy_scratch_cleanup(clear=True)

    def test_ended_smoke_keeps_the_legacy_scratch_record_when_cleanup_fails(self) -> None:
        self.check_legacy_scratch_cleanup(clear=False)

    def test_ended_smoke_lists_the_legacy_task_without_a_checkout(self) -> None:
        self.check_legacy_scratch_cleanup(clear=False, checkout_known=False)

    def check_legacy_scratch_cleanup(self, *, clear: bool, checkout_known: bool = True) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = queue_fixture(root, SSH.replace(
                '(root / "remote").write_text(json.dumps(args))',
                """
if "stop-checkout" in sys.argv[-1]:
    (root / "legacy-host").write_text(sys.argv[-2])
    subprocess.run(["/bin/zsh", "-c", sys.argv[-1]], check=False)
    raise SystemExit(0)
(root / "remote").write_text(json.dumps(args))
""").replace("import json,", "import subprocess, json,"))
            directory = root / mac_queue_run.RUNS / "legacy"
            directory.mkdir(parents=True)
            (directory / "lock").touch()
            state = directory / "argv.json.state"
            bundle = root / "legacy bundle"
            bundle.touch()
            checkout = str(root / "old checkout") if checkout_known else ""
            task = "gate:mac-run-fixture"
            old_state = f"clone={shlex.quote(str(checkout))}\nclone_task={task}\nbundle={shlex.quote(str(bundle))}\n"
            _ = state.write_text(old_state)
            receipt = root / mac_queue_run.RECEIPTS / "legacy.json"
            receipt.parent.mkdir(parents=True)
            receipt.touch()
            record = directory / "run.json"
            _ = record.write_text(json.dumps({"lifecycle_script": "missing/mac_run.sh",
                                             "arguments": [], "host": "saved-mac"}))
            installed = root / ".claude" / mac_queue_run.MAC_RUN_SCRIPT
            installed.parent.mkdir(parents=True)
            _ = shutil.copy2(Path(__file__).resolve().parents[2] / mac_queue_run.MAC_RUN_SCRIPT, installed)
            py = installed.parents[1] / "lib/py"
            py.parent.mkdir()
            write_stdlib_tool(py, """
import json, os, sys
from pathlib import Path
root = Path(os.environ["FAKE_QUEUE_STATE"])
with (root / "legacy-actions").open("a") as stream:
    print(json.dumps([Path(sys.argv[1]).name, *sys.argv[2:]]), file=stream)
if sys.argv[2] == "stop-checkout":
    print("hana-instances: checkout clear")
    sys.exit(int(os.environ["FAKE_STOP_CHECKOUT_STATUS"]))
""")
            result = subprocess.run(self.command(root),
                                    env={**environment, "FAKE_STOP_CHECKOUT_STATUS": str(int(not clear))},
                                    capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(record.exists(), not clear, result.stdout + result.stderr)
            self.assertEqual(receipt.exists(), not clear)
            self.assertEqual(bundle.exists(), not clear)
            if not checkout_known:
                self.assertEqual(state.read_text(), old_state)
                self.assertIn(f"kept for a manual removal list: {state} (scratch, task {task} has no checkout path)",
                              result.stderr)
                self.assertFalse((root / "legacy-host").exists())
                self.assertFalse((root / "legacy-actions").exists())
                return
            self.assertTrue((root / "legacy-host").exists(), result.stdout + result.stderr)
            self.assertEqual((root / "legacy-host").read_text(), "saved-mac")
            actions = [cast(list[str], json.loads(line)) for line in (root / "legacy-actions").read_text().splitlines()]
            expected = [["hana_instances.py", "stop-checkout", str(checkout)]]
            if clear:
                expected.extend([
                    ["scratch.py", "end", "--task", task],
                    ["copies.py", "unregister-remote", "--host", "localhost", "--path", str(checkout)],
                    ["copies.py", "unregister-remote", "--host", "saved-mac", "--path", str(checkout)],
                ])
            else:
                self.assertEqual(state.read_text(), old_state)
                self.assertIn("scratch cleanup unconfirmed", result.stderr)
            self.assertEqual(actions, expected)

    def test_smoke_worker_checks_outside_the_copy_and_requires_explicit_release(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            checkout = root / "checkout"
            checkout.mkdir()
            keep = root / "1.keep"
            cleanup_record = root / "cleanup"
            source = Path(mac_queue_run.__file__).resolve()
            episode = "73b79143c2c846a18d3bdb66b5dd15a0"
            status_file = root / "scratch" / f"mac-smoke-{episode}" / "status"
            status_file.parent.mkdir(parents=True)
            check_stdin = shlex.join([sys.executable, "-c",
                "import os; assert os.path.samestat(os.fstat(0), os.stat(os.devnull))"])
            for disconnected in (False, True):
                with self.subTest(disconnected=disconnected):
                    cleanup_record.unlink(missing_ok=True)
                    _ = status_file.write_text("1\n")
                    fifo = root / "gate"
                    fifo.unlink(missing_ok=True)
                    if disconnected:
                        os.mkfifo(fifo)
                    work = check_stdin + " && print -r -- work-ready"
                    if disconnected:
                        work += "; cat " + shlex.quote(str(fifo))
                    cleanup = (check_stdin + ' && print -rl -- "$PWD" "$1" > "$CLEANUP_RECORD"'
                               + ' && rm -- "$COPIES_KEEP_FILE" "$MAC_SMOKE_LAUNCH_TOKEN"')
                    runner = [sys.executable, str(source)]
                    with subprocess.Popen(
                        [*runner, "smoke-copy", *runner, "smoke-work", work, cleanup],
                        cwd=checkout, env={**os.environ, **config_environment(root / "config"),
                                           "HOME": str(root), "SCRATCH_ROOT": str(root / "scratch"),
                                           "COPIES_COMMAND_LEASE_FDS": "",
                                           "MAC_PLACEMENT": episode + "/attempt",
                                           "COPIES_KEEP_FILE": str(keep), "CLEANUP_RECORD": str(cleanup_record)},
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    ) as process:
                        assert process.stdin is not None and process.stdout is not None
                        try:
                            self.assertTrue(select.select([process.stdout], [], [], TEST_HANG_GUARD_SECS)[0])
                            self.assertEqual(process.stdout.readline(), b"work-ready\n")
                            self.assertFalse(status_file.exists())
                            if not disconnected:
                                self.assertEqual(process.stdout.readline(), b"\n")
                                self.assertEqual(process.stdout.readline(), f"mac_smoke_ready={episode}\n".encode())
                                self.assertIsNone(process.poll())
                                self.assertTrue(cleanup_record.exists())
                                self.assertFalse(status_file.exists())
                                _ = process.stdin.write(b"\n")
                            else:
                                self.assertTrue(keep.exists())
                                self.assertFalse(cleanup_record.exists())
                            process.stdin.close()
                            self.assertEqual(process.wait(timeout=TEST_HANG_GUARD_SECS), 0)
                        finally:
                            if process.poll() is None:
                                process.kill()
                                _ = process.wait(timeout=TEST_HANG_GUARD_SECS)
                    self.assertEqual(cleanup_record.read_text().splitlines(), [str(root), str(checkout)])
                    self.assertFalse(keep.exists())
                    self.assertEqual(status_file.read_text(), f"{int(disconnected)}\n")

    def test_ended_custom_callback_uses_its_saved_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = queue_fixture(root)
            directory = root / mac_queue_run.RUNS / "ended"
            directory.mkdir(parents=True)
            (directory / "lock").touch()
            callback = root / "custom_callback"
            write_stdlib_tool(callback, "import sys\nprint(*sys.argv[1:])\n")
            _ = (directory / "run.json").write_text(json.dumps({"lifecycle_script": str(callback),
                "arguments": ["fixture-argument"], "host": "fake-mac"}))
            result = subprocess.run(self.command(root, "run"), env=environment, capture_output=True,
                                    text=True, timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn(f"cleanup {directory / 'argv.json'} fixture-argument", result.stdout)
            self.assertFalse(directory.exists())

    def test_a_signal_ends_the_run_with_its_default_action(self) -> None:
        for waiting, signum in ((False, signal.SIGTERM), (False, signal.SIGHUP), (True, signal.SIGTERM),
                                (False, signal.SIGINT)):
            with self.subTest(waiting=waiting, signum=signum), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                environment = {**queue_fixture(root), "FAKE_QUEUE_WAIT": "1" if waiting else "0", "FAKE_SSH_HOLD": "1"}
                with subprocess.Popen(self.command(root), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                      text=True, env=environment, start_new_session=True) as process:
                    try:
                        if waiting:
                            wait_until((root / "waiting").exists, root, processes=(process,))
                        else:
                            # Wait for the queue to forward SSH's last line before sending the signal.
                            assert process.stdout is not None
                            queue_output = cast(TextIO, process.stdout)
                            self.assertTrue(select.select([queue_output], [], [], TEST_HANG_GUARD_SECS)[0])
                            self.assertEqual(queue_output.readline().strip(), "Mac command started")
                        if waiting:
                            # A signal to the group also ends the queue call that waits.
                            send_process_group(process.pid, signum)
                        else:
                            process.send_signal(signum)
                        output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
                    finally:
                        if process.poll() is None:
                            send_process_group(process.pid, signal.SIGKILL)
                            _ = process.communicate()
                self.assertEqual(process.returncode, -signum, output + errors)
                events = [event[0] for event in self.events(root)]
                if signum == signal.SIGINT:
                    # Python's own KeyboardInterrupt runs the finally path, which returns the placement.
                    self.assertEqual(events, ["place-step", "cancel-placement"])
                    self.assertEqual(self.events(root)[-1][-1], "the Mac run ended with status 1")
                    self.assertIn("signal_group TERM", (root / "stop").read_text())
                else:
                    # The queue ends the placement when this process ends.
                    self.assertEqual(events, ["place-step"])
                    if not waiting:
                        # The SSH fixture keeps the captured stderr open until it exits.
                        self.assertTrue((root / "input_closed").exists(), errors)
                    # The run's state folder stays until the next run cleans it.
                    runs = root / mac_queue_run.RUNS
                    self.assertEqual(len(list(runs.iterdir())), 1)
                    self.assertEqual(len(list((root / mac_queue_run.RECEIPTS).iterdir())), 1)
                    after = subprocess.run(self.command(root), capture_output=True, text=True,
                                           env={**environment, "FAKE_SSH_HOLD": "0", "FAKE_QUEUE_WAIT": "0"},
                                           timeout=TEST_HANG_GUARD_SECS)
                    self.assertEqual(after.returncode, 0, after.stdout + after.stderr)
                    self.assertEqual(list(runs.iterdir()), [])
                    self.assertEqual(list((root / mac_queue_run.RECEIPTS).iterdir()), [])

    def test_the_mac_work_ends_when_the_ssh_input_closes(self) -> None:
        zsh, setsid = shutil.which("zsh"), shutil.which("setsid")
        if zsh is None or setsid is None:
            self.skipTest("zsh or setsid is not installed")
        for closes in (True, False):
            with self.subTest(closes=closes), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                # The app starts outside the group, as Terminal starts hana on the Mac.
                work = (f"{setsid} -f sh -c 'echo $$ > {root}/app.new; mv {root}/app.new {root}/app; exec sleep 60' </dev/null >/dev/null 2>&1; "
                        + f"while [[ ! -s {root}/app ]]; do sleep 0.05; done; echo started $$; "
                        + ("exec sleep 60" if closes else "exit 7"))
                on_close = (f"kill -0 $(<{root}/work) 2>/dev/null && print alive > {root}/order"
                            + f" || print ended > {root}/order; kill -TERM $(<{root}/app)")
                wrapper = mac_transport.remote_wrapper(work, on_close, 20).replace("/bin/zsh", zsh)
                # Its own session stands in for the SSH session on the Mac.
                with subprocess.Popen([zsh, "-c", wrapper], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      text=True, start_new_session=True) as process:
                    try:
                        assert process.stdout is not None and process.stdin is not None
                        self.assertEqual(process.stdout.readline(), f"mac_group={process.pid}\n")
                        started = cast(str, process.stdout.readline())
                        self.assertRegex(started, r"\Astarted \d+\n\Z")
                        _ = (root / "work").write_text(started.split()[1])
                        app_end = os.pidfd_open(int((root / "app").read_text()))
                        self.addCleanup(os.close, app_end)
                        if closes:
                            work_end = os.pidfd_open(int(started.split()[1]))
                            self.addCleanup(os.close, work_end)
                            process.stdin.close()
                            for end in (work_end, app_end):
                                self.assertEqual(select.select([end], [], [], TEST_HANG_GUARD_SECS)[0], [end])
                            self.assertEqual(process.wait(timeout=TEST_HANG_GUARD_SECS), -signal.SIGTERM)
                            # The group ends first; then the close step stops the app.
                            self.assertEqual((root / "order").read_text(), "ended\n")
                        else:
                            output = cast(str, process.stdout.read())
                            self.assertEqual(process.wait(timeout=TEST_HANG_GUARD_SECS), 0)
                            self.assertEqual(output.splitlines()[-1], "mac_exit=7")
                            # A run that ends by itself leaves the close step to its own cleanup.
                            self.assertFalse((root / "order").exists())
                            os.kill(int((root / "app").read_text()), signal.SIGTERM)
                    finally:
                        if process.poll() is None:
                            send_process_group(process.pid, signal.SIGKILL)
                            _ = process.wait()

    def test_callers_have_no_plain_ssh_build_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _ = (root / "caller.sh").write_text("ssh mac cargo build\n")
            self.assertEqual(len(plain_ssh_builds(root)), 1)
        self.assertEqual(plain_ssh_builds(Path(__file__).resolve().parents[1]), [])

    def test_the_queue_files_set_no_signal_handler(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            lines = ("signal.signal(signal.SIGTERM, stop)", "trap cleanup EXIT", "  trap - INT",
                     "TRAPINT() { exit }", "for each in (signal.SIG_IGN,): pass")
            for number, line in enumerate(lines):
                _ = (root / f"case{number}").write_text("# trap is fine in a comment\n" + line + "\n")
            self.assertEqual(len(signal_handlers(root, tuple(f"case{number}" for number in range(len(lines))))), len(lines))
        self.assertEqual(signal_handlers(Path(__file__).resolve().parents[1], NO_HANDLER_FILES), [])


if __name__ == "__main__":
    _ = unittest.main()
