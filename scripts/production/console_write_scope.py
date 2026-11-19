"""Record writes outside the console check workspace."""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import cast
import fcntl
from importlib.util import module_from_spec, spec_from_file_location
import os
import runpy
import stat
import sys
from threading import local

BARRIER_LOG = "console-barrier.log"
WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND


def descriptor_path(descriptor: int) -> Path:
    if sys.platform == "darwin":
        result = fcntl.fcntl(descriptor, getattr(fcntl, "F_GETPATH", 50), bytes(1024))
        return Path(os.fsdecode(result.split(b"\0", 1)[0]))
    return Path(os.readlink(f"/proc/self/fd/{descriptor}"))


def install_barrier(workspace: Path, process: str) -> None:
    """Deny writes by this Python process outside its workspace."""
    workspace = workspace.resolve()
    log = workspace / BARRIER_LOG
    record = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    open_file = os.open
    opening = local()

    def deny(event: str, name: object, directory: object = -1) -> None:
        message = f"{process} {event}: {name!r}"
        if isinstance(directory, int) and directory >= 0:
            message += f" (dir_fd={directory})"
        _ = os.write(record, (message + "\n").encode(errors="backslashreplace"))
        raise PermissionError(f"Console filesystem barrier: {message}")

    def check(event: str, name: object, directory: object = -1) -> None:
        try:
            if isinstance(name, int):
                mode = os.fstat(name).st_mode
                if event == "open" and (stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode)):
                    return
                path = descriptor_path(name)
            elif isinstance(name, str | bytes):
                path = Path(os.fsdecode(name))
                if not path.is_absolute() and isinstance(directory, int) and directory >= 0:
                    path = descriptor_path(directory) / path
            else:
                raise ValueError("The operation has no path.")
            target = path.resolve()
            if event == "open" and target == Path("/dev/null"):
                return
            location = target if path.name in ("", ".", "..") else path.parent.resolve() / path.name
            if all(candidate.is_relative_to(workspace) and candidate != log for candidate in (location, target)):
                return
        except (OSError, ValueError, RuntimeError):
            pass
        deny(event, name, directory)

    def open_at(path: str | bytes | os.PathLike[str] | os.PathLike[bytes], flags: int, mode: int = 0o777,
                *, dir_fd: int | None = None) -> int:
        if dir_fd is None or not flags & WRITE_FLAGS:
            return open_file(path, flags, mode, dir_fd=dir_fd)
        # The CPython open event omits the directory descriptor.
        check("open", os.fspath(path), dir_fd)
        opening.checked = True
        try:
            return open_file(path, flags, mode, dir_fd=dir_fd)
        finally:
            opening.checked = False

    def audit(event: str, arguments: tuple[object, ...]) -> None:
        match event:
            case "open":
                flags = arguments[2]
                if isinstance(flags, int) and flags & WRITE_FLAGS and not getattr(opening, "checked", False):
                    check(event, arguments[0])
            case "os.rename" | "os.link":
                check(event, arguments[0], arguments[2])
                check(event, arguments[1], arguments[3])
            case "os.remove" | "os.rmdir" | "os.truncate":
                check(event, arguments[0], arguments[1] if event != "os.truncate" else -1)
            case "os.mkdir" | "os.chmod" | "os.symlink":
                check(event, arguments[1] if event == "os.symlink" else arguments[0], arguments[2])
            case "os.chown" | "os.utime":
                check(event, arguments[0], arguments[3])
            case "os.setxattr" | "os.removexattr":
                check(event, arguments[0])
            case "sqlite3.connect":
                deny(event, arguments[0])
            case _:
                pass

    sys.addaudithook(audit)
    os.open = open_at


def select_tree(tree: Path) -> None:
    """Select the scanned tree before the checker imports its code."""
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(tree))
    startup = runpy.run_path(str(tree / "scripts/lib/python_startup/sitecustomize.py"))
    select_checkout = cast(Callable[[list[str]], str], startup["select_checkout"])
    _ = select_checkout([str(tree / Path(__file__).name)])


def adapt_legacy_status() -> None:
    """Add canonical keys to status markup from the unchanged base."""
    from scripts.review_links import page_frame

    render_title = cast(Callable[..., str], page_frame.title)
    components = {
        ("Accounts", "accounts"): "accounts.refresh",
        ("AWS", "aws"): "aws.refresh",
        ("Builds", "builds"): "builds.refresh",
        ("Disks", "disks"): "disks.refresh",
        ("Images", "images"): "images.refresh",
        ("The List", "list"): "list.refresh",
        ("Review", "review"): "review.refresh",
        ("Changes to review", "review"): "review.changes",
        ("Questions to answer", "review"): "review.questions",
        ("Investigations", "review"): "review.investigations",
    }
    marker = '<p class="page-status" data-page-status data-state="Reading"'

    def title(text: str, page: str, **options: object) -> str:
        markup = render_title(text, page, **options)
        key = components.get((text, page))
        if key and markup.count(marker) == 1 and "data-status-component=" not in markup:
            return markup.replace(marker, f'{marker} data-status-component="{key}"', 1)
        return markup

    page_frame.title = title


def main(arguments: list[str]) -> None:
    accounts_child = arguments[0] == "--accounts"
    script, assets, settings_path, *mode = arguments[int(accounts_child):]
    tree = Path.cwd()
    workspace = Path(assets).parent
    install_barrier(workspace, "Accounts child" if accounts_child else "server")
    select_tree(tree)
    spec = spec_from_file_location("console_check_settings", Path(__file__).with_name("console_settings.py"))
    if spec is None or spec.loader is None:
        raise ValueError("The console settings selector is unavailable.")
    settings = module_from_spec(spec)
    sys.modules[spec.name] = settings
    spec.loader.exec_module(settings)
    load = cast(Callable[[Path], object], getattr(settings, "InstalledSettings").load)
    installed = load(Path(settings_path))
    read_installed = cast(Callable[[Path], object], getattr(installed, "select"))(tree)
    if "--legacy-status" in mode:
        adapt_legacy_status()

    os.environ["REVIEW_LINKS_READ_ONLY"] = "1"
    from scripts.accounts import accounts
    from scripts.herdr import pane_launch
    from scripts.lib import build_folders, conf
    from scripts.statusline import rebuild_row

    if not hasattr(accounts, "REVIEW_LINKS_READ_ONLY"):
        setattr(accounts, "_start_session_counts_fetch", lambda: False)
    # The Accounts census can read this cache during a console check.
    if model_cache := cast(object, getattr(accounts, "_PersistentModelScans", None)):
        def keep_model_cache(_cache: object) -> None:
            pass

        setattr(model_cache, "save", keep_model_cache)
    if not hasattr(pane_launch, "REVIEW_LINKS_READ_ONLY"):
        pane_launch.read_entries = pane_launch._read_entries  # pyright: ignore[reportPrivateUsage]
    # Read the exact slot lock without opening it for writing.
    slot_locks = cast(object, getattr(build_folders, "build_folder_lock", build_folders))
    lock_function = "open_lock" if slot_locks is not build_folders else "_open_lock"
    open_lock = cast(Callable[..., int], getattr(slot_locks, lock_function))

    def readonly_slot(path: Path, directory: int, *, create: bool) -> int:
        if create:
            return open_lock(path, directory, create=True)
        descriptor = os.open(path.name + ".lock", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=directory)
        try:
            inode = os.fstat(descriptor)
            if not stat.S_ISREG(inode.st_mode) or inode.st_nlink > 1:
                raise ValueError("a build folder lock must be a regular file with one link")
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    setattr(slot_locks, lock_function, readonly_slot)
    rebuild_row.DEFAULT_CONFIG = conf.path("statusline")

    if accounts_child:
        sys.argv = ["scripts.review_links.accounts_page"]
        _ = runpy.run_module("scripts.review_links.accounts_page", run_name="__main__", alter_sys=True)
        return

    from scripts.review_links import accounts_page

    accounts_page.CHILD = (sys.executable, "-I", str(Path(__file__).resolve()), "--accounts", *arguments)
    sys.argv = [script, assets]
    _ = runpy.run_path(script, init_globals={"read_installed": read_installed}, run_name="__main__")


if __name__ == "__main__":
    main(sys.argv[1:])
