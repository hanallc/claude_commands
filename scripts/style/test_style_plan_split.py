"""Split accounting, cross-production selection and refill recording."""

from __future__ import annotations

from scripts.style.ledger_test_support import legacy_overlapping_tickets
from scripts.style.ledger_test_support import ticket_owner_id

import io
import os
import re
import select
import signal
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
from collections.abc import Callable, Generator, Mapping
from contextlib import chdir, closing, contextmanager, nullcontext, redirect_stderr, redirect_stdout
from datetime import UTC, datetime
from pathlib import Path

from typing import cast, final, override
from unittest import mock


from scripts.style import approved_renames
from scripts.style import declarations
from scripts.style import style_plan
from scripts.production.staff_test_support import fixture_member, isolated_staff_file, production_membership, recorded_showrunner, recorded_unit
from scripts.production import live_units
from scripts.style import unit_plans
from scripts.style import style_proposals

from scripts.lib import berth_board
from scripts.lib.fork_test_support import ForkChildren
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.style import rules
from scripts.style import style_ledger
from scripts.production import style_units

from scripts.style.style_plan_test_support import StylePlanFixture, unit_staff_id
from scripts.style.style_config import StyleUnitAssignment


@final
class StylePlanTests(StylePlanFixture):
    @override
    def setUp(self) -> None:
        super().setUp()
        _ = self.enterContext(isolated_staff_file(self.root / "staff.json", {}))
        _ = recorded_showrunner("sample", self.repo / "docs/product-production.md")
        _ = recorded_unit("sample-style", "names", self.unit, self.doc,
                          plan="docs/style-production/units/names-unit.md")

    def test_combined_unit_classifies_findings_with_each_rules_family(self) -> None:
        self.style_units((StyleUnitAssignment("names", ("names", "types")),
                          StyleUnitAssignment("values", ("values",)),
                          StyleUnitAssignment("modules", ("modules",))))
        names = self.rule("split-names")
        types = self.rule("split-types", "tier: C\nfamily: types\ninput_scope: file\n")
        values = self.rule("split-values", "tier: C\nfamily: values\ninput_scope: file\n")
        self.sync()
        _ = self.approved_decision(names, self.sources[:1], "type", name="names")
        _ = self.approved_decision(types, self.sources[2:3], "type", name="types")
        _ = self.approved_decision(values, self.sources[1:2], "type", name="values")
        ledger = self.ledger()
        definitions = {rule.rule_id: rule for rule in rules.load_rules(self.repo)}
        with (mock.patch.object(style_proposals.RankedWork, "waits", return_value={}) as waits,
              mock.patch.object(style_proposals, "ranked_below_unit", return_value=False)):
            result = style_plan.classify_split(ledger, "names", self.doc, self.base(), unit_plans.AllCrates(),
                                               style_plan.BusyFiles(self.board(), frozenset()), definitions,
                                               style_ledger.GitReadCache(self.repo), "2026-10-28T12:00:00+00:00")

        self.assertEqual((result.ready, result.changed, result.structure, result.other), (2, 0, 0, 0))
        self.assertEqual({call.args[0] for call in waits.call_args_list}, {"names", "types"})

    def test_split_partitions_the_table_left_total_including_ticketed_and_unselected_findings(self) -> None:
        rule = self.rule("split-population")
        names = ("free", "ticketed", "judged", "unsynced", "structure", "exempt", "busy", "gone",
                 "unknown", "closed", "declaration")
        paths = {name: f"crates/alpha/src/{name}.rs" for name in names}
        paths["outside"] = "crates/beta/src/outside.rs"
        self.commit_files({path: f"pub fn {name}() {{}}\n" for name, path in paths.items()})
        issue, findings = self.judged_findings(rule, tuple((path, 1, f"pub fn {name}() {{}}")
                                                        for name, path in paths.items()))
        ledger = self.ledger()
        by_path = {finding.path: finding for finding in findings}
        for name in ("unknown", "closed"):
            decision = issue.with_name(name + ".md")
            text = issue.read_text()
            text = text.replace(rule, "global:rust/removed-rule.md") if name == "unknown" else text.replace("status: open", "status: closed")
            _ = decision.write_text(text)
            with closing(sqlite3.connect(ledger.database)) as connection, connection:
                _ = connection.execute("UPDATE held_findings SET proposal=? WHERE finding_id=?",
                                       (str(decision), by_path[paths[name]].finding_id))
        ledger.ticket_add("2", ticket_owner_id("names"), "proposal", "names", "crates/alpha", [paths["ticketed"]],
                          [rule], proposal=str(issue), finding_ids=(by_path[paths["ticketed"]].finding_id,))
        with legacy_overlapping_tickets():
            ledger.ticket_assign("2", "demo-style-names")
        for index, excluded in enumerate(("declined", "unanswered", "types", "waived", "unfiled", "built"), 2):
            decision = issue.with_name(excluded + ".md")
            text = issue.read_text()
            finding_rule, finding_blob = rule, findings[0].rule_blob
            if excluded == "declined":
                text = text.replace("plan: approved", "plan: approved\nstyle_answer: declined")
            elif excluded == "unanswered":
                text = re.sub(r"unit: [^\n]*\n", "", text)
            elif excluded == "types":
                _ = fixture_member("unit", name="types", parent=recorded_showrunner("sample-style", self.doc).id)
                finding_rule = self.rule("excluded-types", "tier: C\nfamily: types\ninput_scope: file\n")
                finding_blob = next(item.blob for item in rules.load_rules(self.repo) if item.rule_id == finding_rule)
                text = text.replace(f"unit: {unit_staff_id('names')}", f"unit: {unit_staff_id('types')}")
                text = text.replace(rule, finding_rule).replace(findings[0].rule_blob, finding_blob)
                ledger.ticket_add("6001", ticket_owner_id("types"), "read", "types", "crates/alpha",
                                  [paths["free"]], [finding_rule])
                ledger.ticket_drop("6001")
            _ = decision.write_text(text)
            finding = ledger.hold(finding_rule, paths["free"], index, excluded, "type", "before", "after",
                                  finding_blob, "outside")
            if excluded != "unfiled":
                ledger.file_held(finding.finding_id, decision)
            if excluded in ("waived", "built"):
                with closing(sqlite3.connect(ledger.database)) as connection, connection:
                    _ = connection.execute("UPDATE held_findings SET state=? WHERE finding_id=?",
                                           (excluded, finding.finding_id))
        self.commit_files({paths["judged"]: "pub fn judged() { changed(); }\n"})
        _ = (self.repo / paths["gone"]).unlink()
        self.commit_to_every_checkout()
        outcomes: tuple[style_ledger.DeclarationOutcome, ...] = tuple(
            style_ledger.DeclarationGoneFromFile(finding, declarations.Declaration("fn", "declaration", declarations.ModuleItem()))
            if finding.path == paths["declaration"] else style_ledger.DeclarationNotRead(finding) for finding in findings)
        waits = {paths[name]: style_proposals.RankWait("names", paths[name], "types")
                 for name in ("structure", "exempt", "judged", "unsynced", "ticketed")}

        def exempt(finding: style_ledger.HeldFinding, _family: rules.Family) -> bool:
            return finding.path == paths["exempt"]

        self.finish_judging()
        definitions = {definition.rule_id: definition for definition in rules.load_rules(self.repo)}
        measured_at = "2026-10-28T12:00:00+00:00"
        with (mock.patch.object(style_ledger.Ledger, "declaration_reading", return_value=style_ledger.DeclarationReading(self.tip, outcomes)),
              mock.patch.object(style_proposals.RankedWork, "waits", return_value=waits),
              mock.patch.object(style_proposals, "ranked_below_unit", return_value=True),
              mock.patch.object(style_proposals.RankExemptions, "allows", side_effect=exempt),
              mock.patch.object(style_plan, "unsynced_changes", return_value={
                  paths["unsynced"]: style_plan.UnsyncedChange(paths["unsynced"], self.tip)})):
            split = style_plan.classify_split(ledger, "names", self.doc, self.base(),
                                              unit_plans.PilotCrates(frozenset({"crates/alpha"})),
                                              style_plan.BusyFiles(self.board(), frozenset({paths["busy"], paths["ticketed"]})),
                                              definitions, style_ledger.GitReadCache(self.repo), measured_at)
        self.assertEqual(split, style_ledger.RecordedSplit(3, 2, 1, 6, measured_at))
        left = split.ready + split.changed + split.structure + split.other
        output = style_units.table([live_units.LiveUnit("names", None)], {}, doc=self.doc, now=datetime(2026, 10, 28, 12, tzinfo=UTC),
                                   zone=UTC, ledger_paths={"names": ledger.database})
        self.assertIn(f"| 1 | 0 | {left} approved of 14 filed |", output)

    def test_a_merged_ticket_keeps_its_filed_type_finding_in_other_until_a_person_marks_it_built(self) -> None:
        rule = self.rule("split-merged-type")
        self.sync()
        issue = self.approved_decision(rule, self.sources[:1], "type")
        ledger = self.ledger()
        finding, = ledger.proposal_findings(issue)
        ledger.ticket_add("1", ticket_owner_id("names"), "proposal", "names", "crates/alpha", [finding.path],
                          [rule], proposal=str(issue), finding_ids=(finding.finding_id,))
        ledger.ticket_assign("1", "demo-style-names")
        ledger.ticket_checkpoint("1", self.tip)
        self.assertEqual(ledger.ticket_merge("1"),
                         (style_ledger.FindingNotBuilt(finding.finding_id, approved_renames.NoAppliedCheck("type")),))
        held, = ledger.proposal_findings(issue)
        self.assertIsInstance(held.state, style_ledger.Filed)
        definitions = {definition.rule_id: definition for definition in rules.load_rules(self.repo)}
        measured_at = "2026-10-28T12:00:00+00:00"
        with (mock.patch.object(ledger, "declaration_reading", return_value=style_ledger.DeclarationReading(
                  self.tip, (style_ledger.DeclarationNotRead(held),))),
              mock.patch.object(style_proposals.RankedWork, "waits", return_value={}),
              mock.patch.object(style_proposals, "ranked_below_unit", return_value=False)):
            result = style_plan.classify_split(ledger, "names", self.doc, self.base(), unit_plans.AllCrates(),
                                               style_plan.BusyFiles(self.board(), frozenset()), definitions,
                                               style_ledger.GitReadCache(self.repo), measured_at)
        self.assertEqual(result, style_ledger.RecordedSplit(0, 0, 0, 1, measured_at))
        self.assertEqual(ledger.proposal_findings(issue), (held,))

    def test_relative_proposals_use_the_table_population_and_refills_exact_stored_key(self) -> None:
        rule = self.rule("split-relative-proposal")
        self.sync()
        original, _ = self.judged_findings(rule, tuple((path, 1, "pub fn value() {}") for path in self.sources[:2]))
        self.finish_judging()
        ledger = self.ledger()
        folder = self.repo / "decisions"
        folder.mkdir()
        issue = folder / "a.md"
        _ = issue.write_text(original.read_text())
        _ = self.doc.write_text(self.doc.read_text().replace(str(original.parent), str(folder)))
        findings = ledger.proposal_findings(original)
        with closing(sqlite3.connect(ledger.database)) as connection, connection:
            for finding, path in zip(findings, (str(issue), "decisions/a.md"), strict=True):
                _ = connection.execute("UPDATE held_findings SET proposal=? WHERE finding_id=?", (path, finding.finding_id))
        definitions = {definition.rule_id: definition for definition in rules.load_rules(self.repo)}
        measured_at = "2026-10-28T12:00:00+00:00"
        with (chdir(self.repo),
              mock.patch.object(ledger, "declaration_reading", return_value=style_ledger.DeclarationReading(
                  self.tip, tuple(style_ledger.DeclarationNotRead(finding) for finding in findings))),
              mock.patch.object(style_proposals.RankedWork, "waits", return_value={}),
              mock.patch.object(style_proposals, "ranked_below_unit", return_value=False)):
            result = style_plan.classify_split(ledger, "names", self.doc, self.base(), unit_plans.AllCrates(),
                                               style_plan.BusyFiles(self.board(), frozenset()), definitions,
                                               style_ledger.GitReadCache(self.repo), measured_at)
            table = style_units.table([live_units.LiveUnit("names", None)], {}, doc=self.doc,
                                      ledger_paths={"names": ledger.database})
        self.assertEqual(result, style_ledger.RecordedSplit(1, 0, 0, 1, measured_at))
        self.assertIn("| 0 | 0 | 2 |", table)
        self.assertEqual(result.ready + result.changed + result.structure + result.other, 2)

    def test_refill_and_split_readers_agree_on_rank_waits_and_changed_judgments(self) -> None:
        name_rule = self.rule("shared-name-inputs")
        type_rule = self.rule("shared-type-inputs", "tier: C\nfamily: types\ninput_scope: file\n")
        self.sync()
        issue, findings = self.judged_findings(name_rule, tuple((path, 1, "pub fn value() {}")
                                                              for path in self.sources[:2]))
        ledger = self.ledger()
        # The helper files each finding after hold(); site evidence consumes the current ledger row.
        findings = ledger.proposal_findings(issue)
        definition = next(item for item in rules.load_rules(self.repo) if item.rule_id == type_rule)
        ledger.ticket_add("2001", ticket_owner_id("types"), "read", "types", "crates/alpha", [self.sources[1]], [type_rule])
        with legacy_overlapping_tickets():
            ledger.ticket_assign("2001", "style/main")
        _ = ledger.ticket_base("2001")
        for index, path in enumerate(self.sources[:2]):
            _ = ledger.hold(type_rule, path, 1, "type work", "type", "before", "after", definition.blob,
                            "outside" if index == 0 else "2001")
        ledger.ticket_drop("2001")
        self.commit_files({self.sources[1]: "pub fn value() { changed(); }\n"})
        self.finish_judging()
        scopes: dict[rules.Family, unit_plans.PilotScope] = {"names": unit_plans.AllCrates(), "types": unit_plans.AllCrates()}
        cache = style_ledger.GitReadCache(self.repo)
        expected = {self.sources[0]: style_proposals.RankWait("names", self.sources[0], "types")}
        self.assertEqual(style_proposals.ranked_work(ledger, (), scopes).waits("names"), expected)
        self.assertEqual(style_proposals.ranked_work(ledger, (), scopes, reader=cache).waits("names"), expected)
        with style_ledger.Ledger(self.repo, ledger.database, git_cache=cache) as cached_ledger:
            for finding in findings:
                with self.subTest(path=finding.path):
                    unchanged = finding.path == self.sources[0]
                    self.assertEqual(style_proposals.judged_at_current_file(ledger, finding, self.tip), unchanged)
                    self.assertEqual(style_proposals.judged_at_current_file(cached_ledger, finding, self.tip), unchanged)
        reading = style_ledger.DeclarationReading(self.tip, tuple(style_ledger.DeclarationNotRead(finding) for finding in findings))
        definitions = {item.rule_id: item for item in rules.load_rules(self.repo)}
        measured_at = "2026-10-28T12:00:00+00:00"
        output = io.StringIO()
        with (mock.patch.object(style_plan, "family_pilot_scopes", return_value=scopes),
              mock.patch.object(style_proposals.RankExemptions, "allows", return_value=False),
              mock.patch.object(style_proposals, "run_follow", return_value=style_proposals.FollowRun(reading, (), ())),
              mock.patch.object(ledger, "declaration_reading", return_value=reading), redirect_stdout(output)):
            split = style_plan.classify_split(ledger, "names", self.doc, self.base(), unit_plans.AllCrates(),
                                              style_plan.BusyFiles(self.board(), frozenset()), definitions, cache, measured_at)
            _ = style_plan._select_phase(ledger, ticket_owner_id("names"), "demo-style-names", self.plan, self.repo,  # pyright: ignore[reportPrivateUsage]
                                         self.doc, self.plan.read_text(), unit_plans.AllCrates(),
                                         style_plan.BusyFiles(self.board(), frozenset()), frozenset(), definitions, style_plan.MainChanges(self.tip), family="names")
        self.assertEqual(split, style_ledger.RecordedSplit(0, 1, 1, 0, measured_at))
        self.assertIn(f"waiting names: {self.sources[0]} has open types work\n", output.getvalue())
        changed = next(finding for finding in findings if finding.path == self.sources[1])
        self.assertIn(f"waiting {issue}: {changed.path} changed since it was judged (1 finding)\n", output.getvalue())


    def test_split_changes_only_refill_results_and_keeps_the_plan_and_decisions(self) -> None:
        rule = self.rule("read-only-split")
        self.sync()
        issue, _ = self.judged_findings(rule, ((self.sources[0], 1, "pub fn value() {}"),))
        self.finish_judging()
        ledger = self.ledger()
        findings = ledger.proposal_findings(issue)
        before_plan, before_issue = self.plan.read_bytes(), issue.read_bytes()
        with closing(sqlite3.connect(ledger.database)) as connection:
            before = tuple(connection.iterdump())
        output, errors = io.StringIO(), io.StringIO()
        started = datetime.now(UTC)
        with (mock.patch.object(style_plan, "style_document", return_value=self.doc),
              mock.patch.object(style_plan, "busy_files", return_value=style_plan.BusyFiles(self.board(), frozenset())),
              mock.patch.object(style_ledger.Ledger, "declaration_reading", return_value=style_ledger.DeclarationReading(
                  self.tip, tuple(style_ledger.DeclarationNotRead(finding) for finding in findings))),
              mock.patch.object(style_ledger, "refill_lock", side_effect=AssertionError("split takes no refill lock")),
              mock.patch.object(style_ledger.Ledger, "ticket_add", side_effect=AssertionError("split makes no ticket")),
              mock.patch.object(style_ledger.Ledger, "decline_findings", side_effect=AssertionError("split waives nothing")),
              mock.patch.object(style_proposals, "run_follow", side_effect=AssertionError("split applies no declaration follow")),
              mock.patch.object(style_proposals, "proposal_command", side_effect=AssertionError("split changes no decision")),
              mock.patch.object(style_proposals, "renumber", side_effect=AssertionError("split does not renumber")),
              mock.patch.object(style_plan, "write_phase", side_effect=AssertionError("split writes no phase")),
              redirect_stdout(output), redirect_stderr(errors)):
            code = style_plan.main(["split", "--unit", "names", "--repo", str(self.unit)])
        self.assertEqual((code, errors.getvalue()), (0, ""))
        self.assertRegex(output.getvalue(), r"^names: 1/0/0/0 in \d+\.\d{2} min\n$")
        with closing(sqlite3.connect(ledger.database)) as connection:
            after = tuple(line for line in connection.iterdump() if not line.startswith('INSERT INTO "refill_results"'))
            row = cast(tuple[str, str, str, int, int, int, int], connection.execute(
                "SELECT at,outcome,split_at,ready,changed,structure,other FROM refill_results WHERE unit='names'").fetchone())
        self.assertEqual(after, before)
        self.assertEqual((row[0], row[1], *row[3:]), ("", "", 1, 0, 0, 0))
        measured_at = datetime.fromisoformat(row[2])
        self.assertEqual(measured_at.utcoffset(), UTC.utcoffset(measured_at))
        self.assertLessEqual(started, measured_at)
        self.assertLessEqual(measured_at, datetime.now(UTC))
        self.assertEqual((self.plan.read_bytes(), issue.read_bytes()), (before_plan, before_issue))
        rendered = style_units.table([live_units.LiveUnit("names", None)], {}, doc=self.doc, now=measured_at, zone=UTC,
                                     ledger_paths={"names": ledger.database})
        self.assertIn("| names | exited | idle | idle | 0 | 0 | 1: ready 1 · changed 0 · structure 0 · other 0 (0 min) |", rendered)

    def test_a_scan_error_or_time_limit_keeps_the_last_split_and_reports_the_cause(self) -> None:
        _ = self.rule("failed-split")
        self.sync()
        ledger = self.ledger()
        style_ledger.record_split(self.repo, "names", style_ledger.RecordedSplit(9, 8, 7, 6, "2026-10-27T12:00:00+00:00"))
        before = ledger.database.read_bytes()

        clock = self.root / "split-clock"
        _ = clock.write_text("0")
        classify_split = style_plan.classify_split

        for cause in ("cannot read tree", "time limit of 1 min reached"):
            with self.subTest(cause=cause):
                output, errors = io.StringIO(), io.StringIO()

                def classify(ledger: style_ledger.Ledger, unit: str, doc: Path, base: style_plan.UnitBase,
                             scope: unit_plans.PilotScope, busy: style_plan.BusyFiles,
                             definitions: Mapping[str, rules.Rule], cache: style_ledger.GitReadCache,
                             measured_at: str) -> style_ledger.RecordedSplit:
                    if cause == "cannot read tree":
                        raise OSError(cause)
                    result = classify_split(ledger, unit, doc, base, scope, busy, definitions, cache, measured_at)
                    _ = clock.write_text("61")
                    return result

                with (mock.patch.object(style_plan, "style_document", return_value=self.doc),
                      mock.patch.object(style_plan, "time", wraps=time,
                                        monotonic=mock.Mock(side_effect=lambda: float(clock.read_text()))),
                      mock.patch.object(style_plan, "busy_files", return_value=style_plan.BusyFiles(self.board(), frozenset())),
                      mock.patch.object(style_plan, "classify_split", side_effect=classify),
                      mock.patch.object(style_ledger, "record_split_at", wraps=style_ledger.record_split_at) as record,
                      redirect_stdout(output), redirect_stderr(errors)):
                    code = style_plan.main(["split", "--unit", "names", "--repo", str(self.unit), "--timeout-minutes", "1"])
                self.assertEqual((code, output.getvalue()), (1, ""))
                self.assertTrue(errors.getvalue().startswith("split names: "))
                self.assertIn(cause, errors.getvalue())
                record.assert_not_called()
                self.assertEqual(ledger.database.read_bytes(), before)

    def test_split_write_stops_at_its_deadline_on_a_locked_ledger(self) -> None:
        _ = self.rule("write-time-limit")
        self.sync()
        ledger = self.ledger()
        style_ledger.record_split(self.repo, "names", style_ledger.RecordedSplit(9, 8, 7, 6, "2026-10-27T12:00:00+00:00"))
        with closing(sqlite3.connect(ledger.database)) as connection:
            before = tuple(connection.iterdump())
        output, errors = io.StringIO(), io.StringIO()

        limit_seconds = 0.25
        deadline = style_plan.SplitDeadline(limit_seconds, limit_seconds / 60, lambda: 0.0)
        busy_budgets: list[int] = []
        record_split_at = style_ledger.record_split_at

        connect = sqlite3.connect

        def observe_connect(database: str, *, uri: bool, timeout: float) -> sqlite3.Connection:
            connection = connect(database, uri=uri, timeout=timeout)

            def observe_timeout(action: int, pragma: str | None, value: str | None,
                                _schema: str | None, _trigger: str | None) -> int:
                if action == sqlite3.SQLITE_PRAGMA and pragma == "busy_timeout" and value is not None:
                    busy_budgets.append(int(value))
                return sqlite3.SQLITE_OK

            connection.set_authorizer(observe_timeout)
            return connection

        def bounded_write(database: Path, unit: str, split: style_ledger.RecordedSplit, *,
                          deadline: style_plan.SplitDeadline) -> None:
            with mock.patch.object(sqlite3, "connect", side_effect=observe_connect):
                record_split_at(database, unit, split, deadline=deadline)

        # Give the real ledger write the full test budget.
        measured = style_plan.MeasuredSplit(ledger.database, style_ledger.RecordedSplit(
            1, 2, 3, 4, "2026-10-28T12:00:00+00:00"))
        with (closing(sqlite3.connect(ledger.database)) as writer,
              mock.patch.object(style_plan, "split_time_limit", return_value=nullcontext(deadline)),
              mock.patch.object(style_plan, "bounded_split_measurement", return_value=measured),
              mock.patch.object(style_ledger, "record_split_at", side_effect=bounded_write) as record,
              redirect_stdout(output), redirect_stderr(errors)):
            _ = writer.execute("BEGIN IMMEDIATE")
            code = style_plan.main(["split", "--unit", "names", "--repo", str(self.unit),
                                    "--timeout-minutes", str(limit_seconds / 60)])
            writer.rollback()
        self.assertEqual((code, output.getvalue()), (1, ""))
        self.assertRegex(errors.getvalue(), r"^split names: (time limit.*|database is locked)\n$")
        record.assert_called_once_with(ledger.database, "names", measured.split, deadline=deadline)
        self.assertEqual(busy_budgets, [250])
        with closing(sqlite3.connect(ledger.database)) as connection:
            self.assertEqual(tuple(connection.iterdump()), before)

    def test_split_does_not_restart_the_write_deadline_when_a_reader_blocks_commit(self) -> None:
        ledger = self.ledger()
        style_ledger.record_split(self.repo, "names", style_ledger.RecordedSplit(9, 8, 7, 6, "2026-10-27T12:00:00+00:00"))
        before = ledger.database.read_bytes()
        measured = style_plan.MeasuredSplit(ledger.database, style_ledger.RecordedSplit(
            1, 2, 3, 4, "2026-10-28T12:00:00+00:00"))
        output, errors = io.StringIO(), io.StringIO()
        limit_seconds = 2.0
        release_after_seconds = 1.25
        with (closing(sqlite3.connect(ledger.database)) as reader,
              closing(sqlite3.connect(ledger.database, check_same_thread=False)) as reservation,
              mock.patch.object(style_plan, "bounded_split_measurement", return_value=measured),
              redirect_stdout(output), redirect_stderr(errors)):
            mode, = cast(tuple[str], reader.execute("PRAGMA journal_mode").fetchone())
            self.assertIn(mode, {"delete", "truncate", "persist"})
            _ = reader.execute("BEGIN")
            last_ready = cast(tuple[int], reader.execute("SELECT ready FROM refill_results WHERE unit = 'names'").fetchone())
            self.assertEqual(last_ready, (9,))
            with closing(sqlite3.connect(ledger.database, timeout=0)) as probe:
                _ = probe.execute("UPDATE refill_results SET ready = ready + 1 WHERE unit = 'names'")
                with self.assertRaisesRegex(sqlite3.OperationalError, "database is locked"):
                    probe.commit()
                probe.rollback()
            _ = reservation.execute("BEGIN IMMEDIATE")
            release = threading.Timer(release_after_seconds, reservation.rollback)
            started = time.monotonic()
            release.start()
            try:
                code = style_plan.main(["split", "--unit", "names", "--repo", str(self.unit),
                                        "--timeout-minutes", str(limit_seconds / 60)])
                elapsed = time.monotonic() - started
            finally:
                release.cancel()
                release.join(TEST_HANG_GUARD_SECS)
        self.assertLess(elapsed, limit_seconds + release_after_seconds / 2)
        self.assertEqual((code, output.getvalue(), errors.getvalue()),
                         (1, "", f"split names: time limit of {limit_seconds / 60:g} min reached\n"))
        self.assertEqual(ledger.database.read_bytes(), before)

    @contextmanager
    def split_commit_attempt(
        self, *, expire_after_insert: bool = False,
        commit: Callable[[sqlite3.Connection], None] = sqlite3.Connection.commit,
    ) -> Generator[style_plan.MeasuredSplit]:
        ledger = self.ledger()
        style_ledger.record_split(self.repo, "names", style_ledger.RecordedSplit(9, 8, 7, 6, "2026-10-27T12:00:00+00:00"))
        measured = style_plan.MeasuredSplit(ledger.database, style_ledger.RecordedSplit(
            1, 2, 3, 4, "2026-10-28T12:00:00+00:00"))
        connect = sqlite3.connect
        connections: list[sqlite3.Connection] = []
        commit_started = False

        def clock() -> float:
            if commit_started or (expire_after_insert and any(connection.total_changes for connection in connections)):
                return 61.0
            return 0.0

        class TimedCommitConnection(sqlite3.Connection):
            @override
            def commit(self) -> None:
                nonlocal commit_started
                commit_started = True
                commit(self)

        def connect_for_split(database: str, *, uri: bool, timeout: float) -> sqlite3.Connection:
            connection = connect(database, uri=uri, timeout=timeout, factory=TimedCommitConnection)
            connections.append(connection)
            return connection

        deadline = style_plan.SplitDeadline(60.0, 1.0, clock)
        with (mock.patch.object(style_plan, "split_time_limit", return_value=nullcontext(deadline)),
              mock.patch.object(style_plan, "bounded_split_measurement", return_value=measured),
              mock.patch.object(sqlite3, "connect", side_effect=connect_for_split)):
            yield measured

    def test_split_expiry_after_insert_rolls_back_before_commit(self) -> None:
        with self.split_commit_attempt(expire_after_insert=True) as measured:
            before = measured.database.read_bytes()
            with self.assertRaisesRegex(style_ledger.SplitTimedOut, "time limit of 1 min reached"):
                _ = style_plan.split("names", self.unit, timeout_minutes=1)
        self.assertEqual(measured.database.read_bytes(), before)

    def test_split_reports_success_when_commit_finishes_after_the_deadline(self) -> None:
        with self.split_commit_attempt() as measured:
            result = style_plan.split("names", self.unit, timeout_minutes=1)
        self.assertEqual(result, measured.split)
        with closing(sqlite3.connect(measured.database)) as connection:
            row = cast(tuple[str, int, int, int, int], connection.execute(
                "SELECT split_at, ready, changed, structure, other FROM refill_results WHERE unit = 'names'"
            ).fetchone())
        self.assertEqual(row, (result.measured_at, result.ready, result.changed, result.structure, result.other))

    def test_split_preserves_the_commit_error_and_cause_after_the_deadline(self) -> None:
        cause = OSError("fixture storage failed")
        failure = sqlite3.OperationalError("disk I/O error")

        def fail_commit(_connection: sqlite3.Connection) -> None:
            raise failure from cause

        with self.split_commit_attempt(commit=fail_commit) as measured:
            before = measured.database.read_bytes()
            with self.assertRaisesRegex(sqlite3.OperationalError, "disk I/O error") as caught:
                _ = style_plan.split("names", self.unit, timeout_minutes=1)
        self.assertIs(caught.exception, failure)
        self.assertIs(caught.exception.__cause__, cause)
        self.assertEqual(measured.database.read_bytes(), before)

    def test_ledger_lookup_that_returns_after_the_deadline_leaves_the_last_split(self) -> None:
        ledger = self.ledger()
        style_ledger.record_split(self.repo, "names", style_ledger.RecordedSplit(9, 8, 7, 6, "2026-10-27T12:00:00+00:00"))
        before = ledger.database.read_bytes()
        lookup_finished = self.root / "ledger-lookup-finished"
        ledger_location = style_ledger.ledger_location
        output, errors = io.StringIO(), io.StringIO()

        def resolve_after_deadline(repo: Path) -> style_ledger.LedgerLocation:
            location = ledger_location(repo)
            lookup_finished.touch()
            return location

        # Replace the measurement so the test delays only the lookup for the ledger write.
        measurement = style_ledger.RecordedSplit(1, 2, 3, 4, "2026-10-28T12:00:00+00:00")
        with (mock.patch.object(style_plan, "measure_split", return_value=measurement),
              mock.patch.object(style_ledger, "ledger_location", side_effect=resolve_after_deadline),
              mock.patch.object(style_plan, "time", wraps=time,
                                monotonic=mock.Mock(side_effect=lambda: 61.0 if lookup_finished.exists() else 0.0)),
              redirect_stdout(output), redirect_stderr(errors)):
            code = style_plan.main(["split", "--unit", "names", "--repo", str(self.unit), "--timeout-minutes", "1"])

        self.assertTrue(lookup_finished.exists())
        self.assertEqual(ledger.database.read_bytes(), before)
        self.assertEqual((code, output.getvalue(), errors.getvalue()),
                         (1, "", "split names: time limit of 1 min reached\n"))

    def test_killing_the_split_caller_stops_the_blocked_scan_and_removes_its_folder(self) -> None:
        with ForkChildren() as children:
            started_reader, started_writer = children.pipe()
            release_reader, _release_writer = children.pipe()

            def block_scan(*_arguments: object) -> style_ledger.RecordedSplit:
                _ = os.write(started_writer, f"{os.getpid()}\n{os.environ['TMPDIR']}".encode())
                _ = os.read(release_reader, 1)
                raise RuntimeError("The fixture expected the scan to stop")

            with (mock.patch.dict(os.environ, TMPDIR=str(self.root)),
                  mock.patch.object(style_plan, "measure_split", side_effect=block_scan)):
                caller = children.start(
                    lambda: style_plan.main(["split", "--unit", "names", "--repo", str(self.unit)]),
                    pass_fds=(started_writer, release_reader),
                )
            ready, _, _ = select.select([started_reader], [], [], TEST_HANG_GUARD_SECS)
            self.assertTrue(ready, "The scan did not start")
            started = os.read(started_reader, 4096).decode()
            if not started:
                self.fail(f"The split ended before the scan started: {caller.result().stderr}")
            pid_text, folder_text = started.splitlines()
            folder = Path(folder_text)
            self.assertTrue(folder.name.startswith("style-split-"))
            self.assertTrue(folder.is_dir())
            scan_pid = int(pid_text)
            self.assertNotEqual(scan_pid, caller.pid)

            os.kill(caller.pid, signal.SIGKILL)
            self.assertEqual(caller.result().returncode, -signal.SIGKILL)
            ended, _, _ = select.select([started_reader], [], [], TEST_HANG_GUARD_SECS)
            self.assertTrue(ended, "The scan still holds its pipe after the caller died")
            self.assertEqual(os.read(started_reader, 1), b"")
            with self.assertRaises(ProcessLookupError):
                os.kill(scan_pid, 0)
            self.assertFalse(folder.exists())

    def test_split_write_is_refused_after_its_deadline(self) -> None:
        _ = self.rule("expired-write")
        self.sync()
        ledger = self.ledger()
        style_ledger.record_split(self.repo, "names", style_ledger.RecordedSplit(9, 8, 7, 6, "2026-10-27T12:00:00+00:00"))
        before = ledger.database.read_bytes()
        deadline = style_plan.SplitDeadline(time.monotonic() - 1, 1)
        output, errors = io.StringIO(), io.StringIO()
        with (mock.patch.object(style_plan, "split_time_limit", return_value=nullcontext(deadline)),
              mock.patch.object(style_plan, "style_document", return_value=self.doc),
              mock.patch.object(style_plan, "busy_files", return_value=style_plan.BusyFiles(self.board(), frozenset())),
              mock.patch.object(style_plan, "classify_split", return_value=style_ledger.RecordedSplit(
                  1, 2, 3, 4, "2026-10-28T12:00:00+00:00")),
              mock.patch.object(style_ledger, "record_split_at", wraps=style_ledger.record_split_at) as record,
              redirect_stdout(output), redirect_stderr(errors)):
            code = style_plan.main(["split", "--unit", "names", "--repo", str(self.unit)])
        self.assertEqual((code, output.getvalue(), errors.getvalue()),
                         (1, "", "split names: time limit of 1 min reached\n"))
        record.assert_not_called()
        self.assertEqual(ledger.database.read_bytes(), before)

    def test_a_ledger_without_refill_results_is_refused_before_any_scan_or_migration(self) -> None:
        _ = self.rule("old-schema-split")
        self.sync()
        ledger = self.ledger()
        with closing(sqlite3.connect(ledger.database)) as connection, connection:
            _ = connection.execute("DROP TABLE refill_results")
        before = ledger.database.read_bytes()
        output, errors = io.StringIO(), io.StringIO()
        with (mock.patch.object(style_ledger, "preview_ledger", side_effect=AssertionError("no backup or scan of the old schema")),
              mock.patch.object(style_plan, "classify_split", side_effect=AssertionError("no long scan")),
              mock.patch.object(style_ledger, "record_split_at", side_effect=AssertionError("no split write")),
              redirect_stdout(output), redirect_stderr(errors)):
            code = style_plan.main(["split", "--unit", "names", "--repo", str(self.unit)])
        self.assertEqual((code, output.getvalue(), errors.getvalue()),
                         (1, "", "split names: ledger has no refill_results (schema below 25)\n"))
        self.assertEqual(ledger.database.read_bytes(), before)


class UnsyncedChangeTests(unittest.TestCase):
    def test_a_base_commit_not_in_the_synced_tip_names_each_file_it_changed_and_its_newest_commit(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            repo = Path(folder)

            def commit(path: str, text: str) -> str:
                _ = (repo / path).write_text(text)
                for arguments in (("add", path), ("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", path)):
                    _ = subprocess.run(["git", "-C", folder, *arguments], check=True)
                return subprocess.run(["git", "-C", folder, "rev-parse", "HEAD"], check=True, capture_output=True,
                                      text=True).stdout.strip()

            _ = subprocess.run(["git", "init", "-q", folder], check=True)
            tip = commit("a.rs", "fn a() {}\n")
            first = commit("b.rs", "fn b() {}\n")
            newest = commit("b.rs", "fn b() { c(); }\n")
            changes = style_plan.unsynced_changes(repo, style_plan.UnitBranchTip(newest), tip)
            self.assertEqual(dict(changes), {"b.rs": style_plan.UnsyncedChange("b.rs", newest)})
            self.assertNotEqual(first, newest)
            self.assertEqual(changes["b.rs"].line(tip), f"b.rs changed in {newest[:7]}, not in the synced tip {tip[:7]}")
            # Only the first path of a commit loses the newline that `-z` puts before it; a tab path sorts first.
            for path in ("\tfirst.rs", "\nline.rs"):
                _ = (repo / path).write_text("text\n")
            for arguments in (("add", "--all"), ("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "two")):
                _ = subprocess.run(["git", "-C", folder, *arguments], check=True)
            both = subprocess.run(["git", "-C", folder, "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
            self.assertEqual(set(style_plan.unsynced_changes(repo, style_plan.UnitBranchTip(both), newest)), {"\tfirst.rs", "\nline.rs"})
            # A base inside the synced tip, and no unit branch, change no file.
            self.assertEqual(dict(style_plan.unsynced_changes(repo, style_plan.UnitBranchTip(tip), newest)), {})
            self.assertEqual(dict(style_plan.unsynced_changes(repo, style_plan.NoUnitBranch(), tip)), {})


class RefillRecordingTests(unittest.TestCase):
    @override
    def setUp(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        _ = self.enterContext(isolated_staff_file(root / "staff.json", {}))
        production_membership("production")

    def test_main_records_every_outcome_after_next_phase_returns(self) -> None:
        cases: tuple[tuple[object, str, int, str], ...] = (
            (style_plan.PhaseWritten("1"), "phase written", 0, "phase written: 1\n"),
            (style_plan.Resumed("1"), "resumed", 0, "resume 1\n"),
            (style_plan.RepairFirst(), "repair first", 0, "repair first\n"),
            (style_plan.WaitingForMerge("1", "abcdef123"), "waiting for merge", 5,
             "waiting for merge: 1 checkpointed at abcdef1\n"),
            (style_plan.WaitingForSync(0), "waiting for sync", 3, "waiting for sync\n"),
            (style_plan.WaitingForStructure(("alpha", "beta"), 0), "waiting for structure: alpha, beta", 3,
             "waiting for structure: alpha, beta\n"),
            (style_plan.WaitingForSync(0, "read alpha: main touched alpha/lib.rs"),
             "waiting for sync: read alpha: main touched alpha/lib.rs", 3,
             "waiting for sync: read alpha: main touched alpha/lib.rs\n"),
            (style_plan.MergeStyleBranchFirst(1, "abcdef123", "refs/heads/style/main"),
             "merge the style branch first", 6, "merge the style branch first: 1 cell judged at abcdef1 wait for style/main\n"),
            (berth_board.BoardUnavailable("offline"), "board unavailable", 4, "board unavailable: offline\n"),
        )
        for result, words, code, printed in cases:
            with self.subTest(outcome=words, result=result):
                output = io.StringIO()
                calls: list[str] = []

                def next_result(_unit: str, _plan: Path, _repo: Path) -> object:
                    calls.append("returned")
                    return result

                def record(_repo: Path, _unit: str, _outcome: str) -> None:
                    calls.append("recorded")

                with (mock.patch.object(style_plan, "next_phase", side_effect=next_result),
                      mock.patch.object(style_ledger, "record_refill", side_effect=record) as recorded,
                      redirect_stdout(output)):
                    self.assertEqual(style_plan.main(["next-phase", "--unit", "names", "--plan", "/tmp/claude/plan.md",
                                                      "--repo", "/tmp/claude/repo"]), code)
                recorded.assert_called_once_with(Path("/tmp/claude/repo"), "names", words)
                self.assertEqual(calls, ["returned", "recorded"])
                self.assertEqual(output.getvalue(), printed)

    def test_an_exception_from_next_phase_records_nothing(self) -> None:
        for error in (OSError("cannot read"), ValueError("invalid plan"), sqlite3.OperationalError("database error")):
            with self.subTest(error=error):
                output = io.StringIO()
                with (mock.patch.object(style_plan, "next_phase", side_effect=error),
                      mock.patch.object(style_ledger, "record_refill") as recorded, redirect_stderr(output)):
                    self.assertEqual(style_plan.main(["next-phase", "--unit", "names", "--plan", "/tmp/claude/plan.md",
                                                      "--repo", "/tmp/claude/repo"]), 2)
                recorded.assert_not_called()
                self.assertIn(str(error), output.getvalue())




from scripts.production.staff_test_support import StaffTestCase
from scripts.style import unit_files

from scripts.style.style_reader_test_support import oracle_reader


def setUpModule() -> None:
    _ = unittest.enterModuleContext(oracle_reader())


class BranchFileLocationTests(StaffTestCase):
    def test_file_ownership_uses_the_staff_worktree(self) -> None:
        fixture = self.separated_unit_folders()
        rows = unit_files.unit_worktrees(fixture.doc)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].worktree, fixture.worktree)
        self.assertEqual(rows[0].unit, "member")


if __name__ == "__main__":
    _ = unittest.main()
