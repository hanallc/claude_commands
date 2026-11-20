"""Test isolated Hana rendering with only the listed repository files."""
from __future__ import annotations

import ast
from collections.abc import Iterator
from importlib.util import resolve_name
import json
import os
from pathlib import Path
import re
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
RUNTIME_LIST = ROOT / 'scripts/aws_target/hana_render.files'
IMAGE_LIST = ROOT / 'scripts/aws_target/hana_image.files'
IMAGE_INPUTS = (
    'config/hana_aws.conf',
    'config/build_targets.conf',
    'config/build_folders.conf',
    'scripts/aws_target/hana_image.files',
    'scripts/aws_target/hana_render.py',
    'scripts/lib/conf.py',
    'scripts/lib/conf_reads.py',
)

# The guest does not ship these imports for host operations, tests, and type annotations.
UNSHIPPED_IMPORTS = {
    'build_queue/admission_client.py': {'accounts/__init__.py', 'accounts/outage_alerts.py',
        'agents/detached_runs.py', 'agents/worker_lifecycle.py',
        'message/send_budget.py', 'message/send_result.py', 'message/session_send.py', 'notify/push_texts.py'},
    'build_queue/slot_wait.py': {'hooks/checkpoint_gate.py', 'lib/shell_test_support.py'},
    'build_queue/step_estimator.py': {'lint/tmp_sweep.py'},
    'buildlog/record.py': {'aws_target/config.py', 'direct/step_record.py'},
    'buildlog/step_context.py': {'lib/cargo_target_config.py'},
    'copies/copies.py': {'lint/tmp_sweep.py', 'agents/remote.py', 'accounts/__init__.py', 'accounts/accounts.py'},
    'direct/local_release_wait.py': {'build_targets/build_targets.py', 'build_targets/targets.py', 'build_hold/build_hold.py'},
    'lib/build_folders.py': {'lib/descendants.py'},
    'lib/build_steps.py': {'build_hold/build_hold.py', 'production/land_runs.py'},
    'lib/scratch.py': {'direct/phase_table.py', 'production/proposals_table.py', 'production/unit_lookup.py',
        'production/production_doc.py', 'direct/plan_history.py', 'accounts/__init__.py', 'accounts/accounts.py'},
    'lib/unit_runs.py': {'direct/worker_state.py'},
    'lint/memory_admit.py': {'message/unit_labels.py'},
    'mac_test/claim_state.py': {'buildlog/transfer_bytes.py'},
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
            runtime_entries = RUNTIME_LIST.read_text().splitlines()
            image_entries = IMAGE_LIST.read_text().splitlines()
            image_inputs = checkout / 'image-inputs'
            for entries in (runtime_entries, image_entries):
                self.assertEqual(len(entries), len(set(entries)))
                for entry in entries:
                    with self.subTest(entry=entry):
                        relative = Path(entry)
                        self.assertFalse(relative.is_absolute())
                        self.assertNotIn('..', relative.parts)
            for root, entries in ((image_inputs, (*image_entries, *IMAGE_INPUTS)),
                                  (checkout, (*runtime_entries, 'config/copies.conf'))):
                for entry in entries:
                    destination = root / entry
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    if entry.startswith('config/'):
                        _ = destination.write_text('')
                    else:
                        _ = shutil.copyfile(ROOT / entry, destination)
            _ = shutil.copyfile(RUNTIME_LIST, checkout / RUNTIME_LIST.relative_to(ROOT))
            targets = image_inputs / 'config/build_targets.conf'
            target_values = ('loopback.shell_timeout_s=7\nloopback.shell_timeout_s.fixture-guest=11\n'
                + '# range: 1..\nloopback.command_timeout_s.fixture-guest=23\n'
                + 'loopback.command_timeout_s.fixture-other=29\nsync.rsync_timeout_s=60\n')
            _ = targets.write_text(target_values)
            folders = image_inputs / 'config/build_folders.conf'
            folder_values = ('# range: 0.001..60\nlocks.probe_wait_seconds=0.25\n'
                + '# range: 0.001..60\nlocks.probe_wait_seconds.fixture-guest=0.4\n'
                + '# range: 0.001..1\nlocks.probe_retry_seconds=0.005\n'
                + 'patterns.tool={purpose}\npatterns.tool.fixture-guest={purpose}{suffix}\n'
                + 'roots.slots.fixture-guest=/fixture/slots\n'
                + 'slots.fixture-guest.hana.max=3\nslots.fixture-guest.hana.max_bytes=1000\n')
            _ = folders.write_text(folder_values)
            (checkout / 'config').mkdir(exist_ok=True)
            for path in (targets, folders):
                _ = shutil.copyfile(path, checkout / 'config' / path.name)
            copies = checkout / 'config/copies.conf'
            copies_config = ('root=/tmp/copies\nroot.aws-hana=/tmp/guest-copies\n'
                + 'remote_registry=/tmp/default-mirrors\nremote_registry.fixture-guest=/tmp/mirrors\n')
            _ = copies.write_text(copies_config)
            configuration_path = image_inputs / 'config/hana_aws.conf'
            _ = configuration_path.write_text(
                'host=fixture-guest\nenabled=on\n# whole: 1..\nguard_grace_seconds=60\n'
                + '# whole: 1..\nwatchdog_check_seconds=1\n'
                + '# whole: 1..\npermit_max_seconds=1800\n# whole: 1..\nstep_cpu_limit=4\n'
                + '# whole: 1..\nstep_memory_gib=24\n# whole: 1..\nmount_timeout_seconds=15\n'
                + '# whole: 1..\naws_timeout_s=10\n# whole: 1..\ncontrol_poll_seconds=2\n'
                + '# whole: 1..\nresult_transfer_timeout_s=120\n'
                + '# whole: 1..\nresult_transfer_timeout_s.fixture-guest=83\n'
                + 'execution_shell=default\n# whole: 1..\nfree_floor_gib=30\n'
                + '# whole: 1..\nsource_max_bytes=1048576\n# whole: 1..\nsource_download_timeout_s=60\n'
                + '# whole: 1..\nsource_git_timeout_s=60\n'
                + 'source_allowed_environment=RUST_BACKTRACE\nspot_max_usd_per_hour=0.35\n')
            listed = {Path(entry) for entry in runtime_entries}
            missing = {
                f'{source} imports {dependency}'
                for source in listed if source.suffix == '.py'
                for dependency in imported_source_paths(source)
                if dependency not in listed
                and str(dependency.relative_to('scripts')) not in UNSHIPPED_IMPORTS.get(
                    str(source.relative_to('scripts')), ())
            }
            self.assertEqual(sorted(missing), [], 'The guest manifest lacks imported modules')
            result = subprocess.run([sys.executable, '-EB', '-m', 'scripts.aws_target.hana_render',
                                     '--device', '/dev/sdf', 'rendered'], cwd=image_inputs,
                                    capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            output = image_inputs / 'rendered'
            expected_modes = {
                'safety.py': 0o600, 'safety.json': 0o600, 'receiver.py': 0o600, 'receiver.json': 0o600,
                'hana-guard.service': 0o644, 'hana-watchdog.service': 0o644,
                'var-lib-hana.mount': 0o644, 'hana-work.slice': 0o644, 'hana-receiver.service': 0o644,
            }
            self.assertEqual({path.name for path in output.iterdir()}, {*expected_modes, 'runtime'})
            configuration = conf.read(configuration_path)
            with mock.patch('socket.gethostname', return_value='fixture-guest'):
                files = (*hana_guest.render_files(hana_guest.read_settings(configuration), device=Path('/dev/sdf')),
                         *hana_receiver.render_files(hana_receiver.read_settings(configuration, command_timeout_s=23.0)))
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
            image_runtime = output / 'runtime'
            self.assertEqual({str(path.relative_to(image_runtime)) for path in image_runtime.rglob('*')
                              if path.is_file()}, {*image_entries, 'config/build_folders.conf'})
            for path in (*image_entries, 'config/build_folders.conf'):
                self.assertEqual(stat.S_IMODE((image_runtime / path).stat().st_mode), 0o644)
            self.assertEqual((image_runtime / 'config/build_folders.conf').read_text(),
                '# range: 0.001..60\nlocks.probe_wait_seconds=0.4\n# range: 0.001..1\nlocks.probe_retry_seconds=0.005\n')
            receiver = cast(dict[str, object], json.loads((output / 'receiver.json').read_text()))
            self.assertEqual(receiver['result_transfer_timeout_s'], 83)
            self.assertEqual(receiver['command_timeout_s'], 23)
            self.assertNotIn('Environment=CONF_DIR=', (output / 'hana-receiver.service').read_text())
            imported = subprocess.run([sys.executable, '-IB', '-c',
                'import runpy, sys; sys.path.insert(0, sys.argv[1]); '
                + 'runpy.run_path("safety.py"); runpy.run_path("receiver.py"); '
                + 'from scripts.aws_target import hana_payload; '
                + 'assert not any(name in sys.modules for name in '
                + '("scripts.lib.conf", "scripts.aws_target.hana_runner", "scripts.aws_target.hana_execution", '
                + '"scripts.aws_target.aws_cli", "scripts.aws_target.hana_provider", "scripts.aws_target.hana_box"))',
                str(image_runtime)], cwd=output, capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(imported.returncode, 0, imported.stdout + imported.stderr)
            entry = checkout / hana_render.RUNNER_ENTRY
            with entry.open('a') as script:
                _ = script.write('\n# Entry point from the fixture checkout.\n')
            runtime = checkout / 'run'
            for file in hana_render.runtime_files(checkout, configuration_path):
                destination = runtime / file.path
                destination.parent.mkdir(parents=True, exist_ok=True)
                _ = destination.write_bytes(file.content)
                destination.chmod(file.mode)
            self.assertEqual((runtime / 'runner.py').read_bytes(), entry.read_bytes())
            with self.subTest('Render and read an override'):
                self.assertEqual((runtime / 'config/build_targets.conf').read_text(), 'loopback.shell_timeout_s=11\n')
                compiler = checkout / 'compiler-version'
                _ = compiler.write_text('1.99.9\n')
                with (mock.patch.dict(os.environ, {'CONF_DIR': str(runtime / 'config')}),
                      mock.patch('socket.gethostname', return_value='fixture-guest'),
                      mock.patch.object(hana_runner, 'IMAGE_COMPILER_VERSION', compiler)):
                    settings = hana_runner.read_settings(conf.read(runtime / 'config/hana_aws.conf'), 23)
                self.assertEqual((settings.shell_timeout_s, settings.command_timeout_s), (11, 23))
            self.assertEqual((runtime / 'config/copies.conf').read_text(), 'remote_registry=/tmp/mirrors\n')
            self.assertEqual((runtime / 'config/build_folders.conf').read_text(), 'patterns.tool={purpose}{suffix}\n')
            runtime_configuration = runtime / 'config/hana_aws.conf'
            self.assertEqual({key: setting.value for key, setting in conf.settings(runtime_configuration.read_text()).items()},
                {'execution_shell': 'default', 'free_floor_gib': '30', 'source_git_timeout_s': '60',
                 'source_allowed_environment': 'RUST_BACKTRACE'})
            for path, key, effective in (
                (configuration_path, 'result_transfer_timeout_s.fixture-guest', True),
                (configuration_path, 'result_transfer_timeout_s', False),
                (configuration_path, 'source_git_timeout_s', False),
                (targets, 'loopback.command_timeout_s.fixture-guest', True),
                (targets, 'loopback.command_timeout_s.fixture-other', False),
                (targets, 'loopback.shell_timeout_s.fixture-guest', False),
                (folders, 'locks.probe_wait_seconds.fixture-guest', True),
                (folders, 'locks.probe_wait_seconds', False),
                (folders, 'patterns.tool', False),
            ):
                with self.subTest(file=path.name, key=key):
                    self.assertEqual(hana_render.affects_guest_config(key, path), effective)
            original = configuration_path.read_text()
            for source in ('scripts/aws_target/hana_runner.py', 'config/copies.conf'):
                _ = shutil.copyfile(checkout / source, image_inputs / source)
            digest = hana_render.guest_digest(configuration_path, checkout=image_inputs)
            _ = targets.write_text(target_values.replace('sync.rsync_timeout_s=60', 'sync.rsync_timeout_s=90'))
            self.assertEqual(hana_render.guest_digest(configuration_path, checkout=image_inputs), digest)
            for before, after in (('fixture-other=29', 'fixture-other=31'),
                                  ('shell_timeout_s=7', 'shell_timeout_s=8'),
                                  ('shell_timeout_s.fixture-guest=11', 'shell_timeout_s.fixture-guest=13')):
                with self.subTest(before=before, after=after):
                    _ = targets.write_text(target_values.replace(before, after))
                    self.assertEqual(hana_render.guest_digest(configuration_path, checkout=image_inputs), digest)
            with self.subTest('Change the effective command timeout'):
                _ = targets.write_text(target_values.replace('command_timeout_s.fixture-guest=23',
                    'command_timeout_s.fixture-guest=27'))
                changed = hana_render.guest_files(image_inputs, configuration_path, hana_render.DATA_DEVICE)
                self.assertNotEqual(hana_render.digest_files(changed), digest)
                receiver_json = next(file.content for file in changed if file.path == Path('receiver.json'))
                self.assertEqual(cast(dict[str, object], json.loads(receiver_json))['command_timeout_s'], 27)
            _ = targets.write_text(target_values)
            _ = (image_inputs / 'config/copies.conf').write_text(
                copies_config.replace('copies\n', 'other-copies\n'))
            self.assertEqual(hana_render.guest_digest(configuration_path, checkout=image_inputs), digest)
            self.assertEqual(cast(dict[str, object], json.loads((output / 'safety.json').read_text()))['guest_digest'],
                digest.value)
            for before, after in (('0.35', '0.40'), ('execution_shell=default', 'execution_shell=ci'),
                                  ('source_git_timeout_s=60', 'source_git_timeout_s=80')):
                with self.subTest(before=before, after=after):
                    _ = configuration_path.write_text(original.replace(before, after) + '# Sender comment\n')
                    self.assertEqual(hana_render.guest_digest(configuration_path, checkout=image_inputs), digest)
            for path in ('scripts/aws_target/deploy.py', 'scripts/aws_target/hana_runner.py'):
                with self.subTest(path=path):
                    source = image_inputs / path
                    _ = source.write_text('# Code outside the image closure\n')
                    self.assertEqual(hana_render.guest_digest(configuration_path, checkout=image_inputs), digest)
            _ = folders.write_text(folder_values.replace('fixture-guest=0.4', 'fixture-guest=0.5'))
            self.assertNotEqual(hana_render.guest_digest(configuration_path, checkout=image_inputs), digest)
            for key, value, invalid in (
                ('locks.probe_wait_seconds.fixture-guest', '0.4', '61'),
                ('locks.probe_retry_seconds', '0.005', 'broken'),
                ('locks.probe_retry_seconds', '0.005', 'nan'),
            ):
                with self.subTest(key=key, invalid=invalid):
                    _ = folders.write_text(folder_values.replace(f'{key}={value}', f'{key}={invalid}'))
                    with self.assertRaisesRegex(ValueError, re.escape(key.removesuffix('.fixture-guest'))):
                        _ = hana_render.guest_digest(configuration_path, checkout=image_inputs)
            _ = folders.write_text(folder_values)
            with self.subTest('Change the effective result transfer timeout'):
                _ = configuration_path.write_text(original.replace(
                    'result_transfer_timeout_s.fixture-guest=83', 'result_transfer_timeout_s.fixture-guest=89'))
                self.assertNotEqual(hana_render.guest_digest(configuration_path, checkout=image_inputs), digest)
            _ = configuration_path.write_text(original.replace('enabled=on', 'enabled=off'))
            self.assertNotEqual(hana_render.guest_digest(configuration_path, checkout=image_inputs), digest)
            self.assertEqual({str(path.relative_to(runtime)) for path in runtime.rglob('*') if path.is_file()},
                             ({*runtime_entries} - {str(hana_render.RUNNER_ENTRY)}) | {
                                 'runner.py', 'config/hana_aws.conf', 'config/build_targets.conf',
                                 'config/build_folders.conf', 'config/copies.conf'})
            imported = subprocess.run([sys.executable, '-IB', '-c',
                'import sys; sys.path.insert(0, sys.argv[1]); '
                + 'from scripts.aws_target import hana_runner, hana_protocol; '
                + 'from scripts.build_queue import slot_wait; '
                + 'from scripts.lib import build_slots, git_rescue; '
                + 'assert not any(name in sys.modules for name in '
                + '("scripts.aws_target.aws_cli", "scripts.aws_target.hana_provider", "scripts.aws_target.hana_box", '
                + '"scripts.aws_target.hana_placement", "scripts.buildlog.transfer_bytes"))',
                str(runtime)], cwd=runtime, capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(imported.returncode, 0, imported.stdout + imported.stderr)
            launcher = subprocess.run([sys.executable, '-IB', 'runner.py', '--help'], cwd=runtime,
                                      capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(launcher.returncode, 0, launcher.stdout + launcher.stderr)
            self.assertIn('--command-timeout-s', launcher.stdout)
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
