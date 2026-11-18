"""Create, rename, retire and restore a producer."""
from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import cast
from urllib.parse import quote
from uuid import uuid4

from scripts.agents import pane_record, remote, restart, restart_support, shutdown_record, stop_resume
from scripts.herdr import herdr
from scripts.herdr.place import HerdrPlace
from scripts.lib import atomic_file, models_registry
from scripts.message import roster
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


def _profiles() -> dict[str, str]:
    return models_registry.parse_registry(models_registry.registry_text()).get("producers", {})


def _profile(profiles: dict[str, str], name: str) -> models_registry.ProducerProfile:
    path = models_registry.registry_path()
    try:
        return models_registry.parse_producer_profile(profiles.get(name, ""))
    except ValueError as error:
        raise ValueError(f"{path}: producers.{name}: {error}") from error


def _check_name(name: str) -> None:
    if name.casefold() == "morty":
        raise ValueError("morty: reserved producer name")
    staff.validate_name("producer", name)
    if name in {".", ".."} or any(mark in name for mark in "#[]="):
        raise ValueError(f"{name!r}: invalid producer name")


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


def _check_key(name: str, profiles: dict[str, str], *, own: str = "") -> None:
    if any(key.casefold() == name.casefold() and key != own for key in profiles):
        raise ValueError(f"{name}: {models_registry.registry_path()} already has a producer key")


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
    _check_name(name)
    if len(job.splitlines()) > 1 or "\n" in job or "\r" in job:
        raise ValueError("--job must contain one line")
    folder = stop_resume.producer_folder(name)
    path = stop_resume.producer_creation_path(name)
    with (staff.slot_operation("producer-lifecycle", name),
          staff.slot_operation("producer-first-" + name, name)):
        record = staff.initial_snapshot()
        matches = [row for row in record.find(kind="producer") if row.name == name]
        if len(matches) > 1:
            raise ValueError(f"{name}: several producer rows have this name")
        own = matches[0] if matches else None
        saved = _read(path)
        profiles = _profiles()
        if saved:
            folder = Path(_text(saved, "folder"))
            profile = models_registry.parse_producer_profile(_text(saved, "profile"))
            if (model and model not in (profile.model, f"{profile.family}:{profile.model}")
                    or effort and effort != profile.effort or job and job != _text(saved, "job")):
                raise ValueError(f"{name}: retry the creation with its original options")
            if "staff" in saved:
                if own is None or own.id != _text(saved, "staff"):
                    raise ValueError(f"{name}: the saved creation belongs to another staff state")
                if isinstance(saved.get("opening_confirmed"), str):
                    _print_started(own, _text(saved, "account"), _text(saved, "tab"))
                    return 0
            if own is not None and (own.doc != staff.LiveDoc(remote.local_machine(), folder)
                                    or own.worktree != staff.PresentWorktree(remote.local_machine(), folder)):
                raise ValueError(f"{name}: another staff row has this name")
            reservation = _read(stop_resume.producer_admission_record(name))
            if name in profiles and profiles[name] != profile.value():
                raise ValueError(f"{name}: its producer settings changed during creation")
            _check_key(name, profiles, own=name)
            _require_free_name(name, record, own=own, pane=_text(reservation, "pane") if reservation else "")
        else:
            _check_key(name, profiles)
            _require_free_name(name, record)
            profile = _new_profile(model, effort)
            _ = models_registry.producer_settings(name, *profile)
            template = TEMPLATE.read_text(encoding="utf-8")
            values = {"name": name, "job": job}
            readme = re.sub(r"\{(name|job)\}", lambda match: values[match[1]], template)
            saved = {"profile": profile.value(), "job": job, "readme": readme, "folder": str(folder),
                     "opening_since": datetime.now(timezone.utc).isoformat()}
            _write(path, saved)
        _ = models_registry.store_producer(name, *profile)
        _ = os.environ.pop(models_registry.OPERATION_ENVIRONMENT[0], None)
        _create_readme(folder / "README.md", _text(saved, "readme"))
        request = stop_resume.ProducerFirstStart(name, folder)
        result = stop_resume._locked_first_producer_resume(request, to="")  # pyright: ignore[reportPrivateUsage]
        if not isinstance(result, stop_resume.ClaudeUp):
            raise ValueError(result.reason if isinstance(result, stop_resume.ResumeFailed) else f"{name}: first start failed")
        admitted = staff.initial_snapshot().by_current_session(result.session_id)
        if admitted is None or admitted.name != name or admitted.kind != "producer":
            raise ValueError(f"{name}: the first start did not record the producer")
        _write(path, {**_read(path), "staff": admitted.id, "account": result.account, "tab": result.pane_id})
        _print_started(admitted, result.account, result.pane_id)
        return 0


def _protect_morty(row: staff.StaffRow) -> None:
    if row.name.casefold() == "morty":
        raise ValueError("morty: keep the List producer")


def _document_folder(row: staff.StaffRow) -> Path:
    if isinstance(row.doc, (staff.LiveDoc, staff.WrappedDoc)):
        return row.doc.path
    return stop_resume.producer_folder(row.name)


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


def rename(old: str, new: str, repair_displays: Callable[[staff.StaffRow], bool]) -> int:
    """Move the key and repair displays without moving the document."""
    _check_name(new)
    path = _path("rename", old)
    with staff.slot_operation("producer-lifecycle", old):
        record = staff.initial_snapshot()
        saved = _read(path)
        row = record.by_id(staff.StaffId(_text(saved, "staff"))) if saved else lookup(old, record)
        if row is None:
            _require_no_admission(old)
            if saved:
                raise ValueError(f"{path}: the saved rename no longer matches its producer")
            raise ValueError(f"{old}: no active producer")
        previous = _text(saved, "old") if saved else row.name
        if saved:
            if _text(saved, "name") != new:
                raise ValueError(f"{old}: retry the rename to {_text(saved, 'name')}")
            if row.kind != "producer" or row.name not in (previous, new):
                raise ValueError(f"{path}: the saved rename no longer matches its producer")
        if row.kind != "producer" or isinstance(row.retirement, staff.RetiredProducer):
            raise ValueError(f"{old}: no active producer")
        _protect_morty(row)
        _require_local(row)
        with (staff.slot_operation("producer-first-" + previous, previous),
              staff.slot_operation("producer-first-" + new, new),
              staff.row_operation(row.id)):
            _require_no_admission(previous)
            _require_no_admission(new)
            record = staff.initial_snapshot()
            row = record.by_id(row.id)
            if (row is None or row.kind != "producer" or row.name not in (previous, new)
                    or isinstance(row.retirement, staff.RetiredProducer)):
                raise ValueError(f"{old}: the producer changed before the rename")
            _require_local(row)
            folder = _document_folder(row)
            profiles = _profiles()
            if saved:
                profile = models_registry.parse_producer_profile(_text(saved, "profile"))
                folder = Path(_text(saved, "folder"))
                _require_free_name(new, record, own=row)
            elif row.name != new:
                _check_key(new, profiles, own=row.name)
                _require_free_name(new, record, own=row)
                previous = row.name
                profile = _profile(profiles, previous)
                _write(path, {"staff": row.id, "name": new, "old": previous,
                              "profile": profile.value(), "folder": str(folder)})
            else:
                _ = repair_displays(row)
                print(f"{row.name}: the document folder remains {folder}")
                return 0
            keep_old = row.name != previous
            if keep_old and previous in profiles:
                print(f"{previous}: kept the producer key because the saved producer has a different name")
            models_registry.rename_producer(previous, new, profile, keep_old=keep_old)
            if row.name != new:
                row = staff.rename(row.id, new)
            _ = repair_displays(row)
            path.unlink(missing_ok=True)
        print(f"{new}: the document folder remains {folder}")
        return 0


def _send(recipient: str, text: str, key: str) -> None:
    try:
        outcome = restart_support.send_message(recipient, "producer retirement", text, key=key, repeat_minutes=1440)
    except RuntimeError as error:
        raise ValueError(f"{recipient}: {error}") from error
    if outcome["kind"] not in ("sent", "queued", "acknowledged", "window skipped"):
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
        _protect_morty(row)
        _require_local(row)
        path = _path("retire", row.id)
        name = row.name
        with staff.slot_operation("producer-first-" + name, name), staff.row_operation(row.id):
            _require_no_admission(name)
            record = staff.initial_snapshot()
            row = record.by_id(row.id)
            if row is None or row.kind != "producer" or row.name != name:
                raise ValueError(f"{reference}: the producer changed before retirement")
            _require_local(row)
            if isinstance(row.retirement, staff.RetiredProducer):
                _clear_retirement_state(row.id)
                print(f"{row.name}: retired")
                return 0
            return _retire(row, record, path, to)


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


def _retire(row: staff.StaffRow, record: staff.StaffSnapshot, path: Path, to: str | None) -> int:
    try:
        questions = update_registration.question_records_for_document(
            row, update_registration.QuestionStaff.for_record(record), statuses=("open",), strict=True)
    except update_registration.RegistrationFailure as error:
        raise ValueError(str(error)) from error
    if questions:
        raise ValueError(f"{row.name}: close its open questions before retirement")
    keeper = lookup("morty", record)
    if keeper is None:
        raise ValueError("morty: no List producer row")
    saved: dict[str, object] = _read(path)
    if saved and (session := _text(saved, "session")) != row.current_session:
        raise ValueError(f"{row.name}: retirement belongs to session {session}. Current session is {row.current_session}.")
    destination = (record.by_id(staff.StaffId(_text(saved, "destination"))) if saved else lookup(to or "morty", record))
    if destination is None or destination not in staff.active(record, "producer") or destination.id == row.id:
        raise ValueError(f"{to or 'morty'}: choose a different active producer")
    if to and to not in (destination.name, destination.id, destination.current_session):
        raise ValueError(f"{row.name}: finish the retirement to {destination.name}")
    if to or destination.id != keeper.id:
        _require_live_destination(destination)
    profile = (models_registry.parse_producer_profile(_text(saved, "profile")) if saved
               else _profile(_profiles(), row.name))
    handoff = _document_folder(row) / "handoff.md"
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
        models_registry.remove_producer(row.name, profile)
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
                profiles = _profiles()
                _check_key(row.name, profiles, own=row.name)
                if row.name in profiles and profiles[row.name] != profile.value():
                    raise ValueError(f"{row.name}: its producer key has another profile")
                _ = models_registry.store_producer(row.name, *profile)
                _ = staff.set_producer_retirement(row.id, staff.Active())
            _ = os.environ.pop(models_registry.OPERATION_ENVIRONMENT[0], None)
            return stop_resume.resume(row.id, why=why, to=to)
