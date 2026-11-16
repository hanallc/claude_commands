#!/usr/bin/env python3
"""Hold measured app memory until the app and its scope processes end."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
from dataclasses import dataclass, field
from datetime import UTC, datetime
import os
from pathlib import Path
import subprocess
import signal
import sys
import time
import uuid
from typing import override

from scripts.build_queue import admission_client as client, admission_protocol as wire, admission_records as records, queue_place
from scripts.buildlog import store
from scripts.lib.atomic_file import write_text_atomic
from scripts.lint import memory_admit as gate


@dataclass(frozen=True)
class AppHold:
    """The admitted app owns its scope, peak file, and release token."""
    grant: wire.Admitted
    scope: Path
    sidecar: Path


@dataclass
class AppWaitOutput(client.WaitOutput):
    """Publish queue reasons for the command that waits for this app."""
    status_file: Path = field(default_factory=Path)

    @override
    def status(self, text: str) -> None:
        write_text_atomic(self.status_file, text + "\n")
        super().status(text)


def wait_app(process: subprocess.Popen[bytes], hold: AppHold, interval: float) -> int:
    """Preserve the scope peak while the app runs."""
    while True:
        gate.sample_app_peak(hold.scope, Path(hold.grant.max_path))
        try:
            return process.wait(timeout=interval)
        except subprocess.TimeoutExpired:
            pass


def start_peak_slice(name: str) -> None:
    """Keep peak counters after the app scope disappears."""
    _ = subprocess.run(["busctl", "--user", "call", "org.freedesktop.systemd1",
        "/org/freedesktop/systemd1", "org.freedesktop.systemd1.Manager", "StartTransientUnit",
        "ssa(sv)a(sa(sv))", name, "fail", "2", "MemoryAccounting", "b", "true",
        "StopWhenUnneeded", "b", "false", "0"], check=True, capture_output=True, text=True)


def stop_app_members(scope: Path) -> None:
    """Stop app descendants without stopping their supervisor."""
    for pid in gate.scope_members(scope) - {os.getpid()}:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


@dataclass(frozen=True)
class NoAppLauncher:
    """The caller has no owned launcher process."""


NO_APP_LAUNCHER = NoAppLauncher()


def stop_app_scope(scope_file: Path, grace_seconds: float, *,
                   fallback_launcher: subprocess.Popen[bytes] | NoAppLauncher = NO_APP_LAUNCHER) -> None:
    """Stop every app member; the service saves final peaks and retires the slice."""
    launchers: list[wire.Owner | subprocess.Popen[bytes]] = []
    try:
        pid, start = scope_file.with_suffix(".owner").read_text().split()
        launchers.append(wire.Owner(int(pid), start))
    except FileNotFoundError:
        if not isinstance(fallback_launcher, NoAppLauncher):
            launchers.append(fallback_launcher)
    for signal_number in (signal.SIGTERM, signal.SIGKILL):
        for launcher in launchers:
            try:
                if isinstance(launcher, wire.Owner):
                    if records.process_start_time(launcher.pid) == launcher.start_time:
                        os.kill(launcher.pid, signal_number)
                elif launcher.poll() is None:
                    launcher.send_signal(signal_number)
            except ProcessLookupError:
                pass
        if signal_number == signal.SIGTERM:
            try:
                scope = Path(scope_file.read_text().strip())
            except FileNotFoundError:
                if not launchers:
                    return
            else:
                for pid in gate.scope_members(scope):
                    try:
                        os.kill(pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
            time.sleep(grace_seconds)
    # The launcher can publish the scope during the grace period.
    try:
        scope = Path(scope_file.read_text().strip())
        _ = (scope / "cgroup.kill").write_text("1")
    except FileNotFoundError:
        pass
    # Keep the durable record and counters until the service records the final peak.


def stop_main(arguments: list[str]) -> int:
    """Stop a scope from a shell cleanup command."""
    parser = argparse.ArgumentParser(description="Stop the saved app scope")
    _ = parser.add_argument("--scope-file", type=Path, required=True)
    _ = parser.add_argument("--grace", type=float, required=True)
    args = parser.parse_args(arguments, namespace=StopArguments())
    stop_app_scope(args.scope_file, args.grace)
    return 0


def command(binary: Path, repository: Path, purpose: str, owner: str, pid_file: Path,
            arguments: list[str]) -> list[str]:
    """Keep the caller environment command inside the admitted app scope."""
    return [sys.executable, str(Path(__file__).resolve()), "--binary", str(binary),
            "--repo", str(repository), "--purpose", purpose, "--owner", owner,
            "--pid-file", str(pid_file), "--", *arguments]


def current_scope() -> Path:
    """Read the dedicated scope that systemd assigned to this app supervisor."""
    for line in Path("/proc/self/cgroup").read_text().splitlines():
        if line.startswith("0::"):
            return Path("/sys/fs/cgroup") / line[3:].lstrip("/")
    raise RuntimeError("app_gate: the app has no cgroup v2 scope")


def supervise(binary: Path, repository: Path, purpose: str, owner_name: str, pid_file: Path,
              arguments: list[str], scope: Path) -> int:
    """Keep admission independent of the command that requested the app launch."""
    pid = os.getpid()
    start = records.process_start_time(pid)
    if start is None:
        raise RuntimeError("app_gate: the supervisor has no process identity")
    owner = wire.Owner(pid, start)
    episode, since = uuid.uuid4().hex, time.time()
    directory = store.root() / "admission/app-scopes"
    directory.mkdir(parents=True, exist_ok=True)
    sidecar = directory / (scope.name + ".cgroup")
    write_text_atomic(sidecar, str(scope) + "\n")
    identity = client.step_identity(f"app · {binary.name} · {purpose}", str(repository))
    identity = wire.Identity(identity.session_id, owner_name or identity.name, identity.step,
                             identity.folder, identity.unit)

    def refresh() -> wire.AdmitRequest:
        profile = gate.app_profile(binary, repository, datetime.now(UTC), store.root())
        return wire.AdmitRequest(episode, owner, tuple(arguments), str(repository), since,
            str(sidecar), identity, records.NoCallRecord(), records.NoTestWidth(), profile, False)

    interval = records.read_memory_config().program_sample_seconds
    output = AppWaitOutput(status_file=pid_file.with_suffix(".status"))
    output.status("app_gate: waiting for memory admission")
    try:
        reply = queue_place.request_place(queue_place.RefreshedRequest(refresh), output)
    except wire.AdmissionUnavailable as error:
        gate.retire_app_scope(sidecar)
        output.status(f"app_gate: {error}")
        return 2
    if not isinstance(reply, wire.Admitted):
        gate.retire_app_scope(sidecar)
        reason = reply.reason if isinstance(reply, wire.Refused) else "left the admission queue"
        output.status(f"app_gate: {reason}")
        return 2
    output.finished()
    hold = AppHold(reply, scope, sidecar)
    status = 1
    publication_failed = False
    try:
        process = subprocess.Popen(arguments, cwd=repository)
        try:
            app_start = records.process_start_time(process.pid)
            if app_start is None:
                raise OSError("the app exited before its identity was published")
            write_text_atomic(pid_file.with_suffix(".start"), app_start + "\n")
            write_text_atomic(pid_file, str(process.pid) + "\n")
        except OSError as error:
            publication_failed = True
            process.kill()
            stop_app_members(scope)
            try:
                output.status(f"app_gate: {error}")
            except OSError:
                print(f"app_gate: {error}", file=sys.stderr, flush=True)
            raise
        finally:
            result = wait_app(process, hold, interval)
        status = result
        return status
    finally:
        # Retain the hold after errors until every app descendant ends.
        while gate.scope_members(scope) - {pid}:
            if publication_failed:
                stop_app_members(scope)
            gate.sample_app_peak(hold.scope, Path(hold.grant.max_path))
            time.sleep(interval)
        gate.sample_app_peak(hold.scope, Path(hold.grant.max_path))
        _ = queue_place.release_place(hold.grant.reservation, status, hold.grant.cleanup_timing)
        gate.retire_app_scope(hold.sidecar)


class StopArguments(argparse.Namespace):
    scope_file: Path = Path()
    grace: float = 0.0


class AppArguments(argparse.Namespace):
    binary: Path = Path()
    repo: Path = Path()
    purpose: str = ""
    owner: str = ""
    pid_file: Path = Path()
    in_scope: bool = False
    command: list[str] = []


def main(arguments: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if arguments is None else arguments
    if arguments[:1] == ["stop"]:
        return stop_main(arguments[1:])
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--binary", type=Path, required=True)
    _ = parser.add_argument("--repo", type=Path, required=True)
    _ = parser.add_argument("--purpose", required=True)
    _ = parser.add_argument("--owner", default="")
    _ = parser.add_argument("--pid-file", type=Path, required=True)
    _ = parser.add_argument("--in-scope", action="store_true")
    _ = parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(arguments, namespace=AppArguments())
    binary, repository = Path(args.binary).resolve(), Path(args.repo).resolve()
    argv = list(args.command)
    if argv[:1] == ["--"]:
        argv = argv[1:]
    if not argv:
        argv = [str(binary)]
    if not args.in_scope:
        wrapped = command(binary, repository, args.purpose, args.owner, Path(args.pid_file), argv)
        wrapped.insert(2, "--in-scope")
        app_id = uuid.uuid4().hex
        peak_slice = "builds-app" + app_id + ".slice"
        scope = gate.builds_slice_cgroup() / peak_slice / ("app-" + app_id + ".scope")
        directory = store.root() / "admission/app-scopes"
        directory.mkdir(parents=True, exist_ok=True)
        sidecar = directory / (scope.name + ".cgroup")
        start = records.process_start_time(os.getpid())
        if start is None:
            raise RuntimeError("app_gate: the scope launcher has no process identity")
        write_text_atomic(args.pid_file.with_suffix(".owner"), f"{os.getpid()} {start}\n")
        write_text_atomic(sidecar.with_suffix(".owner"), f"{os.getpid()} {start}\n")
        write_text_atomic(sidecar, str(scope) + "\n")
        write_text_atomic(args.pid_file.with_suffix(".cgroup"), str(scope) + "\n")
        start_peak_slice(peak_slice)
        os.execvp("systemd-run", ["systemd-run", "--user", "--scope", "--quiet", "--collect",
            "--slice=" + peak_slice, "--unit=app-" + app_id, *wrapped])
    return supervise(binary, repository, args.purpose, args.owner, Path(args.pid_file), argv, current_scope())


if __name__ == "__main__":
    raise SystemExit(main())
