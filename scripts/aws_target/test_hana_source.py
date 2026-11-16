"""Exercise exact source bundles with private Git repositories and guest slots."""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
from typing import cast, override
import unittest
from unittest import mock
from urllib.parse import urlsplit

from scripts.aws_target import hana_source as source, hana_transport as transport, hana_receiver
from scripts.copies import copies
from scripts.lib import build_folders, build_slots, conf
from scripts.lib.build_slots_test_support import private_slot_config
from scripts.lib.git_test_support import commit_repository
from scripts.lint.memory_test_support import write_config_fixture


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

    def fetch(self, url: str, destination: Path, limits: source.SourceLimits) -> None:
        _ = limits
        _ = destination.write_bytes(self.objects[urlsplit(url).path.rsplit('/', 1)[1]])


class HanaSourceTests(unittest.TestCase):
    def __init__(self, methodName: str = 'runTest') -> None:
        super().__init__(methodName)
        self.root: Path = Path()
        self.pool_settings: Path = Path()
        self.checkout: Path = Path()
        self.base: str = ''
        self.objects: MemorySourceObjects = MemorySourceObjects()

    @property
    def configuration(self) -> conf.ConfigRead:
        return conf.read(Path(os.environ['CONF_DIR']) / 'hana_aws.conf')

    @property
    def limits(self) -> source.SourceLimits:
        return source.read_limits(self.configuration)

    @property
    def sender(self) -> transport.SourceSender:
        return transport.SourceSender(self.objects, self.root / 'acknowledged', self.configuration)

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        private = private_slot_config(self.root, repository='hana')
        self.enterContext(mock.patch.dict(os.environ, private.environment))
        self.pool_settings = private.settings
        _ = write_config_fixture(private.settings,
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

    def prepare(self, checkout: Path | None = None) -> source.SourceManifest:
        return self.sender.prepare(checkout or self.checkout, ('cargo', 'check'), environment={'RUST_BACKTRACE': '1'})

    def materialize(self, manifest: source.SourceManifest):
        return source.materialize(manifest, self.limits, root=self.root / 'guest', fetch=self.objects.fetch,
                                  showrunner='builds', unit='aws', work='source-test')

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
        _ = write_config_fixture(self.configuration.source, 'source_basis_max_commits=1\n')
        first = self.prepare()
        with self.guest(), self.materialize(first) as leased:
            assert isinstance(leased, source.LeasedSource)
            self.sender.acknowledge(leased.identity)
        _ = (self.checkout / 'new').write_text('second request\n')
        second = self.prepare()
        self.assertEqual(second.basis_commits, (first.commit,))
        complete = self.objects.objects[first.bundle_sha256]
        delta = self.objects.objects[second.bundle_sha256]
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
        self.sender.acknowledge(source.SourceIdentity(first.repository, first.commit, first.toolchain))
        _ = (self.checkout / 'new').write_text('later source\n')
        delta = self.prepare()
        with self.guest(), self.materialize(delta) as answer:
            self.assertIsInstance(answer, source.MissingBasis)
            assert isinstance(answer, source.MissingBasis)
            self.assertEqual(answer.repository, delta.repository)
            self.assertIn(first.commit, answer.commits)
            self.assertNotIn(self.base, answer.commits)
            _ = (self.checkout / 'new').write_text('caller changed after the request\n')
            complete = self.sender.retry_complete(self.checkout, delta, answer)
        self.assertEqual(complete.commit, delta.commit)
        self.assertEqual(complete.basis_commits, ())
        with self.guest(), self.materialize(complete) as leased:
            assert isinstance(leased, source.LeasedSource)
            self.assertEqual((leased.path / 'new').read_text(), 'later source\n')

    def test_wrong_sha_changed_commit_and_failed_bundle_verification_are_refused(self) -> None:
        manifest = self.prepare()
        for changed, reason in ((replace(manifest, bundle_sha256='0' * 64), 'BundleDigestMismatch'),
                                (replace(manifest, commit=self.base), 'BundleCommitMismatch')):
            with self.subTest(reason=reason), self.guest():
                if reason == 'BundleDigestMismatch':
                    self.objects.objects[changed.bundle_sha256] = self.objects.objects[manifest.bundle_sha256]
                    changed = replace(changed, bundle_url=self.objects.signed_get(changed.bundle_sha256))
                with self.assertRaisesRegex(source.SourceRefused, reason), self.materialize(changed):
                    self.fail('invalid source was admitted')
        content = b'not a Git bundle\n'
        digest = hashlib.sha256(content).hexdigest()
        self.objects.objects[digest] = content
        invalid = replace(manifest, bundle_sha256=digest, bundle_url=self.objects.signed_get(digest))
        with self.guest(), self.assertRaisesRegex(source.SourceRefused, 'GitSourceRefused'), self.materialize(invalid):
            self.fail('invalid bundle was admitted')

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
        with self.assertRaisesRegex(source.SourceRefused, 'IgnoredInput'):
            _ = self.prepare()
        self.assertEqual(self.objects.uploads, [])

    def test_credentials_and_disallowed_environment_refuse_before_upload(self) -> None:
        _ = (self.checkout / '.env').write_text('fixture secret')
        with self.assertRaisesRegex(source.SourceRefused, 'CredentialOrBuildInput'):
            _ = self.prepare()
        (self.checkout / '.env').unlink()
        for name in ('HOME', 'AWS_ACCESS_KEY_ID', 'CARGO_TARGET_DIR'):
            with self.subTest(name=name), self.assertRaisesRegex(source.SourceRefused, 'EnvironmentRefused'):
                _ = self.sender.prepare(self.checkout, ('cargo', 'check'), environment={name: 'fixture'})
        self.assertEqual(self.objects.uploads, [])

    def test_toolchain_mismatch_refuses_before_execution(self) -> None:
        manifest = replace(self.prepare(), toolchain='0' * 64)
        with self.guest(), self.assertRaisesRegex(source.SourceRefused, 'ToolchainMismatch'), self.materialize(manifest):
            self.fail('wrong toolchain was admitted')

    def test_manifest_json_is_typed_bounded_and_has_no_caller_home(self) -> None:
        manifest = self.prepare()
        self.assertEqual(source.decode_manifest(manifest.encode(), self.limits), manifest)
        self.assertLessEqual(len(manifest.encode()), hana_receiver.CONTROL_MAX_BYTES)
        self.assertEqual(source.MANIFEST_MAX_BYTES, hana_receiver.CONTROL_MAX_BYTES)
        self.assertNotIn(str(Path.home()), manifest.encode().decode())
        with self.assertRaisesRegex(source.SourceRefused, 'ManifestTooLarge'):
            _ = replace(manifest, argv=('x' * hana_receiver.CONTROL_MAX_BYTES,)).encode()
        raw = cast(dict[str, object], json.loads(manifest.encode()))
        raw['argv'] = [True]
        with self.assertRaisesRegex(source.SourceRefused, 'InvalidManifest'):
            _ = source.decode_manifest(json.dumps(raw).encode(), self.limits)

    def test_download_uses_curl_with_total_time_and_size_bounds(self) -> None:
        manifest = self.prepare()
        limits = replace(self.limits, max_bytes=4)
        destination = self.root / 'download'
        _ = destination.write_bytes(b'good')
        with mock.patch('scripts.aws_target.hana_source.subprocess.run') as runner:
            source.download(manifest.bundle_url, destination, limits)
        runner.assert_called_once_with(
            ['curl', '--fail', '--silent', '--max-time', str(limits.download_timeout_s),
             '--max-filesize', '4', '--output', str(destination), manifest.bundle_url],
            check=True, capture_output=True, timeout=limits.download_timeout_s)
        for failure in (subprocess.TimeoutExpired('curl', limits.download_timeout_s),
                        subprocess.CalledProcessError(63, 'curl')):
            with self.subTest(failure=type(failure).__name__):
                with mock.patch('scripts.aws_target.hana_source.subprocess.run', side_effect=failure):
                    with self.assertRaisesRegex(source.SourceRefused, 'SourceDownloadBound'):
                        source.download(manifest.bundle_url, destination, limits)
        _ = destination.write_bytes(b'too much')
        with mock.patch('scripts.aws_target.hana_source.subprocess.run'):
            with self.assertRaisesRegex(source.SourceRefused, 'SourceDownloadBound'):
                source.download(manifest.bundle_url, destination, limits)

    def test_deleted_credentials_never_enter_complete_or_delta_bundle_objects(self) -> None:
        _ = (self.checkout / '.env').write_text('synthetic credential history\n')
        _ = self.git('add', '.env')
        _ = commit_repository(self.checkout, 'old credential')
        secret = self.git('rev-parse', 'HEAD:.env')
        _ = self.git('rm', '.env')
        first = self.prepare()
        self.sender.acknowledge(source.SourceIdentity(first.repository, first.commit, first.toolchain))
        _ = (self.checkout / 'new').write_text('later snapshot\n')
        second = self.prepare()
        self.assertEqual(second.basis_commits, (first.commit,))
        with build_folders.tool_folder('hana-bundle-object-test') as inspection:
            _ = source.git(inspection, self.limits, 'init', '--bare', '--quiet')
            for number, manifest in enumerate((first, second)):
                with self.subTest(bundle=number):
                    self.assertEqual(self.git('rev-list', '--parents', '-1', manifest.commit), manifest.commit)
                    payload = self.objects.objects[manifest.bundle_sha256].split(b'\n\n', 1)[1]
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
        with self.assertRaisesRegex(copies.PreparationError, 'NestedRepository: nested'):
            _ = self.prepare()
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
            with self.assertRaisesRegex(copies.PreparationError, 'SourceChanged'):
                _ = self.prepare()
        self.assertEqual(index.read_bytes(), before)
        self.assertEqual(self.objects.uploads, [])
