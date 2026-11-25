"""The fixture config key check against small committed trees."""
from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import subprocess
import tempfile
import unittest
from typing import override
from unittest import mock

from scripts.production import fixture_conf_keys

# Red 1 of item 191: test_state_backup wrote its own build_targets.conf after the
# row added unreachable_wait_seconds, and the missing-key warning failed the test.
STATE_BACKUP_FIXTURE = '''\
from pathlib import Path


def fixture(config: Path) -> None:
    _ = (config / "build_targets.conf").write_text("""\\
targets = natedev,mac
kind.natedev = local
""", encoding="utf-8")
'''

FIXTURE_SUMMARY = "fixture config keys: {} missing, {} fixtures not checked because their text is not a literal\n"
KEY_READS_SUMMARY = "config key reads: {} of a removed key, {} of a key no config file holds\n"
SUMMARY = FIXTURE_SUMMARY + KEY_READS_SUMMARY.format(0, 0)


class FixtureConfKeyTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.repo: Path = Path()
        self.base: str = ""

    @override
    def setUp(self) -> None:
        self.repo = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="fixture-keys-test-")))
        _ = self.git("init", "-b", "main")
        _ = self.git("config", "user.name", "Key Fixture")
        _ = self.git("config", "user.email", "fixture@example.invalid")
        self.write("config/build_targets.conf", "targets=natedev,mac\nkind.natedev=local\n")
        self.write("scripts/backup/test_state_backup.py", STATE_BACKUP_FIXTURE)
        self.base = self.commit()

    def git(self, *arguments: str) -> str:
        result = subprocess.run(["git", "-C", str(self.repo), *arguments],
                                capture_output=True, text=True, check=True)
        return result.stdout.strip()

    def write(self, path: str, text: str) -> None:
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        _ = target.write_text(text, encoding="utf-8")

    def commit(self) -> str:
        _ = self.git("add", ".")
        _ = self.git("commit", "--allow-empty", "-m", "fixture")
        return self.git("rev-parse", "HEAD")

    def add_key(self, text: str = "# whole: 1..\nunreachable_wait_seconds=600\n") -> str:
        self.write("config/build_targets.conf", "targets=natedev,mac\nkind.natedev=local\n" + text)
        return self.commit()

    def run_check(self, tip: str) -> tuple[int, str]:
        output = StringIO()
        with redirect_stdout(output):
            status = fixture_conf_keys.main(["--repo", str(self.repo), "--base", self.base, "--tip", tip])
        return status, output.getvalue()

    def test_a_fixture_without_a_new_key_fails_with_its_line(self) -> None:
        status, output = self.run_check(self.add_key())
        self.assertEqual(status, 1, output)
        self.assertIn("new keys in config/build_targets.conf: unreachable_wait_seconds\n", output)
        self.assertIn("scripts/backup/test_state_backup.py:5: the config/build_targets.conf fixture "
                      + "lacks unreachable_wait_seconds\n", output)
        self.assertTrue(output.endswith(SUMMARY.format(1, 0)), output)

    def test_a_fixture_that_holds_the_new_key_passes(self) -> None:
        self.write("scripts/backup/test_state_backup.py",
                   STATE_BACKUP_FIXTURE.replace("kind.natedev = local\n",
                                                "kind.natedev = local\nunreachable_wait_seconds = 1\n"))
        status, output = self.run_check(self.add_key())
        self.assertEqual(status, 0, output)
        self.assertTrue(output.endswith(SUMMARY.format(0, 0)), output)

    def test_fixture_paths_resolve_through_names_in_their_scope(self) -> None:
        self.write("scripts/build_queue/placement_test_support.py", '''\
import os
from pathlib import Path
from scripts.lib.atomic_file import write_text_atomic

TARGETS = Path("config") / "build_targets.conf"
HEADER = "targets=natedev\\n"


class Fixture:
    def setUp(self) -> None:
        self.targets = Path(os.environ["CONF_DIR"]) / "build_targets.conf"

    def write(self, root: Path, wait: int) -> None:
        write_text_atomic(root / TARGETS, f"targets=natedev\\nunreachable_wait_seconds={wait}\\n")
        _ = Path(os.environ["CONF_DIR"]).joinpath(TARGETS).write_text(HEADER + "kind.mac=ssh\\n")
        _ = self.targets.write_text(HEADER)


def config_path(config: Path) -> None:
    path = config / "build_targets.conf"
    _ = path.write_text(HEADER + "unreachable_wait_seconds=1\\n")


def records(folder: Path) -> None:
    for path in folder.glob("*.json"):
        _ = path.write_text("{")
''')
        writes = fixture_conf_keys.fixture_writes("support.py", (
            self.repo / "scripts/build_queue/placement_test_support.py").read_text(), {"build_targets.conf"})
        self.assertEqual(sorted((write.line, write.text) for write in writes), [
            (14, "targets=natedev\nunreachable_wait_seconds=0\n"), (15, "targets=natedev\nkind.mac=ssh\n"),
            (16, "targets=natedev\n"), (21, "targets=natedev\nunreachable_wait_seconds=1\n")])
        status, output = self.run_check(self.add_key())
        self.assertEqual(status, 1, output)
        for line in (15, 16):
            self.assertIn(f"scripts/build_queue/placement_test_support.py:{line}: the config/build_targets.conf "
                          + "fixture lacks unreachable_wait_seconds\n", output)
        self.assertTrue(output.endswith(SUMMARY.format(3, 0)), output)

    def test_template_fixtures_texts_that_are_not_literals_and_per_name_keys(self) -> None:
        self.write("scripts/backup/test_state_backup.py", '''\
from pathlib import Path
from scripts.lint.memory_test_support import write_config_fixture


def fixture(config: Path, template: str) -> None:
    _ = write_config_fixture(config / "build_targets.conf", "targets=natedev\\n")
    _ = (config / "build_targets.conf").write_text(template.replace("a", "b"))
    _ = (config / "fixture-mac.conf").write_text("host = fixture-mac\\n")
''')
        status, output = self.run_check(self.add_key("kind.hana-gpu=ssh\nunreachable_wait_seconds=600\n"))
        self.assertEqual(status, 0, output)
        self.assertIn("new keys in config/build_targets.conf: unreachable_wait_seconds\n", output)
        self.assertTrue(output.endswith(SUMMARY.format(0, 1)), output)
        writes = list(fixture_conf_keys.fixture_writes("test_state_backup.py", (
            self.repo / "scripts/backup/test_state_backup.py").read_text(), {"build_targets.conf"}))
        self.assertEqual([(write.line, write.text) for write in writes], [(7, None)])

    def test_a_new_config_file_and_an_unchanged_file_need_no_key_from_the_other(self) -> None:
        self.write("config/machine_facts.conf", "probe_seconds=5\n")
        tip = self.commit()
        self.write("scripts/buildlog/test_machine_facts.py",
                   'from pathlib import Path\n_ = Path("machine_facts.conf").write_text("other=1\\n")\n')
        status, output = self.run_check(self.commit())
        self.assertEqual(status, 1, output)
        self.assertIn("scripts/buildlog/test_machine_facts.py:2: the config/machine_facts.conf fixture "
                      + "lacks probe_seconds\n", output)
        self.assertNotIn("build_targets.conf", output)
        self.assertEqual(self.run_check(tip)[0], 0)

    def test_an_unknown_revision_and_a_source_with_bad_syntax_are_faults(self) -> None:
        self.write("scripts/backup/test_broken.py", "def broken(:\n")
        broken = self.add_key()
        for tip, fault in (("absent", "fatal: "), (broken, "test_broken.py")):
            with self.subTest(tip=tip):
                errors = StringIO()
                with redirect_stdout(StringIO()), mock.patch("sys.stderr", errors):
                    status = fixture_conf_keys.main(["--repo", str(self.repo), "--base", self.base, "--tip", tip])
                self.assertEqual(status, 2)
                self.assertTrue(errors.getvalue().startswith("fixture_conf_keys: "), errors.getvalue())
                self.assertIn(fault, errors.getvalue())

    def test_python_reads_of_removed_renamed_and_deleted_keys(self) -> None:
        self.write("config/legacy.conf", "timeout=60\nbackoff_seconds=5\n")
        self.write("scripts/base_reader.py", 'settings.whole("timeout")\n')
        self.base = self.commit()
        self.write("config/build_targets.conf", "kind.mac=ssh\n")
        (self.repo / "config/legacy.conf").unlink()
        (self.repo / "scripts/base_reader.py").unlink()
        self.write("scripts/z_reader.py", 'settings.whole("timeout")\n')
        self.write("scripts/a_reader.py", 'settings.text("targets")\nsettings.text("kind.natedev")\n')
        self.write("scripts/test_reader.py", 'settings.text("targets")\n')
        self.write("scripts/reader_fixture.py", 'settings.whole("timeout")\n')
        self.write("scripts/nonliteral.py", '# "timeout"\nsettings.text("kind.natedev.extra")\n')
        self.write("tools/reader.py", 'settings.text("targets")\n')
        tip = self.commit()
        self.write("scripts/a_reader.py", 'settings.text("kind.mac")\n')
        status, output = self.run_check(tip)
        self.assertEqual(status, 0, output)
        self.assertEqual(output, (
            "removed keys in config/build_targets.conf: kind.natedev, targets\n"
            + "removed keys in config/legacy.conf: backoff_seconds, timeout\n"
            + "scripts/a_reader.py:1: reads targets, which the tip removes from config/build_targets.conf\n"
            + "scripts/a_reader.py:2: reads kind.natedev, which the tip removes from config/build_targets.conf\n"
            + "scripts/z_reader.py:1: reads timeout, which the tip removes from config/legacy.conf\n"
            + FIXTURE_SUMMARY.format(0, 0) + KEY_READS_SUMMARY.format(3, 0)))

    def test_shell_reads_match_only_whole_tokens(self) -> None:
        self.write("config/build_targets.conf", "kind.natedev=local\n")
        for suffix in ("sh", "zsh", "bash"):
            self.write(f"scripts/read_targets.{suffix}", 'read_setting "targets"\n'
                       + "read_setting retargets targets2 _targets -targets .targets "
                       + "targets_extra targets-extra targets.old\n")
        status, output = self.run_check(self.commit())
        self.assertEqual(status, 0, output)
        self.assertEqual(output, (
            "removed keys in config/build_targets.conf: targets\n"
            + "scripts/read_targets.bash:1: reads targets, which the tip removes from config/build_targets.conf\n"
            + "scripts/read_targets.sh:1: reads targets, which the tip removes from config/build_targets.conf\n"
            + "scripts/read_targets.zsh:1: reads targets, which the tip removes from config/build_targets.conf\n"
            + FIXTURE_SUMMARY.format(0, 0) + KEY_READS_SUMMARY.format(3, 0)))

    def test_shared_key_requires_the_removed_file_name_in_the_same_source(self) -> None:
        self.write("config/other.conf", "targets=mac\n")
        self.base = self.commit()
        self.write("config/build_targets.conf", "kind.natedev=local\n")
        self.write("scripts/from_stem.py", 'settings = conf.read("build_targets")\nsettings.text("targets")\n')
        self.write("scripts/from_file.py", 'settings = conf.read("build_targets.conf")\nsettings.text("targets")\n')
        self.write("scripts/from_stem.sh", 'open_settings build_targets\nread_setting targets\n')
        self.write("scripts/from_file.sh", 'open_settings build_targets.conf\nread_setting targets\n')
        self.write("scripts/from_other.py", 'settings = conf.read("other")\nsettings.text("targets")\n')
        self.write("scripts/from_backup.sh", 'open_settings build_targets.conf.bak\nread_setting targets\n')
        status, output = self.run_check(self.commit())
        self.assertEqual(status, 0, output)
        self.assertEqual(output, (
            "removed keys in config/build_targets.conf: targets\n"
            + "scripts/from_file.py:2: reads targets, which the tip removes from config/build_targets.conf\n"
            + "scripts/from_file.sh:2: reads targets, which the tip removes from config/build_targets.conf\n"
            + "scripts/from_stem.py:2: reads targets, which the tip removes from config/build_targets.conf\n"
            + "scripts/from_stem.sh:2: reads targets, which the tip removes from config/build_targets.conf\n"
            + FIXTURE_SUMMARY.format(0, 0) + KEY_READS_SUMMARY.format(4, 0)))

    def test_missing_fixture_key_still_fails_with_removed_key_reads(self) -> None:
        self.write("config/build_targets.conf", "kind.natedev=local\nunreachable_wait_seconds=600\n")
        self.write("scripts/read_targets.py", 'settings.text("targets")\n')
        status, output = self.run_check(self.commit())
        self.assertEqual(status, 1, output)
        self.assertEqual(output, (
            "new keys in config/build_targets.conf: unreachable_wait_seconds\n"
            + "scripts/backup/test_state_backup.py:5: the config/build_targets.conf fixture lacks "
            + "unreachable_wait_seconds\n"
            + "removed keys in config/build_targets.conf: targets\n"
            + "scripts/read_targets.py:1: reads targets, which the tip removes from config/build_targets.conf\n"
            + FIXTURE_SUMMARY.format(1, 0) + KEY_READS_SUMMARY.format(1, 0)))

    def test_bound_repository_read_reports_a_key_no_config_file_holds(self) -> None:
        self.write("config/hosts/limits.conf", "transfer_timeout.natedev=180\nslots.aws-hana.hana.max_bytes=5\n")
        reader = '''\
from scripts.lib import conf as config

def limits():
    settings = config.read_or_repository("build_targets")
    settings.whole("slots.aws-hana.hana.max")
    settings.whole("transfer_timeout")
    settings.whole(dynamic_key())
    other.whole("not_a_config_read")
'''
        self.write("scripts/limits.py", reader)
        self.write("scripts/test_limits.py", reader)
        status, output = self.run_check(self.commit())
        self.assertEqual(status, 0, output)
        self.assertEqual(output, (
            "scripts/limits.py:5: reads slots.aws-hana.hana.max, which no config file on the tip holds\n"
            + FIXTURE_SUMMARY.format(0, 0) + KEY_READS_SUMMARY.format(0, 1)))

    def test_fixed_reads_resolve_config_parameters_and_bound_key_names(self) -> None:
        self.write("scripts/limits.py", '''\
from scripts.lib.conf import ConfigRead, read, read_or_repository as load_committed

settings = read("build_targets")
module_key = "absent.module_key"
settings.effective_key(module_key)
load_committed("build_targets").values("absent.batch_a", "absent.batch_b", host_only_prefix="ignored.prefix")

def limits(settings: ConfigRead):
    key = "absent.argument"
    settings.text(key=key)
    settings.value("targets", parser_for("ignored.second_argument"))
''')
        status, output = self.run_check(self.commit())
        self.assertEqual(status, 0, output)
        self.assertEqual(output, (
            "scripts/limits.py:5: reads absent.module_key, which no config file on the tip holds\n"
            + "scripts/limits.py:6: reads absent.batch_a, which no config file on the tip holds\n"
            + "scripts/limits.py:6: reads absent.batch_b, which no config file on the tip holds\n"
            + "scripts/limits.py:10: reads absent.argument, which no config file on the tip holds\n"
            + FIXTURE_SUMMARY.format(0, 0) + KEY_READS_SUMMARY.format(0, 4)))

    def test_shell_fixed_reads_and_removed_reads_share_one_sorted_report(self) -> None:
        self.write("config/build_targets.conf", "kind.natedev=local\n")
        self.write("scripts/read_targets.sh", '''\
conf_value "build_targets" 'unlisted_value'
conf_value build_targets targets
conf_get build_targets "unlisted_get"
conf.py get 'build_targets' unlisted_cli
''')
        status, output = self.run_check(self.commit())
        self.assertEqual(status, 0, output)
        self.assertEqual(output, (
            "removed keys in config/build_targets.conf: targets\n"
            + "scripts/read_targets.sh:1: reads unlisted_value, which no config file on the tip holds\n"
            + "scripts/read_targets.sh:2: reads targets, which the tip removes from config/build_targets.conf\n"
            + "scripts/read_targets.sh:3: reads unlisted_get, which no config file on the tip holds\n"
            + "scripts/read_targets.sh:4: reads unlisted_cli, which no config file on the tip holds\n"
            + FIXTURE_SUMMARY.format(0, 0) + KEY_READS_SUMMARY.format(1, 3)))

    def test_advisory_scan_accepts_latin1_and_counts_bad_python(self) -> None:
        _ = (self.repo / "scripts/latin1.py").write_bytes(
            b'# coding: latin-1\n# caf\xe9\n'
            + b'from scripts.lib import conf\nconf.read("build_targets").text("absent.latin1")\n'
        )
        self.write("scripts/bad_syntax.py", "def broken(:\n")
        status, output = self.run_check(self.commit())
        self.assertEqual(status, 0, output)
        self.assertIn("scripts/latin1.py:4: reads absent.latin1, which no config file on the tip holds\n", output)
        self.assertTrue(output.endswith(KEY_READS_SUMMARY.format(0, 1).rstrip("\n")
                                        + ", 1 sources not scanned\n"), output)

    def test_distinct_keys_on_one_line_and_one_report_for_a_split_read(self) -> None:
        self.write("config/build_targets.conf", "kind.natedev=local\n")
        self.write("scripts/reader.py", '''\
from scripts.lib import conf
settings = conf.read("build_targets")
settings.values("targets", "never.present", "targets", "never.present")
settings.text(
    "targets",
)
''')
        status, output = self.run_check(self.commit())
        self.assertEqual(status, 0, output)
        self.assertEqual(output, (
            "removed keys in config/build_targets.conf: targets\n"
            + "scripts/reader.py:3: reads never.present, which no config file on the tip holds\n"
            + "scripts/reader.py:3: reads targets, which the tip removes from config/build_targets.conf\n"
            + "scripts/reader.py:5: reads targets, which the tip removes from config/build_targets.conf\n"
            + FIXTURE_SUMMARY.format(0, 0) + KEY_READS_SUMMARY.format(2, 1)))

    def test_direct_and_aliased_module_imports_resolve_readers(self) -> None:
        self.write("scripts/reader.py", '''\
import scripts.lib.conf as cfg
import scripts.lib.conf
cfg.read("build_targets").whole("absent.alias")
scripts.lib.conf.read("build_targets").whole("absent.direct")
''')
        status, output = self.run_check(self.commit())
        self.assertEqual(status, 0, output)
        self.assertEqual(output, (
            "scripts/reader.py:3: reads absent.alias, which no config file on the tip holds\n"
            + "scripts/reader.py:4: reads absent.direct, which no config file on the tip holds\n"
            + FIXTURE_SUMMARY.format(0, 0) + KEY_READS_SUMMARY.format(0, 2)))

    def test_nested_functions_inherit_readers_even_with_a_local_binding(self) -> None:
        self.write("scripts/reader.py", '''\
from scripts.lib import conf
module_settings = conf.read("build_targets")

def outer():
    settings = conf.read("build_targets")

    def inner():
        settings.whole("absent.enclosing")
        module_settings.whole("absent.module")

    def shadow(settings):
        settings.whole("absent.shadowed")
''')
        status, output = self.run_check(self.commit())
        self.assertEqual(status, 0, output)
        self.assertEqual(output, (
            "scripts/reader.py:8: reads absent.enclosing, which no config file on the tip holds\n"
            + "scripts/reader.py:9: reads absent.module, which no config file on the tip holds\n"
            + "scripts/reader.py:12: reads absent.shadowed, which no config file on the tip holds\n"
            + FIXTURE_SUMMARY.format(0, 0) + KEY_READS_SUMMARY.format(0, 3)))

    def test_reader_bindings_apply_across_reassignments_and_loops(self) -> None:
        self.write("scripts/reader.py", '''\
from scripts.lib import conf

def reads(readers):
    settings = conf.read("build_targets")
    settings.whole("absent.before")
    settings = None
    settings.whole("absent.rebound")
    loop_reader = conf.read("build_targets")
    for loop_reader in readers:
        loop_reader.whole("absent.loop")
''')
        status, output = self.run_check(self.commit())
        self.assertEqual(status, 0, output)
        self.assertEqual(output, (
            "scripts/reader.py:5: reads absent.before, which no config file on the tip holds\n"
            + "scripts/reader.py:7: reads absent.rebound, which no config file on the tip holds\n"
            + "scripts/reader.py:10: reads absent.loop, which no config file on the tip holds\n"
            + FIXTURE_SUMMARY.format(0, 0) + KEY_READS_SUMMARY.format(0, 3)))

    def test_reader_bindings_apply_in_else_branches_and_earlier_closures(self) -> None:
        self.write("scripts/reader.py", '''\
from scripts.lib import conf

def branch(flag):
    settings = conf.read("build_targets")
    if flag:
        settings = None
    else:
        settings.text("absent.else")

def outer():
    def inner():
        settings.text("absent.closure")

    settings = conf.read("build_targets")
    inner()
''')
        status, output = self.run_check(self.commit())
        self.assertEqual(status, 0, output)
        self.assertEqual(output, (
            "scripts/reader.py:8: reads absent.else, which no config file on the tip holds\n"
            + "scripts/reader.py:12: reads absent.closure, which no config file on the tip holds\n"
            + FIXTURE_SUMMARY.format(0, 0) + KEY_READS_SUMMARY.format(0, 2)))

    def test_relative_imports_resolve_the_config_module_and_reader_type(self) -> None:
        self.write("scripts/production/probe.py", '''\
from ..lib import conf
conf.read("build_targets").text("absent.relative_module")
''')
        self.write("scripts/lib/probe.py", '''\
from .conf import ConfigRead

def probe(settings: ConfigRead):
    settings.text("absent.relative_type")
''')
        status, output = self.run_check(self.commit())
        self.assertEqual(status, 0, output)
        self.assertEqual(output, (
            "scripts/lib/probe.py:4: reads absent.relative_type, which no config file on the tip holds\n"
            + "scripts/production/probe.py:2: reads absent.relative_module, which no config file on the tip holds\n"
            + FIXTURE_SUMMARY.format(0, 0) + KEY_READS_SUMMARY.format(0, 2)))

    def test_shell_fixed_reads_accept_assignment_and_interpreter_prefixes(self) -> None:
        self.write("scripts/reader.sh", '''\
CONF_DIR=/tmp conf_value build_targets absent.assignment
./scripts/lib/py ./scripts/lib/conf.py get build_targets absent.wrapper
python3 scripts/lib/conf.py get build_targets absent.python
''')
        status, output = self.run_check(self.commit())
        self.assertEqual(status, 0, output)
        self.assertEqual(output, (
            "scripts/reader.sh:1: reads absent.assignment, which no config file on the tip holds\n"
            + "scripts/reader.sh:2: reads absent.wrapper, which no config file on the tip holds\n"
            + "scripts/reader.sh:3: reads absent.python, which no config file on the tip holds\n"
            + FIXTURE_SUMMARY.format(0, 0) + KEY_READS_SUMMARY.format(0, 3)))

    def test_shell_fixed_reads_use_command_position_and_a_literal_key(self) -> None:
        self.write("config/other.conf", "held.elsewhere=1\n")
        self.write("scripts/reader.sh", '''\
conf_value "build_targets" \\
    continued.key
conf_value "$stem" variable.key
conf_value "$stem" held.elsewhere
: && conf_value 'build_targets' chained.key
:; conf_value build_targets sequence.key
: || conf_value build_targets alternate.key
printf '' | conf_value build_targets pipeline.key
value=$(conf_value build_targets substitution.key)
value=`conf_value build_targets backtick.key`
if :; then conf_value build_targets then.key; else conf_value build_targets else.key; fi
for target in local; do conf_value build_targets loop.key; done
: # conf_value build_targets ignored.comment
echo "conf_value build_targets ignored.example"
echo conf_value build_targets ignored.argument
''')
        status, output = self.run_check(self.commit())
        self.assertEqual(status, 0, output)
        expected = (
            (2, "continued"), (3, "variable"), (5, "chained"), (6, "sequence"), (7, "alternate"),
            (8, "pipeline"), (9, "substitution"), (10, "backtick"), (11, "else"), (11, "then"), (12, "loop"),
        )
        self.assertEqual(output, "".join(
            f"scripts/reader.sh:{line}: reads {key}.key, which no config file on the tip holds\n"
            for line, key in expected
        ) + FIXTURE_SUMMARY.format(0, 0) + KEY_READS_SUMMARY.format(0, len(expected)))

    def test_config_read_annotations_must_resolve_to_the_config_module(self) -> None:
        self.write("scripts/reader.py", '''\
from scripts.lib import conf
from scripts.lib.conf import ConfigRead as Reader

class ConfigRead:
    pass

def local(settings: ConfigRead):
    settings.whole("absent.local")

def foreign(settings: unrelated.ConfigRead):
    settings.whole("absent.foreign")

def declared(settings: conf.ConfigRead):
    settings.whole("absent.qualified")

def imported(settings: Reader):
    settings.whole("absent.imported")
''')
        status, output = self.run_check(self.commit())
        self.assertEqual(status, 0, output)
        self.assertEqual(output, (
            "scripts/reader.py:14: reads absent.qualified, which no config file on the tip holds\n"
            + "scripts/reader.py:17: reads absent.imported, which no config file on the tip holds\n"
            + FIXTURE_SUMMARY.format(0, 0) + KEY_READS_SUMMARY.format(0, 2)))

    def test_class_reads_use_the_module_binding_before_class_reassignment(self) -> None:
        self.write("scripts/reader.py", '''\
from scripts.lib import conf
class Example:
    settings = conf.read("x")
    settings.text("absent.class")
    conf = None
''')
        status, output = self.run_check(self.commit())
        self.assertEqual(status, 0, output)
        self.assertEqual(output, (
            "scripts/reader.py:4: reads absent.class, which no config file on the tip holds\n"
            + FIXTURE_SUMMARY.format(0, 0) + KEY_READS_SUMMARY.format(0, 1)))

    def test_global_and_nonlocal_reader_assignments_bind_in_the_declared_scope(self) -> None:
        self.write("scripts/reader.py", '''\
from scripts.lib import conf

module_settings = None

def load_global():
    global module_settings
    module_settings = conf.read("x")

def read_global():
    module_settings.text("absent.global")

def outer():
    settings = None

    def load_nonlocal():
        nonlocal settings
        settings = conf.read("x")

    def read_nonlocal():
        settings.text("absent.nonlocal")

    load_nonlocal()
    read_nonlocal()

load_global()
read_global()
outer()
''')
        status, output = self.run_check(self.commit())
        self.assertEqual(status, 0, output)
        self.assertEqual(output, (
            "scripts/reader.py:10: reads absent.global, which no config file on the tip holds\n"
            + "scripts/reader.py:20: reads absent.nonlocal, which no config file on the tip holds\n"
            + FIXTURE_SUMMARY.format(0, 0) + KEY_READS_SUMMARY.format(0, 2)))

    def test_a_missing_blob_does_not_stop_later_sources(self) -> None:
        self.write("scripts/after_missing.py", '''\
from scripts.lib import conf
conf.read("x").text("absent.after_missing")
''')
        tip = self.commit()
        missing = "scripts/missing.py"
        self.write(missing, "# This source has no object in the repository.\n")
        sources = {
            missing: self.git("hash-object", "--", str(self.repo / missing)),
            "scripts/after_missing.py": self.git("rev-parse", f"{tip}:scripts/after_missing.py"),
        }
        advisory = fixture_conf_keys.tip_key_reads(self.repo, sources, {}, {})
        self.assertEqual(advisory.sources_not_scanned, 1)
        self.assertEqual(advisory.absent, [
            fixture_conf_keys.AbsentKeyRead("scripts/after_missing.py", 2, "absent.after_missing"),
        ])

    def test_the_scan_runs_without_a_writable_temp_directory(self) -> None:
        self.write("scripts/aws_target/hana_source.py", '''\
from scripts.lib import conf
settings = conf.read("x")
settings.text("slots.aws-hana.hana.max")
settings.text("slots.aws-hana.hana.max_bytes")
''')
        tip = self.commit()
        read_only = self.repo / "read-only"
        read_only.mkdir(mode=0o500)
        self.addCleanup(read_only.chmod, 0o700)
        # Set tempdir to prevent tempfile from using another directory.
        with (mock.patch.dict("os.environ", {"TMPDIR": str(read_only)}),
              mock.patch.object(tempfile, "tempdir", str(read_only))):
            status, output = self.run_check(tip)
        self.assertEqual(status, 0, output)
        self.assertEqual(output, (
            "scripts/aws_target/hana_source.py:3: reads slots.aws-hana.hana.max, which no config file on the tip holds\n"
            + "scripts/aws_target/hana_source.py:4: reads slots.aws-hana.hana.max_bytes, which no config file on the tip holds\n"
            + FIXTURE_SUMMARY.format(0, 0) + KEY_READS_SUMMARY.format(0, 2)))

    def test_key_strings_follow_assignment_order_and_literal_loop_values(self) -> None:
        self.write("config/other.conf", "held=1\n")
        self.base = self.commit()
        self.write("scripts/reader.py", '''\
from scripts.lib import conf
settings = conf.read("x")
key = "held"
settings.text(key)
key = "absent.unused"
for key in ("absent.loop_a", "absent.loop_b"):
    settings.text(key)
''')
        status, output = self.run_check(self.commit())
        self.assertEqual(status, 0, output)
        self.assertEqual(output, (
            "scripts/reader.py:7: reads absent.loop_a, which no config file on the tip holds\n"
            + "scripts/reader.py:7: reads absent.loop_b, which no config file on the tip holds\n"
            + FIXTURE_SUMMARY.format(0, 0) + KEY_READS_SUMMARY.format(0, 2)))

    def test_shell_prefix_skips_redirects_and_treats_a_keyword_after_an_assignment_as_the_command(self) -> None:
        self.write("scripts/reader.sh", '''\
CONF_DIR=/tmp >out conf_value x absent.redirect
CONF_DIR=/tmp then conf_value x absent.control
2>&1 conf_value x absent.descriptor
<in conf_value x absent.input
> out conf_value x absent.output_word
< in conf_value x absent.input_word
''')
        status, output = self.run_check(self.commit())
        self.assertEqual(status, 0, output)
        self.assertEqual(output, (
            "scripts/reader.sh:1: reads absent.redirect, which no config file on the tip holds\n"
            + "scripts/reader.sh:3: reads absent.descriptor, which no config file on the tip holds\n"
            + "scripts/reader.sh:4: reads absent.input, which no config file on the tip holds\n"
            + "scripts/reader.sh:5: reads absent.output_word, which no config file on the tip holds\n"
            + "scripts/reader.sh:6: reads absent.input_word, which no config file on the tip holds\n"
            + FIXTURE_SUMMARY.format(0, 0) + KEY_READS_SUMMARY.format(0, 5)))

    def test_comprehension_walrus_binds_a_name_in_the_enclosing_function(self) -> None:
        self.write("scripts/reader.py", '''\
from scripts.lib import conf

def outer():
    [(settings := None) for _ in range(1)]

    def load():
        nonlocal settings
        settings = conf.read("x")

    def read():
        settings.text("absent.nonlocal")

    load()
    read()

outer()
''')
        status, output = self.run_check(self.commit())
        self.assertEqual(status, 0, output)
        self.assertEqual(output, (
            "scripts/reader.py:11: reads absent.nonlocal, which no config file on the tip holds\n"
            + FIXTURE_SUMMARY.format(0, 0) + KEY_READS_SUMMARY.format(0, 1)))

    def test_an_empty_literal_loop_preserves_the_key_string(self) -> None:
        self.write("scripts/reader.py", '''\
from scripts.lib import conf
settings = conf.read("x")
key = "absent.after_empty"
for key in (): pass
settings.text(key)
''')
        status, output = self.run_check(self.commit())
        self.assertEqual(status, 0, output)
        self.assertEqual(output, (
            "scripts/reader.py:5: reads absent.after_empty, which no config file on the tip holds\n"
            + FIXTURE_SUMMARY.format(0, 0) + KEY_READS_SUMMARY.format(0, 1)))

    def test_a_keyword_after_a_leading_redirect_is_the_command(self) -> None:
        self.write("scripts/reader.sh", "</dev/null then conf_value x absent.control\n")
        status, output = self.run_check(self.commit())
        self.assertEqual(status, 0, output)
        self.assertEqual(output, SUMMARY.format(0, 0))

    def test_a_comprehension_reads_a_key_bound_by_its_walrus(self) -> None:
        self.write("scripts/reader.py", '''\
from scripts.lib import conf

def read():
    settings = conf.read("x")
    [(key := "absent.walrus", settings.text(key)) for _ in range(1)]
''')
        status, output = self.run_check(self.commit())
        self.assertEqual(status, 0, output)
        self.assertEqual(output, (
            "scripts/reader.py:5: reads absent.walrus, which no config file on the tip holds\n"
            + FIXTURE_SUMMARY.format(0, 0) + KEY_READS_SUMMARY.format(0, 1)))

    def test_a_loop_body_assignment_preserves_the_key_from_before_the_loop(self) -> None:
        self.write("scripts/reader.py", '''\
from scripts.lib import conf
settings = conf.read("x")
key = "absent.after_empty"
for key in ():
    key = "absent.never"
settings.text(key)
''')
        status, output = self.run_check(self.commit())
        self.assertEqual(status, 0, output)
        self.assertIn("scripts/reader.py:6: reads absent.after_empty, which no config file on the tip holds\n", output)


if __name__ == "__main__":
    _ = unittest.main()
