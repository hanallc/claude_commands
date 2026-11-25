"""Check manual cleanup with private folders and an injected SSH runner."""
from __future__ import annotations

import fcntl
import errno
import io
import json
import os
import pwd
import shutil
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
from scripts.lib import build_folders, git_files, git_rescue, process_start, removal_guard, session_move
from scripts.lib.git_test_support import commit_repository, fixture_git_environment
from scripts.lint import tmp_sweep
from scripts.production import staff

SCRIPT = Path(__file__).with_name("cleanup_old_folders.py").resolve()
GATE_LEASE_START = "Wed Nov 25 12:00:00 2026" if sys.platform == "darwin" else "123"
REUSED_GATE_LEASE_START = "Wed Nov 25 12:00:01 2026" if sys.platform == "darwin" else "124"
FIXTURE_GATE_DEVICE = -1


class CleanupTests(unittest.TestCase):
    home: Path = Path()
    configuration: Path = Path()
    build: Path = Path()
    gate: Path = Path()

    @override
    def setUp(self) -> None:
        base = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.home = base / "home"
        self.home.mkdir()
        self.build = base / "build"
        self.build.mkdir()
        self.gate = base / "gate"
        self.gate.mkdir()
        self.configuration = base / "disks.conf"
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {"HOME": str(self.home), "CONF_DIR": str(base)})))
        _ = (base / "build_folders.conf").write_text(
            f"roots.build={self.build}\nroots.gate={self.gate}\n# range: 0.001..1\nlocks.probe_retry_seconds=0.005\n")
        _ = self.enterContext(mock.patch.object(
            cleanup_old_folders, "local_machine", return_value=cleanup_old_folders.Machine.NATEDEV))
        _ = self.enterContext(mock.patch.object(cleanup_old_folders, "read_gate_device", return_value=FIXTURE_GATE_DEVICE))
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
            + "cleanup.list_only_reason.mac=bob's hand step\n")
        _ = (self.configuration.parent / "mac_test.conf").write_text("# range: 5e-324..\nfloor_sweep_timeout_s=17\n")

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
        return self.list_digest(output)

    def list_digest(self, output: str) -> str:
        digests = [line.removeprefix("Digest: ") for line in output.splitlines() if line.startswith("Digest: ")]
        self.assertEqual(len(digests), 1, output)
        self.assertRegex(digests[0], r"^[0-9a-f]{64}$")
        return digests[0]

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

    def test_worker_review_preserves_link_orphan_and_gate_fields(self) -> None:
        cleanup = cleanup_old_folders
        orphan = self.build / "p-orphan"
        rows = (
            cleanup.Row(self.home / "worktrees/dangling", 4096, cleanup.WouldRemove("a" * 64),
                        place=cleanup.CleanupPlace.LINK),
            cleanup.Row(orphan, None, removal_guard.Kept("review only"), "no Git checkout",
                        cleanup.worktree_sidecar_names(orphan), cleanup.CleanupPlace.ORPHAN_SIDECARS),
            cleanup.Row(self.gate / "t-old", 8192, cleanup.WouldRemove("b" * 64),
                        sidecars=("t-old.lock",), place=cleanup.CleanupPlace.GATE_FOLDER,
                        gate=cleanup.GateDetails("t", 1_700_000_000_000_000_000,
                                                 (cleanup.LeaseStatus(cleanup.LeaseState.DEAD, "PID 42"),),
                                                 "none found")),
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
def review_and_add(machine: cleanup.Machine, *, mode: cleanup.CleanupMode = cleanup.CleanupMode.STANDARD
                   ) -> tuple[cleanup.HostList, cleanup.CleanupBounds]:
    result = review_local(machine, mode=mode)
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

        def apply_with_new_name(review: cleanup_old_folders.HostList, bounds: cleanup_old_folders.CleanupBounds, *,
                                mode: cleanup_old_folders.CleanupMode = cleanup_old_folders.CleanupMode.STANDARD) -> int:
            late.mkdir()
            return apply(review, bounds, mode=mode)

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
        self.assertRegex(output, r"(?m)^Digest: [0-9a-f]{64}$")
        self.assertEqual((path / "payload").read_text(), "old content\n")
        ssh.assert_called_once()

    def test_list_ends_with_the_apply_command(self) -> None:
        self.write_settings(str(self.candidate()))
        for arguments, scope in (((), ""), (("--host-list",), " --host-list")):
            with self.subTest(arguments=arguments):
                status, output, errors = self.command(*arguments)
                self.assertEqual((status, errors), (0, ""))
                approval = self.list_digest(output)
                self.assertEqual(output.splitlines()[-2:], [
                    "To remove these folders, run:",
                    '"$(command -v python3)" -IB "$HOME/.claude/scripts/disks/cleanup_old_folders.py"'
                    + f"{scope} --go {approval}",
                ])

    def test_kept_rows_and_json_have_no_apply_command(self) -> None:
        self.write_settings("~/missing")
        status, output, errors = self.command("--host-list")
        self.assertEqual((status, errors), (0, ""))
        self.assertTrue(output.endswith(f"Digest: {self.list_digest(output)}\n"))
        self.assertNotIn(" these folders, run:", output)

        self.write_settings(str(self.candidate()))
        status, output, errors = self.command("--host-list", "--json")
        self.assertEqual((status, errors), (0, ""))
        record = cleanup_old_folders.message_object(cast(object, json.loads(output)))
        self.assertEqual(record["digest"], self.approval("--host-list"))
        self.assertNotIn(" these folders, run:", output)

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

    def test_sudo_remote_parent_starts_only_setpriv_and_opens_no_file_for_writing(self) -> None:
        self.write_settings("~/missing")
        mac_settings = self.configuration.parent / "mac_test.conf"
        _ = mac_settings.write_text(mac_settings.read_text().replace(
            "floor_sweep_timeout_s=17\n", "floor_sweep_timeout_s=invalid\n"))
        read_log = self.home / "config-reads.log"
        shift_log = self.home / "config-shift-reads.log"
        program = """
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch
from scripts.disks import cleanup_old_folders as cleanup

def refuse_write(event, arguments):
    if event == "open" and os.geteuid() == 0 and arguments[2] & (os.O_ACCMODE | os.O_CREAT | os.O_TRUNC):
        raise AssertionError(f"the sudo parent opened a file for writing: {arguments[0]}")

sys.addaudithook(refuse_write)
command = ("ssh", "mac")
user = cleanup.SudoCleanupUser(os.getuid(), os.getgid(), "fixture", Path.home(), "/fixture/setpriv")

def process_start(arguments, **kwargs):
    assert arguments[0] == user.setpriv, arguments
    return subprocess.CompletedProcess(arguments, 0)

with patch.object(subprocess, "run", side_effect=process_start) as run:
    with patch.object(os, "geteuid", return_value=0):
        assert cleanup.run_remote(command, user=user).returncode == 0
    assert run.call_count == 1
    assert tuple(run.call_args.args[0][-2:]) == command
"""
        result = subprocess.run((sys.executable, "-B", "-c", program), capture_output=True, text=True, check=False,
                                env=os.environ | {"CONF_READ_LOG": str(read_log),
                                                  "CONF_SHIFT_READ_LOG": str(shift_log)})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(read_log.exists())
        self.assertFalse(shift_log.exists())

    def test_cleanup_import_skips_gate_move_and_showrunners(self) -> None:
        program = """
import sys
from scripts.disks import cleanup_old_folders

loaded = {"scripts.lib.session_move", "scripts.production.showrunners"} & sys.modules.keys()
assert not loaded, sorted(loaded)
"""
        result = subprocess.run((sys.executable, "-B", "-c", program), cwd=SCRIPT.parents[2],
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_sudo_remote_child_uses_the_configured_timeout_and_reports_a_timeout(self) -> None:
        self.write_settings("~/missing")
        command = ("ssh", "mac")
        with (mock.patch.dict(os.environ, {"SUDO_UID": str(os.geteuid()), "SUDO_GID": str(os.getegid())}),
              mock.patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired(command, 17)) as ssh,
              redirect_stderr(io.StringIO()) as errors):
            self.assertEqual(cleanup_old_folders.run_remote_after_drop(command), 1)
        ssh.assert_called_once_with(command, capture_output=True, text=True, timeout=17, check=False)
        self.assertEqual(errors.getvalue(), "SSH timed out after 17 seconds\n")

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

    def test_a_descriptor_that_closes_during_the_scan_does_not_keep_a_folder(self) -> None:
        path = self.candidate()
        self.write_settings(str(path))
        proc, pid = self.proc_fixture()
        (pid / "cwd").symlink_to(self.home)
        descriptor = pid / "fd/5"
        readlink = os.readlink

        def read_descriptor(name: bytes | str | Path) -> bytes | str:
            if os.fsdecode(name) == str(descriptor):
                descriptor.unlink()
            return readlink(name) if isinstance(name, bytes) else readlink(os.fsdecode(name))

        def deny_descriptor(name: bytes | str | Path) -> bytes | str:
            if os.fsdecode(name) == str(descriptor):
                raise PermissionError(errno.EACCES, "denied", name)
            return readlink(name) if isinstance(name, bytes) else readlink(os.fsdecode(name))

        descriptor.symlink_to(path / "payload")
        with (mock.patch.object(cleanup_old_folders, "PROC_ROOT", str(proc)),
              mock.patch.object(os, "readlink", side_effect=deny_descriptor)):
            denied, kept, errors = self.command("--host-list")
        self.assertEqual((denied, errors), (0, ""))
        self.assertIn("open file scan is incomplete: pid 42", kept)
        self.assertTrue(descriptor.is_symlink())
        with (mock.patch.object(cleanup_old_folders, "PROC_ROOT", str(proc)),
              mock.patch.object(os, "readlink", side_effect=read_descriptor)):
            status, output, errors = self.command("--host-list")
        self.assertFalse(descriptor.is_symlink())
        self.assertEqual((status, errors), (0, ""))
        self.assertRegex(output, rf"{str(path)!r}\t[0-9]+\twould remove")

    @unittest.skipUnless(sys.platform == "linux", "requires the Linux cleanup parent")
    def test_sudo_parent_checks_processes_from_other_users_and_starts_an_unprivileged_worker(self) -> None:
        cleanup = cleanup_old_folders
        path = self.candidate()
        self.write_settings(str(path))
        proc, pid = self.proc_fixture()
        uid, gid = os.geteuid(), os.getegid()
        account = pwd.getpwuid(uid)
        (pid / "cwd").symlink_to(path)
        request = json.dumps({"kind": "process", "pid": 43, "paths": [str(path)], "links": False,
                              "descriptors": [], "gate_root": ""})
        review = json.dumps({"kind": "review", "rows": [], "unreadable": []})
        incoming = io.StringIO(f"{request}\n{review}\n")
        outgoing = io.StringIO()
        child = mock.MagicMock(stdout=incoming, stdin=outgoing)
        child.configure_mock(**{"wait.return_value": 0})
        worker = mock.MagicMock()
        worker.configure_mock(**{"__enter__.return_value": child})

        def fixture_executable(name: str) -> str:
            return f"/fixture/{name}"

        with (mock.patch.dict(os.environ, {"SUDO_UID": str(uid), "SUDO_GID": str(gid)}),
              mock.patch.object(os, "geteuid", return_value=0),
              mock.patch.object(cleanup, "PROC_ROOT", str(proc)),
              mock.patch.object(cleanup, "LINUX_BUILD_ROOT", self.build),
              mock.patch.object(cleanup, "protected_mounts", return_value=(self.build / "cargo",)) as masks,
              mock.patch.object(shutil, "which", side_effect=fixture_executable),
              mock.patch.object(subprocess, "Popen", return_value=worker) as spawn,
              mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess(
                  ("ssh",), 255, "", "fixture unreachable\n")) as ssh,
              mock.patch.object(outgoing, "close"), redirect_stdout(io.StringIO()) as output):
            self.assertEqual(cleanup.run_cli(()), 0)
        checked = cleanup.message_object(cast(object, json.loads(outgoing.getvalue())))
        self.assertEqual(checked["paths"], [[str(path), 42, tmp_sweep.WORKS_IN]])
        self.assertEqual(checked["unreadable"], [])
        self.assertIn("Use check: processes of all users.", output.getvalue())
        command = cast(tuple[str, ...], spawn.call_args.args[0])
        self.assertEqual(command[:9], (
            "/fixture/setpriv", f"--reuid={uid}", f"--regid={gid}", "--clear-groups", "--no-new-privs",
            "--inh-caps=-all", "--ambient-caps=-all", "--bounding-set=-all", "/fixture/bwrap",
        ))
        drop = command.index("--cap-drop")
        self.assertEqual(command[drop + 1], "ALL")
        self.assertEqual(command[-2:], (cleanup.CleanupMode.MOVE_GATE_GIT.value, str(self.build / "cargo")))
        masks.assert_called_once_with(Path(account.pw_dir), self.build)
        environment = cast(dict[str, str], spawn.call_args.kwargs["env"])
        self.assertEqual(environment["HOME"], account.pw_dir)
        ssh_command = cast(tuple[str, ...], ssh.call_args.args[0])
        self.assertEqual(ssh_command[:8], command[:8])
        self.assertEqual(ssh_command[8:11], (sys.executable, "-IB", "-c"))
        self.assertEqual(ssh_command[-5:], (
            "ssh", "-o", "BatchMode=yes", "mac", cleanup.REMOTE_COMMAND + " --json"))
        self.assertEqual(ssh.call_args.kwargs["env"], environment)

    @unittest.skipUnless(sys.platform == "linux", "requires the Linux cleanup worker")
    def test_sudo_list_ends_with_the_apply_command(self) -> None:
        cleanup = cleanup_old_folders
        _ = self.gate_checkout_to_move()
        review, _bounds = cleanup.review_local(cleanup.Machine.NATEDEV, mode=cleanup.CleanupMode.MOVE_GATE_GIT)
        incoming = io.StringIO(json.dumps(cleanup.worker_review(review)) + "\n")
        child = mock.MagicMock(stdout=incoming, stdin=io.StringIO())
        child.configure_mock(**{"wait.return_value": 0})
        worker = mock.MagicMock()
        worker.configure_mock(**{"__enter__.return_value": child})
        user = cleanup.SudoCleanupUser(1000, 1000, "fixture", self.home, "/fixture/setpriv")
        with (mock.patch.object(os, "geteuid", return_value=0),
              mock.patch.object(cleanup, "sudo_cleanup_user", return_value=user),
              mock.patch.object(cleanup, "protected_mounts", return_value=()),
              mock.patch.object(cleanup, "masked_cleanup_command", return_value=("bwrap",)),
              mock.patch.object(subprocess, "Popen", return_value=worker),
              redirect_stdout(io.StringIO()) as output):
            self.assertEqual(cleanup.run_cli((), run_ssh=self.unreachable), 0)
        approval = self.list_digest(output.getvalue())
        self.assertIn("would move to disk", output.getvalue())
        self.assertEqual(output.getvalue().splitlines()[-3:], [
            "1 folder, 0.00 GiB, would move from memory to disk.",
            "To move these folders, run:",
            'sudo "$(command -v python3)" -IB "$HOME/.claude/scripts/disks/cleanup_old_folders.py"'
            + f" --go {approval}",
        ])

    def test_move_count_line_counts_only_the_moving_folders(self) -> None:
        cleanup = cleanup_old_folders
        gib = 1 << 30
        rows = (cleanup.Row(Path("/dev/shm/t-a"), 3 * gib // 2, cleanup.WouldMove(Path("/build/a"), "a")),
                cleanup.Row(Path("/dev/shm/t-b"), gib // 2, cleanup.WouldMove(Path("/build/b"), "b")),
                cleanup.Row(Path("/dev/shm/t-c"), None, cleanup.WouldMove(Path("/build/c"), "c")),
                cleanup.Row(Path("/dev/shm/t-d"), 7 * gib, cleanup.WouldRemove("d")))
        local = cleanup.HostList(cleanup.Machine.NATEDEV, rows)
        with redirect_stdout(io.StringIO()) as output:
            cleanup.show_apply_command(local, local, "f" * 64, mode=cleanup.CleanupMode.MOVE_GATE_GIT,
                                       host_list=False)
        self.assertEqual(output.getvalue().splitlines()[0],
                         "3 folders, 2.00 GiB (size unknown for 1), would move from memory to disk.")
        with redirect_stdout(io.StringIO()) as output:
            cleanup.show_apply_command(local, local, "f" * 64, mode=cleanup.CleanupMode.STANDARD, host_list=False)
        self.assertNotIn("would move from memory to disk", output.getvalue())

    @unittest.skipUnless(sys.platform == "linux", "requires the Linux cleanup worker")
    def test_the_worker_applies_only_with_the_invoking_user_ids(self) -> None:
        cleanup = cleanup_old_folders
        uid, gid = os.geteuid(), os.getegid()
        review = cleanup.HostList(cleanup.Machine.NATEDEV, ())
        bounds = cleanup.CleanupBounds(self.home, self.build, frozenset(), self.gate)

        def apply_as_invoker(_review: cleanup_old_folders.HostList, _bounds: cleanup_old_folders.CleanupBounds, *,
                             mode: cleanup_old_folders.CleanupMode = cleanup_old_folders.CleanupMode.STANDARD) -> int:
            self.assertEqual((os.geteuid(), os.getegid()), (uid, gid))
            self.assertIs(mode, cleanup_old_folders.CleanupMode.STANDARD)
            return 0

        for worker_uid, expected in ((0, "refused"), (uid, "applied")):
            with (self.subTest(worker_uid=worker_uid),
                  mock.patch.dict(os.environ, {"SUDO_UID": str(uid), "SUDO_GID": str(gid)}),
                  mock.patch.object(os, "geteuid", return_value=worker_uid),
                  mock.patch.object(os, "getegid", return_value=gid),
                  mock.patch.object(cleanup, "review_local", return_value=(review, bounds)) as inspect,
                  mock.patch.object(cleanup, "apply", side_effect=apply_as_invoker) as apply):
                outgoing = io.StringIO()
                channel = cleanup.WorkerChannel(io.StringIO('{"kind":"apply"}\n'), outgoing)
                if expected == "refused":
                    with self.assertRaisesRegex(ValueError, "unprivileged"):
                        _ = cleanup.serve_masked_cleanup(channel, self.build, frozenset())
                    inspect.assert_not_called()
                    apply.assert_not_called()
                else:
                    self.assertEqual(cleanup.serve_masked_cleanup(channel, self.build, frozenset()), 0)
                    self.assertIn('"kind": "applied", "status": 0', outgoing.getvalue())
                    apply.assert_called_once_with(review, bounds)

    def test_root_without_a_sudo_user_stops_before_review(self) -> None:
        cleanup = cleanup_old_folders
        with (mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(os, "geteuid", return_value=0),
              mock.patch.object(cleanup, "protected_mounts") as mounts,
              mock.patch.object(cleanup, "run_masked_cleanup", side_effect=ValueError("started a worker")),
              mock.patch.object(cleanup, "review_local") as inspect, redirect_stderr(io.StringIO()) as errors):
            self.assertEqual(cleanup.run_cli(("--host-list",)), 1)
        self.assertIn("SUDO_UID", errors.getvalue())
        mounts.assert_not_called()
        inspect.assert_not_called()

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

    def test_gate_rows_check_leases_and_recovery_before_eligibility(self) -> None:
        self.write_settings(str(self.home / "missing"))
        dead = self.candidate("t-dead", home=self.gate)
        live = self.candidate("t-live", home=self.gate)
        _ = self.candidate("mg-unleased", home=self.gate)
        recovery = self.candidate("gate-recovery-record", home=self.gate)
        _ = (recovery / "run.json").write_text("{}\n")
        for path, record in ((dead, {"pid": 42, "process_start": GATE_LEASE_START}), (live, {"pid": 43})):
            _ = build_folders.lease_path(path).write_text(json.dumps(record))
            path.with_suffix(".lock").touch()

        def lease_owner(pid: int) -> process_start.Started | process_start.Gone:
            self.assertIn(pid, (42, 43))
            return process_start.Gone() if pid == 42 else process_start.Started(REUSED_GATE_LEASE_START)

        with mock.patch.object(process_start, "process_start", side_effect=lease_owner):
            status, output, errors = self.command("--host-list")
            self.assertEqual((status, errors), (0, ""))
            rows = {line.split("\t")[1]: line.split("\t")[3] for line in output.splitlines()
                    if line.startswith("natedev\t")}
            self.assertEqual({place for place, result in rows.items() if result.startswith("would remove if named")},
                             {repr(str(dead)), repr(str(self.gate / "mg-unleased"))})
            self.assertIn(f"lease=dead (PID 42 start {GATE_LEASE_START!r})", rows[repr(str(dead))])
            self.assertIn("lease=alive (PID 43)", rows[repr(str(live))])
            self.assertIn("lease PID 43 is still running", rows[repr(str(live))])
            self.assertIn("gate recovery record", rows[repr(str(recovery))])

        self.write_settings(f"{self.gate}/t-*, {recovery}")
        with mock.patch.object(process_start, "process_start",
                               return_value=process_start.Started(REUSED_GATE_LEASE_START)):
            status, output, errors = self.command("--host-list", "--json")
        self.assertEqual((status, errors), (0, ""))
        record = cast(cleanup_old_folders.HostRecord, json.loads(output))
        self.assertEqual([row["path"] for row in record["rows"] if row["fingerprint"]], [str(dead)])
        with mock.patch.object(process_start, "process_start", return_value=process_start.Unreadable("denied")):
            status, output, errors = self.command("--host-list")
        self.assertEqual((status, errors), (0, ""))
        self.assertIn("cannot verify lease PID 42", output)
        self.assertNotRegex(output, rf"{str(dead)!r}\t[0-9]+\twould remove")

    def test_gate_rows_keep_git_repositories_by_name(self) -> None:
        marker_folder = self.candidate("t-directory", home=self.gate)
        (marker_folder / ".git").mkdir()
        marker_file = self.candidate("t-file", home=self.gate)
        _ = (marker_file / ".git").write_text("invalid Git pointer\n")
        module = self.candidate("targeted-test-module/child/.git/modules/x", home=self.gate)
        _ = (module / "payload").write_bytes(b"x" * (2 << 20))
        bare = self.candidate("mg-bare", home=self.gate)
        (bare / "HEAD").touch()
        (bare / "refs").mkdir()
        (bare / "objects").mkdir()
        self.write_settings(str(marker_file))

        status, output, errors = self.command("--host-list")
        self.assertEqual((status, errors), (0, ""))
        for path in (marker_folder, marker_file, module.parents[3], bare):
            self.assertRegex(output, rf"{str(path)!r}\t[0-9]+\tholds a Git repository; manual review")
        self.assertIn("kept (holds a Git repository; manual review): 4 (0.002 GiB)", output)

    def gate_checkout_to_move(self, name: str = "t-checkout") -> Path:
        path = self.candidate(name, home=self.gate)
        metadata = path / ".git"
        metadata.mkdir()
        _ = (metadata / "HEAD").write_text("ref: refs/heads/main\n")
        self.write_settings(str(path))
        return path

    def test_gate_move_keeps_linked_worktrees_at_any_depth(self) -> None:
        cleanup = cleanup_old_folders
        linked = self.candidate("t-linked", home=self.gate)
        nested = self.gate_checkout_to_move("t-nested-linked")
        (nested / "child").mkdir()
        for metadata in (linked / ".git", nested / "child/.git"):
            _ = metadata.write_text("gitdir: /fixture/git/worktrees/linked\n")
        self.write_settings(f"{linked} {nested}")
        review, _bounds = cleanup.review_local(cleanup.Machine.NATEDEV, mode=cleanup.CleanupMode.MOVE_GATE_GIT)
        rows = {row.path: row.outcome for row in review.rows}
        for path in (linked, nested):
            self.assertEqual(rows[path], removal_guard.Kept("linked worktree; its registration is elsewhere"))

    def test_gate_move_keeps_a_nested_clone_with_alternates(self) -> None:
        cleanup = cleanup_old_folders
        path = self.candidate("t-shared", home=self.gate)
        objects = path / "borrower/.git/objects"
        info = objects / "info"
        info.mkdir(parents=True)
        alternate = path / "store/objects"
        alternate.mkdir(parents=True)
        (alternate.parent / "HEAD").touch()
        (alternate.parent / "refs").mkdir()
        alternates = info / "alternates"
        _ = alternates.write_text(f"{alternate}\n")
        self.write_settings(str(path))
        mode = cleanup.CleanupMode.MOVE_GATE_GIT
        review, bounds = cleanup.review_local(cleanup.Machine.NATEDEV, mode=mode)
        row = next(row for row in review.rows if row.path == path)
        self.assertEqual(row.outcome, removal_guard.Kept("Git alternates; manual review"))
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cleanup.apply(review, bounds, mode=mode), 0, output.getvalue())
        self.assertEqual(alternates.read_text(), f"{alternate}\n")
        self.assertTrue(alternate.is_dir())

    def test_gate_move_keeps_a_common_directory_with_alternates_and_no_head(self) -> None:
        cleanup = cleanup_old_folders
        path = self.gate_checkout_to_move()
        plain = self.candidate("t-common", home=self.gate)
        for folder in (path, plain):
            alternates = folder / "nested/common/objects/info/alternates"
            alternates.parent.mkdir(parents=True)
            _ = alternates.write_text("/fixture/objects\n")
        self.write_settings(f"{path} {plain}")
        for mode in (cleanup.CleanupMode.MOVE_GATE_GIT, cleanup.CleanupMode.STANDARD):
            with self.subTest(mode=mode):
                review, _bounds = cleanup.review_local(cleanup.Machine.NATEDEV, mode=mode)
                rows = {row.path: row.outcome for row in review.rows}
                for folder in (path, plain):
                    self.assertEqual(rows[folder], removal_guard.Kept("Git alternates; manual review"))

    @unittest.skipUnless(sys.platform == "linux", "requires the Linux cleanup worker")
    def test_gate_move_requires_the_callers_ownership_at_listing_and_removal(self) -> None:
        cleanup = cleanup_old_folders
        path = self.gate_checkout_to_move()
        target = self.build / "claude/resources/shm-moved" / path.name
        target.parent.mkdir(parents=True)
        mode = cleanup.CleanupMode.MOVE_GATE_GIT
        review, bounds = cleanup.review_local(cleanup.Machine.NATEDEV, mode=mode)
        self.assertIsInstance(next(row.outcome for row in review.rows if row.path == path), cleanup.WouldMove)

        uid, gid = os.geteuid(), os.getegid()
        with (mock.patch.dict(os.environ, {"SUDO_UID": str(uid + 1), "SUDO_GID": str(gid)}),
              mock.patch.object(cleanup, "review_local") as inspect):
            channel = cleanup.WorkerChannel(io.StringIO(), io.StringIO())
            with self.assertRaisesRegex(ValueError, "must run as the sudo caller"):
                _ = cleanup.serve_masked_cleanup(channel, self.build, frozenset(), mode=mode)
            inspect.assert_not_called()

        with mock.patch.object(os, "geteuid", return_value=os.geteuid() + 1):
            other_owner, _bounds = cleanup.review_local(cleanup.Machine.NATEDEV, mode=mode)
        self.assertEqual(next(row.outcome for row in other_owner.rows if row.path == path),
                         removal_guard.Kept("other user's files; live use not provable"))

        fstat = os.fstat
        parent_info = target.parent.stat()

        def other_parent_owner(fd: int) -> os.stat_result:
            info = fstat(fd)
            if os.path.samestat(info, parent_info):
                fields = list(info)
                fields[4] = info.st_uid + 1
                return os.stat_result(fields)
            return info

        with (mock.patch.object(os, "fstat", side_effect=other_parent_owner),
              redirect_stdout(io.StringIO()) as output):
            wrong_parent, _bounds = cleanup.review_local(cleanup.Machine.NATEDEV, mode=mode)
            self.assertEqual(next(row.outcome for row in wrong_parent.rows if row.path == path),
                             removal_guard.Kept("the move target parent belongs to another user"))
            self.assertEqual(cleanup.apply(review, bounds, mode=mode), 1)
        self.assertIn("the move target parent belongs to another user", output.getvalue())
        self.assertEqual((path / "payload").read_text(), "old content\n")
        self.assertEqual(list(target.parent.iterdir()), [])

    @unittest.skipUnless(sys.platform == "linux", "requires the Linux cleanup worker")
    def test_gate_move_keeps_the_source_when_the_verified_copy_changes_before_publication(self) -> None:
        cleanup = cleanup_old_folders
        path = self.gate_checkout_to_move()
        target = self.build / "claude/resources/shm-moved" / path.name
        mode = cleanup.CleanupMode.MOVE_GATE_GIT
        review, bounds = cleanup.review_local(cleanup.Machine.NATEDEV, mode=mode)
        verify = cleanup.verify_gate_copy
        publication_states: list[bool] = []

        def change_after_verification(
                checked: cleanup_old_folders.CheckedCleanup, source: int, destination: int, target_parent: int,
                entries: dict[Path, cleanup_old_folders.CleanupEntry],
                sidecars: cleanup_old_folders.WorktreeSidecars, *, target_path: Path
                ) -> cleanup_old_folders.VerifiedGateCopy:
            publication_states.append(target.exists())
            verified = verify(checked, source, destination, target_parent, entries, sidecars, target_path=target_path)
            if len(publication_states) == 1:
                self.assertEqual(os.fstat(target_parent).st_mode & 0o777, 0o700)
                fd = os.open("payload", os.O_WRONLY | os.O_TRUNC, dir_fd=destination)
                with os.fdopen(fd, "w") as payload:
                    _ = payload.write("changed after verification\n")
            return verified

        with (mock.patch.object(cleanup, "verify_gate_copy", side_effect=change_after_verification),
              redirect_stdout(io.StringIO()) as output):
            self.assertEqual(cleanup.apply(review, bounds, mode=mode), 1, output.getvalue())
        self.assertEqual(publication_states, [False, True])
        self.assertIn("copy differs", output.getvalue())
        self.assertEqual((path / "payload").read_text(), "old content\n")
        self.assertEqual((target / "payload").read_text(), "changed after verification\n")

    @unittest.skipUnless(sys.platform == "linux", "requires the Linux cleanup worker")
    def test_gate_move_keeps_the_source_when_a_published_file_changes_during_hashing(self) -> None:
        cleanup = cleanup_old_folders
        path = self.gate_checkout_to_move()
        target = self.build / "claude/resources/shm-moved" / path.name
        (path / "z-last").touch()
        mode = cleanup.CleanupMode.MOVE_GATE_GIT
        review, bounds = cleanup.review_local(cleanup.Machine.NATEDEV, mode=mode)
        read_entry = session_move.read_entry

        def change_after_hash(parent: int, relative: Path, info: os.stat_result,
                              kind: session_move.HashedEntry, mount: int) -> session_move.TreeEntry:
            result = read_entry(parent, relative, info, kind, mount)
            if relative.name == "z-last" and target.exists() and os.path.samestat(os.fstat(parent), target.stat()):
                _ = (target / "payload").write_text("")
            return result

        with (mock.patch.object(session_move, "read_entry", side_effect=change_after_hash),
              redirect_stdout(io.StringIO()) as output):
            self.assertEqual(cleanup.apply(review, bounds, mode=mode), 1, output.getvalue())
        self.assertIn("the published copy changed", output.getvalue())
        self.assertEqual((path / "payload").read_text(), "old content\n")
        self.assertEqual((target / "payload").read_text(), "")

    @unittest.skipUnless(sys.platform == "linux", "requires the Linux cleanup worker")
    def test_gate_move_keeps_source_when_final_scan_removes_copied_lease(self) -> None:
        cleanup = cleanup_old_folders
        path = self.gate_checkout_to_move()
        target = self.build / "claude/resources/shm-moved" / path.name
        path.with_name(path.name + ".lock").touch()
        source_lease = build_folders.lease_path(path)
        copied_lease = build_folders.lease_path(target)
        lease_contents = '{"pid": 42, "process_start": "123"}'
        _ = source_lease.write_text(lease_contents)
        mode = cleanup.CleanupMode.MOVE_GATE_GIT
        scan = cleanup.require_no_process_use

        def remove_copied_lease(
                scanned_path: Path, fds: Sequence[int], *, sidecars: tuple[Path, ...],
                gate_root: Path | cleanup_old_folders.GateRootNotSelected = cleanup.GateRootNotSelected.NOT_SELECTED,
        ) -> None:
            if scanned_path == path and gate_root == self.gate and target.is_dir():
                copied_lease.unlink()
            scan(scanned_path, fds, sidecars=sidecars, gate_root=gate_root)

        with (mock.patch.object(process_start, "process_start", return_value=process_start.Gone()),
              mock.patch.object(cleanup, "require_no_process_use", side_effect=remove_copied_lease),
              redirect_stdout(io.StringIO()) as output):
            review, bounds = cleanup.review_local(cleanup.Machine.NATEDEV, mode=mode)
            self.assertEqual(cleanup.apply(review, bounds, mode=mode), 1, output.getvalue())
        self.assertIn("the published copy changed during verification", output.getvalue())
        self.assertEqual((path / "payload").read_text(), "old content\n")
        self.assertEqual(source_lease.read_text(), lease_contents)
        self.assertFalse(copied_lease.exists())

    @unittest.skipUnless(sys.platform == "linux", "requires the Linux cleanup worker")
    def test_gate_move_rolls_back_published_paths_when_a_sidecar_rename_fails(self) -> None:
        cleanup = cleanup_old_folders
        path = self.gate_checkout_to_move()
        target = self.build / "claude/resources/shm-moved" / path.name
        lock = path.with_name(path.name + ".lock")
        lock.touch()
        source_lease = build_folders.lease_path(path)
        _ = source_lease.write_text('{"pid": 42, "process_start": "123"}')
        mode = cleanup.CleanupMode.MOVE_GATE_GIT
        rename = session_move.rename_exclusive
        target_present: list[bool] = []

        def refuse_lock(source: int, name: str, parent: int, destination: str) -> None:
            if name == lock.name and os.path.samestat(os.fstat(parent), target.parent.stat()):
                target_present.append(target.is_dir())
                raise OSError(errno.EIO, "fixture cannot publish lock")
            rename(source, name, parent, destination)

        with (mock.patch.object(process_start, "process_start", return_value=process_start.Gone()),
              redirect_stdout(io.StringIO()) as output):
            review, bounds = cleanup.review_local(cleanup.Machine.NATEDEV, mode=mode)
            with mock.patch.object(session_move, "rename_exclusive", side_effect=refuse_lock):
                self.assertEqual(cleanup.apply(review, bounds, mode=mode), 1, output.getvalue())
            self.assertIn("fixture cannot publish lock", output.getvalue())
            self.assertEqual(target_present, [True])
            for source in (path, lock, source_lease):
                self.assertTrue(source.exists(), source)
                self.assertFalse(target.with_name(source.name).exists(), source)
            retry, bounds = cleanup.review_local(cleanup.Machine.NATEDEV, mode=mode)
            self.assertEqual(cleanup.apply(retry, bounds, mode=mode), 0, output.getvalue())
        self.assertEqual((target / "payload").read_text(), "old content\n")
        self.assertFalse(path.exists())

    @unittest.skipUnless(sys.platform == "linux", "requires the Linux cleanup worker")
    def test_gate_move_can_retry_after_fsync_fails_after_a_rename(self) -> None:
        cleanup = cleanup_old_folders
        path = self.gate_checkout_to_move()
        target = self.build / "claude/resources/shm-moved" / path.name
        mode = cleanup.CleanupMode.MOVE_GATE_GIT
        review, bounds = cleanup.review_local(cleanup.Machine.NATEDEV, mode=mode)
        fsync = os.fsync
        failed = False

        def fail_after_rename(fd: int) -> None:
            nonlocal failed
            if not failed and target.is_dir() and os.path.samestat(os.fstat(fd), target.parent.stat()):
                self.assertEqual((target / "payload").read_text(), "old content\n")
                failed = True
                raise OSError(errno.EIO, "fixture cannot sync the published folder")
            fsync(fd)

        with redirect_stdout(io.StringIO()) as output:
            with mock.patch.object(os, "fsync", side_effect=fail_after_rename):
                self.assertEqual(cleanup.apply(review, bounds, mode=mode), 1, output.getvalue())
            self.assertIn("fixture cannot sync the published folder", output.getvalue())
            self.assertEqual((path / "payload").read_text(), "old content\n")
            self.assertFalse(target.exists())
            staged, = target.parent.glob("*/" + target.name)
            self.assertEqual((staged / "payload").read_text(), "old content\n")
            retry, bounds = cleanup.review_local(cleanup.Machine.NATEDEV, mode=mode)
            self.assertEqual(cleanup.apply(retry, bounds, mode=mode), 0, output.getvalue())
        self.assertEqual((target / "payload").read_text(), "old content\n")
        self.assertFalse(path.exists())

    @unittest.skipUnless(sys.platform == "linux", "requires the Linux cleanup worker")
    def test_gate_move_preserves_contents_modes_times_hard_links_and_sidecars(self) -> None:
        cleanup = cleanup_old_folders
        path = self.gate_checkout_to_move()
        payload = path / "payload"
        payload.chmod(0o640)
        os.utime(payload, ns=(1_700_000_000_000_000_000, 1_700_000_001_000_000_000))
        (path / "hard-link").hardlink_to(payload)
        source_info = payload.stat()
        sidecars = {path.with_name(path.name + ".lock"): "",
                    build_folders.lease_path(path): '{"pid": 42, "process_start": "123"}'}
        for sidecar, contents in sidecars.items():
            _ = sidecar.write_text(contents)
        mode = cleanup.CleanupMode.MOVE_GATE_GIT
        remove = removal_guard.remove_cleanup

        def remove_sidecars(checked: removal_guard.CheckedCleanupPath, approved: removal_guard.ApprovedCleanup,
                            *additional: removal_guard.CleanupTarget,
                            before_delete: Callable[[], None] | None = None) -> None:
            self.assertFalse(path.exists())
            self.assertEqual({checked.path, *(entry.checked.path for entry in additional)}, set(sidecars))
            remove(checked, approved, *additional, before_delete=before_delete)

        with (mock.patch.object(process_start, "process_start", return_value=process_start.Gone()),
              mock.patch.object(removal_guard, "remove_cleanup", side_effect=remove_sidecars) as finish,
              redirect_stdout(io.StringIO()) as output):
            review, bounds = cleanup.review_local(cleanup.Machine.NATEDEV, mode=mode)
            self.assertEqual(cleanup.apply(review, bounds, mode=mode), 0, output.getvalue())
        finish.assert_called_once()
        target = self.build / "claude/resources/shm-moved" / path.name
        self.assertFalse(path.exists())
        self.assertEqual((target / ".git/HEAD").read_text(), "ref: refs/heads/main\n")
        self.assertEqual((target / "payload").read_text(), "old content\n")
        target_info = (target / "payload").stat()
        self.assertTrue(os.path.samestat(target_info, (target / "hard-link").stat()))
        self.assertEqual((target_info.st_mode, target_info.st_mtime_ns), (source_info.st_mode, source_info.st_mtime_ns))
        for sidecar, contents in sidecars.items():
            self.assertFalse(sidecar.exists())
            self.assertEqual(target.with_name(sidecar.name).read_text(), contents)
        self.assertEqual(set(target.parent.iterdir()), {target, *(target.with_name(sidecar.name) for sidecar in sidecars)})
        self.assertIn(f"moved {str(path)!r} to {str(target)!r}", output.getvalue())

    @unittest.skipUnless(sys.platform == "linux", "requires the Linux cleanup worker")
    def test_gate_move_keeps_source_and_copy_after_same_size_content_mismatch(self) -> None:
        cleanup = cleanup_old_folders
        path = self.gate_checkout_to_move()
        target = self.build / "claude/resources/shm-moved" / path.name
        lock = path.with_name(path.name + ".lock")
        lock.touch()
        mode = cleanup.CleanupMode.MOVE_GATE_GIT
        review, bounds = cleanup.review_local(cleanup.Machine.NATEDEV, mode=mode)
        copy = cleanup.copy_gate_tree

        def corrupt(source: int, destination: int) -> None:
            copy(source, destination)
            fd = os.open("payload", os.O_WRONLY, dir_fd=destination)
            with os.fdopen(fd, "wb") as payload:
                info = os.fstat(fd)
                _ = payload.write(b"new content\n")
                payload.flush()
                os.utime(fd, ns=(info.st_atime_ns, info.st_mtime_ns))

        with (mock.patch.object(cleanup, "copy_gate_tree", side_effect=corrupt),
              redirect_stdout(io.StringIO()) as output):
            self.assertEqual(cleanup.apply(review, bounds, mode=mode), 1)
        self.assertIn("copy differs", output.getvalue())
        self.assertEqual((path / "payload").read_text(), "old content\n")
        self.assertFalse(target.exists())
        staged, = target.parent.glob("*/" + target.name)
        self.assertEqual((staged / "payload").read_text(), "new content\n")
        self.assertTrue(lock.is_file())
        self.assertTrue(staged.with_name(lock.name).is_file())

    def test_gate_move_keeps_git_rows_with_recovery_use_or_incomplete_scan(self) -> None:
        cleanup = cleanup_old_folders
        recovery = self.gate_checkout_to_move("t-recovery")
        used = self.gate_checkout_to_move("t-used")
        unreadable = self.gate_checkout_to_move("t-unreadable")
        (recovery / ".git/run.json").touch()
        mode = cleanup.CleanupMode.MOVE_GATE_GIT
        for evidence, expected in (
            (cleanup.CleanupProcessReview(paths=((str(used), 42, tmp_sweep.WORKS_IN),)), "pid 42 works in it"),
            (cleanup.CleanupProcessReview(path_failure=removal_guard.Kept("open file scan is incomplete")),
             "open file scan is incomplete"),
        ):
            with (self.subTest(expected=expected), mock.patch.object(cleanup, "process_uses", return_value=evidence)):
                review, _bounds = cleanup.review_local(cleanup.Machine.NATEDEV, mode=mode)
                rows = {row.path: row.outcome for row in review.rows}
                self.assertEqual(rows[recovery], removal_guard.Kept("gate recovery record"))
                self.assertEqual(rows[used if evidence.paths else unreadable], removal_guard.Kept(expected))
        self.assertFalse((self.build / "claude/resources/shm-moved").exists())

    def test_gate_move_list_names_the_target_and_writes_no_copy(self) -> None:
        cleanup = cleanup_old_folders
        path = self.gate_checkout_to_move()
        target = self.build / "claude/resources/shm-moved" / path.name
        mode = cleanup.CleanupMode.MOVE_GATE_GIT
        review, _bounds = cleanup.review_local(cleanup.Machine.NATEDEV, mode=mode)
        record = review.record()
        worker_record = cleanup.worker_review(review)
        decoded = cleanup.decode_cleanup_rows(cast(object, json.loads(json.dumps(worker_record["rows"]))), review.machine)
        self.assertEqual(decoded, review.rows)
        with redirect_stdout(io.StringIO()) as output:
            cleanup.show(review, cleanup.LocalListOnly.REQUESTED, record["digest"])
        self.assertIn(f"would move to disk at {str(target)!r}", output.getvalue())
        self.assertRegex(output.getvalue(), r"would move to disk: 1 \([0-9.]+ GiB\)")
        self.assertTrue(path.is_dir())
        self.assertFalse(target.parent.exists())
        self.assertEqual(record["rows"][0].get("move_to"), str(target))

    @unittest.skipUnless(sys.platform == "linux", "requires the Linux cleanup worker")
    def test_gate_move_keeps_the_source_when_the_target_appears_after_listing(self) -> None:
        cleanup = cleanup_old_folders
        path = self.gate_checkout_to_move()
        target = self.build / "claude/resources/shm-moved" / path.name
        mode = cleanup.CleanupMode.MOVE_GATE_GIT
        review, bounds = cleanup.review_local(cleanup.Machine.NATEDEV, mode=mode)
        target.mkdir(parents=True)
        _ = (target / "existing").write_text("keep this\n")
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cleanup.apply(review, bounds, mode=mode), 1)
        self.assertIn("move target already exists", output.getvalue())
        self.assertEqual((target / "existing").read_text(), "keep this\n")
        self.assertEqual((path / "payload").read_text(), "old content\n")

    @unittest.skipUnless(sys.platform == "linux", "requires the Linux cleanup worker")
    def test_gate_move_moves_bare_and_nested_git_and_keeps_standard_cleanup_rows(self) -> None:
        cleanup = cleanup_old_folders
        bare = self.candidate("t-bare", home=self.gate)
        (bare / "refs").mkdir()
        (bare / "objects").mkdir()
        nested = self.candidate("t-nested", home=self.gate)
        (nested / "child/.git").mkdir(parents=True)
        for head in (bare / "HEAD", nested / "child/.git/HEAD"):
            _ = head.write_text("ref: refs/heads/main\n")
        ordinary = self.candidate()
        self.write_settings(str(ordinary))
        preview, _bounds = cleanup.review_local(cleanup.Machine.NATEDEV)
        self.assertIsInstance(next(row.outcome for row in preview.rows if row.path == ordinary), cleanup.WouldRemove)
        mode = cleanup.CleanupMode.MOVE_GATE_GIT
        moves, bounds = cleanup.review_local(cleanup.Machine.NATEDEV, mode=mode)
        rows = {row.path: row.outcome for row in moves.rows}
        for path in (bare, nested):
            self.assertIsInstance(rows[path], cleanup.WouldMove)
        self.assertEqual(rows[ordinary], removal_guard.Kept("use the standard cleanup list to remove this folder"))
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cleanup.apply(moves, bounds, mode=mode), 0, output.getvalue())
        destination = self.build / "claude/resources/shm-moved"
        for path, relative in ((bare, "HEAD"), (nested, "child/.git/HEAD")):
            self.assertFalse(path.exists())
            self.assertEqual((destination / path.name / relative).read_text(), "ref: refs/heads/main\n")
            self.assertIn(f"moved {str(path)!r} to {str(destination / path.name)!r}", output.getvalue())
        self.assertTrue(ordinary.is_dir())

    @unittest.skipUnless(sys.platform == "linux", "requires the Linux cleanup worker")
    def test_gate_move_keeps_a_source_changed_after_copy_verification(self) -> None:
        cleanup = cleanup_old_folders
        path = self.gate_checkout_to_move()
        metadata = path / "nested/.git"
        metadata.mkdir(parents=True)
        head = metadata / "HEAD"
        _ = head.write_text("ref: refs/heads/main\n")
        mode = cleanup.CleanupMode.MOVE_GATE_GIT
        moves, bounds = cleanup.review_local(cleanup.Machine.NATEDEV, mode=mode)
        verify = cleanup.verify_gate_copy

        def change_source_after_verification(
                checked: cleanup_old_folders.CheckedCleanup, source: int, destination: int, target_parent: int,
                entries: dict[Path, cleanup_old_folders.CleanupEntry],
                sidecars: cleanup_old_folders.WorktreeSidecars, *, target_path: Path
                ) -> cleanup_old_folders.VerifiedGateCopy:
            verified = verify(checked, source, destination, target_parent, entries, sidecars, target_path=target_path)
            info = head.stat()
            _ = head.write_text("ref: refs/heads/next\n")
            os.utime(head, ns=(info.st_atime_ns, info.st_mtime_ns))
            return verified

        with (mock.patch.object(cleanup, "verify_gate_copy", side_effect=change_source_after_verification),
              redirect_stdout(io.StringIO()) as output):
            self.assertEqual(cleanup.apply(moves, bounds, mode=mode), 1, output.getvalue())
        self.assertIn("entry changed before removal", output.getvalue())
        self.assertEqual(head.read_text(), "ref: refs/heads/next\n")
        self.assertEqual((path / "payload").read_text(), "old content\n")
        target = self.build / "claude/resources/shm-moved" / path.name
        self.assertEqual((target / head.relative_to(path)).read_text(), "ref: refs/heads/main\n")

    @unittest.skipUnless(sys.platform == "linux", "requires the Linux cleanup worker")
    def test_gate_move_keeps_a_source_opened_during_the_removal_walk(self) -> None:
        cleanup = cleanup_old_folders
        path = self.gate_checkout_to_move()
        payload = path / "payload"
        payload_info = payload.stat()
        target = self.build / "claude/resources/shm-moved" / path.name
        proc, process = self.proc_fixture()
        (process / "cwd").symlink_to(self.gate)
        (process / "root").symlink_to(self.gate)
        (process / "maps").touch()
        opened = process / "fd/5"
        mount_id = removal_guard.mount_id

        def open_during_walk(fd: int) -> str:
            if target.is_dir() and os.path.samestat(os.fstat(fd), payload_info) and not opened.is_symlink():
                opened.symlink_to(payload)
            return mount_id(fd)

        mode = cleanup.CleanupMode.MOVE_GATE_GIT
        with mock.patch.object(cleanup, "PROC_ROOT", str(proc)):
            review, bounds = cleanup.review_local(cleanup.Machine.NATEDEV, mode=mode)
            self.assertIsInstance(next(row.outcome for row in review.rows if row.path == path), cleanup.WouldMove)
            with (mock.patch.object(removal_guard, "mount_id", side_effect=open_during_walk),
                  redirect_stdout(io.StringIO()) as output):
                self.assertEqual(cleanup.apply(review, bounds, mode=mode), 1, output.getvalue())
        self.assertIn("pid 42 holds a file open", output.getvalue())
        self.assertEqual(payload.read_text(), "old content\n")
        self.assertEqual((target / payload.name).read_text(), "old content\n")

    def test_gate_listing_does_not_start_git(self) -> None:
        source, checkout = self.git_fixture()
        gate_checkout = self.gate / "mg-checkout"
        _ = self.git(source, "worktree", "move", str(checkout), str(gate_checkout))
        fixture = self.candidate("t-checkout", home=self.gate)
        _ = self.git(fixture, "init", "-q")
        self.write_settings(str(gate_checkout))

        with mock.patch.object(subprocess, "Popen", side_effect=AssertionError("gate list started a process")) as run:
            status, output, errors = self.command("--host-list")
        self.assertEqual((status, errors), (0, ""))
        run.assert_not_called()
        for path in (fixture, gate_checkout):
            self.assertRegex(output, rf"{str(path)!r}\t[0-9]+\tholds a Git repository; manual review")

    def test_other_rows_keep_git_data_in_the_gate_root_without_starting_git(self) -> None:
        source, worktree = self.git_fixture()
        _ = self.git(source, "worktree", "move", str(worktree), str(self.home / "linked"))
        worktree = self.home / "linked"
        origin_clone = self.home / "origin-clone"
        alternate_clone = self.home / "alternate-clone"
        _ = self.git(self.home, "clone", "-q", str(source), str(origin_clone))
        _ = self.git(self.home, "clone", "-q", "--shared", str(source), str(alternate_clone))
        gate_repo = self.gate / "mg-source"
        _ = source.rename(gate_repo)
        bare = self.gate / "mg-origin #1"
        _ = self.git(self.home, "clone", "-q", "--bare", str(gate_repo), str(bare))
        _ = self.git(origin_clone, "remote", "set-url", "origin", str(bare))
        gate_alias = self.home / "gate-alias"
        gate_alias.symlink_to(self.gate)
        _ = self.git(alternate_clone, "remote", "set-url", "origin", "https://example.invalid/source")
        _ = (alternate_clone / ".git/objects/info/alternates").write_text(
            f"{gate_alias / gate_repo.name}/.git/objects\n")
        _ = (worktree / ".git").write_text(f"gitdir: {gate_repo}/.git/worktrees/old\n")
        rows = (worktree, origin_clone, alternate_clone)
        self.write_settings(", ".join(map(str, rows)))

        with mock.patch.object(subprocess, "Popen", side_effect=AssertionError("cleanup started Git")) as run:
            status, output, errors = self.command("--host-list")
        self.assertEqual((status, errors), (0, ""))
        run.assert_not_called()
        for path in rows:
            self.assertRegex(output, rf"{str(path)!r}\t[0-9]+\tGit data in the gate root; manual review")

    def test_git_storage_on_the_gate_device_keeps_the_row_without_starting_git(self) -> None:
        source, linked = self.git_fixture()
        clone = self.home / "clone"
        _ = self.git(self.home, "clone", "-q", "--shared", str(source), str(clone))
        registration = git_files.at(linked).git_dir
        read_status = Path.lstat

        for path, storage in ((linked, registration), (clone, clone / ".git"),
                              (clone, source), (clone, source / ".git/objects")):
            with self.subTest(storage=storage):
                self.write_settings(str(path))

                def read_device(place: Path) -> os.stat_result:
                    info = read_status(place)
                    if place == storage:
                        fields = list(info)
                        fields[2] = FIXTURE_GATE_DEVICE
                        return os.stat_result(fields)
                    return info

                with (mock.patch.object(Path, "lstat", read_device),
                      mock.patch.object(subprocess, "Popen", side_effect=AssertionError("cleanup started Git")) as run):
                    status, output, errors = self.command("--host-list")
                self.assertEqual((status, errors), (0, ""))
                run.assert_not_called()
                self.assertRegex(output, rf"{str(path)!r}\t[0-9]+\tGit data in the gate root; manual review")

    @unittest.skipUnless(sys.platform == "linux", "requires Linux process paths")
    def test_gate_rows_are_kept_when_a_process_uses_the_device_through_an_alias(self) -> None:
        first, second = (self.candidate(name, home=self.gate) for name in ("t-first", "mg-second"))
        self.write_settings(f"{self.gate}/*")
        proc, pid = self.proc_fixture()
        thread = pid / "task/43"
        (thread / "fd").mkdir(parents=True)
        status_text = (pid / "status").read_text()
        _ = (thread / "status").write_text(status_text)
        _ = (pid / "status").write_text(status_text.replace("Threads: 1", "Threads: 2"))
        for task in (pid, thread):
            (task / "root").symlink_to(self.gate)
            (task / "cwd").symlink_to(self.gate)
        alternate = self.candidate("mount-alias")
        payload = alternate / "payload"
        maps = pid / "maps"
        maps.touch()
        map_files = pid / "map_files"
        map_files.mkdir()
        mapped = map_files / "1000-2000"
        descriptor = thread / "fd/5"
        cwd = thread / "cwd"
        info = payload.stat()
        mapping = f"{mapped.name} r--p 00000000 {os.major(info.st_dev):x}:{os.minor(info.st_dev):x} {info.st_ino}"
        for reference, target in ((descriptor, payload), (cwd, alternate), (mapped, payload)):
            with self.subTest(reference=reference.name):
                if reference == cwd:
                    cwd.unlink()
                if reference == mapped:
                    _ = maps.write_text(f"{mapping} {payload}\n")
                reference.symlink_to(target)
                with mock.patch.object(cleanup_old_folders, "PROC_ROOT", str(proc)):
                    status, output, errors = self.command("--host-list")
                self.assertEqual((status, errors), (0, ""))
                for path in (first, second):
                    self.assertRegex(output, rf"{str(path)!r}\t[0-9]+\t"
                                     + "gate root open through another path; live use not provable")
                reference.unlink()
                if reference == cwd:
                    cwd.symlink_to(self.gate)

    @unittest.skipUnless(sys.platform == "linux", "requires Linux process paths")
    def test_gate_rows_keep_process_root_and_executable_aliases(self) -> None:
        path = self.candidate("t-first", home=self.gate)
        self.write_settings(str(path))
        proc, pid = self.proc_fixture()
        alias = self.candidate("mount-alias")
        (pid / "cwd").symlink_to(self.gate)
        (pid / "maps").touch()
        root = pid / "root"
        executable = pid / "exe"

        for root_path, exe_path in ((alias, path / "payload"), (self.gate, alias / "payload")):
            with self.subTest(root=root_path, executable=exe_path):
                root.symlink_to(root_path)
                executable.symlink_to(exe_path)
                with mock.patch.object(cleanup_old_folders, "PROC_ROOT", str(proc)):
                    status, output, errors = self.command("--host-list")
                root.unlink()
                executable.unlink()
                self.assertEqual((status, errors), (0, ""))
                self.assertRegex(output, rf"{str(path)!r}\t[0-9]+\t"
                                 + "gate root open through another path; live use not provable")

    def test_an_unreadable_gate_root_keeps_one_row_and_lists_other_folders(self) -> None:
        other = self.candidate()
        cache = self.candidate("sccache", home=self.build)
        self.write_settings(str(other))
        names = os.listdir

        def read_names(path: int | str | Path) -> list[str]:
            if isinstance(path, int) and os.path.samestat(os.fstat(path), self.gate.stat()):
                raise PermissionError(errno.EACCES, "permission denied")
            return names(path)

        with mock.patch.object(os, "listdir", side_effect=read_names):
            status, output, errors = self.command("--host-list")
        self.assertEqual((status, errors), (0, ""))
        self.assertEqual(output.count(f"\t{str(self.gate)!r}\t"), 1)
        self.assertRegex(output, rf"{str(self.gate)!r}\tunknown\t.*permission denied")
        self.assertRegex(output, rf"{str(other)!r}\t[0-9]+\twould remove")
        self.assertRegex(output, rf"{str(cache)!r}\t[0-9]+\tkept: shared tool cache, list only")

    def test_gate_newest_time_includes_the_lock_and_lease(self) -> None:
        path = self.candidate("t-stamps", home=self.gate)
        self.write_settings(str(self.home / "missing"))
        lock = path.with_suffix(".lock")
        lease = build_folders.lease_path(path)
        lock.touch()
        _ = lease.write_text(json.dumps({"pid": 42}))
        for item in (path, path / "payload", lock, lease):
            os.utime(item, ns=(1_000, 1_000))
        for sidecar, newest in ((lock, 2_000), (lease, 3_000)):
            with self.subTest(sidecar=sidecar):
                os.utime(sidecar, ns=(newest, newest))
                with mock.patch.object(process_start, "process_start", return_value=process_start.Gone()):
                    status, output, errors = self.command("--host-list", "--json")
                self.assertEqual((status, errors), (0, ""))
                record = cast(cleanup_old_folders.HostRecord, json.loads(output))
                row = next(row for row in record["rows"] if row["path"] == str(path))
                assert "gate" in row
                self.assertEqual(row["gate"]["newest_ns"], newest)

    def test_gate_row_keeps_a_malformed_lease_start(self) -> None:
        path = self.candidate("t-bad-start", home=self.gate)
        _ = build_folders.lease_path(path).write_text(json.dumps({"pid": 42, "process_start": "invalid"}))
        path.with_suffix(".lock").touch()
        self.write_settings(str(self.home / "missing"))

        with mock.patch.object(process_start, "process_start", return_value=process_start.Started(GATE_LEASE_START)):
            status, output, errors = self.command("--host-list")
        self.assertEqual((status, errors), (0, ""))
        self.assertRegex(output, rf"{str(path)!r}\t[0-9]+\tcannot verify the start time for lease PID 42")

    def test_gate_row_keeps_an_entry_owned_by_another_user(self) -> None:
        path = self.candidate("mg-other-owner/nested", home=self.gate)
        foreign = (path / "payload").stat()
        stamp = cleanup_old_folders.entry_stamp
        self.write_settings(str(self.home / "missing"))

        def foreign_owner(info: os.stat_result) -> cleanup_old_folders.EntryStamp:
            record = stamp(info)
            if os.path.samestat(info, foreign):
                return record._replace(uid=os.geteuid() + 1)
            return record

        with mock.patch.object(cleanup_old_folders, "entry_stamp", side_effect=foreign_owner):
            status, output, errors = self.command("--host-list")
        self.assertEqual((status, errors), (0, ""))
        self.assertRegex(output, rf"{str(path.parent)!r}\t[0-9]+\tother user's files; live use not provable")

    def test_gate_rows_keep_a_path_open_in_a_nonleader_thread(self) -> None:
        gate = self.candidate("mg-thread", home=self.gate)
        self.write_settings(str(gate))
        proc, pid = self.proc_fixture()
        uid = os.geteuid()
        status_text = f"Uid: {uid} {uid} {uid} {uid}\nState: S\n"
        _ = (pid / "status").write_text(status_text + "Threads: 2\n")
        thread = pid / "task/43"
        (thread / "fd").mkdir(parents=True)
        for task in (pid, thread):
            (task / "root").symlink_to(self.gate)
            (task / "cwd").symlink_to(self.gate)
        _ = (thread / "status").write_text(status_text)
        _ = (pid / "maps").write_text("")
        (thread / "fd/5").symlink_to(gate / "payload")
        with mock.patch.object(cleanup_old_folders, "PROC_ROOT", str(proc)):
            status, output, errors = self.command("--host-list")
        self.assertEqual((status, errors), (0, ""))
        row = next(line for line in output.splitlines() if line.startswith(f"natedev\t{str(gate)!r}\t"))
        self.assertRegex(row, r"\t[0-9]+\tpid 42 holds a file open")
        self.assertIn("use=pid 42 holds a file open", row)

    def test_gate_summary_counts_measured_bytes_by_kind_and_verdict(self) -> None:
        large = self.candidate("t-large", home=self.gate)
        _ = (large / "payload").write_bytes(b"x" * (2 << 20))
        targeted = self.candidate("targeted-test-small", home=self.gate)
        unknown = self.candidate("unknown-maker", home=self.gate)
        nested_record = self.candidate("mg-holds-record/archive", home=self.gate) / "run.json"
        nested_record.touch()
        alias = self.gate / "mg-link"
        alias.symlink_to(large)
        orphan = self.gate / "fc-orphan"
        build_folders.lease_path(orphan).touch()
        self.write_settings(str(self.home / "missing"))
        os.utime(large, (1_700_000_000, 1_700_000_000))
        os.utime(large / "payload", (1_700_000_001, 1_700_000_001))
        status, output, errors = self.command("--host-list")
        self.assertEqual((status, errors), (0, ""))
        self.assertIn("newest=2023-11-14T22:13:21Z", output)
        self.assertRegex(output, rf"{str(unknown)!r}\t[0-9]+\tunknown gate entry type")
        self.assertRegex(output, rf"{str(alias)!r}\t[0-9]+\tgate entry is a link")
        self.assertRegex(output, rf"{str(orphan)!r}\t[0-9]+\torphan gate sidecars")
        self.assertRegex(output, rf"{str(nested_record.parents[1])!r}\t[0-9]+\tgate recovery record")
        rows = [line.split("\t")[1] for line in output.splitlines() if line.startswith("natedev\t")]
        self.assertEqual(rows[:2], [repr(str(large)), repr(str(targeted))])
        self.assertIn("Gate rows on natedev: t and targeted-test would remove if named: 2 (0.002 GiB); "
                      + "t would remove if named: 1 (0.002 GiB); targeted-test would remove if named: 1 (0.000 GiB); "
                      + "fc kept: 1 (0.000 GiB); mg kept: 2 (0.000 GiB); unknown kept: 1 (0.000 GiB)", output)

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
                self.assertEqual(self.command("--go", self.list_digest(output))[0], 0)
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
        approval = self.list_digest(output)
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
            approval = self.list_digest(output)
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

    def test_clone_review_skips_a_gate_pointer_only_if_its_git_directory_is_missing(self) -> None:
        _, clone = self.clone_fixture()
        fixture = self.candidate("t-fixture", home=self.gate)
        pointer = fixture / ".git"
        _ = pointer.write_text(f"gitdir: {self.home / 'missing'}\n")

        status, output, errors = self.command("--host-list")
        self.assertEqual((status, errors), (0, ""))
        self.assertRegex(output, rf"{str(clone)!r}\t[0-9]+\twould remove")
        self.assertRegex(output, rf"{str(fixture)!r}\t[0-9]+\tholds a Git repository; manual review")

        broken_registration = self.home / "source/.git/worktrees/no-backlink"
        broken_registration.mkdir(parents=True)
        _ = pointer.write_text(f"gitdir: {broken_registration}\n")
        status, output, errors = self.command("--host-list")
        self.assertEqual((status, errors), (0, ""))
        self.assertNotRegex(output, rf"{str(clone)!r}\t[0-9]+\twould remove")
        self.assertIn(str(broken_registration / "gitdir"), output)

    def test_orphan_row_does_not_block_clone_review_or_removal(self) -> None:
        _, path = self.clone_fixture()
        orphan = self.home / "worktrees/absent"
        orphan.parent.mkdir()
        lock = orphan.with_name("absent.lock")
        lock.touch()
        storage_path = cleanup_old_folders.CleanupBounds.storage_path

        def check_storage(bounds: cleanup_old_folders.CleanupBounds, place: Path, *,
                          follow_links: bool = True, exclude_gate: bool = False) -> Path:
            self.assertNotEqual(place, orphan, "an absent base must never be resolved")
            return storage_path(bounds, place, follow_links=follow_links, exclude_gate=exclude_gate)

        with mock.patch.object(cleanup_old_folders.CleanupBounds, "storage_path", check_storage):
            status, output, errors = self.command("--host-list")
            self.assertEqual((status, errors), (0, ""))
            self.assertRegex(output, rf"{str(path)!r}\t[0-9]+\twould remove")
            self.assertRegex(output, rf"{str(orphan)!r}\t[0-9]+\tlist only: no setting names it")
            approval = self.list_digest(output)
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
        approval = self.list_digest(preview[1])
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
        approval = self.list_digest(preview[1])
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
        approval = self.list_digest(preview[1])
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
            approval = self.list_digest(output)
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
        approval = self.list_digest(output)
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
            approval = self.list_digest(output)
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
            approval = self.list_digest(output)
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

        def apply_after_denial(review: cleanup_old_folders.HostList, bounds: cleanup_old_folders.CleanupBounds, *,
                               mode: cleanup_old_folders.CleanupMode = cleanup_old_folders.CleanupMode.STANDARD) -> int:
            with mock.patch.object(Path, "read_bytes", deny_reads):
                return apply(review, bounds, mode=mode)

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
            approval = self.list_digest(output)
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
            approval = self.list_digest(output)
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
            approval = self.list_digest(output)
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
            approval = self.list_digest(output)
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
        approval = self.list_digest(output)
        self.assertNotEqual(approval, mac_record["digest"])
        status, output, errors = self.command("--go", approval, runner=run)
        self.assertEqual((status, errors), (0, ""))
        self.assertEqual(visited, [cleanup_old_folders.REMOTE_COMMAND + " --json"] * 2
                         + [cleanup_old_folders.REMOTE_COMMAND + " --go " + mac_record["digest"]])
        self.assertIn(f"removed {str(path)!r}", output)
        self.assertFalse(path.exists())
        self.assertTrue((neighbour / "payload").is_file())
