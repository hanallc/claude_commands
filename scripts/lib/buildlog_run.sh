# shellcheck shell=bash

# Pass the command that reads the owner's process start.
buildlog_initialize_run() {
    [[ "${OSTYPE:-}" == linux* && -z "${BUILDLOG_RUN+x}" ]] || return 0
    local run_start
    run_start="$("$@" start "$$")" || return $?
    if [[ -n "$run_start" ]]; then
        export BUILDLOG_RUN="$$:$run_start"
    fi
}
