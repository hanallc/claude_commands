"""Test page publication, HTTP forms, verdict calls and checkpoint routing."""
from __future__ import annotations

from scripts.console_text import check as console_text
from scripts.notify import review
from scripts.production.change_review_test_support import fixture_image, fixture_row_image, pictured_changes, registered_fixture_image

import fcntl
import hashlib
import html
import os
import re
import json
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, redirect_stderr
from dataclasses import dataclass, replace
from datetime import date, datetime
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from html.parser import HTMLParser
from inspect import signature
from io import StringIO
from functools import cache
from pathlib import Path
from types import ModuleType
from typing import Literal, TextIO, cast, override
from collections.abc import Callable, Generator
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit
from uuid import uuid4

from scripts.review_links.browser_test_support import assert_status_states, firefox_probe_results
from scripts.review_links.test_staff_names import without_role_colors
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.lib import atomic_file, conf, scratch, urgency
from scripts.lib.process_group import run_process_group
from scripts.message import send_result
from scripts.lint.memory_test_support import write_config_fixture
from scripts.lib.py_floor_test_support import seed_interpreter_cache
from scripts.production import change_routing, change_review, footer_counts, next_item, record_store, showrunners, staff, unrouted_alert, update_registration
from scripts.production.cli_fixture import invoke_cli, process_boundary, process_boundary_for_case
from scripts.production.staff_test_support import fixture_member, fixture_runtime_id, production_staff_record
from scripts.feature_log import feature_log
from scripts.review_links import change_browser, change_page, serve, image_store, page_frame, review_events, review_link
from scripts.review_links import new_item_dialog, question_record, reply_tokens, review_actions, review_items, review_submissions
from scripts.review_links.server_test_support import started_review_server
from scripts.rebuild import need
from scripts.statusline import rebuild_row
from scripts.agents import remote
from scripts.agents.remote import local_machine
from scripts.production.production_doc import read_production


@dataclass(frozen=True)
class Reply:
    status: int
    body: str
    location: str


class SavedAnswerForm(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.fields: dict[str, str] = {}
        self.validation_requests: dict[str, dict[str, str]] = {}
        self.feedback: dict[str, str] = {}
        self.selected_outcomes: dict[str, str] = {}
        self.textarea_row: str = ""
        self.textarea_text: str = ""

    @override
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "form":
            self.fields = {}
        attributes = dict(attrs)
        name, value = attributes.get("name"), attributes.get("value")
        if tag in ("input", "button") and name is not None and value is not None:
            self.fields[name] = value
        if tag == "textarea" and name == "feedback":
            self.textarea_row = self.fields["row"]
            self.textarea_text = ""
        if tag == "button" and attributes.get("aria-pressed") == "true" and value is not None:
            self.selected_outcomes[self.fields["row"]] = value
        if tag == "button" and value == "validate":
            self.validation_requests[self.fields["row"]] = dict(self.fields)

    @override
    def handle_data(self, data: str) -> None:
        if self.textarea_row:
            self.textarea_text += data

    @override
    def handle_endtag(self, tag: str) -> None:
        if tag == "textarea" and self.textarea_row:
            # HTML removes the first newline after a textarea's opening tag.
            self.feedback[self.textarea_row] = self.textarea_text.removeprefix("\n")
            self.textarea_row = ""


class BrowserForms(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.rows: dict[str, dict[str, str]] = {}
        self.tabs: dict[str, str] = {}
        self.save_urls: dict[str, str] = {}
        self.counts: dict[str, int] = {}
        self.question_counts: dict[str, int] = {}
        self.kind: str = "changes"
        self.pending: dict[str, bool] = {}
        self.outcomes: dict[str, list[str]] = {}
        self.current: str = ""
        self.orders: dict[str, list[str]] = {}
        self.reading_orders: bool = False

    @override
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "script" and "data-change-orders" in values:
            self.reading_orders = True
        if tag == "div" and values.get("data-kind"):
            self.kind = values["data-kind"] or ""
        if tag == "section" and values.get("data-key"):
            self.current = values["data-key"] or ""
            self.rows[self.current] = {}
            self.tabs[self.current] = values.get("data-tab") or ""
            self.pending[self.current] = values.get("data-pending") == "true"
            self.outcomes[self.current] = []
        if tag in ("input", "textarea") and self.current and values.get("name"):
            self.rows[self.current][values["name"] or ""] = values.get("value") or ""
        if tag == "form" and self.current and values.get("method") == "post":
            self.save_urls[self.current] = values.get("action") or ""
        if tag == "button" and self.current and values.get("name") == "outcome":
            self.outcomes[self.current].append(values.get("value") or "")
        if tag == "option" and values.get("data-showrunner"):
            counts = self.question_counts if self.kind == "questions" else self.counts
            counts[values["data-showrunner"] or ""] = int(values.get("data-count") or "0")

    @override
    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self.reading_orders = False
        if tag == "section":
            self.current = ""

    @override
    def handle_data(self, data: str) -> None:
        if self.reading_orders:
            self.orders = cast(dict[str, list[str]], json.loads(data))


SEND_MESSAGE = change_page.send_message


# The Memory bar record predates structured question parts.
MEMORY_BAR_ASK = "Memory bar on the Builds page: what name should the new block have?"
MEMORY_BAR_REVIEWED = MEMORY_BAR_ASK + """

The cap mark is the memory limit for builds that the build queue runs. The block holds:
- sccache, the compiler cache: 2.2 GiB now, nearly all of the block.
- rust-analyzer, the editor's Rust checker: 0 GiB now.
- Other build programs: 0.1 GiB now.

The resources unit named the block 'Build tools'. Nobody approved that name.

'Compiler cache' (recommended): the block shows this name. It fits because nearly all of the block is sccache.

'Build tools': the block keeps the name it has now.

Your own name: the resources unit puts your name on the block."""
MEMORY_BAR_RAW = (MEMORY_BAR_ASK + " The cap mark is the memory limit for builds that the build queue runs. "
    + "The resources unit named the block 'Build tools'. Nobody approved that name. "
    + "Choices: 'Compiler cache': nearly all of the block is sccache. 'Build tools': keep the current name. "
    + "Your own name: the resources unit puts your name on the block. Recommended: 'Compiler cache', because it names the contents.")


@cache
def repository_seed() -> Path:
    """Copy private Git files from one merged fixture repository."""
    temporary = tempfile.TemporaryDirectory(prefix="change-page-seed-")
    unittest.addModuleCleanup(repository_seed.cache_clear)
    unittest.addModuleCleanup(temporary.cleanup)
    repo = Path(temporary.name)
    item = change_review.Visible("Numbered moves", "Run moves list", "Each group starts at 1")
    commands = [
        ["init", "-b", "main"],
        ["config", "user.name", "Review test"],
        ["config", "user.email", "review@example.invalid"],
        ["commit", "--allow-empty", "-m", "initial"],
        ["checkout", "-b", "feature"],
        ["commit", "--allow-empty", "-m",
         f"checkpoint(feature): numbered moves\n\nChange-Review: {item.change} | {item.check} | {item.see} | image 012345abcdef"],
        ["checkout", "main"],
        ["merge", "--no-ff", "feature", "-m", "merge numbered moves"],
    ]
    for arguments in commands:
        _ = subprocess.run(["git", "-C", str(repo), *arguments], capture_output=True, text=True, check=True)
    return repo


class ReviewTextTests(unittest.TestCase):
    def test_button_borders_keep_three_to_one_contrast_in_both_schemes(self) -> None:
        body = change_page.document("").decode()
        colors = re.search(r'background:light-dark\((#[0-9a-f]{6}),(#[0-9a-f]{6})\);color:CanvasText;'
                           + r'border:1px solid light-dark\((#[0-9a-f]{6}),(#[0-9a-f]{6})\)', body)
        assert colors is not None

        def luminance(color: str) -> float:
            channels = [int(color[index:index + 2], 16) / 255 for index in (1, 3, 5)]
            linear = [channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4
                      for channel in channels]
            return sum(channel * weight for channel, weight in zip(linear, (0.2126, 0.7152, 0.0722)))

        for fill, border, background in ((colors[1], colors[3], "#ffffff"), (colors[2], colors[4], "#1c1b22")):
            for adjacent in (fill, background):
                with self.subTest(fill=fill, border=border, adjacent=adjacent):
                    high, low = sorted((luminance(border), luminance(adjacent)), reverse=True)
                    self.assertGreaterEqual((high + 0.05) / (low + 0.05), 3)

    def test_text_without_list_lines_keeps_all_whitespace_in_one_block(self) -> None:
        text = '\n First <line> \r\n\r Second & line\u2028Third\n'
        self.assertEqual(change_page.review_text_html(text),
                         '<p>\n First &lt;line&gt; \r\n\r Second &amp; line\u2028Third\n</p>')

    def test_question_and_answer_show_list_runs_and_text_blocks(self) -> None:
        question = change_page.AnsweredQuestion("q1", "\n  Steps\n continue\n\n  - First\n- Second\n1. Third\n2. Fourth\n\n Done\n", "- Yes\n- No")
        self.assertEqual(change_page.question_html(question),
            '<h2>Question</h2><p>Steps\ncontinue</p><ul><li>First</li><li>Second</li></ul>'
            + '<ol><li>Third</li><li>Fourth</li></ol><p>Done</p><h2>Answer</h2><ul><li>Yes</li><li>No</li></ul>')

    def test_code_blocks_remove_blank_edges_and_keep_internal_text_line_breaks(self) -> None:
        for text, before, after in (
            ('A:\n\n```sh\nx\n```\n\nB.', 'A:', 'B.'),
            ('\n \t\n A:\nSecond line\n \t\n```sh\nx\n```\n \t\nB. \n\n', ' A:\nSecond line', 'B. '),
        ):
            with self.subTest(text=text):
                rendered = change_page.review_html(text)
                self.assertEqual(rendered, '<p>' + before + '</p>'
                                 + '<div class="code-block"><pre><code>x</code></pre>'
                                 + page_frame.COPY_BUTTON + '</div><p>' + after + '</p>')
        self.assertEqual(change_page.review_html('\n\nA:\nSecond line\n\n'), '<p>A:\nSecond line</p>')

    def test_review_text_escapes_text_blocks_and_each_list_item(self) -> None:
        text = '<script>& "text"\n\n- <a href="x"> & **bold**\n1. `code` | <img>'
        self.assertEqual(change_page.review_text_html(text),
                         '<p>&lt;script&gt;&amp; &quot;text&quot;</p>'
                         + '<ul><li>&lt;a href=&quot;x&quot;&gt; &amp; **bold**</li></ul>'
                         + '<ol><li>`code` | &lt;img&gt;</li></ol>')

    def test_each_numbered_run_starts_at_its_first_number(self) -> None:
        self.assertEqual(change_page.review_text_html('3. Third\n4. Fourth\n\n1. First\n- Bullet\n8. Eighth'),
            '<ol start="3"><li>Third</li><li>Fourth</li></ol><ol><li>First</li></ol>'
            + '<ul><li>Bullet</li></ul><ol start="8"><li>Eighth</li></ol>')

    def test_question_text_links_addresses_and_leaves_punctuation_outside(self) -> None:
        link = '<a href="http://h:1/x?a=b&amp;c=d" target="_top">http://h:1/x?a=b&amp;c=d</a>'
        self.assertEqual(change_page.review_text_html("See http://h:1/x?a=b&c=d.\n- <b> https://e.invalid/p)", inline=True),
                         f'<p>See {link}.</p><ul><li>&lt;b&gt; <a href="https://e.invalid/p" target="_top">'
                         + 'https://e.invalid/p</a>)</li></ul>')
        self.assertEqual(change_page.review_text_html("See http://h:1/x"), "<p>See http://h:1/x</p>")

    def test_question_text_shows_backtick_spans_as_code(self) -> None:
        self.assertEqual(change_page.inline_html("Run `a <b>` then see http://h:1/x and `http://h:1/y`"),
                         'Run <code>a &lt;b&gt;</code> then see <a href="http://h:1/x" target="_top">http://h:1/x</a>'
                         + ' and <code>http://h:1/y</code>')
        self.assertEqual(change_browser.item_list([" `deploy.py up --x <y>` ", "Run `ls` now", "`a` and `b`"]),
                         '<h2>Items</h2><ol class="question-items"><li><div class="code-block">'
                         + '<pre><code>deploy.py up --x &lt;y&gt;</code></pre>' + page_frame.COPY_BUTTON + '</div></li>'
                         + '<li>Run <code>ls</code> now</li><li><code>a</code> and <code>b</code></li></ol>')
        # A showrunner reply or answer shows a command line as a code block; a span inside a sentence stays inline.
        for text, expected in (
            ("Run this:\n`git push origin --delete a <b>`\nThen check.",
             '<p>Run this:</p><div class="code-block"><pre><code>git push origin --delete a &lt;b&gt;</code></pre>'
             + page_frame.COPY_BUTTON + '</div><p>Then check.</p>'),
            ("Run `ls` now.", '<p>Run <code>ls</code> now.</p>'),
        ):
            with self.subTest(text=text):
                self.assertEqual(change_browser.answer_html(text), expected)

        command = "git log --grep='Choices: <subject>'"
        command_line = f"On natedev:\n`{command}`"
        block = ('<div class="code-block"><pre><code>' + html.escape(command)
                 + '</code></pre>' + page_frame.COPY_BUTTON + '</div>')
        for section, question in (
            ("lead", command_line),
            ("after items", "1. Read the title.\n2. Check the log.\n\n" + command_line),
            ("choices", "Choices: " + command_line + "\nOr read the log."),
        ):
            with self.subTest(section=section):
                item = update_registration.OutstandingItem(
                    since="now", text="What does the log show?", question="What does the log show?\n\n" + question)
                body, _ = change_browser.question_parts(item)
                self.assertIn(block, body)
        title = "What does the log show?"
        item = update_registration.OutstandingItem(since="now", text=title,
            parts=update_registration.QuestionParts(ask=title, context=command_line, items=[], choices=[], recommend=""))
        self.assertEqual(change_browser.question_parts(item), ('<h2>Context</h2><p>On natedev:</p>' + block, ""))
        self.assertIn(block, change_page.review_text_html(command_line, inline=True))

    def test_shared_inline_markdown_renders_bold_headings_and_lists(self) -> None:
        text = '## Heading\n**Field:** <script> & ready\n- **Owner:** morty\n1. **Next:** `run`'
        expected = ('<h2>Heading</h2><p><strong>Field:</strong> &lt;script&gt; &amp; ready</p>'
                    + '<ul><li><strong>Owner:</strong> morty</li></ul>'
                    + '<ol><li><strong>Next:</strong> <code>run</code></li></ol>')
        self.assertEqual(change_page.review_text_html(text, inline=True), expected)
        self.assertEqual(change_browser.question_paragraphs(text), expected)
        self.assertEqual(change_page.review_text_html('## **Title** ###', inline=True), '<h2><strong>Title</strong></h2>')
        self.assertEqual(change_page.inline_html('**Use `a <b>` now** and `**literal**`'),
                         '<strong>Use <code>a &lt;b&gt;</code> now</strong> and <code>**literal**</code>')
        self.assertEqual(change_page.inline_html('**One** and **Two**'), '<strong>One</strong> and <strong>Two</strong>')
        self.assertEqual(change_page.inline_html('**a `**` b**'), '<strong>a <code>**</code> b</strong>')
        self.assertEqual(change_page.inline_html('** ** and unmatched **word'), '** ** and unmatched **word')

    def test_shared_inline_markdown_preserves_literal_urls_and_paths(self) -> None:
        for text, expected in (
            ('https://example.org/a**b**c',
             '<a href="https://example.org/a**b**c" target="_top">https://example.org/a**b**c</a>'),
            ('src/**/test/**/file.py', 'src/**/test/**/file.py'),
            ('**word** and **Field:** ready', '<strong>word</strong> and <strong>Field:</strong> ready'),
        ):
            with self.subTest(text=text):
                self.assertEqual(change_page.inline_html(text), expected)
                self.assertEqual(change_browser.question_paragraphs(text), '<p>' + expected + '</p>')

    def test_shared_inline_markdown_uses_bold_boundaries_and_code_before_urls(self) -> None:
        for text, expected in (
            ('Use **read/write** mode.', '<p>Use <strong>read/write</strong> mode.</p>'),
            ('## **Read/write**', '<h2><strong>Read/write</strong></h2>'),
            ('https://example.org/`id`',
             '<p><a href="https://example.org/" target="_top">https://example.org/</a><code>id</code></p>'),
            ('(**read/write**).', '<p>(<strong>read/write</strong>).</p>'),
            ('word**bold** and **bold**word', '<p>word**bold** and **bold**word</p>'),
            ('** word** and **word **', '<p>** word** and **word **</p>'),
            ('**Use src/**/test/**/file.py now**', '<p><strong>Use src/**/test/**/file.py now</strong></p>'),
        ):
            with self.subTest(text=text):
                self.assertEqual(change_page.review_text_html(text, inline=True), expected)
                self.assertEqual(change_browser.question_paragraphs(text), expected)

    def test_shared_inline_markdown_requires_content_and_keeps_url_endings(self) -> None:
        for text, expected in (
            ('src/{**/test/**,other}/file.py', 'src/{**/test/**,other}/file.py'),
            ('**https://example.org**',
             '<strong><a href="https://example.org" target="_top">https://example.org</a></strong>'),
            ('Mask: ****', 'Mask: ****'),
            ('https://example.org/a`tail',
             '<a href="https://example.org/a`tail" target="_top">https://example.org/a`tail</a>'),
        ):
            with self.subTest(text=text):
                self.assertEqual(change_page.inline_html(text), expected)
                self.assertEqual(change_browser.question_paragraphs(text), '<p>' + expected + '</p>')

    def test_code_copy_markup_keeps_code_text_separate_from_the_control(self) -> None:
        code = '  first <tag> & value\tsecond  '
        rendered = change_browser.item_html('`' + code + '`')
        self.assertIn('<pre><code>' + html.escape(code) + '</code></pre>' + page_frame.COPY_BUTTON, rendered)
        self.assertEqual(rendered.count(page_frame.COPY_BUTTON), 1)
        self.assertIn('type="button" data-copy-code aria-label="Copy code" title="Copy"', rendered)
        inline = change_page.document(change_page.inline_html('Run `sudo systemctl start session.mount`.')).decode()
        self.assertNotIn(page_frame.COPY_BUTTON, inline)
        self.assertIn('<code>sudo systemctl start session.mount</code>', inline)
        plain = change_page.review_html('Keep this proof text.')
        self.assertEqual(plain, '<p>Keep this proof text.</p>')
        self.assertIn(plain, change_page.document(plain).decode())
        self.assertNotIn(page_frame.COPY_BUTTON, change_page.document(plain).decode())

    def test_question_commands_stay_inside_the_item_that_ends_with_a_colon(self) -> None:
        for block, code in (("```sh\nprintf '<check>'\n```", "printf '<check>'"),
                            ("`printf '<check>'`", "printf '<check>'")):
            for form in ("parts continuation", "parts entry", "one text"):
                with self.subTest(block=block, form=form):
                    item = update_registration.OutstandingItem(since="now", text="Check the steps?")
                    if form.startswith("parts"):
                        items = (["Read the title.", "Run this:\n\n" + block, "Read the result."]
                                 if form == "parts continuation" else
                                 ["Read the title.", "Run this:", block, "Read the result."])
                        item["parts"] = update_registration.QuestionParts(ask="Check the steps?", context="",
                            items=items, recommend="", choices=[])
                    else:
                        item["question"] = "Check the steps?\n\n1. Read the title.\n2. Run this:\n\n" + block + "\n3. Read the result."
                    body, _ = change_browser.question_parts(item)
                    entries = [match.group(1) for match in re.finditer(r"<li>(.*?)</li>", body, re.DOTALL)]
                    self.assertEqual(len(entries), 3)
                    self.assertEqual(entries[0], "Read the title.")
                    self.assertEqual(entries[2], "Read the result.")
                    self.assertTrue(entries[1].startswith('Run this:<div class="code-block"><pre><code>'))
                    self.assertIn("<pre><code>" + html.escape(code) + "</code></pre>", entries[1])
                    self.assertEqual(entries[1].count(page_frame.COPY_BUTTON), 1)
                    self.assertNotIn("```", body)

    def test_question_item_numbers_and_blank_lines_inside_code_do_not_split_the_list(self) -> None:
        for block in ("```text\n1. code line\n\n2. code line\n```", "`1. code line 2. code line`"):
            for final in (False, True):
                with self.subTest(block=block, final=final):
                    text = ("1. Read the title.\n2. Run this:\n\n" + block
                            + ("\n\nContext after the list." if final else "\n3. Read the result."))
                    lead, items, trailing = change_browser.numbered_run(text)
                    self.assertEqual(lead, "")
                    self.assertEqual(len(items), 2 if final else 3)
                    self.assertEqual(items[1], "Run this:\n\n" + block)
                    self.assertEqual(trailing, "Context after the list." if final else "")
                    body, _ = change_browser.text_sections(text)
                    self.assertEqual(body.count("<li>"), 2 if final else 3)
                    self.assertEqual(body.count(page_frame.COPY_BUTTON), 1)

    def test_reviewed_question_without_a_question_mark_uses_its_first_sentence_as_the_title(self) -> None:
        item = update_registration.OutstandingItem(since="now", text="Stored summary",
            reviewed="Check the title. Keep this context once.\n\nCan the later paragraph stay in the body?")
        title, context = change_browser.question_text(item)
        self.assertEqual(title, "Check the title.")
        self.assertEqual(context, "Keep this context once.\n\nCan the later paragraph stay in the body?")
        body, _ = change_browser.question_parts(item)
        self.assertNotIn(title, body)
        self.assertEqual(body.count("Keep this context once."), 1)
        self.assertEqual(body.count("Can the later paragraph stay in the body?"), 1)
        for reviewed in ("Check the title. keep this context once.", "Check the title! keep this context once."):
            item["reviewed"] = reviewed
            self.assertEqual(change_browser.question_text(item), (reviewed, ""))
        for reviewed in ("Check the title. Keep this context once.", "Check the title! Keep this context once."):
            item["reviewed"] = reviewed
            self.assertEqual(change_browser.question_text(item), (reviewed[:16], "Keep this context once."))
        item["reviewed"] = "Keep this question? Keep the present title rule.\n\nContext."
        self.assertEqual(change_browser.question_text(item),
                         ("Keep this question? Keep the present title rule.", "Context."))

    def test_question_parts_show_as_sections_with_choice_buttons(self) -> None:
        parts = update_registration.QuestionParts(
            ask="Approve both page changes to land?", context="Two changes wait.",
            items=["AWS filter\nAfter: http://h:1/i", "Memory <bar>"], recommend="go for both",
            choices=["go for both", 'no "yet"'])
        item = update_registration.OutstandingItem(
            since="2026-11-15T08:00", text="Approve both?", question="joined", parts=parts)
        body, choices = change_browser.question_parts(item)
        self.assertEqual(body, '<p class="question-ask">Approve both page changes to land?</p>'
                         + '<h2>Context</h2><p>Two changes wait.</p><h2>Items</h2><ol class="question-items">'
                         + '<li>AWS filter<br>After: <a href="http://h:1/i" target="_top">http://h:1/i</a></li>'
                         + '<li>Memory &lt;bar&gt;</li></ol><h2>Recommended</h2><p>go for both</p>')
        self.assertEqual(choices, '<fieldset class="question-choices"><legend>Choices</legend>'
                         + '<label><input type="radio" name="choice" value="go for both"><span>go for both</span></label>'
                         + '<label><input type="radio" name="choice" value="no &quot;yet&quot;"><span>no &quot;yet&quot;</span></label>'
                         + '</fieldset>')
        parts["ask"] = "approve both"
        parts["context"] = parts["recommend"] = ""
        parts["items"] = parts["choices"] = []
        self.assertEqual(change_browser.question_parts(item), ("", ""))
        del item["parts"]
        self.assertEqual(change_browser.question_parts(item), ("", ""))

    def test_memory_bar_reviewed_text_keeps_paragraphs_lists_and_three_choice_buttons(self) -> None:
        item = update_registration.OutstandingItem(since="2026-11-15", text=MEMORY_BAR_RAW,
            question=MEMORY_BAR_RAW, reviewed=MEMORY_BAR_REVIEWED)
        title, context = change_browser.question_text(item)
        self.assertEqual(title, MEMORY_BAR_ASK)
        self.assertNotIn(MEMORY_BAR_ASK, context)
        body, choices = change_browser.question_parts(item)
        self.assertNotIn(MEMORY_BAR_ASK, body + choices)
        self.assertIn('<ul><li>sccache, the compiler cache: 2.2 GiB now, nearly all of the block.</li>', body)
        self.assertIn("<li>rust-analyzer, the editor&#x27;s Rust checker: 0 GiB now.</li>", body)
        self.assertIn("<p>The resources unit named the block &#x27;Build tools&#x27;. Nobody approved that name.</p>", body)
        self.assertNotIn('<h2>Recommended</h2>', body)
        self.assertEqual(choices.count('name="choice"'), 3)
        self.assertIn('(recommended)', choices)
        self.assertNotIn('because it names the contents', body + choices)
        item["reviewed"] = MEMORY_BAR_REVIEWED + "\n\nRecommended: Keep the compiler cache name."
        body, choices = change_browser.question_parts(item)
        self.assertIn('<h2>Recommended</h2><p>Keep the compiler cache name.</p>', body)
        self.assertEqual(choices.count('name="choice"'), 3)

    def test_open_question_forms_use_short_titles_and_separate_context(self) -> None:
        parts = update_registration.QuestionParts(ask="Keep the name?", context="First paragraph.\n\nSecond paragraph.",
            items=["First item", "Second item"], recommend="Keep it", choices=["Keep it", "Change it"])
        cases: tuple[tuple[str, update_registration.OutstandingItem, str, int], ...] = (
            ("parts", update_registration.OutstandingItem(since="2026-11-15T20:05", text="Keep the name?", parts=parts), "Keep the name?", 2),
            ("reviewed only", update_registration.OutstandingItem(since="2026-11-15T20:05", text=MEMORY_BAR_RAW,
                reviewed=MEMORY_BAR_REVIEWED), MEMORY_BAR_ASK, 3),
            ("raw one line", update_registration.OutstandingItem(since="2026-11-15T20:05", text="Stored summary",
                question="Keep the name? First paragraph. Second paragraph."), "Keep the name?", 0),
            ("raw choices", update_registration.OutstandingItem(since="2026-11-15T20:05", text=MEMORY_BAR_RAW,
                question=MEMORY_BAR_RAW), MEMORY_BAR_ASK, 3),
        )
        owner = staff.StaffId("owner")
        for name, item, title, choices in cases:
            with self.subTest(form=name):
                record = update_registration.QuestionRecord(Path("/unused/question.md"), owner, owner, item, "open")
                question = change_browser.WaitingQuestion("q1", "owner", record, Path("/unused/production.md"), owner, "asker")
                section = change_browser.question_section(question, change_page.ReviewBrowser("private"), 4)
                self.assertEqual(section.count(title), 1)
                self.assertIn(f'<h1>{title}</h1>', section)
                self.assertEqual(section.count('name="choice"'), choices)
                self.assertNotIn("Asked by", section)
                self.assertIn('<dl class="change-details"><dt>source:</dt><dd>asker</dd>'
                              + '<dt>date:</dt><dd>2026-11-15</dd><dt>time:</dt><dd>20:05</dd></dl>', section)
                if name in ("parts", "raw one line"):
                    self.assertIn('<p>First paragraph.</p><p>Second paragraph.</p>', section)
                if name == "raw choices":
                    self.assertIn('<h2>Recommended</h2><p>&#x27;Compiler cache&#x27;, because it names the contents.</p>', section)
                    self.assertIn('Your own name: the resources unit puts your name on the block.', section)

    def test_reviewed_presence_and_ambiguous_choices_do_not_restore_raw_text_or_invent_buttons(self) -> None:
        item = update_registration.OutstandingItem(since="now", text="Stored summary",
            question="Do not show this raw question? Raw text.", reviewed="Reviewed question?\n\nOne paragraph.\n\nAnother paragraph.")
        self.assertEqual(change_browser.question_parts(item), ('<p>One paragraph.</p><p>Another paragraph.</p>', ""))
        item["reviewed"] = ""
        self.assertEqual(change_browser.question_parts(item), ("", ""))
        _ = item.pop("reviewed")
        item["question"] = "Keep it? Choices: yes = keep; no = drop. Recommended: yes"
        self.assertEqual(change_browser.question_parts(item),
            ('<h2>Choices</h2><ul><li>yes = keep</li><li>no = drop.</li></ul><h2>Recommended</h2><p>yes</p>', ""))

    def test_title_uses_the_first_question_sentence_and_context_keeps_earlier_sentences(self) -> None:
        item = update_registration.OutstandingItem(since="now", text="Stored summary",
            question="A change waits. Keep it? use this name. Is that clear?")
        self.assertEqual(change_browser.question_text(item), ("Keep it?", "A change waits.\n\nuse this name. Is that clear?"))


    def test_one_text_question_shows_items_choices_and_recommendation_as_sections(self) -> None:
        body, choices = change_browser.text_sections(
            "Check two things. 1) A <b> shows; 2) One click works. It takes a minute. "
            + "Choices: (a) ready: launch now; (b) later: name a time. Recommended: ready, see http://h:1/i")
        self.assertEqual(body, '<p>Check two things.</p><h2>Items</h2><ol class="question-items">'
                         + '<li>A &lt;b&gt; shows</li><li>One click works.</li></ol><p>It takes a minute.</p>'
                         + '<h2>Recommended</h2><p>ready, see <a href="http://h:1/i" target="_top">http://h:1/i</a></p>')
        self.assertEqual(choices, '<fieldset class="question-choices"><legend>Choices</legend>'
                         + '<label><input type="radio" name="choice" value="ready: launch now"><span>ready: launch now</span></label>'
                         + '<label><input type="radio" name="choice" value="later: name a time."><span>later: name a time.</span></label>'
                         + '</fieldset>')
        with self.subTest("choices without numbers show as text"):
            self.assertEqual(change_browser.text_sections("Keep it? Choices: yes = keep; no = drop. Recommended: yes"),
                             ('<p>Keep it?</p><h2>Choices</h2><ul><li>yes = keep</li><li>no = drop.</li></ul>'
                              + '<h2>Recommended</h2><p>yes</p>', ""))
        with self.subTest("numbers out of order are not items"):
            self.assertEqual(change_browser.text_sections("Phase 2. Then 1) only one item."),
                             ("<p>Phase 2.</p><p>Then 1) only one item.</p>", ""))
        with self.subTest("lines: a blank line ends the last item"):
            self.assertEqual(change_browser.text_sections("Pick one.\n\nChoices:\n1. Add it. Then measure.\n2. Keep it.\n\nMeanwhile I port."),
                             ('<p>Pick one.</p><p>Meanwhile I port.</p>',
                              '<fieldset class="question-choices"><legend>Choices</legend>'
                              + '<label><input type="radio" name="choice" value="Add it. Then measure."><span>Add it. Then measure.</span></label>'
                              + '<label><input type="radio" name="choice" value="Keep it."><span>Keep it.</span></label></fieldset>'))
        with self.subTest("1. items"):
            self.assertEqual(change_browser.text_sections("Steps: 1. open 2. close")[0],
                             '<p>Steps:</p><h2>Items</h2><ol class="question-items"><li>open</li><li>close</li></ol>')

class ChangePageTests(unittest.TestCase):
    def test_rebuild_staff_requesters_have_role_colors_and_user_names_stay_plain(self) -> None:
        records = [change_browser.Rebuild("2026-11-17T12:00:00+00:00", self.unit.name + " (unit)", "Read the result"),
                   change_browser.Rebuild("2026-11-17T12:01:00+00:00", "nate", "Keep this reason")]
        body = change_browser.owner_groups(records)
        self.assertRegex(body, r'data-color-text="role.unit-director"[^>]*>builder-unit \(unit\)</span>')
        self.assertIn('<h3>nate</h3>', body)

    def test_legacy_question_sources_have_role_colors_and_free_names_stay_plain(self) -> None:
        key = self.file_user_question("Read this result?", asker=self.unit)
        body = self.browser("questions")[2]
        self.assertIn(key, body)
        self.assertRegex(body, r'data-color-text="role.showrunner"[^>]*>page-owner</span>')
        self.assertRegex(body, r'data-color-text="role.unit-director"[^>]*>builder-unit</span>')
        self.assertNotRegex(body, r'class="staff-name"[^>]*>nate</span>')
        browser = change_page.ReviewBrowser('private', kind='questions')
        question = next(item for item in change_browser.prepare(browser, change_page.DeliveryStatus(self.root)).questions
                        if item.key == key)
        unknown = replace(question, asker='nate', record=replace(question.record, asker=staff.StaffId('nate')))
        self.assertIn('<dt>source:</dt><dd>nate</dd>', change_browser.question_section(unknown, browser, 2))

    temporary: Path = Path()
    root: Path = Path()
    repo: Path = Path()
    production: Path = Path()
    review_list: Path = Path()
    page_path: str = ""
    page: change_page.ReviewPage = change_page.ReviewPage(Path(), Path(), (), "")
    port: int = 0
    unit: staff.StaffRow
    showrunner_id: staff.StaffId = staff.StaffId("")
    raw_messages: list[tuple[str, str, str]]
    messages: list[tuple[str, str, str]] = []
    delivery_done: threading.Event = threading.Event()

    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.raw_messages = []
        self.unit = staff.StaffRow(staff.StaffId(""), "unit", "", staff.StaffRoot(), current_session=staff.StaffId(""), unit_type=staff.UnitDetails("direct"))

    def command(self, module: ModuleType, main: Callable[[list[str]], int],
                arguments: list[str]) -> subprocess.CompletedProcess[str]:
        if process_boundary_for_case(self):
            return subprocess.run([str(Path(__file__).resolve().parents[1] / "lib/py"),
                                   str(module.__file__), *arguments],
                                  capture_output=True, text=True, check=False)
        return invoke_cli(module, lambda: main(arguments), arguments, dict(os.environ), Path.cwd())

    def test_both_pages_show_lists_in_the_question_answer_and_check(self) -> None:
        item = change_review.Visible('Numbered moves', 'Steps\n- Click <one>\n- Click two\n\nFinish', 'Result')
        _ = change_review.edit_row(self.review_list, 'C1', item, date(2026, 11, 5), self.production, image=fixture_row_image(self.review_list, 'C1'))
        status = change_page.DeliveryStatus(self.root)
        question = status.ask(self.page, 'C1', 'Which steps?\n1. First\n2. Second')
        assert isinstance(question, change_page.QuestionDelivery)
        self.assertTrue(status.answer_question(self.page, 'C1', question.question_id, 'Use these:\n- One\n- Two'))
        for content in (change_page.render(self.page, status).decode(),
                        change_browser.render(change_page.ReviewBrowser('token'), status).decode()):
            with self.subTest(page=content[:80]):
                self.assertIn('<p>Steps</p>', content)
                self.assertIn('<ul><li>Click &lt;one&gt;</li><li>Click two</li></ul>', content)
                self.assertIn('<p>Finish. You see: Result</p>', content)
                self.assertIn('<h2>Question</h2><p>Which steps?</p><ol><li>First</li><li>Second</li></ol>', content)
                self.assertIn('<h2>Answer</h2><p>Use these:</p><ul><li>One</li><li>Two</li></ul>', content)

    def test_both_pages_copy_question_feedback_and_answer_code_without_boxing_plain_text(self) -> None:
        code = 'git -C /etc/nixos status --short\ngit -C /etc/nixos log --oneline -3'
        fenced = 'Run this in a natedev terminal:\n\n```sh\n' + code + '\n```\n\nRead the result.'
        status = change_page.DeliveryStatus(self.root)
        for feedback in (False, True):
            for answer in (fenced, 'First line\nSecond line'):
                with self.subTest(feedback=feedback, answer=answer):
                    row_id = 'C3' if feedback else 'C1'
                    question = status.ask(self.page, row_id, fenced, kind='feedback' if feedback else 'question')
                    assert isinstance(question, change_page.QuestionDelivery)
                    self.assertTrue(status.answer_question(self.page, row_id, question.question_id, answer))
                    selected = change_page.render(replace(self.page, rows=(row_id,)), status).decode()
                    browser = self.browser()[2]
                    for page in (selected, browser):
                        self.assertEqual(page.count(page_frame.BUTTON_STATES), 1)
                    section = re.search(r'<section\b[^>]*data-key="page-owner/' + row_id
                                        + r'"[^>]*>(.*?)</section>', browser, re.DOTALL)
                    assert section is not None
                    for body in (selected, section[1]):
                        blocks = [html.unescape(match[1]) for match in re.finditer(r'<pre><code>(.*?)</code></pre>', body, re.DOTALL)]
                        self.assertEqual(blocks, [code, code] if answer == fenced else [code])
                        self.assertEqual(body.count(page_frame.COPY_BUTTON), len(blocks))
                        self.assertEqual(body.count('<pre>'), len(blocks))
                        if answer != fenced:
                            prefix = 'Showrunner: ' if feedback else ''
                            self.assertIn('<p>' + prefix + answer + '</p>', body)
                    status.write_slots(self.page, row_id, change_page.DeliverySlots(change_page.NoDelivery(), change_page.NoDelivery()))

    def test_both_pages_keep_a_plain_two_line_check_in_one_paragraph(self) -> None:
        item = change_review.Visible('Numbered moves', 'First line\nSecond line', 'Result')
        _ = change_review.edit_row(self.review_list, 'C1', item, date(2026, 11, 5), self.production, image=fixture_row_image(self.review_list, 'C1'))
        row = change_page.answer_row(self.page, 'C1')
        selected = change_page.render(self.page, change_page.DeliveryStatus(self.root)).decode()
        change = change_browser.WaitingChange('page-owner/C1', 'page-owner',
                                             change_routing.UnavailableProduction(self.review_list), row)
        for body in (selected, change_browser.section(change, 5)):
            self.assertIn('<p>First line\nSecond line. You see: Result</p>', body)
            self.assertNotIn('<pre>', body)
        self.assertIn('p{white-space:pre-wrap;', selected)

    def test_answer_question_holds_a_draft_before_saving_the_answer(self) -> None:
        status = change_page.DeliveryStatus(self.root)
        question = status.ask(self.page, "C1", "What happens next?")
        assert isinstance(question, change_page.QuestionDelivery)
        arguments = ["answer-question", "--production", "page-owner", "--list", str(self.review_list),
                     "--row", "C1", "--question", question.question_id, "--text"]
        path = status.path(self.page, "C1")
        before = path.read_bytes()
        with (patch.dict(os.environ, {"NOTIFY_STATE_DIR": str(self.temporary / "notify")}),
              patch.object(review, "review_question",
                           return_value=review.QuestionAccepted("The window stays open.")) as reader):
            errors = StringIO()
            with redirect_stderr(errors):
                self.assertEqual(change_page.main([*arguments, "Read /tmp/report.txt."]), 1)
            self.assertIn("Revise the reply draft. omit-file-path: /tmp/report.txt", errors.getvalue())
            self.assertEqual(path.read_bytes(), before)
            reader.assert_not_called()

            self.assertEqual(change_page.main([*arguments, "The window stays open."]), 0)
            saved = status.slots(self.page, "C1").question
            self.assertEqual(saved, change_page.AnsweredQuestion(
                question.question_id, question.text, "The window stays open."))
            reader.assert_called_once()

    def test_reply_reader_runs_before_the_delivery_lock_and_fits_the_ssh_timeout(self) -> None:
        status = change_page.DeliveryStatus(self.root)
        question = status.ask(self.page, "C1", "What happens next?")
        assert isinstance(question, change_page.QuestionDelivery)
        words = "The window stays open."
        with (patch.object(status, "locked", wraps=status.locked) as locked,
              patch.object(review, "review_question") as reader):
            def replace_question(*_args: object, **_kwargs: object) -> review.QuestionAccepted:
                locked.assert_not_called()
                slots = status.slots(self.page, "C1")
                assert isinstance(slots.question, change_page.OpenQuestion)
                replacement = replace(slots.question, question_id="new-question")
                with status.locked():
                    status.write_slots(self.page, "C1", replace(slots, question=replacement))
                return review.QuestionAccepted(words)

            reader.side_effect = replace_question
            with self.assertRaisesRegex(question_record.AnswerRefused, "does not match"):
                _ = status.answer_question(self.page, "C1", question.question_id, words)
            saved = status.slots(self.page, "C1").question
            assert isinstance(saved, change_page.OpenQuestion)
            self.assertEqual(saved.question_id, "new-question")

        config = self.temporary / "notify.conf"
        _ = config.write_text("REVIEW_TIMEOUT_SECONDS=113\n")
        with (patch.object(review, "NOTIFY_CONFIG", config),
              patch.object(remote, "local_machine", return_value="natedev"),
              patch.object(remote, "machine_alias", return_value=remote.MachineAlias("mac")),
              patch.object(remote, "this_machine", return_value="natedev"),
              patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as ssh):
            self.assertEqual(change_page.main([
                "answer-question", "--machine", "mac", "--item", "submission/question/owner/item/request",
                "--question", question.question_id, "--text", words,
            ]), 0)
        self.assertEqual(ssh.call_args.kwargs["timeout"], 143)

    def test_native_review_replies_release_the_delivery_lock_and_recheck_the_question(self) -> None:
        from scripts.production import investigation

        report = investigation.file(self.unit.id, "Why did it stop?", "Find the cause.",
                                    waiting_why="The owner must find the cause.")
        keys = (self.file_user_question(), f"investigation/{report.owner}/{report.id}")
        status = change_page.DeliveryStatus(self.root)
        words = "The new layout is ready."
        for key in keys:
            with self.subTest(key=key):
                item = next(item for item in review_items.read(self.root) if item.key == key)
                followup = "Check the new layout."
                if isinstance(item.source, review_items.QuestionSource):
                    delivery, _ = review_actions.question_action(item, "question", followup, status)
                else:
                    delivery, _ = review_actions.investigation_action(item, "question", followup, status)
                assert isinstance(delivery, change_page.MessageDelivery)
                delivery = review_actions.track_question(delivery, key, followup, status)
                change_page.deliver(delivery, status)
                page, row = delivery.page, delivery.row
                slots = status.slots(page, row)
                assert isinstance(slots.question, change_page.OpenQuestion)
                original = slots.question
                source = page.review_list
                before = source.read_bytes()

                def read_draft(*_args: object, **_kwargs: object) -> review.QuestionAccepted:
                    with (self.root / ".review-link-delivery.lock").open("r") as handle:
                        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    with status.locked():
                        replacement = replace(original, question_id="replacement")
                        status.write_slots(page, row, replace(slots, question=replacement))
                    return review.QuestionAccepted(words)

                with patch.object(review, "review_question", side_effect=read_draft) as reader:
                    with self.assertRaisesRegex(question_record.AnswerRefused, "does not match"):
                        _ = change_page.answer_item(key, words, original.question_id, status)
                reader.assert_called_once()
                self.assertEqual(source.read_bytes(), before)
                self.assertEqual(status.slots(page, row).question, replace(original, question_id="replacement"))

                with status.locked():
                    status.write_slots(page, row, slots)
                with patch.object(review, "review_question", return_value=review.QuestionAccepted(words)) as reader:
                    self.assertTrue(change_page.answer_item(key, words, original.question_id, status))
                    self.assertFalse(change_page.answer_item(key, words, original.question_id, status))
                reader.assert_called_once()
                self.assertIn(words, source.read_text())
                self.assertEqual(status.slots(page, row).question,
                                 change_page.AnsweredQuestion(original.question_id, original.text, words))

    def test_answer_question_keeps_internal_line_breaks_and_an_identical_rerun_changes_nothing(self) -> None:
        status = change_page.DeliveryStatus(self.root)
        question = status.ask(self.page, 'C1', 'Which steps?')
        assert isinstance(question, change_page.QuestionDelivery)
        answer = '  \n1. First\n\n2.  Second | <result>\n  '
        arguments = ['answer-question', '--production', 'page-owner', '--list', str(self.review_list),
                     '--row', 'C1', '--question', question.question_id, '--text', answer]
        self.assertEqual(change_page.main(arguments), 0)
        saved = status.slots(self.page, 'C1').question
        self.assertEqual(saved, change_page.AnsweredQuestion(question.question_id, question.text, answer.strip()))
        record = status.path(self.page, 'C1')
        before, modified = record.read_bytes(), record.stat().st_mtime_ns
        self.assertEqual(change_page.main(arguments), 0)
        self.assertEqual((record.read_bytes(), record.stat().st_mtime_ns), (before, modified))
        self.assertIn('<h2>Answer</h2><ol><li>First</li></ol><ol start="2"><li> Second | &lt;result&gt;</li></ol>',
                      change_page.render(self.page, status).decode())

    def test_a_row_without_a_production_document_shows_proof_lists(self) -> None:
        row = change_page.answer_row(self.page, 'C1') | {change_review.CHECK: 'Read\n- First\n- Second'}
        change = change_browser.WaitingChange('orphan/C1', 'orphan',
            change_routing.UnavailableProduction(self.review_list), row)
        self.assertIn('<h2>Proof</h2><p>Read</p><ul><li>First</li><li>Second</li></ul>',
                      change_browser.section(change, 5))

    def test_publications_keep_the_html_and_manifest_under_one_configured_root(self) -> None:
        settings = Path(os.environ["CONF_DIR"]) / "change_review.conf"
        real_write = atomic_file.write_bytes_atomic
        publications: list[tuple[Callable[[], str], str]] = [
            (lambda: change_page.publish(self.production, self.review_list, ["C1"]), change_page.MANIFEST),
        ]
        for index, (publish, manifest) in enumerate(publications):
            first = self.temporary / f"first-{index}"
            second = self.temporary / f"second-{index}"
            _ = write_config_fixture(settings, f"review_links_directory={first}\n")

            def write(path: Path, content: bytes, *, mode: int = 0o600) -> None:
                real_write(path, content, mode=mode)
                if path.name == change_page.PAGE_NAME:
                    _ = write_config_fixture(settings, f"review_links_directory={second}\n")

            with (self.subTest(manifest=manifest), patch.dict(os.environ),
                  patch.object(atomic_file, "write_bytes_atomic", side_effect=write)):
                _ = os.environ.pop("REVIEW_LINKS_ROOT", None)
                url = publish()
                folder = first / urlsplit(url).path.split("/")[1]
                self.assertTrue((folder / change_page.PAGE_NAME).is_file())
                self.assertTrue((folder / manifest).is_file())
                self.assertFalse(second.exists())
                self.assertEqual(review_link.review_root(), second)

    def test_bare_command_prints_the_console_link_with_changes_selected(self) -> None:
        configuration = self.temporary / "config"
        _ = write_config_fixture(configuration / "console.conf",
            "page.review.title=Review\npage.review.url=python:scripts.review_links.review_page:page_url\n"
            + "rebuilds.url=python:scripts.review_links.change_page:rebuilds_url\n")
        _ = (configuration / "memory_feed.conf").write_text(f"output_folder={self.root / 'builds'}\n")
        result = self.command(change_page, change_page.main, [])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        url = urlsplit(result.stdout.strip())
        self.assertEqual((url.scheme, url.path, url.query), ("http", "/console/", ""))
        self.assertEqual(self.command(change_page, change_page.main, []).stdout, result.stdout)
        console = self.request(url.path, {})
        self.assertEqual(console.status, 200)
        self.assertIn('data-url="/console/review.html" data-title="Review"', console.body)
        self.assertIn('data-url="/console/rebuilds.html" data-title="Rebuilds needed"', console.body)
        manifest = self.root / change_page.CONSOLE_FOLDER / change_page.BROWSER_MANIFEST
        token = manifest.read_bytes()
        self.assertEqual(manifest.stat().st_mode & 0o777, 0o600)
        redirect = self.http_request("/console/changes.html", {})
        self.assertEqual((redirect.status, redirect.location), (303, "/console/review.html?type=change"))
        changes = self.legacy_browser_reply("/console/changes.html")
        self.assertIn("if(parent!==window) parent.postMessage('console-place',location.origin);", changes.body)
        self.assertIn("const hidden=()=>document.visibilityState!=='visible' || window.frameElement?.hidden===true;", changes.body)
        self.assertIn("event.data==='console-shown')", changes.body)
        self.assertIn("restart();void refresh(true);", changes.body)
        self.assertIn("createPageRefresh(refreshPage,()=>content.dataset.refreshSeconds,()=>", changes.body)
        self.assertEqual(self.request("/console/rebuilds.html", {}).status, 200)
        self.assertEqual(change_page.page_url(), "/console/changes.html")
        self.assertEqual(manifest.read_bytes(), token)
        self.assertEqual(sorted(path.name for path in manifest.parent.iterdir()), [change_page.BROWSER_MANIFEST])

    def git(self, *arguments: str) -> str:
        return subprocess.run(["git", "-C", str(self.repo), *arguments],
                              capture_output=True, text=True, check=True).stdout.strip()

    def test_an_open_owner_cell_uses_the_current_staff_production_without_a_list_write(self) -> None:
        doc = self.temporary / "destination.md"
        _ = doc.write_text("# Destination\n")
        owner = fixture_member("showrunner", name="destination", doc=staff.LiveDoc(local_machine(), doc))
        before = self.review_list.read_bytes()
        row = change_review.pending(self.review_list)[0]
        _ = staff.move(self.unit.id, staff.ReportsTo(owner.id))
        self.assertEqual(change_page.row_production(self.page, row), doc)
        self.assertEqual(self.review_list.read_bytes(), before)

    @override
    def setUp(self) -> None:
        self.set_up_review(changes=3, legacy_links=True)

    def set_up_review(self, *, changes: int, legacy_links: bool = False) -> None:
        self.temporary = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="change-page-test-")))
        _ = self.enterContext(patch.object(
            review, "review_question", return_value=review.QuestionAccepted("Fixture review passed.")))
        self.enterContext(patch.dict(os.environ, {"NOTIFY_STATE_DIR": str(self.temporary / "notify")}))
        if legacy_links:
            cache_home = self.temporary / "cache"
            seed_interpreter_cache(cache_home)
            self.enterContext(patch.dict(os.environ, {"XDG_CACHE_HOME": str(cache_home), "CLAUDE_PYTHON": ""}))
        self.root = self.temporary / "snapshots"
        configuration = self.temporary / "config"
        configuration.mkdir()
        for stem in ("change_review", "nix_env"):
            _ = write_config_fixture(configuration / f'{stem}.conf')
        self.write_delivery_config()
        _ = self.enterContext(patch.object(unrouted_alert, "send_message"))
        _ = self.enterContext(patch.object(reply_tokens, "platform", "linux"))
        self.enterContext(patch.dict(os.environ, {"CONF_DIR": str(configuration)}))
        self.repo = self.temporary / "repo"
        if legacy_links:
            _ = shutil.copytree(repository_seed(), self.repo)
        self.enterContext(patch.dict(os.environ, {
            "REVIEW_LINKS_ROOT": str(self.root), "REVIEW_LINKS_HOST": "127.0.0.1",
            "NEXT_STATE_DIR": str(self.temporary / "next"),
            "SCRATCH_ROOT": str(self.temporary / "scratch"),
            "SCRATCH_HOLDS": str(self.temporary / "holds.json"),
            "FEATURE_LOG_VAULT": str(self.temporary / "vault"),
            "XDG_STATE_HOME": str(self.temporary / "state"),
            "STAFF_FILE": str(self.temporary / "staff.json"),
            "SHOWRUNNER_STATE_DIR": str(self.temporary / "showrunner-state"),
            "REBUILD_NEEDS_DIR": str(self.temporary / "rebuild-needs"),
            "FOR_YOU_ACTIONS_DIR": str(self.temporary / "for-you-actions"),
            "USER_ZONE": "America/New_York",
        }))
        _ = self.enterContext(patch.object(showrunners, "NOTIFIER_STATE_DIR", self.temporary / "notifier"))
        _ = self.enterContext(patch.object(change_review, "lists_directory", return_value=self.temporary))
        if legacy_links:
            _ = fixture_member("showrunner", name="demo")
        if changes:
            _ = registered_fixture_image(self.root, "012345abcdef")
        item = change_review.Visible("Numbered moves", "Run moves list", "Each group starts at 1")
        merged = self.git("rev-parse", "HEAD") if legacy_links else ""
        # A production file is configuration: neither a recipient nor a checkout
        # comes from the submitted form.
        self.production = self.temporary / "demo.md"
        self.review_list = self.temporary / "change-review - page-owner.md"
        _ = self.production.write_text(f"""# Demo
## Production Context
- **Showrunner checkout:** `{self.repo}`
- **Showrunner session:** fallback-owner
- **Change review:** `{self.review_list}`
""", encoding="utf-8")
        machine = local_machine()
        document = staff.LiveDoc(machine, self.production)
        worktree = staff.PresentWorktree(machine, Path("/unused/feature"))
        if legacy_links:
            runner = fixture_member("showrunner", name="page-owner", doc=document)
            self.unit = fixture_member("unit", name="builder-unit", parent=runner.id, worktree=worktree)
        else:
            runner_id = staff.StaffId(fixture_runtime_id("showrunner::page-owner"))
            unit_id = staff.StaffId(fixture_runtime_id(f"unit:{runner_id}:builder-unit"))
            runner = staff.StaffRow(runner_id, "showrunner", "page-owner", staff.StaffRoot(),
                                    doc=document, current_session=runner_id)
            self.unit = staff.StaffRow(unit_id, "unit", "builder-unit", staff.ReportsTo(runner_id),
                                       worktree=worktree, current_session=unit_id, unit_type=staff.UnitDetails("direct"))
            _ = staff.record_path().write_text(staff.StaffSnapshot((runner, self.unit)).encode())
        self.showrunner_id = runner.id
        rows = [item, item, change_review.Visible(
            "Internal comments clarified", "Read the diff", "The old and new comments agree with the tests")][:changes]
        if rows:
            # The fixture list is outside the fixture repository.
            with patch.object(change_review, "_commit"):
                _ = change_review.append_rows(self.review_list, pictured_changes(rows), date(2026, 11, 1), merged,
                    self.production, producers=[self.unit.id] * changes, record=staff.snapshot())
                for row, kind in (("C1", "See in action"), ("C2", "See in action"), ("C3", "Read only"))[:changes]:
                    _ = change_review.set_row_kind(self.review_list, row, kind, self.production)
        if legacy_links:
            self.page_path = urlsplit(change_page.publish(self.production, self.review_list, ["C1", "C3"])).path
            self.page = change_page.page_at(self.root, self.page_path)
        self.messages = []
        self.raw_messages = []
        self.delivery_done = threading.Event()
        deliver = change_page.deliver

        def finish_delivery(delivery: change_page.MessageDelivery | change_page.NoMessageDelivery, status: change_page.DeliveryStatus) -> None:
            try:
                deliver(delivery, status)
            finally:
                self.delivery_done.set()

        _ = self.enterContext(patch.object(change_page, "deliver", side_effect=finish_delivery))
        _ = self.enterContext(patch.object(change_page, "send_message", side_effect=self.capture_message))
        _ = self.enterContext(patch.object(serve, "tailscale_ip", return_value="127.0.0.1"))
        if not legacy_links:
            return
        # Restart tests enable legacy recovery after they create the records.
        with (patch.object(change_page, "recover_pending"),
              patch.object(serve, "loaded_module_stamps", return_value={})):
            server = self.enterContext(started_review_server(("127.0.0.1", 0), self.root))
        self.port = server.server_port
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        self.addCleanup(thread.join, TEST_HANG_GUARD_SECS)
        self.addCleanup(server.shutdown)

    def write_delivery_config(self, tries: int = 3, pause: int = 0) -> None:
        _ = write_config_fixture(self.temporary / "config/review_links.conf",
                                 f"delivery_tries={tries}\ndelivery_pause_seconds={pause}\nfeedback_rows=5\n"
                                 + "changes_first_root=natedev\nunrouted_alert_retry_seconds=60\npage_refresh_seconds=15\n"
                                 # Undo is off, so each send saves at once; test_send_hold and test_serve check the wait.
                                 + "undo_seconds=0\n")

    def fail_all_but_alerts(self, calls: list[str]) -> Callable[[str, str, str], None]:
        def send(recipient: str, sender: str, message: str, **_options: str) -> None:
            calls.append(message)
            if "delivery needs attention" not in message:
                raise RuntimeError("socket unavailable")
            self.capture_message(recipient, sender, message)
        return send

    def assert_record_cleared(self, row: str = "C1") -> None:
        self.assertFalse(change_page.DeliveryStatus(self.root).path(self.page, row).exists())

    def assert_no_delivery_controls(self, content: str) -> None:
        for removed in ("Message needs attention:", "The message may not have sent",
                        "Saved, but the message did not send", "The question message did not send",
                        "The validation message did not send", "Send saved answer again",
                        "Send question again", "Send validation request again", "data-resend", 'value="retry"'):
            self.assertNotIn(removed, content)

    def test_recovery_does_not_erase_a_live_request_before_its_verdict_saves(self) -> None:
        status = change_page.DeliveryStatus(self.root)
        began, release = threading.Event(), threading.Event()
        begin = status.begin
        def pause_begin(page: change_page.ReviewPage, row: str, kind: Literal["verdict", "validation"],
                        feedback: str = "", *, recipients: tuple[staff.StaffId, ...] = (), user_verdict: bool = False) -> str:
            attempt = begin(page, row, kind, feedback, recipients=recipients, user_verdict=user_verdict)
            began.set()
            if not release.wait(TEST_HANG_GUARD_SECS):
                raise RuntimeError("test did not release the save")
            return attempt
        def save() -> None:
            delivery = change_page.submit(self.page, urlencode(self.answer()), status)
            change_page.deliver(delivery, status)
        with patch.object(status, "begin", side_effect=pause_begin), ThreadPoolExecutor(max_workers=1) as executor:
            result = executor.submit(save)
            try:
                self.assertTrue(began.wait(TEST_HANG_GUARD_SECS))
                before = status.path(self.page, "C1").read_bytes()
                change_page.recover_pending(status)
                self.assertEqual(status.path(self.page, "C1").read_bytes(), before)
                self.assertIsInstance(status.slots(self.page, "C1").verdict, change_page.DeliveryIntent)
            finally:
                release.set()
            result.result(TEST_HANG_GUARD_SECS)
        self.assertEqual(len(self.messages), 1)
        self.assertIsInstance(status.slots(self.page, "C1").verdict, change_page.NoDelivery)
        self.assertEqual(change_page.answer_row(self.page, "C1")["Verdict"], "works")

    def test_a_live_request_and_recovery_send_the_same_row_once_per_recipient(self) -> None:
        self.root = self.root / "ownership"
        status = change_page.DeliveryStatus(self.root)
        delivery = change_page.submit(self.page, urlencode(self.answer(outcome="not works")), status)
        assert isinstance(delivery, change_page.MessageDelivery)
        started, release = threading.Event(), threading.Event()
        recipients: list[staff.StaffId] = []

        def send(target: staff.StaffId, sender: str, message: str, **_options: str) -> None:
            del sender, message
            recipients.append(target)
            started.set()
            if not release.wait(10):
                raise RuntimeError("test did not release the send")

        with patch.object(change_page, "send_to_staff", side_effect=send), ThreadPoolExecutor(max_workers=1) as executor:
            sent = executor.submit(change_page.deliver, delivery, status)
            try:
                self.assertTrue(started.wait(10))
                change_page.recover_pending(status)
                self.assertEqual(recipients, [delivery.recipients[0]])
            finally:
                release.set()
            sent.result(10)
        self.assertCountEqual(recipients, delivery.recipients)
        self.assertEqual(len(recipients), 2)
        self.assertNotIn(delivery.attempt, status.request_attempts)
        self.assert_record_cleared()

    def test_a_live_request_and_recovery_send_different_rows_once_each(self) -> None:
        self.root = self.root / "ownership"
        status = change_page.DeliveryStatus(self.root)
        delivery = change_page.submit(self.page, urlencode(self.answer(outcome="not works")), status)
        assert isinstance(delivery, change_page.MessageDelivery)
        previous = change_page.DeliveryStatus(self.root)
        recovered = previous.begin(self.page, "C3", "validation", "Check C3", recipients=delivery.alert_recipients)
        started, release = threading.Event(), threading.Event()
        messages: list[tuple[staff.StaffId, str]] = []

        def send(target: staff.StaffId, sender: str, message: str, **_options: str) -> None:
            del sender
            messages.append((target, message))
            if "Change review page-owner/C1:" in message:
                started.set()
                if not release.wait(10):
                    raise RuntimeError("test did not release the send")

        with patch.object(change_page, "send_to_staff", side_effect=send), ThreadPoolExecutor(max_workers=1) as executor:
            sent = executor.submit(change_page.deliver, delivery, status)
            try:
                self.assertTrue(started.wait(10))
                change_page.recover_pending(status)
                self.assertEqual([target for target, message in messages if "Change review page-owner/C3:" in message],
                                 list(delivery.alert_recipients))
            finally:
                release.set()
            sent.result(10)
        self.assertCountEqual([target for target, message in messages if "Change review page-owner/C1:" in message],
                              delivery.recipients)
        self.assertIsInstance(status.slots(self.page, "C3").validation, change_page.ValidationRequested)
        self.assertNotIn(delivery.attempt, status.request_attempts)
        self.assertNotIn(recovered, status.request_attempts)

    def test_concurrent_recovery_claims_every_intent_before_routing_and_sends_each_once(self) -> None:
        self.root = self.root / "ownership"
        previous = change_page.DeliveryStatus(self.root)
        runner = staff.snapshot().by_path("page-owner")
        assert runner is not None
        question = previous.ask(self.page, "C1", "Which groups?", recipients=(runner.id,))
        assert isinstance(question, change_page.QuestionDelivery)
        validation = previous.begin(self.page, "C1", "validation", "Check C1", recipients=(runner.id,))
        status = change_page.DeliveryStatus(self.root)
        started, release = threading.Event(), threading.Event()
        assign = change_routing.StaffRouting.owner_and_production

        def pause_assignment(router: change_routing.StaffRouting, listing: Path,
                             row: dict[str, str]) -> change_routing.ChangeAssignment:
            started.set()
            if not release.wait(10):
                raise RuntimeError("test did not release routing")
            return assign(router, listing, row)

        with patch.object(change_routing.StaffRouting, "owner_and_production", autospec=True,
                          side_effect=pause_assignment) as routed, ThreadPoolExecutor(max_workers=1) as executor:
            first = executor.submit(change_page.recover_pending, status)
            try:
                self.assertTrue(started.wait(10))
                for attempt in (validation, question.attempt):
                    self.assertIn(attempt, status.request_attempts)
                    self.assertEqual(status.request_attempts[attempt].state, "claimed")
                change_page.recover_pending(status)
                self.assertEqual(routed.call_count, 1)
                self.assertEqual(self.messages, [])
            finally:
                release.set()
            first.result(10)
        self.assertEqual(len(self.messages), 2)
        self.assertEqual(sum("the user asks you to validate this" in message for _, _, message in self.messages), 1)
        self.assertEqual(sum("the user asks a question" in message for _, _, message in self.messages), 1)
        self.assertEqual(status.request_attempts, {})

    def test_duplicate_deliver_calls_send_once_with_or_without_a_request_claim(self) -> None:
        self.root = self.root / "ownership"
        runner = staff.snapshot().by_path("page-owner")
        assert runner is not None
        for claimed in (True, False):
            with self.subTest(claimed=claimed):
                previous = change_page.DeliveryStatus(self.root)
                attempt = previous.begin(self.page, "C1", "verdict", recipients=(runner.id,))
                status = previous if claimed else change_page.DeliveryStatus(self.root)
                delivery = change_page.MessageDelivery(self.page, "C1", (runner.id,), "Saved answer", attempt)
                started, release = threading.Event(), threading.Event()
                recipients: list[staff.StaffId] = []

                def send(target: staff.StaffId, sender: str, message: str, **_options: str) -> None:
                    del sender, message
                    recipients.append(target)
                    started.set()
                    if not release.wait(10):
                        raise RuntimeError("test did not release the send")

                with patch.object(change_page, "send_to_staff", side_effect=send), ThreadPoolExecutor(max_workers=1) as executor:
                    first = executor.submit(change_page.deliver, delivery, status)
                    try:
                        self.assertTrue(started.wait(10))
                        change_page.deliver(delivery, status)
                        self.assertEqual(recipients, [runner.id])
                    finally:
                        release.set()
                    first.result(10)
                    change_page.deliver(delivery, status)
                self.assertEqual(recipients, [runner.id])
                self.assertNotIn(attempt, status.request_attempts)
                self.assert_record_cleared()

    def test_completed_and_cleared_attempts_release_the_owner_for_a_new_send(self) -> None:
        self.root = self.root / "ownership"
        status = change_page.DeliveryStatus(self.root)
        runner = staff.snapshot().by_path("page-owner")
        assert runner is not None
        for index in range(2):
            attempt = status.begin(self.page, "C1", "verdict", recipients=(runner.id,))
            delivery = change_page.MessageDelivery(self.page, "C1", (runner.id,), "Saved answer", attempt)
            change_page.deliver(delivery, status)
            self.assertNotIn(attempt, status.request_attempts)
            self.assertEqual(len(self.messages), index + 1)
        recorded = status.begin(self.page, "C1", "verdict")
        status.record(change_page.MessageDelivery(self.page, "C1", (), "", recorded))
        self.assertNotIn(recorded, status.request_attempts)
        cleared = status.begin(self.page, "C1", "verdict")
        status.clear(self.page, "C1", "verdict")
        self.assertNotIn(cleared, status.request_attempts)
        question = status.ask(self.page, "C3", "Which groups?")
        assert isinstance(question, change_page.QuestionDelivery)
        with status.locked():
            status.clear_locked(self.page, "C3", status.slots(self.page, "C3"), "question")
        self.assertNotIn(question.attempt, status.request_attempts)
        self.assertEqual(status.request_attempts, {})
        self.assert_record_cleared()

    def test_deliver_releases_a_stale_claim_when_the_saved_intent_is_gone(self) -> None:
        self.root = self.root / "ownership"
        status = change_page.DeliveryStatus(self.root)
        attempt = status.begin(self.page, "C1", "verdict")
        status.path(self.page, "C1").unlink()
        change_page.deliver(change_page.MessageDelivery(self.page, "C1", (), "", attempt), status)
        self.assertNotIn(attempt, status.request_attempts)
        self.assertEqual(self.messages, [])

    def test_duplicate_delivery_keeps_the_sending_owner_when_the_record_disappears(self) -> None:
        self.root = self.root / "ownership"
        status = change_page.DeliveryStatus(self.root)
        runner = staff.snapshot().by_path("page-owner")
        assert runner is not None
        attempt = status.begin(self.page, "C1", "verdict", recipients=(runner.id,))
        delivery = change_page.MessageDelivery(self.page, "C1", (runner.id,), "Saved answer", attempt)
        started, release = threading.Event(), threading.Event()
        with patch.object(change_page, "send_to_staff") as sender, ThreadPoolExecutor(max_workers=1) as executor:
            def send(target: staff.StaffId, name: str, message: str, **_options: str) -> None:
                del target, name, message
                started.set()
                if not release.wait(10):
                    raise RuntimeError("test did not release the send")
            sender.side_effect = send
            first = executor.submit(change_page.deliver, delivery, status)
            try:
                self.assertTrue(started.wait(10))
                status.path(self.page, "C1").unlink()
                change_page.deliver(delivery, status)
                self.assertIn(attempt, status.request_attempts)
                self.assertEqual(status.request_attempts[attempt].state, "sending")
                self.assertEqual(sender.call_count, 1)
            finally:
                release.set()
            first.result(10)
        self.assertNotIn(attempt, status.request_attempts)
        self.assertEqual(sender.call_count, 1)

    def test_replacing_then_clearing_a_verdict_releases_all_claims_and_allows_recovery(self) -> None:
        self.root = self.root / "ownership"
        status = change_page.DeliveryStatus(self.root)
        _ = status.begin(self.page, "C1", "verdict")
        _ = status.begin(self.page, "C1", "verdict")
        status.clear(self.page, "C1", "verdict")
        self.assertEqual(status.request_attempts, {})
        previous = change_page.DeliveryStatus(self.root)
        pending = previous.begin(self.page, "C1", "validation", "Check this", recipients=(self.showrunner_id,))
        change_page.recover_pending(status)
        change_page.recover_pending(status)
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(status.slots(self.page, "C1").validation, change_page.ValidationRequested(pending, "Check this"))
        self.assertEqual(status.request_attempts, {})

    def test_a_visible_replacement_releases_old_claims_when_directory_sync_fails(self) -> None:
        self.root = self.root / "ownership"
        status = change_page.DeliveryStatus(self.root)
        old = status.begin(self.page, "C1", "validation", "Old")
        with patch.object(atomic_file, "sync_folder", side_effect=OSError("directory sync failed")):
            with self.assertRaises(atomic_file.AtomicDurabilityUnconfirmed):
                _ = status.begin(self.page, "C1", "validation", "New", recipients=(self.showrunner_id,))
        self.assertNotIn(old, status.request_attempts)
        self.assertEqual(status.request_attempts, {})
        current = status.slots(self.page, "C1").validation
        assert isinstance(current, change_page.DeliveryIntent)
        self.assertEqual(current.feedback, "New")
        change_page.recover_pending(status)
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(status.slots(self.page, "C1").validation, change_page.ValidationRequested(current.attempt, "New"))

    def test_answering_an_undelivered_question_releases_its_claim_and_allows_recovery(self) -> None:
        self.root = self.root / "ownership"
        status = change_page.DeliveryStatus(self.root)
        question = status.ask(self.page, "C1", "Which groups?")
        assert isinstance(question, change_page.QuestionDelivery)
        assert isinstance(question, change_page.QuestionDelivery)
        self.assertTrue(status.answer_question(self.page, "C1", question.question_id, "All groups"))
        self.assertNotIn(question.attempt, status.request_attempts)
        previous = change_page.DeliveryStatus(self.root)
        pending = previous.begin(self.page, "C1", "validation", "Check this", recipients=(self.showrunner_id,))
        change_page.recover_pending(status)
        change_page.recover_pending(status)
        self.assertEqual(len(self.messages), 1)
        slots = status.slots(self.page, "C1")
        self.assertEqual(slots.validation, change_page.ValidationRequested(pending, "Check this"))
        self.assertEqual(slots.question, change_page.AnsweredQuestion(question.question_id, "Which groups?", "All groups"))
        self.assertEqual(status.request_attempts, {})

    def test_a_replaced_live_send_preserves_the_new_attempt_until_it_delivers_once(self) -> None:
        self.root = self.root / "ownership"
        status = change_page.DeliveryStatus(self.root)
        runner = staff.snapshot().by_path("page-owner")
        assert runner is not None
        old = status.begin(self.page, "C1", "validation", "Old", recipients=(runner.id,))
        started, release = threading.Event(), threading.Event()
        messages: list[str] = []

        def send(target: staff.StaffId, sender: str, message: str, **_options: str) -> None:
            del target, sender
            messages.append(message)
            if message == "Old":
                started.set()
                if not release.wait(10):
                    raise RuntimeError("test did not release the old send")

        with patch.object(change_page, "send_to_staff", side_effect=send), ThreadPoolExecutor(max_workers=1) as executor:
            first = executor.submit(change_page.deliver,
                change_page.MessageDelivery(self.page, "C1", (runner.id,), "Old", old), status)
            try:
                self.assertTrue(started.wait(10))
                new = status.begin(self.page, "C1", "validation", "New", recipients=(runner.id,))
                self.assertNotIn(old, status.request_attempts)
                self.assertEqual(status.request_attempts[new].state, "claimed")
            finally:
                release.set()
            first.result(10)
            self.assertEqual(status.request_attempts[new].state, "claimed")
            current = change_page.MessageDelivery(self.page, "C1", (runner.id,), "New", new)
            change_page.deliver(current, status)
            change_page.deliver(current, status)
        self.assertEqual(messages, ["Old", "New"])
        self.assertEqual(status.slots(self.page, "C1").validation, change_page.ValidationRequested(new, "New"))
        self.assertEqual(status.request_attempts, {})

    def test_recovery_reads_no_staff_or_documents_for_a_completed_question(self) -> None:
        self.root = self.root / "ownership"
        status = change_page.DeliveryStatus(self.root)
        question = status.ask(self.page, "C1", "Which groups?")
        assert isinstance(question, change_page.QuestionDelivery)
        self.assertTrue(status.answer_question(self.page, "C1", question.question_id, "All groups"))
        with patch.object(change_page, "list_routing") as routing, patch.object(staff, "snapshot") as snapshot:
            change_page.recover_pending(status)
        routing.assert_not_called()
        snapshot.assert_not_called()
        self.assertEqual(self.messages, [])

    def test_an_unrouted_row_with_a_missing_alert_setting_still_renders(self) -> None:
        _ = self.enterContext(production_staff_record())
        _ = self.enterContext(patch.object(change_routing, "configured_lists_directory", return_value=self.temporary))
        _ = change_review.append_rows(self.review_list, pictured_changes([change_review.Visible("Conflict", "Read", "Proof")]),
                                      date(2026, 11, 2), "abcdef12", self.production, producers=[staff.StaffId("missing-producer")], record=staff.snapshot())
        config = self.temporary / "config/review_links.conf"
        _ = config.write_text("\n".join(line for line in config.read_text().splitlines()
                                       if line.partition("=")[0].strip() != "unrouted_alert_retry_seconds") + "\n")
        path = change_page.page_url()
        with redirect_stderr(StringIO()) as errors:
            reply = self.legacy_browser_reply(path)
        forms = BrowserForms()
        forms.feed(reply.body)
        self.assertEqual(reply.status, 200)
        self.assertEqual(forms.tabs["page-owner/C4"], "unrouted")
        self.assertIn("review-links unrouted alert:", errors.getvalue())

        key = self.file_user_question("Route an unknown owner")
        browser = change_page.ReviewBrowser("private-owner", kind="questions")
        status = change_page.DeliveryStatus(self.root)
        question = next(item for item in change_browser.prepare(browser, status).questions if item.key == key)
        _ = self.production.write_text(self.production.read_text() + f"- **Proposals:** `{question.record.path.parent}`\n")
        record = staff.snapshot()
        without_owner = staff.StaffSnapshot(tuple(row for row in record.rows if row.id != question.owner))
        with patch.object(staff, "snapshot", return_value=without_owner):
            prepared = change_browser.prepare(browser, status)
        self.assertEqual(next(item.tab for item in prepared.questions if item.key == key), "unrouted")

        with self.subTest(owner="absent without Proposals"):
            _ = self.production.write_text("\n".join(line for line in self.production.read_text().splitlines()
                                                    if not line.startswith("- **Proposals:**")) + "\n")
            with patch.object(staff, "snapshot", return_value=without_owner):
                prepared = change_browser.prepare(browser, status)
                body = change_browser.render(browser, status).decode()
                forms = BrowserForms()
                forms.feed(body)
                self.assertIn(key, {item.key for item in prepared.questions})
                self.assertEqual(next(item.tab for item in prepared.questions if item.key == key), "unrouted")
                self.assertEqual(forms.question_counts["unrouted"], 1)
                self.assertIn('Owner unavailable (1)', body)
                self.assertEqual(forms.outcomes[key], [])
                self.assertNotIn(key, forms.save_urls)
                self.assertIn("No showrunner owns this question.", body)
                self.assertIn("Which layout gives each review card enough space for its controls?", body)
                fields = {"row": key, "token": change_browser.row_token(browser, key),
                          "outcome": "answer", "feedback": "Use compact"}
                with self.assertRaisesRegex(change_page.QuestionAnswerRefused, "No showrunner owns"):
                    _ = change_browser.submit(browser, urlencode(fields), status)

    def test_recovery_clears_a_true_orphan_without_saving_or_sending_a_verdict(self) -> None:
        status = change_page.DeliveryStatus(self.root)
        _ = status.begin(self.page, "C1", "verdict")
        before = self.review_list.read_bytes()
        restarted = change_page.DeliveryStatus(self.root)
        change_page.recover_pending(restarted)
        self.assertEqual(self.review_list.read_bytes(), before)
        self.assertEqual(self.messages, [])
        self.assertIsInstance(restarted.slots(self.page, "C1").verdict, change_page.NoDelivery)

    def test_a_send_that_fails_once_uses_two_tries_and_clears_its_record_without_an_alert(self) -> None:
        calls: list[str] = []
        def send(recipient: str, sender: str, message: str, **_options: str) -> None:
            calls.append(message)
            if len(calls) == 1:
                raise RuntimeError("socket unavailable")
            self.capture_message(recipient, sender, message)
        with patch.object(change_page, "send_message", side_effect=send):
            self.assertEqual(self.request(self.page_path, self.answer()).status, 303)
        self.assertEqual(len(calls), 2)
        self.assertEqual([target for target, _, _ in self.messages], ["page-owner"])
        self.assertTrue(all("delivery needs attention" not in message for message in calls))
        self.assert_record_cleared()

    def test_a_failed_showrunner_send_keeps_the_saved_row_out_of_the_page_and_footer(self) -> None:
        calls: list[str] = []
        with (patch.object(change_page, "send_message", side_effect=self.fail_all_but_alerts(calls)),
              redirect_stderr(StringIO()) as errors):
            self.assertEqual(self.request(self.page_path, self.answer()).status, 303)
        self.assertEqual(len(calls), 3)
        self.assertEqual(self.messages, [])
        self.assertIn("socket unavailable; no separate showrunner for the alert", errors.getvalue())
        self.assert_record_cleared()
        path, forms, content = self.browser()
        del path
        self.assertEqual(forms.counts["page-owner"], 2)
        self.assertEqual(footer_counts.changes(self.production), forms.counts["page-owner"])
        self.assert_no_delivery_controls(content)

    def test_submission_message_recovery_reuses_the_claim_and_receipt(self) -> None:
        recipient = fixture_member('producer', name='producer')
        _ = write_config_fixture(conf.path("review_links"), "unrouted_alert_recipient=page-owner\n")
        runner = showrunners.owner_for("page-owner")
        kind = new_item_dialog.SubmissionKind.BUG
        request = new_item_dialog.NewItemRequest(f'r1-{uuid4().hex}-{kind}-{recipient.id}', kind,
                                                recipient.id, 'Check the build.', new_item_dialog.NoEvidence())
        native = review_submissions.submit(request)
        status = change_page.DeliveryStatus(self.root)
        first = review_actions.prepare_submission_delivery(native, request.words, status)
        assert isinstance(first, change_page.MessageDelivery)
        with self.assertRaisesRegex(review_actions.SubmissionDeliveryPending, "still sending"):
            _ = review_actions.prepare_submission_delivery(native, request.words, status)
        command = change_page.reply_command(native.key)
        self.assertIn(command, first.message)
        sent_message = first.message.removesuffix(command) + change_page.reply_command(native.key, native.reply_token)
        self.assertNotIn(native.reply_token, native.path.read_text(encoding="utf-8"))
        self.assertNotIn(native.reply_token, status.path(first.page, first.row).read_text(encoding="utf-8"))
        with patch.object(change_page, 'send_with_retries', return_value=[
            change_page.DeliveryFailure(recipient.id, 'The fixture sender is unavailable.')
        ]), patch.object(change_page, 'send_to_staff') as alert:
            change_page.deliver(first, status)
        alert.assert_called_once()
        self.assertEqual(alert.call_args.args[0], runner.id)
        self.assertIn('The fixture sender is unavailable.', cast(str, alert.call_args.args[2]))
        retried = review_actions.prepare_submission_delivery(native, request.words, status)
        assert isinstance(retried, change_page.MessageDelivery)
        self.assertEqual(retried.attempt, first.attempt)
        restarted = change_page.DeliveryStatus(self.root)
        with patch.object(change_page, 'send_to_staff') as send:
            with patch.object(restarted, 'write_slots', side_effect=OSError('Cannot write the delivery receipt.')):
                change_page.recover_review_messages(restarted)
            change_page.recover_review_messages(change_page.DeliveryStatus(self.root))
            send.assert_called_once_with(recipient.id, change_page.SENDER, sent_message,
                key=change_page.review_delivery_key(first.page.review_list, first.row, first.attempt))
        self.assertNotIn(native.reply_token, status.path(first.page, first.row).read_text(encoding="utf-8"))
        self.assertNotIn(native.reply_token, status.closeout_path(first.page, first.row).read_text(encoding="utf-8"))
        self.assertIsInstance(review_actions.prepare_submission_delivery(native, request.words, restarted),
                              change_page.NoMessageDelivery)
        self.assertTrue(review_submissions.answer(native.key, 'The check passed.', native.reply_token))
        self.assertIsInstance(review_actions.prepare_submission_delivery(
            review_submissions.submit(request), request.words, restarted), change_page.NoMessageDelivery)

    def test_delivery_failure_survives_restart_until_the_same_row_succeeds(self) -> None:
        status = change_page.DeliveryStatus(self.root)
        recipient = staff.StaffId('fixture-recipient')
        attempt = status.begin(self.page, 'C1', 'verdict', recipients=(recipient,))
        delivery = change_page.MessageDelivery(self.page, 'C1', (recipient,), 'Saved answer', attempt)
        with patch.object(change_page, 'send_to_staff', side_effect=RuntimeError('owner offline')):
            change_page.deliver(delivery, status)
        expected = change_page.FailedDelivery(attempt, self.page.review_list, 'C1', 'owner offline')
        restarted = change_page.DeliveryStatus(self.root)
        self.assertEqual(restarted.failed_delivery(self.page, 'C1'), expected)
        self.assert_record_cleared()
        # A repeated completion cannot erase the failure from its finished attempt.
        restarted.record(delivery)
        self.assertEqual(restarted.failed_delivery(self.page, 'C1'), expected)
        with patch.object(change_page, 'send_to_staff') as sender:
            restarted.notify_closeout_failure(self.page, 'C1', 'Earlier alert', (recipient,))
            other_attempt = restarted.begin(self.page, 'C3', 'verdict', recipients=(recipient,))
            change_page.deliver(replace(delivery, row='C3', attempt=other_attempt), restarted)
            self.assertEqual(restarted.failed_delivery(self.page, 'C1'), expected)
            retry = restarted.begin(self.page, 'C1', 'verdict', recipients=(recipient,))
            change_page.deliver(replace(delivery, attempt=retry), restarted)
            restarted.notify_closeout_failure(self.page, 'C1', 'Earlier alert', (recipient,))
        self.assertEqual(sender.call_count, 3)
        self.assertIsInstance(change_page.DeliveryStatus(self.root).failed_delivery(self.page, 'C1'),
                              change_page.NoFailedDelivery)
        self.assert_record_cleared()

    def test_a_late_failure_cannot_replace_a_newer_attempts_result(self) -> None:
        status = change_page.DeliveryStatus(self.root)
        old = status.begin(self.page, 'C1', 'verdict')
        current = status.begin(self.page, 'C1', 'verdict')
        delivery = change_page.MessageDelivery(self.page, 'C1', (), 'Saved answer', current)
        failed = change_page.FailedDelivery(current, self.page.review_list, 'C1', 'current error')
        status.record(delivery, failed)
        status.record(replace(delivery, attempt=old),
                      change_page.FailedDelivery(old, self.page.review_list, 'C1', 'old error'))
        self.assertEqual(status.failed_delivery(self.page, 'C1'), failed)

    def test_queued_and_unconfirmed_sends_finish_without_an_alert(self) -> None:
        runner = staff.snapshot().by_path("page-owner")
        assert runner is not None
        status = change_page.DeliveryStatus(self.root)
        for code, output in ((1, "QUEUED: recipient absent"), (5, "PENDING: delivery unconfirmed")):
            with self.subTest(output=output):
                result = subprocess.CompletedProcess([], code, output, "")
                with (patch.object(change_page, "send_message", new=SEND_MESSAGE),
                      patch.object(subprocess, "run", return_value=result) as run):
                    failures = change_page.send_with_retries("fixture", (runner.id,), "Saved answer", 3, 0, key="review:fixture")
                    self.assertEqual(failures, [])
                    run.assert_called_once()
                    run.reset_mock()
                    attempt = status.begin(self.page, "C1", "verdict", recipients=(runner.id,))
                    delivery = change_page.MessageDelivery(self.page, "C1", (runner.id,), "Saved answer", attempt,
                                                          (runner.id,))
                    change_page.deliver(delivery, status)
                    run.assert_called_once()
                self.assert_record_cleared()
                self.assertIsInstance(status.failed_delivery(self.page, "C1"), change_page.NoFailedDelivery)

    def test_server_start_delivers_a_legacy_pending_verdict_without_page_context(self) -> None:
        self.assertEqual(self.request(self.page_path, self.answer()).status, 303)
        status = change_page.DeliveryStatus(self.root)
        _ = status.path(self.page, "C1").write_text(json.dumps({
            "attempt": "legacy", "state": "pending", "kind": "verdict"}))
        self.messages.clear()
        self.delivery_done.clear()
        with started_review_server(("127.0.0.1", 0), self.root):
            self.assertTrue(self.delivery_done.is_set())
        self.assertEqual([target for target, _, _ in self.messages], ["page-owner"])
        self.assertIn("C1: works", self.messages[0][2])
        self.assert_record_cleared()

    def test_restart_finds_pending_records_without_page_context_and_reads_each_list_once(self) -> None:
        status = change_page.DeliveryStatus(self.root)
        for row in ("C1", "C3"):
            _ = status.begin(self.page, row, "validation", "Check this change", recipients=(self.showrunner_id,))
            fields = cast(dict[str, object], json.loads(status.path(self.page, row).read_text()))
            self.assertNotIn("page", fields)
        with patch.object(change_review, "review_snapshot", wraps=change_review.review_snapshot) as read:
            change_page.recover_pending(change_page.DeliveryStatus(self.root))
        self.assertEqual(read.call_count, 1)
        self.assertEqual(read.call_args.args, (self.review_list,))
        self.assertEqual(len(self.messages), 2)
        for row in ("C1", "C3"):
            self.assertEqual(sum(f"Change review page-owner/{row}:" in message for _, _, message in self.messages), 1)
            self.assertIsInstance(status.slots(self.page, row).validation, change_page.ValidationRequested)
        before = self.review_list.read_bytes()
        change_page.recover_pending(change_page.DeliveryStatus(self.root))
        self.assertEqual(len(self.messages), 2)
        self.assertEqual(self.review_list.read_bytes(), before)

    def test_restart_ignores_a_legacy_records_stored_page_context(self) -> None:
        status = change_page.DeliveryStatus(self.root)
        _ = status.begin(self.page, "C1", "validation", "Keep these words", recipients=(self.showrunner_id,))
        path = status.path(self.page, "C1")
        fields = cast(dict[str, object], json.loads(path.read_text()))
        fields["page"] = {"review_list": "/missing-list", "production": "/wrong-production", "row": "C999"}
        _ = path.write_text(json.dumps(fields))
        before = self.review_list.read_bytes()
        restarted = change_page.DeliveryStatus(self.root)
        change_page.recover_pending(restarted)
        change_page.recover_pending(restarted)
        self.assertEqual(len(self.messages), 1)
        self.assertIn("Change review page-owner/C1: the user asks you to validate this", self.messages[0][2])
        self.assertIn(f"List: {self.review_list}", self.messages[0][2])
        self.assertIn("Production: page-owner", self.messages[0][2])
        self.assertIn("User feedback: Keep these words", self.messages[0][2])
        self.assertNotIn("/wrong-production", self.messages[0][2])
        self.assertNotIn("page", cast(dict[str, object], json.loads(path.read_text())))
        self.assertEqual(self.review_list.read_bytes(), before)
        self.assertIsInstance(status.slots(self.page, "C1").validation, change_page.ValidationRequested)

    def test_recovered_messages_use_the_same_owner_production_as_live_messages(self) -> None:
        doc = self.temporary / "current-owner-production.md"
        _ = doc.write_text(self.production.read_text())
        runner = fixture_member("showrunner", name="current-owner", doc=staff.LiveDoc(local_machine(), doc))
        _ = staff.move(self.unit.id, staff.ReportsTo(runner.id))
        status = change_page.DeliveryStatus(self.root)
        delivery = change_page.submit(self.page, urlencode(self.answer()), status)
        change_page.deliver(delivery, status)
        self.assertEqual(len(self.messages), 1)
        _ = status.begin(self.page, "C1", "verdict", recipients=(runner.id,))
        change_page.recover_pending(change_page.DeliveryStatus(self.root))
        self.assertEqual(len(self.messages), 2)
        production_lines = [next(line for line in message.splitlines() if line.startswith("Production:"))
                            for _, _, message in self.messages]
        self.assertEqual(production_lines, ["Production: current-owner"] * 2)
        self.assertEqual([recipient for recipient, _, _ in self.messages], ["current-owner"] * 2)
        self.assert_record_cleared()

    def test_browser_and_recovery_use_the_same_owner_and_production_function(self) -> None:
        doc = self.temporary / "current-owner-production.md"
        _ = doc.write_text(self.production.read_text())
        runner = fixture_member("showrunner", name="current-owner", doc=staff.LiveDoc(local_machine(), doc))
        _ = staff.move(self.unit.id, staff.ReportsTo(runner.id))
        assignments: list[change_routing.ChangeAssignment] = []
        resolve = change_routing.StaffRouting.owner_and_production

        def capture(routing: change_routing.StaffRouting, listing: Path, row: dict[str, str]) -> change_routing.ChangeAssignment:
            assignment = resolve(routing, listing, row)
            if listing == self.review_list and row["#"] == "C1":
                assignments.append(assignment)
            return assignment

        status = change_page.DeliveryStatus(self.root)
        _ = status.begin(self.page, "C1", "validation", "Check the proof", recipients=(runner.id,))
        with patch.object(change_routing.StaffRouting, "owner_and_production", autospec=True, side_effect=capture):
            prepared = change_browser.prepare(change_page.ReviewBrowser("token"), status)
            change_page.recover_pending(change_page.DeliveryStatus(self.root))
        change = next(item for item in prepared.changes if item.key == "page-owner/C1")
        self.assertIsInstance(change.page, change_page.AssignedReviewPage)
        assert isinstance(change.page, change_page.AssignedReviewPage)
        self.assertEqual(assignments, [change_routing.ChangeAssignment(change.page.owner, change.page.production)] * 2)
        self.assertEqual(change.page.owner.showrunner_recipients, (runner.id,))
        self.assertEqual(change.page.production, doc)
        self.assertEqual(len(self.messages), 1)
        self.assertIn("Production: current-owner", self.messages[0][2])

    def test_a_row_without_a_staff_unit_uses_its_list_showrunner_and_production(self) -> None:
        row = change_page.answer_row(self.page, "C1") | {"Producer": "", "Merged": "", "Production": "another-production"}
        routing = change_page.list_routing({self.review_list: change_review.review_snapshot(self.review_list)})
        assignment = routing.owner_and_production(self.review_list, row)
        runner = staff.snapshot().by_path("page-owner")
        assert runner is not None
        self.assertEqual(assignment.owner, change_routing.ShowrunnerOwnChange(runner.id, "page-owner"))
        self.assertEqual(assignment.production, self.production)

    def test_two_orphan_answers_save_and_reach_the_root_after_the_first_stamps_its_document(self) -> None:
        listing = self.temporary / "change-review - orphan.md"
        missing = self.temporary / "wrapped-production.md"
        _ = listing.write_text("\n".join((
            "---", "productions:", f'  - "{missing}"', "---", "## To review", "",
            "| # | Change | Merged | How to check |",
            "| --- | --- | --- | --- |",
            "| C1 | First orphan | | Read the first change |",
            "| C2 | Second orphan | | Read the second change |", "")))
        settings = Path(os.environ["CONF_DIR"]) / "review_links.conf"
        _ = write_config_fixture(settings, "unrouted_alert_recipient=page-owner\n")
        runner = staff.snapshot().by_path("page-owner")
        assert runner is not None
        browser = change_page.ReviewBrowser("orphan-token")
        status = change_page.DeliveryStatus(self.root)
        for number in (1, 2):
            with self.subTest(row=number):
                prepared = change_browser.prepare(browser, status)
                change = next(item for item in prepared.changes if item.key == f"orphan/C{number}")
                page = change.page
                self.assertIsInstance(page, change_page.AssignedReviewPage)
                assert isinstance(page, change_page.AssignedReviewPage)
                self.assertEqual(page.owner, change_routing.ShowrunnerOwnChange(runner.id, runner.name))
                self.assertEqual(change.tab, runner.name)
                self.assertEqual(page.production, self.production)
                self.assertEqual(page.owner.showrunner_recipients, (runner.id,))
                delivery = change_page.submit(page, urlencode({"row": f"C{number}", "token": page.token,
                    "outcome": "works", "feedback": ""}), status)
                self.assertIsInstance(delivery, change_page.MessageDelivery)
                assert isinstance(delivery, change_page.MessageDelivery)
                self.assertEqual(delivery.recipients, (runner.id,))
                change_page.deliver(delivery, status)
                self.assertEqual(change_review.review_snapshot(listing).reviewed[-1]["Verdict"], "works")
                self.assertIn(self.production, change_routing.list_documents(listing.read_text()))
                self.assertEqual([recipient for recipient, _, _ in self.messages], [runner.name] * number)
                self.assertIn(f"Change review page-owner/C{number}: works", self.messages[-1][2])
        self.assertEqual({row["#"]: row["Verdict"] for row in change_review.review_snapshot(listing).reviewed},
                         {"C1": "works", "C2": "works"})

    def test_new_delivery_records_never_store_page_context(self) -> None:
        status = change_page.DeliveryStatus(self.root)
        verdict = status.begin(self.page, "C1", "verdict")
        validation = status.begin(self.page, "C2", "validation", "Keep this feedback")
        question = status.ask(self.page, "C3", "Which comments?")
        assert isinstance(question, change_page.QuestionDelivery)
        for row, attempt in (("C1", verdict), ("C2", validation), ("C3", question.attempt)):
            with self.subTest(row=row):
                fields = cast(dict[str, object], json.loads(status.path(self.page, row).read_text()))
                self.assertNotIn("page", fields)
                self.assertEqual(status.slots(self.page, row).intents()[0].attempt, attempt)
        status.record(change_page.MessageDelivery(self.page, "C2", (), "", validation))
        self.assertIsInstance(status.slots(self.page, "C2").validation, change_page.ValidationRequested)
        self.assertNotIn("page", cast(dict[str, object], json.loads(status.path(self.page, "C2").read_text())))
        self.assertTrue(status.answer_question(self.page, "C3", question.question_id, "The comments below the change"))
        self.assertEqual(status.slots(self.page, "C3").question,
                         change_page.AnsweredQuestion(question.question_id, question.text, "The comments below the change"))
        self.assertNotIn("page", cast(dict[str, object], json.loads(status.path(self.page, "C3").read_text())))

    def test_delivery_uses_the_live_config_budget_and_pause(self) -> None:
        self.write_delivery_config(tries=2)
        calls: list[str] = []
        with patch.object(change_page, "send_message", side_effect=self.fail_all_but_alerts(calls)), patch.object(time, "sleep") as pause:
            self.assertEqual(self.request(self.page_path, self.answer()).status, 303)
        self.assertEqual(len(calls), 2)
        pause.assert_called_once_with(0)

    def test_pending_and_legacy_failed_records_have_no_delivery_controls_on_either_page(self) -> None:
        path, forms, body = self.browser()
        del forms, body
        status = change_page.DeliveryStatus(self.root)
        _ = change_review.record_verdict(self.review_list, "C1", "works", date(2026, 11, 2), self.production)
        for state in ("pending", "failed"):
            with self.subTest(state=state):
                _ = status.path(self.page, "C1").write_text(json.dumps({"verdict": {"attempt": "old", "state": state}}))
                self.assert_no_delivery_controls(self.legacy_browser_reply(path).body)
                content = self.request(self.page_path, {}).body
                self.assertIn("Saved: works", content)
                self.assert_no_delivery_controls(content)

    def test_every_message_kind_resolves_a_unit_staff_id_to_its_short_live_session(self) -> None:
        previous_session = staff.native_id(fixture_runtime_id("resources-previous"))
        new_session = staff.native_id(fixture_runtime_id("resources"))
        runner = fixture_member("showrunner", name="resources-unit", runtime_id=str(previous_session))
        owner = change_routing.StaffRoutedChange(self.unit.id, runner.id, runner.name)
        status = change_page.DeliveryStatus(self.root)
        def send_after_account_move(recipient: str, sender: str, message: str, **_options: str) -> None:
            if recipient == f"session:{previous_session}":
                _ = staff.update_current_session(previous_session, new_session)
                raise RuntimeError("the account moved")
            self.capture_message(recipient, sender, message)
        for row, outcome, feedback in (("C1", "not works", ""), ("C2", "validate", "Check"), ("C3", "question", "Why?")):
            with self.subTest(outcome=outcome), patch.object(change_page, "send_message", side_effect=send_after_account_move):
                current = staff.snapshot().by_id(runner.id)
                assert current is not None
                _ = staff.update_current_session(current.current_session, previous_session)
                page = change_page.AssignedReviewPage(self.review_list, self.production, (row,), self.page.token, owner)
                delivery = change_page.submit(page, urlencode(self.answer(row, outcome, feedback)), status)
                change_page.deliver(delivery, status)
        self.assertEqual(sum(recipient == f"session:{new_session}" for recipient, _, _ in self.raw_messages), 3)
        for detail in ("not works", "validate this", "user asks a question"):
            self.assertEqual(sum(detail in message for target, _, message in self.raw_messages
                                 if target == f"session:{new_session}"), 1)
        self.assertTrue(all(target != "resources-unit" and target != "session:resources-unit"
                            for target, _, _ in self.raw_messages))

    def recipient_name(self, recipient: str) -> str:
        if recipient.startswith("session:"):
            member = staff.snapshot().by_current_session(recipient.removeprefix("session:"))
            if member is None:
                raise AssertionError("the recipient must name a current staff session")
            return member.name
        return recipient

    def capture_message(self, recipient: str, sender: str, message: str, **_options: str) -> None:
        self.raw_messages.append((recipient, sender, message))
        self.messages.append((self.recipient_name(recipient), sender, message))

    def request(self, path: str, fields: dict[str, str]) -> Reply:
        self.delivery_done.clear()
        reply = self.http_request(path, fields)
        if fields and reply.status == 303 and fields.get("outcome") != "next":
            self.assertTrue(self.delivery_done.wait(TEST_HANG_GUARD_SECS), "message delivery did not finish")
        return reply

    def http_request(self, path: str, fields: dict[str, str]) -> Reply:
        connection = HTTPConnection("127.0.0.1", self.port, timeout=TEST_HANG_GUARD_SECS)
        try:
            if fields:
                connection.request("POST", path, urlencode(fields),
                                   {"Content-Type": "application/x-www-form-urlencoded"})
            else:
                connection.request("GET", path)
            response = connection.getresponse()
            return Reply(response.status, response.read().decode("utf-8"), response.getheader("Location") or "")
        finally:
            connection.close()

    def answer(self, row: str = "C1", outcome: str = "works", feedback: str = "") -> dict[str, str]:
        return {"row": row, "outcome": outcome, "feedback": feedback, "token": self.page.token}

    def test_change_image_markup_is_shared_by_both_pages_and_absent_without_an_image(self) -> None:
        # Model an existing legacy list before the image rule.
        text, reviewed = self.review_list.read_text().split(change_review.REVIEWED, 1)
        header = next(line for line in text.splitlines() if line.startswith("| # |"))
        image_column = header.split("|").index(f" {change_review.IMAGE} ")
        legacy: list[str] = []
        for line in text.splitlines(keepends=True):
            if line.startswith("|"):
                cells = line.split("|")
                del cells[image_column]
                line = "|".join(cells)
            legacy.append(line)
        _ = self.review_list.write_text("".join(legacy) + change_review.REVIEWED + reviewed)
        _, _, before = self.browser()
        self.assertNotIn('class="change-image"', before)
        shot = self.temporary / "shot.png"
        _ = shot.write_bytes(b"fixture image")
        identity = image_store.register_change(shot)
        text, reviewed = self.review_list.read_text().split(change_review.REVIEWED, 1)
        lines = text.splitlines(keepends=True)
        for index, line in enumerate(lines):
            if line.startswith("| # |"):
                lines[index] = line.rstrip() + f" {change_review.IMAGE} |\n"
            elif line.startswith("| ---"):
                lines[index] = line.rstrip() + " --- |\n"
            elif line.startswith("| C"):
                lines[index] = line.rstrip() + (f" {identity} |\n" if line.startswith("| C1 |") else "  |\n")
        _ = self.review_list.write_text("".join(lines) + change_review.REVIEWED + reviewed)
        _, _, browser = self.browser()
        page = change_page.render(self.page, change_page.DeliveryStatus(self.root)).decode()
        self.assertEqual(browser.count(page_frame.MOTION_SCRIPT), 1)
        self.assertIn("prefers-reduced-motion: reduce", browser)
        for body in (browser, page):
            self.assertEqual(body.count(page_frame.ZOOM_STYLE), 1)
            self.assertEqual(body.count(page_frame.ZOOM_SCRIPT), 1)
            self.assertIn('data-image-zoom class="change-image"', body)
            self.assertIn(f'href="/change-images/{identity}/image.png" target="_blank" rel="noopener"', body)
            self.assertIn('alt="Screenshot: Numbered moves"', body)
            self.assertIn('class="change-text"', body)
            self.assertEqual(body.count('class="change-image"'), 1)
        with patch.object(change_review, "end_row_scratch", return_value=[]):
            _ = change_page.submit(self.page, urlencode({"token": self.page.token, "row": "C1", "outcome": "works", "feedback": ""}),
                                   change_page.DeliveryStatus(self.root))
        self.assertFalse((self.root / image_store.CHANGE_FOLDER / identity).exists())

    def test_code_copy_controls_reach_command_items_and_shared_assets_reach_both_changes_pages(self) -> None:
        _ = self.file_user_question('Copy the command?', question='Copy the command?\n1. In a terminal:\n2. `git -C /etc/nixos diff`')
        question = self.browser('questions')[2]
        changes = self.browser()[2]
        selected = change_page.render(self.page, change_page.DeliveryStatus(self.root)).decode()
        self.assertIn('<pre><code>git -C /etc/nixos diff</code></pre>' + page_frame.COPY_BUTTON, question)
        self.assertEqual(question.count(page_frame.COPY_BUTTON), 1)
        for page in (changes, selected):
            self.assertIn('<p>Run moves list. You see: Each group starts at 1</p>', page)
            self.assertNotIn(page_frame.COPY_BUTTON, page)
        for page in (question, changes, selected):
            with self.subTest(title=page.split('<title>')[1].split('</title>')[0]):
                self.assertEqual(page.count(page_frame.COPY_SCRIPT), 1)
                self.assertEqual(page.count(page_frame.COPY_STYLE), 1)
                self.assertEqual(page.count(page_frame.COPY_BUTTON), page.count('<pre><code>'))

    def test_change_code_blocks_from_the_add_command_copy_exact_text_on_both_pages(self) -> None:
        code = '  printf \'%s\\n\' "copy check $HOME <tag> & value"\n\techo done | cat\n'
        before = 'Before:\n- Read the label.\n\n'
        after = '\n\nAfter:\n- Read the result.'
        for kind in ('See in action', 'Read only'):
            for name, block, expected in (
                ('fenced', '```sh\n' + code + '\n```', [code]),
                ('backtick line', '`echo "$HOME"`', ['echo "$HOME"']),
                ('plain', 'Run `echo` without a code block.', []),
            ):
                with self.subTest(kind=kind, block=name):
                    title = f'{kind}: {name}'
                    with patch.object(console_text, "require"):
                        result = self.command(change_review, change_review.main, [
                            'add', '--repo', str(self.repo), '--production', 'page-owner',
                            '--list', str(self.review_list), '--change', title,
                            '--check', before + block + after, '--see', 'The command stays exact', '--image', fixture_image(),
                        ])
                    self.assertEqual(result.returncode, 0, result.stderr)
                    row = next(row for row in change_review.pending(self.review_list) if row['Change'] == title)
                    self.assertEqual(row[change_review.CHECK], before + block + after + ' You see: The command stays exact')
                    _ = change_review.set_row_kind(self.review_list, row['#'], kind, self.production)
                    selected = change_page.render(replace(self.page, rows=(row['#'],)), change_page.DeliveryStatus(self.root)).decode()
                    browser = self.browser()[2]
                    section = re.search(r'<section\b[^>]*data-key="page-owner/' + row['#']
                                        + r'"[^>]*>(.*?)</section>', browser, re.DOTALL)
                    assert section is not None
                    for body in (section[1], selected):
                        blocks = [html.unescape(match[1]) for match in re.finditer(r'<pre><code>(.*?)</code></pre>', body, re.DOTALL)]
                        self.assertEqual(blocks, expected)
                        self.assertEqual(body.count(page_frame.COPY_BUTTON), len(expected))
                        self.assertIn(change_page.review_text_html(before.rstrip()), body)
                        self.assertIn(change_page.review_text_html(after.lstrip() + ' You see: The command stays exact'), body)
                        if not expected:
                            self.assertIn(change_page.review_text_html(row[change_review.CHECK]), body)

    @unittest.skipUnless(shutil.which('firefox'), 'Firefox is required for browser behavior')
    def test_code_copy_handles_results_repeated_clicks_and_new_blocks_inside_an_iframe(self) -> None:
        text = '  first <tag> & value\n\tsecond\n'
        content = change_page.document('<input id="answer" value="keep this">'
                                       + change_browser.item_html('`placeholder`').replace('placeholder', html.escape(text))
                                       + '<p>Keep <code>inline</code> unchanged.</p>').decode()
        probe = r'''<script>
(async()=>{
 const failures=[], copied=[], turns=ms=>new Promise(resolve=>setTimeout(resolve,ms));
 const check=(value,text)=>{if(!value) failures.push(text);};
 const button=document.querySelector('[data-copy-code]'),code=document.querySelector('pre code');
 Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async text=>{copied.push(text);}}});
 button.click();await turns(0);
 check(copied[0]===code.textContent,'The clipboard receives the exact code text.');
 check(button.title==='Copied' && button.getAttribute('aria-label')==='Copied','Success has an accessible label.');
 check(getComputedStyle(button.querySelector('[data-copy-icon]')).display==='none'
  && getComputedStyle(button.querySelector('[data-copied-icon]')).display!=='none','Success shows the check icon.');
 await turns(1200);button.click();await turns(1000);
 check(button.title==='Copied','A second click restarts the result time.');
 await turns(1100);
 check(button.title==='Copy' && button.getAttribute('aria-label')==='Copy code','The result returns to Copy.');
 navigator.clipboard.writeText=async()=>{throw new Error('Denied');};
 button.click();await turns(0);
 check(button.title==='Copy failed' && getComputedStyle(button.querySelector('[data-copy-icon]')).display!=='none','A failed write keeps the copy icon.');
 await turns(2100);check(button.title==='Copy','A failure returns to Copy.');
 Object.defineProperty(navigator,'clipboard',{configurable:true,value:undefined});
 const field=document.getElementById('answer');field.focus();field.setSelectionRange(1,4);
 document.execCommand=command=>{copied.push(document.querySelector('textarea').value);return command==='copy';};
 button.click();await turns(0);
 check(copied.at(-1)===code.textContent,'The fallback receives the exact code text.');
 check(button.title==='Copied' && document.activeElement===field && field.selectionStart===1 && field.selectionEnd===4,'The fallback restores focus and the field selection.');
 check(!document.querySelector('textarea'),'The fallback removes its textarea.');
 const fresh=document.querySelector('.code-block').cloneNode(true);document.body.append(fresh);
 fresh.querySelector('code').textContent='new block\n';fresh.querySelector('button').click();await turns(0);
 check(copied.at(-1)==='new block\n','A new block works without another listener.');
 document.execCommand=()=>false;button.click();await turns(0);
 check(button.title==='Copy failed','A failed fallback reports failure.');
 await fetch('/results',{method:'POST',body:JSON.stringify(failures)});
})().catch(error=>fetch('/results',{method:'POST',body:JSON.stringify([String(error)])}));
</script>'''
        content = content.replace('</body>', probe + '</body>')
        complete = threading.Event()
        results: list[list[str]] = []

        class CopyProbe(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if self.path == '/complete':
                    if not complete.wait(20):
                        self.send_error(504)
                        return
                    self.send_response(204)
                    self.end_headers()
                    return
                body = (content if self.path == '/frame' else
                        '<iframe src="/frame"></iframe><img src="/complete" hidden alt="">')
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.end_headers()
                _ = self.wfile.write(body.encode())

            def do_POST(self) -> None:
                results.append(cast(list[str], json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
                complete.set()
                self.send_response(204)
                self.end_headers()

            @override
            def log_message(self, format: str, *args: object) -> None:
                pass

        with ThreadingHTTPServer(('127.0.0.1', 0), CopyProbe) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                profile = self.temporary / 'copy-profile'
                profile.mkdir()
                result = run_process_group(['firefox', '--headless', '--no-remote', '--profile', str(profile),
                    '--screenshot', str(self.temporary / 'copy.png'), f'http://127.0.0.1:{server.server_port}/'],
                    timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
            finally:
                server.shutdown()
                thread.join(TEST_HANG_GUARD_SECS)
        self.assertEqual(results, [[]])

    def legacy_browser_reply(self, path: str) -> Reply:
        """Render retained forms from a page that was open before the migration."""
        if not path.startswith("/console/"):
            return self.request(path, {})
        browser = change_browser.page_at(self.root, path)
        body = change_browser.render(browser, change_page.DeliveryStatus(self.root)).decode()
        return Reply(200, body, "")

    def browser(self, kind: change_page.ReviewKind = "changes") -> tuple[str, BrowserForms, str]:
        path = change_page.page_url()
        browser = change_browser.page_at(self.root, path + ("?type=questions" if kind == "questions" else ""))
        body = change_browser.render(browser, change_page.DeliveryStatus(self.root)).decode()
        forms = BrowserForms()
        forms.feed(body)
        return path, forms, body

    def browser_fields(self, forms: BrowserForms, key: str, outcome: str = "works", feedback: str = "") -> dict[str, str]:
        return forms.rows[key] | {"row": key, "outcome": outcome, "feedback": feedback}

    def answer_question(self, text: str, row: str = "C1", *, question_id: str = "") -> subprocess.CompletedProcess[str]:
        if not question_id:
            question = change_page.DeliveryStatus(self.root).slots(self.page, row).question
            question_id = question.question_id if not isinstance(question, change_page.NoQuestion) else "unknown"
        return self.command(change_page, change_page.main,
                            ["answer-question", "--production", "page-owner", "--list", str(self.review_list),
                             "--row", row, "--question", question_id, "--text", text])

    def file_user_question(self, text: str = "Choose the layout", *, asker: staff.StaffRow | None = None,
                           question: str = "Which layout gives each review card enough space for its controls?\n"
                           + "1. Compact <layout>\n2. Wide layout") -> str:
        """Write a legacy Question fixture without allocating an ID."""
        document = self.production.read_text()
        if "- **Merge branch:**" not in document:
            _ = self.production.write_text(document + "- **Merge branch:** main\n- **Log:** progress.log\n")
        record = staff.snapshot()
        identities = update_registration.QuestionStaff.for_record(record)
        owner = next(row for row in record.rows if row.name == "page-owner")
        folder = update_registration.question_folder(self.production, identities)
        assert isinstance(folder, update_registration.QuestionFolder)
        asker = asker or self.unit
        digest = hashlib.sha256(f"{owner.id}:{asker.id}:{text}".encode()).hexdigest()[:16]
        path = folder.path / f"question-{asker.id}-{digest}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        item = update_registration.OutstandingItem(since="2026-11-12T12:00", text=text, question=question,
                                                   asked_by=asker.id)
        update_registration.write_question_record(update_registration.QuestionRecord(path, owner.id, asker.id, item, "open"))
        return "question/" + owner.id + "/" + path.name

    def test_f075_open_shows_a_legacy_question_when_another_source_is_unreadable(self) -> None:
        key = self.file_user_question()
        other_key = self.file_user_question("Read the numbered Question?")
        folder = update_registration.question_folder(self.production, update_registration.QuestionStaff.read())
        assert isinstance(folder, update_registration.QuestionFolder)
        path = folder.path / key.rsplit("/", 1)[-1]
        numbered = folder.path / other_key.rsplit("/", 1)[-1]
        _ = numbered.write_text(numbered.read_text().replace('registration: {', 'registration: {"review_id": "Q8", ', 1))
        before = {record: record.read_bytes() for record in (path, numbered)}
        blocked = record_store.default_question_folder(staff.StaffId("other-owner")) / "question-undecodable.md"
        blocked.parent.mkdir(parents=True)
        _ = blocked.write_bytes(b"---\n\xff\n")
        items = review_items.read(self.root, show="open", kinds=(review_items.ReviewType.QUESTION,))
        self.assertEqual({item.key: item.id for item in items}, {key: "page-owner/"+path.stem, other_key: "page-owner/Q8"})
        self.assertEqual({record: record.read_bytes() for record in before}, before)
        self.assertFalse((record_store.state_root() / "record-ids" / "question.water").exists())

    def test_questions_page_renders_the_reviewed_memory_bar_record_without_changing_it(self) -> None:
        key = self.file_user_question(MEMORY_BAR_ASK, question=MEMORY_BAR_RAW)
        browser = change_page.ReviewBrowser("private", kind="questions")
        question = next(item for item in change_browser.prepare(browser, change_page.DeliveryStatus(self.root)).questions
                        if item.key == key)
        update_registration.write_question_record(replace(question.record,
            item={**question.record.item, "reviewed": MEMORY_BAR_REVIEWED}))
        before = question.record.path.read_bytes()
        _, forms, body = self.browser("questions")
        self.assertIn(key, forms.rows)
        self.assertIn(f'<h1>{MEMORY_BAR_ASK}</h1>', body)
        self.assertEqual(body.count(MEMORY_BAR_ASK), 1)
        self.assertEqual(body.count('name="choice"'), 3)
        self.assertIn('<ul><li>sccache, the compiler cache: 2.2 GiB now, nearly all of the block.</li>', body)
        self.assertNotIn('<h2>Recommended</h2>', body)
        self.assertNotIn('because it names the contents', body)
        self.assertEqual(question.record.path.read_bytes(), before)

    def test_question_footers_show_current_sources_and_user_zone_times(self) -> None:
        runner = next(row for row in staff.snapshot().rows if row.name == "page-owner")
        producer = fixture_member("producer", name="question-producer")
        browser = change_page.ReviewBrowser("private", kind="questions")
        for asker, source in ((self.unit, "page-owner · builder-unit"), (runner, "page-owner"),
                              (producer, "question-producer")):
            with self.subTest(asker=asker.kind):
                key = self.file_user_question(f"Question from {asker.name}?", asker=asker)
                question = next(item for item in change_browser.prepare(browser, change_page.DeliveryStatus(self.root)).questions
                                if item.key == key)
                record = replace(question.record, item={**question.record.item, "since": "2026-11-15T20:05"})
                update_registration.write_question_record(record)
                footer = ('<dl class="change-details">' + f'<dt>source:</dt><dd>{source}</dd>'
                          + '<dt>date:</dt><dd>2026-11-15</dd><dt>time:</dt><dd>20:05</dd></dl>')
                body = self.browser("questions")[2]
                section = next(part for part in body.split('<section ') if f'data-key="{key}"' in part)
                section = section.split('</section>', 1)[0]
                self.assertIn(footer, without_role_colors(section))
                self.assertNotIn("Asked by", section)
                self.assertLess(without_role_colors(section).index('</form>'), without_role_colors(section).index(footer))
                unrouted = replace(question, tab="unrouted", record=record)
                section = change_browser.question_section(unrouted, browser, 4)
                self.assertNotIn('<form', section)
                self.assertIn(footer, without_role_colors(section))
                self.assertLess(without_role_colors(section).index('No showrunner owns this question.'), without_role_colors(section).index(footer))

    def file_other_change(self) -> Path:
        other = self.temporary / "change-review - another.md"
        owner = fixture_member("showrunner", name="another", doc=staff.LiveDoc(local_machine(), self.production))
        _ = change_review.append_rows(other, pictured_changes([change_review.Visible("Other change", "Read the proof", "It agrees")]),
                                      date(2026, 11, 2), "", self.production, producers=[owner.id], record=staff.snapshot())
        return other

    def test_all_lists_load_once_and_the_browser_contains_the_whole_waiting_queue(self) -> None:
        other = self.file_other_change()
        path = change_page.page_url()
        with patch.object(change_review, "read", wraps=change_review.read) as reads:
            reply = self.legacy_browser_reply(path)
        self.assertEqual(reply.status, 200)
        for review_list in (self.review_list, other):
            self.assertEqual(sum(call.args == (review_list,) for call in reads.call_args_list), 1)
        forms = BrowserForms()
        forms.feed(reply.body)
        self.assertEqual(set(forms.rows), {"page-owner/C1", "page-owner/C2", "page-owner/C3", "another/C1"})
        self.assertIn('another (1)', reply.body)
        self.assertIn('page-owner (3)', reply.body)
        self.assertIn("Read only", reply.body)
        self.assertIn("The old and new comments agree with the tests", reply.body)
        self.assertNotIn('value="next"', reply.body)

        question = self.file_user_question()
        _, forms, body = self.browser()
        self.assertIn('data-kind="changes"', body)
        self.assertIn('<title>Changes to review</title>', body)
        self.assertIn('<h1 class="page-title">Changes to review</h1>', body)
        self.assertNotIn("Which layout gives each review card enough space for its controls?", body)
        _, forms, body = self.browser("questions")
        self.assertIn('data-kind="questions"', body)
        self.assertIn('<title>Questions to answer</title>', body)
        self.assertIn('<h1 class="page-title">Questions to answer</h1>', body)
        self.assertIn("Which layout gives each review card enough space for its controls?", body)
        self.assertNotIn("Choose the layout", body)
        self.assertIn("Compact &lt;layout&gt;", body)
        self.assertIn('<dt>source:</dt><dd>page-owner · builder-unit</dd>', without_role_colors(body))
        self.assertNotIn("Asked by", body)
        self.assertNotIn("The old and new comments agree with the tests", body)
        self.assertEqual(forms.tabs[question], "page-owner")
        self.assertEqual(forms.outcomes[question], ["answer", "followup", "skip"])
        self.assertEqual(forms.question_counts["page-owner"], 1)
        for kind in ("changes", "questions"):
            with self.subTest(kind=kind, controls="absent"):
                body = self.browser(kind)[2]
                for removed in ('data-review-type', 'role="tab"', 'data-rebuilds', 'data-total', "total.textContent", "+' waiting'"):
                    self.assertNotIn(removed, body)
        with self.subTest(kind="unknown"):
            for query in ("?type=rebuilds", "?type=changes&type=questions"):
                self.assertEqual(self.request(path + query, {}).status, 303)

        rebuilds = path.rsplit("/", 1)[0] + "/" + change_page.REBUILDS_NAME
        with self.subTest(rebuilds="missing folder"):
            with patch.object(need, "read_needs", wraps=need.read_needs) as reads:
                body = change_browser.render_rebuilds().decode()
            reads.assert_called_once_with([])
            self.assertIn('<title>Rebuilds needed</title>', body)
            self.assertIn('<div data-rebuilds data-count="0" data-label=""><p>No rebuilds needed.</p></div>', body)
            self.assertFalse(Path(os.environ["REBUILD_NEEDS_DIR"]).exists())
            self.assertEqual(self.request(rebuilds, {}).status, 200)
            forwarded = self.http_request("/zzzzzz/" + change_page.REBUILDS_NAME, {})
            self.assertEqual((forwarded.status, forwarded.location), (303, "/console/rebuilds.html"))

        with self.subTest(rebuilds="machine reasons and current names"):
            owner = next(row for row in staff.snapshot().rows if row.name == "page-owner")
            second = fixture_member("unit", name="second-session", parent=owner.id)
            _ = staff.rename(self.unit.id, "renamed <unit>")
            rows: tuple[tuple[need.Machine, str, str, str, str, str], ...] = (
                ("natedev", "b" * 40, "Apply the second settings.", second.id, second.id, "2026-11-12T13:00:00+00:00"),
                ("natedev", "a" * 40, "Apply the first <settings>.", self.unit.id, owner.id, "2026-11-12T12:00:00+00:00"),
                ("mac", "c" * 40, "Apply the Mac settings.", "unknown-staff", "unknown-requester", "2026-11-12T11:00:00+00:00"),
            )
            for machine, commit, reason, concerns, requester, created in rows:
                need.add_need(machine, commit, reason, concerns, requester)
                path = Path(os.environ["REBUILD_NEEDS_DIR"]) / f"{machine}-{commit}.json"
                record = cast(need.NeedRecord, json.loads(path.read_text()))
                record["created_at"] = created
                _ = path.write_text(json.dumps(record))
            with patch.object(need, "read_needs", wraps=need.read_needs) as reads:
                body = change_browser.render_rebuilds().decode()
            reads.assert_called_once_with([])
            answer = rebuild_row.read(Path(os.environ["CONF_DIR"]) / "statusline.conf")
            self.assertIn(change_browser.rebuild_list(answer, {}, staff.snapshot()), self.request(rebuilds, {}).body)
            block = body.split('<div data-rebuilds data-count="2" data-label="Rebuild natedev and the Mac">', 1)[1].rsplit('</div>', 1)[0]
            self.assertLess(block.index('data-machine="natedev"'), block.index('data-machine="mac"'))
            self.assertIn('<h2>Rebuild natedev</h2>', block)
            self.assertIn('<h2>Rebuild the Mac</h2>', block)
            for removed in ("Run <code>rebuild</code>", "five minutes", "One rebuild applies", "Completed requests",
                            "Commit", "Affected session", "running system"):
                self.assertNotIn(removed, block)
            self.assertLess(block.index("Apply the second settings."), block.index("Apply the first &lt;settings&gt;."))
            natedev, mac = block.split('</article>', 1)
            self.assertIn('<div data-owner><h3>second-session (unit)</h3>'
                          + '<dl><dt><time datetime="2026-11-12T13:00:00+00:00">Nov 12, 2026, 08:00</time></dt>'
                          + '<dd>Apply the second settings.</dd></dl></div><div data-owner><h3>page-owner (showrunner)</h3>',
                          without_role_colors(natedev))
            self.assertIn('<h3>Unknown session: unknown-requester</h3>', mac)
            for control in ('<form', '<textarea', 'data-showrunner', 'data-next', 'data-key'):
                self.assertNotIn(control, block)

            with self.subTest(rebuilds="time column"):
                self.assertIn('[data-rebuilds] dl{display:grid;grid-template-columns:max-content minmax(0,1fr)', body)
                self.assertIn('[data-rebuilds] dt{grid-column:1;color:light-dark(#5f5f5f,#b0b0b0)}[data-rebuilds] dd{grid-column:2;', body)

            with self.subTest(rebuilds="requester absent"):
                natedev_needs = next(item for item in need.read_needs() if item.newest["machine"] == "natedev")
                single: need.NeedRecord = {**natedev_needs.newest, "requested_by": ""}
                summary = need.MachineNeeds(single, (single["reason"],), (single,))
                content = change_browser.rebuild_list(
                    rebuild_row.RebuildAnswer(("natedev",), False, needs=(summary,)), {}, staff.snapshot())
                self.assertIn('data-count="1" data-label="Rebuild natedev"', content)
                self.assertIn('<h3>second-session (unit)</h3>', without_role_colors(content))

            with self.subTest(rebuilds="status line comparison"):
                configuration = Path(os.environ["CONF_DIR"]) / "statusline.conf"
                cache = self.temporary / "rebuild-cache"
                _ = configuration.write_text(f"rebuild.cache={cache}\n")
                details = rebuild_row.ExpectedSystems(
                    "d" * 40, ("/etc/nixos", "main", "natedev", "MacBook-Pro"),
                    "/nix/store/" + "1" * 32 + "-natedev", "/nix/store/" + "2" * 32 + "-mac").details()
                _ = cache.write_text(f"rebuild mac\n{2 ** 40}\n{configuration}\n{details}\n")
                _ = rebuild_row.changes_path(cache).write_text(json.dumps({"mac": [
                    {"commit": "c" * 40, "at": "2026-11-12T10:00:00+00:00", "reason": "Apply the Mac settings.",
                     "session": ""},
                    {"commit": "e" * 40, "at": "2026-11-12T14:30:00+00:00", "reason": "memory: <defer> defrag",
                     "session": owner.id}]}))
                body = change_browser.render_rebuilds().decode()
                self.assertEqual(rebuild_row.read(configuration).machines, ("natedev", "mac"))
                block = body.split('<div data-rebuilds data-count="2" data-label="Rebuild natedev and the Mac">', 1)[1]
                mac = block.split('</article>', 1)[1]
                # The filed need covers its commit, so that commit shows once, with the need's requester.
                self.assertEqual(mac.count("Apply the Mac settings."), 1)
                self.assertIn('<section class="rebuild-commits"><h3>Commits</h3>'
                              + '<div data-owner><h4>page-owner (showrunner)</h4>'
                              + '<dl><dt><time datetime="2026-11-12T14:30:00+00:00">Nov 12, 2026, 09:30</time></dt>'
                              + '<dd>memory: &lt;defer&gt; defrag</dd></dl></div></section>', mac)
                self.assertLess(mac.index("Apply the Mac settings."), mac.index("<h3>Commits</h3>"))
                only = change_browser.rebuild_list(rebuild_row.RebuildAnswer(("mac",), False, compared=("mac",)),
                                                   {"mac": (rebuild_row.Change("f" * 40, "2026-11-12T15:00:00+00:00",
                                                                               "Turn on swap", ""),)},
                                                   staff.snapshot())
                self.assertEqual(only, '<div data-rebuilds data-count="1" data-label="Rebuild the Mac">'
                                 + '<article data-machine="mac"><h2>Rebuild the Mac</h2>'
                                 + '<section class="rebuild-commits"><h3>Commits</h3>'
                                 + '<div data-owner><h4>Unknown session</h4>'
                                 + '<dl><dt><time datetime="2026-11-12T15:00:00+00:00">Nov 12, 2026, 10:00</time></dt>'
                                 + '<dd>Turn on swap</dd></dl></div></section></article></div>')

            with self.subTest(rebuilds="one owner and one time each"):
                grouped = change_browser.rebuild_list(
                    rebuild_row.RebuildAnswer(("mac",), False, compared=("mac",)),
                    {"mac": (rebuild_row.Change("1" * 40, "2026-11-12T13:00:00+00:00", "Older", owner.id),
                             rebuild_row.Change("2" * 40, "2026-11-12T15:00:00+00:00", "First", owner.id),
                             rebuild_row.Change("3" * 40, "2026-11-12T14:00:00+00:00", "Between", ""),
                             rebuild_row.Change("4" * 40, "2026-11-12T15:00:00+00:00", "Second", owner.id))},
                    staff.snapshot())
                self.assertEqual(grouped.count('<h4>page-owner (showrunner)</h4>'), 1)
                self.assertIn('<div data-owner><h4>page-owner (showrunner)</h4><dl>'
                              + '<dt><time datetime="2026-11-12T15:00:00+00:00">Nov 12, 2026, 10:00</time></dt>'
                              + '<dd>First</dd><dd>Second</dd>'
                              + '<dt><time datetime="2026-11-12T13:00:00+00:00">Nov 12, 2026, 08:00</time></dt>'
                              + '<dd>Older</dd></dl></div><div data-owner><h4>Unknown session</h4>', grouped)
                self.assertIn('<p>The change list is not available yet.</p>', change_browser.rebuild_list(
                    rebuild_row.RebuildAnswer(("mac",), False, compared=("mac",)), {}, staff.snapshot()))

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required for browser checks")
    def test_page_status_uses_the_real_changes_loader(self) -> None:
        _ = self.file_other_change()
        _ = self.file_user_question()
        _, _, body = self.browser()
        self.assertIn('</h1><p class="page-status"', body)
        self.assertIn(page_frame.STYLE, body)
        self.assertLess(body.index('data-page-status '), body.index('<div data-review-content'))
        assert_status_states(self, body, 15)

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required for browser checks")
    def test_page_status_uses_the_real_questions_loader(self) -> None:
        _ = self.file_other_change()
        _ = self.file_user_question()
        _, _, body = self.browser("questions")
        self.assertEqual(body.count(page_frame.BUTTON_STATES), 1)
        self.assertIn('</h1><p class="page-status"', body)
        self.assertIn(page_frame.STYLE, body)
        self.assertLess(body.index('data-page-status '), body.index('<div data-review-content'))
        assert_status_states(self, body, 15)

    def test_user_question_answers_follow_each_typed_relay(self) -> None:
        destination = fixture_member("showrunner", name="destination")
        owner = next(row for row in staff.snapshot().rows if row.name == "page-owner")
        moved = fixture_member("unit", name="moved-unit", parent=owner.id)
        for name, asker, recipient in (("unit", self.unit, self.unit.name), ("owner", owner, owner.name),
                                        ("moved", moved, destination.name)):
            with self.subTest(relay=name):
                key = self.file_user_question("Choose " + name, asker=asker)
                if name == "moved":
                    _ = staff.move(moved.id, staff.ReportsTo(destination.id))
                path, forms, _ = self.browser("questions")
                self.delivery_done.clear()
                reply = self.http_request(path, self.browser_fields(forms, key, "answer", "Use compact\nKeep the choices"))
                self.assertEqual(reply.status, 200, reply.body)
                self.assertEqual(json.loads(reply.body), {"answer": "Use compact\nKeep the choices"})
                self.assertTrue(self.delivery_done.wait(TEST_HANG_GUARD_SECS))
                self.assertEqual(self.messages[-1][0], recipient)
                self.assertTrue(self.messages[-1][2].startswith("From the user (via the review page): Choose " + name))
                self.assertIn("Use compact\nKeep the choices", self.messages[-1][2])
                if name == "moved":
                    self.assertIn("For: moved-unit", self.messages[-1][2])
                _, after, _ = self.browser("questions")
                self.assertNotIn(key, after.rows)
                count = len(self.messages)
                duplicate = self.http_request(path, self.browser_fields(forms, key, "answer", "Use compact"))
                self.assertEqual(duplicate.status, 400)
                self.assertIn("no longer open", duplicate.body)
                self.assertEqual(len(self.messages), count)

        with self.subTest(requests="two prepared answers"):
            key = self.file_user_question("Keep one delivery")
            browser = change_page.ReviewBrowser("private-concurrent")
            first_status = change_page.DeliveryStatus(self.root)
            second_status = change_page.DeliveryStatus(self.root)
            first = next(item for item in change_browser.prepare(browser, first_status).questions if item.key == key)
            second = next(item for item in change_browser.prepare(browser, second_status).questions if item.key == key)
            delivery, _ = change_browser.submit_question(first, "answer", "First answer", first_status)
            pending = first_status.path(delivery.page, delivery.row).read_bytes()
            with self.assertRaises(change_page.QuestionAnswerRefused):
                _ = change_browser.submit_question(second, "answer", "Second answer", second_status)
            self.assertEqual(first_status.path(delivery.page, delivery.row).read_bytes(), pending)
            count = len(self.messages)
            change_page.deliver(delivery, first_status)
            change_page.recover_question_answers(change_page.DeliveryStatus(self.root))
            self.assertEqual(len(self.messages), count + 1)
            self.assertIn("First answer", self.messages[-1][2])
            self.assertNotIn("Second answer", self.messages[-1][2])

    def test_exact_question_save_requires_a_revision_check(self) -> None:
        key = self.file_user_question("Check the selected question")
        status = change_page.DeliveryStatus(self.root)
        browser = change_page.ReviewBrowser("fixture", kind="questions")
        question = next(item for item in change_browser.prepare(browser, status).questions if item.key == key)
        scope = update_registration.question_scope(question.owner, staff.snapshot())
        args = (scope, question, "answer", "Use the compact layout.")
        before_write = Mock()
        with self.assertRaises(TypeError):
            _ = signature(change_browser.save_question_record).bind(*args, before_write=before_write)

        def refuse() -> object:
            raise review_items.ReviewRecordChanged("The selected question changed.")

        previous = question.record.path.read_bytes()
        with self.assertRaises(review_items.ReviewRecordChanged):
            change_browser.save_question_record(scope, question, "answer", args[3],
                change_browser.ExactQuestionSave(refuse), before_write=before_write)
        before_write.assert_not_called()
        self.assertEqual(question.record.path.read_bytes(), previous)

    def test_failed_question_answer_alerts_the_tab_showrunner(self) -> None:
        producer = fixture_member("producer", name="bob")
        showrunner = showrunners.owner_for("page-owner")
        _ = write_config_fixture(conf.path("review_links"), "unrouted_alert_recipient=page-owner\n")
        showrunner_key = self.file_user_question(asker=producer)
        path = record_store.default_question_folder(producer.id) / "question-from-bob.md"
        path.parent.mkdir(parents=True)
        question = update_registration.OutstandingItem(
            since="2026-11-12T12:00", text="Which timestamps should we use?",
            question="Which timestamps should we use?", asked_by=producer.id)
        update_registration.write_question_record(
            update_registration.QuestionRecord(path, producer.id, producer.id, question, "open"))
        for key in (showrunner_key, f"question/{producer.id}/{path.name}"):
            with self.subTest(key=key):
                status = change_page.DeliveryStatus(self.root)
                browser = change_page.ReviewBrowser("fixture", kind="questions")
                source = next(item for item in change_browser.prepare(browser, status).questions if item.key == key)
                delivery, _ = change_browser.submit_question(source, "answer", "Use the tool clock.", status)
                with patch.object(change_page, "send_to_staff", side_effect=[
                    RuntimeError("recipient is offline"), RuntimeError("recipient is offline"),
                    RuntimeError("recipient is offline"), None,
                ]) as send:
                    change_page.deliver(delivery, status)
                    change_page.recover_question_answers(change_page.DeliveryStatus(self.root))
                self.assertEqual([call.args[0] for call in send.call_args_list], [producer.id] * 3 + [showrunner.id])
                alert = cast(str, send.call_args.args[2])
                self.assertIn("Change review delivery needs attention", alert)
                assert isinstance(delivery, change_page.MessageDelivery)
                self.assertIn(f"List: {delivery.page.review_list}", alert)
                self.assertIn(f"Row: {delivery.row}", alert)
                self.assertIn(f"Recipient: {producer.id}", alert)
                self.assertIn("Tries: 3", alert)
                self.assertIn("Last error: recipient is offline", alert)
                self.assertIn("The user's answer is saved. Pass it on by hand.", alert)
                self.assertIn("Use the tool clock.", alert)

    def test_recovery_reuses_the_alert_key_after_the_clear_write_fails(self) -> None:
        producer = fixture_member("producer", name="bob")
        showrunner = showrunners.owner_for("page-owner")
        status = change_page.DeliveryStatus(self.root)
        path = status.closeout_path(self.page, "C1")
        fields = status.closeout_fields(self.page, "C1")
        alert = change_page.delivery_failure_alert(change_page.DeliveryFailure(producer.id, "Offline"), 3,
                                                  self.page.review_list, "C1", "Use the tool clock.")
        key = change_page.review_delivery_key(self.page.review_list, "C1", "fixture", purpose="alert")
        fields["pending_alerts"] = [{"recipient": showrunner.id, "message": alert, "key": key}]
        status.write_closeout(path, fields)
        sent = subprocess.CompletedProcess([], 0, "SENT: fixture accepted the alert", "")
        with (patch.object(change_page, "send_message", new=SEND_MESSAGE),
              patch.object(subprocess, "run", return_value=sent) as run):
            with patch.object(status, "write_closeout", side_effect=OSError("The clear write failed.")) as write:
                status.send_pending_alerts(path)
            write.assert_called_once()
            change_page.DeliveryStatus(self.root).send_pending_alerts(path)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args_list[0], run.call_args_list[1])
        command = cast(list[str], run.call_args.args[0])
        self.assertEqual(command[command.index("--key") + 1], key)
        self.assertEqual(command[command.index("--recipient-key") + 1], showrunner.id)
        self.assertEqual(status.closeout_fields(self.page, "C1").get("pending_alerts"), [])

    def test_pending_alert_retries_with_its_saved_key_until_the_result_is_final(self) -> None:
        showrunner = showrunners.owner_for("page-owner")
        status = change_page.DeliveryStatus(self.root)
        path = status.closeout_path(self.page, "C1")
        fields = status.closeout_fields(self.page, "C1")
        alert: change_page.PendingDeliveryAlert = {
            "recipient": showrunner.id, "message": "Pass the answer on by hand.", "key": "review:fixture-alert"}
        fields["pending_alerts"] = [alert]
        status.write_closeout(path, fields)
        outcomes = [
            subprocess.CompletedProcess([], 5, "PENDING: delivery unconfirmed", ""),
            subprocess.CompletedProcess([], 6, "RETRY: delivery unconfirmed", ""),
            subprocess.CompletedProcess([], 2, "REFUSED: the recipient cannot take alerts", ""),
        ]
        with (patch.object(change_page, "send_message", new=SEND_MESSAGE),
              patch.object(subprocess, "run", side_effect=outcomes) as run, redirect_stderr(StringIO()) as errors):
            status.send_pending_alerts(path)
            self.assertTrue(status.has_deferred_alerts())
            self.assertEqual(status.closeout_fields(self.page, "C1").get("pending_alerts"), [alert])
            status.recover_deferred_alerts()
            self.assertTrue(status.has_deferred_alerts())
            self.assertEqual(status.closeout_fields(self.page, "C1").get("pending_alerts"), [alert])
            status.recover_deferred_alerts()
            status.recover_deferred_alerts()
        self.assertFalse(status.has_deferred_alerts())
        self.assertEqual(status.closeout_fields(self.page, "C1").get("pending_alerts"), [])
        self.assertIn("REFUSED: the recipient cannot take alerts", errors.getvalue())
        self.assertEqual(run.call_count, 3)
        self.assertEqual(run.call_args_list, [run.call_args_list[0]] * 3)
        command = cast(list[str], run.call_args.args[0])
        self.assertEqual(command[command.index("--key") + 1], alert["key"])

    def test_alert_transport_releases_the_delivery_lock_and_preserves_later_writes(self) -> None:
        showrunner = showrunners.owner_for("page-owner")
        status = change_page.DeliveryStatus(self.root)
        path = status.closeout_path(self.page, "C1")
        fields = status.closeout_fields(self.page, "C1")
        fields["pending_alerts"] = [{
            "recipient": showrunner.id, "message": "Pass the answer on by hand.", "key": "review:fixture-alert"}]
        status.write_closeout(path, fields)

        def send(_recipient: staff.StaffId, _sender: str, _message: str, *, key: str) -> None:
            self.assertEqual(key, "review:fixture-alert")
            with (self.root / ".review-link-delivery.lock").open("a", encoding="utf-8") as contender:
                fcntl.flock(contender.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                current = status.closeout_fields(self.page, "C1")
                current["messages"].append("Another request updated this record.")
                status.write_closeout(path, current)

        with patch.object(change_page, "send_to_staff", side_effect=send) as sender:
            status.send_pending_alerts(path)
        sender.assert_called_once()
        current = status.closeout_fields(self.page, "C1")
        self.assertEqual(current.get("pending_alerts"), [])
        self.assertEqual(current["messages"], ["Another request updated this record."])

    def test_open_question_answer_recovers_for_the_asker_when_the_saved_mark_fails(self) -> None:
        key = self.file_user_question("Keep the saved answer")
        status = change_page.DeliveryStatus(self.root)
        browser = change_page.ReviewBrowser("private", kind="questions")
        question = next(item for item in change_browser.prepare(browser, status).questions if item.key == key)
        write_slots = status.write_slots

        def write(page: change_page.ReviewPage, row: str, slots: change_page.DeliverySlots) -> None:
            if (isinstance(page, change_page.UserQuestionPage) and page.saved["saved"]
                    and isinstance(slots.verdict, change_page.DeliveryIntent)):
                raise OSError("fixture saved mark failed")
            write_slots(page, row, slots)

        with patch.object(status, "write_slots", new=write):
            delivery, answer = change_browser.submit_question(question, "answer", "Keep it", status)
        self.assertEqual(answer, "Keep it")
        self.assertEqual(delivery.recipients, (self.unit.id,))
        self.assertTrue(status.review_save_receipt(delivery.page, delivery.row).exists())
        self.assertEqual(self.messages, [])
        change_page.recover_question_answers(change_page.DeliveryStatus(self.root))
        change_page.recover_question_answers(change_page.DeliveryStatus(self.root))
        self.assertEqual([recipient for recipient, _, _ in self.messages], [self.unit.name])
        self.assertIn("Keep it", self.messages[0][2])
        self.assertFalse(status.review_save_receipt(delivery.page, delivery.row).exists())

    def test_a_failed_question_log_write_preserves_the_answer_delivery(self) -> None:
        key = self.file_user_question("Keep the answer if its log fails")
        status = change_page.DeliveryStatus(self.root)
        browser = change_page.ReviewBrowser("private", kind="questions")
        question = next(item for item in change_browser.prepare(browser, status).questions if item.key == key)
        with patch.object(update_registration, "record_review_log", side_effect=OSError("fixture log write failed")):
            delivery, answer = change_browser.submit_question(question, "answer", "Keep this answer", status)
        self.assertEqual(answer, "Keep this answer")
        self.assertIsInstance(status.slots(delivery.page, delivery.row).verdict, change_page.DeliveryIntent)
        change_page.recover_question_answers(change_page.DeliveryStatus(self.root))
        change_page.recover_question_answers(change_page.DeliveryStatus(self.root))
        self.assertEqual([recipient for recipient, _, _ in self.messages], [self.unit.name])
        self.assertIn(answer, self.messages[0][2])

    def test_question_recovery_ignores_read_errors_on_other_questions(self) -> None:
        key = self.file_user_question("Recover this answer")
        unrelated_key = self.file_user_question("Leave this question alone")
        status = change_page.DeliveryStatus(self.root)
        browser = change_page.ReviewBrowser("private", kind="questions")
        questions = {item.key: item for item in change_browser.prepare(browser, status).questions}
        unrelated = questions[unrelated_key].record.path
        _ = change_browser.submit_question(questions[key], "answer", "Use this answer", status)
        open_path = Path.open

        def read(path: Path, mode: Literal["r"] = "r", buffering: int = -1,
                 encoding: str | None = None, errors: str | None = None, newline: str | None = None) -> TextIO:
            if path == unrelated:
                raise OSError("fixture unrelated question read failed")
            return open_path(path, mode, buffering, encoding, errors, newline)

        with patch.object(Path, "open", new=read):
            change_page.recover_question_answers(change_page.DeliveryStatus(self.root))
        self.assertEqual([recipient for recipient, _, _ in self.messages], [self.unit.name])
        self.assertIn("Use this answer", self.messages[0][2])

    def test_question_recovery_keeps_the_receipt_when_the_source_read_fails(self) -> None:
        key = self.file_user_question("Read the saved answer again")
        status = change_page.DeliveryStatus(self.root)
        browser = change_page.ReviewBrowser("private", kind="questions")
        question = next(item for item in change_browser.prepare(browser, status).questions if item.key == key)
        delivery, _ = change_browser.submit_question(question, "answer", "Use the saved answer", status)
        page = delivery.page
        assert isinstance(page, change_page.UserQuestionPage)
        slots = status.slots(page, delivery.row)
        status.write_slots(replace(page, saved={**page.saved, "saved": False}), delivery.row, slots)
        receipt = status.review_save_receipt(page, delivery.row)
        _ = receipt.write_text(delivery.attempt + "\n")
        open_path = Path.open

        def read(path: Path, mode: Literal["r"] = "r", buffering: int = -1,
                 encoding: str | None = None, errors: str | None = None, newline: str | None = None) -> TextIO:
            if path == question.record.path:
                raise OSError("fixture question read failed")
            return open_path(path, mode, buffering, encoding, errors, newline)

        with patch.object(Path, "open", new=read):
            change_page.recover_question_answers(change_page.DeliveryStatus(self.root))
        self.assertEqual(status.slots(page, delivery.row), slots)
        self.assertTrue(receipt.exists())
        self.assertEqual(self.messages, [])
        change_page.recover_question_answers(change_page.DeliveryStatus(self.root))
        self.assertEqual([recipient for recipient, _, _ in self.messages], [self.unit.name])
        self.assertIn("Use the saved answer", self.messages[0][2])
        self.assertFalse(receipt.exists())

    def test_followup_waits_for_the_reply_and_skip_moves_a_question_to_the_end(self) -> None:
        texts = {self.file_user_question("Choose first"): "Choose first",
                 self.file_user_question("Choose second"): "Choose second"}
        path, forms, body = self.browser("questions")
        first, second = [key for key in forms.rows if key.startswith("question/")]
        self.assertEqual({first, second}, set(texts))
        self.assertIn('<p class="answer-help" id=', body)
        self.assertIn('>Your answer</label>', body)

        with self.subTest(outcome="skip"):
            self.delivery_done.clear()
            self.assertEqual(self.http_request(path, self.browser_fields(forms, first, "skip", "")).status, 200)
            self.assertTrue(self.delivery_done.wait(TEST_HANG_GUARD_SECS))
            _, after, _ = self.browser("questions")
            self.assertEqual([key for key in after.rows if key.startswith("question/")], [second, first])
            self.assertEqual(self.messages[-1][2], "From the user (via the review page), skipped for now; "
                             + f"it stays open at the end of the list: {texts[first]}\nFor: builder-unit")

        with self.subTest(outcome="followup without text"):
            reply = self.http_request(path, self.browser_fields(forms, second, "followup", " "))
            self.assertEqual(reply.status, 400)

        with self.subTest(outcome="followup"):
            self.delivery_done.clear()
            reply = self.http_request(path, self.browser_fields(forms, second, "followup", "Which file holds it?"))
            self.assertEqual(reply.status, 200, reply.body)
            self.assertTrue(self.delivery_done.wait(TEST_HANG_GUARD_SECS))
            self.assertEqual(self.messages[-1][0], self.unit.name)
            self.assertIn("Which file holds it?", self.messages[-1][2])
            self.assertIn(f"update_registration.py reply --production page-owner --item '{texts[second]}' --text",
                          self.messages[-1][2])
            _, after, _ = self.browser("questions")
            self.assertNotIn(second, after.rows)
            self.assertIn(first, after.rows)

        with self.subTest(outcome="reply"):
            record = staff.snapshot()
            owner = next(row for row in record.rows if row.name == "page-owner")
            production = read_production(owner, record)
            identities = update_registration.QuestionStaff.for_record(record)
            update_registration.reply(production, texts[second], "The file is layout.toml.", identities=identities)
            _, after, body = self.browser("questions")
            self.assertIn(second, after.rows)
            self.assertIn("<h2>Follow-up</h2><p>Which file holds it?</p><h2>Reply</h2><p>The file is layout.toml.</p>", body)

    def test_staff_moves_the_unit_changes_to_its_current_showrunner_tab(self) -> None:
        runner = fixture_member("showrunner", name="sdlc", doc=staff.LiveDoc(local_machine(), self.production))
        _ = staff.move(self.unit.id, staff.ReportsTo(runner.id))
        path, forms, body = self.browser()
        self.assertEqual(forms.tabs["page-owner/C1"], "sdlc")
        self.assertIn('<dt>change:</dt><dd>sdlc/C1</dd>', body)
        self.assertNotIn('<dt>change:</dt><dd>page-owner/C1</dd>', body)
        self.assertIn('<dt>change:</dt><dd>sdlc/C1</dd>', self.request(self.page_path, {}).body)
        self.assertIn('sdlc (3)', body)
        reply = self.http_request(path, self.browser_fields(forms, "page-owner/C1", "validate", "Check this yourself"))
        self.assertEqual(reply.status, 200)
        self.assertTrue(self.delivery_done.wait(TEST_HANG_GUARD_SECS))
        self.assertEqual([recipient for recipient, _, _ in self.messages], ["sdlc"])
        self.assertIn("Check this yourself", self.messages[0][2])
        self.assertEqual(len(change_review.pending(self.review_list)), 3)
        refreshed = BrowserForms()
        refreshed.feed(self.legacy_browser_reply(path).body)
        self.assertEqual(list(refreshed.rows), ["page-owner/C2", "page-owner/C3", "page-owner/C1"])

    def test_a_renamed_unit_gets_its_current_staff_session(self) -> None:
        _ = staff.rename(self.unit.id, "x")
        current = staff.update_current_session(self.unit.id, staff.native_id(fixture_runtime_id("session-x")))
        self.assertEqual(self.request(self.page_path, self.answer(outcome="not works")).status, 303)
        self.assertEqual(self.raw_messages[0][0], f"session:{current.current_session}")
        self.assertEqual(self.messages[0][0], "x")

    def test_a_delivery_resolves_the_staff_session_after_an_account_move(self) -> None:
        status = change_page.DeliveryStatus(self.root)
        delivery = change_page.submit(self.page, urlencode(self.answer(outcome="validate")), status)
        owner = staff.snapshot().by_path("page-owner")
        if owner is None:
            self.fail("the showrunner must have a staff row")
        moved = staff.update_current_session(owner.id, staff.native_id(fixture_runtime_id("moved-showrunner")))
        record = cast(dict[str, object], json.loads(status.path(self.page, "C1").read_text()))
        validation = cast(dict[str, object], record["validation"])
        self.assertEqual(validation["recipients"], [owner.id])
        change_page.deliver(delivery, status)
        self.assertEqual(self.raw_messages[-1][0], f"session:{moved.current_session}")

    def test_tabs_with_waiting_changes_keep_their_order_before_empty_tabs(self) -> None:
        other = self.temporary / "change-review - zeta.md"
        owner = fixture_member("showrunner", name="zeta", doc=staff.LiveDoc(local_machine(), self.production))
        _ = change_review.append_rows(other, pictured_changes([change_review.Visible("Other change", "Read", "Proof")]),
                                      date(2026, 11, 2), "", self.production, producers=[owner.id], record=staff.snapshot())
        path, forms, body = self.browser()
        browser = change_browser.page_at(self.root, path)
        prepared = change_browser.prepare(browser, change_page.DeliveryStatus(self.root))
        self.assertEqual(list(prepared.queues), ["page-owner", "zeta", "demo"])
        self.assertEqual(list(forms.counts.items()), [("page-owner", 3), ("zeta", 1), ("demo", 0)])
        self.assertIn("tab=place.get('showrunner') || ''", body)

    def test_sent_validation_and_question_tabs_sort_after_waiting_tabs(self) -> None:
        other = self.temporary / "change-review - alpha.md"
        _ = change_review.append_rows(other, pictured_changes([change_review.Visible("Other change", "Read", "Proof")]),
                                      date(2026, 11, 2), "", self.production, record=staff.snapshot())
        page = change_page.ReviewPage(other, self.production, ("C1",), "")
        status = change_page.DeliveryStatus(self.root)
        for kind in ("validation", "question"):
            with self.subTest(kind=kind):
                status.path(page, "C1").unlink(missing_ok=True)
                if kind == "validation":
                    attempt = status.begin(page, "C1", "validation", waiting_why="The owner must validate the change.")
                else:
                    question = status.ask(page, "C1", "Which change?")
                    if not isinstance(question, change_page.QuestionDelivery):
                        self.fail("a new question must create a message attempt")
                    attempt = question.attempt
                path, pending, body = self.browser()
                del body
                self.assertEqual(list(pending.counts.items()), [("page-owner", 3), ("alpha", 0), ("demo", 0)])
                status.record(change_page.MessageDelivery(page, "C1", (), "", attempt))
                forms = BrowserForms()
                forms.feed(self.legacy_browser_reply(path).body)
                self.assertEqual(list(forms.counts.items()), [("page-owner", 3), ("alpha", 0), ("demo", 0)])
                prepared = change_browser.prepare(change_browser.page_at(self.root, path), change_page.DeliveryStatus(self.root))
                self.assertEqual(list(prepared.queues), ["page-owner", "alpha", "demo"])

    def test_production_aliases_share_one_document_read_per_page_request(self) -> None:
        runner = fixture_member("showrunner", name="sdlc", doc=staff.LiveDoc(local_machine(), self.production))
        _ = staff.move(self.unit.id, staff.ReportsTo(runner.id))
        reads: list[Path] = []
        read_text = Path.read_text

        def read_document(path: Path, encoding: str | None = None, errors: str | None = None) -> str:
            reads.append(path)
            return read_text(path, encoding=encoding, errors=errors)

        with patch.object(Path, "read_text", new=read_document):
            path, forms, body = self.browser()
        del path, body
        self.assertEqual(reads.count(self.production), 1)
        self.assertEqual(forms.counts["sdlc"], 3)
        self.assertEqual(set(forms.save_urls), {"page-owner/C1", "page-owner/C2", "page-owner/C3"})

    def test_an_unrouted_row_refuses_a_question_without_saving_and_still_accepts_a_verdict(self) -> None:
        assert isinstance(self.unit.parent, staff.ReportsTo)
        _ = change_review.append_rows(self.review_list, pictured_changes([change_review.Visible("Unknown", "Read", "Proof")]),
                                      date(2026, 11, 2), "abcdef12", self.production, producers=[staff.StaffId("missing-producer")], record=staff.snapshot())
        path, forms, body = self.browser()
        self.assertIn('Owner unavailable (1)', body)
        self.assertNotIn('>unrouted (<span', body)
        self.assertEqual(forms.tabs["page-owner/C4"], "unrouted")
        self.assertNotIn("question", forms.outcomes["page-owner/C4"])
        with self.subTest(controls="unavailable owner"):
            self.assertEqual(forms.outcomes["page-owner/C4"], ["works", "not works"])
        before = self.review_list.read_bytes()
        status = change_page.DeliveryStatus(self.root)
        browser = change_browser.page_at(self.root, path)
        fields = self.browser_fields(forms, "page-owner/C4", "question", "Who checks this?")
        with self.assertRaisesRegex(question_record.AnswerRefused, "no showrunner to receive the request"):
            _ = change_browser.submit(browser, urlencode(fields), status)
        self.assertEqual(self.review_list.read_bytes(), before)
        self.assertFalse(status.path(self.page, "C4").exists())
        self.assertEqual(self.http_request(path, fields).status, 400)
        with self.subTest(action="feedback without an owner"):
            fields = self.browser_fields(forms, "page-owner/C4", "feedback", "Make the title larger")
            self.assertEqual(self.http_request(path, fields).status, 400)
            self.assertEqual(self.review_list.read_bytes(), before)
            self.assertNotIn("feedback", forms.outcomes["page-owner/C4"])
        with (patch.object(scratch, "end", side_effect=conf.Unusable("scratch settings unavailable")),
              redirect_stderr(StringIO()) as errors):
            self.assertEqual(self.http_request(path, self.browser_fields(forms, "page-owner/C4")).status, 200)
            self.assertTrue(self.delivery_done.wait(TEST_HANG_GUARD_SECS))
        self.assertIn("scratch not removed: scratch settings unavailable", errors.getvalue())
        self.assertEqual(self.messages, [])
        saved = next(row for row in change_review.review_rows(self.review_list) if row["#"] == "C4")
        self.assertEqual(saved["Verdict"], "works")

    def test_a_question_without_recipients_alerts_its_showrunner_and_finishes(self) -> None:
        status = change_page.DeliveryStatus(self.root)
        question = status.ask(self.page, "C1", "Who checks this?")
        assert isinstance(question, change_page.QuestionDelivery)
        change_page.deliver(change_page.MessageDelivery(self.page, "C1", (), "Question", question.attempt, change_page.route_owner(self.page, change_page.answer_row(self.page, "C1")).showrunner_recipients), status)
        self.assertNotIn(question.attempt, status.request_attempts)
        saved = status.slots(self.page, "C1").question
        assert isinstance(saved, change_page.OpenQuestion)
        self.assertIsInstance(saved.delivery, change_page.NoDelivery)
        self.assertEqual([recipient for recipient, _, _ in self.messages], ["page-owner"])
        self.assertIn("no showrunner to send to", self.messages[0][2])
        self.assertIn("Pass it on by hand", self.messages[0][2])

    def test_a_legacy_failed_verdict_shows_the_saved_answer_and_sends_nothing(self) -> None:
        before = self.review_list.read_bytes()
        conflict = change_review.VerdictConflict("not works", "2026-11-01")
        with (patch.object(change_review, "record_verdict", return_value=conflict),
              patch.object(scratch, "end", side_effect=conf.Unusable("scratch settings unavailable")),
              redirect_stderr(StringIO()) as errors):
            self.assertEqual(self.request(self.page_path, self.answer()).status, 400)
        self.assertIn("scratch not removed: scratch settings unavailable", errors.getvalue())
        self.assertEqual(self.review_list.read_bytes(), before)
        self.assert_record_cleared()

        self.assertEqual(self.request(self.page_path, self.answer(outcome="not works")).status, 303)
        status = change_page.DeliveryStatus(self.root)
        _ = status.path(self.page, "C1").write_text(json.dumps({"attempt": "old", "state": "failed", "kind": "verdict"}))
        before = self.review_list.read_bytes()
        self.messages.clear()
        change_page.recover_pending(status)
        content = self.request(self.page_path, {}).body
        self.assertIn("Saved: not works", content)
        self.assert_no_delivery_controls(content)
        self.assertEqual(self.messages, [])
        self.assertEqual(self.review_list.read_bytes(), before)

    def test_a_producer_keeps_its_list_showrunner_without_a_new_staff_parent(self) -> None:
        path, forms, body = self.browser()
        del path, body
        self.assertEqual(forms.tabs["page-owner/C1"], "page-owner")

    def test_a_showrunner_own_change_stays_under_its_source_list(self) -> None:
        merged = self.git("rev-parse", "HEAD")
        _ = self.review_list.write_text(self.review_list.read_text().replace(merged, "").replace(self.unit.id, ""))
        path, forms, body = self.browser()
        del path
        self.assertEqual(forms.tabs["page-owner/C1"], "page-owner")
        self.assertIn('<dt>change:</dt><dd>page-owner/C1</dd>', body)
        self.assertIn('<dt>change:</dt><dd>page-owner/C1</dd>', self.request(self.page_path, {}).body)

    def test_an_unknown_unit_stays_in_its_list_even_when_production_names_another_showrunner(self) -> None:
        foreign = self.temporary / "sdlc-production.md"
        _ = foreign.write_text(self.production.read_text())
        _ = fixture_member("showrunner", name="sdlc", doc=staff.LiveDoc(local_machine(), foreign))
        written = change_review.append_rows(self.review_list, pictured_changes([change_review.Visible("Unknown unit", "Read", "Proof")]),
                                      date(2026, 11, 2), "abcdef12", foreign, record=staff.snapshot())
        owner = staff.snapshot().by_path("sdlc")
        assert owner is not None
        _ = self.review_list.write_text(self.review_list.read_text().replace(
            written[0], written[0].replace(str(owner.id), "sdlc")))
        row = next(row for row in change_review.pending(self.review_list) if row["#"] == "C4")
        self.assertEqual(change_page.row_production(self.page, row), self.production)
        path, forms, body = self.browser()
        del path, body
        self.assertEqual(forms.tabs["page-owner/C4"], "page-owner")

    def test_a_registered_production_saves_when_the_list_has_only_a_missing_document_reference(self) -> None:
        text = self.review_list.read_text().replace(str(self.production), str(self.temporary / "missing.md"))
        _ = self.review_list.write_text(text)
        with patch.object(showrunners, "timer_docs", return_value={"page-owner": self.production}):
            path, forms, body = self.browser()
            del body
            reply = self.http_request(urljoin(path, forms.save_urls["page-owner/C1"]),
                                      self.browser_fields(forms, "page-owner/C1"))
        self.assertEqual(reply.status, 200, reply.body)
        self.assertEqual(cast(object, json.loads(reply.body)), {"answer": "works"})
        self.assertTrue(self.delivery_done.wait(TEST_HANG_GUARD_SECS))
        row = next(row for row in change_review.review_rows(self.review_list) if row["#"] == "C1")
        self.assertEqual(row["Verdict"], "works")
        self.assertEqual([recipient for recipient, _, _ in self.messages], ["page-owner"])

    def test_a_list_without_a_production_document_shows_its_changes_without_answer_controls(self) -> None:
        runner = staff.snapshot().by_path("page-owner")
        assert runner is not None
        _ = staff.set_doc(runner.id, staff.LiveDoc(local_machine(), self.temporary / "missing.md"))
        text = self.review_list.read_text().replace(str(self.production), str(self.temporary / "missing.md"))
        _ = self.review_list.write_text(text)
        path, forms, body = self.browser()
        self.assertEqual(set(forms.rows), {"page-owner/C1", "page-owner/C2", "page-owner/C3"})
        self.assertEqual(forms.save_urls, {})
        self.assertIn("The production document is not available. This change cannot accept an answer.", body)
        browser = change_browser.page_at(self.root, path)
        fields = {"row": "page-owner/C1", "token": change_browser.row_token(browser, "page-owner/C1"),
                  "outcome": "works", "feedback": ""}
        self.assertEqual(self.http_request(path, fields).status, 400)
        self.assertEqual(len(change_review.pending(self.review_list)), 3)

    def test_an_earlier_lists_prerequisites_keep_qualified_ids_in_the_embedded_tab_order(self) -> None:
        runner = fixture_member("showrunner", name="sdlc", doc=staff.LiveDoc(local_machine(), self.production))
        _ = staff.move(self.unit.id, staff.ReportsTo(runner.id))
        earlier = self.temporary / "change-review - earlier.md"
        _ = change_review.append_rows(earlier, pictured_changes([change_review.Visible("First", "Read", "Proof")]),
                                      date(2026, 11, 2), "abcdef12", self.production,
                                      urgency.cell(urgency.read(), "C2"), producers=[self.unit.id], record=staff.snapshot())
        _ = change_review.append_rows(earlier, pictured_changes([change_review.Visible("Second", "Read", "Proof")]),
                                      date(2026, 11, 2), "abcdef12", self.production, producers=[self.unit.id], record=staff.snapshot())
        path, forms, body = self.browser()
        del body
        keys = list(forms.rows)
        self.assertLess(keys.index("earlier/C2"), keys.index("earlier/C1"))
        self.assertEqual(forms.tabs["earlier/C1"], "sdlc")
        self.assertEqual(forms.tabs["earlier/C2"], "sdlc")
        self.assertEqual(forms.counts["sdlc"], 5)
        self.assertEqual(forms.counts["earlier"], 0)
        self.assertEqual(forms.counts["page-owner"], 0)
        self.assertEqual(set(forms.save_urls), set(forms.rows))
        reply = self.http_request(urljoin(path, forms.save_urls["earlier/C2"]),
                                  self.browser_fields(forms, "earlier/C2", "not works", "Needs a correction"))
        self.assertEqual(reply.status, 200, reply.body)
        self.assertTrue(self.delivery_done.wait(TEST_HANG_GUARD_SECS))
        row = next(row for row in change_review.review_rows(earlier) if row["#"] == "C2")
        self.assertEqual(row["Verdict"], "not works: Needs a correction")

    def test_a_dependent_row_follows_its_prerequisite_before_and_after_detachment(self) -> None:
        parent = staff.snapshot().by_path("page-owner")
        assert parent is not None
        earlier = self.temporary / "change-review - sdlc.md"
        document = self.temporary / "sdlc.md"
        _ = document.write_text(f"# Child\n- **Change review:** `{earlier}`\n")
        child = fixture_member("showrunner", name="sdlc", parent=parent.id,
                               doc=staff.LiveDoc(local_machine(), document))
        _ = staff.move(self.unit.id, staff.ReportsTo(child.id))
        _ = change_review.append_rows(earlier, pictured_changes([change_review.Visible("Prerequisite", "Read", "Proof")]),
                                      date(2026, 11, 2), "abcdef12", document,
                                      producers=[self.unit.id], record=staff.snapshot())
        _ = earlier.write_text(earlier.read_text().replace("| C1 |", "| C41 |"))
        result = self.command(change_review, change_review.main,
                              ["add", "--production", "sdlc", "--repo", str(self.repo),
                               "--change", "Dependent", "--check", "Read", "--see", "Proof",
                               "--urgency", "blocks", "--after", "sdlc/C41", "--image", fixture_image()])
        self.assertEqual((result.returncode, result.stdout), (0, "C4\n"), result.stderr)
        added = next(row for row in change_review.pending(self.review_list) if row["#"] == "C4")
        self.assertEqual(urgency.parse_cell(added[change_review.URGENCY])[1], "sdlc/C41")
        for attached in (True, False):
            with self.subTest(attached=attached):
                if not attached:
                    _ = staff.move(child.id, staff.StaffRoot())
                path, forms, body = self.browser()
                del path, body
                keys = list(forms.rows)
                self.assertEqual(forms.tabs["sdlc/C41"], "sdlc")
                self.assertEqual(forms.tabs["page-owner/C4"], "sdlc")
                self.assertLess(keys.index("sdlc/C41"), keys.index("page-owner/C4"))

    def test_rows_from_two_units_use_their_producer_ids_for_their_staff_tabs(self) -> None:
        first = fixture_member("showrunner", name="sdlc", doc=staff.LiveDoc(local_machine(), self.production))
        second = fixture_member("showrunner", name="other", doc=staff.LiveDoc(local_machine(), self.production))
        _ = staff.move(self.unit.id, staff.ReportsTo(first.id))
        second_unit = fixture_member("unit", name="second-unit", parent=second.id)
        _ = change_review.append_rows(self.review_list,
                                      pictured_changes([change_review.Visible("First mixed change", "Read", "First proof"),
                                       change_review.Visible("Second mixed change", "Read", "Second proof")]),
                                      date(2026, 11, 2), "abcdef12", self.production, producers=[self.unit.id, second_unit.id], record=staff.snapshot())
        path, forms, body = self.browser()
        del path, body
        self.assertEqual(forms.tabs["page-owner/C4"], "sdlc")
        self.assertEqual(forms.tabs["page-owner/C5"], "other")

    def test_the_browser_response_does_not_wait_for_a_message_or_show_delivery_controls(self) -> None:
        path, forms, body = self.browser()
        del body
        entered, release = threading.Event(), threading.Event()

        def slow_send(recipient: str, sender: str, message: str, **_options: str) -> None:
            entered.set()
            if not release.wait(TEST_HANG_GUARD_SECS):
                raise RuntimeError("send was not released")
            self.capture_message(recipient, sender, message)

        with patch.object(change_page, "send_message", side_effect=slow_send):
            try:
                reply = self.http_request(path, self.browser_fields(forms, "page-owner/C1"))
                self.assertEqual(reply.status, 200)
                self.assertEqual(json.loads(reply.body), {"answer": "works"})
                self.assertTrue(entered.wait(TEST_HANG_GUARD_SECS))
                self.assertFalse(self.delivery_done.is_set())
                pending = self.legacy_browser_reply(path).body
                refreshed = BrowserForms()
                refreshed.feed(pending)
                self.assertNotIn("page-owner/C1", refreshed.rows)
                self.assertNotIn("Saved: works", pending)
                self.assert_no_delivery_controls(pending)
                self.assertIn('page-owner (2)', pending)
            finally:
                release.set()
                if entered.is_set():
                    self.assertTrue(self.delivery_done.wait(TEST_HANG_GUARD_SECS))
        landed = self.legacy_browser_reply(path).body
        self.assert_no_delivery_controls(landed)

    def test_a_failed_browser_save_returns_plain_words_and_accepts_the_same_typed_answer_again(self) -> None:
        _ = self.git("remote", "add", "origin", "https://github.com/hanallc/claude_commands.git")
        path, forms, body = self.browser()
        del body
        fields = self.browser_fields(forms, "page-owner/C1", "not works", "Keep <my> words & outcome")
        for refusal in ("bad proposal /private/file.json", "feature_log: bad proposal /private/file.json"):
            with (self.subTest(refusal=refusal),
                  patch.object(feature_log, "drop_review", side_effect=feature_log.FeatureLogError(refusal))):
                for _attempt in range(3):
                    failed = self.http_request(path, fields)
                    self.assertEqual(failed.status, 503)
                    self.assertEqual(json.loads(failed.body), {"reason": "The feature log could not be updated."})
                    self.assertNotIn("/private/", failed.body)
                    self.assertEqual(len(change_review.pending(self.review_list)), 3)
                    self.assertEqual(self.messages[-1][2].splitlines()[1], "feature_log: bad proposal /private/file.json")
                    self.assertEqual(len(self.messages), 1)
        with patch.object(feature_log, "drop_review", side_effect=feature_log.FeatureLogError("another failure")):
            self.assertEqual(self.http_request(path, fields).status, 503)
        self.assertEqual(len(self.messages), 2)
        with patch.object(feature_log, "drop_review", side_effect=feature_log.FeatureLogError("bad proposal /private/file.json")):
            other = self.browser_fields(forms, "page-owner/C3", "not works", "Another row")
            self.assertEqual(self.http_request(path, other).status, 503)
        self.assertEqual(len(self.messages), 3)
        status = change_page.DeliveryStatus(self.root)
        with patch.object(feature_log, "drop_review", side_effect=feature_log.FeatureLogError("bad proposal /private/file.json")):
            with self.assertRaises(change_page.FeatureLogFailed):
                _ = change_page.closeout_before_save(self.page, self.production, "C1", "not works", status)
        self.assertEqual(len(self.messages), 3)
        self.delivery_done.clear()
        saved = self.http_request(path, fields)
        self.assertEqual(saved.status, 200)
        self.assertTrue(self.delivery_done.wait(TEST_HANG_GUARD_SECS))
        rows = change_review.review_rows(self.review_list)
        self.assertEqual(next(row["Verdict"] for row in rows if row["#"] == "C1"), "not works: Keep <my> words & outcome")

        key = self.file_user_question("Choose a safe save")
        path, forms, _ = self.browser("questions")
        fields = self.browser_fields(forms, key, "answer", "Keep my typed answer")
        with patch.object(update_registration, "answer", side_effect=OSError("private save unavailable")):
            failed = self.http_request(path, fields)
        self.assertEqual(failed.status, 503)
        self.assertIn("could not be saved", failed.body)
        self.assertIn(key, self.browser("questions")[1].rows)
        self.delivery_done.clear()
        saved = self.http_request(path, fields)
        self.assertEqual(saved.status, 200, saved.body)
        self.assertTrue(self.delivery_done.wait(TEST_HANG_GUARD_SECS))
        self.assertEqual(json.loads(saved.body)["answer"], "Keep my typed answer")

    def test_a_later_browser_delivery_failure_leaves_the_row_saved_without_controls(self) -> None:
        path, forms, body = self.browser()
        del body
        with patch.object(change_page, "send_message", side_effect=RuntimeError("socket unavailable")):
            reply = self.http_request(path, self.browser_fields(forms, "page-owner/C1", "not works", "Please fix"))
            self.assertEqual(reply.status, 200)
            self.assertTrue(self.delivery_done.wait(TEST_HANG_GUARD_SECS))
        content = self.request(self.page_path, {}).body
        self.assertIn("Saved: not works: Please fix", content)
        self.assert_no_delivery_controls(content)
        refreshed = BrowserForms()
        refreshed.feed(self.legacy_browser_reply(path).body)
        self.assertEqual(refreshed.counts["page-owner"], 2)
        self.assert_record_cleared()

        for failures in (1, 3):
            with self.subTest(user_question_failures=failures):
                key = self.file_user_question("Choose retry " + str(failures))
                path, forms, _ = self.browser("questions")
                calls: list[str] = []
                remaining = failures

                def fail_question(recipient: str, sender: str, text: str, **_options: str) -> None:
                    nonlocal remaining
                    calls.append(text)
                    if remaining > 0 and "delivery needs attention" not in text:
                        remaining -= 1
                        raise RuntimeError("private sender unavailable")
                    self.capture_message(recipient, sender, text)

                self.delivery_done.clear()
                with patch.object(change_page, "send_message", side_effect=fail_question):
                    saved = self.http_request(path, self.browser_fields(forms, key, "answer", "Keep it"))
                    self.assertEqual(saved.status, 200, saved.body)
                    self.assertTrue(self.delivery_done.wait(TEST_HANG_GUARD_SECS))
                self.assertEqual(len(calls), 2 if failures == 1 else 4)
                self.assertEqual(sum("delivery needs attention" in text for text in calls), int(failures == 3))
                self.assertNotIn(key, self.browser("questions")[1].rows)

    def test_the_browser_refuses_extra_fields_bad_tokens_and_unshown_rows(self) -> None:
        path, forms, body = self.browser()
        del body
        before = self.review_list.read_bytes()
        valid = self.browser_fields(forms, "page-owner/C1")
        for fields in (valid | {"path": str(self.review_list)}, valid | {"command": "true"},
                       valid | {"token": "é"}, valid | {"row": "page-owner/C999"}, valid | {"row": "../C1"}):
            with self.subTest(fields=fields):
                self.assertEqual(self.http_request(path, fields).status, 400)
        self.assertEqual(self.review_list.read_bytes(), before)
        self.assertEqual(self.messages, [])

        key = self.file_user_question("Choose elsewhere")
        path, forms, _ = self.browser("questions")
        owner = next(row for row in staff.snapshot().rows if row.name == "page-owner")
        _ = update_registration.answer(read_production(owner, staff.snapshot()), "Choose elsewhere")
        count = len(self.messages)
        stale = self.http_request(path, self.browser_fields(forms, key, "answer", "Too late"))
        self.assertEqual(stale.status, 400)
        self.assertIn("no longer open", stale.body)
        self.assertEqual(len(self.messages), count)

    def test_the_browser_uses_the_existing_pin_and_move_order_without_publishing_next_pages(self) -> None:
        _ = next_item.move_to_end(next_item.CHANGE, self.production, key="C1")
        queue = next_item.change_queue(self.production)
        urgency.pin(queue.name, "C3")
        path, forms, body = self.browser()
        self.assertEqual(forms.orders["page-owner"], ["page-owner/C3", "page-owner/C2", "page-owner/C1"])
        before = set(self.root.iterdir())
        self.assertEqual(self.legacy_browser_reply(path).status, 200)
        self.assertEqual(set(self.root.iterdir()), before)
        self.assertIn('data-next hidden>Skip for now', body)

    def test_change_views_use_next_queues_across_children_and_independent_roots(self) -> None:
        root = staff.snapshot().by_path("page-owner")
        assert root is not None
        root = staff.rename(root.id, "natedev")
        docs: dict[str, Path] = {root.name: self.production}
        for name, level, day in (("alpha", urgency.BLOCKS, 3), ("zeta", urgency.LATER, 2)):
            doc = self.temporary / f"{name}.md"
            _ = doc.write_text(f"# {name}\n- **Change review:** `{self.review_list}`\n")
            owner = fixture_member("showrunner", name=name, parent=root.id, doc=staff.LiveDoc(local_machine(), doc))
            docs[name] = doc
            _ = change_review.append_rows(self.review_list, pictured_changes([change_review.Visible(name, "Read", "Proof")]),
                date(2026, 11, day), "", doc, urgency.cell(urgency.Urgency(level)),
                producers=[owner.id], record=staff.snapshot())
        _ = next_item.move_to_end(next_item.CHANGE, docs["alpha"], key="C4")
        tab_queue = next_item.change_queue(self.production)
        urgency.pin(tab_queue.name, "C3")
        all_queue = next_item.change_queue(self.production, below=True)
        urgency.pin(all_queue.name, "C2")
        urgency.move_to_end(all_queue.name, "C5", datetime(2026, 11, 5))
        _ = change_review.append_rows(self.review_list, pictured_changes([change_review.Visible("Unrouted", "Read", "Proof")]),
            date(2026, 10, 1), "", self.production, urgency.cell(urgency.Urgency(urgency.BLOCKS)),
            producers=[staff.StaffId("missing-producer")], record=staff.snapshot())

        def expected(doc: Path, below: bool = False) -> list[str]:
            queue = next_item.change_queue(doc, below)
            prefix = change_review.scope_list(doc).stem.removeprefix(change_review.PREFIX) + "/"
            return [entry.key if "/" in entry.key else prefix + entry.key
                    for entry in urgency.queue_order(queue.entries, queue.name)]

        _, forms, _ = self.browser()
        for name, doc in docs.items():
            with self.subTest(tab=name):
                self.assertEqual(forms.orders[name], expected(doc))
        self.assertEqual(forms.orders[""], expected(self.production, below=True) + ["page-owner/C6"])
        self.assertEqual(list(forms.rows), forms.orders[""])
        self.assertEqual(forms.orders["natedev"][0], "page-owner/C3")
        self.assertEqual(forms.orders[""][0], "page-owner/C2")

        other = self.temporary / "change-review - hana.md"
        doc = self.temporary / "hana.md"
        _ = doc.write_text(f"# Hana\n- **Change review:** `{other}`\n")
        owner = fixture_member("showrunner", name="hana", doc=staff.LiveDoc(local_machine(), doc))
        docs[owner.name] = doc
        _ = change_review.append_rows(other, pictured_changes([change_review.Visible(f"Hana {number}", "Read", "Proof")
                                              for number in range(3)]),
            date(2026, 10, 2), "", doc, urgency.cell(urgency.Urgency(urgency.BLOCKS)),
            producers=[owner.id] * 3, record=staff.snapshot())
        hana_queue = next_item.change_queue(doc, below=True)
        urgency.pin(hana_queue.name, "C3")
        urgency.move_to_end(hana_queue.name, "C1", datetime(2026, 11, 5))
        self.assertEqual(expected(doc, below=True), ["hana/C3", "hana/C2", "hana/C1"])
        settings = self.temporary / "config/review_links.conf"
        for first, roots in (("natedev", ("natedev", "hana")), ("hana", ("hana", "natedev")),
                             ("missing", ("hana", "natedev")), ("alpha", ("hana", "natedev"))):
            _ = write_config_fixture(settings, f"changes_first_root={first}\n")
            _, forms, _ = self.browser()
            with self.subTest(first_root=first):
                for name, production in docs.items():
                    self.assertEqual(forms.orders[name], expected(production))
                self.assertEqual(forms.orders[""], expected(docs[roots[0]], below=True)
                                 + expected(docs[roots[1]], below=True) + ["page-owner/C6"])
                self.assertEqual(forms.orders["unrouted"], ["page-owner/C6"])
                self.assertEqual(list(forms.rows), forms.orders[""])

    def test_empty_lists_keep_their_zero_count_tabs(self) -> None:
        for row in ("C1", "C2", "C3"):
            _ = change_review.record_verdict(self.review_list, row, "works", date(2026, 11, 2), self.production)
        path, forms, body = self.browser()
        del path
        self.assertEqual(forms.rows, {})
        self.assertIn('page-owner (0)', body)

    def test_validation_requests_leave_the_browser_queue_after_delivery_and_a_fresh_prepare(self) -> None:
        path, forms, body = self.browser()
        del body
        self.delivery_done.clear()
        reply = self.http_request(path, self.browser_fields(forms, "page-owner/C1", "validate", "Check the proof"))
        self.assertEqual(reply.status, 200)
        self.assertTrue(self.delivery_done.wait(TEST_HANG_GUARD_SECS))
        browser = change_browser.page_at(self.root, path)
        status = change_page.DeliveryStatus(self.root)
        prepared = change_browser.prepare(browser, status)
        self.assertEqual([item.key for item in prepared.changes if item.waiting], ["page-owner/C2", "page-owner/C3"])
        self.assertEqual([entry.key for entry in prepared.queues["page-owner"].entries], ["C2", "C3"])
        self.assertFalse(any(row.get("Verdict") for row in change_review.review_rows(self.review_list)))
        fresh = BrowserForms()
        content = change_browser.render(browser, change_page.DeliveryStatus(self.root)).decode()
        fresh.feed(content)
        self.assertEqual(fresh.counts["page-owner"], 2)
        self.assertFalse(fresh.pending["page-owner/C1"])
        self.assertEqual(fresh.outcomes["page-owner/C1"], [])
        self.assertNotIn("page-owner/C1", fresh.save_urls)
        self.assertIn("The showrunner will validate this", content)
        self.assertIn("C1: the user asks you to validate this", self.messages[0][2])
        queue = next_item.change_queue(self.production)
        self.assertNotIn("C1", [entry.key for entry in urgency.queue_order(queue.entries, queue.name)])

    def test_removing_a_validation_record_restores_waiting_until_a_verdict_saves(self) -> None:
        path, forms, body = self.browser()
        del body
        status = change_page.DeliveryStatus(self.root)
        browser = change_browser.page_at(self.root, path)
        delivery, answer = change_browser.submit(browser, urlencode(
            self.browser_fields(forms, "page-owner/C1", "validate")), status)
        self.assertEqual(answer, "")
        if isinstance(delivery, change_page.NoMessageDelivery):
            self.fail("the validation request must create a delivery attempt")
        status.record(delivery)
        self.assertNotIn("page-owner/C1", [item.key for item in change_browser.prepare(browser, status).changes if item.waiting])
        status.path(delivery.page, "C1").unlink()
        self.assertIn("page-owner/C1", [item.key for item in change_browser.prepare(browser, status).changes if item.waiting])
        _ = change_review.record_verdict(self.review_list, "C1", "works", date(2026, 11, 2), self.production)
        prepared = change_browser.prepare(browser, change_page.DeliveryStatus(self.root))
        self.assertNotIn("page-owner/C1", [item.key for item in prepared.changes if item.waiting])
        refreshed = BrowserForms()
        refreshed.feed(change_browser.render(browser, status).decode())
        self.assertEqual(refreshed.counts["page-owner"], 2)

    def test_unsent_validation_records_leave_the_waiting_count_without_delivery_controls(self) -> None:
        path, forms, body = self.browser()
        del forms, body
        status = change_page.DeliveryStatus(self.root)
        for state in ("pending", "failed"):
            with self.subTest(state=state):
                _ = status.path(self.page, "C1").write_text(json.dumps({"validation": {
                    "attempt": "old", "state": state, "feedback": "Keep these words"}}))
                prepared = change_browser.prepare(change_browser.page_at(self.root, path), status)
                self.assertNotIn("page-owner/C1", [item.key for item in prepared.changes if item.waiting])
                self.assertNotIn("C1", [entry.key for entry in prepared.queues["page-owner"].entries])
                content = self.legacy_browser_reply(path).body
                refreshed = BrowserForms()
                refreshed.feed(content)
                self.assertEqual(refreshed.counts["page-owner"], 2)
                self.assertFalse(refreshed.pending["page-owner/C1"])
                self.assertIn("The showrunner will validate this", content)
                self.assert_no_delivery_controls(content)

    def test_failed_validation_delivery_stays_out_of_the_count_without_answer_buttons(self) -> None:
        path, forms, body = self.browser()
        del body
        with patch.object(change_page, "send_message", side_effect=RuntimeError("socket unavailable")):
            self.delivery_done.clear()
            self.assertEqual(self.http_request(path, self.browser_fields(
                forms, "page-owner/C1", "validate", "Keep <these> words")).status, 200)
            self.assertTrue(self.delivery_done.wait(TEST_HANG_GUARD_SECS))
        content = self.legacy_browser_reply(path).body
        refreshed = BrowserForms()
        refreshed.feed(content)
        self.assertEqual(refreshed.counts["page-owner"], 2)
        self.assertFalse(refreshed.pending["page-owner/C1"])
        self.assertEqual(refreshed.outcomes["page-owner/C1"], [])
        self.assertEqual(footer_counts.changes(self.production), refreshed.counts["page-owner"])
        self.assertIn("The showrunner will validate this", content)
        self.assert_no_delivery_controls(content)

    def test_a_failed_validation_save_keeps_the_waiting_count(self) -> None:
        path, forms, body = self.browser()
        del body
        with patch.object(next_item, "move_to_end", side_effect=OSError("queue unavailable")):
            reply = self.http_request(path, self.browser_fields(forms, "page-owner/C1", "validate", "Keep this answer"))
        self.assertEqual(reply.status, 503)
        refreshed = BrowserForms()
        refreshed.feed(self.legacy_browser_reply(path).body)
        self.assertEqual(refreshed.counts["page-owner"], 3)
        self.assertTrue(refreshed.pending["page-owner/C1"])

    def test_browser_feedback_waits_for_the_showrunner_note_before_returning_to_the_count(self) -> None:
        path, forms, _ = self.browser()
        before = change_page.answer_row(self.page, "C1")
        self.delivery_done.clear()
        fields = self.browser_fields(forms, "page-owner/C1", "feedback", "Make <this> proof clearer")
        reply = self.http_request(path, fields)
        self.assertEqual(reply.status, 200)
        self.assertEqual(json.loads(reply.body), {"answer": ""})
        self.assertTrue(self.delivery_done.wait(TEST_HANG_GUARD_SECS))
        status = change_page.DeliveryStatus(self.root)
        feedback = status.slots(self.page, "C1").question
        self.assertIsInstance(feedback, change_page.OpenFeedback)
        assert isinstance(feedback, change_page.OpenFeedback)
        saved = cast(change_page.StoredDeliverySlots, json.loads(status.path(self.page, "C1").read_text()))
        saved_question = saved.get("question")
        assert saved_question is not None
        self.assertEqual(saved_question.get("kind"), "feedback")
        row = change_page.answer_row(self.page, "C1")
        log = review_events.decode(row.pop("Review log"))
        self.assertEqual(row, before)
        self.assertEqual([entry.words for entry in log], ["feedback: Make <this> proof clearer"])
        self.assertEqual([recipient for recipient, _, _ in self.messages], ["page-owner"])
        for line in ("C1: the user sends feedback to act on", f"Feedback id: {feedback.question_id}",
                     "Feedback: Make <this> proof clearer"):
            self.assertIn(line, self.messages[0][2])
        self.assertNotIn("Question id:", self.messages[0][2])
        self.assertNotIn("validate this", self.messages[0][2])
        content = self.legacy_browser_reply(path).body
        refreshed = BrowserForms()
        refreshed.feed(content)
        self.assertEqual(refreshed.counts["page-owner"], 2)
        self.assertEqual(footer_counts.changes(self.production), 2)
        prepared = change_browser.prepare(change_browser.page_at(self.root, path), status)
        self.assertEqual([entry.key for entry in prepared.queues["page-owner"].entries], ["C2", "C3"])
        self.assertEqual(refreshed.outcomes["page-owner/C1"], [])
        self.assertIn("The showrunner will act on this", content)
        direct = self.request(self.page_path, {}).body.split('<section id="C1" data-change-card>', 1)[1].split('</section>', 1)[0]
        self.assertIn("The showrunner will act on this", direct)
        self.assertNotIn('<form', direct)
        repeat = self.http_request(path, fields)
        self.assertEqual(repeat.status, 400)
        self.assertEqual(json.loads(repeat.body), {"reason": change_page.WITH_SHOWRUNNER})
        self.assertEqual(len(self.messages), 1)
        note = self.answer_question("The <proof> now has three steps.", question_id=feedback.question_id)
        self.assertEqual(note.returncode, 0, note.stderr)
        content = self.legacy_browser_reply(path).body
        refreshed = BrowserForms()
        refreshed.feed(content)
        self.assertEqual(refreshed.counts["page-owner"], 3)
        self.assertEqual(footer_counts.changes(self.production), 3)
        self.assertEqual(refreshed.outcomes["page-owner/C1"], ["works", "not works", "validate", "question", "feedback"])
        self.assertIn("Showrunner: The &lt;proof&gt; now has three steps.", content)
        self.assertIsInstance(status.slots(self.page, "C1").question, change_page.CompletedFeedback)
        self.assertEqual(self.http_request(path, self.browser_fields(refreshed, "page-owner/C1", "works")).status, 200)
        self.assertTrue(self.delivery_done.wait(TEST_HANG_GUARD_SECS))
        self.assertEqual(next(row["Verdict"] for row in change_review.review_rows(self.review_list) if row["#"] == "C1"), "works")

    def test_answer_preserves_the_time_of_the_change_handoff(self) -> None:
        status = change_page.DeliveryStatus(self.root)
        path = status.path(self.page, 'C1')
        handed_at = '2026-11-16T12:49:08-05:00'
        with patch.object(time, 'time', return_value=datetime.fromisoformat(handed_at).timestamp()):
            request = status.ask(self.page, 'C1', 'Which groups?')
        assert isinstance(request, change_page.QuestionDelivery)
        self.assertEqual(cast(dict[str, object], json.loads(path.read_text())).get('handed_at'), handed_at)

        self.assertTrue(status.answer_question(self.page, 'C1', request.question_id, 'The numbered groups.'))
        self.assertEqual(cast(dict[str, object], json.loads(path.read_text())).get('handed_at'), handed_at)

    def test_an_open_question_leaves_waiting_rows_counts_and_next_after_a_fresh_prepare(self) -> None:
        path, forms, body = self.browser()
        del body
        self.delivery_done.clear()
        reply = self.http_request(path, self.browser_fields(forms, "page-owner/C1", "question", "What does <this> change?"))
        self.assertEqual(reply.status, 200)
        self.assertEqual(json.loads(reply.body), {"answer": ""})
        self.assertTrue(self.delivery_done.wait(TEST_HANG_GUARD_SECS))
        browser = change_browser.page_at(self.root, path)
        prepared = change_browser.prepare(browser, change_page.DeliveryStatus(self.root))
        self.assertEqual([item.key for item in prepared.changes if item.waiting], ["page-owner/C2", "page-owner/C3"])
        self.assertEqual([entry.key for entry in prepared.queues["page-owner"].entries], ["C2", "C3"])
        refreshed = BrowserForms()
        content = change_browser.render(browser, change_page.DeliveryStatus(self.root)).decode()
        refreshed.feed(content)
        self.assertEqual(refreshed.counts["page-owner"], 2)
        self.assertFalse(refreshed.pending["page-owner/C1"])
        self.assertEqual(refreshed.outcomes["page-owner/C1"], [])
        self.assertIn('<h2>Question</h2><p>What does &lt;this&gt; change?</p>', content)
        self.assertIn("The showrunner will answer this", content)
        self.assertEqual([recipient for recipient, _, _ in self.messages], ["page-owner"])
        message = self.messages[0][2]
        for text in ("C1: the user asks a question", f"List: {self.review_list}", "Production: page-owner",
                     "Change: Numbered moves", "Question: What does <this> change?"):
            self.assertIn(text, message)
        question = change_page.DeliveryStatus(self.root).slots(self.page, "C1").question
        if not isinstance(question, change_page.OpenQuestion):
            self.fail("the sent question must remain open")
        self.assertIn(f"Question id: {question.question_id}", message.splitlines())
        self.assertFalse(any(row.get("Verdict") for row in change_review.review_rows(self.review_list)))

    def test_answer_question_restores_the_row_and_shows_the_answer_before_another_question_or_verdict(self) -> None:
        path, forms, body = self.browser()
        del body
        self.delivery_done.clear()
        self.assertEqual(self.http_request(path, self.browser_fields(forms, "page-owner/C1", "question", "Which groups?")).status, 200)
        self.assertTrue(self.delivery_done.wait(TEST_HANG_GUARD_SECS))
        result = self.answer_question("The <numbered> groups.")
        self.assertEqual(result.returncode, 0, result.stderr)
        browser = change_browser.page_at(self.root, path)
        prepared = change_browser.prepare(browser, change_page.DeliveryStatus(self.root))
        self.assertIn("page-owner/C1", [item.key for item in prepared.changes if item.waiting])
        self.assertIn("C1", [entry.key for entry in prepared.queues["page-owner"].entries])
        content = self.legacy_browser_reply(path).body
        refreshed = BrowserForms()
        refreshed.feed(content)
        self.assertEqual(refreshed.counts["page-owner"], 3)
        self.assertEqual(refreshed.outcomes["page-owner/C1"], ["works", "not works", "validate", "question", "feedback"])
        start = content.index('data-key="page-owner/C1"')
        section = content[start:content.index('</section>', start)]
        self.assertIn('<h2>Question</h2><p>Which groups?</p><h2>Answer</h2><p>The &lt;numbered&gt; groups.</p>', section)
        self.assertLess(section.index('<h1>Numbered moves</h1>'), section.index('<h2>Question</h2>'))
        self.delivery_done.clear()
        self.assertEqual(self.http_request(path, self.browser_fields(refreshed, "page-owner/C1", "question", "Does it include nested groups?")).status, 200)
        self.assertTrue(self.delivery_done.wait(TEST_HANG_GUARD_SECS))
        self.assertEqual(self.answer_question("Yes, it includes nested groups.").returncode, 0)
        self.delivery_done.clear()
        self.assertEqual(self.http_request(path, self.browser_fields(refreshed, "page-owner/C1", "works")).status, 200)
        self.assertTrue(self.delivery_done.wait(TEST_HANG_GUARD_SECS))
        self.assertEqual(next(row["Verdict"] for row in change_review.review_rows(self.review_list) if row["#"] == "C1"), "works")

    @process_boundary
    def test_answer_question_refuses_missing_questions_and_changes_nothing_on_an_identical_rerun(self) -> None:
        result = self.answer_question("There is no question.")
        self.assertEqual(result.returncode, 1)
        self.assertIn("no open question", result.stderr)
        status = change_page.DeliveryStatus(self.root)
        _ = status.ask(self.page, "C1", "Which groups?")
        self.assertEqual(self.answer_question("All groups.").returncode, 0)
        record = status.path(self.page, "C1")
        before = (record.read_bytes(), record.stat().st_mtime_ns, self.review_list.read_bytes())
        result = self.answer_question("All groups.")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((record.read_bytes(), record.stat().st_mtime_ns, self.review_list.read_bytes()), before)
        self.assertEqual(self.answer_question("Some groups.").returncode, 1)
        self.assertEqual(record.read_bytes(), before[0])

    def test_answer_question_accepts_current_and_list_ids_without_changing_delivery_keys(self) -> None:
        runner = fixture_member("showrunner", name="sdlc", doc=staff.LiveDoc(local_machine(), self.production))
        _ = staff.move(self.unit.id, staff.ReportsTo(runner.id))
        status = change_page.DeliveryStatus(self.root)
        question = status.ask(self.page, "C1", "Which groups?")
        assert isinstance(question, change_page.QuestionDelivery)
        for identity in ("sdlc/C1", "page-owner/C1"):
            result = self.answer_question("All groups.", identity, question_id=question.question_id)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("sdlc/C1", result.stdout)
        slots = status.slots(self.page, "C1")
        self.assertIsInstance(slots.question, change_page.AnsweredQuestion)
        self.assertFalse(status.path(self.page, "sdlc/C1").exists())

    def test_selected_row_links_accept_current_and_list_ids_with_local_storage_keys(self) -> None:
        runner = fixture_member("showrunner", name="sdlc", doc=staff.LiveDoc(local_machine(), self.production))
        _ = staff.move(self.unit.id, staff.ReportsTo(runner.id))
        before = self.review_list.read_bytes()
        for identity in ("sdlc/C1", "page-owner/C1"):
            with self.subTest(identity=identity):
                result = self.command(change_page, change_page.main, ["--production", "page-owner",
                    "--list", str(self.review_list), identity])
                self.assertEqual(result.returncode, 0, result.stderr)
                page = change_page.page_at(self.root, urlsplit(result.stdout.strip()).path)
                self.assertEqual((page.review_list, page.rows), (self.review_list, ("C1",)))
                self.assertIn('<dt>change:</dt><dd>sdlc/C1</dd>', change_page.render(page, change_page.DeliveryStatus(self.root)).decode())
        self.assertEqual(self.review_list.read_bytes(), before)

    def test_selected_row_link_resolves_the_productions_selected_legacy_list(self) -> None:
        legacy = self.review_list.with_name("legacy-review.md")
        _ = self.review_list.rename(legacy)
        _ = self.production.write_text(self.production.read_text().replace(str(self.review_list), str(legacy)))
        runner = fixture_member("showrunner", name="sdlc", doc=staff.LiveDoc(local_machine(), self.production))
        _ = staff.move(self.unit.id, staff.ReportsTo(runner.id))
        before = legacy.read_bytes()
        result = self.command(change_page, change_page.main, ["--production", "sdlc", "sdlc/C1"])
        self.assertEqual(result.returncode, 0, result.stderr)
        page = change_page.page_at(self.root, urlsplit(result.stdout.strip()).path)
        self.assertEqual((page.review_list, page.rows), (legacy, ("C1",)))
        self.assertIn('<dt>change:</dt><dd>sdlc/C1</dd>', change_page.render(page, change_page.DeliveryStatus(self.root)).decode())
        self.assertEqual(legacy.read_bytes(), before)
        self.assertFalse(self.review_list.exists())

    def test_question_posts_refuse_empty_and_blank_text_without_saving_or_sending(self) -> None:
        path, forms, body = self.browser()
        del body
        before = self.review_list.read_bytes()
        for text in ("", "   ", "\n\n", " \r\n \u2028"):
            with self.subTest(text=text):
                reply = self.http_request(path, self.browser_fields(forms, "page-owner/C1", "question", text))
                self.assertEqual(reply.status, 400)
        reply = self.http_request(path, self.browser_fields(forms, "page-owner/C1", "feedback", "\n \u2028"))
        self.assertEqual(reply.status, 400)
        self.assertEqual(self.review_list.read_bytes(), before)
        self.assertEqual(self.messages, [])
        self.assert_record_cleared()

    def test_feedback_textareas_use_one_live_row_setting_per_page_render(self) -> None:
        settings = self.temporary / "config/review_links.conf"
        browser = change_page.ReviewBrowser("test")
        status = change_page.DeliveryStatus(self.root)
        renderers: tuple[Callable[[], bytes], ...] = (
            lambda: change_page.render(self.page, status),
            lambda: change_browser.render(browser, status),
            lambda: change_page.fallback_answer(self.page, change_page.AnswerNotice("C1", "Try again")),
        )
        for height in (5, 8):
            _ = write_config_fixture(settings, f"feedback_rows={height}\n")
            for render in renderers:
                with self.subTest(height=height, render=render), patch.object(conf, "read", wraps=conf.read) as read:
                    content = render().decode()
                    self.assertIn(f'<textarea id="feedback-C1" name="feedback" rows="{height}">\n</textarea>', content)
                    self.assertIn('label,textarea{display:block}', content)
                    self.assertIn('resize:vertical', content)
                    self.assertEqual(sum(call.args == ("review_links",) for call in read.call_args_list), 1)

        for interval in (15, 5, 300):
            with self.subTest(page_refresh_seconds=interval):
                _ = write_config_fixture(settings,
                    f"page_refresh_seconds={interval}\ncard_fade_out_ms=365\ncard_fade_in_ms=725\n")
                path, _, content = self.browser()
                del path
                self.assertIn(f'data-refresh-seconds="{float(interval)}"', content)
                self.assertIn('data-card-fade-out-ms="365" data-card-fade-in-ms="725"', content)
                # An open page reloads, not updates, when the server's page script is newer than its own.
                self.assertIn(f'data-script="{change_browser.SCRIPT_VERSION}"', content)
                self.assertIn("if(fresh.dataset.script!==content.dataset.script){location.reload();return;}", content)
        for interval in ("4", "301", "nan"):
            with self.subTest(page_refresh_seconds=interval, range="invalid"):
                _ = write_config_fixture(settings, f"page_refresh_seconds={interval}\n")
                with self.assertRaises(conf.Unusable), conf.diagnostics(report=False):
                    _ = conf.read(settings).number("page_refresh_seconds")

    def test_a_missing_feedback_rows_setting_refuses_the_render(self) -> None:
        settings = self.temporary / "config/review_links.conf"
        _ = settings.write_text("\n".join(line for line in settings.read_text().splitlines()
                                         if line.partition("=")[0].strip() != "feedback_rows") + "\n")
        with self.assertRaisesRegex(conf.Unusable, "feedback_rows.*key is missing"):
            _ = change_page.render(self.page, change_page.DeliveryStatus(self.root))

    def test_multiline_browser_answers_preserve_messages_and_repeat_without_changes(self) -> None:
        path, forms, body = self.browser()
        del body
        original = self.review_list.read_bytes()
        status = change_page.DeliveryStatus(self.root)
        record = status.path(self.page, "C1")
        words = "\n First <line> \r\n\r Second & line\u2028Third\n"
        normalized = "\n First <line> \n\n Second & line\nThird"
        joined = "First <line> Second & line Third"
        for outcome in ("works", "not works", "feedback", "validate", "question"):
            with self.subTest(outcome=outcome):
                _ = self.review_list.write_bytes(original)
                record.unlink(missing_ok=True)
                self.messages.clear()
                fields = self.browser_fields(forms, "page-owner/C1", outcome, words)
                self.delivery_done.clear()
                reply = self.http_request(path, fields)
                self.assertEqual(reply.status, 200, reply.body)
                self.assertTrue(self.delivery_done.wait(TEST_HANG_GUARD_SECS))
                verdict = next(row.get("Verdict", "") for row in change_review.review_rows(self.review_list) if row["#"] == "C1")
                expected = f"{outcome}: {joined}" if outcome in ("works", "not works") else ""
                self.assertEqual(verdict, expected)
                self.assertEqual(json.loads(reply.body), {"answer": expected})
                message = next(message for recipient, _, message in self.messages if recipient == "page-owner")
                self.assertIn(("Question: " if outcome == "question" else "Feedback: " if outcome == "feedback" else "User feedback: ") + normalized, message)
                slots = status.slots(self.page, "C1")
                if outcome == "validate":
                    assert isinstance(slots.validation, change_page.ValidationRequested)
                    self.assertEqual(slots.validation, change_page.ValidationRequested(slots.validation.attempt, normalized))
                if outcome in ("question", "feedback"):
                    self.assertIsInstance(slots.question, change_page.OpenQuestion)
                    assert isinstance(slots.question, change_page.OpenQuestion)
                    self.assertEqual(slots.question.text, normalized)
                    self.assertIn('<p> First &lt;line&gt; \n\n Second &amp; line\nThird</p>',
                                  self.legacy_browser_reply(path).body)
                saved = self.review_list.read_bytes()
                delivery = record.read_bytes() if record.exists() else b""
                messages = list(self.messages)
                self.delivery_done.clear()
                repeated = self.http_request(path, fields)
                if outcome in ("works", "not works"):
                    self.assertEqual(repeated.status, 200)
                    self.assertTrue(self.delivery_done.wait(TEST_HANG_GUARD_SECS))
                else:
                    self.assertEqual(repeated.status, 400)
                    self.assertEqual(json.loads(repeated.body), {"reason": change_page.WITH_SHOWRUNNER})
                    self.assertFalse(self.delivery_done.is_set())
                self.assertEqual(self.review_list.read_bytes(), saved)
                self.assertEqual(record.read_bytes() if record.exists() else b"", delivery)
                self.assertEqual(self.messages, messages)

    def test_a_failed_multiline_save_refills_the_textarea_with_its_leading_blank_line(self) -> None:
        words = "\nFirst <line>\n\nSecond & line"
        before = self.review_list.read_bytes()
        with patch.object(change_page, "shown_rows", side_effect=ValueError("render failed")):
            reply = self.request(self.page_path, self.answer(outcome="not works", feedback=words))
        self.assertEqual(reply.status, 400)
        self.assertEqual(self.review_list.read_bytes(), before)
        self.assertIn('rows="5">\n\nFirst &lt;line&gt;\n\nSecond &amp; line</textarea>', reply.body)
        parser = SavedAnswerForm()
        parser.feed(reply.body)
        self.assertEqual(parser.feedback["C1"], words)
        self.assertEqual(parser.selected_outcomes["C1"], "not works")
        self.assertEqual(self.request(self.page_path, self.answer(outcome="not works", feedback=parser.feedback["C1"])).status, 303)
        row = next(row for row in change_review.review_rows(self.review_list) if row["#"] == "C1")
        self.assertEqual(row["Verdict"], "not works: First <line> Second & line")

    def test_question_sends_after_the_response_and_repeated_posts_do_not_write_or_send_twice(self) -> None:
        path, forms, body = self.browser()
        del body
        fields = self.browser_fields(forms, "page-owner/C1", "question", "Which groups?")
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        deliver = change_page.deliver

        def hold_delivery(delivery: change_page.MessageDelivery | change_page.NoMessageDelivery, status: change_page.DeliveryStatus) -> None:
            if isinstance(delivery, change_page.NoMessageDelivery):
                return
            entered.set()
            if not release.wait(TEST_HANG_GUARD_SECS):
                raise RuntimeError("the question delivery was not released")
            try:
                deliver(delivery, status)
            finally:
                finished.set()

        with patch.object(change_page, "deliver", side_effect=hold_delivery):
            try:
                self.assertEqual(self.http_request(path, fields).status, 200)
                self.assertTrue(entered.wait(TEST_HANG_GUARD_SECS))
                record = change_page.DeliveryStatus(self.root).path(self.page, "C1")
                pending = (record.read_bytes(), record.stat().st_mtime_ns)
                refused = self.http_request(path, fields)
                self.assertEqual(refused.status, 400)
                self.assertEqual(json.loads(refused.body), {"reason": change_page.WITH_SHOWRUNNER})
                self.assertEqual((record.read_bytes(), record.stat().st_mtime_ns), pending)
                self.assertEqual(self.messages, [])
            finally:
                release.set()
                self.assertTrue(finished.wait(TEST_HANG_GUARD_SECS))
        sent = (record.read_bytes(), record.stat().st_mtime_ns)
        self.delivery_done.clear()
        refused = self.http_request(path, fields)
        self.assertEqual(refused.status, 400)
        self.assertEqual(json.loads(refused.body), {"reason": change_page.WITH_SHOWRUNNER})
        self.assertFalse(self.delivery_done.is_set())
        self.assertEqual((record.read_bytes(), record.stat().st_mtime_ns), sent)
        self.assertEqual(len(self.messages), 1)

    def test_a_failed_question_delivery_leaves_the_count_and_preserves_the_question(self) -> None:
        path, forms, body = self.browser()
        del body
        with patch.object(change_page, "send_message", side_effect=ValueError("socket unavailable")):
            self.delivery_done.clear()
            self.assertEqual(self.http_request(path, self.browser_fields(
                forms, "page-owner/C1", "question", "Which <groups>?")).status, 200)
            self.assertTrue(self.delivery_done.wait(TEST_HANG_GUARD_SECS))
        content = self.legacy_browser_reply(path).body
        refreshed = BrowserForms()
        refreshed.feed(content)
        self.assertEqual(refreshed.counts["page-owner"], 2)
        prepared = change_browser.prepare(change_browser.page_at(self.root, path), change_page.DeliveryStatus(self.root))
        self.assertNotIn("page-owner/C1", [item.key for item in prepared.changes if item.waiting])
        self.assertNotIn("C1", [entry.key for entry in prepared.queues["page-owner"].entries])
        self.assertIn("Which &lt;groups&gt;?", content)
        self.assertIn("The showrunner will answer this", content)
        current = change_page.DeliveryStatus(self.root)
        question = current.slots(self.page, "C1").question
        self.assertTrue(change_page.review_question_failed(current, self.page, "C1", question))
        self.assertEqual(refreshed.outcomes["page-owner/C1"], [])
        self.assertEqual(footer_counts.changes(self.production), refreshed.counts["page-owner"])
        self.assert_no_delivery_controls(content)

        with self.subTest(delivery="recovery retries the question after both sends fail"):
            restarted = change_page.DeliveryStatus(self.root)
            pending = restarted.slots(self.page, "C1").question
            assert isinstance(pending, change_page.OpenQuestion)
            self.assertIsInstance(pending.delivery, change_page.DeliveryIntent)
            change_page.recover_pending(restarted)
            change_page.recover_pending(change_page.DeliveryStatus(self.root))
            recovered = restarted.slots(self.page, "C1").question
            self.assertEqual(recovered, replace(pending, delivery=change_page.NoDelivery()))
            self.assertEqual(sum(message.startswith("Change review page-owner/C1: the user asks a question")
                                 for _, _, message in self.messages), 1)

        with self.subTest(delivery="feedback retries through the question path"):
            attempts: list[str] = []
            def fail_once(recipient: str, sender: str, message: str, **_options: str) -> None:
                attempts.append(message)
                if len(attempts) == 1:
                    raise RuntimeError("fixture feedback delivery failed")
                self.capture_message(recipient, sender, message)
            self.delivery_done.clear()
            with patch.object(change_page, "send_message", side_effect=fail_once):
                self.assertEqual(self.http_request(path, self.browser_fields(forms, "page-owner/C3", "feedback", "Add a title")).status, 200)
                self.assertTrue(self.delivery_done.wait(TEST_HANG_GUARD_SECS))
            self.assertEqual(len(attempts), 2)
            self.assertTrue(all("the user sends feedback to act on" in message for message in attempts))
            feedback = change_page.DeliveryStatus(self.root).slots(self.page, "C3").question
            self.assertIsInstance(feedback, change_page.OpenFeedback)
            assert isinstance(feedback, change_page.OpenFeedback)
            self.assertIsInstance(feedback.delivery, change_page.NoDelivery)

    def test_a_failed_question_save_keeps_the_row_waiting(self) -> None:
        path, forms, body = self.browser()
        del body
        with patch.object(change_page.DeliveryStatus, "write_slots", side_effect=OSError("record unavailable")):
            reply = self.http_request(path, self.browser_fields(forms, "page-owner/C1", "question", "Which groups?"))
        self.assertEqual(reply.status, 503)
        refreshed = BrowserForms()
        refreshed.feed(self.legacy_browser_reply(path).body)
        self.assertEqual(refreshed.counts["page-owner"], 3)
        self.assertTrue(refreshed.pending["page-owner/C1"])
        self.assertEqual(self.messages, [])

    def test_an_old_answer_cannot_answer_a_new_question(self) -> None:
        status = change_page.DeliveryStatus(self.root)
        first = status.ask(self.page, "C1", "Which groups?")
        if not isinstance(first, change_page.QuestionDelivery):
            self.fail("a new question must create a message attempt")
        result = self.answer_question("All groups.", question_id=first.question_id)
        self.assertEqual(result.returncode, 0, result.stderr)
        record = status.path(self.page, "C1")
        answered = (record.read_bytes(), record.stat().st_mtime_ns)
        result = self.answer_question("All groups.", question_id=first.question_id)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((record.read_bytes(), record.stat().st_mtime_ns), answered)
        self.assertEqual(self.answer_question("Some groups.", question_id=first.question_id).returncode, 1)
        second = status.ask(self.page, "C1", "Does it include nested groups?")
        if not isinstance(second, change_page.QuestionDelivery):
            self.fail("an answered row must accept a new question")
        self.assertNotEqual(first.question_id, second.question_id)
        pending = (record.read_bytes(), record.stat().st_mtime_ns)
        result = self.answer_question("All groups.", question_id=first.question_id)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(len(result.stderr.splitlines()), 1)
        self.assertIn("question id", result.stderr)
        self.assertEqual((record.read_bytes(), record.stat().st_mtime_ns), pending)
        question = status.slots(self.page, "C1").question
        if not isinstance(question, change_page.OpenQuestion):
            self.fail("the second question must remain open")
        self.assertEqual(question.question_id, second.question_id)
        self.assertEqual(question.text, "Does it include nested groups?")

    def test_a_stale_question_resend_cannot_reopen_an_answered_question(self) -> None:
        path, forms, body = self.browser()
        del body
        status = change_page.DeliveryStatus(self.root)
        self.delivery_done.clear()
        self.assertEqual(self.http_request(path, self.browser_fields(forms, "page-owner/C1", "question", "Which groups?")).status, 200)
        self.assertTrue(self.delivery_done.wait(TEST_HANG_GUARD_SECS))
        record = status.path(self.page, "C1")
        sent = (record.read_bytes(), record.stat().st_mtime_ns)
        retry = self.browser_fields(forms, "page-owner/C1", "retry")
        self.assertEqual(self.http_request(path, retry).status, 400)
        self.assertEqual((record.read_bytes(), record.stat().st_mtime_ns), sent)
        self.assertEqual(self.answer_question("All groups.").returncode, 0)
        answered = (record.read_bytes(), record.stat().st_mtime_ns)
        reply = self.http_request(path, retry)
        self.assertEqual(reply.status, 400)
        self.assertEqual(json.loads(reply.body), {"reason": "Check the answer and submit it again."})
        self.assertEqual((record.read_bytes(), record.stat().st_mtime_ns), answered)
        self.assertEqual(len(self.messages), 1)

    def test_a_pending_question_leaves_the_count_and_repeated_saves_change_nothing(self) -> None:
        path, forms, body = self.browser()
        del forms, body
        status = change_page.DeliveryStatus(self.root)
        first = status.ask(self.page, "C1", "Which groups?")
        self.assertIsInstance(first, change_page.QuestionDelivery)
        record = status.path(self.page, "C1")
        pending = (record.read_bytes(), record.stat().st_mtime_ns)
        self.assertIsInstance(status.ask(self.page, "C1", "Which groups?"), change_page.NoDelivery)
        self.assertEqual((record.read_bytes(), record.stat().st_mtime_ns), pending)
        prepared = change_browser.prepare(change_browser.page_at(self.root, path), change_page.DeliveryStatus(self.root))
        self.assertNotIn("page-owner/C1", [item.key for item in prepared.changes if item.waiting])
        self.assertNotIn("C1", [entry.key for entry in prepared.queues["page-owner"].entries])
        content = self.legacy_browser_reply(path).body
        refreshed = BrowserForms()
        refreshed.feed(content)
        self.assertEqual(refreshed.counts["page-owner"], 2)
        self.assertFalse(refreshed.pending["page-owner/C1"])
        self.assertNotIn("C1", [entry.key for entry in next_item.change_queue(self.production).entries])
        self.assert_no_delivery_controls(content)

    def test_a_fresh_question_post_cannot_retry_a_failed_question(self) -> None:
        path, forms, body = self.browser()
        del body
        fields = self.browser_fields(forms, "page-owner/C1", "question", "Which groups?")
        with patch.object(change_page, "send_message", side_effect=RuntimeError("socket unavailable")):
            self.delivery_done.clear()
            self.assertEqual(self.http_request(path, fields).status, 200)
            self.assertTrue(self.delivery_done.wait(TEST_HANG_GUARD_SECS))
        record = change_page.DeliveryStatus(self.root).path(self.page, "C1")
        failed = (record.read_bytes(), record.stat().st_mtime_ns)
        self.delivery_done.clear()
        refused = self.http_request(path, fields)
        self.assertEqual(refused.status, 400)
        self.assertEqual(json.loads(refused.body), {"reason": change_page.WITH_SHOWRUNNER})
        self.assertFalse(self.delivery_done.is_set())
        self.assertEqual((record.read_bytes(), record.stat().st_mtime_ns), failed)
        self.assertEqual(self.messages, [])

    def test_review_question_checks_the_change_list_before_sending(self) -> None:
        status = change_page.DeliveryStatus(self.root)
        delivery = change_page.submit(self.page, urlencode(self.answer(outcome='question', feedback='Which groups?')), status)
        assert isinstance(delivery, change_page.MessageDelivery)
        self.page.review_list.unlink()
        with patch.object(change_page, "send_to_staff") as send:
            change_page.deliver(delivery, status)
        send.assert_not_called()
        self.assertEqual(status.request_attempts, {})

    def test_review_question_stops_retrying_after_an_answer(self) -> None:
        status = change_page.DeliveryStatus(self.root)
        delivery = change_page.submit(self.page, urlencode(self.answer(outcome='question', feedback='Which groups?')), status)
        assert isinstance(delivery, change_page.MessageDelivery)
        question = status.slots(delivery.page, delivery.row).question
        assert isinstance(question, change_page.OpenQuestion)

        def send(*_args: str, **_options: str) -> None:
            self.assertTrue(status.answer_question(delivery.page, delivery.row, question.question_id, "All groups."))
            raise RuntimeError("The fixture sender is offline.")

        with patch.object(change_page, "send_to_staff", side_effect=send) as transport:
            change_page.deliver(delivery, status)
        transport.assert_called_once()
        self.assertIsInstance(status.slots(delivery.page, delivery.row).question, change_page.AnsweredQuestion)
        self.assertEqual(status.request_attempts, {})

    def test_review_question_keeps_a_pending_send_for_recovery(self) -> None:
        status = change_page.DeliveryStatus(self.root)
        delivery = change_page.submit(self.page, urlencode(self.answer(outcome='question', feedback='Which groups?')), status)
        assert isinstance(delivery, change_page.MessageDelivery)
        pending = send_result.ConfirmedSendOutcome(send_result.OutcomeHeader("pending", "Unconfirmed.", ""))
        skipped = send_result.ConfirmedSendOutcome(send_result.OutcomeHeader("skipped", "Already received.", ""))
        with (patch.object(change_page, "send_message", new=SEND_MESSAGE),
              patch.object(unrouted_alert, "message_outcome", return_value=(pending, pending.header.detail)) as transport):
            change_page.deliver(delivery, status)
            self.assertEqual(transport.call_count, 3 * len(delivery.recipients))
            sends = transport.call_args_list.copy()
            question = status.slots(delivery.page, delivery.row).question
            assert isinstance(question, change_page.OpenQuestion)
            self.assertIsInstance(question.delivery, change_page.DeliveryIntent)
            self.assertTrue(status.has_deferred_questions())

            transport.reset_mock(return_value=True)
            transport.return_value = (skipped, skipped.header.detail)
            restarted = change_page.DeliveryStatus(self.root)
            change_page.recover_pending(restarted)
            change_page.recover_deferred_questions(status)

        self.assertEqual(transport.call_args_list, sends[:len(delivery.recipients)])
        self.assertEqual(status.slots(delivery.page, delivery.row).question,
                         replace(question, delivery=change_page.NoDelivery()))

    def test_review_question_configuration_refusal_releases_its_claim(self) -> None:
        status = change_page.DeliveryStatus(self.root)
        delivery = change_page.submit(self.page, urlencode(self.answer(outcome='question', feedback='Which groups?')), status)
        assert isinstance(delivery, change_page.MessageDelivery)
        with (patch.object(conf, "read", side_effect=OSError("Cannot read the retry settings.")),
              patch.object(change_page, "send_to_staff") as send, redirect_stderr(StringIO()) as errors):
            change_page.deliver(delivery, status)
        send.assert_not_called()
        question = status.slots(delivery.page, delivery.row).question
        self.assertTrue(change_page.review_question_failed(status, delivery.page, delivery.row, question))
        self.assertEqual(status.request_attempts, {})
        self.assertEqual(errors.getvalue(), "review-links question C1: Cannot read the retry settings.\n")

    def test_a_server_restart_sends_a_question_interrupted_before_the_send(self) -> None:
        status = change_page.DeliveryStatus(self.root)
        fields = self.answer(outcome='question', feedback='Which groups?')
        delivery = change_page.submit(self.page, urlencode(fields), status)
        assert isinstance(delivery, change_page.MessageDelivery)
        with (patch.object(change_page, 'send_to_staff', side_effect=SystemExit),
              self.assertRaises(SystemExit)):
            change_page.deliver(delivery, status)
        self.assertEqual(self.messages, [])
        closeout = status.closeout_fields(delivery.page, delivery.row)
        self.assertEqual(closeout.get('question_dispatch'), {'attempt': delivery.attempt, 'state': 'started'})
        pending = status.slots(delivery.page, delivery.row).question
        assert isinstance(pending, change_page.OpenQuestion)
        assert isinstance(pending.delivery, change_page.DeliveryIntent)
        self.assertFalse(status.has_review_save_receipt(delivery.page, delivery.row, pending.delivery))

        restarted = change_page.DeliveryStatus(self.root)
        change_page.recover_pending(restarted)
        change_page.recover_pending(restarted)
        self.assertEqual(len(self.messages), 1)
        self.assertIn('Question id: '+pending.question_id, self.messages[0][2])
        self.assertEqual(restarted.slots(delivery.page, delivery.row).question,
                         replace(pending, delivery=change_page.NoDelivery()))

    def test_a_server_restart_delivers_a_pending_question_once(self) -> None:
        status = change_page.DeliveryStatus(self.root)
        question = status.ask(self.page, "C1", "Which groups?")
        assert isinstance(question, change_page.QuestionDelivery)
        self.delivery_done.clear()
        with started_review_server(("127.0.0.1", 0), self.root):
            self.assertTrue(self.delivery_done.is_set())
        saved = status.slots(self.page, "C1").question
        self.assertEqual(saved, change_page.OpenQuestion(question.question_id, "Which groups?", change_page.NoDelivery()))
        self.assertIn("Question id: " + question.question_id, self.messages[0][2])
        change_page.recover_pending(status)
        self.assertEqual(len(self.messages), 1)

        with self.subTest(recovery="feedback uses the question record"):
            feedback = status.ask(self.page, "C3", "Use a larger title", kind="feedback")
            assert isinstance(feedback, change_page.QuestionDelivery)
            count = len(self.messages)
            self.delivery_done.clear()
            with started_review_server(("127.0.0.1", 0), self.root):
                self.assertTrue(self.delivery_done.is_set())
            recovered = change_page.DeliveryStatus(self.root).slots(self.page, "C3").question
            self.assertIsInstance(recovered, change_page.OpenFeedback)
            self.assertEqual(len(self.messages), count + 1)
            self.assertIn("the user sends feedback to act on", self.messages[-1][2])
            self.assertIn("Feedback id: " + feedback.question_id, self.messages[-1][2])
            self.assertIn("Feedback: Use a larger title", self.messages[-1][2])
            change_page.recover_pending(change_page.DeliveryStatus(self.root))
            self.assertEqual(len(self.messages), count + 1)

        key = self.file_user_question("Keep the spacing")
        browser = change_page.ReviewBrowser("private-recovery")
        status = change_page.DeliveryStatus(self.root)
        fields = {"row": key, "token": change_browser.row_token(browser, key), "outcome": "answer", "feedback": "Keep it"}
        delivery, answer = change_browser.submit(browser, urlencode(fields), status)
        self.assertEqual(answer, "Keep it")
        self.assertIsInstance(delivery, change_page.MessageDelivery)
        recovered = change_page.DeliveryStatus(self.root)
        before = len(self.messages)
        change_page.recover_pending(recovered)
        self.assertEqual(len(self.messages), before + 1)
        self.assertIn("Keep the spacing", self.messages[-1][2])
        change_page.recover_pending(change_page.DeliveryStatus(self.root))
        self.assertEqual(len(self.messages), before + 1)

        with self.subTest(answer="save error before another close"):
            key = self.file_user_question("Do not send a failed answer")
            pending_status = change_page.DeliveryStatus(self.root)
            question = next(item for item in change_browser.prepare(browser, pending_status).questions if item.key == key)
            with patch.object(update_registration, "answer", side_effect=OSError("answer disk unavailable")):
                with self.assertRaisesRegex(OSError, "answer disk unavailable"):
                    _ = change_browser.submit_question(question, "answer", "Failed answer", pending_status)
            page = change_page.ReviewPage(question.record.path, self.production, (question.record.path.stem,), "")
            self.assertIsInstance(pending_status.slots(page, page.rows[0]).verdict, change_page.NoDelivery)
            self.assertEqual(pending_status.request_attempts, {})
            update_registration.write_question_record(replace(question.record, status="closed"))
            count = len(self.messages)
            change_page.recover_question_answers(change_page.DeliveryStatus(self.root))
            self.assertEqual(len(self.messages), count)

        with self.subTest(answer="closed with unknown save state"):
            key = self.file_user_question("Keep an uncertain answer private")
            pending_status = change_page.DeliveryStatus(self.root)
            question = next(item for item in change_browser.prepare(browser, pending_status).questions if item.key == key)
            delivery, _ = change_browser.submit_question(question, "answer", "Unconfirmed answer", pending_status)
            path = pending_status.path(delivery.page, delivery.row)
            saved = cast(change_page.StoredDeliverySlots, json.loads(path.read_text()))
            saved_answer = saved.get("user_answer")
            assert saved_answer is not None
            saved_answer["saved"] = False
            _ = path.write_text(json.dumps(saved))
            count = len(self.messages)
            restarted = change_page.DeliveryStatus(self.root)
            with redirect_stderr(StringIO()) as errors:
                change_page.recover_question_answers(restarted)
            self.assertEqual(errors.getvalue(), f"review-links recovery {path.name}: answer save state unknown; not sent\n")
            self.assertEqual(len(self.messages), count)
            self.assertIsInstance(restarted.slots(delivery.page, delivery.row).verdict, change_page.NoDelivery)
            self.assertEqual(restarted.request_attempts, {})

        with self.subTest(answer="deleted question sends nothing"):
            key = self.file_user_question("Delete this question before recovery")
            pending_status = change_page.DeliveryStatus(self.root)
            question = next(item for item in change_browser.prepare(browser, pending_status).questions if item.key == key)
            _ = change_browser.submit_question(question, "answer", "Do not send this", pending_status)
            question.record.path.unlink()
            count = len(self.messages)
            change_page.recover_pending(change_page.DeliveryStatus(self.root))
            self.assertEqual(len(self.messages), count)

    def test_browser_markup_supplies_waiting_rows_and_navigation_after_saves(self) -> None:
        path, forms, body = self.browser()
        del path
        self.assertEqual(forms.pending, dict.fromkeys(forms.rows, True))
        self.assertEqual(forms.outcomes["page-owner/C1"], ["works", "not works", "validate", "question", "feedback"])
        with self.subTest(button="negative verdict label"):
            self.assertIn('name="outcome" value="not works">Does not work</button>', body)
            self.assertNotIn('>Not works</button>', body)
        self.assertIn('data-next hidden>Skip for now', body)
        self.assertNotIn('data-left', body)
        self.assertIn('<p hidden data-empty></p>', body)
        self.assertIn("empty.hidden=current!==null", body)
        self.assertIn("No questions for this showrunner.", body)
        self.assertIn("No questions to answer.", body)
        self.assertIn("No changes for this showrunner.", body)
        self.assertIn("No changes to review.", body)
        self.assertIn("current===row && (tab==='' || tab===row.dataset.tab) && navigation===position", body)
        self.assertIn("motion.settled().then(()=>{if(row.dataset.pending==='false') form.hidden=true;});", body)
        self.assertIn("go(following?.dataset.key || '')", body)
        self.assertNotIn("rows.push(row)", body)
        with self.subTest(refresh="visibility, held content, and position"):
            for rule in ("document.visibilityState!=='visible'", "void refresh();",
                         "createPageRefresh(refreshPage,()=>content.dataset.refreshSeconds,()=>", "pageRefresh.restart()",
                         "document.addEventListener('visibilitychange'", "fetch(location.href,{cache:'no-store'})",
                         "new DOMParser()", "content.replaceWith(fresh)", "fresh.outerHTML===shownPage",
                         "if(!heldPage || activeSaves", "field.value!==''", "document.addEventListener('input'",
                         "details[open]", "field.setSelectionRange(...selection)", "window.scrollTo({left,top,behavior:'instant'})",
                         "row.dataset.key===current?.dataset.key", "ordered.slice(previous+1)"):
                self.assertIn(rule, body)
            # The one full reload is for a page whose script is older than the server's.
            self.assertEqual(body.count('location.reload'), 1)
            self.assertIn('if(fresh.dataset.script!==content.dataset.script){location.reload();return;}', body)
        with self.subTest(refresh="one automatic load and a replacement load after saving"):
            self.assertIn("activeLoads && !supersede", body)
            self.assertIn("activeLoads=true", body)
            self.assertIn("finally{if(request===newestRequest) activeLoads=false;}", body)
            self.assertIn("heldPage=null;newestRequest++;pageRefresh.refresh(true)", body)
            self.assertIn("if(request!==newestRequest || !current()) return", body)
            self.assertIn("if(!fresh) throw new Error('The page has no review list.')", body)
        with self.subTest(refresh="a visible tab replaces an active load"):
            self.assertIn("async function refreshPage(supersede=false,current=()=>true)", body)
            self.assertIn("document.addEventListener('visibilitychange',()=>{restart();void refresh(true);})", body)
            self.assertIn("if(hidden()){draw();return;}", body)
        with self.subTest(navigation="one kind and a showrunner menu"):
            self.assertNotIn('role="tablist"', body)
            self.assertIn('data-kind="changes"', body)
            self.assertIn("kind=content.dataset.kind", body)
            self.assertIn('id="review-showrunner" aria-label="Showrunner"', body)
            self.assertIn("runnerMenu.replaceChildren(new Option('All ('", body)
            self.assertIn("tab=place.get('showrunner') || ''", body)
            self.assertIn("tab==='' || r.dataset.tab===tab", body)
            self.assertIn("runnerMenu.value=tab", body)
        with self.subTest(controls="labels and action groups"):
            self.assertIn('<label for="review-showrunner">Showrunner:</label>', body)
            self.assertIn('>Comment, question or feedback</label>', body)
            self.assertIn('value="validate">Ask showrunner to check</button>', body)
            self.assertIn('<div class="answer-actions"><button name="outcome" value="works">Works</button>'
                          + '<button name="outcome" value="not works">Does not work</button>'
                          + '<button name="outcome" value="validate">Ask showrunner to check</button>', body)
            self.assertIn('value="feedback">Send feedback</button></div></form>', body)
            self.assertNotIn('secondary-actions', body)
        with self.subTest(navigation="Skip joins the action row"):
            self.assertIn("target?.querySelector('form:not([hidden]) .answer-actions')", body)
            self.assertIn('.navigation:empty{display:none}', body)
            self.assertNotIn('position:sticky', change_browser.PAGE_STYLE)
        with self.subTest(card="title and metadata"):
            card = body.split('<section hidden data-type="changes"', 1)[1].split('</section>', 1)[0]
            proof, metadata = card.split('<dl class="change-details">', 1)
            details = metadata.split('</dl>', 1)[0]
            self.assertIn('<h1>Numbered moves</h1>', proof)
            self.assertIn('<h2>How to check</h2>', proof)
            self.assertLess(proof.index('<h2>How to check</h2>'), proof.index('</form>'))
            self.assertIn('<dt>change:</dt><dd>page-owner/C1</dd>', details)
            self.assertIn('<dt>date:</dt><dd>2026-11-01</dd>', details)
            self.assertIn(f'<dt>commit:</dt><dd>{self.git("rev-parse", "HEAD")}</dd>', details)
            self.assertIn('<dt>kind:</dt><dd>See in action</dd>', details)
            self.assertNotIn('<dt>time:</dt>', details)
            self.assertNotIn('<details', card)

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required for browser behavior")
    def test_skip_save_and_empty_states_in_the_browser(self) -> None:
        settings = self.temporary / "config/review_links.conf"
        _ = write_config_fixture(settings, "card_fade_out_ms=25\ncard_fade_in_ms=35\n")
        for text in ("First question", "Second question"):
            _ = self.file_user_question(text, question="Pick one.\n\nChoices:\n1. Compact\n2. Wide")
        other = self.temporary / "change-review - all.md"
        document = self.temporary / "all.md"
        _ = document.write_text(f"# All\n- **Showrunner checkout:** `{self.repo}`\n- **Change review:** `{other}`\n")
        owner = fixture_member("showrunner", name="all", doc=staff.LiveDoc(local_machine(), document))
        _ = change_review.append_rows(other, pictured_changes([change_review.Visible("Another production", "Read", "Proof")]),
                                      date(2026, 11, 2), "", self.production, producers=[owner.id], record=staff.snapshot())
        status = change_page.DeliveryStatus(self.root)
        feedback = status.ask(self.page, "C3", "Make the proof clearer.", kind="feedback")
        assert isinstance(feedback, change_page.QuestionDelivery)
        self.assertTrue(status.answer_question(self.page, "C3", feedback.question_id, "The proof has three steps."))
        urgency.pin(next_item.change_queue(self.production).name, "C3")
        probe = r'''
<script>
(async()=>{
 const failures=[], check=(value,label)=>{if(!value) failures.push(label);};
 const realFetch=window.fetch;
 const mode=new URLSearchParams(location.search).get('scheme');document.documentElement.style.colorScheme=mode;
 try{
  const navigate=(runner='page-owner')=>{
   history.replaceState(null,'','#'+new URLSearchParams({showrunner:runner}));show();
  };
  const stubSave=outcome=>{
   let sent;
   window.fetch=async(url,request)=>{
    if(request.method!=='POST') return {headers:new Headers(),ok:false};
    sent=request.body;return {headers:new Headers(),ok:true,json:async()=>({answer:outcome})};
   };
   return ()=>sent;
  };
  check(tab==='','the page starts with All');
  check(!content.querySelector('[role=tab]'),'the page has no type tabs');
  check(document.documentElement.scrollWidth<=innerWidth,'the page has no horizontal overflow');
  if(kind==='questions'){
   navigate('');
   check(waiting().length===2 && rows.length===2,'the page holds only the waiting questions');
   const question=current;
   check(!question.querySelector('.kind') && question.firstElementChild.tagName==='H1','question title replaces the Question label');
   check(question.querySelector('.change-details').textContent.includes('source:page-owner · builder-unit'),'question keeps the asker');
   check(!question.textContent.includes('Asked by'),'the question has no Asked by line');
   const form=question.querySelector('form'),box=form.querySelector('textarea');
   check(form.querySelector('label[for="'+box.id+'"]').textContent==='Your answer','the box label is Your answer');
   check(form.querySelector('.answer-help').textContent.startsWith('A choice fills this.'),'the box has its help line');
   check(getComputedStyle(question.querySelector('h1')).fontSize==='20px','card title size');
   const buttons=[...form.querySelectorAll('button[name=outcome]')];
   check(buttons.map(button=>button.textContent).join(',')==='Answer,Followup,Skip','the three question buttons');
   check(new Set(buttons.map(button=>Math.round(button.getBoundingClientRect().top))).size===1,'the three buttons share one row');
   check(buttons[0].disabled && buttons[1].disabled && !buttons[2].disabled,'Answer and Followup wait for text');
   check(getComputedStyle(buttons[0]).opacity<getComputedStyle(buttons[2]).opacity,'a disabled button looks disabled');
   check(next.hidden,'a question shows its own Skip, not Skip for now');
   const radios=[...form.querySelectorAll('input[type=radio][name=choice]')];
   check(radios.length===2,'the choices are a list of radio items');
   radios[0].click();
   check(box.value==='Compact' && !buttons[0].disabled,'a choice fills the box and enables Answer');
   box.value='Use compact';box.dispatchEvent(new Event('input',{bubbles:true}));
   check(!buttons[0].disabled && !buttons[1].disabled,'text enables Answer and Followup');
   const sent=stubSave('answer');
   await save(question,form,'answer');
   check(sent().get('outcome')==='answer' && sent().get('feedback')==='Use compact' && !sent().has('choice'),'the answer sends the shared field');
   check(current!==question && question.dataset.pending==='false' && form.hidden,'the answer advances after saving');
   check(!saveNotice.hidden && saveNotice.textContent==='Answer saved.','question save notice');
   for(const [outcome,notice] of [['followup','Follow-up sent.'],['skip','Skipped.']]){
    for(const row of rows){row.dataset.pending='true';row.querySelector('form').hidden=false;}
    navigate('');const row=current,rowForm=row.querySelector('form');
    rowForm.querySelector('textarea').value=outcome==='skip' ? '' : 'Which file?';
    const posted=stubSave('');
    await save(row,rowForm,outcome);
    check(posted().get('outcome')===outcome,outcome+' sends its outcome');
    check(current!==row && row.dataset.pending==='false' && saveNotice.textContent===notice,outcome+' advances with its notice');
   }
   for(const row of rows) row.dataset.pending='false';
   navigate('');check(!empty.hidden && empty.textContent==='No questions to answer.','questions empty page');
   check(next.hidden,'an empty page hides skip');
  } else {
   navigate('');
   check(tab==='' && runnerMenu.options[0].value==='','All is the first menu option and the default filter');
   check(runnerMenu.options[0].textContent==='All (4)' && waiting().length===4,'All counts every waiting change');
   check(waiting().map(row=>row.dataset.tab).join(',')==='all,page-owner,page-owner,page-owner','All follows the root name order');
   check([...runnerMenu.options].slice(1).map(option=>option.value).join(',')==='all,page-owner,demo','the menu keeps the showrunner order');
   check(rows.every(row=>row.dataset.type==='changes'),'the page holds only changes');
   check(current.dataset.key==='all/C1','All starts with its first ordered change');
   navigate('page-owner');
   check(current.dataset.key==='page-owner/C3','the tab starts with its pinned change');
   check(waiting().map(row=>row.dataset.key).join(',')==='page-owner/C3,page-owner/C1,page-owner/C2','the tab receives the /next order');
   next.click();check(current.dataset.key==='page-owner/C1','Skip follows the selected tab order');
   navigate('');check(current.dataset.key==='all/C1','All retains its own order');
   navigate('all');
   check(tab==='all' && runnerMenu.value==='all' && waiting().length===1 && current.dataset.tab==='all','the showrunner named all has a separate filter');
   runnerMenu.focus();
   const focusUpdate=content.cloneNode(true);focusUpdate.dataset.focusUpdate='menu';heldPage=focusUpdate;applyHeldPage();
   check(document.activeElement.id==='review-showrunner','refresh retains focus on the showrunner menu');
   check(tab==='all' && runnerMenu.value==='all' && waiting().length===1,'refresh retains the showrunner named all');
   current=null;tab='';show();
   check(tab==='all' && waiting().length===1,'a page load retains the showrunner named all');
   runnerMenu.value='demo';runnerMenu.dispatchEvent(new Event('change'));
   check(tab==='demo' && current===null,'the menu selects an empty showrunner');
   check(!empty.hidden && empty.textContent==='No changes for this showrunner.','changes empty group');
   check(next.hidden,'empty group hides skip');
   const refreshed=content.cloneNode(true);refreshed.dataset.testUpdate='new';heldPage=refreshed;applyHeldPage();
   check(tab==='demo' && runnerMenu.value==='demo','an automatic update retains the filter');
   current=null;tab='';show();
   check(tab==='demo','a page load reads the filter from the URL');
   navigate();
   for(const key of ['page-owner/C1','page-owner/C3']){
    go(key);await motion.settled();const actions=[...current.querySelectorAll('button[name=outcome]')];
    check(current.dataset.key!== 'page-owner/C3' || current.textContent.includes('Showrunner: The proof has three steps.'),'the feedback note appears above the actions');
    check(getComputedStyle(next.parentElement).position==='static','Skip uses the normal flow');
    check(next.parentElement===actions[0].parentElement && actions.every(button=>button.compareDocumentPosition(next)&Node.DOCUMENT_POSITION_FOLLOWING),'Skip ends the action row');
    check(current.querySelector('.change-details').getBoundingClientRect().top>=next.getBoundingClientRect().bottom,'the details follow the buttons');
    for(const button of actions){
     button.scrollIntoView({block:'center'});const rect=button.getBoundingClientRect();
     check([[rect.left+2,rect.top+2],[rect.right-2,rect.bottom-2]].every(([x,y])=>button.contains(document.elementFromPoint(x,y))),'the full '+button.textContent+' button is visible on '+key);
    }
   }
   await realFetch('/measurements',{method:'POST',body:JSON.stringify({mode,width:innerWidth,actionViews:['initial','feedback note']})});
   window.scrollTo(0,0);navigate();
   const first=current, pending=waiting().length;
   next.click();
   check(current!==first && waiting().length===pending && first.dataset.pending==='true','skip keeps the change open');
   for(const row of rows) row.dataset.pending=String(row===first);
   go(first.dataset.key);check(next.hidden,'one waiting change hides skip');
   first.dataset.pending='false';
   const other=rows.find(r=>r.dataset.tab===tab && r!==first);other.dataset.pending='true';show();
   check(!next.hidden,'a saved card can skip to another waiting change');
   for(const [outcome,comment] of [['works',''],['works','Clear numbers'],['not works','Still small'],
     ['validate','Check the number'],['question','Which number?'],['feedback','Use larger numbers']]){
    for(const row of rows){row.dataset.pending='true';const form=row.querySelector('form');if(form) form.hidden=false;}
    navigate();check(current!==null,'the card exists before '+outcome+' with filter '+tab);
    const row=current,form=row.querySelector('form');form.querySelector('textarea').value=comment;
    const sent=stubSave(outcome);
    await save(row,form,outcome);
    check(sent().get('outcome')===outcome && sent().get('feedback')===comment,outcome+' sends the shared field');
    check(current!==row && row.dataset.pending==='false' && form.hidden,outcome+' advances after saving');
    check(!saveNotice.hidden && saveNotice.textContent==='Review saved.',outcome+' save notice');
    next.click();check(saveNotice.hidden,'skip clears the previous save notice');
   }
   for(const row of rows){row.dataset.pending='true';const form=row.querySelector('form');if(form) form.hidden=false;}
   navigate();const row=current,form=row.querySelector('form'),field=form.querySelector('textarea');
   field.value='Keep <these> words';
   let answer;
   window.fetch=()=>new Promise(resolve=>{answer=resolve;});
   const saving=save(row,form,'not works');
   check(current!==row && row.dataset.pending==='false','Does not work shows the next change before the server answers');
   check(!saveNotice.hidden && saveNotice.textContent==='Saving review.','the notice says the review is saving');
   const shown=current;
   answer({headers:new Headers(),ok:false,json:async()=>({reason:'Fixture save failed.'})});await saving;
   check(current===shown,'a failed save does not move the shown change');
   check(row.dataset.pending==='true' && !form.hidden,'a failed save puts the change back in the list');
   check(field.value==='Keep <these> words','failed save keeps the text');
   check(row.querySelector('[data-save-state]').textContent==='Not saved: Fixture save failed.','failed save shows the reason');
   check(row.dataset.shown && row.querySelector('.change-details dd').textContent===row.dataset.shown,'the notice uses the change id the card shows');
   check(saveNotice.textContent==='Not saved: '+row.dataset.shown+'. Fixture save failed. It is back in the list. Open it','the notice names the change that was not saved');
   saveNotice.querySelector('button').click();
   check(current===row && saveNotice.hidden,'Open it shows the change again');
   for(const row of rows) row.dataset.pending='false';
   navigate('');check(!empty.hidden && empty.textContent==='No changes to review.','changes empty page');
   first.dataset.pending='true';go(first.dataset.key);
   check(parseFloat(getComputedStyle(first.querySelector('h2')).fontSize)<20,'section labels are smaller than the title');
   check(!first.querySelector('details') && first.querySelector('.change-details').textContent.startsWith('change:'),'the details are always shown');
  }
 }catch(error){failures.push(String(error)+'\n'+(error.stack || ''));}
 await realFetch('/results'+location.search,{method:'POST',body:JSON.stringify(failures)});
 parent.postMessage('finished',location.origin);
})();
</script>'''
        cases = (("changes", "light"), ("changes", "dark"), ("questions", "light"))
        pages = {kind: self.browser(kind)[2].replace('</body>', probe + '</body>').encode()
                 for kind in ("changes", "questions")}
        urls = [f"/case?scheme={mode}&kind={kind}" for kind, mode in cases]
        page = f"""<!doctype html><meta charset="utf-8">
        <style>body{{margin:0}}iframe{{display:block;border:0;width:390px;height:1200px}}</style>
        <iframe title="Review probe"></iframe><img src="/probe-complete" alt="" hidden>
        <script>
        const urls={json.dumps(urls)}, frame=document.querySelector('iframe');
        let next=0;
        const load=()=>{{if(next<urls.length)frame.src=urls[next++];}};
        addEventListener('message', event=>{{
          if(event.origin===location.origin && event.source===frame.contentWindow && event.data==='finished')load();
        }});
        load();
        </script>""".encode()
        results: dict[tuple[str, str], list[list[str]]] = {}
        probe_complete = threading.Event()

        class BrowserProbe(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                address = urlsplit(self.path)
                if address.path == "/probe-complete":
                    if not probe_complete.wait(20):
                        self.send_error(504, "The browser checks did not complete.")
                        return
                    self.send_response(204)
                    self.end_headers()
                    return
                if address.path == "/":
                    body = page
                elif address.path == "/case":
                    body = pages[parse_qs(address.query)["kind"][0]]
                else:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                _ = self.wfile.write(body)

            def do_POST(self) -> None:
                payload = self.rfile.read(int(self.headers["Content-Length"]))
                if self.path == "/measurements":
                    print("Review tab measurements:", payload.decode())
                else:
                    query = parse_qs(urlsplit(self.path).query)
                    case = (query["kind"][0], query["scheme"][0])
                    results.setdefault(case, []).append(cast(list[str], json.loads(payload)))
                    if all(case in results for case in cases):
                        probe_complete.set()
                self.send_response(204)
                self.end_headers()

        with ThreadingHTTPServer(("127.0.0.1", 0), BrowserProbe) as server:
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
            thread.start()
            try:
                profile = self.temporary / "browser-profile"
                profile.mkdir()
                run = run_process_group(["firefox", "--headless", "--no-remote", "--profile", str(profile),
                                      "--window-size", "390,1200", "--screenshot", str(self.temporary / "browser.png"),
                                      f"http://127.0.0.1:{server.server_port}/"], timeout=45)
                for kind, mode in cases:
                    with self.subTest(kind=kind, mode=mode):
                        self.assertEqual(run.returncode, 0, run.stderr)
                        self.assertEqual(results.get((kind, mode)), [[]],
                                         "The browser must complete every check without a failure.")
            finally:
                server.shutdown()
                thread.join(TEST_HANG_GUARD_SECS)

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required for browser behavior")
    def test_undo_puts_an_early_answer_back_in_the_list_and_skip_sends_no_hold(self) -> None:
        probe = r'''<script>
(async()=>{
 const failures=[],check=(ok,label)=>{if(!ok) failures.push(label);};
 const realFetch=window.fetch.bind(window),host=window.top.document,posts=[];
 const until=async(test,label)=>{
  const end=performance.now()+8000;
  while(!test()){
   if(performance.now()>end) throw new Error(label);
   await new Promise(resolve=>setTimeout(resolve,20));
  }
 };
 window.fetch=async(url,request={})=>{
  if(String(url).endsWith('/undo')) return Response.json({outcome:'undone'});
  if(request.method!=='POST') return new Promise(()=>{});
  const held=new Headers(request.headers).get('X-Undo-Hold');
  posts.push([request.body.get('outcome'),held]);
  if(held==='1') return Response.json({hold:'a'.repeat(24),seconds:30},{status:202,headers:{'X-Send-Hold':'held'}});
  return Response.json({answer:''});
 };
 try{
  if(!current) show();
  const row=current,form=row.querySelector('form'),field=form.querySelector('textarea');
  field.value='Keep these words.';
  const saving=save(row,form,'not works');
  check(current!==row && row.dataset.pending==='false','Does not work shows the next change during the hold.');
  await until(()=>host.querySelector('[data-send-undo] button'),'The Undo toast did not show.');
  check(host.querySelector('[data-send-undo] span').textContent==='Sent to page-owner and builder-unit.',
   'The toast names both receivers.');
  host.querySelector('[data-send-undo] button').click();
  await saving;
  check(row.dataset.pending==='true' && !form.hidden && field.value==='Keep these words.',
   'After Undo the change is back in the list with its words.');
  check(saveNotice.textContent==='Undone: '+row.dataset.shown+'. It is back in the list. Open it',
   'The notice says that the undone change is back in the list.');
  const open=saveNotice.querySelector('button');
  check(open?.textContent==='Open it','The notice has an Open it button.');
  open?.click();
  check(current===row,'Open it shows the change again.');
  await save(row,form,'skip');
  check(JSON.stringify(posts)===JSON.stringify([['not works','1'],['skip',null]]),'Skip sends no hold header.');
 }catch(error){failures.push(String(error));}
 await realFetch('/results',{method:'POST',body:JSON.stringify(failures)});
})();
</script>'''
        page = self.browser("changes")[2].replace('</body>', probe + '</body>')
        self.assertEqual(firefox_probe_results(page.encode()), [[]])

    def test_submit_writes_through_verdict_and_a_fresh_get_reads_the_saved_record(self) -> None:
        reply = self.request(self.page_path, self.answer())
        self.assertEqual((reply.status, reply.location), (303, self.page_path))
        rows = {row["#"]: row for row in change_review.review_rows(self.review_list)}
        self.assertEqual(rows["C1"]["Verdict"], "works")
        self.assertEqual([row["#"] for row in change_review.pending(self.review_list)], ["C2", "C3"])
        reloaded = self.request(self.page_path, {})
        self.assertEqual(reloaded.status, 200)
        self.assertIn("Saved: works", reloaded.body)
        self.assertIn("Each group starts at 1", reloaded.body)
        self.assertIn("Read only", reloaded.body)
        self.assertEqual([recipient for recipient, _, _ in self.messages], ["page-owner"])
        self.assertIn("C1 landed", self.messages[0][2])
        folder = self.root / self.page_path.split("/")[1]
        self.assertNotIn("works", (folder / change_page.MANIFEST).read_text())
        self.assertNotIn("Saved:", (folder / change_page.PAGE_NAME).read_text())
        self.assertEqual(self.request(self.page_path.rsplit("/", 1)[0] + "/" + change_page.MANIFEST, {}).status, 404)

    def test_only_the_rows_shown_on_this_page_can_be_answered(self) -> None:
        before = self.review_list.read_bytes()
        for row in ("C2", "C999", "../C1"):
            with self.subTest(row=row):
                self.assertEqual(self.request(self.page_path, self.answer(row)).status, 400)
        self.assertEqual(self.review_list.read_bytes(), before)
        self.assertEqual(self.messages, [])

    def test_validate_yourself_keeps_the_verdict_unset_and_leaves_the_next_order(self) -> None:
        self.feature_command("propose", "--review", "page-owner/C1", *self.note_arguments("claude", "Numbered moves"))
        proposal = feature_log.state_dir() / "proposals/page-owner/C1.json"
        before_proposal = proposal.read_bytes()
        initial = self.request(self.page_path, {}).body
        self.assertEqual(initial.count('value="validate">Ask showrunner to check'), len(self.page.rows))
        words = "Please check this yourself"
        reply = self.request(self.page_path, self.answer(outcome="validate", feedback=words))
        self.assertEqual((reply.status, reply.location), (303, self.page_path))
        self.assertFalse(change_page.answer_row(self.page, "C1").get("Verdict"))
        self.assertEqual(proposal.read_bytes(), before_proposal)
        self.assertCountEqual([row["#"] for row in change_review.pending(self.review_list)], ["C1", "C2", "C3"])
        queue = next_item.change_queue(self.production)
        self.assertEqual([entry.key for entry in urgency.queue_order(queue.entries, queue.name)], ["C2", "C3"])
        self.assertEqual([recipient for recipient, _, _ in self.messages], ["page-owner"])
        self.assertIn("C1: the user asks you to validate this", self.messages[0][2])
        self.assertIn(words, self.messages[0][2])
        validation = change_page.DeliveryStatus(self.root).slots(self.page, "C1").validation
        assert isinstance(validation, change_page.ValidationRequested)
        message_lines = self.messages[0][2].splitlines()
        self.assertIn(f"Validation id: {validation.attempt}", message_lines)
        command = message_lines[message_lines.index("Answer with this command:") + 1]
        self.assertEqual(shlex.split(command), [
            "~/.claude/scripts/lib/py", "~/.claude/scripts/production/change_review.py", "verdict",
            "--production", "page-owner", "--list", str(self.review_list), "--row", "C1",
            "--validation", validation.attempt, "--verdict", "<works or problem>",
        ])
        page = self.request(self.page_path, {}).body
        self.assertNotIn("changes left", page)
        self.assertIn('value="next">Skip for now', page)
        self.assertNotIn("Saved:", page)
        self.assert_no_delivery_controls(page)
        following = self.request(self.page_path, {"outcome": "next", "token": self.page.token})
        self.assertEqual(following.status, 303)
        self.assertEqual(change_page.page_at(self.root, urlsplit(following.location).path).rows, ("C2",))

    def test_a_refused_validation_request_does_not_wait_for_delivery(self) -> None:
        with (patch.object(self, "http_request", return_value=Reply(400, "refused", "")),
              patch.object(threading.Event, "wait", side_effect=AssertionError("delivery did not start")) as wait):
            with self.assertRaisesRegex(AssertionError, "400 != 303"):
                self.test_a_failed_validation_message_keeps_the_saved_feedback()
            wait.assert_not_called()

    def test_a_failed_validation_message_keeps_the_saved_feedback(self) -> None:
        words = "Check <this> & keep the proof"
        sent: list[str] = []
        started, release = threading.Event(), threading.Event()
        send = self.fail_all_but_alerts(sent)
        def paused_send(recipient: str, sender: str, message: str, **_options: str) -> None:
            started.set()
            if not release.wait(TEST_HANG_GUARD_SECS):
                raise RuntimeError("test did not release delivery")
            send(recipient, sender, message)
        with patch.object(change_page, "send_message", side_effect=paused_send):
            try:
                reply = self.http_request(self.page_path, self.answer(outcome="validate", feedback=words))
                self.assertEqual(reply.status, 303)
                self.assertTrue(started.wait(TEST_HANG_GUARD_SECS))
                self.assertFalse(self.delivery_done.is_set())
                body = self.http_request(self.page_path, {}).body
                self.assertNotIn(str(self.temporary), body)
                self.assertNotIn("Saved:", body)
            finally:
                release.set()
                if started.is_set():
                    self.assertTrue(self.delivery_done.wait(TEST_HANG_GUARD_SECS))
        self.assertEqual(len(sent), 3)
        self.assertFalse(change_page.answer_row(self.page, "C1").get("Verdict"))
        self.assertEqual(self.messages, [])
        self.assertTrue(all(words in message for message in sent))
        content = self.request(self.page_path, {}).body
        self.assertIn("The showrunner will validate this", content)
        self.assert_no_delivery_controls(content)

    def test_validate_yourself_refuses_a_saved_row_without_moving_it(self) -> None:
        self.assertEqual(self.request(self.page_path, self.answer()).status, 303)
        before = self.review_list.read_bytes()
        queue = next_item.change_queue(self.production)
        self.assertEqual(self.request(self.page_path, self.answer(outcome="validate")).status, 400)
        self.assertEqual(self.review_list.read_bytes(), before)
        self.assertEqual(urgency.ended(queue.name), {})
        self.assertEqual([recipient for recipient, _, _ in self.messages], ["page-owner"])

    def test_validate_yourself_moves_the_qualified_row_from_an_earlier_list(self) -> None:
        earlier = self.temporary / "change-review - demo.md"
        demo = fixture_member("showrunner", name="demo")
        _ = staff.set_doc(demo.id, staff.LiveDoc(local_machine(), self.production))
        _ = change_review.append_rows(earlier, pictured_changes([change_review.Visible("Earlier change", "Run it", "Proof")]),
                                      date(2026, 11, 1), "", self.production, producers=[demo.id], record=staff.snapshot())
        _ = change_review.set_row_kind(earlier, "C1", "Read only", self.production)
        path = urlsplit(change_page.publish(self.production, earlier, ["C1"])).path
        page = change_page.page_at(self.root, path)
        fields = dict(self.answer(outcome="validate"), token=page.token)
        self.assertEqual(self.request(path, fields).status, 303)
        self.assertFalse(change_page.answer_row(page, "C1").get("Verdict"))
        queue = next_item.change_queue(self.production)
        self.assertEqual(set(urgency.ended(queue.name)), {"demo/C1"})
        self.assertNotIn("demo/C1", [entry.key for entry in urgency.queue_order(queue.entries, queue.name)])
        self.assertEqual([recipient for recipient, _, _ in self.messages], ["demo"])
        self.assertNotIn("changes left", self.legacy_browser_reply(path).body)

    def test_paths_commands_extra_fields_and_forged_page_tokens_are_refused(self) -> None:
        before = self.review_list.read_bytes()
        for fields in (dict(self.answer(), path=str(self.production)), dict(self.answer(), command="touch /tmp/no"),
                       dict(self.answer(), token="wrong"), dict(self.answer(), row=str(self.review_list))):
            with self.subTest(fields=fields):
                self.assertEqual(self.request(self.page_path, fields).status, 400)
        self.assertEqual(self.request(self.page_path + "?path=/tmp/no", self.answer()).status, 400)
        self.assertEqual(self.request("/unpublished/changes.html", self.answer()).status, 404)
        self.assertEqual(self.review_list.read_bytes(), before)
        self.assertEqual(self.messages, [])

    def test_write_path_refuses_a_non_tailnet_bind_and_a_lost_address(self) -> None:
        before = self.review_list.read_bytes()
        with patch.object(serve, "tailscale_ip", return_value="100.64.0.99"):
            self.assertEqual(self.request(self.page_path, self.answer()).status, 403)
        with patch.object(serve, "tailscale_ip", side_effect=OSError("address lost")):
            self.assertEqual(self.request(self.page_path, self.answer()).status, 403)
        self.assertEqual(self.review_list.read_bytes(), before)
        self.assertEqual(self.messages, [])

    def test_not_works_and_free_text_follow_the_stored_producer_route(self) -> None:
        for row, outcome, feedback, verdict in (("C1", "not works", "", "not works"),
                                               ("C2", "not works", "café | <still tiny>",
                                                "not works: café | <still tiny>")):
            with self.subTest(outcome=outcome):
                path = urlsplit(change_page.publish(self.production, self.review_list, [row])).path
                page = change_page.page_at(self.root, path)
                fields = dict(self.answer(row, outcome, feedback), token=page.token)
                self.assertEqual(self.request(path, fields).status, 303)
                self.assertEqual([recipient for recipient, _, _ in self.messages[-2:]],
                                 ["builder-unit", "page-owner"])
                for _, _, message in self.messages[-2:]:
                    self.assertIn(feedback or "not works", message)
                    self.assertIn("Numbered moves", message)
                    self.assertIn(str(self.review_list), message)
                saved = next(item for item in change_review.review_rows(self.review_list) if item["#"] == row)
                self.assertEqual(saved["Verdict"], verdict)
                self.assertIn("Saved: " + verdict.replace("<", "&lt;").replace(">", "&gt;"),
                              self.legacy_browser_reply(path).body)

    def test_not_works_with_words_preserves_them_and_returns_to_the_unit(self) -> None:
        words = "I still see the internal ids"
        self.assertEqual(self.request(self.page_path, self.answer(outcome="not works", feedback=words)).status, 303)
        self.assertEqual([target for target, _, _ in self.messages], ["builder-unit", "page-owner"])
        self.assertIn("C1 returns to its unit", self.messages[0][2])
        self.assertIn("User feedback: " + words, self.messages[0][2])
        self.assertIn("Saved: not works: " + words, self.request(self.page_path, {}).body)

    def test_a_change_outside_a_unit_returns_to_its_production_session(self) -> None:
        words = "The proof needs the actual test output"
        self.assertEqual(self.request(self.page_path, self.answer("C3", "not works", words)).status, 303)
        self.assertEqual([target for target, _, _ in self.messages], ["builder-unit", "page-owner"])
        # C3 has no matching trailer. Its merge names one unit, which receives the answer.
        self.messages.clear()
        _ = change_review.append_rows(self.review_list, pictured_changes([change_review.Visible("Local docs", "Read", "Proof")]),
                                      date(2026, 11, 1), "", self.production, record=staff.snapshot())
        _ = change_review.set_row_kind(self.review_list, "C4", "Read only", self.production)
        path = urlsplit(change_page.publish(self.production, self.review_list, ["C4"])).path
        page = change_page.page_at(self.root, path)
        self.assertEqual(self.request(path, dict(self.answer("C4", "not works", words), token=page.token)).status, 303)
        self.assertEqual([target for target, _, _ in self.messages], ["page-owner"])

    def test_read_only_change_stays_visible_with_kind_change_proof_and_controls_after_reload(self) -> None:
        body = self.request(self.page_path, {}).body
        self.assertIn('<dt>kind:</dt><dd>See in action</dd>', body)
        self.assertIn('<dt>kind:</dt><dd>Read only</dd>', body)
        self.assertIn("Internal comments clarified", body)
        self.assertIn("The old and new comments agree with the tests", body)
        self.assertIn('id="feedback-C3"', body)
        self.assertIn('value="works">Works', body)
        self.assertEqual(self.request(self.page_path, self.answer("C3")).status, 303)
        reloaded = self.request(self.page_path, {}).body
        self.assertIn("Internal comments clarified", reloaded)
        self.assertIn("The old and new comments agree with the tests", reloaded)
        self.assertIn('<dt>kind:</dt><dd>Read only</dd>', reloaded)
        self.assertIn("Saved: works", reloaded)

    @process_boundary
    def test_command_publishes_one_row_and_a_batch_through_the_same_path(self) -> None:
        for rows in (["C1"], ["C1", "C2", "C3"]):
            with self.subTest(rows=rows):
                result = self.command(change_page, change_page.main,
                                      ["--production", "page-owner", "--list", str(self.review_list), *rows])
                self.assertEqual((result.returncode, result.stderr), (0, ""))
                path = urlsplit(result.stdout.strip()).path
                reply = self.legacy_browser_reply(path)
                self.assertEqual(reply.status, 200)
                for row in ("C1", "C2", "C3"):
                    self.assertEqual(f'<section id="{row}" data-change-card>' in reply.body, row in rows)

    def test_delivery_failure_keeps_the_saved_answer_and_repeated_saves_send_nothing(self) -> None:
        with patch.object(change_page, "send_message", side_effect=RuntimeError("socket unavailable")):
            reply = self.request(self.page_path, self.answer())
        self.assertEqual(reply.status, 303)
        self.assert_no_delivery_controls(self.request(self.page_path, {}).body)
        self.assertIn("Saved: works", self.request(self.page_path, {}).body)
        self.assertEqual(self.request(self.page_path, self.answer()).status, 303)
        self.assertEqual([target for target, _, _ in self.messages], [])
        before = self.review_list.read_bytes()
        self.assertEqual(self.request(self.page_path, self.answer(outcome="not works")).status, 400)
        self.assertEqual(self.review_list.read_bytes(), before)
        self.assertEqual(len(self.messages), 0)

    def feature_command(self, *arguments: str) -> None:
        result = self.command(feature_log, feature_log.main, list(arguments))
        self.assertEqual((result.returncode, result.stderr), (0, ""))

    def note_arguments(self, project: str, title: str) -> list[str]:
        return ["--project", project, "--title", title, "--kind", "feature", "--summary", title,
                "--behavior", "Each group starts at 1", "--see", "Numbers start at 1",
                "--link", self.git("rev-parse", "HEAD"), "--evidence", "The page test passes",
                "--completed", "2026-11-01", *(["--crate", "hana"] if project == "hana" else [])]

    def proposal(self, review: str) -> dict[str, object]:
        path = feature_log.state_dir() / "proposals" / f"{review}.json"
        return cast(dict[str, object], json.loads(path.read_text(encoding="utf-8")))

    def test_a_feature_log_project_saves_when_the_writer_skips_an_absent_proposal(self) -> None:
        _ = self.git("remote", "add", "origin", "https://github.com/hanallc/claude_commands.git")
        configured = self.temporary / "configured-lists"
        configured.mkdir()
        _ = self.enterContext(patch.object(change_review, "lists_directory", return_value=configured))
        published = self.command(change_page, change_page.main,
                                 ["--production", "page-owner", "--list", str(self.review_list), "C1"])
        self.assertEqual((published.returncode, published.stderr), (0, ""))
        path = urlsplit(published.stdout.strip()).path
        page = change_page.page_at(self.root, path)
        cleanup = replace(change_review.PendingVerdictCleanup.for_row(self.root, self.review_list, "C1"),
                          verdict="works", require_git_commit=False)
        with (patch.object(subprocess, "run", wraps=subprocess.run) as commands,
              patch.object(feature_log, "project_of", wraps=feature_log.project_of) as project,
              patch.object(feature_log, "land_review", wraps=feature_log.land_review) as land,
              patch.object(change_review, "save_verdict", wraps=change_review.save_verdict) as save,
              patch.object(scratch, "end", side_effect=conf.Unusable("scratch settings unavailable"))):
            reply = self.request(path, dict(self.answer(), token=page.token))
        self.assertEqual((reply.status, reply.location), (303, path))
        invocations = [cast(list[str], call.args[0]) for call in commands.call_args_list]
        self.assertTrue(invocations)
        self.assertTrue(all(args[0] == "git" for args in invocations), invocations)
        project.assert_called_once_with(str(self.repo))
        land.assert_called_once_with("page-owner/C1", if_proposed=True, check_only=True)
        save.assert_called_once_with(self.review_list, "C1", "works", self.production, cleanup=cleanup)
        saved = next(row for row in change_review.review_rows(self.review_list) if row["#"] == "C1")
        self.assertEqual(saved["Verdict"], "works")
        self.assertEqual([row["#"] for row in change_review.pending(self.review_list)], ["C2", "C3"])
        self.assertIn("Saved: works", self.request(path, {}).body)
        self.assertEqual([target for target, _, _ in self.messages], ["page-owner"])
        self.assertIn("C1 landed", self.messages[0][2])
        self.assertFalse((feature_log.state_dir() / "proposals/page-owner/C1.json").exists())
        self.assertEqual(change_review.PendingVerdictCleanup.load(cleanup.intent, self.temporary), cleanup)
        shutil.rmtree(self.root / path.split("/")[1])
        before = self.review_list.read_bytes()
        with patch.object(scratch, "end", wraps=scratch.end) as end:
            cleanup.intent.chmod(0o644)
            change_page.recover_pending(change_page.DeliveryStatus(self.root))
            end.assert_not_called()
            self.assertTrue(cleanup.intent.exists())
            cleanup.intent.chmod(0o600)
            change_page.recover_pending(change_page.DeliveryStatus(self.root))
            end.assert_called_once_with("row:page-owner-C1")
        self.assertFalse(cleanup.intent.exists())
        self.assertEqual(self.review_list.read_bytes(), before)
        self.assertEqual([target for target, _, _ in self.messages], ["page-owner"])

    def test_works_with_feedback_lands_the_proposal_after_saving_the_verdict(self) -> None:
        _ = self.git("remote", "add", "origin", "https://github.com/hanallc/claude_commands.git")
        self.feature_command("propose", "--review", "page-owner/C1", *self.note_arguments("claude", "Numbered moves"))
        self.assertEqual(self.request(self.page_path, self.answer(feedback="The numbers are clear")).status, 303)
        self.assertEqual(self.proposal("page-owner/C1")["status"], "landed")
        self.assertTrue((feature_log.vault_dir() / "feature-log/claude/numbered moves.md").is_file())
        saved = next(row for row in change_review.review_rows(self.review_list) if row["#"] == "C1")
        self.assertEqual(saved["Verdict"], "works: The numbers are clear")
        self.assertTrue(change_review.lands(saved["Verdict"]))
        self.assertIn("C1 landed", self.messages[-1][2])
        self.assertIn("Saved: works: The numbers are clear", self.request(self.page_path, {}).body)

    def test_a_negative_verdict_that_starts_with_dashes_drops_the_proposal_and_saves_the_words(self) -> None:
        _ = self.git("remote", "add", "origin", "git@github.com:hanallc/claude_commands.git")
        self.feature_command("propose", "--review", "page-owner/C1", *self.note_arguments("claude", "Numbered moves"))
        self.assertEqual(self.request(self.page_path, self.answer(outcome="not works", feedback="--help")).status, 303)
        self.assertEqual(self.proposal("page-owner/C1")["status"], "dropped")
        self.assertEqual(self.proposal("page-owner/C1")["verdict"], "not works: --help")
        self.assertIn("Saved: not works: --help", self.request(self.page_path, {}).body)
        self.assertEqual([target for target, _, _ in self.messages], ["builder-unit", "page-owner"])

    def test_a_landing_replacement_supersedes_the_old_note(self) -> None:
        _ = self.git("remote", "add", "origin", "https://github.com/hanallc/claude_commands")
        self.feature_command("add", *self.note_arguments("claude", "Old numbers"))
        self.feature_command("propose", "--review", "page-owner/C1", "--old", "Old numbers",
                             *self.note_arguments("claude", "Numbered moves"))
        self.assertEqual(self.request(self.page_path, self.answer()).status, 303)
        self.assertEqual(self.proposal("page-owner/C1")["status"], "landed")
        old = feature_log.parse_note(feature_log.vault_dir() / "feature-log/claude/old numbers.md")
        self.assertEqual((old["state"], old["replaced_by"]), ("superseded", ["numbered moves"]))

    def test_a_malformed_proposal_keeps_the_row_open_and_tells_the_showrunner(self) -> None:
        _ = self.git("remote", "add", "origin", "https://github.com/hanallc/claude_commands.git")
        self.feature_command("propose", "--review", "page-owner/C1", *self.note_arguments("claude", "Numbered moves"))
        before = self.review_list.read_bytes()
        _ = (feature_log.state_dir() / "proposals/page-owner/C1.json").write_text("{", encoding="utf-8")
        reply = self.request(self.page_path, self.answer())
        self.assertEqual(reply.status, 503)
        self.assertIn("Not saved: The feature log could not be updated.", reply.body)
        self.assertEqual(self.review_list.read_bytes(), before)
        self.assertEqual([target for target, _, _ in self.messages], ["page-owner"])
        self.assertIn("C1 stays open", self.messages[0][2])
        self.assertIn("cannot read proposal", self.messages[0][2])

    def test_a_saved_answer_needs_no_delivery_control_or_feature_log_proposal(self) -> None:
        _ = self.git("remote", "add", "origin", "https://github.com/hanallc/claude_commands.git")
        self.feature_command("propose", "--review", "page-owner/C1", *self.note_arguments("claude", "Numbered moves"))
        with patch.object(change_page, "send_message", side_effect=RuntimeError("socket unavailable")):
            self.assertEqual(self.request(self.page_path, self.answer()).status, 303)
        (feature_log.state_dir() / "proposals/page-owner/C1.json").unlink()
        self.assert_no_delivery_controls(self.request(self.page_path, {}).body)
        change_page.recover_pending(change_page.DeliveryStatus(self.root))
        self.assertEqual([target for target, _, _ in self.messages], [])

    def test_other_projects_close_the_row_without_a_feature_log_proposal(self) -> None:
        _ = self.git("remote", "add", "origin", "https://github.com/hanallc/nixos.git")
        self.assertEqual(self.request(self.page_path, self.answer()).status, 303)
        self.assertIn("Saved: works", self.request(self.page_path, {}).body)
        self.assertFalse((feature_log.state_dir() / "proposals").exists())

    def test_the_project_policy_can_require_feature_log_closeout_before_saving(self) -> None:
        _ = self.git("remote", "add", "origin", "https://github.com/natepiano/nixos.git")
        project = feature_log.PROJECTS["natedev"]._replace(closes_by_change_review=True)
        self.feature_command("propose", "--review", "page-owner/C1", *self.note_arguments("natedev", "Numbered moves"))
        before = self.review_list.read_bytes()
        _ = (feature_log.state_dir() / "proposals/page-owner/C1.json").write_text("{", encoding="utf-8")
        with patch.dict(feature_log.PROJECTS, {"natedev": project}):
            reply = self.request(self.page_path, self.answer())
        self.assertEqual(reply.status, 503)
        self.assertIn("Not saved: The feature log could not be updated.", reply.body)
        self.assertEqual(self.review_list.read_bytes(), before)
        self.assertEqual([target for target, _, _ in self.messages], ["page-owner"])

    def test_the_project_policy_can_skip_the_feature_log_operation(self) -> None:
        _ = self.git("remote", "add", "origin", "https://github.com/hanallc/claude_commands.git")
        project = feature_log.PROJECTS["claude"]._replace(closes_by_change_review=False)
        with patch.dict(feature_log.PROJECTS, {"claude": project}):
            self.assertEqual(self.request(self.page_path, self.answer()).status, 303)
        self.assertIn("Saved: works", self.request(self.page_path, {}).body)
        self.assertFalse((feature_log.state_dir() / "proposals").exists())

    def test_the_hana_list_uses_a_bare_review_id(self) -> None:
        _ = self.git("remote", "add", "origin", "https://github.com/hanallc/hana.git")
        review_list = self.review_list.with_name("change-review - hana.md")
        _ = self.review_list.rename(review_list)
        path = urlsplit(change_page.publish(self.production, review_list, ["C1"])).path
        page = change_page.page_at(self.root, path)
        self.feature_command("propose", "--review", "C1", *self.note_arguments("hana", "Numbered moves"))
        self.assertEqual(self.request(path, dict(self.answer(), token=page.token)).status, 303)
        self.assertEqual(self.proposal("C1")["status"], "landed")
        self.assertIn("Saved: works", self.legacy_browser_reply(path).body)

    def test_a_producer_id_routes_feedback_to_its_unit(self) -> None:
        _ = change_review.append_rows(self.review_list, pictured_changes([change_review.Visible("Direct checkpoint", "Run", "Proof")]),
                                      date(2026, 11, 1), "abcdef12", self.production, producers=[self.unit.id], record=staff.snapshot())
        _ = change_review.set_row_kind(self.review_list, "C4", "See in action", self.production)
        path = urlsplit(change_page.publish(self.production, self.review_list, ["C4"])).path
        page = change_page.page_at(self.root, path)
        self.assertEqual(self.request(path, dict(self.answer("C4", "not works"), token=page.token)).status, 303)
        self.assertEqual([target for target, _, _ in self.messages], ["builder-unit", "page-owner"])

    def test_a_child_page_routes_a_sibling_row_through_the_shared_root(self) -> None:
        notifier = self.temporary / "notifier"
        _ = self.enterContext(patch.object(showrunners, "NOTIFIER_STATE_DIR", notifier))
        root = fixture_member("showrunner", name="root")
        child = self.temporary / "child-production.md"
        sibling = self.temporary / "sibling-production.md"
        root_doc = self.temporary / "root-production.md"
        _ = staff.set_doc(root.id, staff.LiveDoc(local_machine(), root_doc))
        for name, doc in (("root", root_doc), ("child", child), ("sibling", sibling)):

            _ = doc.write_text(self.production.read_text().replace("builder-unit", f"{doc.stem}-unit").replace(
                "fallback-owner", name))
            timer = notifier / f"showrunner-{name}"
            timer.mkdir(parents=True)
            _ = (timer / "conf").write_text(f"EVERY=15\nTARGET=session:test\nPROMPT_FILE={timer / 'prompt'}\n"
                                           + f"CHECK=zsh /unused/production_check.sh {name}\n")
            if doc != root_doc:
                _ = fixture_member("showrunner", name=name, parent=root.id, doc=staff.LiveDoc(local_machine(), doc))
        owner = staff.snapshot().by_path("page-owner")
        if owner is None:
            self.fail("the list showrunner must have a staff record")
        sibling_unit = fixture_member("unit", name="sibling-production-unit", parent=owner.id)
        _ = change_review.append_rows(self.review_list, pictured_changes([change_review.Visible("Sibling change", "Run", "Proof")]),
                                      date(2026, 11, 1), "abcdef12", sibling, producers=[sibling_unit.id], record=staff.snapshot())
        _ = change_review.set_row_kind(self.review_list, "C4", "Read only", child)
        path = urlsplit(change_page.publish(child, self.review_list, ["C4"])).path
        page = change_page.page_at(self.root, path)
        self.assertEqual(self.request(path, dict(self.answer("C4", "not works"), token=page.token)).status, 303)
        self.assertEqual([target for target, _, _ in self.messages], ["sibling-production-unit", "page-owner"])

    def test_an_unknown_production_is_refused_before_saving(self) -> None:
        owner = fixture_member("showrunner", name="missing-owner",
                               doc=staff.LiveDoc(local_machine(), self.temporary / "missing-production.md"))
        _ = change_review.append_rows(self.review_list, pictured_changes([change_review.Visible("Unknown owner", "Run", "Proof")]),
                                      date(2026, 11, 1), "", self.production, producers=[owner.id], record=staff.snapshot())
        _ = change_review.set_row_kind(self.review_list, "C4", "Read only", self.production)
        path = urlsplit(change_page.publish(self.production, self.review_list, ["C4"])).path
        page = change_page.page_at(self.root, path)
        before = self.review_list.read_bytes()
        self.assertEqual(self.request(path, dict(self.answer("C4"), token=page.token)).status, 400)
        self.assertEqual(self.review_list.read_bytes(), before)
        self.assertEqual(self.messages, [])

    def test_a_saved_verdict_has_no_delivery_form_after_failure(self) -> None:
        with patch.object(change_page, "send_message", side_effect=RuntimeError("socket unavailable")):
            self.assertEqual(self.request(self.page_path, self.answer(outcome="not works", feedback="Still small")).status, 303)
        content = self.request(self.page_path, {}).body
        self.assertIn("Saved: not works: Still small", content)
        self.assert_no_delivery_controls(content)
        self.assertEqual(self.request(self.page_path, self.answer("C3", "retry")).status, 400)
        before = self.review_list.read_bytes()
        change_page.recover_pending(change_page.DeliveryStatus(self.root))
        self.assertEqual(self.review_list.read_bytes(), before)

    def test_a_non_ascii_token_is_refused_without_changing_the_record(self) -> None:
        before = self.review_list.read_bytes()
        self.assertEqual(self.request(self.page_path, dict(self.answer(), token="é")).status, 400)
        self.assertEqual(self.review_list.read_bytes(), before)
        self.assertEqual(self.messages, [])

    def test_each_section_shows_its_change_text_once(self) -> None:
        body = self.request(self.page_path, {}).body
        text = re.sub(r'alt="[^"]*"', "", body)
        self.assertEqual(text.count("Numbered moves"), 1)
        self.assertEqual(text.count("Internal comments clarified"), 1)

    def test_feedback_that_starts_with_works_still_requests_action_and_retries(self) -> None:
        with self.subTest(request="fine through Send feedback"):
            self.assertEqual(self.request(self.page_path, self.answer("C3", "feedback", "fine with the title")).status, 303)
            self.assertFalse(change_page.answer_row(self.page, "C3").get("Verdict"))
            self.assertIsInstance(change_page.DeliveryStatus(self.root).slots(self.page, "C3").question,
                                  change_page.OpenFeedback)
            self.messages.clear()
        def report_delivery_failure(recipient: str, sender: str, message: str, **_options: str) -> None:
            self.capture_message(recipient, sender, message)
            raise RuntimeError("delivery receipt missing")
        with patch.object(change_page, "send_message", side_effect=report_delivery_failure), redirect_stderr(StringIO()) as error:
            self.assertEqual(self.request(self.page_path, self.answer(outcome="feedback", feedback="works")).status, 303)
        self.assertEqual([target for target, _, _ in self.messages], ["page-owner"] * 3)
        self.assertIn("C1: delivery receipt missing; no separate showrunner for the alert", error.getvalue())
        self.assert_no_delivery_controls(self.request(self.page_path, {}).body)
        before = self.review_list.read_bytes()
        change_page.recover_pending(change_page.DeliveryStatus(self.root))
        self.assertEqual(self.review_list.read_bytes(), before)

    def test_a_failed_unit_delivery_uses_its_budget_and_alerts_the_showrunner_once(self) -> None:
        attempts: list[str] = []
        def fail_unit_delivery(recipient: str, sender: str, message: str, **_options: str) -> None:
            name = self.recipient_name(recipient)
            if "delivery needs attention" not in message:
                attempts.append(name)
            if name == "builder-unit":
                raise RuntimeError("unit socket unavailable")
            self.capture_message(recipient, sender, message)
        with patch.object(change_page, "send_message", side_effect=fail_unit_delivery):
            reply = self.request(self.page_path, self.answer(outcome="not works", feedback="Use a smaller title"))
        self.assertEqual(reply.status, 303)
        self.assertEqual(attempts, ["builder-unit", "page-owner", "builder-unit", "builder-unit"])
        self.assertEqual(sum("delivery needs attention" in message for _, _, message in self.messages), 1)
        self.assertTrue(all("Use a smaller title" in message for _, _, message in self.messages))
        self.assertIn("Recipient: " + str(self.unit.id), self.messages[-1][2])
        self.assertIn("unit socket unavailable", self.messages[-1][2])
        self.assert_record_cleared()
        before = self.review_list.read_bytes()
        change_page.recover_pending(change_page.DeliveryStatus(self.root))
        self.assertEqual(self.review_list.read_bytes(), before)

    def test_the_showrunner_receives_its_first_attempt_before_the_unit_receives_its_second(self) -> None:
        events: list[str] = []

        def fail_the_unit_once(recipient: str, sender: str, message: str, **_options: str) -> None:
            name = self.recipient_name(recipient)
            events.append(name)
            if name == "builder-unit" and events.count(name) == 1:
                raise RuntimeError("unit session unavailable")
            self.capture_message(recipient, sender, message)

        def pause_round(seconds: float) -> None:
            self.assertEqual(seconds, 0)
            events.append("pause")

        with patch.object(change_page, "send_message", side_effect=fail_the_unit_once), patch.object(
                time, "sleep", side_effect=pause_round):
            reply = self.request(self.page_path, self.answer(outcome="not works"))
        self.assertEqual(reply.status, 303)
        self.assertEqual(events, ["builder-unit", "page-owner", "pause", "builder-unit"])
        self.assert_record_cleared()

    def test_a_recipient_that_succeeds_in_the_first_round_receives_no_retry(self) -> None:
        attempts: list[str] = []

        def fail_the_showrunner_once(recipient: str, sender: str, message: str, **_options: str) -> None:
            name = self.recipient_name(recipient)
            attempts.append(name)
            if name == "page-owner" and attempts.count(name) == 1:
                raise RuntimeError("showrunner session unavailable")
            self.capture_message(recipient, sender, message)

        with patch.object(change_page, "send_message", side_effect=fail_the_showrunner_once), patch.object(time, "sleep") as pause:
            reply = self.request(self.page_path, self.answer(outcome="not works"))
        self.assertEqual(reply.status, 303)
        self.assertEqual(attempts, ["builder-unit", "page-owner", "page-owner"])
        self.assertEqual([target for target, _, _ in self.messages], ["builder-unit", "page-owner"])
        pause.assert_called_once_with(0)
        self.assert_record_cleared()

    def test_a_failed_feature_log_save_keeps_the_form_and_hides_error_details(self) -> None:
        _ = self.git("remote", "add", "origin", "https://github.com/hanallc/claude_commands.git")
        self.feature_command("propose", "--review", "page-owner/C1", *self.note_arguments("claude", "Numbered moves"))
        proposal = feature_log.state_dir() / "proposals/page-owner/C1.json"
        original = proposal.read_bytes()
        _ = proposal.write_text("{", encoding="utf-8")
        words = "Still <small> & 'awkward'"
        before = self.review_list.read_bytes()
        errors = StringIO()
        with redirect_stderr(errors):
            reply = self.request(self.page_path, self.answer(outcome="not works", feedback=words))
        self.assertEqual(reply.status, 503)
        self.assertIn("<style>", reply.body)
        self.assertIn("<main>", reply.body)
        self.assertIn("Not saved: The feature log could not be updated.", reply.body)
        self.assertNotIn(str(self.temporary), reply.body)
        self.assertNotIn("Traceback", reply.body)
        self.assertNotIn("feature_log:", reply.body)
        self.assertEqual(self.review_list.read_bytes(), before)
        self.assertIn(str(self.review_list), errors.getvalue())
        self.assertIn("cannot read proposal", errors.getvalue())
        self.assertIn("cannot read proposal", self.messages[0][2])
        self.assertIn(str(self.review_list), self.messages[0][2])
        parser = SavedAnswerForm()
        parser.feed(reply.body)
        self.assertEqual(parser.feedback["C1"], words)
        self.assertEqual(parser.selected_outcomes["C1"], "not works")
        _ = proposal.write_bytes(original)
        self.assertEqual(self.request(self.page_path, self.answer(
            "C1", parser.selected_outcomes["C1"], parser.feedback["C1"])).status, 303)
        self.assertIn("Saved: not works: Still &lt;small&gt;", self.request(self.page_path, {}).body)

    def test_unknown_post_paths_use_the_review_page_style_without_error_details(self) -> None:
        reply = self.request("/missing/changes.html", self.answer())
        self.assertEqual(reply.status, 404)
        self.assertIn("<style>", reply.body)
        self.assertIn("<main><p>This review page is not available.</p></main>", reply.body)
        self.assertNotIn("Error response", reply.body)
        self.assertNotIn(str(self.root), reply.body)

    def test_the_response_and_saved_page_do_not_wait_for_a_slow_message(self) -> None:
        started = threading.Event()
        release = threading.Event()

        def slow_send(recipient: str, sender: str, message: str, **_options: str) -> None:
            started.set()
            if not release.wait(TEST_HANG_GUARD_SECS):
                raise RuntimeError("test message was not released")
            self.capture_message(recipient, sender, message)

        with patch.object(change_page, "send_message", side_effect=slow_send):
            try:
                reply = self.http_request(self.page_path, self.answer())
                self.assertEqual(reply.status, 303)
                self.assertTrue(started.wait(TEST_HANG_GUARD_SECS))
                self.assertFalse(self.delivery_done.is_set())
                self.assertEqual(self.messages, [])
                page = self.http_request(self.page_path, {}).body
                self.assertIn("Saved: works", page)
                self.assert_no_delivery_controls(page)
            finally:
                release.set()
                self.assertTrue(self.delivery_done.wait(TEST_HANG_GUARD_SECS))
        self.assertEqual([recipient for recipient, _, _ in self.messages], ["page-owner"])
        self.assert_no_delivery_controls(self.request(self.page_path, {}).body)

    def test_a_later_message_failure_keeps_delivery_details_off_every_link(self) -> None:
        with patch.object(change_page, "send_message", side_effect=RuntimeError("private failure")):
            self.assertEqual(self.request(self.page_path, self.answer()).status, 303)
        self.assertTrue(self.delivery_done.wait(TEST_HANG_GUARD_SECS))
        other_path = urlsplit(change_page.publish(self.production, self.review_list, ["C1"])).path
        for path in (self.page_path, other_path):
            body = self.legacy_browser_reply(path).body
            self.assertIn("Saved: works", body)
            self.assert_no_delivery_controls(body)
            self.assertNotIn("private failure", body)
            self.assertNotIn(str(self.temporary), body)
        self.assert_record_cleared()
        status = change_page.DeliveryStatus(self.root)
        _ = status.begin(self.page, "C1", "validation", "Keep private")
        self.assertTrue(status.path(self.page, "C1").exists())
        self.assertEqual(self.request("/" + status.path(self.page, "C1").name, {}).status, 404)

    def test_an_interruption_after_the_verdict_preserves_a_pending_message_for_server_recovery(self) -> None:
        save = change_review.save_verdict

        def interrupt_after_verdict(path: Path, row: str, verdict: str, production: Path, *,
                                    cleanup: change_review.PendingVerdictCleanup) -> change_review.VerdictSaved | change_review.VerdictRefused:
            result = save(path, row, verdict, production, cleanup=cleanup)
            if isinstance(result, change_review.VerdictSaved):
                raise RuntimeError("interrupted after verdict")
            return result

        with patch.object(change_review, "save_verdict", side_effect=interrupt_after_verdict):
            reply = self.request(self.page_path, self.answer(feedback="Clear numbers"))
        self.assertEqual(reply.status, 503)
        self.assertEqual(self.messages, [])
        body = self.request(self.page_path, {}).body
        self.assertIn("Saved: works: Clear numbers", body)
        self.assert_no_delivery_controls(body)
        change_page.recover_pending(change_page.DeliveryStatus(self.root))
        self.assertIn("works: Clear numbers", self.messages[0][2])
        self.assert_no_delivery_controls(self.request(self.page_path, {}).body)

    def test_an_intent_write_failure_refuses_the_verdict_write(self) -> None:
        before = self.review_list.read_bytes()
        with patch.object(change_page.DeliveryStatus, "begin", side_effect=OSError("disk full")):
            reply = self.request(self.page_path, self.answer())
        self.assertEqual(reply.status, 503)
        self.assertEqual(self.review_list.read_bytes(), before)
        self.assertEqual(self.messages, [])
        self.assertNotIn("Saved:", reply.body)

    def test_a_git_launch_failure_keeps_the_saved_verdict_and_delivers_it(self) -> None:
        run = cast(Callable[..., object], subprocess.run)

        def fail_commit_probe(args: list[str], **kwargs: object) -> object:
            if args[3:] == ["--literal-pathspecs", "rev-parse", "--is-inside-work-tree"]:
                self.assertIn("works: keep the verdict", self.review_list.read_text())
                raise FileNotFoundError("fixture Git launch failed")
            return run(args, **kwargs)

        with patch.object(subprocess, "run", new=fail_commit_probe):
            reply = self.request(self.page_path, self.answer(feedback="keep the verdict"))
        self.assertEqual(reply.status, 303)
        self.assertIn("Saved: works: keep the verdict", self.request(self.page_path, {}).body)
        self.assertCountEqual([recipient for recipient, _, message in self.messages if "works: keep the verdict" in message],
                              ["builder-unit", "page-owner"])
        self.assert_record_cleared()

    def test_a_failed_or_interrupted_owner_verdict_preserves_validation_ownership_and_feedback(self) -> None:
        status = change_page.DeliveryStatus(self.root)
        self.assertEqual(self.request(self.page_path, self.answer(outcome="validate", feedback="Keep these words")).status, 303)
        validation = status.slots(self.page, "C1").validation
        assert isinstance(validation, change_page.ValidationRequested)
        before = self.review_list.read_bytes()
        for failure in (change_review.ChangeReviewError("verdict write failed"), OSError("verdict write failed"),
                        RuntimeError("interrupted before verdict")):
            with self.subTest(failure=failure):
                with patch.object(change_review, "record_verdict", side_effect=failure):
                    with self.assertRaises(type(failure)):
                        _ = change_review.save_verdict(self.review_list, "C1", "works", self.production,
                                                      validation_id=validation.attempt)
                self.assertEqual(self.review_list.read_bytes(), before)
                self.assertEqual(status.slots(self.page, "C1").validation, validation)
                self.assertEqual(validation.feedback, "Keep these words")
                self.assertIn("C1", [row["#"] for row in change_review.pending(self.review_list)])
                self.assertIsInstance(status.slots(self.page, "C1").verdict, change_page.NoDelivery)
                body = self.request(self.page_path, {}).body
                self.assertIn("The showrunner will validate this", body)
                self.assertNotIn("Saved:", body)
                self.assert_no_delivery_controls(body)

    def test_an_interruption_after_saving_an_owner_verdict_keeps_validation_feedback(self) -> None:
        words = "Check the small text"
        with patch.object(change_page, "send_message", side_effect=RuntimeError("socket unavailable")):
            self.assertEqual(self.request(self.page_path, self.answer(outcome="validate", feedback=words)).status, 303)
        messages = list(self.messages)
        status = change_page.DeliveryStatus(self.root)
        validation = status.slots(self.page, "C1").validation
        assert isinstance(validation, change_page.ValidationRequested)
        with patch.object(change_review, "_verdict_output", side_effect=RuntimeError("interrupted after verdict")):
            with self.assertRaisesRegex(RuntimeError, "interrupted after verdict"):
                _ = change_review.save_verdict(self.review_list, "C1", "works", self.production,
                                              validation_id=validation.attempt)
        slots = status.slots(self.page, "C1")
        self.assertEqual(slots.validation, validation)
        self.assertIsInstance(slots.verdict, change_page.NoDelivery)
        self.assertEqual(self.messages, messages)
        body = self.request(self.page_path, {}).body
        self.assertIn("Saved: works", body)
        self.assert_no_delivery_controls(body)
        change_page.recover_pending(change_page.DeliveryStatus(self.root))
        self.assertEqual(status.slots(self.page, "C1").validation, validation)
        self.assertIsInstance(status.slots(self.page, "C1").verdict, change_page.NoDelivery)
        body = self.request(self.page_path, {}).body
        self.assert_no_delivery_controls(body)

    def test_completing_one_delivery_kind_preserves_the_other_slot(self) -> None:
        status = change_page.DeliveryStatus(self.root)
        for first in ("validation", "verdict"):
            with self.subTest(first=first):
                validation = status.begin(self.page, "C1", "validation", "Keep these words")
                verdict = status.begin(self.page, "C1", "verdict")
                attempts = {"validation": validation, "verdict": verdict}
                other = "verdict" if first == "validation" else "validation"
                first_delivery = change_page.MessageDelivery(self.page, "C1", (), "", attempts[first])
                other_delivery = change_page.MessageDelivery(self.page, "C1", (), "", attempts[other])
                before = status.slots(self.page, "C1")
                remaining = before.verdict if other == "verdict" else before.validation
                status.record(first_delivery)
                after = status.slots(self.page, "C1")
                self.assertEqual(after.verdict if other == "verdict" else after.validation, remaining)
                status.record(first_delivery)
                status.record(first_delivery)
                after = status.slots(self.page, "C1")
                self.assertEqual(after.verdict if other == "verdict" else after.validation, remaining)
                if first == "validation":
                    self.assertEqual(after.validation, change_page.ValidationRequested(validation, "Keep these words"))
                else:
                    self.assertIsInstance(after.verdict, change_page.NoDelivery)
                stored = status.path(self.page, "C1").read_text(encoding="utf-8")
                self.assertEqual(f'"{first}"' in stored, first == "validation")
                self.assertIn(f'"{other}"', stored)
                status.record(other_delivery)
                self.assertEqual(status.slots(self.page, "C1").validation,
                                 change_page.ValidationRequested(validation, "Keep these words"))
                self.assertIsInstance(status.slots(self.page, "C1").verdict, change_page.NoDelivery)

    def test_single_intent_records_migrate_to_their_kind_without_losing_feedback(self) -> None:
        status = change_page.DeliveryStatus(self.root)
        kinds: tuple[Literal["validation", "verdict"], ...] = ("validation", "verdict")
        for kind in kinds:
            with self.subTest(kind=kind):
                _ = status.path(self.page, "C1").write_text(json.dumps({
                    "attempt": "old-attempt", "state": "pending", "kind": kind,
                    "feedback": "Keep these words" if kind == "validation" else "",
                }), encoding="utf-8")
                slots = status.slots(self.page, "C1")
                original = slots.validation if kind == "validation" else slots.verdict
                self.assertEqual(original, change_page.DeliveryIntent(
                    "old-attempt", kind,
                    "Keep these words" if kind == "validation" else ""))
                if kind == "validation":
                    _ = status.begin(self.page, "C1", "verdict")
                else:
                    _ = status.begin(self.page, "C1", "validation", "New words")
                migrated = status.slots(self.page, "C1")
                self.assertEqual(migrated.validation if kind == "validation" else migrated.verdict, original)

    def test_a_stopped_send_leaves_a_pending_record_without_delivery_controls(self) -> None:
        stopped = threading.Event()

        def stop_delivery(delivery: change_page.MessageDelivery, status: change_page.DeliveryStatus) -> None:
            del delivery, status
            stopped.set()
            raise OSError("server stopped")

        with patch.object(change_page, "deliver", side_effect=stop_delivery):
            self.assertEqual(self.http_request(self.page_path, self.answer()).status, 303)
            self.assertTrue(stopped.wait(TEST_HANG_GUARD_SECS))
        status = change_page.DeliveryStatus(self.root)
        intent = status.slots(self.page, "C1").verdict
        self.assertIsInstance(intent, change_page.DeliveryIntent)
        if not isinstance(intent, change_page.DeliveryIntent):
            self.fail("the delivery intent is missing")
        self.assertEqual((intent.kind, intent.feedback), ("verdict", ""))
        body = self.request(self.page_path, {}).body
        self.assertIn("Saved: works", body)
        self.assert_no_delivery_controls(body)

    def test_an_older_success_cannot_clear_a_newer_validation_ownership_mark(self) -> None:
        status = change_page.DeliveryStatus(self.root)
        older = status.begin(self.page, "C1", "validation", "old words")
        newer = status.begin(self.page, "C1", "validation", "new words")
        old = change_page.MessageDelivery(self.page, "C1", (), "", older)
        new = change_page.MessageDelivery(self.page, "C1", (), "", newer)
        status.record(new)
        status.record(old)
        status.record(old)
        self.assertEqual(status.slots(self.page, "C1").validation,
                         change_page.ValidationRequested(newer, "new words"))
        body = self.request(self.page_path, {}).body
        parser = SavedAnswerForm()
        parser.feed(body)
        self.assertNotIn("C1", parser.validation_requests)
        self.assertNotIn("The validation message did not send", body)

    def test_attempt_comparison_and_update_hold_the_lock_against_a_new_attempt(self) -> None:
        status = change_page.DeliveryStatus(self.root)
        attempt = status.begin(self.page, "C1", "validation", "old words")
        old = change_page.MessageDelivery(self.page, "C1", (), "", attempt)
        checked = threading.Event()
        release = threading.Event()
        entering = threading.Event()
        write_bytes = atomic_file.write_bytes_atomic

        def pause_before_write(path: Path, content: bytes, *, mode: int | None = None) -> None:
            if path == status.path(self.page, "C1") and threading.current_thread().name.startswith("old-attempt"):
                checked.set()
                if not release.wait(TEST_HANG_GUARD_SECS):
                    raise RuntimeError("the old attempt was not released")
            write_bytes(path, content, mode=mode)

        def newer_failure() -> str:
            entering.set()
            newer = status.begin(self.page, "C1", "validation", "new words")
            status.record(change_page.MessageDelivery(self.page, "C1", (), "", newer))
            return newer

        with patch.object(atomic_file, "write_bytes_atomic", new=pause_before_write), \
                ThreadPoolExecutor(max_workers=1, thread_name_prefix="old-attempt") as old_worker, \
                ThreadPoolExecutor(max_workers=1) as new_worker:
            previous = old_worker.submit(status.record, old)
            try:
                self.assertTrue(checked.wait(TEST_HANG_GUARD_SECS))
                current = new_worker.submit(newer_failure)
                self.assertTrue(entering.wait(TEST_HANG_GUARD_SECS))
                with (self.root / ".review-link-delivery.lock").open("a+", encoding="utf-8") as contender:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(contender.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                release.set()
            previous.result(TEST_HANG_GUARD_SECS)
            newest = current.result(TEST_HANG_GUARD_SECS)
        self.assertEqual(status.slots(self.page, "C1").validation,
                         change_page.ValidationRequested(newest, "new words"))

    def test_legacy_failed_records_offer_no_delivery_controls_and_send_nothing(self) -> None:
        self.assertEqual(self.request(self.page_path, self.answer()).status, 303)
        status = change_page.DeliveryStatus(self.root)
        self.messages.clear()
        for content in ("failed\n", json.dumps({"feedback": "Check the proof"})):
            _ = status.path(self.page, "C1").write_text(content)
            change_page.recover_pending(status)
            body = self.request(self.page_path, {}).body
            self.assertIn("Saved: works", body)
            self.assert_no_delivery_controls(body)
            self.assertIsInstance(status.slots(self.page, "C1").verdict, change_page.NoDelivery)
        self.assertEqual(self.messages, [])

    def test_a_render_failure_keeps_the_typed_answer_in_an_independent_form(self) -> None:
        words = "Still <small> & 'awkward'"
        before = self.review_list.read_bytes()
        with patch.object(change_page, "shown_rows", side_effect=ValueError(f"private path {self.temporary}")):
            reply = self.request(self.page_path, self.answer(outcome="not works", feedback=words))
        self.assertEqual(reply.status, 400)
        self.assertIn("<style>", reply.body)
        self.assertIn('method="post"', reply.body)
        self.assertNotIn(str(self.temporary), reply.body)
        self.assertNotIn("<small>", reply.body)
        self.assertEqual(self.review_list.read_bytes(), before)
        parser = SavedAnswerForm()
        parser.feed(reply.body)
        self.assertEqual(parser.feedback["C1"], words)
        self.assertEqual(parser.selected_outcomes["C1"], "not works")
        self.assertEqual(self.request(self.page_path, self.answer(
            "C1", parser.selected_outcomes["C1"], parser.feedback["C1"])).status, 303)

    def test_after_a_save_next_publishes_the_queue_head_without_advancing_on_get(self) -> None:
        self.assertEqual(self.request(self.page_path, self.answer()).status, 303)
        queue = next_item.change_queue(self.production)
        urgency.pin(queue.name, "C3")
        folders = set(self.root.iterdir())
        page = self.request(self.page_path, {}).body
        self.assertNotIn("changes left", page)
        self.assertIn('value="next">Skip for now', page)
        self.assertEqual(set(self.root.iterdir()), folders)
        for fields in ({"outcome": "next", "token": "wrong"},
                       {"outcome": "next", "token": self.page.token, "row": "C2"}):
            self.assertEqual(self.request(self.page_path, fields).status, 400)
        reply = self.request(self.page_path, {"outcome": "next", "token": self.page.token})
        self.assertEqual(reply.status, 303)
        path = urlsplit(reply.location).path
        self.assertNotEqual(path, self.page_path)
        self.assertEqual(change_page.page_at(self.root, path).rows, ("C3",))
        self.assertIn("Internal comments clarified", self.legacy_browser_reply(path).body)
        self.assertEqual([row["#"] for row in change_review.pending(self.review_list)], ["C2", "C3"])

    def test_an_empty_queue_shows_zero_without_a_next_control(self) -> None:
        path = urlsplit(change_page.publish(self.production, self.review_list, ["C1", "C2", "C3"])).path
        page = change_page.page_at(self.root, path)
        for row in page.rows:
            self.assertEqual(self.request(path, dict(self.answer(row), token=page.token)).status, 303)
        body = self.legacy_browser_reply(path).body
        self.assertIn("No changes to review.", body)
        self.assertNotIn('value="next"', body)
        self.assertEqual(self.request(path, {"outcome": "next", "token": page.token}).status, 400)


class FixtureStartupRecoveryTests(unittest.TestCase):
    @contextmanager
    def startup_with_record(self, prepare: Callable[[ChangePageTests], None]) -> Generator[ChangePageTests]:
        fixture = ChangePageTests()
        prepared = False
        start = threading.Thread.start

        def prepare_before_start(thread: threading.Thread) -> None:
            nonlocal prepared
            if not prepared:
                prepare(fixture)
                prepared = True
            start(thread)

        try:
            with patch.object(threading.Thread, "start", new=prepare_before_start):
                fixture.setUp()
            self.assertTrue(prepared)
            yield fixture
        finally:
            fixture.doCleanups()

    def test_fixture_startup_cannot_clear_a_later_legacy_verdict(self) -> None:
        def prepare(fixture: ChangePageTests) -> None:
            status = change_page.DeliveryStatus(fixture.root)
            _ = status.path(fixture.page, "C1").write_text(json.dumps({
                "attempt": "old-attempt", "state": "pending", "kind": "verdict",
            }), encoding="utf-8")

        with self.startup_with_record(prepare) as fixture:
            status = change_page.DeliveryStatus(fixture.root)
            _ = status.begin(fixture.page, "C1", "validation", "New words")
            self.assertEqual(status.slots(fixture.page, "C1").verdict,
                             change_page.DeliveryIntent("old-attempt", "verdict"))
            self.assertEqual(fixture.messages, [])

    def test_fixture_startup_cannot_send_during_a_legacy_context_rewrite(self) -> None:
        before = b""

        def prepare(fixture: ChangePageTests) -> None:
            nonlocal before
            before = fixture.review_list.read_bytes()
            status = change_page.DeliveryStatus(fixture.root)
            _ = status.begin(fixture.page, "C1", "validation", "Keep these words", recipients=(fixture.showrunner_id,))

        with self.startup_with_record(prepare) as fixture:
            self.assertEqual(fixture.messages, [])
            status = change_page.DeliveryStatus(fixture.root)
            path = status.path(fixture.page, "C1")
            fields = cast(dict[str, object], json.loads(path.read_text()))
            fields["page"] = {"review_list": "/missing-list", "production": "/wrong-production", "row": "C999"}
            _ = path.write_text(json.dumps(fields), encoding="utf-8")
            restarted = change_page.DeliveryStatus(fixture.root)
            change_page.recover_pending(restarted)
            change_page.recover_pending(restarted)
            self.assertEqual(len(fixture.messages), 1)
            self.assertIn("User feedback: Keep these words", fixture.messages[0][2])
            self.assertIn("Production: page-owner", fixture.messages[0][2])
            self.assertNotIn("page", cast(dict[str, object], json.loads(path.read_text())))
            self.assertEqual(fixture.review_list.read_bytes(), before)
            self.assertIsInstance(status.slots(fixture.page, "C1").validation, change_page.ValidationRequested)



class ChangeDetailsTests(unittest.TestCase):
    def test_the_merged_cell_gives_the_date_time_and_commit(self) -> None:
        row = {"Merged": "2026-11-13 16:20 · 31770c75", change_review.KIND: "See in action"}
        self.assertEqual(change_page.ChangeDetails.read(row, "sdlc/C9").lines(), [
            ("change", "sdlc/C9"), ("date", "2026-11-13"), ("time", "16:20"), ("commit", "31770c75"), ("kind", "See in action")])

    def test_an_unknown_value_is_left_out(self) -> None:
        for merged in ("", "2026-11-13 later · not a hash", "merged 2026-11-13"):
            with self.subTest(merged=merged):
                self.assertEqual(change_page.ChangeDetails.read({"Merged": merged}, "C1").lines(), [("change", "C1")])


if __name__ == "__main__":
    _ = unittest.main()
