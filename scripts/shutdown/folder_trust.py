"""Folder trust policy shared by recorded restart and in-pane resume.

Account discovery stays in accounts; trust-file inspection and copying stay in herdr.
Neither caller needs to import the other's launch machinery.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from threading import Lock

import herdr

# A parallel batch shares the account file and herdr's per-process staging path.
_TRUST_WRITE = Lock()


@dataclass(frozen=True)
class FolderNotTrusted:
    reason: str
    command: str


@dataclass(frozen=True)
class FolderTrustUnreadable:
    reason: str
    command: str


def heal(folder: Path, target_dir: Path, source_dir: Path, *, dry_run: bool = False
         ) -> herdr.Trusted | herdr.TrustCopied | FolderNotTrusted | FolderTrustUnreadable:
    """Copy existing trust to the target, or return the one-time trust command."""
    from scripts.accounts import accounts

    with _TRUST_WRITE:
        trust = herdr.heal_claude_trust(folder, target_dir / ".claude.json",
                                       accounts.trust_sources(target_dir, source_dir), dry_run=dry_run)
    if isinstance(trust, (herdr.Trusted, herdr.TrustCopied)):
        return trust
    command = accounts.trust_command(folder, accounts.NamedAccount(accounts.claude_config_label(target_dir), target_dir))
    if isinstance(trust, herdr.UnreadableTrustFile):
        return FolderTrustUnreadable(str(trust), command)
    return FolderNotTrusted(f"{folder}: Claude has not trusted this folder", command)
