#!/usr/bin/env bash
set -euo pipefail

# Move unpushed default-branch commits onto a PR branch, wait for checks, merge,
# and restore the local default branch.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=ci_watch_lib.sh
source "${SCRIPT_DIR}/ci_watch_lib.sh"

BRANCH_NAME="${1:?Usage: push_pr_branch_and_merge.sh <branch-name>}"

DEFAULT_BRANCH="$(gh repo view --json defaultBranchRef -q .defaultBranchRef.name)"
CURRENT_BRANCH="$(git branch --show-current)"

run_landed_hook() {
  local landed_sha
  landed_sha="$(git rev-parse HEAD)"
  "${SCRIPT_DIR}/../lib/py" "${SCRIPT_DIR}/../lib/land_main.py" \
    --checkout "$PWD" --commit "$landed_sha" --branch "$DEFAULT_BRANCH"
}

if ! git check-ref-format --branch "$BRANCH_NAME" >/dev/null; then
  echo "ERROR: invalid branch name: ${BRANCH_NAME}" >&2
  exit 1
fi

if [[ "$CURRENT_BRANCH" != "$DEFAULT_BRANCH" && "$CURRENT_BRANCH" != "$BRANCH_NAME" ]]; then
  echo "ERROR: PR path must start on ${DEFAULT_BRANCH} or ${BRANCH_NAME}" >&2
  exit 1
fi

if [[ "$CURRENT_BRANCH" == "$DEFAULT_BRANCH" ]]; then
  git fetch origin "refs/heads/${DEFAULT_BRANCH}:refs/remotes/origin/${DEFAULT_BRANCH}"
fi
if [[ "$CURRENT_BRANCH" == "$DEFAULT_BRANCH" && -z "$(git log --format="%h %s" "origin/${DEFAULT_BRANCH}..HEAD")" ]]; then
  git merge --ff-only "origin/${DEFAULT_BRANCH}"
  if [[ "$(git rev-parse HEAD)" != "$(git rev-parse "origin/${DEFAULT_BRANCH}")" ]]; then
    echo "ERROR: ${DEFAULT_BRANCH} does not match origin/${DEFAULT_BRANCH}" >&2
    exit 1
  fi
  echo "Nothing to push."
  # A prior run may have landed the merge but stopped before its hook finished.
  run_landed_hook
  exit 0
fi

if [[ "$CURRENT_BRANCH" == "$DEFAULT_BRANCH" ]]; then
  echo "Creating PR branch ${BRANCH_NAME} from current HEAD..."
  git switch -c "$BRANCH_NAME"
  git branch -f "$DEFAULT_BRANCH" "origin/${DEFAULT_BRANCH}"
else
  echo "Resuming PR branch ${BRANCH_NAME}..."
fi

echo "Pushing ${BRANCH_NAME}..."
BRANCH_REF="refs/heads/$BRANCH_NAME"
BRANCH_TIP="$(git rev-parse --verify "$BRANCH_REF")"
git push -u origin "$BRANCH_NAME"

if PR_NUMBER="$(gh pr view --json number -q .number 2>/dev/null)"; then
  echo "Using existing PR #${PR_NUMBER}."
else
  echo "Opening pull request..."
  gh pr create --fill
  PR_NUMBER="$(gh pr view --json number -q .number)"
fi

echo "Watching checks for PR #${PR_NUMBER}..."
MAX_ATTEMPTS=24
POLL_INTERVAL=5
for attempt in $(seq 1 "$MAX_ATTEMPTS"); do
  set +e
  CHECK_OUTPUT="$(gh pr checks "$PR_NUMBER" 2>&1)"
  CHECK_STATUS="$?"
  set -e

  if [[ "$CHECK_OUTPUT" != *"no checks reported"* ]]; then
    break
  fi

  echo "Attempt ${attempt}/${MAX_ATTEMPTS}: no checks reported yet, waiting ${POLL_INTERVAL}s..."
  sleep "$POLL_INTERVAL"
done

if [[ "$CHECK_OUTPUT" == *"no checks reported"* ]]; then
  echo "ERROR: no checks reported for PR #${PR_NUMBER} after polling." >&2
  exit 1
fi

if ! watch_pr_checks_until_settled "$PR_NUMBER"; then
  echo "ERROR: CI failed for PR #${PR_NUMBER}. Leaving ${BRANCH_NAME} checked out for iteration." >&2
  exit 1
fi

echo "Merging PR #${PR_NUMBER}..."
set +e
MERGE_OUTPUT="$(gh pr merge "$PR_NUMBER" --rebase --match-head-commit "$BRANCH_TIP" 2>&1)"
MERGE_STATUS="$?"
set -e

if [[ -n "$MERGE_OUTPUT" ]]; then
  printf '%s\n' "$MERGE_OUTPUT"
fi

PR_STATE="$(gh pr view "$PR_NUMBER" --json state -q .state)"
if [[ "$PR_STATE" != "MERGED" ]]; then
  echo "ERROR: PR #${PR_NUMBER} has not merged." >&2
  exit "$((MERGE_STATUS == 0 ? 1 : MERGE_STATUS))"
fi
MERGED_HEAD="$(gh pr view "$PR_NUMBER" --json headRefOid -q .headRefOid)"
if [[ "$MERGED_HEAD" != "$BRANCH_TIP" ]]; then
  echo "ERROR: PR #${PR_NUMBER} merged a different commit. The origin ref stays." >&2
  exit 1
fi
if ! "${SCRIPT_DIR}/../lib/py" "${SCRIPT_DIR}/../lib/origin_deletion.py" \
    "$PWD" origin "$BRANCH_REF" "$BRANCH_TIP"; then
  echo "Origin ref cleanup failed for $BRANCH_REF." >&2
fi
MERGE_SHA="$(gh pr view "$PR_NUMBER" --json mergeCommit -q .mergeCommit.oid 2>/dev/null || true)"

git switch "$DEFAULT_BRANCH"
git pull --ff-only

if [[ -n "$MERGE_SHA" ]]; then
  echo "Merged PR #${PR_NUMBER} as ${MERGE_SHA}. CI was green before merge."
else
  echo "Merged PR #${PR_NUMBER}. CI was green before merge."
fi

# The default branch has landed; run the repo's post-push hook, if it has one.
run_landed_hook
