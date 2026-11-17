"""The console Accounts page draws the accounts board table."""
from __future__ import annotations
from scripts.lint.memory_test_support import copy_config_fixture, write_config_fixture

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import html
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from typing import cast, override
from unittest.mock import patch

from scripts.review_links.test_page_frame import assert_status_states
from scripts.accounts import accounts, next_moves
from scripts.lib import conf
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.review_links import accounts_page, console_theme, page_frame, serve
from scripts.whoami import agent_accounts

AT = datetime(2026, 11, 15, 15, tzinfo=timezone.utc)


def status_row(label: str) -> accounts.StatusRow:
    return accounts.StatusRow(
        label=label, tool="claude", folder=f"~/.{label}", login="fixture@example.invalid",
        roles=("units",), role_counts=(accounts.RoleSessionCounts(1, 0, 0, 0),), weekly_left_percent=60,
        resets_at=AT + timedelta(days=1), credits=0, on_credits="no", use_credits=False, read_at=AT,
        read_outcome=agent_accounts.UsageReadOutcome.OK, attempted_at=AT)


MENU_SETUP = '''<script>
let reads=0;const saves=[];const realFetch=fetch;
window.fetch=(path,options)=>options?.method==='POST' ? new Promise(resolve=>saves.push({options,resolve}))
 : (reads++,Promise.resolve({ok:true,text:async()=>FRESH_PAGE}));
</script>'''

MENU_PROBE = '''<script>
(async()=>{
 const failures=[], check=(ok,label)=>{if(!ok) failures.push(label);};
 const flush=()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(()=>setTimeout(resolve,0))));
 const state=()=>document.querySelector('[data-page-status]').dataset.state;
 const menu=document.querySelector('[data-move-menu]');
 const items=()=>[...menu.querySelectorAll('[data-move-target]')];
 const right=(cell,x=40,y=60)=>{const event=new MouseEvent('contextmenu',{bubbles:true,cancelable:true,clientX:x,clientY:y});
  cell.dispatchEvent(event);return event;};
 const key=name=>menu.dispatchEvent(new KeyboardEvent('keydown',{key:name,bubbles:true,cancelable:true}));
 const groupRow=()=>document.querySelector('tr[data-move-group]');
 try{
  if(document.readyState!=='complete') await new Promise(resolve=>addEventListener('load',resolve,{once:true}));
  frameElement.style.width='800px';frameElement.style.height='480px';await flush();
  check(document.querySelectorAll('tr[data-move-group]').length===1 && groupRow().dataset.moveAccount==='claude1'
   && groupRow().dataset.moveGroup==='units','only the selected Claude group line takes the menu');
  check(getComputedStyle(groupRow()).cursor==='context-menu','the group line shows the menu cursor');
  const plain=right(document.querySelector('[data-color-key="account.claude2"]').closest('tr').cells[1]);
  check(!plain.defaultPrevented && menu.hidden,'another line keeps the browser menu');
  const opened=right(groupRow().cells[3]);
  check(opened.defaultPrevented && !menu.hidden,'a right click on the group line opens the page menu');
  check(menu.querySelector('[data-move-title]').textContent==='When claude1 reaches 1%, move units to',
   'the title names the account, percent and group');
  check(JSON.stringify(items().map(item=>item.dataset.moveTarget))==='["claude2","claude3"]','the menu lists the other Claude accounts');
  check(items()[0].textContent==='claude260% left' && items()[1].textContent==='claude3',
   'each item shows the weekly percent left when known');
  check(items()[0].querySelector('.move-name').style.color==='rgb(178, 148, 187)','each item uses the account color');
  check(items()[0].querySelector('.move-left').className==='move-left','a healthy percent keeps the plain tone');
  check(document.activeElement===items()[0],'the first item takes the focus');
  key('ArrowDown');check(document.activeElement===items()[1],'ArrowDown moves to the next item');
  key('ArrowDown');check(document.activeElement===items()[0],'ArrowDown wraps to the first item');
  key('ArrowUp');check(document.activeElement===items()[1],'ArrowUp wraps to the last item');
  key('Home');check(document.activeElement===items()[0],'Home moves to the first item');
  key('End');check(document.activeElement===items()[1],'End moves to the last item');
  const box=menu.getBoundingClientRect();
  check(box.left>=0 && box.right<=innerWidth && box.top>=0 && box.bottom<=innerHeight,'the menu fits the viewport');
  right(groupRow().cells[3],innerWidth-2,innerHeight-2);
  const edge=menu.getBoundingClientRect();
  check(edge.left>=8 && edge.top>=8 && edge.right<=innerWidth-7.5 && edge.bottom<=innerHeight-7.5,
   'a right click at the corner keeps the menu inside the viewport:'+JSON.stringify([edge.left,edge.top,edge.right,edge.bottom,innerWidth,innerHeight]));
  check(state()==='Paused','an open menu pauses refresh');
  key('Escape');check(menu.hidden && state()!=='Paused','Escape closes the menu and resumes refresh');
  right(groupRow().cells[3]);
  document.body.dispatchEvent(new PointerEvent('pointerdown',{bubbles:true}));
  check(menu.hidden,'a click outside closes the menu');
  right(groupRow().cells[3]);
  const readsBefore=reads;
  items()[1].click();
  check(menu.hidden && saves.length===1,'a choice closes the menu and posts once');
  const form=new URLSearchParams(saves[0].options.body);
  check(form.get('group')==='units' && form.get('to')==='claude3' && [...form.keys()].length===2 &&
   saves[0].options.headers['Content-Type']==='application/x-www-form-urlencoded','the post names the group and the account');
  check(state()==='Paused','a pending move holds refresh');
  saves[0].resolve({ok:true,json:async()=>({state:'saved'})});await flush();await flush();
  check(reads===readsBefore+1 && state()!=='Paused','a saved move reloads the board');
  right(groupRow().cells[3]);items()[0].click();
  saves[1].resolve({ok:false,json:async()=>({reason:'next: cannot set entry: busy'})});await flush();
  const error=document.querySelector('[data-color-save-error]');
  check(!error.hidden && error.textContent==='units: next: cannot set entry: busy','a failed move reports its reason in the status line');
 }catch(error){failures.push(String(error));}
 await realFetch('/results',{method:'POST',body:JSON.stringify(failures)});
})();</script>'''


def firefox_probe_results(page: bytes) -> list[list[str]]:
    """Load the page in a headless Firefox frame and return the failure lists its probe posts."""
    results: list[list[str]] = []
    complete = threading.Event()

    class BrowserProbe(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            if self.path == "/complete":
                _ = complete.wait(TEST_HANG_GUARD_SECS)
            elif self.path == "/":
                _ = self.wfile.write(b'<iframe src="/accounts"></iframe><img hidden src="/complete">')
            else:
                _ = self.wfile.write(page)

        def do_POST(self) -> None:
            payload = self.rfile.read(int(self.headers["Content-Length"]))
            results.append(cast(list[str], json.loads(payload)))
            complete.set()
            self.send_response(204)
            self.end_headers()

        @override
        def log_message(self, format: str, *args: object) -> None:
            pass

    with tempfile.TemporaryDirectory() as temporary, ThreadingHTTPServer(("127.0.0.1", 0), BrowserProbe) as server:
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        try:
            profile = Path(temporary) / "profile"
            profile.mkdir()
            run = subprocess.run(["firefox", "--headless", "--no-remote", "--profile", str(profile),
                                  "--screenshot", str(Path(temporary) / "page.png"), f"http://127.0.0.1:{server.server_port}/"],
                                 capture_output=True, text=True, timeout=45)
            if run.returncode:
                raise AssertionError(run.stderr)
        finally:
            server.shutdown()
            thread.join(TEST_HANG_GUARD_SECS)
    return results


class AccountsPageTests(unittest.TestCase):
    @override
    def setUp(self) -> None:
        configuration = Path(self.enterContext(tempfile.TemporaryDirectory())) / "config"
        _ = copy_config_fixture(Path(__file__).resolve().parents[2] / 'config', configuration)
        _ = write_config_fixture(configuration / "accounts.conf", "next_move_percent=1\nusage_stale_minutes=10\n")
        _ = write_config_fixture(configuration / "review_links.conf",
            "page_refresh_seconds=15\naccounts_page_seconds=30\n")
        _ = (configuration / "statusline.conf").write_text("account.claude1=#af875f\naccount.claude2=#b294bb\nrole.unit-director=#d7af00\n",
                                                            encoding="utf-8")
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"CONF_DIR": str(configuration)})))

    def test_board_draws_the_board_table_with_moves_and_problems(self) -> None:
        config = Path(self.enterContext(tempfile.TemporaryDirectory(prefix='accounts-board-config-')))
        _ = write_config_fixture(config / 'accounts.conf')
        _ = (config / 'statusline.conf').write_text('account.claude1=brown\nrole.unit-director=yellow\nrole.worker=#c0c0c0\n')
        _ = cast(object, self.enterContext(patch.dict(os.environ, {'CONF_DIR': str(config)})))
        reading = accounts.StatusReading((status_row("claude1"),), ("* <stale>",), (), timezone.utc, AT,
                                         upcoming_moves=("Upcoming moves:", "units → claude1"))
        markup = accounts_page.board(reading)
        table = accounts.status_board(reading)
        self.assertEqual([column["key"] for column in table["columns"]][1:4], ["label", "left", "roles"])
        self.assertIn('<thead><tr><th class="left"></th><th class="left">account</th><th class="left"></th>'
                      + '<th class="right">left</th><th class="left">role</th><th class="left"></th><th class="right">busy</th>', markup)
        self.assertNotIn(">Color<", markup)
        self.assertEqual(markup.count('<tr class="account">'), 2)
        self.assertEqual(markup.count("<tr>"), 1)
        self.assertIn('<td class="left" data-account-name><span class="default bold" style="color:#af875f" '
                      + 'data-color-text="account.claude1">claude1</span></td><td class="left"><button type="button" class="color-swatch" data-color-key="account.claude1" '
                      + 'data-color-name="claude1" data-saved-color="#af875f" style="background:#af875f" '
                      + 'aria-label="Change claude1 color" aria-haspopup="dialog"></button></td>'
                      + '<td class="right"><span class="default">60%</span></td>', markup)
        self.assertIn('<td class="left"><span class="default" style="color:#808000" data-color-text="role.unit-director">units</span></td>'
                      + '<td class="left"><button type="button" class="color-swatch" data-color-key="role.unit-director" '
                      + 'data-color-name="units" data-saved-color="#808000" style="background:#808000" '
                      + 'aria-label="Change units color" aria-haspopup="dialog"></button></td>'
                      + '<td class="right"><span class="default">1</span></td>', markup)
        self.assertIn('<td class="left" data-account-name><span class="default bold">total</span></td>', markup)
        self.assertIn('<td class="left"></td>', markup)
        self.assertIn('<td class="right"><span class="default">60%</span></td>', markup)
        self.assertIn('<li>units → claude1</li>', markup)
        self.assertIn('<h2>Problems</h2><ul class="problems"><li>&lt;stale&gt;</li></ul>', markup)
        multiple_roles = replace(reading, rows=(replace(status_row("claude1"), roles=("units", "workers", "spare"),
            role_counts=(accounts.RoleSessionCounts(1, 0, 0, 0),) * 2 + (accounts.NoRoleCount(),)),))
        markup = accounts_page.board(multiple_roles)
        self.assertEqual(markup.count('class="color-swatch"'), 3)
        self.assertIn('data-color-key="role.worker" data-color-name="workers" data-saved-color="#c0c0c0"', markup)
        self.assertIn('<td class="left"><span class="default">spare</span></td><td class="left"></td>', markup)
        self.assertNotIn('data-color-name="total"', markup)
        table = accounts.status_board(multiple_roles)
        self.assertEqual(markup.count("<td "), len(table["rows"]) * (len(table["columns"]) + 2))

    def test_account_labels_use_terminal_colors_and_hex_without_css_injection(self) -> None:
        reading = accounts.StatusReading((status_row("claude1"),), (), (), timezone.utc, AT)
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "statusline.conf"
            _ = write_config_fixture(config.parent / 'accounts.conf')
            for value, expected in (("brown", "#af875f"), ("magenta", "#800080"),
                                    ("#A0522D", "#A0522D"), ("#af875f", "#af875f"), ("", ""),
                                    ('red;position:fixed', ""), ('\"><script>alert(1)</script>', "")):
                with self.subTest(value=value), patch.dict(os.environ, {"CONF_DIR": temporary}):
                    _ = config.write_text(f"account.claude1={value}\n" if value else "# no account key\n")
                    markup = accounts_page.board(reading)
                    style = f' style="color:{expected}" data-color-text="account.claude1"' if expected else ""
                    self.assertIn(f'<span class="default bold"{style}>claude1</span>', markup)
                    self.assertNotIn("position:fixed", markup)
                    self.assertNotIn("<script>", markup)
                    self.assertEqual('aria-label="Change claude1 color"' in markup, bool(expected))
                    if value == "#af875f":
                        self.assertEqual(conf.read("statusline").values()["account.claude1"], value)
                        self.assertEqual(accounts.account_color(value), accounts.AccountColor("\x1b[38;2;175;135;95m", value))
                        shell = subprocess.run(["bash", "-c", 'source "$1"; conf_value statusline account.claude1',
                            "hex-reader", str(Path(__file__).resolve().parents[1] / "lib/conf.sh")],
                            capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
                        self.assertEqual((shell.returncode, shell.stdout), (0, value + "\n"), shell.stderr)
                        from scripts.statusline.test_statusline import status_lines
                        self.assertEqual(status_lines({}, colors=config)[1].split(" | ")[0],
                                         "\x1b[38;2;175;135;95mclaude1\x1b[0m")

    def test_color_save_validates_the_request_and_uses_this_checkouts_tune(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {"CONF_DIR": temporary}):
            _ = (Path(temporary) / "statusline.conf").write_text(
                "account.claude1=brown\nrole.units=teal\nrole.worker=white\nlevel.ok=green\n")
            for key, value in (("account.unknown", "#af875f"), ("claude1", "#af875f"), ("role.units", "#af875f"),
                               ("role.showrunner", "#af875f"), ("level.ok", "#af875f"), ("account.claude1", "brown"),
                               ("account.claude1", "#AF875F"), ("account.claude1", "#fff"), ("account.claude1", "#af875f\n")):
                with self.subTest(key=key, value=value), patch.object(subprocess, "run") as run:
                    self.assertIsInstance(accounts_page.save_color(key, value), accounts_page.Refused)
                    run.assert_not_called()
            for status, errors, expected in ((0, "", accounts_page.Saved()),
                    (2, "first line\ntune: cannot publish\n", accounts_page.SaveFailed("tune: cannot publish")),
                    (1, "", accounts_page.SaveFailed("Tune exited 1."))):
                with self.subTest(status=status), patch.object(subprocess, "run",
                        return_value=subprocess.CompletedProcess((), status, "", errors)) as run:
                    self.assertEqual(accounts_page.save_color("account.claude1", "#af875f"), expected)
                    root = Path(accounts_page.__file__).resolve().parents[1]
                    run.assert_called_once_with((str(root / "lib/py"), str(root / "lib/tune.py"),
                        "statusline", "account.claude1", "#af875f", "--why",
                        "The user chose this color with the Accounts page color chooser.", "--user-words",
                        "account.claude1 set to #af875f with Save on the console Accounts page"),
                        capture_output=True, text=True, check=False)
            with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess((), 0, "", "")) as run:
                self.assertEqual(accounts_page.save_color("role.worker", "#123456"), accounts_page.Saved())
                self.assertEqual(cast(tuple[str, ...], run.call_args.args[0])[2:5], ("statusline", "role.worker", "#123456"))
            with patch.object(subprocess, "run", side_effect=OSError("runner unavailable")):
                self.assertEqual(accounts_page.save_color("account.claude1", "#af875f"),
                                 accounts_page.SaveFailed("runner unavailable"))

    def test_board_marks_the_selected_claude_group_lines_and_lists_the_claude_targets(self) -> None:
        rows = (replace(status_row("claude1"), selected_roles=("units",)), replace(status_row("claude2"), weekly_left_percent=12.4),
                replace(status_row("codex1"), tool="codex", selected_roles=("units",)))
        reading = accounts.StatusReading(rows, (), (), timezone.utc, AT)
        markup = accounts_page.board(reading)
        self.assertIn('<tr class="account" data-move-account="claude1" data-move-group="units">', markup)
        self.assertEqual(markup.count("data-move-group="), 1)
        targets = json.dumps([["claude1", "60%", "#af875f", "default"], ["claude2", "12%", "#b294bb", "warning"]])
        self.assertIn(f'<section class="card" data-move-percent="1" data-move-targets="{html.escape(targets, quote=True)}">', markup)
        settings = Path(os.environ["CONF_DIR"]) / "accounts.conf"
        _ = settings.write_text(settings.read_text().replace("next_move_percent=1\n", "next_move_percent=2.5\n"))
        self.assertIn('data-move-percent="2.5"', accounts_page.board(reading))

    def test_next_move_save_checks_the_group_and_account_then_sets_the_first_entry_at_the_configured_percent(self) -> None:
        with patch.object(next_moves, "set_first_next_move") as setter:
            self.assertEqual(accounts_page.save_next_move("workers", "claude2"),
                             accounts_page.Refused("Choose units or showrunners."))
            with patch.object(accounts, "claude_account_dir", side_effect=accounts.AccountsError("accounts: claude9 has no note")):
                self.assertEqual(accounts_page.save_next_move("units", "claude9"),
                                 accounts_page.Refused("accounts: claude9 has no note"))
            setter.assert_not_called()
        with patch.object(accounts, "claude_account_dir", return_value=Path("/nonexistent/claude2")):
            with patch.object(next_moves, "set_first_next_move") as setter:
                self.assertEqual(accounts_page.save_next_move("showrunners", "claude2"), accounts_page.Saved())
                setter.assert_called_once_with("showrunners", 1.0, "claude2")
            with patch.object(next_moves, "set_first_next_move", side_effect=next_moves.NextMoveError("next: cannot set entry: busy")):
                self.assertEqual(accounts_page.save_next_move("units", "claude2"),
                                 accounts_page.SaveFailed("next: cannot set entry: busy"))

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required for browser behavior")
    def test_a_right_click_on_a_group_line_opens_the_move_menu_and_saves_the_chosen_account(self) -> None:
        rows = (replace(status_row("claude1"), selected_roles=("units",)), status_row("claude2"),
                replace(status_row("claude3"), weekly_left_percent=accounts.StatusUnavailable("unavailable")),
                replace(status_row("codex1"), tool="codex"))
        reading = accounts.StatusReading(rows, (), (), timezone.utc, AT)
        with patch.object(accounts, "read_status", return_value=reading):
            content = accounts_page.page()
        initial = accounts_page.AccountsRender(content, 0.5, 200).with_recent_times((0.5,)).decode()
        setup = MENU_SETUP.replace("FRESH_PAGE", json.dumps(initial).replace("</", "<\\/"))
        page = initial.replace("<script>", setup + "<script>", 1).replace("</body>", MENU_PROBE + "</body>").encode()
        self.assertEqual(firefox_probe_results(page), [[]])

    def test_page_reads_stored_status_and_holds_the_refresh_root(self) -> None:
        reading = accounts.StatusReading((), (), (), timezone.utc, AT)
        with tempfile.TemporaryDirectory() as temporary:
            settings = Path(temporary) / "review_links.conf"
            _ = write_config_fixture(Path(temporary) / 'accounts.conf')
            for seconds in (15, 37):
                with self.subTest(refresh_seconds=seconds):
                    _ = write_config_fixture(settings, f"page_refresh_seconds={seconds}\n")
                    with (patch.object(accounts, "read_status", return_value=reading) as status,
                          patch.dict(os.environ, {"CONF_DIR": temporary})):
                        page = accounts_page.page().decode()
                    source: object = status.call_args.kwargs["source"]  # pyright: ignore[reportAny]
                    self.assertIsInstance(source, accounts.StoredUsageOnly)
                    self.assertIn("<div data-accounts>", page)
                    self.assertIn(console_theme.CSS, page)
                    self.assertIn(page_frame.STYLE, page)
                    self.assertIn('<h1 class="page-title">Accounts</h1>', page)
                    self.assertIn("'console-shown'", page)
                    self.assertIn(f'data-refresh-seconds="{float(seconds)}"', page)
                    self.assertEqual(page.count(page_frame.REFRESH_SCRIPT), 1)
                    self.assertIn("createPageRefresh(refresh, () => document.querySelector('main').dataset.refreshSeconds, hold)", page)
                    self.assertNotIn('<input type="color"', page)
                    self.assertNotIn('showPicker', page)
                    self.assertIn('data-color-area role="slider"', page)
                    self.assertIn('type="range" id="account-color-hue"', page)
                    self.assertIn('type="text" id="account-color-value"', page)
                    self.assertIn('data-color-preview', page)
                    for action in ("Try", "Save", "Cancel"):
                        self.assertIn(f'>{action}</button>', page)
                    self.assertNotIn('previousElementSibling', page)

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required for browser checks")
    def test_page_status_uses_the_real_accounts_loader_with_configured_timing(self) -> None:
        reading = accounts.StatusReading((), (), (), timezone.utc, AT)
        with tempfile.TemporaryDirectory() as temporary:
            settings = Path(temporary) / "review_links.conf"
            _ = write_config_fixture(Path(temporary) / 'accounts.conf')
            for seconds in (15, 37):
                with self.subTest(refresh_seconds=seconds):
                    _ = write_config_fixture(settings, f"page_refresh_seconds={seconds}\n")
                    with (patch.object(accounts, "read_status", return_value=reading),
                          patch.dict(os.environ, {"CONF_DIR": temporary})):
                        page = accounts_page.page().decode()
                    assert_status_states(self, page, seconds)

    def test_cached_render_failure_reaches_the_real_loader_and_keeps_the_board(self) -> None:
        reading = accounts.StatusReading((), (), (), timezone.utc, AT)
        with patch.object(accounts, "read_status", return_value=reading):
            content = accounts_page.page()
        content = content.replace(b'<div data-accounts>', b'<div data-accounts><p>board</p>')
        cache = serve.AccountsPageCache()
        with patch.object(accounts_page, "render_with_facts", return_value=accounts_page.AccountsRender(content, .1, time.time())):
            good = cache.replace()
        with patch.object(accounts_page, "render_with_facts", side_effect=OSError("private failure")):
            failed = cache.replace()
        self.assertIn(b'data-read-error="The Accounts render failed."', failed)
        self.assertNotIn(b'private failure', failed)
        self.assertEqual(cache.page, good)
        self.assertEqual(tuple(cache.recent_times), (.1,))

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required for browser checks")
    def test_page_status_reports_a_cached_accounts_render_failure(self) -> None:
        reading = accounts.StatusReading((), (), (), timezone.utc, AT)
        with patch.object(accounts, "read_status", return_value=reading):
            content = accounts_page.page()
        content = content.replace(b'<div data-accounts>', b'<div data-accounts><p>board</p>')
        cache = serve.AccountsPageCache()
        with patch.object(accounts_page, "render_with_facts", return_value=accounts_page.AccountsRender(content, .1, time.time())):
            good = cache.replace()
        with patch.object(accounts_page, "render_with_facts", side_effect=OSError("private failure")):
            failed = cache.replace()
        assert_status_states(self, good.decode(), 15, failed_markup=failed.decode())

    def test_render_runs_the_page_in_a_child_and_names_its_failure(self) -> None:
        with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess((), 0, b"<p>page</p>", b"")) as run:
            self.assertEqual(accounts_page.render(), b"<p>page</p>")
        self.assertEqual(run.call_args.args[0], accounts_page.CHILD)
        with (patch.object(subprocess, "run", return_value=subprocess.CompletedProcess((), 0, b'<p data-page-facts></p>', b"")),
              patch.object(time, "perf_counter", side_effect=[10, 10.75]),
              patch.object(time, "time", return_value=100)):
            rendered = accounts_page.render_with_facts()
        markup = rendered.with_recent_times((0.25, rendered.seconds))
        self.assertIn(b'data-render-seconds="0.750000"', markup)
        self.assertIn(b'data-read-at="100.000000"', markup)
        self.assertIn(b'data-render-median="0.500000"', markup)
        self.assertIn(b'data-render-count="2"', markup)
        with (patch.object(subprocess, "run", return_value=subprocess.CompletedProcess((), 1, b"", b"Traceback\nOSError: no staff\n")),
              self.assertRaisesRegex(OSError, "exited 1: OSError: no staff$")):
            _ = accounts_page.render()
        with (patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired((), 30)),
              self.assertRaisesRegex(OSError, "took longer than 30 s")):
            _ = accounts_page.render()

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required for browser behavior")
    def test_refresh_obeys_visibility_and_updates_the_load_time_and_data_age(self) -> None:
        reading = accounts.StatusReading((status_row("claude1"), status_row("claude2")), (), (), timezone.utc, AT)
        with patch.object(accounts, "read_status", return_value=reading):
            content = accounts_page.page()
        initial = accounts_page.AccountsRender(content, 0.75, 200).with_recent_times((0.25, 0.75)).decode()
        fresh = accounts_page.AccountsRender(content, 0.6, 220).with_recent_times((0.6,)).decode()
        setup = '''<script>
const realFetch=fetch, intervals=new Map();let nextTimer=0, calls=0, visible='visible', tick=0;
Date.now=()=>214000;
Object.defineProperty(performance,'getEntriesByType',{value:()=>[{requestStart:100,responseEnd:350}]});
Object.defineProperty(performance,'now',{value:()=>{tick+=15001;return tick;}});
Object.defineProperty(document,'visibilityState',{get:()=>visible});
window.requestAnimationFrame=run=>setTimeout(()=>run(performance.now()),0);
window.setInterval=(load,ms)=>{intervals.set(++nextTimer,{load,ms});return nextTimer;};
window.clearInterval=id=>intervals.delete(id);
window.fetch=async()=>{calls++;return {ok:true,text:async()=>FRESH_PAGE};};
</script>'''.replace("FRESH_PAGE", json.dumps(fresh).replace("</", "<\\/"))
        probe = '''<script>
(async()=>{
 const failures=[], check=(ok,label)=>{if(!ok) failures.push(label);};
 const flush=()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(()=>setTimeout(resolve,0))));
 const line=()=>document.querySelector('[data-page-status]');
 const state=()=>line().dataset.state;
 const timer=()=>[...intervals.values()].find(value=>value.ms===15000);
 try{
  if(document.readyState!=='complete') await new Promise(resolve=>addEventListener('load',resolve,{once:true}));
  await flush();
  check(state()==='Live' && line().textContent.includes('14 s ago') && line().textContent.includes('render 0.75 s'),'first timing');
  check(line().querySelector('[data-status-render]').title==='Median 0.50 s of the last 2 renders.','median hover text');
  check(timer(),'the Accounts interval');
  const swatch=document.querySelector('[data-color-key="account.claude1"]');
  swatch.scrollIntoView({block:'nearest',inline:'nearest'});
  frameElement.style.height='560px';
  const popup=document.querySelector('[data-color-picker]'), picker=popup.querySelector('#account-color-value');
  const area=popup.querySelector('[data-color-area]'), hue=popup.querySelector('#account-color-hue');
  const preview=popup.querySelector('[data-color-preview]');
  const pick=value=>{picker.value=value;picker.dispatchEvent(new Event('input',{bubbles:true}));};
  const key=(control,value,shiftKey=false)=>control.dispatchEvent(new KeyboardEvent('keydown',
   {key:value,shiftKey,bubbles:true,cancelable:true}));
  const label=()=>swatch.closest('tr').querySelector('[data-account-name] span');
  const action=name=>popup.querySelector('[data-color-'+name+']');
  const original=label().style.color, originalSwatch=swatch.style.background;
  const row=swatch.closest('tr'), cell=swatch.closest('td');
  const heads=[...document.querySelectorAll('thead th')].map(head=>head.textContent);
  check(cell===row.cells[2] && row.cells[1].contains(label()) && heads[1]==='account' && heads[2]==='' && heads[3]==='left',
   'the swatch column without a heading sits between account and left');
  check(heads[4]==='role' && heads[5]==='' && heads[6]==='busy','the role swatch column sits between role and busy');
  check(getComputedStyle(cell).textAlign==='left' && getComputedStyle(swatch).marginLeft==='0px' &&
   Math.abs(swatch.getBoundingClientRect().left-cell.getBoundingClientRect().left-parseFloat(getComputedStyle(cell).paddingLeft))<1,
   'the swatch aligns with the cell content on the left');
  const other=document.querySelector('[data-color-key="account.claude2"]');
  const otherLabel=other.closest('tr').querySelector('[data-account-name] span'), otherColor=otherLabel.style.color;
  const normalFetch=window.fetch;let pendingRead;
  window.fetch=()=>new Promise(resolve=>pendingRead=resolve);
  timer().load();
  swatch.click();
  check(popup.open && document.activeElement===area && area.getBoundingClientRect().height>=130,
   'the swatch opens the visible picker before the click returns');await flush();
  pendingRead({ok:true,text:async()=>'<div data-accounts>old response</div>'});await flush();
  check(swatch.isConnected && popup.open,'an in-flight refresh cannot replace an open chooser');
  window.fetch=normalFetch;
  check(popup.open && picker.value===swatch.dataset.savedColor,'the picker starts with the saved color');
  check([...popup.querySelectorAll('input,button')].every(control=>
   getComputedStyle(control).fontFamily===getComputedStyle(popup).fontFamily &&
   getComputedStyle(control).fontSize===getComputedStyle(popup).fontSize),'the popup controls inherit its font');
  const rect=popup.getBoundingClientRect();
  check(rect.left>=0 && rect.right<=innerWidth && rect.top>=0 && rect.bottom<=innerHeight,'the popup fits a phone viewport');
  frameElement.style.width='280px';dispatchEvent(new Event('resize'));
  const narrow=popup.getBoundingClientRect();
  check(narrow.left>=0 && narrow.right<=innerWidth && popup.scrollWidth<=popup.clientWidth,
   'the picker fits a narrow phone without horizontal scrolling');
  frameElement.style.width='420px';dispatchEvent(new Event('resize'));
  swatch.scrollIntoView({block:'nearest',inline:'nearest'});dispatchEvent(new Event('scroll'));
  const aligned=popup.getBoundingClientRect();
  check(Math.abs(aligned.left-Math.max(8,Math.min(swatch.getBoundingClientRect().left,innerWidth-aligned.width-8)))<1,
   'the popup stays beside the swatch within the viewport');
  frameElement.style.width='';dispatchEvent(new Event('resize'));
  document.documentElement.dataset.theme='light';const light=getComputedStyle(popup).backgroundColor;
  document.documentElement.dataset.theme='dark';const dark=getComputedStyle(popup).backgroundColor;
  check(light!==dark,'the popup uses both themes');delete document.documentElement.dataset.theme;
  check(!popup.querySelector('input[type=color]') && hue.type==='range','the picker uses page controls instead of a native color input');
  check(preview.getBoundingClientRect().height>=56 && preview.getBoundingClientRect().width>200,
   'the dialog shows a large box of color');
  pick('#FF0000');
  check(picker.value==='#ff0000' && hue.value==='0' && preview.style.background==='rgb(255, 0, 0)',
   'typed hex updates the hue and the large color box');
  hue.value='120';hue.dispatchEvent(new Event('input',{bubbles:true}));
  check(picker.value==='#00ff00','the hue slider selects green');
  hue.value='240';hue.dispatchEvent(new Event('input',{bubbles:true}));
  check(picker.value==='#0000ff','the hue slider selects blue');
  key(hue,'Home');check(picker.value==='#ff0000','the hue slider supports the Home key');
  key(hue,'ArrowRight',true);check(hue.value==='10','the hue slider supports keyboard steps');
  key(hue,'ArrowLeft');check(hue.value==='9','the hue slider supports reverse keyboard steps');
  key(hue,'End');check(hue.value==='360' && picker.value==='#ff0000','the hue slider supports the End key');
  key(area,'ArrowLeft',true);key(area,'ArrowDown',true);
  check(area.getAttribute('aria-valuetext')==='Saturation 90%, brightness 90%' && picker.value==='#e61717',
   'area keys change saturation and brightness with an accessible value');
  key(area,'ArrowRight',true);key(area,'ArrowUp',true);
  check(picker.value==='#ff0000','area keys return to full saturation and brightness');
  const box=area.getBoundingClientRect();
  const point=(type,x,y)=>area.dispatchEvent(new PointerEvent(type,{pointerId:7,button:0,
   clientX:box.left+box.width*x,clientY:box.top+box.height*y,bubbles:true}));
  point('pointerdown',0,0);check(picker.value==='#ffffff','the area selects white at the upper left');
  point('pointermove',1,.5);check(picker.value==='#800000','a pointer drag changes saturation and brightness');
  point('pointermove',2,2);check(picker.value==='#000000','a pointer drag clamps at the area edges');
  point('pointerup',2,2);point('pointermove',1,0);
  check(picker.value==='#000000','a released pointer stops changing the pick');
  point('pointerdown',1,0);point('pointercancel',1,0);point('pointermove',0,0);
  check(picker.value==='#ff0000','a cancelled pointer stops changing the pick');
  pick('#234567');
  check(preview.style.background==='rgb(35, 69, 103)' && label().style.color===original &&
   swatch.style.background===originalSwatch,'a pick changes only the color box');
  pick('invalid');
  check(!picker.checkValidity() && action('try').disabled && action('save').disabled,
   'an invalid hex value cannot be tried or saved');
  pick('#123456');timer().load();await flush();
  check(calls===0 && label().style.color===original && swatch.style.background===originalSwatch,
   'picking a color keeps the page unchanged without a request');
  action('try').click();
  check(popup.open && label().style.color==='rgb(18, 52, 86)' && swatch.style.background==='rgb(18, 52, 86)' && calls===0,
   'Try paints the account name and swatch without a request');
  check(swatch.dataset.savedColor==='#af875f','Try keeps the saved color unchanged');
  pick('#234567');
  check(label().style.color==='rgb(18, 52, 86)' && preview.style.background==='rgb(35, 69, 103)',
   'a later pick keeps the last tried color on the page');
  check(otherLabel.style.color===otherColor,'Try keeps the other account color');
  check(state()==='Paused','the chooser pauses refresh');
  action('cancel').click();
  check(!popup.open && label().style.color===original && swatch.style.background===originalSwatch,'Cancel restores the saved color');
  swatch.click();pick('#abcdef');action('try').click();
  popup.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true,cancelable:true}));
  check(!popup.open && label().style.color===original && swatch.style.background===originalSwatch,'Escape restores the saved color');
  swatch.click();
  check(popup.open && picker.value===swatch.dataset.savedColor,'a reopened picker starts with the saved color');
  pick('#654321');action('try').click();
  check(label().style.color==='rgb(101, 67, 33)' && swatch.style.background==='rgb(101, 67, 33)','Try applies a typed hex value');
  popup.dispatchEvent(new Event('cancel',{cancelable:true}));
  check(!popup.open && label().style.color===original && swatch.style.background===originalSwatch,'dialog cancel restores both saved colors');
  const beforeReopenedSaveFetch=window.fetch;let reopenedSave;
  window.fetch=(path,options)=>options?.method==='POST' ?
   new Promise(resolve=>reopenedSave=resolve) : beforeReopenedSaveFetch(path,options);
  swatch.click();pick('#123456');action('save').click();swatch.click();
  check(popup.open && picker.value==='#123456','the same account reopens with its pending color');
  pick('#654321');action('try').click();pick('invalid');
  reopenedSave({ok:false,json:async()=>({reason:'reopened save failed'})});await flush();
  check(label().style.color===original && swatch.style.background===originalSwatch && swatch.dataset.savedColor==='#af875f',
   'a failed save restores the account colors while its dialog remains open');
  check(popup.open && picker.value==='#af875f' && preview.style.background===originalSwatch && picker.checkValidity() &&
   !action('try').disabled && !action('save').disabled,'a failed save restores the reopened picker and its usable controls');
  check(line().textContent.includes('claude1: reopened save failed') && !line().querySelector('[data-color-save-error]').hidden,
   'a failed reopened save reports its error in the page status');
  check(calls===0 && swatch.isConnected && state()==='Paused','the reopened dialog keeps refresh paused after a failed save');
  pick('#234567');action('try').click();
  check(label().style.color==='rgb(35, 69, 103)' && swatch.style.background==='rgb(35, 69, 103)',
   'Try remains usable after the reopened save fails');
  action('cancel').click();
  check(!popup.open && label().style.color===original && swatch.style.background===originalSwatch,
   'Cancel restores the saved colors after the reopened save fails');
  window.fetch=beforeReopenedSaveFetch;
  const pageFetch=window.fetch, saves=[];
  window.fetch=(path,options)=>options?.method==='POST' ?
   new Promise((resolve,reject)=>saves.push({path,options,resolve,reject})) : pageFetch(path,options);
  swatch.click();picker.value='#123456';action('save').click();
  check(!popup.open && [...popup.querySelectorAll('input,button')].every(control=>!control.disabled),
   'Save closes the dialog before the POST completes');
  check(label().style.color==='rgb(18, 52, 86)' && swatch.style.background==='rgb(18, 52, 86)',
   'Save retains the new name and swatch colors');
  check(swatch.dataset.savedColor==='#af875f','a pending POST retains the previous saved color');
  check(saves.length===1 && saves[0].path===location.pathname && saves[0].options.method==='POST','Save posts to this page');
  const form=new URLSearchParams(saves[0].options.body);
  check(form.get('key')==='account.claude1' && form.get('value')==='#123456' && [...form.keys()].length===2 &&
   saves[0].options.headers['Content-Type']==='application/x-www-form-urlencoded','Save sends the form fields');
  timer().load();dispatchEvent(new MessageEvent('message',{source:parent,origin:location.origin,data:'console-shown'}));await flush();
  check(calls===0 && swatch.isConnected && state()==='Paused','a pending POST holds interval and visibility refreshes');
  other.click();
  check(popup.open && picker.value===other.dataset.savedColor,'another swatch opens while a save is pending');
  picker.value='#fedcba';action('save').click();
  check(!popup.open && saves.length===2,'another account saves without waiting for the first POST');
  const otherForm=new URLSearchParams(saves[1].options.body);
  check(otherForm.get('key')==='account.claude2' && otherForm.get('value')==='#fedcba','each pending POST retains its account and color');
  saves[1].resolve({ok:true,json:async()=>({state:'saved'})});await flush();
  check(other.dataset.savedColor==='#fedcba' && otherLabel.style.color==='rgb(254, 220, 186)',
   'success stores the second account color');
  check(calls===0 && swatch.isConnected,'refresh remains held until every POST completes');
  other.click();
  saves[0].resolve({ok:false,json:async()=>({reason:'tune: cannot publish'})});await flush();
  check(popup.open && picker.value==='#fedcba','a failed earlier save keeps another account dialog open');
  check(label().style.color===original && swatch.style.background===originalSwatch && swatch.dataset.savedColor==='#af875f',
   'a failed POST restores its own name, swatch, and saved color');
  check(otherLabel.style.color==='rgb(254, 220, 186)' && other.dataset.savedColor==='#fedcba',
   'a failed earlier save preserves the other successful color');
  check(line().textContent.includes('claude1: tune: cannot publish') && !line().querySelector('[data-color-save-error]').hidden,
   'the page status line reports the save error');
  action('cancel').click();
  other.click();picker.value='#010203';action('save').click();
  swatch.click();picker.value='#abcdef';action('save').click();
  saves[2].reject(new Error('network unavailable'));await flush();
  check(other.dataset.savedColor==='#fedcba' && otherLabel.style.color==='rgb(254, 220, 186)' &&
   other.style.background==='rgb(254, 220, 186)','a network failure restores the second account prior colors');
  check(label().style.color==='rgb(171, 205, 239)' && calls===0,'a network failure preserves another pending color and the refresh hold');
  check(line().textContent.includes('claude2: network unavailable'),'the page status line reports network errors');
  swatch.click();
  saves[3].resolve({ok:true,json:async()=>({state:'saved'})});await flush();
  check(popup.open && swatch.dataset.savedColor==='#abcdef' && calls===0,'success keeps a newly opened chooser intact');
  action('cancel').click();
  check(label().style.color==='rgb(171, 205, 239)','Cancel retains a color that saved while the chooser was open');
  swatch.click();picker.value='#234567';action('save').click();
  swatch.click();picker.value='#654321';action('save').click();
  check(saves.length===5 && !popup.open,'the same account queues another save without holding the dialog open');
  saves[4].resolve({ok:true,json:async()=>({state:'saved'})});await flush();
  check(saves.length===6 && swatch.dataset.savedColor==='#234567' && label().style.color==='rgb(101, 67, 33)',
   'the same account publishes in order and retains its latest preview');
  other.click();
  saves[5].resolve({ok:false,json:async()=>({reason:'later save failed'})});await flush();
  check(popup.open && swatch.dataset.savedColor==='#234567' && label().style.color==='rgb(35, 69, 103)' &&
   swatch.style.background==='rgb(35, 69, 103)','a queued failure restores the previous successful color');
  action('cancel').click();
  swatch.click();picker.value='#abcdef';action('save').click();
  saves[6].resolve({ok:true,json:async()=>({state:'saved'})});await flush();
  check(swatch.dataset.savedColor==='#abcdef','success stores the chosen color before refresh');
  check(!popup.open && calls===1,'a successful save closes the popup and refreshes');
  const roles=[...document.querySelectorAll('[data-color-key="role.unit-director"]')];
  const roleWords=[...document.querySelectorAll('[data-color-text="role.unit-director"]')];
  check(roles.length===2 && roleWords.length===2 && roleWords.every(word=>word.textContent==='units' && word.style.color==='rgb(215, 175, 0)'),
   'each units row shows the unit color swatch and word');
  roles[1].click();
  check(popup.open && picker.value==='#d7af00' && popup.querySelector('#color-title').textContent==='Change units color',
   'a role swatch opens the picker with the role color');
  pick('#102030');action('try').click();
  check(roles.every(button=>button.style.background==='rgb(16, 32, 48)') &&
   roleWords.every(word=>word.style.color==='rgb(16, 32, 48)') &&
   document.querySelector('[data-color-key="account.claude1"]').style.background==='rgb(175, 135, 95)',
   'Try paints every units swatch and word and keeps the account colors');
  action('cancel').click();
  check(roles.every(button=>button.style.background==='rgb(215, 175, 0)') &&
   roleWords.every(word=>word.style.color==='rgb(215, 175, 0)'),'Cancel restores every units color');
  roles[0].click();pick('#405060');action('save').click();
  const roleForm=new URLSearchParams(saves[7].options.body);
  check(roleForm.get('key')==='role.unit-director' && roleForm.get('value')==='#405060','Save posts the role key');
  saves[7].resolve({ok:false,json:async()=>({reason:'role save failed'})});await flush();
  check(roles.every(button=>button.dataset.savedColor==='#d7af00' && button.style.background==='rgb(215, 175, 0)') &&
   roleWords.every(word=>word.style.color==='rgb(215, 175, 0)') && line().textContent.includes('units: role save failed'),
   'a failed role save restores every units color and names the role');
  window.fetch=pageFetch;calls=0;
  timer().load();await flush();
  check(calls===1,'the interval loads once');
  check(state()==='Live' && line().textContent.includes('0 s ago') && line().textContent.includes('render 0.60 s'),'refresh timing and clock skew');
  visible='hidden';document.dispatchEvent(new Event('visibilitychange'));await flush();
  check(!timer() && calls===1,'a hidden tab stops its timer');
  visible='visible';document.dispatchEvent(new Event('visibilitychange'));await flush();
  check(!!timer() && calls===2,'a visible tab restarts and loads once');
  frameElement.hidden=true;timer().load();await flush();
  check(calls===2,'a hidden frame does not load');
  frameElement.hidden=false;
  const shown=(source,origin)=>dispatchEvent(new MessageEvent('message',{source,origin,data:'console-shown'}));
  shown(window,location.origin);shown(parent,'https://other.invalid');await flush();
  check(calls===2,'other messages do not load');
  shown(parent,location.origin);await flush();
  check(calls===3,'the console shows the page and loads once');
  const priorTimers=new Set(intervals.keys()),loads=[];
  const imagesRefresh=createPageRefresh(value=>{loads.push(value);});
  const added=[...intervals.entries()].filter(([id])=>!priorTimers.has(id));
  check(added.length===1 && added[0][1].ms===1000,'a page without seconds adds only the age timer');
  await imagesRefresh.refresh(true);check(loads.length===1 && loads[0]===true,'the helper keeps supersede');
  const pending=[];
  window.fetch=()=>new Promise(resolve=>pending.push(resolve));
  const load=timer().load;
  load();shown(parent,location.origin);
  check(pending.length===2,'two refreshes overlap');
  const response=(board,seconds,finished)=>({ok:true,text:async()=>
   `<div data-accounts>${board}</div><p data-page-status data-page-facts data-render-seconds="${seconds}" `+
   `data-read-at="${finished}" data-render-median="1.10" data-render-count="2"></p>`});
  pending[1](response('newer board',1.2,210));await flush();
  const latest=()=>state()==='Live' && line().textContent.includes('4 s ago') && line().textContent.includes('render 1.20 s');
  check(document.querySelector('[data-accounts]').textContent==='newer board','the newer response updates the board');
  check(latest(),'the newer response updates the timing facts');
  pending[0](response('older board',0.8,190));await flush();
  check(document.querySelector('[data-accounts]').textContent==='newer board','a late older response keeps the newer board');
  check(latest(),'a late older response keeps the newer timing facts');
 }catch(error){failures.push(String(error));}
 await realFetch('/results',{method:'POST',body:JSON.stringify(failures)});
})();</script>'''
        page = initial.replace("<script>", setup + "<script>", 1).replace("</body>", probe + "</body>").encode()
        self.assertEqual(firefox_probe_results(page), [[]])


class ConsoleThemeTests(unittest.TestCase):
    def test_a_page_takes_the_theme_or_only_its_tokens_at_one_placeholder(self) -> None:
        self.assertEqual(console_theme.with_theme(f"<style>{console_theme.PLACEHOLDER}p{{}}</style>"),
                         f"<style>{console_theme.CSS}p{{}}</style>")
        self.assertEqual(console_theme.with_theme(f"<style>{console_theme.TOKENS_PLACEHOLDER}p{{}}</style>"),
                         f"<style>{console_theme.TOKENS}p{{}}</style>")
        self.assertTrue(console_theme.CSS.startswith(console_theme.TOKENS))
        # page_frame owns the main column and the page title.
        self.assertNotRegex(console_theme.CSS, r"(^|[}\n])(main|h1)\{")
        for source in ("<style></style>", console_theme.PLACEHOLDER + console_theme.TOKENS_PLACEHOLDER):
            with self.subTest(source=source), self.assertRaisesRegex(ValueError, "needs one"):
                _ = console_theme.with_theme(source)


if __name__ == "__main__":
    _ = unittest.main()
