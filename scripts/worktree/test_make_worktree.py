"""Test worktree creation when a unit holds a finished worktree."""

from dataclasses import replace
import errno
import json
import os
from pathlib import Path
import platform
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from scripts.agents.remote import local_machine
from scripts.lint.memory_test_support import write_config_fixture
from scripts.worktree import finished_worktrees, worktrees


MAKE_WORKTREE = Path(__file__).with_name("make_worktree.sh").resolve()


class MakeWorktreeTests(unittest.TestCase):
    def test_only_finished_worktrees_for_the_unit_prevent_creation(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="finished-worktrees-"))).resolve()
        home = root / "fixture home"
        source = home / "rust/origin"
        origin = root / "origin.git"
        workspace = root / "work trees"
        config = root / "config"
        config.mkdir()
        _ = write_config_fixture(config / "build_folders.conf", f"roots.workspace={workspace}\n")
        _ = (config / "copies.conf").write_text("# range: 0.001..\ngit_timeout_seconds=5\n")
        staff_path = root / "staff.json"
        environment = {
            "HOME": str(home), "PATH": os.environ.get("PATH", os.defpath), "CONF_DIR": str(config),
            "WORKTREES_STATE_DIR": str(root / "records"), "STAFF_FILE": str(staff_path),
            "CLAUDE_PYTHON": sys.executable, "LC_ALL": "C", "PYTHONUTF8": "1",
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_AUTHOR_NAME": "Fixture", "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
            "GIT_COMMITTER_NAME": "Fixture", "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
        }

        def git(path: Path, *args: str) -> str:
            result = subprocess.run(["git", "-C", str(path), *args], env=environment, text=True,
                                    capture_output=True, check=False, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            return result.stdout.strip()

        source.parent.mkdir(parents=True)
        _ = git(root, "init", "--bare", "--initial-branch=main", str(origin))
        _ = git(root, "clone", str(origin), str(source))
        _ = (source / "tracked.txt").write_text("base\n")
        _ = git(source, "add", "tracked.txt")
        _ = git(source, "commit", "-m", "Create the fixture")
        _ = git(source, "commit", "--allow-empty", "-m", "Advance main")
        _ = git(source, "push", "origin", "main")
        _ = git(source, "config", "status.showUntrackedFiles", "no")
        base = git(source, "rev-parse", "HEAD^")

        def add_tree(path: Path) -> None:
            _ = git(source, "worktree", "add", "--detach", str(path), base)

        home_checkout = workspace / "origin/home/held"
        runner_id, unit_id = str(uuid4()), str(uuid4())
        _ = staff_path.write_text(json.dumps({"schema": 2, "rows": [
            {"id": runner_id, "current_session": runner_id, "kind": "showrunner", "name": "fixture",
             "parent": None, "doc": {"machine": local_machine(), "path": str(source / "tracked.txt"),
                                     "state": "live"}},
            {"id": unit_id, "current_session": unit_id, "kind": "unit", "name": "home", "parent": runner_id,
             "worktree": {"machine": local_machine(), "path": str(home_checkout), "state": "present"}},
        ]}))

        with patch.dict(os.environ, environment, clear=True):
            snapshot = worktrees.staff_record()
            for unit in ("finished", "unlanded", "changed", "untracked", "occupied", "unreadable",
                         "linux-thread", "linux-foreign-process", "linux-permission-denied", "mac-omitted-pid",
                         "other-unit", "other-record", "other-unreadable-record", "home", "retry"):
                with self.subTest(unit=unit):
                    unit_folder = workspace / "origin" / unit
                    held = unit_folder / "held"
                    if unit in ("other-unit", "other-unreadable-record"):
                        held = workspace / "origin/another" / unit
                    add_tree(held)
                    recorded = root / "legacy checkout"
                    directories = finished_worktrees.ProcessDirectoriesRead((root,))
                    destination = unit_folder / "new"
                    expected: tuple[Path, ...] = ()
                    match unit:
                        case "finished":
                            add_tree(recorded)
                            record = worktrees.new_record(recorded, "legacy", source, base, "", "main")
                            layout = worktrees.RecordedWorktreeOutput(unit, "legacy", root / "output")
                            worktrees.write_record(replace(record, layout=layout))
                            expected = (held, recorded)
                        case "unlanded":
                            _ = git(held, "commit", "--allow-empty", "-m", "Keep a local commit")
                        case "changed":
                            _ = (held / "tracked.txt").write_text("change\n")
                        case "untracked":
                            _ = (held / "untracked.txt").write_text("keep\n")
                        case "occupied":
                            directories = finished_worktrees.ProcessDirectoriesRead((held / "subdirectory",))
                        case "unreadable":
                            directories = finished_worktrees.ProcessDirectoriesUnreadable()
                        case "linux-thread" | "linux-foreign-process" | "linux-permission-denied":
                            proc = root / f"{unit}-proc"
                            normal = proc / "10"
                            normal_thread = normal / "task/10"
                            normal_thread.mkdir(parents=True)
                            (normal / "cwd").symlink_to(root)
                            (normal_thread / "cwd").symlink_to(root)
                            if unit == "linux-thread":
                                process = proc / "20"
                                leader = process / "task/20"
                                leader.mkdir(parents=True)
                                for entry in (process, leader):
                                    _ = (entry / "stat").write_text("20 (leader) Z " + "0 " * 18 + "20")
                                thread = process / "task/21"
                                thread.mkdir()
                                (thread / "cwd").symlink_to(held)
                                directories = finished_worktrees.linux_process_directories(proc)
                            elif unit == "linux-foreign-process":
                                foreign = proc / "30"
                                foreign_thread = foreign / "task/30"
                                foreign_thread.mkdir(parents=True)
                                _ = (foreign_thread / "stat").write_text("30 (foreign) S " + "0 " * 18 + "30")
                                stat = Path.stat

                                def process_owner(path: Path, *, follow_symlinks: bool = True) -> os.stat_result:
                                    reading = stat(path, follow_symlinks=follow_symlinks)
                                    if path == foreign:
                                        return os.stat_result((*reading[:4], os.getuid() + 1, *reading[5:]))
                                    return reading

                                with patch.object(Path, "stat", new=process_owner):
                                    directories = finished_worktrees.linux_process_directories(proc)
                                expected = (held,)
                            else:
                                process = proc / "40"
                                thread = process / "task/40"
                                thread.mkdir(parents=True)
                                for entry in (process, thread):
                                    _ = (entry / "stat").write_text("40 (daemon) S " + "0 " * 18 + "40")
                                readlink = Path.readlink

                                def read_cwd(path: Path) -> Path:
                                    if path == thread / "cwd":
                                        raise PermissionError(errno.EACCES, os.strerror(errno.EACCES), str(path))
                                    return readlink(path)

                                with patch.object(Path, "readlink", new=read_cwd):
                                    directories = finished_worktrees.linux_process_directories(proc)
                                expected = (held,)
                        case "mac-omitted-pid":
                            process_tools = self.mac_process_environment(
                                root / "omitted-pid-tools", root, environment, omit_pid=True)
                            with patch.dict(os.environ, process_tools):
                                directories = finished_worktrees.mac_process_directories()
                            expected = (held,)
                        case "other-unreadable-record":
                            worktrees.record_file(held).mkdir(parents=True)
                        case "other-record":
                            record = worktrees.new_record(held, "legacy", source, base, "", "main")
                            layout = worktrees.RecordedWorktreeOutput("another", "legacy", root / "output")
                            worktrees.write_record(replace(record, layout=layout))
                        case "retry":
                            destination = held
                        case "home" | "other-unit":
                            pass
                    self.assertCountEqual(finished_worktrees.finished_worktrees(
                        source, destination, unit, snapshot, read_directories=lambda: directories), expected)
                    if unit == "finished":
                        registration = git(source, "worktree", "list", "--porcelain")
                        refs = git(source, "for-each-ref")
                        self.check_refusal(source, unit, environment, expected)
                        self.assertEqual(git(source, "worktree", "list", "--porcelain"), registration)
                        self.assertEqual(git(source, "for-each-ref"), refs)

    def mac_process_environment(self, tools: Path, cwd: Path, environment: dict[str, str], *,
                                omit_pid: bool = False) -> dict[str, str]:
        tools.mkdir()
        ps = 'printf "%s S\\n" "$PPID" "$$"'
        if omit_pid:
            ps += ' 999999999'
        commands = {"ps": ps, "lsof": f'printf "p%s\\nfcwd\\nn%s\\n" "$PPID" {shlex.quote(str(cwd))}'}
        for name, command in commands.items():
            entry = tools / name
            _ = entry.write_text("#!/bin/sh\n" + command + "\n")
            entry.chmod(0o755)
        return {**environment, "PATH": str(tools) + os.pathsep + environment["PATH"]}

    def check_refusal(self, source: Path, unit: str, environment: dict[str, str],
                      paths: tuple[Path, ...]) -> None:
        command = ["bash", str(MAKE_WORKTREE), "origin", unit, "new"]
        if platform.system() == "Linux":
            # Use a process table that the fixture user can read.
            bubblewrap = shutil.which("bwrap")
            if bubblewrap is None:
                self.skipTest("The Linux shell check requires bwrap.")
            command = [bubblewrap, "--unshare-pid", "--ro-bind", "/", "/", "--proc", "/proc",
                       "--dev-bind", "/dev/null", "/dev/null", "--", *command]
        elif platform.system() == "Darwin":
            environment = self.mac_process_environment(source.parent / "process-tools", source, environment)
        result = subprocess.run(command, cwd=source, env=environment, text=True,
                                capture_output=True, check=False, timeout=15)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout, "")
        heading, *lines = result.stderr.splitlines()
        path_lines = lines[:len(paths)]
        steps = lines[len(paths):len(paths) + 3]
        commands = lines[len(paths) + 3:]
        self.assertEqual(heading, f"{unit} has finished worktrees.")
        self.assertCountEqual(path_lines, [str(path) for path in paths])
        self.assertEqual(steps, ["1. Get the showrunner's yes for each exact path.",
                                 "2. Write each record in ~/.local/state/removal-lists/by-hand.log as " +
                                 "~/.claude/docs/as-built/removal-by-hand.md specifies.",
                                 "3. Run one removal command per path."])
        self.assertCountEqual(commands, [f"git -C '{source}' worktree remove '{path}'" for path in paths])
