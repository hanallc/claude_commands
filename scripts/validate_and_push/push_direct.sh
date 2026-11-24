#!/usr/bin/env bash
set -euo pipefail

# Push the current branch, identify the CI run it triggered, and hand watching
# to the agent.
#
# The agent watches with a 3-minute ScheduleWakeup tick instead of a blocking
# `gh run watch`. That surfaces a per-job status line on every tick, and a red
# job gets diagnosed the tick it turns red rather than after the whole run
# settles. This script therefore stops at the push and prints the identifiers
# the tick loop needs.
#
# land_main runs the repository's post-push hook after landing.
# A hook failure reports its rerun command and keeps the landing successful.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# PUSH_TARGET_BRANCH (from validate_and_push.sh --to) names the remote branch
# to push HEAD to; otherwise the current branch is pushed under its own name.
BRANCH="${PUSH_TARGET_BRANCH:-$(git branch --show-current)}"
DEFAULT_BRANCH="$(git symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null || echo origin/main)"
DEFAULT_BRANCH="${DEFAULT_BRANCH#origin/}"

SHA=""
PUSHED=true
PUBLISH_ONLY=false
FOLLOW_PUBLISHED=false
CI_WORKFLOW=""
CI_EVENT=""
DISPATCH_RESULT=""
CI_PUSH_REPO="${CI_PUSH_REPO:-}"
# CI points release the merge lock before they wait for a run.
# The publication receipt is the final line: <sha> <pushed|contained>.
case "${1:-}" in
  --commit)
    [ "$#" -eq 2 ] || { echo 'usage: push_direct.sh --commit <sha>' >&2; exit 64; }
    SHA="${2:?--commit needs a SHA}"
    ;;
  --publish-only)
    [ "$#" -eq 2 ] || { echo 'usage: push_direct.sh --publish-only <sha>' >&2; exit 64; }
    PUBLISH_ONLY=true
    SHA="${2:?--publish-only needs a SHA}"
    ;;
  --dispatch-published)
    [ "$#" -eq 3 ] || {
      echo 'usage: push_direct.sh --dispatch-published <sha> <workflow>' >&2
      exit 64
    }
    SHA="${2:?--dispatch-published needs a SHA}"
    CI_WORKFLOW="$3"
    ;;
  --follow-published)
    [ "$#" -eq 3 ] || [ "$#" -eq 5 ] || [ "$#" -eq 6 ] || {
      echo 'usage: push_direct.sh --follow-published <sha> <pushed|contained> [workflow event [dispatch-result]]' >&2
      exit 64
    }
    FOLLOW_PUBLISHED=true
    SHA="${2:?--follow-published needs a SHA}"
    CI_WORKFLOW="${4:-}"
    CI_EVENT="${5:-}"
    DISPATCH_RESULT="${6:-}"
    case "$3" in
      pushed) PUSHED=true ;;
      contained) PUSHED=false ;;
      *) echo 'push_direct.sh: invalid publication state' >&2; exit 64 ;;
    esac
    ;;
  '') ;;
  *) echo "push_direct.sh: unknown argument: $1" >&2; exit 64 ;;
esac
if [ -z "$SHA" ]; then
  SHA="$(git rev-parse HEAD)"
fi
if [[ ! "$SHA" =~ ^([0-9a-f]{40}|[0-9a-f]{64})$ ]]; then
  echo 'push_direct.sh: publication requires a full commit SHA' >&2
  exit 64
fi

if [ -n "$CI_WORKFLOW" ]; then
  : "${CI_PUSH_REPO:?}"
fi

publish_branch() {
  if [ -n "${CI_VALIDATED_BASE:-}" ]; then
    git fetch --no-tags "${CI_PUSH_ORIGIN:-origin}" "+refs/heads/${BRANCH}:refs/remotes/origin/${BRANCH}"
    REMOTE_TIP="$(git rev-parse "refs/remotes/origin/${BRANCH}")"
    if [ "$SHA" = "$CI_VALIDATED_BASE" ] &&
       git merge-base --is-ancestor "$SHA" "$REMOTE_TIP"; then
      PUSHED=false
      SHA="$REMOTE_TIP"
      echo "Validated ${CI_VALIDATED_BASE}; origin/${BRANCH} contains it; no push."
    elif ! git merge-base --is-ancestor "$CI_VALIDATED_BASE" "$SHA" ||
         ! git merge-base --is-ancestor "$REMOTE_TIP" "$SHA"; then
      echo "merge branch moved during validation" >&2
      exit 1
    fi
  fi

  if [ "$PUSHED" = true ]; then
    echo "Pushing ${BRANCH} to origin..."
  fi
  PUSH_STATUS=0
  if [ "$BRANCH" = "$DEFAULT_BRANCH" ]; then
    "${SCRIPT_DIR}/../lib/py" "${SCRIPT_DIR}/../lib/land_main.py" \
      --checkout "$PWD" --commit "$SHA" --branch "$BRANCH" || PUSH_STATUS=$?
  elif [ "$PUSHED" = true ]; then
    git push origin "${SHA}:refs/heads/${BRANCH}" || PUSH_STATUS=$?
  fi
  if [ "$PUSH_STATUS" -ne 0 ]; then
    if [ -n "${CI_VALIDATED_BASE:-}" ]; then
      echo "merge branch moved during validation" >&2
    fi
    exit "$PUSH_STATUS"
  fi
  if [ "$PUSHED" = true ]; then
    printf '%s pushed\n' "$SHA"
  else
    printf '%s contained\n' "$SHA"
  fi
}

repository_gh() {
  if [ -n "$CI_PUSH_REPO" ]; then
    gh "$1" "$2" --repo "$CI_PUSH_REPO" "${@:3}"
  else
    gh "$@"
  fi
}

matching_dispatch_runs() {
  local runs
  # Check at most 100 runs. CI dispatch uses no inputs and assumes equivalent inputs for matching runs.
  # A listing delay can cause a later invocation to dispatch again.
  runs="$(repository_gh run list --branch "$BRANCH" --commit "$SHA" --workflow "$CI_WORKFLOW" \
    --event workflow_dispatch --limit 100 --json databaseId,status,conclusion,createdAt,headSha)" || return "$?"
  if [ -z "$runs" ]; then
    printf 'GitHub returned no run list\n' >&2
    return 1
  fi
  jq -ec --arg sha "$SHA" '
    if type != "array" then error("invalid GitHub run list")
    else map(select(.headSha == $sha)) end
  ' <<< "$runs"
}

select_dispatch_run() {
  jq -r --arg bound_time "${1:-}" --argjson bound_id "${2:-0}" '
    if $bound_id == 0 then .
    else map(select([.createdAt, .databaseId] > [$bound_time, $bound_id])) end |
    sort_by(.createdAt, .databaseId) |
    ((map(select(.status != "completed")) | last) // last // empty) |
    "\(.databaseId) \(.createdAt) \(.status) \(.conclusion // "")"
  '
}

dispatch_published() {
  local candidate id created_at status conclusion remote_ref
  candidate="$(matching_dispatch_runs | select_dispatch_run)"
  read -r id created_at status conclusion <<< "$candidate"
  if [ -n "$id" ] && { [ "$status" != completed ] || [ "$conclusion" = success ]; }; then
    printf 'reuse %s %s\n' "$id" "$created_at"
    return 0
  fi

  if ! remote_ref="$(git ls-remote --exit-code "${CI_PUSH_ORIGIN:?}" "refs/heads/$BRANCH")" || [ "${remote_ref%%$'\t'*}" != "$SHA" ]; then
    printf 'origin/%s moved before dispatch of %s\n' "$BRANCH" "$SHA" >&2
    return 1
  fi
  repository_gh workflow run --ref "$BRANCH" -- "$CI_WORKFLOW" </dev/null >/dev/null
  printf 'dispatched %s %s\n' "${id:-0}" "$created_at"
}

if [ "${1:-}" = --dispatch-published ]; then
  dispatch_published
  exit 0
fi

if [ "$FOLLOW_PUBLISHED" = false ]; then
  export -f publish_branch
  export SHA BRANCH PUSHED SCRIPT_DIR DEFAULT_BRANCH
  publish_status=0
  if [ "$BRANCH" = "$DEFAULT_BRANCH" ]; then
    publication_output="$("${SCRIPT_DIR}/../lib/py" "${SCRIPT_DIR}/../lib/main_push_lock.py" \
      --checkout "$PWD" -- bash -euo pipefail -c publish_branch)" || publish_status=$?
  else
    publication_output="$(bash -euo pipefail -c publish_branch)" || publish_status=$?
  fi
  if [ "$publish_status" -ne 0 ]; then
    [ -z "$publication_output" ] || printf '%s\n' "$publication_output"
    exit "$publish_status"
  fi
  receipt="${publication_output##*$'\n'}"
  if [[ "$publication_output" == *$'\n'* ]]; then
    printf '%s\n' "${publication_output%$'\n'*}"
  fi
  read -r SHA publication_state <<< "$receipt"
  if [ "$publication_state" = pushed ]; then PUSHED=true; else PUSHED=false; fi
  if [ "$PUBLISH_ONLY" = true ]; then
    printf '%s\n' "$receipt"
    exit 0
  fi
fi
if [ -n "$CI_PUSH_REPO" ]; then
  REPO="$CI_PUSH_REPO"
  REPO_URL="https://$REPO"
else
  REPO="$(gh repo view --json nameWithOwner -q .nameWithOwner)"
  REPO_URL="https://github.com/$REPO"
fi

# `gh run list --commit` requires a full SHA; a short SHA silently returns
# nothing. The run also does not exist the instant the push lands.
RUN=""
follow_status=0
push_filters=()
if [ "$CI_EVENT" = push ]; then
  push_filters=(--workflow "$CI_WORKFLOW" --event push)
fi
if [ "$CI_EVENT" = workflow_dispatch ]; then
  read -r dispatch_action listed_id listed_created_at <<< "$DISPATCH_RESULT"
  case "$dispatch_action" in
    reuse) RUN="$listed_id $listed_created_at" ;;
    dispatched) ;;
    *) echo 'push_direct.sh: invalid dispatch result' >&2; exit 64 ;;
  esac
fi
for i in $(seq 1 40); do
  if [ -n "$RUN" ]; then
    break
  fi
  if [ "$CI_EVENT" = workflow_dispatch ]; then
    candidate="$(matching_dispatch_runs | select_dispatch_run "$listed_created_at" "$listed_id")"
    read -r run_id run_created_at run_status run_conclusion <<< "$candidate"
    if [ -n "$run_id" ]; then
      if [ "$run_status" = completed ] && [ "$run_conclusion" != success ]; then
        printf 'CI run %s completed with %s (%s/actions/runs/%s)\n' \
          "$run_id" "${run_conclusion:-an unknown conclusion}" "$REPO_URL" "$run_id" >&2
        follow_status=1
        break
      fi
      RUN="$run_id $run_created_at"
    fi
  else
    RUN="$(repository_gh run list --branch "$BRANCH" --commit "$SHA" ${push_filters[@]+"${push_filters[@]}"} --json databaseId,createdAt \
      --jq '.[0] | select(. != null) | "\(.databaseId) \(.createdAt | fromdateiso8601 | strflocaltime("%Y-%m-%d %H:%M:%S %Z"))"' \
      2>/dev/null || true)"
  fi
  if [ -n "$RUN" ]; then
    break
  fi
  echo "Attempt ${i}/40: no CI run yet for ${SHA}, waiting 3s..."
  sleep 3
done

if [ "$follow_status" -eq 0 ]; then
  echo
  echo "=== CI HANDOFF TO AGENT ==="
  echo "repo:    ${REPO}"
  echo "branch:  ${BRANCH}"
  echo "sha:     ${SHA}"
  if [ -n "$RUN" ]; then
    RUN_ID="${RUN%% *}"
    echo "run_id:  ${RUN_ID}"
    echo "url:     $REPO_URL/actions/runs/${RUN_ID}"
    echo "started: ${RUN#* }"
  else
    echo "run_id:  none"
    echo "No run appeared within 120s. Find it with:"
    if [ -n "$CI_WORKFLOW" ]; then
      printf '  gh run list --repo %q --branch %q --commit %q --workflow %q --event %q --json databaseId,status\n' \
        "$CI_PUSH_REPO" "$BRANCH" "$SHA" "$CI_WORKFLOW" "$CI_EVENT"
    else
      echo "  gh run list --branch ${BRANCH} --commit ${SHA} --json databaseId,status"
    fi
  fi
  echo
  echo "Watch this run with a 3-minute ScheduleWakeup tick. Do not block on"
  echo "\`gh run watch\`, and do not sleep-poll in-band."
fi

cancel_prior_runs() {
  local branch sha workflows workflow runs run_id head_sha status live_status
  branch="$BRANCH"
  sha="$SHA"
  if ! workflows="$(repository_gh run list --branch "$branch" --commit "$sha" --json workflowName \
    --jq '[.[].workflowName | select(. != null)] | unique[]' 2>/dev/null)"; then
    printf 'warning: failed to list runs for %s on %s\n' "${sha:0:7}" "$branch" >&2
    return 0
  fi
  if [ -z "$workflows" ]; then
    printf 'no run for %s yet; nothing cancelled\n' "${sha:0:7}"
    return 0
  fi

  while IFS= read -r workflow; do
    for live_status in queued in_progress; do
      if ! runs="$(repository_gh run list --workflow "$workflow" --branch "$branch" --status "$live_status" \
        --json databaseId,headSha,status \
        --jq '.[] | "\(.databaseId)\t\(.headSha)\t\(.status)"' 2>/dev/null)"; then
        printf 'warning: failed to list %s runs for %s on %s\n' "$live_status" "$workflow" "$branch" >&2
        continue
      fi
      [ -n "$runs" ] || continue
      while IFS=$'\t' read -r run_id head_sha status; do
        if [ "$head_sha" != "$sha" ] && { [ "$status" = queued ] || [ "$status" = in_progress ]; }; then
          if repository_gh run cancel "$run_id" >/dev/null 2>&1; then
            printf 'cancelled run %s (%s)\n' "$run_id" "${head_sha:0:7}"
          else
            printf 'warning: failed to cancel run %s\n' "$run_id" >&2
          fi
        fi
      done <<< "$runs"
    done
  done <<< "$workflows"
}

if [ "${CANCEL_PRIOR:-false}" = true ]; then
  cancel_prior_runs
fi
exit "$follow_status"
