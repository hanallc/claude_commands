"""Measure real fixture refills while the source ledger stays unchanged."""

from contextlib import closing
import json
import os
from pathlib import Path
import socket
import shutil
import sqlite3
import sys
from typing import cast, final, override
from unittest.mock import patch

from scripts.production.staff_test_support import isolated_staff_file, recorded_showrunner, recorded_unit
from scripts.style import refill_measure, style_config
from scripts.style.style_plan_test_support import StylePlanFixture


@final
class RefillMeasureTests(StylePlanFixture):
    @override
    def setUp(self) -> None:
        super().setUp()
        _ = self.enterContext(isolated_staff_file(self.root / "staff.json", {}))
        _ = recorded_showrunner("sample", self.repo / "docs/product-production.md")
        _ = recorded_unit("sample-style", "names", self.unit, self.doc,
                          plan="docs/style-production/units/names-unit.md")

    def test_cold_and_warm_refills_preserve_source_and_report_rows_bytes_and_equal_results(self) -> None:
        bin_dir = self.root / "measure-bin"
        bin_dir.mkdir()
        fake = bin_dir / "cargo-berth"
        envelope: dict[str, object] = {
            "output_contract_version": 6, "verb": "board", "status": "unconfigured", "exit_code": 4,
            "reservations": [], "blocked_by": [],
            "message": f"this repository has no cargo-berth configuration at {self.repo}/.claude/config/berth.toml; "
                       + "run `cargo-berth init` to create it",
            "presentation": {"kind": "rendered_blocks", "blocks": []},
            "payload": {"kind": "no_facts", "alerts": []},
        }
        _ = fake.write_text(f"#!{sys.executable}\nimport sys\nprint({json.dumps(envelope)!r})\nsys.exit(4)\n")
        fake.chmod(0o755)
        self.enterContext(patch.dict(os.environ, {"PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"}))
        self.assertEqual(shutil.which("cargo-berth"), str(fake))
        rule = self.rule("measure-name")
        self.sync()
        _ = self.approved_decision(rule, self.sources[:1], "type", name="measure")
        with self.ledger() as ledger:
            database = ledger.database
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
                    report = refill_measure.measure(self.repo, database, style_config.ReaderSource.PYTHON, "names", self.root)
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
        for run in (cold, warm):
            self.assertGreater(cast(float, run["wall_seconds"]), 0)
            self.assertGreater(cast(int, run["peak_rss_bytes"]), 0)
            self.assertGreaterEqual(cast(int, run["write_bytes"]), 0)
            saved = cast(dict[str, dict[str, object]], run["stored"])
            for table in ("refill_inputs", "refill_objects"):
                entry = saved[table]
                kinds = cast(dict[str, dict[str, int]], entry["kinds"])
                self.assertEqual(entry["rows"], sum(kind["rows"] for kind in kinds.values()))
                self.assertEqual(entry["payload_bytes"], sum(kind["payload_bytes"] for kind in kinds.values()))
        self.assertGreater(cast(dict[str, dict[str, int]], cold["stored"])["refill_cells"]["rows"], 0)
