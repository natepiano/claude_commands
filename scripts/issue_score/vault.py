"""Read issue text and confirmed labels without writing to the vault."""
from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import cast
from urllib.parse import unquote, urlsplit

from scripts.feature_log import feature_log

from .questions import BODY_CHARACTER_LIMIT, GOAL_LABELS, IssueState, PROJECT_ABOUT, StyleState

VAULT_ROOT = Path.home() / "rust/hanadocs"
ISSUES_DIR = VAULT_ROOT / "issues"
AREA_DEFINITIONS_PATH = VAULT_ROOT / "conf/issue-areas.md"
PROJECT_BASES_DIR = VAULT_ROOT / "home-base"
STYLE_RULES_DIRS = (Path.home() / "rust/nate_style", Path(__file__).resolve().parents[2] / "docs/style")
RATING_FIELDS = ("backlog_alignment", "backlog_impact", "backlog_urgency", "backlog_effort")
LABEL_FIELDS = ("backlog_goal", *RATING_FIELDS, "category", "area", "backlog_score")
WIKILINK = re.compile(r"\[\[([^\]\n]+)\]\]")
PROPOSED_RANKING = re.compile(r"^\s*Proposed ranking:.*$", re.MULTILINE)
FRONTMATTER_FIELD = re.compile(r'''^(?!-(?:\s|$))("(?:\\.|[^"\\])*"|'(?:''|[^'])*'|[^\s"'#][^:]*):\s*(.*?)\s*$''')


@dataclass(frozen=True)
class RuleSummary:
    name: str
    summary: str


@dataclass(frozen=True)
class MissingStyleRule:
    name: str


@dataclass(frozen=True)
class StyleInputs:
    rules: tuple[RuleSummary | MissingStyleRule, ...]
    style_crate: str
    finding_count: int

    def model_state(self) -> StyleState:
        return {"style_rules": [
                    {"name": rule.name, "summary": rule.summary, "status": "available"}
                    if isinstance(rule, RuleSummary) else {"name": rule.name, "status": "missing"}
                    for rule in self.rules],
                "style_crate": self.style_crate, "finding_count": self.finding_count}


@dataclass(frozen=True)
class NoStyleInputs:
    pass


@dataclass(frozen=True)
class Issue:
    path: Path
    title: str
    project: str
    body: str
    labels: dict[str, str]
    is_style: bool = False
    style_inputs: StyleInputs | NoStyleInputs = NoStyleInputs()


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
    """Require a two-column area table; missing rows retain name-only criteria."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        raise ValueError(f"{path}: area definitions file not found") from None
    definitions: dict[str, str] = {}
    columns = 0
    for line in lines:
        cells = [cell.strip().replace(r"\|", "|")
                 for cell in re.split(r"(?<!\\)\|", line.strip().strip("|"))]
        if [cell.casefold() for cell in cells] == ["area", "definition"]:
            columns = len(cells)
            continue
        if not columns:
            continue
        if len(cells) != columns:
            break
        if all(re.fullmatch(r":?-+:?", cell) for cell in cells):
            continue
        area = normalize_area(cells[0])
        if area and cells[1]:
            definitions[area] = cells[1]
    if not definitions:
        raise ValueError(f"{path}: no area definitions found in area | definition table")
    return definitions


def load_project_names(base_dir: Path = PROJECT_BASES_DIR) -> dict[str, str]:
    """Read each shorthand's project filter from the same bases `/issue` uses."""
    names: dict[str, str] = {}
    for path in sorted(base_dir.glob("issues - *.base")):
        matches = set(re.findall(r'''project\.contains\(link\(["']([^"']+)["']\)\)''', path.read_text(encoding="utf-8")))
        if len(matches) != 1:
            raise ValueError(f"{path}: expected one project.contains(link(...)) project")
        names[path.stem.removeprefix("issues - ")] = matches.pop()
    return names


def _frontmatter(path: Path) -> VaultDocument:
    return parse_document(path, path.read_text(encoding="utf-8"))


def parse_document(path: Path, text: str) -> VaultDocument:
    """Parse a supplied snapshot so validation and editing can share its bytes."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return VaultDocument(path, {}, text)
    closing = next((i for i, line in enumerate(lines[1:], 1) if line.strip() == "---"), 0)
    if not closing:
        raise ValueError(f"{path}: unterminated frontmatter")
    fields: dict[str, list[str]] = {}
    field_name = ""
    for line in lines[1:closing]:
        scalar = FRONTMATTER_FIELD.match(line)
        if scalar:
            field_name, value = _unquote(scalar[1]), scalar[2]
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


def _finding_count(document: VaultDocument) -> int:
    count_text = document.value("style_count")
    count_match = re.search(r"(?m)^Findings:\s*(\d+)", document.body)
    if count_text.isdigit():
        return int(count_text)
    if count_match:
        return int(count_match[1])
    return len(document.fields.get("style_findings", []))


def project_rule_roots(project: str) -> tuple[Path, ...]:
    """Reuse the feature log's repository and member-crate checkout registry."""
    return tuple(root for name, spec in feature_log.PROJECTS.items()
                 if project == name or project in spec.crates for root in spec.checkouts)


def _rule_summary(reference: str, rules_dirs: tuple[Path, ...]) -> RuleSummary | MissingStyleRule:
    relative = reference.split("#", 1)[0].removeprefix("global:").removeprefix("local:")
    url = urlsplit(relative)
    parts = url.path.lstrip("/").split("/", 4)
    if url.netloc == "github.com" and len(parts) == 5 and parts[2] == "blob":
        relative = unquote(parts[4])
    relative = plain(relative).removesuffix(".md") + ".md"
    candidates = [root / relative for root in rules_dirs] + [root / Path(relative).name for root in rules_dirs]
    for path in candidates:
        if not path.is_file():
            continue
        document = _frontmatter(path)
        summary = document.value("summary") or document.value("description")
        if not summary:
            paragraphs = re.split(r"\n\s*\n", document.body.strip())
            summary = next((paragraph for paragraph in paragraphs
                            if paragraph and not paragraph.startswith(("#", "```", "---"))), "")
        if summary:
            return RuleSummary(reference, " ".join(plain(summary).split()))
    return MissingStyleRule(reference)


def scoring_issue(document: VaultDocument, *, rules_dirs: tuple[Path, ...] = STYLE_RULES_DIRS) -> Issue:
    """Build scoring inputs from one issue snapshot, including its owning rules."""
    issue = document.issue()
    if not document.is_style:
        return issue
    references = list(dict.fromkeys(document.fields.get("style_rule", []) + document.fields.get("style_rules", [])))
    roots = project_rule_roots(issue.project)
    inputs = StyleInputs(tuple(_rule_summary(reference, rules_dirs if reference.startswith("global:") else roots + rules_dirs)
                               for reference in references),
                         plain(document.value("style_crate")), _finding_count(document))
    return Issue(issue.path, issue.title, issue.project, issue.body, issue.labels, True, inputs)


def load_scoring_issues(issues_dir: Path = ISSUES_DIR, *,
                        rules_dirs: tuple[Path, ...] = STYLE_RULES_DIRS) -> list[Issue]:
    """Include open style proposals with explicit available or missing rule state."""
    issues = load_open_issues(issues_dir)
    for document in _documents(issues_dir):
        if not document.is_style or plain(document.value("status")).casefold() != "open":
            continue
        issues.append(scoring_issue(document, rules_dirs=rules_dirs))
    return sorted(issues, key=lambda issue: issue.path)


def _style_formula(document: VaultDocument, project_goals: dict[str, str]) -> dict[str, str]:
    count = _finding_count(document)
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
    content: IssueState = {"issue": {"title": plain(issue.title), "project": issue.project,
                                   "project_about": about,
                                   "body": clean_body(PROPOSED_RANKING.sub("", issue.body))}}
    if isinstance(issue.style_inputs, StyleInputs):
        style = issue.style_inputs.model_state()
        content["issue"]["body"] = ""
        content["issue"]["style_rules"] = style["style_rules"]
        content["issue"]["style_crate"] = style["style_crate"]
        content["issue"]["finding_count"] = style["finding_count"]
    return content
