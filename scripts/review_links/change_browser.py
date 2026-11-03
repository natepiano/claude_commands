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

from scripts.lib import urgency
from scripts.production import change_review, change_routing, showrunners, staff
from scripts.production.change_routing import UnavailableProduction, list_documents
from scripts.production.proposals_folder import slug_of
from scripts.review_links import change_page
from scripts.review_links.change_page import ReviewBrowser as ReviewBrowser

MANIFEST = change_page.BROWSER_MANIFEST


@dataclass(frozen=True)
class WaitingChange:
    key: str
    tab: str
    page: change_page.AssignedReviewPage | UnavailableProduction
    row: dict[str, str]
    validation_requested: bool = False
    anomaly: str = ""
    question: change_page.NoQuestion | change_page.OpenQuestion | change_page.AnsweredQuestion = change_page.NoQuestion()

    @property
    def waiting(self) -> bool:
        return (not self.row.get("Verdict") and not self.validation_requested
                and not (isinstance(self.question, change_page.OpenQuestion) and self.question.awaiting_answer))


@dataclass(frozen=True)
class PreparedReviews:
    changes: tuple[WaitingChange, ...]
    queues: dict[str, change_review.ChangeQueue]
    delivery: change_page.DeliveryReadView


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
                 for path in sorted(change_review.LISTS.glob(change_review.PREFIX + "*.md"))}
    documents = dict(showrunners.timer_docs())
    for path, snapshot in snapshots.items():
        docs = list_documents(snapshot.text)
        for doc in docs:
            _ = documents.setdefault(slug_of(doc), doc)
        if docs:
            _ = documents.setdefault(path.stem.removeprefix(change_review.PREFIX), docs[0])
    routing = change_routing.StaffRouting(staff.snapshot(), documents, change_review)
    routing.prepare((path.stem.removeprefix(change_review.PREFIX), snapshot.pending)
                    for path, snapshot in snapshots.items())
    grouped: dict[str, list[WaitingChange]] = {path.stem.removeprefix(change_review.PREFIX): [] for path in snapshots}
    grouped.update({runner.name: [] for runner in routing.runners.values()})
    defaults: dict[str, Path] = {}
    for path, snapshot in snapshots.items():
        fallback = path.stem.removeprefix(change_review.PREFIX)
        defaults[fallback] = path
        source_doc = routing.production_for_list(path)
        recovery = [row for row in snapshot.reviewed if isinstance(delivery.slots(
            change_page.ReviewPage(path, path, (row["#"],), ""), row["#"]).selected(True), change_page.DeliveryIntent)]
        for row in [*snapshot.pending, *recovery]:
            owner = routing.owner(row, fallback)
            tab = owner.tab
            key = fallback + "/" + row["#"]
            doc = (routing.documents.get(tab) if isinstance(source_doc, Path)
                   and tab != fallback and tab in routing.repositories else None)
            doc = doc or source_doc
            page = (change_page.AssignedReviewPage(path, doc, (row["#"],), row_token(browser, key), owner)
                    if isinstance(doc, Path) else doc)
            slots = delivery.slots(change_page.ReviewPage(path, path, (row["#"],), ""), row["#"])
            grouped.setdefault(tab, []).append(WaitingChange(
                key, tab, page, row, isinstance(slots.validation, change_page.ValidationRequested),
                owner.note if isinstance(owner, change_routing.UnroutedChange) else "", slots.question))
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
            if parent is None or parent not in routing.runners:
                break
            root = routing.runners[parent]
        current = (defaults.get(root.name) if root is not None else None) or routing.list_paths.get(tab) or defaults.get(tab, page.review_list)
        name = change_review.queue(current)
        if runner is not None and runner.parent is not None and isinstance(page, change_page.AssignedReviewPage):
            name += "-" + slug_of(page.production)
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
    return PreparedReviews(tuple(changes), queues, delivery)


def section(change: WaitingChange, status: change_page.DeliveryReadView) -> str:
    page, row = change.page, change.row
    anomaly = f"<p>{html.escape(change.anomaly)}</p>" if change.anomaly else ""
    if isinstance(page, UnavailableProduction):
        return f'''<section hidden data-key="{html.escape(change.key)}" data-tab="{html.escape(change.tab)}" data-pending="{str(change.waiting).lower()}">
<p class="kind">{html.escape(row.get(change_review.KIND) or 'Kind not recorded')}</p>
<div class="id">{html.escape(change.key)}</div><h1>{html.escape(row.get('Change', ''))}</h1>
<h2>Proof</h2><pre>{html.escape(row.get(change_review.CHECK, ''))}</pre>
{anomaly}{change_page.question_html(change.question)}<p>{html.escape(page.reason)}</p></section>'''
    verdict = row.get("Verdict", "")
    intent = status.slots(page, row["#"]).selected(bool(verdict))
    notice = ""
    retry = ""
    feedback = ""
    if isinstance(intent, change_page.DeliveryIntent):
        feedback = change.question.text if isinstance(change.question, change_page.OpenQuestion) else intent.feedback
        notice = ("The message may not have sent" if intent.state == "pending"
                  else "Saved, but the message did not send" if verdict
                  else "The question message did not send" if isinstance(change.question, change_page.OpenQuestion)
                  else "The validation message did not send")
        if not verdict:
            retry = ('<button type="button" data-resend data-outcome="retry">Send question again</button>'
                     if isinstance(change.question, change_page.OpenQuestion)
                     else '<button type="button" data-resend>Send validation request again</button>')
    form = change_page.answer_form(page, change_page.AnswerNotice(row["#"], feedback=feedback))
    if verdict:
        form = f'''<form method="post"><input type="hidden" name="row" value="{html.escape(row['#'])}">
<input type="hidden" name="token" value="{html.escape(page.token)}"><input type="hidden" name="feedback" value="">
<button name="outcome" value="retry">Send saved answer again</button></form>'''
    elif change.validation_requested:
        form = ""
    elif isinstance(change.question, change_page.OpenQuestion):
        form = (f'''<form method="post"><input type="hidden" name="row" value="{html.escape(row['#'])}">
<input type="hidden" name="token" value="{html.escape(page.token)}">
<input type="hidden" name="feedback" value=""></form>''' if retry else "")
    form = form.replace('<form method="post">', f'<form method="post" action="{change_page.PAGE_NAME}">')
    saved = (f'<p>Saved: {html.escape(verdict)}</p>' if verdict else
             '<p>The showrunner will validate this</p>' if change.validation_requested else "")
    return f'''<section hidden data-key="{html.escape(change.key)}" data-tab="{html.escape(change.tab)}" data-pending="{str(change.waiting).lower()}">
<p class="kind">{html.escape(row.get(change_review.KIND) or 'Kind not recorded')}</p>
<div class="id">{html.escape(change.key)} · {html.escape(row.get('Merged', ''))}</div>
<h1>{html.escape(row.get('Change', ''))}</h1>
<h2>{'Proof' if row.get(change_review.KIND) == 'Read only' else 'How to check'}</h2>
<pre>{html.escape(row.get(change_review.CHECK, ''))}</pre>
{anomaly}{change_page.question_html(change.question)}{saved}<p role="status" data-save-state>{html.escape(notice)}</p>{form}{retry}</section>'''


def render(browser: ReviewBrowser, status: change_page.DeliveryStatus) -> bytes:
    prepared = prepare(browser, status)
    tabs = ''.join(f'<button type="button" role="tab" data-showrunner="{html.escape(tab)}">'
                   + f'{html.escape(tab)} (<span data-count>{sum(item.tab == tab and item.waiting for item in prepared.changes)}</span>)</button>'
                   for tab in prepared.queues)
    recovery = ''.join('<p><a href="#' + html.escape(urlencode({"showrunner": item.tab, "row": item.key}))
                       + '">Message needs attention: ' + html.escape(item.key) + '</a></p>'
                       for item in prepared.changes if item.row.get("Verdict") or (
                           isinstance(item.question, change_page.OpenQuestion)
                           and isinstance(item.question.delivery, change_page.DeliveryIntent)))
    content = ('''<style>[hidden]{display:none!important}nav{display:flex;gap:4px;overflow-x:auto;padding-bottom:8px}
nav button{white-space:nowrap;margin:0}nav button[aria-selected=true]{outline:2px solid currentColor;outline-offset:-2px}
section+section{border:0;margin:0;padding:0}
.navigation{position:sticky;bottom:0;padding:12px 0;background:Canvas}button{min-height:44px}
</style><h1>Changes to review</h1><nav role="tablist" aria-label="Showrunner">''' + tabs + '</nav>' + recovery
               + ''.join(section(item, prepared.delivery) for item in prepared.changes)
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
        # A saved row can retry delivery with its original signed token.
        source, separator, row_id = key.partition("/")
        paths = {path.stem.removeprefix(change_review.PREFIX): path
                 for path in change_review.LISTS.glob(change_review.PREFIX + "*.md")}
        if not separator or source not in paths or re.fullmatch(r"C\d+", row_id) is None:
            raise change_page.AnswerRefused("this change is not on the page")
        empty: dict[str, str] = {}
        row = next((row for row in change_review.review_rows(paths[source]) if row["#"] == row_id), empty)
        if not row.get("Verdict"):
            raise change_page.AnswerRefused("this change is not on the page")
        documents = dict(showrunners.timer_docs())
        for doc in list_documents(change_review.read(paths[source])):
            _ = documents.setdefault(slug_of(doc), doc)
        routing = change_routing.StaffRouting(staff.snapshot(), documents, change_review)
        owner = routing.owner(row, source)
        tab = owner.tab
        doc = routing.production_for_list(paths[source])
        if isinstance(doc, UnavailableProduction):
            raise change_page.AnswerRefused("the production document is not available")
        page = change_page.AssignedReviewPage(paths[source], doc, (row_id,), row_token(browser, key), owner)
        found = WaitingChange(key, tab, page, row)
    if isinstance(found.page, UnavailableProduction):
        raise change_page.AnswerRefused("the production document is not available")
    fields["row"] = [found.row["#"]]
    rewritten = urlencode({key: values[0] for key, values in fields.items()})
    queue = prepared.queues[found.tab]
    if found.validation_requested and fields["outcome"] == ["validate"]:
        entry = change_review.row_entries([found.row])[0]
        queue = replace(queue, entries=[*queue.entries, replace(entry, key=change_page.queue_key(found.page, found.row["#"]))])
    delivery = change_page.submit(found.page, rewritten, status, prepared=queue)
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
  state.textContent=reply.answer ? 'Saved: '+reply.answer : outcome==='question' || row.querySelector('[data-resend][data-outcome="retry"]') ? 'The showrunner will answer this' : 'The showrunner will validate this';
  row.dataset.saveState='saved';
  row.dataset.pending='false';form.hidden=true;
  row.querySelector('[data-resend]')?.remove();
  if(current===row && tab===row.dataset.tab && navigation===position) advance(row);
  else show();
 }catch(error){
  row.dataset.saveState='failed'; state.textContent='Not saved: '+(error instanceof SyntaxError ? 'The answer could not be saved. Try again.' : error.message);
  let retry=row.querySelector('[data-resend]');
  if(!retry){retry=document.createElement('button');retry.type='button';retry.dataset.resend='';row.append(retry);}
  retry.textContent='Send answer again';retry.onclick=()=>save(row,form,outcome);
 }finally{form.dataset.saving='false';}
}
rows.forEach(row=>{
 const form=row.querySelector('form');
 if(!form) return;
 form.addEventListener('submit',event=>{event.preventDefault();save(row,form,event.submitter.value);});
 const retry=row.querySelector('[data-resend]');
 if(retry) retry.onclick=()=>save(row,form,retry.dataset.outcome || 'validate'); });
window.addEventListener('hashchange',()=>{if(location.hash!==shownHash) navigation++;show();});show();
'''
