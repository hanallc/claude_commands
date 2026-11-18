"""Check console instructions on the published AWS page."""

from dataclasses import replace
from decimal import Decimal
from html import unescape
import json
from pathlib import Path
import re
import shutil
import subprocess
from typing import cast
from unittest import mock

from scripts.aws_target import aws_page, console_help, ledger, ledger_test_support, spot_table
from scripts.lint.memory_test_support import write_config_fixture
from scripts.review_links import page_frame


class AwsPageQuotaHelpTests(ledger_test_support.CostFixture):
    def test_quota_help_is_closed_and_contains_the_ordered_steps_and_links(self) -> None:
        now = ledger_test_support.instant('2026-01-15T12:00:00Z')
        page = aws_page.publish(self.state, now, self.root / 'page', settings_path=self.settings)
        section = page.read_text().split('<h2 class="section-title">Quotas</h2>', 1)[1].split('<div id="quotas">', 1)[0]
        details = re.fullmatch(r'\s*<details class="card" id="quota-help">(.*?)</details>\s*', section, flags=re.S)
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
        _ = write_config_fixture(self.root / 'cloud_gate.conf',
            f'regions={east} {west} {north}\nspot_candidate_regions={west} {east} {candidate_only}\n'
            + 'instance_types=c7a.4xlarge c7a.8xlarge m7a.4xlarge m7a.8xlarge\n'
            + 'gate_wait_usd_per_minute=0\nmax_gate_minutes=12\n')
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
            ledger.RegionalSpotPrice(west, west + 'a', 'c7a.4xlarge', Decimal('0.01'), now - 99999),
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
        self.assertEqual(table['rows'][1:], [[region, *(['—'] * 8), 'no current price'] for region in (west, north)])
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
