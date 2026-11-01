#!/usr/bin/env python3
"""Run independent, bounded maintenance parts; never collect recovery snapshots."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, cast

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.lib import process_group
from scripts.maintenance.settings import Part, Schedule, settings


class Outcome(str, Enum):
    SUCCESS = "ok"
    FAILED = "failed"
    TIMED_OUT = "timed out"
    NOT_DUE = "not due"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class PartResult:
    part: Part
    outcome: Outcome
    seconds: float
    exit_code: int
    output: str = ""


def run_part(part: Part, cadence: Schedule, deadline: float) -> PartResult:
    started = time.monotonic()
    remaining = min(cadence.timeout_seconds, deadline - started)
    if remaining <= 0:
        return PartResult(part, Outcome.TIMED_OUT, 0, 124)
    command = [sys.executable, str(Path(__file__).resolve()), "--part", part.value]
    try:
        with subprocess.Popen(command, cwd=ROOT, start_new_session=True, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True) as child:
            try:
                output, _ = child.communicate(timeout=remaining)
            except subprocess.TimeoutExpired:
                try:
                    process_group.send_process_group(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                output, _ = child.communicate()
                return PartResult(part, Outcome.TIMED_OUT, time.monotonic() - started, 124, output)
            return PartResult(part, {0: Outcome.SUCCESS, os.EX_TEMPFAIL: Outcome.SKIPPED}.get(child.returncode, Outcome.FAILED),
                              time.monotonic() - started, child.returncode, output)
    except OSError as error:
        return PartResult(part, Outcome.FAILED, time.monotonic() - started, 1, str(error))


def run_job() -> list[PartResult]:
    config = settings()
    directory = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "agent-notes/job"
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "lock").open("a") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return [PartResult(part, Outcome.NOT_DUE, 0, 0, "another pass is running") for part in Part]
        path = directory / "completed.json"
        try:
            saved = cast(object, json.loads(path.read_text(encoding="utf-8")))
        except FileNotFoundError:
            saved = {}
        if not isinstance(saved, dict):
            raise ValueError(f"{path}: expected completion times")
        finished: dict[str, float] = {}
        for name, value in cast(dict[str, object], saved).items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                finished[name] = float(value)
        deadline = time.monotonic() + config.job.timeout_seconds
        due = {part for part in Part if time.time() - finished.get(part.value, float('-inf')) >= config.parts[part].interval_seconds}
        results: dict[Part, PartResult] = {}

        def record(result: PartResult) -> None:
            results[result.part] = result
            if result.outcome in (Outcome.SUCCESS, Outcome.SKIPPED):
                finished[result.part.value] = time.time()
                temporary = path.with_suffix(".tmp")
                _ = temporary.write_text(json.dumps(finished) + "\n", encoding="utf-8")
                os.replace(temporary, path)

        # Quota and the pacer read completed notes; the vault follows all writers.
        with ThreadPoolExecutor(max_workers=len(Part)) as pool:
            tasks = {part: pool.submit(run_part, part, config.parts[part], deadline)
                     for part in Part if part in due and part not in (Part.QUOTA, Part.PACER, Part.VAULT)}
            def after_notes(part: Part) -> PartResult:
                if Part.NOTES in tasks:
                    _ = tasks[Part.NOTES].result()
                return run_part(part, config.parts[part], deadline)
            for part in (Part.QUOTA, Part.PACER):
                if part in due:
                    tasks[part] = pool.submit(after_notes, part)
            for task in as_completed(tasks.values()):
                record(task.result())
        if Part.VAULT in due:
            record(run_part(Part.VAULT, config.parts[Part.VAULT], deadline))
        return [results.get(part, PartResult(part, Outcome.NOT_DUE, 0, 0)) for part in Part]


def perform(part: Part) -> int:
    from scripts.herdr import herdr
    from scripts.accounts import refusal_watch
    with refusal_watch.session_observation(), herdr.process_snapshot():
        return _perform(part)


def _perform(part: Part) -> int:
    # Match the sibling imports the existing command-line tools use.
    sys.path[:0] = [str(ROOT / "scripts/whoami"), str(ROOT / "scripts/production")]
    if part in (Part.NOTES, Part.QUOTA, Part.PACER):
        if TYPE_CHECKING:
            from scripts.whoami import agent_notes, codex_pacer
        else:
            import agent_notes
            import codex_pacer
        from scripts.whoami import note_file
        if part is Part.NOTES:
            for line in agent_notes.update(include_warnings=False):
                print(line)
        elif part is Part.QUOTA:
            from scripts.accounts import accounts
            if TYPE_CHECKING:
                from scripts.whoami import quota_alert
            else:
                import quota_alert
            from scripts.whoami import agent_accounts, usage_store
            # Quota uses the notes' saved usage, never a second live account probe.
            workers = agent_accounts.observe_workers_home()
            now = datetime.now(timezone.utc)
            selection = accounts.claude_account_selection()
            signature = agent_notes.timer_usage_signature(selection, workers)
            saved = usage_store.recent_timer_usage(signature, now)
            reports = saved.reports if isinstance(saved, usage_store.RecentTimerUsage) else []
            observed = saved.observed_at if isinstance(saved, usage_store.RecentTimerUsage) else now
            readings = quota_alert.codex_alert_pass(workers, reports, observed)
            for line in [*agent_notes.watch_usage(reports, selection, workers, readings),
                         *quota_alert.alert(list(note_file.read_notes()), selection=selection, codex_readings=readings)]:
                print(line)
        else:
            agent_notes.mark_tier(codex_pacer.tick(list(note_file.read_notes())))
    elif part is Part.REFUSALS:
        if TYPE_CHECKING:
            from scripts.whoami import agent_notes
        else:
            import agent_notes
        from scripts.accounts import accounts, refusal_watch
        from scripts.whoami import agent_accounts, note_file
        def refresh_refusal_usage() -> None:
            refreshed = agent_notes.refresh_usage(accounts.claude_account_selection(),
                agent_accounts.observe_workers_home(), agent_notes.claude_note_logins(note_file.read_notes()))
            for line in refreshed.messages:
                print(line)
        for line in refusal_watch.run(recover_units=False, refresh_usage=refresh_refusal_usage):
            print(line)
    elif part is Part.WATCHDOG:
        from scripts.accounts import unit_watchdog
        for line in unit_watchdog.run(datetime.now(timezone.utc), dry_run=False):
            print(line)
    elif part is Part.PRUNE:
        from scripts.production.prune_phase_notes import main
        return main()
    elif part is Part.HIERARCHY:
        from scripts.maintenance.hana_hierarchy import main
        return main([], refusal_exit=os.EX_TEMPFAIL)
    else:
        from scripts.maintenance import vault_commit
        vault = Path.home() / "rust/hanadocs"
        notes = vault / "agents"
        tracked = vault_commit.git(str(vault), "ls-files", "-z", "--", ":(glob)agents/*.md")
        paths = {str(path) for path in notes.glob("*.md")}
        paths.update(str(vault / path) for path in tracked.stdout.split("\0") if path)
        paths.add(str(vault / "hana hierarchy.md"))
        timestamps = (r"^date_modified:", r"^weekly_usage_checked_at:", r"^weekly_remaining_usage:",
                      r"^credit_balance:", r"^resets:", r"^limit_reset:", r"^Snapshot taken ")
        return vault_commit.main(["--job", "agent-notes", *[arg for pattern in timestamps for arg in ("--timestamp", pattern)], "--", *sorted(paths)])
    return 0


class Arguments(argparse.Namespace):
    part: str = ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--part", choices=[part.value for part in Part])
    args = parser.parse_args(namespace=Arguments())
    try:
        if args.part:
            return perform(Part(args.part))
        results = run_job()
        for result in results:
            print(f"{result.part.value}: {result.outcome.value}; {result.seconds:.3f} s; exit {result.exit_code}")
            if result.output.strip():
                print(result.output.rstrip())
        return int(any(result.outcome in (Outcome.FAILED, Outcome.TIMED_OUT) for result in results))
    except (OSError, ValueError) as error:
        print(f"agent-notes: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
