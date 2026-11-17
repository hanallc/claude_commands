"""Real verify.sh token reservation behavior."""
from __future__ import annotations

from scripts.build_queue import admission_records

from scripts.build_queue.admission_test_support import AdmissionFixture, caller_admission
from scripts.direct.wait_for_test_state import wait_until
from scripts.build_queue import admission_protocol as wire
from scripts.build_queue import placement
from scripts.production.staff_test_support import fixture_runtime_id

import os
import shutil
import signal
import subprocess
import time
from dataclasses import replace
from typing import cast, override
import unittest
from pathlib import Path
from scripts.direct.verify_token_wait_test_support import VerifyTokenWaitFixture, VERIFY, BOARD
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


class VerifyTokenReservationTests(VerifyTokenWaitFixture):
    admission: AdmissionFixture = cast(AdmissionFixture, cast(object, None))
    @override
    def setUp(self) -> None:
        super().setUp()
        self.admission = caller_admission(self, self.root, self.environment)

    def test_a_granted_step_waiting_for_a_busy_token_keeps_its_reservation(self) -> None:
        board = self.root / "board"
        environment = {**self.environment, "UNIT_RUN_DIR": str(board), "UNIT_TEAM_ROLE": "impl"}
        output = self.root / "waiter.out"
        holder = subprocess.Popen(["bash", str(VERIFY), "check", "sample", "--no-cache"], cwd=self.root,
                                  env={**environment, "TEST_BLOCK": "1"},
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        self.started_groups.add(holder.pid)
        waiter: subprocess.Popen[bytes] | None = None
        try:
            self.wait_for_running(self.root / "running", holder)
            held = set(self.admission.server.reservations)
            self.assertEqual(len(held), 1, "the running step holds one reservation")
            with output.open("wb") as stream:
                waiter = subprocess.Popen(["bash", str(VERIFY), "check", "sample", "--no-cache"], cwd=self.root,
                                          env=environment, stdout=stream, stderr=subprocess.STDOUT,
                                          start_new_session=True)
            self.started_groups.add(waiter.pid)
            self.wait_for_output(output, "waiting for the cargo token since", waiter)
            with self.admission.loop_turn():
                self.assertEqual(self.admission.server.waiters, {})
                self.assertLess(held, set(self.admission.server.reservations))
                self.assertEqual(len(self.admission.server.reservations), 2)
            _ = (self.root / "release").touch()
            self.assertEqual(holder.wait(timeout=TEST_HANG_GUARD_SECS), 0)
            self.assertEqual(waiter.wait(timeout=TEST_HANG_GUARD_SECS), 0, output.read_text())
            self.assertEqual((self.root / "cargo-calls").read_text().count("check"), 2)
            self.assertEqual(self.admission.server.reservations, {})
            text = output.read_text(errors="replace")
            self.assertIn("waiting for the cargo token since", text)
            self.assertIn("took the cargo token after 0 min", text)
        finally:
            _ = (self.root / "release").touch()
            for process in (waiter, holder):
                if process is not None:
                    if process.poll() is None:
                        self.end_group(process.pid)
                    _ = process.wait(timeout=TEST_HANG_GUARD_SECS)

    def test_a_hold_during_the_token_wait_after_a_grant_gives_back_the_grant(self) -> None:
        board = self.root / "board"
        lock = board / "locks/cargo.d"
        hold = self.root / "build-hold" / fixture_runtime_id("fixture-hold-owner")
        hold.parent.mkdir(exist_ok=True)
        clock = self.root / "waiter-clock"
        _ = clock.write_text("1000\n")
        environment = {**self.environment, "UNIT_RUN_DIR": str(board), "UNIT_TEAM_ROLE": "impl",
                       "BUILD_TARGETS_POLL_S": "0.5"}
        original_probe = self.admission.server.host_probe

        # The natedev probe reads the hold, as the real probe does.
        def blocked_while_held(key: placement.HostEvidenceKey) -> placement.HostEvidence:
            evidence = original_probe(key)
            if (hold.exists() and key.destination.provider == "local"
                    and isinstance(evidence, placement.FreshHostEvidence)):
                return replace(evidence, eligibility=placement.TemporaryExclusion("blocked"))
            return evidence

        def publish_local(eligibility: placement.HostEligibility) -> None:
            with self.admission.loop_turn():
                for reader in self.admission.server.host_readers.values():
                    if (reader.key.destination.provider == "local"
                            and isinstance(reader.latest, placement.FreshHostEvidence)):
                        self.admission.server.publish_host_evidence(replace(reader.latest, eligibility=eligibility))

        self.admission.server.host_probe = blocked_while_held
        output = self.root / "waiter.out"
        holder = subprocess.Popen(["bash", str(VERIFY), "check", "sample", "--no-cache"], cwd=self.root,
                                  env={**environment, "TEST_BLOCK": "1"},
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        self.started_groups.add(holder.pid)
        waiter: subprocess.Popen[bytes] | None = None
        try:
            self.wait_for_running(self.root / "running", holder)
            held = set(self.admission.server.reservations)
            with output.open("wb") as stream:
                waiter = subprocess.Popen(["bash", str(VERIFY), "check", "sample", "--no-cache"], cwd=self.root,
                                          env={**environment, "UNIT_TOKEN_CLOCK_FILE": str(clock)},
                                          stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
            self.started_groups.add(waiter.pid)
            self.wait_for_output(output, "waiting for the cargo token since", waiter)
            _ = clock.write_text("1020\n")
            publish_local(placement.TemporaryExclusion("blocked"))
            _ = hold.write_text("private hold")
            self.wait_for_output(output, "natedev hold started during the cargo token wait", waiter)
            # The waiter gave back its grant and its queue entry, and it holds no token.
            with self.admission.loop_turn():
                self.assertEqual(set(self.admission.server.reservations), held)
            self.assertEqual([entry.name for entry in (board / "token_waiters").iterdir()
                              if entry.name.endswith(f".{waiter.pid}")], [])
            self.assertEqual((lock / "holder_pid").read_text(), str(holder.pid))
            hold.unlink()
            publish_local(placement.AllowedHost())
            _ = (self.root / "release").touch()
            self.assertEqual(holder.wait(timeout=TEST_HANG_GUARD_SECS), 0)
            self.assertEqual(waiter.wait(timeout=TEST_HANG_GUARD_SECS), 0, output.read_text())
            self.assertEqual((self.root / "cargo-calls").read_text().count("check"), 2)
            self.assertEqual(self.admission.server.reservations, {})
            # The token wait before the hold counts in the call's record.
            waited = [record["token_wait_s"] for record in self.records() if f"-{waiter.pid}-" in str(record["id"])]
            self.assertEqual(waited, [20])
        finally:
            hold.unlink(missing_ok=True)
            _ = (self.root / "release").touch()
            for process in (waiter, holder):
                if process is not None:
                    if process.poll() is None:
                        self.end_group(process.pid)
                    _ = process.wait(timeout=TEST_HANG_GUARD_SECS)

    def test_a_token_wait_ends_with_its_call(self) -> None:
        board = self.root / "board"
        waiting = self.root / "waiting-for-token"
        real_sleep = shutil.which("sleep")
        if real_sleep is None:
            self.fail("the token wait test needs sleep on PATH")
        peer = subprocess.Popen([real_sleep, "600"], start_new_session=True)
        self.started_groups.add(peer.pid)
        self.addCleanup(peer.wait, TEST_HANG_GUARD_SECS)
        taken = subprocess.run(["bash", str(BOARD), "acquire", str(board), "peer", "cargo", "--pid", str(peer.pid),
                                "--hold", "3600", "--wait", "0"],
                               capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(taken.returncode, 0, taken.stderr)
        shell = self.root / "local-attempt.sh"
        _ = shell.write_text('''set -T
probe_local_attempt() {
    if [[ "$BASH_COMMAND" == 'sleep "$pause"' ]]; then : > "$TEST_WAITING"; fi
}
trap probe_local_attempt DEBUG
''')
        environment = {**self.environment, "UNIT_RUN_DIR": str(board), "UNIT_TEAM_ROLE": "impl",
                       "BASH_ENV": str(shell), "TEST_WAITING": str(waiting)}
        output = self.root / "waiter.out"
        with output.open("wb") as stream:
            call = subprocess.Popen(["bash", str(VERIFY), "check", "sample", "--no-cache"], cwd=self.root,
                                    env=environment, stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
        self.started_groups.add(call.pid)
        self.wait_for_running(waiting, call)
        os.kill(call.pid, signal.SIGKILL)
        _ = call.wait(timeout=TEST_HANG_GUARD_SECS)
        deadline = time.monotonic() + TEST_HANG_GUARD_SECS
        while (left := self.live_group_processes(call.pid)) and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertEqual(left, [], output.read_text(errors="replace"))
        self.assertFalse((self.root / "cargo-calls").exists())

    def test_a_memory_waiter_holds_no_token_and_takes_it_after_the_grant(self) -> None:
        # A step waits for memory without its unit's cargo token, so the unit's
        # other calls keep building. The board wrapper logs each token acquire
        # next to the memory waits; the reply filter checks the lock at each wait.
        home = self.root / "busy-home"
        delegate = home / ".claude/scripts/direct"
        delegate.mkdir(parents=True)
        for source in VERIFY.parent.parent.iterdir():
            if source.name != "direct":
                (delegate.parent / source.name).symlink_to(source, target_is_directory=source.is_dir())
        for source in VERIFY.parent.iterdir():
            if source.name != "board.sh":
                (delegate / source.name).symlink_to(source, target_is_directory=source.is_dir())
        _ = (delegate / "board.sh").write_text(
            '#!/bin/bash\n'
            + 'if [[ "$1" == acquire ]]; then printf "token\\n" >> "$TEST_SLEEP_LOG"; fi\n'
            + 'exec bash "$TEST_REAL_BOARD" "$@"\n')
        self.write_wait_sleep('printf "sleep %s\\n" "$*" >> "$TEST_SLEEP_LOG"\n'
                             + 'printf "MemAvailable: 67108864 kB\\n" > "$BUILDLOG_MEMINFO"\n'
                             + 'exit 0\n')
        _ = self.admission.occupy_host()
        _ = Path(self.environment["BUILDLOG_MEMINFO"]).write_text("MemAvailable: 0 kB\n")
        log = self.root / "sleep-log"
        lock = self.root / "board/locks/cargo.d"
        environment = {**self.environment, "HOME": str(home),
                       "UNIT_RUN_DIR": str(self.root / "board"),
                       "UNIT_TEAM_ROLE": "impl", "TEST_REAL_BOARD": str(BOARD),
                       "TEST_SLEEP_LOG": str(log),
                       "BUILDLOG_MEM_POLL_S": "1", "BUILDLOG_MEM_WAIT_LIMIT_S": "2"}
        def free_after_wait(reply: wire.Reply) -> wire.Reply:
            if isinstance(reply, wire.Wait) or (isinstance(reply, wire.PlacementWait) and isinstance(reply.wait, wire.Wait)):
                with log.open("a") as stream:
                    _ = stream.write("wait held\n" if lock.exists() else "wait\n")
                self.admission.free(60 * admission_records.GIB)
            return reply

        self.admission.server.reply_filter = free_after_wait
        result = self.verify(environment)
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output)
        self.assertNotIn("memory wait limit reached", output)
        self.assertNotIn("starting anyway", output)
        lines = log.read_text().splitlines()
        self.assertIn("token", lines)
        self.assertNotIn("wait held", lines, "a memory waiter holds no cargo token")
        first_token = lines.index("token")
        self.assertIn("wait", lines[:first_token], "the step waits for memory before it takes the token")
        self.assertNotIn("wait", lines[first_token:], "the step takes the token after its grant")

    def test_old_two_pass_client_does_not_block_new_token_holder(self) -> None:
        board = self.root / "board"
        old_shell = self.root / "old-client.sh"
        _ = old_shell.write_text('''set -e
source "$TEST_MEMORY_GATE"
buildlog_wait_for_memory cargo check -p sample
: > "$TEST_OLD_READY"
python3 "$TEST_WAIT_HELPER" "$TEST_OLD_RELEASE"
buildlog_memory_python "$BUILDLOG_MEM_ADMIT_SCRIPT" token-wait "$BUILDLOG_MEM_RESERVATION"
BUILDLOG_MEM_RESERVATION=""
: > "$TEST_OLD_GAP"
bash "$TEST_BOARD" acquire "$TEST_BOARD_DIR" old cargo --pid $$ --hold 3600 --wait 30
trap 'bash "$TEST_BOARD" release "$TEST_BOARD_DIR" old cargo --pid $$' EXIT
BUILDLOG_MEM_TOKEN_BACK_AT=$(python3 -c 'import time; print(time.time())')
export BUILDLOG_MEM_TOKEN_BACK_AT
BUILDLOG_MEM_WAIT_SINCE=$BUILDLOG_MEM_WAIT_FROM buildlog_wait_for_memory cargo check -p sample
buildlog_release_memory 0
''')
        ready = self.root / "old-ready"
        gap = self.root / "old-gap"
        release = self.root / "old-release"
        old_errors = self.root / "old.err"
        with old_errors.open("wb") as stream:
            old = subprocess.Popen(["bash", str(old_shell)], cwd=self.root, env={**self.environment,
                "TEST_MEMORY_GATE": str(VERIFY.parents[1] / "lint/memory_gate.sh"),
                "TEST_BOARD": str(BOARD), "TEST_BOARD_DIR": str(board),
                "TEST_OLD_READY": str(ready), "TEST_OLD_GAP": str(gap), "TEST_OLD_RELEASE": str(release)},
                stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
        self.started_groups.add(old.pid)
        self.wait_for_running(ready, old)
        with self.admission.loop_turn():
            self.assertEqual(len(self.admission.server.reservations), 1)
            self.admission.free(20 * admission_records.GIB)
        new_errors = self.root / "new.err"
        new = self.start_memory_waiter({**self.environment, "UNIT_RUN_DIR": str(board),
            "UNIT_TEAM_ROLE": "new", "TEST_BLOCK": "1"}, new_errors)
        _ = release.touch()
        self.wait_for_running(gap, old)
        self.wait_for_running(self.root / "running", new)
        self.assertEqual((board / "locks/cargo.d/holder_pid").read_text(), str(new.pid))
        _ = (self.root / "release").touch()
        self.assertEqual(new.wait(timeout=TEST_HANG_GUARD_SECS), 0, new_errors.read_text())
        self.assertEqual(old.wait(timeout=TEST_HANG_GUARD_SECS), 0, old_errors.read_text())
        wait_until(self.admission.callers_released, self.admission.state_dir)
        self.assertEqual(self.admission.server.waiters, {})
        rows = [line for line in (self.admission.state_dir / "queue_times.jsonl").read_text().splitlines()
                if '"token_gap"' in line]
        self.assertEqual(len(rows), 1)



if __name__ == "__main__":
    _ = unittest.main()
