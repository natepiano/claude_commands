#!/usr/bin/env python3
"""Review the changes since the last simplify run, once per repository each day."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
from datetime import datetime, timezone
import os
from pathlib import Path
import subprocess
import sys
from typing import cast
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.copies import copies
from scripts.direct import simplify_result
from scripts.lib import conf, scratch, user_zone
from scripts.lib.atomic_file import write_text_atomic
from scripts.lib.process_failure import failure_detail
from scripts.usage import agents_log


def git(cwd: Path, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(["git", "-C", str(cwd), *arguments], capture_output=True, text=True, errors="replace", check=False)
    if check and result.returncode:
        raise RuntimeError(failure_detail(result))
    return result


def review(run: Path, cwd: Path, settings: conf.ConfigRead) -> int:
    remote = git(cwd, "ls-remote", "--symref", "origin", "HEAD").stdout
    branch = next(line.split()[1] for line in remote.splitlines() if line.startswith("ref:"))
    tracking = "refs/remotes/origin/" + branch.removeprefix("refs/heads/")
    _ = git(cwd, "fetch", "origin", f"+{branch}:{tracking}", "+refs/simplify/*:refs/simplify/*")
    tip = git(cwd, "rev-parse", tracking).stdout.strip()
    old = git(cwd, "rev-parse", "--verify", "refs/simplify/last", check=False)
    last = old.stdout.strip() if old.returncode == 0 else ""
    base = last or git(cwd, "rev-list", "-1", "--before=" + settings.text("SIMPLIFY_FIRST_RUN_BEFORE"), tip).stdout.strip()
    if not base:
        base = git(cwd, "rev-list", "--max-parents=0", tip).stdout.splitlines()[0]
    diff = git(cwd, "diff", base, tip).stdout
    if not diff:
        print("simplify: no changes")
        return 0
    day = datetime.now(user_zone.user_zone()).date().isoformat()
    claim = "refs/simplify/claim/" + day
    pushed = git(cwd, "push", "--porcelain", "origin", f"--force-with-lease={claim}:", f"{tip}:{claim}", check=False)
    if ("[rejected]" in pushed.stdout and "(stale info)" in pushed.stdout) or any(
        line.startswith("=\t") for line in pushed.stdout.splitlines()
    ):
        print("simplify: claimed today")
        return 0
    if pushed.returncode:
        raise RuntimeError(failure_detail(pushed))
    claims = git(cwd, "ls-remote", "origin", "refs/simplify/claim/*").stdout
    for line in claims.splitlines():
        ref = line.split()[1]
        if ref.removeprefix("refs/simplify/claim/") < day:
            deleted = git(cwd, "push", "origin", ":" + ref, check=False)
            if deleted.returncode:
                detail = "; ".join(failure_detail(deleted).splitlines())
                print(f"simplify: could not delete {ref}: {detail}", file=sys.stderr)

    directory = run / "simplify" / day
    directory.mkdir(parents=True, exist_ok=True)
    report_path = directory / "report.txt"
    status = directory / "status"
    log_path = directory / "agent.log"
    prompt = directory / "prompt.md"
    write_text_atomic(status, "preparing\n")
    state = "error"
    reason = ""
    try:
        write_text_atomic(directory / "diff", diff)
        write_text_atomic(directory / "source_prompt.md", (
            f"Repository: {git(cwd, 'remote', 'get-url', 'origin').stdout.strip()}\nBase: {base}\nTip: {tip}\n\n"
            'The user said: "the scope of the simplify needs to be for everything that has changed '
            'since the last simplify run on codebase that the unit is working on".\n'
        ))
        clone = copies.clone(cwd)
        try:
            _ = git(clone, "checkout", "--detach", tip)
            simplify_result.prepare(directory / "diff", directory / "source_prompt.md", prompt, report_path, settings)
            session = str(uuid4())
            try:
                agents_log.append(session, "review", "simplify", datetime.now(timezone.utc))
            except OSError as error:
                print("simplify: could not record assignment: " + " ".join(str(error).splitlines()), file=sys.stderr)
            env = dict(os.environ)
            env["AGENT_EXEC_EXTRA_ARGS"] = (env.get("AGENT_EXEC_EXTRA_ARGS", "") + f" --session-id {session}").strip()
            write_text_atomic(status, "reviewing\n")
            launched = subprocess.run([
                "bash", str(Path(__file__).resolve().parents[1] / "agents/agent_exec.sh"),
                "direct.simplify", "readonly", str(clone), str(prompt), str(report_path), str(log_path),
            ], env=env, check=False)
            report = report_path.read_text()
            log = log_path.read_text(errors="replace") if log_path.exists() else ""
            reason = simplify_result.validate(report, log, len(settings.words("SIMPLIFY_AGENTS")))
            if launched.returncode and not reason:
                reason = simplify_result.failure_reason(log, launched.returncode)
        finally:
            with scratch.removal_lock(clone.parent):
                scratch.remove_marked(clone.parent, scratch.remove)
        if not reason:
            write_text_atomic(status, "reviewed\n")
            _ = git(cwd, "push", "origin", f"--force-with-lease=refs/simplify/last:{last}", f"{tip}:refs/simplify/last")
            state = "reviewed"
    except (OSError, KeyError, ValueError, RuntimeError, copies.CopyError, scratch.ScratchError) as error:
        reason = str(error)
    if state == "error":
        write_text_atomic(status, "error\n")
        write_text_atomic(report_path, f"ERROR: {reason}\n")
        print(reason, file=sys.stderr)
    print(f"simplify: {state}; report {report_path}")
    return 0 if state == "reviewed" else 1


def main(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--session-dir", type=Path, required=True)
    _ = parser.add_argument("--cwd", type=Path, required=True)
    args = parser.parse_args(arguments)
    try:
        settings = conf.read("direct")
        if settings.text("SIMPLIFY_LENS") == "off":
            print("simplify: off")
            return 0
        return review(cast(Path, args.session_dir).expanduser().resolve(),
                      cast(Path, args.cwd).expanduser().resolve(), settings)
    except (OSError, ValueError, RuntimeError, StopIteration) as error:
        print(f"simplify: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
