"""Read key-value settings shared by lint and build targets."""

def config_values(path: str) -> dict[str, str]:
    """lint.conf's key=value pairs, read as lint_config.sh's _lint_config_raw
    reads them: '#' starts a comment, [section] lines are skipped, the first
    value of a key wins."""
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.readlines()
    except OSError:
        return {}
    values: dict[str, str] = {}
    for raw in lines:
        line = raw.split("#", 1)[0].strip()
        if not line or (line.startswith("[") and line.endswith("]")) or "=" not in line:
            continue
        key, _, value = line.partition("=")
        _ = values.setdefault(key.strip(), value.strip())
    return values

