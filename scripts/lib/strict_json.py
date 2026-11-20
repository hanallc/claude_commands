"""Share JSON parser hooks for strict documents."""
from __future__ import annotations

from math import isfinite
from typing import NoReturn, cast


class DuplicateJsonKey(ValueError):
    """A JSON object contains the same key more than once."""

    def __init__(self, key: str) -> None:
        self.key: str = key
        super().__init__(f'Duplicate JSON key: {key}')


def unique_json_fields(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Build a JSON object and reject duplicate keys."""
    fields: dict[str, object] = {}
    for key, value in pairs:
        if key in fields:
            raise DuplicateJsonKey(key)
        fields[key] = value
    return fields


def reject_json_constant(value: str) -> NoReturn:
    """Reject NaN and infinity tokens in a JSON document."""
    raise ValueError(f'invalid JSON constant: {value}')


def require_finite_json_numbers(value: object) -> None:
    """Reject nonfinite numbers at every depth in a JSON document."""
    if isinstance(value, float) and not isfinite(value):
        raise ValueError('JSON numbers must be finite')
    if isinstance(value, dict):
        for key, item in cast(dict[object, object], value).items():
            require_finite_json_numbers(key)
            require_finite_json_numbers(item)
    elif isinstance(value, list | tuple):
        for item in cast('list[object] | tuple[object, ...]', value):
            require_finite_json_numbers(item)
