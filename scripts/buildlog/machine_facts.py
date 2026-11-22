"""Sample link delay, load and battery for the Build limits machine columns.

`buildlog sample` calls `sample_once` each minute on natedev. The pass probes natedev, the Mac and
the running AWS gate box in parallel threads and saves one state file that the Builds feed reads.
"""
from __future__ import annotations

import json
import math
import os
import re
import socket
import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypedDict, cast, get_args

from scripts.buildlog import store
from scripts.lib import conf
from scripts.lib.atomic_file import write_text_atomic

# The cadence of buildlog-sample.timer, which runs this pass.
SAMPLE_PERIOD_SECONDS = 60
# Facts older than three missed passes are stale.
STALE_SECONDS = 3 * SAMPLE_PERIOD_SECONDS
UPS_NAME = "cyberpower"
# upsc reads the local NUT server, which answers in milliseconds.
UPS_TIMEOUT_SECONDS = 5.0
MAC_COMMAND = "sysctl -n vm.loadavg hw.ncpu; pmset -g batt"
VERSION = 1
MACHINES = ("natedev", "mac", "aws")

FactsState = Literal["read", "load_unreadable", "unreachable", "no_box", "box_running", "failed"]
AddressInfo = tuple[socket.AddressFamily, socket.SocketKind, int, str,
                    tuple[str, int] | tuple[str, int, int, int] | tuple[int, bytes]]


class Battery(TypedDict):
    percent: int
    mains: bool


class MachineFacts(TypedDict):
    state: FactsState
    link_ms: float | None
    load: float | None
    cpus: int | None
    battery: Battery | Literal["unreadable"] | None
    detail: str


class SavedFacts(TypedDict):
    version: int
    written: float
    machines: dict[str, MachineFacts]


class ProbeFailed(Exception):
    """A command or connection gave no usable answer."""


class CommandMissing(ProbeFailed):
    """The command is not installed on this machine."""


@dataclass(frozen=True)
class MacLink:
    host: str
    connect_timeout_s: float
    probe_timeout_s: float


def state_path() -> Path:
    return Path(os.environ.get("MACHINE_FACTS_FILE", str(store.root().parent / "machine-facts.json"))).expanduser()


def run_command(arguments: Sequence[str], timeout: float) -> str:
    try:
        result = subprocess.run(arguments, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                timeout=timeout, check=False)
    except FileNotFoundError as error:
        raise CommandMissing(f"{arguments[0]}: {error}") from error
    except (OSError, subprocess.TimeoutExpired, UnicodeError) as error:
        raise ProbeFailed(f"{arguments[0]}: {error}") from error
    if result.returncode != 0:
        raise ProbeFailed(" ".join(result.stderr.split()) or f"{arguments[0]} exited {result.returncode}")
    return result.stdout


def resolve(host: str, port: int, timeout: float) -> AddressInfo:
    """Look up the address in a daemon thread, because getaddrinfo has no time limit of its own."""
    answer: list[AddressInfo | OSError] = []

    def lookup() -> None:
        try:
            answer.append(socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)[0])
        except OSError as error:
            answer.append(error)

    thread = threading.Thread(target=lookup, name="machine-facts-lookup", daemon=True)
    thread.start()
    thread.join(timeout)
    if not answer:
        raise TimeoutError(f"name lookup of {host} took more than {timeout:g} s")
    if isinstance(answer[0], OSError):
        raise answer[0]
    return answer[0]


def connect_ms(host: str, port: int, timeout: float) -> float:
    """Time one TCP connect after name resolution, so the delay is the link alone."""
    family, kind, protocol, _, address = resolve(host, port, timeout)
    with socket.socket(family, kind, protocol) as connection:
        connection.settimeout(timeout)
        started = time.perf_counter()
        connection.connect(address)
        return (time.perf_counter() - started) * 1000


def read_mac_link() -> MacLink:
    config = conf.read("mac_test")
    return MacLink(config.text("host"), config.number("connect_timeout_s"), config.number("probe_timeout_s"))


def running_gate_box(now: float) -> str:
    """The newest gate box that the cost ledger records as running, or "" when none runs."""
    from scripts.aws_target import ledger
    from scripts.build_targets import target_config

    evidence = ledger.CostLedger(target_config.ssh_target_state_directory("aws")).read_snapshot()
    running = [(row.at, row.state.instance_id) for row in evidence.gate_reservations
               if isinstance(row.state, ledger.GateRunning) and row.expires > now]
    return max(running, default=(0, ""))[1]


@dataclass(frozen=True)
class Probes:
    """The outside readings; tests replace each one."""

    run: Callable[[Sequence[str], float], str] = run_command
    connect: Callable[[str, int, float], float] = connect_ms
    loadavg: Path = Path("/proc/loadavg")
    cpus: Callable[[], int | None] = os.cpu_count
    mac_link: Callable[[], MacLink] = read_mac_link
    aws_box: Callable[[float], str] = running_gate_box


def facts(state: FactsState, *, link_ms: float | None = None, load: float | None = None, cpus: int | None = None,
          battery: Battery | Literal["unreadable"] | None = None, detail: str = "") -> MachineFacts:
    return {"state": state, "link_ms": link_ms, "load": load, "cpus": cpus, "battery": battery, "detail": detail}


def ups_battery(text: str) -> Battery | Literal["unreadable"]:
    """Read `upsc`; a charge or status that does not say mains or battery is unreadable."""
    values = dict(line.split(": ", 1) for line in text.splitlines() if ": " in line)
    status = values.get("ups.status", "").split()
    try:
        percent = round(float(values["battery.charge"]))
    except (KeyError, ValueError):
        return "unreadable"
    if "OB" in status:
        return {"percent": percent, "mains": False}
    if "OL" in status:
        return {"percent": percent, "mains": True}
    return "unreadable"


def natedev_facts(probes: Probes) -> MachineFacts:
    try:
        load: float | None = float(probes.loadavg.read_text().split()[0])
    except (OSError, ValueError, IndexError):
        load = None
    battery: Battery | Literal["unreadable"] | None
    try:
        battery = ups_battery(probes.run(["upsc", UPS_NAME], UPS_TIMEOUT_SECONDS))
    except CommandMissing:
        battery = None
    except ProbeFailed:
        battery = "unreadable"
    cpus = probes.cpus()
    state: FactsState = "read" if load is not None and cpus else "load_unreadable"
    return facts(state, load=load, cpus=cpus, battery=battery)


def mac_battery(text: str) -> Battery | None:
    match = re.search(r"InternalBattery\S*\s.*?(\d+)%", text)
    if match is None:
        return None
    return {"percent": int(match.group(1)), "mains": "'Battery Power'" not in text}


def mac_load(text: str) -> tuple[float, int]:
    """Read `sysctl -n vm.loadavg hw.ncpu`: `{ 1.0 2.0 3.0 }` and then the CPU count."""
    lines = text.splitlines()
    if len(lines) < 2:
        raise ValueError("the Mac answer has no load and CPU lines")
    return float(lines[0].strip("{} ").split()[0]), int(lines[1])


def ssh_address(probes: Probes, link: MacLink) -> tuple[str, int]:
    """Resolve the SSH alias the way ssh does, without a connection."""
    settings = dict(line.split(" ", 1) for line in probes.run(["ssh", "-G", link.host], link.connect_timeout_s).splitlines()
                    if " " in line)
    try:
        return settings["hostname"], int(settings["port"])
    except (KeyError, ValueError) as error:
        raise ProbeFailed(f"ssh -G {link.host} gave no host name and port") from error


def mac_facts(probes: Probes) -> MachineFacts:
    try:
        link = probes.mac_link()
        host, port = ssh_address(probes, link)
    except (ValueError, ProbeFailed) as error:
        return facts("failed", detail=str(error))
    try:
        link_ms = probes.connect(host, port, link.connect_timeout_s)
    except OSError as error:
        return facts("unreachable", detail=f"{host}:{port}: {error}")
    try:
        answer = probes.run(["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={link.connect_timeout_s:g}",
                             link.host, MAC_COMMAND], link.probe_timeout_s)
        load, cpus = mac_load(answer)
    except (ProbeFailed, ValueError) as error:
        return facts("load_unreadable", link_ms=link_ms, detail=str(error))
    return facts("read", link_ms=link_ms, load=load, cpus=cpus, battery=mac_battery(answer))


def aws_facts(probes: Probes, now: float) -> MachineFacts:
    from scripts.aws_target import ledger

    try:
        box = probes.aws_box(now)
    except ledger.LedgerUnavailable as error:
        return facts("failed", detail=str(error))
    if not box:
        return facts("no_box")
    # GateSecurityGroup in docs/plans/builds-aws/cloudformation.json has egress rules only,
    # so no connection from natedev can reach a gate box.
    return facts("box_running", detail=f"gate box {box} accepts no inbound connection")


LIVE_PROBES = Probes()


def sample_once(path: Path, at: float, probes: Probes = LIVE_PROBES) -> SavedFacts:
    """Probe each machine in its own thread and save one file; one failed probe keeps the others."""
    with ThreadPoolExecutor(max_workers=len(MACHINES)) as pool:
        futures = {"natedev": pool.submit(natedev_facts, probes), "mac": pool.submit(mac_facts, probes),
                   "aws": pool.submit(aws_facts, probes, at)}
        machines: dict[str, MachineFacts] = {}
        for machine, future in futures.items():
            try:
                machines[machine] = future.result()
            except Exception as error:  # noqa: BLE001 -- one probe's defect must not cost the other machines their facts
                machines[machine] = facts("failed", detail=f"{type(error).__name__}: {error}")
    saved: SavedFacts = {"version": VERSION, "written": at, "machines": machines}
    write_text_atomic(path, json.dumps(saved, separators=(",", ":")), new_file_mode=0o644)
    return saved


def finite(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def valid_battery(value: object) -> bool:
    if value is None or value == "unreadable":
        return True
    if not isinstance(value, dict):
        return False
    row = cast(dict[str, object], value)
    return frozenset(row) == Battery.__required_keys__ and type(row["percent"]) is int and type(row["mains"]) is bool


def valid_machine(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    row = cast(dict[str, object], value)
    cpus = row.get("cpus")
    return (frozenset(row) == MachineFacts.__required_keys__ and row["state"] in get_args(FactsState)
            and isinstance(row["detail"], str) and all(row[key] is None or finite(row[key]) for key in ("link_ms", "load"))
            and (cpus is None or type(cpus) is int and cpus > 0) and valid_battery(row["battery"]))


def read_facts(path: Path) -> SavedFacts | None:
    """The saved facts, or None when the file is missing or unreadable."""
    try:
        raw = cast(object, json.loads(path.read_text()))
    except (OSError, ValueError, UnicodeError):
        return None
    if not isinstance(raw, dict):
        return None
    row = cast(dict[str, object], raw)
    machines = row.get("machines")
    if (row.get("version") != VERSION or not finite(row.get("written")) or not isinstance(machines, dict)
            or not all(valid_machine(value) for value in cast(dict[str, object], machines).values())):
        return None
    return cast(SavedFacts, cast(object, row))
