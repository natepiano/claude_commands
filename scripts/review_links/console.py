"""Publish one navigation shell for the administrative pages."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import html
import importlib
from pathlib import Path
import re
import tempfile
from typing import cast
from urllib.parse import unquote, urlsplit, urlunsplit

from scripts.lib import atomic_file, conf
from scripts.review_links import review_link

PAGE_NAME = "console.html"
ICONS = Path(__file__).with_name("lucide")
ICON_NAMES = frozenset(("list-checks", "hammer", "panel-left-close", "panel-left-open", "menu"))


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
        match = re.fullmatch(r"python:([a-zA-Z_][\w.]*)\:([a-zA-Z_]\w*)", reference)
        if match is None:
            raise ValueError(f"{prefix}url: expected python:<module>:<function>")
        module = importlib.import_module(match[1])
        try:
            resolver = cast(object, getattr(module, match[2]))
        except AttributeError:
            pages.append(ConsolePage(identity, label, icon, title, UnavailablePage()))
            continue
        if not callable(resolver):
            raise ValueError(f"{prefix}url: the configured function is not callable")
        destination: AvailablePage | UnavailablePage
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
            destination = AvailablePage(path)
        except review_link.SNAPSHOT_ERRORS:
            destination = UnavailablePage()
        pages.append(ConsolePage(identity, label, icon, title, destination))
    if not pages:
        raise ValueError("console.conf has no page entries")
    return tuple(pages)


def icon_svg(name: str) -> str:
    """Read only the vendored icons used by the console."""
    if name not in ICON_NAMES:
        raise ValueError(f"unsupported console icon: {name}")
    source = (ICONS / f"{name}.svg").read_text(encoding="utf-8")
    return source[source.index("<svg"):].replace("<svg", '<svg aria-hidden="true" focusable="false"', 1)


def render(pages: tuple[ConsolePage, ...]) -> bytes:
    """Render navigation and a frame without external fonts, scripts, or styles."""
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
    content = content.replace("<!-- CLOSE -->", icon_svg("panel-left-close"))
    content = content.replace("<!-- OPEN -->", icon_svg("panel-left-open"))
    content = content.replace("<!-- MENU -->", icon_svg("menu"))
    return content.encode("utf-8")


def publish() -> str:
    """Publish the console beside Changes, or in its own snapshot when Changes is unavailable."""
    root = review_link.review_root().resolve()
    pages = read_pages()
    content = render(pages)
    for page in pages:
        if page.identity == "changes" and isinstance(page.destination, AvailablePage):
            path = unquote(urlsplit(page.destination.path).path)
            target = (root / path.lstrip("/")).with_name(PAGE_NAME)
            if not target.resolve().is_relative_to(root) or not target.parent.is_dir():
                raise ValueError("the Changes page is outside the review root or is not available")
            atomic_file.write_bytes_atomic(target, content, mode=0o600)
            return review_link.snapshot_url(root, target) + "?page=changes"
    with tempfile.TemporaryDirectory(prefix="admin-console-") as temporary:
        source = Path(temporary) / PAGE_NAME
        _ = source.write_bytes(content)
        return review_link.snapshot_at(root, [source], new=True)[0] + "?page=" + pages[0].identity


DOCUMENT = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>Hana console</title>
<style>
:root{color-scheme:light dark;font:14px/1.5 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
--nav:#f8f9fc;--page:#fff;--text:#192339;--muted:#626c7e;--border:#e1e5ed;--active:#e8edfd;--accent:#2849a3;--hover:#edf0f6}
@media(prefers-color-scheme:dark){:root{--nav:#141b28;--page:#10151e;--text:#e4eaf4;--muted:#a4afc2;
--border:#2d3748;--active:#253758;--accent:#c2d4ff;--hover:#202b3c}}
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
.frame-area{flex:1;min-height:0;position:relative}iframe{display:block;border:0;width:100%;height:100%;background:var(--page)}
.empty{height:100%;display:grid;place-content:center;text-align:center;padding:24px;color:var(--muted)}
.empty h3{font-size:20px;color:var(--text);margin:0 0 8px}.empty p{max-width:30ch;margin:0}
[hidden]{display:none!important}.dismiss,.mobile-header{display:none}
@media(max-width:680px){.shell,.shell.collapsed{display:flex;flex-direction:column}
.mobile-header{height:56px;flex:none;display:flex;align-items:center;gap:12px;padding:0 12px;
background:var(--nav);border-bottom:1px solid var(--border);z-index:4}
.sidebar{position:fixed;inset:56px auto 0 0;width:244px;box-shadow:10px 0 32px #0002;overflow-y:auto}
.sidebar .brand,.collapsed .sidebar,.page-header{display:none}
.workspace{flex:1;width:100%}
.shell:not(.collapsed) .dismiss{display:block;position:fixed;inset:56px 0 0;border:0;background:#0003;z-index:2}}
</style></head><body>
<div class="shell" id="console-shell">
<header class="mobile-header"><button class="toggle" id="mobile-nav-toggle" type="button"
aria-controls="console-nav" aria-expanded="false" aria-label="Open navigation"><!-- MENU --></button><h1>Hana console</h1></header>
<aside class="sidebar"><div class="brand"><h1>Hana console</h1>
<button class="toggle" id="nav-toggle" type="button" aria-controls="console-nav" aria-expanded="true" aria-label="Collapse navigation">
<span class="close-icon"><!-- CLOSE --></span><span class="open-icon"><!-- OPEN --></span></button></div>
<nav id="console-nav" aria-label="Console pages"><!-- ENTRIES --></nav></aside>
<button class="dismiss" id="nav-dismiss" type="button" aria-label="Collapse navigation" tabindex="-1"></button>
<main class="workspace"><header class="page-header"><h2 id="page-title"></h2></header>
<div class="frame-area"><iframe id="page-frame" title="Selected console page" hidden></iframe>
<section class="empty" id="page-unavailable" hidden><h3>Not available</h3><p>This page is not available. Try another page or open a new console link.</p></section></div></main>
</div>
<script>
(() => {
  const shell = document.getElementById('console-shell');
  const toggle = document.getElementById('nav-toggle');
  const mobileToggle = document.getElementById('mobile-nav-toggle');
  const phone = matchMedia('(max-width:680px)');
  const entries = [...document.querySelectorAll('.nav-entry')];
  const frame = document.getElementById('page-frame');
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
    frame.hidden = !path;
    document.getElementById('page-unavailable').hidden = Boolean(path);
    frame.title = title;
    if (path) { if (frame.getAttribute('src') !== path) frame.src = path; }
    else frame.removeAttribute('src');
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
  document.addEventListener('keydown', event => { if (event.key === 'Escape') collapse(true, true); });
  select(new URL(location.href).searchParams.get('page'));
})();
</script></body></html>
"""
