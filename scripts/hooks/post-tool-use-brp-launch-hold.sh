#!/usr/bin/env bash
# Hand the published MCP process identities to their memory holder.
hook_dir=$(dirname "${BASH_SOURCE[0]}")
exec "$hook_dir/../lib/py" "$hook_dir/../build_queue/app_gate.py" hook-post
