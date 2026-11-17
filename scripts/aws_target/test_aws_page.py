"""Check console instructions on the published AWS page."""

from html import unescape
import re
import os
from unittest import mock

from scripts.aws_target import aws_page, console_help, ledger_test_support, quota_requests


class AwsPageQuotaHelpTests(ledger_test_support.CostFixture):
    def test_quota_help_is_closed_and_contains_the_ordered_steps_and_links(self) -> None:
        now = ledger_test_support.instant('2026-01-15T12:00:00Z')
        page = aws_page.publish(self.state, now, self.root / 'page', settings_path=self.settings)
        section = page.read_text().split('<h2 class="section-title">Quotas</h2>', 1)[1].split('<div id="quotas">', 1)[0]
        section = section[section.index('<details '):]
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

    def test_page_shows_open_requests_and_links_from_the_saved_record(self) -> None:
        now = ledger_test_support.instant('2026-01-15T12:00:00Z')
        request = quota_requests.OpenQuotaRequest('us-east-1', 'L-FIXTURE', 'Fixture Spot & GPU',
            '32', 'CASE_OPENED', 'fixture-case-id', '2026-01-15T10:00:00+00:00')
        quota_requests.save(self.state, {request.quota: (request,)}, now, complete=True)
        with mock.patch.dict(os.environ, {'USER_ZONE': 'America/New_York'}):
            page = aws_page.publish(self.state, now, self.root / 'page', settings_path=self.settings)
        html = page.read_text(encoding='utf-8')
        block = html.split('<h3 id="waiting-quota-title">Waiting on you</h3>', 1)[1].split('</section>', 1)[0]
        self.assertIn('Record updated 2026-01-15 07:00:00 EST.', block)
        self.assertIn('See the case list for the case status.', unescape(re.sub(r'<[^>]+>', '', block)))
        self.assertIn('<td>us-east-1</td>', block)
        self.assertIn(f'<a href="{console_help.quota_page_url(request.region, request.quota_code)}">Fixture Spot &amp; GPU</a>', block)
        self.assertIn('<td class="n">32</td><td>CASE_OPENED</td><td>2026-01-15 05:00:00 EST</td>', block)
        self.assertIn(f'<a href="{console_help.CASE_LIST_URL}">case list</a>', block)
        self.assertNotIn('fixture-case-id', html)

    def test_waiting_requests_are_visible_before_closed_quota_help(self) -> None:
        now = ledger_test_support.instant('2026-01-15T12:00:00Z')
        request = quota_requests.OpenQuotaRequest('us-east-1', 'L-FIXTURE', 'Fixture Spot',
            '32', 'PENDING', '', '')
        quota_requests.save(self.state, {request.quota: (request,)}, now, complete=True)
        page = aws_page.publish(self.state, now, self.root / 'page', settings_path=self.settings)
        section = page.read_text(encoding='utf-8').split('<h2 class="section-title">Quotas</h2>', 1)[1]
        visible, details = section.split('<details class="card" id="quota-help">', 1)
        self.assertIn('id="quota-requests"', visible)
        self.assertIn('Waiting on you', visible)
        self.assertNotIn('Waiting on you', details.split('</details>', 1)[0])

    def test_waiting_requests_use_shared_words_and_one_case_link(self) -> None:
        now = ledger_test_support.instant('2026-01-15T12:00:00Z')
        request = quota_requests.OpenQuotaRequest('us-east-1', 'L-FIXTURE', 'Fixture Spot',
            '32', 'PENDING', '', '')
        quota_requests.save(self.state, {request.quota: (request, request)}, now, complete=True)
        with (mock.patch.object(console_help, 'WAITING_QUOTA_HEADING', 'Fixture heading', create=True),
              mock.patch.object(console_help, 'QUOTA_CASE_STATUS', 'Open {case_list} for details.', create=True),
              mock.patch.object(console_help, 'CASE_LIST_LABEL', 'fixture cases', create=True)):
            block = aws_page.waiting_quota_requests(self.state)
        self.assertIn('>Fixture heading</h3>', block)
        self.assertIn(f'Open <a href="{console_help.CASE_LIST_URL}">fixture cases</a> for details.', block)
        self.assertEqual(block.count(console_help.CASE_LIST_URL), 1)
        self.assertNotIn('<th>Cases</th>', block)

    def test_page_has_no_list_without_open_requests(self) -> None:
        now = ledger_test_support.instant('2026-01-15T12:00:00Z')
        folder = self.root / 'page'
        page = aws_page.publish(self.state, now, folder, settings_path=self.settings)
        missing = page.read_text(encoding='utf-8')
        self.assertIn('No saved quota requests.', missing)
        self.assertNotIn('Waiting on you', missing)
        quota_requests.save(self.state, {}, now, complete=True)
        page = aws_page.publish(self.state, now, folder, settings_path=self.settings)
        closed = page.read_text(encoding='utf-8')
        self.assertNotIn('Waiting on you', closed)
        self.assertIn('<div id="quota-requests"></div>', closed)
