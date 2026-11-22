"""Test private account folders and the staff owner fixture."""
from __future__ import annotations

import json
import os
from pathlib import Path
import select
import signal
import socket
import subprocess
import sys
import tempfile
import textwrap
import unittest
from collections.abc import Callable, Generator
from contextlib import contextmanager, suppress
from typing import TextIO, cast, final, override

from scripts.lib import process_group
from scripts.lint.memory_test_support import config_environment


CHECKOUT = Path(__file__).resolve().parents[2]
OWNER_EXIT_SECONDS = 5
OWNER_START_SECONDS = 20
OWNER_CLOSE_OBSERVE_SECONDS = 1
OWNER_PARENT = '''
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch
from scripts.production.staff_test_support import SharedStaffOwner

wait_for_owner = subprocess.Popen.wait

def report_close(process, *args, **kwargs):
    if kwargs.get("timeout") is not None:
        print("pipe closed", flush=True)
    return wait_for_owner(process, *args, **kwargs)

owner = SharedStaffOwner(Path(sys.argv[1]))
try:
    port = owner.port
    keeper_pid = 0
    if sys.argv[2] == "1":
        keeper_pid = os.fork()
        if keeper_pid == 0:
            os.close(sys.stdout.fileno())
            try:
                sys.stdin.read()
            finally:
                os._exit(0)
    print(port, owner.pid, keeper_pid, flush=True)
    sys.stdin.read()
finally:
    with patch.object(subprocess.Popen, "wait", report_close):
        owner.close()
'''
BLOCKED_REQUEST = '''
import os
from pathlib import Path
import socket
import sys

with socket.create_connection(("127.0.0.1", int(sys.argv[1]))) as connection:
    connection.sendall(f"{os.getpid()}\\n".encode())
    if connection.recv(1) == b"w":
        Path(sys.argv[2]).write_text("accepted request\\n")
        print("fixture request")
'''


@contextmanager
def watch_exit(pid: int) -> Generator[Callable[[float], bool]]:
    """Watch the process before sending a signal."""
    if sys.platform == "darwin":
        queue = select.kqueue()
        try:
            _ = queue.control([select.kevent(pid, select.KQ_FILTER_PROC,
                               select.KQ_EV_ADD | select.KQ_EV_ONESHOT, select.KQ_NOTE_EXIT)], 0)
            yield lambda timeout: bool(queue.control(None, 1, timeout))
        finally:
            queue.close()
        return
    descriptor = os.pidfd_open(pid)
    try:
        yield lambda timeout: bool(select.select([descriptor], [], [], timeout)[0])
    finally:
        os.close(descriptor)


@final
class SharedStaffOwnerTests(unittest.TestCase):
    root: Path = Path()
    environment: dict[str, str] = {}

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.environment = {
            **config_environment(self.root / "config"),
            "HOME": str(self.root),
            "PATH": os.defpath,
            "STAFF_CONFIG": str(self.root / "config/staff.conf"),
            "STAFF_FILE": str(self.root / "staff.json"),
            "STAFF_TEST_CHECKOUT": str(CHECKOUT),
            "STAFF_TEST_OWNER_FILE": str(self.root / "staff.json"),
            "STAFF_TEST_OWNER_SOURCE": str(self.root / "sources"),
            "TMPDIR": str(self.root),
        }

    def parent_line(self, parent: subprocess.Popen[str]) -> str:
        output = cast(TextIO, parent.stdout)
        self.assertTrue(select.select([output], [], [], OWNER_START_SECONDS)[0],
                        "The parent did not report its state")
        return output.readline().strip()

    @contextmanager
    def owner(self, *, fork_writer: bool = False) -> Generator[tuple[subprocess.Popen[str], int, int, int]]:
        command = [sys.executable, "-c", OWNER_PARENT, str(self.root), str(int(fork_writer))]
        with subprocess.Popen(command, cwd=CHECKOUT, env=self.environment, stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, text=True) as parent:
            owner_pid = None
            try:
                port, owner_pid, keeper_pid = map(int, self.parent_line(parent).split())
                self.assertGreater(owner_pid, 1)
                yield parent, port, owner_pid, keeper_pid
            except BaseException:
                if owner_pid is not None and owner_pid > 1:
                    with suppress(ProcessLookupError):
                        process_group.send_process_group(owner_pid, signal.SIGKILL)
                raise
            finally:
                if parent.returncode is None:
                    parent.kill()

    @contextmanager
    def request(self, port: int, script: Path, *arguments: str) -> Generator[socket.socket]:
        payload = dict(arguments=[str(script), *arguments], environment=self.environment,
                       directory=str(self.root), stdin="")
        with socket.create_connection(("127.0.0.1", port), timeout=OWNER_START_SECONDS) as connection:
            connection.sendall(json.dumps(payload).encode() + b"\n")
            yield connection

    @contextmanager
    def blocked_request(self, port: int) -> Generator[tuple[socket.socket, socket.socket, int, Path]]:
        script = self.root / "blocked-request.py"
        _ = script.write_text(BLOCKED_REQUEST)
        written = self.root / "accepted.txt"
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            listener.settimeout(OWNER_START_SECONDS)
            notice_port = cast(tuple[str, int], listener.getsockname())[1]
            with self.request(port, script, str(notice_port), str(written)) as connection:
                peer = listener.accept()[0]
                with peer, peer.makefile("rb") as channel:
                    peer.settimeout(OWNER_START_SECONDS)
                    yield connection, peer, int(channel.readline()), written

    def test_killed_parent_stops_owner_and_active_request(self) -> None:
        with self.owner() as (parent, port, owner_pid, _), self.blocked_request(port) as (_, _, request_pid, _):
            with watch_exit(owner_pid) as owner_exited, watch_exit(request_pid) as request_exited:
                parent.kill()
                self.assertEqual(parent.wait(timeout=OWNER_EXIT_SECONDS), -signal.SIGKILL)
                self.assertTrue(owner_exited(OWNER_EXIT_SECONDS), "The staff owner survived its parent")
                self.assertTrue(request_exited(OWNER_EXIT_SECONDS), "The request survived the staff owner")

    def test_killed_parent_stops_owner_with_inherited_writer(self) -> None:
        with self.owner(fork_writer=True) as (parent, _, owner_pid, keeper_pid):
            self.assertGreater(keeper_pid, 1)
            with watch_exit(owner_pid) as owner_exited, watch_exit(keeper_pid) as keeper_exited:
                parent.kill()
                self.assertEqual(parent.wait(timeout=OWNER_EXIT_SECONDS), -signal.SIGKILL)
                self.assertTrue(owner_exited(OWNER_EXIT_SECONDS), "The inherited writer kept the staff owner alive")
                self.assertFalse(keeper_exited(0), "The forked child did not keep the pipe open")
                cast(TextIO, parent.stdin).close()
                self.assertTrue(keeper_exited(OWNER_EXIT_SECONDS), "The forked child survived fixture cleanup")

    def test_close_lets_an_accepted_request_finish(self) -> None:
        with self.owner() as (parent, port, owner_pid, _):
            with self.blocked_request(port) as (connection, peer, request_pid, written):
                with watch_exit(owner_pid) as owner_exited, watch_exit(request_pid) as request_exited:
                    cast(TextIO, parent.stdin).close()
                    self.assertEqual(self.parent_line(parent), "pipe closed")
                    self.assertFalse(request_exited(OWNER_CLOSE_OBSERVE_SECONDS),
                                     "Close ended the accepted request")
                    peer.sendall(b"w")
                    with connection.makefile("rb") as channel:
                        result = cast(dict[str, object], json.loads(channel.readline()))
                    self.assertEqual(result["status"], 0, result)
                    self.assertEqual(written.read_text(), "accepted request\n")
                    self.assertEqual(parent.wait(timeout=OWNER_EXIT_SECONDS), 0)
                    self.assertTrue(owner_exited(OWNER_EXIT_SECONDS), "The staff owner survived close()")

    def test_close_stops_owner_after_a_request(self) -> None:
        script = self.root / "echo-request.py"
        _ = script.write_text('print("fixture request")\n')
        with self.owner() as (parent, port, owner_pid, _), watch_exit(owner_pid) as owner_exited:
            with self.request(port, script) as connection, connection.makefile("rb") as channel:
                result = cast(dict[str, object], json.loads(channel.readline()))
                self.assertEqual(result, {"status": 0, "stdout": "fixture request\n", "stderr": ""})
            _ = parent.communicate(timeout=OWNER_EXIT_SECONDS)
            self.assertEqual(parent.returncode, 0)
            self.assertTrue(owner_exited(OWNER_EXIT_SECONDS), "The staff owner survived close()")


PREPARE = '''
import os
from pathlib import Path
import pwd
import subprocess
import sys
import unittest
from unittest.mock import patch

root = Path(sys.argv[1])
family, selection = sys.argv[2:]
login_home = root / "login"
login_home.mkdir()
real_claude = login_home / (".claude" if selection == "default" else ".claude-live")
real_codex = login_home / (".codex" if selection == "default" else ".codex-live")
real_claude.mkdir()
real_codex.mkdir()
home = login_home if selection == "default" else root / "environment-home"
home.mkdir(exist_ok=True)
claude = real_claude if selection == "real" else home / "claude"
codex = real_codex if selection == "real" else home / "codex"
os.environ.update(HOME=str(home), CLAUDE_CONFIG_DIR=str(claude), CODEX_HOME=str(codex),
                  STAFF_FILE=str(root / "staff.json"))
if selection == "default":
    del os.environ["CLAUDE_CONFIG_DIR"]
    del os.environ["CODEX_HOME"]
login = pwd.struct_passwd(("fixture", "x", os.getuid(), os.getgid(), "", str(login_home), "/bin/sh"))
with patch.object(pwd, "getpwuid", return_value=login):
    from scripts.production import staff_test_support as fixtures
from scripts.production import staff, session_staff
from scripts.message import sessions
case = unittest.TestCase()
CHILD_READERS = """import os, sys
from pathlib import Path
from scripts.production import staff, session_staff
from scripts.message import sessions
row = staff.by_id(staff.StaffId(sys.argv[1]))
family, folder = sys.argv[2:]
assert os.environ['CLAUDE_CONFIG_DIR' if family == 'claude' else 'CODEX_HOME'] == folder
assert session_staff.worker_family(staff.local_snapshot(), row.id) == family
backend = staff.backend(row)
assert backend.family == family
paths = sessions.transcript_locations(row.current_session) if family == 'claude' else list(
    (backend.codex_home / 'sessions').glob(f'{row.current_session}.jsonl'))
assert len(paths) == 1, paths
assert paths[0].is_relative_to(Path(folder)), paths
assert paths[0].read_bytes() == b''
"""
parent = fixtures.unit_membership("fixture-showrunner", "fixture-unit")
'''


class FixtureAccountTests(unittest.TestCase):
    def run_fixture(self, body: str, family: str, selection: str = "real") -> None:
        with tempfile.TemporaryDirectory() as temporary:
            environment = {key: value for key, value in os.environ.items()
                           if not key.startswith("STAFF_ROW_FD_") and key not in (
                               "STAFF_OPERATION_FD", "PYTHONUNBUFFERED", "REFUSAL_WATCH_INVENTORY")}
            result = subprocess.run(
                [sys.executable, "-c", textwrap.dedent(PREPARE) + textwrap.dedent(body),
                 temporary, family, selection],
                cwd=CHECKOUT, env=environment, capture_output=True, text=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_import_redirects_account_folders_and_preserves_private_selections(self) -> None:
        for family in ("claude", "codex"):
            for selection in ("real", "default", "private"):
                with self.subTest(family=family, selection=selection):
                    self.run_fixture('''
                        selected = Path(os.environ["CLAUDE_CONFIG_DIR" if family == "claude" else "CODEX_HOME"])
                        original = claude if family == "claude" else codex
                        if selection != "private":
                            case.assertFalse(selected.is_relative_to(login_home))
                        else:
                            case.assertEqual(selected, original)
                        backend = staff.ClaudeBackend() if family == "claude" else staff.CodexBackend(selected)
                        row = fixtures.fixture_worker("worker", parent.id, backend=backend)
                        files = list(selected.rglob(f"{row.current_session}.jsonl"))
                        case.assertEqual(len(files), 1)
                        case.assertEqual(files[0].read_bytes(), b"")
                        case.assertEqual(list(real_claude.iterdir()), [])
                        case.assertEqual(list(real_codex.iterdir()), [])
                    ''', family, selection)

    def test_account_path_aliases_redirect_before_worker_publication(self) -> None:
        for family in ("claude", "codex"):
            for alias in ("parent", "relative", "symlink", "account-symlink"):
                with self.subTest(family=family, alias=alias):
                    self.run_fixture(f"alias = {alias!r}\n" + textwrap.dedent('''
                        real_home = real_claude if family == "claude" else real_codex
                        if alias == "parent":
                            selected = login_home / ".." / login_home.name / real_home.name
                        elif alias == "relative":
                            os.chdir(root)
                            selected = Path("login") / real_home.name
                        elif alias == "symlink":
                            selected = root / "account-alias"
                            selected.symlink_to(real_home, target_is_directory=True)
                        else:
                            target = root / "account-target"
                            real_home.rename(target)
                            real_home.symlink_to(target, target_is_directory=True)
                            selected = real_home
                        variable = "CLAUDE_CONFIG_DIR" if family == "claude" else "CODEX_HOME"
                        with patch.dict(os.environ, {variable: str(selected)}):
                            backend = staff.ClaudeBackend() if family == "claude" else staff.CodexBackend(selected)
                            row = fixtures.fixture_worker("alias-worker", parent.id, backend=backend)
                            private_home = Path(os.environ[variable])
                            case.assertEqual(private_home.name, real_home.name)
                            case.assertFalse(private_home.is_relative_to(login_home))
                            observed = staff.backend(row)
                            case.assertEqual(observed.family, family)
                            paths = sessions.transcript_locations(row.current_session) if family == "claude" else list(
                                (observed.codex_home / "sessions").glob(f"{row.current_session}.jsonl"))
                            case.assertEqual(len(paths), 1)
                            case.assertTrue(paths[0].is_relative_to(private_home))
                            case.assertEqual(paths[0].read_bytes(), b"")
                        case.assertEqual(list(real_claude.iterdir()), [])
                        case.assertEqual(list(real_codex.iterdir()), [])
                    '''), family, "private")

    def test_claude_file_creation_updates_the_environment_only_after_redirecting(self) -> None:
        for creator in ("session", "transcript"):
            for selection in ("real", "private"):
                with self.subTest(creator=creator, selection=selection):
                    self.run_fixture(f"creator = {creator!r}\n" + textwrap.dedent('''
                        row = fixtures.fixture_worker("reader-worker", parent.id)
                        environment = {"HOME": str(login_home if selection == "real" else home),
                                       "STAFF_FILE": str(root / "staff.json")}
                        with patch.dict(os.environ, environment, clear=True):
                            before = dict(os.environ)
                            path = fixtures._fixture_session_path(row.current_session, staff.ClaudeBackend())
                            case.assertEqual(dict(os.environ), before)
                            if creator == "session":
                                fixtures.fixture_session(row.current_session, staff.ClaudeBackend())
                            else:
                                written, evidence = fixtures.transcript_fixture(row, staff.ClaudeBackend())
                                case.assertEqual(written, path)
                                case.assertIn(evidence[0][0], written.read_text())
                            if selection == "real":
                                case.assertEqual(os.environ["CLAUDE_CONFIG_DIR"], str(path.parents[2]))
                                case.assertFalse(path.is_relative_to(login_home))
                            else:
                                case.assertNotIn("CLAUDE_CONFIG_DIR", os.environ)
                            case.assertEqual(sessions.transcript_locations(row.current_session), [path])
                        case.assertEqual(list(real_claude.iterdir()), [])
                        case.assertEqual(list(real_codex.iterdir()), [])
                    '''), "claude", selection)

    def test_late_environment_and_explicit_codex_home_redirect_before_worker_publication(self) -> None:
        for family in ("claude", "codex"):
            with self.subTest(family=family):
                self.run_fixture('''
                    environment = {"HOME": str(login_home), "STAFF_FILE": str(root / "staff.json"),
                                   "CLAUDE_CONFIG_DIR": str(real_claude), "CODEX_HOME": str(real_codex)}
                    with patch.dict(os.environ, environment, clear=True):
                        backend = staff.ClaudeBackend() if family == "claude" else staff.CodexBackend(real_codex)
                        row = fixtures.fixture_worker("late-worker", parent.id, backend=backend)
                        selected = Path(os.environ["CLAUDE_CONFIG_DIR" if family == "claude" else "CODEX_HOME"])
                        case.assertFalse(selected.is_relative_to(login_home))
                        case.assertEqual(len(list(selected.rglob(f"{row.current_session}.jsonl"))), 1)
                        case.assertEqual(list(real_claude.iterdir()), [])
                        case.assertEqual(list(real_codex.iterdir()), [])
                        case.assertEqual(session_staff.worker_family(staff.local_snapshot(), row.id), family)
                ''', family)

    def test_readers_and_child_process_find_the_parent_worker_in_the_private_folder(self) -> None:
        for family in ("claude", "codex"):
            with self.subTest(family=family):
                self.run_fixture('''
                    backend = staff.ClaudeBackend() if family == "claude" else staff.CodexBackend(real_codex)
                    row = fixtures.fixture_worker("visible-worker", parent.id, backend=backend)
                    selected = Path(os.environ["CLAUDE_CONFIG_DIR" if family == "claude" else "CODEX_HOME"])
                    case.assertFalse(selected.is_relative_to(login_home))
                    case.assertEqual(session_staff.worker_family(staff.local_snapshot(), row.id), family)
                    observed = staff.backend(row)
                    case.assertEqual(observed.family, family)
                    if family == "claude":
                        paths = sessions.transcript_locations(row.current_session)
                        case.assertEqual(len(paths), 1)
                        case.assertTrue(paths[0].is_relative_to(selected))
                    else:
                        case.assertEqual(observed.codex_home, selected)
                    child = subprocess.run([sys.executable, "-c", CHILD_READERS, row.id, family, str(selected)], capture_output=True, text=True, check=False)
                    case.assertEqual(child.returncode, 0, child.stdout + child.stderr)
                    case.assertEqual(list(real_claude.iterdir()), [])
                    case.assertEqual(list(real_codex.iterdir()), [])
                ''', family)
