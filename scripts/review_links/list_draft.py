"""Define the saved claim for a Scheduled List draft."""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Literal, cast

from scripts.production import record_store, staff
from scripts.review_links import new_item_dialog


def list_records(keeper: staff.StaffId, error_type: type[ValueError] = ValueError) -> record_store.NumberedRecords:
    return record_store.NumberedRecords(keeper, "list", "L", "list item", error_type)


@dataclass(frozen=True)
class ListAssignmentNotice:
    recipient: staff.StaffId
    role: Literal["producer", "showrunner"]
    text: str
    producer_name: str = ""


@dataclass(frozen=True)
class ListDraftClaim:
    """The data needed to recover a Scheduled save."""

    payload_hash: str
    keeper: staff.StaffId
    assignment: str
    readback: tuple[str, str]
    notices: tuple[ListAssignmentNotice, ...] = ()
    notice_warning: str = ""

    def fields(self) -> dict[str, object]:
        return cast(dict[str, object], asdict(self))


@dataclass(frozen=True)
class NewItemDraft:
    """Identify the Scheduled draft that created a List item."""

    id: new_item_dialog.ScheduledDraftId
    claim: ListDraftClaim

    def stored(self) -> str:
        return json.dumps({"request_id": self.id.value, **self.claim.fields()}, ensure_ascii=False)


class NoNewItemDraft(Enum):
    ABSENT = "no New item draft"


type ListDraftOrigin = NewItemDraft | NoNewItemDraft


def read_claim(fields: dict[str, object], payload_hash: str) -> ListDraftClaim:
    keeper, assignment, readback = (fields.get(key) for key in ("keeper", "assignment", "readback"))
    warning = fields.get("notice_warning", "")
    if not (isinstance(keeper, str) and isinstance(assignment, str) and isinstance(warning, str)
            and isinstance(readback, list)):
        raise ValueError("Invalid List draft details.")
    if not keeper or Path(keeper).name != keeper or keeper in (".", ".."):
        raise ValueError("Invalid List keeper staff ID.")
    lines = cast(list[object], readback)
    if len(lines) != 2 or not isinstance(lines[0], str) or not isinstance(lines[1], str):
        raise ValueError("Invalid List readback.")
    raw_notices = fields.get("notices")
    if not isinstance(raw_notices, list):
        raise ValueError("Invalid List draft notices.")
    notices: list[ListAssignmentNotice] = []
    for value in cast(list[object], raw_notices):
        if not isinstance(value, dict):
            raise ValueError("Invalid List draft notice.")
        notice = cast(dict[str, object], value)
        recipient, role, message = (notice.get(key) for key in ("recipient", "role", "text"))
        producer_name = notice.get("producer_name")
        if (not isinstance(recipient, str) or not isinstance(message, str) or not isinstance(producer_name, str)
                or role not in ("producer", "showrunner")):
            raise ValueError("Invalid List draft notice.")
        notices.append(ListAssignmentNotice(staff.StaffId(recipient), role, message, producer_name))
    return ListDraftClaim(payload_hash, staff.StaffId(keeper), assignment, (lines[0], lines[1]), tuple(notices), warning)


def read_list_draft(header: dict[str, str]) -> ListDraftOrigin:
    """Read the Scheduled draft claim from a List item header."""
    stored = header.get("new_item_draft_id")
    if stored is None:
        return NoNewItemDraft.ABSENT
    raw = cast(object, json.loads(stored))
    if not isinstance(raw, dict):
        raise ValueError("Invalid New item draft.")
    fields = cast(dict[str, object], raw)
    draft_id = new_item_dialog.read_scheduled_draft_id(fields.get("request_id"))
    payload_hash = fields.get("payload_hash")
    if not isinstance(payload_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", payload_hash):
        raise ValueError("Invalid List draft fingerprint.")
    return NewItemDraft(draft_id, read_claim(fields, payload_hash))
