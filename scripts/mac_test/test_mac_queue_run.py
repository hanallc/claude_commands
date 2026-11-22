"""Exercise placement return and SSH routing with executable stand-ins."""
from __future__ import annotations

from collections.abc import Callable
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from dataclasses import replace
import fcntl
import io
import json
import os
from pathlib import Path
import re
import select
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
from typing import Literal, TextIO, cast
import unittest
from unittest import mock

from scripts.aws_target import cloud_gate, gate_box
from scripts.build_hold.owning_session import NoOwningSession, OwningSession
from scripts.build_queue import admission_client, admission_wire as wire, execution_identity, placement
from scripts.build_queue.admission_test_support import caller_provider_controls
from scripts.build_targets import build_targets, targets
from scripts.direct.wait_for_test_state import wait_until
from scripts.lib.atomic_file import write_text_atomic
from scripts.lib.checkout_test_support import write_stdlib_tool
from scripts.lib import conf
from scripts.lib.process_start import Started, process_start
from scripts.lib.process_group import send_process_group
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.mac_test import claim_state, eligibility, mac_gate, mac_queue_run, mac_test, offload
from scripts.lint.memory_test_support import config_environment, write_config_fixture
from scripts.production import gate, gate_suites_test_support as gate_fixture
from scripts.production import test_gate_suites_mac as mac_fixture
from scripts.production.gate_routes import SuiteMode
from scripts.production.gate_test_support import gate_suites
from scripts.production.mac_gate_process import MAC_GATE_SCRIPT


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
if sys.argv[1] == "-n" and 'base.with_suffix(".lock").touch()' in sys.argv[-1]:
    (root / "fence").write_text(sys.argv[-2])
    raise SystemExit(0)
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


def queue_fixture(root: Path, ssh_source: str = SSH) -> dict[str, str]:
    queue = root / "queue.py"
    _ = queue.write_text(QUEUE)
    ssh = root / "ssh"
    _ = ssh.write_text(f"#!{sys.executable}\n" + ssh_source)
    ssh.chmod(0o755)
    environment = {**os.environ, **config_environment(root / "config"), "FAKE_QUEUE_STATE": str(root),
                   "PATH": str(root) + os.pathsep + os.environ["PATH"], "HOME": str(root)}
    _ = write_config_fixture(root / "config/mac_gate.conf", "admission_client_command="
                             + shlex.join([sys.executable, str(queue)]) + "\n")
    return environment


class QueueRunTests(unittest.TestCase):
    def command(self, root: Path, kind: str = "shot") -> list[str]:
        return [sys.executable, "-m", "scripts.mac_test.mac_queue_run", "--what", "camera shot", "--kind", kind,
                "--package", "hana", "--worktree", str(root), "--cwd", "/Mac scratch/checkout", "--",
                "/bin/zsh", "-c", "cargo build -p hana; printf '%s' 'quoted value'"]

    def events(self, root: Path) -> list[list[str]]:
        return [cast(list[str], json.loads(line)) for line in (root / "events").read_text().splitlines()]

    def assigned_gate(self, root: Path) -> tuple[mac_queue_run.Arguments, placement.PlacementAssignment, dict[str, str]]:
        environment = queue_fixture(root)
        _ = write_config_fixture(root / "config/mac_test.conf", "host=configured-mac\n")
        watcher = root / "systemd-run"
        _ = watcher.write_text("#!/bin/sh\nexit 0\n")
        watcher.chmod(0o755)
        _ = subprocess.run(["git", "init", "-q", str(root)], check=True, env=environment,
                           timeout=TEST_HANG_GUARD_SECS)
        assignment = placement.PlacementAssignment("episode", "attempt",
            placement.PlacementDestination("mac", "mac", "assigned-mac", claim_state.RepositoryScope(root.name)),
            execution_identity.SourceKeyKnown("tree"), placement.SubmittingOwner("local", os.getpid(), "start"),
            NoOwningSession(), 1)
        args = mac_queue_run.Arguments()
        args.owned_close, args.lifecycle_script, args.worktree = True, "fixture-gate", root
        args.argv = ["--", str(root), "1"]
        path = root / "assignment.json"
        _ = path.write_text(wire.encode_reply(wire.Assignment(assignment)))
        args.assignment_file = str(path)
        return args, assignment, environment

    def write_claim(self, assignment: placement.PlacementAssignment, root: Path, pid: int,
                    start: str, session: str) -> claim_state.ClaimIdentity:
        reference = placement.assigned_claim_reference(assignment)
        paths = claim_state.state_paths(reference.target)
        scope = assignment.destination.scope
        assert isinstance(scope, claim_state.RepositoryScope)
        path = mac_test.claim_path(paths, scope)
        path.parent.mkdir(parents=True, exist_ok=True)
        mac_test.write_json(path, claim_state.RemoteJobClaim(
            claim_id=reference.claim_id, episode=reference.episode, attempt=reference.attempt,
            submitting_session=session, pid=pid, proc_start=start, remote_host=assignment.destination.host,
            repo_name=scope.name, repo_key=scope.key,
            what="gate tests", worktree=str(root), since=mac_test.timestamp(mac_test.utc_now())))
        return claim_state.ClaimIdentity(reference.claim_id, reference.episode, reference.attempt)

    def save_ended_run(self, args: mac_queue_run.Arguments, assignment: placement.PlacementAssignment) -> Path:
        with mac_queue_run.run_directory(assignment.attempt) as run:
            journal = run / "run.json"
            _ = journal.write_text(json.dumps({
                "lifecycle_script": args.lifecycle_script, "arguments": args.argv[1:],
                "host": assignment.destination.host,
                "claim": {"assignment": Path(args.assignment_file).read_text(), "holder_pid": os.getpid()},
            }))
        return journal

    def test_assigned_mac_claim_uses_the_assigned_host(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args, assignment, environment = self.assigned_gate(root)
            with mock.patch.dict(os.environ, environment, clear=True):
                target = eligibility.configured_ssh_target(assignment.destination.target)
                config = eligibility.read_config(target.settings)
                self.assertNotEqual(config.host, assignment.destination.host)
                controller = offload.ProcessController()
                result = offload.claim_for_assigned_mac_run(controller, assignment, args.worktree)
                self.assertIsInstance(result, offload.FreshMacClaim)
                claim = offload.read_assigned_mac_claim(assignment)
                assert isinstance(claim, offload.AssignedMacClaim), claim
                self.assertEqual(claim.record["remote_host"], assignment.destination.host)
                self.assertEqual(claim.record["pid"], os.getpid())
                self.assertIsInstance(offload.release_claim(controller, target.name), offload.ClaimReleaseConfirmed)
                self.assertIsInstance(offload.read_assigned_mac_claim(assignment), offload.MacClaimSettled)

    def test_claude_commands_gate_claim_appears_in_target_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "claude_commands"
            repo.mkdir()
            args, assignment, environment = self.assigned_gate(repo)
            session = "gate-owner"
            assignment = replace(assignment, session=OwningSession(session))
            _ = subprocess.run(["git", "remote", "add", "origin", "https://github.com/natepiano/claude_commands.git"],
                               cwd=repo, env=environment, check=True, timeout=TEST_HANG_GUARD_SECS)
            with mock.patch.dict(os.environ, environment, clear=True):
                target = eligibility.configured_ssh_target(assignment.destination.target)
                controller = offload.ProcessController()
                self.assertIsInstance(offload.claim_for_assigned_mac_run(controller, assignment, args.worktree),
                                      offload.FreshMacClaim)
                try:
                    claims = claim_state.read_claims(claim_state.state_paths(target.name))
                    claim, = claims
                    self.assertEqual(claim.scope, claim_state.RepositoryScope("claude_commands"))
                    since = mac_test.aware_timestamp(claim.record["since"], claim.path, "since")
                    self.assertEqual(build_targets.target_state(target), targets.Running((
                        targets.RunningJob("gate tests", since, targets.NOT_SEEN_IDLE, targets.HolderSession(session)),)))
                finally:
                    self.assertEqual(offload.release_claim(controller, target.name), offload.ClaimReleaseConfirmed())

    def test_assigned_claim_bounds_output_collection_and_keeps_uncertain_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args, assignment, environment = self.assigned_gate(root)
            _ = write_config_fixture(root / "config/mac_test.conf", "leftover_timeout_s=0.5\n")
            controller = offload.ProcessController(claim_in_runner_group=True)
            communicate = subprocess.Popen[str].communicate
            children: list[subprocess.Popen[str]] = []
            command = [sys.executable, "-c", "import signal; signal.pause()"]

            def held(process: subprocess.Popen[str], input: str | None = None,
                     timeout: float | None = None) -> tuple[str, str]:
                if process.args == command:
                    children.append(process)
                    self.assertIsNotNone(timeout, "The assigned Mac claim collected output without a deadline")
                return communicate(process, input, timeout)

            with (mock.patch.dict(os.environ, environment, clear=True),
                  mock.patch.object(offload, "claim_arguments", return_value=command),
                  mock.patch.object(subprocess.Popen, "communicate", new=held)):
                try:
                    with self.assertRaises(subprocess.TimeoutExpired):
                        _ = offload.claim_for_assigned_mac_run(controller, assignment, args.worktree)
                    child, = children
                    self.assertEqual(child.returncode, -signal.SIGKILL)
                    self.assertIsInstance(controller.pending, offload.PendingClaimOperation)
                    self.assertEqual(controller.attempt_evidence.provider, placement.assigned_claim_reference(assignment))
                finally:
                    for child in children:
                        if child.poll() is None:
                            child.kill()
                        _ = child.wait(timeout=TEST_HANG_GUARD_SECS)
                        claim_state.close_process_pipes(child)

    def write_ordinary_claim(self, paths: claim_state.StatePaths, root: Path) -> None:
        paths.directory.mkdir(parents=True, exist_ok=True)
        start = process_start(os.getpid())
        assert isinstance(start, Started)
        mac_test.write_json(paths.run, claim_state.RemoteJobClaim(
            claim_id="ordinary", episode="ordinary", attempt="ordinary", submitting_session="",
            pid=os.getpid(), proc_start=start.start, remote_host="configured-mac",
            what="ordinary test", worktree=str(root), since=mac_test.timestamp(mac_test.utc_now())))

    def exercise_stalled_claim_watch(self, step: Literal["listing", "budget", "final", "linux-lock"],
                                    expected_command: str) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment = queue_fixture(root, "print('active=1\\nwork=0\\nrc=0')\n")
            python_startup = root / "python-startup"
            python_startup.mkdir()
            environment["PYTHONPATH"] = os.pathsep.join((str(python_startup), str(Path(__file__).resolve().parents[2])))
            environment["MAC_TEST_MESSAGE_TIMEOUT_S"] = "1"
            target = "fixture-linux" if step == "linux-lock" else ""
            config_dir = root / "config"
            _ = write_config_fixture(config_dir / "mac_test.conf",
                                     "host=configured-mac\nconnect_timeout_s=3\nleftover_timeout_s=2\n")
            _ = write_config_fixture(config_dir / "mac_gate.conf", "terminate_grace_seconds=0.5\n")
            _ = write_config_fixture(config_dir / "build_admission.conf", "program_stop_seconds=1\n")
            if target:
                _ = write_config_fixture(config_dir / "fixture_linux.conf",
                                         "ci_repo=\nhost=configured-linux\nconnect_timeout_s=3\n", template="mac_test")
                _ = write_config_fixture(config_dir / "build_targets.conf",
                                         "pinned_targets=hana-gpu,fixture-linux\nkind.fixture-linux=ssh\nplatform.fixture-linux=linux\n"
                                         + "settings.fixture-linux=fixture_linux.conf\n")
            with mock.patch.dict(os.environ, environment, clear=True), ExitStack() as resources:
                paths = claim_state.state_paths(target)
                if step in ("listing", "budget"):
                    self.write_ordinary_claim(paths, root)
                if step == "linux-lock":
                    _ = resources.enter_context(claim_state.locked(paths.lock))
                runs = mac_gate.queue_runs_directory()
                runs.mkdir(parents=True)
                (runs / "fixture").touch()
                stalled = root / "step-started"
                fifo = root / "step-hold"
                os.mkfifo(fifo)
                housekeeping = root / "housekeeping-scanned"
                _ = (python_startup / "sitecustomize.py").write_text(
                    "from pathlib import Path\n"
                    + f"step = {step!r}\n"
                    + "stalled, fifo, runs, gate_config, claim, housekeeping, lock = map(Path, "
                    + f"{tuple(map(str, (stalled, fifo, runs, config_dir / 'mac_gate.conf', paths.run, housekeeping, paths.lock)))!r})\n"
                    + self.claim_watch_stall_hook())
                config = mac_test.read_config(target)
                message_budget = mac_test.message_timeout_s()
                housekeeping_budget = config.connect_timeout_s + message_budget
                budget = housekeeping_budget
                if step == "budget":
                    budget += housekeeping_budget
                if step in ("listing", "final"):
                    budget += mac_gate.queue_cancel_seconds(paths.target)
                if step == "final":
                    budget += housekeeping_budget + message_budget
                command = self.one_claim_watch_pass() + f"run_pass({target!r})\n"
                try:
                    result = subprocess.run([sys.executable, "-c", command], capture_output=True, text=True,
                                            timeout=budget, check=False)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn(f"Mac {expected_command} remains unresolved", result.stdout, result.stderr)
                    self.assertIn("Fixture watcher pass ended", result.stdout)
                    if step in ("listing", "budget"):
                        self.assertIn("work_idle_since", paths.run.read_text())
                    pid, stamp = stalled.read_text().split()
                    self.assertNotEqual(process_start(int(pid)), Started(stamp))
                    with mac_test.lock_if_available(paths.claim_watch_lock) as free:
                        self.assertTrue(free)
                    if step == "final":
                        with mac_test.lock_if_available(paths.lock) as free:
                            self.assertTrue(free)
                except subprocess.TimeoutExpired:
                    self.assertTrue(stalled.exists(), "The fixture did not reach the stalled operation")
                    self.fail(f"The {step} operation blocked the watcher past the configured budgets")
                finally:
                    if stalled.exists():
                        pid, stamp = stalled.read_text().split()
                        if process_start(int(pid)) == Started(stamp):
                            os.kill(int(pid), signal.SIGKILL)

    @staticmethod
    def claim_watch_stall_hook() -> str:
        return """
import os
import sys

def mark():
    from scripts.lib.process_start import Started, process_start

    pid = os.getpid()
    identity = process_start(pid)
    assert isinstance(identity, Started)
    stalled.write_text(f'{pid} {identity.start}')

def block():
    mark()
    with fifo.open('rb') as hold:
        hold.read()

def list_directory(path):
    if step == 'listing' and path == runs:
        block()
    return iterdir(path)

def read_text(path, *args, **kwargs):
    if step == 'budget' and path == gate_config:
        block()
    if step == 'final' and path == claim:
        if 'claim-watch-pass' in sys.argv:
            housekeeping.touch()
        elif housekeeping.exists():
            block()
    return read(path, *args, **kwargs)

def open_file(path, *args, **kwargs):
    if step == 'linux-lock' and path == lock:
        mark()
    return open_path(path, *args, **kwargs)

iterdir, read, open_path = Path.iterdir, Path.read_text, Path.open
Path.iterdir, Path.read_text, Path.open = list_directory, read_text, open_file
"""

    @staticmethod
    def one_claim_watch_pass() -> str:
        return """
from types import SimpleNamespace
from scripts.mac_test import mac_test

class FirstPassEnded(Exception):
    pass

def stop_at_pass_end(seconds):
    raise FirstPassEnded

def run_pass(target):
    mac_test.time = SimpleNamespace(sleep=stop_at_pass_end)
    try:
        raise SystemExit(mac_test.command_watch_claim(mac_test.state_paths(target), mac_test.read_config(target)))
    except FirstPassEnded:
        print('Fixture watcher pass ended')
"""

    def test_claim_housekeeping_runs_after_a_stalled_recovery_listing(self) -> None:
        self.exercise_stalled_claim_watch("listing", "claim-watch-recovery")

    def test_claim_housekeeping_runs_after_a_stalled_recovery_budget_read(self) -> None:
        self.exercise_stalled_claim_watch("budget", "claim-watch-budget")

    def test_final_claim_read_timeout_ends_the_pass_and_releases_state_lock(self) -> None:
        self.exercise_stalled_claim_watch("final", "claim-watch-finish")

    def test_held_linux_state_lock_does_not_block_the_claim_watcher_pass(self) -> None:
        self.exercise_stalled_claim_watch("linux-lock", "claim-watch-pass")

    def test_claim_housekeeping_runs_after_invalid_recovery_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment = queue_fixture(root, "print('active=1\\nwork=0\\nrc=0')\n")
            environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
            _ = write_config_fixture(root / "config/mac_test.conf", "host=configured-mac\n")
            watcher = root / "watcher.py"
            _ = watcher.write_text(
                "import os, sys\n"
                + "if sys.argv[1] == 'claim-watch-recovery':\n"
                + "    os.write(1, b'recovery output \\xff\\n')\n"
                + "    os.write(2, b'recovery diagnostic \\xff\\n')\n"
                + "else:\n"
                + "    from scripts.mac_test import mac_test\n"
                + "    raise SystemExit(mac_test.main())\n")
            output, errors = io.StringIO(), io.StringIO()
            with (mock.patch.dict(os.environ, environment, clear=True),
                  mock.patch.object(mac_test, "__file__", str(watcher)),
                  redirect_stdout(output), redirect_stderr(errors)):
                paths = claim_state.state_paths()
                self.write_ordinary_claim(paths, root)
                self.assertTrue(mac_test.supervised_claim_watch_pass(paths, mac_test.read_config()))
                self.assertIn("work_idle_since", paths.run.read_text())
            replacement = chr(0xfffd)
            self.assertIn(f"recovery output {replacement}", output.getvalue())
            self.assertIn(f"recovery diagnostic {replacement}", errors.getvalue())

    def test_claim_watcher_recovers_an_assigned_run_after_housekeeping_stalls(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args, assignment, environment = self.assigned_gate(root)
            stalled = root / "housekeeping-started"
            recovered = root / "recovered"
            ssh = root / "ssh"
            source = ("import os, signal\nfrom pathlib import Path\n"
                      + f"marker = Path({str(stalled)!r})\n"
                      + "if not marker.exists():\n    start = Path('/proc/self/stat').read_text().rsplit(') ', 1)[1].split()[19]\n"
                      + "    marker.write_text(f'{os.getpid()} {start}')\n    signal.pause()\n"
                      + "print('active=1\\nwork=1\\nrc=0')\n")
            _ = ssh.write_text(f"#!{sys.executable}\n" + source)
            cleanup = root / "cleanup"
            _ = cleanup.write_text(f"#!{sys.executable}\nfrom pathlib import Path\n"
                                   + f"Path({str(recovered)!r}).touch()\n")
            cleanup.chmod(0o755)
            args.lifecycle_script = str(cleanup)
            _ = write_config_fixture(root / "config/mac_test.conf",
                                     "host=configured-mac\nconnect_timeout_s=1.6\nclaim_poll_seconds=0.1\n")
            environment["MAC_TEST_MESSAGE_TIMEOUT_S"] = "0.6"
            with mock.patch.dict(os.environ, environment, clear=True):
                paths = claim_state.state_paths(assignment.destination.target)
                self.write_ordinary_claim(paths, root)
                limit = mac_test.read_config().connect_timeout_s + mac_test.message_timeout_s()
                log_path = root / "watcher.log"
                with log_path.open("w") as log:
                    watcher = subprocess.Popen([sys.executable, str(Path(mac_test.__file__)), "watch-claim"],
                                               stdout=log, stderr=log, start_new_session=True)
                    try:
                        wait_until(stalled.exists, root, timeout=TEST_HANG_GUARD_SECS)
                        journal = self.save_ended_run(args, assignment)
                        try:
                            wait_until(recovered.exists, root, timeout=3 * limit)
                        except (AssertionError, TimeoutError) as error:
                            self.fail(f"Stalled housekeeping prevented assigned recovery: {error}\n{log_path.read_text()}")
                        try:
                            wait_until(lambda: not journal.exists(), journal.parent.parent, timeout=3 * limit)
                        except TimeoutError as error:
                            self.fail(f"Assigned recovery left the journal: {error}\n{log_path.read_text()}")
                        pid, start_stamp = stalled.read_text().split()
                        self.assertNotEqual(process_start(int(pid)), Started(start_stamp))
                        paths.run.unlink()
                        self.assertEqual(watcher.wait(timeout=TEST_HANG_GUARD_SECS), 0)
                    finally:
                        if watcher.poll() is None:
                            watcher.kill()
                        _ = watcher.wait(timeout=TEST_HANG_GUARD_SECS)
                        if stalled.exists():
                            pid, start_stamp = stalled.read_text().split()
                            if process_start(int(pid)) == Started(start_stamp):
                                os.kill(int(pid), signal.SIGKILL)

    def test_claim_watcher_recovers_ended_gate_without_placement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args, assignment, environment = self.assigned_gate(root)
            with mock.patch.dict(os.environ, environment, clear=True):
                paths = claim_state.state_paths(assignment.destination.target)
                path = mac_test.claim_path(paths, assignment.destination.scope)
                _ = self.write_claim(assignment, root, os.getpid(), "previous-start",
                                     claim_state.session_id(assignment.session))
                record = mac_test.read_run(path)
                assert isinstance(record, dict) and "remote_host" in record
                record["cleanup_pending"] = True
                mac_test.write_json(path, record)
                args.lifecycle_script = str(root / "old worktree" / MAC_GATE_SCRIPT.name)
                journal = self.save_ended_run(args, assignment)
                config = mac_test.read_config()
                with (mock.patch.object(mac_queue_run, "run_callback", side_effect=(1, 0)) as cleanup,
                      mock.patch.object(mac_test, "check_mac_activity",
                                        return_value=mac_test.ActivityCheckFailed("activity is unavailable")) as activity):
                    mac_queue_run.recover_ended_assigned_runs(paths.target)
                    watching = mac_test.claim_watch_pass(paths, config)
                    self.assertTrue(watching)
                    self.assertTrue(path.exists())
                    self.assertTrue(journal.exists())
                    mac_queue_run.recover_ended_assigned_runs(paths.target)
                    watching = mac_test.claim_watch_pass(paths, config)
                self.assertFalse(path.exists(), "the claim watcher left the settled claim")
                self.assertFalse(journal.exists())
                self.assertFalse(watching)
                installed = str(root / ".claude" / MAC_GATE_SCRIPT)
                self.assertEqual([call.args[:2] for call in cleanup.call_args_list],
                                 [(installed, "cleanup-ended"), (installed, "cleanup-ended")])
                activity.assert_not_called()
                self.assertFalse((root / "events").exists(), "the watcher requested a placement")

    def test_cancel_after_claim_write_releases_the_claim(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args, assignment, environment = self.assigned_gate(root)

            def publish_claim(_controller: offload.ProcessController, assigned: placement.PlacementAssignment,
                              _repo: Path) -> offload.MacClaimAnswer:
                start = process_start(os.getpid())
                assert isinstance(start, Started)
                _ = self.write_claim(assigned, root, os.getpid(), start.start, claim_state.session_id(assigned.session))
                _ = os.write(cancel, b"1")
                return offload.FreshMacClaim(assigned.destination.slot)

            caller, cancel = os.pipe()
            try:
                args.caller = mac_queue_run.QueueCaller((caller,))
                with (mock.patch.dict(os.environ, environment, clear=True),
                      mock.patch.object(offload, "claim_for_assigned_mac_run", side_effect=publish_claim)):
                    self.assertEqual(mac_queue_run.run(args), 2)
                    self.assertEqual(offload.read_assigned_mac_claim(assignment), offload.MacClaimSettled())
                    self.assertFalse((root / mac_gate.QUEUE_RUNS / assignment.attempt / "run.json").exists())
            finally:
                os.close(caller)
                os.close(cancel)

    def test_ended_gate_recovers_the_exact_claim_after_takeover(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args, assignment, environment = self.assigned_gate(root)
            with mock.patch.dict(os.environ, environment, clear=True):
                paths = claim_state.state_paths(assignment.destination.target)
                path = mac_test.claim_path(paths, assignment.destination.scope)
                identity = self.write_claim(assignment, root, os.getpid(), "previous-start",
                                            claim_state.session_id(assignment.session))
                journal = self.save_ended_run(args, assignment)
                with mock.patch.object(mac_queue_run, "run_callback", return_value=1):
                    mac_queue_run.clean_ended_runs(environment)
                self.assertTrue(path.exists())
                self.assertTrue(journal.exists())

                with subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"],
                                      stdin=subprocess.PIPE) as takeover:
                    claim_args = mac_test.CliArguments()
                    claim_args.pid, claim_args.remote = takeover.pid, assignment.destination.host
                    claim_args.worktree = str(root)
                    claim_args.claim_id, claim_args.episode, claim_args.attempt = (
                        identity.claim_id, identity.episode, identity.attempt)
                    with mac_test.locked(paths.lock):
                        result = mac_test.claim_once(claim_args, paths, assignment.destination.scope,
                                                     session=OwningSession("next-runner"))
                    self.assertEqual(result, mac_test.ClaimFinished(13))
                    with mock.patch.object(mac_queue_run, "run_callback", return_value=0) as cleanup:
                        mac_queue_run.clean_ended_runs(environment)
                        self.assertTrue(path.exists())
                        self.assertTrue(journal.exists())
                        cleanup.assert_not_called()
                    _ = takeover.communicate()

                with mock.patch.object(mac_queue_run, "run_callback", return_value=0):
                    mac_queue_run.clean_ended_runs(environment)
                self.assertFalse(path.exists())
                self.assertFalse(journal.exists())

    def test_assigned_gate_refuses_late_backoff_before_remote_preparation(self) -> None:
        self.check_late_gate_policy("backoff")

    def test_assigned_gate_refuses_late_off_switch_before_remote_preparation(self) -> None:
        self.check_late_gate_policy("off")

    def check_late_gate_policy(self, reason: str) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args, assignment, environment = self.assigned_gate(root)
            target = assignment.destination.target
            refused = wire.DispatchStatus(assignment, placement.ClosedAssignment(reason),
                placement.PermissionAvailable(), retry=placement.DoNotRetry(reason))

            def read_backoff(_now: float, _target: str) -> eligibility.BackoffState:
                claim = offload.read_assigned_mac_claim(assignment)
                if reason == "backoff" and isinstance(claim, offload.AssignedMacClaim):
                    return eligibility.ACTIVE_BACKOFF
                return eligibility.NO_BACKOFF

            claim_for_run = offload.claim_for_assigned_mac_run

            def claim_then_change_policy(controller: offload.ProcessController, assigned: placement.PlacementAssignment,
                                         repo: Path) -> offload.MacClaimAnswer:
                claim = claim_for_run(controller, assigned, repo)
                self.assertIsInstance(claim, offload.FreshMacClaim)
                if reason == "off":
                    settings = eligibility.configured_ssh_target(target).settings
                    _ = write_config_fixture(settings, "offload=off\n")
                return claim

            with (mock.patch.dict(os.environ, environment, clear=True),
                  mock.patch.object(eligibility, "read_backoff", side_effect=read_backoff),
                  mock.patch.object(offload, "claim_for_assigned_mac_run", side_effect=claim_then_change_policy),
                  mock.patch.object(mac_queue_run, "run_callback",
                                    side_effect=AssertionError("the runner started remote preparation")) as lifecycle,
                  mock.patch.object(admission_client, "consume_permission") as authorize,
                  mock.patch.object(admission_client, "report_execution", return_value=refused) as report):
                self.assertEqual(mac_queue_run.run(args), 2)
                lifecycle.assert_not_called()
                self.assertFalse((root / "fence").exists())
                self.assertFalse((root / "remote").exists())
                authorize.assert_not_called()
                self.assertEqual(claim_state.read_claims(claim_state.state_paths(target)), ())
                self.assertTrue((root / mac_gate.ASSIGNED_REFUSAL).exists())
                self.assertEqual(cast(wire.ExecutionReport, report.call_args.args[0]).outcome,
                                 placement.PrelaunchRefused(reason))

    def test_assigned_gate_launches_only_after_authorization_and_keeps_unsettled_claims(self) -> None:
        for case in ("completed", "failed tests", "malformed", "blocked", "unknown", "cleanup failed"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                args, assignment, environment = self.assigned_gate(root)
                reference = placement.assigned_claim_reference(assignment)
                identity = claim_state.ClaimIdentity(reference.claim_id, reference.episode, reference.attempt)
                events: list[str] = []
                completed = case in ("completed", "failed tests")
                runs_remote = completed or case in ("malformed", "cleanup failed")
                result_identity = {"attempt": f"mac-gate-{assignment.attempt}", "commit": "commit", "tree": "tree"}
                cloud_gate.save(root / "request.json", {**result_identity, "modules": ["scripts/fixture/test_unit.py"]})
                test_status = int(case == "failed tests")

                def lifecycle(_script: str, phase: str, argv: Path, _arguments: list[str],
                              env: dict[str, str], **_options: object) -> int:
                    events.append(phase)
                    self.assertEqual(env["MAC_QUEUE_HOST"], assignment.destination.host)
                    if phase == "prepare":
                        claim = offload.read_assigned_mac_claim(assignment)
                        assert isinstance(claim, offload.AssignedMacClaim), claim
                        self.assertEqual(claim.record["pid"], os.getpid())
                        _ = argv.write_text(json.dumps(["/bin/zsh", "-c", "fixture"]))
                        _ = argv.with_suffix(".json.close").write_text("fixture-cleanup")
                    return int(phase == "cleanup" and case == "cleanup failed")

                def authorize(request: wire.ConsumePermission) -> wire.DispatchStatus | wire.FirstAuthorization:
                    events.append("authorize")
                    self.assertEqual((request.assignment, request.claim), (assignment, reference))
                    self.assertEqual((root / "fence").read_text(), assignment.destination.host)
                    if case == "unknown":
                        raise wire.AdmissionUnavailable("response lost")
                    if case == "blocked":
                        return wire.DispatchStatus(assignment, placement.CurrentAssignment(),
                                                   placement.PermissionAvailable(), "blocked")
                    return wire.FirstAuthorization(assignment, reference)

                def stream(command: list[str], remote: mac_queue_run.RemoteRun, _ssh: object,
                           emit: Callable[[str], None], **_options: object) -> int:
                    events.append("stream")
                    self.assertIn(assignment.destination.host, command)
                    self.assertIn("episode/attempt", command[-1])
                    self.assertIn("dispatch_status", command[-1])
                    job = gate.GateJobResult("fixture", "test_unit.py", test_status, 1, "")
                    row = {**result_identity, "gate": gate_box.encode_run(gate.GateRun((job,), 1, 0))}
                    emit(mac_gate.RESULT_PREFIX + ("{" if case == "malformed" else json.dumps(row)) + "\n")
                    remote.state = mac_queue_run.RemoteState.EXITED
                    return 0

                def settle_fence(_config: eligibility.SshConnectionSettings, host: str, _command: str,
                                 _target: str, **_options: object) -> offload.LeftoverObservation:
                    events.append("release")
                    self.assertEqual(host, assignment.destination.host)
                    return offload.LeftoverObservation(offload.LeftoversEnded(),
                                                       "dispatch_start_settled=" + offload.pending_start_key(identity))

                def report_execution(report: wire.ExecutionReport) -> wire.DispatchStatus:
                    events.append("report")
                    self.assertEqual((report.assignment, report.claim), (assignment, reference))
                    self.assertEqual(report.path, placement.DispatchPathSettled())
                    self.assertEqual(offload.read_assigned_mac_claim(assignment), offload.MacClaimSettled())
                    self.assertFalse((root / mac_gate.ASSIGNED_COMPLETION).exists())
                    if case == "blocked":
                        self.assertEqual(report.outcome, placement.PrelaunchRefused("blocked"))
                        return wire.DispatchStatus(assignment, placement.ClosedAssignment("blocked"),
                            placement.PermissionAvailable(), retry=placement.DoNotRetry("blocked"))
                    self.assertTrue(completed)
                    self.assertEqual(report.outcome, placement.AttemptCompleted(test_status))
                    return wire.DispatchStatus(assignment, placement.ClosedAssignment("completed"),
                        placement.PermissionConsumed(1))

                with (mock.patch.dict(os.environ, environment, clear=True),
                      mock.patch.object(offload, "read_leftovers", side_effect=settle_fence),
                      mock.patch.object(mac_queue_run, "run_callback", side_effect=lifecycle),
                      mock.patch.object(mac_queue_run, "stream_remote", side_effect=stream),
                      mock.patch.object(mac_queue_run, "capture_command") as place_step,
                      mock.patch.object(admission_client, "consume_permission", side_effect=authorize),
                      mock.patch.object(admission_client, "report_execution", side_effect=report_execution)):
                    self.assertEqual(mac_queue_run.run(args), 0 if completed else 2)
                    place_step.assert_not_called()
                    self.assertEqual("stream" in events, runs_remote)
                    self.assertEqual((root / mac_gate.ASSIGNED_COMPLETION).exists(), completed)
                    self.assertEqual((root / mac_gate.ASSIGNED_REFUSAL).exists(), case == "blocked")
                    self.assertEqual(offload.pending_start_path(reference.target, identity).exists(), case == "cleanup failed")
                    claim = offload.read_assigned_mac_claim(assignment)
                    self.assertIsInstance(claim, offload.AssignedMacClaim if case == "cleanup failed" else offload.MacClaimSettled)
                    expected = ["prepare", "authorize"]
                    if runs_remote:
                        expected.append("stream")
                    expected.append("cleanup")
                    if case != "cleanup failed":
                        expected.append("release")
                    if completed or case == "blocked":
                        expected.append("report")
                    self.assertEqual(events, expected)

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
            self.assertEqual(work[:3], ["cd", "/Mac scratch/checkout", "&&"])
            self.assertEqual(work[3], "MAC_PLACEMENT=" + placed[placed.index("--episode") + 1] + "/attempt")
            self.assertEqual(work[4:], self.command(root)[-3:])
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

    def test_a_caller_watch_failure_after_ready_stops_the_queue_group(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = {**queue_fixture(root), "FAKE_SSH_HOLD": "1"}
            reader, writer = os.pipe()
            try:
                command = self.command(root)
                command[3:3] = ["--caller-fd", str(reader)]
                with subprocess.Popen(command, env=environment, start_new_session=True, pass_fds=(reader,),
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as queue:
                    try:
                        assert queue.stdout is not None
                        queue_output = cast(TextIO, queue.stdout)
                        self.assertTrue(select.select([queue_output], [], [], TEST_HANG_GUARD_SECS)[0])
                        self.assertEqual(queue_output.readline().strip(), "Mac command started")
                        processes = subprocess.check_output(["ps", "-axo", "pid=,ppid=,args="], text=True)
                        watchers: list[int] = []
                        for line in processes.splitlines():
                            fields = line.split(maxsplit=2)
                            if len(fields) == 3 and fields[1] == str(queue.pid) and " watch-caller " in fields[2]:
                                watchers.append(int(fields[0]))
                        self.assertEqual(len(watchers), 1)
                        os.kill(watchers[0], signal.SIGKILL)
                        output, errors = queue.communicate(timeout=10)
                    finally:
                        if queue.poll() is None:
                            send_process_group(queue.pid, signal.SIGKILL)
                            _ = queue.communicate()
                self.assertEqual(queue.returncode, -signal.SIGTERM, output + errors)
                self.assertIn("caller watch ended unexpectedly", errors)
                self.assertEqual([event[0] for event in self.events(root)], ["place-step", "cancel-placement"])
                self.assertIn("signal_group TERM", (root / "stop").read_text())
            finally:
                os.close(reader)
                os.close(writer)

    def test_saved_run_rejects_null_preparation_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory) / "run.json"
            row: dict[str, object] = {"host": "fixture-mac", "arguments": [], "lifecycle_script": "fixture"}
            cloud_gate.save(journal, row)
            self.assertNotIn("lifecycle_started", mac_queue_run.read_run_journal(journal))
            cloud_gate.save(journal, {**row, "lifecycle_started": None})
            with self.assertRaisesRegex(ValueError, "invalid preparation state"):
                _ = mac_queue_run.read_run_journal(journal)
            self.assertTrue(journal.is_file())

    def test_saved_run_rejects_a_string_release_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory) / "run.json"
            saved: dict[str, object] = {"host": "fixture", "arguments": [], "lifecycle_script": "fixture"}
            _ = journal.write_text(json.dumps({**saved, "release_confirmed": "false"}))
            with self.assertRaisesRegex(ValueError, "invalid release confirmation"):
                _ = mac_queue_run.read_run_journal(journal)
            _ = journal.write_text(json.dumps({**saved, "release_confirmed": True}))
            self.assertIs(mac_queue_run.read_run_journal(journal)["settlement"],
                          mac_queue_run.GateSettlement.RELEASE_CONFIRMED)
            _ = journal.write_text(json.dumps({**saved, "settlement": "release-pending"}))
            self.assertIs(mac_queue_run.read_run_journal(journal)["settlement"],
                          mac_queue_run.GateSettlement.RELEASE_PENDING)

    def test_manual_plan_precedes_request_folder_creation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            with mock.patch.dict(os.environ, queue_fixture(home)):
                self.assert_manual_plan()

    def assert_manual_plan(self) -> None:
        root = mac_gate.gate_requests_directory() / "mac-gate-manualfixture"
        request = root / root.name
        journal = mac_gate.queue_runs_directory() / "manualfixture.json"
        mkdir = Path.mkdir

        def inspect(path: Path, mode: int = 0o777, parents: bool = False, exist_ok: bool = False) -> None:
            if path == root:
                self.assertTrue(journal.is_file(), "The request folder has no saved plan")
                plan = mac_queue_run.read_run_journal(journal)
                self.assertEqual(plan["arguments"], [str(request), "1"])
                self.assertIs(plan.get("lifecycle_started"), False)
            mkdir(path, mode, parents, exist_ok)

        gate, queue = socket.socketpair()
        with gate, queue:
            args = mac_queue_run.Arguments()
            args.argv = ["--", str(request), "1"]
            args.owned_close = True
            args.lifecycle_script = str(Path(mac_gate.__file__))
            args.cancel_seconds = mac_gate.checkout_cleanup_seconds()
            args.caller_fd = gate.fileno()
            args.gate_preparation_fd = queue.detach()
            gate.sendall(b"1")
            with mock.patch.object(Path, "mkdir", autospec=True, side_effect=inspect):
                with mac_queue_run.own_gate_request(args):
                    self.assertTrue(root.exists())
            self.assertFalse(journal.exists())
            self.assertFalse(root.exists())

    def test_stale_callback_does_not_stop_the_next_placement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = queue_fixture(root)
            stale = root / mac_gate.QUEUE_RUNS / "stale"
            stale.mkdir(parents=True)
            (stale / "lock").touch()
            saved = stale / "run.json"
            _ = saved.write_text(json.dumps({"lifecycle_script": str(root / "gone"),
                                            "arguments": [], "host": "fake-mac"}))
            result = subprocess.run(self.command(root, "run"), env=environment, capture_output=True,
                                    text=True, timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("cleanup of an ended run failed", result.stderr)
            self.assertTrue(saved.exists())
            self.assertEqual([event[0] for event in self.events(root)], ["place-step", "cancel-placement"])

    def test_ended_gate_uses_the_installed_script_with_an_old_or_missing_callback(self) -> None:
        for old_exists in (False, True):
            with self.subTest(old_exists=old_exists), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                environment = queue_fixture(root)
                stale = root / mac_gate.QUEUE_RUNS / "stale"
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

    def test_cleanup_stops_a_descendant_that_inherits_the_lease(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment = queue_fixture(root)
            callback = root / "callback"
            argv = root / "argv.json"
            child_record = root / "child"
            source = """import os, signal, sys
from pathlib import Path
child = os.fork()
if child == 0:
    signal.pause()
    os._exit(0)
Path(sys.argv[3]).write_text(str(child))
"""
            _ = callback.write_text(f"#!{sys.executable}\n" + source)
            callback.chmod(0o700)
            with mock.patch.dict(os.environ, environment, clear=True):
                budget = mac_gate.checkout_cleanup_seconds()
                try:
                    self.assertEqual(mac_queue_run.run_callback(str(callback), "cleanup", argv,
                                                               [str(child_record)], environment, timeout=budget), 0)
                    child = int(child_record.read_text())
                    self.assertNotIsInstance(process_start(child), Started)
                    with mac_queue_run.creation_lease(argv.with_suffix(".json.cleaning"), fcntl.LOCK_EX):
                        pass
                finally:
                    if child_record.is_file():
                        child = int(child_record.read_text())
                        if isinstance(process_start(child), Started):
                            os.kill(child, signal.SIGKILL)

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
            (saved,) = (root / mac_gate.QUEUE_RUNS).glob("*/run.json")
            recovered = subprocess.run(self.command(root, "run"), env=environment,
                                       capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(recovered.returncode, 0, recovered.stdout + recovered.stderr)
            called = str(saved.with_name("argv.json"))
            self.assertEqual((root / "cleanup_calls").read_text().splitlines(),
                             [f"{callback} cleanup {called}", f"{installed} cleanup-ended {called}"])
            self.assertFalse(saved.exists())

    def assert_caller_cancels_queue(self, *, before_watch: bool) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = {**queue_fixture(root), "FAKE_QUEUE_WAIT": "1"}
            caller, lifetime = os.pipe()
            try:
                if before_watch:
                    os.close(lifetime)
                    lifetime = -1
                command = self.command(root)
                command[command.index("--"):command.index("--")] = ["--caller-fd", str(caller)]
                with subprocess.Popen(command, env=environment, pass_fds=(caller,), start_new_session=True,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as process:
                    try:
                        if not before_watch:
                            wait_until((root / "waiting").exists, root, processes=(process,))
                            os.close(lifetime)
                            lifetime = -1
                        try:
                            output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
                        except subprocess.TimeoutExpired:
                            self.fail("the queue stayed in placement after its caller ended")
                        self.assertEqual(process.returncode, 2, output + errors)
                        self.assertIn("the Mac queue was cancelled", errors)
                        self.assertNotIn("caller watch did not start", errors)
                        if before_watch:
                            self.assertFalse((root / "events").exists())
                        else:
                            self.assertEqual([event[0] for event in self.events(root)],
                                             ["place-step", "cancel-placement"])
                        self.assertEqual(list((root / mac_gate.QUEUE_RUNS).glob("*")), [])
                        self.assertEqual(list((root / mac_queue_run.RECEIPTS).glob("*")), [])
                    finally:
                        if process.poll() is None:
                            send_process_group(process.pid, signal.SIGKILL)
                            _ = process.communicate()
            finally:
                os.close(caller)
                if lifetime >= 0:
                    os.close(lifetime)

    def test_caller_cancels_while_placement_waits(self) -> None:
        self.assert_caller_cancels_queue(before_watch=False)

    def test_caller_cancels_before_the_watch_starts(self) -> None:
        self.assert_caller_cancels_queue(before_watch=True)

    def test_ended_custom_callback_uses_its_saved_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = queue_fixture(root)
            directory = root / mac_gate.QUEUE_RUNS / "ended"
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
                        wait_until((root / ("waiting" if waiting else "running")).exists, root, processes=(process,))
                        if signum == signal.SIGINT:
                            # The queue reads the group before it forwards this line.
                            assert process.stdout is not None
                            queue_output = cast(TextIO, process.stdout)
                            self.assertTrue(select.select([queue_output], [], [], TEST_HANG_GUARD_SECS)[0])
                            self.assertEqual(queue_output.readline().strip(), "Mac command started")
                        if waiting:
                            # A signal to the group also ends the queue call that waits.
                            send_process_group(process.pid, signum)
                        else:
                            process.send_signal(signum)
                        ignored = signum == signal.SIGINT and signal.getsignal(signum) == signal.SIG_IGN
                        if ignored:
                            assert process.stdout is not None
                            self.assertFalse(select.select([process.stdout], [], [], 0.1)[0])
                            process.send_signal(signal.SIGTERM)
                        output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
                    finally:
                        if process.poll() is None:
                            send_process_group(process.pid, signal.SIGKILL)
                            _ = process.communicate()
                self.assertEqual(process.returncode, -(signal.SIGTERM if ignored else signum), output + errors)
                events = [event[0] for event in self.events(root)]
                if signum == signal.SIGINT and not ignored:
                    # Python's own KeyboardInterrupt runs the finally path, which returns the placement.
                    self.assertEqual(events, ["place-step", "cancel-placement"])
                    self.assertEqual(self.events(root)[-1][-1], "the Mac run ended with status 1")
                    self.assertIn("signal_group TERM", (root / "stop").read_text())
                else:
                    # The queue ends the placement when this process ends.
                    self.assertEqual(events, ["place-step"])
                    if not waiting:
                        wait_until((root / "input_closed").exists, root)
                    # The run's state folder stays until the next run cleans it.
                    runs = root / mac_gate.QUEUE_RUNS
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


class MacGateCountTests(gate_fixture.GateSuitesFixture):
    @gate_suites("hooks")
    def test_claude_commands_gate_uses_one_mac_slot(self) -> None:
        repo = self.checkout.rename(self.root / "claude_commands")
        _ = self.git(repo, "remote", "set-url", "origin", "https://github.com/natepiano/claude_commands.git")
        with (repo / ".git/info/exclude").open("a") as exclusions:
            _ = exclusions.write(f"\n/{mac_gate.MODULE_LIST}\n")
        self.write(repo / mac_gate.MODULE_LIST, mac_gate.MODULE_LIST_HEADER)
        controls = caller_provider_controls(self, self.admission)
        paths = controls.claim_paths("mac")
        self.environment["MAC_TEST_STATE_DIR"] = str(paths.directory)
        self.environment["TMPDIR"] = str(self.state)
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, self.environment)))
        self.configure_gate(f"program_first_peak_gib.{SuiteMode.GATE.value}=0.5\nremote_start_hold_seconds=5\n")
        controls.set_host("mac", placement.AllowedHost())
        controls.set_host("natedev", placement.TemporaryExclusion("blocked"))

        queue = self.state / "gate-count-queue.py"
        self.write(queue, f"from {mac_fixture.__name__} import run_queue_with_fixture_transport\n"
                   + "raise SystemExit(run_queue_with_fixture_transport())\n")
        self.write(self.state / "assigned-output", "")
        scripts = Path(mac_queue_run.__file__).resolve().parents[1]
        launcher = str(scripts / "lib/py")
        queue_command = (launcher, str(queue))
        config = Path(self.environment["CONF_DIR"]) / "mac_gate.conf"
        _ = write_config_fixture(config, "admission_client_command="
                                 + shlex.join((launcher, str(scripts / "build_queue/admission_client.py"))) + "\n")
        preamble = (
            "from scripts.aws_target import cloud_gate\n"
            + "from scripts.mac_test import mac_gate\n"
            + "cloud_gate.route = lambda *_args, **_kwargs: 'fixture cloud unavailable'\n"
            + f"mac_gate.QUEUE_COMMAND = {queue_command!r}\n"
        )
        command = [*self.gate_command(preamble=preamble), "--repo", str(repo), "--tests-only", "--full"]
        environment = {**self.environment, "MAC_GATE_TRANSPORT_CASE": mac_fixture.MacTransportCase.HELD.value}
        log = self.state / "first-gate.log"
        ready = self.state / "assigned-ready"
        with log.open("w") as output, subprocess.Popen(
            command, cwd=self.root, env=environment, stdin=subprocess.DEVNULL,
            stdout=output, stderr=output, start_new_session=True,
        ) as first:
            held_claims: tuple[claim_state.HeldClaim, ...] = ()
            try:
                start_budget = mac_test.read_config().connect_timeout_s + mac_test.message_timeout_s()
                try:
                    wait_until(lambda: ready.exists() or first.poll() is not None, self.state,
                               timeout=start_budget, processes=(first,))
                except TimeoutError as error:
                    with self.admission.loop_turn():
                        evidence = self.admission.server.placement_eligibility.copy()
                    self.fail(f"The first gate did not reach the Mac transport: {error}\n{log.read_text()}\n{evidence}")
                self.assertTrue(ready.exists(), log.read_text())
                held_claims = controls.claims("mac")
                self.assertEqual(tuple(claim.scope for claim in held_claims),
                                 (claim_state.RepositoryScope("claude_commands"),),
                                 "The first gate reached the Mac without a claude_commands claim")
                self.assertIsNone(first.poll(), log.read_text())

                # The fake transport has no remote process for the startup probe to observe.
                controls.advance(conf.read("build_admission").whole("remote_start_hold_seconds"))
                controls.set_host("natedev", placement.AllowedHost())
                self.check_local_gate_while_mac_is_held(command, first.pid)
                self.assertEqual(controls.claims("mac"), held_claims)

                self.check_manual_gate_waits_for_the_held_claim(repo, queue_command, environment, first, held_claims)
            finally:
                if first.poll() is None:
                    first.terminate()
                _ = first.wait(timeout=TEST_HANG_GUARD_SECS)
                for claim in held_claims:
                    self.assert_process_stopped(claim.record["pid"])
        self.assertEqual(controls.claims("mac"), ())

    def check_manual_gate_waits_for_the_held_claim(self, repo: Path, queue: tuple[str, str],
                                                   environment: dict[str, str], first: subprocess.Popen[bytes],
                                                   held_claims: tuple[claim_state.HeldClaim, ...]) -> None:
        output = self.state / "manual-mac-result.json"
        busy = self.state / "manual-mac-busy"
        allow_finish = self.state / "allow-manual-finish"
        ready = self.state / "assigned-ready"
        ready.unlink()
        source = ("from scripts.mac_test import mac_gate\n"
                  + f"mac_gate.QUEUE_COMMAND = {queue!r}\n"
                  + "raise SystemExit(mac_gate.main())\n")
        log = self.state / "manual-mac.log"
        reply_bytes = self.admission.server.reply_bytes

        def observe(reply: wire.Reply, *, version: int = wire.VERSION) -> bytes:
            if isinstance(reply, wire.PlacementWait) and any(
                    candidate.destination.target == "mac" and candidate.eligibility == placement.TemporaryExclusion("busy")
                    for candidate in reply.eligibility):
                write_text_atomic(busy, reply.episode)
            return reply_bytes(reply, version=version)

        manual_env = {**environment, "MAC_GATE_TRANSPORT_CASE": mac_fixture.MacTransportCase.HOLD_MANUAL_TESTS.value}
        budget = mac_test.read_config().connect_timeout_s + mac_test.message_timeout_s()
        legacy_queue_pid = 0
        with (mock.patch.object(self.admission.server, "reply_bytes", side_effect=observe),
              log.open("w") as report,
              subprocess.Popen([queue[0], "-c", source, "run", str(repo), str(output)], cwd=self.root, env=manual_env,
                               stdin=subprocess.DEVNULL, stdout=report, stderr=report, start_new_session=True) as manual):
            try:
                self.wait_for_manual_gate(busy, manual, log, budget)
                episode = busy.read_text()
                with self.admission.loop_turn():
                    request = self.admission.server.placement_requests[episode]
                if request.owner.pid != manual.pid:
                    legacy_queue_pid = request.owner.pid
                self.assertIsNone(manual.poll(), log.read_text())
                self.assertFalse(output.exists())
                self.assertFalse(ready.exists())
                self.assertEqual(tuple(claim_state.read_claims(claim_state.state_paths("mac"))), held_claims)
                self.assertEqual(request.owner.pid, manual.pid, "The manual gate did not own the placement request")
                self.assertEqual(request.constraints.selection, placement.MacOnlyHost())
                first.terminate()
                _ = first.wait(timeout=budget)
                self.wait_for_manual_gate(ready, manual, log, budget)
                claims = claim_state.read_claims(claim_state.state_paths("mac"))
                claim, = claims
                self.assertEqual(claim.scope, claim_state.RepositoryScope("claude_commands"))
                self.assertEqual(claim.record["episode"], request.episode)
                self.assertNotEqual(claim.record["claim_id"], held_claims[0].record["claim_id"])
                self.assertEqual(Path(ready.read_text()).name, "mac-gate-" + claim.record["attempt"])
                allow_finish.touch()
                self.assertEqual(manual.wait(timeout=budget), 0, log.read_text())
                self.assertTrue(output.is_file())
            finally:
                allow_finish.touch()
                if manual.poll() is None:
                    manual.terminate()
                _ = manual.wait(timeout=budget)
                if legacy_queue_pid:
                    self.assert_process_stopped(legacy_queue_pid)

    def wait_for_manual_gate(self, marker: Path, process: subprocess.Popen[bytes], log: Path, budget: float) -> None:
        try:
            wait_until(lambda: marker.exists() or process.poll() is not None, self.state,
                       timeout=budget, processes=(process,))
        except TimeoutError as error:
            self.fail(f"The manual gate did not produce {marker.name}: {error}\n{log.read_text()}")
        self.assertTrue(marker.exists(), log.read_text())

    def check_local_gate_while_mac_is_held(self, command: list[str], first_pid: int) -> None:
        environment = {**self.environment, "MAC_GATE_TRANSPORT_CASE": mac_fixture.MacTransportCase.COMPLETE.value}
        budget = mac_test.read_config().connect_timeout_s + mac_test.message_timeout_s()
        try:
            second = subprocess.run(command, cwd=self.root, env=environment, stdin=subprocess.DEVNULL,
                                    capture_output=True, text=True, check=False, timeout=budget)
        except subprocess.TimeoutExpired as error:
            with self.admission.loop_turn():
                evidence = self.admission.server.placement_eligibility.copy()
                replies = [waiter.last_reply for waiter in self.admission.server.waiters.values()]
                assignments = tuple(self.admission.server.assignments.values())
            self.fail(f"The second gate did not finish: {error.stdout!r} {error.stderr!r}\n"
                      + f"{evidence}\n{replies}\n{assignments}")
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        with self.admission.loop_turn():
            requests = [request for request in self.admission.server.placement_requests.values()
                        if request.owner.pid != first_pid and request.profile.step == SuiteMode.GATE.value]
            second_request, = requests
            self.assertEqual(tuple(item.destination.target for item in second_request.constraints.candidates),
                             ("mac", "natedev"))
            attempts = [row for row in self.admission.server.assignments.values()
                        if row.assignment.episode == second_request.episode]
        self.assertEqual([row.assignment.destination.target for row in attempts
                          if row.assignment.destination.provider != "mac"], ["natedev"])
        for row in attempts:
            if row.assignment.destination.provider == "mac":
                self.assertEqual(row.state, placement.ClosedAssignment("busy"))
                self.assertEqual(row.permission, placement.PermissionAvailable())
        starts = [row["module"] for row in self.module_events() if row["event"] == "start"]
        self.assertEqual(starts, ["test_first.py"])


if __name__ == "__main__":
    _ = unittest.main()
