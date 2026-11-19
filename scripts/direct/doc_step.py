#!/usr/bin/env python3
"""Read the staged tree and record a doc pass."""
from __future__ import annotations

try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
import time

from scripts.buildlog.treekey import committed_tree_key
from scripts.copies.copies import staged_tree
from scripts.lib.atomic_file import sync_folder


def record_pass(repo: Path, tree: str, call_id: str) -> None:
    if staged_tree(repo) != tree:
        print("verify.sh: the staged tree changed while doc ran; no doc pass record was written.")
        return
    records = Path.home() / ".local/state/verify/doc_pass"
    records.mkdir(parents=True, exist_ok=True)
    cutoff = time.time() - 14 * 24 * 60 * 60
    for old in records.glob("*.json"):
        try:
            if old.stat().st_mtime < cutoff:
                old.unlink()
        except FileNotFoundError:
            pass
    record = dict(repo_top=str(repo.resolve()), tree_id=tree,
                  time=datetime.now(timezone.utc).isoformat(), call_id=call_id)
    with tempfile.NamedTemporaryFile(mode="w", dir=records, prefix=".doc-", delete=False) as output:
        temporary = Path(output.name)
        try:
            json.dump(record, output)
            output.flush()
            os.fsync(output.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        _ = temporary.replace(records / (tree + ".json"))
        sync_folder(records)
    finally:
        temporary.unlink(missing_ok=True)
    print("verify.sh: doc pass recorded for tree " + tree)


def main(arguments: list[str]) -> int:
    match arguments:
        case ["tree", repo]:
            print(staged_tree(Path(repo)))
        case ["source", repo]:
            tree = staged_tree(Path(repo))
            print(tree, committed_tree_key(tree.encode("ascii")))
        case ["record", repo, tree, call_id]:
            record_pass(Path(repo), tree, call_id)
        case ["target-path", repo, target]:
            print((Path(repo) / target).resolve())
        case _:
            print("doc_step.py: invalid arguments", file=sys.stderr)
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
