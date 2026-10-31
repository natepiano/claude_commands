"""Fast-forward the Mac's vault through natedev; alert once until it works again."""

from __future__ import annotations

import fcntl
import json
import math
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

import rust_release
import store
import sync

if TYPE_CHECKING:
    from ..notify import push_texts
else:
    import push_texts

STATE_NAME = "vault_pull.json"
PUSH_TEXT_ID = "vault.stopped"
LOCAL_VAULT = Path("/home/natepiano/rust/hanadocs")
UNREACHABLE_GRACE_S = 24 * 60 * 60
COMMAND_TIMEOUT_S = 120


@dataclass(frozen=True)
class Pulled:
    commits: int


@dataclass(frozen=True)
class Current:
    pass


@dataclass(frozen=True)
class TrackedFilesChanged:
    pass


@dataclass(frozen=True)
class NotOnMain:
    pass


@dataclass(frozen=True)
class HistoryDiverged:
    pass


@dataclass(frozen=True)
class UntrackedWouldBeOverwritten:
    pass


@dataclass(frozen=True)
class MacUnreachable:
    pass


@dataclass(frozen=True)
class VaultCommandFailed:
    detail: str


PullStop = TrackedFilesChanged | NotOnMain | HistoryDiverged | UntrackedWouldBeOverwritten | MacUnreachable | VaultCommandFailed
PullOutcome = Pulled | Current | PullStop
StopCause = Literal["tracked_files_changed", "not_on_main", "history_diverged", "untracked_would_be_overwritten", "mac_unreachable", "command_failed"]


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
    alerted: bool


@dataclass(frozen=True)
class NoOutage:
    pass


@dataclass(frozen=True)
class VaultState:
    last_good: LastGoodPull | NeverPulled
    outage: OpenOutage | NoOutage


# Mac login can hide the command's exit status; only the printed marker is its outcome.
# No command changes branch, index, or worktree except the final fast-forward.
REMOTE_PULL = """export LC_ALL=C
outcome() { printf 'VAULT_OUTCOME=%s\\n' "$1"; exit 0; }
branch=$(git -C rust/hanadocs symbolic-ref --quiet --short HEAD) || outcome branch
[ "$branch" = main ] || outcome branch
changed=$(git -C rust/hanadocs status --porcelain --untracked-files=no) || outcome failed
[ -z "$changed" ] || outcome tracked
git -C rust/hanadocs merge-base --is-ancestor HEAD refs/remotes/origin/main
ancestor=$?
[ "$ancestor" = 0 ] || { [ "$ancestor" = 1 ] && outcome diverged; outcome failed; }
count=$(git -C rust/hanadocs rev-list --count HEAD..refs/remotes/origin/main) || outcome failed
merged=$(git -C rust/hanadocs merge --ff-only --no-overwrite-ignore refs/remotes/origin/main 2>&1)
result=$?
if [ "$result" != 0 ]; then
  printf '%s\\n' "$merged" >&2
  case "$merged" in
    *"untracked working tree files would be overwritten"*) outcome untracked ;;
    *"Not possible to fast-forward"*|*"not possible to fast-forward"*) outcome diverged ;;
    *) outcome failed ;;
  esac
fi
outcome "pulled:$count"
"""


def run(args: list[str], timeout: float) -> subprocess.CompletedProcess[str] | None:
    """One replaceable transport boundary; sync owns process execution and SSH options."""
    return sync.run(args, timeout)


def pull() -> PullOutcome:
    peer = sync.peer_name()
    pushed = run([
        "git", "-C", str(LOCAL_VAULT), "-c", "core.sshCommand=" + shlex.join(["ssh", *sync.SSH_OPTIONS]),
        "push", f"{peer}:rust/hanadocs", "refs/remotes/origin/main:refs/remotes/origin/main",
    ], COMMAND_TIMEOUT_S)
    if pushed is None:
        return MacUnreachable()
    if pushed.returncode != 0:
        detail = pushed.stderr.lower()
        if any(text in detail for text in ("[rejected]", "non-fast-forward", "fetch first")):
            return HistoryDiverged()
        if pushed.returncode == 255 or any(text in detail for text in (
            "connection timed out", "connection refused", "could not resolve hostname",
            "no route to host", "network is unreachable", "connection closed", "permission denied (publickey",
        )):
            return MacUnreachable()
        return VaultCommandFailed(pushed.stderr.strip() or f"git push exited {pushed.returncode}")
    result = run(["ssh", *sync.SSH_OPTIONS, peer, REMOTE_PULL], COMMAND_TIMEOUT_S)
    if result is None:
        return MacUnreachable()
    markers = [line.removeprefix("VAULT_OUTCOME=") for line in result.stdout.splitlines()
               if line.startswith("VAULT_OUTCOME=")]
    if len(markers) == 1:
        match markers[0]:
            case "tracked":
                return TrackedFilesChanged()
            case "branch":
                return NotOnMain()
            case "diverged":
                return HistoryDiverged()
            case "untracked":
                return UntrackedWouldBeOverwritten()
            case value if value.startswith("pulled:"):
                count = value.removeprefix("pulled:")
                if count.isascii() and count.isdecimal():
                    commits = int(count)
                    return Pulled(commits) if commits else Current()
            case _:
                pass
    elif not markers and result.returncode == 255:
        return MacUnreachable()
    return VaultCommandFailed(result.stderr.strip() or "Mac pull result unavailable")


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
        cause, since, alerted = outage.get("cause"), timestamp(outage.get("since")), outage.get("alerted")
        if cause in ("tracked_files_changed", "not_on_main", "history_diverged", "untracked_would_be_overwritten", "mac_unreachable", "command_failed") and isinstance(since, float) and isinstance(alerted, bool):
            return VaultState(last_good, OpenOutage(cause, since, alerted))
    return VaultState(last_good, NoOutage())


def write_state(state: VaultState) -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    outage = state.outage
    data = {
        "last_good": state.last_good.at if isinstance(state.last_good, LastGoodPull) else None,
        "outage": {"cause": outage.cause, "since": outage.since, "alerted": outage.alerted} if isinstance(outage, OpenOutage) else None,
    }
    partial = path.with_name(path.name + ".tmp")
    _ = partial.write_text(json.dumps(data) + "\n")
    _ = partial.replace(path)


def stop_details(outcome: PullStop) -> tuple[StopCause, str, str]:
    match outcome:
        case TrackedFilesChanged():
            return "tracked_files_changed", "Some files in ~/rust/hanadocs on the Mac have changes that are not committed.", "In a terminal on the Mac, commit or discard the changes in ~/rust/hanadocs."
        case NotOnMain():
            return "not_on_main", "~/rust/hanadocs on the Mac is not on the main branch.", "In a terminal on the Mac, switch ~/rust/hanadocs to the main branch."
        case HistoryDiverged():
            return "history_diverged", "The Mac copy has commits that GitHub does not have, so a safe update is not possible.", "In a terminal on the Mac, push the commits in ~/rust/hanadocs to GitHub."
        case UntrackedWouldBeOverwritten():
            return "untracked_would_be_overwritten", "An update would replace a file on the Mac that git does not track.", "In a terminal on the Mac, move the untracked file that blocks the hanadocs update out of ~/rust/hanadocs. Run git -C ~/rust/hanadocs merge --ff-only origin/main to see its name."
        case MacUnreachable():
            return "mac_unreachable", "natedev could not reach the Mac for more than 24 hours.", "Wake the Mac and connect it to the network."
        case VaultCommandFailed():
            return "command_failed", "The update failed with an error the job does not know.", "In the Claude session named natedev, ask it to read the vault update error in the hourly log."


def vault() -> int:
    # Pause uses this same contact lock, so no Mac work outlives pause's return.
    with sync.sync_lock():
        paused = sync.read_pause()
        if isinstance(paused, sync.SyncPaused):
            print(f"buildlog vault: paused ({paused.why}); buildlog sync resume ends it")
            return 0
        return _vault_locked()


def _vault_locked() -> int:
    # The manual command and the hourly job share both the outage and its send.
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = read_state()
        outcome = pull()
        now = time.time()
        if isinstance(outcome, (Pulled, Current)):
            write_state(VaultState(LastGoodPull(now), NoOutage()))
            print(f"buildlog vault: pulled {outcome.commits} commits" if isinstance(outcome, Pulled) else "buildlog vault: current")
            return 0
        cause, reason, action = stop_details(outcome)
        previous = state.outage
        outage = OpenOutage(cause, previous.since, previous.alerted) if isinstance(previous, OpenOutage) else OpenOutage(cause, now, False)
        last_good = state.last_good.at if isinstance(state.last_good, LastGoodPull) else outage.since
        sleeping = isinstance(outcome, MacUnreachable) and now - last_good <= UNREACHABLE_GRACE_S
        # Save before sending: a send failure must retry without losing the outage's start.
        write_state(VaultState(state.last_good, outage))
        if not outage.alerted and not sleeping:
            # A note's send key has no permanent deduplication window. Commit the
            # alert before sending, so a later save failure cannot repeat it.
            write_state(VaultState(state.last_good, OpenOutage(cause, outage.since, True)))
            try:
                sent = rust_release.send_release_text(push_texts.render(PUSH_TEXT_ID, reason=reason, action=action))
            except (OSError, subprocess.SubprocessError):
                sent = False
            if not sent:
                write_state(VaultState(state.last_good, outage))
        description = outcome.detail if isinstance(outcome, VaultCommandFailed) else reason
        if sleeping:
            description = "Mac unreachable; the next hour retries"
        print("buildlog vault: stopped: " + description)
        return 0 if sleeping else 1
