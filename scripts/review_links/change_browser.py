"""Prepare every change once, with staff tabs and local browser navigation."""
from __future__ import annotations

import hashlib
import hmac
import html
import json
import re
import secrets
from dataclasses import dataclass, replace
from pathlib import Path
from typing import cast
from urllib.parse import parse_qs, urlencode, urlsplit

from scripts.lib import conf, urgency
from scripts.production import change_review, change_routing, staff
from scripts.production.change_routing import UnavailableProduction
from scripts.review_links import change_page
from scripts.review_links.change_page import ReviewBrowser as ReviewBrowser

MANIFEST = change_page.BROWSER_MANIFEST


@dataclass(frozen=True)
class WaitingChange:
    key: str
    tab: str
    page: change_page.AssignedReviewPage | UnavailableProduction
    row: dict[str, str]
    slots: change_page.DeliverySlots = change_page.DeliverySlots(change_page.NoDelivery(), change_page.NoDelivery())
    anomaly: str = ""
    @property
    def question(self) -> change_page.NoQuestion | change_page.OpenQuestion | change_page.AnsweredQuestion:
        return self.slots.question

    @property
    def waiting(self) -> bool:
        return self.slots.waits_for_user(bool(self.row.get("Verdict")))


@dataclass(frozen=True)
class PreparedReviews:
    changes: tuple[WaitingChange, ...]
    queues: dict[str, change_review.ChangeQueue]


def page_at(root: Path, path: str) -> ReviewBrowser:
    match = re.fullmatch(r"/([a-z0-9]{6})/changes\.html", urlsplit(path).path)
    if match is None:
        raise FileNotFoundError("not a change browser")
    folder = root / match[1]
    manifest = folder / MANIFEST
    if folder.is_symlink() or manifest.is_symlink():
        raise ValueError("not a regular browser manifest")
    value = cast(object, json.loads(manifest.read_text(encoding="utf-8")))
    if not isinstance(value, dict):
        raise ValueError("invalid browser manifest")
    fields = cast(dict[str, object], value)
    token, initial = fields.get("token"), fields.get("initial")
    if not isinstance(token, str) or not token or not isinstance(initial, str):
        raise ValueError("invalid browser manifest")
    return ReviewBrowser(token, initial)


def publish(initial: str = "") -> str:
    return change_page.publish_browser(initial)


def row_token(browser: ReviewBrowser, key: str) -> str:
    return hmac.new(browser.token.encode(), key.encode(), hashlib.sha256).hexdigest()


def prepare(browser: ReviewBrowser, status: change_page.DeliveryStatus) -> PreparedReviews:
    delivery = status.read_view()
    snapshots = {path: change_review.review_snapshot(path)
                 for path in sorted(change_review.lists_directory().glob(change_review.PREFIX + "*.md"))}
    routing = change_page.list_routing(snapshots)
    grouped: dict[str, list[WaitingChange]] = {path.stem.removeprefix(change_review.PREFIX): [] for path in snapshots}
    grouped.update({runner.name: [] for runner in routing.runners.values()})
    defaults: dict[str, Path] = {}
    for path, snapshot in snapshots.items():
        fallback = path.stem.removeprefix(change_review.PREFIX)
        defaults[fallback] = path
        for row in snapshot.pending:
            assignment = routing.owner_and_production(path, row)
            owner = assignment.owner
            tab = owner.tab
            key = fallback + "/" + row["#"]
            doc = assignment.production
            page = (change_page.AssignedReviewPage(path, doc, (row["#"],), row_token(browser, key), owner)
                    if isinstance(doc, Path) else doc)
            slots = delivery.slots(change_page.ReviewPage(path, path, (row["#"],), ""), row["#"])
            grouped.setdefault(tab, []).append(WaitingChange(
                key, tab, page, row, slots,
                owner.note if isinstance(owner, change_routing.UnroutedChange) else ""))
    queues: dict[str, change_review.ChangeQueue] = {}
    changes: list[WaitingChange] = []
    for tab, items in sorted(grouped.items()):
        if not items:
            queues[tab] = change_review.ChangeQueue("", [])
            continue
        page = items[0].page
        runner = next((runner for runner in routing.runners.values() if runner.name == tab), None)
        root = runner
        while root is not None:
            parent = root.parent
            if not isinstance(parent, staff.ReportsTo) or parent.id not in routing.runners:
                break
            root = routing.runners[parent.id]
        current = (defaults.get(root.name) if root is not None else None) or routing.list_paths.get(tab) or defaults.get(tab, page.review_list)
        name = change_review.queue(current)
        if runner is not None and isinstance(runner.parent, staff.ReportsTo) and isinstance(page, change_page.AssignedReviewPage):
            name += "-" + runner.name
        entries: list[urgency.Entry[dict[str, str]]] = []
        by_key: dict[str, WaitingChange] = {}
        for item in items:
            key = item.row["#"] if item.page.review_list == current else item.key
            by_key[key] = item
            if item.waiting:
                prefix = "" if item.page.review_list == current else item.key.rsplit("/", 1)[0] + "/"
                entry = change_review.row_entries([item.row])[0]
                after = prefix + entry.after if entry.after and "/" not in entry.after else entry.after
                entries.append(replace(entry, key=key, item=item.row | {"#": key}, after=after))
        prepared = change_review.ChangeQueue(name, entries)
        changes.extend(by_key[entry.key] for entry in urgency.queue_order(prepared.entries, prepared.name))
        changes.extend(item for item in items if not item.waiting)
        queues[tab] = prepared
    queues = dict(sorted(queues.items(), key=lambda item: not item[1].entries))
    return PreparedReviews(tuple(changes), queues)


def section(change: WaitingChange, feedback_rows: int) -> str:
    page, row = change.page, change.row
    anomaly = f"<p>{html.escape(change.anomaly)}</p>" if change.anomaly else ""
    if isinstance(page, UnavailableProduction):
        return f'''<section hidden data-key="{html.escape(change.key)}" data-tab="{html.escape(change.tab)}" data-pending="{str(change.waiting).lower()}">
<p class="kind">{html.escape(row.get(change_review.KIND) or 'Kind not recorded')}</p>
<div class="id">{html.escape(change.key)}</div><h1>{html.escape(row.get('Change', ''))}</h1>
<h2>Proof</h2>{change_page.review_text_html(row.get(change_review.CHECK, ''), "pre")}
{anomaly}{change_page.question_html(change.question)}<p>{html.escape(page.reason)}</p></section>'''
    form = change_page.answer_form(page, change_page.AnswerNotice(row["#"]), feedback_rows).replace(
        '<form method="post">', f'<form method="post" action="{change_page.PAGE_NAME}">') if change.waiting else ""
    text = change_page.answer_status(row.get("Verdict", ""), change.slots)
    saved = f'<p>{html.escape(text)}</p>' if text else ""
    return f'''<section hidden data-key="{html.escape(change.key)}" data-tab="{html.escape(change.tab)}" data-pending="{str(change.waiting).lower()}">
<p class="kind">{html.escape(row.get(change_review.KIND) or 'Kind not recorded')}</p>
<div class="id">{html.escape(change.key)} · {html.escape(row.get('Merged', ''))}</div>
<h1>{html.escape(row.get('Change', ''))}</h1>
<h2>{'Proof' if row.get(change_review.KIND) == 'Read only' else 'How to check'}</h2>
{change_page.review_text_html(row.get(change_review.CHECK, ''), "pre")}
{anomaly}{change_page.question_html(change.question)}{saved}<p role="status" data-save-state></p>{form}</section>'''


def render(browser: ReviewBrowser, status: change_page.DeliveryStatus) -> bytes:
    feedback_rows = conf.read("review_links").whole("feedback_rows")
    prepared = prepare(browser, status)
    tabs = ''.join(f'<button type="button" role="tab" data-showrunner="{html.escape(tab)}">'
                   + f'{html.escape(tab)} (<span data-count>{sum(item.tab == tab and item.waiting for item in prepared.changes)}</span>)</button>'
                   for tab in prepared.queues)
    content = ('''<style>[hidden]{display:none!important}nav{display:flex;gap:4px;overflow-x:auto;padding-bottom:8px}
nav button{white-space:nowrap;margin:0}nav button[aria-selected=true]{outline:2px solid currentColor;outline-offset:-2px}
section+section{border:0;margin:0;padding:0}
.navigation{position:sticky;bottom:0;padding:12px 0;background:Canvas}button{min-height:44px}
</style><h1>Changes to review</h1><nav role="tablist" aria-label="Showrunner">''' + tabs + '</nav>'
               + ''.join(section(item, feedback_rows) for item in prepared.changes)
               + '<p data-left></p><div class="navigation"><button type="button" data-next hidden>Next</button></div>'
               + '<noscript>Enable JavaScript to review changes and use Next.</noscript>'
               + '<script>const initial=' + json.dumps(browser.initial).replace('<', '\\u003c') + ';' + SCRIPT + '</script>')
    return change_page.document(content)


def submit(browser: ReviewBrowser, body: str, status: change_page.DeliveryStatus) -> tuple[change_page.MessageDelivery | change_page.NoMessageDelivery, str]:
    fields = parse_qs(body, keep_blank_values=True, strict_parsing=True)
    if set(fields) != {"row", "token", "outcome", "feedback"} or any(len(value) != 1 for value in fields.values()):
        raise change_page.AnswerRefused("use the answer form on this page")
    key = fields["row"][0]
    if not secrets.compare_digest(fields["token"][0].encode(), row_token(browser, key).encode()):
        raise change_page.AnswerRefused("row or page token does not match this page")
    prepared = prepare(browser, status)
    found = next((item for item in prepared.changes if item.key == key), None)
    if found is None:
        # A repeated save uses its original signed token.
        source, separator, row_id = key.partition("/")
        paths = {path.stem.removeprefix(change_review.PREFIX): path
                 for path in change_review.lists_directory().glob(change_review.PREFIX + "*.md")}
        if not separator or source not in paths or re.fullmatch(r"C\d+", row_id) is None:
            raise change_page.AnswerRefused("this change is not on the page")
        empty: dict[str, str] = {}
        row = next((row for row in change_review.review_rows(paths[source]) if row["#"] == row_id), empty)
        if not row.get("Verdict"):
            raise change_page.AnswerRefused("this change is not on the page")
        routing = change_page.list_routing({paths[source]: change_review.review_snapshot(paths[source])})
        assignment = routing.owner_and_production(paths[source], row)
        owner = assignment.owner
        tab = owner.tab
        doc = assignment.production
        if isinstance(doc, UnavailableProduction):
            raise change_page.AnswerRefused("the production document is not available")
        page = change_page.AssignedReviewPage(paths[source], doc, (row_id,), row_token(browser, key), owner)
        found = WaitingChange(key, tab, page, row)
    if isinstance(found.page, UnavailableProduction):
        raise change_page.AnswerRefused("the production document is not available")
    fields["row"] = [found.row["#"]]
    rewritten = urlencode({key: values[0] for key, values in fields.items()})
    delivery = change_page.submit(found.page, rewritten, status, prepared=prepared.queues[found.tab])
    answer = ""
    if fields["outcome"] not in (["validate"], ["question"]):
        answer = next(row.get("Verdict", "") for row in change_review.review_rows(found.page.review_list)
                      if row["#"] == found.row["#"])
    return delivery, answer


SCRIPT = r'''
const rows=[...document.querySelectorAll('section[data-key]')];
const tabs=[...document.querySelectorAll('[data-showrunner]')];
const next=document.querySelector('[data-next]'), left=document.querySelector('[data-left]');
const remembered=new Map(); let current=null, tab='', navigation=0, shownHash='';
function waiting(){return rows.filter(r=>r.dataset.tab===tab && r.dataset.pending==='true');}
function show(){
 const place=new URLSearchParams(location.hash.slice(1));
 const requested=place.has('row') ? place.get('row') : initial;
 tab=place.get('showrunner') || rows.find(r=>r.dataset.key===requested)?.dataset.tab || tabs[0]?.dataset.showrunner;
 if(!tabs.some(t=>t.dataset.showrunner===tab)) tab=tabs[0]?.dataset.showrunner;
 current=rows.find(r=>r.dataset.key===requested && r.dataset.tab===tab) || waiting()[0] || null;
 rows.forEach(r=>r.hidden=r!==current);
 tabs.forEach(t=>{t.setAttribute('aria-selected',String(t.dataset.showrunner===tab));
  t.querySelector('[data-count]').textContent=rows.filter(r=>r.dataset.tab===t.dataset.showrunner && r.dataset.pending==='true').length;});
 left.textContent=waiting().length+' changes left'; next.hidden=waiting().length===0;
 if(current) remembered.set(tab,current.dataset.key);
 const hash=new URLSearchParams({showrunner:tab || '',row:current?.dataset.key || ''});
 history.replaceState(null,'','#'+hash); shownHash=location.hash; }
function go(key){navigation++;location.hash=new URLSearchParams({showrunner:tab,row:key || ''});show();}
tabs.forEach(t=>t.addEventListener('click',()=>{tab=t.dataset.showrunner;go(remembered.get(tab));}));
function advance(row){
 const at=rows.indexOf(row), candidates=waiting();
 const following=candidates.find(r=>rows.indexOf(r)>at) || candidates[0];
 go(following?.dataset.key || ''); }
next.addEventListener('click',()=>advance(current));
async function save(row,form,outcome){
 if(form.dataset.saving==='true') return;
 form.dataset.saving='true'; const state=row.querySelector('[data-save-state]'), position=navigation;
 const data=new URLSearchParams(new FormData(form));
 data.set('row',row.dataset.key);data.set('outcome',outcome);
 form.querySelectorAll('button[name=outcome]').forEach(b=>b.setAttribute('aria-pressed',String(b.value===outcome)));
 state.textContent='Saving'; row.dataset.saveState='saving';
 try{
  const response=await fetch(form.action,{method:'POST',body:data,headers:{Accept:'application/json'}});
  const reply=await response.json();
  if(!response.ok) throw new Error(reply.reason || 'The answer could not be saved. Try again.');
  state.textContent=reply.answer ? 'Saved: '+reply.answer : outcome==='question' ? 'The showrunner will answer this' : 'The showrunner will validate this';
  row.dataset.saveState='saved';
  row.dataset.pending='false';form.hidden=true;
  if(current===row && tab===row.dataset.tab && navigation===position) advance(row);
  else show();
 }catch(error){
  row.dataset.saveState='failed'; state.textContent='Not saved: '+(error instanceof SyntaxError ? 'The answer could not be saved. Try again.' : error.message);
 }finally{form.dataset.saving='false';}
}
rows.forEach(row=>{
 const form=row.querySelector('form');
 if(!form) return;
 form.addEventListener('submit',event=>{event.preventDefault();save(row,form,event.submitter.value);});
 });
window.addEventListener('hashchange',()=>{if(location.hash!==shownHash) navigation++;show();});show();
'''
