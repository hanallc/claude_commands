#!/usr/bin/env python3
"""Keep waiting configuration rows live across a Git move."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

from collections.abc import Generator, Iterable, Mapping
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, replace
from enum import Enum, auto
import fcntl
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
from typing import Literal, NotRequired, TypedDict, cast

from scripts.lib import build_folders, conf, conf_reads, file_lock, git_index, registry_rows
from scripts.lib.atomic_file import write_text_atomic
from scripts.lib.transcripts import config_dirs

WAITING_PATHS = ":(glob)config/*.conf"
REGISTRY_PATH = "config/models.conf"
ORDER_PATH = "config/settings-orders.json"
_CHECKOUT_LOCKS = ContextVar("checkout_locks", default=frozenset[Path]())


def model_color_host(key: str) -> str:
    """Return the reserved host suffix of a model color key, if present."""
    base = key.rpartition(".")[0]
    if conf.full_model_color_key("statusline", base):
        return conf.host_suffix(key, conf.known_host_names())
    return ""


def model_color_key(stem: str, key: str) -> bool:
    """Allow a new color only for a full model ID in its catalog."""
    if not conf.full_model_color_key(stem, key):
        return False
    model = key.removeprefix("model.")
    if model.startswith("gpt-"):
        override = os.environ.get("MODELS_CONFIG_FILE")
        source = Path(override) if override else conf.path("models")
        try:
            if not override and not source.exists():
                source = Path(__file__).resolve().parents[2] / REGISTRY_PATH
            registry = registry_rows.parse_registry(conf_reads.read_text(source))
        except (OSError, UnicodeError):
            return False
        return model in registry.get("codex.agents", {}) and model not in registry.get("codex.families", {})
    return _claude_model_listed(model)


def _claude_model_listed(model: str) -> bool:
    for directory in config_dirs():
        for path in (directory / "cache/model-catalog").glob("*-cc.json"):
            try:
                value = cast(object, json.loads(path.read_bytes()))
            except (OSError, ValueError):
                continue
            for key in ("catalog", "config", "models"):
                value = cast(dict[str, object], value).get(key) if isinstance(value, dict) else None
            if isinstance(value, list) and any(isinstance(entry, dict)
                    and cast(dict[str, object], entry).get("id") == model for entry in cast(list[object], value)):
                return True
    return False


def model_color_value(value: str) -> bool:
    return re.fullmatch(r"#[0-9a-f]{6}", value) is not None


def append_setting(source: str, key: str, value: str) -> str:
    ending = "\n" if source and not source.endswith("\n") else ""
    return f"{source}{ending}{key}={value}\n"


@dataclass(frozen=True)
class WaitingChange:
    """A live Save that waits for publication or a Mac pull."""

    stem: str
    key: str
    value: str
    message: str
    publish_on_main: bool = True
    save_order: int = 0


class WaitingChangeRecord(TypedDict):
    stem: str
    key: str
    value: str
    message: str
    publish_on_main: NotRequired[bool]
    save_order: NotRequired[int]


def journal_path() -> Path:
    state = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local/state")
    return Path(state) / "live-settings" / "waiting.json"


@dataclass(frozen=True)
class AcceptedOrder:
    order: int


@dataclass(frozen=True)
class AcceptedSave(AcceptedOrder):
    """An accepted Save whose value is still pending."""

    value: str
    legacy_base: str | None = None


type SaveOrders = dict[str, dict[str, int]]


class AcceptedSaveRecord(TypedDict):
    order: int
    value: NotRequired[str | None]
    legacy_base: NotRequired[str | None]


class SettingsJournalRecord(TypedDict):
    last_order: int
    accepted: NotRequired[dict[str, dict[str, AcceptedSaveRecord]]]
    register: NotRequired[dict[str, dict[str, AcceptedSaveRecord]]]
    changes: list[WaitingChangeRecord]


@dataclass
class LockedJournal:
    """The accepted orders, pending values, and publication queue under the journal lock."""

    path: Path
    last_order: int
    accepted: dict[str, dict[str, AcceptedOrder]]
    changes: list[WaitingChange]
    migrating: bool = False

    def save(self) -> None:
        record = {"last_order": self.last_order, "register": self.accepted,
                  "changes": self.changes}
        write_text_atomic(self.path, json.dumps(record, default=asdict, indent=2) + "\n", new_file_mode=0o600)
        self.migrating = False

    def order(self, stem: str, key: str) -> int:
        save = self.accepted.get(stem, {}).get(key)
        return save.order if save else 0

    def accept(self, stem: str, key: str, save: AcceptedSave) -> bool:
        if 0 < self.order(stem, key) >= save.order:
            return False
        self.accepted.setdefault(stem, {})[key] = save
        self.last_order = max(self.last_order, save.order)
        return True

    def settle(self, orders: SaveOrders, legacy: dict[tuple[str, str], str | None]) -> None:
        """Reconcile the accepted and published orders."""
        changed = self._migrate(legacy)
        for stem in self.accepted.keys() | orders.keys():
            keys = self.accepted.setdefault(stem, {})
            published = orders.get(stem, {})
            for key in dict.fromkeys((*keys, *published)):
                save = keys.get(key, AcceptedOrder(0))
                order = published.get(key, 0)
                if not isinstance(save, AcceptedSave) or published_save(save, order, legacy.get((stem, key))):
                    current = AcceptedOrder(max(save.order, order))
                    changed = changed or current != keys.get(key)
                    keys[key] = current
                    self.last_order = max(self.last_order, current.order)
        if changed or self.migrating:
            self.save()

    def _migrate(self, legacy: dict[tuple[str, str], str | None]) -> bool:
        """Move old Mac receipts into the register and assign orders to old publication rows."""
        old = self.changes
        self.changes = []
        for row in old:
            if not row.publish_on_main:
                if row.save_order >= self.order(row.stem, row.key):
                    base = legacy.get((row.stem, row.key)) if not row.save_order else None
                    self.accepted.setdefault(row.stem, {})[row.key] = AcceptedSave(row.save_order, row.value, base)
                    self.last_order = max(self.last_order, row.save_order)
                continue
            if not row.save_order:
                row = replace(row, save_order=self.last_order + 1)
                _ = self.accept(row.stem, row.key, AcceptedSave(row.save_order, row.value))
            self.changes.append(row)
        return old != self.changes


def read_journal(path: Path) -> LockedJournal:
    try:
        data = cast(object, json.loads(path.read_text(encoding="utf-8")))
        if isinstance(data, list):
            return LockedJournal(path, 0, {}, [WaitingChange(**row) for row in cast(list[WaitingChangeRecord], data)])
        record = cast(SettingsJournalRecord, data)
        register = record.get("register")
        changes = [WaitingChange(**row) for row in record["changes"]]
        if register is None:
            waiting = {(row.stem, row.key, row.save_order) for row in changes}
            register = {stem: {key: AcceptedSaveRecord(order=row["order"], value=row.get("value")
                                                      if (stem, key, row["order"]) in waiting else None)
                               for key, row in keys.items()} for stem, keys in record.get("accepted", {}).items()}
        accepted = {stem: {key: AcceptedSave(row["order"], value, row.get("legacy_base"))
                           if (value := row.get("value")) is not None else AcceptedOrder(row["order"])
                           for key, row in keys.items()} for stem, keys in register.items()}
        return LockedJournal(path, record["last_order"], accepted, changes, migrating="register" not in record)
    except FileNotFoundError:
        return LockedJournal(path, 0, {}, [])
    except (ValueError, TypeError, KeyError) as error:
        raise OSError(f"cannot read settings journal {path}: {error}") from error


@contextmanager
def locked_journal(path: Path) -> Generator[LockedJournal]:
    """Serialize journal updates after any Git work has finished."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield read_journal(path)


def replace_value(source: str, index: int, value: str) -> str:
    """Keep the selected line's key, spacing, comment, and line ending."""
    lines = source.splitlines(keepends=True)
    line = lines[index]
    body = line.rstrip("\r\n")
    ending = line[len(body):]
    key, _, right = body.partition("=")
    # The equals sign precedes the value, so only whitespace can start its comment.
    parts = re.split(r"(?<=\s)#", right, maxsplit=1)
    old = parts[0]
    comment = "#" + parts[1] if len(parts) == 2 else ""
    leading = old[:len(old) - len(old.lstrip())]
    trailing = old[len(old.rstrip()):] if old.strip() else old if comment else ""
    if value.startswith("#"):
        # A hash value must touch the equals sign to avoid a comment.
        leading = ""
    lines[index] = f"{key}={leading}{value}{trailing}{comment}{ending}"
    return "".join(lines)


def git(checkout: Path, *arguments: str, input_text: str | None = None,
        environment: dict[str, str] | None = None) -> str:
    done = subprocess.run(["git", "-C", str(checkout), *arguments], input=input_text, text=True,
                          capture_output=True, env=environment,
                          pass_fds=build_folders.held_build_folder_descriptors(), check=False)
    if done.returncode:
        detail = (done.stderr or done.stdout).strip() or f"exit {done.returncode}"
        raise OSError(f"git {' '.join(arguments)}: {detail}")
    return done.stdout


def show(checkout: Path, commit: str, path: str) -> str:
    return git(checkout, "show", f"{commit}:{path}")


def waiting_files(checkout: Path) -> list[str]:
    """Tracked configuration files with a staged or unstaged change."""
    live = git(checkout, "diff", "--name-only", "-z", "HEAD", "--", WAITING_PATHS)
    staged = git(checkout, "diff", "--cached", "--name-only", "-z", "HEAD", "--", WAITING_PATHS)
    return list(dict.fromkeys(path for path in (live + staged).split("\0") if path))


@dataclass(frozen=True)
class WaitingRows:
    """The checked index entries and file text for a move to the target."""

    merge_source: str
    merge_target: str
    index_before: str
    index_after_give: str
    index_after_git: str
    entries: str
    plan: MovePlan


def live_text(path: Path) -> str | None:
    try:
        return path.read_bytes().decode("utf-8")
    except FileNotFoundError:
        return None


def write_files(checkout: Path, files: Mapping[str, str | None]) -> None:
    for name, text in files.items():
        path = checkout / name
        if text is None:
            path.unlink(missing_ok=True)
        elif live_text(path) != text:
            write_text_atomic(path, text)


def tree_text(checkout: Path, commit: str, path: str) -> str | None:
    return show(checkout, commit, path) if git(checkout, "ls-tree", commit, "--", path) else None


def tree_value(checkout: Path, commit: str, stem: str, key: str) -> str | None:
    setting = conf.settings(tree_text(checkout, commit, f"config/{stem}.conf") or "").get(key)
    return setting.value if setting else None


def published_save(save: AcceptedSave, order: int, value: str | None) -> bool:
    if save.order or order > 0:
        return order >= save.order
    return value == save.value or value != save.legacy_base


def published_orders(checkout: Path, commit: str) -> SaveOrders:
    text = tree_text(checkout, commit, ORDER_PATH)
    try:
        return cast(SaveOrders, json.loads(text)) if text else {}
    except ValueError as error:
        raise OSError(f"{commit}:{ORDER_PATH}: {error}") from error


def pending_files(checkout: Path, journal: LockedJournal) -> dict[str, dict[str, AcceptedOrder]]:
    if (checkout / "config").resolve() != conf.directory().resolve():
        return {}
    return {f"config/{stem}.conf": keys for stem, keys in journal.accepted.items()
            if any(isinstance(save, AcceptedSave) for save in keys.values())}


class RegistryEditMode(Enum):
    ORDINARY = auto()
    ADDITIVE_CATALOG = auto()
    PROTECTED_PRODUCER = auto()


# Catalog rows store the replacement line. Producer rows store the protected value.
type PendingRegistryReplay = dict[tuple[str, str], str]


def registry_writer_lock(git_directory: Path) -> Path:
    return git_directory / "models-config.publish.lock"


def _registry_replay_path(git_directory: Path) -> Path:
    return git_directory / "models-config.additive.json"


def _registry_replay_intent(git_directory: Path) -> PendingRegistryReplay:
    source = live_text(_registry_replay_path(git_directory))
    rows = cast(list[tuple[str, str, str]], json.loads(source)) if source is not None else []
    return {(section, key): line for section, key, line in rows}


def record_registry_edit(git_directory: Path, before: str, after: str, mode: RegistryEditMode) -> None:
    """Keep the replay intent for pending registry rows."""
    intent = _registry_replay_intent(git_directory)
    previous = intent.copy()
    producer_values = registry_rows.values(after) if mode == RegistryEditMode.PROTECTED_PRODUCER else {}
    for row in registry_rows.changes(before, after):
        address = row.section, row.key
        match mode:
            case RegistryEditMode.ADDITIVE_CATALOG:
                intent[address] = row.replacement
            case RegistryEditMode.PROTECTED_PRODUCER:
                intent[address] = producer_values[address]
            case RegistryEditMode.ORDINARY:
                _ = intent.pop(address, None)
    if intent != previous:
        path = _registry_replay_path(git_directory)
        if intent:
            write_text_atomic(path, json.dumps([(*address, value) for address, value in intent.items()]) + "\n")
        else:
            path.unlink(missing_ok=True)


def _registry_git_directory(checkout: Path) -> Path:
    return Path(git(checkout, "rev-parse", "--absolute-git-dir").strip())


def _protected_producer_conflicts(intent: PendingRegistryReplay, local: str, target: str) -> dict[str, str]:
    local_values = registry_rows.values(local)
    return {key: row.line for (section, key), row in registry_rows.rows(target).items()
            if section == "producers" and (value := intent.get((section, key))) is not None
            and local_values.get((section, key)) == value != row.value}


def registry_publication_text(git_directory: Path, local: str, committed: str) -> str:
    conflicts = _protected_producer_conflicts(_registry_replay_intent(git_directory), local, committed)
    edits = [registry_rows.RowChange("producers", key, line) for key, line in conflicts.items()]
    return registry_rows.replay(local, edits, False) if edits else local


def _replay_registry(git_directory: Path, base: str, local: str, target: str) -> str:
    intent = _registry_replay_intent(git_directory)
    conflicts = _protected_producer_conflicts(intent, local, target)
    ordinary: list[registry_rows.RowChange] = []
    catalog: list[registry_rows.RowChange] = []
    for row in registry_rows.changes(base, local):
        if row.section == "producers":
            if row.key not in conflicts:
                ordinary.append(row)
        elif intent.get((row.section, row.key)) == row.replacement:
            catalog.append(row)
        else:
            ordinary.append(row)
    text = registry_rows.replay(target, ordinary, False) if ordinary else target
    return registry_rows.replay(text, catalog, True) if catalog else text


def replay_registry_rows(checkout: Path, base: str, local: str, target: str) -> str:
    return _replay_registry(_registry_git_directory(checkout), base, local, target)


def _settled_saves(incoming: str | None, saves: dict[str, AcceptedOrder],
                   published: dict[str, int]) -> dict[str, int]:
    settings = conf.settings(incoming or "") if any(not save.order for save in saves.values()) else {}
    return {key: order for key, save in saves.items() if isinstance(save, AcceptedSave)
            and published_save(save, order := published.get(key, 0),
                               setting.value if not (save.order or order) and (setting := settings.get(key)) else None)}


def hold(checkout: Path, move: CheckoutMove, *, published_registry: str | None) -> MovePlan:
    """Plan writes for paths that Git must leave in place."""
    target = move.tip
    changed = changed_paths(checkout, move.head, target, WAITING_PATHS)
    orders = published_orders(checkout, target)
    plan = MovePlan(source=move.head, target=target, before={}, after={})
    forced = {REGISTRY_PATH} if published_registry is not None or REGISTRY_PATH in changed else set[str]()
    if (checkout / "config").resolve() == conf.directory().resolve():
        # A Save can arrive after preflight, even for a clean file.
        old_orders = published_orders(checkout, move.head) if orders else {}
        changed.update(f"config/{stem}.conf" for stem, keys in orders.items() if keys != old_orders.get(stem))
    else:
        changed.intersection_update(waiting_files(checkout))
    with locked_journal(journal_path()) as pending:
        managed = pending_files(checkout, pending)
        touched = changed | managed.keys() | forced
        snapshots = {path: live_text(checkout / path) for path in touched}
    for path in sorted(touched):
        stem, saves = Path(path).stem, managed.get(path, {})
        incoming = tree_text(checkout, target, path)
        settled = _settled_saves(incoming, saves, orders.get(stem, {}))
        if path not in changed and not settled and path not in forced:
            continue
        head = tree_text(checkout, move.head, path)
        local = snapshots[path]
        plan["before"][path] = local
        if path == REGISTRY_PATH and incoming is not None:
            baseline = published_registry if published_registry is not None else head or ""
            plan["after"][path] = replay_registry_rows(checkout, baseline, local or "", incoming)
        else:
            plan["after"][path] = replay_settings(path, head or "", local or "", incoming, saves, settled)
    return plan


def replay_settings(path: str, head: str, local: str, target: str | None,
                    saves: dict[str, AcceptedOrder], settled: dict[str, int]) -> str | None:
    before, incoming = conf.settings(head), conf.settings(target or "")
    lines = local.splitlines(keepends=True)
    head_lines = head.splitlines(keepends=True)
    baseline = head_lines.copy()
    target_lines = (target or "").splitlines(keepends=True)
    for key, setting in conf.settings(local).items():
        old, new = before.get(key), incoming.get(key)
        if isinstance(save := saves.get(key), AcceptedSave):
            line = lines[setting.line]
            same_save = new and new.value == save.value and settled.get(key) == save.order
            if (key not in settled or same_save) and re.search(r"[ \t]#", line.partition("=")[2]):
                if new:
                    ending = "\n" if target_lines[new.line].endswith("\n") else ""
                    target_lines[new.line] = replace_value(line, 0, save.value).rstrip("\r\n") + ending
                else:
                    target_lines.extend(("\n" if target_lines and not target_lines[-1].endswith("\n") else "",
                                         replace_value(line, 0, save.value)))
            lines[setting.line] = head_lines[old.line] if old else ""
        elif old is None and new and new.value == setting.value and (
                saves or (conf.full_model_color_key(Path(path).stem, key) and model_color_value(setting.value)
                          and lines[setting.line].rstrip("\r\n") == target_lines[new.line].rstrip("\r\n"))):
            lines[setting.line] = ""
        elif old and setting.value != old.value:
            baseline[old.line] = replace_value(baseline[old.line], 0, setting.value)
            if new and new.value == old.value:
                target_lines[new.line] = replace_value(target_lines[new.line], 0, setting.value)
    # Appending a key adds a separator when the last line has no newline.
    normalized, expected = "".join(lines), "".join(baseline)
    if normalized != expected and not ("" in lines and normalized == expected + "\n" and not expected.endswith("\n")):
        raise OSError(f"{path} has local edits other than setting values")
    if target is None and expected != head:
        raise OSError(f"the target removes a file with waiting rows: {path}")
    return "".join(target_lines) if target_lines or target is not None else None


def give(checkout: Path, rows: WaitingRows, environment: dict[str, str] | None = None) -> None:
    """Give Git the target's blobs for waiting rows and refresh the index."""
    if rows.entries:
        _ = git(checkout, "update-index", "-z", "--index-info", input_text=rows.entries, environment=environment)
    refresh_index(checkout, environment)


def refresh_index(checkout: Path, environment: dict[str, str] | None) -> None:
    # Refresh records matching files. Git checks changes during the merge.
    _ = subprocess.run(["git", "-C", str(checkout), "update-index", "-q", "--refresh"], env=environment,
                       capture_output=True, pass_fds=build_folders.held_build_folder_descriptors(), check=False)


@dataclass(frozen=True)
class CheckoutMove:
    head: str
    tip: str


class Diverged(OSError):
    """The target does not descend from the required parent."""


def move_target(checkout: Path, target: str, *, fast_forward: bool,
                allow_contained: bool = False, expected_head: str = "") -> CheckoutMove:
    """Check the target against HEAD while the caller holds the checkout lock."""
    if Path(git(checkout, "rev-parse", "--path-format=absolute", "--git-path", "MERGE_HEAD").strip()).exists():
        raise OSError("an unfinished merge must be completed first")
    if git(checkout, "ls-files", "--unmerged").strip():
        raise OSError("unmerged index entries must be resolved first")
    head = git(checkout, "rev-parse", "HEAD").strip()
    restore_checkout(checkout, head)
    if expected_head and head != expected_head:
        raise Diverged("HEAD changed while the publisher prepared the move")
    tip = head if target == "HEAD" else git(checkout, "rev-parse", f"{target}^{{commit}}").strip()
    if head == tip:
        return CheckoutMove(head, head)
    if fast_forward:
        result = subprocess.run(["git", "-C", str(checkout), "merge-base", head, tip],
                                capture_output=True, text=True, check=False)
        ancestor = result.stdout.strip()
        if allow_contained and ancestor == tip:
            return CheckoutMove(head, head)
        if ancestor != head:
            raise Diverged(f"{target} is not a fast-forward of HEAD")
    return CheckoutMove(head, tip)


def move_marker(checkout: Path) -> Path:
    return Path(git(checkout, "rev-parse", "--path-format=absolute", "--git-path", "waiting-rows.move").strip())


class FileVersion(TypedDict):
    device: int
    inode: int
    changed_ns: int


def file_version(path: Path) -> FileVersion | None:
    try:
        state = path.lstat()
    except (FileNotFoundError, NotADirectoryError):
        return None
    return FileVersion(device=state.st_dev, inode=state.st_ino, changed_ns=state.st_ctime_ns)


class MoveWrites(TypedDict):
    index_before: str
    expected_index: str
    files_before: dict[str, FileVersion | None]
    index_after_give: NotRequired[str]
    index_after_git: NotRequired[str]
    files_written_by_git: NotRequired[dict[str, FileVersion | None]]


class MovePlan(TypedDict):
    source: str
    target: str
    before: dict[str, str | None]
    after: dict[str, str | None]
    before_index_write: NotRequired[Literal[True]]
    writes: NotRequired[MoveWrites]


def changed_paths(checkout: Path, before: str, after: str, *paths: str) -> set[str]:
    if before == after:
        return set()
    return set(git(checkout, "diff", "--name-only", "--no-renames", "-z",
                   before, after, "--", *paths).split("\0")) - {""}


def index_path(checkout: Path) -> Path:
    return Path(git(checkout, "rev-parse", "--path-format=absolute", "--git-path", "index").strip())


def staged_paths(checkout: Path, tree: str) -> set[str]:
    return set(git(checkout, "diff", "--cached", "--name-only", "--no-renames", "-z",
                   tree, "--").split("\0")) - {""}


def worktree_edit(path: str) -> OSError:
    return OSError(f"{path} changed after the move started. Inspect it with git diff HEAD -- {shlex.quote(path)}")


def staged_overlap(path: str) -> OSError:
    return OSError(f"{path}: staged edit overlaps the interrupted move. "
                   + f"Inspect it with git diff --cached -- {shlex.quote(path)}")


def recover_move(checkout: Path, head: str) -> None:
    """Recover only changes that the move recorded."""
    marker = move_marker(checkout)
    if (record := live_text(marker)) is None:
        return
    plan = cast(MovePlan, json.loads(record))
    if head == "HEAD":
        head = git(checkout, "rev-parse", "HEAD").strip()
    if head == plan["source"] and plan.get("before_index_write"):
        marker.unlink()
        return
    if head not in (plan["source"], plan["target"]):
        raise OSError("HEAD is outside the pending settings move")

    if (writes := plan.get("writes")) is None:
        check_move_without_receipt(checkout, plan, head)
    elif head == plan["source"]:
        rollback_move(checkout, plan, writes)
    else:
        settings = plan["after"].keys() - {REGISTRY_PATH}
        target_orders = published_orders(checkout, head) if settings else {}
        target_settings = {path: tree_text(checkout, head, path) for path in settings}
        registry_dir = _registry_git_directory(checkout) if REGISTRY_PATH in plan["after"] else None
        restored = dict(plan["after"])
        with ExitStack() as locks:
            if registry_dir is not None:
                _ = locks.enter_context(file_lock.exclusive(registry_writer_lock(registry_dir)))
            pending = locks.enter_context(locked_journal(journal_path()))
            saves = pending_files(checkout, pending)
            for path, after in restored.items():
                current = live_text(checkout / path)
                before = plan["before"][path]
                if current == after or (current == before and file_version(checkout / path) == writes["files_before"][path]):
                    continue
                if path == REGISTRY_PATH and registry_dir is not None and current is not None:
                    restored[path] = _replay_registry(registry_dir, before or "", current, after or "")
                    continue
                keys = saves.get(path)
                if not keys or current != apply_accepted_values(before or "", keys):
                    raise worktree_edit(path)
                settled = _settled_saves(target_settings[path], keys, target_orders.get(Path(path).stem, {}))
                waiting = {key: save for key, save in keys.items()
                           if isinstance(save, AcceptedSave) and key not in settled}
                if waiting:
                    restored[path] = apply_accepted_values(after or "", waiting)
            write_files(checkout, restored)
    marker.unlink()


def check_move_without_receipt(checkout: Path, plan: MovePlan, head: str) -> None:
    """Keep any path that an older marker cannot account for."""
    if head == plan["source"]:
        paths = changed_paths(checkout, head, plan["target"]) | plan["before"].keys()
        if staged := staged_paths(checkout, head) & paths:
            raise staged_overlap(min(staged))
        dirty = set(git(checkout, "diff", "--name-only", "-z", head, "--").split("\0"))
        for path in sorted(dirty & paths):
            if path not in plan["before"] or live_text(checkout / path) != plan["before"][path]:
                raise worktree_edit(path)
    else:
        for path, after in plan["after"].items():
            if live_text(checkout / path) != after:
                raise worktree_edit(path)


def index_entry(checkout: Path, tree: str, path: str) -> str:
    return git(checkout, "--literal-pathspecs", "ls-tree", "-z", tree, "--", path)


def merge_index_entry(checkout: Path, current: str, base: str, other: str, path: str) -> str:
    """Keep the staged edit when Git can merge it with the move."""
    if current in (base, other):
        return other
    if base == other:
        return current
    current_info, base_info, other_info = (entry.split("\t", 1)[0].split() for entry in (current, base, other))
    if (len(current_info) != 3 or len(base_info) != 3 or len(other_info) != 3
            or current_info[:2] != base_info[:2] or base_info[:2] != other_info[:2]
            or base_info[0] not in ("100644", "100755")):
        raise staged_overlap(path)
    try:
        blob = git(checkout, "merge-file", "--object-id", "--quiet",
                   current_info[2], base_info[2], other_info[2]).strip()
    except OSError:
        raise staged_overlap(path) from None
    return f"{base_info[0]} blob {blob}\t{path}\0"


def index_changes(tree: str, entries: Mapping[str, str]) -> str:
    return "".join(entry or f"0 {'0' * len(tree)}\t{path}\0" for path, entry in entries.items())


def write_index_tree(checkout: Path, tree: str, entries: Mapping[str, str],
                     environment: dict[str, str]) -> str:
    _ = git(checkout, "read-tree", tree, environment=environment)
    _ = git(checkout, "update-index", "-z", "--index-info",
            input_text=index_changes(tree, entries), environment=environment)
    return git(checkout, "write-tree", environment=environment).strip()


def rollback_move(checkout: Path, plan: MovePlan, writes: MoveWrites) -> None:
    """Reverse the recorded index changes and any files that Git wrote."""
    with tempfile.TemporaryDirectory(prefix="waiting-rows-rollback-") as directory:
        index = Path(directory) / "index"
        trial = {**os.environ, "GIT_INDEX_FILE": str(index)}
        before = writes["index_before"]
        gave = writes.get("index_after_give", before)
        written = writes.get("index_after_git", gave)
        git_index.private_copy(index_path(checkout), index)
        current = git(checkout, "write-tree", environment=trial).strip()
        if "index_after_git" not in writes:
            possible = changed_paths(checkout, written, writes["expected_index"]) & writes["files_before"].keys()
            if uncertain := changed_paths(checkout, written, current) & possible:
                path = min(uncertain)
                raise OSError(f"{path}: the move did not record the index write. "
                              + f"Inspect it with git diff --cached -- {shlex.quote(path)}")
        entries: dict[str, str] = {}
        for path in sorted(changed_paths(checkout, before, written)):
            original = index_entry(checkout, before, path)
            staged = index_entry(checkout, current, path)
            if staged != original:
                entries[path] = merge_index_entry(checkout, staged, index_entry(checkout, written, path), original, path)

        files = writes.get("files_written_by_git", {})
        for path, version in writes["files_before"].items():
            current_version = file_version(checkout / path)
            if path in files:
                if current_version != files[path]:
                    raise worktree_edit(path)
            elif path == REGISTRY_PATH and path in plan["before"]:
                continue
            elif current_version != version:
                if path in plan["before"]:
                    before_text = plan["before"][path]
                    current_text = live_text(checkout / path)
                    if current_text == before_text:
                        continue
                    pending = read_journal(journal_path())
                    keys = pending_files(checkout, pending).get(path)
                    if keys and current_text == apply_accepted_values(before_text or "", keys):
                        continue
                raise worktree_edit(path)

        restored_index = write_index_tree(checkout, current, entries, trial) if entries else current
        if files:
            restored = {path: index_entry(checkout, gave, path) for path in files}
            worktree_before = write_index_tree(checkout, written, restored, trial)
            _ = git(checkout, "read-tree", written, environment=trial)
            refresh_index(checkout, trial)
            _ = git(checkout, "read-tree", "-m", "-u", written, worktree_before, environment=trial)
        if restored_index != current:
            _ = git(checkout, "read-tree", "-i", "-m", current, restored_index)


def tree_without(checkout: Path, tree: str, paths: Iterable[str], environment: dict[str, str]) -> str:
    return write_index_tree(checkout, tree, dict.fromkeys(paths, ""), environment)


def check_move(checkout: Path, move: CheckoutMove, *,
               registry_move: bool = False,
               published_registry: str | None = None) -> WaitingRows:
    """Check the move on a private index before Git changes the checkout."""
    index = index_path(checkout)
    folder = (build_folders.tool_folder("models-ff-index") if registry_move
              else tempfile.TemporaryDirectory(prefix="waiting-rows-index-"))
    with folder as directory:
        trial = Path(directory) / "index"
        environment = {**os.environ, "GIT_INDEX_FILE": str(trial)}
        git_index.private_copy(index, trial)
        index_before = git(checkout, "write-tree", environment=environment).strip()
        staged = changed_paths(checkout, move.head, index_before, WAITING_PATHS)
        plan = hold(checkout, move, published_registry=published_registry)
        target_entries = {path: index_entry(checkout, move.tip, path) for path in plan["after"]}
        merged_entries: dict[str, str] = {}
        for path in sorted(staged & target_entries.keys()):
            try:
                merged_entries[path] = merge_index_entry(checkout, index_entry(checkout, index_before, path),
                                                         index_entry(checkout, move.head, path), target_entries[path], path)
            except OSError:
                raise OSError(f"staged settings prevent the move: {path}. "
                              + f"Inspect it with git diff --cached -- {shlex.quote(path)}") from None
        target = write_index_tree(checkout, move.tip, merged_entries, environment) if merged_entries else move.tip
        target_entries.update(merged_entries)
        removed = [path for path, entry in target_entries.items() if not entry]
        source = tree_without(checkout, move.head, removed, environment) if removed else move.head
        entries = index_changes(target, target_entries)
        git_index.private_copy(index, trial)
        rows = WaitingRows(source, target, index_before, index_before, index_before, entries, plan)
        give(checkout, rows, environment)
        gave = git(checkout, "write-tree", environment=environment).strip()
        _ = git(checkout, "read-tree", "--dry-run", "-m", "-u", source, target, environment=environment)
        _ = git(checkout, "read-tree", "-i", "-m", source, target, environment=environment)
        written = git(checkout, "write-tree", environment=environment).strip()
    return replace(rows, index_after_give=gave, index_after_git=written)


def apply_move(checkout: Path, rows: WaitingRows) -> None:
    """Move HEAD and restore settings while the caller holds the checkout lock."""
    plan = rows.plan
    source, target = plan["source"], plan["target"]
    marker = move_marker(checkout)
    paths = changed_paths(checkout, source, target) | plan["before"].keys()
    before = {path: file_version(checkout / path) for path in paths}
    writes = MoveWrites(index_before=rows.index_before, expected_index=rows.index_after_git, files_before=before)
    write_text_atomic(marker, json.dumps({**plan, "before_index_write": True}) + "\n", new_file_mode=0o600)
    if changed := staged_paths(checkout, rows.index_before):
        raise staged_overlap(min(changed))
    plan["writes"] = writes
    try:
        write_text_atomic(marker, json.dumps(plan) + "\n")
        give(checkout, rows)
        writes["index_after_give"] = rows.index_after_give
        write_text_atomic(marker, json.dumps(plan) + "\n")
        _ = git(checkout, "read-tree", "-m", "-u", rows.merge_source, rows.merge_target)
        writes["index_after_git"] = rows.index_after_git
        written_paths = changed_paths(checkout, rows.index_after_give, rows.index_after_git)
        writes["files_written_by_git"] = {
            path: version for path in (written_paths & before.keys()) - plan["before"].keys()
            if (version := file_version(checkout / path)) != before[path]
        }
        write_text_atomic(marker, json.dumps(plan) + "\n")
        _ = git(checkout, "update-ref", "-m", f"waiting_rows: move to {target}", "HEAD", target, source)
    finally:
        restore_checkout(checkout, "HEAD")


@contextmanager
def locked_checkout(checkout: Path, *, wait: bool = True) -> Generator[None]:
    """Serialize moves of the checkout index and HEAD."""
    path = move_marker(checkout).with_suffix(".lock")
    held = _CHECKOUT_LOCKS.get()
    if path in held:
        yield
        return
    with path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        token = _CHECKOUT_LOCKS.set(held | {path})
        try:
            yield
        finally:
            _CHECKOUT_LOCKS.reset(token)


def recover_abandoned_move(checkout: Path) -> None:
    """Recover an interrupted move if no checkout worker holds its lock."""
    marker = move_marker(checkout)
    if not marker.exists() or marker.with_suffix(".lock") in _CHECKOUT_LOCKS.get():
        return
    with ExitStack() as locks:
        try:
            locks.enter_context(locked_checkout(checkout, wait=False))
        except BlockingIOError:
            return
        restore_checkout(checkout, "HEAD")


def move_to(checkout: Path, target: str, *, fast_forward: bool = False, expected_head: str = "") -> None:
    with locked_checkout(checkout):
        move = move_target(checkout, target, fast_forward=fast_forward, expected_head=expected_head)
        if move.head != move.tip:
            apply_move(checkout, check_move(checkout, move))


def restore_checkout(checkout: Path, head: str) -> None:
    """Recover the checkout and restore the live values under the checkout lock."""
    recover_move(checkout, head)
    if (checkout / "config").resolve() != conf.directory().resolve():
        return
    orders, legacy = checkout_orders(checkout, head)
    with locked_journal(journal_path()) as pending:
        pending.settle(orders, legacy)
        files: dict[str, str] = {}
        for relative, keys in pending_files(checkout, pending).items():
            files[relative] = apply_accepted_values(live_text(checkout / relative) or "", keys)
        write_files(checkout, files)


def checkout_orders(checkout: Path, head: str) -> tuple[SaveOrders, dict[tuple[str, str], str | None]]:
    """Read published orders and legacy values before taking the journal lock."""
    orders = published_orders(checkout, head)
    snapshot = read_journal(journal_path())
    old_keys = {(stem, key) for stem, keys in snapshot.accepted.items() for key, save in keys.items()
                if isinstance(save, AcceptedSave) and not save.order and not orders.get(stem, {}).get(key)}
    old_keys.update((row.stem, row.key) for row in snapshot.changes if not row.publish_on_main and not row.save_order)
    legacy = {(stem, key): tree_value(checkout, head, stem, key) for stem, key in old_keys}
    return orders, legacy


def apply_accepted_values(contents: str, keys: Mapping[str, AcceptedOrder]) -> str:
    for key, save in keys.items():
        if isinstance(save, AcceptedSave):
            setting = conf.settings(contents).get(key)
            contents = (replace_value(contents, setting.line, save.value) if setting
                        else append_setting(contents, key, save.value))
    return contents


def restore_live(checkout: Path) -> None:
    with locked_checkout(checkout):
        restore_checkout(checkout, "HEAD")


def main(arguments: list[str] | None = None) -> int:
    """Fast-forward the checkout in the current folder: `waiting_rows.py fast-forward <tip>`."""
    argv = sys.argv[1:] if arguments is None else arguments
    if len(argv) != 2 or argv[0] != "fast-forward":
        print("usage: waiting_rows.py fast-forward <tip>", file=sys.stderr)
        return 2
    checkout = Path.cwd()
    try:
        move_to(checkout, argv[1], fast_forward=True)
    except (OSError, UnicodeError) as error:
        print(f"waiting_rows: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
