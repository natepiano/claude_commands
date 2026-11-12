"""Keep repository checkout creation in copies.py and the named original/mirror paths."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import ast
from collections import deque
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from functools import cache
import io
import json
import os
from pathlib import Path
import re
import sys
from typing import Protocol, cast, override



# Reuse the existing scratch policy behind this combined front.

from scripts.copies import shell_syntax as syntax
from scripts.lib.file_module import load_file_module


class ShellCommands(Protocol):
    def main(self) -> None: ...


SHELL = cast(ShellCommands, cast(object, load_file_module(
    "scratch_guard", Path(__file__).resolve().parents[1] / "hooks/pre-tool-use-scratch-guard.py")))
# Fixture repositories are disposable test inputs, never operational gate checkouts.
TEST_INPUT_REASON = "Tests and fixture/support modules construct isolated disposable repository inputs."
REFUSAL = (
    "Repository trees must be made by copies.py run. "
    + "Example: python3 ~/.claude/scripts/copies/copies.py run "
    + "--repo ~/.claude --commit HEAD -- python3 -m unittest discover. "
    + "A lasting phase tree: bash ~/.claude/scripts/worktree/make_worktree.sh <path> <branch>, from the source repository."
)
GIT_VALUE_OPTIONS = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--config-env", "--super-prefix", "--attr-source"})
COPY_VALUE_OPTIONS = frozenset({"-t", "--target-directory", "-e", "--rsh", "--exclude", "--include",
                                "--filter", "-f", "--chmod", "--chown", "--files-from"})
ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*", re.DOTALL)
REPO_NAME = re.compile(r"(?:^|[_.])(?:repo|repository|checkout|worktree|clone)(?:$|[_.])")
RUNNERS = frozenset({"run", "Popen", "check_call", "check_output", "call", "system", "command"})
TREE_COMMAND = re.compile(r"\bgit\b.*\b(?:clone|worktree\s+add)\b|\b(?:cp|rsync)\b.*-[a-zA-Z]*[arR]")
GIT_MODULES = frozenset({"bulk_run", "scripts.style.bulk_run", "copies", "scripts.copies.copies"})


@dataclass(frozen=True)
class TreeCreation:
    path: Path
    line: int
    operation: str

    @override
    def __str__(self) -> str:
        instruction = "" if self.operation.startswith(("unresolved relevant operation:", "cannot ")) else "; use copies.py run"
        return f"{self.path}:{self.line}: {self.operation}{instruction}"


@dataclass(frozen=True)
class NoForbiddenOperation:
    """No recognized repository-tree operation occurs in the parsed program."""


@dataclass(frozen=True)
class ForbiddenOperation:
    operation: str


@dataclass(frozen=True)
class UnresolvedOperation:
    reason: str


PolicyOutcome = NoForbiddenOperation | ForbiddenOperation | UnresolvedOperation


@dataclass(frozen=True)
class KnownArgument:
    text: str
    glob: bool = False


@dataclass(frozen=True)
class UnknownArgument:
    description: str
    text: str = syntax.UNKNOWN_VALUE


@dataclass(frozen=True)
class UnresolvedChoices:
    """The branch combinations exceed the configured inspection limit."""
    description: str
    text: str = syntax.UNKNOWN_VALUE
    choice_texts: tuple[str, ...] = ()


@dataclass(frozen=True)
class UnknownArguments:
    description: str


@dataclass(frozen=True)
class ForwardedArguments:
    """Abstract function input; concrete verified helper calls supply its argv."""
    description: str


@dataclass(frozen=True)
class ArgumentChoices:
    choices: tuple[tuple[Argument, ...], ...]


Argument = KnownArgument | UnknownArgument | UnresolvedChoices | UnknownArguments | ForwardedArguments | ArgumentChoices


def _retained_choice_texts(words: tuple[Argument, ...]) -> tuple[str, ...]:
    """Keep value fragments without constructing combinations across words."""
    texts: dict[str, None] = {}
    pending = deque(words)
    while pending:
        word = pending.popleft()
        if isinstance(word, ArgumentChoices):
            pending.extend(argument for choice in word.choices for argument in choice)
        elif isinstance(word, (KnownArgument, UnknownArgument, UnresolvedChoices)):
            texts[word.text] = None
            if isinstance(word, UnresolvedChoices):
                texts.update(dict.fromkeys(word.choice_texts))
    return tuple(texts)


@cache
def _choice_limit() -> int:
    from scripts.copies.copies import guard_choice_limit
    return guard_choice_limit()


def _choice_options(choices: tuple[tuple[Argument, ...], ...], description: str) -> tuple[tuple[Argument, ...], ...]:
    options: dict[tuple[Argument, ...], None] = {}
    for choice in choices:
        for vector in _choice_vectors(choice):
            options[vector] = None
            if len(options) > _choice_limit():
                texts = _retained_choice_texts(tuple(argument for choice in choices for argument in choice))
                return ((UnresolvedChoices(description, choice_texts=texts),),)
    return tuple(options)


def _choice_vectors(words: tuple[Argument, ...]) -> tuple[tuple[Argument, ...], ...]:
    """Read a bounded set of combinations and retain unknown values beyond the limit."""
    vectors: list[list[Argument]] = [[]]
    for word in words:
        options = _choice_options(word.choices, "branch choices") if isinstance(word, ArgumentChoices) else ((word,),)
        if len(vectors) * len(options) > _choice_limit():
            options = ((UnresolvedChoices("branch choices", choice_texts=_retained_choice_texts((word,))),),)
        if len(options) == 1:
            for vector in vectors:
                vector.extend(options[0])
        else:
            vectors = [prefix + list(suffix) for prefix in vectors for suffix in options]
    return tuple(tuple(vector) for vector in vectors)


def _merged_arguments(choices: tuple[tuple[Argument, ...], ...], description: str) -> tuple[Argument, ...]:
    options = _choice_options(choices, description)
    return options[0] if len(options) == 1 else (ArgumentChoices(options),)


@dataclass(frozen=True)
class LiteralProgramInput:
    text: str


@dataclass(frozen=True)
class ExpandedProgramInput:
    text: str


@dataclass(frozen=True)
class ProgramWithChoiceTexts:
    """The program retains known text and value fragments above the inspection limit."""
    text: str
    choice_texts: tuple[str, ...]


@dataclass(frozen=True)
class UnknownProgramInput:
    reason: str


ProgramInput = LiteralProgramInput | ExpandedProgramInput | ProgramWithChoiceTexts | UnknownProgramInput
UNAVAILABLE_STDIN = UnknownProgramInput("cannot resolve interpreter stdin")


@dataclass(frozen=True)
class CommandProgram:
    language: str
    source: ProgramInput


@dataclass(frozen=True)
class StdinProgram:
    language: str


@dataclass(frozen=True)
class ScriptProgram:
    """A named script or module; stdin is data, never its program source."""


@dataclass(frozen=True)
class NoProgramInvocation:
    """The interpreter reports information without selecting a program."""


@dataclass(frozen=True)
class OrdinaryInvocation:
    pass


Invocation = CommandProgram | StdinProgram | ScriptProgram | NoProgramInvocation | OrdinaryInvocation


def _program_input(argument: Argument) -> ProgramInput:
    if isinstance(argument, KnownArgument):
        return LiteralProgramInput(argument.text)
    if isinstance(argument, UnknownArgument):
        return ExpandedProgramInput(argument.text)
    if isinstance(argument, UnresolvedChoices):
        return ProgramWithChoiceTexts(argument.text, argument.choice_texts)
    return UnknownProgramInput("cannot resolve interpreter program")


def decode_invocation(words: list[Argument]) -> Invocation:
    """Consume interpreter options before selecting command, stdin or script mode."""
    if not words:
        return OrdinaryInvocation()
    executable = Path(_text(words[0])).name
    if executable == "eval":
        text = " ".join(word.text if isinstance(word, (KnownArgument, UnknownArgument, UnresolvedChoices)) else syntax.UNKNOWN_VALUE
                        for word in words[1:])
        source = (ProgramWithChoiceTexts(text, _retained_choice_texts(tuple(words[1:])))
                  if any(isinstance(word, UnresolvedChoices) for word in words[1:])
                  else LiteralProgramInput(text) if all(isinstance(word, KnownArgument) for word in words[1:])
                  else ExpandedProgramInput(text))
        return CommandProgram("shell", source)
    python = re.fullmatch(r"python(?:[0-9.]+)?", executable) is not None
    if not python and executable not in {"bash", "sh", "zsh"}:
        return OrdinaryInvocation()
    return _python_invocation(words[1:]) if python else _shell_invocation(executable, words[1:])


def _python_invocation(rest: list[Argument]) -> Invocation:
    """CPython options stop at -c, -m, stdin '-', -- or a script operand."""
    index = 0
    while index < len(rest):
        word = rest[index]
        if not isinstance(word, (KnownArgument, UnknownArgument, UnresolvedChoices)):
            return ScriptProgram()
        text = word.text
        if text == "--":
            return StdinProgram("python") if index + 1 == len(rest) or _text(rest[index + 1]) == "-" else ScriptProgram()
        if text == "-":
            return StdinProgram("python")
        if not text.startswith("-"):
            return ScriptProgram()
        if text in {"--help", "--version", "--help-env", "--help-xoptions", "--help-all"}:
            return NoProgramInvocation()
        if text.startswith("--"):
            index += 2 if text == "--check-hash-based-pycs" else 1
            continue
        flags = text[1:]
        for offset, flag in enumerate(flags):
            if flags.startswith(syntax.UNKNOWN_VALUE, offset):
                return ScriptProgram()
            if flag in "?hV":
                return NoProgramInvocation()
            if flag == "c":
                source = ((ProgramWithChoiceTexts(flags[offset + 1:], word.choice_texts) if isinstance(word, UnresolvedChoices)
                           else ExpandedProgramInput(flags[offset + 1:]) if isinstance(word, UnknownArgument)
                           else LiteralProgramInput(flags[offset + 1:])) if offset + 1 < len(flags)
                          else _program_input(rest[index + 1]) if index + 1 < len(rest)
                          else UnknownProgramInput("missing interpreter command"))
                return CommandProgram("python", source)
            if flag == "m":
                return ScriptProgram()
            if flag in "WX":
                if offset + 1 == len(flags):
                    index += 1
                break
        index += 1
    return StdinProgram("python")


def _shell_invocation(executable: str, rest: list[Argument]) -> Invocation:
    """Bash/Zsh invocation grammar; sh uses portable POSIX option meanings.

    Bash long invocation options precede short options. POSIX sh has no
    portable informational option; Python's -h/-V meanings never apply here.
    """
    index = 0
    stdin = False
    long_options = True
    while index < len(rest):
        word = rest[index]
        if not isinstance(word, (KnownArgument, UnknownArgument, UnresolvedChoices)):
            return ScriptProgram()
        text = word.text
        if text == "--" or text == "-":
            index += 1
            break
        if not text.startswith(("-", "+")):
            break
        if text.startswith("--"):
            if executable == "bash" and long_options or executable == "zsh":
                if text in {"--help", "--version"}:
                    return NoProgramInvocation()
                operands = {"--rcfile", "--init-file"} if executable == "bash" else {"--emulate"}
                index += 2 if text in operands else 1
                continue
            return CommandProgram("shell", UnknownProgramInput("unsupported shell long option position"))
        long_options = False
        flags = text[1:]
        for offset, flag in enumerate(flags):
            if flags.startswith(syntax.UNKNOWN_VALUE, offset):
                return ScriptProgram()
            if flag == "c" and text.startswith("-"):
                source = (ProgramWithChoiceTexts(flags[offset + 1:], word.choice_texts) if isinstance(word, UnresolvedChoices)
                          else ExpandedProgramInput(flags[offset + 1:]) if isinstance(word, UnknownArgument)
                          else _program_input(rest[index + 1]) if index + 1 < len(rest)
                          else UnknownProgramInput("missing interpreter command"))
                return CommandProgram("shell", source)
            if flag == "s" and text.startswith("-"):
                stdin = True
            if flag in "oO":
                if offset + 1 == len(flags):
                    index += 1
                break
        index += 1
    return StdinProgram("shell") if stdin or index == len(rest) else ScriptProgram()


def _tree_making_text(text: str) -> bool:
    return (TREE_COMMAND.search(text.replace("\n", " ")) is not None
            or re.search(r"\b(?:copytree|clone_from|worktree)\b|\bRepo\s*\.\s*clone\b", text) is not None)


def inspect_invocation(words: list[Argument], cwd: Path, *, static: bool,
                       stdin: ProgramInput = UNAVAILABLE_STDIN) -> PolicyOutcome:
    """One decoder for shell commands, tool argv and Python subprocess vectors."""
    if any(isinstance(argument, ArgumentChoices) for argument in words):
        outcome: PolicyOutcome = NoForbiddenOperation()
        for vector in _choice_vectors(tuple(words)):
            outcome = _prefer(outcome, inspect_invocation(list(vector), cwd, static=static, stdin=stdin))
        return outcome
    actual = _unwrap(words)
    operation = _operation(actual, cwd, static=static)
    if _copies_command(actual):
        return operation
    invocation = decode_invocation(actual)
    if isinstance(invocation, (ScriptProgram, NoProgramInvocation, OrdinaryInvocation)):
        return operation
    source = stdin if isinstance(invocation, StdinProgram) else invocation.source
    if isinstance(source, UnknownProgramInput):
        # Static abstract shell helpers are checked at concrete call sites.
        return operation if static else _prefer(operation, UnresolvedOperation(source.reason))
    choices: PolicyOutcome = NoForbiddenOperation()
    if isinstance(source, ProgramWithChoiceTexts) and any(
            _tree_making_text(text) for text in (source.text, *source.choice_texts)):
        choices = UnresolvedOperation("cannot resolve interpreter program choices")
    if invocation.language == "shell":
        parsed = inspect_shell(source.text, cwd, static=static)
    else:
        try:
            tree = ast.parse(source.text)
        except SyntaxError:
            if isinstance(source, (ExpandedProgramInput, ProgramWithChoiceTexts)) and _tree_making_text(source.text):
                return _prefer(operation, UnresolvedOperation("cannot parse Python program"))
            return _prefer(operation, choices)
        parsed: PolicyOutcome = NoForbiddenOperation()
        for finding in PythonPolicy(cwd / "<python-command>", static=static).scan(tree):
            result: PolicyOutcome = (UnresolvedOperation(finding.operation.removeprefix("unresolved relevant operation: "))
                                     if finding.operation.startswith("unresolved relevant operation: ")
                                     else ForbiddenOperation(finding.operation))
            parsed = _prefer(parsed, result)
    return _prefer(operation, _prefer(parsed, choices))


def _prefer(left: PolicyOutcome, right: PolicyOutcome) -> PolicyOutcome:
    if isinstance(left, ForbiddenOperation):
        return left
    if isinstance(right, ForbiddenOperation) or isinstance(left, NoForbiddenOperation):
        return right
    return left


def _description(outcome: PolicyOutcome) -> str:
    if isinstance(outcome, ForbiddenOperation):
        return outcome.operation
    if isinstance(outcome, UnresolvedOperation):
        return "unresolved relevant operation: " + outcome.reason
    return ""


def _refusal(outcome: PolicyOutcome) -> str:
    description = _description(outcome)
    return f"{description}: {REFUSAL}" if isinstance(outcome, ForbiddenOperation) else description


def _text(argument: Argument) -> str:
    return argument.text if isinstance(argument, KnownArgument) else ""


def _copies_command(words: list[Argument]) -> bool:
    if not words or not isinstance(words[0], KnownArgument):
        return False
    if Path(words[0].text).name == "copies.py":
        return True
    if re.fullmatch(r"python(?:[0-9.]+)?", Path(words[0].text).name):
        return (len(words) > 1 and Path(_text(words[1])).name == "copies.py"
                or [_text(word) for word in words[1:3]] == ["-m", "scripts.copies.copies"])
    return False


def _operands(words: list[Argument], value_options: frozenset[str]) -> list[Argument]:
    result: list[Argument] = []
    index = 0
    while index < len(words):
        text = _text(words[index])
        if text == "--":
            return result + words[index + 1:]
        if text in value_options:
            index += 2
            continue
        if not text.startswith("-") or isinstance(words[index], UnknownArguments):
            result.append(words[index])
        index += 1
    return result


def _repo_source(word: Argument, cwd: Path, *, static: bool) -> PolicyOutcome:
    if isinstance(word, KnownArgument):
        if (cwd / word.text / ".git").exists():
            return ForbiddenOperation("repository source")
        if word.glob and not static:
            return UnresolvedOperation("recursive copy source contains a glob")
        return NoForbiddenOperation()
    if isinstance(word, ArgumentChoices):
        outcome: PolicyOutcome = NoForbiddenOperation()
        for choice in word.choices:
            for argument in choice:
                outcome = _prefer(outcome, _repo_source(argument, cwd, static=static))
        return outcome
    if static and REPO_NAME.search(word.description.strip("${}")):
        return ForbiddenOperation("repository source")
    return NoForbiddenOperation() if static else UnresolvedOperation("cannot resolve recursive copy source")


def _unwrap(words: list[Argument]) -> list[Argument]:
    while words:
        first = _text(words[0])
        if ASSIGNMENT.fullmatch(first):
            words = words[1:]
        elif Path(first).name in {"env", "sudo", "command", "exec", "nohup", "builtin"}:
            words = words[1:]
            while words and _text(words[0]).startswith("-"):
                count = 2 if _text(words[0]) in {"-u", "--unset", "-C", "--chdir", "--user", "-g"} else 1
                words = words[count:]
        else:
            break
    return words


def _operation(words: list[Argument], cwd: Path, *, static: bool) -> PolicyOutcome:
    """Match only the real command/subcommand, never later operand text."""
    outcome: PolicyOutcome
    if any(isinstance(argument, ArgumentChoices) for argument in words):
        outcome = NoForbiddenOperation()
        for vector in _choice_vectors(tuple(words)):
            outcome = _prefer(outcome, _operation(list(vector), cwd, static=static))
        return outcome
    words = _unwrap(words)
    if not words or _copies_command(words):
        return NoForbiddenOperation()
    if not isinstance(words[0], KnownArgument):
        arguments = [_text(word) for word in words[1:]]
        tree_arguments = (arguments[:1] == ["clone"] or arguments[:2] == ["worktree", "add"]
                          or any(text in {"--archive", "--recursive"}
                                 or re.fullmatch(r"-[A-Za-z]*[arR][A-Za-z]*", text) for text in arguments))
        return (NoForbiddenOperation() if static or isinstance(words[0], UnknownArgument)
                and words[0].description == "expanded value" and not tree_arguments
                else UnresolvedOperation("cannot resolve executable name"))
    program = Path(words[0].text).name
    rest = words[1:]
    if program == "git":
        index = 0
        while index < len(rest):
            argument = rest[index]
            if isinstance(argument, ForwardedArguments):
                return NoForbiddenOperation()
            if not isinstance(argument, KnownArgument):
                return UnresolvedOperation("cannot resolve git subcommand")
            if argument.text == "--":
                index += 1
                break
            if not argument.text.startswith("-"):
                break
            index += 2 if argument.text in GIT_VALUE_OPTIONS else 1
        rest = rest[index:]
        if rest and _text(rest[0]) == "clone":
            return ForbiddenOperation("git clone")
        if rest and _text(rest[0]) == "worktree":
            if len(rest) > 1 and _text(rest[1]) == "add":
                return ForbiddenOperation("git worktree add")
            if len(rest) > 1 and isinstance(rest[1], ForwardedArguments):
                return NoForbiddenOperation()
            if len(rest) > 1 and not isinstance(rest[1], KnownArgument):
                return UnresolvedOperation("cannot resolve git worktree operation")
        return NoForbiddenOperation()
    if program in {"cp", "rsync"}:
        recursive = any(isinstance(word, KnownArgument) and (word.text in {"--archive", "--recursive"}
                        or re.fullmatch(r"-[A-Za-z]*[arR][A-Za-z]*", word.text)) for word in rest)
        if recursive:
            operands = _operands(rest, COPY_VALUE_OPTIONS)
            has_target = any(_text(word) in {"-t", "--target-directory"}
                             or _text(word).startswith("--target-directory=") for word in rest)
            sources = operands if has_target else operands[:-1]
            outcome = NoForbiddenOperation()
            for source in sources:
                result = _repo_source(source, cwd, static=static)
                if isinstance(result, ForbiddenOperation):
                    return ForbiddenOperation(f"{program} recursive repository copy")
                outcome = _prefer(outcome, result)
            return outcome
    return NoForbiddenOperation()


@dataclass
class ShellPolicy:
    cwd: Path
    static: bool
    variables: dict[str, tuple[Argument, ...]]
    directories: set[Path]

    def expand(self, word: syntax.Word) -> list[Argument]:
        value = word.literal()
        if isinstance(value, syntax.LiteralWord):
            text = value.text
            if word.segments and isinstance(word.segments[0], syntax.Literal) and not word.segments[0].quoted:
                text = os.path.expanduser(text)
            glob = any(isinstance(segment, syntax.Literal) and not segment.quoted
                       and any(char in segment.text for char in "*?[") for segment in word.segments)
            return ([UnknownArgument("expanded value", text)] if syntax.UNKNOWN_VALUE in text
                    else [KnownArgument(text, glob)])
        parts: list[Argument] = []
        glob = any(isinstance(segment, syntax.Literal) and not segment.quoted
                   and any(char in segment.text for char in "*?[") for segment in word.segments)
        for segment in word.segments:
            if isinstance(segment, syntax.Literal):
                parts.append(KnownArgument(segment.text))
            elif isinstance(segment, syntax.Parameter):
                key = segment.name.removesuffix("[@]").removesuffix("[*]")
                values = self.variables.get(key, (UnknownArgument(key),))
                whole_parameter = all(part is segment or isinstance(part, syntax.Literal) and not part.text
                                      for part in word.segments)
                if whole_parameter and (segment.name.endswith(("[@]", "[*]"))
                                        or len(values) == 1 and isinstance(values[0], (ForwardedArguments, ArgumentChoices))):
                    return list(values)
                parts.append(values[0] if len(values) == 1 else UnknownArgument(key))
            else:
                parts.append(UnknownArgument("command output" if isinstance(segment, syntax.Execution) else "array"))
        joined: list[tuple[Argument, ...]] = []
        for vector in _choice_vectors(tuple(parts)):
            text = "".join(part.text if isinstance(part, (KnownArgument, UnknownArgument, UnresolvedChoices))
                           else syntax.UNKNOWN_VALUE for part in vector)
            if word.segments and isinstance(word.segments[0], syntax.Literal) and not word.segments[0].quoted:
                text = os.path.expanduser(text)
            if any(isinstance(part, UnresolvedChoices) for part in vector):
                joined.append((UnresolvedChoices("word choices", text, _retained_choice_texts(vector)),))
            elif all(isinstance(part, KnownArgument) for part in vector):
                joined.append((KnownArgument(text, glob),))
            else:
                description = ", ".join(part.description for part in vector if not isinstance(part, (KnownArgument, ArgumentChoices)))
                joined.append((UnknownArgument(description, text),))
        return list(_merged_arguments(tuple(joined), "word choices"))

    def expansions(self, word: syntax.Word) -> PolicyOutcome:
        outcome: PolicyOutcome = NoForbiddenOperation()
        for segment in word.segments:
            programs = ((segment.program,) if isinstance(segment, syntax.Execution)
                        else segment.programs if isinstance(segment, syntax.Parameter) else ())
            for program in programs:
                outcome = _prefer(outcome, self.program(program))
            if isinstance(segment, syntax.ArrayElements):
                for element in segment.words:
                    outcome = _prefer(outcome, self.expansions(element))
        return outcome

    def assignment(self, word: syntax.Word) -> bool:
        if not word.segments or not isinstance(word.segments[0], syntax.Literal):
            return False
        match = re.match(r"([A-Za-z_][A-Za-z0-9_]*)(\+?)=", word.segments[0].text)
        if not match:
            return False
        prefix = word.segments[0]
        remaining = syntax.Word((syntax.Literal(prefix.text[len(match[0]):], prefix.quoted), *word.segments[1:]), word.line)
        arrays = [part for part in remaining.segments if isinstance(part, syntax.ArrayElements)]
        values = (tuple(argument for element in arrays[0].words for argument in self.expand(element))
                  if arrays else tuple(self.expand(remaining)))
        previous = self.variables.get(match[1])
        if match[2] == "+" and previous is not None:
            values = previous + values
        self.variables[match[1]] = values
        return True

    def command(self, command: syntax.SimpleCommand) -> PolicyOutcome:
        outcome: PolicyOutcome = NoForbiddenOperation()
        words: list[Argument] = []
        for word in command.words:
            outcome = _prefer(outcome, self.expansions(word))
            if not words and self.assignment(word):
                continue
            words.extend(self.expand(word))
        for redirect in command.redirects:
            outcome = _prefer(outcome, self.expansions(redirect.target))
            if isinstance(redirect.document, syntax.HereDocument):
                for program in redirect.document.expansions:
                    outcome = _prefer(outcome, self.program(program))
        actual = _unwrap(words)
        if actual and _text(actual[0]) == "cd" and len(actual) == 2 and isinstance(actual[1], KnownArgument):
            self.directories.update(directory / actual[1].text for directory in tuple(self.directories))
        # Expansions run before a service command starts and are never exempted.
        if _copies_command(actual):
            return outcome
        inputs: dict[int, ProgramInput] = {0: UNAVAILABLE_STDIN}
        for redirect in command.redirects:
            if isinstance(redirect.document, syntax.HereDocument):
                inputs[redirect.descriptor] = (ExpandedProgramInput(syntax.expanded_text(redirect.document.body))
                                               if redirect.document.generated else LiteralProgramInput(redirect.document.body))
            elif redirect.operator == "<<<":
                values = self.expand(redirect.target)
                inputs[redirect.descriptor] = (_program_input(values[0]) if len(values) == 1
                                               else UNAVAILABLE_STDIN)
            elif redirect.operator in {"<&", ">&"}:
                values = self.expand(redirect.target)
                source = _text(values[0]) if len(values) == 1 else ""
                moved = source.endswith("-") and source[:-1].isdigit()
                number = source[:-1] if moved else source
                inputs[redirect.descriptor] = (inputs.get(int(number), UNAVAILABLE_STDIN) if number.isdigit()
                                               else UnknownProgramInput("interpreter stdin is closed") if source == "-"
                                               else UNAVAILABLE_STDIN)
                if moved:
                    inputs[int(number)] = UnknownProgramInput("interpreter stdin is closed")
            else:
                # Every replacement invalidates its previous descriptor value,
                # including output/open-read-write redirections on descriptor 0.
                inputs[redirect.descriptor] = UnknownProgramInput("cannot resolve redirected interpreter stdin")
                if redirect.operator in {"&>", "&>>"}:
                    inputs[2] = inputs[redirect.descriptor]
        for directory in self.directories:
            outcome = _prefer(outcome, inspect_invocation(actual, directory, static=self.static, stdin=inputs[0]))
        return outcome

    def program(self, program: syntax.Program) -> PolicyOutcome:
        outcome: PolicyOutcome = NoForbiddenOperation()
        for command in program.commands:
            if isinstance(command, syntax.Compound):
                for word in command.headers:
                    outcome = _prefer(outcome, self.expansions(word))
                previous = dict(self.variables)
                if command.kind == "alternatives":
                    branches: list[dict[str, tuple[Argument, ...]]] = []
                    for branch in command.body.commands:
                        self.variables = dict(previous)
                        outcome = _prefer(outcome, self.program(syntax.Program((branch,))))
                        branches.append(dict(self.variables))
                    self.merge_branches(branches)
                    continue
                if command.kind == "function" and self.static:
                    self.variables["@"] = (ForwardedArguments("function argv"),)
                    self.variables["*"] = (ForwardedArguments("function argv"),)
                outcome = _prefer(outcome, self.program(command.body))
                if command.kind == "function":
                    self.variables = previous
                elif command.kind in {"for", "loop"}:
                    self.merge_branches([previous, self.variables])
            else:
                outcome = _prefer(outcome, self.command(command))
        return outcome

    def merge_branches(self, branches: list[dict[str, tuple[Argument, ...]]]) -> None:
        self.variables = {}
        for key in {key for branch in branches for key in branch}:
            choices = tuple(dict.fromkeys(branch.get(key, (UnknownArgument(key),)) for branch in branches))
            self.variables[key] = _merged_arguments(choices, key)

    def parse(self, source: str) -> PolicyOutcome:
        try:
            return self.program(syntax.parse(source))
        except (syntax.ShellSyntaxError, RecursionError, OSError):
            parsed = self.program(syntax.readable_program(source))
            return parsed if isinstance(parsed, ForbiddenOperation) else NoForbiddenOperation()


def inspect_shell(command: str, cwd: Path, *, static: bool = False) -> PolicyOutcome:
    policy = ShellPolicy(cwd, static, {key: (KnownArgument(value),) for key, value in os.environ.items()}, {cwd})
    return policy.parse(command)


def _first_operation(command: str, cwd: Path, *, static: bool = False) -> str:
    return _description(inspect_shell(command, cwd, static=static))


def refusal(command: str, cwd: Path) -> str:
    return _refusal(inspect_shell(command, cwd))


def hook_refusal(payload: str) -> str:
    try:
        parsed = cast(object, json.loads(payload))
    except ValueError:
        return ""
    if not isinstance(parsed, dict):
        return ""
    fields = cast(dict[str, object], parsed)
    tool_input = fields.get("tool_input")
    if fields.get("tool_name") != "Bash" or not isinstance(tool_input, dict):
        return ""
    command = cast(dict[str, object], tool_input).get("command")
    directory = fields.get("cwd")
    cwd = Path(directory) if isinstance(directory, str) else Path.cwd()
    if isinstance(command, list) and all(isinstance(word, str) for word in cast(list[object], command)):
        return _refusal(inspect_invocation([KnownArgument(word) for word in cast(list[str], command)], cwd, static=False))
    return refusal(command, cwd) if isinstance(command, str) else ""


def _fixture(path: Path) -> bool:
    return (path.name.startswith("test_") or path.stem.endswith(("_test_support", "_fixture", "_test_commands"))
            or "fixtures" in path.parts)


@dataclass(frozen=True)
class ScalarValue:
    argument: Argument


@dataclass(frozen=True)
class ArgumentVector:
    arguments: tuple[Argument, ...]


@dataclass(frozen=True)
class ModuleBinding:
    """A module binding retains its full import path through aliases."""
    name: str


@dataclass(frozen=True)
class KnownGitFunction:
    """The Git function from a module with the known forwarding signature."""


@dataclass(frozen=True)
class FunctionBinding:
    """A name refers to this function, including through an assignment alias."""
    function: DeferredFunction


@dataclass(frozen=True)
class PossibleValues:
    values: tuple[ScalarValue | ArgumentVector | ModuleBinding | KnownGitFunction | FunctionBinding, ...]


PythonValue = ScalarValue | ArgumentVector | ModuleBinding | KnownGitFunction | FunctionBinding | PossibleValues


def _possibilities(value: PythonValue) -> tuple[ScalarValue | ArgumentVector | ModuleBinding | KnownGitFunction | FunctionBinding, ...]:
    return value.values if isinstance(value, PossibleValues) else (value,)


def _union(values: list[PythonValue]) -> PythonValue:
    alternatives: list[ScalarValue | ArgumentVector | ModuleBinding | KnownGitFunction | FunctionBinding] = []
    for value in values:
        for alternative in _possibilities(value):
            if alternative not in alternatives:
                alternatives.append(alternative)
    return alternatives[0] if len(alternatives) == 1 else PossibleValues(tuple(alternatives))


def _join_bindings(target: dict[str, PythonValue], branches: list[dict[str, PythonValue]]) -> None:
    for key in {key for branch in branches for key in branch}:
        target[key] = _union([branch[key] if key in branch else ScalarValue(UnknownArgument(key)) for branch in branches])


def _import_value(name: str) -> ModuleBinding | KnownGitFunction | ScalarValue:
    module, _, member = name.rpartition(".")
    if member == "git" and module in GIT_MODULES:
        return KnownGitFunction()
    if any(module == name or module.startswith(name + ".") for module in GIT_MODULES):
        return ModuleBinding(name)
    return ScalarValue(UnknownArgument(name))


def _python_value(node: ast.AST, bindings: Mapping[str, PythonValue]) -> PythonValue:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        if syntax.UNKNOWN_VALUE in node.value:
            return ScalarValue(UnknownArgument("expanded value", node.value))
        return ScalarValue(KnownArgument(node.value))
    if isinstance(node, ast.Name):
        return bindings.get(node.id, ScalarValue(UnknownArgument(node.id)))
    if isinstance(node, ast.Attribute):
        return _union([_import_value(value.name + "." + node.attr) if isinstance(value, ModuleBinding)
                       else ScalarValue(UnknownArgument(ast.unparse(node)))
                       for value in _possibilities(_python_value(node.value, bindings))])
    if isinstance(node, (ast.List, ast.Tuple)):
        arguments: list[Argument] = []
        for element in node.elts:
            if isinstance(element, ast.Starred):
                value = _python_value(element.value, bindings)
                choices = tuple(tuple(part.arguments) if isinstance(part, ArgumentVector)
                                else (part.argument,) if isinstance(part, ScalarValue) and isinstance(part.argument, ForwardedArguments)
                                else (UnknownArguments(ast.unparse(element.value)),) for part in _possibilities(value))
                arguments.extend(choices[0] if len(choices) == 1 else (ArgumentChoices(choices),))
            else:
                value = _python_value(element, bindings)
                choices = tuple((part.argument,) if isinstance(part, ScalarValue) else (UnknownArgument("nested argv"),)
                                for part in _possibilities(value))
                arguments.extend(choices[0] if len(choices) == 1 else (ArgumentChoices(choices),))
        return ArgumentVector(tuple(arguments))
    if isinstance(node, ast.IfExp):
        left, right = _python_value(node.body, bindings), _python_value(node.orelse, bindings)
        return _union([left, right])
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _python_value(node.left, bindings), _python_value(node.right, bindings)
        alternatives: list[PythonValue] = []
        for first in _possibilities(left):
            for second in _possibilities(right):
                if isinstance(first, ArgumentVector) and isinstance(second, ArgumentVector):
                    alternatives.append(ArgumentVector(first.arguments + second.arguments))
                elif isinstance(first, ScalarValue) and isinstance(second, ScalarValue) and isinstance(first.argument, KnownArgument) and isinstance(second.argument, KnownArgument):
                    alternatives.append(ScalarValue(KnownArgument(first.argument.text + second.argument.text)))
                elif isinstance(first, ScalarValue) and isinstance(second, ScalarValue) and isinstance(first.argument, (KnownArgument, UnknownArgument)) and isinstance(second.argument, (KnownArgument, UnknownArgument)):
                    alternatives.append(ScalarValue(UnknownArgument("expanded value", first.argument.text + second.argument.text)))
                else:
                    alternatives.append(ScalarValue(UnknownArgument(ast.unparse(node))))
        return _union(alternatives)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {"str", "Path"} and len(node.args) == 1:
        return _python_value(node.args[0], bindings)
    return ScalarValue(UnknownArgument(ast.unparse(node)))


def _argv(value: PythonValue) -> list[Argument]:
    if isinstance(value, PossibleValues):
        return [ArgumentChoices(tuple(tuple(_argv(part)) for part in value.values))]
    return (list(value.arguments) if isinstance(value, ArgumentVector) else [value.argument]
            if isinstance(value, ScalarValue) else [UnknownArgument("imported object")])


@dataclass(frozen=True)
class GitForwarder:
    parameters: tuple[str, ...]
    vararg: str
    expression: ast.List | ast.Tuple


def _scope_nodes(node: ast.AST) -> Iterator[ast.AST]:
    """Nodes in one lexical scope, including nested definitions but not their bodies."""
    yield node
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            yield child
        else:
            yield from _scope_nodes(child)


def _git_forwarder(node: ast.FunctionDef | ast.AsyncFunctionDef) -> GitForwarder | None:
    """Verify an argv forwarding definition rather than guessing from its name."""
    if node.args.vararg is None:
        return None
    for expression in _scope_nodes(node):
        if not isinstance(expression, (ast.List, ast.Tuple)) or not expression.elts:
            continue
        first = expression.elts[0]
        if (isinstance(first, ast.Constant) and first.value == "git"
                and any(isinstance(part, ast.Starred) and isinstance(part.value, ast.Name)
                        and part.value.id == node.args.vararg.arg for part in expression.elts)):
            return GitForwarder(tuple(arg.arg for arg in (*node.args.posonlyargs, *node.args.args)), node.args.vararg.arg, expression)
    return None


def _local_names(node: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """Collect names that shadow enclosing bindings throughout a function."""
    names: set[str] = set()
    declarations: set[str] = set()
    for part in _scope_nodes(node):
        if isinstance(part, (ast.Global, ast.Nonlocal)):
            declarations.update(part.names)
        elif isinstance(part, ast.Name) and isinstance(part.ctx, (ast.Store, ast.Del)):
            names.add(part.id)
        elif part is not node and isinstance(part, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(part.name)
        elif isinstance(part, ast.alias):
            names.add(part.asname or part.name.split(".")[0])
        elif isinstance(part, ast.ExceptHandler) and part.name is not None:
            names.add(part.name)
    names.difference_update(declarations)
    names.update(parameter.arg for parameter in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs))
    if node.args.vararg is not None:
        names.add(node.args.vararg.arg)
    if node.args.kwarg is not None:
        names.add(node.args.kwarg.arg)
    return names


@dataclass(frozen=True)
class LexicalScopeSnapshot:
    """The bindings for one lexical scope at a call or after its body."""
    scope: ast.AST
    bindings: dict[str, PythonValue]
    helpers: dict[str, GitForwarder]


@dataclass(frozen=True)
class FunctionExecution:
    """The enclosing scopes that can reach a function execution."""
    enclosing_scopes: tuple[LexicalScopeSnapshot, ...]


@dataclass(frozen=True, eq=False)
class DeferredFunction:
    """A function keeps its lexical scope and the bindings at its calls."""
    definition: ast.FunctionDef | ast.AsyncFunctionDef
    enclosing_scope: ast.AST
    executions: list[FunctionExecution] = field(default_factory=list)


class PythonPolicy:
    def __init__(self, path: Path, *, static: bool = True) -> None:
        self.static: bool = static
        self.path: Path = path
        self.found: list[TreeCreation] = []
        self.module_bindings: dict[str, PythonValue] = {}
        self.module_helpers: dict[str, GitForwarder] = {}
        self.deferred_functions: dict[ast.FunctionDef | ast.AsyncFunctionDef, DeferredFunction] = {}
        self.unscanned_executions: deque[tuple[DeferredFunction, FunctionExecution]] = deque()
        self.final_executions: deque[tuple[DeferredFunction, FunctionExecution]] = deque()
        self.scopes: tuple[LexicalScopeSnapshot, ...] = ()
        self.scope_functions: list[DeferredFunction] = []
        # Verified public signature: copies.git(repo, *arguments) forwards
        # ["git", "-C", str(repo), *arguments]. Local definitions override it.
        self.standard_git: GitForwarder = GitForwarder(("repo",), "arguments", cast(ast.List,
            ast.parse('["git", "-C", str(repo), *arguments]', mode="eval").body))

    def call(self, node: ast.Call, bindings: dict[str, PythonValue], helpers: dict[str, GitForwarder]) -> PolicyOutcome:
        name = node.func.id if isinstance(node.func, ast.Name) else node.func.attr if isinstance(node.func, ast.Attribute) else ""
        arguments = list(node.args)
        arguments.extend(keyword.value for keyword in node.keywords if keyword.arg in {"args", "src"})
        if name == "copytree" and arguments:
            source = _python_value(arguments[0], bindings)
            outcome = _repo_source(_argv(source)[0], self.path.parent, static=True)
            if isinstance(outcome, ForbiddenOperation):
                return ForbiddenOperation("copytree of repository")
            return outcome
        imported_git = any(isinstance(value, KnownGitFunction) for value in _possibilities(_python_value(node.func, bindings)))
        if isinstance(node.func, ast.Name) and name in helpers or imported_git:
            signature = self.standard_git if imported_git else helpers[name]
            values = dict(bindings)
            for index, parameter in enumerate(signature.parameters):
                value = next((keyword.value for keyword in node.keywords if keyword.arg == parameter), None)
                if value is None and index < len(node.args):
                    value = node.args[index]
                values[parameter] = (_python_value(value, bindings) if value is not None
                                     else ScalarValue(UnknownArgument(parameter)))
            rest = node.args[len(signature.parameters):]
            values[signature.vararg] = _python_value(ast.List(elts=list(rest)), bindings)
            return inspect_invocation(_argv(_python_value(signature.expression, values)), self.path.parent, static=self.static)
        if name in RUNNERS and arguments:
            value = _python_value(arguments[0], bindings)
            shell = name == "system" or any(keyword.arg == "shell" and isinstance(keyword.value, ast.Constant)
                                            and keyword.value.value is True for keyword in node.keywords)
            outcome: PolicyOutcome = NoForbiddenOperation()
            for alternative in _possibilities(value):
                if shell and isinstance(alternative, ScalarValue) and isinstance(alternative.argument, (KnownArgument, UnknownArgument)):
                    outcome = _prefer(outcome, inspect_shell(alternative.argument.text, self.path.parent, static=self.static))
                elif isinstance(alternative, ArgumentVector):
                    input_node = next((keyword.value for keyword in node.keywords if keyword.arg == "input"), None)
                    input_value = _python_value(input_node, bindings) if input_node is not None else ScalarValue(UnknownArgument("stdin"))
                    source = _program_input(input_value.argument) if isinstance(input_value, ScalarValue) else UNAVAILABLE_STDIN
                    outcome = _prefer(outcome, inspect_invocation(list(alternative.arguments), self.path.parent,
                                                                 static=self.static, stdin=source))
            return outcome
        return NoForbiddenOperation()

    def expression(self, node: ast.AST, bindings: dict[str, PythonValue], helpers: dict[str, GitForwarder]) -> None:
        if isinstance(node, ast.Call):
            for value in _possibilities(_python_value(node.func, bindings)):
                if isinstance(value, FunctionBinding):
                    self.enqueue(value.function, self.execution(value.function))
            outcome = self.call(node, bindings, helpers)
            if not isinstance(outcome, NoForbiddenOperation):
                self.found.append(TreeCreation(self.path, node.lineno, _description(outcome)))
        for child in ast.iter_child_nodes(node):
            if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                self.expression(child, bindings, helpers)

    def execution(self, pending: DeferredFunction) -> FunctionExecution:
        scopes: list[LexicalScopeSnapshot] = []
        for scope in self.scopes:
            scopes.append(LexicalScopeSnapshot(scope.scope, dict(scope.bindings), dict(scope.helpers)))
            if scope.scope is pending.enclosing_scope:
                break
        return FunctionExecution(tuple(scopes))

    def enqueue(self, pending: DeferredFunction, execution: FunctionExecution) -> None:
        if execution not in pending.executions:
            pending.executions.append(execution)
            self.unscanned_executions.append((pending, execution))

    def body(self, statements: list[ast.stmt], scope: ast.AST, bindings: dict[str, PythonValue],
             helpers: dict[str, GitForwarder]) -> None:
        enclosing_scopes = self.scopes
        enclosing_functions = self.scope_functions
        self.scopes = (*enclosing_scopes, LexicalScopeSnapshot(scope, bindings, helpers))
        self.scope_functions = []
        self.statements(statements, bindings, helpers)
        for pending in self.scope_functions:
            self.final_executions.append((pending, self.execution(pending)))
        self.scopes = enclosing_scopes
        self.scope_functions = enclosing_functions

    def statements(self, statements: list[ast.stmt], bindings: dict[str, PythonValue],
                   helpers: dict[str, GitForwarder]) -> None:
        scopes = self.scopes
        self.scopes = (*scopes[:-1], LexicalScopeSnapshot(scopes[-1].scope, bindings, helpers))
        for statement in statements:
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                signature = _git_forwarder(statement)
                if statement not in self.deferred_functions:
                    self.deferred_functions[statement] = DeferredFunction(statement, self.scopes[-1].scope)
                pending = self.deferred_functions[statement]
                bindings[statement.name] = FunctionBinding(pending)
                _ = helpers.pop(statement.name, None)
                if signature is not None:
                    helpers[statement.name] = signature
                if pending not in self.scope_functions:
                    self.scope_functions.append(pending)
            elif isinstance(statement, ast.ClassDef):
                bindings[statement.name] = ScalarValue(UnknownArgument(statement.name))
                _ = helpers.pop(statement.name, None)
                self.body(statement.body, statement, dict(bindings), dict(helpers))
            elif isinstance(statement, (ast.Import, ast.ImportFrom)):
                for alias in statement.names:
                    if isinstance(statement, ast.Import):
                        name = alias.asname or alias.name.split(".")[0]
                        imported = alias.name if alias.asname else name
                    else:
                        name = alias.asname or alias.name
                        imported = (statement.module + "." if statement.module else "") + alias.name
                    bindings[name] = _import_value(imported)
                    _ = helpers.pop(name, None)
            elif isinstance(statement, (ast.Assign, ast.AnnAssign)):
                value = statement.value
                if value is not None:
                    self.expression(value, bindings, helpers)
                    resolved = _python_value(value, bindings)
                    targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
                    for target in targets:
                        if isinstance(target, ast.Name):
                            bindings[target.id] = resolved
                            _ = helpers.pop(target.id, None)
            elif isinstance(statement, (ast.For, ast.AsyncFor)):
                self.expression(statement.iter, bindings, helpers)
                iterable = statement.iter
                values = ([_python_value(element, bindings) for element in iterable.elts]
                          if isinstance(iterable, (ast.List, ast.Tuple)) else [ArgumentVector((ForwardedArguments("iteration input"),))])
                branches = [dict(bindings)]
                for value in values:
                    branch = dict(bindings)
                    if isinstance(statement.target, ast.Name):
                        branch[statement.target.id] = value
                    self.statements(statement.body, branch, dict(helpers))
                    branches.append(branch)
                _join_bindings(bindings, branches)
                after_else = dict(bindings)
                self.statements(statement.orelse, after_else, dict(helpers))
                _join_bindings(bindings, [dict(bindings), after_else])
            elif isinstance(statement, (ast.If, ast.While, ast.With, ast.AsyncWith, ast.Try, ast.TryStar)):
                incoming = dict(bindings)
                suites: list[list[ast.stmt]] = []
                suites.append(statement.body)
                if isinstance(statement, (ast.If, ast.While)):
                    self.expression(statement.test, bindings, helpers)
                    suites.append(statement.orelse)
                elif isinstance(statement, (ast.With, ast.AsyncWith)):
                    for item in statement.items:
                        self.expression(item.context_expr, bindings, helpers)
                else:
                    suites.extend(handler.body for handler in statement.handlers)
                branches: list[dict[str, PythonValue]] = []
                for suite in suites:
                    branch = dict(incoming)
                    self.statements(suite, branch, dict(helpers))
                    if isinstance(statement, (ast.Try, ast.TryStar)) and suite is statement.body:
                        self.statements(statement.orelse, branch, dict(helpers))
                    branches.append(branch)
                _join_bindings(bindings, branches)
                if isinstance(statement, (ast.Try, ast.TryStar)):
                    self.statements(statement.finalbody, bindings, helpers)
            else:
                self.expression(statement, bindings, helpers)
        self.scopes = scopes

    def function(self, pending: DeferredFunction, execution: FunctionExecution) -> None:
        statement = pending.definition
        module = execution.enclosing_scopes[0]
        self.scopes = execution.enclosing_scopes
        local = dict(self.scopes[-1].bindings)
        local_helpers = dict(self.scopes[-1].helpers)
        for part in _scope_nodes(statement):
            if isinstance(part, ast.Global):
                for name in part.names:
                    local[name] = module.bindings.get(name, ScalarValue(UnknownArgument(name)))
                    _ = local_helpers.pop(name, None)
                    if name in module.helpers:
                        local_helpers[name] = module.helpers[name]
        for name in _local_names(statement):
            local[name] = ScalarValue(UnknownArgument(name))
            _ = local_helpers.pop(name, None)
        if statement.args.vararg is not None:
            local[statement.args.vararg.arg] = ArgumentVector((ForwardedArguments(statement.args.vararg.arg),))
        for parameter in (*statement.args.posonlyargs, *statement.args.args, *statement.args.kwonlyargs):
            if parameter.annotation is not None and any(isinstance(part, ast.Name) and part.id in {"list", "tuple", "Sequence", "Iterable"} for part in ast.walk(parameter.annotation)):
                local[parameter.arg] = ArgumentVector((ForwardedArguments(parameter.arg),))
        self.body(statement.body, statement, local, local_helpers)

    def scan(self, tree: ast.Module) -> list[TreeCreation]:
        self.module_bindings = {}
        self.module_helpers = {"git": self.standard_git}
        self.scopes = ()
        self.body(tree.body, tree, self.module_bindings, self.module_helpers)
        # Calls copy snapshots without argument values or callee assignments.
        # Stable function identities and finite lexical nesting make the queue finite.
        while self.final_executions or self.unscanned_executions:
            while self.final_executions:
                pending, execution = self.final_executions.popleft()
                module = LexicalScopeSnapshot(execution.enclosing_scopes[0].scope,
                                              dict(self.module_bindings), dict(self.module_helpers))
                self.enqueue(pending, FunctionExecution((module, *execution.enclosing_scopes[1:])))
            if self.unscanned_executions:
                pending, execution = self.unscanned_executions.popleft()
                self.function(pending, execution)
        return list(dict.fromkeys(self.found))


def markdown_commands(text: str) -> list[tuple[int, str]]:
    """Only executable examples, not Markdown prose, are shell input."""
    commands: list[tuple[int, str]] = []
    block: list[str] = []
    start = 0
    fenced = False
    for number, line in enumerate(text.splitlines(), 1):
        if line.startswith("```"):
            if fenced:
                commands.append((start, "\n".join(block)))
                block.clear()
                fenced = False
            elif line.strip() in {"```bash", "```sh", "```shell", "```zsh"}:
                fenced = True
                start = number + 1
        elif fenced:
            block.append(line)
        else:
            commands.extend((number, match[1]) for match in re.finditer(r"`([^`]+)`", line)
                            if TREE_COMMAND.search(match[1]))
    return commands


def scan_scripts(root: Path, *, allow_list: Mapping[str, str]) -> list[TreeCreation]:
    """Scan executable script calls, excluding named originals/mirrors and test inputs."""
    found: list[TreeCreation] = []
    for path in sorted(root.rglob("*")):
        if path.suffix not in {".py", ".sh", ".md"} or _fixture(path) or path.relative_to(root).as_posix() in allow_list:
            continue
        text = path.read_text(encoding="utf-8")
        if path.suffix == ".py":
            try:
                tree = ast.parse(text)
            except SyntaxError as error:
                found.append(TreeCreation(path, error.lineno or 1, "cannot parse script"))
                continue
            found.extend(PythonPolicy(path).scan(tree))
        elif path.suffix == ".md":
            for line, command in markdown_commands(text):
                operation = _first_operation(command, path.parent, static=True)
                if operation:
                    found.append(TreeCreation(path, line, operation))
        else:
            operation = _first_operation(text, path.parent, static=True)
            if operation:
                pattern = (r"\bgit\b.*\bworktree\s+add\b" if operation == "git worktree add" else
                           r"\bgit\b.*\bclone\b" if operation == "git clone" else
                           rf"\b{re.escape(operation.split()[0])}\b")
                line = next((number for number, line in enumerate(text.splitlines(), 1)
                             if not line.lstrip().startswith("#") and re.search(pattern, line)), 1)
                found.append(TreeCreation(path, line, operation))
    return sorted(set(found), key=lambda item: (str(item.path), item.line, item.operation))


def hook_timeout() -> float:
    """The shared configured budget used by both hook registries."""
    from scripts.copies.copies import load_config
    return load_config().guard_timeout_seconds


def main() -> None:
    """One registered front for copy policy and the existing scratch policy."""
    payload = sys.stdin.read()
    reason = hook_refusal(payload)
    if reason:
        print(reason + ": provide readable command input." if reason.startswith("unresolved relevant operation:") else reason,
              file=sys.stderr)
        raise SystemExit(2)
    previous = sys.stdin
    try:
        sys.stdin = io.StringIO(payload)
        SHELL.main()
    finally:
        sys.stdin = previous


if __name__ == "__main__":
    main()
