"""Supply private registered images for review write fixtures."""
from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from unittest import mock

from scripts.lint.memory_test_support import write_config_fixture
from scripts.production import change_review, change_routing
from scripts.production.change_review import Visible
from scripts.review_links import image_store

REVIEW_ENDPOINT_SETTINGS = """\
# whole: 1..65535
server_port=8765
# range: 0.001..
tailscale_timeout_seconds=10
# whole: 1..86400
snapshot_allocation_tries=10
"""

REVIEW_SERVER_SETTINGS = REVIEW_ENDPOINT_SETTINGS + """\
# range: 0.001..
tailscale_check_seconds=60
# range: 0.001..
code_check_seconds=5
# range: 0.001..
snapshot_sweep_seconds=3600
# range: 0..
snapshot_retention_seconds=604800
# range: 0..365
change_image_unclaimed_days=14
# range: 1..600
staff_snapshot_seconds=60
"""

REVIEW_DELIVERY_SETTINGS = """\
# whole: 1..20
delivery_tries=3
# range: 0..300
delivery_pause_seconds=5
# text: nonempty name
unrouted_alert_recipient=natedev
# range: 5..3600
unrouted_alert_retry_seconds=60
review_attachments_root=review-attachments
changes_first_root=natedev
"""

REVIEW_REFRESH_SETTINGS = """\
# range: 5..300
page_refresh_seconds=15
# range: 0.5..60
frame_retry_seconds=2
"""

REVIEW_DISPLAY_SETTINGS = REVIEW_REFRESH_SETTINGS + """\
# whole: 1..20
feedback_rows=5
# whole: 1..20
review_words_rows=4
# whole: 1..1440
later_soon_minutes=60
later_evening=18:00
later_evening_until=17:00
later_morning=09:00
later_week_day=Monday
later_week_time=09:00
# whole: 1..300
page_request_timeout_seconds=15
# whole: 1..5000
card_fade_out_ms=450
# whole: 1..5000
card_fade_in_ms=600
# whole: 1..80
list_log_max_height_rem=16
"""


REVIEW_ACCOUNT_SETTINGS = """\
# range: 1..120
accounts_page_seconds=30
# whole: 0..100
accounts_quota_warning_percent=10
# whole: 0..100
accounts_quota_urgent_percent=5
# whole: 1..86400
accounts_quota_same_window_seconds=3600
"""

ROLE_COLOR_CONFIG = (
    "role.showrunner=#a379fd\n"
    "role.unit-director=#ffaf02\n"
    "role.producer=#90fe42\n"
    "role.worker=#ff0404\n"
)


def write_change_review_settings(configuration: Path, root: Path) -> None:
    """Keep the lists and links under the test root."""
    _ = write_config_fixture(configuration / "change_review.conf", defaults=(
        f"lists_directory={root / 'lists'}\nreview_links_directory={root / 'links'}\n"))


def registered_fixture_image(root: Path, identity: str = "") -> str:
    """Register an image under an explicit private root."""
    if identity and image_store.change_image(root, identity):
        return identity
    root.mkdir(parents=True, exist_ok=True)
    image = root / "fixture.png"
    _ = image.write_bytes(b"private review image")
    if identity:
        with mock.patch("secrets.token_hex", return_value=identity):
            return image_store.register_file(image, root / image_store.CHANGE_FOLDER)["id"]
    return image_store.register_file(image, root / image_store.CHANGE_FOLDER)["id"]


def fixture_image() -> str:
    """Register a real image under the private test root."""
    if not os.environ.get("REVIEW_LINKS_ROOT"):
        raise ValueError("The fixture requires a private REVIEW_LINKS_ROOT")
    return registered_fixture_image(change_routing.review_root())


def pictured_changes(changes: Sequence[Visible | change_review.PublicationChange]) -> list[Visible | change_review.PublicationChange]:
    """Supply private images for visible changes in write fixtures."""
    result: list[Visible | change_review.PublicationChange] = []
    for change in changes:
        item = change.item if isinstance(change, change_review.PublicationChange) else change
        pictured = item if item.image else replace(item, image=fixture_image())
        result.append(replace(change, item=pictured) if isinstance(change, change_review.PublicationChange) else pictured)
    return result


def pictured_publication(change: change_review.PublicationChange) -> change_review.PublicationChange:
    """Supply an image before a fixture shares a publication change."""
    return change if change.item.image else replace(change, item=replace(change.item, image=fixture_image()))


def fixture_row_image(path: Path, row: str) -> str:
    """Keep a fixture row's image or supply a private image."""
    try:
        existing = next((item.get(change_review.IMAGE, "") for item in change_review.review_rows(path)
                         if item["#"] == row), "")
    except (change_review.ChangeReviewError, OSError):
        existing = ""
    return existing or fixture_image()


def pictured_review_lines(message: str) -> str:
    """Supply private images for valid trailer lines in write fixtures."""
    lines: list[str] = []
    for line in message.split("\n"):
        if line.startswith("Change-Review:"):
            item = change_review.parse(line.partition(":")[2])
            if isinstance(item, Visible) and not item.image:
                line += f" | image {fixture_image()}"
        lines.append(line)
    return "\n".join(lines)


