#!/usr/bin/env python3
"""Send the user one Pushover alert: the one way any session reaches their phone.

  pushover.py [--priority 0|1|2] [--source NAME] [--key KEY] (--action TEXT | --no-action) TITLE MESSAGE

Priority 0 is a normal notification, 1 high (sounds in Pushover's quiet hours),
2 emergency: it repeats every RETRY_S seconds until the user taps Acknowledge,
for at most EXPIRE_S. Which events get which priority is each caller's policy.

The keys are in KEYS_FILE (PUSHOVER_USER, PUSHOVER_TOKEN; 0600, filled in by the
user). Nothing here prints them. (user, 2026-10-05) Each LOG_FILE JSON line
holds the local time, source, priority, posted title and message, and outcome.

While the user's silence is on (silence.py), a push below priority 2 is kept,
not posted: it prints "pushover: kept: silence until ..." and exits 0, and
`silence.py off` posts it later. A kept push with the same --key from the same
--source replaces the older kept one.

Messages without --action or --no-action are refused before sending. Exit 0
sent or kept, 1 Pushover refused it or could not be reached, 2 refused before sending,
usage, or keys missing; 4 an existing claim is busy, 5 replaced, 6 not sent after failed review. Registry and emergency pushes still send.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import NotRequired, TypedDict, cast

import review
import push_texts
import silence
from user_action import (FIX, REFUSED_BEFORE_SENDING, ActionAllowed, ActionRefused, ActionRequired,
                         ActionUnstated, UserAction, first_line, parse_user_action, refused_at,
                         NotificationSession, NO_SESSION_LINE, split_session, session_prefix)

API_URL = "https://api.pushover.net/1/messages.json"
KEYS_FILE = Path.home() / ".config" / "pushover" / "env"
LOG_FILE = Path.home() / ".local" / "state" / "notify" / "pushover.jsonl"
PRIORITIES = ("0", "1", "2")
RETRY_S = 300
EXPIRE_S = 10800
TITLE_MAX = 250
MESSAGE_MAX = 1024
ACTION_TEXT_MAX = MESSAGE_MAX - len("Action: ") - 1
TIMEOUT_S = 15
USAGE = "usage: pushover.py [--priority 0|1|2] [--source NAME] [--key KEY] [--answerable] (--action TEXT | --no-action) TITLE MESSAGE"


class Keys(TypedDict):
    user: str
    token: str


class Reply(TypedDict, total=False):
    status: int
    request: str
    receipt: str
    errors: list[str]


class ReviewedPushRecord(review.MessageRecord):
    held: NotRequired[bool]
    text_id: NotRequired[str]
    failed_rules: NotRequired[list[str]]
    findings: NotRequired[list[review.RuleFailureRecord]]
    sent_despite_failed_review: NotRequired[bool]
    held_source: NotRequired[str]
    held_identity: NotRequired[str]


class PushLogRecord(TypedDict):
    time: str
    source: str
    priority: str
    title: str
    message: str
    outcome: str


Post = Callable[[str, bytes], tuple[int, bytes]]


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


def fields(keys: Keys, title: str, message: str, priority: str) -> dict[str, str]:
    body = {
        "token": keys["token"],
        "user": keys["user"],
        "title": title[:TITLE_MAX],
        "message": message[:MESSAGE_MAX],
        "priority": priority,
    }
    if priority == "2":
        body |= {"retry": str(RETRY_S), "expire": str(EXPIRE_S)}
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
    return (found or f"parent pid {os.getppid()}")[:80]


def log(source: str, priority: str, title: str, message: str, outcome: str) -> None:
    entry: PushLogRecord = {"time": datetime.now().astimezone().replace(microsecond=0).isoformat(),
             "source": source, "priority": priority, "title": title, "message": message, "outcome": outcome}
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as out:
            _ = out.write(json.dumps(entry) + "\n")
    except OSError as error:
        print(f"pushover: could not write log: {error}", file=sys.stderr)


class Arguments(TypedDict):
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


def parse(argv: list[str]) -> Arguments | None:
    """Parse options without exiting, so callers get send()'s usage status."""
    priority = "0"
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
        if option == "--answerable":
            answerable = True
            index += 1
            continue
        if option == "--no-action":
            no_action = True
            index += 1
            continue
        if option not in ("--priority", "--source", "--key", "--action", "--text-id", "--value") or index + 1 >= len(argv):
            return None
        value = argv[index + 1]
        if option == "--priority":
            if value not in PRIORITIES:
                return None
            priority = value
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
    session: NotificationSession = NO_SESSION_LINE
    header = True
    positionals = argv[index:]
    if text_id:
        try:
            rendered = push_texts.render(text_id, **values)
        except ValueError:
            return None
        if positionals or action is not None or no_action:
            return None
        source = source or rendered.sender
        priority = "2" if priority == "2" else {"note": "0", "decision": "1", "blocked": "2"}[rendered.need]
        action = rendered.action.text if isinstance(rendered.action, ActionRequired) else None
        no_action = action is None
        positionals = [rendered.title, rendered.body]
        session = rendered.session
        header = rendered.header
    elif values:
        return None
    if len(positionals) != 2 or not all(value.strip() for value in positionals):
        return None
    if not text_id:
        session, positionals[1] = split_session(positionals[1])
    return {
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
    arguments = parse(argv)
    if arguments is None:
        print(USAGE, file=sys.stderr)
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
    match refused_at(action, int(priority)):
        case ActionRefused(reason):
            return refuse(source, priority, title, message, reason)
        case ActionAllowed():
            pass
    draft = review.Draft(source, priority, title, action,
                         trimmed_body(action, arguments["message"], arguments["session"], header=arguments["header"]),
                         arguments["answerable"], arguments["session"])
    header, key = arguments["header"], arguments["key"]
    claimed = False
    try:
        found = review.NoHold() if arguments["text_id"] else review.held_for(source)
        if isinstance(found, review.LiveHold):
            claimed = review.claim(source)
            if not claimed:
                print("pushover: held for review: this source's push is being sent", file=sys.stderr)
                return 4
            found = review.held_for(source)
    except OSError as error:
        found = review.UnreadableHold(review.held_path(source), str(error))
    detail = ""
    if review.switched_off():
        outcome: review.ReviewOutcome = review.Unreviewed("review off")
        version = draft
        detail = "review: off"
    else:
        outcome = review.review(draft, revision=claimed)
        # A registered text is fixed and approved ahead of time: the review is a logged check only.
        version = draft if arguments["text_id"] else review.best_version(draft, outcome)
        if isinstance(outcome, review.Unreviewed):
            detail = "unreviewed: " + outcome.why
        elif arguments["text_id"] and isinstance(outcome, review.Rewritten):
            detail = "registered text sent as rendered, rewrite not applied"
    if isinstance(outcome, review.Questions):
        outcome = review.Failed(outcome.best_version, outcome.failed_rules, outcome.metadata, outcome.failures)
    rejected = isinstance(outcome, (review.Failed, review.Withheld))
    send_anyway = rejected and (priority == "2" or bool(arguments["text_id"]))
    if send_anyway:
        detail = "sent despite failed review: " + ", ".join(outcome.failed_rules if isinstance(outcome, (review.Failed, review.Withheld)) else [])
    record: ReviewedPushRecord = {**review.message_record(version)}
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
            completion = review.post_claim(found.hold, lambda: deliver(version, keys_file, post, detail=detail, header=header, key=key))
        if isinstance(completion, review.ClaimCancelled):
            review.log_review(draft, outcome, detail="claim cancelled before posting")
            print("pushover: replaced: a newer version replaced this push; nothing was sent")
            return 5
        code = completion.code if isinstance(completion, review.ClaimFailed) else 0
    else:
        code = deliver(version, keys_file, post, detail=detail, header=header, key=key)
    delivery = review.Sent(version) if code == 0 else review.NotSent()
    review.log_review(draft, outcome, delivery, detail=detail, text_id=arguments["text_id"])
    if code == 0 and send_anyway:
        print("pushover: " + detail)
        if arguments["text_id"]:
            request_template_fix(record)
    if code == 0 and not arguments["text_id"] and isinstance(outcome, (review.Rewritten, review.Questions)):
        print("pushover: sent as rewritten:")
        print(version.title)
        print(first_line(version.action, version.session))
        print(version.body)
    return code


def request_template_fix(record: ReviewedPushRecord) -> None:
    """Direct timer callers use the sender's daily repair-notice handler too."""
    sender = Path(__file__).resolve().parents[1] / "message/send.py"
    try:
        _ = subprocess.Popen([sys.executable, str(sender), "template-fix", json.dumps(record)],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError as error:
        print(f"pushover: template fix notice failed: {error}", file=sys.stderr)
        log("template fix", "0", record["title"], record["body"], f"notice failed: {error}")


def deliver(draft: review.Draft, keys_file: Path = KEYS_FILE,
            post: Post = http_post, *, detail: str = "", header: bool = True, key: str = "") -> int:
    """Post an already reviewed version, including expiry sends; do not review again.

    During silence a non-empty `key` makes the kept push replace an older kept one from the same source.
    """
    source, priority, title = draft.source, draft.priority, draft.title
    message = posted_message(draft.action, draft.body, draft.session, header=header)
    if priority != "2":
        kept = silence.keep({"source": source, "priority": priority, "title": title,
                             "message": message, "detail": detail, "key": key},
                            deliver=lambda push: post_kept(push, keys_file, post))
        if isinstance(kept, silence.Kept):
            log(source, priority, title, message, f"kept: silence {kept.until}" + (f", {detail}" if detail else ""))
            print(f"pushover: kept: silence {kept.until}")
            return 0
    return post_message(source, priority, title, message, keys_file, post, detail)


def post_kept(push: silence.KeptPush, keys_file: Path = KEYS_FILE, post: Post = http_post) -> int:
    """Post a push kept during silence, exactly as it was kept."""
    detail = ", ".join(part for part in ("kept during silence", push["detail"]) if part)
    return post_message(push["source"], push["priority"], push["title"], push["message"], keys_file, post, detail)


def post_message(source: str, priority: str, title: str, message: str,
                 keys_file: Path, post: Post, detail: str) -> int:
    suffix = f", {detail}" if detail else ""
    keys = read_keys(keys_file)
    if keys is None:
        log(source, priority, title, message, "keys missing" + suffix)
        print(f"pushover: PUSHOVER_USER and PUSHOVER_TOKEN must both be set in {keys_file}", file=sys.stderr)
        return 2
    data = urllib.parse.urlencode(fields(keys, title, message, priority)).encode()
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


if __name__ == "__main__":
    sys.exit(send(sys.argv[1:]))
