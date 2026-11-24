#!/usr/bin/env python3
"""Claim a unit worker and submit one named dispatch."""
from __future__ import annotations

try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

from scripts.direct import pass_record as pass_recorder

import argparse
import fcntl
from collections.abc import Callable
import hashlib
from importlib import import_module
import json
import os
import re
import shlex
import subprocess
import sys
import threading
import time
import uuid
from contextlib import ExitStack
from dataclasses import asdict, dataclass, field, replace
from enum import StrEnum
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryFile
from typing import Literal, NoReturn, cast, final, overload

from scripts.astra import astra_roles
from scripts.herdr import herdr_types
from scripts.agents import worker_lifecycle
from scripts.lib.process_failure import failure_detail
from scripts.direct import assign_claude, phase_reservation, progress_history, recorder_state, remove_run_scratch, simplify_daily
from scripts.production import staff, worker_admission
from scripts.lib import conf, models_registry, unit_runs
from scripts.herdr import herdr, side_pane, worker_panes
from scripts.production import worker as staff_worker
from scripts.lib.atomic_file import write_text_atomic
from scripts.lib.user_zone import user_zone
from scripts.lint import style_review
from scripts.usage.agents_log import PHASE_REVIEW_LENSES
from scripts.whoami import quota_state

SCRIPTS = Path(__file__).resolve().parent
PYTHON = str(SCRIPTS / "../lib/py")
PROGRESS = str(SCRIPTS / "progress_history.py")
PASS_RECORD = str(SCRIPTS / "pass_record.py")
RULES = """
Rules for this dispatch:
Run only commands that finish within one call. Never poll, sleep, call wait,
loop on a status file, or make an empty write_stdin poll. Run long commands
through detached_runs.py start; then end your turn and receive the result message.
For Python test runs, pass --kind python-tests before --.
We believe in three gods, Simple, Fast and Beautiful, with correctness as the floor beneath them.
"""


class AssignmentOperation(StrEnum):
    DISPATCH = "dispatch"
    ASTRA_REVIEW = "astra-review"




@final
class Arguments(argparse.Namespace):
    role: str = ""
    target: str = "staff-worker"
    policy: str = ""
    parent: str = ""
    to: str = ""
    new_worker: str = ""
    model: str = ""
    effort: str = ""
    reply_file: str = ""
    log: str = ""
    fix_cause: str = ""
    paths: str = ""
    phase: str = ""
    early_ready: str = ""
    style_since: str = ""
    style_until: str = ""
    session_dir: str = ""
    prompt: str = ""
    cwd: str = ""
    lens: str = ""
    pass_number: int = 1
    image: list[str] = []
    operation: AssignmentOperation = AssignmentOperation.DISPATCH
    completion_actions: str = "{}"


def object_map(value: object) -> dict[str, object]:
    return cast(dict[str, object], value) if isinstance(value, dict) else {}


def text(value: object) -> str:
    return value if isinstance(value, str) else ""


def read_object(path: Path) -> dict[str, object]:
    state = worker_lifecycle.read_worker_state(path)
    recorder_state.require_current_shape(path, state)
    return state




def refuse(message: str) -> NoReturn:
    print(f"assign.sh: {message}", file=sys.stderr)
    raise SystemExit(2)


def call(command: list[str], env: dict[str, str], *, capture: bool = False,
         check: bool = True, input: str | None = None) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, env=env, text=True, capture_output=capture, input=input)
    if check and result.returncode:
        if capture:
            print(result.stderr or result.stdout, file=sys.stderr, end="")
        raise SystemExit(2)
    return result


def absolute_reply_file(value: str) -> str:
    if not Path(value).is_absolute():
        print(f"assign: --reply-file takes an absolute path to the reply file, not text: {value}", file=sys.stderr)
        raise SystemExit(2)
    return value


def parse_arguments() -> Arguments:
    if any(argument.partition("=")[0] == "--summary" for argument in sys.argv[1:]):
        print("assign: --summary was renamed to --reply-file; give an absolute path to the reply file", file=sys.stderr)
        raise SystemExit(2)
    parser = argparse.ArgumentParser(prog="assign.sh", allow_abbrev=False)
    _ = parser.add_argument("--role", required=True,
                            choices=("implement", "test", "fix", "review", "research", "propose", "simplify"))
    _ = parser.add_argument("--operation", type=AssignmentOperation, choices=tuple(AssignmentOperation),
                            default=AssignmentOperation.DISPATCH)
    _ = parser.add_argument("--target", choices=("staff-worker", "consultation-pane", "durable-design-role"), default="staff-worker")
    _ = parser.add_argument("--policy", choices=("astra-reviewer", "astra-designer"), default="")
    _ = parser.add_argument("--parent", default="")
    choice = parser.add_mutually_exclusive_group()
    _ = choice.add_argument("--to", default="")
    _ = choice.add_argument("--new-worker", default="")
    for option in ("model", "effort", "log", "fix-cause", "paths", "phase", "early-ready", "session-dir",
                   "style-since", "style-until"):
        _ = parser.add_argument("--" + option, default="")
    _ = parser.add_argument("--reply-file", type=absolute_reply_file, default=argparse.SUPPRESS)
    _ = parser.add_argument("--completion-actions", default="{}")
    _ = parser.add_argument("--prompt", default="")
    _ = parser.add_argument("--cwd", default=str(Path.cwd()))
    _ = parser.add_argument("--lens", choices=PHASE_REVIEW_LENSES, default="")
    _ = parser.add_argument("--pass", dest="pass_number", type=int, default=1)
    _ = parser.add_argument("--image", action="append", default=[])
    args = parser.parse_args(namespace=Arguments())
    if args.role == "simplify":
        if not args.session_dir:
            parser.error("simplify requires --session-dir")
        return args
    if args.role in ("research", "propose"):
        parser.error(f"{args.role} is not available until Phase 6")
    if args.target != "staff-worker" and not args.policy:
        parser.error("--policy is required for this target")
    if args.target == "consultation-pane" and not args.new_worker:
        parser.error("consultation-pane requires --new-worker")
    if args.target == "durable-design-role" and not args.parent:
        parser.error("durable-design-role requires --parent and an Astra policy")
    if args.target == "durable-design-role" and not (args.to or args.new_worker):
        parser.error("durable-design-role requires --to or --new-worker")
    if not args.prompt and args.target == "staff-worker":
        parser.error("--prompt is required")
    if args.operation == AssignmentOperation.ASTRA_REVIEW and not args.new_worker:
        parser.error("astra-review requires --new-worker")
    if args.operation == AssignmentOperation.ASTRA_REVIEW and args.role != "review":
        parser.error("astra-review requires --role review")
    if args.operation == AssignmentOperation.ASTRA_REVIEW and not args.prompt:
        parser.error("astra-review requires --prompt")
    if args.operation == AssignmentOperation.ASTRA_REVIEW and args.target != "consultation-pane" and not args.session_dir:
        parser.error("astra-review requires --session-dir")
    if args.operation != AssignmentOperation.DISPATCH and (args.lens or args.early_ready or args.style_since):
        parser.error("review stages require the dispatch operation")
    for address in (args.to, args.new_worker):
        if address and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", address):
            parser.error(f"invalid worker name: {address}")
    if args.pass_number < 1:
        parser.error("--pass must be positive")
    if args.early_ready and args.role != "review":
        parser.error("--early-ready requires review")
    if args.style_since and (args.role != "review" or args.early_ready):
        parser.error("--style-since requires a final-diff review")
    if args.style_until and not args.style_since:
        parser.error("--style-until requires --style-since")
    return args


def authorized_paths(args: Arguments, cwd: Path) -> list[str]:
    if args.role == "review":
        if args.paths:
            refuse("--paths is refused for review")
        return []
    if not args.paths or not Path(args.paths).is_file():
        refuse(f"--paths is required for {args.role}")
    paths: list[str] = []
    for line in Path(args.paths).read_text().splitlines():
        value = line.strip()
        if not value:
            continue
        path = Path(value)
        if path.is_absolute() or ".." in path.parts or not (cwd / path).resolve().is_relative_to(cwd):
            refuse(f"invalid authorized path: {value}")
        paths.append(value)
    if not paths:
        refuse("--paths must contain at least one path")
    return paths


def prepare_run(args: Arguments, cwd: Path, prompt: Path, env: dict[str, str]) -> tuple[Path, str]:
    director = staff.runtime_id()
    try:
        record = staff.initial_snapshot()
    except staff.StaffUnreadable as error:
        role = "impl" if args.role == "implement" else args.role
        print(f"staff: {role}: not opened: {error}".replace("\n", " "), file=sys.stderr)
        raise SystemExit(3) from error
    owner = record.owner_of(director)
    session = args.session_dir
    if not session:
        opened = unit_runs.open_run(director)
        if opened is not None:
            session = str(opened)
        elif unit_runs.environ("RUN_DIR"):
            session = unit_runs.environ("RUN_DIR")
        else:
            if owner is not None and owner.kind == "showrunner" and staff_worker.CompletionArtifacts(owner, Path.cwd()).protected:
                refuse("showrunner reviews require the owner's recorded run directory")
            result = call(["bash", str(SCRIPTS / "prepare_session.sh")], env, capture=True)
            session = result.stdout.strip().splitlines()[-1].removeprefix("Session ready at ")
            print(session, flush=True)
    run = Path(session).expanduser().absolute()
    owner = next((row for row in record.find(kind="showrunner") if isinstance(row.run, staff.OpenRun)
                  and row.run.path.absolute() == run), owner)
    if (owner is not None and owner.kind == "showrunner"
            and (artifacts := staff_worker.initializing_review_run_artifacts(owner, run)).protected):
        paths = worker_panes.enrolled_worker_owner(owner)
        if not isinstance(paths, worker_panes.OrdinaryWorkerOwner) and (refusal := worker_panes.review_run_refusal(owner, run, paths)):
            refuse(refusal.reason)
        _ = artifacts.authorized_path(run)
    else:
        run = run.resolve()
        run.mkdir(parents=True, exist_ok=True)
    env["UNIT_RUN_DIR"] = str(run)
    state_path = run / "progress_history_state.json"
    if not state_path.exists():
        _ = call([PYTHON, PROGRESS, "start-run", "--session-dir", str(run), "--working-dir", str(cwd)], env)
    state = read_object(state_path)
    phases = recorder_state.open_phases(state)
    stored_phase = (next((item for item in phases if item.get("id") == args.phase), None)
                    if args.phase else recorder_state.only_open_phase(state))
    last_finished = object_map(state.get("phase")) if not phases else {}
    phase = args.phase or text((stored_phase or last_finished).get("id")) or "1"
    if phase != "ad hoc" and (not re.fullmatch(r"[A-Za-z0-9_.-]+", phase) or phase in (".", "..")):
        refuse(f"invalid phase id: {phase}")
    try:
        run_open = unit_runs.run_is_open(run)
    except staff.StaffUnreadable as error:
        print(f"assign.sh: {error}", file=sys.stderr)
        raise SystemExit(3) from error
    if not run_open or not (phases or last_finished):
        identity = next((match for line in prompt.read_text().splitlines()
                         if (match := re.match(r"^Phase (\S+) — (.*)$", line))), None)
        title = identity.group(2).strip() if identity else text((stored_phase or last_finished).get("title")) or "Assignment"
        _ = call([PYTHON, PROGRESS, "start-phase", "--session-dir", str(run), "--phase-id", phase,
                  "--phase-title", title], env)
    return run, phase






@dataclass
class NoStyleAssignment:
    """This dispatch has no phase style result to capture."""


@dataclass
class StyleAssignment:
    directory: Path
    summary: Path
    captured: bool = False








@dataclass
class AssignmentResult:
    args: Arguments
    run: Path
    phase: str
    role: str
    worker: str
    env: dict[str, str]
    pass_open: bool = False
    started_at: float = field(default_factory=time.time)
    style_assignment: NoStyleAssignment | StyleAssignment = field(default_factory=NoStyleAssignment)

    def failed_pass_command(self) -> list[str]:
        return [PYTHON, PASS_RECORD, "finish-pass", "--session-dir", str(self.run), "--status", "error"]

    def capture_style(self, code: int, artifacts: staff_worker.CompletionArtifacts) -> None:
        assignment = self.style_assignment
        if isinstance(assignment, StyleAssignment) and not assignment.captured:
            style_review.finish(assignment.directory, assignment.summary, code,
                                artifacts=artifacts if artifacts.protected else style_review.StyleArtifactPolicy.STANDALONE)
            assignment.captured = True


def recorded_phase(run: Path, phase_id: str) -> tuple[dict[str, object], dict[str, object]]:
    state = read_object(run / "progress_history_state.json")
    phase = next((item for item in recorder_state.open_phases(state) if item.get("id") == phase_id),
                 dict[str, object]())
    return state, phase


def record_review(dispatch: AssignmentResult, completed: bool, summary: Path) -> None:
    """Keep each launcher's review evidence, including a recorded zero."""
    if dispatch.role != "review":
        return
    record = staff.local_snapshot()
    parent = worker_admission.parent(record, run=dispatch.run)
    member = worker_admission.find_worker(record, parent.id, dispatch.worker)
    write_review_result(dispatch.run, dispatch.phase, dispatch.worker,
        str(member.current_session) if member is not None else "", dispatch.args.lens,
        dispatch.args.pass_number, dispatch.started_at, completed, summary)


def write_review_result(run: Path, phase_id: str, worker: str, thread_id: str, lens: str,
                        pass_number: int, started_at: float, completed: bool, summary: Path) -> None:
    state, phase = recorded_phase(run, phase_id)
    raised = len(re.findall(r"^\s*\d+[.)]\s+", summary.read_text(), re.MULTILINE))
    parent = worker_admission.parent(staff.local_snapshot(), run=run)
    progress_history.write_review_turn(run, {
        "unit": str(parent.id), "phase": phase_id,
        "phase_instance_id": phase.get("instance_id", ""), "history_file": state.get("history_file", ""),
    }, reviewer=worker, thread_id=thread_id, lens=lens,
       pass_number=pass_number, started_at=int(started_at),
       status="reviewed" if completed else "error", raised=raised,
       details={"pool": "", "lease_ordinal": 0, "prime_hash": ""})


@dataclass(frozen=True)
class CompletionFailed:
    reason: str


class CompletionApplied(StrEnum):
    APPLIED = "completion actions applied"


def complete_herdr(run: Path, row: staff.StaffRow, passed: dict[str, object],
                   actions: progress_history.CompletionActions, reply: Path, *,
                   owner: staff.StaffRow, completed: bool = True) -> CompletionApplied | CompletionFailed:
    """Run the saved completion actions after the hook proves DONE."""
    artifacts = staff_worker.CompletionArtifacts(owner, run)
    summary = Path(actions.summary_file) if actions.summary_file else reply
    if actions.summary_file:
        artifacts.write(summary, artifacts.read(reply))
    if actions.style_directory:
        style_review.finish(Path(actions.style_directory), summary, 0 if completed else 1,
                            artifacts=artifacts if artifacts.protected else style_review.StyleArtifactPolicy.STANDALONE)
    phase = object_map(passed.get("phase"))
    phase_id = text(phase.get("id"))
    if passed.get("kind") == "review" and actions.summary_file:
        stamp = passed.get("started_at", 0)
        write_review_result(run, phase_id, row.name, str(row.current_session), text(passed.get("lens")),
            actions.review_pass, float(stamp) if isinstance(stamp, (int, float)) else 0, completed, summary)
    if actions.summary_file:
        shown = f"{owner.name} · {text(passed.get('kind'))}"
        content = artifacts.read(summary)
        if shown not in content:
            artifacts.write(summary, shown + "\n\n" + content)
    if actions.report_file:
        report = Path(actions.report_file)
        if report.resolve() != reply.resolve():
            artifacts.write(report, artifacts.read(summary))
    failure = ""
    if actions.resolve_fix:
        env = dict(os.environ, UNIT_RUN_DIR=str(run), UNIT_TEAM_ROLE=text(passed.get("team_slot")), UNIT_PASS_OWNER="launcher")
        ledger = [PYTHON, str(SCRIPTS / "findings.py"), "landed", "--session-dir", str(run), "--phase", phase_id]
        result = call(ledger, env, capture=True, check=False)
        if result.returncode:
            command = shlex.join(["env", f"UNIT_TEAM_ROLE={env['UNIT_TEAM_ROLE']}", "UNIT_PASS_OWNER=launcher", *ledger])
            failure = ("assign.sh: repair resolution failed; run these commands:\n" + command + "\n"
                       + failure_detail(result))
    if actions.status_file:
        artifacts.write(Path(actions.status_file), "done\n" if completed and not failure else "error\n")
    return CompletionFailed(failure) if failure else CompletionApplied.APPLIED


def finish_herdr_recording(run: Path, instance: str, actions: progress_history.CompletionActions, *,
                           status: Literal["completed", "stopped", "error"]) -> CompletionApplied | CompletionFailed:
    """Record the phase and run ends after the hook completes the pass."""
    if actions.finish_phase == "astra-review" and actions.finish_run:
        slot = progress_history.pass_for_finalization(run, instance).get("team_slot")
        private = unit_runs.read_run_fields(run).get(unit_runs.PRIVATE_HELPER_SLOT)
        if not private or private != slot:
            return CompletionApplied.APPLIED
    commands = [["finish-phase", "--phase-id", actions.finish_phase]] if actions.finish_phase else []
    if actions.finish_run:
        commands.append(["finish-run"])
    failures: list[str] = []
    for arguments in commands:
        try:
            result = call([PYTHON, PROGRESS, *arguments, "--session-dir", str(run),
                           "--status", "error" if failures and status == "completed" else status],
                          dict(os.environ), capture=True, check=False)
            if result.returncode:
                failures.append(failure_detail(result))
        except (OSError, subprocess.SubprocessError) as error:
            failures.append(str(error))
    return CompletionFailed("\n".join(failures)) if failures else CompletionApplied.APPLIED


def finish_repair(dispatch: AssignmentResult, code: int) -> bool:
    ledger = [PYTHON, str(SCRIPTS / "findings.py"), "landed" if code == 0 else "abandon",
              "--session-dir", str(dispatch.run), "--phase", dispatch.phase]
    if code:
        ledger += ["--edits-landed", "--reason", f"the fix worker exited with code {code}"]
    failed = call(ledger, dispatch.env, check=False).returncode != 0
    commands = [ledger]
    if dispatch.pass_open and (code or failed):
        commands.append(dispatch.failed_pass_command())
        failed = call(commands[-1], dispatch.env, check=False).returncode != 0 or failed
    if failed:
        print("assign.sh: repair resolution failed; run these commands:", file=sys.stderr)
        for command in commands:
            print(shlex.join(["env", f"UNIT_TEAM_ROLE={dispatch.env.get('UNIT_TEAM_ROLE', dispatch.worker)}", "UNIT_PASS_OWNER=launcher", *command]), file=sys.stderr)
    dispatch.pass_open = False
    return not failed


def settle_dispatch(dispatch: AssignmentResult, code: int, completed: bool, shown: str,
                    status: Path, summary: Path, reply: Path) -> int:
    """Completed work wins; otherwise ownership loss precedes refusal or failure."""
    owner = worker_admission.parent(staff.local_snapshot(), run=dispatch.run)
    artifacts = staff_worker.CompletionArtifacts(owner, dispatch.run)
    if dispatch.args.early_ready:
        lens_suffix = f"_{dispatch.args.lens}" if dispatch.args.lens else ""
        artifacts.write(status.parent / f"{dispatch.worker}_status{lens_suffix}", "reviewed\n" if completed else "error\n")
    if completed and reply.exists():
        artifacts.write(summary, artifacts.read(reply))
    dispatch.capture_style(0 if completed else code or 1, artifacts)
    record_review(dispatch, completed, summary)
    content = artifacts.read(summary)
    if shown not in content:
        content = shown + "\n\n" + content
        artifacts.write(summary, content)
    if dispatch.args.reply_file:
        artifacts.write(Path(dispatch.args.reply_file), content)
    if not 3 <= code <= 8:
        if dispatch.role == "fix" and dispatch.env.get("UNIT_RESOLVES_ROUND") == "1":
            if not finish_repair(dispatch, 0 if completed else code or 1):
                completed, code = False, 1
        elif dispatch.pass_open and not completed:
            _ = call(dispatch.failed_pass_command(), dispatch.env)
            dispatch.pass_open = False
    artifacts.write(status, "done\n" if completed else "error\n")
    return 0 if completed else code if 3 <= code <= 8 else 2 if code == 2 else 1


def resolve_model(args: Arguments, role: str, env: dict[str, str]) -> tuple[str, str, str, str]:
    task = role if "." in role else f"direct.{role}"
    registry = models_registry.registry_path(env)
    try:
        launch_configuration = models_registry.launch_configuration(env, registry=registry)
    except (OSError, ValueError) as error:
        refuse(str(error))
    result = call(["bash", "-c", '''source "$1" &&
{ IFS= read -r -d "" table; _models_table_load "$table"; } || exit 2
export MODELS_LAUNCH_CONFIGURATION MODELS_CONFIG_FILE
agents_resolve "$2" || exit 2
if [[ -n "$3" || -n "$4" ]]; then
 pair="${3:-$AGENT_WORD}"
 [[ "$pair" == *:* ]] || pair="$pair:${4:-$AGENT_EFFORT}"
 [[ -z "$4" ]] || pair="${pair%%:*}:$4"
 _agents_validate_pair "$2" "$AGENT_FAMILY" "$pair" || exit 2
fi
printf '%s\\n' "$AGENT_FAMILY" "$AGENT_MODEL" "$AGENT_EFFORT" "${AGENT_SERVICE_TIER:-default}" "$MODELS_LAUNCH_CONFIGURATION"
''', "assign", str(SCRIPTS / "../agents/models_config.sh"), task, args.model, args.effort], env,
        capture=True, input=models_registry.configuration_shell(launch_configuration))
    family, model, effort, tier, configuration = result.stdout.strip().splitlines()[-5:]
    env["MODELS_LAUNCH_CONFIGURATION"] = configuration
    return family, model, effort, tier

def image_note(images: list[str]) -> str:
    """Name each attached image for a worker that cannot receive it as an option."""
    if not images:
        return ""
    return "\nRead these images:\n" + "\n".join(str(Path(image).resolve()) for image in images)




def launcher_command() -> list[str]:
    """Name this launch the way the recorder saves it for a repeated launcher."""
    return ["bash", str(SCRIPTS / "assign.sh"), *sys.argv[1:]]


@dataclass(frozen=True)
class ProtectedLauncherStatus:
    artifacts: staff_worker.CompletionArtifacts
    slot: str
    path: Path


class RecorderLauncherStatus(StrEnum):
    RECORDER = "the recorder owns the launcher status"


def record_worker_launch_exit(run: Path, instance: uuid.UUID, outcome: progress_history.AssignmentLaunchExit, *,
                              status: ProtectedLauncherStatus | RecorderLauncherStatus = RecorderLauncherStatus.RECORDER,
                              ) -> progress_history.RecordedAssignmentLaunch | progress_history.AssignmentLaunchNotRecorded:
    """Wait for the worker to finish confirming submission before recording a failure."""
    if outcome is progress_history.AssignmentLaunchExit.SUBMITTED:
        return progress_history.record_assignment_launch_exit(run, instance, outcome)
    target = progress_history.assignment_launch_worker_id(run, instance)
    if isinstance(target, progress_history.AssignmentLaunchNotRecorded):
        return target
    with worker_panes.worker_id_lock(target, blocking=True):
        if isinstance(status, ProtectedLauncherStatus):
            record = staff.local_snapshot()
            row = record.by_id(target)
            if row is None:
                raise OSError(f"{target}: launcher status owner is unavailable")
            status.artifacts.validate_status(worker_panes.worker_pass(row, run, record=record))
        result = progress_history.record_assignment_launch_exit(run, instance, outcome)
        if (isinstance(status, ProtectedLauncherStatus)
                and isinstance(result, progress_history.RecordedAssignmentLaunch) and result.failure_recorded):
            status.artifacts.write(progress_history.worker_artifact(status.path, status.slot, result.slot), "error\n")
        return result


def post_launch_board(artifacts: staff_worker.CompletionArtifacts, slot: str, kind: Literal["register", "blocked"],
                      message: str, env: dict[str, str]) -> None:
    if not artifacts.protected:
        _ = call(["bash", str(SCRIPTS / "board.sh"), "post", str(artifacts.run), slot, kind, message], env)
        return
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", slot) or slot in (".", ".."):
        raise ValueError(f"invalid board slot: {slot}")
    override = env.get("UNIT_NOW_EPOCH")
    zone = user_zone()
    stamp = (datetime.fromtimestamp(int(override.split(".")[0]), zone)
             if override else datetime.now(zone)).strftime("%Y-%m-%dT%H:%M:%S%z")
    flattened = message.translate(str.maketrans("\n\r\t", "   "))
    body = "".join(character for character in flattened if character.isprintable())[:900]
    with artifacts.append(artifacts.run / "board.log") as stream:
        _ = stream.write(f"{stamp} [{slot}] {kind}: {body}\n")


def assignment_prompt(args: Arguments, run: Path, phase: str, role: staff.WorkerRole, slot: str,
                      launcher: list[str], alias: Path, content: str,
                      summary: Path, artifacts: staff_worker.CompletionArtifacts
                      ) -> tuple[Path, NoStyleAssignment | StyleAssignment]:
    """Reuse the recorded prompt or publish a new immutable copy."""
    selected = progress_history.launch_slot(run, slot, launcher)
    state = read_object(run / unit_runs.STATE_FILE)
    passed = object_map(object_map(state.get("pass")).get(selected))
    saved_path = text(passed.get("prompt_file"))
    if saved_path:
        saved = Path(saved_path)
        style_path = progress_history.dispatch_delivery(passed).actions.style_directory
        style = StyleAssignment(Path(style_path), summary) if style_path else NoStyleAssignment()
        if bool(style_path) == bool(args.style_since) and saved.is_file():
            expected = content
            if isinstance(style, StyleAssignment):
                addition = artifacts.read(style.directory / "assignment.md")
                if addition:
                    expected += "\n" + addition
            if artifacts.read(saved) == expected:
                matched = progress_history.dispatch_worker(run, slot=slot, phase=phase, role=role,
                    prompt=saved.resolve(), launcher_command=launcher,
                    work=progress_history.DispatchWork("assign", args.lens, args.pass_number if role == "fix" else 0))
                if isinstance(matched, progress_history.MatchedDispatch):
                    return saved, style

    phase_instance = next((text(item.get("instance_id")) for item in recorder_state.open_phases(state)
                           if item.get("id") == phase), "")
    digest = hashlib.sha256(json.dumps([phase_instance, launcher, content]).encode("utf-8")).hexdigest()
    message = alias.parent / "prompts" / f"{digest}.txt"
    try:
        artifacts.write(message, content, create_only=True)
    except FileExistsError:
        pass
    if artifacts.read(message) != content:
        refuse(f"Saved prompt differs from the assignment: {message}")
    if alias != Path(saved_path) and (not alias.is_file() or artifacts.read(alias) != content):
        artifacts.write(alias, content)
    if args.style_since:
        end = style_review.CommitEnd(args.style_until) if args.style_until else style_review.WORKING_TREE
        directory = style_review.start(Path(args.cwd), args.style_since, alias.parent, message, slot, end,
            artifacts=artifacts if artifacts.protected else style_review.StyleArtifactPolicy.STANDALONE)
        return directory / "prompt.md", StyleAssignment(directory, summary)
    return message, NoStyleAssignment()


def execute_herdr(args: Arguments, run: Path, phase: str, prompt: Path,
                  role: staff.WorkerRole, paths: list[str], env: dict[str, str]) -> int:
    """Check the worker before publishing assignment files."""
    record = staff.snapshot()
    try:
        owner = worker_admission.parent(record, run=run)
        artifacts = staff_worker.CompletionArtifacts(owner, run)
        enrolled = artifacts.protected
        if enrolled:
            if refusal := worker_panes.review_role_refusal(owner, role):
                refuse(refusal.reason)
            try:
                progress_history.check_assigned_review_launch(run, phase, prompt, args.lens)
            except staff.StaffUnreadable:
                raise
            except (OSError, ValueError, SystemExit) as error:
                refuse(str(error))
    except staff.StaffUnreadable as error:
        print(f"staff: {role}: not opened: {error}".replace("\n", " "), file=sys.stderr)
        return 3
    directory = phase_reservation.phase_directory(run, phase)
    launcher = launcher_command()
    named = args.to or args.new_worker
    slot = named or role
    suffix = f"_{args.lens}_{args.pass_number}" if args.lens else f"_{args.pass_number}" if role == "review" else ""
    content = prompt.read_text() + "\n" + RULES + "\nAuthorized paths:\n" + "\n".join(paths)
    content += image_note(args.image)
    if role == "review":
        content += "\nReturn the complete review result in your final reply.\n"
    state = read_object(run / unit_runs.STATE_FILE)
    parent = unit_runs.run_owner(state, record)
    if isinstance(parent, staff.StaffRow):
        work = progress_history.DispatchWork("assign", args.lens, args.pass_number if role == "fix" else 0)
        saved_slot = progress_history.launch_slot(run, slot, launcher)
        passed = pass_recorder.pass_slots(state).get(saved_slot, {})
        saved_path = Path(text(passed.get("prompt_file")))
        saved_command = passed.get("launcher_command")
        same_launcher = isinstance(saved_command, list) and (
            saved_command == launcher or (passed.get("status") in ("active", "refused")
                and pass_recorder.worker_launcher(cast(list[str], saved_command),
                                                  text(passed.get("worker_name"))) == launcher))
        proposed = content
        candidate = directory / f"{slot}_prompt{suffix}.txt"
        style_directory = progress_history.dispatch_delivery(passed).actions.style_directory
        generated_prompt = (
            saved_path == Path(style_directory) / "prompt.md" if style_directory
            else saved_path.parent == directory / "prompts" or saved_path == directory / f"{saved_slot}_prompt{suffix}.txt"
        )
        if (same_launcher and generated_prompt and saved_path.is_file()
                and bool(style_directory) == bool(args.style_since)):
            candidate = saved_path
            if style_directory:
                addition = artifacts.read(Path(style_directory) / "assignment.md")
                if addition:
                    proposed += "\n" + addition
        retry = staff_worker.admit_dispatch(parent.id, progress_history.DispatchTargetAtUse.FROM_LAUNCHER,
            role, candidate, session_dir=run, slot=slot, phase=phase, launcher_command=launcher,
            work=work, prompt_source=proposed.encode("utf-8"), record=record)
        if isinstance(retry, staff_worker.ExitCode):
            return retry
        if named:
            chosen = staff.WorkerName.parse(named)
        elif isinstance(retry.action, pass_recorder.MatchedDispatch):
            chosen = staff.WorkerName.parse(retry.action.worker_name)
        else:
            chosen = staff_worker.select_worker(parent.id, role, record, additional_runs=(run,))
        if not isinstance(chosen, staff.WorkerName):
            return staff_worker.startup_result(staff.WorkerName.parse(role), chosen)
        slot = chosen.value
        if slot != saved_slot:
            candidate = directory / f"{slot}_prompt{suffix}.txt"
            proposed = content
        admission = staff_worker.admit_dispatch(parent.id, chosen, role, candidate, session_dir=run, slot=slot,
            phase=phase, launcher_command=launcher, work=work, prompt_source=proposed.encode("utf-8"), record=record)
        if isinstance(admission, staff_worker.ExitCode):
            return admission
    if not enrolled:
        check = [PYTHON, PASS_RECORD, "check-launch", "--session-dir", str(run), "--pass-kind", role,
                 "--phase-id", phase, "--prompt-file", str(prompt), "--fix-cause", args.fix_cause]
        if args.lens:
            check += ["--lens", args.lens]
        checked = call(check, env, check=False)
        if checked.returncode:
            return 3 if checked.returncode == 3 else 2
    with artifacts.append(run / remove_run_scratch.ASSIGNMENT_PROMPT_LOCK) as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_SH)
        return _execute_herdr(args, run, phase, role, paths, env, lock.fileno(), artifacts,
                              directory=directory, launcher=launcher, slot=slot, suffix=suffix, content=content)


def _execute_herdr(args: Arguments, run: Path, phase: str, role: staff.WorkerRole, paths: list[str],
                   env: dict[str, str], prompt_lock: int, artifacts: staff_worker.CompletionArtifacts, *,
                   directory: Path, launcher: list[str], slot: str,
                   suffix: str, content: str) -> int:
    enrolled = artifacts.protected
    artifacts.mkdir(directory)
    message = directory / f"{slot}_prompt{suffix}.txt"
    summary = directory / f"{slot}_summary{suffix}.txt"
    status = directory / f"{slot}_status{suffix}"
    reply = directory / f"{slot}_reply{suffix}.txt"
    log = directory / f"{slot}_agent{suffix}.log"
    message, style = assignment_prompt(args, run, phase, role, slot, launcher, message, content, summary, artifacts)
    env["UNIT_RUN_DIR"] = str(run)
    env["UNIT_TEAM_ROLE"] = slot
    if role != "review" and not assign_claude.claim_phase(run, phase, Path(args.cwd).expanduser().resolve(),
            log, assign_claude.UnclaimedPhaseStatus.PRESERVE, slot, env):
        return 1
    command = [PYTHON, str(SCRIPTS / "../production/worker.py"), "assign", role, str(message),
               "--session-dir", str(run), "--phase", phase, "--slot", slot, "--kind", "assign", "--reply", str(reply),
               "--lens", args.lens, "--fix-cause", args.fix_cause,
               "--fix-pass", str(args.pass_number if role == "fix" else 0)]
    if args.to or args.new_worker:
        command += ["--name", args.to or args.new_worker]
    if args.model or args.effort:
        selection = resolve_model(args, role, env)
        command += ["--model", selection[1], "--effort", selection[2]]
    if args.early_ready:
        command += ["--early-ready", str(Path(args.early_ready).absolute() if enrolled else Path(args.early_ready).resolve())]
    number = sum(entry.get("event_type") == "pass_started" for entry in recorded_passes(run)) + 1
    artifacts.write(directory / f"dispatch_{number}_paths.json",
                      json.dumps({"worker": slot, "round": number, "paths": paths}))
    actions = pass_recorder.CompletionActions(summary_file=str(summary), report_file=args.reply_file,
        style_directory=str(style.directory) if isinstance(style, StyleAssignment) else "",
        resolve_fix=role == "fix" and env.get("UNIT_RESOLVES_ROUND") == "1", review_pass=args.pass_number)
    worker_env = dict(env)
    if enrolled:
        _ = worker_env.pop(progress_history.ASSIGN_STATUS_FILE_ENV, None)
        actions = replace(actions, status_file=str(status))
    else:
        worker_env[progress_history.ASSIGN_STATUS_FILE_ENV] = str(status)
    instance = uuid.uuid4()
    command += ["--launcher-command", json.dumps(launcher), "--completion-actions", json.dumps(asdict(actions)),
                "--new-pass-instance-id", str(instance)]
    post_launch_board(artifacts, slot, "register", f"role={role}; status in {status}", env)
    if enrolled:
        stamp = datetime.now(user_zone()).strftime("%Y-%m-%dT%H:%M:%S%z")
        with artifacts.append(run / "heartbeat.log") as stream:
            _ = stream.write(f"---- {stamp} [{slot}] ----\n{role}\n")
    else:
        _ = call(["bash", str(SCRIPTS / "../agents/heartbeat.sh"), str(run / "heartbeat.log"),
                  "header", slot, role], env, check=False)
    with artifacts.append(log) as output, TemporaryFile(mode="w+t") as diagnostics:
        offset = output.tell()
        result = subprocess.run(command, env=worker_env, text=True, stdout=output, stderr=diagnostics,
                                pass_fds=(prompt_lock,))
        _ = diagnostics.seek(0)
        errors = diagnostics.read()
        end = output.tell()
        _ = output.write(errors)
    with artifacts.open_read(log) as output:
        _ = output.seek(offset)
        while output.tell() < end:
            line = output.readline()
            if not line:
                break
            if result.returncode:
                print(line, end="", file=sys.stderr)
            elif line.rstrip().endswith(staff_worker.COMPACTION_STARTED_TEXT):
                print(line, end="")
    if result.returncode and errors:
        print(errors, end="", file=sys.stderr)
    if result.returncode == staff_worker.ExitCode.UNCERTAIN:
        return result.returncode
    outcome = progress_history.AssignmentLaunchExit.SUBMITTED
    if result.returncode:
        outcome = (progress_history.AssignmentLaunchExit.CLOSE_FAILED_PASS if result.returncode in (1, 2)
                   else progress_history.AssignmentLaunchExit.FAILED)
    launcher_status = ProtectedLauncherStatus(artifacts, slot, status) if enrolled else RecorderLauncherStatus.RECORDER
    recorded = record_worker_launch_exit(run, instance, outcome, status=launcher_status)
    selected, failure_recorded = slot, False
    if isinstance(recorded, progress_history.RecordedAssignmentLaunch):
        selected, failure_recorded = recorded.slot, recorded.failure_recorded
        if result.returncode and not failure_recorded:
            return result.returncode
    elif not result.returncode:
        selected = progress_history.launch_slot(run, slot, launcher)
    if selected != slot:
        target_log = progress_history.worker_artifact(log, slot, selected)
        artifacts.rename(log, target_log)
        artifacts.write(directory / f"dispatch_{number}_paths.json",
                          json.dumps({"worker": selected, "round": number, "paths": paths}))
    if result.returncode:
        if (failure_recorded and outcome is progress_history.AssignmentLaunchExit.CLOSE_FAILED_PASS
                and role == "fix" and env.get("UNIT_RESOLVES_ROUND") == "1"):
            env["UNIT_PASS_OWNER"] = "launcher"
            failed = AssignmentResult(args, run, phase, role, selected, env, style_assignment=style)
            _ = finish_repair(failed, result.returncode)
        if isinstance(style, StyleAssignment) and failure_recorded:
            style_review.finish(style.directory, summary, result.returncode,
                                artifacts=artifacts if enrolled else style_review.StyleArtifactPolicy.STANDALONE)
        post_launch_board(artifacts, selected, "blocked", f"role={role}; exit {result.returncode}; log {log}", env)
    return result.returncode or staff_worker.DispatchSubmission.SUBMITTED



def require_live_director() -> None:
    """Refuse launchers without a live Claude or Codex ancestor."""
    try:
        pid = os.getppid()
        seen: set[int] = set()
        while pid > 1 and pid not in seen:
            seen.add(pid)
            result = subprocess.run(["ps", "-p", str(pid), "-o", "ppid=,stat=,comm="],
                                    capture_output=True, text=True, check=True)
            parent, state, command = result.stdout.strip().split(None, 2)
            if state.startswith("Z"):
                break
            if Path(command).name in {"claude", "codex"}:
                os.kill(pid, 0)
                return
            pid = int(parent)
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    refuse("assign.sh requires a live Claude or Codex ancestor; run it from the director's session shell")










def pane_policy(name: str, *, model: str = "", effort: str = "") -> worker_panes.LaunchPolicy:
    """Resolve one explicit launch policy and its model overrides."""
    try:
        if name not in ("astra-reviewer", "astra-designer"):
            refuse(f"unknown pane policy: {name}")
        task = "design_roles." + name.removeprefix("astra-")
        policy = worker_panes.launch_policy(cast(staff.WorkerRole, name))
        if model or effort:
            family, selected_model, selected_effort, tier = resolve_model(
                Arguments(model=model, effort=effort), task, dict(os.environ))
            settings = models_registry.LaunchSettings(cast(Literal["claude", "codex"], family),
                selected_model, selected_effort, tier)
            policy = replace(policy, settings=settings)
        return policy
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        refuse(f"{worker_panes.StartRefusal.INVALID_ROLE_CONFIGURATION.value}: {error}")


@overload
def launch(target: worker_panes.ConsultationPane, policy: worker_panes.LaunchPolicy) -> side_pane.SidePane: ...


@overload
def launch(target: worker_panes.StaffWorker | worker_panes.DurableDesignRole,
           policy: worker_panes.LaunchPolicy, *,
           lock: worker_panes.WorkerLockHold | worker_panes.WorkerLockAtUse = worker_panes.WorkerLockAtUse.ACQUIRE,
           ) -> worker_panes.WorkerStart: ...


def launch(target: worker_panes.PaneTarget, policy: worker_panes.LaunchPolicy, *,
           lock: worker_panes.WorkerLockHold | worker_panes.WorkerLockAtUse = worker_panes.WorkerLockAtUse.ACQUIRE,
           ) -> side_pane.SidePane | worker_panes.WorkerStart:
    """Start the named target with the supplied policy."""
    if isinstance(target, worker_panes.ConsultationPane):
        return side_pane.open(target.label, policy, question=target.question, cwd=target.cwd, delivery=target.delivery)
    try:
        started = staff_worker.launch(target, policy, lock=lock)
    except staff.StaffRefused as error:
        started = worker_panes.AdmissionRefused(str(error))
    if isinstance(started, worker_panes.AdmissionRefused):
        return worker_panes.AdmissionRefused(" ".join(started.reason.splitlines()))
    return started


def execute_consultation(args: Arguments, cwd: Path) -> int:
    """Start a consultation without a staff row."""
    opened: side_pane.AbsentSidePane | side_pane.OpenSidePane = side_pane.AbsentSidePane()
    result = 1
    try:
        question = ""
        if args.prompt:
            prompt = Path(args.prompt).expanduser().resolve()
            question = f"Read {prompt}.\n{prompt.read_text()}"
        review = args.operation == AssignmentOperation.ASTRA_REVIEW
        policy = (worker_panes.astra_review_policy(args.model, args.effort) if review else
                  pane_policy(args.policy, model=args.model, effort=args.effort))
        delivery = worker_panes.TranscriptReply() if review else worker_panes.ReplyToCaller()
        current = launch(worker_panes.ConsultationPane(args.new_worker, cwd, question, delivery), policy)
        if isinstance(current, side_pane.UnknownSidePane):
            return worker_panes.ExitCode.UNCERTAIN
        if not isinstance(current, side_pane.OpenSidePane):
            raise side_pane.SidePaneRefused("consultation is not running")
        if args.operation != AssignmentOperation.ASTRA_REVIEW:
            print("done")
            return 0
        opened = current
        client = herdr.Herdr(herdr.shared_session())
        deadline = time.monotonic() + herdr.PROGRAM_TIMEOUT_SECONDS
        while True:
            try:
                thread = client.native_session(current.pane, "codex", max(0, deadline - time.monotonic()))
                break
            except (herdr_types.HerdrFailure, OSError, ValueError, subprocess.SubprocessError):
                if time.monotonic() >= deadline:
                    raise
                _ = threading.Event().wait(max(0, min(0.1, deadline - time.monotonic())))
        process = client.pane_agent_process(current.pane, current.terminal)
        home = Path(herdr.process_environment(process.pid).get("CODEX_HOME") or Path.home() / ".codex")
        source = worker_admission.codex_transcript_source(thread, home)
        reply = staff_worker.consultation_reply(client, current.pane, source, question)
        write_text_atomic(Path(args.reply_file), reply + "\n")
        result = 0
    except side_pane.SidePaneRefused as error:
        print(str(error), file=sys.stderr)
        result = worker_panes.ExitCode.REFUSED
    except (herdr_types.HerdrFailure, conf.Unusable, OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"assign.sh: consultation unavailable: {error}", file=sys.stderr)
        result = worker_panes.ExitCode.UNCERTAIN
    finally:
        if isinstance(opened, side_pane.OpenSidePane):
            try:
                closed = side_pane.close(args.new_worker)
                if isinstance(closed, side_pane.SidePaneCloseUnavailable):
                    print(f"assign.sh: consultation close unproved: {closed.reason}; "
                          + f"pane {opened.pane} ({args.new_worker}) stays open", file=sys.stderr)
                    result = 1
            except (herdr_types.HerdrFailure, OSError, ValueError, subprocess.SubprocessError) as error:
                print(f"assign.sh: consultation close failed: {error}", file=sys.stderr)
                result = 1
    return result


def execute_durable(args: Arguments) -> int:
    """Start or dispatch a durable role through its explicit target."""
    name = staff.WorkerName.parse(args.to or args.new_worker)
    role = cast(staff.WorkerRole, args.policy)
    target = worker_panes.DurableDesignRole(staff.StaffId(args.parent), name, role)
    policy = pane_policy(args.policy, model=args.model, effort=args.effort)
    if not args.prompt:
        current = launch(target, policy)
        if not isinstance(current, worker_panes.OpenWorker):
            return staff_worker.startup_result(name, current)
        print(json.dumps(current.row.to_json()))
        return 0
    prompt = Path(args.prompt).expanduser().resolve()
    if not prompt.is_file() or not args.session_dir or not args.phase:
        refuse("durable dispatch requires --prompt, --session-dir, and --phase")
    result = staff_worker.dispatch(target.parent, name, role, prompt, session_dir=Path(args.session_dir),
        slot=name.value, phase=args.phase, kind="astra",
        reply=Path(args.reply_file) if args.reply_file else staff_worker.REPLY_BESIDE_PROMPT, policy=policy,
        completion_actions=pass_recorder.dispatch_delivery({"delivery": {"actions": cast(object, json.loads(args.completion_actions))}}).actions,
        launcher_command=launcher_command())
    return result


def execute_astra_review_herdr(args: Arguments, run: Path, cwd: Path, prompt: Path,
                              env: dict[str, str]) -> int:
    """Submit one held reviewer and retain its role until completion."""
    command = launcher_command()
    matched = progress_history.dispatch_worker(run, slot=args.new_worker, phase="astra-review", role="astra-reviewer",
                                               prompt=prompt, launcher_command=command)
    if (isinstance(matched, pass_recorder.MatchedDispatch)
            and progress_history.dispatch_completed(run, args.new_worker, matched.instance_id)):
        parent = worker_admission.parent(staff.snapshot(), run=run)
        passed = object_map(object_map(read_object(run / unit_runs.STATE_FILE).get("pass")).get(args.new_worker))
        retry = cast(Callable[[str, staff.StaffRow, Path, dict[str, object]], Callable[[], None]],
                     import_module("scripts.herdr.turn_end").retry_completed_notice)
        retry(args.new_worker, parent, run, passed)()
        return staff_worker.DispatchSubmission.SUBMITTED
    state = unit_runs.read_run_fields(run)
    start = ["start-run", "--session-dir", str(run), "--working-dir", str(cwd)]
    if (not state and run.resolve().parent == unit_runs.ASTRA_RUN_ROOT.resolve()
            and not any(unit_runs.points_to_run(row, run) for row in staff.initial_snapshot().rows)):
        start += ["--private-helper", args.new_worker]
    for arguments in (start,
                      ["start-phase", "--session-dir", str(run), "--phase-id", "astra-review",
                       "--phase-title", "Astra review"]):
        recorded = subprocess.run([PYTHON, PROGRESS, *arguments], env=env, text=True,
                                  stderr=subprocess.STDOUT, check=False)
        if recorded.returncode:
            return recorded.returncode
    parent = worker_admission.parent(staff.snapshot(), run=run)
    private_helper = unit_runs.read_run_fields(run).get(unit_runs.PRIVATE_HELPER_SLOT) == args.new_worker
    target = worker_panes.StaffWorker(parent.id, staff.WorkerName.parse(args.new_worker), "astra-reviewer")
    policy = pane_policy(args.policy or "astra-reviewer", model=args.model, effort=args.effort)
    before = staff_worker.rows_before_launch()
    with ExitStack() as notices, worker_panes.worker_lock(target.parent, target.name) as hold, ExitStack() as cleanup:
        if isinstance(hold, worker_panes.Kept):
            print(hold.blocker, file=sys.stderr)
            return staff_worker.ExitCode.BUSY
        current = launch(target, policy, lock=hold)
        staff_worker.arm_launch_cleanup(cleanup, before, current)
        if not isinstance(current, worker_panes.OpenWorker):
            return staff_worker.startup_result(target.name, current)
        try:
            client = herdr.Herdr(herdr.shared_session())
            process = client.pane_agent_process(current.pane, current.terminal)
            role = astra_roles.hold("reviewer", astra_roles.ExecProcess(process.pid), str(current.row.id))
            if isinstance(role, astra_roles.HoldWorkerUnavailable):
                return 1
            result = staff_worker.dispatch(target.parent, target.name, target.role, prompt,
                session_dir=run, slot=args.new_worker, phase="astra-review", kind="astra",
                reply=Path(args.reply_file), policy=policy, started=current,
                completion_actions=progress_history.CompletionActions(end_worker=True, held_role=role["staff_id"],
                    finish_phase="astra-review" if private_helper else "", finish_run=private_helper),
                launcher_command=launcher_command(), on_submission=cleanup.pop_all, lock=hold, notices=notices)
            if result is staff_worker.DispatchSubmission.SUBMITTED:
                _ = cleanup.pop_all()
            return result
        except (OSError, ValueError) as error:
            print(f"assign.sh: reviewer role unavailable: {error}", file=sys.stderr)
            return 1


def execute_astra_review(args: Arguments, cwd: Path, prompt: Path, env: dict[str, str]) -> int:
    """Publish, hold, review, and close one managed Astra worker."""
    run = Path(args.session_dir).expanduser().absolute()
    return execute_astra_review_herdr(args, run, cwd, prompt, env)


def recorded_passes(run: Path) -> list[dict[str, object]]:
    state = read_object(run / unit_runs.STATE_FILE)
    passes = [{**object_map(value), "team_slot": slot}
              for slot, value in object_map(state.get("pass")).items()]
    history = text(state.get("history_file"))
    if history:
        for line in Path(history).read_text().splitlines():
            entry = object_map(cast(object, json.loads(line)))
            if entry.get("event_type") in ("pass_started", "pass_finished"):
                passes.append(entry)
    return passes


def submission_result(result: int) -> int:
    """Publish the named native submission outcome at the command boundary."""
    if result is staff_worker.DispatchSubmission.SUBMITTED:
        print("submitted")
    return result


def main() -> int:
    require_live_director()
    args = parse_arguments()
    if quota_state.no_worker_mode():
        refuse(quota_state.NO_WORKER_START)
    if args.role == "simplify":
        return simplify_daily.main(["--session-dir", args.session_dir, "--cwd", args.cwd])
    env = dict(os.environ)
    if args.target == "consultation-pane":
        cwd = Path(args.cwd).expanduser().resolve()
        if not cwd.is_dir():
            refuse("--cwd must be a directory")
        return execute_consultation(args, cwd)
    if args.target == "durable-design-role":
        return submission_result(execute_durable(args))
    cwd, prompt = Path(args.cwd).expanduser().resolve(), Path(args.prompt).expanduser().resolve()
    if not cwd.is_dir() or not prompt.is_file():
        refuse("--cwd must be a directory and --prompt must be a readable file")
    if args.operation == AssignmentOperation.ASTRA_REVIEW:
        return submission_result(execute_astra_review(args, cwd, prompt, env))
    paths = authorized_paths(args, cwd)
    run, phase = prepare_run(args, cwd, prompt, env)
    role = "impl" if args.role == "implement" else cast(staff.WorkerRole, args.role)
    return submission_result(execute_herdr(args, run, phase, prompt, role, paths, env))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except recorder_state.OldRecorderState as error:
        raise SystemExit(str(error)) from error
