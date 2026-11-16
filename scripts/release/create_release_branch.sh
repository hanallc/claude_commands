#!/usr/bin/env bash
set -euo pipefail

# Usage: create_release_branch.sh <version> [--dry-run] [--package <name>]
# Creates and checks out a release branch from current HEAD.
# Branch name is release-<version>, or release-<package>-<version> when --package
# is given (single-package release out of a multi-crate workspace).
# With --dry-run, reports what would happen without creating the branch.
# Exit 0 = success, Exit 1 = failure

VERSION="$1"
shift

DRY_RUN=""
PACKAGE=""
while [[ "${1:-}" == --* ]]; do
  case "$1" in
    --dry-run) DRY_RUN="--dry-run"; shift ;;
    --package) PACKAGE="$2"; shift 2 ;;
    *) echo "ERROR: unknown flag: $1" >&2; exit 1 ;;
  esac
done

if [[ -n "$PACKAGE" ]]; then
  BRANCH="release-${PACKAGE}-${VERSION}"
else
  BRANCH="release-${VERSION}"
fi

HEAD_COMMIT=$(git rev-parse HEAD)
if RELEASE_COMMIT=$(git rev-parse --verify "refs/heads/$BRANCH" 2>/dev/null); then
  if [[ "$RELEASE_COMMIT" != "$HEAD_COMMIT" ]]; then
    if [[ -n "$DRY_RUN" ]]; then
      echo "[DRY-RUN] Would refuse release branch $BRANCH"
    fi
    echo "ERROR: release branch $BRANCH is at $RELEASE_COMMIT; HEAD is $HEAD_COMMIT" >&2
    exit 1
  fi
  if [[ -n "$DRY_RUN" ]]; then
    echo "[DRY-RUN] Would reuse release branch $BRANCH at $HEAD_COMMIT"
  else
    git checkout "$BRANCH"
    echo "reusing release branch $BRANCH"
  fi
  exit 0
fi

if [[ "$DRY_RUN" == "--dry-run" ]]; then
  echo "[DRY-RUN] Would create release branch: $BRANCH"
  echo "[DRY-RUN] Would checkout: $BRANCH"
  echo "[DRY-RUN] Current HEAD: $(git rev-parse --short HEAD)"
  exit 0
fi

echo "Creating release branch: $BRANCH"

git checkout -b "$BRANCH"

CURRENT=$(git rev-parse --abbrev-ref HEAD)
if [[ "$CURRENT" != "$BRANCH" ]]; then
  echo "ERROR: Expected to be on $BRANCH, but on $CURRENT" >&2
  exit 1
fi

echo "On release branch: $BRANCH ✓"
