"""Check the published AWS table and quota case instructions."""

from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from html import unescape
import json
from pathlib import Path
import re
import shutil
import subprocess
from typing import cast, override
import unittest
from unittest import mock

from scripts.aws_target import aws_page, cloud_gate, console_help, ledger, ledger_test_support, quota_requests, run_cost, spot_table
from scripts.aws_target.aws_cli import AwsCli
from scripts.lib import conf
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.review_links import page_frame


class AwsPageQuotaHelpTests(ledger_test_support.CostFixture):
    def test_quota_help_is_closed_and_contains_the_ordered_steps_and_case_link(self) -> None:
        now = ledger_test_support.instant('2026-01-15T12:00:00Z')
        page = aws_page.publish(self.state, now, self.root / 'page', settings_path=self.settings)
        section = page.read_text().split('<section class="card" id="prices-quotas">', 1)[1].split('</section>', 1)[0]
        details = re.search(r'<details id="quota-help">(.*?)</details>\s*$', section, flags=re.S)
        assert details is not None
        block = details[1]
        self.assertIn('<summary>How to open the quota cases in AWS</summary>', block)
        self.assertRegex(block, r'<ol><li>.*</li></ol>')
        steps = tuple(match[1] for match in re.finditer(r'<li>(.*?)</li>', block))
        self.assertEqual(tuple(unescape(re.sub(r'<[^>]+>', '', step)) for step in steps), console_help.QUOTA_CASE_STEPS)
        url = console_help.CASE_LIST_URL
        self.assertIn(f'<a href="{url}">{url}</a>', steps[2])
        self.assertEqual(block.count('<a href='), 1)
        self.assertEqual(re.findall(r'<p>(.*?)</p>', unescape(block)), [console_help.WRONG_ACCOUNT])

    def test_quota_help_uses_the_shared_words(self) -> None:
        with (mock.patch.object(console_help, 'QUOTA_CASE_STEPS', ('Open the test console.',)),
              mock.patch.object(console_help, 'WRONG_ACCOUNT', 'Choose the test account.')):
            self.assertEqual(aws_page.quota_help(), '<ol><li>Open the test console.</li></ol>'
                             + '\n<p>Choose the test account.</p>')

    def test_quota_request_uses_plain_labels_when_values_are_missing(self) -> None:
        applied = aws_page.AppliedQuotaUnavailable.NOT_MEASURED
        for status, expected in (
            ('APPROVED', {'request': 'not measured', 'detail': 'closed, quota unknown'}),
            ('IN_PROGRESS', {'request': 'open'}),
            ('FIXTURE_UNKNOWN', {'request': 'not measured', 'detail': 'status unknown'}),
        ):
            with self.subTest(status=status):
                request = quota_requests.quota_request(status, Decimal(32), None, None)
                self.assertEqual(aws_page.quota_request_columns(request, applied, 0),
                                 {'wanted': '32', 'age': 'age unknown', **expected})
        reading = quota_requests.QuotaRequestReadFailed('fixture private diagnostic')
        self.assertEqual(aws_page.quota_request_columns(reading, applied, 0),
                         {'wanted': '—', 'request': 'not measured', 'age': '—', 'detail': 'request read failed'})

    def test_future_quota_request_dates_have_unknown_age(self) -> None:
        now = ledger_test_support.instant('2026-01-15T12:00:00Z')
        future = '2026-01-22T12:00:00Z'
        past = '2026-01-14T12:00:00Z'
        for created, updated in ((future, past), (None, future)):
            with self.subTest(created=created, updated=updated):
                request = quota_requests.quota_request('PENDING', Decimal(32), created, updated)
                self.assertEqual(aws_page.quota_request_age(request, now), 'age unknown')


class PageCostFixture(ledger_test_support.CostFixture):
    @override
    def setUp(self) -> None:
        super().setUp()
        self.costs({'spend_pace_days': '3', 'run_cost_days': '5', 'billed_hours_gap_max_ratio': '0.2'})
        self.configure_gate("""
regions=us-east-1
spot_candidate_regions=us-east-1
instance_types=m7a.8xlarge
price_compare_instance_types=c7a.8xlarge
gpu_instance_types=g6.2xlarge
hana_gpu_instance_types=g6.2xlarge
gate_wait_usd_per_minute=0
unmeasured_region_extra_seconds={}
# whole: 1..
lifetime_minutes=30
# whole: 1..
gpu_spot_quota_request_vcpus=32
""")


class AwsGpuQuotaSummaryTests(PageCostFixture):
    now: int = ledger_test_support.instant('2026-01-15T12:00:00Z')

    def quota_summary(self, applied: dict[str, str], history: dict[str, dict[str, str]]) -> aws_page.GpuQuotaSummary:
        _ = (self.state / 'spot-prices.json').write_text(json.dumps({
            'prices': [],
            'regions': [{'region': region, 'read_at': self.now, 'standard_vcpus': '64', 'gpu_vcpus': vcpus}
                        for region, vcpus in applied.items()],
            'quota_requests': [{'region': region, 'standard': {'state': 'no_request'}, 'gpu': reading}
                               for region, reading in history.items()],
        }))
        tables = spot_table.spot_tables(self.state, self.now, settings_path=self.settings)
        return aws_page.quota_history_data(tables, self.now)['gpu_summary']

    def test_applied_quota_counts_without_request_history(self) -> None:
        for history in (
            {},
            {'us-east-1': {'state': 'no_request'}},
            {'us-east-1': {'state': 'not_measured', 'reason': 'fixture'}},
            {'us-east-1': {'state': 'read_failed', 'reason': 'fixture'}},
        ):
            with self.subTest(history=history):
                self.assertEqual(self.quota_summary({'us-east-1': '32'}, history), {
                    'target_vcpus': 32,
                    'regions_by_outcome': {'granted': ['us-east-1'], 'open': [], 'closed, not granted': []},
                })

    def test_summary_counts_regions_in_the_quota_table(self) -> None:
        config = self.root / 'cloud_gate.conf'
        ledger_test_support.text_setting(config, 'regions', 'us-east-1 us-east-2')
        ledger_test_support.text_setting(config, 'spot_candidate_regions', 'us-east-1 eu-north-1')
        request = {'state': 'requested', 'status': 'PENDING', 'requested_vcpus': '32'}
        self.assertEqual(self.quota_summary(
            {'us-east-1': '32', 'eu-north-1': '8'},
            {'us-east-1': request, 'eu-north-1': request},
        ), {'target_vcpus': 32,
            'regions_by_outcome': {'granted': ['us-east-1'], 'open': ['eu-north-1'], 'closed, not granted': []}})


class AwsPageLaunchStateTests(PageCostFixture):
    now: int = ledger_test_support.instant('2026-01-15T12:00:00Z')

    def published_launch_state(self, *events: Mapping[str, object],
                               default_attempt_ids: bool = True) -> dict[str, object]:
        rows = ({'instance_type': 'm7a.8xlarge', 'region': 'us-east-1', 'mode': 'gate',
                 **({'attempt': f'fixture-{index}'} if default_attempt_ids else {}),
                 **event} for index, event in enumerate(events))
        _ = (self.state / 'cloud-gate-launches.jsonl').write_text(''.join(json.dumps(row) + '\n' for row in rows))
        page = aws_page.publish(self.state, self.now, self.root / 'page', settings_path=self.settings)
        source = page.read_text()
        self.assertNotIn('private diagnostic', source)
        payload = source.split('<script id="aws-prices-data" type="application/json">', 1)[1].split('</script>', 1)[0]
        return ledger.mapping(ledger.mapping(cast(object, json.loads(payload)))['launch_state'])

    def test_account_refusals_count_from_the_last_good_launch(self) -> None:
        normal_codes = ('SpotMaxPriceTooLow', 'VcpuLimitExceeded', 'MaxSpotInstanceCountExceeded',
                        'InsufficientSpotCapacity', 'InsufficientInstanceCapacity')
        state = self.published_launch_state(
            {'time': self.now - 600, 'result': 'refused', 'error_code': 'Blocked'},
            {'time': self.now - 500, 'result': 'launched'},
            {'time': self.now - 240.25, 'result': 'refused', 'error_code': 'ExpiredToken'},
            {'time': self.now - 180, 'result': 'refused', 'error_code': 'Blocked', 'reason': 'private diagnostic'},
            *({'time': self.now - 120 + index, 'result': 'refused', 'error_code': code}
              for index, code in enumerate(normal_codes)),
            {'time': self.now - 10, 'result': 'finished'},
        )
        self.assertEqual(state, {'kind': 'refused', 'code': 'Blocked', 'reason': 'The account is blocked.',
                                 'first_at': self.now - 240.25, 'count': 2, 'last_good_at': self.now - 500,
                                 'latest_attempt_at': self.now - 116})

    def test_a_good_launch_clears_the_refusal_run(self) -> None:
        state = self.published_launch_state(
            {'time': self.now - 180, 'result': 'refused', 'error_code': 'InvalidClientTokenId'},
            {'time': self.now - 120, 'result': 'launched'},
            {'time': self.now - 60, 'result': 'refused', 'error_code': 'InsufficientSpotCapacity'},
        )
        self.assertEqual(state, {'kind': 'working', 'last_good_at': self.now - 120,
                                 'latest_attempt_at': self.now - 60})

    def test_other_refusals_replace_the_working_state_without_publishing_diagnostics(self) -> None:
        for code, shown in (({}, 'unknown code'), ({'error_code': 'fixture-private-id'}, 'unknown code'),
                            ({'error_code': 'UnauthorizedOperation'}, 'UnauthorizedOperation')):
            with self.subTest(code=code):
                state = self.published_launch_state(
                    {'time': self.now - 120, 'result': 'launched'},
                    {'time': self.now - 60, 'result': 'refused', 'reason': 'private diagnostic', **code})
                self.assertEqual((state['kind'], state['code']), ('refused', shown))
                self.assertEqual(state['reason'], 'The launch could not proceed.')
                self.assertNotIn('fixture-private-id', json.dumps(state))

    def test_prelaunch_credential_fallback_counts_and_local_fallback_does_not(self) -> None:
        state = self.published_launch_state(
            {'time': self.now - 120, 'result': 'launched'},
            {'time': self.now - 90, 'result': 'fallback', 'error_code': 'ExpiredToken', 'reason': 'private diagnostic'},
            {'time': self.now - 60, 'result': 'fallback', 'error_code': 'Spot'},
        )
        self.assertEqual((state['kind'], state['code'], state['count']), ('refused', 'ExpiredToken', 1))

    def test_fallback_after_launch_does_not_report_a_refusal(self) -> None:
        launched_at = self.now - 120
        state = self.published_launch_state(
            {'attempt': 'gate-a', 'time': launched_at, 'result': 'launched'},
            {'attempt': 'gate-a', 'time': self.now - 60, 'result': 'fallback', 'error_code': 'AccessDenied',
             'reason': 'GetObject failed: private diagnostic'},
        )
        self.assertEqual(state, {'kind': 'working', 'last_good_at': launched_at, 'latest_attempt_at': launched_at})

    def test_fallback_after_refusal_counts_only_the_gate_attempt(self) -> None:
        refused_at = self.now - 90
        refusal = {'attempt': 'gate-b', 'time': refused_at, 'result': 'refused',
                   'error_code': 'ServiceUnavailable'}
        fallback = {**refusal, 'time': self.now - 60, 'result': 'fallback'}
        for worker_rows in ((refusal,), (refusal, {**refusal, 'time': refused_at + 1})):
            with self.subTest(worker_rows=len(worker_rows)):
                state = self.published_launch_state(*worker_rows, fallback)
                self.assertEqual((state['kind'], state['code'], state['count']), ('refused', 'ServiceUnavailable', 1))
                self.assertEqual(state['latest_attempt_at'], worker_rows[-1]['time'])

    def test_fallback_without_attempt_id_keeps_the_good_launch(self) -> None:
        launched_at = self.now - 120
        for identity in ({}, {'attempt': ''}):
            with self.subTest(identity=identity):
                state = self.published_launch_state(
                    {'time': launched_at, 'result': 'launched', **identity},
                    {'time': self.now - 60, 'result': 'fallback', 'error_code': 'AccessDenied', **identity},
                    default_attempt_ids=False,
                )
                self.assertEqual(state, {'kind': 'working', 'last_good_at': launched_at,
                                         'latest_attempt_at': launched_at})

    def test_fallback_without_attempt_id_does_not_count_the_refusal_twice(self) -> None:
        refused_at = self.now - 120
        for identity in ({}, {'attempt': ''}):
            with self.subTest(identity=identity):
                refusal = {'time': refused_at, 'result': 'refused', 'error_code': 'ServiceUnavailable', **identity}
                state = self.published_launch_state(
                    refusal,
                    {**refusal, 'time': self.now - 60, 'result': 'fallback'},
                    default_attempt_ids=False,
                )
                self.assertEqual((state['kind'], state['count'], state['latest_attempt_at']),
                                 ('refused', 1, refused_at))

    def test_state_without_a_good_launch(self) -> None:
        self.assertEqual(self.published_launch_state(), {'kind': 'no-evidence'})
        state = self.published_launch_state(
            {'time': self.now - 60, 'result': 'refused', 'error_code': 'AuthFailure'})
        self.assertEqual(state, {'kind': 'refused', 'code': 'AuthFailure',
                                 'reason': 'AWS could not authenticate the launch credentials.',
                                 'first_at': self.now - 60, 'count': 1, 'latest_attempt_at': self.now - 60})

    def test_publisher_settings_have_fixed_fixture_values(self) -> None:
        settings = conf.read(self.settings)
        self.assertEqual((settings.whole('spend_pace_days'), settings.whole('run_cost_days'),
                          settings.number('billed_hours_gap_max_ratio')), (3, 5, 0.2))
        self.assertEqual(conf.read('cloud_gate').words('spot_candidate_regions'), ('us-east-1',))


class AwsPageSettledAmountsTests(ledger_test_support.CostFixture):
    def test_spend_shows_the_settled_summary_and_changed_amounts(self) -> None:
        self.text_setting('accounting_zone', 'America/Los_Angeles')
        checked = ledger_test_support.instant('2026-11-19T00:15:00Z')
        start = ledger_test_support.instant('2026-11-04T00:00:00Z')
        end = start + 9 * 86400
        charges = self.state / 'charges'
        charges.mkdir()
        settled = Decimal('7.73')
        unsettled = ledger.ChargeRead('2026-11-13', checked, end, end + 86400, settled, Decimal(0), Decimal(0))
        _ = (charges / '2026-11-13.json').write_text(json.dumps(ledger.charge_record(unsettled)))

        def settled_notes() -> list[str]:
            page = aws_page.publish(self.state, checked, self.root / 'page', settings_path=self.settings)
            payload = page.read_text().split('<script id="aws-prices-data" type="application/json">', 1)[1]
            data = cast(aws_page.PageData, json.loads(payload.split('</script>', 1)[0]))
            return data['settled_notes']

        self.assertEqual(settled_notes(), [])
        reads = ledger_test_support.charge_readings(start, end, settled, Decimal(0))
        for bill in reads:
            record = ledger.charge_record(replace(bill, at=checked - 86400))
            record['amount_reads'] = [
                {'at': checked - 2 * 86400, 'actual_usd': '7.73'},
                {'at': checked - 86400, 'actual_usd': '7.73'},
            ]
            _ = (charges / (bill.day + '.json')).write_text(json.dumps(record))
        self.assertEqual(settled_notes(), [
            'Settled AWS amounts: 9 of 9 did not change after they settled (checked Nov 19).',
        ])

        path = charges / '2026-11-12.json'
        changed = ledger.mapping(cast(object, json.loads(path.read_text())))
        changed.update(at=checked, actual_usd='7.81')
        changed['amount_reads'] = [*ledger.rows(changed, 'amount_reads'), {'at': checked, 'actual_usd': '7.81'}]
        _ = path.write_text(json.dumps(changed))
        self.assertEqual(settled_notes(), [
            'Settled AWS amounts: 8 of 9 did not change after they settled (checked Nov 19).',
            '2026-11-12 changed from USD 7.73 to USD 7.81 (+0.08).',
        ])


@unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for the AWS table checks")
class AwsCombinedTableTests(PageCostFixture):
    def fixture_page(self, *, blocked: bool = False,
                     quota_history: tuple[dict[str, object], ...] = ()) -> Path:
        now = ledger_test_support.instant('2026-01-15T12:00:00Z')
        regions = ('us-east-1', 'us-east-2', 'eu-west-1')
        standard = ('c7a.4xlarge', 'm7a.8xlarge')
        gpu = 'g6.2xlarge'
        config = self.root / 'cloud_gate.conf'
        self.configure_gate('max_gate_minutes=60\n')
        for key, value in (
            ('spot_candidate_regions', ' '.join(reversed(regions))), ('instance_types', ' '.join(standard)),
            ('price_compare_instance_types', standard[1]), ('gpu_instance_types', gpu),
            ('hana_gpu_instance_types', gpu),
        ):
            ledger_test_support.text_setting(config, key, value)
        prices = [
            {'region': region, 'instance_type': size, 'zone': region + 'a', 'usd_per_hour': usd, 'read_at': now - 240}
            for region, row in zip(regions, (('0.1', '0.6', '0.7'), ('0.2', '0.5', '0.8'), ('0.3', '', '0.9')), strict=True)
            for size, usd in zip((*standard, gpu), row, strict=True) if usd
        ]
        _ = (self.state / 'gate-deployment.json').write_text(json.dumps({'regions': [
            {'region': region, 'subnets': [{'id': 'subnet-' + region, 'zone': region + 'a'}]} for region in regions
        ]}))
        readings = [
            {'region': region, 'read_at': now - age, 'standard_vcpus': '137', 'gpu_vcpus': '8' if quota_history and region != 'us-east-2' else '32'}
            for region, age in zip(regions, (240, 7201, 240), strict=True)
        ]
        failures = [{'region': regions[0], 'at': now - 120, 'read': 'price', 'error': 'FixtureError'}]
        _ = (self.state / 'spot-prices.json').write_text(json.dumps({
            'prices': prices, 'regions': readings, 'failures': failures,
            **({'quota_requests': quota_history} if quota_history else {}),
        }))
        events: list[dict[str, object]] = []
        for mode, times in (('merge-gate', (3600, 360)), ('targeted-test', (600, 1800))):
            for index, (size, seconds) in enumerate(zip(standard, times, strict=True)):
                run = {'attempt': f'{mode}-{index}', 'instance_type': size, 'mode': mode, 'region': regions[0]}
                events.extend(({**run, 'time': now - 60 - seconds, 'result': 'launched'},
                               {**run, 'time': now - 60, 'result': 'finished', 'gate_s': seconds - 60}))
        if blocked:
            events.append({'attempt': 'blocked-fixture', 'instance_type': standard[0], 'mode': 'gate',
                           'region': regions[0], 'time': now - 30, 'result': 'refused', 'error_code': 'Blocked'})
        _ = (self.state / 'cloud-gate-launches.jsonl').write_text(''.join(json.dumps(event) + '\n' for event in events))
        placement_regions = (regions[0], 'us-west-1')
        request = ledger.SpotPlacementRequest(ledger.SpotPlacementScope.REGION,
            ledger.SpotPlacementTypeSet.ALL_GATE_TYPES, 8, standard, placement_regions)
        ledger.write_spot_placement_scores(self.state, (ledger.SpotPlacementSample(request, now,
            (ledger.SpotPlacementScore(ledger.SpotPlacementRegion(regions[0]), 9),)),), now)
        return aws_page.publish(self.state, now, self.root / 'page', settings_path=self.settings)

    def run_browser_checks(self, query: str, checks: str, *, blocked: bool = False,
                           quota_history: tuple[dict[str, object], ...] = ()) -> None:
        page = self.fixture_page(blocked=blocked, quota_history=quota_history)
        probe = """<script>
(async () => {
  const result = document.createElement("p");
  result.id = "table-checks";
  document.body.appendChild(result);
  function check(condition, description) { if (!condition) throw new Error(description); }
  try {
""" + checks + """
    result.textContent = "passed";
  } catch (error) {
    result.textContent = String(error);
  }
})();
</script>"""
        _ = page.write_text(page.read_text().replace('</body>', probe + '</body>', 1))
        result = subprocess.run([
            'google-chrome', '--headless', '--no-sandbox', '--disable-gpu', '--disable-background-networking',
            '--no-first-run', '--host-resolver-rules=MAP * ~NOTFOUND', f'--user-data-dir={self.root / "chrome"}',
            '--window-size=760,1000',
            '--dump-dom', page.as_uri() + query,
        ], capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS, check=True)
        marker = re.search(r'<p id="table-checks">(.*?)</p>', result.stdout)
        assert marker is not None, 'The browser must finish the table checks.'
        self.assertEqual(unescape(marker[1]), 'passed')

    def test_disabled_cloud_gates_replace_the_launch_status(self) -> None:
        ledger_test_support.override_fixture_settings(self.root / 'build_targets.conf', 'enabled.aws-gate=off')
        self.run_browser_checks('', """
    const block = document.getElementById("launch-state");
    check(block.dataset.kind === "off", "Disabled cloud gate state");
    check(block.innerText === "Cloud gates are off (build_targets.conf enabled.aws-gate=off). Gates run on natedev.",
          "The disabled state explains where gates run");
""", blocked=True)

    def test_invalid_cloud_gate_switch_keeps_the_other_page_blocks(self) -> None:
        ledger_test_support.override_fixture_settings(self.root / 'build_targets.conf', 'enabled.aws-gate=invalid')
        at = ledger_test_support.instant('2026-01-15T11:59:00Z')
        with ledger.CostLedger(self.state).transaction() as evidence:
            evidence.observations.append(ledger.InstanceObservation(at, 'stopped', 'fixture'))
            evidence.inventories.append(ledger.ResourceInventory(at, (), 'fixture', builder_removed=True))
        self.run_browser_checks('?show=quota', """
    const block = document.getElementById("launch-state");
    check(block.innerText === "build_targets.conf enabled.aws-gate is not valid. Gates run on natedev.",
          "The invalid switch explains where gates run");
    check(document.querySelectorAll("#spend-tiles .tile").length === 3, "Spend still renders");
    check(document.querySelector("#price-table tbody tr"), "Quotas still render");
    const hours = document.getElementById("hours");
    check(hours.querySelector("h2").textContent === "Did AWS bill more machine hours than we used?", "Hours still render");
    check(hours.querySelector("p").textContent === "No AWS bill is available yet for the last 5 days.",
          "The answer explains that no bill is available");
    check(!hours.querySelector("table, details"), "No table or disclosure without a bill");
""", blocked=True)

    def test_billed_hours_filters_sizes_by_activity_in_the_selected_regions(self) -> None:
        start = ledger_test_support.instant('2026-01-14T00:00:00Z')
        end = start + 86400
        with ledger.CostLedger(self.state).transaction() as evidence:
            evidence.hana_compute.append(ledger.HanaComputeExposure(
                start, start + 3600, 1, Decimal('.25'), ledger.HanaExposureClosed(start + 3600),
                'm7a.2xlarge', region='us-east-2'))
        charges = self.state / 'charges'
        charges.mkdir()
        charge = ledger.ChargeRead(
            '2026-01-14', end, start, end, Decimal(1), Decimal(0), Decimal(0),
            (ledger.UsageTypeCost('USE1-SpotUsage:c7a.4xlarge', Decimal('.75'), Decimal(1), 'Hrs'),
             ledger.UsageTypeCost('USE2-SpotUsage:m7a.8xlarge', Decimal('.25'), Decimal('.01'), 'Hrs')))
        _ = (charges / '2026-01-14.json').write_text(json.dumps(ledger.charge_record(charge)))
        self.run_browser_checks('', """
    const sizes = () => [...document.querySelectorAll("#hours details tbody tr")].map(row => row.cells[0].textContent);
    check(sizes().includes("c7a.4xlarge") && sizes().includes("m7a.8xlarge"), "Both active sizes show");
    const east = document.querySelector("#regions [data-region='us-east-1']");
    document.querySelector("#regions .all:last-child").click();
    document.querySelector("#regions [data-region='us-east-2']").click();
    check(sizes().join() === "m7a.8xlarge", "The hidden region's size row is absent");
    const cells = [...document.querySelector("#hours details tbody tr").cells];
    check(cells.slice(1, 5).map(cell => cell.textContent).join("|") === "us-east-2|0.0|0.0|less than 0.1 hours",
          "A marked difference still shows when the hours round to zero");
    check(document.querySelector("#hours p").textContent
          === "Yes. AWS billed less than 0.1 hours more than our records, in 1 row.",
          "The answer and row show the same small difference");
    east.click();
    check(sizes().includes("c7a.4xlarge"), "The row returns when its region is selected");
""")

    def test_launch_state_sits_above_spend_and_updates_after_a_good_launch(self) -> None:
        self.run_browser_checks('', """
    const block = document.getElementById("launch-state");
    const message = () => block.querySelector("[role='status']").textContent;
    check(block.nextElementSibling.id === "spend", "Launch state sits above spend");
    check(block.dataset.kind === "refused", "Refusal style");
    check(!block.innerText.includes("Cloud gates are off"), "The notice is absent when gates are on");
    check(message().includes("AWS refuses launches (Blocked). The account is blocked."),
          "Refusal includes the code and plain reason");
    const {first_at, last_good_at, latest_attempt_at} = data.launch_state;
    check(message().includes(`First refusal: ${launchTime.format(first_at * 1000)}.`)
          && message().includes(`1 refusal since the last good launch (${launchTime.format(last_good_at * 1000)}).`),
          "Refusal times and count");
    check(Number.isFinite(latest_attempt_at)
          && message().includes(`Latest attempt: ${launchTime.format(latest_attempt_at * 1000)}.`),
          "Latest attempt appears in the refused state");
    document.querySelector("#regions .all:last-child").click();
    check(block.dataset.kind === "refused", "Region filters leave launch state intact");
    const next = JSON.stringify({...data, launch_state: {kind: "working", last_good_at: first_at + 1,
      latest_attempt_at: first_at + 61}});
    const gateTime = document.getElementById("aws-gate-time-data").outerHTML;
    window.fetch = async () => ({ok: true, text: async () =>
      `<script type="application/json" id="aws-prices-data">${next}</scr` + "ipt>" + gateTime});
    await loadAws();
    check(block.dataset.kind === "working", "Refresh clears refusal style");
    check(message().includes(`AWS launches work. Last good launch: ${launchTime.format((first_at + 1) * 1000)}.`)
          && message().includes(`Latest attempt: ${launchTime.format((first_at + 61) * 1000)}.`),
          "Refresh shows the last good launch and latest attempt");
""", blocked=True)

    def test_default_table_marks_and_shared_filters(self) -> None:
        self.run_browser_checks('?prices=cost', """
    check(scope === null && scopeCosts().scope === "gate", "The default scope shows gate prices");
    const table = () => document.querySelector("#price-table table");
    const click = selector => document.querySelector(selector).click();
    const spot = () => table().tBodies[0].rows[0].cells[5];
    const gate = () => table().tBodies[0].rows[0].cells[6];
    const params = () => new URLSearchParams(location.search);
    const placement = document.querySelector("#placement-scores");
    const placementRows = () => [...placement.querySelectorAll("tbody tr")];
    check(placement.previousElementSibling.id === "prices-quotas", "Placement follows Prices and quotas");
    check(placementRows()[0].cells[3].textContent === "9 / 9 / 1", "Placement sample renders");
    check(placementRows().some(row => row.cells[0].textContent === "us-west-1")
          && table().tBodies[0].rows.length === 3, "A region with only scores does not get a price row");
    check(table().tHead.rows.length === 2, "Default grouped headers");
    check(table().tHead.rows[0].cells[5].textContent === "c7a.4xlarge (16 vCPUs)", "Type vCPU count");
    check(table().tHead.rows[1].cells[0].textContent === "Spot USD/h", "Spot unit");
    check(table().tHead.rows[1].cells[1].textContent === "Gate USD", "Gate unit");
    check(table().tHead.rows[1].cells[2].textContent === "Fit boxes", "Fit unit");
    const first = table().tBodies[0].rows[0];
    check(first.cells[1].textContent === "137" && first.cells[7].textContent === "8", "Quota and Fit numbers");
    check([...table().querySelectorAll(".quota-request")].map(cell => cell.textContent).join(",")
          === "not measured,not measured,not measured", "Legacy snapshots have no request history");
    check(document.querySelector("#quota-request-note").textContent === "The snapshot has no quota request history.",
          "One note for the legacy table");
    check(first.cells[1].className === "n edge region-quota" && first.cells[7].className === "n", "No quota marks");
    check(spot().textContent === "0.1000" && spot().classList.contains("cheapest"), "Lowest Spot price");
    check(first.querySelector(".spot-status").textContent === "read failed", "Visible failed read");
    check(table().tBodies[0].rows[1].querySelector(".spot-status").textContent === "old", "Visible old read");
    check(first.cells[8].classList.contains("dearest"), "Highest Spot price");
    check(table().tBodies[0].rows[1].cells[9].classList.contains("cheapest"), "Lowest Gate cost in another cell");
    check(table().tBodies[0].rows[2].cells[6].classList.contains("dearest"), "Highest Gate cost");
    check(table().tBodies[0].rows[2].cells[5].classList.contains("end"), "Column box");
    check(table().tBodies[0].rows[2].cells[8].title === "Not sold here", "Unavailable type icon");
    const missingGate = table().tBodies[0].rows[2].cells[9];
    check(missingGate.title === "no current gate price"
          && missingGate.getAttribute("aria-label") === missingGate.title, "Unavailable Gate price note");
    const merge = gate().textContent;
    click("#runs-chips [data-scope='test-affected']");
    check(gate().textContent !== merge, "Runs chips change the Gate values");
    check(document.querySelector("#filter [data-scope='test-affected']").getAttribute("aria-pressed") === "true",
          "Runs chips update Cost per run");
    click("#regions [data-region='us-east-1']");
    check(table().tBodies[0].rows.length === 2 && spot().classList.contains("cheapest"), "Marks use the filtered regions");
    check(!placementRows().some(row => row.cells[0].textContent === "us-east-1"), "The region filter applies to placement scores");
    check(gate().classList.contains("cheapest") && table().tBodies[0].rows[0].cells[9].classList.contains("dearest"),
          "Gate marks use the filtered regions");
    click("#show [data-measure='cost']");
    check(document.querySelector("#runs-label").hidden && document.querySelector("#runs-chips").hidden, "Hide Runs");
    check(params().get("show") === "spot,quota" && params().get("runs") === "test-affected", "Show and Runs URL");
    click("#show [data-measure='spot']");
    check(table().tHead.rows.length === 1 && table().tBodies[0].rows[0].cells.length === 7, "Fit alone");
    click("#show [data-measure='quota']");
    check(table().tBodies[0].rows[0].cells.length === 5 && params().get("show") === "", "Quota remains with all chips off");
    click("#show [data-measure='cost']");
    const gateOnly = table().tBodies[0].rows[0].cells;
    check(gateOnly[5].classList.contains("cheapest") && gateOnly[6].classList.contains("dearest"), "Gate marks with Gate alone");
    click("#show [data-measure='spot']");
    document.head.appendChild(element("style", "#price-table table { min-width: 1000px; }"));
    const scroller = () => document.querySelector("#price-table .scroll");
    check(innerWidth === 760 && scroller().scrollWidth > scroller().clientWidth, "Narrow scroll container");
    scroller().scrollLeft = 150;
    check(scroller().scrollLeft === 150, "Set scroll position before refresh");
    const settled = document.getElementById("settled-notes");
    check(settled.children.length === 0, "No settled notes on the first render");
    const notes = ["Settled AWS amounts: 8 of 9 did not change after they settled (checked Nov 19).",
                   "2026-11-12 changed from USD 7.73 to USD 7.81 (+0.08)."];
    const prices = JSON.parse(document.getElementById("aws-prices-data").textContent);
    let refreshed = JSON.stringify({...prices, render_seconds: 12, settled_notes: notes});
    const gateTime = document.getElementById("aws-gate-time-data").textContent;
    window.fetch = async () => ({ok: true, text: async () =>
      `<script type="application/json" id="aws-prices-data">${refreshed}</scr` + "ipt>"
      + `<script type="application/json" id="aws-gate-time-data">${gateTime}</scr` + "ipt>"});
    const status = await loadAws();
    check(status.renderSeconds === 12 && scroller().scrollLeft === 150, "Refresh keeps scroll position");
    check(settled.innerHTML === notes.map(note => '<p class="note">' + note + '</p>').join(""),
          "Refresh renders the settled notes in Spend");
    refreshed = JSON.stringify({...data, settled_notes: []});
    await loadAws();
    check(settled.children.length === 0, "Refresh removes obsolete settled notes");
    check(params().get("show") === "spot,cost" && params().get("types") === "standard"
          && params().get("runs") === "test-affected", "Refresh keeps the filter URL");
    const pressed = id => [...document.querySelectorAll(`#${id} [aria-pressed='true']`)].map(chip => chip.textContent).join(",");
    check(pressed("show") === "Spot price,Cost per gate" && pressed("types") === "Standard" && pressed("runs-chips") === "Targeted runs",
          "Refresh keeps the selected chips");
    for (const [values, indices, color] of [[[0.1, 0.1, 0.3], [0, 1], "low"], [[0.1, 0.3, 0.3], [1, 2], "high"]]) {
      const {colors} = scaleCells({values: values.map(value => [null, value])}, {rows: [0, 1, 2], columns: [1]});
      check(indices.every(index => colors.get(`${index},1`) === `var(--scale-${color})`), `Tied ${color} endpoint`);
    }
""")

    def test_old_merge_gate_url_selects_the_gate_scope(self) -> None:
        self.run_browser_checks('?prices=cost&runs=merge-gate', """
    check(scope === "gate" && scopeCosts().scope === "gate", "The old URL selects the gate scope");
""")

    def test_cost_switch_uses_the_configured_gate_name_without_standard_types(self) -> None:
        ledger_test_support.text_setting(self.root / 'build_steps.conf', 'names.gate', 'fixture gate')
        read_words = conf.ConfigRead.words

        def words(settings: conf.ConfigRead, key: str) -> tuple[str, ...]:
            if key in ('instance_types', 'price_compare_instance_types'):
                return ()
            return read_words(settings, key)

        with mock.patch.object(conf.ConfigRead, 'words', new=words):
            self.run_browser_checks('?prices=cost', """
    check(scopeCosts().tables.length === 0, "The gate scope has no tables");
    check(document.querySelector("#show [data-measure='cost']").textContent === "Cost per fixture gate",
          "The switch uses the configured gate name");
""")

    def test_quota_cells_and_gpu_summary_use_the_latest_request(self) -> None:
        gpu = {'state': 'requested', 'requested_vcpus': '32', 'created_at': '2026-01-11T10:00:00Z',
               'updated_at': '2026-01-15T10:00:00Z'}
        not_measured = {'state': 'not_measured',
                        'reason': 'The AWS plan key is locked. Run github-warmup in a terminal on natedev.'}
        self.run_browser_checks('?types=gpu&show=quota', """
    const summary = document.querySelector("#gpu-quota-summary");
    const cells = region => [...document.querySelectorAll("#price-table tbody tr")]
      .find(row => row.cells[0].textContent === region).cells;
    const request = region => [...cells(region)].slice(1, 5).map(cell => cell.textContent).join("|");
    const headers = [...document.querySelectorAll("#price-table thead th")].slice(2, 5);
    check(headers.map(cell => cell.textContent).join("|") === "wanted|request|request age", "Request headings");
    check(summary?.textContent === "G and VT quota (32 vCPUs): 1 granted · 1 open · 1 closed, not granted",
          "GPU progress across the three regions");
    check(request("us-east-2") === "32|32|granted|4 days", "The applied quota meets an open request");
    check(request("us-east-1") === "8|32|closed, not granted|4 days", "The applied quota is below the approved request");
    check(request("eu-west-1") === "8|32|open|2 hours", "Age uses the update time without a creation time");
    check([1, 2, 4].every(index => getComputedStyle(cells("us-east-2")[index]).textAlign === "right"),
          "Quota, wanted, and request age align right");
    const grantRegion = document.querySelector("#regions [data-region='us-east-2']");
    grantRegion.click();
    check(summary.textContent === "G and VT quota (32 vCPUs): 0 granted · 1 open · 1 closed, not granted",
          "The summary counts only the visible rows");
    grantRegion.click();
    document.querySelector("#types [data-types='standard']").click();
    check(request("us-east-2") === "137|—|not measured|—" && request("us-east-1") === "137|—|not measured|—",
          "Standard has its own request history");
    check(request("eu-west-1") === "137|—|—|—", "Empty history remains distinct");
    check(document.querySelector("#quota-request-note").textContent
          === "The plan identity is locked. Run github-warmup on natedev.", "One note for the locked identity");
    check(document.querySelectorAll("#price-table td").length === 21, "Three added columns for each region");
""", quota_history=(
            {'region': 'us-east-2', 'standard': not_measured, 'gpu': {**gpu, 'status': 'CASE_OPENED'}},
            {'region': 'us-east-1', 'standard': not_measured, 'gpu': {**gpu, 'status': 'APPROVED'}},
            {'region': 'eu-west-1', 'standard': {'state': 'no_request'},
             'gpu': {**gpu, 'status': 'PENDING', 'created_at': None}},
        ))

    def test_summary_omits_unknown_counts_before_the_first_request_history(self) -> None:
        self.run_browser_checks('?types=gpu&show=quota', """
    const summary = document.querySelector("#gpu-quota-summary");
    const prefix = "G and VT quota (32 vCPUs): ";
    check(summary.textContent === prefix + "3 granted · request history not measured yet",
          "The summary keeps known grants and omits unknown counts");
    const requests = [...document.querySelectorAll("#price-table .quota-request")];
    check(requests.length === 3 && requests.every(cell => cell.textContent === "not measured"),
          "All three request cells show that history is missing");
    const note = document.querySelector("#quota-request-note");
    check(!note.hidden && note.textContent === "The snapshot has no quota request history.", "One history note");
    document.querySelector("#regions .all:last-child").click();
    check(summary.textContent === prefix + "request history not measured yet",
          "The summary omits the grant count when no grant is visible");
""")

    def test_missing_history_note_requires_every_visible_row_to_lack_history(self) -> None:
        self.run_browser_checks('?types=gpu&show=quota', """
    const note = document.querySelector("#quota-request-note");
    const requests = [...document.querySelectorAll("#price-table .quota-request")];
    check(requests.some(cell => cell.textContent === "not measured"), "A missing history cell says not measured");
    check(requests.some(cell => cell.textContent === "—"), "An empty history has a record");
    check(note.hidden && note.textContent === "", "Mixed history has no whole-snapshot note");
    document.querySelector("#regions [data-region='us-east-1']").click();
    check(!note.hidden && note.textContent === "The snapshot has no quota request history.",
          "The note appears when all visible rows lack history");
""", quota_history=(
            {'region': 'us-east-1', 'standard': {'state': 'no_request'}, 'gpu': {'state': 'no_request'}},
        ))

    def test_url_selects_gpu_and_omits_gate(self) -> None:
        self.run_browser_checks('?show=spot,cost,quota&types=gpu&runs=both', """
    const table = document.querySelector("#price-table table");
    check(table.tHead.rows[0].cells[1].textContent === "G and VT quota vCPUs", "GPU quota");
    check(table.tHead.rows[0].cells[5].textContent === "g6.2xlarge (8 vCPUs)", "GPU type");
    check([...table.tHead.rows[1].cells].map(cell => cell.textContent).join(",") === "Spot USD/h,Fit boxes", "GPU omits Gate");
    check(table.tBodies[0].rows[0].cells[6].textContent === "4", "GPU Fit uses the GPU quota and vCPUs");
    check(document.querySelector("#runs-chips [data-scope='both']").getAttribute("aria-pressed") === "true", "Runs URL");
    document.querySelector("#show [data-measure='spot']").click();
    document.querySelector("#show [data-measure='quota']").click();
    const gateNote = document.querySelector("#gpu-gate-note");
    check(!gateNote.hidden && gateNote.textContent === "GPU types have no gate runs.", "GPU Gate-only note");
    document.querySelector("#types [data-types='standard']").click();
    check(new URLSearchParams(location.search).get("types") === "standard", "Types URL");
""")

    def test_hours_answer_and_disclosure_use_the_existing_rule(self) -> None:
        self.costs({'billed_hours_gap_max_ratio': '0.15'})
        checks = {
            ('c7a.4xlarge', 'us-east-1'): run_cost.hours_check(Decimal('1.5'), Decimal('1'), Decimal('.15')),
            ('c7a.4xlarge', 'us-east-2'): run_cost.hours_check(Decimal('1.5'), Decimal('1'), Decimal('.15'),
                                                             hana_reserved_hours=Decimal('.5')),
            ('m7a.8xlarge', 'us-west-1'): run_cost.hours_check(Decimal('0'), Decimal('2.1'), Decimal('.15'),
                                                             hana_reserved_hours=Decimal('1')),
            ('c7a.4xlarge', 'eu-west-1'): run_cost.hours_check(Decimal('5.7'), Decimal('2'), Decimal('.15'),
                                                             hana_reserved_hours=Decimal('1')),
        }
        with mock.patch.object(run_cost, 'hours_checks', return_value=checks):
            self.run_browser_checks('', """
    const section = document.getElementById("hours");
    const table = () => section.querySelector(":scope > .scroll table");
    const rows = () => table() ? [...table().tBodies[0].rows] : [];
    const answer = () => section.querySelector("p").textContent;
    const details = () => section.querySelector("details");
    const cells = row => [...row.cells].map(cell => cell.textContent).join("|");
    const toggleRegion = region => document.querySelector(`#regions [data-region='${region}']`).click();
    check(answer() === "Yes. AWS billed 4.2 hours more than our records, in 2 rows.", "Sum marked higher bills");
    const reported = answer().match(/billed ([0-9.]+) hours more than our records, in ([0-9]+) rows/);
    const visibleSum = rows().reduce((sum, row) => sum + Number(row.cells[4].textContent), 0);
    check(reported && visibleSum.toFixed(1) === reported[1] && rows().length === Number(reported[2]),
          "The amount and count in the answer match the visible rows");
    check(cells(table().tHead.rows[0]) === "type|region|AWS billed|our records|difference|Hana reserved", "Hours headings");
    check(table().tHead.rows[0].cells[5].title ===
          "The check adds these hours reserved for Hana in this region to our recorded hours when it checks for a higher bill.",
          "Hana reserved has a tooltip");
    check(rows().map(cells).join("\\n") === [
      "c7a.4xlarge|eu-west-1|5.7|2.0|+3.7|1.0",
      "c7a.4xlarge|us-east-1|1.5|1.0|+0.5|—"
    ].join("\\n"), "The visible rows are the marked higher bills");
    check([2, 3, 4, 5].every(index => getComputedStyle(rows()[0].cells[index]).textAlign === "right"),
          "Hours align right");
    check(!details().open && details().querySelector("summary").textContent === "All rows (4)", "Closed by default");
    details().querySelector("summary").click();
    check(details().open && details().querySelectorAll("tbody tr").length === 4, "Open to see all rows");
    toggleRegion("eu-west-1");
    check(answer() === "Yes. AWS billed 0.5 hours more than our records, in 1 row.", "One marked higher bill");
    toggleRegion("us-east-1");
    toggleRegion("us-west-1");
    check(answer() === "No. AWS billed 1.5 hours and our records show 1.0 hours. "
          + "No row is more than 15% above the sum of its recorded hours and the hours reserved for Hana.",
          "An in-limit higher bill explains the configured limit");
    check(!table() && details().open, "The region filter leaves only the disclosure open");
    check(details().querySelector("tbody tr").cells[5].textContent === "0.5", "The in-limit row used Hana reserved");
    toggleRegion("us-east-2");
    toggleRegion("us-west-1");
    check(answer() === "No. AWS billed 2.1 hours less than our records, in 1 row.", "One marked lower bill");
    check(!table(), "A marked lower bill stays out of the visible table");
    check(cells(details().querySelector("tbody tr")) === "m7a.8xlarge|us-west-1|0.0|2.1|-2.1",
          "All rows shows the marked lower bill and its negative difference");
""")

    def test_hours_answer_sums_the_displayed_differences(self) -> None:
        limit = Decimal('.15')
        self.costs({'billed_hours_gap_max_ratio': str(limit)})
        checks = {
            ('c7a.4xlarge', 'us-east-1'): run_cost.hours_check(Decimal('1.24'), Decimal('1.06'), limit),
            ('c7a.4xlarge', 'us-east-2'): run_cost.hours_check(Decimal('1.16'), Decimal('1.00'), limit),
        }
        with mock.patch.object(run_cost, 'hours_checks', return_value=checks):
            self.run_browser_checks('', """
    const section = document.getElementById("hours");
    const answer = () => section.querySelector("p").textContent;
    const rows = () => [...section.querySelectorAll(":scope > .scroll tbody tr")];
    const amounts = row => [...row.cells].slice(2, 5).map(cell => cell.textContent).join("|");
    check(rows().map(amounts).join(";") === "1.2|1.1|+0.2;1.2|1.0|+0.2",
          "Each difference rounds after subtraction");
    check(answer() === "Yes. AWS billed 0.4 hours more than our records, in 2 rows.",
          "The answer sums the displayed differences");
    document.querySelector("#regions [data-region='us-east-2']").click();
    check(answer() === "Yes. AWS billed 0.2 hours more than our records, in 1 row.",
          "The filtered row still matches the answer");
""")

    def test_hours_keep_unbilled_rows_and_regions_from_all_checks(self) -> None:
        limit = Decimal('.2')
        checks = {
            ('c7a.4xlarge', 'us-west-2'): run_cost.hours_check(None, Decimal('1.25'), limit),
            ('c7a.4xlarge', 'ca-west-1'): run_cost.hours_check(None, Decimal(0), limit,
                                                             hana_reserved_hours=Decimal('.5')),
        }
        with mock.patch.object(run_cost, 'hours_checks', return_value=checks):
            self.run_browser_checks('', """
    const section = document.getElementById("hours");
    const details = section.querySelector("details");
    check(section.querySelector("p").textContent === "No AWS bill is available yet for the recorded hours.",
          "The answer explains that every row is unbilled");
    check(!section.querySelector(":scope > .scroll"), "No unbilled row is marked");
    check(!details.open && details.querySelector("summary").textContent === "All rows (1)",
          "The closed disclosure includes the unbilled row");
    details.querySelector("summary").click();
    const cells = [...details.querySelector("tbody tr").cells].map(cell => cell.textContent);
    check(cells.join("|") === "c7a.4xlarge|us-west-2|—|1.2|—", "The unbilled row keeps its recorded hours");
    check(document.querySelector("#regions [data-region='us-west-2']"), "The filter includes the unbilled region");
    check(document.querySelector("#regions [data-region='ca-west-1']"),
          "The filter includes a region with a check and no displayed row");
""")


class AwsPagePlacementScoresTests(ledger_test_support.CostFixture):
    def test_page_shows_latest_median_count_and_missing_or_stale_scores(self) -> None:
        now = ledger_test_support.instant('2026-01-15T12:00:00Z')
        region, missing_region = 'us-east-2', 'eu-north-1'
        location = ledger.SpotPlacementRegion(region)
        zone = ledger.SpotPlacementZone(region, 'use2-az1')
        sizes = ('c7a.8xlarge', 'm7a.8xlarge')
        request = ledger.SpotPlacementRequest(ledger.SpotPlacementScope.REGION,
            ledger.SpotPlacementTypeSet.GATE_32_VCPU, 8, sizes, (region, missing_region))
        all_types = replace(request, type_set=ledger.SpotPlacementTypeSet.ALL_GATE_TYPES,
                            instance_types=(*sizes, 'c7a.4xlarge', 'm7a.4xlarge'))
        zonal = replace(request, scope=ledger.SpotPlacementScope.ZONE)
        failed = ledger.SpotPlacementFailure(replace(all_types, boxes=20), now, 'MaxConfigLimitExceededException')
        readings = (
            ledger.SpotPlacementSample(replace(all_types, boxes=4, instance_types=('c7a.4xlarge',)), now, ()),
            ledger.SpotPlacementSample(replace(request, boxes=20, regions=('retired-region',)), now, ()),
            ledger.SpotPlacementSample(replace(request, instance_types=sizes[::-1]), now - 3600,
                (ledger.SpotPlacementScore(location, 6),)),
            ledger.SpotPlacementSample(request, now, (ledger.SpotPlacementScore(location, 9),)),
            ledger.SpotPlacementSample(all_types, now - 3600, (ledger.SpotPlacementScore(location, 10),)),
            ledger.SpotPlacementSample(all_types, now, ()),
            ledger.SpotPlacementSample(zonal, now - 2 * 3600, (ledger.SpotPlacementScore(zone, 5),)),
            failed,
        )
        ledger.write_spot_placement_scores(self.state, readings, now)
        source = aws_page.publish(self.state, now, self.root / 'page', settings_path=self.settings).read_text()
        payload = source.split('<script id="aws-prices-data" type="application/json">', 1)[1].split('</script>', 1)[0]
        data = cast(aws_page.PageData, json.loads(payload))
        scores = data['placement_scores']
        self.assertEqual(scores['title'], 'Spot placement scores')
        self.assertEqual(scores['region_axis'], 'rows')
        self.assertEqual(scores['headers'], ['Region', 'Location', '32 vCPU (8 boxes)', 'All types (8 boxes)',
                                             'All types (20 boxes)'])
        rows = {tuple(row[:2]): row[2:] for row in scores['rows']}
        refused = 'Missing (MaxConfigLimitExceededException)'
        self.assertEqual(rows[region, 'Region'], ['9 / 7.5 / 2', 'Stale: 10 / 10 / 1', refused])
        self.assertEqual(rows[missing_region, 'Region'], ['Missing', 'Missing', refused])
        self.assertEqual(rows[region, 'use2-az1'], ['Stale: 5 / 5 / 1', 'Missing', 'Not sampled'])
        self.assertNotIn(('retired-region', 'Region'), rows)
        self.assertIn('<section class="card" id="placement-scores"></section>', source)
        self.assertIn('data.placement_scores', source)
        self.assertIn('AWS scores a request with fewer than three instance types lower. '
                      + 'Compare the 32-vCPU column with recorded launch results.', source)


class AwsPageGateTimePriceTests(ledger_test_support.CostFixture):
    def test_page_compares_the_lowest_run_cost_for_each_size_in_each_region(self) -> None:
        now = ledger_test_support.instant('2026-01-15T12:00:00Z')
        east, west, north = 'us-east-2', 'us-west-2', 'eu-north-1'
        candidate_only = 'eu-south-2'
        undeployed_type = 'c7i.4xlarge'
        unmeasured_type = 'c7a.2xlarge'
        slow_type = 'm7a.4xlarge'
        deployed_types = ('c7a.4xlarge', 'c7a.8xlarge', slow_type, 'm7a.8xlarge', unmeasured_type)
        _ = (self.root / 'build_steps.conf').write_text('names.gate=gate\n')
        self.text_setting('region', east)
        config = self.root / 'cloud_gate.conf'
        ledger_test_support.override_fixture_settings(config,
            f'regions={east} {west} {north}\nspot_candidate_regions={west} {east} {candidate_only}\n'
            + f'instance_types={" ".join((*deployed_types, undeployed_type))}\n'
            + 'gate_wait_usd_per_minute=0.02\nmax_gate_minutes=12\n'
            + f'region_transfer_usd={json.dumps({east: 0.30, west: 0.08})}\n'
            + f'region_extra_gate_seconds={json.dumps({west: 30})}\n')
        deployment = self.state / 'gate-deployment.json'
        deployed_regions = [
            {'region': east, 'subnets': [{'id': 'subnet-east-a', 'zone': east + 'a'},
                                         {'id': 'subnet-east-c', 'zone': east + 'c'}]},
            {'region': west, 'subnets': [{'id': 'subnet-west', 'zone': west + 'a'}]},
        ]
        _ = deployment.write_text(json.dumps({'instance_types': deployed_types, 'regions': deployed_regions}))
        history: list[dict[str, object]] = []
        prices: list[ledger.RegionalSpotPrice] = []
        for size, rate, seconds in (('c7a.4xlarge', '0.6', 600), (slow_type, '0.6', 780),
                                    ('c7a.8xlarge', '2.4', 360), ('m7a.8xlarge', '1.8', 300)):
            base = {'attempt': size, 'instance_type': size, 'region': east, 'mode': 'gate'}
            history.extend(({**base, 'result': 'launched', 'time': now - 1000},
                            {**base, 'result': 'finished', 'time': now - 1000 + seconds, 'gate_s': seconds - 60}))
            prices.append(ledger.RegionalSpotPrice(east, east + 'a', size, Decimal(rate), now))
        prices.extend((
            ledger.RegionalSpotPrice(east, east + 'b', 'c7a.4xlarge', Decimal('0.006'), now),
            ledger.RegionalSpotPrice(east, east + 'c', 'c7a.4xlarge', Decimal('0.72'), now),
            ledger.RegionalSpotPrice(east, east + 'a', undeployed_type, Decimal('0.012'), now),
            ledger.RegionalSpotPrice(east, east + 'a', unmeasured_type, Decimal('0.1'), now),
            ledger.RegionalSpotPrice(east, east + 'b', 'm7a.4xlarge', Decimal('0.01'), now - 99999),
            ledger.RegionalSpotPrice(east, east + 'b', 'c7a.8xlarge', Decimal('0.01'), now),
            ledger.RegionalSpotPrice(east, east + 'b', 'm7a.8xlarge', Decimal('2'), now),
            ledger.RegionalSpotPrice(west, west + 'a', 'c7a.4xlarge', Decimal('0.6'), now),
            ledger.RegionalSpotPrice(west, west + 'a', 'm7a.8xlarge', Decimal('0.01'), now - 99999),
            ledger.RegionalSpotPrice(north, north + 'a', 'c7a.4xlarge', Decimal('0.01'), now),
        ))
        ledger.write_spot_prices(self.state, prices)
        history_path = self.state / 'cloud-gate-launches.jsonl'
        _ = history_path.write_text(
            ''.join(json.dumps(row) + '\n' for row in sorted(history, key=lambda row: int(str(row['time'])))))
        def publish() -> str:
            return aws_page.publish(self.state, now, self.root / 'page', settings_path=self.settings).read_text()

        def payload(source: str, name: str) -> object:
            raw = source.split(f'<script id="{name}" type="application/json">', 1)[1].split('</script>', 1)[0]
            return cast(object, json.loads(raw))

        source = publish()
        data = ledger.mapping(payload(source, 'aws-gate-time-data'))
        table = cast(aws_page.TableData, data['table'])
        self.assertEqual(table['title'], '32-vCPU gate break-even')
        self.assertEqual(table['region_axis'], 'rows')
        self.assertEqual(data['regions'], [east, west, north, candidate_only])
        self.assertEqual([row[0] for row in table['rows']], [east, west, north])
        self.assertEqual(table['rows'][0], [east, 'c7a.4xlarge', '0.6000', '10.0', '0.1000',
                                           'm7a.8xlarge', '1.8000', '5.0', '0.1500', '0.0100 or more'])
        self.assertEqual(table['rows'][1], [west, 'c7a.4xlarge', '0.6000', '10.0', '0.1900',
                                           *(['—'] * 4), 'no current price'])
        self.assertEqual(table['rows'][2], [north, *(['—'] * 8), 'no current price'])

        page = cast(aws_page.PageData, payload(source, 'aws-prices-data'))
        gates = next(costs for costs in page['runs'] if costs['scope'] == 'gate')
        gate_prices = gates['prices'][0]
        size = 'c7a.4xlarge'
        column = gate_prices['headers'].index(size)
        prices_by_region = {row[0]: row[column] for row in gate_prices['rows']}
        end = datetime.fromtimestamp(now, UTC)
        days = conf.read(self.settings).whole('run_cost_days')
        evidence = run_cost.run_evidence(history, end - timedelta(days=days), end, east)
        pricing = run_cost.GateRunPricing.from_config(conf.read(config), evidence, east, ('gate',))
        for row in table['rows'][:2]:
            region = row[0]
            with self.subTest(region=region):
                self.assertEqual(prices_by_region[region], row[4])
                self.assertEqual(prices_by_region[region], f'{pricing.expected_usd(Decimal("0.6"), size, region):.4f}')
                self.assertIn({'region': region, 'summary': f'Cheapest gate: {size} in {region}, {row[4]} USD (estimate)'},
                              gates['estimates'])
        spot_prices = page['prices'][0]
        for region, instance_type, spot, gate in (
            (east, size, 0.006, '0.1000'),
            (east, undeployed_type, 0.012, 'no current gate price'),
            (west, 'm7a.8xlarge', 0.01, 'no current gate price'),
        ):
            with self.subTest(region=region, instance_type=instance_type):
                index = next(index for index, row in enumerate(spot_prices['rows']) if row[0] == region)
                self.assertEqual(spot_prices['values'][index][spot_prices['headers'].index(instance_type)], spot)
                self.assertEqual(gate_prices['rows'][index][gate_prices['headers'].index(instance_type)], gate)
        for region, instance_type in ((east, undeployed_type), (west, 'm7a.8xlarge')):
            self.assertFalse(any(entry['region'] == region and instance_type in entry['summary']
                                 for entry in gates['estimates']))
        self.assertLess(source.index('id="placement-scores"'), source.index('id="gate-time-price"'))
        self.assertIn('fill(document.getElementById("gate-time-price"), data.gateTimePrice,', source)

        east_index = next(index for index, row in enumerate(gate_prices['rows']) if row[0] == east)
        with self.subTest(gate='13 minutes above the 12-minute limit'):
            slow_column = gate_prices['headers'].index(slow_type)
            self.assertEqual(gate_prices['rows'][east_index][slow_column], 'too slow')
            self.assertIsNone(gate_prices['values'][east_index][slow_column])
            self.assertFalse(any(slow_type in entry['summary'] for entry in gates['estimates']))
        with self.subTest(gate='current price with no run history'):
            empty_column = gate_prices['headers'].index(unmeasured_type)
            self.assertEqual(gate_prices['rows'][east_index][empty_column], 'no runs')
            self.assertIsNone(gate_prices['values'][east_index][empty_column])
            self.assertNotIn([east_index, empty_column], gate_prices['not_sold'])

        with self.subTest(prices='snapshot changes between reads'):
            updated_prices = tuple(replace(price, usd_per_hour=price.usd_per_hour * Decimal('1.5'))
                                   for price in prices)
            with mock.patch.object(ledger, 'read_spot_prices', side_effect=(tuple(prices), updated_prices)) as read:
                updated_source = publish()
            updated_page = cast(aws_page.PageData, payload(updated_source, 'aws-prices-data'))
            updated_gate = updated_page['runs'][0]['prices'][0]['rows'][east_index][column]
            updated_data = ledger.mapping(payload(updated_source, 'aws-gate-time-data'))
            updated_card = cast(aws_page.TableData, updated_data['table'])
            self.assertEqual(updated_gate, '0.1000')
            self.assertEqual(updated_card['rows'][0][4], updated_gate)
            read.assert_called_once_with(self.state)

        values, policy = conf.read(config), conf.read(self.settings)
        with self.subTest(deployment='valid types with a null entry'):
            _ = deployment.write_text(json.dumps({'instance_types': [None, *deployed_types], 'regions': deployed_regions}))
            comparisons = run_cost.gate_comparisons(self.state, now, values, policy)
            small = comparisons[0].small
            self.assertIsInstance(small, run_cost.GateRunEstimate)
            assert isinstance(small, run_cost.GateRunEstimate)
            self.assertEqual(small.instance_type, size)
            self.assertEqual(small.usd_per_run, Decimal('0.1'))
            gates = spot_table.spot_tables(self.state, now, settings_path=self.settings).runs[0]
            self.assertEqual(next(row[column] for row in gates.gate_prices.rows if row[0] == east), '0.1000')
        _ = deployment.write_text(json.dumps({'instance_types': deployed_types, 'regions': deployed_regions}))

        with self.subTest(history='launch before the cutoff and finish inside it'):
            cutoff = now - days * 86400
            crossing = {'attempt': 'cutoff', 'instance_type': size, 'region': east, 'mode': 'gate'}
            recent = [{**row, 'time': now - 460, 'gate_s': 480}
                      if row['attempt'] == slow_type and row['result'] == 'finished' else row for row in history]
            ordered = sorted(recent, key=lambda row: int(str(row['time'])))
            _ = history_path.write_text(
                json.dumps({**crossing, 'result': 'launched', 'time': cutoff - 120}) + '\n'
                + json.dumps({**crossing, 'result': 'finished', 'time': cutoff + 180, 'gate_s': 240}) + '\n'
                + ''.join(json.dumps(row) + '\n' for row in ordered))
            api = AwsCli('fixture', east, 1, execute=mock.Mock(side_effect=AssertionError('Unexpected AWS call')))
            worker = cloud_gate.GateWorker(api, ledger.CostLedger(self.state), self.settings, self.root / 'sidecar',
                                           clock=lambda: now)
            variants = run_cost.read_deployed_gate_variants(self.state, deployed_types, east)
            ranked = worker.rank_variants([row for row in variants if row.region == east
                                           and row.instance_type in (size, slow_type)], 'gate')
            self.assertEqual(ranked[0].instance_type, slow_type)
            gates = spot_table.spot_tables(self.state, now, settings_path=self.settings).runs[0]
            east_prices = next(row for row in gates.gate_prices.rows if row[0] == east)
            self.assertEqual(east_prices[column], '0.1000')
            self.assertEqual(east_prices[gates.gate_prices.headers.index(slow_type)], '0.0900')
            cheapest = next(entry for entry in gates.estimates if entry.region == east)
            self.assertEqual((cheapest.size, cheapest.usd), (ranked[0].instance_type, Decimal('0.09')))
            small = run_cost.gate_comparisons(self.state, now, values, policy)[0].small
            self.assertIsInstance(small, run_cost.GateRunEstimate)
            assert isinstance(small, run_cost.GateRunEstimate)
            self.assertEqual((small.instance_type, small.usd_per_run), (cheapest.size, cheapest.usd))
            recorded = next(table for table in gates.tables if table.title == size)
            self.assertEqual(next(row[2:4] for row in recorded.rows if row[0] == east), ('7.5', '2'))

        deployment.unlink()
        unpriced = run_cost.NoPricedGateVariant()
        expected = tuple(run_cost.GateTimeComparison(region, unpriced, unpriced) for region in (east, west, north))
        for label in ('missing', 'malformed'):
            with self.subTest(deployment=label):
                if label == 'malformed':
                    _ = deployment.write_text('{"regions":[{"region":"us-east-2","subnets":"invalid"}]}')
                tables = spot_table.spot_tables(self.state, now, settings_path=self.settings)
                self.assertFalse(tables.runs[0].estimates)
                self.assertTrue(all(cell == 'no current gate price' for row in tables.runs[0].gate_prices.rows
                                    for cell in row[1:]))
                comparisons = run_cost.gate_comparisons(self.state, now, conf.read(config), conf.read(self.settings))
                self.assertEqual(comparisons, expected)

    def test_page_refresh_redraws_the_break_even_table_and_region_filter(self) -> None:
        now = ledger_test_support.instant('2026-01-15T12:00:00Z')
        before = spot_table.Table('Break-even', ('Region', 'USD/min'), frozenset({1}),
                                  (('us-east-2', '0.0100'),), 'rows')
        after = replace(before, rows=(('us-east-2', '0.0900'), ('test-new-region', 'no current price')))
        with mock.patch.object(aws_page, 'gate_time_price_table', side_effect=(before, after)):
            first = aws_page.publish(self.state, now, self.root / 'page', settings_path=self.settings).read_text()
            second = aws_page.publish(self.state, now + 1, self.root / 'page', settings_path=self.settings).read_text()
        script = first.rsplit('<script>', 1)[1].split('</script>', 1)[0].replace(page_frame.REFRESH_SCRIPT, '')
        fixture = r'''
const assert = require("node:assert/strict");
const ids = new Map();
class Element {
  children = [];
  dataset = {};
  classList = {add() {}, toggle() {}};
  style = {};
  textContent = "";
  append(...children) { this.children.push(...children); }
  appendChild(child) { this.append(child); return child; }
  replaceChildren(...children) { this.children = children; }
  createTHead() { return this.appendChild(new Element()); }
  createTBody() { return this.appendChild(new Element()); }
  insertRow() { return this.appendChild(new Element()); }
  querySelector(selector) { return selector === "p" ? this.children[0] ?? null : null; }
  setAttribute() {}
  addEventListener() {}
}
const launchState = new Element();
launchState.append(new Element());
ids.set("launch-state", launchState);
function parse(html) {
  return {getElementById(id) {
    const start = '<script id="' + id + '" type="application/json">';
    return {textContent: html.split(start)[1].split("</script>")[0]};
  }};
}
const document = {
  getElementById(id) {
    if (id.endsWith("-data")) return parse(first).getElementById(id);
    if (!ids.has(id)) ids.set(id, new Element());
    return ids.get(id);
  },
  createElement: () => new Element(),
  createElementNS: () => new Element(),
  querySelectorAll: () => [],
};
const location = {pathname: "/", search: ""};
const fetch = async () => ({ok: true, text: async () => second});
class DOMParser { parseFromString(html) { return parse(html); } }
function addEventListener() {}
let reload;
function createPageRefresh(load) { reload = load; }
function words(node) {
  return typeof node === "string" ? node : [node.textContent, ...node.children.map(words)].join(" ");
}
'''
        check = r'''
(async () => {
  const table = ids.get("gate-time-price");
  assert.match(words(table), /0\.0100/);
  const result = await reload();
  assert.equal(result.readAt, data.read_at);
  assert.doesNotMatch(words(table), /0\.0100/);
  assert.match(words(table), /0\.0900/);
  assert.match(words(table), /test-new-region/);
  assert.match(words(ids.get("regions")), /test-new-region/);
})().catch(error => { console.error(error); process.exitCode = 1; });
'''
        node = shutil.which('node') or str(next(Path('/nix/store').glob('*nodejs*/bin/node')))
        source = f'const first = {json.dumps(first)};\nconst second = {json.dumps(second)};\n' + fixture + script + check
        result = subprocess.run([node, '-'], input=source, capture_output=True, text=True, timeout=20, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
