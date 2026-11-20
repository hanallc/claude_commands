"""Give cleanup tests a private process table."""

from __future__ import annotations

import os
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from scripts.lib.python_startup.sitecustomize import inherited_paths
from scripts.lint import tmp_sweep


NO_LOCK_FDS: frozenset[int] = frozenset()


@contextmanager
def use_cleanup_process_table(proc: Path) -> Generator[None]:
    def read_processes(prefix: str | tuple[str, ...], *, require_complete: bool = False,
                       lock_fds: frozenset[int] = NO_LOCK_FDS
                       ) -> tmp_sweep.ProcessUsesKnown | tmp_sweep.ProcessUsesUnavailable:
        return tmp_sweep.process_paths(str(proc), prefix, quiesce_fds=lock_fds if require_complete else None)

    with (patch.object(tmp_sweep, "PROC_DIR", str(proc)),
          patch.object(tmp_sweep, "lsof_paths", read_processes)):
        yield


@contextmanager
def private_cleanup_processes(root: Path) -> Generator[Path]:
    """Let the cleanup guard scan fixture processes in this interpreter and its children."""
    proc = root / "cleanup-proc"
    proc.mkdir()
    startup = root / "cleanup-python-startup"
    startup.mkdir()
    bootstrap = Path(__file__).with_name("python_startup") / "sitecustomize.py"
    _ = (startup / "sitecustomize.py").write_text(
        f"""import runpy, sys
_startup = runpy.run_path({str(bootstrap)!r}, run_name='_cleanup_python_startup')
_startup['select_checkout'](sys.argv)
_startup['next_sitecustomize'](__file__)
from pathlib import Path
from scripts.lib.cleanup_process_test_support import use_cleanup_process_table
_cleanup_processes = use_cleanup_process_table(Path({str(proc)!r}))
_cleanup_processes.__enter__()
_startup['finish_startup'](__file__, sys.argv)
""",
        encoding="utf-8",
    )
    python_path = os.pathsep.join((str(startup), *inherited_paths(os.environ.get("PYTHONPATH", ""))))
    with patch.dict(os.environ, {"PYTHONPATH": python_path}), use_cleanup_process_table(proc):
        yield proc
