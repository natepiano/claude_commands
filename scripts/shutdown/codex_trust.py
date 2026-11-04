"""Trust the Codex launch folder for this process."""
import json
from pathlib import Path
from typing import Literal


def trust_arguments(kind: Literal["claude", "codex"], folder: str, arguments: list[str]) -> list[str]:
    if kind == "codex":
        return ["-c", 'projects={' + json.dumps(str(Path(folder).resolve()), ensure_ascii=False)
                + '={trust_level="trusted"}}', *arguments]
    return arguments
