"""Test live configuration, value limits, and recovery from Git history."""
from __future__ import annotations

from collections.abc import Callable
from contextlib import redirect_stderr
from decimal import Decimal
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import cast, override
import unittest
from unittest import mock


from scripts.lib import conf, conf_reads
from scripts.lib import checkout_test_support
from scripts.lint.memory_test_support import commit_config_fixture, write_config_fixture
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


class ConfTests(unittest.TestCase):
    root: Path = Path()
    directory: Path = Path()
    path: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.directory = self.root / "config"
        self.directory.mkdir()
        self.path = self.directory / "example.conf"
        self.enterContext(mock.patch.dict(os.environ, {
            "CONF_DIR": str(self.directory), "HOME": str(self.root / "home"),
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
        }))

    def write(self, value: str) -> None:
        _ = self.path.write_text(value, encoding="utf-8")

    def test_comments_start_only_at_a_line_start_or_after_whitespace(self) -> None:
        self.write("# comment=value\n  # indented=value\n"
                   + "color=#a0522d # comment\nname=brown\t# comment\n"
                   + "fragment=path#part\nmultiple=one#two#three\n")
        self.assertEqual(conf.read("example").values(), {
            "color": "#a0522d", "name": "brown", "fragment": "path#part", "multiple": "one#two#three",
        })

    def git(self, *arguments: str) -> str:
        return subprocess.run(
            ["git", *arguments], cwd=self.root, check=True, capture_output=True,
            text=True, timeout=TEST_HANG_GUARD_SECS,
        ).stdout.strip()

    def commit(self, contents: str) -> None:
        self.write(contents)
        _ = self.git("add", "config/example.conf")
        _ = self.git("commit", "-q", "-m", "configuration revision")

    def history(self) -> None:
        _ = self.git("init", "-q")
        _ = self.git("config", "user.name", "Configuration test")
        _ = self.git("config", "user.email", "conf@example.invalid")

    def test_config_reads_record_all_resolvers_in_the_log_their_environment_names(self) -> None:
        self.history()
        self.commit("value=one\n")
        self.write("other=two\n")
        log = self.root / "reads"
        program = """from pathlib import Path
import os
from scripts.lib import conf, conf_reads
conf.path('example')
conf_reads.read_text(Path(os.environ['CONF_DIR']) / 'example.conf')
conf.path(Path(os.environ['CONF_DIR']) / 'example.conf')
assert conf.read('example').origin('value', str).value == 'one'
os.environ['CONF_READ_LOG'] = os.environ['CONF_READ_LOG'] + '.other'
assert conf.read('example').origin('value', str).value == 'one'
(Path(os.environ['CONF_DIR']) / 'example.conf').write_text('value=one\\n')
conf.read_or_repository('example').text('value')
conf.effective_line('example', 'value')
conf.editable_line('example', 'value')
conf.check('example', 'value', 'two')
"""
        result = subprocess.run([sys.executable, "-c", program], capture_output=True, text=True,
                                env={**os.environ, "CONF_READ_LOG": str(log)}, check=True)
        self.assertEqual(result.stderr, "")
        self.assertTrue(log.is_file(), "The reader must record its config reads")
        self.assertEqual(set(log.read_text().splitlines()), {"example"})
        self.assertGreaterEqual(len(log.read_text().splitlines()), 4)
        # verify_helper.py runs many commands in one process, so each read uses the log its environment names.
        other = log.with_name(log.name + ".other")
        self.assertEqual(set(other.read_text().splitlines()), {"example"})
        self.assertGreaterEqual(len(other.read_text().splitlines()), 5)

    def test_config_read_log_context_restores_the_cached_destination(self) -> None:
        self.write("value=one\n")
        first = self.root / "first-reads"
        second = self.root / "second-reads"
        program = """import os
import sys
from scripts.lib import conf_reads
first, second, source = sys.argv[1:]
with conf_reads.record_reads(first):
    conf_reads.record('first')
    try:
        with conf_reads.record_reads(second):
            conf_reads.read_text(source)
            assert os.environ['CONF_READ_LOG'] == second
            raise ValueError('fixture interruption')
    except ValueError:
        pass
    assert os.environ['CONF_READ_LOG'] == first
    conf_reads.record('restored')
assert 'CONF_READ_LOG' not in os.environ
conf_reads.record('disabled')
"""
        environment = dict(os.environ)
        _ = environment.pop("CONF_READ_LOG", None)
        result = subprocess.run([sys.executable, "-c", program, str(first), str(second), str(self.path)],
                                capture_output=True, text=True, env=environment, check=True)
        self.assertEqual(result.stderr, "")
        self.assertEqual(first.read_text().splitlines(), ["first", "restored"])
        self.assertEqual(second.read_text().splitlines(), ["example"])

    def test_config_read_record_ignores_patched_os_functions(self) -> None:
        log = self.root / "reads"
        with conf_reads.record_reads(log), \
                mock.patch.object(os, "open", side_effect=OSError("fixture open refusal")), \
                mock.patch.object(os, "write", side_effect=OSError("fixture write refusal")), \
                mock.patch.object(os, "close", side_effect=OSError("fixture close refusal")):
            conf_reads.record("example")
        self.assertEqual(log.read_text().splitlines(), ["example"])

    def test_one_read_serves_many_settings_and_a_new_read_observes_changes(self) -> None:
        self.write("# range: 0..\nfraction=2.5\n# whole: 1..\ncount=3\n"
                   + "label=first\nnames=alpha beta\n# one of: small, large\nsize=small\n")
        reads: list[Path] = []
        read_text = Path.read_text

        def counted(source: Path, encoding: str | None = None, errors: str | None = None) -> str:
            reads.append(source)
            return read_text(source, encoding=encoding, errors=errors)

        with mock.patch.object(Path, "read_text", autospec=True, side_effect=counted):
            settings = conf.read("example")
            self.assertEqual(reads, [self.path])
            self.assertEqual(settings.number("fraction"), 2.5)
            self.assertEqual(settings.whole("count"), 3)
            self.assertEqual(settings.text("label"), "first")
            self.assertEqual(settings.words("names"), ("alpha", "beta"))
            self.assertEqual(settings.text("size"), "small")
            self.assertEqual(reads, [self.path])
            self.write("# whole: 1..\ncount=11\n")
            self.assertEqual(settings.whole("count"), 3)
            changed = conf.read("example")
            self.assertEqual(changed.whole("count"), 11)
            self.assertEqual(reads, [self.path, self.path])

    def test_bad_live_setting_recovers_from_history_at_use(self) -> None:
        self.history()
        self.commit("# whole: 1..10\ncount=3\n")
        self.write("# whole: 1..10\ncount=broken\n")
        warning = io.StringIO()
        with redirect_stderr(warning), mock.patch.object(
            conf, "_git", side_effect=AssertionError("History must be read at use"),
        ):
            settings = conf.read("example")
        self.assertEqual(warning.getvalue(), "")
        self.commit("# whole: 1..10\ncount=8\n")
        with redirect_stderr(warning):
            self.assertEqual(settings.whole("count"), 8)
        self.assertEqual(len(warning.getvalue().splitlines()), 1)
        self.assertIn("count='broken'", warning.getvalue())
        self.assertIn(str(self.path), warning.getvalue())
        origin = settings.origin("count", int)
        self.assertIsInstance(origin, conf.RecoveredValue)
        if not isinstance(origin, conf.RecoveredValue):
            self.fail("The invalid live setting must identify history recovery")
        self.assertEqual(origin.value, 8)

    def test_missing_file_defers_recovery_until_a_setting_is_used(self) -> None:
        self.history()
        self.commit("# whole: 1..10\ncount=3\n")
        self.path.unlink()
        warning = io.StringIO()
        with redirect_stderr(warning), mock.patch.object(
            conf, "_git", side_effect=AssertionError("History must be read at use"),
        ):
            settings = conf.read(self.path)
        self.assertEqual(warning.getvalue(), "")
        self.commit("# whole: 1..10\ncount=8\n")
        self.commit("other=2\n")
        self.write("# whole: 1..10\ncount=9\n")
        with redirect_stderr(warning):
            self.assertEqual(settings.whole("count"), 8)
        self.assertEqual(len(warning.getvalue().splitlines()), 1)
        self.assertIn(str(self.path), warning.getvalue())
        self.assertIn("count", warning.getvalue())
        with redirect_stderr(io.StringIO()):
            self.assertEqual(settings.value("count", int, default=4, recover_invalid=False), 8)
            self.path.unlink()
            self.directory.rmdir()
            self.assertEqual(conf.read(self.path).text("count", recover_invalid=False), "8")

    def test_values_and_origin_share_the_live_read_with_host_overrides(self) -> None:
        self.write("[settings]\nlabel=first\nlabel=duplicate\nlabel.testhost=local\noptional=\n"
                   + "# range: 0..\namount=2.5\n")
        with mock.patch("socket.gethostname", return_value="testhost.example"):
            settings = conf.read("example")
            self.write("label=changed\n# range: 0..\namount=9\n")
            self.assertEqual(settings.values(), {
                "label": "local", "label.testhost": "local", "optional": "", "amount": "2.5",
            })
            origin = settings.origin("amount", Decimal)
            self.assertIsInstance(origin, conf.UsableLiveValue)
            if not isinstance(origin, conf.UsableLiveValue):
                self.fail("The valid live setting must retain its origin")
            self.assertEqual(origin.value, Decimal("2.5"))
            self.assertEqual(conf.read("example").values(), {"label": "changed", "amount": "9"})

    def test_values_combines_requested_keys_with_host_only_prefix(self) -> None:
        self.write("label=base\nlabel.testhost=local\nmodel.active.testhost=blue\n"
                   + "model.other.anotherhost=green\naccount.active.testhost=red\n")
        with mock.patch("socket.gethostname", return_value="testhost.example"):
            settings = conf.read("example")
            self.assertEqual(settings.values("label", "missing"), {"label": "local"})
            self.assertEqual(settings.values(host_only_prefix="model."), settings.values() | {"model.active": "blue"})
            self.assertEqual(settings.values("label", "missing", host_only_prefix="model."),
                             {"label": "local", "model.active": "blue"})

    def test_each_reader_returns_its_type_and_preserves_text_after_the_first_equals(self) -> None:
        self.write("# range: ..\nfraction=2.5\n# whole: ..\ncount=-2\nlabel= a=b c # note\nnames= alpha  beta\tempty\n")
        self.assertEqual(conf.read("example").number("fraction"), 2.5)
        self.assertIs(type(conf.read("example").number("fraction")), float)
        self.assertEqual(conf.read("example").whole("count"), -2)
        self.assertIs(type(conf.read("example").whole("count")), int)
        self.assertEqual(conf.read("example").text("label"), "a=b c")
        self.assertEqual(conf.read("example").words("names"), ("alpha", "beta", "empty"))

    def test_empty_and_whitespace_values_are_faults_for_every_reader(self) -> None:
        readers: tuple[Callable[[conf.ConfigRead, str], object], ...] = (
            conf.ConfigRead.number, conf.ConfigRead.whole, conf.ConfigRead.text, conf.ConfigRead.words,
        )
        for reader in readers:
            for value in ("", "   "):
                with self.subTest(reader=reader.__name__, value=value):
                    self.write(f"# {'whole' if reader is conf.ConfigRead.whole else 'range'}: ..\namount={value}\n")
                    warning = io.StringIO()
                    with redirect_stderr(warning), self.assertRaises(conf.Unusable):
                        _ = reader(conf.read("example"), "amount")
                    self.assertEqual(len(warning.getvalue().splitlines()), 1)
                    self.assertIn("amount", warning.getvalue())
        self.history()
        recovery: tuple[tuple[Callable[[conf.ConfigRead, str], object], object], ...] = (
            (conf.ConfigRead.number, 3.0), (conf.ConfigRead.whole, 3),
            (conf.ConfigRead.text, "3"), (conf.ConfigRead.words, ("3",)),
        )
        for reader, expected in recovery:
            form = "whole" if reader is conf.ConfigRead.whole else "range"
            self.commit(f"# reader {reader.__name__}\n# {form}: ..\namount=3\n")
            self.write(f"# {form}: ..\namount=   \n")
            warning = io.StringIO()
            with self.subTest(reader=reader.__name__), redirect_stderr(warning):
                self.assertEqual(reader(conf.read("example"), "amount"), expected)
            self.assertEqual(len(warning.getvalue().splitlines()), 1)

    def test_reader_warnings_use_the_diagnostics_context(self) -> None:
        for recovery in (False, True):
            with self.subTest(recovery=recovery):
                if recovery:
                    self.history()
                    self.commit("# whole: 1..4\ncount=3\n")
                self.write("# whole: 1..4\ncount=broken\n")
                with redirect_stderr(io.StringIO()) as stderr, conf.diagnostics(report=False) as warnings:
                    if recovery:
                        self.assertEqual(conf.read("example").whole("count"), 3)
                    else:
                        with self.assertRaises(conf.Unusable):
                            _ = conf.read("example").whole("count")
                self.assertEqual(stderr.getvalue(), "")
                self.assertEqual(len(warnings), 1)
                self.assertIn("count='broken'", warnings[0])

    def test_map_warnings_use_the_diagnostics_context(self) -> None:
        for has_history in (False, True):
            if has_history:
                self.history()
                self.commit("host=mac\n")
            for damage in ("encoding", "missing file"):
                with self.subTest(has_history=has_history, damage=damage):
                    missing = damage == "missing file"
                    if missing:
                        self.path.unlink()
                    else:
                        _ = self.path.write_bytes(b"\xff")
                    with redirect_stderr(io.StringIO()) as stderr, conf.diagnostics(report=False) as warnings:
                        if has_history:
                            self.assertEqual(conf.read("example").values(), {"host": "mac"})
                        else:
                            with self.assertRaises(conf.Unusable) as caught:
                                _ = conf.read("example").values()
                            if missing:
                                message = str(caught.exception)
                                self.assertEqual(message, f"{self.path}: the file does not exist; add it with its keys")
                                self.assertIn(str(self.path), message)
                    self.assertEqual(stderr.getvalue(), "")
                    self.assertEqual(len(warnings), 1)
                    self.assertIn("keys:", warnings[0])

    def test_sections_are_skipped_and_the_first_value_wins(self) -> None:
        self.write("# note\n[values]\n# whole: ..\n LIMIT = 12 # first\n[another]\nLIMIT=99\nignored\n")
        self.assertEqual(conf.read("example").whole("LIMIT"), 12)

    def test_short_host_override_wins_and_its_first_value_is_used(self) -> None:
        self.write("# whole: ..\ncount=1\n# whole: ..\ncount.testhost=7\ncount.testhost=8\ncount.other=9\n")
        with mock.patch("socket.gethostname", return_value="testhost.example.invalid"):
            self.assertEqual(conf.read("example").whole("count"), 7)
        with mock.patch("socket.gethostname", return_value="unknown.example.invalid"):
            self.assertEqual(conf.read("example").whole("count"), 1)

    def test_an_ignored_host_override_reads_the_key_as_written(self) -> None:
        self.write("# whole: ..\ncount=1\n# whole: ..\ncount.testhost=7\n")
        with mock.patch("socket.gethostname", return_value="testhost.example.invalid"):
            settings = conf.read("example")
            ignored = conf.HostOverride.IGNORED
            self.assertEqual(settings.value("count", conf.positive_whole, host_override=ignored), 1)
            self.assertEqual(settings.value("count.testhost", conf.positive_whole, host_override=ignored), 7)
            self.assertEqual(settings.value("count.other", conf.positive_whole, default=3, host_override=ignored), 3)

    def test_home_config_is_used_when_conf_dir_is_not_set(self) -> None:
        home_config = self.root / "home/.claude/config"
        home_config.mkdir(parents=True)
        _ = (home_config / "example.conf").write_text("# whole: ..\ncount=41\n")
        self.write("# whole: ..\ncount=99\n")
        with mock.patch.dict(os.environ):
            del os.environ["CONF_DIR"]
            self.assertEqual(conf.read("example").whole("count"), 41)

    def test_every_call_observes_a_file_change(self) -> None:
        self.write("# whole: ..\ncount=2\n")
        self.assertEqual(conf.read("example").whole("count"), 2)
        self.write("# whole: ..\ncount=11\n")
        self.assertEqual(conf.read("example").whole("count"), 11)

    def test_temp_home_without_config_fails_and_names_the_live_path(self) -> None:
        home = self.root / "home"
        home.mkdir()
        live = home / ".claude/config/accounts.conf"
        readers: tuple[Callable[[conf.ConfigRead], object], ...] = (
            lambda settings: settings.whole("usage_stale_minutes"),
            lambda settings: settings.text("usage_stale_minutes"),
            lambda settings: settings.words("usage_stale_minutes"),
        )
        with mock.patch.dict(os.environ), redirect_stderr(io.StringIO()) as warning:
            del os.environ["CONF_DIR"]
            self.assertEqual(conf.path("accounts"), live)
            for reader in readers:
                with self.subTest(reader=reader), self.assertRaises(conf.Unusable) as caught:
                    _ = reader(conf.read("accounts"))
                self.assertIn(str(live), str(caught.exception))
            origin = conf.read("accounts").origin("usage_stale_minutes", int)
            with redirect_stderr(io.StringIO()) as map_warning, self.assertRaises(conf.Unusable) as caught:
                _ = conf.read("accounts").values()
            self.assertIn(str(live), str(caught.exception))
            self.assertEqual(len(map_warning.getvalue().splitlines()), 1)
        self.assertIsInstance(origin, conf.UnavailableValue)
        self.assertIn(f"{live}: usage_stale_minutes is missing", origin.reason if isinstance(origin, conf.UnavailableValue) else "")
        self.assertEqual(warning.getvalue(), "")
        self.assertEqual(list(home.iterdir()), [])

    def test_missing_override_file_refuses_until_a_live_file_exists(self) -> None:
        with redirect_stderr(io.StringIO()), self.assertRaises(conf.Unusable):
            _ = conf.read("accounts").whole("usage_stale_minutes")
        with redirect_stderr(io.StringIO()), self.assertRaises(conf.Unusable):
            _ = conf.read("accounts").values()
        self.assertIsInstance(
            conf.read("accounts").origin("usage_stale_minutes", int), conf.UnavailableValue,
        )
        source = self.directory / "accounts.conf"
        _ = source.write_text("# whole: 1..\nusage_stale_minutes=23\n")
        self.assertEqual(conf.read("accounts").whole("usage_stale_minutes"), 23)
        self.assertEqual(conf.read("accounts").values(), {"usage_stale_minutes": "23"})

    def test_missing_live_key_without_a_default_fails_and_names_the_key_and_path(self) -> None:
        del os.environ["CONF_DIR"]
        source = self.root / "home/.claude/config/accounts.conf"
        source.parent.mkdir(parents=True)
        contents = "# whole: 1..\nusage_stale_minutes=23\n"
        _ = source.write_text(contents)
        self.assertEqual(conf.read("accounts").whole("usage_stale_minutes"), 23)
        warning = io.StringIO()
        with redirect_stderr(warning), self.assertRaises(conf.Unusable) as caught:
            _ = conf.read("accounts").text("notes_directory")
        self.assertEqual(str(caught.exception), f"{source}: notes_directory is missing; add it to {source}")
        origin = conf.read("accounts").origin("notes_directory", str)
        self.assertIsInstance(origin, conf.UnavailableValue)
        self.assertEqual(origin.unrecovered() if isinstance(origin, conf.UnavailableValue) else "", str(caught.exception))
        self.assertEqual(warning.getvalue(), "")
        self.assertEqual(source.read_text(), contents)

    def test_missing_key_with_a_code_default_uses_the_default(self) -> None:
        self.write("# whole: 1..\ncount=2\n")
        with mock.patch.object(conf, "_git", side_effect=AssertionError("unexpected Git recovery")):
            self.assertEqual(conf.read("example").value("absent", str, default="fallback"), "fallback")
            self.assertEqual(conf.read("example").value("count", int, default=9), 2)

    def test_missing_file_with_a_code_default_fails_and_names_the_key_and_path(self) -> None:
        with self.assertRaises(conf.Unusable) as caught:
            _ = conf.read("example").value("absent", str, default="fallback")
        self.assertEqual(str(caught.exception),
                         f"{self.path}: absent is missing; add it to {self.path}; the file does not exist")
        origin = conf.read("example").origin("absent", str)
        self.assertIsInstance(origin, conf.UnavailableValue)
        self.assertEqual(origin.unrecovered() if isinstance(origin, conf.UnavailableValue) else "", str(caught.exception))

    def test_missing_override_key_fails(self) -> None:
        source = self.directory / "accounts.conf"
        contents = "# whole: 1..\nusage_stale_minutes=23\n"
        _ = source.write_text(contents)
        self.assertEqual(conf.read("accounts").whole("usage_stale_minutes"), 23)
        with redirect_stderr(io.StringIO()), self.assertRaises(conf.Unusable):
            _ = conf.read("accounts").text("notes_directory")
        self.assertIsInstance(conf.read("accounts").origin("notes_directory", str), conf.UnavailableValue)
        self.assertEqual(source.read_text(), contents)

    def test_missing_accounts_key_recovers_from_git_history(self) -> None:
        source = self.directory / "accounts.conf"
        _ = source.write_text("# whole: 1..\nusage_stale_minutes=23\n")
        commit_config_fixture(source)
        _ = source.write_text("other=2\n")
        with redirect_stderr(io.StringIO()) as warning:
            self.assertEqual(conf.read("accounts").whole("usage_stale_minutes"), 23)
        self.assertEqual(warning.getvalue(),
                         f"warning: {source}: usage_stale_minutes='<missing>': key is missing\n")
        with redirect_stderr(io.StringIO()) as warning:
            origin = conf.read("accounts").origin("usage_stale_minutes", int)
        self.assertIsInstance(origin, conf.RecoveredValue)
        if isinstance(origin, conf.RecoveredValue):
            self.assertEqual(origin.value, 23)
        self.assertEqual(warning.getvalue(), "")

    def test_config_history_ignores_inherited_git_repository_overrides(self) -> None:
        with self.subTest(variable="GIT_DIR"):
            self.write("other=2\n")
            repository = self.root / "repository"
            repository.mkdir()
            committed = repository / self.path.name
            _ = committed.write_text("# whole: 1..\ncount=17\n")
            commit_config_fixture(committed)
            with mock.patch.dict(os.environ, {"GIT_DIR": str(repository / ".git")}):
                settings = conf.read("example")
                self.assertIsInstance(settings.origin("count", int), conf.UnavailableValue)
                with self.assertRaises(conf.Unusable) as caught:
                    _ = settings.whole("count")
                self.assertEqual(str(caught.exception), f"{self.path}: count is missing; add it to {self.path}")

        with self.subTest(variable="GIT_CEILING_DIRECTORIES"):
            self.history()
            self.commit("# whole: 1..\ncount=23\n")
            self.path.unlink()
            with (mock.patch.dict(os.environ, {"GIT_CEILING_DIRECTORIES": str(self.root)}),
                  redirect_stderr(io.StringIO())):
                self.assertEqual(conf.read("example").whole("count"), 23)

    def test_worktree_reader_recovers_from_the_live_config_history(self) -> None:
        home = Path(os.environ["HOME"])
        live = home / ".claude/config/example.conf"
        live.parent.mkdir(parents=True)
        _ = live.write_text("# whole: 1..\ncount=17\n")
        commit_config_fixture(live)
        _ = live.write_text("other=2\n")
        checkout = self.root / "checkout"
        (checkout / "config").mkdir(parents=True)
        reader = checkout / "scripts/lib"
        reader.mkdir(parents=True)
        _ = shutil.copyfile(conf.__file__, reader / "conf.py")
        _ = shutil.copyfile(conf_reads.__file__, reader / "conf_reads.py")
        _ = (checkout / "config/example.conf").write_text("# whole: 1..\ncount=92\n")
        _ = self.git("-C", str(checkout), "init", "-q")
        _ = self.git("-C", str(checkout), "add", "config/example.conf")
        _ = self.git("-C", str(checkout), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                     "commit", "-qm", "checkout configuration")
        environment = {key: value for key, value in os.environ.items() if key not in {"CONF_DIR", "PYTHONPATH"}}
        for missing_file in (False, True):
            with self.subTest(missing_file=missing_file):
                if missing_file:
                    live.unlink()
                result = subprocess.run([sys.executable, "-m", "scripts.lib.conf", "get", "example", "count"],
                                        cwd=checkout, env=environment, capture_output=True, text=True,
                                        check=False, timeout=TEST_HANG_GUARD_SECS)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "17\n")
                self.assertEqual(len(result.stderr.splitlines()), 1)
                self.assertIn(str(live), result.stderr)

    def test_existing_invalid_key_and_explicit_missing_path_fail_without_history(self) -> None:
        del os.environ["CONF_DIR"]
        source = self.root / "home/.claude/config/accounts.conf"
        source.parent.mkdir(parents=True)
        with redirect_stderr(io.StringIO()), self.assertRaises(conf.Unusable):
            _ = conf.read(source).whole("usage_stale_minutes")
        for contents in ("# whole: 1..\nusage_stale_minutes=broken\n", "# whole: 1..\nusage_stale_minutes=0\n"):
            with self.subTest(contents=contents):
                _ = source.write_text(contents)
                with redirect_stderr(io.StringIO()), self.assertRaises(conf.Unusable):
                    _ = conf.read("accounts").whole("usage_stale_minutes")
                self.assertIsInstance(
                    conf.read("accounts").origin("usage_stale_minutes", int), conf.UnavailableValue,
                )

    def test_host_override_uses_its_own_adjacent_limits_for_reads_and_check(self) -> None:
        self.write("# whole: 1..4\ncount=3\n# whole: 8..10\ncount.testhost=9\n")
        with mock.patch("socket.gethostname", return_value="testhost.example"):
            self.assertEqual(conf.read("example").whole("count"), 9)
            conf.check("example", "count", "10")
            with self.assertRaises(conf.Unusable):
                conf.check("example", "count", "4")

    def test_inclusive_and_open_ended_ranges_accept_their_boundaries(self) -> None:
        for limits, value in (("1..4", "1"), ("1..4", "4"), ("..-2", "-2"),
                              ("2..", "2"), ("1.5..2.5", "2.5"), ("..", "-100"),
                              ("1..4", "1.0000000000000000"), ("1..4", "4.0000000000000000")):
            with self.subTest(limits=limits, value=value):
                self.write(f"# range: {limits}\namount={value}\n")
                self.assertEqual(conf.read("example").number("amount"), float(value))
                self.assertEqual((conf.read("example").origin("amount", Decimal),
                                  type(conf.read("example").number("amount"))),
                                 (conf.UsableLiveValue(Decimal(value)), float))
                conf.check("example", "amount", value)

    def test_configuration_import_does_not_load_decimal(self) -> None:
        result = subprocess.run(
            [str(Path(__file__).resolve().parents[2] / "scripts/lib/py"), '-S', '-c',
             "import sys; from scripts.lib import conf; print('decimal' in sys.modules)"],
            cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'False')

    def test_check_refuses_values_that_overflow_the_numeric_reader(self) -> None:
        self.write("# range: 0.001..\namount=1\n")
        with self.assertRaises(conf.Unusable):
            conf.check("example", "amount", "1e400")
        self.assertEqual(conf.read("example").number("amount"), 1.0)

    def test_one_of_limits_accept_members_and_reject_other_values(self) -> None:
        self.write("# one of: small, medium, large\nsize=medium\n")
        self.assertEqual(conf.read("example").text("size"), "medium")
        conf.check("example", "size", "large")
        with self.assertRaises(conf.Unusable) as caught:
            conf.check("example", "size", "huge")
        self.assertIn("size", str(caught.exception))
        self.assertIn(str(self.path), str(caught.exception))

    def test_bad_types_and_out_of_range_values_warn_once_and_refuse_without_history(self) -> None:
        readers: tuple[tuple[Callable[[conf.ConfigRead, str], object], str], ...] = (
            (conf.ConfigRead.number, "bad"), (conf.ConfigRead.number, "nan"), (conf.ConfigRead.number, "inf"),
            (conf.ConfigRead.number, ""), (conf.ConfigRead.whole, "1.5"), (conf.ConfigRead.whole, "true"),
            (conf.ConfigRead.number, "0"), (conf.ConfigRead.number, "5"),
            (conf.ConfigRead.number, "4.0000000000000001"),
            (conf.ConfigRead.number, "0.99999999999999999"),
        )
        for reader, value in readers:
            with self.subTest(reader=reader.__name__, value=value):
                self.write(f"# {'whole' if reader is conf.ConfigRead.whole else 'range'}: 1..4\namount={value}\n")
                warning = io.StringIO()
                with redirect_stderr(warning), self.assertRaises(conf.Unusable) as caught:
                    _ = reader(conf.read("example"), "amount")
                self.assertEqual(len(warning.getvalue().splitlines()), 1)
                self.assertIn(str(self.path), warning.getvalue())
                self.assertIn("amount", warning.getvalue())
                self.assertIn(value, warning.getvalue())
                self.assertIn(str(self.path), str(caught.exception))
                self.assertIn("amount", str(caught.exception))

    def test_missing_file_key_and_bad_encoding_do_not_supply_defaults(self) -> None:
        for damage in ("missing file", "missing key", "bad encoding"):
            with self.subTest(damage=damage):
                self.path.unlink(missing_ok=True)
                if damage == "missing key":
                    self.write("other=2\n")
                elif damage == "bad encoding":
                    _ = self.path.write_bytes(b"amount=\xff\n")
                with redirect_stderr(io.StringIO()), self.assertRaises(conf.Unusable) as caught:
                    _ = conf.read("example").whole("amount")
                self.assertIn(str(self.path), str(caught.exception))
                self.assertIn("amount", str(caught.exception))

    def test_fault_uses_the_newest_committed_value_that_passes_its_own_limits(self) -> None:
        self.history()
        self.commit("# whole: 1..10\namount=3\n")
        self.commit("# whole: 1..10\namount=8\n")
        self.commit("# whole: 1..4\namount=9\n")
        self.write("# whole: 1..10\namount=broken\n")
        warning = io.StringIO()
        with redirect_stderr(warning):
            self.assertEqual(conf.read("example").whole("amount"), 8)
        self.assertEqual(len(warning.getvalue().splitlines()), 1)
        self.assertIn("broken", warning.getvalue())
        self.assertIn("amount", warning.getvalue())
        self.assertIn(str(self.path), warning.getvalue())

    def test_recovery_skips_values_outside_the_current_limits(self) -> None:
        self.history()
        self.commit("# whole: 1..\ncount=2\n# range: 0..\namount=2.5\n# one of: off, on\nmode=on\n")
        self.commit("# whole: 1..\ncount=1\n# range: 0..\namount=9\n# one of: off, on\nmode=off\n")
        self.write("# whole: 1..\ncount=3\n# whole: 2..\ncount.testhost=broken\n"
                   + "# range: 0..4\namount=broken\n# one of: on\nmode=broken\n")
        with mock.patch("socket.gethostname", return_value="testhost.example"), redirect_stderr(io.StringIO()):
            settings = conf.read("example")
            self.assertEqual(settings.whole("count"), 2)
            self.assertEqual(settings.number("amount"), 2.5)
            self.assertEqual(settings.text("mode"), "on")

    def test_recovery_uses_committed_limits_when_no_value_passes_the_current_limit(self) -> None:
        self.history()
        self.commit("# whole: -1000..1000\namount=500\n# one of: on, off\nmode=on\n")
        self.commit("# whole: -1000..1000\namount=1001\n# one of: on, off\nmode=broken\n")
        for limit, reason in (("# whole: -1000.1000", "invalid limit"),
                              ("# whole: -1000..400.5", "expected a whole number"),
                              ("# whole: 1000..-1000", "outside range 1000..-1000"),
                              ("# whole: 0..100", "outside range 0..100")):
            with self.subTest(limit=limit), redirect_stderr(io.StringIO()) as warning:
                self.write(f"{limit}\namount=400\n")
                self.assertEqual(conf.read("example").whole("amount"), 500)
                self.assertEqual(len(warning.getvalue().splitlines()), 1)
                self.assertIn(reason, warning.getvalue())

        self.write("# one of:\nmode=off\n")
        with redirect_stderr(io.StringIO()) as warning:
            self.assertEqual(conf.read("example").text("mode"), "on")
        self.assertEqual(len(warning.getvalue().splitlines()), 1)
        self.assertIn("mode='off': expected one of", warning.getvalue())

    def test_recovery_reads_each_revision_once_when_the_current_limit_rejects_all_values(self) -> None:
        self.history()
        self.commit("# whole: 1..1000\namount=400\n")
        self.commit("# whole: 1..1000\namount=500\n")
        self.commit("# whole: 1..1000\namount=1001\n")
        self.write("# whole: 1..100\namount=600\n")
        git = cast(Callable[..., subprocess.CompletedProcess[str]], getattr(conf, "_git"))
        with mock.patch.object(conf, "_git", wraps=git) as history_reads, redirect_stderr(io.StringIO()):
            self.assertEqual(conf.read("example").whole("amount"), 500)
        # Git reads the checkout, the history, and each of the three revisions.
        self.assertEqual(history_reads.call_count, 5)

    def test_missing_current_key_recovers_from_history_and_host_override_is_validated(self) -> None:
        self.history()
        self.commit("# whole: ..\namount=3\n# whole: 1..9\namount.testhost=7\n")
        self.write("other=0\n")
        with mock.patch("socket.gethostname", return_value="testhost.example"), redirect_stderr(io.StringIO()):
            self.assertEqual(conf.read("example").whole("amount"), 7)

    def test_disabling_invalid_value_recovery_still_recovers_a_missing_key(self) -> None:
        self.history()
        self.commit("count=3\n")
        self.write("other=0\n")
        with redirect_stderr(io.StringIO()):
            self.assertEqual(conf.read("example").value("count", int, recover_invalid=False), 3)
        self.write("count=broken\n")
        with self.assertRaisesRegex(conf.Unusable, "count='broken'"):
            _ = conf.read("example").value("count", int, recover_invalid=False)

    def test_no_good_revision_raises_instead_of_using_a_code_default(self) -> None:
        self.history()
        self.commit("amount=broken\n")
        self.commit("# range: 1..4\namount=9\n")
        self.write("# range: ..\namount=nan\n")
        with redirect_stderr(io.StringIO()), self.assertRaises(conf.Unusable):
            _ = conf.read("example").number("amount")

    def test_check_refuses_missing_key_and_invalid_candidate_without_falling_back_or_writing(self) -> None:
        self.history()
        self.commit("# range: 1..4\namount=2\n")
        before = self.path.read_bytes(), self.path.stat().st_mtime_ns
        for key, value in (("absent", "3"), ("amount", "0"), ("amount", "5"), ("amount", "bad")):
            with self.subTest(key=key, value=value), self.assertRaises(conf.Unusable):
                conf.check("example", key, value)
        self.assertEqual((self.path.read_bytes(), self.path.stat().st_mtime_ns), before)

    def test_cli_get_uses_the_same_validation_and_history(self) -> None:
        self.history()
        self.commit("# range: 1..4\namount=3\n")
        self.write("# range: 1..4\namount=9\n")
        result = subprocess.run(
            [sys.executable, str(Path(conf.__file__)), "get", "example", "amount"],
            capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "3\n")
        self.assertEqual(len(result.stderr.splitlines()), 1)

    def test_map_recovers_the_newest_decodable_file_after_file_damage(self) -> None:
        self.history()
        self.commit("host=old\n")
        self.commit("host=mac\nlinux_only.example=\n# whole: 1..\ntries=3\n")
        _ = self.path.write_bytes(b"host=\xff\n")
        _ = self.git("add", "config/example.conf")
        _ = self.git("commit", "-qm", "damaged encoding")
        for damage in ("missing", "encoding", "directory"):
            with self.subTest(damage=damage):
                self.path.unlink(missing_ok=True)
                if damage == "encoding":
                    _ = self.path.write_bytes(b"host=\xff\n")
                elif damage == "directory":
                    self.path.mkdir()
                warning = io.StringIO()
                with redirect_stderr(warning):
                    self.assertEqual(conf.read("example").values(), {
                        "host": "mac", "linux_only.example": "", "tries": "3",
                    })
                self.assertEqual(len(warning.getvalue().splitlines()), 1)
                self.assertIn(str(self.path), warning.getvalue())
                if damage == "directory":
                    self.path.rmdir()

    def test_map_refuses_a_missing_file_without_history(self) -> None:
        for has_commits in (False, True):
            with self.subTest(has_commits=has_commits):
                if has_commits:
                    self.history()
                    _ = (self.directory / "unrelated.conf").write_text("host=mac\n")
                    _ = self.git("add", "config/unrelated.conf")
                    _ = self.git("commit", "-qm", "unrelated configuration")
                warning = io.StringIO()
                with redirect_stderr(warning), self.assertRaises(conf.Unusable) as caught:
                    _ = conf.read("example").values()
                self.assertEqual(len(warning.getvalue().splitlines()), 1)
                self.assertEqual(str(caught.exception), f"{self.path}: the file does not exist; add it with its keys")
                self.assertIn(str(self.path), str(caught.exception))

    def test_map_refuses_when_no_committed_file_decodes(self) -> None:
        self.history()
        _ = self.path.write_bytes(b"host=\xff\n")
        _ = self.git("add", "config/example.conf")
        _ = self.git("commit", "-qm", "damaged encoding")
        warning = io.StringIO()
        with redirect_stderr(warning), self.assertRaises(conf.Unusable) as caught:
            _ = conf.read("example").values()
        self.assertEqual(len(warning.getvalue().splitlines()), 1)
        self.assertIn(str(self.path), str(caught.exception))

    def test_map_key_faults_keep_history_recovery_and_optional_empty_entries(self) -> None:
        self.history()
        self.commit("# whole: 1..4\ntries=3\noptional=\n")
        self.write("# whole: 1..4\ntries=1.5\noptional=\n")
        warning = io.StringIO()
        with redirect_stderr(warning):
            self.assertEqual(conf.read("example").values(), {"tries": "3", "optional": ""})
        self.assertEqual(len(warning.getvalue().splitlines()), 1)
        self.assertIn("tries", warning.getvalue())

    def test_whole_limits_reject_fractional_publications_and_accept_exact_large_integers(self) -> None:
        self.write("# whole: 1..9007199254740993\ntries=3\n")
        for value in ("1.5", "0", "9007199254740994", "nan"):
            with self.subTest(value=value), self.assertRaises(conf.Unusable):
                conf.check("example", "tries", value)
        for value in ("1", "+3", "9007199254740993"):
            conf.check("example", "tries", value)
            self.write(f"# whole: 1..9007199254740993\ntries={value}\n")
            self.assertEqual(conf.read("example").whole("tries"), int(value))

    def test_whole_limits_accept_open_bounds_and_reject_values_beyond_them(self) -> None:
        for bounds, accepted, refused in (("..-2", "-2", "-1"), ("2..", "2", "1"),
                                          ("..", "-100", "1.5")):
            with self.subTest(bounds=bounds):
                self.write(f"# whole: {bounds}\namount={accepted}\n")
                conf.check("example", "amount", accepted)
                self.assertEqual(conf.read("example").whole("amount"), int(accepted))
                with self.assertRaises(conf.Unusable):
                    conf.check("example", "amount", refused)

    def test_numeric_readers_refuse_the_wrong_limit_form_and_missing_limits(self) -> None:
        for reader, form in ((conf.ConfigRead.whole, "range"), (conf.ConfigRead.number, "whole"),
                             (conf.ConfigRead.whole, "none"), (conf.ConfigRead.number, "none")):
            with self.subTest(reader=reader.__name__, form=form):
                self.write(f"# {form}: 1..4\namount=3\n")
                warning = io.StringIO()
                with redirect_stderr(warning), self.assertRaises(conf.Unusable) as caught:
                    _ = reader(conf.read("example"), "amount")
                self.assertIn("amount", str(caught.exception))
                self.assertIn("limit", str(caught.exception))
                self.assertEqual(len(warning.getvalue().splitlines()), 1)

    def test_numeric_history_uses_the_matching_limit_form(self) -> None:
        self.history()
        self.commit("# whole: 1..4\namount=2\n")
        self.commit("# range: 1..4\namount=3\n")
        with redirect_stderr(io.StringIO()):
            self.assertEqual(conf.read("example").whole("amount"), 2)
        self.commit("# whole: 1..4\namount=4\n")
        with redirect_stderr(io.StringIO()):
            self.assertEqual(conf.read("example").number("amount"), 3.0)

    def test_editable_line_requires_an_exact_key_and_preserves_its_raw_value(self) -> None:
        self.write("# whole: 1..\ncount=invalid\ncount=9\nother.remote=4\n")
        before = self.path.read_bytes()
        self.assertEqual(conf.editable_line("example", "count"), (1, "invalid"))
        self.assertEqual(self.path.read_bytes(), before)
        with mock.patch("socket.gethostname", return_value="remote"):
            self.assertEqual(conf.effective_line("example", "other"), (3, "4"))
            with self.assertRaisesRegex(conf.Unusable, "other: exact stored key is missing"):
                _ = conf.editable_line("example", "other")

    def test_editable_line_refuses_host_overrides_and_names_both_source_lines(self) -> None:
        self.write("# whole: 1..\ncount=3\n# whole: 1..\ncount.remote=4\n")
        before = self.path.read_bytes()
        with mock.patch("socket.gethostname", return_value="local"):
            self.assertEqual(conf.effective_line("example", "count"), (1, "3"))
            with self.assertRaisesRegex(
                conf.Unusable, "count at line 2 has host override count.remote at line 4",
            ):
                _ = conf.editable_line("example", "count")
        self.assertEqual(self.path.read_bytes(), before)

    def test_editable_line_refuses_overrides_for_keys_outside_model_colors(self) -> None:
        for stem, key in (("example", "model.provider"), ("example", "model.gpt-6.2-sol"),
                          ("statusline", "model.provider")):
            with self.subTest(stem=stem, key=key):
                source = self.directory / f"{stem}.conf"
                _ = source.write_text(f"{key}=3\n{key}.remote=4\n", encoding="utf-8")
                with mock.patch("socket.gethostname", return_value="remote"):
                    self.assertEqual(conf.effective_line(stem, key), (1, "4"))
                with mock.patch("socket.gethostname", return_value="local"):
                    with self.assertRaisesRegex(conf.Unusable, "has host override"):
                        _ = conf.editable_line(stem, key)

    def test_editable_line_keeps_each_dotted_model_key_independent(self) -> None:
        _ = write_config_fixture(self.directory / "machines.conf")
        source = self.directory / "statusline.conf"
        keys = ("model.gpt-6", "model.gpt-6.2-sol", "model.gpt-6.2-sol.preview")
        for order in (keys, keys[::-1]):
            with self.subTest(order=order):
                _ = source.write_text("".join(f"{key}=#123456\n" for key in order), encoding="utf-8")
                for line, key in enumerate(order):
                    self.assertEqual(conf.editable_line("statusline", key), (line, "#123456"))

    def test_known_host_names_recover_a_missing_machines_file_with_history(self) -> None:
        source = self.directory / "machines.conf"
        with redirect_stderr(io.StringIO()), self.assertRaises(conf.Unusable) as caught:
            _ = conf.known_host_names()
        self.assertEqual(str(caught.exception), f"{source}: the file does not exist; add it with its keys")
        _ = source.write_text("aliases=fixture\nfixture.hosts=Fixture-Host.local\n")
        expected = frozenset({"local", "fixture", "fixture-host"})
        with mock.patch("socket.gethostname", return_value="local.example"):
            self.assertEqual(conf.known_host_names(), expected)
            commit_config_fixture(source)
            source.unlink()
            with redirect_stderr(io.StringIO()) as warning:
                self.assertEqual(conf.known_host_names(), expected)
            self.assertIn(str(source), warning.getvalue())

    def test_installed_script_refuses_conf_dir_override(self) -> None:
        from scripts.build_hold import override_guard

        home = self.root / "installed-home"
        script = home / ".claude/scripts/lib/probe.py"
        script.parent.mkdir(parents=True)
        _ = script.write_text("# Private installed script.\n")
        with mock.patch.dict(os.environ, {"HOME": str(home)}), \
                mock.patch.object(override_guard, "user_home", return_value=home), self.assertRaises(SystemExit), \
                redirect_stderr(io.StringIO()) as warning:
            override_guard.refuse_overrides(script, "lib/probe.py")
        self.assertIn("CONF_DIR", warning.getvalue())

    def test_value_origin_distinguishes_live_permission_from_recovered_limits(self) -> None:
        self.history()
        self.commit("# range: 0..\namount=40\n")
        self.write("# range: 0..\namount=20\n")
        live = conf.read("example").origin("amount", Decimal)
        self.assertIsInstance(live, conf.UsableLiveValue)
        if not isinstance(live, conf.UsableLiveValue):
            self.fail("The valid live limit must identify live permission")
        self.assertEqual(live.value, Decimal("20"))
        for source in ("other=1\n", "# range: 0..\namount=broken\n",
                       "# range: 0..\namount=-1\n"):
            with self.subTest(source=source), redirect_stderr(io.StringIO()):
                self.write(source)
                recovered = conf.read("example").origin("amount", Decimal)
                self.assertIsInstance(recovered, conf.RecoveredValue)
                if not isinstance(recovered, conf.RecoveredValue):
                    self.fail("History must remain distinct from live spending permission")
                self.assertEqual(recovered.value, Decimal("40"))
                self.assertEqual(conf.read("example").number("amount"), 40)

    def test_value_origin_reports_unavailable_data_without_an_implicit_limit(self) -> None:
        self.write("# range: 0..\namount=broken\n")
        with redirect_stderr(io.StringIO()):
            result = conf.read("example").origin("amount", Decimal)
        self.assertIsInstance(result, conf.UnavailableValue)

    def test_value_origin_reads_changed_live_limits_after_history_recovery(self) -> None:
        self.history()
        self.commit("# range: 0..\namount=40\n")
        self.write("amount=broken\n")
        with redirect_stderr(io.StringIO()):
            recovered = conf.read(self.path).origin("amount", Decimal)
        self.assertIsInstance(recovered, conf.RecoveredValue)
        self.write("# range: 0..\namount=7.25\n")
        live = conf.read(self.path).origin("amount", Decimal)
        self.assertIsInstance(live, conf.UsableLiveValue)
        if not isinstance(live, conf.UsableLiveValue):
            self.fail("A repaired live limit must replace the recovered state")
        self.assertEqual(live.value, Decimal("7.25"))


class HanaConfigurationTests(unittest.TestCase):
    source: Path = Path(__file__).resolve().parents[2] / "config/hana_aws.conf"

    def test_render_configuration_has_no_host_suffixes(self) -> None:
        settings = conf.settings(self.source.read_text())
        self.assertTrue(settings)
        for key in settings:
            with self.subTest(key=key):
                self.assertNotIn('.', key)

    def test_execution_values_have_no_copies_in_hana_configuration(self) -> None:
        settings = conf.settings(self.source.read_text())
        for key in ('execution_compiler_version', 'execution_shell_timeout_s', 'execution_command_timeout_s'):
            self.assertNotIn(key, settings)
        self.assertEqual(sum(line.startswith('free_floor_gib=')
            for line in self.source.read_text().splitlines()), 1)


LIMIT_PREFIXES = ("# whole:", "# range:", "# one of:")


def twin_faults(config: Path, twin: Callable[[str], Path]) -> list[str]:
    """Return one line for each file or key where a test-owned twin differs in shape from config."""
    def keys(path: Path) -> dict[str, str]:
        return {key: setting.limit if setting.limit.startswith(LIMIT_PREFIXES) else ""
                for key, setting in conf.settings(path.read_text(encoding="utf-8")).items()}

    stems = sorted(path.stem for path in config.glob("*.conf"))
    faults: list[str] = []
    for stem in stems:
        source, owned = config / f"{stem}.conf", twin(stem)
        name = owned.name
        if not owned.is_file():
            faults.append(f"{name}: no twin: add the file to test_config/{name}")
        expected = keys(source)
        actual = keys(owned) if owned.is_file() else {}
        for key in sorted(expected.keys() | actual.keys()):
            if key not in actual:
                faults.append(f"{name}: {key}: add the key to test_config/{name}")
            elif key not in expected:
                faults.append(f"{name}: {key}: remove the key from test_config/{name}")
            elif expected[key] != actual[key]:
                faults.append(f"{name}: {key}: copy the limit comment to test_config/{name}")
    for path in sorted(twin("").parent.glob("*.conf")):
        if path.stem in stems:
            continue
        faults.append(f"{path.name}: no config/ file: remove test_config/{path.name}")
        faults.extend(f"{path.name}: {key}: remove the key from test_config/{path.name}"
                      for key in sorted(keys(path)))
    return faults


class TestOwnedConfigTests(unittest.TestCase):
    def test_each_config_file_has_a_test_owned_twin_with_the_same_keys(self) -> None:
        faults = twin_faults(Path(conf.__file__).resolve().parents[2] / "config", conf.test_config_path)
        self.assertEqual(faults, [], "\n".join(faults))

    def test_config_copy_keeps_values_when_the_file_has_no_twin(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        source = root / "config"
        source.mkdir()
        (root / "test_config").mkdir()
        contents = b"# whole: 1..\ncount=8\n"
        _ = (source / "limits.conf").write_bytes(contents)

        copied = conf.copy_for_test(source, root / "fixture")

        self.assertEqual((copied / "limits.conf").read_bytes(), contents)

    def test_runtime_copies_shift_the_test_owned_value_after_a_live_tune(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        source = root / "checkout"
        live = source / "config/detached_runs.conf"
        owned = source / "test_config/detached_runs.conf"
        for path in (live, owned):
            path.parent.mkdir(parents=True)
            _ = path.write_text("# range: 1..30\nRESULT_PUSH_RETRY_SECONDS=5\n")
        with (mock.patch.object(checkout_test_support, "SOURCE", source),
              mock.patch.object(checkout_test_support, "runtime_files",
                                return_value=(owned.relative_to(source),))):
            checkout_test_support.copy_runtime(root / "before")
            _ = live.write_text("# range: 1..30\nRESULT_PUSH_RETRY_SECONDS=8\n")
            checkout_test_support.copy_runtime(root / "after")
        before = root / "before/config/detached_runs.conf"
        after = root / "after/config/detached_runs.conf"
        self.assertEqual(after.read_bytes(), before.read_bytes())
        self.assertEqual(conf.read(after).number("RESULT_PUSH_RETRY_SECONDS"), 6.25)
        self.assertIn("# test config shift: 5 6.25\n", after.read_text())

    def test_twin_check_names_each_file_and_key(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        config, owned = root / "config", root / "test_config"
        config.mkdir()
        owned.mkdir()
        rows: tuple[tuple[str, str | None, list[str]], ...] = (
            ("same keys, other values", "a=1\n# whole: 1 to 9\nb=2\n", []),
            ("missing key", "a=1\n", ["case.conf: b: add the key to test_config/case.conf"]),
            ("host key", "a=1\n# whole: 1 to 9\nb=2\nb.natedev=3\n",
             ["case.conf: b.natedev: remove the key from test_config/case.conf"]),
            ("limit", "a=1\n# whole: 1 to 8\nb=2\n", ["case.conf: b: copy the limit comment to test_config/case.conf"]),
            ("no twin", None, ["case.conf: no twin: add the file to test_config/case.conf",
                               "case.conf: a: add the key to test_config/case.conf",
                               "case.conf: b: add the key to test_config/case.conf"]),
        )
        _ = (config / "case.conf").write_text("a=0\n# whole: 1 to 9\nb=5\n", encoding="utf-8")
        for label, text, expected in rows:
            with self.subTest(label):
                twin = owned / "case.conf"
                twin.unlink(missing_ok=True)
                if text is not None:
                    _ = twin.write_text(text, encoding="utf-8")
                self.assertEqual(twin_faults(config, lambda stem: owned / f"{stem}.conf"), expected)
        _ = (owned / "extra.conf").write_text("a=1\n", encoding="utf-8")
        faults = twin_faults(config, lambda stem: owned / f"{stem}.conf")
        self.assertIn("extra.conf: no config/ file: remove test_config/extra.conf", faults)
        self.assertIn("extra.conf: a: remove the key from test_config/extra.conf", faults)

    def test_fixture_helpers_take_values_from_the_test_owned_file(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        owned = root / "test_config"
        owned.mkdir()
        _ = (owned / "example.conf").write_text("value=test-owned\n", encoding="utf-8")
        fixture = root / "fixture"
        fixture.mkdir()

        def owned_path(stem: str) -> Path:
            return owned / f"{stem}.conf"

        _ = self.enterContext(mock.patch.object(conf, "test_config_path", side_effect=owned_path))
        self.enterContext(mock.patch.dict(os.environ, {"CONF_DIR": str(fixture)}))

        def written() -> str:
            _ = write_config_fixture(fixture / "example.conf")
            return conf.read("example").text("value")

        rows: tuple[tuple[str, Callable[[], str]], ...] = (
            ("read_or_repository", lambda: conf.read_or_repository("example").text("value")),
            ("write_config_fixture", written),
        )
        for label, read in rows:
            with self.subTest(label):
                (fixture / "example.conf").unlink(missing_ok=True)
                self.assertEqual(read(), "test-owned")


if __name__ == "__main__":
    _ = unittest.main()
