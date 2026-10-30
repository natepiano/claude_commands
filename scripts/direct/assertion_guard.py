#!/usr/bin/env python3
"""Report assertion and test changes; refuse unruled removals and weaker checks."""
from __future__ import annotations

import argparse
import ast
from bisect import bisect_right
from collections import Counter, defaultdict, deque
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from pathlib import Path
import re
import subprocess
import sys
from typing import Literal, NamedTuple, NoReturn, cast, override

Side = Literal["old", "new"]
Kind = Literal["removed", "weaker", "changed", "test removed", "test turned off"]
HUNK = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")
FUNCTION = re.compile(r"\b(?:fn|def)\s+([A-Za-z_]\w*)\s*[(<]")
ASSERTION_MACROS = (
    "assert", "assert_eq", "assert_ne", "debug_assert", "debug_assert_eq", "debug_assert_ne",
    "assert_matches", "prop_assert", "prop_assert_eq", "prop_assert_ne")
TRUSTED_MACROS = frozenset(ASSERTION_MACROS + (
    "matches", "print", "println", "eprint", "eprintln", "panic", "unreachable", "todo",
    "unimplemented", "write", "writeln", "dbg"))
MACRO_INVOCATION = re.compile(r"(?<!\w)(?P<name>(?:::\s*)?[A-Za-z_]\w*(?:\s*::\s*[A-Za-z_]\w*)*)\s*!\s*[(\[{]")
ASSERTION = re.compile(
    r"\b(?P<macro>" + "|".join(ASSERTION_MACROS) + r")!\s*\("
    + r"|\b(?P<bare>assert)\s+|\bself\.(?P<method>assert\w+|fail)\s*\("
    + r"|\b(?P<raises>pytest\.raises)\s*\(")
EXACT_CALLS = frozenset((
    "assert_eq", "assert_ne", "debug_assert_eq", "debug_assert_ne", "prop_assert_eq",
    "assertEqual", "assertNotEqual", "assertIs", "assertListEqual", "assertDictEqual",
    "assertTupleEqual", "assertSetEqual", "assertCountEqual", "assertMultiLineEqual", "assertSequenceEqual"))
COMPARISON = re.compile(r"==|!=|<=|>=|(?<![<])<(?![<])|(?<![>])>(?![>])|\bis(?:\s+not)?\b")
PYTHON_LITERAL = re.compile(
    r"'''(?:\\.|'(?!'')|[^'\\])*'''"
    + r'|"""(?:\\.|"(?!"")|[^"\\])*"""'
    + r'''|'(?:\\.|[^'\\])*'|"(?:\\.|[^"\\])*"|\#[^\n]*''', re.DOTALL)
NUMERIC_LITERAL = re.compile(
    r"(?P<digits>0x[0-9a-fA-F_]+|0o[0-7_]+|0b[01_]+|"
    + r"[0-9][0-9_]*(?:\.(?:[0-9][0-9_]*|(?![.\w])))?(?:[eE][+-]?[0-9][0-9_]*)?)"
    + r"(?P<suffix>[iu](?:8|16|32|64|128|size)|f(?:32|64))?")
NUMERIC_TYPES = frozenset(("i8", "i16", "i32", "i64", "i128", "isize",
                           "u8", "u16", "u32", "u64", "u128", "usize", "f32", "f64"))
RUST_LITERAL = re.compile(
    r'''(?:br(?<!\wbr)|r(?<!\wr))(?P<hashes>\#*)"(?s:.*?)"(?P=hashes)'''
    + r'''|b?"(?:\\.|[^"\\])*"|b?'(?:\\(?:u\{[0-9a-fA-F_]+\}|x[0-9a-fA-F]{2}|.)|[^'\\])'|//[^\n]*|/\*'''
    + r"|(?<!\w)(?:" + NUMERIC_LITERAL.pattern + r")(?!\w)", re.DOTALL)
BLOCK_COMMENT = re.compile(r"/\*|\*/")
IDENTIFIER = re.compile(r"\b[A-Za-z_]\w*(?:::[A-Za-z_]\w*)*\b")
CONSTANT_ITEM = re.compile(r"\b(?:const|static)\s+(?:mut\s+)?([A-Za-z_]\w*)\s*:(?P<declared_type>[^={}]+)=")
MODULE_DECLARATION = re.compile(r"\bmod\s+(\w+)\s*([;{])")
WEAKER = re.compile(
    r"\b(?:assert|debug_assert|prop_assert)!\s*\(|\bassert\s+"
    + r"|\bself\.(?:assertTrue|assertFalse|assertLess\w*|assertGreater\w*|assertIn|"
    + r"assertAlmostEqual|assertRegex)\s*\(")
TURNED_OFF = re.compile(
    r"#\[ignore\b|@unittest\.(?:skip|skipIf|skipUnless|expectedFailure)\b"
    + r"|@pytest\.mark\.(?:skip|skipif|xfail)\b|\bself\.skipTest\s*\(|\bpytest\.skip\s*\(")
TEST_MARK = re.compile(r"#\[(?:test|tokio::test|rstest)\b")
PYTHON_TEST = re.compile(r"\bdef\s+(test_\w*)\s*\(")
RULING = re.compile(r"(?:^|(?:-[a-zA-Z]*m|--message)[= \t]*[\"']?)Assertion-Change:"
                    + r"[ \t]*(.+?)::([^\s]+)[ \t]+[—-][ \t]*(\S[^\n]*)$", re.MULTILINE)


class DiffLine(NamedTuple):
    path: str
    side: Side
    number: int
    text: str
    hunk: int

    @property
    def key(self) -> str:
        return normalized(self.text)


@dataclass(frozen=True)
class Finding:
    source: DiffLine
    kind: Kind
    old: str
    new: str
    test: str = "?"
    removed_assertions: int = 0


@dataclass(frozen=True)
class Assertion:
    source: DiffLine
    name: str
    operands: tuple[str, ...]
    exact: bool
    weaker: bool
    hunks: tuple[int, ...]
    subject_text: str


@dataclass(frozen=True)
class NoReplacement:
    """No added assertion meets the pairing rule."""


class GuardError(Exception):
    """The guard cannot judge this diff."""


class GuardParser(argparse.ArgumentParser):
    @override
    def error(self, message: str) -> NoReturn:
        raise GuardError(message)


class GuardArguments(argparse.Namespace):
    repo: str = "."
    head: str = ""
    staged: bool = False
    def __init__(self) -> None:
        super().__init__()
        self.bases: list[str] = []
        self.rulings: list[str] = []


class AssertionSpan(NamedTuple):
    first: int
    last: int
    text: str
    comparison: str


class TouchedAssertion(NamedTuple):
    source: DiffLine
    hunks: tuple[int, ...]
    comparison: str
    numeric_constants: tuple[NumericOccurrence, ...] = ()


def normalized(text: str) -> str:
    parts: list[str] = []
    cursor = 0
    for token in lexical_spans(text, True):
        if token.kind == "literal":
            parts.extend(("".join(text[cursor:token.first].split()), text[token.first:token.last]))
            cursor = token.last
    parts.append("".join(text[cursor:].split()))
    return "".join(parts)


class LexicalSpan(NamedTuple):
    first: int
    last: int
    kind: Literal["literal", "comment"]


def lexical_spans(text: str, rust: bool) -> Iterable[LexicalSpan]:
    """Locate literals and comments, including nested Rust block comments."""
    token_pattern = RUST_LITERAL if rust else PYTHON_LITERAL
    scan = 0
    while match := token_pattern.search(text, scan):
        end = match.end()
        if rust and match[0] == "/*":
            depth = 1
            end = len(text)
            for delimiter in BLOCK_COMMENT.finditer(text, match.end()):
                depth += 1 if delimiter[0] == "/*" else -1
                if not depth:
                    end = delimiter.end()
                    break
        yield LexicalSpan(match.start(), end,
                          "comment" if match[0].startswith(("//", "/*", "#")) else "literal")
        scan = end


def code_lines(lines: list[str], rust: bool, *, keep_literals: bool = False) -> list[str]:
    """Hide strings and comments, with the same line and column positions."""
    text = "\n".join(lines)
    parts: list[str] = []
    cursor = 0
    for token in lexical_spans(text, rust):
        if not keep_literals or token.kind == "comment":
            parts.append(text[cursor:token.first])
            parts.append("\n".join(" " * len(row) for row in text[token.first:token.last].split("\n")))
            cursor = token.last
    if not parts:
        return lines
    parts.append(text[cursor:])
    return "".join(parts).split("\n")


@dataclass(frozen=True)
class LiteralExpression:
    text: str


@dataclass(frozen=True)
class NumericLiteral:
    digits: str
    suffix: str
    default_type: Literal["i32", "f64"]


@dataclass(frozen=True)
class NumericConstant:
    literal: NumericLiteral
    declared_type: str


@dataclass(frozen=True)
class NumericOccurrence:
    first: int
    last: int
    value: NumericLiteral | NumericConstant


@dataclass(frozen=True)
class AssertionExpansion:
    text: str
    numeric_constants: tuple[NumericOccurrence, ...] = ()


@dataclass(frozen=True)
class UnresolvedConstant:
    """Leave an unknown, cyclic or nonliteral expression unchanged."""


UNRESOLVED_CONSTANT = UnresolvedConstant()


@dataclass(frozen=True)
class NameBinding:
    module: tuple[str, ...]
    value: LiteralExpression | NumericConstant | UnresolvedConstant


class UseBinding(NamedTuple):
    name: str
    parts: tuple[str, ...]


class GlobImport(NamedTuple):
    module: tuple[str, ...]
    parts: tuple[str, ...]


class SourceFile(NamedTuple):
    path: str
    side: Side


@dataclass
class ScopeBlock:
    first: int
    last: int
    module: tuple[str, ...]
    module_items: bool = False
    uncertain: bool = False
    children: list[ScopeBlock] = field(default_factory=list)


@dataclass
class ConstantTable:
    block: ScopeBlock
    line_offsets: list[int]
    bindings: dict[str, list[NameBinding]] = field(default_factory=dict)
    globs: list[GlobImport] = field(default_factory=list)
    external_modules: set[tuple[str, ...]] = field(default_factory=set)
    inline_modules: set[tuple[str, ...]] = field(default_factory=set)
    refused_names: set[str] = field(default_factory=set)
    refuse_all: bool = False

    def bind(self, name: str, block: ScopeBlock,
             value: LiteralExpression | NumericConstant | UnresolvedConstant = UNRESOLVED_CONSTANT) -> None:
        self.bindings.setdefault(name, []).append(NameBinding(block.module, value))

    def assertion_scope(self, code: list[str], line: int) -> ScopeBlock:
        match = ASSERTION.search(code[line - 1])
        if match is None:
            return ScopeBlock(0, 0, (), uncertain=True)
        offset = self.line_offsets[line - 1] + match.start()
        block = self.block
        while not block.uncertain:
            index = bisect_right(block.children, offset, key=lambda child: child.first) - 1
            if index < 0 or block.children[index].last <= offset:
                break
            block = block.children[index]
        return block

    def expand(self, text: str, scope: ScopeBlock) -> AssertionExpansion:
        """Substitute only a unique, visible, same-file bare literal const."""
        if scope.uncertain or self.refuse_all:
            return AssertionExpansion(text)
        masked = constant_reference_code(text)
        parts: list[str] = []
        numbers: list[NumericOccurrence] = []
        cursor = 0
        displacement = 0
        for identifier in IDENTIFIER.finditer(masked):
            name = identifier[0]
            before, after = masked[:identifier.start()].rstrip(), masked[identifier.end():].lstrip()
            if ("::" in name or before.endswith((".", ":")) or after.startswith(":")
                    or name in ("true", "false")):
                continue
            bindings = self.bindings.get(name, [])
            if len(bindings) != 1 or name in self.refused_names:
                continue
            binding = bindings[0]
            if scope.module[:len(binding.module)] != binding.module:
                continue
            if isinstance(binding.value, NumericConstant):
                type_name = binding.value.declared_type
                if type_name not in self.bindings and type_name not in self.refused_names:
                    numbers.append(NumericOccurrence(identifier.start() + displacement,
                                                     identifier.end() + displacement, binding.value))
            elif isinstance(binding.value, LiteralExpression):
                parts.extend((text[cursor:identifier.start()], binding.value.text))
                cursor = identifier.end()
                displacement += len(binding.value.text) - len(identifier[0])
        parts.append(text[cursor:])
        return AssertionExpansion("".join(parts), tuple(numbers))


@dataclass(frozen=True)
class SourceRevision:
    commit: str
    mode: Literal["commit", "index", "worktree"] = "commit"


@dataclass
class RevisionSourceCache:
    """Read each changed file and build its census once per revision."""
    sources: dict[tuple[SourceRevision, str], list[str]] = field(default_factory=dict)
    tables: dict[tuple[SourceRevision, str], ConstantTable] = field(default_factory=dict)
    files: dict[SourceRevision, set[str]] = field(default_factory=dict)
    unreadable: set[tuple[SourceRevision, str]] = field(default_factory=set)


def source_revision(base: str, head: str, staged: bool, side: Side) -> SourceRevision:
    if side == "old":
        return SourceRevision(base)
    if staged:
        return SourceRevision("", "index")
    return SourceRevision(head) if head else SourceRevision("", "worktree")


def use_bindings(text: str, prefix: tuple[str, ...] = ()) -> Iterable[UseBinding]:
    """Collect possible bindings from use leaves, aliases and groups."""
    for start, end in argument_spans(text):
        item = " ".join(text[start:end].split())
        if "{" in item:
            path, group = item.split("{", 1)
            yield from use_bindings(group.rsplit("}", 1)[0],
                                    prefix + tuple(part for part in path.split("::") if part))
        elif item:
            path, _, alias = item.partition(" as ")
            parts = prefix + tuple(part.strip() for part in path.strip().split("::"))
            if parts[-1] == "self" and len(parts) > 1:
                parts = parts[:-1]
            if parts:
                yield UseBinding(alias.strip() or parts[-1], parts)


def declaration_end(text: str, start: int, stops: str) -> int:
    """Find a delimiter outside nested groups in already masked code."""
    depth = 0
    for index in range(start, len(text)):
        char = text[index]
        if depth == 0 and char in stops:
            return index
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
    return len(text)


def pairing_code_is_certain(code: str) -> bool:
    """Require fully read code and known statement/item macro environments."""
    if not code.isascii() or re.search(r"\br#|\bextern\s+crate\s+\w+\s+as\b", code):
        return False
    tokens = re.compile(r"#\s*!?\s*\[|" + MACRO_INVOCATION.pattern + r"|[{};]")
    cursor = boundary = 0
    while token := tokens.search(code, cursor):
        cursor = token.end()
        if token[0].startswith("#"):
            cursor = declaration_end(code, cursor, "]") + 1
            if re.search(r"\bmacro_use\b", code[token.end():cursor]):
                return False
            if not code[boundary:token.start()].strip():
                boundary = cursor
        elif token["name"]:
            prefix = code[boundary:token.start()].rstrip()
            if prefix.endswith(":") or (token["name"] not in TRUSTED_MACROS and not prefix):
                return False
        else:
            boundary = cursor
    return True


def constant_reference_code(text: str, *, allow_match_ranges: bool = True) -> str:
    """Hide macro tokens except constant endpoints in matches! range patterns."""
    masked = "\n".join(code_lines(text.split("\n"), True))
    assertion = ASSERTION.search(masked)
    parts: list[str] = []
    cursor = 0
    for macro in MACRO_INVOCATION.finditer(masked):
        if macro.start() < cursor or (allow_match_ranges and assertion and macro.start() == assertion.start()):
            continue
        end = min(len(masked), declaration_end(masked, macro.end(), ")]}") + 1)
        replacement = " " * (end - macro.start())
        if allow_match_ranges and macro["name"] == "matches" and masked[end - 1] in ")]}":
            replacement = (" " * (macro.end() - macro.start())
                           + matches_range_endpoint_code(masked[macro.end():end - 1]) + " ")
        parts.extend((masked[cursor:macro.start()], replacement))
        cursor = end
    parts.append(masked[cursor:])
    return "".join(parts)


def matches_range_endpoint_code(code: str) -> str:
    """Expose bare range endpoints only in the second matches! argument."""
    arguments = argument_spans(code)
    result = [" "] * len(code)
    if len(arguments) not in (2, 3) or (len(arguments) == 3 and code[arguments[2][0]:].strip()):
        return "".join(result)
    first, last = arguments[1]
    pattern = constant_reference_code(code[first:last], allow_match_ranges=False)
    guard = re.search(r"\bif\b", pattern)
    if guard:
        pattern = pattern[:guard.start()]
    # Const blocks hold expressions, whose ranges are not range patterns.
    if "{" in pattern:
        return "".join(result)
    for reference in IDENTIFIER.finditer(pattern):
        before, after = pattern[:reference.start()].rstrip(), pattern[reference.end():].lstrip()
        if "::" not in reference[0] and not before.endswith(("-", ":")) and (
                before.endswith(("..", "..=")) or after.startswith("..")):
            result[first + reference.start():first + reference.end()] = reference[0]
    return "".join(result)


def pattern_names(text: str) -> Iterable[str]:
    """Overcount unfamiliar patterns rather than miss a possible binding."""
    text = text[:declaration_end(text, 0, ":")]
    return (match[0] for match in IDENTIFIER.finditer(text)
            if match[0] not in ("const", "mut", "ref", "self", "Self", "_"))


class GenericBindings(NamedTuple):
    names: tuple[str, ...]
    end: int


def generic_bindings(text: str, start: int) -> GenericBindings:
    """Count type and const parameters without counting names in their bounds."""
    opening = start
    while opening < len(text) and text[opening].isspace():
        opening += 1
    if opening == len(text) or text[opening] != "<":
        return GenericBindings((), start)
    names: list[str] = []
    depth = 1
    groups = 0
    part = opening + 1
    for index in range(part, len(text)):
        char = text[index]
        if char in "([{":
            groups += 1
        elif char in ")]}":
            groups -= 1
        elif not groups and char == "<":
            depth += 1
        elif not groups and char == ">" and text[index - 1] != "-":
            depth -= 1
        if (char == "," and depth == 1 and not groups) or depth == 0:
            match = re.match(r"\s*(?:const\s+)?([A-Za-z_]\w*)", text[part:index])
            if match:
                names.append(match[1])
            part = index + 1
        if depth == 0:
            return GenericBindings(tuple(names), index + 1)
    return GenericBindings(tuple(names), len(text))


def constant_table(lines: list[str], code: list[str]) -> ConstantTable:
    """Census all bindings; only module-item consts can supply literals."""
    text, masked = "\n".join(lines), "\n".join(code)
    root = ScopeBlock(0, len(text), (), module_items=True)
    table = ConstantTable(root, [0] + [match.end() for match in re.finditer("\n", text)])
    if not pairing_code_is_certain(masked):
        table.refuse_all = True
        return table
    blocks = [root]
    tokens = re.compile(r"\b(?:const|static|use|mod|let|for|fn|type|struct|enum|union|trait|impl|extern|macro_rules)\b|[{};|]")
    # Unknown keywords may introduce bindings: refuse their following name.
    use_keywords = frozenset(("as", "async", "await", "break", "const", "continue", "crate", "dyn", "else",
                              "enum", "extern", "false", "fn", "for", "if", "impl", "in", "let", "loop", "match",
                              "mod", "move", "mut", "pub", "ref", "return", "self", "Self", "static", "struct",
                              "super", "trait", "true", "type", "union", "unsafe", "use", "where", "while"))
    for arm in re.finditer(r"(?:^|[{,])\s*([^{};]*?)=>", masked):
        for name in pattern_names(arm[1]):
            table.bind(name, root)
    for words in re.finditer(r"(?=\b([A-Za-z_]\w*)\s+([A-Za-z_]\w*)\b)", masked):
        if words[1] not in use_keywords:
            table.refused_names.add(words[2])
    cursor = 0
    boundary = 0
    literal_definitions: set[int] = set()
    while token := tokens.search(masked, cursor):
        cursor = token.end()
        value = token[0]
        block = blocks[-1]
        if value == "{":
            header = masked[boundary:token.start()]
            function = FUNCTION.search(header)
            nested = ScopeBlock(token.start(), len(text), block.module,
                                uncertain=block.uncertain or (block.module_items and not function)
                                or bool(re.search(r"!\s*(?:\w+\s*)?$|=>|\||\b(?:for|mod|impl|trait|enum|struct)\b", header)))
            block.children.append(nested)
            blocks.append(nested)
            boundary = cursor
        elif value == "}":
            if len(blocks) > 1:
                blocks.pop().last = token.end()
            else:
                root.uncertain = True
            boundary = cursor
        elif value == ";":
            boundary = cursor
        elif value in ("let", "for"):
            end = declaration_end(masked, cursor, "=;")
            if value == "for":
                in_keyword = re.search(r"\bin\b", masked[cursor:end])
                if in_keyword:
                    end = cursor + in_keyword.start()
            for name in pattern_names(masked[cursor:end]):
                table.bind(name, block)
        elif value == "|":
            end = masked.find("|", cursor)
            if end >= 0 and not re.search(r"[;{}]", masked[cursor:end]):
                for start, stop in argument_spans(masked[cursor:end]):
                    for name in pattern_names(masked[cursor:end][start:stop]):
                        table.bind(name, block)
                cursor = end + 1
        elif value == "macro_rules":
            table.refuse_all = True
        elif value == "extern":
            imported_crate = re.match(r"\s+crate\s+(\w+)(?:\s+as\s+(\w+))?", masked[cursor:])
            if imported_crate:
                table.bind(imported_crate[2] or imported_crate[1], block)
        elif value in ("fn", "type", "struct", "enum", "union", "trait", "impl"):
            declaration_name = re.match(r"\s+([A-Za-z_]\w*)", masked[cursor:]) if value != "impl" else None
            if declaration_name:
                table.bind(declaration_name[1], block)
                cursor += declaration_name.end()
            generics = generic_bindings(masked, cursor)
            for name in generics.names:
                table.bind(name, block)
            cursor = generics.end
            if value == "enum":
                opening = masked.find("{", cursor)
                if opening >= 0:
                    end = declaration_end(masked, opening + 1, "}")
                    for first, last in argument_spans(masked[opening + 1:end]):
                        variant = re.match(r"\s*(?:#\[[^]]*\]\s*)*(\w+)", masked[opening + 1:end][first:last])
                        if variant:
                            table.bind(variant[1], block)
            if value == "fn":
                opening = masked.find("(", cursor)
                if opening >= 0:
                    parameters = masked[opening + 1:declaration_end(masked, opening + 1, ")")]
                    for start, end in argument_spans(parameters):
                        for name in pattern_names(parameters[start:end]):
                            table.bind(name, block)
        elif value == "mod":
            declaration = MODULE_DECLARATION.match(masked, token.start())
            if not declaration:
                continue
            name = declaration[1]
            table.bind(name, block)
            if block.module_items:
                standard_path = not re.search(
                    r"#\s*\[\s*(?:path\s*=|cfg_attr\b[^]]*\bpath\s*=)", masked[boundary:token.start()])
                cursor = declaration.end()
                boundary = cursor
                if declaration[2] == "{":
                    table.inline_modules.add(block.module + (name,))
                    nested = ScopeBlock(cursor - 1, len(text), block.module + (name,), module_items=True)
                    block.children.append(nested)
                    blocks.append(nested)
                elif standard_path:
                    table.external_modules.add(block.module + (name,))
        elif value == "use":
            end = masked.find(";", cursor)
            if end >= 0:
                for imported in use_bindings(masked[cursor:end].strip()):
                    if imported.name in TRUSTED_MACROS:
                        table.refuse_all = True
                    if imported.name == "*":
                        table.globs.append(GlobImport(block.module, imported.parts[:-1]))
                    else:
                        table.bind(imported.name, block)
                cursor = end + 1
                boundary = cursor
        elif value in ("const", "static"):
            match = CONSTANT_ITEM.match(masked, token.start())
            if match:
                end = declaration_end(masked, match.end(), ";")
                if value == "const" and block.module_items:
                    literal_definitions.add(match.start(1))
                table.bind(match[1], block, literal_expression(text[match.end():end], match["declared_type"].strip())
                           if value == "const" and block.module_items else UNRESOLVED_CONSTANT)
                cursor = match.end()
            else:
                binding = re.match(r"\s+(?:mut\s+)?(\w+)\s*:", masked[cursor:])
                if binding:
                    table.bind(binding[1], block)
    if len(blocks) != 1:
        root.uncertain = True
    for typed_name in re.finditer(r"\b([A-Za-z_]\w*)\s*:(?!:)", masked):
        if typed_name.start(1) not in literal_definitions:
            table.refused_names.add(typed_name[1])
    return table


def numeric_literal(text: str) -> NumericLiteral | UnresolvedConstant:
    """Read unsigned numeric spelling without evaluating or changing its digits."""
    match = NUMERIC_LITERAL.fullmatch(text)
    if not match:
        return UNRESOLVED_CONSTANT
    digits, suffix = match["digits"], match["suffix"] or ""
    floating = suffix.startswith("f") or (not digits.startswith(("0x", "0o", "0b"))
                                          and ("." in digits or "e" in digits.lower()))
    return NumericLiteral(digits, suffix, "f64" if floating else "i32")


def literal_expression(text: str, declared_type: str) -> LiteralExpression | NumericConstant | UnresolvedConstant:
    """Read one exact literal; numeric pairing also needs its declared type."""
    text = "\n".join(code_lines(text.split("\n"), True, keep_literals=True)).strip()
    number = numeric_literal(text)
    if isinstance(number, NumericLiteral):
        if (declared_type in NUMERIC_TYPES and (not number.suffix or number.suffix == declared_type)
                and declared_type.startswith("f") == (number.default_type == "f64")):
            return NumericConstant(number, declared_type)
        return UNRESOLVED_CONSTANT
    if text in ("true", "false"):
        return LiteralExpression(text)
    token = RUST_LITERAL.fullmatch(text)
    if token:
        if text.startswith("'"):
            return LiteralExpression(text)
        if text.startswith(('"', "r")) and re.fullmatch(r"&\s*(?:'static\s+)?str", declared_type):
            return LiteralExpression(text)
    return UNRESOLVED_CONSTANT


def inspect_globs(repo: Path, base: str, head: str, staged: bool, path: str, side: Side,
                  table: ConstantTable, cache: RevisionSourceCache) -> None:
    """Read standard module globs only to refuse possible competing bindings."""
    if not table.globs:
        return
    revision = source_revision(base, head, staged, side)
    if revision not in cache.files:
        arguments = ("ls-files", "--cached") if revision.mode != "commit" else (
            "ls-tree", "-r", "--name-only", revision.commit)
        cache.files[revision] = set(git(repo, *arguments).splitlines())
    source = Path(path)
    directory = source.parent
    if source.name not in ("lib.rs", "main.rs", "mod.rs") and "src" in source.parts:
        directory = source.with_suffix("")
    for glob in table.globs:
        owner, parts = glob.module, glob.parts
        if parts and parts[0] == "crate":
            if source.name not in ("lib.rs", "main.rs"):
                table.refuse_all = True
                continue
            owner, parts = (), parts[1:]
        elif parts and parts[0] == "self":
            parts = parts[1:]
        while parts and parts[0] == "super" and owner:
            owner, parts = owner[:-1], parts[1:]
        target = owner + parts
        if not parts or any(len(table.bindings.get(name, [])) != 1 for name in target):
            table.refuse_all = True
            continue
        if target in table.inline_modules:
            table.refused_names.update(name for name, bindings in table.bindings.items()
                                       if any(binding.module == target for binding in bindings))
            continue
        if target not in table.external_modules:
            table.refuse_all = True
            continue
        module = directory.joinpath(*target)
        candidates = [candidate.as_posix() for candidate in (module.with_suffix(".rs"), module / "mod.rs")
                      if candidate.as_posix() in cache.files[revision]]
        if len(candidates) != 1:
            table.refuse_all = True
            continue
        key = revision, candidates[0]
        try:
            if key in cache.unreadable:
                table.refuse_all = True
                continue
            if key not in cache.tables:
                rows = read_sources(repo, base, head, staged, [], cache,
                                    (SourceFile(candidates[0], side),))[candidates[0], side]
                cache.tables[key] = constant_table(rows, code_lines(rows, True))
            imported = cache.tables[key]
            if imported.refuse_all or imported.globs or imported.block.uncertain:
                table.refuse_all = True
            table.refused_names.update(imported.bindings)
            table.refused_names.update(imported.refused_names)
        except (GuardError, OSError, UnicodeError):
            cache.unreadable.add(key)
            table.refuse_all = True


def diff_path(text: str) -> str:
    if text.startswith('"'):
        value = cast(object, ast.literal_eval(text))
        if not isinstance(value, str):
            raise GuardError("invalid diff path")
        text = value
    return text[2:] if text.startswith(("a/", "b/")) else text


def parse_diff(text: str) -> list[DiffLine]:
    lines: list[DiffLine] = []
    old_path = new_path = ""
    old_number = new_number = hunk = 0
    in_hunk = False
    for row in text.splitlines():
        if in_hunk and row.startswith(("-", "+")):
            content = row[1:]
            if row[0] == "-":
                if old_path.endswith((".rs", ".py")):
                    lines.append(DiffLine(old_path, "old", old_number, content, hunk))
                old_number += 1
            else:
                if new_path.endswith((".rs", ".py")):
                    lines.append(DiffLine(new_path, "new", new_number, content, hunk))
                new_number += 1
        elif row.startswith("diff --git "):
            in_hunk = False
        elif not in_hunk and row.startswith("--- "):
            old_path = diff_path(row[4:])
        elif not in_hunk and row.startswith("+++ "):
            new_path = diff_path(row[4:])
        elif row.startswith("@@ "):
            match = HUNK.match(row)
            if match is None:
                raise GuardError("invalid diff hunk")
            old_number, new_number = int(match[1]), int(match[2])
            hunk += 1
            in_hunk = True
        elif in_hunk and row.startswith(" "):
            old_number += 1
            new_number += 1
    return lines


def argument_spans(text: str) -> list[tuple[int, int]]:
    """Split call arguments without splitting strings or nested expressions."""
    spans: list[tuple[int, int]] = []
    start = 0
    depth = 0
    quote = ""
    escaped = False
    for index, char in enumerate(text):
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
        elif char in "\"'":
            quote = char
        elif char in "([{":
            depth += 1
        elif char in ")]}":
            if depth == 0:
                spans.append((start, index))
                return spans
            depth -= 1
        elif char == "," and depth == 0:
            spans.append((start, index))
            start = index + 1
    spans.append((start, len(text)))
    return spans


def comparison_operands(text: str) -> tuple[tuple[str, ...], bool]:
    """Read a top-level comparison, including one followed by another condition."""
    text = text.strip()
    openings: list[int] = []
    closes: dict[int, int] = {}
    comparisons: list[tuple[int, int, int, str]] = []
    quote = ""
    escaped = False
    for index, char in enumerate(text):
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
        elif char in "\"'":
            quote = char
        elif char in "([{":
            openings.append(index)
        elif char in ")]}":
            if openings:
                closes[openings.pop()] = index
        elif char in "=!<>" or char == "i":
            comparison = COMPARISON.match(text, index)
            if comparison:
                comparisons.append((len(openings), index, comparison.end(), comparison[0]))
    # Remove only parentheses that enclose the whole condition, in one pass.
    start, end, depth = 0, len(text), 0
    while start < end and text[start] == "(" and closes.get(start) == end - 1:
        start, end, depth = start + 1, end - 1, depth + 1
        while start < end and text[start].isspace():
            start += 1
        while start < end and text[end - 1].isspace():
            end -= 1
    for level, index, right, operator in comparisons:
        if level == depth:
            return ((normalized(text[start:index]), normalized(text[right:end])),
                    operator in ("==", "!=", "is"))
    return (normalized(text[start:end]),), False


def parse_assertion(line: DiffLine, hunks: tuple[int, ...], comparison: str) -> Assertion:
    match = ASSERTION.search(code_lines([comparison], line.path.endswith(".rs"))[0])
    if match is None:
        raise GuardError("cannot read assertion")
    name = match.group("macro") or match.group("method") or match.group("bare") or match.group("raises")
    arguments = comparison[match.end():]
    operands = tuple(arguments[start:end].strip() for start, end in argument_spans(arguments)
                     if arguments[start:end].strip())
    exact = name in EXACT_CALLS
    if name in ("assert", "debug_assert", "prop_assert") and operands:
        operands, exact = comparison_operands(operands[0])
    else:
        operands = tuple(normalized(operand) for operand in operands)
    subject_text = normalized(arguments) if match.group("bare") else normalized(comparison)
    return Assertion(line, name, operands, exact, not exact and is_weaker(comparison), hunks, subject_text)


def assertion_spans(lines: list[str], code_rows: list[str], rust: bool,
                    changes: list[DiffLine]) -> Iterable[AssertionSpan]:
    """Scan each assertion once and stop its statement after at most 40 lines."""
    index = 0
    cursor = 0
    statements = code_lines(lines, rust, keep_literals=True)
    text = "\n".join(lines)
    offsets = [0] + [match.end() for match in re.finditer("\n", text)]
    open_literal_rows: set[int] = set()
    for token in lexical_spans(text, rust):
        if token.kind == "literal":
            first_row = bisect_right(offsets, token.first) - 1
            last_row = bisect_right(offsets, token.last - 1) - 1
            open_literal_rows.update(range(first_row, last_row))
    while index < len(lines):
        while cursor < len(changes) and changes[cursor].number <= index:
            cursor += 1
        if cursor == len(changes):
            break
        # A statement more than 39 lines above the next change cannot hold it.
        index = max(index, changes[cursor].number - 40)
        if not is_assertion(code_rows[index]):
            index += 1
            continue
        first = index
        depth = 0
        parts: list[str] = []
        for index in range(first, min(first + 40, len(lines))):
            row = code_rows[index]
            depth += sum(row.count(char) for char in "([{") - sum(row.count(char) for char in ")]}")
            part = statements[index].strip()
            parts.append(part)
            if depth <= 0 and index not in open_literal_rows and not part.endswith("\\"):
                break
        text = " ".join(parts)
        if is_assertion(text):
            yield AssertionSpan(first + 1, index + 1, text,
                                "\n".join(statements[first:index + 1]).strip())
        index += 1


def touched_assertions(lines: list[DiffLine], sources: dict[tuple[str, Side], list[str]],
                       code: dict[tuple[str, Side], list[str]], constants: dict[tuple[str, Side], ConstantTable]) -> list[TouchedAssertion]:
    groups: dict[tuple[str, Side], list[DiffLine]] = defaultdict(list)
    for line in lines:
        groups[line.path, line.side].append(line)
    assertions: list[TouchedAssertion] = []
    for (path, side), changes in groups.items():
        cursor = 0
        for span in assertion_spans(sources[path, side], code[path, side], path.endswith(".rs"), changes):
            while cursor < len(changes) and changes[cursor].number < span.first:
                cursor += 1
            hunks: dict[int, None] = {}
            while cursor < len(changes) and changes[cursor].number <= span.last:
                hunks[changes[cursor].hunk] = None
                cursor += 1
            if hunks:
                source = DiffLine(path, side, span.first, span.text, next(iter(hunks)))
                expansion = AssertionExpansion(span.comparison)
                if path.endswith(".rs"):
                    table = constants[path, side]
                    expansion = table.expand(expansion.text, table.assertion_scope(code[path, side], span.first))
                assertions.append(TouchedAssertion(source, tuple(hunks), expansion.text, expansion.numeric_constants))
            if cursor == len(changes):
                break
    return assertions


def weaker_subjects(additions: list[Assertion], removals: list[Assertion]) -> dict[tuple[int, str], deque[Assertion]]:
    """Find all old subjects in new checks with one shared prefix scan."""
    subjects = {assertion.operands[0] for assertion in removals if assertion.exact and assertion.operands}
    branches: list[dict[str, int]] = [{}]
    fallback = [0]
    endings: list[list[str]] = [[]]
    for subject in subjects:
        node = 0
        for char in subject:
            if char not in branches[node]:
                branches[node][char] = len(branches)
                branches.append({})
                fallback.append(0)
                endings.append([])
            node = branches[node][char]
        endings[node].append(subject)
    queue = deque(branches[0].values())
    while queue:
        node = queue.popleft()
        for char, child in branches[node].items():
            queue.append(child)
            target = fallback[node]
            while target and char not in branches[target]:
                target = fallback[target]
            fallback[child] = branches[target].get(char, 0)
            endings[child].extend(endings[fallback[child]])
    matches: dict[tuple[int, str], deque[Assertion]] = defaultdict(deque)
    for assertion in additions:
        if not assertion.weaker:
            continue
        text = assertion.subject_text
        node = 0
        found: set[str] = set()
        for index, char in enumerate(text):
            while node and char not in branches[node]:
                node = fallback[node]
            node = branches[node].get(char, 0)
            for subject in endings[node]:
                before = index - len(subject)
                after = index + 1
                if ((before < 0 or not (text[before].isalnum() or text[before] == "_"))
                        and (after == len(text) or not (text[after].isalnum() or text[after] == "_"))):
                    found.add(subject)
        for subject in found:
            for hunk in assertion.hunks:
                matches[hunk, subject].append(assertion)
    return matches


def take_replacement(queues: Iterable[deque[Assertion]], paired: set[DiffLine]) -> Assertion | NoReplacement:
    replacement: Assertion | NoReplacement = NoReplacement()
    for queue in queues:
        while queue and queue[0].source in paired:
            _ = queue.popleft()
        if queue and (isinstance(replacement, NoReplacement)
                      or queue[0].source.number < replacement.source.number):
            replacement = queue[0]
    if isinstance(replacement, Assertion):
        paired.add(replacement.source)
    return replacement


def is_weaker(text: str) -> bool:
    return bool(re.search(r"<=|>=|(?<![=!])<|>|\.\.|\bcontains\s*\(|\bmatches!|"
                          + r"assert(?:Less|Greater|In|AlmostEqual|Regex)", text)
                or (WEAKER.search(text) and not re.search(r"==|!=|\bis\b", text)))


def is_assertion(text: str) -> bool:
    return (("assert" in text or "pytest.raises" in text or "self.fail" in text)
            and ASSERTION.search(text) is not None)


def test_lines(lines: list[DiffLine], code: dict[tuple[str, Side], list[str]]) -> list[tuple[DiffLine, str]]:
    tests: list[tuple[DiffLine, str]] = []
    markers: dict[tuple[str, Side], int] = {}
    for line in lines:
        key = line.path, line.side
        text = code[key][line.number - 1]
        if "#[" not in text and "def" not in text and "fn" not in text:
            continue
        if "#[" in text and TEST_MARK.search(text):
            markers[key] = line.number
        match = PYTHON_TEST.search(text) if "def" in text else None
        if match:
            tests.append((line, match[1]))
        elif "fn" in text and line.path.endswith(".rs") and 0 < line.number - markers.get(key, -4) <= 3:
            function = FUNCTION.search(text)
            if function:
                tests.append((line, function[1]))
    return tests


def unmatched_lines(lines: list[DiffLine]) -> list[DiffLine]:
    added = Counter(line.key for line in lines if line.side == "new")
    removed = Counter(line.key for line in lines if line.side == "old")
    unmatched: list[DiffLine] = []
    for line in lines:
        opposite = removed if line.side == "new" else added
        if opposite[line.key]:
            opposite[line.key] -= 1
        else:
            unmatched.append(line)
    return unmatched


type NumericValues = tuple[NumericLiteral | NumericConstant, ...]


class NumericAssertionKey(NamedTuple):
    path: str
    fragments: tuple[str, ...]
    digits: tuple[str, ...]


class NumericComparison(NamedTuple):
    key: NumericAssertionKey
    values: NumericValues


@dataclass
class NumericAssertionBucket:
    old: dict[NumericValues, deque[TouchedAssertion]] = field(default_factory=dict)
    new: dict[NumericValues, deque[TouchedAssertion]] = field(default_factory=dict)


def numeric_comparison(assertion: TouchedAssertion) -> NumericComparison:
    """Keep the whole assertion and every digit exact, with numeric types separate."""
    text = assertion.comparison
    occurrences = list(assertion.numeric_constants)
    for span in lexical_spans(text, True):
        if span.kind == "literal" and not text[:span.first].rstrip().endswith("-"):
            number = numeric_literal(text[span.first:span.last])
            if isinstance(number, NumericLiteral):
                occurrences.append(NumericOccurrence(span.first, span.last, number))
    occurrences.sort(key=lambda occurrence: occurrence.first)
    fragments: list[str] = []
    digits: list[str] = []
    values: list[NumericLiteral | NumericConstant] = []
    cursor = 0
    for occurrence in occurrences:
        fragments.append(normalized(text[cursor:occurrence.first]))
        value = occurrence.value
        digits.append(value.literal.digits if isinstance(value, NumericConstant) else value.digits)
        values.append(value)
        cursor = occurrence.last
    fragments.append(normalized(text[cursor:]))
    return NumericComparison(NumericAssertionKey(assertion.source.path, tuple(fragments), tuple(digits)), tuple(values))


def numeric_pair_matches(old: NumericLiteral | NumericConstant, new: NumericLiteral | NumericConstant) -> bool:
    """A suffix proves the type; an unsuffixed assertion requires the default type."""
    if isinstance(old, NumericConstant) and isinstance(new, NumericConstant):
        return old == new
    if isinstance(old, NumericLiteral) and isinstance(new, NumericLiteral):
        return old == new
    constant = old if isinstance(old, NumericConstant) else new
    literal = old if isinstance(old, NumericLiteral) else new
    if not isinstance(constant, NumericConstant) or not isinstance(literal, NumericLiteral):
        return False
    if literal.digits != constant.literal.digits:
        return False
    if literal.suffix:
        return constant.declared_type == literal.suffix and constant.literal.suffix in ("", literal.suffix)
    return constant.declared_type == literal.default_type and not constant.literal.suffix


def pair_numeric_constants(touched: list[TouchedAssertion]) -> list[TouchedAssertion]:
    """Pair numeric consts only after every occurrence proves the same type and digits."""
    eligible_paths = {assertion.source.path for assertion in touched if assertion.numeric_constants}
    if not eligible_paths:
        return touched
    buckets: dict[NumericAssertionKey, NumericAssertionBucket] = {}
    for assertion in touched:
        if assertion.source.path not in eligible_paths:
            continue
        comparison = numeric_comparison(assertion)
        if not comparison.values:
            continue
        bucket = buckets.setdefault(comparison.key, NumericAssertionBucket())
        side = bucket.old if assertion.source.side == "old" else bucket.new
        side.setdefault(comparison.values, deque()).append(assertion)
    paired: set[DiffLine] = set()
    for bucket in buckets.values():
        for old_values, old_assertions in bucket.old.items():
            for new_values, new_assertions in bucket.new.items():
                if not old_assertions:
                    break
                if (not new_assertions or not any(isinstance(value, NumericConstant)
                                                  for value in (*old_values, *new_values))
                        or not all(numeric_pair_matches(old, new) for old, new in zip(old_values, new_values))):
                    continue
                for _ in range(min(len(old_assertions), len(new_assertions))):
                    paired.add(old_assertions.popleft().source)
                    paired.add(new_assertions.popleft().source)
    return [assertion for assertion in touched if assertion.source not in paired]


def find_changes(lines: list[DiffLine], touched: list[TouchedAssertion],
                 code: dict[tuple[str, Side], list[str]]) -> list[Finding]:
    touched = pair_numeric_constants(touched)
    statement_lines = unmatched_lines([assertion.source._replace(text=assertion.comparison) for assertion in touched])
    remaining = set(statement_lines)
    assertions = [parse_assertion(assertion.source, assertion.hunks, assertion.comparison)
                  for assertion in touched if assertion.source._replace(text=assertion.comparison) in remaining]
    additions = [assertion for assertion in assertions if assertion.source.side == "new"]
    unmatched = [assertion for assertion in assertions if assertion.source.side == "old"]
    weaker = weaker_subjects(additions, unmatched)
    changed: dict[tuple[int, str, str], deque[Assertion]] = defaultdict(deque)
    exact_changes: dict[tuple[int, str, str], deque[Assertion]] = defaultdict(deque)
    stronger: dict[int, deque[Assertion]] = defaultdict(deque)
    marker_keys = {line.key for line in lines if line.side == "new"
                   and ("#[" in line.text or "@" in line.text or "skip" in line.text)
                   and TURNED_OFF.search(code[line.path, line.side][line.number - 1])}
    marker_lines = unmatched_lines([line for line in lines if line.key in marker_keys]) if marker_keys else []
    findings = [Finding(line, "test turned off", "(none)", line.text.strip())
                for line in marker_lines if line.side == "new"]
    for assertion in additions:
        for hunk in assertion.hunks:
            for operand in set(assertion.operands):
                changed[hunk, assertion.name, operand].append(assertion)
                if not assertion.weaker:
                    exact_changes[hunk, assertion.name, operand].append(assertion)
            if assertion.exact:
                stronger[hunk].append(assertion)
    paired: set[DiffLine] = set()
    replacements: dict[DiffLine, Finding] = {}
    # Check all shared subjects before changed values can use the same additions.
    for assertion in unmatched:
        line = assertion.source
        if assertion.exact and assertion.operands:
            candidate = take_replacement((weaker[hunk, assertion.operands[0]] for hunk in assertion.hunks), paired)
            if isinstance(candidate, Assertion):
                replacements[line] = Finding(line, "weaker", line.text.strip(), candidate.source.text.strip())
    for assertion in unmatched:
        line = assertion.source
        if line in replacements:
            continue
        index = exact_changes if assertion.exact else changed
        candidate = take_replacement((index[hunk, assertion.name, operand] for hunk in assertion.hunks
                                      for operand in set(assertion.operands)), paired)
        if isinstance(candidate, Assertion):
            replacements[line] = Finding(line, "changed", line.text.strip(), candidate.source.text.strip())
    for assertion in unmatched:
        line = assertion.source
        if line in replacements:
            findings.append(replacements[line])
        elif not assertion.exact:
            # A stronger exact check can take the place of a bare check.
            candidate = take_replacement((stronger[hunk] for hunk in assertion.hunks), paired)
            if isinstance(candidate, NoReplacement):
                findings.append(Finding(line, "removed", line.text.strip(), "(none)"))
        else:
            findings.append(Finding(line, "removed", line.text.strip(), "(none)"))
    tests = test_lines(lines, code)
    new_tests = {name for line, name in tests if line.side == "new"}
    for line, name in tests:
        if line.side == "old" and name not in new_tests:
            findings.append(Finding(line, "test removed", line.text.strip(), "(none)"))
    return findings


def git(repo: Path, *arguments: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), "-c", "core.quotepath=false", *arguments], capture_output=True,
                            text=True, encoding="utf-8", errors="replace", check=False)
    if result.returncode:
        raise GuardError(result.stderr.strip() or f"git {arguments[0]} failed")
    return result.stdout


def read_sources(repo: Path, base: str, head: str, staged: bool,
                 lines: list[DiffLine], cache: RevisionSourceCache,
                 extra_files: tuple[SourceFile, ...] = ()) -> dict[tuple[str, Side], list[str]]:
    """Read each changed side once; get all revision contents in one batch."""
    sources: dict[tuple[str, Side], list[str]] = {}
    requests: list[tuple[str, Side, str]] = []
    paths: dict[tuple[str, Side], None] = dict.fromkeys((line.path, line.side) for line in lines)
    paths.update(dict.fromkeys(extra_files))
    for path, side in paths:
        cache_key = source_revision(base, head, staged, side), path
        if cache_key in cache.sources:
            sources[path, side] = cache.sources[cache_key]
            continue
        if side == "new" and not staged and not head:
            sources[path, side] = (repo / path).read_text(encoding="utf-8").splitlines()
        else:
            revision = base if side == "old" else "" if staged else head
            if "\n" in path or "\r" in path:
                raise GuardError("cannot read a path with a line break")
            requests.append((path, side, f"{revision}:{path}"))
    if requests:
        result = subprocess.run(["git", "-C", str(repo), "cat-file", "--batch"],
                                input="".join(f"{revision}\n" for _, _, revision in requests).encode("utf-8"),
                                capture_output=True, check=False)
        if result.returncode:
            raise GuardError(result.stderr.decode("utf-8", errors="replace").strip() or "git cat-file failed")
        offset = 0
        for path, side, revision in requests:
            end = result.stdout.find(b"\n", offset)
            fields = result.stdout[offset:end].split() if end >= 0 else []
            if len(fields) != 3 or fields[1] != b"blob":
                raise GuardError(f"cannot read {revision}")
            size = int(fields[2])
            offset = end + 1
            content = result.stdout[offset:offset + size]
            offset += size
            if len(content) != size or result.stdout[offset:offset + 1] != b"\n":
                raise GuardError("incomplete git cat-file output")
            sources[path, side] = content.decode("utf-8", errors="replace").splitlines()
            offset += 1
    for (path, side), rows in sources.items():
        cache.sources[source_revision(base, head, staged, side), path] = rows
    return sources


@dataclass(frozen=True)
class ProductionMergeBranch:
    name: str


@dataclass(frozen=True)
class ProductionBase:
    commit: str


@dataclass(frozen=True)
class NoProductionBase:
    """No registered production provides a usable merge-branch base."""


def worktree_path_matches(value: str, worktree: Path) -> bool:
    """Compare absolute worktree paths, ignoring empty cells and retirement notes."""
    if not value:
        return False
    try:
        path = Path(value).expanduser()
        return path.is_absolute() and path.resolve() == worktree
    except (OSError, RuntimeError, ValueError):
        return False


def production_merge_branch(repo: Path) -> ProductionMergeBranch | NoProductionBase:
    """Match a Units worktree first, then its branch, across registered docs."""
    original_path = sys.path[:]
    try:
        repository_root = Path(__file__).resolve().parents[2]
        sys.path.insert(0, str(repository_root))
        sys.path.insert(0, str(repository_root / "scripts" / "production"))
        from scripts.production import production_units, showrunners
    except ImportError:
        return NoProductionBase()
    finally:
        sys.path[:] = original_path
    worktree = Path(git(repo, "rev-parse", "--show-toplevel").strip()).resolve()
    branch = git(repo, "branch", "--show-current").strip()
    branch_match: ProductionMergeBranch | NoProductionBase = NoProductionBase()
    for doc in dict.fromkeys(showrunners.timer_docs().values()):
        try:
            lines = doc.read_text(encoding="utf-8").splitlines()
            rows = [row for row in production_units.unit_table(lines) if not any(
                re.search(r"(?:^|[\s(`])(?:retired|removed)\b",
                          row.get(column, ""), re.IGNORECASE)
                for column in ("Status", "Worktree"))]
            worktree_matches = any(worktree_path_matches(
                production_units.cell_value(row.get("Worktree", "")), worktree) for row in rows)
            branch_matches = bool(branch) and any(
                production_units.cell_value(row.get("Branch", "")) == branch for row in rows)
            if worktree_matches or branch_matches:
                match = ProductionMergeBranch(production_units.production_field(lines, "Merge branch"))
                if worktree_matches:
                    return match
                if isinstance(branch_match, NoProductionBase):
                    branch_match = match
        except (OSError, UnicodeError, production_units.Refusal):
            continue
    return branch_match


def production_base(repo: Path) -> ProductionBase | NoProductionBase:
    branch = production_merge_branch(repo)
    if isinstance(branch, ProductionMergeBranch):
        try:
            return ProductionBase(git(repo, "merge-base", "HEAD", f"origin/{branch.name}").strip())
        except GuardError:
            pass
    return NoProductionBase()


def default_base(repo: Path, explicit_base: str = "") -> str:
    """Take the newer upstream/production base, or the nearest remote base."""
    if explicit_base:
        return git(repo, "rev-parse", "--verify", f"{explicit_base}^{{commit}}").strip()
    production = production_base(repo)
    try:
        upstream = git(repo, "merge-base", "HEAD", "@{upstream}").strip()
    except GuardError:
        pass
    else:
        if isinstance(production, ProductionBase) and production.commit != upstream:
            # A descendant always has fewer base..HEAD commits. For diverged bases,
            # the same count selects the nearer one; ties keep the upstream base.
            upstream_count = int(git(repo, "rev-list", "--count", f"{upstream}..HEAD").strip())
            production_count = int(git(repo, "rev-list", "--count", f"{production.commit}..HEAD").strip())
            if production_count < upstream_count:
                return production.commit
        return upstream
    default_remote = ""
    try:
        default_remote = git(repo, "symbolic-ref", "refs/remotes/origin/HEAD").strip()
    except GuardError:
        pass
    nearest: dict[str, int] = {}
    if isinstance(production, ProductionBase):
        nearest[production.commit] = int(git(repo, "rev-list", "--count", f"{production.commit}..HEAD").strip())
    for reference in git(repo, "for-each-ref", "--format=%(refname)", "refs/remotes/").splitlines():
        if reference.endswith("/HEAD") or reference == default_remote:
            continue
        try:
            base = git(repo, "merge-base", "HEAD", reference).strip()
            nearest[base] = int(git(repo, "rev-list", "--count", f"{base}..HEAD").strip())
        except GuardError:
            continue
    if nearest:
        distance = min(nearest.values())
        bases = [base for base, count in nearest.items() if count == distance]
        if len(bases) == 1:
            return bases[0]
    raise GuardError("cannot determine branch base; set VERIFY_ASSERTION_BASE (or --base)")


def commit_bases(repo: Path) -> tuple[str, ...]:
    """The parents of the commit being made, including all pending merge heads."""
    bases = [git(repo, "rev-parse", "--verify", "HEAD^{commit}").strip()]
    merge_head = repo / git(repo, "rev-parse", "--git-path", "MERGE_HEAD").strip()
    try:
        merging = merge_head.read_text(encoding="ascii").splitlines()
    except FileNotFoundError:
        merging = []
    for revision in merging:
        bases.append(git(repo, "rev-parse", "--verify", f"{revision}^{{commit}}").strip())
    return tuple(dict.fromkeys(bases))


def name_tests(findings: list[Finding], sources: dict[tuple[str, Side], list[str]]) -> list[Finding]:
    groups: dict[tuple[str, Side], list[Finding]] = defaultdict(list)
    for finding in findings:
        groups[finding.source.path, finding.source.side].append(finding)
    named: list[Finding] = []
    for (path, side), group in groups.items():
        functions = [(number, match[1]) for number, text in enumerate(sources[path, side], 1)
                     if "fn" in text or "def" in text
                     if (match := FUNCTION.search(text))]
        positions = [number for number, _ in functions]
        for finding in group:
            index = bisect_right(positions, finding.source.number) - 1
            named.append(replace(finding, test=functions[index][1] if index >= 0 else "?"))
    removed_tests = {(finding.source.path, finding.test) for finding in named if finding.kind == "test removed"}
    counts = Counter((finding.source.path, finding.test) for finding in named if finding.kind == "removed")
    combined = [replace(finding, removed_assertions=counts[finding.source.path, finding.test])
                if finding.kind == "test removed" else finding
                for finding in named if finding.kind != "removed"
                or (finding.source.path, finding.test) not in removed_tests]
    return sorted(combined, key=lambda finding: (finding.source.path, finding.source.number))


def read_rulings(sources: list[str], messages: str) -> dict[tuple[str, str], str]:
    texts = [messages]
    for source in sources:
        if source == "-":
            texts.append(sys.stdin.read())
        else:
            path = Path(source).expanduser()
            paths = sorted(path.rglob("*.md")) if path.is_dir() else [path]
            texts.extend(item.read_text(encoding="utf-8") for item in paths)
    return {(match[1], match[2]): why
            for text in texts for match in RULING.finditer(text)
            if (why := match[3].strip().rstrip("\"'").strip())}


def findings_against_base(repo: Path, base: str, head: str, staged: bool,
                          cache: RevisionSourceCache) -> list[Finding]:
    """Judge a single parent against the selected tree, index or revision."""
    diff_args = ["diff", "--no-color", "--no-ext-diff", "-U0", "--find-renames"]
    if staged:
        diff_args.append("--cached")
    diff_args.append(base)
    if head:
        diff_args.append(head)
    diff_args.extend(("--", "*.rs", "*.py"))
    diff = git(repo, *diff_args)
    lines = parse_diff(diff)
    sources = read_sources(repo, base, head, staged, lines, cache)
    code = {key: code_lines(rows, key[0].endswith(".rs")) for key, rows in sources.items()}
    constants: dict[tuple[str, Side], ConstantTable] = {}
    for (path, side), rows in sources.items():
        if path.endswith(".rs"):
            key = source_revision(base, head, staged, side), path
            if key not in cache.tables:
                cache.tables[key] = constant_table(rows, code[path, side])
            inspect_globs(repo, base, head, staged, path, side, cache.tables[key], cache)
            constants[path, side] = cache.tables[key]
    assertions = touched_assertions(lines, sources, code, constants)
    return name_tests(find_changes(lines, assertions, code), code)


def finding_identity(finding: Finding) -> tuple[str, str, Kind, str, str]:
    """Match the same change across parents even when source line numbers differ."""
    return (finding.source.path, finding.test, finding.kind, normalized(finding.old), normalized(finding.new))


def run(arguments: list[str]) -> int:
    parser = GuardParser(description=__doc__)
    _ = parser.add_argument("--repo", default=".")
    _ = parser.add_argument("--base", dest="bases", action="append", default=[])
    new_side = parser.add_mutually_exclusive_group()
    _ = new_side.add_argument("--head", default="")
    _ = new_side.add_argument("--staged", action="store_true")
    _ = parser.add_argument("--rulings", action="append", default=[])
    args = GuardArguments()
    _ = parser.parse_args(arguments, namespace=args)
    repo = Path(args.repo).expanduser().resolve()
    repo = Path(git(repo, "rev-parse", "--show-toplevel").strip())
    if args.bases:
        bases = [default_base(repo, base) for base in args.bases]
    else:
        bases = [default_base(repo)]
        bases.extend(git(repo, "rev-list", "--parents", "-n", "1", "HEAD").split()[2:])
    bases = list(dict.fromkeys(bases))
    cache = RevisionSourceCache()
    findings = findings_against_base(repo, bases[0], args.head, args.staged, cache)
    common = Counter(finding_identity(finding) for finding in findings)
    for base in bases[1:]:
        common &= Counter(finding_identity(finding)
                          for finding in findings_against_base(repo, base, args.head, args.staged, cache))
    shared: list[Finding] = []
    for finding in findings:
        identity = finding_identity(finding)
        if common[identity]:
            shared.append(finding)
            common[identity] -= 1
    messages = "\n".join(git(repo, "log", "--format=%B", f"{base}..{args.head or 'HEAD'}", "--")
                         for base in bases)
    rulings = read_rulings(args.rulings, messages)
    blocking = 0
    for finding in shared:
        why = rulings.get((finding.source.path, finding.test), "")
        prefix = "note: " if finding.kind == "changed" else ""
        suffix = f" — ruled: {why}" if why else ""
        count = f" ({finding.removed_assertions} assertions)" if finding.removed_assertions else ""
        print(f"{prefix}{finding.source.path}:{finding.source.number} {finding.test} "
              + f"{finding.kind}{count}: {finding.old} -> {finding.new}{suffix}")
        if finding.kind != "changed" and not why:
            blocking += 1
    if blocking:
        print(f'assertion guard: {blocking} blocking; add "Assertion-Change: <path>::<test> — <why>" '
              + "to the work order or the commit message, or restore the assertion")
    elif not shared:
        print("assertion guard: no removed or weaker assertions")
    return 1 if blocking else 0


def main() -> int:
    try:
        return run(sys.argv[1:])
    except (GuardError, OSError, UnicodeError, ValueError, SyntaxError) as error:
        print("assertion guard: " + " ".join(str(error).splitlines()), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
