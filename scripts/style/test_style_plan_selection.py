"""Held judgments, proposal state and synchronized branch ancestry."""

from __future__ import annotations

from scripts.production.staff_test_support import foreign_hostname

from scripts.style.ledger_test_support import DEFAULT_STYLE_ASSIGNMENTS, ticket_owner_id
from scripts.style.style_config import StyleUnitAssignment

import errno
import io
import re
import sqlite3
import subprocess
import unittest
from contextlib import closing, redirect_stdout
from dataclasses import replace
from pathlib import Path
from typing import final
from unittest import mock


from scripts.style import style_plan, unit_plans
from scripts.style import style_proposals

from scripts.lib import berth_board
from scripts.style import rules
from scripts.style import style_ledger
from scripts.style import unit_files
from scripts.agents import remote
from scripts.production import staff
from scripts.production.staff_test_support import fixture_member, fixture_unit_location, recorded_showrunner

from scripts.style.style_plan_test_support import StylePlanFixture, unit_staff_id
from scripts.style.unit_plans import AllCrates


def setUpModule() -> None:
    unittest.enterModuleContext(foreign_hostname())


@final
class StylePlanTests(StylePlanFixture):
    def test_refill_uses_its_production_when_another_unit_has_the_same_name(self) -> None:
        _ = self.rule("read")
        self.sync()
        self.finish_judging()
        record = staff.initial_snapshot()
        parent = record.by_doc(remote.local_machine(), self.doc)
        assert parent is not None
        own = next(row for row in record.find(kind="unit", parent=parent.id) if row.name == "names")
        other = fixture_member("showrunner", name="other-style")
        duplicate = fixture_member("unit", name="names", parent=other.id)
        code, output, errors = self.run_next()
        self.assertEqual(code, 0, output + errors)
        tickets = self.ledger().ticket_open(str(own.id))
        self.assertTrue(tickets)
        self.assertEqual({ticket["unit"] for ticket in tickets}, {str(own.id)})
        self.assertEqual(self.ledger().ticket_open(str(duplicate.id)), ())

    def values_unit(self) -> Path:
        worktree = self.root / "values"
        _ = self.git("worktree", "add", "-q", "-b", "demo-style-values", str(worktree))
        plan = worktree / "docs/style-production/units/values-unit.md"
        plan.parent.mkdir(parents=True)
        _ = plan.write_text(self.plan_header().replace("names", "values"))
        self.unit_plan_config(worktree, "values-unit.md")
        _ = fixture_unit_location(recorded_showrunner("sample-style", self.doc), "values", worktree)
        return plan

    def select_for_family(self, family: rules.Family, plan: Path) -> style_ledger.TicketRecord:
        self.finish_judging()
        output = io.StringIO()
        with mock.patch.object(style_proposals, "proposal_command"), redirect_stdout(output):
            code = style_plan.main(["next-phase", "--unit", family, "--plan", str(plan),
                                    "--repo", str(self.repo)])
        self.assertEqual(code, 0, output.getvalue())
        ticket, = self.ledger().ticket_open(ticket_owner_id(family))
        return ticket

    def finish_structure(self, ticket: style_ledger.TicketRecord, rule: str) -> None:
        for path in ticket["files"]:
            self.record(rule, path, self.tip, ticket["id"])
        self.merge_selection(ticket)

    def merge_selection(self, ticket: style_ledger.TicketRecord) -> None:
        ledger = self.ledger()
        ledger.ticket_checkpoint(ticket["id"], self.tip)
        _ = ledger.ticket_merge(ticket["id"])

    def test_refill_alternates_structure_build_and_structure(self) -> None:
        plan = self.modules_unit()
        structure = self.rule("structure", "tier: D\nfamily: modules\ninput_scope: file\n")
        build_rule = self.rule("build", "tier: C\nfamily: modules\ninput_scope: file\n")
        self.sync()
        build_path = self.sources[2]
        issue = self.borrowing_decision(build_rule, "modules", build_path)
        self.record(structure, build_path, self.tip)

        first = self.select_for_family("modules", plan)
        self.assertEqual((first["kind"], first["files"]), ("structure", list(self.sources[:2])))
        self.finish_structure(first, structure)

        build = self.select_for_family("modules", plan)
        findings = [finding.finding_id for finding in self.ledger().proposal_findings(issue)]
        self.assertEqual((build["kind"], build["files"], build["finding_ids"]),
                         ("proposal", [build_path], findings))
        self.merge_selection(build)

        third = self.select_for_family("modules", plan)
        self.assertEqual((third["kind"], third["files"]), ("structure", [self.sources[3]]))

    def test_refill_takes_structure_before_a_queued_build_on_a_structure_turn(self) -> None:
        plan = self.modules_unit()
        build_rule = self.rule("build", "tier: C\nfamily: modules\ninput_scope: file\n")
        self.sync()
        build_path = self.sources[0]
        issue = self.borrowing_decision(build_rule, "modules", build_path, count=3)
        self.finish_judging()
        ledger = self.ledger()
        finished, queued, unclaimed = ledger.proposal_findings(issue)
        owner = ticket_owner_id("modules")
        prior = ledger.next_ticket_id()
        ledger.ticket_add(prior, owner, "proposal", "modules", "crates/alpha", [build_path], [build_rule],
                          proposal=str(issue), finding_ids=(finished.finding_id,))
        ledger.ticket_assign(prior, "demo-style-modules")
        self.merge_selection(ledger.ticket_get(prior))
        waiting = ledger.next_ticket_id()
        ledger.ticket_add(waiting, owner, "proposal", "modules", "crates/alpha", [build_path], [build_rule],
                          proposal=str(issue), finding_ids=(queued.finding_id,))
        structure = self.rule("structure", "tier: D\nfamily: modules\ninput_scope: file\n")
        for path in (build_path, *self.sources[1:3]):
            self.record(structure, path, self.tip)

        ticket = self.select_for_family("modules", plan)

        self.assertEqual((ticket["kind"], ticket["files"]), ("structure", [self.sources[3]]))
        self.assertEqual(ledger.ticket_get(waiting)["state"], "queued")
        self.assertIsInstance(ledger.named_held((unclaimed.finding_id,))[0].build_ticket, style_ledger.NoBuildTicket)

    def test_refill_takes_structure_when_a_queued_build_has_an_uncertain_footprint_at_main(self) -> None:
        plan = self.modules_unit()
        structure = self.rule("structure", "tier: D\nfamily: modules\ninput_scope: file\n")
        build_rule = self.rule("build", "tier: C\nfamily: modules\ninput_scope: file\n")
        self.sync()
        held, ready, build_path, clean = self.sources
        issue = self.borrowing_decision(build_rule, "modules", build_path)
        self.finish_judging()
        for path in (build_path, clean):
            self.record(structure, path, self.tip)
        ledger = self.ledger()
        owner = ticket_owner_id("modules")
        prior = ledger.next_ticket_id()
        ledger.ticket_add(prior, owner, "structure", "modules", "crates/alpha",
                          [held], [structure], cells=((structure, held),))
        ledger.ticket_assign(prior, "demo-style-modules")
        _ = ledger.ticket_base(prior)
        self.record(structure, held, self.tip, prior)
        ledger.ticket_checkpoint(prior, self.tip)
        finding, = ledger.proposal_findings(issue)
        waiting = ledger.next_ticket_id()
        ledger.ticket_add(waiting, owner, "proposal", "modules", "crates/beta", [build_path], [build_rule],
                          proposal=str(issue), finding_ids=(finding.finding_id,))
        uncertain: set[str] = set()
        original_uncertain = style_plan.EditedFiles.uncertain

        def uncertain_at_main(edits: style_plan.EditedFiles, member: style_ledger.HeldFinding) -> tuple[str, ...]:
            if edits.role == style_plan.FootprintRole.MAIN:
                uncertain.add(member.finding_id)
                return (member.path,)
            return original_uncertain(edits, member)

        with (mock.patch.object(style_plan, "main_changes",
                                return_value=style_plan.MainChanges(self.tip, frozenset({"README.md"}))),
              mock.patch.object(style_plan.EditedFiles, "uncertain", new=uncertain_at_main),
              mock.patch.object(style_proposals, "proposal_command")):
            result = style_plan.next_phase("modules", plan, self.repo)

        self.assertEqual(uncertain, {finding.finding_id})
        self.assertIsInstance(result, style_plan.PhaseWritten)
        issued, = (ticket for ticket in ledger.ticket_open(owner) if ticket["state"] == "assigned")
        self.assertEqual((issued["kind"], issued["files"]), ("structure", [ready]))
        self.assertEqual(ledger.ticket_get(waiting)["state"], "queued")

    def test_refill_build_turn_excludes_a_file_with_open_structure(self) -> None:
        self.style_units((StyleUnitAssignment("names", ("names", "modules")),
                          StyleUnitAssignment("types", ("types",)), StyleUnitAssignment("values", ("values",))))
        structure = self.rule("structure", "tier: D\nfamily: modules\ninput_scope: file\n")
        build_rule = self.rule("build", "tier: C\nfamily: modules\ninput_scope: file\n")
        self.sync()
        ready, held = self.sources[:2]
        issue = self.approved_decision(build_rule, (ready, held), "type")
        self.record(structure, ready, self.tip)
        ledger = self.ledger()
        owner = ticket_owner_id("names")
        ledger.ticket_add("4101", owner, "structure", "modules", "crates/alpha",
                          [held], [structure], cells=((structure, held),))
        ledger.ticket_assign("4101", "demo-style-names")
        _ = ledger.ticket_base("4101")
        self.record(structure, held, self.tip, "4101")
        ledger.ticket_checkpoint("4101", self.tip)
        self.assertEqual(next(cell.state for cell in ledger.cells(structure) if cell.path == held), "current")
        checked: set[str] = set()
        original_of = style_plan.EditedFiles.of

        def record_footprint_read(edits: style_plan.EditedFiles, finding: style_ledger.HeldFinding) -> tuple[str, ...]:
            checked.add(finding.path)
            return original_of(edits, finding)

        with (mock.patch.object(style_plan.EditedFiles, "of", new=record_footprint_read),
              mock.patch.object(style_proposals, "proposal_command")):
            result = style_plan.next_phase("names", self.plan, self.repo)

        self.assertIsInstance(result, style_plan.PhaseWritten)
        build, = (ticket for ticket in ledger.ticket_open(owner) if ticket["state"] == "assigned")
        findings = {finding.path: finding for finding in ledger.proposal_findings(issue)}
        self.assertEqual((build["kind"], build["files"], build["finding_ids"]),
                         ("proposal", [ready], [findings[ready].finding_id]))
        self.assertIsInstance(findings[held].build_ticket, style_ledger.NoBuildTicket)
        self.assertEqual(ledger.ticket_get("4101")["state"], "checkpointed")
        self.assertEqual(checked, {ready})

    def approved_findings_blocking_structure(self) -> tuple[Path, Path, str]:
        """Put approved findings on the only file with pending structure work."""
        plan = self.modules_unit()
        _ = self.rule("read")
        self.sync()
        structure = self.pending_structure_on_first_file()
        issue = self.borrowing_decision(structure, "modules", self.sources[0], count=2)
        return plan, issue, structure

    def pending_structure_on_first_file(self) -> str:
        structure = self.rule("structure", "tier: D\nfamily: modules\ninput_scope: file\n")
        for path in self.sources[1:]:
            self.record(structure, path, self.tip)
        return structure

    def ready_modules_build(self) -> tuple[Path, Path]:
        plan = self.modules_unit()
        rule = self.rule("build", "tier: C\nfamily: modules\ninput_scope: file\n")
        self.sync()
        issue = self.borrowing_decision(rule, "modules", self.sources[0])
        return plan, issue

    def test_refill_takes_structure_when_pending_structure_blocks_the_only_build(self) -> None:
        plan, issue, rule = self.approved_findings_blocking_structure()
        ledger = self.ledger()
        prior = ledger.next_ticket_id()
        ledger.ticket_add(prior, ticket_owner_id("modules"), "structure", "modules", "crates/alpha",
                          [self.sources[1]], [rule], cells=((rule, self.sources[1]),))
        ledger.ticket_assign(prior, "demo-style-modules")
        self.finish_structure(ledger.ticket_get(prior), rule)
        ticket = self.select_for_family("modules", plan)
        self.assertEqual((ticket["kind"], ticket["files"], ticket["finding_ids"]),
                         ("structure", [self.sources[0]], []))
        self.assertTrue(all(isinstance(finding.build_ticket, style_ledger.NoBuildTicket)
                            for finding in ledger.proposal_findings(issue)))

    def test_refill_build_fallback_takes_queued_structure_before_fresh_structure(self) -> None:
        plan = self.modules_unit()
        structure = self.rule("structure", "tier: D\nfamily: modules\ninput_scope: file\n")
        build_rule = self.rule("build", "tier: C\nfamily: modules\ninput_scope: file\n")
        self.sync()
        queued_path, fresh_path, prior_path, busy_path = self.sources
        _ = self.borrowing_decision(build_rule, "modules", busy_path)
        self.finish_judging()
        self.record(structure, busy_path, self.tip)
        ledger = self.ledger()
        owner = ticket_owner_id("modules")
        prior = ledger.next_ticket_id()
        ledger.ticket_add(prior, owner, "structure", "modules", "crates/beta",
                          [prior_path], [structure], cells=((structure, prior_path),))
        ledger.ticket_assign(prior, "demo-style-modules")
        self.finish_structure(ledger.ticket_get(prior), structure)
        queued = ledger.next_ticket_id()
        ledger.ticket_add(queued, owner, "structure", "modules", "crates/alpha",
                          [queued_path], [structure], cells=((structure, queued_path),))

        with mock.patch.object(berth_board, "read_board", return_value=self.board(busy_path)):
            first = self.select_for_family("modules", plan)
            self.finish_structure(first, structure)
            second = self.select_for_family("modules", plan)

        self.assertEqual(((first["id"], first["kind"], first["files"]), (second["kind"], second["files"])),
                         ((queued, "structure", [queued_path]), ("structure", [fresh_path])))

    def test_refill_build_fallback_takes_disjoint_structure_while_queued_structure_waits(self) -> None:
        held, queued_path, clean, build_path = self.sources
        fresh = "crates/alpha/src/fresh.rs"
        self.commit_files({fresh: "pub fn fresh() {}\n"})
        plan = self.modules_unit()
        structure = self.rule("structure", "tier: D\nfamily: modules\ninput_scope: file\n")
        build_rule = self.rule("build", "tier: C\nfamily: modules\ninput_scope: file\n")
        self.sync()
        _ = self.borrowing_decision(build_rule, "modules", build_path)
        self.finish_judging()
        for path in (clean, build_path):
            self.record(structure, path, self.tip)
        # The first refill must have no disjoint read candidate.
        self.record(build_rule, clean, self.tip)

        ledger = self.ledger()
        owner = ticket_owner_id("modules")
        checkpoint = ledger.next_ticket_id()
        ledger.ticket_add(checkpoint, owner, "structure", "modules", "crates/alpha",
                          [held], [structure], cells=((structure, held),))
        ledger.ticket_assign(checkpoint, "demo-style-modules")
        _ = ledger.ticket_base(checkpoint)
        self.record(structure, held, self.tip, checkpoint)
        ledger.ticket_checkpoint(checkpoint, self.tip)
        queued = ledger.next_ticket_id()
        ledger.ticket_add(queued, owner, "structure", "modules", "crates/alpha",
                          [held, queued_path], [structure], cells=((structure, held), (structure, queued_path)))

        with (mock.patch.object(style_proposals, "proposal_command"),
              mock.patch.object(berth_board, "read_board", return_value=self.board(build_path, fresh))):
            waiting = style_plan.next_phase("modules", plan, self.repo)
        self.assertEqual(waiting, style_plan.WaitingForMerge(checkpoint, self.tip, f"overlap: {held}"))

        with (mock.patch.object(style_proposals, "proposal_command"),
              mock.patch.object(berth_board, "read_board", return_value=self.board(build_path))):
            result = style_plan.next_phase("modules", plan, self.repo)
        self.assertIsInstance(result, style_plan.PhaseWritten)
        issued, = (ticket for ticket in ledger.ticket_open(owner) if ticket["state"] == "assigned")
        self.assertEqual((issued["kind"], issued["files"]), ("structure", [fresh]))
        self.assertEqual(ledger.ticket_get(queued)["state"], "queued")

    def build_turn_with_coverage_and_checkpoint(self) -> tuple[Path, str]:
        plan = self.modules_unit()
        structure = self.rule("structure", "tier: D\nfamily: modules\ninput_scope: file\n")
        read_rule = self.rule("read", "tier: C\nfamily: modules\ninput_scope: file\n")
        self.sync()
        held, _, _, build_path = self.sources
        _ = self.borrowing_decision(read_rule, "modules", build_path)
        self.finish_judging()
        for path in self.sources[1:]:
            self.record(structure, path, self.tip)
        self.record(read_rule, held, self.tip)
        ledger = self.ledger()
        checkpoint = ledger.next_ticket_id()
        ledger.ticket_add(checkpoint, ticket_owner_id("modules"), "structure", "modules", "crates/alpha",
                          [held], [structure], cells=((structure, held),))
        ledger.ticket_assign(checkpoint, "demo-style-modules")
        _ = ledger.ticket_base(checkpoint)
        self.record(structure, held, self.tip, checkpoint)
        ledger.ticket_checkpoint(checkpoint, self.tip)
        _ = self.enterContext(mock.patch.object(berth_board, "read_board", return_value=self.board(build_path)))
        _ = self.enterContext(mock.patch.object(style_proposals, "proposal_command"))
        return plan, read_rule

    def test_refill_build_fallback_checks_coverage_after_fresh_structure_waits(self) -> None:
        plan, _ = self.build_turn_with_coverage_and_checkpoint()
        manifest = self.rule("manifest", "tier: D\nfamily: modules\ninput_scope: manifest\n")
        self.record(manifest, "crates/beta", self.tip)
        ledger = self.ledger()
        self.assertFalse(ledger.queued_tickets(ticket_owner_id("modules")))

        result = style_plan.next_phase("modules", plan, self.repo)

        self.assertIsInstance(result, style_plan.PhaseWritten)
        issued, = (ticket for ticket in ledger.ticket_open(ticket_owner_id("modules")) if ticket["state"] == "assigned")
        self.assertEqual((issued["kind"], issued["files"]), ("read", [self.sources[2]]))

    def test_refill_build_fallback_checks_later_queued_work_after_a_merge_wait(self) -> None:
        self.style_units(tuple(replace(row, open_tickets=2) if row.unit == "modules" else row
                               for row in DEFAULT_STYLE_ASSIGNMENTS))
        plan, read_rule = self.build_turn_with_coverage_and_checkpoint()
        ledger = self.ledger()
        owner = ticket_owner_id("modules")
        waiting = ledger.next_ticket_id()
        ledger.ticket_add(waiting, owner, "read", "modules", "crates/alpha", [self.sources[0]], [read_rule])
        ready = ledger.next_ticket_id()
        ledger.ticket_add(ready, owner, "read", "modules", "crates/beta", [self.sources[2]], [read_rule])

        result = style_plan.next_phase("modules", plan, self.repo)

        self.assertEqual(result, style_plan.PhasesReady(written=(ready,), resumed=()))
        self.assertEqual(ledger.ticket_get(waiting)["state"], "queued")

    def test_refill_build_fallback_checks_later_coverage_after_a_sync_wait(self) -> None:
        plan, _ = self.build_turn_with_coverage_and_checkpoint()
        changes = style_plan.MainChanges(self.tip, frozenset({self.sources[1]}))
        with mock.patch.object(style_plan, "main_changes", return_value=changes):
            result = style_plan.next_phase("modules", plan, self.repo)

        self.assertIsInstance(result, style_plan.PhaseWritten)
        issued, = (ticket for ticket in self.ledger().ticket_open(ticket_owner_id("modules"))
                   if ticket["state"] == "assigned")
        self.assertEqual((issued["kind"], issued["files"]), ("read", [self.sources[2]]))

    def test_refill_waits_for_structure_at_a_followed_findings_new_file(self) -> None:
        structure = self.rule("structure", "tier: D\nfamily: modules\ninput_scope: file\n")
        build_rule = self.rule("build", "tier: C\nfamily: modules\ninput_scope: file\n")
        old, current = self.sources[0], "crates/alpha/src/current.rs"
        self.commit_files({old: "pub struct Parent {\n    old: u32,\n}\n"})
        plan = self.modules_unit()
        issue, (finding,) = self.judged_findings(build_rule, ((old, 2, "old: u32,"),))
        _ = issue.write_text(issue.read_text().replace(f"unit: {unit_staff_id('names')}", f"unit: {unit_staff_id('modules')}"))
        self.write_generated_body(issue)
        self.finish_judging()
        _ = self.git("mv", old, current)
        self.commit_to_every_checkout()
        _ = self.git("merge", "--ff-only", "main", cwd=plan.parents[3])
        for path in self.sources[1:]:
            self.record(structure, path, self.tip)

        # Following refreshes the generated decision, including its rule link. Use the same
        # rule-origin fixture as the declaration-follow tests so refresh is not skipped.
        _ = self.enterContext(mock.patch.object(style_proposals, "github_rule_root",
                              return_value=style_proposals.GitHubRuleRoot("example/nate_style", "main")))
        first = self.select_for_family("modules", plan)

        followed, = self.ledger().proposal_findings(issue)
        self.assertEqual((followed.finding_id, followed.path), (finding.finding_id, current))
        self.assertIsInstance(followed.build_ticket, style_ledger.NoBuildTicket)
        self.assertEqual((first["kind"], first["files"]), ("structure", [current]))
        self.finish_structure(first, structure)
        ticket = self.select_for_family("modules", plan)
        self.assertEqual((ticket["kind"], ticket["files"], ticket["finding_ids"]),
                         ("proposal", [current], [finding.finding_id]))

    def test_refill_selects_structure_when_its_file_no_longer_has_approved_findings(self) -> None:
        plan, issue = self.ready_modules_build()
        first = self.select_for_family("modules", plan)
        self.assertEqual(first["kind"], "proposal")
        self.ledger().ticket_drop(first["id"])
        _ = issue.write_text(re.sub(r"unit: [^\n]*\n", "", issue.read_text()))
        _ = self.pending_structure_on_first_file()
        ticket = self.select_for_family("modules", plan)
        self.assertEqual((ticket["kind"], ticket["files"]), ("structure", [self.sources[0]]))
        self.assertTrue(all(isinstance(finding.build_ticket, style_ledger.NoBuildTicket)
                            for finding in self.ledger().proposal_findings(issue)))

    def test_refill_selects_structure_after_the_approved_findings_ticket_merges(self) -> None:
        plan, _ = self.ready_modules_build()
        first = self.select_for_family("modules", plan)
        self.assertEqual(first["kind"], "proposal")
        ledger = self.ledger()
        ledger.ticket_checkpoint(first["id"], self.tip)
        _ = self.pending_structure_on_first_file()
        with mock.patch.object(berth_board, "read_board", return_value=self.board(*self.sources[1:])):
            with mock.patch.object(style_proposals, "proposal_command"):
                waiting = style_plan.next_phase("modules", plan, self.repo)
        self.assertIsInstance(waiting, style_plan.WaitingForMerge)
        _ = ledger.ticket_merge(first["id"])
        ticket = self.select_for_family("modules", plan)
        self.assertEqual((ticket["kind"], ticket["files"]), ("structure", [self.sources[0]]))

    def test_refill_resumes_the_same_approved_ticket_without_creating_structure_work(self) -> None:
        plan, _ = self.ready_modules_build()
        first = self.select_for_family("modules", plan)
        self.assertEqual(first["kind"], "proposal")
        _ = self.pending_structure_on_first_file()
        before = plan.read_bytes(), self.count_tickets(), first
        second = self.select_for_family("modules", plan)
        self.assertEqual((plan.read_bytes(), self.count_tickets(), second), before)

    def test_queued_structure_holds_approved_findings_on_its_file(self) -> None:
        plan, issue, structure = self.approved_findings_blocking_structure()
        ledger = self.ledger()
        findings = tuple(finding.finding_id for finding in ledger.proposal_findings(issue))
        ledger.ticket_add("3000", ticket_owner_id("modules"), "proposal", "modules", "crates/alpha",
                          [self.sources[0]], [structure], proposal=str(issue), finding_ids=findings)
        ledger.ticket_add("3001", ticket_owner_id("modules"), "structure", "modules", "crates/alpha",
                          [self.sources[0]], [structure])
        ticket = self.select_for_family("modules", plan)
        self.assertEqual((ticket["id"], ticket["kind"], ticket["files"]), ("3001", "structure", [self.sources[0]]))
        self.assertEqual(self.count_tickets(), 3)
        queued, = ledger.queued_tickets(ticket_owner_id("modules"))
        self.assertEqual((queued["id"], set(queued["finding_ids"])), ("3000", set(findings)))
        before = self.count_tickets()
        self.assertEqual(self.select_for_family("modules", plan), ticket)
        self.assertEqual(self.count_tickets(), before)

    def test_modules_reads_blocking_structure_before_approved_decisions(self) -> None:
        plan = self.modules_unit()
        structure = self.rule("structure", "tier: D\nfamily: modules\ninput_scope: file\n")
        _ = self.values_unit()
        _ = self.rule("values", "tier: C\nfamily: values\ninput_scope: file\n")
        self.sync()
        issue = self.borrowing_decision(structure, "modules", self.sources[2])

        ticket = self.select_for_family("modules", plan)

        self.assertEqual((ticket["kind"], ticket["family"], ticket["files"]),
                         ("structure", "modules", list(self.sources[:2])))
        self.assertEqual(ticket["finding_ids"], [])
        self.assertIsInstance(self.ledger().proposal_findings(issue)[0].build_ticket, style_ledger.NoBuildTicket)

    def test_modules_reads_blocking_structure_despite_unsynced_source_changes(self) -> None:
        plan = self.modules_unit()
        structure = self.rule("structure", "tier: D\nfamily: modules\ninput_scope: file\n")
        _ = self.values_unit()
        values = self.rule("values", "tier: C\nfamily: values\ninput_scope: file\n")
        self.sync()
        issue = self.borrowing_decision(structure, "modules", self.sources[2])
        for path in (self.sources[1], self.sources[3]):
            self.record(structure, path, self.tip)
        for path in self.sources[1:]:
            self.record(values, path, self.tip)

        with (mock.patch.object(style_plan, "unsynced_changes", return_value={
                self.sources[0]: style_plan.UnsyncedChange(self.sources[0], self.tip)}),
              mock.patch.object(style_plan, "main_changes", return_value=style_plan.MainChanges(
                  self.tip, frozenset({self.sources[0]})))):
            ticket = self.select_for_family("modules", plan)

        self.assertEqual((ticket["kind"], ticket["family"], ticket["files"]),
                         ("structure", "modules", [self.sources[0]]))
        self.assertEqual(ticket["finding_ids"], [])
        self.assertIsInstance(self.ledger().proposal_findings(issue)[0].build_ticket, style_ledger.NoBuildTicket)

    def test_modules_skips_held_structure_files_and_manifest(self) -> None:
        plan = self.modules_unit()
        structure = self.rule("structure", "tier: D\nfamily: modules\ninput_scope: file\n")
        _ = self.rule("manifest", "tier: D\nfamily: modules\ninput_scope: manifest\n")
        _ = self.values_unit()
        _ = self.rule("values", "tier: C\nfamily: values\ninput_scope: file\n")
        self.sync()
        ledger = self.ledger()
        ledger.ticket_add("12001", ticket_owner_id("values"), "read", "modules", "crates/alpha",
                          [self.sources[0]], [structure])
        ledger.ticket_assign("12001", "style/main")
        branch = self.git("rev-parse", "demo-style-modules")
        # Board reservations and the other ticket's held paths both exclude source reads.
        board = self.board("crates/alpha/Cargo.toml", *self.sources[:2])
        busy = style_plan.busy_files(self.repo, self.doc, ledger, ticket_owner_id("modules"), board)
        self.assertIn(self.sources[0], busy.paths)
        self.assertTrue(busy.contains("crates/alpha/Cargo.toml"))
        with mock.patch.object(berth_board, "read_board", return_value=board):
            ticket = self.select_for_family("modules", plan)
        self.assertEqual((ticket["kind"], ticket["files"]),
                         ("structure", ["crates/beta/Cargo.toml", *self.sources[2:]]))
        self.assertEqual(self.git("rev-parse", "demo-style-modules"), branch)
        self.assertEqual(ledger.ticket_open(ticket_owner_id("values"))[0]["state"], "assigned")

    def test_owned_structure_skips_product_reservations_and_refill_is_idempotent(self) -> None:
        structure = self.rule("structure", "tier: D\nfamily: modules\ninput_scope: file\n")
        _ = self.rule("read")
        self.sync()
        held, available = self.sources[:2]
        _ = self.plan.write_text(self.plan_header().replace("## Phases", "**Pilot scope:** alpha\n\n## Phases"))
        self.style_units((StyleUnitAssignment("names", ("names", "modules")),
                          StyleUnitAssignment("values", ("values",)), StyleUnitAssignment("types", ("types",))))
        # Owned families use the same busy files before selection.
        candidates = self.candidates("modules", blocked=(held,))
        self.assertEqual({path for ticket in candidates for path in ticket.files}, set(self.sources[1:]))
        ledger = self.ledger()
        # An old queued structure ticket must pass the same availability check.
        ledger.ticket_add("1", ticket_owner_id("names"), "structure", "modules", "crates/alpha", [held], [structure])
        with mock.patch.object(berth_board, "read_board", return_value=self.board(held)):
            code, output, errors = self.run_next()
            self.assertEqual((code, errors), (0, ""), output)
            ticket, = ledger.ticket_open(ticket_owner_id("names"))
            self.assertEqual((ticket["kind"], ticket["family"], ticket["files"]),
                             ("structure", "modules", [available]))
            self.assertIn("dropped 1: files busy", output)
            before = self.plan.read_bytes(), ledger.ticket_open(ticket_owner_id("names")), self.count_tickets()
            code, output, errors = self.run_next()
        self.assertEqual((code, errors), (0, ""), output)
        self.assertEqual((self.plan.read_bytes(), ledger.ticket_open(ticket_owner_id("names")), self.count_tickets()), before)

    def test_structure_selection_omits_a_deleted_file_from_an_older_cell_snapshot(self) -> None:
        structure = self.rule("structure", "tier: D\nfamily: modules\ninput_scope: file\n")
        self.sync()
        ledger = self.ledger()
        cells = tuple(ledger.cells(structure))
        deleted = self.sources[0]
        _ = self.git("rm", deleted)
        tip = self.commit()
        ledger.set_tip(tip, "style/main")
        # The old snapshot still names the deleted file; eligibility and size use the current tip.
        with mock.patch.object(ledger, "cells", return_value=cells):
            tickets = style_plan.candidate_tickets(self.repo, ledger, style_plan.BusyFiles(self.board(), frozenset()),
                                                   "modules", base=style_plan.NoUnitBranch())
        self.assertEqual({path for ticket in tickets for path in ticket.files}, set(self.sources[1:]))

    def test_refill_checks_current_file_presence_even_after_a_declaration_read(self) -> None:
        plan = self.modules_unit()
        read = self.rule("read", "tier: D\nfamily: modules\ninput_scope: file\n")
        self.sync()
        issue = self.borrowing_decision(read, "modules", self.sources[0])
        self.finish_judging()
        finding, = self.ledger().proposal_findings(issue)
        _ = self.git("rm", finding.path)
        self.commit_to_every_checkout()
        _ = self.git("merge", "-q", "--ff-only", "style/main", cwd=self.root / "modules")
        for path in self.sources[1:]:
            self.record(read, path, self.tip)
        # A declaration reading cannot make a missing file available.
        follow = style_proposals.FollowRun(style_ledger.DeclarationReading(
            self.tip, (style_ledger.DeclarationInPlace(finding),)), (), ())
        with mock.patch.object(style_proposals, "run_follow", return_value=follow):
            result = style_plan.next_phase("modules", plan, self.repo)
        self.assertIsInstance(result, style_plan.WaitingForSync)
        self.assertIsInstance(self.ledger().proposal_findings(issue)[0].state, style_ledger.Waived)
        self.assertEqual(self.ledger().ticket_open(ticket_owner_id("modules")), ())

    def test_modules_selects_tier_d_before_an_unrelated_approved_decision(self) -> None:
        plan = self.modules_unit()
        structure = self.rule("structure", "tier: D\nfamily: modules\ninput_scope: file\n")
        self.sync()
        issue = self.borrowing_decision(structure, "modules", self.sources[2])

        ticket = self.select_for_family("modules", plan)

        self.assertEqual((ticket["kind"], ticket["family"], ticket["files"]),
                         ("structure", "modules", list(self.sources[:2])))
        self.assertIsInstance(self.ledger().proposal_findings(issue)[0].build_ticket, style_ledger.NoBuildTicket)

    def test_values_waits_for_structure_owned_by_another_unit(self) -> None:
        plan = self.values_unit()
        _ = self.rule("values", "tier: C\nfamily: values\ninput_scope: file\n")
        _ = self.rule("structure", "tier: D\nfamily: modules\ninput_scope: file\n")
        self.sync()

        result = style_plan.next_phase("values", plan, self.repo)

        self.assertIsInstance(result, style_plan.WaitingForStructure)
        self.assertEqual(self.ledger().ticket_open(ticket_owner_id("values")), ())

    def test_one_unit_selects_tier_d_then_families_by_rank(self) -> None:
        families: tuple[rules.Family, ...] = ("names", "values", "types", "modules")
        self.style_units((StyleUnitAssignment("names", families),))
        owned = {family: self.rule("owned-" + family,
                 f"tier: {'D' if family == 'modules' else 'C'}\nfamily: {family}\ninput_scope: file\n")
                 for family in families}
        self.sync()
        for family, path in zip(families, self.sources, strict=True):
            for other in self.sources:
                if other != path:
                    self.record(owned[family], other, self.tip)

        for family in ("modules", *rules.FAMILY_RANKS):
            with self.subTest(family=family):
                ticket = self.select_for_family("names", self.plan)
                self.assertEqual((ticket["family"], ticket["rules"]), (family, [owned[family]]))
                self.ledger().ticket_drop(ticket["id"])
                for path in self.sources:
                    self.record(owned[family], path, self.tip)

    def test_one_and_four_units_keep_each_tickets_rule_family(self) -> None:
        _ = self.modules_unit()
        _ = self.types_unit()
        _ = self.values_unit()
        families: tuple[rules.Family, ...] = ("names", "values", "types", "modules")
        owned = {family: self.rule("partition-" + family,
                 f"tier: {'D' if family == 'modules' else 'C'}\nfamily: {family}\ninput_scope: file\n")
                 for family in families}
        self.sync()
        for family, path in zip(families, self.sources, strict=True):
            for other in self.sources:
                if other != path:
                    self.record(owned[family], other, self.tip)
        partitions = (tuple(StyleUnitAssignment(family, (family,)) for family in families),
                      (StyleUnitAssignment("names", families),))
        selections: list[set[tuple[str, str]]] = []
        for assignments in partitions:
            self.style_units(assignments)
            ledger = self.ledger()
            scopes = unit_plans.family_pilot_scopes(self.doc, self.repo)
            definitions = {rule.rule_id: rule for rule in rules.load_rules(self.repo)}
            tickets = tuple(ticket for assignment in assignments for ticket in style_plan.ranked_candidates(
                self.repo, ledger, style_plan.BusyFiles(self.board(), frozenset()), assignment.families,
                unit_plans.AllCrates(), scopes, self.base(), frozenset(), definitions, tuple(ledger.cells())))
            selections.append({(rule, ticket.family) for ticket in tickets for rule in ticket.rules})
        self.assertEqual(selections[0], selections[1])
        self.assertEqual(selections[0], {(rule, family) for family, rule in owned.items()})

    def test_combined_unit_keeps_another_owned_familys_queued_ticket(self) -> None:
        self.style_units((StyleUnitAssignment("names", ("names", "types")),
                          StyleUnitAssignment("values", ("values",)), StyleUnitAssignment("modules", ("modules",))))
        names = self.rule("queued-names")
        types = self.rule("queued-types", "tier: C\nfamily: types\ninput_scope: file\n")
        self.sync()
        ledger = self.ledger()
        ledger.ticket_add("4001", ticket_owner_id("names"), "read", "names", "crates/alpha", [self.sources[0]], [names])
        ledger.ticket_add("4002", ticket_owner_id("names"), "read", "types", "crates/beta", [self.sources[2]], [types])
        for path in self.sources:
            if path != self.sources[2]:
                self.record(types, path, self.tip)

        ticket = self.select_for_family("names", self.plan)

        self.assertEqual((ticket["id"], ticket["family"]), ("4002", "types"))
        self.assertEqual([item["id"] for item in ledger.queued_tickets(ticket_owner_id("names"))], ["4001"])

    def test_unit_does_not_select_another_owners_approved_findings(self) -> None:
        plan = self.values_unit()
        values = self.rule("foreign-values", "tier: C\nfamily: values\ninput_scope: file\n")
        self.sync()
        issue = self.borrowing_decision(values, "values", self.sources[0])

        result = style_plan.next_phase("names", self.plan, self.repo)

        self.assertIsInstance(result, style_plan.WaitingForSync)
        self.assertEqual(self.ledger().ticket_open(ticket_owner_id("names")), ())
        self.assertIsInstance(self.ledger().proposal_findings(issue)[0].build_ticket, style_ledger.NoBuildTicket)
        self.assertTrue(plan.exists())

    def test_combined_unit_filters_approved_findings_by_their_rule_family(self) -> None:
        self.style_units((StyleUnitAssignment("names", ("names", "types")),
                          StyleUnitAssignment("values", ("values",)), StyleUnitAssignment("modules", ("modules",))))
        names = self.rule("approved-names")
        types = self.rule("approved-types", "tier: C\nfamily: types\ninput_scope: file\n")
        self.sync()
        names_issue = self.approved_decision(names, self.sources[:1], "type", name="names")
        types_issue = self.approved_decision(types, self.sources[2:3], "type", name="types")
        for rule in (names, types):
            for path in self.sources:
                self.record(rule, path, self.tip)

        ticket = self.select_for_family("names", self.plan)

        self.assertEqual((ticket["kind"], ticket["family"], ticket["rules"]), ("proposal", "types", [types]))
        self.assertEqual(ticket["finding_ids"], [self.ledger().proposal_findings(types_issue)[0].finding_id])
        self.assertIsInstance(self.ledger().proposal_findings(names_issue)[0].build_ticket, style_ledger.NoBuildTicket)

    def test_combined_unit_ranks_coverage_before_a_lower_family_decision(self) -> None:
        self.style_units((StyleUnitAssignment("names", ("names", "types")),
                          StyleUnitAssignment("values", ("values",)), StyleUnitAssignment("modules", ("modules",))))
        names = self.rule("approved-names")
        types = self.rule("pending-types", "tier: C\nfamily: types\ninput_scope: file\n")
        self.sync()
        issue = self.approved_decision(names, self.sources[:1], "type")
        for path in self.sources:
            self.record(names, path, self.tip)
            if path != self.sources[2]:
                self.record(types, path, self.tip)

        ticket = self.select_for_family("names", self.plan)

        self.assertEqual((ticket["kind"], ticket["family"], ticket["rules"]), ("read", "types", [types]))
        self.assertIsInstance(self.ledger().proposal_findings(issue)[0].build_ticket, style_ledger.NoBuildTicket)

    def test_combined_unit_ranks_structure_before_another_familys_queue(self) -> None:
        self.style_units((StyleUnitAssignment("names", ("names", "modules")),
                          StyleUnitAssignment("values", ("values",)), StyleUnitAssignment("types", ("types",))))
        names = self.rule("queued-names")
        structure = self.rule("pending-structure", "tier: D\nfamily: modules\ninput_scope: file\n")
        self.sync()
        ledger = self.ledger()
        ledger.ticket_add("4001", ticket_owner_id("names"), "read", "names", "crates/alpha", [self.sources[0]], [names])

        ticket = self.select_for_family("names", self.plan)

        self.assertEqual((ticket["kind"], ticket["family"], ticket["rules"]), ("structure", "modules", [structure]))
        self.assertEqual([item["id"] for item in ledger.queued_tickets(ticket_owner_id("names"))], ["4001"])

    def test_combined_unit_selects_an_approved_tier_d_finding_before_types_coverage(self) -> None:
        self.style_units((StyleUnitAssignment("names", ("names", "types", "values", "modules")),))
        structure = self.rule("approved-structure", "tier: D\nfamily: modules\ninput_scope: file\n")
        types = self.rule("pending-types", "tier: C\nfamily: types\ninput_scope: file\n")
        self.sync()
        issue = self.approved_decision(structure, self.sources[:1], "type")
        for path in self.sources:
            self.record(structure, path, self.tip)
            if path != self.sources[2]:
                self.record(types, path, self.tip)

        for queued, busy, family in ((False, False, "modules"), (True, False, "modules"), (True, True, "types")):
            with self.subTest(queued=queued, busy=busy):
                ledger = self.ledger()
                if queued and not busy:
                    ledger.ticket_add("4001", ticket_owner_id("names"), "read", "types", "crates/beta",
                                      [self.sources[2]], [types])
                board = self.board(self.sources[0]) if busy else self.board()
                with mock.patch.object(berth_board, "read_board", return_value=board):
                    ticket = self.select_for_family("names", self.plan)
                self.assertEqual((ticket["kind"], ticket["family"], ticket["rules"]),
                                 ("read", "types", [types]) if busy else ("proposal", family, [structure]))
                if not busy:
                    self.assertEqual(ticket["finding_ids"], [ledger.proposal_findings(issue)[0].finding_id])
                if queued and not busy:
                    self.assertEqual([row["id"] for row in ledger.queued_tickets(ticket_owner_id("names"))], ["4001"])
                ledger.ticket_drop(ticket["id"])

    def test_refill_blob_failures_do_not_reissue_held_cells(self) -> None:
        read = self.rule("read", "tier: C\nfamily: names\ninput_scope: crate\n")
        self.sync()
        _ = self.held_proposal(read, self.sources[0])
        with self.ledger() as ledger:
            cell = style_ledger.Cell(read, self.sources[0], "crates/alpha", "C", "names", "stale")
            definitions = {rule.rule_id: rule for rule in rules.load_rules(self.repo)}
            # Exercise the fallback before any later ticket-selection Git reads can fail.
            for code in (errno.EMFILE, errno.ENOENT):
                with self.subTest(errno=code):
                    failure = OSError(code, "PRIVATE launch failure")
                    with (mock.patch.object(style_plan, "_refill_cells", wraps=style_plan._refill_cells) as refill,  # pyright: ignore[reportPrivateUsage]
                          mock.patch.object(subprocess, "run", side_effect=failure)):
                        with self.assertRaises(style_ledger.GitReadFailed) as refused:
                            _ = style_plan._refill_cells(ledger, (cell,), style_plan.NoUnitBranch(),  # pyright: ignore[reportPrivateUsage]
                                                         "names", AllCrates(), {}, definitions)
                    refill.assert_called_once()
                    self.assertIs(refused.exception.__cause__, failure)
            # An actually removed file cannot prove that this held cell is unchanged.
            _ = self.git("rm", self.sources[0])
            removed_tip = self.commit()
            ledger.set_tip(removed_tip, "style/main")
            result = style_plan._refill_cells(ledger, (cell,), style_plan.NoUnitBranch(),  # pyright: ignore[reportPrivateUsage]
                                              "names", AllCrates(), {}, definitions)
            self.assertEqual(result.cells, (cell,))
            self.assertEqual(result.judged_at_base, frozenset())

    def test_reserved_unmerged_dirty_and_other_open_ticket_files_are_excluded(self) -> None:
        rule = self.rule("read")
        self.sync()
        product = self.root / "product"
        _ = self.git("worktree", "add", "-q", "-b", "product-change", str(product))
        _ = self.git("mv", self.sources[1], "crates/alpha/src/renamed.rs", cwd=product)
        _ = self.git("add", "--all", cwd=product)
        _ = self.git("commit", "-qm", "Product", cwd=product)
        _ = (product / self.sources[2]).write_text("dirty\n")
        _ = fixture_unit_location(recorded_showrunner("sample", self.repo / "docs/product-production.md"), "product", product)
        _ = self.modules_unit()
        ledger = self.ledger()
        ledger.ticket_add("3001", ticket_owner_id("modules"), "read", "names", "crates/beta", [self.sources[3]], [rule])
        ledger.ticket_assign("3001", "demo-style-modules")
        self.assertEqual(self.candidates(blocked=(self.sources[0],)), ())
        ledger.ticket_drop("3001")
        self.assertEqual(tuple(path for ticket in self.candidates(blocked=(self.sources[0],)) for path in ticket.files), (self.sources[3],))

    def test_held_files_follow_staff_when_the_worktree_cell_names_another_checkout(self) -> None:
        stale = self.root / "stale-names"
        _ = self.git("worktree", "add", "-q", "-b", "stale-names", str(stale))
        staff_file, stale_file = self.sources[:2]
        _ = (self.unit / staff_file).write_text("staff location change\n")
        _ = (stale / stale_file).write_text("stale cell change\n")
        _ = self.doc.write_text(self.doc.read_text().replace(str(self.unit), str(stale)))

        holders = unit_files.unit_file_holders(self.repo, self.doc, self.ledger())

        self.assertEqual(holders["names"], frozenset({staff_file}))

    def test_slices_preserve_directories_and_split_oversized_directories_with_bounds(self) -> None:
        files: list[str] = []
        for directory, count in (("one", 12), ("two", 12), ("large", 30)):
            for index in range(count):
                path = f"crates/alpha/src/{directory}/file{index:02}.rs"
                self.write(path, "// source\n" * 200)
                files.append(path)
        tip = self.commit()
        slices = style_plan.read_slices(self.repo, tip, tuple(reversed(files)))
        self.assertEqual(slices, style_plan.read_slices(self.repo, tip, tuple(files)))
        self.assertEqual({path for group in slices for path in group}, set(files))
        for group in slices:
            self.assertGreaterEqual(len(group), 10)
            self.assertLessEqual(len(group), 25)
            self.assertLessEqual(len(group) * 200, 5000)
        for directory in ("one", "two"):
            self.assertEqual(sum(any(f"/{directory}/" in path for path in group) for group in slices), 1)

    def test_refill_skips_unchanged_held_read_cells_under_open_proposals_after_other_crate_edits(self) -> None:
        read = self.rule("read", "tier: C\nfamily: names\ninput_scope: crate\n")
        self.sync()
        _ = self.held_proposal(read, self.sources[0])
        self.write(self.sources[1], "pub fn changed() {}\n")
        self.tip = self.commit()
        self.sync()
        self.unit_takes_tip()
        for path in self.sources[1:]:
            self.record(read, path, self.tip)

        self.assertEqual(next(cell.state for cell in self.ledger().cells(read) if cell.path == self.sources[0]), "stale")
        self.assertEqual(self.candidates(), ())
        self.assertEqual(self.run_next(), (3, "waiting for sync\n", ""))
        self.assertEqual(self.count_tickets(), 0)

    def test_open_proposal_structure_cells_neither_reissue_nor_block_other_families_when_the_file_is_unchanged(self) -> None:
        structure = self.rule("structure", "tier: D\nfamily: modules\n")
        read = self.rule("read")
        self.sync()
        _ = self.held_proposal(structure, self.sources[0])
        self.write(self.sources[1], "pub fn changed() {}\n")
        self.tip = self.commit()
        self.sync()
        self.unit_takes_tip()
        for path in self.sources[1:]:
            self.record(structure, path, self.tip)
            self.record(read, path, self.tip)
        self.assertEqual(next(cell.state for cell in self.ledger().cells(structure) if cell.path == self.sources[0]), "stale")
        self.assertEqual(self.candidates("modules"), ())
        self.assertEqual(self.candidates(), (
            style_plan.TicketSelection("read", "names", "crates/alpha", (self.sources[0],), (read,)),
        ))
        with mock.patch.object(berth_board, "read_board", return_value=self.board(self.sources[0])):
            self.assertEqual(self.run_next(), (3, "waiting for sync\n", ""))
        self.assertEqual(self.run_next(), (0, "phase written: 1\n", ""))
        self.assertEqual(self.ledger().ticket_open(ticket_owner_id("names"))[0]["files"], [self.sources[0]])

    def test_refill_reissues_a_held_cell_under_an_open_proposal_when_its_own_file_changes(self) -> None:
        read = self.rule("read", "tier: C\nfamily: names\ninput_scope: crate\n")
        self.sync()
        _ = self.held_proposal(read, self.sources[0])
        self.write(self.sources[0], "pub fn changed() {}\n")
        self.tip = self.commit()
        self.sync()
        self.unit_takes_tip()
        for path in self.sources[1:]:
            self.record(read, path, self.tip)

        self.assertEqual(self.candidates(), (
            style_plan.TicketSelection("read", "names", "crates/alpha", (self.sources[0],), (read,)),
        ))

    def test_refill_reissues_an_unchanged_held_cell_as_soon_as_its_proposal_is_not_open(self) -> None:
        read = self.rule("read", "tier: C\nfamily: names\ninput_scope: crate\n")
        self.sync()
        issue = self.held_proposal(read, self.sources[0])
        self.write(self.sources[1], "pub fn changed() {}\n")
        self.tip = self.commit()
        self.sync()
        self.unit_takes_tip()
        for path in self.sources[1:]:
            self.record(read, path, self.tip)
        self.assertEqual(self.candidates(), ())

        for status in ("closed", "approved", "declined"):
            with self.subTest(status=status):
                _ = issue.write_text(f"---\nstatus: {status}\n---\n")
                self.assertEqual(self.candidates(), (
                    style_plan.TicketSelection("read", "names", "crates/alpha", (self.sources[0],), (read,)),
                ))

    def test_refill_still_issues_stale_cells_that_were_never_held(self) -> None:
        read = self.rule("read", "tier: C\nfamily: names\ninput_scope: crate\n")
        self.sync()
        self.record(read, self.sources[0], self.tip)
        self.write(self.sources[1], "pub fn changed() {}\n")
        self.tip = self.commit()
        self.sync()
        self.unit_takes_tip()
        for path in self.sources[1:]:
            self.record(read, path, self.tip)

        self.assertEqual(self.candidates(), (
            style_plan.TicketSelection("read", "names", "crates/alpha", (self.sources[0],), (read,)),
        ))

    def test_refill_requires_every_finding_on_a_held_cell_to_have_an_open_proposal(self) -> None:
        read = self.rule("read", "tier: C\nfamily: names\ninput_scope: crate\n")
        self.sync()
        _ = self.held_proposal(read, self.sources[0])
        self.write(self.sources[1], "pub fn changed() {}\n")
        self.tip = self.commit()
        self.sync()
        self.unit_takes_tip()
        for path in self.sources[1:]:
            self.record(read, path, self.tip)
        self.assertEqual(self.candidates(), ())

        ledger = self.ledger()
        blob = next(item.blob for item in rules.load_rules(self.repo) if item.rule_id == read)
        finding = ledger.hold(read, self.sources[0], 1, "another defect", "rename", "before", "after", blob, "outside")
        expected = (style_plan.TicketSelection("read", "names", "crates/alpha", (self.sources[0],), (read,)),)
        self.assertEqual(self.candidates(), expected)
        second = self.root / "second-proposal.md"
        _ = second.write_text("---\nstatus: open\n---\n")
        ledger.file_held(finding.finding_id, second)
        self.assertEqual(self.candidates(), ())
        _ = second.write_text("---\nstatus: closed\n---\n")
        self.assertEqual(self.candidates(), expected)

    def test_refill_uses_the_latest_verdict_instead_of_an_older_held_judgment(self) -> None:
        read = self.rule("read", "tier: C\nfamily: names\ninput_scope: crate\n")
        self.sync()
        _ = self.held_proposal(read, self.sources[0])
        self.commit_files({self.sources[0]: (self.repo / self.sources[0]).read_text() + "\npub struct JudgedAgain;\n"})
        self.record(read, self.sources[0], self.tip, ticket="later-clean")
        self.write(self.sources[1], "pub fn changed() {}\n")
        self.tip = self.commit()
        self.sync()
        self.unit_takes_tip()
        for path in self.sources[1:]:
            self.record(read, path, self.tip)

        self.assertEqual(self.candidates(), (
            style_plan.TicketSelection("read", "names", "crates/alpha", (self.sources[0],), (read,)),
        ))

    def test_held_cell_skip_ignores_waived_findings_beside_filed_ones(self) -> None:
        read, expected = self.held_cell_with_final_findings()
        self.hold_more(read, "waived")
        self.assertEqual(self.candidates(), ())
        self.hold_more(read, "unfiled")
        self.assertEqual(self.candidates(), expected)

    def test_held_cell_with_only_final_findings_is_not_skipped(self) -> None:
        read, expected = self.held_cell_with_final_findings()
        self.hold_more(read, "waived", "built", "withdrawn")
        with closing(sqlite3.connect(self.ledger().database)) as connection:
            _ = connection.execute("UPDATE held_findings SET state='built' WHERE state='filed'")
            connection.commit()
        self.assertEqual({type(finding.state) for finding in self.ledger().held_findings(style_ledger.AllHeldFindings())},
                         {style_ledger.Waived, style_ledger.Built, style_ledger.Withdrawn})
        self.assertEqual(self.candidates(), expected)

if __name__ == "__main__":
    _ = unittest.main()
