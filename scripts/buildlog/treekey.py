"""The tree key: one hash that names what a worktree holds.

The key covers HEAD's tree, then every path that differs from it -- staged,
unstaged, deleted, or new and not ignored -- sorted, each as its path, a NUL
and its content: the sha256 of its bytes, `link <target>` for a symlink,
`absent` when deleted. Those are the content rules of verify.sh's pass-record
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

A checkout that is not trusted can name programs in its git config: an
fsmonitor hook, or a clean filter that git status runs on a changed file.
With checkout_programs=False, git runs none of them: the fsmonitor and each
filter driver are turned off, submodule work trees are not read (git status
in a submodule reads the submodule config), and git fetches no missing object.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import hashlib
import os
import subprocess
import time

GIT_TIMEOUT_S = 10


# A partial clone fetches no missing object, so it runs no fetch program of the checkout config.
NO_FETCH_ENVIRONMENT = {"GIT_NO_LAZY_FETCH": "1"}


def git(directory: str, *args: str, options: Sequence[str] = (),
        environment: Mapping[str, str] | None = None) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        ["git", "--no-optional-locks", *options, "-C", directory, *args],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=environment,
    )


def no_program_options(directory: str, environment: Mapping[str, str]) -> list[str] | None:
    """The options that turn off the fsmonitor and each filter driver of the config, or None.

    A driver name with = cannot be given to -c, so it gives None.
    """
    drivers = finish([git(directory, "config", "--null", "--name-only", "--get-regexp",
                          r"^filter\..+\.(clean|process)$", environment=environment)], missing_ok=True)
    if drivers is None:
        return None
    options = ["-c", "core.fsmonitor=false"]
    for driver in sorted({name.rpartition(b".")[0] for name in drivers[0].split(b"\0") if name}):
        if b"=" in driver:
            return None
        name = os.fsdecode(driver)
        options += ["-c", f"{name}.clean=", "-c", f"{name}.process=", "-c", f"{name}.required=false"]
    return options


def finish(processes: list[subprocess.Popen[bytes]], *, missing_ok: bool = False) -> list[bytes] | None:
    """Each process's stdout, or None when any fails or the shared deadline passes.

    With missing_ok, exit 1 is no failure: git config gives it when it finds no name.
    """
    deadline = time.monotonic() + GIT_TIMEOUT_S
    outputs: list[bytes] = []
    try:
        for process in processes:
            stdout, _ = process.communicate(timeout=max(0.0, deadline - time.monotonic()))
            if process.returncode not in ((0, 1) if missing_ok else (0,)):
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


def content(path: bytes) -> bytes:
    if os.path.islink(path):
        return b"link " + os.readlink(path)
    if os.path.isfile(path):
        with open(path, "rb") as handle:
            return hashlib.file_digest(handle, "sha256").digest()
    return b"absent"


def committed_tree_key(tree_id: bytes) -> str:
    """Name a clean checkout of the given tree."""
    return hashlib.sha256(tree_id + b"\n").hexdigest()


def tree_key(directory: str, *, checkout_programs: bool = True) -> str | None:
    """The key of the worktree; with checkout_programs=False, git runs no program that the checkout names."""
    options: list[str] = []
    submodules: list[str] = []
    environment = None if checkout_programs else {**os.environ, **NO_FETCH_ENVIRONMENT}
    try:
        if environment is not None:
            found = no_program_options(directory, environment)
            if found is None:
                return None
            options, submodules = found, ["--ignore-submodules=dirty"]
        processes = [
            git(directory, "rev-parse", "--show-toplevel", "HEAD^{tree}", options=options, environment=environment),
            # Staged, unstaged, deleted and untracked alike, every untracked file
            # by name; porcelain names paths from the top in any folder.
            git(directory, "status", "--porcelain", "-z", "--untracked-files=all", "--no-renames", *submodules,
                options=options, environment=environment),
        ]
    except OSError:
        return None
    outputs = finish(processes)
    if outputs is None:
        return None
    heads, status = outputs
    lines = heads.splitlines()
    if len(lines) != 2:
        return None
    top, tree = lines
    if not status:
        return committed_tree_key(tree)
    key = hashlib.sha256(tree + b"\n")
    # Each entry is "XY PATH"; a path both staged and untracked (git rm --cached) comes twice.
    try:
        for path in sorted({entry[3:] for entry in status.split(b"\0") if len(entry) > 3}):
            key.update(path + b"\0" + content(os.path.join(top, path)))
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
