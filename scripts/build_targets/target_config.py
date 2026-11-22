"""Read the configured targets without loading coordination state readers."""

from __future__ import annotations

from dataclasses import dataclass, field
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from enum import Enum, auto
from pathlib import Path
import os
import re
from typing import Literal
from scripts.build_targets.execution_transport import (
    ExecutionTransport, HanaControlTransport, HanaGpuNextestTransport, SshCommandTransport,
)


SCRIPTS = Path(__file__).resolve().parent.parent
from scripts.lib import conf


Platform = Literal["linux", "macos"]

# One wait for every machine that the queue sends a run to (build_targets.conf).
UNREACHABLE_WAIT_KEY = "unreachable_wait_seconds"
MAX_UNREACHABLE_WAIT_SECONDS = 86400


def _unreachable_wait(value: str) -> int:
    seconds = conf.positive_whole(value)
    if seconds > MAX_UNREACHABLE_WAIT_SECONDS:
        raise ValueError(f"expected at most {MAX_UNREACHABLE_WAIT_SECONDS} seconds")
    return seconds


def unreachable_wait_seconds(registry: conf.ConfigRead) -> int:
    """Read how long a machine may send nothing before its run ends as unreachable."""
    return registry.value(UNREACHABLE_WAIT_KEY, _unreachable_wait, recover_invalid=False)


@dataclass(frozen=True)
class LocalTarget:
    """The local target holds its name and platform."""
    name: str
    platform: Platform


@dataclass(frozen=True)
class SshTarget:
    """The SSH target holds its name, platform, and settings path."""
    name: str
    platform: Platform
    settings: Path


Target = LocalTarget | SshTarget


@dataclass(frozen=True)
class AnyEligibleTarget:
    """The step can use any target that supports its repository and package."""


@dataclass(frozen=True)
class LocalRequired:
    """The reason requires local execution."""
    reason: str


Routing = AnyEligibleTarget | LocalRequired
ANY_ELIGIBLE_TARGET = AnyEligibleTarget()
# verify.sh --local pins a step to natedev.
LOCAL_CALLER = LocalRequired("local caller")
# A step that writes the tree runs on natedev.
WRITES_TREE = LocalRequired("step writes the tree")


@dataclass(frozen=True)
class MacOnly:
    """verify.sh --mac-only: the step runs only on the Mac."""


@dataclass(frozen=True)
class PinnedTarget:
    """verify.sh --target or --mac: the step runs only on the named target."""

    target: str


# Where a step can run, as the caller asked. The placement request carries it to the queue's waiter record.
StepRoute = AnyEligibleTarget | LocalRequired | MacOnly | PinnedTarget
MAC_ONLY = MacOnly()


def step_route(*, local: bool, mac_only: bool, target: str, writes_tree: bool, local_reason: str = "") -> StepRoute:
    """Apply the placement client's order: tree writes and --local stay on natedev, then --mac-only, then a pin."""
    if local_reason:
        return LocalRequired(local_reason)
    if writes_tree:
        return WRITES_TREE
    if local:
        return LOCAL_CALLER
    if mac_only:
        return MAC_ONLY
    if target:
        return PinnedTarget(target)
    return ANY_ELIGIBLE_TARGET


@dataclass(frozen=True)
class EligibleTarget:
    """This target is eligible for execution."""
    target: Target


@dataclass(frozen=True)
class ExcludedTarget:
    """The target is excluded for this reason."""
    target: Target
    reason: str


ConfigError = conf.Unusable


class LiveConfiguration(Enum):
    CURRENT = auto()


@dataclass
class CommandConfiguration:
    """Configuration reads retained only during one short command."""

    configurations: dict[Path, conf.ConfigRead] = field(default_factory=dict)


COMMAND_CONFIGURATION: ContextVar[LiveConfiguration | CommandConfiguration] = ContextVar(
    "build_target_command_configuration", default=LiveConfiguration.CURRENT)


@contextmanager
def command_configuration() -> Generator[None]:
    """Share configuration reads until the command returns."""
    token = COMMAND_CONFIGURATION.set(CommandConfiguration())
    try:
        yield
    finally:
        COMMAND_CONFIGURATION.reset(token)


class MissingSlotCapacity(Enum):
    REQUIRE_CONFIGURED = auto()
    SINGLE_SLOT = auto()


def slot_capacity(values: Mapping[str, str] | conf.ConfigRead, repository: str, *,
                  missing: MissingSlotCapacity) -> int:
    """Read the repository capacity before the target default."""
    key = f"slots.{repository}"
    if isinstance(values, conf.ConfigRead):
        scoped = values.value(key, positive_slot_capacity, default=MissingSlotCapacity.REQUIRE_CONFIGURED)
        if isinstance(scoped, int):
            return scoped
        if missing is MissingSlotCapacity.SINGLE_SLOT:
            return values.value("slots", positive_slot_capacity, default=1)
        return values.value("slots", positive_slot_capacity)
    value = values.get(key, values.get("slots", ""))
    if key not in values and "slots" not in values and missing is MissingSlotCapacity.SINGLE_SLOT:
        return 1
    try:
        return positive_slot_capacity(value)
    except ValueError as error:
        raise ConfigError(f"{key}: {error}") from error


def positive_slot_capacity(value: str) -> int:
    if not value.isascii() or not value.isdecimal() or int(value) < 1:
        raise ValueError("expected a positive integer")
    return int(value)


def require_target_name(target: str) -> None:
    if target in (".", "..") or Path(target).name != target:
        raise ConfigError(f"invalid target name: {target}")


def ssh_target_state_directory(target: str) -> Path:
    """Return the state path without requiring target registration."""
    require_target_name(target)
    if target != "mac":
        configured_root = os.environ.get("BUILD_TARGETS_STATE_DIR")
        root = Path(configured_root).expanduser() if configured_root else Path.home() / ".local/state/build-targets"
        return root / target
    configured = os.environ.get("MAC_TEST_STATE_DIR", "~/.local/state/mac-test")
    return Path(configured).expanduser()


def uses_legacy_mac_layout(platform: Platform) -> bool:
    """Mac targets retain one mirror per source repository name."""
    return platform == "macos"


class RegistryRead(Enum):
    REQUIRE_SETTINGS_FILES = auto()
    PLACEMENT = auto()


def _target_names(value: str) -> tuple[str, ...]:
    names = tuple(name.strip() for name in value.split(","))
    if not all(names) or len(set(names)) != len(names):
        raise ConfigError("expected distinct target names")
    return names


def _target_kind(value: str) -> Literal["local", "ssh"]:
    if value not in ("local", "ssh"):
        raise ValueError(f"expected local or ssh, got {value!r}")
    return value


def _target_platform(value: str) -> Platform:
    if value not in ("linux", "macos"):
        raise ValueError(f"expected linux or macos, got {value!r}")
    return value


def _settings_path(value: str, registry: Path) -> Path:
    if not value:
        raise ValueError("expected a settings file")
    try:
        return (registry.parent / Path(value).expanduser()).resolve()
    except RuntimeError as error:
        raise ValueError(str(error)) from error


def _registry_value[Value](config: conf.ConfigRead, key: str, convert: Callable[[str], Value],
                           purpose: RegistryRead) -> Value:
    if purpose is RegistryRead.PLACEMENT:
        return config.value(key, convert)
    value = config.value(key, str, default="", allow_blank=True)
    try:
        return convert(value)
    except (OSError, ValueError) as error:
        raise ConfigError(f"{key}: {error} in {config.source}") from error


def _read_target(config: conf.ConfigRead, name: str, purpose: RegistryRead) -> Target:
    if purpose is RegistryRead.PLACEMENT:
        require_target_name(name)
    kind = _registry_value(config, f"kind.{name}", _target_kind, purpose)
    platform = _registry_value(config, f"platform.{name}", _target_platform, purpose)
    if kind == "local":
        return LocalTarget(name, platform)
    key = f"settings.{name}"
    settings = _registry_value(config, key, lambda value: _settings_path(value, config.source), purpose)
    if purpose is RegistryRead.REQUIRE_SETTINGS_FILES and not settings.is_file():
        raise ConfigError(f"{key}: settings file does not exist: {settings}")
    if name == "aws" or settings.name == "aws_target.conf":
        raise ConfigError("The permanent AWS builder was removed; use cloud gates")
    return SshTarget(name, platform, settings)


def pinned_target_names(configuration: conf.ConfigRead) -> tuple[str, ...]:
    """Read the names that a caller can select explicitly."""
    pinned = configuration.value("pinned_targets", str, default="", allow_blank=True)
    return tuple(name for item in pinned.split(",") if (name := item.strip()))


def read_targets(path: Path | None = None, *, selected: str = "", target_name: str = "",
                 purpose: RegistryRead = RegistryRead.REQUIRE_SETTINGS_FILES,
                 configuration: conf.ConfigRead | conf.UseConfiguredValue = conf.UseConfiguredValue.CURRENT
                 ) -> tuple[Target, ...]:
    if selected and target_name and selected != target_name:
        raise ConfigError("expected one selected target")
    selected = selected or target_name
    if isinstance(configuration, conf.UseConfiguredValue):
        registry = conf.path("build_targets") if path is None else path
        current = COMMAND_CONFIGURATION.get()
        if isinstance(current, CommandConfiguration):
            if registry not in current.configurations:
                current.configurations[registry] = conf.read(registry)
            configuration = current.configurations[registry]
        else:
            configuration = conf.read(registry)
    names = _registry_value(configuration, "targets", _target_names, purpose)
    if selected and selected not in names and selected in pinned_target_names(configuration):
        names = (selected,)
    targets: list[Target] = []
    for name in names:
        if selected and name != selected:
            continue
        try:
            targets.append(_read_target(configuration, name, purpose))
        except ConfigError:
            if selected or purpose is RegistryRead.REQUIRE_SETTINGS_FILES:
                raise
    return tuple(targets)


def configured_ssh_target(target: str) -> SshTarget:
    """Resolve one SSH target before access to its coordination state."""
    for configured in read_targets(selected=target, purpose=RegistryRead.PLACEMENT):
        if configured.name == target and isinstance(configured, SshTarget):
            require_target_name(target)
            if target == "hana-gpu" and not configured.settings.is_file():
                raise ConfigError(f"settings.{target}: settings file does not exist: {configured.settings}")
            return configured
    raise ConfigError(f"unknown SSH target: {target}")


def read_execution_transport(target: SshTarget, *,
                             configuration: conf.ConfigRead | conf.UseConfiguredValue = conf.UseConfiguredValue.CURRENT
                             ) -> ExecutionTransport:
    """Select the transport before reading connection settings."""
    def transport_kind(value: str) -> str:
        if value == "ssh" or (target.platform == "linux" and value == "hana-control"):
            return value
        if value == "hana-gpu" and target.platform == "linux" and target.name == "hana-gpu":
            return value
        raise ValueError("expected ssh, Linux hana-control, or the Linux hana-gpu target")

    def reporting_host(value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value):
            raise ValueError("expected a safe Hana reporting alias")
        return value

    settings = conf.read(target.settings) if isinstance(configuration, conf.UseConfiguredValue) else configuration
    transport = settings.value("execution_transport", transport_kind, default="ssh",
                               recover_invalid=target.name != "hana-gpu")
    if transport == "hana-gpu":
        return HanaGpuNextestTransport(target.settings, target.name)
    if transport == "ssh":
        return SshCommandTransport(target.settings)
    return HanaControlTransport(target.settings, settings.value("host", reporting_host))


def command_target_name(given: str | None) -> str:
    """Resolve a positional target for clear, drain, or release."""
    configuration = conf.read_or_repository("build_targets")
    selected = given if given is not None else configuration.text("default_target")
    configured = read_targets(selected="" if selected == "all" else selected, configuration=configuration)
    names = {target.name for target in configured}
    names.update(key.removeprefix("sync.hosts.") for key in configuration.values() if key.startswith("sync.hosts."))
    if selected != "all" and selected not in names:
        raise ConfigError(f"unknown target {selected}")
    return selected
