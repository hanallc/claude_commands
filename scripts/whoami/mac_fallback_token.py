"""Renew the Mac relay token and publish its observation dates."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import termios
from contextlib import ExitStack
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone, tzinfo
from enum import Enum
from pathlib import Path

from scripts.lib import atomic_file, conf, file_lock
from scripts.lib.user_zone import user_zone
from scripts.whoami import note_file

ENV_NAME = "CLAUDE_CODE_OAUTH_TOKEN"
NOTE_NAME = "Mac fallback token.md"
ALERT_IDS = frozenset({"quota.mac_token_expiry", "quota.mac_token_login"})
OBSERVATION_INTERVAL = timedelta(hours=1)
OBSERVATION_MAX_AGE = timedelta(days=1)


class Unknown(Enum):
    VALUE = "unknown"


class TokenUnavailable(Enum):
    ABSENT = "no encrypted token"


class TokenDecryptFailed(OSError):
    """GPG returned a nonzero exit status."""


@dataclass(frozen=True)
class MacFallbackToken:
    made: datetime
    checked_at: datetime | Unknown = Unknown.VALUE
    last_good_use: datetime | Unknown = Unknown.VALUE
    last_login_failure: datetime | Unknown = Unknown.VALUE

    @property
    def expires(self) -> datetime:
        return self.made + timedelta(days=365)

    def recently_checked(self, now: datetime, max_age: timedelta = OBSERVATION_MAX_AGE) -> bool:
        return isinstance(self.checked_at, datetime) and timedelta(0) <= now - self.checked_at <= max_age

    def active_login_failure(self, now: datetime) -> datetime | Unknown:
        failure, good = self.last_login_failure, self.last_good_use
        if isinstance(failure, datetime) and failure <= now and (not isinstance(good, datetime) or good < failure):
            return failure
        return Unknown.VALUE

    def days_left(self, now: datetime, zone: tzinfo) -> int:
        return (self.expires.astimezone(zone).date() - now.astimezone(zone).date()).days

    def expiry_lead(self, now: datetime, zone: tzinfo, leads: tuple[int, ...]) -> int | None:
        if now >= self.expires:
            return None
        left = self.days_left(now, zone)
        return next((lead for lead in leads if left <= lead), None)


def configured_account(settings: conf.ConfigRead) -> note_file.NumberedAccountLabel | Unknown:
    value = settings.values("mac_token.account").get("mac_token.account", "")
    label = note_file.parse_account_label(value)
    return label if isinstance(label, note_file.NumberedAccountLabel) and label.tool == "claude" else Unknown.VALUE


def expiry_days(settings: conf.ConfigRead) -> tuple[int, ...]:
    days = tuple(int(word) for word in settings.words("mac_token.expiry_days"))
    if not days or len(set(days)) != len(days) or any(not 1 <= day <= 365 for day in days):
        raise ValueError("Mac token expiry days must be distinct and between 1 and 365")
    return tuple(sorted(days))


def on_mac() -> bool:
    return sys.platform == "darwin"


def encrypted_path() -> Path:
    return Path.home() / ".config/claude/oauth-token.gpg"


def made_at(path: Path) -> datetime | TokenUnavailable:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
    except FileNotFoundError:
        return TokenUnavailable.ABSENT


def prepare_child(environment: dict[str, str]) -> datetime | TokenUnavailable:
    """Put the decrypted value in the child environment and return its file date."""
    path = encrypted_path()
    try:
        made = made_at(path)
        if isinstance(made, TokenUnavailable):
            return made
        result = subprocess.run(["gpg", "--pinentry-mode", "error", "--quiet", "--decrypt", str(path)],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=10, check=False)
        if result.returncode:
            raise TokenDecryptFailed()
        if made_at(path) != made:
            raise ValueError("the encrypted Mac token file changed")
        value = result.stdout.strip()
        if not value:
            raise ValueError("GPG returned no Mac fallback token")
    except UnicodeError:
        raise ValueError("GPG returned unreadable Mac token data") from None
    # See https://code.claude.com/docs/en/authentication#authentication-precedence.
    # See https://code.claude.com/docs/en/env-vars for endpoint overrides.
    # See https://code.claude.com/docs/en/network-config for TLS overrides.
    for name in (
        "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
        "CLAUDE_CODE_USE_ANTHROPIC_AWS", "CLAUDE_CODE_USE_ANTHROPIC_GOOGLE_CLOUD", "CLAUDE_CODE_USE_MANTLE",
        "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY",
        "ANTHROPIC_BASE_URL", "ANTHROPIC_AWS_BASE_URL", "ANTHROPIC_BEDROCK_BASE_URL",
        "ANTHROPIC_BEDROCK_MANTLE_BASE_URL", "ANTHROPIC_FOUNDRY_BASE_URL",
        "ANTHROPIC_GOOGLE_CLOUD_BASE_URL", "ANTHROPIC_VERTEX_BASE_URL", "ANTHROPIC_CUSTOM_HEADERS",
        "http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY",
        "NODE_EXTRA_CA_CERTS", "NODE_TLS_REJECT_UNAUTHORIZED", "SSL_CERT_FILE", "SSL_CERT_DIR",
        "NODE_OPTIONS", "NODE_USE_SYSTEM_CA", "OPENSSL_CONF",
        "CLAUDE_CODE_CERT_STORE", "CLAUDE_CODE_CLIENT_CERT", "CLAUDE_CODE_CLIENT_KEY",
        "CLAUDE_CODE_CLIENT_KEY_PASSPHRASE", "CLAUDE_CODE_DISABLE_MTLS_RELOAD_ON_STALE_CONNECTION",
    ):
        _ = environment.pop(name, None)
    environment["CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST"] = "1"
    environment[ENV_NAME] = value
    return made


def _timestamp(value: str) -> datetime | Unknown:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return Unknown.VALUE
    return parsed if parsed.utcoffset() is not None else Unknown.VALUE


def _read(path: Path) -> MacFallbackToken | TokenUnavailable:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return TokenUnavailable.ABSENT
    if not text.startswith("---\n") or (end := text.find("\n---\n", 4)) < 0:
        return TokenUnavailable.ABSENT
    fields = {match[1]: match[2] for line in text[4:end].splitlines() if (match := note_file.FIELD.fullmatch(line))}
    made = _timestamp(fields.get("made", ""))
    if not isinstance(made, datetime):
        return TokenUnavailable.ABSENT
    return MacFallbackToken(made, _timestamp(fields.get("checked_at", "")),
                            _timestamp(fields.get("last_good_use", "")), _timestamp(fields.get("last_login_failure", "")))


def read() -> MacFallbackToken | TokenUnavailable:
    try:
        return _read(note_file.notes_dir() / NOTE_NAME)
    except (OSError, ValueError):
        return TokenUnavailable.ABSENT


def _write(path: Path, token: MacFallbackToken) -> None:
    fields = {"made": token.made.isoformat(), "checked_at": _time_value(token.checked_at),
              "last_good_use": _time_value(token.last_good_use), "last_login_failure": _time_value(token.last_login_failure)}
    content = "---\n" + "".join(f"{key}: {value}\n" for key, value in fields.items()) + "---\n"
    atomic_file.write_text_atomic(path, content)


def _time_value(at: datetime | Unknown) -> str:
    return at.isoformat() if isinstance(at, datetime) else "unknown"


class RelayLogin(Enum):
    GOOD = "good"
    FAILED = "failed"
    UNCONFIRMED = "unconfirmed"


def _lock_path() -> Path:
    return Path.home() / ".local/state/agent-notes/mac-fallback-token.lock"


def publish_metadata(*, now: datetime | None = None) -> None:
    """Publish an observation of the token file from the Mac notes timer."""
    if not on_mac():
        return
    path = note_file.notes_dir() / NOTE_NAME
    lock = _lock_path()
    lock.parent.mkdir(parents=True, exist_ok=True)
    with file_lock.exclusive(lock):
        made = made_at(encrypted_path())
        if isinstance(made, TokenUnavailable):
            path.unlink(missing_ok=True)
            return
        previous = _read(path)
        token = previous if isinstance(previous, MacFallbackToken) and previous.made == made else MacFallbackToken(made)
        at = now if now is not None else datetime.now(timezone.utc)
        if not token.recently_checked(at, OBSERVATION_INTERVAL):
            _write(path, replace(token, checked_at=at))


def record_relay(made: datetime, login: RelayLogin, now: datetime) -> None:
    """Record a confirmed result for the same token file."""
    if not on_mac() or login is RelayLogin.UNCONFIRMED:
        return
    path = note_file.notes_dir() / NOTE_NAME
    lock = _lock_path()
    lock.parent.mkdir(parents=True, exist_ok=True)
    with file_lock.exclusive(lock):
        if made_at(encrypted_path()) != made:
            return
        previous = _read(path)
        token = previous if isinstance(previous, MacFallbackToken) and previous.made == made else MacFallbackToken(made)
        if login is RelayLogin.GOOD and (not isinstance(token.last_good_use, datetime) or token.last_good_use < now):
            _write(path, replace(token, last_good_use=now))
        if login is RelayLogin.FAILED and (not isinstance(token.last_login_failure, datetime) or token.last_login_failure < now):
            _write(path, replace(token, last_login_failure=now))



class RenewalRefused(Exception):
    """Report a fixed message that contains no token data."""


class ClipboardClearFailed(Exception):
    """The clipboard could not be cleared."""


def _clear_clipboard() -> None:
    try:
        result = subprocess.run(["pbcopy"], input=b"", stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                check=False)
    except OSError:
        raise ClipboardClearFailed from None
    if result.returncode:
        raise ClipboardClearFailed


def _read_new_token(after_prompt: ExitStack) -> bytes:
    with open("/dev/tty", "r+b", buffering=0) as terminal:
        original = termios.tcgetattr(terminal)
        hidden = original.copy()
        hidden[3] &= ~(termios.ECHO | termios.ECHONL)
        try:
            termios.tcsetattr(terminal, termios.TCSAFLUSH, hidden)
            _ = after_prompt.callback(_clear_clipboard)
            try:
                _ = terminal.write(b"\033[?2004hPaste the new token, then press Enter: ")
                line = terminal.readline()
                start, end = b"\033[200~", b"\033[201~"
                if not line.startswith(start):
                    raise RenewalRefused("Paste the token. Do not type it.")
                line = line.removeprefix(start)
                lines: list[bytes] = []
                while line:
                    content, marker, _ = line.partition(end)
                    lines.append(content.translate(None, b"\r\n"))
                    if marker:
                        return b"".join(lines)
                    line = terminal.readline()
                raise RenewalRefused("The paste did not finish.")
            finally:
                _ = terminal.write(b"\033[?2004l\n\033[3J\033[H\033[2J\033]1337;ClearScrollback\a")
        finally:
            termios.tcsetattr(terminal, termios.TCSAFLUSH, original)


def _encrypt_replacement(path: Path, value: bytes) -> datetime:
    text = value.decode("utf-8", errors="surrogateescape").strip()
    if not text:
        raise RenewalRefused("No token entered.")
    if any(character.isspace() for character in text):
        raise RenewalRefused("The token contains whitespace.")
    value = text.encode("utf-8", errors="surrogateescape")
    del text
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".oauth-token.", dir=path.parent)
    temporary = Path(name)
    try:
        os.close(descriptor)
        try:
            result = subprocess.run(["gpg", "--batch", "--yes", "--encrypt", "--recipient", "gh token",
                                     "--output", str(temporary)], input=value, umask=0o077,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        finally:
            del value
        if result.returncode:
            raise RenewalRefused("GPG encryption failed.")
        info = temporary.stat()
        if not info.st_size:
            raise RenewalRefused("GPG produced no encrypted data.")
        made = datetime.fromtimestamp(info.st_mtime, timezone.utc)
        _ = temporary.replace(path)
        return made
    finally:
        temporary.unlink(missing_ok=True)


def renew() -> int:
    if not on_mac():
        print("Run this command in a Mac terminal.", file=sys.stderr)
        return 1
    try:
        account = configured_account(conf.read("showrunners"))
    except (OSError, ValueError):
        account = Unknown.VALUE
    if isinstance(account, Unknown):
        print("Set mac_token.account to a numbered Claude account first.", file=sys.stderr)
        return 1

    try:
        zone = user_zone()
    except (OSError, ValueError):
        print("Cannot read the configured user zone.", file=sys.stderr)
        return 1

    scripts = Path(__file__).resolve().parents[1]
    command = [str(scripts / "lib/py"), str(scripts / "accounts/accounts.py"), "exec", account.label, "setup-token"]
    try:
        setup = subprocess.run(command, check=False)
    except OSError:
        print(f"Cannot start {account.label} setup-token.", file=sys.stderr)
        return 1
    if setup.returncode:
        print(f"{account.label} setup-token failed.", file=sys.stderr)
        return 1

    made: datetime | None = None
    clipboard_failed = False
    try:
        with ExitStack() as after_prompt:
            made = _encrypt_replacement(encrypted_path(), _read_new_token(after_prompt))
    except ClipboardClearFailed:
        clipboard_failed = True
    except RenewalRefused as error:
        print(error, file=sys.stderr)
        return 1
    except (OSError, termios.error):
        print("Cannot read or store the new Mac fallback token.", file=sys.stderr)
        return 1

    metadata_failed = False
    if made is not None:
        try:
            publish_metadata()
        except (OSError, ValueError):
            metadata_failed = True
    if clipboard_failed:
        file_status = "Replaced the encrypted file." if made is not None else "The encrypted file did not change."
        print(f"Could not clear the Mac clipboard. {file_status}", file=sys.stderr)
        return 1
    assert made is not None
    expires = MacFallbackToken(made).expires.astimezone(zone).date()
    if metadata_failed:
        print(f"{account.label} expires {expires}. The Accounts page update failed.", file=sys.stderr)
        return 1
    print(f"{account.label} expires {expires}.")
    return 0


def main() -> int:
    if sys.argv[1:] != ["renew"]:
        print("Usage: mac_fallback_token.py renew", file=sys.stderr)
        return 2
    try:
        return renew()
    except KeyboardInterrupt:
        print("Renewal interrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
