"""Sync the Mac vault through natedev and file one question for each outage."""

from __future__ import annotations

import json
import math
import re
import shlex
import subprocess
import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, cast, get_args

from scripts.buildlog import ci
from scripts.buildlog.health import Machine
from scripts.lib.atomic_file import write_text_atomic
from scripts.lib import file_lock
from scripts.lib.process_failure import failure_detail
from scripts.buildlog import store
from scripts.buildlog import sync
from scripts.lib import conf

STATE_NAME = "vault_pull.json"
LOCAL_VAULT = Path("/home/natepiano/rust/hanadocs")
REGISTRATION = Path.home() / ".claude/scripts/production/update_registration.py"
UNREACHABLE_GRACE_S = 24 * 60 * 60
COMMAND_TIMEOUT_S = 120
ORIGIN_MAIN = "refs/remotes/origin/main"
MAC_MAIN = "refs/remotes/mac/main"


@dataclass(frozen=True)
class Pulled:
    commits: int


@dataclass(frozen=True)
class Current:
    pass


@dataclass(frozen=True)
class MacMerged:
    commits: int


@dataclass(frozen=True)
class DeferredSync:
    reason: str


@dataclass(frozen=True)
class VaultConflict:
    files: tuple[str, ...]


@dataclass(frozen=True)
class NotOnMain:
    pass


@dataclass(frozen=True)
class MacUnreachable:
    pass


@dataclass(frozen=True)
class VaultLocked:
    detail: str


@dataclass(frozen=True)
class VaultCommandFailed:
    detail: str
    machine: Machine = Machine.NATEDEV


PullStop = VaultConflict | NotOnMain | MacUnreachable | VaultLocked | VaultCommandFailed
PullOutcome = Pulled | Current | DeferredSync | PullStop
StopCause = Literal["conflict", "not_on_main", "mac_unreachable", "vault_locked", "command_failed"]
STOP_CAUSES = frozenset(cast(tuple[StopCause, ...], get_args(StopCause)))


@dataclass(frozen=True)
class LastGoodPull:
    at: float


@dataclass(frozen=True)
class NeverPulled:
    pass


@dataclass(frozen=True)
class OpenOutage:
    cause: StopCause
    since: float
    asked: bool
    detail: str


@dataclass(frozen=True)
class NoOutage:
    pass


@dataclass(frozen=True)
class VaultState:
    last_good: LastGoodPull | NeverPulled
    outage: OpenOutage | NoOutage


# Mac login can hide the exit status. The printed marker gives the outcome.
REMOTE_PULL = """export LC_ALL=C
outcome() { printf 'VAULT_OUTCOME=%s\\n' "$1"; exit 0; }
g() { git -C rust/hanadocs "$@"; }
retry_git() {
  retries_left=${vault_lock_retries:-0}
  while :; do
    output=$(g "$@" 2>&1)
    result=$?
    [ "$result" != 0 ] || return 0
    case "$output" in
      *"index.lock"*"File exists"*|*"File exists"*"index.lock"*)
        if [ "$retries_left" -le 0 ]; then
          printf '%s\\n' "$output" >&2
          outcome locked
        fi
        retries_left=$((retries_left - 1))
        sleep "$vault_lock_retry_delay_s" || outcome failed
        ;;
      *) printf '%s\\n' "$output" >&2; return "$result" ;;
    esac
  done
}
branch=$(g symbolic-ref --quiet --short HEAD) || outcome branch
[ "$branch" = main ] || outcome branch
if g rev-parse -q --verify MERGE_HEAD >/dev/null; then
  files=$(g diff --name-only --diff-filter=U) || outcome failed
  if [ -z "$files" ]; then
    files=$(g diff --name-only HEAD) || outcome failed
  fi
  printf '%s\\n' "$files" | sed 's/^/VAULT_CONFLICT=/'
  outcome conflict
fi
retry_git add -A || outcome failed
g diff --cached --quiet
case $? in
  0) ;;
  1) retry_git commit -m 'vault: Mac edits' || outcome failed ;;
  *) outcome failed ;;
esac
count=$(g rev-list --count HEAD..refs/remotes/origin/main) || outcome failed
if ! retry_git merge --no-edit --no-overwrite-ignore refs/remotes/origin/main; then
  files=$(g diff --name-only --diff-filter=U) || outcome failed
  [ -n "$files" ] || outcome failed
  retry_git merge --abort || outcome failed
  printf '%s\\n' "$files" | sed 's/^/VAULT_CONFLICT=/'
  outcome conflict
fi
commit=$(g rev-parse HEAD) || outcome failed
outcome "merged:$count:$commit"
"""


def run(args: list[str], timeout: float) -> subprocess.CompletedProcess[str] | None:
    """Use the shared transport boundary for every external command."""
    return sync.run(args, timeout)


def git(*args: str) -> subprocess.CompletedProcess[str] | VaultCommandFailed:
    result = run(["git", "-C", str(LOCAL_VAULT), *args], COMMAND_TIMEOUT_S)
    if result is None:
        return VaultCommandFailed("natedev git command did not complete")
    return result


def command_failure(result: subprocess.CompletedProcess[str], machine: Machine = Machine.NATEDEV) -> VaultCommandFailed:
    return VaultCommandFailed(failure_detail(result), machine)


def git_ok(*args: str) -> subprocess.CompletedProcess[str] | VaultCommandFailed:
    result = git(*args)
    if isinstance(result, VaultCommandFailed):
        return result
    return command_failure(result) if result.returncode != 0 else result


def mac_git(*args: str) -> subprocess.CompletedProcess[str] | VaultCommandFailed | MacUnreachable:
    transport = ["-c", "core.sshCommand=" + shlex.join(["ssh", *sync.ssh_options(sync.peer_name())])]
    result = git(*transport, *args)
    if isinstance(result, VaultCommandFailed):
        return MacUnreachable()
    if result.returncode == 0:
        return result
    if result.returncode == 255 or any(text in result.stderr.lower() for text in (
        "connection timed out", "connection refused", "could not resolve hostname",
        "no route to host", "network is unreachable", "connection closed", "permission denied (publickey",
    )):
        return MacUnreachable()
    return command_failure(result, Machine.MAC)


def marked(stdout: str, prefix: str) -> tuple[str, ...]:
    return tuple(line.removeprefix(prefix) for line in stdout.splitlines() if line.startswith(prefix))


def remote_outcome(result: subprocess.CompletedProcess[str]) -> MacMerged | PullStop:
    markers = marked(result.stdout, "VAULT_OUTCOME=")
    if len(markers) == 1:
        value = markers[0]
        if value == "branch":
            return NotOnMain()
        if value == "locked":
            return VaultLocked(result.stderr.strip())
        if value == "conflict":
            return VaultConflict(marked(result.stdout, "VAULT_CONFLICT="))
        merged = re.fullmatch(r"merged:([0-9]+):([0-9a-f]{40}|[0-9a-f]{64})", value)
        if merged:
            return MacMerged(int(merged[1]))
    elif not markers and result.returncode == 255:
        return MacUnreachable()
    return command_failure(result, Machine.MAC)


def publish_mac(merged: MacMerged) -> PullOutcome:
    fetched = mac_git("fetch", f"{sync.peer_name()}:rust/hanadocs", f"refs/heads/main:{MAC_MAIN}")
    if isinstance(fetched, (VaultCommandFailed, MacUnreachable)):
        return fetched
    if not ci.warm():
        return DeferredSync("GitHub credentials are cold; the next hour retries")
    fetched = git_ok("fetch", "origin", "main")
    if isinstance(fetched, VaultCommandFailed):
        return fetched
    contained = git("merge-base", "--is-ancestor", MAC_MAIN, ORIGIN_MAIN)
    if isinstance(contained, VaultCommandFailed):
        return contained
    if contained.returncode == 0:
        return Pulled(merged.commits) if merged.commits else Current()
    if contained.returncode != 1:
        return command_failure(contained)
    forward = git("merge-base", "--is-ancestor", ORIGIN_MAIN, MAC_MAIN)
    if isinstance(forward, VaultCommandFailed):
        return forward
    target = MAC_MAIN
    if forward.returncode == 1:
        tree = git("merge-tree", "--write-tree", "--name-only", "-z", ORIGIN_MAIN, target)
        if isinstance(tree, VaultCommandFailed):
            return tree
        if tree.returncode == 1:
            return DeferredSync("GitHub main conflicts with the Mac; the next hour merges it on the Mac")
        if tree.returncode != 0:
            return command_failure(tree)
        commit = git_ok("commit-tree", tree.stdout.split("\0", 1)[0].strip(),
                        "-p", ORIGIN_MAIN, "-p", target, "-m", "vault: merge Mac edits")
        if isinstance(commit, VaultCommandFailed):
            return commit
        target = commit.stdout.strip()
    elif forward.returncode != 0:
        return command_failure(forward)
    pushed = git("push", "--porcelain", "origin", f"{target}:refs/heads/main")
    if isinstance(pushed, VaultCommandFailed):
        return pushed
    if pushed.returncode != 0:
        if any(line.startswith("!") and "[rejected]" in line for line in pushed.stdout.splitlines()):
            return DeferredSync("GitHub main changed; the next hour retries")
        return command_failure(pushed)
    return Pulled(merged.commits)


def pull() -> PullOutcome:
    peer = sync.peer_name()
    pushed = mac_git("push", f"{peer}:rust/hanadocs", f"{ORIGIN_MAIN}:{ORIGIN_MAIN}")
    if isinstance(pushed, (VaultCommandFailed, MacUnreachable)):
        return pushed
    settings = conf.read("build_targets")
    remote = (f"vault_lock_retries={settings.whole('vault.lock_retries')}\n"
              + f"vault_lock_retry_delay_s={settings.number('vault.lock_retry_delay_s')}\n" + REMOTE_PULL)
    result = run(["ssh", *sync.ssh_options(peer), peer, remote], COMMAND_TIMEOUT_S)
    if result is None:
        return MacUnreachable()
    outcome = remote_outcome(result)
    return publish_mac(outcome) if isinstance(outcome, MacMerged) else outcome


def state_path() -> Path:
    return store.root() / STATE_NAME


def timestamp(value: object) -> float | NeverPulled:
    if isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0:
        return float(value)
    return NeverPulled()


def read_state() -> VaultState:
    try:
        value = cast(object, json.loads(state_path().read_text()))
    except (OSError, ValueError):
        return VaultState(NeverPulled(), NoOutage())
    if not isinstance(value, dict):
        return VaultState(NeverPulled(), NoOutage())
    fields = cast(dict[str, object], value)
    good = timestamp(fields.get("last_good"))
    last_good = good if isinstance(good, NeverPulled) else LastGoodPull(good)
    raw_outage = fields.get("outage")
    if isinstance(raw_outage, dict):
        outage = cast(dict[str, object], raw_outage)
        since = timestamp(outage.get("since"))
        if isinstance(since, float):
            cause = outage.get("cause")
            detail = outage.get("detail", "")
            return VaultState(last_good, OpenOutage(
                cause if isinstance(cause, str) and cause in STOP_CAUSES else "command_failed",
                since, outage.get("asked") is True, detail if isinstance(detail, str) else ""))
    return VaultState(last_good, NoOutage())


def write_state(state: VaultState) -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    outage = state.outage
    data = {
        "last_good": state.last_good.at if isinstance(state.last_good, LastGoodPull) else None,
        "outage": {"cause": outage.cause, "since": outage.since, "asked": outage.asked, "detail": outage.detail}
        if isinstance(outage, OpenOutage) else None,
    }
    write_text_atomic(path, json.dumps(data) + "\n")


def stop_details(outcome: PullStop) -> tuple[StopCause, str, str]:
    match outcome:
        case VaultConflict(files):
            return ("conflict", f"The hanadocs merge has a conflict on Mac. Files: {', '.join(files)}.",
                    "In a terminal on the Mac, resolve the conflicting edits in ~/rust/hanadocs. "
                    + "Both copies changed the same content. The commits remain available. "
                    + "1. Combine the edits and commit the result. This keeps the wanted content from both copies. "
                    + "2. Choose one copy for each file and commit the result. This replaces the other copy's content. "
                    + "I recommend choice 1. The next hourly sync retries the merge.")
        case NotOnMain():
            return ("not_on_main", "The hanadocs vault on the Mac is not on main.",
                    "In a terminal on the Mac, select a branch for ~/rust/hanadocs. "
                    + "1. Switch to main. The next hourly sync merges the vault. "
                    + "2. Keep the current branch. The hourly sync remains stopped. I recommend choice 1.")
        case MacUnreachable():
            return ("mac_unreachable", "natedev cannot reach the Mac for the hanadocs sync for at least 24 hours.",
                    "In a terminal on the Mac, restore its network connection. "
                    + "1. Wake the Mac and connect it to the network. The next hourly sync retries. "
                    + "2. Keep the Mac offline. The Mac vault remains unchanged. I recommend choice 1.")
        case VaultLocked(detail):
            return ("vault_locked", f"A Git index lock stops the hanadocs sync on the Mac. Detail: {detail}",
                    "In a terminal on the Mac, examine ~/rust/hanadocs/.git/index.lock. "
                    + "1. Let an active Git process finish. Remove the lock if no Git process uses it. The next hourly sync retries. "
                    + "2. Keep the lock. The sync retries each hour. I recommend choice 1.")
        case VaultCommandFailed(detail, machine):
            return ("command_failed", f"The hanadocs sync has an unknown command error on {machine}. Detail: {detail}",
                    "In the natedev session on natedev, examine the buildlog.service log for the failing command and machine. "
                    + "1. Repair the command error. The next hourly sync retries. "
                    + "2. Leave the error unresolved. The vault sync remains stopped. I recommend choice 1.")


def question_text(since: float) -> str:
    opened = datetime.fromtimestamp(since, UTC)
    return f"Hanadocs vault sync stopped, {opened.isoformat()}"


def registration(command: str, *args: str) -> bool:
    production = conf.read("build_targets").text("vault.question_production")
    result = run([
        "python3", str(REGISTRATION), command, "--production", production, *args,
    ], COMMAND_TIMEOUT_S)
    return result is not None and result.returncode == 0


def ask_question(reason: str, choices: str, since: float) -> bool:
    opened = datetime.fromtimestamp(since, UTC)
    return registration(
        "ask",
        "--text", question_text(since),
        "--question", reason + " " + choices, "--urgency", "soon",
        "--due", (opened + timedelta(days=1)).date().isoformat(),
    )


def withdraw_question(since: float) -> bool:
    return registration("withdraw", "--item", question_text(since))


def vault() -> int:
    with sync.sync_lock():
        paused = sync.read_pause()
        if isinstance(paused, sync.SyncPaused):
            print(f"buildlog vault: paused ({paused.why}); buildlog sync resume ends it")
            return 0
        return _vault_locked()


def _vault_locked() -> int:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock.exclusive(path.with_suffix(".lock")):
        state = read_state()
        outcome = pull()
        now = time.time()
        if isinstance(outcome, DeferredSync):
            write_state(replace(state, last_good=LastGoodPull(now)))
            print("buildlog vault: " + outcome.reason)
            return 0
        if isinstance(outcome, (Pulled, Current)):
            write_state(VaultState(LastGoodPull(now), NoOutage()))
            description = f"buildlog vault: synced {outcome.commits} incoming commits" if isinstance(outcome, Pulled) else "buildlog vault: current"
            if isinstance(state.outage, OpenOutage) and state.outage.asked and not withdraw_question(state.outage.since):
                description += "; the question was not withdrawn"
            print(description)
            return 0
        cause, reason, choices = stop_details(outcome)
        previous = state.outage
        outage = replace(previous, cause=cause, detail=reason) if isinstance(previous, OpenOutage) else OpenOutage(cause, now, False, reason)
        last_good = state.last_good.at if isinstance(state.last_good, LastGoodPull) else outage.since
        sleeping = isinstance(outcome, MacUnreachable) and now - last_good < UNREACHABLE_GRACE_S
        if isinstance(previous, OpenOutage) and not outage.asked and not sleeping:
            outage = replace(outage, asked=True)
            write_state(replace(state, outage=outage))
            if not ask_question(reason, choices, outage.since):
                outage = replace(outage, asked=False)
                write_state(replace(state, outage=outage))
        else:
            write_state(replace(state, outage=outage))
        description = "Mac unreachable; the next hour retries" if sleeping else " ".join(reason.splitlines())
        print("buildlog vault: stopped: " + description)
        return 0 if sleeping or (not isinstance(outcome, VaultCommandFailed)
                                 and (outage.asked or isinstance(previous, NoOutage))) else 1
