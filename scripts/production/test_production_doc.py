"""Production readers use explicit names and one document format."""
from __future__ import annotations

import json
import subprocess
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from scripts.lib.git_test_support import commit_repository, fixture_git_environment
from scripts.production import production_doc, showrunners, staff
from scripts.lib.user_zone import user_zone
from scripts.production.staff_test_support import StaffTestCase, fixture_member


def enrolled_document(doc: Path, main: Path, integration: Path, branch: str, *, log: str = "production.log") -> None:
    doc.parent.mkdir(parents=True, exist_ok=True)
    _ = doc.write_text(
        f"- **{production_doc.MERGE_CHECKOUT_FIELD}:** `{main}`\n"
        + f"- **Repository:** `{main}`\n- **Showrunner checkout:** `{integration}`\n"
        + f"- **Merge branch:** `{branch}`\n- **Log:** `{log}`\n")


class ProductionDocumentTests(StaffTestCase):
    def test_new_document_keeps_its_explicit_name_before_staff_publication(self) -> None:
        document = self.root / "different-production.md"
        _ = document.write_text("# New production\n")
        with document.open("a") as stream:
            _ = stream.write(f"\n- **Merge branch:** `main`\n"
                             + f"- **Showrunner checkout:** `{self.root}`\n- **Log:** `log.md`\n")
        production = production_doc.read_new_production(document, "new-showrunner")
        self.assertEqual((production.name, production.doc, production.checkout, production.log),
                         ("new-showrunner", document, self.root, self.root / "log.md"))
        self.assertIsNone(staff.initial_snapshot().by_path("new-showrunner"))

    def test_existing_document_uses_the_same_fields_with_its_staff_name(self) -> None:
        document = self.root / "different-production.md"
        _ = document.write_text(f"- **Merge branch:** `release`\n"
                                + f"- **Showrunner checkout:** `{self.root}`\n- **Log:** `nested/log.md`\n")
        row = fixture_member("showrunner", name="retained-name", doc=staff.LiveDoc("natedev", document))
        expected = production_doc.Production(document, row.name, "release", self.root,
                                             self.root / "nested/log.md", user_zone(), row)
        self.assertEqual(production_doc.read_production(row.name, staff.snapshot()), expected)
        wrapped = staff.set_doc(row.id, staff.WrappedDoc("natedev", document))
        self.assertEqual(production_doc.read_production(row.name, staff.snapshot()), replace(expected, owner=wrapped))

    def test_both_readers_refuse_an_absolute_log_path(self) -> None:
        document = self.root / "different-production.md"
        _ = document.write_text(f"- **Merge branch:** `main`\n"
                                + f"- **Showrunner checkout:** `{self.root}`\n- **Log:** `/outside/log.md`\n")
        row = fixture_member("showrunner", name="retained-name", doc=staff.LiveDoc("natedev", document))
        with self.assertRaisesRegex(production_doc.Refusal, "Log must be relative"):
            _ = production_doc.read_new_production(document, row.name)
        with self.assertRaisesRegex(production_doc.Refusal, "Log must be relative"):
            _ = production_doc.read_production(row.name, staff.snapshot())

    def test_enrolled_documents_share_main_and_keep_distinct_live_paths(self) -> None:
        repository = self.root / "main"
        environment = fixture_git_environment(self.root)
        _ = subprocess.run(["git", "init", "-qb", "main", str(repository)], env=environment, check=True)
        _ = commit_repository(repository, "Fixture", allow_empty=True, env=environment)
        for name in ("alpha", "beta"):
            with self.subTest(production=name):
                integration = self.root / name
                _ = subprocess.run(["git", "-C", str(repository), "worktree", "add", "-qb", name, str(integration)],
                                   env=environment, check=True)
                document = repository / f"{name}-production-live.md"
                old = integration / f"{name}-production.md"
                _ = old.write_text("# Legacy production\n")
                owner = fixture_member("showrunner", name=name, doc=staff.LiveDoc("natedev", old))
                self.assertIsInstance(production_doc.resolve_production_paths(old), production_doc.LegacyProduction)
                enrolled_document(document, repository, integration, name, log=f"logs/{name}.md")
                paths = production_doc.EnrolledPaths(repository, document, repository / f"logs/{name}.md",
                    repository / production_doc.NOTES_DIRECTORY / owner.id, integration)
                self.assertEqual(production_doc.resolve_production_paths(document), paths)
                _ = staff.set_doc(owner.id, replace(owner.doc, path=document) if isinstance(owner.doc, staff.LiveDoc)
                                  else staff.LiveDoc("natedev", document))
                self.assertEqual(production_doc.resolve_production_paths(document), paths)
                parsed = production_doc.read_production(owner.id)
                self.assertEqual((parsed.checkout, parsed.log), (integration, paths.log))

    def test_guard_ready_requires_current_registrations_and_a_revision_in_main(self) -> None:
        repo = self.root / "installed"
        environment = fixture_git_environment(self.root)
        _ = subprocess.run(["git", "init", "-qb", "main", str(repo)], env=environment, check=True)
        revision = commit_repository(repo, "Install guard", allow_empty=True, env=environment)
        _ = subprocess.run(["git", "-C", str(repo), "switch", "-qc", "other"], env=environment, check=True)
        other = commit_repository(repo, "Other change", allow_empty=True, env=environment)
        state = self.root / "guard"
        state.mkdir()
        codex_home = self.root / "codex"
        codex_home.mkdir()
        settings = self.root / "settings.json"
        events = ("PreToolUse", "PostToolUse")
        claude = [{"settings": str(settings), "event": event, "matcher": "Bash", "command": f"claude-{event}"}
                  for event in events]
        codex = [{"codex_home": str(codex_home), "event": event, "tool": tool, "command": f"codex-{event}-{tool}"}
                 for event in events for tool in ("Bash", "apply_patch")]

        def registrations(entries: list[dict[str, str]], match_field: str) -> str:
            return json.dumps({"hooks": {event: [
                {"matcher": entry[match_field], "hooks": [{"type": "command", "command": entry["command"]}]}
                for entry in entries if entry["event"] == event] for event in events}})

        _ = settings.write_text(registrations(claude, "matcher"))
        hooks = codex_home / "hooks.json"
        proof = state / production_doc.GUARD_READY_FILE
        with patch.object(showrunners, "SHOWRUNNER_STATE_DIR", state), patch.object(production_doc, "GUARD_REPOSITORY", repo):
            for tip, drift, ready in ((revision, False, True), (revision, True, False), (other, False, False)):
                with self.subTest(revision=tip, drift=drift):
                    installed = registrations(codex, "tool")
                    _ = hooks.write_text(installed.replace("codex-PreToolUse-Bash", "codex-PreToolUse-Bash ")
                                         if drift else installed)
                    _ = proof.write_text(json.dumps({"version": 1, "revision": tip, "recorded_at": "2026-11-20T00:00:00Z",
                                                     "claude": claude, "codex": codex}))
                    self.assertEqual(isinstance(production_doc.read_guard_ready(), production_doc.GuardReady), ready)


if __name__ == "__main__":
    _ = unittest.main()
