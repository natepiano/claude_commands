#!/usr/bin/env python3
"""Keep one reviewer per unit for a production and grow it only for concurrent reviews."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, NotRequired, TypedDict, cast

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.lib.process_failure import failure_detail
from scripts.production.showrunners import canonical_slug, timer_docs  # noqa: E402

ReviewerType = Literal["code", "ui"]


@dataclass(frozen=True)
class Unleased:
    """Available to take a new unit lease."""


@dataclass(frozen=True)
class UnitLease:
    """The unit this reviewer belongs to; `ordinal` counts the reviews it has carried."""
    unit: str
    ordinal: int


@dataclass(frozen=True)
class IdleReviewer:
    """No live process owns this reviewer."""


@dataclass(frozen=True)
class ClaimedReviewer:
    pid: int


class LeaseJson(TypedDict):
    unit: str
    ordinal: int


class TemplateJson(TypedDict):
    name: str
    thread_id: str
    prime_hash: str


class ReviewerJson(TypedDict):
    type: ReviewerType
    thread_id: str
    prime_hash: str
    lease: LeaseJson | None
    busy_pid: int | None
    reviews: int
    last_allocated: NotRequired[float]


class PoolJson(TypedDict):
    generation: int
    templates: dict[str, TemplateJson]
    reviewers: dict[str, ReviewerJson]
    stopping: NotRequired[bool]
    next_index: NotRequired[dict[ReviewerType, int]]


class AllocationJson(TypedDict):
    reviewer: str
    thread_id: str
    mesh_dir: str
    lease_ordinal: int
    prime_hash: str


@dataclass(frozen=True)
class Template:
    name: str
    thread_id: str
    prime_hash: str


@dataclass
class Reviewer:
    type: ReviewerType
    thread_id: str
    prime_hash: str
    lease: Unleased | UnitLease
    ownership: IdleReviewer | ClaimedReviewer
    reviews: int = 0
    last_allocated: float = 0.0


@dataclass
class Pool:
    generation: int = 0
    stopping: bool = False
    next_index: dict[ReviewerType, int] = field(default_factory=lambda: {"code": 1, "ui": 1})
    templates: dict[str, Template] = field(default_factory=dict)
    reviewers: dict[str, Reviewer] = field(default_factory=dict)


class PoolArguments(argparse.Namespace):
    production: str = ""
    type: ReviewerType = "code"
    instructions_file: str = ""
    reviewer: str = ""
    thread_id: str = ""
    unit: str = ""
    phase: str = ""
    pass_number: str = ""
    lens: str = ""
    pid: int = 0
    apply: bool = False
    verb: str = ""


class PoolError(Exception):
    """A pool operation cannot safely complete."""


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def reviewer_is_idle(reviewer: Reviewer) -> bool:
    return isinstance(reviewer.ownership, IdleReviewer) or not _alive(reviewer.ownership.pid)


def decode_pool(data: PoolJson) -> Pool:
    pool = Pool(generation=data.get("generation", 0),
                stopping=data.get("stopping", False))
    pool.next_index.update(data.get("next_index", {}))
    for name, record in data.get("templates", {}).items():
        pool.templates[name] = Template(**record)
    for name, record in data.get("reviewers", {}).items():
        lease = record["lease"]
        pid = record["busy_pid"]
        pool.reviewers[name] = Reviewer(
            type=record["type"], thread_id=record["thread_id"], prime_hash=record["prime_hash"],
            # A lease written before unit leases also names a phase; the unit is all it keeps.
            lease=Unleased() if lease is None else UnitLease(lease["unit"], lease["ordinal"]),
            ownership=IdleReviewer() if pid is None else ClaimedReviewer(pid),
            reviews=record["reviews"], last_allocated=record.get("last_allocated", 0.0),
        )
    return pool


def _encode(pool: Pool) -> PoolJson:
    reviewers: dict[str, ReviewerJson] = {}
    for name, reviewer in pool.reviewers.items():
        lease = reviewer.lease
        owner = reviewer.ownership
        reviewers[name] = {
            "type": reviewer.type, "thread_id": reviewer.thread_id, "prime_hash": reviewer.prime_hash,
            "lease": None if isinstance(lease, Unleased) else {"unit": lease.unit, "ordinal": lease.ordinal},
            "busy_pid": None if isinstance(owner, IdleReviewer) else owner.pid,
            "reviews": reviewer.reviews, "last_allocated": reviewer.last_allocated,
        }
    encoded: PoolJson = {
        "generation": pool.generation,
        "next_index": pool.next_index,
        "templates": {name: {
            "name": template.name, "thread_id": template.thread_id, "prime_hash": template.prime_hash,
        } for name, template in pool.templates.items()},
        "reviewers": reviewers,
    }
    if pool.stopping:
        encoded["stopping"] = True
    return encoded


@dataclass(frozen=True)
class ProductionPool:
    directory: Path

    def mesh_dir(self, generation: int) -> Path:
        return self.directory / "mesh" / str(generation)

    @contextmanager
    def locked(self, *, write: bool = True) -> Generator[Pool, None, None]:
        self.directory.mkdir(parents=True, exist_ok=True)
        with (self.directory / "pool.lock").open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                path = self.directory / "pool.json"
                if path.exists():
                    data: PoolJson = cast(PoolJson, json.loads(path.read_text(encoding="utf-8")))
                    pool = decode_pool(data)
                else:
                    pool = Pool()
                yield pool
                if write:
                    self.save(pool)
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def save(self, pool: Pool) -> None:
        """Write while the caller holds locked(write=False) or locked()."""
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.directory,
                                         prefix=".pool-", delete=False) as output:
            temporary = Path(output.name)
            try:
                json.dump(_encode(pool), output)
                output.flush()
                if (os.environ.get("UNIT_SYNC_WRITES") or os.environ.get("PLAN_DELEGATE_SYNC_WRITES", "1")) != "0":
                    os.fsync(output.fileno())
                os.replace(temporary, self.directory / "pool.json")
            finally:
                temporary.unlink(missing_ok=True)

    def mesh(self, verb: str, *arguments: str, generation: int, mesh_generation: int,
             allow_stopping: bool = False) -> subprocess.CompletedProcess[str]:
        with self.locked(write=False) as pool:
            _generation(pool, generation, allow_stopping=allow_stopping)
        mesh_dir = self.mesh_dir(mesh_generation)
        mesh_dir.mkdir(parents=True, exist_ok=True)
        script = os.environ.get("REVIEW_POOL_MESH", str(Path(__file__).resolve().parents[1] / "agents" / "codex_mesh.py"))
        argv = [sys.executable, script, verb, "--session-dir", str(mesh_dir), *arguments]
        try:
            return subprocess.run(argv, capture_output=True, text=True, check=False)
        except OSError as exc:
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr=str(exc))

    def collect_prompts(self, pool: Pool) -> None:
        """Keep every referenced contract, including files being primed by another process."""
        hashes = {template.prime_hash for template in pool.templates.values()}
        hashes.update(reviewer.prime_hash for reviewer in pool.reviewers.values())
        for path in (self.directory / "prime").glob("*.md"):
            if path.stem in hashes:
                continue
            try:
                with path.open("rb") as prompt:
                    try:
                        fcntl.flock(prompt, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        continue
                    path.unlink(missing_ok=True)
            except FileNotFoundError:
                continue


def production_pool(value: str) -> ProductionPool:
    slug = Path(value).name.removesuffix("-production.md")
    if slug in ("", ".", ".."):
        raise PoolError("production must name a production or slug")
    slug = canonical_slug(slug)
    root = Path(os.environ.get("REVIEW_POOL_STATE", "~/.local/state/review-pool")).expanduser()
    return ProductionPool((root / slug).resolve())


def _generation(pool: Pool, expected: int, *, allow_stopping: bool = False) -> None:
    if pool.generation != expected:
        raise PoolError("pool was stopped during the mesh call; result dropped")
    if not allow_stopping:
        _require_finished_stop(pool)


def _require_finished_stop(pool: Pool) -> None:
    if pool.stopping:
        raise PoolError("pool stop did not finish; run stop again")


def _failure(result: subprocess.CompletedProcess[str]) -> str:
    return f"mesh: {failure_detail(result)}"


def _thread_id(result: subprocess.CompletedProcess[str]) -> str:
    if result.returncode != 0:
        raise PoolError(_failure(result))
    thread_id = result.stdout.strip()
    if not thread_id or "\n" in thread_id:
        raise PoolError("mesh returned no single thread id")
    return thread_id


def _ux_rules(guide: Path) -> list[Path]:
    """The guide's rule files: a `.md` in its root whose frontmatter carries a non-empty `test:`."""
    rules: list[Path] = []
    for path in sorted(guide.glob("*.md")):
        lines = path.read_text(encoding="utf-8").splitlines()
        if not lines or lines[0].strip() != "---":
            continue
        for line in lines[1:]:
            if line.strip() == "---":
                break
            if line.startswith("test:") and line.removeprefix("test:").strip().strip("\"'"):
                rules.append(path)
                break
    return rules


def _ui_parts(slug: str) -> list[str]:
    """The ui prompt's parts after its base: the UX guide's rules, then the repository's design decisions and targets."""
    doc = timer_docs().get(slug)
    if doc is None:
        raise PoolError(f"no production doc for {slug}")
    lines = doc.read_text(encoding="utf-8").splitlines()
    value = next((line.removeprefix("- **UX guide:** ") for line in lines if line.startswith("- **UX guide:** ")), "")
    if not value.split():
        raise PoolError("production has no UX guide")
    guide = Path(value.split()[0].strip("`;,.")).expanduser().resolve()
    parts = ["## Rules", f"UX guide: {guide}"]
    parts.extend(f"### {rule.name}\n\n{rule.read_text(encoding='utf-8').rstrip()}" for rule in _ux_rules(guide))
    top = subprocess.run(["git", "-C", str(doc.parent), "rev-parse", "--show-toplevel"],
                         capture_output=True, text=True, check=False).stdout.strip()
    repository = Path(top) if top else doc.parent
    for heading, relative in (("Design decisions", "docs/design-decisions.md"), ("Targets", "docs/hana/targets.md")):
        path = repository / relative
        if path.is_file():
            parts.append(f"## {heading}\n\n{path.read_text(encoding='utf-8').rstrip()}")
    return parts


def standing_prompt(slug: str, reviewer_type: ReviewerType) -> bytes:
    """The prompt a template is primed with: the type's base prompt, and for ui the production's UX rules."""
    config = Path(__file__).resolve().parents[2] / "config"
    defaults: dict[ReviewerType, Path] = {
        "code": config / "review" / "code.md",
        "ui": config / "review_pool" / "ui.md",
    }
    override = os.environ.get("REVIEW_POOL_CONFIG", "")
    base = Path(override) / f"{reviewer_type}.md" if override else defaults[reviewer_type]
    try:
        parts = [base.read_text(encoding="utf-8").rstrip()]
    except OSError as exc:
        raise PoolError(f"no {reviewer_type} base prompt: {exc}") from exc
    if reviewer_type == "ui":
        parts.extend(_ui_parts(slug))
    return ("\n\n".join(parts) + "\n").encode()


def _prime(production: ProductionPool, reviewer_type: ReviewerType, content: bytes) -> str:
    """Prime the type's template from `content` unless it already holds it; returns the prompt hash."""
    prime_hash = hashlib.sha256(content).hexdigest()
    prompt_path = production.directory / "prime" / f"{prime_hash}.md"
    with production.locked() as pool:
        generation = pool.generation
        _require_finished_stop(pool)
        prompt_path.parent.mkdir(parents=True, exist_ok=True)
        if not prompt_path.exists():
            with prompt_path.open("xb") as copied:
                _ = copied.write(content)
        # A shared file lock protects an unpublished prime from prompt collection.
        prompt = prompt_path.open("rb")
        fcntl.flock(prompt, fcntl.LOCK_SH)
        current = pool.templates.get(reviewer_type)
        unchanged = current is not None and current.prime_hash == prime_hash
    try:
        if not unchanged:
            cwd = production.directory / "cwd"
            cwd.mkdir(exist_ok=True)
            name = f"{reviewer_type}-template-{prime_hash[:8]}"
            result = production.mesh("prime", "--name", name, "--instructions-file", str(prompt_path), "--cwd", str(cwd),
                                     generation=generation, mesh_generation=generation)
            with production.locked() as pool:
                _generation(pool, generation)
                pool.templates[reviewer_type] = Template(name, _thread_id(result), prime_hash)
        _repoint_idle(production, reviewer_type, generation)
    finally:
        prompt.close()
        with production.locked(write=False) as pool:
            production.collect_prompts(pool)
    with production.locked(write=False) as pool:
        _generation(pool, generation)
        production.collect_prompts(pool)
    return prime_hash


def command_prime(args: PoolArguments) -> int:
    print(_prime(production_pool(str(args.production)), args.type, Path(str(args.instructions_file)).read_bytes()))
    return 0


def _prime_standing(production: ProductionPool, reviewer_type: ReviewerType) -> None:
    """Prime the template from the standing prompt when it is absent or out of date; the first allocate primes it."""
    with production.locked(write=False) as pool:
        _require_finished_stop(pool)
        primed = reviewer_type in pool.templates
    try:
        content = standing_prompt(production.directory.name, reviewer_type)
    except (PoolError, OSError, UnicodeError) as exc:
        if not primed:
            raise PoolError(f"no primed {reviewer_type} template: {exc}") from exc
        print(f"review_pool: {exc}; the {reviewer_type} template keeps its prompt", file=sys.stderr)
        return
    _ = _prime(production, reviewer_type, content)


def rename_unit(production: str, old: str, new: str) -> int:
    """Carry a renamed unit's reviewers to its new id; returns the leases moved.

    Thread ids and ordinals stay, so the unit keeps the reviewers that already know it.
    """
    pool_dir = production_pool(production)
    if not pool_dir.directory.exists():
        return 0
    moved = 0
    with pool_dir.locked() as pool:
        for reviewer in pool.reviewers.values():
            lease = reviewer.lease
            if isinstance(lease, UnitLease) and lease.unit == new:
                raise PoolError(f"a reviewer is already leased to {new}")
            if isinstance(lease, UnitLease) and lease.unit == old:
                reviewer.lease = UnitLease(new, lease.ordinal)
                moved += 1
    return moved


def _owns(reviewer: Reviewer, pid: int, thread_id: str) -> bool:
    return (reviewer.thread_id == thread_id and isinstance(reviewer.ownership, ClaimedReviewer)
            and reviewer.ownership.pid == pid)


def _retire(production: ProductionPool, name: str, reviewer: Reviewer, generation: int, pid: int,
            reason: str) -> None:
    result = production.mesh("end", "--to", name, generation=generation, mesh_generation=generation)
    with production.locked() as pool:
        _generation(pool, generation)
        current = pool.reviewers.get(name)
        if current is not None and _owns(current, pid, reviewer.thread_id):
            del pool.reviewers[name]
        production.collect_prompts(pool)
    suffix = f"; end failed: {_failure(result)}" if result.returncode else ""
    print(f"review_pool: retired {name}: {reason}{suffix}", file=sys.stderr)


def _repoint(production: ProductionPool, name: str, thread_id: str, prime_hash: str, generation: int,
             pid: int) -> None:
    """Point a reviewer this process claims at the copy of `prime_hash`; its next turn sends that prompt."""
    path = production.directory / "prime" / f"{prime_hash}.md"
    try:
        prompt = path.open("rb")
    except FileNotFoundError:
        print(f"review_pool: {name} keeps its standing prompt: {path.name} was collected", file=sys.stderr)
        return
    with prompt:
        # A shared lock keeps prompt collection off the file until the pool names it.
        fcntl.flock(prompt, fcntl.LOCK_SH)
        result = production.mesh("set-instructions", "--to", name, "--instructions-file", str(path),
                                 generation=generation, mesh_generation=generation)
        if result.returncode:
            print(f"review_pool: {name} keeps its standing prompt: {_failure(result)}", file=sys.stderr)
            return
        with production.locked() as pool:
            _generation(pool, generation)
            current = pool.reviewers.get(name)
            if current is not None and _owns(current, pid, thread_id):
                current.prime_hash = prime_hash
                production.collect_prompts(pool)


def _repoint_idle(production: ProductionPool, reviewer_type: ReviewerType, generation: int) -> None:
    """Give every idle reviewer of the type the current standing prompt; a busy one gets it at its next allocate."""
    pid = os.getpid()
    with production.locked(write=False) as pool:
        _generation(pool, generation)
        names = list(pool.reviewers)
    for name in names:
        with production.locked() as pool:
            _generation(pool, generation)
            reviewer = pool.reviewers.get(name)
            template = pool.templates.get(reviewer_type)
            if (reviewer is None or template is None or reviewer.type != reviewer_type or not reviewer.thread_id
                    or reviewer.prime_hash == template.prime_hash or not reviewer_is_idle(reviewer)):
                continue
            reviewer.ownership = ClaimedReviewer(pid)
            thread_id = reviewer.thread_id
            prime_hash = template.prime_hash
        try:
            _repoint(production, name, thread_id, prime_hash, generation, pid)
        finally:
            with production.locked() as pool:
                current = pool.reviewers.get(name)
                if current is not None and _owns(current, pid, thread_id):
                    current.ownership = IdleReviewer()


def _unit_reviewers(pool: Pool, reviewer_type: ReviewerType, unit: str) -> list[tuple[str, Reviewer]]:
    """The unit's reviewers of one type, most recently allocated first."""
    owned = [(name, reviewer) for name, reviewer in pool.reviewers.items()
             if reviewer.type == reviewer_type and reviewer.thread_id
             and isinstance(reviewer.lease, UnitLease) and reviewer.lease.unit == unit]
    return sorted(owned, key=lambda entry: (entry[1].last_allocated, entry[0]), reverse=True)


def _choose(pool: Pool, reviewer_type: ReviewerType, unit: str, pid: int) -> tuple[str, Reviewer, str]:
    """Pick the unit's idle reviewer, or reserve a new one and name the thread it forks from."""
    template = pool.templates.get(reviewer_type)
    if template is None:
        raise PoolError(f"no primed {reviewer_type} template")
    owned = _unit_reviewers(pool, reviewer_type, unit)
    source = ""
    for name, reviewer in owned:
        if reviewer_is_idle(reviewer):
            break
    else:
        spare = [(name, reviewer) for name, reviewer in pool.reviewers.items()
                 if reviewer.type == reviewer_type and reviewer.thread_id and reviewer_is_idle(reviewer)
                 and isinstance(reviewer.lease, Unleased) and reviewer.prime_hash == template.prime_hash]
        if spare and not owned:
            name, reviewer = spare[0]
        else:
            index = pool.next_index[reviewer_type]
            while f"{reviewer_type}{index}" in pool.reviewers:
                index += 1
            name = f"{reviewer_type}{index}"
            pool.next_index[reviewer_type] = index + 1
            # Every reviewer of the unit is busy: a fork of the newest starts with the unit's context.
            if owned:
                source, parent = owned[0]
                reviewer = Reviewer(reviewer_type, "", parent.prime_hash, parent.lease, IdleReviewer())
            else:
                source = template.name
                reviewer = Reviewer(reviewer_type, "", template.prime_hash, Unleased(), IdleReviewer())
            pool.reviewers[name] = reviewer
    ordinal = reviewer.lease.ordinal if isinstance(reviewer.lease, UnitLease) else 0
    reviewer.lease = UnitLease(unit, ordinal + 1)
    reviewer.ownership = ClaimedReviewer(pid)
    reviewer.last_allocated = time.time()
    return name, reviewer, source


def command_allocate(args: PoolArguments) -> int:
    production = production_pool(str(args.production))
    pid = int(args.pid)
    _prime_standing(production, args.type)
    unit = _lease_unit(production, str(args.unit))
    with production.locked() as pool:
        generation = pool.generation
        _require_finished_stop(pool)
        name, reviewer, source = _choose(pool, args.type, unit, pid)
        template_name = pool.templates[args.type].name
    newly_forked = not reviewer.thread_id
    if newly_forked:
        result = production.mesh("fork", "--from", source, "--name", name,
                                 generation=generation, mesh_generation=generation)
        if result.returncode and source != template_name:
            # The unit's reviewer could not be forked; a template fork still serves the review.
            print(f"review_pool: fork of {source} failed ({_failure(result)}); forking the template",
                  file=sys.stderr)
            result = production.mesh("fork", "--from", template_name, "--name", name,
                                     generation=generation, mesh_generation=generation)
            source = template_name
        with production.locked() as pool:
            _generation(pool, generation)
            current = pool.reviewers.get(name)
            if current is None or not _owns(current, pid, ""):
                raise PoolError(f"{name} allocation ownership changed")
            fork_error = ""
            try:
                thread_id = _thread_id(result)
            except PoolError as exc:
                fork_error = str(exc)
                del pool.reviewers[name]
                production.collect_prompts(pool)
                # Commit removal before reporting a failed fork.
                thread_id = ""
            if thread_id:
                current.thread_id = thread_id
                if source == template_name:
                    # A template fork has none of the unit's context: its lease starts again at the first review.
                    current.lease = UnitLease(unit, 1)
                    current.prime_hash = pool.templates[args.type].prime_hash
                reviewer = current
        if not thread_id:
            raise PoolError(f"fork {name} failed: {fork_error}")
    with production.locked(write=False) as pool:
        _generation(pool, generation)
        template = pool.templates.get(args.type)
        current_hash = template.prime_hash if template is not None else reviewer.prime_hash
    if current_hash != reviewer.prime_hash:
        # Prime passed over this reviewer while it was busy, or forked it from one: this turn gets the new prompt.
        _repoint(production, name, reviewer.thread_id, current_hash, generation, pid)
    with production.locked(write=False) as pool:
        _generation(pool, generation)
        current = pool.reviewers.get(name)
        if current is None or not _owns(current, pid, reviewer.thread_id):
            raise PoolError(f"{name} allocation ownership changed")
        if not isinstance(current.lease, UnitLease):
            raise PoolError(f"{name} allocation lost its lease")
        allocation: AllocationJson = {
            "reviewer": name, "thread_id": current.thread_id, "mesh_dir": str(production.mesh_dir(generation)),
            "lease_ordinal": current.lease.ordinal, "prime_hash": current.prime_hash,
        }
    print(json.dumps(allocation))
    return 0


def command_release(args: PoolArguments) -> int:
    with production_pool(str(args.production)).locked() as pool:
        reviewer = pool.reviewers.get(str(args.reviewer))
        if reviewer is not None and isinstance(reviewer.ownership, ClaimedReviewer) and reviewer.ownership.pid == int(args.pid):
            if args.thread_id and args.thread_id != reviewer.thread_id:
                # The mesh refused the resume and started a new thread: it starts the unit's reviews again.
                reviewer.thread_id = args.thread_id
                if isinstance(reviewer.lease, UnitLease):
                    reviewer.lease = UnitLease(reviewer.lease.unit, 0)
            reviewer.ownership = IdleReviewer()
            reviewer.reviews += 1
    return 0


def _lease_unit(production: ProductionPool, unit: str) -> str:
    """The lease name a caller means: a name some reviewer is leased under wins, else its Units row."""
    with production.locked(write=False) as pool:
        if any(isinstance(reviewer.lease, UnitLease) and reviewer.lease.unit == unit for reviewer in pool.reviewers.values()):
            return unit
    return _unit_id(production.directory.name, unit)


def _unit_id(slug: str, unit: str) -> str:
    """The Units table id a caller means: `trunk` is `trunk-unit` when only that row exists.

    A unit absent from the table, or a production with no readable doc, keeps the name given.
    """
    doc = timer_docs().get(slug)
    if doc is None:
        return unit
    production_scripts = str(Path(__file__).resolve().parents[1] / "production")
    if production_scripts not in sys.path:
        sys.path.insert(0, production_scripts)
    import production_units

    try:
        rows = production_units.unit_table(doc.read_text(encoding="utf-8").splitlines())
    except (OSError, UnicodeError, ValueError, production_units.Refusal):
        return unit
    ids = {production_units.cell_value(cells.get("Unit", "")) for cells in rows}
    return unit if unit in ids or f"{unit}-unit" not in ids else f"{unit}-unit"


def command_retire(args: PoolArguments) -> int:
    """End the unit's reviewers; one still reviewing keeps its turn and is left in place."""
    production = production_pool(str(args.production))
    unit = _lease_unit(production, str(args.unit))
    pid = os.getpid()
    with production.locked(write=False) as pool:
        _require_finished_stop(pool)
        generation = pool.generation
        names = [name for name, reviewer in pool.reviewers.items()
                 if isinstance(reviewer.lease, UnitLease) and reviewer.lease.unit == unit]
    for name in names:
        with production.locked() as pool:
            _generation(pool, generation)
            reviewer = pool.reviewers.get(name)
            if reviewer is None:
                continue
            if not reviewer_is_idle(reviewer):
                print(f"review_pool: {name} is reviewing; left in place", file=sys.stderr)
                continue
            if not reviewer.thread_id:
                # A fork whose allocating process died never got a thread.
                del pool.reviewers[name]
                production.collect_prompts(pool)
                print(name)
                continue
            reviewer.ownership = ClaimedReviewer(pid)
        _retire(production, name, reviewer, generation, pid, f"{unit} retired")
        print(name)
    return 0


def command_end_lease(_args: PoolArguments) -> int:
    # A reviewer belongs to its unit for good; a director still running the old phase-end step ends nothing.
    return 0


def running_units(slug: str) -> set[str]:
    """Units of the production whose unit director runs, plus those whose state is unknown."""
    # unit_lookup imports its siblings by bare name, and it reads herdr, so only migrate loads it.
    production_scripts = str(Path(__file__).resolve().parents[1] / "production")
    if production_scripts not in sys.path:
        sys.path.insert(0, production_scripts)
    import unit_lookup

    return {unit for unit, marked in unit_lookup.marked_units(slug).items()
            if not isinstance(marked.claude, unit_lookup.ClaudeNotRunning)}


@dataclass(frozen=True)
class MigrationStep:
    name: str
    thread_id: str
    keep: bool
    reason: str


def migration_plan(pool: Pool, running: set[str]) -> list[MigrationStep]:
    """Keep each running unit's newest reviewer per type; every other idle reviewer retires."""
    steps: list[MigrationStep] = []
    kept: set[tuple[str, ReviewerType]] = set()
    ordered = sorted(pool.reviewers.items(), key=lambda entry: (entry[1].last_allocated, entry[0]), reverse=True)
    for name, reviewer in ordered:
        lease = reviewer.lease
        if not reviewer_is_idle(reviewer):
            steps.append(MigrationStep(name, reviewer.thread_id, True, "busy"))
        elif not reviewer.thread_id:
            steps.append(MigrationStep(name, "", True, "fork in flight"))
        elif isinstance(lease, Unleased):
            steps.append(MigrationStep(name, reviewer.thread_id, False, "no unit"))
        # Lease names predate one spelling: some directors gave `trunk`, others `trunk-unit`.
        elif lease.unit not in running and f"{lease.unit}-unit" not in running:
            steps.append(MigrationStep(name, reviewer.thread_id, False, f"{lease.unit} is not running"))
        elif (lease.unit, reviewer.type) in kept:
            steps.append(MigrationStep(name, reviewer.thread_id, False, f"{lease.unit} keeps a newer {reviewer.type} reviewer"))
        else:
            kept.add((lease.unit, reviewer.type))
            steps.append(MigrationStep(name, reviewer.thread_id, True, f"{lease.unit}'s {reviewer.type} reviewer"))
    return steps


def command_migrate(args: PoolArguments) -> int:
    production = production_pool(str(args.production))
    running = running_units(production.directory.name)
    with production.locked(write=False) as pool:
        _require_finished_stop(pool)
        generation = pool.generation
        steps = migration_plan(pool, running)
    for step in steps:
        print(f"{'keep' if step.keep else 'retire'}\t{step.name}\t{step.reason}")
    if not args.apply:
        return 0
    pid = os.getpid()
    for step in steps:
        if step.keep:
            continue
        with production.locked() as pool:
            _generation(pool, generation)
            reviewer = pool.reviewers.get(step.name)
            # Allocation may have taken it since the plan was printed.
            if reviewer is None or reviewer.thread_id != step.thread_id or not reviewer_is_idle(reviewer):
                print(f"review_pool: {step.name} changed since the plan; left in place", file=sys.stderr)
                continue
            reviewer.ownership = ClaimedReviewer(pid)
        _retire(production, step.name, reviewer, generation, pid, step.reason)
    return 0


def command_status(args: PoolArguments) -> int:
    with production_pool(str(args.production)).locked(write=False) as pool:
        for name, reviewer in pool.reviewers.items():
            lease = reviewer.lease
            description = "unleased" if isinstance(lease, Unleased) else f"{lease.unit}:{lease.ordinal}"
            print(f"{name}\t{reviewer.type}\t{description}\t{'idle' if reviewer_is_idle(reviewer) else 'busy'}\t{reviewer.reviews}")
    return 0


def command_stop(args: PoolArguments) -> int:
    production = production_pool(str(args.production))
    with production.locked() as pool:
        adoption = production.directory / "ui_adopted"
        if adoption.exists():
            adoption.unlink()
            print("review_pool: ui adoption cleared", file=sys.stderr)
        # Retry an interrupted stop against the same ended generation.
        if not pool.stopping:
            pool.generation += 1
        ended_generation = pool.generation - 1
        pool.stopping = True
        generation = pool.generation
        pool.templates.clear()
        pool.reviewers.clear()
        pool.next_index = {"code": 1, "ui": 1}
    result = production.mesh("stop", generation=generation, mesh_generation=ended_generation, allow_stopping=True)
    with production.locked() as pool:
        _generation(pool, generation, allow_stopping=True)
        if result.returncode == 0:
            pool.stopping = False
            production.collect_prompts(pool)
    if result.returncode:
        raise PoolError(_failure(result))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="verb", required=True)
    for verb in ("prime", "allocate", "release", "retire", "end-lease", "status", "stop", "migrate"):
        command = subparsers.add_parser(verb)
        _ = command.add_argument("--production", required=True)
        if verb in ("prime", "allocate"):
            _ = command.add_argument("--type", choices=("code", "ui"), required=True)
        if verb == "prime":
            _ = command.add_argument("--instructions-file", required=True)
        if verb == "retire":
            _ = command.add_argument("--unit", required=True)
        if verb in ("allocate", "end-lease"):
            _ = command.add_argument("--unit", required=verb == "allocate", default="")
            _ = command.add_argument("--phase", required=verb == "allocate", default="")
        if verb == "allocate":
            _ = command.add_argument("--pass", dest="pass_number", required=True)
            _ = command.add_argument("--lens", default="")
            # Accepted from a review started before reviewers stopped resetting; it changes nothing.
            _ = command.add_argument("--fresh", action="store_true")
        if verb in ("allocate", "release"):
            _ = command.add_argument("--pid", type=int, required=True)
        if verb == "release":
            _ = command.add_argument("--reviewer", required=True)
            _ = command.add_argument("--thread-id", default="")
        if verb == "migrate":
            _ = command.add_argument("--apply", action="store_true")
    args = parser.parse_args(namespace=PoolArguments())
    handlers = {
        "prime": command_prime,
        "allocate": command_allocate, "release": command_release, "retire": command_retire,
        "end-lease": command_end_lease,
        "status": command_status, "stop": command_stop,
        "migrate": command_migrate,
    }
    try:
        return handlers[str(args.verb)](args)
    except (PoolError, OSError, ValueError, KeyError, TypeError) as exc:
        print(f"review_pool: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
