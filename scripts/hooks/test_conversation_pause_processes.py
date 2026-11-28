"""Test conversation-pause deadlines, executable exits, and concurrent processes."""

from __future__ import annotations

import fcntl
import json
import os
import signal
import subprocess
import sys
import time
import unittest
from pathlib import Path
from typing import ClassVar, cast
from unittest.mock import patch

from scripts.direct.wait_for_test_state import wait_until
from scripts.hooks import hook_budget, idle_update, pause_record
from scripts.hooks.conversation_pause_test_support import (
    LIBRARY, NOW, PROMPT_HOOK, QUESTION, SESSION, STOP_HOOK, ConversationPauseFixture,
    asked_read, cross_session, scheduled_wakeup,
)
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


_RUN_HOOK_PROBE = """
import runpy
import sys

runpy.run_path(sys.argv[1], run_name="__main__")
"""


def _block_pause_import(marker: Path, module: str = "conversation_pause") -> str:
    return f"""
import builtins
import os
import threading
from pathlib import Path

real_import = builtins.__import__

def block_pause_import(name, globals=None, locals=None, fromlist=(), level=0):
    if name == "scripts.hooks" and fromlist and {module!r} in fromlist:
        marker = Path({str(marker)!r})
        pending = marker.with_suffix(".tmp")
        pending.write_text(str(os.getpid()))
        pending.replace(marker)
        threading.Event().wait(35)
        raise SystemExit("pause import probe reached its limit")
    return real_import(name, globals, locals, fromlist, level)

builtins.__import__ = block_pause_import
"""


class ConversationPauseProcessTests(ConversationPauseFixture):
    run_in_subprocess: ClassVar[bool] = True

    def run_hook_probe(
        self, hook: Path, payload: dict[str, object], preamble: str, *, budget: int,
    ) -> subprocess.CompletedProcess[str]:
        return self.invoke(
            [sys.executable, "-c", preamble + _RUN_HOOK_PROBE, str(hook)],
            input=json.dumps(payload),
            env={**self.environment, "CONVERSATION_PAUSE_HOOK_BUDGET": str(budget)},
        )

    def test_hooks_install_no_signal_handler(self) -> None:
        _ = self.write_record({"kind": "replying", "user_wrote_at": 50})
        preamble = """
import signal

def refuse_handler(*args, **kwargs):
    raise SystemExit("hook installed a signal handler")

signal.signal = refuse_handler
"""
        cases: tuple[tuple[Path, dict[str, object]], ...] = (
            (STOP_HOOK, {"session_id": SESSION}),
            (PROMPT_HOOK, {"session_id": SESSION, "prompt": "hello"}),
        )
        for hook, payload in cases:
            with self.subTest(hook=hook.name):
                result = self.run_hook_probe(hook, payload, preamble, budget=5)
                self.assertEqual((result.returncode, result.stderr), (0, ""))

    def test_pause_state_root_override_applies_to_all_records(self) -> None:
        root = self.root / "override"
        record = pause_record.PauseRecord(SESSION, (), pause_record.Replying(NOW))
        prompts = (pause_record.ScheduledTaskPrompt("task", "prompt", False),)
        with patch.object(hook_budget, "pause_state_root", return_value=root):
            with pause_record.record_lock():
                pause_record.write_record(record)
            pause_record.write_scheduled_task_prompts(SESSION, prompts)
            self.assertEqual(idle_update.state_dir(), root / "idle-update")

        self.assertTrue((root / ".lock").is_file())
        self.assertEqual(pause_record.read_record(root / f"{SESSION}.json"), record)
        self.assertTrue((root / "scheduled-prompts" / f"{SESSION}.json").is_file())

    def test_stop_worker_imports_no_message_or_production_modules(self) -> None:
        modules_path = self.root / "stop-worker-modules.json"
        _ = self.write_record({"kind": "replying", "user_wrote_at": 50})
        preamble = f"""
import json
import os
import sys
from pathlib import Path

real_exit = os._exit

def record_modules(status):
    if "scripts.hooks.pause_record" in sys.modules or "scripts.hooks.conversation_pause" in sys.modules:
        Path({str(modules_path)!r}).write_text(json.dumps(sorted(sys.modules)))
    real_exit(status)

os._exit = record_modules
"""
        result = self.run_hook_probe(
            STOP_HOOK, {"session_id": SESSION, "session_crons": [scheduled_wakeup("task")]}, preamble, budget=5,
        )
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        modules = cast(list[str], json.loads(modules_path.read_text()))
        self.assertIn("scripts.hooks.pause_record", modules)
        self.assertNotIn("scripts.hooks.conversation_pause", modules)
        forbidden_modules = {"scripts.lib.conf", "scripts.message", "scripts.production"}
        forbidden_prefixes = ("scripts.message.", "scripts.production.")
        self.assertEqual([
            name for name in modules if name in forbidden_modules or name.startswith(forbidden_prefixes)
        ], [])
        self.assertEqual(self.record()["phase"], {
            "kind": "quiet", "user_wrote_at": 50, "reply_ended_at": NOW,
        })
        self.assertEqual(self.scheduled_prompts(), ["task"])

    def test_hook_budget_covers_the_pause_library_import(self) -> None:
        marker = self.root / "pause-import"
        _ = self.write_record({"kind": "replying", "user_wrote_at": 50})
        for hook, module in ((STOP_HOOK, "pause_record"), (PROMPT_HOOK, "conversation_pause")):
            with self.subTest(hook=hook.name):
                marker.unlink(missing_ok=True)
                result = self.run_hook_probe(hook, {"session_id": SESSION}, _block_pause_import(marker, module), budget=1)
                self.assertTrue(marker.exists())
                self.assertEqual((result.returncode, result.stdout), (0, ""))
                self.assertEqual(result.stderr, "conversation-pause: HookBudgetSpent: gave up after 1 seconds\n")

    def test_killing_hook_stops_its_worker_before_the_budget_expires(self) -> None:
        marker = self.root / "pause-worker"
        script = _block_pause_import(marker) + _RUN_HOOK_PROBE
        with subprocess.Popen(
            [sys.executable, "-c", script, str(PROMPT_HOOK)],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True,
            env={**self.environment, "CONVERSATION_PAUSE_HOOK_BUDGET": "30"},
        ) as hook:
            try:
                assert hook.stdin is not None
                _ = hook.stdin.write(json.dumps({"session_id": SESSION, "prompt": "hello"}))
                hook.stdin.close()
                wait_until(marker.exists, marker.parent, timeout=5, processes=(hook,))
                worker_pid = int(marker.read_text())
                self.assertNotEqual(worker_pid, hook.pid)
            finally:
                hook.kill()
            self.assertEqual(hook.wait(timeout=5), -signal.SIGKILL)

        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                os.kill(worker_pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.01)
        self.fail(f"Worker {worker_pid} did not stop within 3 seconds after its hook died")

    def test_schedule_write_timeout_removes_the_temporary_file(self) -> None:
        marker = self.root / "schedule-replace"
        preamble = f"""
import os
import threading
from pathlib import Path

real_replace = os.replace

def block_schedule_replace(source, destination, *args, **kwargs):
    if Path(destination).parent.name == "scheduled-prompts":
        Path({str(marker)!r}).touch()
        threading.Event().wait()
    return real_replace(source, destination, *args, **kwargs)

os.replace = block_schedule_replace
"""
        result = self.run_hook_probe(
            STOP_HOOK, {"session_id": SESSION, "session_crons": [scheduled_wakeup("task")]},
            preamble, budget=1,
        )
        self.assertTrue(marker.exists())
        self.assertEqual((result.returncode, result.stdout), (0, ""))
        self.assertEqual(result.stderr, "conversation-pause: HookBudgetSpent: gave up after 1 seconds\n")
        self.assertEqual(list(self.scheduled_prompts_path().parent.iterdir()), [])

    def test_prompt_hook_invalid_json_reports_one_line_and_exits_zero(self) -> None:
        result = subprocess.run(
            [sys.executable, str(PROMPT_HOOK)],
            input="not json",
            capture_output=True,
            text=True,
            check=False,
            env=self.environment,
            timeout=TEST_HANG_GUARD_SECS,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertEqual(len(result.stderr.splitlines()), 1)
        self.assertTrue(result.stderr.startswith("conversation-pause: JSONDecodeError:"))

    def test_prompt_hook_budget_expires_while_record_lock_is_held(self) -> None:
        _ = self.create_instance("unit-abc")
        self.pause_root.mkdir(parents=True)
        with (self.pause_root / ".lock").open("w") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            result = self.run_prompt(
                "hello",
                extra_env={"CONVERSATION_PAUSE_HOOK_BUDGET": "1"},
            )
        self.assertEqual((result.returncode, result.stdout), (0, ""))
        self.assertEqual(len(result.stderr.splitlines()), 1)
        self.assertIn("HookBudgetSpent", result.stderr)

    def test_stop_hook_budget_expires_while_record_lock_is_held(self) -> None:
        _ = self.write_record({"kind": "replying", "user_wrote_at": 50})
        with (self.pause_root / ".lock").open("w") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            result = self.run_stop(
                extra_env={"CONVERSATION_PAUSE_HOOK_BUDGET": "1"},
            )
        self.assertEqual((result.returncode, result.stdout), (0, ""))
        self.assertEqual(len(result.stderr.splitlines()), 1)
        self.assertIn("HookBudgetSpent", result.stderr)

    def test_prompt_hook_budget_expires_during_notifier_stop(self) -> None:
        instance = self.create_instance("unit-abc")
        result = self.run_prompt(
            "hello",
            extra_env={
                "CONVERSATION_PAUSE_HOOK_BUDGET": "1",
                "STOP_HOLD": "1",
            },
        )
        self.assertEqual((result.returncode, result.stdout), (0, ""))
        self.assertEqual(len(result.stderr.splitlines()), 1)
        self.assertIn("HookBudgetSpent", result.stderr)
        self.assertTrue((instance / "state").read_text().startswith("ENABLED=1\n"))

    def test_tick_releases_record_lock_during_session_lookup(self) -> None:
        marker = self.root / "lookup-started"
        release = self.root / "lookup-release"
        sessions = self.root / "sessions-slow.py"
        _ = sessions.write_text(
            "".join((
                "#!/usr/bin/env python3\n",
                "import os\n",
                f"with open({str(LIBRARY.parents[1] / 'lib/package_root.py')!r}) as package_root: exec(package_root.read(), {{'__file__': package_root.name}})\n",
                "from scripts.direct.wait_for_test_state import wait_until\n",
                "from pathlib import Path\n",
                "Path(os.environ['LOOKUP_MARKER']).touch()\n",
                "release = Path(os.environ['LOOKUP_RELEASE'])\n",
                f"wait_until(release.exists, release.parent, {TEST_HANG_GUARD_SECS})\n",
                "print(os.environ['SESSION_SOCKET'])\n",
            ))
        )
        sessions.chmod(0o755)
        _ = self.write_record(
            {"kind": "quiet", "user_wrote_at": 10, "reply_ended_at": 100}
        )
        environment = {
            **self.environment,
            "CONVERSATION_PAUSE_SESSIONS": str(sessions),
            "CONVERSATION_PAUSE_NOW_EPOCH": "200",
            "LOOKUP_MARKER": str(marker),
            # The lookup holds until the lock is tried, so a loaded machine cannot finish it first.
            "LOOKUP_RELEASE": str(release),
        }
        process = subprocess.Popen(
            [sys.executable, str(LIBRARY), "tick"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
        )
        try:
            self.wait_for_path(marker)
            with (self.pause_root / ".lock").open("w") as lock_file:
                fcntl.flock(
                    lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB
                )
            release.touch()
            _, stderr = process.communicate(timeout=TEST_HANG_GUARD_SECS)
        finally:
            if process.poll() is None:
                process.kill()
                _ = process.wait(timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(process.returncode, 0, stderr)

    def test_prompt_before_send_return_stays_read_and_yes_resumes(self) -> None:
        instance = self.create_instance("unit-abc", enabled=False, paused=True)
        _ = self.write_record(
            {"kind": "question_pending", "due_at": 100},
            instances=(instance.name,),
        )

        tick = self.run_tick_with_prompt_during_send(
            cross_session("conversation-pause", QUESTION),
            now=150,
        )

        self.assertEqual(tick.returncode, 0, tick.stderr)
        self.assertEqual(self.record()["phase"], asked_read(150, 0))
        stopped = self.run_stop(now=160)
        self.assertEqual(
            (stopped.returncode, stopped.stdout, stopped.stderr),
            (0, "", ""),
        )
        self.assertEqual(self.record()["phase"], asked_read(150, 1))
        answer = self.parsed_reply(self.run_prompt("yes", now=170))
        self.assertEqual(
            answer["systemMessage"],
            "Automatic updates are back on: status reports.",
        )
        self.assertTrue((instance / "state").read_text().startswith("ENABLED=1\n"))

    def test_prompt_before_send_return_reserves_yes_after_newer_reply(self) -> None:
        instance = self.create_instance("unit-abc", enabled=False, paused=True)
        _ = self.write_record(
            {"kind": "question_pending", "due_at": 100},
            instances=(instance.name,),
        )
        tick = self.run_tick_with_prompt_during_send(
            cross_session("conversation-pause", QUESTION),
            now=150,
        )
        self.assertEqual(tick.returncode, 0, tick.stderr)

        for ended_at in (160, 170):
            stopped = self.run_stop(now=ended_at)
            self.assertEqual(
                (stopped.returncode, stopped.stdout, stopped.stderr),
                (0, "", ""),
            )
        asked = asked_read(150, 2)
        self.assertEqual(self.record()["phase"], asked)

        answer = self.run_prompt("yes", now=180)
        self.assertEqual(
            (answer.returncode, answer.stdout, answer.stderr),
            (0, "", ""),
        )
        self.assertEqual(self.record()["phase"], asked)
        self.assertTrue((instance / "state").read_text().startswith("ENABLED=0\n"))

    def test_delivery_does_not_overwrite_newer_replying_phase(self) -> None:
        marker = self.root / "send-started"
        release = self.root / "send-release"
        send = self.root / "send-slow.py"
        _ = send.write_text(
            "".join((
                "#!/usr/bin/env python3\n",
                "import os\n",
                f"with open({str(LIBRARY.parents[1] / 'lib/package_root.py')!r}) as package_root: exec(package_root.read(), {{'__file__': package_root.name}})\n",
                "from scripts.direct.wait_for_test_state import wait_until\n",
                "from pathlib import Path\n",
                "Path(os.environ['SEND_MARKER']).touch()\n",
                "release = Path(os.environ['SEND_RELEASE'])\n",
                f"wait_until(release.exists, release.parent, {TEST_HANG_GUARD_SECS})\n",
                "print('SENT: delivered')\n",
            ))
        )
        send.chmod(0o755)
        instance = self.create_instance("unit-abc", enabled=False, paused=True)
        _ = self.create_instance("conversation-pause", run="tick")
        _ = self.write_record(
            {"kind": "question_pending", "due_at": 100},
            instances=("unit-abc",),
        )
        environment = {
            **self.environment,
            "CONVERSATION_PAUSE_SEND": str(send),
            "CONVERSATION_PAUSE_NOW_EPOCH": "150",
            "SEND_MARKER": str(marker),
            # The send holds until the typed prompt is done, however slowly a loaded machine runs it.
            "SEND_RELEASE": str(release),
        }
        process = subprocess.Popen(
            [sys.executable, str(LIBRARY), "tick"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
        )
        try:
            self.wait_for_path(marker)
            typed = self.run_prompt("new topic", now=160)
            release.touch()
            self.assertEqual(
                (typed.returncode, typed.stdout, typed.stderr), (0, "", "")
            )
            _, stderr = process.communicate(timeout=TEST_HANG_GUARD_SECS)
        finally:
            if process.poll() is None:
                process.kill()
                _ = process.wait(timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(process.returncode, 0, stderr)
        self.assertEqual(
            self.record()["phase"], {"kind": "replying", "user_wrote_at": 160}
        )
        self.assertTrue((instance / "state").read_text().startswith("ENABLED=0\n"))

    def test_missing_session_registry_keeps_pause_record_and_report_off(self) -> None:
        instance = self.create_instance("unit-abc", enabled=False, paused=True)
        _ = self.create_instance("conversation-pause", run="tick")
        _ = self.write_record(
            {"kind": "quiet", "user_wrote_at": 10, "reply_ended_at": 100},
            instances=(instance.name,),
        )
        environment = {
            **self.environment,
            "CONVERSATION_PAUSE_NOW_EPOCH": "1000",
            "NOTIFIER_SESSIONS_DIR": str(self.root / "missing-sessions"),
        }
        _ = environment.pop("CONVERSATION_PAUSE_SESSIONS")
        result = subprocess.run(
            [sys.executable, str(LIBRARY), "tick"],
            capture_output=True,
            text=True,
            check=False,
            env=environment,
            timeout=TEST_HANG_GUARD_SECS,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.record()["phase"], {"kind": "question_pending", "due_at": 1_000}
        )
        self.assertTrue((instance / "state").read_text().startswith("ENABLED=0\n"))


if __name__ == "__main__":
    _ = unittest.main()
