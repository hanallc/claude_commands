"""Commit a main-checkout change, push it, and send or queue the Mac pull."""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import subprocess
import sys
from typing import cast

from scripts.lib import main_push_lock, waiting_rows
from scripts.lib.atomic_file import write_text_atomic


class Refusal(Exception):
    """The checkout cannot publish this change."""


@dataclass(frozen=True)
class ExistingFile:
    text: str


@dataclass(frozen=True)
class MissingFile:
    """The destination did not exist before the edit."""


@dataclass(frozen=True)
class MacPullSourceUnrecorded:
    """An older queue record uses the checkout supplied to drain."""


@dataclass(frozen=True)
class MacCheckoutPull:
    mac: PurePosixPath
    source: Path | MacPullSourceUnrecorded


DEFAULT_MAC_CHECKOUT = PurePosixPath("~/.claude")
_LEGACY_MAC_PULL = (MacCheckoutPull(DEFAULT_MAC_CHECKOUT, MacPullSourceUnrecorded()),)


@dataclass(frozen=True)
class PendingMacPull:
    label: str
    text: str
    since: str
    reason: str
    pulls: tuple[MacCheckoutPull, ...] = _LEGACY_MAC_PULL


@dataclass(frozen=True)
class NoPendingMacPull:
    """There is no valid queued Mac pull."""


@dataclass(frozen=True)
class MacPulled:
    """The Mac completed its pull."""


@dataclass(frozen=True)
class MacPullPending:
    """The Mac is unreachable and its pull is queued."""


@dataclass(frozen=True)
class MacPullFailed:
    reason: str


@dataclass(frozen=True)
class CommandTimeLimit:
    seconds: float


@dataclass(frozen=True)
class NoTimeLimit:
    """The caller retains the existing unlimited Git operation."""


UNLIMITED_GIT = NoTimeLimit()
ZONE_SSH_LIMIT = CommandTimeLimit(120)
PUBLICATION_PREFIXES = ("tune(", "zone(", "user zone: ")


def _timeout_detail(error: subprocess.TimeoutExpired) -> str:
    streams = [output.decode(errors="replace")
               for output in (error.stderr, error.stdout) if output]
    return f"timed out after {error.timeout:g} seconds" + "".join(f"; {output.strip()}" for output in streams)


def git(checkout: Path, *arguments: str,
        time_limit: CommandTimeLimit | NoTimeLimit = UNLIMITED_GIT) -> subprocess.CompletedProcess[str]:
    command = ["git", "-C", str(checkout), *arguments]
    try:
        return subprocess.run(command, capture_output=True, text=True, check=False,
                              timeout=time_limit.seconds if isinstance(time_limit, CommandTimeLimit) else None)
    except subprocess.TimeoutExpired as error:
        return subprocess.CompletedProcess(command, 124, "", _timeout_detail(error))


def first_line(result: subprocess.CompletedProcess[str]) -> str:
    parts = [output.strip().splitlines()[0] for output in (result.stderr, result.stdout) if output.strip()]
    return "; ".join(parts) if parts else f"exit {result.returncode}"


def checkout_of(conf: Path) -> Path:
    found = git(conf.parent, "rev-parse", "--show-toplevel")
    if found.returncode:
        raise Refusal(f"{conf.parent} is not in a git checkout: {first_line(found)}")
    return Path(found.stdout.strip())


def ready(checkout: Path, *, fetch_limit: CommandTimeLimit | NoTimeLimit = UNLIMITED_GIT) -> None:
    """Move main to origin/main with the publisher commits on top; keep every waiting row live."""
    branch = git(checkout, "branch", "--show-current").stdout.strip()
    if branch != "main":
        raise Refusal(f"{checkout} is on {branch or 'no branch'}, not main")
    with main_push_lock.main_push_lock(checkout):
        fetched = git(checkout, "fetch", "-q", "origin", "main", time_limit=fetch_limit)
        if fetched.returncode:
            raise Refusal(f"git fetch origin main: {first_line(fetched)}")
        listed = git(checkout, "log", "--format=%H %h %s", "--reverse", "origin/main..main")
        if listed.returncode:
            raise Refusal(f"cannot inspect unpublished commits: {first_line(listed)}")
        commits = [cast(tuple[str, str, str], tuple(line.split(" ", 2))) for line in listed.stdout.splitlines()]
        for _, short, subject in commits:
            if not subject.startswith(PUBLICATION_PREFIXES):
                raise Refusal(f"main holds other commit {short} {subject} that origin does not have; main is unchanged")
        if not git(checkout, "merge-base", "--is-ancestor", "origin/main", "HEAD").returncode:
            return
        target = _rebuilt(checkout, commits)
        try:
            waiting_rows.move_to(checkout, target)
        except (OSError, UnicodeError) as error:
            raise Refusal(f"main is unchanged: {' '.join(str(error).split())}") from error


def _rebuilt(checkout: Path, commits: list[tuple[str, str, str]]) -> str:
    """Copy each publisher commit onto origin/main without the worktree; drop a commit that origin has."""
    def run(*arguments: str, input_text: str | None = None,
            environment: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["git", "-C", str(checkout), *arguments], input=input_text, text=True,
                              capture_output=True, env=environment, check=False)

    tip = run("rev-parse", "origin/main").stdout.strip()
    for commit_id, short, subject in commits:
        parents = run("rev-list", "--parents", "-n", "1", commit_id).stdout.split()[1:]
        if len(parents) != 1:
            raise Refusal(f"main holds merge commit {short} {subject}; main is unchanged")
        merged = run("merge-tree", "--write-tree", "--name-only", "--no-messages",
                     "--merge-base", parents[0], tip, commit_id)
        if merged.returncode:
            files = ", ".join(merged.stdout.splitlines()[1:]) or first_line(merged)
            raise Refusal(f"rebase conflict in {short} {subject}: {files}; main is unchanged")
        tree = merged.stdout.splitlines()[0]
        if tree == run("rev-parse", f"{tip}^{{tree}}").stdout.strip():
            continue
        author = run("show", "-s", "--format=%an%x00%ae%x00%ad", "--date=raw", commit_id).stdout.rstrip("\n")
        name, email, date = author.split("\0")
        message = run("show", "-s", "--format=%B", commit_id).stdout
        made = run("commit-tree", tree, "-p", tip, input_text=message,
                   environment={**os.environ, "GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email,
                                "GIT_AUTHOR_DATE": date})
        if made.returncode:
            raise Refusal(f"cannot copy {short} {subject}: {first_line(made)}; main is unchanged")
        tip = made.stdout.strip()
    return tip


def commit(checkout: Path, conf: Path, message: str, before: ExistingFile | MissingFile,
           *, subject_prefix: str, unchanged: str) -> str:
    """Commit only the named file; restore it if Git fails and HEAD lacks its contents."""
    if not message.startswith(subject_prefix):
        raise Refusal(f"commit subject must start with {subject_prefix!r}")
    committed = git(checkout, "commit", "-q", "--only", "-m", message, "--", str(conf))
    if committed.returncode:
        if git(checkout, "diff", "--quiet", "HEAD", "--", str(conf)).returncode == 0:
            print(f"anomaly: git commit: {first_line(committed)}; the commit exists", file=sys.stderr)
        else:
            if isinstance(before, MissingFile):
                conf.unlink(missing_ok=True)
            else:
                write_text_atomic(conf, before.text)
            raise Refusal(f"git commit: {first_line(committed)}; {unchanged}")
    return git(checkout, "rev-parse", "--short", "HEAD").stdout.strip()


def push(checkout: Path, *, time_limit: CommandTimeLimit | NoTimeLimit = UNLIMITED_GIT) -> tuple[bool, str]:
    """Push main only when every unpublished commit belongs to a configuration publisher."""
    with main_push_lock.main_push_lock(checkout):
        listed = git(checkout, "log", "--format=%s", "origin/main..main")
        if listed.returncode:
            return False, f"not pushed: {first_line(listed)}"
        unpushed = listed.stdout.splitlines()
        if not unpushed:
            return True, "origin has it"
        others = [subject for subject in unpushed if not subject.startswith(PUBLICATION_PREFIXES)]
        if others:
            return False, f"not pushed: main holds {len(others)} other commit(s) origin does not have"
        pushed = git(checkout, "push", "-q", "origin", "main", time_limit=time_limit)
        if pushed.returncode:
            return False, f"not pushed: {first_line(pushed)}"
        return True, "pushed"


def ssh(remote: str, *, ssh_program: str, host: str = "mac",
        time_limit: CommandTimeLimit = ZONE_SSH_LIMIT) -> subprocess.CompletedProcess[str] | str:
    """Run a remote command; a string describes why it could not run."""
    command = [ssh_program, "-o", "BatchMode=yes", host, remote]
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=time_limit.seconds, check=False)
    except subprocess.TimeoutExpired as error:
        return _timeout_detail(error)
    except OSError as error:
        return str(error)


def remote_path(path: PurePosixPath) -> str:
    """Quote a Mac path and let the Mac expand its home directory."""
    text = str(path)
    if text == "~":
        return '"$HOME"'
    if text.startswith("~/"):
        return '"$HOME"/' + shlex.quote(text[2:])
    return shlex.quote(text)


def mac_pull_command(mac: PurePosixPath, source: Path) -> str:
    """Fetch main without shared fetch state and move to its advertised tip; waiting rows stay live."""
    url = f"natedev:{shlex.quote(str(source))}"
    return (f"cd {remote_path(mac)} && tip=$(git ls-remote {url} refs/heads/main | cut -f1) "
            f"&& git fetch -q --no-write-fetch-head {url} main "
            "&& if [ -f scripts/lib/waiting_rows.py ]; "
            "then scripts/lib/py scripts/lib/waiting_rows.py fast-forward \"$tip\"; "
            "else git merge -q --ff-only \"$tip\"; fi; printf 'rc=%s\\n' \"$?\"")


def pull_mac(checkout: Path, *, ssh_program: str, mac: PurePosixPath = DEFAULT_MAC_CHECKOUT,
             time_limit: CommandTimeLimit = ZONE_SSH_LIMIT) -> MacPulled | MacPullFailed:
    """Return the completed Mac pull or its failure reason."""
    done = ssh(mac_pull_command(mac, checkout), ssh_program=ssh_program, time_limit=time_limit)
    if isinstance(done, str):
        return MacPullFailed(done)
    match = re.search(r"(?:^|\n)rc=(\d+)", done.stdout)
    if done.returncode or match is None or int(match.group(1)):
        return MacPullFailed(first_line(done))
    return MacPulled()


def queue_path() -> Path:
    state = os.environ.get("ZONE_STATE") or str(
        Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "zone")
    return Path(state) / "mac.json"


def read_queue() -> PendingMacPull | NoPendingMacPull:
    try:
        value = cast(object, json.loads(queue_path().read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return NoPendingMacPull()
    if not isinstance(value, dict):
        return NoPendingMacPull()
    fields = cast(dict[str, object], value)
    if not all(isinstance(fields.get(key), str) for key in ("zone", "text", "since", "reason")):
        return NoPendingMacPull()
    pulls: list[MacCheckoutPull] = []
    for value in cast(list[object], fields.get("pulls", [{"mac": str(DEFAULT_MAC_CHECKOUT), "source": ""}])):
        row = cast(dict[str, object], value)
        source = str(row["source"])
        pulls.append(MacCheckoutPull(PurePosixPath(str(row["mac"])), Path(source) if source else MacPullSourceUnrecorded()))
    return PendingMacPull(cast(str, fields["zone"]), cast(str, fields["text"]),
                          cast(str, fields["since"]), cast(str, fields["reason"]), tuple(pulls))


def queue(change: PendingMacPull) -> None:
    """Coalesce pulls while retaining an earlier zone notice that still needs delivery."""
    previous = read_queue()
    if isinstance(previous, PendingMacPull):
        pulls = {pull.mac: pull for pull in previous.pulls}
        pulls.update((pull.mac, pull) for pull in change.pulls)
        change = replace(change, pulls=tuple(pulls.values()))
        if not change.text and previous.text:
            change = replace(change, label=previous.label, text=previous.text, since=previous.since)
    _write_queue(change)


def _write_queue(change: PendingMacPull) -> None:
    path = queue_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(path, json.dumps(dict(zone=change.label, text=change.text,
                                           since=change.since, reason=change.reason,
                                           pulls=[dict(mac=str(pull.mac), source=str(pull.source) if isinstance(pull.source, Path) else "")
                                                  for pull in change.pulls]), indent=2) + "\n")


def queue_pull_only(label: str, reason: str, *, pulls: tuple[MacCheckoutPull, ...] = _LEGACY_MAC_PULL) -> None:
    """Queue a pull and retain any waiting zone notice."""
    queue(PendingMacPull(label, "", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), reason, pulls))


def clear_pull_only(mac: PurePosixPath = DEFAULT_MAC_CHECKOUT, *, notice_delivered: bool = False) -> None:
    """Clear a completed pull and retain any waiting zone notice."""
    pending = read_queue()
    if isinstance(pending, PendingMacPull):
        pending = replace(pending, pulls=tuple(pull for pull in pending.pulls if pull.mac != mac),
                          text="" if notice_delivered else pending.text)
        if pending.pulls or pending.text:
            _write_queue(pending)
        else:
            queue_path().unlink(missing_ok=True)
