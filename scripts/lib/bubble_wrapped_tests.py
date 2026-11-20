"""Start tests in Bubblewrap on natedev."""

from __future__ import annotations

from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
import errno
import fcntl
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from typing import NamedTuple, NoReturn

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.lib import conf
from scripts.lib.python_startup.sitecustomize import kernel_guarded


CHECKOUT = Path(__file__).resolve().parents[2]
SYSTEM = Path("/run/current-system/sw")
CONFIG = CHECKOUT / "config/bubble_wrapped_tests.conf"
HOME = "/home/test"
MODULE_VALUES = (
    "BUILDLOG_DIR", "BUILDLOG_LIVE_MEMORY_TRIPWIRE", "HERDR_BIN", "HERDR_PANE_RECORD_DIR",
    "SESSION_TEMP_ROOT", "STAFF_FILE", "TEST_TIMES_OUT",
)


class BubblewrapUnavailable(RuntimeError):
    """The test cannot start in Bubblewrap."""


class GateInputs(NamedTuple):
    checkout: Path
    environment: Mapping[str, str]
    read_only: Sequence[Path]


_gate_inputs: ContextVar[GateInputs | None] = ContextVar("guard_gate_inputs", default=None)


@contextmanager
def gate_inputs(checkout: Path, environment: Mapping[str, str], *,
                read_only: Sequence[Path]) -> Generator[None]:
    """Supply trusted paths and settings for one gate job."""
    token = _gate_inputs.set(GateInputs(checkout, environment, read_only))
    try:
        yield
    finally:
        _gate_inputs.reset(token)


def guarded() -> bool:
    """Check the mount, PID, and network namespaces."""
    return kernel_guarded()


def host_environment(environment: Mapping[str, str]) -> dict[str, str]:
    """Keep only the settings that the host scope and bootstrap need."""
    try:
        return _kept_environment(conf.read(CONFIG), environment)
    except conf.Unusable as error:
        raise BubblewrapUnavailable(str(error)) from error


def _kept_environment(settings: conf.ConfigRead, environment: Mapping[str, str]) -> dict[str, str]:
    keys = settings.words("kept_environment")
    return {key: environment[key] for key in keys if key in environment}


def command(argv: Sequence[str], job_folder: Path, *, lease_fd: int | None = None) -> list[str]:
    """Build a guarded command. The caller owns the job folder lease."""
    if not argv:
        raise BubblewrapUnavailable("a guarded command requires an executable")
    try:
        settings = conf.read(CONFIG)
        inputs = _gate_inputs.get()
        source = os.environ if inputs is None else inputs.environment
        values = _kept_environment(settings, source)
        values.update({key: source[key] for key in MODULE_VALUES if key in source})
        tmp = str(job_folder.resolve(strict=True))
        values.update(TMPDIR=tmp, TMP=tmp, TEMP=tmp, GATE_PARENT_PID="1")
        if source.get("COVERAGE_OUT") == f"{tmp}/coverage.json":
            values["COVERAGE_OUT"] = f"{tmp}/coverage.json"
            if "COVERAGE_TREE_SHA" in source:
                values["COVERAGE_TREE_SHA"] = source["COVERAGE_TREE_SHA"]
        _ = values.pop("DBUS_SESSION_BUS_ADDRESS", None)
        _ = values.setdefault("BUILDLOG_DIR", f"{tmp}/buildlog")
        _ = values.setdefault("SESSION_TEMP_ROOT", f"{tmp}/session")
        if guarded():
            # The kernel ends nested processes when PID 1 of the outer job exits.
            return ["/usr/bin/env", "-i", *(f"{key}={value}" for key, value in values.items()), *argv]

        executable = Path(settings.text("bubblewrap", recover_invalid=False))
        if executable != SYSTEM / "bin/bwrap" or not executable.is_file() or not os.access(executable, os.X_OK):
            raise BubblewrapUnavailable(f"Bubblewrap is unavailable at {SYSTEM / 'bin/bwrap'}")
        checkout = Path.cwd().resolve() if inputs is None else inputs.checkout.resolve()
        working = checkout
        if inputs is None:
            for parent in (working, *working.parents):
                if (parent / "scripts/lib/py").is_file():
                    checkout = parent
                    break
            else:
                checkout = CHECKOUT
                working = CHECKOUT
        folder = Path(tmp)
        if (not folder.is_dir() or folder in working.parents or folder == working
                or folder in CHECKOUT.parents or folder == CHECKOUT
                or folder in (Path("/tmp"), Path("/dev/shm"), Path("/var/tmp"))):
            raise BubblewrapUnavailable(f"unsafe test job folder: {folder}")

        account = Path(tempfile.mkdtemp(prefix="test-account-", dir=folder))
        uid, gid = os.getuid(), os.getgid()
        _ = (account / "passwd").write_text(f"test:x:{uid}:{gid}:Test account:{HOME}:/bin/sh\n", encoding="ascii")
        _ = (account / "group").write_text(f"test:x:{gid}:\n", encoding="ascii")
        arguments = [str(executable), "--unshare-user", "--unshare-pid", "--unshare-net",
                     "--unshare-cgroup", "--unshare-ipc", "--unshare-uts", "--cap-drop", "ALL",
                     "--die-with-parent", "--clearenv", "--proc", "/proc", "--dev", "/dev",
                     "--tmpfs", "/tmp", "--tmpfs", "/dev/shm", "--dir", HOME]
        if lease_fd is None:
            arguments.append("--new-session")
        else:
            # The gate starts a private session with no terminal.
            # Keep PID 1 in that group from clone through exit.
            # PID 1 holds the folder lease even if the gate exits.
            arguments.extend(("--sync-fd", str(lease_fd)))
        for path in ("/nix/store", "/bin", "/usr/bin"):
            arguments.extend(("--ro-bind", path, path))
        for name in ("passwd", "group"):
            arguments.extend(("--ro-bind", str(account / name), f"/etc/{name}"))
        arguments.extend(("--symlink", str(SYSTEM.resolve(strict=True)), str(SYSTEM)))
        cache = f"/var/tmp/dataclass-code-{os.getuid()}"
        arguments.extend(("--ro-bind-try", cache, cache))
        mounts = sorted({checkout, CHECKOUT, *(inputs.read_only if inputs is not None else ())},
                        key=lambda path: (len(path.parts), path))
        for path in mounts:
            if not path.is_relative_to("/nix/store"):
                arguments.extend(("--ro-bind", str(path), str(path)))
        arguments.extend(("--bind", tmp, tmp, "--ro-bind", str(account), str(account)))

        runtime = "/tmp/test-runtime"
        arguments.extend(("--dir", runtime, "--chmod", "0700", runtime,
                          "--symlink", str(CHECKOUT), f"{HOME}/.claude"))
        values.update({
            "HOME": HOME, "USER": "test", "LOGNAME": "test", "PATH": values.get("PATH", str(SYSTEM / "bin")),
            "XDG_CACHE_HOME": "/tmp/test-cache", "XDG_CONFIG_HOME": "/tmp/test-config",
            "XDG_DATA_HOME": "/tmp/test-data", "XDG_STATE_HOME": "/tmp/test-state", "XDG_RUNTIME_DIR": runtime,
            "PYTHONPATH": os.pathsep.join((str(checkout / "scripts/lib/python_startup"), str(checkout))),
            "AWS_EC2_METADATA_DISABLED": "true",
        })
        for key, value in values.items():
            arguments.extend(("--setenv", key, value))
        arguments.extend(("--remount-ro", "/dev", "--remount-ro", "/", "--chdir", str(working), "--", *argv))
        return arguments
    except (conf.Unusable, OSError) as error:
        raise BubblewrapUnavailable(str(error)) from error


def profile_folder() -> Path | None:
    """Recognize the private folder leased by the module profiler."""
    value = os.environ.get("TMPDIR")
    if not value:
        return None
    try:
        folder = Path(value).resolve(strict=True)
        status = folder.stat()
        if (not folder.name.startswith("module-cost") or not stat.S_ISDIR(status.st_mode)
                or stat.S_IMODE(status.st_mode) != 0o700 or status.st_uid != os.getuid()):
            return None
        lock = folder.with_name(folder.name + ".lock")
        descriptor = os.open(lock, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            inode = os.fstat(descriptor)
            if not stat.S_ISREG(inode.st_mode) or inode.st_nlink != 1:
                return None
            if not any(os.path.samestat(inode, entry.stat())
                       for entry in Path("/proc/self/fd").iterdir() if entry != Path(f"/proc/self/fd/{descriptor}")
                       and entry.exists()):
                return None
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return folder
        finally:
            os.close(descriptor)
    except OSError:
        return None
    return None


def _exec(argv: Sequence[str], environment: Mapping[str, str]) -> NoReturn:
    try:
        os.execvpe(argv[0], list(argv), environment)
    except OSError as error:
        raise BubblewrapUnavailable(f"cannot start {argv[0]}: {error}") from error


def _verified_command(argv: Sequence[str], folder: Path) -> list[str]:
    return command([sys.executable, "-I", str(Path(__file__).resolve()), "--inside", *argv], folder)


def run(argv: Sequence[str], folder: Path) -> int:
    """Keep the caller and its folder until the guarded command exits."""
    if not argv:
        raise BubblewrapUnavailable("a guarded command requires an executable")
    try:
        status = subprocess.run(_verified_command(argv, folder), env={}, check=False, restore_signals=False).returncode
    except OSError as error:
        raise BubblewrapUnavailable(f"cannot start {argv[0]}: {error}") from error
    return status if status >= 0 else 128 - status


def enter(argv: Sequence[str]) -> NoReturn:
    """Run a command in Bubblewrap or refuse it."""
    if not argv:
        raise BubblewrapUnavailable("a guarded command requires an executable")
    if guarded():
        _exec(command(argv, Path(tempfile.gettempdir())), {})
    folder = profile_folder()
    if folder is None:
        if "TEST_TIMES_OUT" in os.environ:
            raise BubblewrapUnavailable("test timing output requires a private profiler folder")
        with tempfile.TemporaryDirectory(prefix="bubble-wrapped-tests-", dir="/tmp") as directory:
            raise SystemExit(run(argv, Path(directory)))
    launch = _verified_command(argv, folder)
    # Bubblewrap passes inherited descriptors to its child.
    try:
        for entry in Path("/proc/self/fd").iterdir():
            descriptor = int(entry.name)
            if descriptor > 2:
                try:
                    os.set_inheritable(descriptor, False)
                except OSError as error:
                    if error.errno != errno.EBADF:
                        raise
    except OSError as error:
        raise BubblewrapUnavailable(f"cannot close inherited descriptors: {error}") from error
    _exec(launch, {})


def main(argv: list[str]) -> NoReturn:
    try:
        if argv[:1] == ["--inside"]:
            if not guarded():
                raise BubblewrapUnavailable("the test started outside Bubblewrap")
            if len(argv) < 2:
                raise BubblewrapUnavailable("a guarded command requires an executable")
            _exec(argv[1:], os.environ)
        if argv[:1] == ["--"]:
            enter(argv[1:])
        raise BubblewrapUnavailable("usage: bubble_wrapped_tests.py -- <command>")
    except BubblewrapUnavailable as error:
        raise SystemExit(f"Bubblewrap tests: {error}") from error


if __name__ == "__main__":
    main(sys.argv[1:])
