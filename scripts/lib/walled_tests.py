"""Start tests inside the natedev file, PID, and network walls."""

from __future__ import annotations

from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
import errno
import fcntl
import os
from pathlib import Path
import stat
import sys
from typing import NamedTuple, NoReturn

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.lib import conf
from scripts.lib.python_startup.sitecustomize import kernel_guarded


CHECKOUT = Path(__file__).resolve().parents[2]
SYSTEM = Path("/run/current-system/sw")
CONFIG = CHECKOUT / "config/walled_tests.conf"
HOME = "/home/test"
MODULE_VALUES = (
    "BUILDLOG_DIR", "BUILDLOG_LIVE_MEMORY_TRIPWIRE", "HERDR_BIN", "HERDR_PANE_RECORD_DIR",
    "SESSION_TEMP_ROOT", "STAFF_FILE", "TEST_TIMES_OUT",
)


class WallsUnavailable(RuntimeError):
    """The required test walls cannot start."""


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
    """Check the walls in the current kernel namespaces."""
    return kernel_guarded()


def host_environment(environment: Mapping[str, str]) -> dict[str, str]:
    """Keep only the settings that the host scope and bootstrap need."""
    try:
        keys = conf.read(CONFIG).words("kept_environment")
    except conf.Unusable as error:
        raise WallsUnavailable(str(error)) from error
    return {key: environment[key] for key in keys if key in environment}


def command(argv: Sequence[str], job_folder: Path | None) -> list[str]:
    """Build a guarded command. The caller owns the job folder lease."""
    if not argv:
        raise WallsUnavailable("a guarded command requires an executable")
    if guarded():
        return ["/usr/bin/env", "MERGE_GATE_PARENT_PID=1", *argv]

    try:
        settings = conf.read(CONFIG)
        executable = Path(settings.text("bubblewrap", recover_invalid=False))
        if executable != SYSTEM / "bin/bwrap" or not executable.is_file() or not os.access(executable, os.X_OK):
            raise WallsUnavailable(f"Bubblewrap is unavailable at {SYSTEM / 'bin/bwrap'}")
        inputs = _gate_inputs.get()
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
        folder = job_folder.resolve(strict=True) if job_folder is not None else None
        if folder is not None and (not folder.is_dir() or folder in working.parents or folder == working
                                   or folder in CHECKOUT.parents or folder == CHECKOUT
                                   or folder in (Path("/tmp"), Path("/dev/shm"), Path("/var/tmp"))):
            raise WallsUnavailable(f"unsafe test job folder: {folder}")

        source = os.environ if inputs is None else inputs.environment
        values = {key: source[key] for key in settings.words("kept_environment") if key in source}
        arguments = [str(executable), "--unshare-user", "--unshare-pid", "--unshare-net",
                     "--unshare-cgroup", "--unshare-ipc", "--unshare-uts", "--cap-drop", "ALL",
                     "--new-session", "--die-with-parent", "--clearenv", "--proc", "/proc", "--dev", "/dev",
                     "--tmpfs", "/tmp", "--tmpfs", "/dev/shm", "--dir", HOME]
        for path in ("/nix/store", "/bin", "/usr/bin"):
            arguments.extend(("--ro-bind", path, path))
        arguments.extend(("--symlink", str(SYSTEM.resolve(strict=True)), str(SYSTEM)))
        cache = f"/var/tmp/dataclass-code-{os.getuid()}"
        arguments.extend(("--ro-bind-try", cache, cache))
        mounts = sorted({checkout, CHECKOUT, *(inputs.read_only if inputs is not None else ())},
                        key=lambda path: (len(path.parts), path))
        for path in mounts:
            if not path.is_relative_to("/nix/store"):
                arguments.extend(("--ro-bind", str(path), str(path)))
        if folder is not None:
            arguments.extend(("--bind", str(folder), str(folder)))
            for key in MODULE_VALUES:
                if key in source:
                    values[key] = source[key]

        tmp = str(folder) if folder is not None else "/tmp"
        runtime = "/tmp/test-runtime"
        arguments.extend(("--dir", runtime, "--chmod", "0700", runtime,
                          "--symlink", str(CHECKOUT), f"{HOME}/.claude"))
        _ = values.pop("DBUS_SESSION_BUS_ADDRESS", None)
        values.update({
            "HOME": HOME, "PATH": values.get("PATH", str(SYSTEM / "bin")),
            "XDG_CACHE_HOME": "/tmp/test-cache", "XDG_CONFIG_HOME": "/tmp/test-config",
            "XDG_DATA_HOME": "/tmp/test-data", "XDG_STATE_HOME": "/tmp/test-state", "XDG_RUNTIME_DIR": runtime,
            "TMPDIR": tmp, "TMP": tmp, "TEMP": tmp, "MERGE_GATE_PARENT_PID": "1",
            "PYTHONPATH": os.pathsep.join((str(checkout / "scripts/lib/python_startup"), str(checkout))),
            "AWS_EC2_METADATA_DISABLED": "true",
        })
        _ = values.setdefault("BUILDLOG_DIR", f"{tmp}/buildlog")
        _ = values.setdefault("SESSION_TEMP_ROOT", f"{tmp}/session")
        for key, value in values.items():
            arguments.extend(("--setenv", key, value))
        arguments.extend(("--remount-ro", "/dev", "--remount-ro", "/", "--chdir", str(working), "--", *argv))
        return arguments
    except (conf.Unusable, OSError) as error:
        raise WallsUnavailable(str(error)) from error


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
        raise WallsUnavailable(f"cannot start {argv[0]}: {error}") from error


def enter(argv: Sequence[str]) -> NoReturn:
    """Replace this process with a guarded command or refuse it."""
    if not argv:
        raise WallsUnavailable("a guarded command requires an executable")
    if guarded():
        _exec(argv, os.environ)
    folder = profile_folder()
    if "TEST_TIMES_OUT" in os.environ and folder is None:
        raise WallsUnavailable("test timing output requires a private profiler folder")
    verification = [sys.executable, "-I", str(Path(__file__).resolve()), "--inside", *argv]
    launch = command(verification, folder)
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
        raise WallsUnavailable(f"cannot close inherited descriptors: {error}") from error
    _exec(launch, {})


def main(argv: list[str]) -> NoReturn:
    try:
        if argv[:1] == ["--inside"]:
            if not guarded():
                raise WallsUnavailable("the required test walls are absent after launch")
            if len(argv) < 2:
                raise WallsUnavailable("a guarded command requires an executable")
            _exec(argv[1:], os.environ)
        if argv[:1] == ["--"]:
            enter(argv[1:])
        raise WallsUnavailable("usage: walled_tests.py -- <command>")
    except WallsUnavailable as error:
        raise SystemExit(f"walled tests: {error}") from error


if __name__ == "__main__":
    main(sys.argv[1:])
