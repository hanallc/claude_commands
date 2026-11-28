"""Run checkpoint tests and record and deliver bump gate results."""
from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import subprocess
import tomllib
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from itertools import takewhile
from pathlib import Path
from typing import Literal, NamedTuple, TypedDict, cast

from scripts.build_queue.first_step import FirstStepRefused, gate_targets
from scripts.copies import copies
from scripts.lib.atomic_file import write_text_atomic
from scripts.lib.build_steps import GATE_PRELAUNCH_REFUSED_STATUS, GATE_SCRIPTS, GATE_UNSETTLED_LAUNCH_STATUS, command_program
from scripts.lib.cargo_target_config import cargo_home_folder, cargo_workspace_manifest
from scripts.lib.process_failure import failure_detail
from scripts.message import session_send
from scripts.production import showrunners, staff
from scripts.production.production_doc import ProductionDocument

TREE_CHANGED_STATUS = 77


class Stop(Exception):
    def __init__(self, step: str, state: str, detail: str, message: str) -> None:
        super().__init__(detail)
        self.step: str = step
        self.state: str = state
        self.detail: str = detail
        self.message: str = message


def report(step: str, state: str, detail: str) -> None:
    print(f"{step}: {state} — {detail}", flush=True)


def tool(name: str, fallback: Path, override: str) -> str:
    return os.environ.get(override) or shutil.which(name) or str(fallback)


type PromotionCiPolicy = Literal["cargo-liner", "standard"]


class CiRecord(NamedTuple):
    merges_collected: int
    tip: str
    policy: PromotionCiPolicy = "standard"


class NoCiRecord(NamedTuple):
    pass


class InvalidCiRecord(ValueError):
    pass


def read_ci_record(state_dir: Path) -> CiRecord | NoCiRecord:
    path = state_dir / "ci_points.json"
    try:
        raw = cast(object, json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return NoCiRecord()
    if not isinstance(raw, dict):
        raise InvalidCiRecord(f"invalid {path}")
    fields = cast(dict[str, object], raw)
    count, tip = fields.get("count"), fields.get("tip")
    if not isinstance(count, int) or isinstance(count, bool) or count < 0 or not isinstance(tip, str):
        raise InvalidCiRecord(f"invalid {path}")
    policy = fields.get("policy", "standard")
    if policy not in ("standard", "cargo-liner"):
        raise InvalidCiRecord(f"invalid {path}")
    return CiRecord(count, tip, "cargo-liner" if policy == "cargo-liner" else "standard")


def require_promotion_ci(owner: staff.StaffRow, tip: str, policy: PromotionCiPolicy) -> None:
    """Require a collected verdict for the commit that will be published."""
    state_dir = showrunners.session_state_dir(owner.id) / "waiting_state"
    try:
        record = read_ci_record(state_dir)
    except (OSError, ValueError) as error:
        raise Stop("promote", "held", str(error), "collect CI and retry promotion") from error
    if not isinstance(record, CiRecord) or record.policy != policy or record.tip != tip:
        detail = (f"collect cargo-liner CI on the integration tip before promoting {tip}"
                  if policy == "cargo-liner" else f"collect {owner.name} CI on {tip} before promotion")
        raise Stop("promote", "held", detail, "collect CI and retry promotion")


class CheckpointTestStep(NamedTuple):
    label: str
    command: list[str]
    folder: Path


class RedStep(NamedTuple):
    """A red command and the tests or gate modules its output names; empty names mean the whole command."""

    step: CheckpointTestStep
    names: frozenset[str]


class RedCheckpointTests(Stop):
    """The unresolved commands that prevent a checkpoint publication."""

    def __init__(self, detail: str, message: str, red: tuple[RedStep, ...]) -> None:
        super().__init__("red", "failed", detail, message)
        self.red: tuple[RedStep, ...] = red


class CargoPackage(NamedTuple):
    folder: Path
    name: str


class CargoWorkspace(NamedTuple):
    pass


def read_cargo_package(folder: Path) -> CargoPackage | CargoWorkspace:
    manifest = tomllib.loads((folder / "Cargo.toml").read_text(encoding="utf-8"))
    if "package" not in manifest:
        # A workspace root has no package tests.
        return CargoWorkspace()
    return CargoPackage(folder, cast(str, manifest["package"]["name"]))


def test_steps(production: ProductionDocument, paths: tuple[str, ...],
               merge_tests: tuple[tuple[str, ...], ...]) -> list[CheckpointTestStep]:
    checkout = production.checkout
    packages: set[CargoPackage] = set()
    examples: set[tuple[str, str]] = set()
    for raw in paths:
        path = checkout / raw
        for parent in (path.parent, *path.parents):
            if parent == checkout.parent:
                break
            manifest = parent / "Cargo.toml"
            if manifest.exists():
                package = read_cargo_package(parent)
                if isinstance(package, CargoPackage):
                    packages.add(package)
                break
        match = re.search(r"(?:^|/)crates/([^/]+)/examples/([^/.]+)", raw)
        if match:
            folder = checkout / "crates" / match.group(1)
            if (folder / "Cargo.toml").exists():
                package = read_cargo_package(folder)
                if isinstance(package, CargoPackage):
                    examples.add((package.name, match.group(2)))
    verify = tool("verify.sh", Path.home() / ".claude/scripts/direct/verify.sh", "MERGE_CHECKPOINT_VERIFY")
    verify_command = [verify] if os.access(verify, os.X_OK) else ["bash", verify]
    steps = [CheckpointTestStep(f"package {package.name}", [*verify_command, "test", package.name], package.folder)
             for package in sorted(packages)]
    steps.extend(CheckpointTestStep(f"example {package}/{name}", [*verify_command, "example", package, "--name", name], checkout)
                 for package, name in sorted(examples))
    steps.extend(CheckpointTestStep(f"merge test {index}", list(command), checkout)
                 for index, command in enumerate(merge_tests, 1))
    return steps


def checkpoint_gate_targets(command: Sequence[str], folder: Path) -> tuple[str, ...]:
    """Report unavailable placement as a blocked checkpoint."""
    try:
        return gate_targets(command, folder).names
    except FirstStepRefused as error:
        reason = str(error)
        raise Stop("test", "blocked", reason, reason) from error


def gate_program_start(command: Sequence[str]) -> int:
    """Skip the env prefix and its assignments before identifying a gate."""
    if not command or Path(command[0]).name != "env":
        return 0
    start = 1
    while start < len(command) and "=" in command[start]:
        start += 1
    return start


def gate_copy_command(command: Sequence[str]) -> list[str]:
    """Point the gate at the copy that copies.py prepares."""
    arguments = list(command)
    for index, word in enumerate(command):
        option, equals, _ = word.partition("=")
        if option in ("--checkout", "--repo"):
            if equals:
                arguments[index] = f"{option}={{tree}}"
            elif index + 1 < len(arguments):
                arguments[index + 1] = "{tree}"
    return arguments


def run_test_command(command: list[str], folder: Path, *, merge_head: str = "",
                     raise_start_errors: bool = False) -> subprocess.CompletedProcess[str]:
    locks = copies.COMMAND_LOCKS.get()
    submitted = command
    scripts = Path(__file__).resolve().parents[1]
    start = gate_program_start(command)
    use_copy = bool(merge_head) and command_program(command[start:]) in GATE_SCRIPTS
    if use_copy:
        submitted = [*command[:start], str(scripts / "lib/py"), str(scripts / "copies/copies.py"), "run",
                     "--repo", str(folder.resolve()), "--commit", merge_head, "--",
                     *gate_copy_command(command[start:])]
    elif targets := checkpoint_gate_targets(command, folder):
        submitted = [str(scripts / "lib/py"), str(scripts / "copies/copies.py"), "gate-step",
                     "--repo", str(folder.resolve()),
                     *(word for fd in locks for word in ("--lease-fd", str(fd))), "--", *command]
    try:
        return subprocess.run(submitted, cwd=folder, capture_output=True, text=True, check=False, pass_fds=locks)
    except OSError as error:
        if raise_start_errors and not use_copy:
            raise
        return subprocess.CompletedProcess(command, 127, "", f"{error}\n")


# A nextest failure row: `FAIL [  12.2s] (1012/1067) <binary> <test>`.
NEXTEST_FAIL = re.compile(r"^\s+FAIL \[[^]]*\] \([^)]*\) \S+ (\S+)\s*$", re.MULTILINE)


def failed_tests(output: str) -> frozenset[str]:
    """Return unique names of tests that nextest reports as failed."""
    return frozenset(NEXTEST_FAIL.findall(output))


# A failed module in the gate output: `--- accounts/test_accounts.py exit=1 ---`.
GATE_RED = re.compile(r"^--- (\S+) (?:rerun alone )?exit=(-?\d+) ---$", re.MULTILINE)
# A module that passed when the gate ran it again alone.
GATE_GREEN_ALONE = re.compile(r"^--- (\S+) passed on rerun alone; first run exit=", re.MULTILINE)


def runs_merge_gate(command: Sequence[str]) -> bool:
    return any(Path(word).name in GATE_SCRIPTS for word in command)


def red_names(command: Sequence[str], output: str) -> frozenset[str]:
    """Return the failed modules for a gate command or the failed nextest tests for another command."""
    if not runs_merge_gate(command):
        return failed_tests(output)
    green = frozenset(GATE_GREEN_ALONE.findall(output))
    return frozenset(match[1] for match in GATE_RED.finditer(output) if match[2] != "0") - green


def only_tests(command: list[str], names: frozenset[str]) -> list[str]:
    """Restrict the command to the named gate modules, or tests with verify.sh --filter or nextest -E test(=name)."""
    if runs_merge_gate(command):
        return [*command, *(word for name in sorted(names) for word in ("--module", name))]
    if any(Path(word).name == "verify.sh" for word in command):
        return [*command, *(word for name in sorted(names) for word in ("--filter", name))]
    return [*command, *(word for name in sorted(names) for word in ("-E", f"test(={name})"))]


def narrowed(red: RedStep) -> CheckpointTestStep:
    """Return the red command limited to its red names, or the whole command when it names none."""
    return red.step._replace(command=only_tests(red.step.command, red.names)) if red.names else red.step


class SplitRed(NamedTuple):
    """The red names of a checkpoint merge, split into the unit's own red and the red already on the base."""

    unit: tuple[RedStep, ...]
    base: tuple[RedStep, ...]


def split_base_red(red: RedCheckpointTests, base_steps: Sequence[CheckpointTestStep], log: Path, *,
                   merge_head: str) -> SplitRed:
    """Run each red command on the checked-out base, limited to its red names, and split the names.

    A command that the base does not select, such as a package the checkpoint adds, stays the unit's red.
    A name stays the unit's red unless the base run names it red too. A command with no names
    is the base's red only when the whole command is red on the base.
    """
    selected = {(tuple(step.command), step.folder) for step in base_steps}
    unit: list[RedStep] = []
    base: list[RedStep] = []
    with log.open("a", encoding="utf-8") as output:
        for red_step in red.red:
            step = red_step.step
            if (tuple(step.command), step.folder) not in selected:
                unit.append(red_step)
                continue
            try:
                result = run_test_command(narrowed(red_step).command, step.folder, merge_head=merge_head,
                                          raise_start_errors=True)
            except OSError as error:
                # A command that cannot start on the base, such as a package the merge adds, is the unit's red.
                _ = output.write(f"{step.label.replace(' ', '_')}_BASE_EXIT=not started: {error}\n")
                unit.append(red_step)
                continue
            _ = output.write(result.stdout + result.stderr)
            _ = output.write(f"{step.label.replace(' ', '_')}_BASE_EXIT={result.returncode}\n")
            output.flush()
            stop_test_without_result(step, result, log, merge_head=merge_head)
            if test_passed(result):
                unit.append(red_step)
            elif not red_step.names:
                base.append(red_step)
            else:
                on_base = red_names(step.command, result.stdout + result.stderr) & red_step.names
                if on_base:
                    base.append(RedStep(step, on_base))
                if red_step.names - on_base:
                    unit.append(RedStep(step, red_step.names - on_base))
    return SplitRed(tuple(unit), tuple(base))


def red_labels(red: Sequence[RedStep]) -> str:
    return ", ".join(item.step.label + (f" ({', '.join(sorted(item.names))})" if item.names else "") for item in red)


def stop_test_without_result(step: CheckpointTestStep, result: subprocess.CompletedProcess[str], log: Path, *,
                             merge_head: str) -> None:
    if result.returncode == TREE_CHANGED_STATUS:
        raise Stop("test", "cancelled", f"{step.label} cancelled because the tree changed; log {log}",
                   f"resubmit this checkpoint after your edits are done; test log: {log}")
    if command_program(step.command[gate_program_start(step.command):]) not in GATE_SCRIPTS:
        return
    if result.returncode == GATE_UNSETTLED_LAUNCH_STATUS:
        raise Stop("test", "blocked", f"{step.label} waits for launch settlement with gate status {result.returncode}. Log {log}",
                   f"Check the launch settlement in {log}.")
    if result.returncode == GATE_PRELAUNCH_REFUSED_STATUS:
        raise Stop("test", "blocked", f"{step.label} could not complete placement. Gate status {result.returncode}. Log {log}",
                   f"Check the gate placement in {log}.")
    if merge_head and result.returncode in (copies.COPY_ERROR_STATUS, os.EX_TEMPFAIL, 127):
        raise Stop("test", "blocked", f"{step.label} could not start. Copy status {result.returncode}. Log {log}",
                   f"Check the gate copy and placement in {log}.")


def run_checkpoint_test_steps(production: ProductionDocument, paths: tuple[str, ...],
                              merge_tests: tuple[tuple[str, ...], ...], flakes: frozenset[str],
                              flaky_tests: frozenset[str], log: Path, *,
                              ledger: ProductionDocument | None = None, merge_head: str = "") -> str:
    """Select checkpoint commands and run them with the flake rules."""
    return run_test_steps(ledger or production, test_steps(production, paths, merge_tests), flakes, flaky_tests, log,
                          merge_head=merge_head)


def run_logged_step(step: CheckpointTestStep, log: Path, *, merge_head: str = "") -> subprocess.CompletedProcess[str]:
    with log.open("a", encoding="utf-8") as output:
        result = run_test_command(step.command, step.folder, merge_head=merge_head)
        _ = output.write(result.stdout + result.stderr)
        _ = output.write(f"{step.label.replace(' ', '_')}_EXIT={result.returncode}\n")
    stop_test_without_result(step, result, log, merge_head=merge_head)
    return result


def package_test_folder(folder: Path) -> Path:
    """Use the workspace root when it preserves the package's Cargo config."""
    try:
        package = folder.resolve()
        cwd = str(package)
        manifest = cargo_workspace_manifest([], cwd, cargo_home_folder(cwd, os.environ))
        workspace = manifest.parent.resolve()
        for directory in (package, *package.parents):
            if workspace.is_relative_to(directory):
                break
            config = directory / ".cargo"
            if (config / "config.toml").is_file() or (config / "config").is_file():
                return folder
        return workspace
    except (OSError, ValueError):
        # Let Verify report discovery errors from the package folder.
        return folder


def package_test_results(packages: Sequence[CheckpointTestStep], folder: Path, log: Path
                         ) -> Generator[tuple[CheckpointTestStep, subprocess.CompletedProcess[str]]]:
    """Run packages from one folder. Report a red as its own package step."""
    while len(packages) > 1:
        first = packages[0]
        names = [step.command[-1] for step in packages]
        command = [*first.command, *(word for name in names[1:] for word in ("+", "test", name))]
        joined = CheckpointTestStep(f"packages {', '.join(names)}", command, folder)
        result = run_logged_step(joined, log)
        lines = set(result.stderr.splitlines())
        failed = next((index for index, name in enumerate(names)
                       if f"verify.sh: step result: test {name} exit={result.returncode}" in lines), None)
        if test_passed(result) or failed is None:
            yield joined, result
            return
        yield packages[failed], result
        # A Verify step list ends at the first red. Run any packages it did not reach.
        packages = packages[failed + 1:]
    for step in packages:
        yield step, run_logged_step(step._replace(folder=folder), log)


def initial_test_results(steps: Sequence[CheckpointTestStep], log: Path, *, merge_head: str
                         ) -> Generator[tuple[CheckpointTestStep, subprocess.CompletedProcess[str]]]:
    """Group package tests that can run from the same Cargo workspace root."""
    packages = tuple(takewhile(lambda step: step.label.startswith("package "), steps))
    by_folder: dict[Path, list[CheckpointTestStep]] = {}
    for step in packages:
        by_folder.setdefault(package_test_folder(step.folder), []).append(step)
    for folder, group in by_folder.items():
        yield from package_test_results(group, folder, log)
    for step in steps[len(packages):]:
        yield step, run_logged_step(step, log, merge_head=merge_head)


def run_test_steps(production: ProductionDocument, steps: Sequence[CheckpointTestStep], flakes: frozenset[str],
                   flaky_tests: frozenset[str], log: Path, *, retry_failed_steps: bool = True,
                   merge_head: str = "") -> str:
    """Run checkpoint tests with the caller's retry policy."""
    log.parent.mkdir(parents=True, exist_ok=True)
    if not steps:
        log.touch(exist_ok=True)
    failed: list[tuple[CheckpointTestStep, frozenset[str], frozenset[str]]] = []
    count = 0
    for count, (step, result) in enumerate(initial_test_results(steps, log, merge_head=merge_head), 1):
        if not test_passed(result):
            output_text = result.stdout + result.stderr
            names = red_names(step.command, output_text)
            if not retry_failed_steps:
                report("test", "ok", f"{count} commands ran; 1 red; log {log}")
                raise unit_red((RedStep(step, names),), log)
            failed.append((step, failed_tests(output_text), names))
    red_steps: list[RedStep] = []
    for step, tests, first_names in failed:
        name, command, folder = step
        # Run only the known flaky tests when they cause the failure.
        alone = bool(tests) and tests <= flaky_tests
        with log.open("a", encoding="utf-8") as output:
            result = run_test_command(only_tests(command, tests) if alone else command, folder, merge_head=merge_head)
            _ = output.write(result.stdout + result.stderr)
            _ = output.write(f"{name.replace(' ', '_')}_RERUN_EXIT={result.returncode}\n")
        stop_test_without_result(step, result, log, merge_head=merge_head)
        package = name.removeprefix("package ")
        if test_passed(result) and (alone or package in flakes):
            note = (f"known flaky tests {', '.join(sorted(tests))}: red in {name}, green alone" if alone
                    else f"known flake {package}: red in batch, green alone")
            production.log.parent.mkdir(parents=True, exist_ok=True)
            with production.log.open("a", encoding="utf-8") as ledger:
                _ = ledger.write(f"- {datetime.now(production.zone):%H:%M %Z}: {note}\n")
        else:
            # The rerun decides which names stay red; the first run names them when the rerun names none.
            names = red_names(command, result.stdout + result.stderr) or first_names
            red_steps.append(RedStep(step, names))
    report("test", "ok", f"{count} commands ran; {len(failed)} red; log {log}")
    if red_steps:
        raise unit_red(tuple(red_steps), log)
    report("red", "ok", "all commands green or known flakes green alone")
    return ", ".join(step.label for step in steps) if steps else "no tests"


def unit_red(red: tuple[RedStep, ...], log: Path) -> RedCheckpointTests:
    labels = red_labels(red)
    return RedCheckpointTests(f"{labels} failed; log {log}",
                              f"repair {labels} and send a new checkpoint; test log: {log}", red)


def test_passed(result: subprocess.CompletedProcess[str]) -> bool:
    """Accept only status zero."""
    return result.returncode == 0


class BumpGateRecord(TypedDict):
    """The gate plan and its process or completed result, owned by one merge."""

    tip: str
    checkpoint: str
    production_doc: str
    owner_id: str
    unit_id: str
    name: str
    merge_branch: str
    checkout: str
    # Older gates store the LOG relative to their recorded checkout.
    production_log: str
    zone: str
    unit: str
    delivered_to: list[Literal["unit", "showrunner"]]
    paths: list[str]
    merge_tests: list[list[str]]
    flakes: list[str]
    flaky_tests: list[str]
    started: str
    pid: int
    proc_start: str
    result: Literal["pending", "green", "red", "blocked"]
    detail: str
    log: str

_held_record_locks: ContextVar[frozenset[Path]] = ContextVar("held_record_locks", default=frozenset())


@contextmanager
def record_lock(path: Path) -> Generator[None]:
    held = _held_record_locks.get()
    if path in held:
        yield
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_name(path.name + ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        token = _held_record_locks.set(held | {path})
        try:
            yield
        finally:
            _held_record_locks.reset(token)


def read_record(path: Path) -> BumpGateRecord:
    raw = cast(object, json.loads(path.read_text(encoding="utf-8")))
    if not isinstance(raw, dict):
        raise ValueError(f"invalid bump gate record: {path}")
    return cast(BumpGateRecord, cast(object, raw))


def save_record(path: Path, record: BumpGateRecord) -> None:
    write_text_atomic(path, json.dumps(record, indent=2) + "\n")


def same_gate_runner(first: BumpGateRecord, second: BumpGateRecord) -> bool:
    return (first["tip"], first["pid"], first["proc_start"]) == (second["tip"], second["pid"], second["proc_start"])


def send_result(path: Path, record: BumpGateRecord) -> None:
    """Deliver each pending notice to the current recipient. Save each successful delivery."""
    with record_lock(path):
        current = read_record(path)
        if not same_gate_runner(current, record) or current["result"] == "pending":
            print("bump gate delivery superseded; no notice sent", flush=True)
            return
        text = (f"Bump gate {current['result']}: {current['unit']}; tip {current['tip']}; "
                + f"{current['detail']}; log {current['log']}")
        script = tool("send.py", session_send.SEND_SCRIPT, "MERGE_CHECKPOINT_SEND")
        recipients: tuple[Literal["unit", "showrunner"], ...] = (
            ("showrunner",) if current["result"] == "green" else ("unit", "showrunner"))
        delivered = current.get("delivered_to", [])
        current["delivered_to"] = delivered
        for recipient in recipients:
            if recipient in delivered:
                continue
            for attempt in (1, 2):
                try:
                    members = staff.snapshot().rows_by_id
                    owner = members[staff.StaffId(current["owner_id"])]
                    target = members[staff.StaffId(current["unit_id"])] if recipient == "unit" else owner
                    address = f"session:{target.current_session}"
                    result = subprocess.run(
                        session_send.send_argv("--to", address, "--from", owner.name, "--text", text, send_script=Path(script)),
                        capture_output=True, text=True, check=False,
                    )
                    if result.returncode:
                        raise ValueError(failure_detail(result))
                    delivered.append(recipient)
                    save_record(path, current)
                    break
                except (OSError, ValueError, KeyError) as error:
                    print(f"bump gate send {recipient} attempt {attempt} failed: {error}", flush=True)


def complete_gate(path: Path, record: BumpGateRecord, result: Literal["green", "red", "blocked"], detail: str) -> None:
    with record_lock(path):
        current = read_record(path)
        if not same_gate_runner(current, record):
            print("bump gate completion superseded; no result saved or notice sent", flush=True)
            return
        current["result"] = result
        current["detail"] = " ".join(detail.splitlines())
        save_record(path, current)
    send_result(path, current)
