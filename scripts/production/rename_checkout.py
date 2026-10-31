"""Move a stopped showrunner's checkout, a linked git worktree, to a new path (`rename.py checkout`).

The shutdown launch calls `move` between the showrunner's stop and its launch (restart.move_checkout).
The git move comes first, so a refusal before it keeps the old checkout whole, and every later part is
safe to repeat:

- git: `git worktree move`, then `git worktree repair`.
- Claude: the project folder moves (unit_move.move_configs) and each account's folder trust is copied.
- timers: each update timer whose CHECK names a doc under the old path names it under the new one.
- children: a child doc's Parent production line naming a doc under the old path.
- bevy upgrade watch: a job's RUN line naming a doc under the old path.
- herdr pane records: a pane cwd under the old path.
- the production doc: its Showrunner checkout line names the new path, committed on the merge branch.

`checkout` is the verb: it stops the showrunner through the shutdown path with the move on its
record, and a rerun after the restart pushes the doc commit.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import cast

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

sys.path.insert(0, str(Path(__file__).resolve().parent))
from production_units import Production, read_production  # noqa: E402
import production_units  # noqa: E402
from scripts.production import showrunners  # noqa: E402
import unit_move  # noqa: E402
from rename_state import RenameRefused, write_text  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "shutdown"))
import shutdown_record  # noqa: E402
from shutdown_record import SessionShutdown  # noqa: E402

PARENT_LINE = "- **Parent production:** "
CHECKOUT_LINE = "- **Showrunner checkout:** "


def _under(path: str, old: Path, new: Path) -> str:
    """`path` with the leading folder `old` replaced by `new`; any other path as it is."""
    text = str(old)
    return str(new) + path[len(text):] if path == text or path.startswith(text + "/") else path


def _git(where: Path, *arguments: str) -> str:
    result = subprocess.run(["git", "-C", str(where), *arguments], capture_output=True, text=True, check=False)
    if result.returncode:
        raise RenameRefused(f"git {arguments[0]}: {(result.stderr or result.stdout).strip()}")
    return result.stdout.strip()


def _move_git(old: Path, new: Path) -> None:
    if not old.exists():
        if (new / ".git").exists():
            return
        raise RenameRefused(f"checkout {old} does not exist")
    if unit_move.exists(new):
        raise RenameRefused(f"{new} already exists")
    if (old / ".git").is_dir():
        raise RenameRefused(f"{old} is a main worktree; git moves only a linked worktree")
    if running := unit_move.processes_inside(old):
        raise RenameRefused(f"processes still work in {old}: {', '.join(map(str, running))}")
    _ = _git(old, "worktree", "move", str(old), str(new))
    _ = _git(new, "worktree", "repair")


def _trust_file(directory: Path) -> Path:
    return Path.home() / ".claude.json" if directory == Path.home() / ".claude" else directory / ".claude.json"


def _move_claude(old: Path, new: Path) -> None:
    unit_move.move_configs(str(old), str(new))
    for directory in unit_move.config_dirs():
        trust = _trust_file(directory)
        if trust.exists():
            unit_move.copy_trust(trust, str(old), str(new))


def _rewrite_lines(path: Path, prefix: str, old: Path, new: Path) -> None:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if line.startswith(prefix):
            lines[index] = line.replace(shlex.quote(str(old)) + "/", shlex.quote(str(new)) + "/").replace(
                str(old) + "/", str(new) + "/")
    if (updated := "".join(lines)) != text:
        write_text(path, updated)


def _rewrite_timers(old: Path, new: Path) -> None:
    for instance in showrunners.NOTIFIER_STATE_DIR.glob("*/conf"):
        _rewrite_lines(instance, "CHECK=", old, new)
        _rewrite_lines(instance, "RUN=", old, new)


def _rewrite_children(old: Path, new: Path) -> None:
    for doc in showrunners.timer_docs().values():
        if doc.exists() and not str(doc).startswith(str(old) + "/"):
            _rewrite_lines(doc, PARENT_LINE, old, new)


def _rewrite_panes(old: Path, new: Path) -> None:
    state = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
    for path in (state / "herdr-panes").glob("*.json"):
        try:
            panes = cast(object, json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
        updated = _moved_cwds(panes, old, new)
        if updated != panes:
            write_text(path, json.dumps(updated, indent=2) + "\n")


def _moved_cwds(value: object, old: Path, new: Path) -> object:
    if isinstance(value, dict):
        return {key: (_under(item, old, new) if key == "cwd" and isinstance(item, str) else _moved_cwds(item, old, new))
                for key, item in cast(dict[str, object], value).items()}
    if isinstance(value, list):
        return [_moved_cwds(item, old, new) for item in cast(list[object], value)]
    return value


def _message(doc: Path, checkout: Path) -> str:
    return f"chore({doc.name.removesuffix('-production.md')}): move showrunner checkout to {checkout}"


def _with_checkout(text: str, checkout: Path) -> str:
    """`text` with its Showrunner checkout line naming `checkout`, in the same `~` or absolute form."""
    lines = text.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if line.startswith(CHECKOUT_LINE):
            raw = production_units.cell_value(line[len(CHECKOUT_LINE):])
            home = Path.home()
            named = (f"~/{checkout.relative_to(home)}" if raw.startswith("~") and checkout.is_relative_to(home)
                     else str(checkout))
            lines[index] = line.replace(raw, named, 1)
            break
    return "".join(lines)


def _commit_checkout_line(doc: Path, checkout: Path) -> None:
    """Commit `doc`'s Showrunner checkout line naming `checkout`. A doc with other uncommitted changes refuses."""
    relative = str(doc.relative_to(checkout))
    committed = _git(checkout, "show", f"HEAD:{relative}") + "\n"
    wanted = _with_checkout(committed, checkout)
    if wanted == committed:
        return
    if doc.read_text(encoding="utf-8") not in (committed, wanted):
        raise RenameRefused(f"{doc} has other uncommitted changes; its Showrunner checkout line is not committed")
    write_text(doc, wanted)
    _ = _git(checkout, "commit", "-m", _message(doc, checkout), "--", relative)


def _commit_docs(new: Path) -> None:
    for doc in showrunners.timer_docs().values():
        if doc.exists() and doc.is_relative_to(new):
            _commit_checkout_line(doc, new)


def move(old: Path, new: Path) -> None:
    """Move the checkout `old` to `new` and every place that names a path inside it. Repeats safely."""
    _move_git(old, new)
    _move_claude(old, new)
    _rewrite_timers(old, new)
    _rewrite_children(old, new)
    _rewrite_panes(old, new)
    _commit_docs(new)


def _renamed(checkout: Path, doc: Path, slug: str) -> Path:
    """`checkout` with a former slug in its name replaced by `slug`; `checkout` when no former slug is in it."""
    lines = doc.read_text(encoding="utf-8").splitlines()
    formerly = next((line for line in lines if line.startswith("- **Formerly:**")), "")
    for former in cast(list[str], re.findall(r"`([^`]+)`", formerly)):
        if f"-{former}-" in checkout.name:
            return checkout.with_name(checkout.name.replace(f"-{former}-", f"-{slug}-", 1))
    return checkout


def _showrunner_session(instance: Path) -> str:
    for line in (instance / "conf").read_text(encoding="utf-8").splitlines():
        if line.startswith("TARGET=session:") and line[15:]:
            return line[15:]
    raise RenameRefused(f"{instance.name} has no session TARGET")


def _session_records(session_id: str) -> list[SessionShutdown]:
    try:
        return [saved for saved in shutdown_record.all_session_records() if saved["session"]["session_id"] == session_id]
    except (shutdown_record.InvalidRecord, OSError) as error:
        raise RenameRefused(f"shutdown records unreadable: {error}") from error


def _push(production: Production) -> None:
    _commit_checkout_line(production.doc, production.checkout)
    branch = production.merge_branch
    subject = _git(production.checkout, "log", "-1", "--format=%s")
    ahead = _git(production.checkout, "rev-list", f"origin/{branch}..HEAD")
    if ahead and subject == _message(production.doc, production.checkout):
        _ = _git(production.checkout, "push", "origin", f"HEAD:refs/heads/{branch}")
        print(f"pushed {branch}: {subject}")


def checkout(slug: str, destination: str = "") -> None:
    """Move the showrunner's checkout to `destination`, by default its name with the production's new slug.

    The first run stops the showrunner with the move on its shutdown record and returns; the conductor
    moves the checkout and launches the showrunner in it. A run after the restart pushes the doc commit.
    """
    slug = showrunners.canonical_slug(slug)
    instance = showrunners.NOTIFIER_STATE_DIR / f"showrunner-{slug}"
    checked = showrunners.checked_doc(instance)
    if isinstance(checked, showrunners.NoCheckedDoc):
        raise RenameRefused(f"{slug} has no readable update timer: {checked.reason}")
    production = read_production(checked.path)
    old = production.checkout
    new = Path(destination).expanduser().resolve() if destination else _renamed(old, production.doc, slug)
    if new == old:
        _push(production)
        print(f"{slug}: the showrunner checkout is {old}")
        return
    if unit_move.exists(new):
        raise RenameRefused(f"{new} already exists")
    if not (old / ".git").is_file():
        raise RenameRefused(f"{old} is not a linked worktree; git moves only a linked worktree")
    session_id = _showrunner_session(instance)
    if _session_records(session_id):
        raise RenameRefused(f"{slug}'s showrunner already has a shutdown in progress; let it settle first")
    import rename_unit  # the shutdown conductor imports this module; only the verb needs the shutdown runner
    rename_unit.run_shutdown("down", "--only", session_id, "--restart", "--checkout-move", str(old), str(new))
    if not any("checkout_move" in saved for saved in _session_records(session_id)):
        raise RenameRefused(f"{slug}'s shutdown record does not carry the checkout move")
    print(f"{slug}: the showrunner stops, its checkout moves to {new}, and it restarts there; "
          + f"then run rename.py checkout {slug} again to push the doc commit")
