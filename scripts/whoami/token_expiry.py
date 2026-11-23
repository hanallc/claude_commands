"""Read token end dates and calculate the current reminder window."""
from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, tzinfo
import re

from scripts.whoami import mac_fallback_token, note_file


EXPIRY_ALERT_ID = "quota.token_expiry"
LEGACY_MAC_EXPIRY_ID = "quota.mac_token_expiry"
ALERT_IDS = frozenset({EXPIRY_ALERT_ID, "quota.mac_token_login"})
MAC_TOKEN = "mac_fallback"
MAC_NAME = "Mac fallback token"
END_KEY = re.compile(r"token_expiry\.([a-zA-Z0-9_-]+)\.ends")


@dataclass(frozen=True)
class CalendarEnd:
    day: date


@dataclass(frozen=True)
class ExpiryInstant:
    time: datetime


@dataclass(frozen=True)
class EndUnavailable:
    pass


@dataclass(frozen=True)
class CommandStep:
    instruction: str
    command: str


type RenewalStep = str | CommandStep
type TokenEnd = CalendarEnd | ExpiryInstant | EndUnavailable


@dataclass(frozen=True)
class TokenExpiry:
    key: str
    name: str
    source: str
    purpose: str
    ends: TokenEnd
    renewal_steps: tuple[RenewalStep, ...]

    def end_date(self, zone: tzinfo) -> date | None:
        match self.ends:
            case CalendarEnd(day):
                return day
            case ExpiryInstant(at):
                return at.astimezone(zone).date()
            case EndUnavailable():
                return None

    def days_left(self, now: datetime, zone: tzinfo) -> int | None:
        end_date = self.end_date(zone)
        return None if end_date is None else (end_date - now.astimezone(zone).date()).days

    def expired(self, now: datetime, zone: tzinfo) -> bool:
        match self.ends:
            case CalendarEnd(day):
                return now.astimezone(zone).date() > day
            case ExpiryInstant(at):
                return now >= at
            case EndUnavailable():
                return False

    def expiry_lead(self, now: datetime, zone: tzinfo, leads: tuple[int, ...]) -> int | None:
        left = self.days_left(now, zone)
        if left is None or self.expired(now, zone):
            return None
        return next((lead for lead in leads if left <= lead), None)


@dataclass(frozen=True)
class ConfigToken:
    purpose: str
    renewal_steps: tuple[RenewalStep, ...]


CONFIG_TOKENS = {
    "hana_linux_runners": ConfigToken("Registers the Hana Linux CI runners.", (
        "Regenerate `hana-linux-runners` on GitHub for `hanallc/hana`. Keep Administration read and write. Set 366 days.",
        "Save the new value in `natedev - hana self-hosted ci runner token` in the `hana` vault in 1Password.",
        CommandStep("Run this command in a plain natedev terminal.",
                    "~/.claude/scripts/lib/py ~/.claude/scripts/whoami/hana_runner_token.py reseal"),
        "Commit `/etc/nixos` and rebuild natedev.",
    )),
    "hana_macos_runner_query": ConfigToken("Checks runner availability for optional Hana macOS CI.", (
        "Regenerate `hana-macos-runner-query` on GitHub for `hanallc/hana`. Keep Administration read only.",
        CommandStep("Run this command and paste the new value.", "gh secret set MACOS_RUNNER_QUERY_TOKEN --repo hanallc/hana"),
    )),
}


def expiry_days(value: str) -> tuple[int, ...]:
    days = tuple(int(word) for word in value.split())
    if not days or len(set(days)) != len(days) or any(not 1 <= day <= 365 for day in days):
        raise ValueError("Token expiry days must be distinct and between 1 and 365")
    return tuple(sorted(days))


def mac_token(record: mac_fallback_token.MacFallbackToken | mac_fallback_token.TokenUnavailable,
              account: note_file.NumberedAccountLabel | mac_fallback_token.Unknown) -> TokenExpiry:
    ends = ExpiryInstant(record.expires) if isinstance(record, mac_fallback_token.MacFallbackToken) else EndUnavailable()
    if isinstance(account, mac_fallback_token.Unknown):
        return TokenExpiry(MAC_TOKEN, MAC_NAME, mac_fallback_token.NOTE_NAME, "Signs in the Mac Claude relay.", ends, ())
    return TokenExpiry(MAC_TOKEN, MAC_NAME, mac_fallback_token.NOTE_NAME,
                       f"Signs in the Mac Claude relay for {account.label}.", ends, (
        "In a plain Terminal or iTerm window, not in a herdr pane, run `mac-token-renew`.",
        f"Approve access in the browser while signed in to {account.label}.",
        "Copy the token that setup-token shows, then paste it at the hidden prompt and press Enter.",
    ))


def configured_tokens(configured_keys: Iterable[str],
                      read_values: Callable[[str], Mapping[str, str]]) -> tuple[TokenExpiry, ...]:
    keys = dict.fromkeys(CONFIG_TOKENS)
    for key in configured_keys:
        if match := END_KEY.fullmatch(key):
            keys[match[1]] = None

    tokens: list[TokenExpiry] = []
    for key in keys:
        source = f"token_expiry.{key}.ends"
        ends: CalendarEnd | EndUnavailable = EndUnavailable()
        try:
            value = read_values(source).get(source, "")
            if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
                ends = CalendarEnd(date.fromisoformat(value))
        except (OSError, ValueError):
            pass
        details = CONFIG_TOKENS.get(key, ConfigToken("Purpose unavailable.", ("Renew the token with its issuer.",)))
        tune = CommandStep("Set the new end date.", f"/tune showrunners {source} <YYYY-MM-DD>")
        tokens.append(TokenExpiry(source, key.replace("_", "-"), source, details.purpose, ends,
                                  (*details.renewal_steps, tune)))
    return tuple(tokens)
