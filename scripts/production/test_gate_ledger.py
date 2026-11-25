"""Gate ledger contracts."""

from __future__ import annotations

import unittest

from datetime import UTC, datetime
import json
import io
import os
from pathlib import Path
import select
import sys
import time
from types import SimpleNamespace
from typing import cast, final, override
from collections.abc import Sequence
from contextlib import ExitStack, redirect_stdout
from unittest import mock
from zoneinfo import ZoneInfo
from scripts.production import gate, land_runs
from scripts.lib import build_folders
from scripts.lib.checkout_test_support import write_stdlib_tool
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.lint.memory_test_support import ADMISSION_CONFIG_FIXTURE
from scripts.production.affected_modules import CertifiedBase, FullSuite
from scripts.buildlog.store import host_name
from scripts.production.gate_test_support import FIXTURE_FOLDERS, GATE_TEST_SETTINGS, MergeGateFixture, gate_suites


# Run the same fixture test in one child. The suite tests cover the full runner.
LEDGER_RUNNER = """import runpy
from pathlib import Path
import sys
import unittest

directory, filename = sys.argv[1:]
sys.path.insert(0, str(Path.cwd()))
case = runpy.run_path(str(Path(directory) / filename))["FixtureTest"]
suite = unittest.defaultTestLoader.loadTestsFromTestCase(case)
raise SystemExit(not unittest.TextTestRunner().run(suite).wasSuccessful())
"""

LEDGER_TYPE_CHECKER = r"""import json
import os
from pathlib import Path
import sys

state = Path(os.environ["GATE_TEST_STATE"])
with (state / "tools.jsonl").open("a") as output:
    output.write(json.dumps({"tool": "basedpyright", "args": sys.argv[1:],
                             "cwd": os.getcwd(), "pid": os.getpid()}) + "\n")
(state / "types-ran").touch()
if (state / "types-red").exists():
    print("scripts/fixture.py:1: error: fixture type error")
    print("1 error, 0 warnings, 0 notes")
    raise SystemExit(1)
print("0 errors, 0 warnings, 0 notes")
"""


LEDGER_SETTINGS = {
    "buildlog": """# whole: 1..
INDEX_SPILL_PAGES=65536
# range: 0..
INDEX_TOUCH_MINUTES=60
""",
    "codex_daemon": "# range: 0.001..\ntimeout_seconds=5\n",
    "gate": GATE_TEST_SETTINGS + """# one of: on, off
nightly_result_push=on
# whole: 1..
nightly_result_modules=8
# whole: 1..
nightly_assign_minutes=30
""",
    "postmaster": """# range: 0.001..
claim_grace_seconds=15
# range: 0.001..
wait_seconds=3
""",
    "scratch": """# whole: 1..
default_life_seconds=86400
# whole: 1..
repo_depth=3
# whole: 1..
suffix_length=6
repo_skip=target node_modules .git
# whole: 1..
record_lock_attempts=3
# whole: 1..
removal_remote_timeout_seconds=30
""",
    "send": """# range: 0.001..
caller_margin_seconds=15
# range: 0.001..
delivery_timeout=40
# range: 0.001..
kill_grace=10
# range: 0.001..
remote_transport_seconds=30
# range: 0.001..
waits_review_seconds=30
""",
    "staff": """# range: 0.001..
OPERATION_LOCK_SECONDS=30
# range: 0.001..
REMOTE_SECONDS=20
PRODUCER_WORKSPACE=producers
PRODUCER_FOLDER_ROOT=~/rust/hanadocs
""",
    "test_times": """# whole: 1..
GATE_FAILURE_LOG_KIB=64
# range: 0..
SLOW_TEST_S=0.5
# whole: 1..
TOP_RUNS=5
""",
    "build_steps": """names.gate=gate
names.integrate=integrate
names.test-all=test all
names.test-modules=test named
names.test-affected=test affected
names.test-nightly=test nightly
names.fast-track=fast track
names.toolchain-trial=toolchain trial
names.promote=promote
names.ci=CI
prose.gate=the gate
prose.build=release build
about.integrate=Merges the checkpoint into the {branch} branch, runs every test module, then pushes {branch}
about.test-all=Runs every test module
about.test-modules=Runs {modules} named test modules
about.test-modules-without-unit=Tests {modules} named modules.
about.test-affected=Runs the test modules that the change affects
about.test-nightly=Tests the {branch} branch each night and records a red result with its owner.
changes.integrate={branch} branch
changes.promote=main branch
changes.default=no branch
""",
}


def pin_ledger_settings(directory: Path, root: Path, gate: Path) -> None:
    """Give the gate and admission caller fixed settings in their private folders."""
    directory.mkdir(parents=True, exist_ok=True)
    for stem, contents in LEDGER_SETTINGS.items():
        _ = (directory / f"{stem}.conf").write_text(contents)
    _ = (directory / "build_admission.conf").write_text(
        ADMISSION_CONFIG_FIXTURE.read_text() + """
# range: 0.125..64
merge_gate_governor_gib=4
# whole: 0..50
peak_history_share_percent=10
# range: 0..
progress_profile_sweep_seconds=0.05
# range: 5e-324..
program_first_peak_gib.test-affected=4
# whole: 1..
peak_hold_seconds.gate=180
# whole: 1..
peak_hold_seconds.test-affected=180
# whole: 1..
agent_swap_window_seconds=600
# range: 1..
head_stall_alert_minutes=600
# whole: 1..86400
remote_start_hold_seconds=1200
# range: 5e-324..
request_slow_seconds=0.25
"""
    )
    _ = (directory / "build_folders.conf").write_text(
        f"""roots.system=/tmp
roots.temporary=${{TMPDIR}}/builds
roots.gate={gate}
roots.build={root / 'build'}
roots.build.{host_name()}={root / 'build'}
roots.workspace={root / 'workspaces'}
roots.session={root / 'session'}
roots.copies={root / 'copies'}
roots.scratch={root / 'scratch'}
# whole: 1..
folders.unused_days=2
# range: 0.001..1
locks.probe_retry_seconds=0.005
# range: 0.001..60
locks.probe_wait_seconds=0.25
# whole: 1..64
names.repository_key_length=8
# whole: 1..
names.suffix_length=8
names.suffix_characters=abcdefghijklmnopqrstuvwxyz0123456789_
patterns.named={{showrunner}}-{{unit}}/{{work}}-{{purpose}}
patterns.temporary={{repository}}/{{owner}}-{{purpose}}-{{suffix}}
patterns.gate={{purpose}}-{{suffix}}
patterns.standing={{repository}}/{{owner}}-gate{{suffix}}
patterns.scratch={{purpose}}-{{suffix}}
patterns.legacy-unique={{purpose}}{{suffix}}
patterns.legacy-stable={{purpose}}
patterns.tool={{purpose}}{{suffix}}
prefixes.merge-gate=merge-gate-
prefixes.merge-gate-nightly=merge-gate-nightly-
prefixes.suite-module={{owner}}-
prefixes.queue-place=queue-place-
prefixes.admit-sock=admit-sock-
prefixes.copies-index=copies-index-
# whole: 0..1
seed.enabled=0
# whole: 1..
seed.max_gib=24
# whole: 0..
seed.min_free_gib=350
"""
    )
    _ = (directory / "copies.conf").write_text(
        f"""remote_registry={root / 'remote-mirrors'}
wait_seconds=0.01
# range: 5e-324..
git_timeout_seconds=10
guard_timeout_seconds=10
guard_choice_limit=32
owner.nightly=nightly
owner.style=style
owner.verify=verify
"""
    )


@final
class LedgerJobCompletion:
    """Wait for a real child exit when the gate has a fixture job running."""

    def __init__(self) -> None:
        self.jobs: list[gate.RunningGateJob] = []
        self.start = gate.start_gate_job

    def observe_start(self, suite: str, path: Path, argv: Sequence[str], repo: Path,
                      environment: dict[str, str], log: Path, *,
                      kind: gate.GateJobKind = gate.GateJobKind.TEST_MODULE) -> gate.RunningGateJob:
        job = self.start(suite, path, argv, repo, environment, log, kind=kind)
        self.jobs.append(job)
        return job

    def wait_for_exit(self, _interval: float) -> None:
        self.jobs = [job for job in self.jobs if job.process.returncode is None]
        if not self.jobs:
            return
        with ExitStack() as descriptors:
            waiter = select.poll()
            for job in self.jobs:
                descriptor = os.pidfd_open(job.process.pid)
                _ = descriptors.callback(os.close, descriptor)
                waiter.register(descriptor, select.POLLIN)
            if not waiter.poll(TEST_HANG_GUARD_SECS * 1000):
                raise TimeoutError("a ledger fixture job did not exit")


def fixture_job_memory(roots: set[int]) -> dict[int, float]:
    return dict.fromkeys(roots, 32.0)


@final
class MergeGateLedgerTests(MergeGateFixture):
    @classmethod
    @override
    def fixture_configuration(cls, directory: Path) -> dict[str, str]:
        environment = super().fixture_configuration(directory)
        pin_ledger_settings(directory, directory, Path("/tmp").resolve())
        return {**environment, "STAFF_CONFIG": str(directory / "staff.conf")}

    @override
    def install_fixture_runner(self) -> None:
        self.write(self.checkout / "scripts/test_times/module_run.py", LEDGER_RUNNER)

    @override
    def bind_fixture(self, root: Path) -> None:
        super().bind_fixture(root)
        self.environment["GATE_PYTHON"] = sys.executable

    @override
    def restore_fixture(self, folders: tuple[str, ...]) -> None:
        super().restore_fixture(folders)
        # Keep the host override keys from the shared config templates out of the test.
        pin_ledger_settings(self.root, self.root, self.root.parent)
        pin_ledger_settings(self.root / "config", self.root, self.root.parent)

    @override
    def build_fixture(self, root: Path) -> None:
        super().build_fixture(root)
        write_stdlib_tool(self.bin / "basedpyright", LEDGER_TYPE_CHECKER)
        venv = Path(self.environment["GATE_STATE_DIR"]) / "typecheck-venv"
        self.write(venv / "bin/python", "")
        self.write(venv / "gate-requirements.txt", "\n".join(gate.TYPECHECK_REQUIREMENTS) + "\n")

    @override
    def setUp(self) -> None:
        super().setUp()
        _ = cast(object, self.enterContext(mock.patch.object(gate, "push_nightly_result")))
        _ = cast(object, self.enterContext(mock.patch.object(gate, "_sweep_nightly_build_folders")))
        # The memory tests measure real processes. The ledger tests need only available capacity.
        _ = self.enterContext(mock.patch.object(gate, "process_memory_mib", new=fixture_job_memory))
        completion = LedgerJobCompletion()
        clock = SimpleNamespace(monotonic=time.monotonic, time=time.time, sleep=completion.wait_for_exit)
        _ = self.enterContext(mock.patch.object(gate, "start_gate_job", new=completion.observe_start))
        _ = self.enterContext(mock.patch.object(gate, "time", new=clock))

    @gate_suites(*FIXTURE_FOLDERS)
    def test_clean_pass_records_tree_commit_host_time_checkout_suites_and_source(self) -> None:
        self.assertTrue(self.fixture_root.is_relative_to(Path("/tmp").resolve()))
        result = self.run_gate("--tests-only")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse((self.state / "build-targets/aws/cloud-gate-launches.jsonl").exists())
        self.assert_all_modules_started()
        record, = self.pass_records()
        self.assertEqual(record["tree"], self.git(self.checkout, "rev-parse", "HEAD^{tree}"))
        self.assertEqual(record["commit"], self.base)
        self.assertEqual(record["host"], host_name())
        self.assertEqual(record["checkout"], str(self.checkout))
        self.assertEqual(record["suites"], sorted(self.suites))
        self.assertEqual(record["source"], "tests-only")
        at = datetime.fromisoformat(cast(str, record["at"]))
        self.assertEqual(at.utcoffset(), UTC.utcoffset(at))

    def test_dirty_and_failed_runs_do_not_record_a_pass(self) -> None:
        for state in ("tracked", "staged", "untracked", "failed"):
            with self.subTest(state=state):
                self.environment["GATE_PASSED_DIR"] = str(self.state / state)
                self.clear_gate_events()
                if state == "tracked":
                    self.write(self.checkout / "subject.txt", "edited\n")
                elif state == "staged":
                    _ = self.git(self.checkout, "add", "subject.txt")
                elif state == "untracked":
                    _ = self.git(self.checkout, "restore", "--staged", "--worktree", "subject.txt")
                    self.write(self.checkout / "new file.txt", "untracked\n")
                else:
                    (self.checkout / "new file.txt").unlink()
                    self.write(self.state / "red-hooks", "red")
                result = self.run_gate("--tests-only")
                self.assertEqual(result.returncode, 1 if state == "failed" else 0, result.stdout + result.stderr)
                self.assertEqual(self.pass_records(), [])
                self.assertTrue(self.module_events())
                if state in {"tracked", "untracked"}:
                    path = "subject.txt" if state == "tracked" else "new file.txt"
                    self.assertEqual(self.gate_run_records()[-1]["dirty_paths"], [path])
                    self.assertIn("Dirty checkout: 1 paths", result.stdout)

    @gate_suites("lib", "hooks")
    def test_full_flag_and_red_marker_force_all_modules_and_green_clears_the_marker(self) -> None:
        first = self.run_gate("--tests-only")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        for trigger in ("--full", "red marker"):
            with self.subTest(trigger=trigger):
                marker = self.write_nightly_red_marker()[0]
                if trigger == "--full":
                    marker.unlink()
                self.clear_gate_events()
                result = self.run_gate("--tests-only", *(["--full"] if trigger == "--full" else []))
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assert_all_modules_started()
                self.assertNotIn("tests reused from", result.stdout)
                reason = ("--full" if trigger == "--full" else
                          "last nightly run was red (red-tree, 2026-10-29T00:00:00+00:00)")
                self.assertIn("full suite: " + reason, result.stdout)
                summary = next(line for line in result.stdout.splitlines() if line.startswith("Gate tests:"))
                self.assertIn("full suite: " + reason, summary)
                self.assertEqual(self.pass_records()[-1]["reason"], reason)
                self.assertFalse(marker.exists())
                self.assertEqual(self.pass_records()[-1]["kind"], "full")

    @gate_suites("lib", "hooks")
    def test_merge_gate_runs_all_modules_on_a_small_change(self) -> None:
        subject, _ = self.prepare_affected_subject()
        first = self.run_gate("--tests-only", "--full")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.write(subject, "VALUE = 2\n")
        _ = self.commit(self.checkout, "subject changed")
        self.clear_gate_events()
        result = self.run_gate("--tests-only")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_all_modules_started()
        record = self.gate_run_records()[-1]
        self.assertEqual(record["step"], "gate")
        self.assertEqual(record["selection"], "full")

    def test_dirty_path_record_keeps_fifty_names_and_the_total(self) -> None:
        paths = [f"dirty-{number:02}.txt" for number in range(51)]
        for path in paths:
            self.write(self.checkout / path, "dirty\n")
        result = self.run_gate("--tests-only")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        record = self.gate_run_records()[-1]
        self.assertEqual(record["dirty_paths"], paths[:50])
        self.assertEqual(record["dirty_path_count"], 51)
        self.assertIn("Dirty checkout: 51 paths", result.stdout)
        self.assert_all_modules_started()

    def test_dirty_path_with_non_utf8_bytes_records_the_run_result(self) -> None:
        path = self.checkout / os.fsdecode(b"dirty-\xff.txt")
        self.write(path, "dirty\n")
        result = self.run_gate("--tests-only")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        record = self.gate_run_records()[-1]
        self.assertEqual(record["dirty_paths"], ["dirty-\ufffd.txt"])
        self.assertEqual(record["dirty_path_count"], 1)
        self.assertEqual(record["status"], 0)
        self.assert_all_modules_started()

    def test_staged_rename_records_the_source_and_destination_paths(self) -> None:
        _ = self.git(self.checkout, "mv", "subject.txt", "renamed subject.txt")
        result = self.run_gate("--tests-only")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        record = self.gate_run_records()[-1]
        self.assertEqual(record["dirty_paths"], ["renamed subject.txt", "subject.txt"])
        self.assertEqual(record["dirty_path_count"], 2)
        self.assert_all_modules_started()

    def test_second_tests_only_run_reuses_without_starting_any_module(self) -> None:
        first = self.run_gate("--tests-only")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.clear_gate_events()
        self.environment["USER_ZONE"] = "America/New_York"
        second = self.run_gate("--tests-only")
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertEqual(self.module_events(), [])
        record, = self.pass_records()
        when = datetime.fromisoformat(cast(str, record["at"])).astimezone(
            ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M:%S %Z")
        self.assertIn(f"tests reused from {cast(str, record['tree'])[:12]} ({when}, tests-only, {self.checkout})", second.stdout)

    def test_checkpoint_reuses_unit_tree_without_running_types(self) -> None:
        first = self.run_gate("--tests-only", "--repo", str(self.unit))
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.clear_gate_events()
        second = self.run_gate()
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertEqual(self.module_events(), [])
        self.assertFalse((self.state / "types-ran").exists())
        self.assertIn("tests reused from", second.stdout)
        self.assertEqual(self.git(self.origin, "rev-parse", "integration"), self.git(self.checkout, "rev-parse", "HEAD"))

    @gate_suites("lib", "hooks")
    def test_checkpoint_full_pass_records_and_tests_only_reuses_it(self) -> None:
        first = self.run_gate()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assert_all_modules_started()
        record, = self.pass_records()
        self.assertEqual(record["source"], "checkpoint")
        self.assertEqual(record["commit"], self.git(self.checkout, "rev-parse", "HEAD"))
        self.clear_gate_events()
        second = self.run_gate("--tests-only")
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertEqual(self.module_events(), [])
        self.assertIn(", checkpoint, ", second.stdout)

    @gate_suites("lib", "hooks")
    def test_dirty_host_or_suite_mismatch_runs_all_modules(self) -> None:
        first = self.run_gate("--tests-only")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        original = self.pass_records()[0]
        path = Path(self.environment["GATE_PASSED_DIR"]) / "passed-trees.jsonl"
        for mismatch in ("host", "suites", "dirty"):
            with self.subTest(mismatch=mismatch):
                record = dict(original)
                if mismatch == "host":
                    record["host"] = "other-host"
                elif mismatch == "suites":
                    record["suites"] = [*self.suites, "other-suite"]
                else:
                    self.write(self.checkout / "subject.txt", "new tree\n")
                self.write(path, json.dumps(record) + "\n")
                self.clear_gate_events()
                result = self.run_gate("--tests-only")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertNotIn("tests reused from", result.stdout)
                self.assert_all_modules_started()

    @gate_suites("lib", "hooks")
    def test_base_search_uses_only_newest_full_same_host_suite_ancestor_and_accepts_legacy_records(self) -> None:
        ledger = gate.PassedTreeLedger(self.checkout, "tests-only", gate.clean_test_tree(self.checkout),
                                            sorted(self.suites))
        with mock.patch.dict(os.environ, self.environment):
            with mock.patch.dict(os.environ, {"HOME": str(self.state / "default-home")}):
                old = Path.home() / ".local/state/merge-gate"
                old.mkdir(parents=True)
                _ = os.environ.pop("GATE_PASSED_DIR", None)
                with self.assertRaises(land_runs.GateStateMoveRequired):
                    _ = ledger.path
                with self.assertRaises(land_runs.GateStateMoveRequired):
                    _ = gate.push_record_path(self.checkout, "checkpoint", "fixture")
                os.environ["GATE_PASSED_DIR"] = str(self.state / "explicit-passes")
                self.assertEqual(ledger.path, self.state / "explicit-passes/passed-trees.jsonl")
                self.assertEqual(gate.push_record_path(self.checkout, "checkpoint", "fixture").parent,
                                 self.state / "explicit-passes/push-recovery")
            ledger.record_pass(FullSuite("no certified base"))
            original = self.pass_records()[0]
            _ = original.pop("kind")  # Older full certifications remain usable.
            _ = original.pop("reason")
            self.write(self.checkout / "subject.txt", "a descendant\n")
            head = self.commit(self.checkout, "descendant")
            detached = self.git(self.checkout, "commit-tree", self.git(self.checkout, "rev-parse", "HEAD^{tree}"),
                                "-m", "not an ancestor")
            records = [original]
            for index, changes in enumerate((
                {"kind": "affected", "commit": head}, {"host": "another-host", "commit": head},
                {"suites": ["hooks"], "commit": head}, {"commit": detached},
            )):
                records.append({**original, "at": f"2099-01-0{index + 1}T00:00:00+00:00", **changes})
            self.write(ledger.path, "".join(json.dumps(record) + "\n" for record in records))
            selected = ledger.certified_base()
            self.assertIsInstance(selected, CertifiedBase)
            assert isinstance(selected, CertifiedBase)
            self.assertEqual(selected.commit, self.base)
            # A newer qualifying certification wins even if appended earlier.
            records.insert(0, {**original, "commit": head, "at": "2099-02-01T00:00:00+00:00"})
            self.write(ledger.path, "".join(json.dumps(record) + "\n" for record in records))
            selected = ledger.certified_base()
            self.assertIsInstance(selected, CertifiedBase)
            assert isinstance(selected, CertifiedBase)
            self.assertEqual(selected.commit, head)

    def test_nightly_without_a_build_cache_sends_the_result_and_releases_its_copy(self) -> None:
        from scripts.production import showrunners

        _ = self.git(self.checkout, "rm", "Cargo.toml")
        tip = self.commit(self.checkout, "repository without a build cache")
        _ = self.git(self.checkout, "push", "origin", "HEAD:integration")
        self.write(self.state / "red-hooks", "red")
        with mock.patch.object(showrunners, "tell", return_value="sent to quiet") as notice:
            result = self.run_gate("--nightly")

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        starts = [(row["suite"], row["module"]) for row in self.module_events() if row["event"] == "start"]
        self.assertCountEqual(starts, [(suite, "test_first.py") for suite in self.suites]
                              + ([("lib", "test_second.py")] if "lib" in self.suites else [])
                              + [("hooks", "test_first.py")])
        type_call, = [call for call in self.calls() if call["tool"] == "basedpyright"]
        checkout = Path(type_call["cwd"])
        self.assertNotEqual(checkout, self.checkout)
        self.assertTrue(checkout.exists())
        self.assertEqual(build_folders.build_folder_state(checkout), "free")
        marker = Path(self.environment["GATE_STATE_DIR"]) / "full-required"
        record = cast(dict[str, object], json.loads(marker.read_text()))
        tree = self.git(self.checkout, "rev-parse", f"{tip}^{{tree}}")
        self.assertEqual(record["tree"], tree)
        self.assertEqual(record["failed_modules"], ["hooks/test_first.py"])
        notice.assert_called_once()
        self.assertEqual(notice.call_args.args[:2], ("quiet", f"merge-gate-nightly:{tree}"))
        message = cast(str, notice.call_args.args[2])
        self.assertIn("hooks/test_first.py", message)
        self.assertNotIn(str(checkout), message)

    def test_nightly_reuses_a_cache_and_marks_and_notifies_only_red(self) -> None:
        _ = self.git(self.unit, "push", "origin", "HEAD:integration")
        self.write(self.checkout / "untracked.txt", "keep the caller's edits\n")
        before_head = self.git(self.checkout, "rev-parse", "HEAD")
        before_status = self.git(self.checkout, "status", "--porcelain")
        before_branches = self.git(self.checkout, "show-ref", "--heads")
        copies: list[Path] = []
        caches: list[Path] = []
        marker = Path(self.environment["GATE_STATE_DIR"]) / "full-required"
        for red in (True, False):
            with self.subTest(red=red):
                def suite_run(repo: Path, type_dirs: tuple[str, ...], *, module_time_limit_s: float,
                              full: bool, **_options: object) -> gate.GateRun:
                    self.assertNotEqual(repo, self.checkout)
                    self.assertTrue((repo / ".git").is_file())
                    self.assertEqual(gate.checked_git(repo, "rev-parse", "HEAD"), self.checkpoint)
                    self.assertEqual(gate.checked_git(repo, "status", "--porcelain"), "")
                    self.assertTrue(full)
                    self.assertGreater(module_time_limit_s, 0)
                    self.assertEqual(type_dirs, ("scripts",))
                    self.write(repo / "subject.txt", "only the copy changes\n")
                    copies.append(repo)
                    caches.append((repo / "target").resolve())
                    return gate.GateRun((gate.GateJobResult("hooks", "test_first.py", int(red), 0, "fixture"),), 0, 0)

                output = io.StringIO()
                with mock.patch.dict(os.environ, self.environment), \
                        mock.patch.object(gate, "run_gate_suites", side_effect=suite_run), \
                        mock.patch.object(gate, "tell_nightly_red", return_value=gate.NightlyNoticeUnreceived()) as notice, \
                        mock.patch.object(gate, "nightly_owner", return_value="quiet"), redirect_stdout(output):
                    code = gate.main(["--nightly", "--repo", str(self.checkout), "--merge-branch", "integration",
                                            "--unit", "fixture-merge"])
                self.assertEqual(code, int(red), output.getvalue())
                self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), before_head)
                self.assertEqual(self.git(self.checkout, "status", "--porcelain"), before_status)
                self.assertEqual(self.git(self.checkout, "show-ref", "--heads"), before_branches)
                self.assertEqual((self.checkout / "subject.txt").read_text(), "base\n")
                self.assertTrue(all(path.exists() for path in copies))
                self.assertEqual({build_folders.build_folder_state(path) for path in copies}, {"free"})
                self.assertEqual(len(set(copies)), 1)
                self.assertEqual(len(set(caches)), 1)
                self.assertTrue(caches[0].is_dir())
                if red:
                    record = cast(dict[str, object], json.loads(marker.read_text()))
                    self.assertEqual(record["tree"], self.git(self.checkout, "rev-parse", f"{self.checkpoint}^{{tree}}"))
                    self.assertEqual(record["failed_modules"], ["hooks/test_first.py"])
                    notice.assert_called_once()
                    self.assertEqual(notice.call_args.args[2], ["hooks/test_first.py"])
                else:
                    notice.assert_not_called()

    def test_nightly_without_an_identified_production_uses_the_callers_unit(self) -> None:
        from scripts.production import change_review, showrunners

        _ = self.git(self.unit, "push", "origin", "HEAD:integration")
        observed: list[Path] = []
        retained: list[Path] = []

        def suite_run(repo: Path, *_args: object, **_kwargs: object) -> gate.GateRun:
            observed.append(repo)
            retained.append((repo / "target").resolve().parent)
            lease = cast(dict[str, object], json.loads(repo.with_name(repo.name + ".lease.json").read_text()))
            self.assertEqual(lease["unit"], "fixture-merge")
            self.assertTrue((repo / ".git").is_file())
            return gate.GateRun((gate.GateJobResult("hooks", "test_first.py", 0, 0, "fixture"),), 0, 0)

        with (mock.patch.dict(os.environ, self.environment),
              mock.patch.object(change_review, "production_doc", side_effect=change_review.ChangeReviewError("none")),
              mock.patch.object(showrunners, "timer_docs", return_value={}),
              mock.patch.object(gate, "run_suites", side_effect=suite_run),
              redirect_stdout(io.StringIO())):
            code = gate.main(["--nightly", "--repo", str(self.checkout), "--merge-branch", "integration",
                                    "--unit", "fixture-merge"])
        self.assertEqual(code, 0)
        checkout, = observed
        self.assertEqual(checkout, self.root / "build/origin/fixture-merge/1")
        self.assertTrue(checkout.exists())
        self.assertEqual(build_folders.build_folder_state(checkout), "free")
        self.assertEqual(retained[0], checkout)
        self.assertTrue((retained[0] / "target").is_dir())

    def test_nightly_notice_resolves_the_production_from_its_branch_without_a_calling_session(self) -> None:
        from scripts.production import showrunners, staff
        from scripts.agents import remote

        production = self.checkout / "quiet-production.md"
        self.write(production, "# Quiet production\n")
        output = io.StringIO()
        environment = {**self.environment, "CLAUDE_CODE_SESSION_ID": "", "STAFF_FILE": str(self.root / "staff.json")}
        with mock.patch.dict(os.environ, environment):
            owner = showrunners.owner_for("quiet", staff.local_snapshot())
            _ = staff.set_doc(owner.id, staff.LiveDoc(remote.local_machine(), production))
        with mock.patch.dict(os.environ, environment), \
                mock.patch.object(showrunners, "timer_docs", return_value={"quiet": production}), \
                mock.patch.object(showrunners, "tell", return_value="could not tell quiet: offline") as send, \
                redirect_stdout(output):
            _ = gate.tell_nightly_red(gate.GateArguments(merge_branch="integration"),
                                        "fixture-tree", ["hooks/test_first.py"])
        send.assert_called_once()
        arguments = send.call_args
        assert arguments is not None
        self.assertEqual(arguments.args[:2], ("quiet", "merge-gate-nightly:fixture-tree"))
        self.assertIn("hooks/test_first.py", cast(str, arguments.args[2]))
        self.assertIn("could not tell quiet: offline", output.getvalue())

    def test_unavailable_ledger_runs_all_modules_and_reports_once(self) -> None:
        directory = Path(self.environment["GATE_PASSED_DIR"])
        first = self.run_gate("--tests-only")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        path = directory / "passed-trees.jsonl"
        saved = path.read_text()
        for failure in ("unreadable", "unwritable", "invalid"):
            with self.subTest(failure=failure):
                self.write(path, "invalid json\n" if failure == "invalid" else saved)
                path.chmod(0o200 if failure == "unreadable" else 0o400 if failure == "unwritable" else 0o600)
                self.clear_gate_events()
                try:
                    result = self.run_gate("--tests-only")
                finally:
                    path.chmod(0o600)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(result.stdout.count("Test pass ledger unavailable;"), 1, result.stdout)
                self.assert_all_modules_started()

    def test_changed_checkout_or_failed_append_cannot_record_a_pass(self) -> None:
        finish = gate.finish_gate_job
        for state in ("dirty-at-end", "dirty-at-start", "new-head", "append-fails"):
            with self.subTest(state=state):
                self.environment["GATE_PASSED_DIR"] = str(self.state / state)
                self.clear_gate_events()
                if state == "dirty-at-start":
                    self.write(self.checkout / "subject.txt", "temporary edit\n")

                def finish_and_change(job: gate.RunningGateJob, status: int) -> gate.GateJobResult:
                    result = finish(job, status)
                    if state == "dirty-at-end":
                        self.write(self.checkout / "subject.txt", "edit during run\n")
                    elif state == "dirty-at-start":
                        _ = self.git(self.checkout, "restore", "subject.txt")
                    elif state == "new-head":
                        self.write(self.checkout / "subject.txt", "new commit during run\n")
                        _ = self.commit(self.checkout, "changed during tests")
                    else:
                        path = Path(self.environment["GATE_PASSED_DIR"]) / "passed-trees.jsonl"
                        path.unlink()
                        path.mkdir()
                    return result

                output = io.StringIO()
                with mock.patch.dict(os.environ, self.environment), \
                        mock.patch.object(gate, "finish_gate_job", side_effect=finish_and_change), redirect_stdout(output):
                    run = gate.run_suites(self.checkout)
                self.assertTrue(run.passed, output.getvalue())
                self.assertEqual(len(run.jobs), 1)
                if state == "append-fails":
                    self.assertEqual(output.getvalue().count("Test pass ledger unavailable;"), 1)
                else:
                    self.assertEqual(self.pass_records(), [])
                _ = self.git(self.checkout, "restore", "subject.txt")

    def test_checkpoint_with_reused_tests_does_not_run_a_red_type_check(self) -> None:
        first = self.run_gate("--tests-only", "--repo", str(self.unit))
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.clear_gate_events()
        self.write(self.state / "types-red", "red")
        result = self.run_gate()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("tests reused from", result.stdout)
        self.assertFalse(any(call["tool"] == "basedpyright" for call in self.calls()))
        self.assertEqual(self.module_events(), [])
        merged = self.git(self.checkout, "rev-parse", "HEAD")
        self.assertNotEqual(merged, self.base)
        self.assertEqual(self.git(self.origin, "rev-parse", "integration"), merged)


if __name__ == "__main__":
    _ = unittest.main()
