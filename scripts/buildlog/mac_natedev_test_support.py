"""Private target settings for Mac and natedev report fixtures."""

from pathlib import Path

from scripts.buildlog.index_test_support import BUILDLOG_INDEX_SETTINGS, BUILDLOG_TARGET_SETTINGS
from scripts.lint.memory_test_support import config_environment, write_config_fixture
from scripts.mac_test.config_test_support import MAC_CONNECTION_SETTINGS


def mac_natedev_environment(folder: Path) -> dict[str, str]:
    """Keep two-machine report cases independent of the shipped target list."""
    _ = write_config_fixture(folder / "buildlog.conf", defaults=BUILDLOG_INDEX_SETTINGS)
    _ = write_config_fixture(folder / "build_targets.conf", defaults=BUILDLOG_TARGET_SETTINGS)
    _ = write_config_fixture(folder / "mac_test.conf", defaults=MAC_CONNECTION_SETTINGS)
    environment = config_environment(folder)
    environment["BUILD_TARGETS_STATE_DIR"] = str(folder / "build-targets")
    return environment
