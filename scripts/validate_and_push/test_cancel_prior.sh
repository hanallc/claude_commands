#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TEST_DIR="$(mktemp -d)"
trap 'rm -rf "$TEST_DIR"' EXIT
mkdir -p "$TEST_DIR/scripts" "$TEST_DIR/bin" "$TEST_DIR/lib"

# Exercise the real push path with disposable hooks and command stubs.
cp "$SCRIPT_DIR/validate_and_push.sh" "$SCRIPT_DIR/push_direct.sh" "$TEST_DIR/scripts/"
cat > "$TEST_DIR/scripts/run_validation.sh" <<'STUB'
#!/usr/bin/env bash
set -euo pipefail
touch "$VALIDATION_RAN"
STUB
cat > "$TEST_DIR/scripts/choose_push_path.sh" <<'STUB'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' '{"push_path":"direct"}'
STUB
cat > "$TEST_DIR/lib/py" <<'STUB'
#!/usr/bin/env bash
set -euo pipefail
exec bash "$@"
STUB
chmod +x "$TEST_DIR/lib/py"
cat > "$TEST_DIR/lib/main_push_lock.py" <<'STUB'
#!/usr/bin/env bash
set -euo pipefail
shift 3
exec "$@"
STUB
cat > "$TEST_DIR/lib/land_main.py" <<'STUB'
#!/usr/bin/env bash
set -euo pipefail
branch=
commit=
while [ "$#" -gt 0 ]; do
  case "$1" in
    --branch) branch="$2"; shift 2 ;;
    --commit) commit="$2"; shift 2 ;;
    --checkout) shift 2 ;;
    *) exit 64 ;;
  esac
done
git push origin "$commit:refs/heads/$branch"
if [ "${HOOK_EXIT_STATUS:-0}" -ne 0 ]; then
  echo "HookFailed: exit $HOOK_EXIT_STATUS"
  echo "Rerun: land_main.py --branch $branch" >&2
else
  echo HookAbsent
fi
STUB

cat > "$TEST_DIR/bin/git" <<'STUB'
#!/usr/bin/env bash
set -euo pipefail
printf '%q ' "$@" >> "$GIT_CALLS"
printf '\n' >> "$GIT_CALLS"
case "$1 $2" in
  'push origin') ;;
  'branch --show-current') printf '%s\n' 'ci-test-branch' ;;
  'symbolic-ref --quiet') printf '%s\n' "${GIT_DEFAULT_BRANCH:-origin/ci-test-branch}" ;;
  'rev-parse HEAD') printf '%s\n' 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' ;;
  *) printf 'unexpected git call: %s\n' "$*" >&2; exit 1 ;;
esac
STUB

cat > "$TEST_DIR/bin/gh" <<'STUB'
#!/usr/bin/env bash
set -euo pipefail
printf '%q ' "$@" >> "$GH_CALLS"
printf '\n' >> "$GH_CALLS"
case "$1 $2" in
  'repo view') printf '%s\n' 'example/repo' ;;
  'run list')
    commit=false
    filter=''
    workflow=''
    status=''
    while (( $# > 0 )); do
      case "$1" in
        --commit) commit=true; shift 2 ;;
        --jq) filter="$2"; shift 2 ;;
        --workflow) workflow="$2"; shift 2 ;;
        --status) status="$2"; shift 2 ;;
        *) shift ;;
      esac
    done
    if "$commit"; then
      if [[ "${GH_FAIL_QUERY:-}" == successor ]]; then
        printf 'simulated query failure\n' >&2
        exit 1
      fi
      if [[ "${GH_NO_SUCCESSOR:-0}" == 1 ]]; then
        runs='[]'
      elif [[ "${GH_SUCCESSOR_FAILED:-0}" == 1 ]]; then
        runs='[{"databaseId":101,"headSha":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","status":"completed","conclusion":"failure","workflowName":"CI","createdAt":"2026-10-04T12:00:00Z"}]'
      else
        runs='[{"databaseId":101,"headSha":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","status":"queued","workflowName":"CI","createdAt":"2026-10-04T12:00:00Z"}]'
      fi
    else
      if [[ "$status" == "${GH_FAIL_QUERY:-}" ]]; then
        printf 'simulated query failure\n' >&2
        exit 1
      fi
      runs='[
        {"databaseId":101,"headSha":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","status":"queued","workflowName":"CI"},
        {"databaseId":102,"headSha":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","status":"queued","workflowName":"CI"},
        {"databaseId":103,"headSha":"cccccccccccccccccccccccccccccccccccccccc","status":"in_progress","workflowName":"CI"},
        {"databaseId":104,"headSha":"dddddddddddddddddddddddddddddddddddddddd","status":"completed","workflowName":"CI"},
        {"databaseId":105,"headSha":"eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee","status":"queued","workflowName":"Other"}
      ]'
      runs="$(jq -c --arg workflow "$workflow" --arg status "$status" \
        '[.[] | select(.workflowName == $workflow and .status == $status)]' <<< "$runs")"
    fi
    if [[ -n "$filter" ]]; then jq -r "$filter" <<< "$runs"; else printf '%s\n' "$runs"; fi
    ;;
  'run cancel')
    if [[ "${@: -1}" == "${GH_FAIL_CANCEL_ID:-}" ]]; then
      printf 'simulated cancellation failure\n' >&2
      exit 1
    fi
    ;;
  *) printf 'unexpected gh call: %s\n' "$*" >&2; exit 1 ;;
esac
STUB
cat > "$TEST_DIR/bin/sleep" <<'STUB'
#!/usr/bin/env bash
set -euo pipefail
STUB
chmod +x "$TEST_DIR/bin/git" "$TEST_DIR/bin/gh" "$TEST_DIR/bin/sleep"

export GIT_CALLS="$TEST_DIR/git_calls" GH_CALLS="$TEST_DIR/gh_calls" VALIDATION_RAN="$TEST_DIR/validation-ran"
export PATH="$TEST_DIR/bin:$PATH"
failed=0

line_count() { grep -Fxc "$1" "$2" || true; }
query_line() { printf '%q ' "$@"; }
cancelled_ids() { awk '$1 == "run" && $2 == "cancel" {print $NF}' "$GH_CALLS" | sort -n; }

run_case() {
  local name="$1" expected_status="$2" actual_status=0
  shift 2
  local script=validate_and_push.sh expected_pushes=1
  if [[ "${1:-}" == --follow-published ]]; then
    script=push_direct.sh
    expected_pushes=0
  fi
  : > "$GIT_CALLS"
  : > "$GH_CALLS"
  bash "$TEST_DIR/scripts/$script" "$@" > "$TEST_DIR/stdout" 2> "$TEST_DIR/stderr" || actual_status=$?
  if [[ "$actual_status" != "$expected_status" ]] ||
    [[ "$(line_count 'push origin aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa:refs/heads/ci-test-branch ' "$GIT_CALLS")" != "$expected_pushes" ]]; then
    printf 'FAIL %s: push or exit status (got %s, expected %s)\n' "$name" "$actual_status" "$expected_status"
    cat "$GIT_CALLS" "$GH_CALLS" "$TEST_DIR/stdout" "$TEST_DIR/stderr"
    failed=1
    return 1
  fi
}

check_case() {
  local name="$1"
  shift
  if "$@"; then
    printf 'PASS %s\n' "$name"
  else
    printf 'FAIL %s\n' "$name"
    cat "$GH_CALLS" "$TEST_DIR/stdout" "$TEST_DIR/stderr"
    failed=1
  fi
}

both_older_runs_cancelled() {
  [[ "$(cancelled_ids)" == $'102\n103' ]] &&
    [[ "$(line_count 'cancelled run 102 (bbbbbbb)' "$TEST_DIR/stdout")" == 1 ]] &&
    [[ "$(line_count 'cancelled run 103 (ccccccc)' "$TEST_DIR/stdout")" == 1 ]] &&
    [[ "$(grep -c '^cancelled run ' "$TEST_DIR/stdout" || true)" == 2 ]]
}

hook_failure_keeps_cancellation() {
  both_older_runs_cancelled &&
    grep -Fq "HookFailed: exit $HOOK_EXIT_STATUS" "$TEST_DIR/stdout" &&
    grep -Fq 'Rerun: land_main.py' "$TEST_DIR/stderr"
}

live_queries_are_exact() {
  local workflow_query queued_query running_query
  workflow_query="$(query_line run list --branch ci-test-branch --commit aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa \
    --json workflowName --jq '[.[].workflowName | select(. != null)] | unique[]')"
  queued_query="$(query_line run list --workflow CI --branch ci-test-branch --status queued \
    --json databaseId,headSha,status --jq '.[] | "\(.databaseId)\t\(.headSha)\t\(.status)"')"
  running_query="$(query_line run list --workflow CI --branch ci-test-branch --status in_progress \
    --json databaseId,headSha,status --jq '.[] | "\(.databaseId)\t\(.headSha)\t\(.status)"')"
  [[ "$(line_count "$workflow_query" "$GH_CALLS")" == 1 ]] &&
    [[ "$(line_count "$queued_query" "$GH_CALLS")" == 1 ]] &&
    [[ "$(line_count "$running_query" "$GH_CALLS")" == 1 ]] &&
    [[ "$(awk '$1 == "run" && $2 == "list" {n++} END {print n+0}' "$GH_CALLS")" == 4 ]] &&
    ! grep -Fq -- '--limit' "$GH_CALLS"
}

if run_case 'superseded live runs are cancelled' 0 --to ci-test-branch --cancel-prior; then
  check_case 'superseded live runs are cancelled' both_older_runs_cancelled
  check_case 'live lookup uses one exact query per status' live_queries_are_exact
fi

rm -f "$VALIDATION_RAN"
if CI_VALIDATED_BASE=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa GIT_DEFAULT_BRANCH=origin/main \
    run_case 'a tested SHA goes straight to publication' 0 --to ci-test-branch \
      --publish-tested aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa --cancel-prior; then
  if [[ ! -e "$VALIDATION_RAN" ]] && [[ "$(line_count 'rev-parse HEAD ' "$GIT_CALLS")" == 0 ]] &&
      both_older_runs_cancelled; then
    printf 'PASS a tested SHA goes straight to publication\n'
  else
    printf 'FAIL a tested SHA goes straight to publication\n'
    failed=1
  fi
fi

if run_case 'push without cancellation leaves runs alone' 0 --to ci-test-branch; then
  if [[ "$(awk '$1 == "run" && $2 == "list" {n++} END {print n+0}' "$GH_CALLS")" == 1 ]] &&
    ! grep -Eq '^run cancel |--workflow |workflowName|headSha' "$GH_CALLS"; then
    printf 'PASS push without cancellation leaves runs alone\n'
  else
    printf 'FAIL push without cancellation leaves runs alone\n'
    failed=1
  fi
fi

export GH_FAIL_CANCEL_ID=102
if run_case 'failed cancellation continues to the next run' 0 --to ci-test-branch --cancel-prior; then
  if [[ "$(cancelled_ids)" == $'102\n103' ]] &&
    [[ "$(line_count 'warning: failed to cancel run 102' "$TEST_DIR/stderr")" == 1 ]] &&
    [[ "$(wc -l < "$TEST_DIR/stderr")" == 1 ]] &&
    [[ "$(line_count 'cancelled run 103 (ccccccc)' "$TEST_DIR/stdout")" == 1 ]] &&
    [[ "$(grep -c '^cancelled run ' "$TEST_DIR/stdout" || true)" == 1 ]]; then
    printf 'PASS failed cancellation continues to the next run\n'
  else
    printf 'FAIL failed cancellation continues to the next run\n'
    failed=1
  fi
fi
unset GH_FAIL_CANCEL_ID

export GH_FAIL_QUERY=successor
if run_case 'failed successor query warns and preserves push status' 0 --to ci-test-branch --cancel-prior; then
  if [[ -z "$(cancelled_ids)" ]] &&
    [[ "$(line_count 'warning: failed to list runs for aaaaaaa on ci-test-branch' "$TEST_DIR/stderr")" == 1 ]] &&
    [[ "$(wc -l < "$TEST_DIR/stderr")" == 1 ]]; then
    printf 'PASS failed successor query warns and preserves push status\n'
  else
    printf 'FAIL failed successor query warns and preserves push status\n'
    failed=1
  fi
fi
unset GH_FAIL_QUERY

export GH_FAIL_QUERY=queued
if run_case 'failed queued query still checks running runs' 0 --to ci-test-branch --cancel-prior; then
  if [[ "$(cancelled_ids)" == 103 ]] &&
    [[ "$(line_count 'warning: failed to list queued runs for CI on ci-test-branch' "$TEST_DIR/stderr")" == 1 ]] &&
    [[ "$(wc -l < "$TEST_DIR/stderr")" == 1 ]] &&
    [[ "$(line_count 'cancelled run 103 (ccccccc)' "$TEST_DIR/stdout")" == 1 ]]; then
    printf 'PASS failed queued query still checks running runs\n'
  else
    printf 'FAIL failed queued query still checks running runs\n'
    failed=1
  fi
fi
unset GH_FAIL_QUERY

export HOOK_EXIT_STATUS=7
if run_case 'target branch hook failure reports and still cancels' 0 --to ci-test-branch --cancel-prior; then
  check_case 'target branch hook failure reports and still cancels' hook_failure_keeps_cancellation
fi
export HOOK_EXIT_STATUS=9
if run_case 'current branch hook failure reports and still cancels' 0 --cancel-prior; then
  check_case 'current branch hook failure reports and still cancels' hook_failure_keeps_cancellation
fi
unset HOOK_EXIT_STATUS

export GH_NO_SUCCESSOR=1
if run_case 'missing successor run leaves old runs and explains why' 0 --to ci-test-branch --cancel-prior; then
  if [[ -z "$(cancelled_ids)" ]] &&
    [[ "$(line_count 'no run for aaaaaaa yet; nothing cancelled' "$TEST_DIR/stdout")" == 1 ]] &&
    [[ "$(awk '$1 == "run" && $2 == "list" && /--workflow/ {n++} END {print n+0}' "$GH_CALLS")" == 0 ]]; then
    printf 'PASS missing successor run leaves old runs and explains why\n'
  else
    printf 'FAIL missing successor run leaves old runs and explains why\n'
    failed=1
  fi
fi
unset GH_NO_SUCCESSOR

failed_dispatch_reported() {
  grep -Fxq 'CI run 101 completed with failure (https://github.com/example/repo/actions/runs/101)' "$TEST_DIR/stderr"
}

dispatch_cancellation_warns() {
  [[ "$(cancelled_ids)" == $'102\n103' ]] &&
    grep -Fxq 'warning: failed to cancel run 102' "$TEST_DIR/stderr" &&
    grep -Fxq 'cancelled run 103 (ccccccc)' "$TEST_DIR/stdout"
}

dispatch_leaves_old_runs() {
  [[ -z "$(cancelled_ids)" ]]
}

export GH_SUCCESSOR_FAILED=1 CI_PUSH_REPO=github.com/example/repo
dispatch_args=(--follow-published aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa pushed ci.yml workflow_dispatch 'dispatched 0')
case_name='failed dispatch cancels prior runs and reports failure'
if CANCEL_PRIOR=true run_case "$case_name" 1 "${dispatch_args[@]}"; then
  check_case "$case_name" both_older_runs_cancelled
  check_case 'failed dispatch identifies its run' failed_dispatch_reported
fi

case_name='failed dispatch keeps its status after a cancellation warning'
if CANCEL_PRIOR=true GH_FAIL_CANCEL_ID=102 run_case "$case_name" 1 "${dispatch_args[@]}"; then
  check_case "$case_name" dispatch_cancellation_warns
fi

case_name='failed dispatch leaves prior runs when cancellation is disabled'
if CANCEL_PRIOR=false run_case "$case_name" 1 "${dispatch_args[@]}"; then
  check_case "$case_name" dispatch_leaves_old_runs
fi

tested=cccccccccccccccccccccccccccccccccccccccc
: > "$GIT_CALLS"
: > "$GH_CALLS"
if GIT_DEFAULT_BRANCH=origin/main PUSH_TARGET_BRANCH=ci-test-branch \
    bash "$TEST_DIR/scripts/push_direct.sh" --publish-only "$tested" > "$TEST_DIR/stdout" 2> "$TEST_DIR/stderr" &&
    [[ "$(line_count "push origin $tested:refs/heads/ci-test-branch " "$GIT_CALLS")" == 1 ]] &&
    [[ "$(line_count 'rev-parse HEAD ' "$GIT_CALLS")" == 0 ]] &&
    [[ "$(tail -n 1 "$TEST_DIR/stdout")" == "$tested pushed" ]]; then
  printf 'PASS publish-only pins the non-default branch push to the supplied SHA\n'
else
  printf 'FAIL publish-only pins the non-default branch push to the supplied SHA\n'
  cat "$GIT_CALLS" "$TEST_DIR/stdout" "$TEST_DIR/stderr"
  failed=1
fi

exit "$failed"
