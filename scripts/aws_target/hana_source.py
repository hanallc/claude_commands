"""Verify exact Git source and hold its numbered guest copy."""
from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
from functools import partial
import hashlib
import os
from pathlib import Path
import re
import subprocess

from scripts.aws_target import hana_protocol
from scripts.aws_target.hana_protocol import MissingBasis as MissingBasis
from scripts.lib import build_folders, build_slots, conf

def read_limits(configuration: conf.ConfigRead) -> hana_protocol.SourceLimits:
    return hana_protocol.SourceLimits(configuration.whole('source_git_timeout_s'),
                                      configuration.words('source_allowed_environment'))


@dataclass(frozen=True)
class LeasedSource:
    copy: build_slots.LeasedCopy
    identity: hana_protocol.SourceIdentity

    @property
    def path(self) -> Path:
        return self.copy.path

    @property
    def descriptors(self) -> tuple[int, ...]:
        """Retain the copy lease in every command child."""
        return self.copy.pass_fds


def git(repository: Path, limits: hana_protocol.SourceLimits, *arguments: str, accepted: tuple[int, ...] = (0,),
        descriptors: tuple[int, ...] = ()) -> str:
    command = ['git', '-C', str(repository), *arguments]
    result = subprocess.run(command, capture_output=True, text=True, timeout=limits.git_timeout_s,
                            pass_fds=(*build_folders.held_build_folder_descriptors(), *descriptors))
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


@contextmanager
def materialize(manifest: hana_protocol.SourceManifest, limits: hana_protocol.SourceLimits,
                verified_bundle: VerifiedGitBundle, *, number: int, work: str) -> Generator[LeasedSource | MissingBasis]:
    """Fetch a verified bundle and retain the assigned copy through the command."""
    hana_protocol.validate_manifest(manifest, limits)
    repository = hana_protocol.REPOSITORY
    if manifest.repository.rsplit('-', 1)[0] != repository:
        raise hana_protocol.SourceRefused('The manifest does not name the Hana repository')
    configuration = conf.read_or_repository('build_folders')
    copy_limit = configuration.whole('copies.aws-hana.hana.max')
    size_limit = configuration.whole('copies.aws-hana.hana.max_bytes')
    if type(number) is not int or not 1 <= number <= copy_limit:
        raise ValueError('The assigned copy number exceeds the configured limit')
    main = hana_protocol.main_checkout(Path.home())
    metadata = main / '.git'
    if main.is_symlink() or metadata.is_symlink():
        raise hana_protocol.SourceRefused('The Hana main checkout must use local Git metadata')
    metadata.mkdir(parents=True, exist_ok=True)
    with (metadata / 'hana-source.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        source_git = partial(git, main, limits, descriptors=(lock.fileno(),))
        if not (metadata / 'HEAD').exists():
            _ = source_git('init', '--quiet',
                           '--object-format=' + ('sha256' if len(manifest.commit) == 64 else 'sha1'))
        if not source_git('config', '--local', '--get', 'remote.origin.url', accepted=(0, 1)):
            _ = source_git('config', '--local', 'remote.origin.url', repository)
        if build_folders.repo_word(main) != repository:
            raise hana_protocol.SourceRefused('The Hana main checkout has a different repository name')
        with build_folders.tool_folder('hana-bundle', parent=metadata) as temporary:
            bundle = temporary / 'source.bundle'
            verified_bundle.copy_to(bundle)
            prerequisites = tuple(dict.fromkeys((*manifest.basis_commits, *bundle_prerequisites(bundle))))
            missing = tuple(commit for commit in prerequisites
                            if not source_git('rev-parse', '--verify', '--quiet', commit + '^{commit}', accepted=(0, 1)))
            if missing:
                yield MissingBasis(manifest.repository, missing)
                return
            _ = source_git('bundle', 'verify', str(bundle))
            heads = source_git('bundle', 'list-heads', str(bundle)).splitlines()
            if heads != [manifest.commit + ' refs/heads/hana-source']:
                raise hana_protocol.SourceRefused('BundleCommitMismatch: bundle head differs from the manifest')
            _ = source_git('-c', 'fetch.fsckObjects=true', 'fetch', '--quiet', '--no-tags', str(bundle),
                           '+refs/heads/hana-source:refs/hana/source')
            actual = source_git('rev-parse', 'refs/hana/source^{commit}')
            if actual != manifest.commit:
                raise hana_protocol.SourceRefused('SourceCommitMismatch: fetched commit differs from the manifest')
        _ = source_git('checkout', '--quiet', '--detach', manifest.commit)
    with build_slots.take_copy(main, manifest.commit, showrunner='hana', unit=hana_protocol.COPY_UNIT, work=work,
                               purpose='hana-source', selection=build_slots.ExactSlotRequest(number)) as copy:
        if toolchain_identity(copy.path) != manifest.toolchain:
            raise hana_protocol.SourceRefused('ToolchainMismatch: copy toolchain differs from the manifest')
        build_slots.limit_copy_size(copy, size_limit)
        yield LeasedSource(copy, hana_protocol.SourceIdentity(manifest.repository, manifest.commit, manifest.toolchain))
