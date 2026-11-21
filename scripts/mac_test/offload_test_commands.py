"""Shell stand-ins keep argument records without starting Python per command."""

import shlex
from pathlib import Path
import subprocess
from typing import Literal

from scripts.lib.checkout_test_support import fixture_shim_source, python_tool_source
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.mac_test import offload

_popen = subprocess.Popen


def capture_fence_query(command: str, *, environment: dict[str, str] | None = None) -> offload.CommandOutput:
    """Run the responder query in the fixture's private home."""
    with _popen(["/bin/bash", "-c", command], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, env=environment) as process:
        try:
            stdout, stderr = process.communicate(timeout=TEST_HANG_GUARD_SECS)
        except subprocess.TimeoutExpired:
            process.kill()
            raise
        if process.returncode != 0:
            raise subprocess.CalledProcessError(process.returncode, command, stdout, stderr)
        return offload.CommandOutput(process.returncode, stdout, stderr)


def prepare_probe_fence(command: str) -> str:
    """Run the preparation prefix before the SSH fixture supplies its probe result."""
    prepare, separator, probe = command.partition(" || exit 75\n")
    if separator and prepare.startswith("python3 -c ") and "def fence_record():" in prepare:
        _ = capture_fence_query(prepare)
        return probe
    return command


JSON_SHELL = r'''
json_string() {
    local value=$1
    value=${value//\\/\\\\}
    value=${value//\"/\\\"}
    value=${value//$'\n'/\\n}
    value=${value//$'\r'/\\r}
    value=${value//$'\t'/\\t}
    value=${value//$'\b'/\\b}
    value=${value//$'\f'/\\f}
    printf '"%s"' "$value"
}
json_args() {
    local separator='' argument
    printf '['
    for argument in "$@"; do
        printf '%s' "$separator"
        json_string "$argument"
        separator=,
    done
    printf ']'
}
record_command() {
    printf '{"command":'
    json_string "${0##*/}"
    printf ',"args":'
    json_args "$@"
}
'''


def write_shell(path: Path, source: str) -> None:
    """Bash builtins handle JSON escaping and timed FIFO reads on Linux and macOS."""
    _ = path.write_text("#!/bin/bash\n" + JSON_SHELL + source)
    path.chmod(0o755)


def configure_nextest_trim(shim: Path, trim: int | Literal["run"]) -> None:
    """Keep the log writer active for each trim outcome."""
    original = Path(__file__).resolve().with_name("nextest_log.py")
    interpreter, _, body = fixture_shim_source().partition("\n")
    trim_command = f"exit {trim}" if isinstance(trim, int) else ":"
    source = (interpreter + '\nif [ "${2##*/}" = nextest_log.py ]; then\n'
              + f'    shift 2\n    set -- -B {shlex.quote(str(original))} "$@"\n'
              + f'else\n    {trim_command}\nfi\n' + body)
    _ = shim.write_text(source)
    shim.chmod(0o755)


def install_nextest_log_writer(home: Path) -> Path:
    """Make the checkout's log writer available in a private home."""
    script = home / ".claude/scripts/mac_test/nextest_log.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.symlink_to(Path(__file__).resolve().with_name("nextest_log.py"))
    shim = home / ".claude/scripts/lib/py"
    shim.parent.mkdir(parents=True, exist_ok=True)
    configure_nextest_trim(shim, 0)
    return shim


COMMAND_STUB = r'''
root=$MAC_TEST_STUB_DIR
source "$root/fixture.sh"
command=${0##*/}
if [[ $command == ssh && ${!#} == *'print("dispatch_fence_host=" +'* ]]; then
    IFS= read -r identity < "$root/fence-host"
    printf '%s\n' "$identity"
    exit 0
fi
if [[ $command == systemd-run ]]; then
    json_args "$@" >> "$root/systemd-run.jsonl"
    printf '\n' >> "$root/systemd-run.jsonl"
    exit 0
fi
{
    record_command "$@"
    claim_file=$MAC_TEST_STATE_DIR/claims/hana.json
    if [[ -f $claim_file ]]; then
        IFS= read -r -d '' claim < "$claim_file" || :
        printf ',"claimed":true,"claim":%s' "${claim//$'\n'/}"
    else
        printf ',"claimed":false'
    fi
    if [[ $command == rsync ]]; then
        for argument in "$@"; do
            if [[ $argument == --exclude-from=* ]]; then
                IFS= read -r -d '' exclude < "${argument#*=}" || :
                printf ',"exclude":'
                json_string "$exclude"
                break
            fi
        done
    fi
    printf '}\n'
} >> "$root/events.jsonl"

await_release() {
    # Open both ends so timeout covers the read, including a missing writer.
    IFS= read -r -t "$TEST_HANG_GUARD_SECS" reply <> "$root/$1" || exit 98
}
if [[ $command == rsync ]]; then
    if [[ ${*: -2:1} == *:*/ ]]; then
        printf 'started\n' > "$root/pull-started"
        if (( ${hold_pull:-0} )); then await_release continue-copy; fi
        destination=${!#}
        mkdir -p -- "$destination"
        printf '%s' "${fetch_contents:-image bytes}" > "$destination/shot.png"
        exit "${pull_status:-0}"
    fi
    printf 'started\n' > "$root/copy-started"
    if (( hold_copy )); then await_release continue-copy; fi
    exit "$rsync_status"
fi
if [[ $command != ssh ]]; then
    printf 'called\n' > "$root/$command-called"
    exit 99
fi
remote=${!#}
if [[ $remote == *dispatch_start_settled=* && $remote == *"printf 'leftovers=0"* &&
      $remote != *'select_leftovers()'* ]]; then
    /bin/bash -c "$remote"
    exit $?
fi
if [[ $remote == *sweep.sweep_workspace* && $remote != *'~/.claude/scripts/lint/lint '* ]]; then
    printf 'mac_exit=0\n%s\n' "$run_cleanup_line"
    exit 0
fi
if [[ $remote == *'sweep.py --floor-only --host mac' ]]; then
    printf 'called\n' >> "$root/sweeps"
    if [[ -p $root/continue-sweep ]]; then await_release continue-sweep; fi
    if [[ -f $root/sweep-failure ]]; then
        cat "$root/sweep-failure" >&2
        exit 7
    fi
    printf 'done\n' > "$root/sweep-finished"
    exit 0
fi
if [[ $remote == *fetch_kib=* ]]; then
    if (( ${hold_fetch_probe:-0} )); then
        printf 'started\n' > "$root/fetch-probe-started"
        await_release continue-copy
    fi
    printf '%s\n' "${fetch_answer:-fetch_kib=4
rc=0}"
    exit "${fetch_probe_status:-0}"
fi
if [[ $remote == *'select_leftovers()'* && $remote == *lsof* ]]; then
    printf 'called\n' > "$root/cleanup-called"
    if (( hold_cleanup )); then
        printf 'started\n' > "$root/cleanup-started"
        await_release continue-cleanup
    fi
    if [[ -n $selector_shell ]]; then
        printf -v checks 'export PATH=%q:"$PATH"\n' "$root/selectors"
        for name in pgrep lsof sleep; do
            printf -v check '[ "$(command -v %s)" = %q ] || exit 99\n' "$name" "$root/selectors/$name"
            checks+=$check
        done
        if [[ $selector_shell == sh ]]; then shell=(/bin/sh); else shell=(zsh -f); fi
        "${shell[@]}" -c "$checks$remote" > "$root/selector-answer"
        status=$?
        while IFS= read -r line; do printf '%s\n' "$line"; done < "$root/selector-answer"
        exit "$status"
    fi
    if [[ -n $cleanup_error ]]; then printf '%s\n' "$cleanup_error" >&2; fi
    for line in "${cleanup_lines[@]}"; do printf '%s\n' "$line"; done
    exit "$cleanup_status"
fi
if [[ $remote == *'lint nextest'* || $remote == *'lint check'* || $remote == *'lint clippy'* ||
      $remote == *'lint mend-check'* || $remote == *'lint doc'* ]]; then
    if (( hold_run )); then
        printf 'mac_exit=0\nfixture acknowledgement\n'
        printf 'started\n' > "$root/run-started"
        await_release finish-run
    else
        printf 'started\n' > "$root/run-started"
    fi
    for index in "${!run_lines[@]}"; do
        if [[ ${run_streams[index]} == stdout ]]; then
            printf '%s\n' "${run_lines[index]}"
        else
            printf '%s\n' "${run_lines[index]}" >&2
        fi
        if (( index == 0 && hold_after_first_line )); then
            printf 'sent\n' > "$root/first-line-sent"
            await_release continue-run
        fi
    done
    if (( run_status_reported )); then
        printf '%s\n' "$run_status_line"
        if [[ -n $run_cleanup_line ]]; then printf '%s\n' "$run_cleanup_line"; fi
        python3 - "$claim_file" <<'SETTLE_FENCE'
''' + python_tool_source(r'''
import json
from pathlib import Path
import subprocess
import sys
from scripts.mac_test import claim_state, offload
path = Path(sys.argv[1])
claim = json.loads(path.read_text())
identity = claim_state.ClaimIdentity(claim["claim_id"], claim["episode"], claim["attempt"])
paths = claim_state.state_paths("mac")
key = claim_state.ClaimKey.for_target(paths, identity)
location = claim_state.ClaimRecordLocation(path.relative_to(paths.directory))
journal = offload.record_start_fence(key, claim, location)
if not isinstance(journal, claim_state.StartFenceJournal):
    raise SystemExit(75)
command = offload.start_fence_command(journal.fence, settle=True, preparation=journal.preparation)
raise SystemExit(subprocess.run(["/bin/bash", "-c", command], check=False).returncode)
''') + r'''
SETTLE_FENCE
    fi
    exit "$run_ssh_status"
fi
if [[ $remote == python3\ -c\ * && $remote == *'def fence_record():'* ]]; then
    python3 - "$remote" <<'PREPARE_FENCE' || exit 75
''' + python_tool_source(r'''
import sys
from scripts.mac_test.offload_test_commands import prepare_probe_fence
prepare_probe_fence(sys.argv[1])
''') + r'''
PREPARE_FENCE
fi
printf 'started\n' > "$root/probe-started"
if (( hold_probe )); then await_release continue-probe; fi
if [[ -n $probe_error ]]; then printf '%s\n' "$probe_error" >&2; fi
if [[ -f $root/sweep-finished && -f $root/free-after-sweep ]]; then
    read -r free_after < "$root/free-after-sweep"
    probe_lines=("free_gib=$free_after" 'host=Mac' 'rc=0')
fi
for line in "${probe_lines[@]}"; do printf '%s\n' "$line"; done
exit "$probe_status"
'''


SELECTOR_STUB = r'''
root=$MAC_TEST_STUB_DIR
source "$root/fixture.sh"
{ record_command "$@"; printf '}\n'; } >> "$root/selector-events.jsonl"
case ${0##*/} in
    pgrep) exit "$selector_pgrep_status" ;;
    lsof) exit "$selector_lsof_status" ;;
    sleep) exit 0 ;;
    *) exit 99 ;;
esac
'''


ACTIVITY_SELECTOR_STUB = r'''
root=$ACTIVITY_TEST_DIR
{ record_command "$@"; printf '}\n'; } >> "$root/commands.jsonl"
source "$root/${0##*/}-response.sh"
printf '%s' "$answer"
exit "$status"
'''
