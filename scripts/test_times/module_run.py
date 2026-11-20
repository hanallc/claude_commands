#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""Run unittest discovery and record each test's time."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import time

_STARTED_AT = time.perf_counter()
startup_cpu_s = 0.0
__unittest = True

import ctypes
import json
import os
import fcntl
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
from inspect import currentframe
from pathlib import Path
import resource
import select
import signal
import stat
import sys
import tempfile
from types import FrameType, MethodType, TracebackType
from typing import TYPE_CHECKING, ClassVar, Literal, Protocol, TypedDict, cast, final, override
import unittest
import unittest.case as unittest_case

if TYPE_CHECKING:
    from scripts.test_times.gate_record import TestStatus, TestTime


ExceptionInfo = tuple[type[BaseException], BaseException, TracebackType] | tuple[None, None, None]

# Python may omit the native constant.
_AT_FDCWD = cast(int, getattr(os, "AT_FDCWD", -2 if sys.platform == "darwin" else -100))

_OUTCOME_PRIORITY: dict[TestStatus, int] = {
    "pass": 0, "skip": 1, "expected_failure": 2,
    "unexpected_success": 3, "fail": 4, "error": 5,
}


@dataclass(frozen=True)
class OpenTest:
    test_id: str


@dataclass(frozen=True)
class NoOpenTest:
    """No parent test runs during fixture outcomes."""


@dataclass(frozen=True)
class NoConfigReadLog:
    """The isolated project has no config reader to record its reads."""


class RepositoryConfigRead(RuntimeError):
    """A test tried to read the checkout's tunable config."""


@final
class RepositoryReadAudit:
    """Record repository listings and file reads in this process."""

    def __init__(self, root: Path, folders: set[str], contents: set[str], forbidden_config_paths: set[str]) -> None:
        self.roots = tuple(dict.fromkeys((os.path.realpath(root), os.path.abspath(root))))
        self.folders = folders
        self.contents = contents
        self.forbidden_config_paths = forbidden_config_paths
        self.active = True
        self.process_id = os.getpid()
        self.current_process = os.getpid
        self.decode = os.fsdecode
        self.current_directory = os.getcwd
        self.normalize = os.path.normpath
        self.resolve = os.path.realpath
        self.read_link = os.readlink
        self.get_descriptor_path = fcntl.fcntl
        self.descriptor_path_command = cast(int, getattr(fcntl, "F_GETPATH", 50))
        self.platform = sys.platform
        self.current_frame = currentframe
        self.file_status = os.stat
        self.is_regular = stat.S_ISREG
        self.open_directory: ContextVar[int | None] = ContextVar("open_directory", default=None)
        self.write_flags = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC | os.O_EXCL

    def install(self, cleanup: ExitStack) -> None:
        """Record opens relative to a directory descriptor."""
        native_open = os.open

        def open_at(path: str | bytes | os.PathLike[str] | os.PathLike[bytes], flags: int,
                    mode: int = 0o777, *, dir_fd: int | None = None) -> int:
            if dir_fd is None:
                return native_open(path, flags, mode)
            token = self.open_directory.set(dir_fd)
            try:
                return native_open(path, flags, mode, dir_fd=dir_fd)
            finally:
                self.open_directory.reset(token)

        def restore_open() -> None:
            os.open = native_open

        os.open = open_at
        sys.addaudithook(self)
        _ = cleanup.callback(restore_open)
        _ = cleanup.callback(self.stop)

    def stop(self) -> None:
        """Stop the audit when the test module ends."""
        self.active = False

    def absolute_path(self, path: str, cwd: str) -> str:
        return self.normalize(path if path.startswith("/") else cwd + "/" + path)

    def descriptor_path(self, descriptor: int) -> str:
        if self.platform == "darwin":
            return self.decode(self.get_descriptor_path(
                descriptor, self.descriptor_path_command, bytes(1024)).split(b"\0", 1)[0])
        return self.read_link(f"/proc/self/fd/{descriptor}")

    def listed_folder(self, value: object, cwd: str, *, resolve: bool = False) -> set[str]:
        if value is None:
            value = cwd
        if isinstance(value, int):
            try:
                value = self.descriptor_path(value)
            except (OSError, ValueError):
                return {"/"}
        if not isinstance(value, (str, bytes, os.PathLike)):
            return set()
        path = self.decode(value)
        absolute = (self.resolve(path if path.startswith("/") else cwd + "/" + path)
                    if resolve else self.absolute_path(path, cwd))
        for root in self.roots:
            if absolute == root:
                return {"."}
            if absolute.startswith(root + "/"):
                return {absolute[len(root) + 1:]}
        return set()

    def forbid_config_reads(self, paths: set[str]) -> None:
        config = {path for path in paths if path.startswith("config/") and path.endswith(".conf")}
        if config:
            self.forbidden_config_paths.update(config)
            raise RepositoryConfigRead(f"Use a fixed test config for {', '.join(sorted(config))}.")

    def git_checkout_matches(self, cwd: str, git_dir: str) -> bool:
        from scripts.lib import git_files

        if git_dir:
            actual = self.resolve(self.absolute_path(git_dir, cwd))
            return actual == str(git_files.at(Path(self.roots[0])).git_dir)
        return (bool(self.listed_folder(cwd, self.roots[0]))
                and str(git_files.locate(Path(cwd)).toplevel) in self.roots)

    def child_folders(self, arguments: tuple[object, ...]) -> set[str]:
        executable, command, directory, environment = arguments
        if not isinstance(executable, (str, bytes, os.PathLike)) or self.decode(executable).rsplit("/", 1)[-1] != "git":
            return set()
        if not isinstance(command, (list, tuple)):
            return set()
        values = cast(Sequence[object], command)
        if not values or not all(isinstance(value, (str, bytes, os.PathLike)) for value in values):
            return set()
        argv = [self.decode(cast(str | bytes | os.PathLike[str] | os.PathLike[bytes], value)) for value in values]
        current_directory = self.current_directory()
        cwd = current_directory if directory is None else self.decode(cast(str | bytes | os.PathLike[str], directory))
        cwd = self.absolute_path(cwd, current_directory)
        git_dir = os.environ.get("GIT_DIR", "")
        if environment is not None:
            env = cast(Mapping[str | bytes, str | bytes], environment)
            git_dir = self.decode(env.get("GIT_DIR", env.get(b"GIT_DIR", "")))
        index = 1
        while index < len(argv):
            option = argv[index]
            key, _, value = option.partition("=")
            if option == "-C" and index + 1 < len(argv):
                if argv[index + 1]:
                    cwd = self.absolute_path(argv[index + 1], cwd)
                index += 2
            elif key in {"--git-dir", "--work-tree"}:
                if not value and index + 1 < len(argv):
                    index += 1
                    value = argv[index]
                if key == "--git-dir":
                    git_dir = value
                index += 1
            elif option in {"-c", "--namespace", "--config-env"}:
                index += 2
            elif option.startswith("-"):
                index += 1
            else:
                break
        if index == len(argv):
            return set()
        verb = argv[index]
        if verb not in {"show", "cat-file", "ls-files"}:
            return set()
        cwd = self.resolve(cwd)
        operands = argv[index + 1:]
        if verb in {"show", "cat-file"}:
            for operand in operands:
                if operand == "--" and verb == "show":
                    break
                revision, separator, path = operand.partition(":")
                if not revision and separator:
                    stage, stage_separator, staged_path = path.partition(":")
                    if stage_separator and stage in {"0", "1", "2", "3"}:
                        path = staged_path
                if separator and path.endswith(".conf") and not operand.startswith("-"):
                    base = cwd if path.startswith(("./", "../")) else self.roots[0]
                    config = self.listed_folder(path, base)
                    if config and self.git_checkout_matches(cwd, git_dir):
                        self.forbid_config_reads(config)
            return set()
        if git_dir and not self.git_checkout_matches(cwd, git_dir):
            return set()
        if not self.listed_folder(cwd, self.roots[0]):
            return {"/"} if git_dir else set()
        pathspecs: list[str] = []
        skip_value = False
        for offset, option in enumerate(operands):
            if skip_value:
                skip_value = False
            elif option == "--":
                pathspecs.extend(operands[offset + 1:])
                break
            elif option.startswith("-"):
                skip_value = option in {"-x", "--exclude", "-X", "--exclude-from", "--exclude-per-directory", "--format"}
            else:
                pathspecs.append(option)
        if not pathspecs:
            return {"/"}
        folders: set[str] = set()
        for pathspec in pathspecs:
            # Complex Git pathspecs cover a wider area than the named folder.
            if pathspec.startswith(":") or any(mark in pathspec for mark in ("*", "?", "[")):
                folders.add("/")
                continue
            for folder in self.listed_folder(pathspec, cwd):
                folders.add("/" if folder == "." else folder.rstrip("/") + "/")
        return folders

    def __call__(self, event: str, arguments: tuple[object, ...]) -> None:
        if not self.active or event not in {"os.scandir", "os.listdir", "subprocess.Popen", "open"}:
            return
        if self.current_process() != self.process_id:
            return
        try:
            target = self.folders
            if event == "open":
                mode, flags = arguments[1:3]
                if not isinstance(flags, int):
                    return
                directory = self.open_directory.get()
                if directory == _AT_FDCWD:
                    directory = None
                cwd = self.current_directory() if directory is None else self.descriptor_path(directory)
                paths = self.listed_folder(arguments[0], cwd)
                self.forbid_config_reads(self.listed_folder(arguments[0], cwd, resolve=True))
                if flags & self.write_flags or isinstance(mode, str) and (not mode.startswith("r") or "+" in mode):
                    return
                paths = {path for path in paths if path not in {".", "/", ".git"}
                         and not path.startswith(".git/") and "__pycache__" not in path.split("/")}
                if not paths:
                    return
                # Use the original spelling for the existing-file check.
                path = cast(str | bytes | os.PathLike[str] | os.PathLike[bytes] | int, arguments[0])
                status = self.file_status(path) if directory is None else self.file_status(path, dir_fd=directory)
                if not self.is_regular(status.st_mode):
                    return
                target = self.contents
            elif event == "subprocess.Popen":
                paths = self.child_folders(arguments)
            else:
                cwd = self.current_directory()
                paths = self.listed_folder(arguments[0], cwd)
            if not paths:
                return
            frame: FrameType | None = self.current_frame()
            try:
                while frame is not None:
                    if frame.f_code.co_filename == "<frozen importlib._bootstrap_external>":
                        return
                    frame = frame.f_back
            finally:
                del frame
            target.update(paths)
        except (OSError, ValueError):
            # An observation error does not change the test result.
            return


@dataclass(frozen=True)
class TimesOutput:
    path: Path
    config_log: Path | NoConfigReadLog


@dataclass(frozen=True)
class NoTimesOutput:
    """The caller did not request a timing file."""


class TestOutput(Protocol):
    """Supply the output methods used by the unittest result."""

    def write(self, text: str, /) -> object: ...

    def flush(self) -> object: ...

    def writeln(self, text: str | None = None, /) -> None: ...


if TYPE_CHECKING:
    # The runtime class does not support the type stub's stream parameter.
    TimedResultBase = unittest.TextTestResult[TestOutput]
else:
    TimedResultBase = unittest.TextTestResult


class TimedResult(TimedResultBase):
    def __init__(self, stream: TestOutput, descriptions: bool, verbosity: int,
                 *, durations: int | None = None) -> None:
        super().__init__(stream, descriptions, verbosity, durations=durations)
        self.open_test: OpenTest | NoOpenTest = NoOpenTest()
        self.outcomes: dict[str, TestStatus] = {}
        self.fixtures: set[str] = set()
        self.test_labels: dict[str, str] = {}
        self.cpu_times: dict[str, float] = {}
        self.test_cpu_started: float = 0.0

    @override
    def startTest(self, test: unittest.TestCase) -> None:
        self.test_cpu_started = cpu_seconds()
        self.open_test = OpenTest(test.id())
        self.test_labels[test.id()] = test.id().removeprefix(type(test).__module__ + ".")
        super().startTest(test)

    @override
    def stopTest(self, test: unittest.TestCase) -> None:
        self.cpu_times[test.id()] = cpu_seconds() - self.test_cpu_started
        super().stopTest(test)
        self.open_test = NoOpenTest()

    def record_outcome(self, test: unittest.TestCase, status: TestStatus) -> None:
        if isinstance(self.open_test, OpenTest):
            test_id = self.open_test.test_id
        else:
            test_id = test.id()
            self.fixtures.add(test_id)
        previous = self.outcomes.get(test_id, "pass")
        if _OUTCOME_PRIORITY[status] >= _OUTCOME_PRIORITY[previous]:
            self.outcomes[test_id] = status

    @override
    def addSuccess(self, test: unittest.TestCase) -> None:
        self.record_outcome(test, "pass")
        super().addSuccess(test)

    @override
    def addFailure(self, test: unittest.TestCase, err: ExceptionInfo) -> None:
        self.record_outcome(test, "fail")
        super().addFailure(test, err)

    @override
    def addError(self, test: unittest.TestCase, err: ExceptionInfo) -> None:
        self.record_outcome(test, "error")
        super().addError(test, err)

    @override
    def addSkip(self, test: unittest.TestCase, reason: str) -> None:
        self.record_outcome(test, "skip")
        super().addSkip(test, reason)

    @override
    def addExpectedFailure(self, test: unittest.TestCase, err: ExceptionInfo) -> None:
        self.record_outcome(test, "expected_failure")
        super().addExpectedFailure(test, err)

    @override
    def addUnexpectedSuccess(self, test: unittest.TestCase) -> None:
        self.record_outcome(test, "unexpected_success")
        super().addUnexpectedSuccess(test)

    @override
    def addSubTest(self, test: unittest.TestCase, subtest: unittest.TestCase,
                   err: ExceptionInfo | None) -> None:
        if err is not None:
            exception_type = err[0]
            status = ("fail" if exception_type is not None
                      and issubclass(exception_type, test.failureException) else "error")
            self.record_outcome(test, status)
        super().addSubTest(test, subtest, err)

    @override
    def addDuration(self, test: unittest.TestCase, elapsed: float) -> None:
        super().addDuration(test, elapsed)
        self.collectedDurations[-1] = (test.id(), elapsed)


def timed_outcomes(result: TimedResult) -> list[tuple[str, TestStatus, float]]:
    """Combine each full test id with its outcome and duration."""
    durations = dict(result.collectedDurations)
    return [(test_id, status, 0.0 if status == "skip" else durations.get(test_id, 0.0))
            for test_id, status in result.outcomes.items()]


def cpu_seconds() -> float:
    """Read own CPU and the CPU of children already reaped by this process."""
    own = resource.getrusage(resource.RUSAGE_SELF)
    children = resource.getrusage(resource.RUSAGE_CHILDREN)
    return own.ru_utime + own.ru_stime + children.ru_utime + children.ru_stime


class TestMeasure(TypedDict):
    test: str
    s: float
    status: TestStatus
    kind: Literal["test", "fixture"]
    cpu_s: float


class FixtureMeasure(TypedDict):
    kind: Literal["fixture"]
    test: str
    s: float
    cpu_s: float


def test_times(result: TimedResult) -> list[TestTime]:
    """Keep legacy outcome rows and add the CPU and measure kind."""
    rows: list[TestTime] = []
    for test_id, status, elapsed in timed_outcomes(result):
        fixture = test_id in result.fixtures
        row: TestMeasure = {
            "test": test_id if fixture else result.test_labels[test_id],
            "s": elapsed, "status": status,
            "kind": "fixture" if fixture else "test",
            "cpu_s": result.cpu_times.get(test_id, 0.0),
        }
        rows.append(row)
    return rows


@contextmanager
def fixture_measures(suite: unittest.TestSuite | unittest.TestCase
                     ) -> Generator[list[FixtureMeasure], None, None]:
    """Measure fixture calls without changing unittest's fixture dispatch."""
    rows: list[FixtureMeasure] = []
    restorations: list[tuple[object, str, dict[str, object]]] = []
    classes: set[type[unittest.TestCase]] = set()
    modules: set[str] = set()
    pending: list[tuple[object, str, Callable[[], object], str, bool]] = []
    depth = 0

    def measured(function: Callable[[], object], name: str) -> Callable[[], object]:
        @wraps(function)
        def call() -> object:
            nonlocal depth
            if depth:
                return function()
            depth += 1
            cpu = cpu_seconds()
            started = time.perf_counter()
            try:
                return function()
            finally:
                depth -= 1
                rows.append({"kind": "fixture", "test": name,
                             "s": time.perf_counter() - started,
                             "cpu_s": cpu_seconds() - cpu})
        return call

    def install(owner: object, name: str, function: Callable[[], object], label: str,
                *, class_method: bool = False) -> None:
        original = {name: vars(owner)[name]} if name in vars(owner) else {}
        restorations.append((owner, name, original))
        if class_method and isinstance(function, MethodType):
            unbound = cast(Callable[[type[unittest.TestCase]], object], function.__func__)

            def class_call(cls: type[unittest.TestCase]) -> object:
                return measured(lambda: unbound(cls), f"{name} ({cls.__module__}.{cls.__qualname__})")()

            setattr(owner, name, classmethod(class_call))
        elif class_method:
            setattr(owner, name, staticmethod(measured(function, label)))
        else:
            setattr(owner, name, measured(function, label))

    def visit(test: unittest.TestSuite | unittest.TestCase) -> None:
        if isinstance(test, unittest.TestSuite):
            for child in test:
                visit(child)
            return
        cls = type(test)
        if cls not in classes:
            classes.add(cls)
            for name in ("setUpClass", "tearDownClass", "doClassCleanups"):
                value = cast(object, getattr(cls, name))
                if value is None:
                    continue
                pending.append((cls, name, cast(Callable[[], object], value), f"{name} ({cls.__module__}.{cls.__qualname__})", True))
        module_name = cls.__module__
        if module_name not in modules:
            modules.add(module_name)
            module = sys.modules.get(module_name)
            if module is None:
                return
            for name in ("setUpModule", "tearDownModule"):
                if name in vars(module):
                    value = cast(object, getattr(module, name))
                    if value is None:
                        continue
                    pending.append((module, name, cast(Callable[[], object], value), f"{name} ({module_name})", False))

    try:
        visit(suite)
        for owner, name, function, label, class_method in pending:
            install(owner, name, function, label, class_method=class_method)
        install(unittest_case, "doModuleCleanups", unittest_case.doModuleCleanups, "doModuleCleanups")
        yield rows
    finally:
        for owner, name, original in reversed(restorations):
            if name in original:
                setattr(owner, name, original[name])
            else:
                delattr(owner, name)


def fixture_label(label: str) -> str:
    operation, opening, owner = label.partition(" (")
    if not opening:
        return label
    parts = owner.removesuffix(")").split(".")
    count = 1 if operation.endswith("Module") else 2
    return f"{operation} ({'.'.join(parts[-count:])})"


def write_times(path: Path, result: TimedResult, wall_s: float,
                fixtures: list[FixtureMeasure], config_reads: Sequence[str] | NoConfigReadLog) -> None:
    """Replace the output file without changing the test result on failure."""
    temporary = ""
    try:
        own = resource.getrusage(resource.RUSAGE_SELF)
        children = resource.getrusage(resource.RUSAGE_CHILDREN)
        tests = test_times(result)
        for row in tests:
            if row["test"] in result.fixtures:
                cast(TestMeasure, row)["cpu_s"] = sum(
                    fixture["cpu_s"] for fixture in fixtures if fixture["test"] == row["test"])
        for measure in [*tests, *fixtures]:
            measure["test"] = fixture_label(measure["test"])
        times: dict[str, object] = {
            "wall_s": wall_s,
            "cpu_s": startup_cpu_s + own.ru_utime + own.ru_stime + children.ru_utime + children.ru_stime,
            "tests_s": sum(test["s"] for test in tests),
            "tests_run": result.testsRun,
            "failures": len(result.failures),
            "errors": len(result.errors),
            "skipped": len(result.skipped),
            "tests": tests,
            "fixtures": fixtures,
        }
        if not isinstance(config_reads, NoConfigReadLog):
            times["config_reads"] = sorted(set(config_reads))
        times["folder_scans"] = sorted(TimedRunner.scanned_folders)
        times["content_reads"] = sorted(TimedRunner.read_contents)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=f".{path.name}.", delete=False) as handle:
            temporary = handle.name
            json.dump(times, handle)
            _ = handle.write("\n")
        os.replace(temporary, path)
    except Exception as error:
        detail = str(error).replace("\n", " ")
        print(f"test times not written: {detail}", file=sys.stderr)
    finally:
        if temporary:
            try:
                Path(temporary).unlink(missing_ok=True)
            except OSError:
                pass


class TimedRunner(unittest.TextTestRunner):
    resultclass: Callable[[TestOutput, bool, int], TimedResult] = TimedResult
    destination: ClassVar[TimesOutput | NoTimesOutput] = NoTimesOutput()
    scanned_folders: ClassVar[set[str]] = set()
    read_contents: ClassVar[set[str]] = set()

    @override
    def run(self, test: unittest.TestSuite | unittest.TestCase) -> unittest.TextTestResult:
        with fixture_measures(test) as fixtures:
            result = super().run(test)
        wall_s = time.perf_counter() - _STARTED_AT
        if isinstance(self.destination, TimesOutput):
            log = self.destination.config_log
            reads = log.read_text(encoding="utf-8").splitlines() if isinstance(log, Path) else log
            write_times(self.destination.path, cast(TimedResult, result), wall_s, fixtures, reads)
        return result


def main() -> None:
    try:
        from scripts.lib import conf_reads
    except ImportError:
        from importlib.util import find_spec

        if find_spec("scripts.lib.conf_reads") is not None:
            raise
        run_module(NoConfigReadLog())
        return

    from scripts.lib import build_folders

    with build_folders.tool_folder("config-reads") as temporary:
        log = Path(temporary) / "reads"
        log.touch()
        with conf_reads.record_reads(log):
            run_module(log)


def run_module(config_log: Path | NoConfigReadLog) -> None:
    if len(sys.argv) != 3:
        print("usage: module_run.py <start_dir> <pattern>", file=sys.stderr)
        sys.exit(2)
    start_dir, pattern = sys.argv[1:]
    output = os.environ.pop("TEST_TIMES_OUT", None)
    TimedRunner.destination = NoTimesOutput() if output is None else TimesOutput(Path(output), config_log)
    forbidden_config_paths: set[str] = set()
    try:
        names: list[str] = []
        for path in sorted(Path(start_dir).resolve().glob(pattern)):
            candidates: list[tuple[str, ...]] = []
            for entry in sys.path:
                base = Path(entry).resolve()
                if path.is_relative_to(base):
                    parts = path.relative_to(base).with_suffix("").parts
                    if all(part.isidentifier() for part in parts):
                        candidates.append(parts)
            names.append(".".join(max(candidates, key=len, default=(path.stem,))))
        with ExitStack() as cleanup:
            TimedRunner.scanned_folders = set()
            TimedRunner.read_contents = set()
            audit = RepositoryReadAudit(Path(__file__).absolute().parents[2], TimedRunner.scanned_folders,
                                        TimedRunner.read_contents, forbidden_config_paths)
            audit.install(cleanup)
            suite = unittest.defaultTestLoader.loadTestsFromNames(names)
            result = TimedRunner(warnings=None if cast(list[str], sys.warnoptions) else "default").run(suite)
        sys.exit(1 if not result.wasSuccessful() else 5 if result.testsRun == 0 and not result.skipped else 0)
    except SystemExit:
        if forbidden_config_paths:
            paths = ", ".join(sorted(forbidden_config_paths))
            print(f"{pattern}: use fixed test configs instead of reading {paths}", file=sys.stderr)
            sys.exit(1)
        if "scripts.message.session_send_test_support" in sys.modules:
            from scripts.message import session_send_test_support

            if session_send_test_support.real_sender_was_started():
                print(f"{pattern}: started the real scripts/message/send.py", file=sys.stderr)
                sys.exit(1)
        raise


@contextmanager
def private_run_roots() -> Generator[Callable[[], None]]:
    """Keep imported run roots and child processes under one private folder."""
    from scripts.lib import build_folders

    names = ("SCRATCH_ROOT", "ASTRA_REVIEW_DIR")
    previous = {name: os.environ[name] for name in names if name in os.environ}
    with ExitStack() as cleanup:
        root = cleanup.enter_context(build_folders.tool_folder("test-run-roots"))
        try:
            os.environ.update(SCRATCH_ROOT=str(root / "scratch"), ASTRA_REVIEW_DIR=str(root / "astra"))
            yield cleanup.close
        finally:
            for name in names:
                _ = os.environ.pop(name, None)
            os.environ.update(previous)


def run_with_descendant_cleanup(cleanup_run_roots: Callable[[], None]) -> None:
    """Use kernel events while the supervisor reaps every adopted descendant."""
    global startup_cpu_s
    from scripts.lib.descendants import adopt_descendants

    if not adopt_descendants():
        main()
        return
    libc = ctypes.CDLL(None, use_errno=True)
    watched = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGCHLD)
    blocked = signal.pthread_sigmask(signal.SIG_BLOCK, watched)
    previous_sigchld = signal.signal(signal.SIGCHLD, signal.SIG_DFL)
    signal_set = (ctypes.c_ulong * 16)()
    _ = cast(int, libc.sigemptyset(ctypes.byref(signal_set)))
    for number in watched:
        _ = cast(int, libc.sigaddset(ctypes.byref(signal_set), number))
    signal_fd = cast(int, libc.signalfd(-1, ctypes.byref(signal_set), os.O_CLOEXEC | os.O_NONBLOCK))
    if signal_fd < 0:
        raise OSError(ctypes.get_errno(), "cannot watch runner signals")
    parent_fds: list[int] = []
    parent = os.environ.pop("MERGE_GATE_PARENT_PID", "")
    if parent:
        # No test child exists before the parent-death handoff completes.
        parent_fds.append(os.pidfd_open(int(parent)))
        if cast(int, libc.prctl(1, 0, 0, 0, 0)) != 0:
            raise OSError(ctypes.get_errno(), "cannot watch the gate parent")
    usage = resource.getrusage(resource.RUSAGE_SELF)
    supervisor = os.getpid()
    test_pid = os.fork()
    if test_pid == 0:
        for descriptor in [signal_fd, *parent_fds]:
            os.close(descriptor)
        if cast(int, libc.prctl(1, signal.SIGKILL, 0, 0, 0)) != 0:
            raise OSError(ctypes.get_errno(), "cannot set the test parent death signal")
        if os.getppid() != supervisor:
            os.kill(os.getpid(), signal.SIGKILL)
        startup_cpu_s = usage.ru_utime + usage.ru_stime
        _ = signal.signal(signal.SIGCHLD, previous_sigchld)
        _ = signal.pthread_sigmask(signal.SIG_SETMASK, blocked)
        main()
        return
    status = 0
    received = 0
    test_fd = os.pidfd_open(test_pid)
    try:
        while True:
            ready, _, _ = select.select([signal_fd, test_fd, *parent_fds], [], [])
            if any(descriptor in ready for descriptor in parent_fds):
                received = signal.SIGKILL
                break
            if signal_fd in ready:
                number = int.from_bytes(os.read(signal_fd, 128)[:4], sys.byteorder)
                if number != signal.SIGCHLD:
                    received = number
                    break
            ended = False
            while True:
                pid, child_status = os.waitpid(-1, os.WNOHANG)
                if pid == 0:
                    break
                if pid == test_pid:
                    status = child_status
                    ended = True
                    break
            if ended:
                break
    finally:
        while True:
            children = Path(f"/proc/self/task/{os.getpid()}/children").read_text().split()
            for child in children:
                try:
                    os.kill(int(child), signal.SIGKILL)
                except ProcessLookupError:
                    pass
            try:
                pid, child_status = os.waitpid(-1, 0)
            except ChildProcessError:
                break
            if pid == test_pid:
                status = child_status
        for descriptor in [signal_fd, test_fd, *parent_fds]:
            os.close(descriptor)
    exit_code = os.waitstatus_to_exitcode(status)
    if received or exit_code < 0:
        number = received or -exit_code
        cleanup_run_roots()
        if number not in (signal.SIGKILL, signal.SIGSTOP):
            _ = signal.signal(number, signal.SIG_DFL)
        os.kill(os.getpid(), number)
        _ = signal.pthread_sigmask(signal.SIG_UNBLOCK, {number})
    _ = signal.pthread_sigmask(signal.SIG_SETMASK, blocked)
    sys.exit(exit_code)


if __name__ == "__main__":
    with private_run_roots() as cleanup_run_roots:
        run_with_descendant_cleanup(cleanup_run_roots)
