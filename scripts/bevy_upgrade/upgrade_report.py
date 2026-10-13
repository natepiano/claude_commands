"""Derive upgrade states and print current pins with the user's recorded words."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from pins import Pin
from upgrade_record import Announced, Entry, Record, on_target, target_fit

State = Literal["git ready", "release ready", "re-vendor", "port in PR", "no port yet", "not checked", "done"]
STATE_ORDER: tuple[State, ...] = (
    "git ready", "release ready", "re-vendor", "port in PR", "no port yet", "not checked", "done",
)


def state(entry: Entry, target: str) -> State:
    if on_target(entry["bevy"], target):
        return "done"
    if entry["patched_bevy"] or entry["pin"]["source"] == "path":
        return "re-vendor"
    facts = entry["crates_io"]
    if facts["status"] == "read" and facts["ready"]:
        return "release ready"
    branch = entry["branch"]
    if branch["status"] == "read" and target_fit(branch["requirement"], target) == "on target":
        return "git ready"
    if any(item["kind"] == "pr" and item["state"] == "open" for item in entry["items"]):
        return "port in PR"
    if facts["status"] == "unread":
        return "not checked"
    return "no port yet"


def _pin(pin: Pin) -> str:
    if pin["source"] == "git":
        revision = pin["rev"][:7]
        return f"git {pin['ref'] or revision} @ {revision}"
    if pin["source"] == "path":
        return f"path {pin['path']}, {pin['version']}"
    return pin["version"]


def _cell(text: str) -> str:
    return text.replace("|", r"\|").replace("\r\n", "<br>").replace("\n", "<br>")


def _release(version: str, requirement: str) -> str:
    return f"{version} on Bevy {requirement}" if requirement else f"{version} requirement unknown"


def _crates_io(entry: Entry, target: str) -> str:
    facts = entry["crates_io"]
    if entry["patched_bevy"] or entry["pin"]["source"] == "path":
        cell = "—"
    elif facts["status"] == "unread":
        cell = "—" if on_target(entry["bevy"], target) else "not checked"
    elif facts["status"] == "unlisted":
        cell = "not on crates.io"
    else:
        cell = _release(facts["newest"], facts["releases"].get(facts["newest"], ""))
        if facts["ready"] and facts["ready"] != facts["newest"]:
            cell += f" → {_release(facts['ready'], facts['releases'].get(facts['ready'], ''))}"
    if entry["error"]:
        cell += f"; last check failed: {entry['error']}"
    return cell


def _git(entry: Entry) -> str:
    branch = entry["branch"]
    facts = entry["crates_io"]
    if branch["status"] == "read":
        cell = _release(f"{branch['name']} @ {branch['rev'][:7]}", branch["requirement"])
    elif facts["status"] != "unread" and not facts["repository"]:
        cell = "not on GitHub"
    else:
        cell = "—"
    for item in sorted(entry["items"], key=lambda item: item["number"], reverse=True):
        if item["kind"] == "pr":
            cell += f"; PR #{item['number']} {item['state']} ({item['head']})"
        else:
            cell += f"; issue #{item['number']} {item['state']}"
    return cell


def _next(entry: Entry, target: str, current: State) -> str:
    if current == "done":
        return "—"
    if current == "re-vendor":
        if entry["patched_bevy"]:
            return f"re-vendor from Bevy {target}"
        pin = entry["pin"]
        if pin["source"] == "path":
            return f"port the copy at {pin['path']} to Bevy {target}"
    if current == "release ready":
        facts = entry["crates_io"]
        if facts["status"] == "read":
            return f"bump to {facts['ready']}"
    if current == "git ready":
        branch = entry["branch"]
        answer = entry["answer"]
        if answer is None:
            return "ask the user: git pin?"
        if answer["answer"] == "declined":
            return f"wait for a release (declined {answer['at']})"
        if branch["status"] == "read":
            return f"pin git rev {branch['rev'][:7]} (approved {answer['at']})"
    if current == "port in PR":
        number = max(item["number"] for item in entry["items"]
                     if item["kind"] == "pr" and item["state"] == "open")
        return f"wait for PR #{number}"
    return "wait" if current == "no port yet" else "run check"


def _stale(record: Record, now: datetime, checked: datetime) -> bool:
    return (now - checked > timedelta(hours=8) or bool(record["target_error"])
            or any(entry["error"] for entry in record["dependencies"].values()))


def _upstream_line(record: Record, zone: ZoneInfo, now: datetime) -> str:
    target = record["target_bevy"]
    if target["status"] == "released":
        line = f"Bevy {record['target']}: released as {target['version']} on {target['date']}."
    elif target["status"] == "unread":
        line = f"Bevy {record['target']}: release not looked up yet."
    else:
        line = f"Bevy {record['target']}: not released yet."
    if record["target_error"]:
        line += f" Release lookup failed: {record['target_error']}."
    checked = record["checked"]
    if checked is None:
        return line + " Upstream: no complete check yet."
    read_at = datetime.fromisoformat(checked).astimezone(zone)
    stale = _stale(record, now, read_at)
    return line + (f" Upstream last checked {read_at:%Y-%m-%d %H:%M %Z} (stale)." if stale
                   else f" Upstream checked {read_at:%H:%M %Z}.")


def _row(name: str, entry: Entry, target: str) -> str:
    dependency = name
    if entry["patched_bevy"]:
        dependency += " (patched Bevy crate)"
    elif entry["via"]:
        dependency += f" (via {', '.join(entry['via'])})"
    pin = _pin(entry["pin"])
    if not entry["patched_bevy"]:
        pin += f" on Bevy {entry['bevy']}"
    current = state(entry, target)
    next_step = _next(entry, target, current)
    return f"| {_cell(dependency)} | {_cell(pin)} | {_cell(_crates_io(entry, target))} | {_cell(_git(entry))} | {current} | {_cell(next_step)} |"


def table(record: Record, zone: ZoneInfo, now: datetime) -> str:
    lines = [
        f"Bevy {record['target']} upgrade for {record['slug']}: {record['checkout']}, on Bevy {' and '.join(record['bevy'])}.",
        _upstream_line(record, zone, now),
        "",
        "| Dependency | Our pin | crates.io | Git | State | Next |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    dependencies = record["dependencies"]
    for name in sorted(dependencies, key=lambda name: (STATE_ORDER.index(state(dependencies[name], record["target"])), name)):
        lines.append(_row(name, dependencies[name], record["target"]))
    answers: list[str] = []
    notes: list[str] = []
    for name in sorted(dependencies):
        entry = dependencies[name]
        answer = entry["answer"]
        if answer is not None:
            answers.append(f"- {name}: {answer['answer']} {answer['at']}: \"{answer['words']}\"")
        if entry["note"]:
            notes.append(f"- {name}: {entry['note']}")
    if answers:
        lines.extend(["", "Answers on git pins:", *answers])
    if notes:
        lines.extend(["", "Notes:", *notes])
    return "\n".join(lines)


def announcement(entry: Entry, target: str) -> Announced:
    branch = entry["branch"]
    return Announced(state=state(entry, target),
                     repository=branch["repository"] if branch["status"] == "read" else "",
                     items={str(item["number"]): item["state"] for item in entry["items"]})


def changes(record: Record) -> list[str]:
    previous = record["announced"]
    if previous is None:
        return []
    lines: list[str] = []
    dependencies = record["dependencies"]
    for name in sorted(set(dependencies) | set(previous)):
        if name not in dependencies:
            lines.append(f"- {name}: no longer a dependency")
            continue
        if name not in previous:
            lines.append(f"- {name}: new dependency")
            continue
        entry = dependencies[name]
        current = announcement(entry, record["target"])
        old = previous[name]
        if current["state"] != old["state"]:
            detail = ""
            facts, branch = entry["crates_io"], entry["branch"]
            if current["state"] == "release ready" and facts["status"] == "read":
                detail = f" ({facts['ready']} on Bevy {facts['releases'].get(facts['ready'], '')})"
            elif current["state"] == "git ready" and branch["status"] == "read":
                detail = f" ({branch['name']} @ {branch['rev'][:7]})"
            lines.append(f"- {name}: {old['state']} → {current['state']}{detail}")
        old_items = old["items"] if old["repository"] == current["repository"] else {}
        for item in sorted(entry["items"], key=lambda item: item["number"], reverse=True):
            number = str(item["number"])
            label = "PR" if item["kind"] == "pr" else "issue"
            if number not in old_items:
                lines.append(f"- {name}: new {label} #{number} {item['state']}: {item['title']}")
            elif item["state"] != old_items[number]:
                lines.append(f"- {name}: {label} #{number} {old_items[number]} → {item['state']}")
    return lines


def waiting(record: Record) -> int:
    return sum(state(entry, record["target"]) == "git ready" and entry["answer"] is None
               for entry in record["dependencies"].values())


def line(record: Record, zone: ZoneInfo, now: datetime,
         next_due: datetime | None, pins_error: str) -> str:
    """None means there is no scheduled next check to report."""
    counts = {word: 0 for word in STATE_ORDER}
    awaiting_answer = waiting(record)
    for entry in record["dependencies"].values():
        word = state(entry, record["target"])
        counts[word] += 1
    text = f"Bevy {record['target']} upgrade: {counts['done']} of {len(record['dependencies'])} done"
    for word in STATE_ORDER:
        if word != "done" and counts[word]:
            text += f", {counts[word]} {word}"
            if word == "git ready" and awaiting_answer:
                text += f" ({awaiting_answer} waiting for your answer)"
    checked = record["checked"]
    if checked is None:
        text += "; no complete upstream check yet"
    else:
        read_at = datetime.fromisoformat(checked).astimezone(zone)
        if _stale(record, now, read_at):
            text += f"; upstream last checked {read_at:%Y-%m-%d %H:%M %Z} (stale)"
        else:
            text += f"; checked {read_at:%H:%M %Z}"
            if next_due is not None:
                text += f", next check {next_due.astimezone(zone):%H:%M %Z}"
    if pins_error:
        pins_at = datetime.fromisoformat(record["pins_read"]).astimezone(zone)
        text += f"; pins not re-read since {pins_at:%Y-%m-%d %H:%M %Z}"
    return text
