"""Check the published AWS table and quota case instructions."""

from html import unescape
import json
from pathlib import Path
import re
import shutil
import subprocess
import unittest
from unittest import mock

from scripts.aws_target import aws_page, console_help, ledger_test_support
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


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
        self.run_browser_checks('?prices=cost', """
    const table = () => document.querySelector("#price-table table");
    const click = selector => document.querySelector(selector).click();
    const spot = () => table().tBodies[0].rows[0].cells[2];
    const gate = () => table().tBodies[0].rows[0].cells[3];
    const params = () => new URLSearchParams(location.search);
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
    click("#runs-chips [data-scope='targeted-test']");
    check(gate().textContent !== merge, "Runs chips change the Gate values");
    check(document.querySelector("#filter [data-scope='targeted-test']").getAttribute("aria-pressed") === "true",
          "Runs chips update Cost per run");
    click("#regions [data-region='us-east-1']");
    check(table().tBodies[0].rows.length === 2 && spot().classList.contains("cheapest"), "Marks use the filtered regions");
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
    const refreshed = JSON.stringify({...data, render_seconds: 12});
    window.fetch = async () => ({ok: true, text: async () =>
      `<script type="application/json" id="aws-prices-data">${refreshed}</scr` + "ipt>"});
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
