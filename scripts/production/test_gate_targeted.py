"""Targeted tests select affected modules through the shared real runner."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from typing import ClassVar, cast, override
from unittest import mock

from scripts.build_queue import admission_records
from scripts.copies import copies
from scripts.production import gate
from scripts.build_queue.admission_jobs import StepJob
from scripts.lib import build_folders
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.production.affected_modules import NoCertifiedBase
from scripts.production.gate_test_support import MergeGateFixture, SUITE_MODULE, gate_suites
from scripts.lint.memory_test_support import write_config_fixture
from scripts.test_times.gate_record_test_support import gate_run


class TargetedTestTests(MergeGateFixture):
    suite_mode: ClassVar[gate.SuiteMode] = gate.SuiteMode.TARGETED

    @override
    def setUp(self) -> None:
        super().setUp()
        self.environment["BUILDLOG_PREPARED_COPY"] = str(self.checkout)
        _ = self.enterContext(mock.patch.object(copies, "take_prepared_gate_handoff", return_value=True))
        _ = self.enterContext(mock.patch.object(gate, "checked_gate_target", return_value="natedev"))
        # The fixture's main branch has no full gate pass.
        _ = write_config_fixture(Path(self.environment["CONF_DIR"]) / "gate.conf",
                                 "landing_branches=fixture-landing\nscan_roots=\n")

    def test_targeted_runner_holds_and_removes_its_test_folder(self) -> None:
        allocated: list[Path] = []

        def run(args: gate.GateArguments, mode: gate.SuiteMode, target: gate.GateTarget) -> int:
            self.assertIs(mode, gate.SuiteMode.TARGETED)
            self.assertEqual(target, "natedev")
            self.assertEqual(args.gate_job, StepJob("test-affected"))
            path = Path(os.environ["TMPDIR"])
            allocated.append(path)
            self.assertEqual(build_folders.build_folder_state(path), "held")
            _ = (path / "unfinished-output").write_text("temporary output")
            return 7

        with mock.patch.dict(os.environ, {**self.environment, "TMPDIR": str(self.root)}):
            with mock.patch.object(gate, "run_tests_only", side_effect=run):
                self.assertEqual(gate.main(["--affected", "--repo", str(self.checkout)]), 7)
            self.assertEqual(os.environ["TMPDIR"], str(self.root))
        path, = allocated
        self.assertFalse(path.exists())
        self.assertFalse(path.with_name(path.name + ".lease.json").exists())
        self.assertFalse(path.with_name(path.name + ".lock").exists())

        with self.subTest(entry="script"):
            startup = self.root / "startup"
            self.write(startup / "sitecustomize.py", '''import argparse
import sys

original_exit = argparse.ArgumentParser.exit

def checked_exit(parser, status=0, message=None):
    from scripts.production import gate, merge_gate
    print("gate module identity:", gate is merge_gate is sys.modules["__main__"])
    original_exit(parser, status, message)

argparse.ArgumentParser.exit = checked_exit
''')
            environment = {**self.environment, "PYTHONPATH": os.pathsep.join(
                (str(startup), str(Path(__file__).resolve().parents[2])))}
            result = subprocess.run([sys.executable, str(gate.__file__), "--help"],
                                    cwd=self.checkout, env=environment, capture_output=True,
                                    text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("gate module identity: True", result.stdout)
            self.assertIn("--affected", result.stdout)

    def test_a_targeted_run_with_no_session_names_the_worktree_owner(self) -> None:
        # targeted_test under systemd, or with no session, still names who started its queue requests.
        observed: list[dict[str, str]] = []

        def suites(_repo: Path, *_arguments: object, **_keywords: object) -> gate.GateRun:
            observed.append({name: os.environ[name] for name in admission_records.OWNER_VARIABLES
                             if os.environ.get(name)})
            return gate.GateRun((), 0, 0, tests_reused=True)

        self.environment.update(dict.fromkeys(admission_records.OWNER_VARIABLES, ""))
        # The test fixes the process family to one that names no owner. A terminal or login session family, such
        # as a guest test runner, names the user, and then the worktree owner would not apply.
        unnamed = admission_records.OwnerNotGiven("script · test")
        with mock.patch.dict(os.environ, dict.fromkeys(admission_records.OWNER_VARIABLES, "")), \
             mock.patch.object(admission_records, "family_owner", return_value=unnamed), \
             mock.patch.object(gate, "run_suites", side_effect=suites):
            result = self.run_gate()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(observed, [{"CLAUDE_SESSION_NAME": "verify"}])

    def test_a_changed_suite_set_cannot_reuse_or_certify_an_old_full_pass(self) -> None:
        first = self.run_gate()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        original, = self.pass_records()
        self.write(self.checkout / "scripts" / "added_folder" / "test_added.py", SUITE_MODULE)
        _ = self.commit(self.checkout, "new test folder")
        tree = gate.clean_test_tree(self.checkout)
        folders = sorted([*self.suites, "added_folder"])
        ledger = gate.PassedTreeLedger(self.checkout, "tests-only", tree, folders)
        with mock.patch.dict(os.environ, self.environment):
            # The old full pass cannot certify an ancestor after a folder is added.
            self.assertIsInstance(ledger.certified_base(), NoCertifiedBase)
            # A pass from the old gate could have this same tree but omit its new folder.
            original["tree"] = self.git(self.checkout, "rev-parse", "HEAD^{tree}")
            self.write(ledger.path, json.dumps(original) + "\n")
            self.assertFalse(ledger.reuse())
            self.assertIsInstance(ledger.certified_base(), NoCertifiedBase)
        self.clear_gate_events()
        result = self.run_gate()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("tests reused from", result.stdout)
        self.assertIn("full suite: no certified base", result.stdout)
        starts = [(row["suite"], row["module"]) for row in self.module_events() if row["event"] == "start"]
        self.assertCountEqual(starts, [("hooks", "test_first.py"), ("added_folder", "test_added.py")])
        self.assertEqual(self.pass_records()[-1]["suites"], folders)

    @gate_suites("lib", "hooks", "message")
    def test_affected_modules_are_reported_run_and_recorded_without_certifying_a_full_pass(self) -> None:
        subject, base = self.prepare_affected_subject()
        first = self.run_gate()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.write(subject, "VALUE = 2\n")
        _ = self.commit(self.checkout, "subject changed")
        for _attempt in range(2):
            self.clear_gate_events()
            result = self.run_gate()
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            starts = [(row["suite"], row["module"]) for row in self.module_events() if row["event"] == "start"]
            self.assertEqual(starts, [("hooks", "test_first.py")])
            self.assertIn(f"affected: 1 of {self.module_count} modules (base {base[:12]}, ", result.stdout)
            self.assertRegex(result.stdout, r"\), 1 changed files, selected in [0-9]+\.[0-9] s\nhooks/test_first\.py\n")
            self.assertIn("message: skipped (not affected)", result.stdout)
            self.assertNotIn("tests reused from", result.stdout)
        self.assertEqual([record["kind"] for record in self.pass_records()], ["full", "affected", "affected"])
        self.assertEqual(self.gate_run_records()[-1]["step"], "test-affected")

    @gate_suites("lib", "hooks")
    def test_a_config_change_selects_by_the_full_gate_records_of_the_callers_default_build_log(self) -> None:
        first = self.run_gate()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        # The caller sets no BUILDLOG_DIR, so its build log is the default one under HOME.
        caller_log = Path(self.environment.pop("BUILDLOG_DIR"))
        default_log = Path(self.environment["HOME"]) / ".local/state/buildlog"
        default_log.parent.mkdir(parents=True, exist_ok=True)
        default_log.symlink_to(caller_log, target_is_directory=True)
        # The newest full gate recorded that only lib/test_second.py reads the config file.
        modules: list[dict[str, object]] = []
        for path in sorted((self.checkout / "scripts").glob("*/test_*.py")):
            config_reads: list[str] = ["fixture_settings"] if path.name == "test_second.py" else []
            modules.append({"suite": path.parent.name, "module": path.name, "attempt": 1, "config_reads": config_reads})
        reads: dict[str, object] = {
            **gate_run(), "kind": "gate_run", "v": 1, "id": "fixture-config-reads", "host": "fixture-host",
            "started_at": "2100-01-01T00:00:00.000Z", "selection": "full", "aborted": False, "modules": modules}
        self.write(caller_log / "fixture-host/config-reads.jsonl", json.dumps(reads) + "\n")
        self.write(self.checkout / "config/fixture_settings.conf", "value=changed\n")
        _ = self.commit(self.checkout, "config changed")
        self.clear_gate_events()
        (self.state / "buildlogs.jsonl").unlink()
        result = self.run_gate()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"affected: 1 of {self.module_count} modules", result.stdout)
        starts = [(row["suite"], row["module"]) for row in self.module_events() if row["event"] == "start"]
        self.assertEqual(starts, [("lib", "test_second.py")])
        # The module job writes its own private build log, not the caller's.
        line, = (self.state / "buildlogs.jsonl").read_text().splitlines()
        log = cast(dict[str, str], json.loads(line))
        self.assertTrue(Path(log["buildlog"]).is_relative_to(log["tmpdir"]), log)
        self.assertFalse(Path(log["buildlog"]).is_relative_to(caller_log), log)
        self.assertEqual(list(caller_log.rglob("fixture-log")), [])
        # The gate writes its own record to the caller's build log.
        rows = [cast(dict[str, object], json.loads(line)) for path in caller_log.glob("*/*.jsonl")
                for line in path.read_text().splitlines()]
        self.assertIn(("test-affected", "affected"), [(row.get("step"), row.get("selection")) for row in rows])

    @gate_suites("lib", "hooks", "message")
    def test_a_nightly_red_marker_keeps_selection_by_the_four_rules(self) -> None:
        subject = self.checkout / "scripts/hooks/affected_subject.py"
        self.write(subject, "VALUE = 1\n")
        self.write(self.checkout / "scripts/hooks/test_first.py", SUITE_MODULE + "\n# affected_subject\n")
        self.write(self.checkout / "scripts/lib/test_first.py", "import scripts.hooks.affected_subject\n" + SUITE_MODULE)
        self.write(self.checkout / "scripts/message/test_first.py", 'SUBJECT = "assets/changed.txt"\n' + SUITE_MODULE)
        self.write(self.checkout / "scripts/message/test_unrelated.py", SUITE_MODULE)
        self.write(self.checkout / "assets/changed.txt", "before\n")
        base = self.commit(self.checkout, "four selection routes")
        first = self.run_gate()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        marker, notice = self.write_nightly_red_marker()
        self.write(subject, "VALUE = 2\n")
        self.write(self.checkout / "assets/changed.txt", "after\n")
        self.write(self.checkout / "scripts/lib/test_second.py", SUITE_MODULE + "\n# Changed test module.\n")
        _ = self.commit(self.checkout, "change each selection input")
        self.clear_gate_events()
        result = self.run_gate()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        starts = [(row["suite"], row["module"]) for row in self.module_events() if row["event"] == "start"]
        self.assertCountEqual(starts, [("hooks", "test_first.py"), ("lib", "test_first.py"),
                                       ("lib", "test_second.py"), ("message", "test_first.py")])
        self.assertIn(f"affected: 4 of 5 modules (base {base[:12]}, ", result.stdout)
        self.assertNotIn("full suite:", result.stdout)
        self.assertEqual(marker.read_text(), notice)
        self.assertEqual(self.pass_records()[-1]["kind"], "affected")

    def test_a_nightly_red_marker_allows_reuse_and_stays_after_an_explicit_full_test(self) -> None:
        first = self.run_gate()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        marker, notice = self.write_nightly_red_marker()
        self.clear_gate_events()
        reused = self.run_gate()
        self.assertEqual(reused.returncode, 0, reused.stdout + reused.stderr)
        self.assertIn("tests reused from", reused.stdout)
        self.assertEqual(self.module_events(), [])
        self.assertEqual(marker.read_text(), notice)
        self.assertEqual(len(self.pass_records()), 1)
        self.assertEqual(self.gate_run_records()[-1]["selection"], "reused")
        explicit = self.run_gate("--full")
        self.assertEqual(explicit.returncode, 0, explicit.stdout + explicit.stderr)
        self.assert_all_modules_started()
        self.assertIn("full suite: --full", explicit.stdout)
        self.assertEqual(marker.read_text(), notice)

    @gate_suites("lib", "hooks", "message")
    def test_a_dirty_path_selects_affected_modules_without_reuse_or_a_pass_record(self) -> None:
        subject, base = self.prepare_affected_subject()
        first = self.run_gate()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        records = self.pass_records()
        self.write(subject, "VALUE = 2\n")
        self.clear_gate_events()
        result = self.run_gate()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        starts = [(row["suite"], row["module"]) for row in self.module_events() if row["event"] == "start"]
        self.assertEqual(starts, [("hooks", "test_first.py")])
        self.assertIn(f"affected: 1 of {self.module_count} modules (base {base[:12]}, ", result.stdout)
        self.assertNotIn("full suite:", result.stdout)
        self.assertNotIn("tests reused from", result.stdout)
        self.assertEqual(self.pass_records(), records)
        record = self.gate_run_records()[-1]
        self.assertEqual(record["selection"], "affected")
        self.assertEqual(record["dirty_paths"], ["scripts/hooks/affected_subject.py"])
        self.assertEqual(record["dirty_path_count"], 1)

    def test_unrelated_change_can_pass_with_zero_selected_modules(self) -> None:
        first = self.run_gate()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.write(self.checkout / "subject.txt", "unrelated document\n")
        _ = self.commit(self.checkout, "documentation changed")
        self.clear_gate_events()
        result = self.run_gate()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"affected: 0 of {self.module_count} modules", result.stdout)
        self.assertEqual(self.module_events(), [])
        self.assertEqual(self.pass_records()[-1]["kind"], "affected")
