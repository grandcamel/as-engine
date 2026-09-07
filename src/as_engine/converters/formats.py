"""Small scalar parser registry for tag-driven transforms; never reads locale."""

import re
from collections.abc import Callable
from datetime import date
from typing import Any


def parse_date(value: Any) -> str:
    """Return a real calendar date in YYYY-MM-DD form; timestamps are refused."""
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("date requires YYYY-MM-DD")
    return date.fromisoformat(value).isoformat()


def parse_duration(value: Any) -> int:
    """Parse nonnegative integer seconds or ordered w/d/h/m/s units.

    Units use elapsed time (week=7 days, day=24 hours), not Jira work calendars.
    A product needing workday semantics must register its own format name.
    """
    if type(value) is int and value >= 0:
        return value
    if not isinstance(value, str) or not value.strip():
        raise ValueError("duration requires seconds or w/d/h/m/s units")
    value = value.strip()
    if value.isascii() and value.isdecimal():
        return int(value)
    match = re.fullmatch(
        r"(?:(\d+)w\s*)?(?:(\d+)d\s*)?(?:(\d+)h\s*)?"
        r"(?:(\d+)m\s*)?(?:(\d+)s\s*)?",
        value,
    )
    if match is None or all(x is None for x in match.groups()):
        raise ValueError("duration requires ordered, nonrepeated w/d/h/m/s units")
    return sum(
        int(count or 0) * scale
        for count, scale in zip(match.groups(), [604800, 86400, 3600, 60, 1])
    )


PARSERS: dict[str, Callable[[Any], Any]] = {"date": parse_date, "duration": parse_duration}


def parse(value: Any, format: str) -> Any:
    """Apply a named parser; an unknown format is an explicit usage error."""
    if format not in PARSERS:
        raise ValueError(f"Unknown scalar format: {format}")
    return PARSERS[format](value)
