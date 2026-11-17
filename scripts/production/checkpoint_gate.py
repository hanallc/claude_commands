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
from pathlib import Path
from typing import Literal, NamedTuple, TypedDict, cast

from scripts.copies import copies
from scripts.lib.atomic_file import write_text_atomic
from scripts.lib.process_failure import failure_detail
from scripts.message import session_send
from scripts.production import staff
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


def run_test_command(command: list[str], folder: Path, *, raise_start_errors: bool = False) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(command, cwd=folder, capture_output=True, text=True, check=False, pass_fds=copies.COMMAND_LOCKS.get())
    except OSError as error:
        if raise_start_errors:
            raise
        return subprocess.CompletedProcess(command, 127, "", f"{error}\n")

# A nextest failure row: `FAIL [  12.2s] (1012/1067) <binary> <test>`.
NEXTEST_FAIL = re.compile(r"^\s+FAIL \[[^]]*\] \([^)]*\) \S+ (\S+)\s*$", re.MULTILINE)


def failed_tests(output: str) -> frozenset[str]:
    """Return unique names of tests that nextest reports as failed."""
    return frozenset(NEXTEST_FAIL.findall(output))


# A red module in the merge gate output: `--- accounts/test_accounts.py exit=1 ---`.
GATE_RED = re.compile(r"^--- (\S+) (?:rerun alone )?exit=(-?\d+) ---$", re.MULTILINE)
# A module that the merge gate passed when it ran the module again alone.
GATE_GREEN_ALONE = re.compile(r"^--- (\S+) passed on rerun alone; first run exit=", re.MULTILINE)


def runs_merge_gate(command: Sequence[str]) -> bool:
    return any(Path(word).name in {"merge_gate.py", "gate.py"} for word in command)


def red_names(command: Sequence[str], output: str) -> frozenset[str]:
    """Return the red gate modules of a merge gate command, or the red nextest tests of another command."""
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


def split_base_red(red: RedCheckpointTests, base_steps: Sequence[CheckpointTestStep], log: Path) -> SplitRed:
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
                result = run_test_command(narrowed(red_step).command, step.folder, raise_start_errors=True)
            except OSError as error:
                # A command that cannot start on the base, such as a package the merge adds, is the unit's red.
                _ = output.write(f"{step.label.replace(' ', '_')}_BASE_EXIT=not started: {error}\n")
                unit.append(red_step)
                continue
            _ = output.write(result.stdout + result.stderr)
            _ = output.write(f"{step.label.replace(' ', '_')}_BASE_EXIT={result.returncode}\n")
            output.flush()
            stop_cancelled_test(step.label, result, log)
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


def stop_cancelled_test(name: str, result: subprocess.CompletedProcess[str], log: Path) -> None:
    if result.returncode == TREE_CHANGED_STATUS:
        raise Stop("test", "cancelled", f"{name} cancelled because the tree changed; log {log}",
                   f"resubmit this checkpoint after your edits are done; test log: {log}")


def run_checkpoint_test_steps(production: ProductionDocument, paths: tuple[str, ...],
                              merge_tests: tuple[tuple[str, ...], ...], flakes: frozenset[str],
                              flaky_tests: frozenset[str], log: Path) -> str:
    """Select checkpoint commands and run them with the flake rules."""
    return run_test_steps(production, test_steps(production, paths, merge_tests), flakes, flaky_tests, log)


def run_test_steps(production: ProductionDocument, steps: Sequence[CheckpointTestStep], flakes: frozenset[str],
                   flaky_tests: frozenset[str], log: Path, *, raise_start_errors: bool = False) -> str:
    """Run the given commands and their retries; the caller owns merge rollback."""
    log.parent.mkdir(parents=True, exist_ok=True)
    failed: list[tuple[CheckpointTestStep, frozenset[str], frozenset[str]]] = []
    with log.open("a", encoding="utf-8") as output:
        for step in steps:
            result = run_test_command(step.command, step.folder, raise_start_errors=raise_start_errors)
            _ = output.write(result.stdout + result.stderr)
            _ = output.write(f"{step.label.replace(' ', '_')}_EXIT={result.returncode}\n")
            output.flush()
            stop_cancelled_test(step.label, result, log)
            if not test_passed(result):
                failed.append((step, failed_tests(result.stdout + result.stderr),
                               red_names(step.command, result.stdout + result.stderr)))
    red_steps: list[RedStep] = []
    for step, tests, first_names in failed:
        name, command, folder = step
        # Run only the known flaky tests when they cause the failure.
        alone = bool(tests) and tests <= flaky_tests
        with log.open("a", encoding="utf-8") as output:
            result = run_test_command(only_tests(command, tests) if alone else command, folder,
                                      raise_start_errors=raise_start_errors)
            _ = output.write(result.stdout + result.stderr)
            _ = output.write(f"{name.replace(' ', '_')}_RERUN_EXIT={result.returncode}\n")
        stop_cancelled_test(name, result, log)
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
    report("test", "ok", f"{len(steps)} commands ran; {len(failed)} red; log {log}")
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
    result: Literal["pending", "green", "red"]
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
        red = current["result"] == "red"
        text = (f"Bump gate {'red' if red else 'green'}: {current['unit']}; tip {current['tip']}; "
                + f"{current['detail']}; log {current['log']}")
        script = tool("send.py", session_send.SEND_SCRIPT, "MERGE_CHECKPOINT_SEND")
        recipients: tuple[Literal["unit", "showrunner"], ...] = ("unit", "showrunner") if red else ("showrunner",)
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


def complete_gate(path: Path, record: BumpGateRecord, result: Literal["green", "red"], detail: str) -> None:
    with record_lock(path):
        current = read_record(path)
        if not same_gate_runner(current, record):
            print("bump gate completion superseded; no result saved or notice sent", flush=True)
            return
        current["result"] = result
        current["detail"] = " ".join(detail.splitlines())
        save_record(path, current)
    send_result(path, current)
