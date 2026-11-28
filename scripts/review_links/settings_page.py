"""Render Settings and store the console's Undo time."""
from __future__ import annotations

import html
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict, cast

from scripts.lib import atomic_file, conf, file_lock
from scripts.review_links import console_theme, page_frame, record_table, send_hold

PAGE_PATH = "/console/settings.html"
SETTINGS_FILE = ".review-link-settings.json"


@dataclass(frozen=True)
class ConfigDefault:
    """Use the Undo time from the config."""


@dataclass(frozen=True)
class SavedPick:
    """Use the Undo time the user selected."""

    seconds: int


type UndoChoice = ConfigDefault | SavedPick


@dataclass(frozen=True)
class UndoReceipt:
    """Keep the current choice and its previous choice until the Undo deadline."""

    current: SavedPick
    previous: UndoChoice
    undo_id: str
    expires_ns: int


@dataclass(frozen=True)
class UndoneReceipt:
    """Keep the restored choice and confirm a repeated Undo."""

    restored: UndoChoice
    undo_id: str


type UndoPreference = UndoChoice | UndoReceipt | UndoneReceipt


class SavedUndoTime(TypedDict):
    seconds: int
    hold: str
    previous: int
    remaining_seconds: float


class SettingsSyncUnconfirmed(OSError):
    """The Settings change is visible, and the folder sync failed."""


class SaveSyncUnconfirmed(SettingsSyncUnconfirmed):
    """The response includes the visible choice and its Undo receipt."""

    def __init__(self, visible: SavedUndoTime) -> None:
        super().__init__("The selected time is active. The server could not confirm the save. Select this time again to retry.")
        self.visible: SavedUndoTime = visible


def undo_choices(config: conf.ConfigRead) -> tuple[int, ...]:
    def parse(value: str) -> tuple[int, ...]:
        choices = tuple(conf.positive_whole(word.strip()) for word in value.split(","))
        if len(set(choices)) != len(choices) or any(choice > 60 for choice in choices):
            raise ValueError("Use distinct Undo times from 1 to 60 seconds.")
        return choices

    choices = config.origin("undo_seconds_choices", parse)
    if isinstance(choices, conf.UsableLiveValue):
        return choices.value
    raise conf.Unusable("The undo_seconds_choices setting is missing or invalid.")


def _choice(value: object) -> UndoChoice:
    return SavedPick(value) if type(value) is int and 0 < value <= 60 else ConfigDefault()


def _read(root: Path) -> UndoPreference:
    try:
        value = cast(object, json.loads((root / SETTINGS_FILE).read_text(encoding="utf-8")))
    except (OSError, ValueError, RecursionError):
        return ConfigDefault()
    if not isinstance(value, dict):
        return ConfigDefault()
    fields = cast(dict[str, object], value)
    current = _choice(fields.get("seconds"))
    undo_id, expires_ns = fields.get("undo_id"), fields.get("expires_ns")
    if isinstance(undo_id, str) and send_hold.valid_hold_id(undo_id):
        if fields.get("undone") is True:
            return UndoneReceipt(current, undo_id)
        if isinstance(current, SavedPick) and type(expires_ns) is int and expires_ns > 0:
            return UndoReceipt(current, _choice(fields.get("previous")), undo_id, expires_ns)
    return current


def _current(preference: UndoPreference) -> UndoChoice:
    if isinstance(preference, UndoReceipt):
        return preference.current
    if isinstance(preference, UndoneReceipt):
        return preference.restored
    return preference


def _effective_seconds(choice: UndoChoice, config: conf.ConfigRead) -> int:
    return choice.seconds if isinstance(choice, SavedPick) else config.whole("undo_seconds")


def _allowed_choice(choice: UndoChoice, choices: tuple[int, ...]) -> UndoChoice:
    if isinstance(choice, SavedPick) and choice.seconds in choices:
        return choice
    return ConfigDefault()


def undo_seconds(root: Path, config: conf.ConfigRead) -> int:
    """Read the saved time, or use the default from the config."""
    choice = _current(_read(root))
    if isinstance(choice, SavedPick):
        try:
            choice = _allowed_choice(choice, undo_choices(config))
        except conf.Unusable:
            choice = ConfigDefault()
    return _effective_seconds(choice, config)


def _write(root: Path, preference: UndoReceipt | UndoneReceipt) -> None:
    current = _current(preference)
    fields: dict[str, object] = {
        "seconds": current.seconds if isinstance(current, SavedPick) else None,
        "undo_id": preference.undo_id,
    }
    if isinstance(preference, UndoReceipt):
        previous = preference.previous
        fields.update(previous=previous.seconds if isinstance(previous, SavedPick) else None,
                      expires_ns=preference.expires_ns)
    else:
        fields["undone"] = True
    try:
        atomic_file.write_text_atomic(root / SETTINGS_FILE, json.dumps(fields) + "\n", new_file_mode=0o600)
    except atomic_file.AtomicDurabilityUnconfirmed as error:
        if _read(root) != preference:
            raise
        raise SettingsSyncUnconfirmed(str(error)) from error


def _sync_folder(root: Path) -> None:
    try:
        atomic_file.sync_folder(root)
    except OSError as error:
        raise SettingsSyncUnconfirmed(str(error)) from error


def _saved_time(receipt: UndoReceipt, choices: tuple[int, ...], config: conf.ConfigRead) -> SavedUndoTime:
    remaining = (receipt.expires_ns - time.time_ns()) / 1_000_000_000 - send_hold.RELEASE_GRACE_SECONDS
    previous = _effective_seconds(_allowed_choice(receipt.previous, choices), config)
    return {"seconds": receipt.current.seconds, "hold": receipt.undo_id, "previous": previous,
            "remaining_seconds": max(0.0, remaining)}


def save(root: Path, config: conf.ConfigRead, value: str) -> SavedUndoTime:
    """Save one choice and keep the earlier choice for Undo."""
    choices = undo_choices(config)
    numbers = {str(seconds): seconds for seconds in choices}
    if value not in numbers:
        raise ValueError("Choose an Undo time shown on the Settings page.")
    seconds = numbers[value]
    with file_lock.exclusive(root / (SETTINGS_FILE + ".lock")):
        saved = _read(root)
        if isinstance(saved, UndoReceipt) and saved.current.seconds == seconds:
            receipt = saved
        else:
            previous = _allowed_choice(_current(saved), choices)
            current = _effective_seconds(previous, config)
            if seconds == current:
                visible: SavedUndoTime = {"seconds": seconds, "hold": "", "previous": current, "remaining_seconds": 0.0}
                if isinstance(saved, UndoneReceipt):
                    try:
                        _sync_folder(root)
                    except SettingsSyncUnconfirmed as error:
                        raise SaveSyncUnconfirmed(visible) from error
                return visible
            expires_ns = time.time_ns() + int((seconds + send_hold.RELEASE_GRACE_SECONDS) * 1_000_000_000)
            receipt = UndoReceipt(SavedPick(seconds), previous, send_hold.new_hold_id(), expires_ns)
        try:
            if receipt is saved:
                _sync_folder(root)
            else:
                _write(root, receipt)
        except SettingsSyncUnconfirmed as error:
            raise SaveSyncUnconfirmed(_saved_time(receipt, choices, config)) from error
        return _saved_time(receipt, choices, config)

def undo(root: Path, undo_id: str) -> send_hold.UndoResult:
    """Restore the previous choice if this change is still current."""
    saved = _read(root)
    if not isinstance(saved, (UndoReceipt, UndoneReceipt)) or saved.undo_id != undo_id:
        return send_hold.UndoResult.MISSING
    with file_lock.exclusive(root / (SETTINGS_FILE + ".lock")):
        saved = _read(root)
        if not isinstance(saved, (UndoReceipt, UndoneReceipt)) or saved.undo_id != undo_id:
            return send_hold.UndoResult.MISSING
        if isinstance(saved, UndoneReceipt):
            _sync_folder(root)
            return send_hold.UndoResult.UNDONE
        if time.time_ns() >= saved.expires_ns:
            return send_hold.UndoResult.ALREADY_SENT
        _write(root, UndoneReceipt(saved.previous, undo_id))
    return send_hold.UndoResult.UNDONE


def render(root: Path, config: conf.ConfigRead, token: str) -> bytes:
    """Render the Settings page in the shared frame."""
    try:
        choices = undo_choices(config)
    except conf.Unusable as error:
        return _document(f'<p class="action-failure" role="alert">{html.escape(str(error))}</p>')
    selected = _effective_seconds(_allowed_choice(_current(_read(root)), choices), config)
    buttons = "".join(f'<button type="submit" name="seconds" value="{seconds}" '
                      + f'aria-pressed="{str(seconds == selected).lower()}">{seconds} s</button>'
                      for seconds in choices)
    control = record_table.labelled_chip_row(
        "Undo time", buttons, filter_key="undo-time",
        label_html='<strong>Undo time</strong><span id="undo-time-help">'
        + 'How long the Undo message stays after you save or send.</span>')
    control = control.replace('role="group"', 'role="group" aria-describedby="undo-time-help"', 1)
    form = (f'<form method="post" action="{PAGE_PATH}" class="card" id="undo-time">'
            + f'<input type="hidden" name="token" value="{html.escape(token, quote=True)}">'
            + control + page_frame.action_failure("settings-error") + '</form>')
    return _document(form, page_frame.HELD_SEND_SCRIPT + page_frame.ACTION_STATUS_SCRIPT + SCRIPT)


def _document(content: str, script: str = "") -> bytes:
    return ('''<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Settings</title><style>'''
            + console_theme.CSS + page_frame.STYLE + '</style>' + record_table.STYLE + '<style>' + STYLE
            + '</style></head><body><main><h1 class="page-title">Settings</h1>'
            + content + '</main><script>' + script
            + page_frame.CONSOLE_READY_SCRIPT + '</script></body></html>').encode("utf-8")


STYLE = '''
:root{scrollbar-gutter:auto}
main{max-width:1124px}
.page-title{margin-bottom:16px}
.card{padding:20px 24px}
.card .record-filter-row{grid-template-columns:minmax(0,1fr) auto;align-items:center;gap:16px;margin:0}
.card .record-filter-row>span{font-size:15px}
.card .record-filter-row strong{display:block;color:var(--ink);font-size:16px;margin-bottom:4px}
.card .record-filter-row>div{padding:4px;border:1px solid var(--line);border-radius:9px;background:var(--bg);gap:3px}
[data-filter-row=undo-time] button{justify-content:center;min-width:56px;border:0;border-radius:5px;background:none}
[data-filter-row=undo-time] button[aria-pressed=true]{background:var(--filter-selected-bg)}
@media(max-width:650px){.card .record-filter-row{grid-template-columns:minmax(0,1fr)}
.card .record-filter-row>div{width:fit-content}}
'''

SCRIPT = r'''
(() => {
 const form=document.getElementById('undo-time'), error=document.getElementById('settings-error');
 const choices=[...form.querySelectorAll('button')];
 const select=seconds=>choices.forEach(button=>button.setAttribute('aria-pressed',String(Number(button.value)===seconds)));
 let toast;
 form.addEventListener('submit',async event=>{
  event.preventDefault();
  if(!event.submitter || form.dataset.saving) return;
  const body=new URLSearchParams(new FormData(form,event.submitter));
  form.dataset.saving='true';
  showActionFailure(error);
  try{
   const started=performance.now();
   const response=await fetch(form.action,{method:'POST',body});
   const result=await response.json();
   if(Number.isInteger(result.seconds)) select(result.seconds);
   const reason=response.ok ? '' : result.reason || 'The Undo time could not be saved.';
   if(result.hold){
    toast?.close();
    toast=undefined;
    const transit=Math.max(0,(performance.now()-started)/1000-result.processing_seconds);
    const remaining=result.remaining_seconds-transit;
    if(reason && remaining<=0){showActionFailure(error,reason);return;}
    showActionFailure(error);
    const current=toast=heldFetch.showSaved({hold:remaining>0 ? result.hold : '',
     seconds:remaining>0 ? remaining : 3,reason});
    current.done.then(undone=>{
     if(toast!==current) return;
     if(undone) select(result.previous);
     else if(reason && error.hidden) showActionFailure(error,reason);
    }).catch(failure=>{
     if(toast!==current) return;
     if(failure.undone) select(result.previous);
     showActionFailure(error,failure.message);
    });
   }else if(reason) showActionFailure(error,reason);
  }catch(failure){
   showActionFailure(error,'The Undo time could not be saved. '+(failure.message || ''));
  }finally{delete form.dataset.saving;}
 });
})();
'''
