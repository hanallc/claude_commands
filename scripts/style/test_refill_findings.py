"""Compare maintained finding proofs with the existing semantic readers."""

from __future__ import annotations

import json
import sqlite3
import unittest
from collections.abc import Callable
from contextlib import AbstractContextManager, closing
from dataclasses import replace
from pathlib import Path
from typing import cast, final
from unittest import mock

from scripts.style import refill_facts, refill_findings, style_ledger, style_plan
from scripts.style.ledger_test_support import LedgerTestCase, PARENT_FILE, ticket_owner_id
from scripts.style.refill_findings import encode_proof


@final
class FindingResultsTests(LedgerTestCase):
    def assert_recomputation(self, ids: tuple[str, ...], approval: bool) -> style_ledger.DeclarationReading:
        with self.ledger() as maintained:
            actual = maintained.declaration_reading(ids, lambda _: approval)
        with self.ledger() as full, mock.patch.object(full, "_maintain_findings", False):
            expected = full.declaration_reading(ids, lambda _: approval)
            self.assertEqual(actual, expected)
        with self.ledger() as reopened:
            self.assertEqual(reopened.declaration_reading(ids, lambda _: approval), expected)
        return actual

    def test_finding_views_match_source_and_approval_changes(self) -> None:
        ledger, rule, finding = self.site_fixture(source="fn work() {}\n", line=1, before="work")
        ids = (finding.finding_id,)
        _ = self.assert_recomputation(ids, False)
        for label, changes in (
            ("renamed", {PARENT_FILE: "fn fresh() {}\n"}),
            ("added declaration", {self.sources[1]: "fn fresh() {}\n"}),
            ("removed declaration", {self.sources[1]: "fn other() {}\n"}),
            ("moved declaration", {PARENT_FILE: "fn other() {}\n", self.sources[1]: "fn work() {}\n"}),
        ):
            with self.subTest(change=label):
                _ = self.commit_tip(ledger, dict[str, str | None](changes))
                for approval in (False, True):
                    _ = self.assert_recomputation(ids, approval)
        with closing(sqlite3.connect(ledger.database)) as connection, connection:
            # The trigger reads the subscribers of the changed row, not every subscriber of the table.
            _ = connection.executemany("INSERT INTO refill_dependencies VALUES (?,?,?,?,'')",
                                       [(table, f"unrelated-{index}", "git_object", f"object-{index}")
                                        for table in ("refill_findings", "refill_cells") for index in range(20000)])
            steps: list[int] = []
            connection.set_progress_handler(lambda: steps.append(1) and 0, 1000)
            _ = connection.execute("UPDATE held_findings SET path=?,line=1 WHERE finding_id=?", (self.sources[1], finding.finding_id))
            _ = connection.execute("UPDATE judgments SET verdict=verdict WHERE rule=?", (rule,))
            # The coverage read walks the dependencies of its own catalog's cells, not every stored git object.
            _ = connection.execute(style_ledger.SYNCED_CELL_OBJECTS, ("another-catalog",)).fetchall()
            connection.set_progress_handler(None, 0)
            self.assertLess(len(steps), 20)
        _ = self.assert_recomputation(ids, True)

        with closing(sqlite3.connect(ledger.database)) as connection, connection:
            _ = connection.execute("UPDATE judgments SET verdict='exception' WHERE rule=?", (rule,))
        _ = self.assert_recomputation(ids, True)

        with closing(sqlite3.connect(ledger.database)) as connection, connection:
            _ = connection.execute("UPDATE held_findings SET state='built' WHERE finding_id=?", ids)
        _ = self.assert_recomputation(ids, True)

        with self.subTest(change="built Self endpoint checkpoint"):
            method = ("pub struct Parent;\n\nimpl Parent {\n    pub fn old() {\n        let x = 1;\n"
                      + "    }\n    pub fn caller() {\n        Self::old();\n    }\n}\n")
            _ = self.commit_tip(ledger, {PARENT_FILE: method})
            rule = self.rule("names")
            ledger.ticket_add("4100", ticket_owner_id("judge"), "read", "names", "crates/alpha", [PARENT_FILE], [rule])
            ledger.ticket_assign("4100", "main")
            _ = ledger.ticket_base("4100")
            dependent = ledger.hold(rule, PARENT_FILE, 5, "let x = 1;", "restructure", "Parent::old", "Parent::new",
                                    self.blob(rule), "4100").finding_id
            ledger.ticket_drop("4100")
            ledger.file_held(dependent, self.root / "decision.md")
            endpoint = self.built_finding(ledger, PARENT_FILE, "Self::old", "Self::new", line=8)
            _ = self.commit_tip(ledger, {PARENT_FILE: method.replace("old", "new")})
            before = self.assert_recomputation((dependent,), True)
            self.assertIsInstance(before.outcomes[0], style_ledger.DeclarationMoved)
            self.write(PARENT_FILE, method.replace("Parent", "Other"))
            checkpoint = self.commit("Change the endpoint owner")
            ticket = ledger.named_held([endpoint])[0].ticket
            with closing(sqlite3.connect(ledger.database)) as connection, connection:
                _ = connection.execute("UPDATE tickets SET checkpoint=? WHERE id=?", (checkpoint, ticket))
            after = self.assert_recomputation((dependent,), True)
            self.assertIsInstance(after.outcomes[0], style_ledger.DeclarationGoneFromFile)

    def test_unchanged_views_read_no_historical_source(self) -> None:
        ledger, finding, base, tip = self.site_with_comment_checkpoint()
        _ = self.built_finding(ledger, self.sources[1], "value", "renamed")
        tip = ledger.synced_commit()
        ids = (finding.finding_id,)
        expected = self.assert_recomputation(ids, True)
        with self.ledger() as reopened:
            with (mock.patch.object(reopened, "_compute_declaration_groups", wraps=getattr(reopened, "_compute_declaration_groups")) as groups,
                  mock.patch.object(reopened, "_commit_file", wraps=getattr(reopened, "_commit_file")) as sources,
                  mock.patch.object(reopened, "_prepare_sites", wraps=getattr(reopened, "_prepare_sites")) as preparation):
                self.assertEqual(reopened.declaration_reading(ids, lambda _: True), expected)
                self.assertEqual(reopened.finding_site(finding, tip), ledger.finding_site(finding, tip))
                groups.assert_not_called()
                sources.assert_not_called()
                preparation.assert_not_called()
        with closing(sqlite3.connect(ledger.database)) as connection, connection:
            _ = connection.execute("UPDATE judgments SET verdict_detail='new detail' WHERE rule=?", (finding.rule,))
        with self.ledger() as changed:
            with mock.patch.object(changed, "_built_rename_for", wraps=getattr(changed, "_built_rename_for")) as endpoints:
                self.assertEqual(changed.declaration_reading(ids, lambda _: True), expected)
                endpoints.assert_not_called()
        source_blob = self.git("rev-parse", f"{base}:{PARENT_FILE}")
        object_path = self.repo / ".git/objects" / source_blob[:2] / source_blob[2:]
        hidden = object_path.with_name(object_path.name + ".hidden")
        _ = object_path.rename(hidden)
        try:
            for maintained in (False, True):
                with self.subTest(missing_object=True, maintained=maintained), self.ledger() as reader:
                    with mock.patch.object(reader, "_maintain_findings", maintained):
                        with self.assertRaises(style_ledger.GitReadFailed):
                            _ = reader.declaration_reading(ids, lambda _: True)
        finally:
            _ = hidden.rename(object_path)
        # The source row can change without changing its public finding fields.
        with closing(sqlite3.connect(ledger.database)) as connection, connection:
            _ = connection.execute("UPDATE held_findings SET anchor_commit='' WHERE finding_id=?", ids)
        _ = self.assert_recomputation(ids, True)

        with self.subTest(search="raw identifier", missing_object="unmatched Rust blob"):
            _ = self.commit_tip(ledger, {PARENT_FILE: "pub fn r#type() {}\n",
                                         self.sources[2]: "pub fn unmatched_search_blob() {}\n"})
            rule, (raw,) = self.judge_parent(ledger, ((1, "pub fn r#type() {}"),), before="r#type", after="fresh")
            ledger.ticket_add("1", ticket_owner_id("names"), "proposal", "names", "crates/alpha", [PARENT_FILE], [rule],
                              proposal=str(self.root / "decision.md"), finding_ids=(raw,))
            ledger.ticket_assign("1", "main")
            current = self.commit_tip(ledger, {PARENT_FILE: "pub fn fresh() {}\n"})
            kept = self.assert_recomputation((raw,), True)
            self.assertIsInstance(kept.outcomes[0], style_ledger.KeptForOpenBuild)
            blob = self.git("rev-parse", f"{current}:{self.sources[2]}")
            object_path = self.repo / ".git/objects" / blob[:2] / blob[2:]
            hidden = object_path.with_name(object_path.name + ".hidden")
            _ = object_path.rename(hidden)
            try:
                for maintained in (False, True):
                    with self.subTest(maintained=maintained), self.ledger() as reader:
                        with mock.patch.object(reader, "_maintain_findings", maintained):
                            with self.assertRaises(style_ledger.GitReadFailed):
                                _ = reader.declaration_reading((raw,), lambda _: True)
            finally:
                _ = hidden.rename(object_path)

        with self.subTest(read="findings that share a file"):
            source = "pub fn first() {}\n\npub fn second() {}\n"
            _ = self.commit_tip(ledger, {PARENT_FILE: source})
            _, shared = self.judge_parent(ledger, ((1, "pub fn first() {}"), (3, "pub fn second() {}")),
                                          before="first", after="primary")
            current = self.commit_tip(ledger, {PARENT_FILE: "// header\n" + source})
            _ = self.assert_recomputation(shared, True)
            # A stored reading reads the declarations of a file once, though two findings name the file.
            with self.ledger() as reader, mock.patch.object(refill_facts.RefillFacts, "input_fact", autospec=True,
                                                             side_effect=refill_facts.RefillFacts.input_fact) as facts:
                _ = reader.declaration_reading(shared, lambda _: True)
            declared = [call.args[2] for call in facts.call_args_list if call.args[1] == "finding_declarations"]
            self.assertEqual(declared, [self.git("rev-parse", f"{current}:{PARENT_FILE}")])
        with self.subTest(read="many findings in one declaration group"):
            paths = tuple(f"crates/alpha/src/group_{index}.rs" for index in range(12))
            current = self.commit_tip(ledger, {path: f"fn group_{index}() {{\n    let value = {index};\n}}\n"
                                               for index, path in enumerate(paths)})
            rule = self.rule("names")
            ledger.ticket_add("4200", ticket_owner_id("judge"), "read", "names", "crates/alpha", list(paths), [rule])
            ledger.ticket_assign("4200", "main")
            _ = ledger.ticket_base("4200")
            group = tuple(ledger.hold(rule, path, line, f"group {index} line {line}", "restructure", "value", "fresh",
                                      self.blob(rule), "4200")
                          for index, path in enumerate(paths) for line in (1, 2))
            ledger.ticket_drop("4200")
            group_ids = tuple(item.finding_id for item in group)
            blobs = {path: self.git("rev-parse", f"{current}:{path}") for path in paths}
            for count in (2, len(group_ids)):
                with self.subTest(findings=count), self.ledger() as reader:
                    connection = cast(sqlite3.Connection, getattr(reader, "_connection"))
                    statements: list[str] = []
                    connection.set_trace_callback(statements.append)
                    _ = reader.declaration_reading(group_ids[:count], lambda _: True)
                    connection.set_trace_callback(None)
                    depth = commits = 0
                    for statement in statements:
                        if statement.startswith("SAVEPOINT "):
                            depth += 1
                        elif statement.startswith("RELEASE "):
                            depth -= 1
                            commits += int(depth == 0)
                        elif statement == "COMMIT":
                            commits += 1
                    self.assertEqual(commits, 1)
            with self.ledger() as reader:
                connection = cast(sqlite3.Connection, getattr(reader, "_connection"))
                rows = cast(list[tuple[str, str]], connection.execute(
                    "SELECT finding_id,objects FROM refill_findings WHERE view='synced' "
                    + "AND finding_id IN (SELECT value FROM json_each(?))", (json.dumps(group_ids),)).fetchall())
                self.assertEqual(len(rows), len(group_ids))
                owned = {item.finding_id: blobs[item.path] for item in group}
                for identity, encoded in rows:
                    objects = set(cast(list[str], json.loads(encoded)))
                    self.assertIn(owned[identity], objects)
                    self.assertFalse(objects.intersection(set(blobs.values()) - {owned[identity]}))
            for event in ("refused proof", "changed source"):
                with self.subTest(publication=event), self.ledger() as reader:
                    connection = cast(sqlite3.Connection, getattr(reader, "_connection"))
                    with connection:
                        _ = connection.execute("DELETE FROM refill_findings WHERE finding_id IN "
                                               + "(SELECT value FROM json_each(?))", (json.dumps(group_ids),))
                    # The event comes after the group's last calculation and before the group commits its results.
                    outcome_of = cast(Callable[..., style_ledger.DeclarationOutcome], getattr(reader, "_declaration_outcome"))
                    calculated: list[style_ledger.DeclarationOutcome] = []
                    def after_calculation(*args: object) -> style_ledger.DeclarationOutcome:
                        outcome = outcome_of(*args)
                        calculated.append(outcome)
                        if len(calculated) < len(group_ids):
                            return outcome
                        if event == "refused proof":
                            raise style_ledger.LedgerError("proof refused after calculation")
                        with closing(sqlite3.connect(reader.database)) as writer, writer:
                            _ = writer.execute("UPDATE tickets SET updated_at='changed source' WHERE id='4200'")
                        return outcome
                    with mock.patch.object(reader, "_declaration_outcome", side_effect=after_calculation):
                        if event == "refused proof":
                            with self.assertRaises(style_ledger.LedgerError):
                                _ = reader.declaration_reading(group_ids, lambda _: True)
                        else:
                            _ = reader.declaration_reading(group_ids, lambda _: True)
                    self.assertEqual(connection.execute("SELECT count(*) FROM refill_findings WHERE finding_id IN "
                                     + "(SELECT value FROM json_each(?))", (json.dumps(group_ids),)).fetchone(), (0,))

    def test_footprints_change_with_source_inputs_and_ignore_reservations(self) -> None:
        ledger, _, finding = self.site_fixture(source="fn work() {}\n", line=1, before="work")
        finding = replace(finding, before="crate::work", after="crate::task")
        _ = self.commit_tip(ledger, {"docs/reference.md": "crate::work\n"})
        base = style_plan.NoUnitBranch()
        for change in ("initial", "busy", "recorded need", "tip"):
            with self.subTest(change=change):
                if change == "busy":
                    with closing(sqlite3.connect(ledger.database)) as connection, connection:
                        _ = connection.execute("UPDATE tickets SET reservation_phase='busy' WHERE id=?", (finding.ticket,))
                if change == "recorded need":
                    ledger.finding_need(finding.finding_id, self.sources[2])
                if change == "tip":
                    _ = self.commit_tip(ledger, {"docs/another.md": "work\n"})
                with self.ledger() as full, mock.patch.object(full, "_maintain_findings", False):
                    expected = style_plan.EditedFiles(self.repo, full, base).of(finding)
                with self.ledger() as maintained:
                    edits = style_plan.EditedFiles(self.repo, maintained, base)
                    with mock.patch.object(edits, "_computed_files", wraps=getattr(edits, "_computed_files")) as calculation:
                        self.assertEqual(edits.of(finding), expected)
                        if change == "busy":
                            calculation.assert_not_called()
                with self.ledger() as warm:
                    edits = style_plan.EditedFiles(self.repo, warm, base)
                    with mock.patch.object(edits, "_computed_files", wraps=getattr(edits, "_computed_files")) as reads:
                        self.assertEqual(edits.of(finding), expected)
                        self.assertEqual(edits.uncertain(finding), ())
                        reads.assert_not_called()
                self.assertIn("docs/reference.md", expected)
        alternate = replace(finding, finding_id="another-footprint")
        with self.ledger() as maintained:
            edits = style_plan.EditedFiles(self.repo, maintained, base)
            _ = edits.of(finding), edits.of(alternate)
        with self.ledger() as reopened:
            edits = style_plan.EditedFiles(self.repo, reopened, base)
            with (mock.patch.object(edits, "_computed_files", wraps=getattr(edits, "_computed_files")) as calculation,
                  mock.patch.object(style_ledger, "_git_bytes", wraps=getattr(style_ledger, "_git_bytes")) as reads):
                _ = edits.of(finding)
                calls = reads.call_count
                _ = edits.of(alternate)
                self.assertEqual(reads.call_count, calls)
                calculation.assert_not_called()

    def test_footprint_searches_store_each_domain_once(self) -> None:
        ledger, _, finding = self.site_fixture(source="fn work() {}\n", line=1, before="work")
        base = style_plan.NoUnitBranch()
        findings = tuple(replace(finding, finding_id=f"search-{index}", before=f"crate::word_{index}",
                                 after=f"crate::task_{index}") for index in range(6))

        def footprints(reader: style_ledger.Ledger) -> tuple[tuple[str, ...], ...]:
            edits = style_plan.EditedFiles(self.repo, reader, base)
            return tuple(edits.of(item) for item in findings)

        def expected() -> tuple[tuple[str, ...], ...]:
            with self.ledger() as full, mock.patch.object(full, "_maintain_findings", False):
                return footprints(full)

        def stored_rows() -> tuple[int, int, int]:
            with closing(sqlite3.connect(ledger.database)) as connection:
                keys = "(SELECT consumer_key FROM refill_findings WHERE finding_id LIKE 'search-%')"
                dependencies = cast(tuple[int], connection.execute(
                    "SELECT count(*) FROM refill_dependencies WHERE consumer_key IN " + keys).fetchone())[0]
                objects = cast(tuple[int], connection.execute(
                    "SELECT coalesce(sum(json_array_length(objects)),0) FROM refill_findings "
                    + "WHERE finding_id LIKE 'search-%'").fetchone())[0]
                groups = cast(tuple[int], connection.execute(
                    "SELECT count(*) FROM refill_inputs WHERE input_kind='object_group'").fetchone())[0]
            return dependencies, objects, groups

        measured: dict[int, tuple[int, int, int]] = {}
        added: dict[str, str | None] = {}
        for files in (4, 16):
            added.update({f"docs/scale_{index}.md": f"word_{index % 6} unrelated_{index}\n" for index in range(files)})
            _ = self.commit_tip(ledger, added)
            full = expected()
            with self.subTest(files=files), self.ledger() as maintained:
                with mock.patch.object(refill_findings.FindingResults, "word_index",
                                       autospec=True, side_effect=refill_findings.FindingResults.word_index) as visits:
                    self.assertEqual(footprints(maintained), full)
                    blobs = {self.git("rev-parse", f"HEAD:{path}") for path in added}
                    self.assertLessEqual(sum(call.args[1] in blobs for call in visits.call_args_list), len(blobs))
            with self.ledger() as warm:
                edits = style_plan.EditedFiles(self.repo, warm, base)
                with mock.patch.object(edits, "_computed_files", wraps=getattr(edits, "_computed_files")) as calculation:
                    self.assertEqual(tuple(edits.of(item) for item in findings), full)
                    calculation.assert_not_called()
            measured[files] = stored_rows()
        # Each finding keeps one reference to the domain, so its rows do not grow with the file count.
        self.assertEqual(measured[4][0], measured[16][0])
        self.assertEqual(measured[4][1], measured[16][1])

        tip = ledger.synced_commit()
        unmatched = self.git("rev-parse", f"{tip}:docs/scale_15.md")
        object_path = self.repo / ".git/objects" / unmatched[:2] / unmatched[2:]
        hidden = object_path.with_name(object_path.name + ".hidden")
        _ = object_path.rename(hidden)
        absent = replace(finding, finding_id="search-absent", before="crate::absent_word", after="crate::other")
        try:
            # Like git grep, a missing blob fails a search only when no other blob matches.
            with self.subTest(missing_object="unmatched domain blob"):
                full = expected()
                with self.ledger() as reader:
                    self.assertEqual(footprints(reader), full)
            for maintained in (False, True):
                with self.subTest(missing_object="no match", maintained=maintained), self.ledger() as reader:
                    with mock.patch.object(reader, "_maintain_findings", maintained):
                        with self.assertRaises(style_ledger.GitReadFailed):
                            _ = style_plan.EditedFiles(self.repo, reader, base).of(absent)
        finally:
            _ = hidden.rename(object_path)

        with self.subTest(change="locale"), mock.patch.dict("os.environ", {"LC_ALL": "C"}):
            full = expected()
            with self.ledger() as maintained:
                edits = style_plan.EditedFiles(self.repo, maintained, base)
                with mock.patch.object(edits, "_computed_files", wraps=getattr(edits, "_computed_files")) as calculation:
                    self.assertEqual(tuple(edits.of(item) for item in findings), full)
                    self.assertEqual(calculation.call_count, len(findings))

        with self.subTest(change="matched blob added and removed"):
            _ = self.commit_tip(ledger, {"docs/scale_3.md": "unrelated\n", "docs/late.md": "word_0 word_5\n"})
            self.assertEqual(footprints(ledger), expected())
            with self.ledger() as warm:
                self.assertEqual(footprints(warm), expected())

        with self.subTest(change="tip rewind"):
            ledger.set_tip(tip)
            with self.ledger() as rewound:
                self.assertEqual(footprints(rewound), expected())

        with self.subTest(change="source version"):
            with closing(sqlite3.connect(ledger.database)) as connection, connection:
                _ = connection.execute("UPDATE tickets SET updated_at='changed source' WHERE id=?", (finding.ticket,))
            with self.ledger() as changed:
                self.assertEqual(footprints(changed), expected())

    def test_word_index_keeps_git_word_and_markdown_behavior(self) -> None:
        for path, source in (("docs/words.md", "work working café caféine 引数 引数値\n"),
                             (self.sources[0], "fn work() {} // café 引数\n"),
                             (self.sources[1], "fn working() {}\n")):
            self.write(path, source)
        tip = self.commit("Word references")
        for line_numbers in (True, False):
            _ = self.git("config", "grep.lineNumber", str(line_numbers).lower())
            for word in ("work", "working", "café", "caféine", "引数", "path"):
                with self.subTest(word=word), self.ledger() as ledger:
                    expected = tuple(line.split(":", 1)[1] for line in self.git(
                        "grep", "-l", "-w", "-F", "-e", word, tip, "--", "*.rs", "*.md").splitlines()) if word != "path" else ()
                    observation = cast(Callable[[], AbstractContextManager[None]], getattr(ledger, "_finding_observation"))
                    with observation():
                        self.assertEqual(ledger.files_naming(tip, word, ("*.rs", "*.md")), expected)
                        with mock.patch.object(style_ledger, "_git_bytes", wraps=getattr(style_ledger, "_git_bytes")) as reads:
                            self.assertEqual(ledger.files_naming(tip, word, ("*.rs", "*.md")), expected)
                            self.assertFalse(any("grep" in call.args for call in reads.call_args_list))
        _ = self.git("config", "core.fsync", "unknown-component")
        self.write("docs/warnings.md", "reviewer r#type\n")
        self.write("docs/empty-warning.md", "")
        warning_tip = self.commit("Search with a Git configuration warning")
        for maintained in (False, True):
            for word, expected in (("reviewer", ("docs/warnings.md",)), ("absent_warning_word", ()),
                                   ("r#type", ("docs/warnings.md",)), ("r#missing", ())):
                with self.subTest(warning=True, maintained=maintained, word=word), self.ledger() as ledger:
                    with mock.patch.object(ledger, "_maintain_findings", maintained):
                        observation = cast(Callable[[], AbstractContextManager[None]], getattr(ledger, "_finding_observation"))
                        with observation():
                            self.assertEqual(ledger.files_naming(warning_tip, word, ("*.rs", "*.md")), expected)
        with self.ledger() as ledger:
            decode = cast(Callable[[str], object], getattr(ledger, "_decode_finding_proof"))
            values = (style_ledger.NeedsJudgment("unknown"), Path("café.md"), frozenset({"file.rs"}))
            self.assertEqual(decode(encode_proof(values)), values)


if __name__ == "__main__":
    _ = unittest.main()
