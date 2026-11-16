"""Render one navigation shell for the administrative pages."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import html
import importlib
import json
from pathlib import Path
import re
import sys
from typing import cast
from urllib.parse import unquote, urlencode, urlsplit, urlunsplit

from scripts.lib import atomic_file, conf, file_lock
from scripts.production.change_review import ChangeReviewError
from scripts.production.production_doc import Refusal
from scripts.review_links import change_page, review_link

PAGE_NAME = "console.html"
# The console posts the open page here and reads which other pages hold new items.
NEWS_NAME = "news"
# The console script's placeKey uses the same prefix for the place it keeps for each page.
PLACE_PREFIX = "at."
ICONS = Path(__file__).with_name("lucide")
ICON_NAMES = frozenset(("list-checks", "hammer", "cloud", "hard-drive", "image", "message-circle-question", "panel-left-close",
                        "panel-left-open", "menu", "refresh-cw", "sparkle", "users"))
REFERENCE = re.compile(r"python:([a-zA-Z_][\w.]*)\:([a-zA-Z_]\w*)")
# The console folder keeps the items each page held when the user last opened it, for every browser.
SEEN_NAME = ".review-link-seen.json"
SEEN_LOCK = ".review-link-seen.lock"
ITEM_ERRORS = (*review_link.SNAPSHOT_ERRORS, ChangeReviewError, Refusal)


class NewsRequest:
    """One news request: the review root, and work that the items functions share within the request."""

    def __init__(self, root: Path) -> None:
        self.root: Path = root
        self.results: dict[Callable[[Path], object], object] = {}

    def shared[T](self, function: Callable[[Path], T]) -> T:
        """Run a function of the root once per request; later calls take its first result."""
        if function not in self.results:
            self.results[function] = function(self.root)
        return cast(T, self.results[function])


# A page's items function names the items that wait on the page, such as its waiting changes.
ItemsFunction = Callable[[NewsRequest], frozenset[str]]


@dataclass(frozen=True)
class AvailablePage:
    """A page has a same-origin path under the review root."""

    path: str


@dataclass(frozen=True)
class UnavailablePage:
    """The page's URL function could not supply a path."""


@dataclass(frozen=True)
class ConsolePage:
    identity: str
    label: str
    icon: str
    title: str
    destination: AvailablePage | UnavailablePage
    items: ItemsFunction | None = None


def read_pages() -> tuple[ConsolePage, ...]:
    """Resolve configured pages in file order without hiding unavailable entries."""
    values = conf.read("console").values()
    identities = dict.fromkeys(key.split(".")[1] for key in values
                               if re.fullmatch(r"page\.[a-z][a-z0-9_-]*\.(label|icon|title|url|items)", key))
    pages: list[ConsolePage] = []
    for identity in identities:
        prefix = f"page.{identity}."
        label, icon, title, reference, items = (values.get(prefix + key, "") for key in ("label", "icon", "title", "url", "items"))
        if not label or not title or icon not in ICON_NAMES:
            raise ValueError(f"{prefix}: provide a label, title, and supported Lucide icon")
        pages.append(ConsolePage(identity, label, icon, title, resolve(prefix + "url", reference),
                                 resolve_items(prefix + "items", items) if items else None))
    if not pages:
        raise ValueError("console.conf has no page entries")
    return tuple(pages)


def place_query(identity: str, place: str) -> str:
    """Return the console query that opens a page at a place in it, such as one image."""
    return "?" + urlencode({"page": identity, PLACE_PREFIX + identity: place})


def read_rebuilds() -> AvailablePage | UnavailablePage:
    """Resolve the page that lists open rebuild needs for the rebuild link."""
    return resolve("rebuilds.url", conf.read("console").values().get("rebuilds.url", ""))


def resolve(key: str, reference: str) -> AvailablePage | UnavailablePage:
    """Call a configured URL function and keep its path under the review root."""
    match = REFERENCE.fullmatch(reference)
    if match is None:
        raise ValueError(f"{key}: expected python:<module>:<function>")
    module = importlib.import_module(match[1])
    try:
        resolver = cast(object, getattr(module, match[2]))
    except AttributeError:
        return UnavailablePage()
    if not callable(resolver):
        raise ValueError(f"{key}: the configured function is not callable")
    try:
        path = cast(Callable[[], str], resolver)()
        parsed = urlsplit(path)
        if parsed.scheme or parsed.netloc:
            origin = f"{review_link.link_host()}:{conf.read('review_links').whole('server_port')}"
            if parsed.scheme != "http" or parsed.netloc != origin:
                raise ValueError("the page URL must use the configured review origin")
            path = urlunsplit(("", "", parsed.path, parsed.query, parsed.fragment))
        if (not path.startswith("/") or path.startswith("//")
                or "\\" in unquote(path) or ".." in unquote(parsed.path).split("/")):
            raise ValueError("the page path must stay under the review root")
        return AvailablePage(path)
    except review_link.SNAPSHOT_ERRORS:
        return UnavailablePage()


def resolve_items(key: str, reference: str) -> ItemsFunction:
    """Load the function that names a page's waiting items."""
    match = REFERENCE.fullmatch(reference)
    if match is None:
        raise ValueError(f"{key}: expected python:<module>:<function>")
    function = cast(object, getattr(importlib.import_module(match[1]), match[2]))
    if not callable(function):
        raise ValueError(f"{key}: the configured function is not callable")
    return cast(ItemsFunction, function)


def news(root: Path, pages: tuple[ConsolePage, ...], opened: str) -> tuple[str, ...]:
    """Record the open page's items as seen, and name each page with an item added after its last open."""
    folder = change_page.console_folder(root)
    with file_lock.exclusive(folder / SEEN_LOCK):
        try:
            stored = cast(object, json.loads((folder / SEEN_NAME).read_text(encoding="utf-8")))
        except FileNotFoundError:
            stored = {}
        if not isinstance(stored, dict):
            raise ValueError(f"{SEEN_NAME}: expected an object")
        seen = {page: frozenset(str(key) for key in cast(list[object], keys))
                for page, keys in cast(dict[str, object], stored).items() if isinstance(keys, list)}
        fresh: list[str] = []
        updated = dict(seen)
        request = NewsRequest(root)
        for page in pages:
            if page.items is None or not isinstance(page.destination, AvailablePage):
                continue
            try:
                items = page.items(request)
            except ITEM_ERRORS as error:
                print(f"review-links news: {page.identity}: {error}", file=sys.stderr)
                continue
            # A page with no record starts from its present items, so a first visit marks nothing as new.
            if page.identity == opened or page.identity not in seen:
                updated[page.identity] = items
            elif items - seen[page.identity]:
                fresh.append(page.identity)
        if updated != seen:
            atomic_file.write_text_atomic(folder / SEEN_NAME, json.dumps(
                {page: sorted(keys) for page, keys in updated.items()}, indent=1) + "\n")
    return tuple(fresh)


def icon_svg(name: str) -> str:
    """Read only the vendored icons used by the console."""
    if name not in ICON_NAMES:
        raise ValueError(f"unsupported console icon: {name}")
    source = (ICONS / f"{name}.svg").read_text(encoding="utf-8")
    return source[source.index("<svg"):].replace("<svg", '<svg aria-hidden="true" focusable="false"', 1)


def render(pages: tuple[ConsolePage, ...], rebuilds: AvailablePage | UnavailablePage) -> bytes:
    """Render navigation, the rebuild link and a frame without external fonts, scripts, or styles.

    The rebuild link opens the Rebuilds page in the frame, as a navigation entry opens its page.
    """
    entries: list[str] = []
    new_mark = icon_svg("sparkle")
    for page in pages:
        available = isinstance(page.destination, AvailablePage)
        path = page.destination.path if isinstance(page.destination, AvailablePage) else ""
        label = html.escape(page.label)
        entries.append(f'<a class="nav-entry" href="?page={page.identity}" data-page="{page.identity}" '
                       + f'data-url="{html.escape(path, quote=True)}" data-title="{html.escape(page.title, quote=True)}" '
                       + f'aria-label="{label}' + ('' if available else ' — Not available') + '">'
                       + icon_svg(page.icon) + f'<span class="entry-label">{label}'
                       + ('' if available else '<small>Not available</small>') + '</span>'
                       + ('<span class="new-mark" hidden>' + new_mark + '</span>' if available
                          else '<span class="unavailable-dot" aria-hidden="true"></span>')
                       + f'<span class="tooltip" aria-hidden="true">{label}'
                       + ('<span class="new-text" hidden> · New</span>' if available else ' · Not available') + '</span></a>')
    content = DOCUMENT.replace("<!-- ENTRIES -->", "\n".join(entries))
    rebuilds_path = rebuilds.path if isinstance(rebuilds, AvailablePage) else ""
    settings = conf.read("review_links")
    content = content.replace("<!-- REBUILDS -->", f'data-url="{html.escape(rebuilds_path, quote=True)}"')
    content = content.replace("<!-- SHELL -->", f'data-refresh-seconds="{settings.number("page_refresh_seconds")}" '
                              + f'data-frame-retry-seconds="{settings.number("frame_retry_seconds")}" '
                              + f'data-news="/{change_page.CONSOLE_FOLDER}/{NEWS_NAME}"')
    content = content.replace("<!-- REBUILD -->", icon_svg("refresh-cw"))
    content = content.replace("<!-- CLOSE -->", icon_svg("panel-left-close"))
    content = content.replace("<!-- OPEN -->", icon_svg("panel-left-open"))
    content = content.replace("<!-- MENU -->", icon_svg("menu") + '<span class="new-mark" id="menu-new" hidden>' + new_mark + '</span>')
    return content.encode("utf-8")


DOCUMENT = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>Hana console</title>
<style>
:root{color-scheme:light dark;font:14px/1.5 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
--nav:#f8f9fc;--page:#fff;--text:#192339;--muted:#525c6e;--border:#e1e5ed;--active:#e8edfd;--accent:#2849a3;--hover:#edf0f6;
--notice:#fdf1dc;--notice-text:#7a4300;--notice-hover:#fae6c3}
@media(prefers-color-scheme:dark){:root{--nav:#141b28;--page:#10151e;--text:#e4eaf4;--muted:#a4afc2;
--border:#2d3748;--active:#253758;--accent:#c2d4ff;--hover:#202b3c;--notice:#3a2b12;--notice-text:#ffd28f;--notice-hover:#4a3718}}
*{box-sizing:border-box}body{margin:0;background:var(--page);color:var(--text);overflow:hidden}
.shell{display:grid;grid-template-columns:244px minmax(0,1fr);height:100vh;height:100dvh}
.shell.collapsed{grid-template-columns:72px minmax(0,1fr)}
.sidebar{background:var(--nav);border-right:1px solid var(--border);display:flex;flex-direction:column;min-width:0;z-index:3}
.brand{height:80px;display:flex;align-items:center;gap:12px;padding:0 20px;border-bottom:1px solid var(--border)}
h1{font-size:16px;letter-spacing:-.3px;margin:0;font-weight:650;white-space:nowrap;flex:1}
button,a{-webkit-tap-highlight-color:transparent}button{font:inherit}
.toggle{border:0;border-radius:8px;background:transparent;color:var(--muted);width:36px;height:36px;
display:grid;place-items:center;cursor:pointer;flex:none;padding:8px}.toggle:hover{background:var(--hover);color:var(--text)}
svg{width:20px;height:20px;flex:none}.open-icon{display:none}
nav{padding:22px 12px;display:flex;flex-direction:column;gap:6px}
.nav-entry{position:relative;display:flex;gap:12px;align-items:center;min-height:46px;padding:11px 14px;border-radius:9px;
color:var(--muted);text-decoration:none;font-weight:550;white-space:nowrap}
.nav-entry:hover{background:var(--hover);color:var(--text)}
.entry-label{display:grid}.entry-label small{font-size:10px;font-weight:400;color:var(--muted)}
.nav-entry[aria-current=page]{background:var(--active);color:var(--accent)}
.nav-entry[aria-current=page]:before{content:"";position:absolute;left:-12px;top:13px;bottom:13px;width:3px;background:var(--accent);border-radius:0 3px 3px 0}
:focus-visible{outline:2px solid var(--accent);outline-offset:3px}
.unavailable-dot{width:6px;height:6px;border-radius:50%;background:var(--muted);margin:0 4px 0 auto}
.new-mark{display:grid;place-items:center;margin-left:auto;color:var(--accent)}.new-mark svg{width:14px;height:14px;fill:currentColor}
.collapsed .new-mark{position:absolute;top:7px;left:33px;margin:0}.collapsed .new-mark svg{width:11px;height:11px}
.toggle{position:relative}#menu-new{position:absolute;top:4px;right:4px;left:auto;margin:0}#menu-new svg{width:11px;height:11px}
.tooltip{display:none;position:absolute;left:calc(100% + 10px);top:50%;transform:translateY(-50%);padding:7px 11px;
background:var(--text);color:var(--page);border-radius:6px;font-size:12px;font-weight:500;box-shadow:0 3px 12px #0002;z-index:5;pointer-events:none}
.collapsed .brand{padding:0 18px}.collapsed .entry-label,.collapsed .unavailable-dot{display:none}
.collapsed .brand h1{position:absolute;width:1px;height:1px;clip-path:inset(50%);overflow:hidden;white-space:nowrap}
.collapsed nav{padding:22px 12px}.collapsed .nav-entry{padding:13px 14px}.collapsed .open-icon{display:block}.collapsed .close-icon{display:none}
.collapsed .nav-entry:hover .tooltip,.collapsed .nav-entry:focus .tooltip{display:block}
.workspace{grid-column:2;display:flex;flex-direction:column;min-width:0;min-height:0}
.frame-area{flex:1;min-height:0;position:relative}
.frame-area iframe{position:absolute;inset:0;display:block;border:0;width:100%;height:100%;background:var(--page)}
.frame-area iframe[hidden]{display:block!important;visibility:hidden;pointer-events:none}
.empty{height:100%;display:grid;place-content:center;text-align:center;padding:24px;color:var(--muted)}
.empty h3{font-size:20px;color:var(--text);margin:0 0 8px}.empty p{max-width:30ch;margin:0}
.rebuild-slot{position:fixed;left:0;bottom:0;width:244px;padding:12px;z-index:4}.collapsed .rebuild-slot{width:72px}
.rebuild-link{position:relative;display:flex;gap:12px;align-items:center;width:100%;min-height:46px;padding:11px 14px;border:0;border-radius:9px;
background:var(--notice);color:var(--notice-text);font-weight:600;text-align:left;text-decoration:none;cursor:pointer}
.rebuild-link[aria-current=page]{box-shadow:inset 0 0 0 2px var(--notice-text)}
.rebuild-link .entry-label{min-width:0;overflow-wrap:anywhere}.rebuild-link .tooltip{white-space:nowrap}
.rebuild-link:hover{background:var(--notice-hover)}.rebuild-count{display:none}
.collapsed .rebuild-link .entry-label{display:none}
.collapsed .rebuild-count{display:grid;place-items:center;position:absolute;top:5px;left:28px;min-width:17px;height:17px;padding:0 4px;
border-radius:9px;background:var(--notice-text);color:var(--notice);font-size:11px;font-weight:700}
.collapsed .rebuild-link:hover .tooltip,.collapsed .rebuild-link:focus .tooltip{display:block}
[hidden]{display:none!important}.dismiss,.mobile-header{display:none}
@media(max-width:680px){.shell,.shell.collapsed{display:flex;flex-direction:column}
.mobile-header{height:56px;flex:none;display:flex;align-items:center;gap:12px;padding:0 12px;
background:var(--nav);border-bottom:1px solid var(--border);z-index:4}
.sidebar{position:fixed;inset:56px auto 0 0;width:244px;box-shadow:10px 0 32px #0002;overflow-y:auto}
.sidebar .brand,.collapsed .sidebar{display:none}
.workspace{flex:1;width:100%}
.shell:not(.collapsed) .dismiss{display:block;position:fixed;inset:56px 0 0;border:0;background:#0003;z-index:2}
.rebuild-slot,.collapsed .rebuild-slot{position:static;order:1;width:auto;padding:0;border-top:1px solid var(--border);background:var(--nav)}
.rebuild-slot:has(>[hidden]){display:none}
.rebuild-link,.collapsed .rebuild-link{width:auto;max-width:none;margin:12px}
.collapsed .rebuild-link .entry-label{display:inline}.collapsed .rebuild-count,.collapsed .rebuild-link .tooltip{display:none!important}}
</style></head><body>
<div class="shell" id="console-shell" <!-- SHELL -->>
<header class="mobile-header"><button class="toggle" id="mobile-nav-toggle" type="button"
aria-controls="console-nav" aria-expanded="false" aria-label="Open navigation"><!-- MENU --></button><h1>Hana console</h1></header>
<aside class="sidebar"><div class="brand"><h1>Hana console</h1>
<button class="toggle" id="nav-toggle" type="button" aria-controls="console-nav" aria-expanded="true" aria-label="Collapse navigation">
<span class="close-icon"><!-- CLOSE --></span><span class="open-icon"><!-- OPEN --></span></button></div>
<nav id="console-nav" aria-label="Console pages"><!-- ENTRIES --></nav></aside>
<div class="rebuild-slot"><a class="rebuild-link" id="rebuild-link" href="?page=rebuilds" data-page="rebuilds"
<!-- REBUILDS --> data-title="Rebuilds needed" hidden><!-- REBUILD --><span class="entry-label" id="rebuild-label"></span>
<span class="rebuild-count" id="rebuild-count" aria-hidden="true"></span><span class="tooltip" id="rebuild-tooltip" aria-hidden="true"></span></a></div>
<button class="dismiss" id="nav-dismiss" type="button" aria-label="Collapse navigation" tabindex="-1"></button>
<main class="workspace">
<div class="frame-area">
<section class="empty" id="page-unavailable" hidden><h3>Not available</h3><p>This page is not available. Try another page or open a new console link.</p></section></div></main>
</div>
<script>
(() => {
  const shell = document.getElementById('console-shell');
  const toggle = document.getElementById('nav-toggle');
  const mobileToggle = document.getElementById('mobile-nav-toggle');
  const phone = matchMedia('(max-width:680px)');
  const entries = [...document.querySelectorAll('.nav-entry')];
  const rebuildLink = document.getElementById('rebuild-link');
  // The rebuild link opens its page as a navigation entry does, so it is one of the pages.
  const pages = [...entries, rebuildLink];
  const frameArea = document.querySelector('.frame-area');
  // Each opened page keeps a live frame, so a page keeps its filter, card, scroll and typed text.
  const frames = new Map();
  const placeKey = identity => 'at.' + identity;
  const storageKey = 'admin-console.navigation-collapsed';
  let desktopCollapsed = false;
  try { desktopCollapsed = localStorage.getItem(storageKey) === 'true'; } catch (_) {}
  let collapsed = phone.matches || desktopCollapsed;
  function collapse(value, save = false) {
    collapsed = value;
    shell.classList.toggle('collapsed', value);
    toggle.setAttribute('aria-expanded', String(!value));
    toggle.setAttribute('aria-label', value ? 'Expand navigation' : 'Collapse navigation');
    mobileToggle.setAttribute('aria-expanded', String(!value));
    mobileToggle.setAttribute('aria-label', value ? 'Open navigation' : 'Close navigation');
    document.querySelector('.workspace').inert = phone.matches && !value;
    if (phone.matches && value && document.querySelector('.sidebar').contains(document.activeElement)) mobileToggle.focus();
    if (save && !phone.matches) {
      desktopCollapsed = value;
      try { localStorage.setItem(storageKey, String(value)); } catch (_) {}
    }
  }
  function placeOf(entry, stored) {
    const base = entry.dataset.url;
    if (!base || !stored) return base;
    try {
      const target = new URL(stored, location.href);
      // A recorded place stays on the page's own path, so a link cannot frame another page.
      if (target.origin === location.origin && target.pathname === new URL(base, location.href).pathname) {
        return target.pathname + target.search + target.hash;
      }
    } catch (_) {}
    return base;
  }
  function record(entry) {
    let place;
    try {
      const at = frames.get(entry.dataset.page).contentWindow.location;
      place = placeOf(entry, at.pathname + at.search + at.hash);
    } catch (_) { return; }
    const url = new URL(location.href);
    if (place === entry.dataset.url) url.searchParams.delete(placeKey(entry.dataset.page));
    else url.searchParams.set(placeKey(entry.dataset.page), place);
    history.replaceState(null, '', url);
  }
  function frameFor(entry) {
    let frame = frames.get(entry.dataset.page);
    if (frame) return frame;
    frame = document.createElement('iframe');
    frame.hidden = true;
    frame.src = placeOf(entry, new URL(location.href).searchParams.get(placeKey(entry.dataset.page)));
    let place = frame.src;
    frame.addEventListener('load', () => {
      let loaded = false;
      try { loaded = frame.contentWindow.location.origin === location.origin; } catch (_) {}
      if (!loaded) {
        // A refused load, such as during a server restart, shows the browser's error page; load the page again.
        setTimeout(() => { frame.src = place; }, Number(shell.dataset.frameRetrySeconds) * 1000);
        return;
      }
      place = frame.contentWindow.location.href;
      record(entry);
      try { frame.contentWindow.addEventListener('hashchange', () => record(entry)); } catch (_) {}
    });
    frames.set(entry.dataset.page, frame);
    frameArea.prepend(frame);
    return frame;
  }
  addEventListener('message', event => {
    if (event.origin !== location.origin || event.data !== 'console-place') return;
    const entry = pages.find(item => frames.get(item.dataset.page)?.contentWindow === event.source);
    if (entry) record(entry);
  });
  // The server keeps what each page held when it was last open, so every browser shows the same marks.
  const marked = entries.filter(entry => entry.querySelector('.new-mark'));
  const labels = new Map(marked.map(entry => [entry, entry.getAttribute('aria-label')]));
  const menuNew = document.getElementById('menu-new');
  let fresh = new Set();
  function showNews() {
    marked.forEach(entry => {
      const value = fresh.has(entry.dataset.page) && !entry.hasAttribute('aria-current');
      entry.querySelector('.new-mark').hidden = !value;
      entry.querySelector('.new-text').hidden = !value;
      entry.setAttribute('aria-label', labels.get(entry) + (value ? ' — New items' : ''));
    });
    menuNew.hidden = !marked.some(entry => !entry.querySelector('.new-mark').hidden);
  }
  async function loadNews() {
    const current = pages.find(entry => entry.hasAttribute('aria-current'));
    if (!current?.dataset.url || document.visibilityState !== 'visible') return;
    try {
      // The Rebuilds page has no navigation entry, so its news request names no open page.
      const response = await fetch(shell.dataset.news, {method: 'POST', cache: 'no-store',
        body: new URLSearchParams({open: current === rebuildLink ? '' : current.dataset.page})});
      if (!response.ok) return;
      fresh = new Set((await response.json()).new);
      showNews();
    } catch (_) {}
  }
  function select(identity, navigate = false) {
    const selected = pages.find(entry => entry.dataset.page === identity) || entries[0];
    if (!selected) return;
    pages.forEach(entry => {
      if (entry === selected) entry.setAttribute('aria-current', 'page');
      else entry.removeAttribute('aria-current');
    });
    const title = selected.dataset.title;
    document.title = 'Hana console · ' + title;
    const url = new URL(location.href);
    url.searchParams.set('page', selected.dataset.page);
    history.replaceState(null, '', url);
    const path = selected.dataset.url;
    frames.forEach((frame, page) => { if (page !== selected.dataset.page) frame.hidden = true; });
    document.getElementById('page-unavailable').hidden = Boolean(path);
    if (path) {
      const opened = frames.has(selected.dataset.page);
      const frame = frameFor(selected);
      frame.title = title;
      if (frame.hidden) {
        frame.hidden = false;
        // A page that paused its refresh while hidden loads an update at once; a new page loads anyway.
        if (opened) frame.contentWindow.postMessage('console-shown', location.origin);
      }
    }
    // Opening a page clears its mark at once; the server records the open page.
    showNews();
    loadNews();
    if (navigate && phone.matches) collapse(true);
  }
  collapse(collapsed);
  pages.forEach(entry => entry.addEventListener('click', event => {
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault(); select(entry.dataset.page, true);
  }));
  toggle.addEventListener('click', () => collapse(!collapsed, true));
  mobileToggle.addEventListener('click', () => {
    collapse(!collapsed);
    if (!collapsed) (entries.find(entry => entry.hasAttribute('aria-current')) || entries[0])?.focus();
  });
  phone.addEventListener('change', () => collapse(phone.matches || desktopCollapsed));
  document.getElementById('nav-dismiss').addEventListener('click', () => collapse(true, true));
  document.addEventListener('keydown', event => { if (event.key === 'Escape') collapse(true, true); });
  select(new URL(location.href).searchParams.get('page'));
  let rebuildTimer;
  async function loadRebuilds() {
    if (!rebuildLink.dataset.url || document.visibilityState !== 'visible') return;
    try {
      const response = await fetch(rebuildLink.dataset.url, {cache: 'no-store'});
      if (!response.ok) return;
      const parsed = new DOMParser().parseFromString(await response.text(), 'text/html').querySelector('[data-rebuilds]');
      if (!parsed) return;
      const count = Number(parsed.dataset.count);
      rebuildLink.hidden = count === 0;
      rebuildLink.setAttribute('aria-label', parsed.dataset.label);
      document.getElementById('rebuild-label').textContent = parsed.dataset.label;
      document.getElementById('rebuild-tooltip').textContent = parsed.dataset.label;
      document.getElementById('rebuild-count').textContent = String(count);
      // The open Rebuilds page shows the same read, so it needs no refresh of its own.
      const shown = frames.get(rebuildLink.dataset.page)?.contentDocument?.querySelector('[data-rebuilds]');
      if (shown) shown.replaceWith(shown.ownerDocument.importNode(parsed, true));
    } catch (_) {}
  }
  function refresh() { loadRebuilds(); loadNews(); }
  function scheduleRebuilds() {
    clearInterval(rebuildTimer);
    if (document.visibilityState === 'visible') rebuildTimer = setInterval(refresh, Number(shell.dataset.refreshSeconds) * 1000);
  }
  document.addEventListener('visibilitychange', () => { scheduleRebuilds(); refresh(); });
  loadRebuilds();
  scheduleRebuilds();
})();
</script></body></html>
"""
