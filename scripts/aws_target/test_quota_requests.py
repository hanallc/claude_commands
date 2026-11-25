"""Check the local record of open quota requests."""

from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from scripts.aws_target import quota_requests


class QuotaRequestTests(TestCase):
    def test_record_round_trip_keeps_only_open_requests(self) -> None:
        directory = Path(self.enterContext(TemporaryDirectory()))
        request = quota_requests.OpenQuotaRequest('us-east-1', 'L-FIXTURE', 'Fixture Spot',
            '32.5', 'CASE_OPENED', 'fixture-case', '2026-01-15T12:00:00+00:00')
        closed = tuple(replace(request, status=status) for status in (
            'APPROVED', 'DENIED', 'CLOSED', 'NOT_APPROVED', 'CASE_CLOSED', 'INVALID_REQUEST'))
        quota_requests.save(directory, {request.quota: (request, *closed)}, 100, complete=True)
        self.assertEqual(quota_requests.read(directory), quota_requests.QuotaRequestSnapshot(100, (request,)))
        for row in closed:
            self.assertNotIn(row.status, (directory / quota_requests.RECORD_PATH).read_text(encoding='utf-8'))

    def test_partial_read_removes_closed_requests_and_preserves_unread_quotas(self) -> None:
        directory = Path(self.enterContext(TemporaryDirectory()))
        request = quota_requests.OpenQuotaRequest('us-east-1', 'L-FIXTURE', 'Fixture Spot',
            '32', 'PENDING', '', '')
        unread = replace(request, region='us-west-2')
        quota_requests.save(directory, {request.quota: (request,), unread.quota: (unread,)}, 200, complete=True)
        quota_requests.save(directory, {request.quota: ()}, 250, complete=False)
        self.assertEqual(quota_requests.read(directory), quota_requests.QuotaRequestSnapshot(200, (unread,)))
        quota_requests.save(directory, {}, 300, complete=True)
        self.assertEqual(quota_requests.read(directory), quota_requests.QuotaRequestSnapshot(300, ()))

    def test_mixed_snapshot_keeps_the_old_read_time_until_all_rows_are_current(self) -> None:
        directory = Path(self.enterContext(TemporaryDirectory()))
        earlier = quota_requests.OpenQuotaRequest('us-east-1', 'L-FIXTURE', 'Fixture Spot', '32', 'PENDING', '', '')
        later = replace(earlier, region='us-west-2', status='CASE_OPENED', case_id='fixture-case')
        quota_requests.save(directory, {earlier.quota: (earlier,)}, 100, complete=True)
        quota_requests.save(directory, {later.quota: (later,)}, 200, complete=False)
        self.assertEqual(quota_requests.read(directory), quota_requests.QuotaRequestSnapshot(100, (earlier, later)))
        quota_requests.save(directory, {earlier.quota: (), later.quota: (later,)}, 300, complete=False)
        self.assertEqual(quota_requests.read(directory), quota_requests.QuotaRequestSnapshot(300, (later,)))
