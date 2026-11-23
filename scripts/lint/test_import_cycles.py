"""Tests that scripts/ has no import cycle as basedpyright counts it, and tests the graph rules."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.lint.import_cycles_test_support import cycles

REPO = Path(__file__).resolve().parents[2]
ADVICE = "basedpyright counts lazy imports. Move the shared code to a lower module; do not hide the import."
type Config = dict[str, list[dict[str, str | list[str]]]]
CONFIG: Config = {
    "executionEnvironments": [{"root": "scripts/tool", "extraPaths": [".", "scripts/shared"]}, {"root": "."}],
}


class ScriptsImportCycleTest(unittest.TestCase):
    def test_scripts_have_no_import_cycle(self) -> None:
        found = cycles(REPO)
        if not found:
            return
        lines = [f"{len(found)} import cycles in scripts/:"]
        for number, component in enumerate(found, 1):
            lines.append(f"cycle {number} ({len(component)} modules):")
            lines.extend(f"    {member}" for member in component)
        lines.append(ADVICE)
        self.fail("\n".join(lines))


class ImportGraphFixtureTest(unittest.TestCase):
    def repository(self, files: dict[str, str], config: Config = CONFIG) -> Path:
        """Write a small repository with the config and the given files."""
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        _ = (root / "pyrightconfig.json").write_text(json.dumps(config), encoding="utf-8")
        for name, text in files.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            _ = path.write_text(text, encoding="utf-8")
        return root

    def test_lazy_import_closes_a_cycle(self) -> None:
        root = self.repository({
            "scripts/tool/a.py": "from scripts.tool import b\n",
            "scripts/tool/b.py": "def load() -> None:\n    from scripts.tool import a\n",
        })
        self.assertEqual(cycles(root), [["scripts/tool/a.py", "scripts/tool/b.py"]])

    def test_import_text_in_a_string_is_not_an_edge(self) -> None:
        root = self.repository({
            "scripts/tool/a.py": "import scripts.tool.b\n",
            "scripts/tool/b.py": '"""Usage:\n\nimport scripts.tool.a\n"""\n',
        })
        self.assertEqual(cycles(root), [])

    def test_parenthesized_from_import_gives_an_edge_to_each_submodule(self) -> None:
        root = self.repository({
            "scripts/tool/a.py": "from scripts.tool import (\n    b,\n    c,\n)\n",
            "scripts/tool/b.py": "import scripts.tool.a\n",
            "scripts/tool/c.py": "import scripts.tool.a\n",
        })
        self.assertEqual(cycles(root), [["scripts/tool/a.py", "scripts/tool/b.py", "scripts/tool/c.py"]])

    def test_extra_path_bare_import_gives_an_edge(self) -> None:
        root = self.repository({
            "scripts/tool/a.py": "import shared_module\n",
            "scripts/shared/shared_module.py": "import scripts.tool.a\n",
        })
        self.assertEqual(cycles(root), [["scripts/shared/shared_module.py", "scripts/tool/a.py"]])

    def test_import_module_string_is_not_an_edge(self) -> None:
        root = self.repository({
            "scripts/tool/a.py": "import scripts.tool.b\n",
            "scripts/tool/b.py": 'import importlib\n\n_ = importlib.import_module("scripts.tool.a")\n',
        })
        self.assertEqual(cycles(root), [])

    def test_dotted_import_counts_each_package_init(self) -> None:
        root = self.repository({
            "scripts/pkg/__init__.py": "import scripts.user\n",
            "scripts/pkg/mod.py": "",
            "scripts/user.py": "import scripts.pkg.mod\n",
        })
        self.assertEqual(cycles(root), [["scripts/pkg/__init__.py", "scripts/user.py"]])

    def test_from_import_counts_the_package_init(self) -> None:
        root = self.repository({
            "scripts/pkg/__init__.py": "import scripts.user\n",
            "scripts/pkg/mod.py": "",
            "scripts/user.py": "from scripts.pkg import mod\n",
        })
        self.assertEqual(cycles(root), [["scripts/pkg/__init__.py", "scripts/user.py"]])

    def test_from_dotted_import_skips_the_package_inits(self) -> None:
        root = self.repository({
            "scripts/pkg/__init__.py": "import scripts.user\n",
            "scripts/pkg/mod.py": "",
            "scripts/user.py": "from scripts.pkg.mod import name\n",
        })
        self.assertEqual(cycles(root), [])

    def test_import_as_skips_the_package_inits(self) -> None:
        root = self.repository({
            "scripts/pkg/__init__.py": "import scripts.user\n",
            "scripts/pkg/mod.py": "",
            "scripts/user.py": "import scripts.pkg.mod as mod\n",
        })
        self.assertEqual(cycles(root), [])

    def test_local_submodule_of_a_stdlib_name_is_an_edge(self) -> None:
        root = self.repository({
            "scripts/tool/email/__init__.py": "",
            "scripts/tool/email/mine.py": "import scripts.tool.user\n",
            "scripts/tool/user.py": "import email.mine\n",
        })
        self.assertEqual(cycles(root), [["scripts/tool/email/mine.py", "scripts/tool/user.py"]])

    def test_namespace_folders_merge_across_search_roots(self) -> None:
        root = self.repository({
            "scripts/tool/space/first.py": "",
            "scripts/shared/space/second.py": "import scripts.tool.user\n",
            "scripts/tool/user.py": "from space import second\n",
        })
        self.assertEqual(cycles(root), [["scripts/shared/space/second.py", "scripts/tool/user.py"]])

    def test_star_import_counts_each_module_in_the_package(self) -> None:
        root = self.repository({
            "scripts/pkg/__init__.py": "",
            "scripts/pkg/mod.py": "import scripts.user\n",
            "scripts/user.py": "from scripts.pkg import *\n",
        })
        self.assertEqual(cycles(root), [["scripts/pkg/mod.py", "scripts/user.py"]])

    def test_absolute_import_walks_up_from_the_importing_folder(self) -> None:
        root = self.repository({
            "scripts/tool/sub/a.py": "import helper\n",
            "scripts/tool/sub/helper.py": "from scripts.tool.sub import a\n",
        })
        self.assertEqual(cycles(root), [["scripts/tool/sub/a.py", "scripts/tool/sub/helper.py"]])

    def test_repository_root_is_not_a_search_root_unless_listed(self) -> None:
        config: Config = {"executionEnvironments": [{"root": "scripts/tool", "extraPaths": ["scripts/shared"]}]}
        root = self.repository({
            "scripts/tool/a.py": "import scripts.tool.b\n",
            "scripts/tool/b.py": "import scripts.tool.a\n",
        }, config)
        self.assertEqual(cycles(root), [])

    def test_relative_imports_resolve_against_the_package(self) -> None:
        root = self.repository({
            "scripts/tool/a.py": "from . import b\n",
            "scripts/tool/b.py": "from .a import name\n",
        })
        self.assertEqual(cycles(root), [["scripts/tool/a.py", "scripts/tool/b.py"]])

    def test_relative_submodule_import_skips_the_package_init(self) -> None:
        root = self.repository({
            "scripts/rel/__init__.py": "from scripts.rel import x\n",
            "scripts/rel/x.py": "from .y import z\n",
            "scripts/rel/y.py": "",
        })
        self.assertEqual(cycles(root), [])


if __name__ == "__main__":
    _ = unittest.main()
