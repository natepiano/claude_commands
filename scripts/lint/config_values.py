"""Read key-value settings shared by lint and build targets."""

import os
import re
import sys
from pathlib import Path

DEFAULT_BUILD_OOM_SCORE_ADJ = 500


def config_values(path: str) -> dict[str, str]:
    """lint.conf's key=value pairs, read as lint_config.sh's _lint_config_raw
    reads them: '#' starts a comment, [section] lines are skipped, the first
    value of a key wins."""
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.readlines()
    except (OSError, UnicodeError):
        return {}
    values: dict[str, str] = {}
    for raw in lines:
        line = raw.split("#", 1)[0].strip()
        if not line or (line.startswith("[") and line.endswith("]")) or "=" not in line:
            continue
        key, _, value = line.partition("=")
        _ = values.setdefault(key.strip(), value.strip())
    return values


def build_oom_score_adj() -> int:
    """Build scope's kernel kill adjustment; keep its kill order on bad config."""
    path = os.environ.get("LINT_CONFIG_FILE") or str(Path.home() / ".claude/config/lint.conf")
    values = config_values(path)
    if "build_oom_score_adj" not in values:
        return DEFAULT_BUILD_OOM_SCORE_ADJ
    value = values["build_oom_score_adj"]
    if re.fullmatch(r"-?(0|[1-9][0-9]{0,3})", value):
        adjustment = int(value)
        if -1000 <= adjustment <= 1000:
            return adjustment
    print(f"warning: {path}: build_oom_score_adj={value!r} is invalid "
          + f"(expected integer -1000..1000) — using {DEFAULT_BUILD_OOM_SCORE_ADJ}", file=sys.stderr)
    return DEFAULT_BUILD_OOM_SCORE_ADJ
