"""Commit vault text and publish the reviewed first save."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from scripts.lib import land_main
from scripts.lib.atomic_file import write_bytes_atomic
from scripts.lib.main_push_lock import main_push_lock
from scripts.maintenance.vault_commit import git_bytes


TEXT_SUFFIXES = frozenset(".base .html .js .json .jsonl .md .py .rs .sh .toml .txt .yaml".split())
TEXT_NAMES = frozenset((".gitignore", ".gitattributes", "Cargo.lock"))
NOTE_BACKUP = re.compile(r".*[.]md[.]backup-[0-9]{8}T[0-9]{12}", re.DOTALL)
OBJECT_ID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
MEDIA_SUFFIXES = (
    "avif bmp gif heic heif ico jpeg jpg png svg tif tiff webp "
    "aac aiff flac m4a m4v mkv mov mp3 mp4 ogg opus wav webm"
).split()
EXISTING_PRODUCERS = ("bob", "brain", "dwight", "morty")
OPERATIONS = ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply", "sequencer",
              "BISECT_START")
ORIGIN_MAIN = "refs/remotes/origin/main"
SUBJECT = "vault: Save text files"
SCAN_FINDINGS_EXIT = 10


class SaveError(ValueError):
    pass


def install_producer_media_rules(folder: Path) -> None:
    path = folder / ".gitignore"
    if path.is_symlink():
        raise SaveError(f"{path}: the ignore file is a symlink")
    rules = ("# Ignore producer images and recordings.\n" + "".join(
        "*." + "".join(f"[{letter}{letter.upper()}]" for letter in suffix) + "\n"
        for suffix in MEDIA_SUFFIXES
    )).encode("ascii")
    try:
        current = path.read_bytes()
    except FileNotFoundError:
        current = b""
    if not current.endswith(rules):
        separator = b"\n" if current and not current.endswith(b"\n") else b""
        write_bytes_atomic(path, current + separator + rules)


def git(vault: Path, *args: str, input_data: bytes | None = None,
        env: dict[str, str] | None = None) -> subprocess.CompletedProcess[bytes]:
    environment = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}
    _ = environment.pop("GIT_INDEX_FILE", None)
    if env is not None:
        environment.update(env)
    return git_bytes(str(vault), *args, input_data=input_data, env=environment)


def checked_bytes(vault: Path, *args: str, input_data: bytes | None = None, env: dict[str, str] | None = None) -> bytes:
    result = git(vault, *args, input_data=input_data, env=env)
    if result.returncode:
        detail = os.fsdecode(result.stderr or result.stdout).strip()
        raise SaveError(f"git {args[0]} failed" + (f": {detail}" if detail else ""))
    return result.stdout


def checked(vault: Path, *args: str, env: dict[str, str] | None = None) -> str:
    return os.fsdecode(checked_bytes(vault, *args, env=env))


def git_paths(vault: Path, *args: str) -> list[str]:
    return [os.fsdecode(name) for name in checked_bytes(vault, *args).split(b"\0")[:-1]]


def git_path(vault: Path, name: str) -> Path:
    return Path(checked(vault, "rev-parse", "--path-format=absolute", "--git-path", name).strip())


def writable(vault: Path) -> bool:
    branch = checked(vault, "branch", "--show-current").strip()
    git_dir = Path(checked(vault, "rev-parse", "--absolute-git-dir").strip())
    return branch == "main" and not any((git_dir / name).exists() for name in OPERATIONS)


def staged_paths(vault: Path, *, ignore_intent: bool = True) -> list[str]:
    intent = "--ita-invisible-in-index" if ignore_intent else "--ita-visible-in-index"
    return git_paths(vault, "diff", "--cached", intent, "--no-renames", "--ignore-submodules=none",
                     "--name-only", "-z", "HEAD")


def text_path(vault: Path, name: str, *, tracked: bool) -> bool:
    path = Path(name)
    if ".obsidian" in path.parts:
        return False
    if not (path.suffix.lower() in TEXT_SUFFIXES or path.name in TEXT_NAMES or NOTE_BACKUP.fullmatch(path.name)):
        return False
    try:
        return stat.S_ISREG((vault / path).lstat().st_mode)
    except (FileNotFoundError, NotADirectoryError):
        return tracked


def selected_paths(vault: Path) -> tuple[list[str], list[str]]:
    changed = git_paths(vault, "diff", "--name-only", "--no-renames", "-z", "HEAD")
    new = git_paths(vault, "ls-files", "--others", "--exclude-standard", "-z")
    return ([name for name in changed if text_path(vault, name, tracked=True)],
            [name for name in new if text_path(vault, name, tracked=False)])


def with_paths(vault: Path, paths: list[str], *args: str, env: dict[str, str] | None = None) -> None:
    _ = checked_bytes(vault, *args, "--pathspec-from-file=-", "--pathspec-file-nul",
                      input_data=b"\0".join(map(os.fsencode, paths)) + b"\0", env=env)


def save(vault: Path) -> int:
    with main_push_lock(vault, "vault-save", wait=False):
        if not writable(vault):
            print("Vault save deferred while main is unavailable.")
            return 0
        if staged_paths(vault):
            print("Vault save deferred because the index has staged changes.")
            return 0
        changed, new = selected_paths(vault)
        paths = changed + new
        if paths:
            if new:
                with_paths(vault, new, "add", "--intent-to-add")
            with_paths(vault, paths, "commit", "--only", "-m", SUBJECT)
            print(f"Saved {len(paths)} paths in {checked(vault, 'rev-parse', 'HEAD').strip()}.")
        else:
            print("The vault has no text changes.")
    return 0


@dataclass(frozen=True)
class ReviewedPath:
    name: str
    mode: str
    blob: str
    source_hash: str


@dataclass(frozen=True)
class Review:
    base: str
    head: str
    files: tuple[ReviewedPath, ...]

    @classmethod
    def read(cls, path: Path, expected_hash: str) -> Review:
        try:
            data = path.read_bytes()
        except FileNotFoundError as error:
            raise SaveError("Run first-save scan and review its output first.") from error
        if hashlib.sha256(data).hexdigest() != expected_hash:
            raise SaveError("The manifest hash does not match the scanned manifest.")
        record = cast(dict[str, object], json.loads(data))
        base, head = record["base"], record["head"]
        if not (isinstance(base, str) and OBJECT_ID.fullmatch(base)
                and isinstance(head, str) and OBJECT_ID.fullmatch(head)):
            raise SaveError("The manifest contains invalid commit hashes.")
        files = tuple(
            ReviewedPath(entry["path"], entry["mode"], entry["blob"], entry["source_sha256"])
            for entry in cast(list[dict[str, str]], record["files"])
        )
        return cls(base, head, files)

    def write(self, path: Path) -> str:
        files = [{"path": entry.name, "mode": entry.mode, "blob": entry.blob, "source_sha256": entry.source_hash}
                 for entry in self.files]
        data = (json.dumps({"base": self.base, "head": self.head, "files": files}, indent=2) + "\n").encode()
        write_bytes_atomic(path, data, mode=0o600)
        return hashlib.sha256(data).hexdigest()

    def staged_blobs(self) -> dict[str, tuple[str, str]]:
        return {entry.name: (entry.mode, entry.blob) for entry in self.files}


def diff_blobs(vault: Path, *args: str, env: dict[str, str] | None = None) -> dict[str, tuple[str, str]]:
    data = checked_bytes(vault, "diff", "--raw", "--no-abbrev", "--no-renames", "--ignore-submodules=none",
                         "-z", *args, env=env)
    parts = data.split(b"\0")[:-1]
    blobs: dict[str, tuple[str, str]] = {}
    for header, name in zip(parts[::2], parts[1::2], strict=True):
        _, mode, _, blob, _ = header.split()
        blobs[os.fsdecode(name)] = (mode.decode("ascii"), blob.decode("ascii"))
    return blobs


def temporary_git_environment(vault: Path, folder: Path) -> dict[str, str]:
    objects = folder / "objects"
    objects.mkdir()
    alternate = str(git_path(vault, "objects")).replace("\\", "\\\\").replace('"', '\\"')
    return {
        "GIT_INDEX_FILE": str(folder / "index"),
        "GIT_OBJECT_DIRECTORY": str(objects),
        "GIT_ALTERNATE_OBJECT_DIRECTORIES": f'"{alternate}"',
    }


@dataclass(frozen=True)
class SourceFile:
    mode: int
    data: bytes


def read_source(vault: Path, name: str) -> SourceFile | None:
    path = vault / name
    try:
        mode = path.lstat().st_mode
        if not stat.S_ISREG(mode):
            raise SaveError(f"{name!r} is no longer a regular file.")
        return SourceFile(mode, path.read_bytes())
    except (FileNotFoundError, NotADirectoryError):
        return None


def source_hash(data: bytes | None) -> str:
    return hashlib.sha256(data).hexdigest() if data is not None else ""


def copy_snapshot(vault: Path, folder: Path, blobs: dict[str, tuple[str, str]],
                  env: dict[str, str]) -> tuple[ReviewedPath, ...]:
    entries: list[ReviewedPath] = []
    folder.mkdir()
    for name, (mode, blob) in sorted(blobs.items()):
        file = read_source(vault, name)
        data = file.data if file is not None else None
        if mode == "000000":
            if data is not None:
                raise SaveError(f"{name!r} reappeared during the scan. Scan again.")
        elif mode in ("100644", "100755") and data is not None:
            source = folder / "files" / name
            source.parent.mkdir(parents=True, exist_ok=True)
            _ = source.write_bytes(data)
            raw_blob = f"blob {len(data)}\0".encode() + data
            algorithm = "sha256" if len(blob) == 64 else "sha1"
            if hashlib.new(algorithm, raw_blob).hexdigest() != blob:
                staged = folder / "blobs" / name
                staged.parent.mkdir(parents=True, exist_ok=True)
                _ = staged.write_bytes(checked_bytes(vault, "cat-file", "blob", blob, env=env))
        else:
            raise SaveError(f"{name!r} changed type during the scan. Scan again.")
        entries.append(ReviewedPath(name, mode, blob, source_hash(data)))
    return tuple(entries)


def ancestor(vault: Path, older: str, newer: str) -> bool:
    result = git(vault, "merge-base", "--is-ancestor", older, newer)
    if result.returncode not in (0, 1):
        raise SaveError("Git could not compare the first-save commits.")
    return result.returncode == 0


def fetch_main(vault: Path) -> str:
    _ = checked(vault, "fetch", "origin", f"refs/heads/main:{ORIGIN_MAIN}")
    return checked(vault, "rev-parse", ORIGIN_MAIN).strip()


def scan(vault: Path, *args: str) -> None:
    result = subprocess.run(
        ["nix", "shell", "nixpkgs#gitleaks", "--command", "gitleaks", *args,
         "--redact=100", "--exit-code", str(SCAN_FINDINGS_EXIT)],
        cwd=vault, check=False,
    )
    if result.returncode == SCAN_FINDINGS_EXIT:
        raise SaveError("Gitleaks found a potential secret. Tell sdlc the redacted finding.")
    if result.returncode:
        raise SaveError(f"Gitleaks failed with exit {result.returncode}.")


def scan_first_save(vault: Path, manifest: Path) -> None:
    if staged_paths(vault, ignore_intent=False):
        raise SaveError("The index has staged changes. Clear them before scanning the first save.")
    manifest.unlink(missing_ok=True)
    manifest.with_suffix(".state").unlink(missing_ok=True)
    base = fetch_main(vault)
    head = checked(vault, "rev-parse", "HEAD").strip()
    if not ancestor(vault, base, head):
        raise SaveError("Reconcile local main with origin/main before scanning the first save.")
    for name in EXISTING_PRODUCERS:
        folder = vault / name
        if folder.is_dir() and not folder.is_symlink():
            install_producer_media_rules(folder)
    changed, new = selected_paths(vault)
    paths = sorted(set(changed + new))
    if not paths:
        raise SaveError("The vault has no text changes to scan.")
    with tempfile.TemporaryDirectory(prefix="vault-save-") as temporary:
        folder = Path(temporary)
        env = temporary_git_environment(vault, folder)
        _ = checked(vault, "read-tree", head, env=env)
        with_paths(vault, paths, "add", "-A", env=env)
        blobs = diff_blobs(vault, "--cached", head, env=env)
        if set(blobs) != set(paths):
            raise SaveError("The selected paths changed during staging. Scan again.")
        snapshot = folder / "snapshot"
        files = copy_snapshot(vault, snapshot, blobs, env)
        summary = checked(vault, "diff", "--cached", "--stat", head, env=env).rstrip()
        scan(snapshot, "dir", ".")
        scan(vault, "git", f"--log-opts=--diff-merges=first-parent {base}..{head}", ".")
    if head != checked(vault, "rev-parse", "HEAD").strip():
        raise SaveError("Local main changed during the scan. Scan again.")
    digest = Review(base, head, files).write(manifest)
    print(f"Scanned {len(files)} paths.\n{summary}\nManifest {manifest}\nManifest hash {digest}")


def index_modes(vault: Path, env: dict[str, str]) -> dict[str, str]:
    modes: dict[str, str] = {}
    for entry in checked_bytes(vault, "ls-files", "--stage", "-z", env=env).split(b"\0")[:-1]:
        metadata, name = entry.split(b"\t", 1)
        modes[os.fsdecode(name)] = metadata.split()[0].decode("ascii")
    return modes


def check_blob(vault: Path, entry: ReviewedPath, data: bytes, env: dict[str, str], *, write: bool = False) -> None:
    options = ("-w",) if write else ()
    blob = checked_bytes(vault, "hash-object", *options, "--stdin", f"--path={entry.name}", input_data=data, env=env)
    if blob.strip().decode("ascii") != entry.blob:
        raise SaveError(f"The filtered blob for {entry.name!r} differs from the manifest. Scan again.")


def verify_sources(vault: Path, review: Review, env: dict[str, str]) -> list[tuple[ReviewedPath, bytes]]:
    trust_mode = checked(vault, "config", "--type=bool", "--default=true", "--get", "core.filemode").strip() == "true"
    modes = {} if trust_mode else index_modes(vault, env)
    sources: list[tuple[ReviewedPath, bytes]] = []
    for entry in review.files:
        file = read_source(vault, entry.name)
        if source_hash(file.data if file is not None else None) != entry.source_hash:
            raise SaveError(f"{entry.name!r} changed after the scan. Scan again.")
        mode = "000000"
        if file is not None:
            if trust_mode:
                mode = "100755" if file.mode & stat.S_IXUSR else "100644"
            else:
                mode = modes.get(entry.name, "100644")
        if mode != entry.mode or (file is None and entry.blob != "0" * len(review.head)):
            raise SaveError(f"The mode or deletion for {entry.name!r} differs from the manifest. Scan again.")
        if file is not None:
            check_blob(vault, entry, file.data, env)
            sources.append((entry, file.data))
    return sources


def update_index(vault: Path, review: Review, *, env: dict[str, str] | None = None) -> None:
    entries = b"".join(
        f"{entry.mode} {entry.blob}\t".encode("ascii") + os.fsencode(entry.name) + b"\0"
        for entry in review.files
    )
    _ = checked_bytes(vault, "update-index", "-z", "--index-info", input_data=entries, env=env)


def create_reviewed_commit(vault: Path, review: Review) -> str:
    with tempfile.TemporaryDirectory(prefix="vault-save-") as temporary:
        env = temporary_git_environment(vault, Path(temporary))
        _ = checked(vault, "read-tree", review.head, env=env)
        sources = verify_sources(vault, review, env)
        for entry, data in sources:
            check_blob(vault, entry, data, env, write=True)
        if sources:
            # Import only the verified blobs. Filters can change between the two passes.
            hashes = "".join(f"{blob}\n" for blob in sorted({entry.blob for entry, _ in sources}))
            pack = checked_bytes(vault, "pack-objects", "--stdout", input_data=hashes.encode("ascii"), env=env)
            _ = checked_bytes(vault, "unpack-objects", input_data=pack)
        index_env = {"GIT_INDEX_FILE": env["GIT_INDEX_FILE"]}
        update_index(vault, review, env=index_env)
        if diff_blobs(vault, "--cached", review.head, env=index_env) != review.staged_blobs():
            raise SaveError("The temporary index differs from the manifest. Scan again.")
        tree = checked(vault, "write-tree", env=index_env).strip()
    return checked(vault, "commit-tree", tree, "-p", review.head, "-m", SUBJECT).strip()


def read_commit_state(state: Path, manifest_hash: str) -> str | None:
    try:
        fields = state.read_text(encoding="ascii").split()
    except FileNotFoundError:
        return None
    if len(fields) != 2 or fields[0] != manifest_hash or not OBJECT_ID.fullmatch(fields[1]):
        raise SaveError("The first-save state does not match the manifest.")
    return fields[1]


def recover_reviewed_commit(vault: Path, review: Review, head: str, recorded: str | None) -> str:
    if recorded is None:
        # A stop after update-ref can leave main updated before the state file exists.
        revisions = checked(vault, "log", "--no-show-signature", "--no-decorate",
                            "--format=%H %P", "--ancestry-path", f"{review.head}..{head}")
    elif head == review.head or ancestor(vault, recorded, head):
        revisions = checked(vault, "rev-list", "--parents", "-n", "1", recorded)
    else:
        raise SaveError("Local main does not contain the recorded first-save commit.")

    reviewed = review.staged_blobs()
    for line in revisions.splitlines():
        commit, *parents = line.split()
        if parents == [review.head] and diff_blobs(vault, review.head, commit) == reviewed:
            if head != review.head:
                for name in diff_blobs(vault, commit, head):
                    if name in reviewed or any(str(parent) in reviewed for parent in Path(name).parents):
                        raise SaveError(f"{name!r} changed in local main after the first-save commit. Scan again.")
            return commit
    raise SaveError("Local main does not contain the reviewed commit. Scan again.")


def commit_first_save(vault: Path, manifest: Path, manifest_hash: str) -> None:
    review = Review.read(manifest, manifest_hash)
    state = manifest.with_suffix(".state")
    recorded = read_commit_state(state, manifest_hash)
    head = checked(vault, "rev-parse", "HEAD").strip()
    base = fetch_main(vault)
    if not ancestor(vault, review.base, base):
        raise SaveError("Origin main no longer contains the scanned base.")
    if recorded is None and head == review.head:
        if not ancestor(vault, base, review.head):
            raise SaveError("Origin main advanced outside the scanned history.")
        commit = create_reviewed_commit(vault, review)
    else:
        commit = recover_reviewed_commit(vault, review, head, recorded)
        if not (ancestor(vault, base, commit) or ancestor(vault, commit, base)):
            raise SaveError("Origin main diverged from the reviewed commit.")
    if head == review.head:
        _ = checked(vault, "update-ref", "refs/heads/main", commit, review.head)
    if recorded is None:
        write_bytes_atomic(state, f"{manifest_hash}\n{commit}\n".encode("ascii"), mode=0o600)
    # The reviewed entries match main after the ref update or recovery check.
    update_index(vault, review)
    _ = land_main.land(vault, commit)
    if not ancestor(vault, commit, fetch_main(vault)):
        raise SaveError("Origin main does not contain the reviewed commit.")
    manifest.unlink()
    state.unlink(missing_ok=True)
    print(f"Published the reviewed first save {commit}.")


class Arguments(argparse.Namespace):
    vault: Path = Path()
    command: str = ""
    action: str = ""
    manifest_hash: str = ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--vault", type=Path, default=Path.home() / "rust/hanadocs")
    modes = parser.add_subparsers(dest="command")
    first_save = modes.add_parser("first-save")
    actions = first_save.add_subparsers(dest="action", required=True)
    _ = actions.add_parser("scan")
    _ = actions.add_parser("commit").add_argument("manifest_hash")
    args = parser.parse_args(argv, namespace=Arguments())
    vault = args.vault.resolve()
    try:
        root = Path(checked(vault, "rev-parse", "--show-toplevel").strip()).resolve()
        if vault != root:
            raise SaveError("Use the root of the vault checkout.")
        if args.command != "first-save":
            return save(vault)
        with main_push_lock(vault, "vault-first-save", wait=False):
            if not writable(vault):
                raise SaveError("Use the first save on main with no Git operation in progress.")
            if checked(vault, "config", "--get", "core.fsync").strip() != "committed":
                raise SaveError("Set core.fsync=committed in the vault's Git config.")
            manifest = git_path(vault, "vault-save-manifest.json")
            if args.action == "scan":
                scan_first_save(vault, manifest)
            else:
                commit_first_save(vault, manifest, args.manifest_hash)
        return 0
    except BlockingIOError:
        print("Vault save deferred because the vault lock is busy.", file=sys.stderr)
        return int(args.command == "first-save")
    except (OSError, ValueError, land_main.LandError, subprocess.TimeoutExpired) as error:
        print(f"Vault save failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
