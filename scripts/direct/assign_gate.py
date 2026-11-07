"""Require assign.sh as the parent of a worker launch."""
from __future__ import annotations

import os
import shlex
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[2]
ASSIGN_LAUNCHERS = frozenset({"assign.sh", "assign.py"})
WORKER_LAUNCHERS = ASSIGN_LAUNCHERS | {"review.sh"}
ASSIGN = ROOT / 'scripts/direct/assign.py'
LAUNCHERS = {
    ROOT / 'scripts/direct/review.sh',
    ROOT / 'scripts/agents/agent_exec.sh',
}
REFUSAL = 'Start workers through assign.sh from the director session.'


def assign_role(role: str) -> str:
    """Return the role name accepted by assign.sh."""
    return {"impl": "implement", "astra-designer": "implement", "astra-reviewer": "review"}.get(role, role)


def assign_command(run_dir: str, worker: str, role: str, options: list[tuple[str, str]]) -> str:
    """Build an assignment command with the current worker and run."""
    current = {"--role": assign_role(role), "--session-dir": run_dir, "--to": worker}
    pairs = [(option, current.get(option, value)) for option, value in options if option != "--new-worker"]
    supplied = {pair[0] for pair in pairs}
    pairs.extend((option, value) for option, value in current.items() if option not in supplied)
    return shlex.join(["bash", str(ROOT / "scripts/direct/assign.sh"), *(value for pair in pairs for value in pair)])


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
            words = command.split()
            script = Path(words[1]).resolve() if len(words) > 1 else Path()
            if script == ASSIGN and Path(words[0]).name.startswith('python'):
                return True
            shell_hop = script in LAUNCHERS and Path(words[0]).name in ('bash', 'zsh', 'sh')
            if not shell_hop:
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
