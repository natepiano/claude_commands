"""Read one complete Codex-owned models cache, tolerating an in-place rewrite."""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import cast, final


# Codex truncates and rewrites this file. Retry only unreadable snapshots;
# a complete catalog without a requested model is an authoritative answer.
CACHE_RETRY_DELAYS = (0.1,) * 20


class ModelsCacheUnreadable(ValueError):
    """No complete cache was available within the retry window."""


def read_models_cache(path: Path, *, retry_delays: Sequence[float] = CACHE_RETRY_DELAYS,
                      pause: Callable[[float], None] = time.sleep) -> dict[str, object]:
    """Return one parsed snapshot; all consumers share this file-reading boundary."""
    for attempt in range(len(retry_delays) + 1):
        try:
            document = cast(object, json.loads(path.read_text(encoding="utf-8")))
            if not isinstance(document, dict):
                raise ValueError("expected a JSON object with a models array")
            snapshot = cast(dict[str, object], document)
            if not isinstance(snapshot.get("models"), list):
                raise ValueError("expected a models array")
            return snapshot
        except (OSError, ValueError, UnicodeError) as error:
            if attempt == len(retry_delays):
                raise ModelsCacheUnreadable(
                    f"the models cache could not be read: {path}: {error}"
                ) from error
        pause(retry_delays[attempt])
    raise AssertionError("cache retry loop did not return or raise")


@final
class Arguments(argparse.Namespace):
    path: Path = Path.home() / ".codex/models_cache.json"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("path", type=Path, nargs="?", default=Arguments.path)
    arguments = parser.parse_args(namespace=Arguments())
    try:
        snapshot = read_models_cache(arguments.path)
    except ModelsCacheUnreadable as error:
        print(error, file=sys.stderr)
        return 2
    print(json.dumps(snapshot))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
