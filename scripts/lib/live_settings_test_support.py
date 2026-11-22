"""Fixed settings for tests that publish a setting or wake the Mac."""

from pathlib import Path

from scripts.lint.memory_test_support import write_config_fixture


def write_live_settings_fixture(directory: Path) -> None:
    _ = write_config_fixture(directory / "live_settings.conf", defaults="""\
readers=none
# whole: 1..
fetch_seconds=10
# whole: 1..
push_seconds=10
# whole: 1..
mac_pull_seconds=20
# whole: 1..
mac_write_seconds=10
# whole: 1..
mac_wake_probe_seconds=5
# whole: 1..
mac_wake_pause_seconds=10
""")
