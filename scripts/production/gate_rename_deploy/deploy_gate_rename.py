# Gate rename chunk 6 deletes this folder and its configuration after deployment.
"""Move gate state and convert step names in one recoverable stop window."""
from __future__ import annotations

import argparse
import ast
from collections.abc import Callable, Sequence
from contextlib import ExitStack, redirect_stdout
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta
import fcntl
import json
import os
from pathlib import Path
import select
import shlex
import subprocess
import tempfile
import time
from typing import Literal, cast
from zoneinfo import ZoneInfo

from scripts.build_hold import build_hold, hold_enforcer, override_guard
from scripts.build_hold.owning_session import OwningSession
from scripts.lib import conf, conf_reads, session_folder, user_zone
from scripts.lib.atomic_file import sync_folder, write_text_atomic
from scripts.lib.process_start import Started, Unreadable, process_start
from scripts.production.gate_rename_deploy import move_gate_state
from scripts.production.gate_rename_deploy import rename_step_names as rename
from scripts.production.gate_rename_deploy.rename_step_names import Direction

HOLDER = "gate-rename-deploy"
HERE = Path(__file__).resolve().parent
OVERRIDES = (
    *override_guard.OVERRIDE_VARIABLES,
    "BUILDLOG_DIR", "BUILD_TARGETS_STATE_DIR", "VERIFY_STEPS_DIR", "VERIFY_PROC_ROOT",
    "BUILD_HOLD_ENFORCER_DIR", "BUILD_HOLD_CGROUP_ROOT", "GATE_STATE_DIR", "MERGE_GATE_STATE_DIR",
    session_folder.ROOT_ENV, "TMPDIR", "TEMP", "TMP", "USER_ZONE_CONF", conf_reads.CONF_READ_LOG,
    "GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE", "GIT_CONFIG", "GIT_CONFIG_COUNT",
    "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_GLOBAL",
    "BASH_ENV", "ENV", "LD_PRELOAD", "LD_LIBRARY_PATH", "LD_AUDIT",
    "PYTHONEXECUTABLE", "__PYVENV_LAUNCHER__",
)
PROC_ROOT = move_gate_state.PROC_ROOT
NAMES = ("", "Wait for old AWS attempts", "Set the drain hold", "Wait for kept builds", "Stop waiting gates",
         "Check the conditions before the stop", "Stop services", "Check the buildlog service",
         "Check every conversion condition", "Move the gate folder", "Convert step names",
         "Check the unchanged second conversion", "Start services", "Release the owned hold")
LAST = len(NAMES) - 1
STOP_STEP, MOVE_STEP, CONVERT_STEP, RESTART_STEP = 6, 9, 10, 12


@dataclass(frozen=True)
class NoRecovery:
    """The deployment can continue with its next step."""


@dataclass(frozen=True)
class RecoveryRequired:
    """The saved failure requires recovery before another deployment attempt."""

    step: int
    status: int
    message: str


@dataclass(frozen=True)
class GateFolders:
    """Record both gate paths that the land reader can select."""

    source: tuple[int, int] | None
    destination: tuple[int, int] | None

    @classmethod
    def read(cls, source: Path, destination: Path) -> GateFolders:
        return cls(move_gate_state.path_identity(source, directory=True),
                   move_gate_state.path_identity(destination, directory=True))

    @property
    def moved(self) -> GateFolders:
        return self if self.source is None else GateFolders(None, self.source)


@dataclass
class DeploymentState:
    """The next unfinished step and ownership survive an interrupted command."""

    next_step: int = 1
    owns_hold: bool = False
    stopped_at: float = 0.0
    stop_seconds: float = 0.0
    gate_folders: GateFolders | None = None
    recovery: NoRecovery | RecoveryRequired = field(default_factory=NoRecovery)
    direction: Direction = Direction.FORWARD


@dataclass(frozen=True)
class DeploymentSettings:
    stop_wait_seconds: float
    heartbeat_seconds: float
    hold_eta_hours: int
    zone: ZoneInfo
    error_output_characters: int

    @classmethod
    def read(cls, settings: conf.ConfigRead) -> DeploymentSettings:
        return cls(settings.number("stop_wait_seconds"), settings.number("heartbeat_seconds"),
                   settings.whole("drain_hold_eta_hours"), user_zone.user_zone(),
                   settings.whole("error_output_characters"))


@dataclass(frozen=True)
class DeploymentPaths:
    """The selected installed code and records belong to this deployment."""

    tree: Path
    home: Path
    journal: Path
    roots: rename.RecordRoots
    settings: DeploymentSettings
    lock_fd: int
    discovery_arguments: tuple[str, ...] = ()

    @property
    def writes(self) -> rename.ConversionWrites:
        return rename.ConversionWrites(self.journal.with_name("conversion-writes"))


class DeploymentFailed(Exception):
    """A failed deployment step requires service recovery."""

    def __init__(self, message: str, status: int = 1) -> None:
        super().__init__(message)
        self.status: int = status


def say(message: str) -> None:
    print(message, flush=True)


def save(paths: DeploymentPaths, state: DeploymentState) -> None:
    write_text_atomic(paths.journal, json.dumps(asdict(state)), new_file_mode=0o600)


def folder_identity(value: object) -> tuple[int, int] | None:
    if value is None:
        return None
    if isinstance(value, list):
        fields = cast(list[object], value)
        if len(fields) == 2:
            device, inode = fields
            if type(device) is int and type(inode) is int:
                return device, inode
    raise ValueError("Invalid gate folder identity.")


def load(path: Path, reverse: bool) -> DeploymentState:
    if not path.exists():
        return DeploymentState(direction=Direction.REVERSE if reverse else Direction.FORWARD)
    value = cast(dict[str, object], json.loads(path.read_text()))
    step, owned = value.get("next_step"), value.get("owns_hold")
    began, total = value.get("stopped_at"), value.get("stop_seconds")
    if (not isinstance(step, int) or isinstance(step, bool) or not 1 <= step <= LAST + 1
            or not isinstance(owned, bool) or not isinstance(began, (int, float))
            or not isinstance(total, (int, float)) or began < 0 or total < 0):
        raise DeploymentFailed(f"Invalid state file. Restore it before another run: {path}")
    original = value.get("gate_folders")
    gate_folders = None
    if original is not None:
        if not isinstance(original, dict) or original.keys() != {"source", "destination"}:
            raise DeploymentFailed(f"Invalid gate folders in the state file: {path}")
        fields = cast(dict[str, object], original)
        gate_folders = GateFolders(folder_identity(fields["source"]), folder_identity(fields["destination"]))
        if gate_folders.source is not None and gate_folders.destination is not None:
            raise DeploymentFailed(f"Both gate folders exist in the state file: {path}")
    recovery_value = value.get("recovery", {})
    recovery: NoRecovery | RecoveryRequired = NoRecovery()
    if recovery_value != {}:
        if not isinstance(recovery_value, dict):
            raise DeploymentFailed(f"Invalid recovery in the state file: {path}")
        saved = cast(dict[str, object], recovery_value)
        failed_step, status, message = saved.get("step"), saved.get("status"), saved.get("message")
        if (not isinstance(failed_step, int) or isinstance(failed_step, bool) or not 1 <= failed_step <= LAST
                or not isinstance(status, int) or isinstance(status, bool) or status not in (1, 2)
                or not isinstance(message, str)):
            raise DeploymentFailed(f"Invalid recovery in the state file: {path}")
        recovery = RecoveryRequired(failed_step, status, message)
    try:
        direction = Direction(value.get("direction", Direction.FORWARD))
    except ValueError as error:
        raise DeploymentFailed(f"Invalid direction in the state file: {path}") from error
    requested = Direction.REVERSE if reverse else (Direction.FORWARD if step == LAST + 1 else direction)
    if requested is not direction:
        if step != LAST + 1:
            raise DeploymentFailed(f"Complete the {direction.value} deployment before changing direction.", 2)
        return DeploymentState(direction=requested)
    return DeploymentState(step, owned, float(began), float(total), gate_folders, recovery, direction)


def require_installed_code(tree: Path, direction: Direction) -> None:
    production = tree / "scripts/production"
    land = ast.parse((production / "land_runs.py").read_text())
    reverse = direction is Direction.REVERSE
    gate_path = production / ("merge_gate.py" if reverse else "gate.py")
    _, gate_step = direction.gate_steps()
    expected_folder = f"~/.local/state/{gate_step}"
    default = next((node.value for node in land.body if isinstance(node, ast.Assign)
                    and any(isinstance(target, ast.Name) and target.id == "DEFAULT_STATE_DIR"
                            for target in node.targets)), None)
    if reverse and default is None:
        # The old reader sets the default in state_dir().
        default_matches = any(isinstance(value, ast.Constant) and value.value == expected_folder
            for node in land.body if isinstance(node, ast.FunctionDef) and node.name == "state_dir"
            for value in ast.walk(node))
    else:
        default_matches = isinstance(default, ast.Constant) and default.value == expected_folder
    if default_matches and gate_path.is_file():
        gate = ast.parse(gate_path.read_text())
        modes = {child.value.value for node in gate.body
                 if isinstance(node, ast.ClassDef) and node.name == "SuiteMode"
                 for child in node.body if isinstance(child, ast.Assign) and isinstance(child.value, ast.Constant)}
        if modes == set(direction.names().values()):
            return
    expected = "old" if reverse else "new"
    raise DeploymentFailed(f"Install the {expected} gate code in the live checkout before deployment.", 2)


def run(settings: DeploymentSettings, command: Sequence[str], label: str, *,
        lock_fd: int | None = None, environment: dict[str, str] | None = None) -> str:
    """Keep terminal input for sudo and summarize the child output."""
    with tempfile.TemporaryFile(mode="w+t") as output:
        pass_fds = () if lock_fd is None else (lock_fd,)
        with subprocess.Popen(command, stdout=output, stderr=subprocess.STDOUT, text=True,
                              pass_fds=pass_fds, env=environment) as child:
            while True:
                try:
                    status = child.wait(timeout=settings.heartbeat_seconds)
                    break
                except subprocess.TimeoutExpired:
                    say(f"Still running: {label} ({datetime.now(settings.zone):%H:%M:%S %Z})")
        _ = output.seek(0)
        text = output.read()
    if status:
        lines = [line for line in text.splitlines() if "Refused:" in line or "pid=" in line]
        detail = "\n".join(lines) if lines else text.strip()[-settings.error_output_characters:]
        raise DeploymentFailed(detail or f"Command exited {status}: {label}", 2 if status == 2 else 1)
    return text


def run_python(paths: DeploymentPaths, script: Path, arguments: Sequence[str], label: str, *,
               writes: bool = False) -> str:
    # Some shared helpers start Python again. Keep their startup environment clean.
    environment = {name: value for name, value in os.environ.items() if not name.startswith("PYTHON")}
    command = [str(paths.tree / "scripts/lib/py"), "-I", str(script), *arguments]
    return run(paths.settings, command, label, lock_fd=paths.lock_fd if writes else None, environment=environment)


def service(paths: DeploymentPaths, action: str, unit: str, *, user: bool = False) -> None:
    if user:
        command = ["systemctl", "--user", action, unit]
    else:
        # The shell retains the lock while sudo runs and returns the child's status.
        command = ["bash", "-c", '"$@"\nexit "$?"', HOLDER, "sudo", "systemctl", action, unit]
    _ = run(paths.settings, command, f"{action} {unit}", lock_fd=paths.lock_fd)


def hold_command(paths: DeploymentPaths, action: str) -> None:
    arguments = [action, "natedev", "--holder", HOLDER]
    if action == "drain":
        eta = datetime.now(paths.settings.zone) + timedelta(hours=paths.settings.hold_eta_hours)
        arguments += ["--for", "gate folder and step-name deployment", "--release-eta", eta.strftime("%H:%M"),
                      "--zone", paths.settings.zone.key]
    _ = run_python(paths, paths.tree / "scripts/build_hold/build_hold.py", arguments, f"{action} natedev", writes=True)


def wait_cloud(roots: rename.RecordRoots, settings: DeploymentSettings) -> None:
    while True:
        try:
            rename.require_expired_uploaded_attempts(roots)
            return
        except rename.Refused as error:
            say(f"Waiting: {error}")
            # This is the deployment wait, before the hold and service stop.
            _ = select.select([], [], [], settings.heartbeat_seconds)
            say(f"Still waiting for old AWS attempts ({datetime.now(settings.zone):%H:%M:%S %Z})")


def wait_kept(settings: DeploymentSettings) -> None:
    with ExitStack() as cleanup:
        descriptors: list[int] = []
        holds = build_hold.natedev_holds(build_hold.holder_directory())
        if not holds.in_force:
            raise DeploymentFailed("The drain hold is absent. Set the drain hold before another run.", 2)
        for holder in holds.in_force:
            for kept in holder.keep:
                identity = process_start(kept.pid)
                if isinstance(identity, Unreadable):
                    raise DeploymentFailed(f"Cannot read kept pid={kept.pid}. Check its process before another run.", 2)
                if identity != Started(kept.start_time):
                    continue
                try:
                    descriptor = os.pidfd_open(kept.pid)
                except ProcessLookupError:
                    continue
                _ = cleanup.callback(os.close, descriptor)
                descriptors.append(descriptor)
        while descriptors:
            ready, _, _ = select.select(descriptors, [], [], settings.heartbeat_seconds)
            descriptors = [descriptor for descriptor in descriptors if descriptor not in ready]
            if descriptors:
                say(f"Still waiting for {len(descriptors)} kept builds ({datetime.now(settings.zone):%H:%M:%S %Z})")


def in_force_holder() -> build_hold.Holder:
    holds = build_hold.natedev_holds(build_hold.holder_directory()).in_force
    if not holds:
        raise DeploymentFailed("The drain hold is absent. Set the drain hold before another run.", 2)
    own = build_hold.holder_path(build_hold.holder_directory(), HOLDER).name
    return next((holder for holder in holds if holder.key == own or holder.name == HOLDER), holds[0])


def stop_waiting_gates(settings: DeploymentSettings) -> None:
    """Stop each gate that only waits, by its run record or its exact pid and start time."""
    holder = in_force_holder()
    processes = build_hold.read_audit_processes(PROC_ROOT)
    runs = hold_enforcer.classify_drain_runs(processes, hold_enforcer.admitted_processes(),
                                             hold_enforcer.claimed_processes(PROC_ROOT))
    if isinstance(runs, build_hold.ClaimsUnreadable):
        raise DeploymentFailed(f"Cannot read build claims: {runs.reason}. Correct them before another run.", 2)
    for run in runs:
        if isinstance(run, hold_enforcer.WorkingRun):
            say(f"Working gate remains: pid={run.process.identity.pid} reason={run.reason}")
    waiting = [run for run in runs if isinstance(run, hold_enforcer.WaitingRun)]
    if not waiting:
        say("No gate waits.")
        return
    with ExitStack() as cleanup:
        descriptors: dict[int, int] = {}
        for run in waiting:
            identity = run.process.identity
            detached = hold_enforcer.detached_run(run.process, processes)
            record = (str(detached.path) if isinstance(detached, hold_enforcer.DetachedRun)
                      else "none; stopped by its pid and start time")
            owner = run.owner.session_id if isinstance(run.owner, OwningSession) else "no session"
            result = hold_enforcer.stop_waiting_run(run, holder, processes, PROC_ROOT)
            label = "Stopped" if isinstance(result, hold_enforcer.StoppedRun) else "Not stopped (identity changed or kept)"
            say(f"{label}: pid={identity.pid} owner={owner} record={record} "
                + f"command={' '.join(run.process.argv)} cwd={run.process.cwd}")
            if isinstance(result, hold_enforcer.StoppedRun):
                try:
                    descriptor = os.pidfd_open(identity.pid)
                except ProcessLookupError:
                    continue
                _ = cleanup.callback(os.close, descriptor)
                descriptors[descriptor] = identity.pid
        deadline = time.monotonic() + settings.stop_wait_seconds
        while descriptors and (remaining := deadline - time.monotonic()) > 0:
            ready, _, _ = select.select(list(descriptors), [], [], min(settings.heartbeat_seconds, remaining))
            for descriptor in ready:
                del descriptors[descriptor]
            if descriptors:
                say(f"Still waiting for {len(descriptors)} stopped gates ({datetime.now(settings.zone):%H:%M:%S %Z})")
        if descriptors:
            raise DeploymentFailed("Stopped gates did not exit: " + ", ".join(
                f"pid={pid}" for pid in sorted(descriptors.values())) + ". Check them before another run.", 2)
    say("Tell each owner above to run the gate again after the release.")


def move_paths(paths: DeploymentPaths) -> tuple[Path, Path]:
    source, destination = paths.roots.direction.gate_steps()
    parent = paths.home / ".local/state"
    return parent / source, parent / destination


def original_folders(state: DeploymentState) -> GateFolders:
    if state.gate_folders is None:
        raise DeploymentFailed("The state file has no proof of the original gate folders.")
    return state.gate_folders


def prove_original_state(paths: DeploymentPaths, state: DeploymentState, *, restore_folder: bool = False) -> None:
    """Check the original records and both gate paths."""
    if paths.writes.requires_completion():
        raise DeploymentFailed("Cannot prove the records match their state before deployment.")
    original = original_folders(state)
    old, new = move_paths(paths)
    if restore_folder and original.source is not None:
        current = GateFolders.read(old, new)
        if current == original.moved:
            move_gate_state.rename_folder(new, old)
            say(f"Moved the gate folder back: {new} -> {old}")
        elif current == original:
            sync_folder(old.parent)
    if GateFolders.read(old, new) != original:
        raise DeploymentFailed(f"Cannot verify the original gate folders at {old} and {new}.")


def change_services(paths: DeploymentPaths, action: Literal["start", "stop"]) -> None:
    timer = (True, "buildlog.timer")
    feed = (False, "build-memory-feed.service")
    admission = ((False, "build-admission.socket"), (False, "build-admission.service"))
    order = (*admission, feed, timer) if action == "start" else (timer, feed, *admission)
    errors: list[str] = []
    for user, unit in order:
        try:
            service(paths, action, unit, user=user)
        except (DeploymentFailed, OSError) as error:
            errors.append(str(error))
    if errors:
        raise DeploymentFailed(f"Cannot {action} all services.\n" + "\n".join(errors))


def record_service_start(paths: DeploymentPaths, state: DeploymentState) -> None:
    if state.stopped_at:
        state.stop_seconds += max(0.0, time.time() - state.stopped_at)
        state.stopped_at = 0.0
        save(paths, state)


def release_owned(paths: DeploymentPaths, state: DeploymentState) -> None:
    if state.owns_hold:
        directory = build_hold.holder_directory()
        holder = build_hold.holder_path(directory, HOLDER)
        with build_hold.release_lock() as held:
            published = holder.is_file() or any(
                record.intent["holder_directory"] == str(directory.resolve())
                and record.intent["holder_key"] == holder.name for record in build_hold.recorded_releases(held))
        if published:
            hold_command(paths, "release")
        else:
            say("The owned hold was never published. No release is required.")
        state.owns_hold = False
        save(paths, state)


def conversion_arguments(roots: rename.RecordRoots) -> list[str]:
    return [*roots.direction.arguments(), "--buildlog", str(roots.buildlog),
            "--aws-state", str(roots.aws), "--review-page", str(roots.review),
            *[word for root in roots.wait_roots for word in ("--wait-root", str(root))],
            *[word for root in roots.cloud_roots for word in ("--cloud-root", str(root))]]


def discover_roots(paths: DeploymentPaths) -> rename.RecordRoots:
    say("Find assignment and cloud folders")
    output = run_python(paths, HERE / "deploy_gate_rename.py",
        ["--discover", *paths.roots.direction.arguments(), *paths.discovery_arguments], "Find assignment and cloud folders")
    discovered = cast(dict[str, list[str]], json.loads(output))
    say(f"Done: Find assignment and cloud folders at {datetime.now(paths.settings.zone):%Y-%m-%d %H:%M:%S %Z}")
    return replace(paths.roots, wait_roots=tuple(Path(path) for path in discovered["wait"]),
                   cloud_roots=tuple(Path(path) for path in discovered["cloud"]))


def execute_step(number: int, paths: DeploymentPaths, state: DeploymentState) -> None:
    if number == 1:
        wait_cloud(discover_roots(paths), paths.settings)
    elif number == 2:
        holds = build_hold.natedev_holds(build_hold.holder_directory())
        if not holds.in_force:
            state.owns_hold = True
            save(paths, state)
            hold_command(paths, "drain")
        else:
            say("The existing hold remains owned by its current holder.")
    elif number == 3:
        wait_kept(paths.settings)
    elif number == 4:
        stop_waiting_gates(paths.settings)
    elif number == 5:
        roots = discover_roots(paths)
        _ = run_python(paths, HERE / "rename_step_names.py", ["--check", "early", *conversion_arguments(roots)],
                       NAMES[number])
    elif number == STOP_STEP:
        if not state.stopped_at:
            state.stopped_at = time.time()
            save(paths, state)
        change_services(paths, "stop")
    elif number == 7:
        state_name = run(paths.settings,
            ["systemctl", "--user", "show", "--property=ActiveState", "--value", "buildlog.service"],
            "check buildlog.service").strip()
        if state_name not in ("inactive", "failed"):
            raise DeploymentFailed("buildlog.service still runs. Let it complete before another run.", 2)
    elif number == 8:
        # A gate that started to wait after step 4 cannot reach admission now; stop it before the check.
        stop_waiting_gates(paths.settings)
        old, new = move_paths(paths)
        roots = discover_roots(paths)
        _ = run_python(paths, HERE / "rename_step_names.py", ["--check", "full", *conversion_arguments(roots)],
                       NAMES[number])
        _ = run_python(paths, HERE / "move_gate_state.py", ["--check", "--old", str(old), "--new", str(new)],
                       NAMES[number])
    elif number == MOVE_STEP:
        original = original_folders(state)
        old, new = move_paths(paths)
        current = GateFolders.read(old, new)
        if original.source is not None and current == original:
            _ = run_python(paths, HERE / "move_gate_state.py",
                           ["--defer-typecheck-cleanup", "--old", str(old), "--new", str(new)], NAMES[number],
                           writes=True)
        elif original.source is not None and current == original.moved:
            say(f"The gate folder has already moved to {new}.")
        if GateFolders.read(old, new) != original.moved:
            raise DeploymentFailed(f"The gate folders changed during deployment: {old} and {new}.", 2)
        if original.source is None:
            say("No source gate folder exists.")
    elif number in (CONVERT_STEP, 11):
        roots = discover_roots(paths)
        output = run_python(paths, HERE / "rename_step_names.py",
                            ["--write-marker", str(paths.writes.marker), *conversion_arguments(roots)], NAMES[number],
                            writes=True)
        last = output.strip().splitlines()[-1] if output.strip() else ""
        if number == 11 and not last.endswith("changed=0"):
            raise DeploymentFailed("The second conversion changed records. Inspect the writers before another run.", 2)
        say(last.split("Conversion complete;")[-1].strip())
    elif number == RESTART_STEP:
        original = original_folders(state)
        old, target = move_paths(paths)
        if GateFolders.read(old, target) != original.moved:
            raise DeploymentFailed(f"Cannot verify the gate folders after the move: {old} and {target}.")
        if original.source is not None:
            move_gate_state.remove_typecheck_environment(target)
        change_services(paths, "start")
        record_service_start(paths, state)
        paths.writes.clear()
    elif number == LAST:
        release_owned(paths, state)


def print_resume_command(paths: DeploymentPaths) -> None:
    arguments = shlex.join((*paths.roots.direction.arguments(), *paths.discovery_arguments))
    command = "bash ~/.claude/scripts/production/gate_rename_deploy/deploy_gate_rename.sh"
    say(f"After correcting the cause, continue with: {command}" + (f" {arguments}" if arguments else ""))


def recover(paths: DeploymentPaths, state: DeploymentState, failure: RecoveryRequired) -> int:
    say(f"Failed step {failure.step}: {failure.message}")
    say("Cancel the complete foreground caller with Ctrl-C.")
    say("For a detached caller: detached_runs.py stop --reason 'gate rename deployment' <record.json>.")
    say("For a scoped caller: systemctl --user stop <unit.scope>.")
    needs_restart = bool(state.stopped_at) or failure.step >= STOP_STEP
    try:
        prove_original_state(paths, state, restore_folder=failure.step >= MOVE_STEP)
        if needs_restart:
            change_services(paths, "start")
            prove_original_state(paths, state)
            record_service_start(paths, state)
    except (DeploymentFailed, OSError, ValueError) as error:
        say(f"Recovery blocked: {error}")
        try:
            if needs_restart:
                change_services(paths, "stop")
        except (DeploymentFailed, OSError) as stop_error:
            say(f"Recovery failed to stop the services: {stop_error}")
            say("Keep the drain hold in force.")
            print_resume_command(paths)
            return 1
        say("Keep the services stopped and the drain hold in force.")
        print_resume_command(paths)
        return failure.status
    try:
        release_owned(paths, state)
        state.gate_folders = None
        state.next_step = 2
        state.recovery = NoRecovery()
        save(paths, state)
    except (DeploymentFailed, OSError, ValueError) as error:
        say(f"Recovery failed: {error}")
        print_resume_command(paths)
        return 1
    say("Run this command again after the caller exits or the reported cause is corrected.")
    say(f"Total stop time: {state.stop_seconds:.2f} s.")
    return failure.status


def deploy(paths: DeploymentPaths, state: DeploymentState, *,
           after_step: Callable[[int], None] = lambda _step: None) -> int:
    require_installed_code(paths.tree, state.direction)
    if isinstance(state.recovery, RecoveryRequired):
        if state.recovery.step < RESTART_STEP:
            return recover(paths, state, state.recovery)
        state.recovery = NoRecovery()
    if state.next_step == LAST + 1:
        say(f"Deployment already complete. Total stop time: {state.stop_seconds:.2f} s.")
        return 0
    if state.gate_folders is None:
        if state.stopped_at or state.next_step > STOP_STEP:
            say("Cannot verify the state before the service stop. Keep the drain hold in force.")
            print_resume_command(paths)
            return 2
        old, new = move_paths(paths)
        state.gate_folders = GateFolders.read(old, new)
        if state.gate_folders.source is not None and state.gate_folders.destination is not None:
            say(f"Refused: both gate folders exist: {old} and {new}.")
            print_resume_command(paths)
            return 2
    number = state.next_step
    try:
        save(paths, state)
        while state.next_step <= LAST:
            number = state.next_step
            say(f"{number}. {NAMES[number]}")
            execute_step(number, paths, state)
            state.next_step = number + 1
            save(paths, state)
            say(f"Done: {NAMES[number]} at {datetime.now(paths.settings.zone):%Y-%m-%d %H:%M:%S %Z}")
            after_step(number)
    except (DeploymentFailed, rename.Refused, OSError, ValueError) as error:
        status = error.status if isinstance(error, DeploymentFailed) else 1
        failure = RecoveryRequired(number, status, str(error))
        try:
            if number < RESTART_STEP and not paths.writes.requires_completion():
                state.recovery = failure
                save(paths, state)
                return recover(paths, state, failure)
        except OSError as journal_error:
            say(f"Cannot verify or save the recovery state: {journal_error}")
        say(f"Failed step {failure.step}: {failure.message}")
        if number < RESTART_STEP and state.stopped_at:
            say("Keep the services stopped and the drain hold in force.")
        print_resume_command(paths)
        return status
    say(f"Total stop time: {state.stop_seconds:.2f} s.")
    return 0


def checked_home() -> Path:
    for name in OVERRIDES:
        if name in os.environ:
            raise DeploymentFailed(f"Unset {name} before deployment.", 2)
    home = override_guard.user_home()
    if Path.home().resolve() != home.resolve():
        raise DeploymentFailed("HOME must name the account's home folder.", 2)
    return home


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--wait-root", type=Path, action="append")
    _ = parser.add_argument("--cloud-root", type=Path, action="append")
    _ = parser.add_argument("--reverse", action="store_true", help="undo the deployment with the old gate code installed")
    _ = parser.add_argument("--discover", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    tree = HERE.parents[2]
    try:
        home = checked_home()
        if tree != (home / ".claude").resolve():
            raise DeploymentFailed("Run the deploy command from the live checkout at ~/.claude.", 2)
        configuration = conf.read("gate_rename_deploy")
        settings = DeploymentSettings.read(configuration)
        os.environ["TZ"] = settings.zone.key
        time.tzset()
        reverse = cast(bool, args.reverse)
        wait = cast(list[Path] | None, args.wait_root)
        cloud = cast(list[Path] | None, args.cloud_root)
        if cast(bool, args.discover):
            require_installed_code(tree, Direction.REVERSE if reverse else Direction.FORWARD)
            with tempfile.TemporaryFile(mode="w+t") as details, redirect_stdout(details):
                wait_roots = tuple(wait) if wait is not None else rename.default_wait_roots()
                cloud_roots = tuple(cloud) if cloud is not None else rename.discover_cloud_roots((tree,))
            say(json.dumps({"wait": [str(path) for path in wait_roots], "cloud": [str(path) for path in cloud_roots]}))
            return 0
        state_path = run(settings, ["git", "-C", str(tree), "rev-parse", "--path-format=absolute",
            "--git-path", "gate-rename-deploy"], "Find the deployment state folder").strip()
        state_folder = Path(state_path)
        state_folder.mkdir(exist_ok=True)
        with (state_folder / "deploy_gate_rename.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                say("Another deployment command runs. Let it complete before another run.")
                return 2
            journal = state_folder / "deploy_gate_rename.state.json"
            state = load(journal, reverse)
            roots = rename.configured_roots(state.direction)
            discovery = tuple(word for flag, folders in (("--wait-root", wait), ("--cloud-root", cloud))
                              for folder in folders or () for word in (flag, str(folder)))
            paths = DeploymentPaths(tree, home, journal, roots, settings, lock.fileno(), discovery)
            return deploy(paths, state)
    except (DeploymentFailed, rename.Refused, OSError, ValueError, SyntaxError) as error:
        say(f"Refused: {error}")
        return error.status if isinstance(error, DeploymentFailed) else 2


if __name__ == "__main__":
    raise SystemExit(main())
