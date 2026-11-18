"""Exercise the default doc route with the private verify fixture."""

from pathlib import Path
from typing import final

from scripts.build_queue import placement
from scripts.build_queue.admission_test_support import caller_admission
from scripts.direct.verify_doc_test_support import VerifyDocFixture


@final
class DefaultDocRoutingTests(VerifyDocFixture):
    def test_claude_doc_uses_the_local_staged_snapshot(self) -> None:
        workspace = self.repo
        original = workspace.rename(self.root / ".claude")
        self.repo = original
        _ = self.git("remote", "add", "origin", "git@fixture.invalid:claude_commands")
        _ = self.git("worktree", "add", "-qb", "doc-fixture", str(workspace))
        self.repo = workspace
        self.enable_held_mac_doc()
        (Path(self.environment["BUILD_HOLD_DIR"]) / "other").unlink()
        registry = self.root / "build_targets.conf"
        _ = registry.write_text(registry.read_text().replace("targets=natedev,mac", "targets=mac,natedev"))
        runner = Path(self.environment["VERIFY_MAC_RUNNER"])
        mac_called = self.root / "mac-called"
        _, _, source = runner.read_text().partition("\n")
        self.executable(runner, f"from pathlib import Path\nPath({str(mac_called)!r}).touch()\n" + source)

        lib = self.repo / "alpha/src/lib.rs"
        _ = lib.write_text("pub fn staged() {}\n")
        _ = self.git("add", "alpha/src/lib.rs")
        _ = lib.write_text("pub fn unstaged() {}\n")
        _ = (self.repo / "notes.md").write_text("untracked")
        result = self.verify()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(mac_called.exists(), result.stdout)
        admission = caller_admission(self, self.root, self.environment)
        with admission.loop_turn():
            request, = admission.server.placement_requests.values()
            mac, = (candidate for candidate in request.constraints.candidates
                    if candidate.destination.target == "mac")
        self.assertEqual(mac.eligibility, placement.FixedExclusion("offload_exclude"))
        call, = self.cargo_calls()
        self.assertEqual(call["sources"]["alpha/src/lib.rs"], "pub fn staged() {}\n")
        self.assertFalse(call["notes_present"])
        self.assertNotEqual(call["cwd"], str(self.repo))
