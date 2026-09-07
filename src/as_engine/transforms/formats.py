"""Apply the declared scalar parsers to request fields and parameters."""

from __future__ import annotations

from typing import Any

from ..converters.formats import parse
from ..params import scalar_formats
from . import Context, Transform
from .values import MISSING, set_target, target_value


class Formats(Transform):
    def request(self, context: Context, tag: Any) -> None:
        scalar_formats(context.operation)
        for rule in tag:
            value = target_value(context, rule["target"])
            if value is not MISSING:
                set_target(context, rule["target"], parse(value, rule["format"]))
