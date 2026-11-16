#!/usr/bin/env python3
"""Hold measured app memory until the app and its scope processes end."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
import os
import hashlib
import json
from pathlib import Path
import subprocess
import signal
import sys
import time
import uuid
from typing import cast, override

from scripts.build_queue import admission_client as client, admission_protocol as wire, admission_records as records, queue_place
from scripts.buildlog import store
from scripts.lib import conf
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
    refresh = app_request(binary, repository, purpose, owner_name, sidecar, owner, episode, since, tuple(arguments))

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
        release_app(hold, status)


# Keep MCP Hana launches refused until the live admission proof is approved.
BRP_ADOPTION_ENABLED = False
PROC = Path("/proc")


def app_request(binary: Path, repository: Path, purpose: str, owner_name: str, sidecar: Path,
                owner: wire.Owner, episode: str, since: float, arguments: tuple[str, ...], instances: int = 1
                ) -> Callable[[], wire.AdmitRequest]:
    """Use the same app profile and owner for both launch routes."""
    identity = client.step_identity(f"app · {binary.name} · {purpose}", str(repository))
    identity = wire.Identity(identity.session_id, owner_name or identity.name, identity.step,
                             identity.folder, identity.unit)

    def refresh() -> wire.AdmitRequest:
        profile = gate.app_profile(binary, repository, datetime.now(UTC), store.root())
        profile = replace(profile, peak=replace(profile.peak, bytes=profile.peak.bytes * instances))
        return wire.AdmitRequest(episode, owner, arguments, str(repository), since,
            str(sidecar), identity, records.NoCallRecord(), records.NoTestWidth(), profile, False)

    return refresh


def release_app(hold: AppHold, status: int, *, instances: int = 1) -> None:
    """Release the existing app reservation and retire its sidecar."""
    if instances > 1:
        maximum = Path(hold.grant.max_path)
        try:
            peak = int(maximum.read_text())
        except FileNotFoundError:
            pass
        else:
            write_text_atomic(maximum, f"{peak // instances}\n")
    _ = queue_place.release_place(hold.grant.reservation, status, hold.grant.cleanup_timing)
    gate.retire_app_scope(hold.sidecar)


@dataclass(frozen=True)
class AdoptLaunch:
    binary: Path
    repository: Path
    purpose: str
    instances: int


@dataclass(frozen=True)
class AwaitingHandoff:
    pass


@dataclass(frozen=True)
class FailedHandoff:
    reason: str


@dataclass(frozen=True)
class AdoptedProcesses:
    owners: tuple[wire.Owner, ...]


def object_value(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("expected an object")
    return cast(dict[str, object], value)


def adoption_path(tool_use_id: str) -> Path:
    if not tool_use_id:
        raise ValueError("BRP launch has no tool_use_id")
    key = hashlib.sha256(tool_use_id.encode()).hexdigest()
    return store.root() / "admission/brp-holds" / key


def read_handoff(directory: Path) -> AwaitingHandoff | FailedHandoff | AdoptedProcesses:
    try:
        row = object_value(cast(object, json.loads((directory / "handoff.json").read_text())))
        reason = row.get("error")
        if isinstance(reason, str):
            return FailedHandoff(reason)
        values = row.get("owners")
        if not isinstance(values, list):
            raise ValueError("handoff owners must be a list")
        owners: list[wire.Owner] = []
        for value in cast(list[object], values):
            owner = object_value(value)
            pid, start = owner.get("pid"), owner.get("start_time")
            if type(pid) is not int or pid <= 0 or not isinstance(start, str) or not start:
                raise ValueError("invalid handoff process identity")
            owners.append(wire.Owner(pid, start))
        return AdoptedProcesses(tuple(owners))
    except FileNotFoundError:
        return AwaitingHandoff()
    except (ValueError, TypeError) as error:
        return FailedHandoff(str(error))


def publish_handoff(payload: dict[str, object], proc: Path = PROC) -> None:
    """Save identities at handoff, before a reused PID can name another app."""
    tool_id = payload.get("tool_use_id")
    if not isinstance(tool_id, str):
        return
    directory = adoption_path(tool_id)
    if not directory.is_dir() or (directory / "cancel").exists() or (directory / "released").exists():
        return
    try:
        response = object_value(payload.get("tool_response"))
        # MCP can return its structured result inside text content.
        if "instances" not in response and isinstance(response.get("content"), list):
            content = cast(list[object], response["content"])
            texts = [object_value(item).get("text") for item in content]
            text = next((item for item in texts if isinstance(item, str)), "")
            response = object_value(cast(object, json.loads(text)))
        instances = response.get("instances")
        if not isinstance(instances, list) or not instances:
            raise ValueError("BRP launch returned no instances")
        pids: list[int] = []
        for instance in cast(list[object], instances):
            pid = object_value(instance).get("pid")
            if type(pid) is not int or pid <= 0:
                raise ValueError("BRP launch returned an invalid pid")
            pids.append(pid)
        processes = gate.process_table(proc)
        owners: list[dict[str, int | str]] = []
        for pid in pids:
            identity = processes.get(pid)
            if identity is not None:
                owners.append({"pid": pid, "start_time": identity.start})
        roots = {int(owner["pid"]): str(owner["start_time"]) for owner in owners}
        owners = [{"pid": pid, "start_time": processes[pid].start}
                  for pid in sorted(gate.process_tree(roots, processes))]
        result: dict[str, object] = {"owners": owners}
        manifest = object_value(cast(object, json.loads((directory / "app.adopt").read_text())))
        manifest["processes"] = {str(owner["pid"]): owner["start_time"] for owner in owners}
        write_text_atomic(directory / "app.adopt", json.dumps(manifest))
    except (OSError, ValueError, TypeError) as error:
        result = {"error": str(error)}
    write_text_atomic(directory / "handoff.json", json.dumps(result))


class AdoptionWaitEnded(client.OwnerExitCancellation):
    """The adoption wait reached its deadline or received a cancellation."""


@dataclass
class AdoptWaitOutput(AppWaitOutput):
    directory: Path = field(default_factory=Path)
    deadline: float = 0.0

    def check(self) -> None:
        if time.monotonic() >= self.deadline or (self.directory / "cancel").exists():
            reason = "launch wait limit reached" if time.monotonic() >= self.deadline else "launch cancelled"
            raise AdoptionWaitEnded(f"{reason}; queue place {self.last_position}: " +
                                    ("; ".join(self.last_reasons) or "waiting for memory admission"))

    @override
    def __call__(self, reply: wire.Wait) -> None:
        super().__call__(reply)
        self.status(f"queue place {self.last_position}: " + "; ".join(self.last_reasons))
        self.check()


def adopt(launch: AdoptLaunch, directory: Path, wait_seconds: float, handoff_seconds: float,
          *, proc: Path = PROC) -> int:
    """Own an app hold while MCP launches and runs its process trees."""
    pid = os.getpid()
    start = records.process_start_time(pid)
    if start is None:
        raise OSError("adopt owner has exited")
    directory.mkdir(parents=True, exist_ok=True)
    # The service retains this cleanup record until the holder and hook exit.
    write_text_atomic(directory / "holder.owner", f"{pid} {start}\n")
    sidecar = directory / "app.cgroup"
    write_text_atomic(sidecar, "")
    write_text_atomic(sidecar.with_suffix(".adopt"), json.dumps({"owner_pid": pid,
        "owner_start": start, "processes": {}}))
    output = AdoptWaitOutput(status_file=directory / "status", directory=directory,
                             deadline=time.monotonic() + wait_seconds)
    refresh = app_request(launch.binary, launch.repository, launch.purpose, "", sidecar,
        wire.Owner(pid, start), directory.name, time.time(), (str(launch.binary),), launch.instances)
    try:
        check_seconds = conf.read("build_admission").number("brp_adopt_check_seconds")
        with client.checked_socket_wait(output.check, check_seconds):
            reply = queue_place.request_place(queue_place.RefreshedRequest(refresh), output, before_check=output.check)
    except (AdoptionWaitEnded, wire.AdmissionUnavailable, OSError, ValueError) as error:
        output.status(f"app_gate: {error}")
        write_text_atomic(directory / "refused", str(error))
        return 2
    if not isinstance(reply, wire.Admitted):
        reason = reply.reason if isinstance(reply, wire.Refused) else "left the admission queue"
        write_text_atomic(directory / "refused", reason)
        return 2
    hold = AppHold(reply, directory, sidecar)
    status = 1
    owner_exit = False
    try:
        output.check()
        write_text_atomic(directory / "granted", reply.reservation)
        deadline = time.monotonic() + handoff_seconds
        interval = records.read_memory_config().program_sample_seconds
        while True:
            handoff = read_handoff(directory)
            if not isinstance(handoff, AwaitingHandoff):
                break
            if time.monotonic() >= deadline or (directory / "cancel").exists():
                raise AdoptionWaitEnded("launch handoff wait ended before process identities arrived")
            time.sleep(min(interval, max(0, deadline - time.monotonic())))
        if isinstance(handoff, FailedHandoff):
            output.status(handoff.reason)
            return 2
        tracked = {owner.pid: owner.start_time for owner in handoff.owners}
        while tracked:
            try:
                processes = gate.process_table(proc)
            except OSError as error:
                output.status(f"app process scan failed; retaining memory hold: {error}")
                time.sleep(interval)
                continue
            members = gate.process_tree(tracked, processes)
            tracked = {member: processes[member].start for member in members}
            try:
                write_text_atomic(sidecar.with_suffix(".adopt"), json.dumps({"owner_pid": pid,
                    "owner_start": start, "processes": tracked}))
                memory = gate.process_memory(members, proc)
                peak = memory.bytes + memory.swap_bytes
                maximum = Path(reply.max_path)
                previous = int(maximum.read_text()) if maximum.exists() else 0
                if peak > previous:
                    write_text_atomic(maximum, f"{peak}\n")
            except (OSError, ValueError) as error:
                output.status(f"app sample failed; retaining memory hold: {error}")
            if tracked:
                time.sleep(interval)
        status = 0
        return status
    except AdoptionWaitEnded as error:
        owner_exit = True
        output.status(str(error))
        write_text_atomic(directory / "refused", str(error))
        return 2
    except (OSError, ValueError) as error:
        output.status(str(error))
        write_text_atomic(directory / "refused", str(error))
        return 2
    finally:
        # The service reclaims cancelled holds after fresh evidence confirms the holder exit.
        if not owner_exit:
            release_app(hold, status, instances=launch.instances)
            sidecar.with_suffix(".adopt").unlink(missing_ok=True)
            write_text_atomic(directory / "released", "done")


def start_adopter(launch: AdoptLaunch, directory: Path, wait_seconds: float, handoff_seconds: float) -> None:
    """Detach the holder from the hook."""
    child = os.fork()
    if child == 0:
        try:
            _ = os.setsid()
            if os.fork() != 0:
                os._exit(0)
            with open(os.devnull, "rb") as source, (directory / "holder.log").open("ab", buffering=0) as log:
                _ = os.dup2(source.fileno(), 0)
                _ = os.dup2(log.fileno(), 1)
                _ = os.dup2(log.fileno(), 2)
            os._exit(adopt(launch, directory, wait_seconds, handoff_seconds))
        except BaseException as error:
            write_text_atomic(directory / "refused", str(error))
            os._exit(2)
    _ = os.waitpid(child, 0)


def pre_launch(payload: dict[str, object]) -> str:
    """Return a refusal reason, or an empty reason after a memory grant."""
    if not BRP_ADOPTION_ENABLED:
        return "Hana needs an app memory hold. Use hana_shot.py launch --purpose <feature> (sets env.HANA_TEST_PURPOSE)."
    tool = object_value(payload.get("tool_input"))
    environment = object_value(tool.get("env", {}))
    purpose = environment.get("HANA_TEST_PURPOSE")
    if not isinstance(purpose, str) or not purpose.strip():
        return "Set env.HANA_TEST_PURPOSE to the feature under test."
    count = tool.get("instance_count", 1)
    if type(count) is not int or count < 1:
        return "instance_count must be a positive integer"
    root = Path(str(tool.get("path", payload.get("cwd", Path.cwd())))).expanduser().resolve()
    root = next((parent for parent in (root, *root.parents) if (parent / "crates/hana").is_dir()), root)
    if not (root / "crates/hana").is_dir():
        return "Pass path to a Hana workspace for memory admission."
    profile = tool.get("profile", "debug")
    if profile not in ("debug", "release"):
        return "Use a debug or release Hana profile for memory admission."
    launch = AdoptLaunch(root / "target" / str(profile) / "hana", root, purpose, count)
    tool_id = payload.get("tool_use_id")
    if not isinstance(tool_id, str):
        return "BRP launch has no tool_use_id"
    directory = adoption_path(tool_id)
    directory.mkdir(parents=True, exist_ok=False)
    hook_start = records.process_start_time(os.getpid())
    if hook_start is None:
        return "BRP hook owner has exited"
    write_text_atomic(directory / "hook.owner", f"{os.getpid()} {hook_start}\n")
    settings = conf.read("build_admission")
    wait_seconds = settings.number("brp_adopt_wait_seconds")
    check_seconds = settings.number("brp_adopt_check_seconds")
    handoff_seconds = settings.number("brp_adopt_handoff_seconds")
    if not 0 < wait_seconds <= 900 or not 0 < handoff_seconds <= 300:
        return "BRP adoption time limits are outside their allowed ranges"
    deadline = time.monotonic() + wait_seconds
    start_adopter(launch, directory, wait_seconds, handoff_seconds)
    while time.monotonic() < deadline:
        if (directory / "refused").exists():
            return (directory / "refused").read_text()
        if (directory / "granted").exists() and not (directory / "released").exists():
            return ""
        if (directory / "released").exists():
            return "The app memory holder ended before the MCP launch could start."
        time.sleep(min(check_seconds, max(0, deadline - time.monotonic())))
    write_text_atomic(directory / "cancel", "hook wait limit reached")
    try:
        reason = (directory / "status").read_text().strip()
    except FileNotFoundError:
        reason = "queue place unknown: admission service has not answered"
    return f"Hana launch wait limit reached; {reason}"


def hook_main(event: str) -> int:
    try:
        payload = object_value(cast(object, json.load(sys.stdin)))
        if event == "post":
            publish_handoff(payload)
            return 0
        reason = pre_launch(payload)
    except (OSError, ValueError, conf.Unusable) as error:
        if event == "post":
            return 0
        reason = str(error)
    if reason:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
            "permissionDecision": "deny", "permissionDecisionReason": reason}}))
    return 0


class AdoptArguments(argparse.Namespace):
    directory: Path = Path()
    binary: Path = Path()
    repo: Path = Path()
    purpose: str = ""
    instances: int = 1


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
    if arguments[:1] == ["adopt"]:
        parser = argparse.ArgumentParser(description="Hold memory for an MCP app launch.")
        _ = parser.add_argument("--directory", type=Path, required=True)
        _ = parser.add_argument("--binary", type=Path, required=True)
        _ = parser.add_argument("--repo", type=Path, required=True)
        _ = parser.add_argument("--purpose", required=True)
        _ = parser.add_argument("--instances", type=int, default=1)
        options = parser.parse_args(arguments[1:], namespace=AdoptArguments())
        if options.instances < 1:
            parser.error("instances must be positive")
        settings = conf.read("build_admission")
        return adopt(AdoptLaunch(options.binary, options.repo, options.purpose, options.instances),
            options.directory, settings.number("brp_adopt_wait_seconds"), settings.number("brp_adopt_handoff_seconds"))
    if arguments[:1] in (["hook-pre"], ["hook-post"]):
        return hook_main("pre" if arguments[0] == "hook-pre" else "post")
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
