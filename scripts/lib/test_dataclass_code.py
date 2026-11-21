"""Check the dataclass code cache that the startup hook installs and tmp_sweep.py prunes."""
from __future__ import annotations

from collections.abc import Callable
import marshal
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import CodeType
from typing import cast
import unittest

from scripts.lib.python_startup import sitecustomize as startup
from scripts.lint import tmp_sweep


ROOT = Path(__file__).resolve().parents[2]
SOURCE = "def __create_fn__():\n def __init__(self, a: int) -> None:\n  self.a = a\n return (__init__,)"
DAY = 86400
# The child records each source that dataclasses gives its exec, and compiles it with no cache.
ACTIVE_CHECK = """
import builtins, dataclasses
installed = getattr(dataclasses, "exec", None)
sources = []
def record(source, globals_, locals_):
    sources.append(source)
    builtins.exec(compile(source, "<string>", "exec", 0, dont_inherit=True), globals_, locals_)
dataclasses.exec = record
@dataclasses.dataclass(frozen=True)
class Probe:
    value: int
print(getattr(installed, "__name__", ""), len(sources), Probe(1).value)
"""


def fresh() -> CodeType:
    return compile(SOURCE, "<string>", "exec", 0, dont_inherit=True)


def init_annotations(code: CodeType) -> dict[str, object]:
    """Run the generated source and return the annotations of its __init__."""
    namespace: dict[str, object] = {}
    exec(code, {}, namespace)
    create = cast(Callable[[], tuple[Callable[..., None]]], namespace["__create_fn__"])
    return cast(dict[str, object], getattr(create()[0], "__annotations__"))


def cache(test: unittest.TestCase) -> tuple[str, str]:
    """Make a 0700 cache root for test; return it and the folder of this Python."""
    temporary = tempfile.TemporaryDirectory()
    test.addCleanup(temporary.cleanup)
    root = os.path.join(temporary.name, "cache")
    os.mkdir(root, 0o700)
    return root, startup.dataclass_code_folder(root)


class DataclassCodeTests(unittest.TestCase):
    def test_the_startup_hook_gives_dataclasses_the_cached_exec_on_this_python(self) -> None:
        result = subprocess.run([str(ROOT / "scripts/lib/py"), "-c", ACTIVE_CHECK], cwd=ROOT,
                                capture_output=True, text=True, check=False, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.split(), [startup.dataclass_exec.__name__, "1", "1"])

    def test_a_cached_entry_equals_a_fresh_compile_without_the_hook_future_flags(self) -> None:
        _, folder = cache(self)
        first = startup.dataclass_code(SOURCE, folder)
        second = startup.dataclass_code(SOURCE, folder)
        self.assertEqual(len(os.listdir(folder)), 1)
        for code in (first, second):
            self.assertEqual(code, fresh())
            self.assertEqual(code.co_filename, "<string>")
            self.assertIs(init_annotations(code)["a"], int)

    def test_a_truncated_or_corrupt_entry_compiles_again_and_replaces_the_file(self) -> None:
        _, folder = cache(self)
        _ = startup.dataclass_code(SOURCE, folder)
        [entry] = (Path(folder) / name for name in os.listdir(folder))
        whole = entry.read_bytes()
        for broken in (whole[: len(whole) // 2], b"not marshal", marshal.dumps("text")):
            _ = entry.write_bytes(broken)
            self.assertEqual(startup.dataclass_code(SOURCE, folder), fresh())
            self.assertEqual(entry.read_bytes(), whole)

    def test_a_read_refreshes_an_entry_older_than_a_day(self) -> None:
        _, folder = cache(self)
        _ = startup.dataclass_code(SOURCE, folder)
        [entry] = (os.path.join(folder, name) for name in os.listdir(folder))
        old = time.time() - 2 * DAY
        os.utime(entry, (old, old))
        _ = startup.dataclass_code(SOURCE, folder)
        self.assertGreater(os.stat(entry).st_mtime, old + DAY)

    def test_a_root_another_user_can_open_or_does_not_own_turns_the_cache_off(self) -> None:
        root, _ = cache(self)
        self.assertTrue(startup.dataclass_code_usable(root, os.getuid()))
        self.assertFalse(startup.dataclass_code_usable(root, os.getuid() + 1))
        os.chmod(root, 0o750)
        self.assertFalse(startup.dataclass_code_usable(root, os.getuid()))
        self.assertEqual(startup.dataclass_code_folder(root), "")

    def test_the_prune_removes_unread_entries_and_an_emptied_old_python_folder(self) -> None:
        root, folder = cache(self)
        _ = startup.dataclass_code(SOURCE, folder)
        old_python = os.path.join(root, "cpython-0")
        os.mkdir(old_python, 0o700)
        unread = os.path.join(old_python, "unread")
        Path(unread).touch()
        now = time.time()
        os.utime(unread, (now - 15 * DAY, now - 15 * DAY))
        self.assertEqual(tmp_sweep.prune_dataclass_code(root, now, 14), 1)
        self.assertEqual(sorted(os.listdir(root)), [sys.implementation.cache_tag])
        self.assertEqual(len(os.listdir(folder)), 1)


if __name__ == "__main__":
    _ = unittest.main()
