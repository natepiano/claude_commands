"""Share private copy settings and remote removal fixtures."""
from __future__ import annotations

from collections.abc import Generator, Sequence
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from scripts.copies import copies
from scripts.lint import memory_test_support


def copies_environment(root: Path) -> dict[str, str]:
    configuration = root / "copies.conf"
    _ = memory_test_support.write_config_fixture(configuration,
        f"root={root / 'gates'}\nwait_seconds=0.01\ngit_timeout_seconds=10\nguard_timeout_seconds=10\n")
    return {
        "HOME": str(root), "COPIES_CONFIG": str(configuration),
        "SCRATCH_ROOT": str(root / "scratch"), "SCRATCH_COPIES_ROOT": str(root / "copies"),
        "SCRATCH_HOLDS": str(root / "holds.json"),
    }


@contextmanager
def fake_remote_removal(mirrors: Sequence[copies.RemoteMirror]) -> Generator[mock.MagicMock]:
    for mirror in mirrors:
        copies.register_remote(mirror)
        with copies.remote_lease(mirror):
            pass

    def remove_record(mirror: copies.RemoteMirror) -> None:
        copies.remote_record_path(mirror).unlink()

    with mock.patch.object(copies, "remove_remote_mirror", side_effect=remove_record) as remove:
        yield remove


def mirror_arguments(mirror: copies.RemoteMirror, action: str) -> copies.CopyArguments:
    args = copies.CopyArguments()
    args.path, args.host, args.action = mirror.path, mirror.host, action
    args.owner, args.repository, args.session = mirror.owner, mirror.repository.name, mirror.session
    args.command = ["true"]
    return args


def publish_mirror(mirror: copies.RemoteMirror) -> int:
    copies.register_remote(mirror)
    return copies.mirror_run(mirror_arguments(mirror, "mirror-receive"))
