"""Give each Unix socket fixture a private folder with a short path."""
from __future__ import annotations

import os
import socket
import sys
import tempfile
from pathlib import Path
from types import TracebackType
from typing import override


UNIX_SOCKET_PATH_LIMIT = 103 if sys.platform == "darwin" else 107
LONGEST_SOCKET_SUFFIX = ".codex2/app-server-control/app-server-control.sock"


def socket_parent(prefix: str = "s") -> Path:
    """Use the temporary directory if the longest fixture socket path fits."""
    parent = Path(tempfile.gettempdir())
    # tempfile.mkdtemp adds eight characters to the prefix.
    longest_path = parent / (prefix + "xxxxxxxx") / LONGEST_SOCKET_SUFFIX
    return parent if len(os.fsencode(longest_path)) <= UNIX_SOCKET_PATH_LIMIT else Path("/tmp")


class ShortSocketDirectory:
    """Remove the private socket folder when the fixture closes."""

    def __init__(self, prefix: str) -> None:
        short_prefix = prefix[:1]
        self._folder: tempfile.TemporaryDirectory[str] = tempfile.TemporaryDirectory(
            dir=socket_parent(short_prefix), prefix=short_prefix)
        self.name: str = self._folder.name

    def __enter__(self) -> str:
        return self.name

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None,
                 traceback: TracebackType | None) -> None:
        self.cleanup()

    def cleanup(self) -> None:
        self._folder.cleanup()


def short_socket_directory(prefix: str = "s") -> ShortSocketDirectory:
    """Own a private socket folder for the fixture."""
    return ShortSocketDirectory(prefix)


class ShortUnixSocket(socket.socket):
    """A socket that removes its private path and folder when closed."""

    def __init__(self) -> None:
        super().__init__(socket.AF_UNIX)
        self._directory: ShortSocketDirectory = short_socket_directory()
        self.path: Path = Path(self._directory.name) / "s"

    @override
    def close(self) -> None:
        try:
            super().close()
        finally:
            self._directory.cleanup()

    def bind_socket(self) -> Path:
        self.bind(str(self.path))
        return self.path
