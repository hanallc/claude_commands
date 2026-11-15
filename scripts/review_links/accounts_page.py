"""Render the console's Accounts page: the accounts board that /showrunner:accounts shows."""
from __future__ import annotations

import html
from pathlib import Path
import subprocess
import sys

from scripts.accounts import accounts
from scripts.lib import conf
from scripts.review_links import change_page, console_theme, review_link

PAGE_NAME = "accounts.html"
PAGE_PATH = f"/{change_page.CONSOLE_FOLDER}/{PAGE_NAME}"
# The child process keeps the status read's stderr capture away from the server's request log.
CHILD = (str(Path(__file__).resolve().parents[1] / "lib/py"), "-m", "scripts.review_links.accounts_page")


def page_url() -> str:
    """Return the fixed Accounts page path for config/console.conf."""
    _ = change_page.console_folder(review_link.review_root().resolve())
    return PAGE_PATH


def _spans(spans: list[accounts.BoardSpan]) -> str:
    return "".join(f'<span class="{span["tone"]}{" bold" if span["kind"] == "bold" else ""}">{html.escape(span["text"])}</span>'
                   for span in spans)


def board(reading: accounts.StatusReading) -> str:
    """The accounts board table, then the upcoming moves and the problems."""
    table = accounts.status_board(reading)
    head = "".join(f'<th class="{column["align"]}">{html.escape(column["heading"])}</th>' for column in table["columns"])
    rows = "".join(
        ('<tr class="account">' if row["cells"]["label"] else "<tr>")
        + "".join(f'<td class="{column["align"]}">{_spans(row["cells"][column["key"]])}</td>' for column in table["columns"])
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
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            + '<meta name="viewport" content="width=device-width,initial-scale=1"><title>Accounts</title>'
            + f'{STYLE}</head><body><main><h1>Accounts</h1><div data-accounts>{body}</div></main>'
            + f"<script>{SCRIPT}</script></body></html>").encode()


def render() -> bytes:
    """Render the page in a child process within `accounts_page_seconds`."""
    seconds = conf.read("review_links").number("accounts_page_seconds")
    try:
        done = subprocess.run(CHILD, capture_output=True, timeout=seconds, check=False)
    except subprocess.TimeoutExpired as error:
        raise OSError(f"the page took longer than {seconds:g} s") from error
    if done.returncode:
        reason = done.stderr.decode(errors="replace").strip().splitlines()
        raise OSError(f"the page render exited {done.returncode}: {reason[-1] if reason else 'no message'}")
    return done.stdout


def main() -> int:
    _ = sys.stdout.buffer.write(page())
    return 0


STYLE = "<style>\n" + console_theme.CSS + """h1{margin:0 0 16px}h2{margin:16px 0 4px}
table{border-collapse:collapse;font-size:.86rem;font-variant-numeric:tabular-nums}
.card{width:fit-content;max-width:100%}
th,td{padding:5px 0 5px 18px;white-space:nowrap}th:first-child,td:first-child{padding-left:0}
th{color:var(--ink-2);font-weight:600;border-bottom:1px solid var(--line)}
.left{text-align:left}.right{text-align:right}
tr.account td{border-top:1px solid var(--line)}tbody tr:last-child td{border-top:1px solid var(--ink-3)}
tbody tr:hover td{background:var(--hover)}
.bold{font-weight:600}.success{color:var(--room)}.error{color:var(--error)}.warning{color:var(--warning)}
.inactive{color:var(--ink-3)}
/* A thin red or amber digit falls under 4.5:1; the heavier stroke keeps it at the token contrast. */
.error,.warning{font-weight:600}
.moves,.problems{list-style:none;padding:0;margin:12px 0 0;font-size:.86rem;color:var(--ink-2)}
.moves li,.problems li{padding:2px 0}.problems li{color:var(--warning)}.problems li::before{content:"• "}
</style>"""

SCRIPT = r"""(() => {
let view = document.querySelector('[data-accounts]');
async function refresh() {
  if (document.visibilityState !== 'visible' || window.frameElement?.hidden === true) return;
  try {
    const response = await fetch(location.pathname, {cache: 'no-store'});
    if (!response.ok) return;
    const fresh = new DOMParser().parseFromString(await response.text(), 'text/html').querySelector('[data-accounts]');
    if (!fresh) return;
    const top = scrollY;
    view.replaceWith(fresh);
    view = fresh;
    scrollTo({top, behavior: 'instant'});
  } catch (_) {}
}
addEventListener('message', event => {
  if (event.source === parent && event.origin === location.origin && event.data === 'console-shown') refresh();
});
document.addEventListener('visibilitychange', refresh);
})();"""


if __name__ == "__main__":
    raise SystemExit(main())
