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
from scripts.production import change_review, change_routing, staff, update_registration
from scripts.production.production_doc import read_production
from scripts.production.issues import frontmatter
from scripts.rebuild import need
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
class WaitingQuestion:
    """A filed question stays in its owning production's review tab."""

    key: str
    tab: str
    record: update_registration.QuestionRecord
    production: Path | update_registration.QuestionOwnerUnavailable
    owner: staff.StaffId
    asker: str


@dataclass(frozen=True)
class PreparedReviews:
    changes: tuple[WaitingChange, ...]
    queues: dict[str, change_review.ChangeQueue]
    questions: tuple[WaitingQuestion, ...]
    question_tabs: tuple[str, ...]
    record: staff.StaffSnapshot


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
    identities = update_registration.QuestionStaff.for_record(routing.record)
    questions: list[WaitingQuestion] = []
    for document, lines in routing.document_lines.items():
        for question in update_registration.question_records_for_document(document, identities, include_unrouted=True, lines=lines):
            if question.status != "open":
                continue
            owner = routing.runners.get(question.owner)
            tab = owner.name if owner is not None else "unrouted"
            asker = routing.record.by_id(question.asker)
            questions.append(WaitingQuestion("question/" + question.owner + "/" + question.path.name,
                tab, question, document, question.owner, asker.name if asker is not None else str(question.asker)))
    known = {item.record.path for item in questions}
    for question in update_registration.unowned_question_records(identities):
        if question.status != "open" or question.path in known:
            continue
        asker = routing.record.by_id(question.asker)
        questions.append(WaitingQuestion("question/" + question.owner + "/" + question.path.name,
            "unrouted", question, update_registration.QuestionOwnerUnavailable(question.path), question.owner,
            asker.name if asker is not None else str(question.asker)))
    questions.sort(key=lambda item: (item.record.item["since"], item.key))
    question_tabs = tuple(sorted(set(queues) | {item.tab for item in questions},
                                 key=lambda tab: (not any(item.tab == tab for item in questions), tab)))
    return PreparedReviews(tuple(changes), queues, tuple(questions), question_tabs, routing.record)


def section(change: WaitingChange, feedback_rows: int) -> str:
    page, row = change.page, change.row
    anomaly = f"<p>{html.escape(change.anomaly)}</p>" if change.anomaly else ""
    if isinstance(page, UnavailableProduction):
        return f'''<section hidden data-type="changes" data-key="{html.escape(change.key)}" data-tab="{html.escape(change.tab)}" data-pending="{str(change.waiting).lower()}">
<p class="kind">{html.escape(row.get(change_review.KIND) or 'Kind not recorded')}</p>
<div class="id">{html.escape(change.key)}</div><h1>{html.escape(row.get('Change', ''))}</h1>
<h2>Proof</h2>{change_page.review_text_html(row.get(change_review.CHECK, ''), "pre")}
{anomaly}{change_page.question_html(change.question)}<p>{html.escape(page.reason)}</p></section>'''
    form = change_page.answer_form(page, change_page.AnswerNotice(row["#"]), feedback_rows).replace(
        '<form method="post">', f'<form method="post" action="{change_page.PAGE_NAME}">') if change.waiting else ""
    text = change_page.answer_status(row.get("Verdict", ""), change.slots)
    saved = f'<p>{html.escape(text)}</p>' if text else ""
    return f'''<section hidden data-type="changes" data-key="{html.escape(change.key)}" data-tab="{html.escape(change.tab)}" data-pending="{str(change.waiting).lower()}">
<p class="kind">{html.escape(row.get(change_review.KIND) or 'Kind not recorded')}</p>
<div class="id">{html.escape(change.key)} · {html.escape(row.get('Merged', ''))}</div>
<h1>{html.escape(row.get('Change', ''))}</h1>
<h2>{'Proof' if row.get(change_review.KIND) == 'Read only' else 'How to check'}</h2>
{change_page.review_text_html(row.get(change_review.CHECK, ''), "pre")}
{anomaly}{change_page.question_html(change.question)}{saved}<p role="status" data-save-state></p>{form}</section>'''


def question_section(question: WaitingQuestion, browser: ReviewBrowser, feedback_rows: int) -> str:
    key = html.escape(question.key)
    form = '<p>No showrunner owns this question.</p>' if question.tab == "unrouted" else f'''<form method="post" action="{change_page.PAGE_NAME}">
<input type="hidden" name="row" value="{key}">
<input type="hidden" name="token" value="{row_token(browser, question.key)}">
<label for="answer-{key}">Answer</label><textarea id="answer-{key}" name="feedback" rows="{feedback_rows}"></textarea>
<button name="outcome" value="answer">Save answer</button></form>'''
    return f'''<section hidden data-type="questions" data-key="{key}" data-tab="{html.escape(question.tab)}" data-pending="true">
<p class="kind">Question</p><h1>{html.escape(question.record.item["text"])}</h1>
{change_page.review_text_html(question.record.item.get("question", ""))}
<p>Asked by {html.escape(question.asker)}</p><p role="status" data-save-state></p>{form}</section>'''


def rebuild_list(machines: list[need.MachineNeeds], record: staff.StaffSnapshot) -> str:
    """Show each machine's reasons with the current staff names."""
    def name(identity: str) -> str:
        member = record.by_id(staff.StaffId(identity))
        return html.escape(member.name if member is not None else identity)

    blocks: list[str] = []
    for machine in machines:
        items: list[str] = []
        for item in machine.records:
            requester = (" · requested by " + name(item["requested_by"])
                         if item["requested_by"] and item["requested_by"] != item["concerns"] else "")
            items.append(f'<li><p>{html.escape(item["reason"])}</p><p>For {name(item["concerns"])}{requester}</p></li>')
        blocks.append(f'<article data-machine="{html.escape(machine.newest["machine"])}">'
            + f'<h2>{html.escape(machine.newest["machine"])} · <code>{html.escape(machine.newest["nixos_commit"][:7])}</code></h2>'
            + '<ul>' + ''.join(items) + '</ul></article>')
    return (f'<div hidden data-rebuilds data-count="{len(machines)}">'
            + (''.join(blocks) if blocks else '<p>No rebuild is needed.</p>') + '</div>')


def render(browser: ReviewBrowser, status: change_page.DeliveryStatus) -> bytes:
    feedback_rows = conf.read("review_links").whole("feedback_rows")
    prepared = prepare(browser, status)
    rebuilds = need.read_needs()
    tabs = ''.join(f'<button type="button" role="tab" data-type="changes" data-showrunner="{html.escape(tab)}">'
                   + f'{html.escape(tab)} (<span data-count>{sum(item.tab == tab and item.waiting for item in prepared.changes)}</span>)</button>'
                   for tab in prepared.queues)
    question_tabs = ''.join(f'<button type="button" role="tab" data-type="questions" data-showrunner="{html.escape(tab)}">'
        + f'{html.escape(tab)} (<span data-count>{sum(item.tab == tab for item in prepared.questions)}</span>)</button>'
        for tab in prepared.question_tabs)
    type_tabs = ('<nav role="tablist" aria-label="Review type">'
        + f'<button type="button" role="tab" data-review-type="questions">Questions (<span data-type-count="questions">{len(prepared.questions)}</span>)</button>'
        + f'<button type="button" role="tab" data-review-type="changes">Changes (<span data-type-count="changes">{sum(item.waiting for item in prepared.changes)}</span>)</button>'
        + f'<button type="button" role="tab" data-review-type="rebuilds">Rebuilds (<span data-type-count="rebuilds">{len(rebuilds)}</span>)</button></nav>')
    content = ('''<style>[hidden]{display:none!important}nav{display:flex;gap:4px;overflow-x:auto;padding-bottom:8px}
nav button{white-space:nowrap;margin:0}nav button[aria-selected=true]{outline:2px solid currentColor;outline-offset:-2px}
section+section{border:0;margin:0;padding:0}
[data-rebuilds] article+article{border-top:1px solid GrayText;margin-top:24px;padding-top:12px}
[data-rebuilds] li p{margin:0}[data-rebuilds] li p+p{margin-top:4px}[data-rebuilds] li+li{margin-top:16px}
.navigation{position:sticky;bottom:0;padding:12px 0;background:Canvas}button{min-height:44px}
</style><h1 data-review-heading>Changes to review</h1>''' + type_tabs + '<nav role="tablist" aria-label="Showrunner">' + tabs + question_tabs + '</nav>'
               + ''.join(section(item, feedback_rows) for item in prepared.changes)
               + ''.join(question_section(item, browser, feedback_rows) for item in prepared.questions)
               + rebuild_list(rebuilds, prepared.record)
               + '<p hidden data-empty></p><p data-total></p><div class="navigation"><button type="button" data-next hidden>Next</button></div>'
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
    if key.startswith("question/"):
        question = next((item for item in prepared.questions if item.key == key), None)
        if question is None:
            raise change_page.QuestionAnswerRefused("this question is no longer open; refresh the page")
        if fields["outcome"] != ["answer"] or not fields["feedback"][0].strip():
            raise change_page.AnswerRefused("enter an answer")
        return submit_question(question, fields["feedback"][0], status)
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


def submit_question(question: WaitingQuestion, answer: str, status: change_page.DeliveryStatus) -> tuple[change_page.MessageDelivery, str]:
    """Save through the production API before the existing sender delivers the answer."""
    if question.tab == "unrouted":
        raise change_page.QuestionAnswerRefused("No showrunner owns this question.")
    identities = update_registration.QuestionStaff.read()
    production = read_production(question.owner, identities.record)
    item = question.record.item
    relay = update_registration.answer_relay(production, production.name,
        update_registration.question_asker(item, update_registration.OpenItemWithoutAsker(), identities), identities)
    recipient = relay.recipient if isinstance(relay.recipient, str) else question.owner
    message = "From the user (via the review page): " + item["text"]
    if relay.unit:
        message += "\nFor: " + relay.unit
    message += "\n" + answer
    saved: change_page.StoredUserAnswer = {"saved": False, "source": str(question.record.path), "production": str(production.doc),
        "owner": question.owner, "question": item["text"], "answer": answer, "message": message}
    page = change_page.UserQuestionPage(question.record.path, production.doc, (question.record.path.stem,), "", saved)
    # The delivery lock prevents startup recovery from reading an answer during its save.
    with status.locked():
        if frontmatter(question.record.path).get("status") != ["open"]:
            raise change_page.QuestionAnswerRefused("this question is no longer open; refresh the page")
        attempt = secrets.token_hex()
        intent = change_page.DeliveryIntent(attempt, "verdict", recipients=(recipient,))
        status.write_slots(page, page.rows[0], change_page.DeliverySlots(change_page.NoDelivery(), intent))
        status.request_attempts[attempt] = change_page.AttemptOwnership(status.path(page, page.rows[0]), "claimed")
        try:
            relay = update_registration.answer(production, item["text"], identities=identities, only_open=True)
            recipient = relay.recipient if isinstance(relay.recipient, str) else question.owner
            intent = replace(intent, recipients=(recipient,))
            page = replace(page, saved={**saved, "saved": True})
            status.write_slots(page, page.rows[0], change_page.DeliverySlots(change_page.NoDelivery(), intent))
        except BaseException as error:
            try:
                status.clear_locked(page, page.rows[0], status.slots(page, page.rows[0]), "verdict")
            finally:
                _ = status.request_attempts.pop(attempt, None)
            if isinstance(error, update_registration.RegistrationFailure):
                raise change_page.QuestionAnswerRefused("this question is no longer open; refresh the page") from error
            raise
    return change_page.MessageDelivery(page, page.rows[0], (recipient,), message, attempt, (question.owner,)), answer


SCRIPT = r'''
const rows=[...document.querySelectorAll('section[data-key]')];
const allTabs=[...document.querySelectorAll('[data-showrunner]')];
const types=[...document.querySelectorAll('[data-review-type]')];
const rebuilds=document.querySelector('[data-rebuilds]'), rebuildCount=Number(rebuilds.dataset.count);
const showrunners=document.querySelector('nav[aria-label="Showrunner"]');
const total=document.querySelector('[data-total]');
const next=document.querySelector('[data-next]'), empty=document.querySelector('[data-empty]');
const heading=document.querySelector('[data-review-heading]');
const remembered=new Map(), places=new Map(); let current=null, tab='', kind='changes', navigation=0, shownHash='';
function waiting(){return rows.filter(r=>r.dataset.type===kind && r.dataset.tab===tab && r.dataset.pending==='true');}
function show(){
 const place=new URLSearchParams(location.hash.slice(1));
 const requested=place.has('row') ? place.get('row') : initial;
 kind=place.get('type') || rows.find(r=>r.dataset.key===requested)?.dataset.type || (rows.some(r=>r.dataset.type==='questions') ? 'questions' : 'changes');
 const tabs=allTabs.filter(t=>t.dataset.type===kind);
 const rebuildMode=kind==='rebuilds';
 heading.textContent={questions:'Questions to answer',changes:'Changes to review',rebuilds:'Rebuilds needed'}[kind];
 rebuilds.hidden=!rebuildMode;showrunners.hidden=rebuildMode;
 document.querySelector('.navigation').hidden=rebuildMode;
 types.forEach(t=>{t.setAttribute('aria-selected',String(t.dataset.reviewType===kind));
  t.querySelector('[data-type-count]').textContent=t.dataset.reviewType==='rebuilds' ? rebuildCount : rows.filter(r=>r.dataset.type===t.dataset.reviewType && r.dataset.pending==='true').length;});
 allTabs.forEach(t=>t.hidden=t.dataset.type!==kind);
 tab=place.get('showrunner') || rows.find(r=>r.dataset.key===requested)?.dataset.tab || tabs[0]?.dataset.showrunner;
 if(!tabs.some(t=>t.dataset.showrunner===tab)) tab=tabs[0]?.dataset.showrunner;
 current=rows.find(r=>r.dataset.key===requested && r.dataset.tab===tab && r.dataset.type===kind) || waiting()[0] || null;
 empty.hidden=rebuildMode || current!==null;
 empty.textContent=kind==='questions' ? 'No questions are waiting.' : 'No changes are waiting.';
 rows.forEach(r=>r.hidden=r!==current);
 tabs.forEach(t=>{t.setAttribute('aria-selected',String(t.dataset.showrunner===tab && t.dataset.type===kind));
  t.querySelector('[data-count]').textContent=rows.filter(r=>r.dataset.type===t.dataset.type && r.dataset.tab===t.dataset.showrunner && r.dataset.pending==='true').length;});
 total.textContent=(rows.filter(r=>r.dataset.pending==='true').length+rebuildCount)+' waiting'; next.hidden=rebuildMode || waiting().length===0;
 if(current) remembered.set(kind+'/'+tab,current.dataset.key);
 const hash=new URLSearchParams(rebuildMode ? {type:kind} : {type:kind,showrunner:tab || '',row:current?.dataset.key || ''});
 places.set(kind,hash.toString()); history.replaceState(null,'','#'+hash); shownHash=location.hash; }
function go(key){navigation++;location.hash=new URLSearchParams({type:kind,showrunner:tab,row:key || ''});show();}
allTabs.forEach(t=>t.addEventListener('click',()=>{tab=t.dataset.showrunner;go(remembered.get(kind+'/'+tab));}));
types.forEach(t=>t.addEventListener('click',()=>{navigation++;kind=t.dataset.reviewType;
 location.hash=places.get(kind) || new URLSearchParams({type:kind});show();}));
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
