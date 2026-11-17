"""The console Accounts page draws the accounts board table."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
from scripts.accounts import accounts
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


class AccountsPageTests(unittest.TestCase):
    @override
    def setUp(self) -> None:
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"CONF_DIR": str(Path(__file__).resolve().parents[2] / "config")})))

    def test_board_draws_the_board_table_with_moves_and_problems(self) -> None:
        config = Path(self.enterContext(tempfile.TemporaryDirectory(prefix='accounts-board-config-')))
        _ = shutil.copyfile(Path(__file__).resolve().parents[2] / 'config/accounts.conf', config / 'accounts.conf')
        _ = (config / 'statusline.conf').write_text('account.claude1=brown\n')
        _ = cast(object, self.enterContext(patch.dict(os.environ, {'CONF_DIR': str(config)})))
        reading = accounts.StatusReading((status_row("claude1"),), ("* <stale>",), (), timezone.utc, AT,
                                         upcoming_moves=("Upcoming moves:", "units → claude1"))
        markup = accounts_page.board(reading)
        table = accounts.status_board(reading)
        heads = "".join(f'<th class="{column["align"]}">{column["heading"]}</th>' for column in table["columns"])
        self.assertIn(f"<thead><tr>{heads}</tr></thead>", markup)
        self.assertEqual(markup.count('<tr class="account">'), 2)
        self.assertEqual(markup.count("<tr>"), 1)
        self.assertIn('<td class="left"><span class="default bold" style="color:#af875f">claude1</span>'
                      + '<button type="button" class="color-swatch" data-account-color="claude1" '
                      + 'data-saved-color="#af875f" style="background:#af875f" '
                      + 'aria-label="Change claude1 color" aria-haspopup="dialog"></button></td>', markup)
        self.assertIn('<td class="left"><span class="default bold">total</span></td>', markup)
        self.assertIn('<td class="right"><span class="default">60%</span></td>', markup)
        self.assertIn('<li>units → claude1</li>', markup)
        self.assertIn('<h2>Problems</h2><ul class="problems"><li>&lt;stale&gt;</li></ul>', markup)

    def test_account_labels_use_terminal_colors_and_hex_without_css_injection(self) -> None:
        reading = accounts.StatusReading((status_row("claude1"),), (), (), timezone.utc, AT)
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "statusline.conf"
            _ = (config.parent / "accounts.conf").write_bytes(
                (Path(__file__).resolve().parents[2] / "config/accounts.conf").read_bytes())
            for value, expected in (("brown", "#af875f"), ("magenta", "#800080"),
                                    ("#A0522D", "#A0522D"), ("#af875f", "#af875f"), ("", ""),
                                    ('red;position:fixed', ""), ('\"><script>alert(1)</script>', "")):
                with self.subTest(value=value), patch.dict(os.environ, {"CONF_DIR": temporary}):
                    _ = config.write_text(f"account.claude1={value}\n" if value else "# no account key\n")
                    markup = accounts_page.board(reading)
                    style = f' style="color:{expected}"' if expected else ""
                    self.assertIn(f'<span class="default bold"{style}>claude1</span>', markup)
                    self.assertNotIn("position:fixed", markup)
                    self.assertNotIn("<script>", markup)
                    self.assertEqual('aria-label="Change claude1 color"' in markup, bool(value))
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
            _ = (Path(temporary) / "statusline.conf").write_text("account.claude1=brown\nrole.units=teal\n")
            for label, value in (("unknown", "#af875f"), ("units", "#af875f"), ("claude1", "brown"),
                                 ("claude1", "#AF875F"), ("claude1", "#fff"), ("claude1", "#af875f\n")):
                with self.subTest(label=label, value=value), patch.object(subprocess, "run") as run:
                    self.assertIsInstance(accounts_page.save_color(label, value), accounts_page.ColorRefused)
                    run.assert_not_called()
            for status, errors, expected in ((0, "", accounts_page.ColorSaved()),
                    (2, "first line\ntune: cannot publish\n", accounts_page.ColorSaveFailed("tune: cannot publish")),
                    (1, "", accounts_page.ColorSaveFailed("Tune exited 1."))):
                with self.subTest(status=status), patch.object(subprocess, "run",
                        return_value=subprocess.CompletedProcess((), status, "", errors)) as run:
                    self.assertEqual(accounts_page.save_color("claude1", "#af875f"), expected)
                    root = Path(accounts_page.__file__).resolve().parents[1]
                    run.assert_called_once_with((str(root / "lib/py"), str(root / "lib/tune.py"),
                        "statusline", "account.claude1", "#af875f", "--why",
                        "The user chose this color with the Accounts page color chooser.", "--user-words",
                        "claude1 color set to #af875f with Save on the console Accounts page"),
                        capture_output=True, text=True, check=False)
            with patch.object(subprocess, "run", side_effect=OSError("runner unavailable")):
                self.assertEqual(accounts_page.save_color("claude1", "#af875f"),
                                 accounts_page.ColorSaveFailed("runner unavailable"))

    def test_page_reads_stored_status_and_holds_the_refresh_root(self) -> None:
        reading = accounts.StatusReading((), (), (), timezone.utc, AT)
        with tempfile.TemporaryDirectory() as temporary:
            settings = Path(temporary) / "review_links.conf"
            original = (Path(__file__).resolve().parents[2] / "config/review_links.conf").read_text()
            for seconds in (15, 37):
                with self.subTest(refresh_seconds=seconds):
                    _ = settings.write_text(original.replace("page_refresh_seconds=15", f"page_refresh_seconds={seconds}"))
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
                    self.assertIn('<input type="color"', page)
                    for action in ("Try", "Save", "Cancel"):
                        self.assertIn(f'>{action}</button>', page)

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required for browser checks")
    def test_page_status_uses_the_real_accounts_loader_with_configured_timing(self) -> None:
        reading = accounts.StatusReading((), (), (), timezone.utc, AT)
        with tempfile.TemporaryDirectory() as temporary:
            settings = Path(temporary) / "review_links.conf"
            original = (Path(__file__).resolve().parents[2] / "config/review_links.conf").read_text()
            for seconds in (15, 37):
                with self.subTest(refresh_seconds=seconds):
                    _ = settings.write_text(original.replace("page_refresh_seconds=15", f"page_refresh_seconds={seconds}"))
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
        reading = accounts.StatusReading((status_row("claude1"),), (), (), timezone.utc, AT)
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
  const swatch=document.querySelector('[data-account-color="claude1"]');
  const popup=document.querySelector('[data-color-picker]'), picker=popup.querySelector('input');
  const label=()=>swatch.previousElementSibling, action=name=>popup.querySelector('[data-color-'+name+']');
  const original=label().style.color, originalSwatch=swatch.style.background;
  const normalFetch=window.fetch;let pendingRead;
  window.fetch=()=>new Promise(resolve=>pendingRead=resolve);
  timer().load();
  swatch.click();await flush();
  pendingRead({ok:true,text:async()=>'<div data-accounts>old response</div>'});await flush();
  check(swatch.isConnected && popup.open,'an in-flight refresh cannot replace an open chooser');
  window.fetch=normalFetch;
  check(popup.open && picker.value===swatch.dataset.savedColor,'the picker starts with the saved color');
  check([...popup.querySelectorAll('input,button')].every(control=>
   getComputedStyle(control).fontFamily===getComputedStyle(popup).fontFamily &&
   getComputedStyle(control).fontSize===getComputedStyle(popup).fontSize),'the popup controls inherit its font');
  const rect=popup.getBoundingClientRect();
  check(rect.left>=0 && rect.right<=innerWidth && rect.top>=0 && rect.bottom<=innerHeight,'the popup fits a phone viewport');
  document.documentElement.dataset.theme='light';const light=getComputedStyle(popup).backgroundColor;
  document.documentElement.dataset.theme='dark';const dark=getComputedStyle(popup).backgroundColor;
  check(light!==dark,'the popup uses both themes');delete document.documentElement.dataset.theme;
  picker.value='#123456';action('try').click();timer().load();await flush();
  check(calls===0 && label().style.color==='rgb(18, 52, 86)' && swatch.style.background==='rgb(18, 52, 86)','Try paints without a request');
  check(state()==='Paused','the chooser pauses refresh');
  action('cancel').click();
  check(!popup.open && label().style.color===original && swatch.style.background===originalSwatch,'Cancel restores the saved color');
  swatch.click();picker.value='#abcdef';action('try').click();
  popup.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true,cancelable:true}));
  check(!popup.open && label().style.color===original,'Escape restores the saved color');
  const pageFetch=window.fetch, saves=[];let resolveSave;
  window.fetch=(path,options)=>{saves.push({path,options});return new Promise(resolve=>resolveSave=resolve);};
  swatch.click();picker.value='#123456';action('save').click();
  check([...popup.querySelectorAll('button')].every(button=>button.disabled),'Save disables the buttons');
  check(popup.textContent.includes('Saving…'),'Save shows progress');
  check(saves.length===1 && saves[0].path===location.pathname && saves[0].options.method==='POST','Save posts to this page');
  const form=new URLSearchParams(saves[0].options.body);
  check(form.get('label')==='claude1' && form.get('value')==='#123456' &&
   saves[0].options.headers['Content-Type']==='application/x-www-form-urlencoded','Save sends the form fields');
  resolveSave({ok:false,json:async()=>({reason:'tune: cannot publish'})});await flush();
  check(popup.open && popup.textContent.includes('tune: cannot publish') && !action('cancel').disabled,'a save failure keeps the popup');
  check(label().style.color==='rgb(18, 52, 86)','a save failure keeps the tried color');
  action('cancel').click();check(label().style.color===original,'Cancel restores after a failed save');
  window.fetch=(path,options)=>options?.method==='POST' ?
   Promise.resolve({ok:true,json:async()=>({state:'saved'})}) : pageFetch(path,options);
  swatch.click();picker.value='#abcdef';action('save').click();await flush();
  check(!popup.open && calls===1,'a successful save closes the popup and refreshes');
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
                self.assertEqual(run.returncode, 0, run.stderr)
            finally:
                server.shutdown()
                thread.join(TEST_HANG_GUARD_SECS)
        self.assertEqual(results, [[]])


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
