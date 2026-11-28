"""Render Settings and store the console's Undo time."""
from __future__ import annotations

import html
import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TypedDict, cast

from scripts.lib import atomic_file, conf, file_lock
from scripts.review_links import console_theme, page_frame, record_table, send_hold

PAGE_PATH = "/console/settings.html"
SETTINGS_FILE = ".review-link-settings.json"


@dataclass(frozen=True)
class UndoPreference:
    """Keep the selected time and the one change that Undo can reverse."""

    seconds: int | None = None
    undo_id: str = ""
    previous: int | None = None
    expires_ns: int = 0
    undone: bool = False


class SavedUndoTime(TypedDict):
    seconds: int
    hold: str
    previous: int


def undo_choices(config: conf.ConfigRead) -> tuple[int, ...]:
    def parse(value: str) -> tuple[int, ...]:
        choices = tuple(conf.positive_whole(word.strip()) for word in value.split(","))
        if len(set(choices)) != len(choices) or any(choice > 60 for choice in choices):
            raise ValueError("Use distinct Undo times from 1 to 60 seconds.")
        return choices

    return config.value("undo_seconds_choices", parse)


def _read(root: Path) -> UndoPreference:
    try:
        value = cast(object, json.loads((root / SETTINGS_FILE).read_text(encoding="utf-8")))
    except (OSError, ValueError, RecursionError):
        return UndoPreference()
    if not isinstance(value, dict):
        return UndoPreference()
    fields = cast(dict[str, object], value)
    seconds, previous = fields.get("seconds"), fields.get("previous")
    undo_id, expires_ns, undone = fields.get("undo_id"), fields.get("expires_ns"), fields.get("undone")
    return UndoPreference(
        seconds=seconds if type(seconds) is int else None,
        undo_id=undo_id if isinstance(undo_id, str) and send_hold.valid_hold_id(undo_id) else "",
        previous=previous if type(previous) is int else None,
        expires_ns=expires_ns if type(expires_ns) is int else 0,
        undone=undone is True,
    )


def _selected(seconds: int | None, config: conf.ConfigRead) -> int | None:
    return seconds if seconds is not None and seconds in undo_choices(config) else None


def _effective_seconds(selected: int | None, config: conf.ConfigRead) -> int:
    return config.whole("undo_seconds") if selected is None else selected


def undo_seconds(root: Path, config: conf.ConfigRead) -> int:
    """Read the saved time, or use the default from the config."""
    return _effective_seconds(_selected(_read(root).seconds, config), config)


def _write(root: Path, preference: UndoPreference) -> None:
    atomic_file.write_text_atomic(root / SETTINGS_FILE, json.dumps(asdict(preference)) + "\n", new_file_mode=0o600)


def save(root: Path, config: conf.ConfigRead, value: str) -> SavedUndoTime:
    """Save one choice and keep the earlier choice for Undo."""
    choices = {str(seconds): seconds for seconds in undo_choices(config)}
    if value not in choices:
        raise ValueError("Choose an Undo time shown on the Settings page.")
    seconds = choices[value]
    with file_lock.exclusive(root / (SETTINGS_FILE + ".lock")):
        previous = _selected(_read(root).seconds, config)
        current = _effective_seconds(previous, config)
        if seconds == current:
            return {"seconds": seconds, "hold": "", "previous": current}
        undo_id = send_hold.new_hold_id()
        expires_ns = time.time_ns() + int((seconds + send_hold.RELEASE_GRACE_SECONDS) * 1_000_000_000)
        _write(root, UndoPreference(seconds, undo_id, previous, expires_ns))
    return {"seconds": seconds, "hold": undo_id, "previous": current}


def undo(root: Path, undo_id: str) -> send_hold.UndoResult:
    """Restore the previous choice if this change is still current."""
    if _read(root).undo_id != undo_id:
        return send_hold.UndoResult.MISSING
    with file_lock.exclusive(root / (SETTINGS_FILE + ".lock")):
        saved = _read(root)
        if saved.undo_id != undo_id:
            return send_hold.UndoResult.MISSING
        if saved.undone:
            return send_hold.UndoResult.UNDONE
        if time.time_ns() >= saved.expires_ns:
            return send_hold.UndoResult.ALREADY_SENT
        _write(root, UndoPreference(saved.previous, undo_id, undone=True))
    return send_hold.UndoResult.UNDONE


def render(root: Path, config: conf.ConfigRead, token: str) -> bytes:
    """Render the Settings page in the shared frame."""
    selected = undo_seconds(root, config)
    buttons = "".join(f'<button type="submit" name="seconds" value="{seconds}" '
                      + f'aria-pressed="{str(seconds == selected).lower()}">{seconds} s</button>'
                      for seconds in undo_choices(config))
    control = record_table.labelled_chip_row(
        "Undo time", buttons, filter_key="undo-time",
        label_html='<strong>Undo time</strong><span id="undo-time-help">'
        + 'How long the Undo message stays after you save or send.</span>')
    control = control.replace('role="group"', 'role="group" aria-describedby="undo-time-help"', 1)
    return ('''<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Settings</title><style>'''
            + console_theme.CSS + page_frame.STYLE + '</style>' + record_table.STYLE + '<style>' + STYLE
            + '</style></head><body><main><h1 class="page-title">Settings</h1>'
            + f'<form method="post" action="{PAGE_PATH}" class="card" id="undo-time">'
            + f'<input type="hidden" name="token" value="{html.escape(token, quote=True)}">'
            + control + page_frame.action_failure("settings-error") + '</form></main><script>'
            + page_frame.HELD_SEND_SCRIPT + page_frame.ACTION_STATUS_SCRIPT + SCRIPT
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
   const response=await fetch(form.action,{method:'POST',body});
   const result=await response.json();
   if(!response.ok) throw new Error(result.reason || 'The Undo time could not be saved.');
   select(result.seconds);
   if(result.hold){
    toast?.close();
    const current=toast=heldFetch.showSaved(result);
    current.done.then(undone=>{
     if(undone && toast===current) select(result.previous);
    }).catch(failure=>{
     if(toast===current) showActionFailure(error,failure.message);
    });
   }
  }catch(failure){
   showActionFailure(error,'The Undo time could not be saved. '+(failure.message || ''));
  }finally{delete form.dataset.saving;}
 });
})();
'''
