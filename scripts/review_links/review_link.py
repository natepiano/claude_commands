#!/usr/bin/env python3
"""Publish immutable file snapshots as review links on this machine's tailnet."""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from string import Template
from typing import cast
from urllib.parse import quote
from uuid import uuid4

PORT = 8765


def review_root() -> Path:
    return Path(os.environ.get("REVIEW_LINKS_ROOT") or Path.home() / ".local/share/review-links").expanduser()


def hostname() -> str:
    """The local MagicDNS name; tests may supply it without contacting Tailscale."""
    if override := os.environ.get("REVIEW_LINKS_HOST"):
        return override.rstrip(".")
    result = subprocess.run(["tailscale", "status", "--self", "--json"],
                            capture_output=True, text=True, check=True, timeout=10)
    status = cast(object, json.loads(result.stdout))
    self_status = cast(dict[str, object], status).get("Self") if isinstance(status, dict) else None
    name = cast(dict[str, object], self_status).get("DNSName") if isinstance(self_status, dict) else None
    if not isinstance(name, str) or not name.rstrip("."):
        raise ValueError("Tailscale supplied no MagicDNS name")
    return name.rstrip(".")


def render_markdown(source: str, title: str) -> str:
    """Keep an escaped, readable copy visible until Markdown rendering succeeds."""
    return Template("""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>$title</title>
<style>
:root { color-scheme: light dark; font-family: system-ui, sans-serif; line-height: 1.6; }
body { max-width: 48rem; margin: auto; padding: 1rem; overflow-wrap: anywhere; }
pre { white-space: pre-wrap; overflow-wrap: anywhere; padding: 1rem; background: light-dark(#f0f2f5, #202329); border-radius: .5rem; }
code { font-family: ui-monospace, monospace; }
img { max-width: 100%; height: auto; }
table { display: block; overflow-x: auto; border-collapse: collapse; }
td, th { border: 1px solid #888; padding: .4rem .6rem; }
a { color: light-dark(#145ac2, #91bdff); }
</style></head><body>
<main id="rendered" hidden></main>
<pre id="markdown-source">$source</pre>
<script src="https://cdnjs.cloudflare.com/ajax/libs/marked/12.0.2/marked.min.js"></script>
<script>
try {
  if (window.marked) {
    const source = document.getElementById('markdown-source');
    const rendered = document.getElementById('rendered');
    rendered.innerHTML = marked.parse(source.textContent);
    rendered.hidden = false;
    source.hidden = true;
  }
} catch (_) { /* The source remains readable without the renderer. */ }
</script></body></html>
""").substitute(title=html.escape(title), source=html.escape(source))


def _safe_component(label: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", label).strip(".-")[:64] or "review"


def snapshot(files: list[Path], name: str = "review") -> list[str]:
    """Validate the entire request, copy it once, then return one URL per file."""
    if not files:
        raise ValueError("name at least one file")
    contents: dict[str, bytes] = {}
    output_sources: dict[str, Path] = {}
    destinations: list[str] = []
    for file in files:
        if not file.is_file():
            raise ValueError(f"not a regular file: {file}")
        outputs = {file.name: file.read_bytes()}
        destination = file.name
        if file.suffix.lower() == ".md":
            destination = file.with_suffix(".html").name
            outputs[destination] = render_markdown(outputs[file.name].decode("utf-8"), file.name).encode("utf-8")
        for output, content in outputs.items():
            folded = output.casefold()
            if folded in output_sources:
                raise ValueError(f"snapshot filename collision: {output_sources[folded]} and {file} ({output})")
            output_sources[folded] = file
            contents[output] = content
        destinations.append(destination)
    host = hostname()
    root = review_root()
    session = _safe_component(os.environ.get("CLAUDE_CODE_SESSION_ID", "")[:8] or "manual")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    folder = root / session / f"{stamp}-{_safe_component(name)}-{uuid4().hex[:12]}"
    folder.parent.mkdir(parents=True, exist_ok=True)
    folder.mkdir()
    try:
        for output, content in contents.items():
            _ = (folder / output).write_bytes(content)
    except OSError:
        shutil.rmtree(folder)
        raise
    return [f"http://{host}:{PORT}/{quote((folder / output).relative_to(root).as_posix())}"
            for output in destinations]


def main(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("files", nargs="+", type=Path)
    _ = parser.add_argument("--name", default="review", help="label for this snapshot")
    args = parser.parse_args(arguments)
    try:
        urls = snapshot(cast(list[Path], args.files), cast(str, args.name))
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"review_link: {error}", file=sys.stderr)
        return 2
    print("\n".join(urls))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
