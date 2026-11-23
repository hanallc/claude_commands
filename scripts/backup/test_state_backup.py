"""Check backup and restore with private folders and local substitutes."""
from __future__ import annotations

from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tarfile
from tempfile import TemporaryDirectory
from typing import cast, final, override
import unittest
from unittest.mock import patch

from scripts.backup import state_backup
from scripts.lib import conf
from scripts.backup.state_backup import ALERT_KEY, READ_BLOCK_SIZE, LastBackup, checksum_path
from scripts.lib.atomic_file import prepared_replacement


ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "scripts/backup/state_backup.py"


@final
class StateBackupTests(unittest.TestCase):
    root: Path = Path()
    source: Path = Path()
    mac_folder: Path = Path()
    state: Path = Path()
    last_path: Path = Path()
    ssh_log: Path = Path()
    send_log: Path = Path()
    env: dict[str, str] = {}

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(TemporaryDirectory()))
        home = self.root / "home"
        mac_home = self.root / "mac"
        mac_home.mkdir()
        self.mac_folder = mac_home / "backup folder/aws-state"
        self.source = home / ".local/state/build-targets/aws"
        self.source.mkdir(parents=True)
        self.state = self.root / "state/state-backup"
        self.last_path = self.state / "last.json"
        self.ssh_log = self.root / "ssh.log"
        self.send_log = self.root / "send.log"
        programs = self.root / "bin"
        programs.mkdir()
        config = self.root / "config"
        config.mkdir()
        template = conf.test_config_path("state_backup").read_text(encoding="utf-8")
        _ = (config / "state_backup.conf").write_text(
            template.replace("mac.folder = ~/backups/natedev/aws-state", "mac.folder = ~/backup folder/aws-state"),
            encoding="utf-8",
        )
        _ = (config / "build_targets.conf").write_text("""\
targets = natedev,mac
kind.natedev = local
platform.natedev = linux
kind.mac = ssh
platform.mac = macos
settings.mac = fixture-mac.conf
""", encoding="utf-8")
        _ = (config / "fixture-mac.conf").write_text("""\
host = fixture-mac
# range: 5e-324..
connect_timeout_s = 8.5
# range: 5e-324..
leftover_timeout_s = 10
""", encoding="utf-8")
        self.env = {
            "HOME": str(home),
            "CONF_DIR": str(config),
            "XDG_STATE_HOME": str(self.state.parent),
            "XDG_CACHE_HOME": str(self.root / "cache"),
            "PATH": os.pathsep.join([str(programs), os.environ["PATH"]]),
            "PYTHONPATH": str(ROOT),
            "PYTHONDONTWRITEBYTECODE": "1",
            "TMPDIR": str(self.root),
            "FIXTURE_MAC_HOME": str(mac_home),
            "FIXTURE_SSH_LOG": str(self.ssh_log),
            "FIXTURE_SEND_LOG": str(self.send_log),
            "FIXTURE_PENDING_ALERT": str(self.state / "alert-pending"),
            "FIXTURE_EXPECTED_MAC_HOST": "fixture-mac",
        }
        self.write_program(programs / "ssh", r"""
import os
import sys

assert sys.argv[1:-1] == ["-o", "BatchMode=yes", "-o", "ConnectTimeout=9", "--", os.environ["FIXTURE_EXPECTED_MAC_HOST"]]
with open(os.environ["FIXTURE_SSH_LOG"], "a", encoding="utf-8") as log:
    log.write("ssh\n")
os.environ["HOME"] = os.environ["FIXTURE_MAC_HOME"]
os.execv("/bin/sh", ["/bin/sh", "-c", sys.argv[-1]])
""")
        self.write_program(programs / "shasum", """
import hashlib
import os
import sys

assert sys.argv[1:3] == ["-a", "256"]
with open(sys.argv[3], "rb") as file:
    digest = hashlib.file_digest(file, "sha256").hexdigest()
if os.environ.get("FIXTURE_BAD_MAC_DIGEST"):
    digest = "0" * 64
print(digest, "", sys.argv[3])
""")
        self.write_program(home / ".claude/scripts/message/send.py", r"""
import json
import os
import sys

if sys.argv[1] != "retire" and os.environ.get("FIXTURE_REQUIRE_PENDING_ALERT"):
    assert os.path.isfile(os.environ["FIXTURE_PENDING_ALERT"])
with open(os.environ["FIXTURE_SEND_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps(sys.argv[1:]) + "\n")
""")

    def write_program(self, path: Path, program: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(f"#!{sys.executable}\n{program}", encoding="utf-8")
        path.chmod(0o700)

    def put(self, name: str, contents: bytes = b"fixture data") -> None:
        path = self.source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_bytes(contents)

    def replace_setting(self, name: str, old_line: str, new_line: str) -> None:
        path = self.root / "config" / name
        content = path.read_text(encoding="utf-8")
        self.assertIn(old_line, content)
        _ = path.write_text(content.replace(old_line, new_line), encoding="utf-8")

    def command(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(TOOL), *arguments], cwd=self.root, env=self.env,
            stdin=subprocess.DEVNULL, capture_output=True, text=True, check=False, timeout=20,
        )

    def backup(self) -> LastBackup:
        result = self.command("run")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return cast(LastBackup, json.loads(self.last_path.read_text(encoding="utf-8")))

    def check_run_failed(self, result: subprocess.CompletedProcess[str], message: str) -> None:
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(message, result.stderr)
        self.assertFalse(list(self.state.glob("backup-*")))

    def alerts(self) -> list[list[str]]:
        return [cast(list[str], json.loads(row)) for row in self.send_log.read_text(encoding="utf-8").splitlines()]

    def make_archive(self, *members: tarfile.TarInfo) -> Path:
        archive = self.root / "fixture.tar.gz"
        with tarfile.open(archive, "w:gz") as bundle:
            for member in members:
                bundle.addfile(member, BytesIO(b""))
        self.write_checksum(archive)
        return archive

    def write_checksum(self, archive: Path) -> None:
        digest = sha256(archive.read_bytes()).hexdigest()
        _ = checksum_path(archive).write_text(f"{digest}  {archive.name}\n", encoding="ascii")

    def test_include_and_exclude_use_relative_paths_and_skip_symlinks(self) -> None:
        included = (
            "cost-ledger.json", "charges/2026-01-15.json", "gate-trial-id.txt", "affinity/zone",
            "release-evidence/region/day/value", "hana/executions/id.txt", "paired-trials/day/run/result.json",
        )
        for name in included + ("notes.txt", "charges/day/nested.json", "other/cost-ledger.json"):
            self.put(name)
        self.put("paired-trials/day/run/launch.json", b"X-Amz-Signature=excluded")
        self.put("hana/executions/id.json", b"X-Amz-Credential=excluded")
        outside = self.root / "outside"
        outside.mkdir()
        _ = (outside / "private").write_bytes(b"X-Amz-Signature=outside")
        (self.source / "gate-trial-link.json").symlink_to(outside / "private")
        (self.source / "affinity/link").symlink_to(outside, target_is_directory=True)
        os.mkfifo(self.source / "gate-trial-pipe.txt")
        self.assertEqual(set(self.backup()["manifest"]["files"]), set(included))

    def test_excluded_provider_locks_logs_and_markers_do_not_refuse_backup(self) -> None:
        self.replace_setting("state_backup.conf", "include.patterns = cost-ledger.json", "include.patterns = ** cost-ledger.json")
        self.put("cost-ledger.json")
        self.put("hana/unlisted.txt")
        for name in ("hana/provider/x.json", "release-evidence/worker.lock", "paired-trials/a/b/worker.log",
                     "worker.lock", "worker.log", "up", "gate-up", "unreachable_until"):
            self.put(name, b"X-Amz-Signature=excluded")
        self.assertEqual(set(self.backup()["manifest"]["files"]), {"cost-ledger.json", "hana/unlisted.txt"})
        self.assertFalse(self.send_log.exists())

    def test_backup_skips_atomic_writer_temporary_files(self) -> None:
        self.put("paired-trials/a/b/result.json")
        launch = self.source / "paired-trials/a/b/launch.json"
        evidence = self.source / "release-evidence/event.json"
        with (prepared_replacement(launch, "X-Amz-Signature=fixture", mode=0o600) as launch_stage,
              prepared_replacement(evidence, "AWS_SESSION_TOKEN=fixture", mode=0o600) as evidence_stage):
            self.assertTrue(launch_stage.is_file())
            self.assertTrue(evidence_stage.is_file())
            self.assertEqual(set(self.backup()["manifest"]["files"]), {"paired-trials/a/b/result.json"})
        self.assertFalse(self.send_log.exists())

    def test_second_run_is_unchanged_and_status_lists_last_copy(self) -> None:
        empty = self.command("status")
        self.assertEqual((empty.returncode, empty.stdout, empty.stderr), (0, "no saved backup\n", ""))
        self.put("cost-ledger.json")
        last = self.backup()
        result = self.command("run")
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "unchanged\n", ""))
        self.assertEqual(self.ssh_log.read_text(encoding="utf-8").splitlines(), ["ssh"])
        self.assertEqual(len(list(self.mac_folder.iterdir())), 2)
        self.assertFalse(self.send_log.exists())
        status = self.command("status")
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertEqual(status.stdout, f"{last['time']} {last['archive']} (1 archived)\n")

    def test_destination_changes_send_an_unchanged_manifest(self) -> None:
        self.put("cost-ledger.json")
        original = self.backup()
        self.replace_setting("state_backup.conf", "mac.folder = ~/backup folder/aws-state", "mac.folder = ~/new backup")
        new_folder = self.backup()
        self.assertEqual(new_folder["manifest"], original["manifest"])
        self.assertEqual(new_folder.get("destination"), {"host": "fixture-mac", "folder": "~/new backup"})
        self.assertTrue((self.root / "mac/new backup" / new_folder["archive"]).is_file())

        self.replace_setting("fixture-mac.conf", "host = fixture-mac", "host = other-mac")
        second_home = self.root / "other-mac"
        second_home.mkdir()
        self.env["FIXTURE_MAC_HOME"] = str(second_home)
        self.env["FIXTURE_EXPECTED_MAC_HOST"] = "other-mac"
        new_host = self.backup()
        self.assertEqual(new_host["manifest"], original["manifest"])
        self.assertEqual(new_host.get("destination"), {"host": "other-mac", "folder": "~/new backup"})
        self.assertTrue((second_home / "new backup" / new_host["archive"]).is_file())
        self.assertEqual(self.ssh_log.read_text(encoding="utf-8").splitlines(), ["ssh", "ssh", "ssh"])

    def test_legacy_manifest_without_destination_sends_a_new_copy(self) -> None:
        self.put("cost-ledger.json")
        legacy = self.backup()
        del legacy["destination"]
        _ = self.last_path.write_text(json.dumps(legacy), encoding="utf-8")
        self.assertEqual(self.command("status").returncode, 0)
        current = self.backup()
        self.assertNotEqual(current["archive"], legacy["archive"])
        self.assertEqual(current["manifest"], legacy["manifest"])
        self.assertEqual(current.get("destination"), {"host": "fixture-mac", "folder": "~/backup folder/aws-state"})

    def test_secret_patterns_name_the_file_and_pattern_only(self) -> None:
        examples = (
            ("X-Amz-Signature", "x-amz-signature=hidden"), ("X-Amz-Credential", "X-AMZ-CREDENTIAL=hidden"),
            ("AKIA[0-9A-Z]{16}", "AKIA" + "0" * 16), ("ASIA[0-9A-Z]{16}", "ASIA" + "0" * 16),
            ("aws_secret_access_key", "AWS_SECRET_ACCESS_KEY=hidden"), ("SecretAccessKey", "secretaccesskey=hidden"),
            ("SessionToken", "SESSIONTOKEN=hidden"), ("aws_session_token", "AWS_SESSION_TOKEN=hidden"),
            ("aws_security_token", "AWS_SECURITY_TOKEN=hidden"),
            ("aws_security_token", "aws_security_token=hidden"),
            ("SecurityToken", "SecurityToken=hidden"), ("SecurityToken", "securitytoken=hidden"),
            ("-----BEGIN", "-----BEGIN example-----"),
        )
        name = "charges/2026-01-15.json"
        for pattern, match in examples:
            with self.subTest(pattern=pattern):
                prefix = b" " * (READ_BLOCK_SIZE - 10) if pattern == "aws_secret_access_key" else b""
                self.put(name, prefix + match.encode("ascii"))
                result = self.command("run")
                reason = f"{name!r} matches {pattern}"
                self.check_run_failed(result, reason)
                self.assertEqual(result.stderr, f"state_backup: {reason}\n")
                self.assertNotIn(match, result.stdout + result.stderr + self.send_log.read_text(encoding="utf-8"))
                self.assertFalse(self.last_path.exists())
        self.assertFalse(self.ssh_log.exists())
        self.assertEqual(len(self.alerts()), len(examples))
        for alert in self.alerts():
            self.assertEqual(alert[alert.index("--key") + 1], ALERT_KEY)

    def test_lowercase_key_ids_do_not_match_the_exact_patterns(self) -> None:
        self.put("cost-ledger.json", b"akia" + b"0" * 16 + b" ASIA" + b"a" * 16)
        self.assertIn("cost-ledger.json", self.backup()["manifest"]["files"])
        self.assertFalse(self.send_log.exists())

    def test_mac_refuses_a_symlink_in_a_parent_folder(self) -> None:
        self.put("cost-ledger.json")
        redirected = self.root / "outside/aws-state"
        redirected.mkdir(parents=True)
        redirected.chmod(0o755)
        self.mac_folder.parent.symlink_to(redirected.parent, target_is_directory=True)
        self.check_run_failed(self.command("run"), "the Mac backup path has a symlink")
        self.assertFalse(self.last_path.exists())
        self.assertEqual(list(redirected.iterdir()), [])
        self.assertEqual(stat.S_IMODE(redirected.stat().st_mode), 0o755)

    def test_interrupt_before_archive_rename_leaves_only_the_checksum(self) -> None:
        self.put("cost-ledger.json")
        self.write_program(self.root / "bin/mv", """
import os
import signal
import sys

if sys.argv[-1].endswith(".tar.gz"):
    os.kill(os.getppid(), signal.SIGTERM)
    sys.exit(1)
os.environ["PATH"] = os.environ["PATH"].split(os.pathsep, 1)[1]
os.execvp("mv", sys.argv)
""")
        self.check_run_failed(self.command("run"), "SSH to fixture-mac failed")
        self.assertFalse(self.last_path.exists())
        leftover, = self.mac_folder.iterdir()
        self.assertTrue(leftover.name.endswith(".tar.gz.sha256"))

    def test_alert_sender_keeps_its_own_delivery_budget(self) -> None:
        def send(command: list[str], **options: object) -> subprocess.CompletedProcess[bytes]:
            self.assertEqual(command, ["fixture-send"])
            self.assertNotIn("timeout", options)
            return subprocess.CompletedProcess(command, 0, b"", b"")

        with patch.object(subprocess, "run", side_effect=send):
            self.assertTrue(state_backup.notify(["fixture-send"]))

    def test_alert_warns_when_the_pending_marker_cannot_be_written(self) -> None:
        self.state.mkdir(parents=True)
        (self.state / "alert-pending").mkdir()
        self.put("cost-ledger.json", b"AWS_SESSION_TOKEN=fixture")
        result = self.command("run")
        self.check_run_failed(result, "cannot record the pending alert")
        alert, = self.alerts()
        self.assertIn("A later successful run cannot retire this alert.", " ".join(alert))
        self.assertFalse(self.last_path.exists())

    def test_mac_checksum_mismatch_alerts_without_saving_last_and_success_retires_alert(self) -> None:
        self.put("cost-ledger.json")
        self.env["FIXTURE_BAD_MAC_DIGEST"] = "1"
        self.env["FIXTURE_REQUIRE_PENDING_ALERT"] = "1"
        self.check_run_failed(self.command("run"), "the Mac SHA-256 does not match")
        self.assertFalse(self.last_path.exists())
        self.assertEqual(list(self.mac_folder.iterdir()), [])
        alert, = self.alerts()
        self.assertIn("alert.blocked", alert)
        self.assertEqual(alert[alert.index("--key") + 1], ALERT_KEY)
        del self.env["FIXTURE_BAD_MAC_DIGEST"]
        _ = self.backup()
        _, retired = self.alerts()
        self.assertEqual(retired, ["retire", "--to", "user", "--from", "state-backup", "--key", ALERT_KEY])
        self.assertFalse((self.state / "alert-pending").exists())

    def test_changed_run_keeps_earlier_archives(self) -> None:
        self.put("cost-ledger.json", b"first")
        first = self.backup()
        previous = self.mac_folder / first["archive"]
        earlier_contents = {path: path.read_bytes() for path in (previous, checksum_path(previous))}
        self.put("cost-ledger.json", b"second")
        second = self.backup()
        self.assertNotEqual(first["archive"], second["archive"])
        self.assertEqual({path.name for path in self.mac_folder.iterdir()}, {
            first["archive"], first["archive"] + ".sha256", second["archive"], second["archive"] + ".sha256",
        })
        for path, content in earlier_contents.items():
            self.assertEqual(path.read_bytes(), content)

    def test_restore_refuses_a_nonempty_folder(self) -> None:
        archive = self.make_archive(tarfile.TarInfo("valid"))
        destination = self.root / "restore"
        destination.mkdir()
        existing = destination / "existing"
        _ = existing.write_bytes(b"keep")
        result = self.command("restore", str(archive), "--into", str(destination))
        self.assertEqual(result.returncode, 1)
        self.assertIn("must be empty or absent", result.stderr)
        self.assertEqual(list(destination.iterdir()), [existing])
        self.assertEqual(existing.read_bytes(), b"keep")

    def test_restore_refuses_a_bad_checksum(self) -> None:
        archive = self.make_archive(tarfile.TarInfo("valid"))
        with archive.open("ab") as data:
            _ = data.write(b"changed")
        destination = self.root / "restore"
        result = self.command("restore", str(archive), "--into", str(destination))
        self.assertEqual(result.returncode, 1)
        self.assertIn("SHA-256 does not match", result.stderr)
        self.assertFalse(destination.exists())

    def test_restore_refuses_bad_or_truncated_gzip_without_a_traceback(self) -> None:
        archive = self.make_archive(tarfile.TarInfo("valid"))
        valid = archive.read_bytes()
        for damaged in (b"not a gzip file", valid[:5], valid[:-4]):
            with self.subTest(length=len(damaged)):
                _ = archive.write_bytes(damaged)
                self.write_checksum(archive)
                destination = self.root / "restore"
                result = self.command("restore", str(archive), "--into", str(destination))
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stderr, "state_backup: the archive has invalid or incomplete gzip or tar data\n")
                self.assertFalse(destination.exists())

    def test_restore_refuses_unsafe_members(self) -> None:
        for kind, name in ((tarfile.REGTYPE, "../outside"), (tarfile.REGTYPE, "/outside"),
                           (tarfile.SYMTYPE, "link"), (tarfile.LNKTYPE, "hardlink"), (tarfile.CHRTYPE, "device")):
            with self.subTest(name=name):
                unsafe = tarfile.TarInfo(name)
                unsafe.type = kind
                unsafe.linkname = "valid"
                archive = self.make_archive(tarfile.TarInfo("valid"), unsafe)
                destination = self.root / "restore"
                result = self.command("restore", str(archive), "--into", str(destination))
                self.assertEqual(result.returncode, 1)
                self.assertIn(f"unsafe archive member {name!r}", result.stderr)
                self.assertFalse(destination.exists())
                self.assertFalse((self.root / "outside").exists())

    def test_restore_refuses_a_path_into_source_through_a_symlink(self) -> None:
        archive = self.make_archive(tarfile.TarInfo("valid"))
        alias = self.root / "alias"
        alias.symlink_to(self.source, target_is_directory=True)
        result = self.command("restore", str(archive), "--into", str(alias / "restore"))
        self.assertEqual(result.returncode, 1)
        self.assertIn("overlaps the backup source", result.stderr)
        self.assertEqual(list(self.source.iterdir()), [])

    def test_restore_round_trip_matches_manifest_and_keeps_files_private(self) -> None:
        self.put("cost-ledger.json", b"ledger data")
        self.put("charges/2026-01-15.json", b"\x00line\n")
        last = self.backup()
        archive = self.mac_folder / last["archive"]
        destination = self.root / "restore"
        destination.mkdir()
        result = self.command("restore", str(archive), "--into", str(destination))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        restored = {path.relative_to(destination).as_posix(): sha256(path.read_bytes()).hexdigest()
                    for path in destination.rglob("*") if path.is_file()}
        self.assertEqual(restored, last["manifest"]["files"])
        for folder in (self.mac_folder.parent, self.mac_folder, destination):
            self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700)
        for path in (*self.mac_folder.iterdir(), self.last_path, destination / "cost-ledger.json"):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(sorted(path.name for path in self.state.iterdir()), ["last.json", "run.lock"])


if __name__ == "__main__":
    _ = unittest.main()
