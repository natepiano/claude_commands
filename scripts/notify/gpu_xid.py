"""Plain meanings for the NVIDIA Xid codes that stop natedev's GPU."""
from __future__ import annotations

import sys

XID_MEANINGS: dict[str, str] = {
    "62": "the GPU controller stopped",
    "79": "the computer lost contact with the GPU",
    "119": "the GPU firmware stopped answering",
    "120": "the GPU firmware failed",
    "154": "the driver asks for a GPU reset",
}


def _main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("Usage: gpu_xid.py <code>", file=sys.stderr)
        return 2
    try:
        meaning = XID_MEANINGS[argv[0]]
    except KeyError:
        print(f"gpu_xid: unknown Xid code: {argv[0]!r}", file=sys.stderr)
        return 2
    print(meaning)
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
