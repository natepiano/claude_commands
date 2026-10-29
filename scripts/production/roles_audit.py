#!/usr/bin/env python3
"""Read role names, definitions, production names and message coverage without writes.

Names are case-insensitive literal phrases with word boundaries. Script prose is
static: Python string literals (excluding docstrings, keys, paths and machine
tokens), quoted shell output and cat heredocs. Scripts and strings are not expanded.
"""
from __future__ import annotations

import argparse
import ast
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from functools import partial
from io import StringIO
import json
from pathlib import Path
import re
import shlex
import sys
import tomllib
import tokenize
from typing import Literal, cast

import production_units
import showrunners

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from code_kind import is_test_code  # noqa: E402
from user_zone import user_zone, user_zone_name  # noqa: E402

TermKind = Literal["role", "document", "step", "record"]
NOT_RECORDED = "not recorded"
LIVE_SET = "commands/; docs/ except docs/plans/; skills/; CLAUDE.md; scripts/ strings"
ROLE_SOURCE = "scripts/message/roster.py --json --here (current roles, not historical roles)"


@dataclass(frozen=True)
class NameInflection:
    name: str
    forms: tuple[str, ...]


@dataclass(frozen=True)
class KeptContext:
    path: str
    text: str
    reason: str


@dataclass(frozen=True)
class Term:
    name: str
    kind: TermKind
    defined_in: tuple[str, ...]
    other_names: tuple[str, ...]
    kept: tuple[str, ...] = ()
    other_meaning_files: tuple[str, ...] = ()
    inflected_forms: tuple[NameInflection, ...] = ()
    retired: tuple[str, ...] = ()
    kept_contexts: tuple[KeptContext, ...] = ()


@dataclass(frozen=True)
class LiveFile:
    path: str
    text: str
    source_lines: tuple[int, ...] = ()  # Physical source line for each prose character.


@dataclass(frozen=True)
class SourceProse:
    text: str
    source_lines: tuple[int, ...]  # Physical source line for each prose character.


@dataclass(frozen=True)
class NameCount:
    term: str
    name: str
    form: str
    hits: int
    files: tuple[str, ...]


@dataclass(frozen=True)
class DefinitionFinding:
    term: str
    status: str
    files: tuple[str, ...]


@dataclass(frozen=True)
class RetiredUse:
    path: str
    line: int
    name: str
    replacement: str


@dataclass(frozen=True)
class ProductionNames:
    slug: str
    doc: str
    showrunner: str
    units: tuple[str, ...]


@dataclass(frozen=True)
class ProductionNameFinding:
    kind: str
    name: str
    productions: tuple[str, ...]
    docs: tuple[str, ...]


@dataclass(frozen=True)
class AuditWindow:
    since: datetime
    until: datetime

    def label(self) -> str:
        zone = user_zone()
        return (f"[{self.since.astimezone(zone).isoformat()}, "
                f"{self.until.astimezone(zone).isoformat()}) {user_zone_name()}")


@dataclass(frozen=True)
class MessageRecord:
    at: datetime
    machine: str
    sender: str
    recipient: str
    outcome: str
    sender_role: str = NOT_RECORDED
    sender_production: str = NOT_RECORDED
    recipient_role: str = NOT_RECORDED
    recipient_production: str = NOT_RECORDED


@dataclass(frozen=True)
class MessageSource:
    records: tuple[MessageRecord, ...]
    invalid_lines: int
    status: str


@dataclass(frozen=True)
class SenderContacts:
    sender: str
    to_user: int
    to_other_showrunner: int
    role_unknown: int
    production_unknown: int
    n: int


@dataclass(frozen=True)
class WrongPartyCounts:
    senders: tuple[SenderContacts, ...]
    window_records: int
    sent_records: int
    other_machine_records: int
    machine_unknown: int


def string_list(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{field} must be an array of strings")
    items = cast(list[object], value)
    if not all(isinstance(item, str) and item.strip() for item in items):
        raise ValueError(f"{field} must contain nonempty strings")
    return tuple(cast(str, item) for item in items)


def parse_terms(text: str) -> tuple[Term, ...]:
    data = cast(dict[str, object], tomllib.loads(text))
    rows = data.get("terms")
    if not isinstance(rows, list):
        raise ValueError("term list needs [[terms]] tables")
    terms: list[Term] = []
    for value in cast(list[object], rows):
        if not isinstance(value, dict):
            raise ValueError("each term must be a table")
        row = cast(dict[str, object], value)
        name, kind, definition = row.get("name"), row.get("kind"), row.get("defined_in")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("term name must be a nonempty string")
        if kind not in ("role", "document", "step", "record"):
            raise ValueError(f"{name}: invalid kind")
        if not isinstance(definition, str) or not definition.strip():
            raise ValueError(f"{name}: defined_in must name a file and section, or none")
        definitions = tuple(part.strip() for part in definition.split(";") if part.strip() != "none")
        other_names = string_list(row.get("other_names"), f"{name}.other_names")
        forms = row.get("inflected_forms", {})
        if not isinstance(forms, dict):
            raise ValueError(f"{name}.inflected_forms must be a table")
        inflections: list[NameInflection] = []
        for base, spellings in cast(dict[str, object], forms).items():
            if base not in (name, *other_names):
                raise ValueError(f"{name}: inflected name {base} is not declared")
            inflections.append(NameInflection(base, string_list(spellings, f"{name}.{base}")))
        contexts = row.get("kept_contexts", [])
        if not isinstance(contexts, list):
            raise ValueError(f"{name}.kept_contexts must be an array of tables")
        kept_contexts: list[KeptContext] = []
        for context in cast(list[object], contexts):
            if not isinstance(context, dict):
                raise ValueError(f"{name}: kept context must be a table")
            fields = cast(dict[str, object], context)
            path, snippet, reason = fields.get("path"), fields.get("text"), fields.get("reason")
            if not all(isinstance(field, str) and field.strip() for field in (path, snippet, reason)):
                raise ValueError(f"{name}: kept context needs path, text and reason")
            kept_contexts.append(KeptContext(cast(str, path), cast(str, snippet), cast(str, reason)))
        retired = string_list(row.get("retired", []), f"{name}.retired")
        declared = {name, *other_names, *(form for item in inflections for form in item.forms)}
        if any(old not in declared for old in retired):
            raise ValueError(f"{name}: retired names must be declared forms")
        terms.append(Term(name, kind, definitions, other_names,
                          string_list(row.get("kept", []), f"{name}.kept"),
                          string_list(row.get("other_meaning_files", []), f"{name}.other_meaning_files"),
                          tuple(inflections), retired, tuple(kept_contexts)))
    names = [term.name.casefold() for term in terms]
    if len(set(names)) != len(names):
        raise ValueError("defined term names must be unique")
    return tuple(terms)


def name_pattern(name: str) -> re.Pattern[str]:
    return re.compile(r"(?<![\w-])" + re.escape(name) + r"(?![\w-])", re.IGNORECASE)


def term_forms(term: Term) -> tuple[tuple[str, str], ...]:
    result: list[tuple[str, str]] = []
    for kind, name in (("defined", term.name), *(("other", other) for other in term.other_names)):
        result.append((kind, name))
        for inflection in term.inflected_forms:
            if inflection.name == name:
                result.extend((kind, form) for form in inflection.forms)
    names = [name.casefold() for _, name in result]
    if len(set(names)) != len(names):
        raise ValueError(f"{term.name}: forms must be unique")
    return tuple(result)


def forms_pattern(names: tuple[str, ...]) -> re.Pattern[str]:
    alternatives = "|".join(re.escape(name) for name in sorted(names, key=len, reverse=True))
    return re.compile(r"(?<![\w-])(?:" + alternatives + r")(?![\w-])", re.IGNORECASE)


def without_paths(text: str, kept: tuple[str, ...]) -> str:
    # Slash commands remain names unless the data marks them kept.
    def mask(match: re.Match[str]) -> str:
        token = match.group()
        return token if re.fullmatch(r"/[\w-]+:[\w-]+", token) else " " * len(token)
    text = re.sub(r"(?<![\w])(?:~?/|(?:[\w.$~-]+/)+)[\w./:$~{}-]*", mask, text)
    text = re.sub(r"\b[\w.-]+\.(?:md|py|sh|toml|jsonl?|txt)\b", lambda match: " " * len(match.group()), text)
    for name in sorted(kept, key=len, reverse=True):
        text = re.sub(re.escape(name), lambda match: " " * len(match.group()), text, flags=re.IGNORECASE)
    return text


def measure_names(terms: tuple[Term, ...], files: tuple[LiveFile, ...]) -> tuple[NameCount, ...]:
    kept = tuple(name for term in terms for name in term.kept)
    texts = [(file.path, without_paths(file.text, kept)) for file in files]
    result: list[NameCount] = []
    for term in terms:
        for form, name in term_forms(term):
            pattern = name_pattern(name)
            counts = [(path, len(pattern.findall(text))) for path, text in texts]
            result.append(NameCount(term.name, name, form, sum(n for _, n in counts),
                                    tuple(path for path, n in counts if n)))
    return tuple(result)


def check_retired(terms: tuple[Term, ...], files: tuple[LiveFile, ...]) -> tuple[RetiredUse, ...]:
    """Check extracted source prose only; a kept context applies once in its named file."""
    findings: list[RetiredUse] = []
    for term in terms:
        if not term.retired:
            continue
        retired = {name.casefold() for name in term.retired}
        pattern = forms_pattern(tuple(name for _, name in term_forms(term)))
        for file in files:
            text = file.text
            for context in term.kept_contexts:
                if context.path == file.path:
                    # Preserve line numbers for the source-only diagnostic.
                    blank = "".join("\n" if char == "\n" else " " for char in context.text)
                    text = text.replace(context.text, blank, 1)
            offset = 0
            for number, line in enumerate(text.split("\n"), 1):
                for match in pattern.finditer(without_paths(line, term.kept)):
                    if match.group().casefold() in retired:
                        source_line = file.source_lines[offset + match.start()] if file.source_lines else number
                        findings.append(RetiredUse(file.path, source_line, match.group(), term.name))
                offset += len(line) + 1
    return tuple(findings)


def check_sources(root: Path, *, full_extraction: bool = False) -> tuple[str, ...]:
    """Read repository sources, without production records, memory or message logs."""
    terms = parse_terms((root / "scripts/production/role_terms.toml").read_text(encoding="utf-8"))
    retired_forms = () if full_extraction else tuple(name for term in terms for name in term.retired)
    files, issues = read_live_files(root, retired_forms=retired_forms)
    uses = check_retired(terms, files)
    return (*issues, *(f"{use.path}:{use.line}: retired {use.name!r}; use {use.replacement!r}" for use in uses))


def measure_definitions(terms: tuple[Term, ...], files: tuple[LiveFile, ...]) -> tuple[DefinitionFinding, ...]:
    result: list[DefinitionFinding] = []
    for term in terms:
        sources = {source.split("#", 1)[0].strip() for source in term.defined_in}
        pattern = re.compile(r"^\s*(?:[-*]\s+)?\*\*" + re.escape(term.name)
                             + r"\*\*\s*(?:—|–|:| - )", re.IGNORECASE | re.MULTILINE)
        sources.update(file.path for file in files
                       if file.path not in term.other_meaning_files and pattern.search(file.text))
        if not term.defined_in:
            result.append(DefinitionFinding(term.name, "no defined_in", tuple(sorted(sources))))
        if len(sources) > 1:
            result.append(DefinitionFinding(term.name, "multiple files", tuple(sorted(sources))))
    return tuple(result)


def parse_production(slug: str, path: str, text: str) -> ProductionNames:
    lines = text.splitlines()
    units = tuple(production_units.cell_value(row.get("Unit", ""))
                  for row in production_units.unit_table(lines))
    prefix = "- **Showrunner session:** "
    session = next((production_units.cell_value(line[len(prefix):]) for line in lines
                    if line.startswith(prefix)), slug)
    return ProductionNames(slug, path, session, units)


def measure_production_names(productions: tuple[ProductionNames, ...]) -> tuple[ProductionNameFinding, ...]:
    result: list[ProductionNameFinding] = []
    uses: dict[str, set[int]] = defaultdict(set)
    for index, production in enumerate(productions):
        for unit in production.units:
            if re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*-unit", unit) is None:
                result.append(ProductionNameFinding("unit format", unit, (production.slug,), (production.doc,)))
        for name in {production.slug, production.showrunner, *production.units}:
            uses[name].add(index)
    for name, indices in sorted(uses.items()):
        if len(indices) > 1:
            owners = [productions[index] for index in sorted(indices)]
            result.append(ProductionNameFinding("shared name", name, tuple(p.slug for p in owners),
                                                tuple(p.doc for p in owners)))
    return tuple(result)


def parse_messages(text: str) -> MessageSource:
    records: list[MessageRecord] = []
    invalid = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            raw = cast(object, json.loads(line))
            if not isinstance(raw, dict):
                raise ValueError("message must be an object")
            row = cast(dict[str, object], raw)
            fields = ("time", "from", "to", "outcome")
            if not all(isinstance(row.get(key), str) and row[key] for key in fields):
                raise ValueError("message lacks time, from, to or outcome")
            at = datetime.fromisoformat(cast(str, row["time"]).replace("Z", "+00:00"))
            if at.tzinfo is None:
                raise ValueError("message time needs a UTC offset")
            def field(key: str) -> str:
                value = row.get(key)
                return value if isinstance(value, str) and value else NOT_RECORDED
            records.append(MessageRecord(at, field("machine"), field("from"), field("to"), field("outcome"),
                                         field("from_role"), field("from_production"),
                                         field("to_role"), field("to_production")))
        except (ValueError, TypeError):
            invalid += 1
    return MessageSource(tuple(records), invalid, "read")


def measure_wrong_party(records: tuple[MessageRecord, ...], window: AuditWindow) -> WrongPartyCounts:
    selected = [record for record in records if window.since <= record.at < window.until]
    local = [record for record in selected if record.machine == "natedev" and record.outcome == "sent"]
    groups: dict[str, list[MessageRecord]] = defaultdict(list)
    for record in local:
        groups[record.sender].append(record)
    counts: list[SenderContacts] = []
    for sender, messages in sorted(groups.items()):
        user = other = unknown_role = unknown_production = 0
        for record in messages:
            role = record.sender_role.replace("_", " ").replace("-", " ").casefold()
            if role == NOT_RECORDED:
                unknown_role += 1
            elif role == "unit director":
                if record.recipient == "user" or record.recipient_role.casefold() in ("user", "executive producer"):
                    user += 1
                elif record.recipient_role.casefold() == "showrunner":
                    if NOT_RECORDED in (record.sender_production, record.recipient_production):
                        unknown_production += 1
                    elif record.sender_production != record.recipient_production:
                        other += 1
                elif record.recipient_role == NOT_RECORDED:
                    unknown_role += 1
        counts.append(SenderContacts(sender, user, other, unknown_role, unknown_production, len(messages)))
    return WrongPartyCounts(tuple(counts), len(selected), len(local),
                            sum(record.machine not in ("natedev", NOT_RECORDED) for record in selected),
                            sum(record.machine == NOT_RECORDED for record in selected))


def python_literal_lines(node: ast.Constant, source_lines: tuple[bytes, ...], value: str) -> tuple[int, ...]:
    """Keep each contributing literal's physical line using the file's UTF-8 lines."""
    if node.end_lineno == node.lineno:
        return (node.lineno,) * len(value)
    segment = None
    if node.end_lineno is not None and node.end_col_offset is not None:
        first, last = node.lineno - 1, node.end_lineno - 1
        segment = b"".join((source_lines[first][node.col_offset:],
                            *source_lines[first + 1:last],
                            source_lines[last][:node.end_col_offset])).decode()
    parts: list[str] = []
    lines: list[int] = []
    if segment is not None:
        # Keep continuation indentation inside parentheses when tokenizing the span.
        try:
            tokens = tuple(tokenize.generate_tokens(StringIO("(" + segment + ")").readline))
        except (tokenize.TokenError, SyntaxError):
            # Formatted-string constants can span fragments that are not Python alone.
            tokens = ()
        for token in tokens:
            if token.type != tokenize.STRING:
                continue
            try:
                part = cast(object, ast.literal_eval(token.string))
            except (SyntaxError, ValueError):
                continue
            if not isinstance(part, str):
                continue
            parts.append(part)
            line = node.lineno + token.start[0] - 1
            end_line = node.lineno + token.end[0] - 1
            for character in part:
                lines.append(line)
                if character == "\n":
                    line = min(line + 1, end_line)
    if "".join(parts) == value:
        return tuple(lines)
    # AST constants inside formatted strings need the enclosing literal's span.
    line = node.lineno
    end_line = node.end_lineno if node.end_lineno is not None else line
    lines = []
    for character in value:
        lines.append(line)
        if character == "\n":
            line = min(line + 1, end_line)
    return tuple(lines)


def python_prose(text: str) -> SourceProse:
    tree = ast.parse(text)
    # Match the parser's newline rules; form feeds stay within a physical line.
    encoded_lines = tuple(match.group().encode() for match in re.finditer(r".*?(?:\r\n|\r|\n|$)", text))
    excluded: set[int] = set()
    outputs: set[int] = set()
    scopes: dict[int, ast.AST] = {}
    parents: dict[int, ast.AST] = {}
    values: dict[tuple[int, str], list[ast.AST]] = defaultdict(list)

    def index_scope(node: ast.AST, scope: ast.AST) -> None:
        scopes[id(node)] = scope
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            parents[id(node)] = parents[id(scope)] if isinstance(scope, ast.ClassDef) else scope
            arguments = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
            if node.args.vararg is not None:
                arguments.append(node.args.vararg)
            if node.args.kwarg is not None:
                arguments.append(node.args.kwarg)
            for argument in arguments:
                values[(id(node), argument.arg)] = []
            for default in (*node.args.defaults, *node.args.kw_defaults):
                if default is not None:
                    index_scope(default, scope)
            body = [node.body] if isinstance(node, ast.Lambda) else node.body
            for statement in body:
                index_scope(statement, node)
            if not isinstance(node, ast.Lambda):
                for decorator in node.decorator_list:
                    index_scope(decorator, scope)
            return
        if isinstance(node, ast.ClassDef):
            parents[id(node)] = scope
            for statement in node.body:
                index_scope(statement, node)
            for expression in (*node.bases, *node.decorator_list):
                index_scope(expression, scope)
            return
        for child in ast.iter_child_nodes(node):
            index_scope(child, scope)

    index_scope(tree, tree)
    nodes = tuple(ast.walk(tree))
    for node in nodes:
        scope = scopes.get(id(node), tree)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.body and isinstance(node.body[0], ast.Expr):
                excluded.add(id(node.body[0].value))
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    values[(id(scope), target.id)].append(node.value)
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
            values[(id(scope), node.target.id)].append(node.value)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            values[(id(scope), node.name)].extend(child.value for child in ast.walk(node)
                                                if isinstance(child, ast.Return) and child.value is not None
                                                and scopes.get(id(child)) is node)
        if isinstance(node, ast.Dict):
            excluded.update(id(key) for key in node.keys)
        if isinstance(node, ast.Subscript):
            excluded.add(id(node.slice))
        if isinstance(node, ast.Compare):
            excluded.update(id(child) for child in ast.walk(node))
        if isinstance(node, ast.Call):
            target = node.func
            name = target.id if isinstance(target, ast.Name) else target.attr if isinstance(target, ast.Attribute) else ""
            if name in ("print", "input", "exit", "error", "usage_error", "write", "warn", "warning") or name.endswith("Error"):
                outputs.update(id(child) for arg in node.args for child in ast.walk(arg))
            if name in ("run", "Popen", "Path", "compile"):
                excluded.update(id(child) for arg in node.args for child in ast.walk(arg))
            for keyword in node.keywords:
                if keyword.arg in ("help", "description", "epilog"):
                    outputs.update(id(child) for child in ast.walk(keyword.value))
    # Follow lexical bindings. Helper call arguments are not followed into parameters.
    references = [node for node in nodes if isinstance(node, ast.Name) and id(node) in outputs]
    visited: set[tuple[int, str]] = set()
    while references:
        reference = references.pop()
        scope = scopes.get(id(reference), tree)
        while (id(scope), reference.id) not in values and id(scope) in parents:
            scope = parents[id(scope)]
        binding = (id(scope), reference.id)
        if binding in visited:
            continue
        visited.add(binding)
        for expression in values.get(binding, []):
            for child in ast.walk(expression):
                outputs.add(id(child))
                if isinstance(child, ast.Name):
                    references.append(child)
    strings: list[str] = []
    source_lines: list[int] = []
    for node in nodes:
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str) or id(node) in excluded:
            continue
        value = node.value
        if id(node) in outputs or re.search(r"[A-Za-z][ ,:;!?—]+[A-Za-z]", value):
            if strings:
                source_lines.append(node.lineno)
            strings.append(value)
            source_lines.extend(python_literal_lines(node, encoded_lines, value))
    return SourceProse("\n".join(strings), tuple(source_lines))


def shell_prose(text: str) -> SourceProse:
    strings: list[str] = []
    source_lines: list[int] = []
    lines = iter(enumerate(text.splitlines(), 1))
    for number, line in lines:
        if line.lstrip().startswith("#"):
            continue
        lexer = shlex.shlex(line, posix=False, punctuation_chars=";&|()")
        lexer.whitespace_split = True
        try:
            tokens = list(lexer)
        except ValueError:
            tokens = []
        command_start = True
        output = False
        for token in tokens:
            if all(character in ";&|()" for character in token) or token in ("then", "do", "else"):
                command_start = True
                output = False
            elif command_start:
                if token in ("if", "elif", "while", "until", "!") or re.match(r"[A-Za-z_]\w*=", token):
                    continue
                output = token in ("echo", "printf", "warn", "die", "error")
                command_start = False
            elif output and len(token) >= 2 and token[0] in "'\"" and token[-1] == token[0]:
                if strings:
                    source_lines.append(number)
                strings.append(token[1:-1])
                source_lines.extend([number] * len(token[1:-1]))
        # Consume input bodies even for non-output commands: their text is not shell code.
        for heredoc in re.finditer(r"<<(-?)[ \t]*(['\"]?)([A-Za-z_]\w*)\2", line):
            prefix = line[:heredoc.start()]
            printed = re.search(r"(?:^|[;&|()])\s*cat\b[^;&|]*$", prefix) is not None
            strip_tabs = heredoc.group(1) == "-"
            delimiter = heredoc.group(3)
            for body_number, body_line in lines:
                body = body_line.lstrip("\t") if strip_tabs else body_line
                if body == delimiter:
                    break
                if printed:
                    if strings:
                        source_lines.append(body_number)
                    strings.append(body)
                    source_lines.extend([body_number] * len(body))
    return SourceProse("\n".join(strings), tuple(source_lines))


def extraction_candidate(text: str, python: bool, retired: re.Pattern[str]) -> bool:
    """Select raw matches and every Python constant that can decode or join into one.

    Shell extraction only removes quotes or leading tabs and joins with newlines;
    check_retired matches each line separately. It cannot create a missing form.
    Python extraction emits string Constant values, including formatted fragments.
    Parsing the other Python files also preserves their syntax-error diagnostics.
    """
    if retired.search(text) is not None:
        return True
    if not python:
        return False
    pending: list[ast.AST] = [ast.parse(text)]
    while pending:
        node = pending.pop()
        if isinstance(node, ast.Constant):
            if isinstance(node.value, str) and retired.search(node.value) is not None:
                return True
        elif not isinstance(node, ast.Name):
            # Constants and identifiers have no contributing child constants.
            pending.extend(ast.iter_child_nodes(node))
    return False


def read_live_file(root: Path, retired: re.Pattern[str], select_candidates: bool,
                   path: Path) -> tuple[tuple[LiveFile, ...], tuple[str, ...]]:
    """Read and extract one source without changing the order of scan results."""
    relative = path.relative_to(root)
    script = relative.parts[0] == "scripts"
    try:
        text = path.read_text(encoding="utf-8")
        python = script and (path.suffix == ".py" or "python" in text.partition("\n")[0])
        if select_candidates and not extraction_candidate(text, python, retired):
            return (), ()
        if script:
            prose = python_prose(text) if python else shell_prose(text)
            return (LiveFile(relative.as_posix(), prose.text, prose.source_lines),), ()
        return (LiveFile(relative.as_posix(), text),), ()
    except (OSError, UnicodeError, SyntaxError) as error:
        return (), (f"{relative}: {error}",)


def read_live_files(root: Path, *, retired_forms: tuple[str, ...] = ()) -> tuple[tuple[LiveFile, ...], tuple[str, ...]]:
    paths: set[Path] = {root / "CLAUDE.md"} if (root / "CLAUDE.md").is_file() else set()
    for folder in ("commands", "docs", "skills", "scripts"):
        paths.update(path for path in (root / folder).rglob("*") if path.is_file())
    selected: list[Path] = []
    # No word boundaries: even a raw match inside an identifier selects extraction.
    retired = re.compile("|".join(re.escape(name) for name in retired_forms), re.IGNORECASE)
    for path in sorted(paths):
        relative = path.relative_to(root)
        if any(part.startswith(".") or part in ("__pycache__", "fixtures", "node_modules") for part in relative.parts):
            continue
        if relative.parts[:2] == ("docs", "plans"):
            continue
        script = relative.parts[0] == "scripts"
        if script and (is_test_code(path) or path.suffix not in (".py", ".sh", "")):
            continue
        if not script and path.suffix not in (".md", ".txt"):
            continue
        selected.append(path)
    read = partial(read_live_file, root, retired, bool(retired_forms))
    # Parsing and extraction are CPU-bound. Keep small roots and baseline serial;
    # map preserves sorted source order even when workers finish out of order.
    if retired_forms and len(selected) >= 64:
        with ProcessPoolExecutor(max_workers=8) as pool:
            results = tuple(pool.map(read, selected, chunksize=4))
    else:
        results = tuple(map(read, selected))
    return (tuple(file for result in results for file in result[0]),
            tuple(issue for result in results for issue in result[1]))


def table(title: str, headings: tuple[str, ...], rows: list[tuple[str, ...]]) -> str:
    all_rows = [headings, *rows]
    widths = [max(len(row[index]) for row in all_rows) for index in range(len(headings))]
    return title + "\n" + "\n".join("  ".join(value.ljust(widths[index]) for index, value in enumerate(row)).rstrip()
                                       for row in all_rows)


def report(terms: tuple[Term, ...], files: tuple[LiveFile, ...], productions: tuple[ProductionNames, ...],
           source: MessageSource, window: AuditWindow, issues: tuple[str, ...]) -> str:
    names = measure_names(terms, files)
    definitions = measure_definitions(terms, files)
    production_names = measure_production_names(productions)
    contacts = measure_wrong_party(source.records, window)
    total = sum(row.hits for row in names)
    other = sum(row.hits for row in names if row.form == "other")
    live = f"live: n={len(files)} files"
    definition_set = f"live + declared: n={len(terms)} terms"
    docs = f"productions: n={len(productions)} docs"
    name_rows = [(row.term, row.name, row.form, str(row.hits), str(len(row.files)), live) for row in names]
    name_rows.append(("TOTAL other / all", f"{other} / {total}", f"{other / total:.1%}" if total else "0.0%", str(other), "—", live))
    definition_rows = [(row.term, row.status, "; ".join(row.files) or "none", str(len(row.files)), definition_set)
                       for row in definitions]
    production_rows = [(row.kind, row.name, "; ".join(row.productions), "; ".join(row.docs), str(len(row.docs)), docs)
                       for row in production_names]
    contact_rows = [(row.sender, str(row.to_user), str(row.to_other_showrunner),
                     f"{NOT_RECORDED}: {row.role_unknown}" if row.role_unknown else "0",
                     f"{NOT_RECORDED}: {row.production_unknown}" if row.production_unknown else "0",
                     str(row.n), "window") for row in contacts.senders]
    return "\n\n".join([
        f"Roles baseline\nRoot file set: {LIVE_SET}\nWindow: {window.label()}\n"
        + "Static script strings; no execution or expansion. Tests and fixtures excluded.\n"
        + "Python references use lexical scope; helper call arguments are not followed into parameters.\n"
        + "Names use exact phrases, ignoring case; plurals count only when listed.\n"
        + "Each term/name pair is a separate count; shared other names count for each term.\n"
        + "Names count overlapping phrases independently; check reports the longest phrase once.\n"
        + "Kept contexts remain in lexical Names and are excluded only by check.\n"
        + "Production and message sources: natedev only.",
        table("Names", ("Term", "Name", "Form", "Hits (n)", "Files (n)", "File set"), name_rows),
        table("Definitions (declared sources plus explicit **Name** — definitions; other-meaning files excluded)",
              ("Term", "Finding", "Files", "Files (n)", "File set"),
              definition_rows or [("none", "none", "none", "0", definition_set)]),
        table("Unit and session names — natedev only; all Units rows, including retired rows",
              ("Finding", "Name", "Productions", "Files", "Docs (n)", "File set"),
              production_rows or [("none", "none", "none", "none", "0", docs)]),
        table("Wrong-party asks — natedev only; sent contacts, request intent not recorded",
              ("Sender", "Director to user (n)", "Director to other showrunner (n)", "Role unknown (n)",
               "Production unknown (n)", "Sent (n)", "Window"),
              contact_rows or [("none", "0", "0", "0", "0", "0", "window")]),
        f"Message window: {window.label()}\nSource: ~/.local/state/message/log.jsonl ({source.status})\n"
        + f"Window records n={contacts.window_records}; natedev sent n={contacts.sent_records}; "
        + f"other-machine records excluded n={contacts.other_machine_records}; machine {NOT_RECORDED} n={contacts.machine_unknown}; "
        + f"invalid lines in whole log n={source.invalid_lines}.\n"
        + f"Role source: {ROLE_SOURCE}. Historical role/production fields are {NOT_RECORDED} in the current log schema.\n"
        + "Current names cannot prove past roles.\n"
        + "Recorded zeroes cover only known roles. Unknown counts do not prove absence of wrong-party asks.",
        table("Source issues", ("Source issue", "Issues (n)"), [(issue, "1") for issue in issues] or [("none", "0")]),
    ])


def audit_window(since: str, until: str, now: datetime) -> AuditWindow:
    zone = user_zone()
    end = datetime.combine(date.fromisoformat(until) + timedelta(days=1), time(), zone) if until else now
    start = datetime.combine(date.fromisoformat(since), time(), zone) if since else end - timedelta(days=14)
    if start >= end:
        raise ValueError("since must be before the end of until")
    return AuditWindow(start, end)


def baseline(root: Path, window: AuditWindow) -> str:
    terms = parse_terms((root / "scripts/production/role_terms.toml").read_text(encoding="utf-8"))
    files, file_issues = read_live_files(root)
    issues = list(file_issues)
    productions: list[ProductionNames] = []
    # This is the same timer/doc reader as `showrunners.py list`, without session/socket probes.
    for slug, doc in sorted(showrunners.timer_docs().items()):
        try:
            productions.append(parse_production(slug, str(doc), doc.read_text(encoding="utf-8")))
        except (OSError, UnicodeError, production_units.Refusal) as error:
            issues.append(f"{doc}: {error}")
    path = Path.home() / ".local/state/message/log.jsonl"
    try:
        source = parse_messages(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError) as error:
        source = MessageSource((), 0, f"unavailable: {error}")
        issues.append(f"{path}: {error}")
    return f"Repository: {root}\n" + report(terms, files, tuple(productions), source, window, tuple(issues))


def main(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("action", choices=("baseline", "check"))
    _ = parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    _ = parser.add_argument("--since", default="", help="first local date, YYYY-MM-DD")
    _ = parser.add_argument("--until", default="", help="last local date, inclusive, YYYY-MM-DD")
    args = parser.parse_args(arguments)
    try:
        if cast(str, args.action) == "check":
            issues = check_sources(cast(Path, args.root).resolve())
            for issue in issues:
                print(issue)
            return int(bool(issues))
        window = audit_window(cast(str, args.since), cast(str, args.until), datetime.now(timezone.utc))
        print(baseline(cast(Path, args.root).resolve(), window))
    except (OSError, ValueError) as error:
        print(f"roles_audit: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
