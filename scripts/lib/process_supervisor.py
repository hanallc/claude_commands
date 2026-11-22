"""End a managed workload when its owner releases it."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import ExitStack, closing, suppress
import ctypes
import errno
from functools import cache, partial
import json
import os
from pathlib import Path
import select
import signal
import socket
import stat
from struct import Struct
import subprocess
import sys
import threading
import time
from typing import IO, Literal, Protocol, Self, cast, final, overload, runtime_checkable


STARTUP_SECONDS = 30
CLEANUP_SECONDS = 30
_SESSION_REGISTRATION = "DEVTOOLS_SESSION_REGISTRATION"
_CLEANUP_SUCCEEDED = b"K"
_LAUNCH_CANCELLED = b"C"
_LINUX_RESCAN_SECONDS = 0.01
_MAC_RESCAN_SECONDS = 0.05
_MAC_START_TIME = Struct("=120xQQ")
_writer_lock = threading.Lock()
_writers: set[int] = set()


def _close_forked_writers() -> None:
    for descriptor in _writers:
        os.close(descriptor)
    _writers.clear()
    _writer_lock.release()


os.register_at_fork(before=_writer_lock.acquire, after_in_parent=_writer_lock.release,
                    after_in_child=_close_forked_writers)


@runtime_checkable
class _LinuxProcessDescriptors(Protocol):
    def pidfd_open(self, pid: int, /) -> int: ...


@runtime_checkable
class _LinuxProcessSignals(Protocol):
    def pidfd_send_signal(self, descriptor: int, signum: int, /) -> None: ...


def open_linux_process_descriptor(pid: int) -> int:
    """Open a Linux descriptor for this process."""
    if not isinstance(os, _LinuxProcessDescriptors):
        raise OSError(errno.ENOSYS, "Linux process descriptors are unavailable.")
    return os.pidfd_open(pid)


class MacProcessEvent(Protocol):
    @property
    def ident(self) -> int: ...

    @property
    def filter(self) -> int: ...


class MacProcessEventQueue(Protocol):
    def control(self, changes: Sequence[MacProcessEvent] | None, max_events: int,
                timeout: float | None = None, /) -> list[MacProcessEvent]: ...

    def close(self) -> None: ...


@runtime_checkable
class MacProcessEvents(Protocol):
    KQ_FILTER_READ: int
    KQ_FILTER_PROC: int
    KQ_EV_ADD: int
    KQ_EV_ONESHOT: int
    KQ_NOTE_EXIT: int

    def kqueue(self, /) -> MacProcessEventQueue: ...

    def kevent(self, ident: int, /, *, filter: int, flags: int, fflags: int = 0) -> MacProcessEvent: ...


def mac_process_events() -> MacProcessEvents:
    """Get the native events for pipe reads and process exits."""
    if not isinstance(select, MacProcessEvents):
        raise OSError(errno.ENOSYS, "macOS process events are unavailable.")
    return select


@final
class OwnershipWriter:
    """Close the pipe only in the process that opened it."""

    def __init__(self, descriptor: int) -> None:
        self.descriptor = descriptor
        self.owner = os.getpid()

    def close(self) -> None:
        if os.getpid() == self.owner:
            with _writer_lock:
                if self.descriptor >= 0:
                    _writers.remove(self.descriptor)
                    os.close(self.descriptor)
                    self.descriptor = -1


@final
class StatusPipe:
    """Read startup and exit records from the guardian."""

    def __init__(self, descriptor: int) -> None:
        self.descriptor = descriptor
        self.buffer = b""

    def read(self, timeout: float | None) -> str:
        deadline = None if timeout is None else time.monotonic() + timeout
        while b"\n" not in self.buffer:
            remaining = None if deadline is None else max(0, deadline - time.monotonic())
            if not select.select([self.descriptor], [], [], remaining)[0]:
                raise TimeoutError("The guardian did not report a status.")
            data = os.read(self.descriptor, 4096)
            if not data:
                raise ChildProcessError("The guardian closed its status pipe.")
            self.buffer += data
        line, _, self.buffer = self.buffer.partition(b"\n")
        return line.decode()


@final
class ManagedProcess[Output: (str, bytes)]:
    """Expose the process that runs the command and the session that owns it."""

    def __init__(self, command: Sequence[str], guardian: subprocess.Popen[Output], owner: OwnershipWriter,
                 status: StatusPipe, pid: int, *, text: bool) -> None:
        self.pid = pid
        self.session_id = guardian.pid
        self.stdin = guardian.stdin
        self.stdout = guardian.stdout
        self.stderr = guardian.stderr
        self.returncode: int | None = None
        self.args = tuple(command)
        self._guardian = guardian
        self._owner = owner
        self._status = status
        self._text = text
        self._closed = False
        self._output: dict[IO[Output], bytearray] = {}
        self._mac_start = 0
        platform = sys.platform
        if platform == "darwin":
            with suppress(ProcessLookupError):
                self._mac_start = _mac_process_start(pid)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @property
    def status_descriptor(self) -> int:
        """Return the status descriptor while the process owns it."""
        return self._status.descriptor

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is None:
            try:
                self.returncode = int(self._status.read(timeout))
            except TimeoutError as error:
                assert timeout is not None
                raise subprocess.TimeoutExpired(self.args, timeout) from error
        return self.returncode

    def poll(self) -> int | None:
        try:
            return self.wait(0)
        except subprocess.TimeoutExpired:
            return None

    def send_signal(self, signum: int) -> None:
        if self.poll() is None:
            with suppress(ProcessLookupError):
                platform = sys.platform
                if platform == "linux":
                    _signal_linux_member(self.pid, self.session_id, signum)
                elif self._mac_start:
                    _signal_mac_member(self.pid, self.session_id, self._mac_start, signum)

    def send_group_signal(self, signum: int) -> None:
        from scripts.lib.process_group import send_process_group

        if self.poll() is None:
            with suppress(ProcessLookupError):
                if os.getsid(self.pid) == self.session_id:
                    send_process_group(self.pid, signum)

    def kill(self) -> None:
        self.send_signal(signal.SIGKILL)

    def terminate(self) -> None:
        self.send_signal(signal.SIGTERM)

    def communicate(self, *, timeout: float | None = None) -> tuple[Output, Output]:
        """Read the output pipes and wait for the workload."""
        deadline = None if timeout is None else time.monotonic() + timeout
        if self.stdin is not None:
            self.stdin.close()
        streams = [stream for stream in (self.stdout, self.stderr) if stream is not None and not stream.closed]
        while streams:
            remaining = None if deadline is None else max(0, deadline - time.monotonic())
            readable, _, _ = select.select(streams, [], [], remaining)
            if not readable:
                assert timeout is not None
                raise subprocess.TimeoutExpired(self.args, timeout)
            for stream in readable:
                data = os.read(stream.fileno(), 65536)
                if data:
                    self._output.setdefault(stream, bytearray()).extend(data)
                else:
                    stream.close()
                    streams.remove(stream)
        _ = self.wait(None if deadline is None else max(0, deadline - time.monotonic()))
        return self._captured(self.stdout), self._captured(self.stderr)

    def _captured(self, stream: IO[Output] | None) -> Output:
        data = bytes(self._output.get(stream, b"")) if stream is not None else b""
        if self._text:
            encoding = getattr(stream, "encoding", "utf-8")
            return cast(Output, data.decode(encoding).replace("\r\n", "\n").replace("\r", "\n"))
        return cast(Output, data)

    def close(self) -> None:
        """End ownership and wait for the guardian to finish cleanup."""
        if self._closed or os.getpid() != self._owner.owner:
            return
        self._owner.close()
        try:
            status = self._guardian.wait(timeout=CLEANUP_SECONDS)
            if status:
                raise ChildProcessError(f"The guardian exited with status {status}.")
            _ = self.wait(0)
        finally:
            self._closed = True
            os.close(self._status.descriptor)
            _close_streams(self._guardian)


def _close_streams[Output: (str, bytes)](process: subprocess.Popen[Output]) -> None:
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream is not None:
            with suppress(BrokenPipeError):
                stream.close()


def _register_nested_guardian() -> int:
    """Return the nested guardian's result writer, or -1 for a separate session."""
    try:
        descriptor, device, inode = map(int, os.environ[_SESSION_REGISTRATION].split(":"))
        info = os.fstat(descriptor)
    except (KeyError, ValueError, OSError):
        return -1
    if not stat.S_ISSOCK(info.st_mode) or (info.st_dev, info.st_ino) != (device, inode):
        return -1
    with _writer_lock:
        result_read, result_write = os.pipe()
        _writers.add(result_write)
    try:
        with socket.socket(fileno=os.dup(descriptor)) as registrations:
            _ = socket.send_fds(registrations, [b"R"], [result_read])
        return result_write
    except BaseException:
        OwnershipWriter(result_write).close()
        raise
    finally:
        os.close(result_read)


@overload
def launch(command: Sequence[str], *, cwd: Path | None = ..., env: Mapping[str, str] | None = ...,
           stdin: int | IO[bytes] | IO[str] | None = ..., stdout: int | IO[bytes] | IO[str] | None = ...,
           stderr: int | IO[bytes] | IO[str] | None = ..., text: Literal[False] = ...) -> ManagedProcess[bytes]: ...


@overload
def launch(command: Sequence[str], *, cwd: Path | None = ..., env: Mapping[str, str] | None = ...,
           stdin: int | IO[bytes] | IO[str] | None = ..., stdout: int | IO[bytes] | IO[str] | None = ...,
           stderr: int | IO[bytes] | IO[str] | None = ..., text: Literal[True]) -> ManagedProcess[str]: ...


def launch(command: Sequence[str], *, cwd: Path | None = None, env: Mapping[str, str] | None = None,
           stdin: int | IO[bytes] | IO[str] | None = None, stdout: int | IO[bytes] | IO[str] | None = None,
           stderr: int | IO[bytes] | IO[str] | None = None, text: bool = False,
           ) -> ManagedProcess[bytes] | ManagedProcess[str]:
    """Start a guardian and return after it acknowledges the workload."""
    with _writer_lock:
        owner_read, descriptor = os.pipe()
        _writers.add(descriptor)
    owner = OwnershipWriter(descriptor)
    try:
        status_read, status_write = os.pipe()
    except BaseException:
        os.close(owner_read)
        owner.close()
        raise
    try:
        try:
            result_write = _register_nested_guardian() if sys.platform == "darwin" else -1
            try:
                caller_environment = os.environ if env is None else env
                guardian = subprocess.Popen(
                    [sys.executable, "-I", "-S", str(Path(__file__).resolve()),
                     str(owner_read), str(status_write), str(result_write),
                     json.dumps(caller_environment.get("LC_CTYPE")), *command],
                    cwd=cwd, env=env, stdin=stdin, stdout=stdout, stderr=stderr, text=text,
                    pass_fds=(owner_read, status_write) + ((result_write,) if result_write >= 0 else ()),
                    start_new_session=True,
                )
            except (OSError, ValueError, TypeError):
                if result_write >= 0:
                    with suppress(BrokenPipeError):
                        _ = os.write(result_write, _LAUNCH_CANCELLED)
                raise
            finally:
                if result_write >= 0:
                    OwnershipWriter(result_write).close()
        finally:
            os.close(owner_read)
            os.close(status_write)
    except BaseException:
        owner.close()
        os.close(status_read)
        raise
    try:
        status = StatusPipe(status_read)
        ready = status.read(STARTUP_SECONDS)
        if ready.startswith("error "):
            number, message, filename = cast(tuple[int, str, str | None], json.loads(ready.removeprefix("error ")))
            raise OSError(number, message, filename)
        pid = int(ready)
        if text:
            return ManagedProcess(command, cast("subprocess.Popen[str]", guardian), owner, status, pid, text=True)
        return ManagedProcess(command, cast("subprocess.Popen[bytes]", guardian), owner, status, pid, text=False)
    except BaseException:
        owner.close()
        try:
            _ = guardian.wait(timeout=CLEANUP_SECONDS)
        finally:
            os.close(status_read)
            _close_streams(guardian)
        raise


@final
class CleanupDeadline:
    """Limit cleanup from the time the owner pipe closes."""

    def __init__(self) -> None:
        self.end = time.monotonic() + CLEANUP_SECONDS

    def remaining(self) -> float:
        remaining = self.end - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("The guardian cleanup deadline expired.")
        return remaining


def _enable_linux_subreaper() -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    # PR_SET_CHILD_SUBREAPER makes the guardian adopt orphaned descendants.
    if cast(int, libc.prctl(36, 1, 0, 0, 0)) != 0:
        raise OSError(ctypes.get_errno(), "cannot adopt descendants")
    if signal.getsignal(signal.SIGCHLD) == signal.SIG_IGN:
        raise OSError(errno.EINVAL, "The guardian cannot reap children with SIGCHLD ignored.")


def _linux_children() -> set[int]:
    return {int(pid) for task in Path("/proc/self/task").iterdir()
            for pid in (task / "children").read_text().split()}


def _reap_linux_child(child: subprocess.Popen[bytes], status_write: int) -> int:
    pid, status = os.waitpid(-1, os.WNOHANG)
    if pid == child.pid and child.returncode is None:
        child.returncode = os.waitstatus_to_exitcode(status)
        _report(status_write, str(child.returncode))
    return pid


def _clear_linux_children(child: subprocess.Popen[bytes], status_write: int, deadline: CleanupDeadline) -> None:
    while True:
        try:
            while True:
                _ = deadline.remaining()
                if not _reap_linux_child(child, status_write):
                    break
        except ChildProcessError:
            return
        # Only this guardian reaps these children. Their PIDs stay reserved through the signal pass.
        for pid in _linux_children():
            _ = deadline.remaining()
            with suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
        # New descendants can become children after the previous scan.
        _ = select.select([], [], [], min(_LINUX_RESCAN_SECONDS, deadline.remaining()))


def _watch_linux(owner_read: int, status_write: int, child: subprocess.Popen[bytes]) -> None:
    try:
        while True:
            ready = select.select([owner_read], [], [], _LINUX_RESCAN_SECONDS)[0]
            if ready and not os.read(owner_read, 1):
                break
            with suppress(ChildProcessError):
                while _reap_linux_child(child, status_write):
                    if select.select([owner_read], [], [], 0)[0]:
                        break
    finally:
        _clear_linux_children(child, status_write, CleanupDeadline())


def _signal_linux_member(pid: int, session: int, signum: int) -> None:
    if not isinstance(signal, _LinuxProcessSignals):
        raise OSError(errno.ENOSYS, "Linux process descriptor signals are unavailable.")
    descriptor = open_linux_process_descriptor(pid)
    try:
        if os.getsid(pid) == session:
            signal.pidfd_send_signal(descriptor, signum)
    finally:
        os.close(descriptor)


@cache
def _mac_libproc() -> ctypes.CDLL:
    return ctypes.CDLL("/usr/lib/libproc.dylib")


def _mac_process_start(pid: int) -> int:
    info = ctypes.create_string_buffer(_MAC_START_TIME.size)
    # PROC_PIDTBSDINFO returns struct proc_bsdinfo with its start time in the last two fields.
    count = cast(int, _mac_libproc().proc_pidinfo(pid, 3, ctypes.c_uint64(0), info, len(info)))
    if count != len(info):
        raise ProcessLookupError(pid)
    seconds, micros = cast(tuple[int, int], _MAC_START_TIME.unpack(info))
    return seconds * 1_000_000 + micros


def _mac_session_members(session: int, deadline: CleanupDeadline) -> dict[int, int]:
    snapshot = subprocess.check_output(["/bin/ps", "-A", "-o", "pid="], text=True,
                                       timeout=deadline.remaining())
    members: dict[int, int] = {}
    for row in snapshot.splitlines():
        pid = int(row)
        if pid == session:
            continue
        try:
            if os.getsid(pid) != session:
                continue
        except (ProcessLookupError, PermissionError):
            continue
        try:
            members[pid] = _mac_process_start(pid)
        except ProcessLookupError:
            members[pid] = 0
    return members


def _signal_mac_member(pid: int, session: int, start: int, signum: int) -> None:
    # PID reuse before kill requires macOS to wrap its sequential PID counter at 99999.
    if _mac_process_start(pid) == start and os.getsid(pid) == session:
        os.kill(pid, signum)


def _clear_session(session: int, child: subprocess.Popen[bytes], deadline: CleanupDeadline) -> None:
    rescan_until = time.monotonic() + _MAC_RESCAN_SECONDS
    first_scan = True
    while True:
        members = _mac_session_members(session, deadline)
        # Bounded rescans narrow the macOS fork race without closing it.
        if not members and not first_scan and time.monotonic() >= rescan_until:
            return
        first_scan = False
        for pid, start in members.items():
            _ = deadline.remaining()
            with suppress(ProcessLookupError):
                _signal_mac_member(pid, session, start, signal.SIGKILL)
        _ = child.poll()
        time.sleep(min(0.01, deadline.remaining()))


def _report(descriptor: int, line: str) -> None:
    with suppress(BrokenPipeError):
        _ = os.write(descriptor, (line + "\n").encode())


def _receive_registration(registrations: socket.socket) -> int:
    packet = socket.recv_fds(registrations, 1, 1)
    message, descriptors = packet[0], packet[1]
    if message != b"R" or len(descriptors) != 1:
        for descriptor in descriptors:
            os.close(descriptor)
        raise ChildProcessError("Invalid nested guardian registration.")
    descriptor, = descriptors
    os.set_inheritable(descriptor, False)
    return descriptor


def _read_cleanup_result(descriptor: int, deadline: CleanupDeadline) -> bytes:
    if not select.select([descriptor], [], [], deadline.remaining())[0]:
        raise TimeoutError("A nested guardian did not finish before the cleanup deadline.")
    return os.read(descriptor, 1)


def _confirm_nested_cleanup(registrations: socket.socket, nested_results: list[int],
                            deadline: CleanupDeadline) -> None:
    with suppress(BlockingIOError):
        while True:
            _ = deadline.remaining()
            nested_results.append(_receive_registration(registrations))
    failed = False
    for descriptor in nested_results:
        result = _read_cleanup_result(descriptor, deadline)
        failed |= result not in (_CLEANUP_SUCCEEDED, _LAUNCH_CANCELLED) or bool(_read_cleanup_result(descriptor, deadline))
    if failed:
        raise ChildProcessError("A nested guardian did not report successful cleanup.")


def _watch_mac(owner_read: int, status_write: int, child: subprocess.Popen[bytes],
               registrations: socket.socket) -> None:
    reported = False
    nested_results: list[int] = []
    try:
        source = mac_process_events()
        with closing(source.kqueue()) as events:
            read_events = [source.kevent(descriptor, filter=source.KQ_FILTER_READ, flags=source.KQ_EV_ADD)
                           for descriptor in (owner_read, registrations.fileno())]
            _ = events.control(read_events, 0)
            exit_event = source.kevent(child.pid, filter=source.KQ_FILTER_PROC,
                flags=source.KQ_EV_ADD | source.KQ_EV_ONESHOT, fflags=source.KQ_NOTE_EXIT)
            try:
                _ = events.control([exit_event], 0)
            except ProcessLookupError:
                status = child.wait()
            else:
                # Check once after registration for an exit during setup.
                status = child.poll()
            if status is not None:
                _report(status_write, str(status))
                reported = True
            while True:
                ready = events.control(None, 3)
                if any(event.filter == source.KQ_FILTER_READ and event.ident == owner_read for event in ready):
                    if not os.read(owner_read, 1):
                        break
                for event in ready:
                    if event.filter == source.KQ_FILTER_PROC:
                        if not reported:
                            _report(status_write, str(child.wait()))
                            reported = True
                    elif event.ident == registrations.fileno():
                        nested_results.append(_receive_registration(registrations))
    finally:
        deadline = CleanupDeadline()
        try:
            _clear_session(os.getsid(0), child, deadline)
            status = child.wait(timeout=deadline.remaining())
            if not reported:
                _report(status_write, str(status))
            _confirm_nested_cleanup(registrations, nested_results, deadline)
        finally:
            for descriptor in nested_results:
                os.close(descriptor)


def _supervise(owner_read: int, status_write: int, command: Sequence[str], environment: dict[str, str]) -> int:
    """Watch the owner pipe until the workload can be removed."""
    if select.select([owner_read], [], [], 0)[0] and not os.read(owner_read, 1):
        return 0
    with ExitStack() as descriptors:
        try:
            platform = sys.platform
            if platform == "linux":
                _enable_linux_subreaper()
                child = subprocess.Popen(command, env=environment, process_group=0)
                watch = partial(_watch_linux, owner_read, status_write, child)
            else:
                registrations, register = socket.socketpair(type=socket.SOCK_DGRAM)
                _ = descriptors.enter_context(registrations)
                # Keep the peer open so macOS can drain registrations after the workload exits.
                _ = descriptors.enter_context(register)
                registrations.setblocking(False)
                descriptor = register.fileno()
                info = os.fstat(descriptor)
                environment[_SESSION_REGISTRATION] = f"{descriptor}:{info.st_dev}:{info.st_ino}"
                child = subprocess.Popen(command, env=environment, pass_fds=(descriptor,), process_group=0)
                watch = partial(_watch_mac, owner_read, status_write, child, registrations)
        except OSError as error:
            _report(status_write, "error " + json.dumps([error.errno or errno.EIO, error.strerror, cast(object, error.filename)]))
            return 0
        # Keep the guardian from delaying workload stream EOF.
        for descriptor in (0, 1, 2):
            os.close(descriptor)
        _report(status_write, str(child.pid))
        watch()
    return 0


def supervise(owner_read: int, status_write: int, result_write: int, launch_arguments: Sequence[str]) -> int:
    """Report the result of this guardian's cleanup to its parent guardian."""
    encoded_lc_ctype, *command = launch_arguments
    lc_ctype = cast(str | None, json.loads(encoded_lc_ctype))
    # Python startup can add or replace LC_CTYPE. Restore the caller's value for the workload.
    environment = dict(os.environ)
    if lc_ctype is None:
        _ = environment.pop("LC_CTYPE", None)
    else:
        environment["LC_CTYPE"] = lc_ctype
    if result_write < 0:
        return _supervise(owner_read, status_write, command, environment)
    os.set_inheritable(result_write, False)
    result = b"E"
    try:
        status = _supervise(owner_read, status_write, command, environment)
        result = _CLEANUP_SUCCEEDED
        return status
    finally:
        # Leave the writer open until the guardian exits.
        with suppress(BrokenPipeError):
            _ = os.write(result_write, result)


if __name__ == "__main__":
    raise SystemExit(supervise(int(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3]), sys.argv[4:]))
