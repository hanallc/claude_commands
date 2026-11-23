"""Keep an accounts test's HOME and import-time registry paths together."""

import json
import os
import tempfile
from collections.abc import Callable, Generator, Iterable, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import cast
from unittest.mock import patch

from scripts.accounts import accounts
from scripts.herdr.missing_binary import MISSING_HERDR_BIN
from scripts.lint.memory_test_support import write_config_fixture
from scripts.production.showrunner_registry_test_support import scratch_showrunner_registry
from scripts.whoami import note_file
from scripts.whoami.credit_accounts_test_support import TEST_CREDIT_ACCOUNTS, set_credits


ACCOUNT_STATUS_SETTINGS = """\
# whole: 1..
usage_stale_minutes=10
# range: 0..100
next_move_percent=1
"""

ACCOUNT_MODEL_SETTINGS = """\
# whole: 1..
session_model_cache_entries=64
# whole: 128..32768
session_model_scan_kib=8192
"""

ACCOUNT_OUTAGE_SETTINGS = "# whole: 1..\noutage_claim_lease_seconds=900\n"

ACCOUNT_HOME_SETTINGS = """\
# whole: 1..
quota_relay_timeout_seconds=40
# whole: 1..
quota_edit_timeout_seconds=30
# whole: 1..
account_command_timeout_seconds=15
# whole: 1..
claude_usage_timeout_seconds=15
# whole: 1..
codex_close_timeout_seconds=3
# whole: 1..
codex_version_timeout_seconds=15
# whole: 1..
codex_update_timeout_seconds=600
# whole: 1..
login_probe_timeout_seconds=120
login_probe_directory=/tmp
# whole: 1..
temporary_file_age_days=1
# whole: 1..
move_status_count=10
# whole: 1..
half_point_pace_minutes=30
# whole: 1..
hold_default_minutes=40
# whole: 1..
role_map_timeout_seconds=20
# whole: 1..
transcript_chunk_bytes=65536
# whole: 1..
refusal_reset_wait_minutes=20
# whole: 1..
login_renewal_interval_minutes=60
nixos_repository_root=/etc/nixos
notes_directory=~/rust/hanadocs/agents
home_nixos_repository_root=~/nixos
codex_credit_accounts=none
""" + ACCOUNT_OUTAGE_SETTINGS

ACCOUNT_LOGIN_SITTING_SETTINGS = """\
login_sitting_machine=natedev
login_sitting_first=2026-11-17T10:00:00-05:00
# whole: 1..
login_sitting_interval_days=7
# whole: 1..
login_sitting_window_days=10
# whole: 1..
login_safety_margin_days=3
# whole: 1..
login_peer_read_minutes=60
# whole: 1..
login_peer_read_timeout_seconds=30
"""

ACCOUNT_WORKER_MODE_SETTINGS = """\
no_worker_mode_on=Codex workers have no capacity. Send no assign.
no_worker_mode_off=Codex workers have capacity. Use workers again.
no_worker_mode_fyi=morty dwight
"""


def clear_notes_directory_cache() -> None:
    """Clear the notes folder when a test changes the configuration."""
    configured = cast(object, getattr(note_file, "_notes_dir_at"))
    clear = cast(Callable[[], None], getattr(configured, "cache_clear"))
    clear()


def write_account_settings(
    path: Path, *, folders: tuple[str, ...] = (),
) -> None:
    """A complete scratch configuration, independent of the repository settings."""
    _ = path.write_text(
        f"[trust]\nfolders = {json.dumps(folders)}\n"
    )


def write_credit_accounts(config: Path, labels: Iterable[str]) -> None:
    """Name the Codex accounts that may spend paid credits in a scratch configuration."""
    lines = [line for line in config.read_text(encoding="utf-8").splitlines()
             if not line.startswith("codex_credit_accounts=")]
    _ = config.write_text("\n".join([*lines, f"codex_credit_accounts={' '.join(sorted(labels)) or 'none'}"]) + "\n",
                          encoding="utf-8")


def set_config_credits(label: str, setting: str) -> None:
    """Apply a test's "on" or "off" for one account in the scratch configuration."""
    config = Path(os.environ["CONF_DIR"]) / "accounts.conf"
    held = {line.removeprefix("codex_credit_accounts=") for line in config.read_text(encoding="utf-8").splitlines()
            if line.startswith("codex_credit_accounts=")}
    labels = set(" ".join(held).split())
    set_credits(labels, label, setting)
    write_credit_accounts(config, labels)


@contextmanager
def scratch_accounts_config() -> Generator[Path]:
    """Supply fixed settings for a private account home."""
    with tempfile.TemporaryDirectory(prefix="accounts-config-") as temporary:
        config = Path(temporary)
        _ = write_config_fixture(config / "accounts.conf", defaults=(
            ACCOUNT_HOME_SETTINGS + ACCOUNT_STATUS_SETTINGS + ACCOUNT_MODEL_SETTINGS))
        write_credit_accounts(config / "accounts.conf", TEST_CREDIT_ACCOUNTS)
        clear_notes_directory_cache()
        try:
            with patch.dict(os.environ, {"CONF_DIR": str(config)}):
                yield config
        finally:
            clear_notes_directory_cache()


@contextmanager
def scratch_account_home(home: Path, environment: Mapping[str, str], *, clear: bool = True) -> Generator[None]:
    """Use the supplied scratch home for environment and registry lookups."""
    with tempfile.TemporaryDirectory(prefix="accounts-settings-") as temporary:
        settings = Path(temporary) / "accounts.toml"
        write_account_settings(settings)
        with (scratch_accounts_config() as config,
              patch.object(accounts, "trust_settings_file", return_value=settings),
              # A missing herdr path keeps the host herdr out of the fixture; the gate uses the same path.
              patch.dict(os.environ, {"HERDR_BIN": MISSING_HERDR_BIN, **environment, "HOME": str(home),
                                      "CONF_DIR": str(config)}, clear=clear),
              scratch_showrunner_registry(home)):
            yield
