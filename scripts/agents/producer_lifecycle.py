"""Create and admit a producer."""
from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import cast

from scripts.agents import remote, stop_resume
from scripts.herdr.place import HerdrPlace
from scripts.lib import atomic_file, models_registry
from scripts.message import roster
from scripts.production import staff

TEMPLATE = Path(__file__).resolve().parents[2] / "templates/producer/README.md"


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


def _profiles() -> dict[str, str]:
    return models_registry.parse_registry(models_registry.registry_text()).get("producers", {})


def _check_name(name: str) -> None:
    if name.casefold() == "morty":
        raise ValueError("morty: reserved producer name")
    staff.validate_name("producer", name)
    if name in {".", ".."} or any(mark in name for mark in "#[]="):
        raise ValueError(f"{name!r}: invalid producer name")


def _require_free_name(name: str, record: staff.StaffSnapshot, *,
                       own: staff.StaffRow | None = None, pane: str = "") -> None:
    key = name.casefold()
    if any(row.name.casefold() == key and (own is None or row.id != own.id) for row in record.rows):
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
