"""Commit a main-checkout change, push it, and send or queue the Mac pull."""
from __future__ import annotations

from collections.abc import Callable
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

from scripts.lib import land_main, main_push_lock, waiting_rows
from scripts.lib.land_main import CommandTimeLimit as CommandTimeLimit, NoTimeLimit as NoTimeLimit, UNLIMITED_GIT
from scripts.lib.atomic_file import prepared_replacement, sync_folder, write_text_atomic


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


@dataclass(frozen=True)
class ProductionMacPull:
    """Install an exact commit published to production main."""

    mac: PurePosixPath
    source: Path
    sha: str


@dataclass(frozen=True)
class MacPullAwaitingPublication:
    """An older pull lacks proof of the commit or the settings route."""

    mac: PurePosixPath
    source: Path


type QueuedMacPull = MacCheckoutPull | ProductionMacPull | MacPullAwaitingPublication


DEFAULT_MAC_CHECKOUT = PurePosixPath("~/.claude")
_LEGACY_MAC_PULL = (MacCheckoutPull(DEFAULT_MAC_CHECKOUT, MacPullSourceUnrecorded()),)


@dataclass(frozen=True)
class PendingMacPull:
    label: str
    text: str
    since: str
    reason: str
    pulls: tuple[QueuedMacPull, ...] = _LEGACY_MAC_PULL


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


ZONE_SSH_LIMIT = CommandTimeLimit(120)
PUBLICATION_PREFIXES = ("tune(", "zone(", "user zone: ")
REGISTRY_SUBJECT = "config(models): "
# A landing merges a main move made only of these commits, on config/ paths only, with no retest.
SETTINGS_SUBJECTS = (*PUBLICATION_PREFIXES, REGISTRY_SUBJECT)


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
        head = waiting_rows.git(checkout, "rev-parse", "HEAD").strip()
        listed = git(checkout, "log", "--format=%H %h %s", "--reverse", f"origin/main..{head}")
        if listed.returncode:
            raise Refusal(f"cannot inspect unpublished commits: {first_line(listed)}")
        commits = [cast(tuple[str, str, str], tuple(line.split(" ", 2))) for line in listed.stdout.splitlines()]
        for _, short, subject in commits:
            if not subject.startswith(PUBLICATION_PREFIXES):
                raise Refusal(f"main holds other commit {short} {subject} that origin does not have; main is unchanged")
        if not git(checkout, "merge-base", "--is-ancestor", "origin/main", head).returncode:
            return
        target = _rebuilt(checkout, commits)
        try:
            waiting_rows.move_to(checkout, target, fast_forward=not commits, expected_head=head)
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
        others = [subject for subject in unpushed if not subject.startswith(PUBLICATION_PREFIXES)]
        if others:
            return False, f"not pushed: main holds {len(others)} other commit(s) origin does not have"
        try:
            landed = land_main.land(checkout, "main", time_limit=time_limit)
        except (land_main.LandError, OSError) as error:
            return False, f"not pushed: {error}"
        detail = "pushed" if isinstance(landed.push, land_main.Pushed) else "origin has it"
        if isinstance(landed.hook, (land_main.HookDone, land_main.HookFailed)):
            detail += "; " + land_main.hook_report(landed.hook)
        return True, detail


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
    return (f"cd {remote_path(mac)} "
            "&& { [ ! -f scripts/lib/waiting_rows.py ] || scripts/lib/py scripts/lib/waiting_rows.py fast-forward HEAD; } "
            f"&& tip=$(git ls-remote {url} refs/heads/main | cut -f1) "
            f"&& git fetch -q --no-write-fetch-head {url} main "
            "&& if [ -f scripts/lib/waiting_rows.py ]; "
            "then scripts/lib/py scripts/lib/waiting_rows.py fast-forward \"$tip\"; "
            "else git merge -q --ff-only \"$tip\"; fi; printf 'rc=%s\\n' \"$?\"")


def production_pull_command(pull: ProductionMacPull) -> str:
    url = f"natedev:{shlex.quote(str(pull.source))}"
    sha = shlex.quote(pull.sha)
    return (f"cd {remote_path(pull.mac)} "
            f"&& {{ git fetch -q --no-write-fetch-head {url} {sha} "
            f"|| git fetch -q --no-write-fetch-head {url} main; }} "
            f"&& if git merge-base --is-ancestor {sha} HEAD; then :; "
            "elif [ -f scripts/lib/waiting_rows.py ]; then "
            f"scripts/lib/py scripts/lib/waiting_rows.py fast-forward {sha}; "
            f"else git merge -q --ff-only {sha}; fi; printf 'rc=%s\\n' \"$?\"")


def pull_mac(checkout: Path, *, ssh_program: str, mac: PurePosixPath = DEFAULT_MAC_CHECKOUT,
             time_limit: CommandTimeLimit = ZONE_SSH_LIMIT) -> MacPulled | MacPullFailed:
    """Return the completed Mac pull or its failure reason."""
    return run_mac_pull(mac_pull_command(mac, checkout), ssh_program=ssh_program, time_limit=time_limit)


def run_mac_pull(command: str, *, ssh_program: str,
                 time_limit: CommandTimeLimit = ZONE_SSH_LIMIT) -> MacPulled | MacPullFailed:
    done = ssh(command, ssh_program=ssh_program, time_limit=time_limit)
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


def queue_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return b""


def read_queue() -> PendingMacPull | NoPendingMacPull:
    try:
        return parse_queue(queue_bytes(queue_path()))
    except OSError:
        return NoPendingMacPull()


def parse_queue(contents: bytes) -> PendingMacPull | NoPendingMacPull:
    try:
        value = cast(object, json.loads(contents))
    except ValueError:
        return NoPendingMacPull()
    if not isinstance(value, dict):
        return NoPendingMacPull()
    fields = cast(dict[str, object], value)
    if not all(isinstance(fields.get(key), str) for key in ("zone", "text", "since", "reason")):
        return NoPendingMacPull()
    label = cast(str, fields["zone"])
    notice = cast(str, fields["text"])
    words = label.split()
    old_settings_label = label == "main" or (bool(words) and all("." in word for word in words))
    rows = fields.get("pulls", [{"mac": str(DEFAULT_MAC_CHECKOUT), "source": ""}])
    if not isinstance(rows, list):
        return NoPendingMacPull()
    pulls: list[QueuedMacPull] = []
    for raw in cast(list[object], rows):
        if not isinstance(raw, dict):
            return NoPendingMacPull()
        row = cast(dict[str, object], raw)
        mac, source = row.get("mac"), row.get("source")
        if not isinstance(mac, str) or not mac or not isinstance(source, str):
            return NoPendingMacPull()
        mac_path = PurePosixPath(mac)
        if "sha" in row:
            sha = row["sha"]
            if not source or not isinstance(sha, str) or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", sha):
                return NoPendingMacPull()
            pulls.append(ProductionMacPull(mac_path, Path(source), sha))
        elif not source:
            if row.get("kind") == "production":
                return NoPendingMacPull()
            pulls.append(MacCheckoutPull(mac_path, MacPullSourceUnrecorded()))
        elif row.get("kind") == "settings" or (
                "kind" not in row and mac_path == DEFAULT_MAC_CHECKOUT and old_settings_label and not notice):
            pulls.append(MacCheckoutPull(mac_path, Path(source)))
        else:
            pulls.append(MacPullAwaitingPublication(mac_path, Path(source)))
    return PendingMacPull(label, notice,
                          cast(str, fields["since"]), cast(str, fields["reason"]), tuple(pulls))


def pull_fields(pull: QueuedMacPull) -> dict[str, str]:
    fields = {"mac": str(pull.mac), "source": str(pull.source) if isinstance(pull.source, Path) else "",
              "kind": "settings" if isinstance(pull, MacCheckoutPull) else "production"}
    if isinstance(pull, ProductionMacPull):
        fields["sha"] = pull.sha
    return fields


def update_queue(change: Callable[[PendingMacPull | NoPendingMacPull], PendingMacPull | NoPendingMacPull]) -> None:
    path = queue_path()
    for _ in range(3):
        before = queue_bytes(path)
        previous = parse_queue(before)
        pending = change(previous)
        if pending == previous:
            return
        if isinstance(pending, PendingMacPull) and (pending.pulls or pending.text):
            contents = json.dumps(dict(zone=pending.label, text=pending.text, since=pending.since,
                                       reason=pending.reason, pulls=[pull_fields(pull) for pull in pending.pulls]), indent=2)
            with prepared_replacement(path, contents + "\n", mode=0o600) as prepared:
                if queue_bytes(path) == before:
                    _ = prepared.replace(path)
                    sync_folder(path.parent)
                    return
        elif queue_bytes(path) == before:
            path.unlink(missing_ok=True)
            return
    raise Refusal(f"{path} changed during the Mac queue update")


def queue(change: PendingMacPull) -> None:
    """Keep the newest pull for each purpose and retain the waiting zone notice."""
    def key(pull: QueuedMacPull) -> tuple[PurePosixPath, bool]:
        return pull.mac, isinstance(pull, MacCheckoutPull)

    def merge(previous: PendingMacPull | NoPendingMacPull) -> PendingMacPull:
        if isinstance(previous, NoPendingMacPull):
            return change
        pulls = {key(pull): pull for pull in previous.pulls}
        pulls.update((key(pull), pull) for pull in change.pulls)
        if not change.text and previous.text:
            return replace(previous, reason=change.reason, pulls=tuple(pulls.values()))
        return replace(change, pulls=tuple(pulls.values()))

    update_queue(merge)


def queue_pull_only(label: str, reason: str, *, pulls: tuple[QueuedMacPull, ...] = _LEGACY_MAC_PULL) -> None:
    """Queue a pull and retain any waiting zone notice."""
    queue(PendingMacPull(label, "", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), reason, pulls))


def clear_mac_pull(pull: QueuedMacPull) -> None:
    """Clear the completed pull if it is still queued."""
    def clear(pending: PendingMacPull | NoPendingMacPull) -> PendingMacPull | NoPendingMacPull:
        if isinstance(pending, NoPendingMacPull):
            return pending
        def completed(queued: QueuedMacPull) -> bool:
            return queued == pull or (isinstance(pull, ProductionMacPull) and isinstance(queued, MacPullAwaitingPublication)
                                      and queued.mac == pull.mac and queued.source == pull.source)

        return replace(pending, pulls=tuple(queued for queued in pending.pulls if not completed(queued)))

    update_queue(clear)


def clear_pull_only(mac: PurePosixPath = DEFAULT_MAC_CHECKOUT, *, notice_delivered: bool = False) -> None:
    """Clear the settings pull and retain any waiting production pull."""
    def clear(pending: PendingMacPull | NoPendingMacPull) -> PendingMacPull | NoPendingMacPull:
        if isinstance(pending, NoPendingMacPull):
            return pending
        pulls = tuple(pull for pull in pending.pulls if not isinstance(pull, MacCheckoutPull) or pull.mac != mac)
        return replace(pending, pulls=pulls, text="" if notice_delivered else pending.text)

    update_queue(clear)


def clear_zone_notice(delivered: PendingMacPull) -> None:
    """Clear the delivered zone notice if it is still queued."""
    def clear(pending: PendingMacPull | NoPendingMacPull) -> PendingMacPull | NoPendingMacPull:
        if (isinstance(pending, PendingMacPull)
                and (pending.label, pending.text, pending.since) == (delivered.label, delivered.text, delivered.since)):
            return replace(pending, text="")
        return pending

    update_queue(clear)
