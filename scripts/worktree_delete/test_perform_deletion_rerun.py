"""Check worktree removal reports and the checks before reporting."""
from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import unittest
from typing import cast, override
from unittest.mock import patch

from scripts.agents import remote as staff_remote
from scripts.lib.git_test_support import commit_repository, fixture_git_environment
from scripts.production import staff
from scripts.production.staff_test_support import fixture_member, isolated_staff_file
from scripts.production.test_production_doc import enrolled_document
from scripts.lib.py_floor_test_support import interpreter_cache_environment
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.worktree import worktrees


SCRIPTS = Path(__file__).resolve().parent
BRANCH = "unit-topic"


class DeletionRerunTests(unittest.TestCase):
    root: Path = Path()
    repo: Path = Path()
    worktree: Path = Path()
    output: Path = Path()
    environment: dict[str, str] = {}
    scripts: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.repo = self.root / "repo"
        self.worktree = self.root / "workspaces/repo/unit/topic"
        self.output = self.root / "build/repo/unit/topic"
        settings = self.root / "settings"
        settings.mkdir()
        _ = (settings / "build_folders.conf").write_text(
            f"roots.build={self.root / 'build'}\nroots.workspace={self.root / 'workspaces'}\n")
        self.scripts = self.root / "scripts/worktree_delete"
        self.scripts.mkdir(parents=True)
        nonce_dir = self.root / "confirmations"
        for name in ("record_confirmation.sh", "worktree_delete_validation.sh", "perform_deletion.sh"):
            _ = shutil.copyfile(SCRIPTS / name, self.scripts / name)
        _ = (self.scripts / "confirm_gate.sh").write_text((SCRIPTS / "confirm_gate.sh").read_text().replace(
            'WT_CONFIRM_DIR="/tmp/claude/worktree_delete"', f"WT_CONFIRM_DIR={shlex.quote(str(nonce_dir))}"))
        for name in ("lib", "worktree"):
            (self.scripts.parent / name).symlink_to(SCRIPTS.parent / name, target_is_directory=True)
        staff_file = self.root / "staff.json"
        _ = staff_file.write_text('{"schema": 2, "rows": []}')
        self.environment = {
            **fixture_git_environment(self.root),
            **interpreter_cache_environment(self.root / "cache"),
            "CONF_DIR": str(settings), "WORKTREES_STATE_DIR": str(self.root / "state"),
            "GIT_CONFIG_SYSTEM": os.devnull, "GIT_ALLOW_PROTOCOL": "file", "STAFF_FILE": str(staff_file),
        }
        remote = self.root / "repo.git"
        _ = self.git("init", "-q", "--bare", str(remote))
        _ = self.git("init", "-q", "--initial-branch=main", str(self.repo))
        _ = self.git("commit", "--allow-empty", "-qm", "fixture", directory=self.repo)
        _ = self.git("remote", "add", "origin", str(remote), directory=self.repo)
        _ = self.git("worktree", "add", "-qb", BRANCH, str(self.worktree), directory=self.repo)
        _ = self.git("push", "-q", "origin", "main", BRANCH, directory=self.repo)
        _ = self.git("remote", "set-head", "origin", "main", directory=self.repo)

        base = self.git("rev-parse", "HEAD", directory=self.repo)
        record = replace(worktrees.new_record(self.worktree, BRANCH, self.repo, base, "unit", "main"),
                         layout=worktrees.RecordedWorktreeOutput("unit", "topic", self.output))
        with patch.dict(os.environ, {"WORKTREES_STATE_DIR": self.environment["WORKTREES_STATE_DIR"]}):
            worktrees.write_record(record)
        self.output.mkdir(parents=True)
        _ = (self.output / "artifact").write_text("keep\n")
        with (self.repo / ".git/info/exclude").open("a") as exclude:
            _ = exclude.write("/target\n")
        (self.worktree / "target").symlink_to(self.output)

    def git(self, *args: str, directory: Path | None = None) -> str:
        return subprocess.run(["git", *args], cwd=directory or self.root, env=self.environment,
                              capture_output=True, text=True, check=True,
                              timeout=TEST_HANG_GUARD_SECS).stdout.strip()

    def script(self, name: str, cwd: Path, *arguments: str | Path, check: bool = False) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["bash", str(self.scripts / name), *map(str, arguments)],
                              cwd=cwd, env=self.environment, capture_output=True,
                              text=True, check=check, timeout=TEST_HANG_GUARD_SECS)

    def confirmed_preview(self, cwd: Path, path: Path) -> str:
        result = self.script("worktree_delete_validation.sh", cwd, path, check=True)
        fields = cast(dict[str, object], json.loads(result.stdout))
        preview_id = cast(str, fields["preview_id"])
        _ = self.script("record_confirmation.sh", cwd, preview_id, check=True)
        return preview_id

    def commit_topic(self) -> None:
        _ = (self.worktree / "code.txt").write_text("topic work\n")
        _ = self.git("add", "code.txt", directory=self.worktree)
        _ = self.git("commit", "-qm", "Change code.txt", directory=self.worktree)
        _ = self.git("push", "-q", "origin", BRANCH, directory=self.worktree)

    def assert_retained(self, result: subprocess.CompletedProcess[str], tip: str, *, origin: bool = True) -> None:
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stderr.splitlines(), [
            f"kept for a manual removal list: {path} (worktrees, the checkout is selected for removal)"
            for path in (self.output, self.worktree)
        ])
        self.assertEqual((self.worktree / "target").readlink(), self.output)
        self.assertEqual((self.output / "artifact").read_text(), "keep\n")
        self.assertEqual(self.git("rev-parse", f"refs/heads/{BRANCH}", directory=self.repo), tip)
        if origin:
            self.assertEqual(self.git("ls-remote", "--heads", "origin", BRANCH, directory=self.repo),
                             f"{tip}\trefs/heads/{BRANCH}")

    def test_confirmed_removal_from_a_sibling_lists_both_folders(self) -> None:
        self.commit_topic()
        tip = self.git("rev-parse", "HEAD", directory=self.worktree)
        caller = self.root / "caller"
        _ = self.git("worktree", "add", "-qb", "caller", str(caller), directory=self.repo)
        relative = Path(os.path.relpath(self.worktree, caller))
        lock, lease = self.worktree.with_suffix(".lock"), self.worktree.with_suffix(".lease.json")
        lock.touch()
        _ = lease.write_text("{}\n")
        preview_id = self.confirmed_preview(caller, relative)

        result = self.script("perform_deletion.sh", caller, relative, BRANCH, preview_id)

        self.assert_retained(result, tip)
        self.assertEqual((self.worktree / "code.txt").read_text(), "topic work\n")
        self.assertTrue(lock.is_file())
        self.assertEqual(lease.read_text(), "{}\n")
        rerun = self.script("perform_deletion.sh", caller, relative, BRANCH)
        self.assertNotEqual(rerun.returncode, 0, rerun.stdout + rerun.stderr)
        self.assertIn("no confirmation nonce", rerun.stdout)
        self.assertIn("work has not landed", rerun.stdout)
        self.assertNotIn("kept for a manual removal list", rerun.stderr)

    def test_landed_cleanup_lists_the_same_folders_on_a_rerun(self) -> None:
        self.commit_topic()
        tip = self.git("rev-parse", "HEAD", directory=self.worktree)
        _ = self.git("merge", "--ff-only", BRANCH, directory=self.repo)
        _ = self.git("push", "-q", "origin", "main", directory=self.repo)
        for attempt in (1, 2):
            with self.subTest(attempt=attempt):
                self.assert_retained(self.script("perform_deletion.sh", self.repo, self.worktree, BRANCH), tip)

    def test_confirmed_removal_lists_the_folders_without_origin(self) -> None:
        tip = self.git("rev-parse", "HEAD", directory=self.worktree)
        _ = self.git("remote", "remove", "origin", directory=self.repo)
        preview_id = self.confirmed_preview(self.repo, self.worktree)
        self.assert_retained(self.script("perform_deletion.sh", self.repo, self.worktree,
                                         BRANCH, preview_id), tip, origin=False)

    def test_branch_lookup_error_stops_before_reporting_folders(self) -> None:
        tip = self.git("rev-parse", "HEAD", directory=self.worktree)
        preview_id = self.confirmed_preview(self.repo, self.worktree)
        real_git = shutil.which("git")
        assert real_git is not None
        tools = self.root / "bin"
        tools.mkdir()
        shim = tools / "git"
        _ = shim.write_text(
            '#!/bin/sh\nif [ "$1" = show-ref ]; then\n'
            + "    echo 'fatal: injected branch lookup failure' >&2\n    exit 128\nfi\n"
            + f'exec {shlex.quote(real_git)} "$@"\n')
        shim.chmod(0o700)
        with patch.dict(self.environment, {"PATH": str(tools) + os.pathsep + self.environment["PATH"]}):
            result = self.script("perform_deletion.sh", self.repo, self.worktree, BRANCH, preview_id)
        self.assertEqual(result.returncode, 128, result.stdout + result.stderr)
        self.assertIn("fatal: injected branch lookup failure", result.stderr)
        self.assertNotIn("kept for a manual removal list", result.stderr)
        self.assertTrue(self.worktree.is_dir())
        self.assertEqual(self.git("ls-remote", "--heads", "origin", BRANCH, directory=self.repo),
                         f"{tip}\trefs/heads/{BRANCH}")


class ProtectedDeletionTests(unittest.TestCase):
    root: Path = Path()
    scripts: Path = Path()
    nonce_dir: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        staff_file = self.root / "staff.json"
        _ = staff_file.write_text('{"schema": 2, "rows": []}')
        self.enterContext(isolated_staff_file(staff_file, {}))
        self.enterContext(patch.dict(os.environ, {
            **fixture_git_environment(self.root), "HOME": str(self.root), "GIT_CONFIG_SYSTEM": os.devnull,
            "WORKTREES_STATE_DIR": str(self.root / "worktrees-state"),
            "XDG_STATE_HOME": str(self.root / "state"), "GIT_ALLOW_PROTOCOL": "file",
        }, clear=True))
        source = Path(__file__).resolve().parent
        self.scripts = self.root / "scripts/worktree_delete"
        self.scripts.mkdir(parents=True)
        for name in ("record_confirmation.sh", "worktree_delete_validation.sh", "perform_deletion.sh"):
            _ = shutil.copyfile(source / name, self.scripts / name)
        self.nonce_dir = self.root / "confirmations"
        _ = (self.scripts / "confirm_gate.sh").write_text((source / "confirm_gate.sh").read_text().replace(
            'WT_CONFIRM_DIR="/tmp/claude/worktree_delete"', f"WT_CONFIRM_DIR={shlex.quote(str(self.nonce_dir))}"))
        for name in ("lib", "worktree"):
            (self.scripts.parent / name).symlink_to(source.parent / name, target_is_directory=True)

    def preview(self, checkout: Path, worktree: Path | str) -> dict[str, object]:
        result = subprocess.run(["bash", str(self.scripts / "worktree_delete_validation.sh"), str(worktree)],
                                cwd=checkout, capture_output=True, text=True, check=True)
        return cast(dict[str, object], json.loads(result.stdout))

    def confirm(self, checkout: Path, preview_id: str) -> None:
        _ = subprocess.run(["bash", str(self.scripts / "record_confirmation.sh"), preview_id],
                           cwd=checkout, capture_output=True, text=True, check=True)

    def confirmed_preview(self, checkout: Path, worktree: Path | str) -> str:
        preview_id = cast(str, self.preview(checkout, worktree)["preview_id"])
        self.confirm(checkout, preview_id)
        return preview_id

    def test_protected_integration_refs_during_validation_and_deletion(self) -> None:
        repo, origin, tree = self.root / "repo", self.root / "origin.git", self.root / "integration"
        _ = subprocess.run(["git", "init", "-qb", "main", str(repo)], check=True)
        _ = subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
        _ = commit_repository(repo, "Fixture", allow_empty=True)

        def git(checkout: Path, *arguments: str) -> str:
            return subprocess.run(["git", "-C", str(checkout), *arguments],
                                  check=True, capture_output=True, text=True).stdout

        _ = git(repo, "remote", "add", "origin", str(origin))
        _ = git(repo, "worktree", "add", "-qb", "integration", str(tree))
        _ = git(repo, "push", "-q", "origin", "main", "integration")
        doc = repo / "fixture-production-live.md"
        enrolled_document(doc, repo, tree, "integration")
        local = staff_remote.local_machine()
        owner = fixture_member("showrunner", name="fixture", doc=staff.LiveDoc(local, doc))
        other_origin = self.root / "other.git"
        _ = git(self.root, "clone", "-q", "--bare", str(origin), str(other_origin))
        alias = self.root / "alias.git"
        alias.symlink_to(origin)

        config = self.root / "machines"
        config.mkdir()
        _ = shutil.copyfile(Path(__file__).resolve().parents[2] / "config/machines.conf", config / "machines.conf")
        binaries = self.root / "bin"
        binaries.mkdir()
        self.enterContext(patch.dict(os.environ, {"CONF_DIR": str(config),
                                                 "GIT_ALLOW_PROTOCOL": "file",
                                                 "PATH": f"{binaries}{os.pathsep}{os.environ['PATH']}"}))
        other_host = staff_remote.other_machine()
        ssh = binaries / "ssh"
        _ = ssh.write_text("#!/bin/sh\n"
                           + f'[ "$1" = -o ] && [ "$2" = BatchMode=yes ] && [ "$3" = {other_host} ] || exit 255\n'
                           + f'[ "$4" = {shlex.quote(shlex.join(("cat", str(doc))))} ] || exit 255\n'
                           + f'exec cat {shlex.quote(str(doc))}\n')
        ssh.chmod(0o755)

        def attempt(checkout: Path, worktree: Path, script: str, *arguments: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run(["bash", str(self.scripts / script), str(worktree), *arguments], cwd=checkout,
                                  capture_output=True, text=True, check=False)

        pattern = ("for-each-ref", "--format=%(refname) %(objectname)")
        protected_repos = (repo, origin, other_origin)
        protected_refs = tuple(git(folder, *pattern) for folder in protected_repos)
        staff_file = self.root / "staff.json"
        for index, (case, confirmed, refusal) in enumerate((
                ("local worktree", False, "protected production"),
                ("local worktree", True, "protected production"),
                ("local clone", False, ""),
                ("local clone", True, ""),
                ("missing staff record", True, "cannot read the staff record"),
                ("unrelated repository with the same origin ref", True, ""),
                ("different basenames", False, ""),
                ("file and SSH access", True, ""),
                ("HTTPS and SSH access", True, ""),
                ("wrapped registration", True, ""),
                ("unresolved registration", True, ""),
                ("unresolved enrolled registration", True, ""))):
            with self.subTest(case=case, confirmed=confirmed):
                upstream = other_origin if case == "unrelated repository with the same origin ref" else origin
                host = other_host if upstream == other_origin else local
                location = staff.WrappedDoc(host, doc) if case == "wrapped registration" else staff.LiveDoc(host, doc)
                _ = staff.set_doc(owner.id, location)
                unresolved_id = ""
                if case == "unresolved registration":
                    unresolved = self.root / "unresolved.md"
                    _ = unresolved.write_text("# Unresolved registration\n")
                    unresolved_id = str(fixture_member("showrunner", name="unresolved", doc=staff.LiveDoc(local, unresolved)).id)
                    _ = staff.set_doc(owner.id, staff.WrappedDoc(local, self.root / "retained.md"))
                    _ = (self.root / "retained.md").write_text("- **Merge branch:** retained\n")
                if case == "unresolved enrolled registration":
                    _ = doc.write_text("\n".join(line for line in doc.read_text().splitlines()
                                                if "Merge branch" not in line))
                    unresolved_id = str(owner.id)
                if case == "local worktree":
                    checkout, worktree = repo, tree
                else:
                    checkout, worktree = self.root / f"clone-{index}", self.root / f"tree-{index}"
                    _ = git(self.root, "clone", "-q", upstream.as_uri(), str(checkout))
                    _ = git(checkout, "worktree", "add", "-qb", "integration", str(worktree), "origin/integration")
                    if case == "different basenames":
                        _ = git(checkout, "remote", "set-url", "--push", "origin", str(alias))
                    if case == "HTTPS and SSH access":
                        _ = git(repo, "remote", "set-url", "--push", "origin", f"https://candidate{origin}")
                    if case in ("file and SSH access", "HTTPS and SSH access"):
                        _ = git(checkout, "remote", "set-url", "--push", "origin", f"ssh://candidate{origin}")
                staff_bytes = staff_file.read_bytes()
                protected_preview = ""
                if refusal and confirmed:
                    retained_doc = self.root / "preview-retained.md"
                    _ = retained_doc.write_text("- **Merge branch:** integration\n")
                    _ = staff.set_doc(owner.id, staff.WrappedDoc(local, retained_doc))
                    protected_preview = self.confirmed_preview(checkout, worktree)
                    _ = staff_file.write_bytes(staff_bytes)
                if case == "missing staff record":
                    staff_file.unlink()
                validation = attempt(checkout, worktree, "worktree_delete_validation.sh")
                kept = bool(refusal)
                self.assertEqual(validation.returncode, int(kept), validation.stdout + validation.stderr)
                options = [protected_preview] if protected_preview else []
                if not kept:
                    fields = cast(dict[str, object], json.loads(validation.stdout))
                    preview_id = cast(str, fields["preview_id"])
                    options.append(preview_id)
                    if confirmed:
                        self.confirm(checkout, preview_id)
                    else:
                        options.append("--automatic")
                nonces = {path: path.read_bytes() for path in self.nonce_dir.glob("*")}
                result = attempt(checkout, worktree, "perform_deletion.sh", "integration", *options)
                self.assertEqual(result.returncode, int(kept), result.stdout + result.stderr)
                self.assertTrue(worktree.is_dir())
                if refusal:
                    self.assertIn(refusal, validation.stdout)
                    self.assertIn(refusal, result.stderr)
                    self.assertEqual({path: path.read_bytes() for path in self.nonce_dir.glob("*")}, nonces)
                    self.assertIn("refs/heads/integration", git(checkout, "show-ref"))
                else:
                    self.assertIn('"keep_origin": true', validation.stdout)
                    self.assertIn('"has_remote_branch": false', validation.stdout)
                    self.assertIn('"cleanup": true', validation.stdout)
                    self.assertIn("Kept origin ref refs/heads/integration", result.stdout)
                    if unresolved_id:
                        self.assertIn(unresolved_id, result.stdout)
                    self.assertIn("refs/heads/integration", git(checkout, "show-ref"))
                self.assertEqual(tuple(git(folder, *pattern) for folder in protected_repos), protected_refs)
                if case == "missing staff record":
                    _ = staff_file.write_bytes(staff_bytes)


if __name__ == "__main__":
    _ = unittest.main()
