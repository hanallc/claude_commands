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
from unittest.mock import patch

from scripts.lib.shell_test_support import shell_function, shell_functions
from scripts.direct.tree_watch_test_support import TreeWatchFixture
from scripts.direct.verify_doc_test_support import VerifyDocFixture
from scripts.direct.verify_ssh_targets_test_support import VerifySshTargetsFixture
from scripts.direct.verify_lint_cache_test_support import VerifyLintCacheFixture
from scripts.direct.verify_lint_scope_test_support import VERIFY, CARGO_STUB, TAPLO_STUB
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.test_times import task_cpu


class VerifyPythonStartsTests(VerifyLintCacheFixture):
    def test_lint_trace_has_one_start_per_key_pair_and_no_unnamed_helper(self) -> None:
        _ = self.enable_cache()
        trace = self.root / "python-starts.tsv"
        self.environment["VERIFY_PY_TRACE"] = str(trace)
        self.environment["TEST_CLIPPY_STATUSES"] = "[101, 0]"
        result = self.verify("lint", "sample")
        self.assert_fix_and_judgment_calls()
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
        # Each local compile starts a scope supervisor on Linux.
        supervised_steps = 4 if sys.platform == "linux" else 0
        self.assertEqual(counts["verify_scoped_step.py"], supervised_steps, rows)
        self.assertEqual(len(rows), 6 + supervised_steps, counts)
        # The per-run helper reads the dev features; no Python process of its own starts.
        calls = [line.split("\t") for line in Path(str(trace) + ".calls").read_text().splitlines()]
        in_helper = Counter(call[0] for call in calls)
        self.assertEqual((counts["dev_features.py"], in_helper["dev_features.py"]), (0, 1), rows)
        clippy_repairs = [call for call in calls if call[:2] == ["admission_client.py", "place-step"]
                          and "clippy" in call and "--fix" in call]
        self.assertEqual(len(clippy_repairs), 1)
        repair = clippy_repairs[0]
        self.assertIn("--cargo-folder", repair)
        self.assertEqual(repair[repair.index("--cargo-folder") + 1], str(self.root / "target"))
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


class VerifyCopyPythonStartsTests(VerifyDocFixture):
    def test_managed_doc_uses_the_existing_copy_owners_and_scope_supervisor(self) -> None:
        trace = self.root / "doc-python-starts.tsv"
        self.environment["VERIFY_PY_TRACE"] = str(trace)
        command = self.copies_verify_command("doc")
        extra_starts: set[tuple[str, ...]] = set()
        arguments = task_cpu.command_arguments

        def observe(pid: int) -> list[str]:
            argv = arguments(pid)
            if any(Path(word).name == "verify_checkout_handoff.py" or word == "import sys; print(sys.executable)"
                   for word in argv):
                extra_starts.add(tuple(argv))
            return argv

        log = self.root / "doc-python-starts.log"
        with log.open("wb") as output, patch.object(task_cpu, "command_arguments", observe):
            result = task_cpu.measure(command, cwd=self.repo, environment=self.environment,
                                      timeout_s=TEST_HANG_GUARD_SECS, output_fd=output.fileno())
        self.assertEqual(result.status, 0, log.read_text())
        counts = Counter(line.split("\t")[0] for line in trace.read_text().splitlines())
        self.assertEqual(counts["copies.py"], 2, counts)
        self.assertEqual(counts["verify_scoped_step.py"], 1, counts)
        self.assertFalse(extra_starts, extra_starts)


class VerifyPythonShimProbeTests(unittest.TestCase):
    def test_interpreter_selection_helpers_do_not_inherit_caller_pipes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reader, writer = os.pipe()
            self.addCleanup(os.close, reader)
            self.addCleanup(os.close, writer)
            mkdir = shutil.which("mkdir")
            assert mkdir is not None
            for name in ("python3", "mkdir"):
                helper = root / name
                _ = helper.write_text(f"""#!{sys.executable}
import json
import os
from pathlib import Path
import sys

name = Path(sys.argv[0]).name
if name == "mkdir" or sys.argv[1:3] == ["-I", "-c"]:
    descriptors = {{}}
    for fd in (0, 1, 2, {reader}, {writer}):
        try:
            state = os.fstat(fd)
        except OSError:
            continue
        descriptors[str(fd)] = [state.st_dev, state.st_ino]
    Path({str(root)!r}, name + ".json").write_text(json.dumps(descriptors))
executable = {mkdir!r} if name == "mkdir" else sys.executable
os.execv(executable, [executable, *sys.argv[1:]])
""")
                helper.chmod(0o755)
            environment = {**os.environ, "XDG_CACHE_HOME": str(root / "cache"),
                           "PATH": os.pathsep.join((str(root), os.environ["PATH"]))}
            _ = environment.pop("CLAUDE_PYTHON", None)
            pipe = os.fstat(reader)
            result = subprocess.run(
                [str(VERIFY.parents[1] / "lib/py"), "-c",
                 "import os,sys; s=os.fstat(int(sys.argv[1])); print(sys.stdin.read().strip(), s.st_dev, s.st_ino)",
                 str(reader)],
                input="caller input\n", cwd=root, capture_output=True, text=True,
                env=environment, pass_fds=(reader, writer), timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(result.stdout, f"caller input {pipe.st_dev} {pipe.st_ino}\n")
            null = Path(os.devnull).stat()
            expected = {str(fd): [null.st_dev, null.st_ino] for fd in range(3)}
            for name in ("python3", "mkdir"):
                with self.subTest(helper=name):
                    self.assertEqual(json.loads((root / f"{name}.json").read_text()), expected,
                                     f"{name} inherited a caller stream or descriptor")


class VerifyOutputRootsTests(unittest.TestCase):
    def test_metadata_reuse_keeps_the_export_after_a_config_change(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary).resolve()
            _ = (workspace / "Cargo.toml").write_text('[workspace]\nmembers = []\n')
            metadata = json.dumps({"workspace_root": str(workspace), "target_directory": str(workspace / "target")})
            harness = workspace / "metadata.sh"
            _ = harness.write_text(
                'set -euo pipefail\n'
                + shell_functions(VERIFY.parent.parent / "lint/invoke.sh",
                                  "close_checkout_handoffs", "run_without_checkout_handoffs")
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
            environment = {name: value for name, value in os.environ.items()
                           if not name.startswith(("CARGO_", "BUILDLOG_"))}
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
