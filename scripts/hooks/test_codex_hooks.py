"""Codex hook installation and trust use a sandboxed JSON-RPC app-server."""

from __future__ import annotations

from scripts.lint.memory_test_support import write_config_fixture

import base64
import hashlib
import io
import json
import os
import re
import shlex
import signal
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import cast, override
from unittest import mock

from scripts.copies.test_copy_guard import argv_policy_cases, repair_policy_cases, shell_policy_cases, unresolved_policy_cases

from scripts.hooks import codex_hooks
from scripts.hooks.test_showrunner_guard import GuardFixture, STUB as BASE_STUB
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.production.cli_fixture import invoke_cli, process_boundary, process_boundary_for_case


INSTALLER = Path(__file__).with_name("codex_hooks.py")
FN_COMMAND = '"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/hooks/post-tool-use-fn-length.py"'
RETIRED_COMMAND = FN_COMMAND.replace("fn-length", "mul-add")
FN_HANDLER: dict[str, object] = {"type": "command", "command": FN_COMMAND, "timeout": 10}
BERTH_COMMAND = '"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/hooks/pre-tool-use-berth.py"'
BERTH_GROUP: dict[str, object] = {"matcher": "apply_patch",
                                  "hooks": [{"type": "command", "command": BERTH_COMMAND, "timeout": 30}]}
GUARD_COMMAND = '"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/hooks/../copies/copy_guard.py"'
GUARD_GROUP: dict[str, object] = {"matcher": "Bash",
                                  "hooks": [{"type": "command", "command": GUARD_COMMAND, "timeout": 10}]}
HOLD_COMMAND = '"$HOME/.claude/scripts/hooks/pre-tool-use-build-hold-guard.sh"'
HOLD_GROUP: dict[str, object] = {"matcher": "Bash",
                                "hooks": [{"type": "command", "command": HOLD_COMMAND, "timeout": 10}]}
PRE_GROUPS = [BERTH_GROUP, GUARD_GROUP, HOLD_GROUP]
STE_COMMAND = '"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/hooks/session-start-ste.py"'
STE_GROUP: dict[str, object] = {"matcher": "*",
                               "hooks": [{"type": "command", "command": STE_COMMAND, "timeout": 10}]}
QUEUE_COMMAND = '"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/hooks/codex-queue-drain.py"'
QUEUE_GROUP: dict[str, object] = {"matcher": "*",
                                "hooks": [{"type": "command", "command": QUEUE_COMMAND, "timeout": 10}]}
COMPACTION_COMMAND = ('"$HOME/.claude/scripts/lib/py" '
                      '"$HOME/.claude/scripts/hooks/pre-compact-marker.py" --family codex')
COMPACTION_GROUP: dict[str, object] = {"matcher": "*",
    "hooks": [{"type": "command", "command": COMPACTION_COMMAND, "timeout": 10}]}
CLEAR_TURN_COMMAND = '"$HOME/.claude/scripts/hooks/compaction-marker-clear.sh"'
CLEAR_END_COMMAND = '"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/hooks/compaction-marker-clear.py"'
CLEAR_TURN_GROUP: dict[str, object] = {"matcher": "*",
    "hooks": [{"type": "command", "command": CLEAR_TURN_COMMAND, "timeout": 10}]}
CLEAR_END_GROUP: dict[str, object] = {"matcher": "*",
    "hooks": [{"type": "command", "command": CLEAR_END_COMMAND, "timeout": 10}]}
TURN_END_COMMAND = '"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/hooks/../herdr/turn_end.py"'
TURN_END_GROUP: dict[str, object] = {"matcher": "*",
    "hooks": [{"type": "command", "command": TURN_END_COMMAND, "timeout": 10}]}
LIFECYCLE_GROUPS = {"SessionStart": [STE_GROUP, QUEUE_GROUP], "SubagentStart": [STE_GROUP],
                    "PreCompact": [COMPACTION_GROUP], "UserPromptSubmit": [CLEAR_TURN_GROUP],
                    "PostCompact": [CLEAR_END_GROUP], "Stop": [TURN_END_GROUP]}


INTERRUPTED_REMOVAL = """import os, runpy, signal, sys
from pathlib import Path
from scripts.lib import atomic_file

stop_point, journal_name, registry_name, *installer = sys.argv[1:]
journal = Path(journal_name)
registry = Path(registry_name)
write_text_atomic = atomic_file.write_text_atomic
sync_folder = atomic_file.sync_folder
latest_registry = registry.read_bytes()
journal_writes = 0
registry_writes = 0
journal_stop = {"after journal write": 1, "after normalization journal": 1, "after migration journal": 2}
registry_stop = {"after registry replacement": 1, "after normalization replacement": 1, "after final replacement": 2}

def stop_installer():
    os.kill(os.getpid(), signal.SIGKILL)

def write_then_stop(path, *args, **kwargs):
    global journal_writes
    write_text_atomic(path, *args, **kwargs)
    if path == journal:
        journal_writes += 1
        if journal_writes == journal_stop.get(stop_point):
            stop_installer()
    if stop_point == "after recovery record" and path.name.startswith(".codex-hooks-install-recovery-"):
        stop_installer()

def sync_then_stop(folder):
    global latest_registry, registry_writes
    sync_folder(folder)
    if folder == registry.parent:
        content = registry.read_bytes()
        if content != latest_registry:
            latest_registry = content
            registry_writes += 1
            if registry_writes == registry_stop[stop_point]:
                stop_installer()

def stop_before_unlink(event, args):
    if event == "os.remove" and args[0] == journal_name:
        stop_installer()

if stop_point.startswith("after trust write"):
    os.environ["STUB_INTERRUPT_INSTALLER"] = str(os.getpid())
    if ":" in stop_point:
        _, home, write = stop_point.split(":")
        os.environ["STUB_INTERRUPT_HOME"] = home
        os.environ["STUB_INTERRUPT_WRITE"] = write
elif stop_point == "before journal removal":
    sys.addaudithook(stop_before_unlink)
elif stop_point in registry_stop:
    atomic_file.sync_folder = sync_then_stop
else:
    atomic_file.write_text_atomic = write_then_stop

sys.argv = installer
runpy.run_path(installer[0], run_name="__main__")
"""

STUB = BASE_STUB.replace(
    '    user_config = str(hooks_file.with_name("config.toml"))',
    '''    for key in state.get("disabled", []):
        config["hooks"]["state"].setdefault(key, {})["enabled"] = False
    for key, fields in state.get("extra_settings", {}).items():
        config["hooks"]["state"].setdefault(key, {}).update(fields)
    user_config = str(hooks_file.with_name("config.toml"))''',
).replace(
    '            else:\n                state["trust"][key] = value["trusted_hash"]',
    '''            elif "trusted_hash" in value:
                state["trust"][key] = value["trusted_hash"]
            else:
                state["trust"].pop(key, None)
            if value is None or value.get("enabled") is not False:
                if key in state.get("disabled", []):
                    state["disabled"].remove(key)
            elif key not in state.setdefault("disabled", []):
                state["disabled"].append(key)
            extras = state.setdefault("extra_settings", {})
            fields = {field: setting for field, setting in (value or {}).items()
                      if field not in ("enabled", "trusted_hash")}
            if fields:
                extras[key] = fields
            else:
                extras.pop(key, None)''',
).replace(
    '"enabled": os.environ.get("STUB_DISABLED") != "1" and',
    '"enabled": key not in state.get("disabled", []) and os.environ.get("STUB_DISABLED") != "1" and',
).replace(
    '        if installer_pid == str(os.getppid()):',
    '''        write_count = sum(call["method"] == "config/batchWrite" for call in state["calls"])
        target = os.environ.get("STUB_INTERRUPT_WRITE")
        if (installer_pid == str(os.getppid()) and os.environ.get("STUB_INTERRUPT_HOME", home) == home
                and (target is None or write_count == int(target))):''',
)

RECOVERY_STUB = STUB.replace(
    '        result = read_config(state, params)',
    '''        if os.environ.get("STUB_FAILED_READ") == home:
            print(json.dumps({"id": request["id"], "error": {
                "code": -32602, "message": "fixture config read failed"
            }}), flush=True)
            continue
        result = read_config(state, params)''',
).replace(
    '        write_trust(state, params["edits"])',
    '''        if os.environ.get("STUB_UNSAVED_WRITE") != home:
            write_trust(state, params["edits"])
        if os.environ.get("STUB_COVERAGE_HOME") == home:
            state["trust"][os.environ["STUB_COVERAGE_KEY"]] = "added after planning"''',
)


class CodexHookInstallerTests(unittest.TestCase):
    root: Path = Path()
    home: Path = Path()
    codex_home: Path = Path()
    hooks_file: Path = Path()
    state_file: Path = Path()
    environment: dict[str, str] = {}

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(dir=INSTALLER.resolve().parents[2])))
        self.home = self.root / "home"
        self.home.mkdir()
        self.codex_home = self.root / "codex-home"
        self.codex_home.mkdir()
        self.hooks_file = self.codex_home / "hooks.json"
        self.state_file = self.root / "stub-state.json"
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        executable = bin_dir / "codex"
        _ = executable.write_text(STUB.replace("__PYTHON__", sys.executable))
        executable.chmod(0o755)
        self.environment = {
            **os.environ,
            "HOME": str(self.home),
            "CODEX_HOME": str(self.codex_home),
            "FN_LENGTH_HOOK_STATE": str(self.root / "fn-state"),
            "STUB_STATE": str(self.state_file),
            "PATH": str(bin_dir),
        }
        copies_config = self.root / "fixture-config/copies.conf"
        _ = write_config_fixture(copies_config, "guard_timeout_seconds=10\n")
        self.environment["COPIES_CONFIG"] = str(copies_config)
        _ = self.environment.pop("CODEX_BIN", None)
        _ = self.environment.pop("STUB_DISABLED", None)
        _ = self.environment.pop("STUB_DISABLED_COMMAND", None)
        _ = self.environment.pop("STUB_REFUSE", None)

    def run_installer(self, action: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        if process_boundary_for_case(self):
            return subprocess.run(
                [sys.executable, str(INSTALLER), action],
                capture_output=True, text=True, check=False,
                env=self.environment, cwd=cwd, timeout=TEST_HANG_GUARD_SECS,
            )
        return invoke_cli(codex_hooks, codex_hooks.main, [action], self.environment,
                          cwd if cwd is not None else Path.cwd())

    def run_bash_policy(self, command: str | list[str]) -> subprocess.CompletedProcess[str]:
        hook = next(hook for hook in codex_hooks.HOOKS if hook.name == "scratch-guard")
        script = shlex.split(hook.command)[-1].replace("$HOME/.claude", str(INSTALLER.resolve().parents[2]))
        return subprocess.run([str(INSTALLER.parents[1] / "lib/py"), script], input=json.dumps({
            "tool_name": "Bash", "tool_input": {"command": command}, "cwd": "/"}),
            env={**self.environment, "PATH": os.environ["PATH"]}, text=True,
            capture_output=True, timeout=TEST_HANG_GUARD_SECS, check=False)

    def test_registered_bash_policy_refuses_clones_and_allows_the_checkout_service(self) -> None:
        hook = next(hook for hook in codex_hooks.HOOKS if hook.name == "scratch-guard")
        self.assertEqual((hook.event, hook.matcher), ("PreToolUse", "Bash"))
        for command, expected in (("git clone source destination", 2),
                                  ("copies.py run --repo . --commit HEAD -- true", 0)):
            with self.subTest(command=command):
                result = self.run_bash_policy(command)
                self.assertEqual(result.returncode, expected, result.stderr)
                if expected:
                    self.assertIn("copies.py run", result.stderr)

    def test_registered_policy_distinguishes_literal_and_executable_substitutions(self) -> None:
        for case in shell_policy_cases():
            with self.subTest(command=case.command):
                result = self.run_bash_policy(case.command)
                self.assertEqual(result.returncode, 2 if case.operation else 0, result.stderr)
                if case.operation:
                    self.assertTrue(result.stderr.startswith(case.operation + ":"), result.stderr)
                else:
                    self.assertEqual(result.stdout, "")

    def test_interpreter_vectors_and_unresolved_programs_reach_the_codex_policy(self) -> None:
        for command, operation in argv_policy_cases():
            with self.subTest(command=command):
                result = self.run_bash_policy(command)
                self.assertEqual(result.returncode, 2 if operation else 0, result.stderr)
                self.assertEqual(result.stderr.split(":", 1)[0] if result.stderr else "", operation)
        for command, reason in unresolved_policy_cases():
            with self.subTest(command=command):
                result = self.run_bash_policy(command)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertTrue(result.stderr.startswith("unresolved relevant operation: " + reason + ":"), result.stderr)

    def test_recorded_commands_and_interpreter_descriptor_context_matrix(self) -> None:
        for case in repair_policy_cases():
            with self.subTest(case=case.name, provenance=case.provenance):
                result = self.run_bash_policy(case.command)
                self.assertEqual(result.returncode, 2 if case.expected else 0, result.stderr)
                if case.expected:
                    self.assertTrue(result.stderr.startswith(case.expected + ":"), result.stderr)
                else:
                    self.assertNotIn("copies.py run", result.stderr)

    def state(self) -> dict[str, object]:
        return cast(dict[str, object], json.loads(self.state_file.read_text()))

    def calls(self) -> list[dict[str, object]]:
        return cast(list[dict[str, object]], self.state()["calls"])

    def groups(self, event: str = "PostToolUse") -> list[dict[str, object]]:
        config = cast(dict[str, object], json.loads(self.hooks_file.read_text()))
        hooks = cast(dict[str, object], config["hooks"])
        return cast(list[dict[str, object]], hooks[event])

    def trust(self) -> dict[str, str]:
        return cast(dict[str, str], self.state()["trust"])

    def clear_calls(self) -> None:
        state = self.state()
        state["calls"] = []
        _ = self.state_file.write_text(json.dumps(state))

    def save_trust(self, trust: dict[str, str]) -> None:
        state = self.state()
        state["trust"] = trust
        _ = self.state_file.write_text(json.dumps(state))

    def listed_handlers(self) -> list[dict[str, object]]:
        with mock.patch.dict(os.environ, self.environment, clear=True):
            server = codex_hooks.AppServer(str(self.root / "bin/codex"), str(self.codex_home))
            try:
                _ = server.call("initialize", {})
                result = server.call("hooks/list", {"cwds": [str(self.home)]})
            finally:
                server.close()
        workspaces = cast(list[dict[str, object]], result["data"])
        return cast(list[dict[str, object]], workspaces[0]["hooks"])

    def add_retired_custom_group(self, command: str, event: str = "PostToolUse") -> None:
        config = cast(dict[str, object], json.loads(self.hooks_file.read_text()))
        hooks = cast(dict[str, list[dict[str, object]]], config["hooks"])
        hooks[event].append({"matcher": "apply_patch", "hooks": [
            {"type": "command", "command": RETIRED_COMMAND, "timeout": 99},
            {"type": "command", "command": command, "timeout": 20},
        ]})
        _ = self.hooks_file.write_text(json.dumps(config))

    @process_boundary
    def test_fresh_install_creates_group_and_records_trust(self) -> None:
        result = self.run_installer("install")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.groups(), [
            {"matcher": "apply_patch", "hooks": [FN_HANDLER]},
            QUEUE_GROUP,
        ])
        self.assertTrue(self.hooks_file.read_text().endswith("\n"))
        self.assertIn('\n  "hooks":', self.hooks_file.read_text())
        calls = self.calls()
        self.assertEqual([call["method"] for call in calls],
                         ["initialize", "hooks/list", "config/batchWrite", "hooks/list",
                          "config/batchWrite", "hooks/list", "config/batchWrite", "hooks/list",
                          "config/batchWrite", "hooks/list", "config/batchWrite", "hooks/list",
                          "config/batchWrite", "hooks/list", "config/batchWrite", "hooks/list",
                          "config/batchWrite", "hooks/list", "config/batchWrite", "hooks/list",
                          "config/batchWrite", "hooks/list", "config/batchWrite", "hooks/list",
                          "config/batchWrite", "hooks/list"])
        initialize = cast(dict[str, object], calls[0]["params"])
        client = cast(dict[str, object], initialize["clientInfo"])
        capabilities = cast(dict[str, object], initialize["capabilities"])
        self.assertEqual(client["name"], "codex_hooks")
        self.assertEqual(capabilities["experimentalApi"], True)
        self.assertEqual(calls[1]["params"], {"cwds": [str(self.home)]})
        write = cast(dict[str, object], calls[2]["params"])
        self.assertEqual(write["reloadUserConfig"], True)
        edits = cast(list[dict[str, object]], write["edits"])
        self.assertEqual(len(edits), 1)
        self.assertEqual(edits[0]["keyPath"], "hooks.state")
        self.assertEqual(edits[0]["mergeStrategy"], "upsert")
        self.assertEqual(self.groups("PreToolUse"), PRE_GROUPS)
        trust = self.trust()
        self.assertEqual(len(trust), 12)
        self.assertIn("fn-length codex hook: trusted " + next(iter(trust)), result.stdout)
        self.assertIn(f"berth codex hook: trusted {self.hooks_file}:pre_tool_use:0:0", result.stdout)
        self.assertIn(f"scratch-guard codex hook: trusted {self.hooks_file}:pre_tool_use:1:0", result.stdout)
        self.assertIn(f"build-hold-guard codex hook: trusted {self.hooks_file}:pre_tool_use:2:0", result.stdout)
        self.assertEqual(self.groups("SessionStart"), [STE_GROUP, QUEUE_GROUP])
        self.assertEqual(self.groups("SubagentStart"), [STE_GROUP])
        self.assertEqual(self.groups("PreCompact"), [COMPACTION_GROUP])
        self.assertIn(f"compaction-marker codex hook: trusted {self.hooks_file}:pre_compact:0:0", result.stdout)
        for event, listed_event, group, name, key, matcher in (
            ("UserPromptSubmit", "userPromptSubmit", CLEAR_TURN_GROUP, "compaction-clear-turn", "user_prompt_submit", None),
            ("PostCompact", "postCompact", CLEAR_END_GROUP, "compaction-clear-end", "post_compact", "*"),
            ("Stop", "stop", TURN_END_GROUP, "turn-end", "stop", None),
        ):
            self.assertEqual(self.groups(event), [group])
            self.assertIn(f"{name} codex hook: trusted {self.hooks_file}:{key}:0:0", result.stdout)
            listed = [entry for entry in self.listed_handlers() if entry["eventName"] == listed_event]
            self.assertEqual([(entry["matcher"], entry["trustStatus"], entry["enabled"]) for entry in listed],
                             [(matcher, "trusted", True)])
        self.assertIn(f"ste codex hook: trusted {self.hooks_file}:session_start:0:0", result.stdout)
        self.assertIn(f"ste-subagent codex hook: trusted {self.hooks_file}:subagent_start:0:0", result.stdout)
        self.assertEqual(self.state()["cwd"], str(self.home))
        self.assertEqual(self.state()["argv"], ["app-server"])

    def test_existing_mac_groups_keep_their_indexes(self) -> None:
        old_groups = [
            {"matcher": "Edit|Write", "hooks": [{"type": "command", "command": "random-ack", "timeout": 5}]},
            {"matcher": "apply_patch", "hooks": [{"type": "command", "command": "basedpyright", "timeout": 20}]},
        ]
        _ = self.hooks_file.write_text(json.dumps({"hooks": {"PostToolUse": old_groups}}))
        result = self.run_installer("install")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.groups()[:2], old_groups)
        self.assertEqual(self.groups()[2:], [
            {"matcher": "apply_patch", "hooks": [FN_HANDLER]},
            QUEUE_GROUP,
        ])
        self.assertEqual(len(self.groups()), 4)

    def test_install_keeps_the_hooks_link_and_adds_missing_hooks_to_its_target(self) -> None:
        shared = self.root / "shared"
        shared.mkdir()
        target = shared / "hooks.json"
        existing = {"matcher": "apply_patch", "hooks": [FN_HANDLER]}
        _ = target.write_text(json.dumps({"hooks": {"PostToolUse": [existing]}}))
        self.hooks_file.symlink_to(Path("..") / "shared" / "hooks.json")
        original_link = os.readlink(self.hooks_file)

        result = self.run_installer("install")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(self.hooks_file.is_symlink())
        self.assertEqual(os.readlink(self.hooks_file), original_link)
        self.assertEqual(self.hooks_file.resolve(), target)
        self.assertEqual(self.groups(), [existing, QUEUE_GROUP])
        self.assertEqual(target.read_bytes(), self.hooks_file.read_bytes())
        self.assertEqual(len(self.trust()), 12)
        self.assertEqual({entry.name for entry in shared.iterdir()}, {"hooks.json"})

    def test_install_creates_the_temporary_hooks_file_beside_the_link_target(self) -> None:
        target = self.root / "shared" / "hooks.json"
        self.hooks_file.symlink_to(target)
        output = io.StringIO()
        with (
            mock.patch.dict(os.environ, self.environment, clear=True),
            mock.patch.object(sys, "argv", ["codex_hooks.py", "install"]),
            mock.patch.object(tempfile, "mkstemp", wraps=tempfile.mkstemp) as create,
            redirect_stdout(output),
        ):
            result = codex_hooks.main()
        self.assertEqual(result, 0, output.getvalue())
        create.assert_called_once_with(prefix=".hooks-", suffix=".json", dir=str(target.parent.resolve()))
        self.assertTrue(self.hooks_file.is_symlink())
        self.assertEqual(self.groups(), [
            {"matcher": "apply_patch", "hooks": [FN_HANDLER]},
            QUEUE_GROUP,
        ])

    def _assert_existing_fn_group_keeps_trust(self, old_groups: list[dict[str, object]]) -> None:
        fn_group = {"matcher": "apply_patch", "hooks": [FN_HANDLER]}
        original = [*old_groups, fn_group]
        _ = self.hooks_file.write_text(json.dumps({"hooks": {"PostToolUse": original}}))
        seeded = self.run_installer("install")
        self.assertEqual(seeded.returncode, 0, seeded.stdout + seeded.stderr)
        fn_key = next(key for key in self.trust() if key.endswith(f":{len(old_groups)}:0"))
        fn_hash = self.trust()[fn_key]
        _ = self.hooks_file.write_text(json.dumps({"hooks": {"PostToolUse": original}}))
        self.save_trust({fn_key: fn_hash})
        self.clear_calls()

        result = self.run_installer("install")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.groups(), [*original, QUEUE_GROUP])
        self.assertEqual(self.trust()[fn_key], fn_hash)
        writes = [call for call in self.calls() if call["method"] == "config/batchWrite"]
        written = [key for call in writes
                   for edit in cast(list[dict[str, object]], cast(dict[str, object], call["params"])["edits"])
                   for key in cast(dict[str, object], edit["value"])]
        self.assertEqual(written, [f"{self.hooks_file}:pre_tool_use:{index}:0" for index in range(3)] + [
            f"{self.hooks_file}:session_start:0:0", f"{self.hooks_file}:subagent_start:0:0",
            f"{self.hooks_file}:session_start:1:0", f"{self.hooks_file}:post_tool_use:{len(original)}:0",
            f"{self.hooks_file}:pre_compact:0:0", f"{self.hooks_file}:user_prompt_submit:0:0",
            f"{self.hooks_file}:post_compact:0:0", f"{self.hooks_file}:stop:0:0",
        ])
        self.assertEqual(len(self.trust()), 12)

    def test_existing_natedev_fn_group_keeps_trust(self) -> None:
        self._assert_existing_fn_group_keeps_trust([])

    def test_existing_mac_fn_group_keeps_trust(self) -> None:
        old_groups: list[dict[str, object]] = [
            {"matcher": "Edit|Write", "hooks": [{"type": "command", "command": "random-ack", "timeout": 5}]},
            {"matcher": "apply_patch", "hooks": [{"type": "command", "command": "basedpyright", "timeout": 20}]},
        ]
        self._assert_existing_fn_group_keeps_trust(old_groups)

    def test_second_install_is_idempotent_and_does_not_retrust(self) -> None:
        first = self.run_installer("install")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertEqual(self.groups("PreToolUse").count(HOLD_GROUP), 1)
        before = self.hooks_file.read_bytes()
        modified = self.hooks_file.stat().st_mtime_ns
        trust = self.trust()
        state = self.state()
        state["calls"] = []
        _ = self.state_file.write_text(json.dumps(state))
        second = self.run_installer("install")
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertEqual(self.hooks_file.read_bytes(), before)
        self.assertEqual(self.hooks_file.stat().st_mtime_ns, modified)
        self.assertEqual(self.groups("PreToolUse").count(HOLD_GROUP), 1)
        self.assertEqual(self.trust(), trust)
        self.assertEqual([call["method"] for call in self.calls()], ["initialize", "hooks/list"])
        checked = self.run_installer("check")
        self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)
        self.assertIn("build-hold-guard codex hook: trusted", checked.stdout.splitlines())
        hold = next(entry for entry in self.listed_handlers() if entry["command"] == HOLD_COMMAND)
        self.assertEqual((hold["eventName"], hold["matcher"], hold["trustStatus"], hold["enabled"]),
                         ("preToolUse", "Bash", "trusted", True))

    def test_cleanup_preserves_trusted_custom_handlers_at_new_keys(self) -> None:
        installed = self.run_installer("install")
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        installed_registry = self.hooks_file.read_bytes()
        installed_trust = self.trust()
        self.add_retired_custom_group("custom-post")
        self.add_retired_custom_group("custom-pre", "PreToolUse")
        trusted = self.trust()
        custom_hashes: dict[str, str] = {}
        for entry in self.listed_handlers():
            command = entry["command"]
            if command in {"custom-post", "custom-pre"}:
                key = cast(str, entry["key"])
                current_hash = cast(str, entry["currentHash"])
                trusted[key] = current_hash
                custom_hashes[cast(str, command)] = current_hash
        self.save_trust(trusted)
        self.clear_calls()

        result = self.run_installer("install")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        writes = [call for call in self.calls() if call["method"] == "config/batchWrite"]
        self.assertEqual(len(writes), 2)
        expected = {
            f"{self.hooks_file}:post_tool_use:2:0": custom_hashes["custom-post"],
            f"{self.hooks_file}:pre_tool_use:3:0": custom_hashes["custom-pre"],
        }
        cleaned = {key for key, value in trusted.items() if value in custom_hashes.values()}
        staged, final = (cast(dict[str, object], write["params"]) for write in writes)
        self.assertEqual(staged["reloadUserConfig"], True)
        self.assertEqual(final["reloadUserConfig"], True)
        expected_stage = {key: {"enabled": False} for key in expected.keys() | cleaned}
        expected_final = {**dict.fromkeys(cleaned), **{key: {"trusted_hash": value} for key, value in expected.items()}}
        for params, values in ((staged, expected_stage), (final, expected_final)):
            edits = cast(list[dict[str, object]], params["edits"])
            self.assertCountEqual(edits, [
                {"keyPath": f"hooks.state.{json.dumps(key)}", "value": value, "mergeStrategy": "replace"}
                for key, value in values.items()
            ])
        self.assertEqual(set(self.trust()) - set(trusted), set(expected))
        self.assertEqual(set(trusted) - self.trust().keys(), cleaned)
        for entry in self.listed_handlers():
            if entry["command"] in custom_hashes:
                self.assertEqual(entry["trustStatus"], "trusted")
                self.assertIn(entry["key"], expected)
        self.clear_calls()
        repeated = self.run_installer("install")
        self.assertEqual(repeated.returncode, 0, repeated.stdout + repeated.stderr)
        self.assertEqual([call["method"] for call in self.calls()], ["initialize", "hooks/list"])

        with self.subTest(scenario="interrupted installation in shared homes"):
            _ = self.hooks_file.write_bytes(installed_registry)
            self.save_trust(installed_trust)
            self.assert_interrupted_install_migration()

    def assert_interrupted_install_migration(self) -> None:
        self.add_retired_custom_group("custom duplicate")
        self.add_retired_custom_group("custom control")
        initial = cast(dict[str, dict[str, list[dict[str, object]]]], json.loads(self.hooks_file.read_text()))
        group = cast(list[object], initial["hooks"]["PostToolUse"][2]["hooks"])
        group.append(group[-1])
        original_registry = json.dumps(initial)
        _ = self.hooks_file.write_text(original_registry)

        linked_home = self.home / ".codex2"
        linked_home.mkdir()
        (linked_home / "hooks.json").symlink_to(self.hooks_file)
        linked_state = self.root / "linked-state.json"
        self.environment["STUB_ACCOUNT_STATES"] = json.dumps({str(linked_home): str(linked_state)})
        journal = self.hooks_file.with_name(".codex-hooks-install.json")
        cases = ((self.codex_home, self.state_file, 1), (linked_home, linked_state, 2))
        initial_trust: dict[Path, dict[str, str]] = {}
        expected_active: dict[Path, dict[str, str | None]] = {}
        berth_hashes: dict[Path, str] = {}
        for home, state_file, trusted_duplicate in cases:
            with mock.patch.object(self, "codex_home", home):
                entries = {entry["key"]: cast(str, entry["currentHash"]) for entry in self.listed_handlers()}
            prefix = f"{home / 'hooks.json'}:post_tool_use"
            stale_slot, first, second = (f"{prefix}:2:{index}" for index in range(3))
            control = f"{prefix}:3:1"
            duplicate = (first, second)[trusted_duplicate - 1]
            berth_hashes[state_file] = entries[f"{home / 'hooks.json'}:pre_tool_use:0:0"]
            initial_trust[state_file] = {
                **(self.trust() if home == self.codex_home else {}),
                stale_slot: entries[stale_slot], duplicate: entries[duplicate], control: entries[control],
            }
            expected_active[state_file] = {
                stale_slot: entries[duplicate] if trusted_duplicate == 1 else None,
                first: entries[duplicate] if trusted_duplicate == 2 else None,
                f"{prefix}:3:0": entries[control],
            }

        retired = {"type": "command", "command": RETIRED_COMMAND, "timeout": 99}
        managed = {"type": "command", "command": BERTH_COMMAND, "timeout": 29.5}
        initial["hooks"]["PreToolUse"][0]["hooks"] = [retired, managed]
        handlers = cast(list[dict[str, object]], initial["hooks"]["PostToolUse"][3]["hooks"])
        handlers[1]["timeout"] = 20.5
        fractional_registry = json.dumps(initial)
        fractional_trust: dict[Path, dict[str, str]] = {}
        expected_fractional: dict[Path, dict[str, str | None]] = {}
        for home, state_file, _ in cases:
            prefix = f"{home / 'hooks.json'}:pre_tool_use:0"
            fractional_trust[state_file] = {**initial_trust[state_file], f"{prefix}:1": berth_hashes[state_file]}
            expected_fractional[state_file] = {**expected_active[state_file], f"{prefix}:0": berth_hashes[state_file]}

        unchanged_stages = ("after journal write", "after registry replacement", "after trust write",
                            "before journal removal")
        normalized_stages = ("after normalization journal", "after normalization replacement", "after migration journal",
                             "after final replacement", "after trust write", "before journal removal")
        for normalize, stop_points in ((False, unchanged_stages), (True, normalized_stages)):
            for stop_point in stop_points:
                for retry in ("install", "remove-showrunner-guard"):
                    with self.subTest(normalize=normalize, stop_point=stop_point, retry=retry):
                        journal.unlink(missing_ok=True)
                        starting_registry = fractional_registry if normalize else original_registry
                        _ = self.hooks_file.write_text(starting_registry)
                        starting_trust = fractional_trust if normalize else initial_trust
                        expected_trust = expected_fractional if normalize else expected_active
                        for _, state_file, _ in cases:
                            _ = state_file.write_text(json.dumps({"trust": starting_trust[state_file]}))
                        stopped = subprocess.run(
                            [sys.executable, "-c", INTERRUPTED_REMOVAL, stop_point,
                             str(journal), str(self.hooks_file), str(INSTALLER), "install"],
                            env=self.environment, text=True, capture_output=True, check=False,
                            cwd=INSTALLER.resolve().parents[2], timeout=TEST_HANG_GUARD_SECS,
                        )
                        self.assertEqual(stopped.returncode, -signal.SIGKILL, stopped.stdout + stopped.stderr)
                        self.assertTrue(journal.read_bytes())
                        if stop_point == "after normalization journal":
                            self.assertEqual(self.hooks_file.read_text(), starting_registry)
                        if stop_point in {"after normalization replacement", "after migration journal"}:
                            self.assertEqual(self.groups("PreToolUse")[0]["hooks"], [retired, {**managed, "timeout": 30}])
                            self.assertEqual(self.groups()[3]["hooks"], [retired, {
                                "type": "command", "command": "custom control", "timeout": 21}])
                        if stop_point == "before journal removal":
                            self.assert_install_migration(expected_trust)
                        recovered = self.run_installer(retry)
                        self.assertEqual(recovered.returncode, 0, recovered.stdout + recovered.stderr)
                        self.assert_install_migration(expected_trust)
                        if normalize:
                            self.assertEqual(self.groups("PreToolUse")[0]["hooks"], [{**managed, "timeout": 30}])
                            for home, _, _ in cases:
                                with mock.patch.object(self, "codex_home", home):
                                    entries = self.listed_handlers()
                                control = next(entry for entry in entries if entry["command"] == "custom control")
                                self.assertEqual((control["timeoutSec"], control["trustStatus"]), (21, "modified"))
                        with self.assertRaises(FileNotFoundError):
                            _ = journal.read_bytes()

        with self.subTest(retry="refuse an RPC acknowledgment without saved trust"):
            _ = self.hooks_file.write_text(original_registry)
            for state_file, trust in initial_trust.items():
                _ = state_file.write_text(json.dumps({"trust": trust}))
            executable = self.root / "bin/codex"
            original_stub = executable.read_text()
            silent_stub = original_stub.replace('        write_trust(state, params["edits"])', "        pass")
            self.assertNotEqual(original_stub, silent_stub)
            _ = executable.write_text(silent_stub)
            try:
                refused = self.run_installer("install")
            finally:
                _ = executable.write_text(original_stub)
            self.assertEqual(refused.returncode, 1, refused.stdout + refused.stderr)
            self.assertIn("saved hook trust does not match the journal", refused.stderr)
            self.assertEqual(len(refused.stderr.splitlines()), 1)
            self.assertTrue(journal.read_bytes())
            recovered = self.run_installer("remove-showrunner-guard")
            self.assertEqual(recovered.returncode, 0, recovered.stdout + recovered.stderr)
            self.assert_install_migration(expected_active)
            with self.assertRaises(FileNotFoundError):
                _ = journal.read_bytes()

        self.assert_complete_disabled_migration(linked_home, journal)
        self.assert_staged_disabled_migration(linked_home)
        self.assert_late_alias_recovery()

    def assert_complete_disabled_migration(self, alias: Path, journal: Path) -> None:
        alias_config = alias / "config.toml"
        alias_config.symlink_to(self.codex_home / "config.toml")
        alias_registry = alias / "hooks.json"
        states = self.environment["STUB_ACCOUNT_STATES"]
        self.environment["STUB_ACCOUNT_STATES"] = json.dumps({str(alias): str(self.state_file)})
        _ = (self.root / "bin/codex").write_text(RECOVERY_STUB.replace("__PYTHON__", sys.executable))
        custom = {"type": "command", "command": "explicitly disabled custom", "timeout": 20}
        original = json.dumps({"hooks": {"PostToolUse": [{"matcher": "apply_patch", "hooks": [
            {"type": "command", "command": RETIRED_COMMAND, "timeout": 99}, custom,
        ]}]}})
        source = [f"{registry}:post_tool_use:0:1" for registry in (self.hooks_file, alias_registry)]
        destination = [f"{registry}:post_tool_use:0:0" for registry in (self.hooks_file, alias_registry)]
        stale = [f"{registry}:post_tool_use:97:0" for registry in (self.hooks_file, alias_registry)]
        unrelated = f"{self.root / 'other-hooks.json'}:stop:0:0"
        initial = {"trust": {unrelated: "other registry hash", **dict.fromkeys(stale, "stale hash")}, "disabled": source}
        for stop_point in ("after journal write", "after registry replacement", "after trust write", "before journal removal"):
            for retry in ("install", "remove-showrunner-guard"):
                with self.subTest(complete_disabled_move=stop_point, retry=retry):
                    journal.unlink(missing_ok=True)
                    _ = self.hooks_file.write_text(original)
                    _ = self.state_file.write_text(json.dumps(initial))
                    stopped = subprocess.run(
                        [sys.executable, "-c", INTERRUPTED_REMOVAL, stop_point,
                         str(journal), str(self.hooks_file), str(INSTALLER), "install"],
                        env=self.environment, text=True, capture_output=True, check=False,
                        cwd=INSTALLER.resolve().parents[2], timeout=TEST_HANG_GUARD_SECS,
                    )
                    self.assertEqual(stopped.returncode, -signal.SIGKILL, stopped.stdout + stopped.stderr)
                    if stop_point == "after journal write":
                        recorded = cast(dict[str, object], json.loads(journal.read_text()))
                        groups = cast(dict[str, dict[str, list[str]]], recorded["configs"])
                        groups[str(self.codex_home / "config.toml")]["homes"].reverse()
                        _ = journal.write_text(json.dumps(recorded))
                        saved_journal = journal.read_bytes()
                        for problem, text in (
                            ("conflict", "saved hook setting conflicts with the journal"),
                            ("uncovered", "uncovered hook setting"),
                            ("config changes", "hook or trust destination changed"),
                        ):
                            with self.subTest(problem=problem):
                                trust = cast(dict[str, str], initial["trust"]).copy()
                                if problem == "conflict":
                                    trust[stale[0]] = "different hash"
                                elif problem == "uncovered":
                                    trust[f"{alias_registry}:post_tool_use:98:0"] = "new hash"
                                else:
                                    alias_config.unlink()
                                    alias_config.symlink_to(self.root / "other-config.toml")
                                _ = self.state_file.write_text(json.dumps({**initial, "trust": trust}))
                                try:
                                    refused = self.run_installer(retry)
                                    self.assertEqual(refused.returncode, 1, refused.stdout + refused.stderr)
                                    self.assertIn(text, refused.stderr)
                                    self.assertEqual(journal.read_bytes(), saved_journal)
                                    self.assertEqual(self.hooks_file.read_text(), original)
                                    self.assertEqual(self.trust(), trust)
                                finally:
                                    if problem == "config changes":
                                        alias_config.unlink()
                                        alias_config.symlink_to(self.codex_home / "config.toml")
                                    _ = self.state_file.write_text(json.dumps(initial))
                                    _ = self.hooks_file.write_text(original)
                                    _ = journal.write_bytes(saved_journal)
                    result = self.run_installer(retry)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    managed: set[str] = set()
                    for home, key in zip((self.codex_home, alias), destination):
                        with mock.patch.object(self, "codex_home", home):
                            entries = self.listed_handlers()
                        matching = next(entry for entry in entries if entry["command"] == custom["command"])
                        self.assertEqual((matching["key"], matching["trustStatus"], matching["enabled"]),
                                         (key, "untrusted", False))
                        commands = {hook.command for hook in codex_hooks.HOOKS}
                        managed.update(cast(str, entry["key"]) for entry in entries if entry["command"] in commands)
                    state = self.state()
                    self.assertCountEqual(cast(list[str], state["disabled"]), destination)
                    self.assertEqual({key: value for key, value in self.trust().items() if key not in managed},
                                     {unrelated: "other registry hash"})
                    with self.assertRaises(FileNotFoundError):
                        _ = journal.read_bytes()
        alias_config.unlink()
        self.environment["STUB_ACCOUNT_STATES"] = states

    def assert_staged_disabled_migration(self, alias: Path) -> None:
        linked_state = self.root / "staged-linked-state.json"
        homes = ((self.codex_home, self.state_file), (alias, linked_state))
        self.environment["STUB_ACCOUNT_STATES"] = json.dumps({str(alias): str(linked_state)})
        handler = {"type": "command", "command": "custom identical handler", "timeout": 20}
        group = {"matcher": "apply_patch", "hooks": [handler, handler]}
        _ = self.hooks_file.write_text(json.dumps({"hooks": {"PostToolUse": [group]}}))
        handler_hash = cast(str, self.listed_handlers()[0]["currentHash"])
        for operation in ("install", "remove-showrunner-guard"):
            install = operation == "install"
            marker = self.hooks_file.with_name(".codex-hooks-install.json" if install else ".showrunner-guard-removal.json")
            removable = ({"type": "command", "command": RETIRED_COMMAND, "timeout": 99}
                         if install else self.guard_handler("post"))
            original = json.dumps({"hooks": {"PostToolUse": [{**group, "hooks": [removable, handler, handler]}]}})
            for boundary, staged_count, published, final_count in (
                ("after journal write", 0, False, 0),
                ("version 6 before replacement", 0, False, 0),
                (f"after trust write:{self.codex_home}:1", 1, False, 0),
                (f"after trust write:{alias}:1", 2, False, 0),
                ("after registry replacement", 2, True, 0),
                (f"after trust write:{self.codex_home}:2", 2, True, 1),
                (f"after trust write:{alias}:2", 2, True, 2),
                ("before journal removal", 2, True, 2),
            ):
                for retry in ("install", "remove-showrunner-guard"):
                    with self.subTest(staged_registry=operation, boundary=boundary, retry=retry):
                        marker.unlink(missing_ok=True)
                        _ = self.hooks_file.write_text(original)
                        for index, (home, config_state) in enumerate(homes):
                            slot = f"{home / 'hooks.json'}:post_tool_use:0:{index + 1}"
                            _ = config_state.write_text(json.dumps({"trust": {slot: handler_hash}, "disabled": [slot]}))
                        upgrade = boundary == "version 6 before replacement"
                        stop_point = "after journal write" if upgrade else boundary
                        stopped = subprocess.run(
                            [sys.executable, "-c", INTERRUPTED_REMOVAL, stop_point,
                             str(marker), str(self.hooks_file), str(INSTALLER), operation],
                            env=self.environment, text=True, capture_output=True, check=False,
                            cwd=INSTALLER.resolve().parents[2], timeout=TEST_HANG_GUARD_SECS,
                        )
                        self.assertEqual(stopped.returncode, -signal.SIGKILL, stopped.stdout + stopped.stderr)
                        self.assertEqual(self.hooks_file.read_text() != original, published)
                        self.assert_staged_settings(homes, handler_hash, staged_count, final_count)
                        if upgrade:
                            record = cast(dict[str, object], json.loads(marker.read_text()))
                            record["version"] = 6
                            for plan in cast(dict[str, dict[str, object]], record["configs"]).values():
                                settings = cast(dict[str, dict[str, object]], plan["settings"])
                                for values in settings.values():
                                    _ = values.pop("staged", None)
                            _ = marker.write_text(json.dumps(record))
                        recovered = self.run_installer(retry)
                        self.assertEqual(recovered.returncode, 0, recovered.stdout + recovered.stderr)
                        self.assert_staged_settings(homes, handler_hash, 2, 2)
                        with self.assertRaises(FileNotFoundError):
                            _ = marker.read_bytes()

    def assert_staged_settings(
        self, homes: tuple[tuple[Path, Path], ...], handler_hash: str, staged_count: int, final_count: int,
    ) -> None:
        for index, (home, config_state) in enumerate(homes):
            state = cast(dict[str, object], json.loads(config_state.read_text()))
            keys = [f"{home / 'hooks.json'}:post_tool_use:0:{slot}" for slot in range(3)]
            trust = cast(dict[str, str], state["trust"])
            actual = {key: trust[key] for key in keys if key in trust}
            if index < final_count:
                self.assertEqual(actual, {keys[index]: handler_hash})
                self.assertCountEqual(cast(list[str], state["disabled"]), [keys[index]])
            elif index < staged_count:
                self.assertEqual(actual, {})
                canonical = [f"{self.hooks_file}:post_tool_use:0:{slot}" for slot in range(3)] if index else []
                self.assertCountEqual(cast(list[str], state["disabled"]), keys + canonical)
            else:
                self.assertEqual(actual, {keys[index + 1]: handler_hash})
                self.assertCountEqual(cast(list[str], state["disabled"]), [keys[index + 1]])

    def assert_late_alias_recovery(self) -> None:
        (self.home / ".codex2" / "hooks.json").unlink()
        alias = self.home / ".codex3"
        alias.mkdir()
        alias_registry = alias / "hooks.json"
        alias_config = alias / "config.toml"
        alias_state = self.root / "late-alias-state.json"
        states = cast(dict[str, str], json.loads(self.environment["STUB_ACCOUNT_STATES"]))
        _ = (self.root / "bin/codex").write_text(RECOVERY_STUB.replace("__PYTHON__", sys.executable))
        custom = {"type": "command", "command": "late alias custom", "timeout": 20}
        matcher = "apply_patch"
        main_key = f"{self.hooks_file}:post_tool_use:0:0"
        new_key = f"{alias_registry}:post_tool_use:0:0"
        old_key = f"{alias_registry}:post_tool_use:0:1"
        other_key = f"{self.root / 'other-hooks.json'}:stop:0:0"
        old_guard = self.guard_handler("post")
        retired = {"type": "command", "command": RETIRED_COMMAND, "timeout": 99}
        for version, change, published, extra in (
            (2, "install", False, ""), (2, "install", True, ""),
            (2, "remove-showrunner-guard", False, ""), (2, "remove-showrunner-guard", True, ""),
            (3, "install", True, ""),
            (4, "install", True, "shared config"),
            (2, "install", False, "disabled move"),
            (2, "install", True, "canonical without new homes"),
        ):
            for retry in ("install", "remove-showrunner-guard"):
                with self.subTest(late_alias_version=version, change=change, registry_published=published,
                                  extra=extra, retry=retry):
                    alias_registry.unlink(missing_ok=True)
                    alias_config.unlink(missing_ok=True)
                    shared = extra == "shared config"
                    disabled = extra == "disabled move"
                    second_file = self.state_file if shared else alias_state
                    self.environment["STUB_ACCOUNT_STATES"] = json.dumps({**states, str(alias): str(second_file)})
                    if shared:
                        alias_config.symlink_to(self.codex_home / "config.toml")
                    for name in (".codex-hooks-install.json", ".showrunner-guard-removal.json"):
                        self.hooks_file.with_name(name).unlink(missing_ok=True)
                    _ = self.hooks_file.write_text(json.dumps({"hooks": {"PostToolUse": [
                        {"matcher": matcher, "hooks": [custom]},
                    ]}}))
                    if version == 3:
                        installed = self.run_installer("install")
                        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
                    replacement = self.hooks_file.read_text()
                    listed = next(entry for entry in self.listed_handlers() if entry["command"] == custom["command"])
                    handler_hash = cast(str, listed["currentHash"])
                    removed = retired if change == "install" else old_guard
                    previous = replacement.replace('"timeout": 20', '"timeout": 19.5', 1)
                    if version in (2, 4):
                        previous = json.dumps({"hooks": {"PostToolUse": [
                            {"matcher": matcher, "hooks": [removed, custom]},
                        ]}})
                    current = replacement if published else previous
                    _ = self.hooks_file.write_text(current)
                    _ = self.state_file.write_text(json.dumps({"trust": {}}))
                    marker = self.hooks_file.with_name(".codex-hooks-install.json" if change == "install"
                                                       else ".showrunner-guard-removal.json")
                    record: dict[str, object] = {
                        "version": version, "original_hash": hashlib.sha256(previous.encode()).hexdigest(),
                        "registry": replacement,
                    }
                    if version == 3:
                        record.update({"path": str(self.hooks_file), "homes": [str(self.codex_home)]})
                    elif version == 4:
                        record["trust"] = [{
                            "home": str(self.codex_home), "config": str(self.codex_home / "config.toml"),
                            "retained": [], "unproved": {},
                            "edits": {main_key: None, new_key: {"trusted_hash": handler_hash}},
                        }]
                    else:
                        record["trust"] = [{
                            "home": str(self.codex_home), "config": str(self.codex_home / "config.toml"),
                            "retained": [{"old_key": f"{self.hooks_file}:post_tool_use:0:1",
                                          "destination_key": main_key, "handler_hash": handler_hash, "trust": None}],
                            "obsolete_keys": [] if change == "install" else [f"{self.hooks_file}:post_tool_use:0:1"],
                        }]
                    _ = marker.write_text(json.dumps(record))
                    alias_registry.symlink_to(self.hooks_file)
                    saved_key = new_key if published else old_key
                    canonical_old = f"{self.hooks_file}:post_tool_use:0:1"
                    stale = f"{alias_registry}:post_tool_use:73:0"
                    state = {"trust": {saved_key: handler_hash, other_key: "other-source-hash"}}
                    if shared:
                        state["trust"][stale] = "old alias hash"
                    canonical_stale = f"{self.hooks_file}:post_tool_use:79:0"
                    if disabled or extra == "canonical without new homes":
                        retained = [{"old_key": old_key, "destination_key": new_key,
                                     "handler_hash": handler_hash, "trust": None}] if disabled else []
                        records = cast(list[object], record["trust"])
                        records.append({"home": str(alias), "config": str(alias_config),
                                        "retained": retained, "obsolete_keys": []})
                        _ = marker.write_text(json.dumps(record))
                    if extra == "canonical without new homes":
                        state["trust"][canonical_stale] = "old canonical hash"
                    _ = second_file.write_text(json.dumps({
                        **state, "disabled": [old_key, canonical_old] if disabled else [],
                    }))
                    if version == 3:
                        with mock.patch.dict(self.environment, {"STUB_FAILED_READ": str(alias)}):
                            failed = self.run_installer(retry)
                        self.assertEqual(failed.returncode, 1, failed.stdout + failed.stderr)
                        self.assertIn("fixture config read failed", failed.stderr)
                        self.assertTrue(marker.read_bytes())
                    result = self.run_installer(retry)
                    if version != 3:
                        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                        self.assertEqual(result.stdout, "")
                        self.assertIn("legacy journal lacks complete before and after values", result.stderr)
                        self.assertEqual(json.loads(marker.read_text()), record)
                        self.assertEqual(self.hooks_file.read_text(), current)
                        for home, state_file in ((self.codex_home, self.state_file), (alias, second_file)):
                            with mock.patch.object(self, "codex_home", home):
                                entry = next(item for item in self.listed_handlers() if item["command"] == custom["command"])
                            suffix = ":0" if published else ":1"
                            self.assertEqual((entry["key"], entry["trustStatus"], entry["enabled"]),
                                             (f"{home / 'hooks.json'}:post_tool_use:0{suffix}", "untrusted",
                                              not (disabled and home == alias)))
                            saved = cast(dict[str, object], json.loads(state_file.read_text()))
                            expected_trust = {other_key: "other-source-hash"} if home == alias or shared else {}
                            self.assertEqual(saved["trust"], expected_trust)
                            if home == alias:
                                self.assertEqual(saved["disabled"], [old_key, canonical_old] if disabled else [])
                        continue
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    for home, state_file, key, status in (
                        (self.codex_home, self.state_file, main_key, "untrusted"),
                        (alias, second_file, new_key, "trusted"),
                    ):
                        with mock.patch.object(self, "codex_home", home):
                            entry = next(entry for entry in self.listed_handlers() if entry["command"] == custom["command"])
                        self.assertEqual((entry["key"], entry["trustStatus"]), (key, status))
                        saved = cast(dict[str, dict[str, str]], json.loads(state_file.read_text()))["trust"]
                        self.assertEqual(saved.get(key), handler_hash if status == "trusted" else None)
                        if home == alias:
                            self.assertEqual(saved[other_key], "other-source-hash")
                    with self.assertRaises(FileNotFoundError):
                        _ = marker.read_bytes()

    def assert_install_migration(self, expected_active: dict[Path, dict[str, str | None]]) -> None:
        groups = self.groups()
        commands = [[cast(dict[str, object], handler)["command"] for handler in cast(list[object], group["hooks"])]
                    for group in groups[2:]]
        self.assertEqual(commands, [["custom duplicate", "custom duplicate"], ["custom control"]])
        for state_file, expected in expected_active.items():
            with self.subTest(state_file=state_file.name):
                state = cast(dict[str, dict[str, str]], json.loads(state_file.read_text()))
                self.assertEqual({key: state["trust"].get(key) for key in expected}, expected)

    def test_cleanup_leaves_untrusted_custom_sibling_untrusted(self) -> None:
        installed = self.run_installer("install")
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        self.add_retired_custom_group("custom-untrusted")
        trusted = self.trust()
        self.clear_calls()

        result = self.run_installer("install")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.trust(), trusted)
        self.assertEqual([call["method"] for call in self.calls()].count("config/batchWrite"), 2)
        custom = next(entry for entry in self.listed_handlers() if entry["command"] == "custom-untrusted")
        self.assertEqual(custom["key"], f"{self.hooks_file}:post_tool_use:2:0")
        self.assertEqual(custom["trustStatus"], "untrusted")

    def test_cleanup_does_not_restore_trust_for_disabled_custom_sibling(self) -> None:
        installed = self.run_installer("install")
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        self.add_retired_custom_group("custom-disabled")
        custom = next(entry for entry in self.listed_handlers() if entry["command"] == "custom-disabled")
        trusted = {**self.trust(), cast(str, custom["key"]): cast(str, custom["currentHash"])}
        self.save_trust(trusted)
        self.environment["STUB_DISABLED_COMMAND"] = "custom-disabled"
        self.clear_calls()

        result = self.run_installer("install")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.trust(), {key: value for key, value in trusted.items() if key != custom["key"]})
        self.assertEqual([call["method"] for call in self.calls()].count("config/batchWrite"), 2)
        moved = next(entry for entry in self.listed_handlers() if entry["command"] == "custom-disabled")
        self.assertEqual(moved["key"], f"{self.hooks_file}:post_tool_use:2:0")
        self.assertEqual(moved["trustStatus"], "untrusted")
        self.assertEqual(moved["enabled"], False)

    def assert_custom_command_preserved(self, command: str) -> subprocess.CompletedProcess[str]:
        _ = self.hooks_file.write_text(json.dumps({"hooks": {
            "PostToolUse": [{"matcher": "apply_patch", "hooks": [FN_HANDLER]},
                            {"matcher": "Write", "hooks": [{"type": "command", "command": command}]}, QUEUE_GROUP],
            "PreToolUse": PRE_GROUPS,
            **LIFECYCLE_GROUPS,
        }}, indent=4))
        before = self.hooks_file.read_bytes()

        result = self.run_installer("install")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.hooks_file.read_bytes(), before)
        return result

    def test_command_that_only_mentions_retired_script_stays_byte_for_byte(self) -> None:
        script = RETIRED_COMMAND.rsplit("/", 1)[1].removesuffix('"')
        result = self.assert_custom_command_preserved(f"printf '%s\\n' /archive/{script}")
        self.assertIn(str(self.hooks_file), result.stderr)
        self.assertIn("by hand", result.stderr)

    def test_retired_command_with_shell_suffix_stays_and_warns_about_file(self) -> None:
        result = self.assert_custom_command_preserved(RETIRED_COMMAND + "; true")
        self.assertEqual(result.stderr.splitlines(), [
            f"{self.hooks_file}: remove the remaining retired script handler by hand",
        ])

    def test_install_removes_retired_handlers_and_preserves_other_configuration(self) -> None:
        retired: dict[str, object] = {"type": "command", "command": RETIRED_COMMAND, "timeout": 10}
        other: dict[str, object] = {"type": "command", "command": "basedpyright", "timeout": 20}
        similar: dict[str, object] = {
            "type": "command", "command": RETIRED_COMMAND + ".backup", "timeout": 5,
        }
        absolute: dict[str, object] = {
            "type": "command", "command": RETIRED_COMMAND.replace("$HOME", str(self.home)), "timeout": 99,
        }
        retained_groups: list[dict[str, object]] = [
            {"matcher": "apply_patch", "hooks": [], "description": "old group"},
            {"matcher": "apply_patch", "hooks": [FN_HANDLER]},
            {"matcher": "Write", "hooks": [other, absolute, similar]},
            QUEUE_GROUP,
        ]
        original_groups = [
            {**retained_groups[0], "hooks": [retired]},
            retained_groups[1],
            {**retained_groups[2], "hooks": [other, {**retired, "timeout": 99}, absolute, similar]},
            QUEUE_GROUP,
        ]
        _ = self.hooks_file.write_text(json.dumps({
            "description": "user configuration",
            "hooks": {"PostToolUse": original_groups, "PreToolUse": PRE_GROUPS, **LIFECYCLE_GROUPS},
        }))

        installed = self.run_installer("install")

        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        document = cast(dict[str, object], json.loads(self.hooks_file.read_text()))
        self.assertEqual(document, {
            "description": "user configuration",
            "hooks": {"PostToolUse": retained_groups, "PreToolUse": PRE_GROUPS, **LIFECYCLE_GROUPS},
        })
        self.assertEqual(installed.stderr.splitlines(), [
            f"{self.hooks_file}: remove the remaining retired script handler by hand",
        ])
        self.assertEqual(len(self.trust()), 12)
        self.assertIn(f"{self.hooks_file}:post_tool_use:1:0", self.trust())
        before = self.hooks_file.read_bytes()
        modified = self.hooks_file.stat().st_mtime_ns
        trust = self.trust()
        self.clear_calls()

        repeated = self.run_installer("install")

        self.assertEqual(repeated.returncode, 0, repeated.stdout + repeated.stderr)
        self.assertEqual(self.hooks_file.read_bytes(), before)
        self.assertEqual(self.hooks_file.stat().st_mtime_ns, modified)
        self.assertEqual(self.trust(), trust)
        self.assertNotIn("config/batchWrite", [call["method"] for call in self.calls()])

    def test_relative_paths_resolve_before_app_server_changes_directory(self) -> None:
        """Relative home and binary overrides name the caller's files."""
        self.environment["CODEX_HOME"] = "codex-home"
        self.environment["PATH"] = "bin"
        installed = self.run_installer("install", cwd=self.root)
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        self.assertEqual(len(self.groups()), 2)
        self.assertEqual(self.state()["codex_home"], str(self.codex_home))
        self.environment["CODEX_BIN"] = "bin/codex"
        checked = self.run_installer("check", cwd=self.root)
        self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)
        self.assertEqual(self.state()["codex_home"], str(self.codex_home))

    def test_trusted_later_copy_wins_without_retrusting_first(self) -> None:
        """A trusted duplicate satisfies check and install without a write."""
        duplicate = {"matcher": "apply_patch", "hooks": [FN_HANDLER]}
        _ = self.hooks_file.write_text(json.dumps({"hooks": {"PostToolUse": [duplicate, duplicate]}}))
        first = self.run_installer("install")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        state = self.state()
        trust = cast(dict[str, str], state["trust"])
        self.assertEqual(len(trust), 12)
        first_key = next(key for key in trust if key.endswith(":post_tool_use:0:0"))
        second_key = first_key.replace(":0:0", ":1:0")
        trust[second_key] = trust.pop(first_key)
        state["calls"] = []
        _ = self.state_file.write_text(json.dumps(state))
        checked = self.run_installer("check")
        self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)
        installed = self.run_installer("install")
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        self.assertNotIn("config/batchWrite", [call["method"] for call in self.calls()])
        self.assertEqual(self.trust(), trust)
        self.assertEqual(len(self.groups()), 3)

    def test_same_handler_under_other_matcher_does_not_count(self) -> None:
        existing = {"matcher": "Write", "hooks": [FN_HANDLER]}
        _ = self.hooks_file.write_text(json.dumps({"hooks": {"PostToolUse": [existing]}}))
        result = self.run_installer("install")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.groups(), [existing,
                         {"matcher": "apply_patch", "hooks": [FN_HANDLER]}, QUEUE_GROUP])

    def test_queue_drain_registers_and_trusts_both_events_with_one_adapter(self) -> None:
        installed = self.run_installer("install")
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        handlers = [entry for entry in self.listed_handlers() if entry["command"] == QUEUE_COMMAND]
        self.assertEqual([(entry["eventName"], entry["matcher"], entry["trustStatus"], entry["enabled"])
                          for entry in handlers],
                         [("postToolUse", "*", "trusted", True), ("sessionStart", "*", "trusted", True)])

    def test_install_repairs_managed_and_unmanaged_float_timeouts(self) -> None:
        directory = self.root / "config"
        directory.mkdir()
        self.environment["COPIES_CONFIG"] = str(directory / "copies.conf")
        for seconds, expected, user_seconds, user_expected, retire in (
            (10.0, 10, 5.0, 5, False), (10.25, 11, 5.25, 6, False), (10.25, 11, 5.25, 6, True),
        ):
            with self.subTest(seconds=seconds, retire=retire):
                _ = write_config_fixture(directory / "copies.conf", f"guard_timeout_seconds={seconds}\n")
                handler = {"type": "command", "command": GUARD_COMMAND, "timeout": seconds}
                user_handler = {"type": "command", "command": "user-hook", "timeout": user_seconds,
                                "statusMessage": "Running the user hook"}
                retired = [{"type": "command", "command": RETIRED_COMMAND}] if retire else []
                _ = self.hooks_file.write_text(json.dumps({"hooks": {
                    "PreToolUse": [{"matcher": "Bash", "hooks": [*retired, handler]}],
                    "PostToolUse": [{"matcher": "Edit|Write", "hooks": [*retired, user_handler]}],
                }}))
                self.state_file.unlink(missing_ok=True)
                before = self.run_installer("check")
                self.assertEqual(before.returncode, 1)
                self.assertIn("hooks/list warnings: failed to parse hooks config", before.stderr)
                self.assertIn("expected u64", before.stderr)
                installed = self.run_installer("install")
                self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
                groups = self.groups("PreToolUse")
                handlers = cast(list[dict[str, object]], groups[0]["hooks"])
                self.assertIs(type(handlers[0]["timeout"]), int)
                self.assertEqual(handlers[0]["timeout"], expected)
                self.assertEqual(len(groups), 3)
                user_group = self.groups()[0]
                self.assertEqual(user_group, {"matcher": "Edit|Write", "hooks": [
                    {**user_handler, "timeout": user_expected}]})
                user_handlers = cast(list[dict[str, object]], user_group["hooks"])
                self.assertIs(type(user_handlers[0]["timeout"]), int)
                checked = self.run_installer("check")
                self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)
                self.assertEqual(checked.stdout.splitlines(), [
                    f"{hook.name} codex hook: trusted" for hook in codex_hooks.HOOKS])
                self.assertEqual(checked.stderr, "")

    def test_install_reports_the_field_that_does_not_match_the_codex_listing(self) -> None:
        self.environment["STUB_CHANGED_COMMAND"] = QUEUE_COMMAND
        cases = (("eventName", "session_start"), ("matcher", None),
                 ("handlerType", "prompt"), ("timeoutSec", 20), ("command", "other-command"))
        for field, value in cases:
            with self.subTest(field=field):
                self.environment["STUB_LIST_FIELD"] = field
                self.environment["STUB_LIST_VALUE"] = json.dumps(value)
                result = self.run_installer("install")
                self.assertEqual(result.returncode, 1)
                self.assertIn("codex hooks: install: queue-start: hooks/list", result.stderr)
                self.assertIn(field, result.stderr)
                if field != "command":
                    self.assertIn(f"got {value!r}", result.stderr)
                self.assertIn("queue-start codex hook: absent", result.stdout.splitlines())

    def test_install_reports_codex_listing_warnings_and_errors(self) -> None:
        for kind in ("warnings", "errors"):
            with self.subTest(kind=kind):
                name = "STUB_LIST_" + kind.upper()
                self.environment[name] = "failed to parse hooks config: invalid type: map, expected u64"
                result = self.run_installer("install")
                self.assertEqual(result.returncode, 1)
                self.assertIn(f"codex hooks: hooks/list {kind}: " + self.environment[name], result.stderr)
                self.assertIn("codex hooks: install: queue-start:", result.stderr)
                del self.environment[name]

    def test_context_hooks_are_listed_and_trusted_for_each_event(self) -> None:
        installed = self.run_installer("install")
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        contexts = [entry for entry in self.listed_handlers() if entry["command"] == STE_COMMAND]
        self.assertEqual([(entry["eventName"], entry["matcher"], entry["trustStatus"], entry["enabled"])
                          for entry in contexts],
                         [("sessionStart", "*", "trusted", True), ("subagentStart", "*", "trusted", True)])
        trust = self.trust()
        subagent_key = cast(str, contexts[1]["key"])
        trust[subagent_key] = "stale hash"
        self.save_trust(trust)
        checked = self.run_installer("check")
        self.assertEqual(checked.returncode, 1, checked.stdout + checked.stderr)
        self.assertIn("ste codex hook: trusted", checked.stdout.splitlines())
        self.assertIn("ste-subagent codex hook: modified", checked.stdout.splitlines())
        self.clear_calls()
        repaired = self.run_installer("install")
        self.assertEqual(repaired.returncode, 0, repaired.stdout + repaired.stderr)
        writes = [call for call in self.calls() if call["method"] == "config/batchWrite"]
        self.assertEqual(len(writes), 1)
        params = cast(dict[str, object], writes[0]["params"])
        edits = cast(list[dict[str, object]], params["edits"])
        self.assertEqual(list(cast(dict[str, object], edits[0]["value"])), [subagent_key])

    def test_context_hooks_with_narrow_matchers_do_not_satisfy_install(self) -> None:
        narrow = {"SessionStart": [{**STE_GROUP, "matcher": "startup|resume"}],
                  "SubagentStart": [{**STE_GROUP, "matcher": "apply_patch"}]}
        _ = self.hooks_file.write_text(json.dumps({"hooks": narrow}))
        installed = self.run_installer("install")
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        for event, original in narrow.items():
            self.assertEqual(self.groups(event), [*original, STE_GROUP, *([QUEUE_GROUP] if event == "SessionStart" else [])])
        contexts = [entry for entry in self.listed_handlers() if entry["command"] == STE_COMMAND]
        self.assertEqual([(entry["matcher"], entry["trustStatus"]) for entry in contexts],
                         [("startup|resume", "untrusted"), ("*", "trusted"),
                          ("apply_patch", "untrusted"), ("*", "trusted")])

    def test_context_trust_survives_retired_sibling_removal(self) -> None:
        installed = self.run_installer("install")
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        document = cast(dict[str, object], json.loads(self.hooks_file.read_text()))
        hooks = cast(dict[str, object], document["hooks"])
        hooks["SessionStart"] = [{"matcher": "*", "hooks": [
            {"type": "command", "command": RETIRED_COMMAND},
            {"type": "command", "command": STE_COMMAND, "timeout": 10},
        ]}]
        _ = self.hooks_file.write_text(json.dumps(document))
        trust = self.trust()
        restored_key = f"{self.hooks_file}:session_start:0:0"
        trust[f"{self.hooks_file}:session_start:0:1"] = trust.pop(restored_key)
        self.save_trust(trust)
        self.clear_calls()
        repaired = self.run_installer("install")
        self.assertEqual(repaired.returncode, 0, repaired.stdout + repaired.stderr)
        self.assertEqual(self.groups("SessionStart"), [STE_GROUP, QUEUE_GROUP])
        writes = [call for call in self.calls() if call["method"] == "config/batchWrite"]
        self.assertEqual(len(writes), 2)
        params = cast(dict[str, object], writes[-1]["params"])
        edits = cast(list[dict[str, object]], params["edits"])
        self.assertCountEqual([edit["keyPath"] for edit in edits], [
            f"hooks.state.{json.dumps(restored_key)}", f"hooks.state.{json.dumps(restored_key[:-1] + '1')}",
        ])
        self.assertEqual(self.trust()[restored_key], trust[f"{self.hooks_file}:session_start:0:1"])

    def test_modified_entry_is_trusted_again(self) -> None:
        first = self.run_installer("install")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        state = self.state()
        trust = cast(dict[str, str], state["trust"])
        key = next(iter(trust))
        trust[key] = "stale hash"
        state["calls"] = []
        _ = self.state_file.write_text(json.dumps(state))
        second = self.run_installer("install")
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertIn("config/batchWrite", [call["method"] for call in self.calls()])
        self.assertNotEqual(cast(dict[str, str], self.state()["trust"])[key], "stale hash")
        self.assertEqual(len(self.groups()), 2)

    def test_only_modified_hook_is_retrusted(self) -> None:
        first = self.run_installer("install")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        trust = self.trust()
        fn_key = next(key for key in trust if key.endswith(":post_tool_use:0:0"))
        berth_key = next(key for key in trust if key.endswith(":pre_tool_use:0:0"))
        fn_hash = trust[fn_key]
        trust[berth_key] = "stale hash"
        self.save_trust(trust)
        self.clear_calls()

        result = self.run_installer("install")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.trust()[fn_key], fn_hash)
        writes = [call for call in self.calls() if call["method"] == "config/batchWrite"]
        self.assertEqual(len(writes), 1)
        params = cast(dict[str, object], writes[0]["params"])
        edits = cast(list[dict[str, object]], params["edits"])
        self.assertEqual(list(cast(dict[str, object], edits[0]["value"])), [berth_key])
        self.assertNotEqual(self.trust()[berth_key], "stale hash")

    def test_check_names_berth_when_fn_is_trusted(self) -> None:
        installed = self.run_installer("install")
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        fn_line = "fn-length codex hook: trusted"
        guard_line = "scratch-guard codex hook: trusted"
        hold_line = "build-hold-guard codex hook: trusted"
        start_lines = ["ste codex hook: trusted", "ste-subagent codex hook: trusted",
                       "queue-start codex hook: trusted", "queue-tools codex hook: trusted",
                       "compaction-marker codex hook: trusted", "compaction-clear-turn codex hook: trusted",
                       "compaction-clear-end codex hook: trusted", "turn-end codex hook: trusted"]

        trust = self.trust()
        berth_key = next(key for key in trust if key.endswith(":pre_tool_use:0:0"))
        del trust[berth_key]
        self.save_trust(trust)
        untrusted = self.run_installer("check")
        self.assertEqual(untrusted.returncode, 1)
        self.assertEqual(untrusted.stdout.splitlines(),
                         [fn_line, "berth codex hook: untrusted", guard_line, hold_line, *start_lines])

        trust[berth_key] = "stale hash"
        self.save_trust(trust)
        modified = self.run_installer("check")
        self.assertEqual(modified.returncode, 1)
        self.assertEqual(modified.stdout.splitlines(),
                         [fn_line, "berth codex hook: modified", guard_line, hold_line, *start_lines])

        restored = self.run_installer("install")
        self.assertEqual(restored.returncode, 0, restored.stdout + restored.stderr)
        self.environment["STUB_DISABLED_COMMAND"] = BERTH_COMMAND
        disabled = self.run_installer("check")
        self.assertEqual(disabled.returncode, 1)
        self.assertEqual(disabled.stdout.splitlines(),
                         [fn_line, "berth codex hook: disabled", guard_line, hold_line, *start_lines])
        failed_install = self.run_installer("install")
        self.assertEqual(failed_install.returncode, 1)
        self.assertEqual(failed_install.stdout.splitlines(),
                         [fn_line, "berth codex hook: disabled", guard_line, hold_line, *start_lines])
        self.assertIn("codex hooks: install: berth: trustStatus='trusted', enabled=False", failed_install.stderr)
        _ = self.environment.pop("STUB_DISABLED_COMMAND")

        _ = self.hooks_file.write_text(json.dumps({"hooks": {"PostToolUse": self.groups()[:1]}}))
        absent = self.run_installer("check")
        self.assertEqual(absent.returncode, 1)
        self.assertEqual(absent.stdout.splitlines(),
                         [fn_line, "berth codex hook: absent", "scratch-guard codex hook: absent",
                          "build-hold-guard codex hook: absent", "ste codex hook: absent",
                          "ste-subagent codex hook: absent", "queue-start codex hook: absent",
                          "queue-tools codex hook: absent", "compaction-marker codex hook: absent",
                          "compaction-clear-turn codex hook: absent", "compaction-clear-end codex hook: absent",
                          "turn-end codex hook: absent"])

    def test_check_reports_absent_untrusted_modified_and_disabled(self) -> None:
        absent = self.run_installer("check")
        self.assertEqual(absent.returncode, 1)
        self.assertIn("fn-length codex hook: absent", absent.stdout + absent.stderr)
        _ = self.hooks_file.write_text(json.dumps({"hooks": {"PostToolUse": [
            {"matcher": "apply_patch", "hooks": [FN_HANDLER]},
        ]}}))
        untrusted = self.run_installer("check")
        self.assertEqual(untrusted.returncode, 1)
        self.assertIn("fn-length codex hook: untrusted", untrusted.stdout + untrusted.stderr)
        installed = self.run_installer("install")
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        trusted = self.run_installer("check")
        self.assertEqual(trusted.returncode, 0, trusted.stdout + trusted.stderr)
        state = self.state()
        trust = cast(dict[str, str], state["trust"])
        trust[next(iter(trust))] = "stale hash"
        _ = self.state_file.write_text(json.dumps(state))
        modified = self.run_installer("check")
        self.assertEqual(modified.returncode, 1)
        self.assertIn("fn-length codex hook: modified", modified.stdout + modified.stderr)
        self.environment["STUB_DISABLED"] = "1"
        disabled = self.run_installer("check")
        self.assertEqual(disabled.returncode, 1)
        self.assertIn("fn-length codex hook: disabled", disabled.stdout + disabled.stderr)

    @process_boundary
    def test_dead_app_server_is_reported(self) -> None:
        executable = Path(self.environment["PATH"]) / "codex"
        _ = executable.write_text("#!/bin/sh\nexit 7\n")
        result = self.run_installer("install")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("codex hooks: install: app-server closed before replying", result.stderr)

    def started_server(self, script: str) -> codex_hooks.AppServer:
        executable = Path(self.environment["PATH"]) / "codex"
        _ = executable.write_text(script)
        with mock.patch.dict(os.environ, {"HOME": str(self.home)}):
            server = codex_hooks.AppServer(str(executable), str(self.codex_home))
        self.addCleanup(server.close)
        return server

    # A server that ends is met either by the write of the request or by the read of the reply,
    # whichever comes first. The two tests below fix the order each way; both must read the same.
    def test_a_server_gone_before_the_request_is_written_reads_as_closed(self) -> None:
        server = self.started_server("#!/bin/sh\nexit 7\n")
        _ = server.process.wait(timeout=TEST_HANG_GUARD_SECS)
        with self.assertRaisesRegex(codex_hooks.HookError, "^app-server closed before replying$"):
            _ = server.call("initialize", {})

    def test_a_server_that_ends_after_reading_the_request_reads_as_closed(self) -> None:
        server = self.started_server("#!/bin/sh\nread line\nexit 7\n")
        with self.assertRaisesRegex(codex_hooks.HookError, "^app-server closed before replying$"):
            _ = server.call("initialize", {})

    def test_invalid_hooks_json_is_unchanged(self) -> None:
        original = b"{ invalid json\n"
        _ = self.hooks_file.write_bytes(original)
        result = self.run_installer("install")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn(f"codex hooks: install: {self.hooks_file}", result.stderr)
        self.assertEqual(self.hooks_file.read_bytes(), original)
        self.assertFalse(self.state_file.exists())

    def test_batch_write_refusal_reports_server_error(self) -> None:
        self.environment["STUB_REFUSE"] = "1"
        result = self.run_installer("install")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr.strip(), "codex hooks: install: config/batchWrite: invalid config key: hooks.state")

    def test_remove_showrunner_guard_retains_other_hooks_and_their_trust(self) -> None:
        pre = self.guard_handler("pre")
        post = self.guard_handler("post")
        custom = {"type": "command", "command": "custom apply_patch", "timeout": 10}
        untrusted = {"type": "command", "command": "custom Bash", "timeout": 10}
        mention = {"type": "command", "command": "echo pre-tool-use-showrunner-guard-codex.py", "timeout": 10}
        initial_hooks = json.dumps({"hooks": {
            "PreToolUse": [{"matcher": "Bash", "hooks": [pre]},
                           {"matcher": "apply_patch", "hooks": [pre, custom]},
                           {"matcher": "Bash", "hooks": [untrusted]}],
            "PostToolUse": [{"matcher": "apply_patch", "hooks": [post, FN_HANDLER, mention]}],
        }})
        _ = self.hooks_file.write_text(initial_hooks)
        other_source = f"{self.root / 'other-hooks.json'}:pre_tool_use:0:0"
        initial_trust = {cast(str, entry["key"]): cast(str, entry["currentHash"])
                         for entry in self.listed_handlers() if entry["command"] != untrusted["command"]}
        starting_trust = {**initial_trust, other_source: "other-source-hash"}
        registry = self.hooks_file.resolve()
        journal = registry.with_name(".showrunner-guard-removal.json")
        for stop_point, install_before_retry, verify_identity in (
            ("uninterrupted", False, False),
            ("after journal write", False, False),
            ("after trust write", False, False),
            ("after registry replacement", False, False),
            ("before journal removal", False, False),
            ("after trust write", True, False),
            ("after journal write", True, True),
            ("after trust write", True, True),
            ("after registry replacement", True, True),
            ("before journal removal", True, True),
        ):
            with self.subTest(stop_point=stop_point, install_before_retry=install_before_retry,
                              verify_identity=verify_identity):
                journal.unlink(missing_ok=True)
                _ = self.hooks_file.write_text(initial_hooks)
                self.save_trust(starting_trust)
                if stop_point != "uninterrupted":
                    result = subprocess.run(
                        [sys.executable, "-c", INTERRUPTED_REMOVAL, stop_point,
                         str(journal), str(registry), str(INSTALLER), "remove-showrunner-guard"],
                        env=self.environment, text=True, capture_output=True, check=False,
                        cwd=INSTALLER.resolve().parents[2], timeout=TEST_HANG_GUARD_SECS,
                    )
                    self.assertEqual(result.returncode, -signal.SIGKILL, result.stdout + result.stderr)
                    self.assertTrue(journal.read_bytes())
                    if stop_point in ("after journal write", "after trust write"):
                        self.assertEqual(registry.read_text(), initial_hooks)
                    else:
                        self.assertNotEqual(registry.read_text(), initial_hooks)
                    if stop_point == "after journal write":
                        self.assertEqual(self.trust(), starting_trust)
                    else:
                        self.assertNotEqual(self.trust(), starting_trust)
                if stop_point == "before journal removal":
                    expected_custom = next(value for key, value in initial_trust.items()
                                           if key.endswith(":pre_tool_use:1:1"))
                    self.assertEqual(self.trust()[f"{registry}:pre_tool_use:0:0"], expected_custom)
                    self.assertEqual(self.groups("PreToolUse"), [{"matcher": "apply_patch", "hooks": [custom]},
                                                                 {"matcher": "Bash", "hooks": [untrusted]}])
                if verify_identity:
                    trust_before_retry = self.trust()
                    registry_before_retry = registry.read_bytes()
                    with mock.patch.dict(self.environment, {"STUB_CHANGED_COMMAND": cast(str, custom["command"]),
                                                            "STUB_LIST_FIELD": "currentHash",
                                                            "STUB_LIST_VALUE": '"different hash"'}):
                        refused = self.run_installer("install")
                    self.assertEqual(refused.returncode, 1, refused.stdout + refused.stderr)
                    self.assertIn("retained hook is missing or has a different identity", refused.stderr)
                    self.assertEqual(len(refused.stderr.splitlines()), 1)
                    self.assertEqual(self.trust(), trust_before_retry)
                    self.assertEqual(registry.read_bytes(), registry_before_retry)
                    self.assertTrue(journal.read_bytes())
                if install_before_retry:
                    result = self.run_installer("install")
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    with self.subTest(step="install resolves the removal journal"):
                        with self.assertRaises(FileNotFoundError):
                            _ = journal.read_bytes()
                result = self.run_installer("remove-showrunner-guard")

                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                if not install_before_retry:
                    self.assertEqual(self.groups("PreToolUse"), [{"matcher": "apply_patch", "hooks": [custom]},
                                                                 {"matcher": "Bash", "hooks": [untrusted]}])
                    self.assertEqual(self.groups("PostToolUse"),
                                     [{"matcher": "apply_patch", "hooks": [FN_HANDLER, mention]}])
                retained = self.listed_handlers()
                expected_retained = {custom["command"]: "trusted", untrusted["command"]: "untrusted",
                                     FN_COMMAND: "trusted", mention["command"]: "trusted"}
                if install_before_retry:
                    result = self.run_installer("check")
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertTrue({pre["command"], post["command"]}.isdisjoint(
                        entry["command"] for entry in retained))
                    retained_trust = {entry["command"]: entry["trustStatus"] for entry in retained
                                      if entry["command"] in expected_retained}
                else:
                    retained_trust = {entry["command"]: entry["trustStatus"] for entry in retained}
                self.assertEqual(retained_trust, expected_retained)
                self.assertEqual(self.trust(), {
                    other_source: "other-source-hash",
                    **{cast(str, entry["key"]): cast(str, entry["currentHash"])
                       for entry in retained if entry["trustStatus"] == "trusted"},
                })
                with self.assertRaises(FileNotFoundError):
                    _ = journal.read_bytes()

        self.assert_shared_legacy_removal()
        self.assert_legacy_install_recovery()

    def assert_shared_legacy_removal(self) -> None:
        alias = self.home / ".codex4"
        alias.mkdir()
        alias_registry = alias / "hooks.json"
        alias_registry.symlink_to(self.hooks_file)
        config = self.codex_home / "config.toml"
        (alias / "config.toml").symlink_to(config)
        self.environment["STUB_ACCOUNT_STATES"] = json.dumps({str(alias): str(self.state_file)})
        custom = {"type": "command", "command": "legacy removal custom", "timeout": 11}
        replacement = json.dumps({"hooks": {"PostToolUse": [{"matcher": "apply_patch", "hooks": [custom]}]}})
        original = json.dumps({"hooks": {"PostToolUse": [
            {"matcher": "apply_patch", "hooks": [self.guard_handler("post"), custom]},
        ]}})
        _ = self.hooks_file.write_text(replacement)
        listed = next(entry for entry in self.listed_handlers() if entry["command"] == custom["command"])
        handler_hash = cast(str, listed["currentHash"])
        primary_new, alias_new = (f"{registry}:post_tool_use:0:0" for registry in (self.hooks_file, alias_registry))
        primary_old, alias_old = (f"{registry}:post_tool_use:0:1" for registry in (self.hooks_file, alias_registry))
        unrelated = f"{self.root / 'other-hooks.json'}:stop:0:0"
        foreign = self.root / "other-hooks.json"
        marker = self.hooks_file.with_name(".showrunner-guard-removal.json")
        for stage, representative, published, entries in (
            ("original", self.codex_home, False, {primary_old: handler_hash, alias_old: handler_hash}),
            ("partial writes", self.codex_home, False, {primary_old: handler_hash, alias_new: handler_hash}),
            ("replacement", self.codex_home, True, {primary_new: handler_hash, alias_new: handler_hash}),
            ("replacement with alias first", alias, True, {primary_new: handler_hash, alias_new: handler_hash}),
            ("saved revocation before saved proof", self.codex_home, True, {primary_new: handler_hash}),
            ("alias changes registry", self.codex_home, True, {primary_new: handler_hash}),
        ):
            for retry in ("install", "remove-showrunner-guard"):
                with self.subTest(shared_legacy_removal=stage, retry=retry):
                    current = replacement if published else original
                    _ = self.hooks_file.write_text(current)
                    starting_trust = {**entries, unrelated: "unrelated hash"}
                    self.save_trust(starting_trust)
                    proof: dict[str, object] = {
                        "home": str(representative), "config": str(config), "edits": {
                            primary_old: None, alias_old: None, primary_new: {"trusted_hash": handler_hash},
                            alias_new: {"trusted_hash": handler_hash},
                        },
                    }
                    trust: list[dict[str, object]] = [proof]
                    version = 1
                    if stage == "saved revocation before saved proof":
                        version = 4
                        trust = [
                            {"home": str(alias), "config": str(config), "retained": [],
                             "edits": {alias_new: None}, "unproved": {alias_new: {"trusted_hash": handler_hash}}},
                            {**proof, "retained": [], "unproved": {}},
                        ]
                    _ = marker.write_text(json.dumps({
                        "version": version, "original_hash": hashlib.sha256(original.encode()).hexdigest(),
                        "registry": replacement, "trust": trust,
                    }))
                    saved_journal = marker.read_bytes()
                    changed = stage == "alias changes registry"
                    if changed:
                        _ = foreign.write_text(replacement)
                        alias_registry.unlink()
                        alias_registry.symlink_to(foreign)
                    for attempt in range(2):
                        try:
                            recovered = self.run_installer(retry)
                        finally:
                            if changed:
                                alias_registry.unlink()
                                alias_registry.symlink_to(self.hooks_file)
                        self.assertEqual(recovered.returncode, 1,
                                         f"Attempt {attempt + 1}\n{recovered.stdout}{recovered.stderr}")
                        self.assertEqual(recovered.stdout, "")
                        self.assertEqual(marker.read_bytes(), saved_journal)
                        self.assertEqual(self.hooks_file.read_text(), current)
                        if changed:
                            self.assertIn("hook or trust destination changed", recovered.stderr)
                            self.assertEqual(self.trust(), starting_trust)
                            self.assertEqual(foreign.read_text(), replacement)
                            break
                        self.assertIn("legacy journal lacks complete before and after values", recovered.stderr)
                        suffix = "0" if published else "1"
                        for home in (self.codex_home, alias):
                            with mock.patch.object(self, "codex_home", home):
                                listed = next(entry for entry in self.listed_handlers()
                                              if entry["command"] == custom["command"])
                            self.assertEqual((listed["key"], listed["trustStatus"], listed["enabled"]),
                                             (f"{home / 'hooks.json'}:post_tool_use:0:{suffix}", "untrusted", True))
                        self.assertEqual(self.trust(), {unrelated: "unrelated hash"})
        self.assert_scoped_legacy_revocation(alias, marker)
        alias_registry.unlink()
        marker.unlink(missing_ok=True)

    def assert_scoped_legacy_revocation(self, alias: Path, marker: Path) -> None:
        other = self.home / ".codex5"
        other.mkdir()
        (other / "hooks.json").symlink_to(self.hooks_file)
        other_state = self.root / "second-revocation.json"
        self.environment["STUB_ACCOUNT_STATES"] = json.dumps({
            str(alias): str(self.state_file), str(other): str(other_state),
        })
        keep = {"type": "command", "command": "unchanged sibling", "timeout": 11}
        moved = {**keep, "command": "moved sibling"}
        stop = {**keep, "command": "unchanged stop"}
        group = {"matcher": "apply_patch"}
        original = json.dumps({"hooks": {
            "PostToolUse": [{**group, "hooks": [keep, self.guard_handler("post"), moved]}],
            "Stop": [{"hooks": [stop]}],
        }})
        replacement = json.dumps({"hooks": {
            "PostToolUse": [{**group, "hooks": [keep, moved]}],
            "Stop": [{"hooks": [stop]}],
        }})
        registries = (self.hooks_file, alias / "hooks.json", other / "hooks.json")
        source, destination, stable, stop_keys, stale = (
            [f"{registry}:{slot}" for registry in registries]
            for slot in ("post_tool_use:0:2", "post_tool_use:0:1", "post_tool_use:0:0",
                         "stop:0:0", "post_tool_use:97:0")
        )
        record = {
            "version": 1, "original_hash": hashlib.sha256(original.encode()).hexdigest(), "registry": replacement,
            "trust": [{"home": str(self.codex_home), "config": str(self.codex_home / "config.toml"),
                       "edits": {source[0]: None, destination[0]: {"trusted_hash": "moved hash"}}}],
        }
        for scenario, current, active in (
            ("original registry", original, source),
            ("published registry", replacement, destination),
            ("partial revocation", original, source),
        ):
            for retry in ("install", "remove-showrunner-guard"):
                with self.subTest(legacy_revocation=scenario, retry=retry):
                    for archive in self.codex_home.glob(".codex-hooks-install-recovery-legacy-*.json"):
                        archive.unlink()
                    _ = self.hooks_file.write_text(current)
                    _ = marker.write_text(json.dumps(record))
                    saved_marker = marker.read_bytes()
                    hashes = {entry["command"]: cast(str, entry["currentHash"]) for entry in self.listed_handlers()}
                    retained = {key: hashes[stop["command"]] for key in stop_keys}
                    if current == original:
                        retained.update({key: hashes[keep["command"]] for key in stable})
                    for indexes, state_file in (((0, 1), self.state_file), ((2,), other_state)):
                        scope = {active[index]: hashes[moved["command"]] for index in indexes}
                        scope.update({stale[index]: "outdated trust" for index in indexes})
                        preserved = {key: value for key, value in retained.items()
                                     if any(key.startswith(f"{registries[index]}:") for index in indexes)}
                        _ = state_file.write_text(json.dumps({
                            "trust": {**preserved, **scope},
                            "disabled": [key for index in indexes for key in (active[index], stop_keys[index])],
                            "extra_settings": {
                                key: {"fixture": "keep this field"}
                                for index in indexes for key in (active[index], stop_keys[index])
                            },
                        }))
                    if scenario == "partial revocation":
                        stopped = subprocess.run(
                            [sys.executable, "-c", INTERRUPTED_REMOVAL, f"after trust write:{self.codex_home}:1",
                             str(marker), str(self.hooks_file), str(INSTALLER), retry],
                            env=self.environment, text=True, capture_output=True, check=False,
                            cwd=INSTALLER.resolve().parents[2], timeout=TEST_HANG_GUARD_SECS,
                        )
                        self.assertEqual(stopped.returncode, -signal.SIGKILL, stopped.stdout + stopped.stderr)
                        self.assertEqual(self.trust(), {key: retained[key] for key in (*stable[:2], *stop_keys[:2])})
                        secondary = cast(dict[str, dict[str, object]], json.loads(other_state.read_text()))
                        self.assertEqual(secondary["trust"][active[2]], hashes[moved["command"]])
                        self.assertEqual(secondary["trust"][stale[2]], "outdated trust")
                    for attempt in range(2):
                        result = self.run_installer(retry)
                        self.assertEqual(result.returncode, 1, f"Attempt {attempt + 1}\n{result.stdout}{result.stderr}")
                        self.assertIn("legacy journal lacks complete before and after values", result.stderr)
                        self.assertEqual(marker.read_bytes(), saved_marker)
                        self.assertEqual(self.hooks_file.read_text(), current)
                        for indexes, state_file in (((0, 1), self.state_file), ((2,), other_state)):
                            state = cast(dict[str, object], json.loads(state_file.read_text()))
                            self.assertEqual(state["trust"], {
                                key: value for key, value in retained.items()
                                if any(key.startswith(f"{registries[index]}:") for index in indexes)
                            })
                            self.assertCountEqual(cast(list[str], state["disabled"]), [
                                key for index in indexes for key in (active[index], stop_keys[index])
                            ])
                            self.assertEqual(state["extra_settings"], {
                                key: {"fixture": "keep this field"}
                                for index in indexes for key in (active[index], stop_keys[index])
                            })
                    archive, = self.codex_home.glob(".codex-hooks-install-recovery-legacy-*.json")
                    saved = cast(dict[str, object], json.loads(archive.read_text()))
                    archived = cast(dict[str, dict[str, object]], saved["revoked"])
                    self.assertEqual({key for config in archived.values() for key in config}, {*active, *stale})
        configs = ((self.codex_home, self.state_file, registries[1]), (other, other_state, registries[2]))
        cases = (
            ("config-local Stop", 0, False, False),
            ("config-local Stop reversed", 1, False, False),
            ("config-local aliases", 0, True, False),
            ("config-local partial retry", 1, True, True),
        )
        for scenario, affected, use_aliases, interrupt in cases:
            for current, active_key in ((original, source[0]), (replacement, destination[0])):
                for retry in ("install", "remove-showrunner-guard"):
                    with self.subTest(legacy_v5=scenario, published=current == replacement, retry=retry):
                        for archive in self.codex_home.glob(".codex-hooks-install-recovery-legacy-*.json"):
                            archive.unlink()
                        _ = self.hooks_file.write_text(current)
                        hashes = {entry["command"]: cast(str, entry["currentHash"]) for entry in self.listed_handlers()}
                        moved_hash = hashes[moved["command"]]
                        plans: dict[str, object] = {}
                        expected_revoked: dict[str, dict[str, dict[str, object]]] = {}
                        expected_preserved: dict[str, dict[str, dict[str, object]]] = {}
                        initial_trust: dict[Path, dict[str, str]] = {}
                        final_states: dict[Path, dict[str, object]] = {}
                        for index, (home, state_file, registry_alias) in enumerate(configs):
                            config = str(home / "config.toml")
                            revoked_settings: dict[str, dict[str, object]] = {active_key: {"trusted_hash": moved_hash}}
                            stop_spellings = [stop_keys[0]]
                            if use_aliases:
                                stop_spellings.append(f"{registry_alias}:stop:0:0")
                            extra = {"fixture": f"saved value for {home.name}"}
                            stop_settings: dict[str, dict[str, object]] = {
                                key: {"trusted_hash": hashes[stop["command"]], "enabled": False, **extra}
                                for key in stop_spellings
                            }
                            edits: dict[str, object] = {source[0]: None, destination[0]: {"trusted_hash": moved_hash}}
                            if index == affected:
                                edits[stop_spellings[-1]] = None
                                revoked_settings.update(stop_settings)
                            known_homes = [str(home), str(alias)] if index == 0 else [str(home)]
                            plans[config] = {
                                "registry": str(self.hooks_file), "homes": known_homes, "edits": edits,
                                "retained": {}, "unproved": {}, "audit": [],
                            }
                            expected_revoked[config] = revoked_settings
                            expected_preserved[config] = stop_settings if index != affected else {}
                            stop_trust = dict.fromkeys(stop_spellings, hashes[stop["command"]])
                            initial_trust[state_file] = {active_key: moved_hash, **stop_trust}
                            final_states[state_file] = {
                                "trust": stop_trust if index != affected else {},
                                "disabled": stop_spellings, "extra_settings": dict.fromkeys(stop_spellings, extra),
                            }
                            _ = state_file.write_text(json.dumps({
                                **final_states[state_file], "trust": initial_trust[state_file],
                            }))
                        journal = {
                            "version": 5, "original_hash": hashlib.sha256(original.encode()).hexdigest(),
                            "registry": replacement, "configs": plans,
                        }
                        _ = marker.write_text(json.dumps(journal))
                        saved_marker = marker.read_bytes()
                        if interrupt:
                            stopped = subprocess.run(
                                [sys.executable, "-c", INTERRUPTED_REMOVAL, f"after trust write:{self.codex_home}:1",
                                 str(marker), str(self.hooks_file), str(INSTALLER), retry],
                                env=self.environment, text=True, capture_output=True, check=False,
                                cwd=INSTALLER.resolve().parents[2], timeout=TEST_HANG_GUARD_SECS,
                            )
                            self.assertEqual(stopped.returncode, -signal.SIGKILL, stopped.stdout + stopped.stderr)
                            self.assertEqual(self.trust(), final_states[self.state_file]["trust"])
                            secondary = cast(dict[str, object], json.loads(other_state.read_text()))
                            self.assertEqual(secondary["trust"], initial_trust[other_state])
                        next_retry = "remove-showrunner-guard" if retry == "install" else "install"
                        archived_bytes = b""
                        for command in (retry, next_retry):
                            result = self.run_installer(command)
                            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                            self.assertIn("legacy journal lacks complete before and after values", result.stderr)
                            self.assertEqual(marker.read_bytes(), saved_marker)
                            self.assertEqual(self.hooks_file.read_text(), current)
                            for _, state_file, _ in configs:
                                state = cast(dict[str, object], json.loads(state_file.read_text()))
                                self.assertEqual({field: state[field] for field in final_states[state_file]},
                                                 final_states[state_file])
                            archive, = self.codex_home.glob(".codex-hooks-install-recovery-legacy-*.json")
                            evidence = cast(dict[str, object], json.loads(archive.read_text()))
                            self.assertEqual(evidence["revoked"], expected_revoked)
                            self.assertEqual(evidence["preserved"], expected_preserved)
                            if archived_bytes:
                                self.assertEqual(archive.read_bytes(), archived_bytes)
                            archived_bytes = archive.read_bytes()
        (other / "hooks.json").unlink()

    def assert_legacy_install_recovery(self) -> None:
        retired = {"type": "command", "command": RETIRED_COMMAND, "timeout": 99}
        unique = {"type": "command", "command": "custom unique", "timeout": 15}
        duplicate = {**unique, "command": "custom ambiguous"}
        post = self.guard_handler("post")
        original = json.dumps({"hooks": {"PostToolUse": [
            {"matcher": "apply_patch", "hooks": [retired, unique]},
            {"matcher": "apply_patch", "hooks": [retired, duplicate, duplicate]},
            {"matcher": "apply_patch", "hooks": [post]},
        ]}})
        replacement = json.dumps({"hooks": {"PostToolUse": [
            {"matcher": "apply_patch", "hooks": [unique]},
            {"matcher": "apply_patch", "hooks": [duplicate, duplicate]},
            {"matcher": "apply_patch", "hooks": [post]},
        ]}})
        _ = self.hooks_file.write_text(original)
        entries = {entry["key"]: entry for entry in self.listed_handlers()}
        unique_slot = f"{self.hooks_file}:post_tool_use:0"
        duplicate_slot = f"{self.hooks_file}:post_tool_use:1"
        unique_hash = cast(str, entries[f"{unique_slot}:1"]["currentHash"])
        duplicate_hash = cast(str, entries[f"{duplicate_slot}:1"]["currentHash"])
        initial = {f"{unique_slot}:1": unique_hash, f"{duplicate_slot}:1": duplicate_hash,
                   f"{duplicate_slot}:0": duplicate_hash}
        self.save_trust(initial)
        record: dict[str, object] = {
            "version": 1, "home": str(self.codex_home), "config": str(self.codex_home / "config.toml"),
            "original_hash": hashlib.sha256(original.encode()).hexdigest(),
            "registry_hash": hashlib.sha256(replacement.encode()).hexdigest(),
            "trust": [entry for entry in self.listed_handlers() if entry["trustStatus"] == "trusted"],
        }
        journal = self.hooks_file.with_name(".codex-hooks-install.json")
        for name, current, retry in (
            ("original", original, "remove-showrunner-guard"),
            ("replaced", replacement, "install"),
            ("replaced", replacement, "remove-showrunner-guard"),
            ("archived", replacement, "remove-showrunner-guard"),
            ("unrelated", replacement + "\n", "install"),
            ("unrelated", replacement + "\n", "remove-showrunner-guard"),
            ("retargeted config", original, "install"),
        ):
            with self.subTest(legacy=name, retry=retry):
                for evidence in self.codex_home.glob(".codex-hooks-install-recovery-*.json"):
                    evidence.unlink()
                _ = self.hooks_file.write_text(current)
                self.save_trust(initial)
                recorded = {**record, "config": str(self.root / "wrong.toml")} if name == "retargeted config" else record
                _ = journal.write_text(json.dumps(recorded))
                if name == "archived":
                    stopped = subprocess.run(
                        [sys.executable, "-c", INTERRUPTED_REMOVAL, "after recovery record",
                         str(journal), str(self.hooks_file), str(INSTALLER), "install"],
                        env=self.environment, text=True, capture_output=True, check=False,
                        cwd=INSTALLER.resolve().parents[2], timeout=TEST_HANG_GUARD_SECS,
                    )
                    self.assertEqual(stopped.returncode, -signal.SIGKILL, stopped.stdout + stopped.stderr)
                    self.assertEqual(json.loads(journal.read_text()), record)
                    self.assertEqual(self.trust(), initial)
                result = self.run_installer(retry)
                if name in {"unrelated", "retargeted config"}:
                    self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                    reason = ("registry matches neither legacy install hash" if name == "unrelated"
                              else "hook or trust destination changed")
                    self.assertIn(reason, result.stderr)
                    self.assertEqual(self.hooks_file.read_text(), current)
                    self.assertEqual(self.trust(), initial)
                    self.assertEqual(json.loads(journal.read_text()), recorded)
                    continue
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertIn("legacy journal lacks complete before and after values", result.stderr)
                self.assertEqual(self.hooks_file.read_text(), current)
                self.assertEqual(self.trust(), {})
                self.assertEqual(json.loads(journal.read_text()), record)
                self.assertEqual([(entry["command"], entry["trustStatus"]) for entry in self.listed_handlers()
                                  if entry["command"] != RETIRED_COMMAND],
                                 [(unique["command"], "untrusted"), (duplicate["command"], "untrusted"),
                                  (duplicate["command"], "untrusted"), (post["command"], "untrusted")])
                evidence, = self.codex_home.glob(".codex-hooks-install-recovery-*.json")
                saved = cast(dict[str, object], json.loads(evidence.read_text()))
                self.assertEqual(saved["journal"], record)
                self.assertEqual(saved["revoked"], {
                    str(self.codex_home / "config.toml"): {key: {"trusted_hash": value} for key, value in initial.items()},
                })

        self.assert_shared_legacy_recovery(journal, original, replacement, record, initial, unique_hash, duplicate_hash)

    def assert_shared_legacy_recovery(
        self, journal: Path, original: str, replacement: str, record: dict[str, object],
        initial: dict[str, str], unique_hash: str, duplicate_hash: str,
    ) -> None:
        linked_home = self.home / ".codex2"
        linked_home.mkdir()
        linked_hooks = linked_home / "hooks.json"
        linked_hooks.symlink_to(self.hooks_file)
        linked_state = self.root / "legacy-linked-state.json"
        linked_config = linked_home / "config.toml"
        executable = self.root / "bin/codex"
        _ = executable.write_text(RECOVERY_STUB.replace("__PYTHON__", sys.executable))
        shared_config = self.codex_home / "config.toml"
        other_key = f"{self.root / 'other-hooks.json'}:stop:0:0"
        for scenario, stop_point, fault in (
            ("original", "", ""),
            ("separate configs", "", ""),
            ("shared config", "", ""),
            ("canonical and alias keys", "", ""),
            ("saved evidence", "after recovery record", ""),
            ("between home writes", "after trust write", ""),
            ("before removal", "before journal removal", ""),
            ("second read fails", "", "STUB_FAILED_READ"),
            ("second write is unsaved", "", "STUB_UNSAVED_WRITE"),
            ("second config changes", "after recovery record", "retarget"),
            ("new trust before verification", "", "new trust"),
        ):
            for retry in ("install", "remove-showrunner-guard"):
                with self.subTest(shared_legacy=scenario, retry=retry):
                    for evidence in self.codex_home.glob(".codex-hooks-install-recovery-*.json"):
                        evidence.unlink()
                    linked_config.unlink(missing_ok=True)
                    if scenario == "shared config":
                        linked_config.symlink_to(shared_config)
                    current = original if scenario == "original" else replacement
                    _ = self.hooks_file.write_text(current)
                    _ = journal.write_text(json.dumps(record))
                    second_file = self.state_file if scenario == "shared config" else linked_state
                    self.environment["STUB_ACCOUNT_STATES"] = json.dumps({str(linked_home): str(second_file)})
                    first_prefix = f"{self.hooks_file}:post_tool_use"
                    second_prefix = f"{linked_hooks}:post_tool_use"
                    first_stale = f"{first_prefix}:88:0"
                    second_stale = f"{second_prefix}:88:0"
                    canonical_only = {f"{first_prefix}:94:0": "old canonical hash", f"{first_prefix}:0:0": unique_hash}
                    added_key = f"{first_prefix}:95:0"
                    disabled = f"{second_prefix}:0:0"
                    first = {**initial, other_key: "other-source-hash"}
                    second = {other_key: "other-source-hash"}
                    if scenario == "original":
                        second[f"{second_prefix}:1:2"] = duplicate_hash
                    else:
                        first[first_stale] = "old-first-hash"
                        second.update({second_stale: "old-second-hash", f"{second_prefix}:1:0": duplicate_hash,
                                       disabled: unique_hash})
                        if scenario == "canonical and alias keys":
                            second.update(canonical_only)
                    if second_file == self.state_file:
                        first.update(second)
                    _ = self.state_file.write_text(json.dumps({"trust": first,
                                                               "disabled": [disabled] if second_file == self.state_file else []}))
                    if second_file != self.state_file:
                        _ = second_file.write_text(json.dumps({"trust": second,
                                                               "disabled": [] if scenario == "original" else [disabled]}))
                    if stop_point:
                        stopped = subprocess.run(
                            [sys.executable, "-c", INTERRUPTED_REMOVAL, stop_point,
                             str(journal), str(self.hooks_file), str(INSTALLER), retry],
                            env=self.environment, text=True, capture_output=True, check=False,
                            cwd=INSTALLER.resolve().parents[2], timeout=TEST_HANG_GUARD_SECS,
                        )
                        if stop_point == "before journal removal":
                            self.assertEqual(stopped.returncode, 1, stopped.stdout + stopped.stderr)
                            self.assertIn("legacy journal lacks complete before and after values", stopped.stderr)
                            self.assertEqual(json.loads(journal.read_text()), record)
                        else:
                            self.assertEqual(stopped.returncode, -signal.SIGKILL, stopped.stdout + stopped.stderr)
                    if fault:
                        if fault == "retarget":
                            linked_config.symlink_to(self.root / "other-config.toml")
                            try:
                                refused = self.run_installer(retry)
                            finally:
                                linked_config.unlink()
                            reason = "hook or trust destination changed"
                        elif fault == "new trust":
                            with mock.patch.dict(self.environment, {"STUB_COVERAGE_HOME": str(linked_home),
                                                                    "STUB_COVERAGE_KEY": added_key}):
                                refused = self.run_installer(retry)
                            reason = "uncovered hook trust"
                        else:
                            with mock.patch.dict(self.environment, {fault: str(linked_home)}):
                                refused = self.run_installer(retry)
                            reason = ("fixture config read failed" if fault == "STUB_FAILED_READ"
                                      else "saved hook trust does not match the journal")
                        self.assertEqual(refused.returncode, 1, refused.stdout + refused.stderr)
                        self.assertIn(reason, refused.stderr)
                        self.assertTrue(journal.read_bytes())
                        failed_state = cast(dict[str, dict[str, object]], json.loads(second_file.read_text()))
                        if fault == "new trust":
                            self.assertEqual(failed_state["trust"][added_key], "added after planning")
                        else:
                            self.assertEqual(failed_state["trust"][second_stale], "old-second-hash")
                    recovered = self.run_installer(retry)
                    self.assertEqual(recovered.returncode, 1, recovered.stdout + recovered.stderr)
                    self.assertEqual(recovered.stdout, "")
                    self.assertIn("legacy journal lacks complete before and after values", recovered.stderr)
                    self.assertEqual(self.hooks_file.read_text(), current)
                    for home, state_file, stale in ((self.codex_home, self.state_file, first_stale),
                                                    (linked_home, second_file, second_stale)):
                        with mock.patch.object(self, "codex_home", home):
                            entries = self.listed_handlers()
                        status = [(entry["trustStatus"], entry["enabled"]) for entry in entries
                                  if entry["command"] in {"custom unique", "custom ambiguous"}]
                        primary = home == self.codex_home
                        expected = [("untrusted", True), ("untrusted", True), ("untrusted", True)]
                        if scenario != "original" and not primary:
                            expected[0] = ("untrusted", False)
                        self.assertEqual(status, expected, home)
                        state = cast(dict[str, object], json.loads(state_file.read_text()))
                        trust = cast(dict[str, str], state["trust"])
                        self.assertNotIn(stale, trust)
                        self.assertEqual(trust, {other_key: "other-source-hash"})
                        if scenario == "canonical and alias keys" and not primary:
                            self.assertEqual(state["disabled"], [disabled])
                        for entry in entries:
                            if entry["command"] in {"custom unique", "custom ambiguous"}:
                                key = cast(str, entry["key"])
                                expected_hash = entry["currentHash"] if entry["trustStatus"] == "trusted" else None
                                self.assertEqual(trust.get(key), expected_hash)
                    archive, = self.codex_home.glob(".codex-hooks-install-recovery-*.json")
                    evidence = cast(dict[str, object], json.loads(archive.read_text()))
                    if fault == "new trust":
                        second[added_key] = "added after planning"
                    revoked: dict[str, dict[str, object]] = {
                        str(shared_config): {key: {"trusted_hash": value} for key, value in first.items() if key != other_key},
                    }
                    config = str(shared_config if second_file == self.state_file else linked_config)
                    revoked.setdefault(config, {}).update(
                        (key, {"trusted_hash": value, **({"enabled": False} if key == disabled else {})})
                        for key, value in second.items() if key != other_key
                    )
                    self.assertEqual(evidence["revoked"], revoked)
                    self.assertEqual(json.loads(journal.read_text()), record)

    def guard_handler(self, event_prefix: str) -> dict[str, object]:
        command = ('"$HOME/.claude/scripts/lib/py" '
                   f'"$HOME/.claude/scripts/hooks/{event_prefix}-tool-use-showrunner-guard-codex.py"')
        return {"type": "command", "command": command, "timeout": 20}

    def test_remove_showrunner_guard_keeps_trust_in_each_home_that_shares_hooks(self) -> None:
        linked_home = self.home / ".codex2"
        linked_home.mkdir()
        linked_hooks = linked_home / "hooks.json"
        linked_hooks.symlink_to(self.hooks_file)
        linked_state = self.root / "linked-state.json"
        self.environment["STUB_ACCOUNT_STATES"] = json.dumps({str(linked_home): str(linked_state)})
        _ = self.hooks_file.write_text(json.dumps({"hooks": {"PostToolUse": [
            {"matcher": "apply_patch", "hooks": [self.guard_handler("post"), FN_HANDLER]},
        ]}}))
        for home, state_file in ((self.codex_home, self.state_file), (linked_home, linked_state)):
            with mock.patch.object(self, "codex_home", home):
                entries = self.listed_handlers()
            _ = state_file.write_text(json.dumps({"trust": {entry["key"]: entry["currentHash"] for entry in entries}}))

        result = self.run_installer("remove-showrunner-guard")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(os.readlink(linked_hooks), str(self.hooks_file))
        for home in (self.codex_home, linked_home):
            with self.subTest(home=home), mock.patch.object(self, "codex_home", home):
                self.assertEqual([(entry["command"], entry["trustStatus"]) for entry in self.listed_handlers()],
                                 [(FN_COMMAND, "trusted")])

    def test_remove_showrunner_guard_continues_after_an_invalid_registry(self) -> None:
        invalid = self.home / ".codex"
        invalid.mkdir()
        invalid_hooks = invalid / "hooks.json"
        initial_hooks = json.dumps({"hooks": {"PostToolUse": [
            {"matcher": "apply_patch", "hooks": [self.guard_handler("post"), FN_HANDLER]},
        ]}})
        for invalid_kind in ("malformed JSON", "named pipe"):
            with self.subTest(invalid_registry=invalid_kind):
                _ = self.hooks_file.write_text(initial_hooks)
                invalid_hooks.unlink(missing_ok=True)
                if invalid_kind == "named pipe":
                    os.mkfifo(invalid_hooks)
                    try:
                        result = subprocess.run(
                            [sys.executable, str(INSTALLER), "remove-showrunner-guard"],
                            env=self.environment, text=True, capture_output=True, check=False, timeout=10,
                        )
                    except subprocess.TimeoutExpired:
                        self.fail("Removal blocked on a named pipe before processing the valid registry.")
                    self.assertTrue(stat.S_ISFIFO(invalid_hooks.stat().st_mode))
                else:
                    _ = invalid_hooks.write_text("{\n")
                    result = self.run_installer("remove-showrunner-guard")
                    self.assertEqual(invalid_hooks.read_text(), "{\n")
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertEqual(sum(str(invalid_hooks) in line for line in result.stderr.splitlines()), 1, result.stderr)
                self.assertEqual(self.groups(), [{"matcher": "apply_patch", "hooks": [FN_HANDLER]}])

    def test_remove_showrunner_guard_includes_the_numbered_home_for_the_default_login(self) -> None:
        default = self.home / ".codex"
        default.mkdir()
        numbered = self.home / ".codex2"
        numbered.mkdir()
        login = "fixture@example.invalid"
        claims = base64.urlsafe_b64encode(json.dumps({"email": login}).encode()).decode()
        _ = (default / "auth.json").write_text(json.dumps({"tokens": {"id_token": f"x.{claims}.y"}}))
        notes = self.root / "notes"
        notes.mkdir()
        _ = (notes / "codex2.md").write_text(f"---\nlogin: {login}\n---\n")
        self.environment["AGENT_NOTES_DIR"] = str(notes)
        registry = numbered / "hooks.json"
        initial_hooks = json.dumps({"hooks": {"PostToolUse": [
            {"matcher": "apply_patch", "hooks": [self.guard_handler("post"), FN_HANDLER]},
        ]}})
        for explicit_home in (self.codex_home, numbered):
            with self.subTest(explicit_home=explicit_home):
                self.environment["CODEX_HOME"] = str(explicit_home)
                _ = registry.write_text(initial_hooks)

                result = self.run_installer("remove-showrunner-guard")

                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(cast(object, json.loads(registry.read_text())), {
                    "hooks": {"PostToolUse": [{"matcher": "apply_patch", "hooks": [FN_HANDLER]}]},
                })


class ShowrunnerGuardCodexTests(GuardFixture):
    def run_registered_guard(self, tool: str, fields: dict[str, object]) -> subprocess.CompletedProcess[str]:
        fixture_hooks = (codex_hooks.SHOWRUNNER_GUARD,)
        hook = next(hook for hook in fixture_hooks if hook.event == "PreToolUse" and re.fullmatch(hook.matcher, tool))
        path = shlex.split(hook.command)[-1].replace("$HOME/.claude", str(INSTALLER.resolve().parents[2]))
        return self.run_front(path, tool, fields, cwd=self.root)

    def test_fixture_registration_guards_bash_and_all_patch_headers(self) -> None:
        target = "home/.claude/target.txt"
        outside = "outside/target.txt"
        cases = (
            ("add", f"*** Add File: {target}\n+fixture", target),
            ("delete", f"*** Delete File: {target}", target),
            ("move destination", f"*** Update File: {outside}\n*** Move to: {target}\n@@\n-old\n+new", target),
            ("move source", f"*** Update File: {target}\n*** Move to: {outside}\n@@\n-old\n+new", target),
            ("later target", f"*** Add File: {outside}\n+fixture\n*** Delete File: {target}", target),
            ("outside", f"*** Add File: {outside}\n+fixture", ""),
        )
        for case, body, protected in cases:
            with self.subTest(case=case):
                result = self.run_registered_guard("apply_patch", {"command": f"*** Begin Patch\n{body}\n*** End Patch\n"})
                if protected:
                    self.assertIn(str(self.root / protected), self.assert_deny(result))
                else:
                    self.assert_allow(result)
        command = shlex.join(["git", "-C", str(self.main), "add", "target.txt"])
        _ = self.assert_deny(self.run_registered_guard("Bash", {"command": command}))


if __name__ == "__main__":
    _ = unittest.main()
