"""Check the build-log record for abandoned compiler sessions."""

import fcntl
import os
from pathlib import Path
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from typing import override

from scripts.buildlog import store
from scripts.mac_test.abandoned_sessions import removal_script
from scripts.mac_test.floor_claims import MIRROR_LEASE_FDS


FIXTURE_WAIT_SECONDS = 5


class AbandonedSessionTests(unittest.TestCase):
    home_directory: Path = Path()
    mirror: Path = Path()
    target: Path = Path()
    buildlog: Path = Path()
    recorder_status: Path = Path()

    @override
    def setUp(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.home_directory = root / "home with spaces"
        self.mirror = self.home_directory / "mirror"
        self.target = self.mirror / "target's output"
        self.target.mkdir(parents=True)
        self.buildlog = root / "buildlog"
        self.recorder_status = root / "recorder-status"
        recorder = self.home_directory / ".claude/scripts/lib/py"
        recorder.parent.mkdir(parents=True)
        capture_status = (
            "import sys\n"
            "try:\n"
            "    exec(sys.argv.pop(1))\n"
            "except SystemExit as exit_status:\n"
            f"    with open({str(self.recorder_status)!r}, 'a') as status:\n"
            "        status.write(f'{exit_status.code}\\n')\n"
            "    raise\n"
        )
        _ = recorder.write_text(
            "#!/bin/sh\nshift 2\n"
            + f'exec {shlex.quote(sys.executable)} -B -c {shlex.quote(capture_status)} "$@"\n'
        )
        recorder.chmod(0o755)

    def create_session(self, name: str) -> tuple[Path, Path]:
        session = self.target / "debug/incremental/crate" / f"s-{name}-working"
        session.mkdir(parents=True)
        _ = (session / "compiler-output").write_bytes(b"x" * 8192)
        lock = session.with_name(f"s-{name}.lock")
        _ = lock.write_text("compiler lock")
        return session, lock

    def age_session(self, session: Path) -> None:
        modified = time.time() - 600
        os.utime(session, (modified, modified))

    def session_bytes(self, session: Path) -> int:
        measured = subprocess.run(["du", "-sk", str(session)], capture_output=True, text=True, check=True)
        return int(measured.stdout.split(maxsplit=1)[0]) * 1024

    def run_removal(self, *, pass_fds: tuple[int, ...] = (), lease_fds: str = "") -> None:
        target = f'"$HOME"/{shlex.quote(str(self.target.relative_to(self.home_directory)))}'
        path = os.pathsep.join((str(self.home_directory / "bin"), os.environ.get("PATH", os.defpath)))
        result = subprocess.run(
            ["/bin/sh", "-c", removal_script(target, 3)],
            cwd=self.mirror,
            env={"HOME": str(self.home_directory), "PATH": path,
                 MIRROR_LEASE_FDS: lease_fds,
                 "BUILDLOG_DIR": str(self.buildlog), "PYTHONPATH": str(Path(__file__).resolve().parents[2])},
            pass_fds=pass_fds, capture_output=True, text=True, check=False, timeout=FIXTURE_WAIT_SECONDS,
        )
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))

    def rows(self) -> list[dict[str, object]]:
        return [row for path in self.buildlog.glob("*/*.jsonl") for row in store.locked_rows(path)]

    def recorder_result(self) -> str:
        deadline = time.monotonic() + FIXTURE_WAIT_SECONDS
        while time.monotonic() < deadline:
            if self.recorder_status.is_file():
                status = self.recorder_status.read_text()
                if status.endswith("\n"):
                    return status
            time.sleep(0.01)
        self.fail("the recorder did not finish")

    def recorded_rows(self) -> list[dict[str, object]]:
        self.assertEqual(self.recorder_result(), "0\n")
        return self.rows()

    def test_old_sessions_write_one_row_with_their_total_bytes(self) -> None:
        sessions = [self.create_session(name) for name in ("one", "two with spaces")]
        expected_bytes = 0
        for session, _lock in sessions:
            self.age_session(session)
            expected_bytes += self.session_bytes(session)

        self.run_removal()

        for session, lock in sessions:
            self.assertFalse(session.exists())
            self.assertFalse(lock.exists())
        rows = self.recorded_rows()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row["kind"], row["folder"], row["cause"], row["bytes_freed"], row["extent"]),
                         ("sweep_trim", str(self.target), "abandoned session", expected_bytes, "partial"))

    def test_recent_session_writes_no_row_until_it_is_old(self) -> None:
        session, lock = self.create_session("recent")

        self.run_removal()

        self.assertEqual((session / "compiler-output").read_bytes(), b"x" * 8192)
        self.assertEqual(lock.read_text(), "compiler lock")
        self.assertEqual(self.rows(), [])
        self.assertFalse(self.recorder_status.exists())

        self.age_session(session)
        self.run_removal()

        self.assertEqual([row["cause"] for row in self.recorded_rows()], ["abandoned session"])

    def test_failed_record_write_still_removes_the_session(self) -> None:
        session, lock = self.create_session("old")
        self.age_session(session)
        _ = self.buildlog.write_text("block the creation of a build-log directory")

        self.run_removal()

        self.assertFalse(session.exists())
        self.assertFalse(lock.exists())
        self.assertEqual(self.recorder_result(), "1\n")

    def test_held_build_log_lock_does_not_delay_removal(self) -> None:
        session, lock = self.create_session("old")
        self.age_session(session)
        log = self.buildlog / store.host_name() / f"{store.month(time.time())}.jsonl"
        log.parent.mkdir(parents=True)
        with log.open("a+") as held:
            fcntl.flock(held.fileno(), fcntl.LOCK_EX)
            self.run_removal()
            self.assertFalse(session.exists())
            self.assertFalse(lock.exists())
            self.assertEqual(held.read(), "")
            self.assertFalse(self.recorder_status.exists())

        rows = self.recorded_rows()
        self.assertEqual([row["cause"] for row in rows], ["abandoned session"])

    def test_recorder_releases_inherited_lease_while_build_log_is_locked(self) -> None:
        session, _lock = self.create_session("old")
        self.age_session(session)
        log = self.buildlog / store.host_name() / f"{store.month(time.time())}.jsonl"
        log.parent.mkdir(parents=True)
        lease = self.home_directory / "mirror.lease"

        lease_released = False
        with log.open("a+") as held:
            fcntl.flock(held.fileno(), fcntl.LOCK_EX)
            with lease.open("wb") as owner:
                fcntl.flock(owner.fileno(), fcntl.LOCK_EX)
                self.run_removal(pass_fds=(owner.fileno(),))

            with lease.open("r+b") as contender:
                deadline = time.monotonic() + FIXTURE_WAIT_SECONDS
                while time.monotonic() < deadline:
                    try:
                        fcntl.flock(contender.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        time.sleep(0.01)
                    else:
                        lease_released = True
                        break
            self.assertEqual(held.read(), "")

        rows = self.recorded_rows()
        self.assertTrue(lease_released, "the recorder kept the inherited lease")
        self.assertEqual([row["cause"] for row in rows], ["abandoned session"])

    def assert_shim_paths(self, pid: int) -> None:
        lsof = shutil.which("lsof")
        proc = Path(f"/proc/{pid}")
        if lsof is not None:
            result = subprocess.run(
                [lsof, "-n", "-P", "-p", str(pid), "-Ffn"],
                capture_output=True, text=True, check=True, timeout=FIXTURE_WAIT_SECONDS,
            )
            fields = result.stdout.splitlines()
            cwd = [Path(name[1:]) for fd, name in zip(fields, fields[1:]) if fd == "fcwd" and name.startswith("n")]
            self.assertEqual(cwd, [Path("/")])
            open_paths = [Path(field[1:]) for field in fields if field.startswith("n/")]
        elif Path("/proc/self/cwd").is_symlink():
            self.assertEqual((proc / "cwd").readlink(), Path("/"))
            open_paths = [fd.readlink() for fd in (proc / "fd").iterdir()]
        else:
            return

        mirror = self.mirror.resolve()
        self.assertEqual([path for path in open_paths if path.is_absolute() and path.resolve().is_relative_to(mirror)], [])

    def test_recorder_releases_listed_lease_before_python_starts(self) -> None:
        session, _lock = self.create_session("old")
        self.age_session(session)
        lease = self.target / "mirror.lease"
        recorder = self.home_directory / ".claude/scripts/lib/py"
        shim_body = recorder.read_text().removeprefix("#!/bin/sh\n")
        test_socket, shim_socket = socket.socketpair()
        with test_socket, shim_socket:
            shim_fd = shim_socket.fileno()
            _ = recorder.write_text(
                "#!/bin/sh\n"
                + f"printf '%s\\n' \"$$\" >&{shim_fd}\n"
                + f"IFS= read -r resume <&{shim_fd} || exit 99\n"
                + shim_body
            )
            with lease.open("wb") as owner:
                fcntl.flock(owner.fileno(), fcntl.LOCK_EX)
                self.run_removal(pass_fds=(owner.fileno(), shim_fd), lease_fds=f"invalid,,{owner.fileno()},*")
            shim_socket.close()
            test_socket.settimeout(FIXTURE_WAIT_SECONDS)
            with test_socket.makefile("rb") as ready:
                pid = int(ready.readline())
            with lease.open("r+b") as contender:
                try:
                    fcntl.flock(contender.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    lease_released = False
                else:
                    lease_released = True
            self.assert_shim_paths(pid)
            command_line = subprocess.run(
                ["ps", "-ww", "-o", "args=", "-p", str(pid)],
                capture_output=True, text=True, check=True, timeout=FIXTURE_WAIT_SECONDS,
            )
            self.assertNotIn(str(self.mirror), command_line.stdout)
            test_socket.sendall(b"run\n")

        rows = self.recorded_rows()
        self.assertTrue(lease_released, "the shim kept the listed lease before Python started")
        self.assertEqual([(row["cause"], row["folder"]) for row in rows], [("abandoned session", str(self.target))])

    def test_session_bytes_count_when_lock_removal_fails(self) -> None:
        session, lock = self.create_session("old")
        self.age_session(session)
        expected_bytes = self.session_bytes(session)
        rm = shutil.which("rm")
        assert rm is not None
        fixture_rm = self.home_directory / "bin/rm"
        fixture_rm.parent.mkdir()
        _ = fixture_rm.write_text(
            "#!/bin/sh\n"
            + f'[ "$3" = {shlex.quote(str(session))} ] && [ "$4" = {shlex.quote(str(lock))} ] || exit 99\n'
            + f'{shlex.quote(rm)} -rf -- "$3" || exit 99\n'
            + 'printf "%s\\n" "fixture cannot remove the lock" >&2\n'
            + 'exit 1\n'
        )
        fixture_rm.chmod(0o755)

        self.run_removal()

        self.assertFalse(session.exists())
        self.assertEqual(lock.read_text(), "compiler lock")
        rows = self.recorded_rows()
        self.assertEqual([row["bytes_freed"] for row in rows], [expected_bytes])
