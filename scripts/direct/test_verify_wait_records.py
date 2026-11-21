"""Check verify wait records with private build logs and an admission service."""
from __future__ import annotations

from collections.abc import Callable, Generator
from contextlib import contextmanager, nullcontext, suppress
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from typing import cast
from unittest import mock

from scripts.build_queue import admission_client as client, admission_protocol as wire, admission_records as records
from scripts.build_queue import admission_service, first_step, queue_place
from scripts.build_queue.admission_test_support import FakeClock, caller_admission
from scripts.build_queue.test_placement import PlacementFixture
from scripts.copies import copies
from scripts.direct.verify_examples_test_support import ExampleWorkspaceFixture, workspace_metadata
from scripts.direct.wait_for_test_state import wait_until
from scripts.lib import build_folders, conf
from scripts.lib.process_group import send_process_group
from scripts.lib.shell_test_support import shell_functions
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.lint import memory_admit as gate, nextest_tokens, prebuild
from scripts.lint.memory_test_support import write_pressure_reading


VERIFY = Path(__file__).with_name("verify.sh")
ADMISSION_CLIENT = VERIFY.parent.parent / "build_queue/admission_client.py"
INVOKE = VERIFY.parent.parent / "lint/invoke.sh"
RUN_VERIFY_HELPER = 'verify_helper() { "$CLAUDE_PYTHON" "$@"; }\n'


def wait_rows(root: Path, call_id: str, count: int = 1) -> tuple[records.PlacementWaitRow, ...]:
    root.mkdir(parents=True, exist_ok=True)
    journal = root / "placement-waits"
    wait_until(journal.is_dir, root)
    with mock.patch.dict(os.environ, {"BUILDLOG_DIR": str(root)}):
        wait_until(lambda: len(records.read_placement_waits(call_id)) >= count, journal, watch_writes=True)
        return records.read_placement_waits(call_id)


def wait_totals(root: Path, call_id: str) -> dict[str, float]:
    root.mkdir(parents=True, exist_ok=True)
    journal = root / "placement-waits"
    wait_until(journal.is_dir, root)
    with mock.patch.dict(os.environ, {"BUILDLOG_DIR": str(root)}):
        wait_until(lambda: "local_setup" in records.placement_wait_totals(call_id), journal, watch_writes=True)
        return records.placement_wait_totals(call_id)


def read_transitions(directory: Path) -> list[dict[str, object]]:
    entries = [cast(dict[str, object], json.loads(frame))
               for path in directory.glob(f"*{records.PLACEMENT_WAIT_JOURNAL_SUFFIX}")
               for frame in records.placement_wait_frames(path)]
    return [entry for entry in entries if entry["kind"] == "transition"]



class VerifyFinalWaitRecordsTests(ExampleWorkspaceFixture):
    def test_invalid_dev_features_end_preparation_on_exit(self) -> None:
        _ = self.package(manifest_extra="\n[workspace.metadata.dev]\nfeatures = 1\n")
        pid = os.getpid()
        started = records.process_start_time(pid)
        assert started is not None
        call_id = records.verify_run_call_id(pid, started)
        self.environment.update({"IN_NIX_SHELL": "pure", "CLAUDE_PYTHON": sys.executable,
                                 "BUILDLOG_RUN": f"{pid}:{started}", "BUILDLOG_PREPARED_COPY": str(self.root)})

        result = self.verify("final")

        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("workspace.metadata.dev.features must be a list of strings", result.stderr)
        self.assertIn("no final pass record was written", result.stdout)
        totals = wait_totals(Path(self.environment["BUILDLOG_DIR"]), call_id)
        self.assertGreater(totals["local_setup"], 0)


    def test_detached_verify_keeps_the_launcher_id_and_preparation(self) -> None:
        self.check_detached_verify_preparation(fail_registration=False)

    def test_detached_verify_keeps_the_start_when_launcher_registration_fails(self) -> None:
        self.check_detached_verify_preparation(fail_registration=True)

    def check_detached_verify_preparation(self, *, fail_registration: bool) -> None:
        _ = self.package(manifest_extra="\n[workspace.metadata.dev]\nfeatures = 1\n")
        launcher = self.root / "detach-fixture.py"
        trace = self.root / "handoff"
        _ = launcher.write_text("""\
import json
import os
from pathlib import Path
import subprocess
import sys

call_id = os.environ["VERIFY_CONTINUED_CALL_ID"]
started = float(os.environ["VERIFY_CONTINUED_STARTED_AT"])
Path(os.environ["FIXTURE_HANDOFF"]).write_text(json.dumps([call_id, started]))
command = sys.argv[sys.argv.index("--") + 1:]
label = sys.argv[sys.argv.index("--label") + 1]
directory = Path(os.environ["UNIT_RUN_DIR"]) / "detached_runs"
directory.mkdir(parents=True)
(directory / "fixture.json").write_text(json.dumps(
    {"label": label, "command": command, "status": "finished",
     "command_pid": os.getpid(), "command_started": "1234"}))
result = subprocess.run(command, env={**os.environ, "UNIT_DETACHED_RUN": "1"},
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
assert result.returncode == 2
print("The fixture report uses different wording.")
""")
        self.environment.update({"IN_NIX_SHELL": "pure", "CLAUDE_PYTHON": sys.executable,
                                 "CODEX_THREAD_ID": "fixture-thread", "VERIFY_DETACHED_RUNS_SCRIPT": str(launcher),
                                 "FIXTURE_HANDOFF": str(trace), "UNIT_RUN_DIR": str(self.root / "private-run")})
        if fail_registration:
            python = self.root / "python-fixture"
            _ = python.write_text('''#!/bin/sh
if [ "${5:-}" = placement-wait-start ] && [ -z "${UNIT_DETACHED_RUN:-}" ]; then
    echo 'fixture: launcher registration failed' >&2
    exit 1
fi
exec "$FIXTURE_PYTHON" "$@"
''')
            python.chmod(0o700)
            self.environment.update({"CLAUDE_PYTHON": str(python), "FIXTURE_PYTHON": sys.executable})

        result = self.verify("check")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("The fixture report uses different wording.", result.stdout)
        call_id, started = cast(tuple[str, float], json.loads(trace.read_text()))
        log = Path(self.environment["BUILDLOG_DIR"])
        journal_dir = log / "placement-waits"
        wait_until(journal_dir.is_dir, log)

        actions = ["continue", "finish_continued"]
        if not fail_registration:
            actions.append("register")
        wait_until(lambda: len(read_transitions(journal_dir)) >= len(actions), journal_dir, watch_writes=True)
        transitions = read_transitions(journal_dir)
        self.assertEqual(len(list(journal_dir.glob(f"*{records.PLACEMENT_WAIT_JOURNAL_SUFFIX}"))), 1)
        totals = wait_totals(log, call_id)
        self.assertCountEqual([entry["action"] for entry in transitions], actions)
        times = {entry["action"]: cast(float, entry["at"]) for entry in transitions}
        continuation = next(entry for entry in transitions if entry["action"] == "continue")
        self.assertGreater(cast(float, continuation["observed_at"]), started)
        self.assertEqual(times["continue"], started)
        self.assertEqual(totals["local_setup"], times["finish_continued"] - started)

    def test_a_live_twin_finishes_the_new_call(self) -> None:
        _ = self.package()
        launcher = self.root / "twin-fixture.py"
        trace = self.root / "twin"
        _ = launcher.write_text("""\
import json
import os
from pathlib import Path
import sys

Path(os.environ["FIXTURE_HANDOFF"]).write_text(os.environ["VERIFY_CONTINUED_CALL_ID"])
directory = Path(os.environ["UNIT_RUN_DIR"]) / "detached_runs"
directory.mkdir(parents=True)
command = sys.argv[sys.argv.index("--") + 1:]
(directory / "twin.json").write_text(json.dumps(
    {"label": "verify.sh check [earlier-call]", "command": command, "status": "running",
     "command_pid": os.getpid(), "command_started": "1234"}))
print("run 20261119T130000-1234-cafe started detached for fixture (pid 1234, log /fixture.log)")
""")
        self.environment.update({"IN_NIX_SHELL": "pure", "CLAUDE_PYTHON": sys.executable,
                                 "CODEX_THREAD_ID": "fixture-thread", "VERIFY_DETACHED_RUNS_SCRIPT": str(launcher),
                                 "FIXTURE_HANDOFF": str(trace), "UNIT_RUN_DIR": str(self.root / "private-run")})

        result = self.verify("check")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls_made(), [])
        log = Path(self.environment["BUILDLOG_DIR"])
        self.assertGreater(wait_totals(log, trace.read_text())["local_setup"], 0)

    def test_supervisor_exit_before_command_start_leaves_the_call_to_recovery(self) -> None:
        _ = self.package()
        launcher = self.root / "supervisor-fixture.py"
        run_dir = self.root / "private-run"
        _ = launcher.write_text("""\
import json
import os
from pathlib import Path
import sys

directory = Path(os.environ["UNIT_RUN_DIR"]) / "detached_runs"
directory.mkdir(parents=True)
(directory / "unstarted.json").write_text(json.dumps(
    {"label": sys.argv[sys.argv.index("--label") + 1],
     "command": sys.argv[sys.argv.index("--") + 1:], "status": "running", "pid": os.getpid()}))
""")
        self.environment.update({"IN_NIX_SHELL": "pure", "CLAUDE_PYTHON": sys.executable,
                                 "CODEX_THREAD_ID": "fixture-thread", "VERIFY_DETACHED_RUNS_SCRIPT": str(launcher),
                                 "UNIT_RUN_DIR": str(run_dir)})
        log = Path(self.environment["BUILDLOG_DIR"])
        journal_dir = log / "placement-waits"
        journal_dir.mkdir(parents=True)
        proc = self.root / "proc"
        proc.mkdir()

        result = self.verify("check")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls_made(), [])
        record = cast(dict[str, object], json.loads((run_dir / "detached_runs/unstarted.json").read_text()))
        self.assertNotIn("command_pid", record)
        wait_until(lambda: bool(read_transitions(journal_dir)), journal_dir, watch_writes=True)
        registered, = read_transitions(journal_dir)
        self.assertEqual(registered["action"], "register")
        owner_file, = (journal_dir / "active").glob("*.owner")
        owner = cast(dict[str, object], json.loads(owner_file.read_text()))
        call_id = cast(str, owner["call_id"])
        self.assertTrue(cast(str, record["label"]).endswith(f"[{call_id}]"))
        observed = cast(float, owner["seen_at"])
        with mock.patch.dict(os.environ, {"BUILDLOG_DIR": str(log), "BUILDLOG_PROC": str(proc),
                                         "UNIT_RUN_DIR": str(run_dir)}):
            self.assertTrue(client.detached_verify_handoff(cast(str, record["label"]),
                                                          cast(list[str], record["command"])))
            self.assertEqual(records.placement_wait_totals(call_id), {})
            clock = FakeClock()
            clock.epoch = observed - 1
            recorder = admission_service.PlacementWaitRecorder(log, admission_service.SystemProc(), clock)
            clock.advance(10)
            recorder.sweep_owners()

            self.assertFalse(owner_file.exists())
            self.assertEqual(records.placement_wait_totals(call_id),
                             {"local_setup": observed - cast(float, registered["at"])})

    def test_checkout_failure_after_the_first_step_request_finishes_the_call(self) -> None:
        _ = self.package()
        _ = caller_admission(self, self.root, self.environment)
        self.environment.update({"IN_NIX_SHELL": "pure", "CLAUDE_PYTHON": sys.executable,
                                 "TEST_METADATA": json.dumps(workspace_metadata(self.root))})
        request_dir = self.root / "request"
        request_dir.mkdir()
        grant = wire.Admitted("fixture-reservation", 1, "fixture", "", "", {})
        command = ("bash", str(VERIFY), "check", "sample")
        started = records.process_start_time(os.getpid())
        assert started is not None
        call_id = records.verify_run_call_id(os.getpid(), started)
        log = Path(self.environment["BUILDLOG_DIR"])

        def fail_checkout(*_args: object, **_kwargs: object) -> None:
            self.assertTrue((request_dir / "request.json").is_file())
            self.assertEqual(records.placement_wait_totals(call_id), {})
            raise copies.CopyError("fixture checkout failed")

        with mock.patch.dict(os.environ, self.environment, clear=True), \
                mock.patch.object(copies, "requested_source", return_value=nullcontext(self.root)), \
                mock.patch.object(copies, "commit_at", return_value="head"), \
                mock.patch.object(build_folders, "tool_folder", return_value=nullcontext(request_dir)), \
                mock.patch.object(first_step, "_placement_grant", return_value=grant), \
                mock.patch.object(queue_place, "release_place"), \
                mock.patch.object(copies, "standing", side_effect=fail_checkout):
            with self.assertRaisesRegex(copies.CopyError, "fixture checkout failed"):
                with copies.command_tree("fixture", self.root, copies.CommittedSource("head"), command):
                    self.fail("The checkout failed before Verify could run the command.")
            self.assertNotIn("BUILDLOG_FIRST_STEP_WAIT_CALL_ID", os.environ)

        self.assertGreater(wait_totals(log, call_id)["local_setup"], 0)


class VerifyWaitRecordsTests(unittest.TestCase):
    def test_verify_changes_wait_phases_through_one_function(self) -> None:
        source = VERIFY.read_text()
        transition = shell_functions(VERIFY, "transition_verify_wait")
        outside = source.replace(transition, "", 1)
        self.assertNotRegex(outside, r"\bplacement-wait(?:-start)?\b")
        self.assertNotRegex(outside, r"\b(?:launch_verify_wait_recorder|record_(?:early_)?verify_wait)[ \t]")
        self.assertNotRegex(source, r"\b(?:local_setup|VERIFY_SETUP_OPEN)\b")

    def test_a_killed_guarded_step_keeps_the_command_start(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(os.environ, {"BUILDLOG_DIR": temporary}):
            root = Path(temporary)
            entered = time.time() - 10
            call_id = "guarded-cargo"
            cargo = root / "cargo"
            _ = cargo.write_text('#!/bin/bash\nps -o pgid= -p "$$"\nread -r line\n')
            cargo.chmod(0o700)
            source = VERIFY.read_text()
            owner_code = source[source.index("STEP_OWNER_PY='"):source.index("\n# Preserve the shared invocation policy")]
            shell = owner_code + shell_functions(VERIFY, "buildlog_exec") + """
set -e
BOARD_DIR="$PWD/board"
mkdir -p "$BOARD_DIR/locks/cargo.d"
printf '%s' "$$" > "$BOARD_DIR/locks/cargo.d/holder_pid"
start_token_renewer() { return 0; }
stop_token_renewer() { return 0; }
buildlog_exec_unowned() { "$@"; }
TOKEN_HELD=1
STEP_RECORD_SCRIPT="$VERIFY_START_DIR/step_record.py"
PY="$CLAUDE_PYTHON"
buildlog_exec "$PWD/cargo"
"""
            environment = {**os.environ, "HOME": str(root), "VERIFY_START_DIR": str(VERIFY.parent),
                           "CLAUDE_PYTHON": sys.executable, "BUILDLOG_CALL_ID": call_id}
            with subprocess.Popen(["bash", "-c", shell], cwd=root, env=environment, start_new_session=True,
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True) as process:
                owner_start = records.process_start_time(process.pid)
                assert owner_start is not None
                records.register_placement_waits(call_id, records.PlacementWaitOwner(process.pid, owner_start),
                                                 entered, entered_at=entered)
                assert process.stdout is not None
                group = 0
                try:
                    self.assertTrue(select.select([process.stdout], [], [], TEST_HANG_GUARD_SECS)[0])
                    group = int(cast(str, process.stdout.readline()))
                    _ = wait_totals(root, call_id)
                finally:
                    if group:
                        with suppress(ProcessLookupError):
                            send_process_group(group, signal.SIGKILL)
                    with suppress(ProcessLookupError):
                        send_process_group(process.pid, signal.SIGKILL)
                    _ = process.communicate(timeout=TEST_HANG_GUARD_SECS)
            _ = records.close_orphaned_placement_waits(root, lambda _owner, _seen: records.OwnerLastSeen(entered + 1))
            totals = records.placement_wait_totals(call_id)
            self.assertGreaterEqual(totals["local_setup"], 10)

    def test_a_slow_checkout_is_setup_until_the_command_runs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(os.environ, {"BUILDLOG_DIR": temporary}):
            root = Path(temporary)
            call_id = "slow-checkout"
            args = copies.CopyArguments(tree="tree", capacity_file=str(root / "capacity"), verify_call_id=call_id,
                                        step="check", command=["cargo", "check"])
            records.register_placement_waits(call_id, records.UNTRACKED_WAIT_OWNER, 10, entered_at=10)

            @contextmanager
            def prepare_checkout(*_args: object, **_kwargs: object) -> Generator[Path]:
                clock.return_value = 16.0
                yield root

            def record_start(call: str, _pid: int) -> None:
                now = time.time()
                records.stamp_verify_command(call, now, now)
                self.assertEqual(records.placement_wait_totals(call_id), {"local_setup": 6})

            def run(command: list[str], *, on_start: Callable[[int], None], **_kwargs: object) -> int:
                self.assertEqual(command, ["cargo", "check"])
                self.assertEqual(records.placement_wait_totals(call_id), {})
                with mock.patch.object(records, "record_started_verify_command", side_effect=record_start):
                    on_start(os.getpid())
                return 0

            with mock.patch.object(copies, "commit_at", return_value="head"), \
                    mock.patch.object(copies, "git", return_value=b"head\ntree\n"), \
                    mock.patch.object(copies, "step_owner", return_value="fixture"), \
                    mock.patch.object(copies, "standing", side_effect=prepare_checkout), \
                    mock.patch.object(build_folders, "run_build_command", side_effect=run) as command, \
                    mock.patch.object(time, "time", return_value=10.0) as clock:
                self.assertEqual(copies.run_slot_step(root, args), 0)
                command.assert_called_once()

    def test_cargo_returns_its_status_and_tokens_while_the_writer_is_blocked(self) -> None:
        script = """
import os
import sys
from pathlib import Path
from unittest.mock import patch

from scripts.build_queue import admission_client, admission_records
from scripts.lint import nextest_tokens

if sys.argv[1:] == ["hold-tokens"]:
    run = nextest_tokens.SizedTestRun(
        ("./cargo", "nextest", "run"), 1, (),
        command_prefix=(sys.executable, __file__, "verify-command", "--call-id", "blocked-writer", "--"))
    sys.exit(nextest_tokens.run_with_tokens(run, Path("pool"), 1, None, lambda _width: None, nested_tokens=0))

def blocked_writer(*args):
    if "--no-run" not in sys.argv:
        Path("writer-waiting").touch()
        os.read(sys.stdin.fileno(), 1)

with patch.object(admission_records, "stamp_verify_command", side_effect=blocked_writer):
    sys.exit(admission_client.main(sys.argv[1:]))
"""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            launcher = root / "launcher.py"
            _ = launcher.write_text(f"import sys\nsys.path.insert(0, {str(VERIFY.resolve().parents[2])!r})\n" + script)
            cargo = root / "cargo"
            _ = cargo.write_text("""#!/bin/sh
case "$2" in
    list)
        printf '%s\\n' '{"rust-suites":{"fixture":{"testcases":{"test":{"filter-match":{"status":"matches"}}}}}}'
        exit 0
        ;;
    run)
        [ "$3" != --no-run ] || exit 0
        read -r release < cargo-exit || exit 97
        exit 23
        ;;
esac
exit 2
""")
            cargo.chmod(0o700)
            pool = root / "pool"
            cargo_exit = root / "cargo-exit"
            os.mkfifo(pool)
            os.mkfifo(cargo_exit)
            environment = {**os.environ, "PYTHONPATH": str(VERIFY.parents[2]), "BUILDLOG_DIR": str(root)}
            with os.fdopen(os.open(pool, os.O_RDWR | os.O_NONBLOCK), "r+b", buffering=0) as tokens, \
                    os.fdopen(os.open(cargo_exit, os.O_RDWR | os.O_NONBLOCK), "r+b", buffering=0) as release, \
                    (root / "launcher.log").open("w+") as log:
                _ = tokens.write(b"T")
                with subprocess.Popen(
                    [sys.executable, str(launcher), "hold-tokens"], cwd=root, env=environment,
                    stdin=subprocess.PIPE, stdout=log, stderr=log, start_new_session=True,
                ) as launched:
                    assert launched.stdin is not None
                    try:
                        waiting = root / "writer-waiting"
                        wait_until(lambda: waiting.is_file() or launched.poll() is not None, root, processes=(launched,))
                        _ = log.seek(0)
                        self.assertTrue(waiting.is_file(), log.read())
                        with self.assertRaises(BlockingIOError):
                            _ = os.read(tokens.fileno(), 1)
                        _ = release.write(b"exit\n")
                        status = launched.wait(timeout=TEST_HANG_GUARD_SECS)
                        _ = log.seek(0)
                        self.assertEqual(status, 23, log.read())
                        self.assertEqual(os.read(tokens.fileno(), 1), b"T")
                    finally:
                        launched.stdin.close()
                        if launched.returncode is None:
                            with suppress(ProcessLookupError):
                                send_process_group(launched.pid, signal.SIGKILL)

    def test_valid_prebuild_stamps_its_cargo_build_before_nextest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            call_id = "artifact-build"
            manifest = root / "Cargo.toml"
            _ = manifest.write_text('''[workspace.metadata.dev.prebuild.fixture]
target = "fixture-target"
env = "FIXTURE_ARTIFACT"
for = ["consumer"]
''')
            cargo = root / "cargo"
            _ = cargo.write_text(f'''#!{sys.executable}
import os
from pathlib import Path
import sys

sys.path.insert(0, {str(VERIFY.resolve().parents[2])!r})
from scripts.build_queue import admission_records
from scripts.direct.wait_for_test_state import wait_until

assert sys.argv[1] == "build"
journal = Path(os.environ["BUILDLOG_DIR"]) / "placement-waits"
wait_until(lambda: "local_setup" in admission_records.placement_wait_totals("{call_id}"), journal, watch_writes=True)
print('{{"reason":"compiler-artifact","package_id":"fixture 0.1.0 (file:///fixture)","target":{{"kind":["bin"]}},"executable":"/fixture/bin"}}')
''')
            cargo.chmod(0o700)
            environment = {"PATH": f"{root}:{os.environ['PATH']}", "BUILDLOG_DIR": str(root),
                           "PYTHONPATH": str(VERIFY.parents[2])}
            with mock.patch.dict(os.environ, environment), \
                    mock.patch.object(os, "execvp", side_effect=SystemExit(0)) as nextest, self.assertRaises(SystemExit):
                entered = time.time() - 5
                records.register_placement_waits(call_id, records.UNTRACKED_WAIT_OWNER, entered, entered_at=entered)
                _ = prebuild.main(["--manifest", str(manifest), "--verify-call-id", call_id, "--",
                                   "cargo", "nextest", "run", "-p", "consumer"])
            nextest.assert_called_once_with("cargo", ["cargo", "nextest", "run", "-p", "consumer"])
            with mock.patch.dict(os.environ, environment):
                self.assertGreater(records.placement_wait_totals(call_id)["local_setup"], 0)

    def test_manifest_refusal_counts_as_setup_before_cargo(self) -> None:
        shell = shell_functions(INVOKE, "run_step_attempt", "lint_prebuild_prefix", "buildlog_exec_with_progress",
                                "without_progress_frames") + shell_functions(VERIFY, "buildlog_exec") + r'''
set -eu
buildlog_check_nested_step() { :; }
lint_dev_shell_path() { :; }
buildlog_step_compiles() { :; }
build_hold_wait_local() { :; }
build_hold_check_local() { :; }
buildlog_begin() { :; }
buildlog_wait_for_memory() { BUILDLOG_MEM_OUTCOME=Granted BUILDLOG_MEM_WAIT_S=0; }
cargo_seed_needed() { return 1; }
nextest_thread_report() { :; }
nextest_token_width() { LINT_NEXTEST_TOKEN_WIDTH=""; }
buildlog_exec_unowned() { "$@"; }
buildlog_finish_memory() { :; }
buildlog_retry_unstarted_step() { :; }
buildlog_end() { cat "$2"; }
sweep_after_step() { :; }
BUILDLOG_RETRY_STEP=0
TOKEN_HELD=0
BUILD_FOLDERS_SCRIPT="$BUILD_HELPER_ROOT/lib/build_folders.py"
LINT_PREBUILD_SCRIPT="$BUILD_HELPER_ROOT/lint/prebuild.py"
LINT_TEST_TOKENS_SCRIPT="$BUILD_HELPER_ROOT/lint/nextest_tokens.py"
BUILDLOG_MEM_PY="$CLAUDE_PYTHON"
SANDBOX_SIGNATURE=fixture-unexpected-sandbox
run_step_attempt cargo nextest run --test-threads 2
'''
        for pooled in (False, True):
            with self.subTest(pooled=pooled), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                call_id = "manifest-refusal"
                cargo = root / "cargo"
                _ = cargo.write_text('#!/bin/sh\ntouch cargo-ran\n')
                cargo.chmod(0o700)
                _ = (root / "Cargo.toml").write_text("[workspace.metadata.dev]\nprebuild = 1\n")
                log = root / "buildlog"
                environment = {"HOME": str(root), "PATH": f"{root}:{os.environ['PATH']}",
                               "CLAUDE_PYTHON": sys.executable, "BUILDLOG_DIR": str(log),
                               "BUILD_HELPER_ROOT": str(VERIFY.parent.parent), "VERIFY_START_DIR": str(VERIFY.parent),
                               "VERIFY_PLACEMENT_CALL_ID": call_id, "TMPDIR": str(root),
                               "LINT_NEXTEST_POOL": str(root / "pool") if pooled else ""}
                entered = time.time() - 10
                with mock.patch.dict(os.environ, {"BUILDLOG_DIR": str(log)}):
                    records.register_placement_waits(call_id, records.UNTRACKED_WAIT_OWNER, entered, entered_at=entered)
                    result = subprocess.run(["bash", "-c", shell], cwd=root, env=environment,
                                            capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
                    ended = time.time()
                    records.finish_placement_waits(call_id, ended)

                    self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                    self.assertIn("workspace.metadata.dev.prebuild", result.stdout)
                    self.assertFalse((root / "cargo-ran").exists())
                    self.assertEqual(records.placement_wait_totals(call_id), {"local_setup": ended - entered})

    def test_pooled_nextest_stamps_the_compile_and_test_processes(self) -> None:
        config = mock.Mock(whole=mock.Mock(return_value=2))
        with mock.patch.object(conf, "read", return_value=config), \
                mock.patch.object(nextest_tokens, "run_with_tokens", return_value=0) as holder:
            self.assertEqual(prebuild.main([
                "--verify-nextest", "--call-id", "pooled", "--pool", "/fixture/pool",
                "--width-file", "/fixture/width", "--", "cargo", "nextest", "run", "--test-threads", "8", "-p", "foo",
            ]), 0)
        holder.assert_called_once()
        run = cast(nextest_tokens.SizedTestRun, holder.call_args.args[0])
        self.assertEqual(holder.call_args.args[1:4], (Path("/fixture/pool"), 2, Path("/fixture/width")))
        self.assertEqual(holder.call_args.kwargs, {"nested_tokens": 2})
        self.assertEqual(run.list_selected(), ["cargo", "nextest", "list", "--message-format", "json", "-p", "foo"])
        self.assertEqual(run.build_only()[2:],
                         ["verify-command", "--call-id", "pooled", "--", "cargo", "nextest", "run", "--no-run", "-p", "foo"])
        self.assertEqual(run.with_threads(4)[2:],
                         ["verify-command", "--call-id", "pooled", "--", "cargo", "nextest", "run",
                          "--test-threads", "4", "-p", "foo"])

    def test_a_blocked_recorder_releases_caller_pipes_and_times_out_in_the_run_log(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "direct").mkdir()
            (root / "lib").mkdir()
            (root / "build_queue").mkdir()
            release = root / "release"
            os.mkfifo(release)
            runner = root / "lib/py"
            runner.symlink_to(sys.executable)
            log = root / "run.log"
            _ = (root / "build_queue/admission_client.py").write_text("""\
import os

expected = os.stat(os.environ['RECORDER_LOG'])
assert all(os.path.samestat(os.fstat(fd), expected) for fd in (0, 1, 2))
with open(os.environ['RECORDER_RELEASE']) as gate:
    gate.readline()
""")
            shell = shell_functions(VERIFY, "run_verify_wait_recorder", "launch_verify_wait_recorder",
                                    "transition_verify_wait") + """
transition_verify_wait finish "" 12
printf 'verify continued\\n'
"""
            environment = {"PATH": os.environ["PATH"], "VERIFY_START_DIR": str(root / "direct"),
                           "VERIFY_PLACEMENT_CALL_ID": "async", "RECORDER_RELEASE": str(release),
                           "UNIT_DETACHED_RECORD": str(root / "run.json"), "RECORDER_LOG": str(log)}
            descriptor = os.open(release, os.O_RDWR | os.O_NONBLOCK)
            try:
                result = subprocess.run(["bash", "-c", shell], cwd=root, env=environment, capture_output=True,
                                        text=True, check=False, timeout=5)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "verify continued\n")
                self.assertEqual(result.stderr, "")
                wait_until(lambda: log.exists() and bool(log.read_text()), root, timeout=12, watch_writes=True)
                self.assertEqual(log.read_text(), "verify.sh: wait recorder exceeded 10 seconds\n")
            finally:
                os.close(descriptor)

    def test_request_only_verify_reports_preparation_when_checkout_validation_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            log = root / "buildlog"
            pid = os.getpid()
            started = records.process_start_time(pid)
            assert started is not None
            call_id = records.verify_run_call_id(pid, started)
            environment = {
                "PATH": os.environ["PATH"], "HOME": str(root), "TMPDIR": str(root),
                "CLAUDE_PYTHON": sys.executable, "IN_NIX_SHELL": "pure",
                "CONF_DIR": str(root / "config"),
                "GIT_CEILING_DIRECTORIES": str(root.parent), "GIT_CONFIG_NOSYSTEM": "1",
                "BUILDLOG_DIR": str(log), "BUILDLOG_RUN": f"{pid}:{started}",
                "BUILDLOG_FIRST_STEP_REQUEST": str(root / "request.json"),
            }
            result = subprocess.run(["bash", str(VERIFY), "check"], cwd=root, env=environment,
                capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)

            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn("has no Cargo.toml", result.stderr)
            self.assertGreater(wait_totals(log, call_id)["local_setup"], 0)

    def test_slow_nextest_check_after_a_direct_local_grant_counts_as_setup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            log = root / "buildlog"
            environment = {
                "PATH": os.environ["PATH"], "HOME": str(root), "TMPDIR": str(root),
                "CLAUDE_PYTHON": sys.executable, "BUILDLOG_DIR": str(log),
                "BUILDLOG_CALL_ID": "direct-nextest", "VERIFY_START_DIR": str(VERIFY.parent),
                "PYTHONPATH": str(VERIFY.parents[2]),
                "BUILD_TARGETS_SCRIPT": "fixture-targets", "BUILDLOG_MEM_ADMIT_SCRIPT": "fixture-placement",
                "DOC_REPO_TOP": str(root), "DOC_SOURCE_KEY": "fixture-source", "HOLD_WAIT_DIR": str(root),
            }
            shell = shell_functions(VERIFY, "transition_verify_wait", "placement_once",
                                    "route_test", "take_local_placement", "buildlog_exec") + r'''
set -e
verify_helper() {
    case "$1 $2" in
        "fixture-targets assigned-info") printf '\tlinux\t\n' ;;
        "fixture-placement place-step")
            transition_verify_wait routing "$PLACEMENT_EPISODE" 12
            transition_verify_wait done "$PLACEMENT_EPISODE" 14
            printf 'local\tnatedev\tfixture-host\t0\t%s\tattempt\n' "$PLACEMENT_EPISODE" ;;
        *) "$CLAUDE_PYTHON" "$@" ;;
    esac
}
launch_verify_wait_recorder() { "$CLAUDE_PYTHON" "$VERIFY_START_DIR/../build_queue/admission_client.py" "$@"; }
release_token() { return 0; }
check_routing_hold() { return 0; }
collect_routing_wait() { return 0; }
record_token_wait() { return 0; }
require_nextest() {
    [[ "$PLACEMENT_PROVIDER" == local ]] || return 2
    EPOCHREALTIME=17
}
acquire_token() {
    [[ "$EPOCHREALTIME" == 17 ]] || return 2
    transition_verify_wait slot "$PLACEMENT_EPISODE"
    EPOCHREALTIME=18
    transition_verify_wait token-ready "$PLACEMENT_EPISODE"
    EPOCHREALTIME=19
}
buildlog_exec_unowned() {
    [[ "$3 $6 $7 $8 $9" == "verify-command -- cargo nextest run" ]] || return 2
    "$CLAUDE_PYTHON" -c '
import sys
from unittest.mock import patch
from scripts.build_queue import admission_client as client, admission_records as records

def record_start(call_id, pid):
    records.stamp_verify_command(call_id, 20, 20)

with patch.object(records, "record_started_verify_command", side_effect=record_start):
    sys.exit(client.main([*sys.argv[1:5], sys.executable, "-c", "pass"]))
' "${@:3}"
}
transition_verify_wait register --pid "$$" --wait-at 10
unset EPOCHREALTIME
EPOCHREALTIME=12
PLACEMENT_COMMAND=(cargo nextest run)
CMD=test
TOKEN_HELD=0
route_test
[[ "$PLACEMENT_PROVIDER" == local ]]
EPOCHREALTIME=20
buildlog_exec "${PLACEMENT_COMMAND[@]}"
transition_verify_wait finish "" 30
'''
            result = subprocess.run(["bash", "-c", shell], cwd=root, env=environment, capture_output=True,
                text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(result.returncode, 0, result.stderr)
            rows = wait_rows(log, "direct-nextest", 2)
            reasons = records.PlacementWaitReason
            self.assertEqual([(row["reason"], row["started_at"], row["ended_at"]) for row in rows],
                             [(reasons.ROUTING, 12, 14), (reasons.SLOT, 17, 18)])
            totals = wait_totals(log, "direct-nextest")
            self.assertEqual(totals, {"routing": 2, "slot": 1, "local_setup": 7})
            self.assertEqual(sum(totals.values()), 10)

    def test_direct_placement_counts_the_session_lookup_as_setup(self) -> None:
        with PlacementFixture() as fixture:
            call_id = "direct"
            entered = fixture.clock.now()
            clock_file = fixture.root / "submission-clock"
            targets = fixture.root / "build_targets.conf"
            _ = targets.write_text("targets=natedev\nkind.natedev=local\nplatform.natedev=linux\n")
            environment = {
                **os.environ, "CLAUDE_PYTHON": sys.executable, "PYTHONPATH": str(VERIFY.parents[2]),
                "BUILDLOG_CALL_ID": call_id, "BUILDLOG_MEM_ADMIT_SCRIPT": str(ADMISSION_CLIENT),
                "BUILDLOG_FIRST_STEP_REQUEST": "", "BUILDLOG_FIRST_STEP_ASSIGNMENT": "",
                "BUILD_TARGETS_SCRIPT": "fixture-targets", "FIXTURE_CLOCK": str(clock_file),
                "BUILDLOG_FIRST_STEP_PID": str(os.getpid()), "DOC_REPO_TOP": str(fixture.root),
                "DOC_SOURCE_KEY": "direct-source", "HOLD_WAIT_DIR": str(fixture.temporary),
                "CMD": "check", "PKG": "fixture", "LOCAL_ONLY": "1", "LOCAL_REASON": "fixture only",
                "UNIT_RUN_DIR": "", "UNIT_SESSION_DIR": "", "GIT_CEILING_DIRECTORIES": str(fixture.root.parent),
            }
            shell = shell_functions(VERIFY, "transition_verify_wait", "run", "finish_step_token",
                                    "route_compiling_step", "placement_once") + r'''
verify_helper() {
    if [[ "$2" == submitting-session ]]; then
        printf '%s\n' "$FIXTURE_SUBMITTED_AT" > "$FIXTURE_CLOCK"
        return
    fi
    if [[ "$2" == place-step ]]; then
        "$CLAUDE_PYTHON" -c '
import os, sys
from pathlib import Path
from unittest.mock import patch
from scripts.build_queue import admission_client as client, admission_protocol as wire
from scripts.build_queue.admission_test_support import FakeClock

clock = FakeClock()
clock.epoch = float(Path(os.environ["FIXTURE_CLOCK"]).read_text())

def answer(request, _on_wait, *, report_refusal):
    clock.advance(3)
    return wire.PlacementWait(request.episode, request.since, 1, ())

with patch.object(client, "CLOCK", clock), patch.object(client, "_call", side_effect=answer):
    raise SystemExit(client.main(sys.argv[1:]))
' "${@:2}"
        return
    fi
    "$CLAUDE_PYTHON" "$@"
}
launch_verify_wait_recorder() { verify_helper "$BUILDLOG_MEM_ADMIT_SCRIPT" "$@"; }
buildlog_step_compiles() { [[ "$1 $2" == "cargo check" ]]; }
buildlog_begin() { return 0; }
placement_failed() { return 0; }
route_test() { placement_once; }
run_step() { local STEP_WRITES_TREE=1; route_compiling_step "$@"; }
unset EPOCHREALTIME
EPOCHREALTIME="$1"
export FIXTURE_SUBMITTED_AT="$2"
run cargo check -p "$PKG"
'''
            records.register_placement_waits(call_id, records.UNTRACKED_WAIT_OWNER, entered, entered_at=entered)
            result = subprocess.run(["bash", "-c", shell, "direct", str(entered + 2), str(entered + 5)],
                cwd=fixture.root, env=environment, capture_output=True, text=True, check=False,
                timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(result.returncode, 78, result.stderr)
            records.finish_placement_waits(call_id, entered + 8)
            routing, = records.read_placement_waits(call_id)
            self.assertEqual(records.placement_wait_totals(call_id), {"routing": 3, "local_setup": 5})
            self.assertEqual((routing["reason"], routing["started_at"], routing["ended_at"]),
                             (records.PlacementWaitReason.ROUTING, entered + 5, entered + 8))

    def test_placement_wait_replay_preserves_the_bounds_and_elapsed_time(self) -> None:
        with PlacementFixture() as fixture:
            call_id = "replay"
            entered = fixture.clock.now()
            request_file = fixture.root / "request.json"
            targets = fixture.root / "build_targets.conf"
            _ = targets.write_text("targets=natedev\nkind.natedev=local\nplatform.natedev=linux\n")
            environment = {
                **os.environ, "CLAUDE_PYTHON": sys.executable,
                "BUILDLOG_CALL_ID": call_id, "VERIFY_START_DIR": str(VERIFY.parent),
                "BUILDLOG_MEM_ADMIT_SCRIPT": str(ADMISSION_CLIENT), "BUILDLOG_FIRST_STEP_REQUEST": str(request_file),
                "BUILDLOG_FIRST_STEP_PID": str(os.getpid()), "BUILDLOG_FIRST_STEP_SINCE": str(entered - 30),
                "BUILDLOG_FIRST_STEP_EPISODE": "replay-step-1", "UNIT_RUN_DIR": "", "UNIT_SESSION_DIR": "",
                "BUILD_TARGETS_SCRIPT": str(VERIFY.parent.parent / "build_targets/build_targets.py"),
                "DOC_REPO_TOP": str(fixture.root), "DOC_SOURCE_KEY": "replay-source",
                "HOLD_WAIT_DIR": str(fixture.temporary), "CMD": "check", "PKG": "fixture", "LOCAL_ONLY": "1",
                "LOCAL_REASON": "use the private service", "GIT_CEILING_DIRECTORIES": str(fixture.root.parent),
            }
            shell = RUN_VERIFY_HELPER + shell_functions(VERIFY, "transition_verify_wait", "run", "run_step",
                "route_compiling_step", "placement_once") + '''
launch_verify_wait_recorder() { verify_helper "$BUILDLOG_MEM_ADMIT_SCRIPT" "$@"; }
buildlog_step_compiles() { [[ "$1 $2" == "cargo check" ]]; }
step_writes_tree() { return 1; }
transition_verify_wait register --pid "$$" --wait-at "$1"
unset EPOCHREALTIME
EPOCHREALTIME="$2"
run cargo check -p "$PKG"
'''
            result = subprocess.run(["bash", "-c", shell, "replay", str(entered), str(entered + 2)],
                cwd=fixture.root, env=environment, capture_output=True, text=True, check=False,
                timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(result.returncode, 0, result.stderr)
            request = wire.parse_request(request_file.read_bytes())
            assert isinstance(request, wire.PlacementRequest)
            assert isinstance(request.local, wire.LocalAdmissionContext)
            self.assertIsInstance(request.local.call, records.StepCallRecord)
            assert isinstance(request.local.call, records.StepCallRecord)
            self.assertEqual(request.local.call.step_started_at, entered + 2)
            self.assertEqual(request.since, entered - 30)
            self.assertEqual(records.read_placement_waits(call_id), ())

            def call_service(request: wire.Request, _on_wait: Callable[[wire.Wait], None],
                             *, report_refusal: bool) -> wire.Reply:
                _ = report_refusal
                if fixture.clock.now() == entered + 5:
                    fixture.clock.advance(1)
                reply, = fixture.connect(request).replies()
                return reply

            fixture.clock.advance(5)

            with mock.patch.object(client, "CLOCK", fixture.clock), mock.patch.object(client, "_call", side_effect=call_service):
                fixture.free(0)
                fixture.hold_starts()
                _ = fixture.run_pass()
                waiting = client.request_placement(request)
                self.assertIsInstance(waiting, wire.PlacementWait)

                fixture.clock.advance(4)
                fixture.free(60 * records.GIB)
                write_pressure_reading(fixture.root / "pressure")
                _ = fixture.run_pass()
                self.assertIsInstance(client.request_placement(request), wire.LocalPlacementGranted)

            records.stamp_verify_command(call_id, entered + 10, entered + 10)
            records.finish_placement_waits(call_id, entered + 20)
            rows = wait_rows(fixture.state_dir.parent, call_id, 2)
            reasons = records.PlacementWaitReason
            self.assertEqual([(row["reason"], row["started_at"] - entered, row["ended_at"] - entered) for row in rows],
                [(reasons.ROUTING, 5, 6), (reasons.MEMORY, 6, 10)])
            totals = records.placement_wait_totals(call_id)
            self.assertEqual(totals, {"routing": 1, "memory": 4, "local_setup": 5})
            self.assertEqual(sum(totals.values()), 10)
            with (fixture.state_dir / "queue_times.jsonl").open("rb") as stream:
                admitted, = (event for event in gate.queue_times_backward(stream) if event.get("kind") == "admitted")
            self.assertEqual(admitted["wait_seconds"], 8)

    def test_as_built_documents_call_start_and_end_limits(self) -> None:
        guide = VERIFY.parents[2] / "docs/as-built/build-memory-admission.md"
        content = guide.read_text()
        self.assertIn("A copy that exits before it calls verify has no call record.", content)
        self.assertIn(
            "If Nix refuses the call, recovery ends the wait at the last time it observed the owner alive.",
            content)
