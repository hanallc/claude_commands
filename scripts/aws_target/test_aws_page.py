"""Check the published AWS table and quota case instructions."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from html import unescape
import json
from pathlib import Path
import re
import shutil
import subprocess
from typing import cast
import unittest
from unittest import mock

from scripts.aws_target import aws_page, console_help, ledger, ledger_test_support, run_cost, spot_table
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


@unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for the AWS table checks")
class AwsCombinedTableTests(ledger_test_support.CostFixture):
    def fixture_page(self) -> Path:
        now = ledger_test_support.instant('2026-01-15T12:00:00Z')
        regions = ('us-east-1', 'us-east-2', 'eu-west-1')
        standard = ('c7a.4xlarge', 'm7a.8xlarge')
        gpu = 'g6.2xlarge'
        config = self.root / 'cloud_gate.conf'
        _ = config.write_bytes((Path(__file__).resolve().parents[2] / 'config/cloud_gate.conf').read_bytes())
        for key, value in (
            ('spot_candidate_regions', ' '.join(reversed(regions))), ('instance_types', standard[0]),
            ('price_compare_instance_types', standard[1]), ('gpu_instance_types', gpu),
            ('hana_gpu_instance_types', gpu), ('gate_wait_usd_per_minute', '0'),
        ):
            ledger_test_support.text_setting(config, key, value)
        prices = [
            {'region': region, 'instance_type': size, 'usd_per_hour': usd}
            for region, row in zip(regions, (('0.1', '0.6', '0.7'), ('0.2', '0.5', '0.8'), ('0.3', '', '0.9')), strict=True)
            for size, usd in zip((*standard, gpu), row, strict=True) if usd
        ]
        readings = [
            {'region': region, 'read_at': now - age, 'standard_vcpus': '137', 'gpu_vcpus': '32'}
            for region, age in zip(regions, (240, 7201, 240), strict=True)
        ]
        failures = [{'region': regions[0], 'at': now - 120, 'read': 'price', 'error': 'FixtureError'}]
        _ = (self.state / 'spot-prices.json').write_text(json.dumps({
            'prices': prices, 'regions': readings, 'failures': failures,
        }))
        events: list[dict[str, object]] = []
        for mode, times in (('merge-gate', (3600, 360)), ('targeted-test', (600, 1800))):
            for index, (size, seconds) in enumerate(zip(standard, times, strict=True)):
                run = {'attempt': f'{mode}-{index}', 'instance_type': size, 'mode': mode, 'region': regions[0]}
                events.extend(({**run, 'time': now - 60 - seconds, 'result': 'launched'},
                               {**run, 'time': now - 60, 'result': 'finished', 'gate_s': seconds - 60}))
        _ = (self.state / 'cloud-gate-launches.jsonl').write_text(''.join(json.dumps(event) + '\n' for event in events))
        placement_regions = (regions[0], 'us-west-1')
        request = ledger.SpotPlacementRequest(ledger.SpotPlacementScope.REGION,
            ledger.SpotPlacementTypeSet.ALL_GATE_TYPES, 8, standard, placement_regions)
        ledger.write_spot_placement_scores(self.state, (ledger.SpotPlacementSample(request, now,
            (ledger.SpotPlacementScore(ledger.SpotPlacementRegion(regions[0]), 9),)),), now)
        return aws_page.publish(self.state, now, self.root / 'page', settings_path=self.settings)

    def run_browser_checks(self, query: str, checks: str) -> None:
        page = self.fixture_page()
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

    def test_default_table_marks_and_shared_filters(self) -> None:
        self.run_browser_checks('?prices=cost&runs=merge-gate', """
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
    check(first.cells[1].textContent === "137" && first.cells[4].textContent === "8", "Quota and Fit numbers");
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
    click("#runs-chips [data-scope='test-affected']");
    check(gate().textContent !== merge, "Runs chips change the Gate values");
    check(document.querySelector("#filter [data-scope='test-affected']").getAttribute("aria-pressed") === "true",
          "Runs chips update Cost per run");
    click("#regions [data-region='us-east-1']");
    check(table().tBodies[0].rows.length === 2 && spot().classList.contains("cheapest"), "Marks use the filtered regions");
    check(!placementRows().some(row => row.cells[0].textContent === "us-east-1"), "The region filter applies to placement scores");
    check(gate().classList.contains("cheapest") && table().tBodies[0].rows[0].cells[6].classList.contains("dearest"),
          "Gate marks use the filtered regions");
    click("#show [data-measure='cost']");
    check(document.querySelector("#runs-label").hidden && document.querySelector("#runs-chips").hidden, "Hide Runs");
    check(params().get("show") === "spot,quota" && params().get("runs") === "test-affected", "Show and Runs URL");
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
          && params().get("runs") === "test-affected", "Refresh keeps the filter URL");
    const pressed = id => [...document.querySelectorAll(`#${id} [aria-pressed='true']`)].map(chip => chip.textContent).join(",");
    check(pressed("show") === "Spot price,Cost per gate" && pressed("types") === "Standard" && pressed("runs-chips") === "Targeted runs",
          "Refresh keeps the selected chips");
    for (const [values, indices, color] of [[[0.1, 0.1, 0.3], [0, 1], "low"], [[0.1, 0.3, 0.3], [1, 2], "high"]]) {
      const {colors} = scaleCells({values: values.map(value => [null, value])}, {rows: [0, 1, 2], columns: [1]});
      check(indices.every(index => colors.get(`${index},1`) === `var(--scale-${color})`), `Tied ${color} endpoint`);
    }
""")

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
        _ = (self.root / 'build_steps.conf').write_text('names.gate=gate\n')
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
            base = {'attempt': size, 'instance_type': size, 'region': east, 'mode': 'gate'}
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
        self.assertEqual(table['title'], '32-vCPU gate break-even')
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
  querySelector() { return null; }
  setAttribute() {}
  addEventListener() {}
}
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
