"""Tests for a director's copies.py run as a detached run: the run and its queue place outlive the session."""

from __future__ import annotations

import contextlib
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import traceback
import unittest
from collections.abc import Callable, Generator
from pathlib import Path
from types import SimpleNamespace
from typing import cast, override
from unittest.mock import patch

from scripts.agents import detached_runs
from scripts.build_queue import admission_records as records
from scripts.build_queue import queue_lines
from scripts.copies import copies
from scripts.lib import process_start, unit_runs
from scripts.lib.atomic_file import write_text_atomic
from scripts.lib.fixture_test_support import set_up_fixture
from scripts.lib.signal_test_support import install_signal_pid_guard
from scripts.message import send
from scripts.message.repeat_guard_test_support import install_guard_test_budget
from scripts.production import staff
from scripts.production.staff_test_support import StaffTestCase, fixture_runtime_id, isolated_staff_file

COMMAND = ["fixture-copies.py", "run", "--", "cargo", "check"]
LABEL = "copies.py run -- cargo check"
TWIN_KEY = "key-1"
WAIT_SECONDS = 20.0
# The stand-in supervisor: it names `detached_runs.py supervise <record>` and holds the queue place as its owner.
# A running run also starts a build step that writes to the log.
STAND_IN = """
import os, signal, subprocess, sys
print("queue: waiting for a place", flush=True)
if os.environ["STAND_IN_MODE"] == "running":
    step = subprocess.Popen([sys.executable, "-c",
                             "import time\\nwhile True:\\n    print('step: running', flush=True)\\n    time.sleep(0.1)"])
    with open(sys.argv[-1] + ".step", "w") as out:
        out.write(str(step.pid))
signal.pause()
"""
# The start process exits after it starts the supervisor, as `detached_runs.py start` does.
LAUNCHER = """
import os, subprocess, sys
os.environ["STAND_IN_MODE"] = sys.argv[2]
with open(sys.argv[1], "ab") as log:
    child = subprocess.Popen(sys.argv[3:], stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                             start_new_session=True)
print(child.pid)
"""


# The real start; the tests patch it to run in the test process.
REAL_START_DETACHED = detached_runs._start_detached  # pyright: ignore[reportPrivateUsage]


def setUpModule() -> None:
    install_signal_pid_guard()
    install_guard_test_budget()


def deliver_in_fixture(path: Path) -> int:
    """Keep the sender calls inside the test process."""
    return detached_runs.main(["deliver", str(path)])


def start_in_fixture(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    """Run `detached_runs.py start` in the test process, so that the fixture patches apply."""
    with contextlib.redirect_stdout(io.StringIO()) as output:
        code = detached_runs.main(["start", *arguments])
    return subprocess.CompletedProcess(["start", *arguments], code, output.getvalue())


def started(pid: int) -> str:
    """The start identity of a live process, or an empty string."""
    found = process_start.process_start(pid)
    return found.start if isinstance(found, process_start.Started) else ""


def binary_stdout() -> tuple[io.BytesIO, io.TextIOWrapper]:
    raw = io.BytesIO()
    return raw, io.TextIOWrapper(raw, write_through=True)


def parent_pid(pid: int) -> int:
    return int(Path(f"/proc/{pid}/stat").read_text().rpartition(")")[2].split()[1])


def git(repo: Path, *arguments: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *arguments], capture_output=True, text=True, check=True).stdout.strip()


class ObservedRunTests(unittest.TestCase):
    root: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    session: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    unit: staff.StaffRow  # pyright: ignore[reportUninitializedInstanceVariable]
    mode: str  # pyright: ignore[reportUninitializedInstanceVariable]
    notices: list[tuple[str, str]]  # pyright: ignore[reportUninitializedInstanceVariable]
    stand_ins: list[tuple[int, str]]  # pyright: ignore[reportUninitializedInstanceVariable]
    supervisor_starts: list[list[str]]  # pyright: ignore[reportUninitializedInstanceVariable]
    scope_fails: bool  # pyright: ignore[reportUninitializedInstanceVariable]
    repo: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    worker: staff.StaffRow  # pyright: ignore[reportUninitializedInstanceVariable]

    @override
    def setUp(self) -> None:
        stack = self.enterContext(contextlib.ExitStack())
        self.root = Path(stack.enter_context(tempfile.TemporaryDirectory())).resolve()
        _ = stack.enter_context(isolated_staff_file(self.root / "staff.json", {}))
        self.session = self.root / "unit" / "run-1"
        self.session.mkdir(parents=True)
        config = self.root / "config"
        config.mkdir()
        for name in ("detached_runs", "send", "postmaster", "codex_daemon", "direct", "shutdown"):
            _ = (config / f"{name}.conf").write_text(
                (Path(__file__).resolve().parents[2] / f"config/{name}.conf").read_text())
        stack.enter_context(patch.dict(os.environ, {
            "HOME": str(self.root), "XDG_STATE_HOME": str(self.root / "state"),
            "DESIGN_ROLES_STATE": str(self.root / "astra/design-roles"),
            "BUILDLOG_SCOPE": "0", "CONF_DIR": str(config), "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "fixture", "GIT_AUTHOR_EMAIL": "fixture@localhost",
            "GIT_COMMITTER_NAME": "fixture", "GIT_COMMITTER_EMAIL": "fixture@localhost"}))
        for name in ("UNIT_DETACHED_RECORD", "UNIT_RUN_DIR", "BUILDLOG_OWNER_STAFF_ID", "BUILDLOG_UNIT",
                     "CLAUDE_SESSION_NAME", "CLAUDE_SESSION_FOLDER"):
            _ = os.environ.pop(name, None)
        fixture = set_up_fixture(stack.callback, StaffTestCase())
        _, self.unit, self.worker = fixture.tree()
        self.unit = staff.set_worktree(self.unit.id, staff.PresentWorktree("natedev", self.root), expected=self.unit.worktree)
        self.unit = staff.set_run(self.unit.id, staff.OpenRun(self.session))
        write_text_atomic(self.session / unit_runs.STATE_FILE, json.dumps({"owner_staff_id": self.unit.id}))
        stack.enter_context(patch.dict(os.environ, {"CODEX_THREAD_ID": "",
                                                    "CLAUDE_CODE_SESSION_ID": self.unit.current_session}))
        self.mode = "waiting"
        self.notices = []
        self.stand_ins = []
        self.supervisor_starts = []
        self.scope_fails = False
        self.repo = self.root / "repo"
        self.repo.mkdir()
        _ = git(self.repo, "init", "-q")
        _ = (self.repo / "file.txt").write_text("one\n")
        self.commit("first")

        def director_notice(options: send.Options, *, membership: staff.StaffSnapshot) -> send.Result:
            # The send's own staff lookup picks the conversation that receives the notice.
            member = send.resolve_staff_recipient(options.to, membership)
            assert isinstance(member, staff.StaffRow)
            self.notices.append((str(member.current_session), options.text or ""))
            return send.Result("sent", "the director received the result")

        _ = stack.enter_context(patch.object(send, "_send", director_notice))
        _ = stack.enter_context(patch.object(detached_runs, "deliver_in_fresh_process", side_effect=deliver_in_fixture))
        _ = stack.enter_context(patch.object(detached_runs, "_start_detached", side_effect=start_in_fixture))
        _ = stack.enter_context(self.stand_in_supervisors())
        _ = stack.callback(self.end_stand_ins)

    @contextlib.contextmanager
    def stand_in_supervisors(self) -> Generator[None]:
        """Replace each supervisor with a stand-in. Keep other process starts real."""
        real_popen = cast(Callable[..., subprocess.Popen[str]], subprocess.Popen)

        def spawn(arguments: list[str], **options: object) -> object:
            if "supervise" not in arguments:
                return real_popen(arguments, **options)
            self.assertIs(options.get("start_new_session"), True)
            self.supervisor_starts.append(arguments)
            if arguments[0] == "systemd-run" and self.scope_fails:
                return SimpleNamespace(pid=0, kill=lambda: None, wait=lambda: 1)
            path = arguments[-1]
            launcher = real_popen([sys.executable, "-c", LAUNCHER, str(Path(path).with_suffix(".log")), self.mode,
                                   sys.executable, "-c", STAND_IN, "detached_runs.py", "supervise", path],
                                  stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, text=True)
            output, _ = launcher.communicate(timeout=WAIT_SECONDS)
            pid = int(output)
            self.stand_ins.append((pid, started(pid)))
            return SimpleNamespace(pid=pid)

        with patch.object(subprocess, "Popen", side_effect=spawn):
            yield

    def end_stand_ins(self) -> None:
        for pid, start in self.stand_ins:
            if start and started(pid) == start:
                os.killpg(pid, signal.SIGKILL)
            self.wait_for(lambda: started(pid) != start, f"stand-in {pid} to end")

    def wait_for(self, check: Callable[[], bool], what: str) -> None:
        deadline = time.monotonic() + WAIT_SECONDS
        while not check():
            if time.monotonic() > deadline:
                self.fail(f"timed out: {what}")
            time.sleep(0.05)

    def commit(self, message: str) -> None:
        _ = git(self.repo, "add", "-A")
        _ = git(self.repo, "-c", "commit.gpgsign=false", "commit", "-q", "-m", message)

    def copies_run(self, *source: str) -> int | None:
        """Call copies.py run for `cargo check` on the fixture repository, as a director does."""
        args = copies.parse_copy_arguments(["run", "--repo", str(self.repo), *source, "--", "cargo", "check"])
        return copies.run_detached(args, copies.command_tail(args.command))

    @property
    def runs(self) -> Path:
        return self.session / detached_runs.DETACHED_RUNS_DIR

    def only_record(self) -> Path:
        path, = self.runs.glob("*.json")
        return path

    def read(self, path: Path) -> detached_runs.RunRecord:
        return cast(detached_runs.RunRecord, json.loads(path.read_text(encoding="utf-8")))

    def session_observer(self) -> tuple[int, Path]:
        """Fork a session process group that starts the run and observes it, as the Bash tool shell does."""
        out = self.root / "observer.out"
        pid = os.fork()
        if pid == 0:
            code = 99
            try:
                _ = os.setsid()
                descriptor = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
                _ = os.dup2(descriptor, 1)
                _ = os.dup2(descriptor, 2)
                sys.stdout = io.TextIOWrapper(io.FileIO(1, "w", closefd=False), write_through=True)
                sys.stderr = sys.stdout
                result = detached_runs.start_and_observe(self.session, COMMAND, LABEL, TWIN_KEY)
                code = 98 if result is None else result
            except BaseException:
                traceback.print_exc()
            finally:
                os._exit(code)
        self.addCleanup(self.end_session, pid)
        return pid, out

    def end_session(self, pid: int) -> None:
        """End the session process group, as a stop of the Claude process ends its Bash tool shells."""
        with contextlib.suppress(ProcessLookupError):
            os.killpg(pid, signal.SIGKILL)
        with contextlib.suppress(ChildProcessError):
            _ = os.waitpid(pid, 0)

    def observed_session(self) -> tuple[Path, int, str]:
        """Start the run from a session, wait until the observer prints the log, then end the session."""
        session, out = self.session_observer()
        self.wait_for(lambda: len(list(self.runs.glob("*.json"))) == 1 and self.read(self.only_record())["pid"] != 0,
                      "the detached run to start")
        path = self.only_record()
        # The session child started the stand-in, so this process records it for the cleanup.
        pid = self.read(path)["pid"]
        self.stand_ins.append((pid, started(pid)))
        self.wait_for(lambda: "queue: waiting for a place" in out.read_text(errors="replace"),
                      "the observer to print the queue line")
        self.assertIn("copies: started detached run", out.read_text())
        # The place owner is outside the session: another session and another parent.
        self.assertNotEqual(os.getsid(pid), session)
        self.assertNotEqual(parent_pid(pid), session)
        self.end_session(session)
        return path, pid, self.stand_ins[0][1]

    def restart(self) -> str:
        """The move resumes the director in a new session."""
        new = staff.native_id(fixture_runtime_id("observed-run-restarted-director"))
        self.unit = staff.update_current_session(self.unit.current_session, new)
        os.environ["CLAUDE_CODE_SESSION_ID"] = new
        return new

    def finish(self, path: Path, code: int, text: str) -> detached_runs.RunRecord:
        """Write the exit status and the last log line, as the supervisor does when the command ends."""
        with Path(self.read(path)["log"]).open("a", encoding="utf-8") as log:
            _ = log.write(text + "\n")
        record = self.read(path)
        record["exit_code"] = code
        write_text_atomic(path, json.dumps(record))
        return record

    def test_a_waiting_run_keeps_its_place_owner_when_the_session_ends(self) -> None:
        path, pid, start = self.observed_session()
        self.assertTrue(start)
        # The admission service keeps a place while its owner process lives with the same start time.
        time.sleep(0.5)
        self.assertEqual(started(pid), start)
        self.assertEqual([run["run_id"] for run in detached_runs.live_runs(self.runs, self.unit.name)],
                         [self.read(path)["run_id"]])

    def test_a_running_run_keeps_running_when_the_session_ends(self) -> None:
        self.mode = "running"
        path, pid, start = self.observed_session()
        step_file = Path(str(path) + ".step")
        self.wait_for(step_file.exists, "the build step to start")
        step = int(step_file.read_text())
        step_start = started(step)
        log = Path(self.read(path)["log"])
        size = log.stat().st_size
        self.wait_for(lambda: log.stat().st_size > size, "the build step to write after the session end")
        self.assertEqual((started(pid), started(step)), (start, step_start))
        self.assertTrue(detached_runs.live_runs(self.runs, self.unit.name))

    def test_a_second_call_after_a_restart_attaches_and_submits_nothing(self) -> None:
        observed: list[Path] = []

        def observe(path: Path) -> int:
            observed.append(path)
            return 0

        source = ("--commit", "HEAD", "--apply-uncommitted", str(self.repo))
        _ = (self.repo / "file.txt").write_text("two\n")
        with patch.object(detached_runs, "observe", side_effect=observe):
            with contextlib.redirect_stdout(io.StringIO()) as first:
                self.assertEqual(self.copies_run(*source), 0)
            _ = self.restart()
            # The twin matches the staff ID, so a rename during the wait still attaches.
            self.unit = staff.rename(self.unit.id, "models-renamed")
            with contextlib.redirect_stdout(io.StringIO()) as second:
                self.assertEqual(self.copies_run(*source), 0)
        path = self.only_record()
        record = self.read(path)
        run_id = record["run_id"]
        self.assertEqual(len(self.stand_ins), 1)
        self.assertEqual(observed, [path, path])
        # The run names the commit, not HEAD, so a later commit cannot change what the live run tests.
        self.assertIn(git(self.repo, "rev-parse", "HEAD"), record["command"])
        self.assertNotIn("HEAD", record["command"])
        self.assertIn(f"copies: started detached run {run_id}", first.getvalue())
        self.assertIn(f"copies: attached to the live run {run_id}", second.getvalue())
        self.assertIn(f"copies: record {path}", second.getvalue())
        script = Path(detached_runs.__file__).resolve()
        self.assertIn(f"copies: to stop it: {script.parents[1] / 'lib/py'} {script} stop --reason '<why>' {path}",
                      second.getvalue())

        # A call with no staff session, a worker's call, or a call inside the detached run runs attached.
        for environment in ({"CLAUDE_CODE_SESSION_ID": ""}, {"CLAUDE_CODE_SESSION_ID": self.worker.current_session},
                            {"UNIT_DETACHED_RECORD": str(path)}):
            with self.subTest(environment=environment), patch.dict(os.environ, environment):
                self.assertIsNone(self.copies_run(*source))
        self.assertEqual(len(self.stand_ins), 1)

    def test_a_call_attaches_only_to_a_live_run_of_the_same_source(self) -> None:
        with patch.object(detached_runs, "observe", return_value=0), contextlib.redirect_stdout(io.StringIO()):
            uncommitted = ("--commit", "HEAD", "--apply-uncommitted", str(self.repo))
            self.assertEqual(self.copies_run(*uncommitted), 0)
            # A change to the files is a new source: the call starts a new run.
            _ = (self.repo / "file.txt").write_text("two\n")
            self.assertEqual(self.copies_run(*uncommitted), 0)
            self.assertEqual(len(self.stand_ins), 2)
            # A new commit is a new source for --commit HEAD.
            committed = ("--commit", "HEAD")
            self.assertEqual(self.copies_run(*committed), 0)
            self.commit("second")
            self.assertEqual(self.copies_run(*committed), 0)
            self.assertEqual(len(self.stand_ins), 4)
            # A committed run ignores the working files: after an edit, the same call attaches.
            _ = (self.repo / "file.txt").write_text("three\n")
            self.assertEqual(self.copies_run(*committed), 0)
            self.assertEqual(len(self.stand_ins), 4)
            # A merge of a remote branch resolves only in the run. A repeat call with the same branch name attaches.
            self.assertEqual(self.copies_run(*committed, "--merge", "origin/main"), 0)
            self.assertEqual(self.copies_run(*committed, "--merge", "origin/main"), 0)
            self.assertEqual(len(self.stand_ins), 5)
            self.assertEqual(self.copies_run(*committed, "--merge", "origin/other"), 0)
            self.assertEqual(len(self.stand_ins), 6)

    def test_a_director_has_no_live_run_limit_but_a_worker_does(self) -> None:
        with patch.object(detached_runs, "observe", return_value=0), contextlib.redirect_stdout(io.StringIO()):
            for index in range(4):
                self.assertEqual(detached_runs.start_and_observe(self.session, COMMAND + [str(index)], LABEL, TWIN_KEY), 0)
        self.assertEqual(len(detached_runs.live_runs(self.runs, self.unit.name)), 4)
        self.assertTrue(all(self.read(path).get("director_run") for path in self.runs.glob("*.json")))
        # Another director start, such as a merge checkpoint, keeps the limit and the worker environment.
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as errors:
            code = detached_runs.main(["start", "--session-dir", str(self.session), "--", "checkpoint-command"])
        self.assertEqual(code, detached_runs.LIVE_RUN_LIMIT_REACHED, errors.getvalue())
        # A start for a named worker keeps the limit.
        with patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": ""}), contextlib.redirect_stdout(io.StringIO()):
            for index in range(3):
                self.assertEqual(detached_runs.main(["start", "--session-dir", str(self.session), "--to", "impl1",
                                                     "--", "worker-command", str(index)]), 0)
            with contextlib.redirect_stderr(io.StringIO()) as errors:
                code = detached_runs.main(["start", "--session-dir", str(self.session), "--to", "impl1",
                                           "--", "worker-command", "3"])
        self.assertEqual(code, detached_runs.LIVE_RUN_LIMIT_REACHED, errors.getvalue())

    def test_a_director_run_gets_no_worker_environment(self) -> None:
        with patch.object(detached_runs, "observe", return_value=0), contextlib.redirect_stdout(io.StringIO()):
            _ = detached_runs.start_and_observe(self.session, COMMAND, LABEL, TWIN_KEY)
        path = self.only_record()
        record = self.read(path)
        caller = {"UNIT_RUN_DIR": "/caller/run", "UNIT_TEAM_ROLE": "caller-role", "PATH": os.environ["PATH"]}
        director = detached_runs.command_environment(record, path, caller)
        self.assertEqual((director["UNIT_RUN_DIR"], director["UNIT_TEAM_ROLE"]), ("/caller/run", "caller-role"))
        self.assertEqual((director["UNIT_DETACHED_RUN"], director["BUILDLOG_OWNER_STAFF_ID"]), ("1", self.unit.id))
        del record["director_run"]
        worker = detached_runs.command_environment(record, path, caller)
        self.assertEqual((worker["UNIT_RUN_DIR"], worker["UNIT_TEAM_ROLE"]),
                         (record["session_dir"], record["worker"].rsplit("-", 1)[-1]))

    def test_the_supervisor_starts_in_its_own_user_scope_outside_builds_slice(self) -> None:
        script = Path(detached_runs.__file__).resolve()
        for fails in (False, True):
            with self.subTest(scope_fails=fails):
                self.scope_fails = fails
                self.supervisor_starts.clear()
                with (patch.object(detached_runs, "_scope_available", return_value=True),
                      patch.object(detached_runs, "_entered_scope", return_value=not fails),
                      patch.object(detached_runs, "observe", return_value=0), contextlib.redirect_stdout(io.StringIO())):
                    _ = detached_runs.start_and_observe(self.session, COMMAND + [str(fails)], LABEL, TWIN_KEY)
                path = max(self.runs.glob("*.json"), key=lambda found: self.read(found)["started_at"])
                record = self.read(path)
                supervise = [str(script.parents[1] / "lib/py"), str(script), "supervise", str(path)]
                scoped = ["systemd-run", "--user", "--scope", "--quiet", "--collect", "--expand-environment=no",
                          f"--unit=detached-supervisor-{record['run_id']}.scope", "--", *supervise]
                # The scope has no --slice: it is in the user manager's default slice, not in builds.slice.
                self.assertEqual(self.supervisor_starts, [scoped, supervise] if fails else [scoped])
                self.assertEqual(record["pid"], self.stand_ins[-1][0])
                log = Path(record["log"]).read_text()
                self.assertEqual("the supervisor scope did not start" in log, fails)
        self.assertEqual(detached_runs.supervisor_scope_prefix("run-1", {"BUILDLOG_SCOPE": "0"}), [])

    def test_a_scope_attempt_that_exits_did_not_get_its_scope(self) -> None:
        with subprocess.Popen([sys.executable, "-c", "raise SystemExit(1)"]) as process:
            self.assertFalse(detached_runs._entered_scope(process, "detached-supervisor-run-1.scope"))  # pyright: ignore[reportPrivateUsage]
        with (subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"]) as process,
              patch.object(detached_runs, "_cgroup_name", return_value="detached-supervisor-run-1.scope")):
            try:
                self.assertTrue(detached_runs._entered_scope(process, "detached-supervisor-run-1.scope"))  # pyright: ignore[reportPrivateUsage]
            finally:
                process.kill()

    def test_a_moved_director_run_reads_its_own_hold_key(self) -> None:
        from scripts.build_hold import build_hold, owning_session
        from scripts.build_targets import build_targets

        old = self.unit.current_session
        new = self.restart()
        # The detached command keeps the session ID of its start and the staff ID that the supervisor sets.
        environment = {"CLAUDE_CODE_SESSION_ID": old, "BUILDLOG_OWNER_STAFF_ID": self.unit.id}
        owner = owning_session.owning_session(environment)
        self.assertEqual(owner, owning_session.OwningSession(new))
        holds = self.root / "holds"
        self.assertEqual(build_targets.own_hold_key(owner, holds), build_hold.holder_path(holds, self.unit.id).name)
        # Without the staff ID, the start session stays the owner.
        self.assertEqual(owning_session.owning_session({"CLAUDE_CODE_SESSION_ID": old}), owning_session.OwningSession(old))

    def test_a_director_resume_names_its_live_runs_for_every_resume_path(self) -> None:
        with patch.object(detached_runs, "observe", return_value=0), contextlib.redirect_stdout(io.StringIO()):
            _ = detached_runs.start_and_observe(self.session, COMMAND, LABEL, TWIN_KEY)
        path = self.only_record()
        self.assertEqual(detached_runs.director_resume_context(self.unit),
                         detached_runs.resume_line(self.runs, self.read(path)))
        self.assertEqual(detached_runs.director_resume_context(self.worker), "")
        self.assertEqual(detached_runs.director_resume_context(None), "")
        with (patch.object(detached_runs, "live_runs", side_effect=OSError("unreadable")),
              contextlib.redirect_stderr(io.StringIO()) as errors):
            self.assertEqual(detached_runs.director_resume_context(self.unit), "")
        self.assertIn("detached runs anomaly: unreadable", errors.getvalue())

    def test_start_runs_in_a_separate_process(self) -> None:
        completed = subprocess.CompletedProcess(["start"], 0, "")
        with patch.object(subprocess, "run", return_value=completed) as run:
            self.assertIs(REAL_START_DETACHED(["--label", LABEL]), completed)
        script = Path(detached_runs.__file__).resolve()
        run.assert_called_once_with([str(script.parents[1] / "lib/py"), str(script), "start", "--label", LABEL],
                                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, text=True, check=False)

    def test_the_end_notice_reaches_the_director_after_its_restart(self) -> None:
        path, _, _ = self.observed_session()
        run_id = self.read(path)["run_id"]
        new = self.restart()
        # A rename during the wait keeps the run in the resume message: the staff ID matches it.
        renamed = staff.rename(self.unit.id, "models-renamed")
        self.assertEqual(detached_runs.resume_context(self.runs, renamed.name), "")
        context = detached_runs.resume_context(self.runs, renamed.name, identity=self.unit.id)
        self.assertEqual(context, detached_runs.resume_line(self.runs, self.read(path)))
        self.assertIn(f"Detached run {run_id} remains live. Result: {path}", context)
        self.assertIn("Its result follows you after the move.", context)
        record = self.finish(path, 0, "copies: PASS")
        self.assertIn("sent", detached_runs.deliver(record, detached_runs.CompletedCommand(0)))
        # The send resolves the staff ID to the conversation that the restart started.
        conversation, text = self.notices[-1]
        self.assertEqual((len(self.notices), conversation), (1, new))
        self.assertIn(f"Detached run: {run_id}", text)
        self.assertIn("copies: PASS", text)

    def test_the_end_notice_goes_even_when_the_observer_printed_the_result(self) -> None:
        with patch.object(detached_runs, "observe", return_value=0), contextlib.redirect_stdout(io.StringIO()):
            _ = detached_runs.start_and_observe(self.session, COMMAND, LABEL, TWIN_KEY)
        path = self.only_record()
        log = Path(self.read(path)["log"])
        self.wait_for(lambda: "queue: waiting for a place" in log.read_text(), "the stand-in to print its queue line")
        record = self.finish(path, 1, "copies: FAIL")
        raw, stdout = binary_stdout()
        with patch.object(sys, "stdout", stdout):
            self.assertEqual(detached_runs.observe(path), 1)
        self.assertIn("queue: waiting for a place", raw.getvalue().decode())
        self.assertIn("copies: FAIL", raw.getvalue().decode())
        self.assertIn("sent", detached_runs.deliver(record, detached_runs.CompletedCommand(1)))
        conversation, text = self.notices[-1]
        self.assertEqual((len(self.notices), conversation), (1, self.unit.current_session))
        self.assertIn("copies: FAIL", text)

    def test_stop_ends_the_run_and_its_place_owner(self) -> None:
        self.mode = "running"
        with patch.object(detached_runs, "observe", return_value=0), contextlib.redirect_stdout(io.StringIO()):
            _ = detached_runs.start_and_observe(self.session, COMMAND, LABEL, TWIN_KEY)
        path = self.only_record()
        pid, start = self.stand_ins[0]
        step_file = Path(str(path) + ".step")
        self.wait_for(step_file.exists, "the build step to start")
        step = int(step_file.read_text())
        step_start = started(step)
        # The supervisor publishes the command group after it starts the command.
        record = self.read(path)
        record["command_group"], record["command_started"] = pid, start
        write_text_atomic(path, json.dumps(record))
        with contextlib.redirect_stderr(io.StringIO()) as errors:
            code = detached_runs.main(["stop", "--reason", "the unit withdrew the run", str(path)])
        # The fixture has no systemd scope, so the stop signals the command group and reports it unconfirmed.
        self.assertEqual(code, 1)
        self.assertIn("the command group received TERM without a verified scope", errors.getvalue())
        # The admission service drops a place when its owner process ends.
        self.wait_for(lambda: started(pid) != start and started(step) != step_start, "the run to end")
        self.assertEqual(self.read(path).get("stop_reason"), "the unit withdrew the run")
        with contextlib.redirect_stderr(io.StringIO()):
            _ = detached_runs.reap_orphans(str(self.session))
        self.assertEqual(detached_runs.live_runs(self.runs, self.unit.name), [])
        self.assertEqual(self.read(path)["status"], "finished")
        conversation, text = self.notices[-1]
        self.assertEqual(conversation, self.unit.current_session)
        self.assertIn("Stop reason: the unit withdrew the run", text)

    def test_the_builds_page_names_the_unit_after_the_session_ends(self) -> None:
        old = self.unit.current_session
        _ = self.restart()
        # The detached command keeps the environment of the session that started it.
        with patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": old, "BUILDLOG_OWNER_STAFF_ID": self.unit.id}):
            caller = records.request_owner(str(self.root))
        assert isinstance(caller, records.QueueCaller)
        self.assertEqual((caller.name, caller.unit), (self.unit.name, self.unit.name))
        step = records.WaitingStep(os.getpid(), caller.session_id, caller.name, "gate", str(self.root), time.time(), 0,
                                   records.ReadyForAdmission(), unit=caller.unit)
        # The session registry has no label for the ended session; the stored unit names the row.
        self.assertEqual(queue_lines.step_owner_name(step, {}), self.unit.name)


if __name__ == "__main__":
    _ = unittest.main()
