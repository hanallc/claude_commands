#!/bin/zsh
# Require a live placement for agent and SSH calls.
emulate -L zsh
zmodload zsh/datetime
export LC_ALL=C
program=${0:t}
invoking_bin=${0:a:h:A}
gate_source=${0:A}
gate_bin=${gate_source:h}/bin
nextest_bin=${gate_source:h}/nextest-bin
# Find the real program before adding any gate links to PATH.
real_path=()
for folder in "${path[@]}"; do
    real_folder=${folder:A}
    [[ $real_folder == $invoking_bin || $real_folder == $gate_bin || $real_folder == $nextest_bin ]] \
        || real_path+=("$folder")
done
path=("${real_path[@]}")
real_program=$(whence -p "$program")
[[ -n $real_program ]] || { print -u2 -r -- "mac cargo gate: real $program not found on PATH"; exit 127 }
agent_call=0
[[ ! -v CLAUDECODE && ! -v CODEX_HOME && ! -v SSH_CONNECTION && ! -v SSH_CLIENT ]] || agent_call=1
umask 077
fifo=''
label=user
if (( agent_call )); then
    source ${gate_source:h}/../../lib/conf.sh
    # A refusal about the placement names the queue route; a failed check names its cause only.
    refuse() {
        local refusal=$(conf_value mac_gate refusal_exit_code) || exit 2
        print -u2 -r -- "mac cargo gate refused: $1${2-; run it through mac_queue_run.py}"
        [[ -z $fifo ]] || rm -f -- "$fifo"
        exit $refusal
    }
    placement=${MAC_PLACEMENT:-}
    [[ $placement == ?*/?* ]] || refuse 'no placement'
    episode=${placement%/*}
    attempt=${placement##*/}
    [[ -n $episode && -n $attempt && $episode != *[^A-Za-z0-9._-]* && $attempt != *[^A-Za-z0-9._-]* ]] || refuse 'no placement'
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
    check_placement() {
        mkdir -p "$state/checks" || exit 2
        local cache_file=$state/checks/${placement//[^[:alnum:]_.-]/_}.live
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
    }
    label="placement $placement"

    zmodload zsh/stat || exit 2
    worker_budget=$(conf_value mac_gate worker_budget) || exit 2
    [[ ${worker_budget#+} == <-> && ${#worker_budget} -le 15 ]] && (( worker_budget > 0 )) \
        || refuse 'invalid worker_budget' ''
    worker_budget=$(( worker_budget ))
    nextest_width() {
        local requested=$1 digits width relative=0
        case $requested in
            num-cpus) digits=0; relative=1 ;;
            -<->) digits=${requested#-}; relative=1 ;;
            <->|+<->) digits=${requested#+} ;;
            *) refuse "invalid nextest test thread count: $requested" '' ;;
        esac
        digits=${digits#${digits%%[1-9]*}}
        (( ${#digits} <= 15 )) || refuse 'nextest test thread count is too large' ''
        width=${digits:-0}
        if (( relative )); then
            if [[ -z $logical_cpus ]]; then
                logical_cpus=$(getconf _NPROCESSORS_ONLN 2>/dev/null)
                [[ $logical_cpus == <-> && $logical_cpus != 0 ]] \
                    || refuse 'cannot read the logical CPU count' ''
            fi
            width=$(( logical_cpus - width ))
            if (( width < 1 )); then width=1; fi
        fi
        (( width > 0 )) || refuse 'nextest test thread count must be positive' ''
        REPLY=$(( width > worker_budget ? worker_budget : width ))
    }

    cap_nextest_threads() {
        setopt localoptions extended_glob
        local argument prefix logical_cpus=''
        # Nextest 0.9.143 declares these short options with values in cargo_cli.rs
        # (p, F, Z), dispatch/common.rs (P), and dispatch/core/{filter,list,run}.rs (E, T, R).
        local switches='[rvqhV]#' value_option='[pFZPETR]'
        nextest_width "${NEXTEST_TEST_THREADS-$worker_budget}"
        export NEXTEST_TEST_THREADS=$REPLY
        nextest_arguments=()
        while (( $# )); do
            argument=$1
            shift
            case $argument in
                --) nextest_arguments+=("$argument" "$@"); break ;;
                --jobs|--test-threads|-${~switches}j)
                    (( $# )) || refuse "missing nextest test thread count after $argument" ''
                    nextest_width "$1"
                    nextest_arguments+=("$argument" "$REPLY")
                    shift
                    ;;
                --jobs=*|--test-threads=*|-${~switches}j?*)
                    prefix=${argument%%j*}j
                    if [[ $argument == --* || $argument == "$prefix="* ]]; then prefix=${argument%%=*}=; fi
                    nextest_width "${argument#"$prefix"}"
                    nextest_arguments+=("$prefix$REPLY")
                    ;;
                -${~switches}${~value_option}*) nextest_arguments+=("$argument") ;;
                -[^-]*j*) refuse "cannot read nextest test thread option: $argument" '' ;;
                *) nextest_arguments+=("$argument") ;;
            esac
        done
    }
    if [[ $program == cargo-nextest || $program == cargo-ntr ]]; then
        cap_nextest_threads "$@"
        set -- "${nextest_arguments[@]}"
    fi
    check_relative_cargo_home() {
        setopt localoptions extended_glob
        local argument takes_value=0
        for argument in "$@"; do
            if (( takes_value )); then
                takes_value=0
                continue
            fi
            case $argument in
                --) break ;;
                -[Vvqh]#C*) refuse 'Cargo -C requires an absolute CARGO_HOME' '' ;;
                -[Vvqh]#Z|--config|--color|--explain) takes_value=1 ;;
                -*|+*) ;;
                *) break ;;
            esac
        done
    }
    if [[ $program == cargo ]]; then
        cargo_home=${CARGO_HOME:-$HOME/.cargo}
        if [[ $cargo_home != /* ]]; then
            check_relative_cargo_home "$@"
            cargo_cwd=$(pwd -P) || refuse 'cannot resolve the working directory for Cargo home' ''
            cargo_home="${cargo_cwd%/}/$cargo_home"
            export CARGO_HOME=$cargo_home
        fi
    fi
fi
if ! registration=$("${gate_source:h}/../../lib/py" -B "${gate_source:h}/register.py" "$$" "${MAC_GATE_RECORD:-}" "$label"); then
    if (( agent_call )); then
        refuse "${registration:-cannot register the build root}" ''
    fi
    print -u2 -r -- "mac cargo gate: ${registration:-cannot register the build root}"
    exit 2
fi
IFS=$'\t' read -r state root_key role <<< "$registration"
if [[ $registration == *$'\n'* || $state != /* || $root_key != <->-<-> || $role != (root|nested) ]]; then
    (( ! agent_call )) || refuse 'invalid build root registration' ''
    exit 2
fi
if [[ $role == root ]]; then
    export MAC_GATE_RECORD=$root_key
fi
if (( agent_call )); then
    check_placement
    if [[ $role == root ]]; then
        for file in "$state/checks/"*.live(N); do
            IFS=$'\t' read -r checked cached < "$file"
            if [[ $checked != <->(|.<->) ]] || (( EPOCHREALTIME - checked >= recheck )); then
                rm -f -- "$file"
            fi
        done
        limit=$(conf_value mac_gate placement_check_timeout_seconds) || exit 2
        for file in "$state/"answer-*(N); do
            zstat -A modified +mtime "$file" 2>/dev/null || continue
            (( EPOCHREALTIME - modified[1] <= limit + 1 )) || rm -f -- "$file"
        done
    fi
    jobserver_identity() {
        local -A read_info write_info
        zstat -s -H read_info -f "$pool_read" 2>/dev/null &&
            zstat -s -H write_info -f "$pool_write" 2>/dev/null &&
            [[ $read_info[mode] == p* && $write_info[mode] == p* &&
               $read_info[device] == $write_info[device] && $read_info[inode] == $write_info[inode] ]] \
            || return 1
        REPLY="$read_info[device]:$read_info[inode]"
    }
    if [[ $role == nested ]]; then
        # Reuse the pool only for a verified ancestor with the same placement.
        inherited_pool=${MAC_GATE_JOBSERVER:-}
        [[ $inherited_pool == <->,<->:<->:<-> ]] || refuse 'ancestor jobserver is missing' ''
        pool_read=${inherited_pool%%,*}
        pool_write=${${inherited_pool#*,}%%:*}
        jobserver_identity && [[ $REPLY == ${inherited_pool#*:} ]] \
            || refuse 'ancestor jobserver descriptors are unavailable' ''
    else
        pool_path=$state/jobserver-$root_key
        mkfifo "$pool_path" || refuse 'cannot create the jobserver' ''
        fifo=$pool_path
        pipe_buf=$(getconf PIPE_BUF "$fifo" 2>/dev/null)
        if [[ $pipe_buf != <-> ]] || (( worker_budget - 1 > pipe_buf )); then
            refuse 'worker_budget exceeds the jobserver pipe limit' ''
        fi
        # Open both ends before separate opens so neither end blocks at startup.
        exec {pool_hold}<>"$fifo" && exec {pool_read}<"$fifo" && exec {pool_write}>"$fifo" \
            || refuse 'cannot open the jobserver' ''
        rm -f -- "$fifo" || refuse 'cannot unlink the jobserver' ''
        fifo=''
        exec {pool_hold}>&- || refuse 'cannot close the jobserver setup descriptor' ''
        jobserver_identity || refuse 'cannot identify the jobserver' ''
        # Cargo and rustc already own one implicit slot.
        printf '%*s' "$(( worker_budget - 1 ))" '' >&$pool_write || refuse 'cannot seed the jobserver' ''
        export MAC_GATE_JOBSERVER="$pool_read,$pool_write:$REPLY"
    fi
    unset MAKEFLAGS MFLAGS
    export CARGO_MAKEFLAGS="-j --jobserver-fds=$pool_read,$pool_write --jobserver-auth=$pool_read,$pool_write"
    if [[ $program == cargo ]]; then
        # Cargo resolves aliases before it searches PATH for nextest.
        # Keep Cargo from inserting its own bin ahead of the gate links.
        cargo_home_bin=${cargo_home%/}/bin
        [[ $cargo_home_bin != *:* && $nextest_bin != *:* && -x $nextest_bin/cargo-nextest && -x $nextest_bin/cargo-ntr ]] \
            || refuse 'cannot route Cargo nextest through the gate' ''
        if (( ! ${path[(Ie)$cargo_home_bin]} )); then
            path=("$cargo_home_bin" "${path[@]}")
        fi
        path=("$nextest_bin" "${path[@]}")
    fi
fi
exec "$real_program" "$@"
