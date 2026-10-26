"""A fake cargo and rustfmt for the tests that reach `style_plan.py gate`, so no test runs the real ones."""

from __future__ import annotations

import sys
from pathlib import Path

STATE = "FAKE_FMT_STATE"

# Each fake records its calls in <state>/<name>/calls, prints the stdout and stderr files of that folder, with each
# {cwd} replaced by its directory, and exits with the code in the exit file (0 when there is none).
SCRIPT = f"""import os
import sys
from pathlib import Path

state = Path(os.environ["{STATE}"]) / Path(sys.argv[0]).name
with (state / "calls").open("a") as calls:
    _ = calls.write(" ".join(sys.argv[1:]) + "\\n")
for name, stream in (("stdout", sys.stdout), ("stderr", sys.stderr)):
    if (state / name).exists():
        _ = stream.write((state / name).read_text().replace("{{cwd}}", str(Path.cwd())))
raise SystemExit(int((state / "exit").read_text()) if (state / "exit").exists() else 0)
"""
NAMES = ("cargo", "rustfmt")


def install(bin_dir: Path, state: Path) -> None:
    """Write both fakes into bin_dir, which the caller puts first on PATH, and make the state folder that the
    environment variable STATE names."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    for name in NAMES:
        (state / name).mkdir(parents=True, exist_ok=True)
        fake = bin_dir / name
        _ = fake.write_text(f"#!{sys.executable}\n" + SCRIPT)
        fake.chmod(0o755)


def calls(state: Path, name: str) -> list[str]:
    """The arguments of each call of one fake, one line per call, in order."""
    recorded = state / name / "calls"
    return recorded.read_text().splitlines() if recorded.exists() else []
