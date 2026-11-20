"""Merge gate suite and change review contracts."""

from __future__ import annotations

from contextlib import AbstractContextManager, redirect_stdout
import fcntl
import io
import json
import os
import unittest
from pathlib import Path
import re
from typing import cast, final, override
from unittest import mock

from scripts.lib import scratch
from scripts.production import change_review, merge_gate, staff
from scripts.production.gate_modules import GateRefused
from scripts.production.merge_gate_test_support import FIXTURE_FOLDERS, MergeGateFixture, SUITE_MODULE, gate_suites
from scripts.production.staff_test_support import isolated_source_readers, source_environment
from scripts.production.test_change_review import ProducerFixture


@final
class MergeGateTests(MergeGateFixture):
    def test_checkout_holds_a_shared_copy_lock_through_suites_and_releases_it_on_error(self) -> None:
        copies = self.root / "copies"
        checkout = copies / "show-maker" / "21-gate"
        checkout.parent.mkdir(parents=True)
        _ = self.git(self.checkout, "clone", str(self.checkout), str(checkout))
        self.environment["SCRATCH_COPIES_ROOT"] = str(copies)
        observed: list[Path] = []

        def suites(repo: Path, *_arguments: object, **_keywords: object) -> merge_gate.GateRun:
            self.assertEqual(repo, checkout)
            with scratch.busy_path(repo).open("a") as contender:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(contender, fcntl.LOCK_SH | fcntl.LOCK_NB)
            observed.append(repo)
            raise GateRefused("injected suite stop")

        with mock.patch.object(merge_gate, "run_suites", side_effect=suites):
            result = self.run_gate("--tests-only", "--checkout", str(checkout))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("injected suite stop", result.stderr)
        self.assertEqual(observed, [checkout])
        excludes = (checkout / ".git/info/exclude").read_text().splitlines()
        self.assertEqual(excludes.count("/.claude-busy"), 1)
        self.assertEqual(excludes.count("/.scratch.json"), 1)
        with scratch.busy_path(checkout).open("a") as contender:
            fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)

    @gate_suites("folder_from_the_checkout")
    def test_a_folder_without_a_registry_entry_runs_and_is_reported(self) -> None:
        result = self.run_gate("--tests-only")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_all_modules_started()
        self.assertRegex(result.stdout, r"(?m)^folder_from_the_checkout: OK \d+\.\d+s$")
        self.assertEqual(self.pass_records()[0]["suites"], ["folder_from_the_checkout"])

    def test_module_folders_are_removed_after_success_and_preparation_error(self) -> None:
        prepare = merge_gate.test_module_environment
        for fails in (False, True):
            with self.subTest(raises=fails):
                folders: list[Path] = []

                def prepare_module(root: Path, environment: dict[str, str]) -> dict[str, str]:
                    folders.append(root)
                    self.assertTrue(root.is_dir())
                    if fails:
                        raise RuntimeError("module preparation failed")
                    prepared = prepare(root, {**environment, "HERDR_BIN": "/host/herdr"})
                    self.assertEqual(prepared["HERDR_BIN"], "/nonexistent/herdr")
                    return prepared

                with mock.patch.object(merge_gate, "test_module_environment", side_effect=prepare_module):
                    if fails:
                        with self.assertRaisesRegex(RuntimeError, "module preparation failed"):
                            _ = self.run_gate("--tests-only", "--full", type_dirs=())
                    else:
                        result = self.run_gate("--tests-only", "--full", type_dirs=())
                        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertTrue(folders)
                self.assertTrue(all(not folder.exists() for folder in folders), folders)

    def test_nested_modules_run_and_retry_from_their_own_folder_under_the_top_suite(self) -> None:
        nested = self.checkout / "scripts" / "hooks" / "tests" / "test_first.py"
        self.write(nested, SUITE_MODULE)
        _ = self.commit(self.checkout, "nested test fixture")
        self.write(self.state / "fail-first-hooks", "fail")
        result = self.run_gate("--tests-only", "--full")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        starts = [(row["suite"], row["module"]) for row in self.module_events() if row["event"] == "start"]
        self.assertCountEqual(starts, [("hooks", "test_first.py"), ("hooks", "tests/test_first.py")] * 2)
        self.assertIn("Rerun alone: hooks/tests/test_first.py passed", result.stdout)
        self.assertIn("Rerun alone: hooks/test_first.py passed", result.stdout)
        self.assertRegex(result.stdout, r"(?m)^hooks: OK \d+\.\d+s$")
        self.assertNotRegex(result.stdout, r"(?m)^tests:")
        launches = [call for call in self.calls() if call["tool"] == "suite-python"]
        runner = str(self.checkout / "scripts/test_times/module_run.py")
        self.assertCountEqual([call["args"] for call in launches],
                              [[runner, str(folder), nested.name]
                               for folder in (nested.parent, nested.parent.parent)] * 2)
        self.assertTrue(all(call["cwd"] == str(self.checkout) for call in launches))
        timings = merge_gate.read_measures(self.state / "runner" / "module_seconds.json")
        self.assertEqual(set(timings), {"hooks/test_first.py", "hooks/tests/test_first.py"})
        self.assertEqual(self.pass_records()[0]["suites"], ["hooks"])

    def test_folders_without_test_modules_and_cached_files_are_not_suites(self) -> None:
        scripts = self.checkout / "scripts"
        self.write(scripts / "empty" / "helper.py", "raise AssertionError('helper was run')\n")
        self.write(scripts / "cached" / "__pycache__" / "test_cached.py",
                   "raise AssertionError('cached test was run')\n")
        (scripts / "empty" / "test_directory.py").mkdir()
        _ = self.commit(self.checkout, "non-test folder fixture")
        result = self.run_gate("--tests-only")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_all_modules_started()
        self.assertNotRegex(result.stdout, r"(?m)^(?:empty|cached|__pycache__):")
        self.assertEqual(self.pass_records()[0]["suites"], ["hooks"])

    @gate_suites()
    def test_an_empty_test_list_is_refused_before_any_job_runs(self) -> None:
        result = self.run_gate("--tests-only")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("the test list is empty", result.stderr)
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.module_events(), [])

    def test_missing_type_dir_is_refused_before_any_suite_runs(self) -> None:
        result = self.run_gate("--type-dirs", "accounts")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("type dirs not found", result.stderr)
        self.assertIn("accounts", result.stderr)
        self.assertEqual(self.module_events(), [])
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.base)

    @gate_suites(*FIXTURE_FOLDERS)
    def test_full_gate_runs_all_modules_and_checks_types(self) -> None:
        self.write(self.state / "types-barrier", "overlap")
        self.write(self.state / "types-hold", "hold")
        memory = merge_gate.process_memory_mib

        def sample_and_release(roots: set[int]) -> dict[int, float]:
            measured = memory(roots)
            for call in self.calls():
                if call["tool"] == "basedpyright" and measured.get(call["pid"], 0) > 0:
                    (self.state / "types-sampled").touch()
            return measured

        with mock.patch.object(merge_gate, "process_memory_mib", side_effect=sample_and_release):
            result = self.run_gate("--tests-only")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.base)
        self.assert_not_pushed()
        starts = [row for row in self.module_events() if row["event"] == "start"]
        self.assertCountEqual([(row["suite"], row["module"]) for row in starts],
                              [(suite, "test_first.py") for suite in self.suites] + ([("lib", "test_second.py")] if "lib" in self.suites else []))
        self.assertEqual(len({row["pid"] for row in starts}), self.module_count)
        self.assertEqual(len({row["tmpdir"] for row in starts}), self.module_count)
        self.assertTrue(all(not Path(row["tmpdir"]).exists() for row in starts))
        type_calls = [call for call in self.calls() if call["tool"] == "basedpyright"]
        self.assertEqual(len(type_calls), 1)
        venv = self.state / "runner" / "typecheck-venv"
        self.assertEqual(type_calls[0]["args"], ["--pythonpath", str(venv / "bin" / "python"), "scripts/production"])
        peaks = cast(dict[str, float], json.loads((self.state / "runner" / "types_peak_mib.json").read_text()))
        self.assertEqual(list(peaks), ["scripts/production"])
        self.assertGreater(peaks["scripts/production"], 0)
        self.assertEqual(type_calls[0]["cwd"], str(self.checkout))
        for suite in self.suites:
            self.assertRegex(result.stdout, rf"(?m)^{suite}: OK \d+\.\d+s$")
        peak = re.search(r"peak memory: ([\d.]+) GiB", result.stdout)
        self.assertIsNotNone(peak, result.stdout)
        assert peak is not None
        self.assertGreater(float(peak.group(1)), 0)
        self.assertRegex(result.stdout, r"(?m)^Largest module peak: [\d.]+ GiB \(sampled memory\); module: (?:\w+/test_\w+\.py|\(not sampled\))$")
        launches = [call for call in self.calls() if call["tool"] == "suite-python"]
        self.assertEqual(len(launches), self.module_count)
        runner = str(self.checkout / "scripts/test_times/module_run.py")
        expected = [[runner, str(self.checkout / "scripts" / suite), "test_first.py"] for suite in self.suites]
        if "lib" in self.suites:
            expected.append([runner, str(self.checkout / "scripts/lib"), "test_second.py"])
        self.assertCountEqual([call["args"] for call in launches], expected)
        for call in launches:
            self.assertEqual(call["cwd"], str(self.checkout))
        self.assertEqual(self.git(self.origin, "rev-parse", "main"), self.base)
        self.assertFalse(any(call["tool"] == "ssh" for call in self.calls()))


if __name__ == "__main__":
    _ = unittest.main()


class MergeGateProducerTests(ProducerFixture):
    @override
    def setUp(self) -> None:
        super().setUp()
        environment = source_environment(self.root / "staff-sources")
        _ = self.enterContext(cast(AbstractContextManager[object], mock.patch.dict(os.environ, environment)))
        _ = self.enterContext(isolated_source_readers())

    def test_record_changes_writes_the_checkpoint_units_staff_id(self) -> None:
        merged = self.merge()
        merge_gate.record_changes(self.repo, self.listing, self.doc, "main")
        self.assertEqual(change_review.pending(self.listing)[0]["Producer"], self.unit.id)
        assert isinstance(self.unit.parent, staff.ReportsTo)
        self.assertEqual(change_review.pending(self.listing)[0]["Production"], self.unit.parent.id)

        with self.subTest(producer="recorded before the unit leaves staff"):
            before = self.listing.read_bytes()
            _ = self.listing.unlink()
            record = staff.StaffSnapshot(tuple(row for row in staff.snapshot().rows if row.id != self.unit.id))
            with mock.patch.object(staff, "snapshot", return_value=record):
                merge_gate.record_changes(self.repo, self.listing, self.doc, "main",
                    known_producer=change_review.KnownMergeProducer(merged, self.unit.id))
            self.assertEqual(change_review.pending(self.listing)[0]["Producer"], self.unit.id)
            self.assertEqual(self.listing.read_bytes(), before)

    def test_record_changes_keeps_an_unknown_producer_empty_and_prints_one_named_note(self) -> None:
        record = staff.StaffSnapshot(tuple(row for row in staff.snapshot().rows if row.id != self.unit.id))
        output = io.StringIO()
        with (mock.patch.object(staff, "snapshot", return_value=record), redirect_stdout(output)):
            _ = self.merge()
            merge_gate.record_changes(self.repo, self.listing, self.doc,
                                      "main")
        self.assertEqual(change_review.pending(self.listing)[0]["Producer"], "")
        notes = [line for line in output.getvalue().splitlines() if line.startswith("change review: no staff row")]
        self.assertEqual(len(notes), 1)
        self.assertIn("old-branch", notes[0])
