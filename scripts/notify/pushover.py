#!/usr/bin/env python3
"""Send the user one Pushover alert. Mac pushes and cancellations run on natedev.

  pushover.py --text-id ID --value KEY=VALUE ... [--due ISO8601] [--source NAME] [--key KEY]
  pushover.py cancel [--source NAME] KEY     stop the emergency push sent with --key KEY

Priority 0 is a normal notification, 1 high (sounds in Pushover's quiet hours),
2 emergency: it repeats every RETRY_S seconds until the user taps Acknowledge,
for at most EXPIRE_S. Deleting it on the phone does not stop it.
A decision uses priority 1 only before an explicit deadline within the configured decision window.
Other decisions use priority 0. Release checks the deadline again.
An emergency push with --key carries the Pushover tag push_tag(KEY).
`cancel` stops every emergency push with that tag.
It prints "pushover: cancelled N" or "pushover: nothing to cancel" and exits 0;
it exits 1 when Pushover refused it or could not be reached, 2 on usage or keys missing.

The keys are in KEYS_FILE (PUSHOVER_USER, PUSHOVER_TOKEN; 0600, filled in by the
user). Nothing here prints them. (user, 2026-10-05) Each LOG_FILE JSON line
holds the local time, source, priority, posted title and message, and outcome.

Every posted push ends with one line after review: "Sent by: <sender> · Template: <text id>",
or "Sent by: <sender> · Template: none (free text)" for a legacy kept push. The
sender is the source; "send.py in hana" shows as "hana". A long body is cut so that last line
always fits. A push with no sender is refused and nothing is posted.

While the user's silence is on (silence.py), a push below priority 2 is kept,
not posted: it prints "pushover: kept: silence until ..." and exits 0, and
`silence.py off` posts it later. A kept push with the same --key from the same
--source replaces the older kept one.

New pushes require a registered template before review, credentials, claims, or posting. Exit 0
sent or kept, 1 Pushover refused it or could not be reached, 2 refused before sending,
usage, or keys missing; 4 an existing claim is busy, 5 replaced, 6 not sent after failed review. Registry and emergency pushes still send.
"""

from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import hashlib
import json
import os
import shlex
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import NotRequired, TypedDict, cast

from scripts.message import session_send
from scripts.notify import review
from scripts.notify import push_texts
from scripts.notify import push_reference
from scripts.notify import silence
from scripts.notify import decision_priority
from scripts.notify.user_action import (FIX, REFUSED_BEFORE_SENDING, ActionAllowed, ActionRefused, ActionRequired,
                         ActionUnstated, UserAction, first_line, parse_user_action, refused_at,
                         NotificationSession, NO_SESSION_LINE, split_session, session_prefix)

API_URL = "https://api.pushover.net/1/messages.json"
CANCEL_URL = "https://api.pushover.net/1/receipts/cancel_by_tag/{tag}.json"
KEYS_FILE = Path.home() / ".config" / "pushover" / "env"
LOG_FILE = Path.home() / ".local" / "state" / "notify" / "pushover.jsonl"
PRIORITIES = ("0", "1", "2")
RETRY_S = 300
EXPIRE_S = 10800
TITLE_MAX = 250
MESSAGE_MAX = 1024
ACTION_TEXT_MAX = MESSAGE_MAX - len("Action: ") - 1
TIMEOUT_S = 15
SOURCE_MAX = 80
# send.py's default_sender: the Claude session that ran send.py follows this prefix.
SESSION_SOURCE = "send.py in "
FREE_TEXT = "none (free text)"
USAGE = "usage: pushover.py --text-id ID --value KEY=VALUE ... [--due ISO8601] [--source NAME] [--key KEY]"
CANCEL_USAGE = "usage: pushover.py cancel [--source NAME] KEY"
REMOTE = '"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/notify/pushover.py"'


class Keys(TypedDict):
    user: str
    token: str


class Reply(TypedDict, total=False):
    status: int
    request: str
    receipt: str
    canceled: int
    errors: list[str]


class ReviewedPushRecord(review.MessageRecord):
    reviewed: NotRequired[bool]
    held: NotRequired[bool]
    text_id: NotRequired[str]
    failed_rules: NotRequired[list[str]]
    findings: NotRequired[list[review.RuleFailureRecord]]
    sent_despite_failed_review: NotRequired[bool]
    held_source: NotRequired[str]
    held_identity: NotRequired[str]


class PushLogRecord(TypedDict):
    key: NotRequired[str]
    template: NotRequired[str]
    reference: NotRequired[push_reference.PushReference]
    time: str
    source: str
    priority: str
    title: str
    message: str
    outcome: str


Post = Callable[[str, bytes], tuple[int, bytes]]
AskNatedev = Callable[[Sequence[str], str], subprocess.CompletedProcess[str]]


def read_keys(path: Path) -> Keys | None:
    """Both keys from a KEY=VALUE file, or None when either is missing or empty."""
    if not path.is_file():
        return None
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        name, sep, value = line.partition("=")
        if sep and not name.lstrip().startswith("#"):
            values[name.strip()] = value.strip().strip("\"'")
    user, token = values.get("PUSHOVER_USER", ""), values.get("PUSHOVER_TOKEN", "")
    return {"user": user, "token": token} if user and token else None


def push_tag(key: str) -> str:
    """The Pushover tag for an emergency push with this key: the same key gives the same tag."""
    return "key-" + hashlib.sha256(key.encode()).hexdigest()[:16]


def fields(keys: Keys, title: str, message: str, priority: str, key: str = "") -> dict[str, str]:
    body = {
        "token": keys["token"],
        "user": keys["user"],
        "title": title[:TITLE_MAX],
        "message": message[:MESSAGE_MAX],
        "priority": priority,
    }
    if priority == "2":
        body |= {"retry": str(RETRY_S), "expire": str(EXPIRE_S)}
        if key:
            body["tags"] = push_tag(key)
    return body


def http_post(url: str, data: bytes) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:  # pyright: ignore[reportAny]
            return cast(int, response.status), cast(bytes, response.read())  # pyright: ignore[reportAny]
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def parent_source() -> str:
    """The parent command line, bounded for the audit log."""
    try:
        found = subprocess.run(
            ["ps", "-o", "args=", "-p", str(os.getppid())],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
    except OSError:
        found = ""
    return (found or f"parent pid {os.getppid()}")[:SOURCE_MAX]


def sender_name(source: str) -> str:
    """The "Sent by" name: the session for a send.py source, else the source, on one line."""
    return " ".join(source.removeprefix(SESSION_SOURCE).split())[:SOURCE_MAX]


def log(source: str, priority: str, title: str, message: str, outcome: str, *,
        key: str = "", template: str = "", reference: push_reference.PushReference = push_reference.NO_REFERENCE) -> None:
    entry: PushLogRecord = {"time": datetime.now().astimezone().replace(microsecond=0).isoformat(),
             "source": source, "priority": priority, "title": title, "message": message, "outcome": outcome}
    if key or template or reference["kind"] != "none":
        entry["key"] = key
        entry["template"] = template
        entry["reference"] = reference
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as out:
            _ = out.write(json.dumps(entry) + "\n")
    except OSError as error:
        print(f"pushover: could not write log: {error}; record: {json.dumps(entry)}", file=sys.stderr)


class Arguments(TypedDict):
    reference: push_reference.PushReference
    need: str
    priority: str
    source: str | None
    key: str
    action: str | None
    no_action: bool
    answerable: bool
    title: str
    message: str
    text_id: str
    session: NotificationSession
    header: bool
    due: str


def parse(argv: list[str]) -> Arguments | None:
    """Parse options without exiting, so callers get send()'s usage status."""
    priority = "0"
    question = forwarded_reference = ""
    remote_forward = False
    due = ""
    source: str | None = None
    action: str | None = None
    no_action = False
    answerable = False
    text_id = ""
    push_key = ""
    values: dict[str, str] = {}
    index = 0
    while index < len(argv) and argv[index].startswith("--"):
        option = argv[index]
        if option == "--ssh-forwarded-reference":
            if "SSH_CONNECTION" not in os.environ:
                return None
            remote_forward = True
            index += 1
            continue
        if option == "--answerable":
            answerable = True
            index += 1
            continue
        if option == "--no-action":
            no_action = True
            index += 1
            continue
        if option not in ("--priority", "--source", "--key", "--action", "--text-id", "--value", "--due", "--question", "--push-reference") or index + 1 >= len(argv):
            return None
        value = argv[index + 1]
        if option == "--priority":
            if value not in PRIORITIES:
                return None
            priority = value
        elif option == "--question":
            question = value
        elif option == "--push-reference":
            forwarded_reference = value
        elif option == "--due":
            due = value
        elif option == "--source":
            if not value.strip():
                return None
            source = value
        elif option == "--key":
            push_key = value
        elif option == "--text-id":
            text_id = value
        elif option == "--value":
            key, separator, item = value.partition("=")
            if not separator or key in values:
                return None
            values[key] = item
        else:
            action = value
        index += 2
    try:
        _ = decision_priority.read_due(due)
    except ValueError:
        return None
    try:
        reference = push_reference.admission_reference(text_id, question, forwarded_reference, remote_forward=remote_forward)
    except (OSError, ValueError) as error:
        print(f"pushover: {error}", file=sys.stderr)
        return None
    if not push_key and reference["kind"] == "question":
        push_key = "question:" + reference["path"]
    session: NotificationSession = NO_SESSION_LINE
    header = True
    need = "blocked" if priority == "2" else "decision" if priority == "1" or due else "note"
    positionals = argv[index:]
    if text_id:
        try:
            rendered = push_texts.render(text_id, **values)
        except ValueError:
            return None
        if positionals or action is not None or no_action:
            return None
        source = source or rendered.sender
        need = "blocked" if priority == "2" else rendered.need
        if due and need != "decision":
            return None
        action = rendered.action.text if isinstance(rendered.action, ActionRequired) else None
        no_action = action is None
        positionals = [rendered.title, rendered.body]
        session = rendered.session
        header = rendered.header
    elif values:
        return None
    try:
        priority = decision_priority.priority(need, due)
    except ValueError:
        return None
    if len(positionals) != 2 or not positionals[0].strip() or (not text_id and not positionals[1].strip()):
        return None
    if not text_id:
        session, positionals[1] = split_session(positionals[1])
    return {
        "reference": reference,
        "need": need,
        "due": due,
        "session": session,
        "priority": priority,
        "source": source,
        "key": push_key,
        "action": action,
        "no_action": no_action,
        "answerable": answerable,
        "title": positionals[0],
        "message": positionals[1],
        "text_id": text_id,
        "header": header,
    }


def trimmed_body(action: UserAction, message: str, session: NotificationSession = NO_SESSION_LINE,
                 *, header: bool = True) -> str:
    body_limit = max(MESSAGE_MAX - len(first_line(action, session)) - 1, 0) if header else MESSAGE_MAX
    return message[:body_limit]


def posted_message(action: UserAction, message: str, session: NotificationSession = NO_SESSION_LINE,
                   *, header: bool = True) -> str:
    body = trimmed_body(action, message, session, header=header)
    return f"{first_line(action, session)}\n{body}" if header else body


def refuse(source: str, priority: str, title: str, message: str, reason: str) -> int:
    outcome = f"{REFUSED_BEFORE_SENDING}: {reason}"
    log(source, priority, title, message, outcome)
    print(f"pushover: {outcome}. {FIX}", file=sys.stderr)
    return 2


def send(argv: list[str], keys_file: Path = KEYS_FILE, post: Post = http_post) -> int:
    if "--text-id" not in argv:
        print("pushover: " + push_texts.TEMPLATE_REQUIRED, file=sys.stderr)
        return 2
    arguments = parse(argv)
    if arguments is None:
        print(USAGE, file=sys.stderr)
        return 2
    if not arguments["text_id"]:
        print("pushover: " + push_texts.TEMPLATE_REQUIRED, file=sys.stderr)
        return 2
    priority = arguments["priority"]
    source = arguments["source"] if arguments["source"] is not None else parent_source()
    title = arguments["title"][:TITLE_MAX]
    message = arguments["message"][:MESSAGE_MAX]
    action = parse_user_action(arguments["action"], arguments["no_action"])
    if isinstance(action, ActionRefused):
        return refuse(source, priority, title, message, action.reason)
    if isinstance(action, ActionUnstated):
        return refuse(source, priority, title, message, "no action stated")
    action_limit = max(ACTION_TEXT_MAX - len(session_prefix(arguments["session"])), 0)
    if isinstance(action, ActionRequired) and len(action.text) > action_limit:
        return refuse(source, priority, title, message,
                      f"--action may hold at most {action_limit} characters")
    if len(first_line(action, arguments["session"])) + 1 > MESSAGE_MAX:
        return refuse(source, priority, title, message, "Session and action exceed the message limit")
    action_priority = {"blocked": 2, "decision": 1, "note": 0}[arguments["need"]]
    match refused_at(action, action_priority):
        case ActionRefused(reason):
            return refuse(source, priority, title, message, reason)
        case ActionAllowed():
            pass
    draft = review.Draft(source, priority, title, action,
                         trimmed_body(action, arguments["message"], arguments["session"], header=arguments["header"]),
                         arguments["answerable"], arguments["session"], arguments["due"], arguments["reference"])
    header, key = arguments["header"], arguments["key"]
    registered_text = bool(arguments["text_id"]) and not push_texts.agent_written(arguments["text_id"])
    claimed = False
    try:
        found = review.NoHold() if registered_text else review.held_for(source)
        if isinstance(found, review.LiveHold):
            claimed = review.claim(source)
            if not claimed:
                print("pushover: held for review: this source's push is being sent", file=sys.stderr)
                return 4
            found = review.held_for(source)
    except OSError as error:
        found = review.UnreadableHold(review.held_path(source), str(error))
    detail = ""
    approved = push_texts.approval_date(arguments["text_id"]) if registered_text else ""
    if approved:
        # The text was reviewed and approved ahead of time: no live review, no reviewer wait.
        detail = f"registered text, approved {approved}"
        outcome: review.ReviewOutcome = review.Unreviewed(detail)
        version = draft
    elif review.switched_off():
        outcome = review.Unreviewed("review off")
        version = draft
        detail = "review: off"
    else:
        outcome = (review.review(draft, revision=claimed, text_id=arguments["text_id"]) if registered_text
                   else review.review(draft, revision=claimed))
        # For registered texts that are not agent-written, review is a logged check only.
        version = draft if registered_text else review.best_version(draft, outcome)
        if isinstance(outcome, review.Unreviewed):
            detail = "unreviewed: " + outcome.why
        elif registered_text and isinstance(outcome, review.Rewritten):
            detail = "registered text sent as rendered, rewrite not applied"
    if isinstance(outcome, review.Questions):
        outcome = review.Failed(outcome.best_version, outcome.failed_rules, outcome.metadata, outcome.failures)
    rejected = isinstance(outcome, (review.Failed, review.Withheld))
    template_required = not registered_text and isinstance(outcome, (review.Failed, review.Withheld)) and any(rule.startswith("use-registered-template:") for rule in outcome.failed_rules)
    send_anyway = rejected and not template_required and (priority == "2" or registered_text)
    if send_anyway:
        detail = "sent despite failed review: " + ", ".join(outcome.failed_rules if isinstance(outcome, (review.Failed, review.Withheld)) else [])
    record: ReviewedPushRecord = {**review.message_record(version), "reviewed": not isinstance(outcome, review.Unreviewed)}
    if arguments["text_id"]:
        record["text_id"] = arguments["text_id"]
    if rejected:
        assert isinstance(outcome, (review.Failed, review.Withheld))
        record["failed_rules"] = outcome.failed_rules
        record["findings"] = [{"rule": failure.rule, "quote": failure.quote} for failure in outcome.failures]
        record["held"] = not send_anyway
        record["sent_despite_failed_review"] = send_anyway
    if claimed and isinstance(found, review.LiveHold):
        record["held_source"] = source
        record["held_identity"] = review.hold_identity(found.hold)
    if rejected and not send_anyway and claimed and isinstance(found, review.LiveHold):
        review.retire_claim(found.hold)
    print("pushover: reviewed JSON: " + json.dumps(record), flush=True)
    if rejected and not send_anyway:
        review.log_review(draft, outcome, detail="not sent, failed review", text_id=arguments["text_id"])
        assert isinstance(outcome, (review.Failed, review.Withheld))
        why = outcome.why if isinstance(outcome, review.Withheld) else ", ".join(outcome.failed_rules)
        print("pushover: not sent: " + why, file=sys.stderr)
        return 6
    if claimed:
        if not isinstance(found, review.LiveHold):
            completion: review.ClaimDelivery = review.ClaimCancelled()
        else:
            completion = review.post_claim(found.hold, lambda: deliver(version, keys_file, post, detail=detail, header=header,
                                                                             key=key, text_id=arguments["text_id"]))
        if isinstance(completion, review.ClaimCancelled):
            review.log_review(draft, outcome, detail="claim cancelled before posting", text_id=arguments["text_id"])
            print("pushover: replaced: a newer version replaced this push; nothing was sent")
            return 5
        code = completion.code if isinstance(completion, review.ClaimFailed) else 0
    else:
        code = deliver(version, keys_file, post, detail=detail, header=header, key=key, text_id=arguments["text_id"])
    delivery = review.Sent(version) if code == 0 else review.NotSent()
    review.log_review(draft, outcome, delivery, detail=detail, text_id=arguments["text_id"])
    if code == 0 and send_anyway:
        print("pushover: " + detail)
        if registered_text:
            request_template_fix(record)
    if code == 0 and not registered_text and isinstance(outcome, (review.Rewritten, review.Questions)):
        print("pushover: sent as rewritten:")
        print(version.title)
        print(first_line(version.action, version.session))
        print(version.body)
    return code


def request_template_fix(record: ReviewedPushRecord) -> None:
    """Direct timer callers use the sender's daily repair-notice handler too."""
    try:
        _ = subprocess.Popen(session_send.send_argv("template-fix", json.dumps(record)),
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError as error:
        print(f"pushover: template fix notice failed: {error}", file=sys.stderr)
        log("template fix", "0", record["title"], record["body"], f"notice failed: {error}")


def ask_natedev(argv: Sequence[str], text: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(argv), input=text, capture_output=True, text=True, check=False)


def via_natedev(arguments: Sequence[str], text: str, asker: AskNatedev, *,
                source: str, priority: str, title: str, message: str) -> int:
    """Return natedev's output and status. Log a failed connection without a local post."""
    try:
        reply = asker([*silence.SSH, f"{REMOTE} {shlex.join(arguments)}"], text)
    except (OSError, TimeoutError) as error:
        cause = str(error)
    else:
        if reply.returncode != 255 and (reply.stdout or reply.stderr):
            print(reply.stdout, end="")
            print(reply.stderr, end="", file=sys.stderr)
            return reply.returncode
        cause = reply.stderr.strip() or f"exit {reply.returncode}: no answer"
    log(source, priority, title, message, f"unreachable: natedev: {cause}")
    print(f"pushover: could not reach natedev: {cause}", file=sys.stderr)
    return 1


def deliver(draft: review.Draft, keys_file: Path = KEYS_FILE,
            post: Post = http_post, *, detail: str = "", header: bool = True, key: str = "", text_id: str = "",
            asker: AskNatedev = ask_natedev) -> int:
    """Post an already reviewed version, including expiry sends; do not review again.

    During silence a non-empty `key` makes the kept push replace an older kept one from the same source.
    """
    source, priority, title = draft.source, draft.priority, draft.title
    if not key and draft.reference["kind"] == "question":
        key = "question:" + draft.reference["path"]
    need = "blocked" if priority == "2" else "decision" if draft.due or priority == "1" else "note"
    priority = decision_priority.priority(need, draft.due)
    message = posted_message(draft.action, draft.body, draft.session, header=header)
    push: silence.KeptPush = {"source": source, "priority": priority, "title": title,
                              "message": message, "detail": detail, "key": key, "text_id": text_id,
                              "due": draft.due, "need": need, "reference": draft.reference}
    if silence.on_mac():
        return via_natedev(["deliver"], json.dumps(push), asker,
                           source=source, priority=priority, title=title, message=message)
    return deliver_here(push, keys_file, post)


def deliver_here(push: silence.KeptPush, keys_file: Path = KEYS_FILE, post: Post = http_post) -> int:
    """Keep a reviewed push during silence or post it on natedev."""
    source, priority, title, message = push["source"], push["priority"], push["title"], push["message"]
    detail = push["detail"]
    if priority != "2":
        kept = silence.keep_here(push, deliver=lambda push: post_kept(push, keys_file, post))
        if isinstance(kept, silence.Kept):
            log(source, priority, title, message, f"kept: silence {kept.until}" + (f", {detail}" if detail else ""))
            print(f"pushover: kept: silence {kept.until}")
            return 0
    return post_message(source, priority, title, message, keys_file, post, detail, push["key"], push["text_id"])


def post_kept(push: silence.KeptPush, keys_file: Path = KEYS_FILE, post: Post = http_post) -> int:
    """Drop settled requests and retired alerts before posting kept pushes."""
    reference = push.get("reference", push_reference.NO_REFERENCE)
    reason = "retired template message.repeat_flood" if push["text_id"] == "message.repeat_flood" else ""
    if reference["kind"] == "question":
        try:
            opening = push_reference.read_question(Path(reference["path"]))
        except (OSError, ValueError) as error:
            log(push["source"], push["priority"], push["title"], push["message"],
                f"anomaly: question record {reference['path']}: {error}",
                key=push["key"], template=push["text_id"], reference=reference)
        else:
            if opening.status == "closed":
                reason = "question closed"
            elif opening.since != reference["since"]:
                reason = "question reopened"
    elif reference["kind"] == "review_watch":
        from scripts.production import review_regime
        if review_regime.acknowledged():
            reason = "report acknowledged"
    if reason:
        log(push["source"], push["priority"], push["title"], push["message"], "dropped: " + reason,
            key=push["key"], template=push["text_id"], reference=reference)
        return push_reference.KeptPushDropped()
    detail = ", ".join(part for part in ("kept during silence", push["detail"]) if part)
    need = push.get("need") or {"0": "note", "1": "decision", "2": "blocked"}[push["priority"]]
    priority = decision_priority.priority(need, push.get("due", ""))
    return post_message(push["source"], priority, push["title"], push["message"], keys_file, post, detail,
                        push["key"], push["text_id"])


def post_message(source: str, priority: str, title: str, message: str,
                 keys_file: Path, post: Post, detail: str, key: str = "", text_id: str = "") -> int:
    """Every push to the phone goes out here, with its sender and template on one last line."""
    suffix = f", {detail}" if detail else ""
    sender = sender_name(source)
    if not sender:
        log(source, priority, title, message, f"{REFUSED_BEFORE_SENDING}: no sender" + suffix)
        print(f"pushover: {REFUSED_BEFORE_SENDING}: no sender. Name the sender with --source NAME.", file=sys.stderr)
        return 2
    footer = f"\n\nSent by: {sender} · Template: {text_id or FREE_TEXT}"
    message = message[:MESSAGE_MAX - len(footer)].rstrip() + footer
    keys = read_keys(keys_file)
    if keys is None:
        log(source, priority, title, message, "keys missing" + suffix)
        print(f"pushover: PUSHOVER_USER and PUSHOVER_TOKEN must both be set in {keys_file}", file=sys.stderr)
        return 2
    data = urllib.parse.urlencode(fields(keys, title, message, priority, key)).encode()
    try:
        status, raw = post(API_URL, data)
    except (urllib.error.URLError, TimeoutError) as error:
        log(source, priority, title, message, f"unreachable: {error}" + suffix)
        print(f"pushover: could not reach Pushover: {error}", file=sys.stderr)
        return 1
    try:
        decoded = cast(object, json.loads(raw))
    except json.JSONDecodeError:
        decoded = None
    reply = cast(Reply, cast(object, decoded)) if isinstance(decoded, dict) else Reply()
    if status == 200 and reply.get("status") == 1:
        receipt = reply.get("receipt")
        outcome = f"sent, receipt {receipt}" if receipt else "sent"
        log(source, priority, title, message, outcome + suffix)
        print(f"pushover: {outcome}")
        return 0
    errors = "; ".join(reply.get("errors", [])) or f"HTTP {status}"
    log(source, priority, title, message, f"refused: {errors}" + suffix)
    print(f"pushover: refused: {errors}", file=sys.stderr)
    return 1


def cancel(argv: list[str], keys_file: Path = KEYS_FILE, post: Post = http_post, *,
           asker: AskNatedev = ask_natedev) -> int:
    """Stop every emergency push sent with this key; log the outcome like a post."""
    source = parent_source()
    if len(argv) == 3 and argv[0] == "--source" and argv[1].strip():
        source, argv = argv[1], argv[2:]
    if len(argv) != 1 or not argv[0] or argv[0].startswith("--"):
        print(CANCEL_USAGE, file=sys.stderr)
        return 2
    key = argv[0]
    tag = push_tag(key)
    title, message = "cancel emergency push", f"key {key}, tag {tag}"
    if silence.on_mac():
        return via_natedev(["cancel", "--source", source, key], "", asker,
                           source=source, priority="2", title=title, message=message)
    keys = read_keys(keys_file)
    if keys is None:
        log(source, "2", title, message, "cancel: keys missing")
        print(f"pushover: PUSHOVER_USER and PUSHOVER_TOKEN must both be set in {keys_file}", file=sys.stderr)
        return 2
    try:
        status, raw = post(CANCEL_URL.format(tag=tag), urllib.parse.urlencode({"token": keys["token"]}).encode())
    except (urllib.error.URLError, TimeoutError) as error:
        log(source, "2", title, message, f"cancel: unreachable: {error}")
        print(f"pushover: could not reach Pushover: {error}", file=sys.stderr)
        return 1
    try:
        decoded = cast(object, json.loads(raw))
    except json.JSONDecodeError:
        decoded = None
    reply = cast(Reply, cast(object, decoded)) if isinstance(decoded, dict) else Reply()
    if status == 200 and reply.get("status") == 1:
        count = reply.get("canceled")
        outcome = "nothing to cancel" if count == 0 else f"cancelled {count}" if isinstance(count, int) else "cancelled"
        log(source, "2", title, message, "cancel: " + outcome)
        print(f"pushover: {outcome}")
        return 0
    errors = "; ".join(reply.get("errors", [])) or f"HTTP {status}"
    log(source, "2", title, message, f"cancel: refused: {errors}")
    print(f"pushover: refused: {errors}", file=sys.stderr)
    return 1


def main(argv: list[str], keys_file: Path = KEYS_FILE, post: Post = http_post) -> int:
    if argv == ["deliver"]:
        push = silence.parse_push(sys.stdin.read())
        if push is None:
            print("pushover: deliver reads one push as JSON on stdin", file=sys.stderr)
            return 2
        return deliver_here(push, keys_file, post)
    return cancel(argv[1:], keys_file, post) if argv[:1] == ["cancel"] else send(argv, keys_file, post)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
