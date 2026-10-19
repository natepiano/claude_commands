#!/usr/bin/env python3
"""Set a hanadocs issue's proposal fields: approve, start, close or stars.

Edits one issue file's frontmatter in place and reads nothing else. Every verb also sets
date_modified to today in the user's zone, written "[[YYYY-MM-DD]]" as the vault writes dates.
Each verb holds an exclusive lock on the issue for its whole read, change and write. The lock
file is in the user's runtime folder, never in the issue folder.

Usage:
  proposal_issue.py approve <issue> --showrunner S --unit U --plan P
  proposal_issue.py start <issue>
  proposal_issue.py close <issue> --as-built PATH
  proposal_issue.py rules <issue> --rules URLS
  proposal_issue.py stars <issue> --goal G --alignment A --impact I --urgency U --effort E
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import sys
import tempfile
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import cast

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
from user_zone import user_zone  # noqa: E402
from issues import Issue, frontmatter

FENCE = "---"
# Values the vault writes bare; every other value is written double-quoted.
BARE = frozenset({"stage", "status"})
KEY = re.compile(r"^([A-Za-z_][\w-]*):")
GOAL = re.compile(r"^\d+ - \S.*$")
STAR = "⭐"
# The fields a migrated decision can take from the old issues it merges.
INHERITED_FIELDS = frozenset({"showrunner", "unit", "plan", "stage", "backlog_goal", "backlog_alignment",
                              "backlog_impact", "backlog_urgency", "backlog_effort"})
# The fields close adds or changes; reopen removes them and sets status open.
CLOSE_FIELDS = ("as_built", "reason", "date_closed")
PROPOSED_RANKING = re.compile(r"^Proposed ranking:[^\r\n]*", re.MULTILINE)
EMPTY_RANKING = re.compile(r"^Proposed ranking:[ \t]*(?=\r?$)", re.MULTILINE)


class Refusal(ValueError):
    """The issue file cannot be edited."""


def _quoted(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _value_line(key: str, value: str) -> str:
    return f"{key}: {value if key in BARE else _quoted(value)}\n"


def _closing_fence(lines: list[str], path: Path) -> int:
    if not lines or lines[0].rstrip("\r\n") != FENCE:
        raise Refusal(f"{path} has no frontmatter")
    for index in range(1, len(lines)):
        if lines[index].rstrip("\r\n") == FENCE:
            return index
    raise Refusal(f"{path} has no closing --- for its frontmatter")


def _key_span(lines: list[str], end: int, key: str) -> tuple[int, int] | None:
    """The key's line and its continuation lines, as a slice, or None when the key is absent."""
    for start in range(1, end):
        match = KEY.match(lines[start])
        if match is None or match.group(1) != key:
            continue
        stop = start + 1
        while stop < end and KEY.match(lines[stop]) is None:
            stop += 1
        return start, stop
    return None


def set_fields(text: str, fields: dict[str, str], path: Path) -> tuple[str, list[str]]:
    """The text with each field set, and the keys whose lines changed."""
    lines = text.splitlines(keepends=True)
    changed: list[str] = []
    for key, value in fields.items():
        end = _closing_fence(lines, path)
        line = _value_line(key, value)
        span = _key_span(lines, end, key)
        if span is None:
            lines.insert(end, line)
            changed.append(key)
            continue
        start, stop = span
        newline = lines[start][len(lines[start].rstrip("\r\n")):] or "\n"
        replacement = line[:-1] + newline
        if lines[start:stop] != [replacement]:
            lines[start:stop] = [replacement]
            changed.append(key)
    return "".join(lines), changed


def remove_fields(text: str, keys: tuple[str, ...], path: Path) -> tuple[str, list[str]]:
    """The text without each key and its continuation lines, and the keys it removed."""
    lines = text.splitlines(keepends=True)
    removed: list[str] = []
    for key in keys:
        span = _key_span(lines, _closing_fence(lines, path), key)
        if span is not None:
            del lines[span[0]:span[1]]
            removed.append(key)
    return "".join(lines), removed


def set_list_fields(text: str, fields: dict[str, list[str]], path: Path) -> tuple[str, list[str]]:
    """Set JSON lists without changing other frontmatter or body bytes."""
    lines = text.splitlines(keepends=True)
    changed: list[str] = []
    for key, values in fields.items():
        end = _closing_fence(lines, path)
        span = _key_span(lines, end, key)
        start, stop = span if span is not None else (end, end)
        newline = lines[start][len(lines[start].rstrip("\r\n")):] or "\n"
        replacement = f"{key}: {json.dumps(values, ensure_ascii=False)}{newline}"
        if lines[start:stop] != [replacement]:
            lines[start:stop] = [replacement]
            changed.append(key)
    return "".join(lines), changed


@contextmanager
def _issue_lock(path: Path) -> Generator[None]:
    """Hold an exclusive lock on the issue. The lock file is named from the issue's resolved path."""
    folder = Path(os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir())
    folder.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:32]
    with (folder / f"proposal-issue-{digest}.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def edit_rules(path: Path, urls: list[str], today: date) -> list[str]:
    with _issue_lock(path):
        text, changed = set_list_fields(_read(path), {"style_rules": sorted(set(urls))}, path)
        text, dates = set_fields(text, {"date_modified": _vault_date(today)}, path)
        changed.extend(dates)
        if changed:
            _write(path, text)
        return changed


def _read(path: Path) -> str:
    if not path.is_file():
        raise Refusal(f"{path} is not a file")
    with path.open(encoding="utf-8", newline="") as source:
        return source.read()


def _write(path: Path, text: str) -> None:
    temporary_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as temporary:
            temporary_name = temporary.name
            _ = temporary.write(text)
        os.replace(temporary_name, path)
        temporary_name = ""
    finally:
        if temporary_name:
            Path(temporary_name).unlink(missing_ok=True)


def _stars(name: str, value: str) -> str:
    """One to five stars, from a count or from the stars themselves."""
    count = int(value) if value.isdigit() else len(value) if value and value == STAR * len(value) else 0
    if not 1 <= count <= 5:
        raise Refusal(f"--{name} takes 1 to 5 (or one to five {STAR}), not {value!r}")
    return STAR * count


def _vault_date(today: date) -> str:
    return f"[[{today.isoformat()}]]"


def edit(path: Path, fields: dict[str, str], today: date) -> list[str]:
    """Set the fields and date_modified in the issue at path. Returns the keys whose lines changed."""
    with _issue_lock(path):
        text, changed = set_fields(_read(path), {**fields, "date_modified": _vault_date(today)}, path)
        if changed:
            _write(path, text)
        return changed


def reopen(path: Path, today: date) -> list[str]:
    """Remove the close fields and set status open. The answer stays."""
    with _issue_lock(path):
        text, removed = remove_fields(_read(path), CLOSE_FIELDS, path)
        text, changed = set_fields(text, {"status": "open"}, path)
        if not removed and not changed:
            return []
        text, dates = set_fields(text, {"date_modified": _vault_date(today)}, path)
        _write(path, text)
        return [*removed, *changed, *dates]


def fields_for(args: argparse.Namespace, today: date) -> dict[str, str]:
    verb = cast(str, args.verb)
    if verb == "approve":
        return {
            "showrunner": cast(str, args.showrunner),
            "unit": cast(str, args.unit),
            "plan": cast(str, args.plan),
        }
    if verb == "start":
        return {"stage": "active"}
    if verb == "close":
        detail = {"as_built": cast(str, args.as_built)} if cast(str | None, args.as_built) else {"reason": cast(str, args.reason)}
        return {**detail, "status": "closed", "date_closed": _vault_date(today)}
    goal = cast(str, args.goal)
    if GOAL.match(goal) is None:
        raise Refusal(f"--goal takes a goal like '1 - Ship Hana', not {goal!r}")
    return {
        "backlog_goal": goal,
        **{f"backlog_{name}": _stars(name, cast(str, getattr(args, name))) for name in (
            "alignment", "impact", "urgency", "effort",
        )},
    }


GENERATED_START = "<!-- style-decision:start -->"
GENERATED_END = "<!-- style-decision:end -->"


def decision_body(body: str) -> str:
    return GENERATED_START + "\n" + body.rstrip() + "\n" + GENERATED_END + "\n"


@dataclass(frozen=True)
class UnansweredDecision:
    """The user has not answered this decision."""


@dataclass(frozen=True)
class ApprovedDecision:
    unit: str
    plan: str


@dataclass(frozen=True)
class DeclinedDecision:
    words: str


def decision_answer(issue: Issue) -> UnansweredDecision | ApprovedDecision | DeclinedDecision:
    if issue.value("style_answer") == "declined":
        return DeclinedDecision(issue.value("style_answer_words"))
    if issue.value("unit") and issue.value("plan"):
        return ApprovedDecision(issue.value("unit"), issue.value("plan"))
    return UnansweredDecision()


def _generated_span(text: str, path: Path) -> tuple[int, int]:
    start, end = text.find(GENERATED_START), text.find(GENERATED_END)
    if start < 0 or end < start:
        raise Refusal(f"{path}: missing generated decision body")
    return start, end + len(GENERATED_END)


def require_generated_body(path: Path) -> None:
    """Refuse a decision that a refresh cannot write."""
    _ = _generated_span(_read(path), path)


def refresh_decision(path: Path, count: int, body: str, ranking: str, today: date) -> list[str]:
    """Replace the generated body and style_count, and the ranking while the decision is unanswered.

    Write the file only when its bytes change.
    """
    if count < 0:
        raise Refusal("refresh requires a nonnegative count")
    with _issue_lock(path):
        text = _read(path)
        start, end = _generated_span(text, path)
        head = text[:start]
        if isinstance(decision_answer(Issue(path, frontmatter(path))), UnansweredDecision):
            head = PROPOSED_RANKING.sub(lambda _: f"Proposed ranking: {ranking}", head, count=1)
        updated = head + decision_body(body).rstrip("\n") + text[end:]
        updated, changed = set_fields(updated, {"style_count": str(count)}, path)
        if updated == text:
            return []
        updated, dates = set_fields(updated, {"date_modified": _vault_date(today)}, path)
        _write(path, updated)
        return ["body", *changed, *dates]


def fill_ranking(path: Path, ranking: str) -> bool:
    """Fill an empty Proposed ranking line. Write the file only when its bytes change; True when it does."""
    with _issue_lock(path):
        text = _read(path)
        updated = EMPTY_RANKING.sub(lambda _: f"Proposed ranking: {ranking}", text, count=1)
        if updated == text:
            return False
        _write(path, updated)
        return True


def create(path: Path, project: str, short: str, proposed_by: str, tags: list[str],
           rule: str, crate: str, rule_blob: str, count: int, body: str, template: Path,
           today: date, proposed_ranking: str = "", rule_urls: tuple[str, ...] = (), *,
           initial_fields: Mapping[str, str]) -> bool:
    """Create a decision once, identified by its rule, crate and rule text."""
    keys = {"style_rule": rule, "style_crate": crate, "style_rule_blob": rule_blob}
    if not all(keys.values()) or count < 1:
        raise Refusal("create requires a rule, crate, rule blob and positive count")
    inherited = dict(initial_fields)
    if any(key not in INHERITED_FIELDS for key in inherited):
        raise Refusal("invalid inherited fields: " + ", ".join(sorted(set(inherited) - INHERITED_FIELDS)))
    if path.exists():
        fields = frontmatter(path)
        if any(fields.get(key) != [value] for key, value in keys.items()):
            raise Refusal(f"{path}: different decision key")
        return False
    text = template.expanduser().read_text(encoding="utf-8")
    values = {
        "AREA": "unfiled", "PROJECT": project, "SHORT": short, "PROPOSED_BY": proposed_by,
        "DATE": today.isoformat(), "CATEGORY": "refactor", "DEPENDS_ON": "[]",
    }
    text = re.sub(r"\{\{([A-Z_]+)\}\}", lambda match: values.get(match[1], ""), text)
    lists = {"tags": list(dict.fromkeys(["issue", "proposal", *tags]))}
    if rule_urls:
        lists["style_rules"] = sorted(set(rule_urls))
    text, _ = set_list_fields(text, lists, path)
    text, _ = set_fields(text, {"status": "open", "stage": "backlog", **inherited, **keys, "style_count": str(count)}, path)
    text = text.rstrip() + f"\n\nProposed ranking: {proposed_ranking}\n\n" + decision_body(body)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, prefix=".decision-", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            _ = stream.write(text)
            stream.flush()
            os.link(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    return True


def _initial_fields(raw: str) -> dict[str, str]:
    decoded = cast(object, json.loads(raw))
    if not isinstance(decoded, dict):
        raise Refusal("--initial-fields takes a JSON object of text fields")
    fields: dict[str, str] = {}
    for key, value in cast(dict[str, object], decoded).items():
        if not isinstance(value, str):
            raise Refusal(f"--initial-fields: {key} is not text")
        fields[key] = value
    return fields


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    verbs = parser.add_subparsers(dest="verb", required=True)
    approve = verbs.add_parser("approve", help="record the user's approval; stage stays backlog")
    _ = approve.add_argument("--showrunner", required=True, help="the showrunner's session name")
    _ = approve.add_argument("--unit", required=True, help="the unit, as in the production's Units table")
    _ = approve.add_argument("--plan", required=True, help="'<plan path> Phase N–M'")
    _ = verbs.add_parser("start", help="stage active")
    close = verbs.add_parser("close", help="set as_built and close")
    closure = close.add_mutually_exclusive_group(required=True)
    _ = closure.add_argument("--as-built", help="the as-built doc")
    _ = closure.add_argument("--reason", help="why the issue is closed")
    creation = verbs.add_parser("create", help="create a proposal from held findings")
    _ = creation.add_argument("issue", type=Path)
    for name in ("project", "short", "proposed-by", "rule", "crate", "rule-blob"):
        _ = creation.add_argument(f"--{name}", required=True)
    _ = creation.add_argument("--count", type=int, required=True)
    _ = creation.add_argument("--extra-tags", nargs="+", default=[])
    _ = creation.add_argument("--proposed-ranking", default="")
    _ = creation.add_argument("--rules", default="")
    _ = creation.add_argument("--initial-fields", default="{}")
    _ = creation.add_argument("--body-file", type=Path, required=True)
    _ = creation.add_argument("--template", type=Path, default=Path.home() / "rust/hanadocs/.claude/templates/issue.yaml")
    reopening = verbs.add_parser("reopen", help="reopen a built decision, keeping its answer")
    answer = verbs.add_parser("decline-answer", help="store a decision's declined answer")
    _ = answer.add_argument("--words", required=True)
    linking = verbs.add_parser("rules", help="set the rule links")
    _ = linking.add_argument("--rules", required=True)
    stars = verbs.add_parser("stars", help="set the five ranking values")
    _ = stars.add_argument("--goal", required=True, help="e.g. '1 - Ship Hana'")
    for name in ("alignment", "impact", "urgency", "effort"):
        _ = stars.add_argument(f"--{name}", required=True, help="1 to 5")
    for verb in (approve, verbs.choices["start"], close, stars, linking, reopening, answer):
        _ = verb.add_argument("issue", type=Path)
    args = parser.parse_args(argv)
    verb = cast(str, args.verb)
    # A plan's Issues line writes `~/rust/hanadocs/issues/<name>.md`, quoted since names hold spaces.
    path = cast(Path, args.issue).expanduser()
    today = datetime.now(user_zone()).date()
    try:
        if verb == "create":
            created = create(path, cast(str, args.project), cast(str, args.short), cast(str, args.proposed_by),
                             cast(list[str], args.extra_tags), cast(str, args.rule), cast(str, args.crate),
                             cast(str, args.rule_blob), cast(int, args.count),
                             cast(Path, args.body_file).read_text(), cast(Path, args.template), today,
                             cast(str, args.proposed_ranking),
                             tuple(url for url in cast(str, args.rules).split(",") if url),
                             initial_fields=_initial_fields(cast(str, args.initial_fields)))
            print(f"create {path.name}: {'created' if created else 'unchanged'}")
            return 0
        if verb == "reopen":
            changed = reopen(path, today)
        elif verb == "decline-answer":
            changed = edit(path, {"style_answer": "declined", "style_answer_words": cast(str, args.words)}, today)
        elif verb == "rules":
            changed = edit_rules(path, [url for url in cast(str, args.rules).split(",") if url], today)
        else:
            changed = edit(path, fields_for(args, today), today)
    except (ValueError, OSError, UnicodeError) as error:
        print(f"proposal_issue: {error}", file=sys.stderr)
        return 2
    print(f"{verb} {path.name}: set {', '.join(changed)}" if changed else f"{verb} {path.name}: unchanged")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
