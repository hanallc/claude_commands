"""Exercise the Mac token relay with a private home and fake GPG."""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import termios
import tempfile
import unittest
from collections.abc import Buffer, Generator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import cast, final, override
from unittest.mock import patch

from scripts.accounts import accounts
from scripts.lib import conf, file_lock, models_registry
from scripts.message import send
from scripts.notify import push_texts
from scripts.notify.user_action import ActionRequired
from scripts.whoami import mac_fallback_token, token_expiry


class MacFallbackTokenTests(unittest.TestCase):
    def test_mac_timer_publishes_the_file_date_without_decryption(self) -> None:
        home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        notes = home / "agents"
        notes.mkdir()
        made = datetime(2026, 10, 1, 19, 23, tzinfo=timezone.utc)
        with (patch.dict(os.environ, {"HOME": str(home), "AGENT_NOTES_DIR": str(notes)}, clear=True),
              patch.object(mac_fallback_token, "on_mac", return_value=True),
              patch.object(subprocess, "run", side_effect=AssertionError("unexpected process"))):
            path = mac_fallback_token.encrypted_path()
            path.parent.mkdir(parents=True)
            path.touch()
            os.utime(path, (made.timestamp(), made.timestamp()))
            mac_fallback_token.publish_metadata()
            token = mac_fallback_token.read()
            assert isinstance(token, mac_fallback_token.MacFallbackToken)
            self.assertEqual(token.made, made)
            self.assertIsInstance(token.checked_at, datetime)
            assert isinstance(token.checked_at, datetime)
            mac_fallback_token.publish_metadata(now=token.checked_at + timedelta(minutes=30))
            self.assertEqual(mac_fallback_token.read(), token)
            next_check = token.checked_at + timedelta(hours=1, seconds=1)
            mac_fallback_token.publish_metadata(now=next_check)
            refreshed = mac_fallback_token.read()
            assert isinstance(refreshed, mac_fallback_token.MacFallbackToken)
            self.assertEqual(refreshed.checked_at, next_check)

    def test_waiting_failure_survives_a_newer_success_that_gets_the_lock_first(self) -> None:
        home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        notes = home / "agents"
        notes.mkdir()
        made = datetime(2026, 10, 1, 19, tzinfo=timezone.utc)
        first_failure = made + timedelta(days=1)
        newest_failure = made + timedelta(days=2)
        newest_good = made + timedelta(days=3)
        exclusive = file_lock.exclusive

        @contextmanager
        def success_first(lock: Path) -> Generator[file_lock.LockResult]:
            with patch.object(file_lock, "exclusive", exclusive):
                mac_fallback_token.record_relay(made, mac_fallback_token.RelayLogin.GOOD, newest_good)
            with exclusive(lock) as acquired:
                yield acquired

        with (patch.dict(os.environ, {"HOME": str(home), "AGENT_NOTES_DIR": str(notes)}, clear=True),
              patch.object(mac_fallback_token, "on_mac", return_value=True)):
            path = mac_fallback_token.encrypted_path()
            path.parent.mkdir(parents=True)
            path.touch()
            os.utime(path, (made.timestamp(), made.timestamp()))
            mac_fallback_token.record_relay(made, mac_fallback_token.RelayLogin.FAILED, first_failure)
            with patch.object(file_lock, "exclusive", side_effect=success_first):
                mac_fallback_token.record_relay(made, mac_fallback_token.RelayLogin.FAILED, newest_failure)
            mac_fallback_token.record_relay(made, mac_fallback_token.RelayLogin.FAILED, first_failure)
            mac_fallback_token.record_relay(made, mac_fallback_token.RelayLogin.GOOD, newest_failure)
            token = mac_fallback_token.read()
        assert isinstance(token, mac_fallback_token.MacFallbackToken)
        self.assertEqual(token.last_good_use, newest_good)
        self.assertEqual(token.last_login_failure, newest_failure)
        self.assertIs(token.active_login_failure(newest_good), mac_fallback_token.Unknown.VALUE)

    def test_relay_child_receives_the_token_and_the_selected_folder(self) -> None:
        self.check_relay(authentication_failed=False)

    def test_relay_saves_only_metadata_when_authentication_fails(self) -> None:
        self.check_relay(authentication_failed=True)

    def test_token_relay_removes_higher_priority_environment_credentials(self) -> None:
        self.check_relay(authentication_failed=False, removed_variables=(
            "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
            "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY",
        ))

    def test_token_relay_removes_endpoint_overrides(self) -> None:
        self.check_relay(authentication_failed=False, removed_variables=(
            "ANTHROPIC_BASE_URL", "ANTHROPIC_AWS_BASE_URL", "ANTHROPIC_BEDROCK_BASE_URL",
            "ANTHROPIC_BEDROCK_MANTLE_BASE_URL", "ANTHROPIC_FOUNDRY_BASE_URL",
            "ANTHROPIC_GOOGLE_CLOUD_BASE_URL", "ANTHROPIC_VERTEX_BASE_URL", "ANTHROPIC_CUSTOM_HEADERS",
            "CLAUDE_CODE_USE_ANTHROPIC_AWS", "CLAUDE_CODE_USE_ANTHROPIC_GOOGLE_CLOUD", "CLAUDE_CODE_USE_MANTLE",
            "http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY",
            "NODE_EXTRA_CA_CERTS", "NODE_TLS_REJECT_UNAUTHORIZED", "SSL_CERT_FILE", "SSL_CERT_DIR",
            "NODE_OPTIONS", "NODE_USE_SYSTEM_CA", "OPENSSL_CONF",
            "CLAUDE_CODE_CERT_STORE", "CLAUDE_CODE_CLIENT_CERT", "CLAUDE_CODE_CLIENT_KEY",
            "CLAUDE_CODE_CLIENT_KEY_PASSPHRASE", "CLAUDE_CODE_DISABLE_MTLS_RELOAD_ON_STALE_CONNECTION",
        ))

    def test_relay_keeps_confirmed_delivery_if_a_later_login_fails(self) -> None:
        self.check_relay(authentication_failed=True, confirmed_delivery=True)

    def test_gpg_timeout_reports_a_preparation_failure(self) -> None:
        message = self.relay_fixture()
        with (patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired("gpg", 10)),
              patch.object(send, "_bound_relay", side_effect=AssertionError("unexpected relay"))):
            result = send.relay(message, 10)
        self.assertEqual(result.outcome, "failed")
        self.assertIn("cannot prepare the relay", result.detail)

    def test_unapproved_mac_token_alert_never_reaches_the_phone_sender(self) -> None:
        message = send.Message("user", "quota_alert", "Fixture", "", None)
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        with (patch.dict(os.environ, {"NOTIFY_STATE_DIR": str(root / "notify")}),
              patch.object(push_texts, "REVIEWED_FILE", root / "approvals.json"),
              patch.object(send, "run", return_value=(0, "", "")) as deliver):
            for text_id in token_expiry.ALERT_IDS:
                with self.subTest(text_id=text_id):
                    result = send.user(message, "note", ActionRequired("Check Accounts"), 10, text_id=text_id)
                    self.assertEqual(result.outcome, "refused")
            deliver.assert_not_called()

    def relay_fixture(self, env: dict[str, str] | None = None) -> send.Message:
        home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        binaries = home / "bin"
        notes = home / "agents"
        binaries.mkdir()
        notes.mkdir()
        gpg = binaries / "gpg"
        _ = gpg.write_text("#!/bin/sh\nprintf '%s\\n' fixture\n", encoding="utf-8")
        gpg.chmod(0o700)
        token_file = home / ".config/claude/oauth-token.gpg"
        token_file.parent.mkdir(parents=True)
        token_file.touch()
        made = datetime(2026, 10, 1, 19, 23, tzinfo=timezone.utc)
        os.utime(token_file, (made.timestamp(), made.timestamp()))
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"HOME": str(home), "AGENT_NOTES_DIR": str(notes),
            "CLAUDE_PYTHON": sys.executable, "PYTHONDONTWRITEBYTECODE": "1",
            "PATH": str(binaries) + os.pathsep + os.environ["PATH"], **(env or {})}, clear=True)))
        _ = self.enterContext(patch.object(mac_fallback_token, "on_mac", return_value=True))
        _ = self.enterContext(patch.object(accounts, "account_for_unscoped_launch",
                              return_value=accounts.NamedAccount("claude2", home / ".claude2")))
        _ = self.enterContext(patch.object(models_registry, "claude_model", return_value="fixture"))
        _ = self.enterContext(patch.object(send, "claude_binary", return_value=str(binaries / "claude")))
        _ = self.enterContext(patch.object(send, "STATE", home / "message"))
        attempt = send.ClaudeDeliveryAttempt(send.RelayProcessRecipient("mac", 1, "fixture"), 1, "fixture", "/fixture")
        return send.Message("uds:/fixture", "test", "Fixture", "Fixture relay", None, claude_attempt=attempt)

    def check_relay(self, *, authentication_failed: bool, removed_variables: tuple[str, ...] = (),
                    confirmed_delivery: bool = False) -> None:
        message = self.relay_fixture(dict.fromkeys(removed_variables, "fixture"))
        executable = Path.home() / "bin/claude"
        checks = [
            '[ "${CLAUDE_CODE_OAUTH_TOKEN+x}" = x ]',
            '[ "$CLAUDE_CONFIG_DIR" = "$HOME/.claude2" ]',
            '[ "$CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST" = 1 ]',
            *(f'[ "${{{name}+x}}" != x ]' for name in removed_variables),
        ]
        _ = executable.write_text("#!/bin/sh\nset -e\n" + "\n".join(checks) + "\n", encoding="utf-8")
        executable.chmod(0o700)
        made = datetime(2026, 10, 1, 19, 23, tzinfo=timezone.utc)
        if authentication_failed:
            event = {"type": "assistant", "error": "authentication_failed"}
        else:
            event = {"type": "assistant", "message": {"content": [{"type": "text", "text": "fixture response"}]}}

        def transport(target: send.Message, timeout: float, command: list[str], environment: dict[str, str],
                      _attempt: send.ClaudeDeliveryAttempt) -> send.RelayTransportResponse:
            code, _, _ = send.run(command, "", timeout, environment, Path.home())
            self.assertEqual(code, 0)
            if confirmed_delivery:
                payload = send.agent_text(target, via_relay=True)
                tool = {"type": "tool_use", "name": "SendMessage", "id": "call",
                        "input": {"to": target.to, "message": payload}}
                answer = {"type": "tool_result", "tool_use_id": "call", "content": '{"success":true}'}
                out = "\n".join((json.dumps({"type": "assistant", "message": {"content": [tool]}}),
                                 json.dumps({"type": "user", "message": {"content": [answer]}}), json.dumps(event)))
                return send.RelayTransportResponse(1, out, "", target.to, send.RelayRequestForwarded("call", payload))
            return send.RelayTransportResponse(int(authentication_failed), json.dumps(event), "", target.to,
                                               send.RelayRequestNotForwarded())

        with patch.object(send, "_bound_relay", side_effect=transport):
            result = send.relay(message, 10)
            token = mac_fallback_token.read()

        if confirmed_delivery:
            self.assertEqual(result.outcome, "sent")
        self.assertIsInstance(token, mac_fallback_token.MacFallbackToken)
        assert isinstance(token, mac_fallback_token.MacFallbackToken)
        self.assertEqual(token.made, made)
        self.assertIsInstance(token.last_login_failure if authentication_failed else token.last_good_use, datetime)
        self.assertFalse((Path.home() / "message" / "relay").exists())



@final
class MacTokenRenewalTests(unittest.TestCase):
    SECRET = b"private-renewal-fixture"
    CIPHERTEXT = b"encrypted-fixture"
    RECIPIENT = "aB01" * 10
    TERMINAL_CLEAR = b"\033[?2004l\n\033[3J\033[H\033[2J\033]1337;ClearScrollback\a"

    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.config = Path()
        self.path = Path()
        self.stdin = io.StringIO()
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()
        self.events: list[str] = []
        self.terminal_writes: list[bytes] = []
        self.setup_status = self.gpg_status = self.clipboard_status = 0
        self.paste = b"\033[200~ \t" + self.SECRET + b" \033[201~\n"
        self.interrupt_prompt = self.interrupt_setup = False

    @override
    def setUp(self) -> None:
        home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        notes = home / "agents"
        notes.mkdir()
        config = home / "config"
        config.mkdir()
        self.config = config / "showrunners.conf"
        _ = self.config.write_text(f"mac_token.account=claude2\nmac_token.gpg_recipient={self.RECIPIENT}\n")
        _ = cast(object, self.enterContext(patch.dict(os.environ, {
            "HOME": str(home), "CONF_DIR": str(config), "AGENT_NOTES_DIR": str(notes),
            "USER_ZONE": "America/New_York", "TZ": "UTC", "TERM_PROGRAM": "Apple_Terminal",
        }, clear=True)))
        self.path = mac_fallback_token.encrypted_path()
        self.path.parent.mkdir(parents=True)
        _ = self.path.write_bytes(b"old ciphertext")
        _ = self.enterContext(patch.object(sys, "argv", ["mac_fallback_token.py", "renew"]))
        _ = self.enterContext(patch.object(sys, "stdin", self.stdin))
        _ = self.enterContext(patch.object(sys, "stdout", self.stdout))
        _ = self.enterContext(patch.object(self.stdin, "isatty", return_value=True))
        _ = self.enterContext(patch.object(self.stdout, "isatty", return_value=True))
        _ = self.enterContext(patch.object(sys, "stderr", self.stderr))
        _ = self.enterContext(patch.object(mac_fallback_token, "on_mac", return_value=True))
        _ = self.enterContext(patch.object(mac_fallback_token, "open", side_effect=self.open_tty, create=True))
        _ = self.enterContext(patch.object(termios, "tcgetattr", return_value=[0, 0, 0, 0, 0, 0, []]))
        _ = self.enterContext(patch.object(termios, "tcsetattr"))
        _ = self.enterContext(patch.object(subprocess, "run", side_effect=self.run_command))
        publish = mac_fallback_token.publish_metadata

        def publish_after_replacement() -> None:
            self.events.append("publish")
            self.assertEqual(self.path.read_bytes(), self.CIPHERTEXT)
            self.assertEqual(self.stdout.getvalue(), "")
            publish()

        _ = self.enterContext(patch.object(mac_fallback_token, "publish_metadata", side_effect=publish_after_replacement))

    def open_tty(self, path: str, mode: str, *, buffering: int) -> io.BytesIO:
        self.assertEqual((path, mode, buffering), ("/dev/tty", "r+b", 0))
        fixture = self

        class Terminal(io.BytesIO):
            @override
            def write(self, buffer: Buffer, /) -> int:
                data = bytes(buffer)
                fixture.terminal_writes.append(data)
                return len(data)

            @override
            def readline(self, size: int | None = -1, /) -> bytes:
                if self.tell() == 0:
                    fixture.events.append("prompt")
                if fixture.interrupt_prompt:
                    raise KeyboardInterrupt
                return super().readline(size)

        return Terminal(self.paste)

    def run_command(self, argv: list[str], *, check: bool, input: bytes | None = None, umask: int = -1,
                    stdout: int | None = None, stderr: int | None = None,
                    env: dict[str, str] | None = None) -> subprocess.CompletedProcess[bytes]:
        self.assertFalse(check)
        self.assert_secret_absent(*argv)
        for key, value in (os.environ if env is None else env).items():
            self.assert_secret_absent(key, value)
        if argv[0] == "gpg":
            self.events.append("encrypt")
            self.assertEqual(argv[:-1], [
                "gpg", "--batch", "--yes", "--encrypt", "--recipient", self.RECIPIENT, "--output",
            ])
            self.assertTrue(input == self.SECRET, "GPG must receive the fixture on stdin")
            self.assertEqual(umask, 0o077)
            self.assertEqual((stdout, stderr), (subprocess.DEVNULL, subprocess.DEVNULL))
            temporary = Path(argv[-1])
            self.assertEqual(temporary.parent, self.path.parent)
            self.assertNotEqual(temporary, self.path)
            self.assertEqual(temporary.stat().st_mode & 0o777, 0o600)
            self.assertEqual(self.path.read_bytes(), b"old ciphertext")
            _ = temporary.write_bytes(self.CIPHERTEXT)
            made = datetime(2026, 11, 21, 1, tzinfo=timezone.utc).timestamp()
            os.utime(temporary, (made, made))
            return subprocess.CompletedProcess(argv, self.gpg_status)
        if argv == ["pbcopy"]:
            self.events.append("clipboard")
            self.assertEqual(self.terminal_writes[-1:], [self.TERMINAL_CLEAR])
            self.assertEqual(input, b"")
            self.assertEqual((stdout, stderr), (subprocess.DEVNULL, subprocess.DEVNULL))
            self.assertEqual(list(self.path.parent.iterdir()), [self.path])
            return subprocess.CompletedProcess(argv, self.clipboard_status)
        self.assertEqual((input, stdout, stderr), (None, None, None))
        scripts = Path(mac_fallback_token.__file__).resolve().parents[1]
        self.assertEqual(argv, [
            str(scripts / "lib/py"), str(scripts / "accounts/accounts.py"), "exec", "claude2", "setup-token",
        ])
        self.events.append("setup")
        if self.interrupt_setup:
            raise KeyboardInterrupt
        return subprocess.CompletedProcess(argv, self.setup_status)

    def assert_secret_absent(self, *values: str) -> None:
        for value in values:
            self.assertFalse(self.SECRET in value.encode(), "The token appeared outside GPG stdin")

    def assert_original_file(self) -> None:
        self.assertEqual(self.path.read_bytes(), b"old ciphertext")
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_renews_in_apple_terminal_and_keeps_the_token_on_gpg_stdin(self) -> None:
        self.assertEqual(mac_fallback_token.main(), 0)
        self.assertEqual(self.events, ["setup", "prompt", "encrypt", "clipboard", "publish"])
        stored = mac_fallback_token.read()
        assert isinstance(stored, mac_fallback_token.MacFallbackToken)
        self.assertEqual(stored.made, mac_fallback_token.made_at(self.path))
        output = self.stdout.getvalue()
        errors = self.stderr.getvalue()
        self.assert_secret_absent(output, errors)
        self.assertEqual(output, "claude2 expires 2027-11-20.\n")
        self.assertEqual(errors, "")
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_renews_in_iterm(self) -> None:
        with patch.dict(os.environ, {"TERM_PROGRAM": "iTerm.app"}):
            self.assertEqual(mac_fallback_token.main(), 0)
        self.assertEqual(self.events, ["setup", "prompt", "encrypt", "clipboard", "publish"])
        self.assertEqual(self.stderr.getvalue(), "")

    def test_refuses_off_the_mac_before_reading_config(self) -> None:
        with (patch.object(mac_fallback_token, "on_mac", return_value=False),
              patch.object(conf, "read", side_effect=AssertionError("unexpected config read"))):
            self.assertEqual(mac_fallback_token.main(), 1)
        self.assertEqual(self.events, [])
        self.assertEqual(self.stderr.getvalue(), "Run this command in a Mac terminal.\n")
        self.assert_original_file()

    def test_refuses_when_the_account_is_unset(self) -> None:
        _ = self.config.write_text("mac_token.account=\n")
        self.assertEqual(mac_fallback_token.main(), 1)
        self.assertEqual(self.events, [])
        self.assertEqual(self.stderr.getvalue(), "Set mac_token.account to a numbered Claude account first.\n")
        self.assert_original_file()

    def test_requires_terminal_input_and_output(self) -> None:
        for stream in (self.stdin, self.stdout):
            with self.subTest(stream=stream), patch.object(stream, "isatty", return_value=False):
                _ = self.stderr.seek(0)
                _ = self.stderr.truncate()
                self.assertEqual(mac_fallback_token.main(), 1)
                self.assertEqual(self.stderr.getvalue(), "Run mac-token-renew yourself in a Mac terminal.\n")
                self.assertEqual(self.events, [])
        self.assert_original_file()

    def test_refuses_inside_claude_code(self) -> None:
        with patch.dict(os.environ, {"CLAUDECODE": ""}):
            self.assertEqual(mac_fallback_token.main(), 1)
        self.assertEqual(self.stderr.getvalue(), "Run mac-token-renew yourself in a Mac terminal.\n")
        self.assertEqual(self.events, [])
        self.assert_original_file()

    def assert_refuses_terminal(self) -> None:
        _ = self.stderr.seek(0)
        _ = self.stderr.truncate()
        self.assertEqual(mac_fallback_token.main(), 1)
        self.assertEqual(self.stderr.getvalue(),
                         "Run mac-token-renew in a plain Terminal or iTerm window, not in a herdr pane.\n")
        self.assertEqual(self.events, [])
        self.assert_original_file()

    def test_refuses_ssh_tmux_and_herdr(self) -> None:
        for variable in ("SSH_CONNECTION", "TMUX", "HERDR_ENV"):
            with self.subTest(variable=variable), patch.dict(os.environ, {variable: ""}):
                self.assert_refuses_terminal()

    def test_refuses_unknown_or_missing_terminal_program(self) -> None:
        with patch.dict(os.environ, {"TERM_PROGRAM": "unknown"}):
            self.assert_refuses_terminal()
            del os.environ["TERM_PROGRAM"]
            self.assert_refuses_terminal()

    def test_refuses_without_a_valid_gpg_recipient(self) -> None:
        for setting in ("", "mac_token.gpg_recipient=\n", "mac_token.gpg_recipient=0123\n",
                        "mac_token.gpg_recipient=" + "z" * 40 + "\n"):
            with self.subTest(setting=setting):
                _ = self.config.write_text("mac_token.account=claude2\n" + setting)
                _ = self.stderr.seek(0)
                _ = self.stderr.truncate()
                self.assertEqual(mac_fallback_token.main(), 1)
                self.assertEqual(self.stderr.getvalue(),
                                 "Set mac_token.gpg_recipient to a 40-digit hexadecimal fingerprint.\n")
                self.assertEqual(self.events, [])
        self.assert_original_file()

    def test_setup_failure_clears_before_the_prompt(self) -> None:
        self.setup_status = 1
        self.assertEqual(mac_fallback_token.main(), 1)
        self.assertEqual(self.events, ["setup", "clipboard"])
        self.assertEqual(self.stderr.getvalue(), "claude2 setup-token failed.\n")
        self.assert_original_file()

    def test_interrupt_during_setup_clears_before_the_prompt(self) -> None:
        self.interrupt_setup = True
        self.assertEqual(mac_fallback_token.main(), 130)
        self.assertEqual(self.events, ["setup", "clipboard"])
        self.assertEqual(self.stderr.getvalue(), "Renewal interrupted.\n")
        self.assert_original_file()

    def test_terminal_read_error_after_setup_clears_the_terminal_and_clipboard(self) -> None:
        with patch.object(termios, "tcgetattr", side_effect=termios.error):
            self.assertEqual(mac_fallback_token.main(), 1)
        self.assertEqual(self.events, ["setup", "clipboard"])
        self.assertEqual(self.stderr.getvalue(), "Cannot read or store the new Mac fallback token.\n")
        self.assert_original_file()

    def test_empty_prompt_clears_the_clipboard_without_encryption(self) -> None:
        self.paste = b"\033[200~ \t\033[201~\n"
        self.assertEqual(mac_fallback_token.main(), 1)
        self.assertEqual(self.events, ["setup", "prompt", "clipboard"])
        self.assertEqual(self.stderr.getvalue(), "No token entered.\n")
        self.assert_original_file()

    def test_whitespace_inside_the_paste_refuses_without_encryption(self) -> None:
        for gap in (b" ", b"\t", "\u00a0".encode()):
            with self.subTest(gap=gap):
                self.events.clear()
                _ = self.stderr.seek(0)
                _ = self.stderr.truncate()
                self.paste = b"\033[200~part1" + gap + b"part2\033[201~\n"
                self.assertEqual(mac_fallback_token.main(), 1)
                self.assertEqual(self.events, ["setup", "prompt", "clipboard"])
                self.assertEqual(self.stderr.getvalue(), "The token contains whitespace.\n")
                self.assert_original_file()

    def test_multiline_paste_reaches_gpg_as_one_line(self) -> None:
        split = len(self.SECRET) // 2
        self.paste = b"\033[200~" + self.SECRET[:split] + b"\r\n" + self.SECRET[split:] + b"\033[201~\n"
        self.assertEqual(mac_fallback_token.main(), 0)
        self.assertEqual(self.events, ["setup", "prompt", "encrypt", "clipboard", "publish"])
        self.assertEqual(self.path.read_bytes(), self.CIPHERTEXT)
        self.assertEqual(self.stderr.getvalue(), "")

    def test_unbracketed_input_refuses_without_encryption(self) -> None:
        self.paste = b"fixture-line\nsecond-line\n"
        self.assertEqual(mac_fallback_token.main(), 1)
        self.assertEqual(self.events, ["setup", "prompt", "clipboard"])
        self.assertEqual(self.stderr.getvalue(), "Paste the token. Do not type it.\n")
        self.assert_original_file()

    def test_gpg_failure_removes_the_temporary_file_and_clears_the_clipboard(self) -> None:
        self.gpg_status = 1
        self.assertEqual(mac_fallback_token.main(), 1)
        self.assertEqual(self.events, ["setup", "prompt", "encrypt", "clipboard"])
        self.assertEqual(self.stderr.getvalue(), "GPG encryption failed.\n")
        self.assert_original_file()

    def test_interrupt_at_the_prompt_clears_the_clipboard(self) -> None:
        self.interrupt_prompt = True
        self.assertEqual(mac_fallback_token.main(), 130)
        self.assertEqual(self.events, ["setup", "prompt", "clipboard"])
        self.assertEqual(self.stderr.getvalue(), "Renewal interrupted.\n")
        self.assert_original_file()

    def test_clipboard_failure_exits_nonzero_and_publishes_the_new_date(self) -> None:
        self.clipboard_status = 1
        self.assertEqual(mac_fallback_token.main(), 1)
        self.assertEqual(self.events, ["setup", "prompt", "encrypt", "clipboard", "publish"])
        self.assertEqual(self.stdout.getvalue(), "")
        self.assertEqual(self.stderr.getvalue(), "Could not clear the Mac clipboard. Replaced the encrypted file.\n")

    def test_clipboard_failure_prints_one_line_after_encryption_fails(self) -> None:
        self.clipboard_status = self.gpg_status = 1
        self.assertEqual(mac_fallback_token.main(), 1)
        self.assertEqual(self.events, ["setup", "prompt", "encrypt", "clipboard"])
        self.assertEqual(self.stdout.getvalue(), "")
        self.assertEqual(self.stderr.getvalue(), "Could not clear the Mac clipboard. The encrypted file did not change.\n")
        self.assert_original_file()


class MacTokenPromptTests(unittest.TestCase):
    def test_prompt_disables_echo_and_restores_the_tty(self) -> None:
        writes: list[bytes] = []

        class Terminal(io.BytesIO):
            @override
            def write(self, buffer: Buffer, /) -> int:
                data = bytes(buffer)
                writes.append(data)
                return len(data)

        original: list[int | list[int]] = [0, 0, 0, termios.ECHO | termios.ECHONL, 0, 0, []]
        hidden: list[int | list[int]] = [0, 0, 0, 0, 0, 0, []]
        terminal = Terminal(b"\033[200~private-prompt-fixture\n\033[201~\n")
        with (patch.object(termios, "tcgetattr", return_value=original),
              patch.object(termios, "tcsetattr") as terminal_settings):
            value = mac_fallback_token._read_new_token(terminal)  # pyright: ignore[reportPrivateUsage]
        self.assertTrue(value == b"private-prompt-fixture")
        self.assertEqual(terminal_settings.call_count, 2)
        terminal_settings.assert_any_call(terminal, termios.TCSAFLUSH, hidden)
        terminal_settings.assert_called_with(terminal, termios.TCSAFLUSH, original)
        self.assertEqual(writes, [b"\033[?2004hPaste the new token, then press Enter: "])
