"""Derive Review reply tokens from the shared key."""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
import shlex
import stat
import subprocess
from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from sys import platform

from scripts.lib.atomic_file import sync_folder
from scripts.lib.file_lock import exclusive
from scripts.production import change_routing


KEY_BYTES = 32
KEY_NAME = "reply-token.key"
_served_root = ContextVar[Path]("reply_key_served_root")


class UnavailableReplyToken(OSError):
    """The server cannot mint or derive a reply token."""


@dataclass(frozen=True)
class ReplyTokenProof:
    """The note fields for deriving and verifying one reply token."""

    nonce: str
    sha256: str


@dataclass(frozen=True)
class MintReplyProof:
    """Mint a proof if the native writer opens a new wait."""


MINT_REPLY_PROOF = MintReplyProof()


@contextmanager
def serving(root: Path) -> Generator[None]:
    """Use the active server's root for the key path check."""
    previous = _served_root.set(root)
    try:
        yield
    finally:
        _served_root.reset(previous)


def _inside_root(key: Path, root: Path) -> bool:
    key = key.resolve()
    root = root.resolve()
    if key.is_relative_to(root):
        return True
    try:
        root_stat = root.stat()
    except FileNotFoundError:
        return False
    for ancestor in key.parents:
        try:
            ancestor_stat = ancestor.stat()
        except FileNotFoundError:
            continue
        if os.path.samestat(ancestor_stat, root_stat):
            return True
    return False


def path() -> Path:
    state = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
    key = state / "review-links" / KEY_NAME
    try:
        active = _served_root.get(None)
        root = active if active is not None else change_routing.review_root()
        overlaps = _inside_root(key, root)
    except (OSError, RuntimeError, ValueError) as error:
        raise UnavailableReplyToken("Cannot check the reply key path.") from error
    if overlaps:
        raise UnavailableReplyToken("The reply key must be outside the served Review Links root.")
    return key


def valid(token: str) -> bool:
    return re.fullmatch(r"[A-Za-z0-9_-]{43}", token) is not None


def digest(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def matches(expected: str, token: str) -> bool:
    return valid(token) and hmac.compare_digest(expected, digest(token))


def matches_reference(expected: str, reference: str) -> bool:
    """Check a saved hash or an older raw delivery reference."""
    return matches(expected, reference) or (
        re.fullmatch(r"[0-9a-f]{64}", reference) is not None and hmac.compare_digest(expected, reference))


def _read_key(key_path: Path) -> bytes:
    try:
        descriptor = os.open(key_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            mode = os.fstat(stream.fileno()).st_mode
            if not stat.S_ISREG(mode) or stat.S_IMODE(mode) != 0o600:
                raise UnavailableReplyToken("The local reply key must be a regular file with mode 0600.")
            key = stream.read(KEY_BYTES + 1)
    except (FileNotFoundError, UnavailableReplyToken):
        raise
    except OSError as error:
        raise UnavailableReplyToken("Cannot read the local reply key.") from error
    if len(key) != KEY_BYTES:
        raise UnavailableReplyToken("The local reply key must contain 32 bytes.")
    return key


def _copy_key() -> bytes:
    code = (
        "import sys; from scripts.review_links.reply_tokens import _read_key, path; "
        "sys.stdout.buffer.write(_read_key(path()))"
    )
    command = '"$HOME/.claude/scripts/lib/py" -B -c ' + shlex.quote(code)
    try:
        result = subprocess.run(["ssh", "-o", "BatchMode=yes", "natedev", command],
                                stdin=subprocess.DEVNULL, capture_output=True, timeout=30, check=True)
    except (OSError, subprocess.SubprocessError) as error:
        raise UnavailableReplyToken("Cannot copy the reply key from natedev.") from error
    if len(result.stdout) != KEY_BYTES:
        raise UnavailableReplyToken("The reply key from natedev must contain 32 bytes.")
    return result.stdout


def _ensure_key(key_path: Path) -> bytes:
    try:
        key_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with exclusive(key_path.with_suffix(".lock")):
            try:
                return _read_key(key_path)
            except FileNotFoundError:
                key = _copy_key() if platform == "darwin" else secrets.token_bytes(KEY_BYTES)
            try:
                descriptor = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                return _read_key(key_path)
            with os.fdopen(descriptor, "wb") as stream:
                os.fchmod(stream.fileno(), 0o600)
                _ = stream.write(key)
                stream.flush()
                os.fsync(stream.fileno())
            sync_folder(key_path.parent)
            return key
    except UnavailableReplyToken:
        raise
    except OSError as error:
        raise UnavailableReplyToken("Cannot create the local reply key.") from error


def valid_nonce(nonce: str) -> bool:
    return re.fullmatch(r"[0-9a-f]{32}", nonce) is not None


def _token(key: bytes, nonce: str) -> str:
    code = hmac.digest(key, bytes.fromhex(nonce), "sha256")
    return "r" + base64.urlsafe_b64encode(code).decode("ascii")[:42]


def mint() -> ReplyTokenProof:
    """Create a new nonce and its reply token hash."""
    key = _ensure_key(path())
    nonce = secrets.token_hex(16)
    return ReplyTokenProof(nonce, digest(_token(key, nonce)))


def derive(proof: ReplyTokenProof) -> str:
    """Return the token only when the key and nonce reproduce the note hash."""
    if not valid_nonce(proof.nonce):
        raise UnavailableReplyToken("The note has no valid reply nonce.")
    key_path = path()
    if platform == "darwin":
        key = _ensure_key(key_path)
    else:
        try:
            key = _read_key(key_path)
        except FileNotFoundError as error:
            raise UnavailableReplyToken("The local reply key is missing.") from error
    token = _token(key, proof.nonce)
    if not matches(proof.sha256, token):
        raise UnavailableReplyToken("The local reply key does not match the note.")
    return token
