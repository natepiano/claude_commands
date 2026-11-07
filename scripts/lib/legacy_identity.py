"""Remove retired staff identity from process environments."""
from __future__ import annotations

from collections.abc import Mapping


def without_legacy_identity(environment: Mapping[str, str]) -> dict[str, str]:
    """Keep environment values that do not identify retired staff ownership."""
    return {key: value for key, value in environment.items()
            if key != "STAFF_ID" and not key.startswith("SHOWRUNNER_")}
