"""Give a Git child the environment of this process without the variables that select a repository."""
from __future__ import annotations

from functools import cache
import os
import subprocess


@cache
def _repository_variables() -> frozenset[str]:
    # Git lists these variables, and clears them itself when it enters another repository.
    listing = subprocess.run(["git", "rev-parse", "--local-env-vars"], capture_output=True, text=True, check=True,
                             cwd="/", timeout=30).stdout
    return frozenset(listing.split())


def repository_environment() -> dict[str, str]:
    """Return the process environment without GIT_DIR, GIT_WORK_TREE, GIT_INDEX_FILE and the other repository variables.

    An inherited GIT_DIR, from a hook or a test runner, would make `git -C <folder>` write to another repository.
    The process reads the variable list once.
    """
    removed = _repository_variables()
    return {key: value for key, value in os.environ.items() if key not in removed}
