"""
Data availability semantics.

The single rule this module exists to enforce:

    Unknown data is NOT zero.

A team that genuinely scored 0 goals is real, usable data. An API that did not
return a goals-scored figure is *absent* data. Before Epic 1B.1 both arrived at
the model as `0` and were indistinguishable (GG-001).

Absence is represented as `None`. That is deliberate: it is the standard Python
signal for "no value", it is what `dict.get()` already returns for a missing key,
and unlike a sentinel object it cannot be accidentally used in arithmetic — a
mistake raises `TypeError` instead of silently producing a plausible number.
"""

from enum import Enum
from typing import Any, Iterable, Optional, Tuple

__all__ = ["DataQuality", "is_available", "missing_fields"]


class DataQuality(Enum):
    """
    Whether the data required for a given calculation is present.

    Deliberately binary. Epic 1B.1 specifies a simple availability state and
    explicitly rules out a graded quality score - a partial score invites
    proceeding on "good enough" data, which is the habit that produced GG-001.
    """

    COMPLETE = "COMPLETE"
    INCOMPLETE = "INCOMPLETE"

    @property
    def is_complete(self) -> bool:
        return self is DataQuality.COMPLETE

    @classmethod
    def from_missing(cls, missing: Iterable[str]) -> "DataQuality":
        """COMPLETE only when nothing is missing."""
        return cls.INCOMPLETE if tuple(missing) else cls.COMPLETE


def is_available(value: Optional[float]) -> bool:
    """
    True when a value was actually supplied.

    `is_available(0.0)` is True - a genuine zero is data. Use this rather than a
    truthiness check, because `if value:` treats 0.0 as absent and reintroduces
    exactly the bug this module exists to prevent.
    """
    return value is not None


def missing_fields(obj: Any, field_names: Iterable[str]) -> Tuple[str, ...]:
    """Names of the given attributes that are unavailable, in the order asked for."""
    return tuple(name for name in field_names if not is_available(getattr(obj, name, None)))
