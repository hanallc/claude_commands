"""Exercise placement return and SSH routing with executable stand-ins."""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import ctypes
import errno
import io
import json
import os
from pathlib import Path
import re
import select
import shlex
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
from typing import cast
import unittest
from unittest import mock

from scripts.direct.wait_for_test_state import wait_until
from scripts.lib.process_group import send_process_group
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.mac_test import mac_process_environment, mac_queue_run, mac_test
from scripts.lint.memory_test_support import config_environment, write_config_fixture


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
    if os.environ.get("FAKE_QUEUE_WAIT") == "1":
        (root / "waiting").write_text("waiting")
        time.sleep(60)
    if os.environ.get("FAKE_QUEUE_REFUSE") == "1":
        print("refused\tclosed")
        raise SystemExit(2)
    sys.path.insert(0, os.environ["FAKE_QUEUE_PROJECT"])
    from scripts.build_hold.owning_session import NoOwningSession
    from scripts.build_queue import admission_wire as wire, execution_identity, placement
    from scripts.lib import conf, process_start
    from scripts.mac_test import claim_state, mac_test
    pid = int(option("--pid"))
    start = process_start.process_start(pid)
    assert isinstance(start, process_start.Started)
    assignment = placement.PlacementAssignment(option("--episode"), "attempt",
        placement.PlacementDestination("mac", "mac", "fake-mac", claim_state.RepositoryScope("hana")),
        execution_identity.SourceKeyKnown("fixture"), placement.SubmittingOwner("fixture", pid, start.start),
        NoOwningSession(), float(option("--since")))
    reserved = mac_test.reserve_mac_placement(claim_state.state_paths(), assignment, what="fixture",
        worktree=option("--repo-root"), copy=False, uses_claim=False, configuration=conf.read("mac_test"))
    assert isinstance(reserved, placement.AllowedHost), reserved
    wire.write_assignment(Path(option("--assignment-file")), wire.Assignment(assignment))
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
lost_group = os.environ.get("FAKE_LOST_GROUP")
if "dispatch_start_prepared=" in sys.argv[-1]:
    print(args[-1])
    raise SystemExit(0)
if "leftovers=0" in sys.argv[-1] or "settlement=$(" in sys.argv[-1]:
    key, = (root / "mac-state" / "pending-starts").iterdir()
    count = int(os.environ.get("FAKE_PLACEMENT_REMAINS", "0"))
    if lost_group == "children" and not (root / "children-ended").exists():
        count += 1
    print(f"dispatch_start_settled={key.name}\nleftovers={count}\nrc=0")
    raise SystemExit(0)
if "mac_stopped=" in args[-1]:
    owner = "original"
    with (root / "signals").open("w") as signals:
        for line in args[-1].splitlines():
            if line.startswith("signal_group "):
                signals.write(f"{line.split()[1]} {owner}\n")
            if lost_group == "reused" and line.startswith("sleep "):
                owner = "replacement"
    if lost_group == "children":
        (root / "children-ended").touch()
    print("mac_stopped=4567", flush=True)
    raise SystemExit(0)
(root / "remote").write_text(json.dumps(args))
if "mac_group=" in args[-1]:
    print("mac_group=4567", flush=True)
if os.environ.get("FAKE_SSH_LOST") == "1":
    raise SystemExit(255)
if os.environ.get("FAKE_SSH_HOLD") == "1":
    (root / "running").write_text(str(os.getpid()))
    # The real ssh sends the end of its input to the Mac wrapper, which stops the work.
    _ = sys.stdin.read()
    (root / "input_closed").write_text("closed")
    raise SystemExit(255)
status = int(os.environ.get("FAKE_MAC_STATUS","0"))
print("Mac output", flush=True)
if status == 125:
    print("mac cargo gate refused: placement closed; run it through mac_queue_run.py", file=sys.stderr)
print(f"mac_exit={status}", flush=True)
if "dispatch_start_settled=" in sys.argv[-1]:
    key, = (root / "mac-state" / "pending-starts").iterdir()
    print(f"dispatch_start_pending={key.name}", flush=True)
'''


LIFECYCLE = '''
import json, os, sys
from pathlib import Path

phase, path, *arguments = sys.argv[1:]
argv_file = Path(path)
if phase == "prepare":
    argv_file.write_text(json.dumps(arguments))
else:
    assert phase == "cleanup"
    assert json.loads(argv_file.read_text()) == arguments
with (Path(os.environ["FAKE_QUEUE_STATE"]) / "lifecycle-events").open("a") as stream:
    print(json.dumps([phase, argv_file.parent.name, os.environ["MAC_QUEUE_HOST"]]), file=stream)
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
NO_HANDLER_FILES = ("lint/nextest_tokens.py", "mac_test/mac_queue_run.py", "mac_test/cargo_gate/gate.zsh")
SIGNAL_HANDLER = re.compile(r"\bsignal\.(?:signal|SIG_IGN|SIG_DFL|pthread_sigmask|setitimer)\b|\bfrom signal import\b"
                            + r"|^\s*trap\b|\bTRAP[A-Z]+\b")


def signal_handlers(root: Path, names: tuple[str, ...]) -> list[str]:
    findings: list[str] = []
    for name in names:
        for number, line in enumerate((root / name).read_text().splitlines(), 1):
            if not line.lstrip().startswith("#") and SIGNAL_HANDLER.search(line):
                findings.append(f"{name}:{number}: {line.strip()}")
    return findings


class QueueRunTests(unittest.TestCase):
    def fixture(self, root: Path) -> dict[str, str]:
        queue = root / "queue.py"
        _ = queue.write_text(QUEUE)
        ssh = root / "ssh"
        _ = ssh.write_text(f"#!{sys.executable}\n" + SSH)
        ssh.chmod(0o755)
        systemd = root / "systemd-run"
        _ = systemd.write_text('#!/bin/sh\nprintf "called\\n" > "$FAKE_QUEUE_STATE/watcher-start"\nexit 1\n')
        systemd.chmod(0o755)
        environment = {**os.environ, **config_environment(root / "config"), "FAKE_QUEUE_STATE": str(root),
                       "FAKE_QUEUE_PROJECT": str(Path(__file__).resolve().parents[2]),
                       "MAC_TEST_STATE_DIR": str(root / "mac-state"),
                       "PATH": str(root) + os.pathsep + os.environ["PATH"], "HOME": str(root)}
        _ = write_config_fixture(root / "config/mac_test.conf", "max_active_builds=1\n")
        _ = write_config_fixture(root / "config/mac_gate.conf", "admission_client_command="
                                 + shlex.join([sys.executable, str(queue)]) + "\n")
        return environment

    def command(self, root: Path, kind: str = "shot", *, options: tuple[str, ...] = ()) -> list[str]:
        return [sys.executable, "-m", "scripts.mac_test.mac_queue_run", "--what", "camera shot", "--kind", kind,
                "--package", "hana", "--worktree", str(root), "--cwd", "/Mac scratch/checkout", *options, "--",
                "/bin/zsh", "-c", "cargo build -p hana; printf '%s' 'quoted value'"]

    def events(self, root: Path) -> list[list[str]]:
        return [cast(list[str], json.loads(line)) for line in (root / "events").read_text().splitlines()]

    def test_every_run_returns_the_placement_and_keeps_the_mac_status(self) -> None:
        cases = ((0, False, False), (23, False, False), (125, False, False), (2, True, False), (0, False, True), (2, True, True))
        for kind in ("shot", "install", "probe", "run"):
            for status, queue_refuses, return_fails in cases:
                with (self.subTest(kind=kind, status=status, queue_refuses=queue_refuses, return_fails=return_fails),
                      tempfile.TemporaryDirectory() as temporary):
                    self.check_returned_placement(Path(temporary), kind, status, queue_refuses, return_fails)

    def check_returned_placement(self, root: Path, kind: str, status: int, queue_refuses: bool, return_fails: bool) -> None:
        environment = {**self.fixture(root), "FAKE_MAC_STATUS": str(status),
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
            work_line = next(line for line in remote[2].splitlines() if line.startswith("work="))
            work = shlex.split(shlex.split(work_line)[0].removeprefix("work="))
            self.assertEqual(work[:3], ["cd", "/Mac scratch/checkout", "&&"])
            self.assertEqual(work[3], "MAC_PLACEMENT=" + placed[placed.index("--episode") + 1] + "/attempt")
            self.assertEqual(work[4:], self.command(root)[-3:])
            self.assertEqual(result.stdout.splitlines()[-1], f"mac_exit={status}")
            self.assertEqual(list((root / "mac-state/claims").iterdir()), [])
            self.assertEqual(list((root / "mac-state/pending-starts").iterdir()), [])

    def test_dead_group_leader_with_live_children_retains_the_exact_remote_claim(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = {**self.fixture(root), "FAKE_SSH_LOST": "1", "FAKE_LOST_GROUP": "children"}
            result = subprocess.run(self.command(root), capture_output=True, text=True, env=environment,
                                    timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(result.returncode, 255, result.stdout + result.stderr)
            self.assertEqual([event[0] for event in self.events(root)], ["place-step"])
            record = mac_test.read_run(root / "mac-state/claims/hana.json")
            assert not isinstance(record, mac_test.NoRun) and "remote_host" in record
            self.assertEqual(record["remote_host"], "fake-mac")
            self.assertEqual(record["attempt"], "attempt")
            self.assertTrue(mac_test.run_is_kept(record))
            self.assertEqual(len(list((root / "mac-state/pending-starts").iterdir())), 1)
            self.assertFalse((root / "watcher-start").exists())
            with mock.patch.dict(os.environ, environment):
                paths = mac_test.state_paths()
                mac_test.settle_retained_mac_claims(paths, mac_test.config_path())
                self.assertEqual(len(mac_test.read_claims(paths)), 1)
                (root / "children-ended").touch()
                mac_test.settle_retained_mac_claims(paths, mac_test.config_path())
                self.assertEqual(mac_test.read_claims(paths), ())

    def test_queue_retains_capacity_while_the_remote_attempt_runs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = {**self.fixture(root), "FAKE_PLACEMENT_REMAINS": "1"}
            result = subprocess.run(self.command(root), capture_output=True, text=True, env=environment,
                                    timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertEqual([event[0] for event in self.events(root)], ["place-step"])
            self.assertTrue((root / "mac-state/claims/hana.json").exists())

    def test_lost_ssh_sends_no_signal_to_a_group_reused_during_grace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = {**self.fixture(root), "FAKE_SSH_LOST": "1", "FAKE_LOST_GROUP": "reused"}
            result = subprocess.run(self.command(root), capture_output=True, text=True, env=environment,
                                    timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(result.returncode, 255, result.stdout + result.stderr)
            signals = root / "signals"
            self.assertFalse(signals.exists(), signals.read_text() if signals.exists() else "")
            self.assertEqual([event[0] for event in self.events(root)], ["place-step"])
            self.assertTrue((root / "mac-state/claims/hana.json").exists())

    def test_lost_ssh_preserves_lifecycle_cleanup_for_the_next_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = self.fixture(root)
            lifecycle = root / "lifecycle"
            _ = lifecycle.write_text(f"#!{sys.executable}\n" + LIFECYCLE)
            lifecycle.chmod(0o755)
            lifecycle_events = root / "lifecycle-events"
            lost = subprocess.run(self.command(root, options=("--lifecycle-script", str(lifecycle))),
                                  capture_output=True, text=True, env={**environment, "FAKE_SSH_LOST": "1"},
                                  timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(lost.returncode, 255, lost.stdout + lost.stderr)
            placed, = self.events(root)
            episode = placed[placed.index("--episode") + 1]
            saved = root / mac_queue_run.RUNS / episode
            record = cast(mac_queue_run.SavedRun, json.loads((saved / "run.json").read_text()))
            self.assertEqual(record, {"lifecycle_script": str(lifecycle), "arguments": self.command(root)[-3:],
                                      "host": "fake-mac"})
            prepared = json.dumps(["prepare", episode, "fake-mac"])
            self.assertEqual(lifecycle_events.read_text().splitlines(), [prepared])
            with mock.patch.dict(os.environ, environment):
                paths = mac_test.state_paths()
                self.assertEqual(len(mac_test.read_claims(paths)), 1)
                mac_test.settle_retained_mac_claims(paths, mac_test.config_path())
                self.assertEqual(mac_test.read_claims(paths), ())

            later = subprocess.run(self.command(root), capture_output=True, text=True, env=environment,
                                    timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(later.returncode, 0, later.stdout + later.stderr)
            self.assertEqual(lifecycle_events.read_text().splitlines(),
                             [prepared, json.dumps(["cleanup", episode, "fake-mac"])])
            self.assertFalse(saved.exists())
            self.assertEqual([event[0] for event in self.events(root)], ["place-step", "place-step", "cancel-placement"])

    def test_process_scan_checks_all_strings_and_keeps_unknown_reads(self) -> None:
        arguments = shlex.split(mac_process_environment.placement_pids_command("episode", "attempt"))

        def process_list(command: list[str], *, text: bool) -> str:
            self.assertTrue(text)
            self.assertEqual(command[-1], "pid=,uid=,stat=")
            return f"4567 {os.getuid()} S\n4568 {os.getuid()} S\n"

        exit_errors = {"exited": ProcessLookupError(errno.ESRCH, "process exited"),
                       "exit check failed": PermissionError(errno.EPERM, "process is unreadable")}
        marker = b"MAC_PLACEMENT=episode/attempt"
        header = struct.pack("i", 1) + b"/bin/build\0\0build\0"
        metadata = b"pfz=0x123\0stack_guard=0x456\0"
        environment = b"HOME=/fixture\0"
        unmarked = header + environment + metadata
        cases = {
            "exited": unmarked,
            "size failed": unmarked,
            "read failed": unmarked,
            "exit check failed": unmarked,
            "malformed": header + b"HOME=/fixture",
            "omitted environment": header,
            "empty environment": header + b"\0",
            "complete without padding": unmarked,
            "complete with padding": header + environment + b"\0\0" + metadata,
            "marker in environment": header + marker + b"\0" + metadata,
            "marker in argv": struct.pack("i", 2) + header[4:] + marker + b"\0" + environment + metadata,
            "marker after padding": header + environment + b"\0" + metadata + marker + b"\0",
            "marker prefix only": header + marker + b"-different\0" + metadata,
            "fake pfz before marker": header + b"pfz=0xabc\0" + marker + b"\0" + metadata,
            "short buffer": header + environment + b"\0",
            "short buffer with fake pfz": header + b"pfz=0xabc\0",
        }
        for outcome, data in cases.items():
            with self.subTest(outcome=outcome):
                def sysctl(mib: ctypes.Array[ctypes.c_int], _count: int, buffer: object, size: object,
                           _new: object, _new_count: int) -> int:
                    length = ctypes.cast(cast(ctypes.c_void_p, size), ctypes.POINTER(ctypes.c_size_t))
                    if buffer is None:
                        length.contents.value = len(data)
                        return int(outcome == "size failed" or (outcome == "exited" and mib[2] == 4567))
                    if outcome in ("read failed", "exit check failed"):
                        return 1
                    _ = ctypes.memmove(cast(ctypes.c_void_p, buffer), data, len(data))
                    if not outcome.startswith("short buffer"):
                        length.contents.value = len(data)
                    return 0

                kernel = mock.Mock(sysctl=mock.Mock(side_effect=sysctl))
                output, errors = io.StringIO(), io.StringIO()
                with (mock.patch.object(sys, "platform", "darwin"), mock.patch.object(sys, "argv", arguments[2:]),
                      mock.patch.object(ctypes, "CDLL", return_value=kernel),
                      mock.patch.object(os, "kill", side_effect=exit_errors.get(outcome)) as check,
                      mock.patch.object(subprocess, "check_output", side_effect=process_list),
                      redirect_stdout(output), redirect_stderr(errors), self.assertRaises(SystemExit) as status):
                    exec(compile(arguments[2], "<placement process probe>", "exec"), {"__name__": "__main__"})
                matching = outcome in ("marker in environment", "marker in argv", "marker after padding",
                                       "fake pfz before marker")
                if matching or outcome in ("exited", "complete without padding", "complete with padding",
                                           "marker prefix only"):
                    self.assertEqual(status.exception.code, 0)
                    self.assertEqual(output.getvalue(), "4567\n4568\n" if matching else "")
                    self.assertEqual(errors.getvalue(), "")
                else:
                    self.assertEqual(status.exception.code, 1)
                    self.assertEqual(output.getvalue(), "")
                    self.assertIn("cannot check Mac process 4567", errors.getvalue())
                if outcome in exit_errors:
                    check.assert_called_once_with(4567, 0)

    def test_a_signal_ends_the_run_with_its_default_action(self) -> None:
        for waiting, signum in ((False, signal.SIGTERM), (False, signal.SIGHUP), (True, signal.SIGTERM),
                                (False, signal.SIGINT)):
            with self.subTest(waiting=waiting, signum=signum), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                environment = {**self.fixture(root), "FAKE_QUEUE_WAIT": "1" if waiting else "0", "FAKE_SSH_HOLD": "1"}
                with subprocess.Popen(self.command(root), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                      text=True, env=environment, start_new_session=True) as process:
                    try:
                        wait_until((root / ("waiting" if waiting else "running")).exists, root, processes=(process,))
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
                self.assertEqual(events, ["place-step"])
                if not waiting and signum != signal.SIGINT:
                    wait_until((root / "input_closed").exists, root)
                if not waiting:
                    with mock.patch.dict(os.environ, environment):
                        paths = mac_test.state_paths()
                        self.assertEqual(len(mac_test.read_claims(paths)), 1)
                        mac_test.settle_retained_mac_claims(paths, mac_test.config_path())
                        self.assertEqual(mac_test.read_claims(paths), ())
                if signum != signal.SIGINT:
                    # The next run cleans the state of a run that ended without local cleanup.
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
                wrapper = mac_queue_run.remote_wrapper(work, on_close, 20).replace("/bin/zsh", zsh)
                # Its own session stands in for the SSH session on the Mac.
                with subprocess.Popen([zsh, "-c", wrapper], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      text=True, start_new_session=True) as process:
                    try:
                        assert process.stdout is not None and process.stdin is not None
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
