#!/usr/bin/env python3
"""Set a hanadocs issue's proposal fields: approve, unapprove, start, close or stars.

Each verb edits one issue file's frontmatter in place and reads nothing else, but number and create
read the folder for the highest P number, move-approved edits each open approved proposal in it,
and area-style sets the area on current and legacy style proposals. Every verb also sets
date_modified to today in the user's zone, written "[[YYYY-MM-DD]]" as the vault writes dates.
Each verb holds an exclusive lock on the issue for its whole read, change and write. The lock
file is in the user's state folder, never in the issue folder.

Usage:
  proposal_issue.py approve <issue> --by user|showrunner --showrunner S --unit U --plan P
  proposal_issue.py unapprove <issue> [--dry-run]
  proposal_issue.py reopen <issue>
  proposal_issue.py decline-answer <issue> --words W
  proposal_issue.py start <issue>
  proposal_issue.py close <issue> --as-built PATH
  proposal_issue.py rules <issue> --rules URLS
  proposal_issue.py stars <issue> --goal G --alignment A --impact I --urgency U --effort E
  proposal_issue.py urgency <issue> --level user|blocks|soon|normal|later [--due ISO]
  proposal_issue.py mark-important <issue>
  proposal_issue.py number <issue>...
  proposal_issue.py move-approved <issues folder> [--dry-run]
  proposal_issue.py area-style <issues folder> [--dry-run]

`number` gives each proposal without one the next `proposal_id`, P1 up, as change rows are C1 up;
`create` numbers a new decision the same way. `urgency` sets where the proposal goes among the ones
waiting for the user (urgency.py): `review_urgency`, with `review_due` for soon; normal removes them.
`mark-important` marks a proposal the user called important (`review_urgency: user`, `review_marked`).
`approve` records who approved (`approved_by`: user, or showrunner when a showrunner approves without
the user) and the production of the showrunner it runs in (`approved_in`). A user approval replaces
a showrunner one; a showrunner approval keeps a user one. An approval with no `approved_by` is the
user's. `unapprove` sets an open proposal that a showrunner approved, in the calling showrunner,
back to waiting for approval: the approval fields empty, as filing writes them, and stage backlog.
It refuses a user approval and any other issue; only the user's decline undoes a user approval.
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
from collections.abc import Generator, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import cast

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
import urgency  # noqa: E402
from user_zone import user_zone  # noqa: E402
from issues import PROPOSAL_TAG, Issue, frontmatter, open_issues

FENCE = "---"
# Values the vault writes bare; every other value is written double-quoted.
BARE = frozenset({"stage", "status"})
KEY = re.compile(r"^([A-Za-z_][\w-]*):")
GOAL = re.compile(r"^\d+ - \S.*$")
STAR = "⭐"
# The fields a migrated decision can take from the old issues it merges.
INHERITED_FIELDS = frozenset({"showrunner", "unit", "plan", "approved_by", "approved_in", "stage", "backlog_goal", "backlog_alignment",
                              "backlog_impact", "backlog_urgency", "backlog_effort"})
# The fields close adds or changes; reopen removes them and sets status open.
CLOSE_FIELDS = ("as_built", "reason", "date_closed")
PROPOSED_RANKING = re.compile(r"^Proposed ranking:[^\r\n]*", re.MULTILINE)
EMPTY_RANKING = re.compile(r"^Proposed ranking:[ \t]*(?=\r?$)", re.MULTILINE)
NUMBER_FIELD = "proposal_id"
NUMBER = re.compile(r'^proposal_id:\s*"?P(\d+)"?\s*$', re.MULTILINE)
# The vault keeps closed issues under archive/ beside the issues folder; their numbers stay taken.
ARCHIVE = "archive"
STYLE_AREA = "style"


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
    folder = Path.home() / ".local/state/proposal-issue/locks"
    folder.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:32]
    with (folder / f"proposal-issue-{digest}.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


@contextmanager
def _folder_lock(folder: Path) -> Generator[None]:
    """Hold an exclusive lock on the issues folder, so two filings never take one number."""
    with _issue_lock(folder / ".proposal-numbers"):
        yield


def _highest_number(folder: Path) -> int:
    """The highest P number in the folder or in the vault's archive beside it, or 0."""
    highest = 0
    for root in (folder, folder.parent / ARCHIVE):
        for issue in root.rglob("*.md") if root.is_dir() else ():
            try:
                head = issue.read_text(encoding="utf-8").split(f"\n{FENCE}", 1)[0]
            except (OSError, UnicodeError):
                continue
            highest = max([highest, *(int(found[1]) for found in NUMBER.finditer(head))])
    return highest


def number(paths: list[Path], today: date) -> list[str]:
    """Give each proposal without a number the next one, oldest first. Returns one line per issue."""
    issues = sorted((Issue(path, frontmatter(path)) for path in paths),
                    key=lambda issue: (issue.value("date_created"), issue.path.name))
    lines: list[str] = []
    for issue in issues:
        if "proposal" not in issue.fields.get("tags", []):
            lines.append(f"number {issue.path.name}: not a proposal")
            continue
        with _folder_lock(issue.path.parent):
            taken = NUMBER.search(_read(issue.path).split(f"\n{FENCE}", 1)[0])
            if taken:
                lines.append(f"number {issue.path.name}: P{taken[1]}")
                continue
            given = f"P{_highest_number(issue.path.parent) + 1}"
            _ = edit(issue.path, {NUMBER_FIELD: given}, today)
            lines.append(f"number {issue.path.name}: {given}")
    return lines


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
    """Remove the close fields and set status open. The answer stays, and an approved one is in progress."""
    with _issue_lock(path):
        reopened = {"status": "open"}
        if isinstance(decision_answer(Issue(path, frontmatter(path))), ApprovedDecision):
            reopened["stage"] = IN_PROGRESS_STAGE
        text, removed = remove_fields(_read(path), CLOSE_FIELDS, path)
        text, changed = set_fields(text, reopened, path)
        if not removed and not changed:
            return []
        text, dates = set_fields(text, {"date_modified": _vault_date(today)}, path)
        _write(path, text)
        return [*removed, *changed, *dates]


USER = "user"
SHOWRUNNER = "showrunner"
APPROVERS = (USER, SHOWRUNNER)
# The fields approve sets; unapprove writes them empty, as filing does.
APPROVAL_FIELDS = ("showrunner", "unit", "plan", "approved_by", "approved_in")


def calling_showrunner() -> str:
    """The production slug of the showrunner this session runs, from its update timer."""
    from scripts.production import showrunners  # only approve and unapprove read the update timers

    doc = showrunners.production_of(os.environ.get("CLAUDE_CODE_SESSION_ID", ""))
    if doc is None:
        raise Refusal("this session is no production's showrunner; approve and unapprove run in one")
    return showrunners.production_slug(str(doc))


def approver(issue: Issue) -> str:
    """Who approved: showrunner only when approved_by says so; an approval without the field is the user's."""
    return SHOWRUNNER if issue.value("approved_by") == SHOWRUNNER else USER


def approve(path: Path, fields: dict[str, str], by: str, approved_in: str, today: date) -> list[str]:
    """Record the approval and stage active. A showrunner approval keeps the approver of a user one."""
    with _issue_lock(path):
        return _approve(path, fields, by, approved_in, today)


def approve_unanswered(path: Path, fields: dict[str, str], by: str, approved_in: str, today: date) -> list[str]:
    """Approve as approve does, only while no answer is recorded, read under the same lock as the write."""
    with _issue_lock(path):
        if not isinstance(decision_answer(Issue(path, frontmatter(path))), UnansweredDecision):
            return []
        return _approve(path, fields, by, approved_in, today)


def _approve(path: Path, fields: dict[str, str], by: str, approved_in: str, today: date) -> list[str]:
    """The approval write; the caller holds the issue lock."""
    issue = Issue(path, frontmatter(path))
    kept = by == SHOWRUNNER and isinstance(decision_answer(issue), ApprovedDecision) and approver(issue) == USER
    actor = {} if kept else {"approved_by": by, "approved_in": approved_in}
    text, changed = set_fields(_read(path), {**fields, **actor, "stage": IN_PROGRESS_STAGE,
                                             "date_modified": _vault_date(today)}, path)
    if changed:
        _write(path, text)
    return changed


def unapprove(path: Path, approved_in: str, today: date, *, dry_run: bool) -> list[str]:
    """Set a proposal a showrunner approved in `approved_in` back to waiting for approval.

    A dry run refuses the same issues and writes nothing.
    """
    with _issue_lock(path):
        issue = Issue(path, frontmatter(path))
        if issue.value("status") != "open" or PROPOSAL_TAG not in issue.fields.get("tags", []):
            raise Refusal(f"{path}: not an open proposal")
        if not isinstance(decision_answer(issue), ApprovedDecision):
            raise Refusal(f"{path}: no approval is recorded")
        if approver(issue) == USER:
            raise Refusal(f"{path}: the user approved it; only the user's decline undoes it")
        if issue.value("approved_in") != approved_in:
            raise Refusal(f"{path}: approved in {issue.value('approved_in') or 'no showrunner'}, not in {approved_in}")
        text, changed = set_fields(_read(path), {**dict.fromkeys(APPROVAL_FIELDS, ""), "stage": "backlog",
                                                 "date_modified": _vault_date(today)}, path)
        if not dry_run:
            _write(path, text)
        return changed


REVIEW_FIELDS = ("review_urgency", "review_due", "review_marked")


def set_urgency(path: Path, level: urgency.Urgency, today: date) -> list[str]:
    """Store the proposal's level for the user's review; normal stores nothing."""
    with _issue_lock(path):
        original = _read(path)
        text, removed = remove_fields(original, REVIEW_FIELDS, path)
        text, changed = set_fields(text, urgency.fields(level, "review_"), path)
        if text == original:
            return []
        text, dates = set_fields(text, {"date_modified": _vault_date(today)}, path)
        _write(path, text)
        return [*dict.fromkeys([*removed, *changed]), *dates]


def fields_for(args: argparse.Namespace, today: date) -> dict[str, str]:
    verb = cast(str, args.verb)
    if verb == "start":
        return {"stage": IN_PROGRESS_STAGE}
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


IN_PROGRESS_STAGE = "active"


@dataclass(frozen=True)
class AwaitingApproval:
    """An open proposal the user has not approved: no approval is recorded and its stage is not active."""


@dataclass(frozen=True)
class InProgress:
    """An open proposal approved for implementation, or started, and not yet built."""


ProposalProgress = AwaitingApproval | InProgress


def proposal_progress(issue: Issue) -> ProposalProgress:
    """In progress when the approval is recorded, or when no answer is recorded and the stage is active."""
    match decision_answer(issue):
        case ApprovedDecision():
            return InProgress()
        case UnansweredDecision() if issue.value("stage") == IN_PROGRESS_STAGE:
            return InProgress()
        case _:
            return AwaitingApproval()


def _approved_at_backlog(issue: Issue) -> bool:
    """An open proposal whose approval is recorded and whose stage is still the one it was filed at."""
    return (issue.value("status") == "open" and PROPOSAL_TAG in issue.fields.get("tags", [])
            and isinstance(decision_answer(issue), ApprovedDecision) and issue.value("stage") == "backlog")


@dataclass(frozen=True)
class Moved:
    """The issue's stage is now active, or would be on a dry run."""
    path: Path


@dataclass(frozen=True)
class LeftAlone:
    """Another writer changed the issue after the folder read, and it no longer qualifies."""
    path: Path


@dataclass(frozen=True)
class MoveRefused:
    """The issue file cannot be edited. The run goes on with the other issues."""
    path: Path
    reason: str


MoveOutcome = Moved | LeftAlone | MoveRefused


def _move_one(path: Path, today: date, *, dry_run: bool) -> MoveOutcome:
    """Set stage active on the issue when it still qualifies under the lock.

    A dry run makes the same change, so it refuses the same files, and does not write it.
    """
    with _issue_lock(path):
        if not _approved_at_backlog(Issue(path, frontmatter(path))):
            return LeftAlone(path)
        try:
            text, _ = set_fields(_read(path), {"stage": IN_PROGRESS_STAGE, "date_modified": _vault_date(today)}, path)
            if not dry_run:
                _write(path, text)
        except (ValueError, OSError, UnicodeError) as error:
            return MoveRefused(path, str(error))
        return Moved(path)


def move_approved(folder: Path, today: date, *, dry_run: bool) -> Iterator[MoveOutcome]:
    """Move each open proposal in the folder whose approval is recorded and whose stage is backlog to stage active.

    Gives each outcome as it is written, or as it would be on a dry run. A second run moves none.
    """
    if not folder.is_dir():
        raise Refusal(f"{folder} is not a folder")
    for issue in open_issues(folder, PROPOSAL_TAG):
        if _approved_at_backlog(issue):
            yield _move_one(issue.path, today, dry_run=dry_run)


def area_style(folder: Path, today: date, *, dry_run: bool) -> Iterator[Path]:
    """Set the style area on current and legacy style issues with no area or the unfiled area."""
    if not folder.is_dir():
        raise Refusal(f"{folder} is not a folder")
    for path in sorted(folder.glob("*.md")):
        if not path.is_file():
            continue
        with _issue_lock(path):
            fields = frontmatter(path)
            if not ({"style_rule", "style_rules"} & fields.keys()):
                continue
            text = _read(path)
            lines = text.splitlines(keepends=True)
            span = _key_span(lines, _closing_fence(lines, path), "area")
            if span is not None and re.fullmatch(
                r"area:\s*(?:\"\[\[unfiled\]\]\"|'\[\[unfiled\]\]'|\[\[unfiled\]\])\s*",
                lines[span[0]],
            ) is None:
                continue
            if span is None:
                text, _ = set_fields(text, {"area": f"[[{STYLE_AREA}]]"}, path)
            else:
                start = span[0]
                newline = lines[start][len(lines[start].rstrip("\r\n")):] or "\n"
                lines[start] = _value_line("area", f"[[{STYLE_AREA}]]")[:-1] + newline
                text = "".join(lines)
            text, _ = set_fields(text, {"date_modified": _vault_date(today)}, path)
            if not dry_run:
                _write(path, text)
        yield path


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


class DecisionPathState(Enum):
    AVAILABLE = "available"
    ALREADY_FILED = "already filed"


def _decision_path_state(path: Path, keys: dict[str, str]) -> DecisionPathState:
    """Read the decision key at the path; refuse a different decision."""
    if not path.exists():
        return DecisionPathState.AVAILABLE
    fields = frontmatter(path)
    if any(fields.get(key) != [value] for key, value in keys.items()):
        raise Refusal(f"{path}: different decision key")
    return DecisionPathState.ALREADY_FILED


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
    if _decision_path_state(path, keys) is DecisionPathState.ALREADY_FILED:
        return False
    with _folder_lock(path.parent):
        if _decision_path_state(path, keys) is DecisionPathState.ALREADY_FILED:
            return False
        return _create_numbered(path, template, project, short, proposed_by, tags, keys, count, body, today,
                                proposed_ranking, rule_urls, inherited)


def _create_numbered(path: Path, template: Path, project: str, short: str, proposed_by: str, tags: list[str],
                     keys: dict[str, str], count: int, body: str, today: date, proposed_ranking: str,
                     rule_urls: tuple[str, ...], inherited: dict[str, str]) -> bool:
    """Write the new decision once, with the next free P number."""
    text = template.expanduser().read_text(encoding="utf-8")
    values = {
        "AREA": STYLE_AREA, "PROJECT": project, "SHORT": short, "PROPOSED_BY": proposed_by,
        "DATE": today.isoformat(), "CATEGORY": "refactor", "DEPENDS_ON": "[]",
    }
    text = re.sub(r"\{\{([A-Z_]+)\}\}", lambda match: values.get(match[1], ""), text)
    lists = {"tags": list(dict.fromkeys(["issue", "proposal", *tags]))}
    if rule_urls:
        lists["style_rules"] = sorted(set(rule_urls))
    text, _ = set_list_fields(text, lists, path)
    # An inherited approval starts in progress, by the rule decision_answer reads.
    approved = isinstance(decision_answer(Issue(path, {key: [value] for key, value in inherited.items()})), ApprovedDecision)
    stage = {"stage": IN_PROGRESS_STAGE} if approved else {}
    text, _ = set_fields(text, {"status": "open", "stage": "backlog", **inherited, **stage, **keys,
                                "style_count": str(count), NUMBER_FIELD: f"P{_highest_number(path.parent) + 1}"}, path)
    text = text.rstrip() + f"\n\nProposed ranking: {proposed_ranking}\n\n" + decision_body(body)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, prefix=".decision-", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            _ = stream.write(text)
            stream.flush()
            try:
                os.link(temporary, path)
            except FileExistsError:
                if _decision_path_state(path, keys) is DecisionPathState.ALREADY_FILED:
                    return False
                raise
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


def _main_move_approved(folder: Path, today: date, *, dry_run: bool) -> int:
    moved = 0
    refused = False
    try:
        for outcome in move_approved(folder, today, dry_run=dry_run):
            match outcome:
                case Moved(path=path):
                    moved += 1
                    print(f"move-approved {path.name}: stage {IN_PROGRESS_STAGE}", flush=True)
                case MoveRefused(path=path, reason=reason):
                    refused = True
                    print(f"move-approved {path.name}: refused {reason}", flush=True)
                case LeftAlone():
                    pass
    except (ValueError, OSError, UnicodeError) as error:
        print(f"proposal_issue: {error}", file=sys.stderr)
        return 2
    print(f"move-approved: {moved} moved")
    return 2 if refused else 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    verbs = parser.add_subparsers(dest="verb", required=True)
    approving = verbs.add_parser("approve", help="record the approval; stage active (in progress)")
    _ = approving.add_argument("--by", required=True, choices=APPROVERS,
                             help="user when the user approved, showrunner when a showrunner approved without the user")
    _ = approving.add_argument("--showrunner", required=True, help="the showrunner's session name")
    _ = approving.add_argument("--unit", required=True, help="the unit, as in the production's Units table")
    _ = approving.add_argument("--plan", required=True, help="'<plan path> Phase N–M'")
    unapproving = verbs.add_parser("unapprove", help="set an approved proposal back to waiting for approval")
    _ = unapproving.add_argument("--dry-run", action="store_true", help="print the change and write nothing")
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
    moving = verbs.add_parser("move-approved", help="move each open approved proposal at backlog to stage active")
    _ = moving.add_argument("folder", type=Path)
    _ = moving.add_argument("--dry-run", action="store_true", help="print the moves and write nothing")
    styling = verbs.add_parser("area-style", help="set the area on unfiled current and legacy style issues")
    _ = styling.add_argument("folder", type=Path)
    _ = styling.add_argument("--dry-run", action="store_true", help="print the changes and write nothing")
    answer = verbs.add_parser("decline-answer", help="store a decision's declined answer")
    _ = answer.add_argument("--words", required=True)
    linking = verbs.add_parser("rules", help="set the rule links")
    _ = linking.add_argument("--rules", required=True)
    ranking = verbs.add_parser("urgency", help="set where it goes among the proposals waiting for the user")
    _ = ranking.add_argument("--level", required=True, choices=urgency.LEVELS)
    _ = ranking.add_argument("--due", default="", help="ISO date or time; only with soon")
    important = verbs.add_parser("mark-important", help="the user called it important: it goes first")
    stars = verbs.add_parser("stars", help="set the five ranking values")
    numbering = verbs.add_parser("number", help="give each proposal without one its P number")
    _ = numbering.add_argument("issues", type=Path, nargs="+")
    _ = stars.add_argument("--goal", required=True, help="e.g. '1 - Ship Hana'")
    for name in ("alignment", "impact", "urgency", "effort"):
        _ = stars.add_argument(f"--{name}", required=True, help="1 to 5")
    for verb in (approving, unapproving, verbs.choices["start"], close, stars, linking, reopening, answer, ranking, important):
        _ = verb.add_argument("issue", type=Path)
    args = parser.parse_args(argv)
    verb = cast(str, args.verb)
    today = datetime.now(user_zone()).date()
    if verb == "number":
        try:
            print("\n".join(number([path.expanduser() for path in cast(list[Path], args.issues)], today)))
        except (ValueError, OSError, UnicodeError) as error:
            print(f"proposal_issue: {error}", file=sys.stderr)
            return 2
        return 0
    if verb == "move-approved":
        return _main_move_approved(cast(Path, args.folder).expanduser(), today, dry_run=cast(bool, args.dry_run))
    if verb == "area-style":
        try:
            for changed_path in area_style(cast(Path, args.folder).expanduser(), today, dry_run=cast(bool, args.dry_run)):
                print(f"area-style {changed_path.name}: area [[{STYLE_AREA}]]", flush=True)
        except (ValueError, OSError, UnicodeError) as error:
            print(f"proposal_issue: {error}", file=sys.stderr)
            return 2
        return 0
    # A plan's Issues line writes `~/rust/hanadocs/issues/<name>.md`, quoted since names hold spaces.
    path = cast(Path, args.issue).expanduser()
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
        elif verb == "approve":
            changed = approve(path, {name: cast(str, getattr(args, name)) for name in ("showrunner", "unit", "plan")},
                              cast(str, args.by), calling_showrunner(), today)
        elif verb == "unapprove":
            changed = unapprove(path, calling_showrunner(), today, dry_run=cast(bool, args.dry_run))
        elif verb == "decline-answer":
            changed = edit(path, {"style_answer": "declined", "style_answer_words": cast(str, args.words)}, today)
        elif verb in ("urgency", "mark-important"):
            now = datetime.now(user_zone()).replace(tzinfo=None)
            level = (urgency.mark(now) if verb == "mark-important" or cast(str, args.level) == urgency.USER
                     else urgency.read(cast(str, args.level), cast(str, args.due)))
            changed = set_urgency(path, level, today)
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
