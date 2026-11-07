#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""Publish and refresh files at short review links on this machine's tailnet."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

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
from pathlib import Path
from string import Template, ascii_lowercase, digits
from typing import cast
from urllib.parse import quote

from scripts.lib import atomic_file, conf


SNAPSHOT_ERRORS = (OSError, ValueError, subprocess.SubprocessError)


def private_name(name: str) -> bool:
    """Identify publisher metadata and unfinished writes in any letter case."""
    folded = name.casefold()
    return (folded in (".index.json", ".index.lock") or folded.startswith(".review-link-")
            or atomic_file.temporary_target(folded) is not None)


def review_root() -> Path:
    from scripts.production import change_routing

    return change_routing.review_root()


def link_host() -> str:
    """The local Tailscale IPv4 address, or an explicit host override."""
    if override := os.environ.get("REVIEW_LINKS_HOST"):
        return override.rstrip(".")
    result = subprocess.run(["tailscale", "ip", "-4"],
                            capture_output=True, text=True, check=True, timeout=conf.read("review_links").number("tailscale_timeout_seconds"))
    return str(ipaddress.IPv4Address(result.stdout.strip()))


def render_markdown(source: str, title: str) -> str:
    """Keep an escaped, readable copy visible until Markdown rendering succeeds."""
    metadata = ""
    front = re.match(r"\A\ufeff?---[ \t]*\r?\n(.*?)\r?\n(?:---|\.\.\.)[ \t]*(?:\r?\n|$)", source, re.DOTALL)
    if front and re.search(r"(?m)^[A-Za-z_][\w-]*:", front.group(1)):
        metadata = '<details><summary>Document metadata</summary><pre>' + html.escape(front.group(1)) + '</pre></details>'
        source = source[front.end():]
    return Template("""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>$title</title>
<style>
:root { color-scheme: light dark; font-family: system-ui, sans-serif; font-size: 14px; line-height: 1.5; }
h1 { font-size: 1.7rem; }
h2 { font-size: 1.35rem; }
h3 { font-size: 1.15rem; }
summary { cursor: pointer; color: #888; }
body { margin: auto; padding: 1rem; overflow-wrap: anywhere; }
main > :not(table):not(:has(table)), #markdown-source { max-width: 48rem; margin-left: auto; margin-right: auto; }
details { max-width: 48rem; margin: 0 auto 1rem; }
pre { white-space: pre; overflow-x: auto; overflow-wrap: normal; padding: 1rem; background: light-dark(#f0f2f5, #202329); border-radius: .5rem; }
code { font-family: ui-monospace, monospace; }
img { max-width: 100%; height: auto; }
table { display: table; table-layout: auto; width: 60%; max-width: 100%; margin-left: auto; margin-right: auto; border-collapse: collapse; }
td, th { min-width: 9ch; border: 1px solid #888; padding: .4rem .6rem; overflow-wrap: anywhere; word-break: normal; vertical-align: top; }
th { overflow-wrap: normal; }
td:first-child, th:first-child { min-width: 14ch; }
a { color: light-dark(#145ac2, #91bdff); }
</style></head><body>
$metadata
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
""").substitute(title=html.escape(title), source=html.escape(source), metadata=metadata)


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


def snapshot(files: list[Path], name: str = "review", *, new: bool = False) -> list[str]:
    """Refresh a source set's short URLs; new requests a separate snapshot."""
    return snapshot_at(review_root(), files, name, new=new)


def snapshot_at(root: Path, files: list[Path], name: str = "review", *, new: bool = False) -> list[str]:
    """Publish files under the root selected for this operation."""
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
    settings = conf.read("review_links")
    allocation_tries = settings.whole("snapshot_allocation_tries")
    port = settings.whole("server_port")
    host = link_host()
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".index.lock").open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        index = read_snapshot_index(root)
        source_set = json.dumps(sorted((str(file.resolve()), file.name) for file in files))
        reuse = not new and source_set in index
        if reuse:
            folder = root / index[source_set]
        else:
            for _ in range(allocation_tries):
                folder = root / "".join(secrets.choice(ascii_lowercase + digits) for _ in range(6))
                try:
                    folder.mkdir()
                except FileExistsError:
                    continue
                break
            else:
                raise FileExistsError(f"could not allocate a snapshot id after {allocation_tries} tries")
        try:
            for output, content in contents.items():
                atomic_file.write_bytes_atomic(folder / output, content, mode=0o600)
            for obsolete in folder.iterdir():
                if obsolete.name not in contents and (obsolete.is_file() or obsolete.is_symlink()):
                    obsolete.unlink()
            os.utime(folder, None)
            index[source_set] = folder.name
            atomic_file.write_bytes_atomic(root / ".index.json", json.dumps(index, sort_keys=True).encode("utf-8"), mode=0o600)
        except OSError:
            if not reuse:
                shutil.rmtree(folder)
            raise
    return [f"http://{host}:{port}/{quote((folder / output).relative_to(root).as_posix())}"
            for output in destinations]


def main(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("files", nargs="+", type=Path)
    _ = parser.add_argument("--name", default="review", help="accepted for compatibility; omitted from URLs")
    _ = parser.add_argument("--new", action="store_true", help="make a separate link instead of refreshing")
    args = parser.parse_args(arguments)
    try:
        urls = snapshot(cast(list[Path], args.files), cast(str, args.name), new=cast(bool, args.new))
    except SNAPSHOT_ERRORS as error:
        print(f"review_link: {error}", file=sys.stderr)
        return 2
    print("\n".join(urls))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
