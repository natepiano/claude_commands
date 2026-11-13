"""Read saved AWS deployment records."""
from __future__ import annotations

import json
from pathlib import Path
from typing import cast

def read_record(path: Path) -> dict[str, object]:
    value = cast(object, json.loads(path.read_text()))
    if not isinstance(value, dict):
        raise ValueError(f'{path.name}: expected an evidence record')
    return cast(dict[str, object], value)
