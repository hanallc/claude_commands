#!/usr/bin/env bash
# Smoke launch for /showrunner:produce <PromoteMain/>: builds hana at a checkout
# that sits at the sha being promoted, starts it on a test port, waits until BRP
# answers, and shuts it down over BRP. Exits 0 only when every step passed.
#
# Usage: smoke_launch.sh <checkout> <sha> <log> [port]
#
# The app starts with XDG_CONFIG_HOME at an empty scratch directory, the way
# units launch Hana for screenshots, so it never reads or writes the user's
# live scene, keymap or window state. That also avoids the persisted-window-size
# OIT crash. Linux only: on macOS Hana's config path ignores XDG_CONFIG_HOME.
set -euo pipefail

HANA_TEST_PURPOSE="${HANA_TEST_PURPOSE:-}"
HANA_TEST_OWNER="${HANA_TEST_OWNER:-${CODEX_THREAD_ID:-${CLAUDE_CODE_SESSION_ID:-}}}"
export HANA_TEST_PURPOSE HANA_TEST_OWNER

CHECKOUT="${1:?Usage: smoke_launch.sh <checkout> <sha> <log> [port]}"
SHA="${2:?Usage: smoke_launch.sh <checkout> <sha> <log> [port]}"
LOG="${3:?Usage: smoke_launch.sh <checkout> <sha> <log> [port]}"
LIB="$(cd "$(dirname "${BASH_SOURCE[0]}")/../lib" && pwd)"
source "$LIB/conf.sh"
PORT="${4:-$(conf_value production smoke_port)}"
READY_SECONDS=$(conf_value production smoke_ready_seconds)
EXIT_SECONDS=$(conf_value production smoke_exit_seconds)
REFUSED_PORT=$(conf_value hana_shot refused_port)
CONNECT_SECONDS=$(conf_value production smoke_connect_seconds)
CALL_SECONDS=$(conf_value production smoke_call_seconds)
SIGNAL_GRACE_SECONDS=$(conf_value production smoke_signal_grace_seconds)
POLL_SECONDS=$(conf_value production smoke_poll_seconds)
HOST=$(conf_value hana_shot host)
CHECKOUT="$(cd "$CHECKOUT" && pwd)"

: >"$LOG"
# Log stamps are in the user's zone setting; the machine's zone when the setting cannot be read.
ZONE="$("$LIB/py" "$LIB/user_zone.py" 2>/dev/null)" || ZONE=
stamp() { if [[ -n "$ZONE" ]]; then TZ="$ZONE" date '+%H:%M:%S'; else date '+%H:%M:%S'; fi; }
log() { printf '%s %s\n' "$(stamp)" "$*" | tee -a "$LOG"; }
fail() { log "SMOKE FAILED: $*"; exit 1; }

[[ "$(uname -s)" == Linux ]] || fail "Linux only; Hana ignores XDG_CONFIG_HOME on $(uname -s)"
[[ "$PORT" != "$REFUSED_PORT" ]] || fail "$REFUSED_PORT is the user's live Hana"

# The build must be of exactly this sha; checked again after the run.
check_head() {
    local head
    head="$(git -C "$CHECKOUT" rev-parse HEAD)"
    [[ "$head" == "$(git -C "$CHECKOUT" rev-parse "$SHA^{commit}")" ]] || fail "$CHECKOUT is at $head, not $SHA"
    [[ -z "$(git -C "$CHECKOUT" status --porcelain --untracked-files=no)" ]] || fail "$CHECKOUT has tracked changes"
}
check_head

brp() {
    curl --fail --silent --show-error --connect-timeout "$CONNECT_SECONDS" --max-time "$CALL_SECONDS" \
        --header 'Content-Type: application/json' \
        --data "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"$1\",\"params\":{}}" \
        "http://${HOST}:${PORT}/jsonrpc"
}
brp rpc.discover >/dev/null 2>&1 && fail "port $PORT already answers BRP"

checkout_cargo() ( cd "$CHECKOUT" && direnv exec "$CHECKOUT" cargo "$@" )

log "build: cargo build -p hana at $CHECKOUT ($SHA)"
checkout_cargo build --manifest-path "$CHECKOUT/Cargo.toml" -p hana --bin hana >>"$LOG" 2>&1 \
    || fail "cargo build -p hana"
TARGET_DIR="$(checkout_cargo metadata --no-deps --format-version 1 \
    --manifest-path "$CHECKOUT/Cargo.toml" 2>>"$LOG" | jq -r .target_directory)"
BIN="$TARGET_DIR/debug/hana"
[[ -x "$BIN" ]] || fail "no binary at $BIN"

[[ -n "${HANA_TEST_PURPOSE//[[:space:]]/}" ]] || { printf '%s\n' 'smoke_launch: set HANA_TEST_PURPOSE to the feature under test' >&2; exit 2; }

HANA_PID=""
cleanup() {
    "$LIB/py" "$LIB/../build_queue/app_gate.py" stop --scope-file "$SCRATCH/app.cgroup" \
        --grace "$SIGNAL_GRACE_SECONDS"
    if [[ -n "$HANA_PID" ]]; then
        wait "$HANA_PID" 2>/dev/null || true
    fi
    rm -rf "$SCRATCH"
}
SCRATCH="$(mktemp -d)"
trap cleanup EXIT
mkdir "$SCRATCH/xdg"

# The saved scope lets cleanup stop children after the supervisor exits.
# The binary runs directly, so BEVY_ASSET_ROOT replaces cargo's
# CARGO_MANIFEST_DIR for asset lookup.
log "launch: port $PORT, config $SCRATCH/xdg"
setsid "$LIB/py" "$LIB/../build_queue/app_gate.py" --binary "$BIN" --repo "$CHECKOUT" \
    --purpose "$HANA_TEST_PURPOSE" --owner "$HANA_TEST_OWNER" --pid-file "$SCRATCH/app.pid" -- \
    direnv exec "$CHECKOUT" env XDG_CONFIG_HOME="$SCRATCH/xdg" BRP_EXTRAS_PORT="$PORT" \
    HANA_TEST_PURPOSE="$HANA_TEST_PURPOSE" HANA_TEST_OWNER="$HANA_TEST_OWNER" \
    BEVY_ASSET_ROOT="$CHECKOUT/crates/hana" RUST_BACKTRACE=1 "$BIN" >>"$LOG" 2>&1 &
HANA_PID=$!

last_reason=""
show_admission_status() {
    if [[ -r "$SCRATCH/app.status" ]]; then
        reason="$(<"$SCRATCH/app.status")"
        if [[ "$reason" != "$last_reason" ]]; then
            log "$reason"
            last_reason="$reason"
        fi
    fi
}
until [[ -s "$SCRATCH/app.pid" ]]; do
    show_admission_status
    if ! kill -0 "$HANA_PID" 2>/dev/null; then
        show_admission_status
        fail "${last_reason:-app admission exited; see $LOG}"
    fi
    sleep "$POLL_SECONDS"
done

deadline=$((SECONDS + READY_SECONDS))
until brp rpc.discover >/dev/null 2>&1; do
    kill -0 "$HANA_PID" 2>/dev/null || fail "hana exited before BRP answered"
    ((SECONDS < deadline)) || fail "BRP did not answer within ${READY_SECONDS}s"
    sleep "$POLL_SECONDS"
done
log "ready: BRP answered on port $PORT"

brp brp_extras/shutdown >>"$LOG" 2>&1 || fail "brp_extras/shutdown request"
deadline=$((SECONDS + EXIT_SECONDS))
while kill -0 "$HANA_PID" 2>/dev/null; do
    ((SECONDS < deadline)) || fail "hana still running ${EXIT_SECONDS}s after shutdown"
    sleep "$POLL_SECONDS"
done
status=0
wait "$HANA_PID" || status=$?
HANA_PID=""
((status == 0)) || fail "hana exited with status $status after shutdown"
log "shutdown: hana exited 0"

check_head
log "SMOKE PASSED: $SHA"
