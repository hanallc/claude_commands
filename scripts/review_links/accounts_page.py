"""Render the accounts board on the Hana console Accounts page."""
from __future__ import annotations

from dataclasses import dataclass
import html
from pathlib import Path
import re
from statistics import median
import subprocess
import sys
import time

from scripts.accounts import accounts
from scripts.lib import conf
from scripts.review_links import change_page, console_theme, page_frame, review_link

PAGE_NAME = "accounts.html"
PAGE_PATH = f"/{change_page.CONSOLE_FOLDER}/{PAGE_NAME}"
# The child process keeps the status read's stderr capture away from the server's request log.
CHILD = (str(Path(__file__).resolve().parents[1] / "lib/py"), "-m", "scripts.review_links.accounts_page")
TUNE = (str(Path(__file__).resolve().parents[1] / "lib/py"),
        str(Path(__file__).resolve().parents[1] / "lib/tune.py"))


@dataclass(frozen=True)
class ColorSaved:
    """Tune published the account color."""


@dataclass(frozen=True)
class ColorRefused:
    """The request contains an unsupported account or color."""
    reason: str


@dataclass(frozen=True)
class ColorSaveFailed:
    """Tune could not publish the account color."""
    reason: str


def save_color(label: str, value: str) -> ColorSaved | ColorRefused | ColorSaveFailed:
    """Validate the account color and publish it through this checkout's tune."""
    try:
        colors = conf.read("statusline").values()
    except (OSError, ValueError) as error:
        return ColorSaveFailed(" ".join(str(error).split()))
    if f"account.{label}" not in colors:
        return ColorRefused("The account has no configured color.")
    if not re.fullmatch(r"#[0-9a-f]{6}", value):
        return ColorRefused("Use a color in #rrggbb format.")
    try:
        done = subprocess.run((*TUNE, "statusline", f"account.{label}", value,
            "--why", "The user chose this color with the Accounts page color chooser.",
            "--user-words", f"{label} color set to {value} with Save on the console Accounts page"),
            capture_output=True, text=True, check=False)
    except OSError as error:
        return ColorSaveFailed(" ".join(str(error).split()))
    if done.returncode:
        lines = done.stderr.strip().splitlines()
        return ColorSaveFailed(" ".join(lines[-1].split()) if lines else f"Tune exited {done.returncode}.")
    return ColorSaved()


@dataclass(frozen=True)
class AccountsRender:
    content: bytes
    seconds: float
    finished_at: float

    def with_recent_times(self, times: tuple[float, ...]) -> bytes:
        facts = (f'data-page-facts data-render-seconds="{self.seconds:.6f}" '
                 + f'data-read-at="{self.finished_at:.6f}" '
                 + f'data-render-median="{median(times):.6f}" data-render-count="{len(times)}"')
        return self.content.replace(b'data-page-facts', facts.encode(), 1)


def page_url() -> str:
    """Return the fixed Accounts page path for config/console.conf."""
    _ = change_page.console_folder(review_link.review_root().resolve())
    return PAGE_PATH


def _spans(spans: list[accounts.BoardSpan]) -> str:
    parts: list[str] = []
    for span in spans:
        color = accounts.account_color(span.get("color", ""))
        style = f' style="color:{color.css}"' if isinstance(color, accounts.AccountColor) else ""
        parts.append(f'<span class="{span["tone"]}{" bold" if span["kind"] == "bold" else ""}"{style}>{html.escape(span["text"])}</span>')
    return "".join(parts)


def board(reading: accounts.StatusReading) -> str:
    """The accounts board table, then the upcoming moves and the problems."""
    table = accounts.status_board(reading)
    def cell(key: str, spans: list[accounts.BoardSpan]) -> str:
        content = _spans(spans)
        if key == "label" and spans:
            label = spans[0]["text"]
            if "color" in spans[0]:
                color = accounts.account_color(spans[0]["color"])
                css = color.css if isinstance(color, accounts.AccountColor) else "#000000"
                content += (f'<button type="button" class="color-swatch" data-account-color="{html.escape(label, quote=True)}" '
                            + f'data-saved-color="{css.lower()}" style="background:{css}" '
                            + f'aria-label="Change {html.escape(label, quote=True)} color" aria-haspopup="dialog"></button>')
        return content
    head = "".join(f'<th class="{column["align"]}">{html.escape(column["heading"])}</th>' for column in table["columns"])
    rows = "".join(
        ('<tr class="account">' if row["cells"]["label"] else "<tr>")
        + "".join(f'<td class="{column["align"]}">{cell(column["key"], row["cells"][column["key"]])}</td>' for column in table["columns"])
        + "</tr>" for row in table["rows"])
    moves = "".join(f"<li>{html.escape(line)}</li>" for line in reading.upcoming_moves if line)
    problems = "".join(f"<li>{html.escape(note.removeprefix('* '))}</li>" for note in reading.notes)
    return ('<section class="card"><div class="scroll"><table>'
            + f"<thead><tr>{head}</tr></thead><tbody>{rows}</tbody></table></div>"
            + (f'<ul class="moves">{moves}</ul>' if moves else "")
            + (f'<h2>Problems</h2><ul class="problems">{problems}</ul>' if problems else "") + "</section>")


def page() -> bytes:
    """Read the stored accounts status, then render the page."""
    body = board(accounts.read_status(source=accounts.StoredUsageOnly()))
    refresh_seconds = conf.read("review_links").number("page_refresh_seconds")
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            + '<meta name="viewport" content="width=device-width,initial-scale=1"><title>Accounts</title>'
            + f'{STYLE}</head><body><main data-refresh-seconds="{refresh_seconds}">' + page_frame.title("Accounts", "accounts")
            + f'<div data-accounts>{body}</div></main>' + POPUP
            + f"<script>{SCRIPT}</script></body></html>").encode()


def render() -> bytes:
    """Render the page in a child process within `accounts_page_seconds`."""
    return render_with_facts().content


def render_with_facts() -> AccountsRender:
    """Measure the child process and keep its finish time."""
    seconds = conf.read("review_links").number("accounts_page_seconds")
    started = time.perf_counter()
    try:
        done = subprocess.run(CHILD, capture_output=True, timeout=seconds, check=False)
    except subprocess.TimeoutExpired as error:
        raise OSError(f"the page took longer than {seconds:g} s") from error
    elapsed = time.perf_counter() - started
    finished_at = time.time()
    if done.returncode:
        reason = done.stderr.decode(errors="replace").strip().splitlines()
        raise OSError(f"the page render exited {done.returncode}: {reason[-1] if reason else 'no message'}")
    return AccountsRender(done.stdout, elapsed, finished_at)


def main() -> int:
    _ = sys.stdout.buffer.write(page())
    return 0


STYLE = "<style>\n" + console_theme.CSS + page_frame.STYLE + """
h2{margin:16px 0 4px}
table{border-collapse:collapse;font-size:.86rem;font-variant-numeric:tabular-nums}
.card{width:fit-content;max-width:100%}
th,td{padding:5px 0 5px 18px;white-space:nowrap}th:first-child,td:first-child{padding-left:0}
th{color:var(--ink-2);font-weight:600;border-bottom:1px solid var(--line)}
.left{text-align:left}.right{text-align:right}
tr.account td{border-top:1px solid var(--line)}tbody tr:last-child td{border-top:1px solid var(--ink-3)}
tbody tr:hover td{background:var(--hover)}
.bold{font-weight:600}.success{color:var(--room)}.error{color:var(--error)}.warning{color:var(--warning)}
.inactive{color:var(--ink-3)}
.color-swatch{width:18px;height:18px;vertical-align:middle;margin-left:8px;border:1px solid var(--ink-3);border-radius:4px;cursor:pointer}
.color-picker{position:fixed;inset:auto;margin:0;padding:16px;width:260px;max-width:calc(100vw - 16px);
border:1px solid var(--line);border-radius:12px;background:var(--card);color:var(--ink);box-shadow:0 4px 20px var(--edge)}
.color-picker label{display:block;font-weight:600;margin-bottom:10px}
.color-picker input,.color-picker button{font:inherit}
.color-picker input{display:block;width:100%;height:44px;margin-bottom:12px;background:var(--bg);border:1px solid var(--line)}
.color-actions{display:flex;gap:8px}.color-actions button{flex:1;padding:7px;border:1px solid var(--line);border-radius:6px;
background:var(--bg);color:var(--ink);cursor:pointer}.color-actions button:hover{background:var(--hover)}
.color-actions button:disabled{opacity:.6;cursor:wait}.color-picker [role=status]{font-size:.86rem;overflow-wrap:anywhere;margin:10px 0 0}
.color-picker [role=status]:empty{display:none}
.color-swatch:focus-visible,.color-picker :focus-visible{outline:2px solid var(--accent);outline-offset:3px}
/* A thin red or amber digit falls under 4.5:1; the heavier stroke keeps it at the token contrast. */
.error,.warning{font-weight:600}
.moves,.problems{list-style:none;padding:0;margin:12px 0 0;font-size:.86rem;color:var(--ink-2)}
.moves li,.problems li{padding:2px 0}.problems li{color:var(--warning)}.problems li::before{content:"• "}
</style>"""

POPUP = '''<dialog class="color-picker" aria-labelledby="color-title" data-color-picker>
<label id="color-title" for="account-color-value">Account color</label>
<input type="color" id="account-color-value">
<div class="color-actions"><button type="button" data-color-try>Try</button>
<button type="button" data-color-save>Save</button><button type="button" data-color-cancel>Cancel</button></div>
<p role="status" aria-live="polite"></p></dialog>'''

SCRIPT = page_frame.REFRESH_SCRIPT + r"""(() => {
let view = document.querySelector('[data-accounts]');
let newestRequest = 0;
const popup=document.querySelector('[data-color-picker]'), picker=popup.querySelector('input');
const message=popup.querySelector('[role=status]');
let selected=null, saving=false, savedText='';
const hold=()=>selected ? 'An account color is being edited.' : '';
async function refresh(supersede=false,current=()=>true) {
  if (selected) return;
  const request = ++newestRequest;
  const response = await fetch(location.pathname, {cache: 'no-store'});
  if (!response.ok) throw new Error('HTTP ' + response.status);
  const page = new DOMParser().parseFromString(await response.text(), 'text/html');
  const fresh = page.querySelector('[data-accounts]');
  if (request !== newestRequest || !current() || selected) return;
  if (!fresh) throw new Error('The page has no accounts.');
  const top = scrollY;
  view.replaceWith(fresh);view = fresh;
  scrollTo({top, behavior: 'instant'});
  return pageRefreshFacts(page);
}
const updates=createPageRefresh(refresh, () => document.querySelector('main').dataset.refreshSeconds, hold);
function position(){
 if(!selected) return;
 const box=selected.getBoundingClientRect(), size=popup.getBoundingClientRect();
 popup.style.left=Math.max(8,Math.min(box.left,innerWidth-size.width-8))+'px';
 popup.style.top=Math.max(8,Math.min(box.bottom+8,innerHeight-size.height-8))+'px';
}
function paint(value){
 selected.style.background=value;
 selected.previousElementSibling.style.color=value;
}
function close(restore){
 if(restore){paint(selected.dataset.savedColor);selected.previousElementSibling.style.color=savedText;}
 const button=selected;selected=null;popup.close();button.focus({preventScroll:true});updates.changed();
}
document.addEventListener('click',event=>{
 const button=event.target instanceof Element ? event.target.closest('[data-account-color]') : null;
 if(!button || saving) return;
 if(selected) close(true);
 selected=button;savedText=button.previousElementSibling.style.color;
 picker.value=button.dataset.savedColor;
 popup.querySelector('label').textContent='Change '+button.dataset.accountColor+' color';
 message.textContent='';popup.showModal();position();picker.focus();updates.abandon();
});
popup.querySelector('[data-color-try]').addEventListener('click',()=>paint(picker.value));
popup.querySelector('[data-color-cancel]').addEventListener('click',()=>close(true));
popup.addEventListener('cancel',event=>{event.preventDefault();if(!saving) close(true);});
popup.addEventListener('keydown',event=>{
 if(event.key==='Escape'){event.preventDefault();if(!saving) close(true);}
});
popup.querySelector('[data-color-save]').addEventListener('click',async()=>{
 saving=true;paint(picker.value);message.textContent='Saving…';
 popup.querySelectorAll('button,input').forEach(button=>button.disabled=true);
 try{
  const response=await fetch(location.pathname,{method:'POST',
   headers:{'Content-Type':'application/x-www-form-urlencoded'},
   body:new URLSearchParams({label:selected.dataset.accountColor,value:picker.value}).toString()});
  const result=await response.json();
  if(!response.ok) throw new Error(result.reason || 'The color could not be saved.');
  selected.dataset.savedColor=picker.value;close(false);void updates.refresh(true);
 }catch(error){message.textContent=error.message || 'The color could not be saved.';position();}
 finally{saving=false;popup.querySelectorAll('button,input').forEach(button=>button.disabled=false);}
});
addEventListener('resize',position);addEventListener('scroll',position,{passive:true});
})();"""


if __name__ == "__main__":
    raise SystemExit(main())
