#!/usr/bin/env python3
"""Tests for proposal_issue.py: the proposal verbs that edit one issue's frontmatter."""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import io
import json
import threading
import time
import os
import tempfile
import unittest
from collections.abc import Generator
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from typing import cast, final, override
from unittest import mock

from scripts.console_text.test_support import accepting_console_text
from scripts.agents.remote import local_machine
from scripts.notify import review
from scripts.production import proposal_issue
from scripts.production.issues import Issue, frontmatter
from scripts.production import proposals_folder, record_store, staff, unit_names
from scripts.production.staff_test_support import fixture_runtime_id
from scripts.lib.user_zone import user_zone
from scripts.review_links import new_item_dialog, reply_tokens
from scripts.review_links.review_events import NoMark, ReviewMark
from scripts.style import rules

ISSUE = """---
area: "[[tooling]]"
category:
  - "[[issue structure#feature|feature]]"
date_created: "[[2026-10-01]]"
date_modified: "[[2026-10-01]]"
plan: ""
project: "[[hana]]"
proposed_by: "build-report"
return:
  - "[[issues - hana.base]]"
  - "[[issues.base]]"
stage: backlog
status: open
tags:
- issue
- proposal
---
# body

status: open stays in the body
"""

# Tags as the vault writes them, indented, which issues.frontmatter reads as a list.
VAULT_ISSUE = ISSUE.replace("tags:\n- issue\n- proposal\n", "tags:\n  - issue\n  - proposal\n")


@final
class ProposalIssueTests(unittest.TestCase):
    root: Path = Path()
    runtime: Path = Path()
    real_calling_showrunner = staticmethod(proposal_issue.calling_showrunner)
    issue: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.issue = self.root / "a proposal.md"
        _ = self.issue.write_text(ISSUE, encoding="utf-8")
        home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.runtime = home / ".local/state/proposal-issue/locks"
        _ = self.enterContext(mock.patch.object(Path, "home", return_value=home))
        self.enterContext(accepting_console_text())
        _ = self.enterContext(mock.patch.object(reply_tokens, "platform", "linux"))
        self.enterContext(mock.patch.dict(os.environ, {"USER_ZONE": "America/New_York", "XDG_RUNTIME_DIR": str(self.runtime),
                                                     "XDG_STATE_HOME": str(home / "key-state"),
                                                     "NOTIFY_STATE_DIR": str(home / "notify"),
                                                     "SHOWRUNNER_STATE_DIR": str(home / "showrunners"),
                                                     "REVIEW_LINKS_ROOT": str(home / "review-links")}))
        members: tuple[tuple[str, staff.Kind], ...] = (
            ("hana", "showrunner"), ("build-report", "unit"), ("u", "unit"),
            ("names", "unit"), ("style", "showrunner"), ("sample-style", "showrunner"))
        rows = tuple(staff.StaffRow(staff.StaffId(fixture_runtime_id(name)), kind,
                                    unit_names.name(name) if kind == "unit" else name,
                                    staff.ReportsTo(staff.StaffId(fixture_runtime_id("hana"))) if kind == "unit" else staff.StaffRoot(),
                                    current_session=staff.StaffId(fixture_runtime_id(name)),
                                    unit_type=staff.UnitDetails("direct") if kind == "unit" else staff.NotUnit())
                     for name, kind in members)
        _ = self.enterContext(mock.patch.object(staff, "snapshot", return_value=staff.StaffSnapshot(rows)))
        _ = self.enterContext(mock.patch.object(proposal_issue, "calling_showrunner", return_value=fixture_runtime_id("hana")))

    def test_review_filing_uses_the_existing_number_allocator_and_reads_the_archive(self) -> None:
        folder = self.root / "issues"
        folder.mkdir()
        original = folder / self.issue.name
        _ = original.write_text(VAULT_ISSUE)
        self.assertEqual(proposal_issue.number([original], date(2026, 11, 17)), ["number a proposal.md: P1"])
        recipient = next(row.id for row in staff.snapshot().rows if row.name == "hana")
        token = "r" + "b" * 42
        details = new_item_dialog.ReviewSubmission("request-1", "a" * 64, new_item_dialog.NoEvidence(), (), token)
        with self.assertRaisesRegex(proposal_issue.Refusal, "omit-file-path: /tmp/layout.png"):
            _ = proposal_issue.file_review_proposal(folder, recipient, "Next proposal", "Read /tmp/layout.png.",
                                                    "hana answers this", details)
        self.assertEqual(list(folder.glob("*.md")), [original])
        issue = proposal_issue.file_review_proposal(folder, recipient, "Next proposal", "Words",
                                                    "hana answers this", details)
        self.assertEqual(issue.value("proposal_id"), "P2")
        note = issue.path.read_text(encoding="utf-8")
        self.assertNotIn(token, note)
        self.assertIn(reply_tokens.digest(token), note)
        self.assertEqual(proposal_issue.number([issue.path], date(2026, 11, 17)), ["number next proposal.md: P2"])
        archive = self.root / proposal_issue.ARCHIVE
        archive.mkdir(parents=True)
        archived = issue.path.rename(archive / issue.path.name)
        saved = proposal_issue.find_review_proposal(folder, recipient, new_item_dialog.ByRequestId(details.request_id))
        assert saved is not None
        self.assertEqual(saved.path, archived)
        self.assertEqual(proposal_issue.review_submission(saved), details)

    def test_f099_proposal_filing_reports_the_missing_document_before_writing(self) -> None:
        recipient, other, *_ = staff.snapshot().find(kind="showrunner")
        document = self.root / "other-production.md"
        other = replace(other, doc=staff.LiveDoc(local_machine(), document))
        folder = self.root / "issues"
        former = self.root / "former"
        former.mkdir()
        legacy = former / "legacy.md"
        _ = legacy.write_text(VAULT_ISSUE.replace("---\n", "---\nproposal_id: P8\n", 1))
        details = new_item_dialog.ReviewSubmission("request-1", "a" * 64, new_item_dialog.NoEvidence(), (), "a" * 43)
        prepared: list[tuple[str, Path]] = []

        def file() -> Issue:
            return proposal_issue.file_review_proposal(folder, recipient.id, "Next proposal", "Words",
                                                        "hana answers this", details,
                                                        before_write=lambda identity, path: prepared.append((identity, path)))

        members = staff.StaffSnapshot((recipient, other))
        with mock.patch.object(staff, "snapshot", return_value=members):
            with self.assertRaisesRegex(proposal_issue.Refusal, r"Cannot check the record folders.*other-production.md"):
                _ = file()
            self.assertEqual(prepared, [])
            self.assertEqual(list(folder.glob("*.md")), [])
            self.assertFalse((record_store.state_root() / "record-ids" / "proposal.water").exists())
            _ = document.write_text(f"- **Proposals:** {former}\n")
            saved = file()
        self.assertEqual(saved.value("proposal_id"), "P9")
        self.assertEqual(prepared, [("P9", saved.path)])

    def test_approve_clears_the_waiting_mark_of_a_review_submission(self) -> None:
        folder = self.root / "issues"
        recipient = next(row.id for row in staff.snapshot().rows if row.name == "hana")
        details = new_item_dialog.ReviewSubmission("request-1", "a" * 64, new_item_dialog.NoEvidence(), (), "a" * 43)
        today = date(2026, 11, 17)
        issue = proposal_issue.file_review_proposal(folder, recipient, "Next proposal", "Words",
                                                    "hana answers this", details)
        changed = proposal_issue.approve(issue.path, {"unit": "names"}, proposal_issue.SHOWRUNNER, "hana", today)
        self.assertIn(proposal_issue.REVIEW_MARK_FIELD, changed)
        approved = Issue(issue.path, frontmatter(issue.path))
        self.assertTrue(proposal_issue.review_is_done(approved))
        self.assertEqual(approved.value(proposal_issue.REVIEW_MARK_FIELD), "")
        key = new_item_dialog.ScopedSubmissionKey(new_item_dialog.SubmissionKind.PROPOSAL, recipient,
                                                   issue.value("proposal_id"), details.request_id)
        self.assertFalse(reply_tokens.path().exists())
        with self.assertRaisesRegex(record_store.SubmissionError, "no longer accepts"):
            _ = proposal_issue.reply_to_submission(issue.path, key, "Stale reply", details.reply_token, owner=key.recipient_id)

    def test_number_replaces_a_legacy_reply_token_with_its_hash(self) -> None:
        token = "r" + "b" * 42
        fields: dict[str, object] = {"request_id": "request-1", "request_digest": "a" * 64,
                                     "evidence": {"kind": "none"}, "images": [], "reply_token": token}
        text, _ = proposal_issue.set_fields(VAULT_ISSUE, {"review_submission": json.dumps(fields)}, self.issue)
        _ = self.issue.write_text(text)

        self.assertEqual(proposal_issue.number([self.issue], date(2026, 11, 17)), ["number a proposal.md: P1"])
        note = self.issue.read_text()
        self.assertNotIn(token, note)
        origin = proposal_issue.review_submission(Issue(self.issue, frontmatter(self.issue)))
        assert isinstance(origin, new_item_dialog.ReviewSubmission)
        self.assertTrue(new_item_dialog.matches_reply_token(origin, token))

    def test_f060_number_scans_the_folder_of_a_selected_numbered_proposal(self) -> None:
        today = date(2026, 11, 17)
        _ = self.issue.write_text(VAULT_ISSUE)
        _ = proposal_issue.number([self.issue], today)
        synced = self.root / "synced" / "already numbered.md"
        synced.parent.mkdir()
        _ = synced.write_text(VAULT_ISSUE.replace("stage: backlog", 'proposal_id: "P2"\nstage: backlog'))
        unnumbered = self.root / "new.md"
        _ = unnumbered.write_text(VAULT_ISSUE)
        self.assertEqual(proposal_issue.number([synced, unnumbered], today),
                         ["number already numbered.md: P2", "number new.md: P3"])

    def test_f061_unreadable_selection_refuses_the_entire_number_batch(self) -> None:
        today = date(2026, 11, 17)
        _ = self.issue.write_text(VAULT_ISSUE)
        _ = proposal_issue.number([self.issue], today)
        new = self.root / "new.md"
        _ = new.write_text(VAULT_ISSUE)
        unreadable = self.root / "other" / "unreadable.md"
        unreadable.parent.mkdir()
        _ = unreadable.write_bytes(b"---\n\xff\n")
        with self.assertRaisesRegex(OSError, "Cannot read record.*unreadable.md"):
            _ = proposal_issue.number([new, unreadable], today)
        self.assertEqual(new.read_text(), VAULT_ISSUE)
        self.assertEqual((record_store.state_root() / "record-ids/proposal.water").read_text(), "1\n")

    def test_f081_create_keeps_a_legacy_decision_with_its_proposer(self) -> None:
        members = staff.snapshot()
        owner = next(row for row in members.rows if row.name == "style")
        other = staff.StaffRow(staff.StaffId("other-style"), "showrunner", "other", staff.StaffRoot(),
                               current_session=staff.StaffId("other-session"))
        _ = self.enterContext(mock.patch.object(staff, "snapshot", return_value=staff.StaffSnapshot((*members.rows, other))))
        template = self.root / "template.yaml"
        _ = template.write_text(VAULT_ISSUE)
        legacy = self.root / "decision.md"
        original = (f"---\nproposed_by: {other.id}\nproposal_id: P7\nstyle_rule: rule\n"
                    "style_crate: crate\nstyle_rule_blob: blob\n---\nThe other production decided.\n")
        _ = legacy.write_text(original)
        filed: list[Path] = []
        created = proposal_issue.create(legacy, "alpha", "a", owner.id, ["style"],
                                        "rule", "crate", "blob", 1, "Words.", template, date(2026, 11, 17),
                                        initial_fields={}, filed_at=filed.append)
        self.assertTrue(created)
        decision, = filed
        self.assertNotEqual(decision, legacy)
        self.assertEqual(frontmatter(decision)["production_id"], [owner.id])
        self.assertEqual(legacy.read_text(), original)

    def test_f062_create_checks_both_folders_under_the_proposal_lock(self) -> None:
        owner = next(row for row in staff.snapshot().rows if row.name == "style")
        former, current = self.root / "former", self.root / "current"
        former.mkdir()
        current.mkdir()
        registry = record_store.state_root() / "review-folders" / owner.id / "proposal.json"
        registry.parent.mkdir(parents=True)
        _ = registry.write_text(json.dumps([str(former), str(current)]))
        template = self.root / "template.yaml"
        _ = template.write_text("---\nstatus: open\n---\n")
        chosen: list[Path] = []

        def create(folder: Path) -> bool:
            return proposal_issue.create(folder / "decision.md", "alpha", "a", owner.id, ["style"],
                                         "rule", "crate", "blob", 1, "Words.", template, date(2026, 11, 17),
                                         initial_fields={}, filed_at=chosen.append)

        lock = proposal_issue.proposal_numbers
        competitor: list[bool] = []

        @contextlib.contextmanager
        def enter_lock() -> Generator[record_store.LockedRecordNumbers]:
            if not competitor:
                competitor.append(False)
                competitor[0] = create(former)
            with lock() as numbers:
                yield numbers

        with mock.patch.object(proposal_issue, "proposal_numbers", side_effect=enter_lock):
            self.assertFalse(create(current))
        self.assertEqual(competitor, [True])
        self.assertEqual(chosen, [former / "decision.md"] * 2)
        self.assertEqual(list(current.glob("*.md")), [])

    def test_f063_create_refuses_an_invalid_result_file_before_publication(self) -> None:
        template = self.root / "template.yaml"
        _ = template.write_text("---\nstatus: open\n---\n")
        body = self.root / "body.txt"
        _ = body.write_text("Words.")
        issue = self.root / "decision.md"
        code, _, _ = self.run_cli("create", str(issue), "--project", "alpha", "--short", "a",
                                  "--proposed-by", "style", "--rule", "rule", "--crate", "crate",
                                  "--rule-blob", "blob", "--count", "1", "--body-file", str(body),
                                  "--template", str(template), "--filed-path-file", str(self.root))
        self.assertEqual(code, 2)
        self.assertFalse(issue.exists())

    def test_f068_proposal_count_ignores_an_archive_index(self) -> None:
        doc = self.root / "production.md"
        folder = self.root / "issues"
        folder.mkdir()
        _ = doc.write_text(f"- **Proposals:** {folder}\n")
        issue = folder / "proposal.md"
        _ = issue.write_text(VAULT_ISSUE)
        archive = folder / "archive"
        archive.mkdir()
        index = archive / "README.md"
        _ = index.write_text("# Archive\n")
        sources: list[Path] = []
        self.assertEqual([item.path for item in proposals_folder.proposal_issues(doc, sources=sources)], [issue])
        self.assertIn(index, sources)

    def test_unit_staff_reference_accepts_the_short_name_and_unit_id(self) -> None:
        row = next(member for member in staff.snapshot().rows if member.name == "names")
        for reference in ("names", "names", row.id):
            with self.subTest(reference=reference):
                self.assertEqual(proposal_issue.staff_reference(reference, "unit"), row)
        with self.assertRaises(proposal_issue.Refusal):
            _ = proposal_issue.staff_reference("names unit", "unit")

    def test_proposal_unit_reads_the_unit_id_from_staff(self) -> None:
        row = next(member for member in staff.snapshot().rows if member.name == "names")
        self.assertEqual(proposal_issue.proposal_unit(Issue(self.issue, {"unit": [row.id]})), "names")
        self.assertEqual(proposal_issue.proposal_unit(Issue(self.issue, {})), "")

    def test_a_stored_unit_name_does_not_select_a_staff_row(self) -> None:
        issue = Issue(self.issue, {"unit": ["retired-types"]})
        result = proposal_issue.proposal_unit(issue)
        self.assertEqual(result, "")
        self.assertIsInstance(rules.style_family(result), rules.NotStyleUnit)

    def test_urgency_and_mark_important_set_the_review_fields_and_normal_removes_them(self) -> None:
        code, out, _ = self.run_cli("urgency", str(self.issue), "--level", "soon", "--due", "2026-10-22")
        self.assertEqual((code, out), (0, f"urgency {self.issue.name}: set review_urgency, review_due, date_modified\n"))
        self.assertIn('review_urgency: "soon"\nreview_due: "2026-10-22"\n', self.issue.read_text())
        self.assertEqual(self.run_cli("urgency", str(self.issue), "--level", "soon", "--due", "2026-10-22")[1],
                         f"urgency {self.issue.name}: unchanged\n")
        self.assertEqual(self.run_cli("urgency", str(self.issue), "--level", "blocks", "--due", "2026-10-22")[0], 2)
        self.assertEqual(self.run_cli("mark-important", str(self.issue))[0], 0)
        text = self.issue.read_text()
        self.assertIn('review_urgency: "user"\nreview_marked: "', text)
        self.assertNotIn("review_due", text)
        _ = self.run_cli("urgency", str(self.issue), "--level", "normal")
        self.assertNotIn("review_", self.issue.read_text())

    def test_number_reviews_a_private_draft_once_and_refuses_changes_before_publication(self) -> None:
        draft = self.root / "draft.md"
        destination = self.root / "new proposal.md"
        initial = VAULT_ISSUE + "\nUse the new layout.\n"
        _ = draft.write_text(initial)
        arguments = ("number", "--draft-file", str(draft), str(destination))

        with (mock.patch.object(proposal_issue, "issue_lock", wraps=proposal_issue.issue_lock) as issue_lock,
              mock.patch.object(proposal_issue, "proposal_numbers", wraps=proposal_issue.proposal_numbers) as numbers):
            def approve(_key: review.QuestionKey, text: str, **_options: object) -> review.QuestionAccepted:
                issue_lock.assert_not_called()
                numbers.assert_not_called()
                self.assertFalse(destination.exists())
                return review.QuestionAccepted(text)

            def change_draft(key: review.QuestionKey, text: str, **options: object) -> review.QuestionAccepted:
                accepted = approve(key, text, **options)
                _ = draft.write_text(initial.replace("layout", "menu"))
                return accepted

            with mock.patch.object(review, "review_question", side_effect=change_draft) as reader:
                code, _, errors = self.run_cli(*arguments)
                self.assertEqual(code, 2)
                self.assertIn("the reviewed proposal draft changed", errors)
                self.assertFalse(destination.exists())
                reader.assert_called_once()
            self.assertFalse((record_store.state_root() / "record-ids/proposal.water").exists())

            _ = draft.write_text(initial)
            issue_lock.reset_mock()
            numbers.reset_mock()
            with mock.patch.object(review, "review_question", side_effect=approve) as reader:
                self.assertEqual(self.run_cli(*arguments), (0, "number new proposal.md: P1\n", ""))
                reader.assert_called_once()
                reviewed = cast(str, reader.call_args.args[1])
            saved_body = destination.read_text().split("\n---\n", 1)[1]
            self.assertEqual(reviewed, "## new proposal\n\n" + saved_body)
            self.assertEqual(draft.read_text(), initial)

    def test_native_proposal_reply_uses_the_named_state(self) -> None:
        today = date(2026, 11, 25)
        native = proposal_issue.NoProposalSubmissionReply()
        _ = self.issue.write_text(VAULT_ISSUE)
        self.assertTrue(proposal_issue.record_review(self.issue, "Answer: Use the new layout.", NoMark(), today,
                                                    submission_reply=native))
        issue = Issue(self.issue, frontmatter(self.issue))
        self.assertEqual(proposal_issue.review_log(issue)[-1].words, "Answer: Use the new layout.")

        scoped, _ = proposal_issue.set_fields(VAULT_ISSUE, {"review_submission": "{}"}, self.issue)
        _ = self.issue.write_text(scoped)
        with self.assertRaisesRegex(record_store.SubmissionError, "Use the scoped reply"):
            _ = proposal_issue.record_review(self.issue, "Answer: Use the new layout.", NoMark(), today,
                                             submission_reply=native)
        self.assertEqual(self.issue.read_text(), scoped)

    def test_number_refuses_a_draft_then_numbers_the_revision(self) -> None:
        draft = VAULT_ISSUE + "\nRead /tmp/proof.txt.\n"
        _ = self.issue.write_text(draft)
        with (mock.patch.dict(os.environ, {"NOTIFY_STATE_DIR": str(self.root / "notify")}),
              mock.patch.object(review, "review_question",
                                return_value=review.QuestionAccepted("Use the new layout.")) as reader):
            code, _, errors = self.run_cli("number", str(self.issue))
            self.assertEqual(code, 2)
            self.assertIn("Revise the proposal draft. omit-file-path: /tmp/proof.txt", errors)
            self.assertEqual(self.issue.read_text(), draft)
            reader.assert_not_called()

            _ = self.issue.write_text(VAULT_ISSUE + "\nUse the new layout.\n")
            code, _, errors = self.run_cli("number", str(self.issue))
            self.assertEqual(code, 0, errors)
            self.assertIn("P1", frontmatter(self.issue)[proposal_issue.NUMBER_FIELD])
            self.assertIn("Use the new layout.", self.issue.read_text())
            reader.assert_called_once()

    def run_cli(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            arguments = list(argv)
            for flag in ("--showrunner", "--plan"):
                if flag in arguments:
                    index = arguments.index(flag)
                    del arguments[index:index + 2]
            code = proposal_issue.main(arguments)
        return code, out.getvalue(), err.getvalue()

    def today(self) -> str:
        return f'"[[{datetime.now(user_zone()).date().isoformat()}]]"'

    def text(self) -> str:
        return self.issue.read_text(encoding="utf-8")

    def test_create_fills_template_and_protects_the_decision_key(self) -> None:
        template = self.root / "issue.yaml"
        _ = template.write_text("---\narea: \"[[{{AREA}}]]\"\nproject: \"[[{{PROJECT}}]]\"\n"
                                + "category: [\"[[issue structure#{{CATEGORY}}|{{CATEGORY}}]]\"]\n"
                                + "proposed_by: \"{{PROPOSED_BY}}\"\nbacklog_goal: \"{{BACKLOG_GOAL}}\"\n"
                                + "date_created: \"[[{{DATE}}]]\"\nstatus: open\ntags: [issue]\n---\n")
        body = self.root / "body.md"
        _ = body.write_text("# Findings\nBefore and after.\n")
        issue = self.root / "new style issue.md"
        arguments = ["create", str(issue), "--project", "alpha", "--short", "alpha", "--proposed-by", "sample-style",
                     "--extra-tags", "style", "--rule", "global:rust/names.md", "--crate", "alpha", "--rule-blob", "abc123", "--count", "3", "--body-file", str(body), "--template", str(template)]
        ranking = "goal 1 - Ship Hana · alignment ⭐⭐ · impact ⭐⭐ · urgency ⭐ · effort ⭐"
        for label, flags, expected, proposer in (
                ("new style issue", [], "", "sample-style"),
                ("ranked style issue", ["--proposed-ranking", ranking], ranking, "sample-style"),
                ("unit name issue", [], "", "names"),
                ("unit id issue", [], "", fixture_runtime_id("names"))):
            with self.subTest(ranking=expected, proposer=proposer):
                destination = self.root / (label + ".md")
                command = [*arguments]
                command[1] = str(destination)
                if destination != issue:
                    command[command.index("--rule-blob") + 1] = destination.stem
                command[command.index("--proposed-by") + 1] = proposer
                code, _, err = self.run_cli(*command, *flags)
                self.assertEqual(code, 0, err)
                expected_id = fixture_runtime_id("sample-style" if proposer == "sample-style" else "names")
                self.assertIn(f'proposed_by: "{expected_id}"', destination.read_text())
                self.assertIn(f"\n\nProposed ranking: {expected}\n\n<!-- style-decision:start -->\n# Findings\nBefore and after.\n",
                              destination.read_text())
        from scripts.production.issues import frontmatter
        fields = frontmatter(issue)
        self.assertEqual(fields["tags"], ["issue", "proposal", "style"])
        self.assertNotIn("style_findings", fields)
        self.assertEqual(fields["style_count"], ["3"])
        self.assertEqual(fields["area"], ["style"])
        self.assertIn('area: "[[style]]"\n', issue.read_text())
        self.assertEqual(fields["category"], ["refactor"])
        self.assertEqual(fields["backlog_goal"], [])
        self.assertEqual(fields["project"], ["alpha"])
        self.assertTrue(issue.read_text().split("---", 2)[2].lstrip().startswith("Proposed ranking:"))
        original, stamp = issue.read_bytes(), issue.stat().st_mtime_ns
        self.assertEqual(self.run_cli(*arguments)[0], 0)
        self.assertEqual(issue.stat().st_mtime_ns, stamp)
        arguments[arguments.index("--rule-blob") + 1] = "c"
        code, output, error = self.run_cli(*arguments)
        self.assertEqual(code, 0, error)
        alternate, = self.root.glob("new style issue p*.md")
        self.assertEqual(output, f"create {alternate.name}: created\n")
        self.assertEqual(frontmatter(alternate)["style_rule_blob"], ["c"])
        self.assertEqual(issue.read_bytes(), original)

    def test_area_style_sets_the_area_and_vault_date_on_current_and_closed_legacy_issues(self) -> None:
        forms = (
            ("decision", 'area: "[[unfiled]]"\n', 'style_rule: "global:rust/names.md"\n', "open"),
            ("missing area", "", 'style_rule: "global:rust/names.md"\n', "closed"),
            ("legacy", 'area: "[[unfiled]]"\n',
             'style_rules: ["https://example.test/names.md"]\nstyle_findings: ["held-1"]\n', "closed"),
        )
        for name, area, metadata, status in forms:
            with self.subTest(form=name):
                folder = self.root / name
                folder.mkdir()
                issue = folder / "proposal.md"
                original = (f'---\n{area}date_modified: "[[2000-01-01]]"\nstatus: {status}\n'
                            + 'unit: "names"\nplan: "unit.md"\n' + metadata
                            + 'tags: ["issue", "proposal", "style"]\n---\nUser notes stay.\n')
                _ = issue.write_text(original)
                code, out, err = self.run_cli("area-style", str(folder))
                self.assertEqual((code, err), (0, ""))
                self.assertEqual(len(out.splitlines()), 1)
                self.assertIn(issue.name, out)
                updated = issue.read_text()
                self.assertIn('area: "[[style]]"\n', updated)
                self.assertIn(f"date_modified: {self.today()}\n", updated)
                self.assertEqual(updated.replace('area: "[[style]]"\n', ""),
                                 original.replace(area, "", 1).replace('"[[2000-01-01]]"', self.today()))

    def test_area_style_keeps_comments_after_the_area_line_when_it_updates_the_issue(self) -> None:
        folder = self.root / "issues"
        folder.mkdir()
        issue = folder / "decision.md"
        for newline in (b"\n", b"\r\n"):
            with self.subTest(newline=newline):
                original = (b'---\narea: "[[unfiled]]"\n# Keep this comment and its spacing.  \n\n'
                            b'date_modified: "[[2000-01-01]]"\nstyle_rule: "global:rust/names.md"\n'
                            b'status: open\n---\nUser notes stay.\n').replace(b"\n", newline)
                _ = issue.write_bytes(original)
                expected = original.replace(b'area: "[[unfiled]]"', b'area: "[[style]]"').replace(
                    b'"[[2000-01-01]]"', self.today().encode())
                action = (0, f"area-style {issue.name}: area [[style]]\n", "")
                self.assertEqual(self.run_cli("area-style", str(folder), "--dry-run"), action)
                self.assertEqual(issue.read_bytes(), original)
                self.assertEqual(self.run_cli("area-style", str(folder)), action)
                self.assertEqual(issue.read_bytes(), expected)
                self.assertEqual(self.run_cli("area-style", str(folder)), (0, "", ""))
                self.assertEqual(issue.read_bytes(), expected)

    def test_area_style_keeps_non_style_issues_and_other_areas_byte_identical(self) -> None:
        folder = self.root / "issues"
        folder.mkdir()
        originals: dict[Path, bytes] = {}
        for name, metadata, area in (
            ("plain", "", '"[[unfiled]]"'),
            ("other area", 'style_rule: "global:rust/names.md"\n', '"[[tooling]]"'),
            ("already style", 'style_rules: ["https://example.test/names.md"]\n', '"[[style]]"'),
        ):
            path = folder / (name + ".md")
            original = (f'---\narea: {area}\ndate_modified: "[[2000-01-01]]"\n{metadata}'
                        + 'status: open\n---\nstyle_rule: body text is not metadata\n').encode()
            _ = path.write_bytes(original)
            originals[path] = original
        stamps = {path: path.stat().st_mtime_ns for path in originals}
        self.assertEqual(self.run_cli("area-style", str(folder)), (0, "", ""))
        self.assertEqual({path: path.read_bytes() for path in originals}, originals)
        self.assertEqual({path: path.stat().st_mtime_ns for path in originals}, stamps)

    def test_area_style_dry_run_and_second_run_write_nothing(self) -> None:
        folder = self.root / "issues"
        folder.mkdir()
        issue = folder / "decision.md"
        original = (b'---\narea: "[[unfiled]]"\ndate_modified: "[[2000-01-01]]"\n'
                    b'style_rule: "global:rust/names.md"\nstatus: open\n---\nKeep notes.\n')
        _ = issue.write_bytes(original)
        stamp = issue.stat().st_mtime_ns
        dry = self.run_cli("area-style", str(folder), "--dry-run")
        self.assertEqual((dry[0], dry[2]), (0, ""))
        self.assertEqual(len(dry[1].splitlines()), 1)
        self.assertIn(issue.name, dry[1])
        self.assertEqual(issue.read_bytes(), original)
        self.assertEqual(issue.stat().st_mtime_ns, stamp)
        self.assertEqual(list(folder.iterdir()), [issue])
        self.assertEqual(self.run_cli("area-style", str(folder)), dry)
        updated, stamp = issue.read_bytes(), issue.stat().st_mtime_ns
        self.assertNotEqual(updated, original)
        self.assertEqual(self.run_cli("area-style", str(folder)), (0, "", ""))
        self.assertEqual(issue.read_bytes(), updated)
        self.assertEqual(issue.stat().st_mtime_ns, stamp)
        self.assertEqual(list(folder.iterdir()), [issue])

    def test_number_gives_each_proposal_the_next_free_number_oldest_first(self) -> None:
        folder = self.root / "issues"
        # The vault indents a list's items.
        issue = ISSUE.replace("- issue\n- proposal\n", "  - issue\n  - proposal\n")
        archived = self.root / "archive/hana/old.md"
        archived.parent.mkdir(parents=True)
        _ = archived.write_text(issue.replace("stage: backlog", 'proposal_id: "P4"\nstage: backlog'))
        newer, older, plain = folder / "newer.md", folder / "older.md", folder / "plain.md"
        folder.mkdir()
        _ = newer.write_text(issue.replace("2026-10-01]]\"\ndate_modified", "2026-10-09]]\"\ndate_modified"))
        _ = older.write_text(issue)
        _ = plain.write_text(issue.replace("  - proposal\n", ""))
        self.assertEqual(self.run_cli("number", str(newer), str(plain), str(older)),
                         (0, "number older.md: P5\nnumber plain.md: not a proposal\nnumber newer.md: P6\n", ""))
        self.assertIn('proposal_id: "P5"\n', older.read_text())
        self.assertNotIn("proposal_id", plain.read_text())
        before = newer.read_bytes()
        self.assertEqual(self.run_cli("number", str(newer)), (0, "number newer.md: P6\n", ""))
        self.assertEqual(newer.read_bytes(), before)

    def test_create_numbers_a_new_decision(self) -> None:
        template = self.root / "template.yaml"
        _ = template.write_text("---\nstatus: open\n---\n")
        body = self.root / "body.md"
        _ = body.write_text("# Findings\n")
        issue = self.root / "decision.md"
        code, out, err = self.run_cli("create", str(issue), "--project", "alpha", "--short", "a", "--proposed-by",
                                      "style", "--rule", "global:rust/names.md", "--crate", "alpha", "--rule-blob",
                                      "abc", "--count", "1", "--body-file", str(body), "--template", str(template))
        self.assertEqual((code, out), (0, "create decision.md: created\n"), err)
        self.assertIn('proposal_id: "P1"\n', issue.read_text())

    def test_folder_lock_uses_one_fixed_path_across_runtime_environments(self) -> None:
        runtime = self.root / "runtime"
        first_tmp, second_tmp = self.root / "tmp-a", self.root / "tmp-b"
        for folder in (runtime, first_tmp, second_tmp):
            folder.mkdir()
        digest = hashlib.sha256(str((self.root / ".proposal-numbers").resolve()).encode()).hexdigest()[:32]
        lock_path = self.runtime / f"proposal-issue-{digest}.lock"
        self.assertFalse(self.runtime.exists())
        for use_runtime, temporary in ((True, first_tmp), (False, first_tmp), (False, second_tmp)):
            with self.subTest(runtime=use_runtime, temporary=temporary):
                with (mock.patch.dict(os.environ, {"TMPDIR": str(temporary)}),
                      mock.patch.object(tempfile, "tempdir", None)):
                    if use_runtime:
                        os.environ["XDG_RUNTIME_DIR"] = str(runtime)
                    else:
                        _ = os.environ.pop("XDG_RUNTIME_DIR", None)
                    with proposal_issue._folder_lock(self.root):  # pyright: ignore[reportPrivateUsage]
                        self.assertEqual(list(self.runtime.glob("*.lock")), [lock_path])
                        with lock_path.open("a") as other:
                            with self.assertRaises(BlockingIOError):
                                fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.assertEqual(list(self.runtime.glob("*")), [lock_path])
        for folder in (runtime, first_tmp, second_tmp):
            self.assertEqual(list(folder.iterdir()), [])

    def test_create_keeps_a_same_key_decision_that_appears_at_the_link(self) -> None:
        template = self.root / "template.yaml"
        _ = template.write_text("---\nstatus: open\n---\n")
        issue = self.root / "decision.md"
        original = (b'---\r\nstyle_rule: "global:rust/names.md"\r\nstyle_crate: "alpha"\r\n'
                    b'style_rule_blob: "abc"\r\nproposal_id: "P47"\r\nstyle_count: "9"\r\n'
                    b'unit: "names"\r\nplan: "unit.md"\r\n---\r\nUser notes.\r\n')
        link = os.link
        stamps: list[int] = []

        def competing_create(temporary: Path, path: Path) -> None:
            self.assertEqual(path, issue)
            self.assertTrue(temporary.is_file())
            _ = path.write_bytes(original)
            stamps.append(path.stat().st_mtime_ns)
            link(temporary, path)

        with mock.patch.object(os, "link", side_effect=competing_create):
            self.assertFalse(proposal_issue.create(
                issue, "alpha", "a", "style", ["style"], "global:rust/names.md", "alpha", "abc", 1,
                "# New examples\n", template, date(2026, 10, 20), initial_fields={}))
        self.assertEqual(issue.read_bytes(), original)
        self.assertEqual(stamps, [issue.stat().st_mtime_ns])
        self.assertEqual(list(self.root.glob(".decision-*")), [])

    def test_create_uses_a_free_name_when_another_decision_appears_at_the_link(self) -> None:
        template = self.root / "template.yaml"
        _ = template.write_text("---\nstatus: open\n---\n")
        issue = self.root / "decision.md"
        original = (b'---\nstyle_rule: "global:rust/names.md"\nstyle_crate: "alpha"\n'
                    b'style_rule_blob: "other-blob"\nproposal_id: "P47"\n---\nOther decision.\n')
        link = os.link
        stamps: list[int] = []

        def competing_create(temporary: Path, path: Path) -> None:
            self.assertTrue(temporary.is_file())
            if path == issue:
                _ = path.write_bytes(original)
                stamps.append(path.stat().st_mtime_ns)
            link(temporary, path)

        with mock.patch.object(os, "link", side_effect=competing_create):
            self.assertTrue(proposal_issue.create(
                issue, "alpha", "a", "style", ["style"], "global:rust/names.md", "alpha", "abc", 1,
                "# New examples\n", template, date(2026, 10, 20), initial_fields={}))
        alternate = issue.with_stem("decision p48")
        self.assertIn('proposal_id: "P48"', alternate.read_text())
        self.assertEqual(issue.read_bytes(), original)
        self.assertEqual(stamps, [issue.stat().st_mtime_ns])
        self.assertEqual(list(self.root.glob(".decision-*")), [])

    def test_create_keeps_a_same_key_decision_that_appears_before_the_folder_lock(self) -> None:
        template = self.root / "template.yaml"
        _ = template.write_text("---\nstatus: open\n---\n")
        issue = self.root / "decision.md"
        original = (b'---\r\nstyle_rule: "global:rust/names.md"\r\nstyle_crate: "alpha"\r\n'
                    b'style_rule_blob: "abc"\r\nproposal_id: "P47"\r\nstyle_count: "9"\r\n'
                    b'unit: "names"\r\nplan: "unit.md"\r\n---\r\nUser notes.\r\n')

        @contextlib.contextmanager
        def competing_create(folder: Path) -> Generator[None]:
            self.assertEqual(folder, self.root)
            _ = issue.write_bytes(original)
            stamp = issue.stat().st_mtime_ns
            yield
            self.assertEqual(issue.stat().st_mtime_ns, stamp)

        with mock.patch.object(proposal_issue, "_folder_lock", side_effect=competing_create):
            self.assertFalse(proposal_issue.create(
                issue, "alpha", "a", "style", ["style"], "global:rust/names.md", "alpha", "abc", 1,
                "# New examples\n", template, date(2026, 10, 20), initial_fields={}))
        self.assertEqual(issue.read_bytes(), original)
        self.assertEqual(list(self.root.glob(".decision-*")), [])
        self.assertEqual({path.name for path in self.root.iterdir()},
                         {"a proposal.md", "template.yaml", "decision.md"})

    def test_create_uses_a_free_name_when_another_decision_appears_before_the_folder_lock(self) -> None:
        template = self.root / "template.yaml"
        _ = template.write_text("---\nstatus: open\n---\n")
        issue = self.root / "decision.md"
        original = (b'---\nstyle_rule: "global:rust/names.md"\nstyle_crate: "alpha"\n'
                    b'style_rule_blob: "other-blob"\nproposal_id: "P47"\n---\nOther decision.\n')

        @contextlib.contextmanager
        def competing_create(folder: Path) -> Generator[None]:
            self.assertEqual(folder, self.root)
            _ = issue.write_bytes(original)
            stamp = issue.stat().st_mtime_ns
            try:
                yield
            finally:
                self.assertEqual(issue.stat().st_mtime_ns, stamp)

        with mock.patch.object(proposal_issue, "_folder_lock", side_effect=competing_create):
            self.assertTrue(proposal_issue.create(
                issue, "alpha", "a", "style", ["style"], "global:rust/names.md", "alpha", "abc", 1,
                "# New examples\n", template, date(2026, 10, 20), initial_fields={}))
        self.assertIn('proposal_id: "P48"', issue.with_stem("decision p48").read_text())
        self.assertEqual(issue.read_bytes(), original)
        self.assertEqual(list(self.root.glob(".decision-*")), [])

    def test_f069_create_refuses_an_existing_result_file(self) -> None:
        template = self.root / "template.yaml"
        _ = template.write_text("---\nstatus: open\n---\n")
        body = self.root / "body.txt"
        _ = body.write_text("Examples\n")
        before = self.issue.read_bytes()
        code, _, error = self.run_cli(
            "create", str(self.issue), "--filed-path-file", str(self.issue), "--project", "sample",
            "--short", "s", "--proposed-by", "style", "--rule", "global:rust/names.md", "--crate",
            "alpha", "--rule-blob", "abc", "--count", "1", "--template", str(template), "--body-file", str(body))
        self.assertEqual(code, 2, error)
        self.assertIn("The result file already exists", error)
        self.assertEqual(self.issue.read_bytes(), before)
        self.assertEqual(list(self.root.glob("*.md")), [self.issue])

    def test_create_gives_two_different_decisions_distinct_proposal_numbers(self) -> None:
        template = self.root / "template.yaml"
        _ = template.write_text("---\nstatus: open\n---\n")
        first, second = self.root / "alpha.md", self.root / "beta.md"
        self.assertTrue(proposal_issue.create(
            first, "alpha", "a", "style", ["style"], "global:rust/names.md", "alpha", "abc", 1,
            "# Alpha examples\n", template, date(2026, 10, 20), initial_fields={}))
        original = first.read_bytes()
        self.assertTrue(proposal_issue.create(
            second, "alpha", "a", "style", ["style"], "global:rust/names.md", "beta", "abc", 1,
            "# Beta examples\n", template, date(2026, 10, 20), initial_fields={}))
        from scripts.production.issues import frontmatter
        self.assertEqual((frontmatter(first)["proposal_id"], frontmatter(second)["proposal_id"]),
                         (["P1"], ["P2"]))
        self.assertEqual(first.read_bytes(), original)
        self.assertEqual(list(self.root.glob(".decision-*")), [])

    def test_create_with_rules_preserves_existing_decision_and_answer(self) -> None:
        template = self.root / "template.yaml"
        _ = template.write_text("---\nstatus: open\n---\n")
        body = self.root / "body.md"
        _ = body.write_text("# Keep rule lines\nRule: global:rust/names.md\n")
        issue = self.root / "linked.md"
        urls = "https://github.com/o/r/blob/main/b.md,https://github.com/o/r/blob/main/a.md"
        args = ["create", str(issue), "--project", "sample", "--short", "s", "--proposed-by", "style",
                "--rule", "global:rust/names.md", "--crate", "alpha", "--rule-blob", "abc123", "--count", "1", "--rules", urls, "--template", str(template), "--body-file", str(body)]
        code, _, error = self.run_cli(*args)
        self.assertEqual(code, 0, error)
        from scripts.production.issues import frontmatter
        self.assertEqual(frontmatter(issue)["style_rules"], sorted(urls.split(",")))
        original = issue.read_bytes()
        args[args.index("--rules") + 1] = "https://github.com/o/r/blob/main/changed.md"
        self.assertEqual(self.run_cli(*args)[0], 0)
        self.assertEqual(issue.read_bytes(), original)

    def test_refresh_updates_generated_examples_and_count_preserving_answer_and_notes(self) -> None:
        original = ISSUE.replace("---\n# body", 'unit: "names"\nstyle_count: "1"\n---\n# body')
        original += "\n" + proposal_issue.decision_body("# Old examples\n") + "\nUser notes stay.\n"
        _ = self.issue.write_text(original)
        body = "# New examples\nFindings: 4\n"
        today = datetime.now(user_zone()).date()
        self.assertIn("body", proposal_issue.refresh_decision(self.issue, 4, body, "ranking", today))
        self.assertIn('unit: "names"', self.text())
        self.assertIn('style_count: "4"', self.text())
        self.assertIn("# body\n\nstatus: open stays in the body", self.text())
        self.assertIn("User notes stay.", self.text())
        self.assertIn("# New examples", self.text())
        self.assertNotIn("# Old examples", self.text())
        stamp = self.issue.stat().st_mtime_ns
        self.assertEqual(proposal_issue.refresh_decision(self.issue, 4, body, "ranking", today), [])
        self.assertEqual(self.issue.stat().st_mtime_ns, stamp)

    def test_reopen_removes_every_close_field_and_keeps_the_answer(self) -> None:
        _ = proposal_issue.edit(self.issue, {"unit": "names", "plan": "unit.md", "stage": "active"}, date(2026, 10, 15))
        from scripts.production.issues import frontmatter
        def fields() -> dict[str, list[str]]:
            return {key: value for key, value in frontmatter(self.issue).items() if key != "date_modified"}
        before = fields()
        for closure in (["--reason", "built in names-unit-1"], ["--as-built", "docs/as-built/x.md"]):
            with self.subTest(closure=closure[0]):
                self.assertEqual(self.run_cli("close", str(self.issue), *closure)[0], 0)
                code, _, error = self.run_cli("reopen", str(self.issue))
                self.assertEqual(code, 0, error)
                self.assertEqual(fields(), before)

    def decision_issue(self) -> None:
        text = ISSUE.replace("---\n# body", 'style_count: "1"\n---\n# body') + "\nProposed ranking: effort one\n\n"
        _ = self.issue.write_text(text + proposal_issue.decision_body("# Old examples\n"))

    def test_refresh_and_approve_at_the_same_time_keep_both_changes(self) -> None:
        self.decision_issue()
        today = datetime.now(user_zone()).date()
        original = proposal_issue.write_issue
        approval = threading.Thread(target=proposal_issue.edit, args=(self.issue, {"unit": "names", "plan": "p"}, today))
        def write_after_the_approval_starts(path: Path, text: str) -> None:
            if not approval.is_alive() and "# New examples" in text:
                approval.start()
                time.sleep(0.3)
            original(path, text)
        with mock.patch.object(proposal_issue, "write_issue", side_effect=write_after_the_approval_starts):
            _ = proposal_issue.refresh_decision(self.issue, 2, "# New examples\n", "effort two", today)
            approval.join()
        self.assertIn('unit: "names"', self.text())
        self.assertIn("# New examples", self.text())
        self.assertIn('style_count: "2"', self.text())

    def test_every_writing_verb_holds_the_issue_lock_outside_the_issue_folder(self) -> None:
        today = datetime.now(user_zone()).date()
        def closed() -> None:
            self.decision_issue()
            _ = proposal_issue.edit(self.issue, {"status": "closed"}, date(2026, 10, 15))
        def approved_at_backlog() -> None:
            self.decision_issue()
            _ = self.issue.write_text(self.text().replace("tags:\n- issue\n- proposal\n", "tags: [issue, proposal]\n"))
            _ = proposal_issue.edit(self.issue, {"unit": "names", "plan": "unit.md"}, date(2026, 10, 15))
        def approved_by_showrunner() -> None:
            approved_at_backlog()
            _ = proposal_issue.edit(self.issue, {"approved_by": "showrunner", "approved_in": fixture_runtime_id("hana")}, date(2026, 10, 15))
        verbs = (("edit", closed, lambda: proposal_issue.edit(self.issue, {"stage": "active"}, today)),
                 ("rules", closed, lambda: proposal_issue.edit_rules(self.issue, ["https://example.invalid/rule.md"], today)),
                 ("refresh", closed, lambda: proposal_issue.refresh_decision(self.issue, 3, "# Locked\n", "effort", today)),
                 ("reopen", closed, lambda: proposal_issue.reopen(self.issue, today)),
                 ("unapprove", approved_by_showrunner,
                  lambda: proposal_issue.unapprove(self.issue, fixture_runtime_id("hana"), today, dry_run=False)),
                 ("move-approved", approved_at_backlog,
                  lambda: list(proposal_issue.move_approved(self.root, today, dry_run=False))))
        for name, setup, verb in verbs:
            with self.subTest(verb=name):
                setup()
                blocked: list[bool] = []
                def write(path: Path, text: str) -> None:
                    lock, = self.runtime.glob("proposal-issue-*.lock")
                    with lock.open("a") as other:
                        try:
                            fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        except BlockingIOError:
                            blocked.append(True)
                    _ = path.write_text(text)
                with mock.patch.object(proposal_issue, "write_issue", side_effect=write):
                    _ = verb()
                self.assertEqual(blocked, [True])
                self.assertEqual(sorted(path.name for path in self.root.iterdir()), ["a proposal.md"])

    def test_review_writes_check_the_record_under_the_issue_lock(self) -> None:
        before = self.issue.read_bytes()
        today = date(2026, 11, 15)

        def refuse() -> None:
            lock, = self.runtime.glob("proposal-issue-*.lock")
            with lock.open("a") as other, self.assertRaises(BlockingIOError):
                fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.issue.read_bytes(), before)
            raise proposal_issue.Refusal("The record changed.")

        actions = {
            "decline": lambda: proposal_issue.edit(self.issue, {"style_answer": "declined"}, today, check=refuse),
            "approve": lambda: proposal_issue.record_review(self.issue, "Approve", ReviewMark.DONE, today, check=refuse),
        }
        for outcome, action in actions.items():
            with self.subTest(outcome=outcome), self.assertRaisesRegex(proposal_issue.Refusal, "The record changed"):
                _ = action()
            self.assertEqual(self.issue.read_bytes(), before)
        with self.assertRaisesRegex(proposal_issue.Refusal, "The record changed"):
            proposal_issue.approve_from_review(self.issue, staff.StaffId(fixture_runtime_id("names")),
                                               today, ("Approve",), check=refuse)
        self.assertEqual(self.issue.read_bytes(), before)

    def test_fill_ranking_writes_an_empty_ranking_once_under_the_issue_lock(self) -> None:
        self.decision_issue()
        _ = self.issue.write_text(self.text().replace("Proposed ranking: effort one", "Proposed ranking: "))
        blocked: list[bool] = []
        def write(path: Path, text: str) -> None:
            lock, = self.runtime.glob("proposal-issue-*.lock")
            with lock.open("a") as other:
                try:
                    fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    blocked.append(True)
            _ = path.write_text(text)
        with mock.patch.object(proposal_issue, "write_issue", side_effect=write):
            self.assertTrue(proposal_issue.fill_ranking(self.issue, "effort two"))
            self.assertEqual(blocked, [True])
            self.assertIn("\nProposed ranking: effort two\n", self.text())
            before = self.issue.read_bytes()
            self.assertFalse(proposal_issue.fill_ranking(self.issue, "effort three"))
        self.assertEqual(blocked, [True])
        self.assertEqual(self.issue.read_bytes(), before)
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), ["a proposal.md"])

    def test_create_takes_inherited_fields_as_a_json_object_of_known_text_fields(self) -> None:
        template = self.root / "template.yaml"
        _ = template.write_text("---\nstatus: open\n---\n")
        body = self.root / "body.md"
        _ = body.write_text("# Examples\n")
        issue = self.root / "inherited.md"
        arguments = ["create", str(issue), "--project", "sample", "--short", "s", "--proposed-by", "style",
                     "--rule", "global:rust/names.md", "--crate", "alpha", "--rule-blob", "abc123", "--count", "1",
                     "--template", str(template), "--body-file", str(body), "--initial-fields"]
        for fields, message in (('{"status": "closed"}', "invalid inherited fields: status"),
                                ('{"unit": 1}', "unit is not text"), ('["unit"]', "JSON object")):
            with self.subTest(fields=fields):
                code, _, error = self.run_cli(*arguments, fields)
                self.assertEqual(code, 2)
                self.assertIn(message, error)
                self.assertFalse(issue.exists())
        self.assertEqual(self.run_cli(*arguments, '{"unit": "names"}')[0], 0)
        from scripts.production.issues import frontmatter
        self.assertEqual(proposal_issue.staff_name(frontmatter(issue)["unit"][0]), "names")
        self.assertNotIn("plan", frontmatter(issue))

    def test_a_decision_created_with_an_inherited_approval_starts_in_progress(self) -> None:
        template = self.root / "template.yaml"
        _ = template.write_text("---\nstatus: open\n---\n")
        body = self.root / "body.md"
        _ = body.write_text("# Examples\n")
        from scripts.production.issues import frontmatter
        for name, fields, stage in (("approved", '{"unit": "names", "stage": "backlog"}', "active"),
                                    ("unanswered", '{"stage": "backlog"}', "backlog"),
                                    ("waiting", '{"stage": "waiting"}', "waiting"),
                                    ("no fields", "{}", "backlog")):
            with self.subTest(name=name):
                issue = self.root / f"{name}.md"
                code, _, error = self.run_cli(
                    "create", str(issue), "--project", "sample", "--short", "s", "--proposed-by", "style",
                    "--rule", "global:rust/names.md", "--crate", "alpha", "--rule-blob", name, "--count", "1",
                    "--template", str(template), "--body-file", str(body), "--initial-fields", fields)
                self.assertEqual(code, 0, error)
                self.assertEqual(frontmatter(issue)["stage"], [stage])

    def test_rules_sets_list_preserving_other_keys_and_body_bytes(self) -> None:
        url = "https://github.com/o/r/blob/main/rust/names.md"
        for existing in ("", 'style_rules: ["old"]\n'):
            with self.subTest(existing=existing):
                original = ISSUE.replace("---\n# body", existing + "---\n# body").replace("\n", "\r\n")
                _ = self.issue.write_bytes(original.encode())
                code, _, error = self.run_cli("rules", str(self.issue), "--rules", url)
                self.assertEqual(code, 0, error)
                expected = original.replace('date_modified: "[[2026-10-01]]"', f"date_modified: {self.today()}")
                line = f'style_rules: ["{url}"]\r\n'
                expected = expected.replace(existing.replace("\n", "\r\n"), line) if existing else expected.replace("---\r\n# body", line + "---\r\n# body")
                self.assertEqual(self.issue.read_bytes(), expected.encode())
                stamp = self.issue.stat().st_mtime_ns
                self.assertEqual(self.run_cli("rules", str(self.issue), "--rules", url)[0], 0)
                self.assertEqual(self.issue.stat().st_mtime_ns, stamp)

    def test_close_accepts_a_reason_instead_of_an_as_built_path(self) -> None:
        code, _, err = self.run_cli("close", str(self.issue), "--reason", "declined: keep the API")
        self.assertEqual(code, 0, err)
        self.assertIn('reason: "declined: keep the API"', self.text())
        self.assertIn("status: closed", self.text())
        self.assertNotIn("as_built:", self.text())

    def test_approve_moves_the_proposal_to_in_progress(self) -> None:
        code, out, _ = self.run_cli(
            "approve", str(self.issue), "--by", "user", "--showrunner", "hana", "--unit", "build-report",
            "--plan", "docs/plans/x.md Phase 4–6",
        )
        self.assertEqual(code, 0)
        text = self.text()
        self.assertNotIn('showrunner:', text)
        self.assertIn(f'unit: "{fixture_runtime_id("build-report")}"\n', text)
        self.assertNotIn('plan:', text)
        self.assertIn("stage: active\n", text)
        self.assertNotIn("stage: backlog", text)
        self.assertIn(f"date_modified: {self.today()}\n", text)
        self.assertIn('approved_by: "user"\n', text)
        self.assertIn(f'approved_in: "{fixture_runtime_id("hana")}"\n', text)
        self.assertEqual(out, "approve a proposal.md: set plan, unit, approved_by, approved_in, stage, date_modified\n")
        from scripts.production.issues import Issue, frontmatter
        self.assertEqual(proposal_issue.proposal_progress(Issue(self.issue, frontmatter(self.issue))),
                         proposal_issue.InProgress())

    def test_unapprove_sets_an_approved_proposal_back_to_waiting_for_approval(self) -> None:
        from scripts.production.issues import Issue, frontmatter
        _ = self.issue.write_text(VAULT_ISSUE, encoding="utf-8")
        _ = self.run_cli("approve", str(self.issue), "--by", "showrunner", "--showrunner", "hana", "--unit", "u", "--plan", "p")
        approved = self.text()
        code, out, error = self.run_cli("unapprove", str(self.issue), "--dry-run")
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(out, "unapprove a proposal.md: set unit, approved_by, approved_in, stage\n")
        self.assertEqual(self.text(), approved)
        code, out, error = self.run_cli("unapprove", str(self.issue))
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(out, "unapprove a proposal.md: set unit, approved_by, approved_in, stage\n")
        text = self.text()
        for line in ('unit: ""\n', 'approved_by: ""\n', 'approved_in: ""\n', "stage: backlog\n", f"date_modified: {self.today()}\n"):
            self.assertIn(line, text)
        self.assertEqual(proposal_issue.proposal_progress(Issue(self.issue, frontmatter(self.issue))),
                         proposal_issue.AwaitingApproval())

    def test_unapprove_refuses_an_issue_with_no_approval_to_undo(self) -> None:
        by_showrunner = {"unit": "u", "plan": "p", "approved_by": "showrunner", "approved_in": fixture_runtime_id("hana")}
        cases: tuple[tuple[str, dict[str, str], str], ...] = (("waiting", {}, "no approval is recorded"),
                 ("declined", {**by_showrunner, "style_answer": "declined"}, "no approval is recorded"),
                 ("closed", {**by_showrunner, "status": "closed"}, "not an open proposal"),
                 ("user approval", {**by_showrunner, "approved_by": "user"}, "the user approved it"),
                 ("approval without approved_by", {"unit": "u", "plan": "p"}, "the user approved it"),
                 ("another showrunner", {**by_showrunner, "approved_in": "cargo-liner"},
                  f"approved in cargo-liner, not in {fixture_runtime_id("hana")}"),
                 ("no showrunner recorded", {**by_showrunner, "approved_in": ""}, f"approved in no showrunner, not in {fixture_runtime_id("hana")}"))
        for name, fields, reason in cases:
            with self.subTest(name=name):
                _ = self.issue.write_text(VAULT_ISSUE, encoding="utf-8")
                if fields:
                    _ = proposal_issue.edit(self.issue, fields, date(2026, 10, 15))
                before = self.text()
                for dry_run in ([], ["--dry-run"]):
                    code, _, error = self.run_cli("unapprove", str(self.issue), *dry_run)
                    self.assertEqual(code, 2)
                    self.assertIn(reason, error)
                    self.assertEqual(self.text(), before)
        _ = self.issue.write_text(VAULT_ISSUE.replace("  - proposal\n", ""), encoding="utf-8")
        _ = proposal_issue.edit(self.issue, {"unit": "u", "plan": "p"}, date(2026, 10, 15))
        code, _, error = self.run_cli("unapprove", str(self.issue))
        self.assertEqual(code, 2)
        self.assertIn("not an open proposal", error)

    def test_a_user_approval_replaces_a_showrunner_one_and_a_showrunner_one_keeps_the_user(self) -> None:
        def approve(by: str) -> str:
            code, out, error = self.run_cli("approve", str(self.issue), "--by", by, "--showrunner", "hana",
                                            "--unit", "u", "--plan", "p")
            self.assertEqual((code, error), (0, ""))
            return out
        _ = approve("showrunner")
        self.assertIn('approved_by: "showrunner"\n', self.text())
        self.assertEqual(approve("user"), "approve a proposal.md: set approved_by\n")
        self.assertIn('approved_by: "user"\n', self.text())
        self.assertEqual(approve("showrunner"), "approve a proposal.md: unchanged\n")
        self.assertIn('approved_by: "user"\n', self.text())
        # An approval from before approved_by is the user's, so a showrunner approval keeps it.
        _ = self.issue.write_text(ISSUE, encoding="utf-8")
        _ = proposal_issue.edit(self.issue, {"unit": "u", "plan": "p", "stage": "active"}, date(2026, 10, 15))
        self.assertEqual(approve("showrunner"), "approve a proposal.md: set plan, unit, date_modified\n")
        self.assertNotIn("approved_by", self.text())

    def test_approve_and_unapprove_refuse_outside_a_showrunner(self) -> None:
        before = self.text()
        with mock.patch.object(proposal_issue, "calling_showrunner", self.real_calling_showrunner), \
                mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "no-such-session"}), \
                mock.patch("scripts.production.showrunners.production_of", return_value=None):
            for verb in (["approve", "--by", "user", "--showrunner", "hana", "--unit", "u", "--plan", "p"], ["unapprove"]):
                with self.subTest(verb=verb[0]):
                    code, _, error = self.run_cli(verb[0], str(self.issue), *verb[1:])
                    self.assertEqual(code, 2)
                    self.assertIn("this session is no production's showrunner", error)
                    self.assertEqual(self.text(), before)

    def test_reopen_of_an_approved_proposal_closed_at_backlog_puts_it_in_progress(self) -> None:
        from scripts.production.issues import frontmatter
        for name, fields, stage, changed in (("approved", {"unit": "names", "plan": "unit.md"}, "active",
                                              "reason, status, stage, date_modified"),
                                             ("unanswered", {}, "backlog", "reason, status, date_modified")):
            with self.subTest(name=name):
                _ = self.issue.write_text(ISSUE, encoding="utf-8")
                _ = proposal_issue.edit(self.issue, {**fields, "status": "closed", "reason": "built"}, date(2026, 10, 15))
                self.assertEqual(frontmatter(self.issue)["stage"], ["backlog"])
                code, out, error = self.run_cli("reopen", str(self.issue))
                self.assertEqual(code, 0, error)
                self.assertEqual((frontmatter(self.issue)["status"], frontmatter(self.issue)["stage"]), (["open"], [stage]))
                self.assertEqual(out, f"reopen a proposal.md: set {changed}\n")

    def test_close_from_in_progress_closes_the_proposal_and_keeps_its_stage(self) -> None:
        _ = self.run_cli("approve", str(self.issue), "--by", "user", "--showrunner", "hana", "--unit", "u", "--plan", "p")
        code, _, error = self.run_cli("close", str(self.issue), "--reason", "built in u-1")
        self.assertEqual(code, 0, error)
        from scripts.production.issues import frontmatter
        self.assertEqual((frontmatter(self.issue)["status"], frontmatter(self.issue)["stage"]), (["closed"], ["active"]))

    def test_proposal_progress_reads_an_approval_or_an_active_stage_as_in_progress(self) -> None:
        from scripts.production.issues import Issue
        approval = {"unit": ["names"], "plan": ["unit.md"]}
        for name, fields, expected in (
                ("approved at backlog", {**approval, "stage": ["backlog"]}, proposal_issue.InProgress()),
                ("approved at waiting", {**approval, "stage": ["waiting"]}, proposal_issue.InProgress()),
                ("unanswered at active", {"stage": ["active"]}, proposal_issue.InProgress()),
                ("unanswered at backlog", {"stage": ["backlog"], "plan": ["unit.md"]}, proposal_issue.AwaitingApproval()),
                ("declined and open", {**approval, "stage": ["active"], "style_answer": ["declined"]},
                 proposal_issue.AwaitingApproval())):
            with self.subTest(name=name):
                issue = Issue(self.issue, {"status": ["open"], "tags": ["issue", "proposal"], **fields})
                self.assertEqual(proposal_issue.proposal_progress(issue), expected)

    def approved_folder(self) -> Path:
        """One folder of issues: one open approved proposal at backlog, and each kind move-approved leaves alone."""
        folder = self.root / "issues"
        folder.mkdir()
        approval = 'plan: "unit.md"\nunit: "names"\n'
        for name, answer, stage, status, tags in (
                ("approved", approval, "backlog", "open", "[issue, proposal]"),
                ("unanswered", 'plan: ""\n', "backlog", "open", "[issue, proposal]"),
                ("approved at waiting", approval, "waiting", "open", "[issue, proposal]"),
                ("approved and closed", approval, "backlog", "closed", "[issue, proposal]"),
                ("declined", approval + 'style_answer: "declined"\n', "backlog", "open", "[issue, proposal]"),
                ("approved untagged", approval, "backlog", "open", "[issue]")):
            text = (ISSUE.replace('plan: ""\n', answer).replace("stage: backlog\n", f"stage: {stage}\n")
                    .replace("status: open\ntags:\n- issue\n- proposal\n", f"status: {status}\ntags: {tags}\n"))
            _ = (folder / f"{name}.md").write_text(text, encoding="utf-8")
        return folder

    def test_move_approved_moves_only_open_approved_proposals_at_backlog(self) -> None:
        folder = self.approved_folder()
        before = {path.name: path.read_bytes() for path in folder.iterdir()}
        code, out, error = self.run_cli("move-approved", str(folder))
        self.assertEqual(code, 0, error)
        self.assertEqual(out, "move-approved approved.md: stage active\nmove-approved: 1 moved\n")
        moved = before["approved.md"].decode().replace("stage: backlog\n", "stage: active\n").replace(
            'date_modified: "[[2026-10-01]]"', f"date_modified: {self.today()}")
        self.assertEqual({path.name: path.read_bytes() for path in folder.iterdir()},
                         {**before, "approved.md": moved.encode()})

    def test_a_second_move_approved_changes_nothing(self) -> None:
        folder = self.approved_folder()
        self.assertEqual(self.run_cli("move-approved", str(folder))[0], 0)
        before = {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in folder.iterdir()}
        code, out, error = self.run_cli("move-approved", str(folder))
        self.assertEqual(code, 0, error)
        self.assertEqual(out, "move-approved: 0 moved\n")
        self.assertEqual({path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in folder.iterdir()}, before)

    def test_move_approved_dry_run_prints_the_same_lines_and_writes_nothing(self) -> None:
        folder = self.approved_folder()
        before = {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in folder.iterdir()}
        code, out, error = self.run_cli("move-approved", str(folder), "--dry-run")
        self.assertEqual(code, 0, error)
        self.assertEqual(out, "move-approved approved.md: stage active\nmove-approved: 1 moved\n")
        self.assertEqual({path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in folder.iterdir()}, before)

    def test_move_approved_dry_run_refuses_the_files_the_real_run_refuses_and_both_go_on(self) -> None:
        folder = self.root / "issues"
        folder.mkdir()
        approved = (ISSUE.replace('plan: ""\n', 'plan: "unit.md"\nunit: "names"\n')
                    .replace("tags:\n- issue\n- proposal\n", "tags: [issue, proposal]\n"))
        for name, text in (("a-good", approved), ("b-bad", approved.replace("---\n", "--- \n", 1)), ("c-good", approved)):
            _ = (folder / f"{name}.md").write_text(text, encoding="utf-8")
        bad = (folder / "b-bad.md").read_bytes()
        lines = (f"move-approved a-good.md: stage active\n"
                 f"move-approved b-bad.md: refused {folder / 'b-bad.md'} has no frontmatter\n"
                 f"move-approved c-good.md: stage active\n"
                 f"move-approved: 2 moved\n")
        self.assertEqual(self.run_cli("move-approved", str(folder), "--dry-run")[:2], (2, lines))
        self.assertEqual(self.run_cli("move-approved", str(folder))[:2], (2, lines))
        self.assertIn("stage: active\n", (folder / "a-good.md").read_text(encoding="utf-8"))
        self.assertIn("stage: active\n", (folder / "c-good.md").read_text(encoding="utf-8"))
        self.assertEqual((folder / "b-bad.md").read_bytes(), bad)

    def test_move_approved_refuses_a_missing_folder(self) -> None:
        code, out, error = self.run_cli("move-approved", str(self.root / "absent"))
        self.assertEqual((code, out), (2, ""))
        self.assertIn("absent is not a folder", error)

    def test_insert_goes_before_the_closing_fence_and_replace_stays_in_place(self) -> None:
        _ = self.run_cli(
            "approve", str(self.issue), "--by", "user", "--showrunner", "hana", "--unit", "u", "--plan", "p Phase 1",
        )
        lines = self.text().splitlines()
        fence = lines.index("---", 1)
        self.assertEqual(lines[fence - 3:fence], [f'unit: "{fixture_runtime_id("u")}"', 'approved_by: "user"', f'approved_in: "{fixture_runtime_id("hana")}"'])
        self.assertFalse(any(line.startswith('plan:') for line in lines))

    def test_a_home_relative_path_is_expanded(self) -> None:
        with mock.patch.dict(os.environ, {"HOME": str(self.root)}):
            code, _, _ = self.run_cli("start", "~/a proposal.md")
        self.assertEqual(code, 0)
        self.assertIn("stage: active\n", self.text())

    def test_start_sets_stage_active(self) -> None:
        code, out, _ = self.run_cli("start", str(self.issue))
        self.assertEqual(code, 0)
        self.assertIn("stage: active\n", self.text())
        self.assertEqual(out, "start a proposal.md: set stage, date_modified\n")

    def test_close_sets_as_built_status_and_dates(self) -> None:
        code, _, _ = self.run_cli("close", str(self.issue), "--as-built", "docs/hana/as-built/x.md")
        self.assertEqual(code, 0)
        text = self.text()
        self.assertIn('as_built: "docs/hana/as-built/x.md"\n', text)
        self.assertIn("status: closed\n", text)
        self.assertIn(f"date_closed: {self.today()}\n", text)
        self.assertIn("status: open stays in the body\n", text)

    def test_stars_writes_quoted_stars_from_counts_or_stars(self) -> None:
        code, _, _ = self.run_cli(
            "stars", str(self.issue), "--goal", "1 - Ship Hana", "--alignment", "4",
            "--impact", "⭐⭐⭐", "--urgency", "1", "--effort", "5",
        )
        self.assertEqual(code, 0)
        text = self.text()
        for line in (
            'backlog_goal: "1 - Ship Hana"', 'backlog_alignment: "⭐⭐⭐⭐"', 'backlog_impact: "⭐⭐⭐"',
            'backlog_urgency: "⭐"', 'backlog_effort: "⭐⭐⭐⭐⭐"',
        ):
            self.assertIn(line + "\n", text)

    def test_stars_refuses_values_out_of_range(self) -> None:
        for flag, value in (("--alignment", "0"), ("--impact", "6"), ("--effort", "⭐⭐⭐⭐⭐⭐"), ("--goal", "Ship")):
            argv = {"--goal": "1 - Ship Hana", "--alignment": "3", "--impact": "3", "--urgency": "1", "--effort": "2"}
            argv[flag] = value
            code, _, err = self.run_cli("stars", str(self.issue), *[part for pair in argv.items() for part in pair])
            self.assertEqual(code, 2, flag)
            self.assertIn(flag, err)
            self.assertEqual(self.text(), ISSUE)

    def test_block_list_value_is_replaced_with_its_continuation_lines(self) -> None:
        text, changed = proposal_issue.set_fields(ISSUE, {"tags": "proposal"}, self.issue)
        self.assertEqual(changed, ["tags"])
        self.assertIn('status: open\ntags: "proposal"\n---\n', text)
        self.assertNotIn("- issue\n", text)

    def test_other_lines_stay_byte_identical(self) -> None:
        crlf = ISSUE.replace("\n", "\r\n")
        _ = self.issue.write_bytes(crlf.encode("utf-8"))
        changed = proposal_issue.edit(self.issue, {"stage": "active"}, date(2026, 10, 15))
        self.assertEqual(changed, ["stage", "date_modified"])
        after = self.issue.read_bytes().decode("utf-8").split("\r\n")
        before = crlf.split("\r\n")
        differing = [index for index, (old, new) in enumerate(zip(before, after, strict=True)) if old != new]
        self.assertEqual([after[index] for index in differing], ['date_modified: "[[2026-10-15]]"', "stage: active"])

    def test_second_identical_edit_does_not_write(self) -> None:
        _ = proposal_issue.edit(self.issue, {"stage": "active"}, date(2026, 10, 15))
        stamp = self.issue.stat().st_mtime_ns
        self.assertEqual(proposal_issue.edit(self.issue, {"stage": "active"}, date(2026, 10, 15)), [])
        self.assertEqual(self.issue.stat().st_mtime_ns, stamp)

    def test_refuses_a_missing_file(self) -> None:
        code, out, err = self.run_cli("start", str(self.root / "gone.md"))
        self.assertEqual((code, out), (2, ""))
        self.assertEqual(err.count("\n"), 1)
        self.assertIn("gone.md is not a file", err)

    def test_refuses_a_file_without_frontmatter(self) -> None:
        for body in ("# no frontmatter\n", "---\nstatus: open\n"):
            _ = self.issue.write_text(body, encoding="utf-8")
            code, _, err = self.run_cli("start", str(self.issue))
            self.assertEqual(code, 2)
            self.assertIn("frontmatter", err)
            self.assertEqual(self.text(), body)
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), ["a proposal.md"])


if __name__ == "__main__":
    _ = unittest.main()
