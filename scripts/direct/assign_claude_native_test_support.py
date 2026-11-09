"""Same-path Claude launcher fixtures with cheap, synchronous fake CLI replies."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import shlex
import sys
import tempfile
from typing import ClassVar, override

from scripts.direct import assign_claude_test_support as support


# The fake CLI uses shell builtins for repeated agents and logs commands.
CLAUDE_REPLY_SHIM = r'''#!/bin/bash
set -euo pipefail
root="$STUB_CLAUDE_ROOT"
if [[ "$1" == logs ]]; then
    if [[ -f "$root/pane.bin" ]]; then cat "$root/pane.bin"; else printf 'stub Claude log\n'; fi
elif [[ "$1" == agents && "$2" == --json ]]; then
    if [[ -f "$root/unreadable_listing_once" ]]; then
        rm "$root/unreadable_listing_once"
        : > "$root/poll-ready"
        printf 'not JSON\n'
    else
        IFS= read -r status < "$root/worker_state" || true
        if [[ "$status" == busy ]]; then
            printf 'idle' > "$root/worker_state"
            : > "$root/poll-ready"
        fi
        cat "$root/listing-$status.json"
        printf '\n'
    fi
else
    exec __PYTHON__ "$root/claude-original.py" "$@"
fi
'''


class ClaudeLauncherFixture(support.AssignClaudeLauncherFixture):
    fixture_root: ClassVar[Path]
    fixture_snapshot: ClassVar[Path]

    @classmethod
    @override
    def setUpClass(cls) -> None:
        directory = tempfile.TemporaryDirectory(prefix="launcher-claude-")
        cls.addClassCleanup(directory.cleanup)
        cls.fixture_root = Path(directory.name) / "case"
        cls.fixture_snapshot = cls.fixture_root.parent / "snapshot"
        cls.fixture_root.mkdir()
        builder = cls("runTest")
        builder.root = cls.fixture_root
        builder.prepare_fixture()
        # Keep the original Python fake for --bg, including UUID identity and
        # account environment recording. It also prepares exact listing replies.
        original = support.STUB_CLAUDE.replace(
            "    print(f'backgrounded",
            "    for status in ('busy', 'idle'):\n"
            + "        (root / f'listing-{status}.json').write_text(json.dumps([{'id': 'abc12345', 'sessionId': 'abc12345-' + str(uuid.uuid5(uuid.NAMESPACE_URL, name))[9:], 'name': name, 'status': status}]))\n"
            + "    print(f'backgrounded",
        )
        _ = (builder.root / "claude-original.py").write_text(original)
        for status in ("busy", "idle", "gone"):
            rows = [] if status == "gone" else [{"id": "abc12345", "sessionId": "abc12345-2222-4333-8444-555555555555", "name": "fixture-impl", "status": status}]
            _ = (builder.root / f"listing-{status}.json").write_text(json.dumps(rows))
        _ = shutil.copytree(cls.fixture_root, cls.fixture_snapshot)
        shutil.rmtree(cls.fixture_root)

    @override
    def setUp(self) -> None:
        self.root: Path = self.fixture_root
        _ = shutil.copytree(self.fixture_snapshot, self.root)
        self.history_dir: Path = self.root / "history"
        self.working_dir: Path = self.root / "project"
        self.config_file: Path = self.root / "direct.conf"
        self.prompt_file: Path = self.root / "implementation_prompt.md"
        self.assign_script: Path = self.root / "scripts/direct/assign.sh"
        _ = self.staff_parent
        _ = (self.root / "claude-bin").write_text(self.cli_shim())

    @override
    def tearDown(self) -> None:
        shutil.rmtree(self.root)

    def cli_shim(self) -> str:
        return CLAUDE_REPLY_SHIM.replace("__PYTHON__", shlex.quote(sys.executable))
