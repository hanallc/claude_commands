"""End persistent Spot boxes: cancel each request first, then terminate its boxes.

AWS opens a persistent request again when its box is terminated, and launches a new box with no tags.
So every path that ends such a box calls end_boxes, which cancels before it terminates.
"""
from __future__ import annotations

from scripts.aws_target import ledger
from scripts.aws_target.aws_cli import AwsCli

LIVE_REQUEST_STATES = ('open', 'active', 'disabled')
LIVE_BOX_STATES = 'pending,running,stopping,stopped,shutting-down'


class SpotEndIncomplete(RuntimeError):
    """A request stays open or a box stays live after end_boxes."""


def _requests(api: AwsCli, tag: tuple[str, str], extra: set[str]) -> list[dict[str, object]]:
    rows = list(ledger.rows(api.call('describe-spot-instance-requests', '--filters', f'Name=tag:{tag[0]},Values={tag[1]}'),
                            'SpotInstanceRequests'))
    found = {ledger.text(row, 'SpotInstanceRequestId') for row in rows}
    missing = sorted(extra - found)
    if missing:
        rows = [*rows, *ledger.rows(api.call('describe-spot-instance-requests', '--spot-instance-request-ids', *missing),
                                    'SpotInstanceRequests')]
    return rows


def _boxes(api: AwsCli, tag: tuple[str, str], requests: set[str]) -> dict[str, str]:
    """Map each live box to its request; a box a request launched again has no tags, so read by request too."""
    filters = [f'Name=tag:{tag[0]},Values={tag[1]}']
    if requests:
        filters.append('Name=spot-instance-request-id,Values=' + ','.join(sorted(requests)))
    boxes: dict[str, str] = {}
    for item in filters:
        reply = api.call('describe-instances', '--filters', item, 'Name=instance-state-name,Values=' + LIVE_BOX_STATES)
        for reservation in ledger.rows(reply, 'Reservations'):
            for row in ledger.rows(reservation, 'Instances'):
                boxes[ledger.text(row, 'InstanceId')] = str(row.get('SpotInstanceRequestId', ''))
    return boxes


def end_boxes(api: AwsCli, tag: tuple[str, str], *, keep: str = '', wait_s: float = 900) -> int:
    """Cancel every live request of the tagged boxes, then terminate the boxes, then prove both are gone.

    keep names one box that stays, with its request. A failed cancel raises before any terminate.
    Returns the count of boxes terminated."""
    boxes = _boxes(api, tag, set())
    kept_request = boxes.get(keep, '') if keep else ''
    known = {request for box, request in boxes.items() if request and box != keep}
    requests = [row for row in _requests(api, tag, known) if ledger.text(row, 'SpotInstanceRequestId') != kept_request]
    cancel = sorted(ledger.text(row, 'SpotInstanceRequestId') for row in requests if row.get('State') in LIVE_REQUEST_STATES)
    if cancel:
        _ = api.call('cancel-spot-instance-requests', '--spot-instance-request-ids', *cancel)
    ids = {ledger.text(row, 'SpotInstanceRequestId') for row in requests}
    # Read again after the cancel: a request can launch a box between the first read and the cancel.
    ended = sorted(box for box in _boxes(api, tag, ids) if box != keep)
    if ended:
        _ = api.call('terminate-instances', '--instance-ids', *ended)
        _ = api.call('wait', 'instance-terminated', '--instance-ids', *ended, timeout_s=wait_s)
    left_requests = [row for row in _requests(api, tag, ids)
                     if ledger.text(row, 'SpotInstanceRequestId') != kept_request and row.get('State') in LIVE_REQUEST_STATES]
    left_boxes = [box for box in _boxes(api, tag, ids) if box != keep]
    if left_requests or left_boxes:
        raise SpotEndIncomplete(f'{len(left_requests)} Spot requests stay open and {len(left_boxes)} boxes stay live')
    return len(ended)
