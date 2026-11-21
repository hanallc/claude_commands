"""Test the gate and its publication of change review rows."""

from __future__ import annotations

import unittest
import fcntl

import io
import hashlib
import json
import os
from pathlib import Path
import shlex
from typing import cast, final, override
from contextlib import AbstractContextManager, redirect_stderr, redirect_stdout
from unittest import mock
from scripts.lib import scratch
from scripts.production import change_review, merge_gate, staff
from scripts.production.gate_modules import GateRefused
from scripts.production.test_change_review import ProducerFixture
from scripts.production.staff_test_support import isolated_source_readers, source_environment
from scripts.production.merge_gate_test_support import MergeGateFixture, SUITE_MODULE, gate_suites


@final
class MergeGateTests(MergeGateFixture):
    def test_gate_requires_an_explicit_mode(self) -> None:
        with redirect_stderr(io.StringIO()) as errors, self.assertRaises(SystemExit) as refusal:
            _ = merge_gate.main(["--checkout", str(self.checkout)])
        self.assertEqual(refusal.exception.code, 2)
        self.assertIn("choose --tests-only, --promote-only, or --nightly", errors.getvalue())
        self.assertEqual(self.module_events(), [])
        self.assert_not_pushed()

    def test_every_gate_form_refuses_a_saved_checkpoint_push(self) -> None:
        _ = self.git(self.checkout, "merge", "--no-ff", "--no-edit", self.checkpoint)
        merged = self.git(self.checkout, "rev-parse", "HEAD")
        key = json.dumps([str(self.checkout.resolve()), "checkpoint", f"origin/integration:{self.checkpoint}"])
        self.environment["MERGE_GATE_PASSED_DIR"] = str(self.state / "gate passes")
        path = self.state / "gate passes" / "push-recovery" / (hashlib.sha256(key.encode()).hexdigest() + ".json")
        record = json.dumps({"base": self.base, "commit": merged, "source_commit": self.checkpoint})
        archive = f"mv -i -- {shlex.quote(str(path))} {shlex.quote(str(path.with_suffix('.resolved')))}"
        forms = (
            ["--tests-only"],
            ["--tests-only", "--module", "hooks/test_first.py"],
            ["--promote-only", "--merge-branch", "integration"],
            ["--nightly", "--merge-branch", "integration", "--unit", "fixture"],
            ["nightly-assigned", "--unit", "fixture"],
            ["nightly-check"],
        )
        test_gate = mock.Mock(return_value=0)
        other_gate = mock.Mock(side_effect=AssertionError("the gate started work"))
        with (mock.patch.dict(os.environ, self.environment, clear=True),
              mock.patch.multiple(merge_gate, run_tests_only=test_gate, promote=other_gate,
                                  nightly=other_gate, _sweep_nightly_build_folders=other_gate,
                                  assign_nightly_red=other_gate, check_nightly_assignments=other_gate)):
            promotion = merge_gate.promotion_record_path(self.main, "integration")
            promotion_record = json.dumps({"base": self.base, "commit": merged, "source_commit": merged})
            self.write(promotion, promotion_record)
            self.assertEqual(merge_gate.main(["--tests-only"]), 0)
            test_gate.assert_called_once()
            test_gate.reset_mock()
            self.write(path, record)
            for form in forms:
                with self.subTest(form=form), redirect_stderr(io.StringIO()) as errors:
                    self.assertEqual(merge_gate.main(form), 1)
                    line, = errors.getvalue().splitlines()
                    self.assertTrue(line.startswith(
                        f"REFUSED: {path} records an unpublished checkpoint merge from the removed integrate form."))
                    self.assertIn(f"Check whether {merged} reached the production branch.", line)
                    self.assertIn(f"restore the original checkout to {self.base}.", line)
                    self.assertIn(f"`{archive}`", line)
        test_gate.assert_not_called()
        other_gate.assert_not_called()
        self.assertEqual(path.read_text(), record)
        self.assertEqual(promotion.read_text(), promotion_record)
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), merged)
        self.assert_not_pushed()

    def test_checkout_holds_a_shared_copy_lock_through_suites_and_releases_it_on_error(self) -> None:
        copies = self.root / "copies"
        checkout = copies / "show-maker" / "21-gate"
        checkout.parent.mkdir(parents=True)
        _ = self.git(self.checkout, "clone", str(self.checkout), str(checkout))
        self.environment["SCRATCH_COPIES_ROOT"] = str(copies)
        with mock.patch.dict(os.environ, self.environment):
            commit = self.git(checkout, "rev-parse", "HEAD")
            scratch.register_copy(checkout, task="gate:fixture", owner_unit="fixture",
                                  source_path=str(self.checkout), source_commit=commit,
                                  copy_commit=commit, target_mode="none", seed_source="", borrows_from="")
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
                            _ = self.run_gate("--tests-only", "--full")
                    else:
                        result = self.run_gate("--tests-only", "--full")
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


if __name__ == "__main__":
    _ = unittest.main()
