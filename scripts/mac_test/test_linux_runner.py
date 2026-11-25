"""Check Linux shell caching, execution, and retained claim cleanup."""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import redirect_stderr, redirect_stdout
import io
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
from typing import TypedDict, cast, override
import unittest
from unittest import mock

from scripts.lib import process_stat
from scripts.lib.helper_tree_test_support import COPY_HELPERS, copy_helpers

from scripts.lib.py_floor_test_support import interpreter_cache_environment
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.mac_test import eligibility, linux_runner, offload
from scripts.mac_test.offload_test_commands import WORKSPACE_SWEEP_SETTINGS
from scripts.lint.memory_test_support import UNREACHABLE_WAIT_FIXTURE, write_config_fixture


MISSING_PROCESS_CWD = Path()
BASH = shutil.which("bash") or "/bin/bash"


class ToolEvent(TypedDict):
    tool: str
    args: list[str]
    cwd: str
    inputs: list[str]
    environment: dict[str, str]
    warm: bool


# The fake tools are Bash: a Python start for each call cost most of this module's time.
TOOL_STUB = r'''
quote() {
    local value=$1
    value=${value//\\/\\\\}
    value=${value//\"/\\\"}
    value=${value//$'\n'/\\n}
    value=${value//$'\r'/\\r}
    value=${value//$'\t'/\\t}
    REPLY="\"$value\""
}
has_argument() {
    local wanted=$1 argument
    shift
    for argument in "$@"; do [[ $argument == "$wanted" ]] && return 0; done
    return 1
}
tool=${0##*/}
cd -P . || exit 1
arguments="" separator=""
for argument in "$@"; do quote "$argument"; arguments+="$separator$REPLY"; separator=", "; done
inputs=""
if [[ $tool == nix ]]; then
    for argument in "$@"; do
        [[ $argument == *"#"* ]] || continue
        folder=${argument%%#*}
        folder=${folder#path:}
        printf -v folder '%b' "${folder//%/\\x}"
        folder=${folder%/}
        [[ -d $folder ]] || continue
        inputs="" separator=""
        while IFS= read -r -d '' file; do
            [[ -f $file ]] || continue
            quote "${file#"$folder"/}"
            inputs+="$separator$REPLY"
            separator=", "
        done < <(find "$folder" \( -type f -o -type l \) -print0 | LC_ALL=C sort -z)
    done
fi
environment="" separator=""
for name in ${!BUILDLOG_@} ${!LINT_@} ${!TEST_SHELL_@} ${!RUSTUP_@}; do
    quote "$name"
    environment+="$separator$REPLY: "
    quote "${!name}"
    environment+=$REPLY
    separator=", "
done
warm=false
[[ -e target/warm-proof ]] && warm=true
quote "$tool"
event="{\"tool\": $REPLY, \"args\": [$arguments], "
quote "$PWD"
event+="\"cwd\": $REPLY, \"inputs\": [$inputs], \"environment\": {$environment}, \"warm\": $warm}"
printf '%s\n' "$event" >> "$TOOL_EVENTS"
case $tool in
nix)
    has_argument print-dev-env "$@" || { echo "Unexpected nix command" >&2; exit 1; }
    printf 'export IN_NIX_SHELL=1\nexport TEST_SHELL_PROOF=loaded\n%s\n' "${FAKE_NIX_EXPORTS-}"
    exit "${FAKE_NIX_STATUS:-0}"
    ;;
lint)
    target=${CARGO_TARGET_DIR:-target}
    mkdir -p "$target" || exit 1
    printf 'warm\n' > "$target/warm-proof"
    exit "${FAKE_LINT_STATUS:-0}"
    ;;
rustc)
    [[ $# == 1 && $1 == -V ]] || { echo "Unexpected rustc arguments: $*" >&2; exit 1; }
    printf 'rustc %s\n' "${TEST_SHELL_TOOL_VERSION-fixture}"
    ;;
rustup)
    [[ "$*" == "toolchain install --no-self-update --no-update" && $# == 4 ]] \
        || { echo "Unexpected rustup arguments: $*" >&2; exit 1; }
    if [[ -n ${FAKE_RUSTUP_FAIL-} ]]; then
        echo "error: toolchain download failed" >&2
        exit 1
    fi
    ;;
cargo)
    if [[ "$*" == "metadata --format-version 1 --no-deps" && $# == 4 ]]; then
        [[ -f Cargo.toml ]] || exit 101
        if [[ -n ${FAKE_CARGO_NEEDS_TOOLCHAIN-} && -z ${RUSTUP_TOOLCHAIN-} \
              && ! -f rust-toolchain && ! -f rust-toolchain.toml ]]; then
            echo "error: rustup could not choose a version of cargo to run" >&2
            exit 1
        fi
        if [[ -n ${FAKE_RUSTUP_FAIL-} ]]; then
            echo "error: toolchain is not installed" >&2
            exit 1
        fi
        quote "$PWD"
        workspace=$REPLY
        quote "$PWD/target"
        printf '{"workspace_root": %s, "target_directory": %s}\n' "$workspace" "$REPLY"
        exit 0
    fi
    echo "Unexpected cargo arguments: $*" >&2
    exit 1
    ;;
rm)
    if [[ ${FAKE_RM_FAIL-} == 1 ]]; then
        for argument in "$@"; do [[ $argument == */target ]] && exit 9; done
    fi
    exec -a rm "$TEST_REAL_RM" "$@"
    ;;
*)
    echo "Unexpected fake tool: $tool" >&2
    exit 1
    ;;
esac
'''


class LinuxShellTests(unittest.TestCase):
    root: Path = Path()
    source: Path = Path()
    mirror: Path = Path()
    settings: Path = Path()
    event_path: Path = Path()
    environment: dict[str, str] = {}
    identity: eligibility.KnownRepository = eligibility.KnownRepository("phase-copy", "https://example.com/team/hana")

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="linux-runner-")))
        self.source = self.root / "rust/phase-copy"
        self.source.mkdir(parents=True)
        self.settings = self.root / "linux.conf"
        _ = write_config_fixture(self.root / "mac_test.conf", template="mac_test")
        _ = write_config_fixture(self.settings,
            "offload=on\nhost=fake-linux\nkinds=test,check,doc\n"
            + "free_floor_gib=10\nbuild_budget_gib=32\ntarget_budget_gib=24\n"
            + "target_budget_gib.hana=31\ntarget_budget_gib.phase-copy=2\n"
            + "devshell.hana=ci\ndevshell.phase-copy=wrong\nskip.hana=test(gpu)\n"
            + "devshell=default\nshell_cache=.local/state/build-target/shells\n"
            + f"shell_timeout_s={TEST_HANG_GUARD_SECS}\n",
            template="mac_test",
        )
        registry = self.root / "build_targets.conf"
        _ = registry.write_text(
            "targets=linux,mac\nkind.linux=ssh\nplatform.linux=linux\nsettings.linux=linux.conf\n"
            + "kind.mac=ssh\nplatform.mac=macos\nsettings.mac=mac.conf\n" + UNREACHABLE_WAIT_FIXTURE
        )
        _ = (self.root / "mac.conf").write_text("offload=on\nhost=fake-mac\nlinux_only.phase-copy=\n")
        _ = (self.root / "lint.conf").write_text(WORKSPACE_SWEEP_SETTINGS)
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {
            "HOME": str(self.root), "XDG_CONFIG_HOME": str(self.root / ".config"),
            "BUILD_TARGETS_CONFIG": str(registry), "CONF_DIR": str(self.root),
        })))
        _ = self.enterContext(mock.patch.object(Path, "home", return_value=self.root))
        self.mirror = self.root / offload.remote_mirror(self.identity, "linux", 2)
        self.mirror.mkdir(parents=True)
        self.write_input("flake.nix", "{ outputs = _: {}; }\n")
        self.write_input("flake.lock", "{}\n")
        self.write_input("nix/shell.nix", "{}\n")
        self.write_input("src/main.rs", "fn main() {}\n")
        self.write_input(".env", "private fixture\n")
        self.write_input(".git/config", "private remote fixture\n")
        self.event_path = self.root / "events.jsonl"
        tools = self.root / "bin"
        tools.mkdir()
        for tool in ("nix", "rm", "rustup", "cargo"):
            self.write_tool(tools / tool)
        self.write_tool(self.root / ".claude/scripts/lint/lint")
        _ = copy_helpers(self.root, *COPY_HELPERS)
        _ = self.enterContext(mock.patch.object(linux_runner, "boot_identity", return_value="fixture-boot"))
        real_rm = shutil.which("rm")
        if real_rm is None:
            self.fail("The fixture needs the rm executable")
        self.environment = {
            **interpreter_cache_environment(self.root / "cache"),
            "HOME": str(self.root), "XDG_CONFIG_HOME": str(self.root / ".config"),
            "PATH": f"{tools}:/run/current-system/sw/bin:/usr/bin:/bin",
            "TEST_REAL_RM": real_rm,
            "TOOL_EVENTS": str(self.event_path), "BUILD_TARGETS_CONFIG": str(registry),
            "CONF_DIR": str(self.root),
            "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "lib/python_startup"),
            "BUILDLOG_DIR": str(self.root / "buildlog"),
            "LINT_SWEEP_STATE_DIR": str(self.root / "sweep-state"),
        }

    def write_tool(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(f"#!{BASH}\n" + TOOL_STUB)
        path.chmod(0o755)

    def write_input(self, name: str, contents: str) -> None:
        for root in (self.source, self.mirror):
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            _ = path.write_text(contents)

    def events(self, tool: str) -> list[ToolEvent]:
        if not self.event_path.exists():
            return []
        events = [cast(ToolEvent, json.loads(line)) for line in self.event_path.read_text().splitlines()]
        return [event for event in events if event["tool"] == tool]


    def test_toolchain_locks_follow_the_channel_across_trees(self) -> None:
        commands = self.root / "lock-commands"
        _ = commands.write_text('')
        flock = self.root / "bin/flock"
        _ = flock.write_text('#!/bin/bash\nprintf "%s\\n" "$(readlink /proc/$$/fd/9)" >> "$LOCK_COMMANDS"\n')
        flock.chmod(0o755)
        for name, channel in (("one", "1.99.0"), ("two", "1.99.0"), ("three", "1.99.1")):
            tree = self.root / name
            tree.mkdir()
            _ = (tree / "rust-toolchain.toml").write_text(f'[toolchain]\nchannel = "{channel}"\n')
            result = subprocess.run(["bash", "-c", linux_runner.toolchain_install_script("rustc 1.99.1")],
                cwd=tree, env={**self.environment, "LOCK_COMMANDS": str(commands)},
                capture_output=True, text=True, check=False, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
        locks = commands.read_text().splitlines()
        self.assertEqual(locks[0], locks[1])
        self.assertNotEqual(locks[1], locks[2])
        self.assertEqual(Path(locks[0]).name, "1.99.0.lock")

    def test_path_toolchain_uses_the_file_hash_as_its_lock_name(self) -> None:
        text = '[toolchain]\npath = "/toolchains/custom"\n'
        self.write_input("rust-toolchain.toml", text)
        result = subprocess.run(["bash", "-c", linux_runner.toolchain_install_script("rustc 1.99.0")],
            cwd=self.mirror, env=self.environment, capture_output=True, text=True, check=False, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        lock = self.root / ".rustup" / (hashlib.sha256(text.encode()).hexdigest() + ".lock")
        self.assertTrue(lock.exists())
        self.assertEqual(len(self.events("rustup")), 1)
        self.assertNotIn("RUSTUP_TOOLCHAIN", self.events("rustup")[0]["environment"])

    def test_toolchain_environment_wins_before_the_file_and_plain_version_fallback(self) -> None:
        for file, version, expected in (("", "rustc 1.99.0 (fixture today)", "1.99.0"),
                                        ("", "rustc 1.99.0-nightly (fixture today)", ""),
                                        ("rust-toolchain.toml", "rustc 1.99.1", ""),
                                        ("rust-toolchain", "rustc 1.99.1", "")):
            with self.subTest(file=file, version=version):
                for name in ("rust-toolchain", "rust-toolchain.toml"):
                    (self.mirror / name).unlink(missing_ok=True)
                if file:
                    _ = (self.mirror / file).write_text(
                        '[toolchain]\nchannel = "1.99.0"\n' if file.endswith(".toml") else '1.99.0\n')
                script = linux_runner.toolchain_install_script(version)
                script += '\nprintf "fallback=%s\\n" "${RUSTUP_TOOLCHAIN-unset}"\n'
                for inherited in (None, "nightly-2026-08-01"):
                    with self.subTest(inherited=inherited):
                        environment = dict(self.environment)
                        if inherited:
                            environment["RUSTUP_TOOLCHAIN"] = inherited
                        result = subprocess.run(["bash", "-c", script], cwd=self.mirror,
                            env=environment, capture_output=True, text=True, check=False, timeout=5)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertEqual(result.stdout, f"fallback={inherited or expected or 'unset'}\n")
                        if inherited:
                            self.assertTrue((self.root / ".rustup" / (inherited + ".lock")).exists())
                            self.assertEqual(self.events("rustup")[-1]["environment"]["RUSTUP_TOOLCHAIN"], inherited)


class LinuxProcessTests(unittest.TestCase):
    root: Path = Path()
    mirror: Path = Path()
    proc: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="linux-process-")))
        self.mirror = self.root / "mirror/1"
        self.mirror.mkdir(parents=True)
        self.proc = self.root / "proc"
        self.proc.mkdir()
        boot_file = self.proc / "sys/kernel/random/boot_id"
        boot_file.parent.mkdir(parents=True)
        _ = boot_file.write_text("fixture-boot\n")
        _ = self.enterContext(mock.patch.object(linux_runner, "boot_identity", return_value="fixture-boot"))
        _ = self.enterContext(mock.patch.object(os, "getpid", return_value=900))
        _ = self.enterContext(mock.patch.object(os, "getpgrp", return_value=900))
        _ = self.enterContext(mock.patch.object(Path, "home", return_value=self.root))

    def process(self, pid: int, *, state: str = "S", parent: int = 1, group: int = 100, start: int = 123,
                command: tuple[str, ...] = ("nix", "print-dev-env"), token: str = "",
                cwd: Path = MISSING_PROCESS_CWD) -> None:
        process = self.proc / str(pid)
        process.mkdir()
        fields = [state, str(parent), str(group), *(["0"] * 16), str(start)]
        _ = (process / "stat").write_text(f"{pid} (fixture process) " + " ".join(fields))
        _ = (process / "status").write_text(f"Uid:\t{os.getuid()}\t{os.getuid()}\t{os.getuid()}\t{os.getuid()}\n")
        _ = (process / "cmdline").write_bytes(b"\0".join(word.encode() for word in command) + b"\0")
        _ = (process / "environ").write_bytes(f"BUILD_TARGET_RUN_TOKEN={token}\0".encode() if token else b"")
        if cwd != MISSING_PROCESS_CWD:
            (process / "cwd").symlink_to(cwd)

    def marker(self) -> Path:
        path = self.mirror / ".linux-run.json"
        lease: linux_runner.SlotRun = {"pid": 100, "start_time": 123, "process_group": 100, "token": "selected-run", "boot_id": "fixture-boot"}
        _ = path.write_text(json.dumps(lease))
        return path

    def deny_reads(self, *, environ: tuple[int, ...] = (), cwd: tuple[int, ...] = (),
                   cmdline: tuple[int, ...] = ()) -> None:
        read_bytes = Path.read_bytes
        resolve = Path.resolve
        denied_bytes = {self.proc / str(pid) / "environ" for pid in environ}
        denied_bytes.update(self.proc / str(pid) / "cmdline" for pid in cmdline)
        denied_cwd = {self.proc / str(pid) / "cwd" for pid in cwd}

        def read(path: Path) -> bytes:
            if path in denied_bytes:
                raise PermissionError(f"fixture denies {path.name}")
            return read_bytes(path)

        def resolve_path(path: Path, strict: bool = False) -> Path:
            if path in denied_cwd:
                raise PermissionError("fixture denies cwd")
            return resolve(path, strict=strict)

        _ = self.enterContext(mock.patch.object(Path, "read_bytes", read))
        _ = self.enterContext(mock.patch.object(Path, "resolve", resolve_path))

    def use_private_processes(self) -> None:
        fields = process_stat.process_fields
        processes = linux_runner.slot_processes

        def private_fields(pid: int, proc_root: Path = linux_runner.PROC_ROOT) -> list[str]:
            _ = proc_root
            return fields(pid, self.proc)

        def private_processes(mirror: Path, proc_root: Path = linux_runner.PROC_ROOT, *,
                              require_boot_identity: bool = False) -> tuple[int, ...]:
            _ = proc_root
            return processes(mirror, self.proc, require_boot_identity=require_boot_identity)

        _ = self.enterContext(mock.patch.object(process_stat, "process_fields", side_effect=private_fields))
        _ = self.enterContext(mock.patch.object(linux_runner, "slot_processes", side_effect=private_processes))

    def selector_output(self) -> subprocess.CompletedProcess[str]:
        script = linux_runner.mirror_selector_script(str(self.mirror))
        command = next(shlex.split(line) for line in script.splitlines() if " -c " in line)
        output, errors = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "argv", ["-c", str(self.mirror)]), \
                redirect_stdout(output), redirect_stderr(errors):
            try:
                exec(command[command.index("-c") + 1], {"__name__": "__main__"})
            except SystemExit as stopped:
                status = stopped.code if isinstance(stopped.code, int) else 1
            else:
                status = 0
        return subprocess.CompletedProcess(command, status, output.getvalue(), errors.getvalue())

    def test_foreign_real_uid_skips_unreadable_process_evidence(self) -> None:
        marker = self.marker()
        self.process(102, start=124, cwd=self.root)
        _ = (self.proc / "102/status").write_text(f"Uid:\t{os.getuid() + 1}\t{os.getuid()}\t{os.getuid()}\t{os.getuid()}\n")
        self.deny_reads(environ=(102,), cwd=(102,), cmdline=(102,))

        selected = linux_runner.select_slot_processes(self.mirror, self.proc)

        self.assertEqual(selected.pids, ())
        self.assertFalse(selected.unresolved)
        self.assertEqual(linux_runner.slot_processes(self.mirror, self.proc), ())
        self.assertFalse(marker.exists())

    def test_same_real_uid_keeps_unreadable_process_ownership_unresolved(self) -> None:
        marker = self.marker()
        self.process(102, start=124, cwd=self.root)
        self.deny_reads(environ=(102,), cwd=(102,), cmdline=(102,))
        stat = Path.stat

        def different_proc_owner(path: Path, *, follow_symlinks: bool = True) -> os.stat_result:
            result = stat(path, follow_symlinks=follow_symlinks)
            if path == self.proc / "102":
                fields = list(result)
                fields[4] = os.getuid() + 1
                return os.stat_result(fields)
            return result

        with mock.patch.object(Path, "stat", different_proc_owner):
            selected = linux_runner.select_slot_processes(self.mirror, self.proc)
            self.assertEqual(selected.pids, ())
            self.assertTrue(selected.unresolved)
            with self.assertRaises(linux_runner.SlotProcessesUnresolved):
                _ = linux_runner.slot_processes(self.mirror, self.proc)
        self.assertTrue(marker.exists())

    def test_unmarked_slot_ignores_an_unreadable_same_uid_process(self) -> None:
        self.process(102, cwd=self.root)
        self.deny_reads(environ=(102,), cwd=(102,), cmdline=(102,))

        self.assertEqual(linux_runner.select_slot_processes(self.mirror, self.proc),
                         linux_runner.SlotProcessSelection((), False))

    def test_unmarked_slot_keeps_positive_runner_and_directory_evidence(self) -> None:
        self.process(101, cwd=self.mirror)
        self.process(102, command=("python3", "/scripts/linux_runner.py", "--mirror", str(self.mirror)),
                     cwd=self.root)
        self.deny_reads(cwd=(102,), cmdline=(101,))

        self.assertEqual(linux_runner.select_slot_processes(self.mirror, self.proc),
                         linux_runner.SlotProcessSelection((101, 102), False))

    def test_leftover_selectors_resolve_after_removing_a_previous_boot_marker(self) -> None:
        marker = self.marker()
        lease = cast(linux_runner.SlotRun, json.loads(marker.read_text()))
        lease['boot_id'] = 'previous-boot'
        _ = marker.write_text(json.dumps(lease))
        self.process(102, cwd=self.root)
        self.deny_reads(environ=(102,), cwd=(102,), cmdline=(102,))
        self.use_private_processes()
        with mock.patch.object(offload, "mirror_selector_script",
                               return_value=linux_runner.mirror_selector_script(str(self.mirror))):
            script = offload.leftover_check_script(eligibility.KnownRepository("hana", "https://example.invalid/hana"), "linux")
        selections = script.count('leftover_pids=$(select_leftovers)')
        self.assertEqual(selections, 3)
        for index in range(selections):
            with self.subTest(selection=index):
                reply = self.selector_output()
                self.assertEqual(reply.returncode, 0, reply.stderr)
                self.assertEqual(reply.stdout.strip(), "")
                self.assertFalse(marker.exists())

    def test_unreadable_process_before_the_lease_allows_confirmed_cleanup(self) -> None:
        marker = self.marker()
        self.process(102, start=100, cwd=self.root)
        self.deny_reads(environ=(102,), cwd=(102,), cmdline=(102,))
        self.use_private_processes()

        selected = linux_runner.select_slot_processes(self.mirror, self.proc)

        self.assertEqual(selected.pids, ())
        self.assertFalse(selected.unresolved)
        self.assertEqual(linux_runner.slot_processes(self.mirror, self.proc), ())
        self.assertFalse(marker.exists())
        _ = self.marker()
        selector = self.selector_output()
        self.assertEqual(selector.returncode, 0, selector.stderr)
        self.assertEqual(selector.stdout.strip(), "")
        self.process(900, group=900, start=123, cwd=self.root)
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            with linux_runner.slot_run(self.mirror):
                print("mac_exit=0\nmac_cleanup=kept")
        self.assertFalse(marker.exists())
        self.assertIn("mac_cleanup=kept", output.getvalue())
        self.assertNotIn("mac_cleanup=unconfirmed", output.getvalue())
        self.assertNotIn("Traceback", output.getvalue() + errors.getvalue() + selector.stderr)

    def test_checking_ssh_ancestors_do_not_block_slot_cleanup(self) -> None:
        marker = self.marker()
        self.process(10, start=1, group=10, cwd=self.root)
        _ = (self.proc / "10/status").write_text("Uid:\t0\t0\t0\t0\n")
        self.process(200, parent=10, start=150, group=200, cwd=self.root)
        self.process(201, parent=200, start=151, group=200, cwd=self.root)
        self.process(900, parent=201, start=152, group=900, cwd=self.root)
        self.deny_reads(environ=(200, 201), cwd=(200, 201), cmdline=(200, 201))

        selected = linux_runner.select_slot_processes(self.mirror, self.proc)

        self.assertEqual(selected, linux_runner.SlotProcessSelection((), False))
        self.assertEqual(linux_runner.slot_processes(self.mirror, self.proc), ())
        self.assertFalse(marker.exists())

    def test_concurrent_ssh_session_with_a_root_parent_does_not_block_cleanup(self) -> None:
        marker = self.marker()
        self.process(10, start=1, group=10, cwd=self.root)
        _ = (self.proc / "10/status").write_text("Uid:\t0\t0\t0\t0\n")
        self.process(200, parent=10, start=150, group=200, cwd=self.root)
        other = self.root / "mirror/2"
        other.mkdir()
        self.process(201, parent=200, start=151, group=200, cwd=other, token="another-run")
        self.deny_reads(environ=(200,), cwd=(200,), cmdline=(200,))

        self.assertEqual(linux_runner.select_slot_processes(self.mirror, self.proc),
                         linux_runner.SlotProcessSelection((), False))
        self.assertEqual(linux_runner.slot_processes(self.mirror, self.proc), ())
        self.assertFalse(marker.exists())

    def test_unreadable_reparented_leftover_after_the_lease_stays_unresolved(self) -> None:
        marker = self.marker()
        self.process(1, start=1, group=1, cwd=self.root)
        _ = (self.proc / "1/status").write_text("Uid:\t0\t0\t0\t0\n")
        self.process(102, parent=1, start=150, group=102, cwd=self.root)
        self.deny_reads(environ=(102,), cwd=(102,), cmdline=(102,))

        self.assertEqual(linux_runner.select_slot_processes(self.mirror, self.proc),
                         linux_runner.SlotProcessSelection((), True))
        with self.assertRaises(linux_runner.SlotProcessesUnresolved):
            _ = linux_runner.slot_processes(self.mirror, self.proc)
        self.assertTrue(marker.exists())

    def test_unreadable_leased_process_and_parent_chain_keep_confirmed_ownership(self) -> None:
        marker = self.marker()
        self.process(100, cwd=self.root)
        self.process(101, parent=100, start=124, cwd=self.root)
        self.process(102, parent=101, group=102, start=125, cwd=self.root)
        self.deny_reads(environ=(100, 101, 102), cwd=(100, 101, 102), cmdline=(100, 101, 102))
        self.use_private_processes()

        selected = linux_runner.select_slot_processes(self.mirror, self.proc)

        self.assertEqual(set(selected.pids), {100, 101, 102})
        self.assertFalse(selected.unresolved)
        self.assertTrue(marker.exists())
        selector = self.selector_output()
        self.assertEqual(selector.returncode, 0, selector.stderr)
        self.assertEqual(set(selector.stdout.split()), {"100", "101", "102"})
        self.assertNotIn("Traceback", selector.stdout + selector.stderr)

    def test_denied_environment_preserves_readable_slot_directory_evidence(self) -> None:
        marker = self.marker()
        self.process(101, start=124, cwd=self.mirror)
        self.deny_reads(environ=(101,))
        self.use_private_processes()

        selected = linux_runner.select_slot_processes(self.mirror, self.proc)

        self.assertEqual(selected.pids, (101,))
        self.assertFalse(selected.unresolved)
        self.assertTrue(marker.exists())
        selector = self.selector_output()
        self.assertEqual(selector.returncode, 0, selector.stderr)
        self.assertEqual(selector.stdout.split(), ["101"])
        self.assertNotIn("Traceback", selector.stdout + selector.stderr)

    def test_denied_directory_preserves_readable_run_token_evidence(self) -> None:
        marker = self.marker()
        self.process(101, start=124, token="selected-run", cwd=self.root)
        self.deny_reads(cwd=(101,))
        self.use_private_processes()

        selected = linux_runner.select_slot_processes(self.mirror, self.proc)

        self.assertEqual(selected.pids, (101,))
        self.assertFalse(selected.unresolved)
        self.assertTrue(marker.exists())
        selector = self.selector_output()
        self.assertEqual(selector.returncode, 0, selector.stderr)
        self.assertEqual(selector.stdout.split(), ["101"])
        self.assertNotIn("Traceback", selector.stdout + selector.stderr)

    def test_selector_distinguishes_unresolved_ownership_from_failed_inspection(self) -> None:
        marker = self.marker()
        self.process(102, start=124, cwd=self.root)
        self.deny_reads(environ=(102,), cwd=(102,))
        self.use_private_processes()

        unresolved = self.selector_output()

        self.assertEqual(unresolved.returncode, 3)
        self.assertEqual(unresolved.stdout, "")
        self.assertNotIn("Traceback", unresolved.stderr)
        self.assertTrue(marker.exists())
        self.process(100, start=456, cwd=self.root)

        failed = self.selector_output()

        self.assertEqual(failed.returncode, 1)
        self.assertEqual(failed.stdout, "")
        self.assertNotIn("Traceback", failed.stderr)
        self.assertTrue(marker.exists())

    def test_shell_evaluation_and_runner_remain_owned_outside_the_mirror(self) -> None:
        marker = self.marker()
        self.process(100, cwd=self.root)
        self.process(101, token="selected-run", cwd=self.root)
        self.process(102, token="another-run", cwd=self.root)
        self.process(103, group=103, token="selected-run", cwd=self.root)
        other_slot = self.mirror.parent / "2"
        other_slot.mkdir()
        self.process(104, group=104, cwd=other_slot)

        selected = linux_runner.slot_processes(self.mirror, proc_root=self.proc)

        self.assertEqual(set(selected), {100, 101, 103})
        self.assertTrue(marker.exists())
        fields = process_stat.process_fields(100, proc_root=self.proc)
        self.assertEqual((fields[0], fields[2], fields[19]), ("S", "100", "123"))

    def test_runner_before_marker_publication_selects_only_its_exact_slot(self) -> None:
        self.process(100, command=("python3", ".claude/scripts/mac_test/linux_runner.py", "run", "--mirror", "mirror/1"), cwd=self.root)
        self.process(101, command=("python3", ".claude/scripts/mac_test/linux_runner.py", "run", "--mirror", "mirror/2"), cwd=self.root)

        self.assertEqual(linux_runner.slot_processes(self.mirror, proc_root=self.proc), (100,))

    def test_detached_child_keeps_ownership_after_the_original_runner_ends(self) -> None:
        marker = self.marker()
        self.process(101, token="selected-run", cwd=self.root)

        self.assertEqual(linux_runner.slot_processes(self.mirror, proc_root=self.proc), (101,))
        self.assertTrue(marker.exists())

    def test_reused_runner_pid_cannot_confirm_cleanup(self) -> None:
        marker = self.marker()
        self.process(100, start=456, cwd=self.root)

        with self.assertRaisesRegex(ValueError, "identity changed"):
            _ = linux_runner.slot_processes(self.mirror, proc_root=self.proc)
        self.assertTrue(marker.exists())

    def test_zombies_without_cwd_do_not_block_live_slot_selection(self) -> None:
        self.process(100, state="Z")
        self.process(101, state="X")
        self.process(102, cwd=self.mirror)

        self.assertEqual(linux_runner.slot_processes(self.mirror, proc_root=self.proc), (102,))

    def test_unreadable_live_process_cannot_confirm_cleanup(self) -> None:
        marker = self.marker()
        self.process(102, group=102)

        with self.assertRaises(linux_runner.SlotProcessesUnresolved):
            _ = linux_runner.slot_processes(self.mirror, proc_root=self.proc)
        self.assertTrue(marker.exists())

    def test_ended_processes_remove_the_marker_before_a_fresh_run(self) -> None:
        marker = self.marker()
        self.process(100, state="Z")

        self.assertEqual(linux_runner.slot_processes(self.mirror, proc_root=self.proc), ())
        self.assertFalse(marker.exists())

    def test_detached_children_keep_the_marker_and_emit_uncertain_cleanup(self) -> None:
        fields = ["S", "1", "900", *(["0"] * 16), "123"]
        output = io.StringIO()
        with mock.patch.object(process_stat, "process_fields", return_value=fields), \
                mock.patch.object(linux_runner, "slot_processes", return_value=(101,)), redirect_stdout(output):
            with linux_runner.slot_run(self.mirror):
                self.assertTrue((self.mirror / ".linux-run.json").exists())

        self.assertIn("mac_cleanup=unconfirmed", output.getvalue())
        self.assertTrue((self.mirror / ".linux-run.json").exists())


class LinuxCopyTests(unittest.TestCase):
    root: Path = Path()
    source: Path = Path()
    destination: Path = Path()
    commands: list[list[str]] = []
    stamps: list[str] = []

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="linux-copy-")))
        self.source = self.root / "source"
        self.source.mkdir()
        self.destination = self.root / "received"
        self.destination.mkdir()
        self.commands, self.stamps = [], []
        _ = self.git("init", "-q")
        _ = self.git("config", "user.name", "Fixture")
        _ = self.git("config", "user.email", "fixture@example.invalid")
        registry = self.root / "build_targets.conf"
        _ = registry.write_text("targets=linux\nkind.linux=ssh\nplatform.linux=linux\nsettings.linux=linux.conf\n"
                                + UNREACHABLE_WAIT_FIXTURE)
        _ = write_config_fixture(self.root / "mac_test.conf", template="mac_test")
        _ = write_config_fixture(self.root / "linux.conf", "host=fake-linux\noffload=on\n", template="mac_test")
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {
            "BUILD_TARGETS_CONFIG": str(registry), "CONF_DIR": str(self.root),
        })))

    def git(self, *words: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(self.source), *words], text=True, capture_output=True, check=True, timeout=5,
        )
        return result.stdout.strip()

    def write_source(self, name: str, contents: str) -> Path:
        path = self.source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(contents)
        return path

    def capture(self, arguments: Sequence[str],
                _deadline: offload.CommandDeadline = offload.NO_COMMAND_TIME_LIMIT) -> offload.CommandOutput:
        argv = list(arguments)
        self.commands.append(argv)
        if argv[0] == "git":
            result = subprocess.run(argv, text=True, capture_output=True, check=False, timeout=5)
        elif argv[0] == "rsync":
            local = argv[:-1]
            if "-e" in local:
                index = local.index("-e")
                del local[index:index + 2]
            result = subprocess.run(
                [*local, f"{self.destination}/"], text=True, capture_output=True, check=False, timeout=5,
            )
        elif argv[0] == "ssh":
            self.stamps.append(argv[-1])
            return offload.CommandOutput(0, "", "")
        else:
            self.fail(f"Unexpected external command: {argv}")
        return offload.CommandOutput(result.returncode, result.stdout, result.stderr)

    def config(self) -> eligibility.OffloadConfig:
        return eligibility.read_config(self.root / "linux.conf")

    def test_first_linux_copy_creates_the_missing_repository_and_slot_directories(self) -> None:
        _ = self.write_source("src/lib.rs", "first source\n")
        remote_home = self.root / "remote-home"
        remote_home.mkdir()
        _ = copy_helpers(remote_home, *COPY_HELPERS, "config")
        ssh = self.root / "ssh"
        _ = ssh.write_text('#!/bin/bash\nwhile [[ $1 == -o ]]; do shift 2; done\n'
                           + '[[ $1 == natedev ]] && exit 255\n'
                           + 'shift\ncd "$TEST_REMOTE_HOME" || exit 1\nexec bash -c "$*"\n')
        ssh.chmod(0o755)
        controller = offload.ProcessController()
        request = offload.OffloadRequest(self.source, "hana", "copy", offload.NO_RESULT_FILE, (), owner="verify", target="linux", holder_session="fixture-session")
        identity = eligibility.KnownRepository("source", "https://example.invalid/team/hana")
        environment = {**os.environ, "PATH": f"{self.root}:{os.environ['PATH']}",
                       "TEST_REMOTE_HOME": str(remote_home), "HOME": str(remote_home)}
        with mock.patch.dict(os.environ, environment):
            copied = offload.copy_tree_with_key(controller, self.config(), request, identity)

        self.assertIsInstance(copied, offload.CopiedTree, controller.copy_failure_detail)
        received = remote_home / offload.remote_mirror(identity, "linux", 1) / "src/lib.rs"
        self.assertEqual(received.read_text(), "first source\n")

    def test_second_copy_removes_deleted_files_sends_new_files_and_excludes_ignored_files(self) -> None:
        _ = self.write_source(".gitignore", "private/\n.env\n")
        dropped = self.write_source("tests/b.rs", "old test\n")
        _ = self.write_source("src/lib.rs", "original source\n")
        _ = self.write_source("private/key", "private fixture\n")
        _ = self.write_source(".env", "private environment\n")
        _ = self.write_source("target/local-build", "local build\n")
        _ = self.git("add", ".gitignore", "tests/b.rs", "src/lib.rs")
        _ = self.git("commit", "-qm", "Fixture source")
        controller = offload.ProcessController()
        request = offload.OffloadRequest(self.source, "hana", "copy", offload.NO_RESULT_FILE, (), owner="verify", target="linux", holder_session="fixture-session")
        identity = eligibility.KnownRepository("source", "https://example.invalid/team/hana")
        with mock.patch.object(controller, "capture", side_effect=self.capture):
            self.assertTrue(offload.copy_tree(controller, self.config(), request, identity))
            self.assertEqual((self.destination / "tests/b.rs").read_text(), "old test\n")
            (self.destination / "target").mkdir()
            _ = (self.destination / "target/warm").write_text("retained build\n")
            dropped.unlink()
            _ = self.write_source("tests/new test.rs", "untracked test\n")
            _ = self.write_source("src/lib.rs", "changed source\n")

            self.assertTrue(offload.copy_tree(controller, self.config(), request, identity))

        received = {str(path.relative_to(self.destination)) for path in self.destination.rglob("*") if path.is_file()}
        self.assertEqual(received, {".gitignore", "src/lib.rs", "tests/new test.rs", "target/warm"})
        self.assertEqual((self.destination / "tests/new test.rs").read_text(), "untracked test\n")
        self.assertEqual((self.destination / "src/lib.rs").read_text(), "changed source\n")
        self.assertEqual((self.destination / "target/warm").read_text(), "retained build\n")

    def test_script_stamp_never_labels_dirty_worktree_bytes_as_the_head_commit(self) -> None:
        runner = self.write_source("scripts/mac_test/linux_runner.py", "committed runner\n")
        _ = self.write_source("config/mac_test.conf", "host=fake-linux\n")
        _ = self.write_source("config/build_folders.conf", "prefixes.verify-hold=verify-hold-\n")
        _ = self.git("add", "scripts", "config")
        _ = self.git("commit", "-qm", "Fixture scripts")
        commit = self.git("rev-parse", "HEAD")
        values = self.root / "linux.conf"
        _ = values.write_text(values.read_text() + f"parity.scripts_commit={commit}\n"
                              + "parity.rustc=rustc fixture\nparity.nextest=nextest fixture\nparity.mend=mend fixture\n")
        controller = offload.ProcessController()
        with mock.patch.object(offload, "__file__", str(self.source / "scripts/mac_test/offload.py")), \
                mock.patch.object(controller, "capture", side_effect=self.capture):
            self.assertTrue(offload.copy_tracked_scripts(controller, self.config()))
            self.assertEqual((self.destination / "scripts/mac_test/linux_runner.py").read_text(), "committed runner\n")
            self.assertEqual((self.destination / "config/build_folders.conf").read_text(),
                             "prefixes.verify-hold=verify-hold-\n")
            self.assertTrue(self.stamps)
            self.assertIn(commit, self.stamps[-1])
            shutil.rmtree(self.destination)
            self.destination.mkdir()
            self.stamps.clear()
            _ = runner.write_text("dirty runner\n")

            copied = offload.copy_tracked_scripts(controller, self.config())

        if copied:
            self.assertEqual((self.destination / "scripts/mac_test/linux_runner.py").read_text(), "committed runner\n")
            self.assertTrue(self.stamps)
            self.assertIn(commit, self.stamps[-1])
        else:
            self.assertFalse(self.stamps)
            self.assertFalse((self.destination / "scripts/mac_test/linux_runner.py").exists())

    def test_script_changes_during_transfer_never_publish_a_commit_stamp(self) -> None:
        runner = self.write_source("scripts/mac_test/linux_runner.py", "committed runner\n")
        _ = self.write_source("config/mac_test.conf", "host=fake-linux\n")
        _ = self.git("add", "scripts", "config")
        _ = self.git("commit", "-qm", "Fixture scripts")
        commit = self.git("rev-parse", "HEAD")
        values = self.root / "linux.conf"
        _ = values.write_text(values.read_text() + f"parity.scripts_commit={commit}\n"
                              + "parity.rustc=rustc fixture\nparity.nextest=nextest fixture\nparity.mend=mend fixture\n")
        controller = offload.ProcessController()
        for change in ("worktree", "commit"):
            with self.subTest(change=change):
                _ = runner.write_text("committed runner\n")
                self.stamps.clear()

                def capture(arguments: Sequence[str],
                            deadline: offload.CommandDeadline = offload.NO_COMMAND_TIME_LIMIT) -> offload.CommandOutput:
                    result = self.capture(arguments, deadline)
                    if arguments[0] == "rsync":
                        if change == "worktree":
                            _ = runner.write_text("changed during transfer\n")
                        else:
                            _ = self.git("commit", "--allow-empty", "-qm", "Commit during transfer")
                    return result

                with mock.patch.object(offload, "__file__", str(self.source / "scripts/mac_test/offload.py")), \
                        mock.patch.object(controller, "capture", side_effect=capture):
                    copied = offload.copy_tracked_scripts(controller, self.config())

                self.assertFalse(copied)
                self.assertFalse(self.stamps)
                self.assertEqual((self.destination / "scripts/mac_test/linux_runner.py").read_text(), "committed runner\n")
