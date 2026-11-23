"""Test the vault save with local fixture repositories."""
from __future__ import annotations

import fcntl
import hashlib
import io
import os
import subprocess
import tempfile
import unittest
from contextlib import chdir, redirect_stderr, redirect_stdout
from pathlib import Path
from typing import override
from unittest.mock import patch

from scripts.lib import land_main
from scripts.lib.git_test_support import fixture_git_environment
from scripts.lib.scratch_tree_test_support import scratch_ls_files
from scripts.maintenance import vault_commit, vault_save
from scripts.maintenance.settings import Part


def job_config_fixture() -> str:
    parts = "".join(
        f"[parts.{part.value}]\ninterval_seconds = 120\ntimeout_seconds = 60\n"
        for part in Part if part not in (Part.LOGINS, Part.VAULT_SAVE)
    )
    return """[job]
interval_seconds = 120
timeout_seconds = 120
[parts.logins]
interval_seconds = 120
timeout_seconds = 45
[parts.vault_save]
interval_seconds = 180
timeout_seconds = 30
[vault]
git_seconds = 8
named_paths = 4
[watchdog]
dispatch_seconds = 5
recovery_seconds = 20
pass_dispatch_seconds = 10
result_seconds = 25
""" + parts


class VaultSaveTests(unittest.TestCase):
    vault: Path = Path()
    origin: Path = Path()

    @override
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        self.vault = root / "rust" / "hanadocs"
        self.origin = root / "origin"
        self.vault.mkdir(parents=True)
        self.origin.mkdir()
        config = root / "agent_notes.toml"
        _ = config.write_text(job_config_fixture())
        _ = (root / "main_push_lock.conf").write_text("# whole: 1..\nrecord_days=1\n")
        self.enterContext(patch.dict(os.environ, {
            **fixture_git_environment(root),
            "AGENT_NOTES_JOB_CONFIG": str(config),
            "CONF_DIR": str(root),
        }, clear=True))
        _ = self.git("init", "-q", "-b", "main")
        _ = self.git("init", "-q", "--bare", "-b", "main", repo=self.origin)
        self.write(".gitignore", ".obsidian/\nsettings.local.json\n")
        self.write("remove.md", "Delete this note.\n")
        self.write("ux/kept.png", "The fixture represents a tracked image.\n")
        _ = self.git("add", "-A")
        _ = self.git("commit", "-m", "Fixture base")
        _ = self.git("remote", "add", "origin", str(self.origin))
        _ = self.git("push", "origin", "main")

    def git(self, *args: str, repo: Path | None = None) -> str:
        return os.fsdecode(self.git_output(*args, repo=repo)).rstrip("\n")

    def git_output(self, *args: str, repo: Path | None = None) -> bytes:
        return subprocess.run(
            ["git", "-C", str(repo or self.vault), *args], check=True, capture_output=True,
        ).stdout

    def write(self, name: str, text: str) -> None:
        path = self.vault / name
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(text, encoding="utf-8")

    def command(self, *args: str) -> tuple[int, str, str]:
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = vault_save.main(["--vault", str(self.vault), *args])
        return code, output.getvalue(), errors.getvalue()

    def scan(self) -> str:
        with patch.object(vault_save, "scan"):
            code, output, errors = self.command("first-save", "scan")
        self.assertEqual(code, 0, errors)
        return output.splitlines()[-1].removeprefix("Manifest hash ")

    def mark_first_save_published(self) -> None:
        _ = self.git("update-ref", vault_save.FIRST_SAVE_REF, "HEAD")

    def test_vault_save_waits_for_first_save(self) -> None:
        self.write("reviewed.md", "Save this after review.\n")
        base = self.git("rev-parse", "HEAD")
        index = self.vault / ".git/index"
        original = index.read_bytes()
        code, output, errors = self.command()
        self.assertEqual((code, output, errors), (0, "Vault save waits for the reviewed first save.\n", ""))
        self.assertEqual(self.git("rev-parse", "HEAD"), base)
        self.assertEqual(index.read_bytes(), original)
        self.assertEqual(self.git("status", "--short"), "?? reviewed.md")

        code, _, errors = self.command("first-save", "commit", self.scan())
        self.assertEqual(code, 0, errors)
        published = self.git("rev-parse", "main", repo=self.origin)
        self.assertNotEqual(published, base)
        self.assertEqual(self.git("show-ref", "--hash", "--verify", vault_save.FIRST_SAVE_REF), published)

        self.write("later.md", "Save this on the timer.\n")
        code, output, errors = self.command()
        self.assertEqual(code, 0, errors)
        self.assertIn("Saved 1 paths", output)
        self.assertEqual(self.git("rev-parse", "HEAD^"), published)
        self.assertEqual(self.git("show", "HEAD:later.md"), "Save this on the timer.")

    def test_vault_save_defaults_to_the_home_vault_from_another_repo(self) -> None:
        self.mark_first_save_published()
        home = self.vault.parent.parent
        other = home / "other"
        other.mkdir()
        _ = self.git("init", "-q", "-b", "main", repo=other)
        _ = self.git("commit", "--allow-empty", "-m", "Other fixture base", repo=other)
        _ = (other / "other.md").write_text("Keep this note untracked.\n")
        self.write("note.md", "Save this note.\n")

        with chdir(other), patch.object(Path, "home", return_value=home), redirect_stdout(io.StringIO()):
            self.assertEqual(vault_save.main([]), 0)

        self.assertEqual(self.git("show", "HEAD:note.md"), "Save this note.")
        self.assertEqual(self.git("status", "--short", repo=other), "?? other.md")

    def test_vault_save_selects_text_at_every_depth(self) -> None:
        self.mark_first_save_published()
        names = {"new/nested/note [x].MD", "new/nested/note\rline.txt", "new/nested/note\nline.txt",
                 "issues/question-one.md",
                 "change-review/list.md.backup-20261103T014920761519", "new/nested/Cargo.lock"}
        for name in names:
            self.write(name, "Save this text.\n")
        (self.vault / "remove.md").unlink()
        self.write("new/nested/note.md.backup-latest", "Outside the text rule.\n")
        code, output, errors = self.command()
        self.assertEqual(code, 0, errors)
        self.assertIn("Saved 7 paths", output)
        changed = self.git_output("diff", "--name-only", "-z", "HEAD^", "HEAD").split(b"\0")[:-1]
        self.assertEqual(set(map(os.fsdecode, changed)), names | {"remove.md"})

    def test_vault_save_passes_fsync_on_the_commit_command(self) -> None:
        self.mark_first_save_published()
        self.write("note.md", "Save this note.\n")
        with patch.object(subprocess, "run", wraps=subprocess.run) as run:
            code, _, errors = self.command()
        self.assertEqual(code, 0, errors)
        self.assertIn(
            ["git", "-C", str(self.vault), "-c", "core.fsync=committed", "--literal-pathspecs",
             "commit", "--only", "-m", vault_save.SUBJECT, "--pathspec-from-file=-", "--pathspec-file-nul"],
            [call.args[0] for call in run.call_args_list],
        )

    def test_vault_save_honors_ignores_and_keeps_tracked_media(self) -> None:
        self.mark_first_save_published()
        for name in (".obsidian/settings.md", "new/.obsidian/state.json", "settings.local.json",
                     "bob/nested/shot.PNG", "outside/recording.mp4", "outside/vector.svg"):
            self.write(name, "An excluded fixture file.\n")
        self.write("new/.gitignore", "ignored.md\n")
        self.write("new/ignored.md", "Ignore this note.\n")
        self.write("bob/README.md", "Save this note.\n")
        vault_save.install_producer_media_rules(self.vault / "bob")
        self.write("ux/kept.png", "A local change to tracked media.\n")
        self.assertEqual(self.command()[0], 0)
        self.assertEqual(set(self.git("ls-tree", "-r", "--name-only", "HEAD").splitlines()),
                         {".gitignore", "remove.md", "ux/kept.png", "new/.gitignore", "bob/.gitignore", "bob/README.md"})
        self.assertEqual(self.git("status", "--short", "--", "ux/kept.png"), " M ux/kept.png")
        self.assertEqual(self.git("check-ignore", "bob/nested/shot.PNG"), "bob/nested/shot.PNG")

    def test_vault_save_keeps_generated_bytes(self) -> None:
        self.mark_first_save_published()
        files = ("machines/configuration/background jobs.md", "hana hierarchy.md", "agents/a.md", "agent sessions.md")
        for name in files:
            self.write(name, "date_modified: old\nGenerated text.\n")
        self.assertEqual(self.command()[0], 0)
        for name in files:
            self.write(name, "date_modified: new\nGenerated text.\n")
        self.assertEqual(self.command()[0], 0)
        for name in files:
            self.assertEqual(self.git("show", f"HEAD:{name}"), "date_modified: new\nGenerated text.")
            self.assertEqual((self.vault / name).read_bytes(), b"date_modified: new\nGenerated text.\n")

    def test_vault_save_serializes_writers(self) -> None:
        self.mark_first_save_published()
        self.write("agents/a.md", "New note.\n")
        head = self.git("rev-parse", "HEAD")
        lock_path = self.vault / ".git/main-push.lock"
        with lock_path.open("a") as lock, redirect_stderr(io.StringIO()) as errors:
            fcntl.flock(lock, fcntl.LOCK_EX)
            code, _, reason = self.command()
            self.assertEqual(code, 0)
            self.assertIn("vault lock is busy", reason)
            self.assertEqual(vault_commit.main(["--job", "fixture", str(self.vault / "agents/a.md")]), 0)
            self.assertIn("vault lock is busy", errors.getvalue())
            self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertEqual(self.command()[0], 0)
        self.assertEqual(self.git("show", "HEAD:agents/a.md"), "New note.")

    def test_vault_save_preserves_staged_changes(self) -> None:
        self.mark_first_save_published()
        self.write("reviewed.md", "Staged user content.\n")
        _ = self.git("add", "reviewed.md")
        self.write("reviewed.md", "Unstaged user content.\n")
        self.write("unrelated.md", "Pending vault text.\n")
        code, output, errors = self.command()
        self.assertEqual(code, 0, errors)
        self.assertIn("the index has staged changes", output)
        self.assertEqual(self.git("show", ":reviewed.md"), "Staged user content.")
        self.assertEqual(self.git("status", "--short"), "AM reviewed.md\n?? unrelated.md")

    def test_vault_save_defers_during_bisect(self) -> None:
        self.mark_first_save_published()
        _ = self.git("bisect", "start", "--no-checkout")
        head = self.git("rev-parse", "HEAD")
        self.assertEqual(self.git("branch", "--show-current"), "main")
        self.write("pending.md", "Keep this change pending.\n")
        code, output, errors = self.command()
        self.assertEqual(code, 0, errors)
        self.assertIn("deferred", output)
        self.assertEqual(self.git("rev-parse", "HEAD"), head)

    def test_vault_save_rejected_scan_leaves_index_and_objects_unchanged(self) -> None:
        self.write("bob/note.md", "Fixture text.\n")
        index = self.vault / ".git/index"
        manifest = self.vault / ".git/vault-save-manifest.json"
        original = index.read_bytes()
        list_objects = ("cat-file", "--batch-all-objects", "--batch-check=%(objectname)")
        objects = self.git(*list_objects)
        for failure, succeeded in (("Secret found.", []), ("Secret found.", [None]), ("Scan failed.", [None])):
            with self.subTest(failure=failure, succeeded=len(succeeded)):
                with patch.object(vault_save, "scan", side_effect=[*succeeded, vault_save.SaveError(failure)]) as scan:
                    code, _, errors = self.command("first-save", "scan")
                self.assertEqual(scan.call_count, len(succeeded) + 1)
                self.assertEqual(code, 1)
                self.assertIn(failure, errors)
                self.assertEqual(index.read_bytes(), original)
                self.assertEqual(self.git(*list_objects), objects)
                self.assertFalse(manifest.exists())
                self.assertFalse(manifest.with_name("vault-save-work").exists())

    def test_vault_save_checks_first_push(self) -> None:
        origin = self.git("rev-parse", "origin/main")
        _ = self.git("config", "filter.fixture.clean", "sed '/^derived=/d'")
        self.write(".gitattributes", "bob/note.md filter=fixture\n")
        self.write("older.md", "A local older commit.\n")
        _ = self.git("add", ".gitattributes", "older.md")
        _ = self.git("commit", "-m", "Fixture outgoing commit")
        head = self.git("rev-parse", "HEAD")
        index = self.vault / ".git/index"
        manifest = self.vault / ".git/vault-save-manifest.json"
        work = manifest.with_name("vault-save-work")
        stale = work / "stopped-scan/snapshot/old.md"
        stale.parent.mkdir(parents=True)
        _ = stale.write_text("An old copy.\n", encoding="utf-8")
        original = index.read_bytes()
        self.write("bob/note.md", "derived=old\nReviewed content.\n")
        (self.vault / "remove.md").unlink()
        calls: list[tuple[Path, tuple[str, ...]]] = []

        def scan(folder: Path, *args: str) -> None:
            calls.append((folder, args))
            if args[0] == "dir":
                self.assertEqual((folder / "files/bob/note.md").read_bytes(), b"derived=old\nReviewed content.\n")
                self.assertEqual((folder / "blobs/bob/note.md").read_bytes(), b"Reviewed content.\n")
                self.assertEqual(folder.parent.parent, work)
                self.assertFalse(stale.exists())
            self.assertEqual(index.read_bytes(), original)
            self.assertFalse(manifest.exists())

        with patch.object(vault_save, "scan", side_effect=scan):
            code, output, errors = self.command("first-save", "scan")
        self.assertEqual(code, 0, errors)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][1], ("dir", "."))
        self.assertFalse(work.exists())
        self.assertEqual(calls[1], (self.vault, ("git", f"--log-opts=--diff-merges=first-parent {origin}..{head}", ".")))
        self.assertEqual(index.read_bytes(), original)
        digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
        self.assertIn("Scanned 3 paths.", output)
        self.assertIn(f"Manifest {manifest}", output)
        self.assertIn(f"Manifest hash {digest}", output)
        self.assertEqual(self.command("first-save", "commit", "0" * 64)[0], 1)
        list_objects = ("cat-file", "--batch-all-objects", "--batch-check=%(objectname)")
        objects = self.git(*list_objects)
        _ = self.git("config", "filter.fixture.clean", "cat")
        code, _, errors = self.command("first-save", "commit", digest)
        self.assertEqual(code, 1)
        self.assertIn("filtered blob for 'bob/note.md' differs", errors)
        self.assertEqual(index.read_bytes(), original)
        self.assertEqual(self.git(*list_objects), objects)
        self.assertFalse((self.vault / "remove.md").exists())
        self.assertEqual((self.vault / "bob/note.md").read_bytes(), b"derived=old\nReviewed content.\n")
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertEqual(self.git("rev-parse", "main", repo=self.origin), origin)
        _ = self.git("config", "filter.fixture.clean", "sed '/^derived=/d'")
        self.write("bob/note.md", "derived=new\nReviewed content.\n")
        code, _, errors = self.command("first-save", "commit", digest)
        self.assertEqual(code, 1)
        self.assertIn("changed after the scan", errors)
        self.assertEqual(index.read_bytes(), original)
        self.write("bob/note.md", "derived=old\nReviewed content.\n")
        _ = self.git("config", "core.filemode", "true")
        (self.vault / "bob/note.md").chmod(0o755)
        code, _, errors = self.command("first-save", "commit", digest)
        self.assertEqual(code, 1)
        self.assertIn("mode or deletion for 'bob/note.md' differs", errors)
        self.assertEqual(index.read_bytes(), original)
        _ = self.git("config", "core.filemode", "false")
        code, _, errors = self.command("first-save", "commit", digest)
        self.assertEqual(code, 0, errors)
        self.assertEqual(self.git("rev-parse", "HEAD"), self.git("rev-parse", "main", repo=self.origin))
        self.assertEqual(self.git("show", "HEAD:bob/note.md"), "Reviewed content.")
        self.assertEqual(self.git("ls-tree", "HEAD", "remove.md"), "")
        self.assertEqual(self.git("diff", "--name-only", "HEAD"), "")
        self.assertEqual(scratch_ls_files(self.vault, "--others", "--exclude-standard").stdout, "")
        self.assertFalse(manifest.exists())

    def interrupt_before_index_sync(self, digest: str) -> tuple[int, str, str]:
        update = vault_save.update_index

        def stop_before_live_update(vault: Path, review: vault_save.Review, *,
                                    env: dict[str, str] | None = None) -> None:
            if env is None:
                raise vault_save.SaveError("Stop before index sync.")
            update(vault, review, env=env)

        with patch.object(vault_save, "update_index", side_effect=stop_before_live_update):
            return self.command("first-save", "commit", digest)

    def test_vault_save_retries_after_moving_main(self) -> None:
        name = "save\twith\nspaces.md"
        self.write(name, "First save.\n")
        digest = self.scan()
        base = self.git("rev-parse", "HEAD")
        manifest = self.vault / ".git/vault-save-manifest.json"
        state = manifest.with_suffix(".state")
        reviewed = manifest.read_bytes()
        code, _, errors = self.interrupt_before_index_sync(digest)
        self.assertEqual(code, 1)
        self.assertIn("Stop before index sync.", errors)
        commit = self.git("rev-parse", "HEAD")
        self.assertEqual(self.git("rev-parse", "HEAD^"), base)
        self.assertEqual(state.read_text().splitlines(), [digest, commit])
        self.assertEqual(self.git("rev-parse", "main", repo=self.origin), base)
        self.assertEqual(self.git("show", f"HEAD:{name}"), "First save.")
        self.assertEqual(self.git_output("diff", "--cached", "--name-only", "-z"), os.fsencode(name) + b"\0")
        code, output, errors = self.command()
        self.assertEqual(code, 0, errors)
        self.assertEqual(output, "Vault save waits for the reviewed first save.\n")
        with patch.object(vault_save, "scan") as scan:
            code, _, errors = self.command("first-save", "scan")
        self.assertEqual(code, 1)
        self.assertIn("index has staged changes", errors)
        scan.assert_not_called()
        self.assertEqual(manifest.read_bytes(), reviewed)
        self.assertEqual(state.read_text().splitlines(), [digest, commit])
        self.write("job.md", "A later job commit.\n")
        _ = self.git("add", "job.md")
        _ = self.git("commit", "--only", "-m", "Save a job note", "--", "job.md")
        head = self.git("rev-parse", "HEAD")
        self.assertEqual(self.git("rev-parse", "HEAD^"), commit)
        self.write(name, "An edit after the commit.\n")
        with patch.object(vault_save, "create_reviewed_commit") as create:
            code, _, errors = self.command("first-save", "commit", digest)
        self.assertEqual(code, 0, errors)
        create.assert_not_called()
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertEqual(self.git("rev-parse", "main", repo=self.origin), commit)
        self.assertEqual(self.git("diff", "--cached", "--name-only"), "")
        self.assertEqual(self.git("show", f":{name}"), "First save.")
        self.assertEqual((self.vault / name).read_text(), "An edit after the commit.\n")
        self.assertFalse(manifest.exists())
        self.assertFalse(state.exists())

    def test_vault_save_recovery_refuses_changed_manifest_paths(self) -> None:
        self.write("save.md", "First save.\n")
        digest = self.scan()
        origin = self.git("rev-parse", "HEAD")
        self.assertEqual(self.interrupt_before_index_sync(digest)[0], 1)
        self.write("save.md", "An edit in a later commit.\n")
        _ = self.git("add", "save.md")
        _ = self.git("commit", "-m", "Edit the reviewed note")
        with patch.object(vault_save, "update_index") as sync:
            code, _, errors = self.command("first-save", "commit", digest)
        self.assertEqual(code, 1)
        self.assertIn("'save.md' changed in local main", errors)
        sync.assert_not_called()
        self.assertEqual(self.git("rev-parse", "main", repo=self.origin), origin)
        self.assertTrue((self.vault / ".git/vault-save-manifest.state").exists())

    def test_vault_save_retry_preserves_staged_descendant(self) -> None:
        (self.vault / "remove.md").unlink()
        digest = self.scan()
        with patch.object(land_main, "land", side_effect=land_main.LandError("No push.")):
            code, _, errors = self.command("first-save", "commit", digest)
        self.assertEqual(code, 1)
        self.assertIn("No push.", errors)
        commit = self.git("rev-parse", "HEAD")
        name = "remove.md/new.md"
        self.write(name, "Staged after the first save.\n")
        _ = self.git("add", name)
        self.write("job.md", "A later job commit.\n")
        _ = self.git("add", "job.md")
        _ = self.git("commit", "--only", "-m", "Save a job note", "--", "job.md")

        code, _, errors = self.command("first-save", "commit", digest)
        self.assertEqual(code, 0, errors)
        self.assertEqual(self.git("diff", "--cached", "--name-only"), name)
        self.assertEqual(self.git("show", f":{name}"), "Staged after the first save.")
        self.assertEqual(self.git("rev-parse", "main", repo=self.origin), commit)

    def test_vault_save_retries_when_main_returns_to_the_scanned_parent(self) -> None:
        self.write("save.md", "First save.\n")
        digest = self.scan()
        scanned = self.git("rev-parse", "HEAD")
        code, _, errors = self.interrupt_before_index_sync(digest)
        self.assertEqual(code, 1)
        self.assertIn("Stop before index sync.", errors)
        commit = self.git("rev-parse", "HEAD")
        state = self.vault / ".git/vault-save-manifest.state"
        self.assertEqual(state.read_text().splitlines(), [digest, commit])
        _ = self.git("update-ref", "refs/heads/main", scanned, commit)

        with patch.object(vault_save, "create_reviewed_commit") as create:
            code, _, errors = self.command("first-save", "commit", digest)
        self.assertEqual(code, 0, errors)
        create.assert_not_called()
        self.assertEqual(self.git("rev-parse", "HEAD"), commit)
        self.assertEqual(self.git("rev-parse", "main", repo=self.origin), commit)
        self.assertEqual(self.git("diff", "--cached", "--name-only"), "")
        self.assertFalse(state.exists())

    def test_vault_save_preserves_a_concurrent_git_add(self) -> None:
        self.write("save.md", "First save.\n")
        digest = self.scan()
        checked = vault_save.checked

        def add_before_ref_update(vault: Path, *args: str, env: dict[str, str] | None = None) -> str:
            if args[:2] == ("update-ref", "refs/heads/main"):
                self.write("later.md", "Staged separately.\n")
                _ = self.git("add", "later.md")
                self.write("later.md", "Unstaged edit.\n")
            return checked(vault, *args, env=env)

        with patch.object(vault_save, "checked", side_effect=add_before_ref_update):
            code, _, errors = self.command("first-save", "commit", digest)
        self.assertEqual(code, 0, errors)
        self.assertEqual(self.git("rev-parse", "HEAD"), self.git("rev-parse", "main", repo=self.origin))
        self.assertEqual(self.git("diff", "--name-only", "HEAD^", "HEAD"), "save.md")
        self.assertEqual(self.git("diff", "--cached", "--name-only"), "later.md")
        self.assertEqual(self.git("show", ":later.md"), "Staged separately.")
        self.assertEqual((self.vault / "later.md").read_text(), "Unstaged edit.\n")

    def test_first_save_reports_the_recovery_command_for_a_failed_hook(self) -> None:
        self.write("save.md", "First save.\n")
        self.write(".claude/config/post_push.sh", "exit 9\n")
        digest = self.scan()

        code, _, errors = self.command("first-save", "commit", digest)

        self.assertEqual(code, 0, errors)
        commit = self.git("rev-parse", "HEAD")
        self.assertEqual(self.git("rev-parse", "main", repo=self.origin), commit)
        self.assertIn("post-push hook: failed (exit 9); rerun:", errors)
        self.assertIn(f"LANDED_SHA={commit}", errors)

    def test_hanadocs_push_retries(self) -> None:
        self.write("save.md", "First save.\n")
        digest = self.scan()
        hook = self.origin / "hooks/pre-receive"
        _ = hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o700)
        self.assertEqual(self.command("first-save", "commit", digest)[0], 1)
        head = self.git("rev-parse", "HEAD")
        self.assertNotEqual(head, self.git("rev-parse", "main", repo=self.origin))
        self.write("pending.md", "Wait for publication.\n")
        self.assertEqual(self.command(), (0, "Vault save waits for the reviewed first save.\n", ""))
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        # Verify recovery when the state file is missing.
        (self.vault / ".git/vault-save-manifest.state").unlink()
        hook.unlink()
        code, _, errors = self.command("first-save", "commit", digest)
        self.assertEqual(code, 0, errors)
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertEqual(self.git("rev-parse", "main", repo=self.origin), head)
        self.assertEqual(self.git("show-ref", "--hash", "--verify", vault_save.FIRST_SAVE_REF), head)
