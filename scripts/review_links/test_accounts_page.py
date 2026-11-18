"""The console Accounts page draws the accounts board table."""
from __future__ import annotations
from scripts.lint.memory_test_support import copy_config_fixture, write_config_fixture

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

from scripts.review_links.test_page_frame import assert_status_states
from scripts.accounts import accounts, next_moves, role_map
from scripts.accounts.home_test_support import scratch_account_home
from scripts.lib import conf
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.review_links import accounts_page, console_theme, page_frame, serve
from scripts.whoami import agent_accounts

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
                role_counts=(accounts.RoleSessionCounts(2, 0, 0, 0),) * 2),
        replace(status_row("claude1"), roles=("workers", "units"), weekly_left_percent=10,
                role_counts=(accounts.RoleSessionCounts(1, 0, 0, 0), accounts.RoleSessionCounts(2, 0, 0, 0))),
        replace(status_row("claude3"), roles=(), role_counts=(), weekly_left_percent=accounts.StatusUnavailable("fixture")),
        replace(status_row("codex1"), tool="codex", roles=("producers",), weekly_left_percent=0,
                role_counts=(accounts.RoleSessionCounts(0, 1, 0, 0),)),
        replace(status_row("codex2"), tool="codex", roles=("spare",), role_counts=(accounts.NoRoleCount(),),
                weekly_left_percent=accounts.StatusUnavailable("fixture"))), (), (), timezone.utc, AT,
        upcoming_moves=("Upcoming moves:", "units → claude2"))


class ProducerRoleColorTests(unittest.TestCase):
    def test_the_producer_role_uses_its_color_and_account_names_keep_account_colors(self) -> None:
        configuration = Path(self.enterContext(tempfile.TemporaryDirectory()))
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
 const groupRow=()=>document.querySelector('tr[data-move-group]');
 try{
  if(document.readyState!=='complete') await new Promise(resolve=>addEventListener('load',resolve,{once:true}));
  frameElement.style.width='800px';frameElement.style.height='480px';await flush();
  check(document.querySelectorAll('tr[data-move-group]').length===1 && groupRow().dataset.moveAccount==='claude1'
   && groupRow().dataset.moveGroup==='units','only the selected Claude group line takes the menu');
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
    @override
    def setUp(self) -> None:
        configuration = Path(self.enterContext(tempfile.TemporaryDirectory())) / "config"
        _ = copy_config_fixture(Path(__file__).resolve().parents[2] / "config", configuration)
        _ = write_config_fixture(configuration / "accounts.conf", ACCOUNT_SETTINGS)
        _ = write_config_fixture(configuration / "console.conf", PAGE_STATUS_SETTINGS)
        _ = write_config_fixture(configuration / "review_links.conf", "page_refresh_seconds=15\naccounts_page_seconds=30\n")
        _ = (configuration / "statusline.conf").write_text(
            "account.claude1=#af875f\naccount.claude2=#b294bb\naccount.codex2=#123456\nrole.unit-director=#d7af00\n",
            encoding="utf-8")
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"CONF_DIR": str(configuration)})))

    def test_board_draws_the_board_table_with_moves_and_problems(self) -> None:
        config = Path(self.enterContext(tempfile.TemporaryDirectory(prefix='accounts-board-config-')))
        _ = write_config_fixture(config / "accounts.conf", ACCOUNT_SETTINGS)
        _ = (config / 'statusline.conf').write_text('account.claude1=brown\nrole.unit-director=yellow\nrole.worker=#c0c0c0\n')
        _ = cast(object, self.enterContext(patch.dict(os.environ, {'CONF_DIR': str(config)})))
        reading = accounts.StatusReading((status_row("claude1"),), ("* <stale>",), (), timezone.utc, AT,
                                         upcoming_moves=("Upcoming moves:", "units → claude1"))
        markup = accounts_page.board(reading)
        table = accounts.status_board(reading)
        self.assertEqual([column["key"] for column in table["columns"]][1:4], ["label", "left", "roles"])
        self.assertIn('<thead><tr><th class="left"></th>'
                      + '<th class="left" data-account-sort="account" tabindex="0" aria-sort="ascending">account'
                      + '<span class="sort-arrow" aria-hidden="true">▲</span></th><th class="left"></th>'
                      + '<th class="right" data-account-sort="left" tabindex="0" aria-sort="none">left'
                      + '<span class="sort-arrow" aria-hidden="true">▲</span></th>'
                      + '<th class="left" data-account-sort="role" tabindex="0" aria-sort="none">role'
                      + '<span class="sort-arrow" aria-hidden="true">▲</span></th><th class="left"></th>'
                      + '<th class="left">next</th>'
                      + '<th class="right" data-account-sort="busy" tabindex="0" aria-sort="none">busy'
                      + '<span class="sort-arrow" aria-hidden="true">▲</span></th>', markup)
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
                      + '<td class="left next-cell"><span class="next-muted next-minus" title="Not the selected account for a role">'
                      + page_frame.icon_svg("minus") + '</span></td><td class="right"><span class="default">1</span></td>', markup)
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
        self.assertEqual(markup.count("<td "), len(table["rows"]) * (len(table["columns"]) + 3))

    def test_all_account_sort_orders_include_ties_and_missing_values(self) -> None:
        reading = sort_reading()
        table = accounts.status_board(reading)
        orders = accounts.account_board_sort_orders(reading, table)
        expected = {"account": [0, 1, 2, 3, 4, 5, 6], "left": [5, 0, 1, 2, 3, 4, 6],
                    "role": [3, 5, 2, 6, 0, 1, 4], "busy": [0, 2, 3, 1, 5, 4, 6]}
        for column, indices in expected.items():
            with self.subTest(column=column):
                self.assertEqual([place["index"] for place in orders[column]], indices)
        precise = replace(reading, rows=tuple(replace(row, weekly_left_percent=10.1 if row.label == "claude2" else 10.2)
                          if row.label in ("claude1", "claude2") else row for row in reading.rows))
        self.assertEqual([place["index"] for place in accounts.account_board_sort_orders(precise, accounts.status_board(precise))["left"]],
                         [5, 2, 3, 0, 1, 4, 6])

    def test_reversed_account_sort_orders_keep_missing_values_last(self) -> None:
        reading = sort_reading()
        orders = accounts.account_board_sort_orders(reading, accounts.status_board(reading))
        expected = {"account-reverse": [6, 5, 4, 3, 2, 1, 0], "left-reverse": [3, 2, 1, 0, 5, 6, 4],
                    "role-reverse": [1, 0, 6, 2, 5, 3, 4], "busy-reverse": [5, 1, 2, 3, 0, 6, 4]}
        for column, indices in expected.items():
            with self.subTest(column=column):
                self.assertEqual([place["index"] for place in orders[column]], indices)

    def test_sort_details_repeat_for_split_accounts_and_preserve_the_total(self) -> None:
        reading = sort_reading()
        table = accounts.status_board(reading)
        before = json.dumps(table)
        orders = accounts.account_board_sort_orders(reading, table)
        self.assertEqual([place["details"] for place in orders["role"]], [True, True, True, True, True, False, True])
        self.assertEqual([place["details"] for place in orders["account"]], [True, False, True, False, True, True, True])
        self.assertTrue(all(len(order) == len(table["rows"]) - 1 for order in orders.values()))
        self.assertEqual(json.dumps(table), before)
        self.assertEqual(table["rows"][-1]["cells"]["label"][0]["text"], "total")

    def test_default_account_sort_uses_the_role_order_and_four_focusable_headers(self) -> None:
        reading = sort_reading()
        table = accounts.status_board(reading)
        self.assertEqual([row["cells"]["roles"][0]["text"] for row in table["rows"][:-1]],
                         ["units", "workers", "showrunners", "producers", "", "producers", "spare"])
        markup = accounts_page.board(reading)
        self.assertEqual(markup.count('data-account-sort="'), 4)
        self.assertEqual(markup.count('tabindex="0"'), 4)
        self.assertEqual(markup.count('class="sort-arrow"'), 4)
        self.assertIn('data-account-sort="account" tabindex="0" aria-sort="ascending"', markup)

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
 const header=key=>document.querySelector('[data-account-sort="'+key+'"]');
 const keys=()=>{let account='';return [...document.querySelector('tbody').rows].slice(0,-1).map(row=>{
  account=row.cells[1].textContent || account;return account+':'+row.cells[4].textContent.replace(' ✓','');
 });};
 const click=key=>header(key).click();
 try{
  frameElement.style.width='1280px';frameElement.style.height='700px';await flush();
  const original=keys(),total=document.querySelector('tbody').lastElementChild.outerHTML;
  const moves=document.querySelector('.moves').textContent;
  const geometry=()=>[...document.querySelectorAll('th')].map(node=>[node.getBoundingClientRect().x,node.getBoundingClientRect().width]);
  const initialGeometry=geometry();
  const same=(indices,label)=>{
   check(JSON.stringify(keys())===JSON.stringify(indices.map(index=>original[index])),label);
   check(document.querySelector('tbody').lastElementChild.outerHTML===total,'the total stays last and unchanged: '+label);
   check(document.querySelector('.moves').textContent===moves,'moves stay unchanged: '+label);
   check(geometry().every((box,index)=>box.every((value,side)=>Math.abs(value-initialGeometry[index][side])<.5)),
    'columns keep their positions: '+label);
  };
  check(original[0]==='claude1:units' && original[1]==='claude1:workers','the default account order');
  check(document.querySelectorAll('[data-account-sort]').length===4,'only four headers sort');
  check([...document.querySelectorAll('[data-account-sort]')].every(node=>node.tabIndex===0 && getComputedStyle(node).cursor==='pointer'),
   'the sort headers support focus and show a pointer');
  click('account');same([6,5,4,3,2,1,0],'a second account choice reverses');
  check(header('account').getAttribute('aria-sort')==='descending','the account arrow reverses');
  click('account');same([0,1,2,3,4,5,6],'account ascending');
  click('left');same([5,0,1,2,3,4,6],'left ascending');
  click('left');same([3,2,1,0,5,6,4],'left descending');
  header('role').focus();header('role').dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true,cancelable:true}));
  same([3,5,2,6,0,1,4],'Enter selects role ascending');
  const roleRows=[...document.querySelector('tbody').rows];
  check(roleRows[0].cells[1].textContent==='claude2' && roleRows[2].cells[1].textContent==='claude2',
   'split accounts repeat details');
  check(roleRows[4].cells[1].textContent==='claude1' && roleRows[5].cells[1].textContent==='',
   'a consecutive account run shows details once');
  click('role');same([1,0,6,2,5,3,4],'role descending');
  click('busy');same([0,2,3,1,5,4,6],'busy descending');
  check(header('busy').getAttribute('aria-sort')==='descending' && header('busy').querySelector('.sort-arrow').textContent==='▼',
   'busy starts with the descending arrow');
  click('busy');same([5,1,2,3,0,6,4],'busy ascending');
  check(JSON.parse(localStorage.getItem('accounts-sort')).reverse===true,'the choice enters viewer storage');
  const oldTable=document.querySelector('table');
  document.dispatchEvent(new Event('visibilitychange'));await flush();await flush();
  check(reads>0 && document.querySelector('table')!==oldTable,'a live refresh replaces the table');
  same([5,1,2,3,0,6,4],'a live refresh keeps the sort');
 }catch(error){failures.push(String(error));}
 await realFetch('/results',{method:'POST',body:JSON.stringify(failures)});
})();</script>'''
        page = rendered.replace("<script>", setup + "<script>", 1).replace("</body>", probe + "</body>")
        self.assertEqual(firefox_probe_results(page.encode()), [[]])

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required for browser behavior")
    def test_each_sort_keeps_the_next_cell_with_its_role_row(self) -> None:
        reading = sort_reading()
        rows = tuple(replace(row, roles=("showrunners", "units"), selected_roles=("units", "showrunners"),
                             credits=123, use_credits=True)
                     if row.label == "claude1" else row for row in reading.rows)
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
 const rows=()=>[...document.querySelector('tbody').rows];
 const next=row=>row.querySelector('.next-cell');
 const target=row=>next(row).textContent.trim();
 const details=row=>[...row.cells].slice(-3).map(cell=>cell.textContent);
 const group=name=>document.querySelector('tr[data-move-group="'+name+'"]');
 const flush=()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
 try{
  const original=rows(), cells=new Map(original.map(row=>[row,next(row)]));
  const units=group('units'), showrunners=group('showrunners');
  const accountDetails=details(units);
  check(target(units)==='claude2' && target(showrunners)==='claude3','the two role rows have distinct next targets');
  check(!showrunners.cells[1].textContent,'the second role keeps its next target with hidden account details');
  check(accountDetails[1]==='123' && accountDetails[2]==='yes','the first row has credits and their setting');
  const nextHeader=[...document.querySelectorAll('th')].find(header=>header.textContent==='next');
  const sameRows=()=>rows().every((row,index)=>row===original[index]);
  nextHeader.click();
  check(nextHeader.tabIndex===-1 && sameRows(),'the next header does not sort');
  const markup=new Map(original.map(row=>[row,next(row).innerHTML]));
  for(const column of ['account','left','role','busy']){
   const header=document.querySelector('[data-account-sort="'+column+'"]');
   for(let direction=0;direction<2;direction++){
    header.click();
    check(rows().every(row=>next(row)===cells.get(row) && next(row).innerHTML===markup.get(row)),
     column+' keeps the next cells on their rows in direction '+direction);
    for(const row of [units,showrunners]){
     const expected=row.cells[1].textContent ? accountDetails : ['','',''];
     check(JSON.stringify(details(row))===JSON.stringify(expected),column+' keeps the trailing account details together');
    }
   }
  }
  check(rows().at(-1)===original.at(-1) && target(rows().at(-1))==='','the total keeps its empty next cell');
  const before=rows();
  document.dispatchEvent(new Event('visibilitychange'));await flush();await flush();
  check(rows()[0]!==before[0] && document.querySelector('[data-account-sort="busy"]').getAttribute('aria-sort')==='ascending',
   'the live refresh replaces the rows and keeps busy ascending');
  check(target(group('units'))==='claude3' && target(group('showrunners'))==='none',
   'the live refresh shows the new targets on their role rows');
 }catch(error){failures.push(String(error));}
 await realFetch('/results',{method:'POST',body:JSON.stringify(failures)});
})();</script>'''
        page = rendered.replace("<script>", setup + "<script>", 1).replace("</body>", probe + "</body>")
        self.assertEqual(firefox_probe_results(page.encode()), [[]])

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required for browser behavior")
    def test_sort_reads_saved_choices_and_handles_unavailable_storage(self) -> None:
        with patch.object(accounts, "read_status", return_value=sort_reading()):
            content = accounts_page.AccountsRender(accounts_page.page(), .1, 200).with_recent_times((.1,)).decode()
        for saved, denied in ((True, False), (False, True)):
            with self.subTest(saved=saved, denied=denied):
                setup = '''<script>
Date.now=()=>200000;const realFetch=fetch;
localStorage.setItem('accounts-sort',JSON.stringify({column:'role',reverse:false}));
if(DENIED) Object.defineProperty(window,'localStorage',{get:()=>{throw new Error('Storage denied');}});
</script>'''.replace("DENIED", str(denied).lower())
                probe = '''<script>
const failures=[],check=(ok,label)=>{if(!ok) failures.push(label);};
try{
 const selected=document.querySelector('th[aria-sort="ascending"]');
 check(selected?.dataset.accountSort===(SAVED ? 'role' : 'account'),'the stored choice or default renders');
 document.querySelector('[data-account-sort="busy"]').click();
 check(document.querySelector('[data-account-sort="busy"]').getAttribute('aria-sort')==='descending',
  'sorting works when the storage write fails');
}catch(error){failures.push(String(error));}
realFetch('/results',{method:'POST',body:JSON.stringify(failures)});
</script>'''.replace("SAVED", str(saved).lower())
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
        self.assertEqual(markup.count('data-account-name></td><td class="left"></td><td class="right"></td>'), 2)

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
        self.assertIn('<section class="card" data-move-percent="1" ', markup)
        self.assertIn(f'data-move-targets="{html.escape(targets, quote=True)}">', markup)
        settings = Path(os.environ["CONF_DIR"]) / "accounts.conf"
        _ = write_config_fixture(settings, "next_move_percent=2.5\n")
        self.assertIn('data-move-percent="2.5"', accounts_page.board(reading))

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
                self.assertEqual(len(cells), 7)
                self.assertEqual(cells[-1], "")
                for index in (2, 3, 5):
                    self.assertIn('title="Not the selected account for a role"', cells[index])
                    self.assertIn('lucide-minus', cells[index])
                for index, group in ((0, "units"), (1, "showrunners")):
                    self.assertIn(f'aria-label="Choose the next account for {group}"', cells[index])
                    self.assertIn('aria-haspopup="menu" aria-expanded="false"', cells[index])
                    self.assertIn('lucide-chevron-down', cells[index])
                self.assertNotIn('<button', cells[4])
                if isinstance(first, accounts.NextMovesUnavailable):
                    for index in (0, 1, 4):
                        self.assertIn('>unavailable</span>', cells[index])
                        self.assertIn('fixture damaged log', cells[index])
                else:
                    self.assertIn('>none</span>', cells[1])
                    if first.targets:
                        self.assertIn('class="default bold" style="color:#b294bb" data-color-text="account.claude2">claude2</span>', cells[0])
                        self.assertIn('class="default bold"', cells[4])
                        self.assertIn('style="color:#123456" data-color-text="account.codex2">codex2</span>', cells[4])
                    else:
                        self.assertIn('>none</span>', cells[0])
                        self.assertIn('title="Codex workers move between the Codex accounts by themselves">auto</span>', cells[4])

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
            for group in ("units", "showrunners"):
                with self.subTest(group=group):
                    _ = next_moves.add_next_move(group, 1, "claude2")
                    later = next_moves.add_next_move(group, 2, "claude3")
                    other = next_moves.add_next_move("workers", 3, "codex2")
                    self.assertEqual(accounts_page.clear_next_move(group), accounts_page.Saved())
                    self.assertEqual(next_moves.read_next_moves(), (later, other))
                    self.assertEqual(accounts_page.clear_next_move(group), accounts_page.Saved())
                    before = next_moves.next_moves_file().read_bytes()
                    self.assertEqual(accounts_page.clear_next_move(group), accounts_page.Refused("The group has no next move."))
                    self.assertEqual(accounts_page.clear_next_move("workers"), accounts_page.Refused("Choose units or showrunners."))
                    self.assertEqual(next_moves.next_moves_file().read_bytes(), before)
                    next_moves.remove_next_move("workers", next_moves.FIRST_GROUP_POSITION)
            with patch.object(next_moves, "read_next_moves", side_effect=next_moves.NextMoveError("fixture damaged log")):
                self.assertEqual(accounts_page.clear_next_move("units"), accounts_page.SaveFailed("fixture damaged log"))

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
  const heads=[...document.querySelectorAll('thead th')].map(head=>{
   const copy=head.cloneNode(true);
   copy.querySelector('.sort-arrow')?.remove();
   return copy.textContent;
  });
  check(cell===row.cells[2] && row.cells[1].contains(label()) && heads[1]==='account' && heads[2]==='' && heads[3]==='left',
   'the swatch column without a heading sits between account and left');
  check(heads[4]==='role' && heads[5]==='' && heads[6]==='next' && heads[7]==='busy',
   'the next column follows the role swatch and precedes busy');
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
