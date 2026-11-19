"""Verify exact Git source and hold its shared guest slot."""
from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import os
from pathlib import Path
import re
import socket
import subprocess

from scripts.aws_target import hana_protocol
from scripts.lib import build_folders, build_slots, conf

# Match the control protocol without importing controller or AWS modules.
SOURCE_ROOT = Path('/var/lib/hana')


def read_limits(configuration: conf.ConfigRead) -> hana_protocol.SourceLimits:
    return hana_protocol.SourceLimits(configuration.whole('source_git_timeout_s'),
                                      configuration.words('source_allowed_environment'))


@dataclass(frozen=True)
class MissingBasis:
    repository: str
    commits: tuple[str, ...]


@dataclass(frozen=True)
class LeasedSource:
    slot: build_slots.Slot
    identity: hana_protocol.SourceIdentity

    @property
    def path(self) -> Path:
        return self.slot.path

    @property
    def descriptors(self) -> tuple[int, ...]:
        """Retain the mirror and slot leases in every command child."""
        return build_folders.held_build_folder_descriptors()


def git(repository: Path, limits: hana_protocol.SourceLimits, *arguments: str, accepted: tuple[int, ...] = (0,)) -> str:
    command = ['git', '-C', str(repository), *arguments]
    result = subprocess.run(command, capture_output=True, text=True, timeout=limits.git_timeout_s,
                            pass_fds=build_folders.held_build_folder_descriptors())
    if result.returncode not in accepted:
        error = subprocess.CalledProcessError(result.returncode, command, result.stdout, result.stderr)
        raise hana_protocol.SourceRefused(f'GitSourceRefused: {error}') from error
    return result.stdout.strip()


def toolchain_identity(path: Path) -> str:
    source = path / 'rust-toolchain.toml'
    if source.is_symlink() or not source.is_file():
        raise hana_protocol.SourceRefused('ToolchainMissing: rust-toolchain.toml must be a regular file')
    return hashlib.sha256(source.read_bytes()).hexdigest()


@dataclass(frozen=True)
class VerifiedGitBundle:
    """The range of the Git bundle in the archive that the bootstrap verified."""

    descriptor: int
    offset: int
    size: int

    def copy_to(self, destination: Path) -> None:
        with destination.open('wb') as output:
            offset = self.offset
            remaining = self.size
            while remaining:
                data = os.pread(self.descriptor, min(remaining, 1024 * 1024), offset)
                if not data:
                    raise hana_protocol.SourceRefused('SourceBundleRefused: the verified Git bundle is incomplete')
                _ = output.write(data)
                offset += len(data)
                remaining -= len(data)


def bundle_prerequisites(bundle: Path) -> tuple[str, ...]:
    """Read bounded prerequisite identities before Git verifies the complete bundle."""
    prerequisites: list[str] = []
    consumed = 0
    with bundle.open('rb') as stream:
        while True:
            line = stream.readline(hana_protocol.CONTROL_MAX_BYTES + 1)
            consumed += len(line)
            if consumed > hana_protocol.CONTROL_MAX_BYTES:
                raise hana_protocol.SourceRefused('BundleHeaderBound: source bundle header exceeds the protocol limit')
            if not line or line == b'\n':
                return tuple(prerequisites)
            if line.startswith(b'-'):
                commit = line[1:].split(b' ', 1)[0].decode('ascii', errors='replace')
                if not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', commit):
                    raise hana_protocol.SourceRefused('InvalidBundleHeader: prerequisite is invalid')
                prerequisites.append(commit)


def admit_slot_keys(repository: str) -> None:
    """Require the user's explicit guest pool activation and capacity values."""
    machine = socket.gethostname().split('.')[0]
    configuration = conf.read_or_repository('build_folders')
    if not configuration.values().get(f'roots.slots.{machine}'):
        raise build_slots.SlotsNotConfigured(machine)
    _ = configuration.whole(f'slots.{machine}.{repository}.max')
    _ = configuration.whole(f'slots.{machine}.{repository}.max_bytes')


@contextmanager
def materialize(manifest: hana_protocol.SourceManifest, limits: hana_protocol.SourceLimits,
                verified_bundle: VerifiedGitBundle, *, showrunner: str, unit: str, work: str,
                root: Path = SOURCE_ROOT,
                selection: build_slots.SlotSelection = build_slots.LOWEST_FREE_SLOT) -> Generator[LeasedSource | MissingBasis]:
    """Verify a bundle and retain the slot lease through the caller's command."""
    hana_protocol.validate_manifest(manifest, limits)
    repository_name = manifest.repository.rsplit('-', 1)[0]
    admit_slot_keys(repository_name)
    mirrors = root / 'mirrors' / manifest.repository
    mirrors.mkdir(parents=True, exist_ok=True)
    spec = build_folders.BuildFolderSpec(kind='mirror', parent=root / 'mirrors', suffix_length=0,
                                         repository=manifest.repository, owner='hana', slot=0)
    with build_folders.leased_build_folder(spec) as mirror, (mirrors / 'source.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not (mirror / '.git/HEAD').exists():
            _ = git(mirror, limits, 'init', '--bare', '--quiet',
                    '--object-format=' + ('sha256' if len(manifest.commit) == 64 else 'sha1'), '.git')
        # Keep bare state on the mirror while linked slots inherit normal worktree state.
        _ = git(mirror, limits, 'config', 'extensions.worktreeConfig', 'true')
        _ = git(mirror, limits, 'config', '--local', 'core.bare', 'false')
        _ = git(mirror, limits, 'config', '--worktree', 'core.bare', 'true')
        _ = git(mirror, limits, 'config', '--worktree', 'core.worktree', str(mirror))
        _ = git(mirror, limits, 'config', 'remote.origin.url', repository_name)
        with build_folders.tool_folder('hana-bundle') as temporary:
            bundle = temporary / 'source.bundle'
            verified_bundle.copy_to(bundle)
            prerequisites = tuple(dict.fromkeys((*manifest.basis_commits, *bundle_prerequisites(bundle))))
            missing = tuple(commit for commit in prerequisites
                            if not git(mirror, limits, 'rev-parse', '--verify', '--quiet', commit + '^{commit}', accepted=(0, 1)))
            if missing:
                yield MissingBasis(manifest.repository, missing)
                return
            _ = git(mirror, limits, 'bundle', 'verify', str(bundle))
            heads = git(mirror, limits, 'bundle', 'list-heads', str(bundle)).splitlines()
            if heads != [manifest.commit + ' refs/heads/hana-source']:
                raise hana_protocol.SourceRefused('BundleCommitMismatch: bundle head differs from the manifest')
            _ = git(mirror, limits, '-c', 'fetch.fsckObjects=true', 'fetch', '--quiet', '--no-tags', str(bundle),
                    '+refs/heads/hana-source:refs/heads/hana-source')
            actual = git(mirror, limits, 'rev-parse', 'refs/heads/hana-source^{commit}')
            if actual != manifest.commit:
                raise hana_protocol.SourceRefused('SourceCommitMismatch: fetched commit differs from the manifest')
        fcntl.flock(lock, fcntl.LOCK_UN)
        with build_slots.take_slot(mirror, manifest.commit, showrunner=showrunner, unit=unit, work=work,
                                  purpose='hana-source', wait_policy=build_slots.SlotWaitPolicy.REFUSE,
                                  selection=selection) as slot:
            if toolchain_identity(slot.path) != manifest.toolchain:
                raise hana_protocol.SourceRefused('ToolchainMismatch: slot toolchain differs from the manifest')
            yield LeasedSource(slot, hana_protocol.SourceIdentity(manifest.repository, manifest.commit, manifest.toolchain))
