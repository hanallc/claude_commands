"""In a repository that cargo-berth configures, the lint fix pass writes a private copy.

Only the paths that this worktree's reservations hold come back to the worktree.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import cast, override

from scripts.build_queue.admission_test_support import caller_admission
from scripts.direct import verify_lint_scope_test_support as support

# Print the board in TEST_BERTH_BOARD, as `cargo-berth board --json` does.
BERTH_STUB = r'''
import os
import sys

if sys.argv[1:] != ["board", "--json"]:
    raise SystemExit("unexpected cargo-berth invocation: " + repr(sys.argv[1:]))
print(os.environ["TEST_BERTH_BOARD"])
'''

# A cargo call with --manifest-path runs on the copy: record it, and let mend --fix
# write TEST_MEND_FIXES into the copy.
COPY_HOOK = '''arguments = sys.argv[1:]
if "--manifest-path" in arguments:
    manifest = Path(arguments[arguments.index("--manifest-path") + 1])
    with Path(os.environ["TEST_CARGO_EVENTS"]).with_name("copy-calls.jsonl").open("a", encoding="utf-8") as output:
        output.write(json.dumps({"args": arguments, "target": os.environ.get("CARGO_TARGET_DIR", "")}) + "\\n")
    if arguments[:1] == ["mend"] and "--fix" in arguments:
        for relative, text in json.loads(os.environ.get("TEST_MEND_FIXES", "{}")).items():
            (manifest.parent / relative).write_text(text, encoding="utf-8")
'''


def snapshot(identity: str, root: Path, branch: str, *paths: str) -> dict[str, object]:
    return {
        "reservation_id": identity,
        "target": {"ref": "refs/heads/main"},
        "holder": {"worktree_root": str(root), "branch": {"kind": "attached", "reference": branch}},
        "scopes": [{"kind": "file", "path": path} for path in paths],
        "lifecycle": {"stage": "active"}, "edit_blocking_status": "blocking",
    }


def board(*reservations: dict[str, object]) -> str:
    position = {"generation": 1, "journal_byte_offset": 1}
    empty: dict[str, object] = {"journal_position": position, "entries": []}
    return json.dumps({
        "output_contract_version": 6, "verb": "board", "status": "ok", "exit_code": 0,
        "payload": {"kind": "board", "data": {
            "targets": [{"ref": "refs/heads/main", "reservations": list(reservations)}],
            "ready_now": empty, "waiting": empty, "unresolved_overlaps": empty,
            "unconstrained_reservations": empty, "resolved": empty,
        }},
    })


class VerifyLintBerthCopyTests(support.VerifyLintScopeFixture):
    @override
    def setUp(self) -> None:
        super().setUp()
        _ = caller_admission(self, self.root, self.environment)
        self.use_real_toml_tools()
        for name in ("foreign", "free"):
            _ = (self.root / "src" / f"{name}.rs").write_text(f"fn {name}() {{}}\n", encoding="utf-8")
        self.run_git("add", "--force", "--", "src/foreign.rs", "src/free.rs")
        self.run_git("-c", "user.name=Lint fixture", "-c", "user.email=lint@example.invalid",
                     "-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "neighbors")
        self.write_executable(self.root / "bin" / "cargo-berth", BERTH_STUB)
        cargo = self.root / "bin" / "cargo"
        _ = cargo.write_text(cargo.read_text(encoding="utf-8").replace("arguments = sys.argv[1:]\n", COPY_HOOK, 1),
                             encoding="utf-8")
        metadata = cast(dict[str, object], json.loads(self.environment["TEST_METADATA"]))
        metadata["target_directory"] = str(self.root / "target")
        self.environment["TEST_METADATA"] = json.dumps(metadata)
        self.environment["TEST_BERTH_BOARD"] = board(
            snapshot("own-1", self.root, "refs/heads/mine", "src/lib.rs"),
            snapshot("other-2", self.root.parent / "other", "refs/heads/theirs", "src/foreign.rs"),
        )

    def lint(self, fixes: dict[str, str]) -> tuple[int, str]:
        self.environment["TEST_MEND_FIXES"] = json.dumps(fixes)
        result = self.invoke_verify("lint", "--no-cache", cwd=self.root)
        return result.returncode, result.stdout + result.stderr

    def text(self, relative: str) -> str:
        return (self.root / relative).read_text(encoding="utf-8")

    def copy_calls(self) -> list[dict[str, object]]:
        calls = self.events.with_name("copy-calls.jsonl")
        return [cast(dict[str, object], json.loads(line))
                for line in calls.read_text(encoding="utf-8").splitlines()] if calls.exists() else []

    def assert_copy_gone(self) -> None:
        self.assertEqual(sorted(path.name for path in (self.root / "target").iterdir()
                                if path.name.startswith(("verify-lint-copy", ".verify-lint-copy"))), [])

    def test_held_fix_reaches_the_worktree_through_the_copy(self) -> None:
        status, output = self.lint({"src/lib.rs": "pub(crate) fn value() -> u8 { 1 }\n"})
        self.assertEqual(status, 0, output)
        self.assertEqual(self.text("src/lib.rs"), "pub(crate) fn value() -> u8 { 1 }\n")
        calls = self.copy_calls()
        mend = [call for call in calls if cast(list[str], call["args"])[:1] == ["mend"]]
        self.assertTrue(mend, output)
        for call in calls:
            self.assertEqual(call["target"], str(self.root / "target"))
            manifest = cast(list[str], call["args"])[-1]
            self.assertEqual(manifest, str(self.root / "target/verify-lint-copy/tree/Cargo.toml"))
        self.assert_copy_gone()

    def test_foreign_fix_is_reported_and_not_written(self) -> None:
        status, output = self.lint({"src/foreign.rs": "pub(crate) fn foreign() {}\n"})
        self.assertEqual(status, 0, output)
        self.assertEqual(self.text("src/foreign.rs"), "fn foreign() {}\n")
        self.assertIn("not written: src/foreign.rs is held by reservation other-2 (refs/heads/theirs)", output)
        self.assert_copy_gone()

    def test_free_fix_fails_lint_and_is_not_written(self) -> None:
        status, output = self.lint({"src/lib.rs": "pub(crate) fn value() -> u8 { 1 }\n",
                                    "src/free.rs": "pub(crate) fn free() {}\n"})
        self.assertEqual(status, 1, output)
        self.assertEqual(self.text("src/free.rs"), "fn free() {}\n")
        self.assertEqual(self.text("src/lib.rs"), "pub(crate) fn value() -> u8 { 1 }\n")
        self.assertIn("run `cargo-berth check src/free.rs`, then run lint again", output)
        self.assertFalse(any("clippy" in cast(list[str], call["args"]) for call in self.copy_calls()))
        self.assert_copy_gone()


if __name__ == "__main__":
    _ = unittest.main()
