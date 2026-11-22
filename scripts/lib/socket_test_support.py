"""Keep Unix sockets under the run folder or a short folder the fixture owns."""
from __future__ import annotations

import socket
import os
import tempfile
from contextlib import ExitStack
from pathlib import Path
from types import TracebackType
from typing import override

from scripts.lib import build_folders
from scripts.lib.testing_folders import RUN_FOLDER_ENV
from scripts.lint.memory_test_support import config_environment


PARENT = "test-sockets"
SOCKET_ROOT_ENV = "DEVTOOLS_TEST_SOCKET_ROOT"
# Each process sweeps a parent once: parallel test runs hold many folders there.
_swept: set[Path] = set()


def socket_parent() -> Path:
    """Return the parent; the first call in a process removes the free folders killed runs left."""
    root = (os.environ.get(SOCKET_ROOT_ENV) or os.environ.get(RUN_FOLDER_ENV)
            or os.environ.get("GATE_MODULE_TEMP_DIR") or tempfile.gettempdir())
    parent = Path(root) / PARENT
    parent.mkdir(mode=0o700, exist_ok=True)
    if parent not in _swept:
        _swept.add(parent)
        build_folders.sweep_temporary_build_folders(parent, wait=False)
    return parent


class ShortSocketDirectory:
    """Keep a private socket folder under the socket root."""

    def __init__(self, prefix: str) -> None:
        if configuration := os.environ.get("CONF_DIR"):
            _ = config_environment(Path(configuration))
        self._lifetime: ExitStack = ExitStack()
        folder = self._lifetime.enter_context(build_folders.temporary_build_folder(
            build_folders.UniqueBuildFolder(prefix, build_folders.ExplicitBuildParent(socket_parent()))))
        self.name: str = str(folder)

    def __enter__(self) -> str:
        return self.name

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None,
                 traceback: TracebackType | None) -> None:
        self.cleanup()

    def cleanup(self) -> None:
        self._lifetime.close()


def short_socket_directory(prefix: str = "test-sock-") -> ShortSocketDirectory:
    """Own a private socket folder; use its context or register cleanup."""
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
