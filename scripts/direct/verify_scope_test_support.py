"""Provide a scope launcher for fixtures that use fake Cargo."""

from pathlib import Path


def write_test_scope_launcher(directory: Path) -> None:
    manager = directory / "systemd-run"
    _ = manager.write_text(r'''#!/bin/sh
set -eu
for word in "$@"; do
    case "$word" in --unit=*) unit=${word#--unit=} ;; esac
    [ "$word" != -- ] || break
    shift
done
shift
peak=$4
shift 4
scope="$TMPDIR/$unit"
mkdir "$scope"
printf 'populated 0\n' > "$scope/cgroup.events"
printf '%s\n' "$scope" > "$peak.cgroup"
: > "$peak"
exec "$@" 2>&1
''')
    manager.chmod(0o755)
