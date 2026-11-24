#!/usr/bin/env zsh
# Build and run a commit on the Mac in a numbered copy.
# Usage: mac_run.sh <repo dir> <sha> [seconds]
# Exit: 0 passed, 3 Mac unreachable, 4 setup or cleanup failed, 5 wrong tree,
# 6 build failed, 7 Hana exited early, 8 an example failed, 9 Mac unavailable.
set -o pipefail
lib=${0:A:h}/../lib
if [[ $1 == cleanup-copy ]]; then
  checkout=$2
  launch=$3
  if confirmation=$(rm -f -- "$launch" 2>&1 &&
      "$lib/py" "$lib/../hana_instances/hana_instances.py" stop-checkout "$checkout" 2>&1) &&
     [[ ${confirmation##*$'\n'} == 'hana-instances: checkout clear' ]]; then
    rm -f -- "${COPIES_KEEP_FILE:?}" || exit 4
    exit 0
  fi
  reason="mac_run: checkout clearance unconfirmed for $checkout${confirmation:+: $confirmation}"
  print -r -- "$reason" >> "${COPIES_KEEP_FILE:?}"
  print -u2 -r -- "$reason"
  exit 4
fi
callback_mode=''
argv_file=''
if [[ $1 == prepare || $1 == cleanup ]]; then
  callback_mode=$1
  argv_file=$2
  shift 2
fi
if [[ $callback_mode == cleanup ]]; then
  if [[ -r $argv_file.bundle ]]; then
    bundle=$(< "$argv_file.bundle")
    rm -f -- "$bundle"
    exit $?
  fi
  exit 0
fi
purpose=${HANA_TEST_PURPOSE:-}
owner=${HANA_TEST_OWNER:-${CODEX_THREAD_ID:-${CLAUDE_CODE_SESSION_ID:-}}}
[[ -n ${purpose//[[:space:]]/} ]] || { print -u2 'mac_run: set HANA_TEST_PURPOSE to the feature under test'; exit 2 }
# zsh's q flag quotes labels for the generated command without evaluating them.
purpose_arg=${(q)purpose}
owner_arg=${(q)owner}
source "$lib/conf.sh"
repo=$1 sha=$2
hold=${3:-$(conf_value production mac_run_hold_seconds)} || exit 2
host=$(conf_value production mac_run_host) || exit 2
port=$(conf_value production mac_run_port) || exit 2
log=$(conf_value production mac_run_log) || exit 2
launch=$(conf_value production mac_run_launch) || exit 2
status_file=$(conf_value production mac_run_status) || exit 2
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
source_repo=$(conf_value production mac_run_source) || exit 2
source_arg=${(q)source_repo}
[[ $source_repo != \~/* ]] || source_arg="\$HOME/${(q)${source_repo#\~/}}"

if [[ -z $callback_mode ]]; then
  ssh -o BatchMode=yes $host true 2>/dev/null || { print "mac unreachable"; exit 3 }
  exec "$lib/py" "$lib/../mac_test/mac_queue_run.py" --what "mac run $sha" --kind run --package hana \
    --worktree "$repo" --cwd '' --lifecycle-script "${0:A}" -- "$repo" "$sha" "$hold"
fi
host=$MAC_QUEUE_HOST
sha=$(git -C "$repo" rev-parse --verify "$sha^{commit}") || exit 2
copy_owner=$("$lib/py" -c 'import sys; from pathlib import Path; from scripts.copies import copies; print(copies.source_owner(Path(sys.argv[2])))' "$lib/../copies/copies.py" "$repo") || exit 4

# Prefer the exact commit when the Mac source already has it.
base=$(ssh -o BatchMode=yes "$host" "git -C $source_arg rev-parse --verify $sha^{commit} 2>/dev/null || git -C $source_arg rev-parse --verify HEAD") || exit 4
[[ $base =~ '^[0-9a-f]{40}$' ]] || {
  print -u2 "mac_run: the Mac source returned no commit"
  exit 4
}
ref=''
if [[ $base != $sha ]]; then
  ref=refs/mac-run/tip
  bundle=$(mktemp --suffix=.bundle) || exit 4
  print -r -- "$bundle" > "$argv_file.bundle"
  git -C "$repo" update-ref $ref $sha || exit 4
  if git -C "$repo" cat-file -e "$base^{commit}" 2>/dev/null; then
    git -C "$repo" bundle create -q "$bundle" $ref "^$base"
  else
    git -C "$repo" bundle create -q "$bundle" $ref
  fi
  bundle_exit=$?
  git -C "$repo" update-ref -d $ref
  (( bundle_exit == 0 )) || exit 4
  scp -q "$bundle" "$host:$remote_bundle" || exit 4
fi
remote_work=$(cat <<EOF
checkout=\$PWD
head=\$(git rev-parse HEAD)
print "HEAD=\$head"
[[ \$head == $sha ]] || { print "wrong tree: wanted $sha"; exit 5 }
MACOSX_DEPLOYMENT_TARGET=$deployment_target cargo build -q -p hana || { print 'build failed'; exit 6; }
# Terminal holds the camera grant. The launch file records the child status.
rm -f $status_arg
printf '#!/bin/zsh\nexport MAC_PLACEMENT=%q\n' "\$MAC_PLACEMENT" > $launch_arg
printf 'export checkout=%q; cd %q\\n' "\$checkout" "\$checkout" >> $launch_arg
cat >> $launch_arg <<'HANA_LAUNCH'
HANA_TEST_PURPOSE=$purpose_arg HANA_TEST_OWNER=$owner_arg BEVY_ASSET_ROOT="\$checkout/crates/hana" BRP_EXTRAS_PORT=$port perl -e 'alarm shift; exec @ARGV' $hold "\$checkout/target/debug/hana" > $log_arg 2>&1
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
  BEVY_ASSET_ROOT="\$checkout/crates/\$parts[1]" BRP_EXTRAS_PORT=$port perl -e 'alarm shift; exec @ARGV' $example_hold "\$checkout/target/debug/examples/\$parts[2]" > \$example_log 2>&1
  example_exit=\$?
  pkill -f "\$checkout"/'[t]arget/debug/examples/'\$parts[2]
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
# Use the installed helpers on the Mac. Only the smoke commands come from this callback.
export MAC_RUN_REMOTE_WORK=$remote_work MAC_RUN_SOURCE=$source_repo MAC_RUN_SHA=$sha MAC_RUN_COPY_OWNER=$copy_owner
export MAC_RUN_LAUNCH=$launch MAC_RUN_BUNDLE=$remote_bundle MAC_RUN_BUNDLE_REF=$ref
"$lib/py" -c '
import json, os, sys
from pathlib import Path
from shlex import quote

env = os.environ
source = env["MAC_RUN_SOURCE"]
source = "$HOME/" + quote(source[2:]) if source.startswith("~/") else quote(source)
py = """ "$HOME/.claude/scripts/lib/py" """
copies = """ "$HOME/.claude/scripts/copies/copies.py" """
runner = """ "$HOME/.claude/scripts/mac_test/mac_queue_run.py" """
cleanup = """exec "$HOME/.claude/scripts/production/mac_run.sh" cleanup-copy "$1" """ + quote(env["MAC_RUN_LAUNCH"])
work = env["MAC_RUN_REMOTE_WORK"]
prepare = ""
if ref := env["MAC_RUN_BUNDLE_REF"]:
    bundle = quote(env["MAC_RUN_BUNDLE"])
    prepare = (
        f"git -C {source} fetch -q --no-tags --no-write-fetch-head -- {bundle} {quote(ref)}\n"
        + "status=$?\n"
        + f"rm -f -- {bundle}\n"
        + "(( status == 0 )) || exit 5\n"
    )
command = [
    "/bin/zsh", "-c",
    prepare + f"exec{py}{copies}run --repo {source} --owner " + quote(env["MAC_RUN_COPY_OWNER"])
    + " --commit " + env["MAC_RUN_SHA"] + f" --{py}{runner}smoke-work " + quote(work) + " " + quote(cleanup),
]
Path(sys.argv[1]).write_text(json.dumps(command))
' "$argv_file"
