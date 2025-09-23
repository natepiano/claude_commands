#!/usr/bin/env python3

import json
import sys
import subprocess
import shutil
from pathlib import Path
from typing import TypedDict, NotRequired, cast

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

        # Check if basedpyright is available
        basedpyright_path = find_basedpyright()
        if not basedpyright_path:
            print(json.dumps({"systemMessage": "🐍 Python file edited (no basedpyright)"}))
            return

        # Run basedpyright from .claude directory for consistent config
        cwd_path = Path(hook_input['cwd'])
        claude_dir = cwd_path / '.claude'

        # Convert file_path to relative to .claude directory if possible
        try:
            relative_path = Path(file_path).relative_to(cwd_path)
            relative_to_claude = Path('..') / relative_path
        except ValueError:
            # If file is outside cwd, use absolute path
            relative_to_claude = Path(file_path)

        result = subprocess.run(
            [basedpyright_path, str(relative_to_claude), '--outputjson'],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=str(claude_dir) if claude_dir.exists() else None
        )

        # Parse output with proper types
        error_count, warning_count, error_lines, warning_lines = parse_basedpyright_output(result.stdout)

        # Build response
        if error_count == 0 and warning_count == 0:
            response = {"systemMessage": "✅ basedpyright passed"}
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