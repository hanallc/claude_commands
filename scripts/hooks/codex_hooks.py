#!/usr/bin/env python3
"""Install and trust Codex tool checks, context hooks, and compaction markers.

The tool checks match `apply_patch` or `Bash`. STE runs at SessionStart and
SubagentStart. Their `*` matchers cover every session source and subagent type.
Session sources include startup, resume, clear and compact; they are not tool
names. PreCompact records the start of manual and automatic compaction.
UserPromptSubmit and PostCompact clear the session's compaction marker.
Stop sends worker turn completion to the shared handler.
Listing and trust use the event, matcher, command and timeout together,
so two events can share one script without sharing a trust identity.
"""

from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import contextlib
import fcntl
import hashlib
import json
import math
import os
import re
import selectors
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Generator, Mapping, Sequence
from enum import Enum
from pathlib import Path
from typing import Literal, NamedTuple, NewType, NoReturn, TypedDict, cast


RETIRED_COMMAND = '"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/hooks/post-tool-use-mul-add.py"'


class PythonHookScript(NamedTuple):
    """A hook run through the shared Python launcher."""

    script: str
    arguments: tuple[str, ...] = ()

    def command(self) -> str:
        command = f'"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/hooks/{self.script}"'
        return command + (" " + shlex.join(self.arguments) if self.arguments else "")


class ShellHookFront(NamedTuple):
    """A hook front that starts Python only when its checks need it."""

    script: str

    def command(self) -> str:
        return f'"$HOME/.claude/scripts/hooks/{self.script}"'


class CodexHook:
    """A named handler for one event and matcher that must be installed and trusted."""

    __slots__: tuple[str, ...] = ("name", "command", "event", "_timeout", "matcher")

    def __init__(self, name: str, script: PythonHookScript | ShellHookFront, event: str = "PostToolUse", timeout: int = 10,
                 matcher: str = "apply_patch") -> None:
        self.name: str = name
        self.command: str = script.command()
        self.event: str = event
        self._timeout: int = timeout
        self.matcher: str = matcher

    @property
    def timeout(self) -> int:
        if self.name == "scratch-guard":
            from scripts.copies.copy_guard import hook_timeout
            return math.ceil(hook_timeout())
        if self.name == "showrunner-guard":
            from scripts.hooks.showrunner_guard import hook_timeout

            return math.ceil(hook_timeout())
        return self._timeout

    def handler(self) -> dict[str, object]:
        return {"type": "command", "command": self.command, "timeout": self.timeout}

    def listed_event(self) -> str:
        """The event name `hooks/list` reports: `PreToolUse` lists as `preToolUse`."""
        return self.event[:1].lower() + self.event[1:]


SHOWRUNNER_GUARD = CodexHook(
    "showrunner-guard", PythonHookScript("pre-tool-use-showrunner-guard-codex.py"),
    "PreToolUse", matcher="Bash|apply_patch",
)


HOOKS: tuple[CodexHook, ...] = (
    CodexHook("fn-length", PythonHookScript("post-tool-use-fn-length.py")),
    # cargo-berth's own deadline is ten seconds; the hook allows for a slow start under load.
    CodexHook("berth", PythonHookScript("pre-tool-use-berth.py"), "PreToolUse", 30),
    # Codex sends a shell command as tool `Bash`, as Claude Code does.
    CodexHook("scratch-guard", PythonHookScript("../copies/copy_guard.py"), "PreToolUse", matcher="Bash"),
    CodexHook("build-hold-guard", ShellHookFront("pre-tool-use-build-hold-guard.sh"), "PreToolUse", 10, "Bash"),
    CodexHook("ste", PythonHookScript("session-start-ste.py"), "SessionStart", 10, "*"),
    CodexHook("ste-subagent", PythonHookScript("session-start-ste.py"), "SubagentStart", 10, "*"),
    CodexHook("queue-start", PythonHookScript("codex-queue-drain.py"), "SessionStart", matcher="*"),
    CodexHook("queue-tools", PythonHookScript("codex-queue-drain.py"), matcher="*"),
    CodexHook("compaction-marker", PythonHookScript("pre-compact-marker.py", ("--family", "codex")),
              "PreCompact", matcher="*"),
    CodexHook("compaction-clear-turn", ShellHookFront("compaction-marker-clear.sh"),
              "UserPromptSubmit", matcher="*"),
    CodexHook("compaction-clear-end", PythonHookScript("compaction-marker-clear.py"),
              "PostCompact", matcher="*"),
    CodexHook("turn-end", PythonHookScript("../herdr/turn_end.py"), "Stop", matcher="*"),
)


class HookError(Exception):
    """A local file or app-server failure that prevents a trusted hook."""


class LocatedHook:
    """A named handler as listed by the app-server."""

    __slots__: tuple[str, ...] = ("hook", "key", "current_hash", "trust_status", "enabled")

    def __init__(self, hook: CodexHook, key: str, current_hash: str, trust_status: str, enabled: bool) -> None:
        self.hook: CodexHook = hook
        self.key: str = key
        self.current_hash: str = current_hash
        self.trust_status: str = trust_status
        self.enabled: bool = enabled


class MissingHook:
    """No matching user-layer handler was listed."""

    __slots__: tuple[str, ...] = ("hook",)

    def __init__(self, hook: CodexHook) -> None:
        self.hook: CodexHook = hook


class RemovedRegistryEntry(Enum):
    REMOVED = "removed"


RegistryEntryMoves = dict[str, str | RemovedRegistryEntry]


def _object(value: object, description: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise HookError(f"invalid {description}")
    return cast(dict[str, object], value)


def _home() -> str:
    return os.path.abspath(os.environ.get("HOME") or os.path.expanduser("~"))


def _codex_home() -> str:
    return os.path.abspath(os.environ.get("CODEX_HOME") or os.path.join(_home(), ".codex"))


def known_codex_homes() -> list[Path]:
    home = Path(_home())
    numbered = sorted(path for path in home.glob(".codex*")
                      if re.fullmatch(r"\.codex[0-9]+", path.name) and os.path.lexists(path / "hooks.json"))
    return list(dict.fromkeys((home / ".codex", *numbered, Path(_codex_home()))))


def _command_binary() -> str:
    configured = os.environ.get("CODEX_BIN")
    if configured:
        return os.path.abspath(configured)
    found = shutil.which("codex")
    return os.path.abspath(found) if found else os.path.join(_home(), ".local", "bin", "codex")


def _managed_handlers(group: object, hook: CodexHook) -> list[dict[str, object]]:
    if not isinstance(group, dict):
        return []
    fields = cast(dict[str, object], group)
    if fields.get("matcher") != hook.matcher or not isinstance(fields.get("hooks"), list):
        return []
    handlers = cast(list[object], fields["hooks"])
    return [cast(dict[str, object], handler) for handler in handlers if isinstance(handler, dict)
        and cast(dict[str, object], handler).get("type") == "command"
        and cast(dict[str, object], handler).get("command") == hook.command
    ]


def _is_retired_handler(handler: object) -> bool:
    if not isinstance(handler, dict):
        return False
    entry = cast(dict[str, object], handler)
    return entry.get("type") == "command" and entry.get("command") == RETIRED_COMMAND


class RetiredHandlerRemoval(NamedTuple):
    """The slot changes from removing retired handlers."""

    changed: bool
    moves: RegistryEntryMoves


def _hook_groups(
    hooks: dict[str, object],
) -> Generator[tuple[str, int, dict[str, object], list[object]]]:
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            continue
        for index, group in enumerate(cast(list[object], groups)):
            if isinstance(group, dict):
                fields = cast(dict[str, object], group)
                handlers = fields.get("hooks")
                if isinstance(handlers, list):
                    yield event, index, fields, cast(list[object], handlers)


def _normalize_handler_timeouts(hooks: dict[str, object]) -> bool:
    changed = False
    for *_, handlers in _hook_groups(hooks):
        for handler in handlers:
            if not isinstance(handler, dict):
                continue
            fields = cast(dict[str, object], handler)
            timeout = fields.get("timeout")
            if isinstance(timeout, float) and math.isfinite(timeout):
                fields["timeout"] = math.ceil(timeout)
                changed = True
    return changed


def _remove_retired_handlers(hooks: dict[str, object], path: str, *, report: bool = True) -> RetiredHandlerRemoval:
    changed = False
    moves: RegistryEntryMoves = {}
    needs_manual_removal = False
    for event, group_index, fields, handlers in _hook_groups(hooks):
        event_key = re.sub(r"(?<!^)(?=[A-Z])", "_", event).lower()
        kept: list[object] = []
        for index, handler in enumerate(handlers):
            if _is_retired_handler(handler):
                changed = True
                moves[f"{event_key}:{group_index}:{index}"] = RemovedRegistryEntry.REMOVED
                continue
            if index != len(kept):
                moves[f"{event_key}:{group_index}:{index}"] = f"{event_key}:{group_index}:{len(kept)}"
            kept.append(handler)
            if isinstance(handler, dict):
                entry = cast(dict[str, object], handler)
                command = entry.get("command")
                if entry.get("type") == "command" and isinstance(command, str) and "post-tool-use-mul-add.py" in command:
                    needs_manual_removal = True
        # Preserve group indices when a group becomes empty.
        fields["hooks"] = kept
    if report and needs_manual_removal:
        print(f"{path}: remove the remaining retired script handler by hand", file=sys.stderr)
    return RetiredHandlerRemoval(changed, moves)


def _read_regular_bytes(path: Path) -> bytes:
    if not stat.S_ISREG(path.stat().st_mode):
        raise HookError(f"{path}: expected a regular file")
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise HookError(f"{path}: expected a regular file")
        return source.read()


@contextlib.contextmanager
def _registry_lock(path: Path) -> Generator[None]:
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def _write_installed_registry(destination: Path, registry: str) -> None:
    from scripts.lib.atomic_file import sync_folder

    descriptor, temporary = tempfile.mkstemp(prefix=".hooks-", suffix=".json", dir=str(destination.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            _ = output.write(registry)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)
        sync_folder(destination.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _install_file(path: str, binary: str, destination: Path, marker: Path, *, pending: bool = False) -> None:
    try:
        original = _read_regular_bytes(destination)
    except FileNotFoundError:
        original = b""
        document: dict[str, object] = {"hooks": {}}
    else:
        try:
            parsed = cast(object, json.loads(original))
        except ValueError as error:
            raise HookError(f"{path}: {error}") from error
        document = _object(parsed, path)
    hooks = _object(document.get("hooks"), f"{path} hooks")
    home = str(Path(path).parent)
    homes = _recovery_homes(destination, {home: _config_path(home)})
    if _normalize_handler_timeouts(hooks):
        normalized: TimeoutNormalizationJournal = {
            "version": 3, "original_hash": hashlib.sha256(original).hexdigest(),
            "registry": json.dumps(document, indent=2) + "\n", "path": path,
            "homes": [home for group in homes.values() for home in group["homes"]],
            "configs": {home: config for config, group in homes.items() for home in group["homes"]},
        }
        _publish_registry_journal(marker, normalized)
        _finish_timeout_normalization(destination, marker, original, normalized, binary)
        return
    _complete_install(destination, marker, path, original, document, homes, binary, pending=pending)


def _complete_install(
    destination: Path, marker: Path, path: str, original: bytes, document: dict[str, object],
    homes: dict[str, RegistryTrustGroup], binary: str, *, pending: bool,
) -> None:
    hooks = _object(document.get("hooks"), f"{path} hooks")
    repairs = _remove_retired_handlers(hooks, path)
    changed = repairs.changed
    for hook in HOOKS:
        groups_value = hooks.setdefault(hook.event, [])
        if not isinstance(groups_value, list):
            raise HookError(f"{path}: invalid {hook.event} groups")
        groups = cast(list[object], groups_value)
        managed = [handler for group in groups for handler in _managed_handlers(group, hook)]
        if not any(handler.get("timeout") == hook.timeout for handler in managed):
            groups.append({"matcher": hook.matcher, "hooks": [hook.handler()]})
            changed = True
    if not changed and not pending:
        return
    registry = json.dumps(document, indent=2) + "\n" if changed else original.decode("utf-8")
    if not repairs.moves and not pending:
        _write_installed_registry(destination, registry)
        return
    journal = _prepare_registry_change(homes, original, registry, repairs.moves, binary, install=True)
    _publish_registry_journal(marker, journal)
    _finish_registry_change(destination, marker, original, journal, binary, install=True)


class AppServer:
    """One bounded JSON-RPC conversation over Codex's stdio transport."""

    __slots__: tuple[str, ...] = ("process", "deadline", "buffer", "next_id", "selector")

    def __init__(self, binary: str, codex_home: str) -> None:
        self.deadline: float = time.monotonic() + 30
        self.buffer: bytes = b""
        self.next_id: int = 0
        environment = {**os.environ, "CODEX_HOME": codex_home}
        self.process: subprocess.Popen[bytes] = subprocess.Popen(
            [binary, "app-server"], cwd=_home(), env=environment,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        if self.process.stdin is None or self.process.stdout is None:
            raise HookError("app-server has no stdio pipes")
        self.selector: selectors.BaseSelector = selectors.DefaultSelector()
        _ = self.selector.register(self.process.stdout, selectors.EVENT_READ)

    def close(self) -> None:
        self.selector.close()
        for pipe in (self.process.stdin, self.process.stdout):
            if pipe is not None:
                # A request the server never read is still buffered, and closing fails on it.
                with contextlib.suppress(OSError):
                    pipe.close()
        if self.process.poll() is None:
            self.process.kill()
        try:
            _ = self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.process.kill()
            _ = self.process.wait()

    def _line(self) -> bytes:
        if self.process.stdout is None:
            raise HookError("app-server has no stdout")
        while True:
            line, separator, remainder = self.buffer.partition(b"\n")
            if separator:
                self.buffer = remainder
                return line
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise HookError("app-server timed out")
            if not self.selector.select(remaining):
                raise HookError("app-server timed out")
            chunk = os.read(self.process.stdout.fileno(), 65536)
            if not chunk:
                raise HookError("app-server closed before replying")
            self.buffer += chunk

    def call(self, method: str, params: dict[str, object]) -> dict[str, object]:
        if self.process.stdin is None:
            raise HookError("app-server has no stdin")
        self.next_id += 1
        request_id = self.next_id
        request = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        try:
            _ = self.process.stdin.write((json.dumps(request) + "\n").encode())
            self.process.stdin.flush()
        except BrokenPipeError as error:
            # The server ended before it read the request. Whether the write or the read meets
            # that first is a matter of timing, so both report it the same way.
            raise HookError("app-server closed before replying") from error
        except OSError as error:
            raise HookError(f"app-server write failed: {error}") from error
        while True:
            try:
                response = _object(cast(object, json.loads(self._line())), "app-server response")
            except (ValueError, UnicodeError) as error:
                raise HookError(f"app-server sent invalid JSON: {error}") from error
            if response.get("id") != request_id:
                continue
            if "error" in response:
                error = _object(response["error"], "RPC error")
                raise HookError(f"{method}: {error.get('message', error)}")
            return _object(response.get("result"), f"{method} result")


def _initialize(server: AppServer) -> None:
    _ = server.call(
        "initialize",
        {"clientInfo": {"name": "codex_hooks", "version": "1.0"},
         "capabilities": {"experimentalApi": True}},
    )


def _listed_hook_entries(server: AppServer, path: str, *aliases: str) -> list[dict[str, object]]:
    result = server.call("hooks/list", {"cwds": [_home()]})
    data = result.get("data")
    if not isinstance(data, list):
        raise HookError("invalid hooks/list data")
    entries: list[dict[str, object]] = []
    for workspace in cast(list[object], data):
        fields = _object(workspace, "hooks/list workspace")
        for kind in ("warnings", "errors"):
            messages = fields.get(kind)
            if isinstance(messages, list):
                for message in cast(list[object], messages):
                    print(f"codex hooks: hooks/list {kind}: {message}", file=sys.stderr)
        hooks = fields.get("hooks")
        if not isinstance(hooks, list):
            raise HookError("invalid hooks/list hooks")
        for candidate in cast(list[object], hooks):
            entry = _object(candidate, "hooks/list entry")
            if entry.get("sourcePath") in (path, *aliases):
                entries.append(entry)
    return entries


def _trust_handlers(server: AppServer, values: dict[str, dict[str, str]]) -> None:
    _ = server.call(
        "config/batchWrite",
        {"edits": [{"keyPath": "hooks.state", "value": values, "mergeStrategy": "upsert"}],
         "reloadUserConfig": True},
    )


def _clear_journal(marker: Path) -> None:
    from scripts.lib.atomic_file import sync_folder

    marker.unlink()
    sync_folder(marker.parent)


def _listed_fields(hook: CodexHook) -> dict[str, object]:
    return {"command": hook.command, "eventName": hook.listed_event(),
            "matcher": None if hook.event in ("UserPromptSubmit", "Stop") else hook.matcher,
            "handlerType": "command", "timeoutSec": hook.timeout}


def _active_hook(entry: LocatedHook | MissingHook) -> bool:
    return isinstance(entry, LocatedHook) and entry.trust_status == "trusted" and entry.enabled


def _select_hooks(
    entries: list[dict[str, object]], hooks: Sequence[CodexHook] = HOOKS,
) -> dict[CodexHook, LocatedHook | MissingHook]:
    selected: dict[CodexHook, LocatedHook | MissingHook] = {hook: MissingHook(hook) for hook in hooks}
    definitions = {hook: _listed_fields(hook) for hook in hooks}
    for entry in entries:
        hook = next((hook for hook, fields in definitions.items()
                     if all(entry.get(field) == value for field, value in fields.items())), None)
        if hook is None:
            continue
        key = entry.get("key")
        current_hash = entry.get("currentHash")
        trust_status = entry.get("trustStatus")
        enabled = entry.get("enabled")
        if not isinstance(key, str) or not isinstance(current_hash, str) or not isinstance(trust_status, str) or not isinstance(enabled, bool):
            raise HookError("incomplete hooks/list entry")
        found = LocatedHook(hook, key, current_hash, trust_status, enabled)
        previous = selected[hook]
        if _active_hook(found):
            selected[hook] = found
        elif not _active_hook(previous):
            if isinstance(previous, MissingHook) or (found.trust_status == "trusted" and previous.trust_status != "trusted"):
                selected[hook] = found
    return selected


def _status(entry: LocatedHook | MissingHook) -> str:
    if isinstance(entry, MissingHook):
        return "absent"
    return "disabled" if not entry.enabled else entry.trust_status


def _failure(entry: LocatedHook | MissingHook, listed: list[dict[str, object]]) -> str:
    if isinstance(entry, LocatedHook):
        return f"{entry.hook.name}: trustStatus={entry.trust_status!r}, enabled={entry.enabled!r}"
    expected = _listed_fields(entry.hook)
    candidates = [value for value in listed if value.get("command") == entry.hook.command]
    if not candidates:
        return f"{entry.hook.name}: hooks/list has no command {entry.hook.command!r}"
    closest = max(candidates, key=lambda value: sum(value.get(field) == wanted for field, wanted in expected.items()))
    differences = [f"{field}: expected {wanted!r}, got {closest.get(field)!r}"
                   for field, wanted in expected.items() if closest.get(field) != wanted]
    return f"{entry.hook.name}: hooks/list " + "; ".join(differences)


def check_showrunner_guard(home: Path, registrations: Sequence[tuple[str, str, int]]) -> list[str]:
    """Check that Codex can run the registered showrunner guard handlers."""
    hooks: list[CodexHook] = []
    for matcher, command, timeout in dict.fromkeys(registrations):
        hook = CodexHook(f"showrunner guard ({matcher})", PythonHookScript("pre-tool-use-showrunner-guard-codex.py"),
                         "PreToolUse", timeout, matcher)
        hook.command = command
        hooks.append(hook)
    path = str(home / "hooks.json")
    try:
        server = AppServer(_command_binary(), str(home))
        try:
            _initialize(server)
            listed = _listed_hook_entries(server, path)
            entries = _select_hooks(listed, hooks)
        finally:
            server.close()
    except (HookError, OSError) as error:
        return [f"{path}: {error}"]
    return [f"{path}: {_failure(entry, listed)}" for entry in entries.values() if not _active_hook(entry)]


def _guard_handler(handler: object, script: str) -> bool:
    if not isinstance(handler, dict):
        return False
    entry = cast(dict[str, object], handler)
    command = entry.get("command")
    if entry.get("type") != "command" or not isinstance(command, str):
        return False
    try:
        words = shlex.split(command)
    except ValueError:
        return False
    checkout = Path(_home()) / ".claude"
    paths = [Path(os.path.expandvars(word)).expanduser() for word in words]
    return len(paths) == 2 and all(path.is_absolute() for path in paths) and (
        tuple(path.resolve() for path in paths) ==
        ((checkout / "scripts/lib/py").resolve(), (checkout / "scripts/hooks" / script).resolve())
    )


def _remove_guard_handlers(document: dict[str, object], path: Path) -> RegistryEntryMoves:
    hooks = _object(document.get("hooks", {}), f"{path} hooks")
    moves: RegistryEntryMoves = {}
    for event, prefix in (("PreToolUse", "pre"), ("PostToolUse", "post")):
        groups = hooks.get(event, [])
        if not isinstance(groups, list):
            raise HookError(f"{path}: invalid {event} groups")
        script = f"{prefix}-tool-use-showrunner-guard-codex.py"
        kept_groups: list[object] = []
        for group_index, group in enumerate(cast(list[object], groups)):
            fields = _object(group, f"{path} {event} group")
            handlers = fields.get("hooks")
            if not isinstance(handlers, list):
                raise HookError(f"{path}: invalid {event} handlers")
            kept_handlers: list[object] = []
            for index, handler in enumerate(cast(list[object], handlers)):
                old = f"{prefix}_tool_use:{group_index}:{index}"
                if _guard_handler(handler, script):
                    moves[old] = RemovedRegistryEntry.REMOVED
                    continue
                new = f"{prefix}_tool_use:{len(kept_groups)}:{len(kept_handlers)}"
                if new != old:
                    moves[old] = new
                kept_handlers.append(handler)
            if kept_handlers or not handlers:
                kept_groups.append({**fields, "hooks": kept_handlers})
        if event in hooks:
            hooks[event] = kept_groups
    return moves


def _user_hook_state(server: AppServer, expected_config: str) -> dict[str, object]:
    response = server.call("config/read", {"includeLayers": True})
    layers = response.get("layers")
    if not isinstance(layers, list):
        raise HookError("invalid config/read layers")
    for candidate in cast(list[object], layers):
        layer = _object(candidate, "config/read layer")
        name = _object(layer.get("name"), "config/read layer name")
        if name.get("type") == "user":
            location = name.get("file")
            if not isinstance(location, str) or str(Path(location).resolve()) != expected_config:
                raise HookError(f"{expected_config}: app-server returned a different user config")
            config = _object(layer.get("config"), "config/read user config")
            hooks = _object(config.get("hooks", {}), "user hooks")
            return _object(hooks.get("state", {}), "user hook trust")
    return {}


class RegistryTrustGroup(TypedDict):
    registry: str
    homes: list[str]


class HookSettingTransition(TypedDict):
    """A null value records an absent config key."""

    before: dict[str, object] | None
    staged: dict[str, object] | None
    after: dict[str, object] | None


TrustStage = Literal["before", "staged", "after"]


class HookOccurrenceMove(TypedDict):
    source_key: str
    handler_hash: str


class ConfigTrustPlan(RegistryTrustGroup):
    settings: dict[str, HookSettingTransition]
    retained: dict[str, HookOccurrenceMove]


class RegistryChangeJournal(TypedDict):
    version: Literal[7]
    original_hash: str
    registry: str
    configs: dict[str, ConfigTrustPlan]


class TimeoutNormalizationJournal(TypedDict):
    version: Literal[3]
    original_hash: str
    registry: str
    path: str
    homes: list[str]
    configs: dict[str, str]


def _registry_aliases(path: Path, homes: list[Path]) -> list[Path]:
    aliases: list[Path] = []
    for home in homes:
        alias = home / "hooks.json"
        try:
            if alias.resolve() == path:
                aliases.append(home)
        except (OSError, RuntimeError):
            # A broken link in an unrelated home cannot share this registry.
            if os.path.lexists(alias):
                continue
            raise
    return aliases


def _config_path(home: str) -> str:
    return str(Path(home, "config.toml").resolve())


def _recovery_homes(path: Path, recorded: dict[str, str]) -> dict[str, RegistryTrustGroup]:
    for home, config in recorded.items():
        _check_trust_paths(path, home, config)
    homes = dict(recorded)
    for alias in _registry_aliases(path, known_codex_homes()):
        home = str(alias)
        if home not in homes:
            homes[home] = _config_path(home)
    groups: dict[str, RegistryTrustGroup] = {}
    for home, config in sorted(homes.items(), key=lambda item: (item[1], item[0])):
        groups.setdefault(config, {"registry": str(path), "homes": []})["homes"].append(home)
    return groups


def _registry_spellings(group: RegistryTrustGroup) -> set[str]:
    return {group["registry"], *(str(Path(home, "hooks.json")) for home in group["homes"])}


def _trust_slot(key: str) -> tuple[str, str]:
    parts = key.rsplit(":", 3)
    if len(parts) == 4 and all(index.isascii() and index.isdigit() for index in parts[2:]):
        return parts[0], ":".join(parts[1:])
    return "", ""


@contextlib.contextmanager
def _hook_server(binary: str, home: str) -> Generator[AppServer]:
    server = AppServer(binary, home)
    try:
        _initialize(server)
        yield server
    finally:
        server.close()


def _registry_entries(server: AppServer, home: str, canonical: str) -> dict[str, dict[str, object]]:
    entries: dict[str, dict[str, object]] = {}
    for entry in _listed_hook_entries(server, str(Path(home, "hooks.json")), canonical):
        key = entry.get("key")
        if not isinstance(key, str):
            raise HookError("incomplete hooks/list entry")
        entries[key] = entry
    return entries


def _validate_trust_key(path: Path, config: str, key: str) -> None:
    spelling, slot = _trust_slot(key)
    if spelling == str(path) and slot:
        return
    registry = Path(spelling)
    if registry.name != "hooks.json" or not registry.is_absolute() or not slot:
        raise HookError(f"{key}: invalid registry trust key")
    _check_trust_paths(path, str(registry.parent), config)


def _affected_settings(
    path: Path, config: str, group: RegistryTrustGroup, state: dict[str, object],
) -> dict[str, dict[str, object]]:
    spellings = _registry_spellings(group)
    affected: dict[str, dict[str, object]] = {}
    for key, value in state.items():
        spelling, slot = _trust_slot(key)
        if spelling in spellings or (slot and Path(spelling).resolve() == path):
            _validate_trust_key(path, config, key)
            affected[key] = _object(value, key)
    return affected


def _without_trust(value: dict[str, object]) -> dict[str, object] | None:
    return {field: setting for field, setting in value.items() if field != "trusted_hash"} or None


def _disabled_without_trust(value: dict[str, object] | None) -> dict[str, object]:
    untrusted = _without_trust(value or {})
    return {**(untrusted or {}), "enabled": False}


def _registry_occurrences(registry: bytes | str) -> dict[str, tuple[dict[str, object], object]]:
    document = _object(cast(object, json.loads(registry)), "hook registry")
    hooks = _object(document.get("hooks", {}), "hooks")
    return {
        f"{re.sub(r'(?<!^)(?=[A-Z])', '_', event).lower()}:{group}:{index}":
        ({key: value for key, value in fields.items() if key != "hooks"}, handler)
        for event, group, fields, handlers in _hook_groups(hooks)
        for index, handler in enumerate(handlers)
    }


def _prepare_registry_change(
    homes: dict[str, RegistryTrustGroup], original: bytes, registry: str, moves: RegistryEntryMoves,
    binary: str, *, install: bool,
) -> RegistryChangeJournal:
    live_slots = _registry_occurrences(original).keys() | _registry_occurrences(registry).keys()
    configs: dict[str, ConfigTrustPlan] = {}
    for config, group in homes.items():
        home = group["homes"][0]
        canonical = group["registry"]
        with _hook_server(binary, home) as server:
            state = _user_hook_state(server, config)
            listed = _registry_entries(server, home, canonical)
        before = _affected_settings(Path(canonical), config, group, state)
        occurrences = {_trust_slot(key)[1]: entry for key, entry in listed.items()}
        after: dict[str, dict[str, object] | None] = {
            key: value if _trust_slot(key)[1] in live_slots else _without_trust(value)
            for key, value in before.items()
        }
        retained: dict[str, HookOccurrenceMove] = {}
        destinations: dict[str, dict[str, object] | None] = {}
        staged: set[str] = set()
        spellings = _registry_spellings(group) | {_trust_slot(key)[0] for key in before}
        for spelling in sorted(spellings):
            for slot, destination in moves.items():
                source = f"{spelling}:{slot}"
                value = before.get(source, {})
                staged.add(source)
                after[source] = None
                if destination is RemovedRegistryEntry.REMOVED:
                    continue
                entry = occurrences.get(slot)
                if entry is None or not isinstance(handler_hash := entry.get("currentHash"), str):
                    raise HookError(f"{source}: retained hook is missing or has no identity before registry change")
                target = f"{spelling}:{destination}"
                staged.add(target)
                if install and value.get("enabled") is not False and listed.get(source, {}).get("enabled") is False:
                    destinations[target] = _disabled_without_trust(value)
                else:
                    destinations[target] = value or None
                retained[target] = {"source_key": source, "handler_hash": handler_hash}
        after.update(destinations)
        configs[config] = {
            **group,
            "settings": {key: {"before": before.get(key),
                               "staged": _disabled_without_trust(before.get(key)) if key in staged else before.get(key),
                               "after": value} for key, value in after.items()},
            "retained": retained,
        }
    return {"version": 7, "original_hash": hashlib.sha256(original).hexdigest(),
            "registry": registry, "configs": configs}


def _publish_registry_journal(marker: Path, journal: RegistryChangeJournal | TimeoutNormalizationJournal) -> None:
    from scripts.lib.atomic_file import write_text_atomic

    write_text_atomic(marker, json.dumps(journal) + "\n", new_file_mode=0o600)


def _journal_text(record: dict[str, object], field: str, marker: Path) -> str:
    value = record.get(field)
    if not isinstance(value, str):
        raise HookError(f"{marker}: invalid journal field {field}")
    return value


def _journal_list(record: dict[str, object], field: str, marker: Path) -> list[object]:
    value = record.get(field)
    if not isinstance(value, list):
        raise HookError(f"{marker}: invalid journal field {field}")
    return cast(list[object], value)


def _check_trust_paths(path: Path, home: str, config: str) -> None:
    if (not Path(home).is_absolute() or str(Path(home)) != home or
            Path(home, "hooks.json").resolve() != path or _config_path(home) != config):
        raise HookError(f"{home}: hook or trust destination changed during registry update")


def _pending_trust(state: Mapping[str, object], expected: Mapping[str, object]) -> dict[str, object]:
    return {key: value for key, value in expected.items()
            if (key in state if value is None else state.get(key) != value)}


def _write_trust_migration(
    server: AppServer, state: dict[str, object], pending: dict[str, object], *, install: bool,
) -> None:
    if not pending:
        return
    if install and all(
        isinstance(value, dict) and set(cast(dict[str, object], value)) == {"trusted_hash"}
        and (key not in state or (
            isinstance(state[key], dict) and set(cast(dict[str, object], state[key])) == {"trusted_hash"}
        )) for key, value in pending.items()
    ):
        _trust_handlers(server, cast(dict[str, dict[str, str]], pending))
        return
    edits = [{"keyPath": f"hooks.state.{json.dumps(key, ensure_ascii=False)}",
              "value": value, "mergeStrategy": "replace"} for key, value in pending.items()]
    _ = server.call("config/batchWrite", {"edits": edits, "reloadUserConfig": True})


def _check_registry_revision(path: Path, original: bytes, revision: str, registry: str) -> bool:
    if original == registry.encode("utf-8"):
        return True
    if hashlib.sha256(original).hexdigest() != revision:
        raise HookError(f"{path}: registry changed during hook update")
    return False


def _journal_config(item: object, marker: Path, *, upgrade: bool) -> ConfigTrustPlan:
    fields = _object(item, str(marker))
    homes = _journal_list(fields, "homes", marker)
    if not homes or not all(isinstance(home, str) for home in homes):
        raise HookError(f"{marker}: invalid config recovery homes")
    settings: dict[str, HookSettingTransition] = {}
    for key, value in _object(fields.get("settings"), f"{marker} hook settings").items():
        pair = _object(value, f"{marker} {key}")
        if "before" not in pair or "after" not in pair:
            raise HookError(f"{marker}: missing before or after value for {key}")
        before, after = pair["before"], pair["after"]
        if not upgrade and "staged" not in pair:
            raise HookError(f"{marker}: missing staged value for {key}")
        staged = pair.get("staged")
        settings[key] = {"before": None if before is None else _object(before, key),
                         "staged": None if staged is None else _object(staged, key),
                         "after": None if after is None else _object(after, key)}
    retained: dict[str, HookOccurrenceMove] = {}
    for key, value in _object(fields.get("retained"), f"{marker} retained hooks").items():
        move = _object(value, f"{marker} {key}")
        source = _journal_text(move, "source_key", marker)
        if key not in settings or source not in settings or _trust_slot(source)[0] != _trust_slot(key)[0]:
            raise HookError(f"{marker}: missing setting transition for {key}")
        if (settings[source]["before"] or {}).get("enabled") is False and (
            settings[key]["after"] or {}
        ).get("enabled") is not False:
            raise HookError(f"{marker}: missing disabled setting for {key}")
        retained[key] = {"source_key": source, "handler_hash": _journal_text(move, "handler_hash", marker)}
    moved_keys = retained.keys() | {move["source_key"] for move in retained.values()}
    for key, transition in settings.items():
        before = transition["before"]
        if upgrade:
            changed = key in moved_keys or before != transition["after"]
            transition["staged"] = _disabled_without_trust(before) if changed else before
        elif key in moved_keys and transition["staged"] != _disabled_without_trust(before):
            raise HookError(f"{marker}: invalid staged hook setting for {key}")
        elif transition["staged"] not in (before, _disabled_without_trust(before)):
            raise HookError(f"{marker}: invalid staged hook setting for {key}")
    return {"registry": _journal_text(fields, "registry", marker), "homes": cast(list[str], homes),
            "settings": settings, "retained": retained}


def _validate_registry_plan(path: Path, marker: Path, journal: RegistryChangeJournal) -> None:
    recorded: dict[str, str] = {}
    for config, plan in journal["configs"].items():
        if plan["registry"] != str(path):
            raise HookError(f"{config}: canonical hook registry changed during recovery")
        for home in plan["homes"]:
            if home in recorded:
                raise HookError(f"{marker}: duplicate home in the registry recovery plan")
            recorded[home] = config
        for key in plan["settings"]:
            _validate_trust_key(path, config, key)
    discovered = _recovery_homes(path, recorded)
    homes = {home: config for config, group in discovered.items() for home in group["homes"]}
    if homes != recorded:
        raise HookError(f"{marker}: a new home shares the hook registry. Manual repair is required.")


def _check_retained_hooks(
    listed: dict[str, dict[str, object]], plan: ConfigTrustPlan, *, replaced: bool, complete: bool,
) -> None:
    occurrences = {_trust_slot(key)[1]: entry for key, entry in listed.items()}
    for target, move in plan["retained"].items():
        key = target if replaced else move["source_key"]
        entry = occurrences.get(_trust_slot(key)[1])
        if entry is None or entry.get("currentHash") != move["handler_hash"]:
            raise HookError(f"{key}: retained hook is missing or has a different identity")
        expected = plan["settings"][target]["after"]
        if complete and expected is not None and expected.get("enabled") is False:
            if key in listed and listed[key].get("enabled") is not False:
                raise HookError(f"{key}: the retained hook is no longer disabled")


def _check_config_plan(
    path: Path, config: str, plan: ConfigTrustPlan, state: dict[str, object], *,
    allowed: tuple[TrustStage, ...], target: TrustStage,
) -> dict[str, object]:
    affected = _affected_settings(path, config, plan, state)
    if uncovered := affected.keys() - plan["settings"].keys():
        raise HookError(f"{min(uncovered)}: uncovered hook setting. Manual repair is required.")
    pending: dict[str, object] = {}
    for key, values in plan["settings"].items():
        actual = affected.get(key)
        if actual == values[target]:
            continue
        if not allowed:
            raise HookError(f"{key}: saved hook trust does not match the journal")
        if all(actual != values[stage] for stage in allowed):
            raise HookError(f"{key}: saved hook setting conflicts with the journal")
        pending[key] = values[target]
    return pending


def _check_registry_settings(
    path: Path, journal: RegistryChangeJournal, binary: str, *,
    replaced: bool, allowed: tuple[TrustStage, ...], target: TrustStage,
) -> set[str]:
    needs_write: set[str] = set()
    for config, plan in journal["configs"].items():
        home = plan["homes"][0]
        with _hook_server(binary, home) as server:
            state = _user_hook_state(server, config)
            if _check_config_plan(path, config, plan, state, allowed=allowed, target=target):
                needs_write.add(config)
            if plan["retained"]:
                _check_retained_hooks(_registry_entries(server, home, str(path)), plan,
                                      replaced=replaced, complete=not allowed and target == "after")
    return needs_write


def _check_registry_update(path: Path, marker: Path, journal: RegistryChangeJournal, expected: bytes) -> None:
    _validate_registry_plan(path, marker, journal)
    if _read_regular_bytes(path) != expected:
        raise HookError(f"{path}: registry changed during hook update")


def _apply_registry_settings(
    path: Path, marker: Path, original: bytes, journal: RegistryChangeJournal, binary: str, *,
    replaced: bool, allowed: tuple[TrustStage, ...], target: TrustStage, install: bool,
) -> None:
    needs_write = _check_registry_settings(path, journal, binary, replaced=replaced, allowed=allowed, target=target)
    for config, plan in journal["configs"].items():
        if config not in needs_write:
            continue
        home = plan["homes"][0]
        with _hook_server(binary, home) as server:
            state = _user_hook_state(server, config)
            pending = _check_config_plan(path, config, plan, state, allowed=allowed, target=target)
            _check_registry_update(path, marker, journal, original)
            _write_trust_migration(server, state, pending, install=install and target == "after")
    _ = _check_registry_settings(path, journal, binary, replaced=replaced, allowed=(), target=target)
    _check_registry_update(path, marker, journal, original)


def _finish_registry_change(
    path: Path, marker: Path, original: bytes, journal: RegistryChangeJournal, binary: str, *,
    install: bool, upgrade: bool = False,
) -> None:
    from scripts.lib.atomic_file import write_text_atomic

    registry = journal["registry"]
    replacement = registry.encode("utf-8")
    replaced = _check_registry_revision(path, original, journal["original_hash"], registry)
    _check_registry_update(path, marker, journal, original)
    if upgrade or not replaced:
        allowed: tuple[TrustStage, ...] = ("before", "after") if upgrade and (replaced or not install) else ("before",)
        _apply_registry_settings(path, marker, original, journal, binary,
                                 replaced=replaced, allowed=allowed, target="staged", install=install)
    if upgrade:
        for plan in journal["configs"].values():
            sources = {move["source_key"] for move in plan["retained"].values()}
            for key in sources - plan["retained"].keys():
                plan["settings"][key]["after"] = None
        _publish_registry_journal(marker, journal)
    if not replaced:
        if install:
            _write_installed_registry(path, registry)
        else:
            write_text_atomic(path, registry)
    _apply_registry_settings(path, marker, replacement, journal, binary,
                             replaced=True, allowed=("staged",), target="after", install=install)
    _clear_journal(marker)


def _finish_timeout_normalization(
    path: Path, marker: Path, original: bytes, journal: TimeoutNormalizationJournal, binary: str,
) -> None:
    alias = Path(journal["path"])
    if alias.resolve() != path or str(alias.parent) not in journal["configs"]:
        raise HookError(f"{marker}: a hook registry path changed during timeout normalization")
    homes = _recovery_homes(path, journal["configs"])
    registry = journal["registry"]
    if not _check_registry_revision(path, original, journal["original_hash"], registry):
        _write_installed_registry(path, registry)
    normalized = _read_regular_bytes(path)
    if normalized != registry.encode("utf-8"):
        raise HookError(f"{path}: registry changed after timeout normalization")
    document = _object(cast(object, json.loads(normalized)), str(path))
    _complete_install(path, marker, str(alias), normalized, document, homes, binary, pending=True)


def _legacy_ownership(
    record: dict[str, object], marker: Path, *, install: bool,
) -> tuple[dict[str, str], dict[str, set[str]]]:
    homes: dict[str, str] = {}
    keys: dict[str, set[str]] = {}
    version = record.get("version")
    if version == 1 and install:
        home = _journal_text(record, "home", marker)
        config = _journal_text(record, "config", marker)
        keys[config] = {_journal_text(_object(item, str(marker)), "key", marker)
                        for item in _journal_list(record, "trust", marker)}
        return {home: config}, keys
    if version == 5:
        for config, item in _object(record.get("configs"), f"{marker} configs").items():
            group = _object(item, str(marker))
            for home in _journal_list(group, "homes", marker):
                if not isinstance(home, str) or home in homes:
                    raise HookError(f"{marker}: invalid legacy home")
                homes[home] = config
            affected = set(_object(group.get("edits"), f"{marker} trust edits"))
            affected.update(_object(group.get("unproved", {}), f"{marker} unproved trust"))
            for key, item in _object(group.get("retained", {}), f"{marker} retained hooks").items():
                move = _object(item, str(marker))
                home = _journal_text(move, "home", marker)
                if homes.get(home) != config:
                    raise HookError(f"{marker}: invalid home for a retained hook")
                old = _journal_text(move, "old_slot", marker)
                affected.update((key, f"{Path(home, 'hooks.json')}:{old}"))
            keys[config] = affected
        return homes, keys
    for item in _journal_list(record, "trust", marker):
        group = _object(item, str(marker))
        home = _journal_text(group, "home", marker)
        config = _journal_text(group, "config", marker)
        if home in homes:
            raise HookError(f"{marker}: duplicate legacy home")
        homes[home] = config
        affected = keys.setdefault(config, set())
        if "edits" in group:
            affected.update(_object(group["edits"], f"{marker} trust edits"))
        if "unproved" in group:
            affected.update(_object(group["unproved"], f"{marker} unproved trust"))
        if "obsolete_keys" in group:
            for key in _journal_list(group, "obsolete_keys", marker):
                if not isinstance(key, str):
                    raise HookError(f"{marker}: invalid obsolete key")
                affected.add(key)
        if "retained" in group:
            for item in _journal_list(group, "retained", marker):
                move = _object(item, str(marker))
                affected.update((_journal_text(move, "old_key", marker), _journal_text(move, "destination_key", marker)))
    return homes, keys


def _legacy_groups(
    path: Path, homes: dict[str, str], keys: dict[str, set[str]],
) -> dict[str, RegistryTrustGroup]:
    groups = _recovery_homes(path, homes)
    for config, affected in keys.items():
        if config not in groups:
            raise HookError(f"{config}: no validated home for legacy trust")
        for key in affected:
            _validate_trust_key(path, config, key)
    return groups


def _unchanged_legacy_slots(
    path: Path, original: bytes, revision: str, replacement: str, *, install: bool,
) -> set[str]:
    current = _registry_occurrences(original)
    if hashlib.sha256(original).hexdigest() != revision:
        # Guard removal changes only the two tool events.
        return {slot for slot in current if not install and
                not slot.startswith(("pre_tool_use:", "post_tool_use:"))}
    document = _object(cast(object, json.loads(original)), str(path))
    if install:
        hooks = _object(document.get("hooks", {}), "hooks")
        moves = _remove_retired_handlers(hooks, str(path), report=False).moves
    else:
        moves = _remove_guard_handlers(document, path)
    changed = moves.keys() | {target for target in moves.values() if isinstance(target, str)}
    if replacement:
        final = _registry_occurrences(replacement)
        return {slot for slot, entry in current.items() if slot not in changed and final.get(slot) == entry}
    return current.keys() - changed


ResolvedConfigPath = NewType("ResolvedConfigPath", str)


class LegacyTrustRevocation(NamedTuple):
    """The settings and revocation scope for one validated config."""

    config: ResolvedConfigPath
    group: RegistryTrustGroup
    preserved_occurrences: frozenset[tuple[ResolvedConfigPath, str]]
    before: dict[str, dict[str, object]]
    preserved: dict[str, dict[str, object]]
    edits: dict[str, object]

    def preserves(self, key: str) -> bool:
        return (self.config, _trust_slot(key)[1]) in self.preserved_occurrences

    def settings(self, state: dict[str, object]) -> dict[str, dict[str, object]]:
        return _affected_settings(Path(self.group["registry"]), self.config, self.group, state)


def _plan_legacy_revocation(
    config: ResolvedConfigPath, group: RegistryTrustGroup, binary: str,
    unchanged: frozenset[str], affected_keys: set[str],
) -> LegacyTrustRevocation:
    affected_slots = {_trust_slot(key)[1] for key in affected_keys}
    preserved_slots = unchanged - affected_slots
    with _hook_server(binary, group["homes"][0]) as server:
        state = _user_hook_state(server, config)
    before = _affected_settings(Path(group["registry"]), config, group, state)
    preserved = {key: raw for key, raw in before.items() if _trust_slot(key)[1] in preserved_slots}
    edits: dict[str, object] = {
        key: _without_trust(raw) for key, raw in before.items() if key not in preserved and "trusted_hash" in raw
    }
    return LegacyTrustRevocation(
        config, group, frozenset((config, slot) for slot in preserved_slots), before, preserved, edits,
    )


def _write_legacy_revocation(
    plan: LegacyTrustRevocation, binary: str, check_ownership: Callable[[], None],
) -> None:
    with _hook_server(binary, plan.group["homes"][0]) as server:
        state = _user_hook_state(server, plan.config)
        if plan.settings(state) != plan.before:
            raise HookError(f"{plan.config}: hook settings changed before legacy trust revocation")
        check_ownership()
        _write_trust_migration(server, state, plan.edits, install=False)


def _verify_legacy_revocation(plan: LegacyTrustRevocation, binary: str) -> None:
    with _hook_server(binary, plan.group["homes"][0]) as server:
        state = _user_hook_state(server, plan.config)
    if _pending_trust(state, plan.edits):
        raise HookError(f"{plan.config}: saved hook trust does not match the journal")
    actual = plan.settings(state)
    if _pending_trust(actual, plan.preserved):
        raise HookError(f"{plan.config}: unchanged hook settings differ from the recovery record")
    if any("trusted_hash" in value for key, value in actual.items() if not plan.preserves(key)):
        raise HookError(f"{plan.config}: uncovered hook trust. Manual repair is required.")


def _refuse_legacy_recovery(
    path: Path, marker: Path, original: bytes, record: dict[str, object], binary: str, *, install: bool,
) -> NoReturn:
    from scripts.lib.atomic_file import write_text_atomic

    revision = _journal_text(record, "original_hash", marker)
    snapshot_only = record.get("version") == 1 and install
    if snapshot_only:
        replacement = ""
        replacement_hash = _journal_text(record, "registry_hash", marker)
        if hashlib.sha256(original).hexdigest() not in (revision, replacement_hash):
            raise HookError(f"{path}: registry matches neither legacy install hash")
    else:
        replacement = _journal_text(record, "registry", marker)
        _ = _check_registry_revision(path, original, revision, replacement)
    unchanged = frozenset(_unchanged_legacy_slots(path, original, revision, replacement, install=install))
    homes, keys = _legacy_ownership(record, marker, install=install)
    identity = json.dumps([marker.name, record], sort_keys=True).encode("utf-8")
    digest = hashlib.sha256(identity).hexdigest()
    archive = path.with_name(f".codex-hooks-install-recovery-legacy-{digest}.json")
    try:
        saved = _object(cast(object, json.loads(_read_regular_bytes(archive))), str(archive))
    except FileNotFoundError:
        saved = {}
    if saved:
        if saved.get("journal") != record:
            raise HookError(f"{archive}: recovery record does not match the legacy journal")
        for home, config in _object(saved.get("homes"), f"{archive} homes").items():
            if not isinstance(config, str) or (home in homes and homes[home] != config):
                raise HookError(f"{archive}: conflicting legacy config for {home}")
            homes[home] = config
    groups = _legacy_groups(path, homes, keys)
    revoked = _object(saved.get("revoked", {}), f"{archive} revoked trust")
    preserved_configs = _object(saved.get("preserved", {}), f"{archive} preserved settings")
    revocations: Mapping[ResolvedConfigPath, LegacyTrustRevocation] = {
        ResolvedConfigPath(config): _plan_legacy_revocation(
            ResolvedConfigPath(config), group, binary, unchanged, set() if snapshot_only else keys.get(config, set()),
        )
        for config, group in groups.items()
    }
    for config, plan in revocations.items():
        if config in preserved_configs and plan.preserved != preserved_configs[config]:
            raise HookError(f"{config}: unchanged hook settings differ from the recovery record")
        preserved_configs[config] = plan.preserved
        if plan.edits:
            saved_config = _object(revoked.get(config, {}), f"{archive} {config}")
            revoked[config] = {**{key: plan.before[key] for key in plan.edits}, **saved_config}
    homes = {home: config for config, group in groups.items() for home in group["homes"]}
    if any(plan.edits for plan in revocations.values()):
        evidence = {"journal": record, "homes": homes, "revoked": revoked, "preserved": preserved_configs}
        write_text_atomic(archive, json.dumps(evidence, sort_keys=True) + "\n", new_file_mode=0o600)

    def check_ownership() -> None:
        if _legacy_groups(path, homes, keys) != groups or _read_regular_bytes(path) != original:
            raise HookError(f"{path}: registry changed during legacy trust revocation")

    for plan in revocations.values():
        if plan.edits:
            _write_legacy_revocation(plan, binary, check_ownership)
    for plan in revocations.values():
        _verify_legacy_revocation(plan, binary)
    check_ownership()
    raise HookError(f"{marker}: legacy journal lacks complete before and after values. Manual repair is required.")


def _resume_registry_change(path: Path, marker: Path, binary: str, *, install: bool) -> bool:
    try:
        record = _object(cast(object, json.loads(_read_regular_bytes(marker))), str(marker))
    except FileNotFoundError:
        return False
    original = _read_regular_bytes(path)
    if install and record.get("version") == 3:
        listed_homes = _journal_list(record, "homes", marker)
        if not all(isinstance(home, str) for home in listed_homes):
            raise HookError(f"{marker}: invalid homes for timeout normalization")
        homes = cast(list[str], listed_homes)
        configs = ({home: _journal_text(_object(record["configs"], f"{marker} configs"), home, marker)
                    for home in homes} if "configs" in record else {home: _config_path(home) for home in homes})
        normalized: TimeoutNormalizationJournal = {
            "version": 3, "original_hash": _journal_text(record, "original_hash", marker),
            "registry": _journal_text(record, "registry", marker),
            "path": _journal_text(record, "path", marker), "homes": homes, "configs": configs,
        }
        _finish_timeout_normalization(path, marker, original, normalized, binary)
        return True
    version = record.get("version")
    if version in (1, 2, 4, 5):
        _refuse_legacy_recovery(path, marker, original, record, binary, install=install)
    if version not in (6, 7):
        raise HookError(f"{marker}: invalid registry journal version")
    upgrade = version == 6
    configs = _object(record.get("configs"), f"{marker} configs")
    journal: RegistryChangeJournal = {
        "version": 7, "original_hash": _journal_text(record, "original_hash", marker),
        "registry": _journal_text(record, "registry", marker),
        "configs": {config: _journal_config(item, marker, upgrade=upgrade) for config, item in configs.items()},
    }
    _finish_registry_change(path, marker, original, journal, binary, install=install, upgrade=upgrade)
    return True


def _remove_guard_registry(path: Path, aliases: list[Path]) -> None:
    marker = path.with_name(".showrunner-guard-removal.json")
    install_marker = path.with_name(".codex-hooks-install.json")
    binary = _command_binary()
    with _registry_lock(path):
        if not _resume_registry_change(path, marker, binary, install=False):
            _ = _resume_registry_change(path, install_marker, binary, install=True)
            original = _read_regular_bytes(path)
            document = _object(cast(object, json.loads(original)), str(path))
            moves = _remove_guard_handlers(document, path)
            if not moves:
                return
            registry = json.dumps(document, indent=2) + "\n"
            homes = _recovery_homes(path, {str(alias): _config_path(str(alias)) for alias in aliases})
            journal = _prepare_registry_change(homes, original, registry, moves, binary, install=False)
            _publish_registry_journal(marker, journal)
            _finish_registry_change(path, marker, original, journal, binary, install=False)
    print(f"{path}: removed showrunner guard hooks")


def _remove_showrunner_guard() -> int:
    registries: dict[Path, list[Path]] = {}
    failed = False
    for home in known_codex_homes():
        path = home / "hooks.json"
        try:
            if os.path.lexists(path):
                registries.setdefault(path.resolve(), []).append(home)
        except (OSError, RuntimeError, ValueError) as error:
            print(f"codex hooks: {path}: {' '.join(str(error).splitlines())}", file=sys.stderr)
            failed = True
    for path, aliases in registries.items():
        try:
            _remove_guard_registry(path, aliases)
        except (HookError, OSError, RuntimeError, ValueError) as error:
            print(f"codex hooks: {path}: {' '.join(str(error).splitlines())}", file=sys.stderr)
            failed = True
    return int(failed)


def _run_hooks(action: str, codex_home: str, binary: str, path: str) -> int:
    server = AppServer(binary, codex_home)
    try:
        _initialize(server)
        listed = _listed_hook_entries(server, path)
        entries = _select_hooks(listed)
        if action == "install":
            for hook in HOOKS:
                entry = entries[hook]
                if isinstance(entry, LocatedHook) and entry.trust_status in {"untrusted", "modified"}:
                    _trust_handlers(server, {entry.key: {"trusted_hash": entry.current_hash}})
                    listed = _listed_hook_entries(server, path)
                    entries = _select_hooks(listed)
    finally:
        server.close()
    success = all(_active_hook(entry) for entry in entries.values())
    for hook in HOOKS:
        entry = entries[hook]
        if action == "install" and success and isinstance(entry, LocatedHook):
            print(f"{hook.name} codex hook: trusted {entry.key}")
        else:
            print(f"{hook.name} codex hook: {_status(entry)}")
        if not _active_hook(entry):
            print(f"codex hooks: {action}: {_failure(entry, listed)}", file=sys.stderr)
    return 0 if success else 1


def _run(action: str) -> int:
    if action == "remove-showrunner-guard":
        return _remove_showrunner_guard()
    codex_home = _codex_home()
    binary = _command_binary()
    path = Path(codex_home) / "hooks.json"
    if action == "check":
        return _run_hooks(action, codex_home, binary, str(path))
    destination = path.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with _registry_lock(destination):
        marker = destination.with_name(".showrunner-guard-removal.json")
        install_marker = destination.with_name(".codex-hooks-install.json")
        try:
            _ = _resume_registry_change(destination, marker, binary, install=False)
        except (HookError, OSError, RuntimeError, ValueError) as error:
            reason = " ".join(str(error).splitlines())
            raise HookError(f"{marker}: run remove-showrunner-guard to finish the pending removal. {reason}") from error
        _ = _resume_registry_change(destination, install_marker, binary, install=True)
        _install_file(str(path), binary, destination, install_marker)
        return _run_hooks(action, codex_home, binary, str(path))


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in {"install", "check", "remove-showrunner-guard"}:
        print("usage: codex_hooks.py <install|check|remove-showrunner-guard>", file=sys.stderr)
        return 1
    try:
        return _run(sys.argv[1])
    except (HookError, OSError, RuntimeError, ValueError) as error:
        reason = " ".join(str(error).splitlines())
        print(f"codex hooks: {sys.argv[1]}: {reason}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
