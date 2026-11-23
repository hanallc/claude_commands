"""A copy run waits for memory before it holds a repository slot."""
from __future__ import annotations

from contextlib import ExitStack, nullcontext
from dataclasses import replace
import json
import os
from pathlib import Path
import select
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
from textwrap import dedent
import threading
import time
from typing import ClassVar, NotRequired, TypedDict, cast, override
from types import SimpleNamespace
from zoneinfo import ZoneInfo
import unittest
from unittest import mock

from scripts.build_queue import execution_identity
from scripts.build_queue import admission_client as client, admission_protocol as wire, admission_service, first_step, placement
from scripts.buildlog import repository, treekey
from scripts.build_queue.admission_test_support import AdmissionFixture, private_caller_provider
from scripts.build_targets import target_config
from scripts.copies import copies
from scripts.copies.copies_test_support import AdmissionWaitReached, copies_environment, stop_before_slot
from scripts.build_queue import step_estimator
from scripts.mac_test import eligibility, offload
from scripts.disks import slots
from scripts.direct.verify_mac_offload_test_support import VerifyMacOffloadFixture, reap
from scripts.direct.wait_for_test_state import wait_for_pid, wait_until
from scripts.lib import build_folders, build_slots, conf, copy_record, nix_env
from scripts.build_queue.admission_jobs import StepJob
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.lib.shell_test_support import shell_functions
from scripts.lint.memory_test_support import fake_memory_readings, write_config_fixture, write_pressure_reading
from scripts.production.cli_fixture import ImmutableGitSeed, locked_environment
from scripts.production.staff_test_support import build_fixture_sessions, fixture_member, isolated_staff_file
from scripts.production import bump_gate, checkpoint_gate, staff, waiting
from scripts.production.production_doc import Production


CHECKOUT = Path(__file__).resolve().parents[2]
GATE_COMMAND = """from pathlib import Path
import json, os, sys, time
sys.path.insert(0, os.environ['SLOT_ORDER_CHECKOUT'])
from scripts.build_queue.queue_place import queue_place

for phase in range(2):
    with queue_place('gate', Path.cwd()):
        print(json.dumps({'phase': phase, 'cwd': str(Path.cwd()), 'run': os.environ.get('BUILDLOG_RUN', ''), 'at': time.monotonic()}), flush=True)
        if not sys.stdin.buffer.read(1):
            break
"""

# Use the real placement and copy APIs for callers that need no Cargo process.
CAPACITY_CALLER = """from pathlib import Path
import os
from socket import AF_UNIX, SOCK_STREAM, socket
import sys
import time

from scripts.build_queue import admission_client as client
from scripts.lib import build_slots

endpoint, caller, source_key, folder = sys.argv[1:]
source = Path(folder)
os.chdir(source)
assignment = Path(os.environ["TMPDIR"]) / "assignment.json"
status = client.main(["place-step", "--pid", str(os.getpid()), "--episode", f"capacity-{os.getpid()}",
    "--since", str(time.time()), "--repo-root", str(source), "--source-key", source_key,
    "--kind", "test", "--package", "sample", "--local-only", "--assignment-file", str(assignment),
    "--", "cargo", "nextest", "run"])
if status:
    raise SystemExit(status)
try:
    with build_slots.take_copy(source, "HEAD", showrunner="builds", unit="slot-order", work="test",
            purpose="competing copy") as copy:
        with socket(AF_UNIX, SOCK_STREAM) as ready:
            ready.connect(endpoint)
            ready.sendall(f"{caller}\\t{copy.path}\\n".encode())
            ready.recv(1)
finally:
    client.finish_step(assignment, 0)
"""


class StartedStep(TypedDict):
    phase: int
    cwd: str
    run: str
    at: float
    assignment: NotRequired[str]


class SlotOrderTests(unittest.TestCase):
    repository_seed: ClassVar[ImmutableGitSeed]
    root: Path = Path()
    source: Path = Path()
    slot_root: Path = Path()
    owner: str = ""
    build_sessions: dict[str, str] = {}
    queue: AdmissionFixture = cast(AdmissionFixture, cast(object, None))
    processes: list[subprocess.Popen[str]] = []
    requests: list[wire.Request] = []
    request_times: list[tuple[wire.Request, float]] = []
    replies: list[wire.Reply] = []
    waiting: threading.Event = threading.Event()

    @classmethod
    @override
    def setUpClass(cls) -> None:
        super().setUpClass()
        seed = Path(cls.enterClassContext(tempfile.TemporaryDirectory(prefix="slot-order-seed-")))
        cls.repository_seed = ImmutableGitSeed.create(seed, "main", "Slot Tests", "slots@example.invalid",
            {"Cargo.toml": "[workspace]\n", "tracked.txt": "committed\n", "gate.py": GATE_COMMAND,
             ".gitignore": "target/\n"})

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="slot-order-"))).resolve()
        environment = {**copies_environment(self.root, repository="origin"), **build_fixture_sessions("")}
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, environment)))
        self.queue = self.enterContext(AdmissionFixture(root=Path(environment["CONF_DIR"])))
        self.queue.clock.epoch = time.time()
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {**environment,
            "PYTHONPATH": str(CHECKOUT), "SLOT_ORDER_CHECKOUT": str(CHECKOUT),
            "HERDR_BIN": "/nonexistent/herdr", "BUILDLOG_RUN": ""})))
        for key in ("CARGO_TARGET_DIR", "CARGO_BUILD_BUILD_DIR"):
            _ = os.environ.pop(key, None)
        _ = self.enterContext(isolated_staff_file(self.root / "staff.json", {}))
        parent = fixture_member("showrunner", name="builds")
        member = fixture_member("unit", name="slot-order", parent=parent.id, runtime_id=staff.caller_identity())
        self.owner = str(member.id)
        self.build_sessions = build_fixture_sessions(member.current_session)
        os.environ.update(self.build_sessions)
        self.source = self.root / "repository" / "checkout"
        self.repository_seed.copy_to(self.source, self.source.parent / "origin.git")
        self.slot_root = self.root / "build"
        self.processes = []
        self.requests = []
        self.request_times = []
        self.replies = []
        self.waiting = threading.Event()
        handle_request = self.queue.server.handle_request

        def observe_request(request: wire.Request, connection: admission_service.Connection | admission_service.NoConnection
                            = admission_service.NO_CONNECTION) -> list[wire.Reply]:
            self.requests.append(request)
            self.request_times.append((request, time.monotonic()))
            return handle_request(request, connection)

        def observe_reply(reply: wire.Reply) -> wire.Reply:
            self.replies.append(reply)
            if isinstance(reply, (wire.Wait, wire.PlacementWait)):
                self.waiting.set()
                # Wake a file waiter that also ends when its process exits.
                (self.root / "queue-wait").touch()
            return reply

        _ = self.enterContext(mock.patch.object(self.queue.server, "handle_request", side_effect=observe_request))
        _ = self.enterContext(mock.patch.object(self.queue.server, "reply_filter", side_effect=observe_reply))
        _ = self.enterContext(self.queue.serving())
        self.addCleanup(self.stop_processes)

    def stop_processes(self) -> None:
        for process in self.processes:
            if process.poll() is None:
                process.kill()
            _ = process.communicate(timeout=TEST_HANG_GUARD_SECS)

    def start(self, command: tuple[str, ...], *options: str, since_age: float = 0,
              separate_command: bool = True) -> subprocess.Popen[str]:
        launcher = [sys.executable, "-m", "scripts.copies.copies"]
        if since_age:
            program = ("import sys,time; from types import SimpleNamespace; from unittest.mock import patch; "
                + "from scripts.copies import copies; from scripts.build_queue import first_step; "
                + f"clock=SimpleNamespace(time=lambda: time.time()-{since_age!r})\n"
                + "with patch.object(first_step, 'time', clock): raise SystemExit(copies.main(sys.argv[1:]))\n")
            launcher = [sys.executable, "-c", program]
        separator = ["--"] if separate_command else []
        process = subprocess.Popen([*launcher, "run", "--repo", str(self.source),
            "--commit", "HEAD", *options, *separator, *command], cwd=CHECKOUT, env=dict(os.environ),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.processes.append(process)
        return process

    def next_step(self, process: subprocess.Popen[str]) -> StartedStep:
        assert process.stdout is not None
        self.assertTrue(select.select([process.stdout], [], [], TEST_HANG_GUARD_SECS)[0],
            "the command did not reach its admitted step")
        line = cast(str, process.stdout.readline())
        if not line:
            output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
            self.fail("the command exited before its admitted step: " + output + errors)
        return cast(StartedStep, json.loads(line))

    def finish_step(self, process: subprocess.Popen[str]) -> None:
        assert process.stdin is not None
        _ = process.stdin.write("x")
        process.stdin.flush()

    def finish(self, process: subprocess.Popen[str]) -> None:
        output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(process.returncode, 0, output + errors)

    def start_shell_cargo(self, runtime: Path) -> subprocess.Popen[str]:
        config = Path(os.environ["CONF_DIR"]) / "build_folders.conf"
        _ = config.write_text(config.read_text().replace("folders.check=report", "folders.check=enforce"))
        self.assertEqual(conf.read("build_folders").text("folders.check"), "enforce")
        front, tools = self.root / "front", self.root / "tools"
        front.mkdir()
        tools.mkdir()
        (front / "cargo").symlink_to(CHECKOUT / "scripts/build_hold/cargo")
        systemd = tools / "systemd-run"
        _ = systemd.write_text("#!/bin/sh\nexit 99\n")
        systemd.chmod(0o700)
        cargo = tools / "cargo"
        _ = cargo.write_text(f"#!{sys.executable}\n" + dedent(r"""
            import json, os, sys
            from pathlib import Path

            assert sys.argv[1:] == ["build", "--manifest-path", str(Path.cwd() / "Cargo.toml")]
            assert Path(sys.argv[-1]).is_file()
            assert os.environ["CARGO_FOLDER_CHECKED"] == "1"
            target = Path(os.environ["CARGO_TARGET_DIR"])
            assert target == Path.cwd() / "target"
            print(json.dumps({"cwd": str(Path.cwd())}), flush=True)
            assert sys.stdin.buffer.read(1) == b"x"
            app = target / "debug/app"
            app.parent.mkdir(parents=True, exist_ok=True)
            app.write_text("#!/bin/sh\necho app-ran\n")
            app.chmod(0o700)
            """).lstrip("\n"))
        cargo.chmod(0o700)
        environment = {
            "PATH": os.pathsep.join((str(front), str(tools), os.environ["PATH"])),
            "BUILD_HOLD_DIR": str(self.root / "holds"),
            "CARGO_WRAPPER_CGROUP_FILE": str(self.root / "unscoped-cgroup"),
            "BUILDLOG_SCOPE": "0",
            "BUILDLOG_STEP_SCOPE": "",
            "CARGO_FOLDER_CHECKED": "",
            "XDG_RUNTIME_DIR": str(runtime),
        }
        with mock.patch.dict(os.environ, environment):
            return self.start(("bash", "-c", 'cargo build --manifest-path "$PWD/Cargo.toml" && ./target/debug/app'),
                              "--publish", "target/debug/app")

    def assert_shell_cargo_finishes(self, process: subprocess.Popen[str], checkout: Path) -> None:
        self.finish_step(process)
        output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(process.returncode, 0, output + errors)
        self.assertIn("app-ran", output.splitlines())
        publication, = [line.removeprefix("published: ").rsplit(" ", 1)[0]
                        for line in output.splitlines() if line.startswith("published: ")]
        self.assertEqual(Path(publication).read_bytes(), (checkout / "target/debug/app").read_bytes())
        self.assertEqual(build_folders.build_folder_state(checkout), "free")
        self.assertTrue(self.queue.callers_released())
        self.assertFalse((self.source / "target/debug/app").exists())

    def test_shell_cargo_admits_in_its_leased_copy_and_runs_the_app(self) -> None:
        self.queue.hold_starts()
        runtime = self.queue.socket_dir
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as bus:
            bus.bind(str(runtime / "bus"))
            process = self.start_shell_cargo(runtime)
        self.assertTrue(self.waiting.wait(TEST_HANG_GUARD_SECS), "Cargo did not enter admission")
        checkout = self.slot_root / "origin/slot-order/1"
        self.assertEqual(build_folders.build_folder_state(checkout), "held")
        self.assertFalse((checkout / "target/debug/app").exists())
        write_pressure_reading(self.queue.root / "pressure")
        self.queue.server.wake_loop()
        self.assertEqual(Path(self.next_step(process)["cwd"]), checkout)
        self.assertEqual(build_folders.build_folder_state(checkout), "held")
        self.assertEqual(len(self.queue.caller_reservations()), 1)
        self.assert_shell_cargo_finishes(process, checkout)

    def test_shell_cargo_direct_uses_its_leased_copy_and_runs_the_app(self) -> None:
        self.queue.hold_starts()
        process = self.start_shell_cargo(self.root / "no-bus")
        checkout = self.slot_root / "origin/slot-order/1"
        self.assertEqual(Path(self.next_step(process)["cwd"]), checkout)
        self.assertEqual(build_folders.build_folder_state(checkout), "held")
        self.assert_shell_cargo_finishes(process, checkout)
        self.assertEqual(self.requests, [])

    def report(self) -> slots.SlotReport:
        return slots.read_slots(self.slot_root, conf.read("build_folders"),
            self.queue.server.host, None, self.queue.clock.now(), build_folders.build_folder_state,
            owners=lambda owner: owner)

    def test_scratch_queue_command_waits_before_it_creates_a_slot(self) -> None:
        self.queue.hold_starts()
        owner = fixture_member("showrunner", name="scratch-owner")
        production = Production(self.root / "production.md", owner.name, "main", self.source,
                                self.root / "production.log", ZoneInfo("UTC"), owner)
        unit = waiting.Unit("scratch-unit", "", "main", self.source, None, owner.id)
        command = shlex.join((sys.executable, str(self.source / "gate.py"), "--tests-only"))
        execute = mock.Mock(side_effect=AssertionError("the scratch command started before admission"))
        with (mock.patch.object(waiting, "named_unit", return_value=unit),
              mock.patch.object(waiting, "subprocess", SimpleNamespace(run=execute, STDOUT=subprocess.STDOUT)),
              self.assertRaises(AdmissionWaitReached), stop_before_slot(self, self.slot_root)):
            waiting.scratch_test(production, unit.name, command, self.root / "scratch-state")

    def test_bump_queue_command_waits_before_it_creates_a_slot(self) -> None:
        self.queue.hold_starts()
        tip = subprocess.run(["git", "-C", str(self.source), "rev-parse", "HEAD"], check=True,
            capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS).stdout.strip()
        command = [sys.executable, str(self.source / "gate.py"), "--tests-only"]
        record: checkpoint_gate.BumpGateRecord = {
            "tip": tip, "checkpoint": tip, "production_doc": str(self.root / "production.md"),
            "owner_id": self.owner, "unit_id": self.owner, "name": "fixture", "merge_branch": "main",
            "checkout": str(self.source), "production_log": "production.log", "zone": "UTC", "unit": "fixture",
            "delivered_to": [], "paths": [], "merge_tests": [command], "flakes": [], "flaky_tests": [],
            "started": "2026-11-17T00:00:00Z", "pid": 0, "proc_start": "", "result": "pending",
            "detail": "", "log": str(self.root / "bump.log")}
        path = self.root / "bump.json"
        checkpoint_gate.save_record(path, record)
        with (mock.patch.object(bump_gate, "run_checkpoint_test_steps",
                  side_effect=AssertionError("the bump command started before admission")),
              mock.patch.object(bump_gate, "complete_gate",
                  side_effect=AssertionError("the pending bump gate sent a result")),
              self.assertRaises(AdmissionWaitReached), stop_before_slot(self, self.slot_root)):
            _ = bump_gate.run_gate(path)
        request = next(request for request in self.requests if isinstance(request, wire.AdmitRequest))
        self.assertEqual(request.identity.job, StepJob("test-all"))

    def test_bump_admission_selects_the_package_from_its_tip(self) -> None:
        tools = self.verify_source()
        environment = self.verify_run_environment(tools)
        _ = (Path(environment["CONF_DIR"]) / "build_targets.conf").write_text(
            "targets=natedev\nkind.natedev=local\nplatform.natedev=linux\n")
        tip = copies.commit_at(self.source, "HEAD")
        manifest = self.source / "Cargo.toml"
        _ = manifest.write_text(manifest.read_text().replace('name = "sample"', 'name = "renamed"'))
        _ = copies.git(self.source, "add", "Cargo.toml")
        _ = copies.git(self.source, "-c", "user.name=Test", "-c", "user.email=slots@example.invalid",
                       "commit", "-qm", "rename the current package")
        record: checkpoint_gate.BumpGateRecord = {
            "tip": tip, "checkpoint": tip, "production_doc": str(self.root / "production.md"),
            "owner_id": self.owner, "unit_id": self.owner, "name": "fixture", "merge_branch": "main",
            "checkout": str(self.source), "production_log": "production.log", "zone": "UTC", "unit": "fixture",
            "delivered_to": [], "paths": ["src/lib.rs"], "merge_tests": [], "flakes": [], "flaky_tests": [],
            "started": "2026-11-17T00:00:00Z", "pid": 0, "proc_start": "", "result": "pending",
            "detail": "", "log": str(self.root / "bump.log")}
        path = self.root / "bump.json"
        checkpoint_gate.save_record(path, record)
        self.queue.hold_starts()
        write_pressure_reading(Path(environment["BUILDLOG_MEMORY_PRESSURE"]), full_avg10=99)
        with (mock.patch.dict(os.environ, {**environment, "MERGE_CHECKPOINT_VERIFY": str(CHECKOUT / "scripts/direct/verify.sh")}),
              mock.patch.object(bump_gate, "run_checkpoint_test_steps",
                  side_effect=AssertionError("the bump command started before admission")),
              mock.patch.object(bump_gate, "complete_gate",
                  side_effect=AssertionError("the pending bump gate sent a result")),
              self.assertRaises(AdmissionWaitReached), stop_before_slot(self, self.slot_root)):
            _ = bump_gate.run_gate(path)
        request = next(request for request in self.requests if isinstance(request, wire.PlacementRequest))
        self.assertEqual(request.constraints.package, "sample")

    def test_admitted_command_restores_its_detached_environment_after_success_and_error(self) -> None:
        for previous in ("", "caller-value"):
            for fails in (False, True):
                with self.subTest(previous=previous, fails=fails), mock.patch.dict(os.environ):
                    if previous:
                        os.environ["UNIT_DETACHED_RUN"] = previous
                    else:
                        _ = os.environ.pop("UNIT_DETACHED_RUN", None)
                    command = (sys.executable, str(self.source / "gate.py"), "--tests-only")
                    with self.assertRaisesRegex(ValueError, "private command failed") if fails else nullcontext():
                        with first_step.admitted_run(command, self.source) as admitted:
                            self.assertEqual(os.environ["UNIT_DETACHED_RUN"], "1")
                            admitted.handoff(self.source)
                            self.assertEqual(os.environ["BUILDLOG_PREPARED_COPY"], str(self.source))
                            self.assertEqual(os.environ["UNIT_DETACHED_RUN"], "1")
                            if fails:
                                raise ValueError("private command failed")
                    if previous:
                        self.assertEqual(os.environ["UNIT_DETACHED_RUN"], previous)
                    else:
                        self.assertNotIn("UNIT_DETACHED_RUN", os.environ)

    def test_affected_commands_reserve_memory_for_the_affected_gate(self) -> None:
        gate = str(self.source / "gate.py")
        for command, expected in (
            ((sys.executable, gate, "--affected", "--full"), "test-affected"),
            ((sys.executable, str(self.source / "targeted_test.py")), "test-affected"),
            ((sys.executable, "-X", "--affected", gate, "--tests-only"), "gate"),
        ):
            with self.subTest(command=command):
                offset = len(self.requests)
                with first_step.admitted_run(command, self.source) as admitted:
                    admitted.handoff(self.source)
                requests = [request for request in self.requests[offset:] if isinstance(request, wire.AdmitRequest)]
                self.assertTrue(requests)
                self.assertEqual({request.profile.step for request in requests}, {expected})

    def test_remote_adoption_replays_with_the_parent_owner_and_original_queue_time(self) -> None:
        arguments_file = self.root / "placement-arguments"
        functions = shell_functions(CHECKOUT / "scripts/direct/verify.sh", "placement_once")
        script = functions + r'''
set -eu
verify_helper() {
    if [[ "$2" == assigned-info ]]; then
        printf 'settings\tmacos\tsession\n'
    else
        printf '%s\0' "$@" >> "$TEST_PLACEMENT_ARGUMENTS"
        printf 'ssh\tmac\tmac\t1\tparent-episode\tattempt\n'
    fi
}
placement_failed() { exit "$1"; }
BUILDLOG_FIRST_STEP_ASSIGNMENT="$HOLD_WAIT_DIR/parent-assignment.json"
BUILDLOG_FIRST_STEP_ROUTE=$'ssh\tmac\tmac\t1\tparent-episode\tattempt'
BUILDLOG_FIRST_STEP_SINCE=123.25
BUILDLOG_RUN=4242:parent-start
BUILDLOG_MEM_ADMIT_SCRIPT=admission-client
BUILD_TARGETS_SCRIPT=build-targets
DOC_REPO_TOP=$PWD
PKG=sample CMD=check PLACEMENT_SOURCE_KEY=source PLACEMENT_SESSION=session
PLACEMENT_OWNER_PID=$$ PLACEMENT_EPISODE=child-episode PLACEMENT_SINCE=999
PLACEMENT_ASSIGNMENT_FILE="$HOLD_WAIT_DIR/child-assignment.json"
PLACEMENT_GRANT_FILE="$HOLD_WAIT_DIR/grant.sh"
PLACEMENT_COMMAND=(cargo check --package sample)
placement_once
printf '%s\n' "$PLACEMENT_OWNER_PID" "$PLACEMENT_SINCE" "$PLACEMENT_EPISODE" "$PLACEMENT_ASSIGNMENT_FILE"
[[ ! -e "$TEST_PLACEMENT_ARGUMENTS" ]]
[[ -z "${BUILDLOG_FIRST_STEP_ASSIGNMENT:-}${BUILDLOG_FIRST_STEP_ROUTE:-}${BUILDLOG_FIRST_STEP_SINCE:-}" ]]
placement_once
'''
        for slot_source in ("", str(self.source)):
            with self.subTest(slot_source=slot_source):
                arguments_file.unlink(missing_ok=True)
                environment = {**os.environ, "HOLD_WAIT_DIR": str(self.root),
                               "TEST_PLACEMENT_ARGUMENTS": str(arguments_file), "VERIFY_SLOT_SOURCE": slot_source}
                result = subprocess.run(["bash", "--noprofile", "--norc", "-c", script], cwd=self.source,
                    env=environment, capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(result.stdout.splitlines(),
                    ["4242", "123.25", "parent-episode", str(self.root / "parent-assignment.json")])
                arguments = arguments_file.read_text().split("\0")
                self.assertEqual(arguments.count("place-step"), 1)
                self.assertEqual(arguments[arguments.index("--pid") + 1], "4242")
                self.assertEqual(arguments[arguments.index("--since") + 1], "123.25")
                self.assertEqual(arguments[arguments.index("--episode") + 1], "parent-episode")
                self.assertEqual(arguments[arguments.index("--assignment-file") + 1], str(self.root / "parent-assignment.json"))
                self.assertEqual("--run" in arguments, not slot_source)

    def test_waiting_and_stopped_runs_leave_no_slot_or_lease(self) -> None:
        self.queue.hold_starts()
        process = self.start((sys.executable, str(self.source / "gate.py"), "--tests-only"),
                             separate_command=False)
        self.assertTrue(self.waiting.wait(TEST_HANG_GUARD_SECS), "the copy run did not enter the memory queue")
        with self.queue.loop_turn():
            self.assertTrue(self.queue.server.waiters)
            self.assertFalse(self.queue.caller_reservations())
            reply, = self.queue.server.handle_request(wire.ViewRequest())
            assert isinstance(reply, wire.ViewReply)
            waiting, = reply.reading.view.waiting
            self.assertEqual(waiting.job, StepJob("test-all"))
        self.assertFalse(self.slot_root.exists())
        self.assertEqual(self.report()["groups"], [])
        process.kill()
        _ = process.communicate(timeout=TEST_HANG_GUARD_SECS)
        self.assertFalse(self.slot_root.exists())
        self.assertFalse(list(self.slot_root.glob("*/*/*.lease.json")))

    def test_first_step_and_later_steps_reuse_the_run_and_keep_its_slot(self) -> None:
        occupied = self.queue.occupy_host()
        self.queue.hold_starts()
        _ = (self.source / "tracked.txt").write_text("applied after admission\n")
        ready, proceed = self.root / "preparation-ready", self.root / "preparation-proceed"
        for path in (ready, proceed):
            os.mkfifo(path)
        ready_fd = os.open(ready, os.O_RDWR | os.O_NONBLOCK)
        proceed_fd = os.open(proceed, os.O_RDWR | os.O_NONBLOCK)
        self.addCleanup(os.close, ready_fd)
        self.addCleanup(os.close, proceed_fd)
        binary = shutil.which("git")
        assert binary is not None
        scripts = self.root / "bin"
        scripts.mkdir()
        wrapper = scripts / "git"
        _ = wrapper.write_text("#!/bin/sh\n"
            + "worktree=0 add=0\n"
            + 'for word do case "$word" in worktree) worktree=1;; add) add=1;; esac; done\n'
            + 'if [ "$worktree$add" = 11 ]; then\n'
            + f"  printf 'ready\\n' > {shlex.quote(str(ready))}\n"
            + f"  read -r permission < {shlex.quote(str(proceed))} || :\n"
            + "fi\n"
            + f'exec {shlex.quote(binary)} "$@"\n')
        wrapper.chmod(0o755)
        with mock.patch.dict(os.environ, {"PATH": str(scripts) + os.pathsep + os.environ["PATH"]}):
            process = self.start((sys.executable, str(self.source / "gate.py"), "--tests-only"),
                "--apply-uncommitted", str(self.source))
        self.assertTrue(self.waiting.wait(TEST_HANG_GUARD_SECS))
        self.assertFalse(self.slot_root.exists())
        with self.queue.loop_turn():
            write_pressure_reading(self.queue.root / "pressure")
            self.queue.server.wake_loop()
        self.assertTrue(select.select([ready_fd], [], [], TEST_HANG_GUARD_SECS)[0],
            "the copy run did not reach preparation")
        self.assertEqual(os.read(ready_fd, 6), b"ready\n")
        with self.queue.loop_turn():
            preparation, = self.queue.caller_reservations().values()
            self.assertEqual(preparation.owner.pid, process.pid)
            self.assertGreater(preparation.need, 0)
            self.queue.free(0)
        # Keep the held slot alive across the handoff, independent of scheduling.
        with mock.patch.object(self.queue.clock, "advance", return_value=None):
            self.waiting.clear()
            _ = os.write(proceed_fd, b"x\n")
            first = self.next_step(process)
        self.assertEqual(first["phase"], 0)
        self.assertFalse(self.waiting.is_set(), "the first command step entered the memory queue again")
        self.assertTrue(first["run"].startswith(f"{process.pid}:"), first)
        tree = Path(first["cwd"])
        self.assertEqual(tree, self.slot_root / "origin/slot-order/1")
        self.assertEqual((tree / "tracked.txt").read_text(), "applied after admission\n")
        self.assertEqual(build_folders.build_folder_state(tree), "held")
        memory_requests = [request for request in self.requests if isinstance(request, wire.AdmitRequest)
            and request.profile.step == "gate"]
        self.assertGreaterEqual(len(memory_requests), 2)
        self.assertEqual({request.run for request in memory_requests},
            {wire.RunIdentity(process.pid, first["run"].split(":", 1)[1])})
        release_times = [observed for request, observed in self.request_times if isinstance(request, wire.ReleaseRequest)]
        self.assertTrue(release_times)
        print(f"slot hand-off to the first step: {first['at'] - release_times[-1]:.3f} s")
        self.waiting.clear()
        with self.queue.loop_turn():
            self.queue.free(0)
        with mock.patch.object(self.queue.clock, "advance", return_value=None):
            self.finish_step(process)
            second = self.next_step(process)
        self.assertEqual((second["phase"], second["cwd"], second["run"]), (1, first["cwd"], first["run"]))
        self.assertFalse(self.waiting.is_set(), "the next step entered the memory queue again")
        self.assertEqual(build_folders.build_folder_state(tree), "held")
        self.finish(process)
        self.assertEqual(build_folders.build_folder_state(tree), "free")
        self.queue.release(occupied)

    def test_leaving_before_admission_returns_without_a_slot(self) -> None:
        leave = self.root / "leave"
        self.queue.hold_starts()
        with mock.patch.dict(os.environ, {"LINT_QUEUE_LEAVE_HOOK": "test -e " + shlex.quote(str(leave))}):
            process = self.start((sys.executable, str(self.source / "gate.py"), "--tests-only"))
        self.assertTrue(self.waiting.wait(TEST_HANG_GUARD_SECS), "the queue did not report a wait")
        # The client prints this notice after it receives the wait reply.
        assert process.stderr is not None
        descriptor = process.stderr.fileno()
        deadline = time.monotonic() + TEST_HANG_GUARD_SECS
        notice = bytearray()
        while b"waiting for memory since " not in notice:
            remaining = deadline - time.monotonic()
            self.assertTrue(remaining > 0 and select.select([descriptor], [], [], remaining)[0],
                notice.decode(errors="replace"))
            chunk = os.read(descriptor, 4096)
            self.assertTrue(chunk, notice.decode(errors="replace"))
            notice.extend(chunk)
        leave.touch()
        output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
        errors = notice.decode(errors="replace") + errors
        self.assertEqual(process.returncode, 75, output + errors)
        self.assertFalse(self.slot_root.exists())
        self.assertFalse(list(self.slot_root.glob("*/*/*.lease.json")))
        with self.queue.loop_turn():
            self.assertFalse(self.queue.server.waiters)
            self.assertFalse(self.queue.caller_reservations())

    def test_a_busy_copy_keeps_the_admitted_episode_and_since(self) -> None:
        for since_age in (0.0, 120.0):
            with self.subTest(since_age=since_age):
                self.busy_copy_race(since_age)

    def busy_copy_race(self, since_age: float) -> None:
        self.queue.allow_next_start()
        request_offset = len(self.requests)
        granted, deliver = threading.Event(), threading.Event()
        self.addCleanup(deliver.set)
        observe_reply = self.queue.server.reply_filter

        def pause_grant(reply: wire.Reply) -> wire.Reply:
            if isinstance(reply, wire.Admitted) and not granted.is_set():
                granted.set()
                if not deliver.wait(TEST_HANG_GUARD_SECS):
                    raise AssertionError("the test did not release the admission reply")
            return observe_reply(reply)

        with mock.patch.object(self.queue.server, "reply_filter", side_effect=pause_grant):
            started_at = time.time()
            process = self.start((sys.executable, str(self.source / "gate.py"), "--tests-only"), since_age=since_age)
            self.assertTrue(granted.wait(TEST_HANG_GUARD_SECS))
            with build_slots.take_copy(self.source, "HEAD", showrunner="builds", unit="slot-order",
                    work="race", purpose="competing run") as peer:
                self.assertEqual(peer.path, self.slot_root / "origin/slot-order/1")
                deliver.set()
                started = self.next_step(process)
                copy = Path(started["cwd"])
                self.assertEqual(copy, self.slot_root / "origin/slot-order/2")
                self.assertEqual(build_folders.build_folder_state(peer.path), "held")
                self.assertEqual(build_folders.build_folder_state(copy), "held")
                self.finish(process)

            requests = self.requests[request_offset:]
            memory = [request for request in requests if isinstance(request, wire.AdmitRequest)]
            self.assertGreaterEqual(len(memory), 2)
            first = memory[0]
            self.assertEqual(first.owner.pid, process.pid)
            self.assertEqual({request.run for request in memory}, {first.run})
            preparation = [request for request in memory if request.episode == first.episode]
            self.assertEqual({request.since for request in preparation}, {first.since})
            self.assertFalse(any(isinstance(request, wire.RepositorySlotRequest) for request in requests))
            if since_age:
                self.assertLess(first.since, started_at - since_age / 2)

    def test_verify_waits_for_memory_while_the_gate_keeps_its_copy(self) -> None:
        tools = self.verify_source()
        environment = self.verify_run_environment(tools)
        started, release = self.pause_cargo_check(tools)
        gate = self.root / "gate.py"
        _ = gate.write_text(GATE_COMMAND)
        verify = str(CHECKOUT / "scripts/direct/verify.sh")
        with locked_environment({**os.environ, **environment}, self.queue.loop_access):
            gate_process = self.start((sys.executable, str(gate), "--tests-only"))
            gate_copy = Path(self.next_step(gate_process)["cwd"])
            self.assertEqual(gate_copy, self.slot_root / "verify/slot-order/1")
            self.queue.hold_starts()
            self.waiting.clear()
            step = self.start(("bash", verify, "check", "sample", "--local", "fixture"))
            self.assertTrue(self.waiting.wait(TEST_HANG_GUARD_SECS))
            request = next(request for request in self.requests if isinstance(request, wire.PlacementRequest))
            self.assertEqual(list(gate_copy.parent.glob("*.lease.json")), [build_slots.lease_path(gate_copy)])
            self.assertEqual(build_folders.build_folder_state(gate_copy), "held")
            with self.queue.loop_turn():
                self.assertNotIn(request.episode, self.queue.caller_reservations())
            write_pressure_reading(self.queue.root / "pressure")
            self.queue.server.wake_loop()
            cargo = wait_for_pid(started, processes=(step,))
            checkout = (Path("/proc") / str(cargo) / "cwd").resolve(strict=True)
            self.assertEqual(checkout, gate_copy.with_name("2"))
            self.assertEqual(build_folders.build_folder_state(checkout), "held")
            self.assertEqual(build_folders.build_folder_state(gate_copy), "held")
            self.assertEqual(request.constraints.source_key,
                             execution_identity.SourceKeyKnown(treekey.tree_key(str(checkout)) or ""))
            release.touch()
            self.finish(step)
            self.finish(gate_process)
        self.assertEqual(build_folders.build_folder_state(checkout), "free")
        self.assertEqual(build_folders.build_folder_state(gate_copy), "free")

    def test_other_commands_use_a_numbered_copy_without_a_queue_request(self) -> None:
        self.queue.hold_starts()
        program = "import json,os,sys; print(json.dumps({'phase':0,'cwd':os.getcwd(),'run':os.environ.get('BUILDLOG_RUN','')}),flush=True); sys.stdin.read(1)"
        process = self.start((sys.executable, "-c", program))
        started = self.next_step(process)
        checkout = Path(started["cwd"])
        self.assertEqual(checkout, self.slot_root / "origin/slot-order/1")
        self.assertEqual(build_folders.build_folder_state(checkout), "held")
        self.assertEqual(self.requests, [])
        self.finish(process)
        self.assertEqual(build_folders.build_folder_state(checkout), "free")
        self.assertEqual(self.requests, [])

    def test_snapshot_command_inherits_the_runner_session(self) -> None:
        keys = ("CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID")
        program = "import json, os\n" + f"print(json.dumps({{key: os.getenv(key, '') for key in {keys!r}}}))"
        process = self.start((sys.executable, "-c", program))
        output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(process.returncode, 0, output + errors)
        self.assertEqual(output.strip(), json.dumps({key: self.build_sessions[key] for key in keys}))

    def verify_source(self) -> VerifyMacOffloadFixture:
        tools = VerifyMacOffloadFixture()
        tools.root = self.root / "verify"
        tools.root.mkdir()
        tools.build_fixture()
        tools.environment.update(self.build_sessions)
        real_git = shutil.which("git")
        assert real_git is not None
        shutil.rmtree(tools.root / ".git")
        (tools.bin_directory / "git").unlink()
        (tools.bin_directory / "git").symlink_to(real_git)
        _ = (tools.root / ".gitignore").write_text("/*\n!/Cargo.toml\n!/src/\n")
        _ = (tools.root / "src/main.rs").write_text("fn main() {}\n")
        origin = self.root / "remotes" / "verify.git"
        for arguments in (("init", "-q", "--bare", str(origin)), ("init", "-q", "-b", "main"),
                ("remote", "add", "origin", str(origin)), ("add", "Cargo.toml", "src"),
                ("-c", "user.name=Test", "-c", "user.email=slots@example.invalid", "commit", "-qm", "fixture")):
            _ = subprocess.run([real_git, *arguments], cwd=tools.root, env=tools.environment,
                capture_output=True, check=True, timeout=TEST_HANG_GUARD_SECS)
        tools.environment["TEST_METADATA"] = json.dumps({"workspace_root": str(tools.root),
            "workspace_members": ["sample-id"], "target_directory": str(tools.root / "target"), "packages": [{
                "name": "sample", "id": "sample-id", "manifest_path": str(tools.root / "Cargo.toml"),
                "features": {"default": []}, "dependencies": [], "targets": [
                    {"name": "sample", "kind": ["lib"], "test": True, "src_path": str(tools.root / "src/lib.rs")},
                    {"name": "sample-cli", "kind": ["bin"], "test": False, "src_path": str(tools.root / "src/main.rs")},
                ]}]})
        _ = (tools.root / "lint-config.sh").write_text(
            "lint_config_enabled() { return 0; }\nlint_config_skip_notice() { :; }\n")
        cargo = tools.bin_directory / "cargo"
        _ = cargo.write_text(cargo.read_text().replace('print(os.environ["TEST_METADATA"])',
            'metadata = json.loads(os.environ["TEST_METADATA"])\n'
            + '    metadata["workspace_root"] = str(Path.cwd())\n'
            + '    if \'name = "sample"\' not in Path("Cargo.toml").read_text():\n'
            + '        metadata["workspace_members"] = []; metadata["packages"] = []\n'
            + '    print(json.dumps(metadata))'))
        return tools

    def verify_run_environment(self, tools: VerifyMacOffloadFixture) -> dict[str, str]:
        environment = {**tools.environment, **{key: os.environ[key] for key in (
            "CONF_DIR", "COPIES_CONFIG", "SESSION_TEMP_ROOT", "SESSION_FORMER_ROOT",
            "SCRATCH_ROOT", "SCRATCH_COPIES_ROOT", "SCRATCH_HOLDS", "STAFF_FILE")},
            "ADMISSION_SOCKET": str(self.queue.socket_path), "UNIT_RUN_DIR": "", "UNIT_SESSION_DIR": "",
            "LINT_SKIP_SWEEP": "1"}
        _ = shutil.copy2(tools.root / "build_targets.conf", Path(environment["CONF_DIR"]) / "build_targets.conf")
        _ = shutil.copy2(tools.root / "mac settings.conf", Path(environment["CONF_DIR"]) / "mac settings.conf")
        def provider(key: placement.HostEvidenceKey) -> placement.HostEvidence:
            if key.destination.provider == "local":
                return placement.FreshHostEvidence(key, self.queue.clock.now(), "", placement.AllowedHost())
            return placement.HostEvidenceUnavailable(key, self.queue.clock.now(), "The fixture has no remote provider.")
        _ = self.enterContext(mock.patch.object(self.queue.server, "host_probe", side_effect=provider))
        self.source = tools.root
        return environment

    def test_admission_source_keeps_repository_identity_and_the_real_slot_pool(self) -> None:
        source = self.source
        linked = self.root / "linked-source"
        _ = copies.git(source, "worktree", "add", "--quiet", "--detach", str(linked), "HEAD")
        for origin in ("git@example.invalid:fixture/origin.git", "../origin.git", ""):
            if origin:
                _ = copies.git(source, "remote", "set-url", "origin", origin)
            else:
                _ = copies.git(source, "remote", "remove", "origin")
            for checkout in (source, linked):
                with self.subTest(origin=origin, checkout=checkout):
                    expected = repository.repository_key(str(checkout))
                    canonical = copies.slot_source(checkout)
                    with copies.requested_source(checkout, copies.CommittedSource("HEAD"), copies.NO_MERGE) as snapshot:
                        self.assertEqual(repository.repository_key(str(snapshot)), expected)
                        self.assertEqual(eligibility.repository_identity(snapshot), eligibility.repository_identity(checkout))
                        self.assertEqual(step_estimator.git_identity(["cargo", "check"], str(snapshot))[0],
                            step_estimator.git_identity(["cargo", "check"], str(checkout))[0])
                        self.assertEqual(treekey.tree_key(str(snapshot)), treekey.tree_key(str(checkout)))
                        self.assertEqual(copies.slot_source(snapshot), canonical)
                        if not origin:
                            with self.assertRaisesRegex(build_folders.InvalidFolderForm, "cannot read the origin URL"):
                                with copies.standing(self.owner, snapshot, copies.CommittedSource("HEAD")):
                                    self.fail("A numbered copy needs a Git origin.")
                        else:
                            with copies.standing(self.owner, snapshot, copies.CommittedSource("HEAD")) as tree:
                                self.assertEqual(tree, self.slot_root / "origin/slot-order/1")
                                self.assertEqual(build_slots.source_checkout(tree), canonical)
                    self.assertFalse(copies.ADMISSION_SOURCE_COPIES.get())

    def test_working_changes_leave_no_source_objects_while_waiting_or_after_leaving(self) -> None:
        tools = self.verify_source()
        environment = self.verify_run_environment(tools)
        _ = (tools.root / "src/lib.rs").write_text("pub fn staged() {}\n")
        _ = copies.git(tools.root, "add", "src/lib.rs")
        _ = (tools.root / "src/lib.rs").write_text("pub fn unstaged() {}\n")
        _ = (tools.root / "src/new.rs").write_text("pub fn new() {}\n")
        (tools.root / "src/main.rs").unlink()
        before = copies.git(tools.root, "count-objects", "-v")
        index = (tools.root / ".git/index").read_bytes()
        leave = self.root / "leave"
        _ = write_config_fixture(self.queue.root / "build_admission.conf", "running_pass_seconds=0.05\n")
        self.queue.hold_starts()
        with mock.patch.dict(os.environ, {**environment, "LINT_QUEUE_LEAVE_HOOK": "test -e " + shlex.quote(str(leave))}):
            process = self.start(("bash", str(CHECKOUT / "scripts/direct/verify.sh"), "check", "sample"),
                "--apply-uncommitted", str(tools.root))
        self.assertTrue(self.waiting.wait(TEST_HANG_GUARD_SECS))
        self.assertEqual(copies.git(tools.root, "count-objects", "-v"), before)
        request = next(request for request in self.requests if isinstance(request, wire.PlacementRequest))
        snapshot = Path(request.constraints.repository)
        self.assertEqual((snapshot / "src/lib.rs").read_text(), "pub fn unstaged() {}\n")
        self.assertEqual((snapshot / "src/new.rs").read_text(), "pub fn new() {}\n")
        self.assertFalse((snapshot / "src/main.rs").exists())
        self.assertFalse(self.slot_root.exists())
        leave.touch()
        output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(process.returncode, 75, output + errors)
        self.assertEqual(copies.git(tools.root, "count-objects", "-v"), before)
        self.assertEqual((tools.root / ".git/index").read_bytes(), index)
        self.assertFalse(snapshot.parent.exists())
        self.assertFalse(self.slot_root.exists())
        self.assertNotIn("GIT_OBJECT_DIRECTORY", os.environ)

    def test_next_allocation_removes_a_stopped_verify_waiters_source_copy(self) -> None:
        tools = self.verify_source()
        environment = self.verify_run_environment(tools)
        self.queue.hold_starts()
        with mock.patch.dict(os.environ, environment):
            process = self.start(("bash", str(CHECKOUT / "scripts/direct/verify.sh"), "check", "sample"))
        self.assertTrue(self.waiting.wait(TEST_HANG_GUARD_SECS))
        request = next(request for request in self.requests if isinstance(request, wire.PlacementRequest))
        snapshot = Path(request.constraints.repository)
        leftover = snapshot.parent
        self.assertTrue(snapshot.is_dir())
        self.assertEqual(build_folders.build_folder_state(leftover), "held")
        process.kill()
        _ = process.communicate(timeout=TEST_HANG_GUARD_SECS)
        with build_folders.tool_folder("copies-source", parent=leftover.parent) as replacement:
            self.assertEqual(replacement, leftover)
            self.assertFalse(snapshot.exists())
        self.assertFalse(leftover.exists())
        self.assertFalse(leftover.with_name(leftover.name + ".lease.json").exists())
        self.assertFalse(leftover.with_name(leftover.name + ".lock").exists())
        self.assertFalse(self.slot_root.exists())

    def test_verify_waiter_leaves_without_a_slot_lease_or_folder(self) -> None:
        tools = self.verify_source()
        environment = self.verify_run_environment(tools)
        _ = write_config_fixture(self.queue.root / "build_admission.conf", "running_pass_seconds=0.05\n")
        self.queue.hold_starts()
        with mock.patch.dict(os.environ, {**environment, "LINT_QUEUE_LEAVE_HOOK": "exit 0"}):
            process = self.start(("bash", str(CHECKOUT / "scripts/direct/verify.sh"), "check", "sample"))
        output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(process.returncode, 75, output + errors)
        self.assertFalse(self.slot_root.exists())
        self.assertFalse(list(self.slot_root.glob("*/*/*.lease.json")))
        self.assertFalse(list((self.root / "session").rglob("Cargo.toml")))
        with self.queue.loop_turn():
            self.assertFalse(self.queue.server.waiters)
            self.assertFalse(self.queue.caller_reservations())
        self.assertTrue(any(isinstance(request, wire.CancelPlacement) for request in self.requests))

    def test_waiting_steps_take_numbered_copies_after_admission(self) -> None:
        tools = self.verify_source()
        environment = self.verify_run_environment(tools)
        caller_count = 7
        self.queue.configure(recovery_start_seconds=1, waiting_pass_seconds=0.05, running_pass_seconds=0.1)
        memory = fake_memory_readings(self.queue.root, available_kib=256 * 1024**2, total_kib=256 * 1024**2)
        hold = Path(environment["BUILD_HOLD_DIR"])
        hold.mkdir(exist_ok=True)
        local_hold = hold / "other"
        mac_ready = self.root / "mac-ready"
        environment.update(memory, UNIT_DETACHED_RUN="1", BUILDLOG_RUN="", TEST_RUNNER_MAC="passed",
                           TEST_RUNNER_STATUS="0", TEST_RUNNER_BUSY_UNTIL=str(mac_ready),
                           COPY_SLOT_TEST_ROOT=str(self.root))
        _ = self.enterContext(locked_environment({**os.environ, **environment}, self.queue.loop_access))
        self.addCleanup(reap, f"COPY_SLOT_TEST_ROOT={self.root}")

        def read_host(key: placement.HostEvidenceKey) -> placement.HostEvidence:
            if key.destination.provider == "local":
                return self.queue.server.read_provider_evidence(key)
            return private_caller_provider(self.queue, environment, key)

        # Keep each Cargo step at the socket until the test releases it.
        endpoint = self.queue.socket_dir / "cargo.sock"
        cargo = tools.bin_directory / "cargo"
        cargo_gate = dedent(f'''
            if arguments[:2] == ["nextest", "run"]:
                from socket import AF_UNIX, SOCK_STREAM, socket
                with socket(AF_UNIX, SOCK_STREAM) as ready:
                    ready.connect({str(endpoint)!r})
                    caller = os.environ["COPY_SLOT_TEST_CALLER"]
                    Path("report.txt").write_text("a report from the slot")
                    ready.sendall(f"{{caller}}\\t{{Path.cwd()}}\\n".encode())
                    ready.recv(1)
            ''').strip()
        _ = cargo.write_text(cargo.read_text().replace('if arguments[:2] == ["nextest", "run"]:', cargo_gate))

        condition = threading.Condition()
        memory_waiters: set[str] = set()
        hold_waiters: set[str] = set()
        mac_waiters: set[str] = set()
        reply_filter = self.queue.server.reply_filter

        def observe_wait(reply: wire.Reply) -> wire.Reply:
            if isinstance(reply, wire.PlacementWait):
                with condition:
                    if isinstance(reply.wait, wire.Wait):
                        memory_waiters.add(reply.episode)
                    elif isinstance(reply.wait, wire.PlacementResourceWait):
                        if reply.wait.resource == "natedev":
                            hold_waiters.add(reply.episode)
                        elif reply.wait.resource == "mac":
                            mac_waiters.add(reply.episode)
                    condition.notify_all()
            return reply_filter(reply)

        def wait_for_callers(callers: set[str], count: int) -> None:
            with condition:
                ready = condition.wait_for(lambda: len(callers) >= count, timeout=60)
            if not ready:
                self.fail(f"Only {len(callers)} of {count} callers entered the wait. " + caller_diagnostics())

        verify = str(CHECKOUT / "scripts/direct/verify.sh")
        capacity_caller = self.root / "capacity-caller.py"
        _ = capacity_caller.write_text(CAPACITY_CALLER)
        source_key = treekey.committed_tree_key(copies.git(tools.root, "rev-parse", "HEAD^{tree}").strip())

        def caller_diagnostics() -> str:
            statuses = [process.poll() for process in self.processes]
            errors = {}
            for number, process in enumerate(self.processes):
                if statuses[number] is not None:
                    output, errors[number] = process.communicate(timeout=TEST_HANG_GUARD_SECS)
                    errors[number] += output
            return f"Callers: {statuses}. Output: {errors}. Waits: {memory_waiters}, {hold_waiters}, {mac_waiters}."

        def start_caller(variant: str) -> subprocess.Popen[str]:
            caller = len(self.processes)
            board = self.root / f"board-{caller}"
            temporary = self.root / f"caller-{caller}-tmp"
            temporary.mkdir()
            capacity_command = ("env", f"PYTHONPATH={CHECKOUT}", sys.executable, str(capacity_caller), str(endpoint), str(caller),
                                source_key, str(tools.root))
            caller_environment = {**os.environ, "COPY_SLOT_TEST_CALLER": str(caller), "UNIT_RUN_DIR": str(board),
                                  "VERIFY_SCRIPT": verify, "TMPDIR": str(temporary)}
            if variant == "capacity":
                process = subprocess.Popen(capacity_command,
                    cwd=tools.root, env=caller_environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                self.processes.append(process)
                return process
            if variant == "mac":
                remote = 'bash "$VERIFY_SCRIPT" test sample --mac fixture --no-cache'
                local = 'exec bash "$VERIFY_SCRIPT" test sample --local fixture --no-cache'
                command = ("bash", "-c", remote + " && " + local)
                options = ()
            else:
                command = ("bash", verify, "test", "sample", "--local", "fixture", "--no-cache")
                options = ()
                if variant == "env":
                    command = ("env", "MODE=ci", *command)
                    options = ("--publish", "report.txt")
            with locked_environment(caller_environment, self.queue.loop_access):
                return self.start(command, *options)

        def held_folders() -> set[Path]:
            return {path for path in self.slot_root.glob("*/*/[0-9]*")
                    if path.name.isdecimal() and path.is_dir() and build_folders.build_folder_state(path) == "held"}

        with (mock.patch.object(self.queue.server, "host_probe", side_effect=read_host),
              mock.patch.object(self.queue.server, "reply_filter", side_effect=observe_wait),
              socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener, ExitStack() as steps):
            listener.bind(str(endpoint))
            listener.listen()
            listener.settimeout(60)

            def next_running_step() -> tuple[Path, tuple[socket.socket, subprocess.Popen[str]]]:
                try:
                    connection = steps.enter_context(listener.accept()[0])
                except TimeoutError:
                    self.fail("A caller did not reach Cargo. " + caller_diagnostics())
                connection.settimeout(60)
                with connection.makefile("r") as response:
                    caller, folder = response.readline().rstrip("\n").split("\t", 1)
                return Path(folder), (connection, self.processes[int(caller)])

            first_board = self.root / "board-0"
            first_waiters = first_board / "token_waiters"
            first_waiters.mkdir(parents=True)
            board_helper = str(CHECKOUT / "scripts/direct/board.sh")
            peer = [str(first_board), "peer", "cargo", "--pid", str(os.getpid())]
            _ = subprocess.run(["bash", board_helper, "acquire", *peer], env=environment,
                capture_output=True, check=True, timeout=TEST_HANG_GUARD_SECS)
            self.queue.hold_starts()
            env_caller = start_caller("env")
            try:
                wait_for_callers(memory_waiters, 1)
                env_folders = held_folders()
                self.assertEqual(len(env_folders), 1, "the env wrapper needs its copy before its memory wait")
                write_pressure_reading(self.queue.root / "pressure")
                self.queue.server.wake_loop()
                wait_until(lambda: any(first_waiters.iterdir()), first_waiters, processes=(env_caller,))
                with self.subTest(wait="Cargo token"):
                    self.assertEqual(held_folders(), env_folders)
                with condition:
                    memory_waiters.clear()
                self.queue.hold_starts()
                verify_caller = start_caller("verify")
                for _ in range(2):
                    _ = start_caller("capacity")
                wait_for_callers(memory_waiters, 3)
                with self.subTest(wait="memory"):
                    self.assertEqual(held_folders(), env_folders, "the plain Verify caller holds a copy during its wait")
                _ = local_hold.write_text(json.dumps({"holder": "other", "for": "fixture",
                    "since": "2026-11-18T00:00:00Z", "release_eta": "unknown"}))
                _ = start_caller("mac")
                wait_for_callers(mac_waiters, 1)
                with self.queue.loop_turn():
                    mac_request = next(request for request in self.requests
                        if isinstance(request, wire.PlacementRequest) and request.episode in mac_waiters)
                wrapper_checkout = Path(mac_request.constraints.repository)
                wrapper_folders = env_folders | {wrapper_checkout}
                with self.subTest(wait="Mac", wrapper="bash -c"):
                    self.assertEqual(len(wrapper_folders), 2)
                    self.assertEqual(held_folders(), wrapper_folders)
                    self.assertEqual(build_slots.source_checkout(wrapper_checkout), tools.root)
                for _ in range(2):
                    _ = start_caller("capacity")
                mac_ready.touch()
                self.queue.server.wake_loop()
                _ = subprocess.run(["bash", board_helper, "release", *peer], env=environment,
                    capture_output=True, check=True, timeout=TEST_HANG_GUARD_SECS)
                wait_for_callers(hold_waiters, caller_count)
                with self.queue.loop_turn():
                    waiting_owners = {self.queue.server.placement_requests[episode].owner.pid
                                      for episode in hold_waiters}
                    self.assertEqual(len(waiting_owners), caller_count)
                    self.assertTrue(hold_waiters.isdisjoint(self.queue.server.current_assignments))
                self.assertEqual(held_folders(), wrapper_folders)
            finally:
                _ = subprocess.run(["bash", board_helper, "release", *peer], env=environment,
                    capture_output=True, check=True, timeout=TEST_HANG_GUARD_SECS)
                local_hold.unlink(missing_ok=True)

            write_pressure_reading(self.queue.root / "pressure")
            self.queue.server.wake_loop()
            self.assertEqual(len(self.processes), caller_count)
            running = dict(next_running_step() for _ in range(caller_count))
            self.assertEqual(len(running), caller_count)
            self.assertEqual(set(running), held_folders())
            self.assertIn(wrapper_checkout, running)
            self.assertEqual({folder for folder, (_, process) in running.items() if process is env_caller}, env_folders)
            verify_checkout, = (folder for folder, (_, process) in running.items() if process is verify_caller)
            self.assertNotIn(verify_checkout, wrapper_folders)
            self.assertEqual(build_slots.source_checkout(verify_checkout), tools.root)
            self.assertFalse(any(isinstance(request, wire.RepositorySlotRequest) for request in self.requests))

            def finish_caller(process: subprocess.Popen[str]) -> None:
                output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
                self.assertEqual(process.returncode, 0, output + errors)
                if process is env_caller:
                    self.assertIn("published: ", output)
                    self.assertNotIn("report.txt not built", errors)

            folder, (connection, process) = running.popitem()
            connection.sendall(b"x")
            finish_caller(process)
            self.assertEqual(build_folders.build_folder_state(folder), "free")
            self.assertEqual(set(running), held_folders())
            for connection, _process in running.values():
                connection.sendall(b"x")
            for _connection, process in running.values():
                finish_caller(process)
            self.assertTrue(tools.runner_events(), "The Mac runner did not run.")
            self.assertFalse(held_folders())

    def test_cargo_keeps_the_folder_after_its_launcher_dies(self) -> None:
        tools = self.verify_source()
        environment = self.verify_run_environment(tools)
        marker = f"COPY_SLOT_TEST_ROOT={self.root}"
        environment["COPY_SLOT_TEST_ROOT"] = str(self.root)
        self.addCleanup(reap, marker)
        endpoint = self.queue.socket_dir / "cargo-lock.sock"
        cargo = tools.bin_directory / "cargo"
        pause = dedent(fr'''
            if arguments[:2] == ["nextest", "run"]:
                from socket import AF_UNIX, SOCK_STREAM, socket
                launcher = os.getppid()
                if os.environ["TEST_COPY_STOP"] == "copies TERM":
                    child = os.fork()
                    if child:
                        os.waitpid(child, 0)
                        raise SystemExit(0)
                    os.setsid()
                with socket(AF_UNIX, SOCK_STREAM) as ready:
                    ready.connect({str(endpoint)!r})
                    ready.sendall(f"{{launcher}}\t{{os.getpid()}}\t{{Path.cwd()}}\n".encode())
                    ready.recv(1)
            ''').strip()
        _ = cargo.write_text(cargo.read_text().replace('if arguments[:2] == ["nextest", "run"]:', pause))
        tree = copies.git(tools.root, "rev-parse", "HEAD^{tree}").decode().strip()
        verify_command = ("bash", str(CHECKOUT / "scripts/direct/verify.sh"), "test", "sample", "--local", "fixture")
        with (mock.patch.dict(os.environ, environment),
              socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener):
            listener.bind(str(endpoint))
            listener.listen(1)
            listener.settimeout(TEST_HANG_GUARD_SECS)
            for stop in ("launcher SIGKILL", "copies TERM"):
                with self.subTest(stop=stop):
                    first_request = len(self.requests)
                    with mock.patch.dict(os.environ, {"TEST_COPY_STOP": stop}):
                        process = self.start(verify_command)
                    with listener.accept()[0] as connection:
                        connection.settimeout(TEST_HANG_GUARD_SECS)
                        with connection.makefile("r") as data:
                            launcher, cargo_pid, folder = data.readline().rstrip("\n").split("\t", 2)
                        pid, cargo = int(launcher), int(cargo_pid)
                        cargo_exit = os.pidfd_open(cargo)
                        self.addCleanup(os.close, cargo_exit)
                        launcher_exit = os.pidfd_open(pid)
                        self.addCleanup(os.close, launcher_exit)
                        checkout = Path(folder)
                        self.assertEqual(build_folders.build_folder_state(checkout), "held")
                        if stop == "launcher SIGKILL":
                            request = next(request for request in self.requests[first_request:]
                                           if isinstance(request, wire.PlacementRequest))
                            arguments = (Path("/proc") / launcher / "cmdline").read_text().split("\0")
                            hold = arguments.index("hold")
                            self.assertEqual(Path(arguments[hold - 1]).name, "copies.py")
                            self.assertEqual(arguments[hold:hold + 8], [
                                "hold", "--repo", request.constraints.repository, "--tree", tree,
                                "--step", "test", "--",
                            ])
                            lease = cast(dict[str, object], json.loads(build_slots.lease_path(checkout).read_text()))
                            self.assertEqual(lease["pid"], pid)
                            os.kill(pid, signal.SIGKILL)
                            self.assertTrue(select.select([launcher_exit], [], [], 30)[0])
                            self.assertEqual(build_folders.build_folder_state(checkout), "held")
                            connection.sendall(b"x")
                        else:
                            process.terminate()
                            self.assertTrue(select.select([launcher_exit], [], [], 30)[0],
                                            "copies did not stop the compiling step")
                            self.assertFalse(select.select([cargo_exit], [], [], 0)[0])
                            self.assertEqual(build_folders.build_folder_state(checkout), "held")
                            connection.sendall(b"x")
                    output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
                    expected = 137 if stop == "launcher SIGKILL" else -signal.SIGTERM
                    self.assertEqual(process.returncode, expected, output + errors)
                    self.assertTrue(select.select([cargo_exit], [], [], TEST_HANG_GUARD_SECS)[0])
                    self.assertEqual(build_folders.build_folder_state(checkout), "free")

    def pause_cargo_check(self, tools: VerifyMacOffloadFixture) -> tuple[Path, Path]:
        started, release = self.root / "check-pid", self.root / "check-release"
        marker = f"SLOT_ORDER_CHECK={started}"
        self.addCleanup(reap, marker)
        cargo = tools.bin_directory / "cargo"
        pause = dedent(f'''
            if arguments[:1] == ["check"]:
                sys.path.insert(0, {str(CHECKOUT)!r})
                from scripts.direct.wait_for_test_state import wait_for_file
                Path({str(started)!r}).write_text(str(os.getpid()))
                wait_for_file(Path({str(release)!r}))
            ''')
        heading = 'if arguments[:1] == ["metadata"]:'
        _ = cargo.write_text(cargo.read_text().replace(heading, pause + heading))
        return started, release

    def test_direct_verify_waits_in_a_snapshot_and_builds_in_a_numbered_copy(self) -> None:
        tools = self.verify_source()
        environment = self.verify_run_environment(tools)
        started, release = self.pause_cargo_check(tools)
        self.queue.hold_starts()
        with mock.patch.dict(os.environ, {**environment, "SLOT_ORDER_CHECK": str(started)}):
            process = self.start(("bash", str(CHECKOUT / "scripts/direct/verify.sh"), "check", "sample"))
        self.assertTrue(self.waiting.wait(TEST_HANG_GUARD_SECS))
        request = next(request for request in self.requests if isinstance(request, wire.PlacementRequest))
        snapshot = Path(request.constraints.repository)
        self.assertEqual(snapshot.name, "source")
        self.assertEqual(build_folders.build_folder_state(snapshot.parent), "held")
        self.assertFalse(self.slot_root.exists())

        write_pressure_reading(self.queue.root / "pressure")
        self.queue.server.wake_loop()
        cargo = wait_for_pid(started, processes=(process,))
        checkout = (Path("/proc") / str(cargo) / "cwd").resolve(strict=True)
        self.assertEqual(checkout, self.slot_root / "verify/slot-order/1")
        self.assertEqual(build_folders.build_folder_state(checkout), "held")
        release.touch()
        self.finish(process)
        self.assertEqual(build_folders.build_folder_state(checkout), "free")
        self.assertFalse(snapshot.exists())

    def test_shell_wrapped_verify_doc_publishes_from_its_only_numbered_copy(self) -> None:
        tools = self.verify_source()
        environment = self.verify_run_environment(tools)
        cargo = tools.bin_directory / "cargo"
        stub = dedent(r"""
            if arguments[:1] == ["doc"]:
                target = Path(os.environ["CARGO_TARGET_DIR"])
                assert target == Path.cwd() / "target"
                assert len(list(Path.cwd().parent.glob("*.lease.json"))) == 1
                output = target / "doc/sample/index.html"
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(f"doc from {Path.cwd()}\n")
                raise SystemExit(0)
            """).lstrip("\n")
        heading = 'if arguments[:2] == ["nextest", "--version"]:'
        program = cargo.read_text()
        self.assertIn(heading, program)
        _ = cargo.write_text(program.replace(heading, stub + heading, 1))

        verify = str(CHECKOUT / "scripts/direct/verify.sh")
        asset = "target/doc/sample/index.html"
        with mock.patch.dict(os.environ, environment):
            process = self.start(("bash", "-c", 'exec bash "$1" doc sample --local fixture', "verify-doc", verify),
                                 "--publish", asset)
        output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(process.returncode, 0, output + errors)
        published = [line.removeprefix("published: ").rsplit(" ", 1)[0]
                     for line in output.splitlines() if line.startswith("published: ")]
        self.assertEqual(len(published), 1, output + errors)
        checkout = self.slot_root / "verify/slot-order/1"
        self.assertEqual(Path(published[0]).read_text(), f"doc from {checkout}\n")
        self.assertEqual((checkout / asset).read_bytes(), Path(published[0]).read_bytes())
        self.assertEqual(list(self.slot_root.glob("*/*/*.lease.json")), [build_slots.lease_path(checkout)])
        self.assertEqual(build_folders.build_folder_state(checkout), "free")

    def test_shell_wrapped_verify_doc_refuses_edits_outside_the_index(self) -> None:
        tools = self.verify_source()
        environment = self.verify_run_environment(tools)
        environment["UNIT_DETACHED_RUN"] = "1"
        verify = str(CHECKOUT / "scripts/direct/verify.sh")
        command = 'printf "edited\\n" > "$2" && exec bash "$1" doc sample --local fixture'
        for edited in ("src/lib.rs", "src/untracked.rs"):
            with self.subTest(edited=edited), mock.patch.dict(os.environ, environment):
                process = self.start(("bash", "-c", command, "verify-doc", verify, edited))
                output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
                self.assertEqual(process.returncode, 2, output + errors)
                self.assertEqual(errors.splitlines(), [
                    "verify.sh: the prepared copy has edits outside the index. Stage or commit them first.",
                ])
                self.assertFalse(tools.cargo_events())
                self.assertFalse(self.requests)

    def test_shell_wrapped_verify_doc_builds_after_apply_uncommitted(self) -> None:
        tools = self.verify_source()
        environment = self.verify_run_environment(tools)
        environment["UNIT_DETACHED_RUN"] = "1"
        applied = "pub fn applied() {}\n"
        _ = (tools.root / "src/lib.rs").write_text(applied)
        verify = str(CHECKOUT / "scripts/direct/verify.sh")
        command = 'touch target/ignored && exec bash "$1" doc sample --local fixture'
        with mock.patch.dict(os.environ, environment):
            process = self.start(("bash", "-c", command, "verify-doc", verify),
                                 "--apply-uncommitted", str(tools.root))
        self.finish(process)

        checkout = self.slot_root / "verify/slot-order/1"
        self.assertEqual(copies.git(checkout, "show", ":src/lib.rs").decode(), applied)
        self.assertTrue((checkout / "target/ignored").is_file())
        self.assertEqual(sum(event["args"][:1] == ["doc"] for event in tools.cargo_events()), 1)
        self.assertEqual(list(self.slot_root.glob("*/*/*.lease.json")), [build_slots.lease_path(checkout)])

    def test_shell_wrapped_verify_doc_refuses_an_assume_unchanged_edit(self) -> None:
        tools = self.verify_source()
        environment = self.verify_run_environment(tools)
        environment["UNIT_DETACHED_RUN"] = "1"
        verify = str(CHECKOUT / "scripts/direct/verify.sh")
        command = ('git update-index --assume-unchanged -- src/lib.rs && '
                   + 'printf "edited\\n" > src/lib.rs && exec bash "$1" doc sample --local fixture')
        with mock.patch.dict(os.environ, environment):
            process = self.start(("bash", "-c", command, "verify-doc", verify))
        output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(process.returncode, 2, output + errors)
        self.assertEqual(errors.splitlines(), [
            "verify.sh: the prepared copy has edits outside the index. Stage or commit them first.",
        ])
        self.assertFalse(tools.cargo_events())
        self.assertFalse(self.requests)

    def test_shell_wrapped_verify_doc_records_no_pass_if_cargo_edits_the_copy(self) -> None:
        tools = self.verify_source()
        environment = self.verify_run_environment(tools)
        environment["UNIT_DETACHED_RUN"] = "1"
        cargo = tools.bin_directory / "cargo"
        heading = 'if arguments[:2] == ["nextest", "--version"]:'
        program = cargo.read_text()
        self.assertIn(heading, program)
        edit = dedent("""
            if arguments[:1] == ["doc"]:
                Path("src/lib.rs").write_text("pub fn edited_during_doc() {}\\n")
                raise SystemExit(0)
            """).lstrip("\n")
        _ = cargo.write_text(program.replace(heading, edit + heading, 1))

        verify = str(CHECKOUT / "scripts/direct/verify.sh")
        with mock.patch.dict(os.environ, environment):
            process = self.start(("bash", "-c", 'exec bash "$1" doc sample --local fixture', "verify-doc", verify))
        output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(process.returncode, 77, output + errors)
        self.assertIn("verify.sh: the prepared copy changed during the build. No doc pass was recorded.",
                      errors.splitlines())
        self.assertEqual(sum(event["args"][:1] == ["doc"] for event in tools.cargo_events()), 1)
        records = Path(environment["HOME"]) / ".local/state/verify/doc_pass"
        self.assertFalse(list(records.glob("*.json")))

    def test_older_commit_admits_a_package_absent_from_the_current_checkout(self) -> None:
        tools = self.verify_source()
        environment = self.verify_run_environment(tools)
        started, release = self.pause_cargo_check(tools)
        environment["SLOT_ORDER_CHECK"] = str(started)
        _ = copies.git(tools.root, "tag", "with-package")
        _ = (tools.root / "Cargo.toml").write_text('[workspace]\nmembers = []\n')
        _ = copies.git(tools.root, "add", "Cargo.toml")
        _ = copies.git(tools.root, "commit", "-qm", "Remove the package")
        with mock.patch.dict(os.environ, environment):
            process = self.start(("bash", str(CHECKOUT / "scripts/direct/verify.sh"), "check", "sample"),
                "--commit", "with-package")
        cargo = wait_for_pid(started, processes=(process,))
        checkout = (Path("/proc") / str(cargo) / "cwd").resolve(strict=True)
        self.assertIn('name = "sample"', (checkout / "Cargo.toml").read_text())
        requests = [request for request in self.requests if isinstance(request, wire.PlacementRequest)]
        self.assertTrue(requests)
        self.assertEqual(requests[0].constraints.package, "sample")
        release.touch()
        self.finish(process)

    def test_pinned_gpu_dispatch_uses_the_admitted_checkout(self) -> None:
        functions = shell_functions(CHECKOUT / "scripts/direct/verify.sh", "route_test", "try_reserved_target")
        assignment = self.root / "assignment"
        _ = assignment.write_text("fixture assignment\n")
        script = functions + dedent('''\
            set -eu
            PLACEMENT_ASSIGNMENT_FILE=$1
            MAC_RUNNER=$2
            CMD=test PINNED_TARGET=hana-gpu DOC_REPO_TOP=/caller
            VERIFY_SLOT_SOURCE=/snapshot VERIFY_SLOT_TREE=admitted
            COPIES_STEP_REPOSITORY=/caller
            STEP_TARGET_TABLE='' MAC_ONLY=0 MAC_PASSED=0
            PLACEMENT_COMMAND=(cargo nextest run)
            release_token() { :; }
            admit_verify_run() { :; }
            check_routing_hold() { :; }
            collect_routing_wait() { :; }
            placement_once() {
                PLACEMENT_PROVIDER=ssh STEP_ASSIGNED_PLATFORM=linux
                STEP_ASSIGNED_TARGET=hana-gpu STEP_ASSIGNED_SETTINGS=gpu.conf
            }
            doc_checkout() {
                [[ "$*" == '/snapshot admitted /caller' ]] || exit 3
                MAC_SOURCE_ROOT=/admitted-checkout
                printf 'open\\n'
            }
            verify_helper() { exit 4; }
            require_nextest() { exit 5; }
            try_mac_test() {
                [[ "$1" == gpu.conf && "$STEP_REMOTE_REPO_ROOT" == /admitted-checkout ]] || exit 6
                MAC_PASSED=1
                printf 'dispatch\\n'
            }
            step_close_checkout() { printf 'close\\n'; }
            route_test
            ''')
        result = subprocess.run(("bash", "-c", script, "verify", str(assignment), str(self.root / "missing-runner")),
            capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.splitlines(), ["open", "dispatch", "close"])

    def assert_ssh_source(self, source: Path, caller: Path, revision: str, scenario: str) -> None:
        with copies.requested_source(source, copies.WorktreeSource("HEAD", str(source)),
                                     copies.MergeRevision(revision)) as snapshot:
            self.assertEqual((snapshot / "src/lib.rs").read_text(), "pub fn changed() {}\n")
            self.assertEqual((snapshot / "src/merged.rs").read_text(), "pub fn merged() {}\n")
            self.assertFalse((source / "src/merged.rs").exists())
            if scenario != "clean-linux":
                _ = (snapshot / "src/main.rs").write_text("fn main() { let _ = 1; }\n")
            tree = copies.staged_tree(snapshot, include_worktree=True)
            trust = copies.SshSourceTrust(offload.trusted_linux_source, nix_env.trusted_root, nix_env.registered_worktree)
            trusted = scenario in ("clean-linux", "trusted-linux", "trusted-envrc")
            self.assertEqual(offload.trusted_linux_source(caller), trusted or scenario == "mismatched-linux")
            with copies.remote_step_source(snapshot, tree, caller, trust) as checkout:
                self.assertEqual(checkout.parent, copy_record.root().resolve())
                record = copy_record.read_record(checkout / copy_record.RECORD)
                self.assertIsInstance(record, copy_record.Registered)
                assert isinstance(record, copy_record.Registered)
                self.assertEqual("generated_source" in record.fields, trusted)
                self.assertEqual(offload.trusted_linux_source(checkout), trusted)
                if scenario in ("trusted-envrc", "untrusted-envrc"):
                    self.assertIsNotNone(copy_record.record_path(checkout))
                    self.assertEqual(record.fields.get("source_commit"), copies.commit_at(source, "HEAD") if trusted else None)
                self.assertEqual((checkout / "src/lib.rs").read_text(), "pub fn changed() {}\n")
                self.assertEqual((checkout / "src/merged.rs").read_text(), "pub fn merged() {}\n")
                self.assertEqual(treekey.tree_key(str(checkout)), treekey.committed_tree_key(tree.encode("ascii")))
            self.assertFalse(self.slot_root.exists())

    def test_remote_grant_source_key_matches_the_prepared_slot(self) -> None:
        tools = self.verify_source()
        environment = self.verify_run_environment(tools)
        (self.root / ".claude").symlink_to(tools.home / ".claude", target_is_directory=True)
        remote_tmp = self.root / "remote-tmp"
        remote_tmp.mkdir()
        config = Path(environment["CONF_DIR"])
        _ = write_config_fixture(config / "linux.conf", "offload=on\nhost=fake-linux\nslots=1\nkinds=check,doc\n",
                                 template="mac_test")
        targets = config / "build_targets.conf"
        _ = targets.write_text(targets.read_text().replace("targets=mac,natedev", "targets=mac,peer,natedev")
            + "kind.peer=ssh\nplatform.peer=linux\nsettings.peer=linux.conf\n")
        _ = (tools.root / "src/merged.rs").write_text("pub fn merged() {}\n")
        _ = copies.git(tools.root, "add", "src/merged.rs")
        _ = copies.git(tools.root, "commit", "-qm", "Add the merge source")
        revision = copies.commit_at(tools.root, "HEAD")
        _ = copies.git(tools.root, "checkout", "--detach", "HEAD^")
        _ = (tools.root / "src/lib.rs").write_text("pub fn changed() {}\n")
        started, release = self.pause_cargo_check(tools)
        parser_runner = self.root / "parse-remote.py"
        verify = CHECKOUT / "scripts/direct/verify.sh"
        tools.write_executable(parser_runner, dedent(f'''
            import json
            import os
            from pathlib import Path
            import runpy
            import sys
            from unittest.mock import patch

            sys.path.insert(0, {str(CHECKOUT)!r})
            from scripts.buildlog import treekey
            from scripts.direct.wait_for_test_state import wait_for_file
            from scripts.lib import copy_record
            from scripts.mac_test import offload

            class ParsedRequest(Exception):
                pass

            original = offload.request_from_arguments
            observed = Path(os.environ["TEST_REMOTE_SOURCE"])

            def inspect(arguments):
                request = original(arguments)
                root = request.source_root
                row = [str(Path.cwd()), str(root), treekey.tree_key(str(root)),
                       (root / "src/lib.rs").read_text(), (root / "src/merged.rs").read_text(),
                       (root / "src/main.rs").read_text()]
                if "TEST_REMOTE_ASSIGNMENT" in os.environ:
                    observed.with_suffix(".assignment").write_bytes(Path(arguments.assignment_file).read_bytes())
                if "TEST_TRUST_CALLER" in os.environ:
                    caller = Path(os.environ["TEST_TRUST_CALLER"])
                    repo = request.repo_root
                    accepted = offload.trusted_linux_source(repo)
                    record = copy_record.read_record(repo / copy_record.RECORD)
                    evidence = [
                        offload.trusted_linux_source(caller), accepted,
                        repo.parent == copy_record.root().resolve(),
                        isinstance(record, copy_record.Registered),
                        isinstance(record, copy_record.Registered) and "generated_source" in record.fields,
                    ]
                    observed.with_suffix(".trust").write_text(json.dumps(evidence))
                    if request.verb == "doc":
                        observed.with_suffix(".doc").write_text(json.dumps([
                            str(repo), isinstance(request.source, offload.StagedDocSnapshot),
                            (root / "src/main.rs").read_text(),
                            record.fields.get("source_path") if isinstance(record, copy_record.Registered) else None,
                        ]))
                    if not accepted:
                        os.environ.update(TEST_RUNNER_MAC="declined", TEST_RUNNER_STATUS="75", TEST_RUNNER_REASON="repo")
                temporary = observed.with_suffix(".tmp")
                temporary.write_text(json.dumps(row))
                temporary.replace(observed)
                if "TEST_REMOTE_RELEASE" in os.environ:
                    wait_for_file(Path(os.environ["TEST_REMOTE_RELEASE"]))
                raise ParsedRequest

            with patch.object(offload, "request_from_arguments", side_effect=inspect):
                try:
                    offload.main()
                except ParsedRequest:
                    pass
            runpy.run_path({str(tools.runner)!r}, run_name="__main__")
            '''))
        other = self.root / "other-trusted"
        _ = copies.git(tools.root, "clone", "-q", str(tools.root), str(other))
        tools.write_executable(tools.bin_directory / "direnv", "#!/bin/sh\nexit 1\n")
        for scenario in ("prepared", "clean-linux", "linux-doc", "trusted-linux",
                         "untrusted-linux", "trusted-envrc",
                         "untrusted-envrc", "mismatched-linux", "source-record", "retry"):
            with self.subTest(source=scenario):
                envrc = scenario in ("trusted-envrc", "untrusted-envrc")
                mismatch = scenario == "mismatched-linux"
                source_record = scenario == "source-record"
                doc = scenario == "linux-doc"
                linux = envrc or mismatch or source_record or scenario in (
                    "trusted-linux", "untrusted-linux", "clean-linux", "linux-doc",
                )
                trusted = scenario in ("trusted-linux", "trusted-envrc", "clean-linux", "linux-doc")
                if scenario == "trusted-envrc":
                    _ = (tools.root / ".envrc").write_text("export FIXTURE_ENVRC=1\n")
                    _ = copies.git(tools.root, "add", "-f", ".envrc")
                    _ = copies.git(tools.root, "commit", "-qm", "Add the approved envrc")
                if mismatch:
                    _ = copies.git(tools.root, "rm", "-q", ".envrc")
                    _ = copies.git(tools.root, "commit", "-qm", "Remove the envrc")
                observed = self.root / f"remote-{scenario}.json"
                ready = self.root / f"remote-{scenario}-ready"
                words = ("bash", str(verify), "doc" if doc else "check", "sample",
                         "--target", "peer" if linux else "mac")
                case_environment = {**environment, "VERIFY_MAC_RUNNER": str(parser_runner),
                                    "TEST_REMOTE_SOURCE": str(observed), "SLOT_ORDER_CHECK": str(started),
                                    "TEST_RUNNER_MAC": "passed", "TEST_RUNNER_STATUS": "0"}
                if scenario == "retry":
                    words = words[:-2]
                if scenario == "retry" or (linux and scenario != "clean-linux"):
                    startup = self.root / f"{scenario}-verify-startup.sh"
                    commands = [
                        f'if [[ "$0" == {shlex.quote(str(verify))} && "$PWD" == "${{COPIES_STEP_SOURCE:-}}" ]]; then',
                        "unset BASH_ENV",
                        "printf '%s\\n' 'fn main() { let _ = 1; }' > src/main.rs",
                    ]
                    if mismatch:
                        commands.append("export COPIES_STEP_REPOSITORY=" + shlex.quote(str(other)))
                    if source_record:
                        admitted = shlex.quote(str(observed.with_suffix(".source")))
                        commands.append("printf '{}' > .scratch.json && git add -f .scratch.json"
                                        + " && git add src/main.rs src/lib.rs && "
                                        + f"(git rev-parse --show-toplevel && git write-tree) > {admitted}")
                    _ = startup.write_text("\n".join([*commands, "fi", ""]))
                    case_environment["BASH_ENV"] = str(startup)
                if scenario == "prepared":
                    case_environment["TEST_REMOTE_ASSIGNMENT"] = "1"
                if linux:
                    trusted_folder = other.name if mismatch else tools.root.name if trusted else "other-repository"
                    _ = write_config_fixture(config / "nix_env.conf", f"trusted_home_folders={trusted_folder}\n")
                    case_environment.update(HOME=str(self.root), TMPDIR=str(remote_tmp),
                                            TEST_TRUST_CALLER=str(other if mismatch else tools.root))
                    ready.touch()
                if scenario in ("clean-linux", "trusted-linux", "trusted-envrc", "untrusted-envrc", "mismatched-linux"):
                    with locked_environment({**os.environ, **case_environment}, self.queue.loop_access):
                        self.assert_ssh_source(tools.root, other if mismatch else tools.root, revision, scenario)
                    continue
                if scenario == "retry":
                    case_environment.update(TEST_RUNNER_MAC="declined", TEST_RUNNER_STATUS="75",
                        TEST_RUNNER_REASON="source_changed", TEST_PROVIDER_ALLOW_RUNNER_REFUSALS="1",
                        TEST_REMOTE_RELEASE=str(ready), BUILDLOG_OFF="0", BUILDLOG_SYNC="1")
                else:
                    case_environment["TEST_RUNNER_BUSY_UNTIL"] = str(ready)
                def provider(key: placement.HostEvidenceKey) -> placement.HostEvidence:
                    evidence = private_caller_provider(self.queue, case_environment, key)
                    if scenario == "retry":
                        local = key.destination.provider == "local"
                        if local != ready.exists():
                            assert isinstance(evidence, placement.FreshHostEvidence)
                            return replace(evidence, eligibility=placement.TemporaryExclusion("busy"))
                    return evidence

                self.waiting.clear()
                first_request, first_reply = len(self.requests), len(self.replies)
                with (mock.patch.object(self.queue.server, "host_probe", side_effect=provider),
                      mock.patch.dict(os.environ, case_environment)):
                    process = self.start(words, "--apply-uncommitted", str(tools.root), "--merge", revision)
                    if source_record:
                        stdout, stderr = process.communicate(timeout=TEST_HANG_GUARD_SECS)
                        self.assertEqual(process.returncode, 2, stdout + stderr)
                        self.assertIn("the admitted tree contains .scratch.json", stderr)
                        self.assertFalse(observed.exists())
                        self.assertFalse(self.slot_root.exists())
                        caller, tree = observed.with_suffix(".source").read_text().splitlines()
                        request = next(request for request in self.requests[first_request:]
                                       if isinstance(request, wire.PlacementRequest))
                        self.assertEqual(request.constraints.repository, caller)
                        expected = execution_identity.SourceKeyKnown(treekey.committed_tree_key(tree.encode("ascii")))
                        self.assertEqual(request.constraints.source_key, expected)
                        grant = next(reply for reply in self.replies[first_reply:] if isinstance(reply, wire.Assignment))
                        self.assertEqual(grant.assignment.destination.provider, "ssh")
                        self.assertEqual(grant.assignment.source_key, expected)
                        continue
                    if scenario == "retry" or linux:
                        wait_until(lambda: observed.exists() or process.poll() is not None, self.root,
                                   timeout=45, processes=(process,))
                        self.assertTrue(observed.exists(), process.communicate(timeout=5) if process.poll() is not None else "")
                    else:
                        wait_until(lambda: self.waiting.is_set() or process.poll() is not None, self.root,
                                   processes=(process,))
                        self.assertTrue(self.waiting.is_set(),
                                        process.communicate(timeout=5) if process.poll() is not None else "")
                    request = next(request for request in self.requests[first_request:]
                                   if isinstance(request, wire.PlacementRequest))
                    snapshot = Path(request.constraints.repository)
                    self.assertEqual((snapshot / "src/lib.rs").read_text(), "pub fn changed() {}\n")
                    self.assertEqual((snapshot / "src/merged.rs").read_text(), "pub fn merged() {}\n")
                    self.assertFalse((tools.root / "src/merged.rs").exists())
                    key = treekey.tree_key(str(snapshot))
                    if scenario == "retry":
                        _ = (snapshot / "src/lib.rs").write_text("pub fn retried() {}\n")
                    ready.touch()
                    if scenario == "retry":
                        cargo = wait_for_pid(started, processes=(process,))
                        executed = (Path("/proc") / str(cargo) / "cwd").resolve(strict=True)
                        local_key = treekey.tree_key(str(executed))
                        content = (executed / "src/lib.rs").read_text()
                        release.touch()
                    else:
                        local_key, content = "", ""
                    if linux:
                        stdout, stderr = process.communicate(timeout=TEST_HANG_GUARD_SECS)
                        self.assertTrue(observed.exists(), stdout + stderr)
                        caller_trusted, runner_trusted, under_scratch, registered, marked = cast(
                            list[bool], json.loads(observed.with_suffix(".trust").read_text()))
                        self.assertEqual(caller_trusted, trusted or mismatch)
                        self.assertEqual(runner_trusted, trusted, "Linux source approval changed in the copy")
                        self.assertTrue(under_scratch and registered, "The SSH checkout needs a scratch record")
                        self.assertEqual(marked, trusted)
                        self.assertEqual(process.returncode, 0 if trusted else 2, stdout + stderr)
                        if not trusted:
                            self.assertIn("target peer refused: repo", stderr)
                    else:
                        self.finish(process)
                    if scenario == "retry":
                        self.assertEqual(content, "pub fn retried() {}\n")
                        local = next(reply for reply in self.replies[first_reply:]
                                     if isinstance(reply, wire.LocalPlacementGranted))
                        self.assertEqual(local.assignment.source_key, execution_identity.SourceKeyKnown(local_key or ""))
                        records = [cast(dict[str, object], json.loads(line))
                                   for path in Path(environment["BUILDLOG_DIR"]).glob("*/*.jsonl")
                                   for line in path.read_text().splitlines()]
                        self.assertTrue(any(row.get("kind") == "step" and row.get("tree_key") == local_key
                                            for row in records), records)
                    else:
                        self.assertFalse(self.slot_root.exists())
                    cwd, supplied_root, executed_key, library, merged, binary = cast(
                        list[str], json.loads(observed.read_text()))
                    self.assertEqual(cwd, supplied_root)
                    if scenario == "retry":
                        self.assertEqual(binary, "fn main() { let _ = 1; }\n")
                    if doc:
                        repo, staged, main, caller = cast(
                            tuple[str, bool, str, str], json.loads(observed.with_suffix(".doc").read_text()))
                        self.assertTrue(staged)
                        self.assertEqual(repo, supplied_root)
                        self.assertEqual(main, "fn main() {}\n")
                        self.assertEqual(caller, str(tools.root))
                    self.assertEqual(library, "pub fn changed() {}\n")
                    self.assertEqual(merged, "pub fn merged() {}\n")
                    grant = next(reply for reply in self.replies[first_reply:] if isinstance(reply, wire.Assignment))
                    if linux:
                        self.assertEqual(grant.assignment.destination.provider, "ssh")
                    self.assertEqual(grant.assignment.source_key, execution_identity.SourceKeyKnown(executed_key))
                    if scenario == "prepared":
                        self.assertEqual(grant.assignment.source_key, execution_identity.SourceKeyKnown(key or ""))
                        self.assertEqual(Path(supplied_root), snapshot)
                        saved = client.read_assignment(observed.with_suffix(".assignment"))
                        self.assertIsInstance(saved, wire.Assignment)
                        self.assertEqual(saved.assignment.source_key, execution_identity.SourceKeyKnown(executed_key))

    def test_verify_request_only_keeps_the_first_step_routing_and_creates_no_slot(self) -> None:
        tools = self.verify_source()
        source_key = subprocess.run([sys.executable, str(CHECKOUT / "scripts/build_targets/build_targets.py"),
            "source-key", "--repo-root", str(tools.root)], cwd=tools.root, env=tools.environment,
            capture_output=True, text=True, check=True, timeout=TEST_HANG_GUARD_SECS).stdout.strip()
        cases: tuple[tuple[tuple[str, ...], placement.HostSelection, target_config.StepRoute], ...] = (
            (("check", "sample"), placement.ConfiguredOrder(), target_config.ANY_ELIGIBLE_TARGET),
            (("build", "sample", "--target", "mac"), placement.PinnedHost("mac"), target_config.PinnedTarget("mac")),
            (("test", "sample", "--local", "requires Linux eventfd"), placement.ConfiguredOrder(),
                target_config.LocalRequired("requires Linux eventfd")),
            (("test", "sample", "--mac", "requires AppKit"), placement.MacOnlyHost(), target_config.MAC_ONLY),
            (("lint", "sample"), placement.ConfiguredOrder(), target_config.WRITES_TREE),
            (("doc", "sample"), placement.ConfiguredOrder(), target_config.ANY_ELIGIBLE_TARGET),
        )
        for number, (arguments, selection, route) in enumerate(cases):
            with self.subTest(arguments=arguments):
                request_file = tools.root / f"first-step-{number}.json"
                episode, since = f"verify-first-{number}", time.time()
                environment = {**tools.environment, "HERDR_BIN": "/nonexistent/herdr", "UNIT_RUN_DIR": "",
                    "UNIT_SESSION_DIR": "", "UNIT_DETACHED_RUN": "1", "BUILDLOG_FIRST_STEP_REQUEST": str(request_file),
                    "BUILDLOG_FIRST_STEP_PID": str(os.getpid()), "BUILDLOG_FIRST_STEP_EPISODE": episode,
                    "BUILDLOG_FIRST_STEP_SINCE": str(since), "ADMISSION_SOCKET": str(self.queue.socket_path)}
                started = time.monotonic()
                result = subprocess.run(["bash", str(CHECKOUT / "scripts/direct/verify.sh"), *arguments],
                    cwd=tools.root, env=environment, capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS)
                elapsed = time.monotonic() - started
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                request = wire.parse_request(request_file.read_text())
                self.assertIsInstance(request, wire.PlacementRequest)
                assert isinstance(request, wire.PlacementRequest)
                self.assertEqual((request.episode, request.since, request.owner.pid), (episode, since, os.getpid()))
                self.assertEqual(request.constraints.command_kind, arguments[0])
                self.assertEqual(request.constraints.package, "sample")
                self.assertEqual(request.constraints.repository, str(tools.root))
                self.assertEqual(request.constraints.source_key, execution_identity.SourceKeyKnown(source_key))
                self.assertEqual(request.constraints.selection, selection)
                self.assertEqual(request.route, route)
                self.assertIsInstance(request.local, wire.LocalAdmissionContext)
                assert isinstance(request.local, wire.LocalAdmissionContext)
                self.assertEqual(request.local.argv, request.constraints.command)
                self.assertFalse((tools.root / "build").exists())
                self.assertTrue(all(isinstance(request, wire.BuildHistoryRequest) for request in self.requests),
                    self.requests)
                print(f"verify {arguments[0]} startup to its first placement request: {elapsed:.3f} s")
        self.assertTrue(tools.cargo_events())
        self.assertTrue(all(event["args"][0] == "metadata" or "--version" in event["args"]
            for event in tools.cargo_events()), tools.cargo_events())


if __name__ == "__main__":
    _ = unittest.main()
