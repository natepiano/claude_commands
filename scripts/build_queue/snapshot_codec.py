"""Encode and validate tagged domain records."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import MISSING, fields, is_dataclass
from functools import lru_cache
import math
from pathlib import Path
from types import UnionType
from typing import Callable, Literal, NotRequired, cast, get_args, get_origin, get_type_hints, is_typeddict

@lru_cache(maxsize=None)
def snapshot_fields(schema: type) -> dict[str, object]:
    return cast(dict[str, object], get_type_hints(schema, include_extras=True))


def snapshot_value(value: object) -> object:
    """The cache's tagged records retain domain variants, including nested memory evidence."""
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value) and not isinstance(value, type):
        return {"kind": type(value).__name__, **{field.name: snapshot_value(cast(object, getattr(value, field.name)))
                                                for field in fields(value)}}
    if isinstance(value, Mapping):
        return {key: snapshot_value(item) for key, item in cast(Mapping[str, object], value).items()}
    if isinstance(value, frozenset):
        return [snapshot_value(item) for item in sorted(cast(frozenset[str], value))]
    if isinstance(value, (tuple, list)):
        return [snapshot_value(item) for item in cast(tuple[object, ...] | list[object], value)]
    return value


def parse_snapshot_value(value: object, schema: object) -> object:
    """Validate the wire shape against the same domain fields that the writer serializes."""
    origin, args = get_origin(schema), cast(tuple[object, ...], get_args(schema))
    if schema is object:
        return value
    if origin is NotRequired:
        return parse_snapshot_value(value, args[0])
    # A union of Literal aliases still has typing.Union as its origin on 3.13.
    if origin is UnionType or origin is get_origin(Literal[0] | Literal[1]):
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
    if any(member.name not in record and member.default is MISSING and member.default_factory is MISSING
           for member in fields(schema)):
        raise ValueError("invalid snapshot record fields")
    return cast(Callable[..., object], schema)(**{name: parse_snapshot_value(record[name], kind)
                                                for name, kind in annotations.items() if name in record})
