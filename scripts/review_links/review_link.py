#!/usr/bin/env python3
"""Publish and refresh files at short review links on this machine's tailnet."""
from __future__ import annotations

import argparse
import fcntl
import html
import ipaddress
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from string import Template, ascii_lowercase, digits
from typing import cast
from urllib.parse import quote

PORT = 8765


def private_name(name: str) -> bool:
    """Identify publisher metadata and unfinished writes in any letter case."""
    folded = name.casefold()
    return folded in (".index.json", ".index.lock") or folded.startswith(".review-link-")


def review_root() -> Path:
    return Path(os.environ.get("REVIEW_LINKS_ROOT") or Path.home() / ".local/share/review-links").expanduser()


def link_host() -> str:
    """The local Tailscale IPv4 address, or an explicit host override."""
    if override := os.environ.get("REVIEW_LINKS_HOST"):
        return override.rstrip(".")
    result = subprocess.run(["tailscale", "ip", "-4"],
                            capture_output=True, text=True, check=True, timeout=10)
    return str(ipaddress.IPv4Address(result.stdout.strip()))


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
pre { white-space: pre; overflow-x: auto; overflow-wrap: normal; padding: 1rem; background: light-dark(#f0f2f5, #202329); border-radius: .5rem; }
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
    marked.use({renderer: {html(text) {
      return text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    }}});
    rendered.innerHTML = marked.parse(source.textContent);
    rendered.hidden = false;
    source.hidden = true;
  }
} catch (_) { /* The source remains readable without the renderer. */ }
</script></body></html>
""").substitute(title=html.escape(title), source=html.escape(source))


def read_snapshot_index(root: Path) -> dict[str, str]:
    """Retain only source sets whose snapshot folders still exist."""
    path = root / ".index.json"
    if not path.exists():
        return {}
    decoded = cast(object, json.loads(path.read_text(encoding="utf-8")))
    if not isinstance(decoded, dict):
        raise ValueError("invalid review-links index")
    entries = cast(dict[object, object], decoded)
    return {key: identifier for key, identifier in entries.items()
            if isinstance(key, str) and isinstance(identifier, str)
            and re.fullmatch(r"[a-z0-9]{6}", identifier)
            and not (root / identifier).is_symlink() and (root / identifier).is_dir()}


def replace_file(path: Path, content: bytes) -> None:
    """Publish complete bytes with one rename on the destination filesystem."""
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".review-link-", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            _ = stream.write(content)
        except OSError:
            temporary.unlink(missing_ok=True)
            raise
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def snapshot(files: list[Path], name: str = "review", *, new: bool = False) -> list[str]:
    """Refresh a source set's short URLs; new requests a separate snapshot."""
    _ = name
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
            if private_name(output):
                raise ValueError(f"reserved snapshot filename: {output} ({file})")
            if folded in output_sources:
                raise ValueError(f"snapshot filename collision: {output_sources[folded]} and {file} ({output})")
            output_sources[folded] = file
            contents[output] = content
        destinations.append(destination)
    host = link_host()
    root = review_root()
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".index.lock").open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        index = read_snapshot_index(root)
        source_set = json.dumps(sorted((str(file.resolve()), file.name) for file in files))
        reuse = not new and source_set in index
        if reuse:
            folder = root / index[source_set]
        else:
            for _ in range(10):
                folder = root / "".join(secrets.choice(ascii_lowercase + digits) for _ in range(6))
                try:
                    folder.mkdir()
                except FileExistsError:
                    continue
                break
            else:
                raise FileExistsError("could not allocate a snapshot id after 10 tries")
        try:
            for output, content in contents.items():
                replace_file(folder / output, content)
            for obsolete in folder.iterdir():
                if obsolete.name not in contents and (obsolete.is_file() or obsolete.is_symlink()):
                    obsolete.unlink()
            os.utime(folder, None)
            index[source_set] = folder.name
            replace_file(root / ".index.json", json.dumps(index, sort_keys=True).encode("utf-8"))
        except OSError:
            if not reuse:
                shutil.rmtree(folder)
            raise
    return [f"http://{host}:{PORT}/{quote((folder / output).relative_to(root).as_posix())}"
            for output in destinations]


def main(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("files", nargs="+", type=Path)
    _ = parser.add_argument("--name", default="review", help="accepted for compatibility; omitted from URLs")
    _ = parser.add_argument("--new", action="store_true", help="make a separate link instead of refreshing")
    args = parser.parse_args(arguments)
    try:
        urls = snapshot(cast(list[Path], args.files), cast(str, args.name), new=cast(bool, args.new))
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"review_link: {error}", file=sys.stderr)
        return 2
    print("\n".join(urls))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
