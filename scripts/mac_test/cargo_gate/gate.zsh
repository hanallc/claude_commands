#!/bin/zsh
# Require a live placement for agent and SSH calls.
emulate -L zsh
zmodload zsh/datetime
export LC_ALL=C
program=${0:t}
gate_bin=${0:a:h:A}
gate_source=${0:A}
# Remove the gate folder while preserving every other PATH entry.
real_path=()
for folder in "${path[@]}"; do
    [[ ${folder:A} == $gate_bin ]] || real_path+=("$folder")
done
path=("${real_path[@]}")
real_program=$(whence -p "$program")
[[ -n $real_program ]] || { print -u2 -r -- "mac cargo gate: real $program not found on PATH"; exit 127 }
agent_call=0
[[ ! -v CLAUDECODE && ! -v CODEX_HOME && ! -v SSH_CONNECTION && ! -v SSH_CLIENT ]] || agent_call=1
state=$HOME/.local/state/mac-gate
umask 077
mkdir -p "$state/records" || exit 2
process_start() {
    local stamp=$(ps -p "$1" -o lstart= 2>/dev/null) || return 1
    print -r -- "${(j: :)${=stamp}}"
}
label=user
if (( agent_call )); then
    source ${gate_source:h}/../../lib/conf.sh
    # A refusal about the placement names the queue route; a failed check names its cause only.
    refuse() {
        local refusal=$(conf_value mac_gate refusal_exit_code) || exit 2
        print -u2 -r -- "mac cargo gate refused: $1${2-; run it through mac_queue_run.py}"
        exit $refusal
    }
    placement=${MAC_PLACEMENT:-}
    [[ $placement == ?*/?* ]] || refuse 'no placement'
    episode=${placement%/*}
    attempt=${placement##*/}
    [[ -n $episode && -n $attempt && $episode != *[^A-Za-z0-9._-]* && $attempt != *[^A-Za-z0-9._-]* ]] || refuse 'no placement'
    mkdir -p "$state/checks" || exit 2
    recheck=$(conf_value mac_gate placement_recheck_seconds) || exit 2
    parse_answer() {
        local answer_text=$1 answer_status=$2
        local -a fields
        fields=("${(@ps:\t:)answer_text}")
        [[ $answer_text != *$'\n'* ]] || return 1
        (( $#fields == 6 )) || return 1
        case $answer_status in
            0) [[ $fields[1] == current && $fields[2] == mac ]] || return 1 ;;
            1) [[ $fields[1] == closed || $fields[1] == unknown ]] || return 1
               reason=$fields[6]; [[ -n $reason ]] || return 1 ;;
            *) return 1 ;;
        esac
    }
    cache_file=$state/checks/${placement//[^[:alnum:]_.-]/_}.live
    checked=0 cached=''
    if [[ -r $cache_file ]]; then
        IFS=$'\t' read -r checked cached < "$cache_file"
    fi
    if [[ $cached != $placement ]] || (( EPOCHREALTIME - checked >= recheck )); then
        check_command=$(conf_value mac_gate placement_check_command) || exit 2
        limit=$(conf_value mac_gate placement_check_timeout_seconds) || exit 2
        answer=$state/answer-$$-$EPOCHREALTIME-$RANDOM
        timed_out=$answer.timeout
        errors=$answer.errors
        # No signal handler: the check and its timer share this process group, so a
        # group signal ends them; a later call removes answer files left past the limit.
        /bin/zsh -c "$check_command --episode ${(q)episode} --attempt ${(q)attempt}" > "$answer" 2> "$errors" &
        check_pid=$!
        ( zmodload zsh/zselect; zselect -t $(( limit * 100 ));
          print timeout > "$timed_out"; kill -TERM "$check_pid" 2>/dev/null ) </dev/null >/dev/null 2>&1 &
        timer_pid=$!
        wait "$check_pid"
        check_status=$?
        kill -TERM "$timer_pid" 2>/dev/null
        wait "$timer_pid" 2>/dev/null
        reason='' timer_fired=0 newline=$'\n'
        [[ ! -e $timed_out ]] || timer_fired=1
        answer_text=$(< "$answer")
        error_text=$(< "$errors")
        rm -f -- "$answer" "$timed_out" "$errors"
        if ! parse_answer "$answer_text" "$check_status"; then
            (( ! timer_fired )) || refuse "no answer in ${limit}s" ''
            (( check_status > 1 )) || refuse 'invalid placement answer' ''
            refuse "placement check failed with status $check_status${error_text:+: ${error_text##*$newline}}" ''
        fi
        [[ -z $reason ]] || refuse "$reason"
        print -r -- "$EPOCHREALTIME"$'\t'"$placement" > "$cache_file.$$"
        mv -f -- "$cache_file.$$" "$cache_file"
    fi
    label="placement $placement"
fi
# Verify both the ancestor relation and the recorded process start time.
ancestor=${MAC_GATE_RECORD:-}
recorded=0
if [[ $ancestor == <-> && -r $state/records/$ancestor ]]; then
    IFS=$'\t' read -r recorded_pid recorded_start recorded_label < "$state/records/$ancestor"
    cursor=$PPID
    while [[ $cursor == <-> && $cursor != 0 ]]; do
        if [[ $cursor == $recorded_pid && $(process_start "$cursor") == $recorded_start ]]; then
            recorded=1
            break
        fi
        parent=$(ps -p "$cursor" -o ppid= 2>/dev/null)
        cursor=${parent//[[:space:]]/}
    done
fi
if (( ! recorded )); then
    # Remove expired state only when this call writes a record.
    if (( agent_call )); then
        for file in "$state/checks/"*.live(N); do
            IFS=$'\t' read -r checked cached < "$file"
            if [[ $checked != <->(|.<->) ]] || (( EPOCHREALTIME - checked >= recheck )); then
                rm -f -- "$file"
            fi
        done
        zmodload zsh/stat
        limit=$(conf_value mac_gate placement_check_timeout_seconds) || exit 2
        for file in "$state/"answer-*(N); do
            zstat -A modified +mtime "$file" 2>/dev/null || continue
            (( EPOCHREALTIME - modified[1] <= limit + 1 )) || rm -f -- "$file"
        done
        for file in "$state/records/"<->(N); do
            IFS=$'\t' read -r recorded_pid recorded_start recorded_label < "$file"
            current_start=$(process_start "$recorded_pid")
            [[ -n $current_start && $current_start == $recorded_start ]] || rm -f -- "$file"
        done
    fi
    started=$(process_start $$)
    print -r -- "$$"$'\t'"$started"$'\t'"$label" > "$state/records/$$.new"
    mv -f -- "$state/records/$$.new" "$state/records/$$"
    export MAC_GATE_RECORD=$$
fi
exec "$real_program" "$@"
