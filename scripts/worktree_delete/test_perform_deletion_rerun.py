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

from scripts.agents import remote as staff_remote
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
                self.assertEqual(worktree.is_dir(), kept)
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
                    self.assertEqual(git(checkout, "branch", "--list", "integration"), "")
                self.assertEqual(tuple(git(folder, *pattern) for folder in protected_repos), protected_refs)
                if case == "missing staff record":
                    _ = staff_file.write_bytes(staff_bytes)

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
                preview_id = self.confirmed_preview(caller, relative)

                with lock.open("r+") as descriptor:
                    if held:
                        fcntl.flock(descriptor, fcntl.LOCK_EX)
                    result = subprocess.run(["bash", str(scripts / "perform_deletion.sh"), relative, "topic", preview_id],
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
            preview_id = self.confirmed_preview(repo, worktree)

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
                ["bash", str(scripts / "perform_deletion.sh"), str(worktree), "topic", preview_id],
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
                      "local-race", "confirmed-no-origin", "without-branch-config", "config-error",
                      "protection removed after preview", "origin advanced after confirmation",
                      "absent origin appears after confirmation", "local commit changed after confirmation",
                      "dirty after confirmation", "push destination changed after confirmation"):
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

                confirmed = state in ("confirmed-rerun", "confirmed-no-origin") or " after " in state

                def attempt(preview_id: str = "") -> subprocess.CompletedProcess[str]:
                    options = [preview_id] if preview_id else []
                    return subprocess.run(["bash", str(scripts / "perform_deletion.sh"), str(worktree), "topic", *options],
                                          cwd=repo, env=environment, capture_output=True, text=True, check=False)

                if not confirmed:
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
                preview_id = ""
                if confirmed:
                    staff_file = self.root / "staff.json"
                    staff_bytes = staff_file.read_bytes()
                    if state == "protection removed after preview":
                        protected_doc = root / "retained.md"
                        _ = protected_doc.write_text("- **Merge branch:** topic\n")
                        _ = fixture_member("showrunner", name="retained",
                                           doc=staff.WrappedDoc(staff_remote.local_machine(), protected_doc))
                    reviewed = ""
                    if state == "absent origin appears after confirmation":
                        _ = git("--git-dir", str(remote), "update-ref", "-d", "refs/heads/topic")
                    elif state == "origin advanced after confirmation":
                        _ = git("config", "remote.origin.fetch", "+refs/heads/main:refs/remotes/origin/main")
                        reviewed = git("commit-tree", "topic^{tree}", "-p", "topic", "-m", "Reviewed origin commit").strip()
                        _ = git("push", str(remote), f"{reviewed}:refs/heads/topic")
                    preview = self.preview(repo, worktree)
                    preview_id = cast(str, preview["preview_id"])
                    self.confirm(repo, preview_id)
                    if state == "protection removed after preview":
                        self.assertEqual(preview["origin_action"], "keep")
                        _ = staff_file.write_bytes(staff_bytes)
                    elif state == "absent origin appears after confirmation":
                        self.assertEqual(preview["origin_action"], "absent")
                        _ = git("push", "origin", "topic")
                    elif state == "origin advanced after confirmation":
                        self.assertEqual(preview["origin_commit"], reviewed)
                        self.assertEqual(preview["remote_only_count"], 1)
                        advanced = git("commit-tree", "topic^{tree}", "-p", reviewed, "-m", "New origin commit").strip()
                        _ = git("push", "origin", f"{advanced}:refs/heads/topic")
                    elif state == "local commit changed after confirmation":
                        _ = commit_repository(worktree, "New local commit", allow_empty=True)
                    elif state == "dirty after confirmation":
                        _ = (worktree / "uncommitted").write_text("Keep this file.\n")
                    elif state == "push destination changed after confirmation":
                        peer = root / "peer.git"
                        _ = git("clone", "--bare", str(remote), str(peer))
                        _ = git("remote", "set-url", "--push", "origin", str(peer))
                before_branches = git("for-each-ref", "--format=%(refname) %(objectname)", "refs/heads")
                before_origin = "" if state == "confirmed-no-origin" else git("ls-remote", "--heads", "origin")
                before_worktrees = git("worktree", "list", "--porcelain")

                first = attempt(preview_id)

                if state == "confirmed-rerun":
                    self.assertNotEqual(first.returncode, 0)
                    self.assertTrue(worktree.exists())
                    self.assertEqual(git("branch", "--list", "topic", "--format=%(refname:short)").strip(), "topic")
                    self.assertIn("refs/heads/topic", git("ls-remote", "--heads", "origin", "topic"))
                    hook.unlink()
                    self.assertIn("already used", attempt(preview_id).stderr)
                    second = attempt(self.confirmed_preview(repo, worktree))
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
                    second = attempt()

                    self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
                    self.assertFalse(worktree.exists())
                    self.assertEqual(git("branch", "--list", "topic"), "")
                    self.assertEqual(git("ls-remote", "--heads", "origin", "topic"), "")
                elif state in ("protection removed after preview", "absent origin appears after confirmation"):
                    self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
                    self.assertFalse(worktree.exists())
                    self.assertEqual(git("ls-remote", "--heads", "origin"), before_origin)
                elif state in ("origin advanced after confirmation", "local commit changed after confirmation",
                               "dirty after confirmation", "push destination changed after confirmation"):
                    self.assertNotEqual(first.returncode, 0, first.stdout + first.stderr)
                    self.assertEqual(git("ls-remote", "--heads", "origin"), before_origin)
                    self.assertEqual(git("for-each-ref", "--format=%(refname) %(objectname)", "refs/heads"), before_branches)
                    self.assertTrue(worktree.exists())
                    if state == "dirty after confirmation":
                        self.assertEqual((worktree / "uncommitted").read_text(), "Keep this file.\n")
                    if state == "push destination changed after confirmation":
                        self.assertIn("configured push destination changed", first.stderr)
                        self.assertIn("refs/heads/topic", git("ls-remote", "--heads", str(root / "peer.git")))
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
