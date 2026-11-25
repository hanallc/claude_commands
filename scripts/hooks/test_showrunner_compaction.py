#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""Check the showrunner resume text and the absence of PreCompact writes."""

from __future__ import annotations

from scripts.agents import remote

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Protocol, cast, final, override
from unittest.mock import call as mock_call
from unittest.mock import patch
from scripts.hooks import context_usage
from scripts.lib import conf
from scripts.lib.file_module import load_file_module
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.lint.memory_test_support import write_config_fault, write_config_fixture
from scripts.whoami import account

from scripts.production import staff
from scripts.production.staff_test_support import fixture_member, fixture_runtime_id, isolated_staff_file

SESSION = fixture_runtime_id("compaction-showrunner")
HOOK = Path(__file__).resolve().parent / "showrunner-compaction.py"
# Record any call to the former PreCompact writer.
FAKE_LOG = """\
import json, os, sys
with open(os.environ["CALLS"], "a") as output:
    output.write(json.dumps(sys.argv[1:]) + "\\n")
"""


@final
class ShowrunnerCompactionTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root = Path()
        self.doc = Path()
        self.fake = Path()
        self.calls = Path()
        self.env: dict[str, str] = {}

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.doc = self.root / "docs" / "example-production.md"
        self.doc.parent.mkdir()
        _ = self.doc.write_text("# Example\n", encoding="utf-8")
        timer = self.root / "notifier" / "showrunner-example"
        timer.mkdir(parents=True)
        _ = (timer / "conf").write_text(
            f"TARGET=session:{SESSION}\nCHECK=zsh /x/production_check.sh example\n", encoding="utf-8")
        self.fake = self.root / "update_registration.py"
        _ = self.fake.write_text(FAKE_LOG, encoding="utf-8")
        self.calls = self.root / "calls.jsonl"
        self.env = {**os.environ, "NOTIFIER_STATE_DIR": str(self.root / "notifier"),
                    "SHOWRUNNER_STATE_DIR": str(self.root / "showrunner"),
                    "SHOWRUNNER_UPDATE_REGISTRATION": str(self.fake), "CALLS": str(self.calls)}

        _ = self.enterContext(isolated_staff_file(self.root / "staff.json", self.env))
        _ = fixture_member("showrunner", name="example", runtime_id=SESSION,
                           doc=staff.LiveDoc(remote.local_machine(), self.doc))

    def run_hook(self, payload: dict[str, str], **env: str) -> str:
        result = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload), capture_output=True,
                                text=True, env={**self.env, **env}, timeout=TEST_HANG_GUARD_SECS, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def logged(self) -> list[list[str]]:
        if not self.calls.exists():
            return []
        return [cast(list[str], json.loads(line)) for line in self.calls.read_text(encoding="utf-8").splitlines()]

    def test_precompact_writes_nothing(self) -> None:
        state = self.root / "showrunner" / "example" / "session" / "showrunner_state.json"
        state.parent.mkdir(parents=True)
        _ = state.write_text('{"next": ["merge alpha"]}\n', encoding="utf-8")
        before = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(self.run_hook({"session_id": SESSION, "hook_event_name": "PreCompact",
                                        "trigger": "auto"}), "")
        self.assertEqual({path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}, before)
        self.assertEqual(self.logged(), [])

    def test_session_start_after_compaction_names_the_resume(self) -> None:
        output = self.run_hook({"session_id": SESSION, "hook_event_name": "SessionStart", "source": "compact"})
        context = cast(dict[str, dict[str, str]], json.loads(output))["hookSpecificOutput"]["additionalContext"]
        self.assertIn(f"run `/showrunner:produce {self.doc} resume`", context)
        self.assertIn("just compacted", context)
        self.assertIn("current `next` list from the state file. Act on it first.", context)

    def test_any_other_session_or_source_gets_nothing(self) -> None:
        self.assertEqual(self.run_hook({"session_id": "unit-id", "hook_event_name": "PreCompact"}), "")
        self.assertEqual(self.run_hook({"session_id": "unit-id", "hook_event_name": "SessionStart",
                                        "source": "compact"}), "")
        self.assertEqual(self.run_hook({"session_id": SESSION, "hook_event_name": "SessionStart",
                                        "source": "startup"}), "")
        self.assertEqual(self.logged(), [])


class ContextNoticeTests(unittest.TestCase):
    def test_parse_window_refuses_non_finite_values(self) -> None:
        for value in ("inf", "1e308m"):
            with self.subTest(value=value):
                self.assertIsNone(context_usage.parse_window(value))

    def test_configured_window_uses_the_nearest_claude_process_then_env_and_settings(self) -> None:
        process = context_usage.ProcessRow
        for arguments, env_window, expected in (
            (["/bin/claude", "--autocompact", "300000"], "200k", 300_000),
            (["node", "/lib/@anthropic-ai/claude-code/cli.js", "--autocompact=400k"], "", 400_000),
            (["claude", "--autocompact", "auto"], "200k", 200_000),
            (["claude"], "", 283_800),
            (["claude", "--", "--autocompact=400k"], "", 283_800),
            (["claude"], "bad", 283_800),
        ):
            table = {30: process(20, ["bash", "hook"]), 20: process(10, arguments),
                     10: process(1, ["claude", "--autocompact", "500k"])}
            with (self.subTest(arguments=arguments, env_window=env_window),
                  patch.object(os, "getppid", return_value=30),
                  patch.dict(os.environ, {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": env_window}),
                  patch.object(context_usage, "_process_reader", return_value=table.get),
                  patch.object(context_usage, "read_settings", return_value={"autoCompactWindow": 283_800})):
                self.assertEqual(context_usage.configured_window(), expected)

    def test_configured_window_reads_linux_proc_cmdline_through_hook_wrappers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            proc = Path(directory)
            for pid, parent, arguments in ((30, 20, b"/bin/sh\0hook\0"),
                                           (20, 1, b"/bin/claude\x00--autocompact\x00300k\x00")):
                path = proc / str(pid)
                path.mkdir()
                _ = (path / "stat").write_text(f"{pid} (fixture) S {parent}")
                _ = (path / "cmdline").write_bytes(arguments)
            with (patch.object(sys, "platform", "linux"),
                  patch.object(os, "getppid", return_value=30),
                  patch.dict(os.environ, {"HERDR_PROC": str(proc), "HERDR_PLATFORM": "linux"}),
                  patch.object(context_usage, "read_settings", side_effect=AssertionError("settings read"))):
                self.assertEqual(context_usage.configured_window(), 300_000)

    def test_configured_window_keeps_mac_argument_boundaries(self) -> None:
        def payload(arguments: tuple[str, ...]) -> bytes:
            return (len(arguments).to_bytes(4, sys.byteorder, signed=True)
                    + os.fsencode(arguments[0]) + b"\0\0"
                    + b"\0".join(os.fsencode(argument) for argument in arguments) + b"\0")

        for start, options, expected in ((None, (), 283_800),
                                         (None, ("--autocompact", "300000"), 300_000),
                                         (20, ("--autocompact", "300000"), 300_000)):
            arguments = ("/opt/bin/claude", "--name", "work --autocompact 100000", *options)
            listing = "30 20 /bin/sh hook\n20 10 " + " ".join(arguments) + "\n10 1 claude --autocompact=500k\n"
            kernel_rows = [payload(("/bin/sh", "hook")), payload(arguments)] if start is None else [payload(arguments)]
            with (self.subTest(start=start, options=options),
                  patch.object(sys, "platform", "darwin"),
                  patch.object(os, "getppid", return_value=30),
                  patch.dict(os.environ, {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "", "HERDR_PS": "ps"}),
                  patch.object(context_usage, "read_settings", return_value={"autoCompactWindow": 283_800}),
                  patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, listing)) as processes,
                  patch.object(account, "kern_procargs2", side_effect=kernel_rows) as kernel):
                self.assertEqual(context_usage.configured_window(pid=start), expected)
            processes.assert_called_once_with(["ps", "-axww", "-o", "pid=,ppid=,args="],
                                             capture_output=True, text=True, errors="replace", check=True, timeout=2)
            self.assertEqual([call.args for call in kernel.call_args_list],
                             [(30,), (20,)] if start is None else [(20,)])

    def test_a_showrunner_is_told_to_fill_next_and_not_to_write_a_handoff(self) -> None:
        module = load_file_module(
            "post_tool_use_context_usage", Path(__file__).resolve().parent / "post-tool-use-context-usage.py")
        spec = module.__spec__
        assert spec is not None and spec.loader is not None
        state = Path("/state/example/session/showrunner_state.json")
        notice = cast(str, module.build_context(249_000, 283_800, False, state))  # pyright: ignore[reportAny]
        self.assertIn("write no handoff doc", notice)
        self.assertIn(f"`next` list of your showrunner state file ({state})", notice)
        ordinary = cast(str, module.build_context(249_000, 283_800, False))  # pyright: ignore[reportAny]
        self.assertIn("write a handoff doc first", ordinary)


class ContextMessageHook(Protocol):
    DEBUG_LOG: Path

    def main(self) -> None: ...
    def message_enabled(self, configuration: conf.ConfigRead) -> bool: ...


class ContextMessageSwitchTests(unittest.TestCase):
    root: Path = Path()
    transcript: Path = Path()

    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        module = load_file_module("context_message_switch", HOOK.with_name("post-tool-use-context-usage.py"))
        spec = module.__spec__
        assert spec is not None and spec.loader is not None
        self.hook: ContextMessageHook = cast(ContextMessageHook, cast(object, module))

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.transcript = self.root / "transcript.jsonl"
        _ = self.transcript.write_text(json.dumps({
            "type": "assistant", "message": {"model": "claude-sonnet-4", "usage": {"input_tokens": 160_000}},
        }) + "\n", encoding="utf-8")
        own = self.transcript.with_suffix("") / "subagents" / "agent-fixture-worker.jsonl"
        own.parent.mkdir(parents=True)
        _ = own.write_bytes(self.transcript.read_bytes())
        self.hook.DEBUG_LOG = self.root / "debug.jsonl"
        self.enterContext(patch.dict(os.environ, {
            "CONF_DIR": str(self.root), "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "200k",
            "CLAUDE_CONTEXT_HOOK_MODE": "dynamic", "CLAUDE_CONTEXT_HOOK_DEBUG": "1",
        }))
        _ = write_config_fixture(self.root / "context_usage.conf", "transcript.tail_bytes=524288\n")

    def call(self, *, subagent: bool = False) -> str:
        payload = {"transcript_path": str(self.transcript), "session_id": "fixture-session"}
        if subagent:
            payload["agent_id"] = "fixture-worker"
        output = io.StringIO()
        with patch("sys.stdin", io.StringIO(json.dumps(payload))), patch("sys.stdout", output):
            self.hook.main()
        return output.getvalue()

    def test_off_keeps_the_reading_and_log_without_building_either_context_message(self) -> None:
        _ = write_config_fixture(self.root / "context_usage.conf", "message=off\n")
        with patch.object(self.hook, "showrunner_state") as state, patch.object(self.hook, "build_context") as build:
            for subagent in (False, True):
                with self.subTest(subagent=subagent):
                    self.assertEqual(self.call(subagent=subagent), "")
            state.assert_not_called()
            build.assert_not_called()
        records = [cast(dict[str, object], json.loads(line))
                   for line in self.hook.DEBUG_LOG.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([record["tokens"] for record in records], [160_000, 160_000])
        own = self.transcript.with_suffix("") / "subagents" / "agent-fixture-worker.jsonl"
        self.assertEqual([record["transcript_read"] for record in records], [str(self.transcript), str(own)])

    def test_on_retains_the_main_thread_and_subagent_messages(self) -> None:
        _ = write_config_fixture(self.root / "context_usage.conf", "message=on\n")
        with patch.object(self.hook, "showrunner_state", return_value=None):
            self.assertIn("write a handoff doc first", self.call())
            self.assertIn("return your results now", self.call(subagent=True))

    def test_missing_switch_or_bad_value_keeps_the_old_message(self) -> None:
        path = self.root / "context_usage.conf"
        for content in ("other = off\n", "message = invalid\n"):
            with self.subTest(content=content), patch.object(self.hook, "showrunner_state", return_value=None):
                _ = write_config_fault(path, "transcript.tail_bytes=524288\n" + content)
                self.assertIn("write a handoff doc first", self.call())
        path.unlink()
        self.assertTrue(self.hook.message_enabled(conf.read("context_usage")))
        with patch.object(self.hook, "showrunner_state", return_value=None):
            self.assertIn("write a handoff doc first", self.call())

    def test_an_unusable_scan_size_still_gives_the_context_message(self) -> None:
        _ = write_config_fixture(self.root / "context_usage.conf", "message=on\ntranscript.tail_bytes=0\n")
        with (patch.object(self.hook, "showrunner_state", return_value=None),
              patch.object(context_usage, "read_tail", wraps=context_usage.read_tail) as scan):
            self.assertIn("write a handoff doc first", self.call())
        scan.assert_called_once_with(self.transcript, 4 * 1024 * 1024)

    def test_the_switch_is_read_again_in_the_same_process(self) -> None:
        path = self.root / "context_usage.conf"
        _ = write_config_fixture(path, "message=off\n")
        self.assertEqual(self.call(), "")
        _ = write_config_fixture(path, "message=on\n")
        with patch.object(self.hook, "showrunner_state", return_value=None):
            self.assertIn("write a handoff doc first", self.call())

    def test_each_tool_call_reads_the_scan_size_once(self) -> None:
        path = self.root / "context_usage.conf"
        for size, scans in ((512, [512]), (1, [1, 4194304])):
            with self.subTest(size=size):
                _ = write_config_fixture(path, f"transcript.tail_bytes={size}\n")
                with (patch.object(conf, "read", wraps=conf.read) as read,
                      patch.object(context_usage, "read_tail", wraps=context_usage.read_tail) as scan):
                    self.assertEqual(self.call(), "")
                self.assertEqual([call for call in read.call_args_list if call.args == ("context_usage",)],
                                 [mock_call("context_usage")])
                self.assertEqual(scan.call_args_list, [mock_call(self.transcript, count) for count in scans])
        records = [cast(dict[str, object], json.loads(line))
                   for line in self.hook.DEBUG_LOG.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([record["tokens"] for record in records], [160_000, 160_000])


if __name__ == "__main__":
    _ = unittest.main()
