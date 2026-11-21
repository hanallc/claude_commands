#!/usr/bin/env bash
# Make and record a worktree from its source repository.
set -euo pipefail

if [[ $# != 3 ]]; then
    echo 'usage: make_worktree.sh <repo-word> <unit> <short-name>' >&2
    exit 2
fi

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec "$script_dir/../lib/py" "$script_dir/worktrees.py" make "$@"
