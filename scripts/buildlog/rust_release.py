"""Check stable Rust daily and trial a newer release without touching hana's checkout."""

from __future__ import annotations

import json
import os
import signal
import shutil
import socket
import stat
import subprocess
import tempfile
import tomllib
import urllib.request
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import BinaryIO, Literal, TypedDict, cast

import store

CHANNEL_URL = "https://static.rust-lang.org/dist/channel-rust-stable.toml"
HANA = Path.home() / "rust/hana"
HOLD_DIR = Path.home() / ".local/state/build-hold"
LINT_CONFIG = Path(__file__).resolve().parents[2] / "config/lint.conf"
TRIAL_HEADROOM_GIB = 50
GIB = 2**30


class TrialWaiting(TypedDict):
    status: Literal["waiting"]
    version: str
    reason: str


class TrialFinished(TypedDict):
    status: Literal["finished"]
    version: str
    warnings: int
    warning_crates: int
    errors: int
    error_crates: int
    mend_builds: bool
    target_gib: float
    mend_target_gib: float


class TrialFailed(TypedDict):
    status: Literal["failed"]
    version: str
    step: str
    reason: str
    target_gib: float
    mend_target_gib: float


TrialOutcome = TrialWaiting | TrialFinished | TrialFailed


class ReleaseState(TypedDict):
    check_day: str
    stable_version: str
    release_date: str
    pin: str | None
    trial: TrialOutcome
    text_sent: bool


RunCommand = Callable[[list[str], Path | None, dict[str, str], int], subprocess.CompletedProcess[str]]


def state_path() -> Path:
    return store.root() / "rust_release.json"


def read_state() -> ReleaseState | None:
    try:
        return cast(ReleaseState, json.loads(state_path().read_text()))
    except (OSError, ValueError):
        return None


def write_state(state: ReleaseState) -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".tmp")
    _ = partial.write_text(json.dumps(state, indent=2) + "\n")
    _ = partial.replace(path)


def fetch_channel_file() -> bytes:
    with cast(BinaryIO, urllib.request.urlopen(CHANNEL_URL, timeout=30)) as response:
        return response.read()


def pinned_version() -> str | None:
    result = subprocess.run(
        ["git", "-C", str(HANA), "show", "origin/init/catalyst:rust-toolchain.toml"],
        capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL,
    )
    if result.returncode != 0:
        return None
    toolchain = mapping(mapping(cast(object, tomllib.loads(result.stdout))).get("toolchain"))
    channel = toolchain.get("channel")
    if not isinstance(channel, str):
        return None
    try:
        _ = version_key(channel)
    except ValueError:
        return None
    return channel


def run_process(args: list[str], cwd: Path | None, env: dict[str, str], timeout: int) -> subprocess.CompletedProcess[str]:
    with subprocess.Popen(args, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, stdin=subprocess.DEVNULL, start_new_session=True) as process:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as error:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            stdout, stderr = process.communicate()
            raise subprocess.TimeoutExpired(args, timeout, output=stdout, stderr=stderr) from error
        return subprocess.CompletedProcess(args, process.returncode, stdout, stderr)


def free_space_gib() -> float:
    disk = os.statvfs(Path.home())
    return disk.f_bavail * disk.f_frsize / GIB


def build_hold_active(folder: Path = HOLD_DIR) -> bool:
    try:
        for entry in folder.iterdir():
            try:
                if stat.S_ISREG(entry.stat(follow_symlinks=False).st_mode):
                    return True
            except FileNotFoundError:
                continue
    except FileNotFoundError:
        pass
    return False


def cargo_building() -> bool:
    result = subprocess.run(
        ["pgrep", "-u", str(os.getuid()), "^(cargo|rustc|cargo-nextest)$"],
        capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL,
    )
    return bool(result.stdout.strip()) or result.returncode not in (0, 1)


def send_release_text(title: str, message: str) -> bool:
    sender = Path(__file__).resolve().parents[1] / "notify/pushover.py"
    result = subprocess.run(
        ["python3", str(sender), "--priority", "0", title, message],
        capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL,
    )
    return result.returncode == 0


def version_key(version: str) -> tuple[int, ...]:
    parts = version.split(".")
    if len(parts) not in (2, 3) or any(not part or not part.isascii() or not part.isdecimal() for part in parts):
        raise ValueError(f"invalid Rust toolchain channel: {version}")
    return tuple(int(part) for part in parts)


def newer_than_pin(stable: str, pin: str) -> bool:
    pinned = version_key(pin)
    return version_key(stable)[:len(pinned)] > pinned


def stable_release(channel: bytes) -> tuple[str, str]:
    data = mapping(cast(object, tomllib.loads(channel.decode())))
    package = mapping(mapping(data["pkg"])["rust"])
    return str(package["version"]).split()[0], str(data["date"])


def mapping(value: object) -> dict[str, object]:
    return cast(dict[str, object], value) if isinstance(value, dict) else {}


def free_floor_gib() -> float:
    values: dict[str, str] = {}
    try:
        for raw in LINT_CONFIG.read_text().splitlines():
            line = raw.split("#", 1)[0].strip()
            if line and not (line.startswith("[") and line.endswith("]")) and "=" in line:
                key, _, value = line.partition("=")
                _ = values.setdefault(key.strip(), value.strip())
    except OSError:
        return 0
    raw = values.get(f"sweep_free_floor_gib.{socket.gethostname()}", values.get("sweep_free_floor_gib", "0"))
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 0


def waiting_reason(held: Callable[[], bool], building: Callable[[], bool], free_gib: Callable[[], float]) -> str:
    if held():
        return "build hold active"
    if building():
        return "cargo build active"
    free = free_gib()
    needed = free_floor_gib() + TRIAL_HEADROOM_GIB
    if free < needed:
        return f"{free:.0f} GiB free, needs {needed:.0f}"
    return ""


def trial_environment() -> dict[str, str]:
    env = dict(os.environ)
    if not env.get("CARGO_MAKEFLAGS"):
        try:
            mode = os.stat("/dev/steve").st_mode
            if stat.S_ISCHR(mode) and os.access("/dev/steve", os.R_OK | os.W_OK):
                env["CARGO_MAKEFLAGS"] = "--jobserver-auth=fifo:/dev/steve"
        except OSError:
            pass
    return env


def first_error(result: subprocess.CompletedProcess[str]) -> str:
    for line in result.stderr.splitlines():
        if "error" in line.lower():
            return line.strip()
    for line in result.stdout.splitlines():
        try:
            entry = mapping(cast(object, json.loads(line)))
        except ValueError:
            entry = {}
        message = mapping(entry.get("message"))
        if entry.get("reason") == "compiler-message" and message.get("level") == "error":
            rendered = message.get("rendered", message.get("message", ""))
            if isinstance(rendered, str) and rendered.strip():
                return rendered.strip().splitlines()[0]
        if "error" in line.lower():
            return line.strip()
    return next((line.strip() for line in (result.stderr + "\n" + result.stdout).splitlines() if line.strip()), f"exit {result.returncode}")


def diagnostic_counts(output: str) -> tuple[int, int, int, int]:
    seen: set[tuple[str, str, str, str, str]] = set()
    crates: dict[str, set[str]] = {"warning": set(), "error": set()}
    totals = {"warning": 0, "error": 0}
    for line in output.splitlines():
        try:
            item = mapping(cast(object, json.loads(line)))
        except ValueError:
            continue
        if item.get("reason") != "compiler-message":
            continue
        message = mapping(item.get("message"))
        if message.get("level") not in totals:
            continue
        level = cast(str, message["level"])
        package = str(item.get("package_id", "unknown"))
        code = message.get("code")
        lint = str(mapping(code).get("code", ""))
        spans = message.get("spans")
        primary: dict[str, object] = {}
        if isinstance(spans, list):
            for span in cast(list[object], spans):
                candidate = mapping(span)
                if candidate.get("is_primary"):
                    primary = candidate
                    break
        key = (package, level, lint, str(primary.get("file_name", "")), f"{primary.get('line_start', '')}:{primary.get('column_start', '')}:{primary.get('line_end', '')}:{primary.get('column_end', '')}")
        if key in seen:
            continue
        seen.add(key)
        totals[level] += 1
        crates[level].add(package)
    return totals["warning"], len(crates["warning"]), totals["error"], len(crates["error"])


def directory_gib(path: Path) -> float:
    return sum(entry.stat().st_size for entry in path.rglob("*") if entry.is_file()) / GIB if path.exists() else 0.0


def remove_clone(path: Path) -> None:
    if path.is_symlink():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def default_trial_parent() -> Path:
    return Path(tempfile.gettempdir())


def trial_release(
    version: str, run_command: RunCommand, held: Callable[[], bool], trial_parent: Path,
) -> TrialOutcome:
    clone = trial_parent / f"rust-release-trial-{version}"
    env = trial_environment()
    warnings = warning_crates = errors = error_crates = 0
    step = "toolchain"
    reason = ""
    target_gib = mend_target_gib = 0.0
    mend_builds = False
    try:
        remove_clone(clone)
        steps: list[tuple[str, list[str], Path | None, dict[str, str], int]] = [
            ("toolchain", ["rustup", "toolchain", "install", version, "--profile", "minimal", "--component", "clippy", "--component", "rustc-dev"], None, env, 1800),
            ("clone", ["git", "clone", "--local", "--no-checkout", str(HANA), str(clone)], None, env, 600),
        ]
        for step, args, cwd, step_env, timeout in steps:
            if held():
                return {"status": "waiting", "version": version, "reason": "build hold active"}
            result = run_command(args, cwd, step_env, timeout)
            if held():
                return {"status": "waiting", "version": version, "reason": "build hold active"}
            if result.returncode != 0:
                reason = first_error(result)
                break
        else:
            if held():
                return {"status": "waiting", "version": version, "reason": "build hold active"}
            step = "revision"
            revision = run_command(["git", "-C", str(HANA), "rev-parse", "origin/init/catalyst"], None, env, 30)
            if held():
                return {"status": "waiting", "version": version, "reason": "build hold active"}
            if revision.returncode != 0:
                reason = first_error(revision)
            else:
                sha = revision.stdout.strip()
                checkout = run_command(["git", "-C", str(clone), "checkout", "--detach", sha], None, env, 600)
                if held():
                    return {"status": "waiting", "version": version, "reason": "build hold active"}
                if checkout.returncode != 0:
                    reason = first_error(checkout)
            if not reason:
                if held():
                    return {"status": "waiting", "version": version, "reason": "build hold active"}
                step = "clippy"
                clippy_env = env | {"CARGO_INCREMENTAL": "0", "CARGO_TARGET_X86_64_UNKNOWN_LINUX_GNU_RUSTFLAGS": "-C link-arg=-fuse-ld=mold"}
                clippy = run_command(["nix", "develop", ".#ci", "-c", "cargo", f"+{version}", "clippy", "--workspace", "--all-targets", "--all-features", "--message-format=json"], clone, clippy_env, 7200)
                if held():
                    return {"status": "waiting", "version": version, "reason": "build hold active"}
                warnings, warning_crates, errors, error_crates = diagnostic_counts(clippy.stdout)
                if clippy.returncode != 0 and errors == 0:
                    reason = first_error(clippy)
            if not reason:
                if held():
                    return {"status": "waiting", "version": version, "reason": "build hold active"}
                step = "cargo-mend"
                mend_env = env | {"RUSTC_BOOTSTRAP": "1", "CARGO_TARGET_DIR": str(clone / "mend-target")}
                mend = run_command(["cargo", f"+{version}", "install", "--path", str(Path.home() / "rust/cargo-liner/crates/cargo-mend"), "--root", str(clone / "mend-root")], clone, mend_env, 3600)
                if held():
                    return {"status": "waiting", "version": version, "reason": "build hold active"}
                mend_builds = mend.returncode == 0
        target_gib = directory_gib(clone / "target")
        mend_target_gib = directory_gib(clone / "mend-target")
        if reason:
            return {"status": "failed", "version": version, "step": step, "reason": reason, "target_gib": target_gib, "mend_target_gib": mend_target_gib}
        return {"status": "finished", "version": version, "warnings": warnings, "warning_crates": warning_crates, "errors": errors, "error_crates": error_crates, "mend_builds": mend_builds, "target_gib": target_gib, "mend_target_gib": mend_target_gib}
    except (OSError, subprocess.SubprocessError) as error:
        return {"status": "failed", "version": version, "step": step, "reason": str(error).splitlines()[0], "target_gib": directory_gib(clone / "target"), "mend_target_gib": directory_gib(clone / "mend-target")}
    finally:
        remove_clone(clone)


def count_phrase(number: int, crates: int, kind: str) -> str:
    return f"{number} {kind}{'' if number == 1 else 's'} in {crates} crate{'' if crates == 1 else 's'}"


def trial_text(trial: TrialOutcome) -> str:
    if trial["status"] == "waiting":
        return f"trial waiting: {trial['reason']}"
    if trial["status"] == "failed":
        return f"trial failed: {trial['step']}: {trial['reason']}"
    parts = [count_phrase(trial["warnings"], trial["warning_crates"], "new warning")]
    if trial["errors"]:
        parts.append(count_phrase(trial["errors"], trial["error_crates"], "error"))
    parts.append("cargo-mend builds" if trial["mend_builds"] else "cargo-mend does not build")
    return ", ".join(parts)


def report_line() -> list[str]:
    state = read_state()
    if state is None or state["pin"] is None or not newer_than_pin(state["stable_version"], state["pin"]):
        return []
    trial = state["trial"]
    if trial["status"] == "waiting":
        result = f"waiting, {trial['reason']}"
    elif trial["status"] == "failed":
        result = f"failed, {trial['step']}: {trial['reason']}"
    else:
        result = trial_text(trial)
    release_day = datetime.fromisoformat(state["release_date"]).strftime("%m-%d")
    return [f"Rust {state['stable_version']} out since {release_day}; hana on {state['pin']}. Trial: {result}."]


def check_release(
    *,
    fetch_channel: Callable[[], bytes] = fetch_channel_file,
    read_pin: Callable[[], str | None] = pinned_version,
    run_command: RunCommand = run_process,
    now: Callable[[], datetime] = datetime.now,
    free_gib: Callable[[], float] = free_space_gib,
    held: Callable[[], bool] = build_hold_active,
    building: Callable[[], bool] = cargo_building,
    send_text: Callable[[str, str], bool] = send_release_text,
    trial_parent: Callable[[], Path] = default_trial_parent,
) -> int:
    pin = read_pin()
    if pin is not None:
        try:
            _ = version_key(pin)
        except ValueError:
            pin = None
    state = read_state()
    if pin is None:
        if state is not None:
            state["pin"] = None
            write_state(state)
        return 0
    today = now()
    fetch_failed = False
    if state is None or state["check_day"] != today.date().isoformat():
        try:
            version, release_date = stable_release(fetch_channel())
        except (OSError, ValueError, KeyError, TypeError) as error:
            store.note_error(f"rust release channel: {error}")
            if state is None:
                return 1
            fetch_failed = True
        else:
            previous = state
            fresh: ReleaseState = {
                "check_day": today.date().isoformat(), "stable_version": version, "release_date": release_date,
                "pin": pin,
                "trial": previous["trial"] if previous and previous["trial"]["version"] == version else {"status": "waiting", "version": version, "reason": "awaiting quiet hours"},
                "text_sent": previous["text_sent"] if previous and previous["stable_version"] == version else False,
            }
            state = fresh
    state["pin"] = pin
    if not newer_than_pin(state["stable_version"], pin):
        write_state(state)
        return int(fetch_failed)
    if 2 <= today.hour < 5 and state["trial"]["status"] == "waiting":
        reason = waiting_reason(held, building, free_gib)
        state["trial"] = {"status": "waiting", "version": state["stable_version"], "reason": reason} if reason else trial_release(state["stable_version"], run_command, held, trial_parent())
    write_state(state)
    if 2 <= today.hour < 5 and not state["text_sent"]:
        message = f"Rust {state['stable_version']} out: {trial_text(state['trial'])}. Tell natedev bump or wait."
        try:
            sent = send_text(f"Rust {state['stable_version']} out", message)
        except (OSError, subprocess.SubprocessError) as error:
            store.note_error(f"rust release text: {error}")
            return 1
        if not sent:
            store.note_error("rust release text failed")
            return 1
        state["text_sent"] = True
        write_state(state)
    return int(fetch_failed)
