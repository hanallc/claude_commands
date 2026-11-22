"""Select one checkout for this process and preserve other startup hooks.

The hook also points the method compile of `dataclasses` at a shared code cache.
dataclasses compiles the generated methods of each class from source at import.
On 2026-11-14 that compile was 45-80% of the import CPU of the hot CLIs
(direct.assign 278 of 428 ms). The cache keeps the marshal code of each source
in /var/tmp/dataclass-code-<uid>/<cache tag>. tmp_sweep.py prunes the entries that no
process read in lint.conf `dataclass_code_days` days.
"""

from __future__ import annotations

import os
import stat
import sys
import time
from collections.abc import Callable, Sequence
from importlib.machinery import ModuleSpec, PathFinder
from importlib.util import module_from_spec
from types import CodeType, ModuleType

# Keep typing out of interpreter startup.
TYPE_CHECKING = False
if TYPE_CHECKING:
    from typing import Literal, Protocol

    class _CheckoutStartupAttributes(Protocol):
        def __contains__(self, key: Literal["ready"], /) -> bool: ...
        def pop(self, key: Literal["ready"], /) -> Callable[[], None]: ...
        def setdefault(self, key: Literal["startup_hooks"], default: list[ModuleType], /) -> list[ModuleType]: ...


STARTUP_FILES = ("scripts/lib/py", "scripts/lib/package_root.py", "scripts/lib/python_startup/sitecustomize.py")
TEST_RUNNER_FILES = (*STARTUP_FILES, "scripts/test_times/module_run.py", "scripts/lib/descendants.py",
                     "scripts/lib/conf_reads.py", "scripts/lib/build_folders.py", "scripts/lib/build_folder_lock.py",
                     "scripts/lib/boot_identity.py", "scripts/lib/process_start.py",
                     "scripts/lib/conf.py", "scripts/lib/git_files.py", "config/build_folders.conf",
                     "config/test_times.conf")
_DIRECT_TEST_UNIT_PREFIX = "claude-direct-test-"
_SESSION_STARTUP = "session_python_startup"
_DIRECT_TEST_LIMIT_KEY = "direct_test.time_limit_seconds"
GATE_PARENT_SOURCE_FD = "DEVTOOLS_GATE_PARENT_SOURCE_FD"
DATACLASS_CODE_FILENAME = "<string>"
# dataclasses.py has no __future__ import, so its exec compiles with no flags.
DATACLASS_CODE_FLAGS = 0
# A read refreshes an entry's time once a day, so the prune age counts in days.
DATACLASS_CODE_TOUCH_SECONDS = 86400
_dataclass_code_folder: list[str] = []


def checkout_root(start: str) -> str:
    """Find the checkout that contains a script or working directory."""
    folder = os.path.realpath(start)
    while folder:
        if os.path.isfile(os.path.join(folder, "scripts/lib/py")):
            return folder
        parent = os.path.dirname(folder)
        if parent == folder:
            break
        folder = parent
    return ""


def inherited_paths(value: str) -> list[str]:
    """Keep generic paths and remove checkout roots and startup directories."""
    return [entry for entry in value.split(os.pathsep) if value
            and not os.path.isdir(os.path.join(entry or os.curdir, "scripts"))
            and not entry.rstrip(os.sep).endswith(os.sep + "lib/python_startup")]


def startup_pythonpath(inherited: str) -> str:
    """Keep general Python paths and put this startup hook first."""
    # Older copies skip paths that end in lib/python_startup.
    library = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
    other_paths = (entry for entry in inherited_paths(inherited) if not _session_startup(entry))
    return os.pathsep.join((os.path.join(library, _SESSION_STARTUP), *other_paths))


def _session_startup(entry: str) -> bool:
    return entry.rstrip(os.sep).endswith(os.sep + "lib" + os.sep + _SESSION_STARTUP)


def _selection_inputs(paths: list[str]) -> tuple[tuple[str, ...], str, bool, str]:
    try:
        directory = os.getcwd()
    except OSError:
        directory = ""
    return tuple(paths), os.environ.get("PYTHONPATH", ""), "PYTHONPATH" in os.environ, directory


def select_checkout(arguments: list[str]) -> str:
    """Select the script checkout, working checkout, or helper checkout once."""
    state = sys.modules.setdefault("_devtools_checkout", ModuleType("_devtools_checkout"))
    if not hasattr(state, "__path__"):
        state.__path__ = []
    selected = next(iter(state.__path__), "")
    if selected and state.__dict__.get("completed_selection") == (selected, _selection_inputs(sys.path)):
        return selected
    if not selected:
        script = arguments[0] if arguments else ""
        selected = checkout_root(os.path.dirname(os.path.abspath(script))) if script not in {"", "-", "-c", "-m"} else ""
        selected = selected or checkout_root(os.getcwd())
        selected = selected or os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__)))))
        state.__path__ = [selected]
    paths = inherited_paths(os.environ.get("PYTHONPATH", ""))
    if paths:
        os.environ["PYTHONPATH"] = os.pathsep.join(paths)
    else:
        _ = os.environ.pop("PYTHONPATH", None)
    # Keep the entry script's folder before package imports.
    paths = [entry for entry in sys.path
             if os.path.realpath(entry) == selected
             or (not os.path.isdir(os.path.join(entry or os.curdir, "scripts"))
                 and not entry.rstrip(os.sep).endswith(os.sep + "lib/python_startup")
                 and not _session_startup(entry))]
    if not any(os.path.realpath(entry) == selected for entry in paths):
        script_folder = os.path.dirname(os.path.abspath(arguments[0])) if arguments else ""
        position = int(bool(paths) and os.path.realpath(paths[0]) == script_folder and not getattr(sys.flags, "safe_path", False))
        paths.insert(position, selected)
    sys.path[:] = paths
    state.__dict__["completed_selection"] = (selected, _selection_inputs(sys.path))
    return selected


def dataclass_code_root() -> str:
    """Return this user's dataclass code cache root."""
    return f"/var/tmp/dataclass-code-{os.getuid()}"


def dataclass_code_usable(root: str, uid: int) -> bool:
    """Whether root is a folder that uid owns and that no other user can open."""
    try:
        status = os.lstat(root)
    except OSError:
        return False
    return stat.S_ISDIR(status.st_mode) and status.st_uid == uid and not status.st_mode & 0o077


def dataclass_code_folder(root: str) -> str:
    """Make the cache folder of this Python under root; return "" when the cache stays off."""
    try:
        os.mkdir(root, 0o700)
    except OSError:
        pass
    if not dataclass_code_usable(root, os.getuid()):
        return ""
    folder = os.path.join(root, sys.implementation.cache_tag)
    try:
        os.mkdir(folder, 0o700)
    except FileExistsError:
        pass
    except OSError:
        return ""
    return folder


def dataclass_code(source: str, folder: str) -> CodeType:
    """Return the code that the dataclasses exec compiles from source, from folder when it holds it.

    A missing, truncated or corrupt entry compiles again and replaces the file.
    """
    import hashlib
    import marshal

    key = hashlib.sha1(f"{DATACLASS_CODE_FLAGS} {sys.flags.optimize}\n{source}".encode()).hexdigest()
    path = os.path.join(folder, key)
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        descriptor = -1
    if descriptor >= 0:
        try:
            status = os.fstat(descriptor)
            data = os.read(descriptor, status.st_size)
        except OSError:
            status, data = None, b""
        finally:
            os.close(descriptor)
        try:
            cached: object = marshal.loads(data)  # pyright: ignore[reportAny]
        except (EOFError, ValueError, TypeError):
            cached = None
        if isinstance(cached, CodeType):
            if status is not None and status.st_mtime < time.time() - DATACLASS_CODE_TOUCH_SECONDS:
                try:
                    os.utime(path)
                except OSError:
                    pass
            return cached
    code = compile(source, DATACLASS_CODE_FILENAME, "exec", DATACLASS_CODE_FLAGS, dont_inherit=True)
    partial = f"{path}.{os.getpid()}"
    try:
        descriptor = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            _ = os.write(descriptor, marshal.dumps(code))
        finally:
            os.close(descriptor)
        os.replace(partial, path)
    except OSError:
        try:
            os.unlink(partial)
        except OSError:
            pass
    return code


def dataclass_exec(source: str, globals_: dict[str, object], locals_: dict[str, object]) -> None:
    """Run the generated method source of dataclasses with code from the shared cache."""
    if not _dataclass_code_folder:
        _dataclass_code_folder.append(dataclass_code_folder(dataclass_code_root()))
    folder = _dataclass_code_folder[0]
    code = (dataclass_code(source, folder) if folder
            else compile(source, DATACLASS_CODE_FILENAME, "exec", DATACLASS_CODE_FLAGS, dont_inherit=True))
    exec(code, globals_, locals_)


class DataclassCodeFinder:
    """Set `exec` in `dataclasses` to dataclass_exec when a process first imports it."""

    def find_spec(self, name: str, path: Sequence[str] | None, target: ModuleType | None = None, /) -> ModuleSpec | None:
        if name != "dataclasses":
            return None
        sys.meta_path.remove(self)
        spec = PathFinder.find_spec(name, path, target)
        loader = spec.loader if spec is not None else None
        if spec is None or loader is None:
            return spec
        load = loader.exec_module

        def exec_module(module: ModuleType) -> None:
            load(module)
            module.__dict__["exec"] = dataclass_exec

        vars(loader)["exec_module"] = exec_module
        return spec


def start_process(arguments: list[str]) -> str:
    """Select the checkout and register the direct test boundary."""
    selected = select_checkout(arguments)
    if (os.environ.get("CLAUDE_DISABLE_DATACLASS_CODE_CACHE") != "1"
            and "dataclasses" not in sys.modules
            and not any(type(finder).__name__ == DataclassCodeFinder.__name__ for finder in sys.meta_path)):
        sys.meta_path.insert(0, DataclassCodeFinder())
    _install_unittest_boundary()
    return selected


class UnverifiedGateParent:
    """The process has no verified or trusted gate parent."""


class TrustedGateParent:
    """The platform trusts the gate parent from the environment."""

    def __init__(self, pid: str) -> None:
        self.pid: str = pid


class VerifiedGateParentWithoutDescriptor:
    """The process verified the gate parent and holds no source descriptor."""

    def __init__(self, pid: str) -> None:
        self.pid: str = pid


class VerifiedGateParentWithDescriptor:
    """The process verified the gate parent and holds its source descriptor."""

    def __init__(self, pid: str, descriptor: int) -> None:
        self.pid: str = pid
        self.descriptor: int = descriptor


if TYPE_CHECKING:
    GateParent = (UnverifiedGateParent | TrustedGateParent
                  | VerifiedGateParentWithoutDescriptor | VerifiedGateParentWithDescriptor)


def _gate_source_descriptor(raw: str) -> int:
    return int(raw) if raw.isascii() and raw.isdecimal() and len(raw) <= 10 else -1


def _gate_source_stat(parent: str, descriptor: int) -> os.stat_result:
    path = f"/proc/{parent}/fd/{descriptor}"
    target = os.readlink(path)
    if not target.endswith("/scripts/production/gate.py") or not checkout_root(os.path.dirname(target)):
        raise ValueError("the descriptor does not name a gate source file")
    return os.stat(path)


def _gate_source_identity(parent: str) -> VerifiedGateParentWithDescriptor | UnverifiedGateParent:
    """Check that the gate passed an open descriptor for its source file."""
    descriptor = _gate_source_descriptor(os.environ.get(GATE_PARENT_SOURCE_FD, ""))
    if descriptor < 3:
        return UnverifiedGateParent()
    try:
        opened = os.fstat(descriptor)
        if stat.S_ISREG(opened.st_mode) and os.path.samestat(_gate_source_stat(parent, descriptor), opened):
            return VerifiedGateParentWithDescriptor(parent, descriptor)
    except (OSError, ValueError):
        pass
    return UnverifiedGateParent()


def _gate_command(pid: str) -> bool:
    """Recognize an installed gate that starts a module from a newer checkout."""
    import re
    from pathlib import Path
    from pwd import getpwuid

    proc = Path("/proc", pid)
    try:
        if re.fullmatch(r"python(?:[0-9]+(?:\.[0-9]+)?)?", (proc / "exe").readlink().name) is None:
            return False
        command = (proc / "cmdline").read_bytes().rstrip(b"\0").split(b"\0")
        arguments = iter(os.fsdecode(item) for item in command[1:])
        cwd = (proc / "cwd").resolve(strict=True)
        for item in arguments:
            if item in ("-P", "-B", "-u"):
                continue
            if item == "-m":
                item = next(arguments, "").replace(".", "/") + ".py"
            elif item.startswith("-"):
                return False
            entry = (cwd / item).resolve(strict=True)
            installed = Path(getpwuid(os.getuid()).pw_dir, ".claude").resolve()
            roots = (Path(__file__).resolve().parents[3], installed)
            return (entry.name in {"gate.py", "merge_gate.py", "targeted_test.py", "merge_checkpoint.py"}
                    and any(entry.parent == root / "scripts/production" for root in roots))
    except (KeyError, OSError, ValueError):
        pass
    return False


def _gate_identity() -> GateParent:
    parent = os.environ.get("GATE_PARENT_PID") or os.environ.get("MERGE_GATE_PARENT_PID", "")
    if not parent:
        return UnverifiedGateParent()
    if sys.platform != "linux":
        return TrustedGateParent(parent)
    if parent != str(os.getppid()):
        return UnverifiedGateParent()
    identity = _gate_source_identity(parent)
    if isinstance(identity, UnverifiedGateParent) and _gate_command(parent):
        return VerifiedGateParentWithoutDescriptor(parent)
    return identity


def _remember_gate(parent: str) -> bool:
    state = sys.modules.setdefault("_devtools_checkout", ModuleType("_devtools_checkout"))
    if "test_gate_pidfd" not in state.__dict__:
        try:
            state.__dict__["test_gate_pidfd"] = os.pidfd_open(int(parent))
        except ProcessLookupError:
            return False
    return True


def gate_parent(consume: bool = False) -> GateParent:
    """Identify the gate parent and manage the source descriptor."""
    identity = _gate_identity()
    accepted = (isinstance(identity, TrustedGateParent)
                or (not isinstance(identity, UnverifiedGateParent) and _remember_gate(identity.pid)))
    if consume or not accepted:
        _ = os.environ.pop("GATE_PARENT_PID", None)
        _ = os.environ.pop("MERGE_GATE_PARENT_PID", None)
        _ = os.environ.pop(GATE_PARENT_SOURCE_FD, None)
        if isinstance(identity, VerifiedGateParentWithDescriptor):
            os.close(identity.descriptor)
            identity = VerifiedGateParentWithoutDescriptor(identity.pid)
    elif isinstance(identity, VerifiedGateParentWithDescriptor):
        os.set_inheritable(identity.descriptor, False)
    return identity if accepted else UnverifiedGateParent()


def _unittest_boundary(module: ModuleType) -> None:
    """Start the limit when the process first loads or runs tests."""
    from _thread import allocate_lock
    from functools import wraps
    from unittest import TestCase, TestLoader, TestProgram, TestSuite, TextTestRunner
    from unittest.suite import BaseTestSuite

    if module.__dict__.get("_devtools_test_boundary"):
        return
    module.__dict__["_devtools_test_boundary"] = True
    lock = allocate_lock()
    started = False

    def begin() -> None:
        nonlocal started
        if not started:
            with lock:
                if not started:
                    _begin_direct_test()
                    started = True

    def wrap(owner: type[object], method: Callable[..., object]) -> None:
        @wraps(method)
        def run(*args: object, **kwargs: object) -> object:
            begin()
            return method(*args, **kwargs)

        setattr(owner, method.__name__, run)

    for owner, methods in (
        (TestLoader, (TestLoader.discover, TestLoader.loadTestsFromModule, TestLoader.loadTestsFromName,
                      TestLoader.loadTestsFromNames, TestLoader.loadTestsFromTestCase)),
        (TestProgram, (TestProgram.runTests,)),
        (TextTestRunner, (TextTestRunner.run,)),
        (BaseTestSuite, (BaseTestSuite.run, BaseTestSuite.debug)),
        (TestSuite, (TestSuite.run, TestSuite.debug)),
        (TestCase, (TestCase.run, TestCase.debug)),
    ):
        for method in methods:
            wrap(owner, method)


class UnittestBoundary:
    """Install the test boundary when Python imports unittest."""

    def find_spec(self, name: str, path: Sequence[str] | None, target: ModuleType | None = None, /) -> ModuleSpec | None:
        if name != "unittest":
            return None
        spec = PathFinder.find_spec(name, path, target)
        loader = spec.loader if spec is not None else None
        if loader is not None:
            load = loader.exec_module

            def exec_module(module: ModuleType) -> None:
                sys.meta_path.remove(self)
                load(module)
                _unittest_boundary(module)

            vars(loader)["exec_module"] = exec_module
        return spec


def _install_unittest_boundary() -> None:
    state = sys.modules["_devtools_checkout"]
    if state.__dict__.get("unittest_boundary"):
        return
    state.__dict__["unittest_boundary"] = True
    if "unittest" in sys.modules:
        _unittest_boundary(sys.modules["unittest"])
    else:
        sys.meta_path.insert(0, UnittestBoundary())


def _verified_gate() -> bool:
    """Check for a trusted parent or a parent proof that can pass through a fork."""
    import select

    state = sys.modules.get("_devtools_checkout")
    running_gate: object = state.__dict__.get("test_gate_pidfd") if state is not None else None
    if isinstance(running_gate, int):
        readable, _, _ = select.select([running_gate], [], [], 0)
        if readable:
            return False
        if "GATE_PARENT_PID" in os.environ or "MERGE_GATE_PARENT_PID" in os.environ:
            _ = gate_parent()
        return True
    return not isinstance(gate_parent(), UnverifiedGateParent)


def _launching_gate(parent: str, descriptor: int) -> bool:
    if descriptor >= 3:
        try:
            if stat.S_ISREG(_gate_source_stat(parent, descriptor).st_mode):
                return True
        except (OSError, ValueError):
            pass
    return _gate_command(parent)


def _descends_from_gate_module() -> bool:
    """Use the module's exec environment when a descendant replaces its environment."""
    pid = os.getppid()
    while pid > 1:
        try:
            with open(f"/proc/{pid}/stat", "rb") as source:
                fields = source.read().rpartition(b") ")[2].split()
            parent = int(fields[1])
            session = int(fields[3])
        except (OSError, IndexError, ValueError):
            return False
        # The gate starts each module in a new session.
        if pid == session:
            try:
                with open(f"/proc/{pid}/environ", "rb") as source:
                    environment = dict(item.split(b"=", 1) for item in source.read().split(b"\0") if b"=" in item)
                parent_text = str(parent)
                expected = environment.get(b"GATE_PARENT_PID") or environment.get(b"MERGE_GATE_PARENT_PID", b"")
                if expected == parent_text.encode():
                    raw = environment.get(os.fsencode(GATE_PARENT_SOURCE_FD), b"")
                    if _launching_gate(parent_text, _gate_source_descriptor(os.fsdecode(raw))):
                        return True
            except OSError:
                pass
        pid = parent
    return False


def within_test_limit() -> bool:
    """Identify a gate module or an existing direct test scope."""
    if _verified_gate():
        return True
    if sys.platform != "linux":
        return False
    try:
        if _direct_test_unit(_cgroup(os.getpid())):
            return True
    except OSError:
        pass
    return _descends_from_gate_module()


def _begin_direct_test() -> None:
    if within_test_limit():
        return
    from contextlib import AbstractContextManager, nullcontext
    from pathlib import Path
    from typing import cast

    linux = sys.platform == "linux"
    # An older checkout can predate the private test folder.
    checkout = Path(sys.modules["_devtools_checkout"].__path__[0])
    has_test_folder = (checkout / "scripts/lib/testing_folders.py").is_file()
    if not (linux or has_test_folder):
        return

    from scripts.lib import conf

    # Older checkouts may lack the recorder or this API.
    recorder = sys.modules.get("scripts.lib.conf_reads")
    without_recording = cast(Callable[[], AbstractContextManager[None]],
                             getattr(recorder, "without_recording", nullcontext))

    try:
        with without_recording():
            if linux:
                settings = conf.read("test_times")
                limit = settings.origin(_DIRECT_TEST_LIMIT_KEY, conf.positive_whole)
                if isinstance(limit, conf.UnavailableValue):
                    # The enforcing hook can come from a newer checkout than the test.
                    template = Path(__file__).resolve().parents[3] / "config/test_times.conf"
                    seconds = conf.read(template).value(_DIRECT_TEST_LIMIT_KEY, conf.positive_whole)
                else:
                    seconds = limit.value
                _adopt_direct_test_scope(seconds)
            if has_test_folder:
                from scripts.lib.testing_folders import start_test_run

                start_test_run()
    except (OSError, conf.Unusable, ValueError) as error:
        print(f"direct test: {error}", file=sys.stderr, flush=True)
        raise SystemExit(1) from None


def _cgroup(pid: int) -> str:
    with open(f"/proc/{pid}/cgroup", encoding="utf-8") as rows:
        return next((row[3:].strip() for row in rows if row.startswith("0::")), "")


def _direct_test_unit(cgroup: str) -> str:
    import re

    unit = os.path.basename(cgroup)
    return unit if re.fullmatch(_DIRECT_TEST_UNIT_PREFIX + r"[0-9a-f]{32}\.scope", unit) else ""


def _start_transient_unit(unit: str, properties: Sequence[Sequence[str]], descriptors: tuple[int, ...]) -> None:
    import subprocess

    service = "org.freedesktop.systemd1"
    command = ["busctl", "--user", "--", "call", service, "/org/freedesktop/systemd1", service + ".Manager",
               "StartTransientUnit", "ssa(sv)a(sa(sv))", unit, "fail", str(len(properties)),
               *(field for property_fields in properties for field in property_fields), "0"]
    result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            pass_fds=descriptors, check=False)
    if result.returncode:
        raise ValueError(f"systemd could not start {unit}")


def _adopt_direct_test_scope(seconds: int) -> None:
    """Move this process into a deadline scope at the first unittest call.

    Known limit: a child that started before this call stays outside the scope.
    The deadline does not stop that child.
    """
    from contextlib import ExitStack
    from secrets import token_hex

    pid = os.getpid()
    arguments = [sys.executable, "-IS", os.path.realpath(__file__), str(pid), str(seconds)]
    unit = f"{_DIRECT_TEST_UNIT_PREFIX}{token_hex(16)}"
    reader, writer = os.pipe()
    with os.fdopen(reader, encoding="utf-8") as ready, ExitStack() as close:
        _ = close.callback(os.close, writer)
        descriptor = os.pidfd_open(pid)
        _ = close.callback(os.close, descriptor)
        errors = os.dup(2)
        _ = close.callback(os.close, errors)
        _start_transient_unit(unit + ".service", (
            ("Type", "s", "exec"),
            ("CollectMode", "s", "inactive-or-failed"),
            ("ExecStartEx", "a(sasas)", "1", sys.executable, str(len(arguments)), *arguments, "1", "no-env-expand"),
            ("StandardInputFileDescriptor", "h", str(descriptor)),
            ("StandardOutputFileDescriptor", "h", str(writer)),
            ("StandardErrorFileDescriptor", "h", str(errors)),
        ), (descriptor, writer, errors))
        close.close()
        scope = ready.readline().strip()
    if not scope or scope != _direct_test_unit(_cgroup(pid)):
        raise ValueError("systemd did not place the process in a direct test scope")


def _settle_direct_test_scope(unit: str) -> bool:
    """Stop any descendants and read the result before systemd removes the scope."""
    import subprocess

    show = subprocess.run(["systemctl", "--user", "show", "--property=Result,ActiveState", "--", unit],
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, check=False)
    state = show.stdout.splitlines()
    if "ActiveState=failed" in state:
        _ = subprocess.run(["systemctl", "--user", "reset-failed", "--", unit],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    elif "ActiveState=active" in state or "ActiveState=deactivating" in state:
        _ = subprocess.run(["systemctl", "--user", "stop", "--", unit],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    return "Result=timeout" in state


def _observe_direct_test(pid: int, seconds: int) -> None:
    """Set the scope limit and report an expired limit after the test exits."""
    import select
    import signal
    import subprocess
    from secrets import token_hex

    unit = f"{_DIRECT_TEST_UNIT_PREFIX}{token_hex(16)}.scope"
    cgroup = _cgroup(pid)
    slice_name = next((part for part in reversed(cgroup.split("/")) if part.endswith(".slice")), "app.slice")
    # The manager passes the main process pidfd as the observer's standard input.
    descriptor = os.dup(0)
    try:
        _start_transient_unit(unit, (
            ("PIDFDs", "ah", "1", str(descriptor)),
            ("Slice", "s", slice_name),
            ("RuntimeMaxUSec", "t", str(seconds * 1_000_000)),
            ("KillSignal", "i", str(signal.SIGKILL.value)),
        ), (descriptor,))
        _ = subprocess.run(["systemctl", "--user", "start", "--", unit], check=True)
        print(unit, flush=True)
        _ = select.select([descriptor], [], [])
        if _settle_direct_test_scope(unit):
            print(f"stopped at the {seconds}-second direct test limit; run longer tests with targeted_test.py --module",
                  file=sys.stderr, flush=True)
    finally:
        os.close(descriptor)


def after_startup(callback: Callable[[], None]) -> None:
    """Start an observer after the outer startup hook finishes."""
    sys.modules["_devtools_checkout"].__dict__["ready"] = callback


def finish_startup(filename: str, arguments: list[str]) -> None:
    """Finish path selection before Python adds its automatic search directory."""
    hook = sys.modules.get("sitecustomize")
    if hook is None or not hook.__file__ or os.path.realpath(hook.__file__) != os.path.realpath(filename):
        return
    selected = select_checkout(arguments)
    automatic_paths = sys.path
    if not getattr(sys.flags, "safe_path", False):
        script = arguments[0] if arguments else ""
        automatic = os.getcwd() if script in {"", "-", "-c", "-m"} else os.path.dirname(os.path.abspath(script))
        if os.path.realpath(automatic) == selected:
            sys.path[:] = [entry for entry in sys.path if os.path.realpath(entry) != selected]
            automatic_paths = [automatic, *sys.path]
        elif (not os.path.isdir(os.path.join(automatic, "scripts"))
              and not automatic.rstrip(os.sep).endswith(os.sep + "lib/python_startup")):
            automatic_paths = [automatic, *sys.path]
    state = sys.modules["_devtools_checkout"]
    # Python adds its automatic path after this hook returns.
    state.__dict__["completed_selection"] = (selected, _selection_inputs(automatic_paths))
    attributes: _CheckoutStartupAttributes = state.__dict__
    if "ready" in attributes:
        attributes.pop("ready")()


def _retained_startup_hooks(attributes: _CheckoutStartupAttributes) -> list[ModuleType]:
    return attributes.setdefault("startup_hooks", [])


def next_sitecustomize(filename: str, search: list[str] | None = None) -> None:
    """Run the next hook after this hook's directory."""
    directory = os.path.realpath(os.path.dirname(filename))
    remaining: list[str] = []
    passed_own = False
    for entry in sys.path if search is None else search:
        if os.path.realpath(entry) == directory:
            passed_own = True
        elif passed_own and not entry.rstrip(os.sep).endswith(os.sep + "lib/python_startup"):
            # Continue past redundant copies of this startup helper.
            remaining.append(entry)
    spec = PathFinder.find_spec("sitecustomize", remaining)
    if spec is not None and spec.loader is not None:
        module = module_from_spec(spec)
        # Keep hook contexts active for the process lifetime.
        hooks = _retained_startup_hooks(sys.modules["_devtools_checkout"].__dict__)
        spec.loader.exec_module(module)
        hooks.append(module)


# Reuse this module when an entry loads package_root.py.
if __name__ in sys.modules:
    _ = sys.modules.setdefault("_devtools_python_startup", sys.modules[__name__])


if __name__ == "sitecustomize":
    # Save the hook order before selection removes this startup directory.
    following = sys.path[:]
    _ = start_process(sys.argv)
    next_sitecustomize(__file__, following)
    finish_startup(__file__, sys.argv)


if __name__ == "__main__":
    _observe_direct_test(int(sys.argv[1]), int(sys.argv[2]))
