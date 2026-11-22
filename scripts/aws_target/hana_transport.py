"""Capture exact source and publish immutable Git bundles through signed S3 objects."""
from __future__ import annotations

import ast
from collections.abc import Callable, Generator, Mapping
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import fcntl
import fnmatch
from functools import partial
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
from typing import Protocol, cast

from scripts.aws_target import hana_protocol, hana_provider, hana_render, hana_source, s3_presign
from scripts.aws_target.aws_cli import AwsUnavailable
from scripts.copies import copies
from scripts.lib import build_folders, conf
from scripts.lib.atomic_file import write_text_atomic


class SourceObjects(Protocol):
    """Publish immutable bytes and grant temporary read access by digest."""

    def put_if_absent(self, sha256: str, source: Path) -> None: ...
    def signed_get(self, sha256: str) -> str: ...


class AwsSourceObjects:
    def __init__(self, provider: hana_provider.AwsHanaProvider, configuration: conf.ConfigRead,
                 deployment: str, *, clock: Callable[[], float]):
        if not re.fullmatch(r'[A-Za-z0-9_-]+', deployment):
            raise hana_protocol.SourceRefused('InvalidDeployment: source prefix is invalid')
        self.provider: hana_provider.AwsHanaProvider = provider
        self.timeout: int = configuration.whole('aws_timeout_s')
        self.upload_timeout: int = configuration.whole('source_upload_timeout_s')
        self.url_seconds: int = configuration.whole('source_url_seconds')
        self.prefix: str = 'source/' + deployment + '/'
        self.clock: Callable[[], float] = clock

    def key(self, sha256: str) -> str:
        if not re.fullmatch(r'[0-9a-f]{64}', sha256):
            raise hana_protocol.SourceRefused('InvalidDigest: source key is invalid')
        return self.prefix + sha256

    def put_if_absent(self, sha256: str, source: Path) -> None:
        """Use S3's conditional write instead of an existence check and upload race."""
        try:
            _ = self.provider.api.call('put-object', '--bucket', self.provider.bucket, '--key', self.key(sha256),
                '--body', str(source), '--if-none-match', '*', service='s3api', timeout_s=self.upload_timeout)
        except subprocess.TimeoutExpired as error:
            raise AwsUnavailable(f'put-object: the source bundle of {source.stat().st_size} bytes did not upload '
                                 + f'in {self.upload_timeout} s (source_upload_timeout_s)') from error
        except AwsUnavailable as error:
            if not re.search(r'\((?:PreconditionFailed|412)\)', str(error)):
                raise

    def signed_get(self, sha256: str) -> str:
        credentials = self.provider.api.call('export-credentials', '--format', 'process',
                                             service='configure', timeout_s=self.timeout)
        def text(key: str) -> str:
            value = credentials.get(key, '')
            if not isinstance(value, str):
                raise hana_protocol.SourceRefused('SigningUnavailable: source signing credentials are invalid')
            return value
        signing = s3_presign.SigningKey(text('AccessKeyId'), text('SecretAccessKey'), text('SessionToken'))
        return s3_presign.presign('GET', self.provider.bucket + '.s3.' + self.provider.api.region + '.amazonaws.com',
            self.key(sha256), self.provider.api.region, signing, datetime.fromtimestamp(self.clock(), UTC), self.url_seconds)


@dataclass(frozen=True)
class AcknowledgedCommits:
    repository: str
    commits: tuple[str, ...]


def path_matches(path: str, pattern: str) -> bool:
    return (path.startswith(pattern) or ('/' + pattern in '/' + path)) if pattern.endswith('/') else fnmatch.fnmatchcase(path, pattern)


def check_inputs(source: Path, configuration: conf.ConfigRead) -> None:
    """Refuse excluded source without reading ignored file contents."""
    allowed = configuration.words('source_allowed_ignored')
    ignored = copies.git(source, 'ls-files', '--others', '--ignored', '--exclude-standard', '-z')
    for raw in ignored.split(b'\0'):
        path = os.fsdecode(raw)
        if path and not any(path_matches(path, pattern) for pattern in allowed):
            display_path = repr(path)[1:-1]
            raise hana_protocol.SourceRefused('IgnoredInput: source has an ignored path outside source_allowed_ignored: '
                                              + display_path)
    if any(row.startswith(b'160000 ') for row in copies.git(source, 'ls-files', '--stage', '-z').split(b'\0')):
        raise hana_protocol.SourceRefused('SubmoduleInput: source contains an unsupported Git link')
    forbidden = configuration.words('source_forbidden_paths')
    # A tracked target/ folder is source; only an untracked one is build output.
    for build_output, selection in ((False, '--cached'), (True, '--others')):
        for raw in copies.git(source, 'ls-files', selection, '--exclude-standard', '-z').split(b'\0'):
            path = os.fsdecode(raw)
            if raw and (build_output and path_matches(path, 'target/')
                        or any(path_matches(path, pattern) for pattern in forbidden)):
                # The reason goes into UTF-8 records, so a name that is not UTF-8 shows as escapes.
                raise hana_protocol.SourceRefused('CredentialOrBuildInput: source contains the forbidden path '
                                                + raw.decode('utf-8', 'backslashreplace'))


@dataclass(frozen=True)
class NoRuntimeCapture:
    """This sender has no runtime for a source repair."""


@dataclass(frozen=True)
class CapturedRuntime:
    """The original archive identifies the runtime for a source repair."""

    bundle_sha256: str
    files: tuple[hana_render.RenderedGuestFile, ...]
    abi: int


def _captured_runtime_abi(files: tuple[hana_render.RenderedGuestFile, ...]) -> int:
    """Read the ABI from the protocol bytes that the sender will publish."""
    for file in files:
        if file.path != Path('scripts/aws_target/hana_protocol.py'):
            continue
        definitions = [statement.value for statement in ast.parse(file.content).body
                       if isinstance(statement, ast.Assign) and any(
                           isinstance(target, ast.Name) and target.id == 'RUNTIME_ABI'
                           for target in statement.targets)]
        if len(definitions) == 1:
            definition = definitions[0]
            if isinstance(definition, ast.Constant):
                abi = cast(object, definition.value)
                if type(abi) is int and abi > 0:
                    return abi
        break
    raise hana_protocol.SourceRefused('The captured Hana runtime has no valid ABI')


class SourceSender:
    def __init__(self, objects: SourceObjects, directory: Path, configuration: conf.ConfigRead, *,
                 checkout: Path = hana_render.CHECKOUT):
        self.objects: SourceObjects = objects
        self.directory: Path = directory
        self.configuration: conf.ConfigRead = configuration
        self.limits: hana_protocol.SourceLimits = hana_source.read_limits(configuration)
        self.max_bytes: int = configuration.whole('source_max_bytes')
        self.checkout: Path = checkout
        self.runtime: CapturedRuntime | NoRuntimeCapture = NoRuntimeCapture()

    def record_path(self, repository: str) -> Path:
        if not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]*', repository):
            raise hana_protocol.SourceRefused('InvalidRepository: source repository key is invalid')
        return self.directory / (repository + '.json')

    def acknowledged(self, repository: str) -> AcknowledgedCommits:
        path = self.record_path(repository)
        if not path.exists():
            return AcknowledgedCommits(repository, ())
        raw = cast(object, json.loads(path.read_bytes()))
        if not isinstance(raw, dict):
            raise hana_protocol.SourceRefused('InvalidBasisRecord: acknowledged source record is invalid')
        row = cast(dict[str, object], raw)
        commits = row.get('commits')
        if row.get('repository') != repository or not isinstance(commits, list):
            raise hana_protocol.SourceRefused('InvalidBasisRecord: acknowledged source record differs')
        checked: list[str] = []
        for value in cast(list[object], commits):
            if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', value):
                raise hana_protocol.SourceRefused('InvalidBasisRecord: acknowledged commit is invalid')
            checked.append(value)
        return AcknowledgedCommits(repository, tuple(checked))

    def save(self, record: AcknowledgedCommits) -> None:
        write_text_atomic(self.record_path(record.repository), json.dumps(asdict(record), sort_keys=True) + '\n', new_file_mode=0o600)

    def acknowledge(self, identity: hana_protocol.SourceIdentity) -> None:
        """Retain only commits that the guest reports after source admission."""
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.record_path(identity.repository).with_suffix('.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            previous = self.acknowledged(identity.repository)
            maximum = self.configuration.whole('source_basis_max_commits')
            commits = tuple(dict.fromkeys((identity.commit, *previous.commits)))[:maximum]
            self.save(AcknowledgedCommits(identity.repository, commits))

    def forget_basis(self, answer: hana_protocol.MissingBasis) -> None:
        """Forget the repository's basis so the next bundle is complete."""
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.record_path(answer.repository).with_suffix('.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.save(AcknowledgedCommits(answer.repository, ()))

    def prepare(self, source: Path, argv: tuple[str, ...], *, environment: Mapping[str, str],
                staging: ExitStack, features: tuple[str, ...] = ()) -> tuple[hana_protocol.SourceManifest, Callable[[], None]]:
        """Stage the exact source archive until the sender validates the command."""
        check_inputs(source, self.configuration)
        try:
            commit = copies.capture_source(source)
        except copies.NestedRepository as error:
            raise hana_protocol.SourceRefused(os.fsencode(str(error)).decode('utf-8', 'backslashreplace')) from error
        check_inputs(source, self.configuration)
        # Use the common Git directory so linked worktrees share acknowledgement history.
        common = copies.git(source, 'rev-parse', '--path-format=absolute', '--git-common-dir').decode().strip()
        repository = copies.repository_name(source) + '-' + hashlib.sha256(os.fsencode(common)).hexdigest()
        basis = tuple(value for value in self.acknowledged(repository).commits
                      if value != commit and hana_source.git(source, self.limits, 'cat-file', '-t', value,
                                                             accepted=(0, 128)) == 'commit')
        files = hana_render.runtime_files(self.checkout, self.configuration)
        abi = _captured_runtime_abi(files)
        manifest, upload = staging.enter_context(
            self._stage_archive(source, repository, commit, argv, features, environment, basis, files, abi))
        self.runtime = CapturedRuntime(manifest.bundle_sha256, files, abi)
        return manifest, upload

    def retry_complete(self, source: Path, manifest: hana_protocol.SourceManifest,
                       answer: hana_protocol.MissingBasis, *, staging: ExitStack
                       ) -> tuple[hana_protocol.SourceManifest, Callable[[], None]]:
        """Retry the captured commit without reading changed caller working files."""
        hana_protocol.validate_manifest(manifest, self.limits)
        common = copies.git(source, 'rev-parse', '--path-format=absolute', '--git-common-dir').decode().strip()
        repository = copies.repository_name(source) + '-' + hashlib.sha256(os.fsencode(common)).hexdigest()
        if repository != manifest.repository or answer.repository != repository:
            raise hana_protocol.SourceRefused('RetryRepositoryMismatch: source retry belongs to another repository')
        runtime = self.runtime
        if not isinstance(runtime, CapturedRuntime) or runtime.bundle_sha256 != manifest.bundle_sha256:
            raise hana_protocol.SourceRefused('The original Hana runtime is unavailable for source repair')
        self.forget_basis(answer)
        return staging.enter_context(self._stage_archive(source, repository, manifest.commit, manifest.argv,
            manifest.features, dict(manifest.environment), (), runtime.files, runtime.abi))

    @contextmanager
    def _stage_archive(self, source: Path, repository: str, commit: str, argv: tuple[str, ...], features: tuple[str, ...],
                       environment: Mapping[str, str], basis: tuple[str, ...],
                       runtime: tuple[hana_render.RenderedGuestFile, ...], abi: int
                       ) -> Generator[tuple[hana_protocol.SourceManifest, Callable[[], None]]]:
        common = copies.git(source, 'rev-parse', '--path-format=absolute', '--git-common-dir').decode().strip()
        with build_folders.tool_folder('hana-source-bundle', parent=Path(common)) as temporary:
            # This bare metadata repository isolates bundle refs from the caller's refs.
            _ = hana_source.git(temporary, self.limits, 'init', '--bare', '--quiet',
                                '--object-format=' + ('sha256' if len(commit) == 64 else 'sha1'))
            objects = Path(common) / 'objects'
            alternate = temporary / 'objects/info/alternates'
            _ = alternate.write_text(str(objects) + '\n')
            _ = hana_source.git(temporary, self.limits, 'update-ref', 'refs/heads/hana-source', commit)
            bundle = temporary / 'source.bundle'
            if basis:
                # Root snapshots need explicit prerequisites because they have no shared commit ancestors.
                header = ('# v3 git bundle\n@object-format=sha256\n' if len(commit) == 64 else '# v2 git bundle\n')
                header += ''.join('-' + value + ' source snapshot\n' for value in basis)
                header += commit + ' refs/heads/hana-source\n\n'
                captured_objects = set(hana_source.git(temporary, self.limits, 'rev-list', '--objects',
                                                      '--no-object-names', commit).splitlines())
                known_objects = set(hana_source.git(temporary, self.limits, 'rev-list', '--objects',
                                                   '--no-object-names', *basis).splitlines())
                new_objects = '\n'.join(sorted(captured_objects - known_objects)) + '\n'
                with bundle.open('wb') as output:
                    _ = output.write(header.encode())
                    output.flush()
                    _ = subprocess.run(['git', '-C', str(temporary), 'pack-objects', '--stdout'],
                        input=new_objects.encode(), stdout=output, stderr=subprocess.PIPE, timeout=self.limits.git_timeout_s,
                        pass_fds=(build_folders.build_folder_lock_fd(temporary),), check=True)
            else:
                _ = hana_source.git(temporary, self.limits, 'bundle', 'create', str(bundle), 'refs/heads/hana-source')
            payload = temporary / 'payload.tar'
            marker = hana_render.RenderedGuestFile(Path('abi'), 0o644, f'{abi}\n'.encode())
            with tarfile.open(payload, 'w:', format=tarfile.USTAR_FORMAT) as archive, bundle.open('rb') as source_bundle:
                entry = tarfile.TarInfo('source.bundle')
                entry.mode = 0o644
                entry.size = bundle.stat().st_size
                archive.addfile(entry, source_bundle)
                for file in (*runtime, marker):
                    entry = tarfile.TarInfo((Path('runtime') / file.path).as_posix())
                    entry.mode = file.mode
                    entry.size = len(file.content)
                    archive.addfile(entry, io.BytesIO(file.content))
            if payload.stat().st_size > self.max_bytes:
                raise hana_protocol.SourceRefused('SourceUploadBound: archive exceeds its size bound')
            with payload.open('rb') as stream:
                sha256 = hashlib.file_digest(stream, 'sha256').hexdigest()
            toolchain = copies.git(source, 'show', commit + ':rust-toolchain.toml')
            manifest = hana_protocol.SourceManifest(1, repository, commit, sha256, self.objects.signed_get(sha256),
                basis, argv, features, tuple(sorted(environment.items())), hashlib.sha256(toolchain).hexdigest(),
                runtime_abi=abi)
            hana_protocol.validate_manifest(manifest, self.limits)
            yield manifest, partial(self.objects.put_if_absent, sha256, payload)
