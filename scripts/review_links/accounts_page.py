"""Render the accounts board on the Hana console Accounts page."""
from __future__ import annotations

from collections import Counter
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone, tzinfo
from enum import StrEnum
import html
import json
import re
from pathlib import Path
from statistics import median
import subprocess
import sys
import time
from typing import Literal, TypedDict, cast

from scripts.accounts import accounts, moves, next_moves, role_activity
from scripts.agents import shutdown_record
from scripts.production import staff
from scripts.lib import atomic_file, conf, file_lock, waiting_rows
from scripts.review_links import change_page, console_theme, page_frame, page_version, review_link
from scripts.whoami import agent_accounts, mac_fallback_token, quota_alert, run_out, token_expiry
# The alias keeps the module apart from this page's quota_state() path function.
from scripts.whoami import quota_state as worker_quota_state

PAGE_NAME = "accounts.html"
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


def _spans(spans: list[accounts.BoardSpan], *, title: str = "") -> str:
    parts: list[str] = []
    tooltip = f' title="{html.escape(title, quote=True)}"' if title else ""
    for span in spans:
        color = accounts.account_color(span.get("color", ""))
        style = f' style="color:{color.css}"' if isinstance(color, accounts.AccountColor) else ""
        if "color_key" in span:
            style += f' data-color-text="{html.escape(span["color_key"], quote=True)}"'
        parts.append(f'<span{tooltip} class="{span["tone"]}{" bold" if span["kind"] == "bold" else ""}"{style}>{html.escape(span["text"])}</span>')
    return "".join(parts)


NextMove = accounts.BoardNextMoveTarget | accounts.NextMovesUnavailable | Literal["unselected", "none", "auto"]


def _next_cell(row: accounts.BoardRow, move: NextMove,
               labels: dict[str, accounts.BoardSpan]) -> str:
    """Render the selected group's target or the reason this line has no target."""
    if row["cells"]["label"] and row["cells"]["label"][0]["text"] == "total":
        return ""
    editable = row.get("group")
    if move == "unselected":
        return ('<span class="next-muted next-minus" title="Not the selected account for a role">'
                + page_frame.icon_svg("minus") + '</span>')
    target = move.label if isinstance(move, accounts.BoardNextMoveTarget) else ""
    content = _next_text(move, labels)
    if editable:
        content += (f'<button type="button" class="next-button" data-next-button data-next-target="{html.escape(target, quote=True)}" '
                    + f'data-move-account="{html.escape(editable["account"], quote=True)}" data-move-group="{editable["group"]}" '
                    + f'aria-label="Choose the next account for {editable["group"]}" aria-haspopup="menu" aria-expanded="false">'
                    + page_frame.icon_svg("chevron-down") + '</button>')
    return content


def _next_text(move: accounts.BoardNextMoveTarget | accounts.NextMovesUnavailable | Literal["none", "auto"],
               labels: dict[str, accounts.BoardSpan]) -> str:
    """Render a next move target or state as text, without the move control."""
    if isinstance(move, accounts.BoardNextMoveTarget):
        return _spans([labels.get(move.label, accounts.BoardSpan(text=move.label, tone="default", kind="bold"))])
    if isinstance(move, accounts.NextMovesUnavailable):
        return '<span class="next-muted" title="' + html.escape(move.reason, quote=True) + '">unavailable</span>'
    if move == "none":
        return '<span class="next-muted">none</span>'
    return '<span class="next-muted" title="Codex workers move between the Codex accounts by themselves">auto</span>'


def _next_sort_value(move: NextMove) -> str | None:
    if isinstance(move, accounts.BoardNextMoveTarget):
        return move.label.casefold()
    if isinstance(move, accounts.NextMovesUnavailable):
        return "unavailable"
    return None if move == "unselected" else move


def _swatch(spans: list[accounts.BoardSpan]) -> str:
    if not spans or "color_key" not in spans[0]:
        return ""
    name = html.escape(spans[0]["text"], quote=True)
    color = accounts.account_color(spans[0].get("color", ""))
    css = color.css.lower() if isinstance(color, accounts.AccountColor) else ""
    background = f' style="background:{css}"' if css else ""
    key = html.escape(spans[0]["color_key"], quote=True)
    return (f'<button type="button" class="color-swatch" data-color-key="{key}" '
            + f'data-color-name="{name}" data-saved-color="{css}"{background} '
            + f'aria-label="Change {name} color" aria-haspopup="dialog" data-window-place="color:{key}"></button>')


def _sort_heading(column: accounts.BoardColumn, default: str, descending_columns: tuple[str, ...],
                  *, sort_field: str = "", label_markup: str = "") -> str:
    key = column["key"]
    if not key:
        return f'<th class="{column["align"]}"></th>'
    direction = "ascending" if key == default else "none"
    descending = " data-sort-descending" if key in descending_columns else ""
    label = html.escape(column["heading"])
    arrow = '<span class="sort-arrow" aria-hidden="true">▲</span>'
    word = label_markup or label
    content = arrow + word if column["align"] == "right" else word + arrow
    return (f'<th class="{column["align"]}" data-{sort_field or default}-sort="{html.escape(key, quote=True)}"'
            + f' tabindex="0" aria-sort="{direction}"{descending}>'
            + f'<span class="sort-label">{content}</span></th>')


SortValue = str | float | None
SortValues = dict[str, SortValue]
# Keep the saved sort key for the weekly reset date.
ACCOUNT_SORT_KEYS = {"weekly reset": "resets", "resets": "reset count"}
ACCOUNT_COLUMNS = tuple(
    accounts.BoardColumn(key=ACCOUNT_SORT_KEYS.get(heading, heading),
                         heading=heading, align="right" if align == "right" else "left")
    for heading, align in (
        ("", "left"), ("account", "left"), ("", "left"), ("left", "right"), ("Fable left", "right"),
        ("used / hour", "right"), ("empty at", "left"), ("role", "left"), ("", "left"), ("next", "left"),
        ("weekly reset", "right"), ("resets", "right"), ("credits", "right"), ("enabled", "left"),
    )
)
ROLE_COUNT_COLUMNS = ("busy", "idle", "total")
# Paused and exited sessions are not running. They have no column: a warning icon after the role total names them.
NOT_RUNNING_STATES: tuple[Literal["paused", "exited"], ...] = ("paused", "exited")
BUSY_LINE_HEADING = "busy, last 24 h"
ROLE_COLUMNS = (
    *(accounts.BoardColumn(key=key, heading=key, align="left")
      for key in ("role", "", BUSY_LINE_HEADING, "account", "next")),
    *(accounts.BoardColumn(key=key, heading=key, align="right") for key in ROLE_COUNT_COLUMNS),
)
# The busy line of each role, in SVG pixels: the line area, then the hour labels under it.
BUSY_LINE_WIDTH = 288
BUSY_LINE_HEIGHT = 22
BUSY_LINE_TOTAL_HEIGHT = 35
BUSY_LINE_LABEL_PX = 9
BUSY_LINE_EDGE = 4
BUSY_LINE_TICK = 4
BUSY_LINE_LABEL_HOURS = 3
# The statusline.conf key of the codex pair row tint, and the strength of that tint over the card.
PAIR_TINT_KEY = "background.codex-pair"
PAIR_TINT_PERCENT = 12
ACCOUNT_DETAIL_COLUMNS = "1,0,2,3,4,5,6,10,11,12,13"
DASH = '<span class="next-muted">—</span>'


def _sort_orders(values: list[SortValues], descending: tuple[str, ...]) -> dict[str, list[int]]:
    orders: dict[str, list[int]] = {}
    for key in values[0] if values else ():
        present = [(value, index) for index, row in enumerate(values) if (value := row[key]) is not None]
        missing = [index for index, row in enumerate(values) if row[key] is None]
        for reverse in (False, True):
            ranked = sorted(present, key=lambda pair: pair[0], reverse=(key in descending) != reverse)
            orders[key + ("-reverse" if reverse else "")] = [index for _, index in ranked] + missing
    return orders


def _split_card(title: str, name: str, default: str, columns: Sequence[accounts.BoardColumn],
                rows: list[str], orders: dict[str, list[int]] | dict[str, list[accounts.AccountRowPlacement]],
                *, descending: tuple[str, ...] = (), unsorted: tuple[str, ...] = (),
                sort_field: str = "", attributes: str = "", after: str = "") -> str:
    head = "".join(f'<th class="{column["align"]}">{html.escape(column["heading"])}</th>' if column["key"] in unsorted
                   else _sort_heading(column, default, descending, sort_field=sort_field) for column in columns)
    orders_json = html.escape(json.dumps(orders), quote=True)
    return (f'<section class="card" data-sort-table="{name}" data-sort-default="{default}" '
            f'data-sort-orders="{orders_json}"{attributes}><h2>{title}</h2><div class="scroll">'
            f'<table class="{name}-table" aria-label="{title}"><thead><tr>{head}</tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div>{after}</section>')


def _cell(content: str, css: str = "left") -> str:
    return f'<td class="{css}">{content}</td>'


def _timestamp(value: datetime | accounts.StatusUnavailable | None) -> float | None:
    return value.timestamp() if isinstance(value, datetime) else None


def _forecast_dash(reason: str) -> str:
    return f'<span title="{reason}">{DASH}</span>'


def _shown_lines(lines: list[accounts.BoardRow]) -> list[accounts.BoardRow]:
    """The role lines of one account; an account with no role but spare keeps its first line."""
    return [line for line in lines if line["cells"]["roles"][0]["text"] != "spare"] or lines[:1]


def _shown_moves(reading: accounts.StatusReading, rows_by_account: dict[str, list[accounts.BoardRow]],
                 facts: Mapping[str, accounts.StatusRow]) -> dict[str, list[NextMove]]:
    """The next move of each shown line; while workers move by themselves, each workers or spare Codex line reads auto."""
    moves: dict[str, list[NextMove]] = {
        label: [accounts.account_board_next_move(line, reading.first_next_moves) for line in _shown_lines(lines)]
        for label, lines in rows_by_account.items()}
    # The selected Codex workers line reads auto when workers have no Claude next move; the spare Codex lines join it.
    if any(facts[label].tool == "codex" and "auto" in row_moves for label, row_moves in moves.items()):
        for label, row_moves in moves.items():
            for index, line in enumerate(_shown_lines(rows_by_account[label])):
                if (row_moves[index] == "unselected" and facts[label].tool == "codex"
                        and line["cells"]["roles"][0]["text"] in (accounts.CODEX_ROLE, "spare")):
                    row_moves[index] = "auto"
    return moves


def _joined(names: Sequence[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def _pair_text(pair: Sequence[accounts.StatusRow]) -> str:
    """The user's words for the Codex accounts that take the workers in turn."""
    # Only a known 0% on every account says there is no availability; an unknown value makes no claim.
    empty = all(isinstance(row.weekly_left_percent, (int, float)) and row.weekly_left_percent <= 0 for row in pair)
    working = next((row.label for row in pair if accounts.CODEX_ROLE in row.selected_roles), None)
    now = ("No account has availability. " if empty else f"{working} is working right now. " if working else "")
    mode = "self-actualizing" if worker_quota_state.no_worker_mode() else "working workers"
    return f"{_joined([row.label for row in pair])} alternate when available. {now}We are in {mode} mode."


def _tip_target(target_id: str, css: str, attributes: str, content: str, text: str) -> str:
    """A focusable target whose tooltip text is also its accessible description, in a hidden element next to it."""
    target = html.escape(target_id, quote=True)
    return (f'<span class="{css}" id="{target}"{attributes} tabindex="0" aria-describedby="{target}-tip" data-tip>'
            f'{content}</span><span id="{target}-tip" hidden>{html.escape(text)}</span>')


def _pair_tint(colors: Mapping[str, str]) -> str:
    color = accounts.account_color(colors.get(PAIR_TINT_KEY, ""))
    if not isinstance(color, accounts.AccountColor):
        return ""
    return f' style="background:color-mix(in srgb,{color.css} {PAIR_TINT_PERCENT}%,transparent)"'


def _weekly_reset_ready(row: accounts.StatusRow) -> bool:
    if row.has_running_claude_session or any(isinstance(count, accounts.RoleSessionCounts) and (count.busy or count.idle)
                                            for count in row.role_counts):
        return True
    if row.tool == "codex":
        return isinstance(row.codex_work_readiness, agent_accounts.CodexWorkAvailable)
    return isinstance(row.weekly_left_percent, (int, float)) and row.weekly_left_percent > 0


def _weekly_reset_attributes(reading: accounts.StatusReading) -> dict[str, str]:
    attributes: dict[str, str] = {}
    no_room: dict[str, datetime] = {}
    for row in reading.rows:
        if _weekly_reset_ready(row):
            attributes[row.label] = ' class="weekly-reset-ready"'
        elif isinstance(row.resets_at, datetime) and row.resets_at > reading.checked_at:
            exhausted = (isinstance(row.codex_work_readiness, agent_accounts.CodexWorkExhausted) if row.tool == "codex"
                         else isinstance(row.weekly_left_percent, (int, float)) and row.weekly_left_percent <= 0)
            if exhausted:
                no_room[row.label] = row.resets_at
    if no_room:
        nearest, furthest = min(no_room.values()), max(no_room.values())
        span = furthest - nearest
        for label, reset in no_room.items():
            fraction = (reset - nearest) / span if span else 0
            orange_weight = 33 + 67 * fraction
            attributes[label] = (f' class="weekly-reset-exhausted"'
                                 f' style="--reset-orange-weight:{orange_weight:.8g}%"')
    return attributes


def _account_login_title(login: str) -> str:
    fallback = "No email in the login record"
    try:
        _ = login.encode("utf-8")
    except UnicodeEncodeError:
        return fallback
    return login or fallback


def _accounts_table(reading: accounts.StatusReading, rows_by_account: dict[str, list[accounts.BoardRow]],
                    labels: dict[str, accounts.BoardSpan], alerts: Mapping[str, QuotaAlert],
                    colors: Mapping[str, str]) -> str:
    histories = run_out.read_readings(run_out.READINGS_LOG)
    rows: list[str] = []
    values: list[SortValues] = []
    labels_in_row_order: list[str] = []
    facts = {row.label: row for row in reading.rows}
    reset_attributes = _weekly_reset_attributes(reading)
    moves = _shown_moves(reading, rows_by_account, facts)
    pair = [facts[label] for label, row_moves in moves.items() if facts[label].tool == "codex" and "auto" in row_moves]
    # One Codex account has nothing to take turns with, so it keeps the plain auto text.
    pair_text = _pair_text(pair) if len(pair) > 1 else ""
    pair_row = f' class="auto-pair"{_pair_tint(colors)}' if pair_text else ""
    targets: list[tuple[str, str, str, accounts.BoardTone]] = []
    for label, lines in rows_by_account.items():
        row, cells = facts[label], lines[0]["cells"]
        dot = "Current reading" if cells["dot"][0]["tone"] == "success" else "Reading unavailable or stale"
        left = _spans(cells["left"])
        if alert := alerts.get(label):
            left = _quota_button(label, alert) + left
        fable = row.fable_left_percent if row.tool == "claude" and isinstance(row.fable_left_percent, (int, float)) else None
        fable_text = "" if row.tool == "codex" else DASH
        if fable is not None:
            fable_text = f'<span class="{"error" if fable == 0 else "default"}">{round(fable)}%</span>'
        reset_count = row.limit_reset_count if row.tool == "codex" and isinstance(row.limit_reset_count, int) else None
        reset_count_text = (
            '<span class="next-muted" title="Claude does not give this count to our tools.">—</span>'
            if row.tool == "claude" else DASH if reset_count is None else str(reset_count))
        reset = row.resets_at if isinstance(row.resets_at, datetime) else None
        forecast = (quota_alert.weekly_forecast(histories.get(label, []), reading.checked_at, reset,
                    current=run_out.Reading(row.read_at, 100 - row.weekly_left_percent))
                    if isinstance(row.read_at, datetime) and isinstance(row.weekly_left_percent, (int, float)) else None)
        rate = forecast.used_per_hour if forecast else None
        empty = forecast.empty_at if forecast else None
        no_rate = _forecast_dash("No usage rate is available.")
        if empty is not None:
            empty_text = f'<time datetime="{empty.isoformat()}">{empty.astimezone(reading.zone):{quota_alert.WEEKLY_EMPTY_FORMAT}}</time>'
        elif forecast is None:
            empty_text = no_rate
        elif forecast.lasts_to_deadline:
            empty_text = _forecast_dash("Quota lasts until the weekly reset.")
        elif reset is None:
            empty_text = _forecast_dash("The weekly reset time is unavailable.")
        else:
            empty_text = _forecast_dash("The estimated run-out time has passed.")
        account_values: SortValues = {
            "account": label.casefold(),
            "left": row.weekly_left_percent if isinstance(row.weekly_left_percent, (int, float)) else None,
            "Fable left": fable, "used / hour": rate, "empty at": _timestamp(empty),
            "resets": _timestamp(reset), "reset count": reset_count,
            "credits": float("inf") if row.credits == "unlimited" else
            row.credits if isinstance(row.credits, (int, float)) else None,
            "enabled": 1 if row.use_credits else None,
        }
        if row.tool == "claude":
            color = accounts.account_color(labels[label].get("color", ""))
            targets.append((label, "".join(span["text"] for span in cells["left"]),
                            color.css if isinstance(color, accounts.AccountColor) else "", cells["left"][0]["tone"]))
        name = _spans(cells["label"], title=_account_login_title(row.login))
        account_start = (
            _cell(f'<span title="{dot}" aria-label="{dot}">{_spans(cells["dot"])}</span>')
            + f'<td class="left" data-account-name>{name}</td>'
            + _cell(_swatch(cells["label"])) + _cell(left, "right") + _cell(fable_text, "right")
            + _cell(f"{rate:.1f}%" if rate is not None else no_rate, "right") + _cell(empty_text)
        )
        reset_span_attributes = reset_attributes.get(label, ' class="weekly-reset-unknown"')
        reset_text = f'<span{reset_span_attributes}>{_spans(cells["resets"])}</span>'
        account_end = (_cell(reset_text, "right")
                       + _cell(reset_count_text, "right") + _cell(_spans(cells["credits"]), "right")
                       + _cell(_spans(cells["enabled"])))
        for index, line in enumerate(_shown_lines(lines)):
            spans = line["cells"]["roles"]
            visible_role = [span for span in spans if span["text"] != " ✓"] if spans[0]["text"] != "spare" else []
            role = visible_role[0]["text"] if visible_role else ""
            move = moves[label][index]
            paired = pair_text and move == "auto"
            rows.append(f'<tr{pair_row if paired else ""} data-account-row="{html.escape(label, quote=True)}">'
                        + (account_start if index == 0 else _cell("") + '<td class="left" data-account-name></td>'
                           + _cell("") + _cell("", "right") * 3 + _cell(""))
                        + _cell(_spans(visible_role)) + _cell(_swatch(visible_role))
                        + _cell(_tip_target(f"pair-auto-{label}", "next-muted", "", "auto", pair_text) if paired else _next_cell(line, move, labels), "left next-cell")
                        + (account_end if index == 0 else _cell("", "right") * 3 + _cell("")) + "</tr>")
            values.append({**account_values, "role": role.casefold() or None, "next": _next_sort_value(move)})
            labels_in_row_order.append(label)
    problems = "".join(f"<li>{html.escape(note.removeprefix('* '))}</li>" for note in reading.notes)
    targets_json = html.escape(json.dumps(targets), quote=True)
    percent = next_moves.format_next_move_percent(next_moves.console_next_move_percent())
    descending = ("credits", "enabled")
    orders = {
        key: [accounts.AccountRowPlacement(index=index, details=offset == 0
               or labels_in_row_order[index] != labels_in_row_order[order[offset - 1]])
              for offset, index in enumerate(order)]
        for key, order in _sort_orders(values, descending).items()
    }
    return _split_card("Accounts", "accounts", "account", ACCOUNT_COLUMNS, rows, orders,
                       descending=descending,
                       attributes=(f' data-detail-columns="{ACCOUNT_DETAIL_COLUMNS}"'
                                   f' data-move-percent="{percent}" data-move-targets="{targets_json}"'),
                       after=f'<h2>Problems</h2><ul class="problems">{problems}</ul>' if problems else "")


def _count_tooltip(cell_id: str, value: str, lines: Sequence[str],
                   attribute: Literal["data-model-sessions", "data-role-counts"]) -> str:
    tooltip = html.escape("\n".join(lines), quote=True)
    label = html.escape(value + ": " + ", ".join(lines), quote=True)
    return (f' id="{html.escape(cell_id, quote=True)}" tabindex="0" aria-label="{label}"'
            f' {attribute}="{tooltip}"')


def _role_count_cell(role: str, assignments: list[tuple[str, accounts.BoardRow]], key: str,
                     *, show_breakdown: bool, after: str = "") -> tuple[str, int | None]:
    if not assignments:
        return _cell(after, "right"), None
    counts = [(label, line["cells"][key][0]["text"]) for label, line in assignments]
    total = sum(int(value) for _, value in counts) if all(value.isdecimal() for _, value in counts) else None
    value = str(total) if total is not None else "unknown" if key == "total" else ""
    attributes = ""
    if show_breakdown:
        lines = [f"{label}: {count or 'unknown'}" for label, count in counts]
        attributes = _count_tooltip(f"role-count-{role}-{key}", value or key, lines, "data-role-counts")
    return f'<td class="right"{attributes}>{value}{after}</td>', total


NotRunningNames = dict[str, list[str]]


def _not_running(role: str, assignments: list[tuple[str, accounts.BoardRow]],
                 facts: Mapping[str, accounts.StatusRow]) -> tuple[Counter[str], NotRunningNames]:
    """The count of each not-running state of one role, and the local names in that state."""
    counts: Counter[str] = Counter()
    names: NotRunningNames = {state: [] for state in NOT_RUNNING_STATES}
    for label, line in assignments:
        named = facts[label].named_sessions_by_role.get(role, ()) if label in facts else ()
        for state in NOT_RUNNING_STATES:
            if (count := line["cells"][state][0]["text"]).isdecimal():
                counts[state] += int(count)
                names[state].extend(item.name for item in named if item.state == state)
    return counts, names


def _not_running_text(counts: Counter[str], names: NotRunningNames) -> str:
    """For example "2 not running: 1 paused (tile), 1 exited (names unknown)"; no session not running gives ""."""
    parts: list[str] = []
    for state in NOT_RUNNING_STATES:
        if not (count := counts[state]):
            continue
        known = sorted(names[state], key=lambda name: (name.casefold(), name))[:count]
        unknown = count - len(known)
        # A peer machine sends counts without names; never guess them.
        missing = [] if not unknown else ["names unknown"] if not known else [
            f"{unknown} {'name' if unknown == 1 else 'names'} unknown"]
        parts.append(f"{count} {state} ({', '.join([*known, *missing])})")
    return f"{counts.total()} not running: " + ", ".join(parts) if parts else ""


def _not_running_slot(cell_id: str, text: str, icon: str) -> str:
    """A fixed slot after the total keeps the totals aligned; it holds the icon when sessions are not running."""
    content = _tip_target(cell_id, "not-running", ' role="img" aria-label="not running"', icon, text) if text else ""
    return f'<span class="not-running-slot">{content}</span>'


def _busy_line(role: str, color: str, samples: Sequence[role_activity.RoleBusySample],
               now: datetime, zone: tzinfo) -> str:
    """The role's busy count over the last 24 hours, one point per pixel column at most.

    A sample without the role, or a time gap longer than role_activity.SAMPLE_GAP, breaks the line.
    """
    start = now - role_activity.WINDOW
    window = role_activity.WINDOW.total_seconds()
    column_time = role_activity.WINDOW / BUSY_LINE_WIDTH
    top = max([1, *(sample.busy[role] for sample in samples if role in sample.busy)])
    # Each segment holds (column, count) points; a column ends at its x, and it keeps its largest count.
    segments: list[list[tuple[int, int]]] = []
    gap = True
    previous: datetime | None = None
    for sample in samples:
        count = sample.busy.get(role)
        if previous is not None and sample.at - previous > role_activity.SAMPLE_GAP:
            gap = True
        previous = sample.at
        if count is None:
            gap = True
            continue
        column = min(BUSY_LINE_WIDTH, max(1, -(-(sample.at - start) // column_time)))
        if gap:
            segments.append([])
            gap = False
        points = segments[-1]
        if points and points[-1][0] == column:
            points[-1] = (column, max(points[-1][1], count))
        else:
            points.append((column, count))
    lines = ""
    for points in segments:
        text = [f"{column:.1f},{BUSY_LINE_HEIGHT - 1 - count / top * (BUSY_LINE_HEIGHT - 2):.1f}" for column, count in points]
        # A lone point repeats, so the round cap draws it as a dot.
        lines += f'<polyline points="{" ".join(text * 2 if len(text) == 1 else text)}"/>'
    marks: list[str] = []
    hour = start.astimezone(zone).replace(minute=0, second=0, microsecond=0).astimezone(timezone.utc) + timedelta(hours=1)
    while hour <= now:
        x = (hour - start).total_seconds() / window * BUSY_LINE_WIDTH
        local = hour.astimezone(zone).hour
        marks.append(f'<text x="{x:.1f}" y="{BUSY_LINE_TOTAL_HEIGHT - 3}">{local}</text>'
                     if local % BUSY_LINE_LABEL_HOURS == 0 else
                     f'<line class="busy-tick" x1="{x:.1f}" x2="{x:.1f}" y1="{BUSY_LINE_HEIGHT + 2}" '
                     + f'y2="{BUSY_LINE_HEIGHT + 2 + BUSY_LINE_TICK}"/>')
        hour += timedelta(hours=1)
    return (f'<svg class="busy-line" width="{BUSY_LINE_WIDTH}" height="{BUSY_LINE_TOTAL_HEIGHT}" '
            f'viewBox="{-BUSY_LINE_EDGE} 0 {BUSY_LINE_WIDTH + 2 * BUSY_LINE_EDGE} {BUSY_LINE_TOTAL_HEIGHT}" '
            f'role="img" aria-label="{html.escape(role, quote=True)} busy sessions, last 24 hours">'
            f'<line class="busy-base" x1="0" x2="{BUSY_LINE_WIDTH}" y1="{BUSY_LINE_HEIGHT - .5}" y2="{BUSY_LINE_HEIGHT - .5}"/>'
            f'<g fill="none" stroke="{color or "currentColor"}" stroke-width="1.5" stroke-linejoin="round" '
            f'stroke-linecap="round">{lines}</g>{"".join(marks)}</svg>')


def _roles_table(reading: accounts.StatusReading, groups: dict[str, list[tuple[str, accounts.BoardRow]]],
                 labels: dict[str, accounts.BoardSpan], selected: dict[str, tuple[str, ...]],
                 total: accounts.BoardRow, colors: dict[str, str],
                 samples: Sequence[role_activity.RoleBusySample]) -> str:
    rows: list[str] = []
    values: list[SortValues] = []
    facts = {row.label: row for row in reading.rows}
    role_names = sorted(set(groups) | set(accounts.CLAUDE_GROUPS) | {accounts.CODEX_ROLE})
    not_running = {role: _not_running(role, groups.get(role, []), facts) for role in role_names}
    all_counts = sum((counts for counts, _ in not_running.values()), Counter[str]())
    all_names = {state: [name for _, names in not_running.values() for name in names[state]]
                 for state in NOT_RUNNING_STATES}
    all_text = _not_running_text(all_counts, all_names)
    icon = page_frame.icon_svg("triangle-alert") if all_text else ""
    for role in role_names:
        assignments = sorted(groups.get(role, []), key=lambda item: role not in selected[item[0]])
        role_span = (assignments[0][1]["cells"]["roles"][:1] if assignments
                     else [accounts.board_role_span(role, colors)])
        selected_assignment = next(((label, line) for label, line in assignments if role in selected[label]), None)
        move = (accounts.account_board_next_move(selected_assignment[1], reading.first_next_moves)
                if selected_assignment else "unselected")
        next_text = DASH if move == "unselected" else _next_text(move, labels)
        if selected_assignment is None:
            name = '<span class="inactive">no account</span>'
            account_value = None
        else:
            label, line = selected_assignment
            name = _spans([labels[label], *(span for span in line["cells"]["roles"][1:] if span["text"] != " ✓")])
            account_value = label.casefold()
        selected_label = selected_assignment[0] if selected_assignment else None
        show_breakdown = any(
            label != selected_label and line["cells"]["total"][0]["text"] not in ("", "0")
            for label, line in assignments
        )
        values.append({"role": role.casefold(), "account": account_value,
                       "next": _next_sort_value(move)})
        count_cells: list[str] = []
        for key in ROLE_COUNT_COLUMNS:
            after = (_not_running_slot(f"role-not-running-{role}", _not_running_text(*not_running[role]), icon)
                     if key == "total" else "")
            cell, count = _role_count_cell(role, assignments, key, show_breakdown=show_breakdown, after=after)
            count_cells.append(cell)
            values[-1][key] = count
        color = accounts.account_color(role_span[0].get("color", ""))
        line_color = color.css if isinstance(color, accounts.AccountColor) else ""
        rows.append("<tr>" + _cell(_spans(role_span)) + _cell(_swatch(role_span))
                    + _cell(_busy_line(role, line_color, samples, reading.checked_at, reading.zone), "left busy-cell")
                    + _cell(name) + _cell(next_text, "left next-cell") + "".join(count_cells) + "</tr>")
    cells = total["cells"]
    rows.append('<tr data-summary>' + _cell(_spans(cells["label"])) + _cell("") * 4
                + "".join(_cell(_spans(cells[key]) + (_not_running_slot("role-not-running-total", all_text, icon)
                                                      if key == "total" else ""), "right")
                          for key in ROLE_COUNT_COLUMNS) + "</tr>")
    return _split_card("Roles", "roles", "role", ROLE_COLUMNS, rows, _sort_orders(values, ROLE_COUNT_COLUMNS),
                       descending=ROLE_COUNT_COLUMNS, unsorted=(BUSY_LINE_HEADING,))


def board(reading: accounts.StatusReading, alerts: Mapping[str, QuotaAlert] | None = None) -> str:
    """Render the account facts, roles, and problems from the same reading."""
    lines = accounts.status_board(reading, include_empty_total=True)["rows"]
    rows_by_account: dict[str, list[accounts.BoardRow]] = {}
    labels: dict[str, accounts.BoardSpan] = {}
    groups: dict[str, list[tuple[str, accounts.BoardRow]]] = {}
    label = ""
    for line in lines[:-1]:
        cells = line["cells"]
        if cells["label"]:
            label = cells["label"][0]["text"]
            labels[label] = cells["label"][0]
        rows_by_account.setdefault(label, []).append(line)
        role = cells["roles"][0]["text"]
        if role and role != "spare":
            groups.setdefault(role, []).append((label, line))
    selected = {row.label: row.selected_roles for row in reading.rows}
    colors = accounts.role_colors()
    samples = role_activity.read(reading.checked_at)
    return (_accounts_table(reading, rows_by_account, labels, alerts or {}, colors)
            + _roles_table(reading, groups, labels, selected, lines[-1], colors, samples))


def _model_cell(row: accounts.ModelsBoardRow, column: accounts.BoardColumn) -> str:
    key = column["key"]
    spans = row["cells"][key]
    attributes = ""
    if names := row["session_names"].get(key):
        cell_id = f'model-count-{row["model_name"]}-{row["effort"]}-{key}'
        attributes = _count_tooltip(cell_id, spans[0]["text"], names, "data-model-sessions")
    return f'<td class="{column["align"]}"{attributes}>{_spans(spans)}</td>'


def models_board(reading: accounts.StatusReading) -> str:
    colors = accounts.role_colors()
    table = accounts.models_board(reading, colors)
    role_labels = {role: _spans([accounts.board_role_span(role, colors)]) for role in accounts.MODEL_ROLES}
    head = "".join(_sort_heading(column, "model", accounts.MODEL_COUNT_COLUMNS,
                                label_markup=role_labels.get(column["key"], ""))
                   + ('<th class="left"></th>' if column["key"] == "model" else "")
                   for column in accounts.MODEL_COLUMNS)
    rows = "".join(("<tr data-summary>" if index == len(table.rows) - 1 else "<tr>") + "".join(
        _model_cell(row, column)
        + (f'<td class="left">{_swatch(row["cells"]["model"])}</td>' if column["key"] == "model" else "")
        for column in accounts.MODEL_COLUMNS) + "</tr>" for index, row in enumerate(table.rows))
    orders = html.escape(json.dumps(table.orders), quote=True)
    return (f'<section class="card models-card" data-sort-table="models" data-sort-orders="{orders}" '
            + 'data-detail-columns="0,1">'
            + f'<h2>Models</h2><div class="scroll"><table aria-label="Models"><thead><tr>{head}</tr></thead>'
            + f'<tbody>{rows}</tbody></table></div></section>')


def _step_markup(step: token_expiry.RenewalStep) -> str:
    prose = step.instruction if isinstance(step, token_expiry.CommandStep) else step
    markup = "".join(f"<code>{html.escape(part)}</code>" if index % 2 else html.escape(part)
                     for index, part in enumerate(prose.split("`")))
    if isinstance(step, token_expiry.CommandStep):
        markup += f'<code class="token-command" tabindex="0">{html.escape(step.command)}</code>'
    return markup


def _token_steps(token: token_expiry.TokenExpiry) -> str:
    heading = "Replace the token on the Mac" if token.key == token_expiry.MAC_TOKEN else f"Renew {token.name}"
    content = ("<ol>" + "".join(f"<li>{_step_markup(step)}</li>" for step in token.renewal_steps) + "</ol>"
               if token.renewal_steps else "<p>Set <code>mac_token.account</code> first.</p>")
    return (f'<details class="token-steps" data-token="{html.escape(token.key)}">'
            + f'<summary>{html.escape(heading)}</summary>{content}</details>')


def tokens_board(reading: accounts.StatusReading) -> str:
    mac = mac_fallback_token.read()
    now, zone = reading.checked_at, reading.zone
    settings = conf.read("showrunners")
    try:
        account = mac_fallback_token.configured_account(settings.values("mac_token.account").get("mac_token.account", ""))
    except (OSError, ValueError):
        account = mac_fallback_token.Unknown.VALUE

    try:
        leads = token_expiry.expiry_days(settings.values("token_expiry.lead_days").get("token_expiry.lead_days", ""))
    except (OSError, ValueError):
        leads = ()

    keys = settings.settings if isinstance(settings.settings, dict) else ()
    tokens = (token_expiry.mac_token(mac, account), *token_expiry.configured_tokens(keys, settings.values))
    rows: list[str] = []
    for token in tokens:
        alerts: list[str] = []
        if token.key == token_expiry.MAC_TOKEN and isinstance(mac, mac_fallback_token.MacFallbackToken):
            if isinstance(mac.active_login_failure(now), datetime):
                alerts.append('<span class="error">Login failed</span>')
            if not mac.recently_checked(now):
                alerts.append("Stale token data")
        end_date, days = token.end_date(zone), token.days_left(now, zone)
        end = f'<time datetime="{end_date}">{end_date}</time>' if end_date is not None else "Unknown"
        if token.expired(now, zone):
            alerts.append('<span class="error">Expired</span>')
        elif end_date is None:
            alerts.append("End date unavailable")
        elif not leads:
            alerts.append("Expiry schedule unavailable")
        elif token.expiry_lead(now, zone, leads) is not None:
            alerts.append('<span class="warning">Renew soon</span>')
        cells = (_cell(html.escape(token.name)), _cell(html.escape(token.purpose)), _cell(end),
                 _cell("Unknown" if days is None else str(max(days, 0)), "right"),
                 _cell("<br>".join(alerts) if alerts else "No active alert"))
        rows.append("<tr>" + "".join(cells) + "</tr>")
    head = "".join(f'<th class="{align}" scope="col">{heading}</th>' for heading, align in (
        ("token", "left"), ("purpose", "left"), ("ends", "left"), ("days left", "right"), ("state", "left")))
    steps = "".join(_token_steps(token) for token in tokens)
    return ('<section class="card tokens-card"><h2>Tokens</h2><div class="scroll">'
            f'<table aria-label="Tokens"><thead><tr>{head}</tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div>{steps}</section>')


def _move_duration(elapsed: timedelta) -> str:
    minutes, seconds = divmod(max(0, round(elapsed.total_seconds())), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours} h {minutes} min"
    if minutes:
        return f"{minutes} min" + (f" {seconds} s" if seconds else "")
    return f"{seconds} s"


def _move_time(at: datetime, zone: tzinfo) -> str:
    return html.escape(at.astimezone(zone).strftime("%a %H:%M %Z"))


def _move_cause(move: moves.MoveProgress) -> str:
    origin = move.initiator
    if origin is moves.MoveInitiator.AUTOMATIC:
        return "automatic" + (f" · {html.escape(move.reason)}" if move.reason else "")
    source = f"{origin.name} session" if isinstance(origin, moves.HandMoveSession) else origin.value
    return f"by hand · {html.escape(source)}"


def _move_result(moved: int, total: int, failed: int, already: int) -> str:
    counts = f"{moved} of {total} moved"
    if moved == total:
        return f'<span class="success bold">{counts}</span>'
    return (f'<span class="bold">{counts}</span>'
            + (f' · <span class="error">{failed} failed</span>' if failed else "")
            + (f' · <span class="next-muted">{already} already on target</span>' if already else ""))


def _retryable_members(history: Sequence[moves.MoveProgress], reading: accounts.StatusReading) -> set[tuple[str, str]]:
    failed = [(move.batch_id, row, row.state.target) for move in history for row in move.members
              if row.kind is not moves.MoveMemberKind.REQUEST and isinstance(row.state, moves.MemberMoveFailed)
              and isinstance(row.state.target, str)]
    if not failed:
        return set()
    try:
        try:
            record = staff.local_snapshot()
        except staff.StaffAbsent:
            record = staff.StaffSnapshot(())
        restarting: set[str] = set(shutdown_record.read()) if shutdown_record.active() else set()
    except (OSError, ValueError, staff.StaffRefused):
        return set()
    live = reading.live_claude_accounts.union((observed["session"], account.label)
        for account in reading.rows for observed in account.sessions if observed["state"] in ("busy", "idle"))
    retryable: set[tuple[str, str]] = set()
    for batch_id, row, target in failed:
        reference = row.member.reference
        owner = record.by_id(staff.StaffId(reference)) if row.kind is moves.MoveMemberKind.STAFF else None
        try:
            if row.kind is moves.MoveMemberKind.STAFF and owner is None:
                continue
            identity = str(owner.current_session if owner is not None else staff.native_id(reference))
            owner = owner or record.by_current_session(identity)
            if ((identity, target) in live or not isinstance(staff.row_activity(moves.native_row_key(identity)), staff.RowAvailable)
                    or (owner is not None and (owner.id in restarting
                        or not isinstance(staff.row_activity(owner.id), staff.RowAvailable)))
                    or moves.move_holds_session(identity)):
                continue
        except (OSError, ValueError, staff.StaffRefused):
            continue
        retryable.add((batch_id, reference))
    return retryable


def _move_failure(move: moves.MoveProgress, member: moves.MoveMember,
                  failure: moves.MemberMoveFailed, retryable: Collection[tuple[str, str]]) -> str:
    name = html.escape(member.name)
    action = "did not start on" if failure.stopped else "did not move to"
    message = (f"{name} {action} {html.escape(failure.target)}." if isinstance(failure.target, str)
               else f"{name} did not move.")
    stopped = f" {name} is stopped." if failure.stopped else ""
    retry = ""
    if (move.batch_id, member.reference) in retryable:
        retry = (f' <button type="button" data-retry-move="{html.escape(move.batch_id, quote=True)}"'
                 + f' data-move-member="{html.escape(member.reference, quote=True)}">Move {name} again</button>')
    return f'<p class="mv-line mv-fault"><span class="error">{message}</span> {html.escape(failure.reason)}{stopped}{retry}</p>'


def _move_members(move: moves.MoveProgress) -> str:
    rows: list[str] = []
    for row in move.members:
        if row.kind is moves.MoveMemberKind.REQUEST:
            continue
        state = row.state
        elapsed = ""
        if isinstance(state, moves.MemberMoved):
            destination = f" to {html.escape(state.target)}" if isinstance(state.target, str) and state.target != move.target else ""
            word = f'<span class="success">moved{destination}</span>'
            if state.recovered_holder:
                word += f' <span class="next-muted">after a hung {html.escape(state.recovered_holder)}</span>'
            elapsed = _move_duration(state.elapsed)
        elif isinstance(state, moves.MemberAlreadyOnTarget):
            word = f'<span class="next-muted">already on {html.escape(state.target)}</span>'
            elapsed = _move_duration(state.elapsed)
        elif isinstance(state, moves.MemberMoveFailed):
            word = f'<span class="error">{"did not start" if state.stopped else "did not move"}</span>'
            elapsed = _move_duration(state.elapsed)
        elif isinstance(state, moves.MemberMoving):
            label = state.target if isinstance(state.target, str) else move.target
            word = (f'<span class="mv-starting">starts on {html.escape(label)}</span>' if isinstance(label, str)
                    else '<span class="mv-starting">moving</span>')
        else:
            word = '<span class="next-muted">next</span>'
        rows.append("<tr>" + _cell(html.escape(row.member.name)) + _cell(word) + _cell(elapsed, "right") + "</tr>")
    if not rows:
        return ""
    return (f'<details class="mv-units" data-move-details="{html.escape(move.batch_id, quote=True)}">'
            + f'<summary>All {len(rows)} units</summary><div class="scroll"><table aria-label="Units of this move">'
            + '<thead><tr><th class="left">unit</th><th class="left">state</th><th class="right">time</th></tr></thead>'
            + "<tbody>" + "".join(rows) + "</tbody></table></div></details>")


def _move_current(move: moves.MoveProgress, now: datetime, zone: tzinfo, wait_seconds: float | Literal["configured"],
                  retryable: Collection[tuple[str, str]]) -> str:
    moved = [row.state for row in move.members if isinstance(row.state, moves.MemberMoved)]
    already = [row.state for row in move.members if isinstance(row.state, moves.MemberAlreadyOnTarget)]
    failed = [(row.member, row.state) for row in move.members if isinstance(row.state, moves.MemberMoveFailed)]
    moving = [row for row in move.members if isinstance(row.state, moves.MemberMoving)]
    queued = [row.member.name for row in move.members if isinstance(row.state, moves.MoveQueued)]
    ended = [state.ended for state in (*moved, *already)] + [state.ended for _, state in failed]
    total = len(move.members)
    target = html.escape(move.target) if isinstance(move.target, str) else "destination unavailable"
    if move.active:
        if ended:
            rate = (max(ended) - move.started) / len(ended)
            remaining = max(rate, rate * total - (now - move.started))
            timing = f"about {_move_duration(remaining)} left"
        else:
            timing = "time left unknown"
    else:
        finished = max(ended, default=move.started)
        timing = f"done {_move_time(finished, zone)} · {_move_duration(finished - move.started)}"
    cause = _move_cause(move)
    head = (f'<p class="mv-head"><span class="bold">{html.escape(move.group)} → {target}</span>'
            + f'<span class="next-muted"> · {cause} · started {_move_time(move.started, zone)}</span></p>')
    lines = [head]
    if any(row.kind is not moves.MoveMemberKind.REQUEST for row in move.members):
        segments = (("done", len(moved)), ("now", len(moving)), ("failed", len(failed)))
        bar = "".join(f'<span class="mv-{state}" style="width:{count / total:.3%}"></span>'
                      for state, count in segments if count)
        lines.extend((f'<div class="mv-bar" role="img" aria-label="{len(moved)} of {total} moved">{bar}</div>',
                      f'<p class="mv-line">{_move_result(len(moved), total, len(failed), len(already))}'
                      + f'<span class="next-muted"> · {timing}</span></p>'))
    else:
        lines.append(f'<p class="mv-line"><span class="next-muted">{timing}</span></p>')
    if moving:
        names = html.escape(_joined([row.member.name for row in moving]))
        verb = "starts" if len(moving) == 1 else "start"
        targets = {row.state.target if isinstance(row.state.target, str) else move.target
                   for row in moving if isinstance(row.state, moves.MemberMoving)}
        destination = next(iter(targets)) if len(targets) == 1 else moves.MoveDestinationUnselected()
        motion = f"{verb} on {html.escape(destination)}" if isinstance(destination, str) else (
            "is moving" if len(moving) == 1 else "are moving")
        lines.append(f'<p class="mv-line"><span class="next-muted">Now:</span> {names} {motion}.</p>')
    if queued:
        lines.append(f'<p class="mv-line"><span class="next-muted">Next:</span> {html.escape(queued[0])}.</p>')
    lines.extend(_move_failure(move, member, failure, retryable) for member, failure in failed)
    for row in move.members:
        if isinstance(row.state, moves.MemberMoved) and row.state.recovered_holder:
            if wait_seconds == "configured":
                wait_seconds = conf.read("shutdown").number("move.stuck_seconds")
            name, holder = html.escape(row.member.name), html.escape(row.state.recovered_holder)
            limit = _move_duration(timedelta(seconds=wait_seconds))
            lines.append(f'<p class="mv-line"><span class="next-muted">Note:</span> a hung {holder} held {name}. '
                         + f'The move ended it after {limit} and moved {name}.</p>')
    return "".join(lines) + _move_members(move)


def _move_history(history: Sequence[moves.MoveProgress], zone: tzinfo,
                  retryable: Collection[tuple[str, str]]) -> str:
    rows: list[str] = []
    for move in history:
        moved = [row.state for row in move.members if isinstance(row.state, moves.MemberMoved)]
        already = [row.state for row in move.members if isinstance(row.state, moves.MemberAlreadyOnTarget)]
        failed = [(row.member, row.state) for row in move.members if isinstance(row.state, moves.MemberMoveFailed)]
        result = _move_result(len(moved), len(move.members), len(failed), len(already))
        ended = [state.ended for state in (*moved, *already)] + [state.ended for _, state in failed]
        if all(row.kind is moves.MoveMemberKind.REQUEST for row in move.members):
            result = "".join(_move_failure(move, member, failure, retryable) for member, failure in failed)
        elif failed:
            result = (f'<details data-move-details="{html.escape(move.batch_id, quote=True)}"><summary>{result}</summary>'
                      + "".join(_move_failure(move, member, failure, retryable) for member, failure in failed) + "</details>")
        target = move.target if isinstance(move.target, str) else "destination unavailable"
        rows.append("<tr>" + _cell(_move_time(move.started, zone)) + _cell(html.escape(f"{move.group} → {target}"))
                    + _cell(_move_cause(move)) + _cell(result)
                    + _cell(_move_duration(max(ended, default=move.started) - move.started), "right") + "</tr>")
    return ('<details class="mv-earlier" data-move-details="earlier" open><summary>Earlier moves</summary>'
            + '<div class="scroll"><table aria-label="Earlier moves"><thead><tr><th class="left">started</th>'
            + '<th class="left">move</th><th class="left">cause</th><th class="left">result</th>'
            + '<th class="right">time</th></tr></thead><tbody>' + "".join(rows) + "</tbody></table></div></details>")


def moves_board(history: Sequence[moves.MoveProgress], now: datetime, zone: tzinfo,
                wait_seconds: float | Literal["configured"] = "configured",
                retryable: Collection[tuple[str, str]] = ()) -> str:
    active = [move for move in history if move.active]
    earlier = [move for move in history if not move.active]
    if not active and earlier:
        active.append(earlier.pop(0))
    current = ("".join(f'<div class="mv-current">{_move_current(move, now, zone, wait_seconds, retryable)}</div>' for move in active)
               or '<p class="mv-line"><span class="next-muted">No move in progress.</span></p>')
    return '<section class="card mv-card" aria-label="Moves"><h2>Moves</h2>' + current + _move_history(earlier, zone, retryable) + "</section>"


def page() -> bytes:
    """Read the stored accounts status, then render the page."""
    reading = accounts.read_status(source=accounts.StoredUsageOnly(), include_models=True)
    try:
        cleared = cleared_quotas(quota_state(review_link.review_root()))
        alerts = active_quotas(accounts.weekly_quotas(reading.rows), quota_alert_settings(), cleared)
    except (UnreadableQuotaState, InvalidQuotaThresholds) as error:
        reading = replace(reading, notes=(*reading.notes, str(error)))
        alerts = {}
    history = moves.read_move_progress()
    body = (moves_board(history, reading.checked_at, reading.zone, retryable=_retryable_members(history, reading))
            + board(reading, alerts) + models_board(reading) + tokens_board(reading))
    settings = conf.read("review_links")
    refresh_seconds = settings.number("page_refresh_seconds")
    request_timeout = settings.whole("page_request_timeout_seconds")
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            + '<meta name="viewport" content="width=device-width,initial-scale=1"><title>Accounts</title>'
            + f'{STYLE}</head><body><main data-refresh-seconds="{refresh_seconds}"'
            + f' data-request-timeout-seconds="{request_timeout}">' + page_frame.title("Accounts", "accounts", SCRIPT_VERSION)
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
[data-accounts] .card>h2:first-child{margin-top:0}
.mv-card.card{width:895px;box-sizing:border-box}
.mv-head,.mv-line{margin:0 0 6px;font-size:.93rem}
.mv-head{margin-bottom:8px}
.mv-bar{display:flex;height:10px;margin:0 0 8px;border-radius:5px;overflow:hidden;background:var(--line)}
.mv-done{background:var(--room)}
.mv-now{background:color-mix(in srgb,var(--room) 45%,var(--line))}
.mv-failed{background:var(--error)}
.mv-current+.mv-current,.mv-units,.mv-earlier{margin-top:10px;padding-top:10px;border-top:1px solid var(--line)}
.mv-fault button{margin-left:8px;border:0;border-radius:7px;padding:3px 12px;background:var(--filter-selected-bg);
color:var(--filter-selected-ink);font:inherit;font-weight:600;cursor:pointer}
.mv-fault button:disabled{opacity:.5;cursor:default}
.mv-card summary{width:fit-content;margin-left:-6px;padding:2px 6px;border-radius:4px;font-weight:700;cursor:pointer}
.mv-card :focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.mv-units summary,.mv-earlier>summary{font-size:.93rem}
.mv-earlier .mv-fault{white-space:normal;min-width:16ch}
.card{--column-gap:1.125rem;--table-font-size:.86rem;width:fit-content;max-width:100%;padding-right:var(--column-gap)}
.models-card,.tokens-card{margin-top:24px}
.token-steps{margin-top:12px;padding-top:12px;border-top:1px solid var(--line);font-size:var(--table-font-size)}
.token-steps summary{width:fit-content;margin-left:-6px;padding:2px 6px;border-radius:4px;
font-size:.93rem;font-weight:700;cursor:pointer}
.token-steps summary::marker{color:var(--ink-2)}
.token-steps summary:focus-visible{outline:2px solid var(--accent);outline-offset:1px}
.token-steps[open] summary{margin-bottom:5px}
.token-steps ol{margin:0;padding-left:1.5rem}
.token-steps li + li{margin-top:5px}
.token-steps li::marker,.token-steps p{color:var(--ink-2)}
.token-steps code{overflow-wrap:anywhere}
.token-steps .token-command{display:block;white-space:pre;overflow-x:auto;overflow-wrap:normal;
scrollbar-color:var(--ink-2) var(--line);scrollbar-width:thin}
.token-steps p{margin:0}
.roles-table td{vertical-align:top}
.busy-cell{padding-top:6px;padding-bottom:2px}
.busy-line{display:block;overflow:visible}
.busy-line text{font-size:""" + f"{BUSY_LINE_LABEL_PX}px" + """;fill:var(--ink-3);text-anchor:middle}
.busy-base{stroke:var(--line)}.busy-tick{stroke:var(--ink-3)}
.not-running-slot{display:inline-block;width:20px;text-align:right}
.not-running{display:inline-grid;place-items:center;vertical-align:-2px;color:var(--warning);border-radius:4px;cursor:default}
.not-running svg{width:14px;height:14px}
/* The codex pair rows keep their tint in every sort; next to each other they show no line between them. */
tr.auto-pair+tr.auto-pair td{border-top-color:transparent}
.model-tooltip{position:fixed;width:max-content;padding:7px 11px;background:var(--model-tooltip-bg);
color:var(--model-tooltip-ink);border-radius:6px;
font-size:12px;font-weight:500;box-shadow:0 3px 12px #0002;z-index:5;white-space:pre;overflow:auto}
:root{--model-tooltip-bg:#192339;--model-tooltip-ink:#fff}
@media(prefers-color-scheme:dark){:root:not([data-theme="light"]){--model-tooltip-bg:#e4eaf4;--model-tooltip-ink:#10151e}}
:root[data-theme="dark"]{--model-tooltip-bg:#e4eaf4;--model-tooltip-ink:#10151e}
.model-tooltip.role-counts{text-align:right;font-variant-numeric:tabular-nums}
.model-tooltip.tip-text{white-space:normal;line-height:1.45}
:is([data-model-sessions],[data-role-counts],[data-tip]):focus-visible{outline:2px solid var(--accent);outline-offset:-2px}
/* A tooltip word or icon is smaller than a cell, so its ring sits outside it. */
span[data-tip]:focus-visible{outline-offset:1px;border-radius:3px}
table{border-collapse:collapse;font-size:var(--table-font-size);font-variant-numeric:tabular-nums}
/* Let the last sort arrow use the right padding of the card. */
.card .scroll{margin-right:calc(-1 * var(--column-gap));padding-right:var(--column-gap)}
th,td{padding:5px 0 5px var(--column-gap);white-space:nowrap}th:first-child,td:first-child{padding-left:0}
th{color:var(--ink-2);font-weight:600;border-bottom:1px solid var(--line)}
th[aria-sort]{cursor:pointer}
th[aria-sort]:focus-visible{outline:2px solid var(--accent);outline-offset:-2px}
.sort-label{position:relative}
.sort-arrow{--sort-arrow-gap:calc(var(--table-font-size) * .25);position:absolute;top:50%;
left:calc(100% + var(--sort-arrow-gap));transform:translateY(-50%);width:var(--table-font-size);text-align:center;visibility:hidden}
th.right .sort-arrow{left:auto;right:calc(100% + var(--sort-arrow-gap))}
th[aria-sort=ascending] .sort-arrow,th[aria-sort=descending] .sort-arrow{visibility:visible}
.left{text-align:left}.right{text-align:right}
tbody tr td{border-top:1px solid var(--line)}
.roles-table tbody tr[data-summary] td{border-top:1px solid var(--ink-3)}
tbody tr:hover td{background:var(--hover)}
.bold{font-weight:600}.success{color:var(--room)}.error{color:var(--error)}.warning{color:var(--warning)}
.accounts-table{--reset-green:light-dark(color-mix(in srgb,var(--room) 60%,var(--ink)),
color-mix(in srgb,var(--room) 75%,var(--ink)));
--reset-orange:light-dark(color-mix(in srgb,var(--warning) 94%,var(--ink)),var(--warning))}
.weekly-reset-ready{color:var(--reset-green)}
.weekly-reset-exhausted{color:color-mix(in srgb,var(--reset-green),var(--reset-orange) var(--reset-orange-weight))}
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
.move-menu{position:fixed;z-index:5;margin:0;padding:6px;width:max-content;min-width:220px;max-width:calc(100vw - 16px);box-sizing:border-box;
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
.quota-panel{position:fixed;z-index:5;width:250px;max-width:calc(100vw - 16px);padding:11px 14px;
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

SCRIPT = page_frame.REFRESH_SCRIPT + r"""(() => {
let view = document.querySelector('[data-accounts]');
let newestRequest = 0;
const modelTooltip=document.querySelector('.model-tooltip');
const countCell=node=>node instanceof Element ? node.closest('[data-model-sessions],[data-role-counts],[data-tip]') : null;
// A sentence tooltip wraps at this width; a list tooltip keeps one line per item.
const tipWidth=320;
let hoveredCountCell=null, tooltipCountCell=null;
function positionCountTooltip(){
 const cell=tooltipCountCell;
 modelTooltip.hidden=true;
 if(!cell?.isConnected) return;
 const box=cell.getBoundingClientRect(), table=cell.closest('table').getBoundingClientRect();
 const scroll=cell.closest('.scroll').getBoundingClientRect();
 const left=Math.max(table.left,scroll.left,0), right=Math.min(table.right,scroll.right,innerWidth);
 const top=Math.max(table.top,scroll.top,0), bottom=Math.min(table.bottom,scroll.bottom,innerHeight);
 if(box.bottom<=top || box.top>=bottom || box.right<=left || box.left>=right) return;
 const {roleCounts,modelSessions}=cell.dataset;
 const tip=cell.hasAttribute('data-tip') ? document.getElementById(cell.getAttribute('aria-describedby'))?.textContent ?? '' : undefined;
 modelTooltip.classList.toggle('role-counts',roleCounts!==undefined);
 modelTooltip.classList.toggle('tip-text',tip!==undefined);
 modelTooltip.textContent=roleCounts ?? modelSessions ?? tip;
 modelTooltip.style.maxWidth=Math.min(right-left,tip===undefined ? Infinity : tipWidth)+'px';
 modelTooltip.style.maxHeight=(bottom-top)+'px';
 modelTooltip.hidden=false;
 const size=modelTooltip.getBoundingClientRect();
 const beside=box.right+size.width<=right ? box.right : box.left-size.width;
 modelTooltip.style.left=Math.max(left,Math.min(beside,right-size.width))+'px';
 modelTooltip.style.top=Math.max(top,Math.min(box.top,bottom-size.height))+'px';
}
function showCountTooltip(cell){tooltipCountCell=cell;positionCountTooltip();}
document.addEventListener('pointerover',event=>{
 const cell=countCell(event.target);
 if(cell && cell!==hoveredCountCell){hoveredCountCell=cell;showCountTooltip(cell);}
});
document.addEventListener('pointerout',event=>{
 if(!hoveredCountCell || (countCell(event.target)!==hoveredCountCell && !modelTooltip.contains(event.target))) return;
 if(countCell(event.relatedTarget)===hoveredCountCell || modelTooltip.contains(event.relatedTarget)) return;
 const departed=hoveredCountCell;
 hoveredCountCell=null;
 if(tooltipCountCell===departed) showCountTooltip(countCell(document.activeElement));
});
document.addEventListener('focusin',event=>{
 const cell=countCell(event.target);
 if(cell) showCountTooltip(cell);
});
document.addEventListener('focusout',event=>{
 if(tooltipCountCell && countCell(event.target)===tooltipCountCell) showCountTooltip(hoveredCountCell);
});
addEventListener('scroll',event=>{if(event.target!==modelTooltip) positionCountTooltip();},{capture:true,passive:true});
addEventListener('resize',positionCountTooltip);
document.addEventListener('keydown',event=>{
 if(event.key==='Escape' && !modelTooltip.hidden){hoveredCountCell=null;showCountTooltip(null);}
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
const sortSelector='th[aria-sort]';
const sortKey=header=>header.dataset.accountSort ?? header.dataset.modelSort ?? header.dataset.roleSort;
const sortTables=new WeakMap();
const sortChoices=new Map();
function applySort(card){
 const name=card.dataset.sortTable;
 const headers=[...card.querySelectorAll(sortSelector)];
 if(!sortChoices.has(name)){
  let choice={column:card.dataset.sortDefault ?? 'model',reverse:false};
  try{
   const saved=JSON.parse(localStorage.getItem(name+'-sort'));
   if(saved && headers.some(header=>sortKey(header)===saved.column) && typeof saved.reverse==='boolean') choice=saved;
  }catch{}
  sortChoices.set(name,choice);
 }
 const chosenSort=sortChoices.get(name);
 const table=card.querySelector('table'), body=table.tBodies[0];
 const order=JSON.parse(card.dataset.sortOrders)[chosenSort.column+(chosenSort.reverse ? '-reverse' : '')] ?? [];
 if(card.dataset.detailColumns){
  sortGroupedRows(table,body,order,card.dataset.detailColumns.split(',').map(Number));
 }else{
  if(!sortTables.has(table)) sortTables.set(table,[...body.rows].filter(row=>!row.hasAttribute('data-summary')));
  const rows=sortTables.get(table), total=body.querySelector('[data-summary]');
  for(const index of order) body.insertBefore(rows[index],total);
 }
 for(const header of headers){
  const selected=sortKey(header)===chosenSort.column;
  const descending=header.hasAttribute('data-sort-descending')!==(selected && chosenSort.reverse);
  header.setAttribute('aria-sort',selected ? descending ? 'descending' : 'ascending' : 'none');
  header.querySelector('.sort-arrow').textContent=descending ? '▼' : '▲';
 }
}
function sortGroupedRows(table,body,order,columns){
 const nameCell=columns[0], total=body.querySelector('[data-summary]');
 if(!sortTables.has(table)){
  let group='';
  sortTables.set(table,[...body.rows].filter(row=>row!==total).map(node=>{
   group=node.cells[nameCell].textContent || group;
   return {node,group};
  }));
 }
 const sources=sortTables.get(table), details=new Map();
 for(const {node,group} of sources) if(node.cells[nameCell].textContent){
  details.set(group,columns.map(index=>node.cells[index].innerHTML));
 }
 for(const place of order){
  const {node,group}=sources[place.index];
  columns.forEach((index,offset)=>{
   const cell=node.cells[index], content=place.details ? details.get(group)[offset] : '';
   if(cell.innerHTML!==content) cell.innerHTML=content;
  });
  node.classList.toggle('account',place.details);body.insertBefore(node,total);
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
 if(header && (event.key==='Enter' || event.key===' ')){event.preventDefault();chooseSort(header);}
});
applySorts();
// A refresh keeps each token's renewal steps open or closed as the viewer left them.
function keepOpenSteps(fresh){
 const open=new Set([...view.querySelectorAll('details[data-token][open]')].map(node=>node.dataset.token));
 for(const node of fresh.querySelectorAll('details[data-token]')) node.open=open.has(node.dataset.token);
}
function keepMoveDetails(fresh){
 const open=new Map([...view.querySelectorAll('[data-move-details]')].map(node=>[node.dataset.moveDetails,node.open]));
 for(const node of fresh.querySelectorAll('[data-move-details]')){
  if(open.has(node.dataset.moveDetails)) node.open=open.get(node.dataset.moveDetails);
 }
}
// A refresh keeps focus on the same summary or command of a token's renewal steps.
function keepStepFocus(){
 const steps=document.activeElement?.closest('details[data-token]');
 if(!steps) return null;
 const index=[...steps.querySelectorAll('summary,.token-command')].indexOf(document.activeElement);
 return ()=>[...view.querySelectorAll('details[data-token]')].find(node=>node.dataset.token===steps.dataset.token)
  ?.querySelectorAll('summary,.token-command')[index]?.focus({preventScroll:true});
}
async function refresh(supersede=false,current=()=>true) {
  const request = ++newestRequest;
  const response = await fetch(location.pathname, {cache: 'no-store'});
  if (!response.ok) throw new Error('HTTP ' + response.status);
  const page = new DOMParser().parseFromString(await response.text(), 'text/html');
  const fresh = page.querySelector('[data-accounts]');
  if (request !== newestRequest || !current()) return;
  const newer = reloadForNewScript(page, () => Boolean(hold()));
  if (newer || hold()) return newer;
  if (!fresh) throw new Error('The page has no accounts.');
  const top = scrollY;
  const restoreFocus = countCell(document.activeElement) ? restoreFocusAfterRefresh() : keepStepFocus() || keepMoveFocus();
  keepOpenSteps(fresh);
  keepMoveDetails(fresh);
  view.replaceWith(fresh);view = fresh;
  applySorts();
  scrollTo({top, behavior: 'instant'});
  hoveredCountCell=null;
  restoreFocus?.();
  showCountTooltip(countCell(document.activeElement));
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
  const response=await fetch(location.pathname,{method:'POST',signal:pageRequestSignal(),
   headers:{'Content-Type':'application/x-www-form-urlencoded'},
   body:new URLSearchParams({key:save.key,value:save.value}).toString()});
  const result=await response.json();
  if(!response.ok) throw new Error(result.reason || 'The color could not be saved.');
  for(const button of swatches(save.key)) button.dataset.savedColor=save.value;
 }catch(error){
  // After a timeout the save can still finish, so the color stays. The next refresh shows the true color.
  const unknown=saveTimeoutWords(error);
  if(unknown){for(const button of swatches(save.key)) button.dataset.savedColor=save.value;}
  else{
   for(const button of swatches(save.key)) button.dataset.savedColor=save.oldColor;
   paint(save.key,save.oldColor);
   if(selected?.dataset.colorKey===save.key){picker.value=pickerColor(selected,save.oldColor);readHex();}
  }
  message.textContent=save.name+': '+(unknown || error.message || 'The color could not be saved.');message.hidden=false;
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
 message.textContent='';message.hidden=true;close(false);
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
 const box=button.getBoundingClientRect();
 if(selected) close(true);
 const card=view.querySelector('[data-move-targets]');
 const account=button.dataset.moveAccount, group=button.dataset.moveGroup;
 const targets=JSON.parse(card.dataset.moveTargets).filter(([label])=>label!==account);
 menuRow=button.closest("tr");menuButton=button;button.setAttribute("aria-expanded","true");
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
function keepMoveFocus(){
 const focused=document.activeElement;
 if(focused?.matches('[data-retry-move]')){
  const {retryMove,moveMember}=focused.dataset;
  return ()=>[...view.querySelectorAll('[data-retry-move]')].find(button=>button.dataset.retryMove===retryMove
    && button.dataset.moveMember===moveMember)?.focus({preventScroll:true});
 }
 if(focused?.matches('[data-move-details]>summary')){
  const key=focused.parentElement.dataset.moveDetails;
  return ()=>[...view.querySelectorAll('[data-move-details]')].find(node=>node.dataset.moveDetails===key)
    ?.querySelector('summary')?.focus({preventScroll:true});
 }
 return null;
}
async function saveMove(form,label=form.group,button=null){
 const save={form};
 if(button){button.disabled=true;updates.abandon();}
 pendingMoves.add(save);message.textContent='';message.hidden=true;
 try{
  const response=await fetch(location.pathname,{method:'POST',signal:pageRequestSignal(),
   headers:{'Content-Type':'application/x-www-form-urlencoded'},
   body:new URLSearchParams(form).toString()});
  const result=await response.json();
  if(!response.ok) throw new Error(result.reason || 'The move could not be saved.');
 }catch(error){
  message.textContent=label+': '+(saveTimeoutWords(error) || error.message || 'The move could not be saved.');message.hidden=false;
 }finally{
  if(button) button.disabled=false;
  pendingMoves.delete(save);updates.changed();
  if(!hold()) void updates.refresh(true);
 }
}
document.addEventListener('click',event=>{
 const button=event.target instanceof Element ? event.target.closest('[data-retry-move]') : null;
 if(!button || button.disabled) return;
 void saveMove({retry_move:button.dataset.retryMove,member:button.dataset.moveMember},button.textContent,button);
});
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
 const form=item.dataset.moveClear ? {group:menuButton.dataset.moveGroup,clear:'first'}
  : {group:menuButton.dataset.moveGroup,to:item.dataset.moveTarget};
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
 quotaSaving=true;quotaClear.disabled=true;message.hidden=true;updates.abandon();
 try{
  const response=await fetch(location.pathname,{method:'POST',signal:pageRequestSignal(),
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
  message.textContent=saveTimeoutWords(error) || error.message || 'The quota alert could not be cleared.';message.hidden=false;
 }finally{
  quotaSaving=false;quotaClear.disabled=false;updates.changed();
  if(!hold()) void updates.refresh(true);
 }
});
for(const name of ['resize','scroll']) addEventListener(name,()=>closeQuota(false),{passive:true});
for(const name of ['resize','scroll','blur']) addEventListener(name,closeMenu,{passive:true});
})();"""
VERSION_PARTS = (POPUP, MENU, QUOTA_PANEL, STYLE, SCRIPT)
SCRIPT_VERSION = page_version.script_version(*VERSION_PARTS)


if __name__ == "__main__":
    raise SystemExit(main())
