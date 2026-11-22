"""Check the machine facts pass with fake commands, connections and ledger readings."""
from __future__ import annotations

import json
import os
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
from scripts.buildlog.machine_facts import MacLink, MachineFacts, ProbeFailed, Probes
from scripts.buildlog.test_index import use_test_log


use_test_log()

UPS_MAINS = "battery.charge: 100\nbattery.runtime: 1800\nups.status: OL CHRG\n"
SSH_SETTINGS = "user natepiano\nhostname mac.example.invalid\nport 22\n"
MAC_ON_BATTERY = ("{ 19.56 14.16 13.85 }\n12\nNow drawing from 'Battery Power'\n"
                  " -InternalBattery-0 (id=23920739)\t85%; discharging; 2:16 remaining present: true\n")
MAC_DESKTOP = "{ 2.50 2.00 1.00 }\n24\nNow drawing from 'AC Power'\n"
LINK = MacLink("mac", 6, 15)


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
    probes: Probes = Probes(run=commands, connect=connected(0), mac_link=lambda: LINK, aws_box=no_box)

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.path = self.root / "machine-facts.json"
        loadavg = self.root / "loadavg"
        _ = loadavg.write_text("52.70 50.00 48.01 60/8310 2997407\n")
        self.commands = FakeCommands({"upsc": UPS_MAINS, "ssh -G": SSH_SETTINGS, "ssh": MAC_ON_BATTERY})
        self.probes = Probes(run=self.commands, connect=connected(3.2), loadavg=loadavg, cpus=lambda: 32,
                                     mac_link=lambda: LINK, aws_box=no_box)

    def sample(self, probes: Probes | None = None) -> dict[str, MachineFacts]:
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
        self.assertEqual(ssh, ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=6", "mac", machine_facts.MAC_COMMAND])
        self.assertIn((ssh, 15), self.commands.calls)
        self.assertEqual(json.loads(self.path.read_text())["written"], 1000.0)

    def test_natedev_without_ups_shows_no_battery(self) -> None:
        self.commands.answers["upsc"] = ProbeFailed("upsc: [Errno 2] No such file or directory: 'upsc'")
        self.assertIsNone(self.sample()["natedev"]["battery"])

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
        self.assertEqual(self.sample()["mac"], machine_facts.facts("read", link_ms=3.2,
                                                                    detail="Permission denied (publickey)."))

    def test_aws_box_running_has_no_reading(self) -> None:
        def running(_now: float) -> str:
            return "i-0123456789abcdef0"

        aws = self.sample(replace(self.probes, aws_box=running))["aws"]
        self.assertEqual(aws["state"], "box_running")
        self.assertIn("i-0123456789abcdef0", aws["detail"])

    def test_one_failed_probe_keeps_the_others(self) -> None:
        def broken_link() -> MacLink:
            raise RuntimeError("fixture defect")

        def ledger_gone(_now: float) -> str:
            raise ledger.LedgerUnavailable("ledger evidence is unavailable")

        machines = self.sample(replace(self.probes, mac_link=broken_link, aws_box=ledger_gone))
        self.assertEqual(machines["mac"], machine_facts.facts("failed", detail="RuntimeError: fixture defect"))
        self.assertEqual(machines["aws"], machine_facts.facts("failed", detail="ledger evidence is unavailable"))
        self.assertEqual(machines["natedev"]["load"], 52.7)

    def test_probes_run_in_parallel(self) -> None:
        barrier = threading.Barrier(3, timeout=5)

        def cpus() -> int:
            _ = barrier.wait()
            return 32

        def link() -> MacLink:
            _ = barrier.wait()
            return LINK

        def box(_now: float) -> str:
            _ = barrier.wait()
            return ""

        machines = self.sample(replace(self.probes, cpus=cpus, mac_link=link, aws_box=box))
        self.assertEqual([machines[name]["state"] for name in machine_facts.MACHINES], ["read", "read", "no_box"])

    def test_missing_or_invalid_file_reads_as_none(self) -> None:
        self.assertIsNone(machine_facts.read_facts(self.path))
        for text in ("{", "[]", json.dumps({"version": 1, "written": 1.0, "machines": {"mac": {"state": "read"}}})):
            with self.subTest(text=text):
                _ = self.path.write_text(text)
                self.assertIsNone(machine_facts.read_facts(self.path))

    def test_state_path_follows_its_environment_variable(self) -> None:
        with mock.patch.dict(os.environ, {"MACHINE_FACTS_FILE": str(self.path)}):
            self.assertEqual(machine_facts.state_path(), self.path)
        with mock.patch.dict(os.environ, {"BUILDLOG_DIR": str(self.root / "buildlog")}):
            _ = os.environ.pop("MACHINE_FACTS_FILE", None)
            self.assertEqual(machine_facts.state_path(), self.root / "machine-facts.json")


if __name__ == "__main__":
    _ = unittest.main()
