"""Check the saved gate probe format and its local status command."""
from contextlib import redirect_stdout
from datetime import UTC, datetime
from io import StringIO
import json
from pathlib import Path
from typing import ClassVar
import sys
import tempfile
import unittest
from unittest import mock

from scripts.aws_target import admin_secrets, deploy, gate_probe


class GateProbeTests(unittest.TestCase):
    RECORD: ClassVar[gate_probe.GateProbeRecord] = gate_probe.GateProbeRecord('fixture-deployment', datetime(2026, 1, 1, tzinfo=UTC), (
        gate_probe.ProbeCheck(gate_probe.RegionalCheck(gate_probe.RegionalCheckName.GET_RESULT, 'us-east-2'),
                              gate_probe.ProbeVerdict.PASS, gate_probe.ProbeReason.OK),
        gate_probe.ProbeCheck(gate_probe.GpuLaunchCheck('us-east-2', 'g6.2xlarge'),
                              gate_probe.ProbeVerdict.FAIL, gate_probe.ProbeReason.CHECK_FAILED),
        gate_probe.ProbeCheck(gate_probe.GpuLaunchCheck('us-east-2', 'g4dn.2xlarge'),
                              gate_probe.ProbeVerdict.UNVERIFIED, gate_probe.ProbeReason.NO_OFFERED_SUBNET),
    ))

    @staticmethod
    def write_deployment(directory: Path, identity: str) -> None:
        _ = (directory / 'gate-deployment.json').write_text(json.dumps({
            'deployment_id': identity,
            'regions': [{'region': 'us-east-2'}],
            'gpu_instance_types': ['g6.2xlarge', 'g4dn.2xlarge'],
        }))

    def status(self, directory: Path) -> str:
        argv = ['deploy.py', 'probe-status', '--directory', str(directory)]
        with (mock.patch.object(sys, 'argv', argv),
              mock.patch.object(admin_secrets, 'credentials_file', side_effect=AssertionError('Status read credentials')),
              mock.patch.object(deploy, 'Deployment', side_effect=AssertionError('Status created a deployment')),
              redirect_stdout(StringIO()) as output):
            self.assertEqual(deploy.main(), 0)
        return output.getvalue()

    def test_probe_status_prints_saved_rows_and_age_without_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            self.write_deployment(directory, self.RECORD.deployment_id)
            gate_probe.save_record(directory, self.RECORD)
            self.assertEqual(gate_probe.read_record(directory), self.RECORD)
            lines = self.status(directory).splitlines()
        self.assertRegex(lines[0], r'^probe-status: record age \d+ s$')
        self.assertEqual(lines[1:], [
            'gate-setup probe: get-object: pass (OK)',
            'gate-setup probe: run-instances g6.2xlarge: fail (check-failed)',
            'gate-setup probe: run-instances g4dn.2xlarge unverified. No saved subnet offers this type.',
        ])

    def test_reader_reports_each_unavailable_state_without_verdicts(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            path = directory / gate_probe.FILE_NAME
            unavailable = gate_probe.ProbeRecordUnavailable
            cases: tuple[tuple[str, gate_probe.ProbeRecordUnavailable, str], ...] = (
                ('{', unavailable.CORRUPT, 'corrupt probe record'),
                ('{"version": 1, "checks": 7}', unavailable.CORRUPT, 'corrupt probe record'),
                ('{"version": 0}', unavailable.UNKNOWN_VERSION, 'unknown probe record version'),
            )
            self.assertIs(gate_probe.read_record(directory), unavailable.ABSENT)
            self.assertEqual(self.status(directory), 'no probe record\n')
            for text, state, message in cases:
                with self.subTest(state=state):
                    _ = path.write_text(text)
                    self.assertIs(gate_probe.read_record(directory), state)
                    self.assertEqual(self.status(directory), message + '\n')
            gate_probe.save_record(directory, self.RECORD)
            self.assertIs(gate_probe.read_record(directory), unavailable.NO_DEPLOYMENT)
            self.assertEqual(self.status(directory), 'no readable gate deployment record\n')
            self.write_deployment(directory, 'other-deployment')
            self.assertIs(gate_probe.read_record(directory), unavailable.OTHER_DEPLOYMENT)
            self.assertEqual(self.status(directory), 'probe record is from another deployment\n')

    def test_reader_rejects_untrusted_check_names_and_incomplete_details(self) -> None:
        cases = (
            {'name': 'private-check'},
            {'name': 'termination'},
            {'name': 'run-instances', 'region': 'us-east-2'},
            {'name': 'run-instances', 'region': 'us-west-2', 'instance_type': 'g6.2xlarge'},
            {'name': 'run-instances', 'region': 'us-east-2', 'instance_type': 'private.instance'},
            {'name': 'termination', 'region': 'us-east-2', 'instance_type': 'g6.2xlarge'},
        )
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            self.write_deployment(directory, self.RECORD.deployment_id)
            path = directory / gate_probe.FILE_NAME
            for check in cases:
                with self.subTest(check=check):
                    _ = path.write_text(json.dumps({'version': 1, 'deployment_id': self.RECORD.deployment_id,
                        'ended_at': self.RECORD.ended_at.isoformat(),
                        'checks': [dict(check, verdict='pass', reason='ok')]}))
                    self.assertIs(gate_probe.read_record(directory), gate_probe.ProbeRecordUnavailable.CORRUPT)
                    self.assertEqual(self.status(directory), 'corrupt probe record\n')
