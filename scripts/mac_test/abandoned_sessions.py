"""Generate the shared removal command for abandoned compiler sessions."""

import shlex

from scripts.mac_test.floor_claims import MIRROR_LEASE_FDS


def removal_script(target: str, minutes: int) -> str:
    """Return a silent removal group for the caller's quoted build folder."""
    remove_sessions = (
        'freed_kib=0; for session do '
        'size=$(du -sk "$session") || size=; '
        'rm -rf -- "$session" "${session%-working}.lock" || :; '
        'if [ ! -e "$session" ] && [ ! -L "$session" ] && [ -n "$size" ]; then '
        'kib=${size%%[[:space:]]*}; freed_kib=$((freed_kib + kib)); '
        'fi; done; '
        'if [ "$freed_kib" -gt 0 ]; then printf "%s\\n" "$freed_kib"; fi'
    )
    target_env = "ABANDONED_SESSION_TARGET"
    recorder = (
        'import os; os.closerange(3, os.sysconf("SC_OPEN_MAX")); '
        'import sys, time; from scripts.lint.sweep import record_sweep_removal; '
        'freed_bytes = sum(map(int, sys.argv[1].split())) * 1024; '
        f'saved = record_sweep_removal(os.environ["{target_env}"], '
        '"abandoned session", freed_bytes, "partial", time.time()); '
        'sys.exit(0 if saved else 1)'
    )
    # Use /bin/sh because zsh rejects this redirection for descriptors above 9.
    prepare_recorder = (
        'cd / || exit; '
        f'lease_fds="${{{MIRROR_LEASE_FDS}:-}},"; '
        'while [ -n "$lease_fds" ]; do '
        'fd=${lease_fds%%,*}; lease_fds=${lease_fds#*,}; '
        'case "$fd" in ""|*[!0-9]*) ;; *) eval "exec $fd>&-" ;; esac; '
        'done; exec "$@"'
    )
    return (
        f"{{ {{ abandoned_session_kib=$(find {target} -maxdepth 5 -type d "
        + r"\( -name deps -o -name build -o -name .fingerprint -o -name examples \) -prune -o "
        + f"-type d -name 's-*-working' -mmin +{minutes} -prune "
        + f"-exec sh -c {shlex.quote(remove_sessions)} sh {{}} +); "
        + 'if [ -n "$abandoned_session_kib" ]; then '
        + f'{target_env}={target} /bin/sh -c {shlex.quote(prepare_recorder)} sh '
        + f'nohup "$HOME/.claude/scripts/lib/py" -B -c {shlex.quote(recorder)} '
        + '"$abandoned_session_kib" </dev/null >/dev/null 2>&1 & '
        + 'fi; } >/dev/null 2>&1 || :; }'
    )
