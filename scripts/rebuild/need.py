"""Record, list, and clear needed system rebuilds."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from shlex import quote
from typing import Literal, Protocol, TypedDict, cast

Machine = Literal["natedev", "mac"]
REVISION = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
FULL_NEED_COMMIT = re.compile(r"[0-9a-f]{40}")
SHORT_COMMIT = re.compile(r"[0-9a-f]{4,39}")
GENERATION = re.compile(r"system-[0-9]+-link")
NO_REASON = "No reason given"


class SourceWatch(Protocol):
    """Record file observations that determine whether a result can be saved."""

    def watch(self, path: Path) -> None: ...
    def verified_absence(self, path: Path) -> bool: ...
    def mark_incomplete(self) -> None: ...


class NeedRecord(TypedDict):
    """The JSON fields of an open rebuild need."""

    id: str
    machine: Machine
    nixos_commit: str
    reason: str
    concerns: str
    requested_by: str
    created_at: str


class CompletedNeedRecord(NeedRecord):
    """A rebuild need with evidence of completion."""

    cleared_by: str


@dataclass(frozen=True)
class MachineNeeds:
    """The newest need, reasons, and open records for one machine."""

    newest: NeedRecord
    reasons: tuple[str, ...]
    records: tuple[NeedRecord, ...]

    def record(self) -> NeedRecord:
        """Return the newest fields with the joined open reasons."""
        return {**self.newest, "reason": " ".join(self.reasons)}


def needs_folder() -> Path:
    """Return the needs folder selected by REBUILD_NEEDS_DIR."""
    return Path(os.environ.get("REBUILD_NEEDS_DIR", "~/.local/state/rebuild/needs")).expanduser()


def _read_fields(path: Path) -> dict[str, object]:
    value = cast(object, json.loads(path.read_text(encoding="utf-8")))
    if not isinstance(value, dict):
        raise ValueError("Expected a rebuild request object.")
    fields = cast(dict[str, object], value)
    _ = fields.setdefault("reason", "")
    if fields.get("machine") not in ("natedev", "mac"):
        raise ValueError("Invalid machine.")
    for key in ("id", "nixos_commit", "reason", "concerns", "requested_by", "created_at"):
        if not isinstance(fields.get(key), str):
            raise ValueError(f"Invalid {key}.")
    commit = cast(str, fields["nixos_commit"])
    if not (FULL_NEED_COMMIT.fullmatch(commit) or SHORT_COMMIT.fullmatch(commit)):
        raise ValueError("Invalid nixos_commit.")
    if fields["id"] != f"{fields['machine']}-{commit}":
        raise ValueError("Invalid request ID.")
    _ = request_time(cast(str, fields["created_at"]))
    return fields


def request_time(value: str) -> datetime:
    """Use UTC for old times without a UTC offset."""
    created = datetime.fromisoformat(value)
    return created if created.tzinfo is not None else created.replace(tzinfo=timezone.utc)


def _open_needs(errors: list[str] | None = None, *, watch: SourceWatch | None = None) -> list[NeedRecord]:
    folder = needs_folder()
    if watch is not None:
        watch.watch(folder)
    try:
        paths = sorted(path for path in folder.iterdir() if path.suffix == ".json")
    except FileNotFoundError:
        if watch is not None and not watch.verified_absence(folder):
            raise
        return []
    records: list[NeedRecord] = []
    for path in paths:
        if watch is not None:
            watch.watch(path)
        try:
            fields = _read_fields(path)
            if path.stem != fields["id"]:
                raise ValueError("The file name and request ID differ.")
            records.append(cast(NeedRecord, cast(object, fields)))
        except FileNotFoundError:
            if watch is not None:
                watch.mark_incomplete()
            continue
        except (OSError, ValueError, RecursionError) as error:
            if watch is not None:
                watch.mark_incomplete()
            message = f"Rebuild request {path.name}: {error}"
            if errors is None:
                print(message, file=sys.stderr)
            else:
                errors.append(message)
    return records


@contextmanager
def _need_lock(folder: Path) -> Generator[None]:
    """Hold the folder lock while an add or clear changes a record."""
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def _write_record(path: Path, record: NeedRecord, *, create_only: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                     prefix=".", suffix=".tmp", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            _ = stream.write(json.dumps(record, indent=2) + "\n")
            stream.flush()
            if create_only:
                try:
                    os.link(temporary, path)
                except FileExistsError:
                    pass
            else:
                _ = temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def add_need(machine: Machine, commit: str, reason: str, concerns: str,
             requested_by: str = "") -> None:
    """Create one need unless the same machine and commit already have a record."""
    reason = reason.strip()
    if not reason:
        raise ValueError("Give --reason and explain what changes for the user after the rebuild.")
    if not FULL_NEED_COMMIT.fullmatch(commit):
        raise ValueError("Give --commit as a full 40-character Git hash.")
    identifier = f"{machine}-{commit}"
    folder = needs_folder()
    filename = f"{identifier}.json"
    with _need_lock(folder):
        if (folder / filename).exists() or (folder / "done" / filename).exists():
            return
        record: NeedRecord = {
            "id": identifier,
            "machine": machine,
            "nixos_commit": commit,
            "reason": reason,
            "concerns": concerns,
            "requested_by": requested_by,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        _write_record(folder / filename, record, create_only=True)


def read_needs(errors: list[str] | None = None, *, watch: SourceWatch | None = None) -> list[MachineNeeds]:
    """Read the needs folder and return one summary per machine without subprocesses."""
    groups: dict[Machine, list[NeedRecord]] = {}
    for record in _open_needs(errors, watch=watch):
        groups.setdefault(record["machine"], []).append(record)
    summaries: list[MachineNeeds] = []
    for machine in sorted(groups):
        records = sorted(groups[machine], key=lambda record: request_time(record["created_at"]))
        summaries.append(MachineNeeds(records[-1], tuple(record["reason"].strip() or NO_REASON
                                                       for record in records), tuple(records)))
    return summaries


def read_completed_needs(damaged: list[str] | None = None) -> tuple[CompletedNeedRecord, ...]:
    """Read each completed request once, in order of completion with the newest first."""
    records: dict[tuple[Machine, str], CompletedNeedRecord] = {}
    for path in sorted((needs_folder() / "done").glob("*.json")):
        try:
            fields = _read_fields(path)
            if not isinstance(fields.get("cleared_by"), str) or not fields["cleared_by"]:
                raise ValueError("Missing completion evidence.")
        except FileNotFoundError:
            continue
        except (OSError, ValueError, RecursionError) as error:
            if damaged is not None:
                damaged.append(path.name)
            else:
                print(f"Completed rebuild request {path.name}: {error}", file=sys.stderr)
            continue
        record = cast(CompletedNeedRecord, cast(object, fields))
        key = (record["machine"], record["nixos_commit"])
        if key not in records or completion_time(record) > completion_time(records[key]):
            records[key] = record
    return tuple(sorted(records.values(), key=completion_time, reverse=True))


def completion_time(record: CompletedNeedRecord) -> datetime:
    """Read the time in the completion evidence or use the request time for old records."""
    try:
        completed = datetime.fromisoformat(record["cleared_by"].split(" ", 1)[0])
        if completed.tzinfo is not None:
            return completed
    except ValueError:
        pass
    return request_time(record["created_at"])


def _archive(record: NeedRecord, commit: str, evidence: str) -> None:
    folder = needs_folder()
    source = folder / f"{record['id']}.json"
    with _need_lock(folder):
        if not source.exists():
            return
        identifier = f"{record['machine']}-{commit}"
        completed: CompletedNeedRecord = {**record, "id": identifier, "nixos_commit": commit, "cleared_by": evidence}
        _write_record(folder / "done" / f"{identifier}.json", completed, create_only=True)
        source.unlink()


def system_path(value: str) -> bool:
    """Accept a single absolute Nix store path."""
    return value.startswith("/nix/store/") and len(value.split("/")) == 4 and not any(c.isspace() for c in value)


@dataclass(frozen=True)
class ActiveSystem:
    """The running store path and its revision when proof is available."""

    path: str
    revision: str = ""
    evidence: str = ""


ACTIVE_PROBE = r'''
current=/run/current-system
profile=/nix/var/nix/profiles/system
case $(uname -s) in
    Darwin) stamp() { stat -f '%Fc %i' "$1"; } ;;
    *) stamp() { stat -c '%z %i' "$1"; } ;;
esac
snapshot() {
    active=$(stamp "$current") || return 1
    running=$(realpath "$current") || return 1
    printf '%s\nactivated %s\n' "$running" "$active"
    if [ -e "$running/configuration-revision" ]; then
        revision=$(cat "$running/configuration-revision") || revision=
        printf 'revision %s\n' "$revision"
    fi
    generation=$(readlink "$profile") || return 0
    changed=$(stamp "$profile") || return 0
    if [ "$(realpath "$profile")" = "$running" ]; then
        printf 'generation %s %s\n' "${generation##*/}" "$changed"
    fi
}
before=$(snapshot 2>/dev/null) || exit 1
'''


def active_probe(machine: Machine, *, log: str = "", ssh: str = "mac") -> list[str]:
    """Read the active links and the optional log from one machine."""
    script = ACTIVE_PROBE
    if log:
        path = '"$HOME"/' + quote(log[2:]) if log.startswith("~/") else quote(log)
        script += f'lines=$(tail -n 200 {path} 2>/dev/null || :)\n'
    script += 'after=$(snapshot 2>/dev/null) && [ "$before" = "$after" ] || exit 1\n'
    script += 'printf "%s\\n--- generations ---\\n%s\\n" "$after" "${lines-}"'
    return ["sh", "-c", script] if machine == "natedev" else ["ssh", "-o", "BatchMode=yes", ssh, script]


def _link_time(value: str) -> datetime:
    stamp, _inode = value.rsplit(" ", 1)
    if not stamp[0:1].isdigit():
        raise ValueError("Invalid link timestamp.")
    if re.fullmatch(r"[0-9]+\.[0-9]+", stamp):
        seconds, fraction = stamp.split(".")
        return datetime.fromtimestamp(int(seconds), timezone.utc).replace(microsecond=int(fraction[:6].ljust(6, "0")))
    parsed = datetime.fromisoformat(stamp)
    if parsed.tzinfo is None:
        raise ValueError("Missing time zone.")
    return parsed


def active_system(output: str, log: str = "") -> ActiveSystem | None:
    """Identify the running revision from embedded metadata or its active switch."""
    metadata, separator, probed_log = output.partition("\n--- generations ---")
    lines = metadata.splitlines()
    if not lines or not system_path(lines[0]):
        return None
    running = lines[0]
    unknown = ActiveSystem(running)
    if not separator or (probed_log and not probed_log.startswith("\n")):
        return unknown
    for line in lines[1:]:
        if line == "revision" or line.startswith("revision "):
            revision = line.partition(" ")[2]
            if REVISION.fullmatch(revision.removesuffix("-dirty")):
                evidence = f"{datetime.now(timezone.utc).isoformat()} running {revision} {running}"
                return ActiveSystem(running, revision, evidence)
            return unknown
    activated = next((line.removeprefix("activated ") for line in lines[1:] if line.startswith("activated ")), "")
    profile = next((line.removeprefix("generation ") for line in lines[1:] if line.startswith("generation ")), "")
    generation, _, changed = profile.partition(" ")
    if not GENERATION.fullmatch(generation):
        return unknown
    try:
        active_at = _link_time(activated)
        # A later profile change can select a generation for the next boot.
        if _link_time(changed) >= active_at:
            return unknown
        for line in reversed((log or probed_log).splitlines()):
            fields = line.split()
            if len(fields) < 3 or fields[2].rsplit("/", 1)[-1] != generation:
                continue
            if len(fields) != 5:
                return unknown
            at, action, _, revision, store = fields
            # A log entry in the same second cannot establish order.
            if (action == "switch" and store == running and datetime.fromisoformat(at) > active_at
                    and REVISION.fullmatch(revision.removesuffix("-dirty"))):
                return ActiveSystem(running, revision, line)
            return unknown
    except (ValueError, TypeError, OverflowError, OSError):
        return unknown
    return unknown


def clear_running_needs(machine: Machine, system: ActiveSystem, nixos_repo: Path,
                        run: Callable[[list[str]], str]) -> None:
    """Archive requests covered by the clean active revision."""
    revision = system.revision
    if not system.evidence or not REVISION.fullmatch(revision):
        return
    repo = str(nixos_repo)
    unknown: set[str] = set()
    for record in _open_needs():
        if record["machine"] != machine:
            continue
        commit = record["nixos_commit"]
        if not FULL_NEED_COMMIT.fullmatch(commit):
            resolved = run(["git", "-C", repo, "rev-parse", "--verify", "--quiet", "--end-of-options",
                            f"{commit}^{{commit}}"])
            if (not FULL_NEED_COMMIT.fullmatch(resolved) or not resolved.startswith(commit)
                    or run(["git", "-C", repo, "rev-parse", f"--disambiguate={commit}"]) != resolved):
                continue
            commit = resolved
        base = run(["git", "-C", repo, "merge-base", commit, revision])
        if base == commit:
            _archive(record, commit, system.evidence)
        elif not base:
            for candidate, label in ((revision, f"logged commit {revision}"),
                                     (commit, f"need commit {commit} (need {record['id']})")):
                if candidate not in unknown and not run(["git", "-C", repo, "rev-parse", "--verify",
                                                        f"{candidate}^{{commit}}"]):
                    print(f"anomaly: unknown {label}")
                    unknown.add(candidate)


def clear_needs(machine: Machine, generations: Path, nixos_repo: Path) -> None:
    """Check the selected machine before moving completed requests."""
    try:
        log = generations.read_text(encoding="utf-8")
    except FileNotFoundError:
        log = ""
    system = active_system(command_output(active_probe(machine)), log)
    if system is not None:
        clear_running_needs(machine, system, nixos_repo, command_output)


def command_output(command: list[str], *, timeout: int = 30) -> str:
    """Return command output when the command succeeds within its time limit."""
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout.strip(" \t\r\n") if result.returncode == 0 else ""


class Arguments(argparse.Namespace):
    """The arguments selected by the command parser."""

    command: str = ""
    machine: Machine = "natedev"
    commit: str = ""
    reason: str = ""
    concerns: str = ""
    requested_by: str = ""
    json: bool = False
    generations: Path = Path()
    nixos_repo: Path = Path()


def main(argv: list[str]) -> int:
    """Run one command for the rebuild needs folder."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    add = commands.add_parser("add")
    _ = add.add_argument("--machine", choices=("natedev", "mac"), required=True)
    _ = add.add_argument("--commit", required=True)
    _ = add.add_argument("--reason", required=True, help="Explain what changes for the user after the rebuild.")
    _ = add.add_argument("--concerns", required=True)
    _ = add.add_argument("--requested-by", default="")
    listing = commands.add_parser("list")
    _ = listing.add_argument("--json", action="store_true")
    clear = commands.add_parser("clear")
    _ = clear.add_argument("--machine", choices=("natedev", "mac"), required=True)
    _ = clear.add_argument("--generations", type=Path, required=True)
    _ = clear.add_argument("--nixos-repo", type=Path, required=True)
    args = parser.parse_args(argv, namespace=Arguments())
    if args.command == "add":
        try:
            add_need(args.machine, args.commit, args.reason, args.concerns, args.requested_by)
        except ValueError as error:
            add.error(str(error))
    elif args.command == "clear":
        clear_needs(args.machine, args.generations, args.nixos_repo)
    else:
        records = [summary.record() for summary in read_needs()]
        if args.json:
            print(json.dumps(records))
        else:
            for record in records:
                print(f"{record['machine']} {record['nixos_commit']} {record['reason']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
