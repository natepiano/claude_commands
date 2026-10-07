"""Read screenshot and related tool calls from Claude and Codex transcripts."""

from __future__ import annotations

import json
import ast
import math
import os
import re
import shlex
import subprocess
from collections.abc import Iterable, Iterator
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Literal, cast, override

Agent = Literal["Claude", "Codex"]
CallKind = Literal["shot", "other"]
ProjectAttribution = Literal["repository", "scratchpad", "last_path", "removed_worktree", "scratchpad_target_missing"]


@dataclass(frozen=True)
class ToolCall:
    start: datetime
    end: datetime
    session_id: str
    transcript_path: str
    agent: Agent
    project: str
    kind: CallKind
    source: str
    command: str = ""
    image_count: int = 0
    project_attribution: ProjectAttribution = "last_path"
    script_path: str = ""
    script_paths: tuple[str, ...] = ()
    cwd: str = ""


PREFILTER = (
    "mcp__brp__", "brp_extras/screenshot", "brp_extras_screenshot", "hana_shot.py shot",
    "grim ", "spectacle ", "screencapture ", "import -window",
    "import -display", "import -screen", "playwright", "browser_take_screenshot",
    "browser_screenshot", "take_screenshot", "world.", "bevy/", "hana/",
)
CODEX_PREFILTER = PREFILTER[:-3]
PARALLEL_SCAN_BYTES = 1_000_000_000
RELATED_WRITE = (
    r"(?:\*\*\* (?:Add|Update) File:|cat\s*>{1,2}|tee\s+|\"name\"\s*:\s*\"Write\")" +
    r"[^\n]*(?:rpc|call|invoke|send_request|method|brp\.(?:sh|py)).{0,48}"
    + r"(?:world\.|bevy/|hana/|brp_extras/)"
)
BRP_SKIPS = (
    "brp_launch", "brp_shutdown", "brp_status", "brp_list", "read_log",
    "delete_logs", "list_logs", "type_guide", "registry_schema", "rpc_discover",
)
SHELL_NAMES = {"Bash", "exec_command", "functions.exec_command", "exec", "functions.exec", "shell_command"}
SHELL_SEPARATORS = {";", "&", "&&", "|", "||", "\n"}
TEXT_PROGRAMS = {"rg", "grep", "cat", "sed", "head", "tail", "find", "echo", "printf", "tee"}
SHELL_HINTS = (
    "brp_extras/", "brp_extras_", "world.", "bevy/", "hana/", "hana_shot.py",
    "playwright", "grim ", "spectacle ", "screencapture ", "import -",
)
NON_SCRIPT_SUFFIXES = {".md", ".rst", ".txt", ".rs", ".ron", ".json", ".toml", ".yaml", ".yml", ".log"}
CLAUDE_LINE_HINTS = (*SHELL_HINTS, "mcp__brp__", "browser", "take_screenshot")
CLAUDE_HINT = re.compile(b"|".join(re.escape(hint.encode()) for hint in CLAUDE_LINE_HINTS))
CODEX_HINT = re.compile(b"|".join(re.escape(hint.encode()) for hint in (*CLAUDE_LINE_HINTS, "apply_patch", "Write", "Edit")))
JS_COMMAND = re.compile(r"\bcmd\s*:\s*(\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`)")
TOOL_RESULT_ID = re.compile(r'"tool_use_id"\s*:\s*"([^\"]+)"')
HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z_0-9]*)\1")
PNG_PATH = re.compile(r"[A-Za-z0-9_./~:-]+\.png\b", re.IGNORECASE)
BRP_METHOD = re.compile(r"[\"']?method[\"']?\s*:\s*[\"']([^\"']+)[\"']")
CODE_CALL = re.compile(r"\b(?:rpc|call|invoke|send_request)\s*\(\s*(['\"])([^'\"]+)\1")
SCRIPT_WRITE = re.compile(r"\b(?:cat\s*>{1,2}\s*|tee\s+)([^\s;]+)")
SCRIPT_HEREDOC_WRITE = re.compile(r"\b(?:cat\s*(>>?)\s*|tee\s+(-a\s+)?)([^\s;]+)")
SCREENSHOT_WORD = re.compile(
    r"brp_extras[/_]screenshot|hana_shot\.py\s+shot|\b(?:grim|spectacle|screencapture)\b|" +
    r"\bimport\s+-(?:window|display|screen)\b|\bplaywright\b[^\n;]*\bscreenshot\b",
    re.IGNORECASE,
)


@dataclass
class RememberedScript:
    content: str
    classification_state: Literal["unread", "not_related", "shot", "other"] = "unread"
    source: str = ""
    screenshot_calls: int = 0
    selected_content: dict[tuple[str, ...], str] = field(default_factory=dict)


@dataclass(frozen=True)
class ScriptRun:
    kind: CallKind
    source: str
    path: str
    screenshot_calls: int
    paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class ScriptInvocation:
    path: str
    arguments: tuple[str, ...]
    cwd: str
    mode: Literal["execute", "source"] = "execute"


@dataclass(frozen=True)
class ShellFunction:
    name: str
    content: str
    start: int
    end: int


class TranscriptScan(list[ToolCall]):
    candidate_file_count: int
    bytes_read: int

    def __init__(self, calls: list[ToolCall], candidate_file_count: int, bytes_read: int) -> None:
        super().__init__(calls)
        self.candidate_file_count = candidate_file_count
        self.bytes_read = bytes_read


def _object(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return cast(dict[str, object], value)
    return {}


def _items(value: object) -> list[object]:
    if isinstance(value, list):
        return cast(list[object], value)
    return []


def _string(value: object) -> str:
    return value if isinstance(value, str) else ""


def _stamp(value: object) -> datetime | None:
    stamp = _string(value)
    if not stamp:
        return None
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None


def _candidate_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    args = ["rg", "-l", "-0", "--no-ignore", "--hidden", "--glob", "*.jsonl"]
    for pattern in PREFILTER:
        args.extend(("-F", "-e", pattern))
    result = subprocess.run([*args, str(root)], capture_output=True, check=False)
    if result.returncode not in (0, 1):
        raise RuntimeError(result.stderr.decode(errors="replace"))
    return [Path(name.decode(errors="replace")) for name in result.stdout.split(b"\0") if name]


def _codex_candidate_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    args = ["rg", "-l", "-0", "--no-ignore", "--hidden", "--glob", "*.jsonl"]
    for pattern in CODEX_PREFILTER:
        args.extend(("-F", "-e", pattern))
    direct = subprocess.run([*args, str(root)], capture_output=True, check=False)
    related = subprocess.run(
        ["rg", "-l", "-0", "--pcre2", "--no-ignore", "--hidden", "--glob", "*.jsonl", "-e", RELATED_WRITE, str(root)],
        capture_output=True, check=False,
    )
    for result in (direct, related):
        if result.returncode not in (0, 1):
            raise RuntimeError(result.stderr.decode(errors="replace"))
    names = set(direct.stdout.split(b"\0")) | set(related.stdout.split(b"\0"))
    return [Path(name.decode(errors="replace")) for name in names if name]


def _claude_encoded(path: Path) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def _scratchpad_project(cwd: str, known_cwds: set[str]) -> tuple[str, ProjectAttribution]:
    parts = Path(cwd).parts
    for index, part in enumerate(parts):
        if not part.startswith("claude-") or index + 1 >= len(parts):
            continue
        encoded = parts[index + 1]
        if not encoded.startswith("-") or "scratchpad" not in parts[index + 2:]:
            continue
        same_session = [name for name in known_cwds if _claude_encoded(Path(name)) == encoded]
        if same_session:
            project, _ = _project(max(same_session, key=len), {})
            return project, "scratchpad"
        frontier = [Path("/")]
        best = Path("/")
        while frontier:
            path = frontier.pop()
            prefix = _claude_encoded(path)
            if len(prefix) > len(_claude_encoded(best)):
                best = path
            if prefix == encoded:
                project, _ = _project(str(path), {})
                return project, "scratchpad"
            if not encoded.startswith(("" if path == Path("/") else prefix) + "-"):
                continue
            try:
                children = path.iterdir()
                frontier.extend(child for child in children if child.is_dir()
                                and encoded.startswith(_claude_encoded(child)))
            except OSError:
                continue
        remainder = encoded[len(_claude_encoded(best)):].strip("-")
        if remainder and best != Path("/"):
            parent_project, parent_attribution = _project(str(best), {})
            if parent_attribution == "repository":
                return (parent_project + "-" + remainder, "scratchpad_target_missing")
        return (remainder or best.name, "scratchpad_target_missing")
    return "", "last_path"


def _project(cwd: str, cache: dict[str, tuple[str, ProjectAttribution]],
             known_cwds: set[str] | None = None) -> tuple[str, ProjectAttribution]:
    if cwd in cache:
        return cache[cwd]
    scratchpad = _scratchpad_project(cwd, known_cwds or set()) if "scratchpad" in cwd and "claude-" in cwd else ("", "last_path")
    if scratchpad[0]:
        cache[cwd] = scratchpad
        return cache[cwd]
    original = Path(cwd).expanduser()
    current = original
    while not current.exists() and current != current.parent:
        current = current.parent
    if current.exists():
        result = subprocess.run(
            ["git", "-C", str(current), "rev-parse", "--path-format=absolute", "--git-common-dir"],
            capture_output=True, text=True, check=False,
        )
        if result.returncode == 0:
            common_dir = Path(result.stdout.strip()).resolve()
            repo = common_dir.parent if common_dir.name == ".git" else current
            cache[cwd] = (repo.name, "repository")
            return cache[cwd]
    cache[cwd] = (original.name or "unknown", "last_path")
    return cache[cwd]


def _exec_shell_commands(value: str) -> str:
    if "tools.exec_command" not in value:
        return ""
    commands: list[str] = []
    for match in JS_COMMAND.finditer(value):
        literal = match.group(1)
        if literal.startswith('"'):
            try:
                commands.append(_string(cast(object, json.loads(literal))))
            except json.JSONDecodeError:
                continue
        elif literal.startswith("'"):
            commands.append(literal[1:-1].replace("\\'", "'"))
        else:
            commands.append(literal[1:-1].replace("\\`", "`"))
    return "\n".join(commands)


def _shell_command(name: str, value: object) -> str:
    if name not in SHELL_NAMES:
        return ""
    if isinstance(value, str):
        return _exec_shell_commands(value) if name in {"exec", "functions.exec"} else value
    data = _object(value)
    command = _string(data.get("command")) or _string(data.get("cmd"))
    if command:
        return command
    # Codex's exec wrapper places shell commands inside its JavaScript input.
    nested = _string(data.get("input"))
    return _exec_shell_commands(nested) if name in {"exec", "functions.exec"} else nested


def _without_heredoc_bodies(command: str) -> str:
    if "<<" not in command:
        return command
    lines = command.splitlines(keepends=True)
    kept: list[str] = []
    delimiter = ""
    for line in lines:
        if delimiter:
            if line.strip() == delimiter:
                delimiter = ""
            else:
                continue
            continue
        kept.append(line)
        match = HEREDOC.search(line)
        if match:
            delimiter = match.group(2)
    return "".join(kept)


def _heredoc_executions(command: str) -> list[tuple[str, str]]:
    if "<<" not in command:
        return []
    lines = command.splitlines(keepends=True)
    executions: list[tuple[str, str]] = []
    for index, line in enumerate(lines):
        marker = HEREDOC.search(line)
        if marker is None:
            continue
        end = index + 1
        while end < len(lines) and lines[end].strip() != marker.group(2):
            end += 1
        if end == len(lines):
            continue
        opener = line[:marker.start()]
        body = "".join(lines[index + 1:end])
        opener_segments = _shell_segments(opener)
        program, _ = _program(opener_segments[-1]) if opener_segments else ("", [])
        if program in {"python", "python3", "python3.13"} or program.endswith((".py", "/python", "/python3")) or program.startswith("$") and "-" in opener_segments[-1]:
            executions.append(("python", body))
        elif program in {"bash", "sh", "zsh", "ssh"}:
            executions.append(("shell", body))
        elif program in {"cat", "tee"}:
            written = SCRIPT_WRITE.search(opener)
            if written is not None:
                path = written.group(1)
                rest = "".join(lines[end + 1:])
                script_name = Path(path).name
                run = re.search(
                    r"(?:^|[;&\n])\s*(?:(?:uv\s+run\s+(?:--with\s+\S+\s+)*)?"
                    + r"(?:[^\s;]*python(?:3(?:\.\d+)?)?|bash|sh|zsh|[^\s;]*py\d*\.sh)\s+(?:-[A-Za-z]+\s+)*)?"
                    + r"(?:[^\s;]*/)?" + re.escape(script_name) + r"(?:\s|$)", rest,
                )
                if run is not None:
                    executions.append(("python" if script_name.endswith(".py") else "shell", body))
                elif script_name.endswith(".py"):
                    stem = re.escape(Path(script_name).stem)
                    for imported in re.finditer(r"\bfrom\s+" + stem + r"\s+import\s+([A-Za-z_][A-Za-z_0-9]*)", rest):
                        function = imported.group(1)
                        if re.search(r"\bdef\s+" + function + r"\s*\(", body) and re.search(r"\b" + function + r"\s*\(", rest[imported.end():]):
                            executions.append(("python", body))
                            break
    return executions


def _skipped_brp_method(method: str) -> bool:
    normalized = method.replace("/", "_").replace(".", "_")
    if normalized.startswith("brp_extras_"):
        normalized = "brp_" + normalized.removeprefix("brp_extras_")
    return any(skip in normalized for skip in BRP_SKIPS)


def _shell_segments(command: str) -> list[list[str]]:
    shell_text = _without_heredoc_bodies(command).replace("\\\n", " ")
    lexer = shlex.shlex(shell_text, posix=True, punctuation_chars=";&|\n{}()")
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    lexer.commenters = ""
    segments: list[list[str]] = []
    segment: list[str] = []
    try:
        for token in lexer:
            if token in SHELL_SEPARATORS or all(character in ";&|{}()" for character in token):
                if segment:
                    segments.append(segment)
                segment = []
            else:
                segment.append(token)
    except ValueError:
        return []
    if segment:
        segments.append(segment)
    return segments


def _backtick_commands(command: str) -> list[str]:
    nested: list[str] = []
    start = -1
    single_quoted = False
    escaped = False
    shell_text = _without_heredoc_bodies(command)
    for index, character in enumerate(shell_text):
        if escaped:
            escaped = False
        elif character == "\\":
            escaped = True
        elif character == "'":
            single_quoted = not single_quoted
        elif character == "`" and not single_quoted:
            if start < 0:
                start = index + 1
            else:
                nested.append(shell_text[start:index])
                start = -1
    return nested


def _program(tokens: list[str]) -> tuple[str, list[str]]:
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in {"do", "then", "else", "elif", "!", "time", "command", "exec", "env"}:
            index += 1
        elif token == "nice":
            index += 1
            if index < len(tokens) and tokens[index] in {"-n", "--adjustment"}:
                index += 2
            elif index < len(tokens) and re.fullmatch(r"-[0-9]+", tokens[index]):
                index += 1
        elif token == "timeout":
            index += 1
            while index < len(tokens) and tokens[index].startswith("-"):
                index += 1
            if index < len(tokens):
                index += 1
        elif re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*=.*", token):
            index += 1
        else:
            break
    if index >= len(tokens):
        return "", []
    name = Path(tokens[index]).name
    if name == "uv" and index + 2 < len(tokens) and tokens[index + 1] == "run":
        index += 2
        name = Path(tokens[index]).name
    return name, tokens[index:]


def _method_result(method: str, command: str) -> tuple[CallKind, str, str] | None:
    if method in {"brp_extras/screenshot", "brp_extras_screenshot"}:
        return ("shot", "bash_brp", command)
    if method and not _skipped_brp_method(method):
        return ("other", "bash_brp", command)
    return None


def _code_result(code: str, command: str) -> tuple[CallKind, str, str] | None:
    methods = [match.group(1) for match in BRP_METHOD.finditer(code)]
    methods.extend(match.group(2) for match in CODE_CALL.finditer(code))
    if "brp_extras/screenshot" in methods or "brp_extras_screenshot" in methods:
        return ("shot", "bash_brp", command)
    return next((found for method in methods if (found := _method_result(method, command)) is not None), None)


def _assigned_request_results(command: str) -> list[tuple[CallKind, str, str]]:
    results: list[tuple[CallKind, str, str]] = []
    for assigned in re.finditer(r"\b([A-Za-z_][A-Za-z_0-9]*)=(['\"])(.*?)\2", command, re.DOTALL):
        name = assigned.group(1)
        sent = re.search(r"\bcurl\b[\s\S]*?(?:-d|--data(?:-raw)?)\s+[\"']?\$" + name + r"\b", command[assigned.end():])
        if sent is None:
            continue
        for method in BRP_METHOD.finditer(assigned.group(3)):
            found = _method_result(method.group(1), command)
            if found is not None:
                results.append(found)
    return results


def _shell_classification(command: str) -> tuple[CallKind, str, str] | None:
    candidates = _assigned_request_results(command) if "curl" in command and "method" in command else []
    for language, body in _heredoc_executions(command):
        found = _code_result(body, command) if language == "python" else _shell_classification(body)
        if found is not None:
            candidates.append((found[0], found[1], command))
    if "`" in command:
        for inner in _backtick_commands(command):
            found = _shell_classification(inner)
            if found is not None:
                candidates.append((found[0], found[1], command))
    for tokens in _shell_segments(command):
        program, arguments = _program(tokens)
        if not program or program in TEXT_PROGRAMS or program == "cd":
            continue
        runner = program in {"python", "python3", "curl", "bash", "sh", "zsh", "ssh"} or program.endswith((".py", ".sh"))
        joined = " ".join(arguments)
        if program in {"bash", "sh", "zsh"} and "-c" in arguments:
            code_index = arguments.index("-c") + 1
            if code_index < len(arguments):
                found = _shell_classification(arguments[code_index])
                if found is not None:
                    candidates.append((found[0], found[1], command))
        if program in {"bash", "sh", "zsh"} and len(arguments) > 2:
            for argument in arguments[2:]:
                if re.match(r"(?:rpc|curl|python\d*|bash|\$[A-Za-z_])\s", argument):
                    found = _shell_classification(argument)
                    if found is not None:
                        candidates.append((found[0], found[1], command))
        if program == "ssh" and len(arguments) > 2:
            found = _shell_classification(arguments[-1])
            if found is not None:
                candidates.append((found[0], found[1], command))
        if runner and any("hana_shot.py" in token and index + 1 < len(arguments) and arguments[index + 1] == "shot"
                          for index, token in enumerate(arguments)):
            candidates.append(("shot", "hana_shot", command))
        if runner:
            if "method" in joined:
                for method in BRP_METHOD.finditer(joined):
                    found = _method_result(method.group(1), command)
                    if found is not None:
                        candidates.append(found)
            if program in {"python", "python3"} or program.endswith(".py") or "-c" in arguments:
                code = arguments[arguments.index("-c") + 1] if "-c" in arguments and arguments.index("-c") + 1 < len(arguments) else ""
                found = _code_result(code, command)
                if found is not None:
                    candidates.append(found)
        for token in arguments[1:]:
            if re.fullmatch(r"(?:brp_extras[/_][A-Za-z_]+|world\.[A-Za-z_]+|bevy/[A-Za-z_]+|hana/[A-Za-z_]+)", token):
                found = _method_result(token, command)
                if found is not None:
                    candidates.append(found)
        if program in {"grim", "spectacle", "screencapture"}:
            candidates.append(("shot", program, command))
        if program == "import" and any(flag in arguments for flag in ("-window", "-display", "-screen")):
            candidates.append(("shot", "import", command))
        if program in {"playwright", "npx", "python", "python3"} and "playwright" in joined.lower():
            if "screenshot" in joined.lower():
                candidates.append(("shot", "browser", command))
            if any(action in joined.lower() for action in ("navigate", "click", "fill", "hover", "press", "scroll", "snapshot")):
                candidates.append(("other", "browser", command))
    return max(candidates, key=lambda found: (found[0] == "shot", found[1] == "hana_shot"), default=None)


def _classify(name: str, value: object) -> tuple[CallKind, str, str] | None:
    command = _shell_command(name, value)
    if command and any(hint in command for hint in SHELL_HINTS):
        classified = _shell_classification(command)
        if classified is not None:
            return classified
    if name == "mcp__brp__brp_extras_screenshot":
        return ("shot", "mcp_brp", command)
    if name.startswith("mcp__brp__"):
        if any(skip in name for skip in BRP_SKIPS):
            return None
        return ("other", "brp", command)
    lower_name = name.lower()
    if "playwright" in lower_name or "browser" in lower_name:
        if "screenshot" in lower_name:
            return ("shot", "browser", command)
        if any(part in lower_name for part in ("navigate", "click", "type", "fill", "hover", "press", "scroll", "snapshot")):
            return ("other", "browser", command)
    return None


def _script_path(path: str, cwd: str) -> str:
    expanded = os.path.expanduser(path.strip("\"'"))
    return os.path.normpath(expanded if os.path.isabs(expanded) else os.path.join(cwd, expanded))


def _remember(path: str, content: str, scripts: dict[str, RememberedScript]) -> None:
    if Path(path).suffix.lower() in NON_SCRIPT_SUFFIXES:
        _ = scripts.pop(path, None)
        return
    old = scripts.get(path)
    if old is not None and old.source:
        previous_calls = tuple(line for line in old.content.splitlines() if any(hint in line for hint in SHELL_HINTS))
        current_calls = tuple(line for line in content.splitlines() if any(hint in line for hint in SHELL_HINTS))
        if previous_calls and previous_calls == current_calls:
            scripts[path] = RememberedScript(content, source=old.source)
            return
    if path.endswith(".py"):
        direct = _code_result(content, content)
    else:
        direct = _shell_classification(content) if any(hint in content for hint in SHELL_HINTS) else None
    names = (Path(name).name for name in scripts if name != path)
    if direct is not None or any(name in content for name in names):
        scripts[path] = RememberedScript(content, source=direct[1] if direct is not None else "")
    else:
        _ = scripts.pop(path, None)


def _heredoc_writes(command: str, cwd: str) -> list[tuple[str, str, bool]]:
    lines = command.splitlines(keepends=True)
    writes: list[tuple[str, str, bool]] = []
    current = cwd
    index = 0
    while index < len(lines):
        line = lines[index]
        marker = HEREDOC.search(line)
        prefix = line[:marker.start()] if marker else line
        for tokens in _shell_segments(prefix):
            program, arguments = _program(tokens)
            if program == "cd" and len(arguments) > 1:
                current = _script_path(arguments[1], current)
        if marker is None:
            index += 1
            continue
        target = SCRIPT_HEREDOC_WRITE.search(line[:marker.start()])
        if target is None:
            index += 1
            continue
        end = index + 1
        while end < len(lines) and lines[end].strip() != marker.group(2):
            end += 1
        if end == len(lines):
            index += 1
            continue
        path = _script_path(target.group(3), current)
        writes.append((path, "".join(lines[index + 1:end]), target.group(1) == ">>" or bool(target.group(2))))
        index = end + 1
    return writes


def _patch_changes(patch: str, cwd: str, scripts: dict[str, RememberedScript]) -> None:
    lines = patch.splitlines(keepends=True)
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        if not line.startswith(("*** Add File: ", "*** Update File: ", "*** Delete File: ")):
            index += 1
            continue
        action, raw_path = line[4:].split(": ", 1)
        path = _script_path(raw_path, cwd)
        index += 1
        body: list[str] = []
        while index < len(lines) and not lines[index].startswith("*** "):
            body.append(lines[index])
            index += 1
        if action == "Delete File":
            _ = scripts.pop(path, None)
        elif action == "Add File":
            _remember(path, "".join(part[1:] for part in body if part.startswith("+")), scripts)
        elif action == "Update File":
            old = scripts.get(path)
            content = old.content if old is not None else ""
            before: list[str] = []
            after: list[str] = []
            for part in [*body, "@@\n"]:
                if part.startswith("@@"):
                    if before:
                        previous = "".join(before)
                        replacement = "".join(after)
                        content = content.replace(previous, replacement, 1) if previous in content else content + replacement
                    before, after = [], []
                elif part.startswith("-"):
                    before.append(part[1:])
                elif part.startswith("+"):
                    after.append(part[1:])
                elif part.startswith(" "):
                    before.append(part[1:])
                    after.append(part[1:])
            _remember(path, content, scripts)


def _remember_tool(name: str, value: object, cwd: str, scripts: dict[str, RememberedScript]) -> None:
    data = _object(value)
    if name == "Write":
        path = _string(data.get("file_path"))
        if path:
            _remember(_script_path(path, cwd), _string(data.get("content")), scripts)
    elif name == "Edit":
        path = _script_path(_string(data.get("file_path")), cwd)
        previous = scripts.get(path)
        if previous is not None:
            old = _string(data.get("old_string"))
            new = _string(data.get("new_string"))
            content = previous.content.replace(old, new) if data.get("replace_all") else previous.content.replace(old, new, 1)
            _remember(path, content, scripts)
    elif name.endswith("apply_patch"):
        patch = _string(data.get("input")) if data else _string(value)
        if patch:
            _patch_changes(patch, cwd, scripts)
    else:
        command = _shell_command(name, value)
        if not command or "<<" not in command and "rm" not in command:
            return
        if "<<" in command:
            for path, body, append in _heredoc_writes(command, cwd):
                prior = scripts.get(path)
                _remember(path, (prior.content if prior is not None else "") + body if append else body, scripts)
        if "rm" not in command:
            return
        current = cwd
        for tokens in _shell_segments(command):
            program, arguments = _program(tokens)
            if program == "cd" and len(arguments) > 1:
                current = _script_path(arguments[1], current)
            elif program == "rm":
                for target in arguments[1:]:
                    if not target.startswith("-"):
                        _ = scripts.pop(_script_path(target, current), None)


def _running_invocations(command: str, cwd: str, scripts: dict[str, RememberedScript],
                         fallback_cwd: str = "") -> list[ScriptInvocation]:
    found: list[ScriptInvocation] = []
    current = cwd
    fallback = fallback_cwd
    removed: set[str] = set()
    for tokens in _shell_segments(command):
        program, arguments = _program(tokens)
        if program == "cd" and len(arguments) > 1:
            current = _script_path(arguments[1], current)
            if fallback:
                fallback = _script_path(arguments[1], fallback)
            continue
        if program == "rm":
            removed.update(_script_path(target, current) for target in arguments[1:] if not target.startswith("-"))
            continue
        if program in {"bash", "sh", "zsh"} and "-c" in arguments:
            index = arguments.index("-c") + 1
            if index < len(arguments):
                found.extend(_running_invocations(arguments[index], current, scripts, fallback))
        candidate = ""
        run_arguments: list[str] = []
        if program in {"python", "python3", "python3.13", "bash", "sh", "zsh", "source", "."}:
            for index, argument in enumerate(arguments[1:], 1):
                if argument.startswith("-"):
                    continue
                candidate = argument
                run_arguments = arguments[index + 1:]
                break
        elif arguments and (arguments[0].startswith(("./", "/", "../")) or _script_path(arguments[0], current) in scripts):
            candidate = arguments[0]
            run_arguments = arguments[1:]
        if candidate:
            path = _script_path(candidate, current)
            if path not in scripts and fallback:
                path = _script_path(candidate, fallback)
            if path in scripts and path not in removed:
                found.append(ScriptInvocation(path, tuple(run_arguments), current,
                                              "source" if program in {"source", "."} else "execute"))
    return found


def _python_invocations(content: str, cwd: str, script_dir: str,
                        scripts: dict[str, RememberedScript]) -> list[ScriptInvocation]:
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return []
    found: list[ScriptInvocation] = []
    vector_exec = {"os.execv", "os.execve", "os.execvp", "os.execvpe"}
    list_exec = {"os.execl", "os.execle", "os.execlp", "os.execlpe"}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        name = ast.unparse(node.func)
        if name not in {"subprocess.run", "subprocess.call", "subprocess.check_call", "subprocess.check_output",
                        "subprocess.Popen", "Popen", "os.system", *vector_exec, *list_exec}:
            continue
        try:
            if name in vector_exec and len(node.args) > 1:
                raw = cast(object, ast.literal_eval(node.args[1]))
            elif name in list_exec and len(node.args) > 1:
                raw = cast(object, [ast.literal_eval(arg) for arg in node.args[1:]
                                    if not isinstance(arg, ast.Dict)])
            else:
                raw = cast(object, ast.literal_eval(node.args[0]))
        except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
            continue
        commands: list[str] = []
        if isinstance(raw, str):
            commands.append(raw)
        elif isinstance(raw, (list, tuple)):
            parts = list(cast(list[object] | tuple[object, ...], raw))
            if all(isinstance(item, str) for item in parts):
                commands.append(shlex.join([cast(str, item) for item in parts]))
        for command in commands:
            found.extend(_running_invocations(command, cwd, scripts, script_dir))
    return found


def _shell_functions(content: str) -> list[ShellFunction]:
    lines = content.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    functions: list[ShellFunction] = []
    index = 0
    while index < len(lines):
        opening = re.match(r"\s*(?:function\s+)?([A-Za-z_]\w*)\s*\(\)\s*\{", lines[index])
        if opening is None:
            index += 1
            continue
        end = index + 1
        if not lines[index].rstrip().endswith("}"):
            while end < len(lines) and lines[end].strip() != "}":
                end += 1
            end = min(end + 1, len(lines))
        functions.append(ShellFunction(opening.group(1), "".join(lines[index:end]), offsets[index], offsets[end]))
        index = end
    return functions


def _selected_content(content: str, path: str, arguments: tuple[str, ...], source_context: str = "") -> str:
    selected = arguments[0] if arguments else ""
    if path.endswith(".py"):
        try:
            tree = ast.parse(content)
        except SyntaxError:
            return content
        option_values: dict[str, str] = {}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute) or node.func.attr != "add_argument":
                continue
            flags = [arg.value for arg in node.args if isinstance(arg, ast.Constant) and isinstance(arg.value, str)]
            if not flags or not flags[0].startswith("-"):
                continue
            destination = next((keyword.value.value for keyword in node.keywords
                                if keyword.arg == "dest" and isinstance(keyword.value, ast.Constant)
                                and isinstance(keyword.value.value, str)),
                               max(flags, key=len).lstrip("-").replace("-", "_"))
            for index, argument in enumerate(arguments):
                for flag in flags:
                    if argument == flag and index + 1 < len(arguments):
                        option_values[destination] = arguments[index + 1]
                    elif argument.startswith(flag + "="):
                        option_values[destination] = argument[len(flag) + 1:]
        command_aliases: set[str] = set()
        argument_lists: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            if ast.unparse(node.value) == "sys.argv[1:]":
                argument_lists.update(target.id for target in node.targets if isinstance(target, ast.Name))
            if ast.unparse(node.value) == "sys.argv[1]":
                command_aliases.update(target.id for target in node.targets if isinstance(target, ast.Name))
            if isinstance(node.value, ast.Tuple):
                for target in node.targets:
                    if isinstance(target, ast.Tuple):
                        command_aliases.update(name.id for name, value in zip(target.elts, node.value.elts)
                                               if isinstance(name, ast.Name) and ast.unparse(value) == "sys.argv[1]")
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and any(ast.unparse(node.value) == name + "[0]" for name in argument_lists):
                command_aliases.update(target.id for target in node.targets if isinstance(target, ast.Name))
        changed = False

        class ChooseBranch(ast.NodeTransformer):
            @override
            def visit_Expr(self, node: ast.Expr) -> ast.AST | list[ast.stmt]:
                nonlocal changed
                value = node.value
                if (isinstance(value, ast.Call) and isinstance(value.func, ast.Name)
                        and value.func.id in {"exec", "eval"} and value.args
                        and ast.unparse(value.args[0]) == "sys.argv[1]" and arguments):
                    try:
                        parsed = ast.parse(arguments[0])
                    except SyntaxError:
                        return node
                    changed = True
                    return parsed.body
                return self.generic_visit(node)

            @override
            def visit_If(self, node: ast.If) -> ast.AST | list[ast.stmt]:
                nonlocal changed
                test = node.test
                if not isinstance(test, ast.Compare) or len(test.comparators) != 1 or len(test.ops) != 1:
                    return self.generic_visit(node)
                operand = ast.unparse(test.left)
                if ("sys.argv[" not in operand and operand not in command_aliases
                        and not re.fullmatch(r"(?:args|parsed|namespace)\.[A-Za-z_]\w*", operand)):
                    return self.generic_visit(node)
                try:
                    wanted = cast(object, ast.literal_eval(test.comparators[0]))
                except (ValueError, TypeError):
                    return self.generic_visit(node)
                choice = selected
                position = re.fullmatch(r"sys\.argv\[(\d+)\]", operand)
                if position is not None:
                    offset = int(position.group(1)) - 1
                    choice = arguments[offset] if 0 <= offset < len(arguments) else ""
                elif operand.startswith(("args.", "parsed.", "namespace.")):
                    choice = option_values.get(operand.split(".", 1)[1], selected)
                matches = choice == wanted
                if isinstance(test.ops[0], ast.NotEq):
                    matches = not matches
                elif not isinstance(test.ops[0], ast.Eq):
                    return self.generic_visit(node)
                changed = True
                body = node.body if matches else node.orelse
                chosen: list[ast.stmt] = []
                for statement in body:
                    visited = cast(object, self.visit(statement))
                    if isinstance(visited, list):
                        chosen.extend(cast(list[ast.stmt], visited))
                    elif isinstance(visited, ast.stmt):
                        chosen.append(visited)
                return chosen

        transformed = cast(ast.Module | None, ChooseBranch().visit(tree))
        if transformed is None:
            return content
        functions = {node.name: node for node in transformed.body if isinstance(node, ast.FunctionDef)}
        runtime = [node for node in transformed.body if not isinstance(node, ast.FunctionDef)]
        decorated = [function for function in functions.values() if function.decorator_list]
        reachable: set[str] = set()
        pending = [node for statement in [*runtime, *decorated] for node in ast.walk(statement)]
        while pending:
            node = pending.pop()
            if not isinstance(node, ast.Name) or not isinstance(node.ctx, ast.Load):
                continue
            name = node.id
            if name in functions and name not in reachable:
                reachable.add(name)
                pending.extend(ast.walk(functions[name]))
        reachable.update(function.name for function in decorated)
        selected_tree = ast.Module(body=[*runtime, *(functions[name] for name in functions if name in reachable)],
                                   type_ignores=[])
        return ast.unparse(selected_tree) if changed or len(selected_tree.body) != len(tree.body) else content
    case = re.search(r"\bcase\s+['\"]?\$(\d+)['\"]?\s+in\s*(.*?)\besac\b", content, re.DOTALL)
    branch: re.Match[str] | None = None
    if case is not None:
        options = cast(list[tuple[str, str]], re.findall(r"(?:^|;;)\s*([^;\n)]+)\)\s*(.*?)(?=;;|$)", case.group(2), re.DOTALL))
        offset = int(case.group(1)) - 1
        choice = arguments[offset] if 0 <= offset < len(arguments) else ""
        body = next((body for label, body in options if choice in label.split("|")), "")
        content = content[:case.start()] + body + content[case.end():]
    else:
        branch = re.search(r"\bif\s+\[\[?\s*['\"]?\$(\d+)['\"]?\s*(?:=|==)\s*['\"]?([^\s'\"]+)['\"]?\s*\]\]?\s*;?\s*then\s*(.*?)\s*(?:else\s*(.*?))?\s*fi\b", content, re.DOTALL)
        if branch is not None:
            offset = int(branch.group(1)) - 1
            choice = arguments[offset] if 0 <= offset < len(arguments) else ""
            body = branch.group(3) if choice == branch.group(2) else branch.group(4) or ""
            content = content[:branch.start()] + body + content[branch.end():]
    if case is None and branch is None and not source_context:
        return content
    functions = _shell_functions(content)
    if not functions:
        return content
    outside = content
    for function in reversed(functions):
        outside = outside[:function.start] + "\n" + outside[function.end:]
    outside = "\n".join(line for line in outside.splitlines() if not line.lstrip().startswith("#"))
    active = outside + "\n" + source_context
    included: set[str] = set()
    while True:
        commands = {_program(tokens)[0] for tokens in _shell_segments(active)}
        newly_called = [function for function in functions if function.name not in included
                        and function.name in commands]
        if not newly_called:
            break
        for function in newly_called:
            included.add(function.name)
            active += "\n" + function.content
    return outside + "\n" + "\n".join(function.content for function in functions if function.name in included)


def _screenshot_calls(content: str, python: bool = False) -> int:
    if python:
        try:
            tree = ast.parse(content)
        except SyntaxError:
            return 1
        count = 0
        functions = {node.name: node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}

        def visit(node: ast.AST, multiplier: int, active: frozenset[str]) -> None:
            nonlocal count
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for decorator in node.decorator_list:
                    visit(decorator, multiplier, active)
                if node.decorator_list:
                    for child in node.body:
                        visit(child, multiplier, active | {node.name})
                return
            if isinstance(node, ast.For):
                try:
                    values = cast(object, ast.literal_eval(node.iter))
                except (ValueError, TypeError):
                    values = None
                repeats = len(cast(list[object] | tuple[object, ...], values)) if isinstance(values, (list, tuple)) else 1
                if isinstance(node.iter, ast.Call) and isinstance(node.iter.func, ast.Name) and node.iter.func.id == "range":
                    try:
                        bounds = [cast(int, ast.literal_eval(arg)) for arg in node.iter.args]
                        if 1 <= len(bounds) <= 3 and all(type(bound) is int for bound in bounds):
                            repeats = len(range(*bounds))
                    except (ValueError, TypeError):
                        pass
                for child in node.body:
                    visit(child, multiplier * repeats, active)
                for child in node.orelse:
                    visit(child, multiplier, active)
                return
            if isinstance(node, ast.Call):
                code = ast.get_source_segment(content, node) or ""
                found = _code_result(code, code)
                if found is not None and found[0] == "shot":
                    count += multiplier
                    return
            if (isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
                    and node.id in functions and node.id not in active):
                for child in functions[node.id].body:
                    visit(child, multiplier, active | {node.id})
                return
            for child in ast.iter_child_nodes(node):
                visit(child, multiplier, active)

        visit(tree, 1, frozenset())
        return count
    count = sum(_screenshot_calls(body, language == "python")
                for language, body in _heredoc_executions(content))
    loops: list[int] = []
    pending = 1
    shell_content = "\n".join(line.split(" #", 1)[0] for line in content.splitlines()
                              if not line.lstrip().startswith("#"))
    for tokens in _shell_segments(shell_content):
        if tokens[0] == "for" and "in" in tokens:
            values = tokens[tokens.index("in") + 1:]
            pending = len(values) if values and all(not value.startswith("$") for value in values) else 1
            continue
        if tokens[0] == "do":
            loops.append(pending)
            pending = 1
            tokens = tokens[1:]
        if tokens and tokens[0] == "done":
            if loops:
                _ = loops.pop()
            tokens = tokens[1:]
        if tokens and (found := _shell_classification(shlex.join(tokens))) is not None and found[0] == "shot":
            count += math.prod(loops)
    return max(1, count)


def _script_run(path: str, scripts: dict[str, RememberedScript], seen: set[str],
                arguments: tuple[str, ...] = (), cwd: str = "", source_context: str = "") -> ScriptRun | None:
    if path in seen:
        return None
    script = scripts.get(path)
    if script is None:
        return None
    seen.add(path)
    if script.classification_state == "unread":
        direct = None
        if any(hint in script.content for hint in SHELL_HINTS):
            direct = _code_result(script.content, script.content) if path.endswith(".py") else _shell_classification(script.content)
        if direct is None:
            script.classification_state = "not_related"
        else:
            script.classification_state = direct[0]
            script.source = direct[1]
            script.screenshot_calls = _screenshot_calls(script.content, path.endswith(".py")) if direct[0] == "shot" else 0
    key = arguments
    if source_context:
        selected = _selected_content(script.content, path, arguments, source_context)
    else:
        if key not in script.selected_content:
            script.selected_content[key] = _selected_content(script.content, path, arguments)
        selected = script.selected_content[key]
    if selected == script.content:
        direct = ((cast(CallKind, script.classification_state), script.source, selected)
                  if script.classification_state in {"shot", "other"} else None)
    else:
        direct = _code_result(selected, selected) if path.endswith(".py") else _shell_classification(selected)
    nested_invocations = (_python_invocations(selected, cwd or str(Path(path).parent), str(Path(path).parent), scripts)
                          if path.endswith(".py") else
                          _running_invocations(selected, cwd or str(Path(path).parent), scripts, str(Path(path).parent)))
    nested = [run for child in nested_invocations
              if (run := _script_run(child.path, scripts, seen.copy(), child.arguments, child.cwd,
                                     selected if child.mode == "source" else "")) is not None]
    candidates: list[tuple[CallKind, str]] = []
    if direct is not None:
        candidates.append((direct[0], direct[1]))
    candidates.extend((run.kind, run.source) for run in nested)
    if not candidates:
        return None
    kind, source = max(candidates, key=lambda item: (item[0] == "shot", item[1] == "hana_shot"))
    count = (script.screenshot_calls if selected == script.content else _screenshot_calls(selected, path.endswith(".py"))) if direct is not None and direct[0] == "shot" else 0
    count += sum(run.screenshot_calls for run in nested)
    return ScriptRun(kind, source, path, count, (path, *(child.path for child in nested)))


def _linked_run(command: str, cwd: str, scripts: dict[str, RememberedScript]) -> ScriptRun | None:
    matches = [
        run for invocation in _running_invocations(command, cwd, scripts)
        if (run := _script_run(invocation.path, scripts, set(), invocation.arguments, invocation.cwd,
                              command if invocation.mode == "source" else "")) is not None
    ]
    if not matches:
        return None
    preferred = max(matches, key=lambda run: (run.kind == "shot", run.source == "hana_shot"))
    return ScriptRun(preferred.kind, preferred.source, preferred.path,
                     sum(run.screenshot_calls for run in matches),
                     tuple(path for run in matches for path in run.paths))


def _result_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return " ".join(_result_text(item) for item in cast(list[object], value))
    if not isinstance(value, dict):
        return ""
    data = _object(cast(object, value))
    return " ".join(_result_text(data.get(key)) for key in ("text", "content", "output"))


def _image_count(kind: CallKind, source: str, result: object, command: str, scripted_calls: int = 0) -> int:
    if kind != "shot":
        return 0
    if command or source == "hana_shot":
        paths = set(PNG_PATH.findall(_result_text(result)))
        if paths:
            return len(paths)
        return max(1, scripted_calls or _screenshot_calls(command))
    return 1


def _read_json(line: str) -> dict[str, object]:
    try:
        return _object(cast(object, json.loads(line)))
    except json.JSONDecodeError:
        return {}


def _matching_lines(path: Path, markers: tuple[bytes, ...]) -> Iterator[bytes]:
    args = ["rg", "--no-heading", "--no-line-number", "--text", "-F"]
    for marker in markers:
        args.extend(("-e", marker.decode()))
    with subprocess.Popen([*args, str(path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
        if process.stdout is None:
            raise RuntimeError(f"No output stream for {path}")
        for line in cast(Iterable[bytes], process.stdout):
            yield line.rstrip(b"\n")
        if process.wait() not in (0, 1):
            error = cast(bytes, process.stderr.read()).decode(errors="replace") if process.stderr is not None else ""
            raise RuntimeError(error)


def _claude_calls(path: Path, projects: dict[str, tuple[str, ProjectAttribution]]) -> list[ToolCall]:
    pending: dict[str, tuple[datetime, str, str, ProjectAttribution, CallKind, str, str, str, int, tuple[str, ...], str]] = {}
    pending_writes: dict[str, tuple[str, object, str]] = {}
    known_cwds: set[str] = set()
    scripts: dict[str, RememberedScript] = {}
    script_names: tuple[bytes, ...] = ()
    script_names_text: tuple[str, ...] = ()
    script_name_pattern: re.Pattern[bytes] | None = None
    calls: list[ToolCall] = []
    for line in _matching_lines(path, (b'"tool_use"', b'"tool_result"')):
            if b'"tool_use"' in line:
                if not (CLAUDE_HINT.search(line)
                        or b'"Write"' in line or b'"Edit"' in line
                        or script_name_pattern is not None and script_name_pattern.search(line)):
                    continue
            elif (pending or pending_writes) and b'"tool_result"' in line:
                if not any(use_id.encode() in line for use_id in (*pending, *pending_writes)):
                    continue
            else:
                continue
            row = _read_json(line.decode("utf-8", errors="replace"))
            stamp = _stamp(row.get("timestamp"))
            if stamp is None:
                continue
            message = _object(row.get("message"))
            for raw_part in _items(message.get("content")):
                part = _object(raw_part)
                if part.get("type") == "tool_use":
                    name = _string(part.get("name"))
                    value = part.get("input")
                    command = _shell_command(name, value)
                    cwd = _string(row.get("cwd"))
                    if cwd and "scratchpad" not in cwd:
                        known_cwds.add(cwd)
                    linked = _linked_run(command, cwd, scripts) if command and any(name in command for name in script_names_text) else None
                    classified = _classify(name, value)
                    if linked is not None and (classified is None or linked.kind == "shot" and classified[0] != "shot"):
                        classified = (linked.kind, linked.source, command)
                    use_id = _string(part.get("id"))
                    if name in {"Write", "Edit"}:
                        pending_writes[use_id] = (name, value, cwd)
                    else:
                        _remember_tool(name, value, cwd, scripts)
                    if "<<" in command or name not in {"Write", "Edit"} and "rm" in command:
                        script_names_text = tuple({os.path.basename(script_path) for script_path in scripts})
                        script_names = tuple(name.encode() for name in script_names_text)
                        script_name_pattern = re.compile(b"|".join(re.escape(name) for name in script_names)) if script_names else None
                    if classified is None:
                        continue
                    kind, origin, classified_command = classified
                    project, attribution = _project(cwd, projects, known_cwds)
                    pending[use_id] = (
                        stamp, _string(row.get("sessionId")) or path.stem,
                        project, attribution, kind, origin, classified_command,
                        linked.path if linked is not None else "",
                        linked.screenshot_calls if linked is not None else 0,
                        linked.paths if linked is not None else (), cwd,
                    )
                elif part.get("type") == "tool_result":
                    use_id = _string(part.get("tool_use_id"))
                    write = pending_writes.pop(use_id, None)
                    if write is not None and part.get("is_error") is not True:
                        _remember_tool(*write, scripts)
                        script_names_text = tuple({os.path.basename(script_path) for script_path in scripts})
                        script_names = tuple(name.encode() for name in script_names_text)
                        script_name_pattern = re.compile(b"|".join(re.escape(name) for name in script_names)) if script_names else None
                    use = pending.pop(use_id, None)
                    if use is None:
                        continue
                    start, session, project, attribution, kind, origin, command, script_path, script_calls, script_paths, cwd = use
                    calls.append(ToolCall(
                        start, stamp, session, str(path), "Claude", project, kind, origin,
                        command, _image_count(kind, origin, part.get("content"), command, script_calls),
                        attribution, script_path, script_paths, cwd,
                    ))
    return calls


def _codex_calls(path: Path, projects: dict[str, tuple[str, ProjectAttribution]]) -> list[ToolCall]:
    pending: dict[str, tuple[datetime, CallKind, str, str, str, int, tuple[str, ...], str]] = {}
    scripts: dict[str, RememberedScript] = {}
    script_names: tuple[bytes, ...] = ()
    script_names_text: tuple[str, ...] = ()
    script_name_pattern: re.Pattern[bytes] | None = None
    calls: list[ToolCall] = []
    session = path.stem.rsplit("-", 5)[-1]
    project = path.parent.name
    attribution: ProjectAttribution = "last_path"
    cwd = ""
    for line in _matching_lines(path, (b'"session_meta"', b'"response_item"')):
            if b'"session_meta"' not in line and b'"response_item"' not in line:
                continue
            if b'"session_meta"' not in line:
                if b'"function_call_output"' in line or b'"custom_tool_call_output"' in line:
                    if not pending or not any(use_id.encode() in line for use_id in pending):
                        continue
                elif not (CODEX_HINT.search(line)
                          or script_name_pattern is not None and script_name_pattern.search(line)):
                    continue
            row = _read_json(line.decode("utf-8", errors="replace"))
            stamp = _stamp(row.get("timestamp"))
            payload = _object(row.get("payload"))
            if row.get("type") == "session_meta":
                session = _string(payload.get("id")) or session
                cwd = _string(payload.get("cwd"))
                project, attribution = _project(cwd, projects)
                continue
            if stamp is None or row.get("type") != "response_item":
                continue
            part_type = _string(payload.get("type"))
            if part_type in ("function_call", "custom_tool_call"):
                name = _string(payload.get("name"))
                raw_input = payload.get("arguments", payload.get("input"))
                if isinstance(raw_input, str) and raw_input.startswith("{"):
                    parsed = _read_json(raw_input)
                    raw_input = parsed if parsed else raw_input
                call_cwd = _string(_object(raw_input).get("workdir")) or cwd
                command = _shell_command(name, raw_input)
                linked = _linked_run(command, call_cwd, scripts) if command and any(name in command for name in script_names_text) else None
                classified = _classify(name, raw_input)
                if linked is not None and (classified is None or linked.kind == "shot" and classified[0] != "shot"):
                    classified = (linked.kind, linked.source, command)
                _remember_tool(name, raw_input, call_cwd, scripts)
                if name.endswith("apply_patch") or "<<" in command:
                    script_names_text = tuple({os.path.basename(script_path) for script_path in scripts})
                    script_names = tuple(name.encode() for name in script_names_text)
                    script_name_pattern = re.compile(b"|".join(re.escape(name) for name in script_names)) if script_names else None
                if classified is None:
                    continue
                kind, origin, classified_command = classified
                pending[_string(payload.get("call_id"))] = (
                    stamp, kind, origin, classified_command,
                    linked.path if linked is not None else "",
                    linked.screenshot_calls if linked is not None else 0,
                    linked.paths if linked is not None else (), call_cwd,
                )
            elif part_type in ("function_call_output", "custom_tool_call_output"):
                use = pending.pop(_string(payload.get("call_id")), None)
                if use is None:
                    continue
                start, kind, origin, command, script_path, script_calls, script_paths, call_cwd = use
                calls.append(ToolCall(
                    start, stamp, session, str(path), "Codex", project, kind, origin,
                    command, _image_count(kind, origin, payload.get("output"), command, script_calls),
                    attribution, script_path, script_paths, call_cwd,
                ))
    return calls


def _scan_file(file: tuple[Agent, Path]) -> list[ToolCall]:
    agent, path = file
    projects: dict[str, tuple[str, ProjectAttribution]] = {}
    return _claude_calls(path, projects) if agent == "Claude" else _codex_calls(path, projects)


def scan_calls(claude_root: Path, codex_root: Path) -> TranscriptScan:
    """Read matching transcript files; roots can point at fixture directories."""
    claude_files = _candidate_files(claude_root)
    codex_files = _codex_candidate_files(codex_root)
    files: list[tuple[Agent, Path]] = [*(('Claude', path) for path in claude_files), *(('Codex', path) for path in codex_files)]
    sized: list[tuple[int, Agent, Path]] = sorted(((path.stat().st_size, agent, path) for agent, path in files), reverse=True)
    ordered: list[tuple[Agent, Path]] = [(agent, path) for _, agent, path in sized]
    calls: list[ToolCall] = []
    if sum(size for size, _, _ in sized) > PARALLEL_SCAN_BYTES:
        with ProcessPoolExecutor(max_workers=min(4, os.cpu_count() or 2)) as workers:
            for file_calls in workers.map(_scan_file, ordered):
                calls.extend(file_calls)
    else:
        for file in ordered:
            calls.extend(_scan_file(file))
    live_names = {call.project for call in calls if call.project_attribution == "repository"}
    folded: list[ToolCall] = []
    for call in calls:
        if call.project_attribution in {"last_path", "scratchpad_target_missing"} and (
            call.project_attribution == "scratchpad_target_missing" or not Path(call.cwd).exists()
        ):
            matches = [name for name in live_names
                       if call.project == name or call.project.startswith(name + "-")
                       or call.project == re.sub(r"[^A-Za-z0-9]", "-", name)
                       or call.project.startswith(re.sub(r"[^A-Za-z0-9]", "-", name) + "-")]
            if matches:
                call = replace(call, project=max(matches, key=len), project_attribution="removed_worktree")
        folded.append(call)
    return TranscriptScan(folded, len(files), sum(size for size, _, _ in sized))


def survey_counts(calls: Iterable[ToolCall]) -> dict[str, tuple[int, int, int]]:
    groups: dict[str, tuple[int, set[str], set[str]]] = {}
    for call in calls:
        if call.kind != "shot":
            continue
        count, sessions, projects = groups.setdefault(call.source, (0, set(), set()))
        sessions.add(call.session_id)
        projects.add(call.project)
        groups[call.source] = (count + 1, sessions, projects)
    return {source: (count, len(sessions), len(projects)) for source, (count, sessions, projects) in groups.items()}
