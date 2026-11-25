"""Test the New item dialog against a private HTTP fixture."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import tempfile
import threading
import unittest
from dataclasses import asdict, dataclass, field, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast, override
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from scripts.console_text.test_support import accepting_console_text
from scripts.lib.process_group import run_process_group
from scripts.production import investigation, staff
from scripts.review_links import (console_theme, new_item_dialog, page_frame, reply_tokens, review_page,
                                  review_submissions, serve)
from scripts.review_links.browser_test_support import firefox_probe_results
from scripts.review_links.test_review_submissions import EVIDENCE, OWNER, PNG, identify_fixture_image

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
function stageLaterEdit(edit) {
 const later = dialog.querySelector('[data-later-draft]');
 later.click();
 check(!textarea.readOnly && send.disabled && later.textContent === 'Return to Retry',
       'The separate draft is editable and waits for Retry.');
 edit();
 later.click();
}
async function confirmEarlierSend() {
 const saved = new Promise(resolve => dialog.addEventListener('review-item-saved', resolve, {once:true}));
 send.click();
 return await saved;
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
   check(dialog.querySelectorAll('[name="kind"]').length === 7, 'Seven keyboard-selectable types are present.');
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
   check(attempted[0].request_id === attempted[1].request_id && attempted[1].request_id !== attempted[2].request_id &&
         attempted[0].recipient_id !== attempted[1].recipient_id && attempted[1].kind !== attempted[2].kind,
         'Changing the recipient keeps the draft ID. Changing the kind creates a new ID.');
   type('');
   dialog.querySelector('[data-cancel]').click();
   check(closeToLauncher(), 'Cancel restores focus to the launcher.');

   const ownerPosts = [], ownerEndpoint = '/console/review/new-item?page=builds';
   let pngAvailable = true, discarded, useLiveSends = false, dropPageReply = true;
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
      throw new Error('The Send response was lost.');
     }) : response;
    }
    return json({id:'I2', key:'saved'});
   };
   const checkbox = dialog.querySelector('[name="include_page"]');
   function requestsSince(start) {
    return ownerPosts.slice(start).map(post => post.page_capture ? 'capture' :
     post.discard_capture ? 'discard' : post.request_id ? 'send' : 'unknown').sort().join(',');
   }
   const discardsSince = start => ownerPosts.slice(start).filter(post => post.discard_capture);
   function waitForDiscards(start, count) {
    return new Promise((resolve, reject) => {
     const deadline = setTimeout(() => {
      discarded = undefined;
      reject(new Error('The page did not discard the expected captures.'));
     }, 4000);
     discarded = () => {
      if (discardsSince(start).length < count) return;
      clearTimeout(deadline);
      discarded = undefined;
      resolve();
     };
     discarded();
    });
   }
   function whenShown(element) {
    if (!element.hidden) return Promise.resolve();
    return new Promise(resolve => {
     const observer = new MutationObserver(() => { if (!element.hidden) { observer.disconnect(); resolve(); } });
     observer.observe(element, {attributes:true, attributeFilter:['hidden']});
    });
   }
   async function openOwner(kind = 'question') {
    await open({kind});
    if (!pngAvailable) await whenShown(dialog.querySelector('[data-fallback]'));
    check(checkbox.checked && !checkbox.disabled && !checkbox.closest('label').hidden,
          'The page checkbox is checked and available on open.');
    check(to().length === 9 && to()[0].checked && to()[0].value === 'builds-staff', 'The page owner is selected.');
    check(morty() && !morty().checked, 'The page dialog also offers morty.');
    check(to()[0].nextElementSibling.hasAttribute('data-button-face'), 'The owner chip uses the shared button style.');
    const shown = [...dialog.querySelectorAll('[data-kind]:not([hidden])')].map(chip=>chip.textContent.trim());
    check(shown.join(',') === 'Bug,Question,Feedback,Proposal,Investigation,Enhancement,Scheduled', 'The dialog has all seven types.');
   }
   async function sendOwner() {
    selectType('feedback');
    if (checkbox.checked) await whenShown(pngAvailable ? shot : dialog.querySelector('[data-fallback]'));
    type('Show the queue state.');
    return sendSaved();
   }
   await openOwner('bug');
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
   selectType('question');
   await whenShown(shot);
   const captures = ownerPosts.filter(post => post.page_capture);
   check(shot.href !== image && captures.length === 2 &&
         captures[0].capture_key.slice(0, 35) !== captures[1].capture_key.slice(0, 35),
         'Changing the kind starts a capture for the new draft ID.');
   const beforeCapturedSend = ownerPosts.length;
   await sendOwner();
   const capturedSend = ownerPosts.at(-1);
   check(beforeCapturedSend === 3 && requestsSince(beforeCapturedSend) === 'capture,discard,send' &&
         ownerPosts[beforeCapturedSend + 1].discard_capture === captureKey &&
         capturedSend.capture_key === captureKey && capturedSend.images.length === 0 &&
         capturedSend.evidence.kind === 'none',
         'Send includes the new capture without the fallback rows.' + ' Requests: ' + requestsSince(beforeCapturedSend));

   const beforeSkipped = ownerPosts.length;
   await openOwner();
   checkbox.click();
   const skippedCapture = waitForDiscards(beforeSkipped, 2);
   await sendOwner();
   await skippedCapture;
   const skipped = ownerPosts.findLast(post => post.request_id);
   check(requestsSince(beforeSkipped) === 'capture,capture,discard,discard,send' &&
         !skipped.capture_key && skipped.evidence.kind === 'none' &&
         discardsSince(beforeSkipped).every(post => post.discard_capture === captureKey),
         'Send with the box unchecked discards the capture.' + ' Requests: ' + requestsSince(beforeSkipped));

   const beforeCancel = ownerPosts.length;
   await openOwner();
   await whenShown(shot);
   const canceled = waitForDiscards(beforeCancel, 2);
   dialog.querySelector('[data-cancel]').click();
   await canceled;
   check(requestsSince(beforeCancel) === 'capture,capture,discard,discard' &&
         ownerPosts.at(-1).discard_capture === captureKey && !dialog.open,
         'Closing the dialog discards the capture.' + ' Requests: ' + requestsSince(beforeCancel));

   pngAvailable = false;
   const emoji = 'x'.repeat(239) + '😀';
   const main = document.querySelector('main');
   main.querySelector('h1').textContent = emoji + 'overflow';
   const paragraph = document.createElement('p');
   paragraph.textContent = emoji + 'overflow';
   main.append(paragraph);
   const beforeFallback = ownerPosts.length;
   await openOwner();
   await sendOwner();
   const fallbackSend = ownerPosts.at(-1);
   const fallback = fallbackSend.evidence;
   check(requestsSince(beforeFallback) === 'capture,capture,capture,send' && !fallbackSend.capture_key && fallback.kind === 'page-data' &&
         fallback.rows.some(row=>row.includes('hana nextest')), 'No Firefox sends visible rows.' + ' Requests: ' + requestsSince(beforeFallback));
   const running = fallback.sections?.find(section => section.label === 'Running');
   check(running?.columns[0] === 'Step' && running.columns[1] === 'Stage' &&
         running.rows.some(row => row[0] === 'hana nextest' && row[1] === 'testing'),
         'The fallback keeps the displayed build values with their column labels.');
   check(fallback.title === emoji && fallback.rows.some(row=>row.includes(emoji)),
         'The title and rows keep an emoji at the character limit.');
   check(fallbackSend.page === 'builds', 'Send includes the page identity.');

   pngAvailable = true;
   await openOwner();
   await whenShown(shot);
   const oldKey = captureKey, oldImage = shot.href, at = ownerPosts.length;
   const previousCapture = ownerPosts.findLast(post => post.page_capture).page_capture;
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
         ownerPosts[at + 1].page_capture.place === previousCapture.place,
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
   await whenShown(shot);
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
   await trySend();
   check(error.textContent === 'The Send response was lost.', 'The page Send files before the response is lost.');
   const ownerRequest = ownerPosts.findLast(post => post.request_id);
   stageLaterEdit(() => type('Keep this draft and its image after editing.'));
   dialog.querySelector('[data-cancel]').click();
   await open();
   await whenShown(noScreenshot);
   const preview = dialog.querySelector('[data-previews] img');
   const previewReply = await originalFetch(preview.src).catch(() => null);
   check(textarea.value === ownerRequest.words && textarea.readOnly && previewReply?.ok &&
         send.textContent === 'Retry', 'Reopen locks the submitted words and preserves the pasted image.');
   check((await confirmEarlierSend()).detail.id === 'I2' && dialog.open &&
         textarea.value === 'Keep this draft and its image after editing.',
         'Retry confirms the original item and restores the edited draft.');
   const retried = ownerPosts.findLast(post => post.request_id);
   check(JSON.stringify(retried) === JSON.stringify(ownerRequest),
         'Retry sends the original ID, page, and pasted image.');
   check((await sendSaved()).detail.id === 'I3' &&
         ownerPosts.findLast(post => post.request_id).request_id !== ownerRequest.request_id &&
         ownerPosts.findLast(post => post.request_id).images[0].base64_data === png,
         'The next Send uses a new ID and files the retained words and image.');
   await openOwner();
   dialog.querySelector('[name="kind"][value="investigation"]').click();
   type('The first item before replacement.');
   dropPageReply = true;
   await trySend();
   check(error.textContent === 'The Send response was lost.', 'The first item files before the response is lost.');
   const firstDraft = ownerPosts.at(-1);
   stageLaterEdit(() => {
    type('The replacement item.');
    to()[1].click();
   });
   dialog.querySelector('[data-cancel]').click();
   await open();
   check(textarea.value === firstDraft.words && textarea.readOnly && send.textContent === 'Retry' &&
         to().find(input => input.checked).value === firstDraft.recipient_id,
         'Reopen locks the submitted recipient and words.');
   check((await confirmEarlierSend()).detail.id === 'I4' && dialog.open &&
         textarea.value === 'The replacement item.' && to()[1].checked,
         'Retry confirms the saved ID and restores the changed recipient and words.');
   check(JSON.stringify(ownerPosts.findLast(post => post.request_id)) === JSON.stringify(firstDraft),
         'Retry uses the original payload.');
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
   stageLaterEdit(() => {
    type('Edit the draft.');
    type(undoWords);
   });
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
    check(checkbox.disabled && textarea.readOnly && send.textContent === 'Retry',
          'An uncertain Send locks the submitted screenshot choice.');
    stageLaterEdit(() => {
     checkbox.click();
     dialog.querySelector('[name="kind"][value="bug"]').click();
    });
    await confirmEarlierSend();
    const retry = ownerPosts.findLast(post => post.request_id);
    check(JSON.stringify(retry) === JSON.stringify(first), 'Retry sends the submitted screenshot choice and kind.');
    if (!checked) await whenShown(shot);
    await sendSaved();
    const next = ownerPosts.findLast(post => post.request_id);
    check(next.request_id !== first.request_id && Boolean(next.capture_key) === !checked &&
          next.kind === 'bug' && next.evidence.kind === 'page-data',
          'The separate draft sends the changed kind and evidence under a new ID.');
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
    const wordsBeforeRetry = changed ? 'Keep these edited words after a restart.' : textarea.value;
    if (changed) stageLaterEdit(() => type(wordsBeforeRetry));
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
  check(types.join(',') === 'Bug,Question,Feedback,Proposal,Investigation,Enhancement,Scheduled', 'All seven types appear in order.');
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
  const scheduledInput = dialog.querySelector('[data-kind="scheduled"] input');
  scheduledInput.click();
  const scheduled = dialog.querySelector('[data-scheduled-help]');
  const hidden = element => getComputedStyle(element).display === 'none';
  check(!hidden(send) && !send.disabled && !hidden(scheduled) && hidden(dialog.querySelector('[data-sent-details]')) &&
        hidden(dialog.querySelector('[data-saved-controls]')),
        'Scheduled keeps Send and shows the schedule fields.');
  dialog.querySelector('[data-kind="proposal"] input').click();
  check(scheduled.hidden && !send.hidden && !dialog.querySelector('[data-sent-details]').hidden,
        'The other types can send from the dialog.');
  check(inputs().length === 1 && !selected() && send.disabled && !status.hidden &&
        status.textContent === 'The recipient you chose is not available. Choose a recipient.',
        'Proposal clears the selection and asks for another recipient.');
  dialog.querySelector('[data-kind="question"] input').click();
  check(inputs().length === __RECIPIENTS__.length && !selected() && send.disabled && !status.hidden,
        'Changing the type back keeps the selection empty.');
  inputs().find(input => input.value === alternate.value).click();
  check(selected() === alternate.value && !send.disabled && status.hidden,
        'An explicit selection enables Send and hides the notice.');
  if (__SEND_SCHEDULED__) {
   scheduledInput.click();
   words.value = 'Check the queue. Start Fri Jan 15 2027 13:00. Repeat Daily.';
   words.dispatchEvent(new InputEvent('input', {bubbles:true}));
   const sendResponse = window.fetch;
   let posted;
   window.fetch = (path, options) => {
    if (path === '/console/review/new-item' && options?.body) {
     const payload = JSON.parse(options.body);
     if (payload.kind === 'scheduled') {
      posted = payload;
      return Promise.resolve(Response.json({id:'L12', receipt:'Scheduled → cargo-liner. Producer: morty. List item 12.', readback:[
       'Starts: Fri Jan 15 2027 13:00 PST (16:00 EST)', 'Repeats: Daily'
      ]}));
     }
    }
    return sendResponse(path, options);
   };
   const saved = new Promise(resolve => dialog.addEventListener('review-item-saved', resolve, {once:true}));
   send.click();
   await saved;
   check(posted?.recipient_id === alternate.value && posted.words === words.value &&
         posted.page_recipient === 'selected' && !('capture_key' in posted),
         'Scheduled sends the selected owner and words without capture fields.');
   check(/^s1-[0-9a-f]{32}$/.test(posted?.request_id), 'Changing the kind to Scheduled creates a Scheduled draft ID.');
   check(!hidden(dialog.querySelector('[data-saved-controls]')) && scheduledInput.checked &&
         dialog.querySelector('#new-item-saved').textContent.includes('Saved List item 12.') &&
         dialog.querySelector('#new-item-saved').textContent.includes('Repeats: Daily'),
         'The confirmation shows the saved List number, repeat, and selected type.');
   check(dialog.querySelector('#new-item-saved').textContent.includes('Producer: morty.'),
         'The confirmation names the mapped producer.');
  } else {
   dialog.querySelector('[data-kind="enhancement"] input').click();
   words.value = 'Show the next check in the row.';
   words.dispatchEvent(new InputEvent('input', {bubbles:true}));
   const saved = new Promise(resolve => document.addEventListener('review-item-saved', resolve, {once:true}));
   send.click();
   check(!dialog.open && document.activeElement === buttons[0], 'Send closes the dialog and restores focus at once.');
   check((await saved).detail.id === 'I1', 'Send reports the saved Enhancement.');
  }
 } catch (error) { failures.push(String(error)); }
 await fetch('/results', {method:'POST', body:JSON.stringify(failures)});
 parent.postMessage('finished', location.origin);
})();
"""


RESTORED_SEND_PROBE = r"""
(async () => {
 const failures = [];
 const check = (value, description) => { if (!value) failures.push(description); };
 const dialog = document.querySelector('#review-new-item');
 const button = document.querySelector('[data-owner-new]');
 const words = dialog.querySelector('textarea'), send = dialog.querySelector('[type="submit"]');
 const kind = '__KIND__', scenario = '__SCENARIO__';
 const selectedKind = dialog.querySelector('[name="kind"][value="' + kind + '"]');
 const recipient = id => dialog.querySelector('[name="recipient_id"][value="' + id + '"]');
 let original = JSON.parse(sessionStorage.getItem('test-restored-send') || 'null');
 const http = window.fetch;
 let posted, expired = false;
 const captureChoice = ['renew', 'omit'].includes(scenario);
 const warning = scenario === 'missing-attachment';
 const deferred = warning || ['reopen', 'open', 'unconfirmed'].includes(scenario);
 window.fetch = async (path, options) => {
  if (path.startsWith('/console/review/new-item?capture_key=')) {
   return new Response(Uint8Array.from(atob('__PNG__'), c => c.charCodeAt(0)), {headers:{'Content-Type':'image/png'}});
  }
  if (path === '/console/review/new-item' && options?.body) {
   const payload = JSON.parse(options.body);
   if (payload.page_capture && deferred) return new Promise(() => {});
   if (payload.page_capture && captureChoice) return Response.json({capture_key:payload.capture_key}, {status:202});
   if (payload.discard_capture) return Response.json({});
   if (payload.kind === kind) {
    posted = payload;
    if (!original && scenario === 'unconfirmed') {
     return Response.json({error:'The result is uncertain.'}, {status:503});
    }
    if (!original) throw new TypeError('The Send response was lost.');
    if (captureChoice && !expired) {
     expired = true;
     return Response.json({code:'screenshot_expired', error:'The screenshot expired.'}, {status:409});
    }
    if (warning && payload.request_id === original.request_id) {
     return Response.json({id:'I1', attachment_warning:'The saved screenshot is missing or damaged.'});
    }
    return Response.json(kind === 'scheduled'
     ? {id:'L12', receipt:'Scheduled → cargo-liner. Producer: morty. List item 12.'} : {id:'I1'});
   }
  }
  return http(path, options);
 };
 function ready(test) {
  if (test()) return Promise.resolve();
  return new Promise(resolve => {
   const observer = new MutationObserver(() => { if (test()) { observer.disconnect(); resolve(); } });
   observer.observe(dialog, {attributes:true, childList:true, subtree:true});
  });
 }
 function enter(value) {
  words.value = value;
  words.dispatchEvent(new InputEvent('input', {bubbles:true}));
 }
 try {
  button.closest('[data-page-owner]').dataset.pageOwner = original ? 'review' : '__FIRST_PAGE__';
  button.click();
  const include = dialog.querySelector('[name="include_page"]');
  const withCapture = captureChoice || deferred;
  if (!original && !withCapture && include.checked) include.click();
  await ready(() => !!recipient('cargo-liner') && (!original || !send.disabled));
  if (!original) {
   selectedKind.click();
   recipient('cargo-liner').click();
   enter('Check the queue. Start Mon Nov 30 09:00.');
   if (kind === 'bug') {
    const clipboard = new DataTransfer();
    clipboard.items.add(new File([Uint8Array.from(atob('__PNG__'), c => c.charCodeAt(0))], 'queue.png', {type:'image/png'}));
    const event = new ClipboardEvent('paste', {cancelable:true});
    Object.defineProperty(event, 'clipboardData', {value: clipboard});
    words.dispatchEvent(event);
   }
   if (captureChoice) await ready(() => !dialog.querySelector('[data-shot]').hidden);
   if (deferred) await ready(() => !dialog.querySelector('[data-capture-slot]').hidden);
   send.click();
   await ready(() => posted && send.form.getAttribute('aria-busy') === 'false');
   const namespace = kind === 'scheduled' ? /^s1-/ : /^r6-/;
   check(namespace.test(posted.request_id), 'The first Send uses the namespace for its kind.');
   check(send.textContent === 'Retry' && !send.disabled && words.readOnly &&
         recipient('cargo-liner').matches(':disabled'), 'An uncertain Send locks the submitted fields immediately.');
   if (deferred) {
    check(Boolean(posted.defer_capture), 'The uncertain Bug Send includes a deferred screenshot.');
    original = posted;
    const later = dialog.querySelector('[data-later-draft]');
    later.click();
    check(!words.readOnly && send.disabled, 'The separate draft cannot send before Retry.');
    if (scenario === 'open') {
     const scheduled = dialog.querySelector('[name="kind"][value="scheduled"]');
     scheduled.click();
     check(scheduled.checked && send.disabled, 'The separate draft can select Scheduled before Retry.');
     selectedKind.click();
    }
    enter('Keep my later edit.');
    recipient('builds-staff').click();
    if (scenario === 'reopen') {
     dialog.querySelector('[data-cancel]').click();
     button.click();
     await ready(() => !!recipient('cargo-liner') && !send.disabled);
    } else {
     later.click();
    }
   } else if (!failures.length) {
    sessionStorage.setItem('test-restored-send', JSON.stringify(posted));
    location.reload();
    return;
   }
  }
  if (original) {
   check(selectedKind.checked && recipient('cargo-liner').checked && words.value === original.words,
         'New item restores the kind, recipient, and words.');
   check(words.readOnly && selectedKind.matches(':disabled') && recipient('cargo-liner').matches(':disabled') &&
         include.disabled && include.checked === withCapture && send.textContent === 'Retry',
         'The restored draft locks the submitted fields and displays Retry.');
   if (kind === 'bug') {
    const attachment = dialog.querySelector('[data-previews] button');
    check(attachment?.disabled, 'The restored Bug keeps its pasted image and locks its remove button.');
   }
   recipient('builds-staff').click();
   check(recipient('cargo-liner').checked, 'The submitted recipient cannot change during recovery.');
   if (captureChoice) {
    send.click();
    const choices = dialog.querySelector('[data-replace-capture]');
    await ready(() => !choices.hidden && send.form.getAttribute('aria-busy') === 'false');
    check(JSON.stringify(posted) === JSON.stringify(original), 'Retry first sends the original payload.');
    check(send.disabled && words.readOnly && dialog.querySelector('[data-bug-page-state]').textContent.includes(original.page_state.url),
          'An expired screenshot pauses Retry and keeps the submitted Bug data.');
    choices.querySelector('[data-' + scenario + '-capture]').click();
    await ready(() => choices.hidden && !send.disabled);
   }
   const saved = new Promise(resolve => dialog.addEventListener('review-item-saved', resolve, {once:true}));
   send.click();
   await saved;
   if (captureChoice) {
    const {capture_key, defer_capture, page_state, ...sent} = posted;
    const {capture_key: previousKey, page_state: previousState, ...previous} = original;
    check(JSON.stringify(sent) === JSON.stringify(previous) &&
          JSON.stringify(page_state) === JSON.stringify({...previousState, include_screenshot:false}),
          'Retry preserves the draft ID, words, images, and page data after the screenshot choice.');
    check(scenario === 'omit' ? !capture_key && !defer_capture
     : capture_key !== previousKey && capture_key.startsWith(original.request_id) &&
       defer_capture.place === new URL(original.page_state.url).pathname + new URL(original.page_state.url).search,
     'Retry uses the chosen screenshot action for the original page.');
   } else {
    check(JSON.stringify(posted) === JSON.stringify(original), 'Retry sends the original payload.');
   }
   if (warning) {
    const done = dialog.querySelector('[data-done]');
    const confirmation = dialog.querySelector('#new-item-saved');
    check(confirmation.textContent === 'Saved I1.\nThe saved screenshot is missing or damaged.' &&
          !confirmation.hidden && document.activeElement === done && send.disabled,
          'A saved item with a missing screenshot displays a warning and asks for acknowledgment.');
    const stored = sessionStorage.getItem('console.new-item.submitted');
    dialog.dispatchEvent(new Event('cancel', {cancelable:true}));
    check(dialog.open && stored && sessionStorage.getItem('console.new-item.submitted') === stored,
          'The submitted draft stays until acknowledgment.');
    done.click();
   }
   if (deferred) {
    check(dialog.open && !words.readOnly && words.value === 'Keep my later edit.' && recipient('builds-staff').checked,
          'The later edits return in a separate draft after the receipt.');
    const next = new Promise(resolve => dialog.addEventListener('review-item-saved', resolve, {once:true}));
    send.click();
    await next;
    check(posted.request_id !== original.request_id && posted.words === 'Keep my later edit.',
          'The later edits use a new draft ID.');
    check(/^r6-/.test(posted.request_id), 'The separate Bug draft uses the native namespace.');
   }
   check(!sessionStorage.getItem('console.new-item.submitted'), 'The receipt clears the submitted draft.');
  }
 } catch (error) { failures.push(String(error)); }
 sessionStorage.removeItem('test-restored-send');
 await http('/results', {method:'POST', body:JSON.stringify(failures)});
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


ROW_EVIDENCE_PROBE = r"""
(async () => {
 const failures = [];
 const check = (value, description) => { if (!value) failures.push(description); };
 try {
  let dialog;
  const field = selector => dialog.querySelector(selector);
  const words = () => field('textarea'), send = () => field('[type="submit"]');
  const status = () => field('[data-recipients-status]'), error = () => field('.new-item-error');
  const evidence = () => field('[data-evidence]');
  const include = () => field('[name="include_page"]').closest('label');
  const capture = __CAPTURE__, other = {...capture, step_id: 'other-step', title: 'cargo test', tip: []};
  const cells = document.querySelectorAll('.fixture-active td:last-child, .fixture-active + tr td:last-child');
  const [first, second] = [...cells].map(cell => {
   const button = document.createElement('button');
   button.type = 'button';
   button.textContent = '+';
   cell.append(button);
   return button;
  });
  const until = test => test() ? Promise.resolve() : new Promise(resolve => {
   const observer = new MutationObserver(() => { if (test()) { observer.disconnect(); resolve(); } });
   observer.observe(dialog, {attributes: true, childList: true, subtree: true, characterData: true});
  });
  const selected = () => dialog.querySelector('[name="recipient_id"]:checked')?.value;
  async function openRow(trigger, data) {
   dialog = window.reviewNewItem.open({trigger, recipientName: 'builds', evidence: data});
   await until(() => status().textContent !== 'Loading recipients…');
  }
  async function save(id, trigger) {
   const saved = new Promise(resolve => document.addEventListener('review-item-created', resolve, {once: true}));
   send().click();
   check((await saved).detail.id === id, 'Send reports the saved row item ID.');
   check(!dialog.open && document.activeElement === trigger, 'Send returns focus to the row.');
  }
  function type(text) {
   words().value = text;
   words().dispatchEvent(new InputEvent('input', {bubbles: true}));
  }
  check(window.reviewNewItem.evidenceLimits.tipEntries === 64, 'The launcher API exposes the evidence limits.');
  await openRow(first, capture);
  check(dialog.querySelector('[name="kind"]:checked').value === 'question', 'A row opens a Question.');
  check(selected() === 'builds-staff', 'A row selects builds by name.');
  check(!evidence().hidden && evidence().querySelector('h4').textContent === 'hana nextest', 'The row evidence shows.');
  const memory = evidence().querySelector('.new-item-memory');
  check(memory.querySelector('strong').textContent === '17.9 GiB' &&
        memory.querySelector('.new-item-cap').textContent === 'Queue cap 27.9 GiB' &&
        memory.querySelector('.new-item-memory-legend span').textContent === 'Builds 9.9 GiB',
        'The evidence uses the displayed amounts from the capture.');
  check(include().hidden && dialog.querySelector('[data-capture-slot]').hidden &&
        dialog.querySelector('[data-bug-page-state]').hidden, 'A row sends no screenshot or page state.');
  type('Why does this step wait?');
  dialog.querySelector('[data-cancel]').click();
  await openRow(second, other);
  check(!words().value && evidence().querySelector('h4').textContent === 'cargo test',
        'Another row opens with its own evidence and an empty draft.');
  dialog.querySelector('[data-cancel]').click();
  await openRow(first, {...capture, title: 'A newer reading'});
  check(words().value === 'Why does this step wait?' && evidence().querySelector('h4').textContent === 'hana nextest',
        'Reopen keeps the row draft and its evidence.');
  dialog.querySelector('[name="kind"][value="investigation"]').click();
  send().click();
  await until(() => !error().hidden);
  check(error().textContent === 'Fixture rejected the first send.' && words().value, 'A failed send keeps the draft.');
  await save('I1', first);
  await openRow(second, other);
  type('What does this step run?');
  await save('Q1', second);
  document.querySelector('[data-owner-new]').click();
  dialog = document.querySelector('dialog[open]');
  await until(() => status().textContent !== 'Loading recipients…');
  check(evidence().hidden && !include().hidden && dialog.querySelector('[name="kind"]:checked').value === 'bug',
        'The page owner button still opens the page dialog without row evidence.');
  dialog.querySelector('[data-cancel]').click();
 } catch (failure) { failures.push(String(failure)); }
 await fetch('/results', {method: 'POST', body: JSON.stringify(failures)});
 parent.postMessage('finished', location.origin);
})();
"""


def row_evidence_page() -> bytes:
    captured = asdict(EVIDENCE)
    captured["memory"] = {
        "free_for_next_gib": EVIDENCE.memory.free_for_next_gib, "queue_cap_gib": EVIDENCE.memory.queue_cap_gib,
        "free_text": "17.9 GiB", "cap_text": "27.9 GiB",
        "segments": [{**asdict(segment), "text": text} for segment, text in zip(
            EVIDENCE.memory.segments, ("9.9 GiB", "30.0 GiB", "2.0 GiB", "8.0 GiB", "29.0 GiB"), strict=True)],
    }
    return fixture_page(1440, photo=False, script=ROW_EVIDENCE_PROBE.replace("__CAPTURE__", json.dumps(captured)))


BUG_PROBE = r"""
const failures = [];
const check = (value, description) => { if (!value) failures.push(description); };
const wire = [], fetchHTTP = window.fetch;
let holdCapture = __PHOTO__;
let releaseCapture, loseSend = false, previewPending = false, previewRequests = 0;
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
   const scheduled = dialog.querySelector('[data-kind="scheduled"] input');
   scheduled.click();
   check(scheduled.disabled && !scheduled.checked &&
         dialog.querySelector('[name="kind"]:checked').value === 'bug',
         'A pending Bug prevents a change to Scheduled.');
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
   check(!scheduled.disabled, 'Recovering the earlier Bug enables Scheduled.');
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
   const later = dialog.querySelector('[data-later-draft]');
   check(words.readOnly && listSend.textContent === 'Retry', 'A lost List response locks the submitted fields.');
   later.click();
   type('Keep these edits for a new Send.');
   later.click();
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
const reloadAfterSend = __RELOAD_AFTER_SEND__, shippedPointer = __SHIPPED_POINTER__;
const shippedUnsent = __SHIPPED_UNSENT__, later = dialog.querySelector('[data-later-draft]');
const launch = document.querySelector('[data-owner-new]');
const scheduled = dialog.querySelector('[name="kind"][value="scheduled"]');
const firstKey = 'e'.repeat(32), readyKey = 'f'.repeat(32);
const probeKey = 'test-list-first-send', draftPrefix = 'console.list.pending-send.';
const draftKey = 'console.list.pending-send';
const restored = JSON.parse(sessionStorage.getItem(probeKey) || 'null');
function storeShippedPointer(id, value) {
 sessionStorage.removeItem(draftPrefix + id);
 sessionStorage.removeItem(draftPrefix + 'state.' + id);
 sessionStorage.setItem(draftKey, value);
}
history.replaceState(null, '', '/console/list.html' + location.search);
const posts = [], fetchHTTP = window.fetch;
let captures = 0;
window.fetch = async (path, options) => {
 if (!reloadAfterSend && path.endsWith('?capture_key=' + firstKey)) {
  return fetchHTTP(path.replace(firstKey, readyKey), options);
 }
 if (path === '/console/review/new-item' && options?.method === 'POST') {
  const item = JSON.parse(options.body);
  if (!reloadAfterSend && item.page_capture && ++captures === 1) {
   return Response.json({capture_key:firstKey}, {status:202});
  }
  if (shippedUnsent && item.page_capture) return Response.json({error:'No Firefox.'}, {status:503});
  if (item.discard_capture) return Response.json({});
  if (item.request_id) {
   posts.push(item);
   if (shippedUnsent && item.kind === 'scheduled') {
    if (!restored.scheduled) throw new TypeError('The Scheduled reply was lost.');
    return Response.json({id:'L1'});
   }
   if (!restored && posts.length === 1) {
    if (reloadAfterSend) {
     const response = await fetchHTTP(path, options);
     if (response.status !== 503) return response;
    }
    throw new TypeError('The Send response was lost.');
   }
  }
 }
 return fetchHTTP(path, options);
};
function waitFor(test) {
 if (test()) return Promise.resolve();
 return new Promise((resolve, reject) => {
  const observer = new MutationObserver(() => {
   if (!test()) return;
   clearTimeout(deadline);
   observer.disconnect();
   resolve();
  });
  const deadline = setTimeout(() => {
   observer.disconnect();
   reject(new Error('The List draft did not reach the expected state.'));
  }, 8000);
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
function pasteListImage() {
 const clipboard = new DataTransfer();
 clipboard.items.add(new File([Uint8Array.from(atob('__PNG__'), char => char.charCodeAt(0))], 'pasted.png',
                              {type:'image/png'}));
 const event = new ClipboardEvent('paste', {cancelable:true});
 Object.defineProperty(event, 'clipboardData', {value: clipboard});
 words.dispatchEvent(event);
}
async function checkSeparateScheduledDraft() {
 const pending = () => {
  const key = Object.keys(sessionStorage).find(key => key.startsWith(draftPrefix + 'r6-'));
  return key && {id: key.slice(draftPrefix.length), recipient: sessionStorage.getItem(key)};
 };
 const recipientsReady = () => !!dialog.querySelector('[name="recipient_id"]:checked');
 const waitForStep = (test, step) => Promise.race([
  waitFor(test), new Promise((resolve, reject) => setTimeout(() => reject(new Error(step)), 2500))
 ]);
 const scheduleWords = 'Check the List. Start 2099-11-30T09:00Z.';
 if (!restored) {
  await waitForStep(recipientsReady, 'The first List form did not load its recipient.');
  dialog.querySelector('[name="recipient_id"]:checked').dispatchEvent(new Event('input', {bubbles:true}));
  const {id, recipient} = pending();
  check(/^r6-/.test(id) && !posts.length, 'Recipient selection stores a native draft before Send.');
  storeShippedPointer(id, JSON.stringify({id, recipient}));
  sessionStorage.setItem(probeKey, JSON.stringify({id, recipient}));
 } else if (!restored.scheduled) {
  await waitForStep(recipientsReady, 'The shipped draft did not restore its recipient.');
  type.click();
  enterWords('Keep the first List Send.');
  pasteListImage();
  check(!later.hidden && scheduled.disabled, 'The shipped pointer offers a separate draft.');
  later.click();
  check(scheduled.checked && !words.value, 'The separate Scheduled draft has its own fields.');
  enterWords(scheduleWords);
  dialog.querySelector('[data-cancel]').click();
  launch.click();
  await waitForStep(recipientsReady, 'The native draft did not reopen.');
  check(type.checked && words.value === 'Keep the first List Send.' &&
        dialog.querySelectorAll('[data-previews] img').length === 1, 'Reopen shows the native words and image.');
  later.click();
  check(scheduled.checked && words.value === scheduleWords && !send.disabled, 'The separate draft keeps its words.');
  send.click();
  await waitForStep(sendFinished, 'The Scheduled Send did not return the lost reply.');
  check(posts.length === 1 && /^s1-/.test(posts[0].request_id) && posts[0].words === scheduleWords,
        'The first Scheduled Send uses a fresh ID.');
  check(pending().id === restored.id, 'The Scheduled Send keeps the shipped pointer.');
  sessionStorage.setItem(probeKey, JSON.stringify({...restored, scheduled:posts[0]}));
 } else {
  await waitForStep(() => send.textContent === 'Retry' && !send.disabled, 'The Scheduled Retry did not recover on reload.');
  send.click();
  await waitForStep(() => !dialog.querySelector('#new-item-saved').hidden, 'The Scheduled Retry did not show confirmation.');
  check(posts.length === 1 && JSON.stringify(posts[0]) === JSON.stringify(restored.scheduled) &&
        dialog.querySelector('#new-item-saved').textContent.startsWith('Saved List item 1.'),
        'Scheduled Retry uses the same Send and confirms the saved item.');
  check(pending().id === restored.id, 'Scheduled confirmation keeps the shipped pointer.');
  dialog.querySelector('[data-done]').click();
  launch.click();
  await waitForStep(recipientsReady, 'The native draft did not open after Scheduled confirmation.');
  check(type.checked && words.value === 'Keep the first List Send.' &&
        dialog.querySelectorAll('[data-previews] img').length === 1, 'Scheduled Retry preserves the native words and image.');
  if (send.disabled) throw new Error('The native Send is disabled.');
  const saved = new Promise(resolve => dialog.addEventListener('review-item-created', resolve, {once:true}));
  send.click();
  await Promise.race([saved, waitForStep(sendFinished, 'The native Send did not finish.').then(() => { throw new Error(error.textContent); })]);
  check(posts.length === 2 && posts[1].request_id === restored.id && posts[1].recipient_id === restored.recipient,
        'The native Send still uses the shipped ID and recipient.');
  check(!pending(), 'The native confirmation clears the shipped pointer.');
  return true;
 }
 if (failures.length) return true;
 location.reload();
 return false;
}
async function checkListRetry() {
  if (!restored) {
   type.click();
   enterWords('Keep the first List Send.');
   if (shippedPointer) pasteListImage();
   const evidence = reloadAfterSend ? dialog.querySelector('[data-fallback]') : shot;
   await waitFor(() => !send.disabled && !evidence.hidden);
  } else if (shippedPointer) {
   await waitFor(() => !!dialog.querySelector('[name="recipient_id"]'));
   check(scheduled.disabled && !words.readOnly, 'A shipped draft permits native recovery only.');
   scheduled.click();
   check(send.form.elements.kind.value === 'bug', 'Scheduled cannot replace the shipped draft.');
   type.click();
   check(scheduled.disabled && type.checked, 'The restored draft permits changing the native kind.');
   if (shippedPointer === 'id') dialog.querySelector('[name="recipient_id"][value="' + restored.recipient_id + '"]').click();
   enterWords(restored.words);
  } else {
   await waitFor(() => !send.disabled);
   check(words.value === restored.words && words.readOnly && type.matches(':disabled') && send.textContent === 'Retry',
         'Reload locks the submitted List draft and displays Retry.');
  }
  if (send.disabled) throw new Error('Send is disabled.');
  if (restored && !shippedPointer) {
   const created = new Promise(resolve => document.addEventListener('review-item-created', resolve, {once:true}));
   send.click();
   const result = await Promise.race([
    created, waitFor(sendFinished).then(() => { throw new Error(error.textContent); })
   ]);
   check(posts.length === 1 && JSON.stringify(posts[0]) === JSON.stringify(restored),
         'Reload keeps the draft ID and sends the original payload.');
   check(result.detail.id === 'I1' && !dialog.open, 'The original Send returns the saved item and closes the dialog.');
   check(sessionStorage.getItem(draftPrefix + posts[0].request_id) === null,
         'The saved response clears the draft slot.');
   return true;
  }
  send.click();
  await waitFor(sendFinished);
  if (restored) {
   check(editable() && !words.readOnly, 'A saved item response unlocks the draft.');
   check(posts.length === 1 && posts[0].request_id === restored.request_id && posts[0].kind === restored.kind &&
         posts[0].recipient_id === restored.recipient_id && posts[0].words === restored.words && !posts[0].images.length,
         'Native recovery uses the shipped ID, kind, recipient, and words.');
   check(!scheduled.disabled, 'Scheduled is available after native recovery.');
   check(error.textContent === 'This item was already filed as I1. Any change after that Send was not sent.',
         'The retry shows the first saved item ID.');
   check(!sessionStorage.getItem(draftKey) && sessionStorage.getItem(draftPrefix + posts[0].request_id) === null,
         'An already filed response clears the draft slot and the migrated pointer.');
  } else if (reloadAfterSend && !failures.length) {
   check(words.readOnly && send.textContent === 'Retry', 'The lost reply locks Retry before reload.');
   sessionStorage.setItem(probeKey, JSON.stringify(posts[0]));
   if (shippedPointer) {
    check(posts[0].images.length === 1, 'The first Send includes the pasted image.');
    sessionStorage.removeItem('console.new-item.submitted');
    const {request_id, recipient_id} = posts[0];
    storeShippedPointer(request_id, shippedPointer === 'id' ? request_id :
                        JSON.stringify({id: request_id, recipient: recipient_id}));
   }
   location.reload();
   return false;
  } else if (!reloadAfterSend && !failures.length) {
   check(words.readOnly && send.textContent === 'Retry', 'The lost reply locks Retry before renewal.');
   send.click();
   const renew = dialog.querySelector('[data-renew-capture]');
   await waitFor(() => !renew.closest('[data-replace-capture]').hidden && !renew.disabled);
   check(words.readOnly && send.disabled, 'Retry pauses until the user chooses how to handle the expired screenshot.');
   renew.click();
   await waitFor(() => !send.disabled);
   const created = new Promise(resolve => document.addEventListener('review-item-created', resolve, {once:true}));
   send.click();
   const event = await Promise.race([
    created, waitFor(sendFinished).then(() => { throw new Error(error.textContent); })
   ]);
   check(event.detail.id === 'I1' && !dialog.open, 'The replacement screenshot saves one item.');
   check(posts.length === 3 && posts.every(item => item.request_id === posts[0].request_id),
         'Each Send uses the same draft ID.');
   check(posts[0].capture_key === firstKey && posts[1].capture_key === firstKey &&
         posts[2].capture_key.startsWith(posts[0].request_id) && posts[2].capture_key !== firstKey &&
         posts[2].defer_capture, 'Retry requests a new capture after the first one expires.');
   check(!sessionStorage.getItem(draftKey), 'A successful Send clears the draft ID.');
   check(sessionStorage.getItem(draftPrefix + posts[0].request_id) === null,
         'A successful Send clears the draft slot.');
  }
  return true;
}
(async () => {
 try {
  launch.click();
  if (!await (shippedUnsent ? checkSeparateScheduledDraft() : checkListRetry())) return;
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
async function send(words = '', select = '') {
 document.querySelector('[data-owner-new]').click();
 const dialog = document.querySelector('dialog[open]');
 await waitFor(() => dialog.querySelector('input[name=recipient_id]:checked'));
 const shown = dialog.querySelector('input[name=recipient_id]:checked').value;
 if (select) dialog.querySelector('input[name=recipient_id][value=' + select + ']').click();
 if (words) {
  if (dialog.querySelector('[name=include_page]').checked) dialog.querySelector('[name=include_page]').click();
  const field = dialog.querySelector('textarea');
  field.value = words;
  field.dispatchEvent(new InputEvent('input',{bubbles:true}));
 }
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
    const {item, shown, toast} = await send();
    const original = pending.get(item.request_id);
    check(original?.recipient_id === shown, 'Reload restores the draft ID and its recipient.');
    check(JSON.stringify(original) === JSON.stringify(item), 'Retry sends the original payload.');
    check(toast.querySelector('span').textContent === 'Sending.', 'A retry that can recover a filing shows Sending.');
    toast.getAnimations()[0].finish();
    await Promise.race([created,
     new Promise((resolve, reject) => setTimeout(() => reject(new Error('Retry did not report a saved item.')), 8000))
    ]);
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
    check(earlier.cleared ? item.request_id !== earlier.requestId : item.request_id === earlier.requestId,
          'Retry keeps the ID after reload. A kind change creates a new ID.');
    if (!earlier.cleared) return Promise.resolve(Response.json({error:'The server refused this Send.'}, {status:409}));
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
   check(words.readOnly && send.textContent === 'Retry', 'Reload requires Retry for the submitted draft.');
   send.click();
   await waitFor(() => !words.readOnly && send.form.getAttribute('aria-busy') === 'false');
   selectType('proposal');
   checkEmpty();
   selectType('investigation');
   checkEmpty();
   const draftKeys = Object.keys(sessionStorage).filter(key => /^console\.list\.pending-send\.r6-/.test(key));
   check(draftKeys.length === 1, 'Only the current draft can restore its recipient.');
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

    def fixture_width(url: str) -> int | None:
        address = urlsplit(url)
        if address.path.startswith("/case/"):
            return int(address.path.removeprefix("/case/"))
        widths = parse_qs(address.query).get("width")
        return int(widths[0]) if widths else None

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
            width = fixture_width(self.headers["Referer"])
            assert width is not None
            return fixtures[width]

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
            elif address.path.startswith("/case/") or address.path == "/console/list.html":
                width = fixture_width(self.path)
                if width is None:
                    width = fixture_width(self.headers.get("Referer", ""))
                if width is not None and width in documents:
                    self.response(200, "text/html; charset=utf-8", documents[width])
                else:
                    self.response(404, "text/plain", b"Unknown fixture")
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
                intent: new_item_dialog.SubmissionCaptureIntent = new_item_dialog.NoDeferredCapture.ABSENT
                if "defer_capture" in fields:
                    key = new_item_dialog.read_capture_key(fields["capture_key"])
                    capture = serve.deferred_capture_request(fixture.root, request.page, fields["defer_capture"])
                    intent = new_item_dialog.DeferredCaptureIntent(key, request.request_id, capture)
                with (patch.dict(os.environ, fixture.environment()),
                      patch.object(Path, "home", return_value=fixture.root),
                      patch.object(reply_tokens, "platform", "linux")):
                    item = review_submissions.submit(
                        request, screenshot=image, capture_intent=intent,
                        capture_requested=request.page == "list" and "capture_key" in fields and "defer_capture" not in fields)
                    if (isinstance(intent, new_item_dialog.DeferredCaptureIntent)
                            and isinstance(intent.request, new_item_dialog.PageCaptureRequest)
                            and isinstance(fixture.screenshot, new_item_dialog.PageScreenshot)):
                        _ = review_submissions.attach_submission_screenshot(
                            new_item_dialog.read_submission_key(item.key), fixture.screenshot)
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

    with accepting_console_text(), ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
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
        for shipped_pointer, shipped_unsent in (("", False), ("id", False), ("id and recipient", False),
                                                ("id and recipient", True)):
            with self.subTest(shipped_pointer=shipped_pointer, shipped_unsent=shipped_unsent):
                self.check_list_retry(reload_after_send=True, shipped_pointer=shipped_pointer,
                                      shipped_unsent=shipped_unsent)

    def check_list_retry(self, *, reload_after_send: bool, shipped_pointer: str = "",
                         shipped_unsent: bool = False) -> None:
        temporary = Path(self.enterContext(tempfile.TemporaryDirectory()))
        owner = staff.StaffRow(OWNER, "producer", "builds", staff.StaffRoot(), current_session=OWNER)
        configuration = temporary / "config"
        configuration.mkdir()
        _ = (configuration / "console.conf").write_text(f"page.list.owner={OWNER}\n")
        image = (new_item_dialog.NO_PAGE_SCREENSHOT if reload_after_send else new_item_dialog.PageScreenshot(
            base64.b64decode(PNG), new_item_dialog.PageViewport(1440, 900, True)))
        fixture = DialogFixture(temporary, screenshot=image, reject_first_send=reload_after_send and not shipped_unsent)
        probe = (LIST_RETRY_PROBE.replace("__RELOAD_AFTER_SEND__", json.dumps(reload_after_send))
                 .replace("__SHIPPED_POINTER__", json.dumps(shipped_pointer))
                 .replace("__SHIPPED_UNSENT__", json.dumps(shipped_unsent)))
        page = fixture_page(1440, photo=False, script=probe).replace(
            b'data-page-owner="builds"', b'data-page-owner="list"')
        with patch.object(staff, "snapshot", return_value=staff.StaffSnapshot((owner,))):
            capture_dialogs(temporary / "list-retry.png", {1440: fixture}, pages={1440: page})
        self.assertEqual(fixture.results, [])
        if shipped_unsent:
            self.assertEqual(len(fixture.posts), 1)
        else:
            self.assertEqual(len(fixture.posts), 2)
            self.assertEqual(fixture.posts[0].request_id, fixture.posts[1].request_id)
        with patch.dict(os.environ, fixture.environment()):
            record, = investigation.records(owner.id)
        self.assertEqual((record.id, record.question), ("I1", "Keep the first List Send."))
        submission = record.submission
        assert isinstance(submission, new_item_dialog.ReviewSubmission)
        if shipped_pointer:
            reference, = submission.images
            with patch.dict(os.environ, fixture.environment()):
                attachment = review_submissions.image_folder(submission) / reference.id / reference.file
            self.assertEqual(attachment.read_bytes(), base64.b64decode(PNG))
        else:
            self.assertEqual(submission.images, () if reload_after_send else (review_submissions.PAGE_SCREENSHOT,))
        if isinstance(image, new_item_dialog.PageScreenshot):
            intent = submission.capture_intent
            assert isinstance(intent, new_item_dialog.DeferredCaptureIntent)
            self.assertEqual(intent.capture_key, fixture.wire_posts[-1]["capture_key"])
            reference, = submission.images
            with patch.dict(os.environ, fixture.environment()):
                attachment = review_submissions.image_folder(submission) / reference.id / reference.file
            self.assertEqual(attachment.read_bytes(), image.png)

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required for dialog behavior")
    def test_bug_dialog_reports_the_open_page_even_without_a_screenshot(self) -> None:
        temporary = Path(self.enterContext(tempfile.TemporaryDirectory()))
        fixture = DialogFixture(temporary)
        capture_dialogs(temporary / "bug.png", {1440: fixture}, pages={1440: bug_fixture_page()})
        self.assertEqual(fixture.results, [])

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required for dialog behavior")
    def test_owner_button_opens_seven_types_and_selects_the_owner_from_all_recipients(self) -> None:
        temporary = Path(self.enterContext(tempfile.TemporaryDirectory()))
        owner_probe = OWNER_OPTIONS_PROBE.replace("__SEND_SCHEDULED__", "true")
        probes = {1440: owner_probe, 390: owner_probe}
        for width, kind, page, scenario in ((1280, "scheduled", "builds", "reload"),
                                           (412, "scheduled", "list", "reload"),
                                           (1152, "bug", "builds", "reload"),
                                           (1120, "bug", "builds", "reopen"),
                                           (1100, "bug", "builds", "open"),
                                           (1090, "bug", "builds", "unconfirmed"),
                                           (1080, "bug", "builds", "renew"),
                                           (1040, "bug", "builds", "omit"),
                                           (1000, "bug", "builds", "missing-attachment")):
            probes[width] = (RESTORED_SEND_PROBE.replace("__FIRST_PAGE__", page).replace("__KIND__", kind)
                             .replace("__SCENARIO__", scenario))
        fixtures = {width: DialogFixture(temporary / str(width), owner_reason=OWNER_REASON) for width in probes}
        capture_dialogs(temporary / "options.png", fixtures,
                        pages={width: fixture_page(width, photo=False, script=probe) for width, probe in probes.items()})
        self.assertEqual({width: fixture.results for width, fixture in fixtures.items()}, {1440: [], 390: [], 1280: [], 412: [], 1152: [], 1120: [], 1100: [], 1090: [], 1080: [], 1040: [], 1000: []})

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
            probe = OWNER_OPTIONS_PROBE.replace("__SEND_SCHEDULED__", "false")
            capture_dialogs(temporary / "options.png", fixtures,
                            pages={width: fixture_page(width, photo=False, script=probe) for width in fixtures})
        for width, fixture in fixtures.items():
            with self.subTest(width=width), patch.dict(os.environ, fixture.environment()):
                self.assertEqual(fixture.results, [])
                post, = fixture.wire_posts
                self.assertEqual((post["kind"], post["recipient_id"], post["words"]),
                                 ("enhancement", OTHER_RECIPIENT, "Show the next check in the row."))
                record, = investigation.records(OTHER_RECIPIENT)
                self.assertEqual(record.title, "Enhancement: Show the next check in the row.")

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required for dialog behavior")
    def test_builds_row_sends_its_evidence_to_builds_without_the_page(self) -> None:
        temporary = Path(self.enterContext(tempfile.TemporaryDirectory()))
        recipient = staff.StaffRow(OWNER, "producer", "builds", staff.StaffRoot(), current_session=OWNER)
        _ = self.enterContext(patch.object(staff, "snapshot", return_value=staff.StaffSnapshot((recipient,))))
        fixture = DialogFixture(temporary / "row")
        (fixture.root / "config").mkdir(parents=True)
        capture_dialogs(temporary / "row.png", {1440: fixture}, pages={1440: row_evidence_page()})
        self.assertEqual(fixture.results, [])
        rejected, request, other = fixture.posts
        self.assertEqual(rejected, request)
        self.assertEqual((request.kind, request.recipient_id, request.page), (
            review_submissions.SubmissionKind.INVESTIGATION, OWNER, ""))
        self.assertEqual((other.kind, other.recipient_id, other.page), (review_submissions.SubmissionKind.QUESTION, OWNER, ""))
        self.assertNotEqual(other.request_id, request.request_id)
        assert isinstance(other.evidence, new_item_dialog.BuildStepEvidence)
        self.assertEqual(other.evidence.step_id, "other-step")
        for wire in fixture.wire_posts:
            self.assertNotIn("page", wire)
            self.assertNotIn("capture_key", wire)
        assert isinstance(request.evidence, new_item_dialog.BuildStepEvidence)
        self.assertEqual((request.evidence.step_id, request.evidence.memory.free_text), ("build-step-50", "17.9 GiB"))
        with patch.dict(os.environ, fixture.environment()):
            saved, = investigation.records(OWNER)
        assert isinstance(saved.submission, new_item_dialog.ReviewSubmission)
        card = review_page.submission_assets("investigation/fixture/I1", saved.submission)
        self.assertIn("17.9 GiB free for the next step · Queue cap 27.9 GiB", card)
        self.assertIn("Builds 9.9 GiB · Free RAM 30.0 GiB", card)
        legacy = review_page.submission_assets("investigation/fixture/I1", replace(saved.submission, evidence=EVIDENCE))
        self.assertIn("18 GiB free for the next step · Queue cap 28 GiB", legacy)
        self.assertIn("Builds 10 GiB · Free RAM 30 GiB", legacy)

    def test_render_mounts_one_dialog_with_seven_types(self) -> None:
        markup = new_item_dialog.render()
        self.assertEqual(markup.count('<dialog id="review-new-item"'), 1)
        self.assertIn('name="words" rows="4"', markup)
        self.assertEqual(markup.count('type="radio" name="kind"'), 7)
        self.assertEqual(markup.count('<span data-button-face>'), 7)
        self.assertIn('data-kind="bug"', markup)
        self.assertIn('data-kind="scheduled"', markup)
        self.assertIn('data-kind="enhancement"', markup)
        self.assertIn('class="lucide lucide-sparkles"', markup)
        self.assertIn('<svg aria-hidden="true"', markup)
        self.assertIn('name="include_page" checked', markup)
        self.assertIn('data-sent-details aria-labelledby="sent-details-title"', markup)
        self.assertIn('data-capture-slot hidden', markup)

    def test_build_step_retry_uses_a_receipt_from_before_display_text(self) -> None:
        fixture = DialogFixture(Path(self.enterContext(tempfile.TemporaryDirectory())))
        (fixture.root / "config").mkdir()
        recipient = staff.StaffRow(OWNER, "producer", "builds", staff.StaffRoot(), current_session=OWNER)
        members = staff.StaffSnapshot((recipient,))
        memory = replace(EVIDENCE.memory, queue_cap_gib=None)
        evidence = {**asdict(EVIDENCE), "memory": {
            "free_for_next_gib": memory.free_for_next_gib, "queue_cap_gib": memory.queue_cap_gib,
            "segments": [{"label": segment.label, "gib": segment.gib, "tone": segment.tone}
                         for segment in memory.segments],
        }}
        request = new_item_dialog.read_request({
            "request_id": f"r1-{'a' * 32}-bug-{OWNER}", "kind": "bug", "recipient_id": OWNER,
            "words": DISPLAY_WORDS, "evidence": evidence, "images": [],
        })
        legacy_hash = hashlib.sha256(json.dumps(["bug", DISPLAY_WORDS, evidence, []], sort_keys=True).encode()).hexdigest()
        with (patch.dict(os.environ, fixture.environment()), patch.object(Path, "home", return_value=fixture.root),
              patch.object(staff, "snapshot", return_value=members)):
            with patch.object(review_submissions, "_submission_fingerprint", return_value=legacy_hash):
                saved = review_submissions.submit(request, members=members)
            receipt_path = review_submissions.image_folder(saved).parent / "receipt.json"
            receipt = receipt_path.read_bytes()
            fields = cast(dict[str, object], json.loads(receipt))
            self.assertEqual(fields.get("payload_hash"), legacy_hash)
            self.assertEqual(review_submissions.submit(request, members=members), saved)
            self.assertEqual(receipt_path.read_bytes(), receipt)
            self.assertEqual([item.id for item in investigation.records(OWNER)], [saved.id])

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
