#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo 'Usage: post_publish_hook.sh <crate> <version> <sha> [--dry-run]' >&2
  exit 1
}

if [[ $# -lt 3 || $# -gt 4 ]]; then
  usage
fi
CRATE="$1"
VERSION="$2"
SHA="$3"
if [[ -z "$CRATE" || -z "$VERSION" || -z "$SHA" ]]; then
  usage
fi
if [[ $# -eq 4 && "$4" != --dry-run ]]; then
  usage
fi
if [[ "$VERSION" == v* ]]; then
  echo 'ERROR: version must have no leading v' >&2
  exit 1
fi
if [[ ! "$SHA" =~ ^[[:xdigit:]]{40}$ ]] || ! git rev-parse --verify "$SHA^{commit}" >/dev/null 2>&1; then
  echo 'ERROR: sha must name a full 40-character commit id in this repository' >&2
  exit 1
fi
ROOT=$(git rev-parse --show-toplevel)
if [[ ! -f "$ROOT/.claude/config/post_publish.sh" ]]; then
  echo 'post-publish hook: absent'
  exit 0
fi
if [[ "${4:-}" == --dry-run ]]; then
  printf '[DRY-RUN] Would run post-publish hook: CRATE=%s VERSION=%s SHA=%s\n' "$CRATE" "$VERSION" "$SHA"
  exit 0
fi

if (cd "$ROOT"; export CRATE VERSION SHA; bash .claude/config/post_publish.sh); then
  echo 'post-publish hook: done'
else
  HOOK_EXIT=$?
  echo "post-publish hook: failed (exit $HOOK_EXIT)" >&2
  printf 'rerun: cd %q && CRATE=%q VERSION=%q SHA=%q bash .claude/config/post_publish.sh\n' \
    "$ROOT" "$CRATE" "$VERSION" "$SHA" >&2
  exit 1
fi
