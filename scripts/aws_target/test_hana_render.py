"""Test isolated Hana rendering with only the listed repository files."""
from __future__ import annotations

import ast
from collections.abc import Iterator
from importlib.util import resolve_name
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
from typing import cast
import unittest
from unittest import mock

from scripts.aws_target import hana_guest, hana_receiver, hana_render, hana_runner
from scripts.lib import conf
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


ROOT = Path(__file__).resolve().parents[2]
FILE_LIST = ROOT / 'scripts/aws_target/hana_render.files'

# The guest does not ship these imports for host operations, tests, and type annotations.
UNSHIPPED_IMPORTS = {
    'aws_target/hana_execution.py': {'aws_target/hana_box.py', 'buildlog/sync.py',
        'aws_target/remote.py', 'aws_target/controller_lock.py', 'aws_target/ledger.py'},
    'aws_target/hana_placement.py': {'aws_target/hana_box.py'},
    'build_queue/admission_client.py': {'accounts/__init__.py', 'accounts/outage_alerts.py',
        'message/send_budget.py', 'message/send_result.py', 'message/session_send.py', 'notify/push_texts.py'},
    'build_queue/slot_wait.py': {'hooks/checkpoint_gate.py', 'lib/shell_test_support.py'},
    'build_queue/step_estimator.py': {'lint/tmp_sweep.py'},
    'buildlog/record.py': {'aws_target/config.py', 'direct/step_record.py'},
    'buildlog/step_context.py': {'lib/cargo_target_config.py'},
    'copies/copies.py': {'lint/tmp_sweep.py', 'agents/remote.py', 'accounts/__init__.py', 'accounts/accounts.py',
        'lib/nix_env.py'},
    'direct/local_release_wait.py': {'build_targets/build_targets.py', 'build_targets/targets.py', 'build_hold/build_hold.py'},
    'lib/build_folders.py': {'lib/descendants.py'},
    'lib/scratch.py': {'direct/phase_table.py', 'production/proposals_table.py', 'production/unit_lookup.py',
        'production/production_doc.py', 'direct/plan_history.py', 'accounts/__init__.py', 'accounts/accounts.py'},
    'lib/unit_runs.py': {'direct/worker_state.py'},
    'lint/memory_admit.py': {'message/unit_labels.py'},
    'production/staff.py': {'agents/worker_lifecycle.py', 'agents/remote.py', 'lib/operation_environment.py'},
}


def imported_source_paths(source: Path) -> Iterator[Path]:
    """Find repository modules in imports at all levels of a Python file."""
    package = '.'.join(source.parent.parts)
    for node in ast.walk(ast.parse((ROOT / source).read_bytes(), filename=str(source))):
        if isinstance(node, ast.Import):
            names = [item.name for item in node.names]
        elif isinstance(node, ast.ImportFrom):
            module = resolve_name('.' * node.level + (node.module or ''), package)
            names = [module, *(f'{module}.{item.name}' for item in node.names)]
        else:
            continue
        for name in names:
            if name.startswith('scripts.'):
                path = Path(*name.split('.'))
                for candidate in (path.with_suffix('.py'), path / '__init__.py'):
                    if (ROOT / candidate).is_file():
                        yield candidate


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
                if relative.parts[0] == 'config':
                    _ = destination.write_text('')
                else:
                    _ = shutil.copyfile(ROOT / relative, destination)
            targets = checkout / 'config/build_targets.conf'
            target_values = ('loopback.shell_timeout_s=7\nloopback.shell_timeout_s.fixture-guest=11\n'
                + '# range: 1..\nloopback.command_timeout_s.fixture-guest=23\n'
                + 'loopback.command_timeout_s.fixture-other=29\nsync.rsync_timeout_s=60\n')
            _ = targets.write_text(target_values)
            copies = checkout / 'config/copies.conf'
            copies_config = 'root=/tmp/copies\nroot.aws-hana=/tmp/guest-copies\nremote_registry=/tmp/mirrors\n'
            _ = copies.write_text(copies_config)
            configuration_path = checkout / 'config/hana_aws.conf'
            _ = configuration_path.write_text(
                'host=fixture-guest\nenabled=on\n# whole: 1..\nguard_grace_seconds=60\n'
                + '# whole: 1..\nwatchdog_check_seconds=1\n'
                + '# whole: 1..\npermit_max_seconds=1800\n# whole: 1..\nstep_cpu_limit=4\n'
                + '# whole: 1..\nstep_memory_gib=24\n# whole: 1..\nmount_timeout_seconds=15\n'
                + '# whole: 1..\naws_timeout_s=10\n# whole: 1..\ncontrol_poll_seconds=2\n'
                + 'execution_shell=default\n# whole: 1..\nfree_floor_gib=30\n'
                + '# whole: 1..\nsource_max_bytes=1048576\n# whole: 1..\nsource_download_timeout_s=60\n'
                + '# whole: 1..\nsource_git_timeout_s=60\n'
                + 'source_allowed_environment=RUST_BACKTRACE\nspot_max_usd_per_hour=0.35\n')
            listed = {Path(entry) for entry in entries}
            missing = {
                f'{source} imports {dependency}'
                for source in listed if source.suffix == '.py'
                for dependency in imported_source_paths(source)
                if dependency not in listed
                and str(dependency.relative_to('scripts')) not in UNSHIPPED_IMPORTS.get(
                    str(source.relative_to('scripts')), ())
            }
            self.assertEqual(sorted(missing), [], 'The guest manifest lacks imported modules')
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
            configuration = conf.read(configuration_path)
            files = (*hana_guest.render_files(hana_guest.read_settings(configuration), device=Path('/dev/sdf')),
                     *hana_receiver.render_files(hana_receiver.read_settings(configuration)))
            for file in files:
                actual = output / file.path.name
                with self.subTest(name=file.path.name):
                    self.assertEqual(stat.S_IMODE(actual.stat().st_mode), expected_modes[actual.name])
                    if actual.name == 'safety.json':
                        self.assertRegex(actual.read_text(), r'"guest_digest": "[0-9a-f]{64}"')
                        safety = cast(dict[str, object], json.loads(actual.read_text()))
                        _ = safety.pop('guest_digest')
                        self.assertEqual(safety, cast(object, json.loads(file.content)))
                    else:
                        self.assertEqual(actual.read_bytes(), file.content.encode('utf-8'))
            runtime = output / 'runtime'
            with self.subTest('Render and read an override without a base key'):
                self.assertEqual((runtime / 'config/build_targets.conf').read_text(),
                    'loopback.shell_timeout_s=11\n# range: 1..\nloopback.command_timeout_s=23\n')
                with (mock.patch.dict(os.environ, {'CONF_DIR': str(runtime / 'config')}),
                      mock.patch('socket.gethostname', return_value='fixture-guest')):
                    self.assertEqual(hana_runner.read_timeouts(), hana_runner.GuestExecutionTimeouts(11, 23))
            self.assertEqual((runtime / 'config/copies.conf').read_text(), 'remote_registry=/tmp/mirrors\n')
            self.assertIn('Environment=CONF_DIR=/var/lib/hana-runtime/config',
                          (output / 'hana-receiver.service').read_text())
            self.assertNotIn('spot_max_usd_per_hour', conf.settings((runtime / 'config/hana_aws.conf').read_text()))
            original = configuration_path.read_text()
            digest = hana_render.guest_digest(configuration_path, checkout=checkout)
            _ = targets.write_text(target_values.replace('sync.rsync_timeout_s=60', 'sync.rsync_timeout_s=90'))
            self.assertEqual(hana_render.guest_digest(configuration_path, checkout=checkout), digest)
            for before, after in (('fixture-other=29', 'fixture-other=31'),
                                  ('shell_timeout_s=7', 'shell_timeout_s=8')):
                with self.subTest(before=before, after=after):
                    _ = targets.write_text(target_values.replace(before, after))
                    self.assertEqual(hana_render.guest_digest(configuration_path, checkout=checkout), digest)
            with self.subTest('Change the effective command timeout'):
                _ = targets.write_text(target_values.replace('command_timeout_s.fixture-guest=23',
                    'command_timeout_s.fixture-guest=27'))
                changed = hana_render.guest_files(checkout, configuration_path, hana_render.DATA_DEVICE)
                self.assertNotEqual(hana_render.digest_files(changed), digest)
                rendered_targets = next(file.content for file in changed
                    if file.path == Path('runtime/config/build_targets.conf'))
                self.assertIn(b'\nloopback.command_timeout_s=27\n', rendered_targets)
            _ = targets.write_text(target_values)
            _ = copies.write_text(copies_config.replace('copies\n', 'other-copies\n'))
            self.assertEqual(hana_render.guest_digest(configuration_path, checkout=checkout), digest)
            self.assertEqual(cast(dict[str, object], json.loads((output / 'safety.json').read_text()))['guest_digest'],
                digest.value)
            _ = configuration_path.write_text(original.replace('0.35', '0.40') + '# Sender comment\n')
            self.assertEqual(hana_render.guest_digest(configuration_path, checkout=checkout), digest)
            sender = checkout / 'scripts/aws_target/deploy.py'
            _ = sender.write_text('# Sender code\n')
            self.assertEqual(hana_render.guest_digest(configuration_path, checkout=checkout), digest)
            _ = configuration_path.write_text(original.replace('enabled=on', 'enabled=off'))
            self.assertNotEqual(hana_render.guest_digest(configuration_path, checkout=checkout), digest)
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
