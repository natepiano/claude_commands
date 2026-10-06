"""Read screenshot and related tool calls from Claude and Codex transcripts."""

from __future__ import annotations

import json
import re
import shlex
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Literal, cast

Agent = Literal["Claude", "Codex"]
CallKind = Literal["shot", "other"]


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
    project_is_fallback: bool = False


PREFILTER = (
    "mcp__brp__", "brp_extras/screenshot", "brp_extras_screenshot", "hana_shot.py shot",
    "grim ", "spectacle ", "screencapture ", "import -window",
    "import -display", "import -screen", "playwright", "browser_take_screenshot",
    "browser_screenshot", "take_screenshot",
)
BRP_SKIPS = (
    "brp_launch", "brp_shutdown", "brp_status", "brp_list", "read_log",
    "delete_logs", "list_logs", "type_guide", "registry_schema", "rpc_discover",
)
SHELL_NAMES = {"Bash", "exec_command", "exec", "functions.exec", "shell_command"}
SHELL_SEPARATORS = {";", "&", "&&", "|", "||", "\n"}
TEXT_PROGRAMS = {"rg", "grep", "cat", "sed", "head", "tail", "find", "echo", "printf", "tee"}
SHELL_HINTS = (
    "brp_extras/", "brp_extras_", "world.", "bevy/", "hana/", "hana_shot.py",
    "playwright", "grim ", "spectacle ", "screencapture ", "import -",
)
CLAUDE_LINE_HINTS = (*SHELL_HINTS, "mcp__brp__", "browser", "take_screenshot")
JS_COMMAND = re.compile(r"\bcmd\s*:\s*(\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`)")
TOOL_RESULT_ID = re.compile(r'"tool_use_id"\s*:\s*"([^\"]+)"')
HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z_0-9]*)\1")
PNG_PATH = re.compile(r"[A-Za-z0-9_./~:-]+\.png\b", re.IGNORECASE)
BRP_METHOD = re.compile(r"[\"']?method[\"']?\s*:\s*[\"']([^\"']+)[\"']")
CODE_CALL = re.compile(r"\b(?:rpc|call|invoke|send_request)\s*\(\s*(['\"])([^'\"]+)\1")
SCRIPT_WRITE = re.compile(r"\b(?:cat\s*>{1,2}\s*|tee\s+)([^\s;]+)")


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


def _project(cwd: str, cache: dict[str, tuple[str, bool]]) -> tuple[str, bool]:
    if cwd in cache:
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
            cache[cwd] = (repo.name, False)
            return cache[cwd]
    cache[cwd] = (original.name or "unknown", not original.exists())
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


def _result_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return " ".join(_result_text(item) for item in cast(list[object], value))
    data = _object(value)
    return " ".join(_result_text(data.get(key)) for key in ("text", "content", "output"))


def _image_count(kind: CallKind, source: str, result: object) -> int:
    if kind != "shot":
        return 0
    if source == "hana_shot":
        return len(set(PNG_PATH.findall(_result_text(result))))
    return 1


def _read_json(line: str) -> dict[str, object]:
    try:
        return _object(cast(object, json.loads(line)))
    except json.JSONDecodeError:
        return {}


def _claude_calls(path: Path, projects: dict[str, tuple[str, bool]]) -> list[ToolCall]:
    pending: dict[str, tuple[datetime, str, str, bool, CallKind, str, str]] = {}
    calls: list[ToolCall] = []
    with path.open(encoding="utf-8", errors="replace") as source:
        for line in source:
            if '"tool_use"' in line:
                if not any(hint in line for hint in CLAUDE_LINE_HINTS):
                    continue
            elif pending and '"tool_result"' in line:
                result_id = TOOL_RESULT_ID.search(line)
                if result_id is not None and result_id.group(1) not in pending:
                    continue
            else:
                continue
            row = _read_json(line)
            stamp = _stamp(row.get("timestamp"))
            if stamp is None:
                continue
            message = _object(row.get("message"))
            for raw_part in _items(message.get("content")):
                part = _object(raw_part)
                if part.get("type") == "tool_use":
                    name = _string(part.get("name"))
                    classified = _classify(name, part.get("input"))
                    if classified is None:
                        continue
                    kind, origin, command = classified
                    cwd = _string(row.get("cwd"))
                    project, fallback = _project(cwd, projects)
                    pending[_string(part.get("id"))] = (
                        stamp, _string(row.get("sessionId")) or path.stem,
                        project, fallback, kind, origin, command,
                    )
                elif part.get("type") == "tool_result":
                    use = pending.pop(_string(part.get("tool_use_id")), None)
                    if use is None:
                        continue
                    start, session, project, fallback, kind, origin, command = use
                    calls.append(ToolCall(
                        start, stamp, session, str(path), "Claude", project, kind, origin,
                        command, _image_count(kind, origin, part.get("content")), fallback,
                    ))
    return calls


def _codex_calls(path: Path, projects: dict[str, tuple[str, bool]]) -> list[ToolCall]:
    pending: dict[str, tuple[datetime, CallKind, str, str]] = {}
    calls: list[ToolCall] = []
    session = path.stem.rsplit("-", 5)[-1]
    project = path.parent.name
    fallback = False
    with path.open(encoding="utf-8", errors="replace") as source:
        for line in source:
            if '"session_meta"' not in line and '"response_item"' not in line:
                continue
            row = _read_json(line)
            stamp = _stamp(row.get("timestamp"))
            payload = _object(row.get("payload"))
            if row.get("type") == "session_meta":
                session = _string(payload.get("id")) or session
                project, fallback = _project(_string(payload.get("cwd")), projects)
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
                classified = _classify(name, raw_input)
                if classified is None:
                    continue
                kind, origin, command = classified
                pending[_string(payload.get("call_id"))] = (stamp, kind, origin, command)
            elif part_type in ("function_call_output", "custom_tool_call_output"):
                use = pending.pop(_string(payload.get("call_id")), None)
                if use is None:
                    continue
                start, kind, origin, command = use
                calls.append(ToolCall(
                    start, stamp, session, str(path), "Codex", project, kind, origin,
                    command, _image_count(kind, origin, payload.get("output")), fallback,
                ))
    return calls


def scan_calls(claude_root: Path, codex_root: Path) -> list[ToolCall]:
    """Read matching transcript files; roots can point at fixture directories."""
    projects: dict[str, tuple[str, bool]] = {}
    calls: list[ToolCall] = []
    for path in _candidate_files(claude_root):
        calls.extend(_claude_calls(path, projects))
    for path in _candidate_files(codex_root):
        calls.extend(_codex_calls(path, projects))
    live_names = {name for name, fallback in projects.values() if not fallback}
    folded: list[ToolCall] = []
    for call in calls:
        if call.project_is_fallback:
            matches = [name for name in live_names if call.project == name or call.project.startswith(name + "-")]
            if matches:
                call = replace(call, project=max(matches, key=len), project_is_fallback=False)
        folded.append(call)
    return folded


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
