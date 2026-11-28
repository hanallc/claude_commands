"""Private Unix socket paths whose length does not depend on TMPDIR."""
from __future__ import annotations

from collections.abc import Generator
from contextlib import ExitStack, contextmanager
import os
import socket
from types import TracebackType

from scripts.lib import build_folders, conf
from scripts.lint.memory_test_support import config_environment
from pathlib import Path
from typing import override


# The socket folders' own parent under the configured system root.
PARENT = "test-sockets"
BROWSER_PREFIX = "browser-"
SHORT_SOCKET_PREFIX = "test-sock-"
SHORT_SOCKET_LEAF = "s"
STYLE_SOCKET_PREFIX = "style-socket-"
STYLE_SOCKET_LEAF = "outside.sock"
OUTAGE_DAEMON_SOCKET_LEAF = Path("home/.codex/app-server-control/control.sock")
BROWSER_SINGLETON_LEAF = Path(".org.chromium.Chromium.XXXXXX/SingletonSocket")
PROOF_CHROME_PREFIX = "memory-proof-chrome-"
PROOF_CHROME_TEMP = "tmp"
# Each process sweeps a parent once: parallel test runs hold many folders there.
_swept: set[Path] = set()


def socket_address(parent: Path, leaf: str | Path) -> Path:
    """Build an address within the Unix socket limit."""
    path = parent / leaf
    length = len(os.fsencode(path)) + 1
    if length >= 104:
        raise ValueError(f"{path}: Unix socket address uses {length} bytes including NUL")
    return path


def short_socket_address_examples() -> tuple[Path, ...]:
    """Model the longest addresses in the shared and browser proof folders."""
    settings = conf.read_or_repository("build_folders")
    root = Path(settings.text("roots.system")).expanduser()
    shared = root / PARENT
    suffix = "x" * settings.whole("names.suffix_length")
    longest_prefix = max((SHORT_SOCKET_PREFIX, STYLE_SOCKET_PREFIX, BROWSER_PREFIX), key=len)
    longest_leaf = max((OUTAGE_DAEMON_SOCKET_LEAF, BROWSER_SINGLETON_LEAF, Path(STYLE_SOCKET_LEAF)),
                       key=lambda leaf: len(os.fsencode(leaf)))
    return (
        socket_address(shared / (longest_prefix + suffix), longest_leaf),
        socket_address(shared / (BROWSER_PREFIX + suffix), BROWSER_SINGLETON_LEAF),
        socket_address(shared / (SHORT_SOCKET_PREFIX + suffix), OUTAGE_DAEMON_SOCKET_LEAF),
        proof_chrome_socket_address(root),
    )


def proof_chrome_socket_address(root: Path) -> Path:
    """Check the Chrome address with the suffix length from mkdtemp."""
    return socket_address(root / (PROOF_CHROME_PREFIX + "XXXXXX") / PROOF_CHROME_TEMP, BROWSER_SINGLETON_LEAF)


def socket_parent() -> Path:
    """Return the parent; the first call in a process removes the free folders killed runs left."""
    parent = Path(conf.read_or_repository("build_folders").text("roots.system")).expanduser() / PARENT
    parent.mkdir(mode=0o700, exist_ok=True)
    if parent not in _swept:
        _swept.add(parent)
        build_folders.sweep_temporary_build_folders(parent, wait=False)
    return parent


class ShortSocketDirectory:
    """Keep a short socket folder in its own parent under the configured system root."""

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


def short_socket_directory(prefix: str = SHORT_SOCKET_PREFIX) -> ShortSocketDirectory:
    """Own a private socket folder; use its context or register cleanup."""
    return ShortSocketDirectory(prefix)


def descriptor_socket_path(directory: int, leaf: str) -> str:
    """Use a short Linux address for a socket that must exist in a test folder."""
    if Path(leaf).name != leaf or leaf in {"", ".", ".."}:
        raise ValueError(f"{leaf!r}: expected a socket file name")
    return str(socket_address(Path("/proc/self/fd") / str(directory), leaf))


@contextmanager
def browser_socket_environment() -> Generator[dict[str, str]]:
    """Keep browser sockets in a private short folder until the browser exits."""
    with short_socket_directory(BROWSER_PREFIX) as temporary:
        _ = socket_address(Path(temporary), BROWSER_SINGLETON_LEAF)
        yield {**os.environ, "TMPDIR": temporary, "MAC_CHROMIUM_TMPDIR": temporary}


class ShortUnixSocket(socket.socket):
    """A socket that removes its private path and folder when closed."""

    def __init__(self) -> None:
        directory = short_socket_directory()
        try:
            self.path: Path = socket_address(Path(directory.name), SHORT_SOCKET_LEAF)
            super().__init__(socket.AF_UNIX)
        except BaseException:
            directory.cleanup()
            raise
        self._directory: ShortSocketDirectory = directory

    @override
    def close(self) -> None:
        try:
            super().close()
        finally:
            self._directory.cleanup()

    def bind_socket(self) -> Path:
        self.bind(str(self.path))
        return self.path
