"""Configured displays change independently of their record keys."""
from __future__ import annotations

from collections.abc import Callable
from contextlib import redirect_stderr
from contextvars import ContextVar, copy_context
from io import StringIO
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from typing import cast, override

from scripts.lib import names


NAMES_TOML = '''[jobs]
showrunner = ["showrunner"]
unit_director = ["unit director"]
worker = ["worker"]
implementer = ["implementer"]
tester = ["tester"]
repair_worker = ["repair worker"]
code_reviewer = ["code reviewer"]
ui_reviewer = ["UI reviewer"]
designer = ["designer"]
design_reviewer = ["design reviewer"]
[audiences]
showrunners = ["showrunners"]
unit_directors = ["unit directors"]
agents = ["agents"]
workers = ["workers"]
top_level = ["top level"]
all_roles = ["all roles"]
'''


def fixture_names(*, worker: str = "worker", director: str = "unit director") -> names.Names:
    """Complete fixture configuration; render tests never need the live file."""
    return names.Names(
        {"showrunner": ("showrunner",), "unit_director": (director,), "worker": (worker,),
         "implementer": ("implementer",), "tester": ("tester",), "repair_worker": ("repair worker",),
         "code_reviewer": ("code reviewer",), "ui_reviewer": ("UI reviewer",),
         "designer": ("designer",), "design_reviewer": ("design reviewer",)},
        {"showrunners": ("showrunners",), "unit_directors": ("unit directors",),
         "agents": ("agents",), "workers": ("workers",), "top_level": ("top level",),
         "all_roles": ("all roles",)},
    )


def clear_runtime_names() -> None:
    """Drop the cached runtime load, so the next call loads again."""
    cached = cast(object, getattr(names, "_runtime_names"))
    cast(Callable[[], None], getattr(cached, "cache_clear"))()


def new_command_errors() -> ContextVar[frozenset[names.NamesError]]:
    """An empty set of reported errors, as a new command starts with."""
    return ContextVar("reported_names_errors", default=frozenset[names.NamesError]())


class NamesTests(unittest.TestCase):
    @override
    def setUp(self) -> None:
        clear_runtime_names()
        self.addCleanup(clear_runtime_names)

    def test_runtime_configuration_loads_once_while_the_file_stays_the_same(self) -> None:
        original = fixture_names(worker="builder")
        with patch.object(names, "load_names", side_effect=(original, fixture_names(worker="maker"))) as load:
            self.assertEqual(names.runtime_names(), original)
            self.assertEqual(names.runtime_names(), original)
        load.assert_called_once_with()

    def test_runtime_configuration_loads_again_after_the_file_changes(self) -> None:
        # verify_helper.py runs many commands in one process, and an edit between two of them must show.
        with tempfile.TemporaryDirectory() as directory, redirect_stderr(StringIO()):
            path = Path(directory) / "names.toml"
            _ = path.write_text(NAMES_TOML, encoding="utf-8")
            displays: list[str] = []
            load = names.load_names
            with patch.object(names, "CONFIG_PATH", path), patch.object(names, "load_names", lambda: load(path)):
                for history in ('["worker"]', '["builder", "worker"]'):
                    edit = path.with_suffix(".new")
                    _ = edit.write_text(NAMES_TOML.replace('["worker"]', history), encoding="utf-8")
                    os.replace(edit, path)
                    loaded = names.runtime_names()
                    assert isinstance(loaded, names.Names), loaded
                    displays.append(loaded.job("worker"))
        self.assertEqual(displays, ["worker", "builder"])

    def test_each_command_reports_an_error_once(self) -> None:
        # verify_helper.py runs each call in a copy of its context, as a new process starts a new context.
        error = names.MissingKey("jobs", "once-in-each-command")
        output = StringIO()

        def command() -> None:
            for _ in range(2):
                names.report_error(error)
        with redirect_stderr(output):
            for _ in range(2):
                copy_context().run(command)
        self.assertEqual(output.getvalue().count("once-in-each-command"), 2)

    def test_configuration_errors_name_the_file_table_and_entry_in_plain_text(self) -> None:
        output = StringIO()
        with patch.object(names, "_reported_errors", new_command_errors()), redirect_stderr(output):
            names.report_error(names.MissingKey("jobs", "worker"))
        self.assertEqual(output.getvalue(), f"names: {names.CONFIG_PATH}: [jobs] has no worker entry\n")
        for error, detail in (
            (names.UnknownKey("jobs", "builder"), "[jobs] has an unknown builder entry"),
            (names.MissingDisplayName("jobs", "worker"), "[jobs].worker needs a nonempty list of display names"),
            (names.DuplicateDisplayName("jobs", "builder"), "[jobs] repeats the display name 'builder'"),
            (names.UnreadableNames("/fixture/names.toml", "denied"), "cannot read the names: denied"),
            (names.InvalidNames("/fixture/names.toml", "bad table"), "invalid names: bad table"),
        ):
            with self.subTest(error=error):
                self.assertIn(detail, names.error_text(error))
                self.assertNotIn(type(error).__name__ + "(", names.error_text(error))

    def test_live_configuration_is_valid(self) -> None:
        self.assertIsInstance(names.load_names(names.CONFIG_PATH), names.Names)

    def test_successive_one_line_renames_retain_both_retired_displays(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "names.toml"
            for history, display, retired in (
                ('["worker"]', "worker", ()),
                ('["builder", "worker"]', "builder", ("worker",)),
                ('["maker", "builder", "worker"]', "maker", ("builder", "worker")),
            ):
                with self.subTest(display=display):
                    _ = path.write_text(NAMES_TOML.replace('["worker"]', history), encoding="utf-8")
                    loaded = names.load_names(path)
                    self.assertIsInstance(loaded, names.Names)
                    assert isinstance(loaded, names.Names)
                    self.assertEqual(loaded.job("worker"), display)
                    self.assertEqual(loaded.retired(), retired)
                    self.assertIn(display, loaded.current())
                    self.assertNotIn(display, loaded.retired())

    def test_invalid_keys_histories_and_files_have_distinct_errors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "names.toml"
            self.assertIsInstance(names.load_names(path), names.UnreadableNames)
            for source, expected in (
                (NAMES_TOML.replace('worker = ["worker"]', 'unknown = ["worker"]'), names.UnknownKey),
                (NAMES_TOML.replace('worker = ["worker"]\n', ''), names.MissingKey),
                (NAMES_TOML.replace('["worker"]', '[]'), names.MissingDisplayName),
                (NAMES_TOML.replace('["worker"]', '[""]'), names.MissingDisplayName),
                (NAMES_TOML.replace('["worker"]', '["showrunner"]'), names.DuplicateDisplayName),
                (NAMES_TOML.replace('["worker"]', '["builder", "showrunner"]'), names.DuplicateDisplayName),
                (NAMES_TOML.replace('["worker"]', '["builder", "BUILDER"]'), names.DuplicateDisplayName),
                ('[jobs', names.InvalidNames),
            ):
                with self.subTest(expected=expected.__name__, source=source):
                    _ = path.write_text(source, encoding="utf-8")
                    self.assertIsInstance(names.load_names(path), expected)

    def test_runtime_reports_one_error_once_without_inventing_names(self) -> None:
        error = names.UnreadableNames("/fixture/missing-names.toml", "fixture denied")
        output = StringIO()
        with patch.object(names, "load_names", return_value=error), \
                patch.object(names, "_reported_errors", new_command_errors()), redirect_stderr(output):
            self.assertEqual(names.runtime_names(), error)
            self.assertEqual(names.runtime_names(), error)
        self.assertEqual(output.getvalue().count("fixture denied"), 1)


if __name__ == "__main__":
    _ = unittest.main()
