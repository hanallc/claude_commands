"""Publish and pin the private source for a cloud gate."""
from __future__ import annotations

from base64 import b64encode
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from hashlib import file_digest
import json
from pathlib import Path
import re
import subprocess
import tempfile
import time
from typing import cast
from uuid import uuid4

from scripts.aws_target import ledger
from scripts.aws_target.aws_cli import AwsCli, AwsUnavailable, aws_error_code
from scripts.lib.atomic_file import write_text_atomic
from scripts.lib.file_lock import exclusive


DAY_SECONDS = 24 * 60 * 60
BASE_REFRESH_DAYS = 7
BASE_SELECTION_DAYS = 2 * BASE_REFRESH_DAYS
BASE_RETENTION_DAYS = BASE_SELECTION_DAYS + 1
DELTA_RETENTION_DAYS = 1
CURRENT_BASE_KEY = 'base/current.json'
BASE_BUNDLE_PREFIX = 'base/g'
DELTA_PREFIX = 'delta/'
SOURCE_ARCHIVE = 'private-source.bundle'
NIX_BINARY_CACHE = 'https://cache.nixos.org'
STORE_PATH_PATTERN = r'/nix/store/[0-9a-z]{32}-[a-zA-Z0-9+._?=-]+'


class RegionalSourceBaseUnavailable(ValueError):
    pass


class SourceBaseMismatch(ValueError):
    pass


class SourceBasePublishFailed(ValueError):
    pass


class SourceDeltaUploadFailed(ValueError):
    pass


class GateToolsUnavailable(ValueError):
    pass


@dataclass(frozen=True)
class SourceBucket:
    region: str
    name: str


@dataclass(frozen=True)
class SourceObject:
    key: str
    sha256: str

    def checksum(self) -> str:
        return b64encode(bytes.fromhex(self.sha256)).decode('ascii')


@dataclass(frozen=True)
class PinnedBase:
    generation: str
    commit: str
    published_at: int
    bundle: SourceObject

    @classmethod
    def decode(cls, row: dict[str, object]) -> PinnedBase:
        generation = ledger.text(row, 'generation')
        commit = ledger.text(row, 'commit')
        at = ledger.instant(row, 'published_at')
        digest = ledger.text(row, 'sha256')
        key = ledger.text(row, 'key')
        if (not re.fullmatch(r'g[0-9]{20}-[0-9a-f]{32}', generation)
                or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', commit)
                or not re.fullmatch(r'[0-9a-f]{64}', digest)
                or key != f'base/{generation}/{digest}.bundle'):
            raise RegionalSourceBaseUnavailable('The source base manifest is invalid')
        return cls(generation, commit, at, SourceObject(key, digest))

    def record(self) -> dict[str, object]:
        return {'generation': self.generation, 'commit': self.commit, 'published_at': self.published_at,
                'key': self.bundle.key, 'sha256': self.bundle.sha256}


@dataclass(frozen=True)
class CurrentBase:
    base: PinnedBase
    etag: str


@dataclass(frozen=True)
class InvalidCurrentBase:
    etag: str


@dataclass(frozen=True)
class NoCurrentBase:
    pass


@dataclass(frozen=True)
class BaseOnly:
    base: PinnedBase


@dataclass(frozen=True)
class BaseAndDelta:
    base: PinnedBase
    delta: SourceObject


@dataclass(frozen=True)
class PublicCloneSource:
    pass


@dataclass(frozen=True)
class PrivateBundleSource:
    tool_paths: tuple[str, ...]


PUBLIC_CLONE_SOURCE = PublicCloneSource()


def tool_paths(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise GateToolsUnavailable('GateToolsUnavailable: the gate tool paths are absent')
    paths = cast(list[object], value)
    if any(not isinstance(path, str) or not re.fullmatch(STORE_PATH_PATTERN, path) for path in paths):
        raise GateToolsUnavailable('GateToolsUnavailable: a gate tool path is invalid')
    return tuple(dict.fromkeys(cast(list[str], paths)))


def deployment_source(deployment: dict[str, object]) -> PrivateBundleSource | PublicCloneSource:
    if 'source_tool_paths' not in deployment:
        return PUBLIC_CLONE_SOURCE
    return PrivateBundleSource(tool_paths(deployment['source_tool_paths']))


def bucket_name(deployment: str, region: str) -> str:
    return f'gate-src-{deployment}-{region}'


def prepare_tools(revision: str, packages: Sequence[str]) -> tuple[str, ...]:
    """Resolve the pinned Linux outputs and check the binary cache."""
    nix = ('nix', '--extra-experimental-features', 'nix-command flakes')
    if not re.fullmatch(r'[0-9a-f]{40}', revision) or not packages:
        raise GateToolsUnavailable('GateToolsUnavailable: no pinned packages')
    if any(not re.fullmatch(r'[a-zA-Z0-9_+-]+', package) for package in packages):
        raise GateToolsUnavailable('GateToolsUnavailable: an invalid package name')
    attrs = ' '.join(json.dumps(package) for package in packages)
    select = f'packages: builtins.map (name: (builtins.getAttr name packages).outPath) [ {attrs} ]'
    try:
        result = subprocess.check_output([*nix, 'eval', '--json', f'github:NixOS/nixpkgs/{revision}#legacyPackages.x86_64-linux',
                                          '--apply', select], timeout=300, text=True)
        paths = tool_paths(cast(object, json.loads(result)))
        _ = subprocess.check_output([*nix, 'path-info', '--recursive', '--store', NIX_BINARY_CACHE, *paths],
                                    timeout=300, text=True)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise GateToolsUnavailable('GateToolsUnavailable: pinned packages are unavailable in the Nix binary cache') from error
    return paths


def source_bucket(row: dict[str, object]) -> SourceBucket:
    return SourceBucket(ledger.text(row, 'region'), ledger.text(row, 'source_bucket'))


def bundle_sha256(path: Path) -> str:
    with path.open('rb') as stream:
        return file_digest(stream, 'sha256').hexdigest()


def git(repo: Path, *arguments: str) -> str:
    result = subprocess.run(['git', '-C', str(repo), *arguments], check=False, capture_output=True, text=True)
    if result.returncode:
        raise SourceBaseMismatch('SourceBaseMismatch: git ' + arguments[0] + ' failed: ' + result.stderr.strip())
    return result.stdout.strip()


def create_bundle(repo: Path, bundle: Path, commits: Sequence[str], *, exclude: str = '') -> None:
    """Write a bundle with unique temporary refs for its tips."""
    refs: list[str] = []
    nonce = uuid4().hex
    try:
        for index, commit in enumerate(dict.fromkeys(commits)):
            ref = f'refs/cloud-gates/{nonce}-{index}'
            _ = git(repo, 'update-ref', ref, commit)
            refs.append(ref)
        _ = git(repo, 'bundle', 'create', str(bundle.resolve()), *refs, *(('^' + exclude,) if exclude else ()))
    finally:
        for ref in refs:
            _ = git(repo, 'update-ref', '-d', ref)


def archive(repo: Path, folder: Path, commits: Sequence[str]) -> None:
    with tempfile.TemporaryDirectory(dir=folder) as scratch:
        path = Path(scratch) / SOURCE_ARCHIVE
        create_bundle(repo, path, commits)
        _ = path.replace(folder / SOURCE_ARCHIVE)


def read_current(api: AwsCli, bucket: str, destination: Path) -> CurrentBase | InvalidCurrentBase | NoCurrentBase:
    try:
        reply = api.call('get-object', '--bucket', bucket, '--key', CURRENT_BASE_KEY, str(destination), service='s3api')
    except AwsUnavailable as error:
        code = aws_error_code(error, allow_status=True)
        if code in ('NoSuchKey', 'NotFound', '404'):
            return NoCurrentBase()
        if code not in ('AccessDenied', '403'):
            raise
        # S3 can return 403 for a missing key when the caller has prefix-only list permission.
        listing = api.call('list-objects-v2', '--bucket', bucket, '--prefix', CURRENT_BASE_KEY, service='s3api')
        if 'Contents' in listing and any(row.get('Key') == CURRENT_BASE_KEY for row in ledger.rows(listing, 'Contents')):
            raise
        return NoCurrentBase()
    etag = ledger.text(reply, 'ETag')
    try:
        base = PinnedBase.decode(ledger.mapping(cast(object, json.loads(destination.read_text()))))
    except (RecursionError, ValueError):
        return InvalidCurrentBase(etag)
    return CurrentBase(base, etag)


def head_bundle(api: AwsCli, bucket: str, bundle: SourceObject) -> dict[str, object]:
    reply = api.call('head-object', '--bucket', bucket, '--key', bundle.key, '--checksum-mode', 'ENABLED', service='s3api')
    if reply.get('ChecksumSHA256') != bundle.checksum():
        raise ValueError('The source bundle checksum differs from the expected checksum')
    return reply


def put_bundle(api: AwsCli, bucket: str, bundle: SourceObject, path: Path) -> dict[str, object]:
    try:
        _ = api.call('put-object', '--bucket', bucket, '--key', bundle.key, '--body', str(path),
                     '--if-none-match', '*', '--checksum-sha256', bundle.checksum(), service='s3api')
    except AwsUnavailable as error:
        if aws_error_code(error, allow_status=True) not in ('PreconditionFailed', '412'):
            raise
    return head_bundle(api, bucket, bundle)


def publish_base(api: AwsCli, bucket: str, base: PinnedBase, bundle: Path, scratch: Path) -> None:
    """Verify the object before a conditional write makes it current."""
    current = read_current(api, bucket, scratch / 'previous.json')
    if isinstance(current, CurrentBase) and current.base.generation >= base.generation:
        return
    _ = put_bundle(api, bucket, base.bundle, bundle)
    manifest = scratch / 'current.json'
    _ = manifest.write_text(json.dumps(base.record()) + '\n')
    condition = ('--if-match', current.etag) if isinstance(current, (CurrentBase, InvalidCurrentBase)) else ('--if-none-match', '*')
    try:
        _ = api.call('put-object', '--bucket', bucket, '--key', CURRENT_BASE_KEY, '--body', str(manifest),
                     *condition, service='s3api')
    except AwsUnavailable as error:
        if aws_error_code(error, allow_status=True) not in ('PreconditionFailed', 'ConditionalRequestConflict', '412', '409'):
            raise
        latest = read_current(api, bucket, scratch / 'latest.json')
        if not isinstance(latest, CurrentBase) or latest.base.generation < base.generation:
            raise


def refresh_bases(api: AwsCli, repo: Path, *, regions: Sequence[dict[str, object]], clock: Callable[[], float] = time.time,
                  force: bool = False) -> tuple[SourceBasePublishFailed, ...]:
    """Capture one main commit and publish it to each region that needs a base."""
    started = clock()
    generation = f'g{int(started * 1_000_000_000):020d}-{uuid4().hex}'
    failures: list[SourceBasePublishFailed] = []
    with tempfile.TemporaryDirectory(prefix='gate-source-') as directory:
        scratch = Path(directory)
        bundle = scratch / 'base.bundle'
        due: list[SourceBucket] = []
        for row in regions:
            try:
                target = source_bucket(row)
                if not force:
                    saved = read_current(api.in_region(target.region), target.name, scratch / 'previous.json')
                    if isinstance(saved, CurrentBase) and 0 <= started - saved.base.published_at < BASE_REFRESH_DAYS * DAY_SECONDS:
                        continue
                due.append(target)
            except (AwsUnavailable, OSError, ValueError, subprocess.SubprocessError):
                failures.append(SourceBasePublishFailed(f'SourceBasePublishFailed in {row.get("region", "unknown")}'))
        if not due:
            return tuple(failures)
        try:
            commit = git(repo, 'rev-parse', '--verify', 'refs/remotes/origin/main^{commit}')
            create_bundle(repo, bundle, (commit,))
            digest = bundle_sha256(bundle)
        except (OSError, ValueError, subprocess.SubprocessError):
            failures.extend(SourceBasePublishFailed(f'SourceBasePublishFailed in {target.region}') for target in due)
            return tuple(failures)
        base = PinnedBase(generation, commit, int(started), SourceObject(f'base/{generation}/{digest}.bundle', digest))
        for target in due:
            try:
                publish_base(api.in_region(target.region), target.name, base, bundle, scratch)
            except (AwsUnavailable, OSError, ValueError, subprocess.SubprocessError):
                failures.append(SourceBasePublishFailed(f'SourceBasePublishFailed in {target.region}'))
    return tuple(failures)


def request_commits(request: dict[str, object]) -> tuple[str, ...]:
    if request.get('task') == 'paired':
        return (*(ledger.text(ledger.mapping(request[key]), 'commit') for key in ('base', 'changed')),
                ledger.text(request, 'origin_main'))
    return tuple(ledger.text(request, key) for key in ('commit', 'origin_main'))


def pin_source(api: AwsCli, target: SourceBucket, folder: Path, request: dict[str, object],
               now: float) -> BaseOnly | BaseAndDelta:
    """Select the exact base and prepare its delta before launch."""
    shared = folder.parent if request.get('task') == 'paired' else folder
    expires = ledger.instant(request, 'expires')
    with tempfile.TemporaryDirectory(dir=folder) as directory:
        scratch = Path(directory)
        try:
            current = read_current(api, target.name, scratch / 'current.json')
            if not isinstance(current, CurrentBase):
                raise RegionalSourceBaseUnavailable('The region has no published base')
            base = current.base
            if (not 0 <= now - base.published_at < BASE_SELECTION_DAYS * DAY_SECONDS
                    or expires >= base.published_at + BASE_RETENTION_DAYS * DAY_SECONDS):
                raise RegionalSourceBaseUnavailable('The region has no base usable through the attempt deadline')
            _ = head_bundle(api, target.name, base.bundle)
        except (AwsUnavailable, OSError, ValueError, subprocess.SubprocessError) as error:
            raise RegionalSourceBaseUnavailable(f'RegionalSourceBaseUnavailable in {target.region}') from error
        commits = request_commits(request)
        if all(commit == base.commit for commit in commits):
            return BaseOnly(base)
        receipt = shared / f'delta-{target.region}-{base.generation}.json'
        with exclusive(receipt.with_suffix('.lock')):
            if receipt.is_file():
                previous = ledger.mapping(cast(object, json.loads(receipt.read_text())))
                if previous.get('base_only') is True:
                    return BaseOnly(base)
                if expires < ledger.instant(previous, 'expires'):
                    return BaseAndDelta(base, SourceObject(ledger.text(previous, 'key'), ledger.text(previous, 'sha256')))
            repo = scratch / 'source'
            repo.mkdir()
            _ = git(repo, 'init', '--bare')
            _ = git(repo, 'fetch', '--no-tags', str((shared / SOURCE_ARCHIVE).resolve()),
                    'refs/cloud-gates/*:refs/cloud-gates/*')
            present = subprocess.run(['git', '-C', str(repo), 'cat-file', '-e', base.commit + '^{commit}'],
                                     capture_output=True, check=False)
            if present.returncode:
                base_bundle = scratch / 'base.bundle'
                try:
                    _ = api.call('get-object', '--bucket', target.name, '--key', base.bundle.key,
                                 str(base_bundle), service='s3api')
                    digest = bundle_sha256(base_bundle)
                except (AwsUnavailable, OSError, ValueError, subprocess.SubprocessError) as error:
                    raise RegionalSourceBaseUnavailable(f'RegionalSourceBaseUnavailable in {target.region}') from error
                if digest != base.bundle.sha256:
                    raise SourceBaseMismatch('SourceBaseMismatch: the downloaded base checksum differs')
                _ = git(repo, 'fetch', '--no-tags', str(base_bundle), base.commit)
            if git(repo, 'rev-list', '--count', *commits, '--not', base.commit) == '0':
                write_text_atomic(receipt, json.dumps({'base_only': True}))
                return BaseOnly(base)
            bundle = scratch / 'delta.bundle'
            create_bundle(repo, bundle, commits, exclude=base.commit)
            digest = bundle_sha256(bundle)
            delta = SourceObject(f'{DELTA_PREFIX}{base.generation}/{digest}.bundle', digest)
            try:
                head = put_bundle(api, target.name, delta, bundle)
                uploaded = datetime.fromisoformat(ledger.text(head, 'LastModified').replace('Z', '+00:00'))
                delta_expires = int(uploaded.timestamp()) + DELTA_RETENTION_DAYS * DAY_SECONDS
                if expires >= delta_expires:
                    raise ValueError('The attempt deadline is after the delta expires')
            except (AwsUnavailable, OSError, ValueError, subprocess.SubprocessError) as error:
                raise SourceDeltaUploadFailed(f'SourceDeltaUploadFailed in {target.region}') from error
            write_text_atomic(receipt, json.dumps({**asdict(delta), 'expires': delta_expires}))
            return BaseAndDelta(base, delta)
