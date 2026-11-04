#!/usr/bin/env python3
"""Move production pins to fixed staff ids. Delete this migration in Phase 18."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import cast

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.lib.atomic_file import sync_folder, write_text_atomic
from scripts.production import staff
from scripts.production.store_owner import showrunner_id

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import urgency

KINDS = ("next", "proposal", "question")


def queue_owner(queue: str) -> tuple[str, str, bool]:
    """Separate the queue kind, owner reference and descendant scope."""
    below = queue.endswith("-all-below")
    bare = queue.removesuffix("-all-below") if below else queue
    for kind in KINDS:
        if bare.startswith(f"{kind}-"):
            return kind, bare[len(kind) + 1:], below
    raise staff.StaffRefused(f"{queue}: not a production pin")


def migrate(*, stop_after: int = 0) -> int:
    """Publish each destination before removal of its source."""
    completed = 0
    problems: list[str] = []
    with staff.operation(), urgency.locked():
        for source in sorted(urgency.state_dir().glob("*.json")):
            if not source.name.startswith(tuple(f"{kind}-" for kind in KINDS)):
                continue
            try:
                # Resolve ownership again for each file after a possible clear.
                record = staff.snapshot()

                def identity(reference: str) -> str:
                    return showrunner_id(reference, record=record)

                def queue_name(queue: str) -> str:
                    kind, reference, below = queue_owner(queue)
                    return urgency.scope_queue(f"{kind}-{identity(reference)}", below)

                def item_key(key: str) -> str:
                    reference, separator, text = key.partition(": ")
                    return f"{identity(reference)}: {text}" if separator else key

                kind, _, _ = queue_owner(source.stem)
                target = source.with_name(f"{queue_name(source.stem)}.json")
                original = source.read_text(encoding="utf-8")
                value = cast(object, json.loads(original))
                if not isinstance(value, dict):
                    raise staff.StaffRefused(f"{source}: expected a pin object")
                fields = cast(dict[str, object], value)
                origin = fields.get("source", "")
                question = kind == "question" or (isinstance(origin, str) and origin.startswith("question-"))
                if isinstance(origin, str) and origin.startswith(tuple(f"{each}-" for each in KINDS)):
                    fields["source"] = queue_name(origin)
                if question:
                    current = fields.get("current")
                    if isinstance(current, str):
                        fields["current"] = item_key(current)
                    ended = fields.get("ended")
                    if isinstance(ended, dict):
                        moved = {item_key(key): when for key, when in cast(dict[str, object], ended).items()}
                        if len(moved) != len(cast(dict[str, object], ended)):
                            raise staff.StaffRefused(f"{source}: duplicate migrated item keys")
                        fields["ended"] = moved
                updated = json.dumps(fields) + "\n"
                if target == source:
                    if fields != json.loads(original):
                        write_text_atomic(target, updated)
                    else:
                        continue
                else:
                    if target.exists():
                        if json.loads(target.read_text(encoding="utf-8")) != fields:
                            raise staff.StaffRefused(f"{target}: unequal migration destination")
                    else:
                        write_text_atomic(target, updated)
                    source.unlink()
                    sync_folder(source.parent)
                completed += 1
                print(f"moved {source.name} to {target.name}")
                if stop_after and completed >= stop_after:
                    return 75
            except (OSError, ValueError, staff.StaffRefused, staff.StaffUnreadable) as error:
                problems.append(str(error))
    for problem in problems:
        print(f"unavailable: {problem}", file=sys.stderr)
    print(f"moved {completed}; unavailable {len(problems)}")
    return 1 if problems else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--stop-after", type=int, default=0)
    args = parser.parse_args()
    try:
        return migrate(stop_after=cast(int, args.stop_after))
    except (OSError, ValueError, staff.StaffRefused, staff.StaffUnreadable) as error:
        print(f"migration: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
