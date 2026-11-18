"""Check the Cargo folder in verify's placement request."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import cast
from unittest import mock

from scripts.build_queue import admission_protocol as wire, admission_service as service
from scripts.build_queue.admission_test_support import caller_admission

from scripts.direct import verify_token_wait_test_support as support
from scripts.lib.shell_test_support import shell_functions
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.lint.memory_test_support import write_config_fixture


class VerifyCargoFolderTests(support.VerifyTokenWaitFixture):
    def capture_first_request(self, environment: dict[str, str]) -> Path:
        client = self.root / "record_admission_client.py"
        _ = client.write_text("""\
import json
import sys
from pathlib import Path

def main(argv):
    output = argv[argv.index("--request-file") + 1]
    Path(output).write_text(json.dumps(argv))
    return 0
""")
        shell = self.root / "load-client.sh"
        _ = shell.write_text("""\
source() {
    builtin source "$@"
    local status=$?
    if [[ "$1" == */memory_gate.sh ]]; then
        BUILDLOG_MEM_ADMIT_SCRIPT=$TEST_ADMISSION_CLIENT
        unset -f source
    fi
    return "$status"
}
""")
        request_file = self.root / "request-argv.json"
        environment.update(BUILDLOG_FIRST_STEP_REQUEST=str(request_file),
                           BASH_ENV=str(shell), TEST_ADMISSION_CLIENT=str(client))
        return request_file

    def configure_targets(self) -> None:
        _ = (self.root / "build_targets.conf").write_text(
            """\
targets=natedev,mac
kind.natedev=local
platform.natedev=linux
kind.mac=ssh
platform.mac=macos
settings.mac=mac_test.conf
""")
        _ = write_config_fixture(self.root / "mac_test.conf", f"offload=on\nlinux_only.{self.root.name}=\n",
                                 template="mac_test")

    def test_first_step_passes_the_build_folder_even_with_a_remote_pin(self) -> None:
        output = self.root / "cargo build output"
        output.mkdir()
        alias = self.root / "build alias"
        alias.symlink_to(output, target_is_directory=True)
        environment = self.build_stub([{"name": "sample-app", "kind": ["bin"]}])
        metadata = cast(dict[str, object], json.loads(environment["TEST_METADATA"]))
        metadata.update(workspace_root=str(self.root), build_directory=str(alias))
        environment["TEST_METADATA"] = json.dumps(metadata)
        request_file = self.capture_first_request(environment)
        self.configure_targets()
        for target in ("natedev", "mac"):
            with self.subTest(target=target):
                request_file.unlink(missing_ok=True)
                result = self.build(environment, "--target", target)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                request = cast(list[str], json.loads(request_file.read_text()))
                self.assertEqual(request[0], "place-step")
                self.assertIn("--cargo-folder", request)
                folder_arg = request.index("--cargo-folder")
                self.assertEqual(request[folder_arg + 1], str(output.resolve()))
                self.assertFalse((self.root / "cargo-calls").exists())

    def prepare_lint_request(self) -> dict[str, str]:
        taplo = self.root / "stubs/taplo"
        _ = taplo.write_text("#!/bin/sh\nexit 0\n")
        taplo.chmod(0o755)
        return self.environment.copy()

    def test_env_prefixed_mend_judgment_passes_the_folder(self) -> None:
        environment = self.prepare_lint_request()
        request_file = self.capture_first_request(environment)
        result = subprocess.run(
            ["bash", str(support.VERIFY), "lint", "--check", "sample"], cwd=self.root,
            env=environment, capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        request = cast(list[str], json.loads(request_file.read_text()))
        self.assertIn("--fail-on-warn", request)
        self.assertEqual(request[request.index("--") + 1:][:3], ["env", "RUSTC_WRAPPER=", "cargo"])
        self.assertIn("--cargo-folder", request)
        self.assertEqual(request[request.index("--cargo-folder") + 1], str(self.root / "target"))

    def test_mend_repair_passes_the_folder_despite_remote_flags(self) -> None:
        self.configure_targets()
        environment = self.prepare_lint_request()
        request_file = self.capture_first_request(environment)
        for flags in (("--mac", "fixture"), ("--target", "mac")):
            with self.subTest(flags=flags):
                request_file.unlink(missing_ok=True)
                result = subprocess.run(
                    ["bash", str(support.VERIFY), "lint", "sample", *flags], cwd=self.root,
                    env=environment, capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                request = cast(list[str], json.loads(request_file.read_text()))
                self.assertIn("--fix", request)
                self.assertIn("--writes-tree", request)
                self.assertIn("--cargo-folder", request)
                self.assertEqual(request[request.index("--cargo-folder") + 1], str(self.root / "target"))

    def test_metadata_error_refuses_placement(self) -> None:
        cargo = self.root / "stubs/cargo"
        _ = cargo.write_text(cargo.read_text().replace('#!/bin/sh\n', """\
#!/bin/sh
if [ "$*" = "metadata --format-version 1 --no-deps" ]; then
    echo 'error: cannot read the Cargo configuration' >&2
    exit 101
fi
if [ "$1" = metadata ]; then
    printf '%s\n' '{"packages":[{"name":"sample","targets":[{"name":"sample","kind":["lib"],"test":true}]}]}'
    exit 0
fi
""", 1))
        environment = self.environment.copy()
        request_file = self.capture_first_request(environment)
        result = self.verify(environment)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("error: cannot read the Cargo configuration", result.stderr)
        self.assertFalse(request_file.exists())
        self.assertFalse((self.root / "cargo-calls").exists())

    def cargo_with_configured_folders(self) -> Path:
        config = self.root / ".cargo/config.toml"
        config.parent.mkdir()
        cargo = self.root / "stubs/cargo"
        source = cargo.read_text()
        start = source.index("    printf '{")
        end = source.index('if [ "$1" = check ]; then', start)
        _ = cargo.write_text(source[:start] + r'''    target=$(sed -n 's/^target-dir = "\(.*\)"$/\1/p' "$PACKAGE_ROOT/.cargo/config.toml")
    build=$(sed -n 's/^build-dir = "\(.*\)"$/\1/p' "$PACKAGE_ROOT/.cargo/config.toml")
    printf '{"workspace_root":"%s","target_directory":"%s","build_directory":"%s","packages":[{"name":"sample","targets":[{"name":"sample","kind":["lib"],"test":true}]}]}\n' "$PACKAGE_ROOT" "$target" "$build"
    exit 0
fi
if [ -n "${TEST_BUILD_ALIAS:-}" ]; then ln -sfn "$TEST_BUILD_REPLACEMENT" "$TEST_BUILD_ALIAS"; fi
target=$(sed -n 's/^target-dir = "\(.*\)"$/\1/p' "$PACKAGE_ROOT/.cargo/config.toml")
build=$(sed -n 's/^build-dir = "\(.*\)"$/\1/p' "$PACKAGE_ROOT/.cargo/config.toml")
printf '%s\n' "${CARGO_TARGET_DIR:-$target}" "$(realpath -m "${CARGO_BUILD_BUILD_DIR:-$build}")" > "$PACKAGE_ROOT/cargo-folders"
printf '%s\n' "$*" >> "$CARGO_CALLS"
''' + source[end:])
        _ = self.environment.pop("CARGO_TARGET_DIR")
        return config

    def test_uses_the_first_grant_after_a_cargo_config_edit(self) -> None:
        config = self.cargo_with_configured_folders()
        old_target = self.root / "old-target"
        old_folder = self.root / "old-build"
        new_folder = self.root / "new-build"
        _ = config.write_text(f'[build]\ntarget-dir = "{old_target}"\nbuild-dir = "{old_folder}"\n')
        admission = caller_admission(self, self.root, self.environment)
        previous = admission.server.handle_request
        grants: list[str] = []

        def handle(request: wire.Request,
                   connection: service.Connection | service.NoConnection = service.NO_CONNECTION) -> list[wire.Reply]:
            replies = previous(request, connection)
            if isinstance(request, wire.CancelPlacement):
                admission.allow_next_start()
            for reply in replies:
                if isinstance(reply, wire.LocalPlacementGranted) and reply.assignment.episode not in grants:
                    grants.append(reply.assignment.episode)
                    _ = config.write_text(
                        f'[build]\ntarget-dir = "{self.root / "new-target"}"\nbuild-dir = "{new_folder}"\n')
            return replies

        with mock.patch.object(admission.server, "handle_request", side_effect=handle):
            result = self.verify(self.environment)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.root / "cargo-folders").read_text().splitlines(), [str(old_target), str(old_folder)])
        self.assertEqual(len(grants), 1)
        self.assertIn(str(new_folder), config.read_text())

    def test_pins_the_resolved_folder_after_the_build_symlink_changes(self) -> None:
        config = self.cargo_with_configured_folders()
        target = self.root / "target"
        build = self.root / "original-build"
        build.mkdir()
        replacement = self.root / "replacement-build"
        replacement.mkdir()
        alias = self.root / "build-alias"
        alias.symlink_to(build, target_is_directory=True)
        _ = config.write_text(f'[build]\ntarget-dir = "{target}"\nbuild-dir = "{alias}"\n')
        result = self.verify({**self.environment, "TEST_BUILD_ALIAS": str(alias),
                              "TEST_BUILD_REPLACEMENT": str(replacement)})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(alias.resolve(), replacement)
        self.assertEqual((self.root / "cargo-folders").read_text().splitlines(), [str(target), str(build)])

    def test_remote_handoff_places_again_for_a_local_grant(self) -> None:
        script = shell_functions(support.VERIFY, "place_compiling_step") + r"""
set -eu
CMD=check
BUILDLOG_MEM_ADMIT_SCRIPT=admission
BUILDLOG_MEM_WAIT_SINCE=200
PLACEMENT_SINCE=100
PLACEMENT_CARGO_FOLDER=$PWD
PLACEMENT_CARGO_TARGET=$PWD
PLACEMENT_EPISODE=parent-episode
placements=0
route_compiling_step() {
    placements=$((placements + 1))
    STEP_RAN_ON_MAC=0
    BUILDLOG_SELECTED_LOCAL_GRANT=grant
    case "$placements" in
        1) PLACEMENT_OWNER_PID=42; echo inherited ;;
        2)
            [[ -z "$PLACEMENT_EPISODE" && "$BUILDLOG_MEM_WAIT_SINCE" == 100 ]] || return 1
            PLACEMENT_OWNER_PID=$$
            echo fresh
            ;;
        *) return 1 ;;
    esac
}
verify_helper() {
    [[ "$2" == cancel-placement && "$3" == --episode && "$4" == parent-episode ]]
    [[ "$5" == --pid && "$6" == 42 ]]
    printf 'cancelled\n' >&2
}
place_compiling_step cargo check
"""
        result = subprocess.run(["bash", "-c", script], cwd=self.root, env=self.environment,
                                capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.splitlines(), ["inherited", "fresh"])
        self.assertEqual(result.stderr.splitlines(), ["cancelled"])

    def test_helper_uses_the_step_toolchain_and_manifest(self) -> None:
        output = str((self.root / "build output").resolve())
        cargo = self.root / "stubs" / "cargo"
        _ = cargo.write_text('#!/bin/bash\nprintf "%s\\n" "$@" > "$METADATA_CALLS"\nprintf "%s\\n" "$TEST_METADATA"\n')
        command = ["cargo", "+nightly", "clippy", "--manifest-path", "other/Cargo.toml"]
        metadata = json.dumps({"workspace_root": str(self.root), "target_directory": str(self.root / "target"),
                               "build_directory": output})
        result = subprocess.run(
            [sys.executable, str(support.VERIFY.with_name("verify_cargo_folder.py")), *command],
            cwd=self.root, env={**self.environment, "TEST_METADATA": metadata},
            capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), [str(self.root / "target"), output])
        self.assertEqual((self.root / "metadata-calls").read_text().splitlines(),
                         ["+nightly", "metadata", "--format-version", "1", "--no-deps", "--manifest-path", "other/Cargo.toml"])
