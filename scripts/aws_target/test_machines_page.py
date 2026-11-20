"""Check the AWS Machines page with a private projection and tune setting."""

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from http.server import ThreadingHTTPServer
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


def fixture_data(*, aws_closed: bool = False) -> machines_data.MachinesPageData:
    """Give the page three measured machines and two single-size workloads."""
    small = ledger.GateRateCandidate('c7a.4xlarge', 'eu-south-2')
    large = ledger.GateRateCandidate('c7a.8xlarge', 'eu-north-1')
    reserve = ledger.GateRateCandidate('m7a.8xlarge', 'us-east-2')
    choices = (
        machines_data.MachineChoice(small, 16, 721, D(445), D('.1092'), D('.0135'), True, 0, 721,
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
            D('14.14'), D('218'), D('.065'), limits[1]),
        machines_data.MachineStep(*bands[2], (reserve,), (
            machines_data.MachineRunShare(large, D('.978')), machines_data.MachineRunShare(reserve, D('.014')),
            machines_data.MachineRunShare(small, D('.008'))), D('15.10'), D('222'), D('.068'), limits[2]),
    )
    workload = machines_data.MeasuredWorkload('Merge checks', 'merge-gate', choices, small, 1075, D(4658),
        D('62.72'), ((1, 611), (2, 272), (3, 125), (4, 43), (5, 19), (6, 5)), D('1.50'), D('14.14'), steps,
        ('A month has 52 weeks divided by 12.', 'The measured check rate and share of Spot refusals repeat.',
         'The estimate uses a quota of 136 vCPUs for Sweden.'))
    return machines_data.MachinesPageData(START, END, D('3.00'), D('.05'),
        machines_data.SavedSpotPriceRead(PRICE_READ, aws_closed), (workload,
        machines_data.WorkloadWithoutComparison('Named tests', 'targeted-test',
            'The records hold 608 completed checks on 1 machine size in this period.'),
        machines_data.WorkloadWithoutComparison('Affected tests', 'test-affected',
            'The records hold 38 completed checks on 1 machine size in this period.')))


class _PageServer(ThreadingHTTPServer):
    new_item_token: str = 'fixture-token'


class MachinesPageTests(unittest.TestCase):
    root: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    gate: Path  # pyright: ignore[reportUninitializedInstanceVariable]

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        _ = cast(object, self.enterContext(patch.dict(os.environ, {'CONF_DIR': str(self.root), 'USER_ZONE': 'UTC'})))
        _ = (self.root / 'console.conf').write_text(CONSOLE_SETTINGS)
        _ = (self.root / 'aws_page.conf').write_text("""# range: 1..10
machines.ratio_max=3.00
# range: 0.01..1
machines.ratio_step=0.05
""")
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
        unavailable = machines_data.WorkloadWithoutComparison('Merge checks', 'merge-gate',
            'No eligible measured choice has a saved price.')
        page = machines_page.render(replace(data, workloads=(unavailable, *data.workloads[1:]),
            price_read=machines_data.NoSavedSpotPriceRead()), NOW)
        self.assertIn(unavailable.sentence, page)
        self.assertIn('No saved Spot prices', page)
        self.assertNotIn('id="machine-controls"', page)

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
  check(document.querySelector('.other-workloads').innerText.includes('608 completed checks on 1 machine size'), 'Named tests');
  check(document.querySelector('.other-workloads').innerText.includes('38 completed checks on 1 machine size'), 'Affected tests');
  check(document.querySelectorAll('.sample').length === 2, 'Small samples');
  check(slider.valueAsNumber === 1.5 && amount.value === '31.36', 'Live ratio and ceiling');
  check(document.getElementById('extra-cost').textContent === '$14.14', 'The tile uses the estimate');
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
        'Change from 1.50 to 2.15. The estimate changes by +$0.96 a month.', 'Go confirmation');
  check(document.querySelector('.setting-slider-now').textContent === 'Now 1.50', 'Go leaves the live value until confirmation');
  const saved = new Promise(resolve => document.getElementById('save-limit').addEventListener('setting-saved', resolve, {once:true}));
  document.querySelector('[data-setting-save]').click();
  await saved;
  check(document.querySelector('.setting-slider-now').textContent === 'Now 2.15', 'The confirmed setting is live');
  check(go.disabled, 'The live value has no pending change');
 } catch (error) {
  result = String(error);
 }
 await fetch('/probe?result=' + encodeURIComponent(result));
})();
</script><img src="/finish" alt="" width="1" height="1">"""
        page = markup.replace('</body>', probe + '</body>').encode()
        done = threading.Event()
        results: list[str] = []

        class Handler(serve.ReviewLinksHandler):
            @override
            def do_GET(self) -> None:
                route = urlsplit(self.path)
                match route.path:
                    case console_tune.PAGE_PATH:
                        self.tune_token(route.query)
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
                        self.send_response(200)
                        self.send_header('Content-Type', 'text/html; charset=utf-8')
                        self.end_headers()
                        _ = self.wfile.write(page)
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
            _ = self.gate.write_text(f'{key}={value}\n')
            return subprocess.CompletedProcess(command, 0)

        profile = self.root / 'profile'
        profile.mkdir()
        with (_PageServer(('127.0.0.1', 0), Handler) as server,
              patch.object(console_tune, 'run', side_effect=tune) as run):
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                result = subprocess.run(('firefox', '--headless', '--no-remote',
                    '--profile', str(profile), '--window-size', '1300,900',
                    '--screenshot', str(self.root / 'browser.png'), f'http://127.0.0.1:{server.server_port}/machines.html'),
                    capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS, check=False)
            finally:
                server.shutdown()
                thread.join()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(results, ['passed'])
        self.assertEqual(run.call_args.args[0][tune_fields], ('cloud_gate', RATIO_KEY, '2.15'))
