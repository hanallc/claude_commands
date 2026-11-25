"""Test role colors with private staff rows and configuration files."""
from __future__ import annotations

from dataclasses import replace
import os
import re
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.accounts import accounts
from scripts.production import staff
from scripts.review_links import staff_names


def without_role_colors(markup: str) -> str:
    """Keep layout assertions independent of the new name color spans."""
    markup = re.sub(r'<span class="staff-name" data-color-text="role\.[^"]+" style="color:#[a-fA-F0-9]{6}">([^<]*)</span>',
                    r'\1', markup)
    return re.sub(r'(<option\b[^>]*?) style="color:#[a-fA-F0-9]{6}"', r'\1', markup)


class StaffNameTests(unittest.TestCase):
    def test_role_labels_use_accounts_colors_and_escape_the_text(self) -> None:
        colors = {"role.showrunner": "#123456", "role.unit-director": "yellow",
                  "role.worker": "bright-blue", "role.producer": "pink"}
        with patch.object(accounts, "role_colors", return_value=colors):
            names = staff_names.StaffNames(staff.StaffSnapshot(()))
        for role, group in (("producer", "producers"), ("showrunner", "showrunners"), ("unit", "units"),
                            ("unit director", "units"), ("merge director", "units"), ("style director", "units"),
                            ("worker", "workers")):
            with self.subTest(role=role):
                color = accounts.role_color(group, colors)
                assert isinstance(color, accounts.StaffRoleColor)
                self.assertEqual(names.role_label(role, '<Role & "text">'),
                    f'<span class="staff-name" data-color-text="{color.key}" style="color:{color.css}">'
                    + '&lt;Role &amp; &quot;text&quot;&gt;</span>')
        self.assertEqual(names.role_label("unlisted", "<Role>"), "&lt;Role&gt;")
        with patch.object(accounts, "role_colors", return_value={"role.worker": "invalid"}):
            uncolored = staff_names.StaffNames(staff.StaffSnapshot(()))
        self.assertEqual(uncolored.role_label("worker", "Worker"), "Worker")
        self.assertEqual(uncolored.role_label("unit", "Unit"), "Unit")
        self.assertEqual(uncolored.role_labels(), {})

    def test_every_role_uses_the_accounts_keys_and_current_config(self) -> None:
        kinds: tuple[staff.Kind, ...] = ("showrunner", "unit", "worker", "producer")
        rows = tuple(staff.StaffRow(staff.StaffId(kind), kind, "Name " + kind, staff.StaffRoot(),
                                   current_session=staff.StaffId(kind)) for kind in kinds)
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"CONF_DIR": directory}):
            path = Path(directory) / "statusline.conf"
            _ = path.write_text("role.showrunner=#123456\nrole.unit-director=yellow\nrole.worker=bright-blue\nrole.producer=pink\n")
            names = staff_names.StaffNames(staff.StaffSnapshot(rows))
            for row, group in zip(rows, ("showrunners", "units", "workers", "producers")):
                color = accounts.staff_role_color(row, names.colors)
                self.assertIsInstance(color, accounts.StaffRoleColor)
                assert isinstance(color, accounts.StaffRoleColor)
                self.assertEqual(color.key, accounts.ROLE_COLOR_KEYS[group])
                self.assertIn(f'data-color-text="{color.key}" style="color:{color.css}"', names.html(row.id, row.name))
            self.assertEqual(names.html("nate"), "nate")
            # A unit with no staff entry gets the same span as a registered unit, from the one copy of the markup.
            self.assertEqual(names.unit_open() + rows[1].name + "</span>", names.html(rows[1].id, rows[1].name))
            self.assertEqual(names.html("<unknown>"), "&lt;unknown&gt;")
            _ = path.write_text(path.read_text().replace("#123456", "#abcdef"))
            self.assertIn('color:#abcdef', staff_names.StaffNames(staff.StaffSnapshot(rows)).html(rows[0].id))
            duplicate = replace(rows[1], name=rows[0].name)
            self.assertEqual(staff_names.StaffNames(staff.StaffSnapshot((rows[0], duplicate))).html(rows[0].name), rows[0].name)
