"""Dataclass-based wire models — the serialisation core shared by client and server.

The protocol used to be defined with pydantic. That made the client impossible to
install on platforms without a Rust toolchain: ``pydantic-core`` publishes no
FreeBSD wheel, so ``pip install`` tries to compile it, which rules out pfSense
and anything else with a stock interpreter and no compiler. Every other
dependency is pure Python.

The protocol models never needed what pydantic offers. They are plain data —
strings, ints, bools, enums, lists and nested models — with no coercion rules and
(outside :mod:`hle_common.models`) no validators at all. So this provides the
small API surface the codebase actually used, over stdlib dataclasses:

    model_dump()            model_validate()
    model_dump_json()       model_validate_json()
    model_copy()

Keeping those names means the ~119 call sites across both repos are untouched;
only the model definitions change. Client and server share this one
implementation, so there is no second serialiser to drift from.

Compatibility rules, chosen to match what pydantic did on this data:

* Unknown keys on input are **ignored**, so a newer server can add fields
  without breaking older clients.
* Fields that are absent fall back to their default; a model with a missing
  required field raises :class:`ValueError`.
* ``None`` is preserved rather than dropped, so the JSON shape is unchanged.
* Enums serialise to their value, and parse from either the value or the enum.
"""

from __future__ import annotations

import dataclasses
import json
import types
import typing
from enum import Enum
from typing import Any, TypeVar, Union, get_args, get_origin

T = TypeVar("T", bound="WireModel")

# Resolved type hints per class. get_type_hints() is not cheap and these models
# are serialised on the hot proxy path.
_HINTS: dict[type, dict[str, Any]] = {}


def _hints(cls: type) -> dict[str, Any]:
    cached = _HINTS.get(cls)
    if cached is None:
        # include_extras=False: Annotated metadata is not part of the wire format.
        cached = typing.get_type_hints(cls, include_extras=False)
        _HINTS[cls] = cached
    return cached


def _unwrap_optional(tp: Any) -> Any:
    """Return the non-None member of ``X | None``, else ``tp`` unchanged."""
    if get_origin(tp) in (Union, types.UnionType):
        args = [a for a in get_args(tp) if a is not type(None)]
        if len(args) == 1:
            return args[0]
    return tp


def _dump_value(value: Any) -> Any:
    """Convert a Python value to its JSON-compatible form."""
    if isinstance(value, WireModel):
        return value.model_dump()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (list, tuple)):
        return [_dump_value(v) for v in value]
    if isinstance(value, dict):
        return {k: _dump_value(v) for k, v in value.items()}
    return value


def _load_value(tp: Any, value: Any) -> Any:
    """Convert a decoded JSON value into the type the field declares."""
    if value is None:
        return None

    tp = _unwrap_optional(tp)
    origin = get_origin(tp)

    if origin in (list, tuple):
        args = get_args(tp)
        if not args:
            return list(value)
        return [_load_value(args[0], v) for v in value]

    if origin is dict:
        args = get_args(tp)
        if len(args) != 2:
            return dict(value)
        return {k: _load_value(args[1], v) for k, v in value.items()}

    if isinstance(tp, type):
        if issubclass(tp, WireModel):
            return tp.model_validate(value)
        if issubclass(tp, Enum):
            # Already an enum member when a caller passes objects, not JSON.
            return value if isinstance(value, tp) else tp(value)

    return value


class WireModel:
    """Mixin giving dataclasses the model API the protocol code expects.

    Subclasses must be decorated with :func:`dataclasses.dataclass`.
    """

    __slots__ = ()

    # -- serialisation -----------------------------------------------------
    def model_dump(self, *, mode: str = "python") -> dict[str, Any]:
        """Return a plain dict. ``mode`` is accepted for call-site compatibility.

        Both modes produce JSON-compatible output here: the field types in this
        protocol have no Python-only representations, so there is nothing for
        ``mode="json"`` to convert differently.
        """
        # mypy can't see that subclasses are dataclasses — the decorator is
        # applied on the subclass, not here.
        fields = dataclasses.fields(self)  # type: ignore[arg-type]
        return {f.name: _dump_value(getattr(self, f.name)) for f in fields}

    def model_dump_json(self) -> str:
        # separators: compact, and stable across Python versions.
        return json.dumps(self.model_dump(), separators=(",", ":"))

    # -- parsing -----------------------------------------------------------
    @classmethod
    def model_validate(cls: type[T], data: Any) -> T:
        """Build an instance from a mapping (or pass an instance straight through)."""
        if isinstance(data, cls):
            return data
        if not isinstance(data, dict):
            raise ValueError(f"{cls.__name__} expects a mapping, got {type(data).__name__}")

        hints = _hints(cls)
        kwargs: dict[str, Any] = {}
        for f in dataclasses.fields(cls):  # type: ignore[arg-type]
            if f.name not in data:
                # Absent field: dataclass applies the default, or errors if required.
                continue
            kwargs[f.name] = _load_value(hints.get(f.name, Any), data[f.name])

        try:
            return cls(**kwargs)
        except TypeError as exc:  # missing required field
            raise ValueError(f"{cls.__name__}: {exc}") from exc

    @classmethod
    def model_validate_json(cls: type[T], raw: str | bytes) -> T:
        return cls.model_validate(json.loads(raw))

    # -- misc --------------------------------------------------------------
    def model_copy(self: T, *, update: dict[str, Any] | None = None, deep: bool = False) -> T:
        return dataclasses.replace(self, **(update or {}))  # type: ignore[type-var]
