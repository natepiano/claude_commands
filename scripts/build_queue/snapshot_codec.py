"""Encode and validate tagged domain records."""
from __future__ import annotations

from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import MISSING, fields, is_dataclass
from enum import Enum, auto
from functools import lru_cache
import math
from pathlib import Path
from types import UnionType
from typing import Callable, Literal, NotRequired, cast, get_args, get_origin, get_type_hints, is_typeddict

# Literal unions use typing.Union on Python 3.13.
LITERAL_UNION_ORIGIN = get_origin(Literal[0] | Literal[1])


class SnapshotReadPolicy(Enum):
    ALL_FIELDS = auto()
    ACQUISITION_FIELDS = auto()


_READ_POLICY = ContextVar("snapshot_read_policy", default=SnapshotReadPolicy.ALL_FIELDS)


@contextmanager
def snapshot_read_policy(policy: SnapshotReadPolicy) -> Generator[None]:
    """Limit omitted fields to the current reader and restore the previous policy."""
    token = _READ_POLICY.set(policy)
    try:
        yield
    finally:
        _READ_POLICY.reset(token)


@lru_cache(maxsize=None)
def acquisition_unused_fields(schema: type) -> frozenset[str]:
    """Each omitted field must have a default for the acquisition reader."""
    return frozenset(member.name for member in fields(schema)
                     if member.metadata.get("acquisition_unused") is True
                     and (member.default is not MISSING or member.default_factory is not MISSING))


@lru_cache(maxsize=None)
def snapshot_fields(schema: type) -> dict[str, object]:
    return cast(dict[str, object], get_type_hints(schema, include_extras=True))


@lru_cache(maxsize=None)
def record_field_names(schema: type) -> tuple[str, ...]:
    return tuple(member.name for member in fields(schema))


def wire_record(schema: type, record: dict[str, object], *, writing: bool) -> dict[str, object]:
    """Keep existing wire fields compatible with the pinned client."""
    if (schema.__module__ == "scripts.build_queue.placement"
            and schema.__name__ in ("PlacementAssignment", "PlacementConstraints")):
        source = record.get("source_identity", record.get("source_key"))
        if writing:
            assert isinstance(source, dict)
            identity = cast(dict[str, object], source)
            return {**record, "source_identity": identity, "source_key": identity.get("key", "")}
        legacy = record.get("source_key")
        if (isinstance(legacy, str) and isinstance(source, dict)
                and cast(dict[str, object], source).get("key", "") != legacy):
            source = legacy
        if isinstance(source, str):
            source = ({"kind": "SourceKeyKnown", "key": source} if source else
                      {"kind": "SourceKeyUnavailable", "reason": "older request has no source key"})
        return {**record, "source_key": source}
    if schema.__module__ == "scripts.build_queue.admission_records" and "running" in record:
        if writing:
            running = record["running"]
            assert isinstance(running, list)
            rows = cast(list[object], running)
            local_rows = [row for row in rows if not remote_memory_row(row)]
            if len(local_rows) != len(rows):
                return {**record, "running": local_rows, "running_with_remote": rows}
        elif "running_with_remote" in record:
            return {**record, "running": record["running_with_remote"]}
    return record


def remote_memory_row(value: object) -> bool:
    """Identify remote rows that the pinned memory schema cannot represent."""
    if not isinstance(value, dict):
        return False
    row = cast(dict[str, object], value)
    step = row.get("step") if row.get("kind") == "ExplainedRunningStep" else row
    if not isinstance(step, dict):
        return False
    measured = cast(dict[str, object], step).get("measured")
    return (isinstance(measured, dict)
            and cast(dict[str, object], measured).get("kind") == "RemoteMemoryUnavailable")



def snapshot_value(value: object) -> object:
    """Convert each shared record once within this encoding."""
    converted: dict[int, object] = {}

    def convert(item: object) -> object:
        if type(item) in (str, int, float, bool, type(None)):
            return item
        identity = id(item)
        if identity in converted:
            return converted[identity]
        if isinstance(item, Path):
            result: object = str(item)
        elif is_dataclass(item) and not isinstance(item, type):
            result = wire_record(type(item),
                {"kind": type(item).__name__, **{name: convert(cast(object, getattr(item, name)))
                 for name in record_field_names(type(item))}}, writing=True)
        elif isinstance(item, Mapping):
            result = {key: convert(value) for key, value in cast(Mapping[str, object], item).items()}
        elif isinstance(item, frozenset):
            result = [convert(value) for value in sorted(cast(frozenset[str], item))]
        elif isinstance(item, (tuple, list)):
            result = [convert(value) for value in cast(tuple[object, ...] | list[object], item)]
        else:
            return item
        converted[identity] = result
        return result

    return convert(value)


def parse_snapshot_value(value: object, schema: object) -> object:
    """Validate the wire shape against the same domain fields that the writer serializes."""
    origin, args = get_origin(schema), cast(tuple[object, ...], get_args(schema))
    if schema is object:
        return value
    if origin is NotRequired:
        return parse_snapshot_value(value, args[0])
    if origin is UnionType or origin is LITERAL_UNION_ORIGIN:
        for variant in args:
            try:
                return parse_snapshot_value(value, variant)
            except (ValueError, TypeError, KeyError):
                continue
        raise ValueError("invalid snapshot variant")
    if origin is Literal:
        if value not in args:
            raise ValueError("invalid snapshot literal")
        return value
    if schema is type(None):
        if value is not None:
            raise ValueError("invalid snapshot null")
        return None
    if schema is str or schema is int or schema is float or schema is bool:
        valid = (type(value) is schema or schema is float and type(value) is int)
        if not valid or isinstance(value, (int, float)) and not math.isfinite(value):
            raise ValueError("invalid snapshot scalar")
        return value
    if schema is Path:
        return Path(cast(str, parse_snapshot_value(value, str)))
    if origin in (list, tuple, frozenset, Sequence):
        if not isinstance(value, list):
            raise ValueError("invalid snapshot sequence")
        items = cast(list[object], value)
        if origin in (list, frozenset, Sequence) or len(args) == 2 and args[1] is Ellipsis:
            parsed = [parse_snapshot_value(item, args[0]) for item in items]
        else:
            if len(items) != len(args):
                raise ValueError("invalid snapshot tuple")
            parsed = [parse_snapshot_value(item, kind) for item, kind in zip(items, args, strict=True)]
        return parsed if origin in (list, Sequence) else frozenset(parsed) if origin is frozenset else tuple(parsed)
    if origin in (dict, Mapping):
        if not isinstance(value, dict):
            raise ValueError("invalid snapshot mapping")
        return {parse_snapshot_value(key, args[0]): parse_snapshot_value(item, args[1])
                for key, item in cast(dict[str, object], value).items()}
    if not isinstance(schema, type) or not isinstance(value, dict):
        raise ValueError("invalid snapshot record")
    record = cast(dict[str, object], value)
    annotations = snapshot_fields(schema)
    if is_typeddict(schema):
        if any(name not in record and get_origin(kind) is not NotRequired for name, kind in annotations.items()):
            raise ValueError("missing snapshot row field")
        return {name: parse_snapshot_value(item, annotations[name]) for name, item in record.items()
                if name in annotations}
    if not is_dataclass(schema) or record.get("kind") != schema.__name__:
        raise ValueError("invalid snapshot record tag")
    record = wire_record(schema, record, writing=False)
    if any(member.name not in record and member.default is MISSING and member.default_factory is MISSING
           for member in fields(schema)):
        raise ValueError("invalid snapshot record fields")
    unused = (acquisition_unused_fields(schema)
              if _READ_POLICY.get() is SnapshotReadPolicy.ACQUISITION_FIELDS else frozenset[str]())
    return cast(Callable[..., object], schema)(**{name: parse_snapshot_value(record[name], kind)
                                                for name, kind in annotations.items() if name in record and name not in unused})
