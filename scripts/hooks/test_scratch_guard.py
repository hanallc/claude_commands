"""The scratch guard refuses a command that makes a new loose folder in ~, /tmp, $TMPDIR, /tmp/claude, ~/rust or the
session root."""

from __future__ import annotations

import importlib
import json
import os
import secrets
import shlex
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from collections.abc import Callable
from typing import TypedDict, cast, override
from unittest import mock
from scripts.lint.memory_test_support import write_config_fixture
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


HOOK = Path(__file__).with_name("pre-tool-use-scratch-guard.py")
remake_session_temp = cast(Callable[[str], None],
                           importlib.import_module(f"scripts.hooks.{HOOK.stem}").remake_session_temp)


class HookDecision(TypedDict):
    hookEventName: str
    permissionDecision: str
    permissionDecisionReason: str


class HookResponse(TypedDict):
    hookSpecificOutput: HookDecision


class ScratchGuardTests(unittest.TestCase):
    home: Path = Path()
    temp: Path = Path()
    session_root: Path = Path()
    new_root: Path = Path()
    name: str = ""
    build: Path = Path()

    @override
    def setUp(self) -> None:
        base = Path(os.path.realpath(self.enterContext(tempfile.TemporaryDirectory(prefix="scratch-guard-test-"))))
        self.home, self.temp = base / "home", base / "temp"
        self.build = base / "build"
        config = base / "config"
        _ = write_config_fixture(config / "build_folders.conf",
            f"roots.build={self.build}\nroots.build.natedev={self.build}\n"
            + f"roots.workspace={self.home / 'worktrees'}\n")
        # session_root is the former root; new_root is the session root, absent until a test makes it.
        self.session_root, self.new_root = base / "sessions", base / "session"
        # The scratch parent's guard applies even inside a unit's TMPDIR.
        self.enterContext(mock.patch.dict(os.environ, {"SESSION_FORMER_ROOT": str(self.session_root),
                                                       "SESSION_TEMP_ROOT": str(self.new_root),
                                                       "CONF_DIR": str(config),
                                                       "SCRATCH_ROOT": str(self.temp / "scratch")}))
        # The home folders a real home holds; a new folder directly in home is refused.
        (self.home / "rust" / "hana").mkdir(parents=True)
        (self.home / "worktrees").mkdir()
        self.temp.mkdir()
        # A name no real folder in /tmp holds, so the real /tmp root judges it as new.
        self.name = f"scratch-guard-{secrets.token_hex(6)}"

    def run_hook(self, command: str | list[str], cwd: Path | None = None, tool: str = "Bash",
                 temp: Path | None = None) -> str:
        payload = json.dumps({"session_id": "scratch-session", "tool_name": tool, "tool_input": {"command": command},
                              "cwd": str(cwd or self.home)})
        environment = {**os.environ, "HOME": str(self.home), "TMPDIR": str(temp or self.temp)}
        result = subprocess.run([str(HOOK)], input=payload, capture_output=True, text=True, check=False,
                                timeout=TEST_HANG_GUARD_SECS, env=environment)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def assert_refused(self, command: str | list[str], folder: str, cwd: Path | None = None) -> None:
        output = self.run_hook(command, cwd)
        self.assertNotEqual(output, "", command)
        decision = cast(HookResponse, json.loads(output))["hookSpecificOutput"]
        self.assertEqual((decision["hookEventName"], decision["permissionDecision"]), ("PreToolUse", "deny"))
        self.assertIn(f"makes a new loose folder ({folder})", decision["permissionDecisionReason"])
        self.assertIn("scratch.py new [--task", decision["permissionDecisionReason"])

    def assert_allowed(self, command: str | list[str], cwd: Path | None = None, tool: str = "Bash") -> None:
        self.assertEqual(self.run_hook(command, cwd, tool), "", command)

    def write_hook(self, tool: str, key: str, path: Path) -> str:
        payload = json.dumps({"session_id": "scratch-session", "tool_name": tool,
                              "tool_input": {key: str(path)}, "cwd": str(self.home)})
        environment = {**os.environ, "HOME": str(self.home), "TMPDIR": str(self.temp)}
        return subprocess.run([str(HOOK)], input=payload, capture_output=True, text=True, check=True, timeout=TEST_HANG_GUARD_SECS,
                              env=environment).stdout

    def test_a_write_into_a_new_folder_of_a_root_is_refused_and_rust_scratch_is_a_root(self) -> None:
        rust = self.home / "rust"
        (rust / "scratch" / "old").mkdir(parents=True)
        for tool, key, path, folder in [("Write", "file_path", rust / "loose" / "a.rs", rust / "loose"),
                                        ("NotebookEdit", "notebook_path", rust / "scratch" / "new" / "n.ipynb",
                                         rust / "scratch" / "new")]:
            decision = cast(HookResponse, json.loads(self.write_hook(tool, key, path)))["hookSpecificOutput"]
            self.assertIn(f"makes a new loose folder ({folder})", decision["permissionDecisionReason"])
        for path in (rust / "hana" / "new" / "a.rs", rust / "scratch" / "old" / "a", rust / "notes.md"):
            self.assertEqual(self.write_hook("Write", "file_path", path), "", path)
        folder = rust / "scratch" / self.name
        self.assert_refused(f"mkdir {shlex.quote(str(folder))}", str(folder))

    def test_manual_checkout_creation_is_refused_under_managed_roots(self) -> None:
        sources = self.home / "worktrees"
        workspace = sources / "hana/demo/change"
        numbered = self.build / "hana/demo/1"
        for path in (workspace, numbered):
            destination = shlex.quote(str(path))
            self.assert_refused(f"mkdir -p {destination}", str(path))
            self.assert_refused(f"git clone origin {destination}", str(path))
            self.assertIn(str(path), self.write_hook("Write", "file_path", path / "README"))
        self.assert_allowed("worktrees.py make hana demo change")

    def test_a_worktree_alias_allows_regular_edits(self) -> None:
        repository = self.home / "rust/hana"
        workspace = self.home / "worktrees/hana/demo/change"
        workspace.parent.mkdir(parents=True)
        environment = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
        for command in (("init",), ("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                                    "-c", "commit.gpgsign=false", "commit", "--allow-empty", "-m", "Fixture"),
                        ("worktree", "add", "--detach", str(workspace))):
            _ = subprocess.run(["git", "-C", str(repository), *command], capture_output=True,
                               check=True, timeout=TEST_HANG_GUARD_SECS, env=environment)
        alias = self.home / "old-worktree"
        alias.symlink_to(workspace, target_is_directory=True)
        self.assert_allowed(f"mkdir -p {shlex.quote(str(alias / 'src/sub'))}")
        self.assertEqual(self.write_hook("Write", "file_path", alias / "src/new.rs"), "")
        self.assert_refused(f"git clone origin {shlex.quote(str(alias / 'nested'))}", str(workspace / "nested"))

    def test_deleted_session_temp_folder_is_restored_for_each_tool(self) -> None:
        own = self.session_root / "scratch-session"
        own.mkdir(parents=True, mode=0o700)
        for tool, key in (("Bash", "command"), ("Write", "file_path"), ("NotebookEdit", "notebook_path")):
            own.rmdir()
            with self.subTest(tool=tool):
                output = (self.run_hook("pwd", temp=own) if tool == "Bash"
                          else self.write_hook(tool, key, self.home / "notes.md"))
                self.assertEqual(output, "")
                self.assertTrue(own.is_dir())
                self.assertEqual(stat.S_IMODE(own.stat().st_mode), 0o700)
                self.assertEqual(list(self.session_root.iterdir()), [own])

    def test_existing_session_temp_folder_is_left_as_it_is(self) -> None:
        own = self.session_root / "scratch-session"
        own.mkdir(parents=True, mode=0o750)
        with mock.patch.object(Path, "mkdir") as mkdir:
            remake_session_temp(json.dumps({"session_id": "scratch-session"}))
            mkdir.assert_not_called()
        self.assertEqual(stat.S_IMODE(own.stat().st_mode), 0o750)

    def test_a_deleted_session_folder_in_the_session_root_is_made_again(self) -> None:
        own = self.new_root / "verify"
        (self.new_root / ".id").mkdir(parents=True)
        (self.new_root / ".id" / "scratch-session").symlink_to("verify")
        self.assertEqual(self.run_hook("pwd"), "")
        self.assertTrue(own.is_dir())
        self.assertEqual(os.readlink(self.new_root / ".id" / "scratch-session"), "verify")
        self.assertFalse(self.session_root.exists())

    def test_a_new_folder_in_home_or_the_session_root_is_refused(self) -> None:
        (self.new_root / "verify").mkdir(parents=True)
        self.assert_refused("mkdir ~/port-gate-tree-test", str(self.home / "port-gate-tree-test"))
        self.assert_refused("git clone a ~/port-gate-tree-test2", str(self.home / "port-gate-tree-test2"))
        self.assert_refused(f"mkdir {shlex.quote(str(self.new_root / 'loose'))}", str(self.new_root / "loose"))
        self.assert_allowed("mkdir ~/rust/hana/notes")
        self.assert_allowed(f"mkdir -p {shlex.quote(str(self.new_root / 'verify/fast-track'))}")
        self.assertEqual(self.run_hook("mktemp -d", temp=self.new_root / "verify"), "")

    def test_missing_or_bad_session_id_does_not_read_or_create_a_folder(self) -> None:
        bad_ids: tuple[object, ...] = ("", "../other", "/absolute", "a/b", ".", "..", "id\n", None, 1, [])
        payloads = ["{}", "null", "{", *(json.dumps({"session_id": session_id})
                    for session_id in bad_ids)]
        for raw in payloads:
            with self.subTest(raw=raw), mock.patch.object(os, "stat") as read_stat, \
                    mock.patch.object(Path, "mkdir") as mkdir:
                remake_session_temp(raw)
                read_stat.assert_not_called()
                mkdir.assert_not_called()
        self.assertFalse(self.session_root.exists())

    def test_temp_folder_creation_failure_allows_the_tool_call(self) -> None:
        _ = self.session_root.write_text("a file blocks the session root")
        self.assertEqual(self.run_hook("pwd"), "")
        self.assertEqual(self.session_root.read_text(), "a file blocks the session root")

    def test_temp_folder_mkdir_failure_allows_the_tool_call(self) -> None:
        with mock.patch.object(Path, "mkdir", side_effect=PermissionError("no access")) as mkdir:
            remake_session_temp(json.dumps({"session_id": "scratch-session"}))
            mkdir.assert_called_once_with(mode=0o700, parents=True, exist_ok=True)

    def test_quoted_punctuation_is_data_inside_a_scratch_root(self) -> None:
        for punctuation in ";&|(){}\n":
            for word in ("'" + punctuation + "'", '"' + punctuation + '"',
                         "'" + punctuation * 2 + "'", '"' + punctuation * 2 + '"'):
                with self.subTest(word=word):
                    self.assert_allowed(f"printf '%s\\n' {word} git clone a b", cwd=self.temp)
        for operator in (";", "&&", "|", "\n"):
            with self.subTest(operator=operator):
                self.assert_refused(f"printf x{operator} git clone a b", str(self.temp / "b"), cwd=self.temp)
        self.assert_refused("printf ';';git clone a b", str(self.temp / "b"), cwd=self.temp)

    def test_escaped_punctuation_is_data_inside_a_scratch_root(self) -> None:
        for punctuation in ";&|(){}\n":
            for word in ("\\" + punctuation, ("\\" + punctuation) * 2):
                with self.subTest(word=word):
                    self.assert_allowed(f"printf '%s\\n' {word} git clone a b", cwd=self.temp)

        self.assert_refused("printf \\;;git clone a b", str(self.temp / "b"), cwd=self.temp)

    def test_empty_quoted_words_do_not_start_a_new_command(self) -> None:
        for word in ("''", '""'):
            with self.subTest(word=word):
                self.assert_allowed(f"printf {word} git clone a b", cwd=self.temp)
                self.assert_refused(f"printf {word};git clone a b", str(self.temp / "b"), cwd=self.temp)

    def test_line_continuation_after_a_separator_does_not_hide_a_folder_command(self) -> None:
        folder = self.temp / "new"
        for space in ("", " "):
            for command in ("mkdir new", "cp -r source new", "cargo new new"):
                with self.subTest(space=space, command=command):
                    self.assert_refused("printf x;\\\n" + space + command, str(folder), cwd=self.temp)

    def test_line_continuation_inside_a_command_name_joins_the_word(self) -> None:
        folder = self.temp / "new"
        self.assert_refused("mk\\\ndir new", str(folder), cwd=self.temp)
        self.assert_refused("\\\nmkdir new", str(folder), cwd=self.temp)

    def test_line_continuation_before_a_folder_argument_is_deleted(self) -> None:
        folder = self.temp / "new"
        self.assert_refused("mkdir \\\nnew", str(folder), cwd=self.temp)

    def test_double_quotes_delete_a_line_continuation(self) -> None:
        folder = self.temp / "new"
        self.assert_refused('"mk\\\ndir" new', str(folder), cwd=self.temp)
        self.assert_refused('mkdir "ne\\\nw"', str(folder), cwd=self.temp)

    def test_single_quotes_keep_a_line_continuation(self) -> None:
        folder = self.temp / "literal\\\nname"
        self.assert_allowed("'mk\\\ndir' new", cwd=self.temp)
        self.assert_refused("mkdir 'literal\\\nname'", str(folder), cwd=self.temp)
        self.assert_refused("printf 'literal\\\ntext';\\\nmkdir new",
                            str(self.temp / "new"), cwd=self.temp)

    def test_an_escaped_backslash_keeps_the_newline_separator(self) -> None:
        folder = self.temp / "new"
        for count in (1, 2, 4):
            with self.subTest(backslashes=count):
                self.assert_refused("printf x;" + "\\" * count + "\n mkdir new",
                                    str(folder), cwd=self.temp)
        self.assert_allowed("printf x;" + "\\" * 3 + "\n mkdir new", cwd=self.temp)

    def test_quoted_separator_after_a_line_continuation_remains_data(self) -> None:
        self.assert_allowed("printf x \\\n ';' mkdir new", cwd=self.temp)
        self.assert_refused("printf x \\\n ';';\\\nmkdir new", str(self.temp / "new"), cwd=self.temp)

    def test_each_judged_command_making_a_loose_folder_is_refused(self) -> None:
        tmp, rust = os.path.realpath("/tmp"), self.home / "rust"
        for command, folder in [
            (f"mkdir -p /tmp/{self.name}/sub", f"{tmp}/{self.name}"),
            (f"mkdir -m 755 ~/rust/{self.name}", f"{rust}/{self.name}"),
            (f"cd /tmp && git clone git@github.com:natepiano/{self.name}.git", f"{tmp}/{self.name}"),
            (f"git -C ~/rust/hana worktree add -b x ../{self.name}", f"{rust}/{self.name}"),
            (f"cp -a ~/rust/hana /tmp/{self.name}", f"{tmp}/{self.name}"),
            (f"rsync -a --exclude target ~/rust/hana ~/rust/{self.name}", f"{rust}/{self.name}"),
            (f"cargo new --name x /tmp/claude/{self.name}", f"{os.path.realpath('/tmp/claude')}/{self.name}"),
            ("mktemp -d", f"{self.temp}/tmp.XXXXXXXXXX"),
            ("tar -xzf a.tgz -C ~/rust", f"{rust}/(archive entries)"),
        ]:
            self.assert_refused(command, folder)

    def test_a_copy_into_an_existing_root_names_the_folder_it_makes(self) -> None:
        self.assert_allowed("cp -r ~/rust/hana ~/rust/")  # hana exists: nothing new
        self.assert_refused(f"cp -R hana {shlex.quote(str(self.temp))}", f"{self.temp}/hana", cwd=self.home / "rust")

    def test_task_scratch_scratchpads_run_folders_and_worktrees_are_allowed(self) -> None:
        for command in [
            "mkdir -p /tmp/claude-1000/-home-x/session/scratchpad/copy",
            "mkdir -p /tmp/claude/unit/abc /tmp/claude/delegate/abc /tmp/claude/scratch/run-x",
            "mkdir -p ~/rust/repro",
            "git clone x /tmp/claude/scratch/cargo-liner-C54-demo",
            f"make_a_worktree hana ~/rust/{self.name}",
            "mkdir -p ~/rust/hana/target/sub",
            "mktemp -d -p /tmp/claude-1000/-home-x/session/scratchpad",
            "mktemp /tmp/file.XXXX",
            "cp ~/rust/hana/a /tmp/a",
            "tar -czf /tmp/out.tgz -C ~/rust hana",
            f"ssh mac 'mkdir /tmp/{self.name}'",
            f'mkdir "$WORK/{self.name}"',
        ]:
            self.assert_allowed(command)

    def test_repository_creation_under_unit_and_session_roots_is_refused(self) -> None:
        (self.home / "rust" / "hana" / ".git").mkdir()
        for destination in ("/tmp/claude/unit/private-run/dylib_ab/hana",
                            str(self.session_root / "scratch-session" / "dylib_ab" / "hana")):
            target = shlex.quote(destination)
            for command in (f"git clone ~/rust/hana {target}",
                            f"git -C ~/rust/hana worktree add {target}",
                            f"cp -r ~/rust/hana {target}", f"rsync -a ~/rust/hana/ {target}"):
                with self.subTest(command=command):
                    output = self.run_hook(command)
                    self.assertNotEqual(output, "")
                    decision = cast(HookResponse, json.loads(output))["hookSpecificOutput"]
                    self.assertEqual(decision["permissionDecision"], "deny")
                    self.assertIn("copies.py run", decision["permissionDecisionReason"])
        self.assert_allowed("mkdir -p /tmp/claude/unit/private-run/data")
        self.assert_allowed("cp ~/rust/hana/file /tmp/claude/unit/private-run/file")

    def test_repository_copy_with_an_equals_target_directory_is_refused(self) -> None:
        (self.home / "rust" / "hana" / ".git").mkdir()
        destination = self.session_root / "scratch-session" / "copy-parent"
        destination.mkdir(parents=True)
        self.assert_refused(f"cp -a --target-directory={shlex.quote(str(destination))} ~/rust/hana",
                            f"{destination}/hana")

    def test_rsync_previews_do_not_create_repository_copies(self) -> None:
        (self.home / "rust" / "hana" / ".git").mkdir()
        destination = self.session_root / "scratch-session" / "copy"
        for option in ("--dry-run", "-n", "-an"):
            with self.subTest(option=option):
                self.assert_allowed(f"rsync {option} -a ~/rust/hana/ {shlex.quote(str(destination))}")
        self.assert_refused(f"rsync -a ~/rust/hana/ {shlex.quote(str(destination))}", str(destination))

    def test_rsync_times_flag_does_not_consume_the_repository_source(self) -> None:
        (self.home / "rust" / "hana" / ".git").mkdir()
        destination = self.session_root / "scratch-session" / "copy"
        self.assert_refused(f"rsync -a -t ~/rust/hana/ {shlex.quote(str(destination))}", str(destination))

    def test_rsync_option_values_do_not_enable_a_preview(self) -> None:
        (self.home / "rust" / "hana" / ".git").mkdir()
        destination = self.session_root / "scratch-session" / "copy"
        for option in ("--exclude -n", "--exclude=-n", "--exclude --dry-run", "--include -n",
                       "--filter -n", "--exclude-from -n", "-f -n", "-afn", "-e -n", "-aen"):
            with self.subTest(option=option):
                self.assert_refused(f"rsync -a {option} ~/rust/hana/ {shlex.quote(str(destination))}", str(destination))

    def test_rsync_preview_flags_remain_flags_after_option_values(self) -> None:
        (self.home / "rust" / "hana" / ".git").mkdir()
        destination = self.session_root / "scratch-session" / "copy"
        for option in ("--exclude -n --dry-run", "--exclude=-n -an", "-f -n -n", "-afn --dry-run"):
            with self.subTest(option=option):
                self.assert_allowed(f"rsync -a {option} ~/rust/hana/ {shlex.quote(str(destination))}")

    def test_words_that_are_data_are_never_judged(self) -> None:
        self.assert_allowed(f"cat <<'EOF' > notes.md\nmkdir /tmp/{self.name}\nEOF\necho done")
        self.assert_allowed(f"git commit -m 'mkdir /tmp/{self.name}'")
        self.assert_allowed(f"# mkdir /tmp/{self.name}\necho done")
        self.assert_refused(f"cat <<-EOF\n\tbody\n\tEOF\nmkdir /tmp/{self.name}", f"{os.path.realpath('/tmp')}/{self.name}")

    def test_codex_argv_and_other_tools(self) -> None:
        self.assert_refused(["bash", "-lc", f"mkdir /tmp/{self.name}"], f"{os.path.realpath('/tmp')}/{self.name}")
        self.assert_allowed(f"mkdir /tmp/{self.name}", tool="apply_patch")

    def test_tmpdir_inside_a_session_scratchpad_is_no_root(self) -> None:
        sandbox = self.temp / "claude-1000"
        sandbox.mkdir()
        self.assertEqual(self.run_hook("mktemp -d", temp=sandbox), "")


if __name__ == "__main__":
    _ = unittest.main()
