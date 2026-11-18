"""Seed a new Cargo build directory from a locked local sibling."""

from __future__ import annotations

import ctypes
import fcntl
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
from collections.abc import Callable, Generator, Iterable, Mapping, Sequence
from contextlib import ExitStack, closing, contextmanager
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from typing import cast

from scripts.buildlog import index, repository, step_context, store
from scripts.lib import conf, git_files
from scripts.lib.cargo_output import CargoOutputRoots, CargoOutputUnavailable, cargo_metadata_output_roots
from scripts.lib.cargo_target_config import cargo_config_folders, cargo_config_values, cargo_home_folder, cargo_path_argument


GIB = 1 << 30
COMPILE_STEPS = ("build", "check", "clippy", "doc", "mend", "nextest", "test")
GIT_SHA = re.compile(r"[0-9a-f]{7,64}")
UNIT_HASH = re.compile(r"[0-9a-f]{16}")
MAKE_WORD = re.compile(r"(?:\\.|[^\s\\])+")
PROFILE_FOLDER = {"dev": "debug", "test": "debug", "bench": "release"}
METADATA_TIMEOUT_SECONDS = 5
STAGING_DIRECTORY = ".cargo-seed"
DESTINATION_LOCK_COUNT = 64


@dataclass(frozen=True)
class SeedLimits:
    cargo_root: Path
    max_bytes: int
    min_free_bytes: int


@dataclass(frozen=True)
class SeedDisabled:
    """This machine has no enabled seed policy."""


@dataclass(frozen=True)
class UnmanagedBuildDirectory:
    """The build directory is outside the checkout's managed assignment."""


@dataclass(frozen=True)
class HostCompileProfile:
    name: str

    @property
    def folder(self) -> str:
        return PROFILE_FOLDER.get(self.name, self.name)


@dataclass(frozen=True)
class UnsupportedCompile:
    """This command has no supported host profile to seed."""


@dataclass(frozen=True)
class Sibling:
    worktree: Path
    build_directory: Path
    target_directory: Path
    revision: str
    completed_at: str


@dataclass(frozen=True)
class RankedSibling:
    sibling: Sibling
    distance: int


@dataclass(frozen=True)
class Seeded:
    donor: RankedSibling
    copied_bytes: int
    seconds: float


@dataclass(frozen=True)
class ColdStart:
    reason: str


@dataclass(frozen=True)
class BuildDirectoryExists:
    """Cargo already has a build directory."""


@dataclass(frozen=True)
class ReusableFile:
    source: Path
    size: int


class CheckoutAccess(Enum):
    BUILD = auto()
    REMOVE = auto()


def seed_limits(settings: conf.ConfigRead, host: str) -> SeedLimits | SeedDisabled:
    enabled = settings.origin(f"seed.enabled.{host}", int)
    if isinstance(enabled, conf.UnavailableValue):
        enabled = settings.origin("seed.enabled", int)
    if isinstance(enabled, conf.UnavailableValue) or enabled.value != 1:
        return SeedDisabled()
    values = [settings.origin(key, int) for key in ("seed.max_gib", "seed.min_free_gib")]
    maximum, minimum = values
    if (isinstance(maximum, conf.UnavailableValue) or maximum.value <= 0
            or isinstance(minimum, conf.UnavailableValue) or minimum.value < 0):
        raise ValueError("invalid seed size or free space limit")
    return SeedLimits(managed_cargo_root(settings, host).resolve(strict=True),
                      maximum.value * GIB, minimum.value * GIB)


def managed_cargo_root(settings: conf.ConfigRead, host: str) -> Path:
    root = settings.origin(f"roots.slot_cargo.{host}", str)
    if isinstance(root, conf.UnavailableValue):
        root = settings.origin("roots.slot_cargo", str)
    if isinstance(root, conf.UnavailableValue) or not root.value:
        raise ValueError("Cargo root is unavailable")
    return Path(os.path.expandvars(root.value)).expanduser()


def managed_build_directory(checkout: Path, settings: conf.ConfigRead, host: str, cargo_root: Path
                            ) -> Path | UnmanagedBuildDirectory:
    """Find the checkout's assigned directory through the configured mirror."""
    setting = settings.origin(f"roots.cargo_paths.{host}", str)
    if isinstance(setting, conf.UnavailableValue):
        return UnmanagedBuildDirectory()
    mirror = Path(os.path.expandvars(setting.value)).expanduser()
    if not mirror.is_absolute() or not checkout.is_absolute() or checkout == Path(checkout.anchor):
        return UnmanagedBuildDirectory()
    entry = mirror.joinpath(*checkout.parts[1:])
    directory = entry.parent.resolve() / entry.name
    if (cargo_root not in directory.parents or directory.is_symlink()
            or directory.relative_to(cargo_root).parts[0].startswith(STAGING_DIRECTORY)):
        return UnmanagedBuildDirectory()
    return directory


def compile_profile(argv: list[str]) -> HostCompileProfile | UnsupportedCompile:
    keys = index.build_keys(argv)
    if not isinstance(keys, index.CargoBuildKey):
        return UnsupportedCompile()
    shape = cast(dict[str, object], json.loads(keys.base))
    command, profile = shape["command"], shape["profile"]
    if (command not in COMPILE_STEPS or shape["target"] != []
            or not isinstance(profile, str) or not re.fullmatch(r"[a-zA-Z0-9_-]+", profile)):
        return UnsupportedCompile()
    return HostCompileProfile(profile)


def _target_configured(contents: str) -> bool:
    config = cast(dict[str, object], tomllib.loads(contents))
    build = config.get("build")
    # An include can supply a target that this reader cannot resolve.
    return bool(config.get("include")) or (isinstance(build, dict)
                                           and bool(cast(dict[str, object], build).get("target")))


def uses_configured_target(arguments: list[str], cwd: str, environment: Mapping[str, str]) -> bool:
    """Keep native seeding for Cargo commands with no selected target."""
    if environment.get("CARGO_BUILD_TARGET"):
        return True
    for folder in cargo_config_folders(cwd, environment):
        for name in ("config", "config.toml"):
            try:
                contents = (folder / name).read_text(encoding="utf-8")
            except FileNotFoundError:
                continue
            if _target_configured(contents):
                return True
            break
    return any(_target_configured(value if "=" in value else (Path(cwd) / value).read_text(encoding="utf-8"))
               for value in cargo_config_values(arguments))


def read_seed_roots(program: Sequence[str], arguments: list[str], cwd: str, environment: Mapping[str, str]
                    ) -> CargoOutputRoots | CargoOutputUnavailable:
    """Read Cargo output paths with a limit on discovery time."""
    options = [word for value in cargo_config_values(arguments) for word in ("--config", value)]
    manifest = cargo_path_argument(arguments, "--manifest-path")
    if manifest:
        options.extend(("--manifest-path", manifest))
    target = cargo_path_argument(arguments, "--target-dir")
    if target:
        environment = {**environment, "CARGO_TARGET_DIR": target}
    try:
        result = subprocess.run([*program, "metadata", "--format-version", "1", "--no-deps", *options],
                                cwd=cwd, env=environment, capture_output=True, text=True, check=False,
                                timeout=METADATA_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return CargoOutputUnavailable("cargo metadata timed out")
    if result.returncode:
        return CargoOutputUnavailable(result.stderr.strip() or f"cargo metadata exited {result.returncode}")
    return cargo_metadata_output_roots(result.stdout)


def sibling_history(log_root: Path, repo: str, host: str, rustc: str, profile: HostCompileProfile) -> list[Sibling]:
    """Find the last successful local compile for each build directory and profile."""
    with closing(sqlite3.connect(index.read_only_uri(log_root), uri=True, timeout=0.2)) as connection:
        rows = cast(list[tuple[str, str, str, str, str]], connection.execute("""
            SELECT worktree, build_directory, target_directory, sha, ended_at
            FROM (
                SELECT worktree, build_directory, target_directory, sha, ended_at,
                       row_number() OVER (PARTITION BY build_directory ORDER BY ended_at DESC, id DESC) AS rank
                FROM steps
                WHERE repo_identity = ? AND host = ? AND rustc = ? AND status = 0
                    AND step IN ('build', 'check', 'clippy', 'doc', 'mend', 'nextest', 'test')
                    AND build_directory LIKE '/%' AND target_directory LIKE '/%' AND worktree LIKE '/%'
                    AND sha IS NOT NULL AND ended_at IS NOT NULL
                    AND json_extract(build_base, '$.profile') = ?
                    AND json_array_length(build_base, '$.target') = 0
            )
            WHERE rank = 1
            """, (repo, host, rustc, profile.name)).fetchall())
    return [Sibling(Path(worktree), Path(build), Path(target), revision, completed)
            for worktree, build, target, revision, completed in rows]


def rank_siblings(workspace: Path, siblings: Iterable[Sibling]) -> list[RankedSibling]:
    distances: dict[str, int] = {}
    ranked: list[RankedSibling] = []
    for sibling in siblings:
        revision = sibling.revision
        if not GIT_SHA.fullmatch(revision):
            continue
        if revision not in distances:
            count = repository.output(["git", "rev-list", "--count", f"HEAD...{revision}"],
                                      str(workspace), repository.GIT_TIMEOUT_S)
            distances[revision] = int(count) if count and count.strip().isascii() and count.strip().isdigit() else -1
        if distances[revision] >= 0:
            ranked.append(RankedSibling(sibling, distances[revision]))
    ranked.sort(key=lambda donor: donor.sibling.completed_at, reverse=True)
    ranked.sort(key=lambda donor: donor.distance)
    return ranked


def _directory(path: Path) -> bool:
    try:
        return stat.S_ISDIR(path.lstat().st_mode)
    except FileNotFoundError:
        return False


def _regular_file(path: Path) -> bool:
    try:
        return stat.S_ISREG(path.lstat().st_mode)
    except FileNotFoundError:
        return False


@contextmanager
def _lock(path: Path, *, create: bool = False, wait: bool = False) -> Generator[int]:
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
    fd = os.open(path, flags | (os.O_CREAT if create else 0), 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(f"{path} is not a regular lock file")
        fcntl.flock(fd, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        yield fd
    finally:
        os.close(fd)


def _lock_still_names_inode(path: Path, fd: int) -> bool:
    return os.path.samestat(path.lstat(), os.fstat(fd))


@contextmanager
def checkout_guard(checkout: Path, access: CheckoutAccess) -> Generator[None]:
    """Lock the checkout directory itself, independent of any Cargo output setting."""
    fd = os.open(checkout, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        mode = fcntl.LOCK_EX if access is CheckoutAccess.REMOVE else fcntl.LOCK_SH
        fcntl.flock(fd, mode | fcntl.LOCK_NB)
        if not _lock_still_names_inode(checkout, fd):
            raise OSError(f"checkout changed: {checkout}")
        yield
    finally:
        os.close(fd)


@contextmanager
def destination_guards(destinations: Iterable[Path], limits: SeedLimits) -> Generator[None]:
    """Share a fixed set of lock files across destinations."""
    keys = {int.from_bytes(hashlib.sha256(os.fsencode(path)).digest()[:8]) % DESTINATION_LOCK_COUNT
            for path in destinations}
    directory = limits.cargo_root / ".cargo-seed-locks"
    directory.mkdir(mode=0o700, exist_ok=True)
    if not _directory(directory):
        raise OSError(f"{directory} is not a lock directory")
    with ExitStack() as stack:
        for key in sorted(keys):
            _ = stack.enter_context(_lock(directory / str(key), create=True))
        yield


def _registry_inputs(dep_info: Path, registry: Path) -> bool:
    """Require rustc dep-info for a registry unit with no generated inputs."""
    if not _regular_file(dep_info):
        return False
    text = dep_info.read_text(encoding="utf-8")
    # Cargo also trusts this dep-info when a proc macro reads an untracked environment variable.
    if any(line.startswith("# env-dep:OUT_DIR") for line in text.splitlines()):
        return False
    rule = text.replace("\\\n", " ").split("\n", 1)[0]
    _, delimiter, dependencies = rule.partition(": ")
    if not delimiter:
        return False
    inputs = [Path(re.sub(r"\\(.)", r"\1", match[0])) for match in MAKE_WORD.finditer(dependencies)]
    return bool(inputs) and all(path.is_absolute() and path.is_relative_to(registry)
                                and path.resolve(strict=True).is_relative_to(registry) for path in inputs)


def _unit_files(profile: Path, fingerprint: Path, name: str, registry: Path) -> list[Path]:
    unit, _, digest = fingerprint.name.rpartition("-")
    if not unit or not UNIT_HASH.fullmatch(digest) or not _regular_file(fingerprint / name):
        return []
    if name == "build-script-build-script-build":
        build = profile / "build" / fingerprint.name
        if not _directory(build) or not _directory(build.parent):
            return []
        stem = f"build_script_build-{digest}"
        dep_info = build / f"{stem}.d"
        outputs = [build / stem, build / "build-script-build"]
    else:
        stem = f"{name.removeprefix('lib-')}-{digest}"
        deps = profile / "deps"
        if not _directory(deps):
            return []
        dep_info = deps / f"{stem}.d"
        outputs = [deps / f"lib{stem}{extension}" for extension in (".rmeta", ".rlib", ".so")]
    if not _registry_inputs(dep_info, registry):
        return []
    available = [path for path in outputs if _regular_file(path)]
    if not available:
        return []
    marks = [fingerprint / item for item in (name, f"{name}.json", f"dep-{name}", "invoked.timestamp")]
    if not all(_regular_file(path) for path in marks):
        return []
    return [*marks, *available]


def reusable_files(profile: Path, registry: Path) -> Iterable[ReusableFile]:
    """Select registry libraries and compiled build scripts with their fingerprints."""
    root = profile / ".fingerprint"
    if not _directory(root):
        return
    for fingerprint in root.iterdir():
        if not _directory(fingerprint):
            continue
        for mark in fingerprint.glob("*.json"):
            name = mark.stem
            if name.startswith("lib-") or name == "build-script-build-script-build":
                try:
                    paths = _unit_files(profile, fingerprint, name, registry)
                    files = [ReusableFile(path, path.lstat().st_size) for path in paths]
                except (FileNotFoundError, UnicodeError):
                    continue
                yield from files


def _rename_new(source: Path, destination: Path) -> None:
    """Publish the stage only if Cargo has not created the directory."""
    library = ctypes.CDLL(None, use_errno=True)
    function_type = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_int, ctypes.c_char_p,
                                    ctypes.c_int, ctypes.c_char_p, ctypes.c_uint, use_errno=True)
    rename = cast(Callable[[int, bytes, int, bytes, int], int], function_type(("renameat2", library)))
    # AT_FDCWD and RENAME_NOREPLACE are Linux constants.
    if rename(-100, os.fsencode(source), -100, os.fsencode(destination), 1):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), destination)


def _clear_stages(directory: Path) -> None:
    directory.mkdir(mode=0o700, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise OSError(f"{directory} is not a private seed directory")
    for entry in directory.iterdir():
        if _directory(entry):
            shutil.rmtree(entry)
        else:
            entry.unlink()


def _recover_when_idle(limits: SeedLimits) -> None:
    directory = limits.cargo_root / STAGING_DIRECTORY
    if os.path.lexists(directory):
        try:
            with _lock(limits.cargo_root / ".cargo-seed.lock", create=True):
                _clear_stages(directory)
        except BlockingIOError:
            pass


def _copy_sibling(donor: RankedSibling, profile: Path, staging: Path, destination: Path, registry: Path,
                  limits: SeedLimits, locks: list[tuple[Path, int]]) -> Seeded | ColdStart | BuildDirectoryExists:
    started = time.monotonic()
    files: dict[Path, ReusableFile] = {}
    for entry in reusable_files(profile, registry):
        files[entry.source] = entry
    size = sum(entry.size for entry in files.values())
    if not size:
        return ColdStart("sibling has no reusable files")
    if size > limits.max_bytes:
        return ColdStart("sibling exceeds the size cap")
    if shutil.disk_usage(destination.parent).free - size < limits.min_free_bytes:
        return ColdStart("copy would cross the free space floor")
    with tempfile.TemporaryDirectory(prefix="stage-", dir=staging) as temporary:
        payload = Path(temporary) / "output"
        for entry in files.values():
            target = payload / profile.name / entry.source.relative_to(profile)
            target.parent.mkdir(parents=True, exist_ok=True)
            _ = shutil.copy2(entry.source, target, follow_symlinks=False)
            if not _regular_file(target) or target.stat().st_size != entry.size:
                raise OSError(f"sibling file changed: {entry.source}")
        if not all(_lock_still_names_inode(path, fd) for path, fd in locks):
            raise OSError("sibling lock files changed")
        if shutil.disk_usage(destination.parent).free < limits.min_free_bytes:
            return ColdStart("free space fell during the copy")
        try:
            _rename_new(payload, destination)
        except FileExistsError:
            return BuildDirectoryExists()
        return Seeded(donor, size, time.monotonic() - started)


def _seed_locked(roots: CargoOutputRoots, profile: HostCompileProfile, registry: Path, limits: SeedLimits, staging: Path,
                 donors: Iterable[RankedSibling], active_worktrees: Callable[[], set[Path]]
                 ) -> Seeded | ColdStart | BuildDirectoryExists:
    destination = roots.build_directory
    if os.path.lexists(destination):
        return BuildDirectoryExists()
    if not roots.workspace_root.is_dir():
        return ColdStart("workspace is unavailable")
    ancestor = next(path for path in destination.parents if path.is_dir())
    if ancestor.stat().st_dev != limits.cargo_root.stat().st_dev:
        return ColdStart("build directory is on another disk")
    outcome = ColdStart("no safe sibling")
    busy = active_worktrees()
    for donor in donors:
        sibling = donor.sibling
        worktree = sibling.worktree.resolve()
        build = sibling.build_directory.resolve()
        target = sibling.target_directory.resolve()
        if (worktree == roots.workspace_root or worktree in busy or not worktree.is_dir()
                or not build.is_relative_to(limits.cargo_root) or build == limits.cargo_root
                or destination.is_relative_to(build) or build.is_relative_to(destination)
                or destination.is_relative_to(target) or target.is_relative_to(destination)):
            continue
        build_profile, artifact_profile = build / profile.folder, target / profile.folder
        if not _directory(build_profile) or not _directory(artifact_profile):
            continue
        paths = [build_profile / ".cargo-build-lock", artifact_profile / ".cargo-lock",
                 artifact_profile / ".cargo-artifact-lock"]
        with ExitStack() as stack:
            try:
                locks = [(path, stack.enter_context(_lock(path))) for path in paths]
            except OSError:
                continue
            if worktree in active_worktrees():
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            copied = _copy_sibling(donor, build_profile, staging, destination, registry, limits, locks)
            if not isinstance(copied, ColdStart):
                return copied
            outcome = copied
    return outcome


def _seed_under_checkout(roots: CargoOutputRoots, profile: HostCompileProfile, registry: Path, limits: SeedLimits,
                         donors: Iterable[RankedSibling], active_worktrees: Callable[[], set[Path]]
                         ) -> Seeded | ColdStart | BuildDirectoryExists:
    destination = roots.build_directory
    staging = limits.cargo_root / STAGING_DIRECTORY
    if os.path.lexists(destination):
        _recover_when_idle(limits)
        return BuildDirectoryExists()
    if destination == roots.target_directory:
        _recover_when_idle(limits)
        return ColdStart("Cargo uses one directory for artifacts and intermediates")
    with _lock(limits.cargo_root / ".cargo-seed.lock", create=True, wait=True):
        _clear_stages(staging)
        with destination_guards((destination,), limits):
            return _seed_locked(roots, profile, registry, limits, staging, donors, active_worktrees)


def seed(roots: CargoOutputRoots, profile: HostCompileProfile, registry: Path, limits: SeedLimits,
         donors: Iterable[RankedSibling], active_worktrees: Callable[[], set[Path]]
         ) -> Seeded | ColdStart | BuildDirectoryExists:
    """Publish a seed while the checkout stays reserved."""
    with checkout_guard(_checkout_path(roots.workspace_root), CheckoutAccess.BUILD):
        return _seed_under_checkout(roots, profile, registry, limits, donors, active_worktrees)


def _checkout_path(workspace: Path) -> Path:
    try:
        return git_files.locate(workspace).toplevel
    except git_files.NotACheckout:
        return workspace


def admitted_worktrees(log_root: Path) -> set[Path]:
    from scripts.build_queue.admission_service import read_state

    state = read_state(log_root / "admission")
    return {Path(reservation.worktree).resolve() for reservation in state.reservations.values() if reservation.worktree}


def seed_cargo(argv: list[str], cwd: str, host: str, settings: conf.ConfigRead
               ) -> Seeded | ColdStart | BuildDirectoryExists | SeedDisabled | UnsupportedCompile | UnmanagedBuildDirectory:
    limits = seed_limits(settings, host)
    if isinstance(limits, SeedDisabled):
        return limits
    program, arguments, directory, environment = step_context.cargo_command_context(argv, cwd, os.environ)
    home = cargo_home_folder(directory, environment)
    if not program:
        return UnsupportedCompile()
    roots = read_seed_roots(program, arguments, directory, environment)
    if isinstance(roots, CargoOutputUnavailable):
        return ColdStart(roots.reason)
    checkout = _checkout_path(roots.workspace_root)
    if managed_build_directory(checkout, settings, host, limits.cargo_root) != roots.build_directory:
        return UnmanagedBuildDirectory()
    with checkout_guard(checkout, CheckoutAccess.BUILD):
        profile = compile_profile(argv)
        if isinstance(profile, UnsupportedCompile) or not home or uses_configured_target(arguments, directory, environment):
            return UnsupportedCompile()
        registry = (Path(home) / "registry" / "src").resolve()
        if os.path.lexists(roots.build_directory) or roots.build_directory == roots.target_directory:
            return _seed_under_checkout(roots, profile, registry, limits, (), set)
        repo = repository.repository_key(str(roots.workspace_root))
        if repo == store.UNKNOWN_IDENTITY:
            return ColdStart("repository identity is unavailable")
        rustc = subprocess.run([environment.get("RUSTC", "rustc"), *program[1:], "-V"],
                               cwd=directory, env=environment, capture_output=True, text=True, timeout=3, check=True)
        log_root = store.root()
        history = sibling_history(log_root, repo, host, rustc.stdout.strip(), profile)
        siblings = rank_siblings(roots.workspace_root, history)
        if not siblings:
            return _seed_under_checkout(roots, profile, registry, limits, (), set)
        return _seed_under_checkout(roots, profile, registry, limits, siblings, lambda: admitted_worktrees(log_root))


def main(argv: list[str]) -> int:
    if sys.platform != "linux":
        return 0
    try:
        result = seed_cargo(argv, os.getcwd(), store.host_name(), conf.read(conf.path("build_folders")))
        if isinstance(result, Seeded):
            donor = result.donor.sibling
            print(f"cargo seed: donor={json.dumps(str(donor.build_directory))} distance={result.donor.distance} "
                  + f"bytes={result.copied_bytes} seconds={result.seconds:.3f}")
        elif isinstance(result, ColdStart):
            print(f"cargo seed: {result.reason.splitlines()[0]}. Starting cold.")
    except (OSError, ValueError, RuntimeError, AttributeError, sqlite3.Error,
            subprocess.SubprocessError, KeyboardInterrupt) as error:
        reason = str(error).splitlines()
        print(f"cargo seed: {reason[0] if reason else type(error).__name__}. Starting cold.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
