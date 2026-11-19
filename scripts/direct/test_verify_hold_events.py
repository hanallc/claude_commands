"""Real verification hold waits: events."""

from __future__ import annotations

import ctypes
import errno
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from typing import cast
from unittest import mock

from scripts.direct.verify_hold_wait_test_support import (
    VerifyHoldWaitFixture,
    grant_call,
    local_hold_environment,
    read_cycle_file,
    release_registered_call,
)
from scripts.direct.wait_for_test_state import wait_until
from scripts.lib import process_supervisor
from scripts.lib.shell_test_support import shell_function
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


class VerifyHoldWaitTests(VerifyHoldWaitFixture):
    def test_hold_release_records_before_the_fixture_restores_its_environment(self) -> None:
        environments: tuple[tuple[str, dict[str, str]], ...] = (("ambient", {}), ("marked", self.fixture.environment))
        for count, (name, environment) in enumerate(environments, 1):
            with self.subTest(environment=name):
                self.hold()
                with local_hold_environment(environment):
                    grant_call(self.cycle_file)
                recorded: list[tuple[object, object]] = []
                for path in (self.fixture.root / "buildlog").rglob("*.jsonl"):
                    for line in path.read_text().splitlines():
                        row = cast(dict[str, object], json.loads(line))
                        if row.get("kind") == "hold_event":
                            recorded.append((row.get("event"), row.get("holder")))
                self.assertEqual(recorded, [("ended", "hana")] * count)

    def test_local_admission_waits_for_release_notifications_instead_of_sleeping(self) -> None:
        from scripts.build_targets import build_targets
        from scripts.direct.local_release_wait import LocalReleaseEvents

        self.hold()
        def grant_on_notification(_seconds: float) -> None:
            grant_call(self.cycle_file)

        with local_hold_environment(self.fixture.environment), \
             mock.patch.object(time, "sleep", side_effect=AssertionError("local admission must wake on release")), \
             mock.patch.object(LocalReleaseEvents, "wait", side_effect=grant_on_notification) as waiting:
            self.assertEqual(build_targets.command_wait_local("notification-call"), 0)
        waiting.assert_called_once_with(0.01)

    def test_local_admission_without_inotify_polls_until_release(self) -> None:
        from scripts.build_targets import build_targets

        self.hold()
        library = ctypes.CDLL
        caller = threading.get_ident()

        def unavailable_for_caller(name: str | None, *, use_errno: bool = False) -> object:
            if threading.get_ident() == caller:
                return object()
            return library(name, use_errno=use_errno)

        def release_on_poll(_seconds: float) -> None:
            grant_call(self.cycle_file)

        with local_hold_environment(self.fixture.environment):
            with mock.patch.object(ctypes, "CDLL", side_effect=unavailable_for_caller), \
                 mock.patch.object(time, "sleep", side_effect=release_on_poll) as polling:
                self.assertEqual(build_targets.command_wait_local("polling-call"), 0)
            polling.assert_called_once_with(0.01)
        self.assertEqual(read_cycle_file(self.cycle_file)["entries"], [])

    def test_local_release_waiter_keeps_linux_notifications(self) -> None:
        from scripts.direct import local_release_wait

        holders = self.fixture.root / "event-holders"
        release = self.fixture.root / "event-release"
        holders.mkdir()
        release.mkdir()
        with mock.patch.object(time, "sleep", side_effect=AssertionError("Linux must use notifications")), \
             local_release_wait.local_release_waiter(holders, release) as waiter:
            self.assertIsInstance(waiter, local_release_wait.LocalReleaseEvents)
            waiter.arm()
            _ = (holders / "owner").write_text("released")
            waiter.wait(TEST_HANG_GUARD_SECS)

    def test_local_release_waiter_polls_when_kernel_lacks_inotify(self) -> None:
        from scripts.direct import local_release_wait

        for unavailable in (errno.ENOSYS, errno.ENOTSUP):
            with self.subTest(errno=unavailable), \
                 mock.patch.object(local_release_wait, "LocalReleaseEvents", side_effect=OSError(unavailable, "unavailable")), \
                 mock.patch.object(time, "sleep") as polling:
                with local_release_wait.local_release_waiter(self.fixture.root, self.fixture.root) as waiter:
                    self.assertIsInstance(waiter, local_release_wait.IntervalReleasePolling)
                    waiter.arm()
                    waiter.wait(15)
                polling.assert_called_once_with(15)

    def test_local_release_waiter_does_not_hide_resource_errors(self) -> None:
        from scripts.direct import local_release_wait

        with mock.patch.object(local_release_wait, "LocalReleaseEvents", side_effect=OSError(errno.EMFILE, "exhausted")), \
             self.assertRaises(OSError) as failed:
            _ = local_release_wait.local_release_waiter(self.fixture.root, self.fixture.root)
        self.assertEqual(failed.exception.errno, errno.EMFILE)

    def test_fixture_releases_a_call_when_a_scanned_directory_disappears(self) -> None:
        from scripts.lib import inotify

        self.hold()
        vanished = self.fixture.root / "watch-race" / "nested" / "vanished"
        vanished.mkdir(parents=True)
        removed = threading.Event()
        add_watch = inotify.add_watch

        def remove_before_watch(descriptor: int, path: Path, mask: int = inotify.DIRECTORY_CHANGES) -> int:
            if path == vanished:
                vanished.rmdir()
                removed.set()
            return add_watch(descriptor, path, mask)

        with mock.patch.object(inotify, "add_watch", side_effect=remove_before_watch), \
             release_registered_call(self.cycle_file):
            result = self.fixture.verify("test", "sample", "--local", "directory removal", "--filter", "smoke")
        self.assertTrue(removed.is_set(), "The directory must disappear before the watch.")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(self.fixture.local_runs()), 1)


class PlacementWaitModeTests(unittest.TestCase):
    def test_only_the_first_empty_reply_of_an_episode_wakes_at_the_next_pass(self) -> None:
        source = (Path(__file__).resolve().parent / "verify.sh").read_text()
        with tempfile.TemporaryDirectory() as directory:
            calls = Path(directory) / "calls"
            script = "\n".join((
                "set -euo pipefail",
                'record() { printf \'%s\\n\' "$4 $5" >> "$CALLS"; }',
                "PY=record LOCAL_RELEASE_WAIT_SCRIPT=wait BUILDLOG_CALL_ID=call PHASE_RESOURCE_WAIT=''",
                "PLACEMENT_WAIT_MEMORY=0 PLACEMENT_EVIDENCE_EPISODE='' HOLD_WAITER_PID=''",
                "start_routing_wait() { :; }",
                "build_hold_nothing_held() { return 0; }",
                shell_function(source, "wait_for_placement"),
                "PLACEMENT_EPISODE=one; wait_for_placement; wait_for_placement; wait_for_placement",
                "PLACEMENT_EPISODE=two; wait_for_placement",
                "PLACEMENT_WAIT_MEMORY=1; PLACEMENT_EPISODE=three; wait_for_placement",
            ))
            result = subprocess.run(["bash", "-c", script], env={**os.environ, "CALLS": str(calls)},
                                    capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(calls.read_text().splitlines(), [
                "one evidence", "one resource", "one resource", "two evidence", "three resource"])

    def assert_missing_fixture_ends_wait(self, script: str) -> str:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = root / "fixture"
            fixture.touch()
            ready = root / "ready"
            calls = root / "calls"
            environment = {**os.environ, "FIXTURE": str(fixture),
                           "READY": str(ready), "CALLS": str(calls)}
            command = r"""
set -euo pipefail
exec 3<&0
wait_fixture() {
    if [[ -s "$CALLS" ]]; then
        printf 'repeat\n' >> "$CALLS"
        exit 74
    fi
    printf 'attempt\n' >> "$CALLS"
    : > "$READY"
    read -r -u 3
    [[ -e "$FIXTURE" ]] || return 73
}
"""
            with process_supervisor.launch(["bash", "-c", command + script], env=environment,
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as process:
                wait_until(ready.exists, root, processes=(process,))
                fixture.unlink()
                assert process.stdin is not None
                _ = process.stdin.write("\n")
                process.stdin.flush()
                output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
                self.assertEqual(process.returncode, 73, output + errors)
                self.assertEqual(calls.read_text().splitlines(), ["attempt"])
                return output

    def test_placement_wait_propagates_a_deleted_fixture_failure(self) -> None:
        source = Path(__file__).with_name("verify.sh").read_text()
        script = "\n".join((
            "PY=wait_fixture LOCAL_RELEASE_WAIT_SCRIPT=wait BUILDLOG_CALL_ID=call",
            "PHASE_RESOURCE_WAIT='' PLACEMENT_WAIT_MEMORY=0 PLACEMENT_EVIDENCE_EPISODE=''",
            "HOLD_WAITER_PID='' PLACEMENT_EPISODE=one STEP_TARGET_TABLE='' PINNED_TARGET=''",
            "release_token() { :; }",
            "check_routing_hold() { :; }",
            "placement_attempts=0",
            "placement_once() { (( ++placement_attempts == 1 )) && return 78; return 74; }",
            "start_routing_wait() { :; }",
            "build_hold_nothing_held() { return 0; }",
            shell_function(source, "wait_for_placement"),
            shell_function(source, "route_test"),
            "status=0; route_test || status=$?",
            'printf "waiter=%s\n" "$HOLD_WAITER_PID"; exit "$status"',
        ))
        self.assertEqual(self.assert_missing_fixture_ends_wait(script), "waiter=\n")

    def test_token_wait_propagates_a_deleted_pause_fixture_failure(self) -> None:
        source = Path(__file__).with_name("verify.sh").read_text()
        script = "\n".join((
            "TOKEN_USED=1 TOKEN_HELD=0 TOKEN_SUPERSEDED=0 TOKEN_WAIT_NOTE_MIN=5",
            "TOKEN_FIRST_WAIT_DONE=0 TOKEN_WAIT_HOLD_CHECK='' TOKEN_WAITER_FILE=$FIXTURE.waiter",
            "buildlog_check_nested_step() { :; }",
            "sync_token_state() { :; }",
            "token_wait_clock() { TOKEN_CLOCK_NOW=0; }",
            "count_token_waiters_ahead() { TOKEN_WAITERS_AHEAD=1; }",
            "phase_wait_start() { :; }",
            "phase_wait_finish() { :; }",
            "token_wait_note() { :; }",
            "sleep() { wait_fixture; }",
            shell_function(source, "token_wait_left"),
            shell_function(source, "acquire_token"),
            "status=0; acquire_token || status=$?",
            '[[ ! -e "$TOKEN_WAITER_FILE" ]] || exit 75',
            'exit "$status"',
        ))
        self.assertEqual(self.assert_missing_fixture_ends_wait(script), "")


class ReleaseDeadlineTests(unittest.TestCase):
    def test_wait_returns_by_its_deadline_when_irrelevant_events_never_stop(self) -> None:
        from scripts.direct.local_release_wait import LocalReleaseEvents

        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "watched"
            directory.mkdir()
            with LocalReleaseEvents(directory, directory) as waiter:
                waiter.arm()
                noise = directory / ".noise"
                noise.touch()
                batch = os.read(waiter.descriptor, 65536)
                _ = noise.write_text("noise")
                duration = 0.05
                deadline = time.monotonic() + duration
                limit = deadline + 5

                def continuous_events(descriptor: int, size: int) -> bytes:
                    del descriptor, size
                    if time.monotonic() >= limit:
                        raise AssertionError("The wait must return while events continue.")
                    return batch

                with mock.patch.object(os, "read", side_effect=continuous_events):
                    waiter.wait(duration)
                finished = time.monotonic()
                self.assertGreaterEqual(finished, deadline)
                self.assertLess(finished, limit)


class OpenLogWaitTests(unittest.TestCase):
    def test_shared_wait_reads_a_line_before_the_writer_closes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            log = directory / "output.log"
            log.touch()
            write = threading.Event()
            close = threading.Event()

            def writer() -> None:
                with log.open("a") as stream:
                    if write.wait(TEST_HANG_GUARD_SECS):
                        _ = stream.write("ready\n")
                        stream.flush()
                        _ = close.wait(TEST_HANG_GUARD_SECS)

            def ready() -> bool:
                found = "ready\n" in log.read_text()
                write.set()
                return found

            worker = threading.Thread(target=writer)
            worker.start()
            try:
                wait_until(ready, directory, watch_writes=True)
                self.assertIn("ready\n", log.read_text())
                self.assertTrue(worker.is_alive(), "The writer must keep the log open.")
            finally:
                close.set()
                worker.join(TEST_HANG_GUARD_SECS)


if __name__ == "__main__":
    _ = unittest.main()
