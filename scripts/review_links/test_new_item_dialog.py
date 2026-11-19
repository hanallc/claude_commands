"""Test the New item dialog against a private HTTP fixture."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from dataclasses import asdict, replace, dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Literal, cast, override
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from scripts.production import investigation, staff
from scripts.review_links import console_theme, new_item_dialog, page_frame, review_submissions
from scripts.review_links.test_review_submissions import EVIDENCE, OWNER, PNG, identify_fixture_image

ENDPOINT = "/console/review/new-item"
RECIPIENTS = [{"id": str(OWNER), "name": "builds", "proposal": True}] + [
    {"id": name, "name": name, "proposal": False}
    for name in ("cargo-liner", "hana", "natedev", "sdlc", "spacetime", "brain", "dwight", "morty")]
OTHER_RECIPIENT = staff.StaffId("cargo-liner")
FORM_TOKEN = "fixture-form-token"
RESTARTED_TOKEN = "fixture-after-restart"
NO_CLIP_TEXT = "Last tooltip detail <strong> & all of its text"
DISPLAY_WORDS = "The nextest row stops updating during shard 2."


SHELL = '''
<style>
 main {width: min(1220px, calc(100% - 36px)); margin: 28px auto}
 h1 {font-size: 28px}
 .fixture-memory {display:flex;height:24px;border-radius:6px;overflow:hidden;margin-top:10px}
 .fixture-memory i:nth-child(1) {width:24%;background:#327fd1}
 .fixture-memory i:nth-child(2) {width:34%;background:#199e70}
 .fixture-memory i:nth-child(3) {width:42%;background:#5c5b57}
 table {width:100%;border-collapse:collapse;font-size:14px;text-align:left}
 th,td {padding:10px;border-bottom:1px solid var(--line)}
 #launcher {border-radius:7px;border:1px solid var(--accent);background:var(--card);color:var(--accent);
 font-size:20px;min-width:30px;cursor:pointer}
 .fixture-active {background:#182536}
 .fixture-secondary {margin-top:22px}
 @media(max-width:640px) { .fixture-extra {display:none} }
</style>
<main>
 <h1>Builds</h1>
 <section class="card">
  <h2>Overall memory</h2>
  <p><strong>18.0 GiB</strong> Free for the next step</p>
  <div class="fixture-memory"><i></i><i></i><i></i></div>
 </section>
 <section class="card">
  <h2>Running</h2>
  <table><thead><tr><th>Step</th><th>Stage</th><th class="fixture-extra">Target</th><th></th></tr></thead>
   <tbody><tr class="fixture-active"><td>hana nextest</td><td>testing</td>
    <td class="fixture-extra">natedev</td><td><button id="launcher" aria-label="New item for hana nextest">+</button></td></tr>
    <tr><td>cargo test</td><td>building</td><td class="fixture-extra">mac</td><td></td></tr></tbody></table>
 </section>
 <section class="card fixture-secondary">
  <h2>Recent steps</h2>
  <table><thead><tr><th>Step</th><th>State</th><th class="fixture-extra">Target</th></tr></thead><tbody>
   <tr><td>cargo check</td><td>passed</td><td class="fixture-extra">natedev</td></tr>
   <tr><td>hana checks</td><td>passed</td><td class="fixture-extra">mac</td></tr></tbody></table>
 </section>
</main>
'''


PROBE = r'''
const failures = [];
const check = (ok, description) => { if (!ok) failures.push(description); };
const dialog = document.querySelector('dialog'), body = dialog.querySelector('.new-item-body');
const textarea = dialog.querySelector('textarea'), error = dialog.querySelector('[role="alert"]');
const send = dialog.querySelector('[type="submit"]'), recipient = () => dialog.querySelector('[name="recipient_id"]');
const launch = document.querySelector('#launcher');
const capture = __CAPTURE__, png = '__PNG__';
const open = (options = {}) => window.reviewNewItem.open({trigger: launch, kind: 'bug', evidence: capture, ...options});
const closeToLauncher = () => !dialog.open && document.activeElement === launch;
function type(text) {
 textarea.value = text;
 textarea.dispatchEvent(new InputEvent('input', {bubbles:true}));
}
function paste() {
 const input = new DataTransfer();
 const bytes = Uint8Array.from(atob(png), character => character.charCodeAt(0));
 input.items.add(new File([bytes], 'pasted.png', {type:'image/png'}));
 const event = new ClipboardEvent('paste', {cancelable:true});
 Object.defineProperty(event, 'clipboardData', {value: input});
 textarea.dispatchEvent(event);
}
function untilError() {
 return new Promise(resolve => {
  const observer = new MutationObserver(() => {
   if (!send.disabled && !error.hidden) { observer.disconnect(); resolve(); }
  });
  observer.observe(dialog, {attributes:true, subtree:true});
 });
}
(async () => {
 try {
  if (__PHOTO__) {
   await open();
   recipient().click();
   type(__DISPLAY_WORDS__);
   document.querySelector('#new-item-title').tabIndex = -1;
   document.querySelector('#new-item-title').focus();
   check(dialog.open && dialog.offsetWidth === Math.min(560, innerWidth - 32), 'Screenshot dialog width');
  } else {
   await open();
   check(dialog.open && dialog.matches(':modal'), 'The dialog opens as a native modal.');
   check(document.activeElement.value === 'bug', 'Focus starts on the selected Type radio.');
   check([...dialog.querySelectorAll('[name="kind"]')].length === 4, 'Four keyboard-selectable types are present.');
   check(!!recipient() && recipient().tabIndex === 0, 'To uses native keyboard-selectable radios.');
   check(send.disabled, 'Send needs words and a recipient.');
   type('Check <index>\nAgain');
   check(send.disabled, 'Words alone do not enable Send.');
   recipient().click();
   check(!send.disabled, 'A recipient and words enable Send.');

   const bounds = dialog.getBoundingClientRect();
   check(innerWidth === __WIDTH__, 'Firefox uses the requested viewport width.');
   check(bounds.width === Math.min(560, innerWidth - 32), 'The dialog fits the viewport.');
   check(Math.abs(bounds.left - (innerWidth - bounds.right)) < 1 &&
         Math.abs(bounds.top - (innerHeight - bounds.bottom)) < 1, 'The dialog is centered on both axes.');
   const chips = [...dialog.querySelectorAll('[data-kind]')];
   check((chips[0].offsetTop !== chips[3].offsetTop) === (innerWidth < 480), 'Type chips wrap on a phone.');
   const evidence = dialog.querySelector('[data-evidence]'), tip = dialog.querySelector('.new-item-tip');
   check(evidence.getBoundingClientRect().bottom < textarea.getBoundingClientRect().top, 'Evidence precedes the words.');
   check(tip.lastElementChild.textContent === __LAST_DETAIL__ && !tip.querySelector('strong strong'),
         'The final tooltip entry stays visible as text.');
   check(tip.clientHeight === tip.scrollHeight && getComputedStyle(tip).backgroundColor === 'rgb(36, 36, 34)',
         'The full tooltip uses the dark background and has no internal scroll.');
   check(body.scrollHeight > body.clientHeight, 'Tall evidence scrolls the dialog body.');
   const fixedTop = dialog.querySelector('header').getBoundingClientRect().top;
   const fixedBottom = dialog.querySelector('footer').getBoundingClientRect().bottom;
   body.scrollTop = body.scrollHeight;
   check(body.scrollTop > 0 && fixedTop === dialog.querySelector('header').getBoundingClientRect().top &&
         fixedBottom === dialog.querySelector('footer').getBoundingClientRect().bottom,
         'Header and footer remain visible while the body scrolls.');
   check(body.scrollWidth === body.clientWidth, 'The dialog body has no horizontal scroll.');

   paste();
   paste();
   check(dialog.querySelectorAll('[data-previews] img').length === 2, 'Pasting adds image previews.');
   dialog.querySelector('[data-previews] button').click();
   check(dialog.querySelectorAll('[data-previews] img').length === 1, 'Remove deletes one preview.');
   await fetch('/restart', {method:'POST'});
   const failed = untilError();
   send.click();
   send.click();
   await failed;
   check(dialog.open && error.textContent === 'Fixture rejected the first send.', 'A failed send displays its reason.');
   check(textarea.value === 'Check <index>\nAgain' && dialog.querySelectorAll('[data-previews] img').length === 1,
         'A failed send keeps words and images.');
   check(document.activeElement === send, 'Focus returns to Send on failure.');
   dialog.requestClose();
   await open({kind:'question', evidence:{kind:'none'}});
   check(textarea.value === 'Check <index>\nAgain' && !evidence.hidden && recipient().checked &&
         dialog.querySelectorAll('[data-previews] img').length === 1 &&
         error.textContent === 'Fixture rejected the first send.' && !send.disabled,
         'Reopen keeps the failed draft and its error.');
   const created = new Promise(resolve => document.addEventListener('review-item-created', resolve, {once:true}));
   send.click();
   check((await created).detail.id === 'I1', 'The event reports the saved item ID.');
   check(closeToLauncher(), 'A successful send closes the dialog and returns focus.');

   await open({kind:'question', evidence:{kind:'none'}});
   check(document.activeElement.value === 'question' && evidence.hidden && !textarea.value &&
         dialog.querySelector('[data-previews]').hidden, 'Reopen clears the sent draft and accepts empty evidence.');
   const to = () => [...dialog.querySelectorAll('[name="recipient_id"]')];
   const kinds = dialog.querySelector('[name="kind"][value="proposal"]');
   to()[1].click();
   kinds.click();
   check(to().length === 1 && to()[0].value === 'builds-staff' && !to()[0].checked && send.disabled,
         'Proposal filters the recipients and clears a recipient without a proposal folder.');
   dialog.querySelector('[name="kind"][value="question"]').click();
   check(to().length === 9, 'The other types include all recipients.');
   dialog.requestClose();
   check(closeToLauncher(), 'The native cancel action closes the modal and restores focus.');
   await open({evidence:{kind:'none'}});
   const attempted = [], originalFetch = window.fetch;
   window.fetch = (url, options) => {
    if (url !== '/console/review/new-item' || options?.method !== 'POST') return originalFetch(url, options);
    attempted.push(JSON.parse(options.body));
    if (attempted.length === 1) {
     return Promise.resolve(new Response(
       JSON.stringify({reason:'builds has finished; choose another recipient'}), {status:409}));
    }
    if (attempted.length === 2) {
     return Promise.resolve(new Response(JSON.stringify({error:'Send is forbidden.'}), {status:403}));
    }
    if (attempted.length <= 4) {
     return Promise.resolve(new Response(
       JSON.stringify({code:'stale_token', error:'The token expired again.'}), {status:403}));
    }
    throw new Error('The dialog attempted an extra Send.');
   };
   const trySend = async () => {
    const rejected = untilError();
    send.click();
    await rejected;
   };
   const sendSaved = async () => {
    const saved = new Promise(resolve => document.addEventListener('review-item-created', resolve, {once:true}));
    const failed = untilError().then(() => { throw new Error(error.textContent); });
    send.click();
    return Promise.race([saved, failed]);
   };
   recipient().click();
   type('Keep these words');
   await trySend();
   check(dialog.open && textarea.value === 'Keep these words' &&
         error.textContent === 'builds has finished; choose another recipient', 'A conflict keeps the draft and shows its reason.');
   to()[1].click();
   await trySend();
   check(attempted.length === 2 && error.textContent === 'Send is forbidden.',
         'An unrelated HTTP 403 uses the server message and does not retry.');
   dialog.querySelector('[name="kind"][value="investigation"]').click();
   await trySend();
   window.fetch = originalFetch;
   check(attempted.length === 4 && attempted[2].request_id === attempted[3].request_id &&
         error.textContent === 'The token expired again.', 'The dialog retries an expired token once.');
   check(new Set(attempted.map(post => post.request_id)).size === 1 &&
         attempted[0].recipient_id !== attempted[1].recipient_id && attempted[1].kind !== attempted[2].kind,
         'Changing the recipient or type keeps the unsent draft ID.');
   dialog.querySelector('[data-cancel]').click();
   check(closeToLauncher(), 'Cancel restores focus to the launcher.');

   const ownerPosts = [], ownerEndpoint = '/console/review/new-item?page=builds';
   let pngAvailable = true, discarded, useLiveSends = false, dropPageReply = true;
   const captureKey = 'd34db33fd34db33fd34db33fd34db33f';
   const json = (value, status = 200) => Promise.resolve(new Response(JSON.stringify(value), {status}));
   window.fetch = (url, options) => {
    if (url === ownerEndpoint) {
     return json({token:'owner-token', recipients:[{id:'builds-staff', name:'builds', proposal:false}]});
    }
    if (url !== '/console/review/new-item' || options?.method !== 'POST') return originalFetch(url, options);
    const payload = JSON.parse(options.body);
    ownerPosts.push(payload);
    if (payload.page_capture) {
     return pngAvailable ? json({capture_key: captureKey}, 202) : json({error:'No Firefox.'}, 503);
    }
    if (payload.discard_capture) {
     discarded?.();
     return json({});
    }
    if (useLiveSends) {
     const response = originalFetch(url, {...options, body: JSON.stringify({...payload, token: attempted[0].token})});
     return payload.page && dropPageReply ? response.then(saved => {
      if (!saved.ok) return saved;
      throw new Error('The Send response was lost.');
     }) : response;
    }
    return json({id:'I2', key:'saved'});
   };
   const checkbox = dialog.querySelector('[name="include_page"]');
   async function openOwner() {
    await open({page:'builds', kind:'question', evidence:{kind:'none'}});
    check(checkbox.checked && !checkbox.closest('label').hidden, 'The page checkbox is checked on open.');
    check(to().length === 1 && to()[0].checked && to()[0].value === 'builds-staff', 'The owner is selected.');
    check(to()[0].nextElementSibling.hasAttribute('data-button-face'), 'The owner chip uses the shared button style.');
    const shown = [...dialog.querySelectorAll('[data-kind]:not([hidden])')].map(chip=>chip.textContent.trim());
    check(shown.join(',') === 'Bug,Question,Feedback', 'The owner dialog has the three requested types.');
   }
   async function sendOwner() {
    dialog.querySelector('[name="kind"][value="investigation"]').click();
    type('Show the queue state.');
    return sendSaved();
   }
   await openOwner();
   check(ownerPosts.length === 1 && ownerPosts[0].page_capture.width === innerWidth,
         'Opening the dialog starts one capture at the requested viewport.');
   checkbox.click();
   checkbox.click();
   await sendOwner();
   check(ownerPosts.length === 2 && ownerPosts[1].capture_key === captureKey &&
         ownerPosts[1].images.length === 0, 'Send uses the same capture key after the user checks the box again.');

   await openOwner();
   checkbox.click();
   const skippedCapture = new Promise(resolve => { discarded = resolve; });
   await sendOwner();
   await skippedCapture;
   check(ownerPosts.length === 5 && !ownerPosts[3].capture_key && ownerPosts[3].evidence.kind === 'none' &&
         ownerPosts[4].discard_capture === captureKey, 'Send with the box unchecked discards the capture.');

   await openOwner();
   const canceled = new Promise(resolve => { discarded = resolve; });
   dialog.querySelector('[data-cancel]').click();
   await canceled;
   check(ownerPosts.length === 7 && ownerPosts[6].discard_capture === captureKey && !dialog.open,
         'Closing the dialog discards the capture.');

   pngAvailable = false;
   const emoji = 'x'.repeat(239) + '😀';
   const main = document.querySelector('main');
   main.querySelector('h1').textContent = emoji + 'overflow';
   const paragraph = document.createElement('p');
   paragraph.textContent = emoji + 'overflow';
   main.append(paragraph);
   await openOwner();
   await sendOwner();
   const fallback = ownerPosts[8].evidence;
   check(ownerPosts.length === 9 && !ownerPosts[8].capture_key && fallback.kind === 'page-data' &&
         fallback.rows.some(row=>row.includes('hana nextest')), 'No Firefox sends visible rows.');
   check(fallback.title === emoji && fallback.rows.some(row=>row.includes(emoji)),
         'The title and rows keep an emoji at the character limit.');
   check(ownerPosts[8].page === 'builds', 'Send includes the page identity.');

   useLiveSends = true;
   await openOwner();
   dialog.querySelector('[name="kind"][value="investigation"]').click();
   type('Keep this draft and its image.');
   paste();
   const preview = dialog.querySelector('[data-previews] img');
   await trySend();
   check(error.textContent === 'The Send response was lost.', 'The page Send files before the response is lost.');
   dialog.querySelector('[data-cancel]').click();
   await open({evidence:{kind:'none'}});
   const previewReply = await originalFetch(preview.src).catch(() => null);
   check(textarea.value === 'Keep this draft and its image.' &&
         dialog.querySelector('[data-previews] img') === preview && previewReply?.ok,
         'Standard New keeps the failed page draft and its pasted image.');
   const ownerRequest = ownerPosts.at(-1);
   recipient().click();
   check((await sendSaved()).detail.id === 'I2', 'The retry returns the item already filed.');
   const standardRequest = ownerPosts.at(-1);
   check(!standardRequest.page && !standardRequest.capture_key && standardRequest.images[0].base64_data === png &&
         standardRequest.request_id === ownerRequest.request_id,
         'old -> new: Standard New uses a new request ID -> Standard New keeps the draft request ID.');
   await openOwner();
   dialog.querySelector('[name="kind"][value="investigation"]').click();
   type('The first item before replacement.');
   await trySend();
   check(error.textContent === 'The Send response was lost.', 'The first item files before the response is lost.');
   const firstDraft = ownerPosts.at(-1);
   dialog.querySelector('[data-cancel]').click();
   await open({evidence:{kind:'none'}});
   type('The replacement item.');
   to()[1].click();
   const replacement = (await sendSaved()).detail;
   check(replacement.id === 'I4', 'The replacement files for the new recipient.');
   check(ownerPosts.at(-1).request_id === firstDraft.request_id, 'The replacement keeps the ID after the lost response.');
   await openOwner();
   dialog.querySelector('[name="kind"][value="investigation"]').click();
   const undoWords = 'Restore these words after the edit.';
   type(undoWords);
   await trySend();
   check(error.textContent === 'The Send response was lost.', 'F013 files before the response is lost.');
   type('Edit the draft.');
   type(undoWords);
   dropPageReply = false;
   check((await sendSaved()).detail.id === 'I5', 'F013 returns the first item after undo.');

   const pageTitle = main.querySelector('h1');
   pageTitle.textContent = 'First queue state';
   await openOwner();
   const firstReport = (await sendOwner()).detail;
   const firstReportRequest = ownerPosts.at(-1);
   pageTitle.textContent = 'Second queue state';
   await openOwner();
   const secondReport = (await sendOwner()).detail;
   check(firstReport.id !== secondReport.id, 'F015 files a new item after a confirmed Send.');
   check(firstReportRequest.request_id !== ownerPosts.at(-1).request_id, 'F015 starts a new draft ID after success.');
   const listPosts = [];
   let listOwner = 'first-owner';
   window.fetch = (url, options) => {
    if (url === '/console/review/new-item?page=list') {
     return json({token:'list-token',recipients:[{id:listOwner,name:listOwner}]});
    }
    if (url === '/console/review/new-item' && options?.method === 'POST') {
     const payload = JSON.parse(options.body);
     if (payload.page_capture) return json({});
     listPosts.push(payload);
     return listOwner === 'first-owner' ? json({error:'The List link is unavailable.'}, 503) : json({id:'Q1'});
    }
    return originalFetch(url, options);
   };
   await open({page:'list'});
   type('Keep these List words.');
   await trySend();
   check(error.textContent === 'The List link is unavailable.', 'The first List Send fails.');
   dialog.querySelector('[data-cancel]').click();
   await open({page:'list'});
   await trySend();
   check(listPosts[1].request_id === listPosts[0].request_id &&
         listPosts[1].recipient_id === 'first-owner', 'The same List owner keeps the request ID.');
   dialog.querySelector('[data-cancel]').click();
   listOwner = 'new-owner';
   await open({page:'list'});
   check(to()[0].checked && to()[0].value === listOwner, 'The reopened List dialog selects the new owner.');
   await sendSaved();
   check(listPosts[2].request_id === listPosts[0].request_id && listPosts[2].recipient_id === listOwner &&
         listPosts[2].words === listPosts[0].words, 'The new List owner sends the same words with the same request ID.');

   window.fetch = (url, options) => {
    if (url === '/console/review/new-item?page=list') {
     return json({token:'', recipients:[{id:'list-staff',name:'morty'}],
                  send_disabled:'Sending is off on a practice List.'});
    }
    if (url === '/console/review/new-item' && options?.method === 'POST') {
     ownerPosts.push(JSON.parse(options.body));
     return json({});
    }
    return originalFetch(url, options);
   };
   const beforePractice = ownerPosts.length;
   await open({page:'list',kind:'question'});
   const notice = dialog.querySelector('[data-send-disabled]');
   check(dialog.open && to()[0].checked && to()[0].value === 'list-staff', 'The practice dialog shows the List owner.');
   type('A practice item.');
   check(!notice.hidden && notice.textContent === 'Sending is off on a practice List.',
         'The practice dialog shows one reason.');
   check(send.disabled && checkbox.closest('label').hidden, 'The practice dialog disables Send and hides capture.');
   send.click();
   check(ownerPosts.length === beforePractice, 'The practice dialog makes no POST.');
   dialog.querySelector('[data-cancel]').click();
   window.fetch = originalFetch;
  }
 } catch (failure) {
  failures.push(String(failure));
 }
 await fetch('/results', {method:'POST', body:JSON.stringify(failures)});
 parent.postMessage('finished',location.origin);
})();
'''


def fixture_page(width: int, *, photo: bool) -> bytes:
    evidence = EVIDENCE if photo else replace(EVIDENCE, tip=(
        *EVIDENCE.tip,
        *(review_submissions.TipField(f"Step detail {n}", f"Run {n}, RSS {n} GiB") for n in range(28)),
        review_submissions.TipDetail(NO_CLIP_TEXT)))
    probe = (PROBE.replace("__CAPTURE__", json.dumps(asdict(evidence)))
             .replace("__PHOTO__", json.dumps(photo)).replace("__PNG__", PNG).replace("__WIDTH__", str(width))
             .replace("__LAST_DETAIL__", json.dumps(NO_CLIP_TEXT))
             .replace("__DISPLAY_WORDS__", json.dumps(DISPLAY_WORDS)))
    return (f'<!doctype html><html lang="en" data-theme="dark"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1"><title>Builds</title>'
            f'<style>{console_theme.CSS}{page_frame.BUTTON_STATES}</style>{SHELL}{new_item_dialog.render()}<script>{probe}</script>').encode()


BUG_PROBE = r"""
const failures = [];
const check = (value, description) => { if (!value) failures.push(description); };
const wire = [], fetchHTTP = window.fetch;
window.fetch = async (path, options) => {
 if (!String(path).startsWith('/console/review/new-item')) return fetchHTTP(path, options);
 if (!options.body) return Response.json({token:'fixture',recipients:[{id:'owner',name:'sdlc',proposal:false}]});
 wire.push(JSON.parse(options.body));
 return Response.json({id:'I1',key:'bug/owner/I1'});
};
const dialog = document.querySelector('dialog');
const checkbox = dialog.querySelector('[name="include_page"]');
const words = dialog.querySelector('textarea');
const state = dialog.querySelector('[data-bug-page-state]');
const trigger = document.querySelector('[data-owner-new]');
const open = () => new Promise(resolve => {
 const recipients = dialog.querySelector('[data-recipients]');
 const observer = new MutationObserver(() => {
  if (recipients.querySelector('input:checked')) { observer.disconnect(); resolve(); }
 });
 observer.observe(recipients, {childList:true,subtree:true});
 trigger.click();
});
const type = text => {
 words.value = text;
 words.dispatchEvent(new Event('input', {bubbles:true}));
};
const send = () => new Promise(resolve => {
 dialog.addEventListener('review-item-created', resolve, {once:true});
 dialog.querySelector('[type="submit"]').click();
});
(async () => {
 try {
  history.replaceState(null, '', location.pathname + location.search + '&view=queue#show=open&owner=builds');
  if (!__PHOTO__) {
   for (let index=0; index<21; index++) dispatchEvent(new ErrorEvent('error', {
    message:'Error '+index,filename:location.origin+'/review.js',lineno:9,colno:3}));
   dispatchEvent(new PromiseRejectionEvent('unhandledrejection', {promise:Promise.resolve(),reason:'Rejected'}));
   const table = document.createElement('table');
   table.style.cssText = 'width:100%;table-layout:fixed';
   for (let row=0; row<48; row++) {
    const line = table.insertRow();
    for (let cell=0; cell<16; cell++) line.insertCell().textContent = '😀'.repeat(240);
   }
   document.querySelector('main').append(table);
  }
  await open();
  check(!state.hidden && state.textContent.includes('Page state to send'), 'The Bug dialog shows the page state.');
  check(dialog.querySelector('[data-kind="proposal"]').getClientRects().length === 0,
        'The console owner dialog does not display Proposal.');
  check(state.textContent.includes('Type All · State Open · Owner builds'), 'The dialog shows the selected filters.');
  check(state.textContent.includes('2.40 s') && state.textContent.includes('Could not load …/brain.'),
        'The dialog shows the render time and the notice.');
  check(checkbox.checked && checkbox.labels[0].textContent.trim() === 'Include a screenshot',
        'The screenshot box is checked by default.');
  check(wire.length === 0, 'Opening the Bug dialog requests no screenshot.');
  type('Review takes over two seconds with the builds filter. It also shows a load error.');
  if (__PHOTO__) {
   const title = dialog.querySelector('h2');
   title.tabIndex = -1;
   title.focus();
   const {x,y,width,height} = dialog.getBoundingClientRect();
   failures.push(JSON.stringify([x,y,width,height]));
  } else {
   checkbox.click();
   document.querySelector('.notice').textContent = 'A later notice.';
   await send();
   const [post] = wire, captured = post.page_state;
   check(wire.length === 1 && !post.capture_key && !captured.include_screenshot,
         'Unchecked Send requests no screenshot.');
   check(post.page === 'review' && captured.url === location.href
         && captured.viewport.width === innerWidth && captured.viewport.height === innerHeight && captured.viewport.dark,
         'The request includes the page, the full URL and the viewport.');
   check(captured.render_seconds === 2.4 && captured.notices.join() === 'Could not load …/brain.'
         && post.evidence.rows.some(row=>row.includes('Could not load …/brain.')),
         'The request uses the data from dialog open.');
   const bytes = new TextEncoder().encode(JSON.stringify(post.evidence)).length;
   check(bytes <= 64*1024 && post.evidence.rows.length < 48 &&
         post.evidence.rows.some(row=>row.includes('😀'.repeat(240))),
         'The dialog keeps complete rows within the UTF-8 byte budget.');
   check(captured.script_errors.length === 20 && captured.script_errors[0].message === 'Error 2'
         && captured.script_errors[0].line === 9 && captured.script_errors[0].column === 3
         && captured.script_errors.at(-1).message === 'Rejected', 'The request keeps the last twenty script errors.');
   check(dialog.open === false, 'Send closes the dialog.');
  }
 } catch (error) {
  failures.push(String(error));
 }
 await fetchHTTP('/results', {method:'POST',body:JSON.stringify(failures)});
 parent.postMessage('finished',location.origin);
})();
"""


def bug_fixture_page(*, photo: bool = False) -> bytes:
    probe = BUG_PROBE.replace("__PHOTO__", json.dumps(photo))
    return (f'<!doctype html><html lang="en" data-theme="dark"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1"><title>Review</title>'
            f'{page_frame.PAGE_SCRIPT_ERRORS}<style>{console_theme.CSS}{page_frame.OWNER_STYLE.removeprefix("<style>").removesuffix("</style>")}'
            'main{width:min(1220px,calc(100% - 48px));margin:28px auto}.notice{color:var(--error)}</style></head><body>'
            '<main><div class="page-owner" data-page-owner="review"><span>sdlc</span>'
            f'<button data-owner-new aria-label="New item">{page_frame.icon_svg("message-circle-question")}</button></div>'
            '<h1>Review</h1><p data-page-status data-render-seconds="2.40">Live · render 2.40 s</p>'
            '<div role="group" aria-label="Type"><button data-type-chip="" aria-pressed="true">All <b>2</b></button></div>'
            '<div role="group" aria-label="Show"><button data-show-chip="open" aria-pressed="true">Open</button></div>'
            '<div role="group" aria-label="Owner"><button data-owner-chip="builds" aria-pressed="true">builds</button></div>'
            '<p class="notice">Could not load …/brain.</p></main>'
            f'{new_item_dialog.render()}<script>{probe}</script></body></html>').encode()


@dataclass
class DialogFixture:
    root: Path
    results: list[str] = field(default_factory=list)
    posts: list[review_submissions.NewItemRequest] = field(default_factory=list)
    wire_posts: list[dict[str, object]] = field(default_factory=list)
    token: str = FORM_TOKEN
    recipient_reads: int = 0
    completed: bool = False

    def environment(self) -> dict[str, str]:
        return {"SHOWRUNNER_STATE_DIR": str(self.root / "state"), "REVIEW_LINKS_ROOT": str(self.root / "review"),
                "CONF_DIR": str(self.root / "config")}


def capture_dialogs(output: Path, fixtures: dict[int, DialogFixture], *, pages: dict[int, bytes] | None = None) -> None:
    if pages is None:
        pages = {width: fixture_page(width, photo=False) for width in fixtures}
    documents = pages
    finished = threading.Event()
    outer = f"""<!doctype html><meta charset="utf-8">
    <style>body{{margin:0}}iframe{{display:block;border:0}}</style>
    <iframe title="Dialog probe"></iframe><img src="/complete" alt="" hidden>
    <script>
    const widths={json.dumps(list(fixtures))}, frame=document.querySelector('iframe');
    let next=0;
    const load=()=>{{
      if(next===widths.length)return;
      const width=widths[next++];
      frame.width=width;frame.height=width<480?844:900;
      frame.src='/case?width='+width;
    }};
    addEventListener('message', event=>{{
      if(event.origin===location.origin && event.source===frame.contentWindow && event.data==='finished')load();
    }});
    load();
    </script>""".encode()

    class Handler(BaseHTTPRequestHandler):
        def response(self, status: int, content_type: str, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            _ = self.wfile.write(body)

        def send_json(self, status: int, body: object) -> None:
            self.response(status, "application/json", json.dumps(body).encode())

        def fixture(self) -> DialogFixture:
            width = parse_qs(urlsplit(self.headers["Referer"]).query)["width"][0]
            return fixtures[int(width)]

        def do_GET(self) -> None:
            if self.path == ENDPOINT:
                fixture = self.fixture()
                fixture.recipient_reads += 1
                self.send_json(200, {"token": fixture.token, "recipients": RECIPIENTS})
            elif self.path == "/complete":
                _ = finished.wait(60)
                self.response(204, "image/png", b"")
            elif self.path == "/":
                self.response(200, "text/html; charset=utf-8", outer)
            elif self.path.startswith("/case?"):
                width = int(parse_qs(urlsplit(self.path).query)["width"][0])
                self.response(200, "text/html; charset=utf-8", documents[width])
            else:
                self.response(404, "text/plain", b"Unknown fixture path")

        def do_POST(self) -> None:
            if self.path == "/restart":
                self.fixture().token = RESTARTED_TOKEN
                self.response(204, "text/plain", b"")
                return
            data = self.rfile.read(int(self.headers["Content-Length"]))
            if self.path == "/results":
                fixture = self.fixture()
                fixture.results.extend(cast(list[str], json.loads(data)))
                fixture.completed = True
                self.response(204, "text/plain", b"")
                if all(case.completed for case in fixtures.values()):
                    finished.set()
                return
            if self.path != ENDPOINT:
                self.response(404, "text/plain", b"Unknown fixture path")
                return
            if self.headers["Content-Type"] != "application/json":
                self.send_json(400, {"reason": "The request must use JSON."})
                return
            fixture = self.fixture()
            fields = cast(dict[str, object], json.loads(data))
            fixture.wire_posts.append(fields)
            if fields.get("token") != fixture.token:
                self.send_json(403, {"code": "stale_token", "error": "The New item token expired. Try Send again."})
                return
            try:
                request = review_submissions.read_request(fields)
                fixture.posts.append(request)
                with patch.dict(os.environ, fixture.environment()), patch.object(Path, "home", return_value=fixture.root):
                    item = review_submissions.submit(request)
            except review_submissions.SubmissionConflict as error:
                self.send_json(error.status, {"reason": str(error)})
                return
            except (OSError, ValueError) as error:
                self.send_json(400, {"reason": str(error)})
                return
            if len(fixture.posts) == 1:
                self.send_json(503, {"error": "Fixture rejected the first send."})
                return
            self.send_json(201, {"id": item.id, "key": item.key, "state": "Waiting"})

        @override
        def log_message(self, format: str, *args: object) -> None:
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        try:
            profile = output.parent / "profile"
            profile.mkdir()
            run = subprocess.run(
                ["firefox", "--headless", "--no-remote", "--profile", str(profile),
                 "--window-size", "1440,900", "--screenshot", str(output),
                 f"http://127.0.0.1:{server.server_port}/"],
                capture_output=True, text=True, timeout=75, check=False)
            if run.returncode:
                raise AssertionError(run.stderr)
            if not finished.is_set():
                raise AssertionError("Firefox did not report its fixture results.")
        finally:
            server.shutdown()
            thread.join(1)


class NewItemDialogTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required for dialog behavior")
    def test_bug_dialog_reports_the_open_page_even_without_a_screenshot(self) -> None:
        temporary = Path(self.enterContext(tempfile.TemporaryDirectory()))
        fixture = DialogFixture(temporary)
        capture_dialogs(temporary / "bug.png", {1440: fixture}, pages={1440: bug_fixture_page()})
        self.assertEqual(fixture.results, [])

    def test_render_mounts_one_dialog_with_a_four_line_field_and_four_types(self) -> None:
        markup = new_item_dialog.render()
        self.assertEqual(markup.count('<dialog id="review-new-item"'), 1)
        self.assertIn('name="words" rows="4"', markup)
        self.assertEqual(markup.count('type="radio" name="kind"'), 4)
        self.assertEqual(markup.count('<span data-button-face>'), 4)
        self.assertIn('data-kind="bug"', markup)
        self.assertIn('<svg aria-hidden="true"', markup)
        self.assertLess(markup.index('data-evidence'), markup.index('name="words"'))
        self.assertIn('name="include_page" checked', markup)
        self.assertIn('data-page-capture hidden', markup)
        buttons: tuple[tuple[Literal["review", "list"], str], ...] = (("review", "New"), ("list", "New item"))
        for page, label in buttons:
            with self.subTest(page=page):
                button = new_item_dialog.title_button(page)
                self.assertIn(f'aria-label="New item" title="New item" data-{page}-new>', button)
                self.assertIn(f'<span aria-hidden="true">+</span><span class="review-new-label">{label}</span>', button)

    def test_page_data_and_capture_size_are_checked(self) -> None:
        fields = {"kind": "page-data", "title": "The List", "rows": [["Item", "State"], ["Console", "Open"]]}
        self.assertEqual(new_item_dialog.read_evidence(fields),
                         new_item_dialog.PageDataEvidence("The List", (("Item", "State"), ("Console", "Open"))))
        capture = {"place": "/console/list.html#show=open", "width": 1137, "height": 688, "dark": True}
        self.assertEqual(new_item_dialog.read_page_capture(capture),
                         new_item_dialog.PageCaptureRequest("/console/list.html#show=open", 1137, 688, True))
        for bad in ({"width": True}, {"height": 2161}, {"place": "//other.invalid/"}, {"dark": "true"}):
            with self.subTest(bad=bad), self.assertRaises(new_item_dialog.SubmissionError):
                _ = new_item_dialog.read_page_capture({**capture, **bad})

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required for dialog behavior")
    def test_desktop_and_phone_dialogs(self) -> None:
        temporary = Path(self.enterContext(tempfile.TemporaryDirectory()))
        recipient = staff.StaffRow(OWNER, "producer", "builds", staff.StaffRoot(), current_session=OWNER)
        other = staff.StaffRow(OTHER_RECIPIENT, "producer", "cargo-liner", staff.StaffRoot(),
                               current_session=OTHER_RECIPIENT)
        snapshot = staff.StaffSnapshot((recipient, other))
        _ = self.enterContext(patch.object(staff, "snapshot", return_value=snapshot))
        _ = self.enterContext(patch.object(review_submissions, "_identify_image", autospec=True,
                                           side_effect=identify_fixture_image))
        fixtures = {width: DialogFixture(temporary / str(width)) for width in (1440, 390)}
        for fixture in fixtures.values():
            configuration = fixture.root / "config"
            configuration.mkdir(parents=True)
            _ = (configuration / "console.conf").write_text(f"page.builds.owner={OWNER}\n")
        capture_dialogs(temporary / "dialog.png", fixtures)
        for width, fixture in fixtures.items():
            with self.subTest(width=width), patch.dict(os.environ, fixture.environment()):
                self.assertEqual(fixture.results, [])
                records = investigation.records(OWNER)
                self.assertEqual([item.id for item in records], ["I1", "I2", "I3", "I5", "I6", "I7"])
                original = records[2]
                for report, title in zip(records[-2:], ("First queue state", "Second queue state"), strict=True):
                    self.assertEqual(report.question, "Show the queue state.")
                    assert isinstance(report.submission, new_item_dialog.ReviewSubmission)
                    evidence = report.submission.evidence
                    assert isinstance(evidence, new_item_dialog.PageDataEvidence)
                    self.assertEqual(evidence.title, title)
                replacement, = investigation.records(OTHER_RECIPIENT)
                self.assertEqual(original.question, "The first item before replacement.")
                self.assertEqual((replacement.id, replacement.question), ("I4", "The replacement item."))
                self.assertEqual(fixture.recipient_reads, 8)
                expired, refreshed, reopened, *owner_sends = fixture.wire_posts
                self.assertEqual(expired["token"], FORM_TOKEN)
                self.assertEqual(refreshed, {**expired, "token": RESTARTED_TOKEN})
                self.assertEqual(reopened, refreshed)
                self.assertEqual(len(owner_sends), 8)
                owner_ids = [post["request_id"] for post in owner_sends]
                self.assertEqual(owner_ids[:6:2], owner_ids[1:6:2])
                self.assertEqual(len({expired["request_id"], *owner_ids[:6:2], *owner_ids[6:]}), 6)
                first, retry = fixture.posts[:2]
                self.assertEqual(first, retry)
                self.assertEqual(first.kind, review_submissions.SubmissionKind.BUG)
                self.assertEqual(first.recipient_id, OWNER)
                self.assertEqual(first.evidence.kind, "build-step")
                self.assertEqual(first.images, (review_submissions.PastedImage("image/png", PNG),))
