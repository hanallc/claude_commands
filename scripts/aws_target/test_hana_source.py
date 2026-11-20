"""Exercise exact source bundles with private Git repositories and guest slots."""
from __future__ import annotations

from collections.abc import Generator
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from functools import cached_property
import hashlib
import io
import json
import os
import shutil
from pathlib import Path
import subprocess
import tarfile
import tempfile
from typing import cast, override
import unittest
from unittest import mock

from scripts.aws_target import hana_payload, hana_protocol, hana_receiver, hana_render
from scripts.aws_target import hana_source as source, hana_transport as transport
from scripts.aws_target.ledger_test_support import install_aws_fixture_settings, override_fixture_settings
from scripts.copies import copies
from scripts.lib import build_folders, build_slots, conf
from scripts.lib.build_slots_test_support import private_slot_config
from scripts.lib.git_test_support import commit_repository


class MemorySourceObjects:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.uploads: list[str] = []

    def put_if_absent(self, sha256: str, source: Path) -> None:
        content = source.read_bytes()
        assert hashlib.sha256(content).hexdigest() == sha256
        if sha256 not in self.objects:
            self.objects[sha256] = content
            self.uploads.append(sha256)

    def signed_get(self, sha256: str) -> str:
        return 'https://fixture-bucket.s3.us-east-2.amazonaws.com/source/' + sha256

class HanaSourceTests(unittest.TestCase):
    def __init__(self, methodName: str = 'runTest') -> None:
        super().__init__(methodName)
        self.root: Path = Path()
        self.pool_settings: Path = Path()
        self.checkout: Path = Path()
        self.runtime_checkout: Path = Path()
        self.base: str = ''
        self.objects: MemorySourceObjects = MemorySourceObjects()

    @property
    def configuration(self) -> conf.ConfigRead:
        return conf.read(Path(os.environ['CONF_DIR']) / 'hana_aws.conf')

    @property
    def limits(self) -> hana_protocol.SourceLimits:
        return source.read_limits(self.configuration)

    @cached_property
    def sender(self) -> transport.SourceSender:
        return transport.SourceSender(self.objects, self.root / 'acknowledged', self.configuration,
                                      checkout=self.runtime_checkout)

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        config_dir = self.root / 'config'
        install_aws_fixture_settings(config_dir)
        _ = (config_dir / 'copies.conf').write_text(
            'wait_seconds=1\ngit_timeout_seconds=60\nguard_timeout_seconds=10\n')
        _ = (config_dir / 'build_targets.conf').write_text(
            '# range: 0.001..\nloopback.shell_timeout_s=120\n')
        private = private_slot_config(self.root, repository='hana')
        self.enterContext(mock.patch.dict(os.environ, private.environment))

        self.pool_settings = private.settings
        configuration = config_dir / 'hana_aws.conf'
        _ = configuration.write_text("""\
host=aws-hana
enabled=on
# whole: 1..60
guard_grace_seconds=60
# whole: 1..60
watchdog_check_seconds=1
# whole: 1..1800
permit_max_seconds=1800
# whole: 1..8
step_cpu_limit=4
# whole: 1..24
step_memory_gib=24
# whole: 1..60
mount_timeout_seconds=15
execution_shell=ci
# whole: 1..
free_floor_gib=30
source_allowed_ignored=target/ .direnv/ __pycache__/ **/__pycache__/ .claude/settings.local.json .unit.toml
source_forbidden_paths=.claude/settings.local.json .direnv/ .aws/ .env .env.* **/.env **/.env.* *.pem **/*.pem *.key **/*.key
source_allowed_environment=RUSTFLAGS RUSTDOCFLAGS CARGO_INCREMENTAL RUST_BACKTRACE
# whole: 1..
source_max_bytes=1073741824
# whole: 1..
source_download_timeout_s=120
# whole: 1..
source_git_timeout_s=60
# whole: 1..
source_basis_max_commits=8
""")
        self.runtime_checkout = self.root / 'commands'
        (self.runtime_checkout / 'config').mkdir(parents=True)
        for name in ('build_targets.conf', 'build_folders.conf', 'copies.conf'):
            _ = shutil.copyfile(self.configuration.source.with_name(name), self.runtime_checkout / 'config' / name)
        manifest = self.runtime_checkout / hana_render.RUNTIME_FILE_LIST
        manifest.parent.mkdir(parents=True)
        _ = manifest.write_text('scripts/aws_target/fixture_runtime.py\nscripts/aws_target/hana_protocol.py\n'
            + f'{hana_render.RUNNER_ENTRY}\n')
        _ = shutil.copyfile(Path(__file__).with_name(hana_render.RUNNER_ENTRY.name),
                            self.runtime_checkout / hana_render.RUNNER_ENTRY)
        _ = (manifest.parent / 'hana_protocol.py').write_text(f'RUNTIME_ABI = {hana_protocol.RUNTIME_ABI}\n')
        _ = (manifest.parent / 'fixture_runtime.py').write_text('version = "captured"\n')
        override_fixture_settings(private.settings,
            f'roots.slots.aws-hana={self.root / "guest-slots"}\n'
            + '# whole: 1..\nslots.aws-hana.hana.max=1\n# whole: 1..\nslots.aws-hana.hana.max_bytes=1073741824\n')
        self.checkout = self.enterContext(build_folders.leased_build_folder(build_folders.BuildFolderSpec(
            kind='mirror', parent=self.root / 'sources', suffix_length=0, repository='hana', owner='fixture', slot=0)))
        _ = self.git('init', '--quiet', '-b', 'main')
        _ = self.git('remote', 'add', 'origin', 'https://example.invalid/fixture/hana.git')
        _ = (self.checkout / '.gitignore').write_text('target/\n.direnv/\n__pycache__/\n.claude/settings.local.json\nprivate.bin\n')
        _ = (self.checkout / 'rust-toolchain.toml').write_text('[toolchain]\nchannel = "stable"\n')
        _ = (self.checkout / 'tracked').write_text('committed\n')
        _ = (self.checkout / 'deleted').write_text('remove me\n')
        _ = (self.checkout / 'large').write_bytes(bytes(range(256)) * 2048)
        self.base = commit_repository(self.checkout, 'base')
        self.objects = MemorySourceObjects()

    def git(self, *arguments: str) -> str:
        return subprocess.run(['git', '-C', str(self.checkout), *arguments], check=True, capture_output=True,
                              text=True, timeout=self.limits.git_timeout_s).stdout.strip()

    def prepare(self, checkout: Path | None = None) -> hana_protocol.SourceManifest:
        with ExitStack() as staging:
            manifest, upload = self.sender.prepare(checkout or self.checkout, ('cargo', 'check'),
                                                    environment={'RUST_BACKTRACE': '1'}, staging=staging)
            upload()
            return manifest

    @contextmanager
    def verified_bundle(self, manifest: hana_protocol.SourceManifest) -> Generator[source.VerifiedGitBundle]:
        with (tempfile.TemporaryDirectory(dir=self.root) as directory,
              tempfile.TemporaryFile(dir=self.root) as archive):
            _ = archive.write(self.objects.objects[manifest.bundle_sha256])
            section = hana_payload.unpack_payload(archive, manifest, Path(directory) / 'runtime')
            yield source.VerifiedGitBundle(archive.fileno(), section.offset, section.size)

    @contextmanager
    def materialize(self, manifest: hana_protocol.SourceManifest) -> Generator[source.LeasedSource | source.MissingBasis]:
        with (self.verified_bundle(manifest) as bundle,
              source.materialize(manifest, self.limits, bundle, root=self.root / 'guest',
                  showrunner='builds', unit='aws', work='source-test') as leased):
            yield leased

    def member(self, manifest: hana_protocol.SourceManifest, name: str) -> bytes:
        with tarfile.open(fileobj=io.BytesIO(self.objects.objects[manifest.bundle_sha256]), mode='r:') as archive:
            member = archive.extractfile(name)
            assert member is not None
            with member:
                return member.read()

    def guest(self):
        return mock.patch('socket.gethostname', return_value='aws-hana')

    def test_two_dirty_worktrees_preserve_exact_files_modes_links_index_and_refs(self) -> None:
        with build_slots.take_slot(self.checkout, self.base, showrunner='builds', unit='aws', work='dirty',
                                   purpose='test', wait_policy=build_slots.SlotWaitPolicy.REFUSE) as other:
            for number, checkout in enumerate((self.checkout, other.path)):
                with self.subTest(worktree=number):
                    _ = (checkout / 'tracked').write_text('staged\n')
                    _ = copies.git(checkout, 'add', 'tracked')
                    _ = (checkout / 'tracked').write_text(f'unstaged {number}\n')
                    (checkout / 'deleted').unlink()
                    _ = (checkout / 'new').write_text(f'new {number}\n')
                    (checkout / 'new').chmod(0o755)
                    (checkout / 'link').symlink_to('new')
                    (checkout / 'target').mkdir(exist_ok=True)
                    _ = (checkout / 'target/secret').write_text('never send')
                    index = Path(copies.git(checkout, 'rev-parse', '--path-format=absolute', '--git-path', 'index').decode().strip())
                    index_before = index.read_bytes()
                    refs_before = copies.git(checkout, 'show-ref')
                    manifest = self.prepare(checkout)
                    self.assertEqual(index.read_bytes(), index_before)
                    self.assertEqual(copies.git(checkout, 'show-ref'), refs_before)
                    with self.guest(), self.materialize(manifest) as leased:
                        assert isinstance(leased, source.LeasedSource)
                        self.assertEqual((leased.path / 'tracked').read_text(), f'unstaged {number}\n')
                        self.assertFalse((leased.path / 'deleted').exists())
                        self.assertEqual((leased.path / 'new').read_text(), f'new {number}\n')
                        self.assertTrue((leased.path / 'new').stat().st_mode & 0o111)
                        self.assertEqual(os.readlink(leased.path / 'link'), 'new')
                        self.assertFalse((leased.path / 'target/secret').exists())
                        self.assertEqual(leased.identity.commit, manifest.commit)
                        self.assertEqual(build_folders.build_folder_state(leased.path), 'held')
                        actual = source.git(leased.path, self.limits, 'rev-parse', 'HEAD')
                        self.assertEqual(actual, manifest.commit)
                        self.sender.acknowledge(leased.identity)

    def test_acknowledged_basis_sends_only_new_objects_and_retains_a_small_record(self) -> None:
        override_fixture_settings(self.configuration.source, 'source_basis_max_commits=1\n')
        first = self.prepare()
        with self.guest(), self.materialize(first) as leased:
            assert isinstance(leased, source.LeasedSource)
            self.sender.acknowledge(leased.identity)
        _ = (self.checkout / 'new').write_text('second request\n')
        second = self.prepare()
        self.assertEqual(second.basis_commits, (first.commit,))
        complete = self.member(first, 'source.bundle')
        delta = self.member(second, 'source.bundle')
        self.assertLess(len(delta), len(complete))
        with self.guest(), self.materialize(second) as leased:
            assert isinstance(leased, source.LeasedSource)
            self.assertEqual((leased.path / 'new').read_text(), 'second request\n')
            self.sender.acknowledge(leased.identity)
        self.assertEqual(self.sender.acknowledged(first.repository).commits[0], second.commit)
        self.assertLessEqual(len(self.sender.acknowledged(first.repository).commits),
                             self.configuration.whole('source_basis_max_commits'))

    def test_lost_basis_returns_a_typed_answer_then_a_complete_bundle_succeeds(self) -> None:
        first = self.prepare()
        self.sender.acknowledge(hana_protocol.SourceIdentity(first.repository, first.commit, first.toolchain))
        _ = (self.checkout / 'new').write_text('later source\n')
        delta = self.prepare()
        with self.guest(), self.materialize(delta) as answer:
            self.assertIsInstance(answer, source.MissingBasis)
            assert isinstance(answer, source.MissingBasis)
            self.assertEqual(answer.repository, delta.repository)
            self.assertIn(first.commit, answer.commits)
            self.assertNotIn(self.base, answer.commits)
            _ = (self.checkout / 'new').write_text('caller changed after the request\n')
            runtime = self.runtime_checkout / 'scripts/aws_target/fixture_runtime.py'
            _ = runtime.write_text('version = "changed"\n')
            _ = runtime.with_name('hana_protocol.py').write_text(f'RUNTIME_ABI = {delta.runtime_abi + 1}\n')
            with ExitStack() as staging:
                complete, upload = self.sender.retry_complete(self.checkout, delta, answer, staging=staging)
                upload()
        self.assertEqual(self.objects.uploads, [first.bundle_sha256, delta.bundle_sha256, complete.bundle_sha256])
        self.assertEqual(self.member(complete, 'runtime/scripts/aws_target/fixture_runtime.py'),
                         b'version = "captured"\n')
        self.assertEqual(complete.runtime_abi, delta.runtime_abi)
        self.assertEqual(self.member(complete, 'runtime/abi'), f'{delta.runtime_abi}\n'.encode())
        self.assertEqual(complete.commit, delta.commit)
        self.assertEqual(complete.basis_commits, ())
        with self.guest(), self.materialize(complete) as leased:
            assert isinstance(leased, source.LeasedSource)
            self.assertEqual((leased.path / 'new').read_text(), 'later source\n')

    def test_wrong_sha_changed_commit_and_failed_bundle_verification_are_refused(self) -> None:
        manifest = self.prepare()
        digest = '0' * 64
        self.objects.objects[digest] = self.objects.objects[manifest.bundle_sha256]
        changed = replace(manifest, bundle_sha256=digest, bundle_url=self.objects.signed_get(digest))
        with self.guest(), self.assertRaisesRegex(ValueError, 'BundleDigestMismatch'), self.materialize(changed):
            self.fail('invalid source was admitted')
        changed = replace(manifest, commit=self.base)
        with self.guest(), self.assertRaisesRegex(hana_protocol.SourceRefused, 'BundleCommitMismatch'), self.materialize(changed):
            self.fail('invalid source was admitted')
        invalid_archive = io.BytesIO()
        with (tarfile.open(fileobj=io.BytesIO(self.objects.objects[manifest.bundle_sha256]), mode='r:') as original,
              tarfile.open(fileobj=invalid_archive, mode='w:') as invalid):
            for member in original:
                if member.name == 'source.bundle':
                    content = b'not a Git bundle\n'
                    member.size = len(content)
                    invalid.addfile(member, io.BytesIO(content))
                else:
                    body = original.extractfile(member)
                    assert body is not None
                    with body:
                        invalid.addfile(member, body)
        digest = hashlib.sha256(invalid_archive.getvalue()).hexdigest()
        self.objects.objects[digest] = invalid_archive.getvalue()
        invalid = replace(manifest, bundle_sha256=digest, bundle_url=self.objects.signed_get(digest))
        with (self.guest(), self.assertRaisesRegex(hana_protocol.SourceRefused, 'GitSourceRefused') as refused,
              self.materialize(invalid)):
            self.fail('invalid bundle was admitted')
        self.assertIsInstance(refused.exception.__cause__, subprocess.CalledProcessError)

    def test_full_pool_uses_existing_admission_and_never_creates_an_overflow_slot(self) -> None:
        manifest = self.prepare()
        with self.guest(), self.materialize(manifest) as leased:
            assert isinstance(leased, source.LeasedSource)
            with self.assertRaises(build_slots.SlotsFull), self.materialize(manifest):
                self.fail('full pool admitted source')
            self.assertFalse((self.root / 'guest-slots/hana-slot2').exists())

    def test_missing_guest_slot_keys_stop_admission(self) -> None:
        manifest = self.prepare()
        original = self.pool_settings.read_text()
        for key in ('roots.slots.aws-hana', 'slots.aws-hana.hana.max', 'slots.aws-hana.hana.max_bytes'):
            with self.subTest(key=key):
                _ = self.pool_settings.write_text('\n'.join(line for line in original.splitlines()
                                                            if not line.startswith(key + '=')) + '\n')
                expected = build_slots.SlotsNotConfigured if key.startswith('roots') else conf.Unusable
                with self.guest(), self.assertRaises(expected), self.materialize(manifest):
                    self.fail('missing guest keys admitted source')
                self.assertFalse((self.root / 'guest-slots').exists())
        _ = self.pool_settings.write_text(original)

    def test_ignored_input_outside_the_allowed_list_refuses_before_upload(self) -> None:
        _ = (self.checkout / 'private.bin').write_text('ignored build input')
        with self.assertRaisesRegex(hana_protocol.SourceRefused, 'IgnoredInput'):
            _ = self.prepare()
        self.assertEqual(self.objects.uploads, [])

    def test_ignored_inputs_use_the_allowed_list_and_name_the_refused_path(self) -> None:
        settings = self.root / 'source-inputs.conf'
        _ = (self.checkout / '.gitignore').write_text(
            '/private.bin\n/.clippy.toml\n/.env\n/crates/x/.env\n/generated.rs\n/.cache/\n/.link\n/odd/\n')
        cases = (
            ('private.bin', 'private.bin', ''),
            ('.clippy.toml', '.clippy.toml', ''),
            ('.env', '.env', ''),
            ('crates/x/.env', 'crates/x/.env', ''),
            ('generated.rs', 'generated.rs', ''),
            ('.cache/x', '.cache/x', ''),
            ('.cache/x', '.cache/x', '.cache/'),
            ('.link', '.link', ''),
            (os.fsdecode(b'odd/line\n\x1b[31m\xff'), r'odd/line\n\x1b[31m\udcff', ''),
        )
        for path, displayed_path, allowed in cases:
            with self.subTest(path=path, allowed=allowed):
                _ = settings.write_text(f'source_allowed_ignored=target/ {allowed}\nsource_forbidden_paths=*.key\n')
                ignored = self.checkout / path
                ignored.parent.mkdir(parents=True, exist_ok=True)
                if path == '.link':
                    ignored.symlink_to('tracked')
                else:
                    _ = ignored.write_text('ignored input\n')
                try:
                    if allowed:
                        transport.check_inputs(self.checkout, conf.read(settings))
                        commit = copies.capture_source(self.checkout)
                        self.assertNotIn(path, self.git('ls-tree', '-r', '--name-only', '-z', commit).split('\0'))
                    else:
                        with self.assertRaises(hana_protocol.SourceRefused) as refused:
                            transport.check_inputs(self.checkout, conf.read(settings))
                        self.assertEqual(str(refused.exception),
                                         'IgnoredInput: source has an ignored path outside source_allowed_ignored: '
                                         + displayed_path)
                finally:
                    ignored.unlink()

    def test_credentials_and_disallowed_environment_refuse_before_upload(self) -> None:
        _ = (self.checkout / '.env').write_text('fixture secret')
        with self.assertRaisesRegex(hana_protocol.SourceRefused, 'CredentialOrBuildInput: .* forbidden path .env'):
            _ = self.prepare()
        (self.checkout / '.env').unlink()
        unreadable = Path(os.fsdecode(os.fsencode(self.checkout) + b'/\xff.key'))
        _ = unreadable.write_text('fixture secret')
        with self.assertRaises(hana_protocol.SourceRefused) as refused:
            _ = self.prepare()
        self.assertIn(b'\\xff.key', str(refused.exception).encode())
        unreadable.unlink()
        (self.checkout / 'build/target').mkdir(parents=True)
        _ = (self.checkout / 'build/target/output').write_text('untracked build output')
        _ = (self.checkout / '.gitignore').write_text('.direnv/\n__pycache__/\n.claude/settings.local.json\nprivate.bin\n')
        with self.assertRaisesRegex(hana_protocol.SourceRefused, 'forbidden path build/target/output'):
            _ = self.prepare()
        _ = self.git('add', 'build/target/output')
        self.assertIn('build/target/output', self.git('ls-tree', '-r', '--name-only', self.prepare().commit))
        _ = self.git('rm', '-r', '-f', '--quiet', 'build')
        _ = self.git('checkout', '--quiet', '.gitignore')
        self.objects.uploads.clear()
        for name in ('HOME', 'AWS_ACCESS_KEY_ID', 'CARGO_TARGET_DIR'):
            with (self.subTest(name=name), ExitStack() as staging,
                  self.assertRaisesRegex(hana_protocol.SourceRefused, 'EnvironmentRefused')):
                _, upload = self.sender.prepare(self.checkout, ('cargo', 'check'),
                                                environment={name: 'fixture'}, staging=staging)
                upload()
        self.assertEqual(self.objects.uploads, [])

    def test_toolchain_mismatch_refuses_before_execution(self) -> None:
        manifest = replace(self.prepare(), toolchain='0' * 64)
        with self.guest(), self.assertRaisesRegex(hana_protocol.SourceRefused, 'ToolchainMismatch'), self.materialize(manifest):
            self.fail('wrong toolchain was admitted')

    def test_manifest_json_is_typed_bounded_and_has_no_caller_home(self) -> None:
        manifest = self.prepare()
        self.assertEqual(hana_protocol.decode_manifest(manifest.encode(), self.limits), manifest)
        self.assertLessEqual(len(manifest.encode()), hana_protocol.CONTROL_MAX_BYTES)
        self.assertNotIn(str(Path.home()), manifest.encode().decode())
        at_limit = manifest.encode().ljust(hana_protocol.CONTROL_MAX_BYTES, b' ')
        self.assertEqual(hana_receiver.read_document(at_limit), cast(object, json.loads(manifest.encode())))
        with self.assertRaisesRegex(ValueError, 'protocol limit'):
            _ = hana_receiver.read_document(at_limit + b' ')
        with self.assertRaisesRegex(hana_protocol.SourceRefused, 'ManifestTooLarge'):
            _ = replace(manifest, argv=('x' * hana_protocol.CONTROL_MAX_BYTES,)).encode()
        raw = cast(dict[str, object], json.loads(manifest.encode()))
        for field, value in (('argv', [True]), ('runtime_abi', True)):
            with self.subTest(field=field), self.assertRaisesRegex(hana_protocol.SourceRefused, 'InvalidManifest'):
                _ = hana_protocol.decode_manifest(json.dumps({**raw, field: value}).encode(), self.limits)
        _ = raw.pop('runtime_abi')
        self.assertNotEqual(hana_protocol.decode_manifest(json.dumps(raw).encode(), self.limits).runtime_abi,
                            hana_protocol.RUNTIME_ABI)

    def test_archive_manifest_uses_the_abi_of_the_captured_runtime(self) -> None:
        newer = hana_protocol.RUNTIME_ABI + 1
        runtime = self.runtime_checkout / 'scripts/aws_target/hana_protocol.py'
        _ = runtime.write_text(f'RUNTIME_ABI = {newer}\n')
        manifest = self.prepare()
        self.assertEqual(manifest.runtime_abi, newer)
        self.assertEqual(self.member(manifest, 'runtime/abi'), f'{newer}\n'.encode())

    def test_upload_bound_applies_to_the_entire_archive(self) -> None:
        manifest = self.prepare()
        bundle_size = len(self.member(manifest, 'source.bundle'))
        override_fixture_settings(self.configuration.source, f'source_max_bytes={bundle_size}\n')
        objects = MemorySourceObjects()
        sender = transport.SourceSender(objects, self.root / 'acknowledged', self.configuration,
                                        checkout=self.runtime_checkout)
        with ExitStack() as staging, self.assertRaisesRegex(hana_protocol.SourceRefused, 'archive exceeds its size bound'):
            _, upload = sender.prepare(self.checkout, ('cargo', 'check'), environment={}, staging=staging)
            upload()
        self.assertEqual(objects.uploads, [])

    def test_download_uses_curl_with_total_time_and_size_bounds(self) -> None:
        manifest = self.prepare()
        timeout = self.configuration.whole('source_download_timeout_s')
        with tempfile.TemporaryFile(dir=self.root) as archive:
            _ = archive.write(b'good')
            with mock.patch.object(subprocess, 'run') as runner:
                hana_payload.download_payload(manifest.bundle_url, archive, 4, timeout)
            runner.assert_called_once_with(
                ['curl', '--disable', '--fail', '--silent', '--globoff', '--proto', '=https',
                 '--max-time', str(timeout), '--max-filesize', '4', '--', manifest.bundle_url],
                stdout=archive, stderr=subprocess.DEVNULL, check=True, timeout=timeout)
            for failure in (subprocess.TimeoutExpired('curl', timeout),
                            subprocess.CalledProcessError(63, 'curl')):
                with (self.subTest(failure=type(failure).__name__),
                      mock.patch.object(subprocess, 'run', side_effect=failure),
                      self.assertRaisesRegex(ValueError, 'size or time bound')):
                    hana_payload.download_payload(manifest.bundle_url, archive, 4, timeout)
            _ = archive.write(b'too much')
            with (mock.patch.object(subprocess, 'run'),
                  self.assertRaisesRegex(ValueError, 'size bound')):
                hana_payload.download_payload(manifest.bundle_url, archive, 4, timeout)

    def test_deleted_credentials_never_enter_complete_or_delta_bundle_objects(self) -> None:
        _ = (self.checkout / '.env').write_text('synthetic credential history\n')
        _ = self.git('add', '.env')
        _ = commit_repository(self.checkout, 'old credential')
        secret = self.git('rev-parse', 'HEAD:.env')
        _ = self.git('rm', '.env')
        first = self.prepare()
        self.sender.acknowledge(hana_protocol.SourceIdentity(first.repository, first.commit, first.toolchain))
        _ = (self.checkout / 'new').write_text('later snapshot\n')
        second = self.prepare()
        self.assertEqual(second.basis_commits, (first.commit,))
        with build_folders.tool_folder('hana-bundle-object-test') as inspection:
            _ = source.git(inspection, self.limits, 'init', '--bare', '--quiet')
            for number, manifest in enumerate((first, second)):
                with self.subTest(bundle=number):
                    self.assertEqual(self.git('rev-list', '--parents', '-1', manifest.commit), manifest.commit)
                    payload = self.member(manifest, 'source.bundle').split(b'\n\n', 1)[1]
                    indexed = subprocess.run(['git', '-C', str(inspection), 'index-pack', '--stdin', '--fix-thin'],
                                       input=payload, check=True, capture_output=True, timeout=self.limits.git_timeout_s)
                    objects = source.git(inspection, self.limits, 'cat-file', '--batch-all-objects', '--batch-check')
                    self.assertNotIn(secret, objects)
                    pack = indexed.stdout.decode().split()[-1]
                    packed = source.git(inspection, self.limits, 'verify-pack', '-v',
                                        str(inspection / 'objects/pack' / ('pack-' + pack + '.idx')))
                    self.assertNotIn(secret, packed)
                    if number == 1:
                        self.assertNotIn(self.git('rev-parse', first.commit + ':large'), packed)

    def test_capture_refuses_an_untracked_nested_repository_before_upload(self) -> None:
        nested = self.checkout / 'nested'
        nested.mkdir()
        _ = copies.git(nested, 'init', '--quiet')
        _ = (nested / 'file').write_text('nested source\n')
        _ = commit_repository(nested, 'nested source')
        with self.assertRaisesRegex(hana_protocol.SourceRefused, 'NestedRepository: nested') as refused:
            _ = self.prepare()
        self.assertIsInstance(refused.exception.__cause__, copies.PreparationError)
        self.assertEqual(self.objects.uploads, [])

    def test_executable_mode_change_during_capture_refuses(self) -> None:
        _ = (self.checkout / 'tracked').write_text('modified script\n')
        index = (self.checkout / '.git/index').read_bytes()
        staged_tree = copies.staged_tree
        def change_mode(path: Path, *, include_worktree: bool = False) -> str:
            tree = staged_tree(path, include_worktree=include_worktree)
            if include_worktree:
                (path / 'tracked').chmod(0o755)
            return tree
        with mock.patch.object(copies, 'staged_tree', side_effect=change_mode):
            with self.assertRaisesRegex(copies.PreparationError, 'SourceChanged'):
                _ = self.prepare()
        self.assertEqual((self.checkout / '.git/index').read_bytes(), index)
        self.assertEqual(self.objects.uploads, [])

    def test_source_change_during_capture_refuses_and_preserves_the_real_index(self) -> None:
        index = self.checkout / '.git/index'
        before = index.read_bytes()
        staged_tree = copies.staged_tree
        def change_during_capture(path: Path, *, include_worktree: bool = False) -> str:
            tree = staged_tree(path, include_worktree=include_worktree)
            if include_worktree:
                _ = (path / 'tracked').write_text('changed during capture\n')
            return tree
        with mock.patch.object(copies, 'staged_tree', side_effect=change_during_capture):
            with self.assertRaisesRegex(copies.SourceChangedDuringCapture, 'SourceChanged'):
                _ = self.prepare()
        self.assertEqual(index.read_bytes(), before)
        self.assertEqual(self.objects.uploads, [])
