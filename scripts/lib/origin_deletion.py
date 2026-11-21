"""Delete an origin branch only at its expected commit."""
from __future__ import annotations

try:
    with open(__file__[:__file__.rfind("/") + 1] + "package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
from contextlib import suppress
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
import re
import signal
import subprocess
import sys

from scripts.lib import cancellation, process_group
from scripts.lib.process_failure import failure_detail
from scripts.production import production_doc, staff


@dataclass(frozen=True)
class Deleted:
    """The push deleted the expected ref."""


@dataclass(frozen=True)
class Absent:
    """The destination has no such ref."""


class RetentionCause(Enum):
    PROTECTION = "protection"
    GIT_FAILURE = "git failure"


@dataclass(frozen=True)
class Kept:
    reason: str
    cause: RetentionCause = RetentionCause.PROTECTION


def _git(repository: Path, timeout: float, *args: str) -> subprocess.CompletedProcess[str]:
    command = ["git", "-C", str(repository), *args]
    with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, start_new_session=True, pass_fds=staff.operation_fds()) as child:
        try:
            stdout, stderr = child.communicate(timeout=timeout)
        except BaseException:
            if child.returncode is None:
                with cancellation.defer_cleanup_signals():
                    with suppress(ProcessLookupError):
                        process_group.send_process_group(child.pid, signal.SIGKILL)
                    _ = child.wait()
            raise
        return subprocess.CompletedProcess(command, child.returncode, stdout, stderr)


def delete(repository: Path, destination: str, ref: str, expected: str, *,
           timeout: float = 30) -> Deleted | Absent | Kept:
    """Read current staff authority just before the push. Use a lease on the expected commit."""
    try:
        if (timeout <= 0 or not ref.startswith("refs/heads/") or not destination or destination.startswith("-")
                or not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", expected)
                or not expected.strip("0")
                or _git(repository, timeout, "check-ref-format", ref).returncode):
            return Kept("invalid time limit, destination, branch ref, or expected commit", RetentionCause.GIT_FAILURE)
        if reason := production_doc.origin_ref_protection(ref):
            return Kept(reason)
        pushed = _git(repository, timeout, "push", "--porcelain", f"--force-with-lease={ref}:{expected}",
                      destination, "--delete", ref)
        if pushed.returncode == 0:
            if any(line.startswith("-\t") for line in pushed.stdout.splitlines()):
                return Deleted()
            return Absent()
        if "stale info" in pushed.stdout or "remote ref does not exist" in pushed.stderr:
            urls = _git(repository, timeout, "remote", "get-url", "--push", "--all", destination)
            targets = urls.stdout.splitlines() if urls.returncode == 0 else [destination]
            if targets and all(_git(repository, timeout, "ls-remote", "--exit-code", "--heads", "--", target, ref)
                               .returncode == 2 for target in targets):
                return Absent()
        return Kept(f"delete failed or branch moved; {failure_detail(pushed)}", RetentionCause.GIT_FAILURE)
    except production_doc.ProductionPathsError as error:
        return Kept(str(error))
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        return Kept(str(error), RetentionCause.GIT_FAILURE)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("repository", type=Path)
    _ = parser.add_argument("destination")
    _ = parser.add_argument("ref")
    _ = parser.add_argument("expected")
    class Arguments(argparse.Namespace):
        repository: Path = Path()
        destination: str = ""
        ref: str = ""
        expected: str = ""

    args = parser.parse_args(namespace=Arguments())
    result = delete(args.repository, args.destination, args.ref, args.expected)
    if isinstance(result, Kept):
        failed = result.cause is RetentionCause.GIT_FAILURE
        print(f"Kept origin ref {args.ref}: {result.reason}", file=sys.stderr if failed else sys.stdout)
        return int(failed)
    if isinstance(result, Deleted):
        print(f"Deleted origin ref {args.ref}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
