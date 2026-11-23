#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""Take numbered copies and preserve source before each reset."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

from collections.abc import Generator, Mapping, Sequence
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from types import MappingProxyType
from typing import cast
from uuid import uuid4

from scripts.buildlog.store import host_name
from scripts.lib import build_folders, conf, copy_record, git_files, git_remotes, git_rescue
from scripts.lib.slot_root import configured_slot_root as configured_slot_root


# Python writes these folders beside each imported module; they hold no work.
_TOOL_OUTPUT_FOLDERS = ("__pycache__",)


@dataclass(frozen=True)
class ExactSlotRequest:
    """Acquire only the slot number assigned to this command."""

    number: int


@dataclass(frozen=True)
class LowestFreeSlot:
    """Acquire the lowest free copy number."""


SlotSelection = ExactSlotRequest | LowestFreeSlot
LOWEST_FREE_SLOT = LowestFreeSlot()


@dataclass(frozen=True)
class SlotQueuePosition:
    """Retain the admitted run's place when another run takes the last slot."""

    episode: str
    since: float


class SlotRepositoryMismatch(ValueError):
    """The slot belongs to a different source repository."""

    def __init__(self, path: Path, source: Path) -> None:
        self.path: Path = path
        self.source: Path = source
        super().__init__(f"slots: {path} belongs to a different repository than {source}")


class SlotGitFailure(RuntimeError):
    """A slot Git command failed or exceeded its configured deadline."""


def _run_git(source: Path, arguments: Sequence[str], *, data: bytes | None = None, pass_fds: Sequence[int] = (),
             accepted: tuple[int, ...] = (0,)) -> bytes:
    timeout = build_folders.repository_git_timeout()
    try:
        result = subprocess.run(["git", "-C", str(source), *arguments], input=data, capture_output=True,
                                pass_fds=tuple(dict.fromkeys((*pass_fds, *build_folders.held_build_folder_descriptors()))),
                                timeout=timeout)
    except subprocess.TimeoutExpired as error:
        raise SlotGitFailure(f"git {' '.join(arguments)} in {source}: timed out after {timeout:g} seconds") from error
    if result.returncode not in accepted:
        raise SlotGitFailure(f"git {' '.join(arguments)} in {source}: {result.stderr.decode(errors='replace').strip()}")
    return result.stdout


def _git(source: Path, *arguments: str, pass_fds: Sequence[int] = (), accepted: tuple[int, ...] = (0,)) -> str:
    return _run_git(source, arguments, pass_fds=pass_fds, accepted=accepted).decode().strip()


def _pool_git(repo: Path, *arguments: str, data: bytes | None = None, pass_fds: Sequence[int] = ()) -> bytes:
    # Git children inherit the copy locks. Keep automatic maintenance synchronous.
    return _run_git(repo, ("-c", "maintenance.autoDetach=false", "-c", "gc.autoDetach=false", *arguments),
                    data=data, pass_fds=pass_fds)


def source_checkout(source: Path) -> Path:
    """Read the main checkout, including repositories with separate Git metadata.

    For a numbered copy or its pool, return the live checkout that the pool serves while it exists.
    """
    checkout = _main_checkout(source)
    served = build_folders.pool_source(checkout)
    return served if served is not None and served.is_dir() else checkout


def _main_checkout(source: Path) -> Path:
    # --show-cdup prints a line only inside a work tree: the path from source to its top.
    common, current, bare, *cdup = _git(source, "rev-parse", "--path-format=absolute", "--git-common-dir",
                                       "--git-dir", "--is-bare-repository", "--show-cdup").splitlines()
    if common == current and bare == "false":
        if cdup:
            return (source / cdup[0]).resolve()
        return Path(_git(source, "rev-parse", "--show-toplevel").strip())
    first = _git(source, "worktree", "list", "--porcelain").splitlines()[0]
    checkout = Path(first.removeprefix("worktree "))
    configured = _git(source, "--git-dir", common, "config", "--default", str(checkout),
                      "--get", "core.worktree").strip()
    return (Path(common) / configured).resolve()


def _root(settings: conf.ConfigRead, key: str) -> Path:
    return Path(os.path.expandvars(settings.text(key))).expanduser().resolve()


def slot_cargo_root(settings: conf.ConfigRead) -> Path:
    """Read the root for each slot's build output."""
    return _root(settings, "roots.slot_cargo")


def configured_maximum(settings: conf.ConfigRead, machine: str, repository: str) -> int:
    """Read the repository limit or the machine's default limit."""
    key = f"slots.{machine}.{repository}.max"
    return settings.whole(key if key in settings.values() else f"slots.{machine}.default_max")


def _persistent_source(source: Path) -> Path:
    seen: set[Path] = set()
    while True:
        record_path = copy_record.record_path(source)
        # Session folders also hold task clones outside the configured scratch root.
        record = copy_record.read_record(record_path if record_path is not None else source / copy_record.RECORD)
        if isinstance(record, copy_record.NotRegistered):
            return source
        if source in seen:
            raise SlotGitFailure(f"slots: copy sources form a cycle at {source}; cannot create a persistent slot")
        seen.add(source)
        original = record.fields.get("source_path") if isinstance(record, copy_record.Registered) else None
        if not isinstance(original, str) or not original or not Path(original).is_dir():
            raise SlotGitFailure(f"slots: {source} has no available persistent source; create its pool from the original repository")
        source = source_checkout(Path(original))


# Git has no helper for this scheme, so a push to a remote with this push URL fails.
_NO_PUSH_URL = "no-push://a-copy-pool-never-pushes"


def _filters(settings: git_remotes.GitSettings) -> git_remotes.GitSettings:
    return {key: values for key, values in settings.items() if key.startswith("filter.")}


@contextmanager
def _pool_lock(pool: Path) -> Generator[int]:
    """Hold the one lock for each write to the pool configuration, and yield its descriptor.

    Pass the descriptor to each Git child, so a write keeps the lock if the take process exits.
    """
    with (pool.parent / f"{pool.name}.lock").open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        yield lock.fileno()


def _write_pool_marker(pool: Path, source: Path) -> None:
    """Replace the pool marker in one rename, so a reader never sees a partial path."""
    marker = pool / ".git" / build_folders.POOL_MARKER
    # A failed write keeps its temporary file. Its name is never the marker name, so no reader takes it.
    temporary = marker.with_name(f"{marker.name}.{uuid4().hex}")
    with temporary.open("w", encoding="utf-8") as stream:
        _ = stream.write(f"{source}\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, marker)


_POOL_STAGE = re.compile(r"(?P<pool>.+)\.(?P<kind>clone|failed-clone)-[a-z0-9_]{8}")


@dataclass(frozen=True)
class PoolStage:
    """A folder that a pool clone left beside the pool. It waits for a manual removal."""

    path: Path
    pool_name: str
    failed: bool

    @property
    def reason(self) -> str:
        return "failed pool clone" if self.failed else "crashed pool clone"


def kept_pool_stages(folder: Path) -> list[PoolStage]:
    """List each folder that a pool clone left in the `.pool` folder `folder`. Delete nothing.

    A take renames the stage of a failed clone to `<name>.failed-clone-XXXXXXXX`.
    A crashed take leaves `<name>.clone-XXXXXXXX`.
    """
    try:
        entries = sorted(folder.iterdir())
    except FileNotFoundError:
        return []
    stages: list[PoolStage] = []
    for entry in entries:
        match = _POOL_STAGE.fullmatch(entry.name)
        if match and not entry.is_symlink() and entry.is_dir():
            stages.append(PoolStage(entry, match["pool"], match["kind"] == "failed-clone"))
    return stages


def _stage_hex() -> str:
    return uuid4().hex[:8]


def _new_stage(parent: Path, pool_name: str) -> Path:
    """Make an empty `<name>.clone-XXXXXXXX` folder. Its mode follows the umask, as the mode of a Git clone does.

    Skip a suffix that a clone or failed-clone folder has, so a later failed-clone rename never replaces a folder.
    """
    while True:
        suffix = _stage_hex()
        stage = parent / f"{pool_name}.clone-{suffix}"
        if os.path.lexists(parent / f"{pool_name}.failed-clone-{suffix}"):
            continue
        try:
            stage.mkdir()
        except FileExistsError:
            continue
        return stage


def _fill_pool(stage: Path, source: Path, git: git_remotes.GitRun, locked: tuple[int, ...]) -> None:
    """Fetch `source` into a new repository in the empty folder `stage`, and prepare it as a pool.

    `git clone` deletes its repository when it fails, so the pool uses `init` and `fetch`.
    A failed fetch keeps the repository.
    """
    # A pool has the object format of its live checkout, so a sha256 repository also gets a pool.
    object_format = _git(source, "rev-parse", "--show-object-format", pass_fds=locked)
    _ = git(stage, "init", "--quiet", f"--object-format={object_format}")
    _ = git(stage, "remote", "add", "origin", str(source))
    # The clone refspecs: each branch as a remote ref, each tag, and HEAD. A local fetch never hardlinks objects.
    _ = git(stage, "fetch", "--quiet", "--tags", "origin", "+refs/heads/*:refs/remotes/origin/*",
            "+HEAD:refs/remotes/origin/HEAD")
    # The pool has no local branch, so its HEAD is detached. Remote refs hold the branches.
    head = _git(stage, "rev-parse", "--verify", "refs/remotes/origin/HEAD^{commit}", pass_fds=locked)
    _ = _git(stage, "update-ref", "--no-deref", "HEAD", head, pass_fds=locked)
    _write_pool_marker(stage, source)


def _keep_failed_stage(stage: Path, pool_name: str) -> str:
    """Rename the stage of a failed clone, so that no later take reads it. Return the note that names the kept folder."""
    failed = stage.with_name(f"{pool_name}.failed-clone-{stage.name.removeprefix(f'{pool_name}.clone-')}")
    kept = f"kept for a manual removal list: {stage} (build_slots, crashed pool clone"
    # A rename onto an empty folder replaces it, so never rename onto a path that exists.
    if os.path.lexists(failed):
        return f"{kept}; {failed} exists)"
    try:
        _ = stage.rename(failed)
    except OSError as error:
        return f"{kept}; the rename failed: {error})"
    return f"kept for a manual removal list: {failed} (build_slots, failed pool clone)"


def pool_repository(source: Path) -> Path:
    """Make or refresh the private clone whose linked worktrees are the numbered copies of `source`.

    The pool is a non-bare clone without a checkout at `<build root>/<repo>/.pool/<checkout name>`. Its HEAD is detached,
    and its `origin` is the upstream URL of `source`. Each call copies the refs and `filter.*` keys of
    `source` into the pool. A pool owns its objects, so no copy writes to `source`.
    """
    if build_folders.pool_source(source) is not None:
        return source
    source = source.resolve()
    pool = build_folders.pool_path(source)
    # pool_path reads the origin URL and refuses a source without one.
    origin = _git(source, "remote", "get-url", "origin")
    pool.parent.mkdir(parents=True, exist_ok=True)
    with _pool_lock(pool) as lock:
        locked = (lock,)

        def git(repo: Path, *arguments: str, data: bytes | None = None) -> bytes:
            return _pool_git(repo, *arguments, data=data, pass_fds=locked)

        new = not os.path.lexists(pool)
        if new:
            # A crashed clone leaves its stage folder. The pool lock excludes a clone that runs now.
            for stage in kept_pool_stages(pool.parent):
                if stage.pool_name == pool.name and not stage.failed:
                    print(f"kept for a manual removal list: {stage.path} (build_slots, {stage.reason})", file=sys.stderr)
            # Fill a new stage folder and rename it, so an interrupted clone never looks like a pool.
            staged = _new_stage(pool.parent, pool.name)
            try:
                _fill_pool(staged, source, git, locked)
                _ = staged.rename(pool)
            except BaseException as error:
                if os.path.lexists(pool) and not os.path.lexists(staged):
                    # The interrupt came after the rename. The pool is complete, and no stage stays.
                    raise
                # No code deletes the pool or a stage folder. Keep the stage under a name that no take reads.
                note = _keep_failed_stage(staged, pool.name)
                if isinstance(error, Exception):
                    raise SlotGitFailure(f"slots: the copy pool clone of {source} failed; {note}: {error}") from error
                error.add_note(note)
                raise
        served = build_folders.pool_source(pool)
        if served is not None and served != source and not (served / ".git").exists():
            # The live checkout moved or was deleted. The pool now serves the checkout at the new path.
            _write_pool_marker(pool, source)
            served = source
        if served != source:
            # Envrc trust follows the pool to one live checkout, so a pool never takes a second one.
            raise SlotGitFailure(f"slots: {pool} is not the copy pool of {source}; it serves {served}")
        # One read of the local pool keys serves the remote, filter and pushurl checks.
        settings = git_remotes.read_git_settings(git, pool, "--local")
        git_remotes.refresh_remotes(git, pool, source, origin, new_checkout=new, settings=settings)
        # The local filter.* keys of the pool equal the live local keys. Both read the same global keys.
        live_filters = _filters(git_remotes.read_git_settings(git, source, "--local"))
        pool_filters = _filters(settings)
        for key in sorted(live_filters.keys() | pool_filters.keys()):
            if pool_filters.get(key) != live_filters.get(key):
                if key in pool_filters:
                    _ = git(pool, "config", "--local", "--unset-all", key)
                for value in live_filters.get(key, []):
                    _ = git(pool, "config", "--local", "--add", key, value)
        # A push from a copy must never reach a repository on this machine.
        for remote, blocked in (("source", True), ("origin", git_remotes.local_url(origin))):
            key = f"remote.{remote}.pushurl"
            if blocked and settings.get(key) != [_NO_PUSH_URL]:
                _ = git(pool, "config", "--local", "--replace-all", key, _NO_PUSH_URL)
            elif not blocked and settings.get(key) == [_NO_PUSH_URL]:
                _ = git(pool, "config", "--local", "--unset-all", key)
    return pool.resolve()


@contextmanager
def _slot_commit(repository: Path, source: Path, commit: str) -> Generator[None]:
    """Retain one imported commit in the owner repository until the take ends."""
    if git_files.at(repository).common_dir == git_files.at(source).common_dir:
        yield
        return
    reference = f"refs/build-slots/take-{uuid4().hex}"
    previous = _git(repository, "rev-parse", "HEAD")
    # A commit from the live checkout of the pool can have no parent, such as a snapshot of a dirty worktree.
    served = build_folders.pool_source(repository)
    own = served is not None and git_files.at(served).common_dir == git_files.at(source).common_dir
    try:
        _ = _pool_git(repository, "fetch", "--no-tags", "--no-write-fetch-head", "--force", "--",
                      str(source), f"{commit}:{reference}")
        if not own and not _git(repository, "merge-base", reference, previous, accepted=(0, 1)):
            raise SlotRepositoryMismatch(repository, source)
        yield
    finally:
        _ = _git(repository, "update-ref", "-d", reference)


def _rescue(path: Path, output: Sequence[str] = ()) -> None:
    """Save the source work, but not target, the use stamp or the `output` folders."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    name = f"{path.name}-{stamp}"
    generated = [path / "target", path / build_folders.COPY_USE_STAMP, *(path / folder for folder in output)]
    try:
        _ = git_rescue.rescue(path, name, generated=generated,
                             pass_fds=build_folders.held_build_folder_descriptors())
    except git_rescue.RescueError as error:
        if "status" not in error.command or "index" not in error.stderr:
            raise
        index = git_files.at(path).git_dir / "index"
        with tempfile.NamedTemporaryFile(prefix=".rescue-index-", dir=path, delete=False) as backup:
            saved_index = Path(backup.name)
        _ = index.replace(saved_index)
        try:
            descriptor = build_folders.build_folder_lock_fd(path)
            _ = _git(path, "read-tree", "HEAD", pass_fds=(descriptor,))
            _ = git_rescue.rescue(path, name, generated=generated,
                                 pass_fds=build_folders.held_build_folder_descriptors())
        except BaseException:
            _ = saved_index.replace(index)
            raise
        saved_index.unlink()


def _enable_worktree_config(path: Path, common_dir: Path, descriptor: int) -> None:
    read_extension = ("config", "--local", "--type=bool", "--get", "extensions.worktreeConfig")
    if _git(path, *read_extension, accepted=(0, 1), pass_fds=(descriptor,)) == "true":
        return
    # The common Git folder of a copy is in its pool, so the migration takes the pool lock.
    with _pool_lock(common_dir.parent) as lock:
        descriptors = (descriptor, lock)
        if _git(path, *read_extension, accepted=(0, 1), pass_fds=descriptors) == "true":
            return
        # Preserve the main checkout's location when Git enables worktree configuration.
        for key in ("core.worktree", "core.bare"):
            value = _git(path, "config", "--local", "--get", key, accepted=(0, 1), pass_fds=descriptors)
            if value:
                _ = _git(path, "config", "--file", str(common_dir / "config.worktree"),
                         key, value, pass_fds=descriptors)
                _ = _git(path, "config", "--local", "--unset", key, pass_fds=descriptors)
        _ = _git(path, "config", "--local", "extensions.worktreeConfig", "true", pass_fds=descriptors)


def _exclude_slot_target(path: Path, descriptor: int) -> None:
    """Keep build output ignored only in this slot's worktree."""
    checkout = git_files.at(path)
    _enable_worktree_config(path, checkout.common_dir, descriptor)
    exclude = checkout.git_dir / "info/exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    text = exclude.read_text() if exclude.exists() else ""
    missing = [pattern for pattern in ("/target/", f"/{build_folders.COPY_USE_STAMP}")
               if pattern not in text.splitlines()]
    if missing:
        with exclude.open("a") as output:
            _ = output.write(("\n" if text and not text.endswith("\n") else "") + "\n".join(missing) + "\n")
    _ = _git(path, "config", "--worktree", "core.excludesFile", str(exclude), pass_fds=(descriptor,))


@dataclass(frozen=True)
class LeasedCopy:
    """The source and Cargo paths of a held test copy."""

    path: Path
    repository: str
    number: int
    env: Mapping[str, str]
    pass_fds: tuple[int, ...]

    @property
    def cargo_dir(self) -> Path:
        return self.path / "target"


def _copy_order(parent: Path) -> Generator[int]:
    try:
        numbers = build_folders.copy_numbers(parent)
    except FileNotFoundError:
        numbers = {}
    existing = {number for number, folder in numbers.items() if folder}
    old = {number for number in existing if build_folders.old_layout_copy(parent, number)}
    if old:
        # A take never reuses an old copy. Skip each one without its lock, and name them in one line.
        paths = ", ".join(str(parent / str(number)) for number in sorted(old))
        print(f"kept for a manual removal list: {paths} (build_slots, old layout)", file=sys.stderr)
    yield from sorted(existing - old)
    number = 1
    while True:
        if number not in existing:
            yield number
        number += 1


@contextmanager
def take_copy(source: Path, commit: str, *, showrunner: str, unit: str, work: str,
              purpose: str, selection: SlotSelection = LOWEST_FREE_SLOT,
              live: Path | None = None) -> Generator[LeasedCopy]:
    """Take the assigned number or the first free copy.

    `source` holds `commit`. `live` names the checkout whose pool holds the copy; the default is `source`.
    """
    repository = pool_repository(_persistent_source(source_checkout(source if live is None else live)))
    if build_folders.pool_source(repository) is None:
        raise SlotGitFailure(f"slots: {repository} is not a copy pool under {build_folders.build_root()}; "
                             + "a copy never links to a live checkout")
    name = build_folders.repo_word(repository)
    revision = _git(source, "rev-parse", "--verify", "--end-of-options", f"{commit}^{{commit}}")
    if _git(source, "ls-tree", revision, "--", "target", build_folders.COPY_USE_STAMP):
        raise SlotGitFailure(f"{revision}: the commit tracks a reserved copy path")
    parent = build_folders.unit_folder(name, unit)
    checkout = build_folders.SlotCheckout(repository, revision)
    numbers = (selection.number,) if isinstance(selection, ExactSlotRequest) else _copy_order(parent)
    with _slot_commit(repository, source, revision), ExitStack() as unit_claim:
        _ = unit_claim.enter_context(build_folders.unit_allocation_lock(parent, exclusive=False))
        for number in numbers:
            spec = build_folders.BuildFolderSpec(kind="copy", parent=build_folders.build_root(),
                suffix_length=0, repository=name, slot=number, showrunner=showrunner, unit=unit,
                work=work, purpose=purpose, slot_checkout=checkout, machine=host_name())
            with ExitStack() as held:
                try:
                    path = held.enter_context(build_folders.slot_build_folder(spec))
                except build_folders.SlotSourceBusy:
                    raise
                except build_folders.OldLayoutCopy as error:
                    if isinstance(selection, ExactSlotRequest):
                        raise build_folders.OldLayoutCopy(
                            f"assigned copy {number} at {parent / str(number)} is an old-layout copy "
                            + "on the manual removal list; a person must remove it") from error
                    continue
                except (BlockingIOError, FileExistsError):
                    continue
                descriptor = build_folders.build_folder_lock_fd(path)
                record = build_folders.numbered_copy_record(path)
                if not isinstance(record, build_folders.NumberedCopySource):
                    continue
                build_folders.mark_folder_use(path)
                index = record.git_directory / "index"
                index.with_name("index.lock").unlink(missing_ok=True)
                if not os.path.lexists(index):
                    _ = _git(path, "read-tree", "HEAD", pass_fds=(descriptor,))
                scan = ("ls-files", "--others", "--directory", "--no-empty-directory", "-z", "--exclude=/target/",
                        *(f"--exclude={folder}/" for folder in _TOOL_OUTPUT_FOLDERS))
                rescued = False
                try:
                    untracked = _git(path, *scan, pass_fds=(descriptor,))
                except SlotGitFailure as error:
                    if isinstance(error.__cause__, subprocess.TimeoutExpired):
                        raise
                    _rescue(path)
                    rescued = True
                    untracked = _git(path, *scan, pass_fds=(descriptor,))
                if any(name.endswith("/") for name in untracked.split("\0")):
                    print(f"kept for a manual removal list: {path} (build_slots, untracked folders)", file=sys.stderr)
                    continue
                unit_claim.close()
                target = path / "target"
                if target.is_symlink():
                    target.unlink()
                _exclude_slot_target(path, descriptor)
                if not rescued:
                    output = _git(path, *scan, "--ignored", pass_fds=(descriptor,))
                    _rescue(path, [entry for entry in output.split("\0") if entry.endswith("/")])
                _ = _git(path, "update-index", "--really-refresh", accepted=(0, 1))
                _ = _git(path, "checkout", "--force", "--detach", revision)
                _ = _git(path, "clean", "-ffx", "--exclude=/target",
                         f"--exclude=/{build_folders.COPY_USE_STAMP}")
                target.mkdir(exist_ok=True)
                build_folders.record_numbered_copy_source(path, repository, revision)
                yield LeasedCopy(path, name, number, MappingProxyType({
                    "CARGO_TARGET_DIR": str(target), "CARGO_BUILD_BUILD_DIR": str(target),
                    build_folders.NUMBERED_COPY_LOCK_ENV: str(descriptor),
                }), (descriptor,))
                return
        if isinstance(selection, ExactSlotRequest):
            raise BlockingIOError(f"{parent}: assigned copy {selection.number} is unavailable")


def limit_copy_size(copy: LeasedCopy, limit: int) -> None:
    """Discard Cargo output when a held copy exceeds its size limit."""
    size = _size(copy.path)
    if size > limit:
        shutil.rmtree(copy.cargo_dir)
        copy.cargo_dir.mkdir()
        print(f"slots: {copy.path} was {size / 2**30:g} GiB, over its {limit / 2**30:g} GiB limit. "
              + "The maker deleted its Cargo output.")


def _check_directory_identity(path: Path, expected: build_folders.CopyDirectoryIdentity) -> bool:
    try:
        identity = build_folders.CopyDirectoryIdentity.from_stat(path.lstat())
    except FileNotFoundError:
        return False
    if identity != expected:
        raise SlotGitFailure(f"{path}: directory identity changed during copy removal")
    return True


@contextmanager
def _numbered_copy_busy_marks(copy: build_folders.NumberedCopySource, *, dry_run: bool) -> Generator[bool]:
    """Hold the old busy marks even when the checkout is partly removed."""
    with ExitStack() as stack:
        try:
            parents = [copy.common_directory, copy.path, copy.path.parent]
            with suppress(FileNotFoundError):
                info = copy.git_directory.lstat()
                if stat.S_ISDIR(info.st_mode) and build_folders.CopyDirectoryIdentity.from_stat(info) == copy.git_identity:
                    parents.insert(1, copy.git_directory)
            for parent in parents:
                mark = parent / build_folders.BUSY
                create = not dry_run and parent in (copy.common_directory, copy.git_directory)
                with suppress(FileNotFoundError):
                    descriptor = build_folders.open_busy_mark(mark, create=create)
                    _ = stack.callback(os.close, descriptor)
                    mode = fcntl.LOCK_SH if parent == copy.common_directory else fcntl.LOCK_EX
                    fcntl.flock(descriptor, mode | fcntl.LOCK_NB)
                    if not os.path.samestat(os.fstat(descriptor), mark.lstat()):
                        raise BlockingIOError(f"{mark}: the busy mark changed")
        except OSError:
            yield False
        else:
            yield True


def remove_copy_locked(path: Path) -> bool:
    """Return True when an idle numbered copy can go on a manual removal list. Remove nothing."""
    from scripts.lib import scratch

    record = build_folders.numbered_copy_record(path)
    if isinstance(record, build_folders.NoNumberedCopySource):
        return False
    if isinstance(record, build_folders.UnreadableNumberedCopySource):
        raise SlotGitFailure(record.reason)
    copy = record.copy if isinstance(record, build_folders.NumberedCopyRemovalPending) else record
    if not build_folders.may_remove_folder_tree(path, copy.git_directory):
        return False
    if not _check_directory_identity(copy.common_directory, copy.common_identity):
        return False
    _ = _check_directory_identity(path, copy.checkout_identity)
    with _numbered_copy_busy_marks(copy, dry_run=True) as idle:
        if not idle or not isinstance(scratch.borrow_lifetime(path, scratch_settings=conf.read("scratch")), scratch.Ended):
            return False
        for borrower in scratch.registered_copy_paths():
            evidence = scratch.task_of(borrower)
            if isinstance(evidence, copy_record.Registered):
                borrowed = evidence.fields.get("borrows_from")
                if isinstance(borrowed, str) and borrowed:
                    store = Path(borrowed).resolve()
                    if store.is_relative_to(copy.git_directory) or store.is_relative_to(path.resolve()):
                        return False
        return True


def remove_copy(copy: build_folders.NumberedCopySource | build_folders.NumberedCopyRemovalPending) -> bool:
    """Take the removal lease and return True when the free numbered copy can go on a manual removal list.

    Remove nothing. The caller names the copy.
    """
    path = copy.copy.path if isinstance(copy, build_folders.NumberedCopyRemovalPending) else copy.path
    form = build_folders.parse_form(path)
    if isinstance(form, build_folders.UnverifiedFolderPath):
        return False
    if not isinstance(form, build_folders.BuildCopy):
        raise ValueError(f"{path}: expected a numbered copy")
    with build_folders.folder_removal_lease(path, wait=False, create=False,
                                           root=build_folders.build_root()) as lease:
        if not isinstance(lease, build_folders.HeldFolderRemovalLease):
            return False
        if build_folders.numbered_copy_record(path) != copy or not remove_copy_locked(path):
            return False
        return True


def _size(path: Path) -> int:
    total = 0
    for directory, folders, files in os.walk(path):
        current = Path(directory)
        for name in (*folders, *files):
            total += (current / name).lstat().st_size
    return total


def main(argv: Sequence[str]) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Run a command in a numbered test copy.")
    commands = parser.add_subparsers(required=True)
    run = commands.add_parser("run")
    _ = run.add_argument("--source", type=Path, required=True)
    _ = run.add_argument("--commit", required=True)
    _ = run.add_argument("--showrunner", required=True)
    _ = run.add_argument("--unit", required=True)
    _ = run.add_argument("--work", required=True)
    _ = run.add_argument("--purpose", required=True)
    _ = run.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = cast(list[str], args.command)
    if command[:1] == ["--"]:
        command = command[1:]
    try:
        with take_copy(cast(Path, args.source), cast(str, args.commit), showrunner=cast(str, args.showrunner),
                       unit=cast(str, args.unit), work=cast(str, args.work), purpose=cast(str, args.purpose)) as copy:
            if not command:
                print(copy.path, flush=True)
                _ = sys.stdin.read(1)
                return 0
            descriptor = build_folders.build_folder_lock_fd(copy.path)
            environment = {**os.environ, **copy.env, "BUILD_FOLDER": str(copy.path),
                           "BUILD_FOLDER_LOCK_FD": str(descriptor)}
            return build_folders.run_build_command(command, path=copy.path, environment=environment,
                                                   pass_fds=build_folders.held_build_folder_descriptors())
    except (OSError, ValueError, SlotGitFailure, subprocess.SubprocessError) as error:
        print(f"copies: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
