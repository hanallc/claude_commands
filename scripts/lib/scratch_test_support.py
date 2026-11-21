"""Set private paths and limits for tests that use scratch folders."""
from __future__ import annotations

from pathlib import Path

from scripts.lint.memory_test_support import write_config_fixture


def write_scratch_settings(directory: Path, root: Path, *, visible_root: Path | None = None) -> None:
    _ = write_config_fixture(directory / "scratch.conf", f"""\
visible_root={visible_root or root / 'repro'}
holds_file={root / 'holds.json'}
# whole: 1..
default_life_seconds=86400
# whole: 1..
repo_depth=3
# whole: 1..
suffix_length=6
repo_skip=target node_modules .git
# whole: 1..
record_lock_attempts=3
# whole: 1..
removal_remote_timeout_seconds=30
""", defaults="")
