"""Copy the session reader and its local imports into isolated test trees."""

from pathlib import Path
import shutil


_SESSION_MODULE_FILES = ("sessions.py", "session_records.py", "process_identity.py", "relay_identity.py")


def copy_session_modules(destination: Path) -> None:
    source = Path(__file__).resolve().parent
    destination.mkdir(parents=True, exist_ok=True)
    for name in _SESSION_MODULE_FILES:
        _ = shutil.copy2(source / name, destination / name)
    library = destination.parent / "lib"
    library.mkdir(parents=True, exist_ok=True)
    for name in ("session_paths.py", "boot_identity.py"):
        _ = shutil.copy2(source.parent / "lib" / name, library / name)
