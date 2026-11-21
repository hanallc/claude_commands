#!/usr/bin/env bash
# Preview the deletion and return its identifier with the loss counts.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=confirm_gate.sh
source "$SCRIPT_DIR/confirm_gate.sh"

wt_command deletion-preview "${1:-}" "$WT_CONFIRM_DIR" "$WT_CONFIRM_TTL_SECONDS"
