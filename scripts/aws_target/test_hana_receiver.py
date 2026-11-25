"""Check command admission and the runtime payload in a fixture."""
from __future__ import annotations

from dataclasses import asdict, replace
import hashlib
from io import BytesIO
import json
import os
import shutil
import socket
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
from typing import cast, override
import unittest
import uuid
from unittest import mock

from scripts.aws_target import hana_guest, hana_payload, hana_protocol, hana_receiver
from scripts.aws_target.hana_generation import HanaHostGeneration
from scripts.aws_target.hana_result import HanaAttemptKey, LogicalStepLink
from scripts.build_queue import execution_identity


def request_for(payload: bytes) -> hana_protocol.HanaExecutionRequest:
    command = ('cargo', 'check')
    manifest = hana_protocol.SourceManifest(1, 'hana-fixture', '0' * 40, hashlib.sha256(payload).hexdigest(),
        'https://fixture.s3.us-east-2.amazonaws.com/source', (), command, (), (), '0' * 64,
        runtime_abi=hana_protocol.RUNTIME_ABI)
    key = HanaAttemptKey('episode', 'attempt', execution_identity.assigned_claim_id('episode', 'attempt'),
                         HanaHostGeneration('deployment', 'start', 'boot'))
    return hana_protocol.HanaExecutionRequest(key, manifest, 1, execution_identity.SourceKeyKnown('fixture-source'),
                                              command, LogicalStepLink('call', 'episode'))


def archive_bytes(*files: tuple[str, bytes, bytes]) -> bytes:
    output = BytesIO()
    with tarfile.open(fileobj=output, mode='w', format=tarfile.USTAR_FORMAT) as archive:
        for name, data, kind in files:
            entry = tarfile.TarInfo(name)
            entry.type = kind
            entry.size = len(data)
            if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE):
                entry.linkname = 'runtime/runner.py'
            archive.addfile(entry, BytesIO(data))
    return output.getvalue()


class MountedSupervisor(hana_receiver.GuestCommandSupervisor):
    @override
    def mounted(self) -> bool:
        return True



class HanaPermitReceiverTests(unittest.TestCase):
    def test_renewal_replays_only_after_the_atomic_replace_commits(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = hana_guest.GuestPaths(root, root / 'boot')
            _ = paths.boot_id.write_text('boot')
            settings = hana_guest.SafetySettings(True, 10, 1, 180, 2, 2, 10)
            descriptor: dict[str, object] = {'version': 1, 'deployment': 'deployment', 'start_token': 'start',
                                             'expires': 1200}
            original = hana_guest.WorkPermit(1050, '1', 'boot')
            renewal = hana_guest.WorkPermit(1100, '2', 'boot')
            first: dict[str, object] = {'deployment': 'deployment', 'start_token': 'start', 'token': 'initial',
                'execution': 'fixture', 'permit': asdict(original), 'expected_generation': 0}
            next_permit = {**first, 'token': 'renewal', 'permit': asdict(renewal), 'expected_generation': 1}
            write = os.replace

            def refuse_replace(source: str | os.PathLike[str], destination: str | os.PathLike[str]) -> None:
                self.assertEqual(Path(destination), paths.permit)
                self.assertEqual(json.loads(Path(source).read_text()), asdict(renewal))
                raise OSError('fixture rename failure')

            _ = hana_receiver.receive(settings, descriptor, first, paths=paths, clock=lambda: 1000)
            with mock.patch.object(os, 'replace', side_effect=refuse_replace):
                with self.assertRaisesRegex(OSError, 'fixture rename failure'):
                    _ = hana_receiver.receive(settings, descriptor, next_permit, paths=paths, clock=lambda: 1000)
            self.assertEqual(hana_guest.read_permit(paths.permit), original)
            with mock.patch.object(os, 'replace', wraps=write) as rename:
                reply = hana_receiver.receive(settings, descriptor, next_permit, paths=paths, clock=lambda: 1000)
                self.assertEqual(reply['generation'], 2)
                self.assertEqual(hana_receiver.receive(
                    settings, descriptor, next_permit, paths=paths, clock=lambda: 1000), reply)
                self.assertEqual(rename.call_count, 1)
            with hana_guest.guest_lock(paths):
                self.assertEqual(hana_guest.bound_permit(settings, paths, clock=lambda: 1000, uptime=lambda: 5),
                                 hana_guest.PermitDeadline(renewal, 105))
            with self.assertRaisesRegex(ValueError, 'generation changed'):
                _ = hana_receiver.receive(settings, descriptor, first, paths=paths, clock=lambda: 1000)


class HanaReceiverSettingsTests(unittest.TestCase):
    def test_source_supervision_uses_the_shared_deadline_without_a_compiler(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            policy = directory / 'policy.conf'
            write_policy(policy, directory / 'copies')
            child = mock.Mock(pid=os.getpid())
            settings = hana_receiver.ReceiverSettings(10, 1, 83, 1048576, 120, 23)
            with mock.patch.object(subprocess, 'Popen', return_value=child) as launch:
                _ = hana_receiver.GuestCommandSupervisor(settings, copy_policy=policy).start_source_update(
                    request_for(b''), directory, ['systemd-run', '--uid=hana-runner', '--', *hana_receiver.BOOTSTRAP_COMMAND])
            arguments = cast(list[str], launch.call_args.args[0])
            self.assertIn('--property=RuntimeMaxSec=23', arguments)
            self.assertIn('--property=EnvironmentFile=/etc/hana-aws/runner-environment', arguments)
            self.assertIn('--property=ExitType=main', arguments)
            self.assertNotIn('--property=ExitType=cgroup', arguments)
            self.assertIn('--uid=hana-runner', arguments)
            self.assertIn('--property=TemporaryFileSystem=/var/lib/hana/runner/rust/hana/.git/hana-payload:'
                          + 'mode=1777,nodev,nosuid,size=1048576', arguments)
            self.assertEqual(arguments[arguments.index('--') + 1:],
                             [*hana_receiver.BOOTSTRAP_COMMAND, '1048576', '120', '23'])
            self.assertEqual(json.loads((directory / 'input').read_bytes()),
                             {'action': 'source-update', 'output_max_bytes': hana_protocol.CONTROL_MAX_BYTES,
                              'request': json.loads(hana_protocol.encode_request(request_for(b'')))})

    def test_invalid_runtime_abi_or_policy_refuses_before_launch(self) -> None:
        requested = hana_protocol.RUNTIME_ABI + 1
        previous = hana_protocol.RUNTIME_ABI - 1
        for abi, code, reason in (
            (requested, hana_guest.CommandAdmissionRefusalCode.RUNTIME_ABI_MISMATCH,
             f'The Hana runtime ABI {requested} differs from the image ABI {hana_protocol.RUNTIME_ABI}'),
            (previous, hana_guest.CommandAdmissionRefusalCode.RUNTIME_ABI_MISMATCH,
             f'The Hana runtime ABI {previous} differs from the image ABI {hana_protocol.RUNTIME_ABI}'),
            (hana_protocol.RUNTIME_ABI, hana_guest.CommandAdmissionRefusalCode.PRE_COMMAND_FAILED,
             'The image copy policy is invalid'),
        ):
            with self.subTest(abi=abi), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                paths = hana_guest.GuestPaths(root, root / 'boot')
                _ = paths.boot_id.write_text('boot')
                permit = hana_guest.WorkPermit(1100, '1', 'boot')
                settings = hana_guest.SafetySettings(True, 10, 1, 180, 2, 2, 10)
                hana_guest.write_permit(settings, permit, expected_generation=0, paths=paths, clock=lambda: 1000)
                request = request_for(b'')
                request = replace(request, manifest=replace(request.manifest, runtime_abi=abi))
                prefix = 'https://fixture.s3.us-east-2.amazonaws.com/'
                access = hana_protocol.HanaExecutionAccess(prefix + 'observation', prefix + 'result', 1200)
                envelope = hana_protocol.HanaCommandEnvelope(request, permit, access)
                policy = root / 'policy.conf'
                _ = policy.write_text('locks.probe_retry_seconds=invalid\n')
                supervisor = MountedSupervisor(hana_receiver.ReceiverSettings(10, 1, 83, 1048576, 120, 23),
                                              copy_policy=policy)
                with mock.patch.object(subprocess, 'Popen') as launch:
                    observation = hana_receiver.receive_execution(settings,
                        {'deployment': 'deployment', 'start_token': 'start'}, envelope, supervisor,
                        paths=paths, clock=lambda: 1000, uptime=lambda: 50)
                launch.assert_not_called()
                reported = hana_protocol.decode_observation(hana_protocol.encode_observation(observation))
                self.assertTrue(reported.terminated and reported.lease_free)
                assert isinstance(reported, hana_protocol.ConfirmedCommandAdmissionRefusal)
                self.assertEqual(reported.refusal_code, code)
                self.assertEqual(reported.reason, reason)

    def test_archive_refuses_unsafe_members_and_a_different_abi(self) -> None:
        runner = ('runtime/runner.py', b'fixture', tarfile.REGTYPE)
        bundle = ('source.bundle', b'fixture bundle', tarfile.REGTYPE)
        abi = ('runtime/abi', f'{hana_protocol.RUNTIME_ABI}\n'.encode(), tarfile.REGTYPE)
        for label, marker, extra in (
            ('duplicate', abi, runner),
            ('traversal', abi, ('runtime/../outside', b'bad', tarfile.REGTYPE)),
            ('absolute', abi, ('/outside', b'bad', tarfile.REGTYPE)),
            ('link', abi, ('runtime/link', b'', tarfile.SYMTYPE)),
            ('hard link', abi, ('runtime/link', b'', tarfile.LNKTYPE)),
            ('special', abi, ('runtime/pipe', b'', tarfile.FIFOTYPE)),
            ('other root', abi, ('unrelated', b'bad', tarfile.REGTYPE)),
            ('ABI mismatch', ('runtime/abi', b'99\n', tarfile.REGTYPE), ('runtime/config/fixture', b'', tarfile.REGTYPE)),
        ):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temporary:
                payload = archive_bytes(bundle, runner, marker, extra)
                with self.assertRaises(ValueError):
                    _ = hana_payload.unpack_payload(BytesIO(payload), request_for(payload).manifest, Path(temporary) / 'runtime')

    def test_bootstrap_gives_the_runner_the_verified_source_and_image_copy_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundle = b'fixture source bundle'
            runner = b"""import os
from pathlib import Path
import sys

args = dict(zip(sys.argv[1::2], sys.argv[2::2], strict=True))
source = os.pread(int(args['--bundle-fd']), int(args['--bundle-size']), int(args['--bundle-offset']))
assert source == b'fixture source bundle'
assert Path(__file__).is_relative_to(Path.home() / 'rust/hana/.git/hana-payload')
assert sys.stdin.buffer.read() == Path(os.environ['FIXTURE_INPUT']).read_bytes()
policy = (Path(os.environ['CONF_DIR']) / 'build_folders.conf').read_text()
assert 'roots.build=/fixture/build' in policy
assert '# whole: 1..\\nhana.copies.max=1\\n' in policy
assert '# whole: 1..\\nhana.copies.max_bytes=1073741824\\n' in policy
assert 'hana.copies.max.aws-hana' not in policy
assert '# range: 0..\\nlocks.probe_wait_seconds=0\\n' in policy
assert '/bad' not in policy
assert 'patterns.tool=fixture' in policy
print(args['--command-timeout-s'])
"""
            payload = archive_bytes(('source.bundle', bundle, tarfile.REGTYPE),
                ('runtime/runner.py', runner, tarfile.REGTYPE),
                ('runtime/abi', f'{hana_protocol.RUNTIME_ABI}\n'.encode(), tarfile.REGTYPE),
                ('runtime/config/build_folders.conf',
                 b'patterns.tool=fixture\nroots.build=/bad\nroots.build.aws-hana=/bad\n'
                 + b'hana.copies.max.aws-hana=9\n',
                 tarfile.REGTYPE))
            write_policy(root / 'policy.conf', Path('/fixture/build'))
            for label, cause in (
                ('ready', ''),
                ('import failed', 'failed to start'),
                ('archive failed', 'archive is invalid'),
                ('wrong digest', 'payload sha256 differs'),
                ('download failed', 'download failed'),
            ):
                with self.subTest(label=label):
                    if label == 'import failed':
                        payload = archive_bytes(('source.bundle', bundle, tarfile.REGTYPE),
                            ('runtime/runner.py', b'import missing_hana_fixture_module\n', tarfile.REGTYPE),
                            ('runtime/abi', f'{hana_protocol.RUNTIME_ABI}\n'.encode(), tarfile.REGTYPE),
                            ('runtime/config/build_folders.conf', b'patterns.tool=fixture\n', tarfile.REGTYPE))
                    if label in ('archive failed', 'wrong digest'):
                        payload = b'not a tar archive'
                    request = request_for(b'different archive' if label == 'wrong digest' else payload)
                    command, content, env = bootstrap_fixture(root, payload, request)
                    if label == 'download failed':
                        (root / 'payload.tar').unlink()
                    completed = subprocess.run(command, input=content, capture_output=True, timeout=10,
                                              check=False, env=env)
                    self.assertEqual(completed.returncode, 0, completed.stderr.decode())
                    if cause:
                        outcome = hana_protocol.decode_outcome(completed.stdout)
                        assert isinstance(outcome, hana_protocol.HanaExecutionRefused)
                        self.assertIn(cause, outcome.reason)
                    else:
                        self.assertEqual(completed.stdout, b'23.5\n', completed.stderr.decode())


    def test_capture_bounds_both_streams_before_writing_the_journal(self) -> None:
        bash = shutil.which('bash')
        assert bash is not None
        command = [bash, *hana_receiver.CAPTURE_COMMAND[1:], '256', sys.executable, '-c',
                   "import sys; sys.stdout.write('a' * 1000000); sys.stderr.write('b' * 1000000)"]
        completed = subprocess.run(command, capture_output=True, timeout=10, check=True)
        self.assertEqual(completed.stdout, b'a' * 256)
        self.assertEqual(completed.stderr, b'b' * 8192)

    def test_command_unit_removes_the_runtime_after_a_hard_kill(self) -> None:
        if sys.platform != 'linux' or not shutil.which('systemd-run'):
            self.skipTest('A Linux systemd user manager is required')
        manager = subprocess.run(['systemctl', '--user', 'show-environment'], capture_output=True,
                                 timeout=5, check=False)
        if manager.returncode != 0:
            self.skipTest('A Linux systemd user manager is required')
        with tempfile.TemporaryDirectory() as temporary, socket.socket(socket.AF_UNIX) as listener:
            root = Path(temporary)
            home = root / 'home'
            mount = hana_payload.payload_mount(home)
            write_policy(root / 'policy.conf', root / 'copies')
            address = root / 'ready.sock'
            listener.bind(str(address))
            listener.listen()
            listener.settimeout(10)
            runner = f"""import socket
from pathlib import Path

with socket.socket(socket.AF_UNIX) as connection:
    connection.connect({str(address)!r})
    connection.sendall(str(Path(__file__).parent).encode())
    connection.recv(1)
""".encode()
            payload = archive_bytes(('source.bundle', b'fixture bundle', tarfile.REGTYPE),
                ('runtime/runner.py', runner, tarfile.REGTYPE),
                ('runtime/abi', f'{hana_protocol.RUNTIME_ABI}\n'.encode(), tarfile.REGTYPE),
                ('runtime/config/build_folders.conf', b'patterns.tool=fixture\n', tarfile.REGTYPE))
            bootstrap, _, _ = bootstrap_fixture(root, payload, request_for(payload))
            launch = ['systemd-run', '--user', '--wait', '--collect', '--property=PrivateUsers=yes',
                      '--property=KillSignal=SIGKILL',
                      f'--setenv=PATH={root}:{os.environ["PATH"]}',
                      f'--setenv=FIXTURE_ARCHIVE={root / "payload.tar"}', '--', *bootstrap]
            command_dir = root / uuid.uuid4().hex
            command_dir.mkdir()
            unit = hana_receiver.command_unit(command_dir)
            stop = ['systemctl', '--user', 'stop', unit]
            self.addCleanup(subprocess.run, stop, capture_output=True, timeout=10, check=False)
            settings = hana_receiver.ReceiverSettings(10, 1, 83, 1048576, 120, 23)
            supervisor = hana_receiver.GuestCommandSupervisor(settings, copy_policy=root / 'policy.conf')
            bash = shutil.which('bash')
            assert bash is not None
            environment_path = root / 'environment'
            _ = environment_path.write_text(f'HOME={home}\n')
            with (mock.patch.object(hana_guest, 'RUNNER_HOME', home),
                  mock.patch.object(hana_receiver, 'RUNNER_ENVIRONMENT', environment_path),
                  mock.patch.object(hana_receiver, 'CAPTURE_COMMAND', (bash, *hana_receiver.CAPTURE_COMMAND[1:]))):
                refusal = supervisor.start_source_update(request_for(payload), command_dir, launch)
            self.assertIsNone(refusal)
            socket_connection = listener.accept()[0]
            with socket_connection:
                socket_connection.settimeout(10)
                runtime = Path(socket_connection.recv(4096).decode())
                self.assertTrue(runtime.is_relative_to(mount))
                _ = subprocess.run(stop, capture_output=True, timeout=10, check=True)
                self.assertEqual(socket_connection.recv(1), b'')
            _ = supervisor.children[command_dir].communicate(timeout=10)
            self.assertFalse(runtime.exists())
            self.assertEqual(list(mount.iterdir()), [])


def write_policy(path: Path, build_root: Path) -> None:
    _ = path.write_text(f'roots.build={build_root}\n# whole: 1..\nhana.copies.max=1\n'
        + '# whole: 1..\nhana.copies.max_bytes=1073741824\n'
        + '# range: 0..\nlocks.probe_wait_seconds=0\n# range: 0..\nlocks.probe_retry_seconds=0.005\n')


def bootstrap_fixture(root: Path, payload: bytes, request: hana_protocol.HanaExecutionRequest,
                      *, action: str = 'execute') -> tuple[list[str], bytes, dict[str, str]]:
    _ = (root / 'payload.tar').write_bytes(payload)
    curl = root / 'curl'
    _ = curl.write_text('#!' + sys.executable + '\nimport os, sys\nfrom pathlib import Path\n'
        + "sys.stdout.buffer.write(Path(os.environ['FIXTURE_ARCHIVE']).read_bytes())\n")
    curl.chmod(0o700)
    content = hana_protocol.encode_document({'action': action, 'output_max_bytes': 32,
                                             'request': hana_protocol.request_fields(request)})
    _ = (root / 'input').write_bytes(content)
    home = root / 'home'
    hana_payload.payload_mount(home).mkdir(parents=True, exist_ok=True)
    command = [sys.executable, '-c', '''
from pathlib import Path
import sys
sys.path.insert(0, sys.argv[2])
from scripts.aws_target import hana_payload

root = Path(sys.argv[1])
hana_payload.IMAGE_COPY_POLICY = root / 'policy.conf'
raise SystemExit(hana_payload.main(1048576, 7, 23.5))
''', str(root), str(Path(__file__).resolve().parents[2])]
    env = {**os.environ, 'HOME': str(home), 'PATH': str(root) + ':' + os.environ['PATH'],
           'FIXTURE_INPUT': str(root / 'input'),
           'FIXTURE_ARCHIVE': str(root / 'payload.tar')}
    return command, content, env


if __name__ == '__main__':
    _ = unittest.main()
