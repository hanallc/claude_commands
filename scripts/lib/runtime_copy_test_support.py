"""List the runtime files of this checkout and copy them into test fixtures."""

from __future__ import annotations

from functools import cache
from pathlib import Path

from scripts.lib.checkout_test_support import copy_with_bytecode


SOURCE = Path(__file__).resolve().parents[2]


@cache
def runtime_files() -> tuple[Path, ...]:
    """List runtime files without compiled files or unrelated test modules."""
    helpers = ("test_hang_guard.py", "test_models_registry.py", "test_support.py", "test_phase.py")
    return tuple(path.relative_to(SOURCE) for folder in ("scripts", "config")
                 for path in sorted((SOURCE / folder).rglob("*"))
                 if (path.is_file() or path.is_symlink() and path.is_dir()) and "__pycache__" not in path.parts
                 and (path.suffix != ".py" or not path.name.startswith("test_") or path.name in helpers))


def copy_runtime(checkout: Path, *, configuration: bool = True) -> None:
    """Copy missing runtime files and keep the fixture's existing tools."""
    for relative in runtime_files():
        if not configuration and relative.parts[0] == "config":
            continue
        destination = checkout / relative
        if not destination.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
            copy_with_bytecode(SOURCE / relative, destination)
