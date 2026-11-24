"""Build complete runtime copies and Python tools inside test fixtures."""

import ast
import importlib.util
import os
from functools import cache
from pathlib import Path
import shutil
import shlex
import subprocess
import sys

from scripts.lib.python_startup.sitecustomize import STARTUP_FILES, TEST_RUNNER_FILES, startup_pythonpath


SOURCE = Path(__file__).resolve().parents[2]
# Child fixtures can model a direct launch inside an outer gate module.
STANDALONE_TEST_STARTUP = """import sys
sys.modules["_devtools_python_startup"]._descends_from_gate_module = lambda: False
"""


@cache
def runtime_files() -> tuple[Path, ...]:
    """List runtime files without compiled files or unrelated test modules."""
    helpers = {"test_hang_guard.py", "test_models_registry.py", "test_support.py", "test_phase.py"}
    return tuple(path.relative_to(SOURCE) for folder in ("scripts", "config", "test_config")
                 for path in sorted((SOURCE / folder).rglob("*"))
                 if (path.is_file() or path.is_symlink() and path.is_dir()) and "__pycache__" not in path.parts
                 and (path.suffix != ".py" or not path.name.startswith("test_") or path.name in helpers))


def copy_with_bytecode(source: Path, destination: Path) -> None:
    """Copy a source file and its cached Python bytecode when present."""
    if source.is_symlink() and source.is_dir():
        destination.symlink_to(source.readlink(), target_is_directory=True)
        return
    _ = shutil.copy2(source, destination)
    if source.suffix == ".py":
        bytecode = Path(importlib.util.cache_from_source(str(source)))
        if bytecode.exists():
            target = Path(importlib.util.cache_from_source(str(destination)))
            target.parent.mkdir(parents=True, exist_ok=True)
            _ = shutil.copy2(bytecode, target)


def copy_runtime(checkout: Path, *, configuration: bool = True) -> None:
    """Copy missing runtime files and keep the fixture's existing tools."""
    from scripts.lib.conf import copy_for_test

    for relative in runtime_files():
        if relative.parts[0] == "config" or not configuration and relative.parts[0] == "test_config":
            continue
        destination = checkout / relative
        if not os.path.lexists(destination):
            destination.parent.mkdir(parents=True, exist_ok=True)
            copy_with_bytecode(SOURCE / relative, destination)
    if configuration:
        _ = copy_for_test(SOURCE / "config", checkout / "config")


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
    for relative in (*TEST_RUNNER_FILES, "test_config/build_folders.conf", "test_config/test_times.conf"):
        destination = checkout / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.is_symlink():
            destination.unlink()
        copy_with_bytecode(SOURCE / relative, destination)


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
    os.environ["PYTHONPATH"] = startup_pythonpath(os.environ.get("PYTHONPATH", ""))


def direct_test_fixture_environment(root: Path, *, seconds: int | None = None) -> dict[str, str]:
    """Give the direct test a private folder and an explicit limit."""
    from scripts.lib.testing_folders import RUN_FOLDER_ENV
    from scripts.lint.memory_test_support import write_config_fixture

    config = root / "config"
    _ = write_config_fixture(config / "build_folders.conf",
                             f"roots.build={root / 'build'}\nroots.build.natedev={root / 'build'}\n")
    if seconds is not None:
        _ = write_config_fixture(config / "test_times.conf",
                                 f"# whole: 1..\ndirect_test.time_limit_seconds={seconds}\n")
    environment = {**os.environ, "TMPDIR": str(root), "CONF_DIR": str(config),
                   "XDG_CACHE_HOME": str(root / "cache"), "CLAUDE_PYTHON": sys.executable}
    for name in ("GATE_PARENT_PID", "MERGE_GATE_PARENT_PID", "DEVTOOLS_GATE_PARENT_SOURCE_FD",
                 "TEST_TIMES_OUT", RUN_FOLDER_ENV):
        _ = environment.pop(name, None)
    return environment


def without_user_test_manager(root: Path, environment: dict[str, str]) -> dict[str, str]:
    """Give the child a user bus address that has no listener."""
    return {**environment, "DBUS_SESSION_BUS_ADDRESS": f"unix:path={root / 'absent-bus'}",
            "XDG_RUNTIME_DIR": str(root / "absent-runtime")}


def run_gate_test_fixture(case: Path, arguments: list[str], environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Run a fixture through the same launcher as a gate module."""
    from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
    from scripts.production import gate

    log = case.parent / "gate.log"
    job = gate.start_gate_job("lib", case, arguments, SOURCE, environment, log)
    try:
        status = job.process.wait(timeout=TEST_HANG_GUARD_SECS)
    finally:
        gate.kill_job_group(job)
        job.output.close()
    return subprocess.CompletedProcess(arguments, status, log.read_text(), "")
