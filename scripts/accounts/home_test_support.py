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
from scripts.lint.memory_test_support import write_config_fixture
from scripts.production.showrunner_registry_test_support import scratch_showrunner_registry
from scripts.whoami import note_file
from scripts.whoami.credit_accounts_test_support import TEST_CREDIT_ACCOUNTS, set_credits


def clear_notes_directory_cache() -> None:
    """Clear the notes folder when a test changes the configuration."""
    configured = cast(object, getattr(note_file, "_configured_notes_dir"))
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
    """Use a scratch copy of the account configuration."""
    with tempfile.TemporaryDirectory(prefix="accounts-config-") as temporary:
        config = Path(temporary)
        _ = write_config_fixture(config / "accounts.conf",
            "usage_stale_minutes=10\nquota_relay_timeout_seconds=40\nquota_edit_timeout_seconds=30\n"
            + "account_command_timeout_seconds=15\nclaude_usage_timeout_seconds=15\ncodex_close_timeout_seconds=3\n"
            + "codex_version_timeout_seconds=15\ncodex_update_timeout_seconds=600\nlogin_probe_timeout_seconds=120\n"
            + "login_probe_directory=/tmp\ntemporary_file_age_days=1\nmove_status_count=10\n"
            + "half_point_pace_minutes=30\nhold_default_minutes=40\nnext_move_percent=1\n"
            + "role_map_timeout_seconds=20\ntranscript_chunk_bytes=65536\nrefusal_reset_wait_minutes=20\n"
            + "login_renewal_interval_minutes=60\noutage_claim_lease_seconds=900\n"
            + "nixos_repository_root=/etc/nixos\nnotes_directory=~/rust/hanadocs/agents\nhome_nixos_repository_root=~/nixos\n")
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
              patch.dict(os.environ, {"HERDR_BIN": "/nonexistent/herdr", **environment, "HOME": str(home),
                                      "CONF_DIR": str(config)}, clear=clear),
              scratch_showrunner_registry(home)):
            yield
