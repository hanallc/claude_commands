"""Place the pre-push sequence and return edits before it amends."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import cast, override

from scripts.build_queue import admission_protocol as wire, placement
from scripts.build_queue.admission_test_support import caller_admission, caller_provider_controls
from scripts.direct.verify_hold_wait_test_support import VerifyHoldWaitFixture
from scripts.direct.verify_mac_offload_test_support import RUNNER_STUB
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.lint.memory_test_support import write_config_fixture

VALIDATE = Path(__file__).resolve().parents[1] / "validate_and_push/validate_ci.sh"
CROSS_TARGET = "x86_64-pc-windows-gnu"

CARGO = r'''
import json, os, sys
from pathlib import Path

words = sys.argv[1:]
if words == ["metadata", "--no-deps", "--format-version", "1"]:
    print(os.environ["TEST_METADATA"])
elif words in (["+nightly", "fmt", "--version"], ["nextest", "--version"]):
    print("fixture version")
else:
    with Path(os.environ["TEST_CARGO_EVENTS"]).open("a") as output:
        output.write(json.dumps([os.getcwd(), words]) + "\n")
    source = Path("src/lib.rs")
    if words == ["mend", "--all-targets", "--workspace", "--fix"]:
        source.write_text(source.read_text() + "// mend\n")
    elif words == ["+nightly", "fmt", "--all"]:
        source.write_text(source.read_text() + "// fmt\n")
    elif words == ["build", "-p", "cargo-mend", "--bin", "cargo-mend"]:
        binary = Path(os.environ["CARGO_TARGET_DIR"]) / "debug/cargo-mend"
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_text("#!" + sys.executable + "\n" + os.environ["TEST_SELF_MEND"])
        binary.chmod(0o755)
'''

SELF_MEND = r'''
import json, os, sys
from pathlib import Path

assert os.environ["RUSTC_WRAPPER"] == ""
with Path(os.environ["TEST_CARGO_EVENTS"]).open("a") as output:
    output.write(json.dumps([os.getcwd(), ["cargo-mend", *sys.argv[1:]]]) + "\n")
if "--fix" in sys.argv:
    source = Path("src/lib.rs")
    source.write_text(source.read_text() + "// mend\n")
'''

RUN_VALIDATION_COPY = r'''
import shutil
import subprocess
from scripts.mac_test import eligibility, fix_return, linux_runner, offload

source = Path(arguments[arguments.index("--repo-root") + 1])
mirror = Path(os.environ["TEST_VALIDATION_MIRROR"])
shutil.copytree(source, mirror, dirs_exist_ok=True, ignore=shutil.ignore_patterns(".git", "target"))
sent_key = offload.read_tree_key(source)
verb = arguments[arguments.index("--verb") + 1] if "--verb" in arguments else "nextest"
assert "--validation-step" in arguments
words = tuple(arguments[arguments.index("--") + 1:])
lint = Path.home() / ".claude/scripts/lint/lint"
status = subprocess.run(
    ["bash", str(lint), *linux_runner.lint_invocation(verb, words, validation_step=True)],
    cwd=mirror, check=False,
    env={**os.environ, "CARGO_TARGET_DIR": str(mirror / "target"), "BUILDLOG_CALLER": "verify-linux",
         "LINT_CONFIG_FILE": os.environ["TEST_VALIDATION_MAC_POLICY"]},
).returncode
if status == os.EX_PROTOCOL and verb == "clippy":
    decline(placement.RUST_TARGET_MISSING, "0")
    raise SystemExit(75)
mode, runner_status = ("passed" if status == 0 else "failed"), status
if status == 0 and eligibility.returns_copy_edits(verb):
    if os.environ.get("TEST_VALIDATION_REFUSE_FIX") == verb:
        if os.environ.get("TEST_VALIDATION_EDIT_SOURCE") == "1":
            (source / "src/lib.rs").write_text("// caller edit\n")
        decline(placement.VALIDATION_EDIT_RETURN, "0")
        fixture_outcome = placement.AttemptDeclined(placement.VALIDATION_EDIT_RETURN)
        fixture_evidence = execution_identity.ExecutionEvidenceUnavailable("the private command has ended", time.time())
        raise SystemExit(75)
    else:
        changes = fix_return.MirrorChanges(("src/lib.rs",), ())
        fixed = fix_return.apply_fix(source, mirror, changes, lambda: offload.read_tree_key(source) == sent_key,
                                     require_all=True)
        fix = fix_return.fix_word(fixed)
'''


class VerifyValidationTests(VerifyHoldWaitFixture):
    repo: Path = Path()
    mirror: Path = Path()

    @override
    def setUp(self) -> None:
        super().setUp()
        tool = shutil.which("git")
        if tool is None:
            self.fail("Git is required")
        git = self.fixture.bin_directory / "git"
        git.unlink()
        git.symlink_to(tool)
        self.repo = self.fixture.root / "checkout"
        self.mirror = self.fixture.root / "mac-checkout"
        self.repo.mkdir()
        _ = shutil.copy2(self.fixture.root / "Cargo.toml", self.repo)
        _ = shutil.copytree(self.fixture.root / "src", self.repo / "src")
        cross = self.repo / ".claude/config/cross_targets"
        cross.parent.mkdir(parents=True)
        _ = cross.write_text(CROSS_TARGET + "\n")
        _ = (self.repo / ".gitignore").write_text("target/\n")
        lint_config = self.fixture.home / ".claude/scripts/lint/lint_config.sh"
        caller_policy = self.fixture.root / "caller-lint.conf"
        mac_policy = self.fixture.root / "mac-lint.conf"
        mac_settings = Path(self.fixture.environment["MAC_TEST_CONFIG"])
        _ = write_config_fixture(mac_settings, f"offload=on\nlinux_only.{self.repo.name}=\n", template="mac_test")
        _ = write_config_fixture(caller_policy, "mend=on\n", template="lint")
        _ = write_config_fixture(mac_policy, "mend=off\nfmt=off\nclippy=off\ndoc=off\n", template="lint")
        self.fixture.environment.update({
            "PACKAGE_ROOT": str(self.repo),
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "validation fixture",
            "GIT_AUTHOR_EMAIL": "validation@example.invalid",
            "GIT_COMMITTER_NAME": "validation fixture",
            "GIT_COMMITTER_EMAIL": "validation@example.invalid",
            "LINT_CONFIG_READER": str(lint_config),
            "LINT_CONFIG_FILE": str(caller_policy),
            "LINT_SKIP_SWEEP": "1",
            "LINT_DOC_JOBS": "3",
            "NEXTEST_TEST_THREADS": "2",
            "TEST_RUNNER_MAC": "passed",
            "TEST_RUNNER_STATUS": "0",
            "TEST_VALIDATION_MIRROR": str(self.mirror),
            "TEST_VALIDATION_MAC_POLICY": str(mac_policy),
            "TEST_SELF_MEND": SELF_MEND,
        })
        self.fixture.write_executable(self.fixture.bin_directory / "cargo", CARGO)
        self.fixture.write_executable(self.fixture.bin_directory / "rustup", f'''
import os, sys
assert sys.argv[1:] == ["target", "list", "--installed"]
if not os.environ.get("TEST_MAC_NO_TARGET") or os.getcwd() != os.environ["TEST_VALIDATION_MIRROR"]:
    print({CROSS_TARGET!r})
''')
        self.fixture.write_executable(self.fixture.runner, RUNNER_STUB.replace(
            'if "--assignment-file" in arguments:\n    if (mode == "passed"',
            RUN_VALIDATION_COPY + '\nif "--assignment-file" in arguments:\n    if (mode == "passed"',
        ))
        _ = self.command("git", "init", "-q", "--initial-branch=main")
        _ = self.command("git", "add", ".")
        _ = self.command("git", "commit", "-qm", "source")
        self.fixture.write_executable(self.repo / ".git/hooks/pre-commit", """#!/bin/bash
git show :src/lib.rs >> .git/amended-source
printf '%s\n' '---' >> .git/amended-source
""")

    def command(self, *words: str) -> str:
        result = subprocess.run(words, cwd=self.repo, env=self.fixture.environment, text=True,
                                capture_output=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def start_step(self, script: Path, *args: str) -> subprocess.Popen[str]:
        process = subprocess.Popen(["bash", str(script), *args], cwd=self.repo, env=self.fixture.environment,
                                   text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        self.addCleanup(self.fixture.stop_verify, process)
        return process

    def validate(self) -> subprocess.CompletedProcess[str]:
        return self.fixture.finish_verify(self.start_step(VALIDATE))

    def cargo_commands(self, folder: Path) -> list[list[str]]:
        return [list(words) for cwd, words in
                (cast(tuple[str, list[str]], json.loads(line))
                 for line in self.fixture.cargo_events_path.read_text().splitlines())
                if cwd == str(folder)]

    def assert_remote_validation(self) -> None:
        result = self.validate()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.cargo_commands(self.repo), [])
        self.assertEqual(self.cargo_commands(self.mirror), [
            ["mend", "--all-targets", "--workspace", "--fix"],
            ["+nightly", "fmt", "--all"],
            ["clippy", "--all-targets", "--workspace", "--all-features", "--", "-D", "warnings"],
            ["clippy", "--all-targets", "--workspace", "--all-features", "--target", CROSS_TARGET, "--", "-D", "warnings"],
            ["doc", "--no-deps", "--jobs", "3", "--workspace", "--all-features"],
            ["nextest", "run", "--workspace", "--all-features", "--tests"],
            ["mend", "--all-targets", "--workspace", "--fail-on-warn"],
        ])
        words = [row["args"] for row in self.fixture.runner_events()]
        for arguments, cargo in zip(words, self.cargo_commands(self.mirror), strict=True):
            if cargo[0] == "clippy":
                self.assertEqual(self.placed_command(arguments), ["cargo", *cargo])
        self.assertIn("LINT_DOC_JOBS", words[4])
        self.assertIn("NEXTEST_TEST_THREADS", words[5])
        original = "pub fn value() -> u8 { 1 }\n"
        edits = [original + "// mend\n", original + "// mend\n// fmt\n"]
        self.assertEqual((self.repo / ".git/amended-source").read_text().split("---\n"), [*edits, ""])
        self.assertEqual(self.command("git", "show", "HEAD:src/lib.rs"), edits[-1])
        self.assertEqual(self.command("git", "status", "--porcelain"), "")

    @staticmethod
    def placed_command(arguments: list[str]) -> list[str]:
        return [word.removeprefix("--validation-command-word=") for word in arguments
                if word.startswith("--validation-command-word=")]

    def hold_natedev(self) -> Path:
        holder = "hana"
        self.hold(holder)
        admission = caller_admission(self, self.fixture.root, self.fixture.environment)
        caller_provider_controls(self, admission).set_host("natedev", placement.TemporaryExclusion("blocked"))
        return Path(self.fixture.environment["BUILD_HOLD_DIR"]) / holder

    def release_natedev_after_refusal(self, process: subprocess.Popen[str], holder: Path) -> None:
        self.fixture.wait_until((self.fixture.runner_state / "placement-refusal.json").exists,
                                "the Mac refusal", (process,), directory=self.fixture.runner_state)
        holder.unlink()
        admission = caller_admission(self, self.fixture.root, self.fixture.environment)
        caller_provider_controls(self, admission).set_host("natedev", placement.AllowedHost())

    def test_pre_push_places_each_step_on_the_free_mac_during_a_hold(self) -> None:
        _ = self.hold_natedev()
        self.assert_remote_validation()

    def test_pre_push_places_the_head_on_the_mac_after_a_memory_wait(self) -> None:
        _ = self.fixture.hold_memory()
        admission = caller_admission(self, self.fixture.root, self.fixture.environment)
        controls = caller_provider_controls(self, admission)
        controls.set_host("mac", placement.TemporaryExclusion("busy"))
        previous = admission.server.reply_filter
        waited: list[wire.PlacementWait] = []

        def make_mac_free(reply: wire.Reply) -> wire.Reply:
            if isinstance(reply, wire.PlacementWait) and isinstance(reply.wait, wire.Wait):
                waited.append(reply)
                _ = controls.hosts.pop("mac", None)
            return previous(reply)

        admission.server.reply_filter = make_mac_free
        self.assert_remote_validation()
        self.assertTrue(waited)

    def test_pre_push_fixes_locally_when_the_copy_cannot_return_edits(self) -> None:
        holder = self.hold_natedev()
        self.fixture.environment["VALIDATE_QUICK"] = "1"
        self.fixture.environment["TEST_VALIDATION_REFUSE_FIX"] = "mend-copy"
        process = self.start_step(VALIDATE)
        self.release_natedev_after_refusal(process, holder)
        result = self.fixture.finish_verify(process)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        fix = ["mend", "--all-targets", "--workspace", "--fix"]
        self.assertEqual(self.cargo_commands(self.mirror).count(fix), 1)
        self.assertEqual(self.cargo_commands(self.repo).count(fix), 1)
        amended = (self.repo / ".git/amended-source").read_text().split("---\n")
        self.assertEqual(amended[0], "pub fn value() -> u8 { 1 }\n// mend\n")
        self.assertEqual(self.command("git", "status", "--porcelain"), "")

    def test_pre_push_stops_when_source_changes_before_local_fix(self) -> None:
        _ = self.hold_natedev()
        self.fixture.environment["TEST_VALIDATION_REFUSE_FIX"] = "mend-copy"
        self.fixture.environment["TEST_VALIDATION_EDIT_SOURCE"] = "1"
        before = self.command("git", "rev-parse", "HEAD")
        result = self.validate()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("the source changed after the Mac edit return", result.stderr)
        self.assertEqual(self.cargo_commands(self.repo), [])
        self.assertEqual(self.command("git", "rev-parse", "HEAD"), before)
        self.assertFalse((self.repo / ".git/amended-source").exists())

    def test_cross_target_refusal_places_clippy_on_the_host_with_the_target(self) -> None:
        holder = self.hold_natedev()
        self.fixture.environment["TEST_MAC_NO_TARGET"] = "1"
        process = self.start_step(self.fixture.verify_script, "validation", "clippy", CROSS_TARGET)
        self.release_natedev_after_refusal(process, holder)
        result = self.fixture.finish_verify(process)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        command = ["clippy", "--all-targets", "--workspace", "--all-features", "--target", CROSS_TARGET, "--", "-D", "warnings"]
        self.assertEqual(self.cargo_commands(self.mirror), [])
        self.assertEqual(self.cargo_commands(self.repo), [command])
        event, = self.fixture.runner_events()
        self.assertEqual(self.placed_command(event["args"]), ["cargo", *command])

    def test_self_built_mend_uses_the_mac_checkout_binary(self) -> None:
        _ = self.hold_natedev()
        self.fixture.write_executable(self.fixture.bin_directory / "jq", '''
print("cargo-mend\tCargo.toml")
''')
        self.fixture.environment["VALIDATE_QUICK"] = "1"
        result = self.validate()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.cargo_commands(self.repo), [])
        self.assertEqual(self.cargo_commands(self.mirror), [
            ["build", "-p", "cargo-mend", "--bin", "cargo-mend"],
            ["cargo-mend", "--workspace", "--all-targets", "--fix"],
            ["+nightly", "fmt", "--all"],
            ["build", "-p", "cargo-mend", "--bin", "cargo-mend"],
            ["cargo-mend", "--workspace", "--all-targets", "--fail-on-warn"],
        ])

    def test_local_format_finishes_its_placement(self) -> None:
        admission = caller_admission(self, self.fixture.root, self.fixture.environment)
        caller_provider_controls(self, admission).set_host("mac", placement.TemporaryExclusion("busy"))
        _ = write_config_fixture(Path(self.fixture.environment["LINT_CONFIG_FILE"]), "mend=off\n")
        self.fixture.environment["VALIDATE_QUICK"] = "1"
        result = self.validate()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.cargo_commands(self.repo), [["+nightly", "fmt", "--all"]])
        with admission.loop_turn():
            assignments = tuple(admission.server.assignments.values())
        self.assertEqual(len(assignments), 1)
        self.assertEqual(assignments[0].state, placement.ClosedAssignment("completed"))
