#!/usr/bin/env zsh
# Run a sha on the Mac: ship it from this machine, build hana, run it, check it
# stays up for SECONDS, then stop it. User, 2026-10-04: after each green CI on
# the merge branch, when the Mac is reachable.
#
# Then it builds each demo example with the same cargo arguments you would
# type, so `cargo run` in the clone starts at once, and runs it for
# EXAMPLE_HOLD seconds. User, 2026-10-05: the Ian Hubert demo runs from this clone.
#
# Usage: mac_run.sh <repo dir> <sha> [seconds; production.conf mac_run_hold_seconds sets the default]
# Exit: 0 all stayed up; 3 Mac unreachable; 5 wrong tree; 6 build failed;
# 7 hana exited early; 8 hana stayed up but an example failed; 9 Mac blocked,
# busy with a test, or coordination state unreadable.
set -o pipefail
callback_mode=''
argv_file=''
if [[ $1 == prepare || $1 == cleanup ]]; then
  callback_mode=$1
  argv_file=$2
  shift 2
fi
purpose=${HANA_TEST_PURPOSE:-}
owner=${HANA_TEST_OWNER:-${CODEX_THREAD_ID:-${CLAUDE_CODE_SESSION_ID:-}}}
[[ -n ${purpose//[[:space:]]/} ]] || { print -u2 'mac_run: set HANA_TEST_PURPOSE to the feature under test'; exit 2 }
# zsh's q flag quotes labels for the generated command without evaluating them.
purpose_arg=${(q)purpose}
owner_arg=${(q)owner}
lib=${0:A:h}/../lib
source "$lib/conf.sh"
repo=$1 sha=$2
hold=${3:-$(conf_value production mac_run_hold_seconds)} || exit 2
host=$(conf_value production mac_run_host) || exit 2
# Each run owns a scratch task; EXIT ends it after the test processes finish.
clone=""
clone_arg=""
clone_task=""
port=$(conf_value production mac_run_port) || exit 2
log=$(conf_value production mac_run_log) || exit 2
launch=$(conf_value production mac_run_launch) || exit 2
status_file=$(conf_value production mac_run_status) || exit 2
bundle=""
example_hold=$(conf_value production mac_run_example_seconds) || exit 2
# Each entry names a crate, an example, and its optional feature.
example_settings=$(conf_value production mac_run_examples) || exit 2
deployment_target=$(conf_value production mac_run_deployment_target) || exit 2
status_grace=$(conf_value production mac_run_status_grace_seconds) || exit 2
poll_seconds=$(conf_value production mac_run_poll_seconds) || exit 2
log_lines=$(conf_value production mac_run_log_lines) || exit 2
example_log_lines=$(conf_value production mac_run_example_log_lines) || exit 2
remote_bundle=$(conf_value production mac_run_bundle) || exit 2
example_log_prefix=$(conf_value production mac_run_example_log_prefix) || exit 2
# Quote each configured path before it enters a remote shell command.
log_arg=${(q)log}
launch_arg=${(q)launch}
status_arg=${(q)status_file}
bundle_arg=${(q)remote_bundle}
source_repo=$(conf_value production mac_run_source) || exit 2
source_arg=${(q)source_repo}
[[ $source_repo != \~/* ]] || source_arg="\$HOME/${(q)${source_repo#\~/}}"

# Tailscale SSH serves the Mac, and its exit status is always 0, so a command
# whose status matters prints it as its last line and on_mac returns that.
on_mac() {
  local out code
  out=$(ssh $host "$1; echo mac_exit=\$?") || return $?
  print -rn -- "${out%mac_exit=*}"
  code=${out##*mac_exit=}
  [[ $code == <-> ]] || code=255
  return $code
}

if [[ -z $callback_mode ]]; then
  ssh -o BatchMode=yes $host true 2>/dev/null || { print "mac unreachable"; exit 3 }
  exec "$lib/py" "$lib/../mac_test/mac_queue_run.py" --what "mac run $sha" --kind run --package hana \
    --worktree "$repo" --cwd '' --lifecycle-script "${0:A}" -- "$repo" "$sha" "$hold"
fi
host=$MAC_QUEUE_HOST
copies_script=$lib/../copies/copies.py
save_state() {
  print -rl -- "clone=${(q)clone}" "clone_arg=${(q)clone_arg}" "clone_task=${(q)clone_task}" \
    "bundle=${(q)bundle}" > "$argv_file.state"
}
cleanup_mac_run() {
  [[ ! -r $argv_file.state ]] || source "$argv_file.state"
  [[ -z $bundle ]] || rm -f -- "$bundle"
  [[ -n $clone_task && -n $clone ]] || return 0
  local confirmation
  if confirmation=$(on_mac "~/.claude/scripts/lib/py ~/.claude/scripts/hana_instances/hana_instances.py stop-checkout $clone_arg") && [[ ${confirmation##*$'\n'} == 'hana-instances: checkout clear' ]]; then
    on_mac "~/.claude/scripts/lib/py ~/.claude/scripts/lib/scratch.py end --task $clone_task" >/dev/null || return 4
    on_mac "~/.claude/scripts/lib/py ~/.claude/scripts/copies/copies.py unregister-remote --host localhost --path $clone_arg" >/dev/null || return 4
    "$lib/py" $copies_script unregister-remote --host $host --path $clone || return 4
  else
    print -u2 "mac_run: process exit not confirmed; kept checkout $clone and task $clone_task${confirmation:+: $confirmation}"
    return 4
  fi
}
if [[ $callback_mode == cleanup ]]; then
  cleanup_mac_run
  exit $?
fi
save_state
sha=$(git -C $repo rev-parse --verify "$sha^{commit}") || exit 2

# The Mac cannot fetch from GitHub over ssh, so the commits go as a bundle.
# When the source already holds the commit, no bundle is needed.
clone_task=gate:mac-run-${sha}-$$
save_state
copies_script=$lib/../copies/copies.py
copy_owner=$("$lib/py" -c 'import sys; from pathlib import Path; from scripts.copies import copies; print(copies.source_owner(Path(sys.argv[2])))' $copies_script $repo) || exit 4
copy_owner_arg=${(q)copy_owner}
copy_session=${owner:-pid-$$}
copy_session_arg=${(q)copy_session}
clone=$(on_mac "~/.claude/scripts/lib/py ~/.claude/scripts/copies/copies.py clone $source_arg --task $clone_task --name hana --owner $copy_owner_arg --repository hana --session $copy_session_arg") || exit 4
[[ $clone == /* && $clone != *$'\n'* ]] || { print -u2 'mac_run: scratch clone returned no absolute checkout'; exit 4 }
clone_arg=${(q)clone}
save_state
registry_args=(--repository hana --owner $copy_owner --host $host --path $clone --session $copy_session)
mirror_lease=$("$lib/py" $copies_script register-remote "${registry_args[@]}" --shell-lease) || exit 4
print -r -- "$mirror_lease" > "$argv_file.lease"
"$lib/py" $copies_script register-remote "${registry_args[@]}" || exit 4
base=$(ssh $host "git -C $clone_arg rev-parse HEAD")
if git -C $repo merge-base --is-ancestor $sha $base 2>/dev/null; then
  checkout_command="git -C $clone_arg checkout -q --detach $sha"
else
  ref=refs/mac-run/tip
  bundle=$(mktemp --suffix=.bundle)
  save_state
  git -C $repo update-ref $ref $sha
  if git -C $repo cat-file -e "$base^{commit}" 2>/dev/null && [[ $base != $sha ]]; then
    git -C $repo bundle create -q $bundle $ref "^$base"
  else
    git -C $repo bundle create -q $bundle $ref
  fi
  bundle_exit=$?
  git -C $repo update-ref -d $ref
  (( bundle_exit == 0 )) || { rm -f $bundle; exit 4 }
  scp -q $bundle "$host:$remote_bundle" || { rm -f $bundle; exit 4 }
  rm -f $bundle

  checkout_command="git -C $clone_arg fetch -q $bundle_arg $ref && git -C $clone_arg checkout -q --detach FETCH_HEAD; rm -f $bundle_arg"
fi
remote_work=$(cat <<EOF
$checkout_command
head=\$(git -C $clone_arg rev-parse HEAD)
print "HEAD=\$head"
[[ \$head == $sha ]] || { print "wrong tree: wanted $sha"; exit 5 }
cd $clone_arg || exit 5
MACOSX_DEPLOYMENT_TARGET=$deployment_target cargo build -q -p hana || { print 'build failed'; exit 6; }
# Terminal holds the camera grant. The launch file records the child status.
rm -f $status_arg
printf '#!/bin/zsh\nexport MAC_PLACEMENT=%q\n' "\$MAC_PLACEMENT" > $launch_arg
cat >> $launch_arg <<'HANA_LAUNCH'
cd $clone_arg
HANA_TEST_PURPOSE=$purpose_arg HANA_TEST_OWNER=$owner_arg BEVY_ASSET_ROOT=$clone_arg/crates/hana BRP_EXTRAS_PORT=$port perl -e 'alarm shift; exec @ARGV' $hold $clone_arg/target/debug/hana > $log_arg 2>&1
echo \$? > $status_arg
exit 0
HANA_LAUNCH
chmod +x $launch_arg
open -a Terminal $launch_arg
for i in {1..$(( hold + status_grace ))}; do
  [[ ! -f $status_arg ]] || break
  sleep $poll_seconds
done
run_exit=\$(cat $status_arg 2>/dev/null)
rm -f $launch_arg $status_arg
if [[ \$run_exit != <-> ]]; then
  exit 7
fi
if (( run_exit == 142 )); then
  print "hana stayed up ${hold}s"
  result=0
else
  print "hana exited early: \$run_exit"
  grep -v 'CommandQueue has un-applied' $log_arg | tail -$log_lines
  result=7
fi

failed=0
example_settings=${(q)example_settings}
examples=("\${(@s:;:)example_settings}")
for entry in \$examples; do
  parts=(\${=entry})
  args=(-p \$parts[1] --example \$parts[2])
  (( \$#parts == 3 )) && args+=(--features \$parts[3])
  example_log=${(q)example_log_prefix}\$parts[2].log
  if ! MACOSX_DEPLOYMENT_TARGET=$deployment_target cargo build -q "\${args[@]}" > /dev/null 2>&1; then
    print "example \$parts[2]: build failed"
    failed=1
    continue
  fi
  BEVY_ASSET_ROOT=$clone_arg/crates/\$parts[1] BRP_EXTRAS_PORT=$port perl -e 'alarm shift; exec @ARGV' $example_hold $clone_arg/target/debug/examples/\$parts[2] > \$example_log 2>&1
  example_exit=\$?
  pkill -f $clone_arg/'[t]arget/debug/examples/'\$parts[2]
  if (( example_exit == 142 )); then
    print "example \$parts[2]: stayed up ${example_hold}s  (cargo run \$args)"
  else
    print "example \$parts[2]: exited early: \$example_exit"
    tail -$example_log_lines \$example_log
    failed=1
  fi
done

(( result == 0 && failed )) && result=8
exit \$result
EOF
)
export MAC_RUN_REMOTE_WORK=$remote_work
"$lib/py" -c 'import json,os,sys; from pathlib import Path; Path(sys.argv[1]).write_text(json.dumps(["/bin/zsh", "-c", os.environ["MAC_RUN_REMOTE_WORK"]]))' "$argv_file"
