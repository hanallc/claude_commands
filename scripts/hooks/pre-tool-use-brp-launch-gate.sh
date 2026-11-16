#!/usr/bin/env bash
# Admit a BRP launch through the same memory gate as build steps.

hook_dir=$(dirname "${BASH_SOURCE[0]}")

deny() {
    "$hook_dir/../lib/py" - "$1" <<'PY_DENY'
import json
import sys
print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
      "permissionDecision": "deny", "permissionDecisionReason": sys.argv[1]}}))
PY_DENY
}
payload="$(cat)"
launch_fields="$(printf '%s' "$payload" | "$hook_dir/../lib/py" -c '
import json, sys
value = json.load(sys.stdin)
session = value.get("session_id", "")
print(session if isinstance(session, str) else "")
tool = value.get("tool_input", {})
if not isinstance(tool, dict):
    tool = {}
app = tool.get("target_name", tool.get("app", ""))
if value.get("tool_name") == "mcp__brp__brp_launch" and app == "hana":
    print("hana")
' 2>/dev/null)" || launch_fields=""
session_id="${launch_fields%%$'\n'*}"
if [[ "$launch_fields" == *$'\n'* ]]; then
    export CLAUDE_CODE_SESSION_ID="$session_id"
    printf '%s' "$payload" | "$hook_dir/../lib/py" "$hook_dir/../build_queue/app_gate.py" hook-pre
    exit $?
fi
export CLAUDE_CODE_SESSION_ID="$session_id"
source "$hook_dir/../lint/memory_gate.sh"
unset BUILDLOG_CALL_ID
admission=0
reason="$("$hook_dir/../lib/py" "$BUILD_TARGETS_SCRIPT" check-local)" || admission=$?
if [[ "$admission" == 0 ]]; then
    memory_output="$(buildlog_wait_for_memory 2>&1)" || admission=$?
    [[ -z "$memory_output" ]] || printf '%s\n' "$memory_output" >&2
    if [[ "$admission" != 0 ]]; then
        reason="BRP launch memory check failed (exit $admission)"
        [[ -z "$memory_output" ]] || reason+=": ${memory_output##*$'\n'}"
    fi
fi
if [[ "$admission" != 0 ]]; then
    [[ -n "$reason" ]] || reason="natedev's hold state could not be read; not starting."
    deny "$reason"
    exit 0
fi
exit 0
