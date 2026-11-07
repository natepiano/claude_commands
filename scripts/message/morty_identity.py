"""Match the configured Morty runtime identity."""

from scripts.lib import conf
from scripts.production import staff


def matches(native_id: str) -> bool:
    config = conf.read("send")
    # A recovered configuration value cannot grant authority after a missing live key.
    if not isinstance(config.origin("MORTY_NATIVE_ID", str), conf.UsableLiveValue):
        return False
    try:
        expected = config.text("MORTY_NATIVE_ID")
        kind, _, identity = expected.partition(":")
        if kind not in ("session", "codex"):
            return False
        _ = staff.native_id(identity)
    except (conf.Unusable, staff.StaffRefused):
        return False
    return expected == native_id
