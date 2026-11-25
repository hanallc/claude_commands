"""Check manual cleanup with private folders and an injected SSH runner."""
from __future__ import annotations

import fcntl
import errno
import io
import json
import os
import pwd
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Callable, Iterator, Sequence
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import cast, override
from unittest import mock

from scripts.disks import cleanup_old_folders
from scripts.lib import build_folders, git_files, git_rescue, process_start, removal_guard
from scripts.lib.git_test_support import commit_repository, fixture_git_environment
from scripts.lint import tmp_sweep
from scripts.production import staff

SCRIPT = Path(__file__).with_name("cleanup_old_folders.py").resolve()


class CleanupTests(unittest.TestCase):
    home: Path = Path()
    configuration: Path = Path()
    build: Path = Path()

    @override
    def setUp(self) -> None:
        base = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.home = base / "home"
        self.home.mkdir()
        self.build = base / "build"
        self.build.mkdir()
        self.configuration = base / "disks.conf"
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {"HOME": str(self.home), "CONF_DIR": str(base)})))
        _ = (base / "build_folders.conf").write_text(
            f"roots.build={self.build}\n# range: 0.001..1\nlocks.probe_retry_seconds=0.005\n")
        _ = self.enterContext(mock.patch.object(
            cleanup_old_folders, "local_machine", return_value=cleanup_old_folders.Machine.NATEDEV))
        _ = self.enterContext(mock.patch.object(cleanup_old_folders, "platform_mounts", return_value=lambda: (Path("/"),)))
        _ = self.enterContext(mock.patch.object(removal_guard, "known_names", return_value=removal_guard.KnownNames(
            frozenset(("hana",)), frozenset())))
        _ = self.enterContext(mock.patch.object(staff, "snapshot", return_value=staff.StaffSnapshot(())))
        _ = self.enterContext(mock.patch.object(cleanup_old_folders, "PROC_ROOT", str(base / "missing-proc")))
        _ = self.enterContext(mock.patch.object(tmp_sweep, "lsof_paths", return_value=tmp_sweep.ProcessUsesKnown([])))

    def write_settings(self, dead: str) -> None:
        _ = self.configuration.write_text(
            f"[cleanup]\ncleanup.dead.natedev={dead}\ncleanup.dead.mac={dead}\n"
            + f"cleanup.list_only.natedev={self.build / 'sccache'}\n"
            + "cleanup.list_only.mac=~/build/slot\ncleanup.list_only_reason.natedev=shared tool cache\n"
            + "cleanup.list_only_reason.mac=bob's hand step\n# whole: 1..\ncleanup.ssh_seconds=17\n")

    def candidate(self, name: str = "old", *, home: Path | None = None) -> Path:
        path = (self.home if home is None else home) / name
        path.mkdir(parents=True)
        _ = (path / "payload").write_text("old content\n")
        return path

    def unreachable(self, arguments: Sequence[str]) -> subprocess.CompletedProcess[str]:
        self.assertEqual(tuple(arguments), (
            "ssh", "-o", "BatchMode=yes", "mac", cleanup_old_folders.REMOTE_COMMAND + " --json",
        ))
        return subprocess.CompletedProcess(arguments, 255, "", "connection refused\nssh debug detail\n")

    def command(self, *arguments: str,
                runner: cleanup_old_folders.SshRunner | None = None) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            status = cleanup_old_folders.main(arguments, run_ssh=self.unreachable if runner is None else runner)
        return status, stdout.getvalue(), stderr.getvalue()

    def approval(self, *arguments: str) -> str:
        status, output, errors = self.command(*arguments)
        self.assertEqual(status, 0, errors)
        return output.splitlines()[-1].removeprefix("Digest: ")

    def test_script_help_runs_with_a_private_home(self) -> None:
        result = subprocess.run((sys.executable, "-B", str(SCRIPT), "--help"), cwd=self.home,
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--go DIGEST", result.stdout)

    @unittest.skipUnless(sys.platform == "linux", "requires bubblewrap")
    def test_natedev_refuses_to_list_without_bubblewrap(self) -> None:
        self.write_settings(str(self.candidate()))
        program = """
from unittest.mock import patch
from scripts.disks import cleanup_old_folders as cleanup

with patch.object(cleanup, "local_machine", return_value=cleanup.Machine.NATEDEV):
    raise SystemExit(cleanup.run_cli(("--host-list",)))
"""
        result = subprocess.run(
            (sys.executable, "-B", "-c", program), env=os.environ | {"PATH": str(self.home)},
            capture_output=True, text=True, check=False,
        )
        self.assertEqual((result.returncode, result.stdout), (1, ""), result.stderr)
        self.assertIn("requires bwrap", result.stderr)

    def test_worker_review_preserves_link_and_orphan_fields(self) -> None:
        cleanup = cleanup_old_folders
        orphan = self.build / "p-orphan"
        rows = (
            cleanup.Row(self.home / "worktrees/dangling", 4096, cleanup.WouldRemove("a" * 64),
                        place=cleanup.CleanupPlace.LINK),
            cleanup.Row(orphan, None, removal_guard.Kept("review only"), "no Git checkout",
                        cleanup.worktree_sidecar_names(orphan), cleanup.CleanupPlace.ORPHAN_SIDECARS),
        )
        review = cleanup.HostList(cleanup.Machine.NATEDEV, rows, ((42, "fixture"),))
        channel = io.StringIO()
        cleanup.WorkerChannel(io.StringIO(), channel).send(cleanup.worker_review(review))
        _ = channel.seek(0)
        fields = cleanup.WorkerChannel(channel, io.StringIO()).receive()
        received = cleanup.HostList(
            cleanup.Machine.NATEDEV, cleanup.decode_cleanup_rows(fields["rows"], cleanup.Machine.NATEDEV),
            cleanup.unreadable_records(fields["unreadable"]),
        )
        self.assertEqual(received, review)

    @unittest.skipUnless(sys.platform == "linux", "requires the Linux cleanup parent")
    def test_failed_worker_after_sending_rows_lists_nothing(self) -> None:
        program = 'print(\'{"kind":"review","rows":[],"unreadable":[]}\', flush=True); raise SystemExit(2)'
        with (mock.patch.object(cleanup_old_folders, "protected_mounts", return_value=()),
              mock.patch.object(cleanup_old_folders, "masked_cleanup_command",
                                return_value=(sys.executable, "-c", program)),
              redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors):
            status = cleanup_old_folders.run_cli(("--host-list",))
        self.assertEqual((status, output.getvalue()), (1, ""))
        self.assertIn("the masked cleanup worker failed", errors.getvalue())

    @unittest.skipUnless(sys.platform == "linux", "requires bubblewrap")
    def test_invalid_mask_entries_stop_before_bubblewrap(self) -> None:
        destination = self.candidate("sccache", home=self.build)
        hanadocs = self.home / "rust/hanadocs"
        hanadocs.parent.mkdir()
        for entry, target in ((self.build / "cargo", None), (self.build / "old-link", destination),
                              (hanadocs, self.home / "missing")):
            if target is None:
                entry.touch()
            else:
                entry.symlink_to(target, target_is_directory=True)
            with (self.subTest(entry=entry), mock.patch.object(cleanup_old_folders, "LINUX_BUILD_ROOT", self.build),
                  mock.patch.object(cleanup_old_folders, "masked_cleanup_command") as mask,
                  redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors):
                mask.side_effect = ValueError("started bubblewrap")
                self.assertEqual(cleanup_old_folders.run_cli(("--host-list",)), 1)
                mask.assert_not_called()
                self.assertEqual(output.getvalue(), "")
                self.assertIn(str(entry), errors.getvalue())
            entry.unlink()

    @unittest.skipUnless(sys.platform == "linux", "requires bubblewrap")
    def test_new_protected_entry_discards_the_list(self) -> None:
        self.write_settings(str(self.candidate()))
        staff_file = self.home / "staff.json"
        _ = staff_file.write_text('{"schema":2,"rows":[]}')
        masked_command = cleanup_old_folders.masked_cleanup_command
        git_result = self.home / "late-git-result"
        for during_review in (False, True):
            with self.subTest(during_review=during_review):
                late = self.build / ("old-late" if during_review else "cargo")

                def add_unmasked_entry(mounts: Sequence[Path], command: Sequence[str]) -> tuple[str, ...]:
                    if during_review:
                        setup = f"""
from pathlib import Path
import sys
sys.path.insert(0, sys.argv[1])
from scripts.disks import cleanup_old_folders as cleanup
review_local = cleanup.review_local
def review_and_add(machine):
    result = review_local(machine)
    Path({str(late)!r}).mkdir()
    cleanup.cleanup_git(Path({str(self.home)!r}), "init", "-q", {str(git_result)!r})
    return result
cleanup.review_local = review_and_add
"""
                        command = (*command[:3], setup + command[3], *command[4:])
                    else:
                        late.mkdir()
                    return masked_command(mounts, command)

                with (mock.patch.object(cleanup_old_folders, "LINUX_BUILD_ROOT", self.build),
                      mock.patch.object(cleanup_old_folders, "masked_cleanup_command", side_effect=add_unmasked_entry),
                      mock.patch.dict(os.environ, {"STAFF_FILE": str(staff_file)}),
                      redirect_stdout(io.StringIO()) as output):
                    status = cleanup_old_folders.run_cli(("--host-list",))
                self.assertEqual((status, output.getvalue()), (1, ""))
                self.assertTrue(late.is_dir())
                self.assertFalse(git_result.exists())
                late.rmdir()

    def test_new_protected_name_stops_apply_before_removal(self) -> None:
        path = self.candidate()
        self.write_settings(str(path))
        cleanup = cleanup_old_folders
        apply = cleanup.apply
        late = self.build / "old-late"

        def apply_with_new_name(review: cleanup_old_folders.HostList,
                                bounds: cleanup_old_folders.CleanupBounds) -> int:
            late.mkdir()
            return apply(review, bounds)

        channel = cleanup.WorkerChannel(io.StringIO('{"kind":"apply"}\n'), io.StringIO())
        with (mock.patch.object(cleanup, "process_uses", return_value=cleanup.CleanupProcessReview()),
              mock.patch.object(cleanup, "apply", side_effect=apply_with_new_name),
              self.assertRaisesRegex(Exception, "protected path has no mask")):
            _ = cleanup.serve_masked_cleanup(channel, self.build, frozenset())
        self.assertTrue((path / "payload").is_file())

    @unittest.skipUnless(sys.platform == "linux", "requires the Linux cleanup parent")
    def test_failed_worker_reports_removals_completed_before_it_stops(self) -> None:
        first = self.candidate("first")
        second = self.candidate("second")
        self.write_settings(f"{first}, {second}")
        approval = self.approval("--host-list")
        program = f"""
from pathlib import Path
from unittest.mock import patch
from scripts.disks import cleanup_old_folders as cleanup
from scripts.lib import removal_guard
from scripts.production import staff

remove = cleanup.remove_configured_folder
def remove_then_add_protected_name(*args):
    remove(*args)
    Path({str(self.build / "old-late")!r}).mkdir()

with (patch.object(staff, "snapshot", return_value=staff.StaffSnapshot(())),
      patch.object(removal_guard, "known_names", return_value=removal_guard.KnownNames(frozenset(("hana",)), frozenset())),
      patch.object(cleanup, "process_uses", return_value=cleanup.CleanupProcessReview()),
      patch.object(cleanup, "remove_configured_folder", side_effect=remove_then_add_protected_name)):
    raise SystemExit(cleanup.masked_child(Path({str(self.build)!r}), frozenset()))
"""
        with (mock.patch.object(cleanup_old_folders, "LINUX_BUILD_ROOT", self.build),
              mock.patch.object(cleanup_old_folders, "masked_cleanup_command",
                                return_value=(sys.executable, "-B", "-c", program)),
              redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors):
            status = cleanup_old_folders.run_cli(("--host-list", "--go", approval))
        self.assertEqual(status, 1, errors.getvalue())
        self.assertFalse(first.exists(), errors.getvalue())
        self.assertTrue((second / "payload").is_file())
        self.assertEqual(output.getvalue(), f"removed {str(first)!r}\n")

    @unittest.skipUnless(sys.platform == "linux", "requires bubblewrap")
    def test_protected_mounts_hide_git_index_worktree_files_and_staff_paths(self) -> None:
        source, clone = self.clone_fixture()
        cargo = self.build / "cargo"
        cargo.mkdir()
        index = clone / ".git/index"
        _ = index.rename(cargo / "index")
        index.symlink_to(cargo / "index")
        self.assertEqual(self.git(clone, "status", "--porcelain"), "")
        old_cache = self.build / "old-cache"
        old_cache.mkdir()
        _ = (old_cache / "hidden").write_text("protected content")
        _ = self.git(source, "config", "core.worktree", str(self.build))
        status = self.git(source, "status", "--porcelain", "--untracked-files=all")
        self.assertIn("?? cargo/index", status)
        self.assertIn("?? old-cache/hidden", status)
        staff_alias = self.home / "rust/hanadocs/staff-path"
        staff_alias.parent.mkdir(parents=True)
        staff_alias.symlink_to(clone, target_is_directory=True)
        showrunner = staff.StaffId("11111111-1111-4111-8111-111111111111")
        unit = staff.StaffId("22222222-2222-4222-8222-222222222222")
        rows = (
            staff.StaffRow(showrunner, "showrunner", "fixture", staff.StaffRoot(), current_session=showrunner),
            staff.StaffRow(unit, "unit", "fixture-unit", staff.ReportsTo(showrunner), current_session=unit,
                           unit_type=staff.UnitDetails("direct"), worktree=staff.PresentWorktree("natedev", staff_alias)),
        )
        staff_file = self.home / "staff.json"
        _ = staff_file.write_text(json.dumps({"schema": 2, "rows": [row.to_json() for row in rows]}))
        with mock.patch.dict(os.environ, {"STAFF_FILE": str(staff_file)}):
            self.assertIsNotNone(staff.local_snapshot().by_worktree("natedev", clone))
        program = """
import os
from pathlib import Path
import subprocess
import sys
from scripts.production import staff

source, clone, *masks = map(Path, sys.argv[1:])

def status(repo):
    return subprocess.check_output(
        ("git", "-C", str(repo), "status", "--porcelain", "--untracked-files=all"), text=True,
    )

assert "?? work" in status(clone)
assert "cargo/index" not in status(source)
assert "old-cache/hidden" not in status(source)
assert staff.local_snapshot().by_worktree("natedev", clone) is None
for folder in masks:
    assert not list(folder.iterdir()), folder
    assert os.statvfs(folder).f_flag & os.ST_RDONLY, folder
"""
        masked = cleanup_old_folders.protected_mounts(self.home, self.build)
        command = cleanup_old_folders.masked_cleanup_command(
            masked, (sys.executable, "-B", "-c", program, str(source), str(clone),
                     str(cargo), str(old_cache), str(staff_alias.parent)),
        )
        result = subprocess.run(command, env=os.environ | {"STAFF_FILE": str(staff_file)},
                                text=True, capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((cargo / "index").is_file())
        self.assertEqual((old_cache / "hidden").read_text(), "protected content")
        self.assertEqual(staff_alias.resolve(), clone)

    def test_default_lists_without_removing_and_shows_unreachable_mac(self) -> None:
        path = self.candidate()
        self.write_settings("~/old")
        with mock.patch.object(self, "unreachable", wraps=self.unreachable) as ssh:
            status, output, errors = self.command()
        self.assertEqual((status, errors), (0, ""))
        self.assertRegex(output, rf"natedev\t{str(path)!r}\t[0-9]+\twould remove")
        self.assertIn("mac\t-\tunknown\tmac unreachable: connection refused\n", output)
        self.assertNotIn("ssh debug detail", output)
        self.assertIn("mac total: unknown", output)
        self.assertRegex(output.splitlines()[-1], r"^Digest: [0-9a-f]{64}$")
        self.assertEqual((path / "payload").read_text(), "old content\n")
        ssh.assert_called_once()

    def test_default_ssh_uses_the_cleanup_time_limit(self) -> None:
        self.write_settings("~/missing")
        for stderr, detail in ((None, "SSH timed out after 17 seconds"),
                               (b"connection closed\nssh debug detail\n", "connection closed")):
            error = subprocess.TimeoutExpired("ssh", 17, stderr=stderr)
            with self.subTest(stderr=stderr), mock.patch.object(subprocess, "run", side_effect=error) as ssh:
                status, output, errors = self.command(runner=cleanup_old_folders.run_remote)
                self.assertEqual((status, errors), (0, ""))
                self.assertIn(f"mac\t-\tunknown\tmac unreachable: {detail}\n", output)
                self.assertNotIn("ssh debug detail", output)
                ssh.assert_called_once_with(
                    ("ssh", "-o", "BatchMode=yes", "mac", cleanup_old_folders.REMOTE_COMMAND + " --json"),
                    capture_output=True, text=True, timeout=17, check=False)

    def test_a_changed_digest_refuses_even_when_the_size_is_the_same(self) -> None:
        path = self.candidate()
        self.write_settings("~/old")
        approval = self.approval("--host-list")
        moved = path.with_name("saved")
        _ = path.rename(moved)
        replacement = self.candidate()
        status, output, errors = self.command("--host-list", "--go", approval)
        self.assertEqual((status, output), (1, ""))
        self.assertIn("list digest changed", errors)
        self.assertEqual((replacement / "payload").read_text(), "old content\n")
        self.assertEqual((moved / "payload").read_text(), "old content\n")

    def test_open_files_and_an_incomplete_scan_keep_folders(self) -> None:
        path = self.candidate()
        self.write_settings("~/old")
        for uses, reason in (
            (tmp_sweep.ProcessUsesKnown([(str(path / "payload"), 42, "holds a file")]), "pid 42 holds a file"),
            (tmp_sweep.ProcessUsesIncomplete(), "open file scan is incomplete"),
        ):
            with (self.subTest(reason=reason),
                  mock.patch.object(tmp_sweep, "lsof_paths", return_value=uses) as scan):
                status, output, errors = self.command("--host-list")
            self.assertEqual((status, errors), (0, ""))
            self.assertIn(reason, output)
            self.assertNotIn("would remove", output)
            self.assertTrue((path / "payload").is_file())
            scan.assert_called_once_with((str(path),), require_complete=True, same_user_only=True)

    def proc_fixture(self) -> tuple[Path, Path]:
        proc = self.home / "proc"
        pid = proc / "42"
        pid.mkdir(parents=True)
        (proc / "self").mkdir()
        _ = (proc / "self/status").write_text("CapPrm: 0000000000000000\n")
        uid = os.geteuid()
        _ = (pid / "status").write_text(f"Uid: {uid} {uid} {uid} {uid}\nState: S\nThreads: 1\n")
        (pid / "fd").mkdir()
        (pid / "environ").touch()
        return proc, pid

    def test_list_names_the_user_and_skips_other_users_processes(self) -> None:
        path = self.candidate()
        self.write_settings("~/old")
        proc, pid = self.proc_fixture()
        other_uid = os.geteuid() + 1
        _ = (pid / "status").write_text(f"Uid: {other_uid} {other_uid} {other_uid} {other_uid}\nState: S\n")
        (pid / "cwd").symlink_to(path, target_is_directory=True)
        user = pwd.getpwuid(os.geteuid()).pw_name
        with mock.patch.object(cleanup_old_folders, "PROC_ROOT", str(proc)):
            status, output, errors = self.command("--host-list")
        self.assertEqual((status, errors), (0, ""))
        header = f"Use check: processes of {user} only; other users' processes are not checked."
        self.assertEqual(output.splitlines().count(header), 1)
        self.assertIn("would remove", output)

    def test_proc_io_errors_keep_every_candidate(self) -> None:
        first = self.candidate()
        second = self.candidate("other")
        self.write_settings("~/old, ~/other")
        proc, pid = self.proc_fixture()
        _ = (pid / "comm").write_text("fixture-process\n")
        cwd = pid / "cwd"
        cwd.symlink_to(self.home, target_is_directory=True)
        readlink = os.readlink
        scandir = os.scandir

        def read_cwd(path: bytes | str | Path) -> bytes | str:
            name = os.fsdecode(path)
            if name == str(cwd):
                raise OSError(errno.EIO, "read failed", path)
            return readlink(path) if isinstance(path, bytes) else readlink(name)

        def read_fds(path: str | int) -> Iterator[os.DirEntry[str]]:
            if path == str(pid / "fd"):
                raise OSError(errno.EIO, "read failed", path)
            return scandir(path)

        self.assertIn("would remove", self.command("--host-list")[1])
        with mock.patch.object(cleanup_old_folders, "PROC_ROOT", str(proc)):
            for call, reader in (("readlink", read_cwd), ("scandir", read_fds)):
                with self.subTest(call=call), mock.patch.object(os, call, side_effect=reader):
                    status, output, errors = self.command("--host-list")
                self.assertEqual((status, errors), (0, ""))
                for path in (first, second):
                    self.assertRegex(output, rf"{str(path)!r}\t[0-9]+\topen file scan is incomplete")
                    self.assertIn("pid 42 fixture-process", output)
                    self.assertTrue((path / "payload").is_file())
                self.assertNotIn("would remove", output)

    def test_kernel_denials_keep_folders_and_name_each_process(self) -> None:
        free = self.candidate()
        cwd = self.candidate("cwd")
        open_file = self.candidate("open-file")
        self.write_settings("~/old, ~/cwd, ~/open-file")
        proc, pid = self.proc_fixture()
        status_text = (pid / "status").read_text()
        for number, command in ((43, "hidden-fd"), (44, "reader")):
            folder = proc / str(number)
            (folder / "fd").mkdir(parents=True)
            (folder / "environ").touch()
            _ = (folder / "status").write_text(status_text)
            _ = (folder / "comm").write_text(command + "\n")
        _ = (pid / "comm").write_text("hidden-cwd\n")
        (pid / "cwd").symlink_to(self.home)
        (proc / "43/fd/3").symlink_to(self.home / "other-file")
        (proc / "44/cwd").symlink_to(cwd)
        (proc / "44/fd/3").symlink_to(open_file / "payload")
        readlink = os.readlink

        def read_path(path: bytes | str | Path) -> bytes | str:
            name = os.fsdecode(path)
            if name == str(pid / "cwd"):
                raise PermissionError(errno.EACCES, "denied", path)
            if name == str(proc / "43/fd/3"):
                raise PermissionError(errno.EPERM, "denied", path)
            return readlink(path) if isinstance(path, bytes) else readlink(name)

        with (mock.patch.object(cleanup_old_folders, "PROC_ROOT", str(proc)),
              mock.patch.object(os, "readlink", side_effect=read_path)):
            status, output, errors = self.command("--host-list")
        self.assertEqual((status, errors), (0, ""))
        names = "pid 42 hidden-cwd, pid 43 hidden-fd"
        self.assertEqual(output.splitlines().count(f"Open file scan is incomplete: {names}"), 1)
        for path in (free, cwd, open_file):
            self.assertRegex(output, rf"{str(path)!r}\t[0-9]+\topen file scan is incomplete: {names}")
        self.assertNotIn("would remove", output)

        with mock.patch.object(cleanup_old_folders, "PROC_ROOT", str(proc)):
            status, output, errors = self.command("--host-list")
        self.assertEqual((status, errors), (0, ""))
        self.assertRegex(output, rf"{str(free)!r}\t[0-9]+\twould remove")
        self.assertRegex(output, rf"{str(cwd)!r}\t[0-9]+\tpid 44 works in it")
        self.assertRegex(output, rf"{str(open_file)!r}\t[0-9]+\tpid 44 holds a file open in it")

    def test_pwd_denials_keep_folders_and_links_and_name_each_process_once(self) -> None:
        free = self.candidate()
        held = self.candidate("held")
        root = self.home / "worktrees"
        root.mkdir()
        unused_link = root / "unused"
        used_link = root / "used"
        for link in (unused_link, used_link):
            link.symlink_to(self.home)
        self.write_settings("~/old, ~/held, ~/worktrees/unused, ~/worktrees/used")
        proc, denied = self.proc_fixture()
        _ = (denied / "comm").write_text("hidden-pwd\n")
        for number, working_directory in ((43, used_link), (44, held)):
            folder = proc / str(number)
            (folder / "fd").mkdir(parents=True)
            _ = (folder / "status").write_text((denied / "status").read_text())
            _ = (folder / "environ").write_bytes(f"PWD={working_directory}/child\0".encode())
        environment_only = proc / "45"
        (environment_only / "fd").mkdir(parents=True)
        (environment_only / "environ").touch()
        _ = (environment_only / "status").write_text((denied / "status").read_text())
        _ = (environment_only / "comm").write_text("hidden-environment\n")
        (environment_only / "cwd").symlink_to(self.home)
        read_bytes = Path.read_bytes
        readlink = os.readlink

        def read_environment(path: Path) -> bytes:
            if path in (denied / "environ", environment_only / "environ"):
                raise PermissionError(errno.EPERM, "denied", path)
            return read_bytes(path)

        def read_cwd(path: bytes | str | Path) -> bytes | str:
            name = os.fsdecode(path)
            if name == str(denied / "cwd"):
                raise PermissionError(errno.EACCES, "denied", path)
            return readlink(path) if isinstance(path, bytes) else readlink(name)

        with (mock.patch.object(cleanup_old_folders, "PROC_ROOT", str(proc)),
              mock.patch.object(Path, "read_bytes", read_environment),
              mock.patch.object(os, "readlink", side_effect=read_cwd)):
            status, output, errors = self.command("--host-list")
        self.assertEqual((status, errors), (0, ""))
        names = "pid 42 hidden-pwd, pid 45 hidden-environment"
        self.assertEqual(output.splitlines().count(f"Open file scan is incomplete: {names}"), 1)
        for path in (free, held, unused_link, used_link):
            self.assertRegex(output, rf"{str(path)!r}\t[0-9]+\topen file scan is incomplete: {names}")
        self.assertNotIn("would remove", output)

        with mock.patch.object(cleanup_old_folders, "PROC_ROOT", str(proc)):
            status, output, errors = self.command("--host-list")
        self.assertEqual((status, errors), (0, ""))
        for path in (free, unused_link):
            self.assertRegex(output, rf"{str(path)!r}\t[0-9]+\twould remove")
        self.assertRegex(output, rf"{str(used_link)!r}\t[0-9]+\tpid 43 .* uses the link in PWD")
        self.assertRegex(output, rf"{str(held)!r}\t[0-9]+\tpid 44 uses it in PWD")

    def test_linux_preview_and_apply_use_the_proc_table(self) -> None:
        path = self.candidate()
        self.write_settings("~/old")
        proc, pid = self.proc_fixture()
        cwd = pid / "cwd"
        cwd.symlink_to(path, target_is_directory=True)

        with (mock.patch.object(cleanup_old_folders, "PROC_ROOT", str(proc)),
              mock.patch.object(tmp_sweep, "lsof_paths", return_value=tmp_sweep.ProcessUsesIncomplete()) as lsof):
            status, output, errors = self.command("--host-list")
            self.assertEqual((status, errors), (0, ""))
            self.assertIn("pid 42 works in", output)
            cwd.unlink()
            preview = self.approval("--host-list")
            status, output, errors = self.command("--host-list", "--go", preview)
            self.assertEqual((status, errors), (0, ""))
            self.assertIn(f"removed {str(path)!r}", output)
            lsof.assert_not_called()
        self.assertFalse(path.exists())

    def test_missing_environ_keeps_the_row_until_the_process_exits(self) -> None:
        path = self.candidate()
        self.write_settings(str(path))
        proc, pid = self.proc_fixture()
        _ = (pid / "comm").write_text("missing-environ\n")
        (pid / "environ").unlink()
        with mock.patch.object(cleanup_old_folders, "PROC_ROOT", str(proc)):
            status, output, errors = self.command("--host-list")
            self.assertEqual((status, errors), (0, ""))
            self.assertIn("open file scan is incomplete: pid 42 missing-environ", output)
            self.assertNotIn("would remove", output)
            _ = pid.rename(proc / "exited")
            status, output, errors = self.command("--host-list")
        self.assertEqual((status, errors), (0, ""))
        self.assertRegex(output, rf"{str(path)!r}\t[0-9]+\twould remove")

    def test_matching_live_lease_keeps_and_dead_or_reused_pid_lists(self) -> None:
        path = self.candidate(home=self.build)
        self.write_settings(str(path))
        _ = build_folders.lease_path(path).write_text(json.dumps({"pid": 42, "process_start": "123"}))
        for current, reason in (
            (process_start.Started("123"), "lease PID 42 is still running"),
            (process_start.Gone(), "would remove"),
            (process_start.Started("124"), "would remove"),
            (process_start.Unreadable("denied"), "cannot verify lease PID 42"),
        ):
            with (self.subTest(current=type(current).__name__),
                  mock.patch.object(process_start, "process_start", return_value=current)):
                status, output, errors = self.command("--host-list")
            self.assertEqual((status, errors), (0, ""))
            self.assertIn(reason, output)
            self.assertTrue(path.exists())

    def test_child_lease_keeps_parent_in_preview_and_apply(self) -> None:
        payload = self.candidate("old/nested/copy") / "payload"
        self.write_settings("~/old")
        lease = build_folders.lease_path(payload.parent)
        _ = lease.write_text(json.dumps({"pid": 42, "process_start": "123"}))
        with mock.patch.object(process_start, "process_start", return_value=process_start.Started("123")):
            status, output, errors = self.command("--host-list")
        self.assertEqual((status, errors), (0, ""))
        self.assertIn("lease PID 42 is still running", output)
        self.assertNotIn("would remove", output)

        with mock.patch.object(process_start, "process_start", side_effect=(
            process_start.Gone(), process_start.Gone(), process_start.Started("123"),
        )):
            approval = self.approval("--host-list")
            status, output, errors = self.command("--host-list", "--go", approval)
        self.assertEqual((status, errors), (1, ""))
        self.assertIn("lease PID 42 is still running", output)
        self.assertEqual(payload.read_text(), "old content\n")

    def test_apply_rechecks_use_with_the_lease_held(self) -> None:
        path = self.candidate()
        self.write_settings("~/old")
        lock = path.with_name(path.name + ".lock")
        lock.touch()
        approval = self.approval("--host-list")

        def opened(prefix: tuple[str, ...], *, require_complete: bool, same_user_only: bool,
                   ignored_self_fds: frozenset[int] = tmp_sweep.NO_IGNORED_FDS) -> tmp_sweep.ProcessUsesKnown:
            self.assertEqual(prefix, (str(path),))
            self.assertTrue(require_complete)
            self.assertTrue(same_user_only)
            if ignored_self_fds:
                with lock.open("r+") as contender, self.assertRaises(BlockingIOError):
                    fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
                if changed:
                    _ = (path / "new").write_text("keep the new file")
                    return tmp_sweep.ProcessUsesKnown([])
                return tmp_sweep.ProcessUsesKnown([(str(path / "payload"), 42, "holds a file")])
            return tmp_sweep.ProcessUsesKnown([])

        for changed, reason in (
            (False, "pid 42 holds a file"),
            (True, "identity or contents changed since the list was checked"),
        ):
            with self.subTest(changed=changed), mock.patch.object(tmp_sweep, "lsof_paths", side_effect=opened):
                status, output, errors = self.command("--host-list", "--go", approval)
            self.assertEqual((status, errors), (1, ""))
            self.assertIn(reason, output)
            self.assertTrue((path / "payload").is_file())
        self.assertEqual((path / "new").read_text(), "keep the new file")
        with lock.open("r+") as holder:
            fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertIn("a folder lock is held", self.command("--host-list")[1])

    def test_list_only_takes_priority_over_dead_config(self) -> None:
        cases = (
            (cleanup_old_folders.Machine.MAC, self.home / "build/slot", "bob's hand step"),
            (cleanup_old_folders.Machine.NATEDEV, self.build / "sccache", "shared tool cache"),
        )
        for machine, path, reason in cases:
            with (self.subTest(machine=machine),
                  mock.patch.object(cleanup_old_folders, "local_machine", return_value=machine)):
                _ = self.candidate(path.name, home=path.parent)
                self.write_settings(str(path))
                status, output, errors = self.command()
                self.assertEqual((status, errors), (0, ""))
                self.assertIn(f"{str(path)!r}", output)
                self.assertIn(f"\tkept: {reason}, list only\n", output)
                self.assertNotIn("would remove", output)
                self.assertEqual(self.command("--go", output.splitlines()[-1].removeprefix("Digest: "))[0], 0)
                self.assertTrue((path / "payload").is_file())
                with mock.patch.object(cleanup_old_folders, "platform_mounts", return_value=lambda: (Path("/"), path)):
                    status, output, errors = self.command("--host-list")
                    self.assertEqual((status, errors), (0, ""))
                    self.assertIn(f"{str(path)!r}\tunknown\tkept: {reason}, list only; ", output)
                    self.assertIn(removal_guard.RefusalReason.MOUNT.value, output)

    def test_list_only_cache_preserves_the_scan_error(self) -> None:
        cache = self.candidate("sccache", home=self.build)
        self.write_settings(str(cache))
        failure = removal_guard.ChangedEntry("cache contents changed during the scan")
        with mock.patch.object(cleanup_old_folders, "inspect_cleanup_tree", side_effect=failure):
            status, output, errors = self.command("--host-list")
        self.assertEqual((status, errors), (0, ""))
        self.assertIn(f"{str(cache)!r}\tunknown\tkept: shared tool cache, list only; {failure}\n", output)

    def test_only_known_repositories_have_protected_model_folders(self) -> None:
        fixture = self.build / "p-fixture"
        _ = self.candidate("cache/claude", home=fixture)
        hana = self.build / "hana"
        _ = self.candidate("main", home=hana)
        self.write_settings(f"{fixture}, {hana}")
        status, output, errors = self.command("--host-list")
        self.assertEqual((status, errors), (0, ""))
        self.assertRegex(output, rf"{str(fixture)!r}\t[0-9]+\twould remove")
        self.assertIn(f"{str(hana)!r}\tunknown\tcontains a model folder: {hana / 'main'}", output)

    def git(self, path: Path, *arguments: str) -> str:
        return subprocess.run(("git", "-C", str(path), *arguments), check=True,
                              capture_output=True, text=True).stdout.strip()

    def git_fixture(self, *, linked: bool = True) -> tuple[Path, Path]:
        environment = fixture_git_environment(self.home)
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, environment, clear=True)))
        source = self.home / "source"
        source.mkdir()
        _ = self.git(source, "init", "-q", "-b", "main")
        _ = (source / "work").write_text("original\n")
        base = commit_repository(source, "original", env=environment)
        _ = self.git(source, "update-ref", "refs/remotes/origin/main", base)
        path = source
        if linked:
            path = self.home / "old"
            _ = self.git(source, "worktree", "add", "-q", "--detach", str(path))
        self.write_settings(str(path))
        return source, path

    def clone_fixture(self) -> tuple[Path, Path]:
        source, _ = self.git_fixture(linked=False)
        path = self.home / "old"
        _ = self.git(self.home, "clone", "-q", "--shared", str(source), str(path))
        self.write_settings(str(path))
        return source, path

    def test_dirty_clone_and_its_local_branches_are_saved_to_origin(self) -> None:
        source, path = self.clone_fixture()
        _ = self.git(path, "switch", "-qc", "local/topic")
        _ = (path / "work").write_text("new commit\n")
        branch = commit_repository(path, "local topic")
        _ = self.git(path, "switch", "-q", "main")
        _ = (path / "work").write_text("dirty work\n")
        _ = (path / "untracked").write_text("new work\n")
        status, output, errors = self.command("--host-list")
        self.assertEqual((status, errors), (0, ""))
        self.assertIn(f"clone of {str(source)!r}, dirty: yes, unpushed: 1, stash: no", output)
        self.assertIn(f"saved to {str(source)!r} refs/rescue on --go", output)
        self.assertEqual(self.git(source, "for-each-ref", "refs/rescue"), "")
        approval = output.splitlines()[-1].removeprefix("Digest: ")
        status, output, errors = self.command("--host-list", "--go", approval)
        self.assertEqual((status, errors), (0, ""), output)
        self.assertFalse(path.exists())
        branch_ref = "refs/rescue/old/local/topic"
        self.assertEqual(self.git(source, "rev-parse", branch_ref), branch)
        refs = self.git(source, "for-each-ref", "--format=%(refname)", "refs/rescue/old").splitlines()
        refs.remove(branch_ref)
        self.assertEqual(len(refs), 1)
        saved, = refs
        self.assertEqual(self.git(source, "show", f"{saved}:work"), "dirty work")
        self.assertEqual(self.git(source, "show", f"{saved}:untracked"), "new work")
        self.assertEqual(self.git(source, "for-each-ref", "--format=%(refname)", "refs/heads"), "refs/heads/main")

    def test_clone_ref_conflict_keeps_the_clone_and_the_existing_ref(self) -> None:
        source, path = self.clone_fixture()
        base = self.git(source, "rev-parse", "HEAD")
        ref = "refs/rescue/old/main"
        _ = self.git(source, "update-ref", ref, base)
        _ = (path / "work").write_text("keep this commit\n")
        commit = commit_repository(path, "local work")
        approval = self.approval("--host-list")
        status, output, errors = self.command("--host-list", "--go", approval)
        self.assertEqual((status, errors), (1, ""))
        self.assertIn(f"the source already has a different rescue ref: {ref}", output)
        self.assertEqual(self.git(source, "rev-parse", ref), base)
        self.assertEqual(self.git(path, "rev-parse", "HEAD"), commit)
        self.assertEqual((path / "work").read_text(), "keep this commit\n")

    def test_git_config_includes_keep_the_clone_without_running_git(self) -> None:
        _, path = self.clone_fixture()
        protected = self.home / "rust/hanadocs/private.conf"
        protected.parent.mkdir(parents=True)
        _ = protected.write_text("[user]\nname = protected\n")
        config = path / ".git/config"
        base = config.read_text()
        for section in ("[Include]", '[includeIf "gitdir:**"]', "[includeIf.gitdir]"):
            with self.subTest(section=section):
                _ = config.write_text(base + f"\n{section}\nPath = {protected}\n")
                with mock.patch.object(subprocess, "run", wraps=subprocess.run) as git:
                    status, output, errors = self.command("--host-list")
                self.assertEqual((status, errors), (0, ""))
                self.assertIn(f"{str(path)!r}\t", output)
                self.assertIn("\tgit config include\n", output)
                self.assertNotIn("would remove", output)
                git.assert_not_called()

    def test_git_metadata_links_keep_the_clone_without_running_git(self) -> None:
        _, path = self.clone_fixture()
        git_dir = path / ".git"
        for number, name in enumerate(("index", "HEAD", "config", "objects", "refs", "refs/heads/main")):
            with self.subTest(name=name):
                entry = git_dir / name
                saved = entry.rename(self.build / str(number))
                entry.symlink_to(saved)
                try:
                    self.assert_git_metadata_is_review_only(path)
                finally:
                    entry.unlink()
                    _ = saved.rename(entry)

    def assert_git_metadata_is_review_only(self, path: Path) -> None:
        with mock.patch.object(subprocess, "run", wraps=subprocess.run) as run:
            status, output, errors = self.command("--host-list")
        self.assertEqual((status, errors), (0, ""))
        self.assertIn(f"{str(path)!r}\t", output)
        self.assertIn("review only: Git metadata", output)
        self.assertNotIn("would remove", output)
        run.assert_not_called()

    def test_git_registration_pointers_must_remain_in_the_named_checkout(self) -> None:
        source, path = self.git_fixture()
        registration = git_files.at(path).git_dir
        for name in ("gitdir", "commondir"):
            with self.subTest(name=name):
                entry = registration / name
                saved = entry.rename(self.build / name)
                entry.symlink_to(saved)
                try:
                    self.assert_git_metadata_is_review_only(path)
                finally:
                    entry.unlink()
                    _ = saved.rename(entry)
        backlink = registration / "gitdir"
        original = backlink.read_text()
        _ = backlink.write_text(f"{source / '.git'}\n")
        self.assert_git_metadata_is_review_only(path)
        _ = backlink.write_text(original)
        _ = (registration / "commondir").write_text(f"{source}\n")
        self.assert_git_metadata_is_review_only(path)
        _ = (path / ".git").write_text(f"gitdir: {source / '.git'}\n")
        self.assert_git_metadata_is_review_only(path)

    def test_inherited_git_includes_are_disabled_for_review_and_save(self) -> None:
        source, path = self.clone_fixture()
        protected = self.home / "rust/hanadocs/private.conf"
        protected.parent.mkdir(parents=True)
        _ = protected.write_text("invalid Git config\n")
        inherited = self.home / "inherited.gitconfig"
        _ = inherited.write_text(f"[include]\npath = {protected}\n")
        _ = (path / "work").write_text("keep this work\n")
        overrides = {"GIT_CONFIG_GLOBAL": str(inherited), "GIT_CONFIG_SYSTEM": str(inherited),
                     "GIT_CONFIG_NOSYSTEM": "0"}
        with mock.patch.dict(os.environ, overrides):
            status, output, errors = self.command("--host-list")
            self.assertEqual((status, errors), (0, ""))
            self.assertIn("saved to", output)
            approval = output.splitlines()[-1].removeprefix("Digest: ")
            status, output, errors = self.command("--host-list", "--go", approval)
            self.assertEqual((status, errors), (0, ""), output)
            self.assertEqual(os.environ["GIT_CONFIG_GLOBAL"], str(inherited))
        self.assertFalse(path.exists())
        refs = self.git(source, "for-each-ref", "--format=%(refname)", "refs/rescue/old/snapshot-*")
        self.assertEqual(len(refs.splitlines()), 1)
        self.assertEqual(self.git(source, "show", f"{refs}:work"), "keep this work")

    def test_worktree_listing_skips_inherited_include_paths(self) -> None:
        source, checkout = self.git_fixture()
        protected = self.home / "rust/hanadocs/private.conf"
        protected.parent.mkdir(parents=True)
        _ = protected.write_text("invalid Git config\n")
        inherited = self.home / "inherited.gitconfig"
        _ = inherited.write_text(f"[include]\npath = {protected}\n")
        self.write_settings(f"{source}, {checkout}")
        with mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(inherited)}):
            status, output, errors = self.command("--host-list")
        self.assertEqual((status, errors), (0, ""))
        self.assertIn(f"registration: {str(git_files.at(checkout).git_dir)!r}", output)
        self.assertIn(f"{str(source)!r}\t", output)
        self.assertIn("\tthe clone has registered linked worktrees", output)

    def test_local_git_config_with_a_protected_path_is_review_only(self) -> None:
        _, checkout = self.clone_fixture()
        protected = self.build / "cargo/ignore"
        protected.parent.mkdir()
        _ = protected.write_text("untracked\n")
        _ = (checkout / "untracked").write_text("keep this work\n")
        _ = self.git(checkout, "config", "--local", "core.excludesFile", str(protected))
        with mock.patch.object(cleanup_old_folders, "LINUX_BUILD_ROOT", self.build):
            status, output, errors = self.command("--host-list")
        self.assertEqual((status, errors), (0, ""))
        self.assertIn(f"review only: local Git config names a protected path: {protected}", output)
        self.assertNotIn("would remove", output)

    def test_git_worktree_root_must_match_the_row_before_status(self) -> None:
        _, checkout = self.clone_fixture()
        for root in (str(self.build), "../.."):
            with self.subTest(root=root):
                _ = self.git(checkout, "config", "--local", "core.worktree", root)
                with mock.patch.object(cleanup_old_folders, "cleanup_git", wraps=cleanup_old_folders.cleanup_git) as git:
                    status, output, errors = self.command("--host-list")
                self.assertEqual((status, errors), (0, ""))
                self.assertIn("review only: Git worktree root does not match the row path", output)
                self.assertFalse(any("status" in invocation.args for invocation in git.call_args_list))
                self.assertNotIn("would remove", output)
        _ = self.git(checkout, "config", "--local", "core.worktree", "..")
        status, output, errors = self.command("--host-list")
        self.assertEqual((status, errors), (0, ""))
        self.assertIn("would remove", output)

    def test_clone_with_a_nonlocal_origin_stays(self) -> None:
        _, path = self.clone_fixture()
        _ = self.git(path, "remote", "set-url", "origin", "ssh://invalid.example/project")
        status, output, errors = self.command("--host-list")
        self.assertEqual((status, errors), (0, ""))
        self.assertIn("no local source repository", output)
        self.assertNotIn("would remove", output)
        self.assertEqual((path / "work").read_text(), "original\n")

    def test_list_prunes_protected_folders_before_reading_git_metadata(self) -> None:
        source, path = self.clone_fixture()
        protected = (self.home / "rust/hanadocs", self.build / "cargo", self.build / "old-cache")
        for root in protected:
            root.parent.mkdir(parents=True, exist_ok=True)
            _ = self.git(root.parent, "clone", "-q", "--shared", str(path), str(root))
        original_scandir = os.scandir
        checkout_at = git_files.at
        read_folders: list[Path] = []
        git_folders: list[Path] = []

        def scan(folder: str | int | Path) -> Iterator[os.DirEntry[str]]:
            if not isinstance(folder, int):
                read_folders.append(Path(folder))
            return original_scandir(folder)

        def read_checkout(folder: Path) -> git_files.Checkout:
            git_folders.append(folder)
            return checkout_at(folder)

        with (mock.patch.object(cleanup_old_folders, "LINUX_BUILD_ROOT", self.build),
              mock.patch.object(os, "scandir", side_effect=scan),
              mock.patch.object(git_files, "at", side_effect=read_checkout)):
            status, output, errors = self.command("--host-list")
        self.assertEqual((status, errors), (0, ""))
        self.assertIn(f"clone of {str(source)!r}", output)
        self.assertIn("would remove", output)
        self.assertIn(self.home / "rust", read_folders)
        self.assertIn(self.build, read_folders)
        for seen in read_folders + git_folders:
            self.assertFalse(any(seen.is_relative_to(root) for root in protected), str(seen))

    def test_clone_with_an_alternates_borrower_stays(self) -> None:
        source, path = self.clone_fixture()
        _ = (path / "work").write_text("unpublished changes\n")
        for root in (self.home / "rust", self.home / "worktrees/nested/owner", self.build / "repo/unit"):
            with self.subTest(root=root):
                root.mkdir(parents=True)
                borrower = root / "borrower"
                _ = self.git(root, "clone", "-q", "--shared", str(source), str(borrower))
                objects = borrower / ".git/objects"
                alternate = os.path.relpath(path / ".git/objects", objects)
                with (objects / "info/alternates").open("a") as output:
                    _ = output.write(f"{alternate}\n")
                status, output, errors = self.command("--host-list")
                self.assertEqual((status, errors), (0, ""))
                self.assertIn(f"Git object storage is used by {borrower}", output)
                self.assertNotIn("would remove", output)
                self.assertTrue((path / "work").is_file())
                (objects / "info/alternates").unlink()

    def test_orphan_row_does_not_block_clone_review_or_removal(self) -> None:
        _, path = self.clone_fixture()
        orphan = self.home / "worktrees/absent"
        orphan.parent.mkdir()
        lock = orphan.with_name("absent.lock")
        lock.touch()
        storage_path = cleanup_old_folders.CleanupBounds.storage_path

        def check_storage(bounds: cleanup_old_folders.CleanupBounds, place: Path, *, follow_links: bool = True) -> Path:
            self.assertNotEqual(place, orphan, "an absent base must never be resolved")
            return storage_path(bounds, place, follow_links=follow_links)

        with mock.patch.object(cleanup_old_folders.CleanupBounds, "storage_path", check_storage):
            status, output, errors = self.command("--host-list")
            self.assertEqual((status, errors), (0, ""))
            self.assertRegex(output, rf"{str(path)!r}\t[0-9]+\twould remove")
            self.assertRegex(output, rf"{str(orphan)!r}\t[0-9]+\tlist only: no setting names it")
            approval = output.splitlines()[-1].removeprefix("Digest: ")
            status, output, errors = self.command("--host-list", "--go", approval)
        self.assertEqual((status, errors), (0, ""), output)
        self.assertFalse(path.exists())
        self.assertTrue(lock.is_file())

    def test_dirty_worktree_is_saved_in_the_source_before_removal(self) -> None:
        source, path = self.git_fixture()
        _ = (path / "work").write_text("dirty work\n")
        _ = (path / "untracked").write_text("new work\n")
        head = self.git(source, "rev-parse", "HEAD")
        _ = self.git(source, "update-ref", "refs/stash", head)
        preview = self.command("--host-list")
        self.assertIn("dirty: yes, unpushed: 0, stash: yes", preview[1])
        self.assertIn(f"registration: {str(git_files.at(path).git_dir)!r}", preview[1])
        self.assertIn("saved to refs/rescue on --go", preview[1])
        self.assertEqual(self.git(source, "for-each-ref", "refs/rescue"), "")
        approval = preview[1].splitlines()[-1].removeprefix("Digest: ")
        status, output, errors = self.command("--host-list", "--go", approval)
        self.assertEqual((status, errors), (0, ""))
        self.assertIn(f"removed {str(path)!r}", output)
        self.assertFalse(path.exists())
        self.assertNotIn(str(path), self.git(source, "worktree", "list", "--porcelain"))
        saved = self.git(source, "for-each-ref", "--format=%(refname)", "refs/rescue")
        self.assertEqual(self.git(source, "show", f"{saved}:work"), "dirty work")
        self.assertEqual(self.git(source, "show", f"{saved}:untracked"), "new work")

    def test_removal_preserves_another_missing_worktree_registration(self) -> None:
        source, path = self.git_fixture()
        registration = git_files.at(path).git_dir
        second = self.home / "second"
        _ = self.git(source, "worktree", "add", "-q", "--detach", str(second))
        second_registration = git_files.at(second).git_dir
        _ = second.rename(self.home / "unmounted")
        approval = self.approval("--host-list")
        status, output, errors = self.command("--host-list", "--go", approval)
        self.assertEqual((status, errors), (0, ""), output)
        self.assertFalse(path.exists())
        self.assertFalse(registration.exists())
        self.assertTrue(second_registration.is_dir())
        self.assertEqual((second_registration / "gitdir").read_text().strip(), str(second / ".git"))

    def test_git_locked_worktree_stays(self) -> None:
        source, path = self.git_fixture()
        approval = self.approval("--host-list")
        _ = self.git(source, "worktree", "lock", str(path))
        status, output, errors = self.command("--host-list")
        self.assertEqual((status, errors), (0, ""))
        self.assertIn("\tgit worktree locked\n", output)
        applied = self.command("--host-list", "--go", approval)
        self.assertEqual(applied[0], 1)
        self.assertTrue((path / ".git").is_file())

    def test_git_worktree_locked_during_save_stays(self) -> None:
        source, path = self.git_fixture()
        approval = self.approval("--host-list")
        original_rescue = git_rescue.rescue

        def lock_after_save(work_tree: Path, name: str, *, retain_local_refs: bool,
                            pass_fds: Sequence[int]) -> git_rescue.RescueResult:
            saved = original_rescue(work_tree, name, retain_local_refs=retain_local_refs, pass_fds=pass_fds)
            _ = self.git(source, "worktree", "lock", str(path))
            return saved

        with mock.patch.object(git_rescue, "rescue", side_effect=lock_after_save):
            status, output, errors = self.command("--host-list", "--go", approval)
        self.assertEqual((status, errors), (1, ""))
        self.assertIn("git worktree locked", output)
        self.assertTrue((path / ".git").is_file())
        self.assertTrue((git_files.at(path).git_dir / "locked").is_file())

    def test_git_worktree_registration_changed_during_save_stays(self) -> None:
        _, path = self.git_fixture()
        approval = self.approval("--host-list")
        registration = git_files.at(path).git_dir
        original_rescue = git_rescue.rescue

        def add_after_save(work_tree: Path, name: str, *, retain_local_refs: bool,
                           pass_fds: Sequence[int]) -> git_rescue.RescueResult:
            saved = original_rescue(work_tree, name, retain_local_refs=retain_local_refs, pass_fds=pass_fds)
            _ = (registration / "late").write_text("keep this entry")
            return saved

        with mock.patch.object(git_rescue, "rescue", side_effect=add_after_save):
            status, output, errors = self.command("--host-list", "--go", approval)
        self.assertEqual((status, errors), (1, ""))
        self.assertIn("Git state or contents changed during the save", output)
        self.assertTrue((path / ".git").is_file())
        self.assertEqual((registration / "late").read_text(), "keep this entry")

    def test_cleanup_checks_later_entries_before_the_first_removal(self) -> None:
        _, path = self.git_fixture()
        later = path / "later"
        later.mkdir()
        _ = (later / "original").write_text("unchanged")
        approval = self.approval("--host-list")
        remove = removal_guard.remove_cleanup

        def add_before_remove(checked: removal_guard.CheckedCleanupPath,
                              entries: removal_guard.ApprovedCleanup,
                              *additional: removal_guard.CleanupTarget,
                              before_delete: Callable[[], None] | None = None) -> None:
            _ = (later / "new").write_text("late file")
            remove(checked, entries, *additional, before_delete=before_delete)

        with mock.patch.object(removal_guard, "remove_cleanup", side_effect=add_before_remove):
            status, output, errors = self.command("--host-list", "--go", approval)
        self.assertEqual((status, errors), (1, ""))
        self.assertIn("changed before removal", output)
        self.assertTrue((path / ".git").is_file())
        self.assertTrue((path / "work").is_file())
        self.assertTrue((later / "original").is_file())

    def test_process_use_that_starts_during_guard_preflight_keeps_the_folder(self) -> None:
        for git in (False, True):
            with self.subTest(git=git):
                if git:
                    _, path = self.git_fixture()
                else:
                    path = self.candidate("plain")
                later = path / "later"
                later.mkdir()
                payload = later / "payload"
                _ = payload.write_text("keep this file")
                self.write_settings(str(path))
                lock = path.with_name(path.name + ".lock")
                lock.touch()
                approval = self.approval("--host-list")
                listdir = os.listdir
                inode = later.stat().st_ino
                active = False

                def opened(*_args: object, **_options: object) -> tmp_sweep.ProcessUsesKnown:
                    if not active:
                        return tmp_sweep.ProcessUsesKnown([])
                    with lock.open("r+") as contender, self.assertRaises(BlockingIOError):
                        fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return tmp_sweep.ProcessUsesKnown([(str(payload), 42, "holds a file")])

                def start_use(directory: Path | str | int) -> list[str]:
                    nonlocal active
                    if isinstance(directory, int) and os.fstat(directory).st_ino == inode:
                        active = True
                    return listdir(directory)

                remove = removal_guard.remove_cleanup

                def during_preflight(checked: removal_guard.CheckedCleanupPath,
                                     approved: removal_guard.ApprovedCleanup,
                                     *additional: removal_guard.CleanupTarget,
                                     before_delete: Callable[[], None] | None = None) -> None:
                    with mock.patch.object(os, "listdir", side_effect=start_use):
                        remove(checked, approved, *additional, before_delete=before_delete)

                with (mock.patch.object(tmp_sweep, "lsof_paths", side_effect=opened),
                      mock.patch.object(removal_guard, "remove_cleanup", side_effect=during_preflight)):
                    status, output, errors = self.command("--host-list", "--go", approval)
                self.assertEqual((status, errors), (1, ""), output)
                self.assertIn("pid 42 holds a file", output)
                self.assertEqual(payload.read_text(), "keep this file")

    def test_partial_cleanup_records_deleted_entries_and_keeps_the_git_marker(self) -> None:
        _, path = self.git_fixture()
        early = path / "early"
        _ = early.write_text("delete this first")
        later = path / "later"
        later.mkdir()
        _ = (later / "original").write_text("unchanged")
        approval = self.approval("--host-list")
        unlink = os.unlink

        def add_after_unlink(name: str | Path, *, dir_fd: int | None = None) -> None:
            unlink(name, dir_fd=dir_fd)
            if name == early.name and dir_fd is not None:
                _ = (later / "new").write_text("late file")

        with mock.patch.object(os, "unlink", side_effect=add_after_unlink):
            status, output, errors = self.command("--host-list", "--go", approval)
        self.assertEqual((status, errors), (1, ""))
        self.assertIn("partial removal", output)
        self.assertIn(f"deleted {str(early)!r}", output)
        self.assertFalse(early.exists())
        self.assertTrue((path / ".git").is_file())
        self.assertTrue((later / "new").is_file())
        self.assertTrue((later / "original").is_file())

    def test_unpushed_commit_is_retained_by_the_source_rescue_ref(self) -> None:
        source, path = self.git_fixture()
        _ = self.git(path, "switch", "-qc", "local-work")
        _ = (path / "work").write_text("new commit\n")
        commit = commit_repository(path, "local work")
        preview = self.command("--host-list")
        self.assertIn("dirty: no, unpushed: 1, stash: no", preview[1])
        approval = preview[1].splitlines()[-1].removeprefix("Digest: ")
        self.assertEqual(self.command("--host-list", "--go", approval)[0], 0)
        self.assertFalse(path.exists())
        saved = self.git(source, "for-each-ref", "--format=%(refname)", "refs/rescue")
        self.assertIn(commit, self.git(source, "rev-list", saved))

    def test_git_save_failure_keeps_the_worktree(self) -> None:
        source, path = self.git_fixture()
        _ = (path / "work").write_text("keep my work\n")
        approval = self.approval("--host-list")
        error = git_rescue.RescueError(("git", "update-ref"), "ref is locked")
        with mock.patch.object(git_rescue, "rescue", side_effect=error):
            status, output, errors = self.command("--host-list", "--go", approval)
        self.assertEqual((status, errors), (1, ""))
        self.assertIn("Git save failed", output)
        self.assertIn("ref is locked", output)
        self.assertEqual((path / "work").read_text(), "keep my work\n")
        self.assertIn(str(path), self.git(source, "worktree", "list", "--porcelain"))

    def test_git_use_after_a_good_save_keeps_the_worktree(self) -> None:
        source, path = self.git_fixture()
        _ = (path / "work").write_text("keep active work\n")
        approval = self.approval("--host-list")
        clear = tmp_sweep.ProcessUsesKnown([])
        active = tmp_sweep.ProcessUsesKnown([(str(path / "work"), 42, "holds a file")])
        with mock.patch.object(tmp_sweep, "lsof_paths", side_effect=(clear, clear, active)):
            status, output, errors = self.command("--host-list", "--go", approval)
        self.assertEqual((status, errors), (1, ""))
        self.assertIn("pid 42 holds a file", output)
        self.assertTrue(self.git(source, "for-each-ref", "refs/rescue"))
        self.assertEqual((path / "work").read_text(), "keep active work\n")

    def stash_history(self, source: Path) -> str:
        older = self.git(source, "rev-parse", "HEAD")
        _ = self.git(source, "update-ref", "--create-reflog", "refs/stash", older)
        _ = (source / "work").write_text("second version\n")
        newer = commit_repository(source, "second version")
        _ = self.git(source, "update-ref", "refs/stash", newer)
        return newer

    def test_git_registration_index_in_use_keeps_the_worktree(self) -> None:
        _, path = self.git_fixture()
        registration = git_files.at(path).git_dir
        proc, process = self.proc_fixture()
        (process / "cwd").symlink_to(self.home)
        (process / "fd/3").symlink_to(registration / "index")
        with mock.patch.object(cleanup_old_folders, "PROC_ROOT", str(proc)):
            approval = self.approval("--host-list")
            status, output, errors = self.command("--host-list", "--go", approval)
        self.assertEqual((status, errors), (1, ""), output)
        self.assertIn("pid 42 holds a file open in it", output)
        self.assertTrue((registration / "index").is_file())
        self.assertTrue((path / "work").is_file())

    def test_older_stash_changes_the_digest_with_the_same_tip(self) -> None:
        source, path = self.git_fixture()
        tip = self.stash_history(source)
        before = self.approval("--host-list")
        _ = self.git(source, "reflog", "delete", "refs/stash@{1}")
        self.assertEqual(self.git(source, "rev-parse", "refs/stash"), tip)
        self.assertNotEqual(self.approval("--host-list"), before)
        self.assertTrue(path.is_dir())

    def test_checkout_without_origin_stays(self) -> None:
        source, path = self.git_fixture(linked=False)
        preview = self.command("--host-list")
        self.assertIn("no local source repository", preview[1])
        self.assertIn("dirty: no, unpushed: 0, stash: no", preview[1])
        self.assertNotIn("saved to refs/rescue on --go", preview[1])
        approval = preview[1].splitlines()[-1].removeprefix("Digest: ")
        self.assertEqual(self.command("--host-list", "--go", approval)[0], 0)
        self.assertEqual((path / "work").read_text(), "original\n")
        self.assertEqual(self.git(source, "for-each-ref", "refs/rescue"), "")

    def test_worktree_file_added_after_approval_stays(self) -> None:
        source, path = self.git_fixture()
        new = path / "late"
        approval = self.approval("--host-list")
        original_review = cleanup_old_folders.review_cleanup_git

        def add_after_save(checked: cleanup_old_folders.CheckedCleanup,
                           tree: cleanup_old_folders.CleanupTree,
                           guard: cleanup_old_folders.CleanupGuard) -> cleanup_old_folders.GitCleanupReview:
            review = original_review(checked, tree, guard)
            if "refs/rescue/" in review.refs:
                _ = new.write_text("keep this new file\n")
            return review

        with mock.patch.object(cleanup_old_folders, "review_cleanup_git", side_effect=add_after_save):
            status, output, errors = self.command("--host-list", "--go", approval)
        self.assertEqual((status, errors), (1, ""))
        self.assertIn("changed before removal", output)
        self.assertEqual(new.read_text(), "keep this new file\n")
        self.assertEqual((path / "work").read_text(), "original\n")
        self.assertIn(str(path), self.git(source, "worktree", "list", "--porcelain"))
        self.assertTrue(self.git(source, "for-each-ref", "refs/rescue"))

    def test_older_stash_changed_during_save_keeps_the_worktree(self) -> None:
        source, path = self.git_fixture()
        tip = self.stash_history(source)
        approval = self.approval("--host-list")
        original_rescue = git_rescue.rescue

        def drop_older_entry(work_tree: Path, name: str, *, retain_local_refs: bool,
                             pass_fds: Sequence[int]) -> git_rescue.RescueResult:
            saved = original_rescue(work_tree, name, retain_local_refs=retain_local_refs, pass_fds=pass_fds)
            _ = self.git(source, "reflog", "delete", "refs/stash@{1}")
            return saved

        with mock.patch.object(git_rescue, "rescue", side_effect=drop_older_entry):
            status, output, errors = self.command("--host-list", "--go", approval)
        self.assertEqual((status, errors), (1, ""))
        self.assertIn("Git state or contents changed during the save", output)
        self.assertEqual(self.git(source, "rev-parse", "refs/stash"), tip)
        self.assertTrue((path / "work").is_file())
        self.assertIn(str(path), self.git(source, "worktree", "list", "--porcelain"))

    def test_changed_stash_ref_refuses_reviewed_git_removal(self) -> None:
        source, path = self.git_fixture()
        approval = self.approval("--host-list")
        head = self.git(source, "rev-parse", "HEAD")
        _ = self.git(source, "update-ref", "refs/stash", head)
        status, output, errors = self.command("--host-list", "--go", approval)
        self.assertEqual((status, output), (1, ""))
        self.assertIn("list digest changed", errors)
        self.assertTrue(path.is_dir())
        self.assertEqual(self.git(source, "for-each-ref", "refs/rescue"), "")

    def test_cleanup_reads_stash_without_the_stash_command(self) -> None:
        guard = Path(removal_guard.__file__)
        for module in (SCRIPT, guard, Path(git_rescue.__file__)):
            with self.subTest(module=module):
                self.assertNotRegex(module.read_text(), r"\bgit\s+stash\b|[\"']stash[\"']\s*[,)]")

    def test_config_expands_exact_and_single_component_globs(self) -> None:
        candidates = [self.candidate("with space"), self.candidate("port-gate-tree-1"),
                      self.candidate("port-gate-tree-2")]
        protected = self.candidate("rust/hanadocs")
        sidecar = self.home / "port-gate-tree-1.lock"
        sidecar.touch()
        self.write_settings("~/with space, ~/port-gate-tree*, ~/missing, ~/rust/hanadocs, /build/cargo, /build/old-*")
        status, output, errors = self.command("--host-list")
        self.assertEqual((status, errors), (0, ""))
        for candidate in candidates:
            self.assertIn(f"{str(candidate)!r}", output)
        for forbidden in (str(protected), str(self.home / "missing"), str(sidecar), "/build/cargo", "/build/old-"):
            self.assertNotIn(forbidden, output)
        self.write_settings("~/port*/target")
        status, output, errors = self.command("--host-list")
        self.assertEqual((status, output), (1, ""))
        self.assertIn("final-component glob", errors)

    def saved_target(self, place: str) -> Path:
        saved = self.home / place
        profile = saved / "target/debug"
        profile.mkdir(parents=True)
        (profile / ".cargo-lock").touch()
        _ = (profile / "tool").write_bytes(b"fixture output")
        _ = (profile / "tool.d").write_text("tool: src/main.rs\n")
        return saved

    def test_saved_targets_use_the_configured_roots_and_depth(self) -> None:
        included = tuple(self.saved_target(f"{parent}/.saved-target-found") for parent in (
            "rust", "worktrees", "worktrees/repo", "worktrees/repo/unit", "worktrees/repo/owner/unit",
        ))
        excluded = tuple(self.saved_target(f"{parent}/.saved-target-excluded") for parent in (
            "rust/hanadocs", "rust/other", "worktrees/repo/owner/unit/deeper",
        ))
        (self.home / "worktrees/alias").symlink_to(self.home / "rust/hanadocs", target_is_directory=True)
        self.write_settings("~/rust/.saved-target-*, ~/worktrees/**/.saved-target-*")
        status, output, errors = self.command("--host-list")
        self.assertEqual((status, errors), (0, ""))
        for path in included:
            self.assertRegex(output, rf"{str(path)!r}\t[0-9]+\twould remove")
        for path in excluded:
            self.assertNotIn(str(path), output)
        self.assertNotIn("/alias/", output)

    def test_saved_targets_keep_mixed_contents_and_check_process_use(self) -> None:
        clean = self.saved_target("rust/.saved-target-clean")
        busy = self.saved_target("rust/.saved-target-busy")
        wrapper = self.saved_target("rust/.saved-target-wrapper")
        target = self.saved_target("rust/.saved-target-target")
        profile = self.saved_target("rust/.saved-target-profile")
        doc = self.saved_target("rust/.saved-target-doc")
        notes = doc / "target/doc/notes.txt"
        notes.parent.mkdir()
        wrong_type = self.saved_target("rust/.saved-target-wrong-type")
        (wrong_type / "target/doc/notes").mkdir(parents=True)
        _ = (wrong_type / "target/doc/notes.d").write_text("not Cargo output\n")
        wrong_doc = self.saved_target("rust/.saved-target-wrong-doc")
        (wrong_doc / "target/doc/index.html").mkdir(parents=True)
        doc_profile = self.saved_target("rust/.saved-target-doc-profile")
        doc_folder = doc_profile / "target/doc"
        doc_folder.mkdir()
        for name in (".cargo-lock", "notes", "notes.d"):
            _ = (doc_folder / name).write_text("keep my notes")
        unknown_directory = self.saved_target("rust/.saved-target-unknown-directory")
        (unknown_directory / "target/tmp/notes").mkdir(parents=True)
        for extra in (wrapper / "notes.txt", target / "target/notes.txt", profile / "target/debug/notes.txt", notes):
            _ = extra.write_text("keep my notes")
        self.write_settings("~/rust/.saved-target-*")
        uses = tmp_sweep.ProcessUsesKnown([(str(busy / "target/debug/tool"), 42, "holds a file")])
        with mock.patch.object(tmp_sweep, "lsof_paths", return_value=uses):
            status, output, errors = self.command("--host-list")
            self.assertEqual((status, errors), (0, ""))
            self.assertRegex(output, rf"{str(clean)!r}\t[0-9]+\twould remove")
            self.assertRegex(output, rf"{str(busy)!r}\t[0-9]+\tpid 42 holds a file")
            for path in (wrapper, target, profile, doc, wrong_type, wrong_doc, doc_profile, unknown_directory):
                self.assertRegex(output, rf"{str(path)!r}\t[0-9]+\t.*not verified as Cargo output")
            approval = output.splitlines()[-1].removeprefix("Digest: ")
            status, output, errors = self.command("--host-list", "--go", approval)
        self.assertEqual((status, errors), (0, ""), output)
        self.assertFalse(clean.exists())
        for path in (busy, wrapper, target, profile, doc, wrong_type, wrong_doc, doc_profile, unknown_directory):
            self.assertTrue(path.is_dir())

    def test_saved_target_accepts_known_build_tool_output_under_target(self) -> None:
        saved = self.saved_target("rust/.saved-target-generated")
        target = saved / "target"
        for name in ("mend-analyzing", "mend-wrapper", "external-client", "rust-analyzer"):
            folder = target / name
            folder.mkdir()
            _ = (folder / "libgenerated.rlib").write_bytes(b"build output")
        doc = target / "doc/fixture"
        doc.mkdir(parents=True)
        _ = (doc / "index.html").write_text("<title>API docs</title>")
        for name in ("nextest-hang-wrapper", "debug/cargo-mend", "debug/cargo-tile"):
            binary = target / name
            _ = binary.write_bytes(b"executable")
            binary.chmod(0o755)
        self.write_settings("~/rust/.saved-target-*")
        status, output, errors = self.command("--host-list")
        self.assertEqual((status, errors), (0, ""))
        self.assertRegex(output, rf"{str(saved)!r}\t[0-9]+\twould remove")

    def test_saved_target_removes_known_mend_links_without_removing_their_targets(self) -> None:
        target = self.saved_target("rust/.saved-target-mend-alias")
        mixed = self.saved_target("rust/.saved-target-mixed-alias")
        alias_root = "mend-wrapper/0.22.3+nogit+1793535530725844413/"
        retained = alias_root + "independent-copy-v1/179270272-after-1794725410316912539/cargo-mend"
        old = alias_root + "cargo-mend"
        programs = (
            (retained, self.home / ".cache/cargo-mend/driver-c63a3f15070b7803" / retained),
            (old, self.home / ".local/state/tool-based-ui-hana-migration/mend-0.22.3/bin/cargo-mend"),
        )
        for name, program in programs:
            program.parent.mkdir(parents=True)
            _ = program.write_text("outside executable")
            alias = target / "target" / name
            alias.parent.mkdir(parents=True, exist_ok=True)
            alias.symlink_to(program)
        unexpected = mixed / "target" / retained
        unexpected.parent.mkdir(parents=True)
        unexpected.symlink_to(self.home / "private")
        self.write_settings("~/rust/.saved-target-*")
        status, output, errors = self.command("--host-list")
        self.assertEqual((status, errors), (0, ""))
        self.assertRegex(output, rf"{str(target)!r}\t[0-9]+\twould remove")
        self.assertRegex(output, rf"{str(mixed)!r}\t[0-9]+\t.*not verified as Cargo output")
        approval = output.splitlines()[-1].removeprefix("Digest: ")
        status, output, errors = self.command("--host-list", "--go", approval)
        self.assertEqual((status, errors), (0, ""), output)
        self.assertFalse(target.exists())
        self.assertTrue(unexpected.is_symlink())
        for _, program in programs:
            self.assertEqual(program.read_text(), "outside executable")

    def test_saved_target_checks_wasm_output_and_its_dep_info(self) -> None:
        saved = self.saved_target("rust/.saved-target-wasm")
        profile = saved / "target/wasm32-unknown-unknown/release"
        profile.mkdir(parents=True)
        _ = (profile / "server.wasm").write_bytes(b"build output")
        _ = (profile / "server.d").write_text("server.wasm: src/main.rs\n")
        self.write_settings("~/rust/.saved-target-*")
        status, output, errors = self.command("--host-list")
        self.assertEqual((status, errors), (0, ""))
        self.assertRegex(output, rf"{str(saved)!r}\t[0-9]+\twould remove")
        _ = (profile / "other.wasm").write_bytes(b"unknown output")
        status, output, errors = self.command("--host-list")
        self.assertEqual((status, errors), (0, ""))
        self.assertIn("review only: saved target contents are not verified as Cargo output", output)

    def staff_at(self, path: Path) -> staff.StaffSnapshot:
        row = staff.StaffRow(staff.StaffId("fixture-staff"), "worker", "fixture worker",
                             staff.ReportsTo(staff.StaffId("parent")), current_session=staff.StaffId("fixture"),
                             worktree=staff.PresentWorktree("natedev", path))
        return staff.StaffSnapshot((row,))

    def test_top_worktree_links_check_pwd_and_staff_and_unlink_only_the_link(self) -> None:
        root = self.home / "worktrees"
        root.mkdir()
        destination = self.candidate("destination")
        links = {name: root / name for name in ("dead", "free", "in-pwd", "on-staff")}
        for name, path in links.items():
            path.symlink_to(self.home / "missing" if name == "dead" else destination)
        proc, pid = self.proc_fixture()
        (pid / "cwd").symlink_to(destination)
        _ = (pid / "comm").write_text("fixture-shell\n")
        _ = (pid / "environ").write_bytes(f"PWD={links['in-pwd']}/child\0".encode())
        self.write_settings(", ".join(map(str, links.values())))
        with (mock.patch.object(cleanup_old_folders, "PROC_ROOT", str(proc)),
              mock.patch.object(staff, "snapshot", return_value=self.staff_at(links["on-staff"]))):
            status, output, errors = self.command("--host-list")
            self.assertEqual((status, errors), (0, ""))
            for name in ("dead", "free"):
                self.assertRegex(output, rf"{str(links[name])!r}\t[0-9]+\twould remove")
            self.assertIn("pid 42 fixture-shell uses the link in PWD", output)
            self.assertIn("staff row fixture worker", output)
            approval = output.splitlines()[-1].removeprefix("Digest: ")
            status, output, errors = self.command("--host-list", "--go", approval)
        self.assertEqual((status, errors), (0, ""), output)
        self.assertFalse(links["dead"].is_symlink())
        self.assertFalse(links["free"].is_symlink())
        self.assertTrue(links["in-pwd"].is_symlink())
        self.assertTrue(links["on-staff"].is_symlink())
        self.assertTrue((destination / "payload").is_file())

    def test_final_link_check_keeps_a_link_used_only_in_pwd(self) -> None:
        path = self.home / "worktrees/linked"
        path.parent.mkdir()
        path.symlink_to(self.candidate("destination"))
        self.write_settings(str(path))
        clear = cleanup_old_folders.CleanupProcessReview()
        active = cleanup_old_folders.CleanupProcessReview(pwd_users=((path / "inside", 42),))
        with mock.patch.object(cleanup_old_folders, "process_uses", return_value=clear):
            approval = self.approval("--host-list")
        with mock.patch.object(cleanup_old_folders, "process_uses", side_effect=(clear, clear, active)):
            status, output, errors = self.command("--host-list", "--go", approval)
        self.assertEqual((status, errors), (1, ""), output)
        self.assertIn("pid 42 uses it in PWD", output)
        self.assertTrue(path.is_symlink())

    def test_link_does_not_probe_its_protected_target(self) -> None:
        protected = self.candidate("cargo", home=self.build)
        link = self.home / "worktrees/to-cargo"
        link.parent.mkdir()
        link.symlink_to(os.path.relpath(protected / "payload", link.parent))
        proc, _ = self.proc_fixture()
        self.write_settings(str(link))
        original_stat = os.stat

        def check_stat(path: str | int | Path, *, dir_fd: int | None = None,
                       follow_symlinks: bool = True) -> os.stat_result:
            if not isinstance(path, int):
                self.assertFalse(Path(path).is_relative_to(protected), str(path))
                if follow_symlinks:
                    self.assertNotIn(Path(path), (link, Path(link.name)))
            return original_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

        with (mock.patch.object(cleanup_old_folders, "PROC_ROOT", str(proc)),
              mock.patch.object(cleanup_old_folders, "LINUX_BUILD_ROOT", self.build),
              mock.patch.object(os, "stat", side_effect=check_stat)):
            status, output, errors = self.command("--host-list")
        self.assertEqual((status, errors), (0, ""))
        self.assertIn(f"protected path: {protected}", output)
        self.assertNotIn("would remove", output)

    def test_discovered_places_stay_without_a_setting(self) -> None:
        scratch = self.candidate("p-unlisted", home=self.build)
        target = self.saved_target("rust/.saved-target-unlisted")
        folder = self.candidate("worktrees/unlisted")
        link = folder.with_name("dangling")
        link.symlink_to(self.home / "missing")
        proc, _ = self.proc_fixture()
        self.write_settings("~/missing")
        with mock.patch.object(cleanup_old_folders, "PROC_ROOT", str(proc)):
            status, output, errors = self.command("--host-list")
            self.assertEqual((status, errors), (0, ""))
            for path in (scratch, target, folder, link):
                self.assertRegex(output, rf"{str(path)!r}\t(?:[0-9]+|unknown)\tlist only: no setting names it")
            self.assertNotIn("would remove", output)
            approval = output.splitlines()[-1].removeprefix("Digest: ")
            self.assertEqual(self.command("--host-list", "--go", approval)[0], 0)
        for path in (scratch, target, folder, link):
            self.assertTrue(os.path.lexists(path))

    def test_incomplete_scan_keeps_dangling_links_and_orphan_sidecars(self) -> None:
        link = self.home / "worktrees/dangling"
        link.parent.mkdir()
        link.symlink_to(self.home / "missing")
        orphan = link.with_name("absent")
        lock = orphan.with_name(orphan.name + ".lock")
        lock.touch()
        proc, pid = self.proc_fixture()
        _ = (pid / "comm").write_text("unreadable-shell\n")
        self.write_settings(str(link))
        apply = cleanup_old_folders.apply

        def deny_reads(path: Path) -> bytes:
            if path == pid / "environ":
                raise PermissionError(errno.EACCES, "denied", path)
            return read_bytes(path)

        def apply_after_denial(review: cleanup_old_folders.HostList,
                               bounds: cleanup_old_folders.CleanupBounds) -> int:
            with mock.patch.object(Path, "read_bytes", deny_reads):
                return apply(review, bounds)

        read_bytes = Path.read_bytes
        with mock.patch.object(cleanup_old_folders, "PROC_ROOT", str(proc)):
            with mock.patch.object(Path, "read_bytes", deny_reads):
                status, output, errors = self.command("--host-list")
            self.assertEqual((status, errors), (0, ""))
            for path in (link, orphan):
                self.assertRegex(output, rf"{str(path)!r}\t[0-9]+\t.*open file scan is incomplete: pid 42 unreadable-shell")
            self.assertNotIn("would remove", output)
            approval = self.approval("--host-list")
            with mock.patch.object(cleanup_old_folders, "apply", side_effect=apply_after_denial):
                status, output, errors = self.command("--host-list", "--go", approval)
            self.assertEqual((status, errors), (1, ""))
            self.assertIn("open file scan is incomplete: pid 42 unreadable-shell", output)
        self.assertTrue(link.is_symlink())
        self.assertTrue(lock.is_file())

    def test_orphan_worktree_sidecars_are_list_only(self) -> None:
        root = self.home / "worktrees"
        root.mkdir()
        live = root / "live"
        dead = root / "dead"
        lease_only = root / "lease-only"
        lock_only = root / "lock-only"
        attached = root / "attached"
        attached.symlink_to(self.home / "missing")
        for path, pid in ((live, 42), (dead, 43), (lease_only, 43)):
            _ = build_folders.lease_path(path).write_text(json.dumps({"pid": pid, "process_start": "old"}))
        for path in (dead, live, lock_only, attached):
            path.with_name(path.name + ".lock").touch()
        self.write_settings("~/missing")

        def process(pid: int) -> process_start.ProcessStart:
            return process_start.Started("reused") if pid == 42 else process_start.Gone()

        with mock.patch.object(process_start, "process_start", side_effect=process):
            status, output, errors = self.command("--host-list")
            self.assertEqual((status, errors), (0, ""))
            for path in (dead, lease_only, lock_only):
                self.assertRegex(output, rf"{str(path)!r}\t[0-9]+\tlist only: no setting names it")
            self.assertIn("lease PID 42 is still running", output)
            approval = output.splitlines()[-1].removeprefix("Digest: ")
            status, output, errors = self.command("--host-list", "--go", approval)
        self.assertEqual((status, errors), (0, ""), output)
        for path in (dead, lease_only, lock_only):
            self.assertEqual(build_folders.lease_path(path).exists(), path != lock_only)
            self.assertEqual(path.with_name(path.name + ".lock").exists(), path != lease_only)
        self.assertTrue(build_folders.lease_path(live).is_file())
        self.assertTrue(attached.with_name(attached.name + ".lock").is_file())

    def test_table_quotes_sidecars_with_control_characters_and_invalid_bytes(self) -> None:
        root = self.home / "worktrees"
        root.mkdir()
        path = root / os.fsdecode(b"space\tline\nbyte-\xff")
        lock = path.with_name(path.name + ".lock")
        lock.touch()
        self.write_settings("~/missing")
        status, output, errors = self.command("--host-list")
        self.assertEqual((status, errors), (0, ""))
        rows = [line.split("\t") for line in output.splitlines() if line.startswith("natedev\t")]
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(rows[0]), 4)
        self.assertEqual(rows[0][1], repr(str(path)))
        self.assertTrue(rows[0][3].endswith(f"includes {lock.name!r}"), rows)

    def test_top_real_folders_show_staff_or_git_state_without_approval(self) -> None:
        source, checkout = self.git_fixture()
        busy = self.candidate("worktrees/busy")
        repo = self.home / "worktrees/retired"
        _ = self.git(source, "worktree", "move", str(checkout), str(repo))
        _ = self.git(repo, "switch", "-c", "fixture-branch")
        _ = commit_repository(repo, "unpushed", allow_empty=True)
        _ = (repo / "dirty").write_text("local change")
        model = self.candidate("worktrees/claude")
        proc, pid = self.proc_fixture()
        (pid / "cwd").symlink_to(busy)
        self.write_settings("~/missing")
        with (mock.patch.object(cleanup_old_folders, "PROC_ROOT", str(proc)),
              mock.patch.object(staff, "snapshot", return_value=self.staff_at(busy))):
            status, output, errors = self.command("--host-list")
            self.assertEqual((status, errors), (0, ""))
            self.assertIn("pid 42 works in it; staff row fixture worker", output)
            self.assertRegex(output, rf"{str(repo)!r}\tunknown\tlist only: no setting names it; review only,"
                                     + r" dirty: 1, unpushed: 1, branch: 'fixture-branch'")
            self.assertNotIn(str(model), output)
            self.assertNotIn("would remove", output)
            approval = output.splitlines()[-1].removeprefix("Digest: ")
            self.assertEqual(self.command("--host-list", "--go", approval)[0], 0)
        self.assertTrue(repo.is_dir())
        self.assertTrue(busy.is_dir())

    def test_build_scratch_includes_its_sidecars_and_waits_for_lease_pid(self) -> None:
        path = self.candidate("p-old", home=self.build)
        self.write_settings(str(path))
        lease = build_folders.lease_path(path)
        _ = lease.write_text(json.dumps({"pid": 42, "process_start": "old"}))
        lock = path.with_name(path.name + ".lock")
        lock.touch()
        with mock.patch.object(process_start, "process_start", return_value=process_start.Started("reused")):
            status, output, errors = self.command("--host-list")
        self.assertEqual((status, errors), (0, ""))
        self.assertIn(f"lease PID 42 is still running; includes {lease.name!r}, {lock.name!r}", output)
        unlink = os.unlink

        def after_folder(name: str | Path, *, dir_fd: int | None = None) -> None:
            if str(name) in (lease.name, lock.name):
                self.assertFalse(path.exists())
            unlink(name, dir_fd=dir_fd)

        with (mock.patch.object(process_start, "process_start", return_value=process_start.Gone()),
              mock.patch.object(build_folders.HeldFolderRemovalLease, "unlink_matching_sidecars") as bypass,
              mock.patch.object(os, "unlink", side_effect=after_folder)):
            approval = self.approval("--host-list")
            status, output, errors = self.command("--host-list", "--go", approval)
        self.assertEqual((status, errors), (0, ""), output)
        self.assertFalse(path.exists())
        self.assertFalse(lease.exists())
        self.assertFalse(lock.exists())
        bypass.assert_not_called()

    def test_final_sidecar_check_keeps_an_open_lease_or_incomplete_scan(self) -> None:
        proc, pid = self.proc_fixture()
        descriptor = pid / "fd/3"
        remove = removal_guard.remove_cleanup
        for incomplete in (False, True):
            with self.subTest(incomplete=incomplete):
                path = self.candidate(f"p-old-{int(incomplete)}", home=self.build)
                self.write_settings(str(path))
                lease = build_folders.lease_path(path)
                record = json.dumps({"pid": 42, "process_start": "old"})
                _ = lease.write_text(record)
                lock = path.with_name(path.name + ".lock")
                lock.touch()

                def open_after_removal(checked: removal_guard.CheckedCleanupPath,
                                       approved: removal_guard.ApprovedCleanup,
                                       *additional: removal_guard.CleanupTarget,
                                       before_delete: Callable[[], None] | None = None) -> None:
                    remove(checked, approved, *additional, before_delete=before_delete)
                    if checked.path == path:
                        if incomplete:
                            descriptor.touch()
                        else:
                            descriptor.symlink_to(lease)

                with (mock.patch.object(cleanup_old_folders, "PROC_ROOT", str(proc)),
                      mock.patch.object(process_start, "process_start", return_value=process_start.Gone()),
                      mock.patch.object(removal_guard, "remove_cleanup", side_effect=open_after_removal)):
                    approval = self.approval("--host-list")
                    status, output, errors = self.command("--host-list", "--go", approval)
                descriptor.unlink()
                self.assertEqual((status, errors), (1, ""), output)
                self.assertIn("open file scan is incomplete" if incomplete else "pid 42 holds a file", output)
                self.assertIn(f"deleted {str(path)!r}", output)
                self.assertFalse(path.exists())
                self.assertEqual(lease.read_text(), record)
                self.assertTrue(lock.is_file())

    def test_mac_worktree_sidecars_stay_while_the_lease_pid_is_alive(self) -> None:
        path = self.candidate("worktrees/claude-prioritize-index-refresh")
        self.write_settings(str(path))
        lease = build_folders.lease_path(path)
        _ = lease.write_text(json.dumps({"pid": 42, "process_start": "old"}))
        lock = path.with_name(path.name + ".lock")
        lock.touch()
        with (mock.patch.object(cleanup_old_folders, "local_machine", return_value=cleanup_old_folders.Machine.MAC),
              mock.patch.object(process_start, "process_start", return_value=process_start.Started("current"))):
            status, output, errors = self.command("--host-list")
            self.assertEqual((status, errors), (0, ""))
            self.assertIn("lease PID 42 is still running", output)
            self.assertIn(lease.name, output)
            self.assertIn(lock.name, output)
            approval = output.splitlines()[-1].removeprefix("Digest: ")
            self.assertEqual(self.command("--host-list", "--go", approval)[0], 0)
        self.assertTrue(path.is_dir())
        self.assertTrue(lease.is_file())
        self.assertTrue(lock.is_file())

    def mac_worktree_fixture(self) -> tuple[Path, Path, Path]:
        source, path = self.git_fixture()
        worktree = self.home / "worktrees/claude-prioritize-index-refresh"
        worktree.parent.mkdir()
        _ = self.git(source, "worktree", "move", str(path), str(worktree))
        self.write_settings(str(worktree))
        lease = build_folders.lease_path(worktree)
        _ = lease.write_text(json.dumps({"pid": 42, "process_start": "old"}))
        lock = worktree.with_name(worktree.name + ".lock")
        lock.touch()
        return worktree, lease, lock

    def test_mac_worktree_removes_reviewed_sidecars_after_the_folder(self) -> None:
        worktree, lease, lock = self.mac_worktree_fixture()
        wrong_lock = lock.with_name("other.lock")
        wrong_lock.touch()
        unlink = os.unlink
        removed: list[str] = []

        def record_order(name: str | Path, *, dir_fd: int | None = None) -> None:
            if str(name) in (lease.name, lock.name):
                self.assertFalse(worktree.exists(), "sidecar removed before its folder")
                removed.append(str(name))
            unlink(name, dir_fd=dir_fd)

        with (mock.patch.object(cleanup_old_folders, "local_machine", return_value=cleanup_old_folders.Machine.MAC),
              mock.patch.object(process_start, "process_start", return_value=process_start.Gone()),
              mock.patch.object(os, "unlink", side_effect=record_order)):
            status, output, errors = self.command("--host-list")
            self.assertEqual((status, errors), (0, ""))
            self.assertIn(lease.name, output)
            self.assertIn(lock.name, output)
            approval = output.splitlines()[-1].removeprefix("Digest: ")
            status, output, errors = self.command("--host-list", "--go", approval)
        self.assertEqual((status, errors), (0, ""), output)
        self.assertFalse(worktree.exists())
        self.assertFalse(lease.exists())
        self.assertFalse(lock.exists())
        self.assertTrue(wrong_lock.exists())
        self.assertEqual(removed, [lease.name, lock.name])

    def test_mac_worktree_keeps_a_lease_that_changes_during_git_save(self) -> None:
        worktree, lease, lock = self.mac_worktree_fixture()
        original_rescue = git_rescue.rescue
        replacement = json.dumps({"pid": 43, "process_start": "new"})

        def replace_after_save(work_tree: Path, name: str, *, retain_local_refs: bool,
                               pass_fds: Sequence[int]) -> git_rescue.RescueResult:
            result = original_rescue(work_tree, name, retain_local_refs=retain_local_refs, pass_fds=pass_fds)
            next_lease = lease.with_suffix(".next")
            _ = next_lease.write_text(replacement)
            _ = next_lease.replace(lease)
            return result

        with (mock.patch.object(cleanup_old_folders, "local_machine", return_value=cleanup_old_folders.Machine.MAC),
              mock.patch.object(process_start, "process_start", return_value=process_start.Gone()),
              mock.patch.object(git_rescue, "rescue", side_effect=replace_after_save)):
            approval = self.approval("--host-list")
            status, output, errors = self.command("--host-list", "--go", approval)
        self.assertEqual((status, errors), (1, ""))
        self.assertIn("worktree sidecars changed", output)
        self.assertTrue((worktree / ".git").is_file())
        self.assertEqual(lease.read_text(), replacement)
        self.assertTrue(lock.exists())

    def test_replaced_sidecar_lock_prevents_lease_deletion_after_preflight(self) -> None:
        path = self.candidate("p-old", home=self.build)
        self.write_settings(str(path))
        lease = build_folders.lease_path(path)
        old_record = json.dumps({"pid": 42, "process_start": "old"})
        _ = lease.write_text(old_record)
        lock = path.with_name(path.name + ".lock")
        lock.touch()
        next_lock = lock.with_name("replacement.lock")
        _ = next_lock.write_text("replacement")
        recheck = cleanup_old_folders.CheckedSidecar.recheck
        remove = removal_guard.remove_cleanup

        def replace_after_preflight(checked: removal_guard.CheckedCleanupPath,
                                    approved: removal_guard.ApprovedCleanup,
                                    *additional: removal_guard.CleanupTarget,
                                    before_delete: Callable[[], None] | None = None) -> None:
            checks = 0

            def replace_lock(sidecar: cleanup_old_folders.CheckedSidecar) -> None:
                nonlocal checks
                if sidecar.path == lease:
                    checks += 1
                    if checks == 2:
                        with lock.open("r+") as contender:
                            with self.assertRaises(BlockingIOError):
                                fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        _ = next_lock.replace(lock)
                recheck(sidecar)

            with mock.patch.object(cleanup_old_folders.CheckedSidecar, "recheck", replace_lock):
                remove(checked, approved, *additional, before_delete=before_delete)

        with (mock.patch.object(process_start, "process_start", return_value=process_start.Gone()),
              mock.patch.object(removal_guard, "remove_cleanup", side_effect=replace_after_preflight)):
            approval = self.approval("--host-list")
            status, output, errors = self.command("--host-list", "--go", approval)
        self.assertEqual((status, errors), (1, ""))
        self.assertIn("worktree sidecar lock changed", output)
        self.assertFalse(path.exists())
        self.assertEqual(lease.read_text(), old_record)
        self.assertEqual(lock.read_text(), "replacement")

    def test_combined_digest_gives_the_mac_its_review_and_removes_only_the_local_row(self) -> None:
        path = self.candidate()
        neighbour = self.candidate("unlisted")
        self.write_settings("~/old")
        mac_path = self.home / "mac-old"
        mac = cleanup_old_folders.HostList(
            cleanup_old_folders.Machine.MAC, (cleanup_old_folders.Row(mac_path, 9, cleanup_old_folders.WouldRemove("a" * 64)),))
        mac_record = mac.record()
        visited: list[str] = []

        def run(arguments: Sequence[str]) -> subprocess.CompletedProcess[str]:
            visited.append(arguments[-1])
            if arguments[-1].endswith(" --json"):
                return subprocess.CompletedProcess(arguments, 0, json.dumps(mac_record), "")
            return subprocess.CompletedProcess(arguments, 0, f"removed {str(mac_path)!r}\n", "")

        status, output, errors = self.command(runner=run)
        self.assertEqual((status, errors), (0, ""))
        self.assertIn(f"mac\t{str(mac_path)!r}\t9\twould remove", output)
        self.assertIn("mac total: 9 bytes", output)
        approval = output.splitlines()[-1].removeprefix("Digest: ")
        self.assertNotEqual(approval, mac_record["digest"])
        status, output, errors = self.command("--go", approval, runner=run)
        self.assertEqual((status, errors), (0, ""))
        self.assertEqual(visited, [cleanup_old_folders.REMOTE_COMMAND + " --json"] * 2
                         + [cleanup_old_folders.REMOTE_COMMAND + " --go " + mac_record["digest"]])
        self.assertIn(f"removed {str(path)!r}", output)
        self.assertFalse(path.exists())
        self.assertTrue((neighbour / "payload").is_file())
