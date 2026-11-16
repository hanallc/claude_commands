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

from scripts.accounts import accounts
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.review_links import accounts_page, console_theme, page_frame
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
        reading = accounts.StatusReading((status_row("claude1"),), ("* <stale>",), (), timezone.utc, AT,
                                         upcoming_moves=("Upcoming moves:", "units → claude1"))
        markup = accounts_page.board(reading)
        table = accounts.status_board(reading)
        heads = "".join(f'<th class="{column["align"]}">{column["heading"]}</th>' for column in table["columns"])
        self.assertIn(f"<thead><tr>{heads}</tr></thead>", markup)
        self.assertEqual(markup.count('<tr class="account">'), 2)
        self.assertEqual(markup.count("<tr>"), 1)
        self.assertIn('<td class="left"><span class="default bold" style="color:#af875f">claude1</span></td>', markup)
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
                                    ("#A0522D", "#A0522D"), ("", ""),
                                    ('red;position:fixed', ""), ('\"><script>alert(1)</script>', "")):
                with self.subTest(value=value), patch.dict(os.environ, {"CONF_DIR": temporary}):
                    _ = config.write_text(f"account.claude1={value}\n" if value else "# no account key\n")
                    markup = accounts_page.board(reading)
                    style = f' style="color:{expected}"' if expected else ""
                    self.assertIn(f'<span class="default bold"{style}>claude1</span>', markup)
                    self.assertNotIn("position:fixed", markup)
                    self.assertNotIn("<script>", markup)

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
                    self.assertIn("createPageRefresh(refresh, () => document.querySelector('main').dataset.refreshSeconds)", page)

    def test_render_runs_the_page_in_a_child_and_names_its_failure(self) -> None:
        with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess((), 0, b"<p>page</p>", b"")) as run:
            self.assertEqual(accounts_page.render(), b"<p>page</p>")
        self.assertEqual(run.call_args.args[0], accounts_page.CHILD)
        with (patch.object(subprocess, "run", return_value=subprocess.CompletedProcess((), 0, b'<p data-load-time></p>', b"")),
              patch.object(time, "perf_counter", side_effect=[10, 10.75]),
              patch.object(time, "time", return_value=100)):
            rendered = accounts_page.render_with_facts()
        markup = rendered.with_recent_times((0.25, rendered.seconds))
        self.assertIn(b'data-render-seconds="0.750000"', markup)
        self.assertIn(b'data-render-finished="100.000000"', markup)
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
        reading = accounts.StatusReading((), (), (), timezone.utc, AT)
        with patch.object(accounts, "read_status", return_value=reading):
            content = accounts_page.page()
        initial = accounts_page.AccountsRender(content, 0.75, 200).with_recent_times((0.25, 0.75)).decode()
        fresh = accounts_page.AccountsRender(content, 0.6, 220).with_recent_times((0.6,)).decode()
        setup = '''<script>
const realFetch=fetch, intervals=new Map();let nextTimer=0, calls=0, visible='visible', tick=0;
Date.now=()=>214000;
Object.defineProperty(performance,'getEntriesByType',{value:()=>[{requestStart:100,responseEnd:350}]});
Object.defineProperty(performance,'now',{value:()=>{tick+=30;return tick;}});
Object.defineProperty(document,'visibilityState',{get:()=>visible});
window.setInterval=(load,ms)=>{intervals.set(++nextTimer,{load,ms});return nextTimer;};
window.clearInterval=id=>intervals.delete(id);
window.fetch=async()=>{calls++;return {ok:true,text:async()=>FRESH_PAGE};};
</script>'''.replace("FRESH_PAGE", json.dumps(fresh).replace("</", "<\\/"))
        probe = '''<script>
(async()=>{
 const failures=[], check=(ok,label)=>{if(!ok) failures.push(label);};
 const flush=()=>new Promise(resolve=>setTimeout(resolve,0));
 const line=()=>document.querySelector('[data-load-time]').textContent;
 try{
  if(document.readyState!=='complete') await new Promise(resolve=>addEventListener('load',resolve,{once:true}));
  check(line()==='Data from 14 s ago. Loaded in 0.25 s. The last render took 0.75 s (median 0.50 s of the last 2).','first timing');
  check(intervals.size===1 && [...intervals.values()][0].ms===15000,'the Accounts interval');
  [...intervals.values()][0].load();await flush();
  check(calls===1,'the interval loads once');
  check(line()==='Data from 0 s ago. Loaded in 0.03 s. The last render took 0.60 s (median 0.60 s of the last 1).','refresh timing and clock skew');
  visible='hidden';document.dispatchEvent(new Event('visibilitychange'));await flush();
  check(intervals.size===0 && calls===1,'a hidden tab stops its timer');
  visible='visible';document.dispatchEvent(new Event('visibilitychange'));await flush();
  check(intervals.size===1 && calls===2,'a visible tab restarts and loads once');
  frameElement.hidden=true;[...intervals.values()][0].load();await flush();
  check(calls===2,'a hidden frame does not load');
  frameElement.hidden=false;
  const shown=(source,origin)=>dispatchEvent(new MessageEvent('message',{source,origin,data:'console-shown'}));
  shown(window,location.origin);shown(parent,'https://other.invalid');await flush();
  check(calls===2,'other messages do not load');
  shown(parent,location.origin);await flush();
  check(calls===3,'the console shows the page and loads once');
  const timers=intervals.size, loads=[];
  const imagesRefresh=createPageRefresh(value=>loads.push(value));
  check(intervals.size===timers,'a page without seconds has no timer');
  imagesRefresh.refresh(true);check(loads.length===1 && loads[0]===true,'the helper keeps supersede');
  const pending=[];
  window.fetch=()=>new Promise(resolve=>pending.push(resolve));
  const load=[...intervals.values()][0].load;
  load();load();
  check(pending.length===2,'two refreshes overlap');
  const response=(board,seconds,finished)=>({ok:true,text:async()=>
   `<div data-accounts>${board}</div><p data-load-time data-render-seconds="${seconds}" `+
   `data-render-finished="${finished}" data-render-median="1.10" data-render-count="2"></p>`});
  pending[1](response('newer board',1.2,210));await flush();
  const latest='Data from 4 s ago. Loaded in 0.03 s. The last render took 1.20 s (median 1.10 s of the last 2).';
  check(document.querySelector('[data-accounts]').textContent==='newer board','the newer response updates the board');
  check(line()===latest,'the newer response updates the timing facts');
  pending[0](response('older board',0.8,190));await flush();
  check(document.querySelector('[data-accounts]').textContent==='newer board','a late older response keeps the newer board');
  check(line()===latest,'a late older response keeps the newer timing facts');
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
