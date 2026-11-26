"""Protect the live configuration folder during a Python test run."""
from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
import fcntl
from inspect import getsource
import os
from pathlib import Path
import sys


class LiveConfigWrite(AssertionError):
    """A test attempted to change the live configuration folder."""


def install_live_config_guard(folder: str, violation_log: str, startup_hook: str) -> None:
    """Reject Python writes to the live config and record the first attempt."""
    canonical = os.path.realpath

    def entry_path(path: str) -> str:
        parent, name = os.path.split(path.rstrip(os.sep) or os.sep)
        return os.path.normpath(os.path.join(canonical(parent), name))

    roots = (entry_path(folder), canonical(folder))
    prefixes = tuple(root.rstrip(os.sep) + os.sep for root in roots)
    reported = False
    write_flags = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND

    def descriptor_path(descriptor: int) -> str:
        if sys.platform == "darwin":
            # macOS uses command 50 for F_GETPATH.
            raw = fcntl.fcntl(descriptor, 50, bytes(1024)).split(b"\0", 1)[0]
            return os.fsdecode(raw)
        return os.readlink(f"/proc/self/fd/{descriptor}")

    def check(value: object, directory_fd: object = -1, *, follow_entry: bool) -> None:
        nonlocal reported
        try:
            if isinstance(value, int):
                path = descriptor_path(value)
            elif isinstance(value, (str, bytes)):
                path = os.fsdecode(value)
                if not os.path.isabs(path) and isinstance(directory_fd, int) and directory_fd >= 0:
                    path = os.path.join(descriptor_path(directory_fd), path)
            else:
                return
            absolute = path if os.path.isabs(path) else os.path.join(os.getcwd(), path)
            resolved = canonical(absolute) if follow_entry else entry_path(absolute)
        except (OSError, ValueError):
            return
        if resolved in roots or resolved.startswith(prefixes):
            if not os.path.isfile(startup_hook):
                return
            if not reported:
                try:
                    with open(violation_log, "a", encoding="utf-8", errors="backslashreplace") as log:
                        _ = log.write(f"{absolute}\n")
                except FileNotFoundError:
                    if not os.path.isfile(startup_hook):
                        return
                    raise
                reported = True
            raise LiveConfigWrite(f"Test attempted to write live configuration at {absolute}")

    def check_entry(value: object, directory_fd: object = -1) -> None:
        check(value, directory_fd, follow_entry=False)

    def audit(event: str, arguments: tuple[object, ...]) -> None:
        if event == "open":
            path, _, flags = arguments
            # The event omits dir_fd. The working directory may be wrong.
            if isinstance(flags, int) and flags & write_flags and isinstance(path, (str, bytes)) and os.path.isabs(path):
                exclusive = flags & os.O_CREAT and flags & os.O_EXCL
                check(path, follow_entry=not (flags & os.O_NOFOLLOW or exclusive))
        elif event in {"os.rename", "os.link"}:
            check_entry(arguments[0], arguments[2])
            check_entry(arguments[1], arguments[3])
        elif event == "os.symlink":
            check_entry(arguments[1], arguments[2])
        elif event in {"os.mkdir", "os.remove", "os.rmdir", "shutil.rmtree", "os.chmod", "os.chown", "os.utime"}:
            check_entry(arguments[0], arguments[-1])
        elif event in {"os.setxattr", "os.removexattr"}:
            check_entry(arguments[0])
        elif event == "os.truncate":
            check(arguments[0], follow_entry=True)

    sys.addaudithook(audit)


@contextmanager
def guard_live_config(folder: Path, startup_folder: Path, violation_log: Path) -> Generator[None]:
    """Use the same guard in the runner and its Python child processes."""
    folder = folder.absolute()
    violation_log = violation_log.absolute()
    hook = startup_folder.absolute() / "sitecustomize.py"
    _ = hook.write_text(
        """from __future__ import annotations
import fcntl
import os
import sys
from importlib.machinery import PathFinder
from importlib.util import module_from_spec


""" + getsource(LiveConfigWrite) + "\n\n" + getsource(install_live_config_guard)
        + f"\ninstall_live_config_guard({str(folder)!r}, {str(violation_log)!r}, __file__)\n"
        + """
# Keep the primary hook registered while the next hook runs.
directory = os.path.realpath(os.path.dirname(__file__))
search = iter(sys.path)
for entry in search:
    if os.path.realpath(entry) == directory:
        break
spec = PathFinder.find_spec("sitecustomize", list(search))
if spec is not None and spec.loader is not None:
    module = module_from_spec(spec)
    if __file__ == sys.modules["sitecustomize"].__file__:
        sys.modules["sitecustomize"] = module
    spec.loader.exec_module(module)
""", encoding="utf-8")
    previous = os.environ.get("PYTHONPATH")
    os.environ["PYTHONPATH"] = (str(hook.parent) if previous is None
                                else os.pathsep.join((str(hook.parent), previous)))
    install_live_config_guard(str(folder), str(violation_log), str(hook))
    try:
        yield
    finally:
        if previous is None:
            _ = os.environ.pop("PYTHONPATH", None)
        else:
            os.environ["PYTHONPATH"] = previous
        hook.unlink(missing_ok=True)
        if violation_log.exists():
            with violation_log.open(encoding="utf-8") as log:
                raise LiveConfigWrite(f"Test attempted to write live configuration at {log.readline().rstrip()}")
