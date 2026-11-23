"""Prove that no unattended entry point calls AWS while both AWS machines are off."""
from __future__ import annotations

from collections.abc import Sequence
from contextlib import redirect_stderr
from io import StringIO
import subprocess
from typing import override
from unittest import mock

from scripts.aws_target import aws_target, cloud_gate, hana_box, hana_placement_adapter, ledger
from scripts.aws_target.hana_placement import HanaStepRefused
from scripts.aws_target.ledger_test_support import CostFixture, git_only, override_fixture_settings
from scripts.aws_target.test_hana_box import IDENTITY
from scripts.build_targets.execution_transport import HanaGpuNextestTransport
from scripts.production import gate


CLOUD_GATES_OFF = 'cloud gates are off (cloud_gate.conf enabled=off)'


class AwsOffTests(CostFixture):
    commands: list[str] = []

    @override
    def setUp(self) -> None:
        super().setUp()
        self.configure_gate('enabled=off')
        override_fixture_settings(self.root / 'hana_aws.conf', 'enabled=off')
        # A fake scaler file in the temporary home lets the placement-score reader pass its credential check.
        credentials = self.root / '.config/aws-build/credentials'
        credentials.parent.mkdir(parents=True)
        _ = credentials.write_text('[fixture]\naws_access_key_id = fixture\naws_secret_access_key = fixture\n')
        book = ledger.CostLedger(self.state)
        book.record_inventory(ledger.ResourceInventory(1, (), 'private inventory', True))
        book.observe(ledger.InstanceObservation(1, 'terminated', 'private account inventory'))
        self.commands = []
        # Record and refuse every process other than a git read; callers that catch the refusal still leave a record.
        _ = self.enterContext(mock.patch.object(subprocess, 'run', side_effect=self.git_only))

    def git_only(self, arguments: Sequence[str], **options: object) -> subprocess.CompletedProcess[str]:
        if arguments[0] != 'git':
            self.commands.append(arguments[0])
        return git_only(arguments, **options)

    def refuse_aws(self, arguments: Sequence[str], *, capture_output: bool, text: bool, timeout: float, check: bool,
                   env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        _ = capture_output, text, timeout, check, env
        self.commands.append(arguments[0])
        raise AssertionError('AWS call while AWS is off')

    def test_no_unattended_entry_point_calls_aws(self) -> None:
        workload = self.root / 'hana_aws.conf'
        hana_box.HanaController(self.state, self.settings, workload, hana_box.ProviderNotInstalled()).register(IDENTITY)
        service = mock.Mock(side_effect=AssertionError('A cloud gate service started while AWS is off'))
        with redirect_stderr(StringIO()):
            aws_target.AccountRefresh(self.settings, self.state, execute=self.refuse_aws).tick()
            self.assertEqual(hana_box.HanaController(self.state, self.settings, workload, hana_box.ProviderNotInstalled()).tick(),
                             hana_box.Lifecycle.IDLE_STOPPED)
            self.assertEqual(aws_target.controller_tick(self.settings, self.state, workload), 0)
            _ = aws_target.hourly(self.settings, self.state)
            outcome = cloud_gate.run(self.root, mode=gate.SuiteMode.GATE, full=False,
                                     module_time_limit_s=1800, service=service)
        self.assertEqual(outcome, cloud_gate.LocalFallback(CLOUD_GATES_OFF))
        service.assert_not_called()
        transport = HanaGpuNextestTransport(self.root / 'cloud_gate.conf', 'hana-gpu')
        self.assertEqual(hana_placement_adapter.gpu_setup(transport), HanaStepRefused(CLOUD_GATES_OFF))
        self.assertEqual(self.commands, [])

    def test_hourly_skips_the_hours_gap_check_while_aws_is_off(self) -> None:
        with (mock.patch.object(aws_target, 'snapshot', return_value=0) as prices,
              mock.patch.object(aws_target, 'hours_gap_notices', return_value=0) as hours):
            self.assertEqual(aws_target.hourly(self.settings, self.state), 0)
        prices.assert_not_called()
        hours.assert_not_called()

    def test_hourly_reads_no_placement_scores_while_the_cloud_gate_is_off(self) -> None:
        override_fixture_settings(self.root / 'hana_aws.conf', 'enabled=on')
        with (mock.patch.object(aws_target, 'snapshot', return_value=0) as prices,
              mock.patch.object(aws_target, 'hours_gap_notices', return_value=0) as hours,
              redirect_stderr(StringIO())):
            self.assertEqual(aws_target.hourly(self.settings, self.state), 0)
        prices.assert_called_once_with(self.settings, self.state)
        hours.assert_called_once_with(self.settings, self.state)
        self.assertEqual(self.commands, [])
