"""Render the accounts board on the Hana console Accounts page."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import StrEnum
import html
import json
import re
from pathlib import Path
from statistics import median
import subprocess
import sys
import time
from typing import TypedDict, cast

from scripts.accounts import accounts, next_moves
from scripts.lib import atomic_file, conf, file_lock, waiting_rows
from scripts.review_links import change_page, console_theme, page_frame, review_link

PAGE_NAME = "accounts.html"
SWATCH_COLUMNS = ("label", "roles")
PAGE_PATH = f"/{change_page.CONSOLE_FOLDER}/{PAGE_NAME}"
# The child process keeps the status read's stderr capture away from the server's request log.
CHILD = (str(Path(__file__).resolve().parents[1] / "lib/py"), "-m", "scripts.review_links.accounts_page")
TUNE = (str(Path(__file__).resolve().parents[1] / "lib/py"),
        str(Path(__file__).resolve().parents[1] / "lib/tune.py"))


@dataclass(frozen=True)
class Saved:
    """The page change is saved."""


@dataclass(frozen=True)
class Refused:
    """The request names an unsupported account, color or group."""
    reason: str


@dataclass(frozen=True)
class SaveFailed:
    """The checked change could not be saved."""
    reason: str


QUOTA_CLEAR_FILE = ".accounts-quota-cleared.json"


class QuotaLevel(StrEnum):
    NONE = "none"
    WARNING = "warning"
    URGENT = "urgent"


@dataclass(frozen=True)
class QuotaAlertSettings:
    warning: int
    urgent: int
    same_window_seconds: int

    def level(self, left: float) -> QuotaLevel:
        if 0 <= left <= self.urgent:
            return QuotaLevel.URGENT
        if self.urgent < left <= self.warning:
            return QuotaLevel.WARNING
        return QuotaLevel.NONE

    def same_weekly_window(self, first_reset: str, second_reset: str) -> bool:
        if not first_reset or not second_reset:
            return first_reset == second_reset
        try:
            first = datetime.fromisoformat(first_reset)
            second = datetime.fromisoformat(second_reset)
        except ValueError:
            return False
        return (first.tzinfo is not None and second.tzinfo is not None
                and abs(first - second).total_seconds() < self.same_window_seconds)


class ClearedQuota(TypedDict):
    level: QuotaLevel
    reset: str


@dataclass(frozen=True)
class QuotaAlert:
    level: QuotaLevel
    left: float
    reset: str

    @property
    def name(self) -> str:
        return f"{self.level.title()}: {round(self.left)}% left"


class NavQuota(TypedDict):
    level: QuotaLevel
    name: str


class InvalidQuotaThresholds(ValueError):
    """The warning threshold must exceed the urgent threshold."""


class UnreadableQuotaState(ValueError):
    """The clear records could not be read or parsed."""


def quota_alert_settings() -> QuotaAlertSettings:
    settings = conf.read("review_links")
    warning = settings.whole("accounts_quota_warning_percent")
    urgent = settings.whole("accounts_quota_urgent_percent")
    if warning <= urgent:
        raise InvalidQuotaThresholds("accounts_quota_warning_percent must be greater than accounts_quota_urgent_percent.")
    return QuotaAlertSettings(warning, urgent, settings.whole("accounts_quota_same_window_seconds"))


def quota_state(root: Path) -> Path:
    return root / change_page.CONSOLE_FOLDER / QUOTA_CLEAR_FILE


def account_label(label: str) -> bool:
    return re.fullmatch(r"(?:claude|codex)[1-9][0-9]*|(?:~)?/[a-zA-Z0-9_./ -]+", label) is not None


def cleared_quotas(path: Path) -> dict[str, ClearedQuota]:
    try:
        return _parse_cleared_quotas(cast(object, json.loads(path.read_text(encoding="utf-8"))))
    except FileNotFoundError:
        return {}
    except OSError as error:
        raise UnreadableQuotaState(f"Cannot read {path}: {error.strerror or 'read failed'}.") from error
    except ValueError as error:
        raise UnreadableQuotaState(f"Invalid quota clear state in {path}.") from error


def _parse_cleared_quotas(value: object) -> dict[str, ClearedQuota]:
    if not isinstance(value, dict):
        raise ValueError("The cleared quota file must contain an object.")
    cleared: dict[str, ClearedQuota] = {}
    for label, entry in cast(dict[str, object], value).items():
        if not account_label(label) or not isinstance(entry, dict):
            raise ValueError("The cleared quota file has an invalid account entry.")
        fields = cast(dict[str, object], entry)
        level, reset = fields.get("level"), fields.get("reset")
        if not isinstance(level, str) or level not in (QuotaLevel.WARNING, QuotaLevel.URGENT) or not isinstance(reset, str):
            raise ValueError("The cleared quota file has an invalid level or reset.")
        if reset and datetime.fromisoformat(reset).tzinfo is None:
            raise ValueError("The quota reset needs a time zone.")
        cleared[label] = {"level": QuotaLevel(level), "reset": reset}
    return cleared


def active_quotas(quotas: Mapping[str, accounts.WeeklyQuota], settings: QuotaAlertSettings,
                  cleared: Mapping[str, ClearedQuota]) -> dict[str, QuotaAlert]:
    alerts: dict[str, QuotaAlert] = {}
    for label, quota in quotas.items():
        level = settings.level(quota.left)
        if level is QuotaLevel.NONE or not account_label(label):
            continue
        reset = quota.resets_at.astimezone(timezone.utc).isoformat() if isinstance(quota.resets_at, datetime) else ""
        receipt = cleared.get(label)
        if (receipt is not None and (not reset or settings.same_weekly_window(receipt["reset"], reset))
                and (receipt["level"] is QuotaLevel.URGENT or receipt["level"] is level)):
            continue
        alerts[label] = QuotaAlert(level, quota.left, reset)
    return alerts


def nav_quota(root: Path) -> NavQuota:
    cleared = cleared_quotas(quota_state(root))
    settings = quota_alert_settings()
    alerts = active_quotas(accounts.stored_weekly_quotas(datetime.now(timezone.utc)), settings, cleared)
    if not alerts:
        return {"level": QuotaLevel.NONE, "name": ""}
    alert = min(alerts.values(), key=lambda alert: (alert.level is not QuotaLevel.URGENT, alert.left))
    return {"level": alert.level, "name": alert.name}


def clear_quota(root: Path, label: str, level: str, reset: str) -> Saved | Refused | SaveFailed:
    """Clear the alert if the request names the current level and window."""
    if not account_label(label) or level not in (QuotaLevel.WARNING, QuotaLevel.URGENT):
        return Refused("Use an account label and a warning or urgent level.")
    path = quota_state(root)
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with file_lock.exclusive(path.with_suffix(".lock")):
            cleared = cleared_quotas(path)
            settings = quota_alert_settings()
            current = active_quotas(accounts.stored_weekly_quotas(datetime.now(timezone.utc)), settings, {})
            alert = current.get(label)
            if alert is None or alert.level != level or not settings.same_weekly_window(alert.reset, reset):
                return Refused("The quota alert changed. Read the Accounts page again.")
            receipt = cleared.get(label)
            saved_reset = alert.reset or (receipt["reset"] if receipt is not None else "")
            if (receipt and receipt["level"] is QuotaLevel.URGENT
                    and settings.same_weekly_window(receipt["reset"], saved_reset)):
                return Saved()
            cleared[label] = {"level": alert.level, "reset": saved_reset}
            atomic_file.write_text_atomic(path, json.dumps(cleared, sort_keys=True) + "\n", new_file_mode=0o600)
    except (UnreadableQuotaState, InvalidQuotaThresholds) as error:
        return SaveFailed(str(error))
    except (OSError, ValueError):
        return SaveFailed("The cleared quota could not be saved.")
    return Saved()


QUOTA_ICONS = {QuotaLevel.WARNING: "triangle-alert", QuotaLevel.URGENT: "octagon-alert"}


def quota_icon(level: QuotaLevel) -> str:
    return page_frame.icon_svg(QUOTA_ICONS[level])


def _quota_button(label: str, alert: QuotaAlert) -> str:
    return (f'<button type="button" class="quota-icon" data-quota-level="{alert.level}" '
            + f'data-quota-account="{html.escape(label, quote=True)}" '
            + f'data-quota-reset="{html.escape(alert.reset, quote=True)}" '
            + f'aria-label="{alert.name}" aria-haspopup="dialog" aria-controls="quota-panel" aria-expanded="false">'
            + quota_icon(alert.level) + '</button>')


def save_color(key: str, value: str) -> Saved | Refused | SaveFailed:
    """Validate the color and publish it through this checkout's tune."""
    try:
        if host := waiting_rows.model_color_host(key):
            return Refused(f".{host} is a host override, not a model color.")
        colors = conf.read("statusline").values()
    except (OSError, ValueError) as error:
        return SaveFailed(" ".join(str(error).split()))
    configured = key in colors and (key.startswith(("account.", "model.")) or key in accounts.ROLE_COLOR_KEYS.values())
    if not configured and not waiting_rows.model_color_key("statusline", key):
        return Refused("The page has no configured color for that key.")
    if not waiting_rows.model_color_value(value):
        return Refused("Use a color in #rrggbb format.")
    try:
        done = subprocess.run((*TUNE, "statusline", key, value,
            "--why", "The user chose this color with the Accounts page color chooser.",
            "--user-words", f"{key} set to {value} with Save on the console Accounts page"),
            capture_output=True, text=True, check=False)
    except OSError as error:
        return SaveFailed(" ".join(str(error).split()))
    if done.returncode:
        lines = done.stderr.strip().splitlines()
        return SaveFailed(" ".join(lines[-1].split()) if lines else f"Tune exited {done.returncode}.")
    return Saved()


def save_next_move(group: str, target: str) -> Saved | Refused | SaveFailed:
    """Make the target the group's first next move at the configured percent."""
    if group not in ("units", "showrunners", "producers"):
        return Refused("Choose units, showrunners, or producers.")
    try:
        _ = accounts.claude_account_dir(target)
    except accounts.AccountsError as error:
        return Refused(" ".join(str(error).split()))
    try:
        _ = next_moves.set_first_next_move(group, next_moves.console_next_move_percent(), target)
    except (OSError, ValueError) as error:
        return SaveFailed(" ".join(str(error).split()))
    return Saved()


def clear_next_move(group: str) -> Saved | Refused | SaveFailed:
    """Remove the group's first entry and retain later entries."""
    if group not in ("units", "showrunners", "producers"):
        return Refused("Choose units, showrunners, or producers.")
    try:
        if not any(entry.group == group for entry in next_moves.read_next_moves()):
            return Refused("The group has no next move.")
        next_moves.remove_next_move(group, next_moves.FIRST_GROUP_POSITION)
    except (OSError, ValueError) as error:
        return SaveFailed(" ".join(str(error).split()))
    return Saved()


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
        if "color_key" in span:
            style += f' data-color-text="{html.escape(span["color_key"], quote=True)}"'
        parts.append(f'<span class="{span["tone"]}{" bold" if span["kind"] == "bold" else ""}"{style}>{html.escape(span["text"])}</span>')
    return "".join(parts)


def _row_open(row: accounts.BoardRow) -> str:
    """Open a table line; a line of a Claude group carries the account and group its move menu needs."""
    group = row.get("group")
    move = (f' data-move-account="{html.escape(group["account"], quote=True)}" data-move-group="{group["group"]}"'
            if group else "")
    return f'<tr class="account"{move}>' if row["cells"]["label"] else f"<tr{move}>"


def _next_cell(row: accounts.BoardRow, reading: accounts.StatusReading, labels: dict[str, accounts.BoardSpan]) -> str:
    """Render the selected group's target or the reason this line has no target."""
    if row["cells"]["label"] and row["cells"]["label"][0]["text"] == "total":
        return ""
    move = accounts.account_board_next_move(row, reading.first_next_moves)
    editable = row.get("group")
    if move == "unselected":
        return ('<span class="next-muted next-minus" title="Not the selected account for a role">'
                + page_frame.icon_svg("minus") + '</span>')
    target = ""
    if isinstance(move, accounts.BoardNextMoveTarget):
        target = move.label
        content = _spans([labels.get(target, accounts.BoardSpan(text=target, tone="default", kind="bold"))])
    elif isinstance(move, accounts.NextMovesUnavailable):
        content = '<span class="next-muted" title="' + html.escape(move.reason, quote=True) + '">unavailable</span>'
    elif move == "none":
        content = '<span class="next-muted">none</span>'
    else:
        content = ('<span class="next-muted" title="Codex workers move between the Codex accounts by themselves">auto</span>')
    if editable:
        content += (f'<button type="button" class="next-button" data-next-button data-next-target="{html.escape(target, quote=True)}" '
                    + f'aria-label="Choose the next account for {editable["group"]}" aria-haspopup="menu" aria-expanded="false">'
                    + page_frame.icon_svg("chevron-down") + '</button>')
    return content


def _swatch(spans: list[accounts.BoardSpan]) -> str:
    if not spans or "color_key" not in spans[0]:
        return ""
    name = html.escape(spans[0]["text"], quote=True)
    color = accounts.account_color(spans[0].get("color", ""))
    css = color.css.lower() if isinstance(color, accounts.AccountColor) else ""
    background = f' style="background:{css}"' if css else ""
    return (f'<button type="button" class="color-swatch" data-color-key="{html.escape(spans[0]["color_key"], quote=True)}" '
            + f'data-color-name="{name}" data-saved-color="{css}"{background} '
            + f'aria-label="Change {name} color" aria-haspopup="dialog"></button>')


def _sort_heading(column: accounts.BoardColumn, default: str, descending_columns: tuple[str, ...]) -> str:
    key = column["heading"]
    if not key:
        return f'<th class="{column["align"]}"></th>'
    direction = "ascending" if key == default else "none"
    descending = " data-sort-descending" if key in descending_columns else ""
    label = html.escape(key)
    arrow = '<span class="sort-arrow" aria-hidden="true">▲</span>'
    content = arrow + label if column["align"] == "right" else label + arrow
    return (f'<th class="{column["align"]}" data-{default}-sort="{label}" tabindex="0" aria-sort="{direction}"{descending}>'
            + f'<span class="sort-label">{content}</span></th>')


def board(reading: accounts.StatusReading, alerts: Mapping[str, QuotaAlert] | None = None) -> str:
    """Render the accounts table and its problems."""
    table = accounts.status_board(reading)
    claude = {row.label for row in reading.rows if row.tool == "claude"}
    labels = {line["cells"]["label"][0]["text"]: line["cells"]["label"][0]
              for line in table["rows"] if line["cells"]["label"]}
    def target(spans: list[accounts.BoardSpan], left: list[accounts.BoardSpan]) -> list[str]:
        color = accounts.account_color(spans[0].get("color", ""))
        return [spans[0]["text"], "".join(span["text"] for span in left),
                color.css if isinstance(color, accounts.AccountColor) else "", left[0]["tone"] if left else "default"]
    targets = [target(spans, row["cells"]["left"]) for row in table["rows"]
               if (spans := row["cells"]["label"]) and spans[0]["text"] in claude]
    percent = next_moves.format_next_move_percent(next_moves.console_next_move_percent())
    # A headerless swatch column follows each column whose first span can carry a configured color.
    def heading(column: accounts.BoardColumn) -> str:
        return _sort_heading(column, "account", accounts.ACCOUNT_BOARD_DESCENDING_SORTS)
    next_heading = heading(accounts.BoardColumn(key="next", heading="next", align="left"))
    head = "".join(heading(column)
                   + ('<th class="left"></th>' if column["key"] in SWATCH_COLUMNS else "")
                   + (next_heading if column["key"] == "roles" else "") for column in table["columns"])
    def cell(row: accounts.BoardRow, key: str) -> str:
        content = _spans(row["cells"][key])
        label = row["cells"]["label"]
        if key == "left" and alerts and label and (alert := alerts.get(label[0]["text"])) is not None:
            return _quota_button(label[0]["text"], alert) + content
        return content

    rows = "".join(
        _row_open(row)
        + "".join(f'<td class="{column["align"]}"{" data-account-name" if column["key"] == "label" else ""}>'
                  + f'{cell(row, column["key"])}</td>'
                  + (f'<td class="left">{_swatch(row["cells"][column["key"]])}</td>' if column["key"] in SWATCH_COLUMNS else "")
                  + (f'<td class="left next-cell">{_next_cell(row, reading, labels)}</td>' if column["key"] == "roles" else "")
                  for column in table["columns"])
        + "</tr>" for row in table["rows"])
    problems = "".join(f"<li>{html.escape(note.removeprefix('* '))}</li>" for note in reading.notes)
    return (f'<section class="card" data-move-percent="{percent}" data-sort-table="accounts" '
            + f'data-sort-orders="{html.escape(json.dumps(accounts.account_board_sort_orders(reading, table)), quote=True)}" '
            + f'data-move-targets="{html.escape(json.dumps(targets), quote=True)}"><div class="scroll"><table>'
            + f"<thead><tr>{head}</tr></thead><tbody>{rows}</tbody></table></div>"
            + (f'<h2>Problems</h2><ul class="problems">{problems}</ul>' if problems else "") + "</section>")


def _model_cell(row: accounts.ModelsBoardRow, column: accounts.BoardColumn) -> str:
    key = column["key"]
    spans = row["cells"][key]
    attributes = ""
    if names := row["session_names"].get(key):
        tooltip = html.escape("\n".join(names), quote=True)
        label = html.escape(f'{spans[0]["text"]}: ' + ", ".join(names), quote=True)
        cell_id = html.escape(f'model-count-{row["model_name"]}-{row["effort"]}-{key}', quote=True)
        attributes = f' id="{cell_id}" tabindex="0" aria-label="{label}" data-model-sessions="{tooltip}"'
    return f'<td class="{column["align"]}"{attributes}>{_spans(spans)}</td>'


def models_board(reading: accounts.StatusReading) -> str:
    table = accounts.models_board(reading, accounts.role_colors())
    head = "".join(_sort_heading(column, "model", accounts.MODEL_COUNT_COLUMNS)
                   + ('<th class="left"></th>' if column["key"] == "model" else "")
                   for column in accounts.MODEL_COLUMNS)
    rows = "".join("<tr>" + "".join(
        _model_cell(row, column)
        + (f'<td class="left">{_swatch(row["cells"]["model"])}</td>' if column["key"] == "model" else "")
        for column in accounts.MODEL_COLUMNS) + "</tr>" for row in table.rows)
    orders = html.escape(json.dumps(table.orders), quote=True)
    return (f'<section class="card models-card" data-sort-table="models" data-sort-orders="{orders}">'
            + f'<div class="scroll"><table aria-label="Models"><thead><tr>{head}</tr></thead>'
            + f'<tbody>{rows}</tbody></table></div></section>')


def page() -> bytes:
    """Read the stored accounts status, then render the page."""
    reading = accounts.read_status(source=accounts.StoredUsageOnly(), include_models=True)
    try:
        cleared = cleared_quotas(quota_state(review_link.review_root()))
        alerts = active_quotas(accounts.weekly_quotas(reading.rows), quota_alert_settings(), cleared)
    except (UnreadableQuotaState, InvalidQuotaThresholds) as error:
        reading = replace(reading, notes=(*reading.notes, str(error)))
        alerts = {}
    body = board(reading, alerts) + models_board(reading)
    refresh_seconds = conf.read("review_links").number("page_refresh_seconds")
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            + '<meta name="viewport" content="width=device-width,initial-scale=1"><title>Accounts</title>'
            + f'{STYLE}</head><body><main data-refresh-seconds="{refresh_seconds}">' + page_frame.title("Accounts", "accounts")
            + f'<div data-accounts>{body}</div></main>' + POPUP + MENU + QUOTA_PANEL
            + '<div class="model-tooltip" hidden aria-hidden="true"></div>'
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
.card{--column-gap:1.125rem;--table-font-size:.86rem;width:fit-content;max-width:100%;padding-right:var(--column-gap)}
.models-card{margin-top:24px}
.model-tooltip{position:fixed;width:max-content;padding:7px 11px;background:var(--model-tooltip-bg);
color:var(--model-tooltip-ink);border-radius:6px;
font-size:12px;font-weight:500;box-shadow:0 3px 12px #0002;z-index:5;white-space:pre;overflow:auto}
:root{--model-tooltip-bg:#192339;--model-tooltip-ink:#fff}
@media(prefers-color-scheme:dark){:root:not([data-theme="light"]){--model-tooltip-bg:#e4eaf4;--model-tooltip-ink:#10151e}}
:root[data-theme="dark"]{--model-tooltip-bg:#e4eaf4;--model-tooltip-ink:#10151e}
[data-model-sessions]:focus-visible{outline:2px solid var(--accent);outline-offset:-2px}
table{border-collapse:collapse;font-size:var(--table-font-size);font-variant-numeric:tabular-nums}
/* Let the last sort arrow use the right padding of the card. */
.card .scroll{margin-right:calc(-1 * var(--column-gap));padding-right:var(--column-gap)}
th,td{padding:5px 0 5px var(--column-gap);white-space:nowrap}th:first-child,td:first-child{padding-left:0}
th{color:var(--ink-2);font-weight:600;border-bottom:1px solid var(--line)}
th:is([data-account-sort],[data-model-sort]){cursor:pointer}
th:is([data-account-sort],[data-model-sort]):focus-visible{outline:2px solid var(--accent);outline-offset:-2px}
.sort-label{position:relative}
.sort-arrow{--sort-arrow-gap:calc(var(--table-font-size) * .25);position:absolute;top:50%;
left:calc(100% + var(--sort-arrow-gap));transform:translateY(-50%);width:var(--table-font-size);text-align:center;visibility:hidden}
th.right .sort-arrow{left:auto;right:calc(100% + var(--sort-arrow-gap))}
th[aria-sort=ascending] .sort-arrow,th[aria-sort=descending] .sort-arrow{visibility:visible}
.left{text-align:left}.right{text-align:right}
tbody tr td{border-top:1px solid var(--line)}tbody tr:last-child td{border-top:1px solid var(--ink-3)}
tbody tr:hover td{background:var(--hover)}
.bold{font-weight:600}.success{color:var(--room)}.error{color:var(--error)}.warning{color:var(--warning)}
.inactive{color:var(--ink-3)}
.color-swatch{width:18px;height:18px;vertical-align:middle;background:var(--ink);border:1px solid var(--ink-3);border-radius:4px;cursor:pointer}
.color-picker{position:fixed;inset:auto;margin:0;padding:16px;width:280px;box-sizing:border-box;max-width:calc(100vw - 16px);
max-height:calc(100vh - 16px);overflow:auto;
border:1px solid var(--line);border-radius:12px;background:var(--card);color:var(--ink);box-shadow:0 4px 20px var(--edge)}
.color-picker label{display:block;margin:10px 0 6px}.color-title{font-weight:600;margin-bottom:12px}
.color-picker input,.color-picker button{font:inherit}
.color-area{position:relative;height:132px;border:1px solid var(--ink-3);border-radius:6px;touch-action:none;
background:linear-gradient(to top,#000,transparent),linear-gradient(to right,#fff,transparent),var(--picked-hue)}
.color-cursor{position:absolute;width:10px;height:10px;box-sizing:border-box;border:2px solid #fff;
border-radius:50%;box-shadow:0 0 0 1px #000;transform:translate(-50%,-50%);pointer-events:none}
.color-picker .sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip-path:inset(50%)}
.color-picker input[type=range]{display:block;width:100%;height:16px;margin:4px 0;appearance:none;
border:1px solid var(--ink-3);border-radius:8px;box-sizing:border-box;
background:linear-gradient(to right,#f00,#ff0,#0f0,#0ff,#00f,#f0f,#f00)}
.color-picker input[type=range]::-moz-range-thumb{width:12px;height:22px;border:2px solid var(--ink);
border-radius:4px;background:var(--card)}
.color-picker input[type=range]::-webkit-slider-thumb{appearance:none;width:12px;height:22px;border:2px solid var(--ink);
border-radius:4px;background:var(--card)}
.color-preview{height:56px;margin-top:12px;border:1px solid var(--ink-3);border-radius:6px}
.color-picker input[type=text]{box-sizing:border-box;width:100%;padding:8px;margin-bottom:12px;border-radius:6px;
background:var(--bg);color:var(--ink);border:1px solid var(--line)}
.color-actions{display:flex;gap:8px}.color-actions button{flex:1;padding:7px;border:1px solid var(--line);border-radius:6px;
background:var(--bg);color:var(--ink);cursor:pointer}
.color-actions button:disabled{opacity:.5;cursor:default}
.color-save-error{color:var(--error);overflow-wrap:anywhere}
.color-swatch:focus-visible,.color-picker :focus-visible{outline:2px solid var(--accent);outline-offset:3px}
/* A thin red or amber digit falls under 4.5:1; the heavier stroke keeps it at the token contrast. */
.error,.warning{font-weight:600}
.next-cell{white-space:nowrap}.next-muted{color:var(--ink-2)}
.next-button{display:inline-flex;vertical-align:middle;margin-left:5px;padding:2px;border:0;border-radius:4px;
background:transparent;color:var(--ink-2);cursor:pointer}
.next-button:focus-visible{background:var(--hover)}
.next-button svg,.next-minus svg{width:14px;height:14px;vertical-align:middle}
.move-menu button[aria-checked=true]{background:var(--hover)}
.move-menu{position:fixed;margin:0;padding:6px;width:max-content;min-width:220px;max-width:calc(100vw - 16px);box-sizing:border-box;
border:1px solid var(--line);border-radius:10px;background:var(--card);color:var(--ink);box-shadow:0 4px 20px var(--edge);
font-size:.86rem;font-variant-numeric:tabular-nums}
.move-menu[hidden]{display:none}
.move-title{padding:6px 10px 8px;margin-bottom:4px;color:var(--ink-2);border-bottom:1px solid var(--line)}
.move-menu button{display:flex;justify-content:space-between;gap:24px;width:100%;padding:6px 10px;border:0;border-radius:6px;
background:none;color:inherit;font:inherit;text-align:left;cursor:pointer}
.move-menu button:focus-visible{background:var(--hover);outline:none}
.move-name{font-weight:600}.move-left{color:var(--ink-2)}.move-left.warning{color:var(--warning)}.move-left.error{color:var(--error)}
.move-empty{padding:6px 10px;color:var(--ink-3)}
.quota-icon{display:inline-grid;place-items:center;vertical-align:middle;width:22px;height:22px;margin:-3px 5px -3px 0;
padding:2px;border:0;border-radius:5px;background:transparent;cursor:pointer}
.quota-icon svg{width:16px;height:16px}
[data-quota-level=warning]{color:var(--warning)}[data-quota-level=urgent]{color:var(--error)}
.quota-icon:hover{background:var(--hover)}
.quota-icon[aria-expanded=true]{box-shadow:0 0 0 2px var(--accent)}
.quota-icon:focus-visible,.quota-panel button:focus-visible{outline:2px solid var(--accent);outline-offset:1px}
.quota-panel{position:fixed;z-index:1;width:250px;max-width:calc(100vw - 16px);padding:11px 14px;
border:1px solid var(--line);border-radius:11px;background:var(--hover);color:var(--ink);box-shadow:0 4px 20px var(--edge);font-size:.86rem}
.quota-panel p{color:var(--ink-2);margin:2px 0 10px;font-size:.76rem}
.quota-panel button{border:0;border-radius:7px;padding:5px 14px;background:var(--filter-selected-bg);
color:var(--filter-selected-ink);font:inherit;font-weight:600;cursor:pointer}
.problems{list-style:none;padding:0;margin:12px 0 0;font-size:.86rem}
.problems li{padding:2px 0;color:var(--warning)}.problems li::before{content:"• "}
</style>"""

POPUP = '''<dialog class="color-picker" aria-labelledby="color-title" data-color-picker>
<div id="color-title" class="color-title">Color</div>
<div class="color-area" data-color-area role="slider" tabindex="0" aria-label="Saturation and brightness"
aria-valuemin="0" aria-valuemax="100" aria-valuenow="0"
aria-describedby="color-area-help"><span class="color-cursor" data-color-cursor></span></div>
<span id="color-area-help" class="sr-only">Left and right change saturation. Up and down change brightness.</span>
<label for="account-color-hue">Hue</label>
<input type="range" id="account-color-hue" min="0" max="360" step="1" value="0">
<div class="color-preview" data-color-preview role="img" aria-label="Picked color"></div>
<label for="account-color-value">Hex color</label>
<input type="text" id="account-color-value" value="#000000" pattern="#[0-9a-fA-F]{6}"
maxlength="7" spellcheck="false" autocomplete="off" required>
<div class="color-actions"><button type="button" data-color-try>Try</button>
<button type="button" data-color-save>Save</button>
<button type="button" data-color-cancel>Cancel</button></div></dialog>'''

MENU = ('<div class="move-menu" role="menu" tabindex="-1" aria-labelledby="move-title" hidden data-move-menu>'
        + '<div id="move-title" class="move-title" data-move-title></div><div data-move-items></div></div>')

QUOTA_PANEL = ('<div class="quota-panel" id="quota-panel" role="dialog" aria-labelledby="quota-title" hidden>'
               + '<strong id="quota-title"></strong><p data-quota-description></p>'
               + '<button type="button" data-quota-clear>Clear</button></div>')

SCRIPT = page_frame.REFRESH_SCRIPT + page_frame.ACTION_STATUS_SCRIPT + r"""(() => {
let view = document.querySelector('[data-accounts]');
let newestRequest = 0;
const modelTooltip=document.querySelector('.model-tooltip');
const modelCell=node=>node instanceof Element ? node.closest('[data-model-sessions]') : null;
let hoveredModelCell=null, tooltipModelCell=null;
function positionModelTooltip(){
 const cell=tooltipModelCell;
 modelTooltip.hidden=true;
 if(!cell?.isConnected) return;
 const box=cell.getBoundingClientRect(), table=cell.closest('table').getBoundingClientRect();
 const scroll=cell.closest('.scroll').getBoundingClientRect();
 const left=Math.max(table.left,scroll.left,0), right=Math.min(table.right,scroll.right,innerWidth);
 const top=Math.max(table.top,scroll.top,0), bottom=Math.min(table.bottom,scroll.bottom,innerHeight);
 if(box.bottom<=top || box.top>=bottom || box.right<=left || box.left>=right) return;
 modelTooltip.textContent=cell.dataset.modelSessions;
 modelTooltip.style.maxWidth=(right-left)+'px';modelTooltip.style.maxHeight=(bottom-top)+'px';
 modelTooltip.hidden=false;
 const size=modelTooltip.getBoundingClientRect();
 const beside=box.right+size.width<=right ? box.right : box.left-size.width;
 modelTooltip.style.left=Math.max(left,Math.min(beside,right-size.width))+'px';
 modelTooltip.style.top=Math.max(top,Math.min(box.top,bottom-size.height))+'px';
}
function showModelTooltip(cell){tooltipModelCell=cell;positionModelTooltip();}
document.addEventListener('pointerover',event=>{
 const cell=modelCell(event.target);
 if(cell && cell!==hoveredModelCell){hoveredModelCell=cell;showModelTooltip(cell);}
});
document.addEventListener('pointerout',event=>{
 if(!hoveredModelCell || (modelCell(event.target)!==hoveredModelCell && !modelTooltip.contains(event.target))) return;
 if(modelCell(event.relatedTarget)===hoveredModelCell || modelTooltip.contains(event.relatedTarget)) return;
 const departed=hoveredModelCell;
 hoveredModelCell=null;
 if(tooltipModelCell===departed) showModelTooltip(modelCell(document.activeElement));
});
document.addEventListener('focusin',event=>{
 const cell=modelCell(event.target);
 if(cell) showModelTooltip(cell);
});
document.addEventListener('focusout',event=>{
 if(tooltipModelCell && modelCell(event.target)===tooltipModelCell) showModelTooltip(hoveredModelCell);
});
addEventListener('scroll',event=>{if(event.target!==modelTooltip) positionModelTooltip();},{capture:true,passive:true});
addEventListener('resize',positionModelTooltip);
document.addEventListener('keydown',event=>{
 if(event.key==='Escape' && !modelTooltip.hidden){hoveredModelCell=null;showModelTooltip(null);}
});
const popup=document.querySelector('[data-color-picker]'), picker=popup.querySelector('#account-color-value');
const area=popup.querySelector('[data-color-area]'), hue=popup.querySelector('#account-color-hue');
const cursor=popup.querySelector('[data-color-cursor]'), preview=popup.querySelector('[data-color-preview]');
let saturation=0,brightness=0,dragging=null;
const clamp=value=>Math.max(0,Math.min(1,value));
function drawPick(value){
 picker.value=value;picker.setCustomValidity('');
 for(const action of popup.querySelectorAll('[data-color-try],[data-color-save]')) action.disabled=false;
 area.style.setProperty('--picked-hue','hsl('+hue.value+' 100% 50%)');
 cursor.style.left=saturation*100+'%';cursor.style.top=(1-brightness)*100+'%';
 area.setAttribute('aria-valuenow',Math.round(saturation*100));
 area.setAttribute('aria-valuetext','Saturation '+Math.round(saturation*100)+'%, brightness '+Math.round(brightness*100)+'%');
 preview.style.background=value;preview.setAttribute('aria-label','Picked color '+value);
}
function readHex(){
 if(!/^#[0-9a-f]{6}$/i.test(picker.value)){
  picker.setCustomValidity('Use a color in #rrggbb format.');
  for(const action of popup.querySelectorAll('[data-color-try],[data-color-save]')) action.disabled=true;
  return false;
 }
 const value=picker.value.toLowerCase();
 const rgb=[1,3,5].map(start=>parseInt(value.slice(start,start+2),16)/255);
 const [red,green,blue]=rgb, high=Math.max(...rgb),low=Math.min(...rgb),delta=high-low;
 if(delta){
  const sector=high===red ? (green-blue)/delta : high===green ? (blue-red)/delta+2 : (red-green)/delta+4;
  hue.value=(sector*60+360)%360;
 }
 saturation=high ? delta/high : 0;brightness=high;drawPick(value);return true;
}
function pickHSV(){
 const sector=Number(hue.value)/60,chroma=brightness*saturation;
 const second=chroma*(1-Math.abs(sector%2-1)),base=brightness-chroma;
 const channels=[[chroma,second,0],[second,chroma,0],[0,chroma,second],
  [0,second,chroma],[second,0,chroma],[chroma,0,second]][Math.floor(sector)%6];
 drawPick('#'+channels.map(value=>Math.round((value+base)*255).toString(16).padStart(2,'0')).join(''));
}
function pickPoint(event){
 const box=area.getBoundingClientRect();
 saturation=clamp((event.clientX-box.left)/box.width);brightness=clamp(1-(event.clientY-box.top)/box.height);pickHSV();
}
area.addEventListener('pointerdown',event=>{
 if(event.button!==0) return;
 event.preventDefault();area.focus();dragging=event.pointerId;
 if(event.isTrusted) area.setPointerCapture(event.pointerId);
 pickPoint(event);
});
area.addEventListener('pointermove',event=>{if(dragging===event.pointerId) pickPoint(event);});
for(const name of ['pointerup','pointercancel','lostpointercapture']) area.addEventListener(name,()=>{dragging=null;});
area.addEventListener('keydown',event=>{
 const step=event.shiftKey ? .1 : .01;
 if(event.key==='ArrowLeft') saturation=clamp(saturation-step);
 else if(event.key==='ArrowRight') saturation=clamp(saturation+step);
 else if(event.key==='ArrowUp') brightness=clamp(brightness+step);
 else if(event.key==='ArrowDown') brightness=clamp(brightness-step);
 else return;
 event.preventDefault();pickHSV();
});
hue.addEventListener('input',pickHSV);
hue.addEventListener('keydown',event=>{
 const step=event.shiftKey ? 10 : 1;
 if(['ArrowRight','ArrowUp'].includes(event.key)) hue.value=Number(hue.value)+step;
 else if(['ArrowLeft','ArrowDown'].includes(event.key)) hue.value=Number(hue.value)-step;
 else if(event.key==='Home') hue.value=0;
 else if(event.key==='End') hue.value=360;
 else return;
 event.preventDefault();pickHSV();
});
picker.addEventListener('input',readHex);
const pendingSaves=new Set(), queues=new Map();
const message=document.createElement('span');
message.className='color-save-error';message.dataset.colorSaveError='';message.hidden=true;
message.setAttribute('role','status');message.setAttribute('aria-live','polite');
document.querySelector('[data-page-status]').append(message);
let selected=null;
const menu=document.querySelector('[data-move-menu]'), menuItems=menu.querySelector('[data-move-items]');
let menuRow=null, menuButton=null;
const pendingMoves=new Set();
const quotaPanel=document.querySelector('#quota-panel'), quotaClear=quotaPanel.querySelector('[data-quota-clear]');
let quotaButton=null, quotaSaving=false;
const hold=()=>selected ? 'A color is being edited.' : menuRow ? 'An account move is being chosen.'
 : pendingSaves.size ? 'Colors are being saved.' : pendingMoves.size ? 'An account move is being saved.'
 : quotaSaving ? 'A quota alert is being cleared.' : quotaButton ? 'A quota alert is open.' : '';
const sortSelector='[data-account-sort],[data-model-sort]';
const sortKey=header=>header.dataset.accountSort ?? header.dataset.modelSort;
const sortTables=new WeakMap();
const sortChoices=new Map();
function applySort(card){
 const name=card.dataset.sortTable, models=name==='models';
 const headers=[...card.querySelectorAll(sortSelector)];
 if(!sortChoices.has(name)){
  let choice={column:models ? 'model' : 'account',reverse:false};
  try{
   const saved=JSON.parse(localStorage.getItem(name+'-sort'));
   if(saved && headers.some(header=>sortKey(header)===saved.column) && typeof saved.reverse==='boolean') choice=saved;
  }catch{}
  sortChoices.set(name,choice);
 }
 const chosenSort=sortChoices.get(name), nameCell=models ? 0 : 1;
 const table=card.querySelector('table'), body=table.tBodies[0];
 if(!sortTables.has(table)){
  let group='';
  sortTables.set(table,[...body.rows].slice(0,-1).map(node=>{
   group=node.cells[nameCell].textContent || group;
   return {node,group};
  }));
 }
 const sources=sortTables.get(table), details=new Map();
 const width=table.tHead.rows[0].cells.length;
 const columns=models ? [0,1] : [0,1,2,3,width-3,width-2,width-1];
 for(const {node,group} of sources) if(node.cells[nameCell].textContent){
  details.set(group,columns.map(index=>node.cells[index].innerHTML));
 }
 const order=JSON.parse(card.dataset.sortOrders)[chosenSort.column+(chosenSort.reverse ? '-reverse' : '')];
 const total=body.lastElementChild;
 for(const place of order){
  const {node,group}=sources[place.index];
  columns.forEach((index,offset)=>{node.cells[index].innerHTML=place.details ? details.get(group)[offset] : '';});
  node.classList.toggle('account',place.details);body.insertBefore(node,total);
 }
 for(const header of headers){
  const selected=sortKey(header)===chosenSort.column;
  const descending=header.hasAttribute('data-sort-descending')!==(selected && chosenSort.reverse);
  header.setAttribute('aria-sort',selected ? descending ? 'descending' : 'ascending' : 'none');
  header.querySelector('.sort-arrow').textContent=descending ? '▼' : '▲';
 }
}
function applySorts(){view.querySelectorAll('[data-sort-table]').forEach(applySort);}
function chooseSort(header){
 if(hold()) return;
 const card=header.closest('[data-sort-table]'), name=card.dataset.sortTable;
 const column=sortKey(header), prior=sortChoices.get(name);
 const chosenSort={column,reverse:prior.column===column ? !prior.reverse : false};
 sortChoices.set(name,chosenSort);
 try{localStorage.setItem(name+'-sort',JSON.stringify(chosenSort));}catch{}
 applySort(card);
}
document.addEventListener('click',event=>{
 const header=event.target instanceof Element ? event.target.closest(sortSelector) : null;
 if(header) chooseSort(header);
});
document.addEventListener('keydown',event=>{
 const header=event.target instanceof Element ? event.target.closest(sortSelector) : null;
 if(header && event.key==='Enter'){event.preventDefault();chooseSort(header);}
});
applySorts();
async function refresh(supersede=false,current=()=>true) {
  if (hold()) return;
  const request = ++newestRequest;
  const response = await fetch(location.pathname, {cache: 'no-store'});
  if (!response.ok) throw new Error('HTTP ' + response.status);
  const page = new DOMParser().parseFromString(await response.text(), 'text/html');
  const fresh = page.querySelector('[data-accounts]');
  if (request !== newestRequest || !current() || hold()) return;
  if (!fresh) throw new Error('The page has no accounts.');
  const top = scrollY;
  const restoreFocus = modelCell(document.activeElement) ? restoreFocusAfterRefresh() : null;
  view.replaceWith(fresh);view = fresh;
  applySorts();
  scrollTo({top, behavior: 'instant'});
  hoveredModelCell=null;
  restoreFocus?.();
  showModelTooltip(modelCell(document.activeElement));
  return pageRefreshFacts(page);
}
const updates=createPageRefresh(refresh, () => document.querySelector('main').dataset.refreshSeconds, hold);
function position(){
 if(!selected) return;
 const box=selected.getBoundingClientRect(), size=popup.getBoundingClientRect();
 popup.style.left=Math.max(8,Math.min(box.left,innerWidth-size.width-8))+'px';
 popup.style.top=Math.max(8,Math.min(box.bottom+8,innerHeight-size.height-8))+'px';
}
// Every swatch and word of one key, such as units on each units row, shows one color.
function swatches(key){return [...document.querySelectorAll('[data-color-key]')].filter(button=>button.dataset.colorKey===key);}
function words(key){return [...document.querySelectorAll('[data-color-text]')].filter(word=>word.dataset.colorText===key);}
function queue(key){
 if(!queues.has(key)) queues.set(key,[]);
 return queues.get(key);
}
function savedColor(key){return swatches(key)[0]?.dataset.savedColor || '';}
function pickerColor(button,value){return value || getComputedStyle(button).getPropertyValue('--ink').trim();}
function paint(key,value){
 for(const button of swatches(key)) button.style.background=value;
 for(const word of words(key)) word.style.color=value;
}
function restore(key){
 const pending=queue(key).at(-1);
 paint(key,pending ? pending.value : savedColor(key));
}
function close(restoreSaved){
 const button=selected;selected=null;
 if(restoreSaved) restore(button.dataset.colorKey);
 popup.close();button.focus({preventScroll:true});updates.changed();
}
document.addEventListener('click',event=>{
 const button=event.target instanceof Element ? event.target.closest('[data-color-key]') : null;
 if(!button) return;
 if(selected) close(true);
 selected=button;
 picker.value=pickerColor(button,queue(button.dataset.colorKey).at(-1)?.value || button.dataset.savedColor);
 readHex();
 popup.querySelector('#color-title').textContent='Change '+button.dataset.colorName+' color';
 popup.showModal();position();area.focus();updates.abandon();
});
popup.querySelector('[data-color-try]').addEventListener('click',()=>{if(readHex()) paint(selected.dataset.colorKey,picker.value);});
popup.querySelector('[data-color-cancel]').addEventListener('click',()=>close(true));
popup.addEventListener('cancel',event=>{event.preventDefault();close(true);});
popup.addEventListener('keydown',event=>{
 if(event.key==='Escape'){event.preventDefault();close(true);}
});
async function publish(save){
 const pending=queue(save.key);
 try{
  const response=await fetch(location.pathname,{method:'POST',
   headers:{'Content-Type':'application/x-www-form-urlencoded'},
   body:new URLSearchParams({key:save.key,value:save.value}).toString()});
  const result=await response.json();
  if(!response.ok) throw new Error(result.reason || 'The color could not be saved.');
  for(const button of swatches(save.key)) button.dataset.savedColor=save.value;
 }catch(error){
  for(const button of swatches(save.key)) button.dataset.savedColor=save.oldColor;
  paint(save.key,save.oldColor);
  if(selected?.dataset.colorKey===save.key){picker.value=pickerColor(selected,save.oldColor);readHex();}
  showActionFailure(message,save.name+': '+(error.message || 'The color could not be saved.'));
 }finally{
  pending.shift();pendingSaves.delete(save);
  const next=pending[0];
  if(next){next.oldColor=savedColor(save.key);void publish(next);}
  if(selected?.dataset.colorKey!==save.key) restore(save.key);
  updates.changed();
  if(!pendingSaves.size) void updates.refresh(true);
 }
}
popup.querySelector('[data-color-save]').addEventListener('click',()=>{
 if(!readHex()){picker.reportValidity();return;}
 const button=selected,key=button.dataset.colorKey,pending=queue(key);
 const save={key,name:button.dataset.colorName,value:picker.value,oldColor:button.dataset.savedColor};
 paint(key,save.value);pending.push(save);pendingSaves.add(save);
 showActionFailure(message);close(false);
 if(pending.length===1) void publish(save);
});
addEventListener('resize',position);addEventListener('scroll',position,{passive:true});
function closeMenu(){
 if(!menuRow) return;
 const button=menuButton;menuRow=null;menuButton=null;
 menu.hidden=true;menuItems.replaceChildren();button.setAttribute("aria-expanded","false");
 button.focus({preventScroll:true});updates.changed();
}
function menuItem([label,left,color,tone]){
 const item=document.createElement('button');
 item.type='button';item.setAttribute('role','menuitemradio');item.dataset.moveTarget=label;
 item.setAttribute('aria-checked',String(label===menuButton.dataset.nextTarget));
 const name=document.createElement('span'), room=document.createElement('span');
 name.className='move-name';name.textContent=label;if(color) name.style.color=color;
 room.className='move-left'+(tone==='default' ? '' : ' '+tone);room.textContent=left && left!=='—' ? left+' left' : '';
 item.append(name,room);return item;
}
function openMenu(button){
 const row=button.closest("tr"), box=button.getBoundingClientRect();
 if(selected) close(true);
 const card=view.querySelector('[data-move-targets]');
 const account=row.dataset.moveAccount, group=row.dataset.moveGroup;
 const targets=JSON.parse(card.dataset.moveTargets).filter(([label])=>label!==account);
 menuRow=row;menuButton=button;button.setAttribute("aria-expanded","true");
 menu.querySelector('[data-move-title]').textContent='When '+account+' reaches '+card.dataset.movePercent+'%, move '+group+' to';
 const empty=document.createElement('div');
 empty.className='move-empty';empty.textContent='No other Claude account';
 menuItems.replaceChildren(...(targets.length ? targets.map(menuItem) : [empty]));
 if(button.dataset.nextTarget){
  const clear=document.createElement('button');clear.type='button';clear.dataset.moveClear='first';
  clear.setAttribute('role','menuitem');clear.textContent='No next move';menuItems.append(clear);
 }
 menu.hidden=false;
 const size=menu.getBoundingClientRect();
 menu.style.left=Math.max(8,Math.min(box.left,innerWidth-size.width-8))+'px';
 menu.style.top=Math.max(8,Math.min(box.bottom,innerHeight-size.height-8))+'px';
 (menuItems.querySelector('button') || menu).focus({preventScroll:true});
 updates.abandon();
}
async function saveMove(form){
 const group=form.group, save={form};
 pendingMoves.add(save);showActionFailure(message);
 try{
  const response=await fetch(location.pathname,{method:'POST',
   headers:{'Content-Type':'application/x-www-form-urlencoded'},
   body:new URLSearchParams(form).toString()});
  const result=await response.json();
  if(!response.ok) throw new Error(result.reason || 'The move could not be saved.');
 }catch(error){
  showActionFailure(message,group+': '+(error.message || 'The move could not be saved.'));
 }finally{
  pendingMoves.delete(save);updates.changed();
  if(!hold()) void updates.refresh(true);
 }
}
document.addEventListener('click',event=>{
 const button=event.target instanceof Element ? event.target.closest('[data-next-button]') : null;
 if(!button) return;
 const wasOpen=menuButton===button;closeMenu();if(!wasOpen) openMenu(button);
});
document.addEventListener('keydown',event=>{
 const button=event.target instanceof Element ? event.target.closest('[data-next-button]') : null;
 if(!button || !['Enter',' '].includes(event.key)) return;
 event.preventDefault();button.click();
});
document.addEventListener('pointerdown',event=>{
 if(menuRow && !(event.target instanceof Node && (menu.contains(event.target) || menuButton.contains(event.target)))) closeMenu();
});
menu.addEventListener('click',event=>{
 const item=event.target instanceof Element ? event.target.closest('[data-move-target],[data-move-clear]') : null;
 if(!item || !menuRow) return;
 const form=item.dataset.moveClear ? {group:menuRow.dataset.moveGroup,clear:'first'}
  : {group:menuRow.dataset.moveGroup,to:item.dataset.moveTarget};
 void saveMove(form);closeMenu();
});
menu.addEventListener('keydown',event=>{
 const items=[...menuItems.querySelectorAll('button')], at=items.indexOf(document.activeElement);
 if(event.key==='Escape' || event.key==='Tab'){event.preventDefault();closeMenu();return;}
 if(!items.length || !['ArrowDown','ArrowUp','Home','End'].includes(event.key)) return;
 event.preventDefault();
 const down=event.key==='ArrowDown';
 const next=event.key==='Home' ? 0 : event.key==='End' ? items.length-1
  : at<0 ? (down ? 0 : items.length-1) : (at+(down ? 1 : -1)+items.length)%items.length;
 items[next].focus();
});
function closeQuota(restoreFocus){
 if(!quotaButton) return;
 const button=quotaButton;
 quotaButton=null;quotaPanel.hidden=true;button.setAttribute('aria-expanded','false');
 if(restoreFocus) button.focus({preventScroll:true});
 updates.changed();
}
document.addEventListener('click',event=>{
 const button=event.target instanceof Element ? event.target.closest('[data-quota-account]') : null;
 if(!button || quotaSaving) return;
 const wasOpen=button===quotaButton;
 closeQuota(false);
 if(wasOpen) return;
 quotaButton=button;
 button.setAttribute('aria-expanded','true');
 const [level,left]=button.getAttribute('aria-label').split(': ');
 quotaPanel.querySelector('#quota-title').textContent=button.dataset.quotaAccount+' has '+left;
 quotaPanel.querySelector('[data-quota-description]').textContent=level+' weekly quota alert';
 quotaPanel.hidden=false;
 const box=button.getBoundingClientRect(), size=quotaPanel.getBoundingClientRect();
 quotaPanel.style.left=Math.max(8,Math.min(box.left-8,innerWidth-size.width-8))+'px';
 quotaPanel.style.top=Math.max(8,Math.min(box.bottom+8,innerHeight-size.height-8))+'px';
 quotaClear.focus({preventScroll:true});
 updates.abandon();
});
document.addEventListener('pointerdown',event=>{
 if(quotaButton && !quotaPanel.contains(event.target) && !(event.target instanceof Element
     && event.target.closest('[data-quota-account]'))) closeQuota(false);
});
document.addEventListener('keydown',event=>{
 if(event.key==='Escape' && quotaButton){event.preventDefault();closeQuota(true);}
});
quotaClear.addEventListener('click',async()=>{
 if(!quotaButton || quotaSaving) return;
 const button=quotaButton, {quotaAccount,quotaLevel,quotaReset}=button.dataset;
 quotaSaving=true;quotaClear.disabled=true;showActionFailure(message);
 try{
  const response=await fetch(location.pathname,{method:'POST',
   headers:{'Content-Type':'application/x-www-form-urlencoded'},
   body:new URLSearchParams({quota_account:quotaAccount,quota_level:quotaLevel,quota_reset:quotaReset})});
  const result=await response.json();
  if(!response.ok) throw new Error(result.reason || 'The quota alert could not be cleared.');
  const cell=button.closest('td'), hadFocus=quotaPanel.contains(document.activeElement);
  closeQuota(false);
  for(const icon of view.querySelectorAll('[data-quota-account]')){
   if(icon.dataset.quotaAccount===quotaAccount) icon.remove();
  }
  if(hadFocus){cell.tabIndex=-1;cell.focus({preventScroll:true});}
  window.parent.postMessage('accounts-quota-cleared',location.origin);
 }catch(error){
  showActionFailure(message,error.message || 'The quota alert could not be cleared.');
 }finally{
  quotaSaving=false;quotaClear.disabled=false;updates.changed();
 }
});
for(const name of ['resize','scroll']) addEventListener(name,()=>closeQuota(false),{passive:true});
for(const name of ['resize','scroll','blur']) addEventListener(name,closeMenu,{passive:true});
})();"""


if __name__ == "__main__":
    raise SystemExit(main())
