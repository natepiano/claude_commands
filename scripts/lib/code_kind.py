"""Source classifications shared by repository scans."""
from pathlib import Path


def is_test_code(path: Path) -> bool:
    """Tests, fake tools and shared test helpers contain fixture code that production scans skip."""
    return path.name.startswith(("test_", "fake_")) or path.stem.endswith("_test_support")
