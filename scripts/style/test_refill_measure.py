"""Measure real fixture refills while the source ledger stays unchanged."""

from contextlib import closing
import os
from pathlib import Path
import socket
import shutil
import sqlite3
import subprocess
from typing import cast, final, override
from unittest.mock import patch

from scripts.production.staff_test_support import isolated_staff_file, recorded_showrunner, recorded_unit
from scripts.style import git_process, refill_measure, style_config, style_ledger
from scripts.style.style_plan_test_support import StylePlanFixture, install_unconfigured_board


@final
class RefillMeasureTests(StylePlanFixture):
    @override
    def setUp(self) -> None:
        super().setUp()
        executable = install_unconfigured_board(self.root / "board-bin", self.repo)
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"PATH": str(executable.parent) + os.pathsep + os.environ["PATH"]})))
        _ = self.enterContext(isolated_staff_file(self.root / "staff.json", {}))
        _ = recorded_showrunner("sample", self.repo / "docs/product-production.md")
        _ = recorded_unit("sample-style", "names", self.unit, self.doc,
                          plan="docs/style-production/units/names-unit.md")

    def test_cold_and_warm_refills_preserve_source_and_report_rows_bytes_and_equal_results(self) -> None:
        measurements = refill_measure.StageMeasurements()
        with measurements.installed(True):
            _ = subprocess.run(["git", "-C", str(self.repo), "rev-parse", "HEAD"], capture_output=True, check=True)
        self.assertGreater(measurements.seconds["git_subprocesses"], 0)
        with measurements.installed(True):
            _ = git_process.run_git(("git", "-C", str(self.repo), "rev-parse", "HEAD"))
        self.assertEqual(measurements.counters["git_subprocesses"], 2)
        self.assertEqual(measurements.calls["git_subprocesses"], 2)
        rule = self.rule("measure-name")
        self.sync()
        _ = self.approved_decision(rule, self.sources[:1], "type", name="measure")
        _ = self.candidates()
        with self.ledger() as ledger:
            database = ledger.database
        style_ledger.record_refill(self.repo, "names", "idle")
        reset = self.root / "reset.sqlite"
        refill_measure.backup_ledger(database, reset)
        kept = ("refill_tables", "refill_rows", "refill_results")
        with closing(sqlite3.connect(reset)) as connection:
            original_rows = {table: tuple(connection.execute(f'SELECT * FROM "{table}" ORDER BY 1,2')) for table in kept}
            for table, rows in original_rows.items():
                self.assertTrue(rows, table)
            before_reset = refill_measure.stored_rows(reset)
        removed = refill_measure.clear_derived(reset)
        self.assertTrue(any(cast(dict[str, int], before_reset[table])["rows"] for table in removed))
        with closing(sqlite3.connect(reset)) as connection:
            for table in kept:
                self.assertEqual(tuple(connection.execute(f'SELECT * FROM "{table}" ORDER BY 1,2')), original_rows[table])
            for table in removed:
                self.assertEqual(connection.execute(f'SELECT count(*) FROM "{table}"').fetchone(), (0,), table)
        with closing(sqlite3.connect(database)) as source:
            original = tuple(source.iterdump())
        config_folder = self.root / "measure-config"
        config_folder.mkdir()
        current = Path(os.environ.get("CONF_DIR", str(Path(__file__).resolve().parents[2] / "config")))
        for path in current.glob("*.conf"):
            _ = shutil.copy2(path, config_folder / path.name)
        self.enterContext(patch.dict(os.environ, {"CONF_DIR": str(config_folder)}))
        config = config_folder / "style_reader.conf"
        original_switch = config.read_text()
        reports: list[dict[str, object]] = []
        try:
            for source in (original_switch,
                           f"reader = rust\nreader.{socket.gethostname().split('.', 1)[0]} = rust\n"):
                with self.subTest(configuration=source):
                    _ = config.write_text(source)
                    report = refill_measure.measure(self.repo, database, style_config.ReaderSource.PYTHON, "names", self.root, stages=True)
                    reports.append(report)
                    for name in ("cold", "warm"):
                        run = cast(dict[str, object], report[name])
                        self.assertEqual(run["reader"], style_config.ReaderSource.PYTHON.value)
        finally:
            _ = config.write_text(original_switch)
        report = reports[-1]
        with closing(sqlite3.connect(database)) as source:
            self.assertEqual(tuple(source.iterdump()), original)
        cold = cast(dict[str, object], report["cold"])
        warm = cast(dict[str, object], report["warm"])
        self.assertTrue(report["same_result"])
        self.assertEqual(cold["result_digest"], warm["result_digest"])
        cleared = cast(tuple[str, ...], report["cleared_tables"])
        self.assertIn("refill_inputs", cleared)
        self.assertIn("listed_sites", cleared)
        for table in kept:
            self.assertNotIn(table, cleared)
        for run in (cold, warm):
            self.assertGreater(cast(float, run["wall_seconds"]), 0)
            self.assertGreater(cast(int, run["peak_rss_bytes"]), 0)
            self.assertGreaterEqual(cast(int, run["write_bytes"]), 0)
            stages = cast(dict[str, object], run["stages"])
            self.assertGreater(cast(dict[str, float], stages["seconds"])["git_subprocesses"], 0)
            counters = cast(dict[str, int], stages["counters"])
            self.assertGreater(counters["findings"], 0)
            self.assertGreater(counters["git_subprocesses"], 0)
            saved = cast(dict[str, dict[str, object]], run["stored"])
            for table in kept:
                self.assertGreaterEqual(cast(int, saved[table]["rows"]), len(original_rows[table]), table)
            for table in ("refill_inputs", "refill_objects"):
                entry = saved[table]
                kinds = cast(dict[str, dict[str, int]], entry["kinds"])
                self.assertEqual(entry["rows"], sum(kind["rows"] for kind in kinds.values()))
                self.assertEqual(entry["payload_bytes"], sum(kind["payload_bytes"] for kind in kinds.values()))
        self.assertGreater(cast(dict[str, dict[str, int]], cold["stored"])["refill_cells"]["rows"], 0)
