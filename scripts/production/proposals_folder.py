"""Where a production's proposal issues live: a leaf, so readers such as the footer counts import no cycle."""
from __future__ import annotations

from pathlib import Path

from production_units import Refusal, production_field

PROPOSALS_FIELD = "Proposals"
PROPOSAL_TAG = "proposal"


def issues_folder(doc: Path) -> Path | None:
    """The folder the production doc's Proposals line names, or None when it names none."""
    try:
        lines = doc.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        lines = []
    try:
        folder = Path(production_field(lines, PROPOSALS_FIELD)).expanduser()
    except Refusal:
        return None
    # The doc lives at `<checkout>/docs/plans/<name>.md`; a relative folder is under the checkout.
    return folder if folder.is_absolute() else doc.parent.parent.parent / folder
