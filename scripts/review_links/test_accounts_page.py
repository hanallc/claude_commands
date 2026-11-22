"""The console Accounts page draws the accounts board table."""
from __future__ import annotations
from scripts.lint.memory_test_support import copy_config_fixture, write_config_fixture

from collections import Counter
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import html
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from typing import cast, override
from unittest.mock import patch
from zoneinfo import ZoneInfo

from scripts.review_links.test_page_frame import assert_status_states
from scripts.accounts import accounts, next_moves, role_map
from scripts.accounts.home_test_support import scratch_account_home
from scripts.agents.test_support import write_machine_config
from scripts.lib import conf
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.review_links import accounts_page, console_theme, page_frame, serve
from scripts.whoami import agent_accounts, run_out, usage_store
from scripts.whoami.account_test_support import create_claude_account, stored_room

AT = datetime(2026, 11, 15, 15, tzinfo=timezone.utc)
ACCOUNT_SETTINGS = "next_move_percent=1\nusage_stale_minutes=10\n"
PAGE_STATUS_SETTINGS = (
    "status.round_trip_limit_seconds=1\nstatus.render_limit_seconds=1\n"
    + "status.reading_delay_seconds=1\nstatus.accounts.age_limit_seconds=45\n"
)


def status_row(label: str) -> accounts.StatusRow:
    return accounts.StatusRow(
        label=label, tool="claude", folder=f"~/.{label}", login="fixture@example.invalid",
        roles=("units",), role_counts=(accounts.RoleSessionCounts(1, 0, 0, 0),), weekly_left_percent=60,
        resets_at=AT + timedelta(days=1), credits=0, on_credits="no", use_credits=False, read_at=AT,
        read_outcome=agent_accounts.UsageReadOutcome.OK, attempted_at=AT)


def sort_reading() -> accounts.StatusReading:
    """Include shared accounts, tied values, zero values, and missing values."""
    return accounts.StatusReading((
        replace(status_row("claude2"), roles=("producers", "showrunners"), weekly_left_percent=10,
                role_counts=(accounts.RoleSessionCounts(2, 2, 0, 1), accounts.RoleSessionCounts(2, 2, 1, 10)),
                resets_at=AT + timedelta(days=9), credits=1200.2, selected_roles=("showrunners",)),
        replace(status_row("claude1"), roles=("workers", "units"), weekly_left_percent=10,
                role_counts=(accounts.RoleSessionCounts(1, 10, 0, 5), accounts.RoleSessionCounts(2, 1, 12, 0)),
                resets_at=AT + timedelta(days=9, hours=1), credits=1200.1, selected_roles=("units",)),
        replace(status_row("claude3"), roles=(), role_counts=(), weekly_left_percent=accounts.StatusUnavailable("fixture"),
                resets_at=accounts.StatusUnavailable(), credits=accounts.StatusUnavailable()),
        replace(status_row("codex1"), tool="codex", roles=("producers",), weekly_left_percent=0,
                role_counts=(accounts.RoleSessionCounts(0, 1, 0, 0),)),
        replace(status_row("codex2"), tool="codex", roles=("spare",), role_counts=(accounts.NoRoleCount(),),
                weekly_left_percent=accounts.StatusUnavailable("fixture"), resets_at=AT + timedelta(days=2),
                credits="unlimited", use_credits=True)), (), (), timezone.utc, AT,
        first_next_moves=accounts.FirstNextMoveTargets({"units": "claude3", "showrunners": "claude1"}))


def rendered_sort_orders(markup: str, table: str) -> dict[str, list[int]]:
    encoded = re.search(rf'data-sort-table="{re.escape(table)}" [^>]*data-sort-orders="([^"]+)"', markup)
    assert encoded is not None
    return cast(dict[str, list[int]], json.loads(html.unescape(encoded[1])))


def use_fixture_history(case: unittest.TestCase, scratch: Path) -> None:
    _ = case.enterContext(patch.object(run_out, "READINGS_LOG", scratch / "readings.jsonl"))


class ProducerRoleColorTests(unittest.TestCase):
    def test_the_producer_role_uses_its_color_and_account_names_keep_account_colors(self) -> None:
        configuration = Path(self.enterContext(tempfile.TemporaryDirectory()))
        use_fixture_history(self, configuration)
        _ = write_config_fixture(configuration / "accounts.conf", ACCOUNT_SETTINGS)
        _ = write_config_fixture(configuration / "statusline.conf", "role.producer=pink\n")
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"CONF_DIR": str(configuration)})))
        reading = accounts.StatusReading((replace(status_row("plain-account"), roles=("producers",)),), (), (),
                                         timezone.utc, AT)
        body = accounts_page.board(reading)
        self.assertIn('data-color-text="role.producer"', body)
        self.assertIn('>producers</span>', body)
        self.assertNotRegex(body, r'data-color-text="role\.[^"]+"[^>]*>plain-account</span>')


MENU_SETUP = '''<script>
let reads=0;const saves=[];const realFetch=fetch;let freshPage=FRESH_PAGE;
window.fetch=(path,options)=>options?.method==='POST' ? new Promise(resolve=>saves.push({options,resolve}))
 : (reads++,Promise.resolve({ok:true,text:async()=>freshPage}));
</script>'''

MENU_PROBE = '''<script>
(async()=>{
 const failures=[], check=(ok,label)=>{if(!ok) failures.push(label);};
 const flush=()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(()=>setTimeout(resolve,0))));
 const state=()=>document.querySelector('[data-page-status]').dataset.state;
 const menu=document.querySelector('[data-move-menu]');
 const items=()=>[...menu.querySelectorAll('[data-move-target]')];
 const button=()=>document.querySelector('[data-next-button]');
 const open=()=>button().click();
 const activate=name=>button().dispatchEvent(new KeyboardEvent('keydown',{key:name,bubbles:true,cancelable:true}));
 const key=name=>menu.dispatchEvent(new KeyboardEvent('keydown',{key:name,bubbles:true,cancelable:true}));
 const groupRow=()=>button().closest('tr');
 try{
  if(document.readyState!=='complete') await new Promise(resolve=>addEventListener('load',resolve,{once:true}));
  frameElement.style.width='800px';frameElement.style.height='480px';await flush();
  check(document.querySelectorAll('[data-move-group]').length===1 && button().dataset.moveAccount==='claude1'
   && button().dataset.moveGroup==='units','only the selected Claude group line takes the menu');
  check(document.querySelectorAll('[data-next-button]').length===1,'only the selected Claude line has a button');
  const keepsBrowserMenu=cell=>cell.dispatchEvent(new MouseEvent('contextmenu',{bubbles:true,cancelable:true})) && menu.hidden;
  const unselected=document.querySelector('[data-color-key="account.claude2"]').closest('tr');
  check(keepsBrowserMenu(groupRow().cells[3]),'the selected row keeps the browser menu');
  check(keepsBrowserMenu(unselected.cells[1]),'the unselected row keeps the browser menu');
  open();
  check(!menu.hidden && button().getAttribute('aria-expanded')==='true','a click opens the menu');
  check(menu.querySelector('[data-move-title]').textContent==='When claude1 reaches 1%, move units to',
   'the title names the account, percent and group');
  check(JSON.stringify(items().map(item=>item.dataset.moveTarget))==='["claude2","claude3"]','the menu lists the other Claude accounts');
  check(items()[0].textContent==='claude260% left' && items()[1].textContent==='claude3',
   'each item shows the weekly percent left when known');
  check(items()[0].querySelector('.move-name').style.color==='rgb(178, 148, 187)','each item uses the account color');
  check(items()[0].querySelector('.move-left').className==='move-left','a healthy percent keeps the plain tone');
  check(document.activeElement===items()[0],'the first item takes the focus');
  key('ArrowDown');check(document.activeElement===items()[1],'ArrowDown moves to the next item');
  key('ArrowDown');check(document.activeElement===menu.querySelector('[data-move-clear]'),'ArrowDown reaches No next move');
  key('ArrowDown');check(document.activeElement===items()[0],'ArrowDown wraps to the first item');
  key('ArrowUp');check(document.activeElement===menu.querySelector('[data-move-clear]'),'ArrowUp wraps to the last item');
  key('Home');check(document.activeElement===items()[0],'Home moves to the first item');
  key('End');check(document.activeElement===menu.querySelector('[data-move-clear]'),'End moves to the last item');
  const box=menu.getBoundingClientRect();
  check(box.left>=0 && box.right<=innerWidth && box.top>=0 && box.bottom<=innerHeight,'the menu fits the viewport');
  check(items()[0].getAttribute('role')==='menuitemradio' && items()[0].getAttribute('aria-checked')==='true'
   && items()[1].getAttribute('aria-checked')==='false','the current target is marked');
  key('Escape');
  button().style.cssText='position:fixed;right:0;bottom:0';open();
  const edge=menu.getBoundingClientRect();
  check(edge.left>=8 && edge.top>=8 && edge.right<=innerWidth-7.5 && edge.bottom<=innerHeight-7.5,
   'a button at the corner keeps the menu inside the viewport:'+JSON.stringify([edge.left,edge.top,edge.right,edge.bottom,innerWidth,innerHeight]));
  check(state()==='Paused','an open menu pauses refresh');
  key('Escape');check(menu.hidden && state()!=='Paused' && document.activeElement===button()
   && button().getAttribute('aria-expanded')==='false','Escape closes the menu, returns focus and resumes refresh');
  button().style.cssText='';activate('Enter');check(!menu.hidden,'Enter opens the menu');
  key('Escape');activate(' ');check(!menu.hidden,'Space opens the menu');
  key('Escape');
  open();
  document.body.dispatchEvent(new PointerEvent('pointerdown',{bubbles:true}));
  check(menu.hidden && document.activeElement===button(),'a click outside closes the menu and returns focus');
  open();
  const readsBefore=reads;
  items()[1].click();
  check(menu.hidden && saves.length===1,'a choice closes the menu and posts once');
  const form=new URLSearchParams(saves[0].options.body);
  check(form.get('group')==='units' && form.get('to')==='claude3' && [...form.keys()].length===2 &&
   saves[0].options.headers['Content-Type']==='application/x-www-form-urlencoded','the post names the group and the account');
  check(state()==='Paused','a pending move holds refresh');
  saves[0].resolve({ok:true,json:async()=>({state:'saved'})});await flush();await flush();
  check(reads===readsBefore+1 && state()!=='Paused','a saved move reloads the board');
  open();items()[0].click();
  saves[1].resolve({ok:false,json:async()=>({reason:'next: cannot set entry: busy'})});await flush();
  const error=document.querySelector('[data-color-save-error]');
  check(!error.hidden && error.textContent==='units: next: cannot set entry: busy','a failed move reports its reason in the status line');
  open();menu.querySelector('[data-move-clear]').click();
  const cleared=new URLSearchParams(saves[2].options.body);
  check(cleared.get('group')==='units' && cleared.get('clear')==='first' && [...cleared.keys()].length===2,
   'No next move posts only the group and clear=first');
  check(menu.hidden && document.activeElement===button(),'clear returns focus to the button');
  freshPage=EMPTY_PAGE;
  saves[2].resolve({ok:true,json:async()=>({state:'saved'})});await flush();
  check(button().closest('td').querySelector('.next-muted').textContent==='none','clear refreshes the cell to none');
  open();check(!menu.querySelector('[data-move-clear]'),'an empty group has no clear item');key('Escape');
  open();dispatchEvent(new Event('resize'));check(menu.hidden,'resize closes the menu');
  open();dispatchEvent(new Event('scroll'));check(menu.hidden,'scroll closes the menu');

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
    review_root: Path = Path()

    @override
    def setUp(self) -> None:
        scratch = Path(self.enterContext(tempfile.TemporaryDirectory()))
        use_fixture_history(self, scratch)
        configuration = scratch / "config"
        self.review_root = scratch / "review"
        _ = self.enterContext(patch.object(usage_store, "store_file", return_value=scratch / "home" / "usage.json"))
        _ = copy_config_fixture(Path(__file__).resolve().parents[2] / "config", configuration)
        _ = write_config_fixture(configuration / "accounts.conf", ACCOUNT_SETTINGS)
        _ = write_config_fixture(configuration / "console.conf", PAGE_STATUS_SETTINGS)
        _ = write_config_fixture(configuration / "review_links.conf", "page_refresh_seconds=15\naccounts_page_seconds=30\n")
        _ = (configuration / "statusline.conf").write_text(
            "account.claude1=#af875f\naccount.claude2=#b294bb\naccount.codex2=#123456\nrole.unit-director=#d7af00\n"
            + "model.claude-opus-5-5=#e08a5c\n",
            encoding="utf-8")
        _ = cast(object, self.enterContext(patch.dict(os.environ, {
            "CONF_DIR": str(configuration), "REVIEW_LINKS_ROOT": str(self.review_root),
        })))

    def test_page_reads_quota_clear_state_from_the_fixture_root(self) -> None:
        reading = accounts.StatusReading((replace(status_row("claude1"), weekly_left_percent=4),),
                                         (), (), timezone.utc, AT)
        with (patch.object(accounts, "read_status", return_value=reading),
              patch.object(accounts_page, "cleared_quotas", return_value={}) as cleared):
            _ = accounts_page.page()
        cleared.assert_called_once_with(accounts_page.quota_state(self.review_root))

    def test_next_accounts_response_shows_nav_quota_from_the_same_store(self) -> None:
        home = self.review_root.parent / "home"
        home.mkdir()
        (home / ".claude").mkdir()
        _ = (home / ".claude.json").write_text("{}")
        configuration = Path(os.environ["CONF_DIR"])
        write_machine_config(configuration)
        cache = serve.AccountsPageCache()
        login = "warning-fixture@example.invalid"
        marker = b'data-quota-level="warning" data-quota-account="claude4"'
        resets_at = datetime.now(timezone.utc) + timedelta(days=1)

        def render_fixture() -> accounts_page.AccountsRender:
            return accounts_page.AccountsRender(accounts_page.page(), .5, time.time())

        with (scratch_account_home(home, {"REVIEW_LINKS_ROOT": str(self.review_root),
                  "AGENT_NOTES_DIR": str(home / "notes"), "USER_ZONE": "UTC"}),
              patch.dict(os.environ, {"CONF_DIR": str(configuration)}),
              patch.object(role_map, "is_role_map_host", return_value=True),
              patch.object(role_map, "ssh_role_map", side_effect=AssertionError("unexpected SSH")),
              patch.object(accounts, "_read_local_role_sessions",
                           return_value=accounts.LocalRoleSessions({}, {}, (), (), (), False, frozenset())),
              patch.object(accounts, "_start_session_counts_fetch", return_value=False),
              patch.object(subprocess, "Popen", side_effect=AssertionError("unexpected process")),
              patch.object(time, "monotonic", return_value=100),
              patch.object(accounts_page, "render_with_facts", side_effect=render_fixture)):
            create_claude_account("claude4", login)
            self.assertTrue(usage_store.store_file().is_relative_to(home))
            stored_room(login, 11, resets_at)
            self.assertFalse(marker in cache.get())
            self.assertEqual(accounts_page.nav_quota(self.review_root)["level"], accounts_page.QuotaLevel.NONE)

            stored_room(login, 10, resets_at)
            self.assertEqual(accounts_page.nav_quota(self.review_root),
                             {"level": accounts_page.QuotaLevel.WARNING, "name": "Warning: 10% left"})
            reply = cache.get()
            with cache.condition:
                self.assertTrue(cache.condition.wait_for(lambda: not cache.rendering, TEST_HANG_GUARD_SECS))
            self.assertTrue(marker in reply)

    def test_quota_clear_survives_reset_time_drift_until_the_next_week(self) -> None:
        reset = AT + timedelta(days=1, microseconds=299911)
        path = accounts_page.quota_state(self.review_root)
        path.parent.mkdir(parents=True)
        _ = path.write_text(json.dumps({"claude1": {"level": "urgent", "reset": reset.isoformat()}}), encoding="utf-8")
        cases = ((reset - timedelta(microseconds=526917), 4, False),
                 (reset + timedelta(seconds=3599), 4, False),
                 (reset + timedelta(hours=1), 4, True),
                 (reset + timedelta(days=7), 100, False),
                 (reset + timedelta(days=7), 4, True))
        for observed_reset, left, shows_icon in cases:
            with self.subTest(reset=observed_reset, left=left):
                row = replace(status_row("claude1"), resets_at=observed_reset, weekly_left_percent=left)
                reading = accounts.StatusReading((row,), (), (), timezone.utc, AT)
                with patch.object(accounts, "read_status", return_value=reading):
                    content = accounts_page.page().decode()
                self.assertEqual('data-quota-account="claude1"' in content, shows_icon)

    def test_page_keeps_account_values_when_quota_settings_or_state_are_invalid(self) -> None:
        reading = accounts.StatusReading((replace(status_row("claude1"), weekly_left_percent=4),),
                                         (), (), timezone.utc, AT)
        path = accounts_page.quota_state(self.review_root)
        path.parent.mkdir(parents=True)
        threshold_error = "accounts_quota_warning_percent must be greater than accounts_quota_urgent_percent."
        cases = ((10, 5, "{", f"Invalid quota clear state in {path}."),
                 (5, 5, "{}", threshold_error),
                 (5, 10, "{}", threshold_error))
        with patch.object(accounts, "read_status", return_value=reading):
            for warning, urgent, state, error in cases:
                with self.subTest(warning=warning, urgent=urgent, state=state):
                    _ = path.write_text(state, encoding="utf-8")
                    _ = write_config_fixture(conf.path("review_links"),
                        f"accounts_quota_warning_percent={warning}\naccounts_quota_urgent_percent={urgent}\n")
                    content = accounts_page.page().decode()
                    self.assertIn(">4%</span>", content)
                    self.assertEqual(content.count(f"<li>{html.escape(error)}</li>"), 1)
                    self.assertNotIn('data-quota-account="claude1"', content)

    def test_weekly_quota_icons_follow_the_config_and_skip_missing_readings(self) -> None:
        cases = ((11, ""), (10, "Warning: 10% left"), (6, "Warning: 6% left"),
                 (5, "Urgent: 5% left"), (0, "Urgent: 0% left"), (accounts.StatusUnavailable(), ""))
        quota_rows = tuple(replace(status_row(label), tool="codex" if label.startswith("codex") else "claude",
                                   weekly_left_percent=left)
                           for label, (left, _) in zip(("claude1", "claude2", "codex1", "claude3", "codex2", "claude4"),
                                                         cases, strict=True))
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        reading = accounts.StatusReading(quota_rows, (), (), timezone.utc, AT)
        with patch.object(accounts, "read_status", return_value=reading), patch.dict(
                os.environ, {"REVIEW_LINKS_ROOT": str(root)}):
            content = accounts_page.page().decode()
            for row, (_, name) in zip(quota_rows, cases, strict=True):
                with self.subTest(account=row.label):
                    button = re.search(rf'<button[^>]*data-quota-account="{row.label}"[^>]*>', content)
                    if name:
                        assert button is not None
                        self.assertIn(f'aria-label="{name}"', button[0])
                    else:
                        self.assertIsNone(button)
            _ = write_config_fixture(conf.path("review_links"),
                                     "accounts_quota_warning_percent=12\naccounts_quota_urgent_percent=6\n")
            changed = accounts_page.page().decode()
            self.assertIn('aria-label="Warning: 11% left"', changed)
            self.assertIn('aria-label="Urgent: 6% left"', changed)

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required for browser behavior")
    def test_quota_panel_closes_and_clear_preserves_the_percentage_color(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        reading = accounts.StatusReading((replace(status_row("claude1"), weekly_left_percent=4),),
                                         (), (), timezone.utc, AT)
        with patch.object(accounts, "read_status", return_value=reading), patch.dict(
                os.environ, {"REVIEW_LINKS_ROOT": str(root)}):
            source = accounts_page.page().decode()
        setup = """<script>
const realFetch=window.fetch.bind(window);let clearForm;
window.fetch=(url,options)=>options?.method==='POST'
 ? (clearForm=new URLSearchParams(options.body),Promise.resolve({ok:true,json:async()=>({state:'saved'})}))
 : realFetch(url,options);
</script>"""
        probe = r"""<script>
(async()=>{
 const failures=[],check=(ok,label)=>{if(!ok) failures.push(label);};
 const paint=()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
 try{
  const button=document.querySelector('[data-quota-account=claude1]');
  const panel=document.getElementById('quota-panel'),clear=panel.querySelector('button');
  const percent=button.nextElementSibling,color=getComputedStyle(percent).color;
  button.click();
  check(!panel.hidden && document.activeElement===clear,'the panel opens and focuses Clear');
  check(panel.textContent.includes('claude1 has 4% left') && panel.textContent.includes('Urgent'),
   'the panel names the account, percentage and level');
  clear.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true,cancelable:true}));
  check(panel.hidden && document.activeElement===button,'Escape closes the panel and restores focus');
  button.click();document.body.dispatchEvent(new PointerEvent('pointerdown',{bubbles:true}));
  check(panel.hidden,'an outside click closes the panel');
  button.click();clear.click();await paint();
  check(panel.hidden && !button.isConnected,'Clear closes the panel and removes the button');
  check(clearForm.get('quota_account')==='claude1' && clearForm.get('quota_level')==='urgent',
   'Clear posts the account and level');
  check(percent.textContent==='4%' && getComputedStyle(percent).color===color,'the percentage keeps its color');
 }catch(error){failures.push(String(error));}
 await realFetch('/results',{method:'POST',body:JSON.stringify(failures)});
})();</script>"""
        page = source.replace("</head>", setup + "</head>").replace("</body>", probe + "</body>")
        self.assertEqual(firefox_probe_results(page.encode()), [[]])

    def test_board_draws_the_table_and_problems_without_the_upcoming_list(self) -> None:
        config = Path(self.enterContext(tempfile.TemporaryDirectory(prefix='accounts-board-config-')))
        _ = write_config_fixture(config / "accounts.conf", ACCOUNT_SETTINGS)
        _ = (config / 'statusline.conf').write_text('account.claude1=brown\nrole.unit-director=yellow\nrole.worker=#c0c0c0\n')
        _ = cast(object, self.enterContext(patch.dict(os.environ, {'CONF_DIR': str(config)})))
        reading = accounts.StatusReading((status_row("claude1"),), ("* <stale>",), (), timezone.utc, AT,
                                         upcoming_moves=("Upcoming moves:", "units → claude1"))
        markup = accounts_page.board(reading)
        account_table, roles_table = markup.split('<h2>Roles</h2>')
        self.assertIn('<h2>Accounts</h2>', account_table)
        self.assertIn('aria-label="Accounts"', account_table)
        self.assertIn('aria-label="Roles"', roles_table)
        self.assertIn('data-account-sort="account" tabindex="0" aria-sort="ascending"', account_table)
        self.assertIn('data-role-sort="role" tabindex="0" aria-sort="ascending"', roles_table)
        self.assertIn('style="color:#af875f" data-color-text="account.claude1">claude1</span>', account_table)
        self.assertIn('data-color-key="account.claude1"', account_table)
        self.assertIn('<td class="right"><span class="default">60%</span></td>', account_table)
        self.assertIn('data-color-text="role.unit-director">units</span>', roles_table)
        self.assertIn('data-color-key="role.unit-director"', roles_table)
        self.assertIn('title="Not the selected account for a role"', roles_table)
        self.assertIn(page_frame.icon_svg("minus"), roles_table)
        self.assertIn('<tr data-summary><td class="left"><span class="default bold">total</span>', roles_table)
        self.assertIn('<h2>Problems</h2><ul class="problems"><li>&lt;stale&gt;</li></ul>', account_table)
        self.assertNotIn('Upcoming moves:', markup)
        self.assertNotIn('units → claude1', markup)

    def test_account_and_role_sort_orders_use_full_values_and_keep_missing_values_last(self) -> None:
        reading = sort_reading()
        reading = replace(reading, rows=tuple(
            replace(row, weekly_left_percent=10.1 if row.label == "claude2" else 10.2)
            if row.label in ("claude1", "claude2") else row for row in reading.rows))
        markup = accounts_page.board(reading)
        accounts_order = rendered_sort_orders(markup, "accounts")
        roles_order = rendered_sort_orders(markup, "roles")
        for key, order in {
            "account": [0, 1, 2, 3, 4], "account-reverse": [4, 3, 2, 1, 0],
            "left": [3, 1, 0, 2, 4], "left-reverse": [0, 1, 3, 2, 4],
            "credits": [4, 1, 0, 3, 2], "credits-reverse": [3, 0, 1, 4, 2],
            "resets": [3, 4, 1, 0, 2], "resets-reverse": [0, 1, 4, 3, 2],
            "enabled": [4, 0, 1, 2, 3], "enabled-reverse": [4, 0, 1, 2, 3],
        }.items():
            with self.subTest(key=key):
                self.assertEqual(accounts_order[key], order)
        for key, order in {
            "role": [0, 1, 2, 3, 4], "role-reverse": [4, 3, 2, 1, 0],
            "next": [1, 3, 0, 2, 4], "next-reverse": [3, 1, 0, 2, 4],
            "busy": [0, 1, 3, 4, 2], "busy-reverse": [4, 0, 1, 3, 2],
            "idle": [4, 0, 1, 3, 2], "idle-reverse": [3, 1, 0, 4, 2],
        }.items():
            with self.subTest(key=key):
                self.assertEqual(roles_order[key], order)

    def test_account_roles_list_only_selected_roles_in_role_order(self) -> None:
        rows = (
            replace(status_row("claude1"), roles=("units", "producers"), selected_roles=("units", "producers")),
            status_row("claude2"),
            replace(status_row("claude3"), roles=("showrunners", "producers"), selected_roles=("showrunners",)),
        )
        markup = accounts_page.board(accounts.StatusReading(rows, (), (), timezone.utc, AT))
        cells = [(match[1] or "", match[2]) for match in
                 re.finditer(r'<td class="left account-roles"(?: title="([^"]*)")?>(.*?)</td>', markup)]
        self.assertEqual([(title, re.sub(r"<[^>]+>", "", content)) for title, content in cells],
                         [("producers, units", "producers, units"), ("", ""), ("showrunners", "showrunners")])
        self.assertIn('style="color:#d7af00" data-color-text="role.unit-director">units</span>', cells[0][1])
        orders = rendered_sort_orders(markup, "accounts")
        self.assertEqual(orders["roles"], [0, 2, 1])
        self.assertEqual(orders["roles-reverse"], [2, 0, 1])

    def test_account_roles_use_the_first_selected_account_from_the_roles_table(self) -> None:
        rows = (
            status_row("claude1"),
            replace(status_row("claude2"), selected_roles=("units",)),
            replace(status_row("claude3"), selected_roles=("units",)),
        )
        markup = accounts_page.board(accounts.StatusReading(rows, (), (), timezone.utc, AT))
        titles = [match[1] or "" for match in
                  re.finditer(r'<td class="left account-roles"(?: title="([^"]*)")?>', markup)]
        self.assertEqual(titles, ["", "units", ""])
        roles_table = markup.split('<h2>Roles</h2>', 1)[1]
        first_account = re.search(r'<div class="role-line"><span[^>]*>([^<]+)</span>', roles_table)
        self.assertIsNotNone(first_account)
        assert first_account is not None
        self.assertEqual(first_account[1], "claude2")

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required for browser behavior")
    def test_account_roles_clip_long_lists_and_keep_the_full_title(self) -> None:
        long_roles = ("producers", "regional release coordinators", "units", "workers")
        rows = (
            replace(status_row("claude1"), roles=("showrunners",), selected_roles=("showrunners",)),
            replace(status_row("claude2"), roles=long_roles, selected_roles=long_roles),
        )
        reading = accounts.StatusReading(rows, (), (), timezone.utc, AT)
        with patch.object(accounts, "read_status", return_value=reading):
            rendered = accounts_page.page().decode()
        probe = '''<script>
(async()=>{
 const failures=[], check=(ok,label)=>{if(!ok) failures.push(label);};
 try{
  frameElement.style.width='1280px';
  const [short,long]=document.querySelectorAll('.accounts-table .account-roles');
  const style=getComputedStyle(long);
  check(short.textContent==='showrunners' && short.scrollWidth<=short.clientWidth,
   'the short role list fits');
  check(style.whiteSpace==='nowrap' && long.clientHeight===short.clientHeight,
   'the long role list stays on one line');
  check(style.overflowX==='hidden' && style.textOverflow==='ellipsis' && long.scrollWidth>long.clientWidth,
   'the long role list has an ellipsis');
  check(long.title==='producers, regional release coordinators, units, workers' && long.title===long.textContent,
   'the title shows the full role list');
 }catch(error){failures.push(String(error));}
 await fetch('/results',{method:'POST',body:JSON.stringify(failures)});
})();</script>'''
        page = rendered.replace("</body>", probe + "</body>")
        self.assertEqual(firefox_probe_results(page.encode()), [[]])

    def test_each_table_has_its_own_default_and_focusable_sort_headers(self) -> None:
        markup = accounts_page.board(sort_reading())
        self.assertEqual(re.findall(r'data-account-sort="([^"]+)"', markup),
                         ["account", "left", "roles", "used / hour", "empty at", "resets", "credits", "enabled"])
        self.assertEqual(re.findall(r'data-role-sort="([^"]+)"', markup),
                         ["role", "account", "next", "busy", "idle", "paused", "exited", "total"])
        self.assertEqual(markup.count('tabindex="0"'), 16)
        self.assertEqual(markup.count('class="sort-arrow"'), 16)
        self.assertIn('data-account-sort="account" tabindex="0" aria-sort="ascending"', markup)
        self.assertIn('data-role-sort="role" tabindex="0" aria-sort="ascending"', markup)

    def test_next_sort_uses_the_displayed_values_in_both_directions(self) -> None:
        rows = (
            replace(status_row("claude1"), roles=("showrunners",), selected_roles=("showrunners",)),
            replace(status_row("claude2"), selected_roles=("units",)),
            replace(status_row("claude3"), roles=("spare",)),
            replace(status_row("codex1"), tool="codex", roles=("workers",), selected_roles=("workers",)),
        )
        reading = accounts.StatusReading(rows, (), (), timezone.utc, AT,
                                         first_next_moves=accounts.FirstNextMoveTargets({"units": "claude3"}))
        orders = rendered_sort_orders(accounts_page.board(reading), "roles")

        self.assertEqual(orders["next"], [3, 2, 0, 1])
        self.assertEqual(orders["next-reverse"], [0, 2, 3, 1])

    def test_weekly_forecast_columns_use_recent_history_for_both_tools(self) -> None:
        def history(used: float, rate: float, age_minutes: int = 0) -> list[run_out.Reading]:
            return [run_out.Reading(AT - timedelta(minutes=age_minutes + 10 * step), used - rate * step / 6)
                    for step in range(12, -1, -1)]

        rows = (
            status_row("claude1"),
            replace(status_row("claude2"), resets_at=AT + timedelta(hours=3)),
            status_row("claude3"),
            status_row("claude4"),
            replace(status_row("codex1"), tool="codex"),
            replace(status_row("codex2"), tool="codex", resets_at=accounts.StatusUnavailable()),
        )
        histories = {"claude1": history(40, 10), "claude2": history(40, 10),
                     "claude4": history(40, 10, 16),
                     "codex1": history(40, 15), "codex2": history(40, 5)}
        reading = accounts.StatusReading(rows, (), (), ZoneInfo("America/New_York"), AT)
        with (patch.object(run_out, "read_readings", return_value=histories) as read,
              patch.object(accounts, "read_status", return_value=reading)):
            markup = accounts_page.page().decode()
        read.assert_called_once_with(run_out.READINGS_LOG)

        table = markup.split('aria-label="Accounts"', 1)[1].split("</table>", 1)[0]
        lines = table.split("<tbody>", 1)[1].split("</tr>")[:-1]
        expected = (("10.0%", "Sun 11-15 16:00 EST"), ("10.0%", "—"), ("—", "—"), ("—", "—"),
                    ("15.0%", "Sun 11-15 14:00 EST"), ("5.0%", "—"))
        for row, line, (rate, empty) in zip(rows, lines, expected, strict=True):
            with self.subTest(account=row.label):
                cells = cast(list[str], re.findall(r'<td[^>]*>(.*?)</td>', line, re.S))
                self.assertIn(f">{row.label}</span>", cells[1])
                self.assertEqual(re.sub(r"<[^>]+>", "", cells[5]), rate)
                self.assertEqual(re.sub(r"<[^>]+>", "", cells[6]), empty)
                if row.label == "claude2":
                    self.assertIn('title="Quota lasts until the weekly reset."', cells[6])
                if rate == "—":
                    for cell in cells[5:7]:
                        self.assertIn('title="No usage rate is available."', cell)
        order = rendered_sort_orders(markup, "accounts")
        self.assertEqual(order["used / hour"], [5, 0, 1, 4, 2, 3])
        self.assertEqual(order["empty at"], [4, 0, 1, 2, 3, 5])

    def test_weekly_forecast_excludes_history_before_a_scheduled_reset_or_refill(self) -> None:
        rows = (
            replace(status_row("claude1"), weekly_left_percent=98,
                    resets_at=AT + timedelta(days=7, minutes=-1)),
            replace(status_row("claude2"), weekly_left_percent=98),
        )
        history = [run_out.Reading(AT - timedelta(minutes=2 + 10 * step), 99 - step)
                   for step in range(12, -1, -1)]
        reading = accounts.StatusReading(rows, (), (), timezone.utc, AT)
        with patch.object(run_out, "read_readings", return_value={row.label: history for row in rows}):
            markup = accounts_page.board(reading)

        table = markup.split('aria-label="Accounts"', 1)[1].split("</table>", 1)[0]
        self.assertNotIn("<time", table)
        self.assertEqual(table.count('title="No usage rate is available."'), 4)

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required for browser behavior")
    def test_sort_controls_reverse_and_persist_through_refresh(self) -> None:
        with patch.object(accounts, "read_status", return_value=sort_reading()):
            content = accounts_page.page()
        rendered = accounts_page.AccountsRender(content, .1, 200).with_recent_times((.1,)).decode()
        setup = '''<script>
localStorage.removeItem('accounts-sort');Date.now=()=>200000;
const realFetch=fetch;let reads=0;
window.fetch=()=>{reads++;return Promise.resolve({ok:true,text:async()=>FRESH_PAGE});};
</script>'''.replace("FRESH_PAGE", json.dumps(rendered).replace("</", "<\\/"))
        probe = '''<script>
(async()=>{
 const failures=[],check=(ok,label)=>{if(!ok) failures.push(label);};
 const flush=()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
 const table=name=>document.querySelector('[data-sort-table="'+name+'"] table');
 const header=(name,key)=>[...table(name).querySelectorAll('th[aria-sort]')]
  .find(node=>node.querySelector('.sort-label').textContent.replace(/[▲▼]/g,'')===key);
 const rows=name=>[...table(name).tBodies[0].rows];
 const labels=(name,index)=>rows(name).filter(row=>!row.hasAttribute('data-summary'))
  .map(row=>row.cells[index].textContent).join(',');
 const direction=node=>node.getAttribute('aria-sort');
 const activate=(node,key)=>node.dispatchEvent(new KeyboardEvent('keydown',{key,bubbles:true,cancelable:true}));
 try{
  frameElement.style.width='1280px';frameElement.style.height='520px';await flush();
  const cards=['accounts','roles','models'];
  for(const name of cards){
   const scroll=table(name).parentElement;
   check(scroll.scrollWidth<=scroll.clientWidth+1,name+' fits at 1280px');
   const headings=[...table(name).querySelectorAll('th[aria-sort]')];
   check(headings.every(node=>node.tabIndex===0 && getComputedStyle(node).cursor==='pointer'),
    name+' headers support focus and display a pointer');
   check(headings.filter(node=>direction(node)!=='none').length===1,name+' has one active sort');
  }
  const defaultAccounts='claude1,claude2,claude3,codex1,codex2';
  check(labels('accounts',1)===defaultAccounts,'the default places one row per account');
  const account=header('accounts','account');
  account.click();
  check(direction(account)==='descending' && labels('accounts',1)==='codex2,codex1,claude3,claude2,claude1',
   'the account header reverses the label order');
  account.click();
  check(labels('accounts',1)===defaultAccounts,'the account header restores the label order');
  const left=header('accounts','left');
  left.focus();activate(left,'Enter');
  check(direction(left)==='ascending' && labels('accounts',1)==='codex1,claude1,claude2,claude3,codex2',
   'Enter sorts by remaining quota and keeps missing readings last');
  check(!activate(left,' '),'Space prevents scrolling while sorting');
  const sortedAccounts='claude1,claude2,codex1,claude3,codex2';
  check(direction(left)==='descending' && labels('accounts',1)===sortedAccounts,'Space reverses the sort and keeps missing quota last');
  const busy=header('roles','busy'), total=rows('roles').at(-1);
  busy.click();
  check(direction(busy)==='descending' && busy.querySelector('.sort-arrow').textContent==='▼',
   'busy starts with a descending arrow');
  busy.click();
  check(direction(busy)==='ascending' && rows('roles').at(-1)===total,'the roles summary remains last');
  const saved=name=>JSON.parse(localStorage.getItem(name+'-sort'));
  check(saved('accounts').column==='left' && saved('roles').column==='busy','the tables store independent choices');
  const oldTable=table('accounts');
  document.dispatchEvent(new Event('visibilitychange'));await flush();await flush();
  check(reads>0 && table('accounts')!==oldTable,'refresh replaces the tables');
  check(labels('accounts',1)===sortedAccounts && direction(header('roles','busy'))==='ascending',
   'refresh preserves the sort in both tables');
  for(const name of ['accounts','roles']){
   for(const node of table(name).querySelectorAll('th[aria-sort]')){
    node.click();
    const first=direction(node);
    node.click();
    check(first!=='none' && first!==direction(node),name+' header reverses');
    const tables=[...table(name).querySelectorAll('th[aria-sort]')];
    check(tables.filter(other=>direction(other)!=='none').length===1,name+' keeps one active sort');
   }
  }
 }catch(error){failures.push(String(error));}
 await realFetch('/results',{method:'POST',body:JSON.stringify(failures)});
})();</script>'''
        page = rendered.replace("<script>", setup + "<script>", 1).replace("</body>", probe + "</body>")
        self.assertEqual(firefox_probe_results(page.encode()), [[]])

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required for browser behavior")
    def test_model_counts_show_session_names_on_hover_and_focus(self) -> None:
        model = accounts.ObservedSessionModel("claude-opus-5-5", "low")
        high = accounts.ObservedSessionModel(model.model, "high")
        earlier = accounts.ObservedSessionModel("claude-fable-5-1", "low")
        names = ['alpha & "beta"', "hana-unit (mac)", "zeta"]
        fixture = replace(status_row("claude1"), roles=("units", "workers"),
                          role_counts=(accounts.RoleSessionCounts(4, 0, 0, 0), accounts.UnknownRoleCounts()),
                          models_by_role={"units": Counter({model: 3, high: 1})},
                          model_names_by_role={"units": {model: accounts.ModelSessionNames(names[::-1]),
                                                         high: accounts.ModelSessionNames(["high-unit"])}})
        reading = accounts.StatusReading((fixture,), (), (), timezone.utc, AT)
        with patch.object(accounts, "read_status", return_value=reading):
            page = accounts_page.page().decode()
        refreshed = replace(fixture,
                            role_counts=(accounts.RoleSessionCounts(6, 0, 0, 0), accounts.UnknownRoleCounts()),
                            models_by_role={"units": Counter({earlier: 1, model: 3, high: 2})},
                            model_names_by_role={"units": {**fixture.model_names_by_role["units"],
                                earlier: accounts.ModelSessionNames(["fable-unit"]),
                                high: accounts.ModelSessionNames(["high-unit", "new-unit"])}})
        with patch.object(accounts, "read_status", return_value=replace(reading, rows=(refreshed,))):
            fresh = accounts_page.AccountsRender(accounts_page.page(), .1, 200).with_recent_times((.1,)).decode()
        setup = MENU_SETUP.replace("FRESH_PAGE", json.dumps(fresh).replace("</", "<\\/"))
        probe = r"""<script>
(async()=>{
const failures=[], check=(ok,label)=>{if(!ok) failures.push(label);};
const flush=()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
try{
 frameElement.style.width='800px';frameElement.style.height='520px';await flush();
 const table=document.querySelector('table[aria-label="Models"]');
 const [model,high,total]=table.tBodies[0].rows;
 const count=model.cells[7], tooltip=document.querySelector('.model-tooltip');
 const names=['alpha & "beta"','hana-unit (mac)','zeta'];
 count.scrollIntoView({block:'center'});
 check(count.textContent==='3','the cell contains only the count');
 check(count.tabIndex===0 && !count.hasAttribute('title'),'the tooltip cell supports focus');
 check(names.every(name=>count.getAttribute('aria-label')?.includes(name)),'the accessible name includes every session');
 check(model.cells[3].tabIndex<0 && !model.cells[3].dataset.modelSessions,'zero has no tooltip');
 check(total.cells[7].tabIndex<0 && !total.cells[7].dataset.modelSessions,'unknown has no tooltip');
 count.dispatchEvent(new PointerEvent('pointerover',{bubbles:true}));
 check(!tooltip.hidden && tooltip.textContent===names.join('\n'),'hover shows one line per name in order');
 count.dispatchEvent(new PointerEvent('pointerout',{bubbles:true}));
 check(tooltip.hidden,'leaving the count hides its tooltip');
 count.focus({preventScroll:true});
 check(document.activeElement===count,'keyboard focus reaches the count');
 // The screenshot frame sets the active element without sending a focus event.
 count.dispatchEvent(new FocusEvent('focusin',{bubbles:true}));
 check(!tooltip.hidden && tooltip.textContent===names.join('\n'),'focus shows the same tooltip');
 const tip=tooltip.getBoundingClientRect(), bounds=table.getBoundingClientRect();
 check(tip.left>=Math.max(0,bounds.left) && tip.right<=Math.min(innerWidth,bounds.right)+1 &&
   tip.top>=Math.max(0,bounds.top) && tip.bottom<=Math.min(innerHeight,bounds.bottom)+1,
   'the tooltip stays inside the visible table');
 document.documentElement.dataset.theme='light';
 check(getComputedStyle(tooltip).backgroundColor==='rgb(25, 35, 57)','the light tooltip uses the console color');
 document.documentElement.dataset.theme='dark';
 check(getComputedStyle(tooltip).backgroundColor==='rgb(228, 234, 244)','the dark tooltip uses the console color');
 const other=high.cells[5];
 count.dispatchEvent(new PointerEvent('pointerover',{bubbles:true}));
 other.focus({preventScroll:true});
 other.dispatchEvent(new FocusEvent('focusin',{bubbles:true}));
 check(document.activeElement===other && !tooltip.hidden && tooltip.textContent==='high-unit',
   'focus on a second cell takes priority over the earlier hover');
 document.dispatchEvent(new Event('visibilitychange'));await flush();
 const rows=document.querySelector('table[aria-label="Models"]').tBodies[0].rows;
 const restored=rows[2].cells[5];
 check(!other.isConnected && restored.textContent==='2' && document.activeElement===restored &&
   !tooltip.hidden && tooltip.textContent==='high-unit\nnew-unit',
   'refresh restores focus and the tooltip for the same model, effort and column');
}catch(error){failures.push(String(error));}
realFetch('/probe',{method:'POST',body:JSON.stringify(failures)});
})();
</script>"""
        page = page.replace("<script>", setup + "<script>", 1).replace("</body>", probe + "</body>")
        self.assertEqual(firefox_probe_results(page.encode()), [[]])

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required for browser behavior")
    def test_models_table_sorts_and_previews_and_saves_a_model_color(self) -> None:
        opus = "claude-opus-5-5"
        models = (accounts.ObservedSessionModel(opus, "low"),
                  accounts.ObservedSessionModel("gpt-6.2-sol", "medium"),
                  accounts.ObservedSessionModel(opus, "high"),
                  accounts.ObservedSessionModel("claude-fable-5-1", "max"))
        role_counts = {
            "producers": (5, 0, 2, 4, 1),
            "showrunners": (2, 6, 4, 0, 3),
            "units": (3, 2, 0, 7, 1),
            "workers": (0, 8, 3, 1, 2),
        }
        fixture = replace(status_row("claude1"), roles=tuple(role_counts),
                          role_counts=tuple(accounts.RoleSessionCounts(sum(values), 0, 0, 0)
                                            for values in role_counts.values()),
                          models_by_role={role: Counter({model: count for model, count in zip(models, values) if count})
                                          for role, values in role_counts.items()})
        reading = accounts.StatusReading((fixture,), (), (), timezone.utc, AT)
        with patch.object(accounts, "read_status", return_value=reading):
            content = accounts_page.page()
        rendered = accounts_page.AccountsRender(content, .1, 200).with_recent_times((.1,)).decode()
        setup = MENU_SETUP.replace("FRESH_PAGE", json.dumps(rendered).replace("</", "<\\/"))
        probe = """<script>
(async()=>{
 const failures=[], check=(ok,label)=>{if(!ok) failures.push(label);};
 const flush=()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
 const table=()=>document.querySelector('table[aria-label="Models"]');
 const headings=()=>[...table().tHead.rows[0].cells];
 const header=key=>table().querySelector('[data-model-sort="'+key+'"]');
 const account=key=>document.querySelector('[data-account-sort="'+key+'"]');
 const rows=()=>[...table().tBodies[0].rows];
 const cellText=row=>[...row.cells].map(cell=>cell.textContent);
 const swatch=()=>table().querySelector('[data-color-key="model.claude-opus-5-5"]');
 const word=()=>table().querySelector('[data-color-text="model.claude-opus-5-5"]');
 const picker=document.querySelector('#account-color-value');
 const action=name=>document.querySelector('[data-color-'+name+']');
 const box=node=>{
  const range=document.createRange();range.selectNodeContents(node);
  return range.getBoundingClientRect();
 };
 try{
  frameElement.style.width='1280px';frameElement.style.height='520px';await flush();
  check(headings().map(cell=>cell.dataset.modelSort || '').join(',')===
   'model,,effort,producers,showrunners,units,workers,total','the Models columns are in order');
  check(rows().map(row=>cellText(row).slice(0,3).join(':')).join(',')===
   'claude-fable-5-1::max,claude-opus-5-5::low,::high,gpt-6.2-sol::medium,unknown::-,total::',
   'model IDs and efforts are grouped');
  check(rows().at(-1).cells[7].textContent===document.querySelector('[data-sort-table=roles] tbody')
   .lastElementChild.cells[8].textContent,'both tables show the same total');
  check(rows()[0].cells[4].querySelector('.inactive').textContent==='0','zero has a muted tone');
  check(table().querySelectorAll('[data-color-key="model.claude-opus-5-5"]').length===1,'each model has one swatch');
  check(header('model').getAttribute('aria-sort')==='ascending','model ascending is the default');
  const layout=key=>{
   const cell=header(key), right=cell.classList.contains('right');
   const label=box([...cell.querySelector('.sort-label').childNodes].find(node=>node.nodeType===Node.TEXT_NODE));
   const value=box(rows().map(row=>row.cells[cell.cellIndex]).find(item=>item.textContent));
   const arrow=cell.querySelector('.sort-arrow').getBoundingClientRect();
   const edge=right ? 'right' : 'left';
   check(Math.abs(label[edge]-value[edge])<=1,key+' header aligns with the values');
   check(right ? arrow.right<label.left : arrow.left>label.right,key+' arrow is on the correct side');
   const labels=[...table().querySelectorAll('.sort-label')].map(span=>box(
    [...span.childNodes].find(node=>node.nodeType===Node.TEXT_NODE)));
   check(labels.every(other=>arrow.right<=other.left || arrow.left>=other.right),key+' arrow clears the labels');
   const scroll=table().parentElement;
   check(scroll.scrollWidth===scroll.clientWidth,key+' fits at 1280px');
  };
  layout('model');
  const total=rows().at(-1).outerHTML;
  const names={low:'claude-opus-5-5', medium:'gpt-6.2-sol', high:'claude-opus-5-5',
   max:'claude-fable-5-1','-':'unknown'};
  const expectedOrders={
   model:['max,low,high,medium,-','medium,low,high,max,-'],
   effort:['low,medium,high,max,-','max,high,medium,low,-'],
   producers:['low,max,high,-,medium','medium,-,high,max,low'],
   showrunners:['medium,high,-,low,max','max,low,-,high,medium'],
   units:['max,low,medium,-,high','high,-,medium,low,max'],
   workers:['medium,high,-,max,low','low,max,-,high,medium'],
   total:['medium,max,low,high,-','-,high,low,max,medium'],
  };
  const checkOrder=(key,reverse)=>{
   const ordered=rows().slice(0,-1);
   check(ordered.map(row=>row.cells[2].textContent).join(',')===expectedOrders[key][Number(reverse)],
    key+' shows the expected order');
   let previous='';
   for(const row of ordered){
    const model=names[row.cells[2].textContent], start=model!==previous;
    check(row.cells[0].textContent===(start ? model : ''),key+' labels each consecutive model group');
    check((row.cells[1].querySelector('button')?.dataset.colorKey || '')===
     (start && model!=='unknown' ? 'model.'+model : ''),key+' shows a swatch for each consecutive model group');
    previous=model;
   }
  };
  checkOrder('model',false);
  header('model').click();checkOrder('model',true);
  header('model').click();
  for(const key of ['effort','producers','showrunners','units','workers','total']){
   const cell=header(key), direction=key==='effort' ? 'ascending' : 'descending';
   check(cell.tabIndex===0,key+' supports focus');
   cell.focus();
   cell.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true,cancelable:true}));
   check(cell.getAttribute('aria-sort')===direction,key+' sorts on Enter');
   checkOrder(key,false);
   layout(key);
   cell.click();
   check(cell.getAttribute('aria-sort')===(direction==='ascending' ? 'descending' : 'ascending'),key+' reverses on click');
   checkOrder(key,true);
   layout(key);
   check(rows().at(-1).outerHTML===total,key+' keeps the total at the end');
  }
  header('workers').click();
  check(rows()[0].cells[0].textContent==='gpt-6.2-sol','worker count sorts numerically');
  account('left').click();
  check(JSON.parse(localStorage.getItem('accounts-sort')).column==='left' &&
   JSON.parse(localStorage.getItem('models-sort')).column==='workers','each table stores its own choice');
  const prior=table();
  document.dispatchEvent(new Event('visibilitychange'));await flush();await flush();
  check(table()!==prior && header('workers').getAttribute('aria-sort')==='descending' &&
   rows()[0].cells[0].textContent==='gpt-6.2-sol','refresh retains the Models sort');
  swatch().click();
  picker.value='#102030';action('try').click();
  check(word().style.color==='rgb(16, 32, 48)' && swatch().style.background==='rgb(16, 32, 48)',
   'Try previews the model name and swatch');
  action('cancel').click();
  check(word().style.color==='rgb(224, 138, 92)' && saves.length===0,'Cancel restores the configured color');
  swatch().click();picker.value='#203040';action('save').click();
  const form=new URLSearchParams(saves[0].options.body);
  check(form.get('key')==='model.claude-opus-5-5' && form.get('value')==='#203040','Save sends the model color key');
  saves[0].resolve({ok:true,json:async()=>({state:'saved'})});await flush();
  const defaultSwatch=table().querySelector('[data-color-key="model.gpt-6.2-sol"]');
  const defaultWord=table().querySelector('[data-color-text="model.gpt-6.2-sol"]');
  const popup=document.querySelector('[data-color-picker]');
  for(const [theme,ink] of [['light','#0b0b0b'],['dark','#f0efec']]){
   document.documentElement.dataset.theme=theme;
   check(defaultSwatch.dataset.savedColor==='' &&
    getComputedStyle(defaultSwatch).backgroundColor===getComputedStyle(defaultWord).color,
    'a model without a color uses the theme ink for its name and swatch');
   defaultSwatch.click();
   check(popup.open && picker.value===ink,'the unconfigured swatch opens the picker with the theme ink');
   picker.value='#123456';action('try').click();action('cancel').click();
   check(defaultWord.style.color==='' && defaultSwatch.style.background==='',
    'Cancel restores the theme ink');
  }
  defaultSwatch.click();picker.value='#304050';action('save').click();
  const newColor=new URLSearchParams(saves[1].options.body);
  check(newColor.get('key')==='model.gpt-6.2-sol' && newColor.get('value')==='#304050',
   'Save sends the missing model color key');
  saves[1].resolve({ok:true,json:async()=>({state:'saved'})});await flush();
 }catch(error){failures.push(String(error));}
 await realFetch('/results',{method:'POST',body:JSON.stringify(failures)});
})();</script>"""
        page = rendered.replace("<script>", setup + "<script>", 1).replace("</body>", probe + "</body>")
        self.assertEqual(firefox_probe_results(page.encode()), [[]])

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required for browser behavior")
    def test_each_sort_keeps_the_next_cell_with_its_role_row(self) -> None:
        reading = sort_reading()
        rows = tuple(replace(row, roles=("showrunners", "units"), selected_roles=("units", "showrunners"),
                             credits=123, use_credits=True)
                     if row.label == "claude1" else replace(row, selected_roles=()) for row in reading.rows)
        reading = replace(reading, rows=rows,
                          first_next_moves=accounts.FirstNextMoveTargets({"units": "claude2", "showrunners": "claude3"}))
        with patch.object(accounts, "read_status", return_value=reading):
            rendered = accounts_page.AccountsRender(accounts_page.page(), .1, 200).with_recent_times((.1,)).decode()
        fresh = replace(reading, first_next_moves=accounts.FirstNextMoveTargets({"units": "claude3"}))
        with patch.object(accounts, "read_status", return_value=fresh):
            refreshed = accounts_page.AccountsRender(accounts_page.page(), .1, 200).with_recent_times((.1,)).decode()
        setup = '''<script>
localStorage.removeItem('accounts-sort');Date.now=()=>200000;
const realFetch=fetch;
window.fetch=async()=>({ok:true,text:async()=>FRESH_PAGE});
</script>'''.replace("FRESH_PAGE", json.dumps(refreshed).replace("</", "<\\/"))
        probe = '''<script>
(async()=>{
 const failures=[],check=(ok,label)=>{if(!ok) failures.push(label);};
 const table=()=>document.querySelector('[data-sort-table=roles] table');
 const rows=()=>[...table().tBodies[0].rows];
 const next=row=>row.querySelector('.next-cell');
 const target=row=>next(row).textContent.trim();
 const group=name=>table().querySelector('[data-move-account="claude1"][data-move-group="'+name+'"]').closest('tr');
 const flush=()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
 try{
  const original=rows(), cells=new Map(original.map(row=>[row,next(row)]));
  const units=group('units'), showrunners=group('showrunners');
  check(target(units)==='claude2' && target(showrunners)==='claude3','the two roles have distinct next targets');
  const nextHeader=table().querySelector('[data-role-sort="next"]');
  const button=next(units).querySelector('[data-next-button]');
  button.click();
  const menu=document.querySelector('[data-move-menu]');
  check(!menu.hidden && nextHeader.getAttribute('aria-sort')==='none'
   && rows().every((row,index)=>row===original[index]),'the next button preserves the sort');
  menu.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true,cancelable:true}));
  nextHeader.click();
  check(nextHeader.tabIndex===0 && nextHeader.getAttribute('aria-sort')==='ascending'
   && rows()[0]===units && rows()[1]===showrunners,'the next header sorts targets ascending');
  nextHeader.click();
  check(nextHeader.getAttribute('aria-sort')==='descending' && rows()[0]===showrunners && rows()[1]===units,
   'the next header reverses the target order');
  const markup=new Map(original.map(row=>[row,next(row)?.innerHTML]));
  for(const column of ['account','role','next','idle','paused','exited','total','busy']){
   const header=table().querySelector('[data-role-sort="'+column+'"]');
   for(let direction=0;direction<2;direction++){
    header.click();
    check(rows().every(row=>next(row)===cells.get(row) && next(row)?.innerHTML===markup.get(row)),
     column+' keeps the next cells on their rows in direction '+direction);
   }
  }
  check(rows().at(-1)===original.at(-1) && rows().at(-1).cells[3].textContent==='','the total keeps its empty next cell');
  const before=rows();
  document.dispatchEvent(new Event('visibilitychange'));await flush();await flush();
  check(rows()[0]!==before[0] && table().querySelector('[data-role-sort="busy"]').getAttribute('aria-sort')==='ascending',
   'refresh replaces the rows and keeps busy ascending');
  check(target(group('units'))==='claude3' && target(group('showrunners'))==='none',
   'refresh shows the new targets on their role rows');
 }catch(error){failures.push(String(error));}
 await realFetch('/results',{method:'POST',body:JSON.stringify(failures)});
})();</script>'''
        page = rendered.replace("<script>", setup + "<script>", 1).replace("</body>", probe + "</body>")
        self.assertEqual(firefox_probe_results(page.encode()), [[]])

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required for browser behavior")
    def test_sort_reads_saved_choices_and_handles_unavailable_storage(self) -> None:
        with patch.object(accounts, "read_status", return_value=sort_reading()):
            content = accounts_page.AccountsRender(accounts_page.page(), .1, 200).with_recent_times((.1,)).decode()
        for saved, denied in (("left", False), ("unknown", False), ("left", True)):
            with self.subTest(saved=saved, denied=denied):
                setup = '''<script>
Date.now=()=>200000;const realFetch=fetch;
localStorage.setItem('accounts-sort',JSON.stringify({column:SAVED_COLUMN,reverse:false}));
if(DENIED) Object.defineProperty(window,'localStorage',{get:()=>{throw new Error('Storage denied');}});
</script>'''.replace("SAVED_COLUMN", json.dumps(saved)).replace("DENIED", str(denied).lower())
                probe = '''<script>
const failures=[],check=(ok,label)=>{if(!ok) failures.push(label);};
try{
 const selected=document.querySelector('th[aria-sort="ascending"]');
 check(selected?.dataset.accountSort===EXPECTED_COLUMN,'the stored choice or default renders');
 document.querySelector('[data-role-sort="busy"]').click();
 check(document.querySelector('[data-role-sort="busy"]').getAttribute('aria-sort')==='descending',
  'sorting works when the storage write fails');
}catch(error){failures.push(String(error));}
realFetch('/results',{method:'POST',body:JSON.stringify(failures)});
</script>'''.replace("EXPECTED_COLUMN", json.dumps("left" if saved == "left" and not denied else "account"))
                page = content.replace("<script>", setup + "<script>", 1).replace("</body>", probe + "</body>")
                self.assertEqual(firefox_probe_results(page.encode()), [[]])

    def test_producer_rows_follow_existing_roles_and_show_details_once(self) -> None:
        rows = tuple(replace(status_row(label), tool=tool, roles=("producers", role), selected_roles=(role,),
            role_counts=(accounts.RoleSessionCounts(1, 2, 0, 0), accounts.RoleSessionCounts(4, 0, 0, 0)))
            for label, tool, role in (("claude1", "claude", "showrunners"), ("codex1", "codex", "workers")))
        reading = accounts.StatusReading(rows, (), (), timezone.utc, AT)
        table = accounts.status_board(reading)
        def text(spans: list[accounts.BoardSpan]) -> str:
            return "".join(span["text"] for span in spans)
        self.assertEqual([(text(line["cells"]["label"]), text(line["cells"]["roles"]), text(line["cells"]["busy"]))
                          for line in table["rows"]],
                         [("claude1", "showrunners ✓", "4"), ("", "producers", "1"),
                          ("codex1", "workers ✓", "4"), ("", "producers", "1"), ("total", "", "10")])
        for index in (1, 3):
            for key in ("dot", "label", "left", "resets", "credits", "enabled"):
                self.assertEqual(table["rows"][index]["cells"][key], [])
        markup = accounts_page.board(reading)
        self.assertEqual(markup.count('data-color-key="account.claude1"'), 1)
        self.assertEqual(markup.count("data-account-name"), 2)
        self.assertEqual(markup.count(">producers</span>"), 1)

    def test_account_details_follow_supplied_row_order(self) -> None:
        reading = accounts.StatusReading((replace(status_row("claude1"), roles=("units", "producers")),
                                          status_row("claude2")), (), (), timezone.utc, AT)
        with patch.object(accounts, "account_details_in_row_order", side_effect=list):
            first, producer, other, _total = accounts.status_board(reading)["rows"]
        ordered = accounts.account_details_in_row_order([producer, first, other, first])
        self.assertEqual([line["cells"]["label"] for line in ordered],
                         [producer["cells"]["label"], [], other["cells"]["label"], first["cells"]["label"]])
        self.assertTrue(all(ordered[0]["cells"][key] for key in ("dot", "label", "left", "resets", "credits", "enabled")))
        self.assertTrue(first["cells"]["label"])
        self.assertEqual(accounts.account_details_in_row_order([producer])[0], producer)

    def test_lone_producer_row_has_full_details_and_fixture_swatch(self) -> None:
        config = Path(os.environ["CONF_DIR"]) / "statusline.conf"
        _ = config.write_text(config.read_text() + "role.producer=#123abc\n")
        reading = accounts.StatusReading((replace(status_row("claude1"), roles=("producers",), use_credits=True),),
                                         (), (), timezone.utc, AT)
        cells = accounts.status_board(reading)["rows"][0]["cells"]
        self.assertTrue(all(cells[key] for key in ("dot", "label", "left", "resets", "credits", "enabled")))
        self.assertEqual(cells["roles"][0].get("color_key"), "role.producer")
        markup = accounts_page.board(reading)
        self.assertIn('data-color-key="role.producer" data-color-name="producers" data-saved-color="#123abc"', markup)
        self.assertIn('style="background:#123abc"', markup)
        with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess((), 0, "", "")) as run:
            self.assertEqual(accounts_page.save_color("role.producer", "#123abc"), accounts_page.Saved())
            self.assertEqual(cast(tuple[str, ...], run.call_args.args[0])[2:5], ("statusline", "role.producer", "#123abc"))

    def test_account_labels_use_terminal_colors_and_hex_without_css_injection(self) -> None:
        reading = accounts.StatusReading((status_row("claude1"),), (), (), timezone.utc, AT)
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "statusline.conf"
            _ = write_config_fixture(config.parent / "accounts.conf", ACCOUNT_SETTINGS)
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
                "account.claude1=brown\nrole.units=teal\nrole.worker=white\nlevel.ok=green\nmodel.claude-opus-5-5=#e08a5c\n")
            for key, value in (("account.unknown", "#af875f"), ("claude1", "#af875f"), ("role.units", "#af875f"),
                               ("role.showrunner", "#af875f"), ("level.ok", "#af875f"), ("account.claude1", "brown"),
                               ("account.claude1", "#AF875F"), ("account.claude1", "#fff"), ("account.claude1", "#af875f\n"),
                               ("model.sol", "#af875f"), ("model.unknown", "#af875f")):
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
            for key in ("model.claude-opus-5-5", "model.gpt-6.2-sol"):
                with (self.subTest(model_key=key),
                      patch.object(subprocess, "run", return_value=subprocess.CompletedProcess((), 0, "", "")) as run):
                    self.assertEqual(accounts_page.save_color(key, "#123456"), accounts_page.Saved())
                    self.assertEqual(cast(tuple[str, ...], run.call_args.args[0])[2:5], ("statusline", key, "#123456"))
            with patch.object(subprocess, "run", side_effect=OSError("runner unavailable")):
                self.assertEqual(accounts_page.save_color("account.claude1", "#af875f"),
                                 accounts_page.SaveFailed("runner unavailable"))

    def test_color_save_refuses_a_catalog_model_with_a_known_host_suffix(self) -> None:
        config = Path(os.environ["CONF_DIR"])
        key = "model.gpt-6.2-sol.natedev"
        models = config / "models.conf"
        _ = models.write_text("[codex.agents]\ngpt-6.2-sol.natedev=high\n")
        with (patch.dict(os.environ, {"MODELS_CONFIG_FILE": str(models)}),
              patch.object(subprocess, "run") as run):
            self.assertEqual(accounts_page.save_color(key, "#123456"),
                             accounts_page.Refused(".natedev is a host override, not a model color."))
            run.assert_not_called()

    def test_board_marks_the_selected_claude_group_lines_and_lists_the_claude_targets(self) -> None:
        rows = (replace(status_row("claude1"), selected_roles=("units",)), replace(status_row("claude2"), weekly_left_percent=12.4),
                replace(status_row("codex1"), tool="codex", selected_roles=("units",)))
        reading = accounts.StatusReading(rows, (), (), timezone.utc, AT)
        markup = accounts_page.board(reading)
        self.assertIn('data-move-account="claude1" data-move-group="units"', markup)
        self.assertEqual(markup.count("data-move-group="), 1)
        targets = json.dumps([["claude1", "60%", "#af875f", "default"], ["claude2", "12%", "#b294bb", "warning"]])
        self.assertIn('data-move-percent="1"', markup)
        self.assertIn(f'data-move-targets="{html.escape(targets, quote=True)}">', markup)
        settings = Path(os.environ["CONF_DIR"]) / "accounts.conf"
        _ = write_config_fixture(settings, "next_move_percent=2.5\n")
        self.assertIn('data-move-percent="2.5"', accounts_page.board(reading))

    def test_selected_claude_producers_have_an_editable_next_cell(self) -> None:
        rows = (replace(status_row("claude1"), roles=("producers",), selected_roles=("producers",)),
                status_row("claude2"),
                replace(status_row("codex1"), tool="codex", roles=("producers",)))
        reading = accounts.StatusReading(rows, (), (), timezone.utc, AT,
            first_next_moves=accounts.FirstNextMoveTargets({"producers": "claude2"}))
        markup = accounts_page.board(reading)
        self.assertIn('data-move-account="claude1" data-move-group="producers"', markup)
        self.assertIn('data-next-target="claude2" data-move-account="claude1" data-move-group="producers"', markup)
        self.assertEqual(markup.count("data-move-group="), 1)

    def test_inherited_claude_producers_have_an_editable_next_cell(self) -> None:
        row = replace(status_row("claude1"), roles=("showrunners", "producers"),
                      selected_roles=("showrunners", "producers"), inherited_roles=("producers",))
        reading = accounts.StatusReading((row,), (), (), timezone.utc, AT,
                                         first_next_moves=accounts.FirstNextMoveTargets())
        markup = accounts_page.board(reading)
        self.assertIn('data-move-account="claude1" data-move-group="producers"', markup)
        self.assertIn('> (inherited)</span>', markup)
        self.assertIn('data-next-target="" data-move-account="claude1" data-move-group="producers"', markup)

    def test_next_cells_show_first_targets_empty_groups_unselected_roles_and_unavailable_reads(self) -> None:
        rows = (replace(status_row("claude1"), roles=("units", "showrunners"), selected_roles=("units", "showrunners")),
                status_row("claude2"), replace(status_row("claude3"), roles=("spare",)),
                replace(status_row("codex1"), tool="codex", roles=("workers",), selected_roles=("workers",)),
                replace(status_row("codex2"), tool="codex", roles=("spare",)))
        for first in (accounts.FirstNextMoveTargets({"units": "claude2", "workers": "codex2"}),
                      accounts.FirstNextMoveTargets(), accounts.NextMovesUnavailable("fixture damaged log")):
            with self.subTest(first=first):
                markup = accounts_page.board(accounts.StatusReading(rows, (), (), timezone.utc, AT, first_next_moves=first))
                cells = cast(list[str], re.findall(r'<td class="left next-cell">(.*?)</td>', markup, re.S))
                self.assertEqual(len(cells), 4)
                showrunners, spare, units, workers = cells
                self.assertEqual(spare.count('title="Not the selected account for a role"'), 2)
                self.assertEqual(spare.count('lucide-minus'), 2)
                self.assertIn('lucide-minus', units)
                for cell, group in ((units, "units"), (showrunners, "showrunners")):
                    self.assertIn(f'aria-label="Choose the next account for {group}"', cell)
                    self.assertIn('aria-haspopup="menu" aria-expanded="false"', cell)
                    self.assertIn('lucide-chevron-down', cell)
                self.assertNotIn('<button', workers)
                if isinstance(first, accounts.NextMovesUnavailable):
                    for cell in (showrunners, units, workers):
                        self.assertIn('>unavailable</span>', cell)
                        self.assertIn('fixture damaged log', cell)
                else:
                    self.assertIn('>none</span>', showrunners)
                    if first.targets:
                        self.assertIn('data-color-text="account.claude2">claude2</span>', units)
                        self.assertIn('style="color:#123456" data-color-text="account.codex2">codex2</span>', workers)
                    else:
                        self.assertIn('>none</span>', units)
                        self.assertIn('title="Codex workers move between the Codex accounts by themselves">auto</span>', workers)

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required for browser contrast")
    def test_muted_next_cells_meet_the_contrast_floors_in_both_themes(self) -> None:
        rows = (replace(status_row("claude1"), selected_roles=("units",)), status_row("claude2"),
                replace(status_row("codex1"), tool="codex", roles=("workers",), selected_roles=("workers",)))
        reading = accounts.StatusReading(rows, (), (), timezone.utc, AT, first_next_moves=accounts.FirstNextMoveTargets())
        boards = accounts_page.board(reading) + accounts_page.board(replace(
            reading, first_next_moves=accounts.NextMovesUnavailable("fixture unavailable")))
        probe = '''<script>
(async()=>{
 const failures=[], check=(ok,label)=>{if(!ok) failures.push(label);};
 const luminance=color=>{
  const channels=color.match(/[0-9.]+/g).slice(0,3).map(value=>Number(value)/255);
  const linear=channels.map(value=>value<=0.04045 ? value/12.92 : ((value+0.055)/1.055)**2.4);
  return linear.reduce((sum,value,index)=>sum+value*[0.2126,0.7152,0.0722][index],0);
 };
 const contrast=(first,second)=>{
  const high=Math.max(first,second),low=Math.min(first,second);
  return (high+0.05)/(low+0.05);
 };
 try{
  const cells=[...document.querySelectorAll('.next-muted')];
  check(cells.some(cell=>cell.textContent==='none') && cells.some(cell=>cell.textContent==='auto') &&
   cells.some(cell=>cell.textContent==='unavailable') && cells.some(cell=>cell.querySelector('svg')),
   'the contrast rows include every muted cell');
  for(const theme of ['light','dark']){
   document.documentElement.dataset.theme=theme;
   const colors=new Set(cells.map(cell=>getComputedStyle(cell).color));
   check(colors.size===1,theme+': every muted cell uses one gray');
   for(const background of ['card','hover']){
    for(const card of document.querySelectorAll('.card')) card.style.background='var(--'+background+')';
    for(const cell of cells){
     const fill=luminance(getComputedStyle(cell.closest('.card')).backgroundColor);
     const icon=cell.querySelector('svg');
     const ink=luminance(getComputedStyle(icon || cell).color);
     check(contrast(ink,fill)>=(icon ? 3 : 4.5),theme+' '+background+': '+(icon ? 'minus' : cell.textContent));
     if(icon) check(getComputedStyle(icon).stroke===getComputedStyle(cell).color,theme+': the minus uses the muted gray');
    }
   }
  }
 }catch(error){failures.push(String(error));}
 await fetch('/results',{method:'POST',body:JSON.stringify(failures)});
})();</script>'''
        page = ('<!doctype html><html><head>' + accounts_page.STYLE + '</head><body>' + boards + probe + '</body></html>').encode()
        self.assertEqual(firefox_probe_results(page), [[]])

    def test_clear_removes_only_the_first_group_entry_and_refuses_an_empty_group(self) -> None:
        home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        with (scratch_account_home(home, {}, clear=True), patch.object(role_map, "is_role_map_host", return_value=True)):
            for group in ("units", "showrunners", "producers"):
                with self.subTest(group=group):
                    _ = next_moves.add_next_move(group, 1, "claude2")
                    later = next_moves.add_next_move(group, 2, "claude3")
                    other = next_moves.add_next_move("workers", 3, "codex2")
                    self.assertEqual(accounts_page.clear_next_move(group), accounts_page.Saved())
                    self.assertEqual(next_moves.read_next_moves(), (later, other))
                    self.assertEqual(accounts_page.clear_next_move(group), accounts_page.Saved())
                    before = next_moves.next_moves_file().read_bytes()
                    self.assertEqual(accounts_page.clear_next_move(group), accounts_page.Refused("The group has no next move."))
                    self.assertEqual(accounts_page.clear_next_move("workers"), accounts_page.Refused("Choose units, showrunners, or producers."))
                    self.assertEqual(next_moves.next_moves_file().read_bytes(), before)
                    next_moves.remove_next_move("workers", next_moves.FIRST_GROUP_POSITION)
            with patch.object(next_moves, "read_next_moves", side_effect=next_moves.NextMoveError("fixture damaged log")):
                self.assertEqual(accounts_page.clear_next_move("units"), accounts_page.SaveFailed("fixture damaged log"))

    def test_next_move_save_checks_the_group_and_account_then_sets_the_first_entry_at_the_configured_percent(self) -> None:
        with patch.object(next_moves, "set_first_next_move") as setter:
            self.assertEqual(accounts_page.save_next_move("workers", "claude2"),
                             accounts_page.Refused("Choose units, showrunners, or producers."))
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
    def test_the_next_button_opens_the_move_menu_and_saves_or_clears_the_chosen_account(self) -> None:
        rows = (replace(status_row("claude1"), selected_roles=("units",)), status_row("claude2"),
                replace(status_row("claude3"), weekly_left_percent=accounts.StatusUnavailable("unavailable")),
                replace(status_row("codex1"), tool="codex"))
        reading = accounts.StatusReading(rows, (), (), timezone.utc, AT,
            first_next_moves=accounts.FirstNextMoveTargets({"units": "claude2"}))
        with patch.object(accounts, "read_status", return_value=reading):
            content = accounts_page.page()
        initial = accounts_page.AccountsRender(content, 0.5, 200).with_recent_times((0.5,)).decode()
        with patch.object(accounts, "read_status", return_value=replace(reading, first_next_moves=accounts.FirstNextMoveTargets())):
            empty = accounts_page.page().decode()
        probe = MENU_PROBE.replace("EMPTY_PAGE", json.dumps(empty).replace("</", "<\\/"))
        setup = MENU_SETUP.replace("FRESH_PAGE", json.dumps(initial).replace("</", "<\\/"))
        page = initial.replace("<script>", setup + "<script>", 1).replace("</body>", probe + "</body>").encode()
        self.assertEqual(firefox_probe_results(page), [[]])

    def test_page_reads_stored_status_and_holds_the_refresh_root(self) -> None:
        reading = accounts.StatusReading((), (), (), timezone.utc, AT)
        with tempfile.TemporaryDirectory() as temporary:
            settings = Path(temporary) / "review_links.conf"
            _ = write_config_fixture(Path(temporary) / "accounts.conf", ACCOUNT_SETTINGS)
            _ = write_config_fixture(Path(temporary) / "console.conf", PAGE_STATUS_SETTINGS)
            for seconds in (15, 37):
                with self.subTest(refresh_seconds=seconds):
                    _ = write_config_fixture(settings, f"page_refresh_seconds={seconds}\n")
                    with (patch.object(accounts, "read_status", return_value=reading) as status,
                          patch.dict(os.environ, {"CONF_DIR": temporary})):
                        page = accounts_page.page().decode()
                    source: object = status.call_args.kwargs["source"]  # pyright: ignore[reportAny]
                    self.assertIsInstance(source, accounts.StoredUsageOnly)
                    status.assert_called_once_with(source=accounts.StoredUsageOnly(), include_models=True)
                    self.assertIn("<div data-accounts>", page)
                    self.assertIn(console_theme.CSS, page)
                    self.assertIn(page_frame.STYLE, page)
                    self.assertEqual(page.count(page_frame.BUTTON_STATES), 1)
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
            _ = write_config_fixture(Path(temporary) / "accounts.conf", ACCOUNT_SETTINGS)
            _ = write_config_fixture(Path(temporary) / "console.conf", PAGE_STATUS_SETTINGS)
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
        refresh = conf.read("review_links").number("page_refresh_seconds")
        with (patch.object(time, "monotonic", return_value=100),
              patch.object(accounts_page, "render_with_facts", return_value=accounts_page.AccountsRender(content, .1, time.time()))):
            good = cache.get()
        with (patch.object(time, "monotonic", return_value=101 + refresh),
              patch.object(accounts_page, "render_with_facts", side_effect=OSError("private failure"))):
            failed = cache.get()
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
        refresh = conf.read("review_links").number("page_refresh_seconds")
        with (patch.object(time, "monotonic", return_value=100),
              patch.object(accounts_page, "render_with_facts", return_value=accounts_page.AccountsRender(content, .1, time.time()))):
            good = cache.get()
        with (patch.object(time, "monotonic", return_value=101 + refresh),
              patch.object(accounts_page, "render_with_facts", side_effect=OSError("private failure"))):
            failed = cache.get()
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
        reading = accounts.StatusReading((status_row("claude1"), replace(status_row("claude2"), selected_roles=("units",))),
            (), (), timezone.utc, AT, first_next_moves=accounts.FirstNextMoveTargets({"units": "claude1"}))
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
  const nextTarget=()=>document.querySelector('.next-cell [data-color-text="account.claude1"]');
  const sameAccountColor=()=>nextTarget()?.style.color===label().style.color;
  const action=name=>popup.querySelector('[data-color-'+name+']');
  const original=label().style.color, originalSwatch=swatch.style.background;
  const row=swatch.closest('tr'), cell=swatch.closest('td');
  const headings=table=>[...document.querySelectorAll('table[aria-label="'+table+'"] thead th')].map(head=>{
   const copy=head.cloneNode(true);
   copy.querySelector('.sort-arrow')?.remove();
   return copy.textContent;
  });
  const heads=headings('Accounts'), roleHeads=headings('Roles');
  check(cell===row.cells[2] && row.cells[1].contains(label()) && heads[1]==='account' && heads[2]==='' && heads[3]==='left',
   'the swatch column without a heading sits between account and left');
  check(roleHeads.slice(0,5).join(',')==='role,,account,next,busy',
   'the Roles table puts the next column between account and busy');
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
  check(sameAccountColor(),'Try paints the next target with the account name color');
  pick('#234567');
  check(label().style.color==='rgb(18, 52, 86)' && preview.style.background==='rgb(35, 69, 103)',
   'a later pick keeps the last tried color on the page');
  check(otherLabel.style.color===otherColor,'Try keeps the other account color');
  check(state()==='Paused','the chooser pauses refresh');
  action('cancel').click();
  check(!popup.open && label().style.color===original && swatch.style.background===originalSwatch,'Cancel restores the saved color');
  check(sameAccountColor(),'Cancel restores the next target and account name to the same color');
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
  check(sameAccountColor(),'a pending Save keeps the next target and account name the same color');
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
  check(roles.length===1 && roleWords.length===2 &&
   roleWords.every(word=>word.textContent==='units' && word.style.color==='rgb(215, 175, 0)') &&
   new Set(roleWords.map(word=>word.closest('table'))).size===2,
   'one swatch controls the units label in each table');
  roles[0].click();
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
