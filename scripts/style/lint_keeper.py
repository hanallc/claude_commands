"""Supervise style work when the caller can no longer receive a result."""

from __future__ import annotations

import json
import os
import select
import signal
import subprocess
import sys
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryFile
from typing import TextIO, cast

from scripts.copies import copies
from scripts.lib import build_folders, process_group
from scripts.lib.descendants import adopt_descendants
from scripts.production.production_doc import Refusal
from scripts.style import style_plan, style_sync


@dataclass(frozen=True)
class LintRequest:
    caller: style_sync.OwnerRecord
    folder_parent: Path
    source: Path
    tip: str
    slot_owner: str
    verify: Path
    stop_seconds: float
    inherited_locks: tuple[int, ...]


def stop_verifier_group(group: int, stop_seconds: float) -> None:
    with suppress(ProcessLookupError):
        process_group.send_process_group(group, signal.SIGTERM)
        _ = select.select([], [], [], stop_seconds)
        process_group.send_process_group(group, signal.SIGKILL)


def run_verifier(request: LintRequest, directory: Path, checkout: Path) -> tuple[int, str]:
    adopted = adopt_descendants()
    with TemporaryFile(mode="w+t", dir=directory) as output, subprocess.Popen(
            style_sync.verifier_command(directory, request.caller.owner, verify=request.verify),
            cwd=checkout, env={**os.environ, "UNIT_DETACHED_RUN": "1"},
            stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT, text=True,
            process_group=0, pass_fds=copies.COMMAND_LOCKS.get()) as child:
        # Keep the group leader unreaped until the group has stopped.
        _ = os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOWAIT)
        if not adopted or style_plan.child_process_ids() - {child.pid}:
            stop_verifier_group(child.pid, request.stop_seconds)
        (directory / style_sync.VERIFIER_FILE).unlink(missing_ok=True)
        status = child.wait()
        if adopted:
            style_plan.stop_adopted_children()
        _ = output.seek(0)
        return status, output.read()


def stop_lint_worker(worker: style_plan.StyleWorkerGroup, directory: Path, stop_seconds: float) -> None:
    stop_at = time.monotonic() + stop_seconds
    worker.send(signal.SIGTERM)
    if not select.select([worker.ended], [], [], stop_seconds)[0]:
        worker.send(signal.SIGKILL)
        _ = os.waitid(os.P_PID, worker.pid, os.WEXITED | os.WNOWAIT)
    # The worker can no longer clear the record or reap the verifier.
    try:
        verifier_group = int((directory / style_sync.VERIFIER_FILE).read_text())
    except FileNotFoundError:
        pass
    else:
        stop_verifier_group(verifier_group, stop_seconds)
    remaining = stop_at - time.monotonic()
    if remaining > 0:
        _ = select.select([], [], [], remaining)


def lint_in_slot(request: LintRequest, directory: Path, protocol: TextIO) -> style_sync.LintCompleted:
    token = copies.COMMAND_LOCKS.set(request.inherited_locks)
    try:
        with copies.standing(request.slot_owner, request.source, copies.CommittedSource(request.tip),
                             step="lint") as checkout:
            target = checkout / "target"
            retained_identity = style_sync.target_identity(target)
            (directory / "checkout").symlink_to(checkout, target_is_directory=True)
            print("ready", file=protocol, flush=True)
            if not sys.stdin.readline():
                raise Refusal("lint caller ended before verification")
            status, output = run_verifier(request, directory, checkout)
            rewritten = bool(style_sync.git(checkout, "status", "--porcelain", "--", ".", ":(exclude)target"))
            try:
                rewritten |= style_sync.target_identity(target) != retained_identity
            except (OSError, Refusal):
                rewritten = True
            return {"status": status, "output": output,
                    "rewritten": rewritten, "checkout": str(checkout)}
    finally:
        copies.COMMAND_LOCKS.reset(token)


def keep(request: LintRequest, protocol: TextIO) -> None:
    with build_folders.temporary_build_folder(build_folders.UniqueBuildFolder(
            style_sync.lint_prefix(), build_folders.ExplicitBuildParent(request.folder_parent))) as directory, \
            TemporaryFile(mode="w+t", dir=directory) as reply:
        caller_fd = sys.stdin.fileno()
        monitor = select.poll()
        monitor.register(caller_fd, select.POLLHUP)
        if monitor.poll(0):
            return
        request.caller.write(directory)

        def work() -> None:
            _ = os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
            result: style_sync.LintCompleted | style_sync.LintPreparationFailed
            try:
                result = lint_in_slot(request, directory, protocol)
            except Exception as error:
                result = {"error": str(error)}
            json.dump(result, reply)
            reply.flush()

        with style_plan.worker_group(work) as worker:
            monitor.register(worker.ended, select.POLLIN)
            if any(fd == caller_fd for fd, _ in monitor.poll()):
                stop_lint_worker(worker, directory, request.stop_seconds)
                return
        _ = reply.seek(0)
        print(json.dumps(cast(object, json.load(reply))), file=protocol, flush=True)


def main(argv: list[str]) -> int:
    caller, repo, parent, source, tip, slot_owner, verify, stop_seconds, *locks = argv
    request = LintRequest(style_sync.OwnerRecord(int(caller), Path(repo)), Path(parent), Path(source),
                          tip, slot_owner, Path(verify), float(stop_seconds), tuple(map(int, locks)))
    with suppress(BrokenPipeError), os.fdopen(os.dup(sys.stdout.fileno()), "w", encoding="utf-8") as protocol:
        keep(request, protocol)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
