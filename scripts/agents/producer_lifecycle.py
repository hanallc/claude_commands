"""Create, rename, retire and restore a producer."""
from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import cast
from urllib.parse import quote, unquote
from uuid import uuid4

from scripts.agents import pane_record, producer_creation, remote, restart, restart_support, shutdown_record, stop_resume
from scripts.herdr import herdr, pane_launch
from scripts.herdr.place import HerdrPlace
from scripts.lib import atomic_file, markdown_links, models_registry, session_move, transcripts
from scripts.message import roster, session_records
from scripts.production import staff, the_list, update_registration

TEMPLATE = Path(__file__).resolve().parents[2] / "templates/producer/README.md"
RETIRE_SOURCE = "agents retire"


def _path(operation: str, key: str) -> Path:
    return pane_record.record_directory() / f".producer-{operation}-{quote(key, safe='')}"


def _read(path: Path) -> dict[str, object]:
    try:
        value = cast(object, json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return {}
    if not isinstance(value, dict) or not value:
        raise ValueError(f"{path}: invalid producer operation")
    return cast(dict[str, object], value)


def _write(path: Path, values: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_file.write_text_atomic(path, json.dumps(values, sort_keys=True) + "\n")


def _text(values: Mapping[str, object], key: str) -> str:
    value = values.get(key)
    if not isinstance(value, str):
        raise ValueError(f"the producer operation lacks {key}")
    return value


def _producer_rows(record: staff.StaffSnapshot, name: str) -> list[staff.StaffRow]:
    return [row for row in record.all_rows if row.kind == "producer" and name in (row.name, row.id, row.current_session)]


def lookup(name: str, record: staff.StaffSnapshot) -> staff.StaffRow | None:
    matches = _producer_rows(record, name)
    if len(matches) > 1:
        raise staff.StaffRefused(f"{name}: several producers match; use a staff id")
    return matches[0] if matches else None


def pending_rename(old: str) -> bool:
    return _path("rename", old).exists()


def _require_free_name(name: str, record: staff.StaffSnapshot, *,
                       own: staff.StaffRow | None = None, pane: str = "") -> None:
    key = name.casefold()
    if any(row.name.casefold() == key and (own is None or row.id != own.id) for row in record.all_rows):
        raise ValueError(f"{name}: another staff row has this name")
    report = roster.roster(os.environ, roster.AllMachines(), membership=record)
    for row in report.entries:
        names = {row.name.casefold()}
        if isinstance(row.registry_name, roster.RegistryName):
            names.add(row.registry_name.value.casefold())
        if key not in names:
            continue
        if own is not None and (row.session == roster.ClaudeSession(own.current_session)
                                or row.thread == roster.CodexThread(own.current_session)):
            continue
        if pane and row.machine == remote.local_machine() and isinstance(row.pane, HerdrPlace) and row.pane.pane == pane:
            continue
        raise ValueError(f"{name}: a live session has this name")
    if report.problems:
        raise ValueError(f"{name}: cannot check live session names: {report.problems[0]}")


def _print_started(row: staff.StaffRow, account: str, tab: str) -> None:
    print(f"{row.name}: staff {row.id}, account {account}, tab {tab}")


def _new_profile(model: str, effort: str) -> models_registry.ProducerProfile:
    default = models_registry.producer_defaults()
    family, separator, name = model.partition(":")
    if separator:
        if family not in ("claude", "codex") or not name:
            raise ValueError("--model must name a model or use FAMILY:MODEL")
        return models_registry.ProducerProfile(family, name, effort or default.effort)
    return models_registry.ProducerProfile(default.family, model or default.model, effort or default.effort)


def _create_readme(path: Path, text: str) -> None:
    if path.is_file():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if not atomic_file.create_text_atomic(path, text) and not path.is_file():
        raise FileExistsError(f"{path}: the README path is occupied")


def add(name: str, *, model: str = "", effort: str = "", job: str = "") -> int:
    """Save the creation request before it changes the registry or document."""
    staff.validate_producer_name(name)
    staff.require_no_producer_rename(name)
    if len(job.splitlines()) > 1 or "\n" in job or "\r" in job:
        raise ValueError("--job must contain one line")
    folder = stop_resume.producer_folder(name)
    path = producer_creation.record_path(name)
    with producer_creation.operation(name):
        staff.require_no_producer_rename(name)
        record = staff.initial_snapshot()
        matches = [row for row in record.find(kind="producer") if row.name == name]
        if len(matches) > 1:
            raise ValueError(f"{name}: several producer rows have this name")
        own = matches[0] if matches else None
        saved = producer_creation.read_record(path)
        new_record = not saved
        current_marker = producer_creation.require_record(name, saved)
        if saved:
            folder = Path(_text(saved, "folder"))
            profile = models_registry.parse_producer_profile(_text(saved, "profile"))
            if (model and model not in (profile.model, f"{profile.family}:{profile.model}")
                    or effort and effort != profile.effort or job and job != _text(saved, "job")):
                raise ValueError(f"{name}: retry the creation with its original options")
            if "staff" in saved:
                if own is None or own.id != _text(saved, "staff"):
                    raise ValueError(f"{name}: the saved creation belongs to another staff state")
                if current_marker is None and isinstance(saved.get("opening_confirmed"), str):
                    _print_started(own, _text(saved, "account"), _text(saved, "tab"))
                    return 0
            if own is not None and (own.doc != staff.LiveDoc(remote.local_machine(), folder)
                                    or own.worktree != staff.PresentWorktree(remote.local_machine(), folder)):
                raise ValueError(f"{name}: another staff row has this name")
            reservation = _read(stop_resume.producer_admission_record(name))
            _require_free_name(name, record, own=own, pane=_text(reservation, "pane") if reservation else "")
        else:
            _require_free_name(name, record)
            profile = _new_profile(model, effort)
            _ = models_registry.pending_producer_settings(profile)
            template = TEMPLATE.read_text(encoding="utf-8")
            values = {"name": name, "job": job}
            readme = re.sub(r"\{(name|job)\}", lambda match: values[match[1]], template)
            saved = {"profile": profile.value(), "job": job, "readme": readme, "folder": str(folder),
                     "opening_since": datetime.now(timezone.utc).isoformat(), "creation_id": uuid4().hex}
            producer_creation.write_record(path, saved)
        if current_marker is None and not isinstance(saved.get("opening_confirmed"), str):
            try:
                producer_creation.claim(name, producer_creation.marker_for(saved), profile)
            except staff.StaffRefused:
                if new_record:
                    path.unlink()
                    atomic_file.sync_folder(path.parent)
                raise
        _ = os.environ.pop(models_registry.OPERATION_ENVIRONMENT[0], None)
        _create_readme(folder / "README.md", _text(saved, "readme"))
        request = stop_resume.ProducerFirstStart(name, folder)
        result = stop_resume._locked_first_producer_resume(request, to="")  # pyright: ignore[reportPrivateUsage]
        if not isinstance(result, stop_resume.ClaudeUp):
            raise ValueError(result.reason if isinstance(result, stop_resume.ResumeFailed) else f"{name}: first start failed")
        admitted = staff.initial_snapshot().by_current_session(result.session_id)
        if admitted is None or admitted.name != name or admitted.kind != "producer":
            raise ValueError(f"{name}: the first start did not record the producer")
        producer_creation.write_record(path, {**producer_creation.read_record(path), "staff": admitted.id,
                                               "account": result.account, "tab": result.pane_id})
        _print_started(admitted, result.account, result.pane_id)
        return 0


def _require_local(row: staff.StaffRow) -> None:
    machine = stop_resume.member_machine(row)
    if machine and machine != remote.local_machine():
        raise ValueError(f"{row.name}: run the command on {machine}")


def pending_admission(name: str) -> bool:
    return (bool(name) and Path(name).name == name and name not in {".", ".."}
            and stop_resume.producer_admission_record(name).exists())


def _require_no_admission(name: str) -> None:
    if pending_admission(name):
        raise ValueError(f"{name}: producer admission is pending. Run agents resume {name} first.")


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("invalid producer rename record")
    return cast(dict[str, object], value)


def _entries(saved: Mapping[str, object], key: str) -> list[dict[str, object]]:
    values = saved.get(key)
    if not isinstance(values, list):
        raise ValueError(f"invalid producer rename {key}")
    return [_object(value) for value in cast(list[object], values)]


def _identity(path: Path, *, follow_symlinks: bool = False) -> tuple[int, int] | None:
    try:
        observed = path.stat(follow_symlinks=follow_symlinks)
    except FileNotFoundError:
        return None
    return observed.st_dev, observed.st_ino


def _plan_move(source: Path, destination: Path) -> dict[str, object]:
    identity = _identity(source)
    if identity is None or _identity(destination) is not None:
        raise ValueError(f"{source}: missing source or occupied destination {destination}")
    if destination.parent.is_symlink():
        raise ValueError(f"{destination.parent}: the move destination cannot be a symbolic link")
    return {"source": str(source), "destination": str(destination),
            "device": identity[0], "inode": identity[1]}


@contextmanager
def _open_move_parent(path: Path) -> Generator[int]:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        yield fd
    finally:
        os.close(fd)


def _move_one(move: Mapping[str, object], *, apply: bool, links: Mapping[Path, ClaudeLinkChange]) -> None:
    source = Path(_text(move, "source"))
    destination = Path(_text(move, "destination"))
    if source.parent.is_symlink() or destination.parent.is_symlink():
        raise ValueError(f"{source}: a move parent became a symbolic link")
    expected = move["device"], move["inode"]
    found_source, found_destination = _identity(source), _identity(destination)
    if found_source == expected and found_destination is None:
        if not apply:
            return
        destination.parent.mkdir(exist_ok=True)
        if destination.parent != source.parent:
            atomic_file.sync_folder(destination.parent.parent)
        with _open_move_parent(source.parent) as source_parent:
            target_directory = (nullcontext(source_parent) if destination.parent == source.parent
                                else _open_move_parent(destination.parent))
            with target_directory as target_parent:
                observed = session_move.stat_at(source_parent, source.name)
                if observed is None or (observed.st_dev, observed.st_ino) != expected:
                    raise ValueError(f"{source}: move source changed")
                session_move.rename_exclusive(source_parent, source.name, target_parent, destination.name)
    else:
        link = links.get(destination)
        if not (apply and found_source is None
                and (found_destination == expected or link is not None and _retargeted_link(link))):
            raise ValueError(f"{source}: folder move state changed at {destination}")
        atomic_file.sync_folder(source.parent)
        if destination.parent != source.parent:
            atomic_file.sync_folder(destination.parent)
            atomic_file.sync_folder(destination.parent.parent)


def _claude_moves(row: staff.StaffRow, source: Path, destination: Path) -> tuple[
        list[dict[str, object]], list[ClaudeLinkChange], list[dict[str, object]]]:
    homes = set(session_records.config_dirs())
    if isinstance(row.home, staff.SessionHome):
        homes.add(row.home.path)
    roots = sorted({(home / "projects").resolve() for home in homes if (home / "projects").is_dir()})
    old_key, new_key = transcripts.claude_project_key(source), transcripts.claude_project_key(destination)
    session = str(row.current_session)
    names = (f"{session}.jsonl", f"{session}.jsonl.zst", session)
    locations: dict[Path, Path] = {}
    projects: dict[Path, Path] = {}
    aliases: dict[Path, Path] = {}
    proofs: list[dict[str, object]] = []
    for root in roots:
        old_project, new_project = root / old_key, root / new_key
        if old_project.is_symlink():
            aliases[old_project] = new_project
            continue
        projects[old_project] = new_project
        for name in names:
            original = old_project / name
            identity = _identity(original)
            if identity is None:
                continue
            if name != session and original.is_file():
                target = _identity(original, follow_symlinks=True)
                if target is not None:
                    proofs.append({"source": str(original), "destination": str(new_project / name),
                                   "device": target[0], "inode": target[1]})
            if old_key != new_key:
                locations[original] = new_project / name
    if not proofs:
        raise ValueError(f"{source}: current Claude transcript is absent from its project")
    moves = [_plan_move(original, target) for original, target in locations.items()]
    links: list[ClaudeLinkChange] = []
    if old_key != new_key:
        for alias, end in aliases.items():
            target = projects.get(alias.resolve())
            if target is None:
                raise ValueError(f"{alias}: Claude project link points outside the rename plan")
            if not any(path.parent == target for path in locations.values()):
                continue
            moves.append(_plan_move(alias, end))
            links.append(_plan_link(alias, end, target))
    for root in roots:
        for name in names:
            for path in root.glob(f"*/{name}"):
                if not path.is_symlink() or path.parent.is_symlink():
                    continue
                target = (path.parent / os.readlink(path)).absolute()
                target = Path(os.path.normpath(target))
                target = target.parent.resolve() / target.name
                if target not in locations:
                    continue
                end = locations.get(path, path)
                links.append(_plan_link(path, end, locations[target]))
    return moves, [link for link in links if link.before != link.after], proofs


def _verify_claude_transcripts(proofs: list[dict[str, object]], *, moved: bool) -> None:
    for proof in proofs:
        path = Path(_text(proof, "destination" if moved else "source"))
        if not path.is_file() or _identity(path, follow_symlinks=True) != (proof["device"], proof["inode"]):
            raise ValueError(f"{path}: the current Claude transcript changed during rename")


@dataclass(frozen=True)
class ClaudeLinkChange:
    """The original symlink and the target to publish at its planned path."""

    source: Path
    path: Path
    backup: Path
    before: str
    after: str
    device: int
    inode: int

    def to_json(self) -> dict[str, object]:
        return {"source": str(self.source), "path": str(self.path), "backup": str(self.backup),
                "before": self.before, "after": self.after, "device": self.device, "inode": self.inode}

    @classmethod
    def from_json(cls, entry: Mapping[str, object]) -> ClaudeLinkChange:
        source, path, backup = (Path(_text(entry, key)) for key in ("source", "path", "backup"))
        device, inode = entry["device"], entry["inode"]
        if backup.parent != path.parent or not isinstance(device, int) or not isinstance(inode, int):
            raise ValueError("invalid Claude link change")
        return cls(source, path, backup, _text(entry, "before"), _text(entry, "after"), device, inode)


def _plan_link(source: Path, destination: Path, target: Path) -> ClaudeLinkChange:
    identity = source.lstat()
    before = os.readlink(source)
    after = str(target) if Path(before).is_absolute() else os.path.relpath(target, destination.parent)
    backup = destination.with_name(f".producer-link-{uuid4().hex}")
    return ClaudeLinkChange(source, destination, backup, before, after, identity.st_dev, identity.st_ino)


def _original_link_at(parent: int, name: str, link: ClaudeLinkChange) -> bool:
    evidence = session_move.stat_at(parent, name)
    return (evidence is not None and stat.S_ISLNK(evidence.st_mode)
            and (evidence.st_dev, evidence.st_ino) == (link.device, link.inode)
            and os.readlink(name, dir_fd=parent) == link.before)


def _retargeted_link(link: ClaudeLinkChange) -> bool:
    if link.path.is_symlink() and os.readlink(link.path) == link.after:
        return True
    if _identity(link.path) is not None:
        return False
    with _open_move_parent(link.path.parent) as parent:
        return _original_link_at(parent, link.backup.name, link)


def _verify_link_before_move(link: ClaudeLinkChange) -> None:
    with _open_move_parent(link.source.parent) as parent:
        if not _original_link_at(parent, link.source.name, link) or _identity(link.backup) is not None:
            raise ValueError(f"{link.source}: the Claude link or its backup path changed during rename")


def _retarget_link(link: ClaudeLinkChange) -> None:
    path, backup = link.path, link.backup
    with _open_move_parent(path.parent) as parent:
        current = session_move.stat_at(parent, path.name)
        installed = (current is not None and stat.S_ISLNK(current.st_mode)
                     and os.readlink(path.name, dir_fd=parent) == link.after)
        backed_up = session_move.stat_at(parent, backup.name) is not None
        if backed_up and not _original_link_at(parent, backup.name, link):
            raise ValueError(f"{backup}: the saved Claude link changed during rename")
        if not installed:
            if not backed_up:
                if not _original_link_at(parent, path.name, link):
                    raise ValueError(f"{path}: the Claude link changed during rename")
                session_move.rename_exclusive(parent, path.name, parent, backup.name)
                if not _original_link_at(parent, backup.name, link):
                    session_move.rename_exclusive(parent, backup.name, parent, path.name)
                    raise ValueError(f"{path}: the Claude link changed during rename")
            os.symlink(link.after, path.name, dir_fd=parent)
        os.fsync(parent)
        if _original_link_at(parent, backup.name, link):
            os.unlink(backup.name, dir_fd=parent)
            os.fsync(parent)


def _planned_document(row: staff.StaffRow, before: staff.PresentWorktree,
                      after: staff.PresentWorktree) -> staff.LiveDoc | staff.WrappedDoc | staff.DocUnfilled:
    doc = row.doc
    if isinstance(doc, staff.DocUnfilled) or remote.machine_alias(doc.machine) != remote.machine_alias(before.machine):
        return doc
    return staff.plan_producer_rename_doc(doc, before.path, after.path)


def _rename_plan(row: staff.StaffRow, new: str, profile: models_registry.ProducerProfile) -> dict[str, object]:
    source = stop_resume.producer_folder(row.name).absolute()
    destination = source.with_name(new)
    moves: list[dict[str, object]] = []
    links: list[ClaudeLinkChange] = []
    proofs: list[dict[str, object]] = []
    after = replace(row, name=new)
    if isinstance(row.worktree, staff.PresentWorktree) and row.worktree.path == source:
        if not source.is_dir() or source.is_symlink():
            raise ValueError(f"{source}: the producer folder is not a directory")
        moves.append(_plan_move(source, destination))
        worktree = replace(row.worktree, path=destination)
        after = replace(after, worktree=worktree, doc=_planned_document(row, row.worktree, worktree))
        if profile.family == "claude":
            claude_moves, links, proofs = _claude_moves(row, source, destination)
            moves.extend(claude_moves)
    paths = tuple(Path(_text(entry, field)) for entry in moves for field in ("source", "destination"))
    paths += tuple(path for link in links for path in (link.source, link.path, link.backup))
    paths += tuple(Path(_text(proof, "source")) for proof in proofs if proof["source"] == proof["destination"])
    plan = staff.ProducerRename(uuid4().hex, row, after, paths)
    return {"plan": plan.to_json(), "backend": profile.family, "moves": moves, "links": [link.to_json() for link in links],
            "claude_transcripts": proofs, "creation": producer_creation.plan_completed_record(row.name, row),
            "launches": {}, "vault": {}, "started": False}


def _rewrite_launches(path: Path, saved: dict[str, object], plan: staff.ProducerRename) -> None:
    before, after = plan.before.worktree, plan.after.worktree
    if not isinstance(before, staff.PresentWorktree) or not isinstance(after, staff.PresentWorktree) or before == after:
        return
    journal = _object(saved["launches"])
    filenames = set(pane_record.record_directory().glob("*.json")) | {Path(name) for name in journal}
    for runtime in sorted(filenames):
        with pane_launch.locked(runtime):
            entries = pane_launch.read_entries(runtime)
            if str(runtime) not in journal:
                raw = {pane: _object(value) for pane, value in entries.items()}
                panes = [pane for pane, value in raw.items() if value.get("session_id") == plan.before.current_session
                         and not value.get("staff_id") and value.get("cwd") == str(before.path)]
                if not panes:
                    continue
                journal[str(runtime)] = panes
                _write(path, saved)
            panes = journal[str(runtime)]
            if not isinstance(panes, list) or any(not isinstance(pane, str) for pane in cast(list[object], panes)):
                raise ValueError(f"{runtime}: invalid launch journal")
            for pane in cast(list[str], panes):
                entry = _object(entries.get(pane))
                if (entry.get("session_id") != plan.before.current_session or entry.get("staff_id")
                        or entry.get("cwd") not in (str(before.path), str(after.path))):
                    raise ValueError(f"{runtime}: legacy launch changed for {pane}")
                entry["cwd"] = str(after.path)
            atomic_file.write_text_atomic(runtime, json.dumps(entries, indent=2, sort_keys=True) + "\n")


_WIKI_LINK = re.compile(r"\[\[([^\]\r\n]+)\]\]")
_INLINE_DESTINATION = re.compile(
    r"\]\([ \t]*(?:(?:\r\n?|\n)[ \t]*)?" +
    r"(?:<(?P<angle>[^>\r\n]+)>|(?P<bare>(?:\\[^\r\n]|[^\s<>()\\]|\([^()\r\n]*\))+))"
)


def _rewrite_markdown_note(path: Path, data: str, vault: Path, old: str, new: str) -> str:
    if "](" not in data and "[[" not in data and "hanadocs/" not in data:
        return data

    def rewrite_target(target: str, base: Path) -> str:
        if "/" not in target or "://" in target or target.startswith("//"):
            return target
        absolute = Path(os.path.normpath(base / target.lstrip("/")))
        source = vault / old
        if not absolute.is_relative_to(source):
            return target
        rewritten = vault / new / absolute.relative_to(source)
        prefix = "./" if target.startswith("./") else "/" if target.startswith("/") else ""
        result = prefix + os.path.relpath(rewritten, base)
        return result + "/" if target.endswith("/") else result

    def rewrite_link(match: re.Match[str]) -> str:
        parts = re.split(r"([#|])", match[1], maxsplit=1)
        target = parts[0]
        base = path.parent if target.startswith(("./", "../")) else vault
        return "[[" + rewrite_target(target, base) + "".join(parts[1:]) + "]]"

    def encode_markdown(raw_path: str, result: str, *, angle_brackets: bool, literal: bool) -> str:
        components = raw_path.split("/")
        replacement = markdown_links.encode_path(new, angle_brackets=angle_brackets, literal=literal)
        for index, component in enumerate(components):
            decoded, _ = markdown_links.path_and_fragment(component)
            if unquote(decoded) != old:
                continue
            candidate = "/".join((*components[:index], replacement, *components[index + 1:]))
            decoded, _ = markdown_links.path_and_fragment(candidate)
            if unquote(decoded) == result:
                return candidate
        return markdown_links.encode_path(result, angle_brackets=angle_brackets, literal=literal)

    def rewrite_inline(match: re.Match[str]) -> str:
        group = "angle" if match["angle"] is not None else "bare"
        raw = match[group]
        target, fragment = markdown_links.path_and_fragment(raw)
        decoded = unquote(target)
        base = vault if decoded.startswith("/") else path.parent
        result = rewrite_target(decoded, base)
        if result == decoded and base != vault and not decoded.startswith(("./", "../")):
            result = rewrite_target(decoded, vault)
        if result == decoded:
            return match[0]
        raw_path = raw[:len(raw) - len(fragment)]
        replacement = encode_markdown(raw_path, result, angle_brackets=group == "angle", literal=raw_path == decoded)
        prefix = match[0][:match.start(group) - match.start()]
        suffix = match[0][match.end(group) - match.start():]
        return prefix + replacement + fragment + suffix

    full_path = re.compile(r"(?<![\w.%~+-])hanadocs/" + re.escape(old) + "/")

    def rewrite_links(text: str) -> str:
        paths = full_path.sub(lambda _: f"hanadocs/{new}/", text)
        return _INLINE_DESTINATION.sub(rewrite_inline, _WIKI_LINK.sub(rewrite_link, paths))

    return markdown_links.rewrite_outside_code(data, rewrite_links)


def _vault_files(vault: Path) -> Generator[Path]:
    def refuse(error: OSError) -> None:
        raise error

    for folder, directories, files in os.walk(vault, onerror=refuse):
        directories[:] = sorted(name for name in directories if name != ".git")
        for name in sorted(files):
            path = Path(folder) / name
            if name != ".git" and not path.is_symlink() and path.is_file():
                yield path


def _vault_text(path: Path) -> str | None:
    with path.open("rb") as stream:
        prefix = stream.read(8192)
        if b"\0" in prefix:
            return None
        data = prefix + stream.read()
    try:
        return data.decode("utf-8") if b"\0" not in data else None
    except UnicodeDecodeError:
        return None


@dataclass(frozen=True)
class VaultFileState:
    """The bytes and file metadata used to verify a pending vault edit."""

    content: bytes
    metadata: os.stat_result

    def matches_file(self, path: Path) -> bool:
        current, expected = path.lstat(), self.metadata
        return (os.path.samestat(current, expected) and current.st_mode == expected.st_mode
                and current.st_mtime_ns == expected.st_mtime_ns and current.st_ctime_ns == expected.st_ctime_ns)


def _vault_file_state(path: Path) -> VaultFileState:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"{path}: the vault path is no longer a regular file")
        return VaultFileState(stream.read(), metadata)


def _publish_vault_edit(path: Path, before: str, after: str) -> None:
    expected = before.encode()
    original = _vault_file_state(path)
    if original.content != expected:
        raise ValueError(f"{path}: vault file changed during rename")
    mode = stat.S_IMODE(original.metadata.st_mode)
    with atomic_file.prepared_replacement(path, after.encode(), mode=mode) as temporary:
        if _vault_file_state(path).content != expected or not original.matches_file(path):
            raise ValueError(f"{path}: vault file changed during rename")
        os.replace(temporary, path)
        atomic_file.sync_folder(path.parent)


@dataclass(frozen=True)
class RemainingVaultPath:
    file: Path
    line: int


@dataclass(frozen=True)
class VaultRenameResult:
    changed_lines: int
    remaining_paths: tuple[RemainingVaultPath, ...] = ()

    def to_json(self) -> dict[str, object]:
        return {"lines": self.changed_lines,
                "remaining_paths": [{"file": str(hit.file), "line": hit.line} for hit in self.remaining_paths]}

    @classmethod
    def from_json(cls, saved: Mapping[str, object]) -> VaultRenameResult:
        return cls(cast(int, saved["lines"]),
                   tuple(RemainingVaultPath(Path(_text(hit, "file")), cast(int, hit["line"]))
                         for hit in _entries(saved, "remaining_paths")) if "remaining_paths" in saved else ())


def _remaining_vault_paths(vault: Path, old: str) -> Generator[RemainingVaultPath]:
    spellings = "|".join(map(re.escape, dict.fromkeys((old, quote(old, safe="")))))
    segment = re.compile(r"(?<![\w.%~+-])(?:" + spellings + r")(?=/)|(?<=/)(?:" + spellings + r")(?![\w.%~+/-])")
    for target in _vault_files(vault):
        content = _vault_text(target)
        if content is None:
            continue
        for line, text in enumerate(content.splitlines(), start=1):
            for _ in segment.finditer(text):
                yield RemainingVaultPath(target, line)


def _rewrite_vault(path: Path, saved: dict[str, object], plan: staff.ProducerRename) -> VaultRenameResult:
    before, after = plan.before.worktree, plan.after.worktree
    if not isinstance(before, staff.PresentWorktree) or not isinstance(after, staff.PresentWorktree) or before == after:
        return VaultRenameResult(0)
    vault = before.path.parent
    journal = _object(saved["vault"])
    count = 0
    for name, value in journal.items():
        item = _object(value)
        target = Path(name)
        if target.suffix.casefold() not in (".md", ".markdown"):
            continue
        content = _vault_file_state(target).content
        before, after = _text(item, "before"), _text(item, "after")
        if content == before.encode():
            _publish_vault_edit(target, before, after)
        elif content == after.encode():
            atomic_file.sync_folder(target.parent)
        else:
            raise ValueError(f"{target}: vault file changed during rename")
        count += cast(int, item["lines"])
    for target in _vault_files(vault):
        if target.suffix.casefold() not in (".md", ".markdown") or str(target) in journal:
            continue
        content = _vault_text(target)
        if content is None:
            continue
        edited = _rewrite_markdown_note(target, content, vault, plan.before.name, plan.after.name)
        if edited == content:
            continue
        lines = sum(old != new for old, new in zip(content.splitlines(), edited.splitlines(), strict=True))
        journal[str(target)] = {"before": content, "after": edited, "lines": lines}
        _write(path, saved)
        _publish_vault_edit(target, content, edited)
        count += lines
    return VaultRenameResult(count, tuple(_remaining_vault_paths(vault, plan.before.name)))


def _preflight_session(row: staff.StaffRow) -> models_registry.ProducerProfile:
    profile = models_registry.producer_profile(row.id)
    native = stop_resume.producer_native_backend(row, lambda: pane_record.launch_for_session(row.current_session))
    if isinstance(native, stop_resume.ProducerFamilyUnavailable):
        raise ValueError(f"{row.name}: the native session backend is unavailable")
    if profile.family != native:
        raise ValueError(f"{row.name}: launch profile uses {profile.family}, native session uses {native}")
    _require_stopped(row, native)
    return profile


def _require_stopped(row: staff.StaffRow, backend: str) -> None:
    if not isinstance(herdr.read_conversation(row.current_session, backend), herdr.StoppedConversation):
        raise ValueError(f"{row.name}: stop the {backend} session before renaming")


def _finish_lookup(path: Path, plan: staff.ProducerRename) -> None:
    staff.release_producer_rename(plan.operation)
    if staff.ProducerRename.from_json(_read(path)["plan"]).operation == plan.operation:
        path.unlink()
    atomic_file.sync_folder(path.parent)


def _report_rename(plan: staff.ProducerRename, result: VaultRenameResult) -> int:
    folder = stop_resume.producer_document_folder(plan.after)
    print(f"{plan.after.name}: document folder {folder}, changed {result.changed_lines} vault line(s)")
    for hit in result.remaining_paths:
        print(f"{hit.file}:{hit.line}: old producer path remains")
    return 0


def rename(old: str, new: str, repair_displays: Callable[[staff.StaffRow], bool]) -> int:
    """Move the stopped producer and keep its native conversation."""
    staff.validate_producer_name(new)
    path = _path("rename", old)
    with staff.slot_operation("producer-lifecycle", old):
        saved = _read(path)
        legacy = saved if saved and "plan" not in saved else {}
        plan = staff.ProducerRename.from_json(saved["plan"]) if saved and not legacy else None
        completed = _read(_path("renamed", plan.operation)) if plan is not None else {}
        if plan is not None:
            atomic_file.sync_folder(path.parent)
        if plan is not None and completed:
            _finish_lookup(path, plan)
            current = lookup(old, staff.initial_snapshot())
            if plan.after.name == new and (current is None or current.id == plan.before.id):
                return _report_rename(plan, VaultRenameResult.from_json(completed))
            saved, plan = {}, None
        staff.require_no_producer_rename(new, operation=plan.operation if plan is not None else "")
        record = staff.initial_snapshot()
        if legacy and _text(legacy, "name") != new:
            raise ValueError(f"{old}: retry the rename to {_text(legacy, 'name')}")
        row = (record.by_id(plan.before.id) if plan is not None else
               record.by_id(staff.StaffId(_text(legacy, "staff"))) if legacy else lookup(old, record))
        if row is None or row.kind != "producer" or isinstance(row.retirement, staff.RetiredProducer):
            raise ValueError(f"{old}: no active producer")
        if plan is not None and plan.after.name != new:
            raise ValueError(f"{old}: retry the rename to {plan.after.name}")
        _require_local(row)
        previous = plan.before.name if plan is not None else _text(legacy, "old") if legacy else row.name
        with (staff.slot_operation("producer-first-" + previous, previous),
              staff.slot_operation("producer-first-" + new, new),
              staff.row_operation(row.id, rename_operation=plan.operation if plan is not None else "")):
            record = staff.initial_snapshot()
            row = record.by_id(row.id)
            if row is None:
                raise ValueError(f"{old}: no active producer")
            if plan is None and row.name == new:
                _ = repair_displays(row)
                if legacy:
                    path.unlink()
                    atomic_file.sync_folder(path.parent)
                return 0
            if plan is None:
                if row.name != previous:
                    raise ValueError(f"{old}: the producer changed before rename")
                _require_no_admission(previous)
                _require_no_admission(new)
                _require_free_name(new, record, own=row)
                profile = _preflight_session(row)
                saved = _rename_plan(row, new, profile)
                plan = staff.ProducerRename.from_json(saved["plan"])
                _write(path, saved)
            atomic_file.sync_folder_ancestry(path.parent)
            atomic_file.sync_folder_ancestry(staff.record_path().parent / "rows")
            moves = _entries(saved, "moves")
            links = [ClaudeLinkChange.from_json(entry) for entry in _entries(saved, "links")]
            links_by_path = {link.path: link for link in links}
            proofs = _entries(saved, "claude_transcripts")
            if saved["started"] is not True:
                try:
                    staff.reserve_producer_rename(plan)
                except staff.StaffRefused:
                    _finish_lookup(path, plan)
                    raise
                try:
                    _require_stopped(row, _text(saved, "backend"))
                    before, after = plan.before.worktree, plan.after.worktree
                    if (isinstance(before, staff.PresentWorktree) and isinstance(after, staff.PresentWorktree)
                            and before != after and _planned_document(row, before, after) != plan.after.doc):
                        raise ValueError(f"{row.name}: the producer document path changed during rename")
                    _verify_claude_transcripts(proofs, moved=False)
                    for move in moves:
                        _move_one(move, apply=False, links=links_by_path)
                    for link in links:
                        _verify_link_before_move(link)
                except (OSError, ValueError, staff.StaffRefused):
                    _finish_lookup(path, plan)
                    raise
                saved["started"] = True
                _write(path, saved)
            else:
                staff.reserve_producer_rename(plan)
                _require_stopped(row, _text(saved, "backend"))
            for move in moves:
                _move_one(move, apply=True, links=links_by_path)
            for link in links:
                _retarget_link(link)
            _verify_claude_transcripts(proofs, moved=True)
            _rewrite_launches(path, saved, plan)
            producer_creation.retain_completed_record(previous, row, _object(saved["creation"]))
            result = _rewrite_vault(path, saved, plan)
            row = staff.rename(row.id, new, operation=plan.operation)
            _ = repair_displays(row)
            _write(_path("renamed", plan.operation), result.to_json())
            _finish_lookup(path, plan)
        return _report_rename(plan, result)


def _send(recipient: str, text: str, key: str) -> None:
    try:
        outcome = restart_support.send_message(recipient, "producer retirement", text, key=key, repeat_minutes=1440)
    except RuntimeError as error:
        raise ValueError(f"{recipient}: {error}") from error
    if outcome["kind"] not in ("sent", "queued", "kept", "acknowledged", "window skipped"):
        raise ValueError(f"{recipient}: handoff notice is unconfirmed: {outcome}")


def _request_handoff(row: staff.StaffRow, handoff: Path, profile: models_registry.ProducerProfile) -> None:
    receipt = _path("handoff", row.id)
    saved = _read(receipt)
    if not saved:
        saved = {"notice": uuid4().hex}
        _write(receipt, saved)
    if saved.get("sent") is not True:
        address = "codex:" if profile.family == "codex" else "session:"
        _send(address + row.current_session,
              f"Write {handoff}. List each item, its state, and its open design points.",
              f"producer-handoff:{_text(saved, 'notice')}")
        _write(receipt, {**saved, "sent": True})


def _clear_retirement_state(identity: staff.StaffId) -> None:
    for operation in ("retire", "handoff"):
        _path(operation, identity).unlink(missing_ok=True)


def _showrunner(item: the_list.ListItem, record: staff.StaffSnapshot) -> staff.StaffRow | None:
    row = record.by_id(item.owner) if not isinstance(item.owner, the_list.UnassignedOwner) else None
    while row is not None and row.kind != "showrunner":
        row = record.parent_of(row)
    return row


def _machinery_stop(row: staff.StaffRow) -> None:
    client = herdr.Herdr(herdr.shared_session())
    panes = restart.read_row_panes([row], client)
    if isinstance(panes, restart.RowPanesUnreadable):
        raise ValueError(f"{row.name}: cannot read producer panes: {panes.reason}")
    terminals: dict[str, str] = {}
    for _, pane in panes:
        place = client.pane_row(pane)
        if not isinstance(place, herdr.NoPane):
            terminals[pane] = herdr.json_text(place.get("terminal_id"), "producer terminal")
    if not restart.stop_row(row, client):
        raise ValueError(f"{row.name}: the conversation did not stop")
    for pane, terminal in terminals.items():
        if not client.close_tab_confirmed(pane, target=row.current_session, terminal=terminal):
            raise ValueError(f"{row.name}: the tab did not close")
    shutdown_record.remove_started([row.id])


def retire(reference: str, *, to: str | None) -> int:
    """Record each handoff before removing the profile and marking the producer."""
    with staff.slot_operation("producer-lifecycle", reference):
        record = staff.initial_snapshot()
        row = lookup(reference, record)
        if row is None:
            _require_no_admission(reference)
            raise ValueError(f"{reference}: no producer row")
        _require_local(row)
        path = _path("retire", row.id)
        name = row.name
        with staff.slot_operation("producer-first-" + name, name), staff.row_operation(row.id):
            _require_no_admission(name)
            record = staff.initial_snapshot()
            row = record.by_id(row.id)
            if row is None or row.kind != "producer" or row.name != name:
                raise ValueError(f"{reference}: the producer changed before retirement")
            keeper_id = staff.list_producer_id()
            if keeper_id is None:
                raise ValueError("the List owner is unavailable")
            if row.id == keeper_id:
                raise ValueError(f"{row.name}: the List producer cannot retire")
            keeper = record.by_id(keeper_id)
            if keeper is None or keeper not in staff.active(record, "producer"):
                raise ValueError("the List owner has no active producer row")
            _require_local(row)
            if isinstance(row.retirement, staff.RetiredProducer):
                _clear_retirement_state(row.id)
                print(f"{row.name}: retired")
                return 0
            return _retire(row, keeper, record, path, to)


def _require_live_destination(row: staff.StaffRow) -> None:
    machine = stop_resume.member_machine(row)
    if machine and machine != remote.local_machine():
        report = roster.peer_roster(os.environ)
        live = any(entry.machine == machine and (
            entry.session == roster.ClaudeSession(row.current_session)
            or (entry.thread == roster.CodexThread(row.current_session)
                and entry.status in ("active", "idle", "working", "done", "blocked", "running"))
        ) for entry in report.entries)
    else:
        live = isinstance(herdr.read_conversation(row.current_session), herdr.LiveConversation)
    if not live:
        raise ValueError(f"{row.name}: choose a live destination producer")


def _receipt_item_ids(saved: Mapping[str, object], field: str) -> list[str]:
    values = saved.get(field)
    if not isinstance(values, list) or not all(isinstance(value, str) for value in cast(list[object], values)):
        raise ValueError(f"the retirement receipt has invalid {field} List items")
    return cast(list[str], values)


def _retire(row: staff.StaffRow, keeper: staff.StaffRow, record: staff.StaffSnapshot,
            path: Path, to: str | None) -> int:
    try:
        questions = update_registration.question_records_for_document(
            row, update_registration.QuestionStaff.for_record(record), statuses=("open",), strict=True)
    except update_registration.RegistrationFailure as error:
        raise ValueError(str(error)) from error
    if questions:
        raise ValueError(f"{row.name}: close its open questions before retirement")
    saved: dict[str, object] = _read(path)
    if saved and (session := _text(saved, "session")) != row.current_session:
        raise ValueError(f"{row.name}: retirement belongs to session {session}. Current session is {row.current_session}.")
    destination = record.by_id(staff.StaffId(_text(saved, "destination"))) if saved else keeper
    if not saved and to:
        destination = lookup(to, record)
    if destination is None or destination not in staff.active(record, "producer") or destination.id == row.id:
        raise ValueError(f"{to or keeper.name}: choose a different active producer")
    if to and to not in (destination.name, destination.id, destination.current_session):
        raise ValueError(f"{row.name}: finish the retirement to {destination.name}")
    if to or destination.id != keeper.id:
        _require_live_destination(destination)
    profile = (models_registry.parse_producer_profile(_text(saved, "profile")) if saved
               else models_registry.producer_profile(row.id))
    handoff = stop_resume.producer_document_folder(row) / "handoff.md"
    if not saved and not handoff.is_file():
        _request_handoff(row, handoff, profile)
        print(f"{row.name}: run agents retire again after {handoff} exists.")
        return 0
    if not saved:
        saved = {"profile": profile.value(), "destination": destination.id, "session": row.current_session,
                 "pending": [], "notified": [], "notice": uuid4().hex}
        _write(path, saved)
    _machinery_stop(row)
    pending = list(dict.fromkeys([*_receipt_item_ids(saved, "pending"),
        *(item.id for item in the_list.records(keeper.id) if item.producer == row.id)]))
    notified = _receipt_item_ids(saved, "notified")
    saved["pending"] = pending
    _write(path, saved)
    for index, identity in enumerate(pending):
        item = the_list.move_open_producer(keeper.id, identity, row.id, destination.id, source=RETIRE_SOURCE)
        owner = _showrunner(item, record)
        if owner is not None and item.producer == destination.id and identity not in notified:
            _send("session:" + owner.id, f"{item.id} {item.title}: the new producer is {destination.name}.",
                  f"producer-retire:{_text(saved, 'notice')}:{identity}")
            notified.append(identity)
        saved["pending"] = pending[index + 1:]
        _write(path, saved)
    with the_list.require_no_open_producer_items(keeper.id, row.id):
        models_registry.remove_producer(row.id, profile)
        _ = staff.set_producer_retirement(row.id, staff.RetiredProducer(profile.value()))
    _clear_retirement_state(row.id)
    print(f"{row.name}: retired")
    return 0


def restore(reference: str, *, to: str | None, why: str) -> stop_resume.ResumeResult:
    """Restore the saved profile before the conversation resumes."""
    with staff.slot_operation("producer-lifecycle", reference):
        record = staff.initial_snapshot()
        row = lookup(reference, record)
        if row is None:
            raise ValueError(f"{reference}: no producer row")
        _require_local(row)
        with staff.slot_operation(row.id, row.name):
            if isinstance(row.retirement, staff.RetiredProducer):
                _clear_retirement_state(row.id)
                profile = models_registry.parse_producer_profile(row.retirement.profile)
                _ = models_registry.store_producer(row.id, *profile, preserve_existing=True)
                _ = staff.set_producer_retirement(row.id, staff.Active())
            _ = os.environ.pop(models_registry.OPERATION_ENVIRONMENT[0], None)
            return stop_resume.resume(row.id, why=why, to=to)
