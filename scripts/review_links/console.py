"""Render one navigation shell for the administrative pages."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import html
import importlib
from pathlib import Path
import re
from typing import cast
from urllib.parse import unquote, urlencode, urlsplit, urlunsplit

from scripts.lib import conf
from scripts.review_links import review_link

PAGE_NAME = "console.html"
# The console script's placeKey uses the same prefix for the place it keeps for each page.
PLACE_PREFIX = "at."
ICONS = Path(__file__).with_name("lucide")
ICON_NAMES = frozenset(("list-checks", "hammer", "cloud", "image", "message-circle-question", "panel-left-close",
                        "panel-left-open", "menu", "refresh-cw", "users", "x"))


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


def read_pages() -> tuple[ConsolePage, ...]:
    """Resolve configured pages in file order without hiding unavailable entries."""
    values = conf.read("console").values()
    identities = dict.fromkeys(key.split(".")[1] for key in values
                               if re.fullmatch(r"page\.[a-z][a-z0-9_-]*\.(label|icon|title|url)", key))
    pages: list[ConsolePage] = []
    for identity in identities:
        prefix = f"page.{identity}."
        label, icon, title, reference = (values.get(prefix + key, "") for key in ("label", "icon", "title", "url"))
        if not label or not title or icon not in ICON_NAMES:
            raise ValueError(f"{prefix}: provide a label, title, and supported Lucide icon")
        pages.append(ConsolePage(identity, label, icon, title, resolve(prefix + "url", reference)))
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
    match = re.fullmatch(r"python:([a-zA-Z_][\w.]*)\:([a-zA-Z_]\w*)", reference)
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


def icon_svg(name: str) -> str:
    """Read only the vendored icons used by the console."""
    if name not in ICON_NAMES:
        raise ValueError(f"unsupported console icon: {name}")
    source = (ICONS / f"{name}.svg").read_text(encoding="utf-8")
    return source[source.index("<svg"):].replace("<svg", '<svg aria-hidden="true" focusable="false"', 1)


def render(pages: tuple[ConsolePage, ...], rebuilds: AvailablePage | UnavailablePage) -> bytes:
    """Render navigation, the rebuild link and a frame without external fonts, scripts, or styles."""
    entries: list[str] = []
    for page in pages:
        available = isinstance(page.destination, AvailablePage)
        path = page.destination.path if isinstance(page.destination, AvailablePage) else ""
        label = html.escape(page.label)
        entries.append(f'<a class="nav-entry" href="?page={page.identity}" data-page="{page.identity}" '
                       + f'data-url="{html.escape(path, quote=True)}" data-title="{html.escape(page.title, quote=True)}" '
                       + f'aria-label="{label}' + ('' if available else ' — Not available') + '">'
                       + icon_svg(page.icon) + f'<span class="entry-label">{label}'
                       + ('' if available else '<small>Not available</small>') + '</span>'
                       + ('' if available else '<span class="unavailable-dot" aria-hidden="true"></span>')
                       + f'<span class="tooltip" aria-hidden="true">{label}'
                       + ('' if available else ' · Not available') + '</span></a>')
    content = DOCUMENT.replace("<!-- ENTRIES -->", "\n".join(entries))
    rebuilds_path = rebuilds.path if isinstance(rebuilds, AvailablePage) else ""
    settings = conf.read("review_links")
    content = content.replace("<!-- SHELL -->", f'data-rebuilds="{html.escape(rebuilds_path, quote=True)}" '
                              + f'data-refresh-seconds="{settings.number("page_refresh_seconds")}" '
                              + f'data-frame-retry-seconds="{settings.number("frame_retry_seconds")}"')
    content = content.replace("<!-- REBUILD -->", icon_svg("refresh-cw"))
    content = content.replace("<!-- X -->", icon_svg("x"))
    content = content.replace("<!-- CLOSE -->", icon_svg("panel-left-close"))
    content = content.replace("<!-- OPEN -->", icon_svg("panel-left-open"))
    content = content.replace("<!-- MENU -->", icon_svg("menu"))
    return content.encode("utf-8")


DOCUMENT = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>Hana console</title>
<style>
:root{color-scheme:light dark;font:14px/1.5 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
--nav:#f8f9fc;--page:#fff;--text:#192339;--muted:#626c7e;--border:#e1e5ed;--active:#e8edfd;--accent:#2849a3;--hover:#edf0f6;
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
.unavailable-dot{width:6px;height:6px;border-radius:50%;background:var(--muted);margin-left:auto}
.tooltip{display:none;position:absolute;left:calc(100% + 10px);top:50%;transform:translateY(-50%);padding:7px 11px;
background:var(--text);color:var(--page);border-radius:6px;font-size:12px;font-weight:500;box-shadow:0 3px 12px #0002;z-index:5;pointer-events:none}
.collapsed .brand{padding:0 18px}.collapsed .entry-label,.collapsed .unavailable-dot{display:none}
.collapsed .brand h1{position:absolute;width:1px;height:1px;clip-path:inset(50%);overflow:hidden;white-space:nowrap}
.collapsed nav{padding:22px 12px}.collapsed .nav-entry{padding:13px 14px}.collapsed .open-icon{display:block}.collapsed .close-icon{display:none}
.collapsed .nav-entry:hover .tooltip,.collapsed .nav-entry:focus .tooltip{display:block}
.workspace{grid-column:2;display:flex;flex-direction:column;min-width:0;min-height:0}
.page-header{height:80px;flex:none;display:flex;align-items:center;padding:0 30px;border-bottom:1px solid var(--border);gap:12px}
.page-header h2{font-size:17px;line-height:1.3;letter-spacing:-.3px;margin:0;font-weight:600}
.frame-area{flex:1;min-height:0;position:relative}
.frame-area iframe{position:absolute;inset:0;display:block;border:0;width:100%;height:100%;background:var(--page)}
.frame-area iframe[hidden]{display:block!important;visibility:hidden;pointer-events:none}
.empty{height:100%;display:grid;place-content:center;text-align:center;padding:24px;color:var(--muted)}
.empty h3{font-size:20px;color:var(--text);margin:0 0 8px}.empty p{max-width:30ch;margin:0}
.rebuild-slot{position:fixed;left:0;bottom:0;width:244px;padding:12px;z-index:4}.collapsed .rebuild-slot{width:72px}
.rebuild-link{position:relative;display:flex;gap:12px;align-items:center;width:100%;min-height:46px;padding:11px 14px;border:0;border-radius:9px;
background:var(--notice);color:var(--notice-text);font-weight:600;text-align:left;cursor:pointer}
.rebuild-link .entry-label{min-width:0;overflow-wrap:anywhere}.rebuild-link .tooltip{white-space:nowrap}
.rebuild-link:hover{background:var(--notice-hover)}.rebuild-count{display:none}
.collapsed .rebuild-link .entry-label{display:none}
.collapsed .rebuild-count{display:grid;place-items:center;position:absolute;top:5px;left:28px;min-width:17px;height:17px;padding:0 4px;
border-radius:9px;background:var(--notice-text);color:var(--notice);font-size:11px;font-weight:700}
.collapsed .rebuild-link:hover .tooltip,.collapsed .rebuild-link:focus .tooltip{display:block}
dialog{border:1px solid var(--border);border-radius:12px;padding:0;width:min(560px,calc(100vw - 32px));max-height:calc(100dvh - 64px);
background:var(--page);color:var(--text);box-shadow:0 18px 48px #0004}
dialog::backdrop{background:#0b10184d}
.dialog-head{display:flex;align-items:center;gap:12px;padding:16px 16px 16px 24px;border-bottom:1px solid var(--border)}
.dialog-head h2{flex:1;margin:0;font-size:17px;font-weight:600;letter-spacing:-.3px}
.dialog-body{padding:8px 24px 24px;overflow-wrap:anywhere}.dialog-body article{padding-top:16px}
.dialog-body article+article{border-top:1px solid var(--border);margin-top:16px}
.dialog-body h2{font-size:15px;font-weight:650;margin:0 0 4px}.dialog-body p{margin:0 0 8px;color:var(--muted)}
.dialog-body code{font:13px/1.4 ui-monospace,SFMono-Regular,Menlo,monospace}
.dialog-body ul{list-style:none;margin:12px 0 0;padding:0;display:flex;flex-direction:column;gap:12px}
.dialog-body li{border:1px solid var(--border);border-radius:9px;padding:12px 14px}
.dialog-body dl{display:grid;grid-template-columns:5em minmax(0,1fr);column-gap:12px;row-gap:2px;margin:0;font-size:13px}
.dialog-body dt{color:var(--muted)}.dialog-body dd{margin:0;min-width:0}
.dialog-body>p:last-child{margin:16px 0 0;font-size:13px}
[hidden]{display:none!important}.dismiss,.mobile-header{display:none}
@media(max-width:680px){.shell,.shell.collapsed{display:flex;flex-direction:column}
.mobile-header{height:56px;flex:none;display:flex;align-items:center;gap:12px;padding:0 12px;
background:var(--nav);border-bottom:1px solid var(--border);z-index:4}
.sidebar{position:fixed;inset:56px auto 0 0;width:244px;box-shadow:10px 0 32px #0002;overflow-y:auto}
.sidebar .brand,.collapsed .sidebar,.page-header{display:none}
.workspace{flex:1;width:100%}
.shell:not(.collapsed) .dismiss{display:block;position:fixed;inset:56px 0 0;border:0;background:#0003;z-index:2}
.rebuild-slot,.collapsed .rebuild-slot{left:12px;bottom:12px;width:auto;padding:0}
.rebuild-link,.collapsed .rebuild-link{width:auto;max-width:220px;box-shadow:0 6px 20px #0003}
.collapsed .rebuild-link .entry-label{display:inline}.collapsed .rebuild-count,.collapsed .rebuild-link .tooltip{display:none!important}}
</style></head><body>
<div class="shell" id="console-shell" <!-- SHELL -->>
<header class="mobile-header"><button class="toggle" id="mobile-nav-toggle" type="button"
aria-controls="console-nav" aria-expanded="false" aria-label="Open navigation"><!-- MENU --></button><h1>Hana console</h1></header>
<aside class="sidebar"><div class="brand"><h1>Hana console</h1>
<button class="toggle" id="nav-toggle" type="button" aria-controls="console-nav" aria-expanded="true" aria-label="Collapse navigation">
<span class="close-icon"><!-- CLOSE --></span><span class="open-icon"><!-- OPEN --></span></button></div>
<nav id="console-nav" aria-label="Console pages"><!-- ENTRIES --></nav></aside>
<div class="rebuild-slot"><button class="rebuild-link" id="rebuild-link" type="button" aria-haspopup="dialog"
aria-controls="rebuild-dialog" hidden><!-- REBUILD --><span class="entry-label" id="rebuild-label"></span>
<span class="rebuild-count" id="rebuild-count" aria-hidden="true"></span><span class="tooltip" id="rebuild-tooltip" aria-hidden="true"></span></button></div>
<button class="dismiss" id="nav-dismiss" type="button" aria-label="Collapse navigation" tabindex="-1"></button>
<main class="workspace"><header class="page-header"><h2 id="page-title"></h2></header>
<div class="frame-area">
<section class="empty" id="page-unavailable" hidden><h3>Not available</h3><p>This page is not available. Try another page or open a new console link.</p></section></div></main>
</div>
<dialog id="rebuild-dialog" aria-labelledby="rebuild-title"><div class="dialog-head"><h2 id="rebuild-title">Rebuilds needed</h2>
<button class="toggle" id="rebuild-close" type="button" aria-label="Close"><!-- X --></button></div>
<div class="dialog-body" id="rebuild-body"></div></dialog>
<script>
(() => {
  const shell = document.getElementById('console-shell');
  const toggle = document.getElementById('nav-toggle');
  const mobileToggle = document.getElementById('mobile-nav-toggle');
  const phone = matchMedia('(max-width:680px)');
  const entries = [...document.querySelectorAll('.nav-entry')];
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
    const entry = entries.find(item => frames.get(item.dataset.page)?.contentWindow === event.source);
    if (entry) record(entry);
  });
  function select(identity, navigate = false) {
    const selected = entries.find(entry => entry.dataset.page === identity) || entries[0];
    if (!selected) return;
    entries.forEach(entry => {
      if (entry === selected) entry.setAttribute('aria-current', 'page');
      else entry.removeAttribute('aria-current');
    });
    const title = selected.dataset.title;
    document.title = 'Hana console · ' + title;
    document.getElementById('page-title').textContent = title;
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
    if (navigate && phone.matches) collapse(true);
  }
  collapse(collapsed);
  entries.forEach(entry => entry.addEventListener('click', event => {
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
  const dialog = document.getElementById('rebuild-dialog');
  const rebuildLink = document.getElementById('rebuild-link');
  document.addEventListener('keydown', event => { if (event.key === 'Escape' && !dialog.open) collapse(true, true); });
  select(new URL(location.href).searchParams.get('page'));
  let rebuildTimer;
  async function loadRebuilds() {
    if (!shell.dataset.rebuilds || document.visibilityState !== 'visible') return;
    try {
      const response = await fetch(shell.dataset.rebuilds, {cache: 'no-store'});
      if (!response.ok) return;
      const parsed = new DOMParser().parseFromString(await response.text(), 'text/html').querySelector('[data-rebuilds]');
      if (!parsed) return;
      const count = Number(parsed.dataset.count);
      rebuildLink.hidden = count === 0;
      rebuildLink.setAttribute('aria-label', parsed.dataset.label);
      document.getElementById('rebuild-label').textContent = parsed.dataset.label;
      document.getElementById('rebuild-tooltip').textContent = parsed.dataset.label;
      document.getElementById('rebuild-count').textContent = String(count);
      document.getElementById('rebuild-body').replaceChildren(...document.importNode(parsed, true).childNodes);
      if (count === 0 && dialog.open) dialog.close();
    } catch (_) {}
  }
  function scheduleRebuilds() {
    clearInterval(rebuildTimer);
    if (document.visibilityState === 'visible') rebuildTimer = setInterval(loadRebuilds, Number(shell.dataset.refreshSeconds) * 1000);
  }
  rebuildLink.addEventListener('click', () => dialog.showModal());
  document.getElementById('rebuild-close').addEventListener('click', () => dialog.close());
  dialog.addEventListener('click', event => { if (event.target === dialog) dialog.close(); });
  dialog.addEventListener('close', () => { if (!rebuildLink.hidden) rebuildLink.focus(); });
  document.addEventListener('visibilitychange', () => { scheduleRebuilds(); loadRebuilds(); });
  loadRebuilds();
  scheduleRebuilds();
})();
</script></body></html>
"""
