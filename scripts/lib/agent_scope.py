"""Probe the shared scope for agent processes."""

import os
import shutil
import subprocess

from scripts.lib import conf

_PANE_SCOPE_PROBE_SECONDS = 5.0


def agents_slice() -> str:
    """Read the current slice for agent processes."""
    return conf.read("codex_daemon").text("agents_slice")


def agent_scope_prefix() -> tuple[list[str], str]:
    """Probe the agent scope and return its command prefix or failure reason."""
    systemd_run = os.environ.get("SYSTEMD_RUN", "systemd-run")
    try:
        prefix = [shutil.which(systemd_run) or systemd_run, "--user", "--scope", "--quiet", "--collect",
                  f"--slice={agents_slice()}", "--"]
        probe = subprocess.run([*prefix, "true"], capture_output=True, text=True,
                               check=False, timeout=_PANE_SCOPE_PROBE_SECONDS)
    except (OSError, subprocess.TimeoutExpired, conf.Unusable) as error:
        return [], str(error)
    if probe.returncode != 0:
        return [], probe.stderr.strip() or f"exit {probe.returncode}"
    return prefix, ""
