#!/usr/bin/env python3
"""Verify re-exec uses the real helper with fake direnv and no build tools."""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import select
import shutil
import signal
import subprocess
import sys
from typing import TypedDict, cast, override
import unittest

from scripts.lib.nix_env_test_support import NixFixture
from scripts.lib.shell_test_support import shell_functions
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.lint.memory_test_support import write_config_fixture


VERIFY = Path(__file__).with_name("verify.sh")
SCRIPTS = VERIFY.parent.parent
NOTICE = "verify.sh: not in the nix shell; re-running under nix_env.py exec\n"
LOOP_REFUSAL = "verify.sh: nix_env.py re-exec still has no nix shell (IN_NIX_SHELL unset)\n"

PYTHON_SHIM = '''import json, os, sys
from pathlib import Path
with Path(os.environ["VERIFY_PYTHON_LOG"]).open("a") as output:
    output.write(json.dumps(sys.argv[1:]) + "\\n")
os.execv(os.environ["VERIFY_REAL_PY"], [os.environ["VERIFY_REAL_PY"], *sys.argv[1:]])
'''

TOOL_SETUP = '''"$CLAUDE_PYTHON" - "$@" <<'PYTHON'
import json, os, sys
from pathlib import Path
Path(os.environ["VERIFY_SETUP_LOG"]).write_text(json.dumps({
    "args": sys.argv[1:], "cwd": os.getcwd(),
    "nix_shell": os.environ.get("IN_NIX_SHELL", ""),
    "ldflags": os.environ.get("NIX_LDFLAGS", ""),
    "loop_marker": os.environ.get("VERIFY_NIX_ENV_REEXEC", ""),
    "envrc": os.environ.get("NIX_ENV_ENVRC", ""),
    "keep": os.environ.get("KEEP", ""),
}))
print("verification reached")
PYTHON
exit "$VERIFY_FIXTURE_EXIT"
'''


class VerificationStart(TypedDict):
    args: list[str]
    cwd: str
    nix_shell: str
    ldflags: str
    loop_marker: str
    envrc: str
    keep: str


class VerifyNixShellTests(NixFixture):
    verify_script: Path = Path()
    setup_log: Path = Path()
    python_log: Path = Path()

    @override
    def setUp(self) -> None:
        super().setUp()
        _ = (self.work / "Cargo.toml").write_text("[workspace]\n")
        scripts = self.root / "fixture scripts"
        direct = scripts / "direct"
        direct.mkdir(parents=True)
        self.verify_script = direct / "verify.sh"
        _ = shutil.copy2(VERIFY, self.verify_script)
        _ = shutil.copy2(VERIFY.with_name("file_lock.sh"), direct / "file_lock.sh")
        self.verify_script.chmod(0o755)
        self.script(scripts / "lib/py", PYTHON_SHIM)
        (scripts / "lib/nix_env.py").symlink_to(SCRIPTS / "lib/nix_env.py")
        (scripts / "lib/conf_record.sh").symlink_to(SCRIPTS / "lib/conf_record.sh")
        (scripts / "lint").mkdir()
        _ = shutil.copy2(SCRIPTS / "lint/invoke.sh", scripts / "lint/invoke.sh")
        _ = shutil.copy2(SCRIPTS / "lib/build_folders.py", scripts / "lib/build_folders.py")
        guards = scripts / "build_hold"
        guards.mkdir()
        _ = shutil.copy2(SCRIPTS / "build_hold/override_guard.sh", guards / "override_guard.sh")
        lint = self.home / ".claude/scripts/lint"
        lint.mkdir(parents=True)
        _ = (lint / "invoke.sh").write_text(TOOL_SETUP, encoding="utf-8")
        prepare = ('if [[ ${1:-} == --prepare-temp ]]; then\n'
                   + f'    source "{SCRIPTS / "lint/invoke.sh"}" --prepare-temp\n'
                   + '    return $?\nfi\n')
        _ = (scripts / "lint/invoke.sh").write_text(prepare + TOOL_SETUP, encoding="utf-8")
        self.setup_log = self.root / "verification.json"
        self.python_log = self.root / "python.jsonl"
        self.env.update({"CLAUDE_PYTHON": sys.executable,
                         "VERIFY_REAL_PY": str(SCRIPTS / "lib/py"),
                         "VERIFY_SETUP_LOG": str(self.setup_log),
                         "VERIFY_PYTHON_LOG": str(self.python_log),
                         "VERIFY_FIXTURE_EXIT": "37"})
        # Any accidental tool use must hit a stub, even with a broken setup.
        for name in ("cargo", "nix", "herdr", "claude", "codex", "shutdown.py", "taplo"):
            self.script(self.bin / name, 'raise SystemExit("unexpected tool invocation")\n')

    def invoke(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["bash", str(self.verify_script), *arguments], cwd=self.work,
                              env=self.env, text=True, capture_output=True, check=False, timeout=TEST_HANG_GUARD_SECS)

    def verification_start(self) -> VerificationStart:
        return cast(VerificationStart, json.loads(self.setup_log.read_text(encoding="utf-8")))

    def python_calls(self) -> list[list[str]]:
        if not self.python_log.exists():
            return []
        return [cast(list[str], json.loads(line))
                for line in self.python_log.read_text(encoding="utf-8").splitlines()]

    def helper_calls(self) -> list[list[str]]:
        return [call[index:] for call in self.python_calls() for index, argument in enumerate(call)
                if argument.endswith("/nix_env.py")]

    def test_missing_shell_reexec_preserves_cwd_arguments_and_exit_status(self) -> None:
        envrc = self.envrc(self.work)
        arguments = ("test", "package with spaces", "", "--filter", "a * [b] $literal", "tail")
        result = self.invoke(*arguments)
        self.assertEqual(result.returncode, 37, result.stdout + result.stderr)
        self.assertEqual(result.stderr, NOTICE)
        self.assertEqual(result.stdout, "verification reached\n")
        start = self.verification_start()
        self.assertEqual(start["args"], list(arguments))
        self.assertEqual(start["cwd"], str(self.work))
        self.assertEqual(start["nix_shell"], "impure")
        self.assertEqual(start["ldflags"], "-Lfixture")
        self.assertEqual(start["loop_marker"], "")
        self.assertEqual(start["envrc"], str(envrc.resolve()))
        self.assertEqual([call[1] for call in self.helper_calls()], ["exec"])
        self.assertEqual(self.helper_calls()[0][2:], [str(self.verify_script), *arguments])
        python_calls = self.python_calls()
        self.assertEqual(python_calls, [["-S", str(self.verify_script.parent.parent / "lib/nix_env.py"),
                                        "exec", str(self.verify_script), *arguments]])
        self.assertEqual(sum(call["args"] == ["export", "json"] for call in self.direnv_calls()), 1)

    def test_nearest_ancestor_envrc_is_found_by_the_helper(self) -> None:
        envrc = self.envrc(self.work.parent)
        result = self.invoke("check", "sample")
        self.assertEqual(result.returncode, 37, result.stdout + result.stderr)
        self.assertEqual(result.stderr, NOTICE)
        self.assertEqual(self.direnv_calls()[0]["args"], ["allow", str(envrc)])
        self.assertEqual(self.verification_start()["nix_shell"], "impure")
        self.assertEqual(self.verification_start()["envrc"], str(envrc.resolve()))
        self.assertEqual([call[1] for call in self.helper_calls()], ["exec"])
        self.assertEqual(sum(call["args"] == ["export", "json"] for call in self.direnv_calls()), 1)

    def test_refusal_stops_before_tool_setup_with_only_the_helper_line(self) -> None:
        _ = self.envrc(self.work)
        self.env["DIRENV_FAIL"] = "1"
        result = self.invoke("doc")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(result.stderr, f"nix_env: {self.work}: last failure detail\n")
        self.assertEqual(result.stdout, "")
        self.assertFalse(self.setup_log.exists())
        self.assertEqual([call[1] for call in self.helper_calls()], ["exec"])

    def test_no_envrc_has_no_notice_no_reexec_and_no_direnv_call(self) -> None:
        self.env["NIX_ENV_ENVRC"] = "/stale/worktree/.envrc"
        result = self.invoke("check", "", "sample with spaces")
        self.assertEqual(result.returncode, 37, result.stdout + result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(self.direnv_calls(), [])
        self.assertEqual([call[1] for call in self.helper_calls()], ["exec"])
        self.assertEqual(self.verification_start()["nix_shell"], "")
        self.assertEqual(self.verification_start()["args"], ["check", "", "sample with spaces"])
        self.assertEqual(self.verification_start()["envrc"], "")
        self.assertEqual(self.verification_start()["keep"], "caller")
        self.assertEqual(self.verification_start()["loop_marker"], "")

    def test_existing_shell_is_silent_and_never_calls_the_helper(self) -> None:
        _ = self.envrc(self.work)
        self.env["IN_NIX_SHELL"] = "pure"
        self.env["DIRENV_FAIL"] = "1"
        result = self.invoke("doc")
        self.assertEqual(result.returncode, 37, result.stdout + result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(self.direnv_calls(), [])
        self.assertEqual(self.helper_calls(), [])
        self.assertEqual(self.verification_start()["nix_shell"], "pure")

    def test_reexec_losing_proof_refuses_without_another_exec(self) -> None:
        _ = self.envrc(self.work)
        startup = self.root / "drop-proof.sh"
        _ = startup.write_text("unset IN_NIX_SHELL\n", encoding="utf-8")
        self.env["DIRENV_DIFF"] = json.dumps({"IN_NIX_SHELL": "impure", "BASH_ENV": str(startup)})
        result = self.invoke("doc")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(result.stderr, LOOP_REFUSAL)
        self.assertEqual(result.stdout, "")
        self.assertFalse(self.setup_log.exists())
        self.assertEqual([call[1] for call in self.helper_calls()], ["exec"])
        self.assertEqual(sum(call["args"] == ["export", "json"] for call in self.direnv_calls()), 1)

    def test_user_clock_loads_the_configured_zone_without_site(self) -> None:
        source = VERIFY.read_text(encoding="utf-8")
        function = source[source.index("user_clock() {"):].split("\n}", 1)[0] + "\n}\n"
        clock = self.verify_script.with_name("clock.sh")
        _ = clock.write_text(function + "user_clock\n", encoding="utf-8")
        (self.verify_script.parent.parent / "lib/user_zone.py").symlink_to(SCRIPTS / "lib/user_zone.py")
        self.script(self.bin / "python3", PYTHON_SHIM)
        self.script(self.bin / "date", 'import os\nprint(os.environ["TZ"])\n')
        configuration = self.root / "user_zone.conf"
        _ = configuration.write_text("zone=Europe/Paris\n", encoding="utf-8")
        result = subprocess.run(["bash", str(clock)], cwd=self.work,
                                env={**self.env, "PY": str(self.bin / "python3"),
                                     "USER_ZONE_CONF": str(configuration)},
                                capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "Europe/Paris\n")
        calls = self.python_calls()
        self.assertEqual(calls, [["-S", str(clock.parent / "../lib/user_zone.py")]])


class VerifyScriptPinTests(NixFixture):
    source: Path = Path()
    temporary: Path = Path()
    helper: Path = Path()

    @override
    def setUp(self) -> None:
        super().setUp()
        self.source = self.root / "script repository"
        scripts = self.source / "scripts"
        direct = scripts / "direct"
        direct.mkdir(parents=True)
        _ = shutil.copy2(VERIFY, direct / "verify.sh")
        _ = shutil.copytree(SCRIPTS / "lib", scripts / "lib", ignore=shutil.ignore_patterns("__pycache__"))
        _ = shutil.copytree(SCRIPTS / "build_hold", scripts / "build_hold", ignore=shutil.ignore_patterns("__pycache__"))
        self.helper = scripts / "copies/copies.py"
        self.script(self.helper, 'import sys\nassert sys.argv[1:] == ["--capacity-file"]\nprint("old helper")\n')
        invoke = scripts / "lint/invoke.sh"
        invoke.parent.mkdir()
        _ = invoke.write_text(
            'if [[ ${1:-} == --prepare-temp ]]; then return 0; fi\n'
            + 'printf "ready\\t%s\\t%s\\n" "${VERIFY_SCRIPT_PIN:-}" "${VERIFY_SCRIPT_COMMIT:-}"\n'
            + 'if [[ ${VERIFY_FIXTURE_WAIT:-} == 1 ]]; then IFS= read -r release; fi\n'
            + '"$PY" -c \'from scripts.lib import conf; print(conf.read_or_repository("pin_fixture").text("label"))\'\n'
            + '"$PY" "$BUILD_HELPER_ROOT/copies/copies.py" --capacity-file\n'
            + 'exit $?\n',
            encoding="utf-8",
        )
        config = self.source / "config"
        config.mkdir()
        _ = (config / "pin_fixture.conf").write_text("label=original configuration\n", encoding="utf-8")
        _ = (self.work / "Cargo.toml").write_text("[workspace]\n")
        self.temporary = self.root / "tmp"
        self.temporary.mkdir()
        self.env.update({"IN_NIX_SHELL": "pure", "TMPDIR": str(self.temporary),
                         "CLAUDE_PYTHON": sys.executable, "GIT_CONFIG_NOSYSTEM": "1",
                         "GIT_CONFIG_GLOBAL": "/dev/null"})
        _ = self.git("init", "-q")
        _ = self.git("add", "scripts")
        _ = self.git("commit", "-qm", "Initial Verify scripts")

    def git(self, *arguments: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(self.source), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
             *arguments],
            env=self.env, capture_output=True, text=True, check=True, timeout=TEST_HANG_GUARD_SECS,
        )
        return result.stdout.strip()

    def verify_call(self) -> list[str]:
        return ["bash", str(self.source / "scripts/direct/verify.sh"), "check", "sample"]

    def test_waiting_verify_keeps_its_helper_after_an_install(self) -> None:
        # Give inline Python another checkout to find in its working directory.
        (self.work / "scripts/lib").mkdir(parents=True)
        (self.work / "scripts/lib/py").touch()
        commit = self.git("rev-parse", "HEAD")
        with subprocess.Popen(
            self.verify_call(), cwd=self.work, env={**self.env, "VERIFY_FIXTURE_WAIT": "1"},
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        ) as run:
            try:
                assert run.stdout is not None
                readable, _, _ = select.select([run.stdout], [], [], TEST_HANG_GUARD_SECS)
                self.assertTrue(readable, "Verify did not reach the wait")
                line = cast(str, run.stdout.readline())
                ready, folder, pinned_commit = line.rstrip("\n").split("\t")
                self.assertEqual(ready, "ready")
                self.assertEqual(pinned_commit, commit)
                copy = Path(folder)
                self.assertEqual(copy.parent, self.temporary)
                self.assertFalse((copy / "config").exists())
                self.script(self.helper, 'import sys\nassert sys.argv[1:] == []\nprint("new helper")\n')
                _ = self.git("add", "scripts")
                _ = self.git("commit", "-qm", "Remove the old argument")
                _ = (self.source / "config/pin_fixture.conf").write_text("label=live configuration\n", encoding="utf-8")
                output, errors = run.communicate("release\n", timeout=TEST_HANG_GUARD_SECS)
                self.assertEqual(run.returncode, 0, output + errors)
                self.assertEqual(output, "live configuration\nold helper\n")
                self.assertEqual(errors, "")
                self.assertFalse(copy.exists())
            finally:
                if run.poll() is None:
                    run.kill()

    def test_remote_probe_uses_live_scripts_with_and_without_tracing(self) -> None:
        scripts = self.source / "scripts"
        other_checkout = self.work / "scripts/lib"
        other_checkout.mkdir(parents=True)
        (other_checkout / "py").touch()
        _ = (other_checkout / "trace_fixture.py").write_text('SOURCE = "working checkout"\n')
        _ = (scripts / "lib/trace_fixture.py").write_text('SOURCE = "selected checkout"\n')
        target = scripts / "build_targets/build_targets.py"
        self.script(target, 'raise SystemExit("the remote probe used the pin")\n')
        invoke = scripts / "lint/invoke.sh"
        _ = invoke.write_text('''if [[ ${1:-} == --prepare-temp ]]; then return 0; fi
[[ -n "$VERIFY_SCRIPT_PIN" ]] || exit 98
LIVE_BUILD_TARGETS_SCRIPT="$VERIFY_LIVE_HELPER_ROOT/build_targets/build_targets.py"
BUILD_TARGETS_SCRIPT="$BUILD_HELPER_ROOT/build_targets/build_targets.py"
cat > "$LIVE_BUILD_TARGETS_SCRIPT" <<'PYTHON'
import os
from pathlib import Path
import sys
from scripts.lib import trace_fixture
assert not any(key in os.environ for key in ("VERIFY_SCRIPT_PIN", "VERIFY_SCRIPT_REPO", "VERIFY_SCRIPT_COMMIT"))
assert sys.argv[1:] == ["probe-backoff", "--target", "mac"]
assert all(Path(entry).resolve() != Path.cwd() for entry in sys.path)
assert trace_fixture.SOURCE == "selected checkout"
assert Path(trace_fixture.__file__).resolve().parents[1] == Path(__file__).resolve().parents[1]
print("live probe")
PYTHON
verify_helper "$BUILD_TARGETS_SCRIPT" probe-backoff --target mac
(
    VERIFY_PY_HELPER=INLINE_PROBE exec_verify_live_python -c '
from pathlib import Path
import os
import sys
from scripts.lib import trace_fixture
assert "VERIFY_PY_ENTRY" not in os.environ
assert all(Path(entry).resolve() != Path.cwd() for entry in sys.path)
assert trace_fixture.SOURCE == "selected checkout"
assert Path(trace_fixture.__file__).resolve().parents[1] == Path(sys.argv[1]).resolve().parents[1]
print("inline live probe")
' "$LIVE_BUILD_TARGETS_SCRIPT"
)
exit $?
''')
        _ = self.git("add", "scripts")
        _ = self.git("commit", "-qm", "Add the remote probe fixture")

        trace = self.root / "verify-trace"
        self.env["PYTHONDONTWRITEBYTECODE"] = "1"
        for tracing in (False, True):
            with self.subTest(tracing=tracing):
                _ = self.git("restore", "--", str(target))
                env = {**self.env, "VERIFY_PY_TRACE": str(trace)} if tracing else self.env
                result = subprocess.run(self.verify_call(), cwd=self.work, env=env, text=True, capture_output=True,
                                        check=False, timeout=TEST_HANG_GUARD_SECS)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(result.stdout, "live probe\ninline live probe\n")
                self.assertEqual(list(self.temporary.glob("verify-scripts.*")), [])
                if tracing:
                    helpers = [line.split("\t")[0] for line in trace.read_text().splitlines()]
                    self.assertIn("build_targets.py", helpers)
                    self.assertIn("INLINE_PROBE", helpers)

    def test_cleanup_lock_failure_reports_the_retained_copy(self) -> None:
        self.script(self.bin / "flock", 'raise SystemExit("fixture lock failure")\n')
        result = subprocess.run(self.verify_call(), cwd=self.work, env=self.env, text=True, capture_output=True,
                                check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        copy, = self.temporary.glob("verify-scripts.*")
        self.assertTrue((copy / ".removing").is_file())
        self.assertEqual(result.stderr, f"kept for a manual removal list: {copy} (verify, cleanup lock or removal failed)\n")

    def test_signal_during_cleanup_lock_reports_the_retained_copy(self) -> None:
        ready = self.root / "lock-ready"
        os.mkfifo(ready)
        reader = os.open(ready, os.O_RDWR | os.O_NONBLOCK)
        self.addCleanup(os.close, reader)
        self.script(self.bin / "flock", '''import os
from pathlib import Path
import signal
Path(os.environ["LOCK_READY"]).write_bytes(b"1")
signal.alarm(10)
signal.pause()
''')
        with subprocess.Popen(self.verify_call(), cwd=self.work, env={**self.env, "LOCK_READY": str(ready)},
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as run:
            self.assertTrue(select.select([reader], [], [], TEST_HANG_GUARD_SECS)[0])
            self.assertEqual(os.read(reader, 1), b"1")
            run.send_signal(signal.SIGTERM)
            _, errors = run.communicate(timeout=TEST_HANG_GUARD_SECS)
        copy, = self.temporary.glob("verify-scripts.*")
        self.assertTrue((copy / ".removing").is_file())
        self.assertEqual(errors, f"kept for a manual removal list: {copy} (verify, cleanup interrupted by SIGTERM)\n")

    def test_pinned_helper_removes_its_verify_tree_after_install(self) -> None:
        scripts = self.source / "scripts"
        for name in ("verify_helper.py", "file_lock.sh"):
            _ = shutil.copy2(VERIFY.with_name(name), scripts / "direct" / name)
        installed = self.home / ".claude"
        installed.mkdir()
        (installed / "scripts").symlink_to(scripts, target_is_directory=True)
        live = installed / "scripts/lib/build_folders.py"
        original = live.read_text()
        main = "def main(argv: Sequence[str]) -> int:\n"
        self.assertIn(main, original)
        traced = original.replace(main, main + '    if argv[:1] == ["remove"]:\n        print(__file__)\n', 1)
        _ = live.write_text(traced)
        _ = (scripts / "lint/invoke.sh").write_text(
            'if [[ ${1:-} == --prepare-temp ]]; then return 0; fi\n'
            + shell_functions(VERIFY, "finish_tree_watch")
            + '''stop_tree_watch() { :; }
cancel_tree_run() { :; }
BUILD_FOLDERS_SCRIPT="$BUILD_HELPER_ROOT/lib/build_folders.py"
verify_helper --start
TREE_WATCH_DIR="$(verify_helper "$BUILD_FOLDERS_SCRIPT" tool --tool verify-tree --owner-pid "$$")"
printf '%s\\t%s\\n' "$VERIFY_SCRIPT_PIN" "$TREE_WATCH_DIR"
IFS= read -r release
finish_tree_watch
exit 0
''')
        _ = self.git("add", "scripts")
        _ = self.git("commit", "-qm", "Add the folder lease fixture")

        with subprocess.Popen(
            self.verify_call(), cwd=self.work, env=self.env, text=True,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        ) as run:
            try:
                assert run.stdout is not None
                readable, _, _ = select.select([run.stdout], [], [], TEST_HANG_GUARD_SECS)
                self.assertTrue(readable, "Verify did not acquire its folder lease")
                line = cast(str, run.stdout.readline())
                fields = line.rstrip("\n").split("\t")
                self.assertEqual(len(fields), 2, fields)
                copy, folder = map(Path, fields)
                pinned = copy / "scripts/lib/build_folders.py"
                self.assertEqual(copy.parent, self.temporary)
                self.assertEqual(pinned.read_text(), traced)
                lock = folder.with_name(folder.name + ".lock")
                lease = folder.with_name(folder.name + ".lease.json")
                self.assertTrue(lease.is_file())
                with lock.open("rb") as stream:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)

                replacement = live.with_suffix(".new")
                _ = replacement.write_text(traced + "\n# New installed revision.\n")
                _ = replacement.replace(live)
                _ = self.git("add", "scripts")
                _ = self.git("commit", "-qm", "Replace the installed folder helper")

                output, errors = run.communicate("release\n", timeout=TEST_HANG_GUARD_SECS)
                self.assertEqual(run.returncode, 0, output + errors)
                self.assertEqual(output, f"{pinned}\n")
                for path in (folder, lock, lease, copy):
                    self.assertFalse(path.exists(), path)
            finally:
                if run.poll() is None:
                    run.kill()

    def test_dirty_scripts_run_with_one_notice_and_no_copy(self) -> None:
        self.script(self.helper, 'print("dirty helper")\n')
        result = subprocess.run(self.verify_call(), cwd=self.work, env=self.env, capture_output=True,
                                text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stderr, "verify.sh: scripts are not pinned: the scripts tree has local changes\n")
        self.assertEqual(result.stdout, "ready\t\t\noriginal configuration\ndirty helper\n")
        self.assertEqual(list(self.temporary.iterdir()), [])

    def test_archive_failure_removes_the_copy_before_using_live_scripts(self) -> None:
        git = shutil.which("git")
        assert git is not None
        self.script(self.bin / "git",
                    'import os, sys\nif "archive" in sys.argv[1:]:\n    raise SystemExit(1)\n'
                    + f"os.execv({git!r}, [{git!r}, *sys.argv[1:]])\n")
        result = subprocess.run(self.verify_call(), cwd=self.work, env=self.env, capture_output=True,
                                text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stderr, "verify.sh: scripts are not pinned: could not copy the script tree\n")
        self.assertEqual(result.stdout, "ready\t\t\noriginal configuration\nold helper\n")
        self.assertEqual(list(self.temporary.iterdir()), [])

    def prepare_nix_reexec(self) -> None:
        for path in ("buildlog/store.py", "agents/worker_lifecycle.py"):
            destination = self.source / "scripts" / path
            destination.parent.mkdir(exist_ok=True)
            _ = shutil.copy2(SCRIPTS / path, destination)
        _ = self.git("add", "scripts")
        _ = self.git("commit", "-qm", "Add Nix helper dependencies")
        _ = self.env.pop("IN_NIX_SHELL")
        _ = self.envrc(self.work)

    def test_launcher_failure_removes_the_script_copy(self) -> None:
        self.prepare_nix_reexec()
        self.script(self.source / "scripts/lib/py", 'raise SystemExit("fixture Python launcher failure")\n')
        _ = self.git("add", "scripts")
        _ = self.git("commit", "-qm", "Add the failing Python launcher")
        result = subprocess.run(self.verify_call(), cwd=self.work, env=self.env, capture_output=True,
                                text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(result.stderr, "fixture Python launcher failure\n")
        self.assertEqual(list(self.temporary.iterdir()), [])

    def test_missing_tmpdir_is_created_before_pinning(self) -> None:
        invoke = self.source / "scripts/lint/invoke.sh"
        prepare = 'if [[ ${1:-} == --prepare-temp ]]; then source "$INVOKE_SETUP" --prepare-temp; return $?; fi'
        _ = invoke.write_text(invoke.read_text().replace(
            'if [[ ${1:-} == --prepare-temp ]]; then return 0; fi', prepare))
        _ = self.git("add", "scripts")
        _ = self.git("commit", "-qm", "Use the temporary folder setup")
        missing = self.root / "new session" / "tmp"
        _ = write_config_fixture(Path(self.env["CONF_DIR"]) / "build_folders.conf", "patterns.legacy-stable={purpose}\n")
        self.env.update(TMPDIR=str(missing), INVOKE_SETUP=str(SCRIPTS / "lint/invoke.sh"))

        result = subprocess.run(self.verify_call(), cwd=self.work, env=self.env, capture_output=True,
                                text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        ready, folder, commit = result.stdout.splitlines()[0].split("\t")
        self.assertEqual(ready, "ready")
        self.assertEqual(commit, self.git("rev-parse", "HEAD"))
        self.assertEqual(Path(folder).parent, missing)
        self.assertTrue(missing.is_dir())
        self.assertFalse(Path(folder).exists())

    def test_unset_tmpdir_uses_the_runtime_folder(self) -> None:
        _ = self.env.pop("TMPDIR")
        self.env["XDG_RUNTIME_DIR"] = str(self.temporary)
        result = subprocess.run(self.verify_call(), cwd=self.work, env=self.env, capture_output=True,
                                text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        _, folder, commit = result.stdout.splitlines()[0].split("\t")
        self.assertEqual(commit, self.git("rev-parse", "HEAD"))
        self.assertEqual(Path(folder).parent, self.temporary)
        self.assertFalse(Path(folder).exists())

    def test_script_copy_remains_until_background_sweep_ends(self) -> None:
        scripts = self.source / "scripts"
        ready_path, release_path, cleanup_path = (self.root / name for name in ("ready", "release", "cleanup"))
        for fifo in (ready_path, release_path, cleanup_path):
            os.mkfifo(fifo)
        ready, release, cleanup = (
            os.open(fifo, os.O_RDWR | os.O_NONBLOCK) for fifo in (ready_path, release_path, cleanup_path))
        for fd in (ready, release, cleanup):
            self.addCleanup(os.close, fd)
        self.env.update(SWEEP_READY=str(ready_path), SWEEP_RELEASE=str(release_path),
                        SWEEP_CLEANUP=str(cleanup_path), SWEEP_RESULT=str(self.root / "sweep-result"),
                        XDG_RUNTIME_DIR=str(self.temporary))
        (self.temporary / "lint-sweep").mkdir()
        flock = shutil.which("flock")
        assert flock is not None
        self.script(self.bin / "flock", '''import os, sys
from pathlib import Path
if sys.argv[1] == "-x":
    Path(os.environ["SWEEP_CLEANUP"]).write_text("1")
''' + f"os.execv({flock!r}, [{flock!r}, *sys.argv[1:]])\n")
        self.script(self.bin / "systemd-cat", "import sys\nsys.stdin.buffer.read()\n")
        self.script(scripts / "lint/sweep.py", '''import os
from pathlib import Path
Path(os.environ["SWEEP_READY"]).write_text("1")
with Path(os.environ["SWEEP_RELEASE"]).open() as release:
    release.read(1)
pin = Path(__file__).parents[2]
Path(os.environ["SWEEP_RESULT"]).write_bytes((pin / ".verify-source").read_bytes())
''')
        _ = (scripts / "lint/invoke.sh").write_text(
            'if [[ ${1:-} == --prepare-temp ]]; then return 0; fi\n'
            + shell_functions(SCRIPTS / "lint/invoke.sh", "sweep_after_step", "invoke_sweep")
            + '''run_once() { "$@"; }
lint_config_enabled() { return 0; }
SWEEP_EVERY_S=0
sweep_after_step cargo check
exit 37
''')
        _ = self.git("add", "scripts")
        _ = self.git("commit", "-qm", "Add the background sweep fixture")
        with subprocess.Popen(self.verify_call(), cwd=self.work, env=self.env, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE) as run:
            try:
                for fd in (ready, cleanup):
                    self.assertTrue(select.select([fd], [], [], TEST_HANG_GUARD_SECS)[0], "The sweep or cleanup did not start")
                    self.assertEqual(os.read(fd, 1), b"1")
                copy, = self.temporary.glob("verify-scripts.*")
                self.assertTrue((copy / ".removing").is_file())
                stamp = (copy / ".verify-source").read_bytes()
                with (copy / ".sweep-lock").open("rb") as lock:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                _ = os.write(release, b"1")
                output, errors = run.communicate(timeout=TEST_HANG_GUARD_SECS)
                self.assertEqual(run.returncode, 37, output + errors)
                self.assertEqual(Path(self.env["SWEEP_RESULT"]).read_bytes(), stamp)
                self.assertFalse(copy.exists())
            finally:
                if run.poll() is None:
                    _ = os.write(release, b"1")
                    run.kill()

    def test_nix_refusal_removes_the_script_copy(self) -> None:
        self.prepare_nix_reexec()
        self.env["DIRENV_FAIL"] = "1"
        result = subprocess.run(self.verify_call(), cwd=self.work, env=self.env, capture_output=True,
                                text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(result.stderr, f"nix_env: {self.work}: last failure detail\n", result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(list(self.temporary.iterdir()), [])

    def test_signals_during_nix_setup_remove_the_script_copy(self) -> None:
        self.prepare_nix_reexec()
        ready = self.root / "direnv-ready"
        os.mkfifo(ready)
        reader = os.open(ready, os.O_RDWR | os.O_NONBLOCK)
        self.addCleanup(os.close, reader)
        self.script(self.bin / "direnv", "import os, sys\nfrom pathlib import Path\n"
                    + "if sys.argv[1:2] == ['export']:\n"
                    + "    Path(os.environ['SETUP_READY']).write_text('1')\n"
                    + "    sys.stdin.read(1)\n")
        for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            with self.subTest(number=number), subprocess.Popen(
                self.verify_call(), cwd=self.work, env={**self.env, "SETUP_READY": str(ready)},
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            ) as run:
                try:
                    readable, _, _ = select.select([reader], [], [], TEST_HANG_GUARD_SECS)
                    self.assertTrue(readable, "Nix setup did not reach direnv")
                    self.assertEqual(os.read(reader, 1), b"1")
                    copy, = self.temporary.glob("verify-scripts.*")
                    run.send_signal(number)
                    self.assertEqual(run.wait(timeout=TEST_HANG_GUARD_SECS), 128 + number)
                    self.assertFalse(copy.exists())
                finally:
                    if run.poll() is None:
                        run.kill()

    def test_removed_example_test_refuses_before_the_archive(self) -> None:
        self.script(self.helper, "raise SystemExit(91)\n")
        result = subprocess.run([*self.verify_call()[:2], "example-test", "sample", "demo"],
                                cwd=self.work, env=self.env, capture_output=True, text=True,
                                check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(result.stderr,
                         "verify.sh: example-test is removed: examples carry no tests (user, 2026-10-04).\n")
        self.assertEqual(list(self.temporary.iterdir()), [])


if __name__ == "__main__":
    _ = unittest.main()
