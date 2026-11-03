"""Prove per-file literal extractions; no file writes, ledger access, or build commands.

Accepted requests spell a complete declaration and a placement, for example::

    Add `const LIMIT: usize = 42;` at module level in this file; replace `42` with `LIMIT`.
    Add `const LIMIT: usize = 42;` in fn `run`; replace `42` with `LIMIT`.

Only direct expressions in ordinary, top-level functions are automated. Other Rust
contexts and requests needing interpretation stay with a worker. The judged line
includes its indentation. Callers pass approved findings, never unreviewed ideas.
"""
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol

import style_footprint as rust


class ConstantFinding(Protocol):
    @property
    def finding_id(self) -> str: ...
    @property
    def path(self) -> str: ...
    @property
    def line(self) -> int: ...
    @property
    def text(self) -> str: ...
    @property
    def after(self) -> str: ...


@dataclass(frozen=True)
class ModulePlacement:
    """The file's outer module, not an inline child module."""


@dataclass(frozen=True)
class FunctionPlacement:
    name: str


@dataclass(frozen=True)
class ExactConstant:
    name: str
    declaration: str
    type_tokens: tuple[rust.Token, ...]
    value: rust.Token
    place: ModulePlacement | FunctionPlacement


@dataclass(frozen=True)
class NoExactConstant:
    reason: str = "no exact constant"


@dataclass(frozen=True)
class ConstantRefusal:
    finding_id: str
    reason: str


class TypeProof(Enum):
    NOT_PROVED = "type not proved"
    INTRINSIC = "intrinsic type"
    ANNOTATION = "explicit initializer type"
    CAST = "value-preserving cast"
    DEFAULT = "inference default"


@dataclass(frozen=True)
class KeptConstant:
    finding: ConstantFinding
    constant: ExactConstant
    kind: str = "literal to constant"
    type_proof: TypeProof = TypeProof.NOT_PROVED


@dataclass(frozen=True)
class FileConstants:
    """Same six result fields as bulk_rename.FileRenames; nothing has been written."""
    path: str
    original: str
    edited: str
    kept: tuple[KeptConstant, ...]
    refusals: tuple[ConstantRefusal, ...]


@dataclass(frozen=True)
class ConstantsProved:
    """Every changed token is an approved extraction, including all declarations."""


@dataclass(frozen=True)
class ConstantsNotProved:
    reason: str


# A small explicit request language: never mine a declaration out of arbitrary
# prose that may also ask for other edits, an import, or another destination file.
REQUEST = re.compile(
    r"(?:Add|Define|Reuse) `?(?P<decl>(?:(?:pub(?:\((?:crate|super)\))?)\s+)?"
    + r"const (?P<name>[A-Z_][A-Z_0-9]*):\s*(?P<type>[^=;`]+?)\s*=\s*(?P<value>[^;`]+);)`? "
    + r"(?P<place>at module level in this file|in fn `?[A-Za-z_][A-Za-z_0-9]*`?)"
    + r"(?:; replace `?(?P<old>[^`;]+?)`? with `?(?P<new>[A-Z_][A-Z_0-9]*)`?)?\.?"
)
PRIMITIVES = frozenset({"bool", "char", "u8", "u16", "u32", "u64", "u128", "usize",
                        "i8", "i16", "i32", "i64", "i128", "isize", "f32", "f64"})
TYPE_WORDS = frozenset((name,) for name in PRIMITIVES) | {("&", "str"), ("&", "'", "static", "str")}
NUMBER = re.compile(r"(?:0x[0-9a-fA-F_]+|0o[0-7_]+|0b[01_]+|[0-9][0-9_]*(?:\.[0-9_]+)?"
                    + r"(?:[eE][+-]?[0-9_]+)?)(?:[ui](?:8|16|32|64|128|size)|f(?:32|64))?")


def exact_constant(after: str) -> ExactConstant | NoExactConstant:
    match = REQUEST.fullmatch(after)
    if not match or match['name'] == "_":
        return NoExactConstant()
    declaration_spans = rust.token_spans(match['decl'])
    if any(rust.comment_gaps(match['decl'], declaration_spans, lambda line: line)):
        return NoExactConstant()
    type_tokens = rust.tokens(match['type'])
    type_words = tuple(token.value for token in type_tokens)
    if type_words not in TYPE_WORDS:
        return NoExactConstant()
    values = rust.tokens(match['value'].strip())
    if len(values) != 1 or values[0].kind != "literal":
        return NoExactConstant()
    value = values[0]
    if value.value[0].isdigit() and not NUMBER.fullmatch(value.value):
        return NoExactConstant()
    if match['new'] and (match['new'] != match['name'] or match['old'] != value.value):
        return NoExactConstant()
    place: ModulePlacement | FunctionPlacement = ModulePlacement()
    if match['place'].startswith("in fn "):
        place = FunctionPlacement(match['place'][6:].strip('`'))
        if not match['decl'].startswith("const "):
            return NoExactConstant()
    return ExactConstant(match['name'], match['decl'], type_tokens, value, place)


@dataclass(frozen=True)
class FunctionBody:
    name: str
    start: int
    end: int


class Source:
    """Delimiter locations, with whole-class refusals instead of Rust edge parsing."""
    def __init__(self, text: str) -> None:
        self.spans: tuple[rust.TokenSpan, ...] = rust.token_spans(text)
        self.words: tuple[str, ...] = tuple(span.token.value for span in self.spans)
        self.ends: dict[int, int] = rust.delimiter_ends(self.words)
        self.parents: dict[int, int] = {}
        stack: list[int] = []
        for index, word in enumerate(self.words):
            self.parents[index] = stack[-1] if stack else -1
            if word in {"(", "[", "{"}:
                stack.append(index)
            elif word in {")", "]", "}"}:
                if not stack or self.ends.get(stack.pop()) != index:
                    raise ValueError("unbalanced delimiters")
        if stack:
            raise ValueError("unbalanced delimiters")
        self.macros: tuple[tuple[int, int], ...] = rust.macro_spans(self.words, self.ends)
        self.attributes: tuple[tuple[int, int], ...] = rust.attribute_spans(self.words)
        self.line_tokens: dict[int, list[int]] = {}
        self.literal_sites: dict[tuple[int, rust.Token], list[int]] = {}
        for index, line in enumerate(rust.tokenized_text(text).lines):
            self.line_tokens.setdefault(line, []).append(index)
            if self.spans[index].token.kind == 'literal':
                self.literal_sites.setdefault((line, self.spans[index].token), []).append(index)
        normalized = {word.removeprefix('r#') for word in self.words}
        self.identifiers: dict[str, list[int]] = {}
        for index, span in enumerate(self.spans):
            if span.token.kind == 'identifier':
                self.identifiers.setdefault(span.token.value.removeprefix('r#'), []).append(index)
        self.lines: list[str] = text.splitlines()
        self.begins: dict[int, int] = {end: start for start, end in self.ends.items()}
        self.attribute_ends: set[int] = {end for _, end in self.attributes}
        self.semicolons: dict[int, int] = {}
        finish = len(self.words)
        for index in range(len(self.words) - 1, -1, -1):
            if self.words[index] == ';':
                finish = index
            self.semicolons[index] = finish
        self.uncertain: bool = bool(self.macros or self.attributes or normalized & {"macro", "macro_rules", "macro_use"})
        for index, word in enumerate(self.words):
            if word == "use":
                finish = next((j for j in range(index, len(self.words)) if self.words[j] == ";"), len(self.words))
                self.uncertain |= "*" in self.words[index:finish]
        self.functions: list[FunctionBody] = []
        for index, word in enumerate(self.words):
            if word != "fn" or self.parents[index] != -1 or self.words[index + 2:index + 3] != ("(",):
                continue
            params_end = self.ends.get(index + 2, -1)
            cursor = params_end + 1
            while cursor > 0 and cursor < len(self.words) and self.words[cursor] not in {"{", ";", "<", "where"}:
                cursor += 1
            if cursor > 0 and cursor in self.ends and self.words[cursor] == "{":
                self.functions.append(FunctionBody(self.words[index + 1], cursor, self.ends[cursor]))
        self.bodies: dict[int, FunctionBody] = {}
        self.statements: dict[int, tuple[int, int]] = {}
        for body in self.functions:
            begin = body.start + 1
            for cursor in range(begin, body.end + 1):
                if cursor == body.end or (self.words[cursor] == ';' and self.parents[cursor] == body.start):
                    for site in range(begin, cursor):
                        self.bodies[site] = body
                        self.statements[site] = (begin, cursor)
                    begin = cursor + 1
        self.expressions: dict[tuple[int, int], int] = {}

    def expression(self, index: int, body: FunctionBody) -> bool:
        # Nested blocks, arrays, patterns, generics, attributes and macro trees
        # need Rust semantics. Restrict the entire statement, not one spelling.
        if self.bodies.get(index) != body:
            return False
        begin, end = self.statements[index]
        if (begin, end) in self.expressions:
            return self.expressions[begin, end] <= index < end and self.words[index - 1] != '.'
        expression = self._expression_start(begin, end)
        self.expressions[begin, end] = expression
        return expression <= index < end and self.words[index - 1] != '.'

    def _expression_start(self, begin: int, end: int) -> int:
        words = self.words[begin:end]
        if set(words) & {"{", "}", "[", "]", "<", ">", "=>", "!", "#", "match", "if", "while", "for",
                         "loop", "const", "static", "async", "unsafe", "else", "|", "||"}:
            return end
        expression = begin
        if words[:1] == ("let",):
            expression += 1
            if self.words[expression:expression + 1] == ("mut",):
                expression += 1
            if expression >= end or self.spans[expression].token.kind != "identifier":
                return end
            expression += 1
            if self.words[expression:expression + 1] == (":",):
                expression += 1
                if self.words[expression:expression + 1] not in TYPE_WORDS:
                    return end
                expression += 1
            if self.words[expression:expression + 1] != ("=",):
                return end
            expression += 1
        elif words[:1] == ("return",):
            expression += 1
        if any(word in {"let", "fn", "struct", "enum", "type", "use", "mod", "impl", "trait"}
               for word in self.words[expression:end]):
            return end
        return expression


def _integer_fits(value: str, kind: str) -> bool:
    """Positive integer magnitude, using the smallest pointer width we support."""
    suffix = re.search(r'([ui](?:8|16|32|64|128|size))$', value)
    number = value[:suffix.start()] if suffix else value
    if not re.fullmatch(r'(?:0x[0-9a-fA-F_]+|0o[0-7_]+|0b[01_]+|[0-9][0-9_]*)', number):
        return False
    if not re.fullmatch(r'[ui](?:8|16|32|64|128|size)', kind):
        return False
    magnitude = int(number.replace('_', ''), 0 if number.startswith(('0x', '0o', '0b')) else 10)
    bits = 16 if kind.endswith('size') else int(kind[1:])
    return magnitude < (1 << (bits - (1 if kind.startswith('i') else 0)))


def type_preservation(parsed: Source, site: int, constant: ExactConstant) -> TypeProof:
    """Only the four local proofs authorized for literal extraction."""
    value = constant.value.value
    kind = tuple(token.value for token in constant.type_tokens)
    intrinsic: tuple[str, ...] = ()
    if value.startswith(('"', 'r"', 'r#')):
        intrinsic = ('&', 'str')
        if kind == ('&', "'", 'static', 'str'):
            return TypeProof.INTRINSIC
    elif value.startswith("'"):
        intrinsic = ('char',)
    elif value in {'true', 'false'}:
        intrinsic = ('bool',)
    else:
        suffix = re.search(r'([ui](?:8|16|32|64|128|size)|f(?:32|64))$', value)
        if suffix and NUMBER.fullmatch(value) and not (value.startswith(('0x', '0o', '0b')) and suffix[0].startswith('f')):
            intrinsic = (suffix[0],)
    if intrinsic:
        return TypeProof.INTRINSIC if intrinsic == kind else TypeProof.NOT_PROVED
    if not value[0].isdigit() or not NUMBER.fullmatch(value):
        return TypeProof.NOT_PROVED
    # An initializer must consist of this literal alone, not an operand inside
    # an expression whose intermediate type the annotation does not constrain.
    begin, end = parsed.statements[site]
    words = parsed.words
    prefix = words[begin:site] if site + 1 == end and site - begin <= 10 else ()
    annotated = re.fullmatch(r'(?:let (?:mut )?|const |static (?:mut )?)[A-Za-z_][A-Za-z_0-9]* : (.+) =', ' '.join(prefix))
    if site + 1 == end and annotated and tuple(annotated[1].split()) == kind:
        return TypeProof.ANNOTATION
    if (len(kind) == 1 and words[site + 1:site + 2] == ('as',)
            and site + 2 < end and _integer_fits(value, kind[0])
            and _integer_fits(value, words[site + 2]) and _integer_fits(value, 'i32')):
        return TypeProof.CAST
    integer = bool(re.fullmatch(r'(?:0x[0-9a-fA-F_]+|0o[0-7_]+|0b[01_]+|[0-9][0-9_]*)', value))
    if (integer and kind == ('i32',)) or (not integer and kind == ('f64',)):
        return TypeProof.DEFAULT
    return TypeProof.NOT_PROVED


@dataclass(frozen=True)
class Extraction:
    kept: KeptConstant
    site: int
    insertion: int
    add: bool


@dataclass(frozen=True)
class ConstantPlan:
    source: Source
    extractions: tuple[Extraction, ...]
    refusals: tuple[ConstantRefusal, ...]


def _plan(relative: str, source: str, findings: Sequence[ConstantFinding]) -> ConstantPlan:
    parsed = Source(source)
    lines = source.splitlines(keepends=True)
    extractions: list[Extraction] = []
    refusals: list[ConstantRefusal] = []
    used_sites: set[int] = set()
    constants: dict[tuple[int, str], ExactConstant] = {}
    for finding in findings:
        constant = exact_constant(finding.after)
        reason = ""
        if isinstance(constant, NoExactConstant):
            refusals.append(ConstantRefusal(finding.finding_id, constant.reason))
            continue
        line = finding.line - 1
        # Remove only the line terminator, never indentation or other bytes.
        judged = lines[line].removesuffix('\n').removesuffix('\r') if 0 <= line < len(lines) else ""
        if finding.path != relative or not 0 <= line < len(lines) or judged != finding.text:
            refusals.append(ConstantRefusal(finding.finding_id, "changed since judged"))
            continue
        sites = parsed.literal_sites.get((finding.line, constant.value), [])
        if len(sites) != 1 or sites[0] in used_sites:
            refusals.append(ConstantRefusal(finding.finding_id, "literal is not uniquely located"))
            continue
        site = sites[0]
        body = parsed.bodies.get(site)
        if body is None or not parsed.expression(site, body):
            refusals.append(ConstantRefusal(finding.finding_id, "literal is not an ordinary expression"))
            continue
        type_proof = type_preservation(parsed, site, constant)
        if type_proof is TypeProof.NOT_PROVED:
            refusals.append(ConstantRefusal(finding.finding_id, "type not proved"))
            continue
        insertion = len(parsed.spans)
        if isinstance(constant.place, FunctionPlacement):
            if body.name != constant.place.name or sum(item.name == constant.place.name for item in parsed.functions) != 1:
                refusals.append(ConstantRefusal(finding.finding_id, "no exact constant"))
                continue
            insertion = body.start + 1
        # Macro expansion, glob imports and attributes can introduce names or
        # change scope. Refuse the whole class, even away from this literal.
        key = (insertion, constant.name)
        previous = constants.get(key)
        names = parsed.identifiers.get(constant.name, [])
        add = previous is None
        if previous and (previous.type_tokens != constant.type_tokens or previous.value != constant.value):
            reason = "constant name taken"
        elif names:
            # An existing constant is reused only with one occurrence of its
            # name, in the exact target scope, with equal type and value. Any
            # shadow/import/use ambiguity is intentionally refused.
            name = names[0]
            scope = -1 if isinstance(constant.place, ModulePlacement) else body.start
            finish = parsed.semicolons[name]
            expected = (rust.Token(":", "punctuation"), *constant.type_tokens,
                        rust.Token("=", "punctuation"), constant.value)
            if (len(names) != 1 or name == 0 or parsed.words[name - 1] != "const"
                    or parsed.parents[name] != scope or finish - name - 1 != len(expected)
                    or tuple(span.token for span in parsed.spans[name + 1:finish]) != expected):
                reason = "constant name taken"
            else:
                add = False
        if parsed.uncertain:
            reason = "scope may introduce names"
        # Repeated use of a newly planned name in another scope can shadow a
        # module constant. Do not try to infer which name Rust would resolve.
        if any(old_name == constant.name and old_insertion != insertion for old_insertion, old_name in constants):
            reason = "constant name taken"
        if reason:
            refusals.append(ConstantRefusal(finding.finding_id, reason))
            continue
        constants[key] = constant
        used_sites.add(site)
        extractions.append(Extraction(KeptConstant(finding, constant, type_proof=type_proof), site, insertion, add))
    return ConstantPlan(parsed, tuple(extractions), tuple(refusals))


def _additions(plan: ConstantPlan) -> dict[int, list[str]]:
    additions: dict[int, list[str]] = {}
    for extraction in plan.extractions:
        if extraction.add:
            additions.setdefault(extraction.insertion, []).append(extraction.kept.constant.declaration)
    return additions


def render_constants(source: str, plan: ConstantPlan) -> str:
    """Render planned edits; callers must prove the result before using it."""
    edits: list[tuple[int, int, str]] = []
    for extraction in plan.extractions:
        span = plan.source.spans[extraction.site]
        edits.append((span.start, span.end, extraction.kept.constant.name))
    for index, declarations in _additions(plan).items():
        position = len(source) if index == len(plan.source.spans) else plan.source.spans[index - 1].end
        edits.append((position, position, '\n' + '\n'.join(declarations) + '\n'))
    for start, end, text in sorted(edits, reverse=True):
        source = source[:start] + text + source[end:]
    return source


def constant_check(relative: str, before: str, after: str,
                   findings: Sequence[ConstantFinding]) -> ConstantsProved | ConstantsNotProved:
    """Re-locate all approved sites in the original; account for every output token."""
    try:
        plan = _plan(relative, before, findings)
        if plan.refusals:
            return ConstantsNotProved("a kept finding is not an approved extraction")
        replacements = {extraction.site: extraction.kept.constant.name for extraction in plan.extractions}
        additions = _additions(plan)
        expected: list[rust.Token] = []
        for index in range(len(plan.source.spans) + 1):
            for declaration in additions.get(index, []):
                expected.extend(rust.tokens(declaration))
            if index < len(plan.source.spans):
                expected.append(rust.Token(replacements[index], "identifier") if index in replacements
                                else plan.source.spans[index].token)
        if tuple(expected) != rust.tokens(after):
            return ConstantsNotProved("unexplained token change")
        rendered = render_constants(before, plan)
        expected_gaps = rust.comment_gaps(rendered, rust.token_spans(rendered), lambda line: line)
        if expected_gaps != rust.comment_gaps(after, rust.token_spans(after), lambda line: line):
            return ConstantsNotProved("unexplained comment change")
        return ConstantsProved()
    except (ValueError, rust.UnreadableTokenSpans) as error:
        return ConstantsNotProved(str(error))


def apply_constants(path: Path, relative: str, source: str, findings: Sequence[ConstantFinding]) -> FileConstants:
    """Return proved edits without reading or writing path; caller owns batch validation."""
    _ = path
    try:
        plan = _plan(relative, source, findings)
        edited = render_constants(source, plan)
        kept = tuple(extraction.kept for extraction in plan.extractions)
        proof = constant_check(relative, source, edited, tuple(item.finding for item in kept))
        if isinstance(proof, ConstantsNotProved):
            return FileConstants(relative, source, source, (), tuple(
                ConstantRefusal(finding.finding_id, f"file proof failed: {proof.reason}") for finding in findings))
        return FileConstants(relative, source, edited, kept, plan.refusals)
    except (ValueError, rust.UnreadableTokenSpans) as error:
        return FileConstants(relative, source, source, (), tuple(
            ConstantRefusal(finding.finding_id, f"file proof failed: {error}") for finding in findings))


@dataclass(frozen=True)
class CrateSources:
    """Complete original Rust source snapshot for one Cargo target, keyed by repo path.

    entry is its configured Cargo target root. Missing sources are never read from disk.
    Call separately for different targets; the caller merges only compatible edits.
    """
    entry: str
    files: Mapping[str, str]


@dataclass(frozen=True)
class CratePlacement:
    constants: str


@dataclass(frozen=True)
class CrateRequest:
    constant: ExactConstant
    place: CratePlacement


@dataclass(frozen=True)
class CrateConstantEdit:
    path: str
    original: str
    edited: str
    # A new file differs from an existing empty file, for caller rollback.
    created: bool


@dataclass(frozen=True)
class CrateConstants:
    edits: tuple[CrateConstantEdit, ...]
    kept: tuple[KeptConstant, ...]
    refusals: tuple[ConstantRefusal, ...]


@dataclass(frozen=True)
class CratePlan:
    edits: tuple[CrateConstantEdit, ...]
    kept: tuple[KeptConstant, ...]
    refusals: tuple[ConstantRefusal, ...]
    expected_tokens: tuple[tuple[str, tuple[rust.Token, ...]], ...] = ()


# Explicit declarations remain preferable. The prose form supplies a judged
# name and destination, never a synthesized name. Only intrinsically typed
# literals can supply an omitted type (ordinary strings, chars, bools, suffixes).
CRATE_REQUEST = re.compile(
    r"(?:Add|Define|Reuse) `(?P<decl>[^`]+)` in "
    + r"(?P<dest>[A-Za-z_][A-Za-z_0-9/]*\.rs)(?:; import (?:it|bare) at (?:the )?top)?\.?")
NAMED_REQUEST = re.compile(
    r"(?:Name|Extract|Define|Introduce) (?P<name>[A-Z][A-Z_0-9]+) (?:constant )?in "
    + r"(?:the owning |owning )?(?P<dest>[A-Za-z_][A-Za-z_0-9/]*\.rs)"
    + r"(?: and import (?:it|bare|it bare|at module top))?"
    + r"(?:; (?:import bare|use bare after top import))?"
    + r"(?:[;,] (?:preserve|preserving|retaining|retain) (?:exact query spelling|exact label|label|the exact literal|"
    + r"every configuration value and validation message|exact logging text))?\.?")


def crate_request(finding: ConstantFinding) -> CrateRequest | NoExactConstant:
    after = finding.after
    if re.search(r"(?i)\b(?:computed expected|derive (?:the )?(?:expected|this site)|expected (?:value|values)|geometry helpers)\b", after):
        return NoExactConstant("computed expected values require a worker")
    match = CRATE_REQUEST.fullmatch(after)
    if match:
        constant = exact_constant(f"Add `{match['decl']}` at module level in this file.")
        if isinstance(constant, NoExactConstant):
            return constant
        return CrateRequest(constant, CratePlacement(match['dest']))
    match = NAMED_REQUEST.fullmatch(after)
    if match:
        name, destination = match['name'], match['dest']
    else:
        # Ledger prose often adds preservation notes. A unique explicitly
        # requested identifier and constants destination are data; uncertainty,
        # alternatives and instructions for another transformation are not.
        names: set[str] = set(re.findall(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b", after))
        destinations: set[str] = set(re.findall(r"(?<![A-Za-z0-9_/.])(?:[A-Za-z_][A-Za-z_0-9]*/)*constants\.rs(?![A-Za-z0-9_.])", after))
        if (len(names) != 1 or len(destinations) != 1
                or not re.match(r"(?:Name|Extract|Define|Introduce|One|Use)\b", after)
                or re.search(r"(?i)\b(?:review|if|or|unless|uncertain|compute|derive|calculate|rename|move|merge|delete|remove|rewrite|format|qualified)\b", after)):
            return NoExactConstant()
        name, destination = next(iter(names)), next(iter(destinations))
    literals = [token for token in rust.tokens(finding.text) if token.kind == "literal"]
    if len(literals) != 1:
        return NoExactConstant("literal is not uniquely located")
    value = literals[0].value
    kind = ""
    if value.startswith(('"', 'r"', 'r#')):
        kind = "&str"
    elif value.startswith("'"):
        kind = "char"
    elif value in {"true", "false"}:
        kind = "bool"
    else:
        suffix = re.search(r"([ui](?:8|16|32|64|128|size)|f(?:32|64))$", value)
        if suffix and NUMBER.fullmatch(value) and not (value.startswith(('0x', '0o', '0b')) and suffix[0].startswith('f')):
            kind = suffix[0]
    if not kind:
        return NoExactConstant("no exact constant type")
    constant = exact_constant(f"Add `pub(super) const {name}: {kind} = {value};` at module level in this file.")
    if isinstance(constant, NoExactConstant):
        return constant
    return CrateRequest(constant, CratePlacement(destination))


def _header_end(text: str) -> int:
    """Keep crate inner docs/attributes first, and outer docs attached to their item."""
    cursor = 0
    while cursor < len(text):
        prefix = re.match(r"(?:\s+|//(?!/)[^\n]*(?:\n|$))", text[cursor:])
        if prefix:
            cursor += prefix.end()
            continue
        if text.startswith('/*', cursor):
            # Outer docs belong to the following item. Ordinary and inner doc
            # comments may precede an insertion, but only at their real end.
            if text.startswith('/**', cursor) and not text.startswith('/***', cursor):
                break
            depth = 1
            cursor += 2
            while cursor < len(text) and depth:
                if text.startswith('/*', cursor):
                    depth += 1
                    cursor += 2
                elif text.startswith('*/', cursor):
                    depth -= 1
                    cursor += 2
                else:
                    cursor += 1
            if depth:
                raise ValueError('unterminated header comment')
            continue
        if text[cursor:].startswith("#!["):
            spans = rust.token_spans(text[cursor:])
            words = tuple(span.token.value for span in spans)
            end = rust.delimiter_ends(words).get(2, -1)
            if end < 0:
                raise ValueError("unsupported inner attribute")
            cursor += spans[end].end
            continue
        if text[cursor:].startswith("#!"):
            raise ValueError("unsupported shebang")
        break
    return cursor


def _route_start(parsed: Source, index: int) -> int:
    """Return the complete visibility's start, or refuse an unknown prefix."""
    start = index
    if index and parsed.words[index - 1] == ')':
        opening = parsed.begins[index - 1]
        if opening == 0 or parsed.words[opening - 1] != 'pub':
            raise ValueError('unparsed module visibility')
        visibility = parsed.words[opening + 1:index - 1]
        if visibility not in {('crate',), ('super',), ('self',)} and not re.fullmatch(
                r'in [A-Za-z_][A-Za-z_0-9]*(?: :: [A-Za-z_][A-Za-z_0-9]*)*', ' '.join(visibility)):
            raise ValueError('unparsed module visibility')
        start = opening - 1
    elif index and parsed.words[index - 1] == 'pub':
        start -= 1
    if start and parsed.words[start - 1] not in {';', '}', ']'}:
        raise ValueError('unparsed module prefix')
    return start


class ModuleCoverage(Enum):
    ORDINARY_ROUTES = "prove only ordinary routes for the crate applier"
    EVERY_INSTANCE = "refuse unless every module instance is covered"


def _module_files(snapshot: CrateSources, parsed_files: Mapping[str, Source],
                  coverage: ModuleCoverage = ModuleCoverage.ORDINARY_ROUTES) -> dict[str, tuple[str, ...]]:
    """Resolve only ordinary external modules; no cfg/path attributes or expansion."""
    root = Path(snapshot.entry).parent
    if snapshot.entry not in snapshot.files:
        raise ValueError("crate entry unavailable")
    for path in snapshot.files:
        if Path(path).is_absolute() or ".." in Path(path).parts or str(Path(path)) != path or not Path(path).is_relative_to(root):
            raise ValueError("source outside crate target")
    modules: dict[str, tuple[str, ...]] = {}
    pending: list[tuple[str, tuple[str, ...]]] = [(snapshot.entry, ())]
    while pending:
        path, module = pending.pop()
        if path in modules:
            raise ValueError("ambiguous module graph")
        parsed = parsed_files[path]
        normalized = {word.removeprefix('r#') for word in parsed.words}
        if coverage is ModuleCoverage.EVERY_INSTANCE and any(
                parsed.words[index:index + 2] == ('include', '!') for index in range(len(parsed.words))):
            raise ValueError(f"include loader leaves module instances unproved: {path}")
        opaque = bool(normalized & {'macro', 'macro_rules', 'macro_use'})
        for index, word in enumerate(parsed.words):
            if word == 'use':
                end = next((i for i in range(index, len(parsed.words)) if parsed.words[i] == ';'), len(parsed.words))
                opaque |= '*' in parsed.words[index:end]
        if opaque:
            if coverage is ModuleCoverage.EVERY_INSTANCE:
                raise ValueError(f"opaque module source leaves module instances unproved: {path}")
            continue
        modules[path] = module
        directory = root if path == snapshot.entry else (Path(path).parent if Path(path).name == "mod.rs" else Path(path).with_suffix(""))
        for index, word in enumerate(parsed.words):
            if word != "mod":
                continue
            if parsed.parents[index] != -1 or parsed.words[index + 2:index + 3] != (";",):
                if coverage is ModuleCoverage.EVERY_INSTANCE:
                    raise ValueError(f"nonordinary module route leaves module instances unproved: {path}")
                continue
            # Attribute spans are half-open. Skip an attributed route entirely;
            # cfg/path/custom attributes may make it absent or point elsewhere.
            start = _route_start(parsed, index)
            if start in parsed.attribute_ends:
                if coverage is ModuleCoverage.EVERY_INSTANCE:
                    raise ValueError(f"attributed module route leaves module instances unproved: {path}")
                continue
            name = parsed.words[index + 1]
            if not re.fullmatch(r"[a-z_][a-z_0-9]*", name):
                raise ValueError("unsupported module name")
            choices = [str(directory / f"{name}.rs"), str(directory / name / 'mod.rs')]
            present = [choice for choice in choices if choice in snapshot.files]
            if len(present) != 1:
                raise ValueError("module source missing or ambiguous")
            pending.append((present[0], (*module, name)))
    return modules


def crate_module_paths(snapshot: CrateSources) -> frozenset[str]:
    """Complete target coverage; never return a partial graph to a shared-edit caller."""
    return frozenset(_module_files(snapshot, {path: Source(text) for path, text in snapshot.files.items()},
                                   ModuleCoverage.EVERY_INSTANCE))


def _destination(snapshot: CrateSources, modules: Mapping[str, tuple[str, ...]],
                 parsed_files: Mapping[str, Source], owners: Mapping[Path, list[str]],
                 finding: ConstantFinding, requested: str) -> tuple[str, str, str]:
    root = Path(snapshot.entry).parent
    origin = Path(finding.path)
    if finding.path not in modules or origin.name == 'constants.rs':
        raise ValueError("finding is not in an ordinary crate module")
    # Bare constants.rs means the source's owning directory; never promote a
    # flat foo.rs into a directory just to add constants. Explicit src/ paths
    # are crate-root relative; suffix paths must resolve to one owning ancestor.
    if requested == 'constants.rs':
        destination = origin.parent / 'constants.rs'
    elif requested.startswith('src/'):
        destination = root / requested[4:]
    else:
        matches = [parent / 'constants.rs' for parent in (origin.parent, *origin.parent.parents)
                   if parent.is_relative_to(root) and str(parent / 'constants.rs').endswith('/' + requested)]
        if len(matches) != 1:
            raise ValueError("constant destination is ambiguous")
        destination = matches[0]
    if destination.name != 'constants.rs' or not destination.is_relative_to(root):
        raise ValueError("constant destination is not an owning constants module")
    candidates = owners.get(destination.parent, [])
    if len(candidates) != 1:
        raise ValueError("constants module owner unavailable")
    owner = candidates[0]
    owner_module = modules[owner]
    origin_module = modules[finding.path]
    if origin_module[:len(owner_module)] != owner_module:
        raise ValueError("constant destination is outside the owning scope")
    target = str(destination)
    if target in snapshot.files and modules.get(target) != (*owner_module, 'constants'):
        raise ValueError("constants module is not wired to this target")
    if target not in snapshot.files:
        if str(destination.with_suffix('') / 'mod.rs') in snapshot.files or 'constants' in parsed_files[owner].identifiers:
            raise ValueError('constants module name taken')
    import_path = '::'.join(('crate', *owner_module, 'constants'))
    return target, owner, import_path


class ConstantLayout:
    """A validated, ordered constants file with byte insertion boundaries."""
    def __init__(self, text: str) -> None:
        self.text: str = text
        self.spans: tuple[rust.TokenSpan, ...] = rust.token_spans(text)
        self.sections: dict[str, int] = {}
        self.entries: dict[str, list[tuple[str, int, int]]] = {}
        title = ''
        offset = 0
        for line in text.splitlines(keepends=True):
            end = offset + len(line)
            if line.strip():
                header = re.fullmatch(r'// ([a-z][a-z 0-9_-]*)\n?', line)
                if header:
                    title = header[1]
                    if title in self.sections or not line.endswith('\n'):
                        raise ValueError('constants layout is not organized')
                    self.sections[title] = offset
                    self.entries[title] = []
                else:
                    declaration = re.fullmatch(r'(?:(?:pub(?:\((?:super|crate)\))?)\s+)?const ([A-Z_][A-Z_0-9]*):[^\n]+;\n?', line)
                    if not title or not declaration:
                        raise ValueError('constants layout requires a worker')
                    self.entries[title].append((declaration[1], offset, end))
            offset = end
        if list(self.sections) != sorted(self.sections) or any(
                [name for name, _, _ in entries] != sorted({name for name, _, _ in entries})
                for entries in self.entries.values()):
            raise ValueError('constants layout is not organized')

    def render(self, additions: Mapping[str, str]) -> tuple[str, tuple[rust.Token, ...]]:
        if not additions:
            return self.text, tuple(span.token for span in self.spans)
        edits: dict[int, list[str]] = {}
        if 'extracted values' not in self.sections:
            following = next((offset for title, offset in self.sections.items() if title > 'extracted values'), len(self.text))
            prefix = '\n' if following and not self.text[:following].endswith('\n\n') else ''
            edits[following] = [prefix + '// extracted values\n',
                                *(additions[name] + '\n' for name in sorted(additions)), '\n']
        else:
            entries = self.entries['extracted values']
            cursor = 0
            for name in sorted(additions):
                while cursor < len(entries) and entries[cursor][0] < name:
                    cursor += 1
                position = (entries[cursor][1] if cursor < len(entries) else
                            entries[-1][2] if entries else
                            self.sections['extracted values'] + len('// extracted values\n'))
                prefix = '\n' if position and self.text[position - 1] != '\n' and position not in edits else ''
                edits.setdefault(position, []).append(prefix + additions[name] + '\n')
        return _rewrite(self.text, self.spans, [(position, position, ''.join(lines)) for position, lines in edits.items()])


def _rewrite(text: str, spans: tuple[rust.TokenSpan, ...], edits: Sequence[tuple[int, int, str]]) -> tuple[str, tuple[rust.Token, ...]]:
    """Render once, deriving the required tokens from original spans and edits.

    Tokens introduced by a planned insertion must remain code, even if a bad
    character boundary would instead bury them in an original comment.
    """
    pieces: list[str] = []
    expected: list[rust.Token] = []
    cursor = token = 0
    for start, end, replacement in sorted(edits):
        if start < cursor:
            raise ValueError('overlapping planned edits')
        pieces.extend((text[cursor:start], replacement))
        while token < len(spans) and spans[token].start < start:
            expected.append(spans[token].token)
            token += 1
        expected.extend(rust.tokens(replacement))
        while token < len(spans) and spans[token].start < end:
            token += 1
        cursor = end
    pieces.append(text[cursor:])
    expected.extend(span.token for span in spans[token:])
    return ''.join(pieces), tuple(expected)


def _crate_plan(snapshot: CrateSources, findings: Sequence[ConstantFinding]) -> CratePlan:
    requests: list[tuple[ConstantFinding, CrateRequest]] = []
    refusals: list[ConstantRefusal] = []
    for finding in findings:
        request = crate_request(finding)
        if isinstance(request, NoExactConstant):
            refusals.append(ConstantRefusal(finding.finding_id, request.reason))
        else:
            requests.append((finding, request))
    if not requests:
        return CratePlan((), (), tuple(refusals))
    try:
        parsed_files = {path: Source(source) for path, source in snapshot.files.items()}
        modules = _module_files(snapshot, parsed_files)
    except (ValueError, rust.UnreadableTokenSpans) as error:
        return CratePlan((), (), (*refusals, *(ConstantRefusal(finding.finding_id, str(error))
                                             for finding, _ in requests)))
    root = Path(snapshot.entry).parent
    owners: dict[Path, list[str]] = {}
    for path in modules:
        directory = root if path == snapshot.entry else (Path(path).parent if Path(path).name == 'mod.rs' else Path(path).with_suffix(''))
        owners.setdefault(directory, []).append(path)
    layouts: dict[str, ConstantLayout] = {}
    headers: dict[str, int] = {}
    replacements: dict[str, list[tuple[int, int, str]]] = {}
    imports: dict[str, set[str]] = {}
    wiring: set[str] = set()
    additions: dict[str, dict[str, str]] = {}
    meanings: dict[tuple[str, str], ExactConstant] = {}
    used_sites: set[tuple[str, int]] = set()
    imported_meanings: dict[tuple[str, str], str] = {}
    kept: list[KeptConstant] = []
    for finding, request in requests:
        try:
            constant = request.constant
            target, owner, import_path = _destination(snapshot, modules, parsed_files, owners, finding, request.place.constants)
            parsed = parsed_files[finding.path]
            lines = parsed.lines
            if not 0 < finding.line <= len(lines) or lines[finding.line - 1] != finding.text:
                raise ValueError("changed since judged")
            sites = parsed.literal_sites.get((finding.line, constant.value), [])
            if len(sites) != 1 or (finding.path, sites[0]) in used_sites:
                raise ValueError("literal is not uniquely located")
            site = sites[0]
            body = parsed.bodies.get(site)
            if body is None or not parsed.expression(site, body):
                raise ValueError("literal is not an ordinary expression")
            type_proof = type_preservation(parsed, site, constant)
            if type_proof is TypeProof.NOT_PROVED:
                raise ValueError('type not proved')
            if parsed.uncertain:
                raise ValueError('scope may introduce names')
            if constant.name in parsed.identifiers:
                raise ValueError("constant name taken")
            imported_target = imported_meanings.get((finding.path, constant.name), target)
            if imported_target != target:
                raise ValueError('constant name taken')
            key = (target, constant.name)
            previous = meanings.get(key)
            if previous and (previous.type_tokens != constant.type_tokens or previous.value != constant.value):
                raise ValueError("constant name taken")
            existing = parsed_files.get(target)
            if existing is not None and existing.uncertain:
                raise ValueError('constants scope may introduce names')
            names = existing.identifiers.get(constant.name, []) if existing is not None else []
            add = not previous
            if names and existing is not None:
                name = names[0]
                end = existing.semicolons[name]
                expected = rust.tokens(f': {" ".join(token.value for token in constant.type_tokens)} = {constant.value.value}')
                # Only pub(super) or pub(crate) is accessible to the owning
                # module's children; private/public APIs are not widened.
                visibility = existing.words[max(0, name - 5):name]
                if (len(names) != 1 or existing.parents[name] != -1 or visibility not in
                        {('pub', '(', 'super', ')', 'const'), ('pub', '(', 'crate', ')', 'const')}
                        or end - name - 1 != len(expected)
                        or tuple(span.token for span in existing.spans[name + 1:end]) != expected):
                    raise ValueError("constant name taken")
                add = False
            if not constant.declaration.startswith(('const ', 'pub(super) const ', 'pub(crate) const ')):
                raise ValueError('unsupported constant visibility')
            declaration = ('pub(super) ' if constant.declaration.startswith('const ') else '') + constant.declaration
            if add:
                if target not in layouts:
                    layouts[target] = ConstantLayout(snapshot.files.get(target, ''))
            for path in (finding.path, owner):
                if path not in headers:
                    headers[path] = _header_end(snapshot.files[path])
            span = parsed.spans[site]
            replacements.setdefault(finding.path, []).append((span.start, span.end, constant.name))
            imports.setdefault(finding.path, set()).add(f'use {import_path}::{constant.name};')
            if target not in snapshot.files:
                wiring.add(owner)
            if add:
                additions.setdefault(target, {})[constant.name] = declaration
            meanings[key] = constant
            imported_meanings[(finding.path, constant.name)] = target
            used_sites.add((finding.path, site))
            kept.append(KeptConstant(finding, constant, type_proof=type_proof))
        except ValueError as error:
            refusals.append(ConstantRefusal(finding.finding_id, str(error)))
    outputs: dict[str, str] = {}
    expected_tokens: dict[str, tuple[rust.Token, ...]] = {}
    for target, declarations in additions.items():
        outputs[target], expected_tokens[target] = layouts[target].render(declarations)
    for path in imports.keys() | wiring:
        header = sorted(imports.get(path, set()))
        if path in wiring:
            header.insert(0, 'mod constants;')
        position = headers[path]
        replacements.setdefault(path, []).append((position, position, '\n'.join(header) + '\n\n'))
    for path, changes in replacements.items():
        outputs[path], expected_tokens[path] = _rewrite(snapshot.files[path], parsed_files[path].spans, changes)
    edits = tuple(CrateConstantEdit(path, snapshot.files.get(path, ''), text, path not in snapshot.files)
                  for path, text in sorted(outputs.items()) if path not in snapshot.files or text != snapshot.files[path])
    return CratePlan(edits, tuple(kept), tuple(refusals), tuple(expected_tokens.items()))


def render_crate_constants(plan: CratePlan) -> tuple[CrateConstantEdit, ...]:
    return plan.edits


def crate_constant_check(snapshot: CrateSources, edits: Sequence[CrateConstantEdit],
                         findings: Sequence[ConstantFinding]) -> ConstantsProved | ConstantsNotProved:
    """Re-plan against the complete originals, including new files and mod/use wiring."""
    try:
        plan = _crate_plan(snapshot, findings)
        if plan.refusals:
            return ConstantsNotProved('a kept finding is not an approved extraction')
        expected = {edit.path: edit for edit in plan.edits}
        required_tokens = dict(plan.expected_tokens)
        if len(edits) != len(expected) or {edit.path for edit in edits} != set(expected):
            return ConstantsNotProved('unexplained file change')
        for edit in edits:
            allowed = expected[edit.path]
            if edit.original != allowed.original or edit.created != allowed.created:
                return ConstantsNotProved('original file identity changed')
            if rust.tokens(edit.edited) != required_tokens[edit.path]:
                return ConstantsNotProved('unexplained token change')
            if rust.comment_gaps(edit.edited, rust.token_spans(edit.edited), lambda line: line) != rust.comment_gaps(
                    allowed.edited, rust.token_spans(allowed.edited), lambda line: line):
                return ConstantsNotProved('unexplained comment change')
        return ConstantsProved()
    except (ValueError, rust.UnreadableTokenSpans) as error:
        return ConstantsNotProved(str(error))


def apply_crate_constants(snapshot: CrateSources, findings: Sequence[ConstantFinding]) -> CrateConstants:
    """Pure multi-file applier; A' owns all writes, Cargo validation and rollback."""
    try:
        plan = _crate_plan(snapshot, findings)
        edits = render_crate_constants(plan)
        proof = crate_constant_check(snapshot, edits, tuple(item.finding for item in plan.kept))
        if isinstance(proof, ConstantsProved):
            return CrateConstants(edits, plan.kept, plan.refusals)
        reason = f'crate proof failed: {proof.reason}'
    except (ValueError, rust.UnreadableTokenSpans) as error:
        reason = f'crate proof failed: {error}'
    return CrateConstants((), (), tuple(ConstantRefusal(finding.finding_id, reason) for finding in findings))
