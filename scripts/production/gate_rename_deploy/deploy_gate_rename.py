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
import subprocess
import tempfile
import time
from typing import cast
from zoneinfo import ZoneInfo

from scripts.build_hold import build_hold, hold_enforcer
from scripts.build_hold.owning_session import OwningSession
from scripts.lib import conf, user_zone
from scripts.lib.process_start import Started, Unreadable, process_start
from scripts.production.gate_rename_deploy import rename_step_names as rename
from scripts.production.gate_rename_deploy.rename_step_names import Direction

HOLDER = "gate-rename-deploy"
HERE = Path(__file__).resolve().parent
PROC_ROOT = Path(os.environ.get("VERIFY_PROC_ROOT", "/proc"))
NAMES = ("", "Wait for old AWS attempts", "Set the drain hold", "Wait for kept builds", "Stop waiting gates",
         "Check the conditions before the stop", "Stop services", "Check the buildlog service",
         "Check every conversion condition", "Move the gate folder", "Convert step names",
         "Check the unchanged second conversion", "Start services", "Release the owned hold")
LAST = len(NAMES) - 1
STOP_STEP, MOVE_STEP, CONVERT_STEP = 6, 9, 10


@dataclass(frozen=True)
class NoRecovery:
    """The deployment can continue with its next step."""


@dataclass(frozen=True)
class RecoveryRequired:
    """The saved failure requires recovery before another deployment attempt."""

    step: int
    status: int
    message: str


@dataclass
class DeploymentState:
    """The next unfinished step and ownership survive an interrupted command."""

    next_step: int = 1
    owns_hold: bool = False
    stopped_at: float = 0.0
    stop_seconds: float = 0.0
    # True from the save before the move until the conversion completes.
    moved_folder: bool = False
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
    discovery_arguments: tuple[str, ...] = ()


class DeploymentFailed(Exception):
    """A failed deployment step requires service recovery."""

    def __init__(self, message: str, status: int = 1) -> None:
        super().__init__(message)
        self.status: int = status


def say(message: str) -> None:
    print(message, flush=True)


def save(paths: DeploymentPaths, state: DeploymentState) -> None:
    descriptor, name = tempfile.mkstemp(prefix=".deploy-state-", dir=paths.journal.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(asdict(state), stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, paths.journal)
    finally:
        temporary.unlink(missing_ok=True)


def load(path: Path, reverse: bool) -> DeploymentState:
    if not path.exists():
        return DeploymentState(direction=Direction.REVERSE if reverse else Direction.FORWARD)
    value = cast(dict[str, object], json.loads(path.read_text()))
    step, owned = value.get("next_step"), value.get("owns_hold")
    began, total = value.get("stopped_at"), value.get("stop_seconds")
    moved = value.get("moved_folder", False)
    if (not isinstance(step, int) or isinstance(step, bool) or not 1 <= step <= LAST + 1
            or not isinstance(owned, bool) or not isinstance(moved, bool) or not isinstance(began, (int, float))
            or not isinstance(total, (int, float)) or began < 0 or total < 0):
        raise DeploymentFailed(f"Invalid state file. Restore it before another run: {path}")
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
    return DeploymentState(step, owned, float(began), float(total), moved, recovery, direction)


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

def run(settings: DeploymentSettings, command: Sequence[str], label: str) -> str:
    """Keep terminal input for sudo and summarize the child output."""
    with tempfile.TemporaryFile(mode="w+t") as output:
        with subprocess.Popen(command, stdout=output, stderr=subprocess.STDOUT, text=True) as child:
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


def service(paths: DeploymentPaths, action: str, unit: str, *, user: bool = False) -> None:
    _ = run(paths.settings, [*(["systemctl", "--user"] if user else ["sudo", "systemctl"]), action, unit], f"{action} {unit}")


def hold_command(paths: DeploymentPaths, action: str) -> None:
    command = [str(paths.tree / "scripts/lib/py"), str(paths.tree / "scripts/build_hold/build_hold.py"),
               action, "natedev", "--holder", HOLDER]
    if action == "drain":
        eta = datetime.now(paths.settings.zone) + timedelta(hours=paths.settings.hold_eta_hours)
        command += ["--for", "gate folder and step-name deployment", "--release-eta", eta.strftime("%H:%M"),
                    "--zone", paths.settings.zone.key]
    _ = run(paths.settings, command, f"{action} natedev")


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


def move_back(paths: DeploymentPaths, state: DeploymentState) -> None:
    """Return the gate folder before services start with unconverted step names."""
    if not state.moved_folder:
        return
    old, new = move_paths(paths)
    if os.path.lexists(new) and not os.path.lexists(old):
        os.rename(new, old)
        say(f"Moved the gate folder back: {new} -> {old}")
    elif not os.path.lexists(old) or os.path.lexists(new):
        raise DeploymentFailed(f"Cannot move the gate folder back: old exists={os.path.lexists(old)}; "
                               + f"new exists={os.path.lexists(new)}. Restore {old} before services start.")
    state.moved_folder = False
    save(paths, state)


def restart(paths: DeploymentPaths, state: DeploymentState) -> None:
    errors: list[str] = []
    for user, unit in ((False, "build-admission.socket"), (False, "build-admission.service"),
                       (False, "build-memory-feed.service"), (True, "buildlog.timer")):
        try:
            service(paths, "start", unit, user=user)
        except DeploymentFailed as error:
            errors.append(str(error))
    if errors:
        raise DeploymentFailed("Services did not start. Restore them before releasing the hold: " + "; ".join(errors))
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
    output = run(paths.settings,
        [str(paths.tree / "scripts/lib/py"), str(HERE / "deploy_gate_rename.py"), "--discover",
         *paths.roots.direction.arguments(), *paths.discovery_arguments], "Find assignment and cloud folders")
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
        _ = run(paths.settings, [str(paths.tree / "scripts/lib/py"), str(HERE / "rename_step_names.py"), "--check", "early",
                 *conversion_arguments(roots)], NAMES[number])
    elif number == STOP_STEP:
        if not state.stopped_at:
            state.stopped_at = time.time()
            save(paths, state)
        for user, unit in ((True, "buildlog.timer"), (False, "build-memory-feed.service"),
                           (False, "build-admission.socket"), (False, "build-admission.service")):
            service(paths, "stop", unit, user=user)
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
        _ = run(paths.settings, [str(paths.tree / "scripts/lib/py"), str(HERE / "rename_step_names.py"), "--check", "full",
                 *conversion_arguments(roots)], NAMES[number])
        _ = run(paths.settings, [str(paths.tree / "scripts/lib/py"), str(HERE / "move_gate_state.py"), "--check",
                 "--old", str(old), "--new", str(new)], NAMES[number])
    elif number == MOVE_STEP:
        old, new = move_paths(paths)
        if os.path.lexists(old):
            state.moved_folder = True
            save(paths, state)
            _ = run(paths.settings, [str(paths.tree / "scripts/lib/py"), str(HERE / "move_gate_state.py"),
                     "--old", str(old), "--new", str(new)], NAMES[number])
        else:
            say("The gate folder move is complete or no old folder exists.")
    elif number in (CONVERT_STEP, 11):
        roots = discover_roots(paths)
        output = run(paths.settings, [str(paths.tree / "scripts/lib/py"), str(HERE / "rename_step_names.py"),
                     *conversion_arguments(roots)], NAMES[number])
        last = output.strip().splitlines()[-1] if output.strip() else ""
        if number == 11 and not last.endswith("changed=0"):
            raise DeploymentFailed("The second conversion changed records. Inspect the writers before another run.", 2)
        say(last.split("Conversion complete;")[-1].strip())
        if number == CONVERT_STEP and state.moved_folder:
            state.moved_folder = False
            save(paths, state)
    elif number == 12:
        restart(paths, state)
    elif number == LAST:
        release_owned(paths, state)


def recover(paths: DeploymentPaths, state: DeploymentState, failure: RecoveryRequired) -> int:
    say(f"Failed step {failure.step}: {failure.message}")
    say("Cancel the complete foreground caller with Ctrl-C.")
    say("For a detached caller: detached_runs.py stop --reason 'gate rename deployment' <record.json>.")
    say("For a scoped caller: systemctl --user stop <unit.scope>.")
    try:
        if failure.step in (MOVE_STEP, CONVERT_STEP):
            move_back(paths, state)
        if state.stopped_at or failure.step >= STOP_STEP:
            restart(paths, state)
        release_owned(paths, state)
        state.next_step = 2
        state.recovery = NoRecovery()
        save(paths, state)
    except (DeploymentFailed, OSError) as error:
        say(f"Recovery failed: {error}. Restore the services, then run this command again.")
        return 1
    say("Run this command again after the caller exits or the reported cause is corrected.")
    say(f"Total stop time: {state.stop_seconds:.2f} s.")
    return failure.status


def deploy(paths: DeploymentPaths, state: DeploymentState, *,
           after_step: Callable[[int], None] = lambda _step: None) -> int:
    require_installed_code(paths.tree, state.direction)
    if isinstance(state.recovery, RecoveryRequired):
        return recover(paths, state, state.recovery)
    if state.next_step == LAST + 1:
        say(f"Deployment already complete. Total stop time: {state.stop_seconds:.2f} s.")
        return 0
    save(paths, state)
    try:
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
        state.recovery = RecoveryRequired(state.next_step, status, str(error))
        save(paths, state)
        return recover(paths, state, state.recovery)
    say(f"Total stop time: {state.stop_seconds:.2f} s.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--wait-root", type=Path, action="append")
    _ = parser.add_argument("--cloud-root", type=Path, action="append")
    _ = parser.add_argument("--reverse", action="store_true", help="undo the deployment with the old gate code installed")
    _ = parser.add_argument("--discover", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    home = Path.home()
    tree = HERE.parents[2]
    try:
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
            paths = DeploymentPaths(tree, home, journal, roots, settings, discovery)
            return deploy(paths, state)
    except (DeploymentFailed, rename.Refused, OSError, ValueError, SyntaxError) as error:
        say(f"Refused: {error}")
        return error.status if isinstance(error, DeploymentFailed) else 2


if __name__ == "__main__":
    raise SystemExit(main())
