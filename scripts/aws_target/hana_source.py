"""Verify exact Git source and hold its shared guest slot."""
from __future__ import annotations

from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
import fcntl
import hashlib
import json
from pathlib import Path
import re
import socket
import subprocess
from typing import cast
from urllib.parse import urlsplit

from scripts.lib import build_folders, build_slots, conf

# Match the control protocol without importing controller or AWS modules.
MANIFEST_MAX_BYTES = 65536
SOURCE_ROOT = Path('/var/lib/hana')


class SourceRefused(ValueError):
    """The source cannot satisfy the requested identity or transport bounds."""


@dataclass(frozen=True)
class SourceManifest:
    version: int
    repository: str
    commit: str
    bundle_sha256: str
    bundle_url: str = field(repr=False)
    basis_commits: tuple[str, ...]
    argv: tuple[str, ...]
    features: tuple[str, ...]
    environment: tuple[tuple[str, str], ...]
    toolchain: str

    def encode(self) -> bytes:
        """Encode the immutable request within the control protocol bound."""
        content = json.dumps(asdict(self), sort_keys=True, separators=(',', ':')).encode()
        if len(content) > MANIFEST_MAX_BYTES:
            raise SourceRefused('ManifestTooLarge: source manifest exceeds the control limit')
        return content


@dataclass(frozen=True)
class SourceLimits:
    max_bytes: int
    download_timeout_s: int
    git_timeout_s: int
    allowed_environment: tuple[str, ...]


def read_limits(configuration: conf.ConfigRead) -> SourceLimits:
    return SourceLimits(configuration.whole('source_max_bytes'), configuration.whole('source_download_timeout_s'),
                        configuration.whole('source_git_timeout_s'), configuration.words('source_allowed_environment'))


def validate_manifest(manifest: SourceManifest, limits: SourceLimits) -> None:
    """Reject unsafe paths, revisions, URLs, and inherited environment values."""
    _ = manifest.encode()
    if (manifest.version != 1 or not re.fullmatch(r'[a-zA-Z0-9_][a-zA-Z0-9_.-]*', manifest.repository)
            or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', manifest.commit)
            or any(not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', value) for value in manifest.basis_commits)
            or not re.fullmatch(r'[0-9a-f]{64}', manifest.bundle_sha256)
            or not re.fullmatch(r'[0-9a-f]{64}', manifest.toolchain)
            or not manifest.argv or any('\0' in word for word in (*manifest.argv, *manifest.features))):
        raise SourceRefused('InvalidManifest: source identity or command is invalid')
    names = [name for name, _ in manifest.environment]
    if (len(set(names)) != len(names) or any(name not in limits.allowed_environment
            or name in ('HOME', 'PATH', 'CARGO_HOME', 'RUSTUP_HOME', 'CARGO_TARGET_DIR')
            or any(word in name.upper() for word in ('AWS', 'TOKEN', 'SECRET', 'PASSWORD', 'CREDENTIAL'))
            or '\0' in value for name, value in manifest.environment)):
        raise SourceRefused('EnvironmentRefused: caller environment is not allowed')
    parts = urlsplit(manifest.bundle_url)
    if (parts.scheme != 'https' or not parts.hostname or not parts.hostname.endswith('.amazonaws.com')
            or '.s3.' not in parts.hostname or parts.username or parts.password or parts.fragment):
        raise SourceRefused('InvalidSourceURL: source needs a signed S3 GET URL')


def decode_manifest(content: bytes, limits: SourceLimits) -> SourceManifest:
    """Check JSON types before admitting a source request."""
    if len(content) > MANIFEST_MAX_BYTES:
        raise SourceRefused('ManifestTooLarge: source manifest exceeds the control limit')
    raw = cast(object, json.loads(content))
    if not isinstance(raw, dict):
        raise SourceRefused('InvalidManifest: source manifest must be an object')
    row = cast(dict[str, object], raw)
    def text(key: str) -> str:
        value = row.get(key)
        if not isinstance(value, str):
            raise SourceRefused('InvalidManifest: missing string ' + key)
        return value
    def words(key: str) -> tuple[str, ...]:
        values = row.get(key)
        if not isinstance(values, list) or any(not isinstance(value, str) for value in cast(list[object], values)):
            raise SourceRefused('InvalidManifest: missing string list ' + key)
        return tuple(cast(list[str], values))
    environment = row.get('environment')
    if not isinstance(environment, list):
        raise SourceRefused('InvalidManifest: missing environment list')
    pairs: list[tuple[str, str]] = []
    for value in cast(list[object], environment):
        if not isinstance(value, list):
            raise SourceRefused('InvalidManifest: environment entry must be a pair')
        pair = cast(list[object], value)
        if len(pair) != 2 or not all(isinstance(item, str) for item in pair):
            raise SourceRefused('InvalidManifest: environment entry must contain two strings')
        pairs.append((cast(str, pair[0]), cast(str, pair[1])))
    if type(row.get('version')) is not int:
        raise SourceRefused('InvalidManifest: version must be an integer')
    manifest = SourceManifest(cast(int, row['version']), text('repository'), text('commit'), text('bundle_sha256'),
        text('bundle_url'), words('basis_commits'), words('argv'), words('features'), tuple(pairs), text('toolchain'))
    validate_manifest(manifest, limits)
    return manifest


@dataclass(frozen=True)
class MissingBasis:
    repository: str
    commits: tuple[str, ...]


@dataclass(frozen=True)
class SourceIdentity:
    repository: str
    commit: str
    toolchain: str


@dataclass(frozen=True)
class LeasedSource:
    slot: build_slots.Slot
    identity: SourceIdentity

    @property
    def path(self) -> Path:
        return self.slot.path


def git(repository: Path, limits: SourceLimits, *arguments: str, accepted: tuple[int, ...] = (0,)) -> str:
    try:
        leases = (build_folders.build_folder_lock_fd(repository),)
    except StopIteration:
        leases = ()
    result = subprocess.run(['git', '-C', str(repository), *arguments], capture_output=True,
                            text=True, timeout=limits.git_timeout_s, pass_fds=leases)
    if result.returncode not in accepted:
        raise SourceRefused('GitSourceRefused: ' + ' '.join(arguments[:2]))
    return result.stdout.strip()


def toolchain_identity(path: Path) -> str:
    source = path / 'rust-toolchain.toml'
    if source.is_symlink() or not source.is_file():
        raise SourceRefused('ToolchainMissing: rust-toolchain.toml must be a regular file')
    return hashlib.sha256(source.read_bytes()).hexdigest()


def download(url: str, destination: Path, limits: SourceLimits) -> None:
    """Download a signed object without credentials or an unbounded read."""
    try:
        _ = subprocess.run(['curl', '--fail', '--silent', '--max-time', str(limits.download_timeout_s),
                            '--max-filesize', str(limits.max_bytes), '--output', str(destination), url],
                           check=True, capture_output=True, timeout=limits.download_timeout_s)
        if destination.stat().st_size > limits.max_bytes:
            raise SourceRefused('SourceDownloadBound: source download exceeds its size bound')
    except (OSError, subprocess.SubprocessError):
        raise SourceRefused('SourceDownloadBound: source download failed within its size or time bound') from None


def bundle_prerequisites(bundle: Path) -> tuple[str, ...]:
    """Read bounded prerequisite identities before Git verifies the complete bundle."""
    prerequisites: list[str] = []
    consumed = 0
    with bundle.open('rb') as stream:
        while True:
            line = stream.readline(MANIFEST_MAX_BYTES + 1)
            consumed += len(line)
            if consumed > MANIFEST_MAX_BYTES:
                raise SourceRefused('BundleHeaderBound: source bundle header exceeds the protocol limit')
            if not line or line == b'\n':
                return tuple(prerequisites)
            if line.startswith(b'-'):
                commit = line[1:].split(b' ', 1)[0].decode('ascii', errors='replace')
                if not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', commit):
                    raise SourceRefused('InvalidBundleHeader: prerequisite is invalid')
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
def materialize(manifest: SourceManifest, limits: SourceLimits, *, showrunner: str, unit: str, work: str,
                root: Path = SOURCE_ROOT,
                fetch: Callable[[str, Path, SourceLimits], None] = download) -> Generator[LeasedSource | MissingBasis]:
    """Verify a bundle and retain the slot lease through the caller's command."""
    validate_manifest(manifest, limits)
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
            fetch(manifest.bundle_url, bundle, limits)
            if bundle.stat().st_size > limits.max_bytes:
                raise SourceRefused('SourceDownloadBound: bundle exceeds its size bound')
            with bundle.open('rb') as stream:
                digest = hashlib.file_digest(stream, 'sha256').hexdigest()
            if digest != manifest.bundle_sha256:
                raise SourceRefused('BundleDigestMismatch: source bundle sha256 differs')
            prerequisites = tuple(dict.fromkeys((*manifest.basis_commits, *bundle_prerequisites(bundle))))
            missing = tuple(commit for commit in prerequisites
                            if not git(mirror, limits, 'rev-parse', '--verify', '--quiet', commit + '^{commit}', accepted=(0, 1)))
            if missing:
                yield MissingBasis(manifest.repository, missing)
                return
            _ = git(mirror, limits, 'bundle', 'verify', str(bundle))
            heads = git(mirror, limits, 'bundle', 'list-heads', str(bundle)).splitlines()
            if heads != [manifest.commit + ' refs/heads/hana-source']:
                raise SourceRefused('BundleCommitMismatch: bundle head differs from the manifest')
            _ = git(mirror, limits, '-c', 'fetch.fsckObjects=true', 'fetch', '--quiet', '--no-tags', str(bundle),
                    '+refs/heads/hana-source:refs/heads/hana-source')
            actual = git(mirror, limits, 'rev-parse', 'refs/heads/hana-source^{commit}')
            if actual != manifest.commit:
                raise SourceRefused('SourceCommitMismatch: fetched commit differs from the manifest')
        fcntl.flock(lock, fcntl.LOCK_UN)
        with build_slots.take_slot(mirror, manifest.commit, showrunner=showrunner, unit=unit, work=work,
                                  purpose='hana-source', wait_policy=build_slots.SlotWaitPolicy.REFUSE) as slot:
            if toolchain_identity(slot.path) != manifest.toolchain:
                raise SourceRefused('ToolchainMismatch: slot toolchain differs from the manifest')
            yield LeasedSource(slot, SourceIdentity(manifest.repository, manifest.commit, manifest.toolchain))
