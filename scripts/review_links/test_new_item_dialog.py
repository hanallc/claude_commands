"""Test the New item dialog against a private HTTP fixture."""
from __future__ import annotations

import base64
import json
import os
import shutil
import tempfile
import threading
import unittest
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast, override
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from scripts.lib.process_group import run_process_group
from scripts.production import investigation, staff
from scripts.review_links import console_theme, new_item_dialog, page_frame, reply_tokens, review_submissions
from scripts.review_links.browser_test_support import firefox_probe_results
from scripts.review_links.test_review_submissions import OWNER, PNG, identify_fixture_image

ENDPOINT = "/console/review/new-item"
RECIPIENTS = [{"id": str(OWNER), "name": "builds", "proposal": True}] + [
    {"id": name, "name": name, "proposal": False}
    for name in ("cargo-liner", "hana", "natedev", "sdlc", "spacetime", "brain", "dwight")]
LIST_PRODUCER = staff.StaffId("fixture-list-producer")
RECIPIENTS.append({"id": str(LIST_PRODUCER), "name": "morty", "proposal": False})
OTHER_RECIPIENT = staff.StaffId("cargo-liner")
FORM_TOKEN = "fixture-form-token"
RESTARTED_TOKEN = "fixture-after-restart"
CAPTURE_KEY = "f" * 32
DISPLAY_WORDS = "The nextest row stops updating during shard 2."
OWNER_REASON = "(why builds? because builds owns the Builds page!)"


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
    <td class="fixture-extra">natedev</td><td></td></tr>
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
const launch = document.querySelector('[data-owner-new]'), png = '__PNG__';
const to = () => [...dialog.querySelectorAll('[name="recipient_id"]')];
const morty = () => to().find(input => input.nextElementSibling.textContent === 'morty');
const selectType = kind => dialog.querySelector('[name="kind"][value="'+kind+'"]').click();
const open = ({kind, page = 'builds'} = {}) => new Promise(resolve => {
 const status = dialog.querySelector('[data-recipients-status]');
 const observer = new MutationObserver(() => {
  if (!status.hidden) return;
  observer.disconnect();
  if (kind) selectType(kind);
  resolve();
 });
 observer.observe(status, {attributes:true, attributeFilter:['hidden']});
 launch.closest('[data-page-owner]').dataset.pageOwner = page;
 launch.click();
});
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
async function sendSaved() {
 const created = new Promise((resolve, reject) => {
  const onCreated = event => { observer.disconnect(); resolve(event); };
  const observer = new MutationObserver(() => {
   if (error.hidden || send.disabled) return;
   observer.disconnect();
   document.removeEventListener('review-item-created', onCreated);
   reject(new Error(error.textContent));
  });
  document.addEventListener('review-item-created', onCreated, {once:true});
  observer.observe(dialog, {attributes:true, subtree:true, attributeFilter:['hidden','disabled']});
 });
 send.click();
 const event = await created;
 check(closeToLauncher(), 'Send closes the dialog and returns focus.');
 return event;
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
   check(document.activeElement.id === 'new-item-title' &&
         dialog.querySelector('[name="kind"]:checked').value === 'bug', 'Bug is selected when the dialog opens.');
   check([...dialog.querySelectorAll('[name="kind"]')].length === 6, 'Six keyboard-selectable types are present.');
   check(!!recipient() && recipient().tabIndex === 0, 'To uses native keyboard-selectable radios.');
   check(send.disabled, 'Send needs words and a recipient.');
   dialog.querySelector('[name="include_page"]').click();
   type('Check <index>\nAgain');
   check(recipient().checked && !send.disabled, 'Words and the selected owner enable Send.');

   const bounds = dialog.getBoundingClientRect();
   check(innerWidth === __WIDTH__, 'Firefox uses the requested viewport width.');
   check(bounds.width === Math.min(560, innerWidth - 32), 'The dialog fits the viewport.');
   check(Math.abs(bounds.left - (innerWidth - bounds.right)) < 1 &&
         Math.abs(bounds.top - (innerHeight - bounds.bottom)) < 1, 'The dialog is centered on both axes.');
   const chips = [...dialog.querySelectorAll('[data-kind]')];
   check(chips[0].offsetTop !== chips.at(-1).offsetTop, 'The Type chips wrap within the dialog.');
   const details = dialog.querySelector('[data-sent-details]');
   check(details.getBoundingClientRect().top > textarea.getBoundingClientRect().bottom,
         'The page details follow the words.');
   const fixedTop = dialog.querySelector('header').getBoundingClientRect().top;
   const fixedBottom = dialog.querySelector('footer').getBoundingClientRect().bottom;
   body.scrollTop = body.scrollHeight;
   check(fixedTop === dialog.querySelector('header').getBoundingClientRect().top &&
         fixedBottom === dialog.querySelector('footer').getBoundingClientRect().bottom,
         'The header and footer remain visible when the body scrolls.');
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
   await open();
   check(textarea.value === 'Check <index>\nAgain' && !details.hidden && recipient().checked &&
         dialog.querySelectorAll('[data-previews] img').length === 1 &&
         error.textContent === 'Fixture rejected the first send.' && !send.disabled,
         'Reopen keeps the failed draft and its error.');
   check((await sendSaved()).detail.id === 'I1', 'The event reports the saved item ID.');

   await open({kind:'question'});
   check(!textarea.value && dialog.querySelector('[data-previews]').hidden, 'Reopen clears the sent draft.');
   check(morty()?.value === 'fixture-list-producer', 'The owner dialog offers morty.');
   const kinds = dialog.querySelector('[name="kind"][value="proposal"]');
   to()[1].click();
   kinds.click();
   check(to().length === 1 && to()[0].value === 'builds-staff' && !to()[0].checked && send.disabled,
         'Proposal clears the selection when the recipient has no proposal folder.');
   dialog.querySelector('[name="kind"][value="question"]').click();
   check(to().length === 9, 'The other types include all recipients.');
   dialog.requestClose();
   check(closeToLauncher(), 'The native cancel action closes the modal and restores focus.');
   await open();
   dialog.querySelector('[name="include_page"]').click();
   const attempted = [], originalFetch = window.fetch;
   window.fetch = (url, options) => {
    if (url !== '/console/review/new-item' || options?.method !== 'POST') return originalFetch(url, options);
    const payload = JSON.parse(options.body);
    if (payload.page_capture) return Promise.resolve(Response.json({error:'No Firefox.'}, {status:503}));
    attempted.push(payload);
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
   type('');
   dialog.querySelector('[data-cancel]').click();
   check(closeToLauncher(), 'Cancel restores focus to the launcher.');

   const ownerPosts = [], ownerEndpoint = '/console/review/new-item?page=builds';
   let pngAvailable = true, discarded, useLiveSends = false, dropPageReply = true, lostHold;
   let captureKey = 'd34db33fd34db33fd34db33fd34db33f';
   let ownerToken = 'owner-token';
   let releasePreview, expireNextSend = false, failNextSend = false;
   let captureNumber = 0, restartScenario = false, filedReply = false;
   let previewGate = new Promise(resolve => { releasePreview = resolve; });
   const json = (value, status = 200) => Promise.resolve(new Response(JSON.stringify(value), {status}));
   window.fetch = (url, options) => {
    if (url === ownerEndpoint) {
     return json({token:ownerToken, page_owner:'builds-staff', recipients:__RECIPIENTS__});
    }
    if (url === '/console/review/new-item?capture_key=' + captureKey) {
     check(options.headers['X-Review-Token'] === ownerToken, 'The preview requests use the form token.');
     return previewGate.then(() => originalFetch('data:image/png;base64,' + png));
    }
    if (url !== '/console/review/new-item' || options?.method !== 'POST') return originalFetch(url, options);
    const hold = new Headers(options.headers).get('X-Undo-Request');
    if (lostHold && hold === lostHold && !options.keepalive) {
     lostHold = null;
     return Promise.reject(new Error('The Send response was lost.'));
    }
    const payload = JSON.parse(options.body);
    ownerPosts.push(payload);
    if (payload.page_capture) {
     if (restartScenario) {
      captureKey = (++captureNumber).toString(16).padStart(32, '0');
      previewGate = Promise.resolve();
     }
     return pngAvailable ? json({capture_key: captureKey}, 202) : json({error:'No Firefox.'}, 503);
    }
    if (payload.discard_capture) {
     discarded?.();
     return json({});
    }
    if (expireNextSend) {
     expireNextSend = false;
     ownerToken = 'restarted-owner-token';
     captureKey = 'e34db33fd34db33fd34db33fd34db33f';
     previewGate = new Promise(resolve => { releasePreview = resolve; });
     return json({code:'screenshot_expired', error:'The screenshot expired.'}, 409);
    }
    if (failNextSend) {
     failNextSend = false;
     return json({error:'Could not publish the item. Try Send again.'}, 503);
    }
    if (filedReply) {
     filedReply = false;
     return json({code:'already_filed', id:'I9', key:'first-item'}, 409);
    }
    if (useLiveSends) {
     const response = originalFetch(url, {...options, body: JSON.stringify({...payload, token: attempted[0].token})});
     return payload.page && dropPageReply ? response.then(saved => {
      if (!saved.ok) return saved;
      dropPageReply = false;
      if (options.keepalive) lostHold = hold;
      throw new Error('The Send response was lost.');
     }) : response;
    }
    return json({id:'I2', key:'saved'});
   };
   const checkbox = dialog.querySelector('[name="include_page"]');
   function whenShown(element) {
    if (!element.hidden) return Promise.resolve();
    return new Promise(resolve => {
     const observer = new MutationObserver(() => { if (!element.hidden) { observer.disconnect(); resolve(); } });
     observer.observe(element, {attributes:true, attributeFilter:['hidden']});
    });
   }
   async function openOwner() {
    await open({kind:'question'});
    if (!pngAvailable) await whenShown(dialog.querySelector('[data-fallback]'));
    check(checkbox.checked && !checkbox.disabled && !checkbox.closest('label').hidden,
          'The page checkbox is checked and available on open.');
    check(to().length === 9 && to()[0].checked && to()[0].value === 'builds-staff', 'The page owner is selected.');
    check(morty() && !morty().checked, 'The page dialog also offers morty.');
    check(to()[0].nextElementSibling.hasAttribute('data-button-face'), 'The owner chip uses the shared button style.');
    const shown = [...dialog.querySelectorAll('[data-kind]:not([hidden])')].map(chip=>chip.textContent.trim());
    check(shown.join(',') === 'Bug,Question,Feedback,Proposal,Investigation,Enhancement',
          'The owner dialog has all six types.');
   }
   async function sendOwner() {
    selectType('feedback');
    type('Show the queue state.');
    return sendSaved();
   }
   await openOwner();
   check(ownerPosts.length === 1 && ownerPosts[0].page_capture.width === innerWidth,
         'Opening the dialog starts one capture at the requested viewport.');
   const slot = dialog.querySelector('[data-capture-slot]');
   const shot = slot.querySelector('[data-shot]');
   check(!slot.hidden && !slot.querySelector('[data-taking]').hidden && shot.hidden,
         'The screenshot slot shows the placeholder while the capture runs.');
   checkbox.click();
   check(slot.hidden, 'Clearing the checkbox hides the capture slot.');
   checkbox.click();
   releasePreview();
   await whenShown(shot);
   const image = shot.href;
   dialog.querySelector('[name="kind"][value="bug"]').click();
   check(!shot.hidden && shot.href === image && ownerPosts.length === 1,
         'Changing to Bug keeps the original image and capture request.');
   await sendOwner();
   check(ownerPosts.length === 2 && ownerPosts[1].capture_key === captureKey &&
         ownerPosts[1].images.length === 0 && ownerPosts[1].evidence.kind === 'none',
         'Send uses the same preview without the fallback rows.');

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
   const running = fallback.sections?.find(section => section.label === 'Running');
   check(running?.columns[0] === 'Step' && running.columns[1] === 'Stage' &&
         running.rows.some(row => row[0] === 'hana nextest' && row[1] === 'testing'),
         'The fallback keeps the displayed build values with their column labels.');
   check(fallback.title === emoji && fallback.rows.some(row=>row.includes(emoji)),
         'The title and rows keep an emoji at the character limit.');
   check(ownerPosts[8].page === 'builds', 'Send includes the page identity.');

   pngAvailable = true;
   await openOwner();
   await whenShown(shot);
   const oldKey = captureKey, oldImage = shot.href, at = ownerPosts.length;
   const expired = dialog.querySelector('[data-capture-expired]');
   type('Keep these Question words after a restart.');
   expireNextSend = true;
   dialog.querySelector('[type="submit"]').click();
   await whenShown(expired);
   check(dialog.open && !checkbox.disabled && !slot.querySelector('[data-taking]').hidden && shot.hidden &&
         !dialog.querySelector('[type="submit"]').disabled, 'Expiration shows the placeholder and keeps Send available.');
   check(textarea.value === 'Keep these Question words after a restart.' &&
         ownerPosts.length === at + 2 && ownerPosts[at].kind === 'question' &&
         ownerPosts[at].capture_key === oldKey && ownerPosts[at].token !== ownerToken &&
         ownerPosts[at + 1].token === ownerToken &&
         ownerPosts[at + 1].page_capture.place === ownerPosts[at - 1].page_capture.place,
         'The restart keeps the Question words and takes a screenshot with the new token.');
   releasePreview();
   await whenShown(shot);
   check(shot.href !== oldImage, 'The new preview replaces the expired image.');
   await sendSaved();
   check(ownerPosts.length === at + 3 && ownerPosts[at + 2].capture_key === captureKey &&
         ownerPosts[at + 2].request_id === ownerPosts[at].request_id &&
         ownerPosts[at + 2].evidence.kind === 'none', 'The second click sends the new screenshot only.');

   await openOwner();
   await whenShown(shot);
   type('Keep this draft when Back restores the page.');
   const discardedOnHide = new Promise(resolve => { discarded = resolve; });
   const beforeRestore = captureKey;
   window.dispatchEvent(new PageTransitionEvent('pagehide', {persisted:true}));
   await discardedOnHide;
   check(shot.hidden && !send.disabled, 'Leaving the page clears the preview and keeps Send available.');
   captureKey = 'f34db33fd34db33fd34db33fd34db33f';
   previewGate = new Promise(resolve => { releasePreview = resolve; });
   window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted:true}));
   check(dialog.open && !slot.querySelector('[data-taking]').hidden && !send.disabled,
         'Back restores the screenshot placeholder and keeps Send available.');
   releasePreview();
   await whenShown(shot);
   dialog.querySelector('[name="kind"][value="bug"]').click();
   await sendSaved();
   const restored = ownerPosts.at(-1);
   check(restored.capture_key === captureKey && restored.capture_key !== beforeRestore &&
         restored.evidence.kind === 'page-data' && restored.evidence.rows.length > 0 &&
         restored.words === 'Keep this draft when Back restores the page.',
         'Send uses the new screenshot and the visible Bug rows after Back.');

   pngAvailable = false;
   useLiveSends = true;
   await openOwner();
   const noScreenshot = dialog.querySelector('[data-fallback]');
   await whenShown(noScreenshot);
   dialog.querySelector('[name="kind"][value="investigation"]').click();
   type('Keep this draft and its image.');
   paste();
   const preview = dialog.querySelector('[data-previews] img');
   await trySend();
   check(error.textContent === 'The Send response was lost.', 'The page Send files before the response is lost.');
   const ownerRequest = ownerPosts.findLast(post => post.request_id);
   dialog.querySelector('[data-cancel]').click();
   await open();
   await whenShown(noScreenshot);
   const previewReply = await originalFetch(preview.src).catch(() => null);
   check(textarea.value === 'Keep this draft and its image.' &&
         dialog.querySelector('[data-previews] img') === preview && previewReply?.ok,
         'Reopen keeps the failed page draft and its pasted image.');
   type('Keep this draft and its image after editing.');
   await trySend();
   check(dialog.open && textarea.value === 'Keep this draft and its image after editing.' &&
         error.textContent === 'This item was already filed as I2. Any change after that Send was not sent.',
         'A changed Send reports the first ID and keeps the draft open.');
   const changedRequest = ownerPosts.findLast(post => post.request_id);
   check(changedRequest.page === 'builds' && changedRequest.images[0].base64_data === png &&
         changedRequest.request_id === ownerRequest.request_id,
         'The first retry uses the old request ID and the current evidence.');
   check((await sendSaved()).detail.id === 'I3' &&
         ownerPosts.findLast(post => post.request_id).request_id !== ownerRequest.request_id,
         'The next Send uses a new ID and files the retained draft.');
   await openOwner();
   dialog.querySelector('[name="kind"][value="investigation"]').click();
   type('The first item before replacement.');
   dropPageReply = true;
   await trySend();
   check(error.textContent === 'The Send response was lost.', 'The first item files before the response is lost.');
   const firstDraft = ownerPosts.at(-1);
   dialog.querySelector('[data-cancel]').click();
   await open();
   type('The replacement item.');
   to()[1].click();
   await trySend();
   check(dialog.open && textarea.value === 'The replacement item.' && to()[1].checked &&
         error.textContent === 'This item was already filed as I4. Any change after that Send was not sent.',
         'The notice shows the saved ID and keeps the changed words.');
   check(ownerPosts.at(-1).request_id === firstDraft.request_id, 'The retry uses the original draft ID.');
   const replacement = (await sendSaved()).detail;
   check(replacement.id === 'I5' && ownerPosts.at(-1).request_id !== firstDraft.request_id,
         'The next Send files the changed words for the selected recipient.');
   await openOwner();
   dialog.querySelector('[name="kind"][value="investigation"]').click();
   const undoWords = 'Restore these words after the edit.';
   type(undoWords);
   dropPageReply = true;
   await trySend();
   check(error.textContent === 'The Send response was lost.', 'F013 files before the response is lost.');
   type('Edit the draft.');
   type(undoWords);
   check((await sendSaved()).detail.id === 'I6', 'F013 returns the first item after undo.');

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
   await openOwner();
   morty().click();
   dialog.querySelector('[name="kind"][value="investigation"]').click();
   type('Check the queue for morty.');
   await sendSaved();
   check(ownerPosts.at(-1).recipient_id === 'fixture-list-producer' &&
         ownerPosts.at(-1).page_recipient === 'selected', 'Send keeps the selected List owner.');

   useLiveSends = false;
   pngAvailable = true;
   for (const checked of [false, true]) {
    await openOwner();
    await whenShown(shot);
    if (!checked) checkbox.click();
    type('Change the screenshot choice after a failed Send.');
    failNextSend = true;
    await trySend();
    const first = ownerPosts.at(-1);
    check(!checkbox.disabled, 'A failed Send keeps the screenshot choice available.');
    checkbox.click();
    dialog.querySelector('[name="kind"][value="bug"]').click();
    await sendSaved();
    const retry = ownerPosts.findLast(post => post.request_id);
    check(retry.request_id === first.request_id && Boolean(retry.capture_key) === !checked &&
          retry.kind === 'bug' && retry.evidence.kind === 'page-data',
          'The retry uses the current kind and evidence for the same draft.');
   }
   restartScenario = true;
   const alreadyFiled = 'This item was already filed as I9. Any change after that Send was not sent.';
   for (const changed of [false, true]) {
    await openOwner();
    await whenShown(shot);
    type('Keep these words after a restart.');
    failNextSend = true;
    await trySend();
    const first = ownerPosts.at(-1);
    if (changed) type('Keep these edited words after a restart.');
    const wordsBeforeRetry = textarea.value;
    filedReply = true;
    await trySend();
    const retry = ownerPosts.findLast(post => post.request_id);
    check(retry.request_id === first.request_id && retry.capture_key === first.capture_key,
          'The retry uses the same draft ID and capture key.');
    check(dialog.open && textarea.value === wordsBeforeRetry &&
          error.textContent === alreadyFiled, 'The notice keeps the draft open with its words.');
    await whenShown(shot);
    const renewedKey = captureKey;
    await sendSaved();
    const next = ownerPosts.findLast(post => post.request_id);
    check(renewedKey !== retry.capture_key && next.capture_key === renewedKey &&
          next.request_id !== retry.request_id, 'The next Send uses a new capture and draft ID.');
   }
   const listPosts = [];
   let listToken = 'list-token-before-restart';
   window.fetch = (url, options) => {
    if (url === '/console/review/new-item?page=list') {
     const morty = {id:'fixture-list-producer',name:'morty'};
     const owner = listToken === 'list-token-before-restart' ? morty : {id:'builds-staff',name:'builds'};
     return originalFetch(url, options).then(async response => {
      const fresh = await response.json();
      return json({...fresh, token:owner === morty ? listToken : fresh.token,
                   page_owner:owner.id, recipients:owner === morty ? [morty] : [owner, morty]});
     });
    }
    if (url === '/console/review/new-item' && options?.method === 'POST') {
     const payload = JSON.parse(options.body);
     if (payload.page_capture) return json({error:'No Firefox.'}, 503);
     listPosts.push(payload);
    }
    return originalFetch(url, options);
   };
   await open({page:'list',kind:'investigation'});
   await whenShown(dialog.querySelector('[data-fallback]'));
   type('Keep these List words.');
   listToken = attempted[0].token;
   await trySend();
   check(dialog.open && textarea.value === 'Keep these List words.' && listPosts.length === 1 && !error.hidden,
         'The List keeps the words and shows the refusal after a restart.');
   const listForm = send.form;
   if (listForm.getAttribute('aria-busy') === 'true') await new Promise(resolve => {
    const observer = new MutationObserver(() => {
     if (listForm.getAttribute('aria-busy') === 'false') { observer.disconnect(); resolve(); }
    });
    observer.observe(listForm, {attributes:true, attributeFilter:['aria-busy']});
   });
   await whenShown(dialog.querySelector('[data-fallback]'));
   check(to().find(input => input.checked)?.value === 'fixture-list-producer' &&
         !to().find(input => input.value === 'builds-staff')?.matches(':disabled') && !textarea.disabled,
         'The List keeps the recipient and lets the user edit the draft.');
   const listCreated = new Promise(resolve =>
    document.addEventListener('review-item-created', resolve, {once:true}));
   send.click();
   await listCreated;
   check(!dialog.open && listPosts.length === 2 && listPosts[0].request_id.startsWith('r6-') &&
         listPosts[1].request_id === listPosts[0].request_id && listPosts[1].words === listPosts[0].words &&
         listPosts[1].recipient_id === listPosts[0].recipient_id && listPosts[1].token === listToken,
         'The retry sends the same List draft and recipient with the current token.');

   window.fetch = (url, options) => {
    if (url === '/console/review/new-item?page=list') {
     return json({token:'', page_owner:'list-staff', recipients:[{id:'list-staff',name:'morty'}],
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


OWNER_OPTIONS_PROBE = r"""
(async () => {
 const failures = [];
 const check = (value, description) => { if (!value) failures.push(description); };
 try {
  const buttons = document.querySelectorAll('.page-heading-actions button');
  const dialog = document.querySelector('#review-new-item');
  const recipients = dialog.querySelector('[data-recipients]');
  const loaded = new Promise(resolve => {
   const observer = new MutationObserver(() => {
    if (recipients.children.length) { observer.disconnect(); resolve(); }
   });
   observer.observe(recipients, {childList:true});
  });
  check(buttons.length === 1 && buttons[0].hasAttribute('data-owner-new'), 'The header has one owner button.');
  buttons[0].click();
  if (!dialog.open) throw new Error('The owner button must open the dialog.');
  await loaded;
  const types = [...dialog.querySelectorAll('[data-kind-label]')].map(label => label.textContent);
  check(types.join(',') === 'Bug,Question,Feedback,Proposal,Investigation,Enhancement', 'All six types appear in order.');
  check(dialog.querySelector('[name="kind"]:checked').value === 'bug', 'Bug starts selected.');
  const inputs = () => [...recipients.querySelectorAll('input')];
  check(inputs().map(input => input.value).join() === __RECIPIENTS__.map(row => row.id).join(),
        'To shows the full recipient set.');
  const selected = () => recipients.querySelector('input:checked')?.value;
  check(selected() === 'builds-staff', 'To starts on the page owner.');
  const reason = dialog.querySelector('[data-owner-reason]');
  const group = dialog.querySelector('[data-recipient-group]');
  const description = () => document.getElementById(group.getAttribute('aria-describedby'));
  const reasonShown = () => !reason.hidden && description()?.textContent === __OWNER_REASON__
   && reason.contains(description());
  check(reasonShown(), 'The To group describes why the page owner is the target.');
  const emoji = reason.querySelector('[aria-hidden="true"]');
  check(emoji?.textContent === '\u{1F986}' && !description().contains(emoji),
        'The duck follows the reason and stays out of the description.');
  const legend = group.querySelector('legend').getBoundingClientRect(), box = group.getBoundingClientRect();
  const chips = recipients.getBoundingClientRect();
  const lines = [...description().getClientRects(), ...emoji.getClientRects()];
  const overlaps = (a, b) => a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom;
  check(lines.length && lines[0].top < legend.bottom, 'The reason starts on the To line.');
  check(lines.every(rect => !overlaps(rect, legend) && rect.bottom <= chips.top
                    && rect.left >= box.left - 0.5 && rect.right <= box.right + 0.5),
        'The reason never clips or overlaps the To legend or chips.');
  check(Math.abs(Math.max(...lines.map(rect => rect.right)) - box.right) < 1,
        'The reason sits at the right side of the To line.');
  const alternate = inputs().find(input => input.value === 'cargo-liner');
  alternate.click();
  check(selected() === alternate.value, 'To accepts another recipient.');
  check(reason.hidden && !group.hasAttribute('aria-describedby'), 'Another recipient hides the reason.');
  inputs().find(input => input.value === 'builds-staff').click();
  check(reasonShown(), 'The page owner chip shows the reason again.');
  alternate.click();
  dialog.querySelector('[name="include_page"]').click();
  const words = dialog.querySelector('textarea'), send = dialog.querySelector('[type="submit"]');
  const status = dialog.querySelector('[data-recipients-status]');
  words.value = 'Check a proposal for this recipient.';
  words.dispatchEvent(new InputEvent('input', {bubbles:true}));
  check(!send.disabled, 'The alternate recipient enables Send.');
  dialog.querySelector('[data-kind="proposal"] input').click();
  check(inputs().length === 1 && !selected() && send.disabled && !status.hidden &&
        status.textContent === 'The recipient you chose is not available. Choose a recipient.',
        'Proposal clears the selection and asks for another recipient.');
  dialog.querySelector('[data-kind="question"] input').click();
  check(inputs().length === __RECIPIENTS__.length && !selected() && send.disabled && !status.hidden,
        'Changing the type back keeps the selection empty.');
  inputs().find(input => input.value === alternate.value).click();
  check(selected() === alternate.value && !send.disabled && status.hidden,
        'An explicit selection enables Send and hides the notice.');
  dialog.querySelector('[data-kind="enhancement"] input').click();
  words.value = 'Show the next check in the row.';
  words.dispatchEvent(new InputEvent('input', {bubbles:true}));
  const saved = new Promise(resolve => document.addEventListener('review-item-saved', resolve, {once:true}));
  send.click();
  check(!dialog.open && document.activeElement === buttons[0], 'Send closes the dialog and restores focus at once.');
  check((await saved).detail.id === 'I1', 'Send reports the saved Enhancement.');
 } catch (error) { failures.push(String(error)); }
 await fetch('/results', {method:'POST', body:JSON.stringify(failures)});
 parent.postMessage('finished', location.origin);
})();
"""


def fixture_page(width: int, *, photo: bool, script: str = PROBE) -> bytes:
    probe = (script.replace("__RECIPIENTS__", json.dumps(RECIPIENTS))
             .replace("__PHOTO__", json.dumps(photo)).replace("__PNG__", PNG).replace("__WIDTH__", str(width))
             .replace("__DISPLAY_WORDS__", json.dumps(DISPLAY_WORDS))
             .replace("__OWNER_REASON__", json.dumps(OWNER_REASON)))
    page = (f'<!doctype html><html lang="en" data-theme="dark"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1"><title>Builds</title>'
            f'<style>{console_theme.CSS}</style></head><body>{SHELL}</body></html>').encode()
    owner = staff.StaffRow(OWNER, "producer", "builds", staff.StaffRoot(), current_session=OWNER)
    return page_frame.with_owner(page, "builds", owner, staff.StaffSnapshot((owner,)), new_item_dialog.render).replace(
        b'</body>', b'<script>' + probe.encode() + b'</script></body>')


BUG_PROBE = r"""
const failures = [];
const check = (value, description) => { if (!value) failures.push(description); };
const wire = [], fetchHTTP = window.fetch;
let holdCapture = __PHOTO__;
let releaseCapture, loseSend = false, lostHold, previewPending = false, previewRequests = 0;
const saved = {id:'I1',key:'bug/owner/I1'};
window.fetch = async (path, options) => {
 if (!String(path).startsWith('/console/review/new-item')) return fetchHTTP(path, options);
 if (String(path).includes('capture_key=')) {
  previewRequests++;
  if (previewPending) {
   previewPending = false;
   return Response.json({}, {status:202});
  }
  return new Response(new Blob(['PNG'], {type:'image/png'}));
 }
 if (!options.body) return Response.json({token:'fixture',page_owner:'owner',recipients:[{id:'owner',name:'sdlc',proposal:true}]});
 const hold = new Headers(options.headers).get('X-Undo-Request');
 if (lostHold && hold === lostHold && !options.keepalive) {
  lostHold = null;
  throw new TypeError('The Send response was lost.');
 }
 const payload = JSON.parse(options.body);
 wire.push(payload);
 if (payload.page_capture) {
  if (!holdCapture) return Response.json({error:'No Firefox.'},{status:503});
  return new Promise(resolve => {
   releaseCapture = () => resolve(Response.json({capture_key:payload.capture_key}));
  });
 }
 if (loseSend) {
  loseSend = false;
  if (options.keepalive) lostHold = hold;
  throw new TypeError('The Send response was lost.');
 }
 return Response.json(saved);
};
const dialog = document.querySelector('dialog');
const checkbox = dialog.querySelector('[name="include_page"]');
const words = dialog.querySelector('textarea');
const state = dialog.querySelector('[data-bug-page-state]');
const term = label => [...state.querySelectorAll('dt')].find(node => node.textContent === label);
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
const whenVisible = element => {
 if (!element.hidden) return Promise.resolve();
 return new Promise(resolve => {
  const observer = new MutationObserver(() => {
   if (!element.hidden) { observer.disconnect(); resolve(); }
  });
  observer.observe(element, {attributes:true, attributeFilter:['hidden']});
 });
};
const send = async () => {
 const created = new Promise(resolve => document.addEventListener('review-item-created', resolve, {once:true}));
 dialog.querySelector('[type="submit"]').click();
 const event = await created;
 check(!dialog.open && event.detail.id === 'I1', 'The Bug dialog closes and reports the saved ID.');
};
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
  check(!state.hidden && state.closest('[data-sent-details]').textContent.includes('Sent with your words'),
        'The Bug dialog lists the page facts with the evidence it sends.');
  check(dialog.querySelector('[data-kind="proposal"]').getClientRects().length > 0,
        'The owner dialog displays Proposal.');
  check(state.textContent.includes('Type All · State Open · Owner builds'), 'The dialog shows the selected filters.');
  const address = term('Address');
  check(address?.nextElementSibling.textContent === location.href, 'The Address row shows the full address.');
  check(state.querySelector('[data-visible-text]').textContent === 'Page data and visible text',
        'The Bug block lists page data and visible text.');
  check(state.textContent.includes('2.40 s') && state.textContent.includes('Could not load …/brain.'),
        'The dialog shows the render time and the notice.');
  const errorLines = term('Error lines')?.nextElementSibling;
  check(errorLines?.classList.contains('error-line') && errorLines.textContent === 'Could not load …/brain.',
        'Error lines shows the notice with the error style.');
  if (!__PHOTO__) {
   const scriptErrors = term('Script errors')?.nextElementSibling;
   check(scriptErrors?.classList.contains('error-line') && scriptErrors.textContent.includes('Rejected'),
         'Script errors shows the rejection with the error style.');
  }
  check(checkbox.checked && checkbox.labels[0].textContent.trim() === 'Include a screenshot',
        'The screenshot box is checked by default.');
  check(wire.length === 1 && wire[0].page_capture.place === location.pathname+location.search+location.hash,
        'Opening the Bug dialog starts the screenshot of the current page.');
  type('Review takes over two seconds with the builds filter. It also shows a load error.');
  if (__PHOTO__) {
   check(!dialog.querySelector('[type="submit"]').disabled && !dialog.querySelector('[data-taking]').hidden,
         'The capture is pending and Send is available.');
   const {x,y,width,height} = dialog.getBoundingClientRect();
   failures.push(JSON.stringify([x,y,width,height]));
  } else {
   checkbox.click();
   document.querySelector('.notice').textContent = 'A later notice.';
   await send();
   const post = wire[1], captured = post.page_state;
   check(wire.length === 2 && !post.capture_key && !captured.include_screenshot,
         'Unchecked Send omits the screenshot.');
   check(post.page === 'review' && captured.url === location.href && !('viewport' in captured),
         'The request includes the page and full address without a viewport.');
   check(captured.render_seconds === 2.4 && captured.notices.join() === 'Could not load …/brain.'
         && post.evidence.kind === 'page-data'
         && post.evidence.rows.some(row=>row.includes('Could not load …/brain.')),
         'The request uses the page facts and visible rows from when the dialog opened.');
   check(captured.script_errors.length === 20 && captured.script_errors[0].message === 'Error 2'
         && captured.script_errors[0].line === 9 && captured.script_errors[0].column === 3
         && captured.script_errors.at(-1).message === 'Rejected', 'The request keeps the last twenty script errors.');

   holdCapture = true;
   await open();
   type('Keep the visible rows when a capture takes too long.');
   if (dialog.querySelector('[type="submit"]').disabled) throw new Error('Send waited for the capture key.');
   await send();
   check(wire.length === 4 && wire[3].capture_key === wire[2].capture_key &&
         wire[3].capture_key.startsWith(wire[3].request_id) &&
         wire[3].defer_capture.place === wire[2].page_capture.place && wire[3].evidence.kind === 'page-data',
         'Send carries the draft capture key before the capture response arrives.');
   const evidence = wire[3].evidence;
   const bytes = new TextEncoder().encode(JSON.stringify(evidence)).length;
   check(bytes <= 64*1024 && evidence.rows.length < 48 &&
         evidence.rows.some(row=>row.includes('😀'.repeat(240))),
         'The fallback keeps complete rows within the UTF-8 byte budget.');
   window.dispatchEvent(new PageTransitionEvent('pagehide'));
   releaseCapture();
   check(!dialog.open && wire.length === 4, 'The page leaves without making an attachment request.');

   await open();
   type('Retry the same Bug after the Send response is lost.');
   loseSend = true;
   dialog.querySelector('[type="submit"]').click();
   await whenVisible(dialog.querySelector('.new-item-error'));
   const first = wire.at(-1);
   const beforePreview = previewRequests;
   previewPending = true;
   releaseCapture();
   await whenVisible(dialog.querySelector('[data-shot]'));
   check(previewRequests === beforePreview + 2, 'The dialog requests the preview again after a pending response.');
   await send();
   const retry = wire.at(-1);
   check(JSON.stringify(retry) === JSON.stringify(first),
         'A preview that arrives after a lost response leaves the retry unchanged.');

   trigger.closest('[data-page-owner]').dataset.pageOwner = 'list';
   await open();
   dialog.querySelector('[name="kind"][value="investigation"]').click();
   type('Keep the pending screenshot after a lost List response.');
   loseSend = true;
   dialog.querySelector('[type="submit"]').click();
   await whenVisible(dialog.querySelector('.new-item-error'));
   const listSend = dialog.querySelector('[type="submit"]');
   if (listSend.disabled) await new Promise(resolve => {
    const observer = new MutationObserver(() => {
     if (!listSend.disabled) { observer.disconnect(); resolve(); }
    });
    observer.observe(listSend, {attributes:true, attributeFilter:['disabled']});
   });
   const listFirst = wire.at(-1);
   const started = wire.length;
   type('Keep these edits for a new Send.');
   const listSaved = new Promise(resolve =>
    document.addEventListener('review-item-saved', resolve, {once:true}));
   listSend.click();
   await listSaved;
   check(listFirst.request_id.startsWith('r6-') && listFirst.capture_key &&
         JSON.stringify(wire[started]) === JSON.stringify(listFirst),
         'The List retries with the original capture key and draft ID.');
   const editNotice = dialog.querySelector('.new-item-error');
   check(dialog.open && words.value === 'Keep these edits for a new Send.' && !editNotice.hidden &&
         editNotice.textContent === 'Saved I1. Your edits are still in the dialog.',
         'The dialog keeps the edits after it confirms the original Send.');

   words.value = '';
   dialog.querySelector('[data-close]').click();
   trigger.closest('[data-page-owner]').dataset.pageOwner = 'review';
   document.querySelector('.notice').remove();
   window.consoleScriptErrors.length = 0;
   await open();
   check(!term('Error lines') && !term('Script errors'),
         'A page with no errors omits both error rows.');
   checkbox.click();
   type('Send the page details without errors.');
   await send();
   const clean = wire.at(-1).page_state;
   check(Array.isArray(clean.notices) && clean.notices.length === 0 &&
         Array.isArray(clean.script_errors) && clean.script_errors.length === 0,
         'The request includes both empty error lists.');
  }
 } catch (error) {
  failures.push(String(error));
 }
 await fetchHTTP('/results', {method:'POST',body:JSON.stringify(failures)});
 parent.postMessage('finished',location.origin);
})();
"""


def bug_fixture_page(*, photo: bool = False, script: str = BUG_PROBE) -> bytes:
    probe = script.replace("__PHOTO__", json.dumps(photo))
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


UNDO_PROBE = r"""
const failures = [];
const check = (value, description) => { if (!value) failures.push(description); };
const wire = [], fetchHTTP = window.fetch, host = window.top.document;
const outcome = __OUTCOME__;
const held = '/send-hold/' + 'a'.repeat(24), saved = {id:'I1',key:'bug/owner/I1'};
let realSends = 0;
window.fetch = async (path, options = {}) => {
 path = String(path);
 if (path === held + '/undo') return Response.json({outcome:'undone'});
 if (path === held) {
  realSends++;
  return outcome === 'failure' ? Response.json({error:'The test send failed.'},{status:503}) : Response.json(saved);
 }
 if (!path.startsWith('/console/review/new-item')) return fetchHTTP(path, options);
 if (!options.body) return Response.json({token:'fixture',page_owner:'owner',recipients:[
  {id:'owner',name:'sdlc',proposal:true}, {id:'chosen',name:'chosen producer',proposal:true},
 ]});
 const payload = JSON.parse(options.body);
 // The screenshot stays pending, so the first Send keeps its item for a retry.
 if (payload.page_capture) return new Promise(() => {});
 wire.push([payload, new Headers(options.headers).get('X-Undo-Hold')]);
 if (wire.length === 1) return Response.json({hold:'a'.repeat(24),seconds:30},{status:202,headers:{'X-Send-Hold':'held'}});
 return Response.json(saved);
};
const dialog = document.querySelector('dialog');
const launch = document.querySelector('[data-owner-new]');
const words = dialog.querySelector('textarea'), error = dialog.querySelector('.new-item-error');
const send = dialog.querySelector('[type="submit"]');
const until = async (test, label) => {
 const end = performance.now() + 8000;
 while (!test()) {
  if (performance.now() > end) throw new Error(label);
  await new Promise(resolve => setTimeout(resolve, 20));
 }
};
const type = text => {
 words.value = text;
 words.dispatchEvent(new Event('input', {bubbles:true}));
};
(async () => {
 try {
  launch.click();
  await until(() => dialog.querySelector('[data-recipients] input:checked'), 'The dialog did not load its recipients.');
  dialog.querySelector('[name="recipient_id"][value="chosen"]').click();
  type('The first words.');
  const input = new DataTransfer();
  const bytes = Uint8Array.from(atob('__PNG__'), character => character.charCodeAt(0));
  input.items.add(new File([bytes], 'pasted.png', {type:'image/png'}));
  const paste = new ClipboardEvent('paste', {cancelable:true});
  Object.defineProperty(paste, 'clipboardData', {value:input});
  words.dispatchEvent(paste);
  const image = dialog.querySelector('[data-previews] img')?.src;
  await until(() => !send.disabled, 'Send did not become available.');
  const events = [];
  for (const name of ['review-item-saved', 'review-item-created']) {
   document.addEventListener(name, event => events.push([name, event.detail.id, dialog.open]));
  }
  send.click();
  check(!dialog.open && document.activeElement === launch, 'Send closes the dialog and returns focus at once.');
  const toast = () => host.querySelector('[data-send-undo]');
  await until(() => toast()?.querySelector('button'), 'The Undo toast did not show.');
  check(toast().querySelector('span').textContent === 'Sent to chosen producer.',
        'The toast names the selected recipient.');
  check(toast().querySelector('button').textContent === 'Undo', 'The toast offers Undo.');
  check(!dialog.open && toast().parentElement.parentElement === host.body && events.length === 0 && realSends === 0,
        'The page stays available while the send waits.');
  if (outcome === 'second-draft') {
   launch.click();
   const second = document.querySelector('dialog[open]');
   check(second && second !== dialog && !second.querySelector('textarea').value,
         'New item opens an empty second draft during the hold.');
   second.querySelector('textarea').value = 'Keep a separate second draft.';
   second.querySelector('[data-close]').click();
  }
  const undo = outcome.startsWith('undo') || outcome === 'second-draft';
  if (undo) toast().querySelector('button').click();
  else {
   const [fade] = toast().getAnimations();
   check(fade.effect.getTiming().duration === 30000, 'The toast uses the duration from the server.');
   fade.finish();
  }
  if (undo || outcome === 'failure') {
   await until(() => dialog.open && !error.hidden && !send.disabled, 'The dialog did not reopen for editing.');
   check(error.textContent === (undo ? 'Undone. Edit and send again.' : 'The test send failed.'),
         'The dialog shows the result above the draft.');
   check(words.value === 'The first words.' && dialog.querySelector('[name="kind"]:checked')?.value === 'bug' &&
         dialog.querySelector('[name="recipient_id"]:checked')?.value === 'chosen',
         'The dialog restores the type, recipient and words.');
   check(image && dialog.querySelector('[data-previews] img')?.src === image, 'The dialog restores the pasted image.');
   check(dialog.querySelector('[name="include_page"]').checked && !dialog.querySelector('[data-capture-slot]').hidden,
         'The dialog restores the pending page capture.');
   check(!toast() && events.length === 0 && realSends === (undo ? 0 : 1),
         'The page sends no success event after Undo or failure.');
   if (outcome === 'second-draft') {
    dialog.querySelector('[data-close]').click();
    launch.click();
    check(document.querySelector('dialog[open] textarea')?.value === 'Keep a separate second draft.',
          'Undo restores the first draft and keeps the second draft separate.');
   }
   if (outcome !== 'undo-resend') return;
   type('The edited words.');
   send.click();
  }
  await until(() => events.length === 2, 'The successful Send did not report the saved item.');
  check(JSON.stringify(events) === JSON.stringify([
   ['review-item-saved', 'I1', false], ['review-item-created', 'I1', false]
  ]), 'Send closes the dialog before it reports the saved item.');
  check(!toast(), 'The Undo toast disappears.');
  if (undo) {
   check(wire.length === 2 && wire[0][0].words === 'The first words.' &&
         wire[1][0].words === 'The edited words.' && wire[1][1] === '1',
         'The Send after Undo posts the edited words.');
   check(wire[0][0].capture_key && wire[1][0].capture_key === wire[0][0].capture_key &&
         JSON.stringify(wire[1][0].defer_capture) === JSON.stringify(wire[0][0].defer_capture),
         'The Send after Undo uses the same page capture.');
  } else {
   check(wire.length === 1 && wire[0][0].kind === 'bug' && wire[0][1] === '1', 'Send holds the Bug once.');
  }
 } catch (failure) {
  failures.push(String(failure));
 } finally {
  await fetchHTTP('/results', {method:'POST', body:JSON.stringify(failures)});
 }
})();
"""

LIST_RETRY_PROBE = r'''
const failures = [];
const check = (ok, description) => { if (!ok) failures.push(description); };
const dialog = document.querySelector('#review-new-item');
const words = dialog.querySelector('textarea'), error = dialog.querySelector('.new-item-error');
const send = dialog.querySelector('[type="submit"]'), shot = dialog.querySelector('[data-shot]');
const type = dialog.querySelector('[name="kind"][value="investigation"]');
const include = dialog.querySelector('[name="include_page"]');
const reloadAfterSend = __RELOAD_AFTER_SEND__;
const firstKey = 'e'.repeat(32), readyKey = 'f'.repeat(32);
const probeKey = 'test-list-first-send', draftPrefix = 'console.list.pending-send.';
const restored = JSON.parse(sessionStorage.getItem(probeKey) || 'null');
const posts = [], fetchHTTP = window.fetch;
let captures = 0, lostHold;
window.fetch = async (path, options) => {
 if (!reloadAfterSend && path.endsWith('?capture_key=' + firstKey)) {
  return fetchHTTP(path.replace(firstKey, readyKey), options);
 }
 if (path === '/console/review/new-item' && options?.method === 'POST') {
  const hold = new Headers(options.headers).get('X-Undo-Request');
  if (lostHold && hold === lostHold && !options.keepalive) {
   lostHold = null;
   throw new TypeError('The Send response was lost.');
  }
  const item = JSON.parse(options.body);
  if (!reloadAfterSend && item.page_capture && ++captures === 1) {
   return Response.json({capture_key:firstKey}, {status:202});
  }
  if (item.discard_capture) return Response.json({});
  if (item.request_id) {
   posts.push(item);
   if (!restored && posts.length === 1) {
    if (reloadAfterSend) {
     const response = await fetchHTTP(path, options);
     if (response.status !== 503) return response;
    }
    if (options.keepalive) lostHold = hold;
    throw new TypeError('The Send response was lost.');
   }
  }
 }
 return fetchHTTP(path, options);
};
function waitFor(test) {
 if (test()) return Promise.resolve();
 return new Promise(resolve => {
  const observer = new MutationObserver(() => { if (test()) { observer.disconnect(); resolve(); } });
  observer.observe(dialog, {attributes:true, childList:true, subtree:true});
 });
}
const sendFinished = () => send.form.getAttribute('aria-busy') === 'false' && !error.hidden;
function editable() {
 return dialog.open && !words.disabled && !type.matches(':disabled') && !include.disabled &&
        !dialog.querySelector('[name="recipient_id"]:checked').matches(':disabled');
}
function enterWords(value) {
 words.value = value;
 words.dispatchEvent(new InputEvent('input', {bubbles:true}));
}
(async () => {
 try {
  document.querySelector('[data-owner-new]').click();
  type.click();
  enterWords(restored ? 'The words after reload.' : 'Keep the first List Send.');
  const evidence = reloadAfterSend ? dialog.querySelector('[data-fallback]') : shot;
  await waitFor(() => !send.disabled && !evidence.hidden);
  send.click();
  await waitFor(sendFinished);
  check(editable(), 'A failed Send leaves the draft editable.');
  if (restored) {
   check(posts.length === 1 && posts[0].request_id === restored.request_id && posts[0].words !== restored.words,
         'Reload keeps the draft ID and sends the edited words.');
   check(error.textContent === 'This item was already filed as I1. Any change after that Send was not sent.',
         'The retry shows the first saved item ID.');
   check(sessionStorage.getItem(draftPrefix + posts[0].request_id) === null,
         'An already filed response clears the draft slot.');
  } else if (reloadAfterSend && !failures.length) {
   sessionStorage.setItem(probeKey, JSON.stringify(posts[0]));
   location.reload();
   return;
  } else if (!reloadAfterSend && !failures.length) {
   const preview = shot.href;
   send.click();
   await waitFor(sendFinished);
   check(error.textContent === 'The screenshot expired.' && editable(),
         'The expired screenshot leaves the draft editable.');
   await waitFor(() => !shot.hidden && shot.href !== preview && !send.disabled);
   const created = new Promise(resolve => document.addEventListener('review-item-created', resolve, {once:true}));
   send.click();
   const event = await Promise.race([
    created, waitFor(sendFinished).then(() => { throw new Error(error.textContent); })
   ]);
   check(event.detail.id === 'I1' && !dialog.open, 'The replacement screenshot saves one item.');
   check(posts.length === 3 && posts.every(item => item.request_id === posts[0].request_id),
         'Each Send uses the same draft ID.');
   check(posts[0].capture_key === firstKey && posts[1].capture_key === firstKey && posts[2].capture_key === readyKey,
         'Send uses the new capture after the first one expires.');
   check(sessionStorage.getItem(draftPrefix + posts[0].request_id) === null,
         'A successful Send clears the draft slot.');
  }
 } catch (error) {
  failures.push(String(error));
 }
 sessionStorage.removeItem(probeKey);
 await fetchHTTP('/results', {method:'POST', body:JSON.stringify(failures)});
 parent.postMessage('finished', location.origin);
})();
'''


TWO_LIST_DRAFTS_PROBE = r"""
const failures = [], posts = [];
const fetchHTTP = window.fetch;
const prefix = 'console.list.pending-send.', probeKey = 'test-list-drafts';
const earlier = JSON.parse(sessionStorage.getItem(probeKey) || 'null');
const endpoint = '/console/review/new-item';
const check = (ok, message) => { if (!ok) failures.push(message); };
window.fetch = async (path, options) => {
 if (path.startsWith('/send-hold/')) return Response.json({id:'I1'});
 if (!path.startsWith(endpoint)) return fetchHTTP(path, options);
 if (!options?.body) return Response.json({token:'fixture',page_owner:'owner',recipients:[
  {id:'owner',name:'sdlc',proposal:true}, {id:'other',name:'producer',proposal:true},
 ]});
 const item = JSON.parse(options.body);
 if (!item.request_id) return Response.json({});
 posts.push(item);
 const hold = String(posts.length).repeat(24);
 return Response.json({hold,seconds:30},{status:202,headers:{'X-Send-Hold':'held'}});
};
async function waitFor(test) {
 const end = performance.now() + 8000;
 while (!test()) {
  if (performance.now() > end) throw new Error('The dialog or toast did not appear.');
  await new Promise(resolve => setTimeout(resolve, 20));
 }
}
async function send(words, select = '') {
 document.querySelector('[data-owner-new]').click();
 const dialog = document.querySelector('dialog[open]');
 await waitFor(() => dialog.querySelector('input[name=recipient_id]:checked'));
 const shown = dialog.querySelector('input[name=recipient_id]:checked').value;
 if (select) dialog.querySelector('input[name=recipient_id][value=' + select + ']').click();
 if (dialog.querySelector('[name=include_page]').checked) dialog.querySelector('[name=include_page]').click();
 const field = dialog.querySelector('textarea');
 field.value = words;
 field.dispatchEvent(new InputEvent('input',{bubbles:true}));
 const host = window.top.document;
 const existing = new Set(host.querySelectorAll('[data-send-undo]'));
 dialog.querySelector('[type=submit]').click();
 const freshToast = () => [...host.querySelectorAll('[data-send-undo]')].find(toast => !existing.has(toast));
 await waitFor(freshToast);
 return {item:posts.at(-1), shown, toast:freshToast()};
}
(async () => {
 try {
  if (!earlier) {
   const first = await send('The first draft.', 'other');
   const second = await send('The second draft.');
   check(first.item.request_id !== second.item.request_id, 'Concurrent sends use different draft IDs.');
   check(first.toast.querySelector('span').textContent === 'Sent to producer.' &&
         second.toast.querySelector('span').textContent === 'Sent to sdlc.', 'The first sends name their recipients.');
   check(sessionStorage.getItem(prefix + first.item.request_id) === 'other' &&
         sessionStorage.getItem(prefix + second.item.request_id) === 'owner',
         'Each send stores its own recovery slot.');
   if (!failures.length) {
    sessionStorage.setItem(probeKey, JSON.stringify(posts));
    location.reload();
    return;
   }
  } else {
   const pending = new Map(earlier.map(item => [item.request_id, item]));
   for (const _ of earlier) {
    const created = new Promise(resolve => document.addEventListener('review-item-created', resolve, {once:true}));
    const {item, shown, toast} = await send('Recover a draft.', 'other');
    check(pending.get(item.request_id)?.recipient_id === shown, 'Reload restores the draft ID and its recipient.');
    check(toast.querySelector('span').textContent === 'Sending.', 'A retry that can recover a filing shows Sending.');
    toast.getAnimations()[0].finish();
    await created;
    pending.delete(item.request_id);
    check(sessionStorage.getItem(prefix + item.request_id) === null, 'A save removes its own recovery slot.');
    check([...pending.keys()].every(id => sessionStorage.getItem(prefix + id) !== null),
          'A save keeps the other recovery slot.');
   }
   check(!pending.size, 'The dialog recovers both drafts.');
  }
 } catch (error) { failures.push(String(error)); }
 sessionStorage.removeItem(probeKey);
 await fetchHTTP('/results',{method:'POST',body:JSON.stringify(failures)});
})();
"""


RECIPIENT_DRAFT_PROBE = r'''
const failures = [];
const check = (ok, description) => { if (!ok) failures.push(description); };
const dialog = document.querySelector('#review-new-item');
const send = dialog.querySelector('[type="submit"]'), words = dialog.querySelector('textarea');
const status = dialog.querySelector('[data-recipients-status]');
const launch = document.querySelector('[data-owner-new]');
const include = dialog.querySelector('[name="include_page"]');
const selected = () => dialog.querySelector('[name="recipient_id"]:checked')?.value;
const alternate = () => dialog.querySelector('[name="recipient_id"][value="cargo-liner"]');
const selectType = kind => dialog.querySelector('[name="kind"][value="'+kind+'"]').click();
const scenario = __SCENARIO__, probeKey = 'test-recipient-draft';
const earlier = JSON.parse(sessionStorage.getItem(probeKey) || 'null');
const fetchHTTP = window.fetch;
let firstRequest;
if (scenario === 'reload') {
 window.fetch = (path, options) => {
  if (path === '/console/review/new-item' && options?.method === 'POST') {
   const item = JSON.parse(options.body);
   if (item.request_id) {
    if (!earlier) {
     firstRequest = item.request_id;
     return Promise.reject(new TypeError('Send did not reach the server.'));
    }
    check(item.request_id === earlier.requestId, 'Send keeps the unfiled draft ID after reload.');
   }
  }
  return fetchHTTP(path, options);
 };
}
function waitFor(test) {
 if (test()) return Promise.resolve();
 return new Promise(resolve => {
  const observer = new MutationObserver(() => { if (test()) { observer.disconnect(); resolve(); } });
  observer.observe(dialog, {attributes:true, childList:true, subtree:true});
 });
}
async function open() {
 launch.click();
 await waitFor(() => status.textContent !== 'Loading recipients…');
 if (include.checked) include.click();
}
function checkEmpty() {
 check(!selected() && send.disabled && !status.hidden &&
       status.textContent === 'The recipient you chose is not available. Choose a recipient.',
       'The dialog asks for a recipient and disables Send.');
}
(async () => {
 try {
  await open();
  selectType('investigation');
  words.value = 'Keep the recipient for this draft.';
  words.dispatchEvent(new InputEvent('input', {bubbles:true}));
  if (earlier?.cleared) {
   checkEmpty();
   check(!!alternate(), 'The recipient is available again after the reload.');
   if (!failures.length) {
    alternate().click();
    check(!send.disabled, 'An explicit recipient choice enables Send after reload.');
    if (!send.disabled) {
     send.click();
     await waitFor(() => send.form.getAttribute('aria-busy') === 'false');
     check(!dialog.open, 'The retained draft saves for the selected recipient.');
    }
   }
  } else if (earlier) {
   check(selected() === 'cargo-liner' && !send.disabled, 'Reload restores the selected recipient.');
   selectType('proposal');
   checkEmpty();
   selectType('investigation');
   checkEmpty();
   if (!failures.length) {
    sessionStorage.setItem(probeKey, JSON.stringify({...earlier, cleared:true}));
    location.reload();
    return;
   }
  } else {
   alternate().click();
   check(selected() === 'cargo-liner' && !send.disabled, 'The user selected a recipient before retirement or reload.');
   if (scenario === 'reopen') {
    dialog.requestClose();
    await fetch('/restart', {method:'POST'});
    await open();
    check(words.value === 'Keep the recipient for this draft.', 'Reopen preserves the unsent draft.');
    checkEmpty();
   } else {
    if (scenario === 'refresh') await fetch('/restart', {method:'POST'});
    send.click();
    await waitFor(() => send.form.getAttribute('aria-busy') === 'false');
    if (scenario === 'refresh') {
     check(!alternate(), 'The refreshed options exclude the retired recipient.');
     checkEmpty();
    } else if (!failures.length) {
     if (!firstRequest) throw new Error('The dialog did not send the draft.');
     sessionStorage.setItem(probeKey, JSON.stringify({requestId:firstRequest}));
     location.reload();
     return;
    }
   }
  }
 } catch (error) {
  failures.push(String(error));
 }
 sessionStorage.removeItem(probeKey);
 await fetchHTTP('/results', {method:'POST', body:JSON.stringify(failures)});
 parent.postMessage('finished', location.origin);
})();
'''


@dataclass
class DialogFixture:
    root: Path
    results: list[str] = field(default_factory=list)
    posts: list[review_submissions.NewItemRequest] = field(default_factory=list)
    wire_posts: list[dict[str, object]] = field(default_factory=list)
    token: str = FORM_TOKEN
    recipient_reads: int = 0
    completed: bool = False
    screenshot: new_item_dialog.PageScreenshot | new_item_dialog.NoPageScreenshot = new_item_dialog.NO_PAGE_SCREENSHOT
    reject_first_send: bool = True
    retire_recipient_on_restart: bool = False
    owner_reason: str = ""

    def __post_init__(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        _ = (self.root / "staff.json").write_text('{"schema": 2, "rows": []}\n')

    def environment(self) -> dict[str, str]:
        return {"SHOWRUNNER_STATE_DIR": str(self.root / "state"), "REVIEW_LINKS_ROOT": str(self.root / "review"),
                "XDG_STATE_HOME": str(self.root / "key-state"), "CONF_DIR": str(self.root / "config"),
                "STAFF_FILE": str(self.root / "staff.json")}


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
      frame.src='/case/'+width+'?width='+width;
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
            width = urlsplit(self.headers["Referer"]).path.removeprefix("/case/")
            return fixtures[int(width)]

        def do_GET(self) -> None:
            address = urlsplit(self.path)
            if address.path == ENDPOINT:
                fixture = self.fixture()
                if parse_qs(address.query).get("capture_key") == [CAPTURE_KEY]:
                    image = fixture.screenshot
                    if isinstance(image, new_item_dialog.PageScreenshot):
                        self.response(200, "image/png", image.png)
                        return
                fixture.recipient_reads += 1
                recipients = RECIPIENTS
                if fixture.retire_recipient_on_restart and fixture.token == RESTARTED_TOKEN:
                    recipients = [row for row in RECIPIENTS if row["id"] != OTHER_RECIPIENT]
                self.send_json(200, {"token": fixture.token, "page_owner": OWNER,
                                     "page_owner_reason": fixture.owner_reason, "recipients": recipients})
            elif self.path == "/complete":
                _ = finished.wait(60)
                self.response(204, "image/png", b"")
            elif self.path == "/":
                self.response(200, "text/html; charset=utf-8", outer)
            elif address.path.startswith("/case/"):
                width = int(address.path.removeprefix("/case/"))
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
            if "page_capture" in fields:
                if isinstance(fixture.screenshot, new_item_dialog.PageScreenshot):
                    self.send_json(202, {"capture_key": CAPTURE_KEY})
                else:
                    self.send_json(503, {"error": "No Firefox."})
                return
            fixture.wire_posts.append(fields)
            if fields.get("token") != fixture.token:
                self.send_json(403, {"code": "stale_token", "error": "The New item token expired. Try Send again."})
                return
            try:
                request = review_submissions.read_request(fields)
                fixture.posts.append(request)
                image = fixture.screenshot if fields.get("capture_key") == CAPTURE_KEY else new_item_dialog.NO_PAGE_SCREENSHOT
                with (patch.dict(os.environ, fixture.environment()),
                      patch.object(Path, "home", return_value=fixture.root),
                      patch.object(reply_tokens, "platform", "linux")):
                    item = review_submissions.submit(request, screenshot=image,
                        capture_requested=request.page == "list" and "capture_key" in fields)
            except new_item_dialog.SubmissionScreenshotExpired as error:
                self.send_json(409, {"code": "screenshot_expired", "error": str(error)})
                return
            except review_submissions.SubmissionConflict as error:
                self.send_json(error.status, {"reason": str(error)})
                return
            except (OSError, ValueError) as error:
                self.send_json(400, {"reason": str(error)})
                return
            if fixture.reject_first_send and len(fixture.posts) == 1:
                self.send_json(503, {"error": "Fixture rejected the first send."})
                return
            if isinstance(item, review_submissions.EditedSubmission):
                self.send_json(409, {"code": "already_filed", "id": item.id})
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
            run = run_process_group(
                ["firefox", "--headless", "--no-remote", "--profile", str(profile),
                 "--window-size", "1440,900", "--screenshot", str(output),
                 f"http://127.0.0.1:{server.server_port}/"],
                timeout=75)
            if run.returncode:
                raise AssertionError(run.stderr)
            if not finished.is_set():
                missing = [width for width, fixture in fixtures.items() if not fixture.completed]
                raise AssertionError(f"Firefox did not report results for dialog fixtures {missing}.")
        finally:
            server.shutdown()
            thread.join(1)


class NewItemDialogTests(unittest.TestCase):
    def test_legacy_reply_token_has_the_same_digest_as_the_stored_proof(self) -> None:
        token = "a" * 43
        details = new_item_dialog.ReviewSubmission("fixture", "b" * 64, new_item_dialog.NoEvidence(), (),
                                                  reply_tokens.digest(token))
        fields = new_item_dialog.stored_submission(details)
        self.assertEqual(fields.get("reply_token_sha256"), reply_tokens.digest(token))
        self.assertNotIn(token, json.dumps(fields))
        del fields["reply_token_sha256"]
        fields["reply_token"] = token
        restored = new_item_dialog.read_submission(fields)
        self.assertEqual(restored, details)
        new_item_dialog.require_reply_token(restored, token)
        with self.assertRaises(new_item_dialog.SubmissionError):
            new_item_dialog.require_reply_token(restored, "b" * 43)

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required for dialog behavior")
    def test_list_token_refresh_clears_a_retired_recipient(self) -> None:
        fixture = self.check_recipient_draft("refresh")
        self.assertEqual([post["recipient_id"] for post in fixture.wire_posts], [OTHER_RECIPIENT])
        self.assertEqual(fixture.posts, [])

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required for dialog behavior")
    def test_list_reload_restores_the_unfiled_recipient_and_preserves_an_empty_selection(self) -> None:
        fixture = self.check_recipient_draft("reload")
        item, = fixture.posts
        self.assertEqual(item.recipient_id, OTHER_RECIPIENT)
        with patch.dict(os.environ, fixture.environment()):
            record, = investigation.records(OTHER_RECIPIENT)
        self.assertEqual((record.id, record.question), ("I1", item.words))

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required for dialog behavior")
    def test_reopening_a_kept_unsent_draft_clears_a_retired_recipient(self) -> None:
        fixture = self.check_recipient_draft("reopen")
        self.assertEqual(fixture.wire_posts, [])

    def check_recipient_draft(self, scenario: str) -> DialogFixture:
        temporary = Path(self.enterContext(tempfile.TemporaryDirectory()))
        fixture = DialogFixture(temporary, reject_first_send=False, retire_recipient_on_restart=scenario != "reload")
        probe = RECIPIENT_DRAFT_PROBE.replace("__SCENARIO__", json.dumps(scenario))
        page = fixture_page(1440, photo=False, script=probe)
        if scenario != "reopen":
            page = page.replace(b'data-page-owner="builds"', b'data-page-owner="list"')
        owner = staff.StaffRow(OWNER, "producer", "builds", staff.StaffRoot(), current_session=OWNER)
        alternate = staff.StaffRow(OTHER_RECIPIENT, "producer", "cargo-liner", staff.StaffRoot(),
                                   current_session=OTHER_RECIPIENT)
        configuration = temporary / "config"
        configuration.mkdir()
        _ = (configuration / "console.conf").write_text(f"page.list.owner={owner.id}\n")
        with patch.object(staff, "snapshot", return_value=staff.StaffSnapshot((owner, alternate))):
            capture_dialogs(temporary / "recipient-draft.png", {1440: fixture}, pages={1440: page})
        self.assertEqual(fixture.results, [])
        return fixture

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required for dialog behavior")
    def test_list_send_renews_an_expired_screenshot_after_the_first_send_is_lost(self) -> None:
        self.check_list_retry(reload_after_send=False)

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required for dialog behavior")
    def test_list_send_after_reload_returns_the_item_from_the_lost_reply(self) -> None:
        self.check_list_retry(reload_after_send=True)

    def check_list_retry(self, *, reload_after_send: bool) -> None:
        temporary = Path(self.enterContext(tempfile.TemporaryDirectory()))
        owner = staff.StaffRow(OWNER, "producer", "builds", staff.StaffRoot(), current_session=OWNER)
        configuration = temporary / "config"
        configuration.mkdir()
        _ = (configuration / "console.conf").write_text(f"page.list.owner={OWNER}\n")
        image = (new_item_dialog.NO_PAGE_SCREENSHOT if reload_after_send else new_item_dialog.PageScreenshot(
            base64.b64decode(PNG), new_item_dialog.PageViewport(1440, 900, True)))
        fixture = DialogFixture(temporary, screenshot=image, reject_first_send=reload_after_send)
        probe = LIST_RETRY_PROBE.replace("__RELOAD_AFTER_SEND__", json.dumps(reload_after_send))
        page = fixture_page(1440, photo=False, script=probe).replace(
            b'data-page-owner="builds"', b'data-page-owner="list"')
        with patch.object(staff, "snapshot", return_value=staff.StaffSnapshot((owner,))):
            capture_dialogs(temporary / "list-retry.png", {1440: fixture}, pages={1440: page})
        self.assertEqual(fixture.results, [])
        self.assertEqual(len(fixture.posts), 2)
        self.assertEqual(fixture.posts[0].request_id, fixture.posts[1].request_id)
        with patch.dict(os.environ, fixture.environment()):
            record, = investigation.records(owner.id)
        self.assertEqual((record.id, record.question), ("I1", "Keep the first List Send."))
        submission = record.submission
        assert isinstance(submission, new_item_dialog.ReviewSubmission)
        self.assertEqual(submission.images, () if reload_after_send else (review_submissions.PAGE_SCREENSHOT,))

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required for dialog behavior")
    def test_bug_dialog_reports_the_open_page_even_without_a_screenshot(self) -> None:
        temporary = Path(self.enterContext(tempfile.TemporaryDirectory()))
        fixture = DialogFixture(temporary)
        capture_dialogs(temporary / "bug.png", {1440: fixture}, pages={1440: bug_fixture_page()})
        self.assertEqual(fixture.results, [])

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required for dialog behavior")
    def test_send_after_undo_posts_the_edited_words(self) -> None:
        self.assert_held_send("undo-resend")

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required for dialog behavior")
    def test_non_list_send_closes_the_dialog_at_send(self) -> None:
        self.assert_held_send("success")

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required for dialog behavior")
    def test_undo_reopens_the_new_item_with_the_words(self) -> None:
        self.assert_held_send("undo")

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required for dialog behavior")
    def test_new_item_opens_a_second_draft_during_the_hold(self) -> None:
        self.assert_held_send("second-draft")

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required for dialog behavior")
    def test_list_recovers_two_unresolved_drafts_after_reload(self) -> None:
        page = bug_fixture_page(script=TWO_LIST_DRAFTS_PROBE).replace(
            b'data-page-owner="review"', b'data-page-owner="list"')
        self.assertEqual(firefox_probe_results(page), [[]])

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required for dialog behavior")
    def test_failed_held_send_reopens_the_new_item_with_the_error(self) -> None:
        self.assert_held_send("failure")

    def assert_held_send(self, outcome: str) -> None:
        probe = UNDO_PROBE.replace("__OUTCOME__", json.dumps(outcome)).replace("__PNG__", PNG)
        self.assertEqual(firefox_probe_results(bug_fixture_page(script=probe)), [[]])

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required for dialog behavior")
    def test_owner_button_selects_the_owner_and_sends_an_enhancement(self) -> None:
        temporary = Path(self.enterContext(tempfile.TemporaryDirectory()))
        owner = staff.StaffRow(OWNER, "producer", "builds", staff.StaffRoot(), current_session=OWNER)
        alternate = staff.StaffRow(OTHER_RECIPIENT, "producer", "cargo-liner", staff.StaffRoot(),
                                   current_session=OTHER_RECIPIENT)
        fixtures = {width: DialogFixture(temporary / str(width), reject_first_send=False, owner_reason=OWNER_REASON)
                    for width in (1440, 390)}
        for fixture in fixtures.values():
            configuration = fixture.root / "config"
            configuration.mkdir(parents=True)
            _ = (configuration / "console.conf").write_text(f"page.builds.owner={OWNER}\n")
        with patch.object(staff, "snapshot", return_value=staff.StaffSnapshot((owner, alternate))):
            capture_dialogs(temporary / "options.png", fixtures,
                            pages={width: fixture_page(width, photo=False, script=OWNER_OPTIONS_PROBE) for width in fixtures})
        for width, fixture in fixtures.items():
            with self.subTest(width=width), patch.dict(os.environ, fixture.environment()):
                self.assertEqual(fixture.results, [])
                post, = fixture.wire_posts
                self.assertEqual((post["kind"], post["recipient_id"], post["words"]),
                                 ("enhancement", OTHER_RECIPIENT, "Show the next check in the row."))
                record, = investigation.records(OTHER_RECIPIENT)
                self.assertEqual(record.title, "Enhancement: Show the next check in the row.")

    def test_render_mounts_one_dialog_with_six_types(self) -> None:
        markup = new_item_dialog.render()
        self.assertEqual(markup.count('<dialog id="review-new-item"'), 1)
        self.assertIn('name="words" rows="4"', markup)
        self.assertEqual(markup.count('type="radio" name="kind"'), 6)
        self.assertEqual(markup.count('<span data-button-face>'), 6)
        self.assertIn('data-kind="bug"', markup)
        self.assertIn('data-kind="enhancement"', markup)
        self.assertIn('class="lucide lucide-sparkles"', markup)
        self.assertIn('<svg aria-hidden="true"', markup)
        self.assertIn('name="include_page" checked', markup)
        self.assertIn('data-sent-details aria-labelledby="sent-details-title"', markup)
        self.assertIn('data-capture-slot hidden', markup)

    def test_page_data_and_capture_size_are_checked(self) -> None:
        fields = {"kind": "page-data", "title": "The List", "rows": [["Item", "State"], ["Console", "Open"]]}
        self.assertEqual(new_item_dialog.read_evidence(fields),
                         new_item_dialog.PageDataEvidence("The List", (("Item", "State"), ("Console", "Open"))))
        section = {"label": "Items", "columns": ["Item", "State"], "rows": [["Console", "Open"]]}
        structured = new_item_dialog.read_evidence({**fields, "sections": [section]})
        self.assertEqual(structured, new_item_dialog.PageDataEvidence(
            "The List", (("Item", "State"), ("Console", "Open")),
            (new_item_dialog.PageDataSection("Items", ("Item", "State"), (("Console", "Open"),)),)))
        self.assertEqual(new_item_dialog.evidence_fields(new_item_dialog.read_evidence(fields))["rows"],
                         (("Item", "State"), ("Console", "Open")))
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
        morty = staff.StaffRow(LIST_PRODUCER, "producer", "morty", staff.StaffRoot(),
                               current_session=staff.StaffId("fixture-morty-session"))
        snapshot = staff.StaffSnapshot((recipient, other, morty))
        _ = self.enterContext(patch.object(staff, "snapshot", return_value=snapshot))
        _ = self.enterContext(patch.object(review_submissions, "_identify_image", autospec=True,
                                           side_effect=identify_fixture_image))
        fixtures = {width: DialogFixture(temporary / str(width)) for width in (1440, 390)}
        for fixture in fixtures.values():
            configuration = fixture.root / "config"
            configuration.mkdir(parents=True)
            _ = (configuration / "console.conf").write_text(
                f"page.builds.owner={OWNER}\npage.list.owner={morty.id}\n")
        capture_dialogs(temporary / "dialog.png", fixtures)
        for width, fixture in fixtures.items():
            with self.subTest(width=width), patch.dict(os.environ, fixture.environment()):
                self.assertEqual(fixture.results, [])
                records = investigation.records(OWNER)
                self.assertEqual([item.id for item in records], ["I1", "I2", "I3", "I4", "I6", "I7", "I8"])
                original = records[3]
                for report, title in zip(records[-2:], ("First queue state", "Second queue state"), strict=True):
                    self.assertEqual(report.question, "Show the queue state.")
                    assert isinstance(report.submission, new_item_dialog.ReviewSubmission)
                    evidence = report.submission.evidence
                    assert isinstance(evidence, new_item_dialog.PageDataEvidence)
                    self.assertEqual(evidence.title, title)
                changed, = investigation.records(OTHER_RECIPIENT)
                self.assertEqual((changed.id, changed.question), ("I5", "The replacement item."))
                for_morty, list_retry = investigation.records(LIST_PRODUCER)
                self.assertEqual((for_morty.owner, for_morty.question), (morty.id, "Check the queue for morty."))
                self.assertEqual((list_retry.owner, list_retry.question), (morty.id, "Keep these List words."))
                self.assertEqual(original.question, "The first item before replacement.")
                self.assertGreaterEqual(fixture.recipient_reads, 8)
                expired, refreshed, reopened, *owner_sends, list_expired, list_sent = fixture.wire_posts
                self.assertEqual(expired["token"], FORM_TOKEN)
                self.assertEqual(refreshed, {**expired, "token": RESTARTED_TOKEN})
                self.assertEqual(reopened, refreshed)
                self.assertEqual(list_sent, {**list_expired, "token": RESTARTED_TOKEN})
                self.assertEqual(len(owner_sends), 11)
                owner_ids = [post["request_id"] for post in owner_sends]
                self.assertEqual([owner_ids[0], owner_ids[3], owner_ids[6]], [owner_ids[1], owner_ids[4], owner_ids[7]])
                self.assertEqual(len({expired["request_id"], *owner_ids}), 9)
                first, retry = fixture.posts[:2]
                self.assertEqual(first, retry)
                self.assertEqual(first.kind, review_submissions.SubmissionKind.BUG)
                self.assertEqual(first.recipient_id, OWNER)
                self.assertEqual(first.evidence.kind, "page-data")
                self.assertEqual(first.images, (review_submissions.PastedImage("image/png", PNG),))
