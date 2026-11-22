#!/usr/bin/env python3
"""zone.py: resolve a place to a zone, write it live on both machines, tell every session, then publish it."""
from __future__ import annotations
from scripts.lint.memory_test_support import write_config_fixture

import io
import json
import os
import platform
import shlex
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from typing import cast, override
from unittest import mock

from scripts.lib.git_test_support import git_fixture_environment
from scripts.lib import live_settings, main_publish, zone, user_zone
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.production import land_queue, land_runs

SCRIPT = Path(__file__).with_name("zone.py")
INITIAL_ZONE = "America/Chicago"
CONF_TEXT = f"# Test zone\nzone={INITIAL_ZONE}\n"
# Each fake logs its arguments as one JSON line; the fake ssh is offline while its OFFLINE file exists.
FAKE_SSH = """import json, pathlib, sys
here = pathlib.Path(__file__).parent
with open(here / "ssh.log", "a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
if (here / "OFFLINE").exists():
    print("ssh: connect to host mac port 22: No route to host", file=sys.stderr)
    sys.exit(255)
print("mac: m1 - unit director - sent" if "broadcast.py" in sys.argv[-1] else "rc=0")
"""
FAKE_BROADCAST = """import json, pathlib, sys
with open(pathlib.Path(__file__).parent / "broadcast.log", "a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
print("s1 - showrunner - sent")
print("s2 - unit director - sent")
"""


class ResolveTests(unittest.TestCase):
    NOW: datetime = datetime(2026, 10, 21, 22, tzinfo=timezone.utc)

    def test_an_iana_name_a_city_and_a_one_zone_label_resolve(self) -> None:
        for name, expected in (("America/Curacao", "America/Curacao"), ("america/curacao", "America/Curacao"),
                               ("Curacao", "America/Curacao"), ("Curaçao", "America/Curacao"),
                               ("new york", "America/New_York"), ("UTC", "UTC")):
            with self.subTest(name=name):
                self.assertEqual(zone.resolve(name, self.NOW), expected)

    def test_a_label_many_zones_use_is_refused_by_offset(self) -> None:
        with self.assertRaises(zone.Refusal) as refused:
            _ = zone.resolve("AST", self.NOW)
        message = str(refused.exception)
        self.assertIn("'AST' is the label of", message)
        self.assertIn("at UTC-04:00 (such as America/", message)
        self.assertIn("name the place or the IANA zone", message)

    def test_a_place_that_is_not_a_zone_is_refused(self) -> None:
        with self.assertRaises(zone.Refusal) as refused:
            _ = zone.resolve("Seattle", self.NOW)
        self.assertIn("no zone is named 'Seattle'; give the IANA zone of the place", str(refused.exception))


    def test_notice_tells_agents_to_let_the_tools_add_the_present_time(self) -> None:
        text = zone.notice("America/Curacao", INITIAL_ZONE)
        self.assertIn("Write no present time. Write only future times, such as an ETA. The tools add the present time.",
                      text)
        self.assertNotIn("Read the clock", text)


class StoredZoneTests(unittest.TestCase):
    def test_stored_uses_the_shared_reader_for_values_and_the_default(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        conf = root / "user_zone.conf"
        for contents, expected in ((None, user_zone.DEFAULT_ZONE), ("# no setting\n", user_zone.DEFAULT_ZONE),
                                   ("zone=\n", user_zone.DEFAULT_ZONE), ("zone= Europe/Paris \n", "Europe/Paris")):
            with self.subTest(contents=contents):
                if contents is not None:
                    _ = conf.write_text(contents, encoding="utf-8")
                with mock.patch.object(user_zone, "stored_zone", wraps=user_zone.stored_zone) as reader:
                    self.assertEqual(zone.stored(conf), expected)
                reader.assert_called_once_with(str(conf))


@unittest.skipIf(platform.system() == "Darwin", "/zone set runs on natedev")
class SetTests(unittest.TestCase):
    root: Path = Path()
    checkout: Path = Path()
    fakes: Path = Path()
    env: dict[str, str] = {}

    @override
    def setUp(self) -> None:
        _ = self.enterContext(redirect_stdout(io.StringIO()))
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.checkout, origin, self.fakes = self.root / "checkout", self.root / "origin.git", self.root / "fakes"
        self.fakes.mkdir()
        _ = (self.fakes / "ssh.py").write_text(FAKE_SSH, encoding="utf-8")
        ssh = self.fakes / "ssh"
        _ = ssh.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{self.fakes / "ssh.py"}" "$@"\n', encoding="utf-8")
        ssh.chmod(0o755)
        _ = (self.fakes / "broadcast.py").write_text(FAKE_BROADCAST, encoding="utf-8")
        self.env = {key: value for key, value in git_fixture_environment().items()
                    if key not in ("USER_ZONE", "MAC_WAKE_PROGRAM")}
        self.env.update({
            "HOME": str(self.root), "MODELS_CONFIG_FILE": str(self.checkout / "config/models.conf"),
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
            "USER_ZONE_CONF": str(self.checkout / "config/user_zone.conf"),
            "ZONE_SSH": str(ssh), "ZONE_BROADCAST": str(self.fakes / "broadcast.py"),
            "ZONE_STATE": str(self.root / "state"), "CONF_DIR": str(self.checkout / "config"),
            "XDG_STATE_HOME": str(self.root / "xdg"), "GATE_STATE_DIR": str(self.root / "land-state"),
        })
        _ = self.git(self.root, "init", "-q", "--bare", str(origin))
        _ = self.git(self.root, "init", "-q", "-b", "main", str(self.checkout))
        for arguments in (("config", "user.name", "tester"), ("config", "user.email", "tester@example.invalid"),
                          ("remote", "add", "origin", str(origin))):
            _ = self.git(self.checkout, *arguments)
        (self.checkout / "config").mkdir()
        _ = (self.checkout / "config/user_zone.conf").write_text(CONF_TEXT, encoding="utf-8")
        _ = write_config_fixture(self.checkout / "config/live_settings.conf",
            "fetch_seconds=10\nmac_pull_seconds=20\nmac_write_seconds=10\npush_seconds=10\n")
        _ = (self.checkout / "other.txt").write_text("work in progress\n", encoding="utf-8")
        _ = self.git(self.checkout, "add", "config/user_zone.conf", "config/live_settings.conf")
        _ = self.git(self.checkout, "commit", "-q", "-m", "start")
        _ = self.git(self.checkout, "push", "-q", "origin", "main")

    def git(self, cwd: Path, *arguments: str) -> str:
        return subprocess.run(["git", *arguments], cwd=cwd, env=self.env, capture_output=True, text=True,
                              check=True, timeout=TEST_HANG_GUARD_SECS).stdout

    def run_zone(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(SCRIPT), *arguments], env=self.env, capture_output=True,
                              text=True, check=False, timeout=TEST_HANG_GUARD_SECS)

    def log(self, name: str) -> list[list[str]]:
        path = self.fakes / name
        lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        return [[str(part) for part in json.loads(line)] for line in lines]  # pyright: ignore[reportAny]

    def queued(self) -> Path:
        return self.root / "state/mac.json"

    def test_set_writes_the_zone_live_on_both_machines_tells_them_then_publishes(self) -> None:
        result = self.run_zone("set", "Curacao")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("zone: America/Curacao (AST, UTC-04:00); now ", result.stdout)
        self.assertIn(f"was: {INITIAL_ZONE}", result.stdout)
        self.assertIn("live now: zone=America/Curacao", result.stdout)
        self.assertIn("mac: live now: zone=America/Curacao", result.stdout)
        self.assertIn("mac: pulled", result.stdout)
        self.assertIn("broadcast: 2 sent", result.stdout)
        conf = (self.checkout / "config/user_zone.conf").read_text(encoding="utf-8")
        self.assertEqual(conf, CONF_TEXT.replace(INITIAL_ZONE, "America/Curacao"))
        self.assertEqual(self.git(self.checkout, "log", "-1", "--format=%s", "origin/main").strip(),
                         "zone(America/Curacao): set with /zone (AST)")
        self.assertEqual(self.git(self.checkout, "status", "--short").strip(), "?? other.txt")
        (write, pull) = self.log("ssh.log")
        self.assertIn("live_settings.py\" user_zone zone America/Curacao", write[-1])
        self.assertIn(f"git fetch -q --no-write-fetch-head natedev:{self.checkout} main", pull[-1])
        (sent,) = self.log("broadcast.log")
        self.assertEqual(sent[:4], ["--from", "zone", "--all", sent[3]])
        self.assertRegex(
            sent[3],
            r"^The user's time zone is now America/Curacao \(AST, UTC-04:00\) as of [0-9]{2}:[0-9]{2} AST; "
            + rf"it was {INITIAL_ZONE}\. ",
        )
        self.assertFalse(self.queued().exists())

    def test_a_superseded_mac_write_sends_no_zone_notice(self) -> None:
        mac = self.root / "mac"
        _ = self.git(self.root, "clone", "--quiet", str(self.checkout), str(mac))
        mac_env = {**self.env, "HOME": str(mac), "CONF_DIR": str(mac / "config"),
                   "XDG_STATE_HOME": str(mac / "state")}
        receiver = str(SCRIPT.with_name("live_settings.py"))
        saved = subprocess.run([sys.executable, receiver, "user_zone", "zone", "Europe/Paris"],
                               env={**mac_env, "LIVE_SETTINGS_SAVE_ORDER": "2"}, capture_output=True,
                               text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(saved.returncode, 0, saved.stderr)
        ssh = main_publish.ssh

        def send(command: str, *, ssh_program: str, time_limit: main_publish.CommandTimeLimit,
                 host: str = "mac") -> subprocess.CompletedProcess[str] | str:
            if "live_settings.py" not in command:
                return ssh(command, ssh_program=ssh_program, time_limit=time_limit, host=host)
            order, _, _, *arguments = shlex.split(command)
            self.assertEqual(order, "LIVE_SETTINGS_SAVE_ORDER=1")
            return subprocess.run([sys.executable, receiver, *arguments],
                                  env={**mac_env, "LIVE_SETTINGS_SAVE_ORDER": order.split("=", 1)[1]},
                                  capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)

        with mock.patch.dict(os.environ, self.env, clear=True), \
                mock.patch.object(user_zone, "CONF", self.checkout / "config/user_zone.conf"), \
                mock.patch.object(main_publish, "ssh", side_effect=send), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(zone.set_zone("Curacao"), 0)
        self.assertIn("mac: a newer Save is already live", output.getvalue())
        self.assertNotIn("mac: live now:", output.getvalue())
        self.assertEqual(self.log("broadcast.log"), [])
        self.assertFalse(self.queued().exists())
        self.assertEqual((mac / "config/user_zone.conf").read_text(), CONF_TEXT.replace(INITIAL_ZONE, "Europe/Paris"))

    def test_an_offline_mac_is_queued_and_a_bare_zone_sends_it_later(self) -> None:
        _ = (self.fakes / "OFFLINE").write_text("", encoding="utf-8")
        result = self.run_zone("set", "America/Curacao")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("mac: queued (ssh: connect to host mac port 22: No route to host)", result.stdout)
        (sent,) = self.log("broadcast.log")
        self.assertEqual(sent[:3], ["--here", "--from", "zone"])
        self.assertTrue(self.queued().exists())
        still = self.run_zone()
        self.assertIn("mac: the America/Curacao change still waits since", still.stdout)
        (self.fakes / "OFFLINE").unlink()
        shown = self.run_zone()
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertIn("zone: America/Curacao (AST", shown.stdout)
        self.assertIn("mac: took the America/Curacao change queued at", shown.stdout)
        calls = self.log("ssh.log")
        self.assertIn("git fetch -q --no-write-fetch-head", calls[-2][-1])
        self.assertIn('broadcast.py" --here --from zone --all', calls[-1][-1])
        self.assertFalse(self.queued().exists())

        with self.subTest(queue="legacy pull without checkout fields"):
            _ = self.queued().write_text(json.dumps({"zone": "legacy", "text": "", "since": "2026-11-01",
                                                   "reason": "offline"}))
            shown = self.run_zone()
            self.assertEqual(shown.returncode, 0, shown.stderr)
            self.assertIn("mac: took the legacy change", shown.stdout)
            command = self.log("ssh.log")[-1][-1]
            self.assertIn('cd "$HOME"/.claude && { [ ! -f scripts/lib/waiting_rows.py ] || '
                          + "scripts/lib/py scripts/lib/waiting_rows.py fast-forward HEAD; } && tip=$(git ls-remote", command)
            self.assertIn(f"natedev:{self.checkout} main", command)
            self.assertFalse(self.queued().exists())

        with self.subTest(queue="zone notice with an explicit default pull"):
            _ = self.queued().write_text(json.dumps({"zone": "zone", "text": "Zone changed", "since": "2026-11-01",
                "reason": "offline", "pulls": [{"mac": "~/.claude", "source": str(self.checkout)}]}))
            before = len(self.log("ssh.log"))
            shown = self.run_zone()
            self.assertEqual(shown.returncode, 0, shown.stderr)
            calls = self.log("ssh.log")[before:]
            self.assertEqual(sum("git fetch -q --no-write-fetch-head" in call[-1] for call in calls), 1)
            self.assertIn("broadcast.py", calls[-1][-1])
            self.assertFalse(self.queued().exists())

    def test_a_newer_mac_write_clears_the_older_queued_notice(self) -> None:
        offline = self.fakes / "OFFLINE"
        _ = offline.write_text("")
        first = self.run_zone("set", "Curacao")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertTrue(self.queued().exists())
        offline.unlink()
        before = len(self.log("ssh.log"))

        second = self.run_zone("set", "Paris")

        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertIn("mac: live now: zone=Europe/Paris", second.stdout)
        self.assertFalse(self.queued().exists())
        self.assertFalse(any("broadcast.py" in call[-1] for call in self.log("ssh.log")[before:]))

    def test_a_late_mac_write_failure_cannot_queue_a_superseded_notice(self) -> None:
        def write_mac(stem: str, key: str, value: str, *, order: int) -> str:
            self.assertEqual((stem, key), ("user_zone", "zone"))
            if value == "America/Curacao":
                self.assertEqual(order, 1)
                self.assertEqual(zone.set_zone("Paris"), 0)
                return "mac: not live yet (fixture timeout)"
            self.assertEqual((value, order), ("Europe/Paris", 2))
            return "mac: live now: zone=Europe/Paris"

        with (mock.patch.dict(os.environ, self.env, clear=True),
              mock.patch.object(user_zone, "CONF", self.checkout / "config/user_zone.conf"),
              mock.patch.object(live_settings, "write_mac", side_effect=write_mac),
              mock.patch.object(live_settings, "publish", return_value=[]),
              mock.patch.object(zone, "broadcast_on_mac", return_value=(False, "zone differs")) as retry):
            self.assertEqual(zone.set_zone("Curacao"), 0)

        retry.assert_not_called()
        self.assertFalse(self.queued().exists())

    def test_a_promotion_without_a_tip_receipt_retries_the_saved_pull(self) -> None:
        _ = (self.fakes / "OFFLINE").write_text("", encoding="utf-8")
        _ = self.run_zone("set", "Curacao")
        (self.fakes / "OFFLINE").unlink()
        before = len(self.log("ssh.log"))
        drained = self.run_zone("drain", "--pulled", "~/.claude")
        self.assertIn("mac: took the America/Curacao change", drained.stdout)
        calls = self.log("ssh.log")[before:]
        self.assertEqual(len(calls), 2)
        self.assertIn("git fetch -q --no-write-fetch-head", calls[0][-1])
        self.assertIn("broadcast.py", calls[1][-1])
        self.assertEqual(self.run_zone("drain", "--pulled", "~/.claude").stdout, "")

        with self.subTest(pulled="~/custom checkout"):
            _ = (self.fakes / "OFFLINE").write_text("")
            _ = self.run_zone("set", "Paris")
            before = self.queued().read_bytes()
            drained = self.run_zone("drain", "--pulled", "~/custom checkout")
            self.assertEqual(drained.returncode, 0, drained.stderr)
            self.assertEqual(self.queued().read_bytes(), before)
            (self.fakes / "OFFLINE").unlink()
            drained = self.run_zone("drain", "--pulled", "~/custom checkout")
            self.assertEqual(drained.returncode, 0, drained.stderr)
            self.assertIn("broadcast.py", self.log("ssh.log")[-1][-1])
            self.assertFalse(self.queued().exists())

    def test_a_pull_of_old_main_keeps_the_zone_notice_queued(self) -> None:
        mac_home = self.root / "mac"
        mac = mac_home / ".claude"
        mac_conf = mac / "config/user_zone.conf"
        mac_conf.parent.mkdir(parents=True)
        wrapper = mac / "scripts/lib/py"
        wrapper.parent.mkdir(parents=True)
        sent = self.root / "mac-notice"
        source = SCRIPT.resolve().parents[2]
        _ = wrapper.write_text(
            "#!/bin/sh\ncase \"$1\" in\n"
            + f"  -c) PYTHONPATH={shlex.quote(str(source))} exec {shlex.quote(sys.executable)} -S \"$@\" ;;\n"
            + f"  */broadcast.py) touch {shlex.quote(str(sent))}; echo 'mac: fixture - sent' ;;\n"
            + "esac\n")
        wrapper.chmod(0o755)
        _ = (self.fakes / "ssh.py").write_text(
            "import os, subprocess, sys\nfrom pathlib import Path\n"
            + "command = sys.argv[-1]\n"
            + "if 'live_settings.py' in command:\n    sys.exit(255)\n"
            + "if 'broadcast.py' in command:\n"
            + f"    sys.exit(subprocess.call(['/bin/sh', '-c', command], env={{**os.environ, 'HOME': {str(mac_home)!r}}}))\n"
            + f"live = subprocess.check_output(['git', '-C', {str(self.checkout)!r}, 'show', 'main:config/user_zone.conf'])\n"
            + f"Path({str(mac_conf)!r}).write_bytes(live)\n"
            + "print('rc=0')\n")
        base = self.git(self.checkout, "rev-parse", "HEAD").strip()
        with (mock.patch.dict(os.environ, self.env, clear=True),
              mock.patch.object(user_zone, "CONF", self.checkout / "config/user_zone.conf"),
              mock.patch.object(live_settings, "request_publication", return_value=land_queue.SETTINGS_WAIT_NOTICE)):
            with land_runs.landing([base]):
                self.assertEqual(zone.set_zone("Curacao"), 0)
                self.assertEqual(mac_conf.read_text(), CONF_TEXT)
                self.assertFalse(sent.exists())
                self.assertTrue(self.queued().exists())
            _ = live_settings.publish(self.checkout)
            self.assertIn("mac: took the America/Curacao change", "\n".join(zone.drain(self.checkout, pulled=None)))
        self.assertEqual(mac_conf.read_text(), CONF_TEXT.replace(INITIAL_ZONE, "America/Curacao"))
        self.assertTrue(sent.exists())
        self.assertFalse(self.queued().exists())

    def test_an_ambiguous_name_changes_nothing(self) -> None:
        result = self.run_zone("set", "AST")
        self.assertEqual(result.returncode, 2)
        self.assertIn("zone: 'AST' is the label of", result.stderr)
        self.assertEqual((self.checkout / "config/user_zone.conf").read_text(encoding="utf-8"), CONF_TEXT)
        self.assertEqual(self.log("ssh.log") + self.log("broadcast.log"), [])

    def test_set_retries_a_zone_written_before_its_commit(self) -> None:
        conf = self.checkout / "config/user_zone.conf"
        head = self.git(self.checkout, "rev-parse", "HEAD")
        with mock.patch.dict(os.environ, self.env, clear=True), \
                mock.patch.object(user_zone, "CONF", conf), \
                mock.patch.object(live_settings, "publish", side_effect=RuntimeError("stopped before commit")), \
                self.assertRaises(RuntimeError):
            _ = zone.set_zone("Curacao")
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), head)
        self.assertIn("zone=America/Curacao", conf.read_text())
        retried = self.run_zone("set", "Curacao")
        self.assertEqual(retried.returncode, 0, retried.stdout + retried.stderr)
        self.assertIn("was: the same; nothing to change", retried.stdout)
        self.assertIn("mac: pulled", retried.stdout)
        self.assertEqual(self.git(self.checkout, "show", "origin/main:config/user_zone.conf"),
                         CONF_TEXT.replace(INITIAL_ZONE, "America/Curacao"))
        self.assertEqual(self.git(self.checkout, "log", "-1", "--format=%s").strip(),
                         "zone(America/Curacao): set with /zone (AST)")
        self.assertNotIn("config/user_zone.conf", self.git(self.checkout, "status", "--short"))

    def test_the_same_zone_again_makes_no_commit(self) -> None:
        head = self.git(self.checkout, "rev-parse", "HEAD")
        result = self.run_zone("set", INITIAL_ZONE)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("was: the same; nothing to change", result.stdout)
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), head)

    def test_zone_publishes_a_previous_tune_commit_before_pulling_the_mac(self) -> None:
        other = self.checkout / "config/example.conf"
        _ = other.write_text("# range: 1..10\namount=3\n")
        _ = self.git(self.checkout, "add", str(other))
        _ = self.git(self.checkout, "commit", "-qm", "tune(example): amount 2 -> 3")
        result = self.run_zone("set", "Curacao")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"),
                         self.git(self.checkout, "rev-parse", "origin/main"))
        self.assertEqual(self.git(self.checkout, "show", "origin/main:config/example.conf"),
                         "# range: 1..10\namount=3\n")
        self.assertIn("git fetch -q --no-write-fetch-head", self.log("ssh.log")[1][-1])

    def test_unrelated_local_work_keeps_the_zone_live_and_does_not_pull_the_mac(self) -> None:
        _ = self.git(self.checkout, "add", "other.txt")
        _ = self.git(self.checkout, "commit", "-qm", "unrelated work")
        remote = self.git(self.checkout, "rev-parse", "origin/main")
        head = self.git(self.checkout, "rev-parse", "HEAD")
        result = self.run_zone("set", "Curacao")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("other commit", result.stderr)
        self.assertIn("America/Curacao stays live; main does not have it", result.stderr)
        self.assertIn("zone=America/Curacao", (self.checkout / "config/user_zone.conf").read_text())
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), head)
        self.assertEqual(self.git(self.checkout, "rev-parse", "origin/main"), remote)
        (write,) = self.log("ssh.log")
        self.assertIn("live_settings.py", write[-1])
        self.assertFalse(self.queued().exists())

    def test_zone_uses_the_live_settings_time_limits(self) -> None:
        original_communicate = subprocess.Popen[str].communicate
        observed: dict[str, list[float | None]] = {}

        def communicate(process: subprocess.Popen[str], input: str | None = None,
                        timeout: float | None = None) -> tuple[str, str]:
            command = cast(list[str], process.args)
            if command[0] == "git" and command[3] in ("fetch", "push"):
                observed.setdefault(command[3], []).append(timeout)
            elif command[0] == self.env["ZONE_SSH"]:
                observed.setdefault("ssh", []).append(timeout)
            return original_communicate(process, input, timeout)

        with mock.patch.dict(os.environ, self.env, clear=True), \
                mock.patch.object(user_zone, "CONF", self.checkout / "config/user_zone.conf"), \
                mock.patch.object(subprocess.Popen, "communicate", communicate):
            status = zone.set_zone("Curacao")
        self.assertEqual(status, 0)
        self.assertEqual(observed, {"fetch": [10, 10], "push": [10], "ssh": [10, 20]})
        self.assertIn("LIVE_SETTINGS_SAVE_ORDER=1 ", self.log("ssh.log")[0][-1])

    def test_a_checkout_off_main_is_refused(self) -> None:
        _ = self.git(self.checkout, "switch", "-q", "-c", "work")
        result = self.run_zone("set", "Curacao")
        self.assertEqual(result.returncode, 2)
        self.assertIn("is on work, not main", result.stderr)
        (write,) = self.log("ssh.log")
        self.assertIn("live_settings.py", write[-1])


if __name__ == "__main__":
    _ = unittest.main()
