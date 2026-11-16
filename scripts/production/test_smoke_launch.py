"""Smoke builds use the checkout configuration from any caller folder."""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from scripts.lib.py_floor_test_support import interpreter_cache_environment
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.lib.git_test_support import commit_repository
from scripts.lint.memory_test_support import config_environment, write_config_fixture


COMMAND = Path(__file__).with_name("smoke_launch.sh")


class SmokeLaunchTests(unittest.TestCase):
    def test_both_cargo_calls_use_the_checkout_from_an_external_caller(self) -> None:
        self.assert_checkout_cargo(relative=False)

    def test_relative_checkout_uses_absolute_paths_for_both_cargo_calls(self) -> None:
        self.assert_checkout_cargo(relative=True)

    def test_launch_uses_the_app_gate(self) -> None:
        self.assert_checkout_cargo(relative=False, launch=True)

    def test_queue_time_does_not_consume_the_readiness_deadline(self) -> None:
        self.assert_checkout_cargo(relative=False, launch=True, queued=True)

    def test_cleanup_stops_the_saved_scope_after_the_supervisor_exits(self) -> None:
        self.assert_checkout_cargo(relative=False, launch=True, supervisor_exits=True)

    def test_admission_refusal_reports_its_reason(self) -> None:
        self.assert_checkout_cargo(relative=False, launch=True, refused=True)

    def assert_checkout_cargo(self, *, relative: bool, launch: bool = False,
                              queued: bool = False, refused: bool = False, supervisor_exits: bool = False) -> None:
        with tempfile.TemporaryDirectory(prefix="smoke-launch-") as temporary:
            root = Path(temporary)
            checkout = root / "checkout with spaces"
            caller = root / "caller"
            programs = root / "programs"
            home = root / "home"
            for directory in (checkout, caller, programs, home):
                directory.mkdir()
            for name in ("bash", "git", "jq", "dirname", "date", "tee", "mktemp", "rm", "sleep", "setsid", "mkdir", "env"):
                executable = shutil.which(name)
                if executable is None:
                    self.fail(f"The fixture needs {name}")
                (programs / name).symlink_to(executable)

            records = root / "commands.tsv"
            target = root / "missing target"
            environment = {
                **config_environment(root / "config"),
                "PATH": str(programs),
                "HOME": str(home),
                **interpreter_cache_environment(root / "cache"),
                "USER_ZONE": "UTC",
                "GIT_CONFIG_NOSYSTEM": "1",
                "SMOKE_TEST_RECORDS": str(records),
                "SMOKE_TEST_TARGET": str(target),
                "HANA_TEST_PURPOSE": "smoke feature",
                "HANA_TEST_OWNER": "smoke owner",
                "SMOKE_TEST_READY": str(root / "ready"),
                "SMOKE_TEST_STOP": str(root / "stop"),
                "TMPDIR": str(root),
            }

            _ = write_config_fixture(root / "config/production.conf",
                                     "smoke_ready_seconds=1\nsmoke_poll_seconds=1\n")

            def run(arguments: list[str], folder: Path) -> subprocess.CompletedProcess[str]:
                return subprocess.run(arguments, cwd=folder, env=environment, text=True,
                                      capture_output=True, check=False, timeout=TEST_HANG_GUARD_SECS)

            initialized = run(["git", "init", "--template=", str(checkout)], caller)
            self.assertEqual(initialized.returncode, 0, initialized.stdout + initialized.stderr)
            head = commit_repository(checkout, "Seed the smoke checkout", allow_empty=True, env=environment)

            commands = {
                "uname": '''printf 'uname\t%s\t%s\n' "$PWD" "$*" >> "$SMOKE_TEST_RECORDS"
printf 'Linux\n'
''',
                "direnv": '''printf 'direnv\t%s\t%s\n' "$PWD" "$*" >> "$SMOKE_TEST_RECORDS"
[[ "$1" == exec ]] || exit 2
shift 2
exec "$@"
''',
                "cargo": '''printf 'cargo\t%s\t%s\n' "$PWD" "$*" >> "$SMOKE_TEST_RECORDS"
case "$1" in
    build) exit 0 ;;
    metadata) printf '{"target_directory":"%s"}\n' "$SMOKE_TEST_TARGET" ;;
    *) exit 2 ;;
esac
''',
                # The fixture never connects to a port.
                "curl": "exit 7\n",
            }
            for name, body in commands.items():
                program = programs / name
                _ = program.write_text("#!/usr/bin/env bash\nset -euo pipefail\n" + body)
                program.chmod(0o755)

            command = COMMAND
            if launch:
                scripts = root / "scripts"
                (scripts / "production").mkdir(parents=True)
                (scripts / "build_queue").mkdir()
                (scripts / "lib").mkdir()
                for source in (COMMAND.parent.parent / "lib").iterdir():
                    (scripts / "lib" / source.name).symlink_to(source)
                command = scripts / "production/smoke_launch.sh"
                _ = command.write_text(COMMAND.read_text())
                gate = scripts / "build_queue/app_gate.py"
                _ = gate.write_text("import os, sys, time\nfrom pathlib import Path\n"
                    + "if sys.argv[1] == 'stop':\n"
                    + "    scope_file = Path(sys.argv[sys.argv.index('--scope-file') + 1])\n"
                    + "    if scope_file.exists():\n"
                    + "        scope = Path(scope_file.read_text())\n"
                    + "        _ = (scope / 'cgroup.kill').write_text('1')\n"
                    + f"        with open({str(records)!r}, 'a') as f: _ = f.write('scope-stop\\t' + os.getcwd() + '\\t' + str(scope) + '\\n')\n"
                    + "    raise SystemExit(0)\n"
                    + f"with open({str(records)!r}, 'a') as f:\n"
                    + "    _ = f.write('gate\\t' + os.getcwd() + '\\t' + repr(sys.argv) + '\\n')\n"
                    + "pid = Path(sys.argv[sys.argv.index('--pid-file') + 1])\n"
                    + "status = pid.with_suffix('.status')\n"
                    + "_ = status.write_text('waiting for memory: fixture needs 6 GiB')\n"
                    + ("time.sleep(2)\n" if queued else "")
                    + ("_ = status.write_text('app_gate: fixture policy refuses this app')\nraise SystemExit(2)\n" if refused else "")
                    + (f"scope = Path({str(root / 'app.scope')!r})\nscope.mkdir()\n"
                       + "_ = (scope / 'cgroup.kill').write_text('0')\n"
                       + "_ = (scope / 'cgroup.procs').write_text('424244')\n"
                       + "_ = pid.with_suffix('.cgroup').write_text(str(scope))\n"
                       + "_ = status.write_text('app_gate: supervisor exited')\n"
                       + "raise SystemExit(2)\n" if supervisor_exits else "")
                    + "_ = pid.write_text(str(os.getpid()))\n"
                    + "argv = sys.argv[sys.argv.index('--') + 1:]\nos.execvp(argv[0], argv)\n")
                (target / "debug").mkdir(parents=True)
                app = target / "debug/hana"
                _ = app.write_text("#!/usr/bin/env bash\ntouch \"$SMOKE_TEST_READY\"\n"
                    + "while [[ ! -e \"$SMOKE_TEST_STOP\" ]]; do sleep 0.01; done\n")
                app.chmod(0o755)
                executable = shutil.which("touch")
                assert executable is not None
                (programs / "touch").symlink_to(executable)
                _ = (programs / "curl").write_text("#!/usr/bin/env bash\n"
                    + "if [[ $* == *brp_extras/shutdown* ]]; then touch \"$SMOKE_TEST_STOP\"; fi\n"
                    + "[[ -e \"$SMOKE_TEST_READY\" ]]\n")

            log = root / "smoke.log"
            argument = str(Path("..") / checkout.name) if relative else str(checkout)
            result = run(["bash", str(command), argument, head, str(log)], caller)
            self.assertEqual(result.returncode, 0 if launch and not refused and not supervisor_exits else 1, result.stdout + result.stderr + log.read_text())
            if supervisor_exits:
                self.assertIn("supervisor exited", result.stdout)
                self.assertEqual((root / "app.scope/cgroup.kill").read_text(), "1")
            elif refused:
                self.assertIn("fixture policy refuses this app", result.stdout)
            elif launch:
                self.assertIn("SMOKE PASSED", result.stdout)
                if queued:
                    self.assertIn("waiting for memory: fixture needs 6 GiB", result.stdout)
            else:
                failure = f"SMOKE FAILED: no binary at {target}/debug/hana"
                self.assertIn(failure, result.stdout)
                self.assertIn(failure, log.read_text())
            calls = [line.split("\t", 2) for line in records.read_text().splitlines()]
            cargo = [call for call in calls if call[0] == "cargo"]
            self.assertEqual(cargo, [
                ["cargo", str(checkout),
                 f"build --manifest-path {checkout}/Cargo.toml -p hana --bin hana"],
                ["cargo", str(checkout),
                 f"metadata --no-deps --format-version 1 --manifest-path {checkout}/Cargo.toml"],
            ])
            self.assertEqual([call[1] for call in calls if call[0] == "direnv"],
                             [str(checkout), str(checkout), *([str(caller)] if launch and not refused and not supervisor_exits else [])])
            if launch:
                gate_calls = [call for call in calls if call[0] == "gate"]
                self.assertEqual(len(gate_calls), 1)
                self.assertIn("smoke feature", gate_calls[0][2])
                self.assertIn("smoke owner", gate_calls[0][2])
                if supervisor_exits:
                    stopped = [call for call in calls if call[0] == "scope-stop"]
                    self.assertEqual(stopped, [["scope-stop", str(caller), str(root / "app.scope")]])
            self.assertEqual([call[1] for call in calls if call[0] == "uname"], [str(caller)])


if __name__ == "__main__":
    _ = unittest.main()
