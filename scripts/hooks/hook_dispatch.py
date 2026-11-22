#!/usr/bin/env python3
"""Run an event's Python hooks in registration order in forked children.

Claude runs separate matching hooks in parallel without a documented order.
This dispatcher deliberately uses settings order. Exit 2 wins, discards stdout,
and joins only exit-2 stderr with a blank line. Otherwise we exit 0 and merge
successful JSON objects: permission precedence is deny > defer > ask > allow;
only winning permission reasons join with blank lines. Context joins with blank
lines, system messages with newlines. Any continue:false wins (its stop reasons
join with newlines); PostToolUse/Stop block wins (block reasons join with blank
lines). Unknown keys keep their first value. Two updatedInput values keep the
first and add a system message naming the conflicting hooks.

The docs do not specify how simultaneous stderr, permission reasons,
systemMessage, continue:false/stopReason, decision:block/reason, updatedInput,
or unknown keys combine. These ordering, joining and first-value rules are our
explicit policy for those gaps. Every additionalContext remains available.
Nonblank non-JSON stdout is copied to debug stderr and dropped; invalid JSON or
nonzero/non-2 exit also adds a hook-error system message, since a single exit 0
cannot reproduce separate transcript hook-error notices. Known JSON fields are
validated before merging; a malformed document contributes only its error.
Error messages retain the hook's whole stripped stderr (including traceback),
or "exit N" when stderr is blank. A timeout discards all output and decisions.
With no JSON and no errors, stdout stays empty.

Each child inherits the dispatcher's imports. The copy policy child uses its
loaded entry point. Other children load their hooks as fresh __main__ modules
through importlib's bytecode cache. Each child's streams, file descriptors,
argv, environment, cwd and signals are isolated from later hooks.
The parent enforces the hook deadline independently of alarms in the child.
The settings.json timeout covers the largest sum of matching hook budgets
plus 30 seconds of overhead.
"""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import gc
import importlib.util
import io
import json
import os
import re
import selectors
import signal
import socket
import sys
import tempfile
import time
import traceback
from collections.abc import Callable, Generator, Iterable, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, NoReturn, TextIO, TypedDict, cast

DISPATCH_OVERHEAD_SECONDS = 30
HOOK_SETUP_SECONDS = DISPATCH_OVERHEAD_SECONDS / 3


@dataclass(frozen=True)
class HookSpec:
    filename: str
    matcher: str | None
    timeout: float = 600


@dataclass(frozen=True)
class CopyPolicyHook:
    """The combined copy/scratch front, with a budget read at dispatch time."""

    filename: str = "../copies/copy_guard.py"
    matcher: str = "Bash|Write|NotebookEdit"

    @property
    def timeout(self) -> float:
        from scripts.copies.copy_guard import hook_timeout
        return hook_timeout()


@dataclass(frozen=True)
class HookResult:
    filename: str
    exit_code: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class DispatchResult:
    exit_code: int
    stdout: str
    stderr: str


PRE_TOOL_USE = (
    HookSpec("pre-tool-use-heredoc-guard.py", "Bash"),
    HookSpec("pre-tool-use-tool-output-read.py", "Bash"),
    HookSpec("pre-tool-use-showrunner-move-guard.py", "Bash"),
    HookSpec("pre-tool-use-change-review-gate.py", "Bash"),
    HookSpec("pre-tool-use-checkpoint-doc-gate.py", "Bash"),
    HookSpec("pre-tool-use-push-alone.py", "Bash", 10),
    CopyPolicyHook(),
)
POST_TOOL_USE = (
    HookSpec("post-tool-use-basedpyright.py", "Edit|MultiEdit|Write"),
    HookSpec("post-tool-use-fn-length.py", "Edit|MultiEdit|Write"),
    HookSpec("post-tool-use-context-usage.py", None),
    HookSpec("post-tool-use-push-delivered.py", "PushNotification"),
    HookSpec("post-tool-use-sendmessage-miss.py", "SendMessage"),
)
STOP = (
    HookSpec("compaction-marker-clear.py", None, 10),
    HookSpec("stop-conversation-pause.py", None),
    HookSpec("stop-context-deferral.py", None, 10),
    HookSpec("herdr_tab_title.py", None),
)
EVENTS = {"pre_tool_use": PRE_TOOL_USE, "post_tool_use": POST_TOOL_USE, "stop": STOP}
HOOKS = Path(__file__).resolve().parent
COPY_POLICY_PATH = (HOOKS / CopyPolicyHook.filename).resolve()


class ToolEventPayload(TypedDict, total=False):
    tool_name: str


class HookEventIdentity(TypedDict):
    hookEventName: str


class HookSpecificOutput(HookEventIdentity, total=False):
    permissionDecision: str
    permissionDecisionReason: str
    updatedInput: dict[str, object]
    additionalContext: str
    classifierContext: str


HookOutput = TypedDict("HookOutput", {
    "hookSpecificOutput": HookSpecificOutput,
    "additionalContext": str,
    "systemMessage": str,
    "continue": bool,
    "suppressOutput": bool,
    "terminalSequence": str,
    "stopReason": str,
    "decision": str,
    "reason": str,
}, total=False)


class HookDeadlineExpired(Exception):
    """The parent did not receive the hook result within its budget."""


def _execute(path: Path) -> None:
    if path == COPY_POLICY_PATH:
        from scripts.copies.copy_guard import main as copy_policy_main
        copy_policy_main()
        return
    specification = importlib.util.spec_from_file_location("__main__", path)
    if specification is None or specification.loader is None:
        raise ImportError(f"cannot load hook: {path}")
    module = importlib.util.module_from_spec(specification)
    previous = sys.modules["__main__"]
    sys.modules["__main__"] = module
    try:
        specification.loader.exec_module(module)
    finally:
        sys.modules["__main__"] = previous


def _exception_status(error: BaseException, stderr: TextIO) -> int:
    if isinstance(error, SystemExit):
        if error.code is None:
            return 0
        if isinstance(error.code, int):
            return error.code
        print(error.code, file=stderr)
    else:
        traceback.print_exc(file=stderr)
    return 1


def _capture_hook(path: Path, payload: str, prepare: Callable[[], None]) -> HookResult:
    exit_code = 0
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as stdout, \
            tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as stderr, \
            io.TextIOWrapper(io.BytesIO(payload.encode("utf-8")), encoding="utf-8") as stdin:
        # A hook can close its streams. Keep the capture files open to read the result.
        hook_stdout = os.fdopen(os.dup(stdout.fileno()), "w", encoding="utf-8", errors="replace")
        hook_stderr = os.fdopen(os.dup(stderr.fileno()), "w", encoding="utf-8", errors="replace")
        _ = os.dup2(stdout.fileno(), 1)
        _ = os.dup2(stderr.fileno(), 2)
        sys.stdin, sys.stdout, sys.stderr = stdin, hook_stdout, hook_stderr
        sys.argv = [str(path)]
        try:
            prepare()
            _execute(path)
        except BaseException as error:
            exit_code = _exception_status(error, stderr)
        finally:
            for stream in (hook_stdout, hook_stderr):
                with suppress(Exception):
                    stream.close()
        _ = stdout.seek(0)
        _ = stderr.seek(0)
        return HookResult(path.name, exit_code, stdout.read(), stderr.read())


def _write_frame(output: BinaryIO, value: object) -> None:
    _ = output.write(json.dumps(value).encode("ascii") + b"\n")
    output.flush()


def _report_hook(path: Path, payload: str, output: BinaryIO,
                 timeout: float | Callable[[], float], setup_deadline: float) -> NoReturn:
    def prepare() -> None:
        if callable(timeout):
            try:
                seconds = timeout()
            except BaseException:
                _write_frame(output, setup_deadline)
                raise
            _write_frame(output, time.monotonic() + seconds)

    try:
        result = _capture_hook(path, payload, prepare)
        _write_frame(output, [result.exit_code, result.stdout, result.stderr])
    except BaseException:
        os._exit(1)
    os._exit(0)


def _read_hook_report(descriptor: int, deadline: float, pending: bytearray) -> bytearray:
    end = pending.find(b"\n")
    with selectors.DefaultSelector() as selector:
        _ = selector.register(descriptor, selectors.EVENT_READ)
        while end < 0:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                raise HookDeadlineExpired()
            block = os.read(descriptor, 65536)
            if not block:
                return bytearray()
            end = block.find(b"\n")
            if end >= 0:
                end += len(pending)
            pending.extend(block)
    report = pending[:end]
    del pending[:end + 1]
    return report


def run_hook(path: Path, payload: str, timeout: float | Callable[[], float] = 600) -> HookResult:
    """Execute a child and keep its report if it arrives within the budget."""
    path = path.resolve()
    deadline = time.monotonic() + (HOOK_SETUP_SECONDS if callable(timeout) else timeout)
    reader, writer = os.pipe()
    with os.fdopen(reader, "rb") as input_pipe, os.fdopen(writer, "wb") as output_pipe:
        pid = os.fork()
        if pid == 0:
            input_pipe.close()
            _report_hook(path, payload, output_pipe, timeout, deadline)
        report = bytearray()
        try:
            output_pipe.close()
            pending = bytearray()
            if callable(timeout):
                budget = _read_hook_report(reader, deadline, pending)
                if budget:
                    report = _read_hook_report(reader, cast(float, json.loads(budget)), pending)
            else:
                report = _read_hook_report(reader, deadline, pending)
        except HookDeadlineExpired:
            return HookResult(path.name, 0, "", "")
        finally:
            if not report:
                with suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGKILL)
            _, status = os.waitpid(pid, 0)
    if report:
        code, stdout, stderr = cast(tuple[int, str, str], json.loads(report))
        return HookResult(path.name, code, stdout, stderr)
    code = os.waitstatus_to_exitcode(status)
    return HookResult(path.name, 128 - code if code < 0 else code, "", "")


def _mapping(value: object) -> dict[str, object]:
    return cast(dict[str, object], value) if isinstance(value, dict) else {}


def _valid_fields(document: dict[str, object], strings: Sequence[str],
                  booleans: Sequence[str] = ()) -> bool:
    return (all(isinstance(document[key], str) for key in strings if key in document)
            and all(isinstance(document[key], bool) for key in booleans if key in document))


def _valid_output(document: dict[str, object], event: str) -> bool:
    """Reject the entire hook document when a documented field is malformed.

    Unknown extension keys retain their JSON values. Tool output replacement
    fields also accept arbitrary JSON: their tool-specific schema is unavailable
    here. The deprecated top-level decision value "approve" remains accepted.
    """
    if not _valid_fields(document, ("additionalContext", "systemMessage", "stopReason",
                                    "reason", "decision", "terminalSequence"),
                         ("continue", "suppressOutput")):
        return False
    if "decision" in document and document["decision"] not in ("approve", "block"):
        return False
    if "hookSpecificOutput" not in document:
        return True
    value = document["hookSpecificOutput"]
    if not isinstance(value, dict):
        return False
    specific = _mapping(cast(object, value))
    expected_event = {"pre_tool_use": "PreToolUse", "post_tool_use": "PostToolUse", "stop": "Stop"}
    if specific.get("hookEventName") != expected_event.get(event):
        return False
    if not _valid_fields(specific, ("hookEventName", "permissionDecision", "permissionDecisionReason",
                                   "additionalContext", "classifierContext")):
        return False
    if "permissionDecision" in specific and specific["permissionDecision"] not in ("allow", "ask", "defer", "deny"):
        return False
    return "updatedInput" not in specific or isinstance(specific["updatedInput"], dict)


def _reject_json_constant(value: str) -> NoReturn:
    raise ValueError(f"invalid JSON constant: {value}")


def _merge_fields(documents: Sequence[dict[str, object]]) -> dict[str, object]:
    merged: dict[str, object] = {}
    for document in documents:
        for key, value in document.items():
            _ = merged.setdefault(key, value)
    for key, separator in (("additionalContext", "\n\n"), ("systemMessage", "\n")):
        values = [str(document[key]) for document in documents if key in document]
        if values:
            merged[key] = separator.join(values)
    return merged


def _merge_permissions(specifics: Sequence[dict[str, object]], merged: dict[str, object]) -> None:
    ranks = {"allow": 0, "ask": 1, "defer": 2, "deny": 3}
    decisions = [item for item in specifics if item.get("permissionDecision") in ranks]
    if decisions:
        winner = max(decisions, key=lambda item: ranks[str(item["permissionDecision"])])["permissionDecision"]
        merged["permissionDecision"] = winner
        merged["permissionDecisionReason"] = "\n\n".join(
            str(item["permissionDecisionReason"]) for item in decisions
            if item["permissionDecision"] == winner and "permissionDecisionReason" in item)


def combine_results(event: str, results: Sequence[HookResult]) -> DispatchResult:
    """Combine script outcomes using the explicit policy in this module's docstring."""
    refusals = [result.stderr.rstrip("\n") for result in results if result.exit_code == 2]
    if refusals:
        return DispatchResult(2, "", "\n\n".join(refusals))
    documents: list[dict[str, object]] = []
    names: list[str] = []
    debug: list[str] = []
    for result in results:
        invalid = False
        document: dict[str, object] = {}
        if result.exit_code == 0 and result.stdout.strip():
            try:
                loaded = cast(object, json.loads(result.stdout, parse_constant=_reject_json_constant))
                if not isinstance(loaded, dict):
                    invalid = True
                else:
                    # Known JSON keys are described by HookOutput; the mapping also
                    # retains extension keys whose first values must survive merging.
                    document = _mapping(cast(HookOutput, cast(object, loaded)))
                    invalid = not _valid_output(document, event)
                    # Reject parser/encoder depth-limit disagreements per hook,
                    # before they could prevent the other hooks' JSON rendering.
                    _ = json.dumps(document, allow_nan=False)
            except (ValueError, RecursionError):
                invalid = True
                debug.append(result.stdout.rstrip("\n"))
        if result.exit_code != 0 or invalid:
            detail = result.stderr.strip() or f"exit {result.exit_code}"
            document = {"systemMessage": f"{result.filename} hook error: {detail}"}
        documents.append(document)
        names.append(result.filename)
    merged = _merge_fields(documents)
    specifics = [_mapping(item["hookSpecificOutput"]) for item in documents if "hookSpecificOutput" in item]
    if specifics:
        specific = _merge_fields(specifics)
        if event == "pre_tool_use":
            _merge_permissions(specifics, specific)
        merged["hookSpecificOutput"] = specific
        updates = [name for name, doc in zip(names, documents)
                   if "updatedInput" in _mapping(doc.get("hookSpecificOutput"))]
        if len(updates) > 1:
            conflict = f"updatedInput conflict: {', '.join(updates)}; keeping {updates[0]}"
            merged["systemMessage"] = "\n".join(
                [str(merged["systemMessage"]), conflict] if "systemMessage" in merged else [conflict])
    stopped = [item for item in documents if item.get("continue") is False]
    if stopped:
        merged["continue"] = False
        merged["stopReason"] = "\n".join(str(item["stopReason"]) for item in stopped if "stopReason" in item)
    blocked = [item for item in documents if item.get("decision") == "block"]
    if event in ("post_tool_use", "stop") and blocked:
        merged["decision"] = "block"
        merged["reason"] = "\n\n".join(str(item["reason"]) for item in blocked if "reason" in item)
    return DispatchResult(0, json.dumps(merged) if merged else "", "\n".join(debug))


def _shared_dependencies(hooks: Sequence[HookSpec | CopyPolicyHook]) -> list[str]:
    filenames = {hook.filename for hook in hooks}
    modules: list[str] = []
    if CopyPolicyHook.filename in filenames:
        modules.extend(("scripts.copies.copy_guard", "scripts.copies.copies"))
    if filenames >= {"pre-tool-use-change-review-gate.py", "pre-tool-use-push-alone.py"}:
        modules.append("scripts.hooks.checkpoint_gate")
    if filenames >= {"stop-context-deferral.py", "herdr_tab_title.py"}:
        modules.append("scripts.lib.conf")
    if filenames >= {"stop-conversation-pause.py", "herdr_tab_title.py"}:
        modules.extend(("scripts.lib.script_prompts", "scripts.production.staff"))
    return modules


def _preload_shared_dependencies(modules: Sequence[str]) -> None:
    gc_was_enabled = gc.isenabled()
    gc.disable()
    try:
        for module in modules:
            # The hook still handles its normal import if preloading fails.
            with suppress(Exception):
                _ = importlib.import_module(module)
    finally:
        if gc_was_enabled:
            gc.enable()


@contextmanager
def _freeze_parent_heap() -> Generator[None]:
    """Keep child garbage collection from copying pages it inherits."""
    freeze_here = gc.get_freeze_count() == 0
    if freeze_here:
        gc.freeze()
    try:
        yield
    finally:
        if freeze_here:
            gc.unfreeze()


def _run_hooks(event: str, hooks: Iterable[HookSpec | CopyPolicyHook], payload: str) -> DispatchResult:
    with _freeze_parent_heap():
        results: list[HookResult] = []
        for spec in hooks:
            timeout = (lambda spec=spec: spec.timeout) if isinstance(spec, CopyPolicyHook) else spec.timeout
            results.append(run_hook(HOOKS / spec.filename, payload, timeout))
    return combine_results(event, results)


def _approved_hooks(hooks: Sequence[HookSpec | CopyPolicyHook],
                    control: socket.socket) -> Generator[HookSpec | CopyPolicyHook]:
    control.setblocking(False)
    for spec in hooks:
        # The dispatcher sends no data after approval. EOF means it exited.
        try:
            _ = control.recv(1)
        except BlockingIOError:
            yield spec
        else:
            return


def _preloaded_dispatch(event: str, hooks: Sequence[HookSpec | CopyPolicyHook],
                        payload: str, modules: Sequence[str]) -> DispatchResult:
    deadline = time.monotonic() + HOOK_SETUP_SECONDS
    parent, child = socket.socketpair()
    with parent, child, tempfile.TemporaryFile() as report:
        pid = os.fork()
        if pid == 0:
            parent.close()
            try:
                _preload_shared_dependencies(modules)
                child.sendall(b"1")
                # Wait for permission before a hook can change state.
                if child.recv(1):
                    result = _run_hooks(event, _approved_hooks(hooks, child), payload)
                    _write_frame(report, [result.exit_code, result.stdout, result.stderr])
            except BaseException:
                os._exit(1)
            os._exit(0)
        child.close()
        accepted = False
        try:
            parent.settimeout(max(0, deadline - time.monotonic()))
            if parent.recv(1) and time.monotonic() < deadline:
                accepted = parent.send(b"1") == 1
        except OSError:
            pass
        finally:
            if not accepted:
                with suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGKILL)
            _, status = os.waitpid(pid, 0)
        if accepted:
            if status:
                return DispatchResult(1, "", "hook dispatcher worker failed")
            _ = report.seek(0)
            code, stdout, stderr = cast(tuple[int, str, str], json.load(report))
            return DispatchResult(code, stdout, stderr)
    return _run_hooks(event, hooks, payload)


def main() -> NoReturn:
    if len(sys.argv) != 2 or sys.argv[1] not in EVENTS:
        print("usage: hook_dispatch.py <pre_tool_use|post_tool_use|stop>", file=sys.stderr)
        raise SystemExit(1)
    event = sys.argv[1]
    payload = sys.stdin.read()
    try:
        parsed = cast(object, json.loads(payload))
        tool_name = cast(ToolEventPayload, cast(object, parsed)).get("tool_name", "") if isinstance(parsed, dict) else ""
    except ValueError:
        tool_name = ""
    hooks = [spec for spec in EVENTS[event] if spec.matcher is None or re.fullmatch(spec.matcher, tool_name)]
    modules = _shared_dependencies(hooks)
    result = _preloaded_dispatch(event, hooks, payload, modules) if modules else _run_hooks(event, hooks, payload)
    if result.stdout:
        print(result.stdout)
    if result.stderr:
        print(result.stderr, file=sys.stderr)
    raise SystemExit(result.exit_code)


if __name__ == "__main__":
    main()
