"""Require assign.sh as the parent of a worker launch."""
from __future__ import annotations

import os
from pathlib import Path
import shlex
import subprocess

ROOT = Path(__file__).resolve().parents[2]
ASSIGN = ROOT / 'scripts/direct/assign.py'
LAUNCHERS = {
    ROOT / 'scripts/direct/implement.sh',
    ROOT / 'scripts/direct/review.sh',
    ROOT / 'scripts/ask_a_friend/launch_friend.sh',
    ROOT / 'scripts/agents/agent_bg.sh',
}
REFUSAL = 'Start workers through assign.sh from the director session.'


def called_by_assign() -> bool:
    """Accept the assign parent through known launcher shells only."""
    pid = os.getppid()
    seen: set[int] = set()
    while pid > 1 and pid not in seen:
        seen.add(pid)
        try:
            result = subprocess.run(['ps', '-p', str(pid), '-o', 'ppid=', '-o', 'args='],
                                    capture_output=True, text=True, check=True)
            parent, command = result.stdout.strip().split(None, 1)
            words = shlex.split(command)
            script = Path(words[1]).resolve() if len(words) > 1 else Path()
            if script == ASSIGN and Path(words[0]).name.startswith('python'):
                return True
            if script not in LAUNCHERS or Path(words[0]).name not in ('bash', 'zsh', 'sh'):
                return False
            pid = int(parent)
        except (OSError, ValueError, subprocess.CalledProcessError):
            return False
    return False


def require_assign() -> None:
    if not called_by_assign():
        raise SystemExit(REFUSAL)


if __name__ == '__main__':
    require_assign()
