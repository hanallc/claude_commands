"""Test producer stops and stray removal with private session files."""

from __future__ import annotations

import fcntl
import io
import json
import math
import os
import shlex
import signal
import subprocess
import sys
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import call, patch

from scripts.agents import pane_record, restart, restart_support, shutdown, shutdown_record, stop_resume, test_stop_resume
from scripts.herdr import herdr, pane_launch
from scripts.herdr.test_herdr import codex_foreground_cases
from scripts.herdr.test_worker_panes import PaneWorld
from scripts.lib import atomic_file
from scripts.message import roster, send as message_send, sessions
from scripts.production import staff, worker
from scripts.production.staff_test_support import StaffTestCase, fixture_member, fixture_runtime_id
from scripts.whoami import quota_state


NATIVE_PROCESS = herdr.native_process


def pending_resume_row(row: staff.StaffRow) -> staff.StaffRow:
    assert isinstance(row.paused, staff.Paused)
    pending = staff.ResumePending(f"agents-resume:{row.id}:{row.paused.since}", row.paused)
    return replace(row, paused=staff.Active(), resume_pending=pending)


class PauseWorkersTests(StaffTestCase):
    def test_named_pause_keeps_the_worker_and_resume_sends_one_continuation(self) -> None:
        owner = fixture_member("showrunner", name="production")
        director = fixture_member("unit", name="selected", parent=owner.id)
        child = fixture_member("worker", name="impl", parent=director.id)
        world = PaneWorld()
        world.setup(self)
        world.add(director, "w1:p0")
        world.add(child, "w1:p1")
        world.locations["w1:p1"]["agent_status"] = "working"
        processes = world.foreground.copy()

        with (patch.object(herdr, "reboot_lock", side_effect=ExitStack),
              patch.object(herdr, "read_conversation", return_value=herdr.LiveConversation("claude", (123,))),
              patch.object(restart, "stop_row", side_effect=world.stop),
              patch.object(shutdown, "_message", return_value={"kind": "sent"}) as send,
              patch.object(stop_resume, "running_on_account",
                           return_value=stop_resume.ClaudeUp(director.current_session, "w1:p0", "claude2")),
              patch.object(stop_resume, "resume") as start,
              patch.object(worker, "restore") as restore,
              patch.object(worker, "resume_workers") as worker_message,
              redirect_stdout(io.StringIO())):
            self.assertEqual(shutdown.main(["pause", director.name, "--keep-workers", "--why", "maintenance"]), 0)
            paused = staff.snapshot().by_id(director.id)
            assert paused is not None and isinstance(paused.paused, staff.Paused)
            self.assertTrue(paused.paused.keep_workers)
            send.assert_called_once_with(director,
                "Finish your current turn. Hold at the prompt. Do not start new work. Wait for agents resume.")
            send.reset_mock()
            self.assertEqual(shutdown.main(["resume", director.name]), 0)
            send.assert_called_once_with(pending_resume_row(paused), "Continue where you stopped.", require_live=True,
                                         key=f"agents-resume:{director.id}:{paused.paused.since}")
        self.assertEqual(staff.by_id(director.id), director)
        self.assertEqual(staff.by_id(child.id), child)
        self.assertEqual(world.foreground, processes)
        self.assertEqual(world.locations["w1:p1"]["agent_status"], "working")
        self.assertEqual(world.stops, [])
        start.assert_not_called()
        restore.assert_not_called()
        worker_message.assert_not_called()

    def test_production_pause_keeps_workers_and_skips_the_excluded_director(self) -> None:
        owner = fixture_member("showrunner", name="production")
        selected = fixture_member("unit", name="selected", parent=owner.id)
        excluded = fixture_member("unit", name="excluded", parent=owner.id)
        world = PaneWorld()
        world.setup(self)
        for index, director in enumerate((selected, excluded), start=1):
            child = fixture_member("worker", name="impl", parent=director.id)
            world.add(director, f"w{index}:p0", workspace=f"w{index}", tab=f"w{index}:t1")
            world.add(child, f"w{index}:p1", workspace=f"w{index}", tab=f"w{index}:t1")
        processes = world.foreground.copy()
        with (patch.object(herdr, "reboot_lock", side_effect=ExitStack),
              patch.object(herdr, "read_conversation", return_value=herdr.LiveConversation("claude", (123,))),
              patch.object(restart, "stop_row", side_effect=world.stop),
              patch.object(shutdown, "_message", return_value={"kind": "sent"}) as send,
              redirect_stdout(io.StringIO())):
            self.assertEqual(shutdown.main(["pause", "--production", owner.name, "--except", excluded.name,
                                            "--keep-workers", "--why", "maintenance"]), 0)
        paused = staff.by_id(selected.id)
        assert paused is not None and isinstance(paused.paused, staff.Paused)
        self.assertTrue(paused.paused.keep_workers)
        self.assertEqual(staff.by_id(excluded.id), excluded)
        self.assertEqual(world.foreground, processes)
        self.assertEqual(world.stops, [])
        send.assert_called_once()
        self.assertEqual(send.call_args.args[0], selected)

    def test_production_pause_refuses_running_and_stopped_codex_directors(self) -> None:
        owner = fixture_member("showrunner", name="production")
        running = fixture_member("unit", name="running", parent=owner.id)
        stopped = fixture_member("unit", name="stopped", parent=owner.id)
        claude = fixture_member("unit", name="claude", parent=owner.id)
        observations = {
            running.current_session: herdr.LiveConversation("codex", (123,)),
            stopped.current_session: herdr.StoppedConversation(),
            claude.current_session: herdr.LiveConversation("claude", (124,)),
        }
        with (patch.object(herdr, "read_conversation", side_effect=observations.__getitem__),
              patch.object(pane_launch, "current_kind", return_value="codex") as saved_kind,
              patch.object(shutdown, "_message", return_value={"kind": "sent"}) as send,
              patch.object(stop_resume, "stop_unit_workers") as stop,
              redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors):
            self.assertEqual(shutdown.main(["pause", "--production", owner.name,
                                            "--keep-workers", "--why", "maintenance"]), 2)

        paused = staff.by_id(claude.id)
        assert paused is not None and isinstance(paused.paused, staff.Paused)
        self.assertTrue(paused.paused.keep_workers)
        self.assertEqual(staff.by_id(running.id), running)
        self.assertEqual(staff.by_id(stopped.id), stopped)
        self.assertEqual(output.getvalue(), "claude: paused\n")
        self.assertCountEqual(errors.getvalue().splitlines(), [
            f"{row.name}: --keep-workers refused. The running Codex hook is unverified. Use the default pause."
            for row in (running, stopped)])
        saved_kind.assert_called_once_with(stopped)
        send.assert_called_once()
        self.assertEqual(send.call_args.args[0], claude)
        stop.assert_not_called()

    def test_named_codex_pause_refuses_the_director_on_the_peer(self) -> None:
        owner = fixture_member("showrunner", name="production")
        director = fixture_member("unit", name="selected", parent=owner.id,
                                  worktree=staff.PresentWorktree("mac", self.root))

        def peer(arguments: list[str]) -> tuple[int, str]:
            self.assertEqual(arguments, ["_keep-workers-check", str(director.id), str(director.current_session)])
            with (patch.object(shutdown, "local_machine", return_value="mac"),
                  patch.object(herdr, "read_conversation", return_value=herdr.LiveConversation("codex", (123,))),
                  redirect_stdout(io.StringIO()) as reply):
                return shutdown.main(arguments), reply.getvalue().strip()

        with (patch.object(shutdown, "local_machine", return_value="natedev"),
              patch.object(shutdown, "run_remote", side_effect=peer) as check_peer,
              patch.object(shutdown, "_message") as send,
              patch.object(stop_resume, "stop_unit_workers") as stop,
              redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as errors):
            arguments = ["pause", director.name, "--keep-workers", "--why", "maintenance"]
            self.assertEqual(shutdown.main(arguments), 2)

        self.assertEqual(staff.by_id(director.id), director)
        check_peer.assert_called_once()
        self.assertEqual(errors.getvalue(),
                         "selected: --keep-workers refused. The running Codex hook is unverified. Use the default pause.\n")
        send.assert_not_called()
        stop.assert_not_called()

    def test_plain_pause_stops_the_worker_and_leaves_the_director_running(self) -> None:
        owner = fixture_member("showrunner", name="production")
        director = fixture_member("unit", name="selected", parent=owner.id,
                                  worktree=staff.PresentWorktree("natedev", self.root))
        child = fixture_member("worker", name="impl", parent=director.id)
        world = PaneWorld()
        world.setup(self)
        world.add(director, "w1:p0")
        world.add(child, "w1:p1")
        with (patch.object(herdr, "reboot_lock", side_effect=ExitStack),
              patch.object(restart, "stop_row", side_effect=world.stop),
              patch.object(shutdown, "_message", return_value={"kind": "sent"}),
              redirect_stdout(io.StringIO())):
            self.assertEqual(shutdown.main(["pause", director.name, "--why", "maintenance"]), 0)
        paused = staff.by_id(director.id)
        assert paused is not None and isinstance(paused.paused, staff.Paused)
        self.assertFalse(paused.paused.keep_workers)
        self.assertEqual(world.stops, [child.current_session])
        self.assertEqual(set(world.foreground), {"w1:p0"})

    def test_stopped_kept_worker_resume_restores_the_pause_after_rejection(self) -> None:
        owner = fixture_member("showrunner", name="production")
        director = fixture_member("unit", name="selected", parent=owner.id)
        paused = staff.pause(str(director.id), "maintenance", keep_workers=True)
        for delivered in (False, True):
            def send_continuation(*_args: object, **_kwargs: object) -> restart_support.MessageSendOutcome:
                self.assertEqual(staff.by_id(director.id), pending_resume_row(paused))
                if delivered:
                    return {"kind": "sent"}
                return {"kind": "rejected", "reason": "fixture socket miss"}

            with (self.subTest(delivered=delivered),
                  patch.object(herdr, "reboot_lock", side_effect=ExitStack),
                  patch.object(stop_resume, "running_on_account", return_value=sessions.SessionNotLive()),
                  patch.object(stop_resume, "resume",
                               return_value=stop_resume.ClaudeUp(director.current_session, "w1:p0", "claude2")) as start,
                  patch.object(shutdown, "_message", side_effect=send_continuation) as send,
                  patch.object(shutdown_record, "remove_started") as remove,
                  redirect_stdout(io.StringIO())):
                self.assertEqual(shutdown.main(["resume", director.name]), 0 if delivered else 1)
            start.assert_called_once_with(str(director.id), why="resumed by name", to=None, keep_workers=True)
            assert isinstance(paused.paused, staff.Paused)
            send.assert_called_once_with(pending_resume_row(paused), "Continue where you stopped.", require_live=True,
                                         key=f"agents-resume:{director.id}:{paused.paused.since}")
            if delivered:
                remove.assert_called_once_with([str(director.id)])
            else:
                remove.assert_not_called()
            self.assertEqual(staff.by_id(director.id), director if delivered else paused)

    def test_kept_worker_resume_uses_the_live_staff_route_on_retry(self) -> None:
        owner = fixture_member("showrunner", name="production")
        director = fixture_member("unit", name="selected", parent=owner.id)
        paused = staff.pause(str(director.id), "maintenance", keep_workers=True)
        live = stop_resume.ClaudeUp(director.current_session, "w1:p0", "claude2")

        with (patch.object(herdr, "reboot_lock", side_effect=ExitStack),
              patch.object(stop_resume, "running_on_account", return_value=sessions.SessionNotLive()) as probe,
              patch.object(stop_resume, "resume", return_value=live) as start,
              patch.object(restart_support, "_send_command", return_value=["send.py"]),
              patch.object(restart_support, "_deliver_message",
                           side_effect=[RuntimeError("socket unavailable"), {"kind": "sent"}]) as send,
              patch.object(shutdown_record, "remove_started") as remove,
              redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO())):
            self.assertEqual(shutdown.main(["resume", director.name]), 1)
            self.assertEqual(staff.by_id(director.id), pending_resume_row(paused))
            remove.assert_not_called()
            self.assertEqual(shutdown.main(["resume", director.name]), 0)
        start.assert_called_once()
        probe.assert_called_once()
        remove.assert_called_once_with([str(director.id)])
        assert isinstance(paused.paused, staff.Paused)
        argv = ["send.py", "--to", str(director.id), "--from", "shutdown", "--summary", "agents lifecycle",
                "--recipient-kind", "unit", "--key", f"agents-resume:{director.id}:{paused.paused.since}", "--once-per-key"]
        self.assertEqual(send.call_args_list, [call(argv, str(director.id), "Continue where you stopped.")] * 2)
        self.assertEqual(staff.by_id(director.id), director)


    def test_resume_keeps_pending_after_a_lost_peer_reply_and_recovers_the_receipt(self) -> None:
        owner = fixture_member("showrunner", name="production")
        director = fixture_member("unit", name="selected", parent=owner.id)
        paused = staff.pause(str(director.id), "maintenance", keep_workers=True)
        pending = pending_resume_row(paused)
        assert isinstance(pending.resume_pending, staff.ResumePending)
        key = pending.resume_pending.key
        origin_state = self.root / "origin-messages"
        peer_state = self.root / "peer-messages"
        attempts = 0
        peer_live = True
        receipt_visible = False

        def invoke_sender(args: list[str], text: str) -> subprocess.CompletedProcess[str]:
            output, errors = io.StringIO(), io.StringIO()
            with (patch.object(sys, "stdin", io.StringIO(text)),
                  redirect_stdout(output), redirect_stderr(errors)):
                code = message_send.main(args)
            return subprocess.CompletedProcess(args, code, output.getvalue(), errors.getvalue())

        def run_sender(command: list[str], *, input: str, **_options: object) -> subprocess.CompletedProcess[str]:
            return invoke_sender(command[1:], input)

        def route(*_args: object, **_kwargs: object) -> message_send.RemoteDelivery:
            if not peer_live:
                message_send.usage_error(f"no live session answers to staff ID {director.id}")
            return message_send.RemoteDelivery("mac", director.id)

        def ssh(command: list[str], text: str, _timeout: float) -> tuple[int, str, str]:
            nonlocal attempts
            self.assertEqual(command[3], "mac")
            args = shlex.split(command[-1])[2:]
            if args == ["once-receipt", key]:
                if not receipt_visible:
                    return 255, "", "fixture receipt unavailable"
                with patch.object(message_send, "STATE", peer_state):
                    result = invoke_sender(args, "")
                return result.returncode, result.stdout, result.stderr
            attempts += 1
            self.assertEqual(message_send.read_key_states()[key].get("once_remote"),
                             {"host": "mac", "recipient": director.id})
            if attempts == 1:
                self.assertEqual(text, "Continue where you stopped.")
                with patch.object(message_send, "STATE", peer_state):
                    message_send.record_once(key)
                return 255, "", "fixture lost reply"
            return 3, f"FAILED: {message_send.BEFORE_ACCEPTANCE}no live staff recipient\n", ""

        live = stop_resume.ClaudeUp(director.current_session, "w1:p0", "claude2")
        arguments = ["resume", director.name]
        with (patch.object(herdr, "reboot_lock", side_effect=ExitStack),
              patch.object(stop_resume, "running_on_account", return_value=live),
              patch.object(restart_support, "_send_command", return_value=["send.py"]),
              patch.object(restart_support, "subprocess", SimpleNamespace(run=run_sender)),
              patch.object(shutdown_record, "remove_started"),
              patch.object(message_send, "STATE", origin_state),
              patch.object(message_send, "resolve_recipient", side_effect=route) as resolve,
              patch.object(roster, "roster", return_value=roster.Roster([], [])),
              patch.object(message_send, "run", side_effect=ssh),
              redirect_stdout(io.StringIO()) as output):
            self.assertEqual(shutdown.main(arguments), 1)
            self.assertEqual(staff.by_id(director.id), pending)
            self.assertEqual(shutdown.main(arguments), 1)
            self.assertEqual(staff.by_id(director.id), pending)
            peer_live = False
            self.assertEqual(shutdown.main(arguments), 1)
            self.assertEqual(staff.by_id(director.id), pending)
            self.assertNotIn(": active", output.getvalue())

            receipt_visible = True
            resolve.reset_mock()
            self.assertEqual(shutdown.main(arguments), 0)
            resolve.assert_not_called()
        self.assertEqual(staff.by_id(director.id), director)
        self.assertEqual(attempts, 2)
        self.assertEqual(output.getvalue(), "selected: active\n")


class ProductionResumeTests(StaffTestCase):
    def test_resume_recovers_a_saved_continuation_before_the_active_path(self) -> None:
        director = fixture_member("unit", name="selected", parent=fixture_member("showrunner", name="production").id)
        pause = staff.Paused("2026-11-24T08:31:00-08:00", "director", "review")
        _ = staff.set_hold(str(director.id), pause)
        pending = staff.begin_resume(director.id, pause)
        assert isinstance(pending.resume_pending, staff.ResumePending)

        with (patch.object(herdr, "reboot_lock", side_effect=ExitStack),
              patch.object(stop_resume, "resume") as start,
              patch.object(quota_state, "no_worker_mode", return_value=False),
              patch.object(shutdown, "_message", return_value={"kind": "accepted"}) as send,
              patch.object(shutdown_record, "remove_started") as remove,
              redirect_stdout(io.StringIO()) as output):
            self.assertEqual(shutdown.main(["resume", director.name]), 0)

        start.assert_not_called()
        send.assert_called_once_with(pending, "Continue where you stopped. Resume your existing workers. Do not open new workers.",
                                     key=pending.resume_pending.key)
        remove.assert_called_once_with([str(director.id)])
        self.assertEqual(output.getvalue(), "selected: active\n")
        self.assertEqual(staff.by_id(director.id), director)

    def test_production_resume_reads_the_pause_mode_after_the_previous_resume(self) -> None:
        owner = fixture_member("showrunner", name="production")
        units = [fixture_member("unit", name=name, parent=owner.id) for name in ("alpha", "beta")]
        pause = staff.Paused("2026-11-24T08:31:00-08:00", "director", "review", keep_workers=True)
        first, second = [staff.set_hold(str(row.id), pause) for row in sorted(units, key=lambda row: row.id)]
        plain_pause = replace(pause, keep_workers=False)

        def send(row: staff.StaffRow, _text: str, *, require_live: bool = False, key: str) -> restart_support.MessageSent:
            self.assertEqual(key, f"agents-resume:{row.id}:{pause.since}")
            if row.id == first.id:
                self.assertTrue(require_live)
                _ = staff.set_hold(str(second.id), plain_pause)
            return {"kind": "sent"}

        with (patch.object(herdr, "reboot_lock", side_effect=ExitStack),
              patch.object(shutdown, "_paused_resume_needs_message", return_value=True),
              patch.object(quota_state, "no_worker_mode", return_value=False),
              patch.object(shutdown, "_message", side_effect=send) as messages,
              patch.object(shutdown_record, "remove_started"), redirect_stdout(io.StringIO())):
            self.assertEqual(shutdown.main(["resume", "--production", owner.name]), 0)

        self.assertEqual(messages.call_args_list, [call(pending_resume_row(first), "Continue where you stopped.", require_live=True,
                                                     key=f"agents-resume:{first.id}:{pause.since}"),
            call(pending_resume_row(replace(second, paused=plain_pause)),
                 "Continue where you stopped. Resume your existing workers. Do not open new workers.",
                 key=f"agents-resume:{second.id}:{pause.since}")])

    def test_production_resume_continues_after_a_held_row(self) -> None:
        owner = fixture_member("showrunner", name="production")
        units = [fixture_member("unit", name=name, parent=owner.id) for name in ("alpha", "beta", "gamma")]
        pause = staff.Paused("2026-11-24T08:31:00-08:00", "director", "review")
        first, held, last = [staff.set_hold(str(row.id), pause) for row in sorted(units, key=lambda row: row.id)]
        lock = self.staff_file.parent / "rows" / f"{held.id}.lock"
        lock.parent.mkdir(exist_ok=True)
        holder = f"agents move {held.name} claude2"

        with (lock.open("w") as stream, redirect_stdout(io.StringIO()) as output,
              redirect_stderr(io.StringIO()) as errors, patch.object(herdr, "reboot_lock", side_effect=ExitStack),
              patch.object(shutdown, "_paused_resume_needs_message", return_value=True),
              patch.object(quota_state, "no_worker_mode", return_value=False),
              patch.object(shutdown, "_message", return_value={"kind": "sent"}) as send,
              patch.object(shutdown_record, "remove_started")):
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            json.dump({"pid": os.getpid(), "command": holder}, stream)
            stream.flush()
            self.assertEqual(shutdown.main(["resume", "--production", owner.name]), 2)

        self.assertEqual(output.getvalue().splitlines(), [f"{first.name}: active", f"{last.name}: active"])
        self.assertEqual(errors.getvalue().splitlines(),
                         [f"shutdown: StaffRefused: {held.name}: another operation holds this row (pid {os.getpid()}: {holder})"])
        message = "Continue where you stopped. Resume your existing workers. Do not open new workers."
        self.assertEqual(send.call_args_list, [call(pending_resume_row(row), message, key=f"agents-resume:{row.id}:{pause.since}")
                                               for row in (first, last)])
        self.assertEqual(staff.by_id(first.id), replace(first, paused=staff.Active()))
        self.assertEqual(staff.by_id(held.id), held)
        self.assertEqual(staff.by_id(last.id), replace(last, paused=staff.Active()))


class ProducerCommandsTests(unittest.TestCase):
    def world(self) -> test_stop_resume.StopResumeTests:
        case = test_stop_resume.StopResumeTests("runTest")
        case.setUp()
        self.addCleanup(case.doCleanups)
        return case

    def register(self, case: test_stop_resume.StopResumeTests) -> str:
        identity = case.entry["session_id"]
        directory = case.root / "sessions"
        directory.mkdir(exist_ok=True)
        _ = (directory / "123.json").write_text(json.dumps({
            "pid": 123, "sessionId": identity, "name": "alpha",
            "messagingSocketPath": "", "updatedAt": 1, "procStart": "generation-1"}))
        case.live.return_value = sessions.LiveSessionProcess(123)

        def delivered(pid: int, number: int) -> None:
            if (pid, number) == (123, signal.SIGTERM):
                (directory / "123.json").unlink()
                case.live.return_value = sessions.SessionNotLive()
                case.process.return_value = sessions.SessionNotLive()

        case.kill.side_effect = delivered

        def probe(pid: int, number: int) -> None:
            if number:
                case.kill(pid, number)

        _ = case.enterContext(patch.object(os, "kill", side_effect=probe))
        original = case.client.result

        def process_info(*arguments: str, deadline: float = math.inf) -> dict[str, object]:
            if arguments == ("pane", "process-info", "--pane", "w1:p9"):
                return {"process_info": {"shell_pid": 998042, "foreground_process_group_id": 999,
                    "foreground_processes": [{"pid": 999, "name": "codex", "argv": ["codex"]}]}}
            if arguments == ("pane", "process-info", "--pane", "w1:p2") and (directory / "123.json").is_file():
                return {"process_info": {"shell_pid": 998042, "foreground_process_group_id": 123,
                    "foreground_processes": [{"pid": 123, "name": "claude", "argv": ["claude"]}]}}
            return original(*arguments, deadline=deadline)

        _ = case.enterContext(patch.object(case.client, "result", side_effect=process_info))
        return identity

    def test_stop_selects_the_producer_process_with_duplicate_or_unreported_panes(self) -> None:
        with self.subTest(selection="registry-references"):
            case = self.world()
            identity = case.entry["session_id"]
            other = fixture_runtime_id("registry-display-name")
            registered = [{"sessionId": other, "name": "registry-worker"}]
            with patch.object(sessions, "running_sessions", return_value=registered):
                self.assertEqual(stop_resume.stop_target("registry-worker"),
                                 stop_resume.SessionNotFound("registry-worker"))
                self.assertEqual(stop_resume.stop_target(other), other)
                registered[0]["name"] = identity
                self.assertEqual(stop_resume.stop_target(identity), identity)
                registered[0]["name"] = case.entry["name"]
                self.assertEqual(stop_resume.stop_target(case.entry["name"]), identity)
            case.kill.assert_not_called()
            case.doCleanups()

        for selection in ("duplicate-label", "registry-only"):
            with self.subTest(selection=selection):
                case = self.world()
                identity = self.register(case)
                if selection == "duplicate-label":
                    _ = fixture_member("producer", name="alpha", runtime_id=identity)
                    case.client.rows.append({"pane_id": "w1:p8", "terminal_id": "other-terminal",
                        "tab_id": "other-tab", "workspace_id": "w1", "label": "alpha",
                        "agent_session": {"kind": "id", "value": fixture_runtime_id("stray")}})
                else:
                    _ = case.client.rows[0].pop("agent_session")
                    pane_record.save("agents", {})
                    rejected = io.StringIO()
                    with redirect_stdout(rejected):
                        status = shutdown.main(["stop", "alpha"])
                    self.assertEqual((status, rejected.getvalue().strip(), case.kill.call_args_list),
                                     (2, "alpha: no session on this machine", []))
                output = io.StringIO()
                with patch.object(herdr, "native_process", NATIVE_PROCESS), redirect_stdout(output):
                    status = shutdown.main(["stop", identity if selection == "registry-only" else "alpha"])
                self.assertEqual(status, 0, output.getvalue())
                self.assertEqual(case.kill.call_args_list, [call(123, signal.SIGTERM)])
                self.assertIn(f"{identity}: stopped pid 123", output.getvalue())
                self.assertEqual(case.client.closed, [])
                case.doCleanups()

        from scripts.agents.test_shutdown_cli import DIRECTOR, RetireUnitTests
        from scripts.copies import copies
        from scripts.lib import conf

        case = RetireUnitTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        unit = shutdown.retirement_find_unit("alpha", "alpha")
        member = replace(unit.staff_member, id=staff.StaffId(fixture_runtime_id("fixed-retiring-holder")))
        self.assertNotEqual(member.id, member.current_session)
        unit = replace(unit, staff_member=member)
        holds = Path(os.environ["BUILD_HOLD_DIR"])
        fixed_hold = (holds / DIRECTOR).rename(holds / member.id)
        with patch.object(shutdown, "retirement_remove", return_value=[]) as remove, \
             patch.object(copies, "remove_trees") as trees, redirect_stdout(io.StringIO()) as output:
            code = shutdown.retirement_clear_worktree(unit, settings=conf.read("production"),
                record=staff.StaffSnapshot((member,)), pane_workers=[], pane=herdr.NoPane("alpha"), require_cleanup=True)
        self.assertEqual(code, 0, output.getvalue())
        self.assertIn(["build_hold.py", "release", "--holder", member.id], case.calls)
        self.assertFalse(fixed_hold.exists())
        remove.assert_called_once()
        trees.assert_called_once_with(str(member.id))

        case.doCleanups()
        from scripts.herdr.test_worker_panes import WorkerPaneTests

        case = WorkerPaneTests("runTest")
        case.setUp()
        self.addCleanup(case.doCleanups)
        _ = case.enterContext(patch.object(pane_record, "RECORD_DIR", pane_launch.record_directory()))
        owner = case.worker("selected-native")
        foreign = replace(owner, id=staff.StaffId("foreign-native"),
                          current_session=staff.StaffId("019b3a40-4250-7000-8000-000000000199"))
        case.world.add(owner, "w1:p7", label="misleading-current")
        case.world.add(foreign, "w1:p8", label=owner.name)
        case.world.locations["w1:p7"]["agent_session"] = {"value": "stale-report"}
        case.world.locations["w1:p8"]["agent_session"] = {"value": owner.current_session}
        case.world.record(owner, "w1:p8")
        entries = pane_record.read("agents")
        _ = entries.pop("w1:p7", None)
        pane_record.save("agents", entries)

        with self.subTest(record="reused pane"):
            self.assertEqual(shutdown.retirement_pane(owner, staff.snapshot()),
                             shutdown.RetiringPane("agents", "w1:p7", "terminal-w1:p7"))


    def test_stray_cleanup_uses_the_proved_owner_and_keeps_a_codex_parents_pane(self) -> None:
        world = self.world()
        client = world.client
        codex = fixture_runtime_id("stray-codex-parent")
        claude = world.entry["session_id"]
        pane = "w1:p2"
        terminal = "w1:p2-terminal"
        receipt = world.root / "panes/.producer-admission-alpha"
        _ = receipt.write_text(json.dumps({"pane": pane, "terminal": terminal}))
        native = {456: herdr.NativeAgentProcess(herdr.PaneSessionReport(codex), "start-codex", {}),
                  457: herdr.NativeAgentProcess(herdr.EnvironmentThread(claude), "start-claude", {})}
        observed = {codex: herdr.LiveConversation("codex", (456,)),
                    claude: herdr.LiveConversation("claude", (457,))}
        def read_native(pid: int, *_args: object) -> herdr.NativeAgentProcess:
            return native[pid]

        read = client.result
        with (codex_foreground_cases(world.root) as cases,
              patch.object(client, "pane_activity", return_value=herdr.PaneRunning(("codex", "claude"))),
              patch.object(herdr, "read_conversation", side_effect=observed.__getitem__),
              patch.object(herdr, "native_process", side_effect=read_native),
              patch.object(stop_resume, "producer_conflicts", return_value=[(claude, "")]),
              patch.object(shutdown, "save_producer_cleanup")):
            for case in cases:
                def result(*args: str, deadline: float = math.inf) -> dict[str, object]:
                    if args == ("pane", "process-info", "--pane", pane):
                        return {"process_info": case.info}
                    return read(*args, deadline=deadline)

                with (self.subTest(case=case.name), patch.object(client, "result", side_effect=result),
                      patch.object(shutdown, "finish_producer_cleanup", return_value=0) as finish):
                    self.assertEqual(shutdown.stray_producer_panes(client, codex),
                                     [shutdown.StrayProducerPane(pane, terminal)] if case.owner_proved else [])
                    self.assertEqual(shutdown.remove_stray_producer("alpha", claude), 0)
                    cleanup = cast(shutdown.PendingProducerCleanup, finish.call_args.args[1])
                    self.assertEqual(cleanup.panes, ())
                    self.assertFalse(cleanup.admission_matches)
        self.assertEqual(client.closed, [])

    def test_remove_stray_preserves_the_real_producer_and_completes_a_rerun(self) -> None:
        for target, failure_step in (("stray", ""), ("current", ""), ("split", ""), ("pane-admission", ""),
                ("other-admission", ""), ("foreign-session-on-pane", ""), ("no-row", ""), ("unrelated-unproved", ""),
                ("unretained-admission", ""),
                ("transcript-failure", "transcript"), ("pane-failure", "pane"),
                ("stop-progress-failure", "stop-progress"), ("transcripts-only", "")):
            with self.subTest(target=target):
                case = self.world()
                identity = self.register(case)
                current = identity if target == "current" else fixture_runtime_id("real-alpha")
                expected = (() if target == "no-row" else
                            (fixture_member("producer", name="alpha", runtime_id=current),))
                if target == "split":
                    case.client.rows.append({"pane_id": "w1:p8", "terminal_id": "other-terminal",
                        "tab_id": "w1:t9", "workspace_id": "w1", "label": "other"})
                elif target == "unrelated-unproved":
                    case.client.rows.append({"pane_id": "w1:p9", "terminal_id": "unknown-terminal",
                        "tab_id": "unknown-tab", "workspace_id": "w1", "label": "unknown"})
                receipt = case.root / "panes/.producer-admission-alpha"
                if target == "unretained-admission" or failure_step:
                    pane_record.save("agents", {})
                if failure_step in ("pane", "stop-progress"):
                    _ = case.client.rows[0].pop("agent_session")
                saved = ({"pane": "w1:p2", "terminal": "w1:p2-terminal"}
                         if target == "unretained-admission" or failure_step else
                         {"pane": "w1:p2"} if target == "pane-admission" else
                         {"session_id": fixture_runtime_id("other-admission"), "pane": "w1:p8"}
                         if target == "other-admission" else
                         {"session_id": fixture_runtime_id("other-admission"), "pane": "w1:p2",
                          "terminal": "w1:p2-terminal"} if target == "foreign-session-on-pane" else
                         {"session_id": identity, "pane": "w1:p2"})
                _ = receipt.write_text(json.dumps(saved))
                transcripts: list[Path] = []
                for account in (".claude3", ".claude4"):
                    folder = case.root / account / "projects/private"
                    folder.mkdir(parents=True)
                    path = folder / f"{identity}.jsonl"
                    _ = path.write_text("retained transcript\n")
                    transcripts.append(path)
                if target == "transcripts-only":
                    (case.root / "sessions/123.json").unlink()
                    case.live.return_value = sessions.SessionNotLive()
                    case.process.return_value = sessions.SessionNotLive()
                    case.client.rows.clear()
                    pane_record.save("agents", {})
                    receipt.unlink()
                output = io.StringIO()
                with patch.object(herdr, "native_process", NATIVE_PROCESS), redirect_stdout(output):
                    if failure_step:
                        unlink = Path.unlink

                        def fail_transcript(path: Path, missing_ok: bool = False) -> None:
                            if path == transcripts[-1]:
                                raise OSError("fixture transcript deletion failure")
                            unlink(path, missing_ok=missing_ok)

                        def fail_stop_progress(path: Path, text: str, *, new_file_mode: int | None = None) -> None:
                            if path.name == f".producer-cleanup-alpha-{identity}" and json.loads(text)["step"] == "admission":
                                raise OSError("fixture stop progress failure")
                            atomic_file.write_text_atomic(path, text, new_file_mode=new_file_mode)

                        with ExitStack() as failure:
                            if failure_step == "transcript":
                                _ = failure.enter_context(patch.object(Path, "unlink", new=fail_transcript))
                            elif failure_step == "stop-progress":
                                _ = failure.enter_context(patch.object(shutdown, "write_text_atomic", new=fail_stop_progress))
                            else:
                                _ = failure.enter_context(patch.object(case.client, "close_retained_pane", return_value=False))
                            failed = shutdown.main(["remove-stray", "alpha", identity])
                        self.assertNotEqual(failed, 0)
                        pending = herdr.json_object(cast(object, json.loads(
                            (receipt.parent / f".producer-cleanup-alpha-{identity}").read_text())), "pending cleanup")
                        self.assertEqual(pending["session"], identity)
                        self.assertEqual(pending["processes"], [{"pid": 123, "started": "generation-1", "kind": "claude"}])
                        self.assertEqual(pending["panes"], [{"pane": "w1:p2", "tab": "w1:t9", "terminal": "w1:p2-terminal"}])
                        self.assertEqual(pending["admission"], saved)
                        self.assertTrue(pending["admission_matches"])
                    status = shutdown.main(["remove-stray", "alpha", identity])
                    if failure_step:
                        self.assertIn(f"{identity}: removed stray", output.getvalue())
                    if target != "current":
                        repeated = shutdown.main(["remove-stray", "alpha", identity])
                        self.assertEqual(repeated, 0, output.getvalue())
                if target == "current":
                    self.assertNotEqual(status, 0)
                    self.assertEqual(case.kill.call_args_list, [])
                    self.assertEqual(case.client.closed, [])
                    self.assertEqual([path.read_text() for path in transcripts], ["retained transcript\n"] * 2)
                    self.assertEqual(json.loads(receipt.read_text())["session_id"], identity)
                elif target == "transcripts-only":
                    self.assertEqual(status, 0, output.getvalue())
                    self.assertEqual(case.kill.call_args_list, [])
                    self.assertEqual(case.client.closed, [])
                    self.assertEqual(sessions.transcript_locations(identity), [])
                    self.assertIn(f"{identity}: removed stray", output.getvalue())
                    self.assertIn(f"{identity}: unchanged", output.getvalue())
                else:
                    self.assertEqual(status, 0, output.getvalue())
                    self.assertEqual(case.kill.call_args_list, [call(123, signal.SIGTERM)])
                    self.assertEqual(case.client.closed, ["w1:p2"])
                    self.assertEqual(sessions.transcript_locations(identity), [])
                    if target in ("other-admission", "foreign-session-on-pane"):
                        self.assertEqual(json.loads(receipt.read_text()), saved)
                    else:
                        self.assertEqual(list(receipt.parent.glob(receipt.name)), [])
                    self.assertIn(f"{identity}: unchanged", output.getvalue())
                    if target == "split":
                        self.assertEqual([pane["pane_id"] for pane in case.client.panes()], ["w1:p8"])
                    elif target == "unrelated-unproved":
                        self.assertEqual([pane["pane_id"] for pane in case.client.panes()], ["w1:p9"])
                self.assertEqual(staff.initial_snapshot().rows, expected)
                case.doCleanups()
