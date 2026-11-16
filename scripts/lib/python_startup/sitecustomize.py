"""Select one checkout for this process and preserve other startup hooks.

The hook also points the method compile of `dataclasses` at a shared code cache.
dataclasses compiles the generated methods of each class from source at import.
On 2026-11-14 that compile was 45-80% of the import CPU of the hot CLIs
(direct.assign 278 of 428 ms). The cache keeps the marshal code of each source
in /var/tmp/dataclass-code-<uid>/<cache tag>. tmp_sweep.py prunes the entries that no
process read in lint.conf `dataclass_code_days` days.
"""

from __future__ import annotations

import os
import stat
import sys
import time
from collections.abc import Callable, Sequence
from importlib.machinery import ModuleSpec, PathFinder
from importlib.util import module_from_spec
from types import CodeType, ModuleType


STARTUP_FILES = ("scripts/lib/py", "scripts/lib/package_root.py", "scripts/lib/python_startup/sitecustomize.py")
DATACLASS_CODE_FILENAME = "<string>"
# dataclasses.py has no __future__ import, so its exec compiles with no flags.
DATACLASS_CODE_FLAGS = 0
# A read refreshes an entry's time once a day, so the prune age counts in days.
DATACLASS_CODE_TOUCH_SECONDS = 86400
_dataclass_code_folder: list[str] = []


def checkout_root(start: str) -> str:
    """Find the checkout that contains a script or working directory."""
    folder = os.path.realpath(start)
    while folder:
        if os.path.isfile(os.path.join(folder, "scripts/lib/py")):
            return folder
        parent = os.path.dirname(folder)
        if parent == folder:
            break
        folder = parent
    return ""


def inherited_paths(value: str) -> list[str]:
    """Keep generic paths and remove checkout roots and startup directories."""
    return [entry for entry in value.split(os.pathsep) if value
            and not os.path.isdir(os.path.join(entry or os.curdir, "scripts"))
            and not entry.rstrip(os.sep).endswith(os.sep + "lib/python_startup")]


def _selection_inputs(paths: list[str]) -> tuple[tuple[str, ...], str, bool, str]:
    try:
        directory = os.getcwd()
    except OSError:
        directory = ""
    return tuple(paths), os.environ.get("PYTHONPATH", ""), "PYTHONPATH" in os.environ, directory


def select_checkout(arguments: list[str]) -> str:
    """Select the script checkout, working checkout, or helper checkout once."""
    state = sys.modules.setdefault("_devtools_checkout", ModuleType("_devtools_checkout"))
    if not hasattr(state, "__path__"):
        state.__path__ = []
    selected = next(iter(state.__path__), "")
    if selected and state.__dict__.get("completed_selection") == (selected, _selection_inputs(sys.path)):
        return selected
    if not selected:
        script = arguments[0] if arguments else ""
        selected = checkout_root(os.path.dirname(os.path.abspath(script))) if script not in {"", "-", "-c", "-m"} else ""
        selected = selected or checkout_root(os.getcwd())
        selected = selected or os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__)))))
        state.__path__ = [selected]
    paths = inherited_paths(os.environ.get("PYTHONPATH", ""))
    if paths:
        os.environ["PYTHONPATH"] = os.pathsep.join(paths)
    else:
        _ = os.environ.pop("PYTHONPATH", None)
    # Keep the entry script's folder before package imports.
    paths = [entry for entry in sys.path
             if os.path.realpath(entry) == selected
             or (not os.path.isdir(os.path.join(entry or os.curdir, "scripts"))
                 and not entry.rstrip(os.sep).endswith(os.sep + "lib/python_startup"))]
    if not any(os.path.realpath(entry) == selected for entry in paths):
        script_folder = os.path.dirname(os.path.abspath(arguments[0])) if arguments else ""
        position = int(bool(paths) and os.path.realpath(paths[0]) == script_folder and not getattr(sys.flags, "safe_path", False))
        paths.insert(position, selected)
    sys.path[:] = paths
    state.__dict__["completed_selection"] = (selected, _selection_inputs(sys.path))
    return selected


def dataclass_code_root() -> str:
    """Return this user's dataclass code cache root."""
    return f"/var/tmp/dataclass-code-{os.getuid()}"


def dataclass_code_usable(root: str, uid: int) -> bool:
    """Whether root is a folder that uid owns and that no other user can open."""
    try:
        status = os.lstat(root)
    except OSError:
        return False
    return stat.S_ISDIR(status.st_mode) and status.st_uid == uid and not status.st_mode & 0o077


def dataclass_code_folder(root: str) -> str:
    """Make the cache folder of this Python under root; return "" when the cache stays off."""
    try:
        os.mkdir(root, 0o700)
    except OSError:
        pass
    if not dataclass_code_usable(root, os.getuid()):
        return ""
    folder = os.path.join(root, sys.implementation.cache_tag)
    try:
        os.mkdir(folder, 0o700)
    except FileExistsError:
        pass
    except OSError:
        return ""
    return folder


def dataclass_code(source: str, folder: str) -> CodeType:
    """Return the code that the dataclasses exec compiles from source, from folder when it holds it.

    A missing, truncated or corrupt entry compiles again and replaces the file.
    """
    import hashlib
    import marshal

    key = hashlib.sha1(f"{DATACLASS_CODE_FLAGS} {sys.flags.optimize}\n{source}".encode()).hexdigest()
    path = os.path.join(folder, key)
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        descriptor = -1
    if descriptor >= 0:
        try:
            status = os.fstat(descriptor)
            data = os.read(descriptor, status.st_size)
        except OSError:
            status, data = None, b""
        finally:
            os.close(descriptor)
        try:
            cached: object = marshal.loads(data)  # pyright: ignore[reportAny]
        except (EOFError, ValueError, TypeError):
            cached = None
        if isinstance(cached, CodeType):
            if status is not None and status.st_mtime < time.time() - DATACLASS_CODE_TOUCH_SECONDS:
                try:
                    os.utime(path)
                except OSError:
                    pass
            return cached
    code = compile(source, DATACLASS_CODE_FILENAME, "exec", DATACLASS_CODE_FLAGS, dont_inherit=True)
    partial = f"{path}.{os.getpid()}"
    try:
        descriptor = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            _ = os.write(descriptor, marshal.dumps(code))
        finally:
            os.close(descriptor)
        os.replace(partial, path)
    except OSError:
        try:
            os.unlink(partial)
        except OSError:
            pass
    return code


def dataclass_exec(source: str, globals_: dict[str, object], locals_: dict[str, object]) -> None:
    """Run the generated method source of dataclasses with code from the shared cache."""
    if not _dataclass_code_folder:
        _dataclass_code_folder.append(dataclass_code_folder(dataclass_code_root()))
    folder = _dataclass_code_folder[0]
    code = (dataclass_code(source, folder) if folder
            else compile(source, DATACLASS_CODE_FILENAME, "exec", DATACLASS_CODE_FLAGS, dont_inherit=True))
    exec(code, globals_, locals_)


class DataclassCodeFinder:
    """Set `exec` in `dataclasses` to dataclass_exec when a process first imports it."""

    def find_spec(self, name: str, path: Sequence[str] | None, target: ModuleType | None = None, /) -> ModuleSpec | None:
        if name != "dataclasses":
            return None
        sys.meta_path.remove(self)
        spec = PathFinder.find_spec(name, path, target)
        loader = spec.loader if spec is not None else None
        if spec is None or loader is None:
            return spec
        load = loader.exec_module

        def exec_module(module: ModuleType) -> None:
            load(module)
            module.__dict__["exec"] = dataclass_exec

        vars(loader)["exec_module"] = exec_module
        return spec


def start_process(arguments: list[str]) -> str:
    """Select the checkout at interpreter startup and give a direct test run its folder."""
    selected = select_checkout(arguments)
    if "dataclasses" not in sys.modules and not any(
            type(finder).__name__ == DataclassCodeFinder.__name__ for finder in sys.meta_path):
        sys.meta_path.insert(0, DataclassCodeFinder())
    _start_test_run(selected, arguments)
    return selected


def gate_parent_pid(consume: bool = False) -> str:
    """Read the gate parent and optionally clear both names before tests start."""
    # Chunk 6 removes the old parent variable read.
    parent = os.environ.get("GATE_PARENT_PID") or os.environ.get("MERGE_GATE_PARENT_PID", "")
    if consume:
        _ = os.environ.pop("GATE_PARENT_PID", None)
        _ = os.environ.pop("MERGE_GATE_PARENT_PID", None)
    return parent


def _start_test_run(selected: str, arguments: list[str]) -> None:
    # Decide before any import; module_run.py removes the gate variables later.
    if gate_parent_pid():
        return
    original = sys.orig_argv
    unittest_module = ("-munittest" in original
                       or any(original[index:index + 2] == ["-m", "unittest"] for index in range(len(original))))
    target = arguments[0] if arguments else ""
    if not (unittest_module or target == "unittest" or os.path.basename(target) == "module_run.py"):
        return
    # Partial fixture checkouts can contain only the startup files.
    if not os.path.isfile(os.path.join(selected, "scripts/lib/testing_folders.py")):
        return
    from scripts.lib.testing_folders import start_test_run

    start_test_run()


def after_startup(callback: Callable[[], None]) -> None:
    """Start an observer after the outer startup hook finishes."""
    sys.modules["_devtools_checkout"].__dict__["ready"] = callback


def finish_startup(filename: str, arguments: list[str]) -> None:
    """Finish path selection before Python adds its automatic search directory."""
    hook = sys.modules.get("sitecustomize")
    if hook is None or not hook.__file__ or os.path.realpath(hook.__file__) != os.path.realpath(filename):
        return
    selected = select_checkout(arguments)
    automatic_paths = sys.path
    if not getattr(sys.flags, "safe_path", False):
        script = arguments[0] if arguments else ""
        automatic = os.getcwd() if script in {"", "-", "-c", "-m"} else os.path.dirname(os.path.abspath(script))
        if os.path.realpath(automatic) == selected:
            sys.path[:] = [entry for entry in sys.path if os.path.realpath(entry) != selected]
            automatic_paths = [automatic, *sys.path]
        elif (not os.path.isdir(os.path.join(automatic, "scripts"))
              and not automatic.rstrip(os.sep).endswith(os.sep + "lib/python_startup")):
            automatic_paths = [automatic, *sys.path]
    state = sys.modules["_devtools_checkout"]
    # Python adds its automatic path after this hook returns.
    state.__dict__["completed_selection"] = (selected, _selection_inputs(automatic_paths))
    if "ready" in state.__dict__:
        from typing import cast

        ready = cast(Callable[[], None], state.__dict__.pop("ready"))
        ready()


def next_sitecustomize(filename: str, search: list[str] | None = None) -> None:
    """Run the next hook after this hook's directory."""
    directory = os.path.realpath(os.path.dirname(filename))
    remaining: list[str] = []
    passed_own = False
    for entry in sys.path if search is None else search:
        if os.path.realpath(entry) == directory:
            passed_own = True
        elif passed_own and not entry.rstrip(os.sep).endswith(os.sep + "lib/python_startup"):
            # Continue past redundant copies of this startup helper.
            remaining.append(entry)
    spec = PathFinder.find_spec("sitecustomize", remaining)
    if spec is not None and spec.loader is not None:
        from typing import cast

        module = module_from_spec(spec)
        # Keep hook contexts active for the process lifetime.
        hooks = cast(list[ModuleType], sys.modules["_devtools_checkout"].__dict__.setdefault("startup_hooks", []))
        spec.loader.exec_module(module)
        hooks.append(module)


# Reuse this module when an entry loads package_root.py.
if __name__ in sys.modules:
    _ = sys.modules.setdefault("_devtools_python_startup", sys.modules[__name__])


if __name__ == "sitecustomize":
    # Save the hook order before selection removes this startup directory.
    following = sys.path[:]
    _ = start_process(sys.argv)
    next_sitecustomize(__file__, following)
    finish_startup(__file__, sys.argv)
