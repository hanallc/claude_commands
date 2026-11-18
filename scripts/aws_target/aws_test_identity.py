"""Register and verify credentials for the AWS test account."""

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import cast

from scripts.aws_target import admin_secrets, ledger, plan_identity
from scripts.build_targets.target_config import ssh_target_state_directory
from scripts.lib.atomic_file import create_text_atomic, prepared_replacement, sync_folder


CONSOLE_SETUP = (
    'Switch to OrganizationAccountAccessRole in the test account from the management account. '
    'Create an IAM user with AdministratorAccess and create an access key for that user. '
    'Run test-identity again with a key from the recorded test account to replace the saved key.'
)
ACCOUNT_RECORD = 'test-identity.json'
INSTALL_COMMAND = 'deploy.py test-identity'
CHECK_COMMAND = 'deploy.py test-identity --check'


class TestIdentityRefused(ValueError):
    """The test account or its credentials could not be confirmed."""


@dataclass(frozen=True, repr=False)
class TestKey:
    access_key_id: str
    secret_access_key: str

    def environment(self) -> dict[str, str]:
        return plan_identity.credential_environment(os.environ, self.access_key_id, self.secret_access_key)

    def encode(self) -> bytes:
        return json.dumps({'version': 1, 'AccessKeyId': self.access_key_id,
                           'SecretAccessKey': self.secret_access_key}).encode()

    @classmethod
    def decode(cls, content: bytes) -> 'TestKey':
        fields = ledger.mapping(cast(object, json.loads(content)))
        if fields.get('version') != 1:
            raise ValueError
        return cls(ledger.text(fields, 'AccessKeyId'), ledger.text(fields, 'SecretAccessKey'))


def _key_path() -> Path:
    return plan_identity.credential_path().with_name('test.gpg')


def _recorded_account(record: Path) -> str:
    try:
        fields = ledger.mapping(cast(object, json.loads(record.read_text())))
        account = ledger.text(fields, 'account_id')
        if re.fullmatch(r'[0-9]{12}', account) is None:
            raise ValueError
        return account
    except FileNotFoundError:
        raise
    except (OSError, ValueError):
        raise TestIdentityRefused('Cannot read the test account record. Restore the local record.') from None


def _caller_account(environment: dict[str, str]) -> str:
    # STS needs a region even when the credential environment has no AWS profile.
    reply = plan_identity.IdentityApi('us-east-1', 15, environment).call('sts', 'get-caller-identity')
    return plan_identity.PlanAccount.from_identity(reply).account


def _test_account(environment: dict[str, str], retry_command: str) -> str:
    try:
        return _caller_account(environment)
    except plan_identity.AwsDidNotAnswer:
        raise TestIdentityRefused(f'AWS did not answer. Run {retry_command} again.') from None
    except (OSError, ValueError, subprocess.SubprocessError):
        raise TestIdentityRefused('AWS STS could not verify the test key. Check the access key in the test account.') from None


def _build_account(settings: Path) -> str:
    try:
        return plan_identity.verified_plan_account(settings).account
    except plan_identity.PlanAgentCold:
        raise TestIdentityRefused('The plan key is locked. Run github-warmup.') from None
    except (OSError, ValueError, subprocess.SubprocessError):
        raise TestIdentityRefused(f'Cannot confirm the build account. Run {plan_identity.CHECK_COMMAND}.') from None


def _require_recorded_account(account: str, recorded: str) -> None:
    if account != recorded:
        raise TestIdentityRefused('The key belongs to a different account. Use a key from the recorded test account.')


def test_credentials() -> dict[str, str]:
    """Return the test credentials only after STS confirms the recorded account."""
    try:
        key = TestKey.decode(plan_identity.decrypt_credential(plan_identity.read_private_ciphertext(_key_path())))
        environment = key.environment()
    except FileNotFoundError:
        raise TestIdentityRefused('The test key is missing. Run deploy.py test-identity.') from None
    except plan_identity.PlanAgentCold:
        raise TestIdentityRefused('The test key is locked. Run github-warmup.') from None
    except (OSError, ValueError, subprocess.SubprocessError):
        raise TestIdentityRefused(
            'Cannot read the private test key. Run deploy.py test-identity with a key from the recorded test account.') from None
    try:
        account = _recorded_account(ssh_target_state_directory('aws') / ACCOUNT_RECORD)
    except FileNotFoundError:
        raise TestIdentityRefused('The test account record is missing. Restore the local record.') from None
    _require_recorded_account(_test_account(environment, CHECK_COMMAND), account)
    return environment


def _install(settings: Path) -> None:
    try:
        key = TestKey(admin_secrets.read_hidden('Test AWS access key ID: '),
                      admin_secrets.read_hidden('Test AWS secret access key: '))
    except (OSError, ValueError):
        raise TestIdentityRefused('Enter both key values in a terminal with hidden input.') from None
    try:
        environment = key.environment()
    except plan_identity.InvalidIdentityKey:
        raise TestIdentityRefused('Enter a valid IAM access key ID and secret from the test account.') from None
    account = _test_account(environment, INSTALL_COMMAND)
    if account == _build_account(settings):
        raise TestIdentityRefused('The key belongs to the build account. Create the key in the test account.')
    directory = ssh_target_state_directory('aws')
    record = directory / ACCOUNT_RECORD
    path = _key_path()
    try:
        _require_recorded_account(account, _recorded_account(record))
    except FileNotFoundError:
        if os.path.lexists(path):
            raise TestIdentityRefused('The test account record is missing. Restore the local record.') from None
    try:
        plaintext = key.encode()
        ciphertext = plan_identity.encrypt_credential(plan_identity.encryption_recipient(), plaintext)
        if plan_identity.decrypt_credential(ciphertext) != plaintext:
            raise ValueError
    except plan_identity.EncryptionRecipientUnavailable:
        raise TestIdentityRefused(
            'GPG could not identify the encryption recipient. Restore ~/.config/gh/token.gpg with one GPG recipient. '
            + f'Run {INSTALL_COMMAND} again.') from None
    except (OSError, ValueError, subprocess.SubprocessError):
        raise TestIdentityRefused('Cannot verify GPG encryption for the test key. Run github-warmup and try again.') from None
    with prepared_replacement(path, ciphertext, mode=0o600) as temporary:
        # Sync the account record before publishing the key so a retry can finish an interrupted setup.
        if not create_text_atomic(record, json.dumps({'account_id': account}) + '\n'):
            _require_recorded_account(account, _recorded_account(record))
            sync_folder(record.parent)
        os.replace(temporary, path)
        sync_folder(path.parent)


def command(settings: Path, *, check: bool) -> int:
    try:
        if check:
            _ = test_credentials()
        else:
            _install(settings)
    except TestIdentityRefused as error:
        print(f'test-identity: refused: {error}', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        action = 'test-identity --check again' if check else 'test-identity again with the same account'
        print(f'test-identity: refused: Interrupted. Run deploy.py {action}.', file=sys.stderr)
        return 1
    except OSError:
        print('test-identity: refused: Cannot access local test identity files. Check access to the config and state folders.',
              file=sys.stderr)
        return 1
    print('test-identity: ready')
    return 0
