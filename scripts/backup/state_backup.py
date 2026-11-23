"""Copy the approved AWS history to the Mac and restore it into scratch."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
from collections.abc import Generator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from fnmatch import fnmatchcase
import gzip
from hashlib import file_digest, sha256
import json
from math import ceil
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
from tempfile import TemporaryDirectory
from typing import BinaryIO, Literal, NotRequired, TypedDict, cast
import zlib

from scripts.build_targets import target_config
from scripts.lib import conf, file_lock
from scripts.lib.atomic_file import sync_folder, temporary_target
from scripts.mac_test import eligibility
from scripts.message import session_send
from scripts.notify import push_texts


SECRET_PATTERNS = (
    re.compile(rb"X-Amz-Signature", re.IGNORECASE),
    re.compile(rb"X-Amz-Credential", re.IGNORECASE),
    re.compile(rb"aws_secret_access_key", re.IGNORECASE),
    re.compile(rb"SecretAccessKey", re.IGNORECASE),
    re.compile(rb"SessionToken", re.IGNORECASE),
    re.compile(rb"aws_session_token", re.IGNORECASE),
    re.compile(rb"SecurityToken", re.IGNORECASE),
    re.compile(rb"aws_security_token", re.IGNORECASE),
    re.compile(rb"AKIA[0-9A-Z]{16}"),
    re.compile(rb"ASIA[0-9A-Z]{16}"),
    re.compile(rb"-----BEGIN"),
)
# The longest pattern matches 21 bytes. Keep overlap between read blocks.
SECRET_SCAN_OVERLAP = 20
READ_BLOCK_SIZE = 1024 * 1024
SHA256 = re.compile(r"[0-9a-f]{64}")
ALERT_KEY = "state-backup:aws"
ALERT_SENDER = "state-backup"

# Check the staged archive before publication. Publish its checksum first.
MAC_RECEIVE = """\
set -eu
umask 077
case "$1" in
    '~/'*) test ! -L "$HOME" || exit 4; cd -P "$HOME"; remaining=${1#??} ;;
    /*) cd /; remaining=${1#/} ;;
    *) exit 4 ;;
esac
while test -n "$remaining"; do
    part=${remaining%%/*}
    case "$remaining" in
        */*) remaining=${remaining#*/} ;;
        *) remaining= ;;
    esac
    case "$part" in
        ''|.) continue ;;
        ..) exit 4 ;;
    esac
    test ! -L "./$part" || exit 4
    if test ! -d "./$part"; then
        mkdir -m 700 "./$part"
    fi
    test ! -L "./$part" && test -d "./$part" || exit 4
    expected="${PWD%/}/$part"
    cd -P "./$part"
    test "$PWD" = "$expected" || exit 4
done
name=$2
checksum=$3
chmod 700 .
stage=$(mktemp -d "./.incoming.XXXXXXXX")
trap 'rm -f "$stage/archive" "$stage/checksum"; rmdir "$stage"' EXIT
trap 'exit 1' HUP INT TERM
cat > "$stage/archive"
printf '%s\\n' "$checksum" > "$stage/checksum"
actual=$(shasum -a 256 "$stage/archive")
actual=${actual%% *}
printf '%s\\n' "$actual"
test "$actual" = "${checksum%% *}" || exit 3
test ! -e "./$name"
test ! -L "./$name"
test ! -e "./$name.sha256"
test ! -L "./$name.sha256"
mv -n "$stage/checksum" "./$name.sha256"
test ! -e "$stage/checksum"
mv -n "$stage/archive" "./$name"
test ! -e "$stage/archive"
"""


class BackupRefused(ValueError):
    """The backup or restore cannot proceed."""


class BackupManifest(TypedDict):
    files: dict[str, str]


class BackupDestination(TypedDict):
    host: str
    folder: str


class LastBackup(TypedDict):
    time: str
    archive: str
    manifest: BackupManifest
    # Earlier records have no destination. The next run sends a new copy.
    destination: NotRequired[BackupDestination]


class BackupHistory(Enum):
    NO_COPY = "no saved backup"


@dataclass(frozen=True)
class BackupSettings:
    source: Path
    include: tuple[tuple[str, ...], ...]
    exclude: tuple[tuple[str, ...], ...]
    mac_connection: eligibility.SshConnectionSettings
    mac_folder: str


def absolute_source(value: str) -> Path:
    source = Path(value).expanduser()
    if not source.is_absolute():
        raise BackupRefused("the source must be an absolute path or start with ~/")
    return source


def patterns(value: str) -> tuple[tuple[str, ...], ...]:
    paths = tuple(PurePosixPath(word) for word in value.split())
    if any(path.is_absolute() or not path.parts or ".." in path.parts for path in paths):
        raise BackupRefused("patterns must be relative paths without ..")
    return tuple(path.parts for path in paths)


def settings() -> BackupSettings:
    config = conf.read("state_backup")
    source = config.value("source.folder", absolute_source, recover_invalid=False)
    include = config.value("include.patterns", patterns, recover_invalid=False)
    exclude = config.value("exclude.patterns", patterns, recover_invalid=False)
    folder = config.text("mac.folder", recover_invalid=False)
    folder_parts = PurePosixPath(folder).parts
    if not folder.startswith(("/", "~/")) or len(folder_parts) < 2 or ".." in folder_parts:
        raise BackupRefused("mac.folder must name a folder under / or ~/")
    target = target_config.configured_ssh_target("mac")
    connection = eligibility.read_connection_settings(target.settings)
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", connection.host):
        raise BackupRefused(f"{target.settings}: host must be an SSH host name or alias")
    return BackupSettings(source, include, exclude, connection, folder)


def matches(path: tuple[str, ...], pattern: tuple[str, ...], *, prefix: bool = False) -> bool:
    if not path:
        return prefix or all(part == "**" for part in pattern)
    if not pattern:
        return False
    if pattern[0] == "**":
        return any(matches(path[index:], pattern[1:], prefix=prefix) for index in range(len(path) + 1))
    return fnmatchcase(path[0], pattern[0]) and matches(path[1:], pattern[1:], prefix=prefix)


def walk_failed(error: OSError) -> None:
    raise error


def stage_file(source: BinaryIO, output: BinaryIO, relative: PurePosixPath) -> str:
    digest = sha256()
    overlap = b""
    while block := source.read(READ_BLOCK_SIZE):
        content = overlap + block
        for pattern in SECRET_PATTERNS:
            if pattern.search(content):
                raise BackupRefused(f"{str(relative)!r} matches {pattern.pattern.decode('ascii')}")
        overlap = content[-SECRET_SCAN_OVERLAP:]
        digest.update(block)
        _ = output.write(block)
    return digest.hexdigest()


def snapshot(settings: BackupSettings, staged_files: Path) -> BackupManifest:
    """Check and hash the same bytes that the archive will contain."""
    manifest: BackupManifest = {"files": {}}
    source = os.open(settings.source, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    excluded_trees = tuple(pattern for pattern in settings.exclude if pattern[-1] == "**")
    try:
        for folder, subfolders, names, directory in os.fwalk(".", dir_fd=source, onerror=walk_failed):
            subfolders[:] = [
                name for name in subfolders
                if any(matches(PurePosixPath(folder, name).parts, pattern, prefix=True)
                       for pattern in settings.include)
                and not any(matches(PurePosixPath(folder, name).parts, pattern) for pattern in excluded_trees)
            ]
            for name in names:
                relative = PurePosixPath(folder, name)
                if (temporary_target(name) is not None
                        or not any(matches(relative.parts, pattern) for pattern in settings.include)
                        or any(matches(relative.parts, pattern) for pattern in settings.exclude)):
                    continue
                if not stat.S_ISREG(os.stat(name, dir_fd=directory, follow_symlinks=False).st_mode):
                    continue
                descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                with os.fdopen(descriptor, "rb") as file:
                    if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
                        raise BackupRefused(f"{str(relative)!r} changed file type during the copy")
                    staged = staged_files / relative
                    staged.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    with private_file(staged) as output:
                        manifest["files"][relative.as_posix()] = stage_file(file, output, relative)
    finally:
        os.close(source)
    return manifest


@contextmanager
def private_file(path: Path) -> Generator[BinaryIO]:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as file:
        yield file


def read_last(path: Path) -> LastBackup | Literal[BackupHistory.NO_COPY]:
    try:
        raw = cast(object, json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return BackupHistory.NO_COPY
    if isinstance(raw, dict):
        record = cast(dict[object, object], raw)
        saved_at, archive, manifest = record.get("time"), record.get("archive"), record.get("manifest")
        if isinstance(saved_at, str) and isinstance(archive, str) and isinstance(manifest, dict):
            files = cast(dict[object, object], manifest).get("files")
            if isinstance(files, dict):
                file_hashes: dict[str, str] = {}
                for name, digest in cast(dict[object, object], files).items():
                    if not isinstance(name, str) or not isinstance(digest, str) or not SHA256.fullmatch(digest):
                        break
                    file_hashes[name] = digest
                else:
                    last: LastBackup = {"time": saved_at, "archive": archive, "manifest": {"files": file_hashes}}
                    if "destination" in record:
                        last["destination"] = read_saved_destination(record["destination"], path)
                    return last
    raise BackupRefused(f"{path}: invalid saved backup")


def read_saved_destination(raw: object, path: Path) -> BackupDestination:
    if isinstance(raw, dict):
        fields = cast(dict[object, object], raw)
        host, folder = fields.get("host"), fields.get("folder")
        if isinstance(host, str) and isinstance(folder, str):
            return {"host": host, "folder": folder}
    raise BackupRefused(f"{path}: invalid saved destination")


def checksum_path(archive: Path) -> Path:
    return archive.with_name(archive.name + ".sha256")


def receive_on_mac(settings: BackupSettings, archive: Path, digest: str) -> None:
    checksum = checksum_path(archive).read_text(encoding="ascii").rstrip("\n")
    command = shlex.join(["sh", "-c", MAC_RECEIVE, "state-backup", settings.mac_folder, archive.name, checksum])
    connection = settings.mac_connection
    connect_timeout = ceil(connection.connect_timeout_s)
    with archive.open("rb") as data:
        result = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={connect_timeout}", "--", connection.host, command],
            stdin=data, capture_output=True, timeout=300, check=False,
        )
    if result.returncode == 3 or (result.returncode == 0 and result.stdout.strip() != digest.encode("ascii")):
        raise BackupRefused("the Mac SHA-256 does not match the archive")
    if result.returncode == 4:
        raise BackupRefused("the Mac backup path has a symlink or changed during setup")
    if result.returncode:
        raise BackupRefused(f"SSH to {connection.host} failed with status {result.returncode}")


def save_backup(settings: BackupSettings, state: Path) -> None:
    last_path = state / "last.json"
    last = read_last(last_path)
    destination: BackupDestination = {"host": settings.mac_connection.host, "folder": settings.mac_folder}
    with TemporaryDirectory(prefix="backup-", dir=state) as temporary:
        stage = Path(temporary)
        staged_files = stage / "files"
        manifest = snapshot(settings, staged_files)
        if (not isinstance(last, BackupHistory) and last["manifest"] == manifest
                and last.get("destination") == destination):
            print("unchanged")
            return
        now = datetime.now(UTC)
        archive = stage / f"aws-state-{now:%Y%m%dT%H%M%S%fZ}.tar.gz"
        with private_file(archive) as data, tarfile.open(fileobj=data, mode="w:gz") as bundle:
            for name in sorted(manifest["files"]):
                bundle.add(staged_files / name, arcname=name, recursive=False)
        with archive.open("rb") as source:
            digest = file_digest(source, "sha256").hexdigest()
        with private_file(checksum_path(archive)) as checksum:
            _ = checksum.write(f"{digest}  {archive.name}\n".encode("ascii"))
        receive_on_mac(settings, archive, digest)
        record: LastBackup = {"time": now.isoformat(), "archive": archive.name, "manifest": manifest,
                              "destination": destination}
        staged_last = stage / last_path.name
        with private_file(staged_last) as output:
            _ = output.write((json.dumps(record, sort_keys=True) + "\n").encode("utf-8"))
            output.flush()
            os.fsync(output.fileno())
        os.replace(staged_last, last_path)
        sync_folder(state)
        print(f"{archive.name}: {len(manifest['files'])} archived")


def notify(command: list[str]) -> bool:
    try:
        result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, check=False)
        if result.returncode == 0:
            return True
        print(f"state_backup: send.py exited {result.returncode}", file=sys.stderr)
    except OSError:
        print("state_backup: cannot run send.py", file=sys.stderr)
    return False


def record_pending_alert(path: Path) -> None:
    try:
        with private_file(path):
            pass
    except FileExistsError:
        if path.is_symlink() or not path.is_file():
            raise
    sync_folder(path.parent)


def run(state: Path) -> int:
    send_script = Path.home() / ".claude/scripts/message/send.py"
    alert = state / "alert-pending"
    with ExitStack() as stack:
        try:
            state.mkdir(mode=0o700, parents=True, exist_ok=True)
            _ = stack.enter_context(file_lock.exclusive(state / "run.lock"))
            save_backup(settings(), state)
        except (OSError, ValueError, RuntimeError, tarfile.TarError, subprocess.TimeoutExpired) as error:
            reason = "SSH transfer timed out" if isinstance(error, subprocess.TimeoutExpired) else str(error)
            print(f"state_backup: {reason}", file=sys.stderr)
            try:
                record_pending_alert(alert)
            except OSError:
                print("state_backup: cannot record the pending alert", file=sys.stderr)
                reason += "\nA later successful run cannot retire this alert."
            push = push_texts.render("alert.blocked", sender=ALERT_SENDER, title="AWS state backup failed",
                                     action="Check the state backup service log on natedev.", body=reason)
            _ = notify(push_texts.user_send_argv(push, key=ALERT_KEY, self_clearing=True, send_script=send_script))
            return 1
        if not alert.is_symlink() and alert.is_file() and notify(session_send.send_argv(
                "retire", "--to", "user", "--from", ALERT_SENDER, "--key", ALERT_KEY, send_script=send_script)):
            alert.unlink(missing_ok=True)
        return 0


def restore(archive: Path, destination: Path, source: Path) -> None:
    if destination.is_symlink():
        raise BackupRefused("the restore folder is a symlink")
    destination = destination.resolve()
    source = source.resolve()
    if destination.is_relative_to(source) or source.is_relative_to(destination):
        raise BackupRefused("the restore folder overlaps the backup source")
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise BackupRefused("the restore folder must be empty or absent")
    checksum = checksum_path(archive).read_text(encoding="ascii").rstrip("\n")
    digest, separator, name = checksum.partition("  ")
    if not separator or name != archive.name or not SHA256.fullmatch(digest):
        raise BackupRefused("the SHA-256 file is invalid")
    with archive.open("rb") as data:
        if file_digest(data, "sha256").hexdigest() != digest:
            raise BackupRefused("the archive SHA-256 does not match")
        _ = data.seek(0)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix=".restore-", dir=destination.parent) as temporary:
            stage = Path(temporary) / "files"
            stage.mkdir(mode=0o700)
            try:
                with gzip.GzipFile(fileobj=data) as decoded:
                    with tarfile.open(fileobj=decoded, mode="r:") as bundle:
                        extract_members(bundle, stage)
                    # Read the gzip footer before publishing the restored files.
                    while decoded.read(READ_BLOCK_SIZE):
                        pass
            except (EOFError, gzip.BadGzipFile, zlib.error, tarfile.ReadError) as error:
                raise BackupRefused("the archive has invalid or incomplete gzip or tar data") from error
            os.rename(stage, destination)


def extract_members(bundle: tarfile.TarFile, stage: Path) -> None:
    for member in bundle:
        path = PurePosixPath(member.name)
        if (path.is_absolute() or not path.parts or ".." in path.parts
                or member.type not in (tarfile.REGTYPE, tarfile.AREGTYPE, tarfile.DIRTYPE)):
            raise BackupRefused(f"unsafe archive member {member.name!r}")
        output = stage / path
        if member.isdir():
            output.mkdir(mode=0o700, parents=True, exist_ok=True)
            continue
        output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        content = bundle.extractfile(member)
        if content is None:
            raise BackupRefused(f"cannot read archive member {member.name!r}")
        with content, private_file(output) as restored:
            shutil.copyfileobj(content, restored)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    _ = commands.add_parser("run")
    _ = commands.add_parser("status")
    restore_args = commands.add_parser("restore")
    _ = restore_args.add_argument("archive", type=Path)
    _ = restore_args.add_argument("--into", type=Path, required=True)
    args = parser.parse_args(argv)
    command = cast(str, args.command)
    state = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "state-backup"
    try:
        if command == "run":
            return run(state)
        if command == "restore":
            source = conf.read("state_backup").value("source.folder", absolute_source, recover_invalid=False)
            restore(cast(Path, args.archive), cast(Path, args.into), source)
        else:
            last = read_last(state / "last.json")
            if isinstance(last, BackupHistory):
                print(last.value)
            else:
                print(f"{last['time']} {last['archive']} ({len(last['manifest']['files'])} archived)")
    except (OSError, ValueError, RuntimeError, tarfile.TarError) as error:
        print(f"state_backup: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
