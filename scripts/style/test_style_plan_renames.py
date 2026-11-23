"""Approved item renames permit only their reference and prose edits."""

from __future__ import annotations

import io
import json
import os
import sqlite3
import subprocess
import unittest
from contextlib import closing, redirect_stdout
from pathlib import Path
from typing import cast, final
from unittest import mock


from scripts.style.ledger_test_support import ticket_owner_id

from scripts.style import style_plan
from scripts.style import style_ledger
from scripts.style import approved_renames

from scripts.style import mechanical_classes
from scripts.style import rules

from scripts.style.style_plan_test_support import StylePlanFixture, unit_staff_id


@final
class StylePlanTests(StylePlanFixture):
    def test_approved_rename_allows_only_reference_edits_in_an_outside_file(self) -> None:
        before = ('// Outcome::OldName stays in this comment.\r\n'
                  + 'fn read() { /* nested /* note */ comment */ Outcome :: OldName; '
                  + 'let text = r#"Outcome::OldName é"#; }\r\n')
        self.write(self.sources[1], before)
        _ = self.commit()
        ticket = self.approved_reference_ticket()
        self.write(self.sources[0], "// The finding file keeps unrestricted approval.\nenum Outcome { NewName }\n")
        self.write(self.sources[1], before.replace("Outcome :: OldName", "Outcome :: NewName"))
        _ = self.git("add", self.sources[1])
        self.assertEqual(self.reference_gate(ticket), (0, "approved proposal\n"))

    def test_approved_rename_allows_two_pairs_and_qualified_use_leaves(self) -> None:
        before = ("use crate::m::Outcome::{OldName, Other};\n"
                  + "use crate::m::Outcome::SignalFailed;\n"
                  + "fn read() { Outcome::OldName; Outcome::SignalFailed; }\n")
        self.write(self.sources[1], before)
        _ = self.commit()
        ticket = self.approved_reference_ticket((("Outcome::OldName", "Outcome::NewName"),
                                                 ("Outcome::SignalFailed", "Outcome::Failed")))
        self.write(self.sources[1], before.replace("OldName", "NewName").replace("SignalFailed", "Failed"))
        self.assertEqual(self.reference_gate(ticket), (0, "approved proposal\n"))

    def test_approved_rename_updates_comments_without_code_changes(self) -> None:
        before = ("// OldName and Outcome::OldName\r\n"
                  + "/// See OldName and Outcome::OldName.\r\n"
                  + "//! OldName remains; Outcome::OldName changes.\r\n"
                  + "/* OldName /* Outcome::OldName */ OldName */\r\n"
                  + "/** OldName */\r\n")
        self.write(self.sources[1], before)
        _ = self.commit()
        ticket = self.approved_reference_ticket()
        after = before.replace("OldName", "NewName").replace("//! NewName", "//! OldName")
        self.write(self.sources[1], after)
        self.assertEqual(self.reference_gate(ticket), (0, "approved proposal\n"))

    def test_approved_rename_updates_code_and_comments_together(self) -> None:
        before = "// OldName and Outcome::OldName\nfn read() { Outcome::OldName; }\n"
        self.write(self.sources[1], before)
        _ = self.commit()
        ticket = self.approved_reference_ticket()
        self.write(self.sources[1], before.replace("OldName", "NewName"))
        self.assertEqual(self.reference_gate(ticket), (0, "approved proposal\n"))

    def test_approved_rename_refuses_other_comment_changes_and_partial_words(self) -> None:
        before = "// OldName OldNameX XOldName _OldName OldName_ 1OldName OldName1\n"
        self.write(self.sources[1], before)
        _ = self.commit()
        ticket = self.approved_reference_ticket()
        for after in (before.replace("OldName ", "NewName ", 1) + "// extra\n",
                      before.replace("OldNameX", "NewNameX"),
                      before.replace("XOldName", "XNewName"),
                      before.replace("_OldName", "_NewName"),
                      before.replace("OldName_", "NewName_"),
                      before.replace("1OldName", "1NewName"),
                      before.replace("OldName1", "NewName1")):
            with self.subTest(after=after):
                self.write(self.sources[1], after)
                result, output = self.reference_gate(ticket)
                self.assertEqual(result, 1, output)

    def test_approved_rename_keeps_string_and_char_literals_unchanged(self) -> None:
        before = 'fn read() { let text = "OldName"; let raw = r#"Outcome::OldName"#; let c = \'A\'; }\n'
        self.write(self.sources[1], before)
        _ = self.commit()
        ticket = self.approved_reference_ticket()
        for after in (before.replace('"OldName"', '"NewName"'),
                      before.replace("Outcome::OldName", "Outcome::NewName"),
                      before.replace("'A'", "'B'")):
            with self.subTest(after=after):
                self.write(self.sources[1], after)
                result, output = self.reference_gate(ticket)
                self.assertEqual(result, 1, output)

    def test_approved_rename_updates_only_whole_names_in_markdown(self) -> None:
        path = "docs/renames.md"
        before = "# Outcome::OldName\r\nUse OldName; OldName may stay. OldNameX is unrelated. é\r\n"
        self.write(path, before)
        _ = self.commit()
        ticket = self.approved_reference_ticket()
        after = before.replace("Outcome::OldName", "Outcome::NewName").replace("Use OldName", "Use NewName")
        self.write(path, after)
        self.assertEqual(self.reference_gate(ticket), (0, "approved proposal\n"))
        for refused in (after.replace("may stay", "can stay"),
                        after.replace("OldNameX", "NewNameX"), after.replace("\r\n", "\n")):
            with self.subTest(after=refused):
                self.write(path, refused)
                result, output = self.reference_gate(ticket)
                self.assertEqual(result, 1, output)
                self.assertIn(path, output)

    def test_approved_rename_refuses_reference_edits_in_other_file_types(self) -> None:
        paths = ("notes.txt", "settings.toml")
        for path in paths:
            self.write(path, "Outcome::OldName\n")
        _ = self.commit()
        ticket = self.approved_reference_ticket()
        for path in paths:
            with self.subTest(path=path):
                self.write(path, "Outcome::NewName\n")
                result, output = self.reference_gate(ticket)
                self.assertEqual(result, 1, output)
                self.assertIn(path, output)
                self.write(path, "Outcome::OldName\n")

    def test_approved_rename_ticket_requires_comment_and_markdown_updates(self) -> None:
        _ = self.approved_reference_ticket()
        self.write_approved_phase()
        sentence = ("For each of these renames, find every use through the compiler, not a text search, and update each one "
                    + "in other files: `Outcome::OldName` -> `Outcome::NewName`. "
                    + "Also update every comment and Markdown doc in the repo that means the "
                    + "renamed item, and leave mentions of other items with the same name. "
                    + "Change nothing else in those files.")
        self.assertIn("Apply the approved findings. " + sentence, self.plan.read_text())

    def test_a_proposal_phase_lists_each_widened_path_under_its_files(self) -> None:
        _ = self.doCleanups()
        for needs in ((), ("crates/tui_pane/src/lib.rs",)):
            with self.subTest(needs=needs):
                case = StylePlanTests()
                case.setUp()
                try:
                    ticket = case.approved_reference_ticket((("Outcome::OldName", "Outcome::NewName"),
                                                              ("Outcome::SignalFailed", "Outcome::Failed")))
                    extra = "crates/alpha/src/extra.rs"
                    with redirect_stdout(io.StringIO()):
                        self.assertEqual(style_plan.main(["widen", "--repo", str(case.repo), "--ticket", ticket, "--files", extra,
                                                          "--reason", "the move needs a new file", "--by", "showrunner"]), 0)
                    ledger = case.ledger()
                    identities = ledger.ticket_get(ticket)["finding_ids"]
                    for identity in identities:
                        for path in needs:
                            ledger.finding_need(identity, path)
                        ledger.finding_need(identity, case.sources[0])
                    case.write_approved_phase()
                    at = ledger.ticket_open(ticket_owner_id("names"))[0]["widened"][0]["widened_at"]
                    prefix = (f"**Files:**\n- `{case.sources[0]}`\n"
                              + f"- widened: {extra} — the move needs a new file (showrunner, {at})\n")
                    self.assertIn(prefix, case.plan.read_text())
                    files = case.plan.read_text().split("**Files:**\n", 1)[1].split("\n\n", 1)[0]
                    self.assertEqual(files.count(f"- `{case.sources[0]}`"), 1)
                    for path in needs:
                        line = f"- `{path}` — needed by finding {', '.join(identities)}"
                        self.assertIn(prefix + line + "\n", case.plan.read_text())
                        self.assertEqual(files.count(f"- `{path}`"), 1)
                finally:
                    case.doCleanups()

    def test_a_proposal_phase_names_each_qualified_pair_and_keeps_a_bare_rename_in_its_own_file(self) -> None:
        _ = self.approved_reference_ticket((("Outcome::OldName", "Outcome::NewName"), ("helper", "assistant")))
        self.write_approved_phase()
        text = self.plan.read_text()
        self.assertIn("update each one in other files: `Outcome::OldName` -> `Outcome::NewName`. ", text)
        self.assertNotIn("`helper`", text)
        self.assertIn("Apply any other rename only in its own file; when other files use its old name, hold it. ", text)

    def test_a_proposal_phase_with_only_qualified_pairs_has_no_own_file_rename_line(self) -> None:
        _ = self.approved_reference_ticket()
        self.write_approved_phase()
        self.assertNotIn("Apply any other rename only in its own file", self.plan.read_text())

    def test_nonrename_approval_ticket_omits_comment_and_markdown_instruction(self) -> None:
        _ = self.approved_reference_ticket(kinds=("type",))
        self.write_approved_phase()
        self.assertNotIn("find every use through the compiler", self.plan.read_text())

    def test_a_proposal_phase_lists_only_the_cells_record_takes_and_leaves_out_a_tier_b_cell_with_no_listed_site(self) -> None:
        rule = self.rule("partial", "tier: B\nfamily: names\ncandidates:\n  kind: regex\n  pattern: absent\n  surface: partial\n"
                         + "  uncovered: Read everything else.\n")
        uncovered = rule + "#uncovered"
        self.sync()
        issue = self.root / "partial-approval.md"
        ledger = self.ledger()
        blob = next(item.blob for item in rules.load_rules(self.repo) if item.rule_id == rule)
        ids = tuple(ledger.hold(held_rule, self.sources[0], line, "rename", "rename", "Outcome::Old", "Outcome::New", blob,
                                "outside").finding_id for line, held_rule in enumerate((rule, uncovered), start=1))
        for finding_id in ids:
            ledger.file_held(finding_id, issue)
        _ = issue.write_text(f"---\nstatus: open\ntags: [proposal, style]\nunit: {unit_staff_id('names')}\nplan: approved\n"
                             + f"style_rule: {rule}\nstyle_crate: crates/alpha\nstyle_rule_blob: {blob}\n---\n")
        ledger.ticket_add("1", ticket_owner_id("names"), "proposal", "names", "crates/alpha", [self.sources[0]],
                          [rule, uncovered], proposal=str(issue), finding_ids=ids)
        ticket = ledger.queued_tickets(ticket_owner_id("names"))[0]
        ledger.ticket_assign(ticket["id"], "demo-style-names")
        style_plan.write_phase(ledger, ticket, self.plan, self.plan_header())
        text = self.plan.read_text()
        self.assertIn("`style_ledger.py record --ticket 1 --rule <rule> --path <file>`", text)
        self.assertIn(f"- `{uncovered}` · `{self.sources[0]}`", text)
        self.assertNotIn(f"- `{rule}` · `{self.sources[0]}`", text)
        self.assertNotIn("Record every judged cell", text)

    def test_a_proposal_phase_gives_no_cell_at_a_ticket_file_that_record_refuses_for_its_rule(self) -> None:
        rule = self.rule("approved-cells")
        self.write("docs/notes.md", "Outcome::Old\n")
        self.tip = self.commit()
        self.sync()
        issue = self.root / "cells-approval.md"
        ledger = self.ledger()
        blob = next(item.blob for item in rules.load_rules(self.repo) if item.rule_id == rule)
        ids = tuple(ledger.hold(rule, path, 1, "rename", "rename", "Outcome::Old", "Outcome::New", blob, "outside").finding_id
                    for path in (self.sources[0], "docs/notes.md"))
        for finding_id in ids:
            ledger.file_held(finding_id, issue)
        _ = issue.write_text(f"---\nstatus: open\ntags: [proposal, style]\nunit: {unit_staff_id('names')}\nplan: approved\n"
                             + f"style_rule: {rule}\nstyle_crate: crates/alpha\nstyle_rule_blob: {blob}\n---\n")
        ledger.ticket_add("1", ticket_owner_id("names"), "proposal", "names", "crates/alpha", [self.sources[0], "docs/notes.md"],
                          [rule], proposal=str(issue), finding_ids=ids)
        ticket = ledger.queued_tickets(ticket_owner_id("names"))[0]
        ledger.ticket_assign(ticket["id"], "demo-style-names")
        style_plan.write_phase(ledger, ticket, self.plan, self.plan_header())
        text = self.plan.read_text()
        self.assertIn(f"- `{rule}` · `{self.sources[0]}`", text)
        self.assertNotIn(f"- `{rule}` · `docs/notes.md`", text)

    def test_prose_rename_approval_ticket_omits_reference_update_instruction(self) -> None:
        _ = self.foot_rename_ticket("Rename foot.rs to summary_foot.rs.")
        self.write_approved_phase()
        self.assertIn("Apply the approved findings.", self.plan.read_text())
        self.assertIn("For a file rename", self.plan.read_text())
        self.assertNotIn("find every use through the compiler", self.plan.read_text())

    def test_bare_rename_mention_ignores_unknown_tokens_without_the_old_name(self) -> None:
        self.write(self.sources[0], "enum Outcome { OldName, Other }\n")
        self.write(self.sources[2], "fn unrelated() { § }\n")
        self.write("docs/renames.md", "Use OldName.\n")
        _ = self.commit()
        ticket = self.approved_reference_ticket()
        self.write("docs/renames.md", "Use NewName.\n")
        self.assertEqual(self.reference_gate(ticket), (0, "approved proposal\n"))

    def test_bare_rename_mention_allows_no_matching_rust_files(self) -> None:
        self.write(self.sources[2], "fn unrelated() { § }\n")
        self.write("docs/renames.md", "Use OldName.\n")
        _ = self.commit()
        ticket = self.approved_reference_ticket()
        self.write("docs/renames.md", "Use NewName.\n")
        self.assertEqual(self.reference_gate(ticket), (0, "approved proposal\n"))

    def test_bare_rename_mention_refuses_unknown_tokens_in_a_matching_file(self) -> None:
        self.write(self.sources[0], "enum Outcome { OldName, Other }\n")
        self.write(self.sources[2], "fn read() { Outcome::OldName; § }\n")
        self.write("docs/renames.md", "Use OldName.\n")
        _ = self.commit()
        ticket = self.approved_reference_ticket()
        self.write("docs/renames.md", "Use NewName.\n")
        result, output = self.reference_gate(ticket)
        self.assertEqual(result, 1, output)
        self.assertIn("docs/renames.md", output)

    def test_read_ticket_omits_comment_and_markdown_instruction(self) -> None:
        _ = self.rule("read")
        self.sync()
        self.assertEqual(self.run_next()[0], 0)
        self.assertNotIn("find every use through the compiler", self.plan.read_text())

    def test_approved_rename_allows_bare_comment_mentions_of_a_unique_item(self) -> None:
        self.write(self.sources[0], "enum Outcome { OldName, Other }\nimpl Outcome { fn read() { Self::OldName; } }\n")
        self.write(self.sources[1], "use crate::Outcome::{OldName};\n// OldName and Outcome::OldName\n")
        _ = self.commit()
        ticket = self.approved_reference_ticket()
        self.write(self.sources[1], "use crate::Outcome::{OldName};\n// NewName and Outcome::OldName\n")
        self.assertEqual(self.reference_gate(ticket), (0, "approved proposal\n"))

    def test_approved_rename_refuses_ambiguous_bare_comments_but_allows_qualified_mentions(self) -> None:
        self.write(self.sources[0], "enum Outcome { OldName, Other }\n")
        self.write(self.sources[2], "enum Different { OldName, Other }\n")
        self.write(self.sources[1], "// OldName and Outcome::OldName\n")
        _ = self.commit()
        ticket = self.approved_reference_ticket()
        self.write(self.sources[1], "// NewName and Outcome::OldName\n")
        self.assertEqual(self.reference_gate(ticket)[0], 1)
        self.write(self.sources[1], "// OldName and Outcome::NewName\n")
        self.assertEqual(self.reference_gate(ticket), (0, "approved proposal\n"))

    def test_approved_rename_allows_bare_markdown_mentions_of_a_unique_item(self) -> None:
        self.write(self.sources[0], "enum Outcome { OldName, Other }\n")
        self.write("docs/renames.md", "Use OldName and Outcome::OldName.\n")
        _ = self.commit()
        ticket = self.approved_reference_ticket()
        self.write("docs/renames.md", "Use NewName and Outcome::OldName.\n")
        self.assertEqual(self.reference_gate(ticket), (0, "approved proposal\n"))

    def test_approved_rename_refuses_ambiguous_bare_markdown_but_allows_qualified_mentions(self) -> None:
        self.write(self.sources[0], "enum Outcome { OldName, Other }\n")
        self.write(self.sources[2], "enum Different { OldName, Other }\n")
        self.write("docs/renames.md", "Use OldName and Outcome::OldName.\n")
        _ = self.commit()
        ticket = self.approved_reference_ticket()
        self.write("docs/renames.md", "Use NewName and Outcome::OldName.\n")
        self.assertEqual(self.reference_gate(ticket)[0], 1)
        self.write("docs/renames.md", "Use OldName and Outcome::NewName.\n")
        self.assertEqual(self.reference_gate(ticket), (0, "approved proposal\n"))

    def test_approved_rename_checks_ambiguity_at_head_even_in_the_finding_file(self) -> None:
        self.write(self.sources[0], "enum Outcome { OldName }\nenum Different { OldName }\n")
        self.write("docs/renames.md", "Use OldName.\n")
        _ = self.commit()
        ticket = self.approved_reference_ticket()
        self.write(self.sources[0], "enum Outcome { NewName }\n")
        self.write("docs/renames.md", "Use NewName.\n")
        self.assertEqual(self.reference_gate(ticket)[0], 1)

    def test_approved_rename_uses_the_parent_of_an_absolute_approved_path(self) -> None:
        self.write(self.sources[1], "fn read() { library::Outcome::OldName; }\n")
        _ = self.commit()
        ticket = self.approved_reference_ticket((("::library::Outcome::OldName", "::library::Outcome::NewName"),))
        self.write(self.sources[0], "mod library { enum Outcome { NewName } }\n")
        self.write(self.sources[1], "fn read() { library::Outcome::NewName; }\n")
        self.assertEqual(self.reference_gate(ticket), (0, "approved proposal\n"))

    def test_approved_rename_refuses_other_text_changes_in_reference_files(self) -> None:
        before = ('// Outcome::OldName\nfn read() { Outcome::OldName; let n = 1; '
                  + 'let text = "Outcome::OldName"; }\n')
        self.write(self.sources[1], before)
        _ = self.commit()
        ticket = self.approved_reference_ticket()
        reference = before.replace("{ Outcome::OldName", "{ Outcome::NewName")
        for after in (reference.replace("n = 1", "n = 2"),
                      reference.replace("// Outcome::OldName", "// Outcome::NewName extra"),
                      reference.replace('"Outcome::OldName"', '"Outcome::NewName"'),
                      reference.replace("let n", "let  n"),
                      before.replace("\n", "\r\n")):
            with self.subTest(after=after):
                self.write(self.sources[1], after)
                result, output = self.reference_gate(ticket)
                self.assertEqual(result, 1, output)
                self.assertIn("files outside approved findings:", output)
                self.assertIn(self.sources[1] + " (change is not an approved rename reference)", output)

    def test_approved_proposal_gate_allows_the_files_of_a_pure_move_and_only_them(self) -> None:
        ticket = self.approved_reference_ticket(rule_name="module-structure")
        lib, other, extra = self.sources[2], self.sources[3], "crates/beta/src/extra.rs"
        limit = "/// The most rows.\npub(crate) const LIMIT: usize = 4;\n"
        text = "\npub fn text() -> &'static str { stringify!(a+b) }\n"
        self.write(lib, "mod extra;\nmod other;\n\npub fn value() -> usize { crate::other::LIMIT }\n" + text)
        self.write(other, limit + "\npub(crate) fn read() -> usize { 1 }\n")
        self.write(extra, "pub(crate) fn more() -> usize { 2 }\n")
        _ = self.commit()
        moved = "mod constants;\nmod extra;\nmod other;\n\npub fn value() -> usize { crate::constants::LIMIT }\n" + text
        self.write(lib, moved)
        self.write(other, "pub(crate) fn read() -> usize { 1 }\n")
        self.write("crates/beta/src/constants.rs", limit)
        self.assertEqual(self.reference_gate(ticket), (0, "approved proposal\n"))
        constants = "crates/beta/src/constants.rs"
        # The proof reads code without its spacing, and judge also reads the spacing inside a macro.
        for path, edit, refused in ((lib, moved.replace("LIMIT }", "LIMIT + 1 }"), (lib, other, constants)),
                                    (lib, moved.replace("a+b", "a + b"), (lib, other, constants)),
                                    (extra, "pub(crate) fn more() -> usize {  2 }\n", (extra,))):
            with self.subTest(edit=edit):
                self.write(path, edit)
                result, output = self.reference_gate(ticket)
                self.assertEqual(result, 1, output)
                self.assertIn("files outside approved findings:", output)
                for name in (lib, other, constants, extra):
                    self.assertEqual(name + " (change" in output, name in refused, output)
                self.write(lib, moved)
                self.write(extra, "pub(crate) fn more() -> usize { 2 }\n")

    def test_approved_proposal_gate_allows_a_stale_lint_removal_and_holds_an_added_allow_or_code(self) -> None:
        ticket = self.approved_reference_ticket(rule_name="test-module-allow-boilerplate")
        other = self.sources[3]
        tests = "#[cfg(test)]\n#[allow(clippy::panic, clippy::unwrap_used)]\nmod tests {\n    fn example() {}\n}\n"
        self.write(other, "pub fn read() -> usize { 1 }\n\n" + tests)
        _ = self.commit()
        removed = "pub fn read() -> usize { 1 }\n\n" + tests.replace("clippy::panic, ", "")
        self.write(other, removed)
        self.assertEqual(self.reference_gate(ticket), (0, "approved proposal\n"))
        for edit in (removed.replace("clippy::unwrap_used", "clippy::unwrap_used, clippy::expect_used"),
                     removed.replace("#[cfg(test)]", "#[allow(dead_code)]\n#[cfg(test)]"),
                     removed.replace("{ 1 }", "{ 2 }")):
            with self.subTest(edit=edit):
                self.write(other, edit)
                result, output = self.reference_gate(ticket)
                self.assertEqual(result, 1, output)
                self.assertIn(f"files outside approved findings: {other} (change", output)

    def test_approved_proposal_gate_refuses_a_lint_removal_for_a_ticket_of_another_class(self) -> None:
        ticket = self.approved_reference_ticket(rule_name="module-structure")
        tests = "#[cfg(test)]\n#[allow(clippy::panic, clippy::unwrap_used)]\nmod tests {\n    fn example() {}\n}\n"
        self.write(self.sources[3], "pub fn read() -> usize { 1 }\n\n" + tests)
        _ = self.commit()
        self.write(self.sources[3], "pub fn read() -> usize { 1 }\n\n" + tests.replace("clippy::panic, ", ""))
        result, output = self.reference_gate(ticket)
        self.assertEqual(result, 1, output)
        self.assertIn(f"files outside approved findings: {self.sources[3]} (change", output)

    def test_approved_proposal_gate_reports_a_refused_class_list_as_exit_two(self) -> None:
        ticket = self.approved_reference_ticket()
        self.write(self.sources[3], "pub fn moved() -> usize { 1 }\n")
        refused = mechanical_classes.ClassListError("class pure-move names the unknown proof copy")
        with mock.patch.object(mechanical_classes, "classes", side_effect=refused):
            self.assertEqual(self.reference_gate(ticket), (2, "error: class pure-move names the unknown proof copy\n"))

    def test_approved_proposal_gate_allows_a_widened_file_and_only_it(self) -> None:
        _ = self.doCleanups()
        for authorization in ("widen", "need"):
            with self.subTest(authorization=authorization):
                case = StylePlanTests()
                case.setUp()
                try:
                    ticket = case.approved_reference_ticket()
                    outside, other = case.sources[3], case.sources[2]
                    case.write(outside, "pub fn moved() -> usize { 1 }\n")
                    result, output = case.reference_gate(ticket)
                    self.assertEqual(result, 1, output)
                    refusal = "files outside approved findings: {} (change is not an approved rename reference)"
                    self.assertIn(refusal.format(outside), output)
                    if authorization == "need":
                        ledger = case.ledger()
                        identity, = ledger.ticket_get(ticket)["finding_ids"]
                        ledger.finding_need(identity, outside)
                        with self.assertRaisesRegex(style_ledger.LedgerError, "already allowed by the approved findings"):
                            style_plan.widen(case.repo, ticket, (outside,), "the move needs a new file", "showrunner")
                    else:
                        widened = io.StringIO()
                        with redirect_stdout(widened):
                            self.assertEqual(style_plan.main(["widen", "--repo", str(case.repo), "--ticket", ticket, "--files", outside,
                                                              "--reason", "the move needs a new file", "--by", "showrunner"]), 0)
                        line = widened.getvalue()
                        self.assertTrue(line.startswith(f"widened: {outside} — the move needs a new file (showrunner, ")
                                        and line.endswith(")\n") and line.count("\n") == 1, line)
                    self.assertEqual(case.reference_gate(ticket), (0, "approved proposal\n"))
                    case.write(other, "pub fn other() -> usize { 2 }\n")
                    result, output = case.reference_gate(ticket)
                    self.assertEqual(result, 1, output)
                    self.assertIn(refusal.format(other) + "\n", output)
                    self.assertNotIn(outside, output)
                finally:
                    case.doCleanups()

    def test_ticket_footprints_approve_both_destinations_of_a_compound_rename(self) -> None:
        folder = "crates/alpha/src"
        old, companion = f"{folder}/probe.rs", f"{folder}/probe_tests.rs"
        rule = self.rule("compound-rename")
        issue = self.file_rename_decision(
            rule, ((old, "Rename probe.rs to first.rs. Rename probe_tests.rs to first_tests.rs. Update imports."),),
            ((companion, "fn sample() {}\n"),),
        )
        ledger = self.ledger()
        finding, = ledger.proposal_findings(issue)
        ticket = ledger.next_ticket_id()
        ledger.ticket_add(ticket, ticket_owner_id("names"), "proposal", "names", "crates/alpha", [old], [rule],
                          proposal=str(issue), finding_ids=(finding.finding_id,))
        ledger.ticket_assign(ticket, "main")
        record = ledger.ticket_get(ticket)
        edits = style_plan.EditedFiles(self.repo, ledger, style_plan.NoUnitBranch())
        expected = {old, companion, f"{folder}/first.rs", f"{folder}/first_tests.rs"}
        self.assertTrue(expected.issubset(edits.footprint(finding).files))
        self.assertTrue(expected.issubset(style_plan.ticket_paths(ledger, record, self.repo)))
        _ = (self.repo / old).rename(self.repo / folder / "first.rs")
        _ = (self.repo / companion).rename(self.repo / folder / "first_tests.rs")
        self.write(self.sources[0], "mod gauge;\nmod first;\nmod render;\n")
        self.write(f"{folder}/render.rs", "pub fn draw() { crate::first::sample(); }\n")
        self.assertEqual(self.reference_gate(ticket), (0, "approved proposal\n"))

    def test_gate_requires_both_compound_moves_and_checks_the_first_move_contents(self) -> None:
        folder = "crates/alpha/src"
        old, companion = f"{folder}/foot.rs", f"{folder}/foot_tests.rs"
        new, companion_new = f"{folder}/summary_foot.rs", f"{folder}/summary_foot_tests.rs"
        rule = self.rule("compound-rename")
        issue = self.file_rename_decision(
            rule, ((old, "Rename foot.rs to summary_foot.rs. Rename foot_tests.rs to summary_foot_tests.rs. "
                         + "Update the module declaration, imports, path attribute and documentation references."),),
            ((old, "pub struct SummaryFoot;\n"), (companion, "fn sample() {}\n")),
        )
        ledger = self.ledger()
        finding, = ledger.proposal_findings(issue)
        ticket = ledger.next_ticket_id()
        ledger.ticket_add(ticket, ticket_owner_id("names"), "proposal", "names", "crates/alpha", [old], [rule],
                          proposal=str(issue), finding_ids=(finding.finding_id,))
        ledger.ticket_assign(ticket, "main")
        _ = (self.repo / old).rename(self.repo / new)
        with self.subTest("second move missing"):
            result, output = self.reference_gate(ticket)
            self.assertEqual(result, 1, output)
            self.assertIn("not applied", output)
        _ = (self.repo / companion).rename(self.repo / companion_new)
        self.write(new, "pub fn unrelated() {}\n")
        with self.subTest("first move replaces the file"):
            result, output = self.reference_gate(ticket)
            self.assertEqual(result, 1, output)
            self.assertIn(f"{new} (renamed from {old}: change is not only the rename)", output)
        self.write(new, "pub struct SummaryFoot;\n")
        self.assertEqual(self.reference_gate(ticket), (0, "approved proposal\n"))

    def test_compound_rename_keeps_repository_relative_companion_paths(self) -> None:
        folder = "crates/alpha/src"
        old, companion = f"{folder}/probe.rs", f"{folder}/gauge.rs"
        rule = self.rule("compound-rename")
        issue = self.file_rename_decision(rule, (
            (old, f"Rename probe.rs to first.rs. Rename {companion} to {folder}/meter.rs."),
        ))
        ledger = self.ledger()
        finding, = ledger.proposal_findings(issue)
        paths = style_plan.EditedFiles(self.repo, ledger, style_plan.NoUnitBranch()).footprint(finding).files
        self.assertIn(f"{folder}/meter.rs", paths)
        self.assertNotIn(f"{folder}/{folder}/meter.rs", paths)

    def foot_rename_ticket(self, after: str) -> str:
        folder = "crates/alpha/src"
        old, companion = f"{folder}/foot.rs", f"{folder}/foot_tests.rs"
        rule = self.rule("foot-rename")
        issue = self.file_rename_decision(rule, ((old, after),),
                                         ((old, "pub struct SummaryFoot;\n"), (companion, "fn sample() {}\n")))
        ledger = self.ledger()
        finding, = ledger.proposal_findings(issue)
        ticket = ledger.next_ticket_id()
        ledger.ticket_add(ticket, ticket_owner_id("names"), "proposal", "names", "crates/alpha", [old], [rule],
                          proposal=str(issue), finding_ids=(finding.finding_id,))
        return ticket

    def test_gate_requires_every_rename_in_a_conjunction(self) -> None:
        ticket = self.foot_rename_ticket("Rename foot.rs to summary_foot.rs and foot_tests.rs to summary_foot_tests.rs.")
        src = self.repo / "crates/alpha/src"
        _ = (src / "foot.rs").rename(src / "summary_foot.rs")
        result, output = self.reference_gate(ticket)
        self.assertEqual(result, 1, output)
        self.assertIn("not applied", output)
        _ = (src / "foot_tests.rs").rename(src / "summary_foot_tests.rs")
        self.assertEqual(self.reference_gate(ticket), (0, "approved proposal\n"))

    def assert_gate_requires_both_explicit_moves(self, after: str) -> None:
        ticket = self.foot_rename_ticket(after)
        src = self.repo / "crates/alpha/src"
        _ = (src / "foot.rs").rename(src / "summary_foot.rs")
        result, output = self.reference_gate(ticket)
        self.assertEqual(result, 1, output)
        self.assertIn("not applied", output)
        _ = (src / "foot_tests.rs").rename(src / "summary_foot_tests.rs")
        self.assertEqual(self.reference_gate(ticket), (0, "approved proposal\n"))

    def test_gate_checks_a_move_joined_by_plus(self) -> None:
        self.assert_gate_requires_both_explicit_moves(
            "Rename foot.rs to summary_foot.rs, plus `foot_tests.rs` to `summary_foot_tests.rs`.")

    def test_gate_checks_an_explicit_companion_file_sentence(self) -> None:
        self.assert_gate_requires_both_explicit_moves(
            "Rename foot.rs to summary_foot.rs. Rename the companion file foot_tests.rs to summary_foot_tests.rs.")

    def test_gate_refuses_an_implied_file_move(self) -> None:
        ticket = self.foot_rename_ticket(
            "Rename foot.rs to summary_foot.rs. Apply the same prefix to its companion tests.")
        src = self.repo / "crates/alpha/src"
        _ = (src / "foot.rs").rename(src / "summary_foot.rs")
        result, output = self.reference_gate(ticket)
        self.assertEqual(result, 1, output)
        self.assertIn("cannot parse all file renames", output)
        ledger = self.ledger()
        with self.assertRaisesRegex(style_ledger.LedgerError, "cannot parse all file renames"):
            _ = style_plan.ticket_paths(ledger, ledger.ticket_get(ticket), self.repo)

    def test_gate_refuses_a_directory_rename_without_file_pairs(self) -> None:
        ticket = self.foot_rename_ticket("Rename the directory foot to summary_foot.")
        result, output = self.reference_gate(ticket)
        self.assertEqual(result, 1, output)
        self.assertIn("cannot parse all file renames", output)

    def test_gate_refuses_a_bare_target_with_a_file_source(self) -> None:
        ticket = self.approved_reference_ticket((("foot.rs", "summary_foot"),))
        result, output = self.reference_gate(ticket)
        self.assertEqual(result, 1, output)
        self.assertIn("cannot parse all file renames", output)

    def test_gate_refuses_a_bare_file_pair_without_a_move_plan(self) -> None:
        ticket = self.approved_reference_ticket((("foot.rs", "summary_foot.rs"),))
        result, output = self.reference_gate(ticket)
        self.assertEqual(result, 1, output)
        self.assertIn("cannot parse all file renames", output)

    def test_gate_checks_each_name_in_the_target_scope(self) -> None:
        ticket = self.approved_reference_ticket((("Wrap::Old", "Wrap::New"),))
        self.write(self.sources[0], "enum Wrap { Old, New }\nenum Other { Old }\n")
        result, output = self.reference_gate(ticket)
        self.assertEqual(result, 1, output)
        self.assertIn("not applied", output)
        self.write(self.sources[0], "enum Wrap { Old }\nenum Other { New }\n")
        self.assertEqual(self.reference_gate(ticket)[0], 1)
        self.write(self.sources[0], "enum Wrap { New }\nenum Other { Old }\n")
        self.assertEqual(self.reference_gate(ticket), (0, "approved proposal\n"))

    def test_gate_requires_positive_proof_of_a_qualified_rename(self) -> None:
        ticket = self.approved_reference_ticket((("Wrap::Old", "Wrap::New"),))
        ledger = self.ledger()
        ledger.ticket_assign(ticket, "main")
        finding, = ledger.ticket_get(ticket)["finding_ids"]
        for source in ("enum Wrap { Gone }\nenum Other { New }\n", "enum Wrap { r#Old, New }\n"):
            with self.subTest(source=source):
                self.write(self.sources[0], source)
                result, output = self.reference_gate(ticket)
                self.assertEqual(result, 1, output)
                self.assertIn(f"review: {finding}:", output)
        self.assertEqual(ledger.rename_disposition(finding), approved_renames.NoOuterBindingBlock())
        with closing(sqlite3.connect(ledger.database)) as connection:
            reason = cast(tuple[str] | None, connection.execute(
                "SELECT reason FROM rename_gate_refusals WHERE ticket=? AND finding_id=?", (ticket, finding),
            ).fetchone())
            self.assertIsNotNone(reason)

    def test_queued_need_records_a_same_unit_holder_without_granting_gate_permission(self) -> None:
        ticket = self.approved_reference_ticket()
        ledger = self.ledger()
        record = ledger.ticket_get(ticket)
        finding, = record["finding_ids"]
        held, free = self.sources[1:3]
        holder = ledger.next_ticket_id()
        ledger.ticket_add(holder, ticket_owner_id("names"), "read", "names", "crates/alpha", [held], record["rules"])
        ledger.ticket_assign(holder, "main")
        ledger.finding_need(finding, held)
        self.assertEqual(ledger.finding_needs(finding, acquired_only=True), ())
        self.assertEqual(ledger.finding_needs(finding), (held,))
        ledger.finding_need(finding, free)
        self.assertIn(free, ledger.open_ticket_reservation(record).paths)
        with self.assertRaisesRegex(style_ledger.TicketPathOverlap, f"overlap: {held} held by ticket {holder}"):
            _ = ledger.check_assignment_paths(record)
        ledger.ticket_drop(holder)
        ledger.finding_need(finding, held)
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(style_plan.gate(self.repo, ticket), 1)
        self.assertIn("gate requires an assigned ticket", output.getvalue())

    def test_gate_approves_a_struct_field_rename_outside_the_other_structs_scope(self) -> None:
        ticket = self.approved_reference_ticket((("Wrap::old", "Wrap::new"),))
        self.write(self.sources[0], "struct Wrap { new: u8 }\nstruct Other { old: u8 }\n")
        self.assertEqual(self.reference_gate(ticket), (0, "approved proposal\n"))

    def test_publication_freezes_the_ticket_before_head_moves_and_recovers_afterward(self) -> None:
        ticket = self.approved_reference_ticket()
        ledger = self.ledger()
        ledger.set_tip("HEAD")
        ledger.ticket_assign(ticket, "main")
        previous = ledger.ticket_base(ticket)
        finding, = ledger.ticket_get(ticket)["finding_ids"]
        other = self.sources[1]
        complete = style_plan.complete_publication

        def race() -> None:
            with style_ledger.open_ledger(self.repo) as writer:
                for mutate in (lambda: writer.finding_need(finding, other),
                               lambda: writer.ticket_widen(ticket, (other,), "extra edit", "director", frozenset),
                               lambda: writer.ticket_yield_findings(ticket, (finding,)),
                               lambda: writer.ticket_yield(ticket, self.sources[0]),
                               lambda: writer.ticket_drop(ticket)):
                    with self.assertRaisesRegex(style_ledger.LedgerError, "publication is pending"):
                        _ = mutate()

        def crash_after_ref(*_args: object) -> None:
            self.assertNotEqual(self.git("rev-parse", "HEAD"), previous)
            raise RuntimeError("crash after update-ref")

        def complete_after_race(repo: Path, reader: style_ledger.Ledger,
                                publication: style_ledger.PendingPublication) -> str:
            race()
            with mock.patch.object(style_plan, "update_ticket_index", side_effect=crash_after_ref):
                return complete(repo, reader, publication)

        with mock.patch.object(style_plan, "verify_source", return_value=0), \
                mock.patch.object(style_plan, "run_verification_step", return_value=subprocess.CompletedProcess([], 0)), \
                mock.patch.object(style_plan, "complete_publication", side_effect=complete_after_race):
            with self.assertRaisesRegex(RuntimeError, "crash after update-ref"):
                _ = style_plan.publish_checkpoint(self.repo, ticket, self.root / "run", "1", "Publish\n")
        checkpoint = self.git("rev-parse", "HEAD")
        self.assertEqual(ledger.ticket_get(ticket)["state"], "assigned")
        self.assertEqual(style_plan.publish_checkpoint(self.repo, ticket, self.root / "run", "1", "Retry\n"), checkpoint)
        self.assertEqual(self.git("rev-list", "--count", previous + "..HEAD"), "1")
        self.assertEqual(ledger.ticket_get(ticket)["checkpoint"], checkpoint)
        self.assertEqual(ledger.finding_needs(finding), ())

    def test_gate_and_checkpoint_refuse_a_blocked_need_until_it_is_acquired(self) -> None:
        ticket = self.approved_reference_ticket()
        ledger = self.ledger()
        ledger.ticket_assign(ticket, "main")
        record = ledger.ticket_get(ticket)
        finding, = record["finding_ids"]
        path = self.sources[1]
        holder = ledger.next_ticket_id()
        ledger.ticket_add(holder, ticket_owner_id("judging"), "read", "names", "crates/alpha", [path], record["rules"])
        ledger.ticket_assign(holder, "main")
        ledger.finding_need(finding, path)
        self.assertEqual(ledger.finding_needs(finding), (path,))
        self.assertNotIn(path, ledger.open_ticket_reservation(record).paths)
        result, output = self.reference_gate(ticket)
        self.assertEqual(result, 1, output)
        self.assertIn(f"blocked need: {path}", output)
        with self.assertRaisesRegex(style_ledger.LedgerError, f"blocked need: {path}"):
            ledger.ticket_checkpoint(ticket, "HEAD")
        ledger.ticket_drop(holder)
        ledger.finding_need(finding, path)
        self.assertIn(path, ledger.open_ticket_reservation(record).editable_paths)
        self.assertEqual(self.reference_gate(ticket), (0, "approved proposal\n"))
        ledger.ticket_checkpoint(ticket, "HEAD")

    def test_gate_refuses_a_bare_target_with_a_directory_source(self) -> None:
        ticket = self.approved_reference_ticket((("directory foot", "summary_foot"),))
        result, output = self.reference_gate(ticket)
        self.assertEqual(result, 1, output)
        self.assertIn("cannot parse all file renames", output)

    def test_gate_refuses_two_identical_sources_with_one_destination(self) -> None:
        self.assert_gate_refuses_shared_destination((
            ("foot.rs", "Rename foot.rs to shared.rs and foot_tests.rs to shared.rs."),
        ))

    def test_ticket_refuses_a_destination_shared_by_separate_findings(self) -> None:
        self.assert_gate_refuses_shared_destination((
            ("foot.rs", "Rename foot.rs to shared.rs."),
            ("foot_tests.rs", "Rename foot_tests.rs to shared.rs."),
        ))

    def assert_gate_refuses_shared_destination(self, renames: tuple[tuple[str, str], ...]) -> None:
        folder = "crates/alpha/src"
        old, other, new = (f"{folder}/{name}.rs" for name in ("foot", "foot_tests", "shared"))
        rule = self.rule("shared-destination")
        source = "fn contents() {}\n"
        issue = self.file_rename_decision(rule, tuple((f"{folder}/{name}", after) for name, after in renames),
                                         ((old, source), (other, source)))
        ledger = self.ledger()
        ticket = ledger.next_ticket_id()
        findings = ledger.proposal_findings(issue)
        ledger.ticket_add(ticket, ticket_owner_id("names"), "proposal", "names", "crates/alpha",
                          [finding.path for finding in findings], [rule], proposal=str(issue),
                          finding_ids=tuple(finding.finding_id for finding in findings))
        refusal = f"more than one file rename.*{new}"
        with self.assertRaisesRegex(style_ledger.LedgerError, refusal):
            ledger.ticket_assign(ticket, "main")
        self.assertEqual(ledger.ticket_get(ticket)["state"], "queued")
        _ = (self.repo / old).rename(self.repo / new)
        (self.repo / other).unlink()
        result, output = self.reference_gate(ticket)
        self.assertEqual(result, 1, output)
        with self.assertRaisesRegex(style_ledger.LedgerError, refusal):
            _ = style_plan.ticket_paths(ledger, ledger.ticket_get(ticket), self.repo)
        self.assertIn(f"more than one file rename: {new}", output)

    def test_gate_refuses_an_unsupported_companion_rename(self) -> None:
        ticket = self.foot_rename_ticket("Rename foot.rs to summary_foot.rs and foot_tests.rs to mod.rs.")
        src = self.repo / "crates/alpha/src"
        _ = (src / "foot.rs").rename(src / "summary_foot.rs")
        result, output = self.reference_gate(ticket)
        self.assertEqual(result, 1, output)
        self.assertIn("cannot parse all file renames", output)

    def test_gate_checks_contents_of_a_quoted_file_rename(self) -> None:
        ticket = self.foot_rename_ticket("Rename `foot.rs` to `summary_foot.rs`.")
        src = self.repo / "crates/alpha/src"
        (src / "foot.rs").unlink()
        self.write("crates/alpha/src/summary_foot.rs", "pub fn unrelated() {}\n")
        result, output = self.reference_gate(ticket)
        self.assertEqual(result, 1, output)
        self.assertIn("change is not only the rename", output)
        self.write("crates/alpha/src/summary_foot.rs", "pub struct SummaryFoot;\n")
        self.assertEqual(self.reference_gate(ticket), (0, "approved proposal\n"))

    def test_gate_refuses_a_file_rename_with_no_applied_check(self) -> None:
        ticket = self.foot_rename_ticket("Rename foot.rs to summary_foot.rs.")
        src = self.repo / "crates/alpha/src"
        self.write("crates/alpha/src/foot/child.rs", "fn child() {}\n")
        _ = self.git("add", ".")
        _ = self.git("commit", "-m", "Add a child module")
        _ = (src / "foot.rs").rename(src / "summary_foot.rs")
        result, output = self.reference_gate(ticket)
        self.assertEqual(result, 1, output)
        self.assertIn("cannot verify file rename", output)

    def test_refill_keeps_a_ticket_queued_while_another_unit_holds_its_destination(self) -> None:
        folder = "crates/alpha/src"
        probe, gauge = f"{folder}/probe.rs", f"{folder}/gauge.rs"
        rule = self.rule("read")
        issue = self.file_rename_decision(
            rule, ((probe, "Rename probe.rs to shared.rs."), (gauge, "Rename gauge.rs to shared.rs.")),
        )
        ledger = self.ledger()
        findings = {finding.path: finding for finding in ledger.proposal_findings(issue)}
        holder = ledger.next_ticket_id()
        ledger.ticket_add(holder, ticket_owner_id("judging"), "proposal", "names", "crates/alpha", [probe], [rule],
                          proposal=str(issue), finding_ids=(findings[probe].finding_id,))
        ledger.ticket_assign(holder, "main")
        queued = ledger.next_ticket_id()
        ledger.ticket_add(queued, ticket_owner_id("names"), "proposal", "names", "crates/alpha", [gauge], [rule],
                          proposal=str(issue), finding_ids=(findings[gauge].finding_id,))
        output = self.refill_with_busy()
        self.assertEqual(ledger.ticket_get(queued)["state"], "queued", output)
        self.assertIn(f"{folder}/shared.rs held by ticket {holder}", output)

    def test_ticket_gate_uses_the_remaining_decision_after_a_finding_yield(self) -> None:
        first, second = self.rule("first"), self.rule("second")
        released = self.approved_decision(first, (self.sources[0],), "type", name="first")
        remaining = self.approved_decision(second, (self.sources[1],), "type", name="second")
        ledger = self.ledger()
        ledger.set_tip("HEAD")
        named, = ledger.proposal_findings(released)
        kept, = ledger.proposal_findings(remaining)
        ticket = ledger.next_ticket_id()
        ledger.ticket_add(ticket, ticket_owner_id("names"), "proposal", "names", "crates/alpha",
                          list(self.sources[:2]), [first, second], proposal=str(released),
                          proposals=(remaining,), finding_ids=(named.finding_id, kept.finding_id))
        ledger.ticket_assign(ticket, "main")
        _ = ledger.ticket_yield_findings(ticket, (named.finding_id,))
        self.assertEqual(ledger.ticket_proposals(ticket), (remaining,))
        self.write(kept.path, "pub struct Outcome;\n")
        self.assertEqual(self.reference_gate(ticket), (0, "approved proposal\n"))

    def test_rule_change_keeps_a_ticket_after_yield_removes_the_last_finding_for_that_rule(self) -> None:
        first, second = self.rule("first"), self.rule("second")
        released = self.approved_decision(first, (self.sources[0],), "type", name="first")
        remaining = self.approved_decision(second, (self.sources[1],), "type", name="second")
        ledger = self.ledger()
        ledger.set_tip("HEAD")
        named, = ledger.proposal_findings(released)
        kept, = ledger.proposal_findings(remaining)
        ticket = ledger.next_ticket_id()
        ledger.ticket_add(ticket, ticket_owner_id("names"), "proposal", "names", "crates/alpha",
                          list(self.sources[:2]), [first, second], proposal=str(released),
                          proposals=(remaining,), finding_ids=(named.finding_id, kept.finding_id))
        ledger.ticket_assign(ticket, "main")
        _ = ledger.ticket_yield_findings(ticket, (named.finding_id,))
        rule_file = self.rule_dir / "first.md"
        _ = rule_file.write_text(rule_file.read_text() + "\nNew requirement.\n")
        _ = ledger.withdraw_superseded({rule.rule_id: rule for rule in rules.load_rules(self.repo)})
        record = ledger.ticket_get(ticket)
        self.assertEqual(record["state"], "assigned")
        self.assertEqual(record["rules"], [second])
        self.assertEqual(record["rule_blobs"], {second: kept.rule_blob})
        self.assertEqual(ledger.named_held((kept.finding_id,))[0].build_ticket, style_ledger.BuildTicket(ticket))

    def test_structural_move_authorizes_its_named_new_file(self) -> None:
        folder, _, _ = self.sources[0].rpartition("/")
        destination = f"{folder}/rows_tests.rs"
        ticket = self.approved_reference_ticket(
            (("fn old() {}", "Move the tests into a new file rows_tests.rs."),), ("restructure",),
        )
        ledger = self.ledger()
        ledger.set_tip("HEAD")
        ledger.ticket_assign(ticket, "main")
        _ = ledger.ticket_base(ticket)
        record = ledger.ticket_get(ticket)
        finding, = ledger.named_held(record["finding_ids"])
        edits = style_plan.EditedFiles(self.repo, ledger, style_plan.NoUnitBranch())
        self.assertIn(destination, edits.footprint(finding).files)
        self.assertIn(destination, style_plan.ticket_paths(ledger, record, self.repo))
        self.write(destination, "fn sample() {}\n")
        self.assertEqual(self.reference_gate(ticket), (0, "approved proposal\n"))
        _ = self.commit()
        self.write(destination, "fn changed() {}\n")
        self.assertEqual(self.reference_gate(ticket), (0, "approved proposal\n"))

    def test_structural_move_allows_edits_to_its_existing_destination(self) -> None:
        existing = self.sources[1]
        ticket = self.approved_reference_ticket(
            (("fn old() {}", f"Move the fixtures into {existing}."),), ("restructure",),
        )
        ledger = self.ledger()
        ledger.set_tip("HEAD")
        ledger.ticket_assign(ticket, "main")
        _ = ledger.ticket_base(ticket)
        record = ledger.ticket_get(ticket)
        self.assertIn(existing, style_plan.ticket_paths(ledger, record, self.repo))
        self.write(existing, "fn fixture() {}\n")
        self.assertEqual(self.reference_gate(ticket), (0, "approved proposal\n"))
        finding, = ledger.named_held(record["finding_ids"])
        edits = style_plan.EditedFiles(self.repo, ledger, style_plan.NoUnitBranch())
        self.assertIn(existing, edits.of(finding))

    def test_assignment_refuses_an_existing_split_destination_held_by_another_unit(self) -> None:
        existing = self.sources[1]
        ticket = self.approved_reference_ticket(
            (("fn old() {}", f"Move the fixtures into {existing}."),), ("restructure",),
        )
        ledger = self.ledger()
        ledger.set_tip("HEAD")
        holder = ledger.next_ticket_id()
        record = ledger.ticket_get(ticket)
        ledger.ticket_add(holder, ticket_owner_id("judging"), "read", "names", "crates/alpha",
                          [existing], record["rules"])
        ledger.ticket_assign(holder, "main")
        with self.assertRaisesRegex(style_ledger.TicketPathOverlap, f"overlap: {existing}.*ticket {holder}"):
            ledger.ticket_assign(ticket, "main")
        self.assertEqual(ledger.ticket_get(ticket)["state"], "queued")

    def test_missing_assigned_reservation_refuses_assignment_and_refill(self) -> None:
        folder = "crates/alpha/src"
        old = f"{folder}/foot.rs"
        rule = self.rule("read")
        issue = self.file_rename_decision(
            rule, ((old, "Rename foot.rs to summary_foot.rs. Move no definitions."),),
            ((old, "pub struct Foot;\n"),),
        )
        ledger = self.ledger()
        finding, = ledger.proposal_findings(issue)
        holder = ledger.next_ticket_id()
        ledger.ticket_add(holder, ticket_owner_id("judging"), "proposal", "names", "crates/alpha", [old], [rule],
                          proposal=str(issue), finding_ids=(finding.finding_id,))
        with closing(sqlite3.connect(ledger.database)) as connection, connection:
            _ = connection.execute("UPDATE tickets SET state='assigned',branch='refs/heads/main' WHERE id=?", (holder,))
        with self.assertRaisesRegex(style_ledger.LedgerError, "missing saved reservation"):
            _ = style_plan.busy_files(self.repo, self.doc, ledger, ticket_owner_id("names"), self.board())
        queued = ledger.next_ticket_id()
        ledger.ticket_add(queued, ticket_owner_id("names"), "read", "names", "crates/beta", [self.sources[3]], [rule])
        with self.assertRaisesRegex(style_ledger.LedgerError, "missing saved reservation"):
            ledger.ticket_assign(queued, "main")
        self.assertEqual(ledger.ticket_get(queued)["state"], "queued")

    def test_peer_scans_use_the_saved_reservation_without_parsing_findings(self) -> None:
        old, new = "crates/alpha/src/probe.rs", "crates/alpha/src/frame_log.rs"
        rule = self.rule("read")
        issue = self.file_rename_decision(rule, ((old, "Rename probe.rs to frame_log.rs."),))
        ledger = self.ledger()
        finding, = ledger.proposal_findings(issue)
        holder = ledger.next_ticket_id()
        ledger.ticket_add(holder, ticket_owner_id("judging"), "proposal", "names", "crates/alpha", [old], [rule],
                          proposal=str(issue), finding_ids=(finding.finding_id,))
        ledger.ticket_assign(holder, "main")
        other = ledger.next_ticket_id()
        ledger.ticket_add(other, ticket_owner_id("names"), "read", "names", "crates/beta", [self.sources[3]], [rule])
        with mock.patch.object(approved_renames, "classify_rename", side_effect=AssertionError("peer rename parsed")):
            busy = style_plan.busy_files(self.repo, self.doc, ledger, ticket_owner_id("names"), self.board())
            ledger.ticket_assign(other, "main")
        plan = ledger.open_ticket_reservation(ledger.ticket_get(holder))
        self.assertEqual(set(style_plan.ticket_paths(ledger, ledger.ticket_get(holder), self.repo)), plan.paths)
        self.assertIn(style_ledger.ReservedPath(new, style_ledger.ReservationRole.NEW_DESTINATION), plan.entries)
        owner = self.sources[0]
        self.assertTrue(busy.contains(owner))
        self.assertNotIn(owner, plan.editable_paths)

    def test_checkpoint_checks_added_edit_paths_against_the_saved_peer_plan(self) -> None:
        ticket = self.approved_reference_ticket()
        ledger = self.ledger()
        ledger.set_tip("HEAD")
        ledger.ticket_assign(ticket, "main")
        _ = ledger.ticket_base(ticket)
        record = ledger.ticket_get(ticket)
        path = self.sources[1]
        holder = ledger.next_ticket_id()
        ledger.ticket_add(holder, ticket_owner_id("judging"), "read", "names", "crates/alpha", [path], record["rules"])
        ledger.ticket_assign(holder, "main")
        self.write(path, "fn changed() {}\n")
        checkpoint = self.commit()
        with self.assertRaisesRegex(style_ledger.TicketPathOverlap, f"overlap: {path}.*ticket {holder}"):
            ledger.ticket_checkpoint(ticket, checkpoint)
        self.assertEqual(ledger.ticket_get(ticket)["state"], "assigned")
        ledger.ticket_drop(holder)
        ledger.ticket_checkpoint(ticket, checkpoint)
        plan = ledger.open_ticket_reservation(ledger.ticket_get(ticket))
        self.assertIn(style_ledger.ReservedPath(path, style_ledger.ReservationRole.CHECKPOINT_EDIT), plan.entries)

    def test_structural_move_reads_the_destination_after_a_source_file_name(self) -> None:
        ticket = self.approved_reference_ticket(
            (("fn old() {}", "Move tests from paint_tests.rs into rows_tests.rs."),), ("restructure",),
        )
        ledger = self.ledger()
        ledger.set_tip("HEAD")
        ledger.ticket_assign(ticket, "main")
        record = ledger.ticket_get(ticket)
        destination = "crates/alpha/src/rows_tests.rs"
        self.assertIn(destination, style_plan.ticket_paths(ledger, record, self.repo))
        self.write(destination, "fn sample() {}\n")
        self.assertEqual(self.reference_gate(ticket), (0, "approved proposal\n"))

    def test_widen_authorizes_a_new_file_in_the_live_dispatch(self) -> None:
        ticket = self.approved_reference_ticket()
        ledger = self.ledger()
        ledger.ticket_assign(ticket, "main")
        run = self.root / "run"
        phase = run / "phases" / "17"
        phase.mkdir(parents=True)
        worker = f"style-ticket-{ticket}"
        state = {"pass": {worker: {"status": "active", "phase": {"id": "17", "title": ticket}}}}
        _ = (run / "progress_history_state.json").write_text(json.dumps(state))
        manifest = phase / "dispatch_1_paths.json"
        paths = phase / "ticket-paths.txt"
        original = {"worker": worker, "round": 1, "paths": [self.sources[0]]}
        _ = manifest.write_text(json.dumps(original))
        _ = paths.write_text(self.sources[0] + "\n")
        destination = "crates/alpha/src/rows_tests.rs"
        with mock.patch.dict(os.environ, {"UNIT_RUN_DIR": str(run)}), redirect_stdout(io.StringIO()):
            style_plan.widen(self.repo, ticket, (destination,), "move the tests", worker)
            paths.unlink()
            style_plan.widen(self.repo, ticket, (destination,), "move the tests", worker)
        self.assertEqual(paths.read_text().splitlines(), [self.sources[0], destination])
        self.assertEqual(json.loads(manifest.read_text()), {**original, "paths": [self.sources[0], destination]})
        self.assertEqual(list(phase.glob("dispatch_*_paths.json")), [manifest])
        self.write(destination, "fn sample() {}\n")
        self.assertEqual(self.reference_gate(ticket), (0, "approved proposal\n"))

    def test_widen_retry_restores_dispatch_paths_with_a_later_queued_ticket(self) -> None:
        ticket = self.approved_reference_ticket()
        ledger = self.ledger()
        ledger.ticket_assign(ticket, "main")
        destination = "crates/alpha/src/shared_tests.rs"
        with mock.patch.dict(os.environ, {"UNIT_RUN_DIR": ""}), redirect_stdout(io.StringIO()):
            style_plan.widen(self.repo, ticket, (destination,), "add tests", "director")
        before = ledger.ticket_get(ticket)["widened"]
        rule = self.rule("later-split")
        issue = self.root / "later-split.md"
        old = self.sources[1]
        blob = next(item.blob for item in rules.load_rules(self.repo) if item.rule_id == rule)
        finding = ledger.hold(rule, old, 1, "move tests", "restructure", "tests",
                              "Move the tests into shared_tests.rs.", blob, "outside")
        ledger.file_held(finding.finding_id, issue)
        queued = ledger.next_ticket_id()
        ledger.ticket_add(queued, ticket_owner_id("judging"), "proposal", "names", "crates/alpha", [old], [rule],
                          proposal=str(issue), finding_ids=(finding.finding_id,))
        run = self.root / "run"
        phase = run / "phases" / "17"
        phase.mkdir(parents=True)
        worker = f"style-ticket-{ticket}"
        state = {"pass": {worker: {"status": "active", "phase": {"id": "17", "title": ticket}}}}
        _ = (run / "progress_history_state.json").write_text(json.dumps(state))
        manifest = phase / "dispatch_1_paths.json"
        original = {"worker": worker, "round": 1, "paths": [self.sources[0]]}
        _ = manifest.write_text(json.dumps(original))
        with redirect_stdout(io.StringIO()):
            style_plan.widen(self.repo, ticket, (destination,), "add tests", "director", session_dir=str(run))
        self.assertEqual(ledger.ticket_get(ticket)["widened"], before)
        self.assertEqual((phase / "ticket-paths.txt").read_text().splitlines(), [self.sources[0], destination])
        self.assertEqual(json.loads(manifest.read_text()), {**original, "paths": [self.sources[0], destination]})

if __name__ == "__main__":
    _ = unittest.main()
