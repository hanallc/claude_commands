"""Find the folders that build_folders.tool_folder gives tool runs."""
from __future__ import annotations

import json
from pathlib import Path
from typing import cast


def temporary_folder_settings(root: Path, *, suffix_length: int = 6) -> str:
    """Set the naming and lock rules for private tool folders."""
    return f"""\
roots.gate={root}
roots.system={root}
# whole: 1..
names.suffix_length={suffix_length}
# whole: 1..64
names.repository_key_length=8
names.suffix_characters=abcdefghijklmnopqrstuvwxyz0123456789
patterns.tool={{purpose}}{{suffix}}
patterns.legacy-unique={{purpose}}{{suffix}}
# range: 0..
locks.probe_wait_seconds=0.25
# range: 0.001..
locks.probe_retry_seconds=0.005
"""


BUILD_NAMES = ("targeted-test", "merge-gate", "merge-gate-nightly", "promotion-index", "fast-track-index",
               "fast-track-check", "fast-track-restore-index", "fast-track-promote", "fast-track", "production-scratch",
               "bump-gate", "ci-point", "bulk-rule-test", "style-sync-lint", "copies-index", "queue-place",
               "admit-test", "admit-sock", "checkpoint-doc", "checkpoint-paths")
BUILD_PREFIX_SETTINGS = "".join(f"prefixes.{name}={name}-\n" for name in BUILD_NAMES) + """\
prefixes.suite-module={owner}-
prefixes.rust-release-trial=rust-release-trial-{owner}
prefixes.verify-tree=verify-tree.{owner}.
prefixes.verify-copy=verify-copy.{owner}.
prefixes.verify-hold=verify-hold.{owner}.
prefixes.copy-watch=copy-watch.
prefixes.linux-cache-stage=.{owner}.
prefixes.linux-step=.linux-step-
prefixes.style-review={owner}-
"""


def workspace_folder_settings(root: Path) -> str:
    """Set private roots and naming rules for fixture copies and build slots."""
    return temporary_folder_settings(root, suffix_length=8) + f"""\
roots.temporary={root / 'builds'}
roots.scratch={root / 'scratch'}
roots.copies={root / 'copies'}
roots.slots={root / 'slots'}
roots.slot_cargo={root / 'cargo'}
roots.cargo_paths={root / 'cargo-paths'}
roots.workspace={root / 'workspaces'}
roots.mirror={root / 'mirrors'}
roots.shell={root / 'shells'}
roots.session={root / 'session'}
patterns.named={{showrunner}}-{{unit}}/{{work}}-{{purpose}}
patterns.temporary={{repository}}/{{owner}}-{{purpose}}-{{suffix}}
patterns.gate={{purpose}}-{{suffix}}
patterns.standing={{repository}}/{{owner}}-gate{{suffix}}
patterns.editable={{repository}}-{{owner}}/{{purpose}}
patterns.mirror={{repository}}/{{owner}}-s{{slot}}
patterns.target={{purpose}}
patterns.shell-cache={{repository}}
patterns.scratch={{purpose}}-{{suffix}}
patterns.legacy-stable={{purpose}}
patterns.slot={{repository}}-slot{{slot}}
# whole: 1..
slots.unused_days=7
# range: 5e-324..
slots.wait_seconds=30
# whole: 0..1
seed.enabled=0
# whole: 1..
seed.max_gib=24
# whole: 0..
seed.min_free_gib=350
""" + BUILD_PREFIX_SETTINGS


def _tool_name(name: str, tool: str) -> bool:
    """True for <tool> and for <tool>-2, <tool>-3 and up."""
    number = name.removeprefix(tool + "-")
    return name == tool or (number != name and number.isdigit())


def _lease_pid(folder: Path) -> object:
    try:
        record = cast(object, json.loads(folder.with_name(folder.name + ".lease.json").read_text()))
    except (OSError, ValueError):
        return None
    return cast(dict[str, object], record).get("pid") if isinstance(record, dict) else None


def tool_folders(parent: Path, tool: str, *, pid: int | None = None, nested: bool = False) -> list[Path]:
    """List the tool's run folders in parent, or at any depth when nested.

    A pid keeps only the folders whose lease names that process.
    """
    candidates = parent.glob(f"**/{tool}*") if nested else parent.glob(f"{tool}*")
    return [path for path in candidates
            if path.is_dir() and _tool_name(path.name, tool) and (pid is None or _lease_pid(path) == pid)]
