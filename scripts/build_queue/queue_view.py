"""Read build activity and prepare rows for the Builds page."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field as dataclass_field, replace
from functools import cached_property
from enum import Enum
import os
import inspect
import logging
import re
import shlex
import json
from pathlib import Path
import subprocess
import time
from typing import cast

from scripts.build_queue.scope_activity import ScopeProcesses, LiveBuildRun, NotServiceSlice
from scripts.build_queue import admission_protocol as wire, cloud_progress, progress_profile
from scripts.build_queue.admission_records import (reason_phrase as reason_phrase, behind_place as behind_place,
    eligible_head_position as eligible_head_position)
from scripts.build_queue.queue_evidence import step_labels as step_labels
from scripts.build_queue.queue_lines import Line, Span
from scripts.lib import cargo_target_config, conf
from scripts.lib.atomic_file import write_text_atomic
from scripts.lib.build_steps import RECORDED_STEP_NAMES, StepNames, read_step_names
from scripts.build_queue import admission_records
from scripts.lint import memory_admit as gate
from scripts.message.unit_labels import UnitLabels
from scripts.message import unit_labels


@dataclass(frozen=True)
class MacBuilding:
    """No test executable runs in this build's process tree."""


@dataclass(frozen=True)
class MacTesting:
    test_pid: int


@dataclass(frozen=True)
class MacTestingCounter:
    counter: progress_profile.ProgressCounter


@dataclass(frozen=True)
class MacStageUnavailable:
    """No process reading supplies the build stage."""


MacBuildStage = MacBuilding | MacTesting | MacTestingCounter | MacStageUnavailable


@dataclass(frozen=True)
class MacMemoryUnavailable:
    """No resident memory reading exists for this process tree."""


@dataclass(frozen=True)
class MacJobsUnavailable:
    """The command does not specify a positive jobs value."""


class MacBuildOrigin(Enum):
    CI = "ci"
    NON_CI = "non-ci"
    UNKNOWN = "unknown"


UNRECORDED_OWNER = UnitLabels("")


class MacGateLabel(Enum):
    USER = "user"
    PLACEMENT = "placement"
    CI = "hana-ci"
    UNPLACED = "unplaced"
    UNAVAILABLE = "unavailable"


def parse_gate_label(wire: str) -> MacGateLabel:
    if wire.startswith("placement "):
        return MacGateLabel.PLACEMENT
    return next((label for label in MacGateLabel if label.value == wire), MacGateLabel.UNAVAILABLE)


@dataclass(frozen=True)
class MacBuild:
    pid: int
    folder: str
    started_at: float
    command: str = ""
    repository: str = ""
    stage: MacBuildStage = MacStageUnavailable()
    resident_bytes: int | MacMemoryUnavailable = MacMemoryUnavailable()
    jobs: int | MacJobsUnavailable = MacJobsUnavailable()
    counter: progress_profile.ProgressCounter | progress_profile.NoCounter = progress_profile.NoCounter()
    recorded_unit: str = ""
    session: str = ""
    origin: MacBuildOrigin = MacBuildOrigin.UNKNOWN
    ci_runner: str = ""
    ci_job: str = ""
    gate_label: MacGateLabel = MacGateLabel.UNAVAILABLE
    gate_placement: str = ""


@dataclass(frozen=True)
class UnplacedMacRun:
    pid: int
    started_at: float
    command: str


def unplaced_run_record(run: UnplacedMacRun) -> dict[str, object]:
    return {"pid": run.pid, "started_at": run.started_at, "command": run.command}


def unplaced_run_from_record(row: dict[str, object]) -> UnplacedMacRun:
    return UnplacedMacRun(int(cast(int, row["pid"])), float(cast(float, row["started_at"])), str(row["command"]))


@dataclass(frozen=True)
class MacBuildReading:
    read_at: float
    builds: tuple[MacBuild, ...]
    error: str = ""
    unplaced_runs: tuple[UnplacedMacRun, ...] = ()
    home: str = ""


def mac_reading_path() -> Path:
    """Use the feed folder for the shared periodic Mac reading."""
    folder = Path(os.path.expandvars(conf.read("memory_feed").text("output_folder"))).expanduser()
    return folder / "mac-builds.json"


def mac_activity_refresh_seconds() -> int:
    return conf.read("memory_feed").whole("mac_activity_refresh_seconds")


def mac_command_jobs(command: str) -> int | MacJobsUnavailable:
    match = re.search(r"(?:^|\s)(?:-j\s*|--(?:jobs|test-threads)(?:=|\s+))([0-9]+)(?:\s|$)", command)
    return int(match[1]) if match and int(match[1]) > 0 else MacJobsUnavailable()


def mac_step_name(build: MacBuild, *, names: StepNames = RECORDED_STEP_NAMES,
                  identity: UnitLabels = UNRECORDED_OWNER) -> str:
    try:
        words = shlex.split(build.command)
    except ValueError:
        words = []
    step = "cargo"
    for index, word in enumerate(words):
        name = Path(word).name
        if name in ("cargo", "cargo-nextest"):
            step = "nextest" if name == "cargo-nextest" else "cargo"
            if name == "cargo-nextest":
                break
            for argument in words[index + 1:]:
                if argument in ("build", "check", "clippy", "nextest", "test", "run", "bench"):
                    step = argument
                    break
            break
        if name == "rustc":
            step = "rustc"
            break
    if step == "cargo" and isinstance(build.stage, (MacTesting, MacTestingCounter)):
        step = "test"
    package = ""
    for index, word in enumerate(words):
        if word in ("-p", "--package") and index + 1 < len(words):
            package = words[index + 1]
            break
        if word.startswith("--package="):
            package = word.removeprefix("--package=")
            break
        if word.startswith("-p") and len(word) > 2:
            package = word[2:]
            break
    current_name = " ".join(part for part in (package or build.repository, step) if part)
    return names.for_owner(step, identity.unit if identity.unit != "—" else "", identity.role,
                           ci=build.origin is MacBuildOrigin.CI, current_name=current_name)


def gate_label_text(build: MacBuild) -> str:
    """Serialize the gate identity for records. Keep placement ids out of visible names."""
    if build.gate_label == MacGateLabel.PLACEMENT and build.gate_placement:
        return "placement " + build.gate_placement
    return "" if build.gate_label == MacGateLabel.UNAVAILABLE else build.gate_label.value


def mac_build_record(build: MacBuild) -> dict[str, object]:
    return {"pid": build.pid, "folder": build.folder, "started_at": build.started_at,
            "command": build.command, "repository": build.repository,
            "test_pid": build.stage.test_pid if isinstance(build.stage, MacTesting) else
                0 if isinstance(build.stage, MacBuilding) else -1,
            "resident_bytes": build.resident_bytes if isinstance(build.resident_bytes, int) else -1,
            "counter": {"phase": build.counter.phase, "done": build.counter.done, "total": build.counter.total}
                if isinstance(build.counter, progress_profile.ProgressCounter) else {},
            "recorded_unit": build.recorded_unit, "session": build.session, "origin": build.origin.value,
            "ci_runner": build.ci_runner, "ci_job": build.ci_job, "gate_label": gate_label_text(build)}


def mac_build_from_record(row: dict[str, object]) -> MacBuild:
    test_pid = int(cast(int, row["test_pid"]))
    resident = int(cast(int, row["resident_bytes"]))
    command = str(row["command"])
    counter_data = cast(dict[str, object], row["counter"])
    counter = progress_profile.ProgressCounter(str(counter_data["phase"]), int(cast(int, counter_data["done"])),
        int(cast(int, counter_data["total"]))) if counter_data else progress_profile.NoCounter()
    stage = MacTesting(test_pid) if test_pid > 0 else MacBuilding() if test_pid == 0 else MacStageUnavailable()
    if isinstance(counter, progress_profile.ProgressCounter) and counter.phase.startswith("test:"):
        stage = MacTestingCounter(counter)
    return MacBuild(int(cast(int, row["pid"])), str(row["folder"]), float(cast(float, row["started_at"])),
                    command, str(row["repository"]), stage,
                    resident if resident >= 0 else MacMemoryUnavailable(), mac_command_jobs(command), counter,
                    str(row["recorded_unit"]), str(row["session"]),
                    MacBuildOrigin(str(row.get("origin", MacBuildOrigin.UNKNOWN.value))),
                    str(row.get("ci_runner", "")), str(row.get("ci_job", "")),
                    parse_gate_label(str(row.get("gate_label", ""))),
                    str(row.get("gate_label", "")).removeprefix("placement ")
                    if str(row.get("gate_label", "")).startswith("placement ") else "")


def read_mac_builds() -> MacBuildReading:
    """Read the cached Mac activity without a network call."""
    try:
        data = cast(dict[str, object], json.loads(mac_reading_path().read_text()))
        rows = cast(list[dict[str, object]], data["builds"])
        return MacBuildReading(float(cast(float, data["read_at"])), tuple(mac_build_from_record(row) for row in rows),
                               str(data.get("error", "")), tuple(unplaced_run_from_record(row)
                                   for row in cast(list[dict[str, object]], data.get("unplaced_runs", []))),
                               str(data.get("home", "")))
    except (OSError, ValueError, TypeError, KeyError) as error:
        logging.getLogger(__name__).warning("Mac reading unavailable: %s", error)
        return MacBuildReading(0, (), "Mac reading unavailable")


def mac_reading_status(reading: MacBuildReading, now: float) -> str:
    """Replace old native rows with one short status and the reading age."""
    elapsed = max(0, now - reading.read_at)
    if not reading.error and elapsed <= 2 * mac_activity_refresh_seconds():
        return ""
    age_text = cloud_update_age(elapsed) if reading.read_at else "unavailable"
    return f"Mac reading is old · age {age_text}"


def mac_build_script() -> str:
    """Read cargo tile registrations and process trees in one SSH call."""
    return "python3 - <<'MAC_READING'\n" + inspect.getsource(cargo_target_config) + r'''
import ctypes
import json
import re
import struct
import sys
import os
from pathlib import Path
import shlex
import subprocess
import time
import tomllib

CAPTURE_ROOT = Path("/tmp/cargo-tile")


def output(arguments):
    return subprocess.check_output(arguments, text=True, stderr=subprocess.DEVNULL,
                                   env=dict(os.environ, LC_ALL="C"), timeout=4)


def read_processes():
    rows = {}
    for line in output(["ps", "-axww", "-o", "pid=,ppid=,uid=,rss=,etime=,lstart=,comm="]).splitlines():
        fields = line.split(None, 10)
        if len(fields) != 11:
            continue
        pid, parent, uid, rss = map(int, fields[:4])
        try:
            born = int(time.mktime(time.strptime(" ".join(fields[5:10]), "%a %b %d %H:%M:%S %Y")))
        except ValueError:
            continue
        rows[pid] = dict(pid=pid, parent=parent, uid=uid, resident_bytes=rss * 1024,
                         started_at=born, executable=fields[10], command="")
    for line in output(["ps", "-axww", "-o", "pid=,args="]).splitlines():
        fields = line.split(None, 1)
        if len(fields) == 2 and int(fields[0]) in rows:
            rows[int(fields[0])]["command"] = fields[1]
    return rows


def read_captures(processes):
    try:
        boot = output(["sysctl", "-n", "kern.bootsessionuuid"]).strip()
    except (OSError, subprocess.SubprocessError):
        return {}
    captures = {}
    for account in CAPTURE_ROOT.glob("[0-9]*"):
        if not account.name.isdecimal() or account.is_symlink():
            continue
        for record in (account / "state/pids").glob("*"):
            try:
                pid_text, generation = record.name.split(".", 1)
                pid = int(pid_text)
                process = processes.get(pid)
                if not process or process["uid"] != int(account.name):
                    continue
                if record.is_symlink() or record.stat().st_size > 65536:
                    continue
                raw = record.read_bytes()
                if not raw.endswith(b"\0"):
                    continue
                fields = raw[:-1].split(b"\0")
                if len(fields) < 8 or fields[0] != b"cargo-tile-v3":
                    continue
                if fields[1].decode() != generation or fields[2].decode() != boot:
                    continue
                if int(fields[3]) != process["started_at"]:
                    continue
                log_name = fields[4].decode()
                if Path(log_name).name != log_name:
                    continue
                folder = fields[5].decode()
                if not Path(folder).is_absolute():
                    continue
                count_at = 7 if not fields[6] or fields[6].startswith(b"/") else 6
                arguments = [word.decode() for word in fields[count_at + 1:]]
                if int(fields[count_at]) != len(arguments):
                    continue
                log = account / log_name
                tail = ""
                if not log.is_symlink():
                    try:
                        with log.open("rb") as stream:
                            stream.seek(max(0, log.stat().st_size - 65536))
                            tail = stream.read(65536).decode(errors="replace")
                    except OSError:
                        pass
                captures[pid] = dict(folder=folder, command=shlex.join(["cargo", *arguments]), progress_tail=tail)
            except (OSError, ValueError, UnicodeError, IndexError):
                continue
    return captures


def project_record(folder):
    try:
        tree = Path(folder).resolve()
        for parent in (tree, *tree.parents):
            for name in (".copy-record.json", ".scratch.json", ".gate.json", ".gate-copy.json"):
                record = parent / name
                try:
                    if record.is_symlink() or record.stat().st_size > 65536:
                        continue
                    values = json.loads(record.read_text())
                    owner = values.get("owner_unit", values.get("owner", ""))
                    session = values.get("session", values.get("holder_session", ""))
                    repo = values.get("repo", "")
                    if name == ".gate-copy.json" and not tree.is_relative_to(parent):
                        continue
                    return dict(recorded_unit=owner if isinstance(owner, str) else "",
                                session=session if isinstance(session, str) else "",
                                repository=repo if isinstance(repo, str) else "")
                except (FileNotFoundError, ValueError, TypeError):
                    continue
            if (parent / ".git").exists():
                break
    except OSError:
        pass
    return dict(recorded_unit="", session="", repository="")


def repository_name(folder):
    try:
        remote = output(["git", "-C", folder, "config", "--get", "remote.origin.url"]).strip()
        for _ in range(8):
            if not remote.startswith("/"):
                break
            remote = output(["git", "-C", remote, "config", "--get", "remote.origin.url"]).strip()
        if "://" in remote or remote.startswith("git@"):
            return remote.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        manifest = tomllib.loads((Path(folder) / "Cargo.toml").read_text())
        package = manifest.get("workspace", {}).get("package", {}) or manifest.get("package", {})
        repository = package.get("repository", "")
        if isinstance(repository, str) and ("://" in repository or repository.startswith("git@")):
            return repository.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
    except (OSError, ValueError, TypeError):
        pass
    return ""


def process_environment(pid):
    """Read only Cargo paths, CI labels and the queue placement from the Mac environment."""
    if sys.platform != "darwin":
        return {}
    try:
        kernel = ctypes.CDLL(None, use_errno=True)
        kernel.sysctl.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.c_uint, ctypes.c_void_p,
                                  ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p, ctypes.c_size_t]
        kernel.sysctl.restype = ctypes.c_int
        # sysinfo uses CTL_KERN and KERN_PROCARGS2 for the same process observation.
        mib = (ctypes.c_int * 3)(1, 49, pid)
        size = ctypes.c_size_t()
        if kernel.sysctl(mib, 3, None, ctypes.byref(size), None, 0) or not 4 <= size.value <= 2097152:
            return {}
        data = ctypes.create_string_buffer(size.value)
        if kernel.sysctl(mib, 3, data, ctypes.byref(size), None, 0):
            return {}
        return cargo_environment(data.raw[:size.value])
    except (OSError, ValueError, AttributeError):
        return {}


def cargo_environment(data):
    """Keep only Cargo paths, CI labels and the queue placement from the environment."""
    if len(data) < 4:
        return {}
    argc = struct.unpack_from("i", data)[0]
    if argc < 1:
        return {}
    start = data.find(b"\0", 4)
    if start < 0:
        return {}
    start += 1
    while start < len(data) and data[start] == 0:
        start += 1
    fields = data[start:].split(b"\0")
    selected = {}
    for field in fields[argc:]:
        if not field:
            break
        key, separator, value = field.partition(b"=")
        if separator and key in (b"CARGO_TARGET_DIR", b"CARGO_BUILD_TARGET_DIR", b"CARGO_HOME", b"HOME",
                                 b"RUNNER_NAME", b"GITHUB_JOB", b"MAC_PLACEMENT"):
            selected[os.fsdecode(key)] = os.fsdecode(value)
    return selected


def test_output_directories(pid, folder, command, members, processes):
    """Resolve target settings and observed compiler output directories."""
    try:
        arguments = shlex.split(command)
        environment = process_environment(pid)
        settings = cargo_folder_settings(arguments, folder, environment)
        if "target-dir" in settings:
            setting = settings["target-dir"]
            target = setting.base / setting.value
        else:
            try:
                target = cargo_workspace_manifest(arguments, folder).parent / "target"
            except (OSError, ValueError, TypeError):
                target = Path(folder) / "target"
        targets = {target.resolve()}
    except (OSError, ValueError, TypeError):
        targets = set()
    outputs = set()
    for member in members:
        try:
            words = shlex.split(processes[member]["command"])
            directory = cargo_path_argument(words, "--out-dir") if "--crate-name" in words else ""
            if directory:
                outputs.add((Path(folder) / directory).resolve())
        except (OSError, ValueError):
            continue
    return targets, outputs


def running_test_pid(pid, folder, command, members, processes):
    targets, outputs = test_output_directories(pid, folder, command, members, processes)
    for member in members:
        try:
            executable = Path(processes[member]["executable"]).resolve()
        except (OSError, RuntimeError):
            continue
        if (executable.parent.name == "deps" and
                not executable.name.startswith(("build-script", "build_script_")) and
                (executable.parent in outputs or any(executable.is_relative_to(target) for target in targets))):
            return member
    return 0


def build_origin(pid, processes):
    """Identify a build with a GitHub Actions runner ancestor."""
    cursor = pid
    seen = set()
    while cursor in processes and cursor not in seen:
        seen.add(cursor)
        try:
            words = shlex.split(processes[cursor]["command"])
        except ValueError:
            return "unknown"
        if words and Path(words[0]).name in ("Runner.Worker", "Runner.Listener"):
            return "ci"
        cursor = processes[cursor]["parent"]
    return "non-ci"


def read_gate_records():
    records = {}
    for path in (Path.home() / ".local/state/mac-gate/records").glob("[0-9]*"):
        try:
            pid, stamp, label = path.read_text().rstrip("\n").split("\t")
            born = int(time.mktime(time.strptime(stamp, "%a %b %d %H:%M:%S %Y")))
            records[int(pid)] = (born, label)
        except (OSError, ValueError):
            continue
    return records


PLACEMENT_FORM = re.compile(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+")


def gate_label(pid, processes, records, ci_uid):
    """Use the nearest gate record whose start time matches, or the nearest MAC_PLACEMENT."""
    if processes[pid].get("uid") == ci_uid:
        return "hana-ci"
    seen = set()
    while pid in processes and pid not in seen:
        seen.add(pid)
        process = processes[pid]
        record = records.get(pid)
        if record and record[0] == process["started_at"]:
            return record[1]
        # offload.py and mac_queue_run.py set MAC_PLACEMENT; sccache passes it on to the compiler.
        placement = process_environment(pid).get("MAC_PLACEMENT", "")
        if PLACEMENT_FORM.fullmatch(placement):
            return "placement " + placement
        pid = process["parent"]
    return "unplaced"


def ci_user_id():
    import pwd
    try:
        return pwd.getpwnam("hana-ci").pw_uid
    except KeyError:
        return -1


def unplaced_runs(processes, records, captures):
    """Name each cargo or rustc root with no placement; a compiler under sccache or a build is not a root."""
    candidates = {pid for pid, process in processes.items()
                  if Path(process["executable"]).name in ("cargo", "rustc", "cargo-nextest")}
    roots = candidates | set(captures)
    runs = []
    ci_uid = ci_user_id()
    for pid in candidates:
        cursor, seen = processes[pid]["parent"], {pid}
        while (cursor in processes and cursor not in seen and cursor not in roots
               and Path(processes[cursor]["executable"]).name != "sccache"):
            seen.add(cursor)
            cursor = processes[cursor]["parent"]
        if cursor in processes and cursor not in seen:
            continue
        if gate_label(pid, processes, records, ci_uid) == "unplaced":
            runs.append(dict(pid=pid, started_at=processes[pid]["started_at"], command=processes[pid]["command"]))
    return sorted(runs, key=lambda run: (run["started_at"], run["pid"]))


def read_builds(processes=None, records=None, captures=None):
    processes = read_processes() if processes is None else processes
    captures = read_captures(processes) if captures is None else captures
    records = read_gate_records() if records is None else records
    ci_uid = ci_user_id()
    candidates = set(captures)
    for pid, process in processes.items():
        if Path(process["executable"]).name in ("cargo", "cargo-nextest"):
            candidates.add(pid)
    roots = []
    for pid in candidates:
        cursor = processes[pid]["parent"]
        seen = {pid}
        while cursor in processes and cursor not in seen and cursor not in candidates:
            seen.add(cursor)
            cursor = processes[cursor]["parent"]
        if cursor not in candidates:
            roots.append(pid)
    cwd = {}
    if roots:
        try:
            current = 0
            for line in output(["lsof", "-a", "-p", ",".join(map(str, roots)), "-d", "cwd", "-Fpn"]).splitlines():
                if line.startswith("p"):
                    current = int(line[1:])
                elif line.startswith("n"):
                    cwd[current] = line[1:]
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    builds = []
    for pid in roots:
        members = []
        for child in processes:
            cursor = child
            seen = set()
            while cursor in processes and cursor not in seen and cursor != pid:
                seen.add(cursor)
                cursor = processes[cursor]["parent"]
            if cursor == pid:
                members.append(child)
        capture_ids = [member for member in members if member in captures and captures[member]["progress_tail"]]
        capture = captures.get(pid, {})
        folder = capture.get("folder", cwd.get(pid, ""))
        if not folder:
            continue
        try:
            record = project_record(folder)
        except Exception:
            record = dict(recorded_unit="", session="", repository="")
        command = capture.get("command", processes[pid]["command"])
        try:
            test_pid = running_test_pid(pid, folder, command, members, processes)
        except Exception:
            test_pid = -1
        try:
            repository = record["repository"] or repository_name(folder)
        except Exception:
            repository = ""
        try:
            origin = build_origin(pid, processes)
        except Exception:
            origin = "unknown"
        ci_environment = process_environment(pid) if origin == "ci" else {}
        try:
            folder = str(Path(folder).resolve())
        except OSError:
            pass
        tail = capture.get("progress_tail", "")
        if not tail and capture_ids:
            tail = captures[min(capture_ids, key=lambda value: processes[value]["started_at"])]["progress_tail"]
        builds.append(dict(pid=pid, folder=folder, started_at=processes[pid]["started_at"],
                           command=command, test_pid=test_pid,
                           resident_bytes=sum(processes[member]["resident_bytes"] for member in members),
                           progress_tail=tail, repository=repository,
                           recorded_unit=record["recorded_unit"], session=record["session"],
                           origin=origin, ci_runner=ci_environment.get("RUNNER_NAME", ""),
                           ci_job=ci_environment.get("GITHUB_JOB", ""),
                           gate_label=gate_label(pid, processes, records, ci_uid)))
    return sorted(builds, key=lambda build: (build["started_at"], build["pid"]))


def read_reading():
    processes = read_processes()
    records = read_gate_records()
    captures = read_captures(processes)
    return dict(home=str(Path.home()), builds=read_builds(processes, records, captures),
                unplaced_runs=unplaced_runs(processes, records, captures))


print(json.dumps(read_reading()))
print("rc=0")
''' + "MAC_READING\n"


def refresh_mac_builds() -> None:
    """Refresh the shared reading from the feed loop at its configured interval."""
    now = time.time()
    previous = read_mac_builds()
    try:
        settings = conf.read("mac_test")
        result = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o",
            f"ConnectTimeout={settings.number('connect_timeout_s'):g}", settings.text("host"), mac_build_script()],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, check=False, timeout=10)
        if result.returncode or result.stderr or not result.stdout.endswith("rc=0\n"):
            raise ValueError(" ".join(result.stderr.split()) or "Mac activity check failed")
        data = cast(dict[str, object], json.loads(result.stdout.removesuffix("rc=0\n")))
        records = cast(list[dict[str, object]], data["builds"])
        unplaced = tuple(unplaced_run_from_record(row) for row in
            cast(list[dict[str, object]], data.get("unplaced_runs", [])))
        builds: list[MacBuild] = []
        for row in records:
            counter = progress_profile.parse_progress(str(row.pop("progress_tail")))
            row["counter"] = ({"phase": counter.phase, "done": counter.done, "total": counter.total}
                              if isinstance(counter, progress_profile.ProgressCounter) else {})
            builds.append(mac_build_from_record(row))
        reading = MacBuildReading(now, tuple(sorted(builds, key=lambda build: build.started_at)),
                                  unplaced_runs=unplaced, home=str(data.get("home", "")))
    except (OSError, ValueError, UnicodeError, subprocess.TimeoutExpired) as error:
        logging.getLogger(__name__).warning("Mac reading unavailable: %s", error)
        reading = MacBuildReading(previous.read_at, previous.builds, "Mac reading unavailable",
                                  previous.unplaced_runs, previous.home)
    write_text_atomic(mac_reading_path(), json.dumps({"read_at": reading.read_at, "error": reading.error,
        "builds": [mac_build_record(build) for build in reading.builds],
        "unplaced_runs": [unplaced_run_record(run) for run in reading.unplaced_runs],
        "home": reading.home}), new_file_mode=0o644)

def step_counter(step: admission_records.RunningStep) -> progress_profile.ProgressCounter | progress_profile.NoCounter:
    """Read the counter from the local memory reading."""
    if isinstance(step.measured, admission_records.ProgressAdjustedMemory):
        return step.measured.counter
    return progress_profile.NoCounter("the step has no readable counter")


@dataclass(frozen=True)
class UnknownCpuCount:
    pass


@dataclass(frozen=True)
class QueueModel:
    reading: admission_records.QueueRead
    labels: Mapping[str, UnitLabels]
    names: StepNames = dataclass_field(default_factory=read_step_names)

    @cached_property
    def cloud_labels(self) -> Mapping[str, UnitLabels]:
        missing = {row.identity.session_id for row in cloud_rows(self)
                   if row.identity.session_id and row.identity.session_id not in self.labels}
        return {**unit_labels.unit_labels_for(missing, os.environ), **self.labels} if missing else self.labels


@dataclass(frozen=True)
class CloudPushInterval:
    seconds: float


@dataclass(frozen=True)
class CloudPushIntervalUnavailable:
    reason: str


def cloud_push_interval() -> CloudPushInterval | CloudPushIntervalUnavailable:
    try:
        return CloudPushInterval(conf.read("cloud_gate").number("log_push_s"))
    except conf.Unusable as error:
        return CloudPushIntervalUnavailable(str(error))


def cloud_rows(model: QueueModel) -> tuple[wire.CloudRow, ...]:
    return model.reading.cloud if isinstance(model.reading, wire.CloudQueueRead) else ()


@dataclass(frozen=True)
class CloudUpdateCurrent:
    pass


@dataclass(frozen=True)
class CloudUpdateLate:
    seconds: float


@dataclass(frozen=True)
class CloudWork:
    state: str
    modules: str
    vcpus: int | UnknownCpuCount
    failed_modules: int
    update: CloudUpdateCurrent | CloudUpdateLate


def cloud_work(row: wire.CloudRow, now: float, stale_pushes: int,
               interval: CloudPushInterval | CloudPushIntervalUnavailable) -> CloudWork:
    progress = row.progress
    state = (row.wait.text if isinstance(row.wait, wire.CloudSlotWait) else progress.state
             if isinstance(progress, cloud_progress.CloudProgress) else "progress " + progress.reason)
    modules = (f"{row.counter.done} of {row.counter.total} modules"
               if isinstance(row.counter, progress_profile.ProgressCounter) else "modules unknown")
    update: CloudUpdateCurrent | CloudUpdateLate = CloudUpdateCurrent()
    if (isinstance(progress, cloud_progress.CloudProgress) and isinstance(interval, CloudPushInterval)
            and now - progress.updated_at >= stale_pushes * interval.seconds):
        update = CloudUpdateLate(now - progress.updated_at)
    return CloudWork(state, modules,
                     progress.size_vcpus if isinstance(progress, cloud_progress.CloudProgress) else UnknownCpuCount(),
                     progress.failed_modules if isinstance(progress, cloud_progress.CloudProgress) else 0, update)


def cloud_update_age(seconds: float) -> str:
    for divisor, unit in ((86400, "d"), (3600, "h"), (60, "min")):
        if seconds >= divisor:
            return f"{int(seconds // divisor)} {unit}"
    return f"{int(seconds)} s"


def cloud_detail(row: wire.CloudRow) -> str:
    progress = row.progress
    if not isinstance(progress, cloud_progress.CloudProgress):
        return ""
    zone = progress.zone.removeprefix(progress.region) if progress.region else progress.zone
    location = ", ".join(filter(None, (progress.region, f"zone {zone}" if zone else "")))
    return " · ".join(filter(None, (progress.stage, location)))


def cloud_status(row: wire.CloudRow, work: CloudWork) -> Line:
    if isinstance(row.wait, wire.CloudSlotWait):
        return (Span(" · ".join(filter(None, (work.state, cloud_detail(row))))),)
    text = " · ".join(filter(None, (work.state, cloud_detail(row), work.modules, cloud_failed_modules(work))))
    return (Span(text),)


def cloud_failed_modules(work: CloudWork) -> str:
    if work.failed_modules <= 0:
        return ""
    noun = "module" if work.failed_modules == 1 else "modules"
    return f"{work.failed_modules} failed {noun}"


def cloud_page_status(row: wire.CloudRow, work: CloudWork) -> Line:
    if isinstance(row.wait, wire.CloudSlotWait):
        return cloud_status(row, work)
    parts = (work.state if work.state != "running" else "",
             cloud_detail(row),
             work.modules if isinstance(row.counter, progress_profile.ProgressCounter) else "",
             cloud_failed_modules(work))
    return (Span(" · ".join(filter(None, parts))),)


def cloud_step_labels(row: wire.CloudRow, model: QueueModel) -> UnitLabels:
    identity = row.identity
    label = model.cloud_labels.get(identity.session_id, UnitLabels(identity.name, unit_labels.OWNER_NOT_RECORDED))
    return replace(label, unit=gate.queue_unit(identity.unit, identity.session_id, model.cloud_labels)
                   or unit_labels.OWNER_NOT_RECORDED)


def cloud_step_name(row: wire.CloudRow, model: QueueModel) -> str:
    """Name page steps from their recorded owner's labels."""
    label = model.cloud_labels.get(row.identity.session_id)
    if label is None:
        return model.names.shown(row.identity.step)
    unit = label.unit if label.unit not in ("", "—", unit_labels.OWNER_NOT_RECORDED) else ""
    return model.names.for_owner(row.identity.step, unit, label.role)


@dataclass(frozen=True)
class WaitingBuild:
    position: int
    step: admission_records.WaitingStep


def waiting_builds(model: QueueModel) -> list[WaitingBuild | wire.CloudRow | wire.RepositorySlotRow]:
    """Insert cloud waits by arrival time without changing the local queue order."""
    rows: list[WaitingBuild | wire.CloudRow | wire.RepositorySlotRow] = [WaitingBuild(index, step)
        for index, step in enumerate(model.reading.view.waiting, 1)]
    slot_rows = model.reading.repository_slots if isinstance(model.reading, wire.CloudQueueRead) else ()
    for cloud in sorted([*(row for row in cloud_rows(model) if isinstance(row.wait, wire.CloudSlotWait)), *slot_rows],
                        key=lambda row: row.since):
        position = next((index for index, row in enumerate(rows)
                         if (row.step.since if isinstance(row, WaitingBuild) else row.since) > cloud.since), len(rows))
        rows.insert(position, cloud)
    return rows


def leftover_age(minutes: int) -> str:
    if minutes < 120:
        return f"{minutes} min"
    if minutes < 48 * 60:
        return f"{minutes // 60} h"
    return f"{minutes // (24 * 60)} d"


def leftover_processes(steps: Sequence[admission_records.RunningStep]) -> list[tuple[admission_records.RunningStep, admission_records.UnownedSliceMemory, ScopeProcesses]]:
    """Return the process scopes whose builds have ended."""
    ended: list[tuple[admission_records.RunningStep, admission_records.UnownedSliceMemory, ScopeProcesses]] = []
    for step in steps:
        if not isinstance(step.measured, admission_records.UnownedSliceMemory):
            continue
        activity = step.measured.activity
        if not isinstance(step.measured.job_state, NotServiceSlice):
            continue
        if isinstance(activity, ScopeProcesses) and not isinstance(activity.run, LiveBuildRun):
            ended.append((step, step.measured, activity))
    return ended
