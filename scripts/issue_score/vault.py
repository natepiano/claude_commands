"""Read issue text and confirmed labels without writing to the vault."""
from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import cast

from .questions import BODY_CHARACTER_LIMIT, GOAL_LABELS, IssueState, PROJECT_ABOUT

VAULT_ROOT = Path.home() / "rust/hanadocs"
ISSUES_DIR = VAULT_ROOT / "issues"
AREA_DEFINITIONS_PATH = VAULT_ROOT / "conf/issue-areas.md"
RATING_FIELDS = ("backlog_alignment", "backlog_impact", "backlog_urgency", "backlog_effort")
LABEL_FIELDS = ("backlog_goal", *RATING_FIELDS, "category", "area", "backlog_score")
WIKILINK = re.compile(r"\[\[([^\]\n]+)\]\]")
PROPOSED_RANKING = re.compile(r"^\s*Proposed ranking:.*$", re.MULTILINE)


@dataclass(frozen=True)
class Issue:
    path: Path
    title: str
    project: str
    body: str
    labels: dict[str, str]
    is_style: bool = False


@dataclass(frozen=True)
class SkippedIssue:
    path: Path
    reason: str


@dataclass(frozen=True)
class RatedIssues:
    issues: list[Issue]
    skipped: list[SkippedIssue]


@dataclass(frozen=True)
class VaultDocument:
    """An issue's source text before selecting model state or confirmed labels."""
    path: Path
    fields: dict[str, list[str]]
    body: str

    def value(self, name: str) -> str:
        return " | ".join(self.fields.get(name, []))

    @property
    def is_style(self) -> bool:
        return bool(self.value("style_rule") or self.value("style_rules"))

    def issue(self) -> Issue:
        return Issue(self.path, self.value("title") or self.path.stem,
                     plain(self.value("project")), self.body,
                     {key: plain(self.value(key)) for key in LABEL_FIELDS}, self.is_style)


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        try:
            decoded = cast(object, json.loads(value))
            if isinstance(decoded, str):
                return decoded
        except ValueError:
            return value[1:-1]
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1].replace("''", "'")
    return value


def plain(value: str) -> str:
    return WIKILINK.sub(lambda match: match[1].split("|", 1)[-1], _unquote(value)).strip()


def clean_body(body: str) -> str:
    cleaned = WIKILINK.sub(lambda match: match[1].split("|", 1)[-1], body)
    if len(cleaned) <= BODY_CHARACTER_LIMIT:
        return cleaned
    prefix = cleaned[:BODY_CHARACTER_LIMIT]
    headings = list(re.finditer(r"(?m)^#{1,6}\s", prefix))
    # If the only heading opens the document, keep useful text up to the cap.
    cut = headings[-1].start() if headings and headings[-1].start() > 0 else BODY_CHARACTER_LIMIT
    return prefix[:cut].rstrip()


def normalize_area(area: str) -> str:
    return plain(area).strip("\"'").strip().casefold()


def load_area_definitions(path: Path = AREA_DEFINITIONS_PATH) -> dict[str, str]:
    """Require a readable area table; missing area rows retain name-only criteria."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        raise ValueError(f"{path}: area definitions file not found") from None
    definitions: dict[str, str] = {}
    in_table = False
    for line in lines:
        cells = [cell.strip().replace(r"\|", "|")
                 for cell in re.split(r"(?<!\\)\|", line.strip().strip("|"))]
        if [cell.casefold() for cell in cells] == ["area", "definition", "issues"]:
            in_table = True
            continue
        if not in_table:
            continue
        if len(cells) != 3:
            break
        if all(re.fullmatch(r":?-+:?", cell) for cell in cells):
            continue
        area = normalize_area(cells[0])
        if area and cells[1]:
            definitions[area] = cells[1]
    if not definitions:
        raise ValueError(f"{path}: no area definitions found in area | definition | issues table")
    return definitions


def _frontmatter(path: Path) -> VaultDocument:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return VaultDocument(path, {}, text)
    closing = next((i for i, line in enumerate(lines[1:], 1) if line.strip() == "---"), 0)
    if not closing:
        raise ValueError(f"{path}: unterminated frontmatter")
    fields: dict[str, list[str]] = {}
    field_name = ""
    for line in lines[1:closing]:
        scalar = re.match(r"^([\w-]+):\s*(.*?)\s*$", line)
        if scalar:
            field_name, value = scalar.groups()
            fields[field_name] = []
            if value and value not in ("null", "~", "[]"):
                if value.startswith("[") and not value.startswith("[["):
                    # Vault lists contain simple quoted scalars, including wikilinks.
                    parts: list[str] = re.compile(r'''"(?:\\.|[^"\\])*"|'(?:''|[^'])*'|[^,\[\]]+''', re.MULTILINE).findall(value[1:-1])
                    fields[field_name] = [_unquote(part) for part in parts if part.strip()]
                else:
                    fields[field_name] = [_unquote(value)]
            continue
        item = re.match(r"^\s*-\s+(.+?)\s*$", line)
        if item and field_name:
            fields[field_name].append(_unquote(item[1]))
    return VaultDocument(path, fields, "".join(lines[closing + 1:]))


def _documents(issues_dir: Path) -> list[VaultDocument]:
    return [_frontmatter(path) for path in sorted(issues_dir.glob("*.md")) if path.is_file()]


def load_open_issues(issues_dir: Path = ISSUES_DIR) -> list[Issue]:
    return [document.issue() for document in _documents(issues_dir)
            if plain(document.value("status")).casefold() == "open" and not document.is_style]


def _style_formula(document: VaultDocument, project_goals: dict[str, str]) -> dict[str, str]:
    count_text = document.value("style_count")
    count_match = re.search(r"(?m)^Findings:\s*(\d+)", document.body)
    if count_text.isdigit():
        count = int(count_text)
    elif count_match:
        count = int(count_match[1])
    else:
        count = len(document.fields.get("style_findings", []))
    kind_match = re.search(r"(?m)^(?:Kind:\s*|#{1,6} Style )(rename|type|restructure)\b", document.body)
    kind = document.value("style_kind") or (kind_match[1] if kind_match else "")
    # Old proposals used rename as their category; new decisions state Kind.
    rename = kind == "rename" or plain(document.value("category")) == "rename"
    return {
        "backlog_goal": project_goals.get(plain(document.value("project")), "1 - Ship Hana"),
        "backlog_alignment": "⭐⭐", "backlog_impact": "⭐" if rename else "⭐⭐",
        "backlog_urgency": "⭐", "backlog_effort": "⭐" * (1 if count <= 3 else 2 if count <= 15 else 3),
    }


def load_rated_issues(issues_dir: Path = ISSUES_DIR) -> RatedIssues:
    documents = _documents(issues_dir)
    goals: dict[str, Counter[str]] = {}
    for document in documents:
        goal = plain(document.value("backlog_goal"))
        if (plain(document.value("status")).casefold() == "open" and not document.is_style
                and "style" not in document.fields.get("tags", []) and goal in GOAL_LABELS.values()):
            goals.setdefault(plain(document.value("project")), Counter())[goal] += 1
    project_goals = {project: min(counts, key=lambda goal: (-counts[goal], goal)) for project, counts in goals.items()}
    issues: list[Issue] = []
    skipped: list[SkippedIssue] = []
    for document in documents:
        issue = document.issue()
        reason = ""
        if plain(document.value("status")).casefold() != "open":
            reason = "not open"
        elif PROPOSED_RANKING.search(document.body):
            reason = "unconfirmed Proposed ranking"
        elif (issue.labels["backlog_goal"] not in GOAL_LABELS.values()
              or any(not re.fullmatch("⭐{1,5}", issue.labels[field]) for field in RATING_FIELDS)):
            reason = "missing or invalid ratings"
        elif document.is_style and all(issue.labels[key] == value for key, value in _style_formula(document, project_goals).items()):
            reason = "style ratings match proposal formula"
        if reason:
            skipped.append(SkippedIssue(document.path, reason))
        else:
            issues.append(issue)
    return RatedIssues(issues, skipped)


def area_vocabulary(issues: Iterable[Issue]) -> list[str]:
    return sorted({area for issue in issues if (area := normalize_area(issue.labels.get("area", "")))})


def build_state(issue: Issue) -> IssueState:
    """Only issue content is visible to the model, never current labels."""
    projects = issue.project.split(" | ")
    if any(project not in PROJECT_ABOUT for project in projects):
        raise ValueError(f"Unknown project context: {issue.project}")
    about = " ".join(PROJECT_ABOUT[project] for project in projects)
    return {"issue": {"title": plain(issue.title), "project": issue.project,
                      "project_about": about,
                      "body": clean_body(PROPOSED_RANKING.sub("", issue.body))}}
