"""Test the Studio picture tabs and the avatar file reads."""
from __future__ import annotations

import os
import re
import tempfile
import unittest
from collections import Counter
from dataclasses import replace
from pathlib import Path
from typing import cast, override
from unittest.mock import patch

from scripts.production import staff, the_list
from scripts.review_links import list_page, studio_page


def producer(identity: str, name: str) -> staff.StaffRow:
    return staff.StaffRow(staff.StaffId(identity), "producer", name, staff.StaffRoot(),
                          current_session=staff.StaffId(identity))


MORTY = producer("01a18dbb-7e8f-7641-b3dd-bb2071ab9d7a", "morty")
DWIGHT = producer("920b0a64-6eee-479e-8c07-e03e4dcd4ef6", "dwight")
BOB = producer("a898cb38-dbac-4751-b72b-d0b14ab9c38d", "bob")
BRAIN = replace(producer("01a19d07-c162-7b43-bb48-3ee6c913b3f0", "brain"), retirement=staff.RetiredProducer("opus"))
SHOWRUNNER = staff.StaffRow(staff.StaffId("20b0427d-945b-4c40-bb24-a6bec83dd648"), "showrunner", "sdlc",
                            staff.StaffRoot(), current_session=staff.StaffId("20b0427d-945b-4c40-bb24-a6bec83dd648"))
# A producer with List rows and no staff row.
GHOST = staff.StaffId("0f0f0f0f-0000-4000-8000-000000000001")
MEMBERS = staff.StaffSnapshot((MORTY, DWIGHT, BOB, BRAIN, SHOWRUNNER))
TAB = re.compile(r'data-filter-value="([^"]+)" aria-pressed="[a-z]+" data-filter-count="([0-9]*)">(.*?) <b>')


def tab_parts(markup: str) -> list[tuple[str, str, str]]:
    """Return the value, the count and the label of each tab."""
    return [cast(tuple[str, str, str], found.groups()) for found in TAB.finditer(markup)]


def last(numbers: tuple[str, ...]) -> str:
    return numbers[-1]


class StudioPageTests(unittest.TestCase):
    avatars: Path = Path()
    settings: Path = Path()

    @override
    def setUp(self) -> None:
        temporary = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="studio-page-test-"))).resolve()
        self.avatars = temporary / "avatars"
        configuration = temporary / "config"
        configuration.mkdir()
        self.settings = configuration / "review_links.conf"
        _ = self.settings.write_text(f"studio_avatar_root={self.avatars}\n")
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"CONF_DIR": str(configuration)})))

    def picture(self, folder: str, number: str, sizes: tuple[str, ...] = studio_page.SIZES) -> None:
        (self.avatars / folder).mkdir(parents=True, exist_ok=True)
        for size in sizes:
            _ = (self.avatars / folder / f"{number}-{size}.webp").write_bytes(b"RIFF" + number.encode())

    def markup(self, producers: Counter[the_list.ItemOwner]) -> str:
        identities = studio_page.producer_ids(MEMBERS, producers)
        counts = Counter({f"staff:{identity}": count for identity, count in producers.items()})
        counts["all"] = producers.total()
        return studio_page.tabs(list_page.staff_chips(MEMBERS, identities, counts), "all")

    def tabs(self, producers: Counter[the_list.ItemOwner]) -> list[tuple[str, str, str]]:
        return tab_parts(self.markup(producers))

    def test_tabs_put_all_first_then_each_active_producer_by_name(self) -> None:
        markup = self.markup(Counter({DWIGHT.id: 2, GHOST: 1}))
        self.assertTrue(markup.startswith('<div class="studio-tabs"><div class="record-filter-row" role="group"'
                                          + ' aria-label="Producer" data-filter-row="producer">'))
        self.assertEqual([(value, count) for value, count, _ in tab_parts(markup)],
                         [("all", "3"), (f"staff:{GHOST}", "1"), (f"staff:{BOB.id}", "0"),
                          (f"staff:{DWIGHT.id}", "2"), (f"staff:{MORTY.id}", "0")])
        self.assertIn('<span class="studio-name">dwight</span>', markup)
        self.assertNotIn(BRAIN.id, markup)
        self.assertNotIn(SHOWRUNNER.id, markup)

    def test_a_tab_without_a_complete_picture_shows_the_initial_in_a_circle(self) -> None:
        initials = [('<span class="studio-initial" aria-hidden="true">' + letter + '</span>'
                     + f'<span class="studio-name">{name}</span>')
                    for letter, name in (("A", "All"), ("B", "bob"), ("D", "dwight"), ("M", "morty"))]
        self.assertEqual([label for _, _, label in self.tabs(Counter())], initials)
        self.picture("all", "01")
        self.picture(BOB.id, "01", ("64", "128"))
        (self.avatars / DWIGHT.id).symlink_to(self.avatars / "all", target_is_directory=True)
        (self.avatars / MORTY.id).mkdir()
        for size in studio_page.SIZES:
            (self.avatars / MORTY.id / f"01-{size}.webp").symlink_to(self.avatars / "all" / f"01-{size}.webp")
        labels = [label for _, _, label in self.tabs(Counter())]
        self.assertIn('<img data-keep-src="all"', labels[0])
        self.assertEqual(labels[1:], initials[1:])

    def test_each_render_picks_one_complete_picture_with_the_chooser(self) -> None:
        for number in ("01", "02", "03"):
            self.picture(DWIGHT.id, number)
        self.picture(DWIGHT.id, "04", ("64", "256"))
        for name in ("5-64.webp", "06-32.webp", "notes.txt"):
            _ = (self.avatars / DWIGHT.id / name).write_bytes(b"RIFF")
        with patch.object(studio_page, "choice", side_effect=last) as chooser:
            tabs = self.tabs(Counter({DWIGHT.id: 1}))
        chooser.assert_called_once_with(("01", "02", "03"))
        base = f"{studio_page.AVATAR_PATH}/{DWIGHT.id}/03-"
        self.assertEqual(tabs[-2][2],
                         f'<img data-keep-src="staff:{DWIGHT.id}" src="{base}128.webp"'
                         + f' srcset="{base}64.webp 64w, {base}128.webp 128w, {base}256.webp 256w"'
                         + ' sizes="88px" width="88" height="88" alt=""><span class="studio-name">dwight</span>')

    def test_avatar_reads_only_a_named_picture_below_the_root(self) -> None:
        self.picture("all", "01")
        self.assertEqual(studio_page.avatar("all/01-64.webp"), b"RIFF01")
        _ = (self.avatars / "all" / "02-64.webp").write_bytes(b"x" * (studio_page.MAX_AVATAR_BYTES + 1))
        _ = (self.avatars / "all" / "03-64.webp").write_bytes(b"")
        (self.avatars / "all" / "04-64.webp").symlink_to(self.avatars / "all" / "01-64.webp")
        (self.avatars / BOB.id).symlink_to(self.avatars / "all", target_is_directory=True)
        for name in ("all/1-64.webp", "all/01-32.webp", "all/01-64.png", "../all/01-64.webp", "all/../01-64.webp",
                     "bob/01-64.webp", "all/09-64.webp", "all/02-64.webp", "all/03-64.webp", "all/04-64.webp",
                     f"{BOB.id}/01-64.webp", "all/01-64.webp/extra"):
            with self.subTest(name=name), self.assertRaises(FileNotFoundError):
                _ = studio_page.avatar(name)

    def test_the_avatar_root_is_absolute_or_starts_at_the_home_folder(self) -> None:
        home = self.avatars.parent / "home"
        _ = self.settings.write_text("studio_avatar_root=~/pictures\n")
        with patch.dict(os.environ, {"HOME": str(home)}):
            self.assertEqual(studio_page.avatar_root(), home / "pictures")
        _ = self.settings.write_text("studio_avatar_root=pictures\n")
        with self.assertRaises(ValueError):
            _ = studio_page.avatar_root()


if __name__ == "__main__":
    _ = unittest.main()
