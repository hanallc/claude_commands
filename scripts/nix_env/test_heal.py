"""Healing decisions with isolated journals and injected process observations."""
from __future__ import annotations

from collections.abc import Callable
from contextlib import ExitStack, nullcontext, redirect_stderr, redirect_stdout
from dataclasses import replace
import errno
import fcntl
import io
import json
import os
from pathlib import Path
import shlex
import socket
import subprocess
import sys
import time
from typing import TextIO, cast, override
from unittest.mock import Mock, call, patch

from scripts.lib import conf, nix_env
from scripts.lib.nix_env_test_support import NixFixture
from scripts.lint.memory_test_support import write_config_fixture
from scripts.nix_env import heal
from scripts.agents import stop_resume
from scripts.herdr import herdr
from scripts.notify import push_texts
from scripts.message import sessions
from scripts.production import staff
from scripts.production.staff_test_support import fixture_runtime_id
from scripts.whoami import account


class HealTests(NixFixture):
    session: heal.HealSession = heal.HealSession("fixture", 811, "start-811", "worker", Path(), "idle", Path())
    staff_file: Path = Path()

    @override
    def setUp(self) -> None:
        super().setUp()
        _ = self.envrc(self.work)
        self.session = heal.HealSession("fixture", 811, "start-811", "worker", self.work, "idle", self.home / ".claude3")
        _ = self.enterContext(patch.object(heal, "state_directory", return_value=self.root / "journal"))
        self.staff_file = self.root / "staff.json"
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"STAFF_FILE": str(self.staff_file)})))
        self.write_staff()

    def write_staff(self, *rows: staff.StaffRow) -> None:
        _ = self.staff_file.write_text(json.dumps({"schema": 2, "rows": [row.to_json() for row in rows]}))

    def showrunner_fixture(self) -> tuple[staff.StaffRow, staff.StaffRow, staff.StaffRow]:
        root_id, owner_id = (staff.StaffId(fixture_runtime_id(name)) for name in ("root", "owner"))
        root = staff.StaffRow(root_id, "showrunner", "root", staff.StaffRoot(), current_session=root_id)
        owner = staff.StaffRow(owner_id, "showrunner", "owner", staff.ReportsTo(root.id), current_session=owner_id)
        unit = staff.StaffRow(staff.StaffId(fixture_runtime_id("unit")), "unit", "unit", staff.ReportsTo(owner.id),
                              current_session=staff.StaffId(fixture_runtime_id("unit-session")),
                              unit_type=staff.UnitDetails("direct"), home=staff.SessionHome(self.session.config_dir))
        worker = staff.StaffRow(staff.StaffId(fixture_runtime_id("worker")), "worker", "worker", staff.ReportsTo(unit.id),
                                current_session=staff.StaffId(fixture_runtime_id("worker-session")))
        self.write_staff(root, owner, unit, worker)
        return owner, unit, worker

    def records(self) -> list[dict[str, object]]:
        path = heal.state_directory() / "log.jsonl"
        return [cast(dict[str, object], json.loads(line)) for line in path.read_text().splitlines()] if path.exists() else []

    def test_a_sweep_keeps_its_state_log_and_lock_in_the_selected_folder(self) -> None:
        settings = Path(self.env["CONF_DIR"]) / "nix_env.conf"
        first, second = self.root / "first", self.root / "second"
        second.mkdir()
        _ = write_config_fixture(settings, f"heal_state_directory={first}\n")
        real_lock = fcntl.flock
        locked_paths: list[Path] = []

        def lock(stream: TextIO, operation: int) -> None:
            real_lock(stream, operation)
            locked_paths.append(Path(cast(str, stream.name)))
            _ = write_config_fixture(settings, f"heal_state_directory={second}\n")

        def configured_directory() -> Path:
            return Path(conf.read("nix_env").text("heal_state_directory"))

        with (patch.object(heal, "state_directory", side_effect=configured_directory),
              patch.object(fcntl, "flock", side_effect=lock),
              patch.object(heal, "list_sessions", return_value=heal.HealSessionRead((self.session,))),
              patch.object(heal, "inspect", return_value=heal.Unavailable("fixture metadata"))):
            self.assertEqual(heal.sweep(), 0)
            self.assertEqual(locked_paths, [first / "state.lock"])
            self.assertIn(self.session.session_id, heal.read_history(first))
            self.assertIn("fixture metadata", (first / "log.jsonl").read_text())
            self.assertTrue((first / "state.lock").is_file())
            self.assertEqual(list(second.iterdir()), [])
            self.assertEqual(heal.sweep(), 0)
            self.assertEqual(locked_paths, [first / "state.lock", second / "state.lock"])
            self.assertIn(self.session.session_id, heal.read_history(second))
            self.assertTrue((second / "log.jsonl").is_file())

    def test_the_command_timeout_changes_the_pending_relaunch_budget(self) -> None:
        directory = heal.state_directory()
        directory.mkdir()
        history = heal.new_history()
        history["restart_at"] = 100.0
        history["pending"] = heal.PendingRestart(kind="launching", login="fixture", name=self.session.name,
            cwd=str(self.session.cwd), config_dir=str(self.session.config_dir), pid=self.session.pid,
            proc_start=self.session.proc_start, stop_request_sent=True)
        heal.save_history(directory, {self.session.session_id: history})
        settings = Path(self.env["CONF_DIR"]) / "nix_env.conf"
        _ = write_config_fixture(settings,
            "command_timeout_seconds=550\nrelaunch_launch_seconds=25\nrelaunch_confirmation_seconds=15\n")
        with (patch.object(heal, "list_sessions", return_value=heal.HealSessionRead(())),
              patch.object(time, "time", return_value=1100.0), patch.object(heal, "alert") as alert):
            self.assertEqual(heal.sweep(), 0)
            alert.assert_not_called()
            _ = write_config_fixture(settings, "command_timeout_seconds=1\n")
            self.assertEqual(heal.sweep(), 0)
            alert.assert_called_once()
            self.assertIn("waiting for a live replacement", cast(str, alert.call_args.args[-1]))

    def test_later_envrc_approval_does_not_repair_the_live_process(self) -> None:
        # The session began before approval. Only a restart changes its own env.
        _ = nix_env.ensure(self.work, self.env)
        real_stat = Path.stat
        real_read = Path.read_bytes
        process = Path(f"/proc/{self.session.pid}")

        def stat(path: Path, *, follow_symlinks: bool = True) -> os.stat_result:
            return real_stat(self.work if path == process else path, follow_symlinks=follow_symlinks)

        def read(path: Path) -> bytes:
            return b"DIRENV_DIR=/fixture\0PATH=/bin\0" if path == process / "environ" else real_read(path)

        with (patch.object(sys, "platform", "linux"),
              patch.object(sessions, "process_start", return_value=self.session.proc_start),
              patch.object(Path, "stat", stat), patch.object(Path, "read_bytes", read)):
            self.assertTrue(heal.broken(self.session))
            with patch.object(Path, "read_bytes", return_value=b"IN_NIX_SHELL=impure\0"):
                self.assertFalse(heal.broken(self.session))

    def test_reused_pid_never_counts_as_broken(self) -> None:
        with (patch.object(sessions, "process_start", return_value="different-start"),
              patch.object(Path, "read_bytes", side_effect=AssertionError("reused pid must not be inspected"))):
            self.assertFalse(heal.broken(self.session))
            self.assertIsInstance(heal.inspect(self.session), heal.Gone)

    def test_no_envrc_is_healthy_without_process_or_direnv_probe(self) -> None:
        (self.work / ".envrc").unlink()
        with patch.object(sessions, "process_start", side_effect=AssertionError("no probe needed")):
            self.assertFalse(heal.broken(self.session))
        self.assertEqual(self.direnv_calls(), [])

    def test_registry_reads_all_config_dirs_and_retains_unreadable_paths(self) -> None:
        _ = write_config_fixture(Path(self.env["CONF_DIR"]) / "nix_env.conf",
                                 "session_directory_glob=.claude*/sessions\n")
        for number, name in enumerate((".claude", ".claude3"), 1):
            directory = self.home / name / "sessions"
            directory.mkdir(parents=True)
            record = {"pid": 800 + number, "procStart": "fixture-start", "sessionId": f"sid-{number}",
                      "name": f"worker-{number}", "messagingSocketPath": "", "updatedAt": 1,
                      "cwd": str(self.work), "status": "idle", "kind": "interactive", "entrypoint": "cli"}
            _ = (directory / f"{800 + number}.json").write_text(json.dumps(record))
            _ = (directory / "corrupt.json").write_text("{half written")
        with patch.object(sessions, "process_start", return_value="fixture-start"):
            found = heal.list_sessions()
        self.assertEqual({item.session_id for item in found.sessions}, {"sid-1", "sid-2"})
        self.assertEqual({item.config_dir.name for item in found.sessions}, {".claude", ".claude3"})
        unreadable = tuple(self.home / name / "sessions/corrupt.json" for name in (".claude", ".claude3"))
        self.assertEqual(found.unreadable, unreadable)
        with patch.object(sessions, "process_start", return_value="reused"):
            self.assertEqual(heal.list_sessions(), heal.HealSessionRead((), unreadable))
        self.assertNotIn("NOTIFIER_SESSIONS_DIR", os.environ)

    def test_mac_process_arguments_cannot_pose_as_nix_environment(self) -> None:
        arguments = int(3).to_bytes(4, sys.byteorder, signed=True) + b"/bin/claude\0\0claude\0-p\0IN_NIX_SHELL=spoofed prompt\0"
        with (patch.object(sys, "platform", "darwin"),
              patch.object(sessions, "process_start", return_value=self.session.proc_start),
              patch.object(account, "kern_procargs2", return_value=arguments + b"HOME=/fixture\0") as kernel):
            self.assertTrue(heal.broken(self.session))
            kernel.return_value = arguments + b"IN_NIX_SHELL=impure\0"
            self.assertFalse(heal.broken(self.session))

    def install_sweep(self, selected: list[heal.HealSession]) -> tuple[Mock, Mock, Mock]:
        registry = list(selected)
        healthy: set[tuple[str, int]] = set()

        def observation(session: heal.HealSession) -> heal.Healthy | heal.Broken:
            return heal.Healthy() if (session.session_id, session.pid) in healthy else heal.Broken()

        _ = self.enterContext(patch.object(heal, "list_sessions", side_effect=lambda: heal.HealSessionRead(tuple(registry))))
        _ = self.enterContext(patch.object(heal, "inspect", side_effect=observation))
        held = cast(Mock, self.enterContext(patch.object(heal, "hold_reason", return_value="")))
        ensure = cast(Mock, self.enterContext(patch.object(nix_env, "ensure", return_value=nix_env.Loaded({"IN_NIX_SHELL": "impure"}, self.work / ".envrc"))))
        _ = self.enterContext(patch.object(account, "claude_account", return_value=account.Account("claude", "test-account", "claude3")))
        _ = self.enterContext(patch.object(heal, "stop_completion", return_value=heal.StopComplete()))

        def command_result(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            del kwargs
            if command[2] == "resume":
                session_id = command[command.index("--only") + 1]
                original = next(item for item in selected if item.session_id == session_id)
                replacement = replace(original, pid=original.pid + 10000, proc_start="restarted-" + original.proc_start)
                registry[:] = [item for item in registry if item.session_id != session_id] + [replacement]
                healthy.add((replacement.session_id, replacement.pid))
            return subprocess.CompletedProcess(command, 0, "SENT: fixture", "")

        run = cast(Mock, self.enterContext(patch.object(subprocess, "run", side_effect=command_result)))
        def stop(identity: str) -> stop_resume.Stopped | stop_resume.StopFailed:
            result = cast(subprocess.CompletedProcess[str], run(["shared-api", "stop_resume", "stop", "test-account", "--here", "--only", identity]))
            return stop_resume.Stopped(identity, self.session.pid) if result.returncode == 0 else stop_resume.StopFailed(identity, result.stderr)
        def resume(identity: str, *, why: str) -> stop_resume.ClaudeUp | stop_resume.ResumeFailed:
            self.assertEqual(why, "nix shell heal")
            result = cast(subprocess.CompletedProcess[str], run(["shared-api", "stop_resume", "resume", "test-account", "--only", identity, "--here"]))
            return stop_resume.ClaudeUp(identity, "fixture-pane", "claude3") if result.returncode == 0 else stop_resume.ResumeFailed(identity, result.stderr)
        _ = self.enterContext(patch.object(stop_resume, "stop", side_effect=stop))
        _ = self.enterContext(patch.object(stop_resume, "resume", side_effect=resume))
        return held, ensure, run

    def test_idle_session_restarts_once_and_logs_the_restart(self) -> None:
        held, ensure, run = self.install_sweep([self.session])
        self.assertEqual(heal.sweep(), 0)
        self.assertEqual(ensure.call_args_list, [call(self.work), call(self.work)])
        self.assertGreaterEqual(held.call_count, 2)
        command = cast(list[str], run.call_args.args[0])
        self.assertEqual(command[2:], ["resume", "test-account", "--only", self.session.session_id, "--here"])
        stop = cast(list[str], run.call_args_list[0].args[0])
        self.assertEqual(stop[2:], ["stop", "test-account", "--here", "--only", self.session.session_id])
        self.assertEqual(command[1], "stop_resume")
        entries = self.records()
        self.assertEqual([entry["action"] for entry in entries], ["restart"])
        self.assertEqual(entries[0]["session_id"], self.session.session_id)
        self.assertEqual(entries[0]["cwd"], str(self.work))
        self.assertTrue(all(key in entries[0] for key in ("time", "machine", "name", "reason")))
        self.assertEqual(heal.sweep(), 0)
        self.assertEqual(run.call_count, 2)

    def test_rerun_sends_stop_saved_before_an_interrupted_down_call(self) -> None:
        held, ensure, run = self.install_sweep([self.session])
        held.return_value = ""
        complete = cast(Callable[..., subprocess.CompletedProcess[str]], run.side_effect)
        with (patch.object(heal, "stop_completion", return_value=heal.StopPending()) as completion,
              patch.object(sessions, "process_start", return_value=self.session.proc_start)):
            run.side_effect = KeyboardInterrupt("fixture stopped before down ran")
            with self.assertRaises(KeyboardInterrupt):
                _ = heal.sweep()
            pending = heal.read_history(heal.state_directory())[self.session.session_id]["pending"]
            self.assertEqual(pending["kind"], "stopping")
            if pending["kind"] == "none":
                self.fail("interrupted stop must keep its reservation")
            self.assertFalse(pending["stop_request_sent"])
            interrupted_call = run.call_args
            run.reset_mock()
            run.side_effect = complete
            self.assertEqual(heal.sweep(), 0)
            run.assert_called_once_with(*interrupted_call.args, **interrupted_call.kwargs)
            pending = heal.read_history(heal.state_directory())[self.session.session_id]["pending"]
            if pending["kind"] == "none":
                self.fail("accepted stop must wait for the conductor")
            self.assertTrue(pending["stop_request_sent"])
            self.assertEqual(heal.sweep(), 0)
            self.assertEqual(run.call_count, 1)
            completion.return_value = heal.StopComplete()
            self.assertEqual(heal.sweep(), 0)
        self.assertEqual(heal.read_history(heal.state_directory())[self.session.session_id]["pending"]["kind"], "none")
        self.assertEqual(ensure.call_count, 2)
        commands = [cast(list[str], item.args[0]) for item in run.call_args_list]
        self.assertEqual([command[2] for command in commands], ["stop", "resume"])

    def test_rerun_clears_unsent_stop_and_alerts_if_the_saved_process_is_gone(self) -> None:
        for identity in (None, "reused-pid-start"):
            with self.subTest(identity=identity), patch.object(heal, "state_directory", return_value=self.root / str(identity)):
                held, ensure, run = self.install_sweep([self.session])
                held.return_value = ""
                complete = cast(Callable[..., subprocess.CompletedProcess[str]], run.side_effect)
                run.side_effect = KeyboardInterrupt("fixture stopped before down ran")
                with self.assertRaises(KeyboardInterrupt):
                    _ = heal.sweep()
                run.reset_mock()
                run.side_effect = complete
                with (patch.object(heal, "list_sessions", return_value=heal.HealSessionRead(())),
                      patch.object(heal, "stop_completion", return_value=heal.StopPending()),
                      patch.object(sessions, "process_start", return_value=identity)):
                    self.assertEqual(heal.sweep(), 0)
                    self.assertEqual(heal.sweep(), 0)
                self.assertEqual(heal.read_history(heal.state_directory())[self.session.session_id]["pending"], heal.NoPendingRestart(kind="none"))
                run.assert_called_once()
                command = cast(list[str], run.call_args.args[0])
                self.assertIn("--need", command)
                self.assertIn("nix-heal-fixture", command)
                self.assertNotIn("stop", command)
                self.assertNotIn("resume", command)
                self.assertEqual(ensure.call_count, 1)
                self.assertIn("unsent shutdown request lost its original session process", cast(str, self.records()[-1]["reason"]))

    def test_sent_stop_with_no_record_keeps_waiting_and_legacy_records_are_sent(self) -> None:
        held, ensure, run = self.install_sweep([self.session])
        held.return_value = ""
        with (patch.object(heal, "stop_completion", return_value=heal.StopPending()),
              patch.object(sessions, "process_start", side_effect=AssertionError("a sent stop must not be retried"))):
            self.assertEqual(heal.sweep(), 0)
            pending = heal.read_history(heal.state_directory())[self.session.session_id]["pending"]
            if pending["kind"] == "none":
                self.fail("accepted stop must remain pending")
            self.assertTrue(pending["stop_request_sent"])
            self.assertEqual(heal.sweep(), 0)
            # Existing journals predate the sent mark and must retain their wait.
            legacy = dict(pending)
            del legacy["stop_request_sent"]
            histories = heal.read_history(heal.state_directory())
            histories[self.session.session_id]["pending"] = heal.read_pending(legacy)
            heal.save_history(heal.state_directory(), histories)
            self.assertEqual(heal.sweep(), 0)
            self.assertEqual(run.call_count, 1)
            self.assertEqual(ensure.call_count, 1)
        invalid_marks: tuple[object, ...] = (None, 1, "false", [])
        for invalid in invalid_marks:
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                _ = heal.read_pending({**pending, "stop_request_sent": invalid})

    def test_hold_and_busy_sessions_do_not_load_or_restart_and_log_reason_changes(self) -> None:
        held, ensure, run = self.install_sweep([self.session])
        held.return_value = "build hold: fixture"
        self.assertEqual(heal.sweep(), 0)
        self.assertEqual(heal.sweep(), 0)
        ensure.assert_not_called()
        run.assert_not_called()
        self.assertEqual(len(self.records()), 1)
        held.return_value = ""
        with patch.object(heal, "list_sessions", return_value=heal.HealSessionRead((replace(self.session, status="busy"),))):
            self.assertEqual(heal.sweep(), 0)
        ensure.assert_not_called()
        run.assert_not_called()
        self.assertEqual([entry["reason"] for entry in self.records()], ["build hold: fixture", "session is not idle"])

    def test_refused_environment_sends_one_blocked_push_and_no_restart(self) -> None:
        held, ensure, run = self.install_sweep([self.session])
        held.return_value = ""
        ensure.return_value = nix_env.Refused("nix_env: fixture: no nix shell")
        self.assertEqual(heal.sweep(), 0)
        self.assertEqual(heal.sweep(), 0)
        self.assertEqual(run.call_count, 1)
        command = cast(list[str], run.call_args.args[0])
        self.assertTrue(command[1].endswith("/message/send.py"))
        self.assertEqual(command[command.index("--need") + 1], "blocked")
        self.assertIn("nix-heal-fixture", command)
        self.assertIn("nix_heal.failed", command)
        self.assertIn("--only fixture --check", command[command.index("--clears-when") + 1])
        self.assertEqual([entry["action"] for entry in self.records()], ["refused", "refused"])

    def test_unit_and_worker_alert_the_nearest_showrunner(self) -> None:
        owner, unit, worker = self.showrunner_fixture()
        raw = f"nix_env: {self.work}: .envrc loaded but no nix shell (IN_NIX_SHELL unset)"
        directory = heal.state_directory()
        directory.mkdir()
        limits = heal.HealLimits.read()
        for row, code, status in ((unit, 0, "SENT"), (worker, 1, "QUEUED"), (unit, 0, "KEPT")):
            with self.subTest(kind=row.kind, status=status):
                session = replace(self.session, session_id=str(row.current_session), name=row.name)
                history = heal.new_history()
                with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], code, f"{status}: fixture", "")) as run:
                    heal.alert(directory, limits, session, history, raw)
                    heal.alert(directory, limits, session, history, raw)
                run.assert_called_once()
                command = cast(list[str], run.call_args.args[0])
                self.assertEqual(command[2:-1], ["--to", owner.id, "--from", "nix-heal",
                                                 "--key", f"nix-heal-{session.session_id}", "--text"])
                text = command[-1]
                for detail in (session.name, socket.gethostname(), str(self.work), raw,
                               "The .envrc file loaded, but no nix shell started.", "Get Nate's approval",
                               "/etc/nixos", "to send Nate a push"):
                    self.assertIn(detail, text)
                for verb in ("retry", "escalate"):
                    argv = [str(heal.SCRIPTS / "lib/py"), str(Path(heal.__file__).resolve()), verb,
                            "--only", session.session_id]
                    self.assertIn(shlex.join(argv), text)
                self.assertTrue(history["notified"])
                self.assertEqual(history["alert_reason"], raw)
                self.assertEqual((self.records()[-1]["action"], self.records()[-1]["reason"]),
                                 ("showrunner_alerted", owner.name))

    def test_alert_falls_back_without_an_owner_or_readable_staff(self) -> None:
        directory = heal.state_directory()
        directory.mkdir()
        root_id = staff.StaffId(fixture_runtime_id("root"))
        root = staff.StaffRow(root_id, "showrunner", "root", staff.StaffRoot(), current_session=root_id)
        session = replace(self.session, session_id=str(root.current_session))
        self.write_staff(root)
        for unreadable in (False, True):
            with self.subTest(unreadable=unreadable):
                if unreadable:
                    _ = self.staff_file.write_text("invalid staff JSON")
                history = heal.new_history()
                with (patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "SENT: fixture", "")) as run,
                      redirect_stderr(io.StringIO()) as errors):
                    heal.alert(directory, heal.HealLimits.read(), session, history, "fixture refusal")
                run.assert_called_once()
                command = cast(list[str], run.call_args.args[0])
                self.assertEqual(command[2:4], ["--to", "user"])
                self.assertIn("nix_heal.failed", command)
                self.assertIn("Could not read the staff record" if unreadable else "no owning showrunner", errors.getvalue())
                self.assertTrue(history["notified"])

    def test_alert_falls_back_when_the_showrunner_send_is_unconfirmed(self) -> None:
        owner, unit, _ = self.showrunner_fixture()
        session = replace(self.session, session_id=str(unit.current_session))
        directory = heal.state_directory()
        directory.mkdir()
        limits = heal.HealLimits.read()
        for refusal in (subprocess.CompletedProcess([], 0, "QUEUED: inconsistent exit", ""),
                        subprocess.TimeoutExpired("fixture send", 1)):
            with self.subTest(refusal=refusal):
                history = heal.new_history()
                with (patch.object(subprocess, "run", side_effect=[
                    refusal, subprocess.CompletedProcess([], 0, "SENT: fixture", "")]) as run,
                      redirect_stderr(io.StringIO()) as errors):
                    heal.alert(directory, limits, session, history, "fixture refusal")
                self.assertEqual(run.call_count, 2)
                peer, push = run.call_args_list
                self.assertIn(owner.id, cast(list[str], peer.args[0]))
                self.assertIn("nix_heal.failed", cast(list[str], push.args[0]))
                self.assertEqual(peer.kwargs["timeout"], push.kwargs["timeout"])
                self.assertIn(f"Could not confirm the alert to {owner.name}", errors.getvalue())
                self.assertTrue(history["notified"])

    def test_escalate_uses_the_saved_alert_reason_for_a_live_session(self) -> None:
        _, unit, _ = self.showrunner_fixture()
        session = replace(self.session, session_id=str(unit.current_session), name=unit.name)
        raw = "session still has no nix shell after its heal restart"
        directory = heal.state_directory()
        directory.mkdir()
        history = heal.new_history()
        with (patch.object(heal, "list_sessions", return_value=heal.HealSessionRead((session,))) as registry,
              patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "SENT: fixture", "")) as run,
              redirect_stdout(io.StringIO()) as output):
            heal.alert(directory, heal.HealLimits.read(), session, history, raw)
            heal.alert(directory, heal.HealLimits.read(), session, history, "later refusal")
            heal.log(directory, session, history, "skip", "session is not idle")
            heal.save_history(directory, {session.session_id: history})
            run.reset_mock()
            args = ["escalate", "--only", session.session_id]
            self.assertEqual(heal.main(args), 0)
            run.assert_called_once()
            command = cast(list[str], run.call_args.args[0])
            self.assertEqual(command[2:4], ["--to", "user"])
            self.assertIn("nix_heal.failed", command)
            self.assertIn("cause=An automatic restart did not fix it.", command)
            self.assertEqual(command[command.index("--key") + 1], f"nix-heal-{session.session_id}")
            check = [str(heal.SCRIPTS / "lib/py"), str(Path(heal.__file__).resolve()), "sweep",
                     "--only", session.session_id, "--check"]
            self.assertEqual(command[command.index("--clears-when") + 1], shlex.join(check))
            self.assertEqual(output.getvalue().splitlines(), [f"SENT push for {session.session_id}."])
            run.reset_mock()
            run.return_value = subprocess.CompletedProcess([], 1, "SENT: inconsistent", "first line\nsecond line")
            self.assertEqual(heal.main(args), 1)
            self.assertEqual(output.getvalue().splitlines()[-1], "Push failed. first line second line")
            run.reset_mock()
            heal.save_history(directory, {})
            self.assertEqual(heal.main(args), 1)
            self.assertIn("No failure reason is saved", output.getvalue().splitlines()[-1])
            heal.save_history(directory, {session.session_id: history})
            registry.return_value = heal.HealSessionRead(())
            self.assertEqual(heal.main(args), 1)
            self.assertIn("No live session matches", output.getvalue().splitlines()[-1])
            run.assert_not_called()

    def test_retry_clears_guard_and_next_failed_sweep_alerts_again(self) -> None:
        owner, unit, _ = self.showrunner_fixture()
        session = replace(self.session, session_id=str(unit.current_session))
        _, ensure, run = self.install_sweep([session])
        directory = heal.state_directory()
        directory.mkdir()
        history = heal.new_history()
        history.update(restart_at=time.time(), restart_pid=session.pid - 1, restart_proc_start="previous")
        heal.save_history(directory, {session.session_id: history})
        self.assertEqual(heal.sweep(), 0)
        self.assertEqual(run.call_count, 1)
        self.assertTrue(heal.read_history(directory)[session.session_id]["notified"])

        args = ["retry", "--only", session.session_id]
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(heal.main(args), 0)
        self.assertEqual(len(output.getvalue().splitlines()), 1)
        cleared = heal.read_history(directory)[session.session_id]
        self.assertEqual((cleared["restart_at"], cleared["restart_pid"], cleared["restart_proc_start"]), (0.0, 0, ""))
        self.assertFalse(cleared["notified"])

        def fail_launch(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            del kwargs
            if command[2] == "resume":
                return subprocess.CompletedProcess(command, 1, "", "fixture launch refused")
            return subprocess.CompletedProcess(command, 0, "SENT: fixture", "")

        run.side_effect = fail_launch
        self.assertEqual(heal.sweep(), 0)
        self.assertEqual(ensure.call_args_list, [call(self.work), call(self.work)])
        commands = [cast(list[str], item.args[0]) for item in run.call_args_list]
        self.assertEqual([command[2] for command in commands], ["--to", "stop", "resume", "--to"])
        self.assertTrue(all(command[3] == owner.id for command in (commands[0], commands[-1])))
        pending = heal.read_history(directory)[session.session_id]
        self.assertEqual(pending["pending"]["kind"], "launching")
        self.assertTrue(pending["notified"])
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(heal.main(args), 1)
        self.assertEqual(output.getvalue().splitlines(), [f"A restart is pending for {session.session_id}."])
        self.assertEqual(heal.read_history(directory)[session.session_id], pending)

    def test_retry_lets_the_next_sweep_restart_a_repaired_session(self) -> None:
        _, ensure, run = self.install_sweep([self.session])
        directory = heal.state_directory()
        directory.mkdir()
        history = heal.new_history()
        history.update(restart_at=time.time(), restart_pid=self.session.pid,
                       restart_proc_start=self.session.proc_start, notified=True)
        heal.save_history(directory, {self.session.session_id: history})
        self.assertEqual(heal.sweep(), 0)
        ensure.assert_not_called()
        run.assert_not_called()
        with redirect_stdout(io.StringIO()):
            self.assertEqual(heal.main(["retry", "--only", self.session.session_id]), 0)
        self.assertEqual(heal.sweep(), 0)
        self.assertEqual(ensure.call_args_list, [call(self.work), call(self.work)])
        commands = [cast(list[str], item.args[0])[2] for item in run.call_args_list]
        self.assertEqual(commands, ["stop", "resume"])
        self.assertTrue(heal.read_history(directory)[self.session.session_id]["healthy_since_restart"])

    def test_retained_alert_marks_notified_without_another_send(self) -> None:
        heal.state_directory().mkdir()
        for code, output in ((1, "checking delivery\nQUEUED: offline"), (0, "KEPT: until silence ends"),
                             (0, "SKIPPED: acknowledged"), (0, "REPLACED: newer push")):
            with self.subTest(output=output):
                history = heal.new_history()
                with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], code, output, "")) as run:
                    heal.alert(heal.state_directory(), heal.HealLimits.read(), self.session, history, "fixture refusal")
                    heal.alert(heal.state_directory(), heal.HealLimits.read(), self.session, history, "fixture refusal")
                self.assertTrue(history["notified"])
                self.assertEqual(run.call_count, 1)
                self.assertEqual(self.records(), [])

    def test_failed_alert_with_queued_text_only_on_stderr_remains_unnotified(self) -> None:
        heal.state_directory().mkdir()
        history = heal.new_history()
        with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", "QUEUED: diagnostic")) as run:
            heal.alert(heal.state_directory(), heal.HealLimits.read(), self.session, history, "fixture refusal")
            heal.alert(heal.state_directory(), heal.HealLimits.read(), self.session, history, "fixture refusal")
        self.assertFalse(history["notified"])
        self.assertEqual(run.call_count, 2)
        self.assertEqual([(entry["action"], entry["reason"]) for entry in self.records()],
                         [("push_failed", "QUEUED: diagnostic")] * 2)

    def test_failed_alert_with_queued_text_in_the_payload_remains_unnotified(self) -> None:
        heal.state_directory().mkdir()
        output = "FAILED: unavailable\nQUEUED: quoted result"
        for code in (0, 1, 3):
            with self.subTest(code=code):
                history = heal.new_history()
                with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], code, output, "")):
                    heal.alert(heal.state_directory(), heal.HealLimits.read(), self.session, history, "fixture refusal")
                self.assertFalse(history["notified"])
                self.assertEqual((self.records()[-1]["action"], self.records()[-1]["reason"]), ("push_failed", output))

    def test_unconfirmed_alert_result_remains_retryable(self) -> None:
        heal.state_directory().mkdir()
        for code, output in ((0, ""), (0, "unrelated output"), (0, "QUEUED: inconsistent exit"),
                             (1, "SENT: inconsistent exit"), (2, "REFUSED: action required"), (4, "HELD: review"),
                             (5, "PENDING: active")):
            with self.subTest(code=code, output=output):
                history = heal.new_history()
                with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], code, output, "")) as run:
                    heal.alert(heal.state_directory(), heal.HealLimits.read(), self.session, history, "fixture refusal")
                    heal.alert(heal.state_directory(), heal.HealLimits.read(), self.session, history, "fixture refusal")
                self.assertFalse(history["notified"])
                self.assertEqual(run.call_count, 2)
                self.assertEqual(self.records()[-1]["action"], "push_failed")
                self.assertEqual(self.records()[-1]["reason"], output or f"exit {code}")

    def test_broken_replacement_hits_hour_guard_and_pushes_once(self) -> None:
        _ = write_config_fixture(Path(self.env["CONF_DIR"]) / "nix_env.conf", "restart_interval_seconds=3600\n")
        held, ensure, run = self.install_sweep([self.session])
        held.return_value = ""
        # The command returns but never publishes a healthy replacement.
        def unconfirmed(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            del kwargs
            return subprocess.CompletedProcess(command, 0, "SENT: fixture", "")

        run.side_effect = unconfirmed
        with patch.object(time, "time", return_value=10000):
            self.assertEqual(heal.sweep(), 0)
        replacement = replace(self.session, pid=812, proc_start="replacement")
        with (patch.object(heal, "list_sessions", return_value=heal.HealSessionRead((replacement,))),
              patch.object(time, "time", return_value=10030)):
            self.assertEqual(heal.sweep(), 0)
            self.assertEqual(heal.sweep(), 0)
        self.assertEqual(ensure.call_args_list, [call(self.work), call(self.work)])
        self.assertEqual(run.call_count, 3)
        commands = [cast(list[str], call.args[0]) for call in run.call_args_list]
        self.assertEqual(sum(command[2] == "resume" for command in commands), 1)
        self.assertEqual(sum("--need" in command for command in commands), 1)
        self.assertFalse(any(entry["action"] == "restart" for entry in self.records()))
        self.assertEqual(heal.read_history(heal.state_directory())[self.session.session_id]["pending"]["kind"], "launching")
        push = commands[-1]
        values = dict(push[index + 1].split("=", 1) for index, word in enumerate(push) if word == "--value")
        self.assertIn("An automatic restart did not fix it.",
                      push_texts.render("nix_heal.failed", **values).message)
        raw = "session still has no nix shell after its heal restart"
        self.assertEqual(self.records()[-1]["reason"], raw)
        self.assertNotIn(raw, " ".join(push))

    def test_recovery_failure_phone_causes_keep_raw_diagnostics_only_in_the_log(self) -> None:
        cases = (
            ("refused", f"nix_env: {self.work}: .envrc loaded but no nix shell (IN_NIX_SHELL unset)",
             "The automatic restart could not fix it. The .envrc file loaded, but no nix shell started."),
            ("still_broken", "session still has no nix shell after its heal restart",
             "An automatic restart did not fix it."),
            ("other_refusal", "nix_env: direnv: fixture stderr\nraw diagnostic with internal details",
             "The automatic restart could not fix it. The nix shell setup refused to load."),
        )
        for outcome, raw, cause in cases:
            with self.subTest(outcome=outcome), patch.object(heal, "state_directory", return_value=self.root / outcome):
                held, ensure, run = self.install_sweep([self.session])
                held.return_value = ""
                if outcome == "still_broken":
                    heal.state_directory().mkdir(parents=True)
                    history = heal.new_history()
                    history["restart_at"] = time.time()
                    history["restart_pid"] = self.session.pid - 1
                    history["restart_proc_start"] = "before-restart"
                    heal.save_history(heal.state_directory(), {self.session.session_id: history})
                else:
                    ensure.return_value = nix_env.Refused(raw)
                self.assertEqual(heal.sweep(), 0)
                run.assert_called_once()
                command = cast(list[str], run.call_args.args[0])
                values = dict(command[index + 1].split("=", 1)
                              for index, word in enumerate(command) if word == "--value")
                push = push_texts.render("nix_heal.failed", **values)
                self.assertEqual(push.body,
                    f"The nix shell for {self.session.name}'s worktree did not load, so its builds will fail. "
                    + f"{cause}\n\n"
                    + "The session's worktree changes stay on disk. This alert clears when the session has a nix shell.")
                self.assertEqual(push.title,
                                 f"{self.session.name} builds will fail: no nix shell")
                self.assertIn(
                    f"On {socket.gethostname()}, fix the nix shell in {self.work}, then restart {self.session.name}",
                    push.message)
                self.assertNotIn(raw, " ".join(command))
                self.assertEqual([entry["reason"] for entry in self.records()], [raw])

    def test_hold_acquired_during_environment_load_prevents_restart(self) -> None:
        held, ensure, run = self.install_sweep([self.session])
        held.side_effect = ["", "build hold: acquired"]
        self.assertEqual(heal.sweep(), 0)
        ensure.assert_called_once()
        run.assert_not_called()
        self.assertEqual(self.records()[0]["reason"], "build hold: acquired")

    def test_hold_between_stop_and_launch_persists_pending_restart_without_live_registry(self) -> None:
        held, ensure, run = self.install_sweep([self.session])
        complete = cast(Callable[..., subprocess.CompletedProcess[str]], run.side_effect)

        def stop_or_launch(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            if "stop" in command:
                held.return_value = "build hold: acquired after stop"
            return complete(command, **kwargs)

        run.side_effect = stop_or_launch
        self.assertEqual(heal.sweep(), 0)
        commands = [cast(list[str], call.args[0]) for call in run.call_args_list]
        self.assertEqual(len(commands), 1)
        self.assertNotIn("--restart", commands[0])
        self.assertEqual(self.records()[-1]["action"], "pending")
        # The stopped process has left the registry; recovery must use the journal.
        held.return_value = ""
        with patch.object(heal, "list_sessions", return_value=heal.HealSessionRead(())) as registry:
            self.assertEqual(heal.sweep(), 0)
            replacement = replace(self.session, pid=10811, proc_start="restarted-start-811")
            registry.return_value = heal.HealSessionRead((replacement,))
            self.assertEqual(heal.sweep(), 0)
        commands = [cast(list[str], call.args[0]) for call in run.call_args_list]
        self.assertEqual([command[2] for command in commands], ["stop", "resume"])
        self.assertEqual(commands[1][3:], ["test-account", "--only", self.session.session_id, "--here"])
        self.assertEqual(self.records()[-1]["action"], "restart")
        self.assertEqual(ensure.call_count, 2)

    def test_async_stop_completion_is_required_before_any_relaunch(self) -> None:
        held, ensure, run = self.install_sweep([self.session])
        held.return_value = ""
        with patch.object(heal, "stop_completion", return_value=heal.StopPending()) as completion:
            self.assertEqual(heal.sweep(), 0)
            self.assertEqual(heal.sweep(), 0)
            self.assertEqual(run.call_count, 1)
            self.assertEqual(self.records()[-1]["reason"], "waiting for shutdown stop completion")
            completion.return_value = heal.StopComplete()
            self.assertEqual(heal.sweep(), 0)
        self.assertEqual([cast(list[str], item.args[0])[2] for item in run.call_args_list], ["stop", "resume"])
        self.assertEqual(ensure.call_count, 2)

    def test_pending_restart_refusal_alerts_once_and_never_relaunches(self) -> None:
        held, ensure, run = self.install_sweep([self.session])
        held.return_value = ""
        ensure.side_effect = [nix_env.NoEnvrc({}), nix_env.Refused("nix_env: changed while stopped"),
                              nix_env.Refused("nix_env: changed while stopped")]
        self.assertEqual(heal.sweep(), 0)
        self.assertEqual(heal.sweep(), 0)
        commands = [cast(list[str], item.args[0]) for item in run.call_args_list]
        self.assertEqual(sum(command[2] == "stop" for command in commands), 1)
        self.assertEqual(sum(command[2] == "resume" for command in commands), 0)
        self.assertEqual(sum("--need" in command for command in commands), 1)

    def test_hold_during_pending_environment_load_prevents_the_relaunch(self) -> None:
        held, ensure, run = self.install_sweep([self.session])
        loaded = nix_env.Loaded({"IN_NIX_SHELL": "impure", "NIX_LDFLAGS": "-Lfixture"}, self.work / ".envrc")

        def load(directory: Path) -> nix_env.Loaded:
            self.assertEqual(directory, self.work)
            if ensure.call_count == 2:
                held.return_value = "build hold: acquired during final load"
            return loaded

        ensure.side_effect = load
        self.assertEqual(heal.sweep(), 0)
        self.assertEqual(ensure.call_count, 2)
        commands = [cast(list[str], item.args[0]) for item in run.call_args_list]
        self.assertEqual([command[2] for command in commands], ["stop"])
        self.assertEqual(heal.read_history(heal.state_directory())[self.session.session_id]["pending"]["kind"], "stopping")
        held.return_value = ""
        with patch.object(stop_resume, "resume", wraps=stop_resume.resume) as resume:
            self.assertEqual(heal.sweep(), 0)
        resume.assert_called_once_with(self.session.session_id, why="nix shell heal")
        self.assertEqual(heal.read_history(heal.state_directory())[self.session.session_id]["pending"]["kind"], "none")

    def test_reboot_after_stop_preserves_the_next_sweep_relaunch(self) -> None:
        _held, _ensure, run = self.install_sweep([self.session])
        command = cast(Callable[..., subprocess.CompletedProcess[str]], run.side_effect)
        lock = herdr.reboot_lock_path()
        lock.parent.mkdir(parents=True)
        with ExitStack() as reboot:
            def stop_before_reboot(arguments: list[str], **options: object) -> subprocess.CompletedProcess[str]:
                self.assertEqual(arguments[2], "stop")
                result = command(arguments, **options)
                external = reboot.enter_context(lock.open("a"))
                fcntl.flock(external, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return result

            run.side_effect = stop_before_reboot
            self.assertEqual(heal.sweep(), 0)
        history = heal.read_history(heal.state_directory())[self.session.session_id]
        self.assertEqual(history["pending"]["kind"], "stopping")
        self.assertEqual(history["action"], "pending")
        self.assertEqual(history["reason"], "A reboot is running; wait for it to end.")
        run.side_effect = command
        self.assertEqual(heal.sweep(), 0)
        self.assertEqual(heal.read_history(heal.state_directory())[self.session.session_id]["pending"]["kind"], "none")
        self.assertEqual([cast(list[str], item.args[0])[2] for item in run.call_args_list], ["stop", "resume"])

    def test_relaunch_failure_keeps_pending_and_pushes_once_without_a_duplicate_launch(self) -> None:
        for failure in ("timeout", "nonzero", "oserror"):
            with self.subTest(failure=failure), patch.object(heal, "state_directory", return_value=self.root / failure):
                held, ensure, run = self.install_sweep([self.session])
                held.return_value = ""

                def fail_launch(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                    del kwargs
                    if command[2] == "resume":
                        self.assertIn(self.session.session_id, command)
                        self.assertEqual(heal.read_history(heal.state_directory())[self.session.session_id]["pending"]["kind"], "launching")
                        if failure == "timeout":
                            raise subprocess.TimeoutExpired(command, 30)
                        if failure == "oserror":
                            raise OSError("fixture cannot start restart")
                        return subprocess.CompletedProcess(command, 1, "", "fixture launch refused")
                    return subprocess.CompletedProcess(command, 0, "SENT: fixture", "")

                run.side_effect = fail_launch
                self.assertEqual(heal.sweep(), 0)
                self.assertEqual(heal.read_history(heal.state_directory())[self.session.session_id]["pending"]["kind"], "launching")
                self.assertEqual(heal.sweep(), 0)
                commands = [cast(list[str], item.args[0]) for item in run.call_args_list]
                self.assertEqual(sum(command[2] == "resume" for command in commands), 1)
                self.assertEqual(sum("--need" in command for command in commands), 1)
                self.assertEqual(ensure.call_count, 2)
                # Delayed success after a timeout must clear the reservation,
                # rather than start another copy of the session.
                replacement = replace(self.session, pid=912, proc_start="late-replacement")
                with (patch.object(heal, "list_sessions", return_value=heal.HealSessionRead((replacement,))),
                      patch.object(heal, "inspect", return_value=heal.Healthy())):
                    self.assertEqual(heal.sweep(), 0)
                self.assertEqual(heal.read_history(heal.state_directory())[self.session.session_id]["pending"]["kind"], "none")
                self.assertEqual(sum(cast(list[str], item.args[0])[2] == "resume" for item in run.call_args_list), 1)

    def test_successful_restart_command_retains_pending_until_the_replacement_is_healthy(self) -> None:
        held, ensure, run = self.install_sweep([self.session])
        held.return_value = ""

        def success_without_registry(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            del kwargs
            return subprocess.CompletedProcess(command, 0, "", "")

        run.side_effect = success_without_registry
        self.assertEqual(heal.sweep(), 0)
        self.assertEqual(heal.read_history(heal.state_directory())[self.session.session_id]["pending"]["kind"], "launching")
        replacement = replace(self.session, pid=913, proc_start="confirmed-replacement")
        path = self.root / "unreadable.json"
        with (patch.object(heal, "list_sessions", return_value=heal.HealSessionRead((), (path,))) as registry,
              patch.object(heal, "inspect", return_value=heal.Healthy())):
            with redirect_stderr(io.StringIO()):
                self.assertEqual(heal.sweep(), 0)
            self.assertEqual(self.records()[-1]["reason"], f"session registry unreadable: {path}")
            self.assertEqual(heal.read_history(heal.state_directory())[self.session.session_id]["pending"]["kind"], "launching")
            registry.return_value = heal.HealSessionRead((replace(replacement, config_dir=self.home / ".claude-wrong"),))
            self.assertEqual(heal.sweep(), 0)
            self.assertEqual(heal.read_history(heal.state_directory())[self.session.session_id]["pending"]["kind"], "launching")
            registry.return_value = heal.HealSessionRead((replacement,))
            self.assertEqual(heal.sweep(), 0)
        self.assertEqual(heal.read_history(heal.state_directory())[self.session.session_id]["pending"]["kind"], "none")
        self.assertEqual(run.call_count, 2)
        self.assertEqual(ensure.call_count, 2)
        self.assertEqual(self.records()[-1]["action"], "restart")

    def test_registry_discovery_keeps_a_denied_account_directory(self) -> None:
        configured = self.root / "configured-sessions"
        configured.mkdir()
        denied = self.home / ".claude7" / "sessions"
        denied.parent.mkdir(parents=True)
        stat = Path.stat

        def stat_path(path: Path, *, follow_symlinks: bool = True) -> os.stat_result:
            if path == denied:
                raise PermissionError(path)
            return stat(path, follow_symlinks=follow_symlinks)

        with (patch.dict(os.environ, {"NOTIFIER_SESSIONS_DIR": str(configured)}),
              patch.object(Path, "stat", stat_path), redirect_stderr(io.StringIO()) as errors):
            self.assertEqual(heal.sweep(check=True), 1)
        self.assertIn(f"session registry unreadable: {denied}", errors.getvalue())

    def test_recursive_registry_discovery_keeps_nested_records_and_denied_paths(self) -> None:
        _ = write_config_fixture(Path(self.env["CONF_DIR"]) / "nix_env.conf",
                                 "session_directory_glob=.claude*/**/sessions\n")
        configured = self.root / "configured-sessions"
        configured.mkdir()
        nested = self.home / ".claude3" / "nested" / "account" / "sessions"
        nested.mkdir(parents=True)
        _ = (nested / "live.json").write_text(json.dumps({
            "pid": 811, "procStart": "start-811", "sessionId": "nested", "name": "worker",
            "messagingSocketPath": "", "updatedAt": 1, "cwd": str(self.work), "status": "idle",
        }))
        denied = self.home / ".claude3" / "denied"
        denied.mkdir()
        scandir = os.scandir
        denied_is_dir = Mock(side_effect=PermissionError(errno.EACCES, "denied"))
        denied_entry = Mock(spec=os.DirEntry, is_dir=denied_is_dir)
        denied_entry.name = denied.name

        for operation in ("scandir", "is_dir"):
            def scan(path: Path | str) -> object:
                if operation == "scandir" and Path(path) == denied:
                    raise PermissionError(errno.EACCES, "denied", str(denied))
                if operation == "is_dir" and Path(path) == denied.parent:
                    with scandir(path) as entries:
                        return nullcontext(iter([denied_entry if entry.name == denied.name else entry for entry in entries]))
                return scandir(path)

            with (self.subTest(operation=operation),
                  patch.dict(os.environ, {"NOTIFIER_SESSIONS_DIR": str(configured)}),
                  patch.object(os, "scandir", side_effect=scan),
                  patch.object(sessions, "headless", return_value=False),
                  patch.object(sessions, "process_start", return_value="start-811"),
                  patch.object(heal, "inspect", return_value=heal.Healthy()),
                  redirect_stderr(io.StringIO()) as errors):
                found = heal.list_sessions()
                self.assertEqual(tuple(item.session_id for item in found.sessions), ("nested",))
                self.assertEqual(found.unreadable, (denied,))
                self.assertEqual(heal.find_session("missing"),
                                 heal.Unavailable(f"session registry unreadable: {denied}"))
                self.assertEqual(heal.sweep(check=True), 1)
                self.assertEqual(errors.getvalue(), f"nix-heal: session registry unreadable: {denied}\n")

    def test_unreadable_fresh_registry_prevents_a_stop_after_loading_the_environment(self) -> None:
        held, ensure, run = self.install_sweep([self.session])
        held.return_value = ""
        path = self.root / "unreadable.json"
        with patch.object(heal, "list_sessions", side_effect=[
                heal.HealSessionRead((self.session,)), heal.HealSessionRead((), (path,))]):
            self.assertEqual(heal.sweep(), 0)
        ensure.assert_called_once()
        run.assert_not_called()
        self.assertEqual(self.records()[-1]["reason"], f"session registry unreadable: {path}")

    def test_incomplete_registry_cannot_prove_absence_or_health(self) -> None:
        for cause in ("missing folder", "unreadable record", "readable empty folder"):
            registry = self.root / cause
            if cause != "missing folder":
                registry.mkdir()
            unreadable = registry
            if cause == "unreadable record":
                unreadable = registry / "unfinished.json"
                _ = unreadable.write_text("{")
            with (self.subTest(cause=cause), patch.dict(os.environ, {"NOTIFIER_SESSIONS_DIR": str(registry)}),
                  patch.object(sessions, "process_start", side_effect=AssertionError("unexpected process probe")),
                  redirect_stderr(io.StringIO()) as errors):
                found = heal.find_session("missing")
                if cause == "readable empty folder":
                    self.assertEqual(found, heal.Gone())
                    self.assertEqual(heal.sweep(check=True), 0)
                    self.assertEqual(errors.getvalue(), "")
                else:
                    reason = f"session registry unreadable: {unreadable}"
                    self.assertEqual(found, heal.Unavailable(reason))
                    self.assertEqual(heal.sweep(check=True), 1)
                    self.assertEqual(errors.getvalue(), f"nix-heal: {reason}\n")

    def test_check_requires_a_live_healthy_session_to_clear_alert(self) -> None:
        with patch.object(heal, "list_sessions", return_value=heal.HealSessionRead(())):
            self.assertEqual(heal.sweep("missing", check=True), 1)
        with (patch.object(heal, "list_sessions", return_value=heal.HealSessionRead((self.session,))),
              patch.object(heal, "inspect", return_value=heal.Unavailable("metadata unreadable"))):
            self.assertEqual(heal.sweep("fixture", check=True), 1)
        with (patch.object(heal, "list_sessions", return_value=heal.HealSessionRead((self.session,))),
              patch.object(heal, "inspect", return_value=heal.Healthy())):
            self.assertEqual(heal.sweep("fixture", check=True), 0)

    def test_one_failed_session_does_not_prevent_next_restart(self) -> None:
        other = replace(self.session, session_id="other", pid=813)
        held, ensure, run = self.install_sweep([self.session, other])
        held.return_value = ""
        ensure.side_effect = [OSError("fixture unreadable"), nix_env.NoEnvrc({}), nix_env.NoEnvrc({})]
        self.assertEqual(heal.sweep(), 0)
        self.assertEqual([entry["action"] for entry in self.records()], ["refused", "restart"])
        self.assertEqual(run.call_count, 2)
        self.assertIn("other", cast(list[str], run.call_args.args[0]))
