"""Prepare every change once, with staff tabs and local browser navigation."""
from __future__ import annotations

import hashlib
import hmac
import html
import json
import re
import secrets
from dataclasses import dataclass, replace
from datetime import datetime
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
                entry = change_review.row_entries([item.row])[0]
                after = change_review.prerequisite_key(entry.after, item.page.review_list, current)
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
    shown = change_review.display_id(row, change.tab)
    anomaly = f"<p>{html.escape(change.anomaly)}</p>" if change.anomaly else ""
    if isinstance(page, UnavailableProduction):
        return f'''<section hidden data-type="changes" data-key="{html.escape(change.key)}" data-tab="{html.escape(change.tab)}" data-pending="{str(change.waiting).lower()}">
<h1>{html.escape(row.get('Change', ''))}</h1>
{change_page.change_details(row, shown)}
<h2>Proof</h2>{change_page.review_text_html(row.get(change_review.CHECK, ''), "pre")}
{anomaly}{change_page.question_html(change.question)}<p>{html.escape(page.reason)}</p></section>'''
    form = change_page.answer_form(page, change_page.AnswerNotice(row["#"]), feedback_rows).replace(
        '<form method="post">', f'<form method="post" action="{change_page.PAGE_NAME}">') if change.waiting else ""
    text = change_page.answer_status(row.get("Verdict", ""), change.slots)
    saved = f'<p>{html.escape(text)}</p>' if text else ""
    return f'''<section hidden data-type="changes" data-key="{html.escape(change.key)}" data-tab="{html.escape(change.tab)}" data-pending="{str(change.waiting).lower()}">
<h1>{html.escape(row.get('Change', ''))}</h1>
{change_page.change_details(row, shown)}
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
<h1>{html.escape(question.record.item["text"])}</h1>
{change_page.review_text_html(question.record.item.get("question", ""))}
<p>Asked by {html.escape(question.asker)}</p><p role="status" data-save-state></p>{form}</section>'''


def rebuild_list(machines: list[need.MachineNeeds], record: staff.StaffSnapshot) -> str:
    """Show the rebuild action before its reasons and session details."""
    def session(label: str, identity: str) -> str:
        member = record.by_id(staff.StaffId(identity))
        text = f"{label}: {member.name} ({member.kind})" if member is not None else f"Unknown session: {identity}"
        return '<p>' + html.escape(text) + '</p>'

    blocks: list[str] = []
    for machine in sorted(machines, key=lambda item: (
            item.newest["machine"] not in ("natedev", "mac"), item.newest["machine"] != "natedev", item.newest["machine"])):
        machine_name = machine.newest["machine"]
        name = "the Mac" if machine_name == "mac" else machine_name
        terminal = "Terminal" if machine_name == "mac" else "a terminal"
        items: list[str] = []
        details: list[str] = []
        for item in sorted(machine.records, key=lambda item: datetime.fromisoformat(item["created_at"]), reverse=True):
            requester = (session("Requested by", item["requested_by"])
                         if item["requested_by"] and item["requested_by"] != item["concerns"] else "")
            items.append(f'<li>{html.escape(item["reason"])}</li>')
            details.append('<li><p>Configuration commit: <code>' + html.escape(item["nixos_commit"]) + '</code></p>'
                           + session("Affected session", item["concerns"]) + requester + '</li>')
        blocks.append(f'<article data-machine="{html.escape(machine_name)}">'
            + f'<h2>Rebuild {html.escape(name)}</h2><p>Run <code>rebuild</code> in {terminal} on {html.escape(name)}.</p>'
            + ('<p>Mac updates can take five minutes while the Mac is reachable.</p>' if machine_name == "mac" else '')
            + ('<p>One rebuild applies all listed changes.</p>' if len(machine.records) >= 2 else '')
            + '<p>After the rebuild</p><ul>' + ''.join(items) + '</ul>'
            + '<details><summary>Details</summary><ol>' + ''.join(details) + '</ol></details></article>')
    return (f'<div hidden data-rebuilds data-count="{len(machines)}">'
            + (''.join(blocks) + '<p>Completed requests clear automatically. Refresh this page after the rebuild.</p>'
               if blocks else '<p>No rebuilds needed.</p>') + '</div>')


def render(browser: ReviewBrowser, status: change_page.DeliveryStatus) -> bytes:
    settings = conf.read("review_links")
    feedback_rows = settings.whole("feedback_rows")
    refresh_seconds = settings.number("page_refresh_seconds")
    prepared = prepare(browser, status)
    rebuilds = need.read_needs()
    menus: list[str] = []
    for kind, names in (("changes", prepared.queues), ("questions", prepared.question_tabs)):
        for name in names:
            count = (sum(item.tab == name and item.waiting for item in prepared.changes) if kind == "changes"
                     else sum(item.tab == name for item in prepared.questions))
            label = "Owner unavailable" if name == "unrouted" else name
            menus.append(f'<option data-type="{kind}" data-showrunner="{html.escape(name)}" '
                         + f'data-count="{count}" value="{html.escape(name)}">{html.escape(label)} ({count})</option>')
    type_tabs = '<nav class="review-types" role="tablist" aria-label="Review type">'
    for kind, label, count in (("questions", "Questions", len(prepared.questions)),
                               ("changes", "Changes", sum(item.waiting for item in prepared.changes)),
                               ("rebuilds", "Rebuilds", len(rebuilds))):
        type_tabs += (f'<button type="button" id="type-{kind}" role="tab" aria-controls="review-panel" '
                      + f'aria-selected="{str(kind == "changes").lower()}" data-review-type="{kind}">'
                      + f'{label} (<span data-type-count="{kind}">{count}</span>)</button>')
    type_tabs += '</nav>'
    content = ('''<style>[hidden]{display:none!important}
.review-types{display:flex;flex-wrap:nowrap;margin:0;gap:0}
.review-types button{flex:1;min-width:0;margin:0;padding:10px 6px;white-space:nowrap;font-size:clamp(13px,3.6vw,17px);background:transparent;color:light-dark(#595959,#b0b0b0);border:1px solid transparent;border-bottom:1px solid light-dark(#707070,#a0a0a0);border-top:3px solid transparent}
.review-types button[aria-selected=true]{color:CanvasText;background:Canvas;border-color:light-dark(#707070,#a0a0a0);border-bottom-color:Canvas;border-top-color:Highlight}
.review-types button:focus-visible{outline-offset:-5px}
.review-panel{border:1px solid light-dark(#707070,#a0a0a0);border-top:0;padding:16px;min-width:0;background:Canvas}
[data-showrunner-filter]{display:flex;align-items:center;gap:8px;flex-wrap:nowrap;margin-bottom:16px}
[data-showrunner-filter] label{white-space:nowrap}
[data-showrunner-filter] select{font:inherit;min-width:0;max-width:100%;flex:1;min-height:44px;color:CanvasText;background:Canvas}
.feedback-actions{display:flex;flex-wrap:nowrap;gap:8px}.feedback-actions button{margin-right:0;font-size:16px;white-space:nowrap}
section+section{border:0;margin:0;padding:0}
[data-rebuilds] article+article{border-top:1px solid GrayText;margin-top:16px;padding-top:16px}
[data-rebuilds] h2{margin-top:0}[data-rebuilds] li+li{margin-top:16px}
[data-rebuilds] details p{margin:0}[data-rebuilds] details p+p{margin-top:8px}
.navigation{padding:12px 0;background:Canvas}button{min-height:44px}
</style>''' + f'<div data-review-content data-refresh-seconds="{refresh_seconds}"><h1 data-review-heading>Changes to review</h1>'
               + type_tabs + '<div class="review-panel" id="review-panel" role="tabpanel" aria-labelledby="type-changes">'
               + '<div data-showrunner-filter><label for="review-showrunner">Showrunner:</label>'
               + '<select id="review-showrunner" aria-label="Showrunner"><option value="">All</option></select></div>'
               + '<template data-runner-options>' + ''.join(menus) + '</template>'
               + '<p hidden role="status" data-save-notice></p>'
               + ''.join(section(item, feedback_rows) for item in prepared.changes)
               + ''.join(question_section(item, browser, feedback_rows) for item in prepared.questions)
               + rebuild_list(rebuilds, prepared.record)
               + '<p hidden data-empty></p><div class="navigation"><button type="button" data-next hidden>Skip for now</button></div>'
               + '<noscript>Enable JavaScript to review items and use Skip for now.</noscript></div></div>'
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
let content=document.querySelector('[data-review-content]');
let rows,runnerOptions,types,rebuilds,rebuildCount,showrunners,runnerMenu,next,empty,heading,saveNotice;
let shownPage=content.outerHTML, heldPage=null, newestRequest=0, activeLoads=0, activeSaves=0, refreshTimer;
const remembered=new Map(), places=new Map(); let current=null, tab='', kind='changes', navigation=0, shownHash='';
function bindPage(){
 rows=[...content.querySelectorAll('section[data-key]')];
 runnerOptions=[...content.querySelector('[data-runner-options]').content.children];
 types=[...content.querySelectorAll('[data-review-type]')];
 rebuilds=content.querySelector('[data-rebuilds]');rebuildCount=Number(rebuilds.dataset.count);
 showrunners=content.querySelector('[data-showrunner-filter]');runnerMenu=showrunners.querySelector('select');
 next=content.querySelector('[data-next]');empty=content.querySelector('[data-empty]');
 heading=content.querySelector('[data-review-heading]');saveNotice=content.querySelector('[data-save-notice]');
 runnerMenu.addEventListener('change',()=>{tab=runnerMenu.value;go(remembered.get(kind+'/'+tab));});
 types.forEach(t=>t.addEventListener('click',()=>{navigation++;clearNotice();kind=t.dataset.reviewType;
  location.hash=places.get(kind) || new URLSearchParams({type:kind});show();}));
 content.querySelector('[role=tablist]').addEventListener('keydown',event=>{
  let index=types.indexOf(document.activeElement);
  if(event.key==='ArrowRight') index=(index+1)%types.length;
  else if(event.key==='ArrowLeft') index=(index+types.length-1)%types.length;
  else if(event.key==='Home') index=0;
  else if(event.key==='End') index=types.length-1;
  else return;
  event.preventDefault();types[index].click();types[index].focus();
 });
 next.addEventListener('click',()=>advance(current));
 rows.forEach(row=>{
  const form=row.querySelector('form');
  if(form) form.addEventListener('submit',event=>{event.preventDefault();save(row,form,event.submitter.value);});
 });
}
function waiting(){return rows.filter(r=>r.dataset.type===kind && (tab==='' || r.dataset.tab===tab) && r.dataset.pending==='true');}
function show(){
 const place=new URLSearchParams(location.hash.slice(1));
 const requested=place.has('row') ? place.get('row') : initial;
 kind=place.get('type') || rows.find(r=>r.dataset.key===requested)?.dataset.type || (rows.some(r=>r.dataset.type==='questions') ? 'questions' : 'changes');
 const options=runnerOptions.filter(option=>option.dataset.type===kind);
 const rebuildMode=kind==='rebuilds';
 heading.textContent={questions:'Questions to answer',changes:'Changes to review',rebuilds:'Rebuilds needed'}[kind];
 rebuilds.hidden=!rebuildMode;showrunners.hidden=rebuildMode;
 document.querySelector('.navigation').hidden=rebuildMode;
 types.forEach(t=>{t.setAttribute('aria-selected',String(t.dataset.reviewType===kind));t.tabIndex=t.dataset.reviewType===kind ? 0 : -1;
  t.querySelector('[data-type-count]').textContent=t.dataset.reviewType==='rebuilds' ? rebuildCount : rows.filter(r=>r.dataset.type===t.dataset.reviewType && r.dataset.pending==='true').length;});
 content.querySelector('[role=tabpanel]').setAttribute('aria-labelledby','type-'+kind);
 tab=place.get('showrunner') || '';
 if(tab!=='' && !options.some(option=>option.value===tab)) tab='';
 runnerMenu.replaceChildren(new Option('All ('+rows.filter(row=>row.dataset.type===kind && row.dataset.pending==='true').length+')',''),...options.map(option=>{
  const item=option.cloneNode(true);
  item.textContent=(item.value==='unrouted' ? 'Owner unavailable' : item.value)+' ('+rows.filter(row=>row.dataset.type===kind && row.dataset.tab===item.value && row.dataset.pending==='true').length+')';
  return item;
 }));runnerMenu.value=tab;
 current=rows.find(r=>r.dataset.key===requested && (tab==='' || r.dataset.tab===tab) && r.dataset.type===kind) || waiting()[0] || null;
 empty.hidden=rebuildMode || current!==null;
 const typeWaiting=rows.some(r=>r.dataset.type===kind && r.dataset.pending==='true');
 empty.textContent=kind==='questions' ? (typeWaiting ? 'No questions for this showrunner.' : 'No questions to answer.') :
  (typeWaiting ? 'No changes for this showrunner.' : 'No changes to review.');
 rows.forEach(r=>r.hidden=r!==current);
 next.hidden=rebuildMode || !waiting().some(r=>r!==current);
 if(current) remembered.set(kind+'/'+tab,current.dataset.key);
 const hash=new URLSearchParams(rebuildMode ? {type:kind} : {type:kind,showrunner:tab || '',row:current?.dataset.key || ''});
 places.set(kind,hash.toString()); history.replaceState(null,'','#'+hash); shownHash=location.hash; }
function clearNotice(){saveNotice.hidden=true;saveNotice.textContent='';}
function go(key){navigation++;clearNotice();location.hash=new URLSearchParams({type:kind,showrunner:tab,row:key || ''});show();}
function advance(row){
 const at=rows.indexOf(row), candidates=waiting();
 const following=candidates.find(r=>rows.indexOf(r)>at) || candidates[0];
 go(following?.dataset.key || ''); }
async function save(row,form,outcome){
 if(form.dataset.saving==='true') return;
 activeSaves++;let saved=false;
 clearNotice();form.dataset.saving='true'; const state=row.querySelector('[data-save-state]'), position=navigation;
 const data=new URLSearchParams(new FormData(form));
 data.set('row',row.dataset.key);data.set('outcome',outcome);
 form.querySelectorAll('button[name=outcome]').forEach(b=>b.setAttribute('aria-pressed',String(b.value===outcome)));
 state.textContent='Saving'; row.dataset.saveState='saving';
 try{
  const response=await fetch(form.action,{method:'POST',body:data,headers:{Accept:'application/json'}});
  const reply=await response.json();
  if(!response.ok) throw new Error(reply.reason || 'The answer could not be saved. Try again.');
  state.textContent=reply.answer ? 'Saved: '+reply.answer : outcome==='question' ? 'The showrunner will answer this' : outcome==='feedback' ? 'The showrunner will act on this' : 'The showrunner will validate this';
  row.dataset.saveState='saved';
  saved=true;
  row.dataset.pending='false';form.hidden=true;
  if(current===row && (tab==='' || tab===row.dataset.tab) && navigation===position){
   advance(row);saveNotice.textContent=row.dataset.type==='questions' ? 'Answer saved.' : 'Review saved.';saveNotice.hidden=false;
  }
  else show();
 }catch(error){
  row.dataset.saveState='failed'; state.textContent='Not saved: '+(error instanceof SyntaxError ? 'The answer could not be saved. Try again.' : error.message);
 }finally{
  form.dataset.saving='false';activeSaves--;
  // Read after the save, so an older response cannot restore the saved item.
  if(saved){heldPage=null;newestRequest++;refreshPage(true);}
  else applyHeldPage();
 }
}
function detailKey(detail){
 const owner=detail.closest('[data-key],[data-machine]');
 return (owner?.dataset.key || 'rebuild/'+owner?.dataset.machine)+'/'+[...owner.querySelectorAll('details')].indexOf(detail);
}
function applyHeldPage(){
 if(!heldPage || activeSaves || [...content.querySelectorAll('textarea,input[type=text]')].some(field=>field.value!=='' && !field.closest('form')?.hidden)) return;
 const fresh=heldPage;heldPage=null;
 if(fresh.outerHTML===shownPage) return;
 const opened=new Set([...content.querySelectorAll('details[open]')].map(detailKey));
 const left=scrollX,top=scrollY,focused=document.activeElement;
 const controlId=focused===runnerMenu || types.includes(focused) ? focused.id : '';
 const fieldRow=focused?.closest('[data-key]')?.dataset.key;
 const fieldName=focused?.matches('textarea,input[type=text]') ? focused.name : '';
 const selection=[focused?.selectionStart,focused?.selectionEnd,focused?.selectionDirection];
 const previous=rows.indexOf(current),following=rows.slice(previous+1).map(row=>row.dataset.key);
 const candidates=[...fresh.querySelectorAll('section[data-key]')].filter(row=>row.dataset.type===kind && (tab==='' || row.dataset.tab===tab) && row.dataset.pending==='true');
 const keep=candidates.find(row=>row.dataset.key===current?.dataset.key);
 const chosen=keep || following.map(key=>candidates.find(row=>row.dataset.key===key)).find(Boolean) || candidates[0];
 const notice=saveNotice.hidden ? '' : saveNotice.textContent;
 shownPage=fresh.outerHTML;content.replaceWith(fresh);content=fresh;bindPage();
 content.querySelectorAll('details').forEach(detail=>detail.open=opened.has(detailKey(detail)));
 history.replaceState(null,'','#'+new URLSearchParams(kind==='rebuilds' ? {type:kind} : {type:kind,showrunner:tab || '',row:chosen?.dataset.key || ''}));
 show();
 if(notice){saveNotice.textContent=notice;saveNotice.hidden=false;}
 if(controlId) content.querySelector('#'+controlId)?.focus({preventScroll:true});
 const field=rows.find(row=>row.dataset.key===fieldRow)?.querySelector('[name="'+fieldName+'"]');
 if(field && !field.closest('section').hidden){field.focus({preventScroll:true});field.setSelectionRange(...selection);}
 window.scrollTo({left,top,behavior:'instant'});scheduleRefresh();
}
async function refreshPage(supersede=false){
 if(document.visibilityState!=='visible' || (activeLoads && !supersede)) return;
 const request=++newestRequest;
 activeLoads++;
 try{
  const response=await fetch(location.href,{cache:'no-store'});
  if(!response.ok) return;
  const fresh=new DOMParser().parseFromString(await response.text(),'text/html').querySelector('[data-review-content]');
  if(request!==newestRequest || !fresh) return;
  heldPage=fresh;applyHeldPage();
 }catch(error){}
 finally{activeLoads--;}
}
function scheduleRefresh(){
 clearInterval(refreshTimer);
 if(document.visibilityState==='visible') refreshTimer=setInterval(refreshPage,Number(content.dataset.refreshSeconds)*1000);
}
document.addEventListener('input',()=>applyHeldPage());
document.addEventListener('visibilitychange',()=>{scheduleRefresh();if(document.visibilityState==='visible') refreshPage(true);});
window.addEventListener('hashchange',()=>{if(location.hash!==shownHash){navigation++;clearNotice();}show();});
bindPage();show();scheduleRefresh();
'''
