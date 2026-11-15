"""Exercise placement return and SSH routing with executable stand-ins."""
from __future__ import annotations

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
from typing import cast
import unittest

from scripts.direct.wait_for_test_state import wait_until
from scripts.lib.process_group import send_process_group
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.mac_test import mac_queue_run
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
    Path(option("--assignment-file")).write_text(json.dumps({"episode":option("--episode"),"attempt":"attempt"}))
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
        environment = {**os.environ, **config_environment(root / "config"), "FAKE_QUEUE_STATE": str(root),
                       "PATH": str(root) + os.pathsep + os.environ["PATH"], "HOME": str(root)}
        _ = write_config_fixture(root / "config/mac_gate.conf", "admission_client_command="
                                 + shlex.join([sys.executable, str(queue)]) + "\n")
        return environment

    def command(self, root: Path) -> list[str]:
        return [sys.executable, "-m", "scripts.mac_test.mac_queue_run", "--what", "camera shot", "--kind", "shot",
                "--package", "hana", "--worktree", str(root), "--cwd", "/Mac scratch/checkout", "--",
                "/bin/zsh", "-c", "cargo build -p hana; printf '%s' 'quoted value'"]

    def events(self, root: Path) -> list[list[str]]:
        return [cast(list[str], json.loads(line)) for line in (root / "events").read_text().splitlines()]

    def test_every_run_returns_the_placement_and_keeps_the_mac_status(self) -> None:
        for status, queue_refuses, return_fails in ((0, False, False), (23, False, False), (125, False, False), (2, True, False), (0, False, True), (2, True, True)):
            with self.subTest(status=status, queue_refuses=queue_refuses, return_fails=return_fails), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                environment = {**self.fixture(root), "FAKE_MAC_STATUS": str(status),
                               "FAKE_QUEUE_REFUSE": "1" if queue_refuses else "0", "FAKE_RETURN_FAIL": "1" if return_fails else "0"}
                result = subprocess.run(self.command(root), capture_output=True, text=True, env=environment,
                                        timeout=TEST_HANG_GUARD_SECS)
                self.assertEqual(result.returncode, status or (1 if return_fails else 0), result.stdout + result.stderr)
                events = self.events(root)
                self.assertEqual([event[0] for event in events], ["place-step", "cancel-placement" if queue_refuses else "finish-step"])
                placed, released = events
                self.assertIn("--once", placed)
                self.assertIn("--wait-events", placed)
                self.assertEqual(placed[placed.index("--target") + 1], "mac")
                assignment = Path(placed[placed.index("--assignment-file") + 1])
                self.assertEqual(assignment.exists(), return_fails)
                if return_fails:
                    self.assertTrue(assignment.is_relative_to(root / ".local/state"))
                    self.assertIn(str(assignment), result.stderr)
                    retry = result.stderr.split("return with: ", 1)[1].strip()
                    retried = subprocess.run(retry, shell=True, env={**environment, "FAKE_RETURN_FAIL": "0"},
                                             capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS)
                    self.assertEqual(retried.returncode, 0, retried.stderr)
                    self.assertFalse(assignment.exists())
                if queue_refuses:
                    self.assertEqual(released[released.index("--episode") + 1], placed[placed.index("--episode") + 1])
                    self.assertFalse((root / "remote").exists())
                else:
                    self.assertEqual(released[released.index("--assignment-file") + 1], str(assignment))
                    self.assertEqual(released[released.index("--status") + 1], str(status))
                    remote = cast(list[str], json.loads((root / "remote").read_text()))
                    wrapper = shlex.split(remote[2])
                    work = shlex.split(wrapper[0].removeprefix("work="))
                    self.assertEqual(work[:3], ["cd", "/Mac scratch/checkout", "&&"])
                    self.assertEqual(work[3], "MAC_PLACEMENT=" + placed[placed.index("--episode") + 1] + "/attempt")
                    self.assertEqual(work[4:], self.command(root)[-3:])
                    self.assertEqual(result.stdout.splitlines()[-1], f"mac_exit={status}")

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
                if signum == signal.SIGINT:
                    # Python's own KeyboardInterrupt runs the finally path, which returns the placement.
                    self.assertEqual(events, ["place-step", "finish-step"])
                    self.assertEqual(self.events(root)[-1][-1], "1")
                    self.assertIn("signal_group TERM", (root / "stop").read_text())
                else:
                    # The queue ends the placement when this process ends.
                    self.assertEqual(events, ["place-step"])
                    if not waiting:
                        wait_until((root / "input_closed").exists, root)
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
                wrapper = mac_queue_run.remote_wrapper(work, on_close, 20).replace("/bin/zsh", zsh)
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
