"""A copy run waits for memory before it holds a repository slot."""
from __future__ import annotations

from contextlib import nullcontext
import json
import os
from pathlib import Path
import select
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from typing import NotRequired, TypedDict, cast, override
from types import SimpleNamespace
from zoneinfo import ZoneInfo
import unittest
from unittest import mock

from scripts.build_queue import admission_client as client, admission_protocol as wire, admission_service, first_step, placement
from scripts.buildlog import repository, treekey
from scripts.build_queue.admission_test_support import AdmissionFixture
from scripts.build_targets import target_config
from scripts.copies import copies
from scripts.copies.copies_test_support import AdmissionWaitReached, copies_environment, stop_before_slot
from scripts.build_queue import step_estimator
from scripts.mac_test import eligibility
from scripts.disks import slots
from scripts.direct.verify_mac_offload_test_support import VerifyMacOffloadFixture
from scripts.lib import build_folders, build_slots, conf
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.lib.shell_test_support import shell_functions
from scripts.lint.memory_test_support import write_config_fixture, write_pressure_reading
from scripts.production.cli_fixture import ImmutableGitSeed
from scripts.production.staff_test_support import fixture_member, isolated_staff_file
from scripts.production import bump_gate, checkpoint_gate, waiting
from scripts.production.production_doc import Production


CHECKOUT = Path(__file__).resolve().parents[2]
GATE_COMMAND = """from pathlib import Path
import json, os, sys, time
sys.path.insert(0, os.environ['SLOT_ORDER_CHECKOUT'])
from scripts.build_queue.queue_place import queue_place

for phase in range(2):
    with queue_place('merge-gate', Path.cwd()):
        print(json.dumps({'phase': phase, 'cwd': str(Path.cwd()), 'run': os.environ.get('BUILDLOG_RUN', ''), 'at': time.monotonic()}), flush=True)
        if not sys.stdin.buffer.read(1):
            break
"""


class StartedStep(TypedDict):
    phase: int
    cwd: str
    run: str
    at: float
    assignment: NotRequired[str]


class SlotOrderTests(unittest.TestCase):
    root: Path = Path()
    source: Path = Path()
    slot_root: Path = Path()
    owner: str = ""
    queue: AdmissionFixture = cast(AdmissionFixture, cast(object, None))
    processes: list[subprocess.Popen[str]] = []
    requests: list[wire.Request] = []
    request_times: list[tuple[wire.Request, float]] = []
    replies: list[wire.Reply] = []
    waiting: threading.Event = threading.Event()
    slot_waiting: threading.Event = threading.Event()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="slot-order-"))).resolve()
        environment = copies_environment(self.root, repository="origin")
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
        member = fixture_member("unit", name="slot-order", parent=parent.id)
        self.owner = str(member.id)
        os.environ["CLAUDE_CODE_SESSION_ID"] = member.current_session
        seed = ImmutableGitSeed.create(self.root / "repository", "main", "Slot Tests", "slots@example.invalid",
            {"Cargo.toml": "[workspace]\n", "tracked.txt": "committed\n", "merge_gate.py": GATE_COMMAND,
             ".gitignore": "target/\n"})
        self.source = seed.checkout
        self.slot_root = self.root / "slots"
        self.processes = []
        self.requests = []
        self.request_times = []
        self.replies = []
        self.waiting = threading.Event()
        self.slot_waiting = threading.Event()
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
            if isinstance(reply, wire.RepositorySlotWaiting):
                self.slot_waiting.set()
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

    def start(self, command: tuple[str, ...], *options: str, since_age: float = 0) -> subprocess.Popen[str]:
        launcher = [sys.executable, "-m", "scripts.copies.copies"]
        if since_age:
            program = ("import sys,time; from types import SimpleNamespace; from unittest.mock import patch; "
                + "from scripts.copies import copies; from scripts.build_queue import first_step; "
                + f"clock=SimpleNamespace(time=lambda: time.time()-{since_age!r})\n"
                + "with patch.object(first_step, 'time', clock): raise SystemExit(copies.main(sys.argv[1:]))\n")
            launcher = [sys.executable, "-c", program]
        process = subprocess.Popen([*launcher, "run", "--repo", str(self.source),
            "--commit", "HEAD", *options, "--", *command], cwd=CHECKOUT, env=dict(os.environ),
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

    def report(self) -> slots.SlotReport:
        return slots.read_slots(self.slot_root, self.root / "cargo", conf.read("build_folders"),
            self.queue.server.host, None, self.queue.clock.now(), build_folders.build_folder_state,
            owners=lambda owner: owner)

    def test_scratch_queue_command_waits_before_it_creates_a_slot(self) -> None:
        self.queue.hold_starts()
        owner = fixture_member("showrunner", name="scratch-owner")
        production = Production(self.root / "production.md", owner.name, "main", self.source,
                                self.root / "production.log", ZoneInfo("UTC"), owner)
        unit = waiting.Unit("scratch-unit", "", "main", self.source, None, owner.id)
        command = shlex.join((sys.executable, str(self.source / "merge_gate.py"), "--tests-only"))
        execute = mock.Mock(side_effect=AssertionError("the scratch command started before admission"))
        with (mock.patch.object(waiting, "named_unit", return_value=unit),
              mock.patch.object(waiting, "subprocess", SimpleNamespace(run=execute, STDOUT=subprocess.STDOUT)),
              self.assertRaises(AdmissionWaitReached), stop_before_slot(self, self.slot_root)):
            waiting.scratch_test(production, unit.name, command, self.root / "scratch-state")

    def test_bump_queue_command_waits_before_it_creates_a_slot(self) -> None:
        self.queue.hold_starts()
        tip = subprocess.run(["git", "-C", str(self.source), "rev-parse", "HEAD"], check=True,
            capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS).stdout.strip()
        command = [sys.executable, str(self.source / "merge_gate.py"), "--tests-only"]
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
        with (mock.patch.dict(os.environ, {**environment, "MERGE_CHECKPOINT_VERIFY": str(tools.root / "verify.sh")}),
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
                    command = (sys.executable, str(self.source / "merge_gate.py"), "--tests-only")
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
        result = subprocess.run(["bash", "--noprofile", "--norc", "-c", script], cwd=self.source,
            env={**os.environ, "HOLD_WAIT_DIR": str(self.root), "TEST_PLACEMENT_ARGUMENTS": str(arguments_file)},
            capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.splitlines(),
            ["4242", "123.25", "parent-episode", str(self.root / "parent-assignment.json")])
        arguments = arguments_file.read_text().split("\0")
        self.assertEqual(arguments.count("place-step"), 1)
        self.assertEqual(arguments[arguments.index("--pid") + 1], "4242")
        self.assertEqual(arguments[arguments.index("--since") + 1], "123.25")
        self.assertEqual(arguments[arguments.index("--episode") + 1], "parent-episode")
        self.assertEqual(arguments[arguments.index("--assignment-file") + 1], str(self.root / "parent-assignment.json"))

    def test_waiting_and_stopped_runs_leave_no_slot_or_lease(self) -> None:
        self.queue.hold_starts()
        process = self.start((sys.executable, str(self.source / "merge_gate.py"), "--tests-only"))
        self.assertTrue(self.waiting.wait(TEST_HANG_GUARD_SECS), "the copy run did not enter the memory queue")
        with self.queue.loop_turn():
            self.assertTrue(self.queue.server.waiters)
            self.assertFalse(self.queue.caller_reservations())
        self.assertFalse(self.slot_root.exists())
        self.assertEqual(self.report()["groups"], [])
        process.kill()
        _ = process.communicate(timeout=TEST_HANG_GUARD_SECS)
        self.assertFalse(self.slot_root.exists())
        self.assertFalse(list(self.slot_root.glob("*.lease.json")))

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
        _ = wrapper.write_text(f"#!{sys.executable}\nimport os,sys\n"
            + "if 'worktree' in sys.argv and 'add' in sys.argv:\n"
            + f" with open({str(ready)!r}, 'w') as notice: notice.write('ready\\n')\n"
            + f" with open({str(proceed)!r}, 'rb') as permission: permission.read(1)\n"
            + f"os.execv({binary!r}, [{binary!r}, *sys.argv[1:]])\n")
        wrapper.chmod(0o755)
        with mock.patch.dict(os.environ, {"PATH": str(scripts) + os.pathsep + os.environ["PATH"]}):
            process = self.start((sys.executable, str(self.source / "merge_gate.py"), "--tests-only"),
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
        self.waiting.clear()
        _ = os.write(proceed_fd, b"x")
        first = self.next_step(process)
        self.assertEqual(first["phase"], 0)
        self.assertFalse(self.waiting.is_set(), "the first command step entered the memory queue again")
        self.assertTrue(first["run"].startswith(f"{process.pid}:"), first)
        tree = Path(first["cwd"])
        self.assertEqual(tree, self.slot_root / "origin-slot1")
        self.assertEqual((tree / "tracked.txt").read_text(), "applied after admission\n")
        self.assertEqual(build_folders.build_folder_state(tree), "held")
        memory_requests = [request for request in self.requests if isinstance(request, wire.AdmitRequest)
            and request.profile.step == "merge-gate"]
        self.assertGreaterEqual(len(memory_requests), 2)
        self.assertEqual({request.run for request in memory_requests},
            {wire.RunIdentity(process.pid, first["run"].split(":", 1)[1])})
        release_times = [observed for request, observed in self.request_times if isinstance(request, wire.ReleaseRequest)]
        self.assertTrue(release_times)
        print(f"slot hand-off to the first step: {first['at'] - release_times[-1]:.3f} s")
        self.waiting.clear()
        with self.queue.loop_turn():
            self.queue.free(0)
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
        _ = write_config_fixture(self.queue.root / "build_admission.conf", "running_pass_seconds=0.05\n")
        self.queue.hold_starts()
        with mock.patch.dict(os.environ, {"LINT_QUEUE_LEAVE_HOOK": "test -e " + shlex.quote(str(leave))}):
            process = self.start((sys.executable, str(self.source / "merge_gate.py"), "--tests-only"))
        self.assertTrue(self.waiting.wait(TEST_HANG_GUARD_SECS), "the caller did not enter the memory queue")
        leave.touch()
        output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(process.returncode, 75, output + errors)
        self.assertFalse(self.slot_root.exists())
        self.assertFalse(list(self.slot_root.glob("*.lease.json")))
        with self.queue.loop_turn():
            self.assertFalse(self.queue.server.waiters)
            self.assertFalse(self.queue.caller_reservations())

    def test_a_lost_slot_race_keeps_the_admitted_episode_and_since(self) -> None:
        _ = write_config_fixture(self.root / "config/build_folders.conf",
            f"# whole: 1..\nslots.{self.queue.server.host}.origin.max=1\n")
        for since_age in (0.0, 120.0):
            with self.subTest(since_age=since_age):
                self.lost_slot_race(since_age)

    def lost_slot_race(self, since_age: float) -> None:
        self.queue.allow_next_start()
        self.slot_waiting.clear()
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
            process = self.start((sys.executable, str(self.source / "merge_gate.py"), "--tests-only"), since_age=since_age)
            self.assertTrue(granted.wait(TEST_HANG_GUARD_SECS))
            with build_slots.take_slot(self.source, "HEAD", showrunner="builds", unit=self.owner,
                    work="race", purpose="competing run", wait_policy=build_slots.SlotWaitPolicy.REFUSE):
                deliver.set()
                self.assertTrue(self.slot_waiting.wait(TEST_HANG_GUARD_SECS))
                memory = [request for request in self.requests[request_offset:] if isinstance(request, wire.AdmitRequest)]
                slot = [request for request in self.requests[request_offset:] if isinstance(request, wire.RepositorySlotRequest)]
                self.assertTrue(memory)
                self.assertTrue(slot)
                self.assertEqual({(request.episode, request.since) for request in slot},
                    {(memory[0].episode, memory[0].since)})
                self.assertTrue(all(request.deadline > time.time() for request in slot), slot)
                if since_age:
                    self.assertLess(memory[0].since, started_at - since_age / 2)
                with self.queue.loop_turn():
                    preparation, = self.queue.caller_reservations().values()
                    self.assertEqual(preparation.owner.pid, process.pid)
            started = self.next_step(process)
            self.assertEqual(Path(started["cwd"]), self.slot_root / "origin-slot1")
            self.finish(process)

    def test_other_commands_take_the_slot_without_a_memory_queue_request(self) -> None:
        self.queue.hold_starts()
        program = "import json,os,sys; print(json.dumps({'phase':0,'cwd':os.getcwd(),'run':os.environ.get('BUILDLOG_RUN','')}),flush=True); sys.stdin.read(1)"
        process = self.start((sys.executable, "-c", program))
        started = self.next_step(process)
        self.assertEqual(Path(started["cwd"]), self.slot_root / "origin-slot1")
        self.assertEqual(build_folders.build_folder_state(Path(started["cwd"])), "held")
        self.assertEqual(self.requests, [])
        self.finish(process)

    def verify_source(self) -> VerifyMacOffloadFixture:
        tools = VerifyMacOffloadFixture()
        tools.root = self.root / "verify"
        tools.root.mkdir()
        tools.build_fixture()
        real_git = shutil.which("git")
        assert real_git is not None
        shutil.rmtree(tools.root / ".git")
        (tools.bin_directory / "git").unlink()
        (tools.bin_directory / "git").symlink_to(real_git)
        _ = (tools.root / ".gitignore").write_text("/*\n!/Cargo.toml\n!/src/\n")
        _ = (tools.root / "src/main.rs").write_text("fn main() {}\n")
        for arguments in (("init", "-q", "-b", "main"), ("add", "Cargo.toml", "src"),
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
            "SCRATCH_ROOT", "SCRATCH_COPIES_ROOT", "SCRATCH_HOLDS", "CLAUDE_CODE_SESSION_ID", "STAFF_FILE")},
            "ADMISSION_SOCKET": str(self.queue.socket_path), "UNIT_RUN_DIR": "", "UNIT_SESSION_DIR": ""}
        _ = shutil.copy2(tools.root / "build_targets.conf", Path(environment["CONF_DIR"]) / "build_targets.conf")
        _ = shutil.copy2(tools.root / "mac settings.conf", Path(environment["CONF_DIR"]) / "mac settings.conf")
        def provider(key: placement.HostEvidenceKey) -> placement.HostEvidence:
            if key.destination.provider == "local":
                return placement.FreshHostEvidence(key, self.queue.clock.now(), "", placement.AllowedHost())
            return placement.HostEvidenceUnavailable(key, self.queue.clock.now(), "The fixture has no remote provider.")
        _ = self.enterContext(mock.patch.object(self.queue.server, "host_probe", side_effect=provider))
        self.source = tools.root
        command = tools.root / "verify.sh"
        _ = command.write_text("exec "+sys.executable+" -c "+shlex.quote(
            "import json,os,sys,time; print(json.dumps({'phase':0,'cwd':os.getcwd(),"
            + "'run':os.environ.get('BUILDLOG_RUN',''),'at':time.monotonic(),"
            + "'assignment':os.environ.get('BUILDLOG_FIRST_STEP_ASSIGNMENT','')}),flush=True); sys.stdin.read(1)"))
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
                        with copies.standing(self.owner, snapshot, copies.CommittedSource("HEAD")) as tree:
                            pool = "origin" if origin else source.name
                            self.assertEqual(tree, self.slot_root / (pool + "-slot1"))
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
            process = self.start(("bash", str(tools.root / "verify.sh"), "check", "sample"),
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
            process = self.start(("bash", str(tools.root / "verify.sh"), "check", "sample"))
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
            process = self.start(("bash", str(tools.root / "verify.sh"), "check", "sample"))
        output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(process.returncode, 75, output + errors)
        self.assertFalse(self.slot_root.exists())
        self.assertFalse(list(self.slot_root.glob("*.lease.json")))
        self.assertFalse(list((self.root / "session").rglob("Cargo.toml")))
        with self.queue.loop_turn():
            self.assertFalse(self.queue.server.waiters)
            self.assertFalse(self.queue.caller_reservations())
        self.assertTrue(any(isinstance(request, wire.CancelPlacement) for request in self.requests))

    def test_older_commit_admits_a_package_absent_from_the_current_checkout(self) -> None:
        tools = self.verify_source()
        environment = self.verify_run_environment(tools)
        _ = subprocess.run(["git", "-C", str(tools.root), "tag", "with-package"], check=True, capture_output=True)
        _ = (tools.root / "Cargo.toml").write_text('[workspace]\nmembers = []\n')
        for arguments in (("add", "Cargo.toml"), ("commit", "-qm", "Remove the package")):
            _ = subprocess.run(["git", "-C", str(tools.root), *arguments], check=True, capture_output=True)
        with mock.patch.dict(os.environ, environment):
            process = self.start(("bash", str(tools.root / "verify.sh"), "check", "sample"),
                "--commit", "with-package")
        started = self.next_step(process)
        self.assertIn('name = "sample"', (Path(started["cwd"]) / "Cargo.toml").read_text())
        requests = [request for request in self.requests if isinstance(request, wire.PlacementRequest)]
        self.assertTrue(requests)
        self.assertEqual(requests[0].constraints.package, "sample")
        self.finish(process)

    def test_remote_grant_source_key_matches_the_prepared_slot(self) -> None:
        tools = self.verify_source()
        environment = self.verify_run_environment(tools)
        _ = (tools.root / "src/merged.rs").write_text("pub fn merged() {}\n")
        for arguments in (("add", "src/merged.rs"), ("commit", "-qm", "Add the merge source")):
            _ = subprocess.run(["git", "-C", str(tools.root), *arguments], check=True, capture_output=True)
        revision = subprocess.run(["git", "-C", str(tools.root), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True).stdout.strip()
        _ = subprocess.run(["git", "-C", str(tools.root), "checkout", "--detach", "HEAD^"],
            check=True, capture_output=True)
        _ = (tools.root / "src/lib.rs").write_text("pub fn changed() {}\n")
        def provider(key: placement.HostEvidenceKey) -> placement.HostEvidence:
            return placement.FreshHostEvidence(key, self.queue.clock.now(), "", placement.AllowedHost())
        with mock.patch.object(self.queue.server, "host_probe", side_effect=provider), \
                mock.patch.dict(os.environ, environment):
            process = self.start(("bash", str(tools.root / "verify.sh"), "check", "sample", "--target", "mac"),
                "--apply-uncommitted", str(tools.root), "--merge", revision)
            started = self.next_step(process)
            grant = client.read_assignment(Path(started.get("assignment", "")))
            self.assertIsInstance(grant, wire.Assignment)
            self.assertEqual(grant.assignment.source_key, placement.SourceKeyKnown(treekey.tree_key(started["cwd"]) or ""))
            self.assertEqual((Path(started["cwd"]) / "src/lib.rs").read_text(), "pub fn changed() {}\n")
            self.assertEqual((Path(started["cwd"]) / "src/merged.rs").read_text(), "pub fn merged() {}\n")
            self.assertFalse((tools.root / "src/merged.rs").exists())
            self.finish(process)

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
                self.assertEqual(request.constraints.source_key, placement.SourceKeyKnown(source_key))
                self.assertEqual(request.constraints.selection, selection)
                self.assertEqual(request.route, route)
                self.assertIsInstance(request.local, wire.LocalAdmissionContext)
                assert isinstance(request.local, wire.LocalAdmissionContext)
                self.assertEqual(request.local.argv, request.constraints.command)
                self.assertFalse((tools.root / "slots").exists())
                self.assertTrue(all(isinstance(request, wire.BuildHistoryRequest) for request in self.requests),
                    self.requests)
                print(f"verify {arguments[0]} startup to its first placement request: {elapsed:.3f} s")
        self.assertTrue(tools.cargo_events())
        self.assertTrue(all(event["args"][0] == "metadata" or "--version" in event["args"]
            for event in tools.cargo_events()), tools.cargo_events())


if __name__ == "__main__":
    _ = unittest.main()
