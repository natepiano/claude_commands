#!/usr/bin/env python3

import json
import os
import sys
import subprocess
import shutil
from pathlib import Path
from typing import TypedDict, NotRequired, cast

# A config file in an ancestor directory means the file belongs to a real Python
# project. Without one, basedpyright falls back to its default strictness and
# buries a throwaway script in reportAny/reportUnknown noise.
PROJECT_CONFIG_FILES: tuple[str, ...] = ('pyproject.toml', 'pyrightconfig.json', 'setup.cfg', 'setup.py')

# Session scratchpads and temp dirs hold disposable scripts, not maintained code.
TEMP_ROOTS: tuple[str, ...] = ('/tmp', '/private/tmp', '/var/folders', '/private/var/folders')

class Range(TypedDict):
    start: dict[str, int]  # {line: int, character: int}
    end: dict[str, int]    # {line: int, character: int}

class Diagnostic(TypedDict):
    file: str
    severity: str  # "error" | "warning" | "information"
    message: str
    range: Range
    rule: NotRequired[str]

class Summary(TypedDict):
    filesAnalyzed: int
    errorCount: int
    warningCount: int
    informationCount: int
    timeInSec: float

class BasedpyrightOutput(TypedDict):
    version: str
    time: str
    generalDiagnostics: list[Diagnostic]
    summary: Summary

class ToolInput(TypedDict):
    file_path: NotRequired[str]
    content: NotRequired[str]
    old_string: NotRequired[str]
    new_string: NotRequired[str]

class ToolResponse(TypedDict):
    filePath: NotRequired[str]
    success: NotRequired[bool]

class HookInput(TypedDict):
    session_id: str
    transcript_path: str
    cwd: str
    hook_event_name: str
    tool_name: str
    tool_input: ToolInput
    tool_response: ToolResponse

def get_file_path(hook_input: HookInput) -> str:
    """Extract file path from hook input data."""
    # Try tool_input first
    if 'file_path' in hook_input['tool_input']:
        return hook_input['tool_input']['file_path']

    # Try tool_response as fallback
    if 'filePath' in hook_input['tool_response']:
        return hook_input['tool_response']['filePath']

    return ""

def find_basedpyright() -> str | None:
    """Find basedpyright executable path."""
    # Try system PATH first
    path = shutil.which('basedpyright')
    if path:
        return path

    # Try local installation
    home_path = Path.home() / '.local' / 'bin' / 'basedpyright'
    if home_path.exists():
        return str(home_path)

    return None

def is_scratchpad(file_path: Path) -> bool:
    """True for disposable scripts — session scratchpads and temp directories."""
    if 'scratchpad' in file_path.parts:
        return True

    roots = list(TEMP_ROOTS)
    tmpdir = os.environ.get('TMPDIR')
    if tmpdir:
        roots.append(tmpdir.rstrip('/'))

    text = str(file_path)
    return any(text.startswith(root + '/') for root in roots)

def find_project_root(file_path: Path) -> Path | None:
    """Nearest ancestor holding a Python project config, or None if unconfigured."""
    for parent in file_path.parents:
        if any((parent / name).exists() for name in PROJECT_CONFIG_FILES):
            return parent
    return None

def parse_basedpyright_output(output_json: str) -> tuple[int, int, list[str], list[str]]:
    """Parse basedpyright JSON output and extract diagnostics."""
    try:
        output = cast(BasedpyrightOutput, json.loads(output_json))
    except json.JSONDecodeError:
        return 0, 0, [], []

    error_count = output['summary']['errorCount']
    warning_count = output['summary']['warningCount']
    error_lines: list[str] = []
    warning_lines: list[str] = []

    for diag in output['generalDiagnostics']:
        file_base = Path(diag['file']).name
        line_num = diag['range']['start']['line']
        message = diag['message']
        line_text = f"  {file_base}:{line_num}: {message}"

        if diag['severity'] == 'error':
            error_lines.append(f"❌ {line_text}")
        elif diag['severity'] == 'warning':
            warning_lines.append(f"⚠️ {line_text}")

    return error_count, warning_count, error_lines, warning_lines

def main() -> None:
    """Main hook logic."""
    try:
        # Read JSON input from stdin
        hook_input = cast(HookInput, json.load(sys.stdin))

        # Extract file path
        file_path = get_file_path(hook_input)

        # Check if it's a Python file
        if not file_path.endswith('.py'):
            print(json.dumps({"continue": True}))
            return

        target = Path(file_path)

        # Disposable scripts get no type-check gate at all
        if is_scratchpad(target):
            print(json.dumps({"continue": True}))
            return

        # Check if basedpyright is available
        basedpyright_path = find_basedpyright()
        if not basedpyright_path:
            print(json.dumps({"systemMessage": "🐍 Python file edited (no basedpyright)"}))
            return

        # Run from the project root so its config is picked up; fall back to the
        # file's own directory, where only the interpreter version is assumed.
        project_root = find_project_root(target)
        command = [basedpyright_path]
        if project_root:
            cwd = project_root
            command.append(str(target))
        else:
            cwd = target.parent
            command.extend(['--pythonversion', f'{sys.version_info.major}.{sys.version_info.minor}', target.name])
        command.append('--outputjson')

        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=10,
            cwd=str(cwd)
        )

        # Parse output with proper types
        error_count, warning_count, error_lines, warning_lines = parse_basedpyright_output(result.stdout)

        # Unconfigured directories run at default strictness, so their warnings
        # are noise about a project that was never set up. Errors still report.
        hidden_warnings = 0
        if not project_root:
            hidden_warnings = warning_count
            warning_count, warning_lines = 0, []

        # Build response
        if error_count == 0 and warning_count == 0:
            suffix = f" ({hidden_warnings} unconfigured warning(s) hidden)" if hidden_warnings else ""
            response = {"systemMessage": f"✅ basedpyright passed{suffix}"}
        elif error_count == 0:
            context = "\n"
            if warning_lines:
                context += "\n".join(warning_lines) + "\n"
            context += f"{warning_count} warnings\n"

            response = {
                "systemMessage": f"⚠️ basedpyright passed with {warning_count} warning(s)",
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": context
                }
            }
        else:
            context = "\n"
            if error_lines:
                context += "\n".join(error_lines) + "\n"
            if warning_lines:
                context += "\n".join(warning_lines) + "\n"
            context += f"{error_count} errors, {warning_count} warnings\n"

            response = {
                "systemMessage": "💥 basedpyright failed",
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": context
                }
            }

        print(json.dumps(response))

    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError):
        print(json.dumps({"systemMessage": "🐍 Python file edited (basedpyright issue)"}))
    except Exception:
        print(json.dumps({"continue": True}))

if __name__ == "__main__":
    main()