"""Check the names read from private build step settings."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.lib.build_steps import InvalidNameTemplate, NameTemplate, NoOwnerTemplate, command_script, read_step_names
from scripts.message.owner_role import OwnerRole


class BuildStepNamesTests(unittest.TestCase):
    def test_owner_templates_validate_when_the_config_is_read(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            _ = (Path(folder) / "build_steps.conf").write_text(
                "owned.default={unit} {step}\n"
                + "owned.check={unit:d}\n"
                + "names.check=type check\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"CONF_DIR": folder}):
                names = read_step_names()
        self.assertIsInstance(names.owner_template("test", "fixture", OwnerRole.UNIT), NameTemplate)
        self.assertIsInstance(names.owner_template("check", "fixture", OwnerRole.UNIT), InvalidNameTemplate)
        self.assertIs(names.owner_template("check", "", OwnerRole.UNIT), NoOwnerTemplate.FOUND)
        self.assertEqual(names.for_owner("test", "fixture", OwnerRole.UNIT), "fixture test")
        self.assertEqual(names.for_owner("check", "fixture", OwnerRole.UNIT), "type check")
        self.assertEqual(names.for_owner("check", "", OwnerRole.UNIT), "type check")
        self.assertEqual(names.for_owner("check", "fixture", OwnerRole.UNIT, current_name="old name"),
                         names.shown("check"))

    def test_command_script_keeps_the_path_with_a_space(self) -> None:
        path = "/fixture/local repo/check_rows.py"
        self.assertEqual(command_script(("python3", "-u", path)), path)
        for path in ("/fixture/check rows.py", "/fixture/O'Brien/check_rows.py"):
            with self.subTest(path=path):
                self.assertEqual(command_script(("python3", path)), path)
