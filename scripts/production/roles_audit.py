#!/usr/bin/env python3
"""Check retired names and fixed display names without writes.

Names are case-insensitive literal phrases with word boundaries. Script prose is
static: Python string literals (excluding docstrings, keys, paths and machine
tokens), quoted shell output and cat heredocs. Scripts and strings are not expanded.
"""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import ast
from collections import defaultdict, deque
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from functools import lru_cache, partial
import gc
from io import StringIO
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tomllib
import tokenize
from typing import cast

from scripts.lib.code_kind import is_test_code  # noqa: E402
from scripts.lib import names as display_names  # noqa: E402


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
    other_names: tuple[str, ...]
    kept: tuple[str, ...] = ()
    inflected_forms: tuple[NameInflection, ...] = ()
    retired: tuple[str, ...] = ()
    kept_contexts: tuple[KeptContext, ...] = ()


@dataclass(frozen=True)
class LiveFile:
    path: str
    text: str
    source_lines: tuple[int, ...] = ()  # Physical source line for each prose character.
    printed_text: str = ""
    printed_lines: tuple[int, ...] = ()


@dataclass(frozen=True)
class SourceProse:
    text: str
    source_lines: tuple[int, ...]  # Physical source line for each prose character.
    printed_text: str = ""
    printed_lines: tuple[int, ...] = ()


@dataclass(frozen=True)
class CurrentName:
    """The live name that replaces a retired one."""
    name: str


@dataclass(frozen=True)
class NoCurrentName:
    """The term itself is retired: the thing it named is gone."""


@dataclass(frozen=True)
class RetiredUse:
    path: str
    line: int
    name: str
    replacement: CurrentName | NoCurrentName

    def hint(self) -> str:
        if isinstance(self.replacement, NoCurrentName):
            return f"{self.path}:{self.line}: retired {self.name!r}; no current name replaces it"
        return f"{self.path}:{self.line}: retired {self.name!r}; use {self.replacement.name!r}"


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
        kept = string_list(row.get("kept", []), f"{name}.kept")
        _ = string_list(row.get("other_meaning_files", []), f"{name}.other_meaning_files")
        terms.append(Term(name, other_names, kept,
                          tuple(inflections), retired, tuple(kept_contexts)))
    names = [term.name.casefold() for term in terms]
    if len(set(names)) != len(names):
        raise ValueError("defined term names must be unique")
    return tuple(terms)


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


def check_retired(terms: tuple[Term, ...], files: tuple[LiveFile, ...]) -> tuple[RetiredUse, ...]:
    """Check extracted source prose only; a kept context applies once in its named file."""
    findings: list[RetiredUse] = []
    for term in terms:
        if not term.retired:
            continue
        retired = {name.casefold() for name in term.retired}
        replacement = NoCurrentName() if term.name.casefold() in retired else CurrentName(term.name)
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
                        findings.append(RetiredUse(file.path, source_line, match.group(), replacement))
                offset += len(line) + 1
    return tuple(findings)


def display_terms(loaded: display_names.Names) -> tuple[Term, ...]:
    return tuple(Term(history[0], tuple(history[1:]),
                      retired=tuple(history[1:]))
                 for group in (loaded.jobs, loaded.audiences)
                 for history in group.values() if len(history) > 1)


@dataclass(frozen=True)
class DisplayLabel:
    path: str
    line: int
    name: str


@dataclass(frozen=True)
class SentenceRoleWord:
    path: str
    line: int
    name: str


@dataclass(frozen=True)
class UnreadSource:
    path: str
    reason: str

    def warning(self) -> str:
        return f"{self.path}: {self.reason}"


@dataclass(frozen=True)
class LiveSources:
    files: tuple[LiveFile, ...]
    unread_sources: tuple[UnreadSource, ...]


@dataclass(frozen=True)
class SourceCheck:
    issues: tuple[str, ...]
    sentences: tuple[SentenceRoleWord, ...]
    labels: tuple[DisplayLabel, ...]
    unread_sources: tuple[UnreadSource, ...]


def display_label_context(line: str, match: re.Match[str]) -> bool:
    """Identify a whole label or a complete status column."""
    left = re.split(r"[·|\t]|\s[-—]\s", line[:match.start()])[-1].strip()
    right = re.split(r"[·|\t]|\s[-—]\s", line[match.end():])[0].strip()
    return not left and (not right or right.isdecimal() or right == "\ufffc")


def check_fixed_display(loaded: display_names.Names, files: tuple[LiveFile, ...], *, prose: bool = True) -> tuple[DisplayLabel | SentenceRoleWord, ...]:
    """Classify output labels separately from role words inside sentences."""
    current = forms_pattern(loaded.current())
    findings: list[DisplayLabel | SentenceRoleWord] = []
    for file in files:
        if not file.path.startswith("scripts/"):
            continue
        offset = 0
        for line in file.printed_text.split("\n"):
            for match in current.finditer(line):
                number = file.printed_lines[offset + match.start()] if file.printed_lines else 1
                # A whole literal or a complete shown-name/status column is a label.
                # Other words in that segment make it sentence prose.
                if display_label_context(line, match):
                    findings.append(DisplayLabel(file.path, number, match.group()))
                elif prose:
                    findings.append(SentenceRoleWord(file.path, number, match.group()))
            offset += len(line) + 1
    return tuple(findings)


def source_check(root: Path, *, full_extraction: bool = False, prose: bool = True) -> SourceCheck:
    """Read repository sources, without production records, memory or message logs."""
    terms = parse_terms((root / "scripts/production/role_terms.toml").read_text(encoding="utf-8"))
    loaded = display_names.load_names(root / "config/names.toml")
    if not isinstance(loaded, display_names.Names):
        return SourceCheck((display_names.error_text(loaded, root / "config/names.toml"),), (), (), ())
    forms = (*tuple(name for term in terms for name in term.retired), *loaded.retired())
    sources = read_live_sources(root, retired_forms=() if full_extraction else forms,
                               fixed_forms=() if full_extraction else loaded.current(), prose=prose)
    return check_live_sources(terms, loaded, sources, prose=prose)


def check_live_sources(terms: tuple[Term, ...], loaded: display_names.Names, sources: LiveSources, *, prose: bool = True) -> SourceCheck:
    """Check findings and coverage from the same source read."""
    histories = display_terms(loaded)
    files = sources.files
    uses = check_retired(terms, files)
    # Stored keys and snapshots keep their values. Display retirement concerns output.
    display_files = tuple(LiveFile(file.path, file.printed_text, file.printed_lines)
                          if file.path.startswith("scripts/") else file for file in files)
    uses += check_retired(histories, display_files)
    classified = check_fixed_display(loaded, files, prose=prose)
    return SourceCheck(
        (*(source.warning() for source in sources.unread_sources),
         *(use.hint() for use in uses),
         *(f"{use.path}:{use.line}: fixed display name {use.name!r}; read config/names.toml"
           for use in classified if isinstance(use, DisplayLabel))),
        tuple(use for use in classified if isinstance(use, SentenceRoleWord)) if prose else (),
        tuple(use for use in classified if isinstance(use, DisplayLabel)), sources.unread_sources)


def check_sources(root: Path, *, full_extraction: bool = False) -> tuple[str, ...]:
    return source_check(root, full_extraction=full_extraction, prose=False).issues


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


@lru_cache(maxsize=1)
def python_tree(text: str) -> ast.Module:
    """Reuse the current source tree during selection and extraction."""
    return ast.parse(text)


def python_fragments(expression: ast.AST, encoded_lines: tuple[bytes, ...] = ()) -> tuple[str, tuple[int, ...]]:
    """Join static fragments and retain source lines when the caller needs them."""
    if isinstance(expression, ast.Constant) and isinstance(expression.value, str):
        return expression.value, python_literal_lines(expression, encoded_lines, expression.value) if encoded_lines else ()
    if isinstance(expression, ast.JoinedStr):
        parts = [python_fragments(part, encoded_lines) for part in expression.values]
    elif isinstance(expression, ast.BinOp) and isinstance(expression.op, ast.Add):
        parts = [python_fragments(expression.left, encoded_lines), python_fragments(expression.right, encoded_lines)]
    else:
        return "\ufffc", (expression.lineno if isinstance(expression, ast.expr) else 1,)
    return "".join(part[0] for part in parts), tuple(line for part in parts for line in part[1])


def python_prose(text: str) -> SourceProse:
    tree = python_tree(text)
    # Match the parser's newline rules; form feeds stay within a physical line.
    encoded_lines = tuple(match.group().encode() for match in re.finditer(r".*?(?:\r\n|\r|\n|$)", text))
    excluded: set[ast.AST] = set()
    outputs: set[ast.AST] = set()
    printed_roots: list[ast.AST] = []
    scopes: dict[ast.AST, ast.AST] = {}
    parents: dict[ast.AST, ast.AST] = {}
    values: dict[tuple[ast.AST, str], list[ast.AST]] = defaultdict(list)

    analysis_types = {ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef,
                      ast.Assign, ast.AnnAssign, ast.Dict, ast.Subscript, ast.Compare, ast.Call}
    scope_types = {ast.Name, ast.Assign, ast.AnnAssign, ast.FunctionDef, ast.AsyncFunctionDef}
    analysis_nodes: list[ast.AST] = []
    names: list[ast.Name] = []
    strings_and_joins: list[ast.AST] = []
    returns: dict[ast.AST, list[ast.AST]] = defaultdict(list)
    scope_roots: dict[ast.AST, ast.AST] = {}
    pending: deque[tuple[ast.AST, ast.AST, bool]] = deque([(tree, tree, True)])
    while pending:
        node, scope, indexed = pending.popleft()
        kind = type(node)
        if indexed and kind in scope_types:
            scopes[node] = scope
        if kind in analysis_types:
            analysis_nodes.append(node)
        elif kind is ast.Name:
            names.append(cast(ast.Name, node))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            strings_and_joins.append(node)
        elif kind in (ast.JoinedStr, ast.BinOp):
            strings_and_joins.append(node)
        elif isinstance(node, ast.Return) and node.value is not None:
            returns[scope if indexed else tree].append(node.value)
        if kind in (ast.Name, ast.Constant):
            continue
        if indexed and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            parents[node] = parents[scope] if isinstance(scope, ast.ClassDef) else scope
            arguments = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
            if node.args.vararg is not None:
                arguments.append(node.args.vararg)
            if node.args.kwarg is not None:
                arguments.append(node.args.kwarg)
            for argument in arguments:
                values[(node, argument.arg)] = []
            for default in (*node.args.defaults, *node.args.kw_defaults):
                if default is not None:
                    scope_roots[default] = scope
            body = [node.body] if isinstance(node, ast.Lambda) else node.body
            for statement in body:
                scope_roots[statement] = node
            if not isinstance(node, ast.Lambda):
                for decorator in node.decorator_list:
                    scope_roots[decorator] = scope
            scope, indexed = tree, False
        elif indexed and isinstance(node, ast.ClassDef):
            parents[node] = scope
            for statement in node.body:
                scope_roots[statement] = node
            for expression in (*node.bases, *node.decorator_list):
                scope_roots[expression] = scope
            scope, indexed = tree, False
        for child in ast.iter_child_nodes(node):
            if not child._fields:
                continue
            if child in scope_roots:
                pending.append((child, scope_roots.pop(child), True))
            else:
                pending.append((child, scope, indexed))
    for node in analysis_nodes:
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.body and isinstance(node.body[0], ast.Expr):
                excluded.add(node.body[0].value)
        if isinstance(node, ast.Assign):
            scope = scopes.get(node, tree)
            for target in node.targets:
                if isinstance(target, ast.Name):
                    values[(scope, target.id)].append(node.value)
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
            scope = scopes.get(node, tree)
            values[(scope, node.target.id)].append(node.value)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            scope = scopes.get(node, tree)
            values[(scope, node.name)].extend(returns[node])
        if isinstance(node, ast.Dict):
            excluded.update(key for key in node.keys if key is not None)
        if isinstance(node, ast.Subscript):
            excluded.add(node.slice)
        if isinstance(node, ast.Compare):
            excluded.update(child for child in ast.walk(node))
        if isinstance(node, ast.Call):
            target = node.func
            name = target.id if isinstance(target, ast.Name) else target.attr if isinstance(target, ast.Attribute) else ""
            if name in ("print", "input", "exit", "error", "usage_error", "write", "warn", "warning") or name.endswith("Error"):
                outputs.update(child for arg in node.args for child in ast.walk(arg))
                printed_roots.extend(node.args)
            if name in ("run", "Popen", "Path", "compile"):
                excluded.update(child for arg in node.args for child in ast.walk(arg))
            for keyword in node.keywords:
                if keyword.arg in ("help", "description", "epilog"):
                    outputs.update(child for child in ast.walk(keyword.value))
                    printed_roots.append(keyword.value)
    # Follow lexical bindings. Helper call arguments are not followed into parameters.
    references = [node for node in names if node in outputs]
    visited: set[tuple[ast.AST, str]] = set()
    while references:
        reference = references.pop()
        scope = scopes.get(reference, tree)
        while (scope, reference.id) not in values and scope in parents:
            scope = parents[scope]
        binding = (scope, reference.id)
        if binding in visited:
            continue
        visited.add(binding)
        for expression in values.get(binding, []):
            for child in ast.walk(expression):
                outputs.add(child)
                if isinstance(child, ast.Name):
                    references.append(child)
    # Output values have a narrower flow than lexical prose. Record construction,
    # serialization, lookups and comparisons do not turn their keys into labels.
    output_roots = tuple(printed_roots)
    printed_outputs: set[ast.AST] = set()
    printed_bindings: set[tuple[ast.AST, str]] = set()
    while printed_roots:
        expression = printed_roots.pop()
        if expression in printed_outputs:
            continue
        printed_outputs.add(expression)
        if isinstance(expression, ast.Name):
            scope = scopes.get(expression, tree)
            while (scope, expression.id) not in values and scope in parents:
                scope = parents[scope]
            binding = (scope, expression.id)
            if binding not in printed_bindings:
                printed_bindings.add(binding)
                printed_roots.extend(values.get(binding, []))
        elif isinstance(expression, (ast.JoinedStr, ast.FormattedValue, ast.BinOp)):
            printed_roots.extend(ast.iter_child_nodes(expression))
        elif isinstance(expression, ast.IfExp):
            # Each possible output has its own label or sentence context. The
            # condition only selects a branch; its compared keys are not output.
            printed_roots.extend((expression.body, expression.orelse))
        elif isinstance(expression, (ast.List, ast.Tuple, ast.Set)):
            printed_roots.extend(expression.elts)
        elif isinstance(expression, ast.Call):
            target = expression.func
            if isinstance(target, ast.Name):
                # Local helpers contribute their returns, not their input contracts.
                printed_roots.append(target)
                if target.id in ("str", "repr", "sorted"):
                    printed_roots.extend(expression.args)
            elif isinstance(target, ast.Attribute) and target.attr in (
                    "join", "format", "strip", "lstrip", "rstrip", "lower", "upper", "casefold"):
                printed_roots.append(target.value)
                printed_roots.extend(expression.args)
    strings: list[str] = []
    source_lines: list[int] = []
    printed: list[str] = []
    printed_lines: list[int] = []

    def conditional_fragments(
        expression: ast.AST, bindings: frozenset[tuple[ast.AST, str]],
    ) -> tuple[list[tuple[str, tuple[int, ...]]], set[ast.AST]]:
        """Keep each branch inside the context in which its value is printed."""
        if isinstance(expression, ast.IfExp):
            branches = [conditional_fragments(branch, bindings)
                        for branch in (expression.body, expression.orelse)]
            return ([part for branch in branches for part in branch[0]],
                    {child for child in ast.walk(expression)}
                    | {node for branch in branches for node in branch[1]})
        if isinstance(expression, ast.Name):
            scope = scopes.get(expression, tree)
            while (scope, expression.id) not in values and scope in parents:
                scope = parents[scope]
            binding = (scope, expression.id)
            if binding not in bindings:
                branches = [conditional_fragments(value, bindings | {binding})
                            for value in values.get(binding, [])]
                handled = {node for branch in branches for node in branch[1]}
                if handled:
                    return [part for branch in branches for part in branch[0]], handled | {expression}
        if isinstance(expression, ast.JoinedStr):
            children = expression.values
        elif isinstance(expression, ast.FormattedValue):
            children = [expression.value]
        elif isinstance(expression, ast.BinOp) and isinstance(expression.op, ast.Add):
            children = [expression.left, expression.right]
        else:
            return [python_fragments(expression, encoded_lines)], set()
        branches = [conditional_fragments(child, bindings) for child in children]
        handled = {node for branch in branches for node in branch[1]}
        if not handled:
            return [python_fragments(expression, encoded_lines)], set()
        combinations: list[tuple[str, tuple[int, ...]]] = [("", ())]
        for branch in branches:
            combinations = [(prefix + suffix, before + after)
                            for prefix, before in combinations for suffix, after in branch[0]]
        return combinations, handled | {child for child in ast.walk(expression)}

    # Emit an entire string expression once, preserving sentence and column
    # context across both concatenated literals and formatted values.
    composite_parts: set[ast.AST] = set()
    for expression in output_roots:
        variants, handled = conditional_fragments(expression, frozenset())
        if not handled:
            continue
        composite_parts.update(handled)
        for value, locations in variants:
            if printed:
                printed_lines.append(locations[0] if locations else 1)
            printed.append(value)
            printed_lines.extend(locations)
    for node in strings_and_joins:
        if (isinstance(node, ast.JoinedStr) or (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add))) and node in printed_outputs and node not in composite_parts:
            composite_parts.update(child for child in ast.walk(node) if child is not node)
            value, locations = python_fragments(node, encoded_lines)
            if printed:
                printed_lines.append(node.lineno)
            printed.append(value)
            printed_lines.extend(locations)
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str) or node in excluded:
            continue
        value = node.value
        if node in printed_outputs and node not in composite_parts:
            if printed:
                printed_lines.append(node.lineno)
            printed.append(value)
            printed_lines.extend(python_literal_lines(node, encoded_lines, value))
        if node in outputs or re.search(r"[A-Za-z][ ,:;!?—]+[A-Za-z]", value):
            if strings:
                source_lines.append(node.lineno)
            strings.append(value)
            source_lines.extend(python_literal_lines(node, encoded_lines, value))
    # Release recursive closures before the next file allocates its syntax tree.
    del conditional_fragments
    return SourceProse("\n".join(strings), tuple(source_lines), "\n".join(printed), tuple(printed_lines))


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
            printed = re.search(r"(?:^|[;&|()])\s*cat\b(?:[^;&|]|[012]?>&[12])*$", prefix) is not None
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
    return SourceProse("\n".join(strings), tuple(source_lines), "\n".join(strings), tuple(source_lines))


NO_FIXED_FORMS: frozenset[str] = frozenset()


def extraction_candidate(text: str, python: bool, retired: re.Pattern[str],
                         fixed: frozenset[str] = NO_FIXED_FORMS, *, prose: bool = True) -> bool:
    """Select decoded constants and every Python string join.

    Shell extraction only removes quotes or leading tabs and joins with newlines;
    check_retired matches each line separately. It cannot create a missing form.
    Python extraction emits string Constant values, including formatted fragments.
    Additions and f-strings can assemble text through bindings and conditional branches.
    Select all such expressions without duplicating extraction's binding analysis.
    Parsing the other Python files also preserves their syntax-error diagnostics.
    """
    if not python:
        return retired.search(text) is not None or any(name in text.casefold() for name in fixed)
    current = forms_pattern(tuple(fixed)) if fixed else re.compile(r"(?!)")
    pending: list[ast.AST] = [python_tree(text)]
    while pending:
        node = pending.pop()
        if isinstance(node, ast.Constant):
            if isinstance(node.value, str):
                if retired.search(node.value) is not None and retired.search(without_paths(node.value, ())) is not None:
                    return True
                if prose and any(name in node.value.casefold() for name in fixed):
                    return True
                if not prose and any(display_label_context(line, match)
                                     for line in node.value.split("\n") for match in current.finditer(line)):
                    return True
        elif ((isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add))
              or isinstance(node, ast.JoinedStr)):
            return True
        elif not isinstance(node, ast.Name):
            # Constants and identifiers have no contributing child constants.
            pending.extend(ast.iter_child_nodes(node))
    return False


def read_live_file(root: Path, retired: re.Pattern[str], select_candidates: bool,
                   path: Path, *, fixed: frozenset[str] = NO_FIXED_FORMS, prose: bool = True) -> LiveSources:
    """Read and extract one source without changing the order of scan results."""
    relative = path.relative_to(root)
    script = relative.parts[0] == "scripts"
    try:
        text = path.read_text(encoding="utf-8")
        python = script and (path.suffix == ".py" or "python" in text.partition("\n")[0])
        if select_candidates and not extraction_candidate(text, python, retired, fixed if script else NO_FIXED_FORMS, prose=prose):
            return LiveSources((), ())
        if script:
            extracted = python_prose(text) if python else shell_prose(text)
            return LiveSources((LiveFile(relative.as_posix(), extracted.text, extracted.source_lines, extracted.printed_text, extracted.printed_lines),), ())
        return LiveSources((LiveFile(relative.as_posix(), text),), ())
    except (OSError, UnicodeError, SyntaxError) as error:
        return LiveSources((), (UnreadSource(relative.as_posix(), str(error)),))


def read_live_sources(root: Path, *, retired_forms: tuple[str, ...] = (), fixed_forms: tuple[str, ...] = (), prose: bool = True) -> LiveSources:
    listed = subprocess.run(
        ["git", "-C", str(root), "ls-files", "--cached", "--others", "--exclude-standard", "-z",
         "--", "commands/", "docs/", "skills/", "scripts/", "CLAUDE.md"],
        capture_output=True, text=True, check=False)
    if listed.returncode == 0:
        paths: set[Path] = {root / name for name in listed.stdout.split("\0") if name and (root / name).is_file()}
    elif "not a git repository" in listed.stderr:
        # Fixture callers also audit source folders outside Git trees.
        paths = {root / "CLAUDE.md"} if (root / "CLAUDE.md").is_file() else set()
        for folder in ("commands", "docs", "skills", "scripts"):
            paths.update(path for path in (root / folder).rglob("*") if path.is_file())
    else:
        raise ValueError(f"cannot list audit sources through git: {listed.stderr.strip()}")
    selected: list[Path] = []
    # No word boundaries: even a raw match inside an identifier selects extraction.
    retired = re.compile("|".join(re.escape(name) for name in retired_forms) if retired_forms else r"(?!)", re.IGNORECASE)
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
    read = partial(read_live_file, root, retired, bool(retired_forms or fixed_forms),
                   fixed=frozenset(name.casefold() for name in fixed_forms), prose=prose)
    # Parsing and extraction are CPU-bound. Keep small roots and full extraction serial;
    # map preserves sorted source order even when workers finish out of order.
    if (retired_forms or fixed_forms) and len(selected) >= 64:
        # Each worker exits after this scan. Reference counting releases its ASTs.
        with ProcessPoolExecutor(initializer=gc.disable) as pool:
            results = tuple(pool.map(read, selected, chunksize=1))
    else:
        results = tuple(map(read, selected))
    return LiveSources(tuple(file for result in results for file in result.files),
                       tuple(source for result in results for source in result.unread_sources))


def read_live_files(root: Path, *, retired_forms: tuple[str, ...] = (), fixed_forms: tuple[str, ...] = ()) -> tuple[tuple[LiveFile, ...], tuple[str, ...]]:
    sources = read_live_sources(root, retired_forms=retired_forms, fixed_forms=fixed_forms)
    return sources.files, tuple(source.warning() for source in sources.unread_sources)


def main(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("action", choices=("check",))
    _ = parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    _ = parser.add_argument("--prose", action="store_true", help="list role words in output sentences")
    args = parser.parse_args(arguments)
    try:
        result = source_check(cast(Path, args.root).resolve(), prose=cast(bool, args.prose))
        for issue in result.issues:
            print(issue)
        if cast(bool, args.prose):
            print(f"role words in sentences: {len(result.sentences)}")
            for use in result.sentences:
                print(f"{use.path}:{use.line}: {use.name!r}")
        return int(bool(result.issues))
    except (OSError, ValueError) as error:
        print(f"roles_audit: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
