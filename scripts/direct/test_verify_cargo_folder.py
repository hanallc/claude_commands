"""Check the Cargo folder in verify's placement request."""
from __future__ import annotations

import json
import subprocess
import sys
from typing import cast

from scripts.direct import verify_token_wait_test_support as support
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.lint.memory_test_support import write_config_fixture


class VerifyCargoFolderTests(support.VerifyTokenWaitFixture):
    def test_first_step_passes_the_build_folder_only_for_local_placement(self) -> None:
        output = self.root / "cargo build output"
        output.mkdir()
        alias = self.root / "build alias"
        alias.symlink_to(output, target_is_directory=True)
        environment = self.build_stub([{"name": "sample-app", "kind": ["bin"]}])
        metadata = cast(dict[str, object], json.loads(environment["TEST_METADATA"]))
        metadata.update(workspace_root=str(self.root), build_directory=str(alias))
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
        environment.update(TEST_METADATA=json.dumps(metadata), BUILDLOG_FIRST_STEP_REQUEST=str(request_file),
                           BASH_ENV=str(shell), TEST_ADMISSION_CLIENT=str(client))
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
        for target in ("natedev", "mac"):
            with self.subTest(target=target):
                request_file.unlink(missing_ok=True)
                result = self.build(environment, "--target", target)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                request = cast(list[str], json.loads(request_file.read_text()))
                self.assertEqual(request[0], "place-step")
                if target == "natedev":
                    folder_arg = request.index("--cargo-folder")
                    self.assertEqual(request[folder_arg + 1], str(output.resolve()))
                else:
                    self.assertNotIn("--cargo-folder", request)
                self.assertFalse((self.root / "cargo-calls").exists())

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
        self.assertEqual(result.stdout, output + "\n")
        self.assertEqual((self.root / "metadata-calls").read_text().splitlines(),
                         ["+nightly", "metadata", "--format-version", "1", "--no-deps", "--manifest-path", "other/Cargo.toml"])
