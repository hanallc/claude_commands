"""The fixture config key check against small committed trees."""
from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import subprocess
import tempfile
import unittest
from typing import override
from unittest import mock

from scripts.production import fixture_conf_keys

# Red 1 of item 191: test_state_backup wrote its own build_targets.conf after the
# row added unreachable_wait_seconds, and the missing-key warning failed the test.
STATE_BACKUP_FIXTURE = '''\
from pathlib import Path


def fixture(config: Path) -> None:
    _ = (config / "build_targets.conf").write_text("""\\
targets = natedev,mac
kind.natedev = local
""", encoding="utf-8")
'''

SUMMARY = "fixture config keys: {} missing, {} fixtures not checked because their text is not a literal\n"


class FixtureConfKeyTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.repo: Path = Path()
        self.base: str = ""

    @override
    def setUp(self) -> None:
        self.repo = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="fixture-keys-test-")))
        _ = self.git("init", "-b", "main")
        _ = self.git("config", "user.name", "Key Fixture")
        _ = self.git("config", "user.email", "fixture@example.invalid")
        self.write("config/build_targets.conf", "targets=natedev,mac\nkind.natedev=local\n")
        self.write("scripts/backup/test_state_backup.py", STATE_BACKUP_FIXTURE)
        self.base = self.commit()

    def git(self, *arguments: str) -> str:
        result = subprocess.run(["git", "-C", str(self.repo), *arguments],
                                capture_output=True, text=True, check=True)
        return result.stdout.strip()

    def write(self, path: str, text: str) -> None:
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        _ = target.write_text(text, encoding="utf-8")

    def commit(self) -> str:
        _ = self.git("add", ".")
        _ = self.git("commit", "--allow-empty", "-m", "fixture")
        return self.git("rev-parse", "HEAD")

    def add_key(self, text: str = "# whole: 1..\nunreachable_wait_seconds=600\n") -> str:
        self.write("config/build_targets.conf", "targets=natedev,mac\nkind.natedev=local\n" + text)
        return self.commit()

    def run_check(self, tip: str) -> tuple[int, str]:
        output = StringIO()
        with redirect_stdout(output):
            status = fixture_conf_keys.main(["--repo", str(self.repo), "--base", self.base, "--tip", tip])
        return status, output.getvalue()

    def test_a_fixture_without_a_new_key_fails_with_its_line(self) -> None:
        status, output = self.run_check(self.add_key())
        self.assertEqual(status, 1, output)
        self.assertIn("new keys in config/build_targets.conf: unreachable_wait_seconds\n", output)
        self.assertIn("scripts/backup/test_state_backup.py:5: the config/build_targets.conf fixture "
                      + "lacks unreachable_wait_seconds\n", output)
        self.assertTrue(output.endswith(SUMMARY.format(1, 0)), output)

    def test_a_fixture_that_holds_the_new_key_passes(self) -> None:
        self.write("scripts/backup/test_state_backup.py",
                   STATE_BACKUP_FIXTURE.replace("kind.natedev = local\n",
                                                "kind.natedev = local\nunreachable_wait_seconds = 1\n"))
        status, output = self.run_check(self.add_key())
        self.assertEqual(status, 0, output)
        self.assertTrue(output.endswith(SUMMARY.format(0, 0)), output)

    def test_fixture_paths_resolve_through_names_in_their_scope(self) -> None:
        self.write("scripts/build_queue/placement_test_support.py", '''\
import os
from pathlib import Path
from scripts.lib.atomic_file import write_text_atomic

TARGETS = Path("config") / "build_targets.conf"
HEADER = "targets=natedev\\n"


class Fixture:
    def setUp(self) -> None:
        self.targets = Path(os.environ["CONF_DIR"]) / "build_targets.conf"

    def write(self, root: Path, wait: int) -> None:
        write_text_atomic(root / TARGETS, f"targets=natedev\\nunreachable_wait_seconds={wait}\\n")
        _ = Path(os.environ["CONF_DIR"]).joinpath(TARGETS).write_text(HEADER + "kind.mac=ssh\\n")
        _ = self.targets.write_text(HEADER)


def config_path(config: Path) -> None:
    path = config / "build_targets.conf"
    _ = path.write_text(HEADER + "unreachable_wait_seconds=1\\n")


def records(folder: Path) -> None:
    for path in folder.glob("*.json"):
        _ = path.write_text("{")
''')
        writes = fixture_conf_keys.fixture_writes("support.py", (
            self.repo / "scripts/build_queue/placement_test_support.py").read_text(), {"build_targets.conf"})
        self.assertEqual(sorted((write.line, write.text) for write in writes), [
            (14, "targets=natedev\nunreachable_wait_seconds=0\n"), (15, "targets=natedev\nkind.mac=ssh\n"),
            (16, "targets=natedev\n"), (21, "targets=natedev\nunreachable_wait_seconds=1\n")])
        status, output = self.run_check(self.add_key())
        self.assertEqual(status, 1, output)
        for line in (15, 16):
            self.assertIn(f"scripts/build_queue/placement_test_support.py:{line}: the config/build_targets.conf "
                          + "fixture lacks unreachable_wait_seconds\n", output)
        self.assertTrue(output.endswith(SUMMARY.format(3, 0)), output)

    def test_template_fixtures_texts_that_are_not_literals_and_per_name_keys(self) -> None:
        self.write("scripts/backup/test_state_backup.py", '''\
from pathlib import Path
from scripts.lint.memory_test_support import write_config_fixture


def fixture(config: Path, template: str) -> None:
    _ = write_config_fixture(config / "build_targets.conf", "targets=natedev\\n")
    _ = (config / "build_targets.conf").write_text(template.replace("a", "b"))
    _ = (config / "fixture-mac.conf").write_text("host = fixture-mac\\n")
''')
        status, output = self.run_check(self.add_key("kind.hana-gpu=ssh\nunreachable_wait_seconds=600\n"))
        self.assertEqual(status, 0, output)
        self.assertIn("new keys in config/build_targets.conf: unreachable_wait_seconds\n", output)
        self.assertTrue(output.endswith(SUMMARY.format(0, 1)), output)
        writes = list(fixture_conf_keys.fixture_writes("test_state_backup.py", (
            self.repo / "scripts/backup/test_state_backup.py").read_text(), {"build_targets.conf"}))
        self.assertEqual([(write.line, write.text) for write in writes], [(7, None)])

    def test_a_new_config_file_and_an_unchanged_file_need_no_key_from_the_other(self) -> None:
        self.write("config/machine_facts.conf", "probe_seconds=5\n")
        tip = self.commit()
        self.write("scripts/buildlog/test_machine_facts.py",
                   'from pathlib import Path\n_ = Path("machine_facts.conf").write_text("other=1\\n")\n')
        status, output = self.run_check(self.commit())
        self.assertEqual(status, 1, output)
        self.assertIn("scripts/buildlog/test_machine_facts.py:2: the config/machine_facts.conf fixture "
                      + "lacks probe_seconds\n", output)
        self.assertNotIn("build_targets.conf", output)
        self.assertEqual(self.run_check(tip)[0], 0)

    def test_an_unknown_revision_and_a_source_with_bad_syntax_are_faults(self) -> None:
        self.write("scripts/backup/test_broken.py", "def broken(:\n")
        broken = self.add_key()
        for tip, fault in (("absent", "fatal: "), (broken, "test_broken.py")):
            with self.subTest(tip=tip):
                errors = StringIO()
                with redirect_stdout(StringIO()), mock.patch("sys.stderr", errors):
                    status = fixture_conf_keys.main(["--repo", str(self.repo), "--base", self.base, "--tip", tip])
                self.assertEqual(status, 2)
                self.assertTrue(errors.getvalue().startswith("fixture_conf_keys: "), errors.getvalue())
                self.assertIn(fault, errors.getvalue())


if __name__ == "__main__":
    _ = unittest.main()
