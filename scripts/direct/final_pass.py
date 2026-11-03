"""Record and query a full gate pass for one clean Git tree."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import time
from typing import cast


def command(*args: str) -> str:
    return subprocess.check_output(args, text=True, stderr=subprocess.PIPE, timeout=30,
                                   env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"}).strip()


def clean_tree() -> tuple[str, str]:
    if command("git", "status", "--porcelain", "--untracked-files=all", "--ignore-submodules=none"):
        raise ValueError("the tree is dirty")
    head = command("git", "rev-parse", "HEAD")
    tree = command("git", "rev-parse", "HEAD^{tree}")
    return tree, head


def compiler() -> str:
    return command("rustc", "-vV")


def verify_hash(script: Path) -> str:
    return hashlib.sha256(script.read_bytes()).hexdigest()


def records() -> Path:
    return Path(os.environ.get("VERIFY_STATE_DIR", str(Path.home() / ".local/state/verify"))) / "final_pass"


def start(script: Path) -> dict[str, object]:
    try:
        tree, head = clean_tree()
        repo = command("git", "rev-parse", "--show-toplevel")
        try:
            repo = command("git", "remote", "get-url", "origin")
        except subprocess.CalledProcessError:
            pass
        return {"tree": tree, "head": head, "repo": repo, "rustc": compiler(),
                "verify_sha256": verify_hash(script)}
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        return {"reason": str(error).splitlines()[0]}


def finish(script: Path, initial: dict[str, object], status: int, features: str) -> None:
    try:
        if status:
            raise ValueError(f"final failed (exit {status})")
        if "reason" in initial:
            raise ValueError(str(initial["reason"]))
        tree, head = clean_tree()
        if tree != initial.get("tree"):
            raise ValueError("the tree changed while final ran")
        if compiler() != initial.get("rustc") or verify_hash(script) != initial.get("verify_sha256"):
            raise ValueError("the compiler or verify.sh changed while final ran")
        row = {**initial, "head": head, "features": features, "host": socket.gethostname(),
               "status": 0, "at": datetime.now(timezone.utc).isoformat()}
        folder = records()
        folder.mkdir(parents=True, exist_ok=True)
        cutoff = time.time() - 14 * 24 * 60 * 60
        for old in folder.glob("*.json"):
            try:
                if old.stat().st_mtime < cutoff:
                    old.unlink()
            except FileNotFoundError:
                pass
        with tempfile.NamedTemporaryFile(mode="w", dir=folder, prefix=".final-", delete=False) as output:
            temporary = Path(output.name)
            try:
                json.dump(row, output)
                output.flush()
                os.fsync(output.fileno())
            except BaseException:
                temporary.unlink(missing_ok=True)
                raise
        try:
            _ = temporary.replace(folder / f"{tree}.json")
            directory = os.open(folder, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)
        print(f"verify.sh: final pass recorded for tree {tree}")
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"verify.sh: no final pass record was written: {str(error).splitlines()[0]}")


def query(script: Path, tree: str) -> int:
    try:
        if not tree:
            tree, _ = clean_tree()
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", tree):
            raise ValueError("invalid tree id")
        path = records() / f"{tree}.json"
        try:
            value = cast(object, json.loads(path.read_text()))
        except FileNotFoundError:
            raise ValueError(f"no final pass record for tree {tree}") from None
        if not isinstance(value, dict):
            raise ValueError("invalid final pass record")
        row = cast(dict[str, object], value)
        if row.get("tree") != tree or type(row.get("status")) is not int or row["status"] != 0:
            raise ValueError("final pass record has a different tree or status")
        if row.get("rustc") != compiler():
            raise ValueError("final pass rustc differs")
        if row.get("verify_sha256") != verify_hash(script):
            raise ValueError("final pass verify.sh differs")
        for field in ("head", "repo", "features", "host", "at"):
            if not isinstance(row.get(field), str):
                raise ValueError(f"invalid final pass {field}")
        print(json.dumps(row))
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"verify.sh: {str(error).splitlines()[0]}", file=sys.stderr)
        return 1


def main(args: list[str]) -> int:
    action, script_text, *rest = args
    script = Path(script_text)
    if action == "query" and len(rest) <= 1:
        return query(script, rest[0] if rest else "")
    if action == "start" and not rest:
        print(json.dumps(start(script)))
        return 0
    if action == "finish" and len(rest) == 3:
        initial = cast(dict[str, object], json.loads(rest[0]))
        finish(script, initial, int(rest[1]), rest[2])
        return 0
    print("verify.sh: usage: final-pass [<tree>]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
