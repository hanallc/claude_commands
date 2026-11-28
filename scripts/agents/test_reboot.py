"""Test agents reboot with private state and simulated lifecycle operations."""
from __future__ import annotations

import fcntl
import io
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from datetime import timezone
from pathlib import Path
from threading import Event
from typing import cast, final, override
from unittest.mock import patch

from scripts.accounts import accounts
from scripts.agents import producer_lifecycle, producer_transfer, reboot, restart, shutdown, shutdown_record, stop_resume
from scripts.herdr import herdr
from scripts.maintenance import herdr_upkeep
from scripts.production import staff


class Terminal(io.StringIO):
    @override
    def isatty(self) -> bool:
        return True


@final
class RebootTests(unittest.TestCase):
    root: Path = Path()
    directory: Path = Path()
    start_lock: Path = Path()
    lib: Path = Path()
    events: list[str] = []
    report: restart.BulkReport | None = None
    save_stop: bool = True
    fail_stop: bool = False
    fail_server: bool = False
    interrupt_server: bool = False
    fail_start: bool = False
    plan_status: int = 0
    move_status: int = 0
    move_diagnostics: str = ""
    live_sessions: tuple[staff.StaffRow, ...] = ()
    stop_ids: tuple[str, ...] = ("alpha", "beta")

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.directory = self.root / "state/agents/reboot"
        self.events = []
        config = self.root / "config"
        config.mkdir()
        _ = (config / "shutdown.conf").write_text(
            "reboot_log_folder=reboot\n# range: 0.001..10\nreboot_spinner_seconds=0.1\n")
        _ = cast(object, self.enterContext(patch.dict(os.environ, {
            "CONF_DIR": str(config),
            "XDG_STATE_HOME": str(self.root / "state"),
            "XDG_CONFIG_HOME": str(config),
            "SHUTDOWN_STATE_DIR": str(self.root / "shutdown"),
            "HERDR_PANE_ID": "",
            "HERDR_ENV": "",
        })))
        self.start_lock = config / "herdr/sessions/agents/reboot.lock"
        _ = self.enterContext(patch.object(Path, "home", return_value=self.root))
        lib = self.root / ".claude/scripts/lib"
        lib.mkdir(parents=True)
        shim = lib / "py"
        _ = shim.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "$@"\n')
        shim.chmod(0o700)
        _ = (lib / "session_move.py").write_text("""\
import sys
from pathlib import Path
lib = Path(__file__).parent
if sys.argv[1:] == ['--dry-run']:
    print('session_move: would move 8 folders (1.75 GiB), kept 2 live, refused 0 other')
    sys.exit(int((lib / 'plan-status').read_text()))
else:
    print('copy 1024 bytes /scratch/alpha -> /sessions/alpha')
    print((lib / 'diagnostics').read_text(), file=sys.stderr, end='')
    code = int((lib / 'status').read_text())
    refused = int(code == 1 and 'pending' not in (lib / 'diagnostics').read_text())
    print(f'session_move: moved 1 folders (0.00 GiB), kept 2 live, refused {refused} other')
    sys.exit(code)
""")
        self.lib = lib
        self.capture_steps()

    def capture_steps(self) -> None:
        use_report = restart.use_report
        move_folders = reboot.move_folders

        @contextmanager
        def report(value: restart.BulkReport) -> Generator[None]:
            self.report = value
            with use_report(value):
                yield

        def move(log: reboot.RunLog, *, dry_run: bool = False) -> reboot.MoveResult:
            self.events.append("plan" if dry_run else "move")
            if not dry_run:
                self.check_upkeep(held=True)
            return move_folders(log, dry_run=dry_run)

        _ = self.enterContext(patch.object(restart, "use_report", side_effect=report))
        _ = self.enterContext(patch.object(restart, "stop_all", side_effect=self.stop))
        _ = self.enterContext(patch.object(restart, "live_rows", side_effect=self.check_sessions))
        _ = self.enterContext(patch.object(restart, "restart", side_effect=self.start))
        _ = self.enterContext(patch.object(reboot, "move_folders", side_effect=move))
        _ = self.enterContext(patch.object(herdr, "herdr_binary", return_value="herdr"))
        _ = self.enterContext(patch.object(herdr, "run_program", side_effect=self.server))
        _ = self.enterContext(patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot(())))

    def check_upkeep(self, *, held: bool) -> None:
        with herdr_upkeep.upkeep_lock(blocking=False) as directory:
            self.assertEqual(directory is None, held)

    def stop(self) -> int:
        self.events.append("stop")
        self.check_upkeep(held=True)
        assert self.report is not None
        self.report.targets.update({key: key for key in self.stop_ids})
        if self.save_stop:
            shutdown_record.update(started=[], pending=list(self.stop_ids))
        print("Stop detail from stdout")
        print("Stop detail from stderr", file=sys.stderr)
        for key in self.report.targets:
            if key == "beta" and self.fail_stop:
                self.report.failure(key, key, "The session still runs")
            else:
                self.report.finish(key)
        if not self.fail_stop:
            self.live_sessions = ()
        return 0

    def check_sessions(self, _record: staff.StaffSnapshot, _client: herdr.Herdr) -> restart.CompleteLiveRows:
        self.events.append("check")
        self.check_upkeep(held=True)
        return restart.CompleteLiveRows(list(self.live_sessions))

    def start(self) -> int:
        self.events.append("start")
        self.check_upkeep(held=True)
        assert self.report is not None
        for key in shutdown_record.read():
            self.report.targets[key] = key
        if self.fail_start:
            self.report.failure("beta", "beta", "The session failed to start")
            return 0
        for key in self.report.targets:
            self.report.finish(key)
        shutdown_record.save([])
        return 0

    def server(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        self.check_upkeep(held=True)
        if args[-2:] == ["status", "server"]:
            return subprocess.CompletedProcess(args, 0, "status: running\n", "")
        self.assertEqual(args, ["herdr", "--session", "agents", "server", "stop"])
        self.assertEqual(self.live_sessions, ())
        self.events.append("server")
        if self.interrupt_server:
            raise KeyboardInterrupt
        return subprocess.CompletedProcess(args, int(self.fail_server), "", "Server refused\n" if self.fail_server else "")

    def invoke(self, *arguments: str) -> tuple[int, list[str]]:
        _ = (self.lib / "plan-status").write_text(str(self.plan_status))
        _ = (self.lib / "status").write_text(str(self.move_status))
        _ = (self.lib / "diagnostics").write_text(self.move_diagnostics)
        with redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors:
            result = shutdown.main(["reboot", *arguments])
        self.assertEqual(errors.getvalue(), "")
        return result, output.getvalue().splitlines()

    def test_step_order_and_gate_require_a_saved_stop(self) -> None:
        self.save_stop = False
        code, lines = self.invoke()
        self.assertEqual(code, 1)
        self.assertEqual(self.events, ["plan", "stop"])
        self.assertIn("alpha: The restart record does not contain this session. Nate runs agents reboot again.", lines)
        self.assertIn("Nate has a step.", lines)

        self.save_stop = True
        self.fail_stop = True
        self.events.clear()
        code, lines = self.invoke()
        self.assertEqual(code, 1)
        self.assertEqual(self.events, ["plan", "stop"])
        self.assertIn("beta: The session still runs. Nate runs agents reboot again.", lines)

        self.fail_stop = False
        self.events.clear()
        code, lines = self.invoke()
        self.assertEqual(code, 0)
        self.assertEqual(self.events, ["plan", "stop", "check", "server", "move", "start"])
        self.assertEqual(lines[-2:], ["Nate has no step.", "Attach: herdr session attach agents"])

    def test_rerun_continues_at_the_failed_step(self) -> None:
        self.fail_server = True
        code, lines = self.invoke()
        self.assertEqual(code, 1)
        self.assertEqual(self.events, ["plan", "stop", "check", "server"])
        self.assertIn("alpha: The reboot stopped before it could restart this session. Nate runs agents reboot again.", lines)

        self.fail_server = False
        self.fail_start = True
        self.events.clear()
        code, lines = self.invoke()
        self.assertEqual(code, 1)
        self.assertEqual(self.events, ["check", "server", "move", "start"])
        self.assertIn("beta: The session failed to start. Nate runs agents reboot again.", lines)

        self.fail_start = False
        self.events.clear()
        self.assertEqual(self.invoke()[0], 0)
        self.assertEqual(self.events, ["start"])
        self.assertFalse((self.directory / "reboot.json").exists())

    def test_server_retry_saves_and_stops_a_session_resumed_between_runs(self) -> None:
        self.fail_server = True
        self.assertEqual(self.invoke()[0], 1)
        self.fail_server = False
        self.fail_start = True
        self.live_sessions = (staff.StaffRow(staff.StaffId("alpha"), "producer", "alpha", staff.StaffRoot(),
                                             current_session=staff.StaffId("alpha-native")),)
        self.stop_ids = ("alpha",)
        self.events.clear()
        self.assertEqual(self.invoke()[0], 1)
        self.assertEqual(self.events, ["check", "plan", "stop", "check", "server", "move", "start"])
        self.assertEqual(set(shutdown_record.read()), {"alpha", "beta"})

    def test_server_retry_refuses_start_commands_between_the_check_and_stop(self) -> None:
        self.directory.mkdir(parents=True)
        reboot.Checkpoint(reboot.Step.SERVER).save(self.directory / "reboot.json")
        shutdown_record.save(["alpha"])
        stop_server = reboot.stop_server
        refusal = "A reboot is running; wait for it to end."

        def server(log: reboot.RunLog) -> None:
            self.assertEqual(self.events, ["check"])
            commands = (["resume", "alpha"], ["resume", "--production", "alpha"],
                        ["resume", "alpha", "--resend-opening-prompt"],
                        ["restart"], ["restart", "--all"], ["restart", "--only", "alpha"],
                        ["add-producer", "alpha"], ["transfer", "alpha"], ["move", "alpha", "claude2"])
            with patch.object(os, "getpid", return_value=os.getpid() + 1):
                for arguments in commands:
                    with (self.subTest(arguments=arguments), redirect_stdout(io.StringIO()) as output,
                          redirect_stderr(io.StringIO()) as errors):
                        self.assertEqual(shutdown.main(arguments), 1)
                        self.assertEqual(output.getvalue(), "")
                        self.assertEqual(errors.getvalue().splitlines(), [refusal])
                self.assertEqual(stop_resume.resume("alpha"), stop_resume.ResumeFailed("alpha", refusal))
            stop_server(log)

        with (patch.object(reboot, "stop_server", side_effect=server),
              patch.object(shutdown, "_lifecycle", side_effect=AssertionError("Resume passed the lock")),
              patch.object(stop_resume, "_resume", side_effect=AssertionError("Resume passed the lock")),
              patch.object(stop_resume, "resend_producer_opening", side_effect=AssertionError("Resend passed the lock")),
              patch.object(producer_transfer, "transfer", side_effect=AssertionError("Transfer passed the lock")),
              patch.object(producer_lifecycle, "add", side_effect=AssertionError("Add passed the lock")),
              patch.object(accounts, "main", side_effect=AssertionError("Move passed the lock"))):
            self.assertEqual(self.invoke()[0], 0)
        self.assertEqual(self.events, ["check", "server", "move", "start"])
        self.assertEqual(shutdown_record.read(), [])

        def resume(_options: shutdown.CommandLine) -> int:
            with self.start_lock.open("a") as lock, self.assertRaises(BlockingIOError):
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return 0

        with patch.object(shutdown, "_lifecycle", side_effect=resume) as start:
            self.assertEqual(shutdown.main(["resume", "alpha"]), 0)
        start.assert_called_once()

    def test_log_folder_change_does_not_change_the_start_lock_or_checkpoint(self) -> None:
        config = self.root / "config" / "shutdown.conf"
        old_logs = self.directory.with_name("old-logs")
        new_logs = self.directory.with_name("new-logs")
        _ = config.write_text("reboot_log_folder=old-logs\n# range: 0.001..10\nreboot_spinner_seconds=0.1\n")
        old_logs.mkdir(parents=True)
        reboot.Checkpoint(reboot.Step.SERVER).save(old_logs / "reboot.json")
        shutdown_record.save(["alpha"])
        stop_server = reboot.stop_server

        def server(log: reboot.RunLog) -> None:
            self.assertTrue((self.directory / "reboot.json").is_file())
            self.assertFalse((old_logs / "reboot.json").exists())
            _ = config.write_text("reboot_log_folder=new-logs\n# range: 0.001..10\nreboot_spinner_seconds=0.1\n")
            with patch.object(os, "getpid", return_value=os.getpid() + 1), redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(shutdown.main(["resume", "alpha"]), 1)
            self.assertEqual(errors.getvalue(), "A reboot is running; wait for it to end.\n")
            stop_server(log)

        with (patch.object(reboot, "stop_server", side_effect=server),
              patch.object(shutdown, "_lifecycle", side_effect=AssertionError("Resume passed the lock"))):
            code, lines = self.invoke()
        self.assertEqual(code, 0)
        self.assertEqual(Path(lines[0]).parent, old_logs)
        self.assertEqual(self.events, ["check", "server", "move", "start"])
        self.assertTrue(self.start_lock.is_file())
        self.assertFalse((self.directory / "reboot.json").exists())
        self.assertFalse((old_logs / "lock").exists())
        self.assertFalse(new_logs.exists())
        code, lines = self.invoke("--dry-run")
        self.assertEqual(code, 0)
        self.assertEqual(Path(lines[0]).parent, new_logs)

    def test_reboot_start_launches_under_its_exclusive_lock(self) -> None:
        self.directory.mkdir(parents=True)
        reboot.Checkpoint(reboot.Step.START).save(self.directory / "reboot.json")
        shutdown_record.save(["alpha"])
        client = herdr.Herdr(herdr.SHARED_SESSION)

        def start_agent(*_args: object, **_kwargs: object) -> herdr.PromptSubmissionNotRequired:
            with self.start_lock.open("a") as external, self.assertRaises(BlockingIOError):
                fcntl.flock(external, fcntl.LOCK_SH | fcntl.LOCK_NB)
            return herdr.PromptSubmissionNotRequired()

        def restore() -> int:
            launched = herdr.launch_prepared_agent(client.session, herdr.NewTab("units", "alpha"),
                herdr.PreparedAgentEnvironment(self.root, {}), "codex", [], herdr.NoOpeningPrompt(),
                1000, agent="alpha", client=client)
            self.assertEqual(launched.pane, "fixture-pane")
            return self.start()

        with (patch.object(client, "open_tab", return_value="fixture-pane"),
              patch.object(client, "run_in_pane"),
              patch.object(herdr.Herdr, "_start_agent", side_effect=start_agent) as start,
              patch.object(restart, "restart", side_effect=restore)):
            self.assertEqual(self.invoke()[0], 0)
        start.assert_called_once()
        self.assertEqual(self.events, ["start"])
        self.assertEqual(shutdown_record.read(), [])

    def test_concurrent_workers_use_the_report_count_and_total(self) -> None:
        terminal = io.StringIO()
        display = reboot.StepDisplay(reboot.Step.START, terminal, reboot.RunLog(io.StringIO(), timezone.utc))
        reporting_first = Event()
        second_ready = Event()

        def member(name: str, count: int, total: int) -> None:
            if name == "alpha / first":
                reporting_first.set()
                self.assertTrue(second_ready.wait(5))
            display.member(name, count, total)

        alpha, beta = (staff.StaffRow(staff.StaffId(name), "unit", name, staff.StaffRoot(),
                                    current_session=staff.StaffId(f"{name}-native"))
                       for name in ("alpha", "beta"))
        first = staff.StaffRow(staff.StaffId("first"), "worker", "first", staff.ReportsTo(alpha.id),
                               current_session=staff.StaffId("first-native"))
        second = staff.StaffRow(staff.StaffId("second"), "worker", "second", staff.ReportsTo(beta.id),
                                current_session=staff.StaffId("second-native"))
        record = staff.StaffSnapshot((alpha, beta, first, second))
        report = restart.BulkReport(self.root / "fixture.log", "agents restart", "agents reboot",
                                    reboot=True, on_finish=member)
        for owner in (alpha, beta):
            report.track(owner, record)
        for owner in (alpha, beta):
            report.finish(owner.id)
        first_workers = restart.RestoredWorkers(report, record)
        second_workers = restart.RestoredWorkers(report, record)

        def restore_second() -> None:
            self.assertTrue(reporting_first.wait(5))
            second_ready.set()
            second_workers.add(second.id)

        with ThreadPoolExecutor(max_workers=2) as pool:
            first_job = pool.submit(first_workers.add, first.id)
            second_job = pool.submit(restore_second)
            first_job.result(timeout=5)
            second_job.result(timeout=5)
        self.assertEqual([line.split("  ")[2] for line in terminal.getvalue().splitlines()], [
            "1 of 2 back - alpha", "2 of 2 back - beta",
            "3 of 3 back - alpha / first", "4 of 4 back - beta / second"])

    def test_interrupted_server_stop_names_the_saved_sessions(self) -> None:
        self.interrupt_server = True
        code, lines = self.invoke()
        self.assertEqual(code, 1)
        self.assertEqual(self.events, ["plan", "stop", "check", "server"])
        for name in ("alpha", "beta"):
            self.assertIn(f"{name}: The reboot stopped before it could restart this session. "
                          + "Nate runs agents reboot again.", lines)
        self.assertIn("agents reboot: The reboot was interrupted. Nate runs agents reboot again.", lines)
        self.check_upkeep(held=False)

    def test_refuses_in_a_herdr_pane_on_one_line(self) -> None:
        with patch.dict(os.environ, {"HERDR_PANE_ID": "w1:p2"}):
            self.assertEqual(self.invoke(), (2, ["Run agents reboot in a terminal outside Herdr."]))
        self.assertEqual(self.events, [])
        self.assertFalse(self.directory.exists())

    def test_upkeep_lock_releases_after_a_failure(self) -> None:
        self.fail_server = True
        self.assertEqual(self.invoke()[0], 1)
        self.check_upkeep(held=False)

    def test_pipe_gets_one_progress_line_per_session(self) -> None:
        code, lines = self.invoke()
        self.assertEqual(code, 0)
        self.assertEqual([line.rsplit("  ", 1)[0] for line in lines[2:4]], [
            "1 of 4  Stop the sessions  1 of 2 stopped - alpha",
            "1 of 4  Stop the sessions  2 of 2 stopped - beta",
        ])
        self.assertEqual([line.rsplit("  ", 1)[0] for line in lines[6:8]], [
            "4 of 4  Restart the sessions  1 of 2 back - alpha",
            "4 of 4  Restart the sessions  2 of 2 back - beta",
        ])
        self.assertFalse(any("\r" in line or "\x1b" in line for line in lines))

    def test_terminal_replaces_each_step_line(self) -> None:
        _ = (self.lib / "plan-status").write_text("0")
        _ = (self.lib / "status").write_text("0")
        _ = (self.lib / "diagnostics").write_text("")
        with redirect_stdout(Terminal()) as output:
            self.assertEqual(shutdown.main(["reboot"]), 0)
        lines = output.getvalue().split("\n")
        self.assertEqual(len(lines), 9)
        self.assertRegex(lines[2], r"\r\x1b\[K1 of 4  Stop the sessions  \d+:\d\d \|")
        self.assertIn("\r\x1b[K1 of 4  Stop the sessions  1 of 2 stopped - alpha", lines[2])
        self.assertIn("\r\x1b[K4 of 4  Restart the sessions  2 of 2 back - beta", lines[5])

    def test_log_records_times_and_latest_points_to_the_new_run(self) -> None:
        _, first = self.invoke()
        _, second = self.invoke()
        log = Path(second[0])
        self.assertNotEqual(first[0], second[0])
        self.assertTrue(Path(first[0]).is_file())
        self.assertEqual(log.parent, self.directory)
        self.assertEqual((self.directory / "latest.log").resolve(), log)
        details = log.read_text().splitlines()
        self.assertEqual(details[0].split(" ", 1)[1], str(log))
        self.assertIn("Stop detail from stderr", log.read_text())
        self.assertIn("Stop detail from stdout", log.read_text())
        for line in details:
            self.assertRegex(line, r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}[+-]\d\d:\d\d ")

    def test_move_kept_lines_go_to_the_log_and_copy_write_failure_names_the_folder(self) -> None:
        self.move_status = 1
        self.move_diagnostics = (
            "session_move: kept /scratch/beta: Could not write the target\n"
            "session_move: copy write failed at /scratch/beta\n"
        )
        code, lines = self.invoke()
        self.assertEqual(code, 1)
        self.assertIn("start", self.events)
        self.assertTrue(lines[5].endswith("session_move: moved 1 folders (0.00 GiB), kept 2 live, refused 1 other"))
        self.assertFalse(any("kept /scratch/beta" in line for line in lines))
        self.assertIn("session_move: kept /scratch/beta: Could not write the target", Path(lines[0]).read_text())
        self.assertIn("session_move: copy write failed at /scratch/beta. 1 folder could not move. "
                      + "The verify owner checks the log.", lines)

    def test_move_live_copy_pending_is_reported_with_zero_refusals(self) -> None:
        self.move_status = 1
        self.move_diagnostics = "session_move: live copy pending at /scratch/beta\n"
        code, lines = self.invoke()
        self.assertEqual(code, 1)
        self.assertEqual(self.events[-2:], ["move", "start"])
        self.assertIn("session_move: live copy pending at /scratch/beta. The verify owner checks the log.", lines)

    def test_dry_run_shows_the_idle_size_and_live_folder_count(self) -> None:
        code, lines = self.invoke("--dry-run")
        self.assertEqual(code, 0)
        self.assertEqual(self.events, ["plan"])
        self.assertEqual(lines[1:], ["Plan: 1.75 GiB in idle folders now, 2 live folders sized after the stop."])
        self.assertFalse(shutdown_record.record_path().exists())
        self.assertFalse((self.directory / "reboot.json").exists())

    def test_dry_run_suppresses_incomplete_totals_and_fails(self) -> None:
        self.plan_status = 1
        code, lines = self.invoke("--dry-run")
        self.assertEqual(code, 1)
        self.assertEqual(self.events, ["plan"])
        self.assertEqual(lines[1:], ["Plan: Incomplete. See the session_move details in the log."])
        self.assertIn("1.75 GiB", Path(lines[0]).read_text())
        self.assertFalse((self.directory / "reboot.json").exists())
