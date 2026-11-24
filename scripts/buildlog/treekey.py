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

A checkout that is not trusted can name programs in its git config: an
fsmonitor hook, or a clean filter that git status runs on a changed file.
With checkout_programs=False, git runs none of them: the fsmonitor and each
filter driver are turned off, submodule work trees are not read (git status
in a submodule reads the submodule config), and git fetches no missing object.
ignored_paths asks git in the same form.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import hashlib
import os
import subprocess
import sys
import time

GIT_TIMEOUT_S = 10
WORKTREE_STATUS_ARGS = (
    "status", "--porcelain", "-z", "--untracked-files=all", "--no-renames", "--ignore-submodules=none",
)


# A partial clone fetches no missing object, so it runs no fetch program of the checkout config.
NO_FETCH_ENVIRONMENT = {"GIT_NO_LAZY_FETCH": "1"}


def git(directory: str, *args: str, environment: Mapping[str, str] | None = None,
        stdin: int = subprocess.DEVNULL) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        ["git", "--no-optional-locks", "-C", directory, *args],
        stdin=stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=environment,
    )


def no_program_environment(directory: str, environment: Mapping[str, str]) -> dict[str, str] | None:
    """The environment that turns off the fsmonitor and each filter driver of the config, or None.

    GIT_CONFIG_COUNT has the scope of -c, so it wins over every config file of the checkout.
    """
    drivers = finish([git(directory, "config", "--null", "--name-only", "--get-regexp",
                          r"^filter\..+\.(clean|process)$", environment=environment)], missing_ok=True)
    if drivers is None:
        return None
    settings = [("core.fsmonitor", "false")]
    for driver in sorted({name.rpartition(b".")[0] for name in drivers[0].split(b"\0") if name}):
        name = os.fsdecode(driver)
        settings += [(f"{name}.clean", ""), (f"{name}.process", ""), (f"{name}.required", "false")]
    result = {**environment, "GIT_CONFIG_COUNT": str(len(settings))}
    for index, (key, value) in enumerate(settings):
        result[f"GIT_CONFIG_KEY_{index}"] = key
        result[f"GIT_CONFIG_VALUE_{index}"] = value
    return result


def no_program_git_environment(directory: str) -> dict[str, str] | None:
    """The environment of a git call that runs no program that the checkout names, or None when git fails."""
    return no_program_environment(directory, {**os.environ, **NO_FETCH_ENVIRONMENT})


def finish(processes: list[subprocess.Popen[bytes]], *, missing_ok: bool = False,
           stdin_bytes: bytes = b"") -> list[bytes] | None:
    """Each process's stdout, or None when any fails or the shared deadline passes.

    With missing_ok, exit 1 is no failure: git config and git check-ignore give it when they find no name.
    A process started with a stdin pipe gets stdin_bytes.
    """
    deadline = time.monotonic() + GIT_TIMEOUT_S
    outputs: list[bytes] = []
    try:
        for process in processes:
            stdout, _ = process.communicate(stdin_bytes or None, timeout=max(0.0, deadline - time.monotonic()))
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
            for pipe in (process.stdin, process.stdout):
                if pipe is not None:
                    pipe.close()
    return outputs


def ignored_paths(directory: str, paths: Sequence[str]) -> set[str] | None:
    """The paths, relative to directory, that git ignores; None when git fails. git runs no program that the
    checkout names."""
    try:
        environment = no_program_git_environment(directory)
        if environment is None:
            return None
        process = git(directory, "check-ignore", "--stdin", "-z", environment=environment, stdin=subprocess.PIPE)
    except OSError:
        return None
    outputs = finish([process], missing_ok=True, stdin_bytes=b"".join(os.fsencode(path) + b"\0" for path in paths))
    return None if outputs is None else {os.fsdecode(name) for name in outputs[0].split(b"\0") if name}


def content(path: bytes, *, checkout_programs: bool = True) -> bytes:
    if os.path.islink(path):
        return b"link " + os.readlink(path)
    if os.path.isfile(path):
        with open(path, "rb") as handle:
            return hashlib.file_digest(handle, "sha256").digest()
    return gitlink_content(path, checkout_programs=checkout_programs) or b"absent"


def tracked_gitlink(path: bytes, *, checkout_programs: bool = True) -> bool:
    """Check the parent worktree index, including staged submodules."""
    parent, name = os.path.split(path)
    if not name:  # Git status adds a slash to an untracked repository.
        return False
    directory = os.fsdecode(parent or b".")
    environment = None if checkout_programs else no_program_git_environment(directory)
    if not checkout_programs and environment is None:
        raise OSError(f"no safe Git environment for {directory}")
    outputs = finish([git(directory, "--literal-pathspecs", "ls-files", "--stage", "-z", "--",
                          os.fsdecode(name), environment=environment)])
    if outputs is None:
        raise OSError(f"cannot check whether {os.fsdecode(path)} is a tracked submodule")
    return any(entry.startswith(b"160000 ") and entry.partition(b"\t")[2] == name
               for entry in outputs[0].split(b"\0"))


def gitlink_content(path: bytes, *, checkout_programs: bool = True) -> bytes:
    """Return the checked-out commit and content key for a tracked submodule."""
    if not os.path.isdir(path) or not os.path.lexists(os.path.join(path, b".git")):
        return b""
    if not tracked_gitlink(path, checkout_programs=checkout_programs):
        return b""
    submodule_key = _tree_key(os.fsdecode(path), submodule=True, checkout_programs=checkout_programs)
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


def tree_key(directory: str, *, exclude_session_dirs: bool = False, checkout_programs: bool = True) -> str | None:
    """Read the worktree key with optional session and checkout program controls."""
    return _tree_key(directory, submodule=False, exclude_session_dirs=exclude_session_dirs,
                     checkout_programs=checkout_programs)


def _tree_key(directory: str, *, submodule: bool, exclude_session_dirs: bool = False,
              checkout_programs: bool = True) -> str | None:
    revision = ("HEAD^{tree}", "HEAD") if submodule else ("HEAD^{tree}",)
    status_args = WORKTREE_STATUS_ARGS
    environment = None
    try:
        if not checkout_programs:
            environment = no_program_git_environment(directory)
            if environment is None:
                return None
            status_args = (*WORKTREE_STATUS_ARGS[:-1], "--ignore-submodules=dirty")
        processes = [
            git(directory, "rev-parse", "--show-toplevel", *revision, environment=environment),
            git(directory, *status_args, environment=environment),
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
            key.update(path + b"\0" + content(os.path.join(top, path), checkout_programs=checkout_programs))
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
