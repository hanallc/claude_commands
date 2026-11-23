"""Check the machine facts pass with fake commands, connections and ledger readings."""
from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
import unittest
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import override
from unittest import mock

from scripts.aws_target import ledger
from scripts.buildlog import machine_facts
from scripts.buildlog.machine_facts import CommandMissing, FactsUnread, Machine, MachineFacts, ProbeFailed, Probes
from scripts.buildlog.test_index import use_test_log
from scripts.lib import conf
from scripts.build_targets.target_test_support import UNREACHABLE_WAIT_FIXTURE_SECONDS
from scripts.mac_test import eligibility, offload


use_test_log()

UPS_MAINS = "battery.charge: 100\nbattery.runtime: 1800\nups.status: OL CHRG\n"
SSH_SETTINGS = "user natepiano\nhostname mac.example.invalid\nport 22\n"
MAC_ON_BATTERY = ("{ 19.56 14.16 13.85 }\n12\nNow drawing from 'Battery Power'\n"
                  " -InternalBattery-0 (id=23920739)\t85%; discharging; 2:16 remaining present: true\n")
MAC_DESKTOP = "{ 2.50 2.00 1.00 }\n24\nNow drawing from 'AC Power'\n"
SETTINGS = eligibility.SshProbeSettings(conf.ConfigRead("mac_test", Path("mac_test.conf"), conf.settings(
    "host=mac\n# range: 5e-324..\nconnect_timeout_s=6\n# range: 5e-324..\nprobe_timeout_s=15\n"), False),
    UNREACHABLE_WAIT_FIXTURE_SECONDS)


def no_box(_now: float) -> str:
    return ""


class FakeCommands:
    """Answer each command by its first words; an exception answer is raised."""

    def __init__(self, answers: dict[str, str | ProbeFailed]) -> None:
        self.answers: dict[str, str | ProbeFailed] = answers
        self.calls: list[tuple[list[str], float]] = []

    def __call__(self, arguments: Sequence[str], timeout: float) -> str:
        self.calls.append((list(arguments), timeout))
        key = "ssh -G" if arguments[:2] == ["ssh", "-G"] else arguments[0]
        answer = self.answers[key]
        if isinstance(answer, ProbeFailed):
            raise answer
        return answer


def connected(ms: float) -> Callable[[str, int, float], float]:
    def connect(_host: str, _port: int, _timeout: float) -> float:
        return ms
    return connect


class MachineFactsTests(unittest.TestCase):
    root: Path = Path()
    path: Path = Path()
    commands: FakeCommands = FakeCommands({})
    probes: Probes = Probes(run=commands, connect=connected(0), mac_settings=lambda: SETTINGS, aws_box=no_box)

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.path = self.root / "machine-facts.json"
        loadavg = self.root / "loadavg"
        _ = loadavg.write_text("52.70 50.00 48.01 60/8310 2997407\n")
        self.commands = FakeCommands({"upsc": UPS_MAINS, "ssh -G": SSH_SETTINGS, "ssh": MAC_ON_BATTERY})
        self.probes = Probes(run=self.commands, connect=connected(3.2), loadavg=loadavg, cpus=lambda: 32,
                                     mac_settings=lambda: SETTINGS, aws_box=no_box)

    def sample(self, probes: Probes | None = None) -> dict[Machine, MachineFacts]:
        saved = machine_facts.sample_once(self.path, 1000.0, probes or self.probes)
        self.assertEqual(machine_facts.read_facts(self.path), saved)
        return saved["machines"]

    def test_pass_saves_link_load_and_battery_for_each_machine(self) -> None:
        machines = self.sample()
        self.assertEqual(machines["natedev"], machine_facts.facts("read", load=52.7, cpus=32,
                                                                  battery={"percent": 100, "mains": True}))
        self.assertEqual(machines["mac"], machine_facts.facts("read", link_ms=3.2, load=19.56, cpus=12,
                                                              battery={"percent": 85, "mains": False}))
        self.assertEqual(machines["aws"], machine_facts.facts("no_box"))
        ssh = next(arguments for arguments, _ in self.commands.calls if arguments[:2] == ["ssh", "-o"])
        self.assertEqual(ssh, ["ssh", *offload.ssh_options(SETTINGS), "mac", machine_facts.MAC_COMMAND])
        self.assertIn("ConnectTimeout=6", ssh)
        self.assertIn((ssh, 15), self.commands.calls)
        self.assertEqual(json.loads(self.path.read_text())["written"], 1000.0)

    def test_natedev_without_ups_shows_no_battery(self) -> None:
        self.commands.answers["upsc"] = CommandMissing("upsc: [Errno 2] No such file or directory: 'upsc'")
        self.assertEqual(self.sample()["natedev"]["battery"], "no_battery")

    def test_ups_that_does_not_answer_is_unreadable(self) -> None:
        for answer in (ProbeFailed("Error: Driver not connected"), "battery.charge: 90\nups.status: OFF\n",
                       "ups.status: OL\n"):
            with self.subTest(answer=answer):
                self.commands.answers["upsc"] = answer
                self.assertEqual(self.sample()["natedev"]["battery"], "unreadable")

    def test_ups_on_battery_is_not_mains(self) -> None:
        self.commands.answers["upsc"] = "battery.charge: 64\nups.status: OB DISCHRG\n"
        self.assertEqual(self.sample()["natedev"]["battery"], {"percent": 64, "mains": False})

    def test_mac_without_battery_shows_no_battery(self) -> None:
        self.commands.answers["ssh"] = MAC_DESKTOP
        self.assertEqual(self.sample()["mac"], machine_facts.facts("read", link_ms=3.2, load=2.5, cpus=24))

    def test_mac_link_failure_is_unreachable_and_skips_ssh(self) -> None:
        def refuse(host: str, port: int, _timeout: float) -> float:
            raise TimeoutError(f"{host}:{port} timed out")

        mac = self.sample(replace(self.probes, connect=refuse))["mac"]
        self.assertEqual(mac["state"], "unreachable")
        self.assertEqual([arguments[:2] for arguments, _ in self.commands.calls if arguments[0] == "ssh"], [["ssh", "-G"]])

    def test_mac_ssh_failure_keeps_the_link_delay(self) -> None:
        self.commands.answers["ssh"] = ProbeFailed("Permission denied (publickey).")
        self.assertEqual(self.sample()["mac"], machine_facts.facts("load_unreadable", link_ms=3.2,
                                                                    detail="Permission denied (publickey)."))

    def test_mac_answer_without_a_valid_load_is_load_unreadable(self) -> None:
        for answer in ("{ nan 1.0 1.0 }\n12\n", "{ -1.0 1.0 1.0 }\n12\n", "{ 1.0 1.0 1.0 }\n0\n"):
            with self.subTest(answer=answer):
                self.commands.answers["ssh"] = answer
                machines = self.sample()
                self.assertEqual(machines["mac"]["state"], "load_unreadable")
                self.assertIsNot(machine_facts.read_facts(self.path), FactsUnread.VALUE)

    def test_name_lookup_past_its_time_limit_is_a_timeout(self) -> None:
        release = threading.Event()

        def slow_lookup(*_arguments: object, **_options: object) -> list[machine_facts.AddressInfo]:
            _ = release.wait(5)
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 22))]

        try:
            with mock.patch("socket.getaddrinfo", slow_lookup), self.assertRaises(TimeoutError):
                _ = machine_facts.connect_ms("mac.example", 22, 0.05)
        finally:
            release.set()

    def test_name_lookup_error_fails_at_once(self) -> None:
        def bad_name(*_arguments: object, **_options: object) -> list[machine_facts.AddressInfo]:
            raise UnicodeError("encoding with 'idna' codec failed (UnicodeError: label empty or too long)")

        with mock.patch("socket.getaddrinfo", bad_name), self.assertRaises(OSError) as caught:
            _ = machine_facts.connect_ms("mac..example", 22, 5)
        self.assertNotIsInstance(caught.exception, TimeoutError)
        self.assertIsInstance(caught.exception.__cause__, ValueError)

    def test_aws_box_running_has_no_reading(self) -> None:
        def running(_now: float) -> str:
            return "i-0123456789abcdef0"

        aws = self.sample(replace(self.probes, aws_box=running))["aws"]
        self.assertEqual(aws["state"], "box_running")
        self.assertIn("i-0123456789abcdef0", aws["detail"])

    def test_one_failed_probe_keeps_the_others(self) -> None:
        def broken_settings() -> eligibility.SshProbeSettings:
            raise RuntimeError("fixture defect")

        def ledger_gone(_now: float) -> str:
            raise ledger.LedgerUnavailable("ledger evidence is unavailable")

        machines = self.sample(replace(self.probes, mac_settings=broken_settings, aws_box=ledger_gone))
        self.assertEqual(machines["mac"], machine_facts.facts("failed", detail="RuntimeError: fixture defect"))
        self.assertEqual(machines["aws"], machine_facts.facts("failed", detail="ledger evidence is unavailable"))
        self.assertEqual(machines["natedev"]["load"], 52.7)

    def test_probes_run_in_parallel(self) -> None:
        barrier = threading.Barrier(3, timeout=5)

        def cpus() -> int:
            _ = barrier.wait()
            return 32

        def settings() -> eligibility.SshProbeSettings:
            _ = barrier.wait()
            return SETTINGS

        def box(_now: float) -> str:
            _ = barrier.wait()
            return ""

        machines = self.sample(replace(self.probes, cpus=cpus, mac_settings=settings, aws_box=box))
        states: dict[Machine, str] = {name: facts["state"] for name, facts in machines.items()}
        self.assertEqual(states, {"natedev": "read", "mac": "read", "aws": "no_box"})

    def test_missing_or_invalid_file_reads_as_unread(self) -> None:
        self.assertIs(machine_facts.read_facts(self.path), FactsUnread.VALUE)
        for text in ("{", "[]", json.dumps({"version": 1, "written": 1.0, "machines": {"mac": {"state": "read"}}}),
                     json.dumps({"version": 1, "written": 1.0, "machines": {"pc": machine_facts.facts("no_box")}}),
                     json.dumps({"version": 1, "written": 1.0,
                                 "machines": {"mac": {**machine_facts.facts("read"), "battery": None}}})):
            with self.subTest(text=text):
                _ = self.path.write_text(text)
                self.assertIs(machine_facts.read_facts(self.path), FactsUnread.VALUE)

    def test_state_path_follows_its_environment_variable(self) -> None:
        with mock.patch.dict(os.environ, {"MACHINE_FACTS_FILE": str(self.path)}):
            self.assertEqual(machine_facts.state_path(), self.path)
        with mock.patch.dict(os.environ, {"BUILDLOG_DIR": str(self.root / "buildlog")}):
            _ = os.environ.pop("MACHINE_FACTS_FILE", None)
            self.assertEqual(machine_facts.state_path(), self.root / "machine-facts.json")


if __name__ == "__main__":
    _ = unittest.main()
