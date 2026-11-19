"""Bound helper starts without changing lint, test or watched input identities."""

from __future__ import annotations

from collections import Counter
import json
import os
from pathlib import Path
import tempfile
import shutil
import subprocess
import sys
from typing import override
import unittest
from unittest import mock

from scripts.lib.shell_test_support import shell_function
from scripts.lib.helper_tree_test_support import verify_test_environment
from scripts.direct.tree_watch_test_support import TreeWatchFixture
from scripts.direct.verify_ssh_targets_test_support import VerifySshTargetsFixture
from scripts.direct.verify_lint_cache_test_support import VerifyLintCacheFixture
from scripts.direct.verify_lint_scope_test_support import VERIFY, CARGO_STUB, TAPLO_STUB
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


class VerifyPythonStartsTests(VerifyLintCacheFixture):
    @classmethod
    @override
    def setUpClass(cls) -> None:
        with mock.patch.dict(os.environ, {"COPIES_RUN_PID": str(os.getpid())}):
            super().setUpClass()

    def test_lint_trace_has_one_start_per_key_pair_and_no_unnamed_helper(self) -> None:
        self.assert_lint_start_count(self)
        self.assert_ssh_helper_calls()

    def assert_lint_start_count(self, fixture: VerifyLintCacheFixture) -> None:
        _ = fixture.enable_cache()
        trace = fixture.root / "python-starts.tsv"
        fixture.environment["VERIFY_PY_TRACE"] = str(trace)
        fixture.environment["TEST_CLIPPY_STATUSES"] = "[101, 0]"
        result = fixture.verify("lint", "sample")
        fixture.assert_fix_and_judgment_calls()
        rows = [line.split("\t") for line in trace.read_text().splitlines()]
        self.assertTrue(rows, result.stdout + result.stderr)
        for row in rows:
            self.assertEqual(len(row), 3, row)
            self.assertGreaterEqual(int(row[1]), 0)
            self.assertGreaterEqual(int(row[2]), 0)
        counts = Counter(row[0] for row in rows)
        print("lint helper starts:", dict(sorted(counts.items())), "total:", len(rows), flush=True)
        self.assertNotIn("INLINE_PY", counts, rows)
        self.assertNotIn("STDIN_PY", counts, rows)
        # The trace counts interpreter starts, not calls inside the helper.
        self.assertEqual(counts["tree_watch.py"], 2, rows)
        self.assertEqual(counts["memory_kill.py"], 1, rows)
        self.assertEqual(counts["step_context.py"], 0, rows)
        # The live helper serves context and folder calls without new interpreters.
        self.assertEqual(counts["build_folders.py"], 0, rows)
        # The dev features read also runs in the helper, so no inline Python starts for it.
        self.assertEqual(len(rows), 6, counts)
        # The per-run helper reads the dev features; no Python process of its own starts.
        in_helper = Counter(line.split("\t")[0] for line in Path(str(trace) + ".calls").read_text().splitlines())
        self.assertEqual((counts["dev_features.py"], in_helper["dev_features.py"]), (0, 1), rows)

    def assert_ssh_helper_calls(self) -> None:
        for target, mode in (("unknown", "passed"), ("linux", "disk")):
            with self.subTest(target=target, mode=mode):
                routing = VerifySshTargetsFixture()
                routing.setUp()
                try:
                    routing.configure("natedev,linux")
                    routing_trace = routing.root / "routing-python.tsv"
                    detached_record = routing.root / "detached.json"
                    _ = detached_record.write_text('{"run_id": "routing-test", "command": [], "status": "finished"}')
                    refused = routing.verify("test", "sample", "--target", target, extra={
                        "VERIFY_PY_TRACE": str(routing_trace),
                        "UNIT_DETACHED_RECORD": str(detached_record),
                        "TEST_SSH_MODES": json.dumps({"linux": mode}),
                    })
                    self.assertNotEqual(refused.returncode, 0, refused.stdout + refused.stderr)
                    self.assertEqual(routing.local_runs(), [])
                    entries = Counter(line.split("\t")[0] for line in routing_trace.read_text().splitlines())
                    calls = Counter(line.split("\t")[0]
                                    for line in Path(str(routing_trace) + ".calls").read_text().splitlines())
                    for program in ("build_targets.py", "build_folders.py", "detached_runs.py"):
                        self.assertGreater(calls[program], 0, (target, program, entries, calls))
                        self.assertEqual(entries[program], 0, (target, program, entries, calls))
                finally:
                    routing.doCleanups()

    def test_caller_settings_leave_lint_starts_and_ssh_verification_unchanged(self) -> None:
        caller = Path(self.enterContext(tempfile.TemporaryDirectory()))
        hook = caller / "startup.sh"
        _ = hook.write_text(': > "${BASH_SOURCE[0]}.ran"\n')
        variables = {
            "COPIES_RUN_PID": str(os.getpid()),
            "BUILDLOG_FIRST_STEP_REQUEST": str(caller / "request.json"),
            "BUILDLOG_FIRST_STEP_ASSIGNMENT": str(caller / "assignment.json"),
            "BUILDLOG_FIRST_STEP_PID": str(os.getpid()),
            "BUILDLOG_FIRST_STEP_EPISODE": "caller-episode",
            "BUILDLOG_FIRST_STEP_ROUTE": "caller-route",
            "BUILDLOG_FIRST_STEP_SINCE": "123",
            "VERIFY_HELPER_STARTED": "1",
            "BASH_ENV": str(hook),
            "GIT_INDEX_FILE": str(caller / "index"),
        }
        with mock.patch.dict(os.environ, variables):
            with self.subTest(fixture="lint"):
                lint = VerifyLintCacheFixture()
                self.addCleanup(lint.doCleanups)
                lint.setUp()
                self.assert_lint_start_count(lint)
                self.assertFalse(variables.keys() & lint.environment.keys())

            with self.subTest(fixture="nested SSH"):
                routing = VerifySshTargetsFixture()
                self.addCleanup(routing.doCleanups)
                routing.setUp()
                routing.configure("linux,natedev")
                result = routing.verify("test", "sample", "--target", "linux")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual([event["target"] for event in routing.ssh_events()], ["linux"])
                self.assertEqual(routing.local_runs(), [])
                self.assertFalse(variables.keys() & routing.environment.keys())
        self.assertFalse(hook.with_suffix(".sh.ran").exists())
        self.assertFalse((caller / "index").exists())

    def test_lint_code_key_tracks_inputs_and_clears_unreadable_keys_with_both_hash_tools(self) -> None:
        home = self.root / "key-home"
        invoke = home / ".claude/scripts/lint/invoke.sh"
        invoke.parent.mkdir(parents=True)
        _ = invoke.write_text("# private code input\n")
        fix_return = home / "fix_return.py"
        _ = fix_return.write_text("# places the lint copy\n")
        harness = self.root / "code-key.sh"
        _ = harness.write_text("\n".join(shell_function(VERIFY.read_text(), name) for name in ("hash_stream", "lint_code_files", "lint_code_key")))
        environment = {**self.environment, "HOME": str(home), "BUILD_HELPER_ROOT": str(home / ".claude/scripts"),
                       "LINT_CONFIG_FILE": str(self.config),
                       "FIX_RETURN_SCRIPT": str(fix_return), "PY": str(self.root / "python-must-not-start")}
        bash = shutil.which("bash")
        assert bash is not None

        def key(*, remove: str = "", unreadable: bool = False) -> str:
            result = subprocess.run(
                [bash, "-euc", 'source "$1"; if [[ -n "$2" ]]; then rm -- "$2"; fi; lint_code_key',
                 "code-key", str(harness), remove], cwd=self.root, env=environment,
                capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            if unreadable:
                self.assertEqual(result.stdout, "\n")
            else:
                self.assertRegex(result.stdout, r"\A[0-9a-f]{64}\n\Z")
            return result.stdout

        original = key()
        for path in (harness, invoke, fix_return, self.config):
            with self.subTest(path=path.name):
                before = path.read_bytes()
                _ = path.write_bytes(before + b"\n# changed input\n")
                self.assertNotEqual(key(), original)
                _ = path.write_bytes(before)
                absent = key(remove=str(path))
                self.assertNotEqual(absent, original)
                # A present file containing the absence marker is still present.
                if path != harness:
                    _ = path.write_bytes(b"absent")
                    self.assertNotEqual(key(), absent)
                _ = path.write_bytes(before)
                self.assertEqual(key(), original)
        tools = self.root / "hash-tools"
        tools.mkdir()
        for name in ("cat", "shasum"):
            tool = shutil.which(name)
            assert tool is not None
            (tools / name).symlink_to(tool)
        unreadable = self.root / "unreadable.conf"
        _ = unreadable.write_text("# no read permission\n")
        unreadable.chmod(0)
        self.addCleanup(unreadable.chmod, 0o600)
        self.assertTrue(unreadable.is_file(), "regression input must pass the shell's -f check")
        read = subprocess.run(
            ["cat", "--", str(unreadable)], capture_output=True,
            timeout=TEST_HANG_GUARD_SECS,
        )
        self.assertNotEqual(read.returncode, 0, "regression input must fail a real read")
        for path in (environment["PATH"], str(tools)):
            with self.subTest(path=path):
                environment["PATH"] = path  # The restricted PATH forces the real shasum fallback.
                self.assertEqual(key(), original)
                environment["LINT_CONFIG_FILE"] = str(unreadable)
                _ = key(unreadable=True)
                environment["LINT_CONFIG_FILE"] = str(self.config)


class VerifyOutputRootsTests(unittest.TestCase):
    def test_metadata_reuse_keeps_the_export_after_a_config_change(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary).resolve()
            _ = (workspace / "Cargo.toml").write_text('[workspace]\nmembers = []\n')
            metadata = json.dumps({"workspace_root": str(workspace), "target_directory": str(workspace / "target")})
            harness = workspace / "metadata.sh"
            _ = harness.write_text(
                'set -euo pipefail\n'
                + shell_function(VERIFY.read_text(), "verify_helper") + "\n"
                + shell_function(VERIFY.read_text(), "set_gate_metadata") + "\n"
                + shell_function(VERIFY.read_text(), "load_metadata") + "\n"
                + "cargo() { [[ ${CARGO_FAIL:-0} == 0 ]] || return 1; cat metadata.json; }\n"
                + 'set_gate_metadata\nprintf "%s\\n" "$BUILDLOG_OUTPUT_ROOTS"\n'
                + 'mkdir .cargo\nprintf \'[build]\\nbuild-dir = "changed-build"\\n\' > .cargo/config.toml\n'
                + 'METADATA_READY=0; CMD=doc; ARGS=(); load_metadata\n'
                + 'printf "reused:%s\\n" "$BUILDLOG_OUTPUT_ROOTS"\n'
                + 'export BUILDLOG_OUTPUT_ROOTS=stale; CARGO_FAIL=1\n'
                + 'set_gate_metadata || true\n'
                + 'printf "failed:%s:%s\\n" "$GATE_METADATA" "$BUILDLOG_OUTPUT_ROOTS"\n')
            _ = (workspace / "metadata.json").write_text(metadata)
            environment = verify_test_environment()
            environment.update({"PY": sys.executable, "VERIFY_START_DIR": str(VERIFY.parent),
                                "VERIFY_METADATA_SCRIPT": str(VERIFY.with_name("verify_metadata.py"))})
            result = subprocess.run(["bash", str(harness)], cwd=workspace, env=environment,
                                    capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            exported, reused, failed = result.stdout.splitlines()
            evidence, root, target, build = exported.split("\t")
            self.assertRegex(evidence, r"\A[0-9a-f]{64}\Z")
            self.assertEqual((root, target, build), (str(workspace), str(workspace / "target"),
                                                   str(workspace / "target")))
            self.assertEqual(reused, "reused:" + exported)
            self.assertEqual(failed, "failed::")


class PairedTreeKeyTests(TreeWatchFixture):
    @override
    def setUp(self) -> None:
        super().setUp()
        for name, content in (("cargo", CARGO_STUB), ("taplo", TAPLO_STUB)):
            tool = self.root / "tools" / name
            _ = tool.write_text(f"#!{sys.executable}\n" + content)
            tool.chmod(0o755)
        self.environment.update({
            "TEST_CARGO_EVENTS": str(self.root / "session/cargo-events"),
            "TEST_TAPLO_EVENTS": str(self.root / "session/taplo-events"),
            "VERIFY_LINT_VERSIONS_FILE": str(self.root / "session/lint-versions"),
        })
        session = self.root / "session"
        session.mkdir(exist_ok=True)
        _ = (session / "lint.conf").write_text("# key input\n")
        harness = session / "keys.sh"
        _ = harness.write_text('set -euo pipefail\nCMD=$1; shift; ARGS=("$@")\n'
                               + 'GATE_METADATA=$TEST_METADATA\n'
                               + "\n".join(shell_function(VERIFY.read_text(), name) for name in ("verify_helper", "lint_code_files", "tree_key", "set_verdict_keys")) + '''
tree_key
tree_key --tree-content
set_verdict_keys --watch-start "$UNIT_SESSION_DIR/start"
printf '%s\\n' "$VERDICT_KEY" "$VERDICT_TREE_KEY"
''')
        self.environment.update({
            "PY": sys.executable, "TEST_METADATA": json.dumps(self.metadata),
            "TREE_WATCH_SCRIPT": str(VERIFY.with_name("tree_watch.py")),
            "CACHE_DIR": str(session / "cache"), "LINT_CONFIG_FILE": str(session / "lint.conf"),
            "HOME": str(self.root), "BUILD_HELPER_ROOT": str(self.root / ".claude/scripts"),
            "FILTER_RUN": "0", "HOLD_WAIT_DIR": str(session),
        })

    def assert_pair(self, *words: str) -> None:
        result = subprocess.run(
            ["bash", str(self.root / "session/keys.sh"), *words],
            cwd=self.root, env=self.environment, capture_output=True, text=True,
            timeout=TEST_HANG_GUARD_SECS,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        keys = result.stdout.splitlines()
        self.assertEqual(len(keys), 4, result.stdout + result.stderr)
        for key in keys:
            self.assertRegex(key, r"\A[0-9a-f]{64}\Z")
        self.assertEqual(keys[:2], keys[2:], "paired keys diverged from separate calls")
        self.assertNotEqual(keys[0], keys[1], "cache key lost script inputs")
        self.assertEqual((self.root / "session/start").read_text().strip(),
                         self.key("--workspace" if words[0] == "lint" else "sample"))

    def test_lint_pair_matches_separate_keys_and_watch_start_on_changed_inputs(self) -> None:
        self.assert_pair("lint", "sample")
        _ = (self.root / "sample/src/new.rs").write_text("// new source\n")
        self.environment["RUSTFLAGS"] = "-C opt-level=1"
        self.assert_pair("lint", "", "--check")
        _ = (self.root / "session/lint.conf").write_text("# changed config\n")
        self.assert_pair("lint", "sample")

    def test_test_pair_preserves_package_filters_environment_and_watch_start(self) -> None:
        self.environment["FILTER_RUN"] = "1"
        self.environment["NEXTEST_RETRIES"] = "2"
        self.assert_pair("test", "sample", "--filter", "z", "--filter", "a", "--filter", "z")
        source = self.root / "sample/src/lib.rs"
        _ = source.write_text(source.read_text() + "// changed input\n")
        self.assert_pair("test", "sample", "--features", "a", "--filter", "render")


if __name__ == "__main__":
    _ = unittest.main()
