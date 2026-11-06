"""Read the kernel identity for one process filesystem."""
from pathlib import Path

PROC_ROOT = Path("/proc")


def parse_boot_id(text: str) -> str:
    identity = text.strip()
    if not identity:
        raise ValueError("boot identity is missing")
    return identity


def boot_identity(proc_root: Path = PROC_ROOT) -> str:
    return parse_boot_id((proc_root / "sys/kernel/random/boot_id").read_text())
