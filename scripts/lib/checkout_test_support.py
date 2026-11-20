"""Build complete runtime copies and Python tools inside test fixtures."""

import ast
import importlib.util
import os
from functools import cache
from pathlib import Path
import shutil
import shlex
import sys

from scripts.lib.conf import TEST_CONFIG_DIRECTORY, TEST_CONFIG_PATH
from scripts.lib.python_startup.sitecustomize import STARTUP_FILES, TEST_RUNNER_FILES, inherited_paths


SOURCE = Path(__file__).resolve().parents[2]


@cache
def runtime_files() -> tuple[Path, ...]:
    """List runtime files without compiled files or unrelated test modules."""
    helpers = {"test_hang_guard.py", "test_models_registry.py", "test_support.py", "test_phase.py"}
    return tuple(path.relative_to(SOURCE) for path in sorted((SOURCE / "scripts").rglob("*"))
                 if path.is_file() and "__pycache__" not in path.parts
                 and (path.suffix != ".py" or not path.name.startswith("test_") or path.name in helpers))


def copy_with_bytecode(source: Path, destination: Path) -> None:
    """Copy a source file and its cached Python bytecode when present."""
    _ = shutil.copy2(source, destination)
    if source.suffix == ".py":
        bytecode = Path(importlib.util.cache_from_source(str(source)))
        if bytecode.exists():
            target = Path(importlib.util.cache_from_source(str(destination)))
            target.parent.mkdir(parents=True, exist_ok=True)
            _ = shutil.copy2(bytecode, target)


def copy_runtime(checkout: Path, *, configuration: bool = True) -> None:
    """Copy missing runtime files and keep the fixture's existing tools."""
    for relative in runtime_files():
        if relative.is_relative_to(TEST_CONFIG_PATH):
            continue
        destination = checkout / relative
        if not destination.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
            copy_with_bytecode(SOURCE / relative, destination)
    copy_test_configuration(checkout, configuration=configuration)


def copy_test_configuration(checkout: Path, *, configuration: bool = True) -> None:
    """Seed private config paths with fixed defaults."""
    def seed(source: Path, destination: Path) -> None:
        if destination.is_symlink():
            destination.unlink()
        if source.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
            for child in sorted(source.iterdir()):
                seed(child, destination / child.name)
        elif not destination.exists():
            _ = shutil.copy2(source, destination)

    seed(TEST_CONFIG_DIRECTORY, checkout / TEST_CONFIG_PATH)
    if configuration:
        seed(TEST_CONFIG_DIRECTORY, checkout / "config")


def copy_startup(checkout: Path) -> None:
    """Install the shared startup files in a temporary checkout."""
    for relative in STARTUP_FILES:
        destination = checkout / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.is_symlink():
            destination.unlink()
        copy_with_bytecode(SOURCE / relative, destination)


def copy_test_runner(checkout: Path) -> None:
    """Install the test runner and its dependencies in a temporary checkout."""
    for relative in TEST_RUNNER_FILES:
        if relative.startswith("config/"):
            continue
        destination = checkout / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.is_symlink():
            destination.unlink()
        copy_with_bytecode(SOURCE / relative, destination)
    copy_test_configuration(checkout)


def python_tool_source(source: str) -> str:
    """Use the shared checkout rule before a generated tool imports scripts."""
    startup = SOURCE / "scripts/lib/python_startup/sitecustomize.py"
    bootstrap = ("import runpy, sys\n"
                 + f"runpy.run_path({str(startup)!r})['select_checkout'](sys.argv)\n")
    syntax = ast.parse(source)
    position = 0
    for statement in syntax.body:
        if ((isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant)
             and isinstance(statement.value.value, str) and position == 0)
                or (isinstance(statement, ast.ImportFrom) and statement.module == "__future__")):
            position = statement.end_lineno or statement.lineno
        else:
            break
    lines = source.splitlines(keepends=True)
    lines.insert(position, bootstrap)
    return f"#!{sys.executable}\n" + "".join(lines)


def write_python_tool(path: Path, source: str) -> None:
    """Write a Python fixture tool with the shared startup rule."""
    _ = path.write_text(python_tool_source(source), encoding="utf-8")
    path.chmod(0o755)


def write_stdlib_tool(path: Path, source: str) -> None:
    """Write a fixture tool that imports only the standard library, so it skips the startup rule."""
    _ = path.write_text(f"#!{sys.executable} -IS\n" + source, encoding="utf-8")
    path.chmod(0o755)


def fixture_shim_source() -> str:
    """Preserve native arguments without an interpreter discovery probe."""
    startup = SOURCE / "scripts/lib/python_startup"
    return ("#!/bin/sh\nfixture_python=" + shlex.quote(sys.executable) + "\n"
            + "export PYTHONPATH=" + shlex.quote(str(startup)) + '\"${PYTHONPATH+:$PYTHONPATH}\"\n'
            + 'exec "${CLAUDE_PYTHON:-${TEST_REAL_PYTHON:-$fixture_python}}" "$@"\n')


def restore_startup_environment() -> None:
    """Keep startup active when a fixture interpreter replaces itself."""
    startup = SOURCE / "scripts/lib/python_startup"
    os.environ["PYTHONPATH"] = os.pathsep.join((str(startup), *inherited_paths(os.environ.get("PYTHONPATH", ""))))
