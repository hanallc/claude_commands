#!/usr/bin/env python3
"""Exercise doc verification against real Git trees and fake Cargo."""
from __future__ import annotations

from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
from threading import Event
from typing import cast
import unittest
from unittest import mock

from scripts.build_queue import execution_identity
from scripts.build_queue import admission_protocol as wire, admission_service as service
from scripts.agents.remote import local_machine
from scripts.buildlog import treekey
from scripts.build_queue.admission_test_support import caller_admission
from scripts.direct.verify_doc_test_support import CARGO_STUB, VERIFY, VerifyDocFixture
from scripts.direct.verify_mac_offload_test_support import processes_carrying, reap
from scripts.direct.wait_for_test_state import wait_for_pid, wait_until
from scripts.lib import build_folders, build_slots
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.lint.memory_test_support import fixture_feed_code_settings, write_pressure_reading
from scripts.production import staff
from scripts.production.staff_test_support import build_fixture_sessions, fixture_member, isolated_staff_file


with fixture_feed_code_settings():
    from scripts.build_queue.memory_feed import FeedStaff


DOC_COMMAND = ("bash", str(VERIFY), "doc")


class VerifyDocCheckoutTests(VerifyDocFixture):
    def start_doc(self, *, command: tuple[str, ...] = DOC_COMMAND) -> tuple[subprocess.Popen[bytes], Path, str]:
        log = self.root / "verify.log"
        marker = f"TEST_DOC_CHECKOUT={self.root}"
        with log.open("wb") as output:
            process = subprocess.Popen(
                command, cwd=self.repo, stdout=output, stderr=output,
                env={**self.environment, "TEST_DOC_CHECKOUT": str(self.root)}, start_new_session=True,
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

    def test_doc_holds_no_checkout_while_waiting_for_the_cargo_token(self) -> None:
        with self.hold_cargo_token():
            process, log, _ = self.start_doc()
            wait_until(lambda: "waiting for the cargo token since" in log.read_text(),
                       self.root, processes=(process,), watch_writes=True)
            self.assertEqual([build_folders.build_folder_state(path)
                              for path in (self.root / "build").glob("*/*/[0-9]*/")], [])
            self.assertFalse(self.events.exists())
        self.assertEqual(process.wait(TEST_HANG_GUARD_SECS), 0, log.read_text())
        self.assertEqual(len(self.pass_records()), 1)

    def test_hold_during_the_cargo_token_wait_leaves_no_doc_checkout(self) -> None:
        admission = caller_admission(self, self.root, self.environment)
        waiting = Event()

        def observe_wait(reply: wire.Reply) -> wire.Reply:
            if isinstance(reply, wire.PlacementWait) and isinstance(reply.wait, wire.PlacementResourceWait):
                waiting.set()
            return reply

        holders = Path(self.environment["BUILD_HOLD_DIR"])
        holders.mkdir()
        holder = holders / "hana"
        with (self.hold_cargo_token(),
              mock.patch.object(admission.server, "reply_filter", side_effect=observe_wait)):
            process, log, _ = self.start_doc()
            wait_until(lambda: "waiting for the cargo token since" in log.read_text(),
                       self.root, processes=(process,), watch_writes=True)
            self.assertEqual(list((self.root / "build").glob("*/*/[0-9]*/")), [])
            _ = holder.write_text(json.dumps({
                "holder": "hana", "since": "2026-11-17T20:00:00+00:00", "for": "review", "release_eta": "unknown",
            }))
            try:
                self.assertTrue(waiting.wait(TEST_HANG_GUARD_SECS), log.read_text())
                self.assertEqual(list((self.root / "build").glob("*/*/[0-9]*/")), [])
                self.assertFalse(admission.caller_reservations())
                self.assertFalse(self.events.exists())
            finally:
                holder.unlink()
        self.assertEqual(process.wait(TEST_HANG_GUARD_SECS), 0, log.read_text())
        self.assertEqual(len(self.pass_records()), 1)

    def test_doc_admission_wait_names_the_unit_without_a_session(self) -> None:
        _ = self.enterContext(isolated_staff_file(self.root / "doc-staff.json", self.environment))
        showrunner = fixture_member("showrunner", name="builds")
        unit = fixture_member("unit", name="doc-proof", parent=showrunner.id,
                              worktree=staff.PresentWorktree(local_machine(), self.repo))
        _ = self.environment.pop("CLAUDE_CODE_SESSION_ID", None)
        _ = self.environment.pop("CODEX_THREAD_ID", None)
        self.assert_doc_wait_identity(showrunner, unit, session="")

    def test_doc_admission_wait_names_the_unit_from_an_unregistered_clone(self) -> None:
        registered = self.root / "registered" / self.repo.name
        registered.parent.mkdir()
        _ = self.repo.rename(registered)
        _ = subprocess.run(["git", "clone", "-q", "--", str(registered), str(self.repo)],
                           env=self.environment, check=True, timeout=TEST_HANG_GUARD_SECS)
        _ = self.enterContext(isolated_staff_file(self.root / "doc-staff.json", self.environment))
        showrunner = fixture_member("showrunner", name="builds")
        unit = fixture_member("unit", name="doc-proof", parent=showrunner.id,
                              worktree=staff.PresentWorktree(local_machine(), registered),
                              runtime_id=staff.caller_identity())
        self.environment.update(build_fixture_sessions(unit.current_session))
        self.assert_doc_wait_identity(showrunner, unit, session=unit.current_session)

    def assert_doc_wait_identity(self, showrunner: staff.StaffRow, unit: staff.StaffRow, *, session: str) -> None:
        admission = caller_admission(self, self.root, self.environment)
        waiting = Event()
        requests: list[wire.PlacementRequest] = []
        handle_request = admission.server.handle_request

        def observe_request(request: wire.Request, connection: service.Connection | service.NoConnection
                            = service.NO_CONNECTION) -> list[wire.Reply]:
            if isinstance(request, wire.PlacementRequest):
                requests.append(request)
            return handle_request(request, connection)

        def observe_wait(reply: wire.Reply) -> wire.Reply:
            if isinstance(reply, wire.PlacementWait) and isinstance(reply.wait, wire.Wait):
                waiting.set()
            return reply

        admission.hold_starts()
        with (mock.patch.object(admission.server, "reply_filter", side_effect=observe_wait),
              mock.patch.object(admission.server, "handle_request", side_effect=observe_request)):
            process, log, _ = self.start_doc()
            try:
                self.assertTrue(waiting.wait(TEST_HANG_GUARD_SECS), log.read_text())
                with admission.loop_turn():
                    request = requests[0]
                feed = FeedStaff(staff.StaffSnapshot((showrunner, unit)))
                identity = request.identity
                self.assertEqual(identity.session_id, session)
                self.assertEqual(identity.folder, str(self.repo))
                labels = feed.labels_for(identity.session_id, request.owner.host, Path(identity.folder))
                assert labels is not None
                self.assertEqual(labels.unit, unit.name)
            finally:
                write_pressure_reading(self.root / "pressure")
        self.assertEqual(process.wait(TEST_HANG_GUARD_SECS), 0, log.read_text())
    def test_remote_doc_refuses_a_tree_staged_during_checkout_preparation(self) -> None:
        self.enable_held_mac_doc()
        wrapper = self.root / "checkout-interpreter"
        staged = self.repo / "alpha/src/lib.rs"
        original_tree = self.git("write-tree")
        self.executable(wrapper, f'''import os
from pathlib import Path
import subprocess
import sys

command = [sys.executable, *sys.argv[1:]]
arguments = sys.argv[1:]
if not (len(arguments) >= 2 and arguments[0].endswith("/copies.py")
        and arguments[1] == "hold" and "--scratch" in arguments and "--" not in arguments):
    os.execv(sys.executable, command)
with subprocess.Popen(command, stdout=subprocess.PIPE, text=True) as copy:
    checkout = copy.stdout.readline()
    if not checkout:
        raise SystemExit(copy.wait() or 1)
    Path({str(staged)!r}).write_text("pub fn staged_during_checkout() {{}}\\n")
    subprocess.run(["git", "-C", {str(self.repo)!r}, "add", "--", {str(staged)!r}], check=True)
    print(checkout, end="", flush=True)
    raise SystemExit(copy.wait())
''')
        self.environment["CLAUDE_PYTHON"] = str(wrapper)
        result = self.verify()
        self.assertNotEqual(self.git("write-tree"), original_tree, result.stdout + result.stderr)
        self.assertFalse(self.events.exists(), "the remote runner dispatched the old tree")
        self.assertEqual(result.returncode, 77, result.stdout + result.stderr)
        self.assertIn("the staged tree changed", result.stderr)
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
                self.assertEqual(list((self.root / "build").glob("*")), [])
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
        self.assertTrue(checkout.is_relative_to(self.root / "build"))
        with mock.patch.dict(os.environ, self.environment, clear=True):
            key = treekey.tree_key(str(checkout))
        assert key is not None
        self.assertEqual(grant.assignment.source_key, execution_identity.SourceKeyKnown(key))
        rows = (cast(dict[str, object], json.loads(line))
                for path in (self.root / "buildlog").glob("*/*.jsonl")
                for line in path.read_text().splitlines())
        row, = (row for row in rows if row.get("kind") == "step" and row.get("step") == "doc")
        self.assertEqual(row["tree_key"], key)
        self.assertIs(row["tree_changed"], False)
        self.assertEqual(len(self.pass_records()), 1)

    def test_hold_after_grant_refuses_doc_without_taking_a_checkout(self) -> None:
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
        self.assertEqual(list((self.root / "build").glob("*/*/[0-9]*/")), [])

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
        with mock.patch.dict(os.environ, self.environment, clear=True), build_slots.take_copy(
                original, "HEAD", showrunner="builds", unit="doc-test", work="prepared copy", purpose="doc") as slot:
            self.repo: Path = slot.path
            self.environment["BUILDLOG_PREPARED_COPY"] = str(slot.path)
            self.environment["TEST_METADATA"] = self.environment["TEST_METADATA"].replace(str(original), str(slot.path))
            result = self.verify()
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(self.cargo_calls()[0]["cwd"], str(slot.path))
            self.assertEqual(build_folders.build_folder_state(slot.path), "held")
            self.assertEqual(list((self.root / "build").glob("*/*/*.lease.json")),
                             [build_slots.lease_path(slot.path)])

    def test_doc_takes_its_own_slot_when_the_prepared_copy_names_another_tree(self) -> None:
        self.environment["BUILDLOG_PREPARED_COPY"] = str(self.root / "another-copy")
        result = self.verify()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        checkout = Path(self.cargo_calls()[0]["cwd"])
        self.assertNotEqual(checkout, self.repo)
        self.assertTrue(checkout.is_relative_to(self.root / "build"))
        self.assertEqual(build_folders.build_folder_state(checkout), "free")
        self.assertEqual(len(list((self.root / "build").glob("*/*/*.lease.json"))), 1)

    def test_doc_with_an_old_copy_call_reports_that_verify_must_run_again(self) -> None:
        verify = self.root / "home/.claude/scripts/direct/verify.sh"
        source = verify.read_text(encoding="utf-8")
        current = r'''set -- "$PY" "$STEP_COPIES_SCRIPT" hold --repo "$VERIFY_SLOT_SOURCE" --tree "$VERIFY_SLOT_TREE" \
            --step "$CMD" -- "$@"'''
        legacy = 'set -- "$PY" "$STEP_COPIES_SCRIPT" hold --repo "$VERIFY_SLOT_SOURCE"'
        self.assertEqual(source.count(current), 1)
        _ = verify.write_text(source.replace(current, legacy), encoding="utf-8")

        process, log, _ = self.start_doc(command=("bash", str(verify), "doc", "alpha"))
        status = process.wait(TEST_HANG_GUARD_SECS)
        output = log.read_text(encoding="utf-8")
        self.assertIn("copies: hold requires --tree. Run verify.sh again.", output)
        self.assertNotEqual(status, 0, output)
        self.assertNotIn("the following arguments are required", output)
        self.assertFalse(self.events.exists())
        self.assertEqual(self.pass_records(), [])

        retry = self.verify("alpha")
        self.assertEqual(retry.returncode, 0, retry.stdout + retry.stderr)
        self.assertEqual(len(self.cargo_calls()), 1)
        self.assertEqual(len(self.pass_records()), 1)

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
        self.assertTrue(checkout.is_relative_to(self.root / "build"))
        self.assertTrue(checkout.exists())
        self.assertEqual(build_folders.build_folder_state(checkout), "free")
        self.assertEqual(initial["target"], str(checkout / "target"))
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

    def test_doc_without_target_override_reuses_the_copy_cache(self) -> None:
        del self.environment["CARGO_TARGET_DIR"]
        for run in range(2):
            with self.subTest(run=run):
                result = self.verify()
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        first, second = self.cargo_calls()
        self.assertEqual(first["target"], str(Path(first["cwd"]) / "target"))
        self.assertEqual(second["target"], first["target"])
        self.assertEqual(second["cwd"], first["cwd"])
        self.assertTrue(all(Path(call["cwd"]).exists() for call in (first, second)))
        self.assertEqual({build_folders.build_folder_state(Path(call["cwd"])) for call in (first, second)}, {"free"})
        records = list((self.root / "build").glob("*/*/*.lease.json"))
        self.assertEqual(len(records), 1)
        self.assertTrue((Path(first["cwd"]) / "target").is_dir())


if __name__ == "__main__":
    _ = unittest.main()
