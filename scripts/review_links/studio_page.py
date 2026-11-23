"""Show The Studio: the List rows under one picture tab for each producer."""
from __future__ import annotations

import html
import re
from collections import Counter
from dataclasses import replace
from pathlib import Path
from random import choice
from typing import Literal

from scripts.lib import conf
from scripts.production import staff, the_list
from scripts.review_links import change_page, list_page, record_table

PAGE_PATH = f"/{change_page.CONSOLE_FOLDER}/studio.html"
AVATAR_PATH = f"/{change_page.CONSOLE_FOLDER}/studio/avatars"
# The All tab reads the folder "all"; each producer tab reads the folder named by its staff ID.
FOLDER = re.compile(r"all|[0-9a-f-]{36}")
PICTURE = re.compile(r"([0-9]{2})-(64|128|256)\.webp")
SIZES = ("64", "128", "256")
# The largest avatar file is about 21 KB.
MAX_AVATAR_BYTES = 64 * 1024


def page_url() -> str:
    """Return the fixed page path for config/console.conf."""
    return PAGE_PATH


def avatar_root() -> Path:
    """The folder named by studio_avatar_root in config/review_links.conf."""
    root = Path(conf.read_or_repository("review_links").text("studio_avatar_root")).expanduser()
    if not root.is_absolute():
        raise ValueError(f"studio_avatar_root: {root}: expected an absolute path or a path that starts with ~")
    return root.resolve()


def pictures(root: Path, folder: str) -> tuple[str, ...]:
    """Return each picture number that has every size. A missing or linked folder has none."""
    path = root / folder
    try:
        if not FOLDER.fullmatch(folder) or path.resolve() != path:
            return ()
        sizes: dict[str, set[str]] = {}
        for entry in path.iterdir():
            if (found := PICTURE.fullmatch(entry.name)) and not entry.is_symlink():
                sizes.setdefault(found[1], set()).add(found[2])
    except OSError:
        return ()
    return tuple(sorted(number for number, found in sizes.items() if found == set(SIZES)))


def tab_label(root: Path, chip: record_table.Chip) -> record_table.Chip:
    """Put a random picture, or the initial in a circle, above the chip name."""
    folder = chip.value.removeprefix("staff:")
    numbers = pictures(root, folder)
    if numbers:
        base = f"{AVATAR_PATH}/{folder}/{choice(numbers)}-"
        sources = ", ".join(f"{base}{size}.webp {size}w" for size in SIZES)
        picture = (f'<img data-keep-src="{html.escape(chip.value, quote=True)}" src="{base}128.webp"'
                   + f' srcset="{sources}" sizes="88px" width="88" height="88" alt="">')
    else:
        initial = html.escape(html.unescape(chip.label_html)[:1].upper())
        picture = f'<span class="studio-initial" aria-hidden="true">{initial}</span>'
    return replace(chip, label_html=picture + f'<span class="studio-name">{chip.label_html}</span>')


def tabs(members: staff.StaffSnapshot, producers: Counter[the_list.ItemOwner]) -> str:
    """Render the Producer filter as picture tabs: All, then each producer by name."""
    counts: Counter[the_list.ItemOwner] = Counter({row.id: 0 for row in staff.active(members, "producer")
                                                   if not isinstance(row.retirement, staff.RetiredProducer)})
    counts.update(producers)
    root = avatar_root()
    chips = [tab_label(root, chip) for chip in list_page.staff_chips(members, counts)]
    return '<div class="studio-tabs">' + record_table.chip_row('Producer', chips, 'all') + '</div>'


STYLE = '''<style>
.studio-tabs .record-filter-row{display:block;margin-bottom:12px}
.studio-tabs .record-filter-row>span{display:none}
.studio-tabs .record-filter-row>div{gap:10px}
.studio-tabs [data-filter-row] button{position:relative;display:block;width:88px;height:88px;padding:0;
border:2px solid var(--line);border-radius:12px;overflow:hidden;background:var(--card)}
.studio-tabs [data-filter-row] button[aria-pressed=true]{border-color:var(--filter-selected-bg);
box-shadow:0 0 0 2px var(--filter-selected-bg)}
.studio-tabs [data-filter-row] button::after{content:"";position:absolute;inset:auto 0 0;height:36px;
background:linear-gradient(transparent,rgb(0 0 0 / 72%))}
.studio-tabs img{display:block;width:100%;height:100%;object-fit:cover}
.studio-initial{position:absolute;top:10px;left:50%;translate:-50% 0;display:grid;place-items:center;width:44px;
height:44px;border-radius:50%;background:var(--filter-selected-bg);color:var(--filter-selected-ink);
font-size:20px;font-weight:600}
.studio-tabs :is(.studio-name,b){position:absolute;bottom:4px;z-index:1;color:#fff;font-size:12px;line-height:16px}
.studio-name{left:6px;right:24px;overflow:hidden;white-space:nowrap;text-overflow:ellipsis;text-align:left}
.studio-tabs b{right:6px}
</style>'''

STUDIO_VIEW = list_page.ListView("The Studio", STYLE, tabs, list_page.no_producer_control)


def render(root: Path, *, include_counts: bool = True, defer_counts: bool = False,
           status: change_page.DeliveryStatus | Literal["standalone"] = "standalone",
           review_records: list_page.ReviewRecords | list_page.LinkedReviewRead = list_page.LinkedReviewRead.FROM_DISK
           ) -> bytes:
    """Render the List rows in the Studio view."""
    return list_page.render(root, include_counts=include_counts, defer_counts=defer_counts, status=status,
                            review_records=review_records, view=STUDIO_VIEW)


def avatar(relative: str) -> bytes:
    """Read one picture below the avatar root. Raise FileNotFoundError for any other path."""
    folder, _, name = relative.partition("/")
    if not FOLDER.fullmatch(folder) or not PICTURE.fullmatch(name):
        raise FileNotFoundError(relative)
    path = avatar_root() / folder / name
    if path.resolve() != path:
        raise FileNotFoundError(relative)
    with path.open("rb") as stream:
        content = stream.read(MAX_AVATAR_BYTES + 1)
    if not 0 < len(content) <= MAX_AVATAR_BYTES:
        raise FileNotFoundError(relative)
    return content
