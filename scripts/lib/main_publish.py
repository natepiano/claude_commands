"""Commit a main-checkout change, push it, and send or queue the Mac pull."""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import subprocess
import sys
from typing import cast

from scripts.lib.atomic_file import write_text_atomic


class Refusal(Exception):
    """The checkout cannot publish this change."""


@dataclass(frozen=True)
class ExistingFile:
    text: str


@dataclass(frozen=True)
class MissingFile:
    """The destination did not exist before the edit."""


@dataclass(frozen=True)
class MacPullSourceUnrecorded:
    """An older queue record uses the checkout supplied to drain."""


@dataclass(frozen=True)
class MacCheckoutPull:
    mac: PurePosixPath
    source: Path | MacPullSourceUnrecorded


DEFAULT_MAC_CHECKOUT = PurePosixPath("~/.claude")
_LEGACY_MAC_PULL = (MacCheckoutPull(DEFAULT_MAC_CHECKOUT, MacPullSourceUnrecorded()),)


@dataclass(frozen=True)
class PendingMacPull:
    label: str
    text: str
    since: str
    reason: str
    pulls: tuple[MacCheckoutPull, ...] = _LEGACY_MAC_PULL


@dataclass(frozen=True)
class NoPendingMacPull:
    """There is no valid queued Mac pull."""


@dataclass(frozen=True)
class MacPulled:
    """The Mac completed its pull."""


@dataclass(frozen=True)
class MacPullPending:
    """The Mac is unreachable and its pull is queued."""


@dataclass(frozen=True)
class MacPullFailed:
    reason: str


@dataclass(frozen=True)
class CommandTimeLimit:
    seconds: float


@dataclass(frozen=True)
class NoTimeLimit:
    """The caller retains the existing unlimited Git operation."""


UNLIMITED_GIT = NoTimeLimit()
ZONE_SSH_LIMIT = CommandTimeLimit(120)
PUBLICATION_PREFIXES = ("tune(", "zone(", "user zone: ")


def _timeout_detail(error: subprocess.TimeoutExpired) -> str:
    streams = [output.decode(errors="replace")
               for output in (error.stderr, error.stdout) if output]
    return f"timed out after {error.timeout:g} seconds" + "".join(f"; {output.strip()}" for output in streams)


def git(checkout: Path, *arguments: str,
        time_limit: CommandTimeLimit | NoTimeLimit = UNLIMITED_GIT) -> subprocess.CompletedProcess[str]:
    command = ["git", "-C", str(checkout), *arguments]
    try:
        return subprocess.run(command, capture_output=True, text=True, check=False,
                              timeout=time_limit.seconds if isinstance(time_limit, CommandTimeLimit) else None)
    except subprocess.TimeoutExpired as error:
        return subprocess.CompletedProcess(command, 124, "", _timeout_detail(error))


def first_line(result: subprocess.CompletedProcess[str]) -> str:
    parts = [output.strip().splitlines()[0] for output in (result.stderr, result.stdout) if output.strip()]
    return "; ".join(parts) if parts else f"exit {result.returncode}"


def checkout_of(conf: Path) -> Path:
    found = git(conf.parent, "rev-parse", "--show-toplevel")
    if found.returncode:
        raise Refusal(f"{conf.parent} is not in a git checkout: {first_line(found)}")
    return Path(found.stdout.strip())


def ready(checkout: Path, *, fetch_limit: CommandTimeLimit | NoTimeLimit = UNLIMITED_GIT) -> None:
    """Update main from origin and rebase pending publisher commits when main diverges."""
    branch = git(checkout, "branch", "--show-current").stdout.strip()
    if branch != "main":
        raise Refusal(f"{checkout} is on {branch or 'no branch'}, not main")
    fetched = git(checkout, "fetch", "-q", "origin", "main", time_limit=fetch_limit)
    if fetched.returncode:
        raise Refusal(f"git fetch origin main: {first_line(fetched)}")
    listed = git(checkout, "log", "--format=%h %s", "origin/main..main")
    if listed.returncode:
        raise Refusal(f"cannot inspect unpublished commits: {first_line(listed)}")
    commits = listed.stdout.splitlines()
    for commit_line in commits:
        if not commit_line.partition(" ")[2].startswith(PUBLICATION_PREFIXES):
            raise Refusal(f"main holds other commit {commit_line} that origin does not have; main is unchanged")
    merged = git(checkout, "merge", "-q", "--ff-only", "origin/main")
    if not merged.returncode:
        return
    if not commits:
        raise Refusal(f"{checkout} main does not take origin/main as a fast-forward: {first_line(merged)}")
    rebased = git(checkout, "rebase", "--no-autostash", "--no-update-refs", "--empty=drop", "origin/main")
    if not rebased.returncode:
        return
    conflicted = git(checkout, "diff", "--name-only", "--diff-filter=U")
    if conflicted.stdout:
        stopped = git(checkout, "show", "-s", "--format=%h %s", "REBASE_HEAD")
        cause = f"rebase conflict in {stopped.stdout.strip()}: {', '.join(conflicted.stdout.splitlines())}"
    else:
        cause = f"rebase refused: {first_line(rebased)}"
    aborted = git(checkout, "rebase", "--abort")
    if aborted.returncode and conflicted.stdout:
        raise Refusal(f"{cause}; cannot restore main: {first_line(aborted)}")
    raise Refusal(" ".join(cause.split()))


def commit(checkout: Path, conf: Path, message: str, before: ExistingFile | MissingFile,
           *, subject_prefix: str, unchanged: str) -> str:
    """Commit only the named file; restore it if Git fails and HEAD lacks its contents."""
    if not message.startswith(subject_prefix):
        raise Refusal(f"commit subject must start with {subject_prefix!r}")
    committed = git(checkout, "commit", "-q", "--only", "-m", message, "--", str(conf))
    if committed.returncode:
        if git(checkout, "diff", "--quiet", "HEAD", "--", str(conf)).returncode == 0:
            print(f"anomaly: git commit: {first_line(committed)}; the commit exists", file=sys.stderr)
        else:
            if isinstance(before, MissingFile):
                conf.unlink(missing_ok=True)
            else:
                write_text_atomic(conf, before.text)
            raise Refusal(f"git commit: {first_line(committed)}; {unchanged}")
    return git(checkout, "rev-parse", "--short", "HEAD").stdout.strip()


def push(checkout: Path, *, time_limit: CommandTimeLimit | NoTimeLimit = UNLIMITED_GIT) -> tuple[bool, str]:
    """Push main only when every unpublished commit belongs to a configuration publisher."""
    listed = git(checkout, "log", "--format=%s", "origin/main..main")
    if listed.returncode:
        return False, f"not pushed: {first_line(listed)}"
    unpushed = listed.stdout.splitlines()
    if not unpushed:
        return True, "origin has it"
    others = [subject for subject in unpushed if not subject.startswith(PUBLICATION_PREFIXES)]
    if others:
        return False, f"not pushed: main holds {len(others)} other commit(s) origin does not have"
    pushed = git(checkout, "push", "-q", "origin", "main", time_limit=time_limit)
    if pushed.returncode:
        return False, f"not pushed: {first_line(pushed)}"
    return True, "pushed"


def ssh(remote: str, *, ssh_program: str, host: str = "mac",
        time_limit: CommandTimeLimit = ZONE_SSH_LIMIT) -> subprocess.CompletedProcess[str] | str:
    """Run a remote command; a string describes why it could not run."""
    command = [ssh_program, "-o", "BatchMode=yes", host, remote]
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=time_limit.seconds, check=False)
    except subprocess.TimeoutExpired as error:
        return _timeout_detail(error)
    except OSError as error:
        return str(error)


def remote_path(path: PurePosixPath) -> str:
    """Quote a Mac path and let the Mac expand its home directory."""
    text = str(path)
    if text == "~":
        return '"$HOME"'
    if text.startswith("~/"):
        return '"$HOME"/' + shlex.quote(text[2:])
    return shlex.quote(text)


def mac_pull_command(mac: PurePosixPath, source: Path) -> str:
    """Fetch main without shared fetch state and merge its advertised tip."""
    url = f"natedev:{shlex.quote(str(source))}"
    return (f"cd {remote_path(mac)} && tip=$(git ls-remote {url} refs/heads/main | cut -f1) "
            f"&& git fetch -q --no-write-fetch-head {url} main "
            "&& git merge -q --ff-only \"$tip\"; printf 'rc=%s\\n' \"$?\"")


def pull_mac(checkout: Path, *, ssh_program: str, mac: PurePosixPath = DEFAULT_MAC_CHECKOUT,
             time_limit: CommandTimeLimit = ZONE_SSH_LIMIT) -> MacPulled | MacPullFailed:
    """Return the completed Mac pull or its failure reason."""
    done = ssh(mac_pull_command(mac, checkout), ssh_program=ssh_program, time_limit=time_limit)
    if isinstance(done, str):
        return MacPullFailed(done)
    match = re.search(r"(?:^|\n)rc=(\d+)", done.stdout)
    if done.returncode or match is None or int(match.group(1)):
        return MacPullFailed(first_line(done))
    return MacPulled()


def queue_path() -> Path:
    state = os.environ.get("ZONE_STATE") or str(
        Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "zone")
    return Path(state) / "mac.json"


def read_queue() -> PendingMacPull | NoPendingMacPull:
    try:
        value = cast(object, json.loads(queue_path().read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return NoPendingMacPull()
    if not isinstance(value, dict):
        return NoPendingMacPull()
    fields = cast(dict[str, object], value)
    if not all(isinstance(fields.get(key), str) for key in ("zone", "text", "since", "reason")):
        return NoPendingMacPull()
    pulls: list[MacCheckoutPull] = []
    for value in cast(list[object], fields.get("pulls", [{"mac": str(DEFAULT_MAC_CHECKOUT), "source": ""}])):
        row = cast(dict[str, object], value)
        source = str(row["source"])
        pulls.append(MacCheckoutPull(PurePosixPath(str(row["mac"])), Path(source) if source else MacPullSourceUnrecorded()))
    return PendingMacPull(cast(str, fields["zone"]), cast(str, fields["text"]),
                          cast(str, fields["since"]), cast(str, fields["reason"]), tuple(pulls))


def queue(change: PendingMacPull) -> None:
    """Coalesce pulls while retaining an earlier zone notice that still needs delivery."""
    previous = read_queue()
    if isinstance(previous, PendingMacPull):
        pulls = {pull.mac: pull for pull in previous.pulls}
        pulls.update((pull.mac, pull) for pull in change.pulls)
        change = replace(change, pulls=tuple(pulls.values()))
        if not change.text and previous.text:
            change = replace(change, label=previous.label, text=previous.text, since=previous.since)
    _write_queue(change)


def _write_queue(change: PendingMacPull) -> None:
    path = queue_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(path, json.dumps(dict(zone=change.label, text=change.text,
                                           since=change.since, reason=change.reason,
                                           pulls=[dict(mac=str(pull.mac), source=str(pull.source) if isinstance(pull.source, Path) else "")
                                                  for pull in change.pulls]), indent=2) + "\n")


def queue_pull_only(label: str, reason: str, *, pulls: tuple[MacCheckoutPull, ...] = _LEGACY_MAC_PULL) -> None:
    """Queue a pull and retain any waiting zone notice."""
    queue(PendingMacPull(label, "", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), reason, pulls))


def clear_pull_only(mac: PurePosixPath = DEFAULT_MAC_CHECKOUT, *, notice_delivered: bool = False) -> None:
    """Clear a completed pull and retain any waiting zone notice."""
    pending = read_queue()
    if isinstance(pending, PendingMacPull):
        pending = replace(pending, pulls=tuple(pull for pull in pending.pulls if pull.mac != mac),
                          text="" if notice_delivered else pending.text)
        if pending.pulls or pending.text:
            _write_queue(pending)
        else:
            queue_path().unlink(missing_ok=True)
