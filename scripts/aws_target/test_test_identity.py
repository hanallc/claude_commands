"""Verify the test account command with fake AWS and GPG executors."""

from collections.abc import Sequence
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import traceback
from typing import cast, override
import unittest
from unittest import mock

from scripts.aws_target import admin_secrets, aws_test_identity, deploy, plan_identity
from scripts.lib import conf
from scripts.lib.atomic_file import sync_folder


BUILD_ACCOUNT = '000000000000'
TEST_ACCOUNT = '000000000001'
OTHER_ACCOUNT = '000000000002'
FIRST_KEY = 'AKIA' + 'F' * 16
SECOND_KEY = 'AKIA' + 'S' * 16
SECRET = 's' * 40
PLAN_KEY = 'AKIA' + 'P' * 16
PLAN_SECRET = 'p' * 40
SETTINGS = Path('fixture.conf')


class IdentityCommands:
    def __init__(self) -> None:
        self.accounts: dict[str, str] = {FIRST_KEY: TEST_ACCOUNT, SECOND_KEY: TEST_ACCOUNT, PLAN_KEY: BUILD_ACCOUNT}
        self.calls: list[tuple[str, ...]] = []
        self.identities: list[str] = []
        self.ciphertexts: dict[bytes, bytes] = {}
        self.sts_failure: str = ''
        self.plan_sts_failure: bool = False
        self.gpg_failure: bool = False
        self.recipient_missing: bool = False
        self.agent_cold: bool = False

    def execute(self, arguments: Sequence[str], *, input: bytes | None = None, capture_output: bool,
                timeout: float, check: bool, env: dict[str, str]) -> subprocess.CompletedProcess[bytes]:
        assert capture_output and not check and timeout > 0
        self.calls.append(tuple(arguments))
        if arguments[0] == 'aws':
            assert input is None
            assert arguments[3:5] == ['sts', 'get-caller-identity']
            assert env['AWS_CONFIG_FILE'] == env['AWS_SHARED_CREDENTIALS_FILE'] == '/dev/null'
            assert env['AWS_EC2_METADATA_DISABLED'] == 'true'
            assert not {'AWS_SESSION_TOKEN', 'AWS_PROFILE', 'AWS_DEFAULT_PROFILE', 'AWS_ENDPOINT_URL',
                        'AWS_CONTAINER_CREDENTIALS_FULL_URI', 'AWS_WEB_IDENTITY_TOKEN_FILE', 'AWS_ROLE_ARN'} & env.keys()
            key = env['AWS_ACCESS_KEY_ID']
            assert env['AWS_SECRET_ACCESS_KEY'] == (PLAN_SECRET if key == PLAN_KEY else SECRET)
            self.identities.append(key)
            if self.sts_failure == 'timeout':
                raise subprocess.TimeoutExpired(arguments, timeout, stderr=f'{key} {SECRET}'.encode())
            if self.sts_failure == 'refused' or (key == PLAN_KEY and self.plan_sts_failure):
                error = ('An error occurred (InvalidClientTokenId) when calling the GetCallerIdentity operation: '
                         + f'{key} {SECRET}')
                return subprocess.CompletedProcess(arguments, 254, b'', error.encode())
            if self.sts_failure == 'malformed':
                return subprocess.CompletedProcess(arguments, 0, f'{key} {SECRET}'.encode(), b'')
            account = self.accounts[key]
            identity = f'user{plan_identity.IAM_PATH}{plan_identity.NAME}' if key == PLAN_KEY else 'user/fixture'
            reply = {'Account': account, 'Arn': f'arn:aws:iam::{account}:{identity}'}
            return subprocess.CompletedProcess(arguments, 0, json.dumps(reply).encode(), b'')
        assert arguments[0] == 'gpg' and input is not None
        if '--list-only' in arguments:
            if self.recipient_missing:
                return subprocess.CompletedProcess(arguments, 2, b'', b'gpg: missing encrypted GitHub token')
            return subprocess.CompletedProcess(arguments, 0, b'[GNUPG:] ENC_TO AAAAAAAAAAAAAAAA 1 0\n', b'')
        if '--encrypt' in arguments:
            assert 'AAAAAAAAAAAAAAAA!' in arguments
            if self.gpg_failure:
                return subprocess.CompletedProcess(arguments, 2, b'', input)
            ciphertext = f'encrypted-fixture-{len(self.ciphertexts)}'.encode()
            self.ciphertexts[ciphertext] = input
            return subprocess.CompletedProcess(arguments, 0, ciphertext, b'')
        assert '--decrypt' in arguments and '--batch' in arguments
        if self.agent_cold:
            return subprocess.CompletedProcess(arguments, 2, b'', b'[GNUPG:] ERROR pkdecrypt_failed 67108949\n')
        if input not in self.ciphertexts:
            return subprocess.CompletedProcess(arguments, 2, b'', input)
        return subprocess.CompletedProcess(arguments, 0, self.ciphertexts[input], b'')


class TestIdentityTests(unittest.TestCase):
    @override
    def setUp(self) -> None:
        home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        state = home / '.local/state/build-targets/aws'
        state.mkdir(parents=True)
        self.deployment: Path = state / 'deployment.json'  # pyright: ignore[reportUninitializedInstanceVariable]
        self.record: Path = state / 'test-identity.json'  # pyright: ignore[reportUninitializedInstanceVariable]
        self.key: Path = home / '.config/aws-build/test.gpg'  # pyright: ignore[reportUninitializedInstanceVariable]
        self.commands: IdentityCommands = IdentityCommands()  # pyright: ignore[reportUninitializedInstanceVariable]
        _ = self.deployment.write_text(json.dumps({'account_id': BUILD_ACCOUNT}))
        _ = self.enterContext(mock.patch.object(Path, 'home', return_value=home))
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {'AWS_PROFILE': 'fixture-profile',
            'AWS_DEFAULT_PROFILE': 'fixture-default-profile', 'AWS_SESSION_TOKEN': 'fixture-token',
            'AWS_ENDPOINT_URL': 'https://example.invalid', 'AWS_ROLE_ARN': 'fixture-role',
            'AWS_CONTAINER_CREDENTIALS_FULL_URI': 'https://example.invalid/credentials',
            'AWS_WEB_IDENTITY_TOKEN_FILE': 'fixture-token-file'}, clear=True)))
        _ = self.enterContext(mock.patch.object(subprocess, 'run', new=self.commands.execute))
        _ = self.enterContext(mock.patch.object(conf, 'read', new=self.read_settings))
        self.save_plan_key()

    def read_settings(self, settings: Path) -> conf.ConfigRead:
        self.assertEqual(settings, SETTINGS)
        return conf.ConfigRead(SETTINGS, SETTINGS, conf.settings('region=us-west-2\n'), False)

    def save_plan_key(self, key: str = PLAN_KEY) -> None:
        path = plan_identity.credential_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        ciphertext = b'plan-ciphertext-fixture'
        _ = path.write_bytes(ciphertext)
        path.chmod(0o600)
        owner = plan_identity.PlanAccount('aws', BUILD_ACCOUNT)
        self.commands.ciphertexts[ciphertext] = plan_identity.PlanCredentials(key, PLAN_SECRET, owner).encode()

    def run_command(self, *arguments: str, key: str = FIRST_KEY, secret: str = SECRET) -> tuple[int, str]:
        with (mock.patch.object(sys, 'argv', ['deploy.py', 'test-identity', '--settings', str(SETTINGS), *arguments]),
              mock.patch.object(admin_secrets, 'read_hidden', side_effect=(key, secret)) as prompt,
              redirect_stdout(StringIO()) as output, redirect_stderr(StringIO()) as errors):
            status = deploy.main()
        if arguments == ('--check',):
            prompt.assert_not_called()
        else:
            self.assertEqual(prompt.call_args_list, [mock.call('Test AWS access key ID: '),
                                                    mock.call('Test AWS secret access key: ')])
        text = output.getvalue() + errors.getvalue()
        self.assert_private(text)
        return status, text

    def assert_private(self, text: str) -> None:
        for private in (FIRST_KEY, SECOND_KEY, SECRET, PLAN_KEY, PLAN_SECRET, TEST_ACCOUNT, BUILD_ACCOUNT, OTHER_ACCOUNT):
            self.assertNotIn(private, text)
            self.assertNotIn(private, str(self.commands.calls))

    def assert_nothing_saved(self) -> None:
        self.assertFalse(self.key.exists())
        self.assertFalse(self.record.exists())
        self.assertFalse(any('--encrypt' in arguments for arguments in self.commands.calls))

    def test_install_check_and_replace_key_for_same_account(self) -> None:
        self.assertEqual(self.run_command(), (0, 'test-identity: ready\n'))
        self.assertEqual(self.commands.identities, [FIRST_KEY, PLAN_KEY])
        sts_regions = [call[2] for call in self.commands.calls if call[0] == 'aws']
        self.assertEqual(sts_regions, ['us-east-1', 'us-west-2'])
        self.assertEqual(stat.S_IMODE(self.key.stat().st_mode), 0o600)
        first = self.key.read_bytes()
        self.assertNotIn(SECRET.encode(), first)
        self.assertEqual(json.loads(self.record.read_text()), {'account_id': TEST_ACCOUNT})
        self.assertEqual(self.run_command('--check'), (0, 'test-identity: ready\n'))
        self.assertEqual(aws_test_identity.test_credentials()['AWS_ACCESS_KEY_ID'], FIRST_KEY)
        self.assertEqual(self.run_command(key=SECOND_KEY), (0, 'test-identity: ready\n'))
        self.assertNotEqual(self.key.read_bytes(), first)
        self.assertEqual(aws_test_identity.test_credentials()['AWS_ACCESS_KEY_ID'], SECOND_KEY)
        self.assertEqual(self.commands.identities, [FIRST_KEY, PLAN_KEY, FIRST_KEY, FIRST_KEY, SECOND_KEY, PLAN_KEY, SECOND_KEY])

    def test_sts_failure_stores_nothing_and_hides_diagnostics(self) -> None:
        cannot_verify = 'AWS STS could not verify the test key. Check the access key in the test account.'
        for failure, expected in (
            ('refused', cannot_verify),
            ('timeout', 'AWS did not answer. Run deploy.py test-identity again.'),
            ('malformed', cannot_verify),
        ):
            with self.subTest(failure=failure):
                self.commands.sts_failure = failure
                self.assertEqual(self.run_command(), (1, f'test-identity: refused: {expected}\n'))
                self.assert_nothing_saved()

    def test_build_account_key_is_refused(self) -> None:
        self.commands.accounts[FIRST_KEY] = BUILD_ACCOUNT
        self.assertEqual(self.run_command(), (1, 'test-identity: refused: The key belongs to the build account. '
                                              + 'Create the key in the test account.\n'))
        self.assert_nothing_saved()

    def test_stale_build_record_cannot_admit_a_build_account_key(self) -> None:
        _ = self.deployment.write_text(json.dumps({'account_id': OTHER_ACCOUNT}))
        self.commands.accounts[FIRST_KEY] = BUILD_ACCOUNT
        self.assertEqual(self.run_command(), (1, 'test-identity: refused: The key belongs to the build account. '
                                              + 'Create the key in the test account.\n'))
        self.assertEqual(self.commands.identities, [FIRST_KEY, PLAN_KEY])
        self.assert_nothing_saved()
        self.commands.accounts[FIRST_KEY] = TEST_ACCOUNT
        self.assertEqual(self.run_command(), (0, 'test-identity: ready\n'))
        self.assertEqual(self.commands.identities, [FIRST_KEY, PLAN_KEY, FIRST_KEY, PLAN_KEY])

    def test_different_account_cannot_replace_key(self) -> None:
        self.assertEqual(self.run_command(), (0, 'test-identity: ready\n'))
        saved = self.key.read_bytes(), self.record.read_bytes()
        self.commands.accounts[SECOND_KEY] = OTHER_ACCOUNT
        self.assertEqual(self.run_command(key=SECOND_KEY), (1, 'test-identity: refused: The key belongs to a different account. '
                                                            + 'Use a key from the recorded test account.\n'))
        self.assertEqual((self.key.read_bytes(), self.record.read_bytes()), saved)

    def test_cannot_install_without_evidence_of_the_build_account(self) -> None:
        self.commands.plan_sts_failure = True
        self.assertEqual(self.run_command(), (1, 'test-identity: refused: Cannot confirm the build account. '
                                              + 'Run deploy.py plan-identity --check.\n'))
        self.assertEqual(self.commands.identities, [FIRST_KEY, PLAN_KEY])
        self.assert_nothing_saved()
        self.commands.plan_sts_failure = False
        plan_identity.credential_path().unlink()
        self.assertEqual(self.run_command(), (1, 'test-identity: refused: Cannot confirm the build account. '
                                              + 'Run deploy.py plan-identity --check.\n'))
        self.assert_nothing_saved()

    def test_cold_plan_key_names_the_warmup_command(self) -> None:
        self.commands.agent_cold = True
        self.assertEqual(self.run_command(), (1, 'test-identity: refused: The plan key is locked. Run github-warmup.\n'))
        self.assertEqual(self.commands.identities, [FIRST_KEY])
        self.assert_nothing_saved()

    def test_blank_or_malformed_credentials_never_call_aws(self) -> None:
        for key, secret in (('', SECRET), ('malformed-key', SECRET), (FIRST_KEY, ''), (FIRST_KEY, 'malformed secret')):
            with self.subTest(key_is_valid=key == FIRST_KEY, blank=not key or not secret):
                self.assertEqual(self.run_command(key=key, secret=secret), (
                    1, 'test-identity: refused: Enter a valid IAM access key ID and secret from the test account.\n'))
                self.assertEqual(self.commands.calls, [])
                self.assert_nothing_saved()

    def test_plan_check_uses_the_same_isolated_key_validation(self) -> None:
        with (mock.patch.object(sys, 'argv', ['deploy.py', 'plan-identity', '--check', '--settings', str(SETTINGS)]),
              mock.patch.object(admin_secrets, 'read_hidden') as prompt,
              redirect_stdout(StringIO()) as output, redirect_stderr(StringIO()) as errors):
            self.assertEqual(deploy.main(), 0)
            self.assertEqual(output.getvalue(), 'plan-identity: ready\n')
            self.assertEqual(self.commands.identities, [PLAN_KEY])
            self.assertEqual(self.commands.calls[-1][2], 'us-west-2')
            self.save_plan_key('malformed-plan-key')
            self.assertEqual(deploy.main(), 1)
            self.assertEqual(self.commands.identities, [PLAN_KEY])
            self.assertEqual(errors.getvalue(), 'plan-identity: refused: The saved plan key has invalid fields. '
                             + 'Run deploy.py down --workload plan-identity --approve-destroy, '
                             + 'then deploy.py plan-identity --approve-resources.\n')
            prompt.assert_not_called()
            self.assert_private(output.getvalue() + errors.getvalue())

    def test_interrupted_check_names_the_check_command(self) -> None:
        self.assertEqual(self.run_command(), (0, 'test-identity: ready\n'))
        saved = self.key.read_bytes(), self.record.read_bytes()
        with mock.patch.object(subprocess, 'run', side_effect=KeyboardInterrupt):
            self.assertEqual(self.run_command('--check'), (1, 'test-identity: refused: Interrupted. '
                                                           + 'Run deploy.py test-identity --check again.\n'))
        self.assertEqual((self.key.read_bytes(), self.record.read_bytes()), saved)
        self.assertEqual(self.run_command('--check'), (0, 'test-identity: ready\n'))

    def test_locked_test_key_names_the_warmup_command(self) -> None:
        self.assertEqual(self.run_command(), (0, 'test-identity: ready\n'))
        self.commands.agent_cold = True
        self.assertEqual(self.run_command('--check'), (1, 'test-identity: refused: The test key is locked. '
                                                       + 'Run github-warmup.\n'))
        self.commands.agent_cold = False
        self.assertEqual(self.run_command('--check'), (0, 'test-identity: ready\n'))

    def test_interrupted_publication_can_finish_on_retry(self) -> None:
        replace = os.replace
        with mock.patch.object(os, 'replace', side_effect=KeyboardInterrupt):
            self.assertEqual(self.run_command(), (1, 'test-identity: refused: Interrupted. '
                                                  + 'Run deploy.py test-identity again with the same account.\n'))
        self.assertFalse(self.key.exists())
        self.assertEqual(json.loads(self.record.read_text()), {'account_id': TEST_ACCOUNT})
        with mock.patch.object(aws_test_identity, 'sync_folder', wraps=sync_folder) as sync:
            def finish(source: Path, path: Path) -> None:
                self.assertEqual(path, self.key)
                sync.assert_called_once_with(self.record.parent)
                replace(source, path)
            with mock.patch.object(os, 'replace', side_effect=finish):
                self.assertEqual(self.run_command(), (0, 'test-identity: ready\n'))
        self.assertEqual(self.run_command('--check'), (0, 'test-identity: ready\n'))

    def test_local_write_failure_names_the_action(self) -> None:
        with mock.patch.object(os, 'replace', side_effect=PermissionError):
            self.assertEqual(self.run_command(), (1, 'test-identity: refused: Cannot access local test identity files. '
                                                  + 'Check access to the config and state folders.\n'))
        self.assertFalse(self.key.exists())
        self.assertEqual(self.run_command(), (0, 'test-identity: ready\n'))

    def test_check_refuses_missing_files(self) -> None:
        self.assertEqual(self.run_command('--check'), (1, 'test-identity: refused: The test key is missing. '
                                                       + 'Run deploy.py test-identity.\n'))
        self.assertEqual(self.run_command(), (0, 'test-identity: ready\n'))
        self.record.unlink()
        self.assertEqual(self.run_command('--check'), (1, 'test-identity: refused: The test account record is missing. '
                                                       + 'Restore the local record.\n'))

    def test_unreadable_or_damaged_test_key_names_the_install_command(self) -> None:
        for cause in ('permissions', 'ciphertext'):
            with self.subTest(cause=cause):
                self.assertEqual(self.run_command(), (0, 'test-identity: ready\n'))
                if cause == 'permissions':
                    self.key.chmod(0o644)
                else:
                    _ = self.key.write_text(SECRET)
                self.assertEqual(self.run_command('--check'), (
                    1, 'test-identity: refused: Cannot read the private test key. '
                    + 'Run deploy.py test-identity with a key from the recorded test account.\n'))
                self.assertEqual(self.run_command(key=SECOND_KEY), (0, 'test-identity: ready\n'))
                self.assertEqual(self.run_command('--check'), (0, 'test-identity: ready\n'))

    def test_check_and_reader_refuse_sts_failure_or_account_change(self) -> None:
        self.assertEqual(self.run_command(), (0, 'test-identity: ready\n'))
        self.commands.sts_failure = 'timeout'
        self.assertEqual(self.run_command('--check'), (1, 'test-identity: refused: AWS did not answer. '
                                                       + 'Run deploy.py test-identity --check again.\n'))
        with self.assertRaises(aws_test_identity.TestIdentityRefused) as error:
            _ = aws_test_identity.test_credentials()
        self.assert_private(''.join(traceback.format_exception(error.exception)))
        self.commands.sts_failure = ''
        for account in (BUILD_ACCOUNT, OTHER_ACCOUNT):
            self.commands.accounts[FIRST_KEY] = account
            self.assertEqual(self.run_command('--check'), (1, 'test-identity: refused: The key belongs to a different account. '
                                                           + 'Use a key from the recorded test account.\n'))
            with self.assertRaises(aws_test_identity.TestIdentityRefused) as error:
                _ = aws_test_identity.test_credentials()
            self.assert_private(''.join(traceback.format_exception(error.exception)))

    def test_gpg_failure_stores_nothing_and_hides_diagnostics(self) -> None:
        for failure, expected in (
            ('recipient_missing', 'GPG could not identify the encryption recipient. '
                                  + 'Restore ~/.config/gh/token.gpg with one GPG recipient. '
                                  + 'Run deploy.py test-identity again.'),
            ('gpg_failure', 'Cannot verify GPG encryption for the test key. Run github-warmup and try again.'),
        ):
            with self.subTest(failure=failure):
                with mock.patch.object(self.commands, failure, True):
                    self.assertEqual(self.run_command(), (1, f'test-identity: refused: {expected}\n'))
                self.assertFalse(self.key.exists())
                self.assertFalse(self.record.exists())


if __name__ == '__main__':
    _ = unittest.main()
