"""Compare the ledger readers with the binary supplied by the Rust integration test."""

from __future__ import annotations

import json
import locale
import os
import sqlite3
import sys
import tempfile
from collections.abc import Callable
from contextlib import AbstractContextManager, closing, nullcontext
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import cast
from unittest import mock

from scripts.style import declarations, style_config, style_ledger, style_plan, style_reader
from scripts.lib import berth_board
from scripts.style.ledger_test_support import LedgerTestCase, PARENT_FILE
from scripts.style.refill_findings import encode_proof
from scripts.style.refill_inputs import input_identity


@dataclass(frozen=True)
class SearchAnswer:
    name: str
    pathspecs: tuple[str, ...]
    maintained: bool
    paths: tuple[str, ...]
    failure: str


@dataclass(frozen=True)
class LedgerReading:
    tip: style_ledger.RepositoryTip
    cached_tip: style_ledger.RepositoryTip
    entries: dict[str, dict[str, style_ledger.TreeEntry]]
    searches: tuple[SearchAnswer, ...]


def ledger_reading(repo: Path, root: Path, source: style_config.ReaderSource) -> LedgerReading:
    """Read the same inventory and searches through the selected implementation."""
    settings = root / "style_reader.conf"
    _ = settings.write_text(f"# one of: python, rust\nreader={source.value}\n")
    read_tip = cast(Callable[[Path, str], style_ledger.RepositoryTip], vars(style_ledger)["_tip"])
    tip = read_tip(repo, "HEAD")
    cached_tip = style_ledger.GitReadCache(repo).tip(tip.commit)
    entries = {revision: style_ledger.tree_entries(repo, revision)
               for revision in ("HEAD", "HEAD~0", tip.commit[:12], tip.tree, "HEAD:nested")}
    searches: list[SearchAnswer] = []
    for maintained in (False, True):
        with style_ledger.Ledger(repo, root / f"{source.value}-{maintained}-{os.environ['LC_ALL']}.sqlite") as ledger:
            observation = cast(Callable[[], AbstractContextManager[None]], getattr(ledger, "_finding_observation"))
            with observation() if maintained else nullcontext():
                for pathspecs in (("*.rs",), ("*.rs", "*.md")):
                    for name in ("shared", "working", "café", "引数", "absent_word", "r#type", "T", "T²", "²shared", "x", "xⅧ"):
                        try:
                            paths = ledger.files_naming(tip.commit, name, pathspecs)
                            answer = SearchAnswer(name, pathspecs, maintained, paths, "")
                        except style_ledger.GitReadFailed as error:
                            answer = SearchAnswer(name, pathspecs, maintained, (), type(error).__name__)
                        searches.append(answer)
    return LedgerReading(tip, cached_tip, entries, tuple(searches))



def refill_readings(settings: Path) -> dict[str, str]:
    """Compare real refill results and require no saved Rust blob facts."""
    LedgerTestCase.setUpClass()
    fixture = LedgerTestCase(methodName="runTest")
    try:
        fixture.setUp()
        _ = settings.write_text("reader=python\n")
        ledger, rule, finding = fixture.site_fixture(
            source="//! Work.\r\nfn work() {\r\n    let old = 791;\r\n    work();\r\n}\r\n", line=3, keep_ticket_open=True)
        changes: tuple[tuple[style_ledger.FindingKind, str, str], ...] = (
            ("rename", "a.rs", "Rename a.rs to renamed.rs"), ("restructure", "work", "Split the module"))
        others = tuple(ledger.hold(rule, PARENT_FILE, 2, "fn work() {", kind, before, after, finding.rule_blob, finding.ticket)
                       for kind, before, after in changes)
        ledger.ticket_drop(finding.ticket)
        for other in others:
            ledger.file_held(other.finding_id, fixture.root / "decision.md")
        identities = (finding.finding_id, *(other.finding_id for other in others))
        _ = fixture.rule("listed", "tier: B\nfamily: names\ninput_scope: file\ncandidates:\n  kind: regex\n  surface: whole\n  pattern: old\n")
        invalid_path = "crates/alpha/vendor/invalid.rs"
        ignored_path = "crates/alpha/vendor/ignored.rs"
        fixture.write(ignored_path, "mod child;\nfn ignored() {}\n")
        _ = (fixture.repo / invalid_path).write_bytes(b"fn work() {} // \xff\n")
        recovery_source = "fn recovered() {}\n"
        recovery_file = fixture.root / "recovered.rs"
        _ = recovery_file.write_text(recovery_source)
        recovery_blob = fixture.git("hash-object", "-w", str(recovery_file))
        tip = fixture.commit_tip(ledger, {PARENT_FILE: "// Header.\r\n//! Work.\r\nfn work() {\r\n    let old = 791;\r\n    work();\r\n}\r\n"})
        database = ledger.database
        ledger.close()
        readings: dict[str, str] = {}
        for source in style_config.ReaderSource:
            _ = settings.write_text(f"reader={source.value}\n")
            copy = fixture.root / f"refill-{source.value}.sqlite"
            with closing(sqlite3.connect(database)) as original, closing(sqlite3.connect(copy)) as target:
                original.backup(target)
                tables = cast(list[tuple[str]], target.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'refill_%'").fetchall())
                for (table,) in tables:
                    _ = target.execute(f'DELETE FROM "{table}"')
                target.commit()
            with style_ledger.Ledger(fixture.repo, copy) as reader:
                with reader.finding_reads():
                    read_tip = cast(Callable[[str], style_ledger.RepositoryTip], getattr(reader, "_tip"))
                    code_of = cast(Callable[[str], style_ledger.RustCode | style_ledger.NotTokenized],
                                   getattr(reader, "_rust_code"))
                    with mock.patch.object(style_reader, "read_blob_facts", wraps=style_reader.read_blob_facts) as requests:
                        inventory = read_tip(tip)
                        ignored_code = code_of(inventory.blobs[ignored_path])
                        if not isinstance(ignored_code, style_ledger.RustCode):
                            raise AssertionError("the ignored Rust file has no code identity")
                        if source == style_config.ReaderSource.RUST and requests.call_count != 1:
                            raise AssertionError(f"the tip needs {requests.call_count} blob requests, including ignored Rust files")
                    tickets = style_plan.candidate_tickets(
                        fixture.repo, reader, style_plan.BusyFiles(berth_board.Board(()), frozenset()),
                        "names", base=style_plan.NoUnitBranch())
                    followed = reader.declaration_reading(identities, lambda _: True)
                    findings = reader.named_held(identities)
                    sites = reader.finding_sites(findings, tip)
                    edited = style_plan.EditedFiles(fixture.repo, reader, style_plan.NoUnitBranch())
                    edited.read({tip: (PARENT_FILE,)})
                    files = tuple(edited.of(current) for current in findings)
                    blob = fixture.git("rev-parse", f"{tip}:{PARENT_FILE}")
                    modules = edited.declaration_text(blob)
                    declared = cast(Callable[[str, str], declarations.DeclaredFile], getattr(reader, "_declared_file"))(tip, PARENT_FILE)
                    item = next(item for item in declared.declared_items if item.declaration.name == "work")
                    body = declarations.item_body(declared, item)
                    if not isinstance(body, declarations.ReadableBody):
                        raise AssertionError("the fixture function has no readable body")
                    names = cast(Callable[[str, str, declarations.DeclaredItem, declarations.ReadableBody],
                                          declarations.NameSites | declarations.UnreadableBody],
                                 getattr(reader, "_old_name_sites"))(tip, PARENT_FILE, item, body)
                    code = code_of(blob)
                    code_identity = code.digest.hex() if isinstance(code, style_ledger.RustCode) else code
                    invalid = cast(Callable[[str, str], declarations.DeclaredFile],
                                   getattr(reader, "_declared_file"))(tip, invalid_path)
                    if tuple(item.declaration.name for item in invalid.declared_items) != ("work",):
                        raise AssertionError("replacement decoding lost the invalid UTF-8 file's declaration")
                    if code_of(inventory.blobs[invalid_path]) != style_ledger.NOT_TOKENIZED:
                        raise AssertionError("invalid UTF-8 must keep the strict code identity refusal")
                    observe = cast(Callable[[], AbstractContextManager[None]], getattr(reader, "_finding_observation"))
                    with observe():
                        links = cast(Callable[[str, str], style_ledger.PlainModuleLinks | style_ledger.UnprovedModuleLinks],
                                     getattr(reader, "_plain_module_links"))(tip, ignored_path)
                    if source == style_config.ReaderSource.RUST:
                        object_path = fixture.repo / ".git/objects" / recovery_blob[:2] / recovery_blob[2:]
                        hidden = object_path.with_name(object_path.name + ".hidden")
                        _ = object_path.rename(hidden)
                        try:
                            try:
                                _ = reader.blob_facts(recovery_blob)
                            except style_ledger.CheckCouldNotRun:
                                pass
                            else:
                                raise AssertionError("the absent blob provided declaration facts")
                        finally:
                            _ = hidden.rename(object_path)
                        recovered = reader.blob_facts(recovery_blob).declared_file(recovery_source)
                        if tuple(item.declaration.name for item in recovered.declared_items) != ("recovered",):
                            raise AssertionError("the restored blob did not provide its declaration")
                readings[source.value] = encode_proof((tickets, followed, sites, files, modules, declared.declared_items,
                                                        names, code_identity, invalid.declared_items, links))
            with closing(sqlite3.connect(copy)) as connection:
                forbidden = cast(list[tuple[str]], connection.execute(
                    "SELECT fact_kind FROM refill_objects WHERE fact_kind IN ('tree','tree_entry') "
                    + "UNION ALL SELECT input_kind FROM refill_inputs WHERE input_kind IN ('rust_code','finding_declarations')"
                ).fetchall())
                module_links = cast(list[tuple[str]], connection.execute(
                    "SELECT input_identity FROM refill_inputs WHERE input_kind='finding_module_links'").fetchall())
                if (input_identity(inventory.blobs[ignored_path]),) not in module_links:
                    raise AssertionError("the reader did not save the ignored file's module links")
                objects = cast(list[tuple[str]], connection.execute(
                    "SELECT objects FROM refill_findings WHERE finding_id=? AND view='synced'",
                    (finding.finding_id,)).fetchall())
                if not objects or any(blob not in cast(list[str], json.loads(value)) for (value,) in objects):
                    raise AssertionError("the first declaration read lost the finding's source blob")
            if source == style_config.ReaderSource.RUST and forbidden:
                raise AssertionError(f"the Rust refill saved blob facts: {forbidden}")
        if readings["python"] != readings["rust"]:
            raise AssertionError(f"refill results differ: {readings}")
        return readings
    finally:
        fixture.doCleanups()
        LedgerTestCase.doClassCleanups()


def main(repo: Path, binary: Path) -> None:
    """Print both readings without building or changing the target worktree."""
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        previous = {key: os.environ.get(key) for key in ("CONF_DIR", "LC_ALL")}
        os.environ["CONF_DIR"] = str(root)
        try:
            unicode_locale = ""
            for candidate in ("C.UTF-8", "en_US.UTF-8", "UTF-8"):
                try:
                    unicode_locale = locale.setlocale(locale.LC_CTYPE, candidate)
                    break
                except locale.Error:
                    continue
            if not unicode_locale:
                raise RuntimeError("the parity test requires a UTF-8 locale")
            readings: dict[str, dict[str, object]] = {}
            with style_reader.use_client(style_reader.ReaderClient(binary)):
                for source in style_config.ReaderSource:
                    readings[source.value] = {}
                    for selected_locale in ("C", unicode_locale):
                        os.environ["LC_ALL"] = selected_locale
                        readings[source.value][selected_locale] = asdict(ledger_reading(repo, root, source))
                refill = refill_readings(root / "style_reader.conf")
                for source, result in refill.items():
                    readings[source]["refill"] = result
            print(json.dumps(readings, ensure_ascii=False))
        finally:
            for key, value in previous.items():
                if value is None:
                    _ = os.environ.pop(key, None)
                else:
                    os.environ[key] = value


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
