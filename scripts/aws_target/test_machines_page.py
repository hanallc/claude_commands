"""Check the AWS Machines page with a private projection and tune setting."""

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from fractions import Fraction
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import os
import shutil
import subprocess
import tempfile
import threading
from typing import cast, override
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from scripts.aws_target import ledger, machines_data, machines_page
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.review_links import accounts_page, console_tune, serve

START = datetime(2026, 11, 13, tzinfo=UTC)
END = datetime(2026, 11, 20, tzinfo=UTC)
NOW = int(END.timestamp()) + 12 * 3600
PRICE_READ = NOW - 3600
RATIO_KEY = 'faster_box_max_cost_ratio'
WORKLOAD_NAME = 'gate'
PAGE_SETTINGS = """# range: 1..10
machines.ratio_max=3.00
# range: 0.01..1
machines.ratio_step=0.05
"""
CONSOLE_SETTINGS = """# range: 0..60
status.round_trip_limit_seconds=1
# range: 0..60
status.render_limit_seconds=1
# range: 0..60
status.reading_delay_seconds=0
# range: 0..10000
status.aws_machines.age_limit_seconds=3900
"""
D = Decimal


def cost_step(ratio: str, extra: str) -> machines_data.CostRatioStep:
    return machines_data.CostRatioStep(machines_data.CostRatioCutover(Fraction(ratio)), D(extra))


def fixture_data(*, aws_closed: bool = False) -> machines_data.MachinesPageData:
    """Give the page three measured machines and two single-size workloads."""
    small = ledger.GateRateCandidate('c7a.4xlarge', 'eu-south-2')
    large = ledger.GateRateCandidate('c7a.8xlarge', 'eu-north-1')
    reserve = ledger.GateRateCandidate('m7a.8xlarge', 'us-east-2')
    choices = (
        machines_data.MachineChoice(small, 16, 980, D(445), D('.1092'), D('.0135'), True, 0, 980,
                                    False, D(136), False, D(1), D(1), D(1)),
        machines_data.MachineChoice(large, 32, 30, D(248), D('.2424'), D('.0167'), True, 0, 30,
                                    True, D(136), True, D('.978'), D(1), D('.978')),
        machines_data.MachineChoice(reserve, 32, 65, D(220), D('.468'), D('.0286'), True, 39, 65,
                                    True, D(300), False, D(1), D('.625'), D('.625')),
    )
    bands = ((D('1'), D('1.20')), (D('1.25'), D('2.10')), (D('2.15'), D('3.00')))
    limits = tuple(tuple(machines_data.MachineLimit(D(tick) / 100, (D(tick) / 100 - 1) * D('62.72'))
                         for tick in range(int(start * 100), int(end * 100) + 1, 5)) for start, end in bands)
    steps = (
        machines_data.MachineStep(*bands[0], (), (machines_data.MachineRunShare(small, D(1)),),
                                  D(0), D(0), machines_data.NoHoursSaved(), limits[0]),
        machines_data.MachineStep(*bands[1], (large,), (
            machines_data.MachineRunShare(large, D('.978')), machines_data.MachineRunShare(small, D('.022'))),
            D('14.14'), D(218), D('14.14') / D(218), limits[1]),
        machines_data.MachineStep(*bands[2], (reserve,), (
            machines_data.MachineRunShare(large, D('.978')), machines_data.MachineRunShare(reserve, D('.014')),
            machines_data.MachineRunShare(small, D('.008'))), D('15.10'), D(222), D('15.10') / D(222), limits[2]),
    )
    workload = machines_data.MeasuredWorkload(WORKLOAD_NAME, 'merge-gate', choices, small, 1075, D(4658),
        D('62.72'), ((1, 611), (2, 272), (3, 125), (4, 43), (5, 19), (6, 5)), D('1.50'), D('14.14'), steps,
        (cost_step('1', '0'), cost_step('1.25', '14.14'), cost_step('2.15', '15.10')),
        ('A month has 52 weeks divided by 12.',
         f'The measured run rate, overlap, and share of Spot refusals repeat for {WORKLOAD_NAME}.',
         'The estimate uses a quota of 136 vCPUs for Sweden.'))
    return machines_data.MachinesPageData(START, END, D('3.00'), D('.05'),
        machines_data.SavedSpotPriceRead(PRICE_READ, aws_closed), (workload,
        machines_data.WorkloadWithoutComparison('targeted test', 'targeted-test',
            'The records for targeted test hold 608 completed runs on 1 machine size in this period.'),
        machines_data.WorkloadWithoutComparison('test affected', 'test-affected',
            'The records for test affected hold 38 completed runs on 1 machine size in this period.')))


def fixture_two_jobs() -> machines_data.MachinesPageData:
    """Give the two measured jobs different limits for their faster machines."""
    data = fixture_data()
    gate = data.workloads[0]
    assert isinstance(gate, machines_data.MeasuredWorkload)
    small, large = (replace(choice, checks=count, launched=count, small_sample=False)
                    for choice, count in zip(gate.choices[:2], (568, 40), strict=True))
    large = replace(large, quota_fit_share=D(1), spot_granted_share=D('.75'), free_share=D('.75'))
    base, extra, hours = D('35.28'), D('5.08'), D(70)
    limits = tuple(machines_data.MachineLimit(limit.ratio, (limit.ratio - 1) * base)
                   for step in gate.steps for limit in step.limits)
    steps = (
        replace(gate.steps[0], last_ratio=D('1.30'), limits=limits[:7]),
        machines_data.MachineStep(D('1.35'), D('3'), (large.machine,), (
            machines_data.MachineRunShare(large.machine, D('.75')),
            machines_data.MachineRunShare(small.machine, D('.25'))),
            extra, hours, extra / hours, limits[7:]),
    )
    targeted = machines_data.MeasuredWorkload('targeted test', 'targeted-test', (small, large), small.machine,
        608, D(2635), base, ((1, 608),), gate.live_ratio, extra, steps,
        (cost_step('1', '0'), cost_step('1.35', '5.08')), ('The measured run rate repeats.',))
    return replace(data, workloads=(gate, targeted, data.workloads[2]))


class _PageServer(ThreadingHTTPServer):
    bound_address: str = '127.0.0.1'
    new_item_token: str = 'fixture-token'


class MachinesPageTests(unittest.TestCase):
    root: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    gate: Path  # pyright: ignore[reportUninitializedInstanceVariable]

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        _ = cast(object, self.enterContext(patch.dict(os.environ, {'CONF_DIR': str(self.root), 'USER_ZONE': 'UTC'})))
        _ = (self.root / 'console.conf').write_text(CONSOLE_SETTINGS)
        _ = (self.root / 'aws_page.conf').write_text(PAGE_SETTINGS)
        self.gate = self.root / 'cloud_gate.conf'
        _ = self.gate.write_text(f'{RATIO_KEY}=1.50\n')

    def test_page_publishes_the_saved_projection_in_the_shared_folder(self) -> None:
        folder = self.root / 'pages'
        with patch.object(machines_data, 'read_machines_data', return_value=fixture_data()) as read:
            path = machines_page.publish(self.root, NOW, folder)
        read.assert_called_once_with(self.root, NOW)
        self.assertEqual(path, folder / 'machines.html')
        self.assertEqual(path.stat().st_mode & 0o777, 0o644)
        self.assertIn('<h1 class="page-title">AWS Machines</h1>', path.read_text())

    def test_page_with_no_comparison_shows_the_recorded_reason(self) -> None:
        data = fixture_data()
        unavailable = machines_data.WorkloadWithoutComparison(WORKLOAD_NAME, 'merge-gate',
            'The records for gate hold 1 completed run on 1 machine size in this period.')
        page = machines_page.render(replace(data, workloads=(unavailable, *data.workloads[1:]),
            price_read=machines_data.NoSavedSpotPriceRead()), NOW)
        self.assertIn(f'<h2>Other workloads</h2>\n<p>{unavailable.sentence}</p>', page)
        self.assertEqual(page.count(unavailable.sentence), 1)
        self.assertIn('No saved Spot prices', page)
        self.assertNotIn('id="machine-controls"', page)

    def test_page_uses_the_workload_name_in_the_details(self) -> None:
        data = fixture_two_jobs()
        workload = replace(data.workloads[0], name='Landing & validation')
        page = machines_page.render(replace(data, workloads=(workload, *data.workloads[1:])), NOW)
        self.assertIn('<h2>All jobs</h2>', page)
        self.assertIn('<h2>Where the jobs run · Landing &amp; validation', page)
        self.assertIn('Set the limit</button>', page)
        self.assertIn('1,075 completed runs for Landing &amp; validation', page)
        self.assertNotRegex(page.lower(), r'merge[- ](?:check|gate)')

    def test_page_shows_a_comparison_when_only_a_targeted_test_has_one(self) -> None:
        data = fixture_data()
        primary = data.workloads[0]
        assert isinstance(primary, machines_data.MeasuredWorkload)
        targeted = replace(primary, name='test named', mode='test-modules')
        page = machines_page.render(replace(data, workloads=(*data.workloads[1:], targeted)), NOW)
        self.assertIn('<h2>What we measured for test named</h2>', page)
        self.assertEqual(page.count('data-job-projection>'), 1)
        self.assertEqual(page.count('id="machine-controls"'), 1)
        self.assertNotIn('<select', page)

    def test_registry_reads_the_limits_from_private_settings(self) -> None:
        _ = (self.root / 'aws_page.conf').write_text("""# range: 1..10
machines.ratio_max=2.00
# range: 0.01..1
machines.ratio_step=0.25
""")
        with patch.object(console_tune, 'run') as run:
            for value, reason in (
                ('2.25', 'Enter a value from 1.00 to 2.00.'),
                ('1.30', 'Use steps of 0.25 from 1.00.'),
            ):
                with self.subTest(value=value):
                    outcome = console_tune.save('aws_machines', RATIO_KEY, value)
                    self.assertEqual(outcome.result, accounts_page.Refused(reason))
            run.assert_not_called()

    def test_registry_refuses_missing_or_invalid_live_limits(self) -> None:
        with (patch('scripts.lib.conf._committed_files', return_value=iter((PAGE_SETTINGS,))) as history,
              patch.object(console_tune, 'run') as run):
            for key, value in (('machines.ratio_max', '3.00'), ('machines.ratio_step', '0.05')):
                for replacement in ('', f'{key}=invalid\n'):
                    with self.subTest(key=key, replacement=replacement):
                        _ = (self.root / 'aws_page.conf').write_text(
                            PAGE_SETTINGS.replace(f'{key}={value}\n', replacement))
                        outcome = console_tune.save('aws_machines', RATIO_KEY, '1.50')
                        self.assertEqual(outcome.result, accounts_page.Refused(
                            f'Set a valid {key} in aws_page.conf.'))
            run.assert_not_called()
            history.assert_not_called()

    def test_fixed_slider_can_lower_a_higher_live_limit(self) -> None:
        data = fixture_data()
        merge = data.workloads[0]
        assert isinstance(merge, machines_data.MeasuredWorkload)
        off = replace(merge.steps[0], last_ratio=D(1), limits=merge.steps[0].limits[:1])
        page = machines_page.render(replace(data, ratio_max=D(1),
            workloads=(replace(merge, steps=(off,)), *data.workloads[1:])), NOW)
        self.assertIn('min="1.00" max="1"', page)
        self.assertIn('Now 1.50', page)
        self.assertIn('--setting-fill:0%;--setting-live:0%', page)

    @unittest.skipUnless(shutil.which('firefox'), 'Firefox is required for the Machines page checks')
    def test_browser_uses_saved_limits_and_confirms_the_registered_setting(self) -> None:
        markup = machines_page.render(fixture_data(aws_closed=True), NOW)
        probe = r"""<script>
(async () => {
 function check(condition, label) { if (!condition) throw new Error(label); }
 const slider = document.getElementById('cost-limit-value');
 const amount = document.getElementById('monthly-limit');
 const go = document.querySelector('[data-setting-go]');
 let result = 'passed';
 try {
  check(document.querySelector('.price-pill').innerText.includes('AWS is closed. Last saved prices'), 'Closed AWS price');
  check(document.querySelector('.price-pill time').dateTime === '2026-11-20T11:00:00+00:00', 'Saved price time');
  const otherWorkloads = document.querySelector('.other-workloads').innerText;
  check(otherWorkloads.includes('targeted test hold 608 completed runs on 1 machine size'), 'Targeted test');
  check(otherWorkloads.includes('test affected hold 38 completed runs on 1 machine size'), 'Test affected');
  check(!/\bmerge\b/i.test(document.querySelector('main').innerText), 'No old name in the visible page');
  check(document.querySelectorAll('.sample').length === 2, 'Small samples');
  check(slider.valueAsNumber === 1.5 && amount.value === '31.36', 'Live ratio and ceiling');
  check(go.textContent === 'Set the limit to 1.50', 'Go uses the shared limit');
  go.dispatchEvent(new Event('click'));
  document.querySelector('[data-setting-save]').click();
  check(document.querySelector('[data-setting-confirm]').hidden, 'An unchanged value has no confirmation');
  check(document.getElementById('extra-cost').textContent === '$14.14', 'The tile uses the estimate');
  check(document.querySelector('[data-job-extra]').textContent === '$14.14', 'One job matches the total');
  check(document.getElementById('saved-hour-cost').textContent ===
        document.querySelector('[data-step="1"]').lastElementChild.textContent, 'The table and tile use the same price rounding');
  check(document.getElementById('monthly-cap').textContent.includes('$31.36'), 'The cap uses the ceiling');
  const chart = document.getElementById('machine-chart');
  chart.dispatchEvent(new MouseEvent('click', {clientX: chart.getBoundingClientRect().right - 16}));
  check(slider.valueAsNumber === 3 && amount.value === '125.44', 'Clicking the chart selects the amount');
  slider.value = '1.25';
  slider.dispatchEvent(new Event('input', {bubbles:true}));
  check(amount.value === '15.68', 'Moving the slider sets the ceiling');
  amount.value = '31.37';
  amount.dispatchEvent(new Event('input', {bubbles:true}));
  check(slider.valueAsNumber === 1.5, 'Entering an amount selects the highest limit within it');
  check(amount.value === '31.37', 'The amount keeps the entered ceiling');
  document.querySelector('[data-step="2"] td').click();
  check(slider.valueAsNumber === 2.15 && amount.value === '72.13', 'Clicking a step selects its first limit');
  check(document.querySelector('[data-choice="2"] [data-share]').textContent === '1.4%', 'The distribution uses the saved share');
  go.click();
  check(document.querySelector('[data-setting-summary]').textContent ===
        'Change from 1.50 to 2.15. gate +$0.96 a month. Total +$0.96 a month.', 'Go confirmation');
  check(document.querySelector('.setting-slider-now').textContent === 'Now 1.50', 'Go leaves the live value until confirmation');
  const saved = new Promise(resolve => document.getElementById('save-limit').addEventListener('setting-saved', resolve, {once:true}));
  document.querySelector('[data-setting-save]').click();
  await saved;
  check(document.querySelector('.setting-slider-now').textContent === 'Now 2.15', 'The confirmed setting is live');
  check(go.disabled, 'The live value has no pending change');
  go.dispatchEvent(new Event('click'));
  document.querySelector('[data-setting-save]').click();
  check(document.querySelector('[data-setting-confirm]').hidden, 'The saved value has no confirmation');
 } catch (error) {
  result = String(error);
 }
 await fetch('/probe?result=' + encodeURIComponent(result));
})();
</script>"""
        self.browser(markup, probe, expected_saves=('2.15',))

    @unittest.skipUnless(shutil.which('firefox'), 'Firefox is required for the Machines page checks')
    def test_browser_shows_the_total_and_updates_both_jobs(self) -> None:
        markup = machines_page.render(fixture_two_jobs(), NOW)
        probe = """<script>
(async () => {
const jobs = [...document.querySelectorAll('[data-job-projection]')];
const slider = document.getElementById('cost-limit-value');
const amount = document.getElementById('monthly-limit');
const go = document.querySelector('[data-setting-go]');
const summary = document.querySelector('[data-setting-summary]');
const jobValues = selector => jobs.map(job => job.querySelector(selector).textContent);
function check(condition, label) { if (!condition) throw new Error(label); }
let result = 'passed';
try {
 check(jobValues('h2').join('|') === 'Where the jobs run · gate at 1.50|Where the jobs run · targeted test at 1.50',
       'Jobs are shown in data order');
 check(jobValues('[data-job-extra]').join('|') === '$14.14|$5.08', 'Job estimates');
 check(document.getElementById('extra-cost').textContent === '$19.22', 'Total estimate');
 check(document.getElementById('saved-hours').textContent === '288 h', 'Total hours');
 check(document.getElementById('saved-hour-cost').textContent === '$0.07', 'Cost per hour uses total hours');
 check(amount.value === '49.00' && document.getElementById('monthly-cap').textContent.includes('$49.00'),
       'The amount and the cap use the total ceiling');
 const steps = [...document.querySelectorAll('[data-step]')];
 check(steps[2].innerText.includes('targeted test') && steps[2].innerText.includes('c7a.8xlarge'),
       'The step names the job and the added machine');
 steps[1].querySelector('button').click();
 check(slider.valueAsNumber === 1.25 && amount.value === '24.50', 'A step selects its first limit');
 check(jobValues('[data-job-ratio]').join('|') === '1.25|1.25', 'Both jobs follow the slider');
 check(jobValues('[data-job-extra]').join('|') === '$14.14|$0.00', 'The targeted test has no extra cost at 1.25');
 check(jobValues('[data-choice="1"] [data-share]').join('|') === '97.8%|0%', 'Both jobs use their saved shares');
 go.click();
 check(summary.textContent === 'Change from 1.50 to 1.25. targeted test -$5.08 a month. Total -$5.08 a month.',
       'Confirmation names the one job that changes');
 slider.value = '1.55';
 slider.dispatchEvent(new Event('input', {bubbles:true}));
 go.click();
 check(summary.textContent === 'Change from 1.50 to 1.55. Total $0.00 a month.',
       'Confirmation names no job when the estimate stays the same');
 steps[0].querySelector('button').click();
 go.click();
 check(summary.textContent === 'Change from 1.50 to 1.00. gate -$14.14 a month, targeted test -$5.08 a month. '
       + 'Total -$19.22 a month.', 'Confirmation names both jobs and the total');
 const saved = new Promise(resolve => document.getElementById('save-limit').addEventListener('setting-saved', resolve, {once:true}));
 document.querySelector('[data-setting-save]').click();
 await saved;
 steps[2].querySelector('button').click();
 go.click();
 check(summary.textContent === 'Change from 1 to 1.35. gate +$14.14 a month, targeted test +$5.08 a month. '
       + 'Total +$19.22 a month.', 'Confirmation after save: ' + summary.textContent);
} catch (error) {
 result = String(error);
}
await fetch('/probe?result=' + encodeURIComponent(result));
})();
</script>"""
        self.browser(markup, probe, expected_saves=('1',))

    @unittest.skipUnless(shutil.which('firefox'), 'Firefox is required for the Machines page checks')
    def test_browser_uses_the_exact_live_text_and_its_step_for_confirmation(self) -> None:
        data = fixture_data()
        workload = data.workloads[0]
        assert isinstance(workload, machines_data.MeasuredWorkload)
        for live, live_extra, monthly_change in (
                ('1.24999999999999999', '4.00', '+$10.14'), ('1.2750', '14.14', '$0.00'),
                ('1.25000000000000001', '14.14', '$0.00')):
            with self.subTest(live=live):
                cost_steps = (*workload.cost_steps[:1], cost_step('1.23', '4'), *workload.cost_steps[1:])
                live_workload = replace(workload, live_ratio=D(live), live_extra_usd_per_month=D(live_extra),
                                        cost_steps=cost_steps)
                markup = machines_page.render(replace(data, workloads=(live_workload, *data.workloads[1:])), NOW)
                parts = f'gate {monthly_change} a month. ' if monthly_change != '$0.00' else ''
                expected = json.dumps(f'Change from {live} to 1.25. {parts}Total {monthly_change} a month.')
                probe = """<script>
const sliderInput = document.getElementById('cost-limit-value');
const untouched = previewOpen() === '';
sliderInput.value = '1.25';
sliderInput.dispatchEvent(new Event('input', {bubbles:true}));
const edited = previewOpen() === 'The cost limit preview is open.';
document.querySelector('[data-setting-go]').click();
const summary = document.querySelector('[data-setting-summary]').textContent;
const passed = untouched && edited && !document.querySelector('[data-setting-confirm]').hidden &&
 summary === EXPECTED;
void fetch('/probe?result=' + encodeURIComponent(passed ? 'passed' : JSON.stringify({untouched, edited, summary})));
</script>""".replace('EXPECTED', expected)
                self.browser(markup, probe)

    @unittest.skipUnless(shutil.which('firefox'), 'Firefox is required for the Machines page checks')
    def test_browser_uses_the_end_costs_for_live_ratios_outside_the_ranges(self) -> None:
        data = fixture_data()
        job = data.workloads[0]
        assert isinstance(job, machines_data.MeasuredWorkload)
        for live, selected, extra, change in (
                ('0.99', 1.0, '0.00', '+$14.14'), ('12', 3.0, '15.10', '-$0.96')):
            with self.subTest(live=live):
                workload = replace(job, live_ratio=D(live), live_extra_usd_per_month=D(extra))
                markup = machines_page.render(replace(data, workloads=(workload, *data.workloads[1:])), NOW)
                expected = [selected, f'${extra}', f'${extra}',
                            f'Change from {live} to 1.25. gate {change} a month. Total {change} a month.']
                probe = """<script>
const input = document.getElementById('cost-limit-value');
const initial = [input.valueAsNumber, document.getElementById('extra-cost').textContent,
                 document.querySelector('[data-job-extra]').textContent];
input.value = '1.25';
input.dispatchEvent(new Event('input', {bubbles:true}));
document.querySelector('[data-setting-go]').click();
const actual = JSON.stringify([...initial, document.querySelector('[data-setting-summary]').textContent]);
void fetch('/probe?result=' + encodeURIComponent(actual === JSON.stringify(EXPECTED) ? 'passed' : actual));
</script>""".replace('EXPECTED', json.dumps(expected))
                self.browser(markup, probe)

    @unittest.skipUnless(shutil.which('firefox'), 'Firefox is required for the Machines page checks')
    def test_browser_uses_the_off_step_live_ratio_from_a_tune_reply(self) -> None:
        data = fixture_data()
        workload = data.workloads[0]
        assert isinstance(workload, machines_data.MeasuredWorkload)
        steps = (workload.steps[0], replace(workload.steps[1], extra_usd_per_month=D('2.30')), workload.steps[2])
        workload = replace(workload, live_ratio=D('1.00'), live_extra_usd_per_month=D(0), steps=steps,
            cost_steps=(cost_step('1', '0'), cost_step('1.23', '2.30'), workload.cost_steps[2]))
        markup = machines_page.render(replace(data, workloads=(workload, *data.workloads[1:])), NOW)
        probe = """<script>
(async () => {
 const input = document.getElementById('cost-limit-value');
 const go = document.querySelector('[data-setting-go]');
 const root = document.getElementById('save-limit');
 function choose(value) {
  input.value = value;
  input.dispatchEvent(new Event('input', {bubbles:true}));
  go.click();
 }
 const saved = new Promise(resolve => root.addEventListener('setting-saved', resolve, {once:true}));
 choose('1.25');
 root.querySelector('[data-setting-save]').click();
 await saved;
 choose('1.20');
 const summary = root.querySelector('[data-setting-summary]').textContent;
 const expected = 'Change from 1.24 to 1.20. gate -$2.30 a month. Total -$2.30 a month.';
 await fetch('/probe?result=' + encodeURIComponent(summary === expected ? 'passed' : summary));
})();
</script>"""
        self.browser(markup, probe, expected_saves=('1.25',), tune_reply_live='1.24')

    @unittest.skipUnless(shutil.which('firefox'), 'Firefox is required for the Machines page checks')
    def test_browser_subtracts_the_displayed_amounts_in_cents(self) -> None:
        data = fixture_data()
        workload = data.workloads[0]
        assert isinstance(workload, machines_data.MeasuredWorkload)
        steps = (workload.steps[0], replace(workload.steps[1], extra_usd_per_month=D('1.010')),
                 replace(workload.steps[2], extra_usd_per_month=D('1.015')))
        workload = replace(workload, live_extra_usd_per_month=D('1.010'), steps=steps,
            cost_steps=(cost_step('1', '0'), cost_step('1.25', '1.010'), cost_step('2.15', '1.015')))
        markup = machines_page.render(replace(data, workloads=(workload, *data.workloads[1:])), NOW)
        probe = """<script>
const input = document.getElementById('cost-limit-value');
const tile = document.getElementById('extra-cost');
const before = tile.textContent;
input.value = '2.15';
input.dispatchEvent(new Event('input', {bubbles:true}));
document.querySelector('[data-setting-go]').click();
const summary = document.querySelector('[data-setting-summary]').textContent;
const expected = 'Change from 1.50 to 2.15. gate +$0.01 a month. Total +$0.01 a month.';
const passed = before === '$1.01' && tile.textContent === '$1.02' && summary === expected;
void fetch('/probe?result=' + encodeURIComponent(passed ? 'passed' : summary));
</script>"""
        self.browser(markup, probe)

    @unittest.skipUnless(shutil.which('firefox'), 'Firefox is required for the Machines page checks')
    def test_browser_refreshes_an_untouched_off_step_preview(self) -> None:
        data = fixture_data()
        workload = data.workloads[0]
        assert isinstance(workload, machines_data.MeasuredWorkload)
        live_workload = replace(workload, live_ratio=D('1.249'), live_extra_usd_per_month=D(0))
        data = replace(data, workloads=(live_workload, *data.workloads[1:]))
        probe = """<script>
(async()=>{
 const readAt = pageRefreshFacts().readAt;
 if (readAt === INITIAL) {
  if (previewOpen()) {
   await fetch('/probe?result=An+untouched+page+reports+a+preview');
   return;
  }
  await fetch('/publish-newer');
  document.dispatchEvent(new Event('visibilitychange'));
 } else {
  await fetch('/probe?result=' + (readAt === INITIAL + 1 ? 'passed' : 'Wrong+publication'));
 }
})();
</script>""".replace('INITIAL', str(NOW))
        self.browser(machines_page.render(data, NOW), probe, newer_markup=machines_page.render(data, NOW + 1))

    def browser(self, markup: str, probe: str, *, expected_saves: tuple[str, ...] = (), newer_markup: str = '',
                tune_reply_live: str = '') -> None:
        page = markup.replace('</body>', probe + '</body>').encode()
        newer = newer_markup.replace('</body>', probe + '</body>').encode()
        published_newer = threading.Event()
        done = threading.Event()
        results: list[str] = []
        saves: list[str] = []

        class Handler(serve.ReviewLinksHandler):
            @override
            def do_GET(self) -> None:
                route = urlsplit(self.path)
                match route.path:
                    case console_tune.PAGE_PATH:
                        self.tune_token(route.query)
                    case '/':
                        self.html_response(200, b'<!doctype html><iframe src="/machines.html" '
                                                + b'style="width:100%;height:1000px;border:0"></iframe>'
                                                + b'<img src="/finish" alt="" width="1" height="1">')
                    case '/publish-newer':
                        published_newer.set()
                        self.send_response(204)
                        self.end_headers()
                    case '/probe':
                        results.extend(parse_qs(route.query)['result'])
                        self.send_response(204)
                        self.end_headers()
                        done.set()
                    case '/finish':
                        _ = done.wait(15)
                        self.send_response(204)
                        self.end_headers()
                    case '/machines.html':
                        self.html_response(200, newer if published_newer.is_set() else page)
                    case _:
                        self.send_error(404)

            @override
            def do_POST(self) -> None:
                route = urlsplit(self.path)
                if route.path == console_tune.PAGE_PATH:
                    self.tune_post(route.query)
                else:
                    self.send_error(404)

            @override
            def tailscale_refusal(self) -> str:
                return ''

        tune_fields = slice(len(accounts_page.TUNE), len(accounts_page.TUNE) + 3)

        def tune(command: tuple[str, ...], **_options: object) -> subprocess.CompletedProcess[str]:
            document, key, value = command[tune_fields]
            self.assertEqual((document, key), ('cloud_gate', RATIO_KEY))
            saves.append(value)
            _ = self.gate.write_text(f'{key}={tune_reply_live or value}\n')
            return subprocess.CompletedProcess(command, 0)

        profile = Path(self.enterContext(tempfile.TemporaryDirectory(dir=self.root)))
        with (_PageServer((_PageServer.bound_address, 0), Handler) as server,
              patch.object(console_tune, 'run', side_effect=tune)):
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                result = subprocess.run(('firefox', '--headless', '--no-remote',
                    '--profile', str(profile), '--window-size', '1300,900',
                    '--screenshot', str(self.root / 'browser.png'),
                    f'http://{server.bound_address}:{server.server_port}/'),
                    capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS, check=False)
            finally:
                server.shutdown()
                thread.join()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(results, ['passed'])
        self.assertEqual(tuple(saves), expected_saves)
