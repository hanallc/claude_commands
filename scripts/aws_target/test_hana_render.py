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
HANA_CONFIG = Path('config/hana_aws.conf')
BUILD_TARGETS_CONFIG = Path('config/build_targets.conf')
HANA_RENDER_SETTINGS = """\
enabled=on
# whole: 1..60
guard_grace_seconds=19
# whole: 1..60
watchdog_check_seconds=2
# whole: 1..1800
permit_max_seconds=90
# whole: 1..8
step_cpu_limit=3
# whole: 1..24
step_memory_gib=12
# whole: 1..60
mount_timeout_seconds=8
# whole: 1..
aws_timeout_s=5
# whole: 1..
control_poll_seconds=3
"""
BUILD_TARGETS_SETTINGS = """\
# range: 0.01..300
hold_sender_timeout_seconds=17
loopback.shell_timeout_s=13
# range: 1..3600
loopback.command_timeout_s=43
"""


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
                if relative == HANA_CONFIG:
                    _ = destination.write_text(HANA_RENDER_SETTINGS, encoding='utf-8')
                elif relative == BUILD_TARGETS_CONFIG:
                    _ = destination.write_text(BUILD_TARGETS_SETTINGS, encoding='utf-8')
                else:
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
            self.assertEqual({path.name for path in output.iterdir()}, {*expected_modes, 'runtime'})
            configuration = conf.read(checkout / HANA_CONFIG)
            files = (*hana_guest.render_files(hana_guest.read_settings(configuration), device=Path('/dev/sdf')),
                     *hana_receiver.render_files(hana_receiver.read_settings(configuration)))
            for file in files:
                actual = output / file.path.name
                with self.subTest(name=file.path.name):
                    self.assertEqual(stat.S_IMODE(actual.stat().st_mode), expected_modes[actual.name])
                    self.assertEqual(actual.read_bytes(), file.content.encode('utf-8'))
            runtime = output / 'runtime'
            self.assertEqual((runtime / HANA_CONFIG).read_bytes(), HANA_RENDER_SETTINGS.encode('utf-8'))
            self.assertEqual((runtime / BUILD_TARGETS_CONFIG).read_bytes(),
                             (checkout / BUILD_TARGETS_CONFIG).read_bytes())
            self.assertIn('Environment=CONF_DIR=/var/lib/hana-runtime/config',
                          (output / 'hana-receiver.service').read_text())
            self.assertEqual({str(path.relative_to(runtime)) for path in runtime.rglob('*') if path.is_file()},
                             {*entries, 'runner.py'})
            imported = subprocess.run([sys.executable, '-I', '-c',
                'import sys; sys.path.insert(0, sys.argv[1]); '
                + 'from scripts.aws_target import hana_receiver, hana_runner, hana_execution; '
                + 'from scripts.build_queue import slot_wait; '
                + 'from scripts.lib import build_slots, git_rescue; '
                + 'assert not any(name in sys.modules for name in '
                + '("scripts.aws_target.aws_cli", "scripts.aws_target.hana_provider", "scripts.aws_target.hana_box"))',
                str(runtime)], cwd=runtime, capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(imported.returncode, 0, imported.stdout + imported.stderr)
            launcher = runtime / 'scripts/build_queue/program_start.py'
            startup = subprocess.run([sys.executable, '-E', '-B', str(launcher)], cwd=runtime,
                                     capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(startup.returncode, 2, startup.stdout + startup.stderr)
            self.assertIn('program start: expected --program-start', startup.stderr)
            for path in runtime.rglob('*'):
                if path.is_file() and '__pycache__' not in path.parts:
                    self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644)

    def test_render_configuration_has_no_host_suffixes(self) -> None:
        settings = conf.settings((ROOT / 'config/hana_aws.conf').read_text())
        self.assertTrue(settings)
        for key in settings:
            with self.subTest(key=key):
                self.assertNotIn('.', key)

    def test_execution_values_have_no_copies_in_hana_configuration(self) -> None:
        settings = conf.settings((ROOT / 'config/hana_aws.conf').read_text())
        for key in ('execution_compiler_version', 'execution_shell_timeout_s', 'execution_command_timeout_s'):
            self.assertNotIn(key, settings)
        self.assertEqual(sum(line.startswith('free_floor_gib=')
            for line in (ROOT / 'config/hana_aws.conf').read_text().splitlines()), 1)


if __name__ == '__main__':
    _ = unittest.main()
