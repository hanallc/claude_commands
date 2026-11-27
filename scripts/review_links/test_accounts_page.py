"""The console Accounts page draws the accounts board table."""
from __future__ import annotations
from scripts.lint.memory_test_support import copy_config_fixture, write_config_fixture

from collections import Counter
from collections.abc import Callable
from contextlib import redirect_stderr
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import html
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import io
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import threading
import unittest
from types import SimpleNamespace
from typing import cast, override
from unittest.mock import ANY, patch
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from scripts.review_links.browser_test_support import assert_status_states, firefox_probe_results, newer_script_reload_results
from scripts.accounts import accounts, login_sittings, moves, next_moves, role_activity, role_map
from scripts.accounts.home_test_support import scratch_account_home
from scripts.agents import remote, shutdown_record, stop_resume
from scripts.agents.test_support import write_machine_config
from scripts.lib import conf
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.production import staff
from scripts.review_links import accounts_page, console_theme, new_item_dialog, page_frame, serve
from scripts.review_links.server_test_support import started_review_server
from scripts.whoami import account, agent_accounts, note_file, quota_state, run_out, usage_store
from scripts.whoami.account_test_support import create_claude_account, stored_room

AT = datetime(2026, 11, 15, 15, tzinfo=timezone.utc)
ACCOUNT_SETTINGS = "next_move_percent=1\nusage_stale_minutes=10\n"
PAGE_STATUS_SETTINGS = (
    "status.round_trip_limit_seconds=1\nstatus.render_limit_seconds=1\n"
    + "status.reading_delay_seconds=1\nstatus.accounts.age_limit_seconds=45\n"
)
LOGIN_CARD_SETTINGS = (
    "login_sitting_machine=natedev\nlogin_sitting_first=2026-11-15T07:00:00-08:00\n"
    "# whole: 1..\nlogin_sitting_interval_days=5\n"
    "# whole: 1..\nlogin_safety_margin_days=3\n"
    "# whole: 1..\nlogin_peer_read_minutes=60\n"
    "# whole: 1..\nlogin_peer_read_timeout_seconds=30\n"
    "# whole: 1..\nlogin_card.error_days=2\n"
    "# whole: 1..\nlogin_card.warning_days=6\n"
)


def status_row(label: str) -> accounts.StatusRow:
    return accounts.StatusRow(
        label=label, tool="claude", folder=f"~/.{label}", login="fixture@example.invalid",
        roles=("units",), role_counts=(accounts.RoleSessionCounts(1, 0, 0, 0),), weekly_left_percent=60,
        resets_at=AT + timedelta(days=1), credits=0, on_credits="no", use_credits=False, read_at=AT,
        read_outcome=agent_accounts.UsageReadOutcome.OK, attempted_at=AT)


def move_progress_fixture() -> moves.MoveProgress:
    minute = timedelta(minutes=1)
    return moves.MoveProgress(
        "d" * 32, "units", "claude1", "", moves.HandMoveSession("brain"), AT,
        tuple(moves.MoveMemberProgress(moves.MoveMember(key, name), state) for key, name, state in (
            ("member-1", "lfo", moves.MemberMoved(AT + minute, minute, "start", "claude1")),
            ("member-2", "praxis", moves.MemberMoveFailed(AT + minute, minute, "Claude gave no prompt in 90 s.",
                                                        "claude1", stopped=True)),
            ("member-3", "fastest", moves.MemberMoving(AT + minute, "claude1")),
            ("member-4", "report", moves.MoveQueued()),
        )),
    )


def first_moves(*entries: next_moves.NextMove) -> accounts.FirstNextMoveEntries:
    return accounts.FirstNextMoveEntries({entry.group: entry for entry in entries})


def claude_move(group: accounts.ClaudeGroup, label: str) -> next_moves.ClaudeGroupMove:
    return next_moves.ClaudeGroupMove(f"{group}-fixture", group, 1, 0, label, AT)


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
        first_next_moves=first_moves(claude_move("units", "claude3"), claude_move("showrunners", "claude1")))


def rendered_sort_orders(markup: str, table: str) -> dict[str, list[int]]:
    encoded = re.search(rf'data-sort-table="{re.escape(table)}" [^>]*data-sort-orders="([^"]+)"', markup)
    assert encoded is not None
    orders = cast(dict[str, list[int | accounts.AccountRowPlacement]], json.loads(html.unescape(encoded[1])))
    return {key: [place if isinstance(place, int) else place["index"] for place in places]
            for key, places in orders.items()}


def use_fixture_history(case: unittest.TestCase, scratch: Path) -> None:
    """Keep the run-out, busy-line and no-worker mode reads in the scratch folder."""
    _ = case.enterContext(patch.object(run_out, "READINGS_LOG", scratch / "readings.jsonl"))
    _ = case.enterContext(patch.object(role_activity, "history_path", return_value=scratch / "role_activity.jsonl"))
    _ = cast(object, case.enterContext(patch.dict(os.environ, {"XDG_STATE_HOME": str(scratch / "state")})))


# The fixed slot after each role total, with the warning icon when sessions are not running.
NOT_RUNNING_SLOT = re.compile(
    r'<span class="not-running-slot">(?:<span class="not-running".*?</svg>\s*</span><span id="[^"]+" hidden>[^<]*</span>)?</span>',
    re.S)


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


class MovesBoardTests(unittest.TestCase):
    def test_interrupted_moves_show_unfinished_units_as_failures(self) -> None:
        scratch = Path(self.enterContext(tempfile.TemporaryDirectory()))
        _ = self.enterContext(patch.object(moves, "store_file", return_value=scratch / "moves.jsonl"))
        move = move_progress_fixture()
        moves.write_move_progress(moves.TrackedMove(move, moves.ClaimOwner(21, "start"), AT))
        with patch.object(moves, "_claim_owner_is_live", return_value=False):
            interrupted, = moves.read_move_progress()
        self.assertFalse(interrupted.active)
        self.assertEqual(interrupted.members[:2], move.members[:2])
        failures = [row.state.reason for row in interrupted.members[2:] if isinstance(row.state, moves.MemberMoveFailed)]
        self.assertEqual(failures, ["The move ended before it recorded a result."] * 2)

    def test_current_move_shows_its_origin_progress_order_and_failures(self) -> None:
        move = move_progress_fixture()
        board = accounts_page.moves_board((move,), AT + timedelta(seconds=60), ZoneInfo("America/Los_Angeles"), 75,
                                         {(move.batch_id, "member-2")})
        self.assertIn("units → claude1", board)
        self.assertIn("by hand · brain session · started Sun 07:00 PST", board)
        self.assertIn('aria-label="1 of 4 moved"', board)
        self.assertIn("about 1 min left", board)
        self.assertIn('<span class="next-muted">Now:</span> fastest starts on claude1.', board)
        self.assertIn('<span class="next-muted">Next:</span> report.', board)
        self.assertIn("<summary>All 4 units</summary>", board)
        self.assertIn("praxis did not start on claude1.", board)
        self.assertIn("The retry did not start. Try again.", board)
        self.assertNotIn("Claude gave no prompt in 90 s.", board)
        self.assertIn("praxis is stopped.", board)
        self.assertIn(f'data-retry-move="{move.batch_id}" data-move-member="member-2">Move praxis again', board)
        self.assertIn("The move ended it after 1 min 15 s and moved lfo.", board)

    def test_pruning_keeps_unended_batches_and_the_three_newest_end_marks(self) -> None:
        scratch = Path(self.enterContext(tempfile.TemporaryDirectory()))
        _ = self.enterContext(patch.object(moves, "store_file", return_value=scratch / "moves.jsonl"))
        move = move_progress_fixture()
        owner = moves.ClaimOwner(21, "gone")
        unended = moves.TrackedMove(move, owner, AT)
        moves.write_move_progress(unended)
        finished = moves.MemberMoved(AT, timedelta(), target="claude2")
        for index in range(1, 6):
            at = AT + timedelta(minutes=index)
            earlier = replace(move, batch_id=f"{index:032x}", started=AT - timedelta(days=index),
                              members=(replace(move.members[0], state=finished),))
            ending = moves.TrackedMove(earlier, owner, at, ended=at)
            moves.write_move_progress(ending)
        with patch.object(moves, "_claim_owner_is_live", return_value=False):
            self.assertEqual(len(moves.read_move_progress()), 6)
        moves.finish_move_progress(moves.read_tracked_move(f"{5:032x}"))
        with patch.object(moves, "_claim_owner_is_live", return_value=False):
            records = moves.read_move_progress()
        self.assertEqual([item.batch_id for item in records], [move.batch_id, *(f"{index:032x}" for index in (3, 4, 5))])
        self.assertIsInstance(moves.read_tracked_move(move.batch_id).ended, moves.MoveNotEnded)

    def test_member_destinations_survive_a_new_target_and_unknown_failures_have_no_retry(self) -> None:
        scratch = Path(self.enterContext(tempfile.TemporaryDirectory()))
        _ = self.enterContext(patch.object(moves, "store_file", return_value=scratch / "moves.jsonl"))
        move = move_progress_fixture()
        owner = moves.own_claim_owner()
        failed = moves.MemberMoveFailed(AT, timedelta(), "Fixture target refusal.")
        changed = replace(move, target=moves.MoveDestinationUnselected(),
                          members=(*move.members[:2], replace(move.members[2], state=failed), move.members[3]))
        moves.write_move_progress(moves.TrackedMove(changed, owner, AT))
        current = moves.read_tracked_move(move.batch_id).progress
        self.assertEqual(current.members[0].state, move.members[0].state)
        self.assertEqual(current.members[2].state, failed)
        board = accounts_page.moves_board((current,), AT, ZoneInfo("America/Los_Angeles"), 75)
        self.assertIn("units → destination unavailable", board)
        self.assertIn("moved to claude1", board)
        self.assertIn("fastest did not move.", board)
        self.assertNotIn("Move fastest again", board)

    def test_now_uses_moving_when_the_destination_is_unknown(self) -> None:
        move = move_progress_fixture()
        row = replace(move.members[2], state=moves.MemberMoving(AT))
        move = replace(move, target=moves.MoveDestinationUnselected(), members=(row,))
        board = accounts_page.moves_board((move,), AT, timezone.utc, 75)
        self.assertIn('<span class="next-muted">Now:</span> fastest is moving.', board)

    def test_group_refusal_has_no_unit_details_or_progress_count(self) -> None:
        move = move_progress_fixture()
        refusal = moves.MoveMemberProgress(moves.MoveMember("units", "units"),
            moves.MemberMoveFailed(AT, timedelta(), "Fixture target refused.", "claude1"), moves.MoveMemberKind.REQUEST)
        refused = replace(move, batch_id="e" * 32, members=(refusal,))
        board = accounts_page.moves_board((refused,), AT, timezone.utc, 75)
        self.assertIn("The retry did not start. Try again.", board)
        self.assertNotIn("Fixture target refused.", board)
        self.assertNotIn("All 1 units", board)
        self.assertNotIn('class="mv-bar"', board)
        self.assertNotIn("0 of 1 moved", board)
        self.assertNotIn("1 failed", board)
        archive = accounts_page.moves_board((move, refused), AT, timezone.utc, 75)
        earlier = archive.split("Earlier moves", 1)[1]
        self.assertIn("The retry did not start. Try again.", earlier)
        self.assertNotIn("Fixture target refused.", earlier)
        self.assertNotIn("0 of 1 moved", earlier)
        self.assertNotIn("1 failed", earlier)

    def test_timer_cause_uses_a_safe_display_reason_and_next_counts_waiting_members(self) -> None:
        move = move_progress_fixture()
        move = replace(move, initiator=moves.MoveInitiator.AUTOMATIC,
                       reason=moves.move_display_reason("refusal", "claude3 refused: weekly limit until fixture@example.invalid"),
                       members=(*move.members, moves.MoveMemberProgress(moves.MoveMember("member-5", "second"), moves.MoveQueued())))
        board = accounts_page.moves_board((move,), AT, ZoneInfo("America/Los_Angeles"), 75)
        self.assertIn("automatic · claude3 refused: weekly limit", board)
        self.assertNotIn("fixture@example.invalid", board)
        self.assertIn('<span class="next-muted">Next:</span> report. 1 more units wait.</p>', board)
        self.assertEqual(moves.move_display_reason("auto", "auto: fixture@example.invalid"), "scheduled quota limit")
        self.assertEqual(moves.move_display_reason("auto", "claude3 at 1e-05%"), "claude3 at 1e-05%")
        fable = replace(move, reason=moves.move_display_reason("auto", "claude1 at 20% Fable"))
        board = accounts_page.moves_board((fable,), AT, ZoneInfo("America/Los_Angeles"), 75)
        self.assertIn("automatic · claude1 at 20% Fable", board)
        for suffix in (" fable", "  Fable", " Fable ", " Fable fixture@example.invalid"):
            with self.subTest(suffix=suffix):
                self.assertEqual(moves.move_display_reason("auto", "claude1 at 20%" + suffix), "scheduled quota limit")

    def test_move_failure_text_uses_the_saved_kind_and_reads_old_records(self) -> None:
        scratch = Path(self.enterContext(tempfile.TemporaryDirectory()))
        _ = self.enterContext(patch.object(moves, "store_file", return_value=scratch / "moves.jsonl"))
        move = move_progress_fixture()
        rows: list[moves.MoveMemberProgress] = []
        cases = (
            ("Fixture conflict.", moves.MoveFailureKind.MOVE_IN_PROGRESS,
             "Another move is in progress. Try again when it ends."),
            ("Fixture session missing.", moves.MoveFailureKind.NO_SAVED_SESSION, "fixture &lt;1&gt; has no saved session."),
            ("Another move is in progress.", moves.MoveFailureKind.OTHER, "The retry did not start. Try again."),
            ("Another move is in progress.", None, "Another move is in progress. Try again when it ends."),
            ("No saved session was found.", None, "fixture &lt;4&gt; has no saved session."),
            ("Another operation held this row.", None, "The retry did not start. Try again."),
            ("No saved session was found.", "fixture future kind", "The retry did not start. Try again."),
        )
        for index, (reason, kind, _) in enumerate(cases):
            rows.append(moves.MoveMemberProgress(
                moves.MoveMember(str(index), f"fixture <{index}>"),
                moves.MemberMoveFailed(AT, timedelta(), reason, failure_kind=(
                    kind if isinstance(kind, moves.MoveFailureKind) else moves.MoveFailureKind.OTHER))))
        move = replace(move, members=tuple(rows))
        moves.write_move_progress(moves.TrackedMove(move, moves.ClaimOwner(21, "start"), AT))
        path = scratch / "move-progress" / f"{move.batch_id}.json"
        record = cast(dict[str, list[dict[str, object]]], json.loads(path.read_text()))
        for fields, (_, kind, _) in zip(record["members"], cases):
            if kind is None:
                del fields["failure_kind"]
            elif isinstance(kind, str):
                fields["failure_kind"] = kind
            else:
                self.assertEqual(fields["failure_kind"], kind.value)
        _ = path.write_text(json.dumps(record), encoding="utf-8")
        saved = moves.read_tracked_move(move.batch_id).progress
        with redirect_stderr(io.StringIO()) as errors:
            board = accounts_page.moves_board((saved,), AT, timezone.utc, 75)
            earlier = accounts_page.moves_board((move_progress_fixture(), saved), AT, timezone.utc, 75)
        self.assertIn("Earlier moves", earlier)
        self.assertEqual(errors.getvalue(), "")
        for row, (reason, _, _) in zip(saved.members, cases):
            assert isinstance(row.state, moves.MemberMoveFailed)
            self.assertEqual(row.state.reason, reason)
        lines = [match[1] for match in re.finditer(r'<p class="mv-line mv-fault">(.*?)</p>', board)]
        self.assertEqual(len(lines), len(cases))
        for line, (reason, _, message) in zip(lines, cases):
            self.assertTrue(line.endswith(message), line)
            self.assertNotIn(f"</span> {reason}</p>", line)

    def test_saved_move_uses_plain_labels_for_old_name_and_holder_fallbacks(self) -> None:
        scratch = Path(self.enterContext(tempfile.TemporaryDirectory()))
        _ = self.enterContext(patch.object(moves, "store_file", return_value=scratch / "moves.jsonl"))
        move = move_progress_fixture()
        identity = "c278a59b-8f28-4b73-9e28-32dbfca17648"
        row = moves.MoveMemberProgress(moves.MoveMember(identity, identity),
                                       moves.MemberMoved(AT, timedelta(), "operation", "claude1"))
        noncanonical = identity.upper()
        named = replace(row, member=moves.MoveMember(noncanonical, noncanonical))
        move = replace(move, initiator=moves.HandMoveSession(identity), members=(row, named))
        moves.write_move_progress(moves.TrackedMove(move, moves.ClaimOwner(21, "start"), AT))
        saved = moves.read_tracked_move(move.batch_id).progress
        board = accounts_page.moves_board((saved,), AT, timezone.utc, 75)
        self.assertEqual(saved.members[0].member.reference, identity)
        self.assertNotIn(identity, board)
        self.assertIn(noncanonical, board)
        self.assertIn("by hand · unnamed session", board)
        self.assertIn("a hung task held unnamed session.", board)
        self.assertIn("after a hung task", board)

    def test_completed_moves_show_the_end_and_earlier_origins(self) -> None:
        move = move_progress_fixture()
        ended = tuple(row for row in move.members if isinstance(row.state, (moves.MemberMoved, moves.MemberMoveFailed)))
        finished = replace(move, members=ended)
        earlier = replace(finished, batch_id="a" * 32, group="producers", target="claude2",
                          initiator=moves.MoveInitiator.CONSOLE)
        automatic = replace(earlier, batch_id="b" * 32, group="units", target="claude4",
                            initiator=moves.MoveInitiator.AUTOMATIC, reason="claude3 at 0%")
        zone = ZoneInfo("America/Los_Angeles")
        board = accounts_page.moves_board((finished, earlier, automatic), AT, zone, 75)
        self.assertIn("done Sun 07:01 PST · 1 min", board)
        self.assertIn("<summary>Earlier moves</summary>", board)
        self.assertIn("producers → claude2", board)
        self.assertIn("by hand · console page", board)
        self.assertIn("automatic · claude3 at 0%", board)
        self.assertNotIn(">Now:", board)
        self.assertNotIn(">Next:", board)
        self.assertIn("No move in progress.", accounts_page.moves_board((), AT, zone, 75))


MENU_SETUP = '''<script>
let reads=0;const saves=[];const realFetch=fetch;let freshPage=FRESH_PAGE;
window.fetch=(path,options)=>options?.method==='POST' ? new Promise((resolve,reject)=>saves.push({options,resolve,reject}))
 : (reads++,Promise.resolve({ok:true,text:async()=>freshPage}));
</script>'''

MOVE_DIALOG_PROBE = '''<script>
(async()=>{
 const failures=[], check=(ok,label)=>{if(!ok) failures.push(label);};
 const flush=()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(()=>setTimeout(resolve,0))));
 const dialog=document.querySelector('[data-move-dialog]'), form=dialog.querySelector('form');
 const button=group=>document.querySelector('[data-next-button][data-move-group="'+group+'"]');
 const target=value=>form.querySelector('[name="target"][value="'+value+'"]');
 const measure=value=>form.querySelector('[name="measure"][value="'+value+'"]');
 const percent=form.elements.namedItem('percent'), save=form.querySelector('[data-move-submit]');
 const cancel=form.querySelector('[data-move-cancel]'), now=form.querySelector('[data-move-now]');
 const error=form.querySelector('[data-move-error]');
 let nativeValidation=0;form.addEventListener('invalid',()=>nativeValidation++,true);
 const fields=index=>Object.fromEntries(new URLSearchParams(saves[index].options.body));
 const reply=(index,ok,result)=>saves[index].resolve({ok,json:async()=>result});
 const key=(name,shiftKey=false)=>document.activeElement.dispatchEvent(new KeyboardEvent('keydown',
  {key:name,bubbles:true,cancelable:true,shiftKey}));
 const starts=group=>{button(group).focus();key('Enter');};
 try{
  if(document.readyState!=='complete') await new Promise(resolve=>addEventListener('load',resolve,{once:true}));
  frameElement.style.width='800px';frameElement.style.height='480px';await flush();
  const state=()=>document.querySelector('[data-page-status]').dataset.state;
  starts('units');
  check(dialog.open && button('units').getAttribute('aria-haspopup')==='dialog','Enter opens a dialog');
  check(dialog.querySelector('#move-title').textContent==='Next move of units' &&
   dialog.querySelector('[data-move-current]').textContent==='The units run on claude1 now.','the form names the role and account');
  check(percent.value==='4.5' && measure('fable').checked && target('claude2').checked,'the form reads the units settings');
  check(target('claude2').parentElement.textContent==='claude260% left' &&
   target('claude2').nextElementSibling.style.color==='rgb(178, 148, 187)','a target shows quota and account color');
  check(!target('claude1') && !target('codex1'),'the current and Codex accounts are excluded');
  check(document.activeElement===target('claude2') && state()==='Paused','focus enters the dialog and refresh pauses');
  key('Tab',true);check(document.activeElement===save,'Shift+Tab wraps to Save');
  key('Tab');check(document.activeElement===target('claude2'),'Tab wraps to the selected target');
  target('claude3').click();measure('all-models').click();percent.value='7.5';
  check(saves.length===0,'editing sends no request');
  key('Escape');
  check(!dialog.open && document.activeElement===button('units'),'Escape returns focus to the next cell');
  starts('showrunners');
  check(percent.value==='2' && measure('all-models').checked && target('claude1').checked,'the form reads the other role settings');
  cancel.click();
  check(!dialog.open && saves.length===0 && document.activeElement===button('showrunners'),'Cancel sends no request');
  starts('units');
  check(percent.value==='4.5' && measure('fable').checked && target('claude2').checked,'reopening discards local edits');
  const saveFailure='The settings were not saved. Try again.';
  target('claude3').click();
  for(const invalid of ['101','-1','']){
   percent.value=invalid;save.click();
   check(saves.length===0 && dialog.open && percent.value===invalid && document.activeElement===percent &&
    nativeValidation===0 && !error.hidden && error.textContent===saveFailure,
    'invalid percent shows the form message and keeps the edit: '+invalid);
  }
  percent.value='9.25';save.click();
  check(JSON.stringify(fields(0))===JSON.stringify({group:'units',to:'claude3',percent:'9.25',measure:'fable'}),
   'Save sends the target, percent, and measure');
  const saveReason='The queue is busy.';
  reply(0,false,{reason:saveReason});await flush();
  const moveFailure='The move did not start. Try again.';
  check(dialog.open && !error.hidden && error.textContent===saveReason && percent.value==='9.25',
   'Save failure keeps the dialog and edits and shows the server reason');
  save.click();reply(1,true,{state:'saved'});await flush();await flush();
  check(!dialog.open && document.activeElement===button('units'),'Save closes the dialog and keeps focus across a refresh');
  starts('units');
  target('claude3').click();percent.value='88';measure('all-models').click();now.click();
  check(JSON.stringify(fields(2))===JSON.stringify({group:'units',move_now:'claude3'}),
   'Move now posts only the role and target');
  saves[2].resolve({ok:false,status:409,json:async()=>({state:'move-in-progress',reason:'fixture details'})});
  await flush();
  check(dialog.open && error.textContent==='Another move is in progress. Try again when it ends.',
   'Move now shows the in-progress message');
  now.click();reply(3,false,{reason:'private launcher error'});await flush();
  check(dialog.open && error.textContent===moveFailure,'a launch refusal shows the move message');
  now.click();saves[4].reject(new DOMException('fixture deadline','TimeoutError'));await flush();
  check(dialog.open && error.textContent===moveFailure,'a Move now timeout shows the move message');
  now.click();reply(5,true,{state:'saved'});await flush();await flush();
  starts('units');
  check(percent.value==='4.5' && measure('fable').checked,'Move now did not save the edits');
  target('').click();check(now.disabled && saves.length===6,'No next move disables Move now and sends no request');
  percent.value='';save.click();
  check(JSON.stringify(fields(6))===JSON.stringify({group:'units',clear:'first'}),'Save clears the first entry without percent validation');
  const clearReason='The group has no next move.';
  reply(6,false,{reason:clearReason});await flush();
  check(dialog.open && !error.hidden && error.textContent===clearReason && target('').checked,
   'Clear failure keeps the dialog and selection and shows the server reason');
  save.click();saves[7].resolve({ok:true,json:async()=>{throw new SyntaxError('fixture bad JSON');}});await flush();
  check(dialog.open && error.textContent===saveFailure,'a bad Clear reply shows the save message');
  save.click();saves[8].reject(new TypeError('fixture network error'));await flush();
  check(dialog.open && error.textContent===saveFailure,'a Clear network error shows the save message');
  save.click();
  freshPage=EMPTY_PAGE;
  reply(9,true,{state:'saved'});await flush();await flush();
  starts('units');
  check(percent.value==='3' && measure('all-models').checked && target('').checked && now.disabled,
   'an empty group uses the configured percent and all models');
  const edge=dialog.getBoundingClientRect();
  check(edge.left>=7.5 && edge.top>=7.5 && edge.right<=innerWidth-7.5 && edge.bottom<=innerHeight-7.5,
   'the dialog stays inside the viewport');
  cancel.click();
  freshPage=UNAVAILABLE_PAGE;
  document.dispatchEvent(new Event('visibilitychange'));await flush();await flush();
  starts('units');
  const unavailable=form.querySelector('[data-move-unavailable]');
  check(dialog.open && !unavailable.hidden && unavailable.textContent==='The next moves cannot be read now.',
   'the dialog names an unavailable queue');
  check(save.disabled && now.disabled && percent.matches(':disabled') && document.activeElement===cancel,
   'an unavailable queue disables both actions and focuses Cancel');
  key('Tab');check(document.activeElement===cancel,'focus stays in the unavailable dialog');
  now.click();save.click();check(saves.length===10,'an unavailable queue sends no POST');
  cancel.click();
 }catch(error){failures.push(String(error));}
 await realFetch('/results',{method:'POST',body:JSON.stringify(failures)});
})();</script>'''

class AccountsPageTests(unittest.TestCase):
    review_root: Path = Path()
    reminder_state: Path = Path()

    @override
    def setUp(self) -> None:
        scratch = Path(self.enterContext(tempfile.TemporaryDirectory()))
        use_fixture_history(self, scratch)
        configuration = scratch / "config"
        self.review_root = scratch / "review"
        self.reminder_state = scratch / "login_sittings.json"
        _ = self.enterContext(patch.object(login_sittings, "reminder_state_path", return_value=self.reminder_state))
        _ = self.enterContext(patch.object(remote, "local_machine", return_value="natedev"))
        _ = self.enterContext(patch.object(remote, "sys", SimpleNamespace(platform="linux")))
        _ = self.enterContext(patch.object(usage_store, "store_file", return_value=scratch / "home" / "usage.json"))
        _ = self.enterContext(patch.object(moves, "store_file", return_value=scratch / "moves.jsonl"))
        _ = copy_config_fixture(Path(__file__).resolve().parents[2] / "config", configuration)
        _ = write_config_fixture(configuration / "accounts.conf", ACCOUNT_SETTINGS + LOGIN_CARD_SETTINGS)
        _ = write_config_fixture(configuration / "console.conf", PAGE_STATUS_SETTINGS)
        _ = write_config_fixture(configuration / "review_links.conf", "page_refresh_seconds=15\naccounts_page_seconds=30\n")
        _ = write_config_fixture(configuration / "shutdown.conf", "move.stuck_seconds=75\n")
        _ = (configuration / "statusline.conf").write_text(
            "account.claude1=#af875f\naccount.claude2=#b294bb\naccount.codex2=#123456\nrole.unit-director=#d7af00\n"
            + "model.claude-opus-5-5=#e08a5c\n",
            encoding="utf-8")
        _ = cast(object, self.enterContext(patch.dict(os.environ, {
            "CONF_DIR": str(configuration), "REVIEW_LINKS_ROOT": str(self.review_root),
            "HOME": str(scratch / "fixture-home"), "AGENT_NOTES_DIR": str(scratch / "agents"),
        })))

    def test_page_reads_move_progress_above_accounts_and_uses_the_configured_wait(self) -> None:
        move = move_progress_fixture()
        moves.write_move_progress(moves.TrackedMove(move, moves.own_claim_owner(), AT))
        with (patch.object(accounts, "read_status", return_value=sort_reading()),
              patch.object(shutdown_record, "active", return_value=False),
              patch.object(staff, "local_snapshot", return_value=staff.StaffSnapshot(())),
              patch.object(staff, "row_activity", return_value=staff.RowAvailable())):
            page = accounts_page.page()
        self.assertLess(page.index(b'aria-label="Moves"'), page.index(b'aria-label="Accounts"'))
        self.assertIn(b"The move ended it after 1 min 15 s and moved lfo.", page)

    def test_moves_page_hides_retry_on_the_target_or_while_an_operation_holds_the_member(self) -> None:
        from scripts.agents import inventory

        move = move_progress_fixture()
        identity = "c278a59b-8f28-4b73-9e28-32dbfca17648"
        failed = replace(move.members[1], member=moves.MoveMember(identity, "praxis"))
        move = replace(move, members=(move.members[0], failed, *move.members[2:]))
        moves.write_move_progress(moves.TrackedMove(move, moves.own_claim_owner(), AT))
        cases: tuple[tuple[frozenset[tuple[str, str]], staff.RowAvailable | staff.RowBusy, bool, bool], ...] = (
            (frozenset(), staff.RowAvailable(), False, True),
            (frozenset({(identity, "claude2")}), staff.RowAvailable(), False, True),
            (frozenset({(identity, "claude1")}), staff.RowAvailable(), False, False),
            (frozenset(), staff.RowBusy(), False, False),
            (frozenset(), staff.RowAvailable(), True, False),
        )
        for sessions, activity, claimed, visible in cases:
            reading = replace(sort_reading(), live_claude_accounts=sessions)
            with (self.subTest(activity=activity, claimed=claimed, visible=visible),
                  patch.object(accounts, "read_status", return_value=reading),
                  patch.object(inventory, "listing", side_effect=AssertionError("second session scan")),
                  patch.object(shutdown_record, "active", return_value=False),
                  patch.object(staff, "local_snapshot", return_value=staff.StaffSnapshot(())),
                  patch.object(staff, "row_activity", return_value=activity),
                  patch.object(moves, "move_holds_session", return_value=claimed)):
                page = accounts_page.page()
            self.assertEqual(b"Move praxis again</button>" in page, visible)
            self.assertIn(b"praxis did not start on claude1.", page)

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required")
    def test_retry_posts_one_batch_member_and_refresh_keeps_move_details_open(self) -> None:
        move = move_progress_fixture()
        identity = "c278a59b-8f28-4b73-9e28-32dbfca17648"
        failed = replace(move.members[1], member=moves.MoveMember(identity, "praxis"))
        move = replace(move, members=(move.members[0], failed, *move.members[2:]))
        moves.write_move_progress(moves.TrackedMove(move, moves.own_claim_owner(), AT))
        with (patch.object(accounts, "read_status", return_value=sort_reading()),
              patch.object(shutdown_record, "active", return_value=False),
              patch.object(staff, "local_snapshot", return_value=staff.StaffSnapshot(())),
              patch.object(staff, "row_activity", return_value=staff.RowAvailable())):
            page = accounts_page.page().decode()
        setup = """<script>
const realMoveFetch=fetch, retryPosts=[];
let completeRetry, retryError;
window.fetch=(path,options)=>{
 if(options?.method==='POST' && path===location.pathname){
  if(retryError) return retryError();
  retryPosts.push(Object.fromEntries(new URLSearchParams(options.body)));
  const response=realMoveFetch(path,options);
  return new Promise(resolve=>{completeRetry=()=>resolve(response);});
 }
 return realMoveFetch(path,options);
};
</script>"""
        probe = """<script>
(async()=>{
 const failures=[];
 const check=(good,meaning)=>{if(!good) failures.push(meaning);};
 const details=()=>document.querySelector('.mv-units');
 const before=details();
 before.open=true;
 const button=document.querySelector('[data-retry-move]');
 button.click();
 button.click();
 check(button.disabled && retryPosts.length===1,'send only one retry while it is pending');
 check(retryPosts[0]?.retry_move===before.dataset.moveDetails &&
       retryPosts[0]?.member===button.dataset.moveMember,'post the batch and the failed member');
 const refreshed=new Promise((resolve,reject)=>{
  const observer=new MutationObserver(()=>{if(details()!==before){observer.disconnect();resolve();}});
  observer.observe(document.querySelector('main'),{childList:true});
  setTimeout(()=>{observer.disconnect();reject(new Error('The page did not refresh.'));},4000);
 });
 completeRetry?.();
 try{
  await refreshed;
  check(details().open,'keep All units open');
  const notice=document.querySelector('[data-color-save-error]');
  check(notice.hidden,'the server accepts the retry');
  const retryMessage=()=>new Promise(resolve=>{
   const retry=document.querySelector('[data-retry-move]');
   const observer=new MutationObserver(()=>{
    if(!retry.disabled){observer.disconnect();resolve(notice.textContent);}
   });
   observer.observe(retry,{attributes:true,attributeFilter:['disabled']});
   retry.click();
  });
  const refusal='The retry did not start. Try again.';
  retryError=()=>Promise.resolve(new Response(JSON.stringify({reason:refusal}),{status:503}));
  check(await retryMessage()===refusal,'show the retry failure text');
  retryError=()=>Promise.resolve(new Response(JSON.stringify({reason:'Fixture private diagnostic.'}),{status:400}));
  check(await retryMessage()===refusal,'use the same text for an unexpected JSON reason');
  retryError=()=>Promise.resolve(new Response(JSON.stringify({reason:'Fixture private diagnostic.'}),{status:200}));
  check(await retryMessage()===refusal,'show the failure when a successful HTTP response lacks a state');
  retryError=()=>Promise.resolve(new Response(JSON.stringify({state:'queued'}),{status:200}));
  check(await retryMessage()===refusal,'show the failure when the state is not saved');
  retryError=()=>Promise.reject(new Error('fixture network failure'));
  check(await retryMessage()===refusal,'use the same text after a request failure');
 }catch(error){failures.push(String(error));}
 await realMoveFetch('/results',{method:'POST',body:JSON.stringify(failures)});
})();
</script>"""
        page = page.replace("</head>", setup + "</head>").replace("</body>", probe + "</body>")
        self.review_root.mkdir()
        fixture = self.review_root.parent
        staff_file = fixture / "staff.json"
        _ = staff_file.write_text('{"schema":2,"rows":[]}', encoding="utf-8")
        with (patch.dict(os.environ, {"STAFF_FILE": str(staff_file)}),
              patch.object(serve, "tailscale_ip", return_value="127.0.0.1"),
              patch.object(serve, "loaded_module_stamps", return_value={}),
              patch.object(stop_resume, "shutdown_state_root", return_value=fixture / "shutdown"),
              patch.object(stop_resume, "_detach", return_value=fixture / "move.log") as detach,
              started_review_server(("127.0.0.1", 0), self.review_root) as server):
            worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
            worker.start()

            def post_page(body: bytes) -> tuple[int, bytes]:
                connection = HTTPConnection("127.0.0.1", server.server_port, timeout=TEST_HANG_GUARD_SECS)
                try:
                    connection.request("POST", accounts_page.PAGE_PATH, body, {
                        "Content-Type": "application/x-www-form-urlencoded",
                        "Origin": f"http://127.0.0.1:{server.server_port}",
                    })
                    response = connection.getresponse()
                    return response.status, response.read()
                finally:
                    connection.close()

            try:
                self.assertEqual(firefox_probe_results(page.encode(), post_page=post_page), [[]])
            finally:
                server.shutdown()
                worker.join(TEST_HANG_GUARD_SECS)
            detach.assert_called_once_with(["batch", "--target", "claude1", "--user-command", "--", identity],
                                           "move", initiator=moves.MoveInitiator.CONSOLE)

    def login_card_reading(self) -> accounts.StatusReading:
        urgent = login_sittings.LoginEnd("mac", "claude2", AT + timedelta(days=1, hours=2))
        warning = login_sittings.LoginEnd("natedev", "claude1", AT + timedelta(days=2))
        default = login_sittings.LoginEnd("mac", "claude1", AT + timedelta(days=6))
        next_mac = login_sittings.LoginEnd("mac", "claude3", AT + timedelta(days=9))
        login_sittings.save_state(self.reminder_state, login_sittings.ReminderState(
            sitting=AT, named={default, warning, urgent}, peer=(default, next_mac, urgent)))
        rows = (
            replace(status_row("claude3"), login_ends_at=agent_accounts.LoginEndKnown(AT + timedelta(days=10))),
            replace(status_row("claude1"), login_ends_at=agent_accounts.LoginEndKnown(warning.at)),
        )
        return accounts.StatusReading(rows, (), (), ZoneInfo("America/Los_Angeles"), AT)

    def test_login_card_shows_reported_dates_in_end_order_and_the_following_request(self) -> None:
        reading = self.login_card_reading()
        with (patch.object(remote, "local_machine", return_value="natedev"),
              patch.object(accounts, "read_status", return_value=reading) as status):
            page = accounts_page.page().decode()
        start = page.index('<h2>Logins</h2>')
        card = page[start:page.index("</section>", start)]
        self.assertLess(start, page.index('<h2>Accounts</h2>'))
        self.assertEqual(re.findall(r'<th class="left" scope="col">(.*?)</th>', card),
                         ["account", "machine", "login ends", "action"])
        self.assertEqual(re.findall(r'/login in a [^<]+', card), [
            "/login in a claude2 session on mac",
            "/login in a claude1 session on natedev",
            "/login in a claude1 session on mac",
        ])
        self.assertIn('style="color:#b294bb" data-color-text="account.claude2">claude2</span>', card)
        self.assertIn('Next request: claude3 on mac, Fri 11-20', card)
        self.assertNotIn("fixture@example.invalid", card)
        self.assertNotIn("<button", card)
        status.assert_called_once_with(source=accounts.StoredUsageOnly(), include_models=True)

    def test_login_card_uses_configured_tones_and_the_user_zone(self) -> None:
        reading = self.login_card_reading()
        with patch.object(remote, "local_machine", return_value="natedev"):
            card = accounts_page.logins_board(reading)
        self.assertEqual(re.findall(r'<time datetime="[^"]+"><span class="([^"]+)">([^<]+)</span>', card), [
            ("error", "Mon 11-16 09:00 PST"),
            ("warning", "Tue 11-17 07:00 PST"),
            ("default", "Sat 11-21 07:00 PST"),
        ])

    def test_login_card_removes_only_the_machine_date_that_changes(self) -> None:
        reading = self.login_card_reading()
        original = self.reminder_state.read_bytes()
        renewed = replace(reading.rows[1], login_ends_at=agent_accounts.LoginEndKnown(AT + timedelta(days=30)))
        with patch.object(remote, "local_machine", return_value="natedev"):
            card = accounts_page.logins_board(replace(reading, rows=(reading.rows[0], renewed)))
        self.assertNotIn("/login in a claude1 session on natedev", card)
        self.assertIn("/login in a claude1 session on mac", card)
        self.assertIn("/login in a claude2 session on mac", card)
        self.assertEqual(self.reminder_state.read_bytes(), original)

    def test_login_card_shows_the_empty_line_without_a_table(self) -> None:
        reading = accounts.StatusReading((), (), (), timezone.utc, AT)
        card = accounts_page.logins_board(reading)
        self.assertIn('<p class="lg-next next-muted">No login request.</p>', card)
        self.assertNotIn("<table", card)
        self.assertNotIn("Next request:", card)

    def test_login_card_shows_safety_before_the_next_sitting(self) -> None:
        reading = self.login_card_reading()
        for ends_in, requests_on in ((2, "Sun 11-15"), (5, "Tue 11-17")):
            with self.subTest(ends_in=ends_in):
                row = replace(status_row("claude4"), login_ends_at=agent_accounts.LoginEndKnown(AT + timedelta(days=ends_in)))
                card = accounts_page.logins_board(replace(reading, rows=(*reading.rows, row)))
                self.assertIn(f"Next request: claude4 on natedev, {requests_on}", card)

    def test_login_card_uses_account_label_fallback_for_missing_and_invalid_colors(self) -> None:
        reading = self.login_card_reading()
        with patch.object(accounts, "role_colors", return_value={"account.claude2": "invalid"}):
            card = accounts_page.logins_board(reading)
        self.assertEqual(card.count('<span class="default bold">claude1</span>'), 2)
        self.assertIn('<span class="default bold">claude2</span>', card)
        self.assertNotIn("data-color-text", card)

    def test_mac_login_card_reads_the_local_copy_until_it_is_stale(self) -> None:
        leader_reading = self.login_card_reading()
        state = login_sittings.load_state(self.reminder_state)
        state.local = tuple(login_sittings.LoginEnd("natedev", row.label, row.login_ends_at.at)
                            for row in leader_reading.rows
                            if isinstance(row.login_ends_at, agent_accounts.LoginEndKnown))
        mac_rows = tuple(replace(status_row(end.account), login_ends_at=agent_accounts.LoginEndKnown(end.at))
                         for end in state.peer)
        with (patch.object(remote, "local_machine", return_value="fixture-mac-host"),
              patch.object(remote, "sys", SimpleNamespace(platform="darwin")),
              patch.object(accounts, "read_status", return_value=replace(leader_reading, rows=mac_rows)),
              patch.object(subprocess, "run", side_effect=AssertionError("unexpected process"))):
            self.assertEqual(remote.local_machine(), "fixture-mac-host")
            self.assertEqual(remote.this_machine(), "mac")
            mac_reading = replace(leader_reading, rows=mac_rows)
            self.assertIn("Login requests unavailable.", accounts_page.logins_board(mac_reading))
            copy = login_sittings.ReminderStateCopy("natedev", AT, state)
            self.assertEqual(login_sittings.main(["exchange"], io.StringIO(copy.encode()), io.StringIO(), io.StringIO()), 0)
            card = accounts_page.logins_board(mac_reading)
            self.assertEqual(accounts_page.logins_board(replace(mac_reading, rows=())), card)
            renewed = tuple(replace(row, login_ends_at=agent_accounts.LoginEndKnown(AT + timedelta(days=30)))
                            if row.label == "claude1" else row for row in mac_rows)
            renewed_card = accounts_page.logins_board(replace(mac_reading, rows=renewed))
            self.assertNotIn("/login in a claude1 session on mac", renewed_card)
            self.assertIn("/login in a claude1 session on natedev", renewed_card)
            boundary = AT + timedelta(hours=2, seconds=30)
            boundary_card = accounts_page.logins_board(replace(mac_reading, checked_at=boundary))
            self.assertIn("Next request: claude3 on mac, Fri 11-20", boundary_card)
            self.assertIn("/login in a claude1 session on natedev", boundary_card)
            stale = accounts_page.logins_board(replace(mac_reading, checked_at=boundary + timedelta(seconds=1)))
            self.assertIn("Login requests unavailable.", stale)
            self.assertNotIn("<table", stale)
        self.assertEqual(card, accounts_page.logins_board(leader_reading))

    def test_page_reads_quota_clear_state_from_the_fixture_root(self) -> None:
        reading = accounts.StatusReading((replace(status_row("claude1"), weekly_left_percent=4),),
                                         (), (), timezone.utc, AT)
        with (patch.object(accounts, "read_status", return_value=reading),
              patch.object(accounts_page, "cleared_quotas", return_value={}) as cleared):
            _ = accounts_page.page()
        cleared.assert_called_once_with(accounts_page.quota_state(self.review_root))

    def test_page_shows_token_states_and_renewal_steps_below_models(self) -> None:
        notes = self.review_root.parent / "agents"
        notes.mkdir()
        made = AT - timedelta(days=335)
        _ = (notes / "Mac fallback token.md").write_text(
            f"---\naccount: claude9\nmade: {made.isoformat()}\nchecked_at: {AT.isoformat()}\n"
            + f"last_good_use: {(AT - timedelta(minutes=1)).isoformat()}\n"
            + f"last_login_failure: {AT.isoformat()}\n---\n", encoding="utf-8")
        config = Path(os.environ["CONF_DIR"]) / "showrunners.conf"
        dates = ("token_expiry.lead_days=30 7\n"
                 + "token_expiry.hana_linux_runners.ends=2026-11-22\n"
                 + "token_expiry.hana_macos_runner_query.ends=2026-11-14\n")
        _ = config.write_text("mac_token.account=\n" + dates, encoding="utf-8")
        with (patch.dict(os.environ, {"AGENT_NOTES_DIR": str(notes)}),
              patch.object(accounts, "read_status", return_value=sort_reading())):
            page = accounts_page.page()
            mac_section = page.split(b'<details class="token-steps" data-token="mac_fallback">', 1)[1].split(b"</details>", 1)[0]
            self.assertIn(b"<p>Set <code>mac_token.account</code> first.</p>", mac_section)
            self.assertNotIn(b"<ol>", mac_section)
            _ = config.write_text("mac_token.account=claude2\n" + dates, encoding="utf-8")
            page = accounts_page.page()
        self.assertLess(page.index(b'aria-label="Models"'), page.index(b'aria-label="Tokens"'))
        table = page.split(b'aria-label="Tokens"', 1)[1].split(b"</table>", 1)[0]
        rows = [match[1] for match in re.finditer(rb"<tr>(<td.*?)</tr>", table)]
        self.assertEqual(len(rows), 3)
        for row, expected in zip(rows, (
            (b"Mac fallback token", b"for claude2.", b">2026-12-15</time>", b">30</td>",
             b'<span class="error">Login failed</span><br><span class="warning">Renew soon</span>'),
            (b"hana-linux-runners", b"Registers the Hana Linux CI runners.", b">2026-11-22</time>", b">7</td>",
             b'<span class="warning">Renew soon</span>'),
            (b"hana-macos-runner-query", b"Checks runner availability", b">2026-11-14</time>", b">0</td>",
             b'<span class="error">Expired</span>'),
        )):
            for value in expected:
                self.assertIn(value, row)
        sections = [section.split(b"</details>", 1)[0].decode()
                    for section in page.split(b'<details class="token-steps" ')[1:]]
        self.assertEqual([section.split("<ol>", 1)[0] for section in sections], [
            'data-token="mac_fallback"><summary>Replace the token on the Mac</summary>',
            'data-token="token_expiry.hana_linux_runners.ends"><summary>Renew hana-linux-runners</summary>',
            'data-token="token_expiry.hana_macos_runner_query.ends"><summary>Renew hana-macos-runner-query</summary>',
        ])
        renewal, linux, macos = sections
        steps = [match[1] for match in re.finditer(r"<li>(.*?)</li>", renewal)]
        self.assertEqual(steps, [
            "In a plain Terminal or iTerm window, not in a herdr pane, run <code>mac-token-renew</code>.",
            "Approve access in the browser while signed in to claude2.",
            "Copy the token that setup-token shows, then paste it at the hidden prompt and press Enter.",
        ])
        self.assertNotIn("<pre>", renewal)
        self.assertNotIn('class="token-command"', renewal)
        self.assertEqual(linux.count("<li>"), 5)
        self.assertIn("Keep Administration read and write. Set 366 days.", linux)
        self.assertIn(
            'Run this command in a plain natedev terminal.<code class="token-command" tabindex="0">'
            + "~/.claude/scripts/lib/py ~/.claude/scripts/whoami/hana_runner_token.py reseal</code>", linux)
        self.assertNotIn("systemd-creds", linux)
        self.assertIn("/tune showrunners token_expiry.hana_linux_runners.ends &lt;YYYY-MM-DD&gt;</code>", linux)
        self.assertEqual(macos.count("<li>"), 3)
        self.assertIn("Keep Administration read only.", macos)
        self.assertIn('class="token-command" tabindex="0">gh secret set MACOS_RUNNER_QUERY_TOKEN --repo hanallc/hana</code>', macos)
        self.assertIn("/tune showrunners token_expiry.hana_macos_runner_query.ends &lt;YYYY-MM-DD&gt;</code>", macos)
        self.assertIn(b".token-command{display:block;white-space:pre;overflow-x:auto;", page)
        self.assertIn(b"tbody tr td{border-top:1px solid var(--line)}", page)
        self.assertIn(b".roles-table tbody tr[data-summary] td{border-top:1px solid var(--ink-3)}", page)
        self.assertNotIn(b"tbody tr:last-child td{border-top:", page)

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required for browser behavior")
    def test_open_renewal_steps_stay_open_through_refresh(self) -> None:
        config = Path(os.environ["CONF_DIR"]) / "showrunners.conf"
        _ = config.write_text("mac_token.account=claude2\ntoken_expiry.lead_days=30 7\n"
                              + "token_expiry.hana_linux_runners.ends=2026-11-22\n"
                              + "token_expiry.hana_macos_runner_query.ends=2026-11-14\n", encoding="utf-8")
        with (patch.dict(os.environ, {"AGENT_NOTES_DIR": str(self.review_root.parent / "agents")}),
              patch.object(accounts, "read_status", return_value=sort_reading())):
            content = accounts_page.page()
        rendered = accounts_page.AccountsRender(content, .1, 200).with_recent_times((.1,)).decode()
        setup = '''<script>
Date.now=()=>200000;
const realFetch=fetch;let reads=0;
window.fetch=()=>{reads++;return Promise.resolve({ok:true,text:async()=>FRESH_PAGE});};
</script>'''.replace("FRESH_PAGE", json.dumps(rendered).replace("</", "<\\/"))
        probe = '''<script>
(async()=>{
 const failures=[],check=(ok,label)=>{if(!ok) failures.push(label);};
 const flush=()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
 const steps=()=>[...document.querySelectorAll('.tokens-card details.token-steps')];
 const opened=()=>steps().filter(node=>node.open).map(node=>node.dataset.token).join(',');
 try{
  check(steps().length===3 && opened()==='','each token has closed renewal steps');
  const linux=steps().find(node=>node.dataset.token==='token_expiry.hana_linux_runners.ends');
  const summary=linux.querySelector('summary');
  check(getComputedStyle(summary).cursor==='pointer','the summary shows a pointer');
  check(!linux.querySelector('ol').checkVisibility(),'closed steps are hidden');
  summary.click();await flush();
  check(opened()==='token_expiry.hana_linux_runners.ends' && linux.querySelector('ol').checkVisibility(),
   'a click opens one token');
  document.dispatchEvent(new Event('visibilitychange'));await flush();await flush();
  check(reads>0 && !linux.isConnected,'refresh replaces the steps');
  check(opened()==='token_expiry.hana_linux_runners.ends','refresh keeps the open steps open and the others closed');
  const fresh=()=>steps().find(node=>node.dataset.token==='token_expiry.hana_linux_runners.ends');
  const oldSummary=fresh().querySelector('summary');oldSummary.focus();
  document.dispatchEvent(new Event('visibilitychange'));await flush();await flush();
  check(!oldSummary.isConnected && document.activeElement===fresh().querySelector('summary'),
   'refresh keeps focus on the summary');
  const oldCommand=fresh().querySelector('.token-command');oldCommand.focus();
  document.dispatchEvent(new Event('visibilitychange'));await flush();await flush();
  check(!oldCommand.isConnected && document.activeElement===fresh().querySelector('.token-command'),
   'refresh keeps focus on the command');
 }catch(error){failures.push(String(error));}
 await realFetch('/results',{method:'POST',body:JSON.stringify(failures)});
})();</script>'''
        page = rendered.replace("<script>", setup + "<script>", 1).replace("</body>", probe + "</body>")
        self.assertEqual(firefox_probe_results(page.encode()), [[]])

    def test_page_marks_old_mac_token_metadata_stale(self) -> None:
        notes = self.review_root.parent / "agents"
        notes.mkdir()
        made = AT - timedelta(days=2)
        _ = (notes / "Mac fallback token.md").write_text(
            f"---\nmade: {made.isoformat()}\nchecked_at: {made.isoformat()}\n---\n", encoding="utf-8")
        with (patch.dict(os.environ, {"AGENT_NOTES_DIR": str(notes)}),
              patch.object(accounts, "read_status", return_value=sort_reading())):
            page = accounts_page.page()
        self.assertIn(b">Stale token data</td>", page)

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
        warning = accounts_page.quota_alert_settings().warning

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
            stored_room(login, warning + 1, resets_at)
            self.assertFalse(marker in cache.get())
            self.assertEqual(accounts_page.nav_quota(self.review_root)["level"], accounts_page.QuotaLevel.NONE)

            stored_room(login, warning, resets_at)
            self.assertEqual(accounts_page.nav_quota(self.review_root),
                             {"level": accounts_page.QuotaLevel.WARNING, "name": f"Warning: {warning}% left"})
            reply = cache.get()
            with cache.condition:
                self.assertTrue(cache.condition.wait_for(lambda: not cache.rendering, TEST_HANG_GUARD_SECS))
            self.assertTrue(marker in reply)

    def test_quota_clear_survives_reset_time_drift_until_the_next_week(self) -> None:
        reset = AT + timedelta(days=1, microseconds=299911)
        path = accounts_page.quota_state(self.review_root)
        path.parent.mkdir(parents=True)
        _ = path.write_text(json.dumps({"claude1": {"level": "urgent", "reset": reset.isoformat()}}), encoding="utf-8")
        settings = conf.read(Path(os.environ["CONF_DIR"]) / "review_links.conf")
        urgent = settings.whole("accounts_quota_urgent_percent")
        window = timedelta(seconds=settings.whole("accounts_quota_same_window_seconds"))
        cases = ((reset - timedelta(microseconds=526917), urgent, False),
                 (reset + window - timedelta(seconds=1), urgent, False),
                 (reset + window, urgent, True),
                 (reset + timedelta(days=7), 100, False),
                 (reset + timedelta(days=7), urgent, True))
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
        settings = accounts_page.quota_alert_settings()
        warning, urgent = settings.warning, settings.urgent
        cases = ((warning + 1, ""), (warning, f"Warning: {warning}% left"), (urgent + 1, f"Warning: {urgent + 1}% left"),
                 (urgent, f"Urgent: {urgent}% left"), (0, "Urgent: 0% left"), (accounts.StatusUnavailable(), ""))
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
                                     f"accounts_quota_warning_percent={warning + 1}\n"
                                     + f"accounts_quota_urgent_percent={urgent + 1}\n")
            changed = accounts_page.page().decode()
            self.assertIn(f'aria-label="Warning: {warning + 1}% left"', changed)
            self.assertIn(f'aria-label="Urgent: {urgent + 1}% left"', changed)

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required for browser behavior")
    def test_quota_panel_closes_and_clear_preserves_the_percentage_color(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        reading = accounts.StatusReading((replace(status_row("claude1"), weekly_left_percent=4),),
                                         (), (), timezone.utc, AT)
        with patch.object(accounts, "read_status", return_value=reading), patch.dict(
                os.environ, {"REVIEW_LINKS_ROOT": str(root)}):
            source = accounts_page.page().decode()
        setup = """<script>
const realFetch=window.fetch.bind(window);let clearForm,holdRefresh=false,heldRefresh=null;
// After holdRefresh, the first refresh waits for the probe, and later refreshes never answer.
window.fetch=(url,options)=>options?.method==='POST'
 ? (clearForm=new URLSearchParams(options.body),Promise.resolve({ok:true,json:async()=>({state:'saved'})}))
 : holdRefresh ? new Promise(resolve=>{if(!heldRefresh) heldRefresh=resolve;})
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
  // A refresh that starts while the panel is open must not bring the alert back after the clear.
  const old='<!doctype html>'+document.documentElement.outerHTML;
  button.click();holdRefresh=true;document.dispatchEvent(new Event('visibilitychange'));
  // The fetch stub runs during dispatch. Click Clear before Firefox can resize the frame and close the panel.
  check(heldRefresh,'a refresh is in flight while the panel is open');
  clear.click();
  check(clearForm?.get('quota_account')==='claude1' && clearForm.get('quota_level')==='urgent',
   'Clear posts the account and level');
  await paint();
  heldRefresh?.({ok:true,text:async()=>old});
  for(let wait=0;wait<5;wait++) await paint();
  check(panel.hidden && !button.isConnected,'Clear closes the panel and removes the button');
  check(!document.querySelector('[data-quota-account=claude1]'),'a refresh from before the clear does not bring the alert back');
  check(percent.isConnected && percent.textContent==='4%' && getComputedStyle(percent).color===color,
   'the percentage keeps its color');
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
        self.assertIn('title="Not the selected account for a role"', account_table)
        self.assertIn(page_frame.icon_svg("minus"), account_table)
        self.assertIn('<span class="inactive">no account</span>', roles_table)
        self.assertIn('<tr data-summary><td class="left"><span class="default bold">total</span>', roles_table)
        self.assertIn('<h2>Problems</h2><ul class="problems"><li>&lt;stale&gt;</li></ul>', account_table)
        self.assertNotIn('Upcoming moves:', markup)
        self.assertNotIn('units → claude1', markup)

    def test_account_name_titles_use_disk_logins_for_all_seven_accounts(self) -> None:
        labels = ("claude1", "claude2", "claude3", "claude4", "claude5", "codex1", "codex2")
        rows = tuple(replace(status_row(label), tool="claude" if label.startswith("claude") else "codex",
                             login="" if label == "codex2" else f"{label}&fixture@example.invalid",
                             roles=(), role_counts=()) for label in labels)
        markup = accounts_page.board(accounts.StatusReading(rows, (), (), timezone.utc, AT))
        titles = dict(re.findall(r'<td class="left" data-account-name><span title="([^"]*)"[^>]*>([^<]+)</span>', markup))
        expected = {f"{label}&amp;fixture@example.invalid": label for label in labels[:-1]}
        expected["No email in the login record"] = "codex2"
        self.assertEqual(titles, expected)

    def test_malformed_disk_login_shows_the_fallback_in_the_page(self) -> None:
        directory = self.review_root.parent / ".claude1"
        directory.mkdir()
        _ = (directory / ".claude.json").write_text(json.dumps({
            "oauthAccount": {"emailAddress": "fixture\ud800@example.invalid"},
        }))
        login = agent_accounts.claude_on_disk(directory).email
        self.assertTrue(login and "\ud800" in login)
        row = replace(status_row("claude1"), login=login or "")
        reading = accounts.StatusReading((row,), (), (), timezone.utc, AT)
        with patch.object(accounts, "read_status", return_value=reading):
            page = accounts_page.page()
        self.assertIn(b'<td class="left" data-account-name><span title="No email in the login record"', page)

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required for browser colors")
    def test_model_role_headings_use_the_accounts_role_colors(self) -> None:
        config = Path(os.environ["CONF_DIR"]) / "statusline.conf"
        colors = {"role.producer": "#112233", "role.showrunner": "#223344",
                  "role.unit-director": "#334455", "role.worker": "#445566"}
        _ = config.write_text("".join(f"{key}={value}\n" for key, value in colors.items()))
        reading = sort_reading()
        markup = accounts_page.models_board(reading)
        for role, key in accounts.ROLE_COLOR_KEYS.items():
            with self.subTest(role=role):
                heading = markup.split(f'data-model-sort="{role}"', 1)[1].split("</th>", 1)[0]
                self.assertIn(f'style="color:{colors[key]}" data-color-text="{key}">{role}</span>', heading)
        probe = """<script>
const failures=[];
for(const theme of ['light','dark']){
 document.documentElement.dataset.theme=theme;
 for(const heading of document.querySelectorAll('[data-model-sort] [data-color-text^="role."]')){
  const key=heading.dataset.colorText;
  const cell=document.querySelector('.accounts-table [data-color-text="'+key+'"]');
  if(!cell || getComputedStyle(cell).color!==getComputedStyle(heading).color)
   failures.push(theme+' '+key+' has a different color.');
 }
}
fetch('/results',{method:'POST',body:JSON.stringify(failures)});
</script>"""
        page = ('<!doctype html><html><head>' + accounts_page.STYLE + '</head><body>'
                + accounts_page.board(reading) + markup + probe + '</body></html>').encode()
        self.assertEqual(firefox_probe_results(page), [[]])

    def test_fable_left_and_codex_reset_count_use_stored_values_and_sort_numerically(self) -> None:
        make_row = cast(Callable[..., accounts.StatusRow], getattr(accounts, "_status_row"))
        reset_count = cast(Callable[[str], int | accounts.StatusUnavailable], getattr(accounts, "_codex_limit_reset_count"))
        folder = self.review_root.parent
        claude = accounts.AccountStatus("~/.claude1", folder / ".claude1", True, True, True,
                                        "claude1", "claude1@example.invalid", "", (), (), accounts.McpServerDifferences())
        weekly = usage_store.StoredWindow(left=60, at=AT.isoformat(), resets=(AT + timedelta(days=4)).isoformat())
        fable = usage_store.StoredWindow(left=9, at=AT.isoformat(), resets=(AT + timedelta(days=2)).isoformat())
        rows = [
            make_row(claude, usage_store.StoredUsage(weekly=weekly, weekly_fable=fable), AT, ()),
            make_row(replace(claude, label="claude2"),
                     usage_store.StoredUsage(weekly=weekly, weekly_fable={**fable, "left": 100}), AT, ()),
            make_row(replace(claude, label="claude3"),
                     usage_store.StoredUsage(weekly=weekly, weekly_fable={**fable, "resets": AT.isoformat()}), AT, ()),
        ]
        with patch.object(note_file, "notes_dir", return_value=folder):
            for label, value in (("codex1", "12"), ("codex2", "0"), ("codex3", "-1")):
                _ = (folder / note_file.note_name(label)).write_text(f"---\nlimit_reset_count: {value}\n---\n")
                rows.append(replace(status_row(label), tool="codex", fable_left_percent=25,
                                    limit_reset_count=reset_count(label)))

        markup = accounts_page.board(accounts.StatusReading(tuple(rows), (), (), timezone.utc, AT))
        table = markup.split('aria-label="Accounts"', 1)[1].split("</table>", 1)[0]
        lines = cast(list[str], re.findall(r'<tr[^>]+data-account-row="[^"]+">(.*?)</tr>', table, re.S))
        cells = [cast(list[str], re.findall(r'<td[^>]*>(.*?)</td>', line, re.S)) for line in lines]
        self.assertEqual([row[4] for row in cells[2:]], [accounts_page.DASH, "", "", ""])
        self.assertEqual([re.sub("<[^>]+>", "", row[4]) for row in cells[:2]], ["9%", "100%"])
        self.assertEqual([re.sub("<[^>]+>", "", row[11]) for row in cells], ["—", "—", "—", "12", "0", "—"])
        self.assertIn('title="Claude does not give this count to our tools."', cells[0][11])
        orders = rendered_sort_orders(markup, "accounts")
        self.assertEqual(orders["Fable left"], [0, 1, 2, 3, 4, 5])
        self.assertEqual(orders["Fable left-reverse"], [1, 0, 2, 3, 4, 5])
        self.assertEqual(orders["reset count"], [4, 3, 0, 1, 2, 5])
        self.assertEqual(orders["reset count-reverse"], [3, 4, 0, 1, 2, 5])

    def test_weekly_reset_orange_weight(self) -> None:
        for position, expected in ((0, 0), (0.125, 0.25), (0.25, 0.5), (0.5, 2 / 3), (1, 1)):
            with self.subTest(position=position):
                self.assertAlmostEqual(accounts_page.weekly_reset_orange_weight(position), expected)

    def test_weekly_reset_colors_use_the_stored_codex_work_rule(self) -> None:
        make_row = cast(Callable[..., accounts.StatusRow], getattr(accounts, "_status_row"))
        folder = self.review_root.parent / ".codex1"
        status = accounts.CodexStatus("~/.codex1", folder, True, True, "codex1", "fixture@example.invalid", "", (), ())
        reset = (AT + timedelta(hours=3)).isoformat()
        weekly = usage_store.StoredWindow(left=0, at=AT.isoformat(), resets=reset)
        for weekly_left, five, balance, credits, expected in (
            (0, None, "10", agent_accounts.CreditsOn(), "ready"),
            (0, None, "10", agent_accounts.CreditsOff(), "exhausted"),
            (20, None, "0", agent_accounts.CreditsOff(), "ready"),
            (20, 0, "0", agent_accounts.CreditsOff(), "exhausted"),
            (20, 0, "unlimited", agent_accounts.CreditsOn(), "ready"),
        ):
            with self.subTest(weekly=weekly_left, five=five, credits=credits, expected=expected):
                held = usage_store.StoredUsage(weekly={**weekly, "left": weekly_left},
                                               credits=usage_store.StoredCredits(balance=balance, at=AT.isoformat()))
                if five is not None:
                    held["five_hour"] = {**weekly, "left": five}
                with patch.object(agent_accounts, "codex_credits_setting", return_value=credits):
                    row = make_row(status, held, AT, ())
                reading = accounts.StatusReading((row,), (), (), timezone.utc, AT)
                markup = accounts_page.board(reading)
                self.assertIn(f'class="weekly-reset-{expected}"', markup)
                if expected == "exhausted":
                    self.assertIn('style="--reset-orange-weight:100%"', markup)

    def test_weekly_reset_colors_reject_future_codex_readings(self) -> None:
        make_row = cast(Callable[..., accounts.StatusRow], getattr(accounts, "_status_row"))
        folder = self.review_root.parent / ".codex1"
        status = accounts.CodexStatus("~/.codex1", folder, True, True, "codex1", "fixture@example.invalid", "", (), ())
        at = AT.isoformat()
        future = (AT + timedelta(minutes=30)).isoformat()
        reset = (AT + timedelta(hours=3)).isoformat()
        weekly = usage_store.StoredWindow(left=20, at=at, resets=reset)
        for name, usage in (
            ("weekly", usage_store.StoredUsage(weekly={**weekly, "at": future})),
            ("five-hour", usage_store.StoredUsage(weekly=weekly, five_hour={**weekly, "at": future})),
            ("credits", usage_store.StoredUsage(weekly={**weekly, "left": 0},
                credits=usage_store.StoredCredits(balance="10", at=future))),
        ):
            with (self.subTest(reading=name),
                  patch.object(agent_accounts, "codex_credits_setting", return_value=agent_accounts.CreditsOn())):
                row = make_row(status, usage, AT, ())
                reading = accounts.StatusReading((row,), (), (), timezone.utc, AT)
                self.assertIsInstance(row.codex_work_readiness, agent_accounts.CodexWorkUnknown)
                self.assertIn('class="weekly-reset-unknown"', accounts_page.board(reading))

    def test_unassigned_running_claude_session_keeps_its_weekly_reset_green(self) -> None:
        from scripts.herdr import pane_launch
        from scripts.message import sessions

        root = self.review_root.parent
        directory = root / ".claude1"
        scan = cast(Callable[..., accounts.LocalRoleSessions], getattr(accounts, "_observed_role_sessions"))
        count = cast(Callable[..., tuple[tuple[accounts.StatusRow, ...], tuple[str, ...]]],
                     getattr(accounts, "_count_role_sessions"))
        live = sessions.SessionRecord(pid=100, sessionId="fixture-session", name="Unassigned",
                                      messagingSocketPath="", updatedAt=1, tmux="", formerNames=[], status="idle")
        with (patch.object(sessions, "sessions_dir", return_value=root / "sessions"),
              patch.object(sessions, "read_registry", return_value=sessions.RegistryRead((), ())),
              patch.object(sessions, "running_sessions", return_value=[live]),
              patch.object(pane_launch, "record_directory", return_value=root / "launches"),
              patch.object(staff, "local_snapshot", return_value=staff.StaffSnapshot(())),
              patch.object(accounts, "_session_config_dir", return_value=account.ConfiguredClaudeFolder(directory))):
            observed = scan(accounts.GroupAccounts(), include_workers=False, include_models=False,
                            model_reader=accounts.SessionModelReader())
        row = replace(status_row("claude1"), folder=str(directory), roles=(), role_counts=(), weekly_left_percent=0)
        rows, _ = count((row,), observed)
        self.assertEqual((rows[0].roles, rows[0].role_counts, rows[0].sessions), ((), (), ()))
        reading = accounts.StatusReading(rows, (), (), timezone.utc, AT)
        self.assertIn('class="weekly-reset-ready"', accounts_page.board(reading))

    def test_weekly_reset_scale_excludes_accounts_with_a_role_or_weekly_quota_left(self) -> None:
        exhausted = replace(status_row("claude1"), weekly_left_percent=0, roles=(), role_counts=())
        later = AT + timedelta(days=4)
        for excluded in (
            replace(exhausted, label="claude2", roles=("units",), selected_roles=("units",),
                    role_counts=(accounts.RoleSessionCounts(0, 0, 0, 0),), resets_at=later),
            replace(exhausted, label="codex1", tool="codex", roles=("spare",), weekly_left_percent=20,
                    role_counts=(accounts.NoRoleCount(),), resets_at=later,
                    codex_work_readiness=agent_accounts.CodexWorkExhausted(),
                    codex_weekly_freshness=accounts.CodexWeeklyFreshness.FRESH),
        ):
            with self.subTest(account=excluded.label):
                reading = accounts.StatusReading((exhausted, excluded), (), (), timezone.utc, AT)
                markup = accounts_page.board(reading)
                self.assertEqual(re.findall(r'<span (class="weekly-reset-[^"]+"[^>]*)>', markup),
                                 ['class="weekly-reset-exhausted" style="--reset-orange-weight:100%"'] * 2)

    def test_weekly_reset_scale_end_is_independent_of_codex_work_readiness(self) -> None:
        make_row = cast(Callable[..., accounts.StatusRow], getattr(accounts, "_status_row"))
        folder = self.review_root.parent / ".codex1"
        status = accounts.CodexStatus("~/.codex1", folder, True, True, "codex1", "fixture@example.invalid", "", (), ())
        exhausted = replace(status_row("claude1"), weekly_left_percent=0, roles=(), role_counts=())
        at = AT.isoformat()
        weekly = usage_store.StoredWindow(left=0, at=at, resets=(AT + timedelta(days=4)).isoformat())
        for usage, color in (
            (usage_store.StoredUsage(weekly=weekly), "unknown"),
            (usage_store.StoredUsage(weekly=weekly, credits=usage_store.StoredCredits(balance="10", at=at)), "ready"),
        ):
            with (self.subTest(color=color),
                  patch.object(agent_accounts, "codex_credits_setting", return_value=agent_accounts.CreditsOn())):
                codex = make_row(status, usage, AT, ("spare",))
                reading = accounts.StatusReading((exhausted, codex), (), (), timezone.utc, AT)
                markup = accounts_page.board(reading)
                self.assertEqual(re.findall(r'<span (class="weekly-reset-[^"]+"[^>]*)>', markup), [
                    'class="weekly-reset-exhausted" style="--reset-orange-weight:50%"',
                    f'class="weekly-reset-{color}"',
                ])

    def test_weekly_reset_scale_end_requires_a_fresh_codex_weekly_reading(self) -> None:
        age_limit = timedelta(minutes=7)
        _ = write_config_fixture(conf.path("accounts"), "usage_stale_minutes=7\n")
        make_row = cast(Callable[..., accounts.StatusRow], getattr(accounts, "_status_row"))
        folder = self.review_root.parent / ".codex1"
        status = accounts.CodexStatus("~/.codex1", folder, True, True, "codex1", "fixture@example.invalid", "", (), ())
        exhausted = replace(status_row("claude1"), weekly_left_percent=0, roles=(), role_counts=())
        five = usage_store.StoredWindow(left=40, at=(AT - timedelta(minutes=8)).isoformat(),
                                       resets=(AT + timedelta(hours=2)).isoformat())
        for observed, weight in (
            (AT - age_limit, 50),
            (AT - age_limit - timedelta(seconds=1), 100),
            (AT + timedelta(seconds=1), 100),
        ):
            with (self.subTest(observed=observed),
                  patch.object(agent_accounts, "codex_credits_setting", return_value=agent_accounts.CreditsOff())):
                weekly = usage_store.StoredWindow(left=0, at=observed.isoformat(),
                                                 resets=(AT + timedelta(days=4)).isoformat())
                usage = usage_store.StoredUsage(weekly=weekly, five_hour=five)
                codex = make_row(status, usage, AT, ("spare",))
                reading = accounts.StatusReading((exhausted, codex), (), (), timezone.utc, AT)
                markup = accounts_page.board(reading)
                self.assertEqual(re.findall(r'<span (class="weekly-reset-[^"]+"[^>]*)>', markup), [
                    f'class="weekly-reset-exhausted" style="--reset-orange-weight:{weight}%"',
                    'class="weekly-reset-unknown"',
                ])

    def test_weekly_reset_scale_uses_status_after_usage_store_refresh(self) -> None:
        home = self.review_root.parent / "home"
        configuration = Path(os.environ["CONF_DIR"])
        write_machine_config(configuration)
        claude = accounts.AccountStatus("~/.claude1", home / ".claude1", True, True, True,
                                       "claude1", "fixture-claude@example.invalid", "", (), (),
                                       accounts.McpServerDifferences())
        codex = accounts.CodexStatus("~/.codex2", home / ".codex2", True, True, "codex2",
                                    "fixture-codex@example.invalid", "spare", (), ())
        at = datetime.now(timezone.utc)
        weekly_claude = usage_store.StoredWindow(left=0, at=at.isoformat(),
                                                  resets=(at + timedelta(days=1)).isoformat())
        weekly_codex = usage_store.StoredWindow(left=0, at=at.isoformat(),
                                                 resets=(at + timedelta(days=4)).isoformat())
        claude_key = usage_store.key("Claude", claude.login)
        codex_key = usage_store.key("Codex", codex.login)
        stored = {claude_key: usage_store.StoredUsage(weekly=weekly_claude),
                  codex_key: usage_store.StoredUsage(weekly=weekly_codex)}

        def refresh_store(reading: accounts.StatusReading) -> str:
            refreshed_at = (reading.checked_at + timedelta(seconds=1)).isoformat()
            updated = usage_store.StoredUsage(weekly={**weekly_codex, "at": refreshed_at})
            with usage_store.read_lock():
                usage_store.write({**stored, codex_key: updated})
            return ""

        with (scratch_account_home(home, {"USER_ZONE": "UTC"}, clear=False),
              patch.dict(os.environ, {"CONF_DIR": str(configuration)}),
              patch.object(role_map, "is_role_map_host", return_value=True),
              patch.object(accounts, "account_statuses", return_value=[claude]),
              patch.object(accounts, "codex_statuses", return_value=[codex]),
              patch.object(accounts, "_status_workers_account",
                           return_value=accounts.CodexWorkersAccount("codex1", home / ".codex1")),
              patch.object(accounts, "group_accounts", return_value=accounts.GroupAccounts()),
              patch.object(accounts, "_read_local_role_sessions",
                           return_value=accounts.LocalRoleSessions({}, {}, (), (), (), False, frozenset())),
              patch.object(accounts, "_start_session_counts_fetch", return_value=False),
              patch.object(agent_accounts, "codex_credits_setting", return_value=agent_accounts.CreditsOff()),
              patch.object(usage_store, "load", wraps=usage_store.load) as load,
              patch.object(accounts_page, "logins_board", side_effect=refresh_store) as refresh):
            with usage_store.read_lock():
                usage_store.write(stored)
            page = accounts_page.page()
        refresh.assert_called_once()
        load.assert_called_once_with()
        weights = cast(list[bytes],
                       re.findall(rb'class="weekly-reset-exhausted" style="--reset-orange-weight:([0-9.]+)%"', page))
        self.assertEqual(len(weights), 2)
        self.assertAlmostEqual(float(weights[0]), 50, delta=0.01)
        self.assertEqual(float(weights[1]), 100)

    def test_weekly_reset_colors_when_resets_tie_or_expire(self) -> None:
        exhausted = replace(status_row("claude1"), weekly_left_percent=0, roles=(), role_counts=())
        for days, expected in (
            (1, 'class="weekly-reset-exhausted" style="--reset-orange-weight:100%"'),
            (0, 'class="weekly-reset-unknown"'),
            (-1, 'class="weekly-reset-unknown"'),
        ):
            with self.subTest(days=days):
                first = replace(exhausted, resets_at=AT + timedelta(days=days))
                second = replace(first, label="claude2")
                reading = accounts.StatusReading((first, second), (), (), timezone.utc, AT)
                markup = accounts_page.board(reading)
                self.assertEqual(re.findall(r'<span (class="weekly-reset-[^"]+"[^>]*)>', markup), [expected] * 2)

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required for browser contrast")
    def test_weekly_reset_colors_show_room_running_sessions_and_time_until_reset(self) -> None:
        statusline = Path(os.environ["CONF_DIR"]) / "statusline.conf"
        _ = statusline.write_text(statusline.read_text(encoding="utf-8") + "background.codex-pair=#7896ff\n",
                                  encoding="utf-8")
        exhausted = replace(status_row("claude1"), weekly_left_percent=0, roles=(), role_counts=())
        rows = (
            replace(exhausted, weekly_left_percent=1, resets_at=AT + timedelta(days=30)),
            replace(exhausted, label="claude2", roles=("units",), role_counts=(accounts.RoleSessionCounts(0, 1, 0, 0),)),
            replace(exhausted, label="claude3", resets_at=AT + timedelta(days=1)),
            replace(exhausted, label="claude4", resets_at=AT + timedelta(days=4)),
            replace(exhausted, label="claude5", resets_at=AT + timedelta(days=8)),
            replace(exhausted, label="claude6", resets_at=AT + timedelta(days=13)),
            replace(exhausted, label="codex1", tool="codex", roles=("workers",), selected_roles=("workers",),
                    weekly_left_percent=1, codex_work_readiness=agent_accounts.CodexWorkAvailable()),
            replace(exhausted, label="codex2", tool="codex", roles=("spare",), resets_at=AT + timedelta(days=20),
                    codex_work_readiness=agent_accounts.CodexWorkExhausted(),
                    codex_weekly_freshness=accounts.CodexWeeklyFreshness.FRESH),
            replace(exhausted, label="codex3", tool="codex", weekly_left_percent=accounts.StatusUnavailable()),
        )
        reading = accounts.StatusReading(rows, (), (), timezone.utc, AT, first_next_moves=accounts.FirstNextMoveEntries())
        probe = """<script>
const failures=[], check=(ok,label)=>{if(!ok) failures.push(label);};
const canvas=document.createElement('canvas').getContext('2d',{willReadFrequently:true});
const pixel=(...colors)=>{
 canvas.clearRect(0,0,1,1);
 for(const color of colors){canvas.fillStyle=color;canvas.fillRect(0,0,1,1);}
 return [...canvas.getImageData(0,0,1,1).data].slice(0,3);
};
const luminance=pixel=>{
 const channels=pixel.map(value=>value/255);
 const linear=channels.map(value=>value<=0.04045 ? value/12.92 : ((value+0.055)/1.055)**2.4);
 return linear.reduce((sum,value,index)=>sum+value*[0.2126,0.7152,0.0722][index],0);
};
const contrast=(base,ink)=>(Math.max(base,ink)+0.05)/(Math.min(base,ink)+0.05);
const background=node=>getComputedStyle(node).backgroundColor;
const fill=cell=>pixel(background(cell.closest('.card')),background(cell.closest('tr')),background(cell.parentElement));
try{
 const cells=[...document.querySelectorAll('.accounts-table td > [class^=weekly-reset-]')];
 const ready=cells.filter(cell=>cell.matches('.weekly-reset-ready'));
 const exhausted=cells.filter(cell=>cell.matches('.weekly-reset-exhausted'));
 const labels=spans=>spans.map(cell=>cell.closest('tr').dataset.accountRow).join(',');
 check(labels(ready)==='claude1,claude2,codex1','Accounts with room or a running session use green.');
 check(labels(exhausted)==='claude3,claude4,claude5,claude6,codex2','Exhausted accounts use the gradient.');
 check(labels(cells.filter(cell=>cell.matches('.weekly-reset-unknown')))==='codex3','Unknown keeps the default color.');
 const weightProperty='--reset-orange-weight';
 const weights=exhausted.map(cell=>cell.style.getPropertyValue(weightProperty));
 check(weights.join(',')==='10%,40%,60%,76.666667%,100%',
  'The orange weight follows the time from the reading to the furthest exhausted reset.');
 const paired=cells.filter(cell=>cell.closest('tr').matches('.auto-pair'));
 check(labels(paired)==='codex1,codex2','The pair includes an available account and an exhausted account.');
 for(const theme of ['light','dark']){
  document.documentElement.dataset.theme=theme;
  const inks=cells.map(cell=>getComputedStyle(cell.firstElementChild || cell).color);
  const readyInks=ready.map(cell=>inks[cells.indexOf(cell)]);
  check(new Set(readyInks).size===1 && new Set(inks).size===7,theme+' has green and five distinct gradient colors.');
  check(fill(paired[0]).join(',')!==fill(cells[0]).join(','),theme+' has a tinted pair background.');
  for(const state of ['default','hover','limit']){
   for(const [index,cell] of cells.entries()){
    const row=cell.closest('tr');
    const tint=row.style.backgroundColor;
    if(state==='hover') cell.parentElement.style.backgroundColor='var(--hover)';
    if(state==='limit' && paired.includes(cell)){
     const color=theme==='dark' ? 'white' : 'black';
     row.style.backgroundColor='color-mix(in srgb,'+color+' TINT_PERCENT%,transparent)';
    }
    const base=luminance(fill(cell));
    const ink=luminance(pixel(inks[index]));
    const ratio=contrast(base,ink);
    check(ratio>=4.5,[theme,row.dataset.accountRow,state,ratio.toFixed(2)].join(' '));
    if(cell===paired[1]){
     const savedWeight=cell.style.getPropertyValue(weightProperty);
     for(let weight=0;weight<=100;weight++){
      cell.style.setProperty(weightProperty,weight+'%');
      const gradientInk=luminance(pixel(getComputedStyle(cell.firstElementChild || cell).color));
      const gradientRatio=contrast(base,gradientInk);
      check(gradientRatio>=4.5,[theme,state,weight+'%',gradientRatio.toFixed(2)].join(' '));
     }
     cell.style.setProperty(weightProperty,savedWeight);
    }
    cell.parentElement.style.removeProperty('background-color');
    row.style.backgroundColor=tint;
   }
  }
 }
}catch(error){failures.push(String(error));}
fetch('/results',{method:'POST',body:JSON.stringify(failures)});
</script>""".replace("TINT_PERCENT", str(accounts_page.PAIR_TINT_PERCENT))
        page = ('<!doctype html><html><head>' + accounts_page.STYLE + '</head><body>'
                + accounts_page.board(reading) + probe + '</body></html>').encode()
        self.assertEqual(firefox_probe_results(page), [[]])

    def test_account_and_role_sort_orders_use_full_values_and_keep_missing_values_last(self) -> None:
        reading = sort_reading()
        reading = replace(reading, rows=tuple(
            replace(row, weekly_left_percent=10.1 if row.label == "claude2" else 10.2)
            if row.label in ("claude1", "claude2") else row for row in reading.rows))
        markup = accounts_page.board(reading)
        accounts_order = rendered_sort_orders(markup, "accounts")
        roles_order = rendered_sort_orders(markup, "roles")
        for key, order in {
            "account": [0, 1, 2, 3, 4, 5, 6], "account-reverse": [6, 5, 4, 2, 3, 0, 1],
            "left": [5, 2, 3, 0, 1, 4, 6], "left-reverse": [0, 1, 2, 3, 5, 4, 6],
            "credits": [6, 2, 3, 0, 1, 5, 4], "credits-reverse": [5, 0, 1, 2, 3, 6, 4],
            "resets": [5, 6, 2, 3, 0, 1, 4], "resets-reverse": [0, 1, 2, 3, 6, 5, 4],
            "enabled": [6, 0, 1, 2, 3, 4, 5], "enabled-reverse": [6, 0, 1, 2, 3, 4, 5],
        }.items():
            with self.subTest(key=key):
                self.assertEqual(accounts_order[key], order)
        for key, order in {
            "role": [0, 1, 2, 3, 4, 5], "role-reverse": [5, 4, 3, 0, 1, 2],
            "busy": [0, 1, 2, 3, 4, 5], "busy-reverse": [5, 0, 1, 2, 3, 4],
            "idle": [5, 0, 1, 2, 3, 4], "idle-reverse": [4, 3, 0, 1, 2, 5],
        }.items():
            with self.subTest(key=key):
                self.assertEqual(roles_order[key], order)

    def test_shared_account_renders_one_details_row_and_one_added_role_row(self) -> None:
        rows = (
            replace(status_row("claude1"), roles=("units", "producers"), selected_roles=("units", "producers")),
            replace(status_row("codex1"), tool="codex", roles=("spare",)),
        )
        markup = accounts_page.board(accounts.StatusReading(rows, (), (), timezone.utc, AT))
        table = markup.split('aria-label="Accounts"', 1)[1].split("</table>", 1)[0]
        lines = cast(list[str], re.findall(r'<tr data-account-row="[^"]+">(.*?)</tr>', table, re.S))
        first, added, spare = (cast(list[str], re.findall(r'<td[^>]*>(.*?)</td>', line, re.S)) for line in lines)
        self.assertIn(">claude1</span>", first[1])
        self.assertIn('data-color-text="role.unit-director">units</span>', first[7])
        self.assertIn(">producers</span>", added[7])
        self.assertIn('data-move-account="claude1" data-move-group="producers"', added[9])
        self.assertEqual(added[:7] + added[10:], [""] * 11)
        self.assertIn(">codex1</span>", spare[1])
        self.assertEqual(spare[7:9], ["", ""])
        self.assertNotIn("spare", markup)
        self.assertNotIn("✓", markup)
        orders = rendered_sort_orders(markup, "accounts")
        self.assertEqual(orders["role"], [1, 0, 2])
        self.assertEqual(orders["role-reverse"], [0, 1, 2])

    def test_roles_shows_each_account_and_keeps_the_total(self) -> None:
        rows = (
            replace(status_row("claude1"), roles=("units", "producers"),
                    role_counts=(accounts.RoleSessionCounts(7, 0, 2, 1), accounts.RoleSessionCounts(0, 0, 0, 0)),
                    named_sessions_by_role={"units": (accounts.NamedSession("tile", "paused"),)}),
            replace(status_row("claude2"), selected_roles=("units",),
                    role_counts=(accounts.RoleSessionCounts(2, 3, 0, 4),),
                    named_sessions_by_role={"units": (accounts.NamedSession("report", "exited"),)}),
            replace(status_row("claude3"), roles=("producers",), selected_roles=("producers",),
                    role_counts=(accounts.RoleSessionCounts(8, 0, 0, 0),)),
        )
        reading = accounts.StatusReading(rows, (), (), timezone.utc, AT,
                                         first_next_moves=first_moves(claude_move("units", "claude1")))
        markup = accounts_page.board(reading)
        roles_table = markup.split('<h2>Roles</h2>', 1)[1]
        units, other, _ = roles_table.split(">units</span>", 1)[1].split("</tr>", 2)
        self.assertIn('data-color-text="account.claude2">claude2</span></td>', units)
        self.assertIn('<td class="left next-cell"><span class="default bold" style="color:#af875f" '
                      + 'data-color-text="account.claude1">claude1</span></td>', units)
        self.assertTrue(other.startswith('<tr>' + '<td class="left"></td>' * 3))
        self.assertIn('<span class="default bold" style="color:#af875f">claude1</span>', other)
        self.assertIn('<td class="left next-cell"><span class="next-muted">none</span></td>', other)
        self.assertNotIn("data-color-text", other)
        self.assertNotIn("data-role-counts", units + other)
        self.assertIn("4 not running: 4 exited (report, 3 names unknown)", units)
        self.assertIn("3 not running: 2 paused (tile, 1 name unknown), 1 exited (names unknown)", other)
        for row, expected in ((units, ["2", "3", "9"]), (other, ["7", "0", "10"])):
            self.assertEqual(re.findall(r'<td class="right">(.*?)</td>', NOT_RUNNING_SLOT.sub("", row)), expected)
        total = roles_table.split("<tr data-summary>", 1)[1].split("</tr>", 1)[0]
        self.assertEqual(re.findall(r'<td class="right">(.*?)</td>', NOT_RUNNING_SLOT.sub("", total)),
                         [f'<span class="default">{value}</span>' for value in (17, 3, 27)])
        orders = rendered_sort_orders(markup, "roles")
        self.assertEqual(orders["busy"][:3], [2, 3, 0])
        self.assertEqual(orders["busy-reverse"][:3], [0, 2, 3])
        producers = roles_table.split(">producers</span>", 1)[1].split("</tr>", 1)[0]
        self.assertNotIn("data-role-counts", producers)

    def test_roles_count_sorts_rank_the_whole_role(self) -> None:
        rows = (
            replace(status_row("claude1"), selected_roles=("units",),
                    role_counts=(accounts.RoleSessionCounts(3, 2, 0, 0),)),
            replace(status_row("claude2"), role_counts=(accounts.RoleSessionCounts(20, 12, 0, 0),)),
            replace(status_row("codex1"), tool="codex", roles=("workers",), selected_roles=("workers",),
                    role_counts=(accounts.RoleSessionCounts(18, 10, 0, 0),)),
        )
        markup = accounts_page.board(accounts.StatusReading(rows, (), (), timezone.utc, AT))
        orders = rendered_sort_orders(markup, "roles")
        for column in ("busy", "idle", "total"):
            with self.subTest(column=column):
                self.assertEqual(orders[column], [2, 3, 4, 0, 1])
                self.assertEqual(orders[column + "-reverse"], [4, 2, 3, 0, 1])

    def test_roles_orders_other_accounts_by_count_then_label(self) -> None:
        rows = tuple(replace(status_row(label), selected_roles=("units",) if label == "claude4" else (),
                             role_counts=(accounts.RoleSessionCounts(count, 0, 0, 0),))
                     for label, count in (("claude1", 6), ("claude2", 11), ("claude3", 6),
                                          ("claude4", 0), ("claude5", 0)))
        reading = accounts.StatusReading(rows, (), (), timezone.utc, AT)
        roles = accounts_page.board(reading).split('aria-label="Roles"', 1)[1].split("</table>", 1)[0]
        self.assertEqual(re.findall(r">(claude\d+)</span>", roles), ["claude4", "claude2", "claude1", "claude3"])
        self.assertNotIn("role-count-units-", roles)

    def test_a_role_with_one_account_keeps_its_row(self) -> None:
        reading = accounts.StatusReading((replace(status_row("claude1"), selected_roles=("units",)),), (),
                                         (), timezone.utc, AT, first_next_moves=accounts.FirstNextMoveEntries())
        roles = accounts_page.board(reading).split('aria-label="Roles"', 1)[1].split("</table>", 1)[0]
        units = roles.split(">units</span>", 1)[1].split("</tr>", 1)[0]
        self.assertEqual(roles.count("<tr>"), 5)
        self.assertIn('data-color-text="account.claude1">claude1</span>', units)
        self.assertIn('data-color-key="role.unit-director"', units)
        self.assertIn('aria-label="units busy sessions, last 24 hours"', units)
        self.assertIn('<td class="left next-cell"><span class="next-muted">none</span></td>', units)
        self.assertEqual(re.findall(r'<td class="right">(.*?)</td>', NOT_RUNNING_SLOT.sub("", units)), ["1", "0", "1"])
        self.assertNotIn("data-role-counts", units)

    def test_unassigned_role_keeps_observed_counts(self) -> None:
        reading = accounts.StatusReading((status_row("claude1"),), (), (), timezone.utc, AT)
        roles_table = accounts_page.board(reading).split('aria-label="Roles"', 1)[1]
        units = roles_table.split(">units</span>", 1)[1].split("</tr>", 1)[0]
        cells = re.findall(r'<td class="right" [^>]*data-role-counts="([^"]+)">([0-9]+)</td>', NOT_RUNNING_SLOT.sub("", units))
        self.assertIn('<td class="left"><span class="inactive">no account</span></td>', units)
        self.assertEqual(cells, [(f"claude1: {count}", str(count)) for count in (1, 0, 1)])

    def test_roles_tooltip_names_accounts_with_unknown_counts(self) -> None:
        rows = (
            replace(status_row("claude1"), role_counts=(accounts.UnknownRoleCounts(),)),
            replace(status_row("claude2"), selected_roles=("units",),
                    role_counts=(accounts.RoleSessionCounts(2, 0, 1, 0),)),
        )
        markup = accounts_page.board(accounts.StatusReading(rows, (), (), timezone.utc, AT))
        roles_table = markup.split('aria-label="Roles"', 1)[1]
        primary, other, _ = roles_table.split(">units</span>", 1)[1].split("</tr>", 2)
        self.assertEqual(re.findall(r'<td class="right">(.*?)</td>', NOT_RUNNING_SLOT.sub("", primary)), ["2", "0", "3"])
        cells = re.findall(r'<td class="right" [^>]*data-role-counts="([^"]+)">([^<]*)</td>',
                           NOT_RUNNING_SLOT.sub("", other))
        self.assertEqual(cells, [("claude1: unknown", ""), ("claude1: unknown", ""), ("claude1: unknown", "unknown")])

    def test_roles_next_column_shows_each_target_as_text_without_a_control(self) -> None:
        rows = (
            replace(status_row("claude1"), roles=("showrunners", "units"), selected_roles=("units", "showrunners")),
            replace(status_row("claude2"), roles=("producers",), selected_roles=()),
            replace(status_row("codex1"), tool="codex", roles=("workers",), selected_roles=("workers",)),
        )
        reading = accounts.StatusReading(rows, (), (), timezone.utc, AT,
                                         first_next_moves=first_moves(claude_move("units", "claude2")))
        markup = accounts_page.board(reading)
        account_table, roles_table = markup.split('aria-label="Roles"', 1)
        self.assertIn('data-next-button', account_table)
        self.assertNotIn('data-next-button', roles_table)
        heads = re.findall(r'<th class="[a-z]+"(?: data-role-sort="([^"]*)")?', roles_table.split("</thead>", 1)[0])
        self.assertEqual(heads[:5], ["role", "", "", "account", "next"])
        def next_cell(role: str) -> str:
            row = roles_table.split(f">{role}</span>", 1)[1].split("</tr>", 1)[0]
            match = re.search(r'<td class="left next-cell">(.*?)</td>', row)
            assert match is not None
            return match.group(1)
        self.assertEqual(re.sub(r"<[^>]+>", "", next_cell("units")), "claude2")
        self.assertIn('class="default bold"', next_cell("units"))
        self.assertEqual(re.sub(r"<[^>]+>", "", next_cell("showrunners")), "none")
        self.assertEqual(next_cell("producers"), accounts_page.DASH)
        self.assertEqual(re.sub(r"<[^>]+>", "", next_cell("workers")), "auto")
        orders = rendered_sort_orders(markup, "roles")
        self.assertEqual(orders["next"], [3, 2, 1, 0])

    def test_roles_shows_a_total_without_accounts(self) -> None:
        reading = accounts.StatusReading((), (), (), timezone.utc, AT)
        roles_table = accounts_page.board(reading).split('aria-label="Roles"', 1)[1]
        self.assertEqual(roles_table.count('<span class="inactive">no account</span>'), 4)
        total = roles_table.split('<tr data-summary>', 1)[1].split("</tr>", 1)[0]
        self.assertIn('<span class="default bold">total</span>', total)
        self.assertEqual(re.findall(r'<td class="right">(.*?)</td>', total),
                         ['<span class="default">0</span>'] * 2 + ['<span class="default">0</span><span class="not-running-slot"></span>'])

    def busy_lines(self, samples: list[role_activity.RoleBusySample], zone: ZoneInfo | None = None,
                   now: datetime = AT) -> dict[str, str]:
        """Render the board with the samples in the zone (New York when none) and return each role's busy line SVG."""
        zone = zone or ZoneInfo("America/New_York")
        reading = accounts.StatusReading((status_row("claude1"),), (), (), zone, now)
        with patch.object(role_activity, "read", return_value=samples) as read:
            roles_table = accounts_page.board(reading).split('aria-label="Roles"', 1)[1]
        read.assert_called_once_with(now)
        return {role: line for line, role in cast(list[tuple[str, str]], re.findall(
            r'(<svg class="busy-line" [^>]*aria-label="([a-z]+) busy sessions, last 24 hours">.*?</svg>)', roles_table))}

    def test_roles_table_shows_the_busy_line_column_and_no_paused_or_exited_column(self) -> None:
        roles_table = accounts_page.board(sort_reading()).split('aria-label="Roles"', 1)[1]
        heads = cast(list[str], re.findall(r'<th class="[a-z]+"[^>]*>(.*?)</th>', roles_table.split("</thead>", 1)[0]))
        self.assertEqual([re.sub(r"<[^>]+>|▲", "", head) for head in heads],
                         ["role", "", "busy, last 24 h", "account", "next", "busy", "idle", "total"])
        self.assertNotIn("paused", roles_table.split("</thead>", 1)[0])
        self.assertNotIn("exited", roles_table.split("</thead>", 1)[0])
        rows = cast(list[str], re.findall(r"<tr(?: data-summary)?>(.*?)</tr>", roles_table.split("<tbody>", 1)[1], re.S))
        for row in rows[:-1]:
            cells = cast(list[str], re.findall(r"<td[^>]*>(.*?)</td>", row, re.S))
            self.assertEqual(len(cells), 8)
            if cells[0]:
                self.assertTrue(cells[2].startswith('<svg class="busy-line" width="288" height="35" viewBox="-4 0 296 35" role="img"'))
            else:
                self.assertEqual(cells[:3], ["", "", ""])
        summary = cast(list[str], re.findall(r'<td class="[^"]+">(.*?)</td>', rows[-1], re.S))
        self.assertEqual(len(summary), 8)
        self.assertEqual(summary[1:5], [""] * 4)

    def test_busy_line_joins_close_samples_and_breaks_at_a_missing_role_or_a_time_gap(self) -> None:
        def sample(minutes_ago: int, **busy: int) -> role_activity.RoleBusySample:
            return role_activity.RoleBusySample(AT - timedelta(minutes=minutes_ago), busy)

        def points(line: str) -> list[str]:
            return cast(list[str], re.findall(r'<polyline points="([^"]+)"/>', line))

        # A pixel column is 5 minutes, and a point sits at the end of its column.
        lines = self.busy_lines([sample(6, units=2), sample(0, units=4, workers=1)])
        self.assertEqual(points(lines["units"]), ["287.0,11.0 288.0,1.0"])
        self.assertIn('stroke="#d7af00"', lines["units"])
        # One sample has no line to draw, so the round cap shows it as a dot; the scale is at least 1.
        self.assertEqual(points(lines["workers"]), ["288.0,1.0 288.0,1.0"])
        self.assertNotIn("<polyline", lines["producers"])
        missing = self.busy_lines([sample(25, units=1), sample(17, workers=1), sample(9, units=2), sample(0, units=2)])
        self.assertEqual(points(missing["units"]), ["283.0,11.0 283.0,11.0", "287.0,1.0 288.0,1.0"])
        # Samples at the SAMPLE_GAP limit join; a longer gap breaks the line.
        self.assertEqual(role_activity.SAMPLE_GAP, timedelta(minutes=10))
        late = self.busy_lines([sample(40, units=1), sample(30, units=1), sample(15, units=2)])
        self.assertEqual(points(late["units"]), ["280.0,11.0 282.0,11.0", "285.0,1.0 285.0,1.0"])

    def test_busy_line_keeps_the_largest_count_of_each_pixel_column(self) -> None:
        def sample(minutes_ago: int, units: int) -> role_activity.RoleBusySample:
            return role_activity.RoleBusySample(AT - timedelta(minutes=minutes_ago), {"units": units})

        peak = self.busy_lines([sample(8, 1), sample(6, 0), sample(4, 3), sample(2, 1), sample(0, 2)])
        self.assertEqual(re.findall(r'<polyline points="([^"]+)"/>', peak["units"]), ["287.0,14.3 288.0,1.0"])
        # A sample every two minutes for a full day gives one point for each of the 288 columns.
        day = self.busy_lines([sample(minutes, minutes % 7) for minutes in range(24 * 60, -1, -2)])["units"]
        line = cast(list[str], re.findall(r'<polyline points="([^"]+)"/>', day))
        self.assertEqual(len(line), 1)
        columns = [float(point.split(",")[0]) for point in line[0].split()]
        self.assertEqual(columns, [float(column) for column in range(1, 289)])

    def hour_marks(self, line: str) -> list[tuple[float, str]]:
        """Each hour mark as its x and its label; a tick has no label."""
        return [(float(x), label) for x, label in cast(list[tuple[str, str]], re.findall(
            r'<(?:text|line class="busy-tick") x1?="([0-9.]+)"[^>]*?(?:/>|>([0-9]+)</text>)', line))]

    def test_busy_line_labels_every_third_hour_of_the_zone_and_ticks_the_others(self) -> None:
        for zone, labels in ((ZoneInfo("America/New_York"), ["12", "15", "18", "21", "0", "3", "6", "9"]),
                             (ZoneInfo("UTC"), ["18", "21", "0", "3", "6", "9", "12", "15"])):
            with self.subTest(zone=zone):
                line = self.busy_lines([], zone)["units"]
                self.assertEqual(re.findall(r"<text [^>]*>([0-9]+)</text>", line), labels)
                self.assertEqual(len(re.findall(r'<line class="busy-tick"', line)), 16)
                self.assertIn('<line class="busy-base" x1="0" x2="288" y1="21.5" y2="21.5"/>', line)
                self.assertNotIn("<polyline", line)
        first = self.busy_lines([], ZoneInfo("America/New_York"))["units"]
        # 12:00 EST is two hours after the window start at 10:00 EST.
        self.assertIn('<text x="24.0" y="32">12</text>', first)
        self.assertIn('<line class="busy-tick" x1="12.0" x2="12.0" y1="24" y2="28"/>', first)

    def test_busy_line_marks_follow_the_hour_when_now_is_not_on_the_hour(self) -> None:
        # The window starts at 10:20 EST, so 11:00 is 40 minutes (8 px) in and 12:00 is 20 px in.
        marks = self.hour_marks(self.busy_lines([], ZoneInfo("America/New_York"), AT + timedelta(minutes=20))["units"])
        self.assertEqual(marks[:2], [(8.0, ""), (20.0, "12")])
        self.assertEqual([x for x, _ in marks], [8.0 + 12 * index for index in range(24)])
        self.assertEqual([label for _, label in marks if label], ["12", "15", "18", "21", "0", "3", "6", "9"])

    def test_busy_line_marks_stay_one_hour_apart_on_a_daylight_saving_day(self) -> None:
        zone = ZoneInfo("America/New_York")
        # Each window ends at 12:00 local time on the day the clock changes.
        for now, labels in ((datetime(2026, 11, 1, 12, tzinfo=zone), ["15", "18", "21", "0", "3", "6", "9", "12"]),
                            (datetime(2026, 3, 8, 12, tzinfo=zone), ["12", "15", "18", "21", "0", "3", "6", "9", "12"])):
            with self.subTest(now=now):
                marks = self.hour_marks(self.busy_lines([], zone, now.astimezone(timezone.utc))["units"])
                self.assertEqual([x for x, _ in marks], [12.0 * index for index in range(1, 25)])
                self.assertEqual([label for _, label in marks if label], labels)

    def test_warning_icon_names_paused_and_exited_sessions_and_the_total_sums_them(self) -> None:
        rows = (
            replace(status_row("claude1"), selected_roles=("units",), role_counts=(accounts.RoleSessionCounts(2, 1, 1, 1),),
                    named_sessions_by_role={"units": (accounts.NamedSession("tile", "paused"),
                                                      accounts.NamedSession("fix2", "exited"),
                                                      accounts.NamedSession("busy-unit", "busy"))}),
            replace(status_row("claude2"), roles=("showrunners",), selected_roles=("showrunners",)),
            # The local worker count has no session names.
            replace(status_row("codex1"), tool="codex", roles=("workers",), selected_roles=("workers",),
                    role_counts=(accounts.RoleSessionCounts(0, 0, 2, 0),)),
        )
        roles_table = accounts_page.board(accounts.StatusReading(rows, (), (), timezone.utc, AT)).split('aria-label="Roles"', 1)[1]

        def warning(row: str) -> str:
            match = re.search(r'<span class="not-running" id="([^"]+)" role="img" aria-label="not running" tabindex="0" '
                              + r'aria-describedby="\1-tip" data-tip><svg[^>]*lucide-triangle-alert.*?</svg>\s*</span>'
                              + r'<span id="\1-tip" hidden>([^<]+)</span>', row, re.S)
            assert match is not None
            return html.unescape(match[2])

        def row(role: str) -> str:
            return roles_table.split(f">{role}</span>", 1)[1].split("</tr>", 1)[0]

        self.assertEqual(warning(row("units")), "2 not running: 1 paused (tile), 1 exited (fix2)")
        self.assertEqual(warning(row("workers")), "2 not running: 2 paused (names unknown)")
        for role in ("showrunners", "producers"):
            self.assertNotIn('class="not-running"', row(role))
            self.assertIn('<span class="not-running-slot"></span></td>', row(role))
        total = roles_table.split("<tr data-summary>", 1)[1].split("</tr>", 1)[0]
        self.assertEqual(warning(total), "4 not running: 3 paused (tile, 2 names unknown), 1 exited (fix2)")
        self.assertEqual(roles_table.count('<span class="not-running" id="role-not-running-'), 3)

    def test_codex_pair_rows_carry_the_tint_and_the_hover_text(self) -> None:
        statusline = Path(os.environ["CONF_DIR"]) / "statusline.conf"
        base = statusline.read_text(encoding="utf-8")
        rows = (replace(status_row("claude1"), selected_roles=("units",)),
                replace(status_row("codex1"), tool="codex", roles=("spare",), role_counts=(accounts.NoRoleCount(),),
                        weekly_left_percent=30),
                replace(status_row("codex2"), tool="codex", roles=("workers",), selected_roles=("workers",),
                        weekly_left_percent=20))
        reading = accounts.StatusReading(rows, (), (), timezone.utc, AT, first_next_moves=accounts.FirstNextMoveEntries())

        def left(*percents: float | accounts.StatusUnavailable) -> accounts.StatusReading:
            return replace(reading, rows=(rows[0], *(replace(row, weekly_left_percent=percent)
                                                     for row, percent in zip(rows[1:], percents, strict=True))))

        def pair(reading: accounts.StatusReading, color: str = "#7896ff", *,
                 no_workers: bool = False) -> list[tuple[str, str, str]]:
            _ = statusline.write_text(base + f"background.codex-pair={color}\n", encoding="utf-8")
            with patch.object(quota_state, "no_worker_mode", return_value=no_workers):
                table = accounts_page.board(reading).split('aria-label="Roles"', 1)[0]
            return [(style, label, html.unescape(tip)) for style, label, tip in cast(list[tuple[str, str, str]], re.findall(
                r'<tr class="auto-pair"((?: style="[^"]+")?) data-account-row="([^"]+)">.*?'
                + r'<td class="left next-cell"><span class="next-muted" id="pair-auto-\2" tabindex="0" '
                + r'aria-describedby="pair-auto-\2-tip" data-tip>auto</span><span id="pair-auto-\2-tip" hidden>([^<]+)</span></td>',
                table, re.S))]

        def words(now: str, mode: str) -> str:
            return f"codex1 and codex2 alternate when available. {now}We are in {mode} mode."

        tint = ' style="background:color-mix(in srgb,#7896ff 12%,transparent)"'
        working = words("codex2 is working right now. ", "working workers")
        self.assertEqual(pair(reading), [(tint, "codex1", working), (tint, "codex2", working)])
        # The mode and the quota are separate facts: quota left with no-worker mode on, then no quota with it off.
        self.assertEqual({tip for _, _, tip in pair(reading, no_workers=True)},
                         {words("codex2 is working right now. ", "self-actualizing")})
        self.assertEqual({tip for _, _, tip in pair(left(0, 0))},
                         {words("No account has availability. ", "working workers")})
        # An unknown value makes no claim, so the workers account stays the working account.
        self.assertEqual({tip for _, _, tip in pair(left(accounts.StatusUnavailable("fixture"), 0))}, {working})
        # With no selected workers account, the text leaves out the working sentence and does not fail.
        self.assertEqual(accounts_page._pair_text([replace(row, selected_roles=()) for row in rows[1:]]),  # pyright: ignore[reportPrivateUsage]
                         words("", "working workers"))
        named = ' style="background:color-mix(in srgb,#af87ff 12%,transparent)"'
        self.assertEqual([style for style, _, _ in pair(reading, "purple")], [named, named])
        for invalid in ("#12345", "not-a-color", ""):
            with self.subTest(color=invalid):
                self.assertEqual([style for style, _, _ in pair(reading, invalid)], ["", ""])
        worker_target = next_moves.WorkersToClaude(next_moves.ClaudeWorkersTarget("claude1"))
        targeted = replace(reading, first_next_moves=first_moves(next_moves.WorkersMove("fixture", 1, worker_target, AT)))
        self.assertNotIn("auto-pair", accounts_page.board(targeted))
        self.assertIn("tr.auto-pair+tr.auto-pair td{border-top-color:transparent}", accounts_page.STYLE)

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required for browser behavior")
    def test_warning_icon_and_pair_auto_show_their_text_on_hover_and_focus(self) -> None:
        statusline = Path(os.environ["CONF_DIR"]) / "statusline.conf"
        _ = statusline.write_text(statusline.read_text(encoding="utf-8") + "background.codex-pair=#7896ff\n", encoding="utf-8")
        rows = (replace(status_row("claude1"), selected_roles=("units",), role_counts=(accounts.RoleSessionCounts(3, 0, 1, 0),),
                        named_sessions_by_role={"units": (accounts.NamedSession("tile", "paused"),)}),
                replace(status_row("codex1"), tool="codex", roles=("spare",), role_counts=(accounts.NoRoleCount(),)),
                replace(status_row("codex2"), tool="codex", roles=("workers",), selected_roles=("workers",)))
        reading = accounts.StatusReading(rows, (), (), timezone.utc, AT, first_next_moves=accounts.FirstNextMoveEntries())
        with patch.object(accounts, "read_status", return_value=reading):
            page = accounts_page.page().decode()
        # The refreshed reading drains both Codex accounts, so the pair text changes.
        drained = replace(reading, rows=(rows[0], *(replace(row, weekly_left_percent=0) for row in rows[1:])))
        with patch.object(accounts, "read_status", return_value=drained):
            fresh = accounts_page.AccountsRender(accounts_page.page(), .1, 200).with_recent_times((.1,)).decode()
        setup = MENU_SETUP.replace("FRESH_PAGE", json.dumps(fresh).replace("</", "<\\/"))
        probe = r"""<script>
(async()=>{
const failures=[], check=(ok,label)=>{if(!ok) failures.push(label);};
const flush=()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
try{
 if(document.readyState!=='complete') await new Promise(resolve=>addEventListener('load',resolve,{once:true}));
 frameElement.style.width='1100px';frameElement.style.height='700px';await flush();
 const tooltip=document.querySelector('.model-tooltip'), icon=document.querySelector('#role-not-running-units');
 icon.scrollIntoView({block:'center'});
 icon.dispatchEvent(new PointerEvent('pointerover',{bubbles:true}));
 check(!tooltip.hidden && tooltip.textContent==='1 not running: 1 paused (tile)','hover on the icon shows the names');
 icon.dispatchEvent(new PointerEvent('pointerout',{bubbles:true}));
 check(tooltip.hidden,'leaving the icon hides its tooltip');
 icon.focus({preventScroll:true});icon.dispatchEvent(new FocusEvent('focusin',{bubbles:true}));
 check(document.activeElement===icon && !tooltip.hidden && tooltip.textContent==='1 not running: 1 paused (tile)',
  'focus on the icon shows the same text');
 const description=target=>document.getElementById(target.getAttribute('aria-describedby'));
 check(icon.getAttribute('role')==='img' && icon.getAttribute('aria-label')==='not running' &&
  description(icon)?.hidden && description(icon)?.textContent==='1 not running: 1 paused (tile)',
  'the icon is a named image with the tooltip text as its description');
 const slots=[...document.querySelectorAll('.roles-table .not-running-slot')].map(slot=>slot.getBoundingClientRect().width);
 check(slots.length===5 && slots.every(width=>width===slots[0]),'every total keeps the same slot width');
 const auto=document.querySelector('#pair-auto-codex2');
 auto.scrollIntoView({block:'center'});
 auto.focus({preventScroll:true});auto.dispatchEvent(new FocusEvent('focusin',{bubbles:true}));
 const words='codex1 and codex2 alternate when available. codex2 is working right now. We are in working workers mode.';
 check(!tooltip.hidden && tooltip.textContent===words,'focus on auto shows the pair text');
 check(!auto.hasAttribute('role') && !auto.hasAttribute('aria-label') && description(auto)?.textContent===words,
  'auto keeps its own text and takes the pair text as its description');
 check(getComputedStyle(tooltip).whiteSpace==='normal' && tooltip.getBoundingClientRect().width<=320,
  'the pair text wraps in a narrow tooltip');
 const pairRows=[...document.querySelectorAll('tr.auto-pair')];
 check(pairRows.length===2 && pairRows.every(row=>getComputedStyle(row).backgroundColor!=='rgba(0, 0, 0, 0)'),
  'both pair rows are tinted');
 const between=()=>getComputedStyle(document.querySelector('tr.auto-pair+tr.auto-pair').cells[0]).borderTopColor;
 check(between()==='rgba(0, 0, 0, 0)','no line between the pair rows');
 document.querySelector('[data-account-sort="account"]').click();await flush();
 check(document.querySelectorAll('tr.auto-pair').length===2 && between()==='rgba(0, 0, 0, 0)',
  'the reverse sort keeps the tint and no line between the pair rows');
 const focused=document.querySelector('#pair-auto-codex1');
 focused.scrollIntoView({block:'center'});
 focused.focus({preventScroll:true});focused.dispatchEvent(new FocusEvent('focusin',{bubbles:true}));
 document.dispatchEvent(new Event('visibilitychange'));await flush();
 const restored=document.querySelector('#pair-auto-codex1');
 const drained='codex1 and codex2 alternate when available. No account has availability. We are in working workers mode.';
 check(!focused.isConnected && document.activeElement===restored && !tooltip.hidden && tooltip.textContent===drained,
  'a refresh keeps focus on auto and shows the new pair text');
}catch(error){failures.push(String(error));}
realFetch('/probe',{method:'POST',body:JSON.stringify(failures)});
})();
</script>"""
        page = page.replace("<script>", setup + "<script>", 1).replace("</body>", probe + "</body>")
        self.assertEqual(firefox_probe_results(page.encode()), [[]])

    def test_each_table_has_its_own_default_and_focusable_sort_headers(self) -> None:
        markup = accounts_page.board(sort_reading())
        self.assertEqual(re.findall(r'data-account-sort="([^"]+)"', markup),
                         ["account", "left", "Fable left", "used / hour", "empty at", "role", "next",
                          "resets", "reset count", "credits", "enabled"])
        self.assertEqual(re.findall(r'data-role-sort="([^"]+)"', markup),
                         ["role", "account", "next", "busy", "idle", "total"])
        self.assertEqual(len(re.findall(r'<th [^>]*tabindex="0"', markup)), 17)
        self.assertEqual(markup.count('class="sort-arrow"'), 17)
        self.assertIn('<th class="left">busy, last 24 h</th>', markup)
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
                                         first_next_moves=first_moves(claude_move("units", "claude3")))
        orders = rendered_sort_orders(accounts_page.board(reading), "accounts")

        self.assertEqual(orders["next"], [3, 1, 0, 2])
        self.assertEqual(orders["next-reverse"], [0, 1, 3, 2])

    def test_weekly_forecast_columns_use_recent_history_for_both_tools(self) -> None:
        lookback = 50
        _ = write_config_fixture(conf.path("accounts"), f"rate.lookback_minutes={lookback}\n")

        def history(used: float, rate: float, age_minutes: int = 0) -> list[run_out.Reading]:
            ages = (*range(age_minutes, lookback, 10), lookback, lookback + 10)
            return [run_out.Reading(AT - timedelta(minutes=age),
                                    used - rate * (age - age_minutes) / 60 - max(0, age - lookback) / 10)
                    for age in reversed(ages)]

        rows = (
            status_row("claude1"),
            replace(status_row("claude2"), resets_at=AT + timedelta(hours=3)),
            status_row("claude3"),
            replace(status_row("claude4"), read_at=AT - timedelta(minutes=16)),
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

    def test_weekly_forecast_excludes_history_before_a_reset_and_skips_the_refill_span(self) -> None:
        _ = write_config_fixture(conf.path("accounts"), "rate.lookback_minutes=50\n")
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
        lines = table.split("<tbody>", 1)[1].split("</tr>")[:-1]
        self.assertEqual(lines[0].count('title="No usage rate is available."'), 2)
        self.assertNotIn("<time", lines[0])
        self.assertIn(">6.0%</td>", lines[1])
        self.assertIn("Mon 11-16 07:20 UTC", lines[1])

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required for browser behavior")
    def test_an_open_page_reloads_for_a_newer_script_after_a_visible_edit_or_save_ends(self) -> None:
        _ = write_config_fixture(Path(os.environ["CONF_DIR"]) / "review_links.conf",
                                 "page_refresh_seconds=15\naccounts_page_seconds=30\npage_request_timeout_seconds=5\n")
        with patch.object(accounts, "read_status", return_value=sort_reading()):
            content = accounts_page.page()
        rendered = accounts_page.AccountsRender(content, .1, 200).with_recent_times((.1,)).decode()
        self.assertEqual(rendered.count(f'data-script-version="{accounts_page.SCRIPT_VERSION}"'), 1)
        # A newer server's layout that this script cannot sort: the same entries made the old script throw
        # "insertBefore: parameter 1 is not of type 'Node'" on every update.
        newer = re.sub(r'(data-sort-table="roles" data-sort-default="role" data-sort-orders=")[^"]*"',
                       lambda match: match.group(1) + html.escape(json.dumps({"role": [{"index": 0, "details": True}]})) + '"',
                       rendered.replace(accounts_page.SCRIPT_VERSION, "newer-script"), count=1)
        self.assertNotEqual(newer, rendered.replace(accounts_page.SCRIPT_VERSION, "newer-script"))
        setup = "<script>localStorage.removeItem('roles-sort');Date.now=()=>200000;</script>"
        newer_reply = "()=>Promise.resolve({ok:true,text:async()=>NEWER_PAGE})".replace(
            "NEWER_PAGE", json.dumps(newer).replace("</", "<\\/"))
        # The page has no text field of its own, so the probe adds one.
        place = ("document.querySelector('[data-accounts]').before(document.createElement('textarea'));"
                 + "scrollTo({top:120,behavior:'instant'});")
        page = rendered.replace("<script>", setup + "<script>", 1)
        draft = "type(document.querySelector('textarea'),'typed words');"
        with self.subTest(hold="a visible draft"):
            self.assertEqual(newer_script_reload_results(
                page, newer=newer_reply, place=place, start=draft,
                end="type(document.querySelector('textarea'),'');"), [[]])
        with self.subTest(hold="none, for a hidden draft"):
            self.assertEqual(newer_script_reload_results(
                page, newer=newer_reply, place=place, holds=False,
                start=draft + "document.querySelector('textarea').hidden=true;"), [[]])
        with self.subTest(hold="a color save that never answers"):
            save = """
  const timeout=AbortSignal.timeout, saveDeadline=new AbortController();
  AbortSignal.timeout=()=>saveDeadline.signal;
  try{
   document.querySelector('[data-color-key="account.claude1"]').click();
   type(document.querySelector('#account-color-value'),'#123456');
   document.querySelector('[data-color-picker] [data-color-save]').click();
  }finally{AbortSignal.timeout=timeout;}
  check(!document.querySelector('[data-color-picker]').open,'the save closes the color dialog');
"""
            # Report failures before navigation clears the probe state.
            release = """
  if(failures.length) throw new Error('The save hold checks failed.');
  saveDeadline.abort(new DOMException('The save timed out.','TimeoutError'));
"""
            self.assertEqual(newer_script_reload_results(
                page, newer=newer_reply, place=place, start=save, saves=True, end=release), [[]])

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required for browser behavior")
    def test_sort_controls_reverse_and_persist_through_refresh(self) -> None:
        reading = sort_reading()
        reading = replace(reading, rows=tuple(replace(row, roles=(*row.roles, "units"),
            role_counts=(*row.role_counts, accounts.RoleSessionCounts(9, 0, 0, 0))) if row.label == "claude2"
            else row for row in reading.rows))
        with patch.object(accounts, "read_status", return_value=reading):
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
  .map(row=>row.cells[index].textContent).filter(Boolean).join(',');
 const direction=node=>node.getAttribute('aria-sort');
 const activate=(node,key)=>node.dispatchEvent(new KeyboardEvent('keydown',{key,bubbles:true,cancelable:true}));
 const checkUnits=()=>{
  const units=rows('roles').find(row=>row.cells[0].textContent==='units');
  const other=units?.nextElementSibling;
  check(units?.cells[3].textContent==='claude1' && units.cells[5].textContent==='2' &&
   other?.cells[3].textContent==='claude2' && other.cells[5].textContent==='9' &&
   [...other.cells].slice(0,3).every(cell=>!cell.firstChild),
   'the second units account stays under the selected row');
  check(rows('roles').at(-1).hasAttribute('data-summary'),'the roles summary remains last');
 };
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
  check(labels('accounts',1)===defaultAccounts,'the default shows each account label once');
  checkUnits();
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
  checkUnits();
  for(const name of ['accounts','roles']){
   for(const node of table(name).querySelectorAll('th[aria-sort]')){
    node.click();
    checkUnits();
    const first=direction(node);
    node.click();
    checkUnits();
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
        observed = replace(status_row("claude2"), role_counts=(accounts.RoleSessionCounts(7, 0, 0, 0),))
        reading = accounts.StatusReading((fixture, observed), (), (), timezone.utc, AT)
        with patch.object(accounts, "read_status", return_value=reading):
            page = accounts_page.page().decode()
        refreshed = replace(fixture,
                            role_counts=(accounts.RoleSessionCounts(6, 0, 0, 0), accounts.UnknownRoleCounts()),
                            models_by_role={"units": Counter({earlier: 1, model: 3, high: 2})},
                            model_names_by_role={"units": {**fixture.model_names_by_role["units"],
                                earlier: accounts.ModelSessionNames(["fable-unit"]),
                                high: accounts.ModelSessionNames(["high-unit", "new-unit"])}})
        with patch.object(accounts, "read_status", return_value=replace(reading, rows=(refreshed, observed))):
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
 const busy=document.querySelector('#role-count-workers-busy');
 busy.scrollIntoView({block:'center'});
 busy.focus({preventScroll:true});
 busy.dispatchEvent(new FocusEvent('focusin',{bubbles:true}));
 check(!busy.textContent && !tooltip.hidden && tooltip.textContent==='claude1: unknown',
   'focus explains the missing count on the unassigned role');
 check(getComputedStyle(tooltip).textAlign==='right','account counts align to the right');
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
   .lastElementChild.cells[7].textContent,'both tables show the same total');
  check(rows()[0].cells[4].querySelector('.inactive').textContent==='0','zero has a muted tone');
  check(table().querySelectorAll('[data-color-key="model.claude-opus-5-5"]').length===1,'each model has one swatch');
  check(header('model').getAttribute('aria-sort')==='ascending','model ascending is the default');
  const labelBox=span=>box(span.querySelector('span:not(.sort-arrow)') ??
   [...span.childNodes].find(node=>node.nodeType===Node.TEXT_NODE));
  const layout=key=>{
   const cell=header(key), right=cell.classList.contains('right');
   const label=labelBox(cell.querySelector('.sort-label'));
   const value=box(rows().map(row=>row.cells[cell.cellIndex]).find(item=>item.textContent));
   const arrow=cell.querySelector('.sort-arrow').getBoundingClientRect();
   const edge=right ? 'right' : 'left';
   check(Math.abs(label[edge]-value[edge])<=1,key+' header aligns with the values');
   check(right ? arrow.right<label.left : arrow.left>label.right,key+' arrow is on the correct side');
   const labels=[...table().querySelectorAll('.sort-label')].map(labelBox);
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
                          first_next_moves=first_moves(claude_move("units", "claude2"),
                                                      claude_move("showrunners", "claude3")))
        with patch.object(accounts, "read_status", return_value=reading):
            rendered = accounts_page.AccountsRender(accounts_page.page(), .1, 200).with_recent_times((.1,)).decode()
        fresh = replace(reading, first_next_moves=first_moves(claude_move("units", "claude3")))
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
 const table=()=>document.querySelector('[data-sort-table=accounts] table');
 const rows=()=>[...table().tBodies[0].rows];
 const next=row=>row.querySelector('.next-cell');
 const target=row=>next(row).textContent.trim();
 const group=name=>table().querySelector('[data-move-account="claude1"][data-move-group="'+name+'"]').closest('tr');
 const flush=()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
 try{
  const original=rows(), cells=new Map(original.map(row=>[row,next(row)]));
  const units=group('units'), showrunners=group('showrunners');
  check(target(units)==='claude2' && target(showrunners)==='claude3','the two roles have distinct next targets');
  const nextHeader=table().querySelector('[data-account-sort="next"]');
  const details=row=>[...row.cells].filter(cell=>cell.cellIndex<7 || cell.cellIndex>9).map(cell=>cell.innerHTML);
  const shown=details(units);
  const empty=Array(shown.length).fill('');
  check(JSON.stringify(details(showrunners))===JSON.stringify(empty),'the added row starts with no account data');
  const button=next(units).querySelector('[data-next-button]');
  button.click();
  const menu=document.querySelector('[data-move-dialog]');
  check(menu.open && nextHeader.getAttribute('aria-sort')==='none'
   && rows().every((row,index)=>row===original[index]),'the next button preserves the sort');
  menu.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true,cancelable:true}));
  nextHeader.click();
  check(nextHeader.tabIndex===0 && nextHeader.getAttribute('aria-sort')==='ascending'
   && rows()[0]===units && rows()[1]===showrunners,'the next header sorts targets ascending');
  nextHeader.click();
  check(nextHeader.getAttribute('aria-sort')==='descending' && rows()[0]===showrunners && rows()[1]===units,
   'the next header reverses the target order');
  check(JSON.stringify(details(showrunners))===JSON.stringify(shown) &&
   JSON.stringify(details(units))===JSON.stringify(empty),'the account data moves to the first row of the group');
  const markup=new Map(original.map(row=>[row,next(row)?.innerHTML]));
  for(const column of ['account','role','next','left','Fable left','used / hour','empty at','resets','reset count','credits','enabled']){
   const header=table().querySelector('[data-account-sort="'+column+'"]');
   for(let direction=0;direction<2;direction++){
    header.click();
    check(rows().every(row=>next(row)===cells.get(row) && next(row)?.innerHTML===markup.get(row)),
     column+' keeps the next cells on their rows in direction '+direction);
   }
  }
  table().querySelector('[data-account-sort="role"]').click();
  check(Math.abs(rows().indexOf(units)-rows().indexOf(showrunners))>1 &&
   [units,showrunners].every(row=>JSON.stringify(details(row))===JSON.stringify(shown)),
   'separated groups each show the account details');
  const before=rows();
  document.dispatchEvent(new Event('visibilitychange'));await flush();await flush();
  check(rows()[0]!==before[0] && table().querySelector('[data-account-sort="role"]').getAttribute('aria-sort')==='ascending',
   'refresh replaces the rows and keeps the role sort');
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
        for saved, denied in (("left", False), ("resets", False), ("unknown", False), ("left", True)):
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
 const key=EXPECTED_COLUMN;
 check(selected?.dataset.accountSort===key,'the stored choice or default renders');
 if(key==='resets') check(selected.textContent.includes('weekly reset'),'the saved reset sort still uses the date');
 document.querySelector('[data-role-sort="busy"]').click();
 check(document.querySelector('[data-role-sort="busy"]').getAttribute('aria-sort')==='descending',
  'sorting works when the storage write fails');
}catch(error){failures.push(String(error));}
realFetch('/results',{method:'POST',body:JSON.stringify(failures)});
</script>'''.replace("EXPECTED_COLUMN", json.dumps(saved if saved in ("left", "resets") and not denied else "account"))
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
        self.assertEqual(markup.count("data-account-name"), 4)
        self.assertEqual(markup.count(">producers</span>"), 3)

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
                    self.assertIn(f'<span title="fixture@example.invalid" class="default bold"{style}>claude1</span>', markup)
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
        self.assertIn(f'data-move-targets="{html.escape(targets, quote=True)}"', markup)
        settings = Path(os.environ["CONF_DIR"]) / "accounts.conf"
        _ = write_config_fixture(settings, "next_move_percent=2.5\n")
        self.assertIn('data-move-percent="2.5"', accounts_page.board(reading))

    def test_selected_claude_producers_have_an_editable_next_cell(self) -> None:
        rows = (replace(status_row("claude1"), roles=("producers",), selected_roles=("producers",)),
                status_row("claude2"),
                replace(status_row("codex1"), tool="codex", roles=("producers",)))
        reading = accounts.StatusReading(rows, (), (), timezone.utc, AT,
            first_next_moves=first_moves(claude_move("producers", "claude2")))
        markup = accounts_page.board(reading)
        self.assertIn('data-move-account="claude1" data-move-group="producers"', markup)
        self.assertIn('data-next-target="claude2" data-move-account="claude1" data-move-group="producers"', markup)
        self.assertEqual(markup.count("data-move-group="), 1)

    def test_inherited_claude_producers_have_an_editable_next_cell(self) -> None:
        row = replace(status_row("claude1"), roles=("showrunners", "producers"),
                      selected_roles=("showrunners", "producers"), inherited_roles=("producers",))
        reading = accounts.StatusReading((row,), (), (), timezone.utc, AT,
                                         first_next_moves=accounts.FirstNextMoveEntries())
        markup = accounts_page.board(reading)
        self.assertIn('data-move-account="claude1" data-move-group="producers"', markup)
        self.assertIn('> (inherited)</span>', markup)
        self.assertIn('data-next-target="" data-move-account="claude1" data-move-group="producers"', markup)

    def test_next_cells_show_first_targets_empty_groups_unselected_roles_and_unavailable_reads(self) -> None:
        rows = (replace(status_row("claude1"), roles=("units", "showrunners"), selected_roles=("units", "showrunners")),
                status_row("claude2"), replace(status_row("claude3"), roles=("spare",)),
                replace(status_row("codex1"), tool="codex", roles=("workers",), selected_roles=("workers",)),
                replace(status_row("codex2"), tool="codex", roles=("spare",)))
        worker_move = next_moves.WorkersMove("workers-fixture", 1, next_moves.CodexWorkersTarget("codex2"), AT)
        for first in (first_moves(claude_move("units", "claude2"), worker_move),
                      accounts.FirstNextMoveEntries(), accounts.NextMovesUnavailable("fixture damaged log")):
            with self.subTest(first=first):
                markup = accounts_page.board(accounts.StatusReading(rows, (), (), timezone.utc, AT, first_next_moves=first))
                account_table, roles_table = markup.split('aria-label="Roles"', 1)
                role_cells = cast(list[str], re.findall(r'<td class="left next-cell">(.*?)</td>', roles_table, re.S))
                self.assertFalse(any('<button' in cell for cell in role_cells))
                if isinstance(first, accounts.NextMovesUnavailable):
                    self.assertEqual(sum('>unavailable</span>' in cell for cell in role_cells), 3)
                cells = cast(list[str], re.findall(r'<td class="left next-cell">(.*?)</td>', account_table, re.S))
                self.assertEqual(len(cells), 6)
                units, showrunners, unselected, spare, workers, codex_spare = cells
                # With no Claude target for workers, the spare Codex account takes workers in turn and reads auto.
                workers_auto = isinstance(first, accounts.FirstNextMoveEntries) and "workers" not in first.entries
                for cell in (unselected, spare, *(() if workers_auto else (codex_spare,))):
                    self.assertIn('title="Not the selected account for a role"', cell)
                    self.assertIn('lucide-minus', cell)
                for cell, group in ((units, "units"), (showrunners, "showrunners")):
                    self.assertIn(f'aria-label="Choose the next account for {group}"', cell)
                    self.assertIn('aria-haspopup="dialog" aria-expanded="false"', cell)
                    self.assertIn('lucide-chevron-down', cell)
                self.assertNotIn('<button', workers)
                if isinstance(first, accounts.NextMovesUnavailable):
                    for cell in (showrunners, units, workers):
                        self.assertIn('>unavailable</span>', cell)
                        self.assertIn('fixture damaged log', cell)
                else:
                    self.assertIn('>none</span>', showrunners)
                    if first.entries:
                        self.assertIn('data-color-text="account.claude2">claude2</span>', units)
                        self.assertIn('style="color:#123456" data-color-text="account.codex2">codex2</span>', workers)
                    else:
                        self.assertIn('>none</span>', units)
                        for cell in (workers, codex_spare):
                            self.assertIn('hidden>codex1 and codex2 alternate when available.', cell)
                            self.assertIn('>auto</span>', cell)

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required for browser contrast")
    def test_muted_next_cells_meet_the_contrast_floors_in_both_themes(self) -> None:
        rows = (replace(status_row("claude1"), selected_roles=("units",)), status_row("claude2"),
                replace(status_row("codex1"), tool="codex", roles=("workers",), selected_roles=("workers",)))
        reading = accounts.StatusReading(rows, (), (), timezone.utc, AT, first_next_moves=accounts.FirstNextMoveEntries())
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

    def test_next_move_save_checks_the_group_and_account_then_sets_the_submitted_threshold(self) -> None:
        with patch.object(next_moves, "set_first_next_move") as setter:
            self.assertEqual(accounts_page.save_next_move_settings("workers", "claude2", "2.5", "fable"),
                             accounts_page.Refused("Choose units, showrunners, or producers."))
            with patch.object(accounts, "claude_account_dir", side_effect=accounts.AccountsError("accounts: claude9 has no note")):
                self.assertEqual(accounts_page.save_next_move_settings("units", "claude9", "2.5", "fable"),
                                 accounts_page.Refused("accounts: claude9 has no note"))
            setter.assert_not_called()
        with patch.object(accounts, "claude_account_dir", return_value=Path("/nonexistent/claude2")):
            with patch.object(next_moves, "set_first_next_move") as setter:
                self.assertEqual(accounts_page.save_next_move_settings("showrunners", "claude2", "2.5", "fable"),
                                 accounts_page.Saved())
                setter.assert_called_once_with("showrunners", 2.5, "claude2", measure=next_moves.QuotaMeasure.FABLE)
            with patch.object(next_moves, "set_first_next_move", side_effect=next_moves.NextMoveError("next: cannot set entry: busy")):
                self.assertEqual(accounts_page.save_next_move_settings("units", "claude2", "2.5", "fable"),
                                 accounts_page.SaveFailed("next: cannot set entry: busy"))

    def test_move_now_leaves_the_current_targets_next_move_in_the_log(self) -> None:
        home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        output = home / "move.log"
        with (scratch_account_home(home, {}, clear=True),
              patch.object(role_map, "is_role_map_host", return_value=True),
              patch.object(accounts, "claude_account_dir", return_value=home / ".claude2"),
              patch.object(stop_resume, "detach", return_value=output) as detach):
            _ = next_moves.add_next_move("units", 1, "claude2")
            _ = next_moves.add_next_move("units", 2, "claude3")
            before = next_moves.next_moves_file().read_bytes()
            with patch.object(accounts, "move_group") as move:
                self.assertEqual(accounts_page.move_group_now("units", "claude2"), accounts_page.Saved())
                move.assert_not_called()
            detach.assert_called_once_with(["console-group-move", "units", "claude2"], "move",
                                           program=Path(accounts.__file__), initiator=moves.MoveInitiator.CONSOLE)
            self.assertEqual(next_moves.next_moves_file().read_bytes(), before)

            destination = accounts.MoveTargetReady(accounts.NamedAccount("claude2", home / ".claude2"), ())
            members = accounts.GroupMoveMembers((), (), False, frozenset())
            with (patch.object(accounts, "check_move_target", return_value=destination),
                  patch.object(accounts, "group_transition", return_value=accounts.NoGroupTransition()),
                  patch.object(accounts, "group_move_members", return_value=members),
                  patch.object(accounts, "set_group_account") as select):
                self.assertEqual(accounts.main(["console-group-move", "--log", str(output),
                                                "--requester", "", "units", "claude2"]), 0)
            select.assert_called_once_with("units", "claude2", why="units moved by hand",
                                           keep_claim=ANY, reuse_current=True, prepare=ANY)
            self.assertEqual(next_moves.next_moves_file().read_bytes(), before)

    def test_two_detached_group_starts_refuse_the_second_before_selection(self) -> None:
        home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        first_log, second_log = home / "first.log", home / "second.log"
        starts: list[list[str]] = []

        def detach(arguments: list[str], _task: str, *, program: Path, initiator: moves.MoveInitiator) -> Path:
            self.assertEqual(program, Path(accounts.__file__))
            self.assertIs(initiator, moves.MoveInitiator.CONSOLE)
            output = first_log if not starts else second_log
            starts.append([arguments[0], "--log", str(output), *arguments[1:]])
            return output

        write = accounts.write_role_map

        def start_second(change: role_map.RoleMapChange | role_map.WorkersClaudeRouteChange) -> None:
            self.assertFalse(accounts.groups_file().exists())
            self.assertEqual(moves.read_move_progress(), ())
            self.assertEqual(accounts.main(starts[1]), 1)
            self.assertFalse(accounts.groups_file().exists())
            write(change)

        destination = accounts.MoveTargetReady(accounts.NamedAccount("claude2", home / ".claude2"), ())
        members = accounts.GroupMoveMembers((), (), False, frozenset())
        with (scratch_account_home(home, {}, clear=True),
              patch.object(role_map, "is_role_map_host", return_value=True),
              patch.object(accounts, "claude_account_dir", return_value=home / ".claude2"),
              patch.object(accounts, "check_move_target", return_value=destination) as check,
              patch.object(accounts, "group_move_members", return_value=members),
              patch.object(accounts, "write_role_map", side_effect=start_second) as selecting,
              patch.object(stop_resume, "detach", side_effect=detach)):
            self.assertEqual(accounts_page.move_group_now("units", "claude2"), accounts_page.Saved())
            self.assertEqual(accounts_page.move_group_now("units", "claude3"), accounts_page.Saved())
            self.assertEqual(len(starts), 2)
            self.assertEqual(accounts.main(starts[0]), 0)
            self.assertEqual(accounts.current_move_account("units"), "claude2")
            check.assert_called_once()
            selecting.assert_called_once()

            refused, = moves.read_move_progress()
            self.assertEqual(refused.target, "claude3")
            self.assertIs(refused.initiator, moves.MoveInitiator.CONSOLE)
            reason = "Another move is in progress. Try again when it ends."
            self.assertEqual(second_log.read_text(), reason + "\n")
            card = accounts_page.moves_board((refused,), AT, timezone.utc)
            self.assertIn(reason, card)

    def test_dialog_posts_save_clear_and_hand_move_through_the_accounts_route(self) -> None:
        home = Path(self.enterContext(tempfile.TemporaryDirectory()))

        def account_home(label: str) -> Path:
            return home / f".{accounts.numbered_account_label(label, 'claude').label}"

        class AccountsPostServer(ThreadingHTTPServer):
            bound_address: str = "127.0.0.1"
            mode: serve.ServerMode = serve.ServerMode.LIVE
            accounts_cache: serve.AccountsPageCache = serve.AccountsPageCache()

            def request_tailscale_address(self) -> str:
                return self.bound_address

        with (scratch_account_home(home, {}, clear=True),
              patch.object(role_map, "is_role_map_host", return_value=True),
              patch.object(accounts, "claude_account_dir", new=account_home),
              AccountsPostServer(("127.0.0.1", 0), serve.ReviewLinksHandler) as server):
            original = next_moves.add_next_move("units", 4.5, "claude4", next_moves.HalfPointHold(),
                                                fallback_targets=("claude2", "claude3"))
            assert isinstance(original, next_moves.ClaudeGroupMove)
            later = next_moves.add_next_move("units", 2, "claude3")
            other = next_moves.add_next_move("showrunners", 3, "claude2")
            worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
            worker.start()

            def post(fields: dict[str, str], *, origin: str = "same") -> tuple[int, bytes]:
                headers = {"Content-Type": "application/x-www-form-urlencoded"}
                if origin:
                    headers["Origin"] = f"http://127.0.0.1:{server.server_port}" if origin == "same" else origin
                connection = HTTPConnection("127.0.0.1", server.server_port, timeout=TEST_HANG_GUARD_SECS)
                try:
                    connection.request("POST", accounts_page.PAGE_PATH, urlencode({"group": "units", **fields}), headers)
                    response = connection.getresponse()
                    return response.status, response.read()
                finally:
                    connection.close()

            try:
                settings = {"to": "claude4", "percent": "4.5", "measure": "fable"}
                self.assertEqual(post(settings)[0], 200)
                first, second, third = next_moves.read_next_moves()
                assert isinstance(first, next_moves.ClaudeGroupMove)
                self.assertNotEqual(first.id, original.id)
                self.assertEqual(first, replace(original, id=first.id, measure=next_moves.QuotaMeasure.FABLE))
                self.assertEqual((second, third), (later, other))
                with redirect_stderr(io.StringIO()) as errors:
                    for invalid, reason in (
                        ({"percent": "nan"}, "next: percent must be from 0 to 100"),
                        ({"measure": "weekly"}, "next: measure must be all-models or fable"),
                    ):
                        status, body = post({**settings, **invalid})
                        self.assertEqual((status, json.loads(body)), (400, {"reason": reason}))
                        self.assertEqual(errors.getvalue().count(reason), 1)
                self.assertEqual(post({**settings, "move_now": "claude4"})[0], 400)
                self.assertEqual(post({"to": "claude4"})[0], 400)
                before = next_moves.next_moves_file().read_bytes()
                action = {"move_now": "claude4"}
                with patch.object(stop_resume, "detach", return_value=home / "move.log") as detach:
                    self.assertEqual(post(action, origin="")[0], 403)
                    self.assertEqual(post(action, origin="http://other.invalid")[0], 403)
                    with patch.object(moves, "read_move_progress", return_value=(move_progress_fixture(),)):
                        status, body = post(action)
                    self.assertEqual(status, 409)
                    self.assertEqual(json.loads(body)["state"], "move-in-progress")
                    detach.assert_not_called()
                    self.assertEqual(post(action)[0], 200)
                    detach.assert_called_once_with(["console-group-move", "units", "claude4"], "move",
                                                   program=Path(accounts.__file__), initiator=moves.MoveInitiator.CONSOLE)
                with (patch.object(stop_resume, "detach", side_effect=OSError("fixture launch failure")),
                      redirect_stderr(io.StringIO()) as errors):
                    status, body = post(action)
                    self.assertEqual((status, json.loads(body)), (503, {}))
                    self.assertEqual(errors.getvalue().count("fixture launch failure"), 1)
                self.assertEqual(next_moves.next_moves_file().read_bytes(), before)
                self.assertEqual(post({"clear": "first"})[0], 200)
                status, body = post({"group": "producers", "clear": "first"})
                self.assertEqual((status, json.loads(body)), (400, {"reason": "The group has no next move."}))
                self.assertEqual(next_moves.read_next_moves(), (later, other))
            finally:
                server.shutdown()
                worker.join(TEST_HANG_GUARD_SECS)

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required for browser behavior")
    def test_dialog_edits_each_role_and_contains_the_keyboard_focus(self) -> None:
        _ = write_config_fixture(conf.path("accounts"), "next_move_percent=3\n")
        rows = (replace(status_row("claude1"), selected_roles=("units",)),
                replace(status_row("claude2"), roles=("showrunners",), selected_roles=("showrunners",)),
                replace(status_row("claude3"), weekly_left_percent=accounts.StatusUnavailable("unavailable")),
                replace(status_row("codex1"), tool="codex"))
        units = next_moves.ClaudeGroupMove("unit-fixture", "units", 4.5, 0, "claude2", AT,
                                         measure=next_moves.QuotaMeasure.FABLE)
        runners = next_moves.ClaudeGroupMove("runner-fixture", "showrunners", 2, 0, "claude1", AT)
        reading = accounts.StatusReading(rows, (), (), timezone.utc, AT,
            first_next_moves=first_moves(units, runners))
        with patch.object(accounts, "read_status", return_value=reading):
            content = accounts_page.page()
        initial = accounts_page.AccountsRender(content, 0.5, 200).with_recent_times((0.5,)).decode()
        with patch.object(accounts, "read_status", return_value=replace(reading, first_next_moves=accounts.FirstNextMoveEntries())):
            empty = accounts_page.page().decode()
        unreadable = replace(reading, first_next_moves=accounts.NextMovesUnavailable("fixture read error"))
        with patch.object(accounts, "read_status", return_value=unreadable):
            unavailable = accounts_page.page().decode()
        probe = (MOVE_DIALOG_PROBE.replace("EMPTY_PAGE", json.dumps(empty).replace("</", "<\\/"))
                 .replace("UNAVAILABLE_PAGE", json.dumps(unavailable).replace("</", "<\\/")))
        setup = MENU_SETUP.replace("FRESH_PAGE", json.dumps(initial).replace("</", "<\\/"))
        page = initial.replace("<script>", setup + "<script>", 1).replace("</body>", probe + "</body>").encode()
        self.assertEqual(firefox_probe_results(page), [[]])

    def test_page_reads_stored_status_and_holds_the_refresh_root(self) -> None:
        reading = accounts.StatusReading((), (), (), timezone.utc, AT)
        with tempfile.TemporaryDirectory() as temporary:
            settings = Path(temporary) / "review_links.conf"
            _ = write_config_fixture(Path(temporary) / "accounts.conf", ACCOUNT_SETTINGS + LOGIN_CARD_SETTINGS)
            _ = write_config_fixture(Path(temporary) / "console.conf", PAGE_STATUS_SETTINGS)
            _ = write_config_fixture(Path(temporary) / "showrunners.conf", "mac_token.account=\n")
            write_machine_config(Path(temporary))
            for seconds in (15, 37):
                with self.subTest(refresh_seconds=seconds):
                    _ = write_config_fixture(settings, f"page_refresh_seconds={seconds}\n")
                    with (patch.object(accounts, "read_status", return_value=reading) as status,
                          patch.dict(os.environ, {"CONF_DIR": temporary, "USER_ZONE": "UTC"})):
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
            _ = write_config_fixture(Path(temporary) / "accounts.conf", ACCOUNT_SETTINGS + LOGIN_CARD_SETTINGS)
            _ = write_config_fixture(Path(temporary) / "console.conf", PAGE_STATUS_SETTINGS)
            _ = write_config_fixture(Path(temporary) / "showrunners.conf", "mac_token.account=\n")
            write_machine_config(Path(temporary))
            for seconds in (15, 37):
                with self.subTest(refresh_seconds=seconds):
                    _ = write_config_fixture(settings, f"page_refresh_seconds={seconds}\n")
                    with (patch.object(accounts, "read_status", return_value=reading),
                          patch.dict(os.environ, {"CONF_DIR": temporary, "USER_ZONE": "UTC"})):
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
        diagnostics = b"fixture child warning\nreview-links: move failed: Fixture start failed.\n"
        with (patch.object(subprocess, "run", return_value=subprocess.CompletedProcess((), 0, b"<p>page</p>",
                                                                                       diagnostics)) as run,
              redirect_stderr(io.StringIO()) as errors):
            self.assertEqual(accounts_page.render(), b"<p>page</p>")
        self.assertEqual(run.call_args.args[0], accounts_page.CHILD)
        self.assertEqual(errors.getvalue(), "")
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

    def test_new_item_opens_above_the_color_picker_and_its_screenshot_shows_the_picker(self) -> None:
        from scripts.review_links.test_page_frame import PAGE_OWNER, WINDOW_BROWSERS, WINDOW_PROBE, window_probe_results

        if not WINDOW_BROWSERS:
            self.skipTest("Chrome or Firefox is required for the window rule.")
        reading = accounts.StatusReading((status_row("claude1"), status_row("claude2")), (), (), timezone.utc, AT)
        with patch.object(accounts, "read_status", return_value=reading):
            content = accounts_page.page()
        rendered = accounts_page.AccountsRender(content, .1, 200).with_recent_times((.1,))
        markup = page_frame.with_owner(rendered, "accounts", PAGE_OWNER, staff.StaffSnapshot((PAGE_OWNER,)),
                                       new_item_dialog.render)
        probe = r'''
(async()=>{
 if(nested) return;
 try{
  await painted();
  const popup=document.querySelector('[data-color-picker]');
  document.querySelector('[data-color-key="account.claude1"]').click();
  await until(()=>popup.open,'the color picker');
  await painted();
  check(pressNewItem('over the color picker')!==null,'The press reaches New item.');
  const capture=await newItemOver(popup,'the color picker');
  const snapshot=await capturedDocument(capture);
  check(capture.place===location.pathname && snapshot.querySelector('[data-color-picker][open][data-capture-modal]'),
   'The snapshot includes the open color picker.');
  check(!snapshot.querySelector('#review-new-item'),'The snapshot omits New item.');
  await closeNewItem(popup,'the color picker');
 }catch(error){failures.push(String(error));}
 await report();
})();'''
        page = markup.replace(b"</body>", b"<script>" + (WINDOW_PROBE + probe).encode() + b"</script></body>", 1)
        for browser in WINDOW_BROWSERS:
            with self.subTest(browser=browser):
                self.assertEqual(window_probe_results({"/console/accounts.html": page}, "/console/accounts.html", browser),
                                 [[]])

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required for browser behavior")
    def test_refresh_obeys_visibility_and_updates_the_load_time_and_data_age(self) -> None:
        reading = accounts.StatusReading((status_row("claude1"), replace(status_row("claude2"), selected_roles=("units",))),
            (), (), timezone.utc, AT, first_next_moves=first_moves(claude_move("units", "claude1")))
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
  check(heads.slice(4,12).join(',')==='Fable left,used / hour,empty at,role,,next,weekly reset,resets',
   'Accounts keeps the quota, role, and reset columns in order');
  check(roleHeads.join(',')==='role,,busy, last 24 h,account,next,busy,idle,total',
   'Roles shows the role, its busy line, account, next and counts');
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
  const accountScroll=cell.closest('.scroll'), accountCard=accountScroll.closest('.card');
  check(accountScroll.scrollWidth>accountScroll.clientWidth && getComputedStyle(accountScroll).overflowX==='auto' &&
   accountScroll.getBoundingClientRect().right<=accountCard.getBoundingClientRect().right &&
   document.documentElement.scrollWidth<=innerWidth,'the wide Accounts table scrolls inside its card');
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
  // A held refresh still reads the page, so it can find a newer script. It does not change the view.
  pick('#123456');timer().load();await flush();
  check(calls===1 && swatch.isConnected && label().style.color===original && swatch.style.background===originalSwatch,
   'a held refresh reads the page and keeps the view and the pick unchanged');
  calls=0;action('try').click();
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
  check(swatch.isConnected && state()==='Paused','the reopened dialog keeps refresh paused after a failed save');
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
  check(swatch.isConnected && state()==='Paused','a pending POST holds interval and visibility refreshes');
  other.click();
  check(popup.open && picker.value===other.dataset.savedColor,'another swatch opens while a save is pending');
  picker.value='#fedcba';action('save').click();
  check(!popup.open && saves.length===2,'another account saves without waiting for the first POST');
  const otherForm=new URLSearchParams(saves[1].options.body);
  check(otherForm.get('key')==='account.claude2' && otherForm.get('value')==='#fedcba','each pending POST retains its account and color');
  saves[1].resolve({ok:true,json:async()=>({state:'saved'})});await flush();
  check(other.dataset.savedColor==='#fedcba' && otherLabel.style.color==='rgb(254, 220, 186)',
   'success stores the second account color');
  check(swatch.isConnected,'refresh remains held until every POST completes');
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
  check(label().style.color==='rgb(171, 205, 239)' && swatch.isConnected,'a network failure preserves another pending color and the refresh hold');
  check(line().textContent.includes('claude2: network unavailable'),'the page status line reports network errors');
  swatch.click();
  saves[3].resolve({ok:true,json:async()=>({state:'saved'})});await flush();
  check(popup.open && swatch.dataset.savedColor==='#abcdef' && swatch.isConnected,'success keeps a newly opened chooser intact');
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
  swatch.click();picker.value='#abcdef';action('save').click();calls=0;
  saves[6].resolve({ok:true,json:async()=>({state:'saved'})});await flush();
  check(swatch.dataset.savedColor==='#abcdef','success stores the chosen color before refresh');
  check(!popup.open && calls===1,'a successful save closes the popup and refreshes');
  const roles=[...document.querySelectorAll('[data-color-key="role.unit-director"]')];
  const roleWords=[...document.querySelectorAll('[data-color-text="role.unit-director"]')];
  const tables=nodes=>nodes.map(node=>node.closest('table').getAttribute('aria-label')).sort().join(',');
  check(tables(roles)==='Accounts,Accounts,Roles' && tables(roleWords)==='Accounts,Accounts,Models,Roles' &&
   roleWords.every(word=>word.textContent==='units' && word.style.color==='rgb(215, 175, 0)'),
   'Accounts and Roles show the units swatch and all three tables show the units label');
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
