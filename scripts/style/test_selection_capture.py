"""Prove capture and replay against private selection fixtures."""

from contextlib import closing
import hashlib
import os
from pathlib import Path
import sqlite3
import shutil
import subprocess
from typing import cast, final, override
from unittest.mock import patch

from scripts.lib import conf
from scripts.production.staff_test_support import isolated_staff_file, recorded_showrunner, recorded_unit
from scripts.style import refill_measure, selection_capture, style_config, style_ledger
from scripts.style.style_plan_test_support import StylePlanFixture, install_unconfigured_board


@final
class SelectionCaptureTests(StylePlanFixture):
    database: Path = Path()
    proposal: Path = Path()

    @override
    def setUp(self) -> None:
        super().setUp()
        executable = install_unconfigured_board(self.root / "board-bin", self.repo)
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"PATH": str(executable.parent) + os.pathsep + os.environ["PATH"]})))
        _ = self.enterContext(isolated_staff_file(self.root / "staff.json", {}))
        _ = recorded_showrunner("sample", self.repo / "docs/product-production.md")
        _ = recorded_unit("sample-style", "names", self.unit, self.doc,
                          plan="docs/style-production/units/names-unit.md")
        rule = self.rule("capture-name")
        self.sync()
        self.proposal = self.approved_decision(rule, self.sources[:1], "type", name="capture")
        with self.ledger() as ledger:
            self.database = ledger.database

    def source_rows(self) -> tuple[str, ...]:
        """Read all source rows for an exact preservation check."""
        with closing(sqlite3.connect(self.database)) as connection:
            return tuple(connection.iterdump())

    def test_capture_preserves_source_rows_and_hashes_each_copied_file(self) -> None:
        _ = self.rule("interrupted-capture")
        removed_rule = self.rule_dir / "interrupted-capture.md"
        folder = self.root / "capture"
        copied_sources: list[Path] = []
        copy_file = shutil.copyfile

        def record_copy(source: Path, destination: Path) -> Path:
            copied = copy_file(source, destination)
            copied_sources.append(source)
            return copied

        with (patch.object(selection_capture, "repository_files", side_effect=RuntimeError("capture interrupted")),
              patch.object(shutil, "copyfile", side_effect=record_copy)):
            with self.assertRaisesRegex(RuntimeError, "capture interrupted"):
                _ = selection_capture.capture(self.repo, self.database, "names", folder)
        self.assertIn(removed_rule, copied_sources)
        removed_rule.unlink()
        before = self.source_rows()
        manifest = selection_capture.capture(self.repo, self.database, "names", folder)
        self.assertEqual(self.source_rows(), before)

        copied = {str(entry["copy"]) for entry in manifest["files"]}
        actual = {path.relative_to(folder).as_posix() for path in folder.rglob("*")
                  if path.is_file() and path != folder / "manifest.json"}
        self.assertEqual(copied, actual)
        self.assertFalse((folder / "rules/rust/interrupted-capture.md").exists())
        replayed = selection_capture.replay(folder, style_config.ReaderSource.PYTHON, self.root)
        self.assertFalse(replayed["external_changes"])
        for entry in manifest["files"]:
            with self.subTest(source=entry["source"], kind=entry["kind"]):
                self.assertEqual(hashlib.sha256((folder / entry["copy"]).read_bytes()).hexdigest(), entry["sha256"])
        sources = {Path(entry["source"]).resolve() for entry in manifest["files"]}
        for source in (self.database, self.plan, self.proposal, self.rule_dir / "capture-name.md"):
            self.assertIn(source.resolve(), sources)

    def test_replays_share_one_digest_and_report_unconfigured_input_changes(self) -> None:
        before = self.source_rows()
        folder = self.root / "capture"
        manifest = selection_capture.capture(self.repo, self.database, "names", folder)
        first = selection_capture.replay(folder, style_config.ReaderSource.PYTHON, self.root)
        second = selection_capture.replay(folder, style_config.ReaderSource.PYTHON, self.root)
        self.assertEqual(first["result_digest"], second["result_digest"])
        self.assertFalse(first["external_changes"])
        self.assertFalse(second["external_changes"])
        ignore = self.repo / ".git/info/exclude"
        entry, = (entry for entry in manifest["files"] if Path(entry["source"]).resolve() == ignore.resolve())
        _ = ignore.write_text(ignore.read_text() + "\n# Capture replay change.\n")
        changed = selection_capture.replay(folder, style_config.ReaderSource.PYTHON, self.root)
        self.assertEqual(changed["result_digest"], first["result_digest"])
        self.assertEqual([(Path(change["source"]).resolve(), change["expected"], change["actual"])
                          for change in changed["external_changes"]],
                         [(ignore.resolve(), entry["sha256"], hashlib.sha256(ignore.read_bytes()).hexdigest())])
        self.assertEqual(self.source_rows(), before)

        with self.subTest(environment="unset XDG_CONFIG_HOME"):
            configuration = self.root / ".config" / "cargo-mend" / "config.toml"
            configuration.parent.mkdir(parents=True, exist_ok=True)
            _ = configuration.write_text("# Private capture configuration.\n")
            with patch.dict(os.environ):
                _ = os.environ.pop("XDG_CONFIG_HOME", None)
                _ = self.rule("capture-mend", "tier: A\nmechanism: mend\n")
                with self.ledger() as ledger:
                    ledger.lint_record(self.tip, "mend", True, {})
                original = self.source_rows()
                state = self.root / "direct-state"
                database = state / style_ledger.ledger_location(self.repo).database.parent.name / "ledger.sqlite"
                database.parent.mkdir(parents=True)
                refill_measure.backup_ledger(self.database, database)
                _ = refill_measure.clear_derived(database)
                direct = refill_measure.measured_run(self.repo, database, "names",
                                                     dict(os.environ, STYLE_LEDGER_STATE_DIR=str(state)))
                captured = self.root / "xdg-capture"
                _ = selection_capture.capture(self.repo, self.database, "names", captured)
                replayed = selection_capture.replay(captured, style_config.ReaderSource.PYTHON, self.root)
                self.assertEqual(direct["result_digest"], replayed["result_digest"])
                self.assertEqual(self.source_rows(), original)

        with self.subTest(input="staff configuration also in CONF_DIR"):
            config = self.root / "shared-config"
            config.mkdir()
            for path in conf.directory().glob("*.conf"):
                _ = shutil.copy2(path, config / path.name)
            staff_config = config / "staff.conf"
            original_config = (Path(__file__).resolve().parents[2] / "config/staff.conf").read_text()
            _ = staff_config.write_text(original_config)
            with patch.dict(os.environ, {"CONF_DIR": str(config), "STAFF_CONFIG": str(staff_config)}):
                captured = self.root / "staff-capture"
                _ = selection_capture.capture(self.repo, self.database, "names", captured)
                _ = staff_config.write_text(original_config + "\n# Changed live staff configuration.\n")
                private = self.root / "staff-environment"
                private.mkdir()
                environment = selection_capture.replay_environment(captured, style_config.ReaderSource.PYTHON, private)
                self.assertNotEqual(Path(environment["STAFF_CONFIG"]), staff_config)
                self.assertEqual(Path(environment["STAFF_CONFIG"]).read_text(), original_config)
                report = selection_capture.replay(captured, style_config.ReaderSource.PYTHON, self.root)
                self.assertIn(str(staff_config), [entry["source"] for entry in report["external_changes"]])

        with self.subTest(input="untracked Rust file in source repository"):
            captured = self.root / "source-capture"
            _ = selection_capture.capture(self.repo, self.database, "names", captured)
            added = self.repo / "new_source.rs"
            _ = added.write_text("pub fn new_source() {}\n")
            report = selection_capture.replay(captured, style_config.ReaderSource.PYTHON, self.root)
            self.assertTrue(any(entry["source"] in {str(added), "git:status:" + str(self.repo)}
                                for entry in report["external_changes"]), report["external_changes"])

        with self.subTest(input="XDG Git include"):
            xdg = self.root / "include-xdg"
            git_config = xdg / "git/config"
            git_config.parent.mkdir(parents=True)
            extra = git_config.parent / "extra.conf"
            _ = git_config.write_text("[include]\n    path = extra.conf\n")
            _ = extra.write_text("[observation]\n    marker = captured\n")
            with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(xdg)}):
                # GIT_CONFIG_GLOBAL makes Git skip the XDG file; the merge gate sets it.
                _ = os.environ.pop("GIT_CONFIG_GLOBAL", None)
                captured = self.root / "include-capture"
                _ = selection_capture.capture(self.repo, self.database, "names", captured)
                _ = extra.write_text("[observation]\n    marker = changed\n")
                private = self.root / "include-environment"
                private.mkdir()
                environment = selection_capture.replay_environment(captured, style_config.ReaderSource.PYTHON, private)
                result = subprocess.run(["git", "-C", str(self.repo), "config", "--get", "observation.marker"],
                                        env=environment, capture_output=True, text=True, check=False)
                report = selection_capture.replay(captured, style_config.ReaderSource.PYTHON, self.root)
                self.assertTrue(result.stdout.strip() == "captured" or
                                any(entry["source"] in {str(extra), "git:configuration"}
                                    for entry in report["external_changes"]),
                                (result.stdout, report["external_changes"]))
