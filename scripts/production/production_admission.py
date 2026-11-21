"""Keep a production's admission with its fixed staff ID."""
from __future__ import annotations

import json
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal, cast

from scripts.agents.remote import local_machine
from scripts.lib.atomic_file import create_text_atomic, prepared_replacement, sync_folder
from scripts.lib.file_lock import exclusive
from scripts.lib.git_exclude import ExcludeRefused, require_untracked_ignored
from scripts.production import session_staff, staff
from scripts.production.production_doc import (
    LegacyProduction, Production, ProductionDocument, Refusal, read_guard_ready, read_new_production,
)
from scripts.production.production_names import GUARD_READY_FILE, MERGE_CHECKOUT_FIELD
from scripts.production.record_store import state_root


AdmissionBasis = Literal["rollout inventory", "guard and document"]


@dataclass(frozen=True)
class NoVerifiedMove:
    """The receipt names only the admitted document."""


@dataclass(frozen=True)
class AdmissionReceipt:
    document: Path
    machine: str
    basis: AdmissionBasis
    moved_document: Path | NoVerifiedMove = NoVerifiedMove()

    def permits(self, production: Production, machine: str) -> bool:
        if machine != self.machine:
            return False
        if isinstance(production.merge_checkout, LegacyProduction):
            return self.basis == "rollout inventory" and production.doc == self.document
        expected = self.document if self.basis == "guard and document" else self.moved_document
        return production.doc == expected

    def fields(self) -> dict[str, str]:
        fields = {"document": str(self.document), "machine": self.machine, "basis": self.basis}
        if isinstance(self.moved_document, Path):
            fields["moved_document"] = str(self.moved_document)
        return fields


def admission_folder() -> Path:
    return state_root() / "admission"


def require_guard_ready() -> None:
    """Require the installed guard proof before a production starts."""
    if read_guard_ready() is None:
        raise Refusal(f"new production creation requires a valid {GUARD_READY_FILE}")


def require_new_document(document: ProductionDocument) -> None:
    require_guard_ready()
    if isinstance(document.merge_checkout, LegacyProduction):
        raise Refusal(f"{document.doc}: new productions require {MERGE_CHECKOUT_FIELD}")
    main = session_staff.production_session_directory(document.doc, document.checkout)
    for path in (document.doc, document.log):
        try:
            require_untracked_ignored(main, path.relative_to(main).as_posix())
        except (ExcludeRefused, ValueError) as error:
            raise Refusal(str(error)) from error


def _local_document(production: Production) -> staff.LiveDoc:
    binding = production.owner.doc
    if (not isinstance(binding, staff.LiveDoc) or binding.machine != local_machine()
            or binding.path.resolve() != production.doc):
        raise Refusal(f"{production.doc}: admission requires the exact local staff document")
    return binding


def _parse_receipt(raw: object, source: Path) -> AdmissionReceipt:
    if not isinstance(raw, dict):
        raise Refusal(f"{source}: expected an admission receipt")
    fields = cast(dict[str, object], raw)
    document, machine, basis = fields.get("document"), fields.get("machine"), fields.get("basis")
    if (not isinstance(document, str) or not Path(document).is_absolute() or not isinstance(machine, str)
            or not machine or basis not in ("rollout inventory", "guard and document")):
        raise Refusal(f"{source}: invalid admission receipt")
    if "moved_document" not in fields:
        return AdmissionReceipt(Path(document), machine, cast(AdmissionBasis, basis))
    moved = fields["moved_document"]
    if basis != "rollout inventory" or not isinstance(moved, str) or not Path(moved).is_absolute():
        raise Refusal(f"{source}: invalid moved document")
    return AdmissionReceipt(Path(document), machine, "rollout inventory", Path(moved))


def _new_inventory(identities: frozenset[staff.StaffId]) -> dict[str, dict[str, str]]:
    entries: dict[str, dict[str, str]] = {}
    record = staff.snapshot()
    for identity in sorted(identities):
        row = record.by_id(identity)
        if row is None or row.kind != "showrunner":
            raise Refusal(f"{identity}: expected an existing showrunner staff ID")
        if not isinstance(row.doc, staff.LiveDoc) or row.doc.machine != local_machine():
            raise Refusal(f"{identity}: expected a local live document")
        production = read_new_production(row.doc.path, row.name)
        if not isinstance(production.merge_checkout, LegacyProduction):
            raise Refusal(f"{identity}: expected a legacy production")
        entries[identity] = AdmissionReceipt(production.doc, row.doc.machine, "rollout inventory").fields()
    return entries


def capture_rollout_inventory(identities: Sequence[str]) -> Path:
    """Freeze the existing legacy documents and publish their receipts."""
    ids = frozenset(staff.native_id(identity) for identity in identities)
    if not ids:
        raise Refusal("name the existing legacy staff IDs")
    path = admission_folder() / "rollout-inventory.json"
    try:
        saved = path.read_bytes()
    except FileNotFoundError:
        entries = _new_inventory(ids)
        _ = create_text_atomic(path, json.dumps(entries, indent=2, sort_keys=True) + "\n")
        saved = path.read_bytes()
    raw = cast(object, json.loads(saved))
    if not isinstance(raw, dict):
        raise Refusal(f"{path}: expected a rollout inventory")
    entries = cast(dict[str, object], raw)
    if entries.keys() != ids:
        raise Refusal(f"{path}: the rollout inventory already names a fixed list of staff IDs")
    receipts = {identity: _parse_receipt(entries[identity], path) for identity in ids}
    for identity, receipt in receipts.items():
        if receipt.basis != "rollout inventory" or isinstance(receipt.moved_document, Path):
            raise Refusal(f"{path}: expected a legacy admission")
        target = _receipt_path(identity)
        if not create_text_atomic(target, json.dumps(receipt.fields()) + "\n"):
            existing = _parse_receipt(cast(object, json.loads(target.read_bytes())), target)
            if replace(existing, moved_document=NoVerifiedMove()) != receipt:
                raise Refusal(f"{target}: already records another admission")
    return path


def _receipt_path(identity: staff.StaffId) -> Path:
    return admission_folder() / f"{staff.native_id(identity)}.json"


@contextmanager
def _locked_receipt(production: Production) -> Generator[Path]:
    path = _receipt_path(production.owner.id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with exclusive(path.with_suffix(".lock")):
        yield path


def _admit(production: Production, machine: str, path: Path) -> AdmissionReceipt:
    try:
        saved = path.read_bytes()
    except FileNotFoundError:
        before = production.doc.read_bytes()
        document = read_new_production(production.doc, production.name)
        require_new_document(document)
        if production.doc.read_bytes() != before or document.for_owner(production.owner) != production:
            raise Refusal(f"{production.doc}: document changed during admission")
        receipt = AdmissionReceipt(production.doc, machine, "guard and document")
        if create_text_atomic(path, json.dumps(receipt.fields()) + "\n"):
            return receipt
        saved = path.read_bytes()
    receipt = _parse_receipt(cast(object, json.loads(saved)), path)
    if not receipt.permits(production, machine):
        raise Refusal(f"{production.doc}: the admission receipt names a different document or form")
    return receipt


def require_admission(production: Production) -> None:
    binding = _local_document(production)
    with _locked_receipt(production) as path:
        _ = _admit(production, binding.machine, path)


def record_verified_move(production: Production, destination: Path) -> None:
    """Record the verified destination and retain the source for rollback."""
    binding = _local_document(production)
    with _locked_receipt(production) as path:
        receipt = _admit(production, binding.machine, path)
        if receipt.basis != "rollout inventory":
            raise Refusal(f"{production.doc}: the move requires a legacy admission")
        if receipt.moved_document == destination:
            return
        if isinstance(receipt.moved_document, Path):
            raise Refusal(f"{production.doc}: the receipt already names {receipt.moved_document}")
        before = path.read_bytes()
        if _parse_receipt(cast(object, json.loads(before)), path) != receipt:
            raise Refusal(f"{path}: the receipt changed during the move")
        updated = replace(receipt, moved_document=destination)
        with prepared_replacement(path, json.dumps(updated.fields()) + "\n", mode=0o600) as temporary:
            if path.read_bytes() != before:
                raise Refusal(f"{path}: the receipt changed during the move")
            _ = temporary.replace(path)
            sync_folder(path.parent)
