#!/usr/bin/env python3
"""Allocate production reviewers immediately and retain their context for a phase."""
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
from scripts.production.showrunners import canonical_slug  # noqa: E402

ReviewerType = Literal["code", "ui"]
Split = Literal["hash", "all", "off"]


@dataclass(frozen=True)
class Unleased:
    """Available to take a new phase lease."""


@dataclass(frozen=True)
class PhaseLease:
    unit: str
    phase: str
    ordinal: int


@dataclass(frozen=True)
class IdleReviewer:
    """No live process owns this reviewer."""


@dataclass(frozen=True)
class ClaimedReviewer:
    pid: int


class LeaseJson(TypedDict):
    unit: str
    phase: str
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
    served: list[list[str]]
    reviews: int
    last_allocated: NotRequired[float]


class PoolJson(TypedDict):
    split: Split
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
    lease: Unleased | PhaseLease
    ownership: IdleReviewer | ClaimedReviewer
    served: list[tuple[str, str]] = field(default_factory=list)
    reviews: int = 0
    last_allocated: float = 0.0


@dataclass
class Pool:
    split: Split = "hash"
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
    fresh: bool = False
    set: Split = "hash"
    stale_hours: float = 12.0
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


def _idle(reviewer: Reviewer) -> bool:
    return isinstance(reviewer.ownership, IdleReviewer) or not _alive(reviewer.ownership.pid)


def _decode(data: PoolJson) -> Pool:
    pool = Pool(split=data.get("split", "hash"), generation=data.get("generation", 0),
                stopping=data.get("stopping", False))
    pool.next_index.update(data.get("next_index", {}))
    for name, record in data.get("templates", {}).items():
        pool.templates[name] = Template(**record)
    for name, record in data.get("reviewers", {}).items():
        lease = record["lease"]
        pid = record["busy_pid"]
        pool.reviewers[name] = Reviewer(
            type=record["type"], thread_id=record["thread_id"], prime_hash=record["prime_hash"],
            lease=Unleased() if lease is None else PhaseLease(**lease),
            ownership=IdleReviewer() if pid is None else ClaimedReviewer(pid),
            served=[(pair[0], pair[1]) for pair in record["served"]],
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
            "lease": None if isinstance(lease, Unleased) else {
                "unit": lease.unit, "phase": lease.phase, "ordinal": lease.ordinal,
            },
            "busy_pid": None if isinstance(owner, IdleReviewer) else owner.pid,
            "served": [[number, lens] for number, lens in reviewer.served],
            "reviews": reviewer.reviews, "last_allocated": reviewer.last_allocated,
        }
    encoded: PoolJson = {
        "split": pool.split, "generation": pool.generation,
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
                    pool = _decode(data)
                else:
                    pool = Pool()
                yield pool
                if write:
                    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.directory,
                                                     prefix=".pool-", delete=False) as output:
                        temporary = Path(output.name)
                        try:
                            json.dump(_encode(pool), output)
                            output.flush()
                            # Disposable fixtures may omit disk durability; production syncs by default.
                            if os.environ.get("PLAN_DELEGATE_SYNC_WRITES", "1") != "0":
                                os.fsync(output.fileno())
                            os.replace(temporary, path)
                        finally:
                            temporary.unlink(missing_ok=True)
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

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


def _production(value: str) -> ProductionPool:
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
    return result.stderr.strip() or result.stdout.strip() or f"mesh exited {result.returncode}"


def _thread_id(result: subprocess.CompletedProcess[str]) -> str:
    if result.returncode != 0:
        raise PoolError(_failure(result))
    thread_id = result.stdout.strip()
    if not thread_id or "\n" in thread_id:
        raise PoolError("mesh returned no single thread id")
    return thread_id


def command_prime(args: PoolArguments) -> int:
    production = _production(str(args.production))
    reviewer_type = args.type
    content = Path(str(args.instructions_file)).read_bytes()
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
            _retire_unleased(production, reviewer_type, generation)
    finally:
        prompt.close()
        with production.locked(write=False) as pool:
            production.collect_prompts(pool)
    with production.locked(write=False) as pool:
        _generation(pool, generation)
        production.collect_prompts(pool)
    print(prime_hash)
    return 0


def command_unit_memory(args: PoolArguments) -> int:
    if not args.unit or Path(args.unit).name != args.unit or args.unit in (".", ".."):
        raise PoolError("unit must name one memory file")
    path = _production(args.production).directory / "memory" / f"{args.unit}.md"
    try:
        content = path.read_bytes()
    except FileNotFoundError:
        return 0
    _ = sys.stdout.buffer.write(content)
    return 0


def command_arm(args: PoolArguments) -> int:
    production = _production(str(args.production))
    with production.locked(write=False) as pool:
        enabled = "code" in pool.templates and (
            pool.split == "all" or (pool.split == "hash" and
                hashlib.sha256(f"{args.unit}\n{args.phase}".encode()).digest()[0] % 2 == 0)
        )
    print("pool" if enabled else "own")
    return 0


def command_split(args: PoolArguments) -> int:
    with _production(str(args.production)).locked() as pool:
        pool.split = args.set
    return 0


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


def _retire_unleased(production: ProductionPool, reviewer_type: ReviewerType, generation: int) -> None:
    pid = os.getpid()
    with production.locked(write=False) as pool:
        _generation(pool, generation)
        names = list(pool.reviewers)
    for name in names:
        with production.locked() as pool:
            _generation(pool, generation)
            reviewer = pool.reviewers.get(name)
            template = pool.templates.get(reviewer_type)
            if (reviewer is None or template is None or reviewer.type != reviewer_type
                    or reviewer.prime_hash == template.prime_hash
                    or not isinstance(reviewer.lease, Unleased) or not _idle(reviewer)):
                continue
            reviewer.ownership = ClaimedReviewer(pid)
        _retire(production, name, reviewer, generation, pid, "old prime hash")


def _choose(pool: Pool, reviewer_type: ReviewerType, unit: str, phase: str, number: str,
            lens: str, pid: int) -> tuple[str, Reviewer, Template]:
    template = pool.templates.get(reviewer_type)
    if template is None:
        raise PoolError(f"no primed {reviewer_type} template")
    candidates = [(name, reviewer) for name, reviewer in pool.reviewers.items()
                  if reviewer.type == reviewer_type and reviewer.thread_id and _idle(reviewer)]
    for name, reviewer in candidates:
        lease = reviewer.lease
        if (isinstance(lease, PhaseLease) and (lease.unit, lease.phase) == (unit, phase)
                and not any(served_pass == number and served_lens != lens for served_pass, served_lens in reviewer.served)):
            break
    else:
        for name, reviewer in candidates:
            if isinstance(reviewer.lease, Unleased) and reviewer.prime_hash == template.prime_hash:
                break
        else:
            index = pool.next_index[reviewer_type]
            while f"{reviewer_type}{index}" in pool.reviewers:
                index += 1
            name = f"{reviewer_type}{index}"
            pool.next_index[reviewer_type] = index + 1
            reviewer = Reviewer(reviewer_type, "", template.prime_hash, Unleased(), IdleReviewer())
            pool.reviewers[name] = reviewer
    ordinal = reviewer.lease.ordinal if isinstance(reviewer.lease, PhaseLease) else 0
    reviewer.lease = PhaseLease(unit, phase, ordinal + 1)
    reviewer.ownership = ClaimedReviewer(pid)
    reviewer.served.append((number, lens))
    reviewer.last_allocated = time.time()
    return name, reviewer, template


def command_allocate(args: PoolArguments) -> int:
    production = _production(str(args.production))
    pid = int(args.pid)
    while True:
        with production.locked() as pool:
            generation = pool.generation
            _require_finished_stop(pool)
            name, reviewer, template = _choose(pool, args.type, str(args.unit),
                                               str(args.phase), str(args.pass_number), str(args.lens), pid)
        newly_forked = not reviewer.thread_id
        if newly_forked:
            result = production.mesh("fork", "--from", template.name, "--name", name,
                                     generation=generation, mesh_generation=generation)
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
                    reviewer.thread_id = thread_id
            if not thread_id:
                raise PoolError(f"fork {name} failed: {fork_error}")
        if args.fresh and not newly_forked:
            result = production.mesh("reset", "--to", name, generation=generation, mesh_generation=generation)
            with production.locked(write=False) as pool:
                _generation(pool, generation)
            if result.returncode:
                _retire(production, name, reviewer, generation, pid, f"reset failed: {_failure(result)}")
                continue
        with production.locked(write=False) as pool:
            _generation(pool, generation)
            current = pool.reviewers.get(name)
            if current is None or not _owns(current, pid, reviewer.thread_id):
                raise PoolError(f"{name} allocation ownership changed")
            if not isinstance(current.lease, PhaseLease):
                raise PoolError(f"{name} allocation lost its lease")
            allocation: AllocationJson = {
                "reviewer": name, "thread_id": current.thread_id, "mesh_dir": str(production.mesh_dir(generation)),
                "lease_ordinal": current.lease.ordinal, "prime_hash": current.prime_hash,
            }
        print(json.dumps(allocation))
        return 0


def command_release(args: PoolArguments) -> int:
    with _production(str(args.production)).locked() as pool:
        reviewer = pool.reviewers.get(str(args.reviewer))
        if reviewer is not None and isinstance(reviewer.ownership, ClaimedReviewer) and reviewer.ownership.pid == int(args.pid):
            if args.thread_id and args.thread_id != reviewer.thread_id:
                reviewer.thread_id = args.thread_id
            reviewer.ownership = IdleReviewer()
            reviewer.reviews += 1
    return 0


def _end_reviewers(production: ProductionPool, leases: dict[str, PhaseLease]) -> list[str]:
    ended: list[str] = []
    pid = os.getpid()
    for name, expected_lease in leases.items():
        with production.locked() as pool:
            _require_finished_stop(pool)
            reviewer = pool.reviewers.get(name)
            if reviewer is None or reviewer.lease != expected_lease:
                continue
            if not _idle(reviewer):
                print(f"review_pool: {name} is busy; lease left intact", file=sys.stderr)
                continue
            if not reviewer.thread_id:
                del pool.reviewers[name]
                production.collect_prompts(pool)
                print(f"review_pool: removed {name}: reservation owner is dead", file=sys.stderr)
                ended.append(name)
                continue
            reviewer.ownership = ClaimedReviewer(pid)
            generation = pool.generation
            template = pool.templates.get(reviewer.type)
            obsolete = template is None or template.prime_hash != reviewer.prime_hash
        if obsolete:
            _retire(production, name, reviewer, generation, pid, "old prime hash")
        else:
            result = production.mesh("reset", "--to", name, generation=generation, mesh_generation=generation)
            with production.locked(write=False) as pool:
                _generation(pool, generation)
            if result.returncode:
                _retire(production, name, reviewer, generation, pid, f"reset failed: {_failure(result)}")
            else:
                with production.locked() as pool:
                    _generation(pool, generation)
                    current = pool.reviewers.get(name)
                    if current is None or not _owns(current, pid, reviewer.thread_id):
                        raise PoolError(f"{name} lease-end ownership changed")
                    template = pool.templates.get(current.type)
                    obsolete = template is None or template.prime_hash != current.prime_hash
                    if not obsolete:
                        current.lease = Unleased()
                        current.served.clear()
                        current.ownership = IdleReviewer()
                        production.collect_prompts(pool)
                if obsolete:
                    _retire(production, name, reviewer, generation, pid, "old prime hash")
        ended.append(name)
    return ended


def command_end_lease(args: PoolArguments) -> int:
    production = _production(str(args.production))
    with production.locked(write=False) as pool:
        _require_finished_stop(pool)
        leases = {name: reviewer.lease for name, reviewer in pool.reviewers.items()
                  if isinstance(reviewer.lease, PhaseLease) and reviewer.lease.unit == str(args.unit)
                 and (not args.phase or reviewer.lease.phase == str(args.phase))}
    _ = _end_reviewers(production, leases)
    return 0


def command_health(args: PoolArguments) -> int:
    production = _production(str(args.production))
    cutoff = time.time() - float(args.stale_hours) * 3600
    with production.locked(write=False) as pool:
        _require_finished_stop(pool)
        leases = {name: reviewer.lease for name, reviewer in pool.reviewers.items()
                  if isinstance(reviewer.lease, PhaseLease) and reviewer.last_allocated < cutoff}
    for name in _end_reviewers(production, leases):
        print(name)
    return 0


def command_status(args: PoolArguments) -> int:
    with _production(str(args.production)).locked(write=False) as pool:
        for name, reviewer in pool.reviewers.items():
            lease = reviewer.lease
            description = "unleased" if isinstance(lease, Unleased) else f"{lease.unit}:{lease.phase}:{lease.ordinal}"
            print(f"{name}\t{reviewer.type}\t{description}\t{'idle' if _idle(reviewer) else 'busy'}\t{reviewer.reviews}")
    return 0


def command_stop(args: PoolArguments) -> int:
    production = _production(str(args.production))
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
    for verb in ("prime", "arm", "split", "allocate", "release", "end-lease", "status", "health", "stop", "unit-memory"):
        command = subparsers.add_parser(verb)
        _ = command.add_argument("--production", required=True)
        if verb in ("prime", "allocate"):
            _ = command.add_argument("--type", choices=("code", "ui"), required=True)
        if verb == "prime":
            _ = command.add_argument("--instructions-file", required=True)
        if verb == "unit-memory":
            _ = command.add_argument("--unit", required=True)
        if verb in ("arm", "allocate", "end-lease"):
            _ = command.add_argument("--unit", required=True)
            _ = command.add_argument("--phase", required=verb != "end-lease", default="")
        if verb == "allocate":
            _ = command.add_argument("--pass", dest="pass_number", required=True)
            _ = command.add_argument("--lens", default="")
            _ = command.add_argument("--fresh", action="store_true")
        if verb in ("allocate", "release"):
            _ = command.add_argument("--pid", type=int, required=True)
        if verb == "release":
            _ = command.add_argument("--reviewer", required=True)
            _ = command.add_argument("--thread-id", default="")
        if verb == "split":
            _ = command.add_argument("--set", choices=("hash", "all", "off"), required=True)
        if verb == "health":
            _ = command.add_argument("--stale-hours", type=float, default=12.0)
    args = parser.parse_args(namespace=PoolArguments())
    handlers = {
        "prime": command_prime, "arm": command_arm, "split": command_split,
        "allocate": command_allocate, "release": command_release, "end-lease": command_end_lease,
        "status": command_status, "health": command_health, "stop": command_stop,
        "unit-memory": command_unit_memory,
    }
    try:
        return handlers[str(args.verb)](args)
    except (PoolError, OSError, ValueError, KeyError, TypeError) as exc:
        print(f"review_pool: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
