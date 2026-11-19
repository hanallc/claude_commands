"""The tree key: one hash that names what a worktree holds.

The key covers HEAD's tree, then every path that differs from it -- staged,
unstaged, deleted, or new and not ignored -- sorted, each as its path, a NUL
and its content: the sha256 of its bytes, `link <target>` for a symlink,
`absent` when deleted. A changed submodule includes its checked-out commit and
tree key. Regular files use the content rules of verify.sh's pass-record
key (TREE_KEY_PY), without its worktree path, rustc, script hashes, command
words or environment: this key names the tree alone, and the build log matches
the worktree and the argv on columns of their own. Two clones holding the same
files have the same key.

Every git call passes --no-optional-locks: git must never refresh the index of
a worktree another session is using, since an index.lock collision breaks
that session's git. That is why the changed paths come from `git status`,
which honours the flag, and not from `git diff HEAD` plus `git ls-files
--others` as in verify.sh: porcelain diff rewrites a stat-dirty index even
with the flag (git 2.54, 2026-10-02), and plumbing diff-index lists a
stat-dirty file as changed, so the key would move whenever another session's
git refreshed the index. No git objects are written either. Both git calls
run at once; any failure (not a repository, an unborn HEAD, a timeout) gives
None.
"""

from __future__ import annotations

from enum import Enum, auto
import hashlib
import os
import subprocess
import sys
import time

GIT_TIMEOUT_S = 10
WORKTREE_STATUS_ARGS = (
    "status", "--porcelain", "-z", "--untracked-files=all", "--no-renames", "--ignore-submodules=none",
)


def git(directory: str, *args: str) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        ["git", "--no-optional-locks", "-C", directory, *args],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )


def finish(processes: list[subprocess.Popen[bytes]]) -> list[bytes] | None:
    """Each process's stdout, or None when any fails or the shared deadline passes."""
    deadline = time.monotonic() + GIT_TIMEOUT_S
    outputs: list[bytes] = []
    try:
        for process in processes:
            stdout, _ = process.communicate(timeout=max(0.0, deadline - time.monotonic()))
            if process.returncode != 0:
                return None
            outputs.append(stdout)
    except subprocess.TimeoutExpired:
        return None
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                _ = process.wait()
            if process.stdout is not None:
                process.stdout.close()
    return outputs


class GitlinkScope(Enum):
    ALL_CHECKOUTS = auto()
    TRACKED_SUBMODULES = auto()


def content(path: bytes, *, gitlinks: GitlinkScope = GitlinkScope.ALL_CHECKOUTS) -> bytes:
    if os.path.islink(path):
        return b"link " + os.readlink(path)
    if os.path.isfile(path):
        with open(path, "rb") as handle:
            return hashlib.file_digest(handle, "sha256").digest()
    return gitlink_content(path, gitlinks=gitlinks) or b"absent"


def tracked_gitlink(path: bytes) -> bool:
    """Check the parent worktree index, including staged submodules."""
    parent, name = os.path.split(path)
    if not name:  # Git status adds a slash to an untracked repository.
        return False
    outputs = finish([git(os.fsdecode(parent or b"."), "--literal-pathspecs",
                          "ls-files", "--stage", "-z", "--", os.fsdecode(name))])
    if outputs is None:
        raise OSError(f"cannot check whether {os.fsdecode(path)} is a tracked submodule")
    return any(entry.startswith(b"160000 ") and entry.partition(b"\t")[2] == name
               for entry in outputs[0].split(b"\0"))


def gitlink_content(path: bytes, *, gitlinks: GitlinkScope = GitlinkScope.ALL_CHECKOUTS) -> bytes:
    """Return the checked-out commit and content key for a Git worktree."""
    if not os.path.isdir(path) or not os.path.lexists(os.path.join(path, b".git")):
        return b""
    if gitlinks is GitlinkScope.TRACKED_SUBMODULES and not tracked_gitlink(path):
        return b""
    submodule_key = _tree_key(os.fsdecode(path), submodule=True, gitlinks=gitlinks)
    if submodule_key is None:
        raise OSError(f"no tree key for {os.fsdecode(path)}")
    return b"gitlink " + submodule_key.encode("ascii")


def session_directory_prefixes(top: bytes) -> tuple[bytes, ...]:
    """Find the session and run directories inside this worktree."""
    top = os.path.realpath(top)
    return tuple(
        folder + b"/" for folder in (
            os.path.relpath(os.path.realpath(os.fsencode(name)), top)
            for name in {os.environ.get("UNIT_SESSION_DIR", ""), os.environ.get("UNIT_RUN_DIR", "")} if name
        ) if not folder.startswith(b"..") and folder != b"."
    )


def changed_paths(status: bytes, *, exclude: tuple[bytes, ...] = ()) -> list[bytes]:
    """List each changed path outside the excluded prefixes once."""
    return sorted({entry[3:] for entry in status.split(b"\0") if len(entry) > 3 and not entry[3:].startswith(exclude)})


def committed_tree_key(tree_id: bytes) -> str:
    """Name a clean checkout of the given tree."""
    return hashlib.sha256(tree_id + b"\n").hexdigest()


def tree_key(directory: str, *, exclude_session_dirs: bool = False) -> str | None:
    return _tree_key(directory, submodule=False, exclude_session_dirs=exclude_session_dirs)


def _tree_key(directory: str, *, submodule: bool, exclude_session_dirs: bool = False,
              gitlinks: GitlinkScope = GitlinkScope.ALL_CHECKOUTS) -> str | None:
    revision = ("HEAD^{tree}", "HEAD") if submodule else ("HEAD^{tree}",)
    try:
        processes = [
            git(directory, "rev-parse", "--show-toplevel", *revision),
            # Staged, unstaged, deleted and untracked alike, every untracked file
            # by name; porcelain names paths from the top in any folder.
            git(directory, *WORKTREE_STATUS_ARGS),
        ]
    except OSError:
        return None
    outputs = finish(processes)
    if outputs is None:
        return None
    heads, status = outputs
    lines = heads.splitlines()
    if len(lines) != 1 + len(revision):
        return None
    top, *identity = lines
    if submodule and os.path.realpath(os.fsencode(directory)) != top:
        return None
    tree = b"\0".join(identity)
    if not status:
        return committed_tree_key(tree)
    key = hashlib.sha256(tree + b"\n")
    excluded = session_directory_prefixes(top) if exclude_session_dirs else ()
    try:
        for path in changed_paths(status, exclude=excluded):
            key.update(path + b"\0" + content(os.path.join(top, path), gitlinks=gitlinks))
    except OSError:
        return None
    return key.hexdigest()


def worktree(directory: str) -> str | None:
    """The top folder of the worktree holding directory, as the build log's worktree column names it."""
    try:
        result = subprocess.run(
            ["git", "--no-optional-locks", "-C", directory, "rev-parse", "--show-toplevel"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    top = result.stdout.strip()
    return top if result.returncode == 0 and top else None


if __name__ == "__main__":
    key = tree_key(sys.argv[1], exclude_session_dirs="--exclude-session-dirs" in sys.argv[2:])
    if key is None:
        raise SystemExit(1)
    print(key)
