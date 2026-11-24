#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""Read live configuration and recover missing or invalid values from Git."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

from collections.abc import Callable, Generator, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from enum import Enum
import math
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
from threading import Lock
from typing import TYPE_CHECKING, TypeVar, override

from scripts.lib import conf_reads


if TYPE_CHECKING:
    from decimal import Decimal


_warning_sink: ContextVar[Callable[[str, bool], None] | None] = ContextVar("configuration_warning_sink", default=None)


def _print_warning(message: str) -> None:
    print(message, file=sys.stderr)


def _warning(message: str, *, report: bool = True) -> None:
    sink = _warning_sink.get()
    if sink is not None:
        sink(message, report)
    elif report:
        _print_warning(message)


@contextmanager
def diagnostics(*, report: bool = True) -> Generator[list[str]]:
    """Collect reader warnings for a reply, with optional local output."""
    messages: list[str] = []

    def collect(message: str, new_warning: bool) -> None:
        messages.append(message)
        if report and new_warning:
            _print_warning(message)

    token = _warning_sink.set(collect)
    try:
        yield messages
    finally:
        _warning_sink.reset(token)


@dataclass(frozen=True)
class ConfigurationStamp:
    """Identify the file and its modification when the reader opens it."""

    device: int
    inode: int
    modified_ns: int
    changed_ns: int

    @classmethod
    def from_stat(cls, info: os.stat_result) -> ConfigurationStamp:
        return cls(info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns)


class UnstampedConfiguration(Enum):
    UNSTAMPED = "no file stamp"


def file_stamp(source: Path) -> ConfigurationStamp | UnstampedConfiguration:
    """Stamp the file without a read, so a cache keyed by the stamp sees each edit."""
    try:
        return ConfigurationStamp.from_stat(source.stat())
    except OSError:
        return UnstampedConfiguration.UNSTAMPED


_report_changes: ContextVar[bool] = ContextVar("report_configuration_changes", default=False)
_reported_bad_edits: set[tuple[Path, str, str, ConfigurationStamp | UnstampedConfiguration]] = set()
_bad_edits_lock = Lock()


@contextmanager
def report_key_changes() -> Generator[None]:
    """Report each change to an invalid live key once."""
    token = _report_changes.set(True)
    try:
        yield
    finally:
        _report_changes.reset(token)


def _key_warning(source: Path, key: str, reason: str, bad: str,
                 stamp: ConfigurationStamp | UnstampedConfiguration) -> None:
    report = True
    if _report_changes.get():
        identity = (source, key, bad, stamp)
        with _bad_edits_lock:
            report = identity not in _reported_bad_edits
            _reported_bad_edits.add(identity)
    _warning(f"warning: {reason}", report=report)


class Unusable(ValueError):
    """The named configuration key has no usable value."""


@dataclass(frozen=True)
class Setting:
    """The setting holds its stored value, limit, and source line."""
    value: str
    limit: str
    line: int


Result = TypeVar("Result")


@dataclass(frozen=True)
class UsableLiveValue[Value]:
    """The live configuration supplies this usable value."""
    value: Value


@dataclass(frozen=True)
class RecoveredValue[Value]:
    """The recovered configuration value includes the reason for recovery."""
    value: Value
    reason: str


@dataclass(frozen=True)
class UnavailableValue:
    """The reason explains why no usable configuration value is available."""
    reason: str

    def unrecovered(self) -> str:
        """Give the reason after the committed history gave no usable value."""
        return f"{self.reason}; no usable committed value"


@dataclass(frozen=True)
class _MissingValue(UnavailableValue):
    """The live file or key is missing and has no usable value."""

    @override
    def unrecovered(self) -> str:
        """Give the repair instruction for the missing key or file."""
        return self.reason


class _MissingKey(ValueError):
    """The file has no effective entry for the requested key."""


def _missing_key(source: Path, key: str) -> str:
    return f"{source}: {key} is missing; add it to {source}"


def _missing_file(source: Path) -> str:
    return f"{source}: the file does not exist; add it with its keys"


@dataclass(frozen=True)
class _UnreadableFile:
    """The file read failure holds its reason and whether the file is missing."""
    reason: str
    missing: bool


class UseConfiguredValue(Enum):
    """Read current configuration when the call starts."""

    CURRENT = "current configuration"


class HostOverride(Enum):
    """Tell whether a `<key>.<host>` key for the reading host replaces the key.

    IGNORED fits a key whose last part names something other than the reading host, such as a watched machine.
    """

    APPLIES = "the reading host key replaces the key"
    IGNORED = "the key is read as written"


@dataclass(frozen=True)
class ConfigRead:
    """Keep one live file read for the settings used in one pass."""

    source: Path
    settings: dict[str, Setting] | _UnreadableFile
    stamp: ConfigurationStamp | UnstampedConfiguration = UnstampedConfiguration.UNSTAMPED

    @property
    def file_missing(self) -> bool:
        """The file was missing when the reader opened it."""
        return isinstance(self.settings, _UnreadableFile) and self.settings.missing

    def _resolve(self, key: str, convert: Callable[[str], Result], *, warn: bool,
                 default: Result | UseConfiguredValue = UseConfiguredValue.CURRENT, allow_blank: bool = False,
                 recover_invalid: bool = True, use_file_limits: bool = True,
                 host_override: HostOverride = HostOverride.APPLIES
                 ) -> UsableLiveValue[Result] | RecoveredValue[Result] | UnavailableValue:
        bad = "<unreadable>"

        def read_key(settings: dict[str, Setting], *, use_default: bool = True) -> tuple[Result, str | None]:
            nonlocal bad
            try:
                setting = _setting(settings, key, host_override)
            except _MissingKey:
                if not use_default or isinstance(default, UseConfiguredValue):
                    raise
                return default, None
            bad = setting.value
            if not use_file_limits:
                return convert(setting.value), setting.value
            if allow_blank and not setting.value and not setting.limit.startswith(("# whole:", "# range:", "# one of:")):
                return convert(""), setting.value
            return _converted(setting, convert), setting.value

        missing: _MissingValue | None = None
        try:
            if isinstance(self.settings, _UnreadableFile):
                if self.settings.missing:
                    raise _MissingKey(self.settings.reason)
                raise ValueError(self.settings.reason)
            value, _ = read_key(self.settings)
            return UsableLiveValue(value)
        except (OSError, UnicodeError, ValueError, ArithmeticError) as error:
            if isinstance(error, _MissingKey):
                repair = _missing_key(self.source, key)
                if self.file_missing:
                    repair += "; the file does not exist"
                missing = _MissingValue(repair)
                bad = "<missing>"
            reason = f"{self.source}: {key}={bad!r}: {error}"
        if not recover_invalid:
            return missing if missing is not None else UnavailableValue(reason)
        if warn and missing is None:
            _key_warning(self.source, key, reason, bad, self.stamp)
        current_limit = ""
        if use_file_limits and isinstance(self.settings, dict):
            current = self.settings.get(self.effective_key(key) if host_override is HostOverride.APPLIES else key)
            if current is not None and current.limit.startswith(("# whole:", "# range:", "# one of:")):
                current_limit = current.limit
        fallback: RecoveredValue[Result] | None = None
        for committed in _committed_files(self.source):
            try:
                value, raw_value = read_key(_parse(committed), use_default=missing is None)
            except (ValueError, ArithmeticError):
                continue
            if not current_limit:
                if warn and missing is not None:
                    _key_warning(self.source, key, reason, "<missing>", self.stamp)
                return RecoveredValue(value, reason)
            if fallback is None:
                fallback = RecoveredValue(value, reason)
            if raw_value is None:
                continue
            try:
                _limits(current_limit, raw_value)
            except (ValueError, ArithmeticError):
                continue
            return RecoveredValue(value, reason)
        if missing is not None:
            return missing
        return fallback if fallback is not None else UnavailableValue(reason)

    def origin(self, key: str, convert: Callable[[str], Result], *, use_file_limits: bool = True
               ) -> UsableLiveValue[Result] | RecoveredValue[Result] | UnavailableValue:
        """Read a live or recovered value. The converter can supply all validation."""
        return self._resolve(key, convert, warn=False, use_file_limits=use_file_limits)

    def value(self, key: str, convert: Callable[[str], Result], *,
              default: Result | UseConfiguredValue = UseConfiguredValue.CURRENT, allow_blank: bool = False,
              recover_invalid: bool = True, host_override: HostOverride = HostOverride.APPLIES) -> Result:
        """Read one key with an optional default. A caller can require a live value."""
        result = self._resolve(key, convert, warn=True, default=default, allow_blank=allow_blank,
                               recover_invalid=recover_invalid, host_override=host_override)
        if isinstance(result, UnavailableValue):
            raise Unusable(result.unrecovered() if recover_invalid else result.reason)
        return result.value

    def text(self, key: str, *, recover_invalid: bool = True) -> str:
        """Read text and allow defaults for missing values."""
        return self.value(key, str, recover_invalid=recover_invalid)

    def effective_key(self, key: str) -> str:
        """Return the host override key when the live file has one."""
        if isinstance(self.settings, dict):
            return effective_key(self.settings, key, host_name=_local_host_name())
        return key

    def number(self, key: str) -> float:
        return self.value(key, _number)

    def whole(self, key: str) -> int:
        return self.value(key, _whole)

    def words(self, key: str) -> tuple[str, ...]:
        return self.value(key, lambda value: tuple(value.split()))

    def values(self, *keys: str, host_only_prefix: str = "") -> dict[str, str]:
        """Return all values when no keys are given. Add base keys for host overrides that match the prefix."""
        settings = self.settings
        if isinstance(settings, _UnreadableFile):
            settings = _recover_file(self.source, settings)
        selected = dict.fromkeys(keys or settings)
        if host_only_prefix:
            suffix = f".{_local_host_name()}"
            for key in settings:
                if key.startswith(host_only_prefix) and key.endswith(suffix):
                    selected[key.removesuffix(suffix)] = None
        result: dict[str, str] = {}
        for key in selected:
            try:
                setting = _setting(settings, key)
            except _MissingKey:
                continue
            if not setting.value and not setting.limit.startswith(("# whole:", "# range:", "# one of:")):
                result[key] = setting.value
                continue
            try:
                _limits(setting.limit, setting.value)
                result[key] = setting.value
            except ValueError:
                result[key] = self.text(key)
        return result


def _recover_file(source: Path, fault: _UnreadableFile) -> dict[str, Setting]:
    _warning(f"warning: {source}: keys: {fault.reason}")
    for contents in _committed_files(source):
        return _parse(contents)
    if fault.missing:
        raise Unusable(_missing_file(source))
    raise Unusable(f"{source}: keys: {fault.reason}; no usable committed file")


@dataclass(frozen=True)
class ConfigurationBytes:
    """The file bytes and the stamp of the open file."""

    data: bytes
    stamp: ConfigurationStamp


def _file_stamp(descriptor: int) -> ConfigurationStamp:
    return ConfigurationStamp.from_stat(os.fstat(descriptor))


def read_bytes(source: Path) -> ConfigurationBytes:
    """Read bytes for a pass that also needs a configuration digest."""
    with source.open("rb") as stream:
        # An edit during the read must invalidate the stamp.
        stamp = _file_stamp(stream.fileno())
        return ConfigurationBytes(stream.read(), stamp)


def read(file: str | Path, *, load: Callable[[Path], ConfigurationBytes] | None = None) -> ConfigRead:
    """Read and parse the live file once; resolve faults when a setting is used."""
    source = path(file)
    stamp: ConfigurationStamp | UnstampedConfiguration = UnstampedConfiguration.UNSTAMPED
    try:
        if load is not None:
            snapshot = load(source)
            stamp = snapshot.stamp
            contents = snapshot.data.decode("utf-8")
        elif _report_changes.get():
            with source.open(encoding="utf-8") as stream:
                try:
                    contents = stream.read()
                finally:
                    stamp = _file_stamp(stream.fileno())
        else:
            contents = source.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        return ConfigRead(source, _UnreadableFile(str(error), isinstance(error, FileNotFoundError)), stamp)
    return ConfigRead(source, _parse(contents), stamp)


def read_or_repository(stem: str) -> ConfigRead:
    """Use the test-owned file when the fixture has no copy."""
    if "CONF_DIR" in os.environ and not path(stem).exists():
        return read(test_config_path(stem))
    return read(stem)


_TEST_CONFIG = Path(__file__).resolve().parents[2] / "test_config"


def test_config_path(stem: str) -> Path:
    """Return the test-owned copy of a repository config file. /tune never changes it."""
    return _TEST_CONFIG / f"{stem}.conf"


def copy_for_test(source: Path, destination: Path) -> Path:
    """Copy a checkout config folder for a test and keep its links.

    Each top-level .conf file takes the values of its test-owned twin.
    A file or link that the destination already holds stays.
    """
    import shutil

    def present(folder: str, names: list[str]) -> set[str]:
        target = destination / Path(folder).relative_to(source)
        return {name for name in names if os.path.lexists(target / name) and not os.path.isdir(target / name)}

    def copy(path: str, target: str) -> str:
        file = Path(path)
        owned = test_config_path(file.stem) if file.parent == source and file.suffix == ".conf" else file
        return shutil.copyfile(owned if owned.is_file() else file, target)

    # A new destination holds nothing to keep, so it skips the per-entry check.
    keep = present if destination.exists() else None
    return Path(shutil.copytree(source, destination, symlinks=True, ignore=keep, copy_function=copy,
                                dirs_exist_ok=True))


def directory() -> Path:
    """Return the live configuration folder, or the test fixture override."""
    return Path(os.environ.get("CONF_DIR") or "~/.claude/config").expanduser()


def path(file: str | Path) -> Path:
    conf_reads.record(file if isinstance(file, str) else file.stem)
    if isinstance(file, Path):
        return file.expanduser()
    if not file or Path(file).name != file or file.endswith(".conf"):
        raise Unusable(f"{file}: file: expected a configuration stem")
    return directory() / f"{file}.conf"


def _parse(source: str) -> dict[str, Setting]:
    settings: dict[str, Setting] = {}
    previous = ""
    for index, raw in enumerate(source.splitlines()):
        line = re.split(r"(?:^|\s)#", raw, maxsplit=1)[0].strip()
        if line and not line.startswith("["):
            key, separator, value = line.partition("=")
            if separator:
                _ = settings.setdefault(key.strip(), Setting(value.strip(), previous, index))
        previous = raw.strip()
    return settings


def settings(source: str) -> dict[str, Setting]:
    """Return each stored key of a config text, with its first value and line."""
    return _parse(source)


def _local_host_name() -> str:
    return socket.gethostname().partition(".")[0]


def effective_key(settings: dict[str, Setting], key: str, *, host_name: str) -> str:
    """Select the override for the specified short host name when it exists."""
    candidate = f"{key}.{host_name}"
    return candidate if candidate in settings else key


def _setting(settings: dict[str, Setting], key: str, host_override: HostOverride = HostOverride.APPLIES) -> Setting:
    try:
        if host_override is HostOverride.IGNORED:
            return settings[key]
        return settings[effective_key(settings, key, host_name=_local_host_name())]
    except KeyError:
        raise _MissingKey("key is missing") from None


def _limits(limit: str, value: str) -> None:
    if not value.strip():
        raise ValueError("value is empty")
    if limit.startswith("# one of:"):
        choices = tuple(word.strip() for word in limit.removeprefix("# one of:").split(","))
        if value not in choices:
            raise ValueError(f"expected one of {', '.join(choices)}")
    elif limit.startswith(("# whole:", "# range:")):
        form = "# whole:" if limit.startswith("# whole:") else "# range:"
        bounds = limit.removeprefix(form).strip().split("..")
        if len(bounds) != 2:
            raise ValueError(f"invalid limit {limit}")
        convert = _whole if form == "# whole:" else decimal
        number = convert(value)
        low, high = (bound.strip() for bound in bounds)
        if (low and number < convert(low)) or (high and number > convert(high)):
            raise ValueError(f"outside range {low}..{high}")


def decimal(value: str) -> Decimal:
    from decimal import Decimal, InvalidOperation

    try:
        result = Decimal(value)
    except InvalidOperation as error:
        raise ValueError("expected a finite number") from error
    if not result.is_finite() or not math.isfinite(float(result)):
        raise ValueError("expected a finite number")
    return result


def on_off(value: str) -> bool:
    if value not in ("on", "off"):
        raise ValueError("expected on or off")
    return value == "on"


def _number(value: str) -> float:
    return float(decimal(value))


def _whole(value: str) -> int:
    if not re.fullmatch(r"[+-]?[0-9]+", value):
        raise ValueError("expected a whole number")
    return int(value)


def positive_whole(value: str) -> int:
    result = _whole(value)
    if result <= 0:
        raise ValueError("expected a positive whole number")
    return result


def positive_whole_seconds(value: str) -> float:
    return float(positive_whole(value))


_GIT_REPOSITORY_OVERRIDES = frozenset({
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
})


def _git(folder: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    environment = {name: value for name, value in os.environ.items() if name not in _GIT_REPOSITORY_OVERRIDES}
    try:
        return subprocess.run(["git", "-C", str(folder), *arguments], env=environment,
                              capture_output=True, text=True, check=False)
    except (OSError, UnicodeError) as error:
        return subprocess.CompletedProcess(["git", *arguments], 1, "", str(error))


def _committed_files(source: Path) -> Iterator[str]:
    folder = next((parent for parent in source.absolute().parents if parent.is_dir()), source.parent)
    root = _git(folder, "rev-parse", "--show-toplevel")
    if root.returncode:
        return
    checkout = Path(root.stdout.strip())
    try:
        relative = source.resolve().relative_to(checkout.resolve()).as_posix()
    except ValueError:
        return
    history = _git(checkout, "log", "--format=%H", "--", relative)
    for revision in history.stdout.splitlines() if history.returncode == 0 else ():
        committed = _git(checkout, "show", f"{revision}:{relative}")
        if committed.returncode == 0:
            yield committed.stdout


def _converted(setting: Setting, convert: Callable[[str], Result]) -> Result:
    whole = convert is _whole or convert is positive_whole or convert is positive_whole_seconds
    if whole and not setting.limit.startswith("# whole:"):
        raise ValueError("expected a # whole: limit")
    if convert is _number and not setting.limit.startswith("# range:"):
        raise ValueError("expected a # range: limit")
    result = convert(setting.value)
    _limits(setting.limit, setting.value)
    return result


def effective_line(file: str | Path, key: str) -> tuple[int, str]:
    """Return the effective source line index and its value without a write."""
    source = path(file)
    try:
        setting = _setting(_parse(source.read_text(encoding="utf-8")), key)
    except (OSError, UnicodeError, ValueError) as error:
        raise Unusable(f"{source}: {key}: {error}") from error
    return setting.line, setting.value


def known_host_names() -> frozenset[str]:
    """Return the local and configured short host names in lowercase."""
    source = path("machines")
    try:
        machines = _parse(source.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        machines = _recover_file(source, _UnreadableFile(str(error), missing=True))
    aliases = machines.get("aliases")
    hosts = {_local_host_name().casefold()}
    if aliases is not None:
        for alias in aliases.value.split():
            hosts.add(alias.casefold())
            names = machines.get(f"{alias}.hosts")
            if names is not None:
                hosts.update(name.partition(".")[0].casefold() for name in names.value.split())
    return frozenset(hosts)


def host_suffix(key: str, names: frozenset[str]) -> str:
    """Return the known host suffix or an empty string."""
    base, _, suffix = key.rpartition(".")
    return suffix if base and suffix.casefold() in names else ""


def full_model_color_key(file: str | Path, key: str) -> bool:
    """Check for a full model ID in a statusline color key."""
    is_statusline = file.name == "statusline.conf" if isinstance(file, Path) else file == "statusline"
    return is_statusline and re.fullmatch(
        r"model\.(?:gpt-[0-9][a-z0-9-]*(?:\.[a-z0-9][a-z0-9-]*)*|claude-[a-z][a-z0-9-]*-[0-9][a-z0-9-]*)", key,
    ) is not None


def host_override(file: str | Path, settings: dict[str, Setting], key: str) -> str | None:
    """Find an override. Require a known host suffix for full model color keys."""
    prefix = f"{key}."
    candidates = [candidate for candidate in settings if candidate.startswith(prefix)]
    if not candidates:
        return None
    if not full_model_color_key(file, key):
        return candidates[0]
    names = known_host_names()
    return next((candidate for candidate in candidates if host_suffix(candidate, names) == candidate[len(prefix):]), None)


def editable_line(file: str | Path, key: str) -> tuple[int, str]:
    """Return an exact stored key's line and value when no host override exists."""
    source = path(file)
    try:
        settings = _parse(source.read_text(encoding="utf-8"))
        if key not in settings:
            raise ValueError("exact stored key is missing")
        setting = settings[key]
        if (candidate := host_override(source, settings, key)) is not None:
            raise ValueError(
                f"{key} at line {setting.line + 1} has host override "
                + f"{candidate} at line {settings[candidate].line + 1}"
            )
    except (OSError, UnicodeError, ValueError) as error:
        raise Unusable(f"{source}: {key}: {error}") from error
    return setting.line, setting.value


def check(file: str | Path, key: str, value: str) -> None:
    source = path(file)
    try:
        setting = _setting(_parse(source.read_text(encoding="utf-8")), key)
        _limits(setting.limit, value)
    except (OSError, UnicodeError, ValueError) as error:
        raise Unusable(f"{source}: {key}={value!r}: {error}") from error


def main(arguments: list[str] | None = None) -> int:
    args = sys.argv[1:] if arguments is None else arguments
    if len(args) != 3 or args[0] != "get":
        print("usage: conf.py get FILE KEY", file=sys.stderr)
        return 2
    try:
        print(read(args[1]).text(args[2]))
    except Unusable as error:
        print(str(error), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
