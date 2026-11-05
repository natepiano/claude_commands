"""Open Codex in its launch folder and trust that folder for this process."""
import json
from pathlib import Path
from typing import Literal


def trust_arguments(kind: Literal["claude", "codex"], folder: str, arguments: list[str]) -> list[str]:
    if kind == "codex":
        # -C sets the working root, so a resume after a folder move asks no directory question.
        root = str(Path(folder).resolve())
        return ["-C", root, "-c", 'projects={' + json.dumps(root, ensure_ascii=False)
                + '={trust_level="trusted"}}', *arguments]
    return arguments
