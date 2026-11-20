"""Prove capture and replay against private selection fixtures."""

from contextlib import closing, redirect_stderr
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import shutil
import subprocess
import sys
import tempfile
import unittest
from typing import cast, final, override
from unittest.mock import patch

from scripts.lib import conf
from scripts.production.staff_test_support import isolated_staff_file, recorded_showrunner, recorded_unit
from scripts.style import refill_measure, selection_capture, style_config, style_ledger, style_reader
from scripts.style.style_plan_test_support import StylePlanFixture, install_unconfigured_board


class SelectionPublicationTests(unittest.TestCase):
    root: Path = Path()
    home: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="selection-capture-fixture-")).resolve()
        self.addCleanup(shutil.rmtree, self.root)
        self.home = self.root / "home"
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"HOME": str(self.home)})))

    @staticmethod
    def stage(_repo: Path, _ledger: Path, _unit: str, folder: Path) -> selection_capture.CaptureManifest:
        _ = (folder / "value").write_text("new capture")
        _ = (folder / "manifest.json").write_text('{"format": 2}')
        return cast(selection_capture.CaptureManifest, cast(object, {"format": 2}))

    def test_capture_publishes_to_the_default_state_folder_in_a_fresh_home(self) -> None:
        output = selection_capture.capture_output_parent()
        self.assertFalse(self.home.exists())
        with patch.object(selection_capture, "capture_inputs", side_effect=self.stage):
            _ = selection_capture.capture(self.root, self.root, "fixture", output)

        self.assertEqual((output / "value").read_text(), "new capture")
        self.assertEqual((output / "manifest.json").read_text(), '{"format": 2}')
        self.assertCountEqual([path.name for path in output.iterdir()], ["value", "manifest.json"])

    def test_capture_publishes_to_a_missing_ancestor_of_the_staging_folder(self) -> None:
        output = self.home / ".local"
        staging_parent = selection_capture.capture_output_parent()
        self.assertFalse(output.exists())
        self.assertTrue(staging_parent.is_relative_to(output))
        with patch.object(selection_capture, "capture_inputs", side_effect=self.stage):
            _ = selection_capture.capture(self.root, self.root, "fixture", output)

        self.assertEqual((output / "value").read_text(), "new capture")
        self.assertEqual((output / "manifest.json").read_text(), '{"format": 2}')
        self.assertEqual(list(staging_parent.iterdir()), [])

    def test_capture_keeps_canaries_and_refuses_an_existing_output(self) -> None:
        root = self.root
        output = root / "capture"

        with patch.object(selection_capture, "capture_inputs", side_effect=self.stage):
            _ = selection_capture.capture(root, root, "fixture", output)
        self.assertEqual((output / "value").read_text(), "new capture")
        paths = [output]
        for location in (self.home, self.home / "rust", self.home / "worktrees", self.home / ".claude"):
            canary = location / "canary"
            canary.mkdir(parents=True)
            paths.append(canary)
        for path in paths:
            _ = (path / "value").write_text("fixture data")
            with self.subTest(path=path), redirect_stderr(io.StringIO()) as error:
                with patch.object(sys, "argv", ["selection_capture", "capture", "--repo", str(root),
                                                "--ledger", str(root), "--unit", "fixture", "--folder", str(path)]):
                    with self.assertRaises(SystemExit) as exit_status:
                        _ = selection_capture.main()
                self.assertEqual(exit_status.exception.code, 2)
                self.assertEqual(len(error.getvalue().splitlines()), 1)
                self.assertIn(f"{path} exists. Pick a new path or remove the old path by hand.", error.getvalue())
                self.assertEqual((path / "value").read_text(), "fixture data")

        late = root / "late-output"
        mkdir = Path.mkdir

        def mkdir_with_competing_output(path: Path, mode: int = 0o777,
                                        parents: bool = False, exist_ok: bool = False) -> None:
            if path == late:
                mkdir(late)
            mkdir(path, mode=mode, parents=parents, exist_ok=exist_ok)

        with (patch.object(Path, "mkdir", new=mkdir_with_competing_output),
              self.assertRaises(selection_capture.CaptureOutputExists)):
            _ = selection_capture.capture(root, root, "fixture", late)
        self.assertTrue(late.is_dir())
        self.assertEqual(list(late.iterdir()), [])
        self.assertEqual(list(selection_capture.capture_output_parent().iterdir()), [])

        retry = root / "retry"
        with patch.object(selection_capture, "capture_inputs", side_effect=self.stage):
            with (patch.object(selection_capture, "write_text_atomic", side_effect=OSError("write failed")),
                  self.assertRaisesRegex(OSError, "write failed")):
                _ = selection_capture.capture(root, root, "fixture", retry)
            self.assertFalse(retry.exists())
            _ = selection_capture.capture(root, root, "fixture", retry)
        self.assertEqual((retry / "value").read_text(), "new capture")
        self.assertTrue((retry / "manifest.json").is_file())


@final
class SelectionCaptureTests(StylePlanFixture):
    database: Path = Path()
    proposal: Path = Path()

    @override
    def setUp(self) -> None:
        super().setUp()
        executable = install_unconfigured_board(self.root / "board-bin", self.repo)
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"PATH": str(executable.parent) + os.pathsep + os.environ["PATH"]})))
        reader = executable.with_name("cargo-style")
        _ = reader.write_text("#!/bin/sh\nprintf 'cargo-style 2.3.4 protocol 7\\n'\n")
        reader.chmod(0o755)
        configuration = self.root / "capture-config"
        configuration.mkdir()
        for path in conf.directory().glob("*.conf"):
            _ = shutil.copy2(path, configuration / path.name)
        _ = (configuration / "style_reader.conf").write_text(
            "# one of: python, rust\nreader=python\n# whole: 1..\nprotocol=7\n"
            + "# text:\nminimum_version=2.3.4\n")
        _ = cast(object, self.enterContext(patch.dict(os.environ, {
            "CONF_DIR": str(configuration), "STYLE_READER_BINARY": str(reader)})))
        style_reader.reset_reader_cache()
        self.addCleanup(style_reader.reset_reader_cache)
        _ = self.enterContext(isolated_staff_file(self.root / "staff.json", {}))
        _ = recorded_showrunner("sample", self.repo / "docs/product-production.md")
        _ = recorded_unit("sample-style", "names", self.unit, self.doc,
                          plan="docs/style-production/units/names-unit.md")
        rule = self.rule("capture-name")
        self.sync()
        self.proposal = self.approved_decision(rule, self.sources[:1], "type", name="capture")
        with self.ledger() as ledger:
            self.database = ledger.database

    @override
    def git(self, *args: str, cwd: Path | None = None) -> str:
        result = subprocess.run(["git", "-C", str(cwd or self.repo), *args],
                                env=selection_capture.git_environment(), capture_output=True, text=True, check=True)
        return result.stdout.strip()

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
        self.assertEqual(manifest["tools"]["cargo_style"], "cargo-style 2.3.4 protocol 7")
        self.assertFalse(any(entry["kind"] == "reader_stamp" for entry in manifest["files"]))

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
        with self.subTest(input="installed reader version"):
            reader = Path(os.environ["STYLE_READER_BINARY"])
            _ = reader.write_text("#!/bin/sh\nprintf 'cargo-style 2.3.5 protocol 7\\n'\n")
            changed = selection_capture.replay(folder, style_config.ReaderSource.PYTHON, self.root)
            self.assertEqual(changed["result_digest"], replayed["result_digest"])
            self.assertEqual(changed["external_changes"], [{"source": "cargo-style --version",
                "expected": "cargo-style 2.3.4 protocol 7", "actual": "cargo-style 2.3.5 protocol 7"}])

    def test_replays_share_one_digest_and_report_unconfigured_input_changes(self) -> None:
        before = self.source_rows()
        folder = self.root / "capture"
        manifest = selection_capture.capture(self.repo, self.database, "names", folder)
        self.assertEqual(manifest["format"], 2)
        first = selection_capture.replay(folder, style_config.ReaderSource.PYTHON, self.root)
        second = selection_capture.replay(folder, style_config.ReaderSource.PYTHON, self.root)
        self.assertEqual(first["result_digest"], second["result_digest"])
        self.assertFalse(first["external_changes"])
        self.assertFalse(second["external_changes"])
        with self.subTest(input="format-1 capture"):
            manifest_path = folder / "manifest.json"
            original_manifest = manifest_path.read_text()
            legacy: dict[str, object] = {**manifest, "format": 1, "tools": {"reader_source": "old-source-tree"}}
            _ = manifest_path.write_text(json.dumps(legacy))
            try:
                for operation in (selection_capture.replay, selection_capture.measure_capture):
                    with self.assertRaisesRegex(selection_capture.UnsupportedCaptureFormat,
                                                "capture format 1.*capture again"):
                        _ = operation(folder, style_config.ReaderSource.PYTHON, self.root)
                for command in ("replay", "measure"):
                    result = subprocess.run(
                        [sys.executable, "-m", "scripts.style.selection_capture", command,
                         "--folder", str(folder), "--workspace", str(self.root), "--reader", "python"],
                        capture_output=True, text=True, check=False)
                    self.assertEqual(result.returncode, 2)
                    self.assertIn("capture format 1 is not supported; capture again", result.stderr)
                    self.assertEqual(len(result.stderr.splitlines()), 1)
            finally:
                _ = manifest_path.write_text(original_manifest)
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
                # GIT_CONFIG_GLOBAL makes Git skip the XDG file. The gate sets this variable.
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
