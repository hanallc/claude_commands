"""Selection contracts against small committed source trees."""
from __future__ import annotations

import ast
from collections.abc import Iterable
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime
import fcntl
from functools import partial
import json
from multiprocessing import active_children
from multiprocessing.connection import Connection
from multiprocessing.context import SpawnProcess
import os
from pathlib import Path
import posixpath
import signal
import subprocess
import sqlite3
import tempfile
import unittest
from typing import cast, override
from unittest import mock
from weakref import ref

from scripts.buildlog import index as buildlog_index
from scripts.mac_test.eligibility import MAC_LINUX_TARGET
from scripts.production import affected_modules as affected
from scripts.test_times.gate_record_test_support import gate_run


def block_extraction_worker(connection: Connection) -> None:
    with connection:
        _ = cast(object, connection.recv())
        connection.send(os.getpid())
        signal.pause()


def kill_extraction_worker_for_new_test(connection: Connection, *, partial_result: bool) -> None:
    with connection:
        sources = cast(list[affected.IndexedSource], connection.recv())
        if any(path == "scripts/checks/test_new.py" for _, path, _ in sources):
            if partial_result:
                # Write a frame header and one byte before the worker dies.
                _ = os.write(connection.fileno(), b"\x00\x00\x00\x08\x00")
            os.kill(os.getpid(), signal.SIGKILL)
        connection.send(affected.extract_slice_results(sources))


def fail_extraction_worker(connection: Connection) -> None:
    with mock.patch.object(affected, "extract_facts", side_effect=ValueError("unsupported operand type")):
        affected.extract_slice(connection)


class AffectedModulesTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.repo: Path = Path()
        self.base: affected.CertifiedBase = affected.CertifiedBase("", "")
        self.modules: list[Path] = []

    @override
    def setUp(self) -> None:
        self.repo = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="affected-test-")))
        _ = self.git("init", "-b", "main")
        _ = self.git("config", "maintenance.auto", "false")
        _ = self.git("config", "user.name", "Selection Fixture")
        _ = self.git("config", "user.email", "fixture@example.invalid")
        self.set_gate_config()
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {"CONF_DIR": str(self.repo / ".git/test-config")})))
        self.write(".git/test-config/buildlog.conf", (Path(__file__).resolve().parents[2] / "config/buildlog.conf").read_text())
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {"BUILDLOG_DIR": str(self.repo / ".git/buildlog")})))
        self.write(".gitignore", "__pycache__/\n")
        # Ten unrelated modules distinguish direct selection from a full selection.
        for index in range(10):
            self.add_test(f"other/test_unrelated_{index}.py", "VALUE = 1\n")
        self.certify()

    def git(self, *arguments: str) -> str:
        result = subprocess.run(["git", "-C", str(self.repo), *arguments],
                                capture_output=True, text=True, check=True)
        return result.stdout.strip()

    def write(self, path: str, text: str) -> None:
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        _ = target.write_text(text, encoding="utf-8")

    def set_gate_config(self, *, branches: str = "main builds natedev sdlc", scan_roots: str = "",
                        processes: int = 1) -> None:
        self.write(".git/test-config/gate.conf", f"# text:\nlanding_branches={branches}\n"
                   + f"# text:\nscan_roots={scan_roots}\n"
                   + f"# whole: 1..\naffected.extract.processes={processes}\n")

    def add_test(self, path: str, text: str) -> None:
        self.write(f"scripts/{path}", text)
        self.modules.append(self.repo / "scripts" / path)

    def commit(self) -> str:
        _ = self.git("add", ".")
        _ = self.git("commit", "--allow-empty", "-m", "fixture")
        return self.git("rev-parse", "HEAD")

    def certify(self) -> None:
        self.base = affected.CertifiedBase(self.commit(), datetime.now(UTC).isoformat())
        self.record_configs({})

    def record_configs(self, reads: dict[str, list[str]]) -> None:
        modules = [{"suite": path.parent.name, "module": path.name, "attempt": 1,
                    "config_reads": reads.get(path.name, [])} for path in self.modules]
        run = {**gate_run(), "kind": "gate_run", "v": 1, "id": "fixture-full", "host": "mac",
               "selection": "full", "aborted": False, "modules": modules}
        self.write(".git/buildlog/mac/fixture.jsonl", json.dumps(run) + "\n")

    def selection(self) -> affected.AffectedSelection:
        result = affected.select_affected(self.repo, self.base, self.modules, affected.dirty_paths(self.repo))
        self.assertIsInstance(result, affected.AffectedSelection, result)
        assert isinstance(result, affected.AffectedSelection)
        return result

    def cache_dir(self) -> Path:
        return self.repo / ".git" / "gate-state"

    def cache_file(self) -> Path:
        return self.cache_dir() / affected.FACT_CACHE_NAME

    def test_config_selection_uses_the_newest_full_record_and_direct_names(self) -> None:
        self.write("config/limits.conf", "value=one\n")
        self.write("scripts/reader.py", 'from scripts.lib import conf\nconf.read("limits")\n')
        self.add_test("checks/test_runtime.py", "import scripts.reader\n")
        self.add_test("checks/test_indirect.py", "import scripts.reader\n")
        self.add_test("checks/test_named.py", '# config/limits.conf\n')
        self.add_test("checks/test_missing.py", "VALUE = 1\n")
        self.certify()
        self.record_configs({"test_runtime.py": ["limits"]})
        log = self.repo / ".git/buildlog/mac/fixture.jsonl"
        run = cast(dict[str, object], json.loads(log.read_text()))
        module_rows = cast(list[dict[str, object]], run["modules"])
        for row in module_rows:
            if row["module"] == "test_missing.py":
                del row["config_reads"]
        earlier = {**run, "id": "earlier", "started_at": "2020-01-01T00:00:00Z",
                   "modules": [{**row, "config_reads": ["limits"]} for row in module_rows]}
        aborted = {**earlier, "id": "aborted", "started_at": "2030-01-01T00:00:00Z", "aborted": True}
        affected_run = {**aborted, "id": "affected", "aborted": False, "selection": "affected"}
        _ = log.write_text("".join(json.dumps(row) + "\n" for row in (earlier, run, aborted, affected_run)))
        self.add_test("checks/test_new.py", "VALUE = 1\n")
        self.write("config/limits.conf", "value=two\n")
        from scripts.buildlog import index
        with mock.patch.object(index, "read_only", wraps=index.read_only) as reads:
            selected = self.selection()
        self.assertEqual(reads.call_count, 1)
        self.assertEqual({path.name for path in selected.modules}, {"test_runtime.py", "test_named.py", "test_new.py", "test_missing.py"})
        with mock.patch.object(index, "read_only", side_effect=sqlite3.OperationalError("unreadable index")):
            result = affected.select_affected(self.repo, self.base, self.modules, affected.dirty_paths(self.repo))
        self.assertIsInstance(result, affected.FullSuite)
        assert isinstance(result, affected.FullSuite)
        self.assertIn("unreadable index", result.reason)
        self.write("config/limits.conf", "value=one\n")
        (self.repo / "scripts/checks/test_new.py").unlink()
        with mock.patch.object(affected, "latest_module_reads", side_effect=AssertionError("No config changed")):
            _ = affected.select_affected(self.repo, self.base, self.modules[:-1], set())

    def test_config_selection_skips_a_newer_full_gate_without_config_records(self) -> None:
        self.write("config/limits.conf", "value=one\n")
        self.add_test("checks/test_reader.py", "VALUE = 1\n")
        self.add_test("checks/test_other.py", "VALUE = 1\n")
        self.certify()
        self.record_configs({"test_reader.py": ["limits"]})
        log = self.repo / ".git/buildlog/mac/fixture.jsonl"
        recorded = cast(dict[str, object], json.loads(log.read_text()))
        module_rows = cast(list[dict[str, object]], recorded["modules"])
        unrecorded = {**recorded, "id": "newer-unrecorded", "started_at": "2030-01-01T00:00:00Z",
                      "modules": [{key: value for key, value in row.items() if key != "config_reads"}
                                  for row in module_rows]}
        _ = log.write_text("".join(json.dumps(row) + "\n" for row in (recorded, unrecorded)))
        self.write("config/limits.conf", "value=two\n")
        self.assertEqual(self.selection().modules, (self.repo / "scripts/checks/test_reader.py",))

    def test_folder_records_select_added_changed_and_removed_paths(self) -> None:
        self.add_test("production/test_staff_accessors.py", "VALUE = 1\n")
        self.add_test("checks/test_folder.py", "VALUE = 1\n")
        self.write("scripts/component/seed.txt", "base\n")
        self.write("assets/kept.txt", "base\n")
        self.write("docs/kept.txt", "base\n")
        cases: tuple[tuple[str, str, object, bool, tuple[str, ...]], ...] = (
            ("added accessor", "scripts/component/new_accessor.py", [], False, ("test_staff_accessors.py",)),
            ("removed file", "assets/removed.txt", ["assets"], True, ("test_folder.py",)),
            ("removed directory", "assets/old/only.txt", ["assets"], True, ("test_folder.py",)),
            ("changed file", "assets/changed.txt", ["assets"], False, ()),
            ("changed recursive file", "scripts/component/seed.txt", ["scripts/"], False, ("test_folder.py",)),
            ("changed config", "config/limits.conf", ["config"], False, ()),
            ("new directory", "docs/new/deep/page.md", ["docs"], False, ("test_folder.py",)),
            ("unlisted directory", "other/place.txt", [], False, ()),
            ("recursive boundary", "scripts-extra/file.txt", ["scripts/"], False, ()),
            ("null record", "other/unknown.txt", None, False, ("test_folder.py",)),
            ("repository prefix", "other/all.txt", ["/"], False, ("test_folder.py",)),
        )
        for label, path, folders, removed, expected in cases:
            with self.subTest(case=label):
                if removed or label in {"changed file", "changed recursive file", "changed config"}:
                    self.write(path, "base\n")
                self.certify()
                log = self.repo / ".git/buildlog/mac/fixture.jsonl"
                run = cast(dict[str, object], json.loads(log.read_text()))
                module_rows = cast(list[dict[str, object]], run["modules"])
                for row in module_rows:
                    row["folder_scans"] = (["scripts/", "commands/", "docs/"]
                                           if row["module"] == "test_staff_accessors.py" and label == "added accessor"
                                           else folders if row["module"] == "test_folder.py" else [])
                _ = log.write_text(json.dumps(run) + "\n")
                if removed:
                    (self.repo / path).unlink()
                    if label == "removed directory":
                        (self.repo / path).parent.rmdir()
                else:
                    self.write(path, "from scripts.production import staff\n" if label == "added accessor" else "changed\n")
                _ = self.commit()
                self.assertEqual(tuple(module.name for module in self.selection().modules), expected)

    def test_extraction_preserves_import_order_and_lexical_scan_scope(self) -> None:
        facts = affected.extract_facts("""
if enabled:
    import deep as renamed
import shallow
from scripts.lib import conf
ROOT = Path(__file__).parent

@capture(ROOT.glob("decorator"))
def scan(root=ROOT.glob("default")):
    nested = lambda: ROOT.glob("lambda")
    outer(root, lambda: check("ls-files"))
    def child():
        return ROOT.glob("child")

def scan(root):
    outer(root, lambda: check("ls-files"))
    conf.read("nested_setting")
shutil.copytree(factory() / "config", destination)
""", "fixture.py")
        self.assertEqual(facts.imports, ((0, "shallow", ()), (0, "scripts.lib", ("conf",)), (0, "deep", ())))
        self.assertEqual(facts.config_reads, frozenset({"*", "nested_setting"}))
        root = affected.ScanPath(affected.SourceFileRoot(), ("..",))
        parameter = affected.ScanPath(affected.ParameterRoot("root"))
        self.assertEqual(tuple((scope.name, scope.parameters, scope.scans) for scope in facts.scan_scopes), (
            ("", (), ()),
            ("scan", ("root",), (affected.ScanPattern(parameter, "*", True),)),
            ("scan.child", (), (affected.ScanPattern(root, "child", False),)),
        ))

    def test_deep_defaults_keep_facts_and_select_the_config_reader(self) -> None:
        template = ('from scripts.lib import conf\n'
                    + 'def read(value={negations}conf.read("deep_setting")):\n'
                    + '    return value\n')
        path = "scripts/checks/test_reader.py"
        shallow = template.format(negations="not ")
        expected = affected.extract_facts(shallow, path)
        self.assertEqual(expected.config_reads, frozenset({"deep_setting"}))
        self.add_test("checks/test_reader.py", shallow)
        for depth in (988, 1800):
            with self.subTest(depth=depth):
                source = template.format(negations="not " * depth)
                self.assertEqual(affected.extract_facts(source, path), expected)
                self.write(path, source)
                self.write("config/deep_setting.conf", "value=before\n")
                self.certify()
                self.write("config/deep_setting.conf", "value=after\n")
                _ = self.commit()
                self.assertEqual(self.selection().modules, (self.repo / path,))

    def test_extraction_releases_ast_nodes(self) -> None:
        original_parse = ast.parse
        functions: list[ref[ast.expr]] = []

        def parse(text: str, *, filename: str) -> ast.Module:
            tree = original_parse(text, filename=filename)
            statement = tree.body[0]
            assert isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call)
            functions.append(ref(statement.value.func))
            return tree

        with mock.patch.object(ast, "parse", new=parse):
            _ = affected.extract_facts('read("settings")', "fixture.py")
            self.assertIsNone(functions[0]())

    def test_copytree_config_detection_applies_to_the_source(self) -> None:
        facts = affected.extract_facts('shutil.copytree(source, destination / "config")', "fixture.py")
        self.assertEqual(facts.config_reads, frozenset())

    def test_scan_graph_reuses_shared_acyclic_calls(self) -> None:
        caller = "scripts/checks/test_caller.py"
        helper = "scripts/lib/audit.py"
        leaf = "scripts/lib/leaf.py"
        facts = {
            caller: affected.extract_facts("""
from pathlib import Path
from scripts.lib import audit
ROOT = Path(__file__).parent

def shared(folder):
    return (folder / "marker").glob("*.txt")

def first():
    return shared(ROOT)

def second():
    return shared(folder=ROOT)

def different():
    return shared(ROOT / "other")

def cycle_a(folder):
    cycle_b(folder / "a")
    return folder.glob("*.a")

def cycle_b(folder):
    cycle_a(folder / "b")
    return folder.glob("*.b")

def from_a():
    return cycle_a(ROOT)

def from_b():
    return cycle_b(ROOT)

FILES = audit.collect(ROOT)
""", caller),
            helper: affected.extract_facts("""
from pathlib import Path
from scripts.lib import leaf

def collect(folder):
    leaf.deep(folder)
    return folder.glob("*.remote")

FILES = collect(Path(__file__).parent)
""", helper),
            leaf: affected.extract_facts('def deep(folder):\n    return folder.glob("*.deep")\n', leaf),
        }
        normalize = posixpath.normpath
        marker_calls: list[str] = []

        def count_marker_paths(path: str | bytes) -> str | bytes:
            if isinstance(path, str) and path.endswith("/marker"):
                marker_calls.append(path)
            return normalize(path)

        with mock.patch.object(posixpath, "normpath", count_marker_paths):
            scans = affected.repository_scan_graph(self.repo, facts, {})
        self.assertEqual(scans, {
            caller: frozenset(affected.RepositoryScan(folder, pattern, False) for folder, pattern in (
                ("scripts/checks/marker", "*.txt"), ("scripts/checks/other/marker", "*.txt"),
                ("scripts/checks", "*.remote"), ("scripts/checks", "*.a"), ("scripts/checks", "*.b"),
                ("scripts/checks/a", "*.b"), ("scripts/checks/b", "*.a"),
            )),
            helper: frozenset(affected.RepositoryScan("scripts/lib", pattern, False) for pattern in ("*.remote", "*.deep")),
            leaf: frozenset(),
        })
        self.assertEqual(len(marker_calls), 2)

    def test_source_audits_select_changed_file_kinds_without_scan_records(self) -> None:
        scans = (
            ('ROOT.rglob("*.py")', "scripts/component/subject.py", True),
            ('ROOT.rglob("*.py")', "config/limits.conf", False),
            ('ROOT.glob("**/*.py")', "scripts/component/subject.py", True),
            ('os.walk(ROOT)', "scripts/component/subject.py", True),
            ('subprocess.check_output(["git", "ls-files"], cwd=ROOT)', "commands/subject.md", True),
            ('scan(ROOT)', "scripts/component/subject.py", True),
            ('Audit.scan(ROOT)', "scripts/component/subject.py", True),
            ('imported_scan(ROOT)', "scripts/component/subject.py", True),
            ('audit.scan(ROOT)', "scripts/component/subject.py", True),
            ('audit', "scripts/component/subject.py", False),
            ('bare_audit.scan(ROOT)', "scripts/component/subject.py", True),
            ('scan_imported_root()', "scripts/component/subject.py", True),
            ('IMPORTED_ROOT.rglob("*.py")', "scripts/component/subject.py", True),
            ('IMPORTED_ROOT.rglob("*.py")', "assets/unrelated.txt", False),
            ('imported_scan(IMPORTED_ROOT)', "scripts/component/subject.py", True),
            ('imported_scan(scratch)', "scripts/component/subject.py", False),
            ('scratch.rglob("*.py")', "scripts/component/subject.py", False),
            ('source_scan(ROOT)', "assets/unrelated.txt", False),
            ('audit.scan(ROOT)', "assets/unrelated.txt", False),
            ('audit.noop(ROOT)', "scripts/unrelated/subject.py", False),
            ('(ROOT / "scripts/accounts").glob("*.py")', "scripts/unrelated/subject.py", False),
            ('(ROOT / "scripts/accounts").glob("*.py")', "scripts/accounts/nested/subject.py", False),
            ('(ROOT / "scripts/accounts").glob("*.py")', "scripts/accounts/subject.py", True),
            ('(ROOT / "scripts/accounts").rglob("*.py")', "scripts/accounts/nested/subject.py", True),
            ('(ROOT / "scripts/accounts").rglob("*.py")', "scripts/unrelated/subject.py", False),
            ("config_test_support", "assets/unrelated.conf", False),
            ("config_test_support", "config/limits.conf", True),
            ('declared_scan(ROOT)', "scripts/component/nested/subject.txt", True),
            ('declared_scan(ROOT)', "commands/component/nested/subject.txt", True),
            ('declared_scan(ROOT)', "outside/subject.md", False),
            ('(ROOT / os.environ.get("SCAN_FOLDER", "scripts")).rglob("*.py")', "scripts/component/subject.py", True),
            ('ROOT.glob(os.environ.get("SCAN_PATTERN", "**/*.py"))', "scripts/component/subject.py", True),
            ('(ROOT / os.environ.get("SCAN_FOLDER", "scripts/component")).glob("*.py")', "scripts/component/subject.py", True),
            (f"Path({str(self.repo)!r}).rglob('*.py')", "scripts/component/subject.py", True),
        )
        declaration_errors = {
            "checks/test_absent.py:scripts/component": "test module is not a live gate module",
            "checks/test_audit.py:scripts/stlye": "repository path does not exist",
            "checks/test_audit.py:scripts/component:extra": "expected exactly one ':'",
        }
        cases = [(expression, changed, selected,
                  "checks/test_audit.py:scripts/component checks/test_audit.py:commands/component"
                  if expression == "declared_scan(ROOT)" else "") for expression, changed, selected in scans]
        cases.extend(("declared_scan(ROOT)", "outside/subject.md", False, declaration)
                     for declaration in declaration_errors)
        self.write("commands/component/seed.txt", "fixture\n")
        for expression, changed, selected, declaration in cases:
            with self.subTest(scan=expression, changed=changed, declaration=declaration):
                self.set_gate_config(scan_roots=declaration)
                self.write(changed, "# before\n")
                self.write("scripts/component/audit.py", "import os\nfrom scripts.component import directory_walk\n"
                    + "def scan(folder):\n    return list(folder.rglob('*.py'))\n"
                    + "def noop(folder):\n    return None\n"
                    + "def unused_walk(folder):\n    return list(os.walk(folder))\n")
                self.write("scripts/component/directory_walk.py", "import os\n"
                    + "def scan(folder):\n    return list(os.walk(folder))\n")
                self.write("scripts/component/root_audit.py", "from pathlib import Path\n"
                    + "ROOT = Path(__file__).resolve().parents[2]\n"
                    + "def scan(folder):\n    return list(folder.rglob('*.py'))\n")
                self.write("scripts/component/source_root.py", "from pathlib import Path\nROOT = Path(__file__).resolve().parents[2]\n")
                self.write("scripts/component/config_test_support.py", "from pathlib import Path\n"
                    + "SOURCE = Path(__file__).resolve().parents[2] / 'config'\n"
                    + "def config_files():\n    return list(SOURCE.glob('*.conf'))\n")
                self.add_test("checks/test_audit.py", "from pathlib import Path\nimport os, subprocess\n"
                    + "from scripts.component.audit import scan as imported_scan\nfrom scripts.component import audit\n"
                    + "import audit as bare_audit\n"
                    + ("from scripts.component import config_test_support\n" if expression == "config_test_support" else "")
                    + ("from scripts.component.root_audit import scan as root_scan, ROOT as IMPORTED_ROOT\n"
                       + "def scan_imported_root():\n    return root_scan(IMPORTED_ROOT)\n" if expression == "scan_imported_root()" else "")
                    + ("from scripts.component.source_root import ROOT as IMPORTED_ROOT\n"
                       if expression in {'IMPORTED_ROOT.rglob("*.py")', "imported_scan(IMPORTED_ROOT)"} else "")
                    + "ROOT = Path(__file__).resolve().parents[2]\nscratch = Path('/fixture')\n"
                    + "def scan(folder):\n    return list(folder.rglob('*.py'))\n"
                    + "def declared_scan(folder):\n    return list((folder / 'outside').glob('*.md'))\n"
                    + "def source_scan(folder):\n    return list(folder.rglob('*.py'))\n"
                    + "def scratch_scan(folder):\n    return list(os.walk(folder))\n"
                    + "class Audit:\n    @staticmethod\n    def scan(folder):\n        return list(folder.rglob('*.py'))\n"
                    + f"FILES = {expression}\n")
                self.certify()
                self.record_configs({})
                self.write(changed, "# after\n")
                _ = self.commit()
                for cached in (False, True):
                    with self.subTest(cached=cached):
                        result = affected.select_affected(self.repo, self.base, self.modules,
                            affected.dirty_paths(self.repo), state_dir=self.cache_dir())
                        if declaration in declaration_errors:
                            self.assertIsInstance(result, affected.FullSuite)
                            assert isinstance(result, affected.FullSuite)
                            self.assertIn("graph error:", result.reason)
                            self.assertIn(declaration, result.reason)
                            self.assertIn(declaration_errors[declaration], result.reason)
                        else:
                            self.assertIsInstance(result, affected.AffectedSelection)
                            assert isinstance(result, affected.AffectedSelection)
                            self.assertEqual(result.modules, (self.repo / "scripts/checks/test_audit.py",) if selected else ())
                self.modules.remove(self.repo / "scripts/checks/test_audit.py")

        words = [
            "issue_score/test_experiment.py:config/issue_score.conf",
            "issue_score/test_score_issues.py:config/issue_score.conf",
            "maintenance/test_vault_commit.py:config/agent_notes.toml",
            "review_links/test_review_link.py:scripts/lib/py",
            "usage/test_collect_mac.py:scripts/lib/py",
        ]
        data_inputs = (
            ("config/issue_score.conf", ("issue_score/test_experiment.py", "issue_score/test_score_issues.py")),
            ("config/agent_notes.toml", ("maintenance/test_vault_commit.py",)),
            ("scripts/lib/py", ("review_links/test_review_link.py", "usage/test_collect_mac.py")),
            ("outside/subject.md", ("agents/test_models_conf_publish.py", "lint/test_style_review.py")),
        )
        for changed, names in data_inputs:
            with self.subTest(changed=changed):
                self.write(changed, "before\n")
                declaration = " ".join(word for word in words if word.partition(":")[0] in names)
                for word in declaration.split():
                    path = self.repo / word.partition(":")[2]
                    if not path.exists():
                        path.mkdir(parents=True)
                self.set_gate_config(scan_roots=declaration)
                for name in names:
                    self.add_test(name, "from pathlib import Path\n"
                                  + "ROOT = Path(__file__).resolve().parents[2]\nFILES = list(ROOT.rglob('*'))\n")
                self.certify()
                self.write(changed, "after\n")
                _ = self.commit()
                for cached in (False, True):
                    with self.subTest(cached=cached):
                        result = affected.select_affected(self.repo, self.base, self.modules,
                            affected.dirty_paths(self.repo), state_dir=self.cache_dir())
                        self.assertIsInstance(result, affected.AffectedSelection)
                        assert isinstance(result, affected.AffectedSelection)
                        self.assertEqual({path.relative_to(self.repo).as_posix() for path in result.modules},
                                         {f"scripts/{name}" for name in names})
                for name in names:
                    self.modules.remove(self.repo / "scripts" / name)

    def test_content_reads_select_read_files_and_keep_config_selection(self) -> None:
        self.add_test("checks/test_reader.py", "VALUE = 1\n")
        cases: tuple[tuple[str, str, object, bool, bool, tuple[str, ...]], ...] = (
            ("changed script", "scripts/component/subject.py", ["scripts/component/subject.py"], False, True, ("test_reader.py",)),
            ("removed script", "scripts/component/subject.py", ["scripts/component/subject.py"], True, True, ("test_reader.py",)),
            ("copied config", "config/limits.conf", ["config/limits.conf"], False, True, ("test_reader.py",)),
            ("null content", "scripts/component/subject.py", None, False, True, ("test_reader.py",)),
            ("empty content", "scripts/component/subject.py", [], False, True, ()),
            ("no content gate", "scripts/component/subject.py", [], False, False, ()),
        )
        for label, path, contents, removed, recorded, expected in cases:
            with self.subTest(case=label):
                self.write(path, "VALUE = 1\n")
                self.certify()
                log = self.repo / ".git/buildlog/mac/fixture.jsonl"
                run = cast(dict[str, object], json.loads(log.read_text()))
                module_rows = cast(list[dict[str, object]], run["modules"])
                for row in module_rows:
                    row["folder_scans"] = []
                    if recorded:
                        row["content_reads"] = contents if row["module"] == "test_reader.py" else []
                run.update(id="content-full", started_at="2028-01-01T00:00:00Z")
                newer = {**run, "id": "newer-no-content", "started_at": "2029-01-01T00:00:00Z",
                         "modules": [{key: value for key, value in row.items() if key != "content_reads"}
                                     for row in module_rows]}
                ignored: list[dict[str, object]] = [{**run, "id": selection, "selection": selection, "aborted": aborted,
                            "started_at": "2030-01-01T00:00:00Z",
                            "modules": [{**row, "content_reads": []} for row in module_rows]}
                           for selection, aborted in (("full", True), ("affected", False))]
                _ = log.write_text("".join(json.dumps(record) + "\n" for record in (run, newer, *ignored)))
                if removed:
                    (self.repo / path).unlink()
                else:
                    self.write(path, "# .astimezone()\nVALUE = 2\n")
                _ = self.commit()
                self.assertEqual(tuple(module.name for module in self.selection().modules), expected)

    def test_module_history_uses_run_id_lookups(self) -> None:
        query = affected.MODULE_HISTORY_SQL
        old_query = (
            "WITH latest AS (SELECT "
            + "(SELECT id FROM gate_runs WHERE selection = 'full' AND aborted = 0 "
            + "AND EXISTS (SELECT 1 FROM gate_modules AS recorded WHERE recorded.run_id = gate_runs.id "
            + "AND recorded.config_reads IS NOT NULL) ORDER BY started_at DESC, id DESC LIMIT 1) AS config_gate, "
            + "(SELECT id FROM gate_runs WHERE selection = 'full' AND aborted = 0 "
            + "AND EXISTS (SELECT 1 FROM gate_modules AS recorded WHERE recorded.run_id = gate_runs.id "
            + "AND recorded.folder_scans IS NOT NULL) ORDER BY started_at DESC, id DESC LIMIT 1) AS folder_gate, "
            + "(SELECT id FROM gate_runs WHERE selection = 'full' AND aborted = 0 "
            + "AND EXISTS (SELECT 1 FROM gate_modules AS recorded WHERE recorded.run_id = gate_runs.id "
            + "AND recorded.content_reads IS NOT NULL) ORDER BY started_at DESC, id DESC LIMIT 1) AS content_gate) "
            + "SELECT suite, module, CASE WHEN run_id = config_gate THEN config_reads END, "
            + "CASE WHEN run_id = folder_gate THEN folder_scans END, "
            + "CASE WHEN run_id = content_gate THEN content_reads END FROM gate_modules CROSS JOIN latest "
            + "WHERE run_id = config_gate OR run_id = folder_gate OR run_id = content_gate ORDER BY suite, module, attempt"
        )
        with closing(sqlite3.connect(":memory:")) as connection:
            for statement in buildlog_index.schema_statements():
                _ = connection.execute(statement)
            plan = cast(list[tuple[int, int, int, str]],
                        connection.execute("EXPLAIN QUERY PLAN " + query, (MAC_LINUX_TARGET,)).fetchall())
            details = [row[3] for row in plan]
            self.assertIn("SEARCH gate_modules USING INDEX sqlite_autoindex_gate_modules_1 (run_id=?)", details)
            self.assertFalse(any(detail.startswith("SCAN gate_modules") for detail in details), details)

            _ = connection.executemany("INSERT INTO gate_runs (id, started_at, selection, aborted) VALUES (?, ?, ?, ?)", (
                ("shared", "2026-01-01", "full", 0),
                ("folder", "2026-01-02", "full", 0),
                ("older-tie", "2026-01-03", "full", 0),
                ("winner-tie", "2026-01-03", "full", 0),
                ("null", "2026-01-04", "full", 0),
                ("aborted", "2026-01-05", "full", 1),
                ("affected", "2026-01-06", "affected", 0),
            ))
            insert = ("INSERT INTO gate_modules (run_id, suite, module, attempt, config_reads, folder_scans, "
                      + "content_reads) VALUES (?, 'checks', 'test_reader.py', ?, ?, ?, ?)")
            _ = connection.execute(insert, ("shared", 1, '["shared"]', '[]', '["assets/x"]'))
            shared = cast(list[tuple[object, ...]], connection.execute(query, (MAC_LINUX_TARGET,)).fetchall())
            self.assertCountEqual(shared, cast(list[tuple[object, ...]], connection.execute(old_query).fetchall()))
            self.assertEqual(shared, [("checks", "test_reader.py", '["shared"]', '[]', '["assets/x"]')])

            _ = connection.executemany(insert, (
                ("folder", 1, None, '["assets"]', None),
                ("older-tie", 1, '["loser"]', None, None),
                ("winner-tie", 1, '[]', None, None),
                ("winner-tie", 2, '["retry"]', None, None),
                ("null", 1, None, None, None),
                ("aborted", 1, '[]', '[]', '[]'),
                ("affected", 1, '[]', '[]', '[]'),
            ))
            separate = cast(list[tuple[object, ...]], connection.execute(query, (MAC_LINUX_TARGET,)).fetchall())
            self.assertCountEqual(separate, cast(list[tuple[object, ...]], connection.execute(old_query).fetchall()))
            self.assertCountEqual(separate, [
                ("checks", "test_reader.py", None, None, '["assets/x"]'),
                ("checks", "test_reader.py", None, '["assets"]', None),
                ("checks", "test_reader.py", '[]', None, None),
                ("checks", "test_reader.py", '["retry"]', None, None),
            ])

    def test_folder_and_config_records_use_independent_latest_full_gates(self) -> None:
        self.write("config/limits.conf", "value=one\n")
        self.write("assets/kept.txt", "base\n")
        self.add_test("checks/test_config.py", "VALUE = 1\n")
        self.add_test("checks/test_folder.py", "VALUE = 1\n")
        self.add_test("checks/test_unrecorded.py", "VALUE = 1\n")
        self.certify()
        self.record_configs({"test_config.py": ["limits"]})
        log = self.repo / ".git/buildlog/mac/fixture.jsonl"
        config_gate = cast(dict[str, object], json.loads(log.read_text()))
        config_rows = cast(list[dict[str, object]], config_gate["modules"])
        folder_rows = [{key: value for key, value in row.items() if key != "config_reads"} for row in config_rows]
        for row in folder_rows:
            row["folder_scans"] = ["assets"] if row["module"] == "test_folder.py" else []
            if row["module"] == "test_unrecorded.py":
                row["folder_scans"] = None
        folder_gate = {**config_gate, "id": "folder-full", "started_at": "2030-01-01T00:00:00Z", "modules": folder_rows}
        empty_gate = {**folder_gate, "id": "newer-no-records", "started_at": "2031-01-01T00:00:00Z",
                      "modules": [{key: value for key, value in row.items() if key != "folder_scans"} for row in folder_rows]}
        for selection, aborted in (("full", True), ("affected", False)):
            ignored = {**folder_gate, "id": "ignored", "started_at": "2032-01-01T00:00:00Z",
                       "selection": selection, "aborted": aborted,
                       "modules": [{**row, "folder_scans": ["/"]} for row in folder_rows]}
            _ = log.write_text("".join(json.dumps(run) + "\n" for run in (config_gate, folder_gate, empty_gate, ignored)))
            self.write("config/limits.conf", "value=two\n")
            self.write("assets/new.txt", "added\n")
            self.assertEqual({module.name for module in self.selection().modules},
                             {"test_config.py", "test_folder.py", "test_unrecorded.py"})

    def test_newer_guest_gate_does_not_supply_module_reads(self) -> None:
        module = {"suite": "checks", "module": "test_reader.py", "attempt": 1}
        run = {**gate_run(), "kind": "gate_run", "v": 1, "id": "full", "host": "mac",
               "selection": "full", "aborted": False, "started_at": "2026-01-01T00:00:00Z",
               "modules": [{**module, "config_reads": ["limits"], "folder_scans": ["assets"],
                            "content_reads": ["docs/index.md"]}]}
        self.write(".git/buildlog/mac/fixture.jsonl", json.dumps(run) + "\n")
        expected = affected.latest_module_reads()
        self.assertEqual(set(expected), {"scripts/checks/test_reader.py"})

        guest_module: dict[str, object] = {**module, "config_reads": [], "folder_scans": [], "content_reads": []}
        guest: dict[str, object] = {**run, "id": "guest-full", "host": "mac-linux", "source": "mac-linux",
                                    "started_at": "2026-01-02T00:00:00Z", "modules": [guest_module]}
        self.write(".git/buildlog/mac-linux/2026-01.jsonl", json.dumps(guest) + "\n")
        self.assertEqual(affected.latest_module_reads(), expected)

    def test_landing_bases_exclude_changes_that_already_passed_the_gate(self) -> None:
        self.write("scripts/landed/subject.py", "VALUE = 1\n")
        self.write("scripts/own/subject.py", "VALUE = 1\n")
        self.add_test("checks/test_landed.py", "import scripts.landed.subject\n")
        self.add_test("checks/test_own.py", "import scripts.own.subject\n")
        self.certify()
        self.write("scripts/landed/subject.py", "VALUE = 2\n")
        landing = self.commit()
        _ = self.git("update-ref", "refs/remotes/origin/builds", landing)
        self.write("scripts/own/subject.py", "VALUE = 2\n")
        _ = self.commit()
        result = self.selection()
        self.assertEqual(result.changed_files, ("scripts/own/subject.py",))
        self.assertEqual(result.modules, (self.repo / "scripts/checks/test_own.py",))
        self.assertEqual(result.base, affected.CertifiedBase(landing, self.git("show", "-s", "--format=%cI", landing)))
        without_certificate = affected.select_affected(self.repo, affected.NoCertifiedBase(), self.modules, set())
        self.assertEqual(without_certificate, result)

    def test_a_merge_with_a_landing_branch_keeps_the_units_earlier_changes(self) -> None:
        self.write("scripts/landed/subject.py", "VALUE = 1\n")
        self.write("scripts/own/subject.py", "VALUE = 1\n")
        self.add_test("checks/test_landed.py", "import scripts.landed.subject\n")
        self.add_test("checks/test_own.py", "import scripts.own.subject\n")
        self.certify()
        _ = self.git("switch", "-c", "unit")
        self.write("scripts/own/subject.py", "VALUE = 2\n")
        _ = self.commit()
        _ = self.git("switch", "main")
        self.write("scripts/landed/subject.py", "VALUE = 2\n")
        landing = self.commit()
        _ = self.git("update-ref", "refs/remotes/origin/builds", landing)
        _ = self.git("switch", "unit")
        _ = self.git("merge", "--no-ff", "main", "-m", "Merge the landing branch")
        result = self.selection()
        self.assertEqual(result.changed_files, ("scripts/own/subject.py",))
        self.assertEqual(result.modules, (self.repo / "scripts/checks/test_own.py",))
        self.assertEqual(result.base.commit, landing)

    def test_a_join_selects_a_test_that_main_added_for_a_module_the_row_changed(self) -> None:
        # Red 2 of item 191: the row changed a settings type, and main had added a test that builds it.
        self.write("scripts/lib/eligibility.py", "VALUE = 1\n")
        self.certify()
        _ = self.git("switch", "-c", "row")
        self.write("scripts/lib/eligibility.py", "VALUE = 2\n")
        _ = self.commit()
        self.assertEqual(self.selection().modules, ())
        _ = self.git("switch", "main")
        self.write("scripts/buildlog/machine_facts.py", "from scripts.lib import eligibility\n")
        self.add_test("buildlog/test_machine_facts.py", "from scripts.lib import eligibility\n")
        _ = self.git("update-ref", "refs/remotes/origin/main", self.commit())
        _ = self.git("switch", "row")
        _ = self.git("merge", "--no-ff", "main", "-m", "Join main")
        result = self.selection()
        self.assertEqual(result.changed_files, ("scripts/lib/eligibility.py",))
        self.assertEqual(result.modules, (self.repo / "scripts/buildlog/test_machine_facts.py",))

    def test_recorded_content_reads_select_a_test_whose_module_embeds_a_changed_source(self) -> None:
        # Red 3 of item 191: gate_box.GUEST embeds the startup hook source through inspect.getsource.
        self.write("scripts/lib/sitecustomize.py", "NAME = 'gate'\n")
        # A computed module name keeps the import graph from selecting the test.
        self.write("scripts/aws_target/gate_box.py", "import importlib\nimport inspect\nHOOK = 'site' + 'customize'\n"
                   + "GUEST = inspect.getsource(importlib.import_module(f'scripts.lib.{HOOK}'))\n")
        self.add_test("aws_target/test_gate_box.py", "from scripts.aws_target import gate_box\n")
        self.certify()
        log = self.repo / ".git/buildlog/mac/fixture.jsonl"
        run = cast(dict[str, object], json.loads(log.read_text()))
        for row in cast(list[dict[str, object]], run["modules"]):
            row["folder_scans"] = []
            row["content_reads"] = (["scripts/lib/sitecustomize.py", "scripts/aws_target/gate_box.py"]
                                    if row["module"] == "test_gate_box.py" else [])
        _ = log.write_text(json.dumps(run) + "\n")
        self.write("scripts/lib/sitecustomize.py", "NAME = 'merge_gate'\n")
        _ = self.commit()
        self.assertEqual(self.selection().modules, (self.repo / "scripts/aws_target/test_gate_box.py",))

    def test_a_dirty_file_at_the_landing_tip_is_the_only_changed_path(self) -> None:
        self.add_test("checks/test_own.py", "VALUE = 1\n")
        landing = self.commit()
        _ = self.git("update-ref", "refs/remotes/origin/builds", landing)
        self.write("scripts/checks/test_own.py", "VALUE = 2\n")
        result = self.selection()
        self.assertEqual(result.changed_files, ("scripts/checks/test_own.py",))
        self.assertEqual(result.modules, (self.repo / "scripts/checks/test_own.py",))
        self.assertEqual(result.base.commit, landing)

    def test_base_choice_counts_paths_and_preserves_candidate_priority(self) -> None:
        for name in ("first", "second", "own"):
            self.add_test(f"checks/test_{name}.py", "VALUE = 0\n")
        self.certify()
        self.write("scripts/checks/test_first.py", "VALUE = 1\n")
        first = self.commit()
        self.write("scripts/checks/test_second.py", "VALUE = 1\n")
        second = self.commit()
        self.write("scripts/checks/test_first.py", "VALUE = 0\n")
        landing = self.commit()
        self.write("scripts/checks/test_own.py", "VALUE = 1\n")
        _ = self.commit()
        unrelated = self.git("commit-tree", "HEAD^{tree}", "-m", "Unrelated history")
        for branch, commit in (("main", first), ("builds", second), ("natedev", landing),
                               ("sdlc", landing), ("stable", self.base.commit), ("unrelated", unrelated)):
            _ = self.git("update-ref", f"refs/remotes/origin/{branch}", commit)
        cases = (
            (self.base, "main", self.base.commit, ("own", "second")),
            (self.base, "builds", self.base.commit, ("own", "second")),
            (self.base, "main builds natedev sdlc", landing, ("own",)),
            (affected.NoCertifiedBase(), "builds stable", second, ("first", "own")),
            (affected.NoCertifiedBase(), "stable builds", self.base.commit, ("own", "second")),
            (self.base, "missing unrelated", self.base.commit, ("own", "second")),
        )
        for certificate, branches, expected_base, changed in cases:
            with self.subTest(branches=branches, certificate=certificate):
                self.set_gate_config(branches=branches)
                with mock.patch.object(affected, "git_output", wraps=affected.git_output) as git:
                    result = affected.select_affected(self.repo, certificate, self.modules, set())
                self.assertIsInstance(result, affected.AffectedSelection)
                assert isinstance(result, affected.AffectedSelection)
                self.assertEqual(result.base.commit, expected_base)
                expected_paths = tuple(f"scripts/checks/test_{name}.py" for name in changed)
                self.assertEqual(result.changed_files, expected_paths)
                self.assertCountEqual(result.modules, tuple(self.repo / path for path in expected_paths))
                compared = [call.args[-2] for call in git.call_args_list if call.args[1] == "diff"]
                self.assertEqual(len(compared), len(set(compared)))

    def test_cached_scopes_share_identical_binding_tuples(self) -> None:
        facts = affected.extract_facts("""
ROOT = Path(__file__).parent

def left():
    return ROOT.glob("left")

def right():
    return ROOT.rglob("right")

def shadow(root):
    ROOT = root
    return ROOT.glob("shadow")
""", "fixture.py")
        _, payload, _ = affected.fact_row("blob", facts)
        row = cast(dict[str, object], json.loads(payload))
        self.assertIsInstance(row["scan_bindings"], list)
        self.assertEqual(len(cast(list[object], row["scan_bindings"])), 2)
        decoded = affected.decoded_facts(row)
        self.assertEqual(decoded, facts)
        module, left, right, shadow = decoded.scan_scopes
        self.assertIs(module.bindings, left.bindings)
        self.assertIs(left.bindings, right.bindings)
        self.assertIsNot(shadow.bindings, left.bindings)

        scope_rows = cast(list[list[object]], row["scan_scopes"])
        for index in (-1, 2, True, "0"):
            with self.subTest(index=index):
                scope_rows[0][2] = index
                with self.assertRaisesRegex(ValueError, "invalid scan binding index"):
                    _ = affected.decoded_facts(row)

    def test_cached_and_uncached_graphs_are_equal(self) -> None:
        sources = {
            "scripts/component/__init__.py": "VALUE = 1\n",
            "scripts/component/subject.py": "VALUE = 2\n",
            "scripts/duplicate/subject.py": "VALUE = 3\n",
            "scripts/component/test_adapter.py": "from . import subject\nfrom ..component import subject\n",
            "scripts/checks/test_imports.py": "import scripts.component.test_adapter, subject\nfrom subject import VALUE\nimport json\n",
        }
        for path, text in sources.items():
            self.write(path, text)
        _ = self.commit()
        expected = affected.dependency_graph(self.repo)
        cold = affected.dependency_graph(self.repo, state_dir=self.cache_dir())
        with mock.patch.object(ast, "parse", side_effect=AssertionError("warm cache parsed source")):
            warm = affected.dependency_graph(self.repo, state_dir=self.cache_dir())
        self.assertEqual(cold, expected)
        self.assertEqual(warm, expected)

    def test_new_blob_reparses_only_the_changed_file(self) -> None:
        self.write("scripts/component/test_subject.py", "VALUE = 20\n")
        _ = self.commit()
        _ = affected.dependency_graph(self.repo, state_dir=self.cache_dir())
        old_blob = self.git("rev-parse", "HEAD:scripts/component/test_subject.py")
        self.write("scripts/component/test_subject.py", "VALUE = 21\n")
        _ = self.commit()
        new_blob = self.git("rev-parse", "HEAD:scripts/component/test_subject.py")
        with closing(sqlite3.connect(self.cache_file())) as database, database:
            _ = database.executescript("""
                CREATE TABLE writes (operation TEXT, blob TEXT);
                CREATE TRIGGER log_insert AFTER INSERT ON facts BEGIN
                    INSERT INTO writes VALUES ('insert', new.blob);
                END;
                CREATE TRIGGER log_update AFTER UPDATE ON facts BEGIN
                    INSERT INTO writes VALUES ('update', new.blob);
                END;
                CREATE TRIGGER log_delete AFTER DELETE ON facts BEGIN
                    INSERT INTO writes VALUES ('delete', old.blob);
                END;
                """)
        with (mock.patch.object(ast, "parse", wraps=ast.parse) as parse,
              mock.patch.object(affected, "fact_row", wraps=affected.fact_row) as encode):
            actual = affected.dependency_graph(self.repo, state_dir=self.cache_dir())
        self.assertEqual([call.kwargs["filename"] for call in parse.call_args_list],
                         ["scripts/component/test_subject.py"])
        self.assertEqual([call.args[0] for call in encode.call_args_list], [new_blob])
        with closing(sqlite3.connect(self.cache_file())) as database:
            writes = cast(list[tuple[str, str]], database.execute("SELECT operation, blob FROM writes").fetchall())
        self.assertCountEqual(writes, [("insert", new_blob), ("delete", old_blob)])
        self.assertEqual(actual, affected.dependency_graph(self.repo))

    def test_fact_cache_name_includes_both_versions(self) -> None:
        self.assertEqual(affected.FACT_CACHE_NAME,
                         f"affected-module-facts-{affected.CACHE_FORMAT_VERSION}-{affected.EXTRACTOR_VERSION}.sqlite")

    def test_fact_cache_keeps_other_versions_in_the_same_folder(self) -> None:
        cache = self.cache_file()
        cache.parent.mkdir()
        old_format = affected.CACHE_FORMAT_VERSION - 1
        old = cache.with_name(f"affected-module-facts-{old_format}-{affected.EXTRACTOR_VERSION}.sqlite")
        facts = {"example": affected.extract_facts("VALUE = 1\n", "example.py")}
        self.assertTrue(affected.write_fact_cache(old, facts, None, set(facts)))
        with closing(sqlite3.connect(old)) as database, database:
            _ = database.execute("UPDATE cache_version SET format = ?", (old_format,))
        before = (old.read_bytes(), old.stat().st_mtime_ns)

        with mock.patch.object(affected, "read_fact_cache", wraps=affected.read_fact_cache) as read:
            _ = affected.dependency_graph(self.repo, state_dir=self.cache_dir())

        read.assert_called_once_with(cache)
        self.assertTrue(affected.read_fact_cache(cache))
        self.assertEqual((old.read_bytes(), old.stat().st_mtime_ns), before)

    def test_fact_cache_keeps_cached_base_blobs_and_prunes_unrelated_blobs(self) -> None:
        cached_path = "scripts/cached.py"
        missing_path = "scripts/missing.py"
        self.write(cached_path, "CACHED = 1\n")
        self.write(missing_path, "VALUE = 2\n")
        self.certify()
        cached_base = self.git("rev-parse", f"HEAD:{cached_path}")
        missing_base = self.git("rev-parse", f"HEAD:{missing_path}")
        self.write(missing_path, "VALUE = 3\n")
        _ = self.commit()
        unrelated = self.git("rev-parse", f"HEAD:{missing_path}")
        _ = affected.dependency_graph(self.repo, state_dir=self.cache_dir())
        self.write(cached_path, "VALUE = 4\n")
        self.write(missing_path, "VALUE = 5\n")
        _ = self.commit()

        with (mock.patch.object(ast, "parse", wraps=ast.parse) as parse,
              mock.patch.object(affected, "blob_texts", wraps=affected.blob_texts) as read,
              mock.patch.object(affected, "revision_files", wraps=affected.revision_files) as listing):
            result = affected.select_affected(self.repo, self.base, self.modules, set(), state_dir=self.cache_dir())

        self.assertIsInstance(result, affected.AffectedSelection)
        self.assertEqual([call.kwargs["filename"] for call in parse.call_args_list], [cached_path, missing_path])
        read.assert_not_called()
        listing.assert_called_once_with(self.repo, self.base.commit)
        facts = affected.read_fact_cache(self.cache_file())
        assert facts is not None
        self.assertIn(cached_base, facts)
        self.assertNotIn(missing_base, facts)
        self.assertNotIn(unrelated, facts)
        for path in (cached_path, missing_path):
            self.assertIn(self.git("rev-parse", f"HEAD:{path}"), facts)

    def test_fact_cache_keeps_current_facts_when_the_optional_base_listing_fails(self) -> None:
        subject = "scripts/subject.py"
        self.write(subject, "SUBJECT = 1\n")
        self.add_test("checks/test_reader.py", "from scripts import subject\n")
        self.certify()
        base_blob = self.git("rev-parse", f"HEAD:{subject}")
        with mock.patch.object(affected, "revision_files", side_effect=affected.GraphError("ls-tree failed")) as listing:
            cold = affected.select_affected(self.repo, self.base, self.modules, set(), state_dir=self.cache_dir())
            self.assertIsInstance(cold, affected.AffectedSelection)
            listing.assert_not_called()

            self.write(subject, "SUBJECT = 2\n")
            _ = self.commit()
            expected = self.selection()
            result = affected.select_affected(self.repo, self.base, self.modules, set(), state_dir=self.cache_dir())

        self.assertEqual(expected.modules, (self.repo / "scripts/checks/test_reader.py",))
        self.assertEqual(result, expected)
        listing.assert_called_once_with(self.repo, self.base.commit)
        facts = affected.read_fact_cache(self.cache_file())
        assert facts is not None
        self.assertIn(self.git("rev-parse", f"HEAD:{subject}"), facts)
        self.assertNotIn(base_blob, facts)

    def test_invalid_cache_rebuilds_without_changing_selection(self) -> None:
        self.write("scripts/subject.py", "VALUE = 1\n")
        self.add_test("checks/test_reader.py", "from scripts import subject\n")
        self.certify()
        self.write("scripts/subject.py", "VALUE = 2\n")
        _ = self.commit()
        expected = self.selection()
        self.assertEqual(expected.modules, (self.repo / "scripts/checks/test_reader.py",))
        cache = self.cache_file()
        legacy = cache.with_suffix(".json")
        legacy.parent.mkdir(parents=True)
        _ = legacy.write_text("legacy cache")
        for condition in ("cold", "warm", "file damage", "old format", "old extractor", "bad digest", "bad payload"):
            with self.subTest(condition=condition):
                if condition == "file damage":
                    _ = cache.write_bytes(b"broken database")
                if condition in {"old format", "old extractor", "bad digest", "bad payload"}:
                    with closing(sqlite3.connect(cache)) as database, database:
                        if condition == "old format":
                            _ = database.execute("UPDATE cache_version SET format = ?", (affected.CACHE_FORMAT_VERSION - 1,))
                        elif condition == "old extractor":
                            _ = database.execute("UPDATE cache_version SET extractor = -1")
                        elif condition == "bad digest":
                            _ = database.execute("UPDATE facts SET digest = X'00'")
                        else:
                            payload = b'{"imports": null, "tokens": []}'
                            blob = self.git("rev-parse", "HEAD:scripts/subject.py")
                            _ = database.execute("UPDATE facts SET payload = ?, digest = ? WHERE blob = ?",
                                                 (payload, affected.fact_digest(blob, payload), blob))
                with mock.patch.object(ast, "parse", wraps=ast.parse) as parse:
                    actual = affected.select_affected(self.repo, self.base, self.modules, set(), state_dir=self.cache_dir())
                self.assertEqual(parse.call_count, 0 if condition == "warm" else 3)
                self.assertEqual(actual, expected)
                self.assertIsNotNone(affected.read_fact_cache(cache))
                self.assertEqual(legacy.read_text(), "legacy cache")

    def test_busy_cache_reads_published_facts_without_writing(self) -> None:
        subject = "scripts/subject.py"
        self.write(subject, "VALUE = 1\n")
        self.add_test("checks/test_reader.py", "from scripts import subject\n")
        self.certify()
        _ = affected.dependency_graph(self.repo, state_dir=self.cache_dir())
        self.write(subject, "VALUE = 2\n")
        _ = self.commit()
        expected = self.selection()
        before = self.cache_file().read_bytes()
        graph_for_files = affected.graph_for_files

        def interleave(repo: Path, paths: set[str], blobs: dict[str, str], dirty: set[str],
                       cached: dict[str, affected.SourceFacts], *, worktree: bool,
                       run: affected.SelectionRun | None = None, processes: int = 1) -> affected.SourceGraph:
            with (mock.patch.object(affected, "graph_for_files", graph_for_files),
                  mock.patch.object(ast, "parse", wraps=ast.parse) as parse,
                  mock.patch.object(affected, "write_fact_cache", wraps=affected.write_fact_cache) as write,
                  mock.patch.object(fcntl, "flock", wraps=fcntl.flock) as lock):
                reader = affected.select_affected(self.repo, self.base, self.modules, set(), state_dir=self.cache_dir())
            self.assertEqual(reader, expected)
            assert isinstance(reader.run, affected.SelectionRun)
            self.assertIs(reader.run.lock, affected.FactCacheLock.BUSY_SNAPSHOT)
            self.assertEqual((reader.run.cache_hits, reader.run.cache_misses), (11, 1))
            self.assertEqual([call.kwargs["filename"] for call in parse.call_args_list], [subject])
            attempts = [call for call in lock.call_args_list if call.args[1] == fcntl.LOCK_EX | fcntl.LOCK_NB]
            self.assertEqual(len(attempts), 1)
            write.assert_not_called()
            self.assertEqual(self.cache_file().read_bytes(), before)
            return graph_for_files(repo, paths, blobs, dirty, cached, worktree=worktree, run=run, processes=processes)

        with mock.patch.object(affected, "graph_for_files", side_effect=interleave) as graph:
            writer = affected.select_affected(self.repo, self.base, self.modules, set(), state_dir=self.cache_dir())
        graph.assert_called_once()
        self.assertEqual(writer, expected)
        assert isinstance(writer.run, affected.SelectionRun)
        self.assertIs(writer.run.lock, affected.FactCacheLock.PUBLISHED)
        facts = affected.read_fact_cache(self.cache_file())
        self.assertIsNotNone(facts)
        assert facts is not None
        self.assertIn(self.git("rev-parse", f"HEAD:{subject}"), facts)

    def test_selection_run_counts_cold_and_warm_facts(self) -> None:
        self.add_test("checks/test_reader.py", "VALUE = 1\n")
        self.certify()
        self.write("scripts/checks/test_reader.py", "VALUE = 2\n")
        _ = self.commit()
        cold = affected.select_affected(self.repo, self.base, self.modules, set(), state_dir=self.cache_dir())
        warm = affected.select_affected(self.repo, self.base, self.modules, set(), state_dir=self.cache_dir())
        self.assertIsInstance(cold, affected.AffectedSelection)
        self.assertEqual(warm, cold)
        assert isinstance(cold.run, affected.SelectionRun) and isinstance(warm.run, affected.SelectionRun)
        self.assertEqual((cold.run.cache_hits, cold.run.cache_misses), (9, 2))
        self.assertEqual(cold.run.extracted_files, 2)
        self.assertIs(cold.run.lock, affected.FactCacheLock.PUBLISHED)
        self.assertEqual((warm.run.cache_hits, warm.run.cache_misses), (11, 0))
        self.assertIs(warm.run.lock, affected.FactCacheLock.TAKEN)
        self.assertEqual(warm.run.extracted_files, 0)
        self.assertEqual(warm.run.extraction_s, 0)
        for run in (cold.run, warm.run):
            self.assertGreaterEqual(run.cache_read_s, 0)
            self.assertGreaterEqual(run.graph_s, 0)
            self.assertGreaterEqual(run.other_s, 0)

    def test_missing_extraction_processes_requires_the_full_suite(self) -> None:
        self.write(".git/test-config/gate.conf", "# text:\nlanding_branches=main\n")
        result = affected.select_affected(self.repo, self.base, self.modules, set(), state_dir=self.cache_dir())
        assert isinstance(result, affected.FullSuite)
        self.assertIn("affected.extract.processes='<missing>'", result.reason)
        self.assertFalse(self.cache_file().exists())

    def add_extraction_sources(self, value: str) -> None:
        for index in range(24):
            self.write(f"scripts/component/source_{index}.py", f"VALUE = {value!r}, {index}\n")

    def test_extraction_balances_text_lengths_and_returns_facts_in_path_order(self) -> None:
        sources: list[tuple[str, str]] = []
        for index, length in enumerate((100, 80, 50, 90, 70, 60)):
            text = f"import module_{index}\n#"
            sources.append((f"scripts/source_{index}.py", text.ljust(length, "x")))

        slices = affected.extraction_slices(sources, processes=3)
        self.assertEqual([[index for index, _, _ in batch] for batch in slices], [[0, 2], [3, 5], [1, 4]])
        self.assertEqual([sum(len(text) for _, _, text in batch) for batch in slices], [150, 150, 150])

        expected = [(path, affected.extract_facts(text, path)) for path, text in sources]
        events: list[str] = []
        start = SpawnProcess.start
        send = Connection.send

        def record_start(worker: SpawnProcess) -> None:
            start(worker)
            events.append("start")

        def record_send(connection: Connection, batch: list[affected.IndexedSource]) -> None:
            send(connection, batch)
            events.append("send")

        with (mock.patch.object(SpawnProcess, "start", autospec=True, side_effect=record_start),
              mock.patch.object(Connection, "send", autospec=True, side_effect=record_send)):
            results = affected.extract_slices(sources, processes=3)
        self.assertEqual(events, ["start"] * 3 + ["send"] * 3)
        self.assertEqual(list(results.items()), expected)

    def test_pooled_extraction_preserves_selection_and_cached_facts(self) -> None:
        self.add_extraction_sources("base")
        self.write("scripts/component/removed.py", "REMOVED = True\n")
        self.write("scripts/component/dirty.py", "VALUE = 'committed'\n")
        self.add_test("checks/test_reader.py", "from scripts.component import source_1, removed\n")
        self.certify()
        self.add_extraction_sources("current")
        self.write("scripts/component/duplicate.py", "VALUE = 'current', 1\n")
        self.write("scripts/component/invalid.py", "invalid (\n")
        (self.repo / "scripts/component/removed.py").unlink()
        _ = self.commit()
        self.write("scripts/component/dirty.py", "VALUE = 'dirty'\n")
        dirty = affected.dirty_paths(self.repo)
        serial_state = self.cache_dir() / "serial"
        pool_state = self.cache_dir() / "pool"
        self.set_gate_config(processes=1)
        with mock.patch.object(affected, "extract_slices") as serial_pool:
            serial = affected.select_affected(self.repo, self.base, self.modules, dirty, state_dir=serial_state)
        serial_pool.assert_not_called()
        self.set_gate_config(processes=8)
        with mock.patch.object(affected, "extract_slices", wraps=affected.extract_slices) as pool:
            pooled = affected.select_affected(self.repo, self.base, self.modules, dirty, state_dir=pool_state)
        self.assertEqual(pool.call_count, 2)
        assert isinstance(serial, affected.AffectedSelection) and isinstance(pooled, affected.AffectedSelection)
        self.assertEqual(serial.modules, (self.repo / "scripts/checks/test_reader.py",))
        self.assertEqual(pooled, serial)
        serial_facts = affected.read_fact_cache(serial_state / affected.FACT_CACHE_NAME)
        pool_facts = affected.read_fact_cache(pool_state / affected.FACT_CACHE_NAME)
        self.assertTrue(serial_facts)
        self.assertEqual(pool_facts, serial_facts)
        assert isinstance(serial.run, affected.SelectionRun) and isinstance(pooled.run, affected.SelectionRun)
        self.assertEqual(pooled.run.cache_hits, serial.run.cache_hits)
        self.assertEqual(pooled.run.cache_misses, serial.run.cache_misses)
        self.assertEqual(pooled.run.extracted_files, serial.run.extracted_files)

    def test_pooled_extraction_skips_a_small_number_of_misses(self) -> None:
        self.add_extraction_sources("base")
        self.certify()
        _ = affected.dependency_graph(self.repo, state_dir=self.cache_dir())
        self.add_test("checks/test_new.py", "NEW = True\n")
        _ = self.commit()
        self.set_gate_config(processes=8)
        with mock.patch.object(affected, "extract_slices") as pool:
            result = affected.select_affected(self.repo, self.base, self.modules, set(), state_dir=self.cache_dir())
        pool.assert_not_called()
        assert isinstance(result, affected.AffectedSelection) and isinstance(result.run, affected.SelectionRun)
        self.assertEqual(result.modules, (self.repo / "scripts/checks/test_new.py",))
        self.assertEqual((result.run.cache_misses, result.run.extracted_files), (1, 1))

    def test_pooled_extraction_retries_serially_after_a_start_error(self) -> None:
        self.add_extraction_sources("base")
        self.certify()
        self.add_test("checks/test_new.py", "NEW = True\n")
        _ = self.commit()
        self.set_gate_config(processes=8)
        start_worker = SpawnProcess.start
        started: list[int] = []

        def fail_start(worker: SpawnProcess) -> None:
            if started:
                raise OSError("cannot start worker")
            start_worker(worker)
            assert worker.pid is not None
            started.append(worker.pid)

        with mock.patch.object(SpawnProcess, "start", autospec=True, side_effect=fail_start) as start:
            result = affected.select_affected(self.repo, self.base, self.modules, set(), state_dir=self.cache_dir())
        self.assertEqual(start.call_count, 2)
        with self.assertRaises(ChildProcessError):
            _ = os.waitpid(started[0], os.WNOHANG)
        assert isinstance(result, affected.AffectedSelection) and isinstance(result.run, affected.SelectionRun)
        self.assertEqual(result.modules, (self.repo / "scripts/checks/test_new.py",))
        self.assertEqual((result.run.cache_misses, result.run.extracted_files), (26, 26))
        self.assertIs(result.run.lock, affected.FactCacheLock.PUBLISHED)

    def test_pooled_extraction_worker_error_returns_a_graph_error(self) -> None:
        self.add_extraction_sources("base")
        self.certify()
        self.set_gate_config(processes=8)
        with mock.patch.object(affected, "extract_slice", fail_extraction_worker):
            result = affected.select_affected(self.repo, self.base, self.modules, set(), state_dir=self.cache_dir())
        assert isinstance(result, affected.FullSuite)
        self.assertIn("graph error: unsupported operand type", result.reason)
        self.assertFalse(self.cache_file().exists())

    def test_pooled_extraction_reaps_workers_on_interrupt(self) -> None:
        self.add_extraction_sources("base")
        self.certify()
        self.set_gate_config(processes=8)

        def terminate(number: int, _frame: object) -> None:
            raise SystemExit(128 + number)

        for signum, error in ((signal.SIGINT, KeyboardInterrupt), (signal.SIGTERM, SystemExit)):
            with self.subTest(signum=signum):
                before = {child.pid for child in active_children()}
                worker_pids: set[int] = set()

                def interrupt(receivers: Iterable[Connection]) -> list[Connection]:
                    for child in active_children():
                        if child.pid is not None and child.pid not in before:
                            worker_pids.add(child.pid)
                    receiver = next(iter(receivers))
                    self.assertTrue(receiver.poll(10))
                    self.assertIn(cast(int, receiver.recv()), worker_pids)
                    signal.raise_signal(signum)
                    raise AssertionError("No interrupt handler ran")

                previous = signal.signal(signal.SIGTERM, terminate)
                try:
                    with (mock.patch.object(affected, "extract_slice", block_extraction_worker),
                          mock.patch.object(affected, "wait", side_effect=interrupt),
                          self.assertRaises(error)):
                        _ = affected.select_affected(self.repo, self.base, self.modules, set(), state_dir=self.cache_dir())
                finally:
                    _ = signal.signal(signal.SIGTERM, previous)
                self.assertEqual(len(worker_pids), 8)
                for worker_pid in worker_pids:
                    with self.assertRaises(ChildProcessError):
                        _ = os.waitpid(worker_pid, os.WNOHANG)
                self.assertFalse(self.cache_file().exists())
                with self.cache_file().with_suffix(".lock").open("ab") as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_killed_worker_retries_only_its_slice_serially(self) -> None:
        self.add_extraction_sources("base")
        self.certify()
        self.add_test("checks/test_new.py", "NEW = True\n")
        _ = self.commit()
        with mock.patch.object(affected, "extract_facts", wraps=affected.extract_facts) as serial_extract:
            serial = affected.select_affected(self.repo, self.base, self.modules, set())
        assert isinstance(serial, affected.AffectedSelection) and isinstance(serial.run, affected.SelectionRun)
        serial_sources = [(cast(str, call.args[1]), cast(str, call.args[0])) for call in serial_extract.call_args_list]
        killed_slice = next(batch for batch in affected.extraction_slices(serial_sources, processes=8)
                            if any(path == "scripts/checks/test_new.py" for _, path, _ in batch))
        expected_retries = [serial_extract.call_args_list[index] for index, _, _ in sorted(killed_slice)]
        self.set_gate_config(processes=8)

        for partial_result in (False, True):
            target = partial(kill_extraction_worker_for_new_test, partial_result=partial_result)
            with (self.subTest(partial_result=partial_result),
                  mock.patch.object(affected, "extract_slice", target),
                  mock.patch.object(affected, "extract_facts", wraps=affected.extract_facts) as retry):
                pooled = affected.select_affected(self.repo, self.base, self.modules, set())
            self.assertEqual(pooled, serial)
            assert isinstance(pooled.run, affected.SelectionRun)
            self.assertEqual(pooled.run.cache_misses, serial.run.cache_misses)
            self.assertEqual(pooled.run.extracted_files, serial.run.extracted_files)
            self.assertEqual(retry.call_args_list, expected_retries)

    def test_pooled_extraction_retries_only_the_slice_with_a_send_error(self) -> None:
        sources = {f"scripts/source_{index}.py": f"import module_{index}\n#".ljust(length, "x")
                   for index, length in enumerate((100, 90, 60, 50))}
        expected = [affected.extract_facts(text, path) for path, text in sources.items()]
        send = Connection.send

        def fail_send(connection: Connection, batch: list[affected.IndexedSource]) -> None:
            if any(index == 0 for index, _, _ in batch):
                raise BrokenPipeError("worker closed the pipe")
            send(connection, batch)

        with (mock.patch.object(Connection, "send", autospec=True, side_effect=fail_send),
              mock.patch.object(affected, "extract_facts", wraps=affected.extract_facts) as retry):
            read_facts = affected.pooled_fact_reader(list(sources), sources.__getitem__, 2, None)
            results = [read_facts(path) for path in sources]
        self.assertEqual(results, expected)
        retry_paths = ("scripts/source_0.py", "scripts/source_3.py")
        self.assertEqual(retry.call_args_list, [mock.call(sources[path], path) for path in retry_paths])

    def test_pooled_source_read_error_matches_serial_without_rereading(self) -> None:
        self.add_extraction_sources("base")
        self.certify()
        broken = self.repo / "scripts/component/source_8.py"
        read_text = Path.read_text
        results: list[affected.FullSuite] = []
        for processes in (1, 8):
            attempts = 0

            def read(path: Path, encoding: str | None = None, errors: str | None = None) -> str:
                nonlocal attempts
                if path == broken:
                    attempts += 1
                    if attempts == 1:
                        raise OSError(f"cannot read {path}")
                return read_text(path, encoding=encoding, errors=errors)

            self.set_gate_config(processes=processes)
            with mock.patch.object(Path, "read_text", autospec=True, side_effect=read):
                selection = affected.select_affected(self.repo, self.base, self.modules, set())
            self.assertEqual(attempts, 1)
            assert isinstance(selection, affected.FullSuite)
            results.append(selection)
        serial, pooled = results
        self.assertEqual(pooled, serial)
        assert isinstance(serial.run, affected.SelectionRun) and isinstance(pooled.run, affected.SelectionRun)
        self.assertEqual(pooled.run.cache_misses, serial.run.cache_misses)
        self.assertEqual(pooled.run.extracted_files, serial.run.extracted_files)

    def test_pooled_invalid_test_counts_only_the_sources_the_graph_uses(self) -> None:
        self.add_extraction_sources("base")
        self.add_test("a/test_invalid.py", "invalid (\n")
        self.write("scripts/a/z_overflow.py", "from pathlib import Path\nROOT = Path(__file__).parents[10_000_000_000_000_000_000]\n")
        self.certify()
        results: list[affected.FullSuite] = []

        for processes in (1, 8):
            self.set_gate_config(processes=processes)
            selection = affected.select_affected(self.repo, self.base, self.modules, set(), state_dir=self.cache_dir())
            assert isinstance(selection, affected.FullSuite) and isinstance(selection.run, affected.SelectionRun)
            results.append(selection)
            self.assertIn("scripts/a/test_invalid.py", selection.reason)
            self.assertEqual((selection.run.cache_misses, selection.run.extracted_files), (1, 1))
            self.assertFalse(self.cache_file().exists())
        serial, pooled = results
        self.assertEqual(pooled, serial)

        self.write("scripts/a/test_invalid.py", "VALUE = True\n")
        for processes in (1, 8):
            with self.subTest(processes=processes):
                self.set_gate_config(processes=processes)
                with self.assertRaises(OverflowError):
                    _ = affected.select_affected(self.repo, self.base, self.modules, {"scripts/a/test_invalid.py"},
                                                 state_dir=self.cache_dir())

    def test_invalid_source_reads_count_misses_before_extraction(self) -> None:
        invalid = self.repo / "scripts/broken.py"
        _ = invalid.write_bytes(b"\xff")
        self.certify()
        current = affected.select_affected(self.repo, self.base, self.modules, set(), state_dir=self.cache_dir())
        invalid.unlink()
        _ = self.commit()
        historical = affected.select_affected(self.repo, self.base, self.modules, set(), state_dir=self.cache_dir())
        for result, extracted in ((current, 0), (historical, 1)):
            assert isinstance(result, affected.FullSuite)
            self.assertIn("utf-8", result.reason)
            assert isinstance(result.run, affected.SelectionRun)
            self.assertEqual(result.run.extracted_files, extracted)
            self.assertEqual(result.run.cache_misses, extracted + 1)

    def test_source_reads_in_both_graphs_count_as_extraction_time(self) -> None:
        current = "scripts/current.py"
        historical = "scripts/historical.py"
        self.write(historical, "VALUE = 2\n")
        self.certify()
        (self.repo / historical).unlink()
        self.write(current, "VALUE = 3\n")
        _ = self.commit()
        now = 0.0
        read_text = Path.read_text
        blob_texts = affected.blob_texts

        def clock() -> float:
            return now

        def read_current(path: Path, encoding: str | None = None, errors: str | None = None) -> str:
            nonlocal now
            if path == self.repo / current:
                now += 20.0
            return read_text(path, encoding=encoding, errors=errors)

        def read_historical(repo: Path, blobs: set[str]) -> dict[str, str]:
            nonlocal now
            now += 40.0
            return blob_texts(repo, blobs)

        with (mock.patch.object(affected, "monotonic", side_effect=clock),
              mock.patch.object(Path, "read_text", autospec=True, side_effect=read_current),
              mock.patch.object(affected, "blob_texts", side_effect=read_historical)):
            result = affected.select_affected(self.repo, self.base, self.modules, set())

        assert isinstance(result, affected.AffectedSelection)
        assert isinstance(result.run, affected.SelectionRun)
        self.assertEqual(result.run.extraction_s, 60.0)
        self.assertEqual(result.run.graph_s, 0.0)
        self.assertEqual(result.run.total_s, 60.0)

    def test_cache_cleanup_error_after_replacement_keeps_publication_result(self) -> None:
        run = affected.SelectionRun(lock=affected.FactCacheLock.TAKEN)
        cache_path = self.cache_file()
        cache_path.parent.mkdir()
        facts = {"example": affected.extract_facts("VALUE = 1\n", "example.py")}
        cache = affected.FactCache(facts, cache_path, run=run)

        class FailingCleanup(tempfile.TemporaryDirectory[str]):
            @override
            def cleanup(self) -> None:
                super().cleanup()
                raise OSError("cleanup failed")

        with mock.patch.object(tempfile, "TemporaryDirectory", FailingCleanup):
            cache.retain(set(facts))
        self.assertIs(run.lock, affected.FactCacheLock.PUBLISHED)
        self.assertEqual(affected.read_fact_cache(cache_path), facts)

    def test_deleted_blob_is_pruned_and_new_targets_resolve_cached_facts(self) -> None:
        self.write("scripts/component/test_caller.py", "import test_future_subject\n")
        _ = self.commit()
        _ = affected.dependency_graph(self.repo, state_dir=self.cache_dir())
        self.write("scripts/component/test_future_subject.py", "VALUE = 42\n")
        _ = self.commit()
        blob = self.git("rev-parse", "HEAD:scripts/component/test_future_subject.py")
        with mock.patch.object(ast, "parse", wraps=ast.parse) as parse:
            graph, _ = affected.dependency_graph(self.repo, state_dir=self.cache_dir())
        self.assertEqual([call.kwargs["filename"] for call in parse.call_args_list],
                         ["scripts/component/test_future_subject.py"])
        self.assertEqual(graph["scripts/component/test_caller.py"],
                         {"scripts/component/test_future_subject.py"})
        (self.repo / "scripts/component/test_future_subject.py").unlink()
        _ = self.commit()
        with mock.patch.object(ast, "parse", side_effect=AssertionError("unchanged source parsed")):
            graph, _ = affected.dependency_graph(self.repo, state_dir=self.cache_dir())
        self.assertEqual(graph["scripts/component/test_caller.py"], set())
        facts = affected.read_fact_cache(self.cache_file())
        self.assertIsNotNone(facts)
        assert facts is not None
        self.assertNotIn(blob, facts)

    def test_shared_blob_resolves_relative_imports_from_each_current_path(self) -> None:
        for component in ("first", "second"):
            self.write(f"scripts/{component}/test_adapter.py", "from . import subject\n")
            self.write(f"scripts/{component}/subject.py", "VALUE = 42\n")
        _ = self.commit()
        expected = affected.dependency_graph(self.repo)
        self.assertEqual(affected.dependency_graph(self.repo, state_dir=self.cache_dir()), expected)
        self.assertEqual(affected.dependency_graph(self.repo, state_dir=self.cache_dir()), expected)
        self.assertEqual(expected[0]["scripts/first/test_adapter.py"], {"scripts/first/subject.py"})
        self.assertEqual(expected[0]["scripts/second/test_adapter.py"], {"scripts/second/subject.py"})

    def test_unstaged_sources_and_symlinks_do_not_reuse_index_blob_facts(self) -> None:
        self.write("scripts/component/subject.py", "VALUE = 42\n")
        self.write("scripts/component/test_adapter.py", "VALUE = 43\n")
        self.write("scripts/component/target.txt", "VALUE = 44\n")
        (self.repo / "scripts/component/test_link.py").symlink_to("target.txt")
        _ = self.commit()
        _ = affected.dependency_graph(self.repo, state_dir=self.cache_dir())
        self.write("scripts/component/test_adapter.py", "import scripts.component.subject\n")
        self.write("scripts/component/target.txt", "import scripts.component.subject\n")
        actual = affected.dependency_graph(self.repo, state_dir=self.cache_dir())
        self.assertEqual(actual, affected.dependency_graph(self.repo))
        self.assertEqual(actual[0]["scripts/component/test_adapter.py"], {"scripts/component/subject.py"})
        self.assertEqual(actual[0]["scripts/component/test_link.py"], {"scripts/component/subject.py"})

    def test_bare_imports_select_each_matching_source_without_transitive_selection(self) -> None:
        self.write("scripts/component/subject.py", "VALUE = 1\n")
        self.write("scripts/duplicate/subject.py", "VALUE = 1\n")
        self.write("scripts/component/adapter.py", "from scripts.component import subject\n")
        self.add_test("checks/test_qualified.py", "import scripts.component.adapter\n")
        self.add_test("checks/test_bare.py", "from subject import VALUE\n")
        self.add_test("checks/test_mention.py", 'SOURCE = "../component/subject.py"\n')
        self.certify()
        self.write("scripts/component/subject.py", "VALUE = 2\n")
        _ = self.commit()
        result = self.selection()
        self.assertCountEqual([path.name for path in result.modules],
                              ["test_bare.py"])
        self.assertEqual(result.changed_files, ("scripts/component/subject.py",))
        # Bare imports conservatively cover every same-named source.
        self.certify()
        self.write("scripts/duplicate/subject.py", "VALUE = 3\n")
        _ = self.commit()
        self.assertCountEqual([path.name for path in self.selection().modules],
                              ["test_bare.py"])

    def test_imported_test_module_selects_its_importer_without_requesting_full_suite(self) -> None:
        self.add_test("agents/test_native_worker.py", "VALUE = 1\n")
        self.add_test("direct/test_workers.py", "from scripts.agents.test_native_worker import VALUE\n")
        self.certify()
        self.write("scripts/agents/test_native_worker.py", "VALUE = 2\n")
        _ = self.commit()
        self.assertCountEqual([path.name for path in self.selection().modules],
                              ["test_native_worker.py", "test_workers.py"])

    def test_package_initializers_and_relative_imports_are_dependencies(self) -> None:
        self.write("scripts/component/__init__.py", "VALUE = 1\n")
        self.write("scripts/component/subject.py", "VALUE = 1\n")
        self.write("scripts/component/adapter.py", "from . import subject\n")
        self.add_test("checks/test_package.py", "from scripts.component.adapter import subject\n")
        self.certify()
        self.write("scripts/component/__init__.py", "VALUE = 2\n")
        _ = self.commit()
        self.assertEqual([path.name for path in self.selection().modules], ["test_package.py"])

    def test_configuration_names_without_complete_paths_select_their_readers(self) -> None:
        self.write("config/limits.conf", "amount=1\n")
        self.write("scripts/component/reader.py", 'import conf\nVALUE = conf.read("limits").whole("amount")\n')
        self.add_test("checks/test_limits.py", "import reader\n")
        self.certify()
        self.record_configs({"test_limits.py": ["limits"]})
        self.write("config/limits.conf", "amount=2\n")
        _ = self.commit()
        result = self.selection()
        self.assertEqual(result.modules, (self.repo / "scripts/checks/test_limits.py",))

    def test_configuration_changes_select_recorded_readers(self) -> None:
        readers = (
            ("module", 'from scripts.lib import conf\nVALUE = conf.read("limits")\n'),
            ("module alias", 'from scripts.lib import conf as settings\nVALUE = settings.read("limits")\n'),
            ("qualified alias", 'import scripts.lib.conf as settings\nVALUE = settings.read("limits")\n'),
            ("qualified module", 'import scripts.lib.conf\nVALUE = scripts.lib.conf.read("limits")\n'),
            ("function", 'from scripts.lib.conf import read\nVALUE = read("limits")\n'),
            ("function alias", 'from scripts.lib.conf import read as load\nVALUE = load("settings/limits.conf")\n'),
            ("path token", 'SETTINGS = "settings/limits.conf"\n'),
            ("relative cycle", 'from . import adapter\nfrom scripts.lib import conf\nVALUE = conf.read("limits")\n'),
        )
        self.write("scripts/component/adapter.py", "from . import reader\n")
        self.add_test("checks/test_limits.py", "from scripts.component import adapter\n")
        for name, reader in readers:
            with self.subTest(reader=name):
                self.write("config/limits.conf", "amount=1\n")
                self.write("config/unrelated.conf", "amount=1\n")
                self.write("scripts/component/reader.py", reader)
                self.certify()
                self.record_configs({"test_limits.py": ["limits"]})
                self.write("config/limits.conf", "amount=2\n")
                _ = self.commit()
                self.assertEqual(self.selection().modules, (self.repo / "scripts/checks/test_limits.py",))
                self.certify()
                self.record_configs({"test_limits.py": ["limits"]})
                self.write("config/unrelated.conf", "amount=2\n")
                _ = self.commit()
                self.assertEqual(self.selection().modules, ())

        fixtures = (
            'from pathlib import Path\nVALUE = (Path(__file__).parents[2] / "config/limits.conf").read_text()\n',
            'from scripts.lib import conf\nVALUE = conf.read("limits")\n',
            'from pathlib import Path\nimport shutil\nshutil.copytree(Path(__file__).parents[2] / "config", "fixture")\n',
            'from pathlib import Path\nNAME = "limits"\nVALUE = (Path(__file__).parents[2] / "config" / f"{NAME}.conf").read_text()\n',
        )
        for fixture in fixtures:
            with self.subTest(fixture=fixture):
                self.write("config/limits.conf", "amount=1\n")
                self.write("scripts/component/reader_test_support.py", fixture)
                self.write("scripts/component/test_fixture.py", "from scripts.component import reader_test_support\n")
                self.write("scripts/checks/test_limits.py", "from scripts.component import test_fixture\n")
                self.certify()
                self.record_configs({})
                self.write("config/limits.conf", "amount=2\n")
                _ = self.commit()
                self.assertEqual(self.selection().modules, (self.repo / "scripts/checks/test_limits.py",))

        self.write("config/.conf", "amount=1\n")
        self.write("scripts/checks/test_limits.py", 'NAME = ".conf"\n')
        self.certify()
        self.write("config/.conf", "amount=2\n")
        _ = self.commit()
        self.assertEqual(self.selection().modules, (self.repo / "scripts/checks/test_limits.py",))

    def test_configuration_selection_reuses_all_source_facts(self) -> None:
        self.write("config/limits.conf", "amount=1\n")
        self.write("scripts/component/reader.py", 'from scripts.lib import conf\nVALUE = conf.read("limits")\n')
        self.write("scripts/component/adapter.py", "import reader\n")
        self.add_test("checks/test_limits.py", "from scripts.component import adapter\n")
        self.certify()
        self.record_configs({"test_limits.py": ["limits"]})
        _ = affected.select_affected(self.repo, self.base, self.modules, set(), state_dir=self.cache_dir())
        self.write("config/limits.conf", "amount=2\n")
        _ = self.commit()
        with mock.patch.object(ast, "parse", wraps=ast.parse) as parse:
            result = affected.select_affected(self.repo, self.base, self.modules, set(), state_dir=self.cache_dir())
        self.assertIsInstance(result, affected.AffectedSelection)
        assert isinstance(result, affected.AffectedSelection)
        self.assertEqual(result.modules, (self.repo / "scripts/checks/test_limits.py",))
        self.assertEqual(parse.call_count, 0)
        self.write("scripts/component/reader.py", 'from scripts.lib import conf\nVALUE = conf.read("limits").whole("amount")\n')
        _ = self.commit()
        with mock.patch.object(ast, "parse", wraps=ast.parse) as parse:
            changed = affected.select_affected(self.repo, self.base, self.modules, set(), state_dir=self.cache_dir())
        self.assertEqual(changed, affected.AffectedSelection(self.base,
            ("config/limits.conf", "scripts/component/reader.py"), result.modules))
        self.assertEqual([call.kwargs["filename"] for call in parse.call_args_list],
                         ["scripts/component/reader.py"])

    def test_unreferenced_inputs_select_only_tests_in_a_changed_python_folder(self) -> None:
        paths = ["scripts/lib/new.py", "scripts/other/conftest.py", "settings.json",
                 "scripts/production/gate.py", "scripts/production/affected_modules.py",
                 "scripts/agents/models_config.sh"]
        for path in paths:
            with self.subTest(path=path):
                self.write(path, "# changed\n")
                _ = self.commit()
                self.assertEqual(self.selection().modules, ())
                self.certify()

    def test_deleted_and_renamed_python_sources_select_direct_importers(self) -> None:
        self.write("scripts/component/deleted.py", "VALUE = 1\n")
        self.write("scripts/component/renamed.py", "VALUE = 1\n")
        self.add_test("checks/test_deleted.py", "import scripts.component.deleted\n")
        self.add_test("checks/test_old_name.py", "import scripts.component.renamed\n")
        self.add_test("checks/test_new_name.py", "import scripts.component.new\n")
        self.certify()
        (self.repo / "scripts/component/deleted.py").unlink()
        _ = self.commit()
        self.assertEqual([path.name for path in self.selection().modules], ["test_deleted.py"])
        self.certify()
        _ = self.git("mv", "scripts/component/renamed.py", "scripts/component/new.py")
        _ = self.commit()
        self.assertCountEqual([path.name for path in self.selection().modules],
                              ["test_old_name.py", "test_new_name.py"])

    def test_unreferenced_shell_files_and_outside_files_select_no_modules(self) -> None:
        self.add_test("component/test_orphan.py", "VALUE = 1\n")
        self.certify()
        self.write("scripts/component/new.sh", "true\n")
        _ = self.commit()
        self.assertEqual(self.selection().modules, ())
        self.certify()
        self.write("notes/unrelated.md", "new notes\n")
        _ = self.commit()
        self.assertEqual(self.selection().modules, ())

    def test_a_changed_test_selects_itself(self) -> None:
        self.add_test("checks/test_changed.py", "VALUE = 1\n")
        self.certify()
        self.write("scripts/checks/test_changed.py", "VALUE = 2\n")
        _ = self.commit()
        result = self.selection()
        self.assertEqual([path.name for path in result.modules], ["test_changed.py"])

    def test_selections_at_and_above_forty_percent_stay_affected(self) -> None:
        self.write("scripts/component/subject.py", "VALUE = 1\n")
        # Four of the ten original modules depend on this module.
        for index in range(4):
            self.write(f"scripts/other/test_unrelated_{index}.py", "import scripts.component.subject\n")
        self.certify()
        self.write("scripts/component/subject.py", "VALUE = 2\n")
        _ = self.commit()
        self.assertEqual(len(self.selection().modules), 4)
        self.write("scripts/other/test_unrelated_4.py", "import scripts.component.subject\n")
        _ = self.commit()
        self.assertEqual(len(self.selection().modules), 5)

    def test_dirty_paths_allow_selection_but_no_base_and_invalid_tests_require_full_runs(self) -> None:
        self.write("untracked.txt", "pending\n")
        self.assertEqual(self.selection().modules, ())
        _ = self.commit()
        self.assertEqual(affected.select_affected(self.repo, affected.NoCertifiedBase(), self.modules, affected.dirty_paths(self.repo)),
                         affected.FullSuite("no certified base"))
        self.write("scripts/component/invalid.py", "this is invalid (\n")
        self.write("scripts/component/test_broken.py", "this is invalid (\n")
        _ = self.commit()
        result = affected.select_affected(self.repo, self.base, self.modules, affected.dirty_paths(self.repo))
        self.assertIsInstance(result, affected.FullSuite)
        assert isinstance(result, affected.FullSuite)
        self.assertIn("graph error:", result.reason)
        self.assertIn("broken.py", result.reason)

    def test_direct_imports_and_same_folder_select_without_a_second_import_step(self) -> None:
        self.write("scripts/component/subject.py", "VALUE = 1\n")
        self.write("scripts/adapters/adapter.py", "import scripts.component.subject\n")
        self.add_test("checks/test_direct.py", "import scripts.component.subject\n")
        self.add_test("checks/test_indirect.py", "import scripts.adapters.adapter\n")
        self.add_test("component/test_neighbor.py", "VALUE = 1\n")
        self.add_test("component/test_named.py", "SOURCE = 'subject.py'\n")
        self.add_test("component/test_plain.py", "import subject\n")
        self.add_test("component/test_comment.py", "# subject\n")
        self.add_test("component/test_fragment.py", "SOURCE = 'subjects my_subject subject_helper'\n")
        self.add_test("checks/test_named.py", "SOURCE = 'subject.py'\n")
        self.certify()
        self.write("scripts/component/subject.py", "VALUE = 2\n")
        _ = self.commit()
        for removed in (False, True):
            with self.subTest(removed=removed):
                if removed:
                    (self.repo / "scripts/component/subject.py").unlink()
                    _ = self.commit()
                self.assertCountEqual(self.selection().modules,
                                      [self.repo / "scripts" / path for path in
                                       ("checks/test_direct.py", "component/test_named.py",
                                        "component/test_plain.py", "component/test_comment.py")])

    def test_complete_path_tokens_select_only_the_test_that_names_the_path(self) -> None:
        self.write("config/custom.json", "{}\n")
        self.write("verify.sh", "true\n")
        self.write("scripts/adapters/adapter.py", "# config/custom.json\n")
        self.add_test("checks/test_path.py", "# ./config/custom.json\n")
        self.add_test("checks/test_root_path.py", "SCRIPT = 'verify.sh'\n")
        self.add_test("checks/test_fragment.py", "PATHS = 'custom.json old/config/custom.json config/custom.json.bak'\n")
        self.add_test("checks/test_indirect_path.py", "import scripts.adapters.adapter\n")
        self.certify()
        self.write("config/custom.json", '{"changed": true}\n')
        self.write("verify.sh", "false\n")
        _ = self.commit()
        self.assertCountEqual([path.name for path in self.selection().modules],
                              ["test_path.py", "test_root_path.py"])

        self.certify()
        self.write("scripts/component/tool.sh", "true\n")
        self.write("scripts/component/reader_test_support.py",
                   'from pathlib import Path\nTOOL = Path(__file__).with_name("tool.sh")\n'
                   + 'from scripts.component import relay_test_support\n')
        self.write("scripts/component/relay_test_support.py",
                   "from scripts.component import reader_test_support\n")
        self.write("scripts/component/test_fixture.py", "from scripts.component import relay_test_support\n")
        self.write("scripts/adapters/path_adapter.py",
                   "from scripts.component import reader_test_support\n")
        for path, text in (
            ("component/test_support_path.py", "from scripts.component import relay_test_support\n"),
            ("checks/test_folder_path.py", "PARTS = 'component tool.sh'\n"),
            ("checks/test_basename.py", "NAME = 'tool.sh'\n"),
            ("checks/test_inherited_fixture.py", "from scripts.component import test_fixture\n"),
            ("checks/test_support_through_production.py", "from scripts.adapters import path_adapter\n"),
        ):
            self.add_test(path, text)
        self.certify()
        self.write("scripts/component/tool.sh", "false\n")
        _ = self.commit()
        self.assertCountEqual([path.name for path in self.selection().modules],
                              ["test_support_path.py", "test_folder_path.py", "test_inherited_fixture.py"])

    def test_a_changed_script_selects_tests_of_every_script_that_calls_it(self) -> None:
        self.write("scripts/component/tool.sh", "# runner.sh calls this file.\ntrue\n")
        self.write("scripts/component/runner.sh", 'source "$SCRIPT_DIR/tool.sh"\n')
        self.write("scripts/entry/front.sh", 'exec "$HOME/.claude/scripts/component/runner.sh"\n')
        self.write("scripts/entry/launch", 'exec "$SCRIPT_DIR/front.sh"\n')
        (self.repo / "scripts/entry/launch").chmod(0o755)
        self.write("scripts/other/elsewhere.sh", 'source "$SCRIPT_DIR/tool.sh"\n')
        self.write("scripts/other/fragment.sh", "cat old/component/tool.sh\n")
        self.write("scripts/component/helper.py", 'COMMAND = ["scripts/component/tool.sh"]\n')
        for name, text in (
            ("test_tool.py", "SCRIPT = 'scripts/component/tool.sh'\n"),
            ("test_runner.py", "PARTS = 'component runner.sh'\n"),
            ("test_front.py", "SCRIPT = 'scripts/entry/front.sh'\n"),
            ("test_launch.py", "SCRIPT = 'scripts/entry/launch'\n"),
            ("test_elsewhere.py", "SCRIPT = 'scripts/other/elsewhere.sh'\n"),
            ("test_fragment.py", "SCRIPT = 'scripts/other/fragment.sh'\n"),
            ("test_helper.py", "SCRIPT = 'scripts/component/helper.py'\n"),
            ("test_relative_front.py", "SCRIPT = 'SCRIPT_DIR/front.sh'\n"),
        ):
            self.add_test(f"checks/{name}", text)
        self.certify()
        expected = ["test_tool.py", "test_runner.py", "test_front.py", "test_launch.py"]
        for removed in (False, True):
            with self.subTest(removed=removed):
                if removed:
                    (self.repo / "scripts/component/tool.sh").unlink()
                else:
                    self.write("scripts/component/tool.sh", "# runner.sh calls this file.\nfalse\n")
                _ = self.commit()
                self.assertCountEqual([path.name for path in self.selection().modules], expected)
                self.certify()

    def test_dirty_paths_include_edits_untracked_files_deletions_and_renames(self) -> None:
        self.write("scripts/component/subject.py", "VALUE = 1\n")
        self.write("scripts/component/deleted.py", "VALUE = 1\n")
        self.write("scripts/component/old.py", "VALUE = 1\n")
        self.add_test("checks/test_dirty.py", "import scripts.component.subject\n")
        self.add_test("checks/test_new.py", "import scripts.component.new\n")
        self.add_test("checks/test_deleted.py", "import scripts.component.deleted\n")
        self.add_test("checks/test_old.py", "import scripts.component.old\n")
        self.add_test("checks/test_renamed.py", "import scripts.component.renamed\n")
        self.certify()
        self.write("scripts/component/subject.py", "VALUE = 2\n")
        self.write("scripts/component/new.py", "VALUE = 3\n")
        (self.repo / "scripts/component/deleted.py").unlink()
        _ = self.git("mv", "scripts/component/old.py", "scripts/component/renamed.py")
        result = self.selection()
        self.assertCountEqual([path.name for path in result.modules],
                              ["test_dirty.py", "test_new.py", "test_deleted.py", "test_old.py", "test_renamed.py"])
        self.assertEqual(result.changed_files,
                         ("scripts/component/deleted.py", "scripts/component/new.py", "scripts/component/old.py",
                          "scripts/component/renamed.py", "scripts/component/subject.py"))

    def test_deletion_uses_base_imports_without_a_base_scan_graph(self) -> None:
        self.write("scripts/component/deleted.py", "VALUE = 1\n")
        self.add_test("checks/test_reader.py", "import scripts.component.deleted\n")
        self.certify()
        (self.repo / "scripts/component/deleted.py").unlink()
        _ = self.commit()
        with mock.patch.object(affected, "repository_scan_graph", wraps=affected.repository_scan_graph) as scans:
            self.assertEqual(self.selection().modules, (self.repo / "scripts/checks/test_reader.py",))
        scans.assert_called_once()

        graph_for_files = affected.graph_for_files

        def without_base_imports(repo: Path, paths: set[str], blobs: dict[str, str], dirty: set[str],
                                 cached: dict[str, affected.SourceFacts], *, worktree: bool,
                                 run: affected.SelectionRun | None = None, processes: int = 1) -> affected.SourceGraph:
            graph = graph_for_files(repo, paths, blobs, dirty, cached, worktree=worktree, run=run, processes=processes)
            return graph if worktree else replace(graph, imports={})

        with mock.patch.object(affected, "graph_for_files", side_effect=without_base_imports):
            self.assertEqual(self.selection().modules, ())

    def test_unresolved_absolute_scan_paths_require_the_full_suite(self) -> None:
        source = "scripts/component/subject.py"
        invalid_path = str(self.repo / "bad") + "\0"
        for historical in (False, True):
            with self.subTest(historical=historical):
                self.write(source, "from pathlib import Path\n"
                           + (f"FILES = Path({invalid_path!r}).glob('*.txt')\n" if historical
                              else f"ignored(Path({invalid_path!r}))\n"))
                if historical:
                    self.certify()
                    (self.repo / source).unlink()
                _ = self.commit()
                result = affected.select_affected(self.repo, self.base, self.modules, set())
                self.assertIsInstance(result, affected.FullSuite)
                assert isinstance(result, affected.FullSuite)
                self.assertIn("embedded null", result.reason)

    def test_deep_scan_call_chain_preserves_the_full_suite_fallback(self) -> None:
        source = "scripts/component/subject.py"
        invalid_path = str(self.repo / "bad") + "\0"
        chain = "".join(f"def hop_{index}():\n    hop_{index + 1}()\n" for index in range(983))
        self.write(source, "from pathlib import Path\n" + chain
                   + f"def hop_983():\n    ignored(Path({invalid_path!r}))\n")
        _ = self.commit()
        for historical in (False, True):
            with self.subTest(historical=historical):
                if historical:
                    self.certify()
                    (self.repo / source).unlink()
                    _ = self.commit()
                result = affected.select_affected(self.repo, self.base, self.modules, set())
                self.assertIsInstance(result, affected.FullSuite)
                assert isinstance(result, affected.FullSuite)
                self.assertIn("embedded null", result.reason)

    def test_deep_scan_call_chain_resolves_the_leaf_scan(self) -> None:
        self.write("assets/subject.txt", "before\n")
        chain = "".join(f"def hop_{index}(root):\n    hop_{index + 1}(root)\n" for index in range(983))
        self.write("scripts/component/subject.py", chain
                   + "def hop_983(root):\n    return list((root / 'assets').glob('*.txt'))\n")
        self.add_test("checks/test_reader.py", "from pathlib import Path\n"
                      + "from scripts.component.subject import hop_0\n"
                      + "hop_0(Path(__file__).resolve().parents[2])\n")
        self.certify()
        self.write("assets/subject.txt", "after\n")
        _ = self.commit()
        self.assertEqual(self.selection().modules, (self.repo / "scripts/checks/test_reader.py",))

    def test_deep_imported_binding_chain_preserves_the_full_suite_fallback(self) -> None:
        source = "scripts/component/subject.py"
        invalid_path = str(self.repo / "bad") + "\0"
        for index in range(982):
            self.write(f"scripts/component/hop_{index}.py",
                       f"from scripts.component.hop_{index + 1} import ROOT as NEXT\n" + "ROOT = NEXT\n")
        self.write("scripts/component/hop_982.py", "from pathlib import Path\n" + f"ROOT = Path({invalid_path!r})\n")
        self.write(source, "from scripts.component.hop_0 import ROOT\n" + "FILES = ROOT.glob('*.txt')\n")
        _ = self.commit()
        for historical in (False, True):
            with self.subTest(historical=historical):
                if historical:
                    self.certify()
                    (self.repo / source).unlink()
                    _ = self.commit()
                result = affected.select_affected(self.repo, self.base, self.modules, set())
                self.assertIsInstance(result, affected.FullSuite)
                assert isinstance(result, affected.FullSuite)
                self.assertIn("embedded null", result.reason)

    def test_imported_binding_cycle_preserves_candidate_order(self) -> None:
        reader = "scripts/component/subject.py"
        sources = {
            reader: "from scripts.component.roots import ROOT\n" + "FILES = ROOT.glob('*.txt')\n",
            "scripts/component/roots.py": "from scripts.component.roots import ROOT as NEXT\n"
            + "ROOT = NEXT / 'nested'\n",
            "scripts/component/roots/__init__.py": "from pathlib import Path\n"
            + "ROOT = Path(__file__).parents[3] / 'assets'\n",
        }
        facts = {path: affected.extract_facts(text, path) for path, text in sources.items()}
        scans = affected.repository_scan_graph(self.repo, facts, {})
        self.assertEqual(scans[reader], frozenset({affected.RepositoryScan("assets/nested", "*.txt", False)}))

    def test_cached_duplicate_scope_names_keep_every_scan_root(self) -> None:
        self.write("assets/subject.md", "before\n")
        self.add_test("checks/test_reader.py", "from pathlib import Path\n"
                      + "ROOT = Path(__file__).resolve().parents[2]\n"
                      + "FILES = list((ROOT / 'assets').glob('*.md'))\n")
        self.certify()
        self.write("assets/subject.md", "after\n")
        _ = self.commit()
        expected = self.selection()
        self.assertEqual(expected.modules, (self.repo / "scripts/checks/test_reader.py",))
        _ = affected.dependency_graph(self.repo, state_dir=self.cache_dir())
        cache_path = self.cache_file()
        cache = affected.read_fact_cache(cache_path)
        assert cache is not None
        blob = self.git("rev-parse", "HEAD:scripts/checks/test_reader.py")
        facts = cache[blob]
        cache[blob] = replace(facts, scan_scopes=(*facts.scan_scopes, affected.ScanScope("", (), (), (), ())))
        self.assertTrue(affected.write_fact_cache(cache_path, cache, None, set(cache)))
        with mock.patch.object(ast, "parse", side_effect=AssertionError("warm cache parsed source")):
            result = affected.select_affected(self.repo, self.base, self.modules, set(), state_dir=self.cache_dir())
        self.assertEqual(result, expected)

    def test_invalid_historical_test_still_requires_a_full_suite(self) -> None:
        self.add_test("checks/test_deleted.py", "broken (\n")
        self.certify()
        deleted = self.repo / "scripts/checks/test_deleted.py"
        deleted.unlink()
        self.modules.remove(deleted)
        _ = self.commit()
        result = affected.select_affected(self.repo, self.base, self.modules, set())
        self.assertIsInstance(result, affected.FullSuite)
        assert isinstance(result, affected.FullSuite)
        self.assertIn("scripts/checks/test_deleted.py", result.reason)
        assert isinstance(result.run, affected.SelectionRun)
        self.assertGreater(result.run.cache_misses, 0)
        self.assertGreaterEqual(result.run.graph_s, 0)

    def test_optimized_selection_matches_baseline(self) -> None:
        self.write("scripts/component/deleted.py", "VALUE = 1\n")
        self.write("scripts/shared.py", "VALUE = 1\n")
        self.add_test("checks/test_scans.py", "from pathlib import Path\n"
                      + "def scan(root):\n    return (root / 'assets').rglob('*.txt')\n"
                      + "FILES = scan(Path(__file__).parents[2])\n")
        self.add_test("checks/test_shared.py", "from scripts import shared\n")
        self.add_test("checks/test_historical.py", "import scripts.component.deleted\n")
        self.add_test("checks/test_unrelated.py", "VALUE = 1\n")
        self.certify()
        files = tuple(f"assets/data-{number:03}.txt" for number in range(298))
        for path in files:
            self.write(path, "fixture\n")
        self.write("scripts/shared.py", "VALUE = 2\n")
        (self.repo / "scripts/component/deleted.py").unlink()
        _ = self.commit()
        expected = affected.AffectedSelection(
            self.base, (*files, "scripts/component/deleted.py", "scripts/shared.py"),
            tuple(self.repo / f"scripts/checks/test_{name}.py" for name in ("scans", "shared", "historical")),
        )
        for cache in ("cold", "warm"):
            with self.subTest(cache=cache):
                self.assertEqual(affected.select_affected(self.repo, self.base, self.modules, set(),
                                                          state_dir=self.cache_dir()), expected)

    def test_removed_python_sources_reuse_both_graphs_without_parsing_or_cache_writes(self) -> None:
        self.write("scripts/component/subject.py", "VALUE = 1\n")
        self.add_test("checks/test_deleted.py", "import scripts.component.subject\n")
        self.certify()
        (self.repo / "scripts/component/subject.py").unlink()
        self.write("scripts/checks/test_deleted.py", "import scripts.component.subject\n# Source removed.\n")
        _ = self.commit()
        first = affected.select_affected(self.repo, self.base, self.modules, affected.dirty_paths(self.repo),
                                         state_dir=self.cache_dir())
        self.assertIsInstance(first, affected.AffectedSelection)
        with (mock.patch.object(ast, "parse", wraps=ast.parse) as parse,
              mock.patch.object(affected, "write_fact_cache", wraps=affected.write_fact_cache) as write,
              mock.patch.object(affected, "blob_texts", wraps=affected.blob_texts) as read):
            second = affected.select_affected(self.repo, self.base, self.modules, affected.dirty_paths(self.repo),
                                              state_dir=self.cache_dir())
        self.assertEqual(second, first)
        self.assertEqual(parse.call_count, 0)
        write.assert_not_called()
        read.assert_not_called()

    def test_a_dirty_python_test_does_not_write_the_fact_cache(self) -> None:
        self.add_test("checks/test_dirty.py", "VALUE = 1\n")
        self.certify()
        expected = affected.select_affected(self.repo, self.base, self.modules, affected.dirty_paths(self.repo),
                                            state_dir=self.cache_dir())
        self.assertIsInstance(expected, affected.AffectedSelection)
        published = self.cache_file().read_bytes()
        self.write("scripts/checks/test_dirty.py", "VALUE = 2\n")
        with mock.patch.object(affected, "write_fact_cache", wraps=affected.write_fact_cache) as write:
            result = affected.select_affected(self.repo, self.base, self.modules, affected.dirty_paths(self.repo),
                                              state_dir=self.cache_dir())
        self.assertIsInstance(result, affected.AffectedSelection)
        assert isinstance(result, affected.AffectedSelection)
        self.assertEqual([path.name for path in result.modules], ["test_dirty.py"])
        write.assert_not_called()
        self.assertEqual(self.cache_file().read_bytes(), published)

    def test_invalid_non_test_python_sources_still_select_their_importers(self) -> None:
        self.write("scripts/component/subject.py", "VALUE = 1\n")
        self.add_test("checks/test_importer.py", "import scripts.component.subject\n")
        self.certify()
        self.write("scripts/component/subject.py", "this is invalid (\n")
        _ = self.commit()
        self.assertEqual([path.name for path in self.selection().modules], ["test_importer.py"])

    def test_a_deleted_test_does_not_select_its_folder_neighbors(self) -> None:
        self.add_test("checks/test_deleted.py", "VALUE = 1\n")
        self.add_test("checks/test_neighbor.py", "VALUE = 2\n")
        self.certify()
        deleted = self.repo / "scripts/checks/test_deleted.py"
        deleted.unlink()
        self.modules.remove(deleted)
        _ = self.commit()
        self.assertEqual(self.selection().modules, ())


if __name__ == "__main__":
    _ = unittest.main()
