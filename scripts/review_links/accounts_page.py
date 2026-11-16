"""Render the console's Accounts page: the accounts board that /showrunner:accounts shows."""
from __future__ import annotations

from dataclasses import dataclass
import html
from pathlib import Path
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


@dataclass(frozen=True)
class AccountsRender:
    content: bytes
    seconds: float
    finished_at: float

    def with_recent_times(self, times: tuple[float, ...]) -> bytes:
        facts = (f'data-load-time data-render-seconds="{self.seconds:.6f}" '
                 + f'data-render-finished="{self.finished_at:.6f}" '
                 + f'data-render-median="{median(times):.6f}" data-render-count="{len(times)}"')
        return self.content.replace(b'data-load-time', facts.encode(), 1)


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
    refresh_seconds = conf.read("review_links").number("page_refresh_seconds")
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            + '<meta name="viewport" content="width=device-width,initial-scale=1"><title>Accounts</title>'
            + f'{STYLE}</head><body><main data-refresh-seconds="{refresh_seconds}"><h1 class="page-title">Accounts</h1>'
            + f'<p class="load-time" data-load-time></p><div data-accounts>{body}</div></main>'
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
.load-time{color:var(--ink-2);font-size:.86rem;margin:0 0 16px}
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

SCRIPT = page_frame.REFRESH_SCRIPT + r"""(() => {
let view = document.querySelector('[data-accounts]');
let timing = document.querySelector('[data-load-time]');
let newestRequest = 0;
function showTime(waited) {
  const age = Math.max(0, Math.floor(Date.now()/1000 - Number(timing.dataset.renderFinished)));
  timing.textContent = `Data from ${age} s ago. Loaded in ${waited.toFixed(2)} s. ` +
    `The last render took ${Number(timing.dataset.renderSeconds).toFixed(2)} s ` +
    `(median ${Number(timing.dataset.renderMedian).toFixed(2)} s of the last ${timing.dataset.renderCount}).`;
}
async function refresh() {
  const request = ++newestRequest;
  try {
    const started = performance.now();
    const response = await fetch(location.pathname, {cache: 'no-store'});
    if (!response.ok) return;
    const source = await response.text(), waited = (performance.now() - started)/1000;
    const page = new DOMParser().parseFromString(source, 'text/html');
    const fresh = page.querySelector('[data-accounts]'), freshTiming = page.querySelector('[data-load-time]');
    if (request !== newestRequest || !fresh || !freshTiming) return;
    const top = scrollY;
    view.replaceWith(fresh);
    view = fresh;
    timing.replaceWith(freshTiming);timing = freshTiming;showTime(waited);
    scrollTo({top, behavior: 'instant'});
  } catch (_) {}
}
function showInitialTime() {
  const navigation = performance.getEntriesByType('navigation')[0];
  showTime(navigation ? (navigation.responseEnd-navigation.requestStart)/1000 : 0);
}
if (document.readyState === 'complete') showInitialTime();
else addEventListener('load', showInitialTime, {once: true});
createPageRefresh(refresh, () => document.querySelector('main').dataset.refreshSeconds);
})();"""


if __name__ == "__main__":
    raise SystemExit(main())
