"""Test the environment reader that the scope walk uses on each service pass."""
from __future__ import annotations

from pathlib import Path
import tempfile
from typing import override
import unittest

from scripts.build_queue.scope_activity import SCOPE_KEYS, environment_values


class EnvironmentValuesTests(unittest.TestCase):
    environ: Path = Path()

    @override
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.environ = Path(directory.name) / "environ"

    def read(self, raw: bytes) -> dict[str, str]:
        _ = self.environ.write_bytes(raw)
        return environment_values(self.environ, SCOPE_KEYS)

    def test_values_match_a_parse_of_the_whole_environment(self) -> None:
        raw = (b"BUILDLOG_RUN=41:900\0HOME=/home/user\0XBUILDLOG_RUN=no\0BUILDLOG_RUN_X=no\0"
               b"CLAUDE_SESSION_NAME=queue=unit\0EMPTY=\0PYTEST_CURRENT_TEST=\0BUILDLOG_RUN=42:901\0"
               b"CODEX_THREAD_ID=\xff\xfeid")
        whole = dict(item.split("=", 1) for item in raw.decode(errors="replace").split("\0") if "=" in item)
        self.assertEqual(self.read(raw), {key: whole[key] for key in SCOPE_KEYS if key in whole})

    def test_the_last_copy_of_a_key_wins(self) -> None:
        self.assertEqual(self.read(b"BUILDLOG_RUN=1:1\0BUILDLOG_RUN=2:2\0"), {"BUILDLOG_RUN": "2:2"})

    def test_a_key_inside_a_value_is_not_a_key(self) -> None:
        self.assertEqual(self.read(b"NOTE=BUILDLOG_RUN=7:7\0"), {})

    def test_an_unreadable_environment_has_no_values(self) -> None:
        self.assertEqual(environment_values(self.environ, SCOPE_KEYS), {})


if __name__ == "__main__":
    _ = unittest.main()
