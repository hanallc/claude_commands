"""Keep copy operations outside live slot, gate, and session roots during tests."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from typing import cast
from unittest.mock import patch

from scripts.copies import copies
from scripts.lib import build_folders, conf, session_folder
from scripts.lib.build_slots_test_support import private_slot_config
from scripts.lint.memory_test_support import write_config_fixture
from scripts.production.cli_fixture import ImmutableGitSeed


def live_roots() -> tuple[Path, ...]:
    values = conf.read_or_repository("build_folders").values()
    configured = [Path(value).expanduser() for key, value in values.items()
                  if key in ("roots.slots", "roots.session") or key.startswith(("roots.slots.", "roots.session."))]
    return tuple(path.resolve() for path in (
        *configured, Path.home() / "worktrees/gates", Path.home() / "worktrees",
        session_folder.configured_root(), session_folder.former_root(),
        build_folders.inherited_temporary_parent(), copies.remote_registry(),
    ))


class LiveRootsGuardTests(unittest.TestCase):
    def test_copy_operations_and_cleanup_use_only_private_roots(self) -> None:
        caller_environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        _ = cast(object, self.enterContext(patch.dict(os.environ, caller_environment, clear=True)))
        caller_roots = live_roots()
        candidates = (Path("/var/tmp"), Path("/tmp"), Path("/dev/shm"), Path.home())
        parents = [parent for parent in map(Path.resolve, candidates)
                   if parent.is_dir() and os.access(parent, os.W_OK | os.X_OK)
                   and not any(parent.is_relative_to(live) for live in caller_roots)]
        self.assertTrue(parents, f"No writable fixture parent is outside the live roots: {caller_roots}")
        # The guarded roots and the roots used by the copy operations are siblings.
        with tempfile.TemporaryDirectory(dir=parents[0]) as folder:
            base = Path(folder).resolve()
            self.assertFalse(any(base.is_relative_to(parent) for parent in caller_roots), caller_roots)
            guarded = base / "guarded"
            root = base / "private"
            protected = private_slot_config(guarded, repository="origin")
            private = private_slot_config(root, repository="origin")
            for fixture in (protected, private):
                _ = write_config_fixture(Path(fixture.environment["COPIES_CONFIG"]),
                                         "git_timeout_seconds=30\nguard_timeout_seconds=30\n")
            guarded_temporary = guarded / "temporary"
            guarded_temporary.mkdir()
            with patch.dict(os.environ, {**protected.environment, "TMPDIR": str(guarded_temporary),
                                         "HOME": str(guarded / "home")}):
                protected_roots = live_roots()
                registry = str(copies.remote_registry().resolve())
            live = (*protected_roots, *caller_roots)
            self.assertFalse(any(root.is_relative_to(parent) for parent in live), live)
            temporary = root / "temporary"
            temporary.mkdir()
            home = root / "home"
            home.mkdir()
            environment = {"PATH": os.environ.get("PATH", os.defpath), **private.environment,
                           "TMPDIR": str(temporary), "HOME": str(home),
                           "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(Path(__file__).resolve().parents[2])}
            with patch.dict(os.environ, environment, clear=True):
                source = ImmutableGitSeed.create(root, "main", "Copy Tests", "copy@example.invalid",
                    {"source.txt": "source\n"}).checkout
            program = '''import json, os, pathlib, subprocess, sys
from scripts.copies import copies
from scripts.lib import build_folders, copy_record, scratch
from scripts.lib.cargo_output_test_support import fake_cargo_output_roots
root, source = map(pathlib.Path, sys.argv[1:3])
forbidden = tuple(pathlib.Path(value) for value in json.loads(sys.argv[3]))
def checked(value, descriptor=None, *, writing=False):
    if not isinstance(value, (str, bytes, os.PathLike)):
        return
    path = pathlib.Path(os.fsdecode(value))
    if not path.is_absolute():
        base = pathlib.Path('/proc/self/fd/' + str(descriptor)).resolve() if descriptor not in (None, -1) else pathlib.Path.cwd()
        path = base / path
    path = path.resolve()
    if any(path == parent or path.is_relative_to(parent) for parent in forbidden):
        raise AssertionError('test writes under a live root: ' + str(path))
    if writing and path != pathlib.Path('/dev/null') and not path.is_relative_to(root):
        raise AssertionError('test writes outside its fixture root: ' + str(path))
def audit(event, arguments):
    if event == 'open':
        name, mode, flags = arguments
        if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
            checked(name, writing=True)
    elif event in ('os.mkdir', 'os.remove', 'os.rmdir', 'os.chmod'):
        checked(arguments[0], arguments[-1], writing=True)
    elif event == 'os.rename':
        checked(arguments[0], arguments[2], writing=True)
        checked(arguments[1], arguments[3], writing=True)
    elif event == 'shutil.rmtree':
        checked(arguments[0], arguments[1], writing=True)
    elif event == 'subprocess.Popen':
        executable, argv, cwd, environment = arguments
        git = pathlib.Path(os.fsdecode(executable)).name == 'git'
        if cwd is not None:
            checked(cwd, writing=git)
        base = pathlib.Path.cwd() if cwd is None else pathlib.Path(os.fsdecode(cwd)).resolve()
        for position, argument in enumerate(argv):
            path = pathlib.Path(os.fsdecode(argument))
            checked(base / path, writing=git and position > 0 and path.is_absolute())
open_file = os.open
def checked_open(path, flags, mode=0o777, *, dir_fd=None):
    if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
        checked(path, dir_fd, writing=True)
    return open_file(path, flags, mode, dir_fd=dir_fd)
os.open = checked_open
sys.addaudithook(audit)
for parent in (*forbidden, pathlib.Path(sys.argv[4])):
    try:
        sys.audit('open', str(parent / 'guard-probe'), 'w', os.O_WRONLY | os.O_CREAT)
    except AssertionError as error:
        if not str(error).startswith('test writes under a live root: '):
            raise
    else:
        raise AssertionError('the guard accepted a live root: ' + str(parent))
for target, reason in ((forbidden[0], 'under a live root'), (root.parent / 'outside', 'outside its fixture root')):
    try:
        subprocess.run(['git', 'rev-parse', '--resolve-git-dir', str(target / 'git-probe')],
                       capture_output=True, check=False)
    except AssertionError as error:
        if not str(error).startswith('test writes ' + reason + ': '):
            raise
    else:
        raise AssertionError('the guard accepted a Git target: ' + str(target))
for parent in forbidden:
    target = parent / 'touch-probe'
    try:
        sys.audit('subprocess.Popen', 'touch', ['touch', str(target)], root, None)
    except AssertionError as error:
        if not str(error).startswith('test writes under a live root: '):
            raise
    else:
        raise AssertionError('the guard accepted a touch target: ' + str(target))
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
        scratch.remove_marked(clone)
    assert not clone.exists()
print('standing, workspace, clone, and cleanup stayed in private roots')
import unittest
suite = unittest.defaultTestLoader.loadTestsFromName('scripts.buildlog.test_rust_release')
result = unittest.TextTestRunner(verbosity=1).run(suite)
assert result.wasSuccessful(), 'the guarded buildlog module failed'
print('buildlog fixture stayed in private roots')
'''
            result = subprocess.run([sys.executable, "-c", program, str(root), str(source),
                                     json.dumps([str(parent) for parent in live]), registry],
                                    env=environment, cwd=root, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("cleanup stayed in private roots", result.stdout)
            self.assertIn("buildlog fixture stayed in private roots", result.stdout)
