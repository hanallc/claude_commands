"""Private Unix socket paths whose length does not depend on TMPDIR."""
from __future__ import annotations

import socket
import os
from contextlib import ExitStack
from types import TracebackType

from scripts.lib import build_folders, conf
from scripts.lib.build_folders_test_support import temporary_folder_settings
from scripts.lint.memory_test_support import seed_config_fixture_folder
from pathlib import Path
from typing import override


# The socket folders' own parent under the configured system root.
PARENT = "test-sockets"
SYSTEM_SOCKET_ROOT = Path("/tmp")
# Each process sweeps a parent once: parallel test runs hold many folders there.
_swept: set[Path] = set()


def socket_parent(system_root: Path | None = None) -> Path:
    """Return the parent; the first call in a process removes the free folders killed runs left."""
    root = system_root or SYSTEM_SOCKET_ROOT
    if system_root is None and (configuration := os.environ.get("CONF_DIR")):
        settings = Path(configuration) / "build_folders.conf"
        if settings.is_file():
            configured_root = conf.read(settings).values().get("roots.system")
            if configured_root:
                root = Path(configured_root)
    parent = root / PARENT
    parent.mkdir(mode=0o700, exist_ok=True)
    if parent not in _swept:
        _swept.add(parent)
        build_folders.sweep_temporary_build_folders(parent, wait=False)
    return parent


class ShortSocketDirectory:
    """Keep a short socket folder in its own parent under the configured system root."""

    def __init__(self, prefix: str, *, system_root: Path | None = None) -> None:
        if configuration := os.environ.get("CONF_DIR"):
            seed_config_fixture_folder(Path(configuration), {
                "build_folders.conf": temporary_folder_settings(SYSTEM_SOCKET_ROOT),
            })
        self._lifetime: ExitStack = ExitStack()
        folder = self._lifetime.enter_context(build_folders.temporary_build_folder(
            build_folders.UniqueBuildFolder(prefix, build_folders.ExplicitBuildParent(socket_parent(system_root)))))
        self.name: str = str(folder)

    def __enter__(self) -> str:
        return self.name

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None,
                 traceback: TracebackType | None) -> None:
        self.cleanup()

    def cleanup(self) -> None:
        self._lifetime.close()


def short_socket_directory(prefix: str = "test-sock-", *,
                           system_root: Path | None = None) -> ShortSocketDirectory:
    """Own a private socket folder; use its context or register cleanup."""
    return ShortSocketDirectory(prefix, system_root=system_root)


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
