"""Test isolated Hana rendering with only the listed repository files."""
from __future__ import annotations

from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

from scripts.aws_target import hana_guest, hana_receiver
from scripts.lib import conf
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


ROOT = Path(__file__).resolve().parents[2]
FILE_LIST = ROOT / 'scripts/aws_target/hana_render.files'


class HanaRenderTests(unittest.TestCase):
    def test_listed_files_render_all_guest_names_modes_and_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            checkout = Path(temporary) / 'checkout'
            entries = FILE_LIST.read_text().splitlines()
            self.assertEqual(len(entries), len(set(entries)))
            for entry in entries:
                relative = Path(entry)
                self.assertFalse(relative.is_absolute())
                self.assertNotIn('..', relative.parts)
                destination = checkout / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                _ = shutil.copyfile(ROOT / relative, destination)
            result = subprocess.run([sys.executable, '-E', '-m', 'scripts.aws_target.hana_render',
                                     '--device', '/dev/sdf', 'rendered'], cwd=checkout,
                                    capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            output = checkout / 'rendered'
            expected_modes = {
                'safety.py': 0o600, 'safety.json': 0o600, 'receiver.py': 0o600, 'receiver.json': 0o600,
                'hana-guard.service': 0o644, 'hana-watchdog.service': 0o644,
                'var-lib-hana.mount': 0o644, 'hana-work.slice': 0o644, 'hana-receiver.service': 0o644,
            }
            self.assertEqual({path.name for path in output.iterdir()}, set(expected_modes))
            configuration = conf.read(ROOT / 'config/hana_aws.conf')
            files = (*hana_guest.render_files(hana_guest.read_settings(configuration), device=Path('/dev/sdf')),
                     *hana_receiver.render_files(hana_receiver.read_settings(configuration)))
            for file in files:
                actual = output / file.path.name
                with self.subTest(name=file.path.name):
                    self.assertEqual(stat.S_IMODE(actual.stat().st_mode), expected_modes[actual.name])
                    self.assertEqual(actual.read_bytes(), file.content.encode('utf-8'))

    def test_render_configuration_has_no_host_suffixes(self) -> None:
        settings = conf.settings((ROOT / 'config/hana_aws.conf').read_text())
        self.assertTrue(settings)
        for key in settings:
            with self.subTest(key=key):
                self.assertNotIn('.', key)


if __name__ == '__main__':
    _ = unittest.main()
