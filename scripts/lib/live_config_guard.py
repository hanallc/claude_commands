"""Keep test processes from changing the live configuration folder."""
from __future__ import annotations

import os
import pwd
import sys
from pathlib import Path

_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
_PATH_EVENTS = {"os.remove": 1, "os.rmdir": 1, "os.truncate": 1, "os.rename": 2, "os.replace": 2}


class LiveConfigWrite(RuntimeError):
    """A test tried to change a file in the live configuration folder."""


def live_folder() -> Path:
    """Return the real user's live configuration folder, whatever HOME a test sets."""
    return Path(pwd.getpwuid(os.getuid()).pw_dir, ".claude", "config").resolve()


def refuse(path: str | bytes | os.PathLike[str] | os.PathLike[bytes], *, folder: Path) -> None:
    """Raise when path is inside folder after links resolve."""
    target = Path(os.path.realpath(os.fsdecode(path)))
    if target == folder or folder in target.parents:
        raise LiveConfigWrite(f"{target}: a test must not change the live configuration folder {folder}; "
                              + "set CONF_DIR to a fixture folder")


def install(folder: Path) -> None:
    """Refuse each write, rename and removal inside folder for the rest of this process."""
    def audit(event: str, arguments: tuple[object, ...]) -> None:
        if event == "open":
            path, mode, flags = arguments
            writes = any(letter in mode for letter in "wax+") if isinstance(mode, str) else (
                isinstance(flags, int) and bool(flags & _WRITE_FLAGS))
            paths = [path] if writes else []
        else:
            paths = list(arguments[:_PATH_EVENTS.get(event, 0)])
        for path in paths:
            if isinstance(path, str | bytes | os.PathLike):
                refuse(path, folder=folder)

    sys.addaudithook(audit)
