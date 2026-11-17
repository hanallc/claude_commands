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
from scripts.lib.build_steps import StepNames
from scripts.message.owner_role import OwnerRole


def without_role_colors(markup: str) -> str:
    """Keep layout assertions independent of the new name color spans."""
    markup = re.sub(r'<span class="staff-name" data-color-text="role\.[^"]+" style="color:#[a-fA-F0-9]{6}">([^<]*)</span>',
                    r'\1', markup)
    return re.sub(r'(<option\b[^>]*?) style="color:#[a-fA-F0-9]{6}"', r'\1', markup)


class StaffNameTests(unittest.TestCase):
    def test_step_parts_preserve_configured_templates_and_mark_only_staff_fields(self) -> None:
        for template in ("{unit} {step}", "builder: {step} for {unit}", "{unit}: {unit}; {step}"):
            names = StepNames({"owned.default": template, "names.build": "builder"})
            parts = staff_names.owned_step_parts(names, "build", "builder", OwnerRole.UNIT)
            self.assertEqual(staff_names.parts_text(parts), names.for_owner("build", "builder", OwnerRole.UNIT))
            self.assertEqual(sum(part["kind"] == "staff" for part in parts), template.count("{unit}"))

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
            self.assertEqual(names.html("<unknown>"), "&lt;unknown&gt;")
            _ = path.write_text(path.read_text().replace("#123456", "#abcdef"))
            self.assertIn('color:#abcdef', staff_names.StaffNames(staff.StaffSnapshot(rows)).html(rows[0].id))
            duplicate = replace(rows[1], name=rows[0].name)
            self.assertEqual(staff_names.StaffNames(staff.StaffSnapshot((rows[0], duplicate))).html(rows[0].name), rows[0].name)
