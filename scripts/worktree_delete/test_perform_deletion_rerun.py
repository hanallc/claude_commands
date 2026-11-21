"""Preserve work during deletion and complete deletion retries."""
from __future__ import annotations

import fcntl
import json
import os
import shlex
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import cast, override
from unittest.mock import patch

from scripts.agents import remote
from scripts.lib.git_test_support import commit_repository, fixture_git_environment
from scripts.production import staff
from scripts.production.staff_test_support import fixture_member, isolated_staff_file
from scripts.production.test_production_doc import enrolled_document


class DeletionRerunTests(unittest.TestCase):
    root: Path = Path()
    scripts: Path = Path()
    nonce_dir: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(isolated_staff_file(self.root / "staff.json", {}))
        self.enterContext(patch.dict(os.environ, {
            **fixture_git_environment(self.root), "HOME": str(self.root), "GIT_CONFIG_SYSTEM": os.devnull,
            "WORKTREES_STATE_DIR": str(self.root / "worktrees-state"),
            "XDG_STATE_HOME": str(self.root / "state"),
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

    def test_protected_integration_stops_validation_and_both_deletion_routes(self) -> None:
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
        _ = fixture_member("showrunner", name="fixture", doc=staff.LiveDoc(remote.local_machine(), doc))
        clone = self.root / "clone"
        clone_tree = self.root / "clone-integration"
        _ = git(self.root, "clone", "-q", origin.as_uri(), str(clone))
        _ = git(clone, "worktree", "add", "-qb", "integration", str(clone_tree), "origin/integration")

        def attempt(checkout: Path, worktree: Path, script: str, *arguments: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run(["bash", str(self.scripts / script), str(worktree), *arguments], cwd=checkout,
                                  capture_output=True, text=True, check=False)

        pattern = ("for-each-ref", "--format=%(refname) %(objectname)")
        refs = tuple(git(folder, *pattern) for folder in (repo, clone, origin))
        for checkout, worktree, confirmed in ((repo, tree, False), (repo, tree, True),
                                              (clone, clone_tree, False), (clone, clone_tree, True)):
            with self.subTest(checkout=checkout, confirmed=confirmed):
                validation = attempt(checkout, worktree, "worktree_delete_validation.sh")
                self.assertEqual(validation.returncode, 1, validation.stderr)
                self.assertIn("protected production", validation.stdout)
                if confirmed:
                    result = attempt(checkout, worktree, "record_confirmation.sh", "integration")
                    self.assertEqual(result.returncode, 0, result.stderr)
                nonces = {path: path.read_bytes() for path in self.nonce_dir.glob("*")}
                result = attempt(checkout, worktree, "perform_deletion.sh", "integration")
                self.assertEqual(result.returncode, 1)
                self.assertIn("protected production", result.stderr)
                self.assertTrue(worktree.is_dir())
                self.assertEqual(tuple(git(folder, *pattern) for folder in (repo, clone, origin)), refs)
                self.assertEqual({path: path.read_bytes() for path in self.nonce_dir.glob("*")}, nonces)

    def test_confirmed_removal_releases_the_pair_when_the_lock_is_free(self) -> None:
        scripts = self.scripts
        for held in (False, True):
            with self.subTest(held=held), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                repo = root / "repo"
                caller, tree = root / "trees" / "caller", root / "trees" / "topic"
                _ = subprocess.run(["git", "init", "-q", "--initial-branch=main", str(repo)], check=True)
                _ = subprocess.run(["git", "-C", str(repo), "-c", "user.name=Test", "-c",
                                    "user.email=test@example.invalid", "-c", "commit.gpgsign=false",
                                    "commit", "--allow-empty", "-qm", "fixture"], check=True)
                for path in (caller, tree):
                    _ = subprocess.run(["git", "-C", str(repo), "worktree", "add", "-qb", path.name, str(path)],
                                       check=True)
                lease, lock = tree.with_suffix(".lease.json"), tree.with_suffix(".lock")
                _ = lease.write_text("{}\n")
                lock.touch()
                relative = os.path.relpath(tree, caller)
                _ = subprocess.run(["bash", str(scripts / "record_confirmation.sh"), relative, "topic"],
                                   cwd=caller, capture_output=True, text=True, check=True)

                with lock.open("r+") as descriptor:
                    if held:
                        fcntl.flock(descriptor, fcntl.LOCK_EX)
                    result = subprocess.run(["bash", str(scripts / "perform_deletion.sh"), relative, "topic"],
                                            cwd=caller, capture_output=True, text=True, check=False)

                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertFalse(tree.exists())
                self.assertEqual(lease.exists(), held)
                self.assertEqual(lock.exists(), held)
                self.assertEqual(result.stderr.count("Worktree pair kept"), int(held))

    def test_branch_lookup_error_stops_before_remote_deletion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, remote, worktree = root / "repo", root / "remote.git", root / "topic"
            _ = subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
            _ = subprocess.run(["git", "init", "-q", "--initial-branch=main", str(repo)], check=True)

            def git(*args: str) -> str:
                return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout

            _ = git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "-c", "commit.gpgsign=false",
                    "commit", "--allow-empty", "-qm", "fixture")
            _ = git("remote", "add", "origin", str(remote))
            _ = git("worktree", "add", "-qb", "topic", str(worktree))
            _ = git("push", "-q", "origin", "topic")
            scripts = self.scripts
            _ = subprocess.run(["bash", str(scripts / "record_confirmation.sh"), str(worktree), "topic"],
                               cwd=repo, capture_output=True, text=True, check=True)

            real_git = shutil.which("git")
            assert real_git is not None
            shim_dir = root / "bin"
            shim_dir.mkdir()
            remote_reached = root / "remote-reached"
            shim = shim_dir / "git"
            _ = shim.write_text("\n".join([
                "#!/bin/sh",
                "if [ \"$1\" = show-ref ]; then",
                "    echo 'fatal: injected branch lookup failure' >&2",
                "    exit 128",
                "fi",
                "if [ \"$1\" = ls-remote ] || [ \"$1\" = push ]; then",
                f"    : > {shlex.quote(str(remote_reached))}",
                "fi",
                f"exec {shlex.quote(real_git)} \"$@\"",
                "",
            ]))
            shim.chmod(0o755)
            result = subprocess.run(
                ["bash", str(scripts / "perform_deletion.sh"), str(worktree), "topic"],
                cwd=repo, capture_output=True, text=True, check=False,
                env={**os.environ, "PATH": str(shim_dir) + os.pathsep + os.environ["PATH"]},
            )
            self.assertEqual(result.returncode, 128, result.stdout + result.stderr)
            self.assertIn("fatal: injected branch lookup failure", result.stderr)
            self.assertFalse(remote_reached.exists())
            self.assertEqual(git("branch", "--list", "topic", "--format=%(refname:short)").strip(), "topic")
            self.assertIn("refs/heads/topic", git("ls-remote", "--heads", "origin", "topic"))

    def test_remote_failure_reruns_with_missing_local_branch(self) -> None:
        for state in ("confirmed-rerun", "cleanup", "unlanded", "cleanup-changed", "origin-race",
                      "local-race", "confirmed-no-origin", "without-branch-config", "config-error"):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                repo, remote, worktree = root / "repo", root / "remote.git", root / "topic"
                _ = subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
                _ = subprocess.run(["git", "init", "-q", "--initial-branch=main", str(repo)], check=True)

                def git(*args: str) -> str:
                    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout

                _ = git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "-c", "commit.gpgsign=false",
                        "commit", "--allow-empty", "-qm", "fixture")
                _ = git("remote", "add", "origin", str(remote))
                _ = git("worktree", "add", "-qb", "topic", str(worktree))
                _ = git("push", "-q", "origin", "topic")
                hook = remote / "hooks/pre-receive"
                if state == "confirmed-rerun":
                    _ = hook.write_text("#!/bin/sh\nexit 1\n")
                    hook.chmod(0o755)
                else:
                    _ = (worktree / "code.txt").write_text("topic work\n")
                    _ = git("-C", str(worktree), "add", "code.txt")
                    _ = git("-C", str(worktree), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                            "-c", "commit.gpgsign=false", "commit", "-qm", "Change code.txt")
                    _ = git("push", "-q", "origin", "topic")
                    if state != "unlanded":
                        _ = git("merge", "--ff-only", "topic")
                    _ = git("push", "-q", "origin", "main")
                    _ = git("remote", "set-head", "origin", "main")
                if state == "confirmed-no-origin":
                    _ = git("remote", "remove", "origin")
                scripts = self.scripts
                environment = {**os.environ, "WORKTREES_STATE_DIR": str(root / "worktrees-state")}

                def attempt() -> subprocess.CompletedProcess[str]:
                    if state in ("confirmed-rerun", "confirmed-no-origin"):
                        _ = subprocess.run(["bash", str(scripts / "record_confirmation.sh"), str(worktree), "topic"],
                                           cwd=repo, capture_output=True, text=True, check=True)
                    return subprocess.run(["bash", str(scripts / "perform_deletion.sh"), str(worktree), "topic"],
                                          cwd=repo, env=environment, capture_output=True, text=True, check=False)

                if state not in ("confirmed-rerun", "confirmed-no-origin"):
                    validation = subprocess.run(
                        ["bash", str(scripts / "worktree_delete_validation.sh"), str(worktree)],
                        cwd=repo, env=environment, capture_output=True, text=True, check=True)
                    fields = cast(dict[str, object], json.loads(validation.stdout))
                    if state in ("cleanup", "unlanded"):
                        self.assertEqual(fields["cleanup"], state == "cleanup")
                    else:
                        self.assertTrue(fields["cleanup"])
                    self.assertTrue(fields["cleanup_reason"])
                origin_commit = ""
                local_commit = ""
                if state in ("cleanup-changed", "origin-race", "local-race", "config-error"):
                    real_git = shutil.which("git")
                    assert real_git is not None
                    shim_dir = root / "bin"
                    shim_dir.mkdir()
                    marker = root / "race-injected"
                    if state == "config-error":
                        injection = [
                            'if [ "$1" = config ] && [ "$3" = --remove-section ]; then',
                            "    echo 'injected config failure' >&2",
                            "    exit 3",
                            "fi",
                        ]
                    elif state == "cleanup-changed":
                        injection = [
                            'if [ "$1" = worktree ] && [ "$2" = remove ]; then',
                            f"    if [ ! -f {shlex.quote(str(marker))} ]; then",
                            f"        : > {shlex.quote(str(marker))}",
                            f"        printf 'edit after cleanup\\n' > {shlex.quote(str(worktree / 'code.txt'))}",
                            "    fi",
                            "fi",
                        ]
                    elif state == "origin-race":
                        clone = root / "origin-writer"
                        _ = git("clone", "--branch", "topic", str(remote), str(clone))
                        _ = (clone / "origin.txt").write_text("concurrent origin work\n")
                        _ = git("-C", str(clone), "add", "origin.txt")
                        _ = git("-C", str(clone), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                                "-c", "commit.gpgsign=false", "commit", "-qm", "Change origin.txt")
                        origin_commit = git("-C", str(clone), "rev-parse", "HEAD").strip()
                        injection = [
                            "for argument do",
                            '    if [ "$argument" = --delete ]; then',
                            f"        if [ ! -f {shlex.quote(str(marker))} ]; then",
                            f"            : > {shlex.quote(str(marker))}",
                            f"            {shlex.quote(real_git)} -C {shlex.quote(str(clone))} push origin HEAD:refs/heads/topic >&2 || exit 1",
                            "        fi",
                            "    fi",
                            "done",
                        ]
                    else:
                        clone = root / "local-writer"
                        _ = git("clone", "--branch", "topic", str(remote), str(clone))
                        _ = (clone / "code.txt").write_text("concurrent local work\n")
                        _ = git("-C", str(clone), "add", "code.txt")
                        _ = git("-C", str(clone), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                                "-c", "commit.gpgsign=false", "commit", "-qm", "Change code.txt")
                        local_commit = git("-C", str(clone), "rev-parse", "HEAD").strip()
                        _ = git("fetch", str(clone), "topic")
                        injection = [
                            'if [ "$1" = worktree ] && [ "$2" = remove ]; then',
                            f"    if [ ! -f {shlex.quote(str(marker))} ]; then",
                            f"        : > {shlex.quote(str(marker))}",
                            f"        {shlex.quote(real_git)} -C {shlex.quote(str(worktree))} reset --hard {local_commit} >&2 || exit 1",
                            "    fi",
                            "fi",
                        ]
                    shim = shim_dir / "git"
                    _ = shim.write_text("\n".join([
                        "#!/bin/sh", *injection, f'exec {shlex.quote(real_git)} "$@"', "",
                    ]))
                    shim.chmod(0o755)
                    environment["PATH"] = str(shim_dir) + os.pathsep + os.environ["PATH"]
                _ = git("config", "--local", "branch.topic.merge", "refs/heads/topic")
                if state == "without-branch-config":
                    _ = git("config", "--local", "--remove-section", "branch.topic")
                before_branches = git("for-each-ref", "--format=%(refname) %(objectname)", "refs/heads")
                before_origin = "" if state == "confirmed-no-origin" else git("ls-remote", "--heads", "origin")
                before_worktrees = git("worktree", "list", "--porcelain")

                first = attempt()

                if state == "confirmed-rerun":
                    self.assertNotEqual(first.returncode, 0)
                    self.assertTrue(worktree.exists())
                    self.assertEqual(git("branch", "--list", "topic", "--format=%(refname:short)").strip(), "topic")
                    self.assertIn("refs/heads/topic", git("ls-remote", "--heads", "origin", "topic"))
                    hook.unlink()
                    second = attempt()
                    self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
                    self.assertFalse(worktree.exists())
                    self.assertEqual(git("branch", "--list", "topic"), "")
                    self.assertEqual(git("ls-remote", "--heads", "origin", "topic"), "")
                elif state in ("cleanup", "without-branch-config"):
                    self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
                    self.assertFalse(worktree.exists())
                    self.assertEqual(git("branch", "--list", "topic"), "")
                    self.assertEqual(git("ls-remote", "--heads", "origin", "topic"), "")
                elif state == "confirmed-no-origin":
                    self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
                    self.assertFalse(worktree.exists())
                    self.assertEqual(git("branch", "--list", "topic"), "")
                    self.assertNotIn("origin", git("remote").splitlines())
                elif state == "config-error":
                    self.assertEqual(first.returncode, 3, first.stdout + first.stderr)
                    self.assertIn("injected config failure", first.stderr)
                    self.assertFalse(worktree.exists())
                    self.assertEqual(git("branch", "--list", "topic"), "")
                    self.assertEqual(git("config", "--local", "--get", "branch.topic.merge"), "refs/heads/topic\n")
                    (root / "bin/git").unlink()
                    _ = subprocess.run(["bash", str(scripts / "record_confirmation.sh"), str(worktree), "topic"],
                                       cwd=repo, capture_output=True, text=True, check=True)

                    second = attempt()

                    self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
                    self.assertFalse(worktree.exists())
                    self.assertEqual(git("branch", "--list", "topic"), "")
                    self.assertEqual(git("ls-remote", "--heads", "origin", "topic"), "")
                elif state == "cleanup-changed":
                    self.assertNotEqual(first.returncode, 0)
                    self.assertIn("the tree changed after the cleanup check", first.stdout)
                    self.assertEqual((worktree / "code.txt").read_text(), "edit after cleanup\n")
                    self.assertEqual(git("for-each-ref", "--format=%(refname) %(objectname)", "refs/heads"), before_branches)
                    self.assertEqual(git("worktree", "list", "--porcelain"), before_worktrees)
                elif state == "origin-race":
                    self.assertNotEqual(first.returncode, 0)
                    self.assertIn("origin branch deletion failed", first.stdout)
                    self.assertIn("stale info", first.stderr)
                    self.assertEqual(git("ls-remote", "--heads", "origin", "topic").split()[0], origin_commit)
                    self.assertEqual((worktree / "code.txt").read_text(), "topic work\n")
                    self.assertEqual(git("for-each-ref", "--format=%(refname) %(objectname)", "refs/heads"), before_branches)
                    self.assertEqual(git("worktree", "list", "--porcelain"), before_worktrees)
                elif state == "local-race":
                    self.assertNotEqual(first.returncode, 0)
                    self.assertIn("the local branch changed after the cleanup check", first.stdout)
                    self.assertEqual(git("rev-parse", "refs/heads/topic").strip(), local_commit)
                    self.assertFalse(worktree.exists())
                    self.assertEqual(git("show", "topic:code.txt"), "concurrent local work\n")
                    self.assertEqual(git("rev-parse", "topic").strip(), local_commit)
                else:
                    self.assertNotEqual(first.returncode, 0)
                    self.assertIn("no confirmation nonce", first.stdout)
                    self.assertIn("work has not landed", first.stdout)
                    self.assertTrue(worktree.exists())
                    self.assertEqual((worktree / "code.txt").read_text(), "topic work\n")
                    self.assertEqual(git("for-each-ref", "--format=%(refname) %(objectname)", "refs/heads"), before_branches)
                    self.assertEqual(git("ls-remote", "--heads", "origin"), before_origin)
                    self.assertEqual(git("worktree", "list", "--porcelain"), before_worktrees)
                if state in ("confirmed-rerun", "cleanup", "confirmed-no-origin", "without-branch-config", "config-error"):
                    config = subprocess.run(["git", "-C", str(repo), "config", "--local", "--get", "branch.topic.merge"],
                                            capture_output=True, text=True, check=False)
                    self.assertEqual(config.returncode, 1, config.stdout + config.stderr)
                    self.assertEqual(config.stdout, "")


if __name__ == "__main__":
    _ = unittest.main()
