"""The one machine watch: one notice per outage, to the right receiver, for every machine kind."""

from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import cast, final, override
from unittest import mock

from scripts.aws_target import hana_box, hana_placement
from scripts.build_targets import target_config, targets
from scripts.build_targets.target_config import Target
from scripts.buildlog import cli, mac_idle, mac_idle_alert, machine_facts, machine_watch, sample
from scripts.buildlog.mac_idle_test_support import NotificationGuardedTestCase
from scripts.buildlog.test_index import use_test_log
from scripts.direct import step_record
from scripts.lib import conf
from scripts.lint.memory_test_support import write_config_fixture
from scripts.mac_test import mac_test
from scripts.message import send_budget

use_test_log()

START = 100_000.0
HOUR = 3600.0


def write_block(path: Path, block: mac_test.ActiveMacBlock) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mac_test.write_block(path, block)


class FakeController:
    """A read-only Hana controller whose image is current or stale."""

    def __init__(self, current: bool) -> None:
        self.current: bool = current

    def inspect(self) -> object:
        snapshot = mock.Mock(spec=hana_box.HanaLifecycleEvidence)
        snapshot.deployment = "deployment"
        return snapshot

    def image_is_current(self, _deployment: object) -> bool:
        return self.current


@final
class MachineWatchTests(NotificationGuardedTestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root = Path()
        self.paths = machine_watch.WatchPaths(Path(), mac_idle.MacIdlePaths(Path(), Path(), Path()))
        self.commands: list[list[str]] = []
        self.texts: list[str] = []
        self.image_current = True
        self.gpu: tuple[str, ...] | hana_placement.HanaStepRefused = ("g6.xlarge",)
        self.ssh_state: targets.TargetState = targets.Free()
        self.refused: set[str] = set()

    @override
    def setUp(self) -> None:
        super().setUp()
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        config = self.root / "config"
        config.mkdir()
        self.enterContext(mock.patch.dict(os.environ, {
            "CONF_DIR": str(config),
            "BUILD_TARGETS_STATE_DIR": str(self.root / "targets"),
            "MAC_TEST_STATE_DIR": str(self.root / "mac-state"),
            "MAC_IDLE_STATE_DIR": str(self.root / "mac-idle"),
            "MACHINE_WATCH_STATE_DIR": str(self.root / "watch"),
            "VERIFY_STEPS_DIR": str(self.root / "steps"),
            "BUILDLOG_DIR": str(self.root / "buildlog"),
            "BUILD_HOLD_DIR": str(self.root / "holds"),
        }))
        _ = write_config_fixture(config / "build_targets.conf", "enabled.mac-linux=on\n")
        _ = write_config_fixture(config / "mac_test.conf", "offload=on\n")
        for stem in ("mac_linux", "hana_aws", "cloud_gate", "lint"):
            _ = write_config_fixture(config / f"{stem}.conf")
        (self.root / "mac-state").mkdir()
        self.paths = machine_watch.watch_paths()
        _ = self.enterContext(mock.patch.object(subprocess, "run", side_effect=self.fake_run))
        _ = self.enterContext(mock.patch.object(send_budget, "seconds", return_value=30.0))
        _ = self.enterContext(mock.patch.object(hana_placement, "controller_for", side_effect=self.controller))
        _ = self.enterContext(mock.patch.object(hana_placement, "gpu_setup", side_effect=self.gpu_setup))
        _ = self.enterContext(mock.patch.object(targets, "ssh_target_state", side_effect=self.read_ssh_state))

    def controller(self, *_args: object, **_kwargs: object) -> FakeController:
        return FakeController(self.image_current)

    def gpu_setup(self, _transport: object) -> tuple[str, ...] | hana_placement.HanaStepRefused:
        return self.gpu

    def read_ssh_state(self, _target: object) -> targets.TargetState:
        return self.ssh_state

    def fake_run(self, command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        """Record each send.py call. No push or message leaves the test. A receiver in `refused` gets no message."""
        if not any(word.endswith("send.py") for word in command):
            return subprocess.CompletedProcess(command, 1, "", "")
        self.commands.append(command)
        self.texts.append(str(kwargs.get("input", "")))
        if "--to" in command and command[command.index("--to") + 1] in self.refused:
            return subprocess.CompletedProcess(command, 1, "", "")
        return subprocess.CompletedProcess(command, 0, "SENT\n", "")

    def target(self, name: str) -> Target:
        return next(target for target in machine_watch.machines() if target.name == name)

    def notice(self, name: str) -> float:
        key = "excluded_notice_seconds.mac" if name == "mac" else "excluded_notice_seconds"
        return conf.read(self.root / "config/build_targets.conf").value(
            key, conf.positive_whole_seconds, host_override=conf.HostOverride.IGNORED)

    def watch(self, name: str, now: float) -> None:
        machine_watch.watch_machine(self.target(name), self.paths, now, conf.read("build_targets"))

    def write_alert(self, now: float, state: str, cause: str, waiting: int, since: float = START) -> None:
        mac_idle.write_record(self.paths.mac.state / "alert.json", {
            "version": 1, "written": now, "state": state, "since": since, "changed": since, "waiting": waiting,
            "cause": cause, "holder": "", "detail": "", "until": now + HOUR if state == "unreachable" else 0,
            "messaged": True})

    def mac_pass(self, now: float, state: str = "unreachable", cause: str = "", waiting: int = 2) -> None:
        self.write_alert(now, state, cause, waiting)
        self.watch("mac", now)

    def step(self, name: str, state: step_record.StepState, targets_: tuple[str, ...], selected: str = "") -> None:
        step_record.write(step_record.StepRecord(
            name, os.getpid(), step_record.process_start(os.getpid()), self.root / "repo", "test",
            step_record.NamedPackage("sample"), step_record.AnyTarget(targets_), state, "2026-11-23T00:00:00Z",
            selected_target=selected))

    def unusable_pass(self, name: str, now: float) -> None:
        if name == "mac":
            self.mac_pass(now)
        else:
            self.watch(name, now)

    def waiting_pass(self, name: str, now: float) -> None:
        if name == "mac":
            self.mac_pass(now, "idle", "free", 3)
        else:
            self.watch(name, now)

    def make_unusable(self, name: str) -> None:
        if name == "mac-linux":
            self.ssh_state = targets.Unreachable("failed probe")
        elif name == "hana-aws":
            self.image_current = False
        elif name == "hana-gpu":
            self.gpu = hana_placement.HanaStepRefused("Hana GPU setup is required.")

    def receivers(self) -> list[str]:
        """Name each receiver: "nate" for a phone push, else the session that gets the message."""
        return ["nate" if "--text-id" in command else command[command.index("--to") + 1]
                for command in self.commands]

    def option(self, flag: str, index: int = -1) -> str:
        command = self.commands[index]
        return command[command.index(flag) + 1]

    def test_the_mac_uses_its_own_notice_time_and_another_machine_uses_the_shared_one(self) -> None:
        settings = conf.read("build_targets")
        self.assertEqual(machine_watch.notice_seconds("mac", settings), self.notice("mac"))
        shared = self.notice("natedev")
        for name in ("natedev", "mac-linux", "hana-aws", "hana-gpu"):
            with self.subTest(machine=name):
                self.assertEqual(machine_watch.notice_seconds(name, settings), shared)

    def test_an_unusable_machine_sends_one_notice_to_its_receivers_after_its_notice_time(self) -> None:
        sessions = ["bob", "builds"]
        for name, receivers in (("mac", ["nate"]), ("mac-linux", ["nate"]),
                                ("hana-aws", sessions), ("hana-gpu", sessions)):
            with self.subTest(machine=name):
                notice = self.notice(name)
                self.commands.clear()
                self.make_unusable(name)
                for now in (START, START + notice - 1):
                    self.unusable_pass(name, now)
                self.assertEqual(self.commands, [])
                self.unusable_pass(name, START + notice)
                self.assertEqual(self.receivers(), receivers)
                self.unusable_pass(name, START + notice + 60)
                self.assertEqual(self.receivers(), receivers)
                if name == "mac":
                    self.assertEqual(self.option("--text-id"), "mac_idle.unreachable")
                    self.assertEqual(self.option("--key"), f"mac-idle-{int(START)}")
                elif receivers == ["nate"]:
                    self.assertEqual(self.option("--text-id"), machine_watch.UNREACHABLE_TEXT)
                    self.assertEqual(self.option("--key"), f"machine-watch-{name}-{int(START)}")
                else:
                    self.assertEqual(self.option("--key"), f"machine-watch-{name}-{int(START)}")
                    self.assertIn(f"{name} has run no work for {int(notice // 60)} min", self.texts[-1])

    def test_notices_off_stops_only_the_named_machine_push(self) -> None:
        _ = write_config_fixture(self.root / "config/build_targets.conf", "notices.mac=off\n")
        _ = write_config_fixture(self.root / "config/mac_linux.conf", "enabled=on\n")
        notice = self.notice("mac")
        now = START + notice
        self.mac_pass(now)
        self.assertEqual(self.commands, [])
        self.assertEqual(machine_watch.read_notice(self.paths.state / "mac.json"),
                         {"since": START, "sent": False, "kind": machine_watch.NoticeKind.PUSH})

        self.make_unusable("mac-linux")
        self.watch("mac-linux", START)
        self.watch("mac-linux", START + self.notice("mac-linux"))
        self.assertEqual(self.receivers(), ["nate"])
        self.assertEqual(self.option("--key"), f"machine-watch-mac-linux-{int(START)}")

        self.waiting_pass("mac", now + 1)
        self.waiting_pass("mac", now + 1 + notice)
        self.assertEqual(self.receivers(), ["nate", "bob", "builds"])

    def test_missing_notices_key_defaults_to_on(self) -> None:
        path = self.root / "config/build_targets.conf"
        _ = write_config_fixture(path, "notices.unknown=invalid\n")
        _ = path.write_text(path.read_text().replace("notices.mac=on\n", ""))
        settings = conf.read("build_targets")
        now = START + self.notice("mac")
        self.write_alert(now, "unreachable", "", 2)

        with mock.patch.object(conf.ConfigRead, "value", autospec=True,
                               side_effect=conf.ConfigRead.value) as read_value:
            machine_watch.watch_machine(self.target("mac"), self.paths, now, settings)

        self.assertEqual(self.receivers(), ["nate"])
        self.assertEqual(self.option("--text-id"), "mac_idle.unreachable")
        read_value.assert_any_call(settings, "notices.mac", conf.on_off, default=True, recover_invalid=False,
                                   host_override=conf.HostOverride.IGNORED)

    def test_invalid_notices_value_is_refused(self) -> None:
        _ = write_config_fixture(self.root / "config/build_targets.conf", "notices.mac=invalid\n")
        with self.assertRaisesRegex(conf.Unusable, r"notices\.mac='invalid': expected (on or off|one of on, off)"):
            self.mac_pass(START + self.notice("mac"))
        self.assertEqual(self.commands, [])

    def test_steps_that_wait_on_an_idle_machine_message_bob_and_builds_once(self) -> None:
        """A machine that takes no work is ours to repair, so no machine sends a phone push for it."""
        for name in ("natedev", "mac", "mac-linux", "hana-aws", "hana-gpu"):
            with self.subTest(machine=name):
                notice = self.notice(name)
                self.commands.clear()
                if name == "natedev":
                    self.step("waiting", step_record.StepState.WAITING_HOLD, (name,))
                elif name != "mac":
                    self.step("waiting", step_record.StepState.WAITING_SSH, (name,), selected=name)
                if name != "mac":
                    self.step("running", step_record.StepState.ON_SSH, ("other",), selected="other")
                for now in (START, START + notice - 1):
                    self.waiting_pass(name, now)
                self.assertEqual(self.commands, [])
                self.waiting_pass(name, START + notice)
                self.assertEqual(self.receivers(), ["bob", "builds"])
                self.waiting_pass(name, START + notice + 60)
                self.assertEqual(self.receivers(), ["bob", "builds"])
                self.assertEqual({self.option("--key", index) for index in (0, 1)},
                                 {f"machine-watch-{name}-{int(START)}"})
                steps = "3 steps wait" if name == "mac" else "1 step waits"
                self.assertIn(f"{steps} for it, and nobody turned it off.", self.texts[-1])
                if name != "mac":
                    # Work on the machine ends the outage, and a new wait starts a new one.
                    state = (step_record.StepState.ON_NATEDEV if name == "natedev" else step_record.StepState.ON_SSH)
                    self.step("running", state, (name,), selected="" if name == "natedev" else name)
                    self.waiting_pass(name, START + notice + 120)
                    self.assertFalse((self.paths.state / f"{name}.json").exists())
                    for record in (self.root / "steps").glob("*.json"):
                        record.unlink()
                    self.assertEqual(len(self.commands), 2)

    def test_a_machine_that_is_off_on_purpose_sends_nothing(self) -> None:
        config = self.root / "config"

        def block(name: str) -> None:
            since = datetime.fromtimestamp(START, timezone.utc)
            write_block(target_config.ssh_target_state_directory(name) / "block.json", mac_test.ActiveMacBlock(
                "research", "a recovery", since, since + timedelta(hours=4), mac_test.HolderQueue("research"),
                mac_test.NO_SHOWRUNNER, "off_before", mac_test.FREE_MESSAGE_NOT_NEEDED))

        for name, turn_off in (
                ("natedev", lambda: write_config_fixture(config / "build_targets.conf", "enabled.natedev=off\n")),
                ("mac", lambda: write_config_fixture(config / "mac_test.conf", "offload=off\n")),
                ("mac-linux", lambda: block("mac-linux")),
                ("hana-aws", lambda: write_config_fixture(config / "hana_aws.conf", "enabled=off\n")),
                ("hana-gpu", lambda: write_config_fixture(config / "cloud_gate.conf", "enabled=off\n"))):
            with self.subTest(machine=name):
                _ = turn_off()
                self.make_unusable(name)
                # natedev has no unusable cause, so its outage is a step that waits for its hold.
                if name == "natedev":
                    self.step("waiting", step_record.StepState.WAITING_HOLD, (name,))
                else:
                    self.step("waiting", step_record.StepState.WAITING_SSH, (name,), selected=name)
                for now in (START, START + HOUR, START + 2 * HOUR):
                    self.unusable_pass(name, now)
                self.assertEqual(self.commands, [])
                self.assertFalse((self.paths.state / f"{name}.json").exists())
                self.assertIsInstance(mac_test.machine_off_on_purpose(name), mac_test.OffOnPurpose)

    def test_hana_blocked_by_a_unit_is_off_on_purpose(self) -> None:
        since = datetime.fromtimestamp(START, timezone.utc)
        write_block(target_config.ssh_target_state_directory("hana-aws") / "block.json", mac_test.ActiveMacBlock(
            "research", "image work", since, since + timedelta(hours=4), mac_test.HolderQueue("research"),
            mac_test.NO_SHOWRUNNER, "off_before", mac_test.FREE_MESSAGE_NOT_NEEDED))
        self.assertEqual(mac_test.machine_off_on_purpose("hana-aws"),
                         mac_test.OffOnPurpose("research blocks it: image work"))
        self.assertIs(mac_test.machine_off_on_purpose("hana-aws", mac_test.AskingSession("research")),
                      mac_test.MACHINE_IN_USE)

    def write_launches(self, *rows: dict[str, object]) -> None:
        log = target_config.ssh_target_state_directory("aws") / "cloud-gate-launches.jsonl"
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a") as stream:
            _ = stream.write("".join(json.dumps(row) + "\n" for row in rows))

    def test_a_refused_cloud_gate_credential_makes_the_gpu_route_unusable(self) -> None:
        """The newest refusal decides, and the outage starts at the first refusal of the current run."""
        refused = {"result": "refused", "error_code": "AuthFailure"}
        self.write_launches({"time": START - 10 * HOUR, "result": "launched"}, {"time": START - 9 * HOUR, **refused},
                            {"time": START - 8 * HOUR, "result": "launched"}, {"time": START - 2 * HOUR, **refused},
                            {"time": START, "result": "finished"}, {"time": START, **refused})
        self.assertEqual(machine_watch.refused_credential(), machine_watch.Outage(
            machine_watch.Unusable("AWS could not authenticate the launch credentials."), START - 2 * HOUR))
        self.write_launches({"time": START + 60, "result": "launched"})
        self.assertIs(machine_watch.refused_credential(), machine_watch.USABLE)

    def test_a_capacity_refusal_does_not_end_a_credential_refusal_run(self) -> None:
        """The watch reads the run as the AWS page does: a capacity refusal is part of the run."""
        refused = {"result": "refused", "error_code": "AuthFailure"}
        self.write_launches({"time": START - 10 * HOUR, "result": "launched"}, {"time": START - 3 * HOUR, **refused},
                            {"time": START - 2 * HOUR, "result": "refused", "error_code": "InsufficientInstanceCapacity"},
                            {"time": START, **refused})
        self.assertEqual(machine_watch.refused_credential(), machine_watch.Outage(
            machine_watch.Unusable("AWS could not authenticate the launch credentials."), START - 3 * HOUR))

    def test_an_old_credential_refusal_sends_at_the_first_pass(self) -> None:
        notice = self.notice("hana-gpu")
        self.write_launches({"time": START - notice, "result": "refused", "error_code": "AuthFailure"})
        self.watch("hana-gpu", START)
        self.assertEqual(self.receivers(), ["bob", "builds"])
        self.write_launches({"time": START + 60, "result": "refused", "error_code": "AuthFailure"})
        self.watch("hana-gpu", START + 120)
        self.assertEqual(self.receivers(), ["bob", "builds"])
        self.assertEqual(self.option("--key"), f"machine-watch-hana-gpu-{int(START - notice)}")
        self.assertIn("AWS could not authenticate the launch credentials.", self.texts[-1])

    def test_a_change_of_notice_kind_starts_a_new_outage(self) -> None:
        """mac-linux goes from no answer (push) to waiting steps (message) and back; each kind sends once."""
        notice = self.notice("mac-linux")
        self.make_unusable("mac-linux")
        self.step("waiting", step_record.StepState.WAITING_SSH, ("mac-linux",), selected="mac-linux")
        for now in (START, START + notice):
            self.watch("mac-linux", now)
        self.assertEqual(self.receivers(), ["nate"])
        self.ssh_state = targets.Free()
        answered = START + notice + 60
        self.watch("mac-linux", answered)
        self.assertTrue(machine_watch.cleared("mac-linux", START))
        for now in (answered + notice - 1, answered + notice, answered + notice + 60):
            self.watch("mac-linux", now)
        self.assertEqual(self.receivers(), ["nate", "bob", "builds"])
        self.assertEqual(self.option("--key"), f"machine-watch-mac-linux-{int(answered)}")
        self.make_unusable("mac-linux")
        lost = answered + 2 * notice
        for now in (lost, lost + notice, lost + notice + 60):
            self.watch("mac-linux", now)
        self.assertEqual(self.receivers(), ["nate", "bob", "builds", "nate"])
        self.assertEqual(self.option("--key"), f"machine-watch-mac-linux-{int(lost)}")
        self.assertFalse(machine_watch.cleared("mac-linux", lost))

    def test_a_change_of_notice_kind_on_the_mac_starts_a_new_outage(self) -> None:
        notice = self.notice("mac")
        self.mac_pass(START + notice)
        self.assertEqual(self.receivers(), ["nate"])
        free = START + notice + 60
        for now in (free, free + notice - 1, free + notice, free + notice + 60):
            self.mac_pass(now, "idle", "free", 3)
        self.assertEqual(self.receivers(), ["nate", "bob", "builds"])
        self.assertEqual(self.option("--key"), f"machine-watch-mac-{int(free)}")
        self.mac_pass(free + notice + 120)
        self.assertEqual(self.receivers(), ["nate", "bob", "builds", "nate"])

    def test_a_disk_exclusion_with_waiting_steps_is_one_outage(self) -> None:
        """A free, disk and free Mac with waiting steps is one outage and sends one message."""
        notice = self.notice("mac")
        for now, cause in ((START, "free"), (START + 60, "disk"), (START + 120, "free"),
                           (START + notice, "disk"), (START + notice + 60, "free")):
            self.mac_pass(now, "idle", cause, 3)
        self.assertEqual(self.receivers(), ["bob", "builds"])
        self.assertEqual(self.option("--key"), f"machine-watch-mac-{int(START)}")

    def test_each_wait_state_counts_only_for_the_machine_it_waits_for(self) -> None:
        states = step_record.StepState
        for state, selected, counted in ((states.WAITING_HOLD, "", {"natedev"}),
                                         (states.WAITING_MAC, "", {"mac"}),
                                         (states.WAITING_SSH, "mac-linux", {"mac-linux"}),
                                         (states.WAITING_SSH, "", set[str]())):
            with self.subTest(state=state, selected=selected):
                self.step("waiting", state, ("natedev", "mac", "mac-linux", "hana-aws"), selected=selected)
                self.assertEqual({name for name in ("natedev", "mac", "mac-linux", "hana-aws")
                                  if machine_watch.waiting_steps(self.target(name))}, counted)

    def test_a_natedev_hold_in_force_is_off_on_purpose(self) -> None:
        holds = self.root / "holds"
        holds.mkdir()
        _ = (holds / "nate").write_text("2026-11-23T00:00:00Z kernel update\n")
        self.assertEqual(mac_test.machine_off_on_purpose("natedev"), mac_test.OffOnPurpose("nate holds it: kernel update"))
        self.step("waiting", step_record.StepState.WAITING_HOLD, ("natedev",))
        for now in (START, START + 2 * HOUR):
            self.watch("natedev", now)
        self.assertEqual(self.commands, [])
        self.assertFalse((self.paths.state / "natedev.json").exists())
        (holds / "nate").unlink()
        holds.rmdir()
        _ = holds.write_text("not a folder\n")
        self.assertIs(mac_test.machine_off_on_purpose("natedev"), mac_test.MACHINE_IN_USE)

    def test_a_lost_outage_ends_the_push_and_a_live_one_keeps_it(self) -> None:
        notice = self.notice("mac-linux")
        self.make_unusable("mac-linux")
        self.watch("mac-linux", START)
        self.watch("mac-linux", START + notice)
        self.assertEqual(len(self.commands), 1)
        self.assertFalse(machine_watch.cleared("mac-linux", START))
        self.assertTrue(machine_watch.cleared("mac-linux", START - 60))
        self.ssh_state = targets.Free()
        self.watch("mac-linux", START + notice + 60)
        self.assertTrue(machine_watch.cleared("mac-linux", START))
        self.assertEqual(machine_watch.main(["cleared", "--machine", "mac-linux", "--since", str(START)]), 0)

    def idle_block(self, reason: str, showrunner: mac_test.Showrunner, start: float = START) -> None:
        since = datetime.fromtimestamp(start, timezone.utc)
        mac_test.write_block(self.root / "mac-state/block.json", mac_test.ActiveMacBlock(
            "research", reason, since, since + timedelta(hours=4), mac_test.RecordedSession("research", "research"),
            showrunner, "off_before", mac_test.FREE_MESSAGE_NOT_NEEDED))

    def test_an_idle_unit_block_messages_bob_and_the_holder_showrunner_once(self) -> None:
        notice = self.notice("mac")
        self.idle_block("P15 F11 L0 run", mac_test.NamedShowrunner("builds"))
        for now in (START + 60, START + notice - 1):
            self.mac_pass(now, "idle", "blocked", 0)
        self.assertEqual(self.commands, [])
        for now in (START + notice, START + notice + 60):
            self.mac_pass(now, "idle", "blocked", 0)
        self.assertEqual(self.receivers(), ["bob", "builds"])
        self.assertEqual({self.option("--key", index) for index in (0, 1)},
                         {f"machine-watch-mac-block-{int(START)}"})
        self.assertIn(f"research has blocked the Mac for {int(notice // 60)} min", self.texts[0])

    def test_an_idle_block_counts_from_the_later_of_the_idle_start_and_the_block_start(self) -> None:
        notice = self.notice("mac")
        placed = START + 300
        self.idle_block("P15 F11 L0 run", mac_test.NamedShowrunner("builds"), placed)
        self.mac_pass(START + notice, "idle", "blocked", 0)
        self.assertEqual(self.commands, [])
        self.mac_pass(placed + notice, "idle", "blocked", 0)
        self.assertEqual(self.receivers(), ["bob", "builds"])
        self.assertEqual(self.option("--key"), f"machine-watch-mac-block-{int(placed)}")

    def test_a_new_idle_block_sends_again(self) -> None:
        notice = self.notice("mac")
        self.idle_block("P15 F11 L0 run", mac_test.NamedShowrunner("builds"))
        self.mac_pass(START + notice, "idle", "blocked", 0)
        renewed = START + 2 * notice
        self.idle_block("P16 F2 L0 run", mac_test.NamedShowrunner("builds"), renewed)
        for now in (renewed + notice - 1, renewed + notice, renewed + notice + 60):
            self.mac_pass(now, "idle", "blocked", 0)
        self.assertEqual(self.receivers(), ["bob", "builds", "bob", "builds"])
        self.assertEqual(self.option("--key"), f"machine-watch-mac-block-{int(renewed)}")

    def test_a_partial_block_send_retries_only_the_failed_receiver(self) -> None:
        notice = self.notice("mac")
        self.idle_block("P15 F11 L0 run", mac_test.NamedShowrunner("builds"))
        self.refused = {"builds"}
        with redirect_stderr(io.StringIO()):
            self.mac_pass(START + notice, "idle", "blocked", 0)
        self.refused = set()
        for now in (START + notice + 60, START + notice + 120):
            self.mac_pass(now, "idle", "blocked", 0)
        self.assertEqual(self.receivers(), ["bob", "builds", "builds"])

    def test_a_failed_block_check_does_not_stop_the_mac_push(self) -> None:
        notice = self.notice("mac")
        with mock.patch.object(machine_watch, "watch_idle_block", side_effect=OSError("fixture")), \
             redirect_stderr(io.StringIO()) as errors:
            self.mac_pass(START + notice)
        self.assertIn("mac block check failed: fixture", errors.getvalue())
        self.assertEqual(self.receivers(), ["nate"])

    def test_an_idle_block_finds_the_showrunner_through_the_holder_labels(self) -> None:
        notice = self.notice("mac")
        from scripts.message.unit_labels import UnitLabels

        self.idle_block("P15 F11 L0 run", mac_test.NO_SHOWRUNNER)
        with mock.patch.object(mac_idle, "unit_labels_for",
                               return_value={"research": UnitLabels("research", showrunner="sdlc")}):
            self.mac_pass(START + notice, "idle", "blocked", 0)
        self.assertEqual(self.receivers(), ["bob", "sdlc"])

    def test_a_block_that_nate_ordered_sends_no_message(self) -> None:
        notice = self.notice("mac")
        for reason, offload in (("nate closes the Mac", "on"), ("P15 F11 L0 run", "off")):
            with self.subTest(reason=reason, offload=offload):
                _ = write_config_fixture(self.root / "config/mac_test.conf", f"offload={offload}\n")
                self.idle_block(reason, mac_test.NamedShowrunner("builds"))
                for now in (START + notice, START + 2 * notice):
                    self.mac_pass(now, "idle", "blocked", 0)
                self.assertEqual(self.commands, [])

    def test_a_failed_machine_check_does_not_stop_the_other_machines(self) -> None:
        self.make_unusable("mac-linux")
        self.make_unusable("hana-aws")
        broken = [OSError("fixture"), machine_watch.Outage(machine_watch.Unusable("x"), machine_watch.FIRST_SEEN),
                  machine_watch.USABLE, machine_watch.Outage(machine_watch.Unusable("y"), machine_watch.FIRST_SEEN)]
        # The Mac has its own reading, so the four results go to hana-aws, mac-linux, natedev and hana-gpu.
        self.assertEqual([target.name for target in machine_watch.machines() if target.name != "mac"],
                         ["hana-aws", "mac-linux", "natedev", "hana-gpu"])
        with mock.patch.object(machine_watch, "unusable", side_effect=broken), \
             mock.patch.object(mac_idle, "watch_once"), redirect_stderr(io.StringIO()) as errors:
            machine_watch.watch_once(self.paths, START)
        self.assertIn("hana-aws check failed: fixture", errors.getvalue())
        saved = sorted(path.stem for path in self.paths.state.glob("*.json"))
        self.assertEqual(saved, ["hana-gpu", "mac-linux"])

    def test_minute_sample_runs_the_watch_on_natedev(self) -> None:
        with mock.patch.object(sample, "sample", return_value=sample.SampleTaken(1000, "natedev")), \
             mock.patch.object(machine_watch, "watch_once") as watch, \
             mock.patch.object(machine_facts, "sample_once") as facts:
            self.assertEqual(cli.main(["sample"]), 0)
        watch.assert_called_once()
        self.assertEqual(cast(float, watch.call_args.args[1]), 1000)
        facts.assert_called_once_with(machine_facts.state_path(), 1000)

    def test_the_saved_alert_must_be_fresh(self) -> None:
        self.write_alert(START, "unreachable", "", 2)
        stale = START + mac_idle_alert.read_idle_config().stale_seconds + 1
        self.assertIsInstance(machine_watch.mac_reading(self.paths, stale), machine_watch.NoChange)
        self.assertIsInstance(machine_watch.mac_reading(self.paths, START), machine_watch.Outage)


if __name__ == "__main__":
    _ = unittest.main()
