"""The agent registry as Python readers see it: the committed defaults with this machine's overlay.

`config/agents.local.conf` (untracked) holds only the rows that differ from `config/agents.conf`;
`scripts/agents/agents_overlay.awk` merges the two, the same program `agents_config.sh` runs, so
Python and bash readers can never see different rows.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

DEFAULT_PATH = Path("~/.claude/config/agents.conf").expanduser()
OVERLAY_AWK = Path(__file__).resolve().parents[1] / "agents" / "agents_overlay.awk"


def registry_path() -> Path:
    """The committed registry: `AGENTS_CONFIG_FILE` when set, else the default."""
    configured = os.environ.get("AGENTS_CONFIG_FILE")
    return Path(configured) if configured else DEFAULT_PATH


def overlay_path(registry: Path) -> Path:
    """The overlay beside `registry`: `AGENTS_LOCAL_CONFIG_FILE` when set, else `<name>.local.conf`."""
    configured = os.environ.get("AGENTS_LOCAL_CONFIG_FILE")
    if configured:
        return Path(configured)
    stem = registry.name[: -len(".conf")] if registry.name.endswith(".conf") else registry.name
    return registry.with_name(f"{stem}.local.conf")


def registry_text(registry: Path | None = None) -> str:
    """The registry text with the overlay applied. Raises OSError when the registry cannot be read."""
    path = registry or registry_path()
    overlay = overlay_path(path)
    if not overlay.is_file():
        return path.read_text(encoding="utf-8")
    _ = path.stat()
    merged = subprocess.run(
        ["awk", "-v", "mode=view", "-f", str(OVERLAY_AWK), str(overlay), str(path)],
        capture_output=True, text=True, check=False,
    )
    if merged.returncode != 0:
        raise OSError(f"cannot merge {overlay} over {path}: {merged.stderr.strip()}")
    return merged.stdout
