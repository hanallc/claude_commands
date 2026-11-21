"""Keep a production's admission with its fixed staff ID."""
from __future__ import annotations

import json
import os
import stat
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal, cast

from scripts.agents import remote
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
class DocumentRename:
    admitted: Path
    source: Path
    destination: Path


type DocumentBinding = staff.LiveDoc | staff.WrappedDoc


@dataclass(frozen=True)
class PendingDocumentRebind:
    source: DocumentBinding
    destination: DocumentBinding
    device: int
    inode: int

    def verify_file(self, binding: DocumentBinding) -> None:
        info = binding.path.lstat()
        if (not stat.S_ISREG(info.st_mode) or binding.path.resolve() != binding.path
                or (info.st_dev, info.st_ino) != (self.device, self.inode)):
            raise staff.StaffRefused(f"{binding.path}: the document differs from the prepared file")


@dataclass(frozen=True)
class NoPendingDocumentRebind:
    """The receipt has no unfinished document change."""


@dataclass(frozen=True)
class AdmissionReceipt:
    document: Path
    machine: str
    basis: AdmissionBasis
    moved_document: Path | NoVerifiedMove = NoVerifiedMove()
    renames: tuple[DocumentRename, ...] = ()
    pending: PendingDocumentRebind | NoPendingDocumentRebind = NoPendingDocumentRebind()

    def current_path(self, original: Path) -> Path:
        current = original
        for rename in self.renames:
            if rename.admitted == original:
                current = rename.destination
        return current

    def original_paths(self) -> tuple[Path, ...]:
        return (self.document, self.moved_document) if isinstance(self.moved_document, Path) else (self.document,)

    def paths(self) -> tuple[Path, ...]:
        return tuple(self.current_path(original) for original in self.original_paths())

    def permits(self, production: Production, machine: str) -> bool:
        if machine != self.machine:
            return False
        if isinstance(production.merge_checkout, LegacyProduction):
            return self.basis == "rollout inventory" and production.doc == self.current_path(self.document)
        expected = self.document if self.basis == "guard and document" else self.moved_document
        return isinstance(expected, Path) and production.doc == self.current_path(expected)

    def fields(self) -> dict[str, object]:
        fields: dict[str, object] = {"document": str(self.document), "machine": self.machine, "basis": self.basis}
        if isinstance(self.moved_document, Path):
            fields["moved_document"] = str(self.moved_document)
        if self.renames:
            fields["renames"] = [
                [str(rename.admitted), str(rename.source), str(rename.destination)] for rename in self.renames]
        if isinstance(self.pending, PendingDocumentRebind):
            fields["pending"] = {
                "source": _binding_fields(self.pending.source), "destination": _binding_fields(self.pending.destination),
                "device": self.pending.device, "inode": self.pending.inode,
            }
        return fields


def admission_folder() -> Path:
    if os.environ.get("STAFF_FILE") and not os.environ.get("SHOWRUNNER_STATE_DIR"):
        return staff.record_path().parent / "showrunner/admission"
    return state_root() / "admission"


def _binding_fields(binding: DocumentBinding) -> dict[str, str]:
    return {"path": str(binding.path), "state": "live" if isinstance(binding, staff.LiveDoc) else "wrapped"}


def _absolute_path(raw: object, source: Path) -> Path:
    if not isinstance(raw, str) or not Path(raw).is_absolute():
        raise Refusal(f"{source}: expected an absolute document path")
    return Path(raw)


def _fields(raw: object, source: Path) -> dict[str, object]:
    if not isinstance(raw, dict):
        raise Refusal(f"{source}: expected an admission record")
    return cast(dict[str, object], raw)


def _parse_binding(raw: object, machine: str, source: Path) -> DocumentBinding:
    fields = _fields(raw, source)
    path = _absolute_path(fields.get("path"), source)
    match fields.get("state"):
        case "live":
            return staff.LiveDoc(machine, path)
        case "wrapped":
            return staff.WrappedDoc(machine, path)
        case _:
            raise Refusal(f"{source}: invalid document state")


def _parse_renames(raw: object, source: Path) -> tuple[DocumentRename, ...]:
    if not isinstance(raw, list):
        raise Refusal(f"{source}: expected document renames")
    renames: list[DocumentRename] = []
    for item in cast(list[object], raw):
        if not isinstance(item, list):
            raise Refusal(f"{source}: invalid document rename")
        paths = cast(list[object], item)
        if len(paths) != 3:
            raise Refusal(f"{source}: invalid document rename")
        admitted, before, after = paths
        renames.append(DocumentRename(_absolute_path(admitted, source), _absolute_path(before, source),
                                      _absolute_path(after, source)))
    return tuple(renames)


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
    if (not isinstance(binding, staff.LiveDoc) or binding.machine != remote.local_machine()
            or binding.path.resolve() != production.doc):
        raise Refusal(f"{production.doc}: admission requires the exact local staff document")
    return binding


def _parse_receipt(raw: object, source: Path) -> AdmissionReceipt:
    fields = _fields(raw, source)
    document = _absolute_path(fields.get("document"), source)
    machine, basis = fields.get("machine"), fields.get("basis")
    if not isinstance(machine, str) or not machine or basis not in ("rollout inventory", "guard and document"):
        raise Refusal(f"{source}: invalid admission receipt")
    receipt = AdmissionReceipt(document, machine, basis, renames=_parse_renames(fields.get("renames", []), source))
    if "moved_document" in fields:
        if basis != "rollout inventory":
            raise Refusal(f"{source}: invalid moved document")
        receipt = replace(receipt, moved_document=_absolute_path(fields["moved_document"], source))
    if "pending" not in fields:
        return receipt
    pending = _fields(fields["pending"], source)
    device, inode = pending.get("device"), pending.get("inode")
    if not isinstance(device, int) or not isinstance(inode, int):
        raise Refusal(f"{source}: invalid prepared file identity")
    return replace(receipt, pending=PendingDocumentRebind(
        _parse_binding(pending.get("source"), machine, source),
        _parse_binding(pending.get("destination"), machine, source), device, inode))


def _new_inventory(identities: frozenset[staff.StaffId]) -> dict[str, dict[str, object]]:
    entries: dict[str, dict[str, object]] = {}
    record = staff.snapshot()
    for identity in sorted(identities):
        row = record.by_id(identity)
        if row is None or row.kind != "showrunner":
            raise Refusal(f"{identity}: expected an existing showrunner staff ID")
        if not isinstance(row.doc, staff.LiveDoc) or row.doc.machine != remote.local_machine():
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
        if receipt.basis != "rollout inventory" or receipt != AdmissionReceipt(receipt.document, receipt.machine, receipt.basis):
            raise Refusal(f"{path}: expected a legacy admission")
        with _locked_receipt(identity) as target:
            if not create_text_atomic(target, json.dumps(receipt.fields()) + "\n"):
                existing = _parse_receipt(cast(object, json.loads(target.read_bytes())), target)
                if AdmissionReceipt(existing.document, existing.machine, existing.basis) != receipt:
                    raise Refusal(f"{target}: already records another admission")
    return path


def _receipt_path(identity: staff.StaffId) -> Path:
    return admission_folder() / f"{staff.native_id(identity)}.json"


@contextmanager
def _locked_receipt(identity: staff.StaffId) -> Generator[Path]:
    path = _receipt_path(identity)
    path.parent.mkdir(parents=True, exist_ok=True)
    with exclusive(path.with_suffix(".lock")):
        yield path


def _write_receipt(path: Path, before: bytes, receipt: AdmissionReceipt) -> None:
    with prepared_replacement(path, json.dumps(receipt.fields()) + "\n", mode=0o600) as temporary:
        if path.read_bytes() != before:
            raise staff.StaffRefused(f"{path}: the receipt changed during the document update")
        _ = temporary.replace(path)
        sync_folder(path.parent)


def _document_row(identity: staff.StaffId) -> staff.StaffRow:
    row = staff.authority_snapshot().by_id(identity)
    if row is None or row.kind != "showrunner":
        raise staff.StaffRefused(f"{identity}: the showrunner row is unavailable")
    return row


def _require_moved_file(pending: PendingDocumentRebind) -> None:
    if pending.source.path != pending.destination.path and os.path.lexists(pending.source.path):
        raise staff.StaffRefused(f"{pending.source.path}: the source document still exists")
    pending.verify_file(pending.destination)


def _finish_receipt(path: Path, saved: bytes, receipt: AdmissionReceipt, pending: PendingDocumentRebind) -> AdmissionReceipt:
    renames = receipt.renames
    if pending.source.path != pending.destination.path:
        original = next((item for item in receipt.original_paths() if receipt.current_path(item) == pending.source.path), None)
        if original is None:
            raise staff.StaffRefused(f"{path}: the pending source is outside the receipt")
        renames += (DocumentRename(original, pending.source.path, pending.destination.path),)
    finished = replace(receipt, renames=renames, pending=NoPendingDocumentRebind())
    _write_receipt(path, saved, finished)
    return finished


def prepare_document_rebind(row: staff.StaffRow, destination: DocumentBinding) -> None:
    """Record the current binding and file identity before moving the file."""
    source = row.doc
    if (isinstance(source, staff.DocUnfilled) or source.machine != destination.machine
            or source.machine != remote.local_machine()):
        raise staff.StaffRefused(f"{row.id}: prepare the change on the document machine")
    with _locked_receipt(row.id) as path:
        try:
            saved = path.read_bytes()
        except FileNotFoundError:
            return
        if _document_row(row.id).doc != source:
            raise staff.StaffRefused(f"{row.id}: the staff document changed before the move")
        receipt = _parse_receipt(cast(object, json.loads(saved)), path)
        if isinstance(receipt.pending, PendingDocumentRebind):
            pending = receipt.pending
            if destination != pending.destination or source not in (pending.source, pending.destination):
                raise staff.StaffRefused(f"{row.id}: finish the pending document change to {pending.destination.path}")
            if source == pending.source and os.path.lexists(source.path):
                pending.verify_file(source)
            else:
                _require_moved_file(pending)
            return
        if source == destination:
            return
        if (receipt.machine != source.machine or source.path not in receipt.paths()
                or destination.path != source.path and destination.path in receipt.paths()):
            raise staff.StaffRefused(f"{row.id}: the receipt does not authorize this document change")
        info = source.path.lstat()
        pending = PendingDocumentRebind(source, destination, info.st_dev, info.st_ino)
        pending.verify_file(source)
        _write_receipt(path, saved, replace(receipt, pending=pending))


def complete_document_rebind(row: staff.StaffRow, destination: DocumentBinding) -> staff.StaffRow:
    """Publish the prepared binding and finish the receipt on this machine."""
    if destination.machine != remote.local_machine():
        raise staff.StaffRefused(f"{row.id}: complete the change on the document machine")
    with _locked_receipt(row.id) as path:
        try:
            saved = path.read_bytes()
        except FileNotFoundError:
            return row if row.doc == destination else staff.set_doc(row.id, destination)
        receipt = _parse_receipt(cast(object, json.loads(saved)), path)
        pending = receipt.pending
        if not isinstance(pending, PendingDocumentRebind):
            return row if row.doc == destination else staff.set_doc(row.id, destination, expected=row.doc)
        actual = _document_row(row.id)
        if destination != pending.destination or actual.doc not in (pending.source, pending.destination):
            raise staff.StaffRefused(f"{row.id}: the staff document conflicts with the pending change")
        _require_moved_file(pending)
        for directory in dict.fromkeys((pending.source.path.parent, pending.destination.path.parent)):
            sync_folder(directory)
        if actual.doc == pending.source:
            actual = staff.set_doc(row.id, destination, expected=pending.source)
        _ = _finish_receipt(path, saved, receipt, pending)
        return actual


def verify_document_change(identity: staff.StaffId, source: DocumentBinding,
                           destination: DocumentBinding | staff.DocUnfilled, conditional: bool) -> None:
    """Check a staff write on the receipt machine without taking its lock."""
    path = _receipt_path(identity)
    try:
        saved = path.read_bytes()
    except FileNotFoundError:
        return
    except OSError as error:
        raise staff.StaffUnreadable(f"{identity}: cannot read the admission receipt: {error}") from error
    try:
        receipt = _parse_receipt(cast(object, json.loads(saved)), path)
        pending = receipt.pending
        if isinstance(pending, PendingDocumentRebind):
            if conditional and (source, destination) == (pending.source, pending.destination):
                _require_moved_file(pending)
                return
        elif isinstance(destination, staff.DocUnfilled):
            # A demotion removes the live binding. The receipt can serve a rollback.
            return
        elif (isinstance(destination, staff.WrappedDoc) and destination.machine == source.machine == receipt.machine
              and destination.path == source.path and source.path in receipt.paths()):
            return
        elif (isinstance(source, staff.LiveDoc) and isinstance(destination, staff.LiveDoc)
              and destination.machine == source.machine == receipt.machine
              and len(receipt.paths()) == 2 and {source.path, destination.path} == set(receipt.paths())):
            return
    except (OSError, ValueError, Refusal) as error:
        raise staff.StaffRefused(f"{identity}: cannot verify the document change: {error}") from error
    raise staff.StaffRefused(f"{identity}: an admitted document change requires a prepared move")


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
    if isinstance(receipt.pending, PendingDocumentRebind):
        pending = receipt.pending
        actual = _document_row(production.owner.id)
        if actual.doc != pending.destination or actual.doc != production.owner.doc:
            raise Refusal(f"{production.doc}: finish the pending document change")
        _require_moved_file(pending)
        receipt = _finish_receipt(path, saved, receipt, pending)
    if not receipt.permits(production, machine):
        raise Refusal(f"{production.doc}: the admission receipt names a different document or form")
    return receipt


def require_admission(production: Production) -> None:
    binding = _local_document(production)
    with _locked_receipt(production.owner.id) as path:
        _ = _admit(production, binding.machine, path)


def record_verified_move(production: Production, destination: Path) -> None:
    """Record the verified destination and retain the source for rollback."""
    binding = _local_document(production)
    with _locked_receipt(production.owner.id) as path:
        receipt = _admit(production, binding.machine, path)
        if receipt.basis != "rollout inventory":
            raise Refusal(f"{production.doc}: the move requires a legacy admission")
        if isinstance(receipt.moved_document, Path) and receipt.current_path(receipt.moved_document) == destination:
            return
        if isinstance(receipt.moved_document, Path):
            raise Refusal(f"{production.doc}: the receipt already names {receipt.moved_document}")
        before = path.read_bytes()
        if _parse_receipt(cast(object, json.loads(before)), path) != receipt:
            raise Refusal(f"{path}: the receipt changed during the move")
        _write_receipt(path, before, replace(receipt, moved_document=destination))
