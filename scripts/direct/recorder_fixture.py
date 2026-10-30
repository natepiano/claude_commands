"""Run the real recorder CLI against disposable state, including its lock."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
import os
import signal
import subprocess
from unittest.mock import patch

# Initialize dependencies before invoke_cli swaps HOME. The recorder currently
# imports user_zone eagerly; its production integrations remain real children.
from scripts.direct import progress_history
from scripts.lib import user_zone
from scripts.production.cli_fixture import invoke_cli


STUB_ATOMIC_JSON_WRITER = '''
def write_json(path, value):
    import json
    import os
    import tempfile
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=path.name + '.')
    try:
        with os.fdopen(descriptor, 'w') as output:
            json.dump(value, output)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
'''


def communicate_launcher(process: subprocess.Popen[str], timeout: float = 300) -> tuple[str, str]:
    """Reap a fixture's isolated launcher group before reporting a hang."""
    try:
        return process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        _ = process.communicate()
        raise


def invoke_recorder(arguments: Sequence[str], environment: Mapping[str, str],
                    cwd: Path, *, check: bool = False) -> subprocess.CompletedProcess[str]:
    home = Path(environment["HOME"])

    def main() -> int:
        progress_history.main()
        return 0

    progress_history._config_digest.cache_clear()  # pyright: ignore[reportPrivateUsage]
    try:
        with (
            patch.object(progress_history, "DEFAULT_CONFIG_PATH", home / ".claude/config/direct.conf"),
            patch.object(progress_history, "DEFAULT_AGENTS_CONFIG_PATH", home / ".claude/config/agents.conf"),
            patch.object(user_zone, "CONF", Path(environment.get("USER_ZONE_CONF") or
                                               str(Path(user_zone.__file__).parents[2] / "config/user_zone.conf"))),
        ):
            result = invoke_cli(progress_history, main, list(arguments), environment, cwd)
    finally:
        progress_history._config_digest.cache_clear()  # pyright: ignore[reportPrivateUsage]
    if check:
        result.check_returncode()
    return result
