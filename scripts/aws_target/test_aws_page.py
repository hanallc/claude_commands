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

from scripts.aws_target import aws_page, console_help, ledger, ledger_test_support, quota_requests, run_cost, spot_table
from scripts.lib import conf
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.lint.memory_test_support import write_config_fixture
from scripts.review_links import page_frame


class AwsPageQuotaHelpTests(ledger_test_support.CostFixture):
    def test_quota_help_is_closed_and_contains_the_ordered_steps_and_links(self) -> None:
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
        for index, url in ((0, console_help.SETTINGS_URL), (4, console_help.CASE_LIST_URL)):
            self.assertIn(f'<a href="{url}">{url}</a>', steps[index])
        self.assertIn(console_help.WRONG_ACCOUNT, unescape(block))
        self.assertIn(console_help.PASSKEY_NOTE, unescape(block))

    def test_quota_help_uses_the_shared_words(self) -> None:
        with (mock.patch.object(console_help, 'QUOTA_CASE_STEPS', ('Open the test console.',)),
              mock.patch.object(console_help, 'WRONG_ACCOUNT', 'Choose the test account.'),
              mock.patch.object(console_help, 'PASSKEY_NOTE', 'Unlock the test manager.')):
            self.assertEqual(aws_page.quota_help(), '<ol><li>Open the test console.</li></ol>'
                             + '\n<p>Choose the test account.</p>\n<p>Unlock the test manager.</p>')

    def test_quota_request_uses_plain_labels_when_values_are_missing(self) -> None:
        applied = aws_page.AppliedQuotaUnavailable.NOT_MEASURED
        for status, expected in (
            ('APPROVED', 'closed, quota unknown'), ('IN_PROGRESS', 'open'), ('FIXTURE_UNKNOWN', 'status unknown'),
        ):
            with self.subTest(status=status):
                request = quota_requests.quota_request(status, Decimal(32), None, None)
                self.assertEqual(aws_page.quota_request_cell(request, applied, 0),
                                 {'text': f'32 vCPUs wanted · {expected}, age unknown'})
        reading = quota_requests.QuotaRequestReadFailed('fixture private diagnostic')
        self.assertEqual(aws_page.quota_request_cell(reading, applied, 0), {'text': 'request read failed'})

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
        _ = write_config_fixture(self.root / 'cloud_gate.conf', """
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
        rows = ({'instance_type': 'm7a.8xlarge', 'region': 'us-east-1', 'mode': 'merge-gate',
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


@unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for the AWS table checks")
class AwsCombinedTableTests(PageCostFixture):
    def fixture_page(self, *, blocked: bool = False,
                     quota_history: tuple[dict[str, object], ...] = ()) -> Path:
        now = ledger_test_support.instant('2026-01-15T12:00:00Z')
        regions = ('us-east-1', 'us-east-2', 'eu-west-1')
        standard = ('c7a.4xlarge', 'm7a.8xlarge')
        gpu = 'g6.2xlarge'
        config = self.root / 'cloud_gate.conf'
        for key, value in (
            ('spot_candidate_regions', ' '.join(reversed(regions))), ('instance_types', standard[0]),
            ('price_compare_instance_types', standard[1]), ('gpu_instance_types', gpu),
            ('hana_gpu_instance_types', gpu),
        ):
            ledger_test_support.text_setting(config, key, value)
        prices = [
            {'region': region, 'instance_type': size, 'usd_per_hour': usd}
            for region, row in zip(regions, (('0.1', '0.6', '0.7'), ('0.2', '0.5', '0.8'), ('0.3', '', '0.9')), strict=True)
            for size, usd in zip((*standard, gpu), row, strict=True) if usd
        ]
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
            events.append({'attempt': 'blocked-fixture', 'instance_type': standard[0], 'mode': 'merge-gate',
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

    def test_launch_state_sits_above_spend_and_updates_after_a_good_launch(self) -> None:
        self.run_browser_checks('', """
    const block = document.getElementById("launch-state");
    const message = () => block.querySelector("[role='status']").textContent;
    check(block.nextElementSibling.id === "spend", "Launch state sits above spend");
    check(block.dataset.kind === "refused", "Refusal style");
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
    const table = () => document.querySelector("#price-table table");
    const click = selector => document.querySelector(selector).click();
    const spot = () => table().tBodies[0].rows[0].cells[2];
    const gate = () => table().tBodies[0].rows[0].cells[3];
    const params = () => new URLSearchParams(location.search);
    const placement = document.querySelector("#placement-scores");
    const placementRows = () => [...placement.querySelectorAll("tbody tr")];
    check(placement.previousElementSibling.id === "prices-quotas", "Placement follows Prices and quotas");
    check(placementRows()[0].cells[3].textContent === "9 / 9 / 1", "Placement sample renders");
    check(placementRows().some(row => row.cells[0].textContent === "us-west-1")
          && table().tBodies[0].rows.length === 3, "A region with only scores does not get a price row");
    check(table().tHead.rows.length === 2, "Default grouped headers");
    check(table().tHead.rows[0].cells[2].textContent === "c7a.4xlarge (16 vCPUs)", "Type vCPU count");
    check(table().tHead.rows[1].cells[0].textContent === "Spot USD/h", "Spot unit");
    check(table().tHead.rows[1].cells[1].textContent === "Gate USD", "Gate unit");
    check(table().tHead.rows[1].cells[2].textContent === "Fit boxes", "Fit unit");
    const first = table().tBodies[0].rows[0];
    check(first.cells[1].firstChild.textContent === "137" && first.cells[4].textContent === "8", "Quota and Fit numbers");
    check([...table().querySelectorAll(".quota-request")].map(cell => cell.textContent).join(",")
          === "not measured,not measured,not measured", "Legacy snapshots have no request history");
    check(document.querySelector("#quota-request-note").textContent === "The snapshot has no quota request history.",
          "One note for the legacy table");
    check(first.cells[1].className === "n edge region-quota" && first.cells[4].className === "n", "No quota marks");
    check(spot().textContent === "0.1000" && spot().classList.contains("cheapest"), "Lowest Spot price");
    check(first.querySelector(".spot-status").textContent === "read failed", "Visible failed read");
    check(table().tBodies[0].rows[1].querySelector(".spot-status").textContent === "old", "Visible old read");
    check(first.cells[5].classList.contains("dearest"), "Highest Spot price");
    check(table().tBodies[0].rows[1].cells[6].classList.contains("cheapest"), "Lowest Gate cost in another cell");
    check(table().tBodies[0].rows[2].cells[3].classList.contains("dearest"), "Highest Gate cost");
    check(table().tBodies[0].rows[2].cells[2].classList.contains("end"), "Column box");
    check(table().tBodies[0].rows[2].cells[5].title === "Not sold here", "Unavailable type icon");
    const merge = gate().textContent;
    click("#runs-chips [data-scope='targeted-test']");
    check(gate().textContent !== merge, "Runs chips change the Gate values");
    check(document.querySelector("#filter [data-scope='targeted-test']").getAttribute("aria-pressed") === "true",
          "Runs chips update Cost per run");
    click("#regions [data-region='us-east-1']");
    check(table().tBodies[0].rows.length === 2 && spot().classList.contains("cheapest"), "Marks use the filtered regions");
    check(!placementRows().some(row => row.cells[0].textContent === "us-east-1"), "The region filter applies to placement scores");
    check(gate().classList.contains("cheapest") && table().tBodies[0].rows[0].cells[6].classList.contains("dearest"),
          "Gate marks use the filtered regions");
    click("#show [data-measure='cost']");
    check(document.querySelector("#runs-label").hidden && document.querySelector("#runs-chips").hidden, "Hide Runs");
    check(params().get("show") === "spot,quota" && params().get("runs") === "targeted-test", "Show and Runs URL");
    click("#show [data-measure='spot']");
    check(table().tHead.rows.length === 1 && table().tBodies[0].rows[0].cells.length === 4, "Fit alone");
    click("#show [data-measure='quota']");
    check(table().tBodies[0].rows[0].cells.length === 2 && params().get("show") === "", "Quota remains with all chips off");
    click("#show [data-measure='cost']");
    const gateOnly = table().tBodies[0].rows[0].cells;
    check(gateOnly[2].classList.contains("cheapest") && gateOnly[3].classList.contains("dearest"), "Gate marks with Gate alone");
    click("#show [data-measure='spot']");
    document.head.appendChild(element("style", "#price-table table { min-width: 1000px; }"));
    const scroller = () => document.querySelector("#price-table .scroll");
    check(innerWidth === 760 && scroller().scrollWidth > scroller().clientWidth, "Narrow scroll container");
    scroller().scrollLeft = 150;
    check(scroller().scrollLeft === 150, "Set scroll position before refresh");
    const prices = JSON.parse(document.getElementById("aws-prices-data").textContent);
    const refreshed = JSON.stringify({...prices, render_seconds: 12});
    const gateTime = document.getElementById("aws-gate-time-data").textContent;
    window.fetch = async () => ({ok: true, text: async () =>
      `<script type="application/json" id="aws-prices-data">${refreshed}</scr` + "ipt>"
      + `<script type="application/json" id="aws-gate-time-data">${gateTime}</scr` + "ipt>"});
    const status = await loadAws();
    check(status.renderSeconds === 12 && scroller().scrollLeft === 150, "Refresh keeps scroll position");
    check(params().get("show") === "spot,cost" && params().get("types") === "standard"
          && params().get("runs") === "targeted-test", "Refresh keeps the filter URL");
    const pressed = id => [...document.querySelectorAll(`#${id} [aria-pressed='true']`)].map(chip => chip.textContent).join(",");
    check(pressed("show") === "Spot price,Cost per gate" && pressed("types") === "Standard" && pressed("runs-chips") === "Targeted runs",
          "Refresh keeps the selected chips");
    for (const [values, indices, color] of [[[0.1, 0.1, 0.3], [0, 1], "low"], [[0.1, 0.3, 0.3], [1, 2], "high"]]) {
      const {colors} = scaleCells({values: values.map(value => [null, value])}, {rows: [0, 1, 2], columns: [1]});
      check(indices.every(index => colors.get(`${index},1`) === `var(--scale-${color})`), `Tied ${color} endpoint`);
    }
""")

    def test_quota_cells_and_gpu_summary_use_the_latest_request(self) -> None:
        gpu = {'state': 'requested', 'requested_vcpus': '32', 'created_at': '2026-01-11T10:00:00Z',
               'updated_at': '2026-01-15T10:00:00Z'}
        not_measured = {'state': 'not_measured',
                        'reason': 'The AWS plan key is locked. Run github-warmup in a terminal on natedev.'}
        self.run_browser_checks('?types=gpu&show=quota', """
    const summary = document.querySelector("#gpu-quota-summary");
    const cell = region => [...document.querySelectorAll("#price-table tbody tr")]
      .find(row => row.cells[0].textContent === region).cells[1];
    const request = region => cell(region).querySelector(".quota-request").textContent;
    check(summary?.textContent === "G and VT quota (32 vCPUs): 1 granted · 1 open · 1 closed, not granted",
          "GPU progress across the three regions");
    check(cell("us-east-2").firstChild.textContent === "32" && request("us-east-2") === "32 vCPUs wanted · granted, 4 days",
          "The applied quota meets an open request");
    check(cell("us-east-1").firstChild.textContent === "8"
          && request("us-east-1") === "32 vCPUs wanted · closed, not granted, 4 days",
          "The applied quota is below the approved request");
    check(request("eu-west-1") === "32 vCPUs wanted · open, 2 hours", "Age uses the update time without a creation time");
    const grantRegion = document.querySelector("#regions [data-region='us-east-2']");
    grantRegion.click();
    check(summary.textContent === "G and VT quota (32 vCPUs): 0 granted · 1 open · 1 closed, not granted",
          "The summary counts only the visible rows");
    grantRegion.click();
    document.querySelector("#types [data-types='standard']").click();
    check(request("us-east-2") === "not measured" && request("us-east-1") === "not measured",
          "Standard has its own request history");
    check(request("eu-west-1") === "no request", "Empty history remains distinct");
    check(document.querySelector("#quota-request-note").textContent
          === "The plan identity is locked. Run github-warmup on natedev.", "One note for the locked identity");
    check(document.querySelectorAll("#price-table td").length === 12, "No extra table column");
""", quota_history=(
            {'region': 'us-east-2', 'standard': not_measured, 'gpu': {**gpu, 'status': 'CASE_OPENED'}},
            {'region': 'us-east-1', 'standard': not_measured, 'gpu': {**gpu, 'status': 'APPROVED'}},
            {'region': 'eu-west-1', 'standard': {'state': 'no_request'},
             'gpu': {**gpu, 'status': 'PENDING', 'created_at': None}},
        ))

    def test_missing_history_note_requires_every_visible_row_to_lack_history(self) -> None:
        self.run_browser_checks('?types=gpu&show=quota', """
    const note = document.querySelector("#quota-request-note");
    const requests = [...document.querySelectorAll("#price-table .quota-request")];
    check(requests.some(cell => cell.textContent === "not measured"), "A missing history cell says not measured");
    check(requests.some(cell => cell.textContent === "no request"), "An empty history has a record");
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
    check(table.tHead.rows[0].cells[2].textContent === "g6.2xlarge (8 vCPUs)", "GPU type");
    check([...table.tHead.rows[1].cells].map(cell => cell.textContent).join(",") === "Spot USD/h,Fit boxes", "GPU omits Gate");
    check(table.tBodies[0].rows[0].cells[3].textContent === "4", "GPU Fit uses the GPU quota and vCPUs");
    check(document.querySelector("#runs-chips [data-scope='both']").getAttribute("aria-pressed") === "true", "Runs URL");
    document.querySelector("#show [data-measure='spot']").click();
    document.querySelector("#show [data-measure='quota']").click();
    const gateNote = document.querySelector("#gpu-gate-note");
    check(!gateNote.hidden && gateNote.textContent === "GPU types have no gate runs.", "GPU Gate-only note");
    document.querySelector("#types [data-types='standard']").click();
    check(new URLSearchParams(location.search).get("types") === "standard", "Types URL");
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
        self.text_setting('region', east)
        config = self.root / 'cloud_gate.conf'
        _ = write_config_fixture(config,
            f'regions={east} {west} {north}\nspot_candidate_regions={west} {east} {candidate_only}\n'
            + 'instance_types=c7a.4xlarge c7a.8xlarge m7a.4xlarge m7a.8xlarge\n'
            + 'gate_wait_usd_per_minute=0.02\nmax_gate_minutes=12\n'
            + f'region_transfer_usd={json.dumps({east: 0.30, west: 0.08})}\n'
            + f'region_extra_gate_seconds={json.dumps({west: 30})}\n')
        _ = (self.state / 'gate-deployment.json').write_text(json.dumps({'regions': [
            {'region': east, 'subnets': [{'id': 'subnet-east', 'zone': east + 'a'}]},
            {'region': west, 'subnets': [{'id': 'subnet-west', 'zone': west + 'a'}]},
        ]}))
        history: list[dict[str, object]] = []
        prices: list[ledger.RegionalSpotPrice] = []
        for size, rate, seconds in (('c7a.4xlarge', '0.6', 600), ('m7a.4xlarge', '0.05', 780),
                                    ('c7a.8xlarge', '2.4', 360), ('m7a.8xlarge', '1.8', 300)):
            base = {'attempt': size, 'instance_type': size, 'region': east, 'mode': 'merge-gate'}
            history.extend(({**base, 'result': 'launched', 'time': now - 1000},
                            {**base, 'result': 'finished', 'time': now - 1000 + seconds, 'gate_s': seconds - 60}))
            prices.append(ledger.RegionalSpotPrice(east, east + 'a', size, Decimal(rate), now))
        prices.extend((
            ledger.RegionalSpotPrice(east, east + 'b', 'm7a.4xlarge', Decimal('0.01'), now - 99999),
            ledger.RegionalSpotPrice(east, east + 'b', 'c7a.8xlarge', Decimal('0.01'), now),
            ledger.RegionalSpotPrice(east, east + 'b', 'm7a.8xlarge', Decimal('2'), now),
            ledger.RegionalSpotPrice(west, west + 'a', 'c7a.4xlarge', Decimal('0.6'), now),
            ledger.RegionalSpotPrice(west, west + 'a', 'm7a.8xlarge', Decimal('0.01'), now - 99999),
            ledger.RegionalSpotPrice(north, north + 'a', 'c7a.4xlarge', Decimal('0.01'), now),
        ))
        ledger.write_spot_prices(self.state, prices)
        _ = (self.state / 'cloud-gate-launches.jsonl').write_text(
            ''.join(json.dumps(row) + '\n' for row in sorted(history, key=lambda row: int(str(row['time'])))))
        source = aws_page.publish(self.state, now, self.root / 'page', settings_path=self.settings).read_text()
        payload = source.split('<script id="aws-gate-time-data" type="application/json">', 1)[1].split('</script>', 1)[0]
        data = ledger.mapping(cast(object, json.loads(payload)))
        table = cast(aws_page.TableData, data['table'])
        self.assertEqual(table['region_axis'], 'rows')
        self.assertEqual(data['regions'], [east, west, north, candidate_only])
        self.assertEqual([row[0] for row in table['rows']], [east, west, north])
        self.assertEqual(table['rows'][0], [east, 'c7a.4xlarge', '0.6000', '10.0', '0.1000',
                                           'm7a.8xlarge', '1.8000', '5.0', '0.1500', '0.0100 or more'])
        self.assertEqual(table['rows'][1], [west, 'c7a.4xlarge', '0.6000', '10.0', '0.1900',
                                           *(['—'] * 4), 'no current price'])
        self.assertEqual(table['rows'][2], [north, *(['—'] * 8), 'no current price'])

        payload = source.split('<script id="aws-prices-data" type="application/json">', 1)[1].split('</script>', 1)[0]
        page = cast(aws_page.PageData, json.loads(payload))
        merge_gates = next(costs for costs in page['runs'] if costs['scope'] == 'merge-gate')
        gate_prices = merge_gates['prices'][0]
        size = 'c7a.4xlarge'
        column = gate_prices['headers'].index(size)
        prices_by_region = {row[0]: row[column] for row in gate_prices['rows']}
        end = datetime.fromtimestamp(now, UTC)
        days = conf.read(self.settings).whole('run_cost_days')
        evidence = run_cost.run_evidence(history, end - timedelta(days=days), end, east)
        pricing = run_cost.GateRunPricing.from_config(conf.read(config), evidence, east, ('merge-gate',))
        for row in table['rows'][:2]:
            region = row[0]
            with self.subTest(region=region):
                self.assertEqual(prices_by_region[region], row[4])
                self.assertEqual(prices_by_region[region], f'{pricing.expected_usd(Decimal("0.6"), size, region):.4f}')
        self.assertLess(source.index('id="placement-scores"'), source.index('id="gate-time-price"'))
        self.assertIn('fill(document.getElementById("gate-time-price"), data.gateTimePrice,', source)

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
