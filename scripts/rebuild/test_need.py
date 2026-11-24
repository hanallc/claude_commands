"""Test rebuild needs with scratch folders and scratch Git repositories."""

from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from pathlib import Path
from typing import cast, override
from unittest.mock import patch

from scripts.rebuild import need


class NeedTests(unittest.TestCase):
    """Keep every need record and Git operation inside a scratch folder."""

    root: Path = Path()
    folder: Path = Path()

    @override
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.folder = self.root / "needs"
        self.enterContext(patch.dict(os.environ, {"REBUILD_NEEDS_DIR": str(self.folder),
                                                  "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}))

    def command(self, *arguments: str) -> str:
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(need.main(list(arguments)), 0)
        return output.getvalue()

    def record(self, machine: need.Machine, commit: str) -> need.NeedRecord:
        return cast(need.NeedRecord, json.loads(
            (self.folder / f"{machine}-{commit}.json").read_text(encoding="utf-8")))

    def test_add_and_same_key_rerun(self) -> None:
        commit = "a" * 40
        _ = self.command("add", "--machine", "natedev", "--commit", commit,
                         "--reason", "Apply the settings.", "--concerns", "staff-a",
                         "--requested-by", "staff-b")
        filename = self.folder / f"natedev-{commit}.json"
        before = filename.read_bytes(), filename.stat().st_mtime_ns
        record = self.record("natedev", commit)
        self.assertEqual(set(record), {"id", "machine", "nixos_commit", "reason", "concerns",
                                       "requested_by", "created_at"})
        self.assertEqual(record["requested_by"], "staff-b")
        self.assertEqual(record["concerns"], "staff-a")
        self.assertEqual(record["reason"], "Apply the settings.")
        self.assertEqual(record["nixos_commit"], commit)
        self.assertEqual(record["machine"], "natedev")
        _ = datetime.fromisoformat(record["created_at"])
        need.add_need("natedev", commit, "A different reason.", "staff-c")
        self.assertEqual((filename.read_bytes(), filename.stat().st_mtime_ns), before)
        need.add_need("mac", commit, "Apply the Mac settings.", "staff-a")
        self.assertEqual(self.record("mac", commit)["requested_by"], "")
        self.assertEqual(len(list(self.folder.glob("*.json"))), 2)
        self.assertEqual(list(self.folder.glob("*.tmp")), [])

    def test_add_refuses_a_missing_or_blank_reason(self) -> None:
        args = ["add", "--machine", "natedev", "--commit", "a" * 40, "--concerns", "staff-a"]
        for reason in ([], ["--reason", " \n\t"]):
            with self.subTest(reason=reason), redirect_stderr(io.StringIO()) as error:
                with self.assertRaises(SystemExit) as stopped:
                    _ = need.main([*args, *reason])
                self.assertEqual(stopped.exception.code, 2)
                self.assertIn("--reason", error.getvalue())
        with self.assertRaisesRegex(ValueError, "--reason"):
            need.add_need("natedev", "a" * 40, "", "staff-a")
        self.assertFalse(self.folder.exists())

    def test_add_refuses_short_and_malformed_commits(self) -> None:
        for commit in ("a" * 7, "g" * 40, "a" * 64):
            with self.subTest(commit=commit), self.assertRaisesRegex(ValueError, "full 40-character Git hash"):
                need.add_need("natedev", commit, "Apply the settings.", "staff-a")
        self.assertFalse(self.folder.exists())

    def test_list_newest_per_machine_and_joined_reasons_without_subprocess(self) -> None:
        need.add_need("natedev", "a" * 40, "Apply the first settings.", "staff-a")
        need.add_need("natedev", "b" * 40, "Apply the second settings.", "staff-b")
        need.add_need("mac", "c" * 40, "Apply the Mac settings.", "staff-c")
        for commit, created_at in (("a" * 40, "2026-01-02T00:00:00+00:00"),
                                   ("b" * 40, "2026-01-01T23:00:00-02:00")):
            record = self.record("natedev", commit)
            record["created_at"] = created_at
            _ = (self.folder / f"natedev-{commit}.json").write_text(json.dumps(record), encoding="utf-8")
        done = self.folder / "done"
        done.mkdir()
        _ = (done / "ignored.json").write_text("invalid JSON", encoding="utf-8")
        with patch("subprocess.run", side_effect=AssertionError("A reader called a subprocess.")), \
                patch("subprocess.Popen", side_effect=AssertionError("A reader started a subprocess.")):
            summaries = need.read_needs()
            self.assertEqual(len(summaries), 2)
            local = next(summary for summary in summaries if summary.newest["machine"] == "natedev")
            self.assertEqual(local.newest["nixos_commit"], "b" * 40)
            self.assertEqual(local.reasons, ("Apply the first settings.", "Apply the second settings."))
            self.assertEqual(local.records, (self.record("natedev", "a" * 40), self.record("natedev", "b" * 40)))
            self.assertEqual([record["concerns"] for record in local.records], ["staff-a", "staff-b"])
            rows = cast(list[need.NeedRecord], json.loads(self.command("list", "--json")))
            row = next(record for record in rows if record["machine"] == "natedev")
            self.assertEqual(row["reason"], "Apply the first settings. Apply the second settings.")
            self.assertIn(f"natedev {'b' * 40} {row['reason']}", self.command("list"))

    def test_list_missing_folder_is_empty(self) -> None:
        self.assertEqual(need.read_needs(), [])
        self.assertEqual(self.command("list", "--json"), "[]\n")
        self.assertFalse(self.folder.exists())

    def git(self, *arguments: str) -> str:
        result = subprocess.run(["git", "-C", str(self.root), *arguments], check=True,
                                capture_output=True, text=True,
                                env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                                     "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
                                     "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.invalid"})
        return result.stdout.strip()

    def history(self) -> tuple[str, str]:
        _ = self.git("init", "--quiet")
        _ = self.git("-c", "commit.gpgsign=false", "commit", "--quiet", "--allow-empty", "-m", "First settings")
        ancestor = self.git("rev-parse", "HEAD")
        _ = self.git("-c", "commit.gpgsign=false", "commit", "--quiet", "--allow-empty", "-m", "Second settings")
        return ancestor, self.git("rev-parse", "HEAD")

    def clear(self, machine: need.Machine, *lines: str, generation: int = 42,
              activated: str = "2026-11-12T12:34:55.123456789-0500") -> str:
        generations = self.root / "generations.log"
        _ = generations.write_text("\n".join(lines) + "\n", encoding="utf-8")
        profile_at = "2026-11-12T12:34:50-0500"
        if machine == "mac":
            activated = str(datetime.fromisoformat(activated).timestamp())
            profile_at = str(datetime.fromisoformat(profile_at).timestamp())
        proof = (f"/nix/store/test-system\nactivated {activated} 100\n"
                 f"generation system-{generation}-link {profile_at} 101\n--- generations ---")
        output = need.command_output

        def fixture_output(args: list[str]) -> str:
            return output(args) if args[0] == "git" else proof

        with patch.object(need, "command_output", side_effect=fixture_output) as probe:
            result = self.command("clear", "--machine", machine, "--generations", str(generations),
                                  "--nixos-repo", str(self.root))
        self.assertEqual(probe.call_args_list[0].args[0][0], "sh" if machine == "natedev" else "ssh")
        return result

    def log_line(self, commit: str, action: str = "switch") -> str:
        return f"2026-11-12T12:34:56-0500 {action} system-42-link {commit} /nix/store/test-system"

    def legacy_need(self, commit: str, prefix: str) -> None:
        source = self.folder / f"natedev-{commit}.json"
        legacy: need.NeedRecord = {**self.record("natedev", commit), "id": f"natedev-{prefix}", "nixos_commit": prefix}
        _ = source.rename(self.folder / f"natedev-{prefix}.json").write_text(json.dumps(legacy))

    def test_clear_ancestor_moves_record_with_log_line(self) -> None:
        ancestor, descendant = self.history()
        need.add_need("natedev", ancestor, "Apply the first settings.", "staff-a")
        need.add_need("natedev", descendant, "Apply the second settings.", "staff-b")
        need.add_need("mac", ancestor, "Apply the Mac settings.", "staff-c")
        line = self.log_line(descendant)
        self.assertEqual(self.clear("natedev", line), "")
        for commit in (ancestor, descendant):
            filename = f"natedev-{commit}.json"
            self.assertFalse((self.folder / filename).exists())
            archived = cast(need.CompletedNeedRecord, json.loads((self.folder / "done" / filename).read_text()))
            self.assertEqual(archived["cleared_by"], line)
            before = (self.folder / "done" / filename).read_bytes()
            need.add_need("natedev", commit, "A repeated request.", "staff-c")
            self.assertFalse((self.folder / filename).exists())
            self.assertEqual((self.folder / "done" / filename).read_bytes(), before)
        self.assertEqual(self.record("mac", ancestor)["reason"], "Apply the Mac settings.")
        self.assertEqual(self.clear("mac", line), "")
        mac = self.folder / "done" / f"mac-{ancestor}.json"
        self.assertTrue(mac.exists())

    def test_clear_keeps_dirty_active_revision_open(self) -> None:
        ancestor, descendant = self.history()
        need.add_need("mac", ancestor, "Apply the settings.", "staff-a")
        line = self.log_line(descendant + "-dirty")
        self.assertEqual(self.clear("mac", self.log_line(descendant), line), "")
        self.assertEqual(self.record("mac", ancestor)["nixos_commit"], ancestor)
        self.assertFalse((self.folder / "done").exists())

    def test_clear_keeps_newer_unlogged_activation_of_same_store_open(self) -> None:
        ancestor, descendant = self.history()
        need.add_need("natedev", ancestor, "Apply the settings.", "staff-a")
        for generation, at in ((43, "2026-11-12T12:34:55-0500"), (42, "2026-11-12T12:34:56.1-0500")):
            with self.subTest(generation=generation):
                self.assertEqual(self.clear("natedev", self.log_line(descendant), generation=generation, activated=at), "")
                self.assertEqual(self.record("natedev", ancestor)["nixos_commit"], ancestor)
        self.assertFalse((self.folder / "done").exists())

    def test_clear_resolves_legacy_short_commits_and_skips_bad_records(self) -> None:
        ancestor, descendant = self.history()
        need.add_need("natedev", ancestor, "Apply the first settings.", "staff-a")
        need.add_need("natedev", descendant, "Apply the second settings.", "staff-a")
        short = ancestor[:12]
        self.legacy_need(ancestor, short)
        bad = self.folder / "bad.json"
        _ = bad.write_text("{", encoding="utf-8")
        with redirect_stderr(io.StringIO()) as error:
            self.assertEqual(self.clear("natedev", self.log_line(descendant)), "")
        self.assertEqual(error.getvalue().splitlines()[0].split(":")[0], "Rebuild request bad.json")
        self.assertEqual(len(error.getvalue().splitlines()), 1)
        completed = need.read_completed_needs()
        self.assertEqual({record["nixos_commit"] for record in completed}, {ancestor, descendant})
        self.assertTrue((self.folder / "done" / f"natedev-{ancestor}.json").exists())
        need.add_need("natedev", ancestor, "A repeated request.", "staff-a")
        self.assertEqual(list(self.folder.glob("*.json")), [bad])

    def test_clear_refuses_a_branch_named_for_a_short_hash(self) -> None:
        ancestor, descendant = self.history()
        prefix = descendant[:12]
        need.add_need("natedev", descendant, "Apply the second settings.", "staff-a")
        self.legacy_need(descendant, prefix)
        _ = self.git("branch", prefix, ancestor)
        self.assertEqual(self.git("rev-parse", "--verify", f"{prefix}^{{commit}}"), ancestor)
        self.assertEqual(self.clear("natedev", self.log_line(ancestor)), "")
        self.assertEqual(self.record("natedev", prefix)["nixos_commit"], prefix)
        self.assertEqual(need.read_completed_needs(), ())

    def test_malformed_probe_log_cannot_supply_embedded_revision(self) -> None:
        running = "/nix/store/test-system"
        commit = "a" * 40
        probe = f"{running}\nactivated 2026-11-12T12:34:55-0500 100\n--- generations ---\nrevision {commit}"
        self.assertEqual(need.active_system(probe), need.ActiveSystem(running))

    def test_clear_unknown_commit_keeps_needs_and_reports_one_anomaly(self) -> None:
        ancestor, descendant = self.history()
        need.add_need("natedev", ancestor, "Apply the first settings.", "staff-a")
        need.add_need("natedev", descendant, "Apply the second settings.", "staff-b")
        unknown = "f" * 40
        line = self.log_line(unknown)
        output = self.clear("natedev", line, line)
        self.assertEqual(output, f"anomaly: unknown logged commit {unknown}\n")
        self.assertEqual(len(list(self.folder.glob("*.json"))), 2)
        self.assertFalse((self.folder / "done").exists())

    def test_clear_unknown_need_commit_keeps_need_and_reports_one_anomaly(self) -> None:
        _ancestor, descendant = self.history()
        unknown = "f" * 40
        need.add_need("natedev", unknown, "Apply the settings.", "staff-a")
        line = self.log_line(descendant)
        output = self.clear("natedev", line, line)
        self.assertEqual(output, f"anomaly: unknown need commit {unknown} (need natedev-{unknown})\n")
        self.assertEqual(self.record("natedev", unknown)["nixos_commit"], unknown)
        self.assertFalse((self.folder / "done").exists())

    def test_clear_non_ancestor_keeps_need(self) -> None:
        ancestor, descendant = self.history()
        need.add_need("natedev", descendant, "Apply the second settings.", "staff-a")
        self.assertEqual(self.clear("natedev", self.log_line(ancestor)), "")
        self.assertTrue((self.folder / f"natedev-{descendant}.json").exists())

    def test_clear_ignores_boot_and_test(self) -> None:
        ancestor, descendant = self.history()
        need.add_need("natedev", ancestor, "Apply the settings.", "staff-a")
        self.assertEqual(self.clear("natedev", self.log_line(descendant, "boot"),
                                    self.log_line(descendant, "test")), "")
        self.assertTrue((self.folder / f"natedev-{ancestor}.json").exists())


if __name__ == "__main__":
    _ = unittest.main()
