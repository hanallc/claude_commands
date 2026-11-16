"""Keep copy operations outside live slot and gate roots during tests."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from scripts.copies import copies
from scripts.lib import conf
from scripts.lib.build_slots_test_support import private_slot_config
from scripts.production.cli_fixture import ImmutableGitSeed


class LiveRootsGuardTests(unittest.TestCase):
    def test_copy_operations_and_cleanup_use_only_private_roots(self) -> None:
        values = conf.read_or_repository("build_folders").values()
        live = [str(Path(value).expanduser().resolve()) for key, value in values.items()
                if key == "roots.slots" or key.startswith("roots.slots.")]
        live.append(str(Path.home() / "worktrees/gates"))
        live.append(str(Path.home() / "worktrees"))
        registry = str(copies.remote_registry().resolve())
        live.append(registry)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            private = private_slot_config(root, repository="origin")
            source = ImmutableGitSeed.create(root, "main", "Copy Tests", "copy@example.invalid",
                {"source.txt": "source\n"}).checkout
            temporary = root / "temporary"
            temporary.mkdir()
            home = root / "home"
            home.mkdir()
            program = '''import json, os, pathlib, sys
from scripts.copies import copies
from scripts.lib import build_folders, copy_record, scratch
from scripts.lib.cargo_output_test_support import fake_cargo_output_roots
root, source = map(pathlib.Path, sys.argv[1:3])
forbidden = tuple(pathlib.Path(value) for value in json.loads(sys.argv[3]))
def checked(value, descriptor=None):
    if not isinstance(value, (str, bytes, os.PathLike)):
        return
    path = pathlib.Path(os.fsdecode(value))
    if not path.is_absolute():
        base = pathlib.Path('/proc/self/fd/' + str(descriptor)).resolve() if descriptor not in (None, -1) else pathlib.Path.cwd()
        path = base / path
    path = path.resolve()
    if any(path == parent or path.is_relative_to(parent) for parent in forbidden):
        raise AssertionError('test writes under a live root: ' + str(path))
def audit(event, arguments):
    if event == 'open':
        name, mode, flags = arguments
        if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
            checked(name)
    elif event in ('os.mkdir', 'os.remove', 'os.rmdir', 'os.chmod'):
        checked(arguments[0], arguments[-1] if len(arguments) > 1 else None)
    elif event == 'os.rename':
        checked(arguments[0], arguments[2])
        checked(arguments[1], arguments[3])
    elif event == 'shutil.rmtree':
        checked(arguments[0], arguments[1])
    elif event == 'subprocess.Popen':
        executable, argv, cwd, environment = arguments
        if cwd is not None:
            checked(cwd)
        for argument in argv:
            if isinstance(argument, str) and argument.startswith('/'):
                checked(argument)
open_file = os.open
def checked_open(path, flags, mode=0o777, *, dir_fd=None):
    if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
        checked(path, dir_fd)
    return open_file(path, flags, mode, dir_fd=dir_fd)
os.open = checked_open
sys.addaudithook(audit)
for parent in (*forbidden, pathlib.Path(sys.argv[4])):
    try:
        sys.audit('open', str(parent / 'guard-probe'), 'w', os.O_WRONLY | os.O_CREAT)
    except AssertionError:
        pass
    else:
        raise AssertionError('the guard accepted a live root: ' + str(parent))
_ = os.environ.pop('SCRATCH_COPIES_ROOT', None)
with fake_cargo_output_roots():
    mirror = copies.RemoteMirror('private-test', copies.SelectedRepository('origin'),
                                 'fixture.invalid', str(root / 'remote'), '')
    copies.register_remote(mirror)
    record = copies.remote_record_path(mirror)
    assert record.is_relative_to(root / 'remote-mirrors')
    assert record.is_file()
    copies.unregister_remote(mirror.host, mirror.path)
    assert not record.exists()
    with copies.standing('private-test', source, copies.CommittedSource('HEAD')) as gate:
        assert gate.is_relative_to(root / 'slots')
        assert (gate / 'source.txt').read_text() == 'source\\n'
    assert build_folders.build_folder_state(gate) == 'free'
    with copies.workspace('private-test', source, 'HEAD', name='editor', branch='editor') as workspace:
        assert workspace.is_relative_to(root / 'workspaces')
        assert copy_record.copy_folder(workspace) == workspace
        assert (workspace / 'source.txt').read_text() == 'source\\n'
    copies.remove_trees('private-test')
    assert not workspace.exists()
    assert gate.exists()
    clone = copies.clone(source, task='gate:private-proof', word='proof')
    assert clone.is_relative_to(root / 'scratch')
    assert (clone / 'source.txt').read_text() == 'source\\n'
    with scratch.removal_lock(clone) as free:
        assert free
        scratch.remove_marked(clone, scratch.remove)
    assert not clone.exists()
print('standing, workspace, clone, and cleanup stayed in private roots')
import unittest
suite = unittest.defaultTestLoader.loadTestsFromName('scripts.buildlog.test_rust_release')
result = unittest.TextTestRunner(verbosity=1).run(suite)
assert result.wasSuccessful(), 'the guarded buildlog module failed'
print('buildlog fixture stayed in private roots')
'''
            environment = {**os.environ, **private.environment, "TMPDIR": str(temporary), "HOME": str(home),
                           "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(Path(__file__).resolve().parents[2])}
            result = subprocess.run([sys.executable, "-c", program, str(root), str(source), json.dumps(live), registry],
                                    env=environment, cwd=root, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("cleanup stayed in private roots", result.stdout)
            self.assertIn("buildlog fixture stayed in private roots", result.stdout)
