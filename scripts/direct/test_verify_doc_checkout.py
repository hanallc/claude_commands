#!/usr/bin/env python3
"""Exercise doc verification against real Git trees and fake Cargo."""
from __future__ import annotations

from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import os
from pathlib import Path
import select
import subprocess
import sys
from threading import Event
from typing import cast
import unittest
from unittest import mock

from scripts.build_queue import admission_protocol as wire, admission_records, admission_service as service, placement
from scripts.buildlog import treekey
from scripts.build_queue.admission_test_support import AdmissionFixture, caller_admission
from scripts.direct.verify_doc_test_support import CARGO_STUB, VERIFY, VerifyDocFixture
from scripts.direct.verify_mac_offload_test_support import processes_carrying, reap, wait_for_marker_exit
from scripts.direct.verify_scope_test_support import use_fixture_scope_readings, write_real_scope_launcher
from scripts.direct.wait_for_test_state import wait_for_pid, wait_until
from scripts.lib import build_folders, build_slots
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.lint.memory_admit import scope_members
from scripts.lint.memory_test_support import write_config_fixture, write_pressure_reading
from scripts.production.staff_test_support import fixture_member, isolated_staff_file


DOC_COMMAND = ("bash", str(VERIFY), "doc")


class VerifyDocCheckoutTests(VerifyDocFixture):
    def start_doc(self, *, command: tuple[str, ...] = DOC_COMMAND,
                  pass_fds: tuple[int, ...] = ()) -> tuple[subprocess.Popen[bytes], Path, str]:
        log = self.root / "verify.log"
        marker = f"TEST_DOC_CHECKOUT={self.root}"
        with log.open("wb") as output:
            process = subprocess.Popen(
                command, cwd=self.repo, stdout=output, stderr=output,
                env={**self.environment, "TEST_DOC_CHECKOUT": str(self.root)}, start_new_session=True, pass_fds=pass_fds,
            )
        self.addCleanup(process.wait, timeout=TEST_HANG_GUARD_SECS)
        self.addCleanup(reap, marker)
        return process, log, marker

    @contextmanager
    def hold_cargo_token(self) -> Generator[None]:
        board = self.root / "board"
        self.environment.update({"UNIT_RUN_DIR": str(board), "UNIT_TEAM_ROLE": "doc"})
        helper = VERIFY.with_name("board.sh")
        token = [str(board), "other", "cargo", "--pid", str(os.getpid())]
        _ = subprocess.run(["bash", str(helper), "acquire", *token], env=self.environment,
                           check=True, capture_output=True, timeout=TEST_HANG_GUARD_SECS)
        try:
            yield
        finally:
            _ = subprocess.run(["bash", str(helper), "release", *token], env=self.environment,
                               check=True, capture_output=True, timeout=TEST_HANG_GUARD_SECS)

    @contextmanager
    def occupy_doc_slot(self, wait_policy: build_slots.SlotWaitPolicy = build_slots.SlotWaitPolicy.QUEUE) -> Generator[Path]:
        admission = caller_admission(self, self.root, self.environment)
        settings = Path(self.environment["CONF_DIR"]) / "build_folders.conf"
        _ = write_config_fixture(settings, f"# whole: 1..\nslots.{admission.server.host}.workspace.max=1\n")
        with mock.patch.dict(os.environ, self.environment), build_slots.take_slot(
                self.repo, "HEAD", showrunner="builds", unit="doc-test", work="competing copy", purpose="doc",
                wait_policy=wait_policy) as slot:
            yield slot.path

    def use_real_scope(self) -> AdmissionFixture:
        write_real_scope_launcher(self.root / "bin")
        admission = caller_admission(self, self.root, self.environment)
        use_fixture_scope_readings(self, self.root, admission)
        return admission

    def install_scope_interpreter(self, supervisor_code: str) -> None:
        wrapper = self.root / "scope-interpreter"
        self.executable(wrapper, '''import os
import sys
arguments = sys.argv[1:]
script_arguments = arguments[1:] if arguments[0] == "-P" else arguments
script = script_arguments[0]
if not script.endswith("/verify_scoped_step.py"):
    os.execv(sys.executable, [sys.executable, *arguments])
''' + supervisor_code)
        self.environment["CLAUDE_PYTHON"] = str(wrapper)

    def test_doc_finishes_while_another_caller_holds_the_cargo_token(self) -> None:
        with self.hold_cargo_token():
            result = self.verify()
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(len(self.cargo_calls()), 1)
            self.assertEqual(len(self.pass_records()), 1)

    def assert_doc_retains_checkout_until_scope_empties(self, *, interrupt: bool,
                                                       sweep_scope_sidecar: bool = False,
                                                       command: tuple[str, ...] = DOC_COMMAND) -> None:
        admission = self.use_real_scope()
        scope_file = self.root / "doc-scope"
        child_pid = self.root / "doc-descendant"
        pending_pid = child_pid.with_suffix(".new")
        lease_at_launch = self.root / "lease-at-launch.json"
        release = self.root / "end-cargo"
        helper = VERIFY.with_name("wait_for_test_state.py")
        self.executable(self.root / "bin/cargo", CARGO_STUB.replace(
            '    raise SystemExit(int(os.environ.get("TEST_DOC_STATUS", "0")))', f'''
    sys.path.insert(0, {str(VERIFY.parents[2])!r})
    from scripts.direct.wait_for_test_state import wait_for_file

    checkout = Path.cwd()
    lease = checkout.with_name(checkout.name + ".lease.json")
    Path({str(lease_at_launch)!r}).write_text(lease.read_text())
    child = subprocess.Popen([sys.executable, {str(helper)!r}, {str(self.root / "end-descendant")!r}],
                             close_fds=True)
    Path({str(scope_file)!r}).write_text(os.environ["BUILDLOG_STEP_SCOPE"])
    Path({str(pending_pid)!r}).write_text(str(child.pid))
    Path({str(pending_pid)!r}).replace({str(child_pid)!r})
    wait_for_file(Path({str(release)!r}))
    raise SystemExit(0)
'''))
        process, log, marker = self.start_doc(command=command)
        wait_until(lambda: child_pid.is_file() or process.poll() is not None, self.root, processes=(process,))
        self.assertTrue(child_pid.is_file(), log.read_text())
        descendant = int(child_pid.read_text())
        scope = Path(scope_file.read_text())
        call, = self.cargo_calls()
        checkout = Path(call["cwd"])
        lease = cast(dict[str, object], json.loads(lease_at_launch.read_text()))
        copy_owner = os.pidfd_open(cast(int, lease["pid"]))
        self.addCleanup(os.close, copy_owner)
        kill_file = scope / "cgroup.kill"
        self.assertIn(descendant, scope_members(scope))
        kill_file.chmod(0)
        try:
            if sweep_scope_sidecar:
                reservation, = admission.caller_reservations().values()
                Path(reservation.sidecar).unlink()
            if interrupt:
                process.terminate()
            else:
                release.touch()
            status = process.wait(TEST_HANG_GUARD_SECS)
            self.assertIn(descendant, scope_members(scope))
            ended, _, _ = select.select([copy_owner], [], [], TEST_HANG_GUARD_SECS)
            self.assertTrue(ended, "the checkout copy owner remained after Verify exited")
            with build_folders.collector_lease(checkout) as free:
                self.assertFalse(free, "the collector accepted a checkout with a populated scope")
            self.assertEqual(lease["scope"], str(scope))
            self.assertEqual(status, 125, log.read_text())
            self.assertIn("keeping the reservation", log.read_text())
            _ = admission.run_pass()
            self.assertTrue(admission.caller_reservations())
            self.assertFalse(self.pass_records())
            with self.assertRaises(build_slots.SlotsFull), self.occupy_doc_slot(build_slots.SlotWaitPolicy.REFUSE):
                self.fail("another caller took the checkout while the scope was populated")
        finally:
            kill_file.chmod(0o200)
            _ = kill_file.write_text("1\n")
            self.assertEqual(wait_for_marker_exit(marker, TEST_HANG_GUARD_SECS), set())
            _ = admission.run_pass()
        self.assertEqual(build_folders.build_folder_state(checkout), "free")
        with self.occupy_doc_slot(build_slots.SlotWaitPolicy.REFUSE) as next_checkout:
            self.assertEqual(next_checkout, checkout)

    def test_doc_scope_stop_failure_retains_checkout_after_cargo_exits(self) -> None:
        self.assert_doc_retains_checkout_until_scope_empties(interrupt=False)

    def test_doc_scope_stop_failure_retains_checkout_after_interrupt(self) -> None:
        self.assert_doc_retains_checkout_until_scope_empties(interrupt=True)

    def test_doc_scope_stop_failure_retains_checkout_and_reservation_after_sidecar_sweep(self) -> None:
        self.assert_doc_retains_checkout_until_scope_empties(interrupt=False, sweep_scope_sidecar=True)

    def test_copies_run_binds_the_prepared_doc_checkout_before_cargo(self) -> None:
        with isolated_staff_file(self.root / "staff.json", self.environment):
            caller = fixture_member("showrunner", name="doc-copy")
            self.environment["CLAUDE_CODE_SESSION_ID"] = caller.current_session
            copies = VERIFY.parents[1] / "copies/copies.py"
            self.assert_doc_retains_checkout_until_scope_empties(interrupt=False, command=(
                sys.executable, str(copies), "run", "--repo", str(self.repo), "--commit", "HEAD", "--", *DOC_COMMAND,
            ))

    def test_doc_refuses_launch_when_the_scope_publication_is_lost(self) -> None:
        config = Path(self.environment["CONF_DIR"]) / "direct.conf"
        _ = write_config_fixture(config, "# range: 5e-324..\nVERIFY_SCOPE_PUBLICATION_SECONDS=1\n")
        waiting = self.root / "scope-awaiting-permission"
        self.environment["TEST_SCOPE_WAITING"] = str(waiting)
        launcher = self.root / "bin/systemd-run"
        self.executable(launcher, launcher.read_text().replace('exec "$@" 2>&1', '''
rm -- "$peak.cgroup"
: > "$TEST_SCOPE_WAITING"
IFS= read -r permission || exit 125
exit 99
'''))
        self.install_scope_interpreter(f'''
from pathlib import Path
import runpy
sys.path.insert(0, {str(VERIFY.parents[2])!r})
from scripts.direct.wait_for_test_state import wait_for_file
from unittest.mock import patch
from scripts.direct import verify_scoped_step

start = verify_scoped_step.subprocess.Popen

def start_after_publication_is_lost(*args, **kwargs):
    process = start(*args, **kwargs)
    wait_for_file(Path({str(waiting)!r}))
    return process

with patch.object(verify_scoped_step.subprocess, "Popen", start_after_publication_is_lost):
    sys.argv = script_arguments
    runpy.run_path(script, run_name="__main__")
''')
        process, log, _ = self.start_doc()
        wait_until(lambda: waiting.is_file() or process.poll() is not None, self.root, processes=(process,))
        self.assertTrue(waiting.is_file(), log.read_text())
        self.assertEqual(process.wait(timeout=8), 125, log.read_text())
        output = log.read_text()
        self.assertIn("the checkout scope publication timed out", output)
        self.assertIn("keeping the reservation", output)
        self.assertFalse(self.events.exists())
        self.assertFalse(self.pass_records())

    def test_doc_scope_start_error_writes_release_state_before_status_125(self) -> None:
        recorded_failure = self.root / "recorded-scope-failure"
        self.install_scope_interpreter(f'''
import runpy
from pathlib import Path
from unittest.mock import patch

with patch.object(os, "setpgid", side_effect=OSError("cannot set the supervisor group")):
    sys.argv = script_arguments
    try:
        runpy.run_path(script, run_name="__main__")
    except SystemExit:
        Path({str(recorded_failure)!r}).write_text(Path(sys.argv[3]).read_text())
        raise
''')
        result = self.verify()
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 125, output)
        self.assertIn("cannot set the supervisor group", recorded_failure.read_text())
        self.assertIn("keeping the reservation", output)
        self.assertFalse(self.events.exists())
        self.assertFalse(self.pass_records())
        with self.occupy_doc_slot(build_slots.SlotWaitPolicy.REFUSE):
            pass

    def test_doc_scope_command_does_not_inherit_checkout_pipe(self) -> None:
        _ = self.use_real_scope()
        lease_pipe = self.root / "lease-pipe"
        cargo_pipes = self.root / "cargo-pipes"
        self.install_scope_interpreter(f'''
from pathlib import Path
Path({str(lease_pipe)!r}).write_text(os.readlink("/proc/self/fd/9"))
os.execv(sys.executable, [sys.executable, *sys.argv[1:]])
''')
        self.executable(self.root / "bin/cargo", CARGO_STUB.replace(
            '    raise SystemExit(int(os.environ.get("TEST_DOC_STATUS", "0")))', f'''
    targets = []
    for descriptor in Path("/proc/self/fd").iterdir():
        try:
            targets.append(os.readlink(descriptor))
        except FileNotFoundError:
            pass
    Path({str(cargo_pipes)!r}).write_text("\\n".join(targets))
    raise SystemExit(0)
'''))
        result = self.verify()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        pipe = lease_pipe.read_text()
        self.assertTrue(pipe.startswith("pipe:["))
        self.assertNotIn(pipe, cargo_pipes.read_text().splitlines())

    def test_doc_refuses_a_tree_staged_during_the_copy_slot_wait(self) -> None:
        admission = caller_admission(self, self.root, self.environment)
        waiting = Event()

        def observe_wait(reply: wire.Reply) -> wire.Reply:
            if isinstance(reply, wire.RepositorySlotWaiting):
                waiting.set()
            return reply

        with (self.occupy_doc_slot(),
              mock.patch.object(admission.server, "reply_filter", side_effect=observe_wait)):
            process, log, _ = self.start_doc()
            self.assertTrue(waiting.wait(TEST_HANG_GUARD_SECS), log.read_text())
            reservation, = admission.caller_reservations().values()
            self.assertEqual(reservation.owner.pid, process.pid)
            self.assertFalse(self.events.exists())
            _ = (self.repo / "alpha/src/lib.rs").write_text("pub fn staged_while_waiting() {}\n")
            _ = self.git("add", "alpha/src/lib.rs")
        self.assertEqual(process.wait(TEST_HANG_GUARD_SECS), 77, log.read_text())
        self.assertIn("the staged tree changed", log.read_text())
        self.assertFalse(self.events.exists())
        self.assertEqual(self.pass_records(), [])
        self.assertFalse(admission.caller_reservations())

    def test_sigterm_reaps_the_copy_holder_during_the_slot_wait(self) -> None:
        admission = caller_admission(self, self.root, self.environment)
        waiting = Event()

        def observe_wait(reply: wire.Reply) -> wire.Reply:
            if isinstance(reply, wire.RepositorySlotWaiting):
                waiting.set()
            return reply

        with (self.occupy_doc_slot(),
              mock.patch.object(admission.server, "reply_filter", side_effect=observe_wait)):
            process, log, marker = self.start_doc()
            self.assertTrue(waiting.wait(TEST_HANG_GUARD_SECS), log.read_text())
            with admission.loop_turn():
                waiter, = admission.server.repository_slot_waiters.values()
                self.assertIn(waiter.request.owner.pid, processes_carrying(marker))
            process.terminate()
            self.assertEqual(process.wait(TEST_HANG_GUARD_SECS), 143, log.read_text())
            self.assertEqual(processes_carrying(marker), set())
            with admission.loop_turn():
                _ = admission.server.run_pass()
                self.assertFalse(admission.server.repository_slot_waiters)
            self.assertFalse(admission.caller_reservations())
        self.assertFalse(self.events.exists())
        self.assertEqual(self.pass_records(), [])

    def test_doc_records_a_pass_while_the_background_sweep_waits(self) -> None:
        release = self.root / "sweep-release"
        started = self.root / "sweep-pid"
        sweep_events = self.root / "sweep-events"
        os.mkfifo(release)
        wrapper = self.root / "sweep-interpreter"
        self.executable(wrapper, f'''import os, sys
from pathlib import Path
if sys.argv[1].endswith("/sweep.py"):
    for descriptor in (8, 74):
        try:
            os.write(descriptor, f"sweep started on {{descriptor}}\\n".encode())
        except OSError:
            pass
    Path({str(started)!r}).write_text(str(os.getpid()))
    with open({str(release)!r}) as gate:
        gate.read()
else:
    os.execv(sys.executable, [sys.executable, *sys.argv[1:]])
''')
        self.executable(self.root / "bin/systemd-cat", "#!/bin/sh\nexec cat\n")
        self.environment.update({"LINT_SKIP_SWEEP": "0", "CLAUDE_PYTHON": str(wrapper)})
        process, log, marker = self.start_doc(command=(
            "bash", "-c", 'exec 8>"$1" 74>&8; shift; exec bash "$@"',
            "doc-sweep", str(sweep_events), str(VERIFY), "doc",
        ))
        sweep_pid = wait_for_pid(started, processes=(process,))
        self.assertEqual(process.wait(TEST_HANG_GUARD_SECS), 0, log.read_text())
        self.assertIn(sweep_pid, processes_carrying(marker))
        self.assertEqual(len(self.pass_records()), 1)
        with release.open("w"):
            pass
        self.assertEqual(sweep_events.read_text().splitlines(), ["sweep started on 8", "sweep started on 74"])

    def test_doc_waits_for_admission_without_a_slot_or_lease(self) -> None:
        admission = caller_admission(self, self.root, self.environment)
        waiting = Event()
        grants: list[wire.LocalPlacementGranted] = []
        reservation = self.root / "doc-reservation"
        self.environment.update({"BUILDLOG_OFF": "0", "BUILDLOG_SYNC": "1"})
        member = self.repo / "alpha/src/lib.rs"
        staged = "pub fn staged() {}\n"
        _ = member.write_text(staged)
        _ = self.git("add", "alpha/src/lib.rs")
        _ = member.write_text("pub fn unstaged() {}\n")
        _ = (self.repo / "notes.md").write_text("untracked")
        self.executable(self.root / "bin/cargo", CARGO_STUB.replace(
            'elif arguments[:1] == ["doc"]:', 'elif arguments[:1] == ["doc"]:\n' +
            f'    Path({str(reservation)!r}).write_text(os.environ["BUILDLOG_RESERVED_STEP"].split("\\t")[0])'))

        def observe_reply(reply: wire.Reply) -> wire.Reply:
            if isinstance(reply, wire.PlacementWait) and isinstance(reply.wait, wire.Wait):
                waiting.set()
            if isinstance(reply, wire.LocalPlacementGranted):
                grants.append(reply)
            return reply

        admission.hold_starts()
        with (mock.patch.object(admission.server, "reply_filter", side_effect=observe_reply),
              ThreadPoolExecutor(max_workers=1) as executor):
            result = executor.submit(self.verify)
            try:
                self.assertTrue(waiting.wait(TEST_HANG_GUARD_SECS), "doc did not wait for admission")
                self.assertFalse(admission.caller_reservations())
                self.assertEqual(list((self.root / "slots").glob("*")), [])
                self.assertFalse(self.events.exists())
            finally:
                write_pressure_reading(self.root / "pressure")
            finished = result.result(timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(finished.returncode, 0, finished.stdout + finished.stderr)
        grant, = grants
        self.assertEqual(reservation.read_text(), grant.grant.reservation)
        self.assertFalse(admission.caller_reservations())
        call, = self.cargo_calls()
        self.assertEqual(call["sources"]["alpha/src/lib.rs"], staged)
        self.assertFalse(call["notes_present"])
        checkout = Path(call["cwd"])
        self.assertTrue(checkout.is_relative_to(self.root / "slots"))
        key = treekey.tree_key(str(checkout))
        assert key is not None
        self.assertEqual(grant.assignment.source_key, placement.SourceKeyKnown(key))
        rows = (cast(dict[str, object], json.loads(line))
                for path in (self.root / "buildlog").glob("*/*.jsonl")
                for line in path.read_text().splitlines())
        row, = (row for row in rows if row.get("kind") == "step" and row.get("step") == "doc")
        self.assertEqual(row["tree_key"], key)
        self.assertIs(row["tree_changed"], False)
        self.assertEqual(len(self.pass_records()), 1)

    def test_hold_after_grant_releases_doc_checkout_without_waiting(self) -> None:
        admission = caller_admission(self, self.root, self.environment)
        holders = Path(self.environment["BUILD_HOLD_DIR"])
        holders.mkdir()
        holder = holders / "hana"
        grants: list[wire.LocalPlacementGranted] = []
        later_requests: list[wire.PlacementRequest] = []
        handle_request = admission.server.handle_request

        def observe_request(request: wire.Request, connection: service.Connection | service.NoConnection
                            = service.NO_CONNECTION) -> list[wire.Reply]:
            if grants and isinstance(request, wire.PlacementRequest):
                later_requests.append(request)
            return handle_request(request, connection)

        def start_hold(reply: wire.Reply) -> wire.Reply:
            if isinstance(reply, wire.LocalPlacementGranted):
                grants.append(reply)
                _ = holder.write_text(json.dumps({
                    "holder": "hana", "since": "2026-11-17T20:00:00+00:00", "for": "review", "release_eta": "unknown",
                }))
            return reply

        with (mock.patch.object(admission.server, "reply_filter", side_effect=start_hold),
              mock.patch.object(admission.server, "handle_request", side_effect=observe_request)):
            result = self.verify()
        self.assertEqual(result.returncode, 75, result.stdout + result.stderr)
        self.assertIn("natedev is held by hana for review", result.stdout + result.stderr)
        self.assertEqual(len(grants), 1)
        self.assertEqual(later_requests, [])
        self.assertTrue(holder.exists())
        self.assertFalse(self.events.exists())
        self.assertEqual(self.pass_records(), [])
        self.assertFalse(admission.caller_reservations())
        checkout, = (self.root / "slots").glob("workspace-slot*/")
        self.assertEqual(build_folders.build_folder_state(checkout), "free")

    def test_doc_refuses_a_tree_staged_during_admission(self) -> None:
        admission = caller_admission(self, self.root, self.environment)
        waiting = Event()

        def observe_wait(reply: wire.Reply) -> wire.Reply:
            if isinstance(reply, wire.PlacementWait) and isinstance(reply.wait, wire.Wait):
                waiting.set()
            return reply

        admission.hold_starts()
        with (mock.patch.object(admission.server, "reply_filter", side_effect=observe_wait),
              ThreadPoolExecutor(max_workers=1) as executor):
            result = executor.submit(self.verify)
            try:
                self.assertTrue(waiting.wait(TEST_HANG_GUARD_SECS), "doc did not wait for admission")
                _ = (self.repo / "alpha/src/lib.rs").write_text("pub fn staged_while_waiting() {}\n")
                _ = self.git("add", "alpha/src/lib.rs")
            finally:
                write_pressure_reading(self.root / "pressure")
            finished = result.result(timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(finished.returncode, 77, finished.stdout + finished.stderr)
        self.assertIn("the staged tree changed", finished.stderr)
        self.assertFalse(self.events.exists())
        self.assertEqual(self.pass_records(), [])
        self.assertFalse(admission.caller_reservations())

    def test_doc_uses_the_prepared_copy_without_taking_a_second_slot(self) -> None:
        original = self.repo
        with mock.patch.dict(os.environ, self.environment), build_slots.take_slot(
                original, "HEAD", showrunner="builds", unit="doc-test", work="prepared copy", purpose="doc") as slot:
            self.repo: Path = slot.path
            self.environment["BUILDLOG_PREPARED_COPY"] = str(slot.path)
            descriptor = build_folders.build_folder_lock_fd(slot.path)
            self.environment["BUILDLOG_PREPARED_COPY_LOCK_FD"] = str(descriptor)
            self.environment["BUILDLOG_RUN"] = f"{os.getpid()}:{admission_records.process_start_time(os.getpid())}"
            self.environment["TEST_METADATA"] = self.environment["TEST_METADATA"].replace(str(original), str(slot.path))
            process, log, _ = self.start_doc(pass_fds=(descriptor,))
            self.assertEqual(process.wait(timeout=TEST_HANG_GUARD_SECS), 0, log.read_text())
            self.assertEqual(self.cargo_calls()[0]["cwd"], str(slot.path))
            self.assertEqual(build_folders.build_folder_state(slot.path), "held")
            self.assertEqual(list((self.root / "slots").glob("*-slot*.lease.json")),
                             [build_slots.lease_path(slot.path)])

    def test_doc_takes_its_own_slot_when_the_prepared_copy_names_another_tree(self) -> None:
        self.environment["BUILDLOG_PREPARED_COPY"] = str(self.root / "another-copy")
        result = self.verify()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        checkout = Path(self.cargo_calls()[0]["cwd"])
        self.assertNotEqual(checkout, self.repo)
        self.assertTrue(checkout.is_relative_to(self.root / "slots"))
        self.assertEqual(build_folders.build_folder_state(checkout), "free")
        self.assertEqual(len(list((self.root / "slots").glob("*-slot*.lease.json"))), 1)

    def test_doc_replaces_checkout_and_reuses_target_preserving_unchanged_mtimes(self) -> None:
        stamp = 1_000_000_000_000_000_000
        probe = '''
    if os.environ.get("TEST_DOC_STAMP"):
        stamp = int(os.environ["TEST_DOC_STAMP"])
        os.utime("beta/src/lib.rs", ns=(stamp, stamp))
    with Path(os.environ["TEST_DOC_TIMES"]).open("a") as output:
        output.write(str(Path("beta/src/lib.rs").stat().st_mtime_ns) + "\\n")
    assert Path("deleted.txt").is_file() == (os.environ.get("TEST_DOC_DELETED") == "present")
'''
        self.executable(self.root / "bin/cargo", CARGO_STUB.replace(
            'elif arguments[:1] == ["doc"]:', 'elif arguments[:1] == ["doc"]:' + probe))
        times = self.root / "doc-times"
        self.environment.update({"TEST_DOC_STAMP": str(stamp), "TEST_DOC_TIMES": str(times),
                                 "TEST_DOC_DELETED": "present"})
        removed = self.repo / "deleted.txt"
        _ = removed.write_text("remove in second tree\n", encoding="utf-8")
        _ = self.git("add", "deleted.txt")
        first = self.verify()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        initial = self.cargo_calls()[0]
        checkout = Path(initial["cwd"])
        self.assertTrue(checkout.is_relative_to(self.root / "slots"))
        self.assertTrue(checkout.exists())
        self.assertEqual(build_folders.build_folder_state(checkout), "free")
        self.assertEqual(initial["target"], str(self.root / "target"))
        del self.environment["TEST_DOC_STAMP"]
        self.environment["TEST_DOC_DELETED"] = "absent"
        _ = (self.repo / "alpha/src/lib.rs").write_text("pub fn next_tree() {}\n", encoding="utf-8")
        _ = self.git("add", "alpha/src/lib.rs")
        removed.unlink()
        _ = self.git("add", "-u", "--", "deleted.txt")
        second = self.verify()
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        current = self.cargo_calls()[1]
        self.assertEqual(current["cwd"], initial["cwd"])
        self.assertTrue(Path(current["cwd"]).exists())
        self.assertEqual(build_folders.build_folder_state(Path(current["cwd"])), "free")
        self.assertEqual(current["target"], initial["target"])
        self.assertEqual(times.read_text().splitlines(), [str(stamp), str(stamp)])
        self.assertEqual(current["sources"]["alpha/src/lib.rs"], "pub fn next_tree() {}\n")

    def test_doc_target_defaults_to_repo_metadata_and_reuses_cache(self) -> None:
        del self.environment["CARGO_TARGET_DIR"]
        for run in range(2):
            with self.subTest(run=run):
                result = self.verify()
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        first, second = self.cargo_calls()
        self.assertEqual(first["target"], str(self.root / "target"))
        self.assertEqual(second["target"], first["target"])
        self.assertEqual(second["cwd"], first["cwd"])
        self.assertTrue(all(Path(call["cwd"]).exists() for call in (first, second)))
        self.assertEqual({build_folders.build_folder_state(Path(call["cwd"])) for call in (first, second)}, {"free"})
        records = list((self.root / "slots").glob("*-slot*.lease.json"))
        self.assertEqual(len(records), 1)
        self.assertTrue((Path(first["cwd"]) / "target").is_dir())


if __name__ == "__main__":
    _ = unittest.main()
