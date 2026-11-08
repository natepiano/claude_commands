"""Let a Linux process adopt its orphaned descendants."""
from __future__ import annotations

import ctypes
import sys
from typing import cast


def adopt_descendants() -> bool:
    """Enable orphan adoption. Return false where prctl is unavailable."""
    if sys.platform != "linux":
        return False
    libc = ctypes.CDLL(None, use_errno=True)
    if not hasattr(libc, "prctl"):
        return False
    # PR_SET_CHILD_SUBREAPER is 36 in the Linux ABI.
    if cast(int, libc.prctl(36, 1, 0, 0, 0)) != 0:
        raise OSError(ctypes.get_errno(), "cannot adopt descendants")
    return True
