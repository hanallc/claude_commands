from __future__ import annotations

import json
import os
import runpy
import shlex
import sys
import tempfile
import unittest
from collections.abc import Callable
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from typing import cast, override
from unittest import mock

from scripts.agents import remote
from scripts.hooks import compaction_marker, conversation_pause, mac_files
from scripts.lib import unit_runs
from scripts.message import escalate, review_hold


class MacFilesTests(unittest.TestCase):
    root: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        _ = (self.root / "production.conf").write_text("mac_run_host=fixture@mac\n", encoding="utf-8")
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {"CONF_DIR": str(self.root)})))
        _ = self.enterContext(mock.patch.object(sys, "platform", "linux"))
        _ = self.enterContext(mock.patch.object(remote, "local_machine", return_value="natedev"))

    def test_finds_quoted_and_unquoted_paths_in_text(self) -> None:
        screenshot = ("/Users/fixture/Library/Application Support/CleanShot/media/media_fixture/"
                      + "CleanShot 2026-11-20 at 10.41.31@2x.png")
        for prompt, expected in (
            (f'Read "{screenshot}".', (screenshot,)),
            (f"Read {screenshot}.", (screenshot,)),
            ("Read /Users/fixture/project/Users/archive/shot.png.",
             ("/Users/fixture/project/Users/archive/shot.png",)),
            ("/Users/fixture/folder, or use fallback.png", ()),
            ("/Users/fixture/folder; or use fallback.png", ()),
            ("/Users/fixture/folder — see fallback.png", ()),
            ("/Users/fixture/folder – see fallback.png", ()),
            (r"Open /Users/fixture/CleanShot\ 2026-11-20\ at\ 10.41.31@2x.png.",
             ("/Users/fixture/CleanShot 2026-11-20 at 10.41.31@2x.png",)),
            (r"Open /Users/fixture/Shot\\name.png.", (r"/Users/fixture/Shot\name.png",)),
            (r"Open /Users/fixture/Shot\\\ name.png.", (r"/Users/fixture/Shot\ name.png",)),
            ("/Users/fixture/folder. Then open fallback.png", ()),
            ("/Users/fixture/folder: use /Users/fixture/actual.png", ("/Users/fixture/actual.png",)),
            ("Open '/Users/fixture/project.v1/Report Draft' please.",
             ("/Users/fixture/project.v1/Report Draft",)),
            ("See (/Users/fixture/Folder.v1/Screen Shot.png), then /Users/fixture/log.txt.",
             ("/Users/fixture/Folder.v1/Screen Shot.png", "/Users/fixture/log.txt")),
            ("See `/Users/fixture/Screen Shot.png` or /Users/fixture/Screen Shot.png.",
             ("/Users/fixture/Screen Shot.png",)),
            ("No screenshot or file from /home/fixture/shot.png.", ()),
            ("Ignore /mnt/Users/fixture/shot.png and see /Users/fixture/actual.png in the draft.",
             ("/Users/fixture/actual.png",)),
        ):
            with self.subTest(prompt=prompt):
                self.assertEqual(mac_files.find_paths(prompt), expected)

    def test_gives_one_command_per_path_in_prompt_order(self) -> None:
        first = "/Users/fixture/First/Shot.png"
        second = "/Users/fixture/Second/Shot.png"
        context = mac_files.context(f"Read {first}, {second}, or {first}.")
        self.assertEqual(context.splitlines(), [
            f"The Mac file {path} is on the Mac. Copy it into your scratch folder with: "
            + f"scp 'fixture@mac:{path}' \"<your scratch folder>/\" and then read the copy."
            for path in (first, second)
        ])

    def test_the_command_escapes_shell_quotes_and_sftp_patterns(self) -> None:
        path = r"/Users/fixture/Nate's [draft]*?.png"
        context = mac_files.context(f'Open "{path}".')
        command = context.removesuffix(" and then read the copy.")
        self.assertEqual(shlex.split(command.split(" with: ", 1)[1]), [
            "scp", r"fixture@mac:/Users/fixture/Nate's \[draft\]\*\?.png", "<your scratch folder>/",
        ])

    def test_skips_the_mac_and_other_linux_hosts(self) -> None:
        for platform, machine in (("darwin", "natedev"), ("linux", "other-linux")):
            with (
                self.subTest(platform=platform, machine=machine),
                mock.patch.object(sys, "platform", platform),
                mock.patch.object(remote, "local_machine", return_value=machine),
            ):
                self.assertEqual(mac_files.context("Open /Users/fixture/shot.png."), "")

    def run_hook(
        self, source: conversation_pause.PromptSource, *,
        prompt: str = "Open /Users/fixture/shot.png.",
        reply: conversation_pause.HookReply = conversation_pause.NoReply.NOTHING,
        record_prompt: Callable[[Callable[[str], None]], None] = lambda _emit: None,
        log_event: Callable[[str], None] = lambda _event: None,
    ) -> str:
        hook = Path(__file__).with_name("user-prompt-submit-conversation-pause.py")
        main = cast(Callable[[], None], runpy.run_path(str(hook))["main"])
        output = StringIO()
        stdin = StringIO(json.dumps({"session_id": "fixture", "prompt": prompt}))

        def timestamp() -> float:
            log_event("timestamp")
            return 1

        def pause(*_arguments: object, **_options: object) -> conversation_pause.HookReply:
            log_event("pause")
            return reply

        def record(_session: str, _at: float, emit: Callable[[str], None], **_options: object) -> None:
            log_event("record")
            record_prompt(emit)

        with (
            mock.patch.object(sys, "stdin", stdin),
            redirect_stdout(output),
            mock.patch.object(compaction_marker, "clear"),
            mock.patch.object(conversation_pause, "prompt_source", return_value=source),
            mock.patch.object(conversation_pause, "now_epoch", new=timestamp),
            mock.patch.object(conversation_pause, "keep_native_review_prompt", return_value=False),
            mock.patch.object(conversation_pause, "consume_scheduled_prompt"),
            mock.patch.object(conversation_pause, "session_staff"),
            mock.patch.object(conversation_pause, "is_showrunner_or_unit_director", return_value=False),
            mock.patch.object(unit_runs, "open_run", return_value=None),
            mock.patch.object(escalate, "typed"),
            mock.patch.object(review_hold, "record_user_prompt", new=record),
            mock.patch.object(conversation_pause, "message_arrived", new=pause),
        ):
            main()
        return output.getvalue()

    def test_prompt_without_a_mac_path_does_not_read_the_route(self) -> None:
        with mock.patch.object(Path, "read_text", side_effect=AssertionError("unexpected route read")):
            self.assertEqual(self.run_hook(
                conversation_pause.PromptSource.TYPED,
                prompt="The /Users/fixture/folder — see fallback.png",
            ), "")

    def test_route_read_follows_the_timestamp_and_pause(self) -> None:
        events: list[str] = []

        def read_route(path: Path, *, encoding: str) -> str:
            self.assertEqual(path, self.root / "production.conf")
            self.assertEqual(encoding, "utf-8")
            events.append("route")
            return "mac_run_host=fixture@mac\n"

        with mock.patch.object(Path, "read_text", new=read_route):
            self.assertIn("scp ", self.run_hook(
                conversation_pause.PromptSource.TYPED, log_event=events.append,
            ))
        self.assertEqual(events, ["timestamp", "pause", "route", "record"])

    def test_typed_prompt_emits_file_context_with_or_without_a_pause_reply(self) -> None:
        line = ("The Mac file /Users/fixture/shot.png is on the Mac. Copy it into your scratch folder with: "
                + "scp 'fixture@mac:/Users/fixture/shot.png' \"<your scratch folder>/\" and then read the copy.")
        for reply, context in (
            (conversation_pause.NoReply.NOTHING, line),
            (conversation_pause.ContextOnly("Reports paused."), "Reports paused.\n\n" + line),
        ):
            with self.subTest(reply=reply):
                self.assertEqual(self.run_hook(conversation_pause.PromptSource.TYPED, reply=reply), json.dumps({
                    "hookSpecificOutput": {
                        "hookEventName": "UserPromptSubmit",
                        "additionalContext": context,
                    },
                }) + "\n")

    def test_emit_uses_the_prepared_context_without_reading_config(self) -> None:
        mac_context = mac_files.context("Open /Users/fixture/shot.png.")

        def record(emit: Callable[[str], None]) -> None:
            with mock.patch.object(Path, "read_text", side_effect=AssertionError("unexpected read during emit")):
                emit("Held message.")

        self.assertEqual(self.run_hook(
            conversation_pause.PromptSource.TYPED,
            reply=conversation_pause.ContextOnly("Reports paused."),
            record_prompt=record,
        ), json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": f"Reports paused.\n\nHeld message.\n\n{mac_context}",
            },
        }) + "\n")

    def test_other_prompt_sources_do_not_parse_mac_paths(self) -> None:
        for source in conversation_pause.PromptSource:
            if source is conversation_pause.PromptSource.TYPED:
                continue
            with (
                self.subTest(source=source),
                mock.patch.object(mac_files, "find_paths", side_effect=AssertionError("unexpected parse")),
            ):
                self.assertEqual(self.run_hook(source), "")

    def test_missing_route_keeps_the_pause_reply(self) -> None:
        (self.root / "production.conf").unlink()
        self.assertEqual(self.run_hook(
            conversation_pause.PromptSource.TYPED, reply=conversation_pause.ContextOnly("Reports paused."),
        ), json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": "Reports paused.",
            },
        }) + "\n")
