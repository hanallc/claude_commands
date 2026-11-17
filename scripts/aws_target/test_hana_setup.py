"""Verify preparation, explicit approval, and isolated stack setup with private fakes."""
from __future__ import annotations

from collections.abc import Sequence
from io import StringIO
import json
from pathlib import Path
import subprocess
from typing import cast, override
from unittest import mock

from scripts.aws_target import aws_cli, deploy, hana_box, hana_image, hana_spend, ledger, remote
from scripts.aws_target.control_test_support import TestClock
from scripts.aws_target.config import DeploymentSettings
from scripts.aws_target.ledger_test_support import CostFixture, text_setting
from scripts.aws_target.test_hana_provider import FakeAws, IDENTITY, REPOSITORY
from scripts.lib import conf


class HanaSetupTests(CostFixture):
    workload: Path = Path()
    clock: TestClock = TestClock()

    @override
    def setUp(self) -> None:
        super().setUp()
        self.clock = TestClock()
        self.workload = self.root / 'hana_aws.conf'
        _ = self.workload.write_text((REPOSITORY / 'config/hana_aws.conf').read_text())
        for key, value in {'ami_id': 'fixture-image', 'ami_owner_id': '000000000000',
                           'vpc_id': 'fixture-vpc', 'subnet_id': 'fixture-subnet'}.items():
            text_setting(self.workload, key, value)
        book = ledger.CostLedger(self.state)
        book.record_inventory(ledger.ResourceInventory(self.clock.now, (), 'private setup inventory', True))
        book.observe(ledger.InstanceObservation(self.clock.now, 'terminated', 'private setup account'))
        original = conf.read_or_repository
        def configuration(name: str) -> conf.ConfigRead:
            return conf.read(self.workload) if name == 'hana_aws' else original(name)
        _ = self.enterContext(mock.patch.object(conf, 'read_or_repository', side_effect=configuration))
        original_path = conf.path
        def config_path(name: str) -> Path:
            return self.workload if name == 'hana_aws' else original_path(name)
        _ = self.enterContext(mock.patch.object(conf, 'path', side_effect=config_path))

    def subject(self, fake: FakeAws) -> deploy.Deployment:
        return deploy.Deployment(self.settings, self.state, self.root / 'output', execute=fake, clock=self.clock, notify=lambda _event: True)

    def test_prepare_is_local_and_saves_separate_configuration_evidence(self) -> None:
        fake = FakeAws(self.clock)
        subject = self.subject(fake)
        # The existing validator reads account metadata before Hana replaces resource inputs.
        with mock.patch.object(deploy, 'read_deployment_settings') as read:
            read.return_value = self.base_settings()
            prepared = subject.prepare_hana()
            again = subject.prepare_hana()
        self.assertEqual(prepared.fingerprint, again.fingerprint)
        self.assertEqual(fake.calls, [])
        settings = hana_spend.read_settings(conf.read(self.workload))
        self.assertEqual(prepared.parameters['RootGiB'], str(settings.root_gib))
        self.assertEqual(prepared.parameters['DataGiB'], str(settings.data_gib))
        self.assertEqual(prepared.parameters['SpotMaxPrice'], str(settings.spot_max_usd_per_hour))
        self.assertTrue((subject.output / 'hana/preflight.json').exists())
        self.assertFalse((subject.output / 'preflight.json').exists())
        self.assertFalse(settings.enabled)

    def base_settings(self) -> DeploymentSettings:
        return DeploymentSettings('us-east-2', 'fixture-admin', 'fixture-gates', 'fixture-image',
            '000000000000', 'fixture-release', 'us-east-2b', 'fixture-vpc', 'fixture-subnet', '/dev/xvda',
            1, 2, 3, conf.read(self.workload).whole('aws_timeout_s'),
            conf.read(self.settings).whole('preflight_max_age_s'), 'fixture-scaler')

    def test_setup_requires_current_preparation_before_any_aws_call(self) -> None:
        fake = FakeAws(self.clock)
        subject = self.subject(fake)
        with mock.patch.object(deploy, 'read_deployment_settings', return_value=self.base_settings()), \
                mock.patch.object(subject, '_authorize'), mock.patch('sys.stdout', new_callable=StringIO):
            _ = subject.prepare_hana()
            data_gib = conf.read(self.workload).whole('data_gib')
            _ = self.workload.write_text(self.workload.read_text().replace(f'data_gib={data_gib}\n', f'data_gib={data_gib + 1}\n'))
            with self.assertRaisesRegex(deploy.DeploymentRefused, 'preparation expired|inputs changed'):
                _ = subject.hana_setup(approve_resources=True)
        self.assertEqual(fake.calls, [])

    def test_setup_reserves_costs_before_creating_resources_and_registers_only_after_stop(self) -> None:
        fake = FakeAws(self.clock, state='running')
        subject = self.subject(fake)
        original_call = fake.__call__
        calls: list[str] = []
        def api_call(operation: str, *arguments: str, service: str = 'ec2', timeout_s: float | None = None) -> dict[str, object]:
            calls.append(operation)
            if operation == 'execute-change-set':
                evidence = ledger.CostLedger(self.state).read_snapshot()
                self.assertTrue(any(isinstance(row.state, ledger.HanaExposureOpen) for row in evidence.hana_compute))
                self.assertTrue(evidence.hana_storage)
                self.assertEqual(service, 'cloudformation')
                self.assertIn('hana-build', arguments)
                return {}
            if operation == 'describe-change-set':
                return {'Status': 'CREATE_COMPLETE', 'Changes': []}
            if operation in ('create-change-set', 'wait') and service == 'cloudformation':
                return {}
            if operation == 'describe-stacks':
                outputs = {'Instance': IDENTITY.instance, 'ControlBucket': 'fixture-bucket', 'DataVolume': 'fixture-volume'}
                return {'Stacks': [{'StackStatus': 'CREATE_COMPLETE', 'Outputs': [
                    {'OutputKey': key, 'OutputValue': value} for key, value in outputs.items()]}]}
            import subprocess
            result = original_call(['aws', '--profile', 'fixture-admin', '--region', IDENTITY.region,
                service, operation, *arguments, '--output', 'json'], capture_output=True, text=True,
                timeout=conf.read(self.workload).whole('aws_timeout_s') if timeout_s is None else timeout_s, check=False)
            if result.returncode:
                raise subprocess.CalledProcessError(result.returncode, 'fake')
            return ledger.mapping(cast(object, json.loads(result.stdout)))
        with mock.patch.object(deploy, 'read_deployment_settings', return_value=self.base_settings()), \
                mock.patch.object(subject, '_authorize'), mock.patch.object(subject, '_validate_inputs') as validate, \
                mock.patch.object(aws_cli.AwsCli, 'call', side_effect=api_call), \
                mock.patch('sys.stdout', new_callable=StringIO):
            prepared = subject.prepare_hana()
            saved_path = self.state / 'hana/setup.json'
            saved = remote.read_record(saved_path)
            saved['deployment'] = IDENTITY.deployment
            _ = saved_path.write_text(json.dumps(saved))
            # Refresh preparation for the chosen fake deployment identity.
            _ = subject.prepare_hana()
            self.assertTrue(subject.hana_setup(approve_resources=True))
            validate.assert_called_once()
        record = remote.read_record(self.state / 'hana/lifecycle.json')
        self.assertEqual(record['state'], hana_box.Lifecycle.IDLE_STOPPED)
        self.assertEqual(ledger.mapping(record['identity'])['spot_request'], IDENTITY.spot_request)
        self.assertIn('stop-instances', calls)
        self.assertLess(calls.index('create-change-set'), calls.index('execute-change-set'))
        self.assertNotIn('deploy', calls)
        self.assertNotIn('start-instances', calls)
        self.assertFalse((self.state / 'gate-deployment.json').exists())
        self.assertEqual(conf.read(self.workload).text('enabled'), 'off')
        self.assertEqual(prepared.parameters['InstanceType'], conf.read(self.workload).text('instance_type'))

    def test_setup_reservation_does_not_enable_start_or_command_permission(self) -> None:
        settings = hana_spend.read_settings(conf.read(self.workload))
        spending = hana_spend.HanaSpending(ledger.CostLedger(self.state), self.settings, settings)
        reservation = spending.reserve_setup(self.clock.now, self.clock.now + settings.permit_max_seconds)
        self.assertIsInstance(reservation, hana_spend.StartReserved)
        assert isinstance(reservation, hana_spend.StartReserved)
        self.assertIsInstance(spending.check_start(self.clock.now, reservation.exposure.generation), hana_spend.PermitRefused)
        self.assertIsInstance(spending.bind_start(self.clock.now, reservation.exposure.generation, 'fixture-boot'), hana_spend.PermitRefused)

    def test_template_uses_retained_disks_persistent_spot_and_no_guest_credentials(self) -> None:
        template = remote.read_record(deploy.HANA_TEMPLATE)
        resources = ledger.mapping(template['Resources'])
        launch = ledger.mapping(ledger.mapping(ledger.mapping(resources['LaunchTemplate'])['Properties'])['LaunchTemplateData'])
        market = ledger.mapping(ledger.mapping(launch['InstanceMarketOptions'])['SpotOptions'])
        self.assertEqual(market['SpotInstanceType'], 'persistent')
        self.assertEqual(market['InstanceInterruptionBehavior'], 'stop')
        self.assertNotIn('IamInstanceProfile', launch)
        self.assertNotIn('KeyName', launch)
        self.assertEqual(ledger.mapping(resources['DataVolume'])['DeletionPolicy'], 'Retain')
        self.assertNotIn('AWS::IAM::User', deploy.HANA_TEMPLATE.read_text())

    def built_image(self) -> Path:
        result = self.root / 'store/0123456789abcdefghij-nixos-image-amazon'
        (result / 'nix-support').mkdir(parents=True)
        disk = result / 'nixos-image-amazon.vhd'
        _ = disk.write_bytes(b'fixture disk')
        _ = (result / 'nix-support/image-info.json').write_text(json.dumps({
            'label': '26.05.fixture', 'boot_mode': 'legacy-bios', 'system': 'x86_64-linux',
            'logical_bytes': str(4 * 2**30), 'file': str(disk)}))
        return result

    def test_built_image_name_follows_the_store_hash_and_refuses_unknown_files(self) -> None:
        built = hana_image.read_built(self.built_image())
        self.assertEqual(built.name, 'nixos-hana-26.05.fixture-0123456789ab')
        self.assertEqual(built.source_format, 'vpc')
        self.assertEqual(built.boot_mode, 'legacy-bios')
        with self.assertRaises(hana_image.ImageRefused):
            _ = hana_image.read_built(self.root / 'missing')

    def image_aws(self, existing: bool) -> tuple[list[tuple[str, tuple[str, ...]]], object]:
        calls: list[tuple[str, tuple[str, ...]]] = []
        def api_call(operation: str, *arguments: str, service: str = 'ec2', timeout_s: float | None = None) -> dict[str, object]:
            _ = service, timeout_s
            calls.append((operation, arguments))
            if operation == 'describe-route-tables':
                return {'RouteTables': [{'Associations': [{'Main': True}], 'Routes': [
                    {'DestinationCidrBlock': '0.0.0.0/0', 'State': 'active', 'GatewayId': 'igw-fixture'}]}]}
            if operation == 'describe-subnets':
                return {'Subnets': [{'SubnetId': 'fixture-subnet-c', 'AvailabilityZone': 'us-east-2c', 'State': 'available'},
                                    {'SubnetId': 'fixture-subnet-b', 'AvailabilityZone': 'us-east-2b', 'State': 'available'}]}
            if operation == 'describe-images':
                if '--image-ids' in arguments or existing:
                    return {'Images': [{'ImageId': 'fixture-hana-image', 'OwnerId': 'fixture-owner', 'State': 'available'}]}
                return {'Images': []}
            if operation == 'register-image':
                return {'ImageId': 'fixture-hana-image'}
            return {}
        return calls, api_call

    def run_image(self, existing: bool) -> tuple[list[tuple[str, tuple[str, ...]]], list[list[str]], list[list[str]]]:
        calls, api_call = self.image_aws(existing)
        local: list[list[str]] = []
        uploads: list[list[str]] = []
        def execute(arguments: Sequence[str], *, capture_output: bool, text: bool, timeout: float, check: bool,
                    env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
            _ = capture_output, text, timeout, check, env
            local.append(list(arguments))
            if arguments[0] == 'qemu-img':
                _ = Path(arguments[-1]).write_bytes(b'raw')
            return subprocess.CompletedProcess(list(arguments), 0, '/nix/store/fixture-coldsnap\n', '')
        def admin(_execute: object) -> object:
            def authorized(arguments: Sequence[str], **_options: object) -> subprocess.CompletedProcess[str]:
                uploads.append(list(arguments))
                return subprocess.CompletedProcess(list(arguments), 0, 'fixture-snapshot\n', '')
            return authorized
        subject = deploy.Deployment(self.settings, self.state, self.root / 'output', execute=execute,
                                    clock=self.clock, notify=lambda _event: True)
        with mock.patch.object(deploy, 'read_deployment_settings', return_value=self.base_settings()), \
                mock.patch.object(subject, '_authorize'), mock.patch.object(subject, '_guard'), \
                mock.patch.object(subject.admin, 'executor', side_effect=admin), \
                mock.patch.object(aws_cli.AwsCli, 'call', side_effect=api_call), \
                mock.patch('sys.stdout', new_callable=StringIO):
            self.assertTrue(subject.hana_image(self.built_image(), approve_resources=True))
        self.assertFalse((subject.output / 'hana/image.raw').exists())
        return calls, local, uploads

    def test_image_without_approval_makes_no_aws_call(self) -> None:
        fake = FakeAws(self.clock)
        with mock.patch('sys.stdout', new_callable=StringIO):
            self.assertFalse(self.subject(fake).hana_image(self.built_image()))
        self.assertEqual(fake.calls, [])
        self.assertIsInstance(hana_image.saved(self.state), hana_image.NoRegisteredImage)

    def test_image_upload_registers_imds_v2_root_disk_and_saves_the_zone_subnet(self) -> None:
        calls, local, uploads = self.run_image(existing=False)
        self.assertEqual(local[0][:5], ['qemu-img', 'convert', '-f', 'vpc', '-O'])
        self.assertEqual(uploads[0][0], '/nix/store/fixture-coldsnap/bin/coldsnap')
        register = dict(calls)['register-image']
        self.assertIn('v2.0', register)
        self.assertIn('DeviceName=/dev/xvda,Ebs={SnapshotId=fixture-snapshot,VolumeType=gp3,DeleteOnTermination=true}', register)
        self.assertEqual(hana_image.saved(self.state), hana_image.RegisteredImage(
            'nixos-hana-26.05.fixture-0123456789ab', 'fixture-hana-image', 'fixture-owner', 'fixture-vpc', 'fixture-subnet-b'))

    def test_image_with_the_same_name_is_not_uploaded_again(self) -> None:
        calls, local, uploads = self.run_image(existing=True)
        self.assertEqual((local, uploads), ([], []))
        self.assertNotIn('register-image', dict(calls))
        self.assertIsInstance(hana_image.saved(self.state), hana_image.RegisteredImage)

    def test_saved_image_fills_only_empty_configuration_values(self) -> None:
        _ = hana_image.save(self.state, hana_image.RegisteredImage('fixture-name', 'saved-image', 'saved-owner',
                                                                   'saved-vpc', 'saved-subnet'))
        text_setting(self.workload, 'ami_id', '')
        text_setting(self.workload, 'ami_owner_id', '')
        subject = self.subject(FakeAws(self.clock))
        with mock.patch.object(deploy, 'read_deployment_settings', return_value=self.base_settings()):
            settings, parameters, _ = subject._hana_inputs()  # pyright: ignore[reportPrivateUsage]
        self.assertEqual((settings.ami_id, settings.ami_owner_id), ('saved-image', 'saved-owner'))
        self.assertEqual((settings.vpc_id, parameters['SubnetId']), ('fixture-vpc', 'fixture-subnet'))
